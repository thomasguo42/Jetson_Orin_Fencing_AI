#!/usr/bin/env python3
"""
Experimental limb-only keypoint repair pipeline with jump-safe tracking.

This script reruns extraction from the source phrase video using YOLO pose,
detects anomalous arm/leg joints on the freshly extracted keypoints,
interpolates short interior bad runs, and writes repaired artifacts into a
separate output folder.

Compared to the base limb-interp experiment, this variant keeps the narrow
limb repair but adds a hard anti-switch layer during tracking: when a
candidate detection looks too far from the predicted fencer identity, the
track is treated as missing for that frame instead of being overwritten by a
different person.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import shutil
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np
import pandas as pd
from ultralytics import YOLO

import sys

BUNDLE_ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(BUNDLE_ROOT))
from src.referee import analysis  # type: ignore
from src.referee.analysis import (  # type: ignore
    correct_fisheye_video,
    parse_txt_file,
    process_video_and_extract_data,
    referee_decision,
    render_overlay_video,
    sanitize_for_json,
    save_keypoints_to_excel,
)


KP_NAMES = {
    0: "nose",
    1: "left_eye",
    2: "right_eye",
    3: "left_ear",
    4: "right_ear",
    5: "left_shoulder",
    6: "right_shoulder",
    7: "left_elbow",
    8: "right_elbow",
    9: "left_wrist",
    10: "right_wrist",
    11: "left_hip",
    12: "right_hip",
    13: "left_knee",
    14: "right_knee",
    15: "left_ankle",
    16: "right_ankle",
}

TRACKING_INDICES_RE = re.compile(
    r"tracking\s+indices\s*->\s*fencer\s*1:\s*(-?\d+)\s*,\s*fencer\s*2:\s*(-?\d+)\s*$",
    re.IGNORECASE,
)
TARGET_JOINTS = [7, 8, 9, 10, 13, 14, 15, 16]
BASE_INTERPOLATE_MAX_GAP = 5
MAX_INTERP_RUN = 4
BRIDGE_GAP = 1
LIMB_MAX_INTERP_RUN = 8
BOOTSTRAP_FRAMES_DEFAULT = 8

JOINT_BONES = {
    7: [(5, 7, "left_upper_arm"), (7, 9, "left_lower_arm")],
    8: [(6, 8, "right_upper_arm"), (8, 10, "right_lower_arm")],
    9: [(7, 9, "left_lower_arm")],
    10: [(8, 10, "right_lower_arm")],
    13: [(11, 13, "left_thigh"), (13, 15, "left_shin")],
    14: [(12, 14, "right_thigh"), (14, 16, "right_shin")],
    15: [(13, 15, "left_shin")],
    16: [(14, 16, "right_shin")],
}

LIMBS = [
    {"name": "left_arm", "joints": [7, 9]},
    {"name": "right_arm", "joints": [8, 10]},
    {"name": "left_leg", "joints": [13, 15]},
    {"name": "right_leg", "joints": [14, 16]},
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phrase-dir", type=Path, required=True, help="Phrase folder with video/txt/keypoints.")
    parser.add_argument(
        "--model-path",
        type=Path,
        default=BUNDLE_ROOT / "models" / "yolo26x-pose.pt",
        help="YOLO pose weights to use for fresh extraction.",
    )
    parser.add_argument("--yolo-conf", type=float, default=0.15, help="YOLO detection confidence threshold.")
    parser.add_argument(
        "--yolo-verbose",
        action="store_true",
        help="Show Ultralytics per-frame timing output during detection/tracking.",
    )
    parser.add_argument(
        "--bootstrap-frames",
        type=int,
        default=BOOTSTRAP_FRAMES_DEFAULT,
        help="How many corrected-video frames to scan when selecting the front left/right fencers.",
    )
    parser.add_argument(
        "--sam-threshold",
        type=float,
        default=0.15,
        help="Deprecated legacy option. Ignored.",
    )
    parser.add_argument(
        "--sam-mask-threshold",
        type=float,
        default=0.5,
        help="Deprecated legacy option. Ignored.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Output directory. Defaults to results/experimental_limb_interp/<phrase_name>.",
    )
    parser.add_argument(
        "--repair-only",
        action="store_true",
        help="Skip fisheye/YOLO extraction and repair the existing keypoints Excel in the phrase folder.",
    )
    parser.add_argument(
        "--progress",
        action="store_true",
        help="Show fisheye/overlay progress bars.",
    )
    return parser.parse_args()


def _find_file(folder: Path, pattern: str) -> Path:
    matches = sorted(folder.glob(pattern))
    if not matches:
        raise FileNotFoundError(f"No match for {pattern} in {folder}")
    return matches[0]


def _find_input_video(folder: Path) -> Path:
    avis = sorted(folder.glob("*.avi"))
    if avis:
        return avis[0]
    mp4s = sorted([p for p in folder.glob("*.mp4") if "_overlay" not in p.name and "_corrected" not in p.name])
    if mp4s:
        return mp4s[0]
    raise FileNotFoundError(f"No input video found in {folder}")


def _find_corrected_video(folder: Path, input_video: Path) -> Optional[Path]:
    preferred = folder / f"{input_video.stem}_corrected.mp4"
    if preferred.exists():
        return preferred
    corrected = sorted(folder.glob("*_corrected.mp4"))
    return corrected[0] if corrected else None


def _find_existing_keypoints_excel(folder: Path) -> Optional[Path]:
    preferred = sorted(
        p for p in folder.glob("*_keypoints.xlsx")
        if "_limb_interp_" not in p.name and "_reextracted_" not in p.name and "_posefix_" not in p.name
    )
    if preferred:
        return preferred[0]
    candidates = sorted(
        p for p in folder.glob("*.xlsx")
        if "_limb_interp_" not in p.name and "_reextracted_" not in p.name and "_posefix_" not in p.name
    )
    return candidates[0] if candidates else None


def _load_excel_dicts(excel_path: Path) -> Tuple[Dict[int, List[float]], Dict[int, List[float]], Dict[int, List[float]], Dict[int, List[float]]]:
    xls = pd.ExcelFile(excel_path)
    left_x = pd.read_excel(xls, sheet_name="Left_X").to_dict(orient="list")
    left_y = pd.read_excel(xls, sheet_name="Left_Y").to_dict(orient="list")
    right_x = pd.read_excel(xls, sheet_name="Right_X").to_dict(orient="list")
    right_y = pd.read_excel(xls, sheet_name="Right_Y").to_dict(orient="list")
    return (
        {int(k): [float(v) if pd.notna(v) else math.nan for v in vals] for k, vals in left_x.items()},
        {int(k): [float(v) if pd.notna(v) else math.nan for v in vals] for k, vals in left_y.items()},
        {int(k): [float(v) if pd.notna(v) else math.nan for v in vals] for k, vals in right_x.items()},
        {int(k): [float(v) if pd.notna(v) else math.nan for v in vals] for k, vals in right_y.items()},
    )


def _dicts_to_arrays(
    xdata: Dict[int, List[float]],
    ydata: Dict[int, List[float]],
) -> Tuple[np.ndarray, np.ndarray]:
    cols = sorted(xdata.keys())
    x = np.stack([np.asarray(xdata[c], dtype=float) for c in cols], axis=1)
    y = np.stack([np.asarray(ydata[c], dtype=float) for c in cols], axis=1)
    return x, y


def _arrays_to_dicts(x: np.ndarray, y: np.ndarray) -> Tuple[Dict[int, List[float]], Dict[int, List[float]]]:
    return (
        {idx: x[:, idx].astype(float).tolist() for idx in range(x.shape[1])},
        {idx: y[:, idx].astype(float).tolist() for idx in range(y.shape[1])},
    )


def _segment_length(x: np.ndarray, y: np.ndarray, frame_idx: int, a: int, b: int) -> Optional[float]:
    ax, ay = x[frame_idx, a], y[frame_idx, a]
    bx, by = x[frame_idx, b], y[frame_idx, b]
    if not (np.isfinite(ax) and np.isfinite(ay) and np.isfinite(bx) and np.isfinite(by)):
        return None
    return float(math.hypot(ax - bx, ay - by))


def _series_threshold(values: List[float], *, floor: float) -> float:
    if not values:
        return floor
    arr = np.asarray(values, dtype=float)
    med = float(np.median(arr))
    mad = float(np.median(np.abs(arr - med)))
    return float(max(floor, (med * 3.0), med + (4.0 * mad)))


def _prediction_error(x: np.ndarray, y: np.ndarray, frame_idx: int, joint_idx: int) -> Optional[float]:
    if frame_idx <= 0 or frame_idx >= len(x) - 1:
        return None
    prev_valid = np.isfinite(x[frame_idx - 1, joint_idx]) and np.isfinite(y[frame_idx - 1, joint_idx])
    curr_valid = np.isfinite(x[frame_idx, joint_idx]) and np.isfinite(y[frame_idx, joint_idx])
    next_valid = np.isfinite(x[frame_idx + 1, joint_idx]) and np.isfinite(y[frame_idx + 1, joint_idx])
    if not (prev_valid and curr_valid and next_valid):
        return None
    pred_x = 0.5 * (x[frame_idx - 1, joint_idx] + x[frame_idx + 1, joint_idx])
    pred_y = 0.5 * (y[frame_idx - 1, joint_idx] + y[frame_idx + 1, joint_idx])
    return float(math.hypot(x[frame_idx, joint_idx] - pred_x, y[frame_idx, joint_idx] - pred_y))


def _bone_stats(x: np.ndarray, y: np.ndarray) -> Dict[Tuple[int, int, str], float]:
    medians: Dict[Tuple[int, int, str], float] = {}
    for joint_idx in TARGET_JOINTS:
        for a, b, label in JOINT_BONES[joint_idx]:
            key = (a, b, label)
            if key in medians:
                continue
            vals = []
            for frame_idx in range(len(x)):
                dist = _segment_length(x, y, frame_idx, a, b)
                if dist is not None:
                    vals.append(dist)
            medians[key] = float(np.median(vals)) if vals else 0.0
    return medians


def detect_limb_anomalies(
    xdata: Dict[int, List[float]],
    ydata: Dict[int, List[float]],
    *,
    side_label: str,
) -> Dict[str, Any]:
    x, y = _dicts_to_arrays(xdata, ydata)
    bone_medians = _bone_stats(x, y)
    flagged_by_joint: Dict[int, List[int]] = defaultdict(list)
    examples: List[Dict[str, Any]] = []
    frame_counter: Counter = Counter()

    for joint_idx in TARGET_JOINTS:
        step_vals: List[float] = []
        pred_vals: List[float] = []
        for frame_idx in range(1, len(x)):
            if np.isfinite(x[frame_idx, joint_idx]) and np.isfinite(y[frame_idx, joint_idx]) and np.isfinite(x[frame_idx - 1, joint_idx]) and np.isfinite(y[frame_idx - 1, joint_idx]):
                step_vals.append(float(math.hypot(x[frame_idx, joint_idx] - x[frame_idx - 1, joint_idx], y[frame_idx, joint_idx] - y[frame_idx - 1, joint_idx])))
        for frame_idx in range(1, len(x) - 1):
            pred_err = _prediction_error(x, y, frame_idx, joint_idx)
            if pred_err is not None:
                pred_vals.append(pred_err)

        step_thr = _series_threshold(step_vals, floor=0.30)
        pred_thr = _series_threshold(pred_vals, floor=0.18)

        for frame_idx in range(len(x)):
            if not (np.isfinite(x[frame_idx, joint_idx]) and np.isfinite(y[frame_idx, joint_idx])):
                continue

            prev_step = None
            next_step = None
            if frame_idx > 0 and np.isfinite(x[frame_idx - 1, joint_idx]) and np.isfinite(y[frame_idx - 1, joint_idx]):
                prev_step = float(math.hypot(x[frame_idx, joint_idx] - x[frame_idx - 1, joint_idx], y[frame_idx, joint_idx] - y[frame_idx - 1, joint_idx]))
            if frame_idx + 1 < len(x) and np.isfinite(x[frame_idx + 1, joint_idx]) and np.isfinite(y[frame_idx + 1, joint_idx]):
                next_step = float(math.hypot(x[frame_idx + 1, joint_idx] - x[frame_idx, joint_idx], y[frame_idx + 1, joint_idx] - y[frame_idx, joint_idx]))
            max_step = max([v for v in [prev_step, next_step] if v is not None], default=0.0)
            pred_err = _prediction_error(x, y, frame_idx, joint_idx)

            bone_flags = []
            for a, b, label in JOINT_BONES[joint_idx]:
                med = bone_medians[(a, b, label)]
                if med <= 1e-6:
                    continue
                dist = _segment_length(x, y, frame_idx, a, b)
                if dist is None:
                    continue
                if dist < (0.55 * med) or dist > (1.60 * med):
                    bone_flags.append(
                        {
                            "bone": label,
                            "length": round(dist, 4),
                            "median_length": round(med, 4),
                        }
                    )

            strong_step = max_step >= (1.35 * step_thr)
            moderate_step = max_step >= step_thr
            strong_pred = pred_err is not None and pred_err >= (1.35 * pred_thr)
            moderate_pred = pred_err is not None and pred_err >= pred_thr

            flagged = False
            reason = None
            if strong_step or strong_pred:
                flagged = True
                reason = "strong_temporal_outlier"
            elif (moderate_step or moderate_pred) and bone_flags:
                flagged = True
                reason = "temporal_plus_bone_outlier"
            elif len(bone_flags) >= 2:
                flagged = True
                reason = "multi_bone_outlier"

            if not flagged:
                continue

            flagged_by_joint[joint_idx].append(frame_idx)
            frame_counter[frame_idx] += 1
            examples.append(
                {
                    "side": side_label,
                    "frame": int(frame_idx),
                    "joint": int(joint_idx),
                    "joint_name": KP_NAMES[joint_idx],
                    "reason": reason,
                    "max_step": round(max_step, 4),
                    "step_threshold": round(step_thr, 4),
                    "prediction_error": round(pred_err, 4) if pred_err is not None else None,
                    "prediction_threshold": round(pred_thr, 4),
                    "bone_flags": bone_flags,
                }
            )

    return {
        "flagged_by_joint": {str(j): sorted(frames) for j, frames in flagged_by_joint.items()},
        "flagged_count": int(sum(len(v) for v in flagged_by_joint.values())),
        "top_frames": [{"frame": int(f), "count": int(c)} for f, c in frame_counter.most_common(10)],
        "examples": examples[:80],
    }


def _merge_joint_runs(frames: List[int]) -> List[Tuple[int, int]]:
    if not frames:
        return []
    frames = sorted(set(frames))
    runs: List[Tuple[int, int]] = []
    start = frames[0]
    prev = frames[0]
    for frame_idx in frames[1:]:
        if frame_idx - prev <= BRIDGE_GAP + 1:
            prev = frame_idx
            continue
        runs.append((start, prev))
        start = frame_idx
        prev = frame_idx
    runs.append((start, prev))
    return runs


def repair_limb_runs(
    xdata: Dict[int, List[float]],
    ydata: Dict[int, List[float]],
    anomaly_report: Dict[str, Any],
    *,
    side_label: str,
) -> Tuple[Dict[int, List[float]], Dict[int, List[float]], Dict[str, Any]]:
    x, y = _dicts_to_arrays(xdata, ydata)
    limb_repair_log: List[Dict[str, Any]] = []
    joint_repair_log: List[Dict[str, Any]] = []

    def _interpolate_run(joint_idx: int, start: int, end: int) -> bool:
        if start <= 0 or end >= len(x) - 1:
            return False
        if not (
            np.isfinite(x[start - 1, joint_idx])
            and np.isfinite(y[start - 1, joint_idx])
            and np.isfinite(x[end + 1, joint_idx])
            and np.isfinite(y[end + 1, joint_idx])
        ):
            return False
        left_x = float(x[start - 1, joint_idx])
        left_y = float(y[start - 1, joint_idx])
        right_x = float(x[end + 1, joint_idx])
        right_y = float(y[end + 1, joint_idx])
        span = (end + 1) - (start - 1)
        for frame_idx in range(start, end + 1):
            alpha = (frame_idx - (start - 1)) / span
            x[frame_idx, joint_idx] = left_x + ((right_x - left_x) * alpha)
            y[frame_idx, joint_idx] = left_y + ((right_y - left_y) * alpha)
        return True

    joint_map = {int(j): frames for j, frames in anomaly_report["flagged_by_joint"].items()}

    for limb in LIMBS:
        active_joints = [joint_idx for joint_idx in limb["joints"] if joint_map.get(joint_idx)]
        if len(active_joints) < 2:
            continue
        union_frames = sorted({frame_idx for joint_idx in active_joints for frame_idx in joint_map[joint_idx]})
        for start, end in _merge_joint_runs(union_frames):
            run_len = end - start + 1
            if run_len > LIMB_MAX_INTERP_RUN:
                limb_repair_log.append(
                    {
                        "limb": limb["name"],
                        "joints": active_joints,
                        "start_frame": int(start),
                        "end_frame": int(end),
                        "length": int(run_len),
                        "status": "skipped_long_run",
                    }
                )
                continue
            if start <= 0 or end >= len(x) - 1:
                limb_repair_log.append(
                    {
                        "limb": limb["name"],
                        "joints": active_joints,
                        "start_frame": int(start),
                        "end_frame": int(end),
                        "length": int(run_len),
                        "status": "skipped_edge_run",
                    }
                )
                continue
            missing_anchor = False
            for joint_idx in active_joints:
                if not (
                    np.isfinite(x[start - 1, joint_idx])
                    and np.isfinite(y[start - 1, joint_idx])
                    and np.isfinite(x[end + 1, joint_idx])
                    and np.isfinite(y[end + 1, joint_idx])
                ):
                    missing_anchor = True
                    break
            if missing_anchor:
                limb_repair_log.append(
                    {
                        "limb": limb["name"],
                        "joints": active_joints,
                        "start_frame": int(start),
                        "end_frame": int(end),
                        "length": int(run_len),
                        "status": "skipped_missing_anchor",
                    }
                )
                continue

            for joint_idx in active_joints:
                _interpolate_run(joint_idx, start, end)
            limb_repair_log.append(
                {
                    "limb": limb["name"],
                    "joints": active_joints,
                    "start_frame": int(start),
                    "end_frame": int(end),
                    "length": int(run_len),
                    "status": "interpolated",
                }
            )

    interim_x, interim_y = _arrays_to_dicts(x, y)
    remaining_report = detect_limb_anomalies(interim_x, interim_y, side_label=side_label)
    joint_map = {int(j): frames for j, frames in remaining_report["flagged_by_joint"].items()}

    for joint_idx in TARGET_JOINTS:
        runs = _merge_joint_runs(joint_map.get(joint_idx, []))
        for start, end in runs:
            run_len = end - start + 1
            if run_len > MAX_INTERP_RUN:
                joint_repair_log.append(
                    {
                        "joint": int(joint_idx),
                        "joint_name": KP_NAMES[joint_idx],
                        "start_frame": int(start),
                        "end_frame": int(end),
                        "length": int(run_len),
                        "status": "skipped_long_run",
                    }
                )
                continue
            if start <= 0 or end >= len(x) - 1:
                joint_repair_log.append(
                    {
                        "joint": int(joint_idx),
                        "joint_name": KP_NAMES[joint_idx],
                        "start_frame": int(start),
                        "end_frame": int(end),
                        "length": int(run_len),
                        "status": "skipped_edge_run",
                    }
                )
                continue
            if not (
                np.isfinite(x[start - 1, joint_idx])
                and np.isfinite(y[start - 1, joint_idx])
                and np.isfinite(x[end + 1, joint_idx])
                and np.isfinite(y[end + 1, joint_idx])
            ):
                joint_repair_log.append(
                    {
                        "joint": int(joint_idx),
                        "joint_name": KP_NAMES[joint_idx],
                        "start_frame": int(start),
                        "end_frame": int(end),
                        "length": int(run_len),
                        "status": "skipped_missing_anchor",
                    }
                )
                continue

            _interpolate_run(joint_idx, start, end)
            joint_repair_log.append(
                {
                    "joint": int(joint_idx),
                    "joint_name": KP_NAMES[joint_idx],
                    "start_frame": int(start),
                    "end_frame": int(end),
                    "length": int(run_len),
                    "status": "interpolated",
                }
            )

    fixed_x, fixed_y = _arrays_to_dicts(x, y)
    return fixed_x, fixed_y, {"limb_repairs": limb_repair_log, "joint_repairs": joint_repair_log}


class JumpSafeTwoFencerTracker(analysis.TwoFencerTracker):
    """Base tracker with only hard anti-switch gates added."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.active_match_cost_threshold = 1.35
        self.recovering_match_cost_threshold = 1.85
        self.reacquire_cost_threshold = 2.35
        self.max_center_jump_frac = 0.42
        self.max_center_jump_px = 72.0
        self.max_vertical_jump_frac = 0.34
        self.max_vertical_jump_px = 52.0
        self.max_horizontal_jump_frac = 0.70
        self.max_horizontal_jump_px = 96.0
        self.max_torso_jump_frac = 0.36
        self.max_torso_jump_px = 44.0
        self.max_area_log_ratio_hard = 0.75
        self.max_posture_hard = 1.05
        self.max_kpt_cost_hard = 0.95
        self.miss_relax_px_per_frame = 18.0
        self.miss_relax_frac_per_frame = 0.10

    def _mean_joint_distance(
        self,
        det_kpts: np.ndarray,
        ref_kpts: np.ndarray,
        joint_ids: Sequence[int],
    ) -> Optional[float]:
        dists: List[float] = []
        for joint_idx in joint_ids:
            if joint_idx >= len(det_kpts) or joint_idx >= len(ref_kpts):
                continue
            dx, dy = float(det_kpts[joint_idx][0]), float(det_kpts[joint_idx][1])
            rx, ry = float(ref_kpts[joint_idx][0]), float(ref_kpts[joint_idx][1])
            if not (np.isfinite(dx) and np.isfinite(dy) and np.isfinite(rx) and np.isfinite(ry)):
                continue
            if dx <= 0 or dy <= 0 or rx <= 0 or ry <= 0:
                continue
            dists.append(float(math.hypot(dx - rx, dy - ry)))
        if not dists:
            return None
        return float(np.mean(dists))

    def _hard_identity_reject(
        self,
        tid: int,
        track: Any,
        det_kpts: np.ndarray,
        meta: Dict[str, Any],
    ) -> bool:
        bbox = meta.get("bbox")
        center = meta.get("center")
        area = float(meta.get("area", 0.0) or 0.0)
        if bbox is None or center is None or area <= 0:
            return True

        prev_bbox = track.bbox
        prev_center = track.centroid
        pred_center = track.predict_center() if track.centroid is not None else None
        if prev_bbox is None or prev_center is None or pred_center is None:
            return False

        prev_w = max(float(prev_bbox[2] - prev_bbox[0]), 1.0)
        prev_h = max(float(prev_bbox[3] - prev_bbox[1]), 1.0)
        prev_diag = max(float(math.hypot(prev_w, prev_h)), 1.0)
        miss_count = int(self.miss_count.get(tid, 0))
        relax_px = miss_count * self.miss_relax_px_per_frame
        relax_frac = miss_count * self.miss_relax_frac_per_frame

        det_cx, det_cy = float(center[0]), float(center[1])
        pred_dx = abs(det_cx - float(pred_center[0]))
        pred_dy = abs(det_cy - float(pred_center[1]))
        pred_dist = float(math.hypot(pred_dx, pred_dy))

        max_center = max(
            self.max_center_jump_px + relax_px,
            prev_diag * (self.max_center_jump_frac + relax_frac),
        )
        max_vertical = max(
            self.max_vertical_jump_px + relax_px,
            prev_h * (self.max_vertical_jump_frac + relax_frac),
        )
        max_horizontal = max(
            self.max_horizontal_jump_px + relax_px,
            prev_w * (self.max_horizontal_jump_frac + relax_frac),
        )
        if pred_dist > max_center or pred_dy > max_vertical or pred_dx > max_horizontal:
            return True

        torso_jump = self._mean_joint_distance(det_kpts, track.kpts, [5, 6, 11, 12])
        if torso_jump is not None:
            max_torso = max(
                self.max_torso_jump_px + relax_px,
                prev_diag * (self.max_torso_jump_frac + relax_frac),
            )
            if torso_jump > max_torso:
                return True

        prev_area = max(float(analysis.bbox_area(prev_bbox)), 1.0)
        area_ratio = max(area / prev_area, prev_area / max(area, 1.0))
        area_log_ratio = abs(math.log(max(area_ratio, 1e-6)))
        if area_log_ratio > (self.max_area_log_ratio_hard + (0.18 * miss_count)):
            return True

        posture = analysis.temporal_posture_distance(det_kpts, track.kpts)
        if posture is not None and posture > (self.max_posture_hard + (0.12 * miss_count)):
            return True

        torso_ref = max(analysis.torso_scale(track.kpts), 1.0)
        kpt_cost = analysis.robust_kpt_distance(det_kpts, track.kpts) / torso_ref
        if np.isfinite(kpt_cost) and kpt_cost > (self.max_kpt_cost_hard + (0.10 * miss_count)):
            return True

        return False

    def _soft_match_cost(
        self,
        tid: int,
        track: Any,
        det_kpts: np.ndarray,
        meta: Dict[str, Any],
    ) -> Optional[float]:
        if self._hard_identity_reject(tid, track, det_kpts, meta):
            return None
        return super()._soft_match_cost(tid, track, det_kpts, meta)


