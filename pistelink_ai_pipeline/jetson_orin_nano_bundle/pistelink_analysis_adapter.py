"""Adapter from PisteLink match sessions to the local streaming analyzer."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from local_streaming_manager import LocalStreamingSessionManager, ensure_local_analyzer_service


@dataclass(frozen=True)
class AnalyzerConfig:
    bundle_root: Path
    python_executable: Path
    model_path: Optional[Path]
    fisheye_backend: str = "none"
    yolo_conf: float = 0.15
    yolo_imgsz: int = 512
    yolo_half: bool = False
    yolo_verbose: bool = False
    bootstrap_frames: int = 8
    queue_max: int = 720
    jpeg_quality: int = 80
    frame_encoding: str = "jpeg"
    startup_timeout: float = 120.0
    result_timeout: float = 300.0


class PisteLinkAnalyzerSession:
    def __init__(self, config: AnalyzerConfig, match_dir: Path, match_id: str, phrase_dir: Optional[Path] = None):
        self.config = config
        self.match_dir = match_dir
        self.phrase_dir = phrase_dir or match_dir
        self.match_id = match_id
        self.output_dir = match_dir / "ai" / "live_analysis"
        self.manager = LocalStreamingSessionManager(
            phrase_dir=self.phrase_dir,
            base_name=match_id,
            bundle_root=config.bundle_root,
            python_executable=config.python_executable,
            output_dir=self.output_dir,
            model_path=config.model_path,
            fisheye_backend=config.fisheye_backend,
            yolo_conf=config.yolo_conf,
            yolo_imgsz=config.yolo_imgsz,
            yolo_half=config.yolo_half,
            yolo_verbose=config.yolo_verbose,
            bootstrap_frames=config.bootstrap_frames,
            queue_max=config.queue_max,
            jpeg_quality=config.jpeg_quality,
            frame_encoding=config.frame_encoding,
            startup_timeout=config.startup_timeout,
            result_timeout=config.result_timeout,
        )

    def start(self, fps: float, width: int, height: int, expected_frames: int = 0, *, start_paused: bool = False) -> bool:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.phrase_dir.mkdir(parents=True, exist_ok=True)
        return self.manager.start_session(
            session_id=self.match_id,
            fps=fps,
            width=width,
            height=height,
            expected_frames=expected_frames,
            start_paused=start_paused,
        )

    def begin_streaming(self, fps: float, width: int, height: int, expected_frames: int = 0, *, start_paused: bool = False) -> bool:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.phrase_dir.mkdir(parents=True, exist_ok=True)
        return self.manager.begin_session(
            session_id=self.match_id,
            fps=fps,
            width=width,
            height=height,
            expected_frames=expected_frames,
            start_paused=start_paused,
        )

    def wait_until_ready(self, timeout: float, *, fail_on_timeout: bool = False) -> bool:
        return self.manager.wait_until_ready(timeout=timeout, fail_on_timeout=fail_on_timeout)

    def activate_frame_stream(self, start_frame_number: int) -> None:
        self.manager.activate_frame_stream(start_frame_number)

    def live_degraded(self, expected_total_frames: int) -> bool:
        return self.manager.live_degraded(expected_total_frames)

    def end(self, signal_data: bytes, signal_filename: str, total_frames: int) -> Optional[Dict[str, Any]]:
        self.manager.end_session(
            signal_data=signal_data,
            signal_filename=signal_filename,
            total_frames=total_frames,
        )
        return self.manager.get_result(timeout=self.config.result_timeout)

    def cancel(self, reason: str = "") -> None:
        self.manager.cancel_session(reason or "pistelink_cancelled")


def warm_pistelink_analyzer(config: AnalyzerConfig, width: int, height: int) -> None:
    ensure_local_analyzer_service(
        bundle_root=config.bundle_root,
        python_executable=config.python_executable,
        model_path=config.model_path,
        fisheye_backend=config.fisheye_backend,
        yolo_conf=config.yolo_conf,
        yolo_imgsz=config.yolo_imgsz,
        yolo_half=config.yolo_half,
        yolo_verbose=config.yolo_verbose,
        bootstrap_frames=config.bootstrap_frames,
        startup_timeout=config.startup_timeout,
        result_timeout=config.result_timeout,
        width=width,
        height=height,
    )


def default_analyzer_config() -> AnalyzerConfig:
    bundle_dir = Path(__file__).resolve().parent
    project_root = bundle_dir.parent
    default_bundle_root = project_root / "portable_fencing_pipeline_low_latency_streaming"
    bundle_root = Path(os.environ.get("PISTELINK_ANALYZER_ROOT", default_bundle_root)).expanduser()

    python_executable = _default_python_executable(bundle_root)
    model_path = _default_model_path(bundle_root)

    return AnalyzerConfig(
        bundle_root=bundle_root,
        python_executable=python_executable,
        model_path=model_path,
        fisheye_backend=os.environ.get("PISTELINK_ANALYZER_FISHEYE_BACKEND", "none"),
        yolo_conf=float(os.environ.get("PISTELINK_ANALYZER_YOLO_CONF", "0.15")),
        yolo_imgsz=int(os.environ.get("PISTELINK_ANALYZER_YOLO_IMGSZ", "512")),
        yolo_half=_env_bool("PISTELINK_ANALYZER_YOLO_HALF", False),
        yolo_verbose=_env_bool("PISTELINK_ANALYZER_YOLO_VERBOSE", False),
        bootstrap_frames=int(os.environ.get("PISTELINK_ANALYZER_BOOTSTRAP_FRAMES", "8")),
        queue_max=int(os.environ.get("PISTELINK_ANALYZER_QUEUE_MAX", "720")),
        jpeg_quality=int(os.environ.get("PISTELINK_ANALYZER_JPEG_QUALITY", "80")),
        frame_encoding=os.environ.get("PISTELINK_ANALYZER_FRAME_ENCODING", "jpeg"),
        startup_timeout=float(os.environ.get("PISTELINK_ANALYZER_STARTUP_TIMEOUT", "120")),
        result_timeout=float(os.environ.get("PISTELINK_ANALYZER_RESULT_TIMEOUT", "300")),
    )


def _default_python_executable(bundle_root: Path) -> Path:
    env_value = os.environ.get("PISTELINK_ANALYZER_PYTHON")
    if env_value:
        return Path(env_value).expanduser()

    candidates = [
        bundle_root / ".venv" / "bin" / "python",
        Path("/home/thomas/fencing/portable_fencing_pipeline_low_latency_streaming/.venv/bin/python"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return Path(sys.executable)


def _default_model_path(bundle_root: Path) -> Optional[Path]:
    env_value = os.environ.get("PISTELINK_ANALYZER_MODEL_PATH")
    if env_value:
        return Path(env_value).expanduser()

    candidates = [
        bundle_root
        / "experiments"
        / "yolov8_pose"
        / "matrix_all_20260404"
        / "yolo26l-pose"
        / "yolo26l-pose_fast_fp16_ultra.engine",
        bundle_root / "yolo26s-pose.pt",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}
