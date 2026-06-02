from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from functools import lru_cache

import cv2
import matplotlib.pyplot as plt
import numpy as np
import openpyxl
import pandas as pd
import torch
from dataclasses import asdict, dataclass, field, is_dataclass
from typing import Any, Dict, List, Optional, Tuple
from ultralytics import YOLO
from tqdm import tqdm
import warnings
warnings.filterwarnings('ignore')

from src.referee.video_timing import (
    infer_video_fps,
    map_time_to_frame_index,
    resolve_phrase_video_path,
    validate_frame_preserving,
)

logger = logging.getLogger(__name__)

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "AIzaSyDy_RRq8hd8rTYILt_mYtMH8GtM41GFp6I")
GEMINI_MODEL = "models/gemini-2.5-flash-lite"

REFERENCE_INIT_FOLDER = "20251106_210036_phrase74_20251121T062543Z"
REFERENCE_INIT_EXCEL = "20251106_210036_phrase74_compressed_keypoints.xlsx"

def valid_mask(kpts_xy: np.ndarray):
    return (kpts_xy[:, 0] > 0) & (kpts_xy[:, 1] > 0)

def valid_points(kpts_xy: np.ndarray):
    return kpts_xy[valid_mask(kpts_xy)]

def kpt_centroid(kpts_xy: np.ndarray):
    vp = valid_points(kpts_xy)
    if len(vp) == 0:
        return (0.0, 0.0)
    return (float(vp[:, 0].mean()), float(vp[:, 1].mean()))

def bbox_from_keypoints(kpts_xy: np.ndarray):
    vp = valid_points(kpts_xy)
    if len(vp) < 2:
        return None
    x1, y1 = vp[:, 0].min(), vp[:, 1].min()
    x2, y2 = vp[:, 0].max(), vp[:, 1].max()
    return (int(x1), int(y1), int(x2), int(y2))

def bbox_center(bbox: Tuple[int, int, int, int]):
    x1, y1, x2, y2 = bbox
    return ((x1 + x2) / 2, (y1 + y2) / 2)

def bbox_area(bbox: Tuple[int, int, int, int]):
    x1, y1, x2, y2 = bbox
    return (x2 - x1) * (y2 - y1)

def bbox_iou(b1: Optional[Tuple[int, int, int, int]],
             b2: Optional[Tuple[int, int, int, int]]):
    if b1 is None or b2 is None:
        return 0.0
    x1 = max(b1[0], b2[0])
    y1 = max(b1[1], b2[1])
    x2 = min(b1[2], b2[2])
    y2 = min(b1[3], b2[3])
    inter = max(0, x2 - x1) * max(0, y2 - y1)
    area1 = bbox_area(b1)
    area2 = bbox_area(b2)
    union = area1 + area2 - inter
    return inter / union if union > 0 else 0.0

def torso_scale(kpts: np.ndarray):
    if len(kpts) < 17:
        return 1.0
    left_shoulder = kpts[5]
    right_shoulder = kpts[6]
    left_hip = kpts[11]
    right_hip = kpts[12]
    pts = [left_shoulder, right_shoulder, left_hip, right_hip]
    valid = [p for p in pts if p[0] > 0 and p[1] > 0]
    if len(valid) < 2:
        return 1.0
    coords = np.array(valid)
    dists = np.linalg.norm(coords[:, None] - coords[None, :], axis=2)
    return float(dists.max())

def robust_kpt_distance(k1: np.ndarray, k2: np.ndarray):
    mask = valid_mask(k1) & valid_mask(k2)
    if not mask.any():
        return float('inf')
    diff = k1[mask] - k2[mask]
    return float(np.linalg.norm(diff, axis=1).mean())

def composite_track_cost(
    k_det: np.ndarray,
    k_prev: np.ndarray,
    prev_bbox: Optional[Tuple[int, int, int, int]],
    pred_center: Optional[Tuple[float, float]],
    frame_diag: float,
):
    kpt_dist = robust_kpt_distance(k_det, k_prev)
    det_bbox = bbox_from_keypoints(k_det)
    if det_bbox is None or prev_bbox is None:
        bbox_cost = 0.5
    else:
        iou = bbox_iou(det_bbox, prev_bbox)
        bbox_cost = 1.0 - iou
    if pred_center is not None and det_bbox is not None:
        det_center = bbox_center(det_bbox)
        motion_dist = math.hypot(det_center[0] - pred_center[0],
                                 det_center[1] - pred_center[1])
        motion_cost = motion_dist / frame_diag if frame_diag > 0 else 0.0
    else:
        motion_cost = 0.0
    scale = torso_scale(k_prev)
    kpt_cost = kpt_dist / scale if scale > 0 else kpt_dist
    posture_cost = temporal_posture_distance(k_det, k_prev)
    if posture_cost is None:
        # Fallback when too few posture features are available.
        return 0.7 * kpt_cost + 0.2 * bbox_cost + 0.1 * motion_cost
    # Keep keypoint distance + posture as majority signals.
    return 0.45 * kpt_cost + 0.35 * posture_cost + 0.12 * bbox_cost + 0.08 * motion_cost


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _resolve_reference_excel_path() -> Optional[Path]:
    env_path = os.environ.get("REFEREE_INIT_REFERENCE_XLSX")
    if env_path:
        path = Path(env_path).expanduser()
        if path.exists():
            return path
    root = _repo_root()
    candidates = [
        root / "data" / "training_data" / REFERENCE_INIT_FOLDER / REFERENCE_INIT_EXCEL,
        root / "blade_touch_rule" / "training_data" / REFERENCE_INIT_FOLDER / REFERENCE_INIT_EXCEL,
        root / "blade_touch_rule" / "non_blade_data" / REFERENCE_INIT_FOLDER / REFERENCE_INIT_EXCEL,
    ]
    for path in candidates:
        if path.exists():
            return path
    return None


def _read_keypoint_row(df_x: pd.DataFrame, df_y: pd.DataFrame, row_idx: int = 0) -> np.ndarray:
    if row_idx >= len(df_x) or row_idx >= len(df_y):
        raise ValueError(f"Reference sheet missing row {row_idx}")
    kpts = np.full((17, 2), np.nan, dtype=float)

    kp_cols = [f"kp_{i}" for i in range(17)]
    if all(col in df_x.columns and col in df_y.columns for col in kp_cols):
        for i in range(17):
            kpts[i, 0] = float(df_x.iloc[row_idx][f"kp_{i}"])
            kpts[i, 1] = float(df_y.iloc[row_idx][f"kp_{i}"])
        return kpts

    str_cols = [str(i) for i in range(17)]
    if all(col in df_x.columns and col in df_y.columns for col in str_cols):
        for i in range(17):
            kpts[i, 0] = float(df_x.iloc[row_idx][str(i)])
            kpts[i, 1] = float(df_y.iloc[row_idx][str(i)])
        return kpts

    int_cols = list(range(17))
    if all(col in df_x.columns and col in df_y.columns for col in int_cols):
        for i in range(17):
            kpts[i, 0] = float(df_x.iloc[row_idx][i])
            kpts[i, 1] = float(df_y.iloc[row_idx][i])
        return kpts

    cols_x = list(df_x.columns)[:17]
    cols_y = list(df_y.columns)[:17]
    for i, (cx, cy) in enumerate(zip(cols_x, cols_y)):
        kpts[i, 0] = float(df_x.iloc[row_idx][cx])
        kpts[i, 1] = float(df_y.iloc[row_idx][cy])
    return kpts


@lru_cache(maxsize=1)
def _load_reference_first_frame_postures() -> Tuple[Optional[np.ndarray], Optional[np.ndarray]]:
    path = _resolve_reference_excel_path()
    if path is None:
        logger.warning(
            "Reference keypoint Excel not found for first-frame fencer initialization; "
            "falling back to area/side initialization."
        )
        return None, None

    xls = pd.ExcelFile(path)
    sheet_map = {name.lower(): name for name in xls.sheet_names}

    def _sheet(name: str) -> pd.DataFrame:
        key = name.lower()
        if key not in sheet_map:
            raise ValueError(f"Missing sheet '{name}' in {path}; available: {xls.sheet_names}")
        return pd.read_excel(xls, sheet_name=sheet_map[key])

    try:
        left_x = _sheet("left_x")
        left_y = _sheet("left_y")
        right_x = _sheet("right_x")
        right_y = _sheet("right_y")
        left_ref = _read_keypoint_row(left_x, left_y, row_idx=0)
        right_ref = _read_keypoint_row(right_x, right_y, row_idx=0)
    except Exception as exc:
        logger.warning(
            "Failed to load posture reference from %s (%s); "
            "falling back to area/side initialization.",
            path,
            exc,
        )
        return None, None

    logger.info("Loaded first-frame posture reference for tracker init from %s", path)
    return left_ref, right_ref


def _kpt_valid_at(kpts: np.ndarray, idx: int) -> bool:
    if idx >= len(kpts):
        return False
    x, y = float(kpts[idx][0]), float(kpts[idx][1])
    return np.isfinite(x) and np.isfinite(y) and x > 0 and y > 0


def _segment_length(kpts: np.ndarray, i: int, j: int) -> Optional[float]:
    if not (_kpt_valid_at(kpts, i) and _kpt_valid_at(kpts, j)):
        return None
    p = np.array(kpts[i], dtype=float)
    q = np.array(kpts[j], dtype=float)
    dist = float(np.linalg.norm(p - q))
    if dist <= 1e-9:
        return None
    return dist


def _joint_angle(kpts: np.ndarray, a: int, b: int, c: int) -> Optional[float]:
    if not (_kpt_valid_at(kpts, a) and _kpt_valid_at(kpts, b) and _kpt_valid_at(kpts, c)):
        return None
    pa = np.array(kpts[a], dtype=float)
    pb = np.array(kpts[b], dtype=float)
    pc = np.array(kpts[c], dtype=float)
    v1 = pa - pb
    v2 = pc - pb
    n1 = float(np.linalg.norm(v1))
    n2 = float(np.linalg.norm(v2))
    if n1 <= 1e-9 or n2 <= 1e-9:
        return None
    cos_theta = float(np.dot(v1, v2) / (n1 * n2))
    cos_theta = float(np.clip(cos_theta, -1.0, 1.0))
    return float(math.acos(cos_theta))


def _pose_descriptor(kpts: np.ndarray) -> Dict[str, Dict[str, float]]:
    angle_features: Dict[str, float] = {}
    ratio_features: Dict[str, float] = {}

    angle_specs = [
        ("left_elbow_angle", 5, 7, 9),
        ("right_elbow_angle", 6, 8, 10),
        ("left_shoulder_angle", 7, 5, 11),
        ("right_shoulder_angle", 8, 6, 12),
        ("left_hip_angle", 5, 11, 13),
        ("right_hip_angle", 6, 12, 14),
        ("left_knee_angle", 11, 13, 15),
        ("right_knee_angle", 12, 14, 16),
        ("torso_left_chain_angle", 5, 11, 12),
        ("torso_right_chain_angle", 6, 12, 11),
    ]
    for name, a, b, c in angle_specs:
        value = _joint_angle(kpts, a, b, c)
        if value is not None:
            angle_features[name] = value

    ratio_specs = [
        ("left_upper_to_lower_arm", (5, 7), (7, 9)),
        ("right_upper_to_lower_arm", (6, 8), (8, 10)),
        ("left_thigh_to_shin", (11, 13), (13, 15)),
        ("right_thigh_to_shin", (12, 14), (14, 16)),
        ("left_torso_to_thigh", (5, 11), (11, 13)),
        ("right_torso_to_thigh", (6, 12), (12, 14)),
        ("shoulder_to_hip_width", (5, 6), (11, 12)),
        ("stance_to_hip_width", (15, 16), (11, 12)),
        ("left_arm_to_left_leg", (5, 9), (11, 15)),
        ("right_arm_to_right_leg", (6, 10), (12, 16)),
    ]
    for name, (n1a, n1b), (n2a, n2b) in ratio_specs:
        len1 = _segment_length(kpts, n1a, n1b)
        len2 = _segment_length(kpts, n2a, n2b)
        if len1 is None or len2 is None or len2 <= 1e-9:
            continue
        ratio_features[name] = float(len1 / len2)

    return {"angles": angle_features, "ratios": ratio_features}