def extract_tracks_with_jump_safe_tracker(
    video_path: str,
    model: YOLO,
    *,
    initial_detection_indices: Optional[Tuple[int, int]] = None,
    initial_detection_bboxes: Optional[Tuple[Tuple[float, float, float, float], Tuple[float, float, float, float]]] = None,
    initial_frame_index: int = 0,
    detection_conf: Optional[float] = None,
    detection_verbose: bool = False,
) -> List[List[Dict[str, Any]]]:
    original_tracker_cls = analysis.TwoFencerTracker
    analysis.TwoFencerTracker = JumpSafeTwoFencerTracker
    try:
        return analysis.extract_tracks_from_video(
            video_path,
            model,
            initial_detection_indices=initial_detection_indices,
            initial_detection_bboxes=initial_detection_bboxes,
            initial_frame_index=initial_frame_index,
            detection_conf=detection_conf,
            detection_verbose=detection_verbose,
        )
    finally:
        analysis.TwoFencerTracker = original_tracker_cls


def build_output_dir(phrase_dir: Path, output_dir: Optional[Path]) -> Path:
    if output_dir is not None:
        return output_dir
    return BUNDLE_ROOT / "runtime_outputs" / "experimental_limb_interp_jumpsafe" / phrase_dir.name


def _read_tracking_indices(txt_path: Path) -> Optional[Tuple[int, int]]:
    if not txt_path.exists():
        return None
    last: Optional[Tuple[int, int]] = None
    with txt_path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            match = TRACKING_INDICES_RE.search(line.strip())
            if not match:
                continue
            last = (int(match.group(1)), int(match.group(2)))
    return last


