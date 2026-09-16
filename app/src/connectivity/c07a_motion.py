"""Optional, fail-safe high-level UART client for the C07A motion controller."""
from __future__ import annotations
import collections
import logging
import os
import select
try:
    import termios
except ImportError:  # Host-side Windows tests intentionally have no POSIX UART.
    termios = None
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Deque, Optional

LOG = logging.getLogger("nuanyu.c07a")
class MotionAction(str, Enum):
    STOP="STOP"; GREET="GREET"; NOD="NOD"; SHAKE="SHAKE"; THINK="THINK"; HAPPY="HAPPY"; COMFORT="COMFORT"; REMIND="REMIND"; ALERT="ALERT"; OFFLINE="OFFLINE"; GOODBYE="GOODBYE"

_SPEECH_SCENE_ACTIONS = {
    "wakeup": MotionAction.GREET,
    "proactive_greeting": MotionAction.GREET,
    "happy": MotionAction.HAPPY,
    "proactive_emotion": MotionAction.COMFORT,
    "proactive_sit": MotionAction.REMIND,
    "proactive_low": MotionAction.REMIND,
    "reminder": MotionAction.REMIND,
    "sensor_report": MotionAction.REMIND,
    "user_leave": MotionAction.GOODBYE,
}


def speech_motion_action(scene: str = "chat", mood: str = "") -> MotionAction:
    """Return the stable, short in-place gesture for an assistant utterance.

    The mapping intentionally uses interaction scenes rather than attempting
    to classify every generated sentence.  This keeps the physical response
    predictable and prevents a verbose reply from producing repeated motion.
    """
    key = str(scene or "chat").strip().lower()
    if key == "mood":
        mood_key = str(mood or "").strip()
        if mood_key in ("开心", "高兴"):
            return MotionAction.HAPPY
        if mood_key in ("焦虑", "疲惫", "崩溃", "难过"):
            return MotionAction.COMFORT
    return _SPEECH_SCENE_ACTIONS.get(key, MotionAction.NOD)


@dataclass(frozen=True)
class MotionConfig:
    enabled: bool=False; mock: bool=True; port: str=""; baud: int=115200; ack_timeout_s: float=0.8; done_timeout_s: float=4.0; heartbeat_s: float=0.5
    @classmethod
    def from_env(cls) -> "MotionConfig":
        enabled=os.environ.get("MOTION_FEEDBACK_ENABLED",os.environ.get("C07A_MOTION_ENABLED","false")).strip().lower()=="true"
        return cls(enabled=enabled,mock=os.environ.get("C07A_MOTION_MOCK","true").strip().lower()=="true",port=os.environ.get("C07A_MOTION_PORT","").strip(),baud=int(os.environ.get("C07A_MOTION_BAUD","115200")),ack_timeout_s=float(os.environ.get("C07A_ACK_TIMEOUT_S","0.8")),done_timeout_s=float(os.environ.get("C07A_DONE_TIMEOUT_S","4.0")),heartbeat_s=float(os.environ.get("C07A_HEARTBEAT_S","0.5")))
