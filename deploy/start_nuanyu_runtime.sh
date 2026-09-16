#!/bin/sh
set -u

# Resolve the repository from this script's own location so the tree can be
# run in place. deploy/ and app/ are siblings.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# Deployment root: where logs, pid files, runtime config and state live.
# Defaults to the repository itself. Set NUANYU_ROOT to deploy the runtime
# somewhere else (the reference board uses /userdata_fibo).
ROOT="${NUANYU_ROOT:-$REPO_DIR}"
# Export it so the Python side resolves the same root this script uses.
NUANYU_ROOT="$ROOT"
export NUANYU_ROOT
APP_DIR="$ROOT/app"
WEB="$APP_DIR/nuanyu_web.py"
LOG="$ROOT/nuanyu_runtime.log"
PID_FILE="$ROOT/nuanyu_web.pid"
LOCK_FILE=/run/nuanyu_runtime.lock
L610="$APP_DIR/src/connectivity/l610_service.py"
DISPLAY_SCRIPT="$APP_DIR/src/display/launch_display.sh"

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    echo "[$(date '+%F %T')] runtime already owned" >> "$LOG"
    exit 75
fi

if [ -f "$ROOT/config/nuanyu.env" ]; then
    set -a
    . "$ROOT/config/nuanyu.env"
    set +a
fi

if [ -f "$ROOT/config/c07a_motion.env" ]; then
    set -a
    . "$ROOT/config/c07a_motion.env"
    set +a
fi

export NUANYU_TTS_BACKEND="${NUANYU_TTS_BACKEND:-drizzle}"
export ASR_ENABLED="${ASR_ENABLED:-true}"
export ASR_BACKEND="${ASR_BACKEND:-whisper_tiny_cpu}"
export ASR_FALLBACK_BACKEND="${ASR_FALLBACK_BACKEND:-none}"
export CAMERA_DEVICE="${CAMERA_DEVICE:-/dev/video2}"
export CAMERA_FALLBACK_URL="${CAMERA_FALLBACK_URL:-http://127.0.0.1:5016/camera.jpg}"
export AUDIO_OUTPUT_MODE="${AUDIO_OUTPUT_MODE:-auto}"
export PC_SPEAKER_URL="${PC_SPEAKER_URL:-http://127.0.0.1:5015/play}"
# Historical demo config forced all sound to the PC. The desktop release is
# board-first; audio_router performs the PC fallback only after board failure.
export SURGE_PC_SPEAKER=0
export FIBO_TTS_PC_ONLY=0
# ALSA card numbers move whenever a USB microphone/camera is added or
# removed. Resolve the Qualcomm playback card by name on every launch instead
# of relying on the historical fixed card number.
detected_hph_card="$(awk '/lahaina-yupikiot/{gsub(/[^0-9]/, "", $1); print $1; exit}' /proc/asound/cards 2>/dev/null)"
if [ -n "$detected_hph_card" ]; then
    export FIBO_TTS_HPH_CARD="$detected_hph_card"
else
    export FIBO_TTS_HPH_CARD="${FIBO_TTS_HPH_CARD:-2}"
fi
export FIBO_TTS_HPH_DEVICE="${FIBO_TTS_HPH_DEVICE:-0}"
export ZIPVOICE_SERVER_URL="${ZIPVOICE_SERVER_URL:-http://127.0.0.1:5018}"
export DEEPSEEK_BASE_URL="${DEEPSEEK_BASE_URL:-http://127.0.0.1:5019}"
export DOUBAO_TTS_API_URL="${DOUBAO_TTS_API_URL:-http://127.0.0.1:5019/doubao/tts}"
# ONNX Runtime thread counts are pinned in code, not here: the FER backend sets
# both to 1 (app/src/vision/fer_onnx_backend.py). ORT_NUM_INTRAOP_THREADS and
# ORT_NUM_INTEROP_THREADS used to be exported here but nothing reads them -
# not ONNX Runtime, and no module in this tree.
export VOICE_BAUD="${VOICE_BAUD:-9600}"

web_pid=""
l610_pid=""

log() {
    echo "[$(date '+%F %T')] $*" | tee -a "$LOG"
}

stop_children() {
    for child in "$web_pid" "$l610_pid"; do
        if [ -n "$child" ] && kill -0 "$child" 2>/dev/null; then
            kill "$child" 2>/dev/null || true
        fi
    done
    sleep 1
    for child in "$web_pid" "$l610_pid"; do
        if [ -n "$child" ] && kill -0 "$child" 2>/dev/null; then
            kill -9 "$child" 2>/dev/null || true
        fi
    done
    rm -f "$PID_FILE"
}