def _load_video_frame(video_path: Path, frame_idx: int) -> np.ndarray:
    cap = cv2.VideoCapture(str(video_path))
    if frame_idx > 0:
        cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
    ok, frame = cap.read()
    cap.release()
    if not ok or frame is None:
        raise ValueError(f"Cannot read frame {frame_idx} from {video_path}")
    return cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)


def _bbox_iou(box_a: List[float], box_b: List[float]) -> float:
    ax1, ay1, ax2, ay2 = box_a
    bx1, by1, bx2, by2 = box_b
    inter_x1 = max(ax1, bx1)
    inter_y1 = max(ay1, by1)
    inter_x2 = min(ax2, bx2)
    inter_y2 = min(ay2, by2)
    inter_w = max(0.0, inter_x2 - inter_x1)
    inter_h = max(0.0, inter_y2 - inter_y1)
    inter = inter_w * inter_h
    area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
    denom = area_a + area_b - inter
    return inter / denom if denom > 0 else 0.0


def _valid_joint(kpts: np.ndarray, joint_idx: int) -> bool:
    return joint_idx < len(kpts) and float(kpts[joint_idx][0]) > 0.0 and float(kpts[joint_idx][1]) > 0.0


def _max_joint_y(kpts: np.ndarray, joint_indices: Sequence[int]) -> float:
    values = [float(kpts[idx][1]) for idx in joint_indices if _valid_joint(kpts, idx)]
    return max(values) if values else float("nan")