def _angle_distance(a: float, b: float) -> float:
    diff = abs(a - b)
    diff = min(diff, (2.0 * math.pi) - diff)
    return float(diff / math.pi)


def _ratio_distance(a: float, b: float) -> float:
    if a <= 0 or b <= 0:
        return float("inf")
    return float(abs(math.log(a / b)))


def posture_distance_to_reference(det_kpts: np.ndarray, ref_kpts: Optional[np.ndarray]) -> float:
    if ref_kpts is None:
        return float("inf")
    if det_kpts is None or len(det_kpts) < 17:
        return float("inf")

    det_desc = _pose_descriptor(det_kpts)
    ref_desc = _pose_descriptor(ref_kpts)

    angle_common = set(det_desc["angles"]).intersection(ref_desc["angles"])
    ratio_common = set(det_desc["ratios"]).intersection(ref_desc["ratios"])

    distances: List[float] = []
    for name in angle_common:
        distances.append(_angle_distance(det_desc["angles"][name], ref_desc["angles"][name]))
    for name in ratio_common:
        d = _ratio_distance(det_desc["ratios"][name], ref_desc["ratios"][name])
        if np.isfinite(d):
            distances.append(d)

    # Require enough comparable geometric features for stable matching.
    if len(distances) < 6:
        return float("inf")
    return float(np.mean(distances))


def temporal_posture_distance(k_det: np.ndarray, k_prev: np.ndarray) -> Optional[float]:
    """Posture distance for frame-to-frame matching.

    Returns None when posture features are too sparse to be reliable.
    """
    d = posture_distance_to_reference(k_det, k_prev)
    if not np.isfinite(d):
        return None
    return float(d)

class _TrackState:
    def __init__(
        self,
        tid: int,
        kpts: np.ndarray,
        frame_idx: int,
        anchor_kpts: Optional[np.ndarray] = None,
    ):
        self.id = tid
        self.kpts = kpts
        self.bbox = bbox_from_keypoints(kpts)
        self.centroid = kpt_centroid(kpts)
        self.prev_centroid: Optional[Tuple[float, float]] = None
        self.last_seen = frame_idx
        self.anchor_kpts = np.array(anchor_kpts, copy=True) if anchor_kpts is not None else np.array(kpts, copy=True)
        self.anchor_bbox = bbox_from_keypoints(self.anchor_kpts)

    def predict_center(self):
        if self.prev_centroid is None:
            return self.centroid
        dx = self.centroid[0] - self.prev_centroid[0]
        dy = self.centroid[1] - self.prev_centroid[1]
        return (self.centroid[0] + dx, self.centroid[1] + dy)

