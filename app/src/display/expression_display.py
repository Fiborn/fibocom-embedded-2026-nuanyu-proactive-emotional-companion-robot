#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Nuanyu HDMI expression-screen renderer — native Wayland (wl_shell) + cairo

Why not firefox/GTK/WebKit:
  weston 8.0.0 on this board has an incomplete xdg-shell stable
  implementation (resize is missing arguments), and the firefox 136 /
  GTK 3.24 / WebKit2GTK 4.0 clients all crash on protocol errors.
  wl_shell is weston 8's native protocol and fully supported
  (verified with weston-simple-shm).

The board's libwayland-client is a stripped build (no protocol stub
symbols), so this file constructs wayland protocol requests directly
through the generic wl_proxy_marshal* mechanism.

⚠️ Stripped-libwayland pitfalls on this board (all worked around):
  - wl_display_roundtrip depends on the removed wl_display_sync → segfault
    → handshake with wl_display_dispatch instead
  - wl_proxy_marshal_array_constructor's 'n' argument does not allocate an
    object id automatically
    → use the wl_proxy_marshal_constructor (varargs) variant
  - ⚠️ The stripped libwayland here *consumes* one va_arg for 'n' (the
    standard build does not; measured: fcntl(4096) reads size as fd).
    The varargs must therefore pass a placeholder (None) for every 'n'
    so the following arguments stay aligned.

Usage (invoked by launch_display.sh):
  XDG_RUNTIME_DIR=/run/user/root WAYLAND_DISPLAY=wayland-0 python3 expression_display.py