def _bootstrap_pose_quality(kpts: np.ndarray) -> float:
    lower = sum(1 for idx in (11, 12, 13, 14, 15, 16) if _valid_joint(kpts, idx)) / 6.0
    upper = sum(1 for idx in (5, 6) if _valid_joint(kpts, idx)) / 2.0
    ankles = sum(1 for idx in (15, 16) if _valid_joint(kpts, idx)) / 2.0
    return float((0.5 * lower) + (0.3 * upper) + (0.2 * ankles))


def _bootstrap_candidate_from_detection(
    kpts: np.ndarray,
    *,
    frame_idx: int,
    detection_idx: int,
    width: int,
    height: int,
) -> Optional[Dict[str, object]]:
    bbox = analysis.bbox_from_keypoints(kpts)
    if bbox is None:
        return None
    x1, y1, x2, y2 = [float(v) for v in bbox]
    box_w = x2 - x1
    box_h = y2 - y1
    if box_w < 20.0 or box_h < 60.0:
        return None

    center_x = 0.5 * (x1 + x2)
    center_y = 0.5 * (y1 + y2)
    bottom_y = y2
    ankle_y = _max_joint_y(kpts, (15, 16))
    knee_y = _max_joint_y(kpts, (13, 14))
    hip_y = _max_joint_y(kpts, (11, 12))
    pose_quality = _bootstrap_pose_quality(kpts)

    if bottom_y < height * 0.22 or pose_quality < 0.15:
        return None

    ankle_depth = ankle_y if np.isfinite(ankle_y) else bottom_y
    knee_depth = knee_y if np.isfinite(knee_y) else ankle_depth
    hip_depth = hip_y if np.isfinite(hip_y) else knee_depth
    frame_area = max(float(width * height), 1.0)
    area_score = min(1.0, ((box_w * box_h) / (frame_area * 0.10)))

    edge_margin = width * 0.03
    top_margin = height * 0.05
    center_deadzone = width * 0.04
    edge_penalty = 0.0
    if x1 < edge_margin:
        edge_penalty += (edge_margin - x1) / max(edge_margin, 1.0)
    if x2 > width - edge_margin:
        edge_penalty += (x2 - (width - edge_margin)) / max(edge_margin, 1.0)
    if y1 < top_margin:
        edge_penalty += (top_margin - y1) / max(top_margin, 1.0)

    center_penalty = 0.0
    hcenter = width / 2.0
    center_dist = abs(center_x - hcenter)
    if center_dist < center_deadzone:
        center_penalty = (center_deadzone - center_dist) / max(center_deadzone, 1.0)

    small_penalty = max(0.0, ((height * 0.16) - box_h) / max(height * 0.16, 1.0))
    bottom_y_norm = bottom_y / max(float(height), 1.0)
    ankle_y_norm = ankle_depth / max(float(height), 1.0)
    knee_y_norm = knee_depth / max(float(height), 1.0)
    hip_y_norm = hip_depth / max(float(height), 1.0)
    depth_score = (
        (0.55 * bottom_y_norm)
        + (0.25 * ankle_y_norm)
        + (0.15 * knee_y_norm)
        + (0.05 * area_score)
    )
    raw_score = (
        depth_score
        + (0.20 * pose_quality)
        - (0.12 * edge_penalty)
        - (0.08 * center_penalty)
        - (0.05 * small_penalty)
    )
    return {
        "frame_idx": int(frame_idx),
        "detection_idx": int(detection_idx),
        "side": "left" if center_x < hcenter else "right",
        "box_xyxy": [x1, y1, x2, y2],
        "center_xy": [center_x, center_y],
        "bbox_area": float(box_w * box_h),
        "bottom_y": float(bottom_y),
        "ankle_y": float(ankle_depth),
        "knee_y": float(knee_depth),
        "hip_y": float(hip_depth),
        "bottom_y_norm": float(bottom_y_norm),
        "ankle_y_norm": float(ankle_y_norm),
        "knee_y_norm": float(knee_y_norm),
        "hip_y_norm": float(hip_y_norm),
        "area_score": float(area_score),
        "pose_quality": float(pose_quality),
        "edge_penalty": float(edge_penalty),
        "center_penalty": float(center_penalty),
        "small_penalty": float(small_penalty),
        "depth_score": float(depth_score),
        "raw_score": float(raw_score),
    }