class TwoFencerTracker:
    """Two-fencer keypoint tracker with robust matching."""
    def __init__(
        self,
        frame_w: int,
        frame_h: int,
        max_miss: int = 45,
        edge_margin_frac: float = 0.06,
        top_margin_frac: float = 0.12,
        bottom_margin_frac: float = 0.20,
        hcenter_sigma_frac: float = 0.22,
        center_deadzone_frac: float = 0.06,
    ):
        self.frame_w = frame_w
        self.frame_h = frame_h
        self.max_miss = max_miss
        self.frame_diag = math.hypot(frame_w, frame_h)
        self.edge_margin = int(frame_w * edge_margin_frac)
        self.top_margin = int(frame_h * top_margin_frac)
        self.bottom_margin = int(frame_h * bottom_margin_frac)
        self.hcenter_sigma = frame_w * hcenter_sigma_frac
        # Exclude detections close to the camera centerline; fencers should be on left/right.
        self.center_deadzone = frame_w * center_deadzone_frac
        self.unmatched_cost = 2.8
        self.max_candidate_cost = 8.0
        self.active_match_cost_threshold = 1.85
        self.recovering_match_cost_threshold = 2.35
        self.side_penalty_weight = 0.35
        self.center_penalty_weight = 0.10
        self.area_penalty_weight = 0.20
        self.edge_penalty_weight = 0.30
        self.crossing_penalty_weight = 0.90
        self.anchor_posture_weight = 0.55
        self.anchor_area_weight = 0.20
        self.reacquire_cost_threshold = 3.2
        self.side_guard_frac = 0.18
        self.crossing_margin_px = frame_w * 0.02
        self.tracks: Dict[int, Optional[_TrackState]] = {0: None, 1: None}
        self.lost_tracks: Dict[int, Optional[_TrackState]] = {0: None, 1: None}
        self.miss_count = {0: 0, 1: 0}
        self.frame_idx = 0
        self.initialized = False
        self.left_ref_posture, self.right_ref_posture = _load_reference_first_frame_postures()

    def _center_safe(self, center: Tuple[float, float]):
        hcenter = self.frame_w / 2
        return abs(center[0] - hcenter) >= self.center_deadzone

    def _edge_safe(self, center: Tuple[float, float]):
        x, y = center
        if x < self.edge_margin or x > (self.frame_w - self.edge_margin):
            return False
        if y < self.top_margin or y > (self.frame_h - self.bottom_margin):
            return False
        return True

    def _bbox_edge_safe(self, bbox: Optional[Tuple[int, int, int, int]]) -> bool:
        if bbox is None:
            return False
        x1, y1, x2, y2 = bbox
        # Discard detections touching or very near frame boundaries.
        if x1 <= self.edge_margin or x2 >= (self.frame_w - self.edge_margin):
            return False
        if y1 <= self.top_margin or y2 >= (self.frame_h - self.bottom_margin):
            return False
        return True

    def _init_score(self, kpts: np.ndarray, side: str):
        c = kpt_centroid(kpts)
        if not self._edge_safe(c):
            return -1e9
        if not self._center_safe(c):
            return -1e9
        hcenter = self.frame_w / 2
        dist_from_center = abs(c[0] - hcenter)
        gaussian = math.exp(-0.5 * (dist_from_center / self.hcenter_sigma) ** 2)
        if side == 'left':
            side_score = 1.0 if c[0] < hcenter else 0.0
        else:
            side_score = 1.0 if c[0] >= hcenter else 0.0
        return gaussian + side_score

    def _pick_initial_tracks(self, detections: List[np.ndarray]):
        if len(detections) < 2:
            return None, None

        hcenter = self.frame_w / 2.0
        left_candidates = []
        right_candidates = []

        for det in detections:
            bbox = bbox_from_keypoints(det)
            if bbox is None or not self._bbox_edge_safe(bbox):
                continue
            cx, _ = bbox_center(bbox)
            area = bbox_area(bbox)
            candidate = (det, area)
            if cx < hcenter:
                left_candidates.append(candidate)
            else:
                right_candidates.append(candidate)

        left_candidates.sort(key=lambda item: item[1], reverse=True)
        right_candidates.sort(key=lambda item: item[1], reverse=True)
        left_candidates = left_candidates[:3]
        right_candidates = right_candidates[:3]

        def _pick_by_posture(candidates: List[Tuple[np.ndarray, float]], reference: Optional[np.ndarray]):
            if not candidates:
                return None
            if reference is None:
                # Fallback when reference is unavailable: largest area candidate.
                return candidates[0][0]
            ranked = []
            for det_kpts, area in candidates:
                dist = posture_distance_to_reference(det_kpts, reference)
                ranked.append((dist, -area, det_kpts))
            ranked.sort(key=lambda item: (item[0], item[1]))
            return ranked[0][2]

        left_pick = _pick_by_posture(left_candidates, self.left_ref_posture)
        right_pick = _pick_by_posture(right_candidates, self.right_ref_posture)

        if left_pick is None or right_pick is None:
            # If one half has no valid candidates after edge filtering, fallback to previous scoring.
            scores_left = [(i, self._init_score(d, 'left')) for i, d in enumerate(detections)]
            scores_right = [(i, self._init_score(d, 'right')) for i, d in enumerate(detections)]
            scores_left.sort(key=lambda x: x[1], reverse=True)
            scores_right.sort(key=lambda x: x[1], reverse=True)
            if not scores_left or not scores_right:
                return None, None
            best_left_idx = scores_left[0][0]
            best_right_idx = scores_right[0][0]
            if best_left_idx == best_right_idx:
                if len(scores_left) > 1:
                    best_left_idx = scores_left[1][0]
                else:
                    return None, None
            return detections[best_left_idx], detections[best_right_idx]

        return left_pick, right_pick

    def initialize(self, detections: List[np.ndarray]):
        left_kpts, right_kpts = self._pick_initial_tracks(detections)
        if left_kpts is None or right_kpts is None:
            return
        self.tracks[0] = _TrackState(0, left_kpts, self.frame_idx)
        self.tracks[1] = _TrackState(1, right_kpts, self.frame_idx)
        self.lost_tracks = {0: None, 1: None}
        self.miss_count = {0: 0, 1: 0}
        self.initialized = True

    def initialize_with_detection_indices(
        self,
        detections: List[np.ndarray],
        first_idx: int,
        second_idx: int,
    ) -> bool:
        """
        Initialize tracker from explicit detection indices in the current frame.

        The two detections are assigned to left/right tracks by x-position to preserve
        the existing pipeline convention: track 0 = left side, track 1 = right side.
        """
        if first_idx == second_idx:
            return False
        if first_idx < 0 or second_idx < 0:
            return False
        if first_idx >= len(detections) or second_idx >= len(detections):
            return False

        first_kpts = detections[first_idx]
        second_kpts = detections[second_idx]
        first_box = bbox_from_keypoints(first_kpts)
        second_box = bbox_from_keypoints(second_kpts)
        if first_box is None or second_box is None:
            return False

        first_cx, _ = bbox_center(first_box)
        second_cx, _ = bbox_center(second_box)
        if first_cx <= second_cx:
            left_kpts, right_kpts = first_kpts, second_kpts
        else:
            left_kpts, right_kpts = second_kpts, first_kpts

        self.tracks[0] = _TrackState(0, left_kpts, self.frame_idx)
        self.tracks[1] = _TrackState(1, right_kpts, self.frame_idx)
        self.lost_tracks = {0: None, 1: None}
        self.miss_count = {0: 0, 1: 0}
        self.initialized = True
        return True

    def initialize_with_keypoints(self, left_kpts: np.ndarray, right_kpts: np.ndarray) -> bool:
        left_box = bbox_from_keypoints(left_kpts)
        right_box = bbox_from_keypoints(right_kpts)
        if left_box is None or right_box is None:
            return False
        left_cx, _ = bbox_center(left_box)
        right_cx, _ = bbox_center(right_box)
        if left_cx > right_cx:
            left_kpts, right_kpts = right_kpts, left_kpts
        self.tracks[0] = _TrackState(0, left_kpts, self.frame_idx)
        self.tracks[1] = _TrackState(1, right_kpts, self.frame_idx)
        self.lost_tracks = {0: None, 1: None}
        self.miss_count = {0: 0, 1: 0}
        self.initialized = True
        return True

    def _edge_penalty(self, bbox: Optional[Tuple[int, int, int, int]]) -> float:
        if bbox is None:
            return 1.0
        x1, y1, x2, y2 = bbox
        left_over = max(0.0, float(self.edge_margin - x1))
        right_over = max(0.0, float(x2 - (self.frame_w - self.edge_margin)))
        top_over = max(0.0, float(self.top_margin - y1))
        bottom_over = max(0.0, float(y2 - (self.frame_h - self.bottom_margin)))
        norm = max(float(max(self.frame_w, self.frame_h)), 1.0)
        return float((left_over + right_over + top_over + bottom_over) / norm)

    def _center_penalty(self, cx: float) -> float:
        if self.center_deadzone <= 1e-6:
            return 0.0
        hcenter = self.frame_w / 2.0
        dist = abs(cx - hcenter)
        if dist >= self.center_deadzone:
            return 0.0
        return float((self.center_deadzone - dist) / self.center_deadzone)

    def _side_penalty(self, tid: int, cx: float) -> float:
        hcenter = self.frame_w / 2.0
        half_w = max(self.frame_w / 2.0, 1.0)
        if tid == 0:
            return float(max(0.0, (cx - hcenter) / half_w))
        return float(max(0.0, (hcenter - cx) / half_w))

    def _soft_match_cost(
        self,
        tid: int,
        track: _TrackState,
        det_kpts: np.ndarray,
        meta: Dict[str, Any],
    ) -> Optional[float]:
        bbox = meta["bbox"]
        center = meta["center"]
        area = meta["area"]
        if bbox is None or center is None or area <= 0:
            return None

        pred_center = track.predict_center()
        base = composite_track_cost(det_kpts, track.kpts, track.bbox, pred_center, self.frame_diag)
        if not np.isfinite(base):
            return None

        area_penalty = 0.0
        prev_bbox = track.bbox
        if prev_bbox is not None:
            prev_area = max(float(bbox_area(prev_bbox)), 1.0)
            ratio = max(float(area) / prev_area, 1e-6)
            area_penalty = abs(math.log(ratio))

        anchor_posture_penalty = 0.0
        if track.anchor_kpts is not None:
            anchor_posture = temporal_posture_distance(det_kpts, track.anchor_kpts)
            if anchor_posture is None:
                anchor_posture_penalty = 1.0
            else:
                anchor_posture_penalty = float(anchor_posture)

        anchor_area_penalty = 0.0
        if track.anchor_bbox is not None:
            anchor_area = max(float(bbox_area(track.anchor_bbox)), 1.0)
            anchor_ratio = max(float(area) / anchor_area, 1e-6)
            anchor_area_penalty = abs(math.log(anchor_ratio))

        side_penalty = self._side_penalty(tid, float(center[0]))
        center_penalty = self._center_penalty(float(center[0]))
        edge_penalty = self._edge_penalty(bbox)

        total = base
        total += self.area_penalty_weight * area_penalty
        total += self.side_penalty_weight * side_penalty
        total += self.center_penalty_weight * center_penalty
        total += self.edge_penalty_weight * edge_penalty
        total += self.anchor_posture_weight * anchor_posture_penalty
        total += self.anchor_area_weight * anchor_area_penalty
        return float(total)

    def _assignment_cost_threshold(self, tid: int) -> float:
        if self.miss_count.get(tid, 0) > 0:
            return self.recovering_match_cost_threshold
        return self.active_match_cost_threshold

    def _mark_track_lost(self, tid: int):
        track = self.tracks.get(tid)
        if track is not None:
            self.lost_tracks[tid] = track
        self.tracks[tid] = None

    def _reacquire_reference_state(self, tid: int) -> Optional[_TrackState]:
        lost = self.lost_tracks.get(tid)
        if lost is not None:
            return lost
        ref_posture = self.left_ref_posture if tid == 0 else self.right_ref_posture
        if ref_posture is None:
            return None
        return _TrackState(tid, np.array(ref_posture, copy=True), self.frame_idx, anchor_kpts=np.array(ref_posture, copy=True))

    def _reacquire_missing_tracks(
        self,
        detections: List[np.ndarray],
        det_meta: List[Dict[str, Any]],
        used_det_indices: set,
    ):
        if not detections:
            return
        for tid in (0, 1):
            if self.tracks[tid] is not None:
                continue
            reference_track = self._reacquire_reference_state(tid)

            best_idx = None
            best_cost = float("inf")
            for d_idx, det_kpts in enumerate(detections):
                if d_idx in used_det_indices:
                    continue
                if reference_track is not None:
                    c = self._soft_match_cost(tid, reference_track, det_kpts, det_meta[d_idx])
                    if c is None:
                        continue
                else:
                    center = det_meta[d_idx]["center"]
                    bbox = det_meta[d_idx]["bbox"]
                    if center is None or bbox is None:
                        continue
                    c = self.side_penalty_weight * self._side_penalty(tid, float(center[0]))
                    c += self.center_penalty_weight * self._center_penalty(float(center[0]))
                    c += self.edge_penalty_weight * self._edge_penalty(bbox)
                if c < best_cost:
                    best_cost = c
                    best_idx = d_idx

            if best_idx is None or best_cost > self.reacquire_cost_threshold:
                continue
            anchor_kpts = reference_track.anchor_kpts if reference_track is not None else None
            self.tracks[tid] = _TrackState(tid, detections[best_idx], self.frame_idx, anchor_kpts=anchor_kpts)
            self.miss_count[tid] = 0
            used_det_indices.add(best_idx)

    def update(self, detections: List[np.ndarray]):
        if not self.initialized:
            self.initialize(detections)
            self.frame_idx += 1
            return
        if len(detections) == 0:
            for tid in (0, 1):
                if self.tracks[tid] is not None:
                    self.miss_count[tid] += 1
                if self.miss_count[tid] > self.max_miss:
                    self._mark_track_lost(tid)
            self.frame_idx += 1
            return
        det_meta = []
        for det_kpts in detections:
            bbox = bbox_from_keypoints(det_kpts)
            if bbox is None:
                det_meta.append(
                    {
                        "bbox": None,
                        "center": None,
                        "area": 0.0,
                    }
                )
                continue
            det_meta.append(
                {
                    "bbox": bbox,
                    "center": bbox_center(bbox),
                    "area": float(bbox_area(bbox)),
                }
            )
        costs = {}
        for tid in (0, 1):
            track = self.tracks[tid]
            if track is None:
                continue
            for d_idx, det_kpts in enumerate(detections):
                c = self._soft_match_cost(tid, track, det_kpts, det_meta[d_idx])
                if c is None:
                    continue
                costs[(tid, d_idx)] = c
        assignments = self._select_assignments(costs, det_meta, len(detections))
        used_det_indices = {d_idx for d_idx in assignments.values()}
        for tid in (0, 1):
            if tid in assignments:
                d_idx = assignments[tid]
                self._overwrite_track(self.tracks[tid], detections[d_idx])
                self.lost_tracks[tid] = None
                self.miss_count[tid] = 0
            else:
                if self.tracks[tid] is not None:
                    self.miss_count[tid] += 1
                    if self.miss_count[tid] > self.max_miss:
                        self._mark_track_lost(tid)
        self._reacquire_missing_tracks(detections, det_meta, used_det_indices)
        self.frame_idx += 1

    def _select_assignments(
        self,
        costs: Dict[Tuple[int, int], float],
        det_meta: List[Dict[str, Any]],
        num_detections: int,
    ) -> Dict[int, int]:
        active_tids = [tid for tid in (0, 1) if self.tracks[tid] is not None]
        if not active_tids:
            return {}

        options: Dict[int, List[Optional[int]]] = {}
        for tid in active_tids:
            valid = []
            match_threshold = self._assignment_cost_threshold(tid)
            for d_idx in range(num_detections):
                c = costs.get((tid, d_idx))
                if c is None:
                    continue
                if np.isfinite(c) and c <= min(self.max_candidate_cost, match_threshold):
                    valid.append((d_idx, c))
            valid.sort(key=lambda item: item[1])
            options[tid] = [None] + [d_idx for d_idx, _ in valid]

        if len(active_tids) == 1:
            tid = active_tids[0]
            best_idx = None
            best_total = self.unmatched_cost
            for d_idx in options[tid]:
                total = self.unmatched_cost if d_idx is None else costs[(tid, d_idx)]
                if total < best_total:
                    best_total = total
                    best_idx = d_idx
            return {tid: best_idx} if best_idx is not None else {}

        tid_left, tid_right = active_tids
        best_total = float("inf")
        best_assign: Dict[int, int] = {}

        for d_left in options[tid_left]:
            for d_right in options[tid_right]:
                if d_left is not None and d_right is not None and d_left == d_right:
                    continue

                if d_left is not None and d_right is not None:
                    c_left = det_meta[d_left]["center"]
                    c_right = det_meta[d_right]["center"]
                    if c_left is not None and c_right is not None:
                        crossing_violation = float(c_left[0] - c_right[0] - self.crossing_margin_px)
                        if crossing_violation > 0:
                            total_crossing = crossing_violation / max(self.frame_w * 0.25, 1.0)
                        else:
                            total_crossing = 0.0
                    else:
                        total_crossing = 0.0
                else:
                    total_crossing = 0.0

                total = 0.0
                total += self.unmatched_cost if d_left is None else costs[(tid_left, d_left)]
                total += self.unmatched_cost if d_right is None else costs[(tid_right, d_right)]
                total += self.crossing_penalty_weight * total_crossing

                if total < best_total:
                    best_total = total
                    best_assign = {}
                    if d_left is not None:
                        best_assign[tid_left] = d_left
                    if d_right is not None:
                        best_assign[tid_right] = d_right

        return best_assign

    def _overwrite_track(self, track: _TrackState, kpts: np.ndarray):
        track.prev_centroid = track.centroid
        track.kpts = kpts
        track.bbox = bbox_from_keypoints(kpts)
        track.centroid = kpt_centroid(kpts)
        track.last_seen = self.frame_idx

    def _maybe_update_track(self, track: Optional[_TrackState], kpts: np.ndarray, cost: float):
        if track is None or cost > self.unmatched_cost:
            return
        self._overwrite_track(track, kpts)

    def get_track(self, tid: int):
        t = self.tracks.get(tid)
        return t.kpts if t is not None else None


# ---------------------------------------------------------------------------
# Fisheye video correction helpers
# ---------------------------------------------------------------------------
def _fisheye_safe_cap(path: str):
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise FileNotFoundError(f"Cannot open video: {path}")
    return cap

def _fisheye_meta(path: str):
    cap = _fisheye_safe_cap(path)
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = cap.get(cv2.CAP_PROP_FPS)
    frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cap.release()
    return width, height, fps, frame_count

def _build_fisheye_maps(width: int, height: int, strength: float = -0.18, balance: float = 0.0):
    cx, cy = width / 2, height / 2
    fx = fy = max(width, height)
    K = np.array([[fx, 0, cx], [0, fy, cy], [0, 0, 1]], dtype=np.float64)
    D = np.array([strength, 0, 0, 0], dtype=np.float64)
    new_K = cv2.fisheye.estimateNewCameraMatrixForUndistortRectify(
        K, D, (width, height), np.eye(3), balance=balance
    )
    map1, map2 = cv2.fisheye.initUndistortRectifyMap(
        K, D, np.eye(3), new_K, (width, height), cv2.CV_16SC2
    )
    return map1, map2

def _auto_crop_from_maps(map1: np.ndarray, map2: np.ndarray, border_mode=cv2.BORDER_CONSTANT):
    h, w = map1.shape[:2]
    mask = np.ones((h, w), dtype=np.uint8) * 255
    warped = cv2.remap(mask, map1, map2, cv2.INTER_LINEAR, borderMode=border_mode, borderValue=0)
    coords = cv2.findNonZero(warped)
    if coords is None:
        return 0, 0, w, h
    x, y, cw, ch = cv2.boundingRect(coords)
    return x, y, cw, ch

def _resolve_ffmpeg_executable() -> Optional[str]:
    env_ffmpeg = os.environ.get("FFMPEG_BIN")
    if env_ffmpeg and os.path.exists(env_ffmpeg):
        return env_ffmpeg
    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        return system_ffmpeg
    try:
        import imageio_ffmpeg

        bundled_ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        bundled_ffmpeg = None
    if bundled_ffmpeg and os.path.exists(bundled_ffmpeg):
        return bundled_ffmpeg
    return None

