from __future__ import annotations

"""Debug referee pipeline for fencing phrase analysis.

Architecture at a glance:
1. Parse phrase metadata and side-hit timestamps from TXT.
2. Load and frame-cap keypoints from XLSX.
3. Detect low-level signals (pause/retreat, lunge, arm extension, blade contact).
4. Resolve right-of-way and winner in ``referee_decision``.

This file is intentionally parameter-driven: all tunable constants live in the
configuration block below so behavior can be inspected/changed in one place.
"""

import argparse
import contextlib
import json
import math
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import joblib

SCRIPTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPTS_DIR.parent
sys.path.append(str(SCRIPTS_DIR))
sys.path.append(str(PROJECT_ROOT))

import blade_touch_referee as btr  # type: ignore
import classify_accident_contact as cac  # type: ignore
import classify_blade_contact_benefit as cbc  # type: ignore
from src.referee.video_timing import (
    find_phrase_video_file as shared_find_phrase_video_file,
    infer_video_fps,
    map_time_to_frame_index,
    resolve_phrase_video_path,
)

# ============================================================================
# Configuration
# ============================================================================

# Runtime and logging.
DEBUG_LOGGING = True
LOGISTIC_MODEL_CACHE = None

# Default filesystem locations.
INPUT_SEARCH_DIRS = [
    PROJECT_ROOT / "runtime_outputs" / "experimental_limb_interp_jumpsafe",
    PROJECT_ROOT / "runtime_inputs",
    PROJECT_ROOT / "runtime_outputs" / "fps30_validation" / "correct_results" / "no_blade_contact",
    PROJECT_ROOT / "runtime_outputs" / "fps30_validation" / "mismatched_results" / "no_blade_contact",
    PROJECT_ROOT / "runtime_outputs" / "fps30_validation" / "correct_results" / "blade_contact",
    PROJECT_ROOT / "runtime_outputs" / "fps30_validation" / "mismatched_results" / "blade_contact",
    PROJECT_ROOT / "data" / "training_data",
]
DEBUG_OUTPUT_PATH = PROJECT_ROOT / "logs" / "debug.txt"
MODEL_PATH = PROJECT_ROOT / "results" / "blade_touch_referee_model.joblib"
FPS_THRESHOLD_BASE = 15.0

# Data layout / keypoint indexing.
DEFAULT_FPS = 30.0
MIN_VALID_FPS = 0.1
NUM_KEYPOINTS = 17
KP_BACK_FOOT = 15
KP_FRONT_FOOT = 16
KP_WEAPON_WRIST = 10
KP_FRONT_HIP = 12
KP_WEAPON_SHOULDER = 6
KP_WEAPON_ELBOW = 8
CENTER_OF_MASS_KEYPOINTS = [5, 6, 11, 12]
VIDEO_EXTENSIONS = (".mp4", ".avi", ".mov", ".mkv", ".webm")
VIDEO_DEPRIORITIZE_TOKENS = ("overlay",)
VIDEO_PREFER_TOKENS = ("corrected",)
FFPROBE_TIMEOUT_SECONDS = 8
PHRASE_END_LINE_TOKEN = "Phrase recording ended"

# Frame preprocessing.
DROP_TAIL_FRAMES = 0
HIT_FRAME_DELAY = 0

# Pause-overlap gating used to enter attack-window comparison.
PAUSE_OVERLAP_MAX_TOTAL_SECONDS = 1.0
PAUSE_OVERLAP_MIN_RATIO = 0.5
PAUSE_OVERLAP_MAX_END_DELTA_FRAMES = 6

# Lunge detection and lunge-based early decision.
LUNGE_DISTANCE_THRESHOLD = 1.0
LUNGE_EXPANSION_DISTANCE_THRESHOLD = 0.9
LUNGE_MIN_CONSECUTIVE_FRAMES = 5
LUNGE_BACKWARD_THRESHOLD = 0.08
LUNGE_MAX_BACKWARD_FRAME_RATIO = 0.30
LUNGE_OVERLAP_START_GAP_FRAMES = 15
LUNGE_BENEFICIAL_HIT_END_GRACE_FRAMES = 2
LUNGE_RESPONSE_BUFFER_FRAMES = 6
LUNGE_END_PAUSE_MIN_FRAMES = 2
LUNGE_END_PAUSE_START_WINDOW_FRAMES = 4
LUNGE_HIP_ANGLE_MIN_DEG = 125.0
LUNGE_HIP_ANGLE_MIN_FRAMES = 2
LUNGE_HIP_ANGLE_DISTANCE_OVERRIDE = 1.2
LUNGE_HIP_ANGLE_DISTANCE_OVERRIDE_MIN_FRAMES = 2

# Pause / retreat detection.
PAUSE_INITIAL_START_FRAME = 0
PAUSE_VELOCITY_THRESHOLD = 0.035
PAUSE_RETREAT_THRESHOLD = 0.035
PAUSE_MIN_FRAMES = 8
PAUSE_Y_VARIANCE_THRESHOLD = 0.0012
PAUSE_BACK_FOOT_FORWARD_THRESHOLD = 0.05

# Slow-start extraction.
SLOW_START_MAX_START_FRAME = 10
SLOW_START_MAX_DURATION_SECONDS = 0.5

# Arm extension detection.
ARM_EXTENSION_X_DISTANCE_THRESHOLD = 0.45
ARM_EXTENSION_MIN_FRAMES = 2
ARM_EXTENSION_MAX_HIT_GAP_FRAMES = 8
ARM_EXTENSION_STRAIGHT_ANGLE_DEG = 140.0
ARM_EXTENSION_HARMFUL_MAX_FRAMES = 17
ARM_EXTENSION_HARMFUL_EARLY_END_FRAMES = 8
ARM_EXTENSION_HARMFUL_BODY_ANGLE_DEG = 70.0
ARM_EXTENSION_RESPONSE_BUFFER_FRAMES = 6

# Simultaneous attack-window weighting.
SIMULTANEOUS_ARM_START_WEIGHT = 0.6
SIMULTANEOUS_LUNGE_START_WEIGHT = 0.4

# Misc attack windows and logistic model behavior.
ATTACK_WINDOW_DEFAULT_START_FRAME = 0
LOGISTIC_P_LEFT_INDEX = 1
LOGISTIC_LEFT_THRESHOLD = 0.5
LOGISTIC_DEFAULT_WINNER = "right"

BLADE_BENEFIT_PRE_AHEAD_WEIGHT = -1.0
BLADE_BENEFIT_POST_AHEAD_WEIGHT = 0.75
BLADE_BENEFIT_SCORE_THRESHOLD = 0.14
BLADE_BENEFIT_AHEAD_MARGIN = 0.08
BLADE_BENEFIT_HIGH_CONF_MARGIN = 0.16
BLADE_BENEFIT_AHEAD_SCALE = 0.08

BTR_BASE_BLEND_WINDOW = int(getattr(btr, "BLEND_WINDOW", 5))
BTR_BASE_VELOCITY_LAG = int(getattr(btr, "VELOCITY_LAG", 2))
BTR_BASE_MOMENTUM_WINDOW = int(getattr(btr, "MOMENTUM_WINDOW", 6))


def _normalise_fps_for_scaling(fps: float) -> float:
    if not np.isfinite(fps) or fps < MIN_VALID_FPS:
        return FPS_THRESHOLD_BASE
    return fps


def _scale_frame_threshold(frames: int, fps: float, *, minimum: int = 0) -> int:
    if frames <= 0:
        return 0
    safe_fps = _normalise_fps_for_scaling(fps)
    scaled = int(round(frames * safe_fps / FPS_THRESHOLD_BASE))
    return max(minimum, scaled)


def _scale_per_frame_threshold(value: float, fps: float) -> float:
    safe_fps = _normalise_fps_for_scaling(fps)
    return value * (FPS_THRESHOLD_BASE / safe_fps)


def _configure_blade_touch_feature_extractor(fps: float) -> None:
    safe_fps = _normalise_fps_for_scaling(fps)
    btr.FPS = safe_fps
    btr.BLEND_WINDOW = _scale_frame_threshold(BTR_BASE_BLEND_WINDOW, safe_fps, minimum=1)
    btr.VELOCITY_LAG = _scale_frame_threshold(BTR_BASE_VELOCITY_LAG, safe_fps, minimum=1)
    btr.MOMENTUM_WINDOW = _scale_frame_threshold(BTR_BASE_MOMENTUM_WINDOW, safe_fps, minimum=1)

# ============================================================================
# Logging helper
# ============================================================================

def _debug(message: str) -> None:
    if DEBUG_LOGGING:
        print(message)

def _decide_attack_by_arm_and_speed(
    left_xdata: Dict[int, List[float]],
    left_ydata: Dict[int, List[float]],
    right_xdata: Dict[int, List[float]],
    right_ydata: Dict[int, List[float]],
    left_slow_start: Optional[PauseInterval],
    right_slow_start: Optional[PauseInterval],
    window_start: int,
    window_end: int,
    fps: float,
    left_window_start: Optional[int] = None,
    right_window_start: Optional[int] = None,
    left_window_end: Optional[int] = None,
    right_window_end: Optional[int] = None,
    left_lunges: Optional[List[LungeInterval]] = None,
    right_lunges: Optional[List[LungeInterval]] = None,
) -> Tuple[str, str, Dict, List[ArmExtensionInterval], List[ArmExtensionInterval]]:
    """Compare attacks inside configured frame windows.

    Simultaneous attack-window policy:
    1) Presence of near-hit arm extension outranks lunge presence.
    2) If arm presence matches, presence of beneficial lunge breaks the tie.
    3) If both sides match on signal presence, compare weighted start frames
       across the available signals, with arm extension weighted slightly more.
    4) If still tied, apply slow-start penalty, then speed.
    """
    left_start = window_start if left_window_start is None else left_window_start
    right_start = window_start if right_window_start is None else right_window_start
    left_end = window_end if left_window_end is None else left_window_end
    right_end = window_end if right_window_end is None else right_window_end
    if left_end < left_start:
        left_end = left_start
    if right_end < right_start:
        right_end = right_start

    _debug(
        "[AttackWindow] "
        f"left={left_start}-{left_end} right={right_start}-{right_end}"
    )

    def _latest_beneficial_lunge_start(
        lunges: Optional[List[LungeInterval]],
        start: int,
        end: int,
    ) -> Optional[LungeInterval]:
        if not lunges:
            return None
        eligible = [
            interval for interval in lunges
            if (not interval.is_penalizing)
            and interval.start_frame >= start
            and interval.start_frame <= end
            and interval.end_frame <= end
        ]
        if not eligible:
            return None
        return max(eligible, key=lambda interval: (interval.end_frame, interval.start_frame))

    left_extensions = detect_arm_extension(
        left_xdata,
        left_ydata,
        is_left_fencer=True,
        fps=fps,
        hit_frame=left_end,
        start_frame=left_start,
        end_frame=left_end,
        debug=True,
    )
    right_extensions = detect_arm_extension(
        right_xdata,
        right_ydata,
        is_left_fencer=False,
        fps=fps,
        hit_frame=right_end,
        start_frame=right_start,
        end_frame=right_end,
        debug=True,
    )

    left_near_hit_extensions = [interval for interval in left_extensions if interval.near_hit]
    right_near_hit_extensions = [interval for interval in right_extensions if interval.near_hit]
    left_latest_ext = left_near_hit_extensions[-1] if left_near_hit_extensions else None
    right_latest_ext = right_near_hit_extensions[-1] if right_near_hit_extensions else None
    left_beneficial_lunge = _latest_beneficial_lunge_start(left_lunges, left_start, left_end)
    right_beneficial_lunge = _latest_beneficial_lunge_start(right_lunges, right_start, right_end)

    left_has_arm = left_latest_ext is not None
    right_has_arm = right_latest_ext is not None
    left_has_lunge = left_beneficial_lunge is not None
    right_has_lunge = right_beneficial_lunge is not None

    if left_has_arm != right_has_arm:
        if left_has_arm:
            return (
                "left",
                "Left wins: left has a near-hit arm extension while right does not; "
                "arm extension outranks lunge presence in simultaneous attack-window judging.",
                {},
                left_extensions,
                right_extensions,
            )
        return (
            "right",
            "Right wins: right has a near-hit arm extension while left does not; "
            "arm extension outranks lunge presence in simultaneous attack-window judging.",
            {},
            left_extensions,
            right_extensions,
        )

    if left_has_lunge != right_has_lunge:
        if left_has_lunge:
            return (
                "left",
                "Left wins: arm-extension presence is tied, but only left has a beneficial lunge "
                "in the attack window.",
                {},
                left_extensions,
                right_extensions,
            )
        return (
            "right",
            "Right wins: arm-extension presence is tied, but only right has a beneficial lunge "
            "in the attack window.",
            {},
            left_extensions,
            right_extensions,
        )

    weighted_start_info: Dict[str, float] = {}

    def _weighted_start(
        arm_interval: Optional[ArmExtensionInterval],
        lunge_interval: Optional[LungeInterval],
        side_label: str,
    ) -> Optional[float]:
        components: List[Tuple[str, float, float]] = []
        if arm_interval is not None and left_has_arm and right_has_arm:
            components.append(("arm", float(arm_interval.effective_start_frame), SIMULTANEOUS_ARM_START_WEIGHT))
        if lunge_interval is not None and left_has_lunge and right_has_lunge:
            components.append(("lunge", float(lunge_interval.start_frame), SIMULTANEOUS_LUNGE_START_WEIGHT))
        if not components:
            return None
        total_weight = sum(weight for _, _, weight in components)
        score = sum(start * weight for _, start, weight in components) / total_weight
        weighted_start_info[f"{side_label}_weighted_start"] = score
        for name, start, weight in components:
            weighted_start_info[f"{side_label}_{name}_start_frame"] = start
            weighted_start_info[f"{side_label}_{name}_weight"] = weight
        return score

    left_weighted_start = _weighted_start(left_latest_ext, left_beneficial_lunge, "left")
    right_weighted_start = _weighted_start(right_latest_ext, right_beneficial_lunge, "right")

    if left_weighted_start is not None and right_weighted_start is not None:
        if left_weighted_start < right_weighted_start:
            return (
                "left",
                "Left wins: both sides match on arm-extension and beneficial-lunge presence, "
                f"but left's weighted signal start is earlier "
                f"({left_weighted_start:.2f} vs {right_weighted_start:.2f} frames; "
                f"arm_weight={SIMULTANEOUS_ARM_START_WEIGHT:.2f}, "
                f"lunge_weight={SIMULTANEOUS_LUNGE_START_WEIGHT:.2f}).",
                weighted_start_info,
                left_extensions,
                right_extensions,
            )
        if right_weighted_start < left_weighted_start:
            return (
                "right",
                "Right wins: both sides match on arm-extension and beneficial-lunge presence, "
                f"but right's weighted signal start is earlier "
                f"({right_weighted_start:.2f} vs {left_weighted_start:.2f} frames; "
                f"arm_weight={SIMULTANEOUS_ARM_START_WEIGHT:.2f}, "
                f"lunge_weight={SIMULTANEOUS_LUNGE_START_WEIGHT:.2f}).",
                weighted_start_info,
                left_extensions,
                right_extensions,
            )

    if left_slow_start and not right_slow_start:
        return (
            "right",
            "Signal presence and weighted start are tied; left loses on slow-start penalty.",
            weighted_start_info,
            left_extensions,
            right_extensions,
        )
    if right_slow_start and not left_slow_start:
        return (
            "left",
            "Signal presence and weighted start are tied; right loses on slow-start penalty.",
            weighted_start_info,
            left_extensions,
            right_extensions,
        )

    left_speed, left_accel = calculate_speed_acceleration(
        left_xdata, left_ydata, start_frame=left_start, end_frame=left_end
    )
    right_speed, right_accel = calculate_speed_acceleration(
        right_xdata, right_ydata, start_frame=right_start, end_frame=right_end
    )

    speed_info = {
        'left_speed': left_speed,
        'left_accel': left_accel,
        'right_speed': right_speed,
        'right_accel': right_accel
    }

    if left_speed > right_speed:
        return (
            "left",
            f'No arm extensions detected. Left faster (speed: {left_speed:.3f} vs {right_speed:.3f})',
            speed_info,
            left_extensions,
            right_extensions,
        )
    return (
        "right",
        f'No arm extensions detected. Right faster (speed: {right_speed:.3f} vs {left_speed:.3f})',
        speed_info,
        left_extensions,
        right_extensions,
    )