def _append_bootstrap_candidate(
    clusters: List[Dict[str, object]],
    candidate: Dict[str, object],
    *,
    width: int,
    height: int,
) -> None:
    frame_diag = max(math.hypot(width, height), 1.0)
    best_idx: Optional[int] = None
    best_match_score = float("-inf")
    for idx, cluster in enumerate(clusters):
        if cluster["side"] != candidate["side"]:
            continue
        gap = int(candidate["frame_idx"]) - int(cluster["last_frame_idx"])
        if gap <= 0 or gap > 2:
            continue
        iou = _bbox_iou(candidate["box_xyxy"], cluster["last_box_xyxy"])  # type: ignore[arg-type]
        cx, cy = candidate["center_xy"]  # type: ignore[misc]
        px, py = cluster["last_center_xy"]  # type: ignore[misc]
        center_dist = math.hypot(float(cx) - float(px), float(cy) - float(py)) / frame_diag
        if iou < 0.10 and center_dist > 0.08:
            continue
        match_score = iou - (0.6 * center_dist)
        if match_score > best_match_score:
            best_match_score = match_score
            best_idx = idx

    if best_idx is None:
        clusters.append(
            {
                "side": candidate["side"],
                "members": [candidate],
                "frames": {int(candidate["frame_idx"])},
                "best_by_frame": {int(candidate["frame_idx"]): candidate},
                "last_frame_idx": int(candidate["frame_idx"]),
                "last_box_xyxy": list(candidate["box_xyxy"]),
                "last_center_xy": list(candidate["center_xy"]),
            }
        )
        return

    cluster = clusters[best_idx]
    cluster["members"].append(candidate)  # type: ignore[index]
    cluster["frames"].add(int(candidate["frame_idx"]))  # type: ignore[index]
    cluster["last_frame_idx"] = int(candidate["frame_idx"])
    cluster["last_box_xyxy"] = list(candidate["box_xyxy"])
    cluster["last_center_xy"] = list(candidate["center_xy"])
    best_by_frame = cluster["best_by_frame"]  # type: ignore[assignment]
    existing = best_by_frame.get(int(candidate["frame_idx"]))
    if existing is None or float(candidate["raw_score"]) > float(existing["raw_score"]):
        best_by_frame[int(candidate["frame_idx"])] = candidate


