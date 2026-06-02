import sys
import time
import math
import wave
import struct
import platform
import select
import subprocess
import threading
import json
import os
import queue
import re
import shutil
import tempfile
from collections import deque
from bisect import bisect_left
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Set, Tuple

import serial
import tkinter as tk
from tkinter import ttk

try:
    import requests  # Requires 'pip install requests'
except ImportError as exc:
    raise SystemExit(
        "The 'requests' package is required for remote referee uploads. Install it with 'pip install requests'."
    ) from exc

from judge_client import response_payload, submit_phrase
from local_streaming_manager import (
    LocalStreamingSessionManager,
    ensure_local_analyzer_service,
    shutdown_shared_local_analyzer,
)

try:
    import cv2  # Requires 'pip install opencv-python'
except ImportError as exc:
    raise SystemExit(
        "OpenCV (cv2) is required for camera recording. Install it with 'pip install opencv-python'."
    ) from exc

try:
    import numpy as np
except ImportError as exc:
    raise SystemExit(
        "NumPy is required for camera recording. Install it with 'pip install numpy'."
    ) from exc

try:
    import asyncio
    import websockets
    STREAMING_AVAILABLE = True
except ImportError:
    STREAMING_AVAILABLE = False
    print("Warning: 'websockets' not installed. Streaming mode disabled. Install with 'pip install websockets'.")


# --- CONFIGURATION ---
# !!! CHANGE THIS to your Arduino's serial port if the env override is not sufficient !!!
SERIAL_PORT = os.environ.get("FENCING_SERIAL_PORT", "/dev/ttyACM0")
try:
    BAUD_RATE = int(os.environ.get("FENCING_BAUD_RATE", "115200"))
except ValueError:
    BAUD_RATE = 115200
RECORDINGS_DIR = Path(os.environ.get("FENCING_RECORDINGS_DIR", "recordings")).expanduser()
if not RECORDINGS_DIR.is_absolute():
    RECORDINGS_DIR = (Path(__file__).resolve().parent / RECORDINGS_DIR).resolve()
RECORDINGS_DIR.mkdir(exist_ok=True)

try:
    CAMERA_INDEX = int(os.environ.get("FENCING_CAMERA_INDEX", "0"))
except ValueError:
    CAMERA_INDEX = 0
CAMERA_DEVICE = os.environ.get("FENCING_CAMERA_DEVICE", "").strip() or None
try:
    CAMERA_WIDTH = int(os.environ.get("FENCING_CAMERA_WIDTH", "1280"))
except ValueError:
    CAMERA_WIDTH = 1280
try:
    CAMERA_HEIGHT = int(os.environ.get("FENCING_CAMERA_HEIGHT", "720"))
except ValueError:
    CAMERA_HEIGHT = 720
try:
    CAMERA_TARGET_FPS = float(os.environ.get("FENCING_CAMERA_FPS", "30"))
except ValueError:
    CAMERA_TARGET_FPS = 30.0
NATIVE_GSTREAMER_CAPTURE = os.environ.get("FENCING_NATIVE_GSTREAMER_CAPTURE", "true").lower() in {"1", "true", "yes"}
try:
    CAMERA_ANALYSIS_QUEUE_MAX = int(os.environ.get("FENCING_ANALYSIS_QUEUE_MAX", "32"))
except ValueError:
    CAMERA_ANALYSIS_QUEUE_MAX = 32
try:
    CAMERA_WRITER_QUEUE_MAX = int(os.environ.get("FENCING_WRITER_QUEUE_MAX", "64"))
except ValueError:
    CAMERA_WRITER_QUEUE_MAX = 64
try:
    CAMERA_POLL_INTERVAL_MS = int(os.environ.get("FENCING_CAMERA_POLL_MS", "10"))
except ValueError:
    CAMERA_POLL_INTERVAL_MS = 10

# --- Remote referee configuration ---
REFEREE_SERVER_URL = os.environ.get("REFEREE_SERVER_URL", "http://192.168.50.2:8765/judge").rstrip("/")
try:
    REFEREE_REQUEST_TIMEOUT = float(os.environ.get("REFEREE_REQUEST_TIMEOUT", "300"))
except ValueError:
    REFEREE_REQUEST_TIMEOUT = 300.0
REFEREE_INCLUDE_KEYPOINTS = os.environ.get("REFEREE_INCLUDE_KEYPOINTS", "false").lower() in {"1", "true", "yes"}

REFEREE_TRANSCODE_VIDEO = os.environ.get("REFEREE_TRANSCODE_VIDEO", "false").lower() in {"1", "true", "yes"}
try:
    REFEREE_TRANSCODE_CRF = int(os.environ.get("REFEREE_TRANSCODE_CRF", "28"))
except ValueError:
    REFEREE_TRANSCODE_CRF = 28
REFEREE_TRANSCODE_PRESET = os.environ.get("REFEREE_TRANSCODE_PRESET", "veryfast")

# --- Streaming configuration ---
REFEREE_USE_STREAMING = os.environ.get("REFEREE_USE_STREAMING", "false").lower() in {"1", "true", "yes"}
REFEREE_STREAMING_ENCODING = os.environ.get("REFEREE_STREAMING_ENCODING", "jpeg")  # jpeg, png, raw
try:
    REFEREE_STREAMING_JPEG_QUALITY = int(os.environ.get("REFEREE_STREAMING_JPEG_QUALITY", "85"))
except ValueError:
    REFEREE_STREAMING_JPEG_QUALITY = 85
REFEREE_STREAMING_SAVE_LOCAL = os.environ.get("REFEREE_STREAMING_SAVE_LOCAL", "true").lower() in {"1", "true", "yes"}

REFEREE_USE_LOCAL_STREAMING_ANALYZER = os.environ.get("REFEREE_USE_LOCAL_STREAMING_ANALYZER", "false").lower() in {"1", "true", "yes"}
REFEREE_LOCAL_ANALYZER_ROOT = Path(
    os.environ.get(
        "REFEREE_LOCAL_ANALYZER_ROOT",
        "/home/thomas/fencing/portable_fencing_pipeline_low_latency_streaming",
    )
).resolve()
_local_analyzer_python = Path(
    os.environ.get(
        "REFEREE_LOCAL_ANALYZER_PYTHON",
        str(REFEREE_LOCAL_ANALYZER_ROOT / ".venv" / "bin" / "python"),
    )
).expanduser()
if not _local_analyzer_python.is_absolute():
    _local_analyzer_python = (Path.cwd() / _local_analyzer_python).absolute()
REFEREE_LOCAL_ANALYZER_PYTHON = _local_analyzer_python
_local_output_root = os.environ.get("REFEREE_LOCAL_ANALYZER_OUTPUT_ROOT", "").strip()
REFEREE_LOCAL_ANALYZER_OUTPUT_ROOT = Path(_local_output_root).resolve() if _local_output_root else None
_local_model_path = os.environ.get("REFEREE_LOCAL_ANALYZER_MODEL_PATH", "").strip()
REFEREE_LOCAL_ANALYZER_MODEL_PATH = Path(_local_model_path).resolve() if _local_model_path else None
REFEREE_LOCAL_ANALYZER_FISHEYE_BACKEND = os.environ.get("REFEREE_LOCAL_ANALYZER_FISHEYE_BACKEND", "none")
try:
    REFEREE_LOCAL_ANALYZER_BOOTSTRAP_FRAMES = int(os.environ.get("REFEREE_LOCAL_ANALYZER_BOOTSTRAP_FRAMES", "8"))
except ValueError:
    REFEREE_LOCAL_ANALYZER_BOOTSTRAP_FRAMES = 8
try:
    REFEREE_LOCAL_ANALYZER_QUEUE_MAX = int(os.environ.get("REFEREE_LOCAL_ANALYZER_QUEUE_MAX", "120"))
except ValueError:
    REFEREE_LOCAL_ANALYZER_QUEUE_MAX = 120
try:
    REFEREE_LOCAL_ANALYZER_STARTUP_TIMEOUT = float(os.environ.get("REFEREE_LOCAL_ANALYZER_STARTUP_TIMEOUT", "30"))
except ValueError:
    REFEREE_LOCAL_ANALYZER_STARTUP_TIMEOUT = 30.0
try:
    REFEREE_LOCAL_ANALYZER_YOLO_CONF = float(os.environ.get("REFEREE_LOCAL_ANALYZER_YOLO_CONF", "0.15"))
except ValueError:
    REFEREE_LOCAL_ANALYZER_YOLO_CONF = 0.15
try:
    REFEREE_LOCAL_ANALYZER_YOLO_IMGSZ = int(os.environ.get("REFEREE_LOCAL_ANALYZER_YOLO_IMGSZ", "512"))
except ValueError:
    REFEREE_LOCAL_ANALYZER_YOLO_IMGSZ = 512
REFEREE_LOCAL_ANALYZER_YOLO_HALF = os.environ.get("REFEREE_LOCAL_ANALYZER_YOLO_HALF", "false").lower() in {"1", "true", "yes"}
REFEREE_LOCAL_ANALYZER_YOLO_VERBOSE = os.environ.get("REFEREE_LOCAL_ANALYZER_YOLO_VERBOSE", "false").lower() in {"1", "true", "yes"}

SEND_TO_SERVER_DEFAULT = os.environ.get("REFEREE_SEND_TO_SERVER", "true").lower() in {"1", "true", "yes"}

WINNER_BLUE_ACTIVE = "#1e90ff"
WINNER_BLUE_INACTIVE = "#303030"


# --- Global state tracked from Arduino ---
fencer1_led = False
fencer2_led = False
system_state = "INITIALIZING"
last_log_line = ""
phrase_counter = 0
phrase_active = False

# --- Recording/logging bookkeeping ---
signal_file_handle = None
current_phrase_folder: Path | None = None
current_base_name: str | None = None
current_phrase_number: int | None = None
last_phrase_folder: Path | None = None
last_base_name: str | None = None
pending_phrase_number: int | None = None
winner_recorded = False
awaiting_winner = False
awaiting_remote_result = False
referee_upload_thread: threading.Thread | None = None
awaiting_manual_winner = False
phrase_requires_remote_review = False
declared_winner_side: Optional[str] = None
manual_winner_side: Optional[str] = None
last_phrase_right_hit = False
last_phrase_left_hit = False
winner_right_indicator_active = False
winner_left_indicator_active = False
# Tracks which fencers already have explicit hit log entries during the active scoring exchange
_logged_hit_sides: Set[str] = set()
phrase_start_arduino_ms: Optional[int] = None
phrase_start_host_ns: Optional[int] = None
phrase_start_command_host_ns: Optional[int] = None
phrase_end_timestamp_us: Optional[int] = None
arduino_clock_offset_ns: Optional[int] = None
arduino_clock_sync_rtt_ns: Optional[int] = None

# --- Runtime references ---
app = None
ser = None
camera_recorder = None
camera_status_message = "Initializing camera..."
camera_available = False
streaming_session_manager = None
send_to_server_mode = SEND_TO_SERVER_DEFAULT
CONSOLE_DETAIL_LOGS = os.environ.get("FENCING_CONSOLE_DETAIL_LOGS", "true").lower() in {"1", "true", "yes"}
_last_console_event_message = ""
_last_console_camera_status = ""
_last_console_record_status = ""
_last_console_mode_status = ""
_last_console_ui_state = ""


@dataclass
class TimedLogEntry:
    timestamp_seconds: float
    timestamp_us: int
    message: str


phrase_timed_log_entries: list[TimedLogEntry] = []


def warm_local_analyzer_service() -> None:
    global camera_status_message

    if not REFEREE_USE_LOCAL_STREAMING_ANALYZER:
        return
    if camera_recorder is None:
        return

    try:
        ensure_local_analyzer_service(
            bundle_root=REFEREE_LOCAL_ANALYZER_ROOT,
            python_executable=REFEREE_LOCAL_ANALYZER_PYTHON,
            model_path=REFEREE_LOCAL_ANALYZER_MODEL_PATH,
            fisheye_backend=REFEREE_LOCAL_ANALYZER_FISHEYE_BACKEND,
            yolo_conf=REFEREE_LOCAL_ANALYZER_YOLO_CONF,
            yolo_imgsz=REFEREE_LOCAL_ANALYZER_YOLO_IMGSZ,
            yolo_half=REFEREE_LOCAL_ANALYZER_YOLO_HALF,
            yolo_verbose=REFEREE_LOCAL_ANALYZER_YOLO_VERBOSE,
            bootstrap_frames=REFEREE_LOCAL_ANALYZER_BOOTSTRAP_FRAMES,
            startup_timeout=REFEREE_LOCAL_ANALYZER_STARTUP_TIMEOUT,
            result_timeout=REFEREE_REQUEST_TIMEOUT,
            width=camera_recorder.width,
            height=camera_recorder.height,
        )
        print("[LOCAL_ANALYZER] Persistent local analyzer is warm")
    except Exception as exc:
        print(f"[LOCAL_ANALYZER] Failed to warm persistent local analyzer: {exc}")
        if camera_status_message == "Ready":
            camera_status_message = "Ready (local analyzer warmup failed)"


def append_to_phrase_log(phrase_dir: Path, base_name: str, message: str) -> None:
    """Append a diagnostic line to the phrase log, regardless of open handles."""
    log_path = phrase_dir / f"{base_name}.txt"
    timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
    try:
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(f"[{timestamp}] {message}\n")
    except OSError:
        pass
    if CONSOLE_DETAIL_LOGS:
        print(f"[PHRASE_LOG] {base_name}: {message}", flush=True)


def reset_phrase_timing_state(clear_entries: bool = True) -> None:
    """Reset phrase-local timing anchors and any collected timed log entries."""
    global phrase_start_arduino_ms, phrase_start_host_ns, phrase_start_command_host_ns
    global phrase_end_timestamp_us, phrase_timed_log_entries

    phrase_start_arduino_ms = None
    phrase_start_host_ns = None
    phrase_start_command_host_ns = None
    phrase_end_timestamp_us = None
    if clear_entries:
        phrase_timed_log_entries = []


def sync_arduino_clock(serial_connection: serial.Serial, attempts: int = 8) -> tuple[Optional[int], Optional[int]]:
    """Estimate host monotonic time offset from the Arduino's millis() clock."""
    best_offset_ns: Optional[int] = None
    best_rtt_ns: Optional[int] = None

    try:
        serial_connection.reset_input_buffer()
    except serial.SerialException:
        return None, None

    for _ in range(attempts):
        try:
            serial_connection.reset_input_buffer()
            t0 = time.perf_counter_ns()
            serial_connection.write(b'u')
            raw_line = serial_connection.readline().decode("utf-8", errors="ignore").strip()
            t1 = time.perf_counter_ns()
        except serial.SerialException:
            return best_offset_ns, best_rtt_ns

        if not raw_line.startswith("TIME_MS:"):
            time.sleep(0.02)
            continue

        try:
            arduino_ms = int(raw_line.split(":", 1)[1].strip())
        except ValueError:
            time.sleep(0.02)
            continue

        rtt_ns = t1 - t0
        midpoint_ns = (t0 + t1) // 2
        offset_ns = midpoint_ns - (arduino_ms * 1_000_000)
        if best_rtt_ns is None or rtt_ns < best_rtt_ns:
            best_offset_ns = offset_ns
            best_rtt_ns = rtt_ns
        time.sleep(0.02)

    return best_offset_ns, best_rtt_ns


