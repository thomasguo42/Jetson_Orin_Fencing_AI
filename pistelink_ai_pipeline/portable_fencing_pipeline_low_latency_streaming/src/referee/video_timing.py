from __future__ import annotations

import json
import math
import shutil
import subprocess
from bisect import bisect_left, bisect_right
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

import cv2


VIDEO_EXTENSIONS = (".mp4", ".avi", ".mov", ".mkv", ".webm")
VIDEO_DEPRIORITIZE_TOKENS = ("overlay",)
VIDEO_PREFER_TOKENS = ("corrected",)
FFPROBE_TIMEOUT_SECONDS = 20


def _score_video_candidate(path: Path) -> tuple[int, str]:
    """Lower score is preferred."""
    name = path.name.lower()
    ext_rank = {ext: idx for idx, ext in enumerate(VIDEO_EXTENSIONS)}
    score = ext_rank.get(path.suffix.lower(), len(VIDEO_EXTENSIONS) + 1)
    if any(token in name for token in VIDEO_DEPRIORITIZE_TOKENS):
        score += 100
    if any(token in name for token in VIDEO_PREFER_TOKENS):
        score -= 10
    return score, name


def find_phrase_video_file(phrase_dir: Path) -> Optional[Path]:
    candidates = [
        p for p in phrase_dir.iterdir()
        if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS
    ]
    if not candidates:
        return None
    return sorted(candidates, key=_score_video_candidate)[0]


def _resolve_ffprobe_executable() -> Optional[str]:
    return shutil.which("ffprobe")


def _opencv_capture(video_path: str):
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise ValueError(f"Cannot open video with OpenCV: {video_path}")
    return cap


def _opencv_nominal_fps(video_path: str) -> float:
    cap = _opencv_capture(video_path)
    try:
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
    finally:
        cap.release()
    if not math.isfinite(fps) or fps <= 0:
        raise ValueError(f"Could not determine FPS from OpenCV for {video_path}")
    return fps


def _opencv_frame_count(video_path: str) -> int:
    cap = _opencv_capture(video_path)
    try:
        frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if frame_count > 0:
            return frame_count
        counted = 0
        while True:
            ok, _frame = cap.read()
            if not ok:
                break
            counted += 1
        return counted
    finally:
        cap.release()