def _summarize_bootstrap_cluster(cluster: Dict[str, object], total_frames: int) -> Dict[str, object]:
    members: List[Dict[str, object]] = cluster["members"]  # type: ignore[assignment]

    def _median(key: str) -> float:
        values = [float(member[key]) for member in members]
        return float(np.median(values)) if values else 0.0

    persistence = len(cluster["frames"]) / max(total_frames, 1)  # type: ignore[arg-type]
    summary = {
        "side": cluster["side"],
        "member_count": len(members),
        "frame_count": len(cluster["frames"]),  # type: ignore[arg-type]
        "persistence": float(persistence),
        "median_bottom_y_norm": _median("bottom_y_norm"),
        "median_ankle_y_norm": _median("ankle_y_norm"),
        "median_knee_y_norm": _median("knee_y_norm"),
        "median_hip_y_norm": _median("hip_y_norm"),
        "median_pose_quality": _median("pose_quality"),
        "median_area_score": _median("area_score"),
        "median_edge_penalty": _median("edge_penalty"),
        "median_center_penalty": _median("center_penalty"),
        "median_small_penalty": _median("small_penalty"),
        "median_raw_score": _median("raw_score"),
        "score": float(
            (0.55 * _median("bottom_y_norm"))
            + (0.20 * _median("ankle_y_norm"))
            + (0.10 * _median("knee_y_norm"))
            + (0.10 * _median("pose_quality"))
            + (0.05 * _median("area_score"))
            + (0.20 * persistence)
            - (0.12 * _median("edge_penalty"))
            - (0.08 * _median("center_penalty"))
            - (0.05 * _median("small_penalty"))
        ),
    }
    cluster["summary"] = summary
    return summary


def _choose_bootstrap_pair(
    left_clusters: List[Dict[str, object]],
    right_clusters: List[Dict[str, object]],
    total_frames: int,
) -> Optional[Dict[str, object]]:
    best_pair: Optional[Dict[str, object]] = None
    best_score = float("-inf")
    for left_cluster in left_clusters:
        left_summary = left_cluster["summary"]  # type: ignore[index]
        left_by_frame = left_cluster["best_by_frame"]  # type: ignore[assignment]
        left_frames = set(left_by_frame.keys())
        for right_cluster in right_clusters:
            right_summary = right_cluster["summary"]  # type: ignore[index]
            right_by_frame = right_cluster["best_by_frame"]  # type: ignore[assignment]
            common_frames = sorted(left_frames.intersection(right_by_frame.keys()))
            if not common_frames:
                continue

            best_frame = min(
                common_frames,
                key=lambda frame_idx: (
                    -(
                        float(left_by_frame[frame_idx]["raw_score"])
                        + float(right_by_frame[frame_idx]["raw_score"])
                    ),
                    frame_idx,
                ),
            )
            frame_bonus = len(common_frames) / max(total_frames, 1)
            pair_score = float(left_summary["score"]) + float(right_summary["score"]) + (0.15 * frame_bonus)
            if pair_score > best_score:
                best_score = pair_score
                best_pair = {
                    "pair_score": pair_score,
                    "common_frame_count": len(common_frames),
                    "frame_idx": int(best_frame),
                    "left_cluster_summary": left_summary,
                    "right_cluster_summary": right_summary,
                    "left": left_by_frame[best_frame],
                    "right": right_by_frame[best_frame],
                }
    return best_pair


def _write_bootstrap_locator_outputs(
    folder: Path,
    frame_rgb: np.ndarray,
    bootstrap_loc: Dict[str, object],
) -> Dict[str, str]:
    init_frame_path = folder / "bootstrap_init_frame.png"
    overlay_path = folder / "bootstrap_fencer_overlay.png"
    metadata_path = folder / "bootstrap_fencer_metadata.json"

    cv2.imwrite(str(init_frame_path), cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR))
    overlay_bgr = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR)
    colors = {"left": (90, 90, 255), "right": (255, 140, 80)}
    for side_name in ("left", "right"):
        candidate = bootstrap_loc.get(side_name)
        if not isinstance(candidate, dict):
            continue
        x1, y1, x2, y2 = [int(round(v)) for v in candidate["box_xyxy"]]
        color = colors[side_name]
        cv2.rectangle(overlay_bgr, (x1, y1), (x2, y2), color, 3)
        label = (
            f"{side_name} f{int(candidate['frame_idx']) + 1} "
            f"score={float(candidate['raw_score']):.3f} "
            f"bottom={float(candidate['bottom_y_norm']):.3f}"
        )
        cv2.putText(
            overlay_bgr,
            label,
            (x1 + 4, max(24, y1 - 10)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.6,
            color,
            2,
            cv2.LINE_AA,
        )

    cv2.imwrite(str(overlay_path), overlay_bgr)
    metadata_path.write_text(json.dumps(sanitize_for_json(bootstrap_loc), indent=2), encoding="utf-8")
    return {
        "init_frame": str(init_frame_path),
        "overlay": str(overlay_path),
        "metadata": str(metadata_path),
    }


def _locate_front_fencers_with_yolo_bootstrap(
    corrected_video: Path,
    model: YOLO,
    *,
    bootstrap_frames: int,
    yolo_conf: float,
    yolo_verbose: bool,
) -> Dict[str, object]:
    cap = cv2.VideoCapture(str(corrected_video))
    if not cap.isOpened():
        raise ValueError(f"Cannot open corrected video for bootstrap: {corrected_video}")

    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    predict_kwargs = analysis.ultralytics_predict_kwargs(verbose=yolo_verbose, conf=yolo_conf)
    total_frames = max(1, int(bootstrap_frames))
    scanned_frames = 0
    raw_candidate_count = 0
    accepted_candidate_count = 0
    clusters: List[Dict[str, object]] = []

    try:
        for frame_idx in range(total_frames):
            ok, frame = cap.read()
            if not ok or frame is None:
                break
            scanned_frames += 1
            results = model(frame, **predict_kwargs)
            detections: List[np.ndarray] = []
            if len(results) > 0 and results[0].keypoints is not None:
                raw_kpts = results[0].keypoints.xy
                if hasattr(raw_kpts, "cpu"):
                    raw_kpts = raw_kpts.cpu().numpy()
                else:
                    raw_kpts = np.array(raw_kpts)
                for det_idx in range(raw_kpts.shape[0]):
                    detections.append(raw_kpts[det_idx])

            raw_candidate_count += len(detections)
            for det_idx, det_kpts in enumerate(detections):
                candidate = _bootstrap_candidate_from_detection(
                    det_kpts,
                    frame_idx=frame_idx,
                    detection_idx=det_idx,
                    width=width,
                    height=height,
                )
                if candidate is None:
                    continue
                accepted_candidate_count += 1
                _append_bootstrap_candidate(clusters, candidate, width=width, height=height)
    finally:
        cap.release()

    if scanned_frames == 0:
        return {
            "algorithm": "yolo_front_bootstrap_v1",
            "scanned_frames": 0,
            "raw_candidate_count": 0,
            "accepted_candidate_count": 0,
            "cluster_count": 0,
            "left_cluster_count": 0,
            "right_cluster_count": 0,
            "frame_idx": None,
            "left": None,
            "right": None,
            "pair_score": None,
        }

    left_clusters = [cluster for cluster in clusters if cluster["side"] == "left"]
    right_clusters = [cluster for cluster in clusters if cluster["side"] == "right"]
    left_summaries = [
        _summarize_bootstrap_cluster(cluster, scanned_frames)
        for cluster in left_clusters
    ]
    right_summaries = [
        _summarize_bootstrap_cluster(cluster, scanned_frames)
        for cluster in right_clusters
    ]
    best_pair = _choose_bootstrap_pair(left_clusters, right_clusters, scanned_frames)

    locator: Dict[str, object] = {
        "algorithm": "yolo_front_bootstrap_v1",
        "scanned_frames": scanned_frames,
        "raw_candidate_count": raw_candidate_count,
        "accepted_candidate_count": accepted_candidate_count,
        "cluster_count": len(clusters),
        "left_cluster_count": len(left_clusters),
        "right_cluster_count": len(right_clusters),
        "left_cluster_summaries": sorted(left_summaries, key=lambda item: float(item["score"]), reverse=True)[:3],
        "right_cluster_summaries": sorted(right_summaries, key=lambda item: float(item["score"]), reverse=True)[:3],
        "frame_idx": None,
        "left": None,
        "right": None,
        "pair_score": None,
    }
    if best_pair is not None:
        locator.update(best_pair)
    return locator


def _render_yolo_all_people_overlay(
    corrected_video: Path,
    output_path: Path,
    model: YOLO,
    *,
    yolo_conf: float,
    yolo_verbose: bool,
) -> None:
    cap = cv2.VideoCapture(str(corrected_video))
    if not cap.isOpened():
        raise ValueError(f"Cannot open corrected video for YOLO overlay: {corrected_video}")

    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(output_path), fourcc, fps, (width, height))
    if not writer.isOpened():
        cap.release()
        raise ValueError(f"Cannot create YOLO overlay video: {output_path}")

    predict_kwargs = analysis.ultralytics_predict_kwargs(verbose=yolo_verbose, conf=yolo_conf)
    try:
        while True:
            ok, frame = cap.read()
            if not ok or frame is None:
                break
            results = model(frame, **predict_kwargs)
            plotted = results[0].plot(boxes=True, labels=True)
            writer.write(plotted)
    finally:
        cap.release()
        writer.release()