def _mux_audio_if_any(original_path: str, corrected_path: str, out_path: str):
    ffprobe_bin = shutil.which("ffprobe")
    if ffprobe_bin is None:
        logger.warning("ffprobe not available; keeping video-only stream for %s", original_path)
        if corrected_path != out_path:
            shutil.move(corrected_path, out_path)
        return out_path

    probe = subprocess.run(
        [ffprobe_bin, "-v", "error", "-select_streams", "a:0", "-show_entries",
         "stream=codec_type", "-of", "default=noprint_wrappers=1:nokey=1", original_path],
        capture_output=True, text=True
    )
    has_audio = (probe.returncode == 0 and probe.stdout.strip() == "audio")
    if not has_audio:
        if corrected_path != out_path:
            shutil.move(corrected_path, out_path)
        return out_path

    ffmpeg_bin = _resolve_ffmpeg_executable()
    if ffmpeg_bin is None:
        logger.warning("ffmpeg not available; keeping video-only stream for %s", original_path)
        if corrected_path != out_path:
            shutil.move(corrected_path, out_path)
        return out_path

    temp_out = out_path + ".temp.mp4"
    cmd = [
        ffmpeg_bin, "-y", "-i", corrected_path, "-i", original_path,
        "-map", "0:v:0", "-map", "1:a:0?", "-c:v", "copy", "-c:a", "aac",
        "-movflags", "+faststart", temp_out
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        logger.warning("Audio mux failed, keeping video-only: %s", result.stderr)
        if corrected_path != out_path:
            shutil.move(corrected_path, out_path)
        return out_path
    if not validate_frame_preserving(corrected_path, temp_out):
        logger.warning("Audio mux changed video timing for %s; keeping video-only stream", original_path)
        if os.path.exists(temp_out):
            os.remove(temp_out)
        if corrected_path != out_path:
            shutil.move(corrected_path, out_path)
        return out_path
    if os.path.exists(corrected_path) and corrected_path != temp_out:
        os.remove(corrected_path)
    shutil.move(temp_out, out_path)
    return out_path

def correct_fisheye_video(
    input_path: str,
    output_path: Optional[str] = None,
    strength: float = -0.18,
    balance: float = 0.0,
    keep_audio: bool = True,
    border_mode: int = cv2.BORDER_CONSTANT,
    border_value: Tuple[int, int, int] = (0, 0, 0),
    progress: bool = False,
) -> str:
    if not os.path.exists(input_path):
        raise FileNotFoundError(f"Input not found: {input_path}")

    width, height, fps, frame_count = _fisheye_meta(input_path)
    logger.info(
        "Fisheye correction: %s (%dx%d @ %.3f FPS, %d frames, strength=%s, balance=%s)",
        input_path,
        width,
        height,
        fps,
        frame_count,
        strength,
        balance,
    )

    map1, map2 = _build_fisheye_maps(width, height, strength=strength, balance=balance)
    crop_x, crop_y, crop_w, crop_h = _auto_crop_from_maps(map1, map2, border_mode=border_mode)
    logger.debug("Fisheye crop ROI: x=%d, y=%d, w=%d, h=%d", crop_x, crop_y, crop_w, crop_h)

    cap = _fisheye_safe_cap(input_path)

    if output_path is None:
        out_dir = Path(input_path).parent
        out_name = f"{Path(input_path).stem}_corrected.mp4"
        output_path = str(out_dir / out_name)

    tmp_out = output_path if output_path.endswith(".mp4") else output_path + ".mp4"
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(tmp_out, fourcc, fps, (crop_w, crop_h))
    if not writer.isOpened():
        cap.release()
        raise RuntimeError(f"Could not open writer for {tmp_out}")

    iterator = range(frame_count)
    for _ in tqdm(iterator, desc="FisheyeUndistort", disable=not progress):
        ok, frame = cap.read()
        if not ok:
            break
        undistorted = cv2.remap(
            frame,
            map1,
            map2,
            interpolation=cv2.INTER_LINEAR,
            borderMode=border_mode,
            borderValue=border_value,
        )
        roi = undistorted[crop_y : crop_y + crop_h, crop_x : crop_x + crop_w]
        writer.write(roi)

    cap.release()
    writer.release()

    if keep_audio:
        logger.debug("Attempting audio mux for %s", input_path)
        final_path = _mux_audio_if_any(input_path, tmp_out, output_path)
    else:
        final_path = tmp_out if tmp_out == output_path else shutil.move(tmp_out, output_path)

    logger.info("Fisheye correction complete: %s", final_path)
    return str(final_path)

SKELETON = [
    (15, 13), (13, 11), (16, 14), (14, 12), (11, 12),
    (5, 11), (6, 12), (5, 6),
    (5, 7), (7, 9), (6, 8), (8, 10),
    (11, 13), (13, 15), (12, 14), (14, 16),
]

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


def torch_runtime_device() -> torch.device:
    return torch.device("cuda:0" if torch.cuda.is_available() else "cpu")


def ultralytics_device_arg() -> int | str:
    # Ultralytics accepts a CUDA device index for GPU inference.
    return 0 if torch.cuda.is_available() else "cpu"


def ultralytics_predict_kwargs(*, verbose: bool, conf: Optional[float] = None) -> Dict[str, Any]:
    kwargs: Dict[str, Any] = {
        "verbose": verbose,
        "device": ultralytics_device_arg(),
    }
    if conf is not None:
        kwargs["conf"] = float(conf)
    return kwargs

def fill_with_linear_regression(data, c):
    """
    Fill NaN values using linear regression from neighboring valid points
    """
    filled = data.copy()
    for col in filled.columns:
        series = filled[col]
        if series.isna().all():
            continue
        nan_indices = series.index[series.isna()].tolist()
        if not nan_indices:
            continue
        valid_indices = series.index[~series.isna()].tolist()
        if len(valid_indices) < 2:
            filled[col].fillna(0, inplace=True)
            continue
        valid_x = np.array(valid_indices)
        valid_y = series.loc[valid_indices].values
        for nan_idx in nan_indices:
            before = [i for i in valid_indices if i < nan_idx]
            after = [i for i in valid_indices if i > nan_idx]
            if before and after:
                x1, y1 = before[-1], series.loc[before[-1]]
                x2, y2 = after[0], series.loc[after[0]]
                slope = (y2 - y1) / (x2 - x1) if x2 != x1 else 0
                filled.loc[nan_idx, col] = y1 + slope * (nan_idx - x1)
            elif before:
                filled.loc[nan_idx, col] = series.loc[before[-1]]
            elif after:
                filled.loc[nan_idx, col] = series.loc[after[0]]
            else:
                filled.loc[nan_idx, col] = 0
    return filled


def _interpolate_short_internal_gaps(
    xdata: Dict[int, List[float]],
    ydata: Dict[int, List[float]],
    max_gap: int,
) -> Tuple[Dict[int, List[float]], Dict[int, List[float]]]:
    if max_gap <= 0:
        return xdata, ydata

    xdf = pd.DataFrame(xdata, dtype=float)
    ydf = pd.DataFrame(ydata, dtype=float)
    xdf = xdf.interpolate(method="linear", axis=0, limit=max_gap, limit_area="inside")
    ydf = ydf.interpolate(method="linear", axis=0, limit=max_gap, limit_area="inside")
    return (
        {int(col): xdf[col].tolist() for col in xdf.columns},
        {int(col): ydf[col].tolist() for col in ydf.columns},
    )

def extract_tracks_from_video(
    video_path,
    model,
    initial_detection_indices: Optional[Tuple[int, int]] = None,
    initial_detection_bboxes: Optional[Tuple[Tuple[float, float, float, float], Tuple[float, float, float, float]]] = None,
    initial_frame_index: int = 0,
    detection_conf: Optional[float] = None,
    detection_verbose: bool = False,
):
    """
    Extract YOLO pose tracks from video using persistent two-fencer tracking.
    Optional forced initialization can be delayed until `initial_frame_index`
    so callers can bootstrap from a short early window instead of frame 0.
    Returns: list of tracks per frame
    """
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise ValueError(f"Cannot open video: {video_path}")

    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    tracker = TwoFencerTracker(frame_w=width, frame_h=height)
    tracks_per_frame = []
    predict_kwargs = ultralytics_predict_kwargs(verbose=detection_verbose, conf=detection_conf)

    def _target_det_score(target_box, det_box) -> float:
        if det_box is None:
            return -1.0
        target_i = tuple(int(round(v)) for v in target_box)
        iou = bbox_iou(target_i, det_box)
        tcx, tcy = bbox_center(target_i)
        dcx, dcy = bbox_center(det_box)
        center_dist = math.hypot(tcx - dcx, tcy - dcy) / max(tracker.frame_diag, 1.0)
        return float(iou - 0.25 * center_dist)

    def _match_initial_boxes_to_detection_indices(
        detections: List[np.ndarray],
        target_boxes: Tuple[Tuple[float, float, float, float], Tuple[float, float, float, float]],
    ) -> Optional[Tuple[int, int]]:
        det_boxes: List[Optional[Tuple[int, int, int, int]]] = [bbox_from_keypoints(det) for det in detections]
        if len(det_boxes) < 2:
            return None

        left_target, right_target = target_boxes
        left_ranked = sorted(
            ((idx, _target_det_score(left_target, det_box)) for idx, det_box in enumerate(det_boxes)),
            key=lambda item: item[1],
            reverse=True,
        )
        right_ranked = sorted(
            ((idx, _target_det_score(right_target, det_box)) for idx, det_box in enumerate(det_boxes)),
            key=lambda item: item[1],
            reverse=True,
        )

        if not left_ranked or not right_ranked:
            return None
        if left_ranked[0][1] <= 0 or right_ranked[0][1] <= 0:
            return None

        left_idx = left_ranked[0][0]
        right_idx = right_ranked[0][0]
        if left_idx == right_idx:
            fallback_left = next((idx for idx, score in left_ranked[1:] if score > 0 and idx != right_idx), None)
            fallback_right = next((idx for idx, score in right_ranked[1:] if score > 0 and idx != left_idx), None)
            if fallback_left is not None:
                left_idx = fallback_left
            elif fallback_right is not None:
                right_idx = fallback_right
            else:
                return None

        return (int(left_idx), int(right_idx))

    def _recover_initial_keypoints_from_crops(
        frame_bgr: np.ndarray,
        target_boxes: Tuple[Tuple[float, float, float, float], Tuple[float, float, float, float]],
    ) -> Optional[Tuple[np.ndarray, np.ndarray]]:
        def _recover_one(target_box: Tuple[float, float, float, float]) -> Optional[np.ndarray]:
            x1, y1, x2, y2 = [float(v) for v in target_box]
            pad = max(48, int(round(max(x2 - x1, y2 - y1) * 0.35)))
            ix1 = max(0, int(math.floor(x1)) - pad)
            iy1 = max(0, int(math.floor(y1)) - pad)
            ix2 = min(width, int(math.ceil(x2)) + pad)
            iy2 = min(height, int(math.ceil(y2)) + pad)
            if ix2 <= ix1 or iy2 <= iy1:
                return None

            crop = frame_bgr[iy1:iy2, ix1:ix2]
            if crop.size == 0:
                return None

            crop_results = model(crop, **predict_kwargs)
            if len(crop_results) == 0 or crop_results[0].keypoints is None:
                return None

            crop_kpts = crop_results[0].keypoints.xy
            if hasattr(crop_kpts, "cpu"):
                crop_kpts = crop_kpts.cpu().numpy()
            else:
                crop_kpts = np.array(crop_kpts)

            best_kpts = None
            best_score = -1.0
            for det_kpts in crop_kpts:
                global_kpts = np.array(det_kpts, copy=True)
                valid = (global_kpts[:, 0] > 0) & (global_kpts[:, 1] > 0)
                global_kpts[valid, 0] += ix1
                global_kpts[valid, 1] += iy1
                det_box = bbox_from_keypoints(global_kpts)
                score = _target_det_score(target_box, det_box)
                if score > best_score:
                    best_score = score
                    best_kpts = global_kpts

            return best_kpts if best_score > 0 else None

        left_target, right_target = target_boxes
        left_kpts = _recover_one(left_target)
        right_kpts = _recover_one(right_target)
        if left_kpts is None or right_kpts is None:
            return None
        return left_kpts, right_kpts

    def _augment_detections_with_track_crops(
        frame_bgr: np.ndarray,
        detections: List[np.ndarray],
    ) -> Tuple[List[np.ndarray], Dict[int, np.ndarray]]:
        augmented = list(detections)
        recovered_by_tid: Dict[int, np.ndarray] = {}
        if not tracker.initialized:
            return augmented, recovered_by_tid

        def _meta_for(det_kpts: np.ndarray) -> Optional[Dict[str, Any]]:
            bbox = bbox_from_keypoints(det_kpts)
            if bbox is None:
                return None
            return {
                "bbox": bbox,
                "center": bbox_center(bbox),
                "area": float(bbox_area(bbox)),
            }

        for tid in (0, 1):
            track = tracker.tracks.get(tid)
            if track is None or track.bbox is None:
                continue

            best_existing_cost = float("inf")
            for det_kpts in augmented:
                meta = _meta_for(det_kpts)
                if meta is None:
                    continue
                cost = tracker._soft_match_cost(tid, track, det_kpts, meta)
                if cost is not None:
                    best_existing_cost = min(best_existing_cost, cost)

            if best_existing_cost <= tracker.reacquire_cost_threshold:
                continue

            x1, y1, x2, y2 = track.bbox
            pred_cx, pred_cy = track.predict_center()
            box_w = max(float(x2 - x1), 1.0)
            box_h = max(float(y2 - y1), 1.0)
            half_w = max(110.0, box_w * 2.1)
            half_h = max(140.0, box_h * 1.5)
            ix1 = max(0, int(math.floor(pred_cx - half_w)))
            iy1 = max(0, int(math.floor(pred_cy - half_h)))
            ix2 = min(width, int(math.ceil(pred_cx + half_w)))
            iy2 = min(height, int(math.ceil(pred_cy + half_h)))
            if ix2 <= ix1 or iy2 <= iy1:
                continue

            crop = frame_bgr[iy1:iy2, ix1:ix2]
            if crop.size == 0:
                continue

            crop_results = model(crop, **predict_kwargs)
            if len(crop_results) == 0 or crop_results[0].keypoints is None:
                continue

            crop_kpts = crop_results[0].keypoints.xy
            if hasattr(crop_kpts, "cpu"):
                crop_kpts = crop_kpts.cpu().numpy()
            else:
                crop_kpts = np.array(crop_kpts)

            best_det = None
            best_cost = float("inf")
            for det_kpts in crop_kpts:
                global_kpts = np.array(det_kpts, copy=True)
                valid = (global_kpts[:, 0] > 0) & (global_kpts[:, 1] > 0)
                global_kpts[valid, 0] += ix1
                global_kpts[valid, 1] += iy1
                meta = _meta_for(global_kpts)
                if meta is None:
                    continue
                cost = tracker._soft_match_cost(tid, track, global_kpts, meta)
                if cost is None:
                    continue
                if cost < best_cost:
                    best_cost = cost
                    best_det = global_kpts

            if best_det is None or best_cost > tracker.max_candidate_cost:
                continue

            best_bbox = bbox_from_keypoints(best_det)
            if best_bbox is None:
                continue
            recovered_by_tid[tid] = best_det
            duplicate = False
            for det_kpts in augmented:
                det_bbox = bbox_from_keypoints(det_kpts)
                if det_bbox is None:
                    continue
                if bbox_iou(best_bbox, det_bbox) > 0.7:
                    duplicate = True
                    break
            if not duplicate:
                augmented.append(best_det)

        return augmented, recovered_by_tid

    frame_idx = 0
    while True:
        ret, frame = cap.read()
        if not ret:
            break

        height, width = frame.shape[:2]
        results = model(frame, **predict_kwargs)
        detections = []
        if len(results) > 0 and results[0].keypoints is not None:
            kpts = results[0].keypoints.xy
            if hasattr(kpts, "cpu"):
                kpts = kpts.cpu().numpy()
            else:
                kpts = np.array(kpts)
            for i in range(kpts.shape[0]):
                detections.append(kpts[i])

        recovered_by_tid: Dict[int, np.ndarray] = {}
        if frame_idx > 0 and tracker.initialized:
            detections, recovered_by_tid = _augment_detections_with_track_crops(frame, detections)

        forced_init_pending = (
            not tracker.initialized
            and frame_idx < initial_frame_index
            and (initial_detection_indices is not None or initial_detection_bboxes is not None)
        )
        if forced_init_pending:
            tracks_per_frame.append([
                {
                    'track_id': tid,
                    'keypoints': None,
                    'box': None,
                    'observed': False,
                }
                for tid in (0, 1)
            ])
            frame_idx += 1
            continue

        forced_init_applied = False
        if (
            frame_idx == initial_frame_index
            and not tracker.initialized
        ):
            chosen_indices = initial_detection_indices
            recovered_keypoints = None
            if chosen_indices is None and initial_detection_bboxes is not None:
                chosen_indices = _match_initial_boxes_to_detection_indices(detections, initial_detection_bboxes)
                if chosen_indices is None:
                    recovered_keypoints = _recover_initial_keypoints_from_crops(frame, initial_detection_bboxes)
                    if recovered_keypoints is None:
                        logger.warning(
                            "SAM-derived initial boxes could not be matched to detections for %s; "
                            "falling back to auto-initialization.",
                            video_path,
                        )
                    else:
                        forced = tracker.initialize_with_keypoints(recovered_keypoints[0], recovered_keypoints[1])
                        if not forced:
                            logger.warning(
                                "SAM crop recovery could not initialize tracker for %s; "
                                "falling back to auto-initialization.",
                                video_path,
                            )
                        else:
                            forced_init_applied = True

            if not forced_init_applied and chosen_indices is not None:
                forced = tracker.initialize_with_detection_indices(
                    detections,
                    int(chosen_indices[0]),
                    int(chosen_indices[1]),
                )
                if not forced:
                    logger.warning(
                        "Manual tracking indices %s could not initialize tracker for %s; "
                        "falling back to auto-initialization.",
                        chosen_indices,
                        video_path,
                    )
                else:
                    forced_init_applied = True

        if forced_init_applied:
            # Preserve user-selected detections exactly on frame 0.
            tracker.frame_idx += 1
        else:
            tracker.update(detections)
            current_frame_idx = tracker.frame_idx - 1
            for tid, recovered_kpts in recovered_by_tid.items():
                track = tracker.tracks.get(tid)
                if track is not None and track.last_seen == current_frame_idx:
                    continue
                if track is None:
                    tracker.tracks[tid] = _TrackState(tid, recovered_kpts, current_frame_idx)
                else:
                    tracker._overwrite_track(track, recovered_kpts)
                tracker.lost_tracks[tid] = None
                tracker.miss_count[tid] = 0

        current_frame_idx = tracker.frame_idx - 1
        frame_tracks = []
        for tid in (0, 1):
            track = tracker.tracks.get(tid)
            observed = track is not None and track.last_seen == current_frame_idx
            kpts = tracker.get_track(tid) if observed else None
            bbox = bbox_from_keypoints(kpts) if kpts is not None else None
            frame_tracks.append({
                'track_id': tid,
                'keypoints': kpts.copy() if kpts is not None else None,
                'box': np.array(bbox, dtype=float) if bbox else None,
                'observed': observed,
            })

        tracks_per_frame.append(frame_tracks)
        frame_idx += 1

    cap.release()
    return tracks_per_frame

def process_video_and_extract_data(tracks_per_frame, interpolate_max_gap: int = 2):
    """
    Process tracks and extract normalized keypoint data
    Ensures consistent left/right fencer assignment and foot swapping
    """
    left_xdata = {k: [] for k in range(17)}
    left_ydata = {k: [] for k in range(17)}
    right_xdata = {k: [] for k in range(17)}
    right_ydata = {k: [] for k in range(17)}
    video_angle = ''
    c = None
    
    # Find the first frame with keypoints for both tracks
    for tracks in tracks_per_frame:
        track_map = {t['track_id']: t for t in tracks}
        left_track = track_map.get(0)
        right_track = track_map.get(1)
        if not left_track or not right_track:
            continue

        k0 = left_track.get('keypoints')
        k1 = right_track.get('keypoints')
        if k0 is None or k1 is None:
            continue
        if len(k0) < 17 or len(k1) < 17:
            continue
        if k0[15][0] <= 0 or k0[16][0] <= 0 or k1[15][0] <= 0 or k1[16][0] <= 0:
            continue

        values = [k0[15][0], k0[16][0], k1[15][0], k1[16][0]]
        sorted_values = sorted(values, reverse=True)
        b = sorted_values[1]
        a = sorted_values[2]
        c = abs((b - a) / 4)

        bbox_left = left_track.get('box')
        bbox_right = right_track.get('box')
        if bbox_left is None and k0 is not None:
            bbox_left = np.array(bbox_from_keypoints(k0), dtype=float)
        if bbox_right is None and k1 is not None:
            bbox_right = np.array(bbox_from_keypoints(k1), dtype=float)

        if bbox_left is not None and bbox_right is not None:
            left_box_area = (bbox_left[2] - bbox_left[0]) * (bbox_left[3] - bbox_left[1])
            right_box_area = (bbox_right[2] - bbox_right[0]) * (bbox_right[3] - bbox_right[1])

            if left_box_area >= 1.75 * right_box_area:
                video_angle = 'left'
            elif right_box_area >= 1.75 * left_box_area:
                video_angle = 'right'
            else:
                video_angle = 'middle'
        break
    
    if c is None:
        raise ValueError("No valid frame with keypoints for both tracks found in the video")
    
    # Extract data for all frames
    def _append_side_nan(xdata, ydata):
        for j in range(17):
            xdata[j].append(np.nan)
            ydata[j].append(np.nan)

    def _append_side_keypoints(
        keypoints: Optional[np.ndarray],
        xdata: Dict[int, List[float]],
        ydata: Dict[int, List[float]],
        is_left: bool,
    ):
        if keypoints is None or len(keypoints) < 17:
            _append_side_nan(xdata, ydata)
            return

        points = np.array(keypoints, copy=True)
        pairs = [(1, 2), (3, 4), (5, 6), (7, 8), (9, 10), (11, 12), (13, 14), (15, 16)]
        for kp1, kp2 in pairs:
            if kp1 >= len(points) or kp2 >= len(points):
                continue
            if is_left and points[kp1][0] > points[kp2][0]:
                points[[kp1, kp2]] = points[[kp2, kp1]]
            elif not is_left and points[kp1][0] < points[kp2][0]:
                points[[kp1, kp2]] = points[[kp2, kp1]]

        for j in range(17):
            if points[j][0] > 0 and points[j][1] > 0:
                xdata[j].append(points[j][0] / c)
                ydata[j].append(points[j][1] / c)
            else:
                xdata[j].append(np.nan)
                ydata[j].append(np.nan)

    for tracks in tracks_per_frame:
        track_map = {t['track_id']: t for t in tracks}
        left_track = track_map.get(0)
        right_track = track_map.get(1)

        left_kpts = left_track.get('keypoints') if left_track else None
        right_kpts = right_track.get('keypoints') if right_track else None

        bbox_left = left_track.get('box')
        if bbox_left is None and left_kpts is not None:
            lb = bbox_from_keypoints(left_kpts)
            bbox_left = np.array(lb, dtype=float) if lb is not None else None

        bbox_right = right_track.get('box')
        if bbox_right is None and right_kpts is not None:
            rb = bbox_from_keypoints(right_kpts)
            bbox_right = np.array(rb, dtype=float) if rb is not None else None

        if bbox_left is not None and bbox_right is not None:
            center_left = (bbox_left[0] + bbox_left[2]) / 2
            center_right = (bbox_right[0] + bbox_right[2]) / 2
            if center_left > center_right:
                left_kpts, right_kpts = right_kpts, left_kpts

        _append_side_keypoints(left_kpts, left_xdata, left_ydata, is_left=True)
        _append_side_keypoints(right_kpts, right_xdata, right_ydata, is_left=False)
    
    left_xdata, left_ydata = _interpolate_short_internal_gaps(left_xdata, left_ydata, interpolate_max_gap)
    right_xdata, right_ydata = _interpolate_short_internal_gaps(right_xdata, right_ydata, interpolate_max_gap)
    return left_xdata, left_ydata, right_xdata, right_ydata, c, video_angle

def save_keypoints_to_excel(left_xdata, left_ydata, right_xdata, right_ydata, output_path):
    """
    Save keypoint data to Excel file with 4 sheets
    """
    with pd.ExcelWriter(output_path, engine='openpyxl') as writer:
        pd.DataFrame(left_xdata).to_excel(writer, sheet_name='Left_X', index=False)
        pd.DataFrame(left_ydata).to_excel(writer, sheet_name='Left_Y', index=False)
        pd.DataFrame(right_xdata).to_excel(writer, sheet_name='Right_X', index=False)
        pd.DataFrame(right_ydata).to_excel(writer, sheet_name='Right_Y', index=False)

def _get_skeleton_connections():
    """Return COCO keypoint connections."""
    return [
        (15, 13), (13, 11), (16, 14), (14, 12), (11, 12),
        (5, 11), (6, 12), (5, 6),
        (5, 7), (7, 9), (6, 8), (8, 10),
        (1, 2), (0, 1), (0, 2), (1, 3), (2, 4),
        (3, 5), (4, 6)
    ]

def _denormalize_keypoint(x: float, y: float, c: float):
    if np.isnan(x) or np.isnan(y):
        return None
    return (int(x * c), int(y * c))

def _draw_keypoints_on_frame(
    frame: np.ndarray,
    left_xdata: Dict[int, List[float]],
    left_ydata: Dict[int, List[float]],
    right_xdata: Dict[int, List[float]],
    right_ydata: Dict[int, List[float]],
    frame_idx: int,
    c_value: float,
    draw_skeleton: bool,
    draw_labels: bool,
):
    """Draw keypoints and skeleton on a single frame."""
    skeleton = _get_skeleton_connections()
    
    # Draw left fencer (blue)
    if frame_idx < len(left_xdata[0]):
        points = {}
        for kp_idx in range(17):
            x = left_xdata[kp_idx][frame_idx]
            y = left_ydata[kp_idx][frame_idx]
            pt = _denormalize_keypoint(x, y, c_value)
            if pt is not None:
                points[kp_idx] = pt
                cv2.circle(frame, pt, 3, (255, 0, 0), -1)
                if draw_labels:
                    cv2.putText(frame, str(kp_idx), pt, cv2.FONT_HERSHEY_SIMPLEX, 0.3, (255, 255, 255), 1)
        
        if draw_skeleton:
            for kp1, kp2 in skeleton:
                if kp1 in points and kp2 in points:
                    cv2.line(frame, points[kp1], points[kp2], (255, 0, 0), 2)
    
    # Draw right fencer (green)
    if frame_idx < len(right_xdata[0]):
        points = {}
        for kp_idx in range(17):
            x = right_xdata[kp_idx][frame_idx]
            y = right_ydata[kp_idx][frame_idx]
            pt = _denormalize_keypoint(x, y, c_value)
            if pt is not None:
                points[kp_idx] = pt
                cv2.circle(frame, pt, 3, (0, 255, 0), -1)
                if draw_labels:
                    cv2.putText(frame, str(kp_idx), pt, cv2.FONT_HERSHEY_SIMPLEX, 0.3, (255, 255, 255), 1)
        
        if draw_skeleton:
            for kp1, kp2 in skeleton:
                if kp1 in points and kp2 in points:
                    cv2.line(frame, points[kp1], points[kp2], (0, 255, 0), 2)
    
    return frame


def _draw_frame_counter(frame: np.ndarray, frame_idx: int, total_frames: int):
    label = f"Frame {frame_idx + 1}/{total_frames}"
    origin = (12, 28)
    cv2.putText(frame, label, origin, cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 0), 3, cv2.LINE_AA)
    cv2.putText(frame, label, origin, cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 1, cv2.LINE_AA)
    return frame