Expression state comes from http://127.0.0.1:5004/api/status (polled every 400ms).
"""
import ctypes
import os
import sys
import time
import json
import math
import mmap
import urllib.request

import cairo

# ═══════════════════════════ wayland-client ctypes bindings ═══════════════════════

_wl = ctypes.CDLL("libwayland-client.so.0")
P = ctypes.c_void_p

_KEEPALIVE = []  # Hold message/types array refs so ctypes structs (pointers) survive GC


class _WlMessage(ctypes.Structure):
    _fields_ = [("name", ctypes.c_char_p), ("signature", ctypes.c_char_p),
                ("types", ctypes.POINTER(P))]


class _WlInterface(ctypes.Structure):
    _fields_ = [("name", ctypes.c_char_p), ("version", ctypes.c_int),
                ("method_count", ctypes.c_int), ("methods", ctypes.POINTER(_WlMessage)),
                ("event_count", ctypes.c_int), ("events", ctypes.POINTER(_WlMessage))]


def _msg(name, sig):
    """Build a wl_message.  The board's stripped demarshaller always uses the
    types array (NULL means segfault), so every argument gets a non-NULL
    array (a NULL element means "any object")."""
    n = len(sig)
    arr = (P * max(n, 1))()
    _KEEPALIVE.append(arr)
    return _WlMessage(name.encode(), sig.encode(), ctypes.cast(arr, ctypes.POINTER(P)))


def _iface(name, version, methods, events):
    methods_arr = (_WlMessage * max(len(methods), 1))(*methods) if methods else None
    events_arr = (_WlMessage * max(len(events), 1))(*events) if events else None
    _KEEPALIVE.append(methods_arr)
    _KEEPALIVE.append(events_arr)
    return _WlInterface(name.encode(), version,
                        len(methods), ctypes.cast(methods_arr, ctypes.POINTER(_WlMessage)) if methods_arr else None,
                        len(events), ctypes.cast(events_arr, ctypes.POINTER(_WlMessage)) if events_arr else None)


# ── Protocol interface definitions (wayland.xml, compatible with the board libwayland) ──

_wl_display_iface = _iface("wl_display", 1,
                           [_msg("sync", "n"), _msg("get_registry", "n")],
                           [_msg("error", "ous"), _msg("delete_id", "u")])

_wl_registry_iface = _iface("wl_registry", 1,
                            [_msg("bind", "usun")],
                            [_msg("global", "usu"), _msg("global_remove", "u")])

_wl_compositor_iface = _iface("wl_compositor", 4,
                              [_msg("create_surface", "n"), _msg("create_region", "n")], [])

_wl_shell_iface = _iface("wl_shell", 1,
                         [_msg("get_shell_surface", "no")], [])

_wl_shell_surface_iface = _iface("wl_shell_surface", 1,
                                 [_msg("pong", "u"), _msg("move", "ou"), _msg("resize", "ouu"),
                                  _msg("set_toplevel", ""), _msg("set_transient", "ouu"),
                                  _msg("set_fullscreen", "uuo"), _msg("set_popup", "ououo"),
                                  _msg("set_maximized", "o"), _msg("set_title", "s"),
                                  _msg("set_class", "s")],
                                 [_msg("ping", "u"), _msg("configure", "uii"), _msg("popup_done", "")])

_wl_shm_iface = _iface("wl_shm", 1,
                       [_msg("create_pool", "nhi")],
                       [_msg("format", "u")])

_wl_shm_pool_iface = _iface("wl_shm_pool", 1,
                            [_msg("create_buffer", "niiiiu"), _msg("destroy", ""), _msg("resize", "i")], [])

_wl_buffer_iface = _iface("wl_buffer", 1,
                          [_msg("destroy", "")],
                          [_msg("release", "")])

_wl_surface_iface = _iface("wl_surface", 4,
                           [_msg("destroy", ""), _msg("attach", "oii"), _msg("damage", "iiii"),
                            _msg("frame", "n"), _msg("set_opaque_region", "o"),
                            _msg("set_input_region", "o"), _msg("commit", ""),
                            _msg("set_buffer_transform", "i"), _msg("set_buffer_scale", "i"),
                            _msg("damage_buffer", "iiii"), _msg("offset", "ii")],
                           [_msg("enter", "o"), _msg("leave", "o")])

_wl_output_iface = _iface("wl_output", 3, [],
                          [_msg("geometry", "iiiiiss"), _msg("mode", "uiii"),
                           _msg("done", ""), _msg("scale", "i")])


# ── libwayland core functions ──
def _proto(name, restype, argtypes):
    fn = getattr(_wl, name)
    fn.restype = restype
    fn.argtypes = argtypes
    return fn


_display_connect = _proto("wl_display_connect", P, [ctypes.c_char_p])
# wl_display_roundtrip depends on the removed wl_display_sync and segfaults; not used.
_display_dispatch = _proto("wl_display_dispatch", ctypes.c_int, [P])
_display_dispatch_pending = _proto("wl_display_dispatch_pending", ctypes.c_int, [P])
_display_flush = _proto("wl_display_flush", ctypes.c_int, [P])
# varargs marshal variant ('n' object ids are allocated by the marshaller, no va_arg consumed)
_marshal_var = _proto("wl_proxy_marshal", None, [P, ctypes.c_uint32])
_marshal_var_ctor = _proto("wl_proxy_marshal_constructor", P, [P, ctypes.c_uint32, P])
_proxy_add_listener = _proto("wl_proxy_add_listener", ctypes.c_int, [P, P, P])
_proxy_destroy = _proto("wl_proxy_destroy", None, [P])


# ── Event callback types ──
# wayland listener calling convention: (data, proxy, args...)
_GlobalFn = ctypes.CFUNCTYPE(None, P, P, ctypes.c_uint32, ctypes.c_char_p, ctypes.c_uint32)
_RemoveFn = ctypes.CFUNCTYPE(None, P, P, ctypes.c_uint32)
_PingFn = ctypes.CFUNCTYPE(None, P, P, ctypes.c_uint32)
_ConfigureFn = ctypes.CFUNCTYPE(None, P, P, ctypes.c_uint32, ctypes.c_int32, ctypes.c_int32)
_PopupFn = ctypes.CFUNCTYPE(None, P, P)


class _RegistryListener(ctypes.Structure):
    _fields_ = [("global", _GlobalFn), ("global_remove", _RemoveFn)]


class _ShellSurfaceListener(ctypes.Structure):
    _fields_ = [("ping", _PingFn), ("configure", _ConfigureFn), ("popup_done", _PopupFn)]


def _obj(x):
    """proxy pointer → c_void_p (varargs must wrap it as a pointer to avoid 32-bit truncation)"""
    return P(x) if x else None


# ═══════════════════════════ State and expression mapping ═══════════════════════════

STATUS_URL = "http://127.0.0.1:5004/api/status"
POLL_INTERVAL = 0.4          # status poll interval (s)
FRAME_INTERVAL = 1.0 / 30.0  # animation frame rate
TRANSITION_STEP = 0.04       # expression transition speed (same as display.html)

COLORS = {
    "idle":      ((240, 192, 96), (232, 168, 48)),
    "welcome":   ((245, 208, 128), (240, 160, 32)),
    "listening": ((128, 200, 224), (80, 160, 192)),
    "thinking":  ((192, 176, 224), (144, 112, 192)),
    "speaking":  ((240, 192, 96), (232, 168, 48)),
    "happy":     ((248, 216, 96), (240, 160, 32)),
    "tired":     ((192, 208, 224), (128, 152, 176)),
    "focus":     ((128, 224, 192), (64, 176, 128)),
    "sleep":     ((96, 128, 160), (64, 88, 112)),
}

_ctx = {"fetch_errors": 0}


def _fetch_status():
    try:
        with urllib.request.urlopen(STATUS_URL, timeout=1.5) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def map_emotion(s):
    """Map /api/status to an expression name (same priority order as display.html)"""
    if s.get("assistant_sleeping"):
        return "sleep"
    if s.get("study_running"):
        return "focus"
    if s.get("ai_pending"):
        return "thinking"
    tts = s.get("tts") or {}
    if "speak" in str(tts.get("state") or ""):
        return "speaking"
    if s.get("listening"):
        return "listening"
    emo = (s.get("current_emotion") or "neutral").lower()
    if emo == "happy":
        return "happy"
    if emo in ("sad", "angry", "fear"):
        return "tired"
    if emo == "surprise":
        return "happy"
    if s.get("return_prompt"):
        return "welcome"
    return "idle"


# ═══════════════════════════ cairo expression drawing ═══════════════════════════

def _rgb(color):
    return color[0] / 255.0, color[1] / 255.0, color[2] / 255.0


def _ellipse_fill(cr, cx, cy, rx, ry, color, alpha=1.0):
    """Filled ellipse (scale affects only the path, not later line widths)"""
    cr.save()
    cr.translate(cx, cy)
    cr.scale(1.0, ry / rx if rx else 1.0)
    cr.arc(0, 0, rx, 0, 2 * math.pi)
    cr.restore()
    r, g, b = _rgb(color)
    cr.set_source_rgba(r, g, b, alpha)
    cr.fill()


def _round_line(cr, x1, y1, x2, y2, width, color):
    cr.set_source_rgb(*_rgb(color))
    cr.set_line_width(width)
    cr.set_line_cap(cairo.LINE_CAP_ROUND)
    cr.move_to(x1, y1)
    cr.line_to(x2, y2)
    cr.stroke()


def draw_face(cr, W, H, t, emotion, progress):
    cx, cy = W / 2.0, H / 2.0
    R = min(W, H) * 0.32
    fill, stroke = COLORS.get(emotion, COLORS["idle"])

    cr.set_source_rgb(0x0D / 255.0, 0x0F / 255.0, 0x10 / 255.0)
    cr.paint()

    # Rotate the whole frame 180° (the panel is mounted upside down)
    cr.save()
    cr.translate(W / 2, H / 2)
    cr.rotate(math.pi)
    cr.translate(-W / 2, -H / 2)

    # Glow
    glow = cairo.RadialGradient(cx, cy, R * 0.6, cx, cy, R * 1.5)
    r, g, b = _rgb(fill)
    glow.add_color_stop_rgba(0, r, g, b, 0.15)
    glow.add_color_stop_rgba(1, 0, 0, 0, 0)
    cr.set_source(glow)
    cr.arc(cx, cy, R * 1.8, 0, 2 * math.pi)
    cr.fill()

    # Body circle (breathing / bouncing)
    breathe = math.sin(t * 0.02) * 0.04 + 1.0 if emotion == "sleep" else 1.0
    bounce = math.sin(t * 0.06) * 0.03 + 1.0 if emotion == "happy" else 1.0
    scale = breathe * bounce * (1 + progress * 0.02 * math.sin(t * 0.1))
    r = R * scale

    # Shadow
    _ellipse_fill(cr, cx, cy + R * 0.15, r * 0.95, r * 0.12, (0, 0, 0), 0.3)

    # Main circle
    body_y = cy - R * 0.05 * progress
    cr.set_source_rgb(*_rgb(fill))
    cr.arc(cx, body_y, r, 0, 2 * math.pi)
    cr.fill()
    cr.set_source_rgb(*_rgb(stroke))
    cr.set_line_width(R * 0.06)
    cr.arc(cx, body_y, r, 0, 2 * math.pi)
    cr.stroke()

    # ── Eyes ──
    eye_y = cy - R * 0.15
    eye_spacing = R * 0.35
    eye_r = R * 0.14
    WHITE = (250, 250, 248)
    PUPIL = (26, 26, 26)

    def draw_eye(ex, ey, side):
        if emotion == "happy":
            _ellipse_fill(cr, ex, ey, eye_r * 1.1, eye_r * 0.5, WHITE)  # ^_^
        elif emotion == "tired":
            _ellipse_fill(cr, ex, ey, eye_r * 1.1, eye_r * 0.3, WHITE)  # -_-
        elif emotion == "sleep":
            cr.set_source_rgb(*_rgb(stroke))
            cr.rectangle(ex - eye_r, ey, eye_r * 2, R * 0.04)           # _ _
            cr.fill()
            return
        else:
            _ellipse_fill(cr, ex, ey, eye_r * 1.05, eye_r * 1.2, WHITE)
        if emotion == "thinking":
            look_x = -eye_r * 0.4 if side == "left" else eye_r * 0.4
        else:
            look_x = 0.0
        look_y = -eye_r * 0.2 if emotion == "listening" else (eye_r * 0.3 if emotion == "tired" else 0.0)
        pupil_r = eye_r * 0.52
        cr.set_source_rgb(*_rgb(PUPIL))
        cr.arc(ex + look_x, ey + look_y, pupil_r, 0, 2 * math.pi)
        cr.fill()
        cr.set_source_rgb(1, 1, 1)
        cr.arc(ex + look_x - pupil_r * 0.3, ey + look_y - pupil_r * 0.3, pupil_r * 0.28, 0, 2 * math.pi)
        cr.fill()

    draw_eye(cx - eye_spacing, eye_y, "left")
    draw_eye(cx + eye_spacing, eye_y, "right")

    # ── Eyebrows ──
    brow_y = eye_y - eye_r * 1.5
    brow_w = R * 0.05

    def draw_brow(bx, by, side):
        if emotion in ("tired", "sleep"):
            angle = 0.15
        elif emotion == "happy":
            angle = -0.1
        elif emotion == "focus":
            angle = -0.25
        elif emotion == "thinking":
            angle = -0.1 if side == "left" else 0.25
        else:
            angle = 0.0
        ln = eye_r * 1.6
        dx = math.cos(angle - math.pi / 2) * ln
        dy = math.sin(angle - math.pi / 2) * ln
        _round_line(cr, bx - dx / 2, by - dy / 2, bx + dx / 2, by + dy / 2, brow_w, stroke)

    draw_brow(cx - eye_spacing, brow_y, "left")
    draw_brow(cx + eye_spacing, brow_y, "right")

    # ── Mouth ──
    mouth_y = cy + R * 0.25
    mouth_w = R * 0.05
    cr.set_source_rgb(*_rgb(stroke))
    cr.set_line_width(mouth_w)
    cr.set_line_cap(cairo.LINE_CAP_ROUND)

    if emotion == "happy":
        cr.arc(cx, mouth_y - R * 0.05, R * 0.2, 0.2, math.pi - 0.2)
        cr.stroke()
    elif emotion == "tired":
        cr.arc(cx, mouth_y + R * 0.15, R * 0.15, math.pi + 0.3, -0.3)
        cr.stroke()
    elif emotion == "speaking":
        open_amt = math.sin(t * 0.15) * 0.3 + 0.4
        _ellipse_fill(cr, cx, mouth_y, R * 0.12, R * 0.08 * open_amt, stroke)
    elif emotion == "sleep":
        _ellipse_fill(cr, cx, mouth_y, R * 0.06, R * 0.05, stroke)
    elif emotion == "thinking":
        cr.arc(cx + R * 0.08, mouth_y, R * 0.08, 0.5, math.pi * 1.3)
        cr.stroke()
    elif emotion == "focus":
        _round_line(cr, cx - R * 0.12, mouth_y, cx + R * 0.12, mouth_y, mouth_w, stroke)
    else:
        cr.arc(cx, mouth_y, R * 0.15, 0.15, math.pi - 0.15)
        cr.stroke()

    # ── Blush ──
    if emotion in ("happy", "welcome"):
        blush_y = cy + R * 0.05
        for side in (-1, 1):
            _ellipse_fill(cr, cx + side * R * 0.5, blush_y, R * 0.12, R * 0.07, stroke, 0.18)

    # ── Thinking dots ──
    if emotion == "thinking":
        dot_x, dot_base_y = cx + R * 0.7, cy - R * 0.5
        for i in range(3):
            alpha = ((t * 0.05 + i * 0.33) % 1.0) * 0.6 + 0.2
            r_, g_, b_ = _rgb(stroke)
            cr.set_source_rgba(r_, g_, b_, alpha)
            cr.arc(dot_x, dot_base_y - i * R * 0.15, R * 0.04, 0, 2 * math.pi)
            cr.fill()

    # ── Zzz (sleeping) ──
    if emotion == "sleep":
        zx, zy = cx + R * 0.75, cy - R * 0.6
        r_, g_, b_ = _rgb(stroke)
        cr.select_font_face("Noto Serif CJK SC", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
        cr.set_font_size(R * 0.18)
        for i in range(3):
            alpha = (t * 0.03 + i * 0.4) % 1.0
            cr.set_source_rgba(r_, g_, b_, alpha)
            cr.move_to(zx + i * R * 0.1, zy - i * R * 0.18)
            cr.show_text("z")

    # ── Offline hint ──
    if _ctx["fetch_errors"] > 2:
        cr.set_source_rgba(1.0, 0.31, 0.24, 0.8)
        cr.select_font_face("Noto Serif CJK SC", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
        cr.set_font_size(R * 0.1)
        cr.move_to(cx, cy + R * 1.3)
        cr.show_text("等待连接…")

    cr.restore()  # undo the 180° rotation


# ═══════════════════════════ Wayland session ═══════════════════════════

class DisplaySession(object):
    def __init__(self):
        self.display = None
        self.shell_surface = None
        self.surface = None
        self.shm = None
        self.output = None
        # Panel is 800x480 (HX8399); the LT8912 EDID claims 1080p, so the canvas
        # must be pinned to match the panel
        self.width = 800
        self.height = 480
        self._registry_listener = None
        self._shell_listener = None
        self._compositor_name = None
        self._shell_name = None
        self._shm_name = None
        self._output_name = None

    def _on_global(self, data, proxy, name, interface, version):
        # NOTE: the bound method's first positional parameter is taken by self,
        # so the real argument order must match the CFUNCTYPE argtypes
        # (data, proxy, name, interface, version)
        if interface == b"wl_compositor":
            self._compositor_name = name
        elif interface == b"wl_shell":
            self._shell_name = name
        elif interface == b"wl_shm":
            self._shm_name = name
        elif interface == b"wl_output":
            self._output_name = name

    def _on_ping(self, data, proxy, serial):
        _marshal_var(self.shell_surface, 0, ctypes.c_uint32(serial))  # pong

    def _on_configure(self, data, proxy, edges, w, h):
        # Pinned to 800x480: the LT8912 EDID reports 1080p, but the panel is
        # actually 800x480, so the canvas must match the panel rather than
        # follow weston's 1080p output.
        self.width, self.height = 800, 480

    def connect(self):
        self.display = _display_connect(None)
        if not self.display:
            raise RuntimeError("wl_display_connect failed (XDG_RUNTIME_DIR/WAYLAND_DISPLAY?)")

        # wl_display.get_registry (opcode 1; opcode 0 = sync)
        # NOTE: on this board 'n' consumes a va_arg, so a placeholder None is required
        registry = _marshal_var_ctor(self.display, 1, ctypes.byref(_wl_registry_iface), None)
        self._registry_listener = _RegistryListener(_GlobalFn(self._on_global),
                                                    _RemoveFn(lambda *a: None))
        _proxy_add_listener(registry, ctypes.byref(self._registry_listener), None)
        _display_flush(self.display)

        # Wait for the compositor/shell/shm globals (wl_display_dispatch blocks and processes)
        deadline = time.time() + 5.0
        while not (self._compositor_name and self._shell_name and self._shm_name):
            if time.time() > deadline:
                raise RuntimeError("timeout waiting for wayland globals")
            _display_dispatch(self.display)

        # wl_registry.bind (opcode 0): name(u), interface(s), version(u), id(n)
        # the trailing 'n' also consumes a va_arg (placeholder None)
        compositor = _marshal_var_ctor(registry, 0, ctypes.byref(_wl_compositor_iface),
                                       ctypes.c_uint32(self._compositor_name),
                                       ctypes.c_char_p(b"wl_compositor"),
                                       ctypes.c_uint32(4), None)
        self.shell = _marshal_var_ctor(registry, 0, ctypes.byref(_wl_shell_iface),
                                       ctypes.c_uint32(self._shell_name),
                                       ctypes.c_char_p(b"wl_shell"),
                                       ctypes.c_uint32(1), None)
        self.shm = _marshal_var_ctor(registry, 0, ctypes.byref(_wl_shm_iface),
                                     ctypes.c_uint32(self._shm_name),
                                     ctypes.c_char_p(b"wl_shm"),
                                     ctypes.c_uint32(1), None)
        if self._output_name:
            self.output = _marshal_var_ctor(registry, 0, ctypes.byref(_wl_output_iface),
                                            ctypes.c_uint32(self._output_name),
                                            ctypes.c_char_p(b"wl_output"),
                                            ctypes.c_uint32(3), None)
        else:
            self.output = None

        # wl_compositor.create_surface (opcode 0)
        self.surface = _marshal_var_ctor(compositor, 0, ctypes.byref(_wl_surface_iface), None)
        # wl_shell.get_shell_surface (opcode 0): id(n), surface(o)
        self.shell_surface = _marshal_var_ctor(self.shell, 0, ctypes.byref(_wl_shell_surface_iface),
                                               None, _obj(self.surface))
        self._shell_listener = _ShellSurfaceListener(
            ping=_PingFn(self._on_ping),
            configure=_ConfigureFn(self._on_configure),
            popup_done=_PopupFn(lambda *a: None),
        )
        _proxy_add_listener(self.shell_surface, ctypes.byref(self._shell_listener), None)
        # wl_shell_surface.set_fullscreen (opcode 5): method, framerate, output
        # the stripped build requires a non-NULL output
        _marshal_var(self.shell_surface, 5, ctypes.c_uint32(0), ctypes.c_uint32(0), _obj(self.output))
        _display_flush(self.display)

    def dispatch(self):
        _display_dispatch_pending(self.display)
        _display_flush(self.display)


class DoubleBuffer(object):
    """Double-buffered shm buffers (each slot: buffer, pool, width, height, mmap)"""

    def __init__(self, session):
        self.session = session
        self._slots = [None, None]
        self._idx = 0
        self._realloc()

    def _realloc(self):
        self._free()
        w, h = self.session.width, self.session.height
        stride = w * 4
        for i in range(2):
            fd = os.memfd_create("nuanyu-display", 0)
            os.ftruncate(fd, stride * h)
            # wl_shm.create_pool (opcode 0): id(n), fd(h), size(i)
            pool = _marshal_var_ctor(self.session.shm, 0, ctypes.byref(_wl_shm_pool_iface),
                                     None, ctypes.c_int32(fd), ctypes.c_int32(stride * h))
            # wl_shm_pool.create_buffer (opcode 0): id(n), offset, w, h, stride, format
            buf = _marshal_var_ctor(pool, 0, ctypes.byref(_wl_buffer_iface),
                                    None, ctypes.c_int32(0), ctypes.c_int32(w), ctypes.c_int32(h),
                                    ctypes.c_int32(stride), ctypes.c_uint32(0))  # ARGB8888
            mm = mmap.mmap(fd, stride * h)
            os.close(fd)
            self._slots[i] = (buf, pool, w, h, mm)

    def _free(self):
        for slot in self._slots:
            if slot:
                _marshal_var(slot[0], 0)         # wl_buffer.destroy
                _marshal_var(slot[1], 1)         # wl_shm_pool.destroy
                slot[4].close()
        self._slots = [None, None]

    def ensure_size(self):
        if self._slots[0] and self._slots[0][2] == self.session.width and self._slots[0][3] == self.session.height:
            return
        self._realloc()

    def current(self):
        return self._slots[self._idx]

    def swap(self):
        self._idx = 1 - self._idx


def main():
    session = DisplaySession()
    session.connect()
    print("[display] connected, surface=%s (%dx%d)" % (hex(session.shell_surface), session.width, session.height),
          flush=True)

    db = DoubleBuffer(session)
    cur_emotion = "idle"
    target_emotion = "idle"
    progress = 1.0
    t = 0.0
    last_poll = 0.0
    last_frame = 0.0

    while True:
        now = time.time()

        # Status polling
        if now - last_poll >= POLL_INTERVAL:
            last_poll = now
            s = _fetch_status()
            if s is not None:
                _ctx["fetch_errors"] = 0
                tgt = map_emotion(s)
                if tgt != target_emotion:
                    target_emotion = tgt
                    cur_emotion = target_emotion
                    progress = 0.0
            else:
                _ctx["fetch_errors"] += 1

        # Frame-rate control
        if now - last_frame < FRAME_INTERVAL:
            time.sleep(0.004)
            session.dispatch()
            continue
        last_frame = now
        t += 1.0

        if progress < 1.0:
            progress = min(1.0, progress + TRANSITION_STEP)

        db.ensure_size()
        w, h = session.width, session.height
        buf, pool, _, _, mm = db.current()

        surface = cairo.ImageSurface.create_for_data(memoryview(mm), cairo.FORMAT_ARGB32, w, h, w * 4)
        cr = cairo.Context(surface)
        draw_face(cr, w, h, t, cur_emotion, progress)
        surface.flush()

        # wl_surface.attach(1) / damage(2) / commit(6)
        _marshal_var(session.surface, 1, _obj(buf), ctypes.c_int32(0), ctypes.c_int32(0))
        _marshal_var(session.surface, 2, ctypes.c_int32(0), ctypes.c_int32(0),
                     ctypes.c_int32(w), ctypes.c_int32(h))
        _marshal_var(session.surface, 6)
        session.dispatch()
        db.swap()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