def rerun_extraction(
    input_video: Path,
    txt_path: Path,
    output_dir: Path,
    model_path: Path,
    *,
    progress: bool,
    yolo_conf: float,
    yolo_verbose: bool,
    bootstrap_frames: int,
) -> Dict[str, Any]:
    if not model_path.exists():
        raise FileNotFoundError(f"YOLO weights not found: {model_path}")

    corrected_video = output_dir / f"{input_video.stem}_corrected.mp4"
    corrected_video = Path(
        correct_fisheye_video(
            input_path=str(input_video),
            output_path=str(corrected_video),
            progress=progress,
        )
    )

    model = YOLO(str(model_path))
    yolo_all_people_overlay = output_dir / f"{input_video.stem}_yolo_all_people_overlay.mp4"
    _render_yolo_all_people_overlay(
        corrected_video,
        yolo_all_people_overlay,
        model,
        yolo_conf=yolo_conf,
        yolo_verbose=yolo_verbose,
    )
    tracking_indices = _read_tracking_indices(txt_path)
    manual_indices_present = tracking_indices is not None
    bootstrap_loc: Optional[Dict[str, object]] = None
    bootstrap_outputs: Optional[Dict[str, str]] = None
    initial_boxes = None
    init_mode = "auto"
    initial_frame_index = 0
    tracks_per_frame: List[List[Dict[str, Any]]]
    if tracking_indices is not None:
        init_mode = "manual"
        tracks_per_frame = extract_tracks_with_jump_safe_tracker(
            str(corrected_video),
            model,
            initial_detection_indices=tracking_indices,
            detection_conf=yolo_conf,
            detection_verbose=yolo_verbose,
        )
    else:
        bootstrap_loc = _locate_front_fencers_with_yolo_bootstrap(
            corrected_video,
            model,
            bootstrap_frames=bootstrap_frames,
            yolo_conf=yolo_conf,
            yolo_verbose=yolo_verbose,
        )
        if bootstrap_loc["left"] is not None and bootstrap_loc["right"] is not None:
            initial_frame_index = int(bootstrap_loc.get("frame_idx") or 0)
            initial_boxes = (
                tuple(bootstrap_loc["left"]["box_xyxy"]),  # type: ignore[index]
                tuple(bootstrap_loc["right"]["box_xyxy"]),  # type: ignore[index]
            )
            init_frame_rgb = _load_video_frame(corrected_video, initial_frame_index)
            bootstrap_outputs = _write_bootstrap_locator_outputs(output_dir, init_frame_rgb, bootstrap_loc)
            init_mode = "bootstrap"
            tracks_per_frame = extract_tracks_with_jump_safe_tracker(
                str(corrected_video),
                model,
                initial_detection_bboxes=initial_boxes,
                initial_frame_index=initial_frame_index,
                detection_conf=yolo_conf,
                detection_verbose=yolo_verbose,
            )
        else:
            if bootstrap_loc is not None:
                fallback_frame_rgb = _load_video_frame(corrected_video, 0)
                bootstrap_outputs = _write_bootstrap_locator_outputs(output_dir, fallback_frame_rgb, bootstrap_loc)
            tracks_per_frame = extract_tracks_with_jump_safe_tracker(
                str(corrected_video),
                model,
                detection_conf=yolo_conf,
                detection_verbose=yolo_verbose,
            )
    left_x, left_y, right_x, right_y, norm_constant, video_angle = process_video_and_extract_data(
        tracks_per_frame,
        interpolate_max_gap=BASE_INTERPOLATE_MAX_GAP,
    )

    return {
        "corrected_video": corrected_video,
        "tracks_per_frame": tracks_per_frame,
        "left_x": left_x,
        "left_y": left_y,
        "right_x": right_x,
        "right_y": right_y,
        "norm_constant": norm_constant,
        "video_angle": video_angle,
        "tracking_indices": list(tracking_indices) if tracking_indices is not None else None,
        "manual_indices_present": manual_indices_present,
        "init_mode": init_mode,
        "bootstrap_locator": bootstrap_loc,
        "bootstrap_outputs": bootstrap_outputs,
        "bootstrap_used": bool(initial_boxes is not None),
        "bootstrap_frame_index": initial_frame_index if initial_boxes is not None else None,
        "yolo_all_people_overlay": str(yolo_all_people_overlay),
        "tracker_mode": "jump_safe_base",
    }