def render_overlay_video(
    video_path: Path,
    output_path: Path,
    left_xdata: Dict[int, List[float]],
    left_ydata: Dict[int, List[float]],
    right_xdata: Dict[int, List[float]],
    right_ydata: Dict[int, List[float]],
    normalisation_constant: float,
    draw_skeleton: bool = True,
    draw_labels: bool = False,
    draw_frame_counter: bool = True,
    show_progress: bool = False,
):
    """Generate an overlay video with keypoints drawn on each frame."""
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise ValueError(f"Cannot open video: {video_path}")
    
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = cap.get(cv2.CAP_PROP_FPS)
    frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    
    fourcc = cv2.VideoWriter_fourcc(*'mp4v')
    out = cv2.VideoWriter(str(output_path), fourcc, fps, (width, height))
    
    if not out.isOpened():
        cap.release()
        raise RuntimeError(f"Could not open writer for {output_path}")
    
    frame_idx = 0
    iterator = range(frame_count) if not show_progress else tqdm(range(frame_count), desc="Rendering overlay")
    
    for _ in iterator:
        ret, frame = cap.read()
        if not ret:
            break
        
        frame = _draw_keypoints_on_frame(
            frame, left_xdata, left_ydata, right_xdata, right_ydata,
            frame_idx, normalisation_constant, draw_skeleton, draw_labels
        )
        if draw_frame_counter:
            frame = _draw_frame_counter(frame, frame_idx, frame_count)
        
        out.write(frame)
        frame_idx += 1
    
    cap.release()
    out.release()