class C07AMotionClient:
    """C07A is optional: any transport failure stops/closes only this client."""
    def __init__(self, config: Optional[MotionConfig]=None,event_sink: Optional[Callable[[str,str],None]]=None):
        self.config=config or MotionConfig.from_env(); self._event_sink=event_sink; self._fd=None; self._lock=threading.RLock(); self._rx=bytearray(); self._pending=None; self._queued=None; self._acked=False; self._pending_at=0.0; self._last_heartbeat=0.0; self._history:Deque[str]=collections.deque(maxlen=40); self._connected=False; self._last_error=""; self._next_retry_at=0.0
    def start(self)->bool:
        with self._lock:
            if not self.config.enabled: self._record("disabled"); return False
            if self._connected:return True
            if self.config.mock:self._connected=True; self._record("mock connected"); return True
            now=time.monotonic()
            if now<self._next_retry_at:return False
            if not self.config.port:return self._fail_retry("C07A_MOTION_PORT is empty")
            try:
                self._fd=self._open_uart(self.config.port,self.config.baud)
                self._connected=True; self._record("uart connected"); return True
            except OSError as exc:return self._fail_retry("UART open failed: %s"%exc)
    def emergency_stop(self, reason="", await_reply=True, clear_queue=True) -> bool:
        with self._lock:
            if clear_queue:
                self._queued = None
            if not self._connected:return False
            # Keep STOP as the pending command so its ACK/DONE replies are
            # consumed normally instead of being mistaken for unsolicited
            # traffic and retriggering the emergency stop forever.
            if await_reply and not self.config.mock:
                self._pending=MotionAction.STOP; self._acked=False; self._pending_at=time.monotonic()
            else:
                self._pending=None; self._acked=False
            try:
                self._write("ACT:STOP\n")
                self._record("tx ACT:STOP reason=%s"%reason[:80])
                if not await_reply or self.config.mock:
                    self._pending=None; self._acked=False
                return True
            except OSError as exc:self._fail("STOP write failed: %s"%exc); return False
    def close(self)->None:
        with self._lock:
            self.emergency_stop("client close")
            if self._fd is not None:
                try:os.close(self._fd)
                except OSError:pass
            self._fd=None; self._connected=False; self._pending=None; self._queued=None; self._acked=False
    def trigger_after_stop(self, action: MotionAction|str, reason="") -> bool:
        """Run one gesture after the current gesture completes.

        TTS may begin while THINK or another gesture is still active. C07A
        firmware revisions do not all support preempting THINK with STOP, so
        speech gestures are queued behind the current DONE frame instead of
        forcing a STOP that can be reported as ERR:UNKNOWN. Only one
        successor is retained; the newest speech event wins.
        """
        try: action=action if isinstance(action,MotionAction) else MotionAction(str(action).upper())
        except ValueError:self._record("reject unknown action=%s"%action); return False
        with self._lock:
            if action is MotionAction.STOP:return self.emergency_stop(reason)
            if not self.config.enabled or (not self._connected and not self.start()):return False
            if self._pending is None:
                return self._start_action_locked(action, reason)
            self._queued = (action, reason)
            self._record("queued %s after %s reason=%s" % (
                action.value, self._pending.value, reason[:80]
            ))
            return True
    def trigger(self, action: MotionAction|str, reason="")->bool:
        try: action=action if isinstance(action,MotionAction) else MotionAction(str(action).upper())
        except ValueError:self._record("reject unknown action=%s"%action); return False
        with self._lock:
            if not self.config.enabled or (not self._connected and not self.start()):return False
            if action is MotionAction.STOP:return self.emergency_stop(reason)
            if self._pending is not None:self._record("reject busy=%s action=%s"%(self._pending.value,action.value)); return False
            return self._start_action_locked(action, reason)
    def _start_action_locked(self, action, reason):
        try:self._write("ACT:%s\n"%action.value)
        except OSError as exc:self._fail("action write failed: %s"%exc); self.close(); return False
        self._pending=action; self._acked=False; self._pending_at=time.monotonic(); self._record("tx ACT:%s reason=%s"%(action.value,reason[:80])); return True
    def _start_queued_action_locked(self):
        if self._queued is None or self._pending is not None:
            return self._pending is not None or self._queued is None
        action, reason = self._queued
        self._queued = None
        return self._start_action_locked(action, reason)
    def poll(self)->None:
        with self._lock:
            if not self.config.enabled or not self._connected:return
            if self.config.mock:
                if self._pending:
                    self._record("mock ACK:%s DONE:%s"%(self._pending.value,self._pending.value)); self._pending=None; self._acked=False; self._start_queued_action_locked()
                return
            try:
                self._read_lines(); now=time.monotonic()
                if self._pending and not self._acked and now-self._pending_at>self.config.ack_timeout_s:self._fail("ACK timeout for %s"%self._pending.value); self.emergency_stop("ACK timeout", await_reply=False)
                elif self._pending and now-self._pending_at>self.config.done_timeout_s:self._fail("DONE timeout for %s"%self._pending.value); self.emergency_stop("DONE timeout", await_reply=False)
                if now-self._last_heartbeat>=self.config.heartbeat_s:self._write("PING\n"); self._last_heartbeat=now
            except OSError as exc:self._fail("UART I/O failed: %s"%exc); self.close()
    def health(self)->dict:
        if not self._lock.acquire(timeout=0.05):
            return {"enabled":self.config.enabled,"mock":self.config.mock,"connected":self._connected,"port":self.config.port or None,"baud":self.config.baud,"pending":None,"queued":None,"last_error":"status lock busy","history":[]}
        try:return {"enabled":self.config.enabled,"mock":self.config.mock,"connected":self._connected,"port":self.config.port or None,"baud":self.config.baud,"pending":self._pending.value if self._pending else None,"queued":self._queued[0].value if self._queued else None,"last_error":self._last_error or None,"history":list(self._history)}
        finally:self._lock.release()
    def _read_lines(self):
        if self._fd is None:return
        ready,_,_=select.select([self._fd],[],[],0)
        if self._fd not in ready:return
        data=os.read(self._fd,512)
        if not data:raise OSError("C07A UART EOF")
        self._rx.extend(data)
        while b"\n" in self._rx:
            raw,_,tail=self._rx.partition(b"\n"); self._rx=bytearray(tail); self._handle_line(raw.decode("ascii",errors="replace").strip())
        if len(self._rx)>256:self._fail("oversized UART line"); self.emergency_stop("RX overflow", await_reply=False); self._rx.clear()
    def _handle_line(self,line):
        if not line:return
        self._record("rx %s"%line)
        if line.startswith("ACK:"):
            reply_action=line[4:]
            if self._pending and reply_action==self._pending.value:
                self._acked=True
                self._last_error=""
            elif reply_action==MotionAction.STOP.value or self._pending is MotionAction.STOP:
                # A STOP may preempt an action whose ACK/DONE is still in the
                # UART buffer.  STOP is also deliberately fire-and-forget on
                # failure paths, so its reply is benign when no command is
                # pending.  Do not turn either case into a STOP feedback loop.
                self._record("ignore stale %s"%line)
            elif self._pending is None:
                # Action was interrupted or timed out (pending already cleared);
                # drop every late ACK instead of cascading into emergency_stop.
                self._record("ignore late ACK %s"%line)
            else:
                self._fail("unexpected ACK: %s"%line); self.emergency_stop("bad ACK", await_reply=False)
        elif line.startswith("DONE:"):
            reply_action=line[5:]
            if self._pending and reply_action==self._pending.value:
                self._pending=None; self._acked=False; self._last_error=""; self._start_queued_action_locked()
            elif reply_action==MotionAction.STOP.value or self._pending is MotionAction.STOP:
                self._record("ignore stale %s"%line)
            elif self._pending is None:
                self._record("ignore late DONE %s"%line)
            else:
                self._fail("unexpected DONE: %s"%line); self.emergency_stop("bad DONE", await_reply=False)
        elif line.startswith("ERR:"):self._fail(line); self._pending=None; self._acked=False
        elif line!="PONG":self._fail("unknown reply: %s"%line); self.emergency_stop("unknown reply", await_reply=False)
    def _write(self,text):
        if self.config.mock:return
        if self._fd is None:raise OSError("C07A UART is closed")
        # Send exactly one protocol frame. The firmware is line framed and
        # already discards empty lines; adding synthetic sync bytes can create
        # delayed parser diagnostics on some UART driver revisions.
        os.write(self._fd,text.encode("ascii"))
    @staticmethod
    def _open_uart(port,baud):
        if termios is None:raise OSError("POSIX UART support is unavailable on this host")
        speed={9600:termios.B9600,115200:termios.B115200}.get(baud)
        if speed is None:raise OSError("unsupported baud %s"%baud)
        fd=os.open(port,os.O_RDWR|os.O_NOCTTY|os.O_NONBLOCK); attrs=termios.tcgetattr(fd); attrs[0]=attrs[1]=attrs[3]=0; attrs[2]=termios.CS8|termios.CREAD|termios.CLOCAL; attrs[4]=attrs[5]=speed; attrs[6][termios.VMIN]=attrs[6][termios.VTIME]=0; termios.tcsetattr(fd,termios.TCSANOW,attrs); termios.tcflush(fd,termios.TCIOFLUSH); return fd
    def _record(self,text):LOG.info("[C07A] %s",text); self._history.append(text); self._event_sink and self._event_sink("c07a",text)
    def _fail(self,text):self._last_error=text; self._record("error %s"%text)
    def _fail_retry(self,text):self._fail(text); self._next_retry_at=time.monotonic()+5.0; return False