stop_display() {
    # HDMI expression display: shut down Weston/Firefox kiosk on stop
    if [ -x "$DISPLAY_SCRIPT" ]; then
        "$DISPLAY_SCRIPT" stop 2>/dev/null || true
    fi
}

trap 'stop_children; stop_display; exit 0' INT TERM HUP

mkdir -p "$ROOT"
if [ -f "$LOG" ] && [ "$(wc -c < "$LOG")" -gt 8388608 ]; then
    mv "$LOG" "$LOG.1"
fi

chmod 666 /dev/ttyHS1 2>/dev/null || true
chmod 666 /dev/video* 2>/dev/null || true
# Always awake on boot: clear leftover quiet/sleep state, otherwise handle_chat
# returns empty immediately and the chat history is not displayed.
rm -f "$ROOT/.nuanyu_stop" "$ROOT/.nuanyu_sleeping" "$ROOT/.nuanyu_quiet" \
    "$ROOT/.nuanyu_tts_unhealthy"

pkill -f 'python3.*nuanyu_web.py' 2>/dev/null || true
pkill -f 'python3.*l610_service.py' 2>/dev/null || true
sleep 1

if [ -f "$L610" ]; then
    python3 "$L610" >> "$ROOT/l610.log" 2>&1 &
    l610_pid=$!
    log "L610 started pid=$l610_pid"
fi

failures=0
while true; do
    cd "$APP_DIR" || exit 2
    log "starting Nuanyu web runtime"
    python3 "$WEB" 9>&- >> "$LOG" 2>&1 &
    web_pid=$!
    echo "$web_pid" > "$PID_FILE"

    ready=0
    elapsed=0
    while kill -0 "$web_pid" 2>/dev/null && [ "$elapsed" -lt 90 ]; do
        if ss -ltn 2>/dev/null | grep -q ':5004'; then
            ready=1
            failures=0
            log "web ready pid=$web_pid after ${elapsed}s"
            # HDMI expression display: start asynchronously once the web app is
            # ready (do not block).
            # 2026-08-07 the display went back to dual-head HDMI wired directly
            # to the board -> driven by the board renderer.
            if [ -x "$DISPLAY_SCRIPT" ]; then
                "$DISPLAY_SCRIPT" start &
            fi
            break
        fi
        sleep 2
        elapsed=$((elapsed + 2))
    done

    if [ "$ready" -eq 1 ]; then
        response_failures=0
        while kill -0 "$web_pid" 2>/dev/null; do
            sleep 3
            # Memory watchdog (competition safety net): the web app sits at
            # ~3.7GB after a normal load; if it spikes past 5GB (slow leak or
            # SDK anomaly), restart so the whole machine is not dragged down.
            if [ -r "/proc/$web_pid/status" ]; then
                _rss_kb=$(awk '/VmRSS/{print $2}' "/proc/$web_pid/status" 2>/dev/null)
                if [ -n "$_rss_kb" ] && [ "$_rss_kb" -gt 5242880 ]; then
                    log "web RSS ${_rss_kb}KB > 5GB, restarting to protect system"
                    kill "$web_pid" 2>/dev/null || true
                    break
                fi
            fi
            # A wedged request lock used to leave the listening socket alive
            # while every /api/status thread piled up forever.  Probe the real
            # status path and bound the thread pool as a last-resort guard.
            if curl -fsS --max-time 3 "http://127.0.0.1:5004/api/status" \
                    >/dev/null 2>&1; then
                response_failures=0
            else
                response_failures=$((response_failures + 1))
                if [ "$response_failures" -ge 3 ]; then
                    log "web /api/status failed 3 consecutive probes, restarting"
                    kill "$web_pid" 2>/dev/null || true
                    break
                fi
            fi
            _thread_count=$(find "/proc/$web_pid/task" -mindepth 1 -maxdepth 1 \
                -type d 2>/dev/null | wc -l)
            if [ "$_thread_count" -gt 120 ]; then
                log "web thread count $_thread_count > 120, restarting"
                kill "$web_pid" 2>/dev/null || true
                break
            fi
            if [ -f "$ROOT/.nuanyu_tts_unhealthy" ]; then
                log "Fibocom TTS DSP timed out, restarting cleanly"
                kill "$web_pid" 2>/dev/null || true
                break
            fi
        done
        log "web process exited"
    else
        log "web failed before readiness"
        kill "$web_pid" 2>/dev/null || true
    fi

    failures=$((failures + 1))
    if [ "$failures" -ge 3 ]; then
        log "crash loop detected; cooling down 15s"
        sleep 15
    else
        sleep 3
    fi
done