def build_decision_summary(decision: Dict[str, Any], phrase: FencingPhrase):
    """Prepare structured context for natural language explanation."""
    winner = decision.get("winner", "unknown")
    reason = decision.get("reason", "")
    
    left_pauses = decision.get("left_pauses", [])
    right_pauses = decision.get("right_pauses", [])
    
    blade_analysis = decision.get("blade_analysis", "")
    speed_comparison = decision.get("speed_comparison", {})
    
    summary = f"Winner: {winner}\nReason: {reason}\n"
    summary += f"Left pauses: {len(left_pauses)}, Right pauses: {len(right_pauses)}\n"
    summary += f"Blade analysis: {blade_analysis}\n"
    summary += f"Speed comparison: {speed_comparison}\n"
    
    return summary

def generate_gemini_reason(decision: Dict[str, Any], phrase: FencingPhrase):
    """Use Gemini to craft a one-sentence fencing explanation."""
    try:
        import google.generativeai as genai
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel(GEMINI_MODEL)
        
        summary = build_decision_summary(decision, phrase)
        prompt = f"Based on this fencing analysis, provide a one-sentence explanation of the decision:\n{summary}"
        
        response = model.generate_content(prompt)
        return response.text.strip()
    except Exception as e:
        logger.warning(f"Gemini API call failed: {e}")
        return decision.get("reason", "")

def process_all_videos(base_path, model):
    """
    Process all videos in the training_data folder structure
    """
    base_path = Path(base_path)
    results = []
    
    for video_dir in base_path.iterdir():
        if not video_dir.is_dir():
            continue
        
        video_files = list(video_dir.glob("*.avi")) + list(video_dir.glob("*.mp4"))
        txt_files = list(video_dir.glob("*.txt"))
        
        if not video_files or not txt_files:
            continue
        
        video_path = video_files[0]
        txt_path = txt_files[0]
        
        try:
            tracks = extract_tracks_from_video(str(video_path), model)
            left_x, left_y, right_x, right_y, c, angle = process_video_and_extract_data(tracks)
            
            excel_path = video_dir / f"{video_path.stem}_keypoints.xlsx"
            save_keypoints_to_excel(left_x, left_y, right_x, right_y, str(excel_path))
            
            phrase = parse_txt_file(str(txt_path), video_path=str(video_path))
            decision = referee_decision(phrase, left_x, left_y, right_x, right_y, c)
            
            results.append({
                'video': str(video_path),
                'decision': decision,
                'phrase': phrase
            })
            
        except Exception as e:
            logger.error(f"Error processing {video_path}: {e}")
    
    return results


"""
AI Fencing Referee Analysis System

IMPORTANT POSITION MAPPING:
- Fencer 1 = Right fencer = Right side of screen = right_xdata/right_ydata
- Fencer 2 = Left fencer = Left side of screen = left_xdata/left_ydata

In TXT files:
- "Right Fencer" = Fencer 1 = right_xdata/right_ydata
- "Left Fencer" = Fencer 2 = left_xdata/left_ydata

Movement directions:
- Left fencer (Fencer 2) advances right (+x direction)
- Right fencer (Fencer 1) advances left (-x direction)

Weapon hands (fencers face each other):
- Left fencer (Fencer 2): weapon in right hand (keypoint 10)
- Right fencer (Fencer 1): weapon in left hand (keypoint 9)
"""

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

@dataclass
class FencingPhrase:
    """Contains all data for a fencing phrase
    Important: Fencer 1 = Right fencer, Fencer 2 = Left fencer"""
    start_time: float
    start_frame: int
    simultaneous_hit_time: Optional[float]
    simultaneous_hit_frame: Optional[int]
    blade_contacts: List[BladeContact]
    lockout_start: Optional[float]
    declared_winner: str
    fps: float = 0.0

@dataclass
class AnalysisResult:
    """Container for a single video/txt analysis run."""
    phrase: FencingPhrase
    decision: Optional[Dict[str, Any]]
    frames_processed: int
    processing_time: float
    video_angle: Optional[str]
    normalisation_constant: Optional[float]
    left_xdata: Optional[Dict[int, List[float]]] = None
    left_ydata: Optional[Dict[int, List[float]]] = None
    right_xdata: Optional[Dict[int, List[float]]] = None
    right_ydata: Optional[Dict[int, List[float]]] = None
    video_path: Optional[str] = None
    txt_path: Optional[str] = None
    excel_path: Optional[str] = None
    input_signal_path: Optional[str] = None
    natural_reason: Optional[str] = None
    lunge_detected: Optional[Dict[str, bool]] = None
    artifacts: Dict[str, str] = field(default_factory=dict)

    def to_dict(
        self,
        include_keypoints: bool = False,
    ):
        """Convert to dictionary for JSON serialization."""
        result = {
            'phrase': asdict(self.phrase),
            'decision': self.decision,
            'frames_processed': self.frames_processed,
            'processing_time': self.processing_time,
            'video_angle': self.video_angle,
            'normalisation_constant': self.normalisation_constant,
            'video_path': str(self.video_path) if self.video_path else None,
            'txt_path': str(self.txt_path) if self.txt_path else None,
            'excel_path': str(self.excel_path) if self.excel_path else None,
            'input_signal_path': str(self.input_signal_path) if self.input_signal_path else None,
            'natural_reason': self.natural_reason,
            'lunge_detected': self.lunge_detected,
            'artifacts': dict(self.artifacts),
        }
        
        if include_keypoints:
            result['left_xdata'] = self.left_xdata
            result['left_ydata'] = self.left_ydata
            result['right_xdata'] = self.right_xdata
            result['right_ydata'] = self.right_ydata
        
        return sanitize_for_json(result)