def _current_phrase_start_host_ns() -> Optional[int]:
    """Return the best-known host monotonic anchor for Arduino phrase time zero."""
    if phrase_start_host_ns is not None:
        return phrase_start_host_ns
    return phrase_start_command_host_ns


def update_last_event(message: str) -> None:
    """Update the UI-visible last event message and refresh the app safely."""
    global last_log_line, _last_console_event_message
    last_log_line = message
    if CONSOLE_DETAIL_LOGS and message and message != _last_console_event_message:
        print(f"[EVENT] {message}", flush=True)
        _last_console_event_message = message
    if app and hasattr(app, "root"):
        app.root.after(0, app.refresh)


def mirror_console_status(prefix: str, message: str) -> None:
    """Mirror deduplicated UI status strings to the terminal."""
    if not CONSOLE_DETAIL_LOGS:
        return
    text = str(message or "").strip()
    if not text:
        return
    print(f"[{prefix}] {text}", flush=True)


def send_mode_description() -> str:
    """Return a short human-readable description of the current upload mode."""
    if send_to_server_mode:
        return "Server mode (send phrase to Pi judge)"
    return "Local mode (manual/local only)"


def set_send_to_server_mode(enabled: bool) -> None:
    """Switch between remote upload and local-only storage modes."""
    global send_to_server_mode, phrase_requires_remote_review
    if send_to_server_mode == enabled:
        return

    send_to_server_mode = enabled
    mode_text = send_mode_description()
    print(f"[MODE] Transfer mode changed: {mode_text}")
    update_last_event(f"Transfer mode changed: {mode_text}")

    if not enabled and phrase_requires_remote_review:
        phrase_requires_remote_review = False


def winner_side_label(side: str) -> str:
    if side == "right":
        return "Right Fencer (Green Lamp)"
    if side == "left":
        return "Left Fencer (Red Lamp)"
    return side


def set_winner_indicator(side: Optional[str]) -> None:
    """Update winner indicator state and refresh the UI."""
    global winner_right_indicator_active, winner_left_indicator_active, declared_winner_side
    declared_winner_side = side
    winner_right_indicator_active = side == "right"
    winner_left_indicator_active = side == "left"
    if app and hasattr(app, "root"):
        app.root.after(0, app.update_winner_indicators)


def clear_winner_indicator() -> None:
    set_winner_indicator(None)


def reset_logged_hit_sides() -> None:
    """Clear tracking of per-fencer hit log entries for the active phrase."""
    _logged_hit_sides.clear()


def _emit_missing_double_hit_entries(timestamp_seconds: Optional[float], timestamp_us: Optional[int]) -> None:
    """Write synthetic hit log entries for sides missing during a double hit."""
    missing = {"left", "right"} - _logged_hit_sides
    if not missing:
        _logged_hit_sides.clear()
        return

    for side in ("left", "right"):
        if side not in missing:
            continue
        scoring_side = "Left" if side == "left" else "Right"
        target_side = "Right" if side == "left" else "Left"
        message = f"HIT: {scoring_side} scores on {target_side}! (simultaneous)"
        if timestamp_seconds is not None and timestamp_us is not None:
            write_timed_signal_entry(timestamp_seconds, timestamp_us, message)
        else:
            write_signal_line(message)
    _logged_hit_sides.clear()


def process_hit_log_entry(
    message_text: str,
    timestamp_seconds: Optional[float],
    timestamp_us: Optional[int] = None,
) -> None:
    """Track individual hit log lines and ensure doubles log both timestamps."""
    normalized = message_text.strip().lower()
    if not normalized:
        return

    if normalized.startswith("phrase recording started") or normalized.startswith("phrase recording ended"):
        reset_logged_hit_sides()
        return

    if normalized.startswith("hit: left scores"):
        _logged_hit_sides.add("left")
        return

    if normalized.startswith("hit: right scores"):
        _logged_hit_sides.add("right")
        return

    if normalized.startswith("hit: simultaneous"):
        _emit_missing_double_hit_entries(timestamp_seconds, timestamp_us)


def log_winner_selection(side: str, source: str) -> None:
    label = winner_side_label(side)
    log_phrase_message(f"{source} winner: {label}")


def log_phrase_message(message: str) -> None:
    if signal_file_handle:
        write_signal_line(message)
    else:
        resolved = _resolve_latest_phrase()
        if resolved:
            append_to_phrase_log(resolved[0], resolved[1], message)


def speak_text(message: str) -> None:
    message = message.strip()
    if not message:
        return

    system = platform.system()
    try:
        if system == "Darwin":
            subprocess.Popen(["say", message])
            return
        if system == "Windows":
            escaped = message.replace("'", "''")
            ps_script = (
                "Add-Type -AssemblyName System.Speech;"
                "$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
                f"$synth.Speak('{escaped}')"
            )
            subprocess.Popen(["powershell", "-NoProfile", "-Command", ps_script])
            return
        if shutil.which("espeak"):
            subprocess.Popen(["espeak", message])
            return
    except Exception:
        pass

    print(f"[Broadcast] {message}")


def broadcast_winner_reason(side: str, reason: str) -> None:
    reason = (reason or "").replace("\n", " ").strip()
    if not reason:
        reason = f"Only one light. {winner_side_label(side)} wins."

    speak_text(reason)

    resolved = _resolve_latest_phrase()
    if resolved:
        append_to_phrase_log(resolved[0], resolved[1], f"Broadcast: {reason}")


def single_hit_broadcast_message(side: str) -> str:
    if side == "right":
        return "Only one light. Right fencer wins."
    if side == "left":
        return "Only one light. Left fencer wins."
    return "Only one light recorded."


def interpret_score_summary(score_summary: str) -> Tuple[Optional[bool], Optional[bool]]:
    """Return (right_hit, left_hit) from the score summary string."""
    right_hit = None
    left_hit = None
    try:
        parts = score_summary.split("Scores ->", 1)[1].strip()
        entries = [segment.strip() for segment in parts.split(",")]
        for entry in entries:
            if entry.startswith("Fencer 1:"):
                right_hit = entry.split(":", 1)[1].strip().upper() == "HIT"
            elif entry.startswith("Fencer 2:"):
                left_hit = entry.split(":", 1)[1].strip().upper() == "HIT"
    except Exception:
        right_hit = None
        left_hit = None
    return right_hit, left_hit


def _phrase_artifact_paths(phrase_dir: Path, base_name: str) -> Tuple[Path, Path, Path]:
    video_path = phrase_dir / f"{base_name}.avi"
    signal_path = phrase_dir / f"{base_name}.txt"
    result_path = phrase_dir / f"{base_name}_result.json"
    return video_path, signal_path, result_path


def _resolve_latest_phrase() -> Tuple[Path, str] | None:
    """Return the folder/name for the current or last completed phrase."""
    if current_phrase_folder and current_base_name:
        return current_phrase_folder, current_base_name
    if last_phrase_folder and last_base_name:
        return last_phrase_folder, last_base_name
    return None


def prepare_video_for_upload(video_path: Path, phrase_dir: Path, base_name: str) -> Tuple[Path, str, Optional[Path]]:
    """Optionally transcode the video to a smaller format before upload."""
    default_mime = "video/x-msvideo"
    if not REFEREE_TRANSCODE_VIDEO:
        return video_path, default_mime, None

    ffmpeg_path = shutil.which("ffmpeg")
    if not ffmpeg_path:
        append_to_phrase_log(phrase_dir, base_name, "FFmpeg not found; uploading raw video")
        return video_path, default_mime, None

    temp_root = Path(tempfile.mkdtemp(prefix="upload_", dir=str(phrase_dir)))
    compressed_path = temp_root / f"{video_path.stem}_compressed.mp4"

    cmd = [
        ffmpeg_path,
        "-y",
        "-i",
        str(video_path),
        "-c:v",
        "libx264",
        "-preset",
        REFEREE_TRANSCODE_PRESET,
        "-crf",
        str(REFEREE_TRANSCODE_CRF),
        "-movflags",
        "faststart",
        "-an",
        str(compressed_path),
    ]

    try:
        completed = subprocess.run(cmd, capture_output=True, check=False)
        if completed.returncode != 0 or not compressed_path.exists():
            append_to_phrase_log(
                phrase_dir,
                base_name,
                f"FFmpeg transcode failed (code {completed.returncode}); uploading raw video",
            )
            shutil.rmtree(temp_root, ignore_errors=True)
            return video_path, default_mime, None

        original_size = video_path.stat().st_size if video_path.exists() else 0
        compressed_size = compressed_path.stat().st_size if compressed_path.exists() else 0
        ratio = (compressed_size / original_size) if original_size else 1.0
        append_to_phrase_log(
            phrase_dir,
            base_name,
            f"FFmpeg compressed video to MP4 ({compressed_size / 1_000_000:.2f} MB, "
            f"{ratio:.0%} of original)",
        )
        return compressed_path, "video/mp4", temp_root
    except Exception as exc:
        append_to_phrase_log(phrase_dir, base_name, f"Video transcode error: {exc}; uploading raw video")
        shutil.rmtree(temp_root, ignore_errors=True)
        return video_path, default_mime, None


def _submit_phrase_to_referee(video_path: Path, signal_path: Path, phrase_dir: Path, base_name: str) -> Dict[str, Any]:
    if not REFEREE_SERVER_URL:
        raise RuntimeError("Pi judge server URL is not configured")
    if not video_path.is_file():
        raise FileNotFoundError(f"Video file not found: {video_path}")
    if not signal_path.is_file():
        raise FileNotFoundError(f"Signal file not found: {signal_path}")

    prepared_video_path, video_mime, cleanup_dir = prepare_video_for_upload(video_path, phrase_dir, base_name)

    try:
        response = submit_phrase(
            video_path=prepared_video_path,
            txt_path=signal_path,
            server=REFEREE_SERVER_URL,
            request_id=base_name,
            timeout=REFEREE_REQUEST_TIMEOUT,
        )
        payload = response_payload(response)
        if not response.ok and isinstance(payload, dict):
            payload.setdefault("status_code", response.status_code)
        return payload if isinstance(payload, dict) else {"payload": payload}
    finally:
        if cleanup_dir:
            shutil.rmtree(cleanup_dir, ignore_errors=True)


def _summarise_referee_result(result: Dict[str, Any]) -> str:
    winner = result.get("winner") or result.get("declared_winner")
    reason = result.get("natural_language_reason") or result.get("reason") or result.get("detail")
    summary = "Pi judge result"
    if winner:
        summary += f", winner={winner}"
    if reason:
        summary += f" ({reason})"
    return summary


def _streaming_result_source_label() -> str:
    if streaming_session_manager is not None:
        label = getattr(streaming_session_manager, "result_label", None)
        if isinstance(label, str) and label.strip():
            return label.strip()
    return "streaming"


def _streaming_apply_source_label() -> str:
    if streaming_session_manager is not None:
        label = getattr(streaming_session_manager, "apply_label", None)
        if isinstance(label, str) and label.strip():
            return label.strip()
    return "Pi judge (streaming)"


