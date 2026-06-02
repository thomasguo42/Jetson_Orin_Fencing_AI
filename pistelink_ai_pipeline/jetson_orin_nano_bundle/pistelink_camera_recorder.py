"""Camera wrapper for the PisteLink AI service.

This reuses the existing Jetson camera recorder so camera selection, GStreamer
settings, and the live analyzer frame path stay aligned with the current
low-latency pipeline.  The wrapper adds epoch-ms frame timestamps because the
PisteLink backend timestamps events in Unix time.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path
from typing import Iterable, List, Optional

import control_fencing

from pistelink_signal_adapter import FrameTimestamp, write_frame_timestamps


def default_camera_settings() -> tuple[float, int, int]:
    return (
        float(getattr(control_fencing, "CAMERA_TARGET_FPS", 30.0) or 30.0),
        int(getattr(control_fencing, "CAMERA_WIDTH", 1280) or 1280),
        int(getattr(control_fencing, "CAMERA_HEIGHT", 720) or 720),
    )


class PisteLinkCameraRecorder:
    def __init__(self, camera_index: Optional[int] = None):
        self._perf_anchor_ns = time.perf_counter_ns()
        self._epoch_anchor_ns = time.time_ns()
        selected_index = control_fencing.CAMERA_INDEX if camera_index is None else camera_index
        self._recorder = control_fencing.CameraRecorder(selected_index)

    @property
    def width(self) -> int:
        return int(getattr(self._recorder, "width", 0) or getattr(self._recorder, "camera_width", 0) or 0)

    @property
    def height(self) -> int:
        return int(getattr(self._recorder, "height", 0) or getattr(self._recorder, "camera_height", 0) or 0)

    @property
    def fps(self) -> float:
        return float(getattr(self._recorder, "fps", 0) or getattr(self._recorder, "actual_fps", 30.0) or 30.0)

    @property
    def frame_count(self) -> int:
        return int(getattr(self._recorder, "frame_counter", 0) or 0)

    def start(self, output_avi_path: Path, streaming_manager: object) -> bool:
        output_avi_path.parent.mkdir(parents=True, exist_ok=True)
        self._recorder.start(str(output_avi_path), streaming_manager=streaming_manager)
        return True

    def stop(self) -> None:
        self._recorder.stop()

    def release(self) -> None:
        self._recorder.shutdown()

    def wait_for_first_frame(self, timeout_s: float = 2.0) -> Optional[FrameTimestamp]:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            frames = self.frame_timestamps()
            if frames:
                return frames[0]
            time.sleep(0.02)
        return None

    def frame_timestamps(self) -> List[FrameTimestamp]:
        perf_times: Iterable[int] = self._recorder.get_recorded_frame_timestamps_ns()
        frames: List[FrameTimestamp] = []
        for index, perf_ns in enumerate(perf_times):
            epoch_ns = self._epoch_anchor_ns + (int(perf_ns) - self._perf_anchor_ns)
            frames.append(FrameTimestamp(frame=index, ts=epoch_ns // 1_000_000, mono_ns=int(perf_ns)))
        return frames

    def write_frame_timestamps(self, path: Path) -> List[FrameTimestamp]:
        frames = self.frame_timestamps()
        write_frame_timestamps(path, frames)
        return frames


def transcode_avi_to_mp4(input_avi: Path, output_mp4: Path, timeout_s: float = 120.0) -> Path:
    """Convert the current MJPG/AVI recording into the MP4 required by PisteLink."""

    if input_avi.suffix.lower() == ".mp4":
        return input_avi

    output_mp4.parent.mkdir(parents=True, exist_ok=True)
    errors: List[str] = []

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is not None:
        try:
            return _transcode_with_ffmpeg(ffmpeg, input_avi, output_mp4, timeout_s)
        except Exception as exc:
            errors.append(str(exc))

    gst_launch = shutil.which("gst-launch-1.0")
    if gst_launch is not None:
        try:
            return _transcode_with_gstreamer(gst_launch, input_avi, output_mp4, timeout_s)
        except Exception as exc:
            errors.append(str(exc))

    detail = "; ".join(errors) if errors else "neither ffmpeg nor gst-launch-1.0 is available"
    raise RuntimeError(f"failed to produce PisteLink MP4 output: {detail}")


def _transcode_with_ffmpeg(ffmpeg: str, input_avi: Path, output_mp4: Path, timeout_s: float) -> Path:
    cmd = [
        ffmpeg,
        "-y",
        "-i",
        str(input_avi),
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "28",
        "-movflags",
        "+faststart",
        "-an",
        str(output_mp4),
    ]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout_s)
    if result.returncode != 0:
        tail = result.stderr[-2000:] if result.stderr else "unknown ffmpeg error"
        raise RuntimeError(f"ffmpeg failed while producing MP4: {tail}")
    return output_mp4


def _transcode_with_gstreamer(gst_launch: str, input_avi: Path, output_mp4: Path, timeout_s: float) -> Path:
    if output_mp4.exists():
        output_mp4.unlink()
    cmd = [
        gst_launch,
        "-e",
        "filesrc",
        f"location={input_avi}",
        "!",
        "avidemux",
        "!",
        "jpegdec",
        "!",
        "videoconvert",
        "!",
        "video/x-raw,format=I420",
        "!",
        "x264enc",
        "tune=zerolatency",
        "speed-preset=veryfast",
        "bitrate=6000",
        "key-int-max=30",
        "!",
        "h264parse",
        "config-interval=-1",
        "!",
        "mp4mux",
        "faststart=true",
        "!",
        "filesink",
        f"location={output_mp4}",
    ]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout_s)
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "unknown GStreamer error")[-2000:]
        raise RuntimeError(f"GStreamer failed while producing MP4: {tail}")
    if not output_mp4.exists() or output_mp4.stat().st_size <= 0:
        raise RuntimeError("GStreamer did not produce a non-empty MP4")
    return output_mp4