def main() -> int:
    args = parse_args()
    phrase_dir = args.phrase_dir.resolve()
    if not phrase_dir.exists():
        raise FileNotFoundError(f"Phrase dir not found: {phrase_dir}")
    if not args.model_path.resolve().exists():
        raise FileNotFoundError(f"YOLO weights not found: {args.model_path}")

    input_video = _find_input_video(phrase_dir)
    txt_path = _find_file(phrase_dir, "*.txt")

    output_dir = build_output_dir(phrase_dir, args.output_dir.resolve() if args.output_dir else None)
    output_dir.mkdir(parents=True, exist_ok=True)

    copied_video = output_dir / input_video.name
    copied_txt = output_dir / txt_path.name
    shutil.copy2(input_video, copied_video)
    shutil.copy2(txt_path, copied_txt)

    if args.repair_only:
        source_excel = _find_existing_keypoints_excel(phrase_dir)
        if source_excel is None:
            raise FileNotFoundError(f"No existing keypoints Excel found in {phrase_dir}")
        source_corrected = _find_corrected_video(phrase_dir, input_video)
        if source_corrected is None:
            raise FileNotFoundError(f"No corrected video found in {phrase_dir}")
        copied_corrected = output_dir / source_corrected.name
        shutil.copy2(source_corrected, copied_corrected)
        left_x, left_y, right_x, right_y = _load_excel_dicts(source_excel)
        original_result = None
        original_result_path = phrase_dir / "analysis_result.json"
        if original_result_path.exists():
            with open(original_result_path, "r", encoding="utf-8") as f:
                original_result = json.load(f)
        norm_constant = original_result.get("normalisation_constant") if isinstance(original_result, dict) else None
        video_angle = original_result.get("video_angle") if isinstance(original_result, dict) else None
        extraction = {
            "corrected_video": copied_corrected,
            "tracks_per_frame": [],
            "tracking_indices": _read_tracking_indices(copied_txt),
            "manual_indices_present": _read_tracking_indices(copied_txt) is not None,
            "init_mode": "existing_keypoints",
            "bootstrap_locator": None,
            "bootstrap_outputs": None,
            "bootstrap_used": False,
            "bootstrap_frame_index": None,
            "tracker_mode": "existing_keypoints",
            "source_excel": str(source_excel),
            "repair_only": True,
        }
    else:
        extraction = rerun_extraction(
            copied_video,
            copied_txt,
            output_dir,
            args.model_path.resolve(),
            progress=args.progress,
            yolo_conf=args.yolo_conf,
            yolo_verbose=args.yolo_verbose,
            bootstrap_frames=args.bootstrap_frames,
        )
        copied_corrected = extraction["corrected_video"]
        left_x = extraction["left_x"]
        left_y = extraction["left_y"]
        right_x = extraction["right_x"]
        right_y = extraction["right_y"]
        norm_constant = extraction["norm_constant"]
        video_angle = extraction["video_angle"]
        original_result = None
        original_result_path = phrase_dir / "analysis_result.json"
        if original_result_path.exists():
            with open(original_result_path, "r", encoding="utf-8") as f:
                original_result = json.load(f)

    before_left = detect_limb_anomalies(left_x, left_y, side_label="left")
    before_right = detect_limb_anomalies(right_x, right_y, side_label="right")

    left_x_fixed, left_y_fixed, left_repair = repair_limb_runs(left_x, left_y, before_left, side_label="left")
    right_x_fixed, right_y_fixed, right_repair = repair_limb_runs(right_x, right_y, before_right, side_label="right")

    after_left = detect_limb_anomalies(left_x_fixed, left_y_fixed, side_label="left")
    after_right = detect_limb_anomalies(right_x_fixed, right_y_fixed, side_label="right")

    repaired_excel = output_dir / f"{input_video.stem}_limb_interp_keypoints.xlsx"
    save_keypoints_to_excel(left_x_fixed, left_y_fixed, right_x_fixed, right_y_fixed, str(repaired_excel))

    repaired_overlay = output_dir / f"{input_video.stem}_limb_interp_overlay.mp4"
    render_overlay_video(
        video_path=copied_corrected,
        output_path=repaired_overlay,
        left_xdata=left_x_fixed,
        left_ydata=left_y_fixed,
        right_xdata=right_x_fixed,
        right_ydata=right_y_fixed,
        normalisation_constant=float(norm_constant) if isinstance(norm_constant, (int, float)) else 1.0,
        draw_skeleton=True,
        draw_labels=True,
        show_progress=args.progress,
    )

    phrase = parse_txt_file(str(copied_txt), video_path=str(copied_corrected))
    raw_decision = referee_decision(
        phrase,
        left_x,
        left_y,
        right_x,
        right_y,
        normalisation_constant=norm_constant,
    )
    repaired_decision = referee_decision(
        phrase,
        left_x_fixed,
        left_y_fixed,
        right_x_fixed,
        right_y_fixed,
        normalisation_constant=norm_constant,
    )

    result = {
        "input_phrase_dir": str(phrase_dir),
        "model_path": str(args.model_path.resolve()),
        "repair_only": args.repair_only,
        "source_excel": extraction.get("source_excel"),
        "yolo_detection_confidence": args.yolo_conf,
        "tracking_indices": extraction["tracking_indices"],
        "manual_indices_present": extraction["manual_indices_present"],
        "init_mode": extraction["init_mode"],
        "bootstrap_fencer_init": extraction["bootstrap_locator"],
        "bootstrap_outputs": extraction["bootstrap_outputs"],
        "bootstrap_used": extraction["bootstrap_used"],
        "bootstrap_frame_index": extraction.get("bootstrap_frame_index"),
        "tracker_mode": extraction.get("tracker_mode"),
        "yolo_all_people_overlay": extraction.get("yolo_all_people_overlay"),
        "repaired_excel": str(repaired_excel),
        "repaired_overlay": str(repaired_overlay),
        "normalisation_constant": norm_constant,
        "video_angle": video_angle,
        "frames_analyzed": len(extraction["tracks_per_frame"]),
        "before_anomalies": {"left": before_left, "right": before_right},
        "after_anomalies": {"left": after_left, "right": after_right},
        "repair_report": {"left": left_repair, "right": right_repair},
        "original_analysis_result": original_result,
        "reextracted_decision": sanitize_for_json(raw_decision),
        "repaired_decision": sanitize_for_json(repaired_decision),
    }
    result_path = output_dir / "analysis_result_limb_interp_experimental.json"
    result_path.write_text(json.dumps(sanitize_for_json(result), indent=2), encoding="utf-8")

    summary = {
        "before_left_anomalies": before_left["flagged_count"],
        "after_left_anomalies": after_left["flagged_count"],
        "before_right_anomalies": before_right["flagged_count"],
        "after_right_anomalies": after_right["flagged_count"],
        "original_winner": original_result.get("winner") if isinstance(original_result, dict) else None,
        "reextracted_winner": raw_decision.get("winner"),
        "repaired_winner": repaired_decision.get("winner"),
        "result_json": str(result_path),
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