def _normalize_referee_winner(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    if normalized in {"right", "left"}:
        return normalized
    return None


def _local_fallback_winner() -> Optional[str]:
    if last_phrase_right_hit and not last_phrase_left_hit:
        return "right"
    if last_phrase_left_hit and not last_phrase_right_hit:
        return "left"
    return None


def _apply_referee_result(result: Dict[str, Any], phrase_dir: Path, base_name: str, source_label: str) -> str:
    global awaiting_winner, winner_recorded, phrase_requires_remote_review
    global awaiting_manual_winner, manual_winner_side

    summary = _summarise_referee_result(result)
    append_to_phrase_log(phrase_dir, base_name, summary)

    broadcast_reason = result.get("natural_language_reason") or result.get("reason")
    final_side = _normalize_referee_winner(result.get("winner") or result.get("declared_winner"))

    if final_side is None:
        fallback_side = _local_fallback_winner()
        if fallback_side:
            final_side = fallback_side
            append_to_phrase_log(
                phrase_dir,
                base_name,
                f"Pi judge returned no winner; falling back to scoreboard winner: {winner_side_label(final_side)}",
            )
            if not broadcast_reason:
                broadcast_reason = single_hit_broadcast_message(final_side)
            summary += f" | Fallback winner: {winner_side_label(final_side)}"

    winner_recorded = final_side is not None
    awaiting_winner = False
    phrase_requires_remote_review = False
    awaiting_manual_winner = False
    manual_winner_side = final_side

    if final_side:
        set_winner_indicator(final_side)
        log_winner_selection(final_side, source_label)
        broadcast_message = broadcast_reason or f"{source_label} confirms {winner_side_label(final_side)} wins."
        broadcast_winner_reason(final_side, broadcast_message)

    update_last_event(summary)
    return summary


def _streaming_result_worker(phrase_dir: Path, base_name: str) -> None:
    """Background worker to wait for streaming result."""
    global awaiting_remote_result, awaiting_winner, winner_recorded
    global phrase_requires_remote_review, awaiting_manual_winner, manual_winner_side, declared_winner_side
    global camera_status_message, streaming_session_manager

    result_path = phrase_dir / f"{base_name}_result.json"
    result_source = _streaming_result_source_label()
    apply_source = _streaming_apply_source_label()

    try:
        append_to_phrase_log(phrase_dir, base_name, f"Waiting for {result_source} processing result")
        update_last_event(f"Processing via {result_source}...")

        # Wait for result from streaming session
        if not streaming_session_manager:
            raise RuntimeError("No active streaming session")

        result = streaming_session_manager.get_result(timeout=REFEREE_REQUEST_TIMEOUT)

        if not result:
            raise RuntimeError("No result received from streaming session")

        # Save result to file
        with open(result_path, "w", encoding="utf-8") as handle:
            json.dump(result, handle, indent=2, sort_keys=True)
            handle.write("\n")

        append_to_phrase_log(phrase_dir, base_name, f"{apply_source} result saved to {result_path.name}")
        _apply_referee_result(result, phrase_dir, base_name, apply_source)
    except Exception as exc:
        error_message = f"{apply_source} result error: {exc}"
        append_to_phrase_log(phrase_dir, base_name, error_message)
        awaiting_winner = False
        phrase_requires_remote_review = False
        awaiting_manual_winner = False
        update_last_event(error_message)
    finally:
        awaiting_remote_result = False
        camera_status_message = "Ready"
        streaming_session_manager = None
        if app and hasattr(app, "root"):
            app.root.after(0, app.refresh)


def _remote_referee_worker(phrase_dir: Path, base_name: str) -> None:
    """Background upload/analysis worker."""
    global awaiting_remote_result, awaiting_winner, winner_recorded
    global phrase_requires_remote_review, awaiting_manual_winner, manual_winner_side, declared_winner_side
    global camera_status_message

    video_path, signal_path, result_path = _phrase_artifact_paths(phrase_dir, base_name)

    append_to_phrase_log(phrase_dir, base_name, "Uploading phrase to Pi judge over Ethernet")
    update_last_event("Uploading phrase to Pi judge...")

    try:
        result = _submit_phrase_to_referee(video_path, signal_path, phrase_dir, base_name)
        with open(result_path, "w", encoding="utf-8") as handle:
            json.dump(result, handle, indent=2, sort_keys=True)
            handle.write("\n")

        append_to_phrase_log(phrase_dir, base_name, f"Pi judge response saved to {result_path.name}")
        _apply_referee_result(result, phrase_dir, base_name, "Pi judge")
    except Exception as exc:
        error_message = f"Pi judge error: {exc}"
        append_to_phrase_log(phrase_dir, base_name, error_message)
        awaiting_winner = False
        phrase_requires_remote_review = False
        awaiting_manual_winner = False
        update_last_event(error_message)
    finally:
        awaiting_remote_result = False
        camera_status_message = "Ready"
        if app and hasattr(app, "root"):
            app.root.after(0, app.refresh)


def start_remote_referee_pipeline() -> None:
    """Finalize the current phrase and submit it to the Pi judge service."""
    global awaiting_remote_result, referee_upload_thread, awaiting_winner, camera_status_message
    global streaming_session_manager

    if not send_to_server_mode:
        update_last_event("Server mode disabled. Result stored locally.")
        return

    if not phrase_requires_remote_review:
        update_last_event("Pi judge skipped: phrase does not require review")
        return

    resolved = _resolve_latest_phrase()
    if not resolved:
        update_last_event("Pi judge skipped: no phrase artifacts found")
        return

    has_active_streaming = bool(streaming_session_manager and streaming_session_manager.is_active())

    if REFEREE_USE_LOCAL_STREAMING_ANALYZER and not has_active_streaming and not REFEREE_SERVER_URL:
        awaiting_winner = True
        update_last_event("Local analyzer unavailable; result must be selected manually")
        return

    if not has_active_streaming and not REFEREE_SERVER_URL:
        awaiting_winner = True
        update_last_event("Pi judge skipped: server URL not configured")
        return

    phrase_dir, base_name = resolved

    if signal_file_handle:
        try:
            signal_file_handle.flush()
        except Exception:
            pass

    stop_phrase_recording()

    if awaiting_remote_result:
        return

    # Check if we're using streaming
    if streaming_session_manager and streaming_session_manager.is_active():
        append_to_phrase_log(phrase_dir, base_name, f"Waiting for {_streaming_result_source_label()} result")
        update_last_event(f"Processing via {_streaming_result_source_label()}...")
        camera_status_message = "Processing"

        awaiting_remote_result = True
        thread = threading.Thread(
            target=_streaming_result_worker,
            args=(phrase_dir, base_name),
            daemon=True,
        )
        referee_upload_thread = thread
        thread.start()
    else:
        # Fall back to traditional upload
        append_to_phrase_log(phrase_dir, base_name, "Preparing Pi judge submission")
        update_last_event("Preparing Pi judge submission...")
        camera_status_message = "Uploading to Pi judge"

        awaiting_remote_result = True
        thread = threading.Thread(
            target=_remote_referee_worker,
            args=(phrase_dir, base_name),
            daemon=True,
        )
        referee_upload_thread = thread
        thread.start()


def handle_phrase_results(score_summary: str) -> None:
    """Interpret phrase results, handle automatic winners, and manage review workflow."""
    global awaiting_winner, awaiting_manual_winner, winner_recorded
    global phrase_requires_remote_review, manual_winner_side
    global last_phrase_right_hit, last_phrase_left_hit
    global camera_status_message

    phrase_info = _resolve_latest_phrase()
    right_hit, left_hit = interpret_score_summary(score_summary)
    last_phrase_right_hit = bool(right_hit)
    last_phrase_left_hit = bool(left_hit)
    manual_winner_side = None
    clear_winner_indicator()

    if right_hit is not None and left_hit is not None:
        interpretation = (
            f"Interpretation -> Right Fencer: {'HIT' if right_hit else 'MISS'}, "
            f"Left Fencer: {'HIT' if left_hit else 'MISS'}"
        )
        if phrase_info:
            append_to_phrase_log(phrase_info[0], phrase_info[1], interpretation)

    winner_recorded = False
    awaiting_manual_winner = False
    awaiting_winner = False
    phrase_requires_remote_review = False

    double_hit = bool(right_hit) and bool(left_hit)
    single_hit = bool(right_hit) ^ bool(left_hit)

    if double_hit:
        if send_to_server_mode:
            phrase_requires_remote_review = True
            awaiting_manual_winner = False
            awaiting_winner = True
            if phrase_info:
                append_to_phrase_log(phrase_info[0], phrase_info[1], "Submitting double-hit phrase to Pi judge")
            update_last_event("Double hit detected. Sending phrase to Pi judge...")
            camera_status_message = "Uploading to Pi judge"
            start_remote_referee_pipeline()
        else:
            phrase_requires_remote_review = False
            awaiting_manual_winner = True
            awaiting_winner = True
            update_last_event("Double hit detected. Select the winner (local archive only).")
            if phrase_info:
                append_to_phrase_log(phrase_info[0], phrase_info[1], "Awaiting manual winner selection (double hit)")
            camera_status_message = "Awaiting manual winner"
    elif single_hit:
        if send_to_server_mode:
            phrase_requires_remote_review = True
            awaiting_manual_winner = False
            awaiting_winner = True
            if phrase_info:
                append_to_phrase_log(phrase_info[0], phrase_info[1], "Submitting single-hit phrase to Pi judge")
            update_last_event("Single hit detected. Sending phrase to Pi judge...")
            camera_status_message = "Uploading to Pi judge"
            start_remote_referee_pipeline()
        else:
            phrase_requires_remote_review = False
            awaiting_manual_winner = True
            awaiting_winner = True
            update_last_event("Select the winner to confirm the result.")
            if phrase_info:
                append_to_phrase_log(
                    phrase_info[0],
                    phrase_info[1],
                    "Awaiting manual winner selection (single hit confirmation)"
                )
            camera_status_message = "Awaiting winner confirmation"
    else:
        phrase_requires_remote_review = False
        update_last_event("No valid winner detected; ready for next phrase.")
        camera_status_message = "Ready"


def ensure_beep_asset() -> Path:
    """Create (if needed) a 1s sine-wave tone for macOS playback."""
    asset_path = Path(__file__).with_name("beep_tone_10s.wav")

    if asset_path.exists():
        return asset_path

    sample_rate = 44100
    duration = 1.0
    frequency = 900.0
    amplitude = 0.3
    num_samples = int(sample_rate * duration)

    with wave.open(str(asset_path), "w") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)  # 16-bit
        wav_file.setframerate(sample_rate)
        for i in range(num_samples):
            sample = amplitude * math.sin(2 * math.pi * frequency * (i / sample_rate))
            wav_file.writeframes(struct.pack("<h", int(sample * 32767)))

    return asset_path