def parse_txt_file(
    txt_path: str,
    fps: Optional[float] = None,
    video_path: Optional[str] = None,
) -> FencingPhrase:
    """Parse the TXT file to extract timing information.
    
    Updated scoring rule:
    - Treat the phrase as a double touch only when the scoreboard line reports
      ``Scores -> Fencer 1: HIT, Fencer 2: HIT`` (case-insensitive).
    - Any other scoreboard combination is considered a single-light phrase and
      should be skipped by downstream processing.
    """
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
                t = float(match.group(1))
                blade_contacts.append(BladeContact(time=t, frame=0))

        if "Lockout period started" in line:
            match = re.search(r'(\d+\.\d+)s', line)
            if match:
                lockout_start = float(match.group(1))

        if "HIT:" in line:
            match = re.search(r'(\d+\.\d+)s', line)
            if match:
                hit_events.append(float(match.group(1)))

        if "Scores ->" in line:
            match_f1 = re.search(r'Fencer 1:\s*(\w+)', line, re.IGNORECASE)
            match_f2 = re.search(r'Fencer 2:\s*(\w+)', line, re.IGNORECASE)
            if match_f1:
                scoreboard_f1 = match_f1.group(1).strip().upper()
            if match_f2:
                scoreboard_f2 = match_f2.group(1).strip().upper()

        if re.search(r'(Confirmed result winner|Manual selection winner):\s*(Right|Left)', line, re.IGNORECASE):
            match = re.search(r'(Right|Left)', line, re.IGNORECASE)
            if match:
                declared_winner = match.group(1).lower()

    if start_time is None:
        raise ValueError(f"Could not find start time in {txt_path}")

    if scoreboard_f1 != "HIT" or scoreboard_f2 != "HIT":
        raise ValueError(
            f"Skipping {txt_path}: Not a double-touch phrase "
            f"(Fencer 1: {scoreboard_f1}, Fencer 2: {scoreboard_f2})"
        )

    if simultaneous_hit_time is None:
        if hit_events:
            simultaneous_hit_time = hit_events[0]
        else:
            raise ValueError(f"Could not find simultaneous hit time in {txt_path}")

    resolved_video_path = resolve_phrase_video_path(txt_path, explicit_video_path=video_path)
    effective_fps = float(fps) if fps and fps > 0 else None
    if resolved_video_path is not None:
        effective_fps = infer_video_fps(str(resolved_video_path))
        for blade_contact in blade_contacts:
            blade_contact.frame = map_time_to_frame_index(
                blade_contact.time,
                resolved_video_path,
                mode="containing",
            )
        start_frame = map_time_to_frame_index(start_time, resolved_video_path, mode="containing")
        simultaneous_hit_frame = map_time_to_frame_index(
            simultaneous_hit_time,
            resolved_video_path,
            mode="containing",
        )
    else:
        effective_fps = effective_fps or 15.0
        for blade_contact in blade_contacts:
            blade_contact.frame = int(blade_contact.time * effective_fps)
        start_frame = int(start_time * effective_fps)
        simultaneous_hit_frame = int(simultaneous_hit_time * effective_fps)

    return FencingPhrase(
        start_time=start_time,
        start_frame=start_frame,
        simultaneous_hit_time=simultaneous_hit_time,
        simultaneous_hit_frame=simultaneous_hit_frame,
        blade_contacts=blade_contacts,
        lockout_start=lockout_start,
        declared_winner=declared_winner or "unknown",
        fps=effective_fps or 15.0
    )

def load_keypoints_from_excel(excel_path: str):
    """
    Load keypoint data from Excel file
    
    Returns: (left_xdata, left_ydata, right_xdata, right_ydata)
    Where:
    - left_xdata, left_ydata = Fencer 2 (left side of screen)
    - right_xdata, right_ydata = Fencer 1 (right side of screen)
    """
    xl = pd.ExcelFile(excel_path)
    left_x = xl.parse('Left_X')
    left_y = xl.parse('Left_Y')
    right_x = xl.parse('Right_X')
    right_y = xl.parse('Right_Y')
    
    left_xdata = {i: left_x[str(i)].tolist() for i in range(17)}
    left_ydata = {i: left_y[str(i)].tolist() for i in range(17)}
    right_xdata = {i: right_x[str(i)].tolist() for i in range(17)}
    right_ydata = {i: right_y[str(i)].tolist() for i in range(17)}
    
    return left_xdata, left_ydata, right_xdata, right_ydata

def calculate_center_of_mass(xdata: Dict, ydata: Dict, frame_idx: int):
    """
    Calculate center of mass from key body points (hips and shoulders)
    Keypoints: 5=left_shoulder, 6=right_shoulder, 11=left_hip, 12=right_hip
    """
    key_points = [5, 6, 11, 12]
    x_coords = []
    y_coords = []
    
    for kp in key_points:
        x = xdata[kp][frame_idx]
        y = ydata[kp][frame_idx]
        if not np.isnan(x) and not np.isnan(y):
            x_coords.append(x)
            y_coords.append(y)
    
    if not x_coords:
        return None, None
    
    return np.mean(x_coords), np.mean(y_coords)


def detect_lunge(xdata: Dict[int, List[float]],
                 ydata: Dict[int, List[float]],
                 normalisation_constant: float,
                 frames_to_check: int = 5,
                 threshold_m: float = 0.8) -> bool:
    """
    Detect if a lunge occurred by measuring front-to-back foot distance increase.
    Operates on entire dataset range.
    """
    start_frame = 0
    end_frame = len(xdata[16]) - 1
    
    if end_frame - start_frame < frames_to_check:
        return False
    
    distance_samples = []
    for i in range(start_frame, min(start_frame + frames_to_check, end_frame + 1)):
        front_x = xdata[16][i]
        front_y = ydata[16][i]
        back_x = xdata[15][i]
        back_y = ydata[15][i]
        
        if not (np.isnan(front_x) or np.isnan(front_y) or np.isnan(back_x) or np.isnan(back_y)):
            dist = math.hypot(front_x - back_x, front_y - back_y)
            dist_m = dist * normalisation_constant
            distance_samples.append(dist_m)
    
    if len(distance_samples) < 2:
        return False
    
    avg_dist = np.mean(distance_samples)
    increasing = distance_samples[-1] > distance_samples[0]
    return avg_dist >= threshold_m and increasing

def detect_pause_retreat_intervals(xdata: Dict, ydata: Dict, is_left_fencer: bool, 
                                   fps: float = 15.0) -> List[PauseInterval]:
    """
    Detect pause/retreat intervals for a fencer using simplified logic:
    - Use only front foot (keypoint 16)
    - No smoothing
    - Raw velocity check
    - Y-variance filter on front foot
    - Back foot (keypoint 15) movement filter with fragment-based filtering
    - Assumes entire dataset range
    """
    intervals = []
    
    start_frame = 0
    end_frame = len(xdata[16]) - 1
    
    if start_frame >= end_frame:
        return intervals
    
    # Get front foot positions
    front_foot_x = [xdata[16][i] for i in range(start_frame, end_frame + 1)]
    front_foot_y = [ydata[16][i] for i in range(start_frame, end_frame + 1)]
    
    # Calculate raw velocities of front foot
    velocities = []
    for i in range(1, len(front_foot_x)):
        if not np.isnan(front_foot_x[i]) and not np.isnan(front_foot_x[i-1]):
            vel = front_foot_x[i] - front_foot_x[i-1]
        else:
            vel = 0
        velocities.append(vel)
    
    expected_direction = 1 if is_left_fencer else -1
    
    # --- TUNABLE PARAMS ---
    pause_threshold = 0.03
    retreat_threshold = 0.03 # Threshold to distinguish Retreat from Pause
    min_pause_frames = 4
    y_variance_threshold = 0.001
    back_foot_threshold = 0.05 # Threshold for back foot movement
    # ----------------------
    
    pause_frames = []
    current_pause_frames = []

    def process_and_filter_interval(frames):
        if len(frames) < min_pause_frames:
            return

        # 1. Determine if Pause or Retreat
        # Get velocities for these frames
        interval_vels = []
        for f_idx in frames:
            v_idx = f_idx - start_frame - 1
            if 0 <= v_idx < len(velocities):
                interval_vels.append(abs(velocities[v_idx]))
        
        if not interval_vels:
            return

        avg_abs_vel = np.mean(interval_vels)
        
        # If average velocity is high, it's a retreat (valid break of ROW)
        # We skip variance and back foot checks for retreats
        if avg_abs_vel > retreat_threshold:
            pause_frames.append(frames)
            return

        # Else, it's a Pause. Apply strict filters.
        
        # Filter: Back Foot Movement (Keypoint 15)
        # Identify valid frames where back foot velocity is within threshold
        valid_frames = []
        for f_idx in frames:
            # Calculate bf_vel for this frame
            bf_vel = 0.0
            if f_idx > 0 and f_idx < len(xdata[15]):
                curr_bf = xdata[15][f_idx]
                prev_bf = xdata[15][f_idx-1]
                if not np.isnan(curr_bf) and not np.isnan(prev_bf):
                    bf_vel = (curr_bf - prev_bf) * expected_direction
            
            # Check threshold (max limit for forward movement)
            # If bf_vel is high positive (moving forward), we reject the frame.
            # If bf_vel is low or negative (retreating), we keep it.
            if bf_vel < back_foot_threshold:
                valid_frames.append(f_idx)

        # Split valid_frames into continuous segments
        if not valid_frames:
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
        
        # Check each segment
        for segment in segments:
            # Check Length
            if len(segment) < min_pause_frames:
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
                if y_var >= y_variance_threshold:
                    continue # Failed Y-var check
            
            # Passed checks
            pause_frames.append(segment)
    
    for i, vel in enumerate(velocities):
        frame_idx = start_frame + i + 1
        
        # Check if paused (near zero velocity) or retreating (opposite direction)
        is_paused = (abs(vel) < pause_threshold) or (vel * expected_direction < 0)
        
        if is_paused:
            current_pause_frames.append(frame_idx)
        else:
            process_and_filter_interval(current_pause_frames)
            current_pause_frames = []
    
    # Handle end of loop
    process_and_filter_interval(current_pause_frames)
    
    for pf in pause_frames:
        intervals.append(PauseInterval(
            start_frame=pf[0],
            end_frame=pf[-1],
            start_time=pf[0] / fps,
            end_time=pf[-1] / fps,
            duration=(pf[-1] - pf[0]) / fps
        ))
    
    return intervals

def analyze_blade_contact(left_xdata: Dict, left_ydata: Dict, right_xdata: Dict,
                         right_ydata: Dict, contact_frame: int, 
                         current_right_of_way: str = 'none',
                         attack_variance_threshold: float = 0.1) -> Tuple[str, Dict]:
    """Determine blade priority around contact."""

    window_before = 8
    window_after = 3

    max_frame_left = len(left_xdata[10]) - 1
    max_frame_right = len(right_xdata[9]) - 1
    max_frame = min(max_frame_left, max_frame_right)

    start_f = max(0, contact_frame - window_before)
    end_f = min(contact_frame + window_after, max_frame)

    if start_f >= end_f:
        return 'right', {
            'analysis_window': 'invalid',
            'contact_frame': contact_frame,
            'left_variance': 0.0,
            'right_variance': 0.0,
            'samples_left': 0,
            'samples_right': 0,
        }

    def compute_variance(xdata, ydata, kp_idx) -> Tuple[float, float, int]:
        xs = []
        ys = []
        for i in range(start_f, end_f + 1):
            x = xdata[kp_idx][i]
            y = ydata[kp_idx][i]
            if not np.isnan(x) and not np.isnan(y):
                xs.append(x)
                ys.append(y)
        if len(xs) < 2:
            return 0.0, 0.0, len(xs)
        return float(np.var(xs, ddof=1)), float(np.var(ys, ddof=1)), len(xs)

    left_var_x, left_var_y, left_samples = compute_variance(left_xdata, left_ydata, 10)
    right_var_x, right_var_y, right_samples = compute_variance(right_xdata, right_ydata, 9)

    left_total = left_var_x + left_var_y
    right_total = right_var_x + right_var_y

    winner = 'right' # Default
    
    if current_right_of_way == 'none':
        if left_samples == 0 and right_samples == 0:
            winner = 'right'
        elif left_total > right_total:
            winner = 'left'
        elif right_total > left_total:
            winner = 'right'
        else:
            winner = 'right'
    else:
        if current_right_of_way == 'left':
            if left_total > attack_variance_threshold:
                winner = 'left'
            else:
                winner = 'right'
        elif current_right_of_way == 'right':
            if right_total > attack_variance_threshold:
                winner = 'right'
            else:
                winner = 'left'

    details = {
        'analysis_window': f'frames {start_f}-{end_f}',
        'contact_frame': contact_frame,
        'left_variance_x': left_var_x,
        'left_variance_y': left_var_y,
        'left_variance_total': left_total,
        'right_variance_x': right_var_x,
        'right_variance_y': right_var_y,
        'right_variance_total': right_total,
        'samples_left': left_samples,
        'samples_right': right_samples,
        'current_right_of_way': current_right_of_way,
    }

    return winner, details