def _pause_overlap_ok(
    left_pauses: List[PauseInterval],
    right_pauses: List[PauseInterval],
    fps: float,
    max_total_seconds: float = PAUSE_OVERLAP_MAX_TOTAL_SECONDS,
    min_overlap_ratio: float = PAUSE_OVERLAP_MIN_RATIO,
    max_end_frame_delta: int = PAUSE_OVERLAP_MAX_END_DELTA_FRAMES,
    left_last_end: Optional[int] = None,
    right_last_end: Optional[int] = None,
) -> Tuple[bool, float]:
    """Check whether left/right pauses are sufficiently overlapping."""
    left_frames = set()
    for interval in left_pauses:
        left_frames.update(range(interval.start_frame, interval.end_frame + 1))
    right_frames = set()
    for interval in right_pauses:
        right_frames.update(range(interval.start_frame, interval.end_frame + 1))

    if not left_frames or not right_frames:
        return False, 0.0

    left_total = len(left_frames)
    right_total = len(right_frames)
    max_total_frames = fps * max_total_seconds
    if left_total >= max_total_frames or right_total >= max_total_frames:
        return False, 0.0

    overlap_frames = len(left_frames & right_frames)
    overlap_ratio = overlap_frames / max(left_total, right_total)
    if overlap_ratio < min_overlap_ratio:
        return False, overlap_ratio

    if left_last_end is not None and right_last_end is not None:
        if abs(left_last_end - right_last_end) > max_end_frame_delta:
            return False, overlap_ratio

    _debug(
        "[PauseOverlap] totals "
        f"left={left_total} right={right_total} overlap={overlap_frames} "
        f"ratio={overlap_ratio:.2f}"
    )
    return True, overlap_ratio

def _load_logistic_model():
    global LOGISTIC_MODEL_CACHE
    if LOGISTIC_MODEL_CACHE is None:
        if not MODEL_PATH.exists():
            return None
        payload = joblib.load(MODEL_PATH)
        LOGISTIC_MODEL_CACHE = payload
    return LOGISTIC_MODEL_CACHE

# ============================================================================
# Data models
# ============================================================================

@dataclass
class BladeContact:
    """Represents a blade-to-blade contact"""
    time: float
    frame: int

@dataclass
class PauseInterval:
    """Represents a pause/retreat interval"""
    start_frame: int
    end_frame: int
    start_time: float
    end_time: float
    duration: float
    is_retreat: bool = False

@dataclass
class LungeInterval:
    """Represents a lunge interval based on front/back foot distance."""
    start_frame: int
    end_frame: int
    start_time: float
    end_time: float
    is_penalizing: bool = True
    classification: str = "penalizing"
    overlap_hit_frames: List[int] = field(default_factory=list)

@dataclass
class FencingPhrase:
    """
    Contains all data for a fencing phrase
    Important: Fencer 1 = Right fencer, Fencer 2 = Left fencer
    """
    start_time: float
    start_frame: int
    simultaneous_hit_time: Optional[float]  # Both fencers hit at this time
    simultaneous_hit_frame: Optional[int]
    blade_contacts: List[BladeContact]
    lockout_start: Optional[float]
    declared_winner: str
    fps: float = DEFAULT_FPS

# ============================================================================
# Generic helpers
# ============================================================================

def _time_to_frame(time_s: float, fps: float) -> int:
    """Convert seconds to integer frame index."""
    return int(time_s * fps)

