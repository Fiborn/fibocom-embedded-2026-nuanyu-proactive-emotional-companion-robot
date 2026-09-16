"""Reliable audio output routing for board-first desktop deployments."""

import os
import urllib.request


PC_SPEAKER_URL = os.environ.get(
    "PC_SPEAKER_URL",
    os.environ.get("SURGE_PC_SPEAKER_URL", "http://127.0.0.1:5015/play"),
)


def output_mode():
    mode = os.environ.get("AUDIO_OUTPUT_MODE", "auto").strip().lower()
    return mode if mode in ("auto", "board", "pc") else "auto"


def play_on_pc_bytes(wav_bytes, timeout=12):
    if not wav_bytes:
        return False
    try:
        req = urllib.request.Request(
            PC_SPEAKER_URL,
            data=wav_bytes,
            headers={"Content-Type": "audio/wav"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return 200 <= response.status < 300
    except Exception as exc:
        print("[AudioRouter] PC playback failed: %s" % str(exc)[:160],
              flush=True)
        return False


def play_on_pc_file(wav_path, timeout=12):
    try:
        with open(wav_path, "rb") as wav_file:
            return play_on_pc_bytes(wav_file.read(), timeout=timeout)
    except Exception as exc:
        print("[AudioRouter] Cannot read WAV: %s" % str(exc)[:160],
              flush=True)
        return False


def play_file_board_first(wav_path, board_player):
    """Play on the board, falling back to the PC only in auto mode."""
    mode = output_mode()
    if mode != "pc":
        try:
            if bool(board_player(wav_path)):
                return True, "board"
        except Exception as exc:
            print("[AudioRouter] Board playback failed: %s" % str(exc)[:160],
                  flush=True)
        if mode == "board":
            return False, "board"
    ok = play_on_pc_file(wav_path)
    return ok, "pc" if ok else "none"


def play_bytes_board_first(wav_bytes, board_player):
    """Play WAV bytes on the board, then use the desktop bridge if needed."""
    mode = output_mode()
    if mode != "pc":
        try:
            if bool(board_player(wav_bytes)):
                return True, "board"
        except Exception as exc:
            print("[AudioRouter] Board playback failed: %s" % str(exc)[:160],
                  flush=True)
        if mode == "board":
            return False, "board"
    ok = play_on_pc_bytes(wav_bytes)
    return ok, "pc" if ok else "none"