def calculate_speed_acceleration(xdata: Dict, ydata: Dict) -> Tuple[float, float]:
    """
    Calculate average speed and acceleration over entire dataset range.
    """
    start_frame = 0
    end_frame = len(xdata[16]) - 1
    
    if end_frame - start_frame < 2:
        return 0.0, 0.0
    
    speeds = []
    accelerations = []
    
    prev_x, prev_y = None, None
    prev_speed = None
    
    for i in range(start_frame, end_frame + 1):
        x = xdata[16][i]
        y = ydata[16][i]
        
        if np.isnan(x) or np.isnan(y):
            continue
        
        if prev_x is not None:
            dist = math.hypot(x - prev_x, y - prev_y)
            speeds.append(dist)
            
            if prev_speed is not None:
                accel = abs(dist - prev_speed)
                accelerations.append(accel)
            
            prev_speed = dist
        
        prev_x, prev_y = x, y
    
    avg_speed = np.mean(speeds) if speeds else 0.0
    avg_accel = np.mean(accelerations) if accelerations else 0.0
    
    return float(avg_speed), float(avg_accel)

def referee_decision(phrase: FencingPhrase, left_xdata: Dict, left_ydata: Dict,
                    right_xdata: Dict, right_ydata: Dict,
                    normalisation_constant: Optional[float] = None) -> Dict:
    """
    Main referee decision logic implementing FIE right-of-way rules.
    """
    
    # Detect pause/retreat intervals for both fencers
    left_pauses = detect_pause_retreat_intervals(
        left_xdata, left_ydata, is_left_fencer=True, fps=phrase.fps
    )
    right_pauses = detect_pause_retreat_intervals(
        right_xdata, right_ydata, is_left_fencer=False, fps=phrase.fps
    )
    
    # Determine right-of-way based on pauses
    current_right_of_way = 'none'
    
    if left_pauses and not right_pauses:
        current_right_of_way = 'right'
    elif right_pauses and not left_pauses:
        current_right_of_way = 'left'
    elif left_pauses and right_pauses:
        # Both paused - compare timing
        left_pause_time = left_pauses[0].start_time
        right_pause_time = right_pauses[0].start_time
        if left_pause_time < right_pause_time:
            current_right_of_way = 'right'
        else:
            current_right_of_way = 'left'
    
    # Detect lunges
    left_lunge = detect_lunge(
        left_xdata, left_ydata, normalisation_constant
    ) if normalisation_constant else False
    
    right_lunge = detect_lunge(
        right_xdata, right_ydata, normalisation_constant
    ) if normalisation_constant else False
    
    # Calculate speed and acceleration
    left_speed, left_accel = calculate_speed_acceleration(
        left_xdata, left_ydata
    )
    right_speed, right_accel = calculate_speed_acceleration(
        right_xdata, right_ydata
    )
    
    # Analyze blade contacts
    blade_winner = 'none'
    blade_details = {}
    
    if phrase.blade_contacts:
        first_contact = phrase.blade_contacts[0]
        blade_winner, blade_details = analyze_blade_contact(
            left_xdata, left_ydata, right_xdata, right_ydata,
            first_contact.frame, current_right_of_way
        )
    
    # Make final decision
    winner = 'right'  # Default
    reason = ""
    
    if current_right_of_way == 'left':
        winner = 'left'
        reason = "Left has right-of-way"
        if right_pauses:
            reason += f" (only right paused at {right_pauses[0].start_time:.2f}s)"
    elif current_right_of_way == 'right':
        winner = 'right'
        reason = "Right has right-of-way"
        if left_pauses:
            reason += f" (only left paused at {left_pauses[0].start_time:.2f}s)"
    else:
        # No clear right-of-way from pauses
        if blade_winner != 'none':
            winner = blade_winner
            reason = f"Blade analysis favors {blade_winner}"
        elif left_speed > right_speed * 1.2:
            winner = 'left'
            reason = "Left had significantly higher speed"
        elif right_speed > left_speed * 1.2:
            winner = 'right'
            reason = "Right had significantly higher speed"
        else:
            winner = 'right'
            reason = "Simultaneous action, default to right"
    
    return {
        'winner': winner,
        'reason': reason,
        'left_pauses': [asdict(p) for p in left_pauses],
        'right_pauses': [asdict(p) for p in right_pauses],
        'blade_analysis': blade_winner,
        'blade_details': blade_details,
        'speed_comparison': {
            'left_speed': left_speed,
            'right_speed': right_speed,
            'left_accel': left_accel,
            'right_accel': right_accel,
        },
        'lunge_detected': {
            'left': left_lunge,
            'right': right_lunge,
        }
    }

def process_video(
    video_path: str,
    txt_path: str,
    model: Optional[YOLO] = None,
    model_path: str = "yolo26x-pose.pt",
    return_keypoints: bool = False,
    output_dir: Optional[Path] = None,
    save_excel: bool = False,
) -> Dict[str, Any]:
    """
    Process a single video and return analysis results.
    """
    start_time = time.time()
    
    if model is None:
        model = YOLO(model_path)
    
    # Extract tracks
    tracks_per_frame = extract_tracks_from_video(video_path, model)
    
    # Process data
    left_xdata, left_ydata, right_xdata, right_ydata, c, video_angle = \
        process_video_and_extract_data(tracks_per_frame)
    
    # Save Excel if requested
    excel_path = None
    if save_excel and output_dir:
        excel_filename = Path(video_path).stem + "_keypoints.xlsx"
        excel_path = output_dir / excel_filename
        save_keypoints_to_excel(left_xdata, left_ydata, right_xdata, right_ydata, str(excel_path))
    
    # Parse phrase
    phrase = parse_txt_file(txt_path, video_path=video_path)
    
    # Make decision
    decision = referee_decision(phrase, left_xdata, left_ydata, right_xdata, right_ydata, c)
    
    processing_time = time.time() - start_time
    
    result = {
        'phrase': asdict(phrase),
        'decision': decision,
        'frames_processed': len(tracks_per_frame),
        'processing_time': processing_time,
        'video_angle': video_angle,
        'normalisation_constant': c,
        'video_path': video_path,
        'txt_path': txt_path,
        'excel_path': str(excel_path) if excel_path else None,
    }
    
    if return_keypoints:
        result['left_xdata'] = left_xdata
        result['left_ydata'] = left_ydata
        result['right_xdata'] = right_xdata
        result['right_ydata'] = right_ydata
    
    return sanitize_for_json(result)

def main():
    parser = argparse.ArgumentParser(description="AI Fencing Referee")
    parser.add_argument("video", help="Path to video file")
    parser.add_argument("txt", help="Path to txt file")
    parser.add_argument(
        "--model",
        default="yolo26x-pose.pt",
        help="Path to the YOLO pose model weights (default: yolo26x-pose.pt)",
    )
    parser.add_argument("--output", help="Output directory for results")
    parser.add_argument("--save-excel", action="store_true", help="Save keypoints to Excel")
    parser.add_argument("--save-overlay", action="store_true", help="Save overlay video")
    
    args = parser.parse_args()
    
    output_dir = Path(args.output) if args.output else Path(args.video).parent
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Load model
    model = YOLO(args.model)
    
    # Process video
    result = process_video(
        args.video,
        args.txt,
        model=model,
        return_keypoints=args.save_overlay,
        output_dir=output_dir,
        save_excel=args.save_excel,
    )
    
    # Save JSON result
    json_path = output_dir / "analysis_result.json"
    with open(json_path, 'w') as f:
        json.dump(result, f, indent=2)
    
    print(f"Analysis complete. Results saved to {json_path}")
    print(f"Winner: {result['decision']['winner']}")
    print(f"Reason: {result['decision']['reason']}")
    
    # Generate overlay if requested
    if args.save_overlay and 'left_xdata' in result:
        overlay_path = output_dir / (Path(args.video).stem + "_overlay.mp4")
        render_overlay_video(
            Path(args.video),
            overlay_path,
            result['left_xdata'],
            result['left_ydata'],
            result['right_xdata'],
            result['right_ydata'],
            result['normalisation_constant'],
            draw_skeleton=True,
            draw_labels=True,
            show_progress=True,
        )
        print(f"Overlay video saved to {overlay_path}")

if __name__ == "__main__":
    main()

@dataclass
class AnalysisResult:
    input_video_path: str
    input_signal_path: str
    decision: Dict[str, Any]
    artifacts: Dict[str, str]
    metadata: Dict[str, Any]
    
    def to_dict(self, include_keypoints: bool = False) -> Dict[str, Any]:
        d = {
            "input_video": self.input_video_path,
            "input_signal": self.input_signal_path,
            "decision": self.decision,
            "artifacts": self.artifacts,
            "metadata": self.metadata,
        }
        return sanitize_for_json(d)

def analyze_video_signal(
    video_path: str,
    signal_path: str,
    model: Optional[YOLO] = None,
    model_path: str = "yolo26x-pose.pt",
    return_keypoints: bool = False,
    output_dir: Optional[Path] = None,
    save_excel: bool = True,
    save_overlay: bool = True,
    overlay_draw_skeleton: bool = True,
    overlay_draw_labels: bool = False,
    overlay_show_progress: bool = False,
    phrase: Optional[FencingPhrase] = None,
    fisheye_enabled: bool = False,
    fisheye_strength: float = -0.18,
    fisheye_balance: float = 0.0,
    fisheye_keep_audio: bool = True,
    fisheye_progress: bool = False,
) -> AnalysisResult:
    """
    Comprehensive analysis pipeline compatible with referee_service.py
    """
    if output_dir is None:
        output_dir = Path(video_path).parent
    output_dir.mkdir(parents=True, exist_ok=True)

    artifacts = {}
    
    # 1. Fisheye Correction
    processing_video_path = video_path
    if fisheye_enabled:
        corrected_path = output_dir / (Path(video_path).stem + "_corrected.mp4")
        processing_video_path = correct_fisheye_video(
            video_path,
            str(corrected_path),
            strength=fisheye_strength,
            balance=fisheye_balance,
            keep_audio=fisheye_keep_audio,
            progress=fisheye_progress
        )
        artifacts["corrected_video"] = str(processing_video_path)

    # 2. Run Analysis (reuse process_video logic but adapted)
    if model is None:
        model = YOLO(model_path)
        
    # Extract tracks
    tracks_per_frame = extract_tracks_from_video(processing_video_path, model)
    
    # Process data
    left_xdata, left_ydata, right_xdata, right_ydata, c, video_angle = \
        process_video_and_extract_data(tracks_per_frame)
        
    # Save Excel
    if save_excel:
        excel_path = output_dir / (Path(video_path).stem + "_keypoints.xlsx")
        save_keypoints_to_excel(left_xdata, left_ydata, right_xdata, right_ydata, str(excel_path))
        artifacts["excel"] = str(excel_path)

    # Parse phrase if not provided
    if phrase is None:
        phrase = parse_txt_file(signal_path, video_path=processing_video_path)

    # Make decision
    decision = referee_decision(phrase, left_xdata, left_ydata, right_xdata, right_ydata, c)
    
    # Generate Overlay
    if save_overlay:
        overlay_path = output_dir / (Path(video_path).stem + "_overlay.mp4")
        render_overlay_video(
            Path(processing_video_path),
            overlay_path,
            left_xdata,
            left_ydata,
            right_xdata,
            right_ydata,
            c,
            draw_skeleton=overlay_draw_skeleton,
            draw_labels=overlay_draw_labels,
            show_progress=overlay_show_progress
        )
        artifacts["analysis_video"] = str(overlay_path)

    metadata = {
        "video_angle": video_angle,
        "normalisation_constant": c,
        "frames_processed": len(tracks_per_frame),
    }

    return AnalysisResult(
        input_video_path=video_path,
        input_signal_path=signal_path,
        decision=decision,
        artifacts=artifacts,
        metadata=metadata
    )
