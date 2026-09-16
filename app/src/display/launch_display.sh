#!/bin/sh
# Nuanyu HDMI expression-screen launcher (SC171/QCS6490 build)
#
# Notes:
#   On this board (SC171) the system display is managed by
#   init_display.service; Weston uses the sdm-backend and only one
#   compositor may hold the SDM output.  This script therefore does
#   **not start its own Weston** — it reuses the system Weston session
#   (wayland-0) to launch the native wayland expression renderer
#   expression_display.py.
#   (Not firefox/GTK/WebKit: weston 8.0.0 here has an incomplete
#     xdg-shell stable and every modern client crashes; wl_shell is
#     weston 8's native protocol.)
#
#   Prerequisite: [output] DP-1 / DSI-1 mode=on in /etc/xdg/weston/weston.ini
#   (HDMI output enabled, read by init_display.service)
#
# Usage:
#   launch_display.sh {start|stop|restart}

# Deployment root — overridable so the app can run outside /userdata_fibo.
# With NUANYU_ROOT unset the default reproduces the on-board layout exactly.
NUANYU_ROOT="${NUANYU_ROOT:-/userdata_fibo}"
DISPLAY_LOG="$NUANYU_ROOT/display.log"
DISPLAY_PID_FILE="$NUANYU_ROOT/display.pid"
# This script lives in the display directory it launches from.
DISPLAY_DIR="$(cd "$(dirname "$0")" && pwd)"
WAYLAND_SOCKET=/run/user/root/wayland-0

# ── Clean up leftover processes ──
cleanup() {
    pkill -f 'expression_display.py' 2>/dev/null || true
    sleep 1
}

# ── Wait for the web service to become ready ──
wait_web() {
    for i in $(seq 1 30); do
        if ss -tln 2>/dev/null | grep -q ':5004'; then
            return 0
        fi
        sleep 1
    done
    return 1
}

# ── Start ──
start_display() {
    cleanup
    echo "[$(date '+%H:%M:%S')] Starting HDMI display..." > "$DISPLAY_LOG"

    # Check either display output: DP-1 (native HDMI) or DSI-1 (LT8912 DSI→HDMI bridge).
    # Match "connected" exactly so the "disconnected" substring is not matched.
    dp_status="$(cat /sys/class/drm/card0-DP-1/status 2>/dev/null)"
    dsi_status="$(cat /sys/class/drm/card0-DSI-1/status 2>/dev/null)"
    if [ "$dp_status" != "connected" ] && [ "$dsi_status" != "connected" ]; then
        echo "[$(date '+%H:%M:%S')] No display connected (DP=$dp_status DSI=$dsi_status) — skipping" >> "$DISPLAY_LOG"
        return 0
    fi

    # Wait for the web service
    wait_web || {
        echo "[$(date '+%H:%M:%S')] Web service not ready — skipping display" >> "$DISPLAY_LOG"
        return 1
    }

    # Check the system Weston session (init_display.service)
    if [ ! -S "$WAYLAND_SOCKET" ]; then
        echo "[$(date '+%H:%M:%S')] Weston socket $WAYLAND_SOCKET not found (init_display.service down?)" >> "$DISPLAY_LOG"
        return 1
    fi

    # Reuse the system Weston session to start the native wayland renderer
    XDG_RUNTIME_DIR=/run/user/root \
    WAYLAND_DISPLAY=wayland-0 \
    python3 "$DISPLAY_DIR/expression_display.py" \
            >> "$DISPLAY_LOG" 2>&1 &
    renderer_pid=$!
    sleep 2

    if ! kill -0 "$renderer_pid" 2>/dev/null; then
        echo "[$(date '+%H:%M:%S')] Renderer failed to start" >> "$DISPLAY_LOG"
        return 1
    fi

    echo "$renderer_pid" > "$DISPLAY_PID_FILE"
    echo "[$(date '+%H:%M:%S')] Display ready — renderer PID=$renderer_pid" >> "$DISPLAY_LOG"
    return 0
}

# ── Stop ──
stop_display() {
    echo "[$(date '+%H:%M:%S')] Stopping display..." >> "$DISPLAY_LOG"
    if [ -f "$DISPLAY_PID_FILE" ]; then
        pid=$(cat "$DISPLAY_PID_FILE")
        kill "$pid" 2>/dev/null || true
    fi
    cleanup
    rm -f "$DISPLAY_PID_FILE"
}

case "${1:-start}" in
    start)  start_display ;;
    stop)   stop_display ;;
    restart) stop_display; sleep 1; start_display ;;
    *)      echo "Usage: $0 {start|stop|restart}"; exit 1 ;;
esac