class BeepController:
    """Manage a continuous beep that stays active while any hit lamps are on."""

    def __init__(self):
        self._lock = threading.Lock()
        self._sources: Set[str] = set()
        self._system = platform.system()
        self._process: Optional[subprocess.Popen] = None
        self._stop_event: Optional[threading.Event] = None
        self._thread: Optional[threading.Thread] = None
        self._beep_asset: Optional[Path] = None
        self._windows_active = False

    def start(self, source: str) -> None:
        with self._lock:
            self._sources.add(source)
            if self._is_playing():
                return
            self._start_output()

    def stop(self, source: str) -> None:
        with self._lock:
            if source in self._sources:
                self._sources.remove(source)
            if not self._sources:
                self._stop_output()

    def reset(self) -> None:
        with self._lock:
            self._sources.clear()
            self._stop_output()

    def _is_playing(self) -> bool:
        if self._system == "Windows":
            return self._windows_active
        if self._system == "Darwin":
            return self._thread is not None and self._thread.is_alive()
        return self._thread is not None and self._thread.is_alive()

    def _start_output(self) -> None:
        if self._system == "Windows":
            try:
                import winsound
                winsound.PlaySound("SystemHand", winsound.SND_ASYNC | winsound.SND_LOOP | winsound.SND_ALIAS)
                self._windows_active = True
            except Exception:
                self._start_fallback_thread()
            return

        if self._system == "Darwin":
            try:
                if not self._beep_asset:
                    self._beep_asset = ensure_beep_asset()
                if self._thread and self._thread.is_alive():
                    return
                self._stop_event = threading.Event()
                self._thread = threading.Thread(target=self._mac_beep_loop, daemon=True)
                self._thread.start()
            except Exception:
                self._process = None
                self._start_fallback_thread()
            return

        self._start_fallback_thread()

    def _start_fallback_thread(self) -> None:
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._console_beep_loop, daemon=True)
        self._thread.start()

    def _stop_output(self) -> None:
        if self._system == "Windows":
            try:
                import winsound
                winsound.PlaySound(None, winsound.SND_PURGE)
            except Exception:
                pass
            self._windows_active = False
            return

        if self._system == "Darwin":
            if self._stop_event:
                self._stop_event.set()
            if self._thread:
                self._thread.join(timeout=1)
            if self._process:
                try:
                    self._process.terminate()
                    self._process.wait(timeout=1)
                except Exception:
                    pass
            self._process = None
            self._stop_event = None
            self._thread = None
            return

        if self._stop_event:
            self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=1)
        self._stop_event = None
        self._thread = None

    def _mac_beep_loop(self) -> None:
        if not self._beep_asset:
            self._console_beep_loop()
            return
        stop_event = self._stop_event
        while stop_event and not stop_event.is_set():
            try:
                self._process = subprocess.Popen(
                    ["afplay", "-q", "1", str(self._beep_asset)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                while True:
                    if stop_event.is_set():
                        try:
                            self._process.terminate()
                            self._process.wait(timeout=1)
                        except Exception:
                            pass
                        break
                    ret = self._process.poll()
                    if ret is not None:
                        break
                    time.sleep(0.05)
            except Exception:
                time.sleep(0.1)
            finally:
                if self._process:
                    if self._process.poll() is None:
                        try:
                            self._process.terminate()
                            self._process.wait(timeout=1)
                        except Exception:
                            pass
                    self._process = None
            if stop_event.is_set():
                break

    def _console_beep_loop(self) -> None:
        stop_event = self._stop_event
        if not stop_event:
            return
        while not stop_event.is_set():
            sys.stdout.write('\a')
            sys.stdout.flush()
            if stop_event.wait(0.05):
                break


beep_controller = BeepController()


class StreamingSessionManager:
    """Manages real-time video streaming to the referee service via WebSocket."""

    def __init__(self, server_url: str):
        """Initialize streaming session manager.

        Args:
            server_url: Base URL of server (e.g., "http://localhost:8080")
        """
        self.server_url = server_url.rstrip("/")

        # Convert HTTP URL to WebSocket URL
        if server_url.startswith("http://"):
            self.ws_url = server_url.replace("http://", "ws://", 1)
        elif server_url.startswith("https://"):
            self.ws_url = server_url.replace("https://", "wss://", 1)
        else:
            self.ws_url = server_url

        self.ws_url = f"{self.ws_url}/stream"

        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._frame_queue: Optional[queue.Queue] = None
        self._session_id: Optional[str] = None
        self._active = False
        self._result: Optional[Dict[str, Any]] = None
        self._error: Optional[Exception] = None
        self._encoding = REFEREE_STREAMING_ENCODING
        self._jpeg_quality = REFEREE_STREAMING_JPEG_QUALITY

    def start_session(
        self,
        session_id: str,
        fps: float,
        width: int,
        height: int,
        expected_frames: int = 0,
    ) -> bool:
        """Start a new streaming session.

        Args:
            session_id: Unique identifier for this session
            fps: Frames per second
            width: Frame width
            height: Frame height
            expected_frames: Expected total frames (0 if unknown)

        Returns:
            True if session started successfully, False otherwise
        """
        if self._active:
            print(f"[STREAMING] Session already active, cannot start new one")
            return False

        print(f"[STREAMING] Starting session: {session_id}")
        print(f"[STREAMING] - Video: {width}x{height} @ {fps} FPS")
        print(f"[STREAMING] - Encoding: {self._encoding} (quality: {self._jpeg_quality})")
        print(f"[STREAMING] - WebSocket URL: {self.ws_url}")

        self._session_id = session_id
        self._frame_queue = queue.Queue(maxsize=120)  # Buffer up to 4 seconds at 30fps
        self._active = True
        self._result = None
        self._error = None

        # Start background thread with asyncio event loop
        self._thread = threading.Thread(
            target=self._run_async_loop,
            args=(session_id, fps, width, height, expected_frames),
            daemon=True,
        )
        self._thread.start()

        return True

    def queue_frame(self, frame: Any, frame_number: int) -> bool:
        """Queue a frame for streaming.

        Args:
            frame: OpenCV frame (numpy array)
            frame_number: Frame sequence number

        Returns:
            True if queued successfully, False if queue is full or session not active
        """
        if not self._active or not self._frame_queue:
            if frame_number == 0:
                print(f"[STREAMING] WARNING: Cannot queue frame {frame_number}, session not active")
            return False

        try:
            # Non-blocking put with timeout
            self._frame_queue.put((frame, frame_number), block=True, timeout=0.1)
            # Log every 30 frames (once per second at 30fps)
            if frame_number % 30 == 0:
                print(f"[STREAMING] Queued frame {frame_number}")
            return True
        except queue.Full:
            print(f"[STREAMING] WARNING: Frame queue full, dropping frame {frame_number}")
            return False

    def end_session(self, signal_data: bytes, signal_filename: str, total_frames: int) -> None:
        """End the streaming session and send signal data.

        Args:
            signal_data: Signal file bytes
            signal_filename: Name of signal file
            total_frames: Total number of frames sent
        """
        if not self._active or not self._frame_queue:
            print(f"[STREAMING] end_session called but session not active")
            return

        # Send sentinel to signal end of frames
        print(f"[STREAMING] Queueing END_SESSION sentinel (total_frames={total_frames}, signal_size={len(signal_data)})")
        try:
            self._frame_queue.put(("END_SESSION", total_frames, signal_data, signal_filename), block=False)
            print(f"[STREAMING] END_SESSION sentinel queued successfully")
        except queue.Full:
            print(f"[STREAMING] ERROR: Frame queue full, cannot queue END_SESSION")

    def cancel_session(self, reason: str = "") -> None:
        """Abort the streaming session without uploading signal/video."""
        if not self._active or not self._frame_queue:
            print("[STREAMING] cancel_session called but session not active")
            return
        reason = reason or "user_cancelled"
        print(f"[STREAMING] Cancelling streaming session ({reason})")
        try:
            self._frame_queue.put(("CANCEL_SESSION", reason), block=False)
        except queue.Full:
            print("[STREAMING] ERROR: Frame queue full, cannot cancel session")

    def get_result(self, timeout: float = 300.0) -> Optional[Dict[str, Any]]:
        """Wait for and return the processing result.

        Args:
            timeout: Maximum time to wait in seconds

        Returns:
            Result dictionary if successful, None if timeout or error
        """
        if self._thread:
            self._thread.join(timeout=timeout)

        if self._error:
            raise self._error

        return self._result

    def is_active(self) -> bool:
        """Check if session is currently active."""
        return self._active

    def _run_async_loop(
        self,
        session_id: str,
        fps: float,
        width: int,
        height: int,
        expected_frames: int,
    ) -> None:
        """Run the asyncio event loop in background thread."""
        print(f"[STREAMING] Background thread started for session: {session_id}")
        try:
            # Create new event loop for this thread
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)

            # Run the streaming coroutine
            self._loop.run_until_complete(
                self._stream_session(session_id, fps, width, height, expected_frames)
            )
            print(f"[STREAMING] Background thread completed successfully")
        except Exception as exc:
            print(f"[STREAMING] ERROR in background thread: {exc}")
            import traceback
            traceback.print_exc()
            self._error = exc
        finally:
            if self._loop:
                self._loop.close()
            self._active = False
            print(f"[STREAMING] Background thread exiting")

    async def _stream_session(
        self,
        session_id: str,
        fps: float,
        width: int,
        height: int,
        expected_frames: int,
    ) -> None:
        """Async coroutine to handle WebSocket streaming."""
        try:
            print(f"[STREAMING] Connecting to WebSocket: {self.ws_url}")
            async with websockets.connect(
                self.ws_url,
                ping_interval=20,
                ping_timeout=10,
                close_timeout=10,
            ) as websocket:
                print(f"[STREAMING] WebSocket connected successfully")

                # Send session start
                start_message = {
                    "type": "session_start",
                    "session_id": session_id,
                    "fps": fps,
                    "width": width,
                    "height": height,
                    "expected_frames": expected_frames,
                    "video_format": "bgr24",
                }

                print(f"[STREAMING] Sending session_start message")
                await websocket.send(json.dumps(start_message))

                # Wait for acknowledgment
                print(f"[STREAMING] Waiting for session_start acknowledgment...")
                response = await asyncio.wait_for(websocket.recv(), timeout=30.0)
                response_data = json.loads(response)
                print(f"[STREAMING] Received response: {response_data.get('type')}")

                if response_data.get("type") == "error":
                    error_msg = response_data.get('error_message')
                    print(f"[STREAMING] Server error: {error_msg}")
                    raise RuntimeError(f"Server error: {error_msg}")

                # Stream frames from queue
                print(f"[STREAMING] Session started, ready to receive frames")
                frame_count = 0
                cancelled = False
                while True:
                    # Get frame from queue (non-blocking check)
                    try:
                        item = await asyncio.get_event_loop().run_in_executor(
                            None, self._frame_queue.get, True, 1.0
                        )
                    except queue.Empty:
                        continue

                    # Check for end sentinel (check if first element is a string)
                    if isinstance(item, tuple) and len(item) >= 1 and isinstance(item[0], str):
                        marker = item[0]
                        if marker == "END_SESSION" and len(item) == 4:
                            total_frames = item[1]
                            signal_data = item[2]
                            signal_filename = item[3]
                            print(f"[STREAMING] END_SESSION received, total_frames={total_frames}")
                            break
                        if marker == "CANCEL_SESSION":
                            cancel_reason = item[1] if len(item) > 1 else ""
                            print(f"[STREAMING] CANCEL_SESSION received ({cancel_reason})")
                            cancelled = True
                            break

                    frame, frame_number = item

                    # Log first frame
                    if frame_number == 0:
                        print(f"[STREAMING] Received first frame from queue")

                    # Encode frame
                    if self._encoding == "jpeg":
                        encode_params = [cv2.IMWRITE_JPEG_QUALITY, self._jpeg_quality]
                        ret, encoded = cv2.imencode(".jpg", frame, encode_params)
                    elif self._encoding == "png":
                        ret, encoded = cv2.imencode(".png", frame)
                    elif self._encoding == "raw":
                        h, w, c = frame.shape
                        header = (
                            h.to_bytes(4, "little")
                            + w.to_bytes(4, "little")
                            + c.to_bytes(4, "little")
                        )
                        encoded = header + frame.tobytes()
                        ret = True
                    else:
                        continue

                    if not ret:
                        continue

                    if self._encoding in ("jpeg", "png"):
                        frame_bytes = encoded.tobytes()
                    else:
                        frame_bytes = encoded

                    # Frame numbers must be contiguous for the server; if capture skipped numbers
                    # (e.g., dropped frames), remap to the sequential count actually delivered.
                    frame_number_to_emit = frame_count
                    if frame_number != frame_number_to_emit:
                        print(
                            "[STREAMING] NOTE: Remapping captured frame "
                            f"{frame_number} to sequential {frame_number_to_emit}"
                        )

                    # Send frame metadata
                    frame_message = {
                        "type": "frame",
                        "session_id": session_id,
                        "frame_number": frame_number_to_emit,
                        "timestamp": time.time(),
                        "encoding": self._encoding,
                        "quality": self._jpeg_quality if self._encoding == "jpeg" else 100,
                        "size": len(frame_bytes),
                    }

                    await websocket.send(json.dumps(frame_message))
                    await websocket.send(frame_bytes)

                    # Wait for ACK
                    ack = await asyncio.wait_for(websocket.recv(), timeout=10.0)
                    ack_data = json.loads(ack)

                    if ack_data.get("type") == "error":
                        error_msg = ack_data.get("error_message", "Unknown error")
                        recoverable = ack_data.get("recoverable", False)
                        print(f"[STREAMING] Frame {frame_number} error: {error_msg} (recoverable: {recoverable})")
                        if not recoverable:
                            raise RuntimeError(f"Server error: {error_msg}")

                    frame_count += 1

                    # Log every 30 frames
                    if frame_count % 30 == 0:
                        print(f"[STREAMING] Sent {frame_count} frames")

                # Use the actual number of frames that were acknowledged by the server.
                actual_frames_sent = frame_count
                if cancelled:
                    print(f"[STREAMING] Session cancelled; skipping upload of {actual_frames_sent} frames")
                    return
                if total_frames != actual_frames_sent:
                    print(
                        "[STREAMING] NOTE: Adjusting total_frames from "
                        f"{total_frames} to actual sent count {actual_frames_sent}"
                    )
                    total_frames = actual_frames_sent

                # Send session end
                print(f"[STREAMING] Sending session_end (total_frames={total_frames})")
                end_message = {
                    "type": "session_end",
                    "session_id": session_id,
                    "total_frames": total_frames,
                }

                await websocket.send(json.dumps(end_message))

                # Wait for session end ACK
                print(f"[STREAMING] Waiting for session_end acknowledgment...")
                response = await asyncio.wait_for(websocket.recv(), timeout=30.0)
                response_data = json.loads(response)
                print(f"[STREAMING] session_end response: {response_data.get('type')}")

                if response_data.get("type") == "error":
                    error_msg = response_data.get('error_message')
                    print(f"[STREAMING] session_end error: {error_msg}")
                    raise RuntimeError(f"Server error: {error_msg}")

                # Send signal data
                print(f"[STREAMING] Sending signal data ({len(signal_data)} bytes)")
                signal_message = {
                    "type": "signal",
                    "session_id": session_id,
                    "filename": signal_filename,
                    "size": len(signal_data),
                }

                await websocket.send(json.dumps(signal_message))
                await websocket.send(signal_data)
                print(f"[STREAMING] Signal data sent")

                # Wait for processing messages
                print(f"[STREAMING] Waiting for processing result...")
                while True:
                    response = await asyncio.wait_for(websocket.recv(), timeout=300.0)
                    response_data = json.loads(response)

                    msg_type = response_data.get("type")
                    print(f"[STREAMING] Received message type: {msg_type}")

                    if msg_type == "process_complete":
                        print(f"[STREAMING] Processing complete!")
                        self._result = response_data.get("result", {})
                        break
                    elif msg_type == "process_progress":
                        stage = response_data.get("stage", "")
                        progress = response_data.get("progress", 0.0)
                        message = response_data.get("message", "")
                        print(f"[STREAMING] Progress: {stage} ({progress*100:.0f}%) - {message}")
                    elif msg_type == "error":
                        error_msg = response_data.get("error_message", "Unknown error")
                        print(f"[STREAMING] Processing error: {error_msg}")
                        raise RuntimeError(f"Processing error: {error_msg}")

        except Exception as exc:
            print(f"[STREAMING] Exception in _stream_session: {exc}")
            import traceback
            traceback.print_exc()
            self._error = exc
        finally:
            self._active = False
            print(f"[STREAMING] _stream_session exiting")


def play_start_audio() -> tuple[float, float]:
    """Play the 'On guard, ready, fence' audio cue and return (start_ts, end_ts)."""
    cue_start = time.time()
    try:
        system = platform.system()
        if system == "Darwin":
            subprocess.run(
                ["say", "On guard. Ready. Fence."],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        elif system == "Windows":
            command = (
                'Add-Type -AssemblyName System.speech;'
                '$speak = New-Object System.Speech.Synthesis.SpeechSynthesizer;'
                '$speak.Speak("On guard. Ready. Fence.");'
            )
            subprocess.run(
                ["powershell", "-Command", command],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            subprocess.run(
                ["espeak", "On guard. Ready. Fence."],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
    except Exception as exc:
        print(f"Audio cue failed: {exc}")
    cue_end = time.time()
    return cue_start, cue_end


class _NativeGStreamerCapture:
    """Read raw BGR frames from a native GStreamer pipeline via stdout."""

    def __init__(self, *, device_path: Path, width: int, height: int, fps: float) -> None:
        self.device_path = Path(device_path).resolve()
        self.width = int(width)
        self.height = int(height)
        self.fps = float(fps)
        self.frame_bytes = self.width * self.height * 3
        self._stderr_tail: deque[str] = deque(maxlen=80)
        self._stderr_thread: Optional[threading.Thread] = None
        self._process: Optional[subprocess.Popen[bytes]] = None
        self._stdout_fd: Optional[int] = None
        self._closed = False
        self._launch()

    def _launch(self) -> None:
        gst_launch = shutil.which("gst-launch-1.0")
        if not gst_launch:
            raise RuntimeError("gst-launch-1.0 is not available")

        target_fps = max(1, int(round(self.fps or 30.0)))
        cmd = [
            gst_launch,
            "-q",
            "v4l2src",
            f"device={self.device_path}",
            "do-timestamp=true",
            "!",
            f"image/jpeg,width={self.width},height={self.height},framerate={target_fps}/1",
            "!",
            "jpegdec",
            "!",
            "videoconvert",
            "!",
            "video/x-raw,format=BGR",
            "!",
            "fdsink",
            "fd=1",
            "sync=false",
        ]

        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        if process.stdout is None or process.stderr is None:
            process.kill()
            raise RuntimeError("Failed to create GStreamer stdout/stderr pipes")

        self._process = process
        self._stdout_fd = process.stdout.fileno()
        self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stderr_thread.start()

    def _drain_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        try:
            while True:
                line = process.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                if not text:
                    continue
                self._stderr_tail.append(text)
                if CONSOLE_DETAIL_LOGS:
                    print(f"[GSTREAMER_CAPTURE] {text}", flush=True)
        finally:
            try:
                process.stderr.close()
            except Exception:
                pass

    def _read_exact(self, size: int, timeout: float = 2.0) -> Optional[bytes]:
        stdout_fd = self._stdout_fd
        if stdout_fd is None:
            return None

        deadline = time.perf_counter() + max(0.0, timeout)
        data = bytearray()
        while len(data) < size:
            process = self._process
            if process is None:
                return None
            if process.poll() is not None and len(data) == 0:
                return None

            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                return None

            try:
                ready, _, _ = select.select([stdout_fd], [], [], remaining)
            except (ValueError, OSError):
                return None
            if not ready:
                return None

            try:
                chunk = os.read(stdout_fd, size - len(data))
            except OSError:
                return None
            if not chunk:
                return None
            data.extend(chunk)

        return bytes(data)

    def isOpened(self) -> bool:
        return self._process is not None and self._process.poll() is None and self._stdout_fd is not None

    def read(self) -> tuple[bool, Optional[np.ndarray]]:
        payload = self._read_exact(self.frame_bytes)
        if payload is None:
            return False, None
        frame = np.frombuffer(payload, dtype=np.uint8).reshape((self.height, self.width, 3))
        return True, frame

    def get(self, prop_id: int) -> float:
        if prop_id == cv2.CAP_PROP_FRAME_WIDTH:
            return float(self.width)
        if prop_id == cv2.CAP_PROP_FRAME_HEIGHT:
            return float(self.height)
        if prop_id == cv2.CAP_PROP_FPS:
            return float(self.fps)
        return 0.0

    def error_summary(self) -> str:
        if not self._stderr_tail:
            return ""
        return " | ".join(list(self._stderr_tail)[-4:])

    def release(self) -> None:
        if self._closed:
            return
        self._closed = True

        process = self._process
        self._process = None
        self._stdout_fd = None
        if process is None:
            return

        try:
            if process.stdout is not None:
                process.stdout.close()
        except Exception:
            pass

        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    pass
        else:
            try:
                process.wait(timeout=0.1)
            except subprocess.TimeoutExpired:
                pass

        if self._stderr_thread and self._stderr_thread.is_alive():
            self._stderr_thread.join(timeout=1.0)


class CameraRecorder:
    """Lightweight camera helper that keeps the device open and writes frames while active."""

    def __init__(self, index: int = 0):
        self.index = index
        self.capture = None
        self._prime_frame = None
        self.source_description = ""
        self.target_width = CAMERA_WIDTH
        self.target_height = CAMERA_HEIGHT
        self.target_fps = CAMERA_TARGET_FPS

        self._open_camera_with_probe()

        print(
            f"Camera initialized at {self.width}x{self.height} @ {self.fps:.2f} FPS "
            f"(requested {self.target_width}x{self.target_height} @ {self.target_fps} FPS, "
            f"source {self.source_description})"
        )

        self.recording = False
        self.writer = None
        self._writer_io_lock = threading.Lock()
        self.lock = threading.Lock()
        self.overlay_frame_counter = os.environ.get("FENCING_OVERLAY_FRAME_COUNTER", "true").lower() in {"1", "true", "yes"}

        # Streaming support
        self.streaming_manager: Optional[StreamingSessionManager] = None
        self.frame_counter = 0
        self.record_start_time_ns: Optional[int] = None
        self.last_recorded_fps: Optional[float] = None
        self.recorded_frame_timestamps_ns: list[int] = []
        self._prime_frame_capture_ns = time.perf_counter_ns() if self._prime_frame is not None else None
        self._shutdown_event = threading.Event()
        self._capture_fail_streak = 0
        self._writer_queue: "queue.Queue[tuple[str, Any] | tuple[str, int, Any]]" = queue.Queue(
            maxsize=max(1, CAMERA_WRITER_QUEUE_MAX)
        )
        self._writer_block_warned = False
        self._analysis_queue: "queue.Queue[tuple[str, Any] | tuple[str, Any, int, Any]]" = queue.Queue(
            maxsize=max(1, CAMERA_ANALYSIS_QUEUE_MAX)
        )
        self._analysis_drop_warned = False
        self._writer_thread = threading.Thread(target=self._writer_loop, daemon=True)
        self._analysis_thread = threading.Thread(target=self._analysis_loop, daemon=True)
        self._capture_thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._writer_thread.start()
        self._analysis_thread.start()
        self._capture_thread.start()

    @staticmethod
    def _video_node_number(path: Path) -> Optional[int]:
        match = re.fullmatch(r"video(\d+)", path.name)
        if not match:
            return None
        return int(match.group(1))

    @classmethod
    def _sysfs_video_index(cls, path: Path) -> Optional[int]:
        node_number = cls._video_node_number(path)
        if node_number is None:
            return None
        sysfs_index = Path(f"/sys/class/video4linux/video{node_number}/index")
        if not sysfs_index.exists():
            return None
        try:
            return int(sysfs_index.read_text(encoding="utf-8").strip())
        except Exception:
            return None

    @classmethod
    def _linux_capture_nodes(cls) -> list[tuple[Path, int]]:
        nodes: list[tuple[Path, int]] = []
        seen_nodes: set[Path] = set()

        for pattern in ("/dev/v4l/by-id/*-video-index0", "/dev/v4l/by-path/*-video-index0"):
            for path in sorted(Path("/").glob(pattern.lstrip("/"))):
                if not path.exists():
                    continue
                resolved = path.resolve()
                node_number = cls._video_node_number(resolved)
                if node_number is None or resolved in seen_nodes:
                    continue
                seen_nodes.add(resolved)
                nodes.append((resolved, node_number))

        for path in sorted(Path("/dev").glob("video*")):
            node_number = cls._video_node_number(path)
            if node_number is None or path in seen_nodes:
                continue
            if cls._sysfs_video_index(path) != 0:
                continue
            seen_nodes.add(path)
            nodes.append((path, node_number))

        return nodes

    def _native_gstreamer_candidate_specs(self) -> list[tuple[Path, str]]:
        if not sys.platform.startswith("linux"):
            return []
        if not NATIVE_GSTREAMER_CAPTURE:
            return []
        if not shutil.which("gst-launch-1.0"):
            return []

        specs: list[tuple[Path, str]] = []
        seen: set[Path] = set()

        def _add(path: Path) -> None:
            resolved = path.resolve()
            if resolved in seen:
                return
            seen.add(resolved)
            specs.append((resolved, f"{resolved} via native GStreamer"))

        if CAMERA_DEVICE:
            camera_path = Path(CAMERA_DEVICE).expanduser()
            if camera_path.exists():
                resolved = camera_path.resolve()
                requested_node_number = self._video_node_number(resolved)
                requested_sysfs_index = self._sysfs_video_index(resolved)
                if requested_node_number is not None and requested_sysfs_index == 0:
                    _add(resolved)

        for video_path, _node_number in self._linux_capture_nodes():
            _add(video_path)

        return specs

    def _candidate_specs(self) -> list[tuple[object, Optional[int], str, bool]]:
        specs: list[tuple[object, Optional[int], str, bool]] = []
        seen: set[tuple[str, str]] = set()
        notes: list[str] = []

        def _add(source: object, backend: Optional[int], label: str, *, mjpeg: bool = False) -> None:
            key = (repr(source), str(backend))
            if key in seen:
                return
            seen.add(key)
            specs.append((source, backend, label, mjpeg))

        def _gstreamer_mjpeg_pipeline(device_path: Path) -> str:
            width = max(1, int(self.target_width or 1280))
            height = max(1, int(self.target_height or 720))
            fps = max(1, int(round(self.target_fps or 30.0)))
            return (
                f"v4l2src device={device_path} do-timestamp=true ! "
                f"image/jpeg,width={width},height={height},framerate={fps}/1 ! "
                "jpegdec ! videoconvert ! video/x-raw,format=BGR ! "
                "appsink drop=true sync=false max-buffers=1"
            )

        if sys.platform == "darwin":
            _add(self.index, cv2.CAP_AVFOUNDATION, f"index {self.index} via AVFoundation")
            _add(self.index, cv2.CAP_ANY, f"index {self.index} via default backend")
            return specs

        if CAMERA_DEVICE:
            camera_path = Path(CAMERA_DEVICE).expanduser()
            if camera_path.exists():
                resolved = camera_path.resolve()
                requested_node_number = self._video_node_number(resolved)
                requested_sysfs_index = self._sysfs_video_index(resolved)
                if requested_node_number is not None and requested_sysfs_index == 0:
                    _add(requested_node_number, cv2.CAP_V4L2, f"index {requested_node_number} via V4L2", mjpeg=True)
                    _add(
                        _gstreamer_mjpeg_pipeline(resolved),
                        cv2.CAP_GSTREAMER,
                        f"{resolved} via GStreamer MJPEG",
                    )
                elif requested_node_number is not None:
                    notes.append(
                        f"{resolved} is video-index{requested_sysfs_index}; this is usually not the capture node. "
                        "Prefer the matching *-video-index0 device."
                    )
                else:
                    _add(
                        _gstreamer_mjpeg_pipeline(resolved),
                        cv2.CAP_GSTREAMER,
                        f"{resolved} via GStreamer MJPEG",
                    )

        _add(self.index, cv2.CAP_V4L2, f"index {self.index} via V4L2", mjpeg=True)

        for video_path, node_number in self._linux_capture_nodes():
            _add(node_number, cv2.CAP_V4L2, f"index {node_number} via V4L2", mjpeg=True)
            _add(
                _gstreamer_mjpeg_pipeline(video_path),
                cv2.CAP_GSTREAMER,
                f"{video_path} via GStreamer MJPEG",
            )

        for note in notes:
            _add(note, None, note)

        return specs

    def _probe_native_gstreamer_candidate(
        self,
        device_path: Path,
        label: str,
    ) -> tuple[Optional[_NativeGStreamerCapture], Optional[Any], Optional[str]]:
        try:
            candidate = _NativeGStreamerCapture(
                device_path=device_path,
                width=self.target_width,
                height=self.target_height,
                fps=self.target_fps,
            )
        except Exception as exc:
            return None, None, f"{label}: launch error ({exc})"

        frame = None
        readable_frames = 0
        for _ in range(3):
            ok, candidate_frame = candidate.read()
            if ok and candidate_frame is not None and getattr(candidate_frame, "size", 0) > 0:
                frame = candidate_frame
                readable_frames += 1
                continue
            break

        if frame is None or readable_frames < 3:
            error_detail = candidate.error_summary()
            candidate.release()
            if readable_frames <= 0:
                message = f"{label}: no readable frames during probe"
            else:
                message = f"{label}: only {readable_frames} readable frame(s) during probe"
            if error_detail:
                message += f" ({error_detail})"
            return None, None, message

        return candidate, frame, None

    def _probe_candidate(
        self,
        source: object,
        backend: Optional[int],
        label: str,
        mjpeg: bool,
    ) -> tuple[Optional[cv2.VideoCapture], Optional[Any], Optional[str]]:
        try:
            if backend is None or backend == cv2.CAP_ANY:
                candidate = cv2.VideoCapture(source)
            else:
                candidate = cv2.VideoCapture(source, backend)
        except Exception as exc:
            return None, None, f"{label}: open error ({exc})"

        if not candidate.isOpened():
            candidate.release()
            return None, None, f"{label}: open returned false"

        if backend == cv2.CAP_V4L2:
            candidate.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        if mjpeg:
            candidate.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        if self.target_width:
            candidate.set(cv2.CAP_PROP_FRAME_WIDTH, self.target_width)
        if self.target_height:
            candidate.set(cv2.CAP_PROP_FRAME_HEIGHT, self.target_height)
        if self.target_fps:
            candidate.set(cv2.CAP_PROP_FPS, self.target_fps)

        frame = None
        readable_frames = 0
        for _ in range(60):
            ok, candidate_frame = candidate.read()
            if ok and candidate_frame is not None and getattr(candidate_frame, "size", 0) > 0:
                frame = candidate_frame
                readable_frames += 1
                if readable_frames >= 3:
                    break
                time.sleep(0.02)
                continue
            time.sleep(0.05)

        if frame is None or readable_frames < 3:
            candidate.release()
            if readable_frames <= 0:
                return None, None, f"{label}: no readable frames during probe"
            return None, None, f"{label}: only {readable_frames} readable frame(s) during probe"

        return candidate, frame, None

    def _open_camera_with_probe(self) -> None:
        errors: list[str] = []

        for device_path, label in self._native_gstreamer_candidate_specs():
            candidate, frame, error = self._probe_native_gstreamer_candidate(device_path, label)
            if error is not None:
                errors.append(error)
                continue

            assert candidate is not None
            assert frame is not None
            self.capture = candidate
            self._prime_frame = frame
            self._prime_frame_capture_ns = time.perf_counter_ns()
            self.source_description = label
            self.height, self.width = frame.shape[:2]
            self.fps = float(self.capture.get(cv2.CAP_PROP_FPS) or 0)
            if not self.fps or self.fps < 1:
                self.fps = self.target_fps or 30.0
            return

        for source, backend, label, mjpeg in self._candidate_specs():
            if backend is None:
                errors.append(label)
                continue
            candidate, frame, error = self._probe_candidate(source, backend, label, mjpeg)
            if error is not None:
                errors.append(error)
                continue

            assert candidate is not None
            assert frame is not None
            self.capture = candidate
            self._prime_frame = frame
            self._prime_frame_capture_ns = time.perf_counter_ns()
            self.source_description = label
            self.height, self.width = frame.shape[:2]
            self.fps = float(self.capture.get(cv2.CAP_PROP_FPS) or 0)
            if not self.fps or self.fps < 1:
                self.fps = self.target_fps or 30.0
            return

        raise RuntimeError(
            "Could not open a camera source that produced frames. "
            + ("Attempts: " + "; ".join(errors) if errors else "Ensure the camera is connected and readable.")
        )

    def start(self, output_path: str, streaming_manager: Optional[StreamingSessionManager] = None) -> None:
        with self.lock:
            if self.recording:
                return

            self.frame_counter = 0
            self.recorded_frame_timestamps_ns = []
            self.streaming_manager = streaming_manager

            if REFEREE_STREAMING_SAVE_LOCAL or not streaming_manager:
                fourcc = cv2.VideoWriter_fourcc(*'MJPG')
                writer = cv2.VideoWriter(output_path, fourcc, self.fps, (self.width, self.height))
                if not writer.isOpened():
                    writer.release()
                    raise RuntimeError(f"Unable to open video file for writing: {output_path}")
                with self._writer_io_lock:
                    self.writer = writer
            else:
                with self._writer_io_lock:
                    self.writer = None

            self.recording = True
            self.record_start_time_ns = time.perf_counter_ns()
            self.last_recorded_fps = None
            self._capture_fail_streak = 0
            self._writer_block_warned = False
            self._analysis_drop_warned = False
            print(
                f"[CAMERA] Recording armed -> {output_path} "
                f"(streaming={'yes' if streaming_manager else 'no'}, source={self.source_description})",
                flush=True,
            )

    def stop(self) -> int:
        """Stop recording and return total frame count."""
        with self.lock:
            self.recording = False
            total_frames = self.frame_counter
            active_streaming_manager = self.streaming_manager
            self.streaming_manager = None

            if len(self.recorded_frame_timestamps_ns) >= 2:
                elapsed_ns = self.recorded_frame_timestamps_ns[-1] - self.recorded_frame_timestamps_ns[0]
                if elapsed_ns > 0:
                    self.last_recorded_fps = (total_frames - 1) / (elapsed_ns / 1_000_000_000)
                else:
                    self.last_recorded_fps = None
            elif self.record_start_time_ns:
                elapsed_ns = max(0, time.perf_counter_ns() - self.record_start_time_ns)
                if elapsed_ns > 0 and total_frames > 0:
                    self.last_recorded_fps = total_frames / (elapsed_ns / 1_000_000_000)
                else:
                    self.last_recorded_fps = None
            else:
                self.last_recorded_fps = None
            self.record_start_time_ns = None
            print(
                f"[CAMERA] Recording stopped -> frames={total_frames}, "
                f"measured_fps={self.last_recorded_fps if self.last_recorded_fps is not None else 'n/a'}",
                flush=True,
            )
            writer_active = self.writer is not None
        if writer_active:
            self._drain_writer_queue(timeout=3.0)
            with self._writer_io_lock:
                writer = self.writer
                self.writer = None
                if writer is not None:
                    writer.release()
        if active_streaming_manager is not None:
            self._drain_analysis_queue(timeout=2.0)
        return total_frames

    def update(self) -> None:
        # Capture now runs on a dedicated thread to avoid Tk/serial jitter.
        return

    def get_recorded_frame_timestamps_ns(self) -> list[int]:
        with self.lock:
            return list(self.recorded_frame_timestamps_ns)

    def _capture_loop(self) -> None:
        while not self._shutdown_event.is_set():
            if not self.capture or not self.capture.isOpened():
                time.sleep(0.01)
                continue

            if self._prime_frame is not None:
                frame = self._prime_frame
                capture_ns = self._prime_frame_capture_ns or time.perf_counter_ns()
                self._prime_frame = None
                self._prime_frame_capture_ns = None
            else:
                ret, frame = self.capture.read()
                capture_ns = time.perf_counter_ns()
                if not ret or frame is None:
                    self._capture_fail_streak += 1
                    if self.recording and self._capture_fail_streak in {1, 10, 30, 60}:
                        print(
                            f"[CAMERA] WARNING: capture.read() failed while recording "
                            f"(streak={self._capture_fail_streak}, source={self.source_description})",
                            flush=True,
                        )
                    time.sleep(0.005)
                    continue
                self._capture_fail_streak = 0

            with self.lock:
                if not self.recording:
                    continue
                self._record_frame_locked(frame, capture_ns)

    def _enqueue_analysis_frame(self, frame: Any, frame_index: int) -> None:
        streaming_manager = self.streaming_manager
        if not streaming_manager or not streaming_manager.is_active():
            return
        try:
            self._analysis_queue.put_nowait(("FRAME", streaming_manager, frame_index, frame))
        except queue.Full:
            if not self._analysis_drop_warned:
                print(
                    "[CAMERA] WARNING: analysis handoff queue is full; "
                    "dropping live-analysis frames to protect recording FPS",
                    flush=True,
                )
                self._analysis_drop_warned = True

    def _analysis_loop(self) -> None:
        while True:
            try:
                item = self._analysis_queue.get(timeout=0.1)
            except queue.Empty:
                if self._shutdown_event.is_set():
                    return
                continue

            kind = item[0]
            if kind == "STOP":
                self._analysis_queue.task_done()
                return

            _kind, streaming_manager, frame_index, frame = item
            try:
                if streaming_manager and streaming_manager.is_active():
                    streaming_manager.queue_frame(frame, frame_index)
            except Exception as exc:
                print(f"[CAMERA] WARNING: failed to queue frame for analysis: {exc}", flush=True)
            finally:
                self._analysis_queue.task_done()

    def _queue_writer_frame(self, frame: Any, frame_index: int) -> None:
        if not self.writer:
            return
        try:
            self._writer_queue.put(("FRAME", frame_index, frame), timeout=0.25)
        except queue.Full:
            if not self._writer_block_warned:
                print(
                    "[CAMERA] WARNING: writer queue is full; capture thread is waiting on video writer",
                    flush=True,
                )
                self._writer_block_warned = True
            self._writer_queue.put(("FRAME", frame_index, frame))

    def _writer_loop(self) -> None:
        while True:
            try:
                item = self._writer_queue.get(timeout=0.1)
            except queue.Empty:
                if self._shutdown_event.is_set():
                    return
                continue

            kind = item[0]
            if kind == "STOP":
                self._writer_queue.task_done()
                return

            _kind, frame_index, frame = item
            try:
                with self._writer_io_lock:
                    writer = self.writer
                    if writer is None:
                        continue
                    frame_to_write = frame
                    if self.overlay_frame_counter:
                        frame_to_write = frame.copy()
                        frame_label = f"Frame: {frame_index:06d}"
                        cv2.putText(
                            frame_to_write,
                            frame_label,
                            (20, 40),
                            cv2.FONT_HERSHEY_SIMPLEX,
                            1.0,
                            (0, 0, 0),
                            5,
                            cv2.LINE_AA,
                        )
                        cv2.putText(
                            frame_to_write,
                            frame_label,
                            (20, 40),
                            cv2.FONT_HERSHEY_SIMPLEX,
                            1.0,
                            (255, 255, 255),
                            2,
                            cv2.LINE_AA,
                        )
                    writer.write(frame_to_write)
            finally:
                self._writer_queue.task_done()

    def _drain_analysis_queue(self, timeout: float = 2.0) -> None:
        deadline = time.perf_counter() + max(0.0, timeout)
        while self._analysis_queue.unfinished_tasks > 0 and time.perf_counter() < deadline:
            time.sleep(0.005)

    def _drain_writer_queue(self, timeout: float = 3.0) -> None:
        deadline = time.perf_counter() + max(0.0, timeout)
        while self._writer_queue.unfinished_tasks > 0 and time.perf_counter() < deadline:
            time.sleep(0.005)

    def _record_frame_locked(self, frame: Any, capture_ns: int) -> None:
        if frame.shape[1] != self.width or frame.shape[0] != self.height:
            try:
                frame = cv2.resize(frame, (self.width, self.height))
            except Exception:
                return

        frame_index = self.frame_counter

        self._queue_writer_frame(frame, frame_index)

        self._enqueue_analysis_frame(frame, frame_index)

        self.recorded_frame_timestamps_ns.append(capture_ns)
        self.frame_counter += 1
        if frame_index == 0:
            print(
                f"[CAMERA] First frame recorded from {self.source_description} "
                f"at {frame.shape[1]}x{frame.shape[0]}",
                flush=True,
            )

    def shutdown(self) -> None:
        if self.recording or self.writer is not None or self.streaming_manager is not None:
            self.stop()
        self._shutdown_event.set()
        if self._capture_thread.is_alive():
            self._capture_thread.join(timeout=1.0)
        try:
            self._analysis_queue.put_nowait(("STOP", None))
        except queue.Full:
            pass
        if self._analysis_thread.is_alive():
            self._analysis_thread.join(timeout=1.0)
        try:
            self._writer_queue.put_nowait(("STOP", None))
        except queue.Full:
            pass
        if self._writer_thread.is_alive():
            self._writer_thread.join(timeout=1.0)
        if self.capture and self.capture.isOpened():
            self.capture.release()


def write_signal_line(line: str) -> None:
    """Write a line to the active signal file, if one is open."""
    if signal_file_handle:
        signal_file_handle.write(line + '\n')
        signal_file_handle.flush()
    if CONSOLE_DETAIL_LOGS:
        print(f"[SIGNAL] {line}", flush=True)


def event_time_to_frame_index(timestamp_us: int, frame_timestamps_ns: Optional[list[int]] = None) -> Optional[int]:
    """Map an Arduino phrase-relative timestamp to the nearest recorded video frame."""
    if timestamp_us < 0:
        return None

    start_host_ns = _current_phrase_start_host_ns()
    if start_host_ns is None:
        return None

    if frame_timestamps_ns is None:
        if not camera_recorder:
            return None
        frame_timestamps_ns = camera_recorder.get_recorded_frame_timestamps_ns()
    if not frame_timestamps_ns:
        return None

    event_host_ns = start_host_ns + (timestamp_us * 1_000)
    insertion_idx = bisect_left(frame_timestamps_ns, event_host_ns)
    if insertion_idx <= 0:
        return 0
    if insertion_idx >= len(frame_timestamps_ns):
        return len(frame_timestamps_ns) - 1

    prev_idx = insertion_idx - 1
    prev_delta = abs(event_host_ns - frame_timestamps_ns[prev_idx])
    next_delta = abs(frame_timestamps_ns[insertion_idx] - event_host_ns)
    if prev_delta <= next_delta:
        return prev_idx
    return insertion_idx


def format_timed_event_line(
    timestamp_seconds: float,
    message_text: str,
    *,
    timestamp_us: Optional[int] = None,
    frame_index: Optional[int] = None,
) -> str:
    """Format a log line with a human-readable timestamp and, when available, a direct frame mapping."""
    if frame_index is None and timestamp_us is not None:
        frame_index = event_time_to_frame_index(timestamp_us)
    if frame_index is None:
        return f"{timestamp_seconds:6.3f}s | {message_text}"
    return f"{timestamp_seconds:6.3f}s | frame {frame_index:06d} | {message_text}"


_TIMED_LINE_PATTERN = re.compile(
    r"^\s*(?P<ts>\d+\.\d{3})s\s*\|\s*(?:frame\s+\d+\s*\|\s*)?(?P<msg>.*)$"
)


def write_timed_signal_entry(timestamp_seconds: float, timestamp_us: int, message_text: str) -> str:
    """Track and write a timed phrase event using the active mapping metadata."""
    phrase_timed_log_entries.append(
        TimedLogEntry(
            timestamp_seconds=timestamp_seconds,
            timestamp_us=timestamp_us,
            message=message_text,
        )
    )
    formatted_line = format_timed_event_line(
        timestamp_seconds,
        message_text,
        timestamp_us=timestamp_us,
    )
    write_signal_line(formatted_line)
    return formatted_line


def recalculate_phrase_log_frames(
    phrase_dir: Path,
    base_name: str,
    timed_entries: list[TimedLogEntry],
    frame_timestamps_ns: list[int],
) -> None:
    """Rewrite timestamped log lines using direct nearest-frame timestamp lookup."""
    if not timed_entries or not frame_timestamps_ns:
        return

    log_path = phrase_dir / f"{base_name}.txt"
    try:
        original_lines = log_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return

    rewritten: list[str] = []
    timed_entry_idx = 0
    for idx, line in enumerate(original_lines):
        match = _TIMED_LINE_PATTERN.match(line)
        if not match:
            rewritten.append(line)
            continue

        if timed_entry_idx >= len(timed_entries):
            rewritten.append(line)
            continue

        entry = timed_entries[timed_entry_idx]
        timed_entry_idx += 1
        frame_index = event_time_to_frame_index(entry.timestamp_us, frame_timestamps_ns)
        rewritten.append(
            format_timed_event_line(
                entry.timestamp_seconds,
                entry.message,
                timestamp_us=entry.timestamp_us,
                frame_index=frame_index,
            )
        )

    try:
        log_path.write_text("\n".join(rewritten) + "\n", encoding="utf-8")
    except OSError:
        return


def prepare_phrase_artifacts(phrase_number: int) -> None:
    """Create the directory, log file, and start the camera for a forthcoming phrase."""
    global signal_file_handle, current_phrase_folder, current_base_name
    global current_phrase_number, pending_phrase_number, camera_status_message, camera_available
    global streaming_session_manager

    if signal_file_handle:
        return  # Already prepared

    timestamp = time.strftime('%Y%m%d_%H%M%S')
    base_name = f"{timestamp}_phrase{phrase_number:02d}"
    phrase_dir = (RECORDINGS_DIR / base_name).resolve()
    phrase_dir.mkdir(parents=True, exist_ok=True)

    signal_path = phrase_dir / f"{base_name}.txt"
    handle = open(signal_path, 'w', buffering=1, encoding='utf-8')

    write_time = time.strftime('%Y-%m-%d %H:%M:%S')
    handle.write(f"Phrase {phrase_number} initialized at {write_time}\n")
    handle.flush()

    current_phrase_folder = phrase_dir
    current_base_name = base_name
    current_phrase_number = phrase_number
    pending_phrase_number = phrase_number
    winner_recorded = False
    globals()['awaiting_winner'] = False

    globals()['signal_file_handle'] = handle
    clear_winner_indicator()
    globals()['awaiting_manual_winner'] = False
    globals()['phrase_requires_remote_review'] = False
    globals()['declared_winner_side'] = None
    globals()['manual_winner_side'] = None
    globals()['last_phrase_right_hit'] = False
    globals()['last_phrase_left_hit'] = False
    reset_logged_hit_sides()
    reset_phrase_timing_state()

    if camera_available and camera_recorder:
        try:
            # Start streaming session if enabled
            streaming_mgr = None
            if REFEREE_USE_LOCAL_STREAMING_ANALYZER and send_to_server_mode:
                print(f"[LOCAL_ANALYZER] Attempting to start local analyzer for phrase {phrase_number}")
                try:
                    local_output_dir = (
                        REFEREE_LOCAL_ANALYZER_OUTPUT_ROOT / base_name
                        if REFEREE_LOCAL_ANALYZER_OUTPUT_ROOT is not None
                        else None
                    )
                    streaming_mgr = LocalStreamingSessionManager(
                        phrase_dir=phrase_dir,
                        base_name=base_name,
                        bundle_root=REFEREE_LOCAL_ANALYZER_ROOT,
                        python_executable=REFEREE_LOCAL_ANALYZER_PYTHON,
                        output_dir=local_output_dir,
                        model_path=REFEREE_LOCAL_ANALYZER_MODEL_PATH,
                        fisheye_backend=REFEREE_LOCAL_ANALYZER_FISHEYE_BACKEND,
                        yolo_conf=REFEREE_LOCAL_ANALYZER_YOLO_CONF,
                        yolo_imgsz=REFEREE_LOCAL_ANALYZER_YOLO_IMGSZ,
                        yolo_half=REFEREE_LOCAL_ANALYZER_YOLO_HALF,
                        yolo_verbose=REFEREE_LOCAL_ANALYZER_YOLO_VERBOSE,
                        bootstrap_frames=REFEREE_LOCAL_ANALYZER_BOOTSTRAP_FRAMES,
                        queue_max=REFEREE_LOCAL_ANALYZER_QUEUE_MAX,
                        jpeg_quality=REFEREE_STREAMING_JPEG_QUALITY,
                        startup_timeout=REFEREE_LOCAL_ANALYZER_STARTUP_TIMEOUT,
                        result_timeout=REFEREE_REQUEST_TIMEOUT,
                    )
                    session_id = f"{base_name}"
                    if streaming_mgr.start_session(
                        session_id=session_id,
                        fps=camera_recorder.fps,
                        width=camera_recorder.width,
                        height=camera_recorder.height,
                        expected_frames=0,
                    ):
                        streaming_session_manager = streaming_mgr
                        write_signal_line("Local analyzer session started")
                        camera_status_message = "Recording + Local analysis"
                        print(f"[LOCAL_ANALYZER] Local analyzer initialized successfully")
                    else:
                        streaming_mgr = None
                        camera_status_message = "Recording (local analyzer failed to start)"
                        print(f"[LOCAL_ANALYZER] Failed to start session")
                except Exception as exc:
                    print(f"[LOCAL_ANALYZER] Exception starting local analyzer: {exc}")
                    import traceback
                    traceback.print_exc()
                    write_signal_line(f"Failed to start local analyzer: {exc}")
                    streaming_mgr = None
                    camera_status_message = "Recording"
            elif (
                REFEREE_USE_STREAMING
                and STREAMING_AVAILABLE
                and REFEREE_SERVER_URL
                and send_to_server_mode
            ):
                print(f"[STREAMING] Attempting to start streaming for phrase {phrase_number}")
                print(f"[STREAMING] Server URL: {REFEREE_SERVER_URL}")
                try:
                    streaming_mgr = StreamingSessionManager(REFEREE_SERVER_URL)
                    session_id = f"{base_name}"
                    if streaming_mgr.start_session(
                        session_id=session_id,
                        fps=camera_recorder.fps,
                        width=camera_recorder.width,
                        height=camera_recorder.height,
                        expected_frames=0,  # Unknown duration
                    ):
                        streaming_session_manager = streaming_mgr
                        write_signal_line("Streaming session started")
                        camera_status_message = "Recording + Streaming"
                        print(f"[STREAMING] Streaming session initialized successfully")
                    else:
                        streaming_mgr = None
                        camera_status_message = "Recording (streaming failed to start)"
                        print(f"[STREAMING] Failed to start session (returned False)")
                except Exception as exc:
                    print(f"[STREAMING] Exception starting streaming: {exc}")
                    import traceback
                    traceback.print_exc()
                    write_signal_line(f"Failed to start streaming: {exc}")
                    streaming_mgr = None
                    camera_status_message = "Recording"
            else:
                if REFEREE_USE_LOCAL_STREAMING_ANALYZER and not send_to_server_mode:
                    print(f"[LOCAL_ANALYZER] Local analyzer disabled because server mode is off")
                elif not REFEREE_USE_STREAMING and not REFEREE_USE_LOCAL_STREAMING_ANALYZER:
                    print(f"[STREAMING] Streaming disabled (REFEREE_USE_STREAMING=false)")
                elif not STREAMING_AVAILABLE:
                    print(f"[STREAMING] Streaming unavailable (websockets not installed)")
                elif not REFEREE_SERVER_URL:
                    print(f"[STREAMING] No server URL configured")

            camera_recorder.start(str(phrase_dir / f"{base_name}.avi"), streaming_manager=streaming_mgr)

            if not streaming_mgr:
                camera_status_message = "Recording"

        except Exception as exc:
            camera_status_message = f"Camera error: {exc}"
            camera_available = False
            write_signal_line(camera_status_message)
    else:
        camera_status_message = "Camera unavailable"


def stop_phrase_recording(discard_artifacts: bool = False) -> None:
    """Stop camera capture and close the current signal file."""
    global signal_file_handle, current_phrase_folder, current_base_name
    global current_phrase_number, pending_phrase_number, last_phrase_folder, last_base_name
    global camera_status_message, camera_available, streaming_session_manager
    global declared_winner_side, manual_winner_side

    if signal_file_handle is None and current_phrase_folder is None and current_base_name is None:
        return

    phrase_dir_to_remove: Optional[Path] = current_phrase_folder if discard_artifacts else None
    total_frames = 0
    frame_timestamps_ns: list[int] = []
    if camera_available and camera_recorder:
        total_frames = camera_recorder.stop()
        frame_timestamps_ns = camera_recorder.get_recorded_frame_timestamps_ns()
        camera_status_message = "Ready"
        if total_frames <= 0 and not discard_artifacts:
            write_signal_line(
                "Camera captured 0 frames; recorded AVI contains no images. "
                "Check camera device selection and whether the camera is streaming."
            )
        elif camera_recorder.last_recorded_fps and not discard_artifacts:
            write_signal_line(
                f"Camera captured {total_frames} frames @ "
                f"{camera_recorder.last_recorded_fps:.2f} FPS (target {camera_recorder.target_fps:.2f})"
            )
    else:
        if not camera_available and "error" not in camera_status_message.lower():
            camera_status_message = "Camera unavailable"

    if (
        not discard_artifacts
        and current_phrase_folder
        and current_base_name
        and phrase_timed_log_entries
        and frame_timestamps_ns
    ):
        recalculate_phrase_log_frames(
            current_phrase_folder,
            current_base_name,
            list(phrase_timed_log_entries),
            frame_timestamps_ns,
        )
        if signal_file_handle:
            signal_file_handle.seek(0, os.SEEK_END)

    # End streaming session if active
    if streaming_session_manager and streaming_session_manager.is_active():
        if discard_artifacts:
            streaming_session_manager.cancel_session("phrase_discarded")
            streaming_session_manager = None
            print("[STREAMING] Session cancelled; no data uploaded")
        else:
            print(f"[STREAMING] Ending session (total_frames={total_frames})")
            if current_phrase_folder and current_base_name and signal_file_handle:
                # Flush and read signal data
                signal_file_handle.flush()
                signal_path = current_phrase_folder / f"{current_base_name}.txt"
                signal_data = signal_path.read_bytes()
                signal_filename = signal_path.name

                # End the session with signal data
                print(f"[STREAMING] Sending end_session to background thread")
                streaming_session_manager.end_session(signal_data, signal_filename, total_frames)
                write_signal_line(f"Streaming session ended ({total_frames} frames sent)")
                print(f"[STREAMING] end_session queued, background thread will handle it")

    if signal_file_handle:
        signal_file_handle.flush()
        signal_file_handle.close()

    if current_phrase_folder and not discard_artifacts:
        last_phrase_folder = current_phrase_folder
        last_base_name = current_base_name
    elif discard_artifacts:
        last_phrase_folder = None
        last_base_name = None

    current_phrase_folder = None
    current_base_name = None
    current_phrase_number = None
    pending_phrase_number = None
    globals()['signal_file_handle'] = None
    globals()['winner_recorded'] = False
    globals()['awaiting_winner'] = False
    declared_winner_side = None
    manual_winner_side = None
    reset_logged_hit_sides()
    reset_phrase_timing_state()

    if discard_artifacts and phrase_dir_to_remove and phrase_dir_to_remove.exists():
        shutil.rmtree(phrase_dir_to_remove, ignore_errors=True)


class FencingApp:
    """Simple Tkinter UI that mirrors a fencing scoring box."""

    def __init__(self, root: tk.Tk, serial_connection: serial.Serial) -> None:
        self.root = root
        self.serial = serial_connection

        root.title("AI Fencing Scoring Box")
        root.geometry("1100x900")
        root.minsize(980, 820)
        root.resizable(True, True)

        style = ttk.Style(root)
        style.configure("Start.TButton", font=("Helvetica", 20, "bold"), padding=18)

        main_frame = ttk.Frame(root, padding=35)
        main_frame.grid(row=0, column=0, sticky="nsew")
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)
        for col in range(2):
            main_frame.columnconfigure(col, weight=1)
        main_frame.rowconfigure(1, weight=1)

        title = ttk.Label(main_frame, text="Fencing Scoring System", font=("Helvetica", 26, "bold"))
        title.grid(row=0, column=0, columnspan=2, pady=(0, 30))

        self.canvas_size = 520
        indicator_margin = 36
        indicator_outline = 6
        winner_canvas_width = 220
        winner_canvas_height = 110
        winner_indicator_margin = 26

        self.fencer1_canvas = tk.Canvas(
            main_frame,
            width=self.canvas_size,
            height=self.canvas_size,
            highlightthickness=0
        )
        self.fencer1_canvas.grid(row=1, column=0, padx=40, pady=(0, 12), sticky="n")
        self.fencer1_indicator = self.fencer1_canvas.create_oval(
            indicator_margin,
            indicator_margin,
            self.canvas_size - indicator_margin,
            self.canvas_size - indicator_margin,
            fill="dim gray",
            outline="#202020",
            width=indicator_outline
        )
        self.fencer1_winner_canvas = tk.Canvas(
            main_frame,
            width=winner_canvas_width,
            height=winner_canvas_height,
            highlightthickness=0
        )
        self.fencer1_winner_canvas.grid(row=2, column=0, padx=40, pady=(0, 8), sticky="n")
        self.fencer1_winner_indicator = self.fencer1_winner_canvas.create_oval(
            winner_indicator_margin,
            winner_indicator_margin,
            winner_canvas_width - winner_indicator_margin,
            winner_canvas_height - winner_indicator_margin,
            fill=WINNER_BLUE_INACTIVE,
            outline="#202020",
            width=indicator_outline
        )
        ttk.Label(main_frame, text="Right Fencer (Green Lamp)", font=("Helvetica", 22)).grid(row=3, column=0, pady=(8, 32))

        self.fencer2_canvas = tk.Canvas(
            main_frame,
            width=self.canvas_size,
            height=self.canvas_size,
            highlightthickness=0
        )
        self.fencer2_canvas.grid(row=1, column=1, padx=40, pady=(0, 12), sticky="n")
        self.fencer2_indicator = self.fencer2_canvas.create_oval(
            indicator_margin,
            indicator_margin,
            self.canvas_size - indicator_margin,
            self.canvas_size - indicator_margin,
            fill="dim gray",
            outline="#202020",
            width=indicator_outline
        )
        self.fencer2_winner_canvas = tk.Canvas(
            main_frame,
            width=winner_canvas_width,
            height=winner_canvas_height,
            highlightthickness=0
        )
        self.fencer2_winner_canvas.grid(row=2, column=1, padx=40, pady=(0, 8), sticky="n")
        self.fencer2_winner_indicator = self.fencer2_winner_canvas.create_oval(
            winner_indicator_margin,
            winner_indicator_margin,
            winner_canvas_width - winner_indicator_margin,
            winner_canvas_height - winner_indicator_margin,
            fill=WINNER_BLUE_INACTIVE,
            outline="#202020",
            width=indicator_outline
        )
        ttk.Label(main_frame, text="Left Fencer (Red Lamp)", font=("Helvetica", 22)).grid(row=3, column=1, pady=(8, 32))

        controls_frame = ttk.Frame(main_frame)
        controls_frame.grid(row=4, column=0, columnspan=2, pady=(10, 20), sticky="ew")
        controls_frame.columnconfigure(0, weight=1)
        controls_frame.columnconfigure(1, weight=1)

        self.start_button = ttk.Button(controls_frame, text="Start Bout", style="Start.TButton", command=self.start_phrase)
        self.start_button.grid(row=0, column=0, padx=(0, 10), ipadx=30, ipady=12, sticky="ew")
        self.cancel_button = ttk.Button(
            controls_frame,
            text="Cancel Bout",
            command=self.cancel_phrase,
        )
        self.cancel_button.grid(row=0, column=1, padx=(10, 0), ipadx=30, ipady=12, sticky="ew")

        mode_frame = ttk.LabelFrame(main_frame, text="Result Handling")
        mode_frame.grid(row=5, column=0, columnspan=2, pady=(0, 15), sticky="ew")
        mode_frame.columnconfigure(0, weight=1)
        mode_frame.columnconfigure(1, weight=1)

        self.review_mode_var = tk.StringVar(value="server" if send_to_server_mode else "local")
        ttk.Radiobutton(
            mode_frame,
            text="Send to server",
            value="server",
            variable=self.review_mode_var,
            command=self.on_review_mode_change,
        ).grid(row=0, column=0, padx=10, pady=6, sticky="w")
        ttk.Radiobutton(
            mode_frame,
            text="Local only",
            value="local",
            variable=self.review_mode_var,
            command=self.on_review_mode_change,
        ).grid(row=0, column=1, padx=10, pady=6, sticky="w")
        self.mode_status_label = ttk.Label(
            mode_frame,
            text=f"Current mode: {send_mode_description()}",
            font=("Helvetica", 11, "italic"),
        )
        self.mode_status_label.grid(row=1, column=0, columnspan=2, pady=(2, 4))

        winner_frame = ttk.Frame(main_frame)
        winner_frame.grid(row=6, column=0, columnspan=2, pady=(0, 15))
        ttk.Label(winner_frame, text="Select Winner", font=("Helvetica", 14, "bold")).grid(row=0, column=0, columnspan=2, pady=(0, 8))

        self.right_winner_button = ttk.Button(
            winner_frame,
            text="Right Fencer Wins (Green)",
            command=lambda: self.record_winner("right")
        )
        self.right_winner_button.grid(row=1, column=0, padx=10, ipadx=10, ipady=4)

        self.left_winner_button = ttk.Button(
            winner_frame,
            text="Left Fencer Wins (Red)",
            command=lambda: self.record_winner("left")
        )
        self.left_winner_button.grid(row=1, column=1, padx=10, ipadx=10, ipady=4)

        self.state_label = ttk.Label(main_frame, text="State: INITIALIZING", font=("Helvetica", 16))
        self.state_label.grid(row=7, column=0, columnspan=2, pady=(15, 5))

        self.record_status_label = ttk.Label(main_frame, text="Recording: --", font=("Helvetica", 14))
        self.record_status_label.grid(row=8, column=0, columnspan=2, pady=(0, 5))

        self.camera_status_label = ttk.Label(main_frame, text=f"Camera: {camera_status_message}", font=("Helvetica", 14))
        self.camera_status_label.grid(row=9, column=0, columnspan=2, pady=(8, 12))

        self.last_event_label = ttk.Label(main_frame, text="Last Event: ---", wraplength=460, justify="center", font=("Helvetica", 13))
        self.last_event_label.grid(row=10, column=0, columnspan=2, pady=(5, 18))

        ttk.Label(
            main_frame,
            text="Hits light the lamps (with beep) at any time for testing.\nPress Start to begin recording a bout.",
            justify="center",
            font=("Helvetica", 13)
        ).grid(row=11, column=0, columnspan=2)

    def start_phrase(self) -> None:
        """Play the cue, then begin a new phrase recording."""
        global system_state, phrase_active, pending_phrase_number
        if not self.serial or not self.serial.is_open:
            return
        clear_winner_indicator()
        globals()['declared_winner_side'] = None
        globals()['manual_winner_side'] = None
        globals()['phrase_requires_remote_review'] = False
        globals()['awaiting_manual_winner'] = False
        globals()['last_phrase_right_hit'] = False
        globals()['last_phrase_left_hit'] = False
        state = system_state.strip()
        if state not in {"WAITING_FOR_COMMAND", "DISPLAYING_RESULTS"}:
            return

        next_phrase_number = phrase_counter + 1
        phrase_active = True
        pending_phrase_number = next_phrase_number
        self.start_button.config(state=tk.DISABLED)

        threading.Thread(
            target=self._run_start_sequence,
            args=(next_phrase_number,),
            daemon=True,
        ).start()

    def cancel_phrase(self) -> None:
        """Cancel the in-progress phrase and discard its artifacts."""
        global phrase_active, pending_phrase_number, phrase_requires_remote_review
        global awaiting_remote_result, awaiting_manual_winner, winner_recorded
        global last_log_line

        if not current_phrase_folder and not signal_file_handle:
            update_last_event("No active phrase to cancel.")
            return

        update_last_event("Cancelling current phrase...")
        try:
            if self.serial and self.serial.is_open:
                self.serial.write(b'c')
        except serial.SerialException as exc:
            print(f"Failed to send cancel command: {exc}")

        stop_phrase_recording(discard_artifacts=True)
        phrase_active = False
        pending_phrase_number = None
        winner_recorded = False
        awaiting_manual_winner = False
        awaiting_remote_result = False
        phrase_requires_remote_review = False
        globals()['system_state'] = "WAITING_FOR_COMMAND"
        clear_winner_indicator()
        beep_controller.reset()
        last_log_line = "Phrase cancelled. Ready for next phrase."
        update_last_event("Phrase cancelled. Ready for next phrase.")

    def on_review_mode_change(self) -> None:
        """Handle user toggling between remote upload and local-only modes."""
        selected = self.review_mode_var.get()
        set_send_to_server_mode(selected == "server")
        self.mode_status_label.config(text=f"Current mode: {send_mode_description()}")

    def record_winner(self, side: str) -> None:
        global winner_recorded, awaiting_winner, awaiting_manual_winner
        global manual_winner_side
        if side not in {"right", "left"}:
            return
        if awaiting_remote_result:
            return
        if not awaiting_manual_winner:
            return
        if system_state.strip() != "DISPLAYING_RESULTS" or phrase_active or not last_phrase_folder:
            return

        manual_winner_side = side
        awaiting_manual_winner = False
        awaiting_winner = phrase_requires_remote_review
        self.right_winner_button.config(state=tk.DISABLED)
        self.left_winner_button.config(state=tk.DISABLED)

        label = winner_side_label(side)
        print(f"Winner button selected: {label}")

        if phrase_requires_remote_review:
            winner_recorded = False
            log_winner_selection(side, "Manual selection")
            update_last_event(f"Manual selection: {label}. Sending to remote referee...")
            start_remote_referee_pipeline()
        else:
            winner_recorded = True
            log_winner_selection(side, "Confirmed result")
            set_winner_indicator(side)
            update_last_event(f"Winner confirmed: {label}")
            globals()['camera_status_message'] = "Ready"
            if send_to_server_mode:
                broadcast_winner_reason(side, single_hit_broadcast_message(side))

    def _run_start_sequence(self, phrase_number: int) -> None:
        cue_start, cue_end = play_start_audio()
        prepare_phrase_artifacts(phrase_number)
        start_str = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(cue_start))
        write_signal_line(f"Audio cue executed at {start_str}")
        write_signal_line(f"Audio cue duration: {cue_end - cue_start:.2f}s")
        write_signal_line(f"Start command issued at {time.strftime('%Y-%m-%d %H:%M:%S')}")

        global phrase_active, pending_phrase_number, phrase_start_command_host_ns
        try:
            phrase_start_command_host_ns = time.perf_counter_ns()
            print(f"[PHRASE] Sending start command for phrase {phrase_number}", flush=True)
            self.serial.write(b's')
        except serial.SerialException:
            write_signal_line("Failed to send start command to Arduino.")
            phrase_active = False
            pending_phrase_number = None
            phrase_start_command_host_ns = None
        finally:
            self.root.after(0, self.refresh)

    def refresh(self) -> None:
        """Refresh UI components based on the latest global state."""
        global _last_console_camera_status, _last_console_record_status
        global _last_console_mode_status, _last_console_ui_state

        desired_mode = "server" if send_to_server_mode else "local"
        if self.review_mode_var.get() != desired_mode:
            self.review_mode_var.set(desired_mode)
        mode_text = f"Current mode: {send_mode_description()}"
        self.mode_status_label.config(text=mode_text)
        if CONSOLE_DETAIL_LOGS and mode_text != _last_console_mode_status:
            mirror_console_status("MODE_STATUS", mode_text)
            _last_console_mode_status = mode_text

        active_color1 = "lime green" if fencer1_led else "dim gray"
        active_color2 = "red" if fencer2_led else "dim gray"
        self.fencer1_canvas.itemconfig(self.fencer1_indicator, fill=active_color1)
        self.fencer2_canvas.itemconfig(self.fencer2_indicator, fill=active_color2)

        state_text = f"State: {system_state.strip()}"
        self.state_label.config(text=state_text)
        if CONSOLE_DETAIL_LOGS and state_text != _last_console_ui_state:
            mirror_console_status("UI_STATE", state_text)
            _last_console_ui_state = state_text

        if current_base_name:
            record_text = f"Active recording: {current_base_name}"
        elif last_base_name:
            record_text = f"Last recording: {last_base_name}"
        else:
            record_text = "Recording: --"
        self.record_status_label.config(text=record_text)
        if CONSOLE_DETAIL_LOGS and record_text != _last_console_record_status:
            mirror_console_status("RECORD_STATUS", record_text)
            _last_console_record_status = record_text

        camera_text = f"Camera: {camera_status_message}"
        self.camera_status_label.config(text=camera_text)
        if CONSOLE_DETAIL_LOGS and camera_text != _last_console_camera_status:
            mirror_console_status("CAMERA_STATUS", camera_text)
            _last_console_camera_status = camera_text

        event_text = last_log_line if last_log_line else "---"
        self.last_event_label.config(text=f"Last Event: {event_text}")

        can_start = (
            system_state.strip() in {"WAITING_FOR_COMMAND", "DISPLAYING_RESULTS"}
            and not phrase_active
            and not awaiting_winner
            and not awaiting_remote_result
        )
        self.start_button.config(state=tk.NORMAL if can_start else tk.DISABLED)
        cancel_enabled = current_phrase_folder is not None
        self.cancel_button.config(state=tk.NORMAL if cancel_enabled else tk.DISABLED)

        buttons_enabled = (
            system_state.strip() == "DISPLAYING_RESULTS"
            and not phrase_active
            and awaiting_manual_winner
            and not awaiting_remote_result
        )
        state_for_winner = tk.NORMAL if buttons_enabled else tk.DISABLED
        self.right_winner_button.config(state=state_for_winner)
        self.left_winner_button.config(state=state_for_winner)
        self.update_winner_indicators()

    def update_winner_indicators(self) -> None:
        right_color = WINNER_BLUE_ACTIVE if winner_right_indicator_active else WINNER_BLUE_INACTIVE
        left_color = WINNER_BLUE_ACTIVE if winner_left_indicator_active else WINNER_BLUE_INACTIVE
        self.fencer1_winner_canvas.itemconfig(self.fencer1_winner_indicator, fill=right_color)
        self.fencer2_winner_canvas.itemconfig(self.fencer2_winner_indicator, fill=left_color)

    def on_close(self) -> None:
        """Handle application shutdown."""
        try:
            stop_phrase_recording()
            if camera_recorder:
                camera_recorder.shutdown()
            if self.serial and self.serial.is_open:
                self.serial.close()
            beep_controller.reset()
        except serial.SerialException:
            pass
        self.root.destroy()


def parse_arduino_message(line: str, received_host_ns: Optional[int] = None) -> None:
    """Parse messages from Arduino and update global state/UI."""
    global system_state, fencer1_led, fencer2_led, last_log_line
    global phrase_counter, phrase_active, camera_status_message, pending_phrase_number
    global phrase_start_arduino_ms, phrase_start_host_ns, phrase_end_timestamp_us

    stripped = line.strip()
    if not stripped:
        return
    if stripped.startswith("===") or stripped.startswith("Baselines:") or stripped.startswith("[EVENT]"):
        return

    try:
        prefix, content = stripped.split(':', 1)
    except ValueError:
        print(f"Unformatted data from Arduino: {stripped}")
        return

    prefix = prefix.strip()
    content = content.strip()

    if prefix == "TIME_MS":
        # Handled during startup sync. Ignore during normal polling.
        pass
    elif prefix == "PHRASE_START_MS":
        try:
            phrase_start_arduino_ms = int(content)
        except ValueError:
            phrase_start_arduino_ms = None
        if phrase_start_arduino_ms is not None and arduino_clock_offset_ns is not None:
            phrase_start_host_ns = arduino_clock_offset_ns + (phrase_start_arduino_ms * 1_000_000)
        elif phrase_start_command_host_ns is not None:
            phrase_start_host_ns = phrase_start_command_host_ns
        else:
            phrase_start_host_ns = received_host_ns
    elif prefix == "STATE":
        system_state = content
        print(f"[STATE] Arduino -> {content}", flush=True)
        if content == "RECORDING":
            phrase_counter += 1
            pending_phrase_number = None
            if not current_phrase_number:
                prepare_phrase_artifacts(phrase_counter)
            write_signal_line(f"Recording started at {time.strftime('%Y-%m-%d %H:%M:%S')}")
            if arduino_clock_sync_rtt_ns is not None:
                write_signal_line(
                    "Frame timing: direct timestamp mapping enabled "
                    f"(clock sync RTT {arduino_clock_sync_rtt_ns / 1_000_000:.1f} ms)"
                )
            else:
                write_signal_line("Frame timing: direct timestamp mapping enabled (host command fallback)")
            phrase_active = True
            last_log_line = "Recording started"
            if camera_available:
                camera_status_message = "Recording"
            globals()['awaiting_winner'] = False
            reset_logged_hit_sides()
        elif content == "LOCKOUT_PERIOD":
            phrase_active = True
            write_signal_line("Lockout active (0.200s window)")
            last_log_line = "Lockout active (0.200s window)"
        elif content == "DISPLAYING_RESULTS":
            score_summary = f"Scores -> Fencer 1: {'HIT' if fencer1_led else 'MISS'}, Fencer 2: {'HIT' if fencer2_led else 'MISS'}"
            write_signal_line(score_summary)
            write_signal_line("Phrase ended")
            last_log_line = score_summary
            phrase_active = False
            print(f"[PHRASE] DISPLAYING_RESULTS received; stopping recording", flush=True)
            stop_phrase_recording()
            handle_phrase_results(score_summary)
            if awaiting_manual_winner:
                camera_status_message = "Awaiting manual winner"
        elif content == "WAITING_FOR_COMMAND":
            if phrase_active:
                write_signal_line("Phrase cancelled before completion")
            phrase_active = False
            last_log_line = "Ready for next phrase"
            print(f"[PHRASE] WAITING_FOR_COMMAND received; stopping recording", flush=True)
            stop_phrase_recording()
        else:
            last_log_line = content
    elif prefix == "SCORE":
        if content == "F1_ON":
            fencer1_led = True
            beep_controller.start("F1")
        elif content == "F2_ON":
            fencer2_led = True
            beep_controller.start("F2")
        elif content == "F1_OFF":
            fencer1_led = False
            beep_controller.stop("F1")
        elif content == "F2_OFF":
            fencer2_led = False
            beep_controller.stop("F2")
        elif content == "RESET":
            fencer1_led = False
            fencer2_led = False
            beep_controller.reset()
    elif prefix == "LOG":
        message_text = content
        timestamp_seconds = None
        timestamp_us = None

        if '|' in content:
            raw_timestamp, message_text = content.split('|', 1)
            raw_timestamp = raw_timestamp.strip()
            message_text = message_text.strip()
            if raw_timestamp.isdigit():
                try:
                    timestamp_us = int(raw_timestamp)
                    timestamp_seconds = timestamp_us / 1_000_000.0
                except ValueError:
                    timestamp_us = None
                    timestamp_seconds = None
            else:
                try:
                    timestamp_seconds = float(raw_timestamp)
                    timestamp_us = int(round(timestamp_seconds * 1_000_000))
                except ValueError:
                    timestamp_us = None
                    timestamp_seconds = None
        else:
            message_text = message_text.strip()

        if timestamp_seconds is not None and timestamp_us is not None:
            if message_text.strip().lower().startswith("phrase recording ended"):
                phrase_end_timestamp_us = timestamp_us
            formatted_line = write_timed_signal_entry(timestamp_seconds, timestamp_us, message_text)
        else:
            formatted_line = message_text
            write_signal_line(formatted_line)

        last_log_line = formatted_line
        process_hit_log_entry(message_text, timestamp_seconds, timestamp_us)
    elif prefix == "CONTACT":
        # Debug contact info ignored
        pass
    else:
        print(f"Unknown prefix from Arduino: {line}")
        return

    if app:
        app.refresh()


def drain_serial_startup_messages(
    serial_connection: serial.Serial,
    *,
    window_seconds: float = 0.25,
) -> None:
    """Consume any initial Arduino status lines before the Tk poll loop starts."""
    deadline = time.perf_counter() + max(0.0, window_seconds)
    while time.perf_counter() < deadline:
        saw_line = False
        try:
            while serial_connection.in_waiting > 0:
                raw_line = serial_connection.readline().decode('utf-8', errors='ignore').strip()
                if not raw_line:
                    continue
                saw_line = True
                parse_arduino_message(raw_line, received_host_ns=time.perf_counter_ns())
        except serial.SerialException:
            return

        if saw_line:
            continue
        time.sleep(0.01)


def schedule_serial_poll(root: tk.Tk) -> None:
    """Poll the serial port for new lines and reschedule the next poll."""
    if not ser or not ser.is_open:
        return

    try:
        while ser.in_waiting > 0:
            raw_line = ser.readline().decode('utf-8', errors='ignore').strip()
            if raw_line:
                parse_arduino_message(raw_line, received_host_ns=time.perf_counter_ns())
    except serial.SerialException as exc:
        print(f"\nSerial connection lost: {exc}")
        write_signal_line(f"Serial connection lost: {exc}")
        try:
            ser.close()
        except serial.SerialException:
            pass
        return

    root.after(CAMERA_POLL_INTERVAL_MS, lambda: schedule_serial_poll(root))


if __name__ == "__main__":
    try:
        ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=0.1)
        time.sleep(2)  # Allow Arduino to reset
    except serial.SerialException as err:
        print(f"\nError: Could not open serial port '{SERIAL_PORT}'.")
        print("Please check the port and ensure Arduino is connected.")
        print(f"Details: {err}")
        raise SystemExit(1)

    drain_serial_startup_messages(ser)
    arduino_clock_offset_ns, arduino_clock_sync_rtt_ns = sync_arduino_clock(ser)
    if arduino_clock_offset_ns is not None and arduino_clock_sync_rtt_ns is not None:
        print(
            "Arduino clock sync established "
            f"(best RTT {arduino_clock_sync_rtt_ns / 1_000_000:.1f} ms)"
        )
    else:
        print("Warning: Arduino clock sync unavailable; falling back to host start-command timing.")

    if system_state.strip() == "INITIALIZING":
        system_state = "WAITING_FOR_COMMAND"
        last_log_line = "Ready for next phrase"

    try:
        camera_recorder = CameraRecorder(CAMERA_INDEX)
        camera_available = True
        camera_status_message = "Ready"
    except Exception as camera_err:
        camera_recorder = None
        camera_available = False
        camera_status_message = f"Camera unavailable: {camera_err}"
        print(camera_status_message)
        print("If you're on macOS, grant camera access to Terminal/python in System Settings > Privacy & Security.")

    if camera_available:
        warm_local_analyzer_service()

    root = tk.Tk()
    app = FencingApp(root, ser)
    app.refresh()

    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.after(50, lambda: schedule_serial_poll(root))

    try:
        root.mainloop()
    finally:
        stop_phrase_recording()
        shutdown_shared_local_analyzer()
        if camera_recorder:
            camera_recorder.shutdown()
        if ser and ser.is_open:
            ser.close()