def _run_ffprobe_json(args: list[str]) -> dict[str, Any]:
    ffprobe_bin = _resolve_ffprobe_executable()
    if ffprobe_bin is None:
        raise FileNotFoundError("ffprobe is not available on PATH")
    proc = subprocess.run(
        [ffprobe_bin, "-v", "error", *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=FFPROBE_TIMEOUT_SECONDS,
    )
    return json.loads(proc.stdout)


@lru_cache(maxsize=4096)
def get_video_nominal_fps(video_path: str) -> float:
    try:
        payload = _run_ffprobe_json(
            [
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=avg_frame_rate,r_frame_rate",
                "-of",
                "json",
                video_path,
            ]
        )
        streams = payload.get("streams") or []
        if not streams:
            raise ValueError(f"No video stream found in {video_path}")
        stream0 = streams[0]
        for key in ("avg_frame_rate", "r_frame_rate"):
            raw = stream0.get(key)
            if not raw or raw in {"0/0", "0"}:
                continue
            if "/" in raw:
                num, den = raw.split("/", 1)
                den_f = float(den)
                if den_f == 0:
                    continue
                fps = float(num) / den_f
            else:
                fps = float(raw)
            if fps > 0:
                return fps
    except Exception:
        pass
    return _opencv_nominal_fps(video_path)


@lru_cache(maxsize=2048)
def get_video_frame_timestamps(video_path: str) -> tuple[float, ...]:
    try:
        payload = _run_ffprobe_json(
            [
                "-select_streams",
                "v:0",
                "-show_frames",
                "-show_entries",
                "frame=best_effort_timestamp_time",
                "-of",
                "json",
                video_path,
            ]
        )
        frames = payload.get("frames") or []
        timestamps: list[float] = []
        for frame in frames:
            raw = frame.get("best_effort_timestamp_time")
            if raw is None:
                continue
            try:
                t = float(raw)
            except (TypeError, ValueError):
                continue
            timestamps.append(t)
        if timestamps:
            return tuple(timestamps)
    except Exception:
        pass

    fps = get_video_nominal_fps(video_path)
    frame_count = _opencv_frame_count(video_path)
    if frame_count <= 0:
        raise ValueError(f"No frame timestamps found for {video_path}")
    return tuple(float(i) / fps for i in range(frame_count))


def get_video_frame_count(video_path: str) -> int:
    return len(get_video_frame_timestamps(video_path))


def frame_index_at_or_after(time_s: float, frame_timestamps: tuple[float, ...]) -> int:
    if not frame_timestamps:
        return 0
    idx = bisect_left(frame_timestamps, max(0.0, time_s))
    return min(idx, len(frame_timestamps) - 1)


def containing_frame_index(time_s: float, frame_timestamps: tuple[float, ...]) -> int:
    if not frame_timestamps:
        return 0
    if time_s <= frame_timestamps[0]:
        return 0
    idx = bisect_right(frame_timestamps, time_s) - 1
    return max(0, min(idx, len(frame_timestamps) - 1))


def nearest_frame_index(time_s: float, frame_timestamps: tuple[float, ...]) -> int:
    if not frame_timestamps:
        return 0
    right = bisect_left(frame_timestamps, time_s)
    if right <= 0:
        return 0
    if right >= len(frame_timestamps):
        return len(frame_timestamps) - 1
    left = right - 1
    if abs(frame_timestamps[right] - time_s) < abs(time_s - frame_timestamps[left]):
        return right
    return left


def map_time_to_frame_index(
    time_s: float,
    video_path: str | Path,
    mode: str = "containing",
) -> int:
    timestamps = get_video_frame_timestamps(str(video_path))
    if mode == "containing":
        return containing_frame_index(time_s, timestamps)
    if mode == "at_or_after":
        return frame_index_at_or_after(time_s, timestamps)
    if mode == "nearest":
        return nearest_frame_index(time_s, timestamps)
    raise ValueError(f"Unsupported frame-mapping mode: {mode}")


def infer_video_fps(video_path: str | Path) -> float:
    timestamps = get_video_frame_timestamps(str(video_path))
    if len(timestamps) < 2:
        return get_video_nominal_fps(str(video_path))
    deltas = [
        timestamps[i + 1] - timestamps[i]
        for i in range(len(timestamps) - 1)
        if timestamps[i + 1] > timestamps[i]
    ]
    if not deltas:
        return get_video_nominal_fps(str(video_path))
    deltas.sort()
    median = deltas[len(deltas) // 2]
    if median <= 0:
        return get_video_nominal_fps(str(video_path))
    return 1.0 / median


def resolve_phrase_video_path(
    txt_path: str | Path,
    explicit_video_path: Optional[str | Path] = None,
) -> Optional[Path]:
    if explicit_video_path is not None:
        return Path(explicit_video_path)
    phrase_dir = Path(txt_path).resolve().parent
    return find_phrase_video_file(phrase_dir)


def load_timing_metadata(phrase_dir: str | Path) -> dict[str, Any]:
    path = Path(phrase_dir) / "timing_metadata.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def video_stream_signature(video_path: str | Path) -> dict[str, Any]:
    timestamps = get_video_frame_timestamps(str(video_path))
    fps = infer_video_fps(str(video_path))
    return {
        "frame_count": len(timestamps),
        "first_pts": float(timestamps[0]),
        "last_pts": float(timestamps[-1]),
        "fps": float(fps),
    }


def validate_frame_preserving(source_path: str | Path, candidate_path: str | Path) -> bool:
    try:
        src = video_stream_signature(source_path)
        cand = video_stream_signature(candidate_path)
    except Exception:
        return False
    if src["frame_count"] != cand["frame_count"]:
        return False
    if not math.isclose(src["fps"], cand["fps"], rel_tol=1e-3, abs_tol=1e-3):
        return False
    return True