def _as_positive_float(value: Optional[str]) -> Optional[float]:
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _as_positive_int(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _score_video_candidate(path: Path) -> Tuple[int, str]:
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
    """Find the best candidate phrase video file in a folder."""
    return shared_find_phrase_video_file(phrase_dir)


def find_phrase_txt_file(phrase_dir: Path) -> Optional[Path]:
    txt_files = sorted(
        p for p in phrase_dir.glob("*.txt")
        if p.name not in {"json.txt", "jjLog.txt", "debug.txt"}
    )
    return txt_files[0] if txt_files else None


def find_phrase_excel_file(phrase_dir: Path) -> Optional[Path]:
    preferred = sorted(phrase_dir.glob("*_keypoints.xlsx"))
    if preferred:
        return preferred[0]
    candidates = sorted(phrase_dir.glob("*.xlsx"))
    return candidates[0] if candidates else None


def _extract_phrase_end_time_from_txt(txt_path: Path) -> Optional[float]:
    """Extract phrase duration from TXT line containing 'Phrase recording ended'."""
    if not txt_path.exists():
        return None
    end_time = None
    with txt_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if PHRASE_END_LINE_TOKEN not in line:
                continue
            match = re.search(r"(\d+\.\d+)s", line)
            if match:
                end_time = float(match.group(1))
    return end_time if end_time is not None and end_time > 0 else None


def _find_txt_file(phrase_dir: Path) -> Optional[Path]:
    return find_phrase_txt_file(phrase_dir)


def infer_phrase_fps(
    phrase_dir: Path,
    txt_path: Optional[Path] = None,
    fallback_fps: float = DEFAULT_FPS,
) -> float:
    """Infer FPS from the actual phrase video stream."""
    video_path = find_phrase_video_file(phrase_dir)
    if video_path is None:
        _debug(f"[FPS] no video found in {phrase_dir}; fallback fps={fallback_fps:.3f}")
        return fallback_fps
    try:
        fps = infer_video_fps(str(video_path))
    except Exception as exc:
        _debug(f"[FPS] ffprobe failed for {video_path.name}: {exc}; fallback fps={fallback_fps:.3f}")
        return fallback_fps
    if fps < MIN_VALID_FPS:
        _debug(
            f"[FPS] invalid computed fps for {video_path.name}: {fps:.6f}; "
            f"fallback fps={fallback_fps:.3f}"
        )
        return fallback_fps
    _debug(f"[FPS] {video_path.name}: fps={fps:.6f}")
    return fps


def infer_phrase_fps_from_video_path(
    video_path: Path,
    fallback_fps: float = DEFAULT_FPS,
) -> float:
    """Infer FPS directly from a known phrase video path."""
    try:
        fps = infer_video_fps(str(video_path))
    except Exception as exc:
        _debug(f"[FPS] ffprobe failed for {video_path.name}: {exc}; fallback fps={fallback_fps:.3f}")
        return fallback_fps
    if fps < MIN_VALID_FPS:
        _debug(
            f"[FPS] invalid computed fps for {video_path.name}: {fps:.6f}; "
            f"fallback fps={fallback_fps:.3f}"
        )
        return fallback_fps
    _debug(f"[FPS] {video_path.name}: fps={fps:.6f}")
    return fps


def sanitize_for_json(value):
    """Convert numpy/dataclass values into JSON-serialisable primitives."""
    if value is None:
        return None
    if isinstance(value, (str, bool)):
        return value
    if isinstance(value, (int, np.integer)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        if math.isnan(value) or math.isinf(value):
            return None
        return float(value)
    if isinstance(value, (list, tuple, set)):
        return [sanitize_for_json(item) for item in value]
    if isinstance(value, dict):
        return {str(key): sanitize_for_json(item) for key, item in value.items()}
    if isinstance(value, np.ndarray):
        return sanitize_for_json(value.tolist())
    if is_dataclass(value):
        return sanitize_for_json(asdict(value))
    if hasattr(value, "tolist") and not isinstance(value, str):
        return sanitize_for_json(value.tolist())
    if isinstance(value, Path):
        return str(value)
    return value


def run_referee_on_keypoints(
    *,
    txt_path: Path,
    video_path: Path,
    left_x: Dict,
    left_y: Dict,
    right_x: Dict,
    right_y: Dict,
    normalisation_constant: Optional[float] = None,
    decision_output_path: Optional[Path] = None,
    debug_output_path: Optional[Path] = None,
    debug_logging: bool = False,
) -> Dict[str, Any]:
    """Run the FPS30 referee directly on in-memory keypoint dictionaries."""
    global DEBUG_LOGGING

    txt_path = Path(txt_path)
    video_path = Path(video_path)

    def _run_core() -> Dict[str, Any]:
        phrase_fps = infer_phrase_fps_from_video_path(video_path, fallback_fps=DEFAULT_FPS)
        phrase = parse_txt_file(str(txt_path), fps=phrase_fps, video_path=str(video_path))
        if left_x and KP_FRONT_FOOT in left_x:
            max_frame = len(left_x[KP_FRONT_FOOT]) - 1
            _trim_phrase_to_frames(phrase, max_frame)
        side_hit_events = extract_side_hit_events(str(txt_path), fps=phrase.fps, video_path=str(video_path))
        decision = referee_decision(
            phrase,
            left_x,
            left_y,
            right_x,
            right_y,
            normalisation_constant=normalisation_constant,
            side_hit_events=side_hit_events,
        )
        persisted_decision = sanitize_for_json(decision)
        if normalisation_constant is not None and "normalisation_constant" not in persisted_decision:
            persisted_decision["normalisation_constant"] = normalisation_constant
        if "phrase_fps" not in persisted_decision:
            persisted_decision["phrase_fps"] = phrase.fps
        if decision_output_path is not None:
            decision_output_path.parent.mkdir(parents=True, exist_ok=True)
            decision_output_path.write_text(json.dumps(persisted_decision, indent=2), encoding="utf-8")
        return persisted_decision

    previous_debug_logging = DEBUG_LOGGING
    DEBUG_LOGGING = debug_logging
    try:
        if debug_output_path is not None:
            debug_output_path.parent.mkdir(parents=True, exist_ok=True)
            with open(debug_output_path, "w", encoding="utf-8") as log_file:
                with contextlib.redirect_stdout(log_file), contextlib.redirect_stderr(log_file):
                    print(f"Analyzing {txt_path.parent}...")
                    return _run_core()
        return _run_core()
    finally:
        DEBUG_LOGGING = previous_debug_logging

def parse_txt_file(
    txt_path: str,
    fps: float = DEFAULT_FPS,
    video_path: Optional[str] = None,
) -> FencingPhrase:
    """Parse the TXT file to extract timing information using provided FPS."""
    with open(txt_path, 'r') as f:
        lines = f.readlines()
    
    start_time = None
    simultaneous_hit_time = None
    blade_contacts = []
    lockout_start = None
    declared_winner = None
    scoreboard_f1 = None
    scoreboard_f2 = None
    hit_events: List[float] = []
    
    for line in lines:
        if "Phrase recording started" in line:
            match = re.search(r'(\d+\.\d+)s', line)
            if match:
                start_time = float(match.group(1))
        
        if "Simultaneous valid hits" in line:
            match = re.search(r'(\d+\.\d+)s', line)
            if match:
                simultaneous_hit_time = float(match.group(1))

        if "Off-Target: Blade-to-blade contact" in line:
            match = re.search(r'(\d+\.\d+)s', line)
            if match:
                time = float(match.group(1))
                blade_contacts.append(BladeContact(time=time, frame=0))

        if "| HIT:" in line:
            match = re.search(r'(\d+\.\d+)s', line)
            if match:
                hit_events.append(float(match.group(1)))

        if "Lockout period started" in line:
            match = re.search(r'(\d+\.\d+)s', line)
            if match:
                lockout_start = float(match.group(1))

        if "Winner:" in line:
            match = re.search(r'Winner: (\w+)', line)
            if match:
                declared_winner = match.group(1)

        if "Scores ->" in line:
            match = re.search(
                r"Scores -> Fencer 1:\s*([A-Za-z]+),\s*Fencer 2:\s*([A-Za-z]+)",
                line,
            )
            if match:
                scoreboard_f1 = match.group(1).strip().upper()
                scoreboard_f2 = match.group(2).strip().upper()
    
    both_hit = (scoreboard_f1 == "HIT" and scoreboard_f2 == "HIT")

    if both_hit:
        if simultaneous_hit_time is None:
            if hit_events:
                simultaneous_hit_time = max(hit_events)
            elif lockout_start is not None:
                simultaneous_hit_time = lockout_start
            elif start_time is not None:
                simultaneous_hit_time = start_time
    else:
        simultaneous_hit_time = None

    resolved_video_path = resolve_phrase_video_path(txt_path, explicit_video_path=video_path)
    effective_fps = fps
    if resolved_video_path is not None:
        effective_fps = infer_video_fps(str(resolved_video_path))
        for blade_contact in blade_contacts:
            blade_contact.frame = map_time_to_frame_index(
                blade_contact.time,
                resolved_video_path,
                mode="containing",
            )
        start_frame = map_time_to_frame_index(start_time or 0.0, resolved_video_path, mode="containing")
        simultaneous_hit_frame = (
            map_time_to_frame_index(simultaneous_hit_time, resolved_video_path, mode="containing")
            if simultaneous_hit_time is not None
            else None
        )
    else:
        for blade_contact in blade_contacts:
            blade_contact.frame = _time_to_frame(blade_contact.time, effective_fps)
        start_frame = _time_to_frame(start_time, effective_fps) if start_time else 0
        simultaneous_hit_frame = _time_to_frame(simultaneous_hit_time, effective_fps) if simultaneous_hit_time else None

    return FencingPhrase(
        start_time=start_time or 0.0,
        start_frame=start_frame,
        simultaneous_hit_time=simultaneous_hit_time,
        simultaneous_hit_frame=simultaneous_hit_frame,
        blade_contacts=blade_contacts,
        lockout_start=lockout_start,
        declared_winner=declared_winner,
        fps=effective_fps
    )


def extract_side_hit_events(
    txt_path: str,
    fps: float = DEFAULT_FPS,
    video_path: Optional[str] = None,
) -> Dict[str, List[Dict[str, float]]]:
    """
    Extract explicit side hit lines from TXT and map them to frame indices.

    Returns keys:
    - left_scores_on_right: [{time, frame}, ...]
    - right_scores_on_left: [{time, frame}, ...]
    """
    pattern = re.compile(
        r"(?P<time>\d+\.\d+)s\s*\|\s*HIT:\s*(?P<scorer>Left|Right)\s+scores\s+on\s+(?P<target>Left|Right)!",
        re.IGNORECASE,
    )
    events = {
        "left_scores_on_right": [],
        "right_scores_on_left": [],
    }

    with open(txt_path, "r") as f:
        for line in f:
            m = pattern.search(line)
            if not m:
                continue
            time_s = float(m.group("time"))
            scorer = m.group("scorer").lower()
            target = m.group("target").lower()
            resolved_video_path = resolve_phrase_video_path(txt_path, explicit_video_path=video_path)
            if resolved_video_path is not None:
                frame = map_time_to_frame_index(time_s, resolved_video_path, mode="containing")
            else:
                frame = _time_to_frame(time_s, fps)
            payload = {"time": time_s, "frame": frame}

            if scorer == "left" and target == "right":
                events["left_scores_on_right"].append(payload)
            elif scorer == "right" and target == "left":
                events["right_scores_on_left"].append(payload)

    return events

def load_keypoints_from_excel(excel_path: str) -> Tuple[Dict, Dict, Dict, Dict]:
    """Load keypoint data from Excel file.

    Supports both legacy lowercase sheet names and title-cased variants:
    - left_x / Left_X
    - left_y / Left_Y
    - right_x / Right_X
    - right_y / Right_Y
    """
    xls = pd.ExcelFile(excel_path)
    normalised = {name.strip().lower(): name for name in xls.sheet_names}

    required = {
        "left_x": None,
        "left_y": None,
        "right_x": None,
        "right_y": None,
    }
    for key in required:
        required[key] = normalised.get(key)

    missing = [key for key, actual in required.items() if actual is None]
    if missing:
        raise ValueError(
            f"Missing required keypoint sheets {missing} in {excel_path}; "
            f"available sheets: {xls.sheet_names}"
        )

    df_left_x = pd.read_excel(xls, sheet_name=required["left_x"])
    df_left_y = pd.read_excel(xls, sheet_name=required["left_y"])
    df_right_x = pd.read_excel(xls, sheet_name=required["right_x"])
    df_right_y = pd.read_excel(xls, sheet_name=required["right_y"])

    def _extract_keypoint_dict(df: pd.DataFrame, sheet_name: str) -> Dict[int, List[float]]:
        # Support both column schemas:
        # - kp_0 ... kp_16
        # - 0 ... 16 (int or string headers)
        lookup = {str(col).strip().lower(): col for col in df.columns}
        data: Dict[int, List[float]] = {}
        missing: List[int] = []
        for i in range(NUM_KEYPOINTS):
            preferred = f"kp_{i}"
            fallback = str(i)
            actual_col = lookup.get(preferred) if preferred in lookup else lookup.get(fallback)
            if actual_col is None:
                missing.append(i)
                continue
            data[i] = pd.to_numeric(df[actual_col], errors="coerce").tolist()
        if missing:
            raise ValueError(
                f"Missing keypoint columns {missing} in sheet '{sheet_name}' of {excel_path}; "
                f"available columns: {list(df.columns)}"
            )
        return data

    left_xdata = _extract_keypoint_dict(df_left_x, required["left_x"] or "left_x")
    left_ydata = _extract_keypoint_dict(df_left_y, required["left_y"] or "left_y")
    right_xdata = _extract_keypoint_dict(df_right_x, required["right_x"] or "right_x")
    right_ydata = _extract_keypoint_dict(df_right_y, required["right_y"] or "right_y")
    
    if DROP_TAIL_FRAMES > 0:
        left_xdata = _trim_keypoint_data(left_xdata, DROP_TAIL_FRAMES)
        left_ydata = _trim_keypoint_data(left_ydata, DROP_TAIL_FRAMES)
        right_xdata = _trim_keypoint_data(right_xdata, DROP_TAIL_FRAMES)
        right_ydata = _trim_keypoint_data(right_ydata, DROP_TAIL_FRAMES)

    return left_xdata, left_ydata, right_xdata, right_ydata


def _dict_to_array(data: Dict[int, List[float]]) -> np.ndarray:
    return np.array([data[kp] for kp in range(NUM_KEYPOINTS)], dtype=float).T

def _trim_keypoint_data(data: Dict[int, List[float]], drop_frames: int) -> Dict[int, List[float]]:
    if drop_frames <= 0:
        return data
    trimmed = {}
    for kp, values in data.items():
        trimmed[kp] = values[:-drop_frames] if len(values) > drop_frames else []
    return trimmed

def _truncate_keypoint_data(data: Dict[int, List[float]], end_frame: Optional[int]) -> Dict[int, List[float]]:
    """Truncate keypoint arrays to [0, end_frame] inclusive."""
    if end_frame is None:
        return data
    if end_frame < 0:
        return {kp: [] for kp in data.keys()}
    clipped = {}
    keep = end_frame + 1
    for kp, values in data.items():
        clipped[kp] = values[:keep]
    return clipped

def _max_frame_from_keypoints(data: Dict[int, List[float]]) -> int:
    values = data.get(KP_FRONT_FOOT)
    if values is None:
        return -1
    return len(values) - 1

def _trim_phrase_to_frames(phrase: FencingPhrase, max_frame: int) -> None:
    if max_frame < 0:
        phrase.blade_contacts = []
        phrase.simultaneous_hit_time = None
        phrase.simultaneous_hit_frame = None
        phrase.lockout_start = None
        return

    max_time = max_frame / phrase.fps if phrase.fps else None

    phrase.blade_contacts = [
        bc for bc in phrase.blade_contacts if bc.frame <= max_frame
    ]

    if max_time is not None:
        if phrase.simultaneous_hit_time is not None and phrase.simultaneous_hit_time > max_time:
            phrase.simultaneous_hit_time = None
            phrase.simultaneous_hit_frame = None
        if phrase.lockout_start is not None and phrase.lockout_start > max_time:
            phrase.lockout_start = None

    if phrase.simultaneous_hit_time is not None:
        phrase.simultaneous_hit_frame = _time_to_frame(phrase.simultaneous_hit_time, phrase.fps)

def calculate_center_of_mass(xdata: Dict, ydata: Dict, frame_idx: int) -> Tuple[float, float]:
    """Calculate center of mass from key body points (hips and shoulders)"""
    max_frame = len(xdata[KP_WEAPON_SHOULDER]) - 1
    if frame_idx > max_frame or frame_idx < 0:
        return np.nan, np.nan
    
    key_points = CENTER_OF_MASS_KEYPOINTS
    valid_x = []
    valid_y = []
    
    for kp in key_points:
        x = xdata[kp][frame_idx]
        y = ydata[kp][frame_idx]
        if not np.isnan(x) and not np.isnan(y):
            valid_x.append(x)
            valid_y.append(y)
    
    if not valid_x:
        return np.nan, np.nan
    
    return np.mean(valid_x), np.mean(valid_y)



def calculate_speed_acceleration(
    xdata: Dict,
    ydata: Dict,
    start_frame: int = 0,
    end_frame: Optional[int] = None,
) -> Tuple[float, float]:
    """Calculate average speed and acceleration for the dataset slice."""
    max_frame = len(xdata[KP_FRONT_FOOT]) - 1
    if end_frame is None or end_frame > max_frame:
        end_frame = max_frame
    start_frame = max(0, min(start_frame, end_frame))
    
    front_foot_x = []
    com_x = []
    
    for i in range(start_frame, end_frame + 1):
        ff_x = xdata[KP_FRONT_FOOT][i]
        if not np.isnan(ff_x):
            front_foot_x.append(ff_x)
        
        com_x_val, _ = calculate_center_of_mass(xdata, ydata, i)
        if not np.isnan(com_x_val):
            com_x.append(com_x_val)
    
    velocities = []
    for positions in [front_foot_x, com_x]:
        if len(positions) > 1:
            for i in range(1, len(positions)):
                vel = abs(positions[i] - positions[i-1])
                velocities.append(vel)
    
    avg_speed = np.mean(velocities) if velocities else 0
    
    if len(velocities) > 1:
        accelerations = [abs(velocities[i] - velocities[i-1]) for i in range(1, len(velocities))]
        avg_acceleration = np.mean(accelerations)
    else:
        avg_acceleration = 0
    
    return avg_speed, avg_acceleration

def detect_lunge_intervals(
    xdata: Dict[int, List[float]],
    ydata: Dict[int, List[float]],
    is_left_fencer: bool,
    fps: float = DEFAULT_FPS,
    threshold: float = LUNGE_DISTANCE_THRESHOLD,
    expansion_threshold: float = LUNGE_EXPANSION_DISTANCE_THRESHOLD,
    min_consecutive: int = LUNGE_MIN_CONSECUTIVE_FRAMES,
    hit_frames: Optional[List[int]] = None,
) -> List[LungeInterval]:
    """Detect lunge intervals based on front/back foot distance.

    Two-stage detection:
    1) Build core intervals from frames where distance > `threshold` (default 1.0).
    2) Expand interval edges using adjacent frames with distance > `expansion_threshold`
       (default 0.9), while core occupancy remains strictly > 50% of total interval length.
    """
    max_frame = len(xdata[KP_FRONT_FOOT]) - 1
    intervals: List[LungeInterval] = []
    if max_frame < 0:
        return intervals
    scaled_min_consecutive = max(min_consecutive, 1)
    scaled_overlap_start_gap = max(LUNGE_OVERLAP_START_GAP_FRAMES, 1)
    beneficial_hit_end_grace = max(LUNGE_BENEFICIAL_HIT_END_GRACE_FRAMES, 0)
    scaled_backward_threshold = _scale_per_frame_threshold(LUNGE_BACKWARD_THRESHOLD, fps)

    side = "left" if is_left_fencer else "right"
    _debug(
        f"[Lunge:{side}] start max_frame={max_frame} threshold={threshold} "
        f"expansion_threshold={expansion_threshold} "
        f"min_consecutive={scaled_min_consecutive} hit_frames={sorted(set(hit_frames or []))}"
    )

    def _three_point_angle(frame_idx: int, a: int, b: int, c: int) -> float:
        if (
            frame_idx >= len(xdata[a]) or frame_idx >= len(ydata[a])
            or frame_idx >= len(xdata[b]) or frame_idx >= len(ydata[b])
            or frame_idx >= len(xdata[c]) or frame_idx >= len(ydata[c])
        ):
            return float("nan")

        ax, ay = xdata[a][frame_idx], ydata[a][frame_idx]
        bx, by = xdata[b][frame_idx], ydata[b][frame_idx]
        cx, cy = xdata[c][frame_idx], ydata[c][frame_idx]
        if any(np.isnan([ax, ay, bx, by, cx, cy])):
            return float("nan")

        ba_x, ba_y = ax - bx, ay - by
        bc_x, bc_y = cx - bx, cy - by
        ba_norm = math.hypot(ba_x, ba_y)
        bc_norm = math.hypot(bc_x, bc_y)
        if ba_norm == 0.0 or bc_norm == 0.0:
            return float("nan")

        cos_angle = (ba_x * bc_x + ba_y * bc_y) / (ba_norm * bc_norm)
        cos_angle = max(-1.0, min(1.0, cos_angle))
        return math.degrees(math.acos(cos_angle))

    lunge_frames = []
    lunge_distances: List[float] = []
    for i in range(0, max_frame + 1):
        if i >= len(xdata[KP_BACK_FOOT]) or i >= len(xdata[KP_FRONT_FOOT]):
            _debug(f"[Lunge:{side}] frame={i} missing keypoints")
            lunge_frames.append(False)
            lunge_distances.append(np.nan)
            continue
        x_rear = xdata[KP_BACK_FOOT][i]
        y_rear = ydata[KP_BACK_FOOT][i]
        x_front = xdata[KP_FRONT_FOOT][i]
        y_front = ydata[KP_FRONT_FOOT][i]
        if any(np.isnan([x_rear, y_rear, x_front, y_front])):
            _debug(f"[Lunge:{side}] frame={i} nan keypoints")
            lunge_frames.append(False)
            lunge_distances.append(np.nan)
            continue
        dist = math.hypot(x_front - x_rear, y_front - y_rear)
        angle_13_11_12 = _three_point_angle(i, 13, 11, 12)
        angle_11_12_14 = _three_point_angle(i, 11, 12, 14)
        lunge_distances.append(dist)
        is_lunge = dist > threshold
        angle_13_11_12_str = f"{angle_13_11_12:.2f}" if np.isfinite(angle_13_11_12) else "nan"
        angle_11_12_14_str = f"{angle_11_12_14:.2f}" if np.isfinite(angle_11_12_14) else "nan"
        _debug(
            f"[Lunge:{side}] frame={i} dist={dist:.3f} lunge={is_lunge} "
            f"angle_13_11_12={angle_13_11_12_str}deg "
            f"angle_11_12_14={angle_11_12_14_str}deg"
        )
        lunge_frames.append(is_lunge)

    expected_direction = 1 if is_left_fencer else -1
    start = None
    core_ranges: List[Tuple[int, int]] = []
    for i, is_lunge in enumerate(lunge_frames):
        if is_lunge and start is None:
            start = i
            _debug(f"[Lunge:{side}] core interval start frame={start}")
        elif not is_lunge and start is not None:
            end = i - 1
            if end - start + 1 >= scaled_min_consecutive:
                _debug(f"[Lunge:{side}] core interval candidate {start}-{end} accepted length={end - start + 1}")
                core_ranges.append((start, end))
            else:
                _debug(f"[Lunge:{side}] core interval candidate {start}-{end} rejected length={end - start + 1}")
            start = None

    if start is not None:
        end = len(lunge_frames) - 1
        if end - start + 1 >= scaled_min_consecutive:
            _debug(f"[Lunge:{side}] core interval candidate {start}-{end} accepted length={end - start + 1}")
            core_ranges.append((start, end))
        else:
            _debug(f"[Lunge:{side}] core interval candidate {start}-{end} rejected length={end - start + 1}")

    for core_start, core_end in core_ranges:
        expanded_start = core_start
        expanded_end = core_end
        core_len = core_end - core_start + 1

        while True:
            changed = False

            left_candidate = expanded_start - 1
            if left_candidate >= 0:
                left_dist = lunge_distances[left_candidate]
                if not np.isnan(left_dist) and left_dist > expansion_threshold:
                    proposed_len = expanded_end - left_candidate + 1
                    core_ratio = core_len / proposed_len if proposed_len > 0 else 0.0
                    if core_ratio > 0.5:
                        expanded_start = left_candidate
                        changed = True
                        _debug(
                            f"[Lunge:{side}] expand left -> frame {left_candidate} dist={left_dist:.3f} "
                            f"core_ratio={core_ratio:.3f}"
                        )
                    else:
                        _debug(
                            f"[Lunge:{side}] stop left expansion at frame {left_candidate}: "
                            f"core_ratio={core_ratio:.3f} (<=0.5)"
                        )

            right_candidate = expanded_end + 1
            if right_candidate <= max_frame:
                right_dist = lunge_distances[right_candidate]
                if not np.isnan(right_dist) and right_dist > expansion_threshold:
                    proposed_len = right_candidate - expanded_start + 1
                    core_ratio = core_len / proposed_len if proposed_len > 0 else 0.0
                    if core_ratio > 0.5:
                        expanded_end = right_candidate
                        changed = True
                        _debug(
                            f"[Lunge:{side}] expand right -> frame {right_candidate} dist={right_dist:.3f} "
                            f"core_ratio={core_ratio:.3f}"
                        )
                    else:
                        _debug(
                            f"[Lunge:{side}] stop right expansion at frame {right_candidate}: "
                            f"core_ratio={core_ratio:.3f} (<=0.5)"
                        )

            if not changed:
                break

        if expanded_start != core_start or expanded_end != core_end:
            _debug(
                f"[Lunge:{side}] interval expanded {core_start}-{core_end} -> "
                f"{expanded_start}-{expanded_end} core_len={core_len} total_len={expanded_end - expanded_start + 1}"
            )
        intervals.append(
            LungeInterval(
                start_frame=expanded_start,
                end_frame=expanded_end,
                start_time=expanded_start / fps,
                end_time=expanded_end / fps,
            )
        )

    valid_intervals = []
    hit_frame_set = set(hit_frames or [])
    for interval in intervals:
        qualifying_angle_frames: List[int] = []
        strong_distance_frames: List[int] = []
        for f_idx in range(interval.start_frame, interval.end_frame + 1):
            angle_13_11_12 = _three_point_angle(f_idx, 13, 11, 12)
            dist = lunge_distances[f_idx] if 0 <= f_idx < len(lunge_distances) else float("nan")
            angle_str = f"{angle_13_11_12:.2f}" if np.isfinite(angle_13_11_12) else "nan"
            dist_str = f"{dist:.3f}" if np.isfinite(dist) else "nan"
            qualifies = np.isfinite(angle_13_11_12) and angle_13_11_12 > LUNGE_HIP_ANGLE_MIN_DEG
            strong_distance = np.isfinite(dist) and dist > LUNGE_HIP_ANGLE_DISTANCE_OVERRIDE
            _debug(
                f"[Lunge:{side}] hip-angle-check frame={f_idx} "
                f"dist={dist_str} "
                f"angle_13_11_12={angle_str}deg qualifies={qualifies} "
                f"threshold={LUNGE_HIP_ANGLE_MIN_DEG:.1f}"
            )
            if qualifies:
                qualifying_angle_frames.append(f_idx)
            if strong_distance:
                strong_distance_frames.append(f_idx)

        hip_angle_override = (
            len(strong_distance_frames) >= LUNGE_HIP_ANGLE_DISTANCE_OVERRIDE_MIN_FRAMES
        )
        if len(qualifying_angle_frames) < LUNGE_HIP_ANGLE_MIN_FRAMES and not hip_angle_override:
            _debug(
                f"[Lunge:{side}] rejected interval {interval.start_frame}-{interval.end_frame}: "
                f"hip-angle frames={qualifying_angle_frames} count={len(qualifying_angle_frames)} "
                f"minimum={LUNGE_HIP_ANGLE_MIN_FRAMES} threshold={LUNGE_HIP_ANGLE_MIN_DEG:.1f} "
                f"strong-distance frames={strong_distance_frames} "
                f"override_threshold={LUNGE_HIP_ANGLE_DISTANCE_OVERRIDE:.1f} "
                f"override_min={LUNGE_HIP_ANGLE_DISTANCE_OVERRIDE_MIN_FRAMES}"
            )
            continue

        if hip_angle_override and len(qualifying_angle_frames) < LUNGE_HIP_ANGLE_MIN_FRAMES:
            _debug(
                f"[Lunge:{side}] accepted interval {interval.start_frame}-{interval.end_frame} via "
                "strong-distance override: "
                f"strong-distance frames={strong_distance_frames} "
                f"count={len(strong_distance_frames)} "
                f"override_threshold={LUNGE_HIP_ANGLE_DISTANCE_OVERRIDE:.1f} "
                f"override_min={LUNGE_HIP_ANGLE_DISTANCE_OVERRIDE_MIN_FRAMES}"
            )
        else:
            _debug(
                f"[Lunge:{side}] accepted hip-angle filter for interval {interval.start_frame}-{interval.end_frame}: "
                f"frames={qualifying_angle_frames} count={len(qualifying_angle_frames)} "
                f"minimum={LUNGE_HIP_ANGLE_MIN_FRAMES} threshold={LUNGE_HIP_ANGLE_MIN_DEG:.1f}"
            )

        overlapping_hits = sorted(
            hf for hf in hit_frame_set if interval.start_frame <= hf <= interval.end_frame
        )
        beneficial_overlap_hits = sorted(
            hf for hf in hit_frame_set
            if interval.start_frame <= hf <= (interval.end_frame + beneficial_hit_end_grace)
        )
        interval.overlap_hit_frames = beneficial_overlap_hits
        interval_classification = "penalizing"

        pause_threshold = _scale_per_frame_threshold(PAUSE_VELOCITY_THRESHOLD, fps)
        pause_like_runs: List[List[int]] = []
        current_pause_like_run: List[int] = []
        for f_idx in range(interval.start_frame + 1, interval.end_frame + 1):
            if f_idx >= len(xdata[KP_FRONT_FOOT]):
                break
            curr_ff = xdata[KP_FRONT_FOOT][f_idx]
            prev_ff = xdata[KP_FRONT_FOOT][f_idx - 1]
            if np.isnan(curr_ff) or np.isnan(prev_ff):
                ff_vel_signed = 0.0
                reason = "nan_to_zero"
            else:
                ff_vel_signed = (curr_ff - prev_ff) * expected_direction
                reason = "raw"
            pause_like = abs(ff_vel_signed) < pause_threshold or ff_vel_signed < 0
            _debug(
                f"[Lunge:{side}] pause-check frame={f_idx} front_foot_pause_like={pause_like} "
                f"signed_vel={ff_vel_signed:.4f} threshold={pause_threshold:.4f} source={reason}"
            )
            if pause_like:
                current_pause_like_run.append(f_idx)
            elif current_pause_like_run:
                pause_like_runs.append(current_pause_like_run.copy())
                current_pause_like_run = []
        if current_pause_like_run:
            pause_like_runs.append(current_pause_like_run.copy())

        late_pause_like_runs = [
            run for run in pause_like_runs
            if len(run) >= LUNGE_END_PAUSE_MIN_FRAMES
            and (interval.end_frame - run[-1]) <= LUNGE_END_PAUSE_START_WINDOW_FRAMES
        ]
        y_filtered_late_pause_like_runs: List[List[int]] = []
        for run in late_pause_like_runs:
            y_coords = []
            for f_idx in run:
                if f_idx < len(ydata[KP_FRONT_FOOT]):
                    y = ydata[KP_FRONT_FOOT][f_idx]
                    if not np.isnan(y):
                        y_coords.append(y)

            if len(y_coords) > 1:
                y_var = np.var(y_coords)
                _debug(
                    f"[Lunge:{side}] pause-check run={run[0]}-{run[-1]} "
                    f"y_var={y_var:.6f} threshold={PAUSE_Y_VARIANCE_THRESHOLD:.6f}"
                )
                if y_var >= PAUSE_Y_VARIANCE_THRESHOLD:
                    _debug(
                        f"[Lunge:{side}] pause-check run={run[0]}-{run[-1]} "
                        "rejected high_y_variance"
                    )
                    continue
            else:
                _debug(
                    f"[Lunge:{side}] pause-check run={run[0]}-{run[-1]} "
                    "y_var skipped (insufficient non-NaN y samples)"
                )

            y_filtered_late_pause_like_runs.append(run)

        if beneficial_overlap_hits:
            late_overlap_hits = [
                hf for hf in beneficial_overlap_hits
                if (hf - interval.start_frame) >= scaled_overlap_start_gap
            ]
            if y_filtered_late_pause_like_runs:
                chosen_run = max(y_filtered_late_pause_like_runs, key=lambda run: (run[-1], len(run)))
                _debug(
                    f"[Lunge:{side}] interval {interval.start_frame}-{interval.end_frame} "
                    f"kept penalizing despite hit overlap: front-foot pause-like run "
                    f"{chosen_run} count={len(chosen_run)} "
                    f"limit={LUNGE_END_PAUSE_MIN_FRAMES} "
                    f"end_gap={interval.end_frame - chosen_run[-1]} "
                    f"window={LUNGE_END_PAUSE_START_WINDOW_FRAMES} "
                    f"beneficial_hit_frames={beneficial_overlap_hits} "
                    f"strict_overlap_hits={overlapping_hits} "
                    f"y_var_threshold={PAUSE_Y_VARIANCE_THRESHOLD:.6f}"
                )
            elif not late_overlap_hits:
                interval_classification = "beneficial_overlap"
                _debug(
                    f"[Lunge:{side}] interval {interval.start_frame}-{interval.end_frame} "
                    f"classified beneficial_overlap hit_overlap={beneficial_overlap_hits} "
                    f"strict_overlap_hits={overlapping_hits} "
                    f"end_grace={beneficial_hit_end_grace} "
                    f"(all gaps < {scaled_overlap_start_gap})"
                )
            else:
                _debug(
                    f"[Lunge:{side}] interval {interval.start_frame}-{interval.end_frame} "
                    f"hit_overlap={beneficial_overlap_hits} "
                    f"strict_overlap_hits={overlapping_hits} but kept: "
                    f"late overlap gaps>={scaled_overlap_start_gap} "
                    f"hits={late_overlap_hits}"
                )
        has_backward = False
        backward_frames = 0
        interval_len = max(1, interval.end_frame - interval.start_frame + 1)
        for f_idx in range(interval.start_frame + 1, interval.end_frame + 1):
            if f_idx >= len(xdata[KP_BACK_FOOT]):
                continue
            curr_bf = xdata[KP_BACK_FOOT][f_idx]
            prev_bf = xdata[KP_BACK_FOOT][f_idx - 1]
            if np.isnan(curr_bf) or np.isnan(prev_bf):
                continue
            bf_vel = (curr_bf - prev_bf) * expected_direction
            if bf_vel < -scaled_backward_threshold:
                backward_frames += 1
                _debug(
                    f"[Lunge:{side}] interval {interval.start_frame}-{interval.end_frame} "
                    f"backward frame={f_idx} bf_vel={bf_vel:.4f} "
                    f"threshold={-scaled_backward_threshold:.4f} "
                    f"count={backward_frames}/{interval_len}"
                )
        backward_ratio = backward_frames / interval_len
        if backward_ratio > LUNGE_MAX_BACKWARD_FRAME_RATIO:
            has_backward = True
            _debug(
                f"[Lunge:{side}] interval {interval.start_frame}-{interval.end_frame} "
                f"rejected backward_ratio={backward_ratio:.3f} "
                f"limit={LUNGE_MAX_BACKWARD_FRAME_RATIO:.3f} "
                f"count={backward_frames}/{interval_len}"
            )
        else:
            _debug(
                f"[Lunge:{side}] interval {interval.start_frame}-{interval.end_frame} "
                f"backward_ratio={backward_ratio:.3f} "
                f"limit={LUNGE_MAX_BACKWARD_FRAME_RATIO:.3f} "
                f"count={backward_frames}/{interval_len}"
            )
        if not has_backward:
            interval.classification = interval_classification
            interval.is_penalizing = interval_classification == "penalizing"
            _debug(
                f"[Lunge:{side}] interval {interval.start_frame}-{interval.end_frame} accepted "
                f"classification={interval.classification} penalizing={interval.is_penalizing}"
            )
            valid_intervals.append(interval)
    return valid_intervals

# ============================================================================
# Signal detection
# ============================================================================

def detect_pause_retreat_intervals(xdata: Dict, ydata: Dict, is_left_fencer: bool, 
                                   fps: float = DEFAULT_FPS) -> List[PauseInterval]:
    """
    Detect pause/retreat intervals for a fencer using simplified logic:
    - Use only front foot (keypoint 16)
    - No smoothing
    - Raw velocity check
    - Y-variance filter on front foot
    - Back foot (keypoint 15) movement filter
    - Assumes entire dataset range
    """
    intervals = []
    side = "left" if is_left_fencer else "right"
    
    start_frame = PAUSE_INITIAL_START_FRAME
    end_frame = len(xdata[KP_FRONT_FOOT]) - 1
    
    if start_frame >= end_frame:
        _debug(
            f"[PauseDetect:{side}] rejected window start={start_frame} end={end_frame} "
            "(insufficient frames)"
        )
        return intervals
    
    # Get front foot positions
    front_foot_x = [xdata[KP_FRONT_FOOT][i] for i in range(start_frame, end_frame + 1)]
    front_foot_y = [ydata[KP_FRONT_FOOT][i] for i in range(start_frame, end_frame + 1)]
    
    # Calculate raw velocities of front foot
    expected_direction = 1 if is_left_fencer else -1

    pause_threshold = _scale_per_frame_threshold(PAUSE_VELOCITY_THRESHOLD, fps)
    retreat_threshold = _scale_per_frame_threshold(PAUSE_RETREAT_THRESHOLD, fps)
    min_pause_frames = max(PAUSE_MIN_FRAMES, 1)
    y_variance_threshold = PAUSE_Y_VARIANCE_THRESHOLD
    back_foot_threshold = _scale_per_frame_threshold(PAUSE_BACK_FOOT_FORWARD_THRESHOLD, fps)

    _debug(
        f"[PauseDetect:{side}] start window={start_frame}-{end_frame} "
        f"pause_threshold={pause_threshold:.4f} retreat_threshold={retreat_threshold:.4f} "
        f"min_pause_frames={min_pause_frames} y_var_threshold={y_variance_threshold:.6f} "
        f"back_foot_threshold={back_foot_threshold:.4f} expected_direction={expected_direction:+d}"
    )

    velocities = []
    for i in range(1, len(front_foot_x)):
        frame_idx = start_frame + i
        if not np.isnan(front_foot_x[i]) and not np.isnan(front_foot_x[i-1]):
            vel = front_foot_x[i] - front_foot_x[i-1]
            _debug(f"[PauseDetect:{side}] frame={frame_idx} vel={vel:.4f} source=raw")
        else:
            vel = 0
            _debug(f"[PauseDetect:{side}] frame={frame_idx} vel=0.0000 source=nan_to_zero")
        velocities.append(vel)

    # print (velocities) # Removed raw velocity print to reduce noise
    
    pause_frames: List[Tuple[List[int], bool]] = []
    current_pause_frames = []

    def process_and_filter_interval(frames):
        if not frames:
            return
        if len(frames) < min_pause_frames:
            _debug(
                f"[PauseDetect:{side}] interval {frames[0]}-{frames[-1]} "
                f"rejected short length={len(frames)} (<{min_pause_frames})"
            )
            return

        # 1. Determine if Pause or Retreat
        # Get velocities for these frames
        interval_vels = []
        for f_idx in frames:
            v_idx = f_idx - start_frame - 1
            if 0 <= v_idx < len(velocities):
                interval_vels.append(abs(velocities[v_idx]))
        
        if not interval_vels:
            _debug(
                f"[PauseDetect:{side}] interval {frames[0]}-{frames[-1]} rejected "
                "no velocity samples"
            )
            return

        avg_abs_vel = np.mean(interval_vels)
        _debug(f"[PauseDetect:{side}] interval {frames[0]}-{frames[-1]} avg_abs_vel={avg_abs_vel:.4f}")
        
        # If average velocity is high, it's a retreat (valid break of ROW)
        # We skip variance and back foot checks for retreats
        if avg_abs_vel > retreat_threshold:
            _debug(f"[PauseDetect:{side}] classified=RETREAT threshold={retreat_threshold:.4f}")
            pause_frames.append((frames, True))
            return

        _debug(
            f"[PauseDetect:{side}] classified=PAUSE threshold={retreat_threshold:.4f} "
            "applying back-foot and y-variance filters"
        )
        
        # Filter: Back Foot Movement (Keypoint 15)
        # Identify valid frames where back foot velocity is within threshold
        valid_frames = []
        for f_idx in frames:
            # Calculate bf_vel for this frame
            bf_vel = 0.0
            if f_idx > 0 and f_idx < len(xdata[KP_BACK_FOOT]):
                curr_bf = xdata[KP_BACK_FOOT][f_idx]
                prev_bf = xdata[KP_BACK_FOOT][f_idx-1]
                if not np.isnan(curr_bf) and not np.isnan(prev_bf):
                    bf_vel = (curr_bf - prev_bf) * expected_direction
                else:
                    _debug(
                        f"[PauseDetect:{side}] frame={f_idx} back-foot nan -> bf_vel=0.0000"
                    )
            else:
                _debug(
                    f"[PauseDetect:{side}] frame={f_idx} back-foot unavailable -> bf_vel=0.0000"
                )
            
            # Check threshold (max limit for forward movement)
            # If bf_vel is high positive (moving forward), we reject the frame.
            # If bf_vel is low or negative (retreating), we keep it.
            if bf_vel < back_foot_threshold:
                valid_frames.append(f_idx)
                _debug(
                    f"[PauseDetect:{side}] keep frame={f_idx} back_foot_vel={bf_vel:.4f} "
                    f"threshold={back_foot_threshold:.4f}"
                )
            else:
                _debug(
                    f"[PauseDetect:{side}] reject frame={f_idx} back_foot_vel={bf_vel:.4f} "
                    f"threshold={back_foot_threshold:.4f}"
                )

        # Split valid_frames into continuous segments
        if not valid_frames:
            _debug(f"[PauseDetect:{side}] interval {frames[0]}-{frames[-1]} rejected no valid back-foot frames")
            return

        segments = []
        if valid_frames:
            current_segment = [valid_frames[0]]
            for i in range(1, len(valid_frames)):
                if valid_frames[i] == valid_frames[i-1] + 1:
                    current_segment.append(valid_frames[i])
                else:
                    segments.append(current_segment)
                    current_segment = [valid_frames[i]]
            segments.append(current_segment)
        _debug(f"[PauseDetect:{side}] interval {frames[0]}-{frames[-1]} segments={segments}")
        
        # Check each segment
        for segment in segments:
            # Check Length
            if len(segment) < min_pause_frames:
                _debug(
                    f"[PauseDetect:{side}] segment {segment[0]}-{segment[-1]} "
                    f"rejected short length={len(segment)} (<{min_pause_frames})"
                )
                continue
            
            # Check Y-Variance
            y_coords = []
            for f_idx in segment:
                idx = f_idx - start_frame
                if 0 <= idx < len(front_foot_y):
                    y = front_foot_y[idx]
                    if not np.isnan(y):
                        y_coords.append(y)
            
            if len(y_coords) > 1:
                y_var = np.var(y_coords)
                _debug(
                    f"[PauseDetect:{side}] segment {segment[0]}-{segment[-1]} "
                    f"y_var={y_var:.6f} threshold={y_variance_threshold:.6f}"
                )
                if y_var >= y_variance_threshold:
                    _debug(f"[PauseDetect:{side}] reject segment=high_y_variance")
                    continue # Failed Y-var check
            else:
                _debug(
                    f"[PauseDetect:{side}] segment {segment[0]}-{segment[-1]} "
                    "y_var skipped (insufficient non-NaN y samples)"
                )
            
            # Passed checks
            _debug(f"[PauseDetect:{side}] accept segment {segment[0]}-{segment[-1]}")
            pause_frames.append((segment, False))
    
    for i, vel in enumerate(velocities):
        frame_idx = start_frame + i + 1
        
        # Check if paused (near zero velocity) or retreating (opposite direction)
        is_paused = (abs(vel) < pause_threshold) or (vel * expected_direction < 0)
        _debug(
            f"[PauseDetect:{side}] frame={frame_idx} classify="
            f"{'pause_candidate' if is_paused else 'active'} "
            f"vel={vel:.4f} abs_vel={abs(vel):.4f}"
        )
        
        if is_paused:
            if not current_pause_frames:
                _debug(f"[PauseDetect:{side}] candidate interval start frame={frame_idx}")
            current_pause_frames.append(frame_idx)
        else:
            if current_pause_frames:
                _debug(
                    f"[PauseDetect:{side}] candidate interval end frame={current_pause_frames[-1]} "
                    f"length={len(current_pause_frames)} -> evaluate"
                )
            process_and_filter_interval(current_pause_frames)
            current_pause_frames = []
    
    # Handle end of loop
    if current_pause_frames:
        _debug(
            f"[PauseDetect:{side}] candidate interval end frame={current_pause_frames[-1]} "
            f"length={len(current_pause_frames)} -> evaluate (end_of_window)"
        )
    process_and_filter_interval(current_pause_frames)
    
    for pf, is_retreat in pause_frames:
        intervals.append(PauseInterval(
            start_frame=pf[0],
            end_frame=pf[-1],
            start_time=pf[0] / fps,
            end_time=pf[-1] / fps,
            duration=(pf[-1] - pf[0]) / fps,
            is_retreat=is_retreat
        ))

    if intervals:
        for interval in intervals:
            label = "RETREAT" if interval.is_retreat else "PAUSE"
            _debug(
                f"[PauseDetect:{side}] final interval {interval.start_frame}-{interval.end_frame} "
                f"type={label} duration={interval.duration:.3f}s"
            )
    else:
        _debug(f"[PauseDetect:{side}] final intervals none")
    
    return intervals


def _extract_slow_starts(
    pauses: List[PauseInterval],
    fps: float,
) -> Tuple[List[PauseInterval], Optional[PauseInterval]]:
    """Reclassify opening short pauses as slow starts."""
    if not pauses:
        return pauses, None

    max_frames = int(fps * SLOW_START_MAX_DURATION_SECONDS)
    slow_start_start_frame = SLOW_START_MAX_START_FRAME
    remaining: List[PauseInterval] = []
    slow_start: Optional[PauseInterval] = None

    for interval in pauses:
        length_frames = interval.end_frame - interval.start_frame + 1
        if interval.start_frame <= slow_start_start_frame and length_frames < max_frames:
            if slow_start is None or interval.end_frame > slow_start.end_frame:
                slow_start = interval
            continue
        remaining.append(interval)

    return remaining, slow_start

@dataclass
class ArmExtensionInterval:
    """Represents an arm extension interval"""
    start_frame: int
    end_frame: int
    start_time: float
    end_time: float
    duration: float
    avg_distance: float
    avg_angle: float
    effective_start_frame: int
    effective_start_time: float
    near_hit: bool
    is_harmful_overlong: bool
    is_harmful_early_end: bool

def detect_arm_extension(xdata: Dict, ydata: Dict, is_left_fencer: bool,
                        fps: float = DEFAULT_FPS,
                        hit_frame: Optional[int] = None,
                        start_frame: int = 0,
                        end_frame: Optional[int] = None,
                        debug: bool = False) -> List[ArmExtensionInterval]:
    """Detect arm extension intervals based on horizontal reach and elbow straightening."""
    intervals: List[ArmExtensionInterval] = []
    debug_side = 'left' if is_left_fencer else 'right'

    hand_kp = KP_WEAPON_WRIST
    hip_kp = KP_FRONT_HIP
    shoulder_kp = KP_WEAPON_SHOULDER
    elbow_kp = KP_WEAPON_ELBOW

    distance_threshold = ARM_EXTENSION_X_DISTANCE_THRESHOLD
    min_extension_frames = max(ARM_EXTENSION_MIN_FRAMES, 1)
    max_hit_frame_gap = max(ARM_EXTENSION_MAX_HIT_GAP_FRAMES, 1)
    straight_angle_threshold = ARM_EXTENSION_STRAIGHT_ANGLE_DEG
    harmful_body_angle_threshold = ARM_EXTENSION_HARMFUL_BODY_ANGLE_DEG

    total_frames = len(xdata[hand_kp])
    if end_frame is None or end_frame >= total_frames:
        end_frame = total_frames - 1
    start_frame = max(0, min(start_frame, end_frame))
    if end_frame - start_frame < 1:
        if debug:
            _debug(
                f"[ArmExt:{debug_side}] rejected window start={start_frame} end={end_frame} "
                "(insufficient frames)"
            )
        return intervals
    if debug:
        _debug(
            f"[ArmExt:{debug_side}] start window={start_frame}-{end_frame} "
            "reach_metric=abs(wrist_x-front_hip_x) "
            f"x_distance_threshold={distance_threshold} min_extension_frames={min_extension_frames} "
            f"straight_angle_threshold={straight_angle_threshold} max_hit_gap={max_hit_frame_gap} "
            f"harmful_body_angle_threshold={harmful_body_angle_threshold}"
        )

    distance_by_frame: Dict[int, float] = {}
    reach_mask: List[bool] = []
    window_frames = list(range(start_frame, end_frame + 1))

    for frame_idx in window_frames:
        hand_x, hand_y = xdata[hand_kp][frame_idx], ydata[hand_kp][frame_idx]
        hip_x, hip_y = xdata[hip_kp][frame_idx], ydata[hip_kp][frame_idx]

        valid_distance = not (
            np.isnan(hand_x) or np.isnan(hand_y) or np.isnan(hip_x) or np.isnan(hip_y)
        )
        dist = abs(hand_x - hip_x) if valid_distance else 0.0
        distance_by_frame[frame_idx] = dist
        reached = valid_distance and dist >= distance_threshold
        reach_mask.append(reached)
        if debug:
            if not valid_distance:
                _debug(f"[ArmExt:{debug_side}] frame={frame_idx} rejected x_distance=nan_keypoints")
            else:
                _debug(
                    f"[ArmExt:{debug_side}] frame={frame_idx} wrist_x={hand_x:.3f} hip_x={hip_x:.3f} "
                    f"x_distance={dist:.3f} "
                    f"reached={reached}"
                )

    extension_frames: List[List[int]] = []
    current_frames: List[int] = []
    for idx, extended in enumerate(reach_mask):
        frame_idx = window_frames[idx]
        if extended:
            current_frames.append(frame_idx)
        else:
            if current_frames and len(current_frames) >= min_extension_frames:
                extension_frames.append(current_frames.copy())
                if debug:
                    _debug(
                        f"[ArmExt:{debug_side}] candidate interval {current_frames[0]}-{current_frames[-1]} "
                        f"accepted by reach length={len(current_frames)}"
                    )
            elif current_frames and debug:
                _debug(
                    f"[ArmExt:{debug_side}] candidate interval {current_frames[0]}-{current_frames[-1]} "
                    f"rejected short length={len(current_frames)} (<{min_extension_frames})"
                )
            current_frames = []
    if len(current_frames) >= min_extension_frames:
        extension_frames.append(current_frames)
        if debug:
            _debug(
                f"[ArmExt:{debug_side}] candidate interval {current_frames[0]}-{current_frames[-1]} "
                f"accepted by reach length={len(current_frames)}"
            )
    elif current_frames and debug:
        _debug(
            f"[ArmExt:{debug_side}] candidate interval {current_frames[0]}-{current_frames[-1]} "
            f"rejected short length={len(current_frames)} (<{min_extension_frames})"
        )

    if not extension_frames:
        if debug:
            _debug(f"[ArmExt:{debug_side}] rejected all candidates: no reach interval passed length filter")
        return intervals

    if hit_frame is None:
        hit_frame = end_frame

    def _elbow_angle_components(frame_idx: int) -> Optional[Tuple[float, float, float, float, str]]:
        shoulder_x, shoulder_y = xdata[shoulder_kp][frame_idx], ydata[shoulder_kp][frame_idx]
        elbow_x, elbow_y = xdata[elbow_kp][frame_idx], ydata[elbow_kp][frame_idx]
        hand_x, hand_y = xdata[hand_kp][frame_idx], ydata[hand_kp][frame_idx]
        if (
            np.isnan(shoulder_x) or np.isnan(shoulder_y)
            or np.isnan(elbow_x) or np.isnan(elbow_y)
            or np.isnan(hand_x) or np.isnan(hand_y)
        ):
            return None
        upper = np.array([shoulder_x - elbow_x, shoulder_y - elbow_y])
        lower = np.array([hand_x - elbow_x, hand_y - elbow_y])
        if np.linalg.norm(upper) == 0 or np.linalg.norm(lower) == 0:
            return None
        dot = float(np.dot(upper, lower))
        cross_z = float((upper[0] * lower[1]) - (upper[1] * lower[0]))
        cos_theta = dot / (np.linalg.norm(upper) * np.linalg.norm(lower))
        cos_theta = max(-1.0, min(1.0, float(cos_theta)))
        inner = math.degrees(math.acos(cos_theta))
        reflex = 360.0 - inner
        signed = math.degrees(math.atan2(cross_z, dot))
        directed_ccw = signed if signed >= 0.0 else signed + 360.0

        # Select elbow angle based on elbow position relative to shoulder-wrist line.
        # - elbow above line  -> reflex
        # - elbow below line  -> inner
        # - ambiguous/vertical -> inner (safe default)
        side = "on_line"
        dx_sw = hand_x - shoulder_x
        if abs(dx_sw) > 1e-9:
            y_on_line = shoulder_y + ((hand_y - shoulder_y) * (elbow_x - shoulder_x) / dx_sw)
            if elbow_y < y_on_line:
                side = "above"
            elif elbow_y > y_on_line:
                side = "below"
        else:
            side = "vertical"

        selected = reflex if side == "above" else inner
        return inner, reflex, directed_ccw, selected, side

    def _arm_body_angle(frame_idx: int) -> Optional[float]:
        shoulder_x, shoulder_y = xdata[shoulder_kp][frame_idx], ydata[shoulder_kp][frame_idx]
        elbow_x, elbow_y = xdata[elbow_kp][frame_idx], ydata[elbow_kp][frame_idx]
        hip_x, hip_y = xdata[hip_kp][frame_idx], ydata[hip_kp][frame_idx]
        if (
            np.isnan(shoulder_x) or np.isnan(shoulder_y)
            or np.isnan(elbow_x) or np.isnan(elbow_y)
            or np.isnan(hip_x) or np.isnan(hip_y)
        ):
            return None
        arm_vec = np.array([elbow_x - shoulder_x, elbow_y - shoulder_y], dtype=float)
        body_vec = np.array([hip_x - shoulder_x, hip_y - shoulder_y], dtype=float)
        arm_norm = np.linalg.norm(arm_vec)
        body_norm = np.linalg.norm(body_vec)
        if arm_norm == 0.0 or body_norm == 0.0:
            return None
        cos_theta = float(np.dot(arm_vec, body_vec) / (arm_norm * body_norm))
        cos_theta = max(-1.0, min(1.0, cos_theta))
        return math.degrees(math.acos(cos_theta))

    harmful_early_end_gap = max(ARM_EXTENSION_HARMFUL_EARLY_END_FRAMES, 1)

    for frames in extension_frames:
        if not frames:
            if debug:
                _debug(f"[ArmExt:{debug_side}] rejected interval: empty frame list")
            continue

        interval_start = frames[0]
        interval_end = frames[-1]
        if debug:
            _debug(
                f"[ArmExt:{debug_side}] evaluating interval {interval_start}-{interval_end} "
                f"hit_frame={hit_frame}"
            )

        near_hit = True
        hit_gap = hit_frame - interval_end
        if hit_gap > max_hit_frame_gap:
            near_hit = False
            if debug:
                _debug(
                    f"[ArmExt:{debug_side}] interval {interval_start}-{interval_end} "
                    f"marked near_hit=False gap={hit_gap} (>{max_hit_frame_gap})"
                )
        elif debug:
            _debug(
                f"[ArmExt:{debug_side}] interval {interval_start}-{interval_end} "
                f"marked near_hit=True gap={hit_gap}"
            )

        effective_start = None
        angle_samples: List[float] = []
        arm_body_angle_samples: List[float] = []
        for frame in frames:
            components = _elbow_angle_components(frame)
            if components is None:
                if debug:
                    _debug(
                        f"[ArmExt:{debug_side}] interval {interval_start}-{interval_end} "
                        f"frame={frame} rejected elbow=nan_keypoints"
                    )
                continue
            _, _, _, angle, elbow_position = components
            angle_samples.append(angle)
            arm_body_angle = _arm_body_angle(frame)
            if arm_body_angle is not None:
                arm_body_angle_samples.append(arm_body_angle)
            if debug:
                body_angle_msg = (
                    f"{arm_body_angle:.2f}deg" if arm_body_angle is not None else "nan_keypoints"
                )
                _debug(
                    f"[ArmExt:{debug_side}] interval {interval_start}-{interval_end} "
                    f"frame={frame} elbow_angle_selected={angle:.2f}deg "
                    f"elbow_position={elbow_position} "
                    f"arm_body_angle={body_angle_msg} "
                    f"straight={angle >= straight_angle_threshold}"
                )
            if effective_start is None and angle >= straight_angle_threshold:
                effective_start = frame

        if effective_start is None:
            if debug:
                _debug(
                    f"[ArmExt:{debug_side}] rejected interval {interval_start}-{interval_end}: "
                    f"no elbow angle >= {straight_angle_threshold}deg"
                )
            continue

        interval_len = len(frames)
        harmful_overlong = interval_len > ARM_EXTENSION_HARMFUL_MAX_FRAMES
        harmful_early_end = hit_gap >= harmful_early_end_gap
        harmful_candidate = harmful_overlong or harmful_early_end
        harmful_reasons: List[str] = []
        if harmful_overlong:
            harmful_reasons.append("overlong")
        if harmful_early_end:
            harmful_reasons.append("early_end")
        avg_dist = float(np.mean([distance_by_frame[f] for f in frames])) if frames else 0.0
        avg_angle = float(np.mean(angle_samples)) if angle_samples else 0.0
        max_arm_body_angle = max(arm_body_angle_samples) if arm_body_angle_samples else None

        if harmful_candidate:
            harmful_body_angle_ok = (
                max_arm_body_angle is not None and max_arm_body_angle >= harmful_body_angle_threshold
            )
            if debug:
                angle_repr = (
                    f"{max_arm_body_angle:.2f}deg" if max_arm_body_angle is not None else "nan_keypoints"
                )
                _debug(
                    f"[ArmExt:{debug_side}] harmful candidate {interval_start}-{interval_end} "
                    f"reasons={','.join(harmful_reasons)} "
                    f"body_angle_check={angle_repr} "
                    f"threshold={harmful_body_angle_threshold} "
                    f"passes={harmful_body_angle_ok}"
                )
            if not harmful_body_angle_ok:
                if debug:
                    _debug(
                        f"[ArmExt:{debug_side}] discarded interval {interval_start}-{interval_end}: "
                        f"triggered harmful condition(s) {','.join(harmful_reasons)} "
                        "but failed arm-body angle gate"
                    )
                continue

        if debug:
            harmful_status = "none"
            if harmful_reasons:
                harmful_status = ",".join(harmful_reasons)
            _debug(
                f"[ArmExt:{debug_side}] reach interval {interval_start}-{interval_end} (len={interval_len}) "
                f"avg x_distance={avg_dist:.3f} (threshold {distance_threshold})"
            )
            _debug(
                f"[ArmExt:{debug_side}] accepted interval {interval_start}-{interval_end} "
                f"effective_start={effective_start} "
                f"angle_threshold={straight_angle_threshold}deg "
                f"max_arm_body_angle="
                f"{f'{max_arm_body_angle:.2f}deg' if max_arm_body_angle is not None else 'nan_keypoints'} "
                f"near_hit={near_hit} "
                f"harmful_status={harmful_status} "
                f"harmful_overlong={harmful_overlong} "
                f"harmful_early_end={harmful_early_end} "
                f"length={interval_len} "
                f"overlong_limit={ARM_EXTENSION_HARMFUL_MAX_FRAMES} "
                f"early_end_gap={hit_gap} "
                f"early_end_limit={harmful_early_end_gap}"
            )

        intervals.append(ArmExtensionInterval(
            start_frame=interval_start,
            end_frame=interval_end,
            start_time=interval_start / fps,
            end_time=interval_end / fps,
            duration=(interval_end - interval_start) / fps,
            avg_distance=avg_dist,
            avg_angle=avg_angle,
            effective_start_frame=effective_start,
            effective_start_time=effective_start / fps,
            near_hit=near_hit,
            is_harmful_overlong=harmful_overlong,
            is_harmful_early_end=harmful_early_end,
        ))

    if debug:
        if intervals:
            summary = []
            for interval in intervals:
                tags: List[str] = []
                if interval.near_hit:
                    tags.append("near_hit")
                if interval.is_harmful_overlong:
                    tags.append("overlong")
                if interval.is_harmful_early_end:
                    tags.append("early_end")
                tag_suffix = f":{','.join(tags)}" if tags else ""
                summary.append(f"{interval.start_frame}-{interval.end_frame}{tag_suffix}")
            _debug(f"[ArmExt:{debug_side}] final intervals {summary}")
        else:
            _debug(f"[ArmExt:{debug_side}] final intervals none")

    return intervals


# ============================================================================
# Priority scoring
# ============================================================================

def analyze_blade_contact(left_xdata: Dict, left_ydata: Dict, right_xdata: Dict,
                         right_ydata: Dict, contact_frame: int,
                         current_right_of_way: str = 'none',
                         fps: float = DEFAULT_FPS) -> Tuple[str, Dict]:
    """Determine blade priority via learned logistic feature scoring."""
    _configure_blade_touch_feature_extractor(fps)

    left_x_arr, left_y_arr = _dict_to_array(left_xdata), _dict_to_array(left_ydata)
    right_x_arr, right_y_arr = _dict_to_array(right_xdata), _dict_to_array(right_ydata)

    left_feat = btr.compute_fencer_features(left_x_arr, left_y_arr, contact_frame, direction=+1.0)
    right_feat = btr.compute_fencer_features(right_x_arr, right_y_arr, contact_frame, direction=-1.0)

    features = {f'left_{k}': v for k, v in left_feat.items()}
    features.update({f'right_{k}': v for k, v in right_feat.items()})
    features['front_gap'] = right_feat['front_now'] - left_feat['front_now']
    features['front_gap_change'] = right_feat['front_progress'] - left_feat['front_progress']
    features['front_velocity_gap'] = right_feat['front_velocity'] - left_feat['front_velocity']
    features['stance_gap'] = right_feat['stance_now'] - left_feat['stance_now']

    diff_fields = [
        'front_progress',
        'front_velocity',
        'front_velocity_mean_window',
        'front_velocity_peak_window',
        'front_wrist_progress',
        'front_knee_progress',
        'front_height_change',
        'weapon_lead',
        'weapon_lead_progress',
        'weapon_vs_com',
        'stance_progress',
        'com_progress',
        'attack_lead_time',
        'attack_progress_rate',
    ]
    for key in diff_fields:
        lkey = f'left_{key}'
        rkey = f'right_{key}'
        if lkey in features and rkey in features:
            features[f'delta_{key}'] = features[lkey] - features[rkey]

    model_payload = _load_logistic_model()
    rationale = ''
    winner = current_right_of_way if current_right_of_way in {'left', 'right'} else LOGISTIC_DEFAULT_WINNER

    if model_payload:
        feature_names = model_payload['features']
        missing = [name for name in feature_names if name not in features]
        if not missing:
            vector = np.array([[features[name] for name in feature_names]], dtype=float)
            scaler = model_payload['scaler']
            model = model_payload['model']
            if scaler is not None:
                vector = scaler.transform(vector)
            probs = model.predict_proba(vector)[0]
            pred_label = 'left' if probs[LOGISTIC_P_LEFT_INDEX] >= LOGISTIC_LEFT_THRESHOLD else 'right'
            winner = pred_label
            rationale = f'logistic momentum vote (p_left={probs[LOGISTIC_P_LEFT_INDEX]:.2f})'
        else:
            rationale = f'missing features for logistic model: {missing}'
    else:
        rationale = 'logistic model unavailable, falling back to pause ROW'

    details = {
        'contact_frame': contact_frame,
        'left_features': left_feat,
        'right_features': right_feat,
        'feature_fps': _normalise_fps_for_scaling(fps),
        'logistic_available': bool(model_payload),
        'rationale': rationale,
    }

    _debug(f"[BladeContact] frame={contact_frame} rationale={rationale} winner={winner}")
    return winner, details


def classify_blade_contact_case(
    left_xdata: Dict,
    left_ydata: Dict,
    right_xdata: Dict,
    right_ydata: Dict,
    contact_time_s: float,
    contact_frame: int,
    normalisation_constant: Optional[float] = None,
    fps: float = DEFAULT_FPS,
) -> Dict[str, Any]:
    """Classify a blade contact as accident/non-accident, then score benefit side."""
    left_x_arr = _dict_to_array(left_xdata).astype(float)
    left_y_arr = _dict_to_array(left_ydata).astype(float)
    right_x_arr = _dict_to_array(right_xdata).astype(float)
    right_y_arr = _dict_to_array(right_ydata).astype(float)

    scale = 1.0
    if normalisation_constant is not None and np.isfinite(normalisation_constant) and normalisation_constant > 0:
        scale = float(normalisation_constant)
    if scale != 1.0:
        left_x_arr *= scale
        left_y_arr *= scale
        right_x_arr *= scale
        right_y_arr *= scale

    accident_feat = cac.compute_accident_features(
        left_x_arr,
        left_y_arr,
        right_x_arr,
        right_y_arr,
        fps,
        contact_time_s,
    )
    predicted_is_accident, accident_confidence, wrist_pre_margin, post_speed_margin = cac.predict_is_accident(
        accident_feat
    )

    benefit_feat = cbc.compute_contact_features(
        left_x_arr,
        left_y_arr,
        right_x_arr,
        right_y_arr,
        fps,
        contact_time_s,
    )
    benefit_pred = cbc.predict_benefit_side(
        benefit_feat,
        pre_ahead_weight=BLADE_BENEFIT_PRE_AHEAD_WEIGHT,
        score_threshold=BLADE_BENEFIT_SCORE_THRESHOLD,
        ahead_margin=BLADE_BENEFIT_AHEAD_MARGIN,
        high_conf_margin=BLADE_BENEFIT_HIGH_CONF_MARGIN,
        ahead_scale=BLADE_BENEFIT_AHEAD_SCALE,
        post_ahead_weight=BLADE_BENEFIT_POST_AHEAD_WEIGHT,
    )

    details: Dict[str, Any] = {
        'contact_time_s': contact_time_s,
        'contact_frame': contact_frame,
        'accident_prediction': {
            'predicted_is_accident': bool(predicted_is_accident),
            'confidence': accident_confidence,
            'wrist_pre_margin': wrist_pre_margin,
            'post_speed_margin': post_speed_margin,
            'post_speed_was_imputed': int(
                not math.isfinite(float(accident_feat['sym_max_wrist_speed_post']))
            ),
            'features': accident_feat,
        },
        'benefit_prediction': {
            'predicted_side': benefit_pred.predicted_side,
            'confidence': benefit_pred.confidence,
            'method': benefit_pred.method,
            'score_left': benefit_pred.score_left,
            'prob_left': benefit_pred.prob_left,
            'features': benefit_feat,
        },
    }

    if predicted_is_accident:
        details['rationale'] = (
            'Accident classifier triggered; treat this blade contact as non-decisive '
            'and continue with the no-blade-contact path.'
        )
    else:
        details['rationale'] = (
            f"Non-accident blade contact; benefit classifier favors {benefit_pred.predicted_side} "
            f"({benefit_pred.confidence} confidence)."
        )

    _debug(
        "[BladeContactCase] "
        f"frame={contact_frame} accident={bool(predicted_is_accident)} "
        f"benefit={benefit_pred.predicted_side} "
        f"benefit_conf={benefit_pred.confidence}"
    )
    return details

# ============================================================================
# Referee orchestration helpers
# ============================================================================

def _collect_hit_frames(
    phrase: FencingPhrase,
    side_hit_events: Optional[Dict[str, List[Dict[str, float]]]],
) -> Tuple[List[int], List[int]]:
    """Collect per-side hit frames with simultaneous-hit fallback."""
    left_hit_frames: List[int] = []
    right_hit_frames: List[int] = []

    if side_hit_events:
        for event in side_hit_events.get("left_scores_on_right", []):
            frame = event.get("frame")
            if frame is not None:
                left_hit_frames.append(int(frame))
        for event in side_hit_events.get("right_scores_on_left", []):
            frame = event.get("frame")
            if frame is not None:
                right_hit_frames.append(int(frame))

    if phrase.simultaneous_hit_frame is not None:
        if not left_hit_frames:
            left_hit_frames.append(phrase.simultaneous_hit_frame)
        if not right_hit_frames:
            right_hit_frames.append(phrase.simultaneous_hit_frame)

    hit_frame_delay = HIT_FRAME_DELAY
    left_hit_frames = [frame + hit_frame_delay for frame in left_hit_frames]
    right_hit_frames = [frame + hit_frame_delay for frame in right_hit_frames]
    return left_hit_frames, right_hit_frames


def _clamp_hit_frames(hit_frames: List[int], max_frame: int) -> List[int]:
    """Clamp hit frames to [0, max_frame] and deduplicate."""
    if max_frame < 0:
        return []
    return sorted(set(min(max(frame, 0), max_frame) for frame in hit_frames))


def _latest_pause_markers(
    pauses: List[PauseInterval],
) -> Tuple[Optional[float], Optional[int], Optional[float]]:
    """Return (last_end_time, last_end_frame, earliest_start_among_latest_end)."""
    if not pauses:
        return None, None, None
    last_end_time = max(p.end_time for p in pauses)
    last_end_frame = max(p.end_frame for p in pauses)
    latest_candidates = [p for p in pauses if p.end_frame == last_end_frame]
    latest_start = min(p.start_time for p in latest_candidates) if latest_candidates else None
    return last_end_time, last_end_frame, latest_start


def _earliest_hit_time(
    phrase: FencingPhrase,
    side_hit_events: Optional[Dict[str, List[Dict[str, float]]]] = None,
) -> Optional[float]:
    """Return earliest hit time from explicit side-hit events, fallback to simultaneous hit."""
    candidates: List[float] = []
    if side_hit_events:
        for key in ("left_scores_on_right", "right_scores_on_left"):
            for event in side_hit_events.get(key, []):
                t = event.get("time")
                if isinstance(t, (int, float)):
                    candidates.append(float(t))
    if phrase.simultaneous_hit_time is not None:
        candidates.append(float(phrase.simultaneous_hit_time))
    return min(candidates) if candidates else None


def _select_last_relevant_blade_contact(
    phrase: FencingPhrase,
    interaction_hit_time: Optional[float],
    interaction_hit_frame: Optional[int],
) -> Optional[BladeContact]:
    """Return latest blade contact within 1s before earliest hit and before lockout."""
    if interaction_hit_time is None:
        return None
    blade_contacts_before_hit = [
        bc for bc in phrase.blade_contacts
        if (interaction_hit_time - 1.0) <= bc.time < interaction_hit_time
        and (phrase.lockout_start is None or bc.time < phrase.lockout_start)
        and (interaction_hit_frame is None or bc.frame <= interaction_hit_frame)
    ]
    return blade_contacts_before_hit[-1] if blade_contacts_before_hit else None


def _compute_interaction_hit_frame(
    left_hit_frame: Optional[int],
    right_hit_frame: Optional[int],
    phrase: FencingPhrase,
) -> Optional[int]:
    """Return earliest available hit frame for cross-fencer interaction checks."""
    candidates = [f for f in [left_hit_frame, right_hit_frame] if f is not None]
    if candidates:
        return min(candidates)
    return phrase.simultaneous_hit_frame


def referee_decision(phrase: FencingPhrase, left_xdata: Dict, left_ydata: Dict,
                    right_xdata: Dict, right_ydata: Dict,
                    normalisation_constant: Optional[float] = None,
                    side_hit_events: Optional[Dict[str, List[Dict[str, float]]]] = None) -> Dict:
    """Main refereeing logic.

    Pipeline:
    1) Build hit-frame timeline and per-side frame cutoffs.
    2) Detect pauses/lunges/arm-extension on cutoff keypoints.
    3) Resolve priority via lunge short-circuit, overlap attack-window, or
       pause/arm reset ordering, then optionally blade-contact override.
    """
    _ = normalisation_constant  # Kept for API compatibility with caller contracts.
    result = {
        'winner': None,
        'reason': '',
        'left_pauses': [],
        'right_pauses': [],
        'left_slow_start': None,
        'right_slow_start': None,
        'blade_analysis': None,
        'blade_details': None,
        'speed_comparison': None,
        'lunge_detected': {'left': [], 'right': [], 'latest': None},
        'priority_events': {'left': {}, 'right': {}, 'latest': None},
    }

    # 1) Prepare hit frames and frame-capped keypoint views.
    left_hit_frames, right_hit_frames = _collect_hit_frames(phrase, side_hit_events)

    left_max_frame = _max_frame_from_keypoints(left_xdata)
    right_max_frame = _max_frame_from_keypoints(right_xdata)

    left_hit_frames = _clamp_hit_frames(left_hit_frames, left_max_frame)
    right_hit_frames = _clamp_hit_frames(right_hit_frames, right_max_frame)

    left_cutoff_frame = min(left_hit_frames) if left_hit_frames else left_max_frame
    right_cutoff_frame = min(right_hit_frames) if right_hit_frames else right_max_frame

    left_eval_x = _truncate_keypoint_data(left_xdata, left_cutoff_frame)
    left_eval_y = _truncate_keypoint_data(left_ydata, left_cutoff_frame)
    right_eval_x = _truncate_keypoint_data(right_xdata, right_cutoff_frame)
    right_eval_y = _truncate_keypoint_data(right_ydata, right_cutoff_frame)
    shared_max_frame = min(_max_frame_from_keypoints(left_eval_x), _max_frame_from_keypoints(right_eval_x))

    _debug(
        "[FrameCap] "
        f"left_hit_frames={left_hit_frames} right_hit_frames={right_hit_frames} "
        f"left_cutoff={left_cutoff_frame} right_cutoff={right_cutoff_frame} "
        f"shared_max={shared_max_frame}"
    )

    # 2) Detect pause/retreat markers.
    left_pauses_raw = detect_pause_retreat_intervals(
        left_eval_x, left_eval_y, is_left_fencer=True, fps=phrase.fps
    )
    right_pauses_raw = detect_pause_retreat_intervals(
        right_eval_x, right_eval_y, is_left_fencer=False, fps=phrase.fps
    )

    left_pauses = left_pauses_raw
    right_pauses = right_pauses_raw
    left_pauses, left_slow_start = _extract_slow_starts(left_pauses, phrase.fps)
    right_pauses, right_slow_start = _extract_slow_starts(right_pauses, phrase.fps)
    result['left_slow_start'] = asdict(left_slow_start) if left_slow_start else None
    result['right_slow_start'] = asdict(right_slow_start) if right_slow_start else None

    result['left_pauses'] = left_pauses
    result['right_pauses'] = right_pauses

    left_last_pause_end, left_last_pause_end_frame, left_latest_pause_start = _latest_pause_markers(left_pauses)
    right_last_pause_end, right_last_pause_end_frame, right_latest_pause_start = _latest_pause_markers(right_pauses)

    left_hit_frame = min(left_hit_frames) if left_hit_frames else None
    right_hit_frame = min(right_hit_frames) if right_hit_frames else None

    interaction_hit_frame = _compute_interaction_hit_frame(left_hit_frame, right_hit_frame, phrase)
    interaction_hit_time = _earliest_hit_time(phrase, side_hit_events)
    if interaction_hit_time is None:
        interaction_hit_time = (
            interaction_hit_frame / phrase.fps
            if interaction_hit_frame is not None
            else phrase.simultaneous_hit_time
        )

    # Blade-contact judging path: latest relevant contact within 1s before earliest hit.
    last_blade_contact = _select_last_relevant_blade_contact(
        phrase,
        interaction_hit_time,
        interaction_hit_frame,
    )
    if last_blade_contact:
        _debug(
            f"[BladePreHit] using latest pre-hit contact time={last_blade_contact.time:.3f}s "
            f"frame={last_blade_contact.frame} earliest_hit_time={interaction_hit_time:.3f}s"
        )
    effective_blade_contact = last_blade_contact
    blade_case_details: Optional[Dict[str, Any]] = None
    if last_blade_contact:
        if shared_max_frame < 0 or last_blade_contact.frame > shared_max_frame:
            effective_blade_contact = None
            result['blade_analysis'] = (
                f'Blade contact at {last_blade_contact.time:.2f}s ignored '
                '(outside per-side frame cutoff)'
            )
        else:
            blade_case_details = classify_blade_contact_case(
                left_xdata,
                left_ydata,
                right_xdata,
                right_ydata,
                last_blade_contact.time,
                last_blade_contact.frame,
                normalisation_constant=normalisation_constant,
                fps=phrase.fps,
            )
            result['blade_details'] = blade_case_details
            accident_prediction = blade_case_details['accident_prediction']
            benefit_prediction = blade_case_details['benefit_prediction']
            if accident_prediction['predicted_is_accident']:
                effective_blade_contact = None
                result['blade_analysis'] = (
                    f"Blade contact at {last_blade_contact.time:.2f}s classified accident "
                    f"({accident_prediction['confidence']}); ignored for priority."
                )
            else:
                result['blade_analysis'] = (
                    f"Blade contact at {last_blade_contact.time:.2f}s classified non-accident; "
                    f"benefit={benefit_prediction['predicted_side']} "
                    f"({benefit_prediction['confidence']})."
                )

    left_lunges = detect_lunge_intervals(
        left_eval_x,
        left_eval_y,
        is_left_fencer=True,
        fps=phrase.fps,
        hit_frames=left_hit_frames,
    )
    right_lunges = detect_lunge_intervals(
        right_eval_x,
        right_eval_y,
        is_left_fencer=False,
        fps=phrase.fps,
        hit_frames=right_hit_frames,
    )
    result['lunge_detected'] = {
        'left': [asdict(l) for l in left_lunges],
        'right': [asdict(l) for l in right_lunges],
        'latest': None,
    }

    left_penalizing_lunges = [l for l in left_lunges if l.is_penalizing]
    right_penalizing_lunges = [l for l in right_lunges if l.is_penalizing]

    latest_left = left_penalizing_lunges[-1] if left_penalizing_lunges else None
    latest_right = right_penalizing_lunges[-1] if right_penalizing_lunges else None
    latest_side = None
    latest_interval = None
    if latest_left and latest_right:
        if latest_left.end_frame > latest_right.end_frame:
            latest_side, latest_interval = 'left', latest_left
        elif latest_right.end_frame > latest_left.end_frame:
            latest_side, latest_interval = 'right', latest_right
    elif latest_left:
        latest_side, latest_interval = 'left', latest_left
    elif latest_right:
        latest_side, latest_interval = 'right', latest_right

    if latest_side and latest_interval:
        result['lunge_detected']['latest'] = {
            'side': latest_side,
            'start_frame': latest_interval.start_frame,
            'end_frame': latest_interval.end_frame,
            'start_time': latest_interval.start_time,
            'end_time': latest_interval.end_time,
        }

    arm_debug = DEBUG_LOGGING
    left_extensions = detect_arm_extension(
        left_eval_x,
        left_eval_y,
        is_left_fencer=True,
        fps=phrase.fps,
        hit_frame=left_hit_frame,
        debug=arm_debug,
    )
    right_extensions = detect_arm_extension(
        right_eval_x,
        right_eval_y,
        is_left_fencer=False,
        fps=phrase.fps,
        hit_frame=right_hit_frame,
        debug=arm_debug,
    )

    result['left_arm_extensions'] = [asdict(e) for e in left_extensions]
    result['right_arm_extensions'] = [asdict(e) for e in right_extensions]

    left_harmful_extensions = [
        e for e in left_extensions
        if e.is_harmful_overlong or e.is_harmful_early_end
    ]
    right_harmful_extensions = [
        e for e in right_extensions
        if e.is_harmful_overlong or e.is_harmful_early_end
    ]
    latest_left_harmful_ext = left_harmful_extensions[-1] if left_harmful_extensions else None
    latest_right_harmful_ext = right_harmful_extensions[-1] if right_harmful_extensions else None
    latest_harmful_ext_side = None
    latest_harmful_ext = None
    if latest_left_harmful_ext and latest_right_harmful_ext:
        if latest_left_harmful_ext.end_frame > latest_right_harmful_ext.end_frame:
            latest_harmful_ext_side, latest_harmful_ext = 'left', latest_left_harmful_ext
        elif latest_right_harmful_ext.end_frame > latest_left_harmful_ext.end_frame:
            latest_harmful_ext_side, latest_harmful_ext = 'right', latest_right_harmful_ext
    elif latest_left_harmful_ext:
        latest_harmful_ext_side, latest_harmful_ext = 'left', latest_left_harmful_ext
    elif latest_right_harmful_ext:
        latest_harmful_ext_side, latest_harmful_ext = 'right', latest_right_harmful_ext

    def _interval_overlaps_window(
        interval_start: int,
        interval_end: int,
        window_start: int,
        window_end: int,
    ) -> bool:
        return interval_end >= window_start and interval_start <= window_end

    def _harmful_events_in_attack_window(
        harmful_extensions: List[ArmExtensionInterval],
        lunges: List[LungeInterval],
        window_start: int,
        window_end: int,
    ) -> List[str]:
        harmful_events: List[str] = []
        for interval in harmful_extensions:
            if _interval_overlaps_window(interval.start_frame, interval.end_frame, window_start, window_end):
                harmful_reasons: List[str] = []
                if interval.is_harmful_overlong:
                    harmful_reasons.append("overlong")
                if interval.is_harmful_early_end:
                    harmful_reasons.append("early_end")
                reason_suffix = ",".join(harmful_reasons) if harmful_reasons else "harmful"
                harmful_events.append(
                    f"harmful_arm_extension[{interval.start_frame}-{interval.end_frame}:{reason_suffix}]"
                )
        for interval in lunges:
            if not interval.is_penalizing:
                continue
            if _interval_overlaps_window(interval.start_frame, interval.end_frame, window_start, window_end):
                harmful_events.append(
                    f"harmful_lunge[{interval.start_frame}-{interval.end_frame}:{interval.classification}]"
                )
        return harmful_events

    overlap_ok, overlap_ratio = _pause_overlap_ok(
        left_pauses,
        right_pauses,
        phrase.fps,
        left_last_end=left_last_pause_end_frame,
        right_last_end=right_last_pause_end_frame,
    )
    if overlap_ok and effective_blade_contact is None:
        left_attack_start = left_last_pause_end_frame if left_last_pause_end_frame is not None else 0
        right_attack_start = right_last_pause_end_frame if right_last_pause_end_frame is not None else 0
        left_attack_end = left_hit_frame if left_hit_frame is not None else shared_max_frame
        right_attack_end = right_hit_frame if right_hit_frame is not None else shared_max_frame
        left_harmful_window_events = _harmful_events_in_attack_window(
            left_harmful_extensions,
            left_lunges,
            left_attack_start,
            left_attack_end,
        )
        right_harmful_window_events = _harmful_events_in_attack_window(
            right_harmful_extensions,
            right_lunges,
            right_attack_start,
            right_attack_end,
        )
        if left_harmful_window_events or right_harmful_window_events:
            _debug(
                "[AttackWindow] overlap window blocked by harmful events: "
                f"left={left_harmful_window_events or ['none']} "
                f"right={right_harmful_window_events or ['none']}"
            )
        else:
            window_start = min(left_attack_start, right_attack_start)
            window_end = max(left_attack_end, right_attack_end)
            winner, detail, speed_info, left_ext, right_ext = _decide_attack_by_arm_and_speed(
                left_eval_x,
                left_eval_y,
                right_eval_x,
                right_eval_y,
                left_slow_start,
                right_slow_start,
                window_start=window_start,
                window_end=window_end,
                fps=phrase.fps,
                left_window_start=left_attack_start,
                right_window_start=right_attack_start,
                left_window_end=left_attack_end,
                right_window_end=right_attack_end,
                left_lunges=left_lunges,
                right_lunges=right_lunges,
            )
            result['left_arm_extensions'] = [asdict(e) for e in left_ext]
            result['right_arm_extensions'] = [asdict(e) for e in right_ext]
            if speed_info:
                result['speed_comparison'] = speed_info
            result['winner'] = winner
            result['reason'] = (
                f'Pauses overlap (>{int(PAUSE_OVERLAP_MIN_RATIO * 100)}%, '
                f'<{PAUSE_OVERLAP_MAX_TOTAL_SECONDS:.1f}s, ratio={overlap_ratio:.2f}). {detail}'
            )
            return result
    if overlap_ok and effective_blade_contact is not None:
        _debug("[PauseOverlap] overlap detected but skipped because pre-hit blade contact exists")

    if not left_pauses and not right_pauses and not effective_blade_contact:
        left_attack_start = ATTACK_WINDOW_DEFAULT_START_FRAME
        right_attack_start = ATTACK_WINDOW_DEFAULT_START_FRAME
        left_attack_end = left_hit_frame if left_hit_frame is not None else _max_frame_from_keypoints(left_eval_x)
        right_attack_end = right_hit_frame if right_hit_frame is not None else _max_frame_from_keypoints(right_eval_x)
        if left_attack_end < left_attack_start:
            left_attack_end = left_attack_start
        if right_attack_end < right_attack_start:
            right_attack_end = right_attack_start
        left_harmful_window_events = _harmful_events_in_attack_window(
            left_harmful_extensions,
            left_lunges,
            left_attack_start,
            left_attack_end,
        )
        right_harmful_window_events = _harmful_events_in_attack_window(
            right_harmful_extensions,
            right_lunges,
            right_attack_start,
            right_attack_end,
        )
        if left_harmful_window_events or right_harmful_window_events:
            _debug(
                "[AttackWindow] no-pause window blocked by harmful events: "
                f"left={left_harmful_window_events or ['none']} "
                f"right={right_harmful_window_events or ['none']}"
            )
        else:
            window_start = min(left_attack_start, right_attack_start)
            window_end = max(left_attack_end, right_attack_end)
            winner, detail, speed_info, left_ext, right_ext = _decide_attack_by_arm_and_speed(
                left_eval_x,
                left_eval_y,
                right_eval_x,
                right_eval_y,
                left_slow_start,
                right_slow_start,
                window_start=window_start,
                window_end=window_end,
                fps=phrase.fps,
                left_window_start=left_attack_start,
                right_window_start=right_attack_start,
                left_window_end=left_attack_end,
                right_window_end=right_attack_end,
                left_lunges=left_lunges,
                right_lunges=right_lunges,
            )
            result['left_arm_extensions'] = [asdict(e) for e in left_ext]
            result['right_arm_extensions'] = [asdict(e) for e in right_ext]
            if speed_info:
                result['speed_comparison'] = speed_info
            result['winner'] = winner
            result['reason'] = f'No pauses/blade contacts. {detail}'

            return result

    def _latest_pause_event(
        pauses: List[PauseInterval],
        side_label: str,
    ) -> Optional[Dict[str, Any]]:
        if not pauses:
            return None
        latest = max(pauses, key=lambda interval: (interval.end_frame, interval.start_frame))
        return {
            'side': side_label,
            'type': 'retreat' if latest.is_retreat else 'pause',
            'start_frame': latest.start_frame,
            'end_frame': latest.end_frame,
            'start_time': latest.start_time,
            'end_time': latest.end_time,
            'priority_end_frame': latest.end_frame,
            'priority_end_time': latest.end_time,
            'response_buffer_frames': 0,
            'duration': latest.duration,
        }

    def _latest_harmful_lunge_event(
        lunges: List[LungeInterval],
        side_label: str,
    ) -> Optional[Dict[str, Any]]:
        penalizing = [interval for interval in lunges if interval.is_penalizing]
        if not penalizing:
            return None
        latest = max(penalizing, key=lambda interval: (interval.end_frame, interval.start_frame))
        response_buffer = max(LUNGE_RESPONSE_BUFFER_FRAMES, 0)
        return {
            'side': side_label,
            'type': 'harmful_lunge',
            'start_frame': latest.start_frame,
            'end_frame': latest.end_frame,
            'start_time': latest.start_time,
            'end_time': latest.end_time,
            'priority_end_frame': latest.end_frame + response_buffer,
            'priority_end_time': (latest.end_frame + response_buffer) / phrase.fps,
            'response_buffer_frames': response_buffer,
            'classification': latest.classification,
            'overlap_hit_frames': latest.overlap_hit_frames,
        }

    def _latest_harmful_arm_event(
        extensions: List[ArmExtensionInterval],
        side_label: str,
    ) -> Optional[Dict[str, Any]]:
        harmful = [
            interval for interval in extensions
            if interval.is_harmful_overlong or interval.is_harmful_early_end
        ]
        if not harmful:
            return None
        latest = max(harmful, key=lambda interval: (interval.end_frame, interval.start_frame))
        response_buffer = max(ARM_EXTENSION_RESPONSE_BUFFER_FRAMES, 0)
        harmful_reason = []
        if latest.is_harmful_overlong:
            harmful_reason.append('overlong')
        if latest.is_harmful_early_end:
            harmful_reason.append('early_end')
        return {
            'side': side_label,
            'type': 'harmful_arm_extension',
            'start_frame': latest.start_frame,
            'end_frame': latest.end_frame,
            'start_time': latest.start_time,
            'end_time': latest.end_time,
            'priority_end_frame': latest.end_frame + response_buffer,
            'priority_end_time': (latest.end_frame + response_buffer) / phrase.fps,
            'response_buffer_frames': response_buffer,
            'effective_start_frame': latest.effective_start_frame,
            'effective_start_time': latest.effective_start_time,
            'length_frames': latest.end_frame - latest.start_frame + 1,
            'harmful_reason': harmful_reason,
        }

    left_priority_events = {
        'pause_or_retreat': _latest_pause_event(left_pauses, 'left'),
        'harmful_lunge': _latest_harmful_lunge_event(left_lunges, 'left'),
        'harmful_arm_extension': _latest_harmful_arm_event(left_extensions, 'left'),
    }
    right_priority_events = {
        'pause_or_retreat': _latest_pause_event(right_pauses, 'right'),
        'harmful_lunge': _latest_harmful_lunge_event(right_lunges, 'right'),
        'harmful_arm_extension': _latest_harmful_arm_event(right_extensions, 'right'),
    }
    result['priority_events'] = {
        'left': left_priority_events,
        'right': right_priority_events,
        'latest': None,
    }

    all_priority_events = [
        event
        for events in (left_priority_events, right_priority_events)
        for event in events.values()
        if event is not None
    ]

    if all_priority_events:
        max_end_frame = max(event['priority_end_frame'] for event in all_priority_events)
        latest_by_end = [
            event for event in all_priority_events if event['priority_end_frame'] == max_end_frame
        ]
        max_start_frame = max(event['start_frame'] for event in latest_by_end)
        latest_candidates = [event for event in latest_by_end if event['start_frame'] == max_start_frame]

        if len({event['side'] for event in latest_candidates}) == 1:
            decisive_event = latest_candidates[0]
            result['priority_events']['latest'] = decisive_event
            losing_side = decisive_event['side']
            right_of_way = 'right' if losing_side == 'left' else 'left'
            priority_row = (
                f"{right_of_way.capitalize()} has right-of-way: latest harmful/reset event is "
                f"{decisive_event['type']} by {losing_side} at "
                f"{decisive_event['priority_end_time']:.2f}s "
                f"(buffered frame {decisive_event['priority_end_frame']}, raw end frame "
                f"{decisive_event['end_frame']})"
            )
        else:
            latest_candidates_sorted = sorted(
                latest_candidates,
                key=lambda event: (
                    event['priority_end_frame'],
                    event['start_frame'],
                    event['type'],
                    event['side'],
                ),
            )
            result['priority_events']['latest'] = latest_candidates_sorted
            right_of_way = 'right'
            priority_row = (
                "Right has right-of-way: latest harmful/reset events are tied on both sides at "
                f"frame {max_end_frame}; defaulting to right for determinism."
            )
    else:
        right_of_way = 'none'
        priority_row = 'No pause/retreat, harmful lunge, or harmful arm-extension events detected'
    
    if effective_blade_contact:
        benefit_prediction = (blade_case_details or {}).get('benefit_prediction', {})
        blade_beater = benefit_prediction.get('predicted_side')
        if blade_beater not in {'left', 'right'}:
            result['winner'] = right_of_way
            result['reason'] = f'{priority_row}. Blade contact analysis unavailable; fallback to harmful/reset timeline'
            return result

        result['winner'] = blade_beater
        if blade_beater == right_of_way:
            result['reason'] = (
                f"{priority_row}. Non-accident blade contact favored {blade_beater} "
                f"({benefit_prediction.get('confidence', 'unknown')} confidence)."
            )
        else:
            result['reason'] = (
                f"{priority_row}. Non-accident blade contact overrode harmful/reset context and favored "
                f"{blade_beater} ({benefit_prediction.get('confidence', 'unknown')} confidence)."
            )
    else:
        result['winner'] = right_of_way
        result['reason'] = priority_row
    
    return result

def _resolve_target_dir(subfolder: str) -> Path:
    """Resolve phrase directory from configured search roots."""
    for root in INPUT_SEARCH_DIRS:
        candidate = root / subfolder
        if candidate.exists():
            return candidate
    for root in INPUT_SEARCH_DIRS:
        if not root.exists():
            continue
        matches = sorted(
            p for p in root.rglob(subfolder)
            if p.is_dir()
        )
        if matches:
            return matches[0]
    # Return first root candidate to keep error message concrete.
    return INPUT_SEARCH_DIRS[0] / subfolder


# ============================================================================
# CLI entrypoint
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description="Debug AI Referee Logic")
    parser.add_argument("subfolder", help="Name of the subfolder to analyze")
    args = parser.parse_args()

    target_dir = _resolve_target_dir(args.subfolder)
    if not target_dir.exists():
        print(f"Subfolder not found in configured paths: {target_dir}")
        sys.exit(1)

    DEBUG_OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(DEBUG_OUTPUT_PATH, "w") as log_file, contextlib.redirect_stdout(log_file), contextlib.redirect_stderr(log_file):
        print(f"Analyzing {target_dir}...")

        txt_path = find_phrase_txt_file(target_dir)
        excel_path = find_phrase_excel_file(target_dir)
        video_path = find_phrase_video_file(target_dir)
        json_path = target_dir / "analysis_result.json"
        experimental_json_path = target_dir / "analysis_result_limb_interp_experimental.json"
    
        if txt_path is None or excel_path is None or video_path is None:
            print("Missing required files (txt, xlsx, or video)")
            sys.exit(1)
        
        norm_constant = None
        for candidate in (json_path, experimental_json_path):
            if not candidate.exists():
                continue
            try:
                with open(candidate, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                norm_constant = data.get("normalisation_constant")
                if norm_constant is not None:
                    break
            except Exception:
                continue
            
        phrase_fps = infer_phrase_fps(target_dir, txt_path=txt_path, fallback_fps=DEFAULT_FPS)
        phrase = parse_txt_file(str(txt_path), fps=phrase_fps, video_path=str(video_path))
        left_x, left_y, right_x, right_y = load_keypoints_from_excel(str(excel_path))
        if left_x and KP_FRONT_FOOT in left_x:
            max_frame = len(left_x[KP_FRONT_FOOT]) - 1
            _trim_phrase_to_frames(phrase, max_frame)
        side_hit_events = extract_side_hit_events(str(txt_path), fps=phrase.fps, video_path=str(video_path))
        
        decision = referee_decision(
            phrase, 
            left_x, left_y, 
            right_x, right_y, 
            normalisation_constant=norm_constant,
            side_hit_events=side_hit_events,
        )

        print("\n" + "="*60)
        print("HIT FRAME SUMMARY")
        print("="*60)
        print(f"Left scores on Right (time/frame): {side_hit_events['left_scores_on_right']}")
        print(f"Right scores on Left (time/frame): {side_hit_events['right_scores_on_left']}")
        print("="*60)
        
        print("\n" + "="*60)
        print("DEBUG RESULT")
        print("="*60)
        print(json.dumps(sanitize_for_json(decision), indent=2))
        print("="*60)

        persisted_decision = sanitize_for_json(decision)
        if norm_constant is not None and "normalisation_constant" not in persisted_decision:
            persisted_decision["normalisation_constant"] = norm_constant
        if "phrase_fps" not in persisted_decision:
            persisted_decision["phrase_fps"] = phrase.fps
        json_path.write_text(json.dumps(persisted_decision, indent=2), encoding="utf-8")

if __name__ == "__main__":
    main()
