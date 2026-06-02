#!/usr/bin/env python3
"""Classify whether the last blade contact before first hit is an accident.

This script is designed for phrase folders under `new_data/double_hit` that already
contain:
  - `<phrase>_keypoints.xlsx`
  - `analysis_result.json` (for normalization constant)
  - `<phrase>.txt` with blade-contact timeline lines

Current rule:
  - Compute the last blade contact at or before the first HIT.
  - Derive two label-free features around that contact:
      sym_min_wrist_fwd_pre
      sym_max_wrist_speed_post
  - Impute missing post-contact wrist speed with the training median.
  - Predict accident when both features clear tuned thresholds.

This rule improved in-dataset accuracy over the earlier shallow-tree baseline, but it
still depends partly on post-contact availability. It should be treated as the current
best binary heuristic, not a production-grade classifier.
"""

from __future__ import annotations

import argparse
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import cv2
import numpy as np
import pandas as pd


TIME_LINE_RE = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)s\s*\|\s*(.*)$", re.IGNORECASE)

POST_SPEED_IMPUTE = 820.0
MIN_WRIST_FWD_PRE_THRESHOLD = 13.0
MAX_WRIST_SPEED_POST_THRESHOLD = 820.0


def _norm_tag(tag: str) -> str:
    return " ".join(tag.strip().lower().replace("_", " ").split())


@dataclass
class SelectedContact:
    contact_time_s: float
    contact_label_raw: Optional[str]
    first_hit_time_s: Optional[float]
    total_contacts: int


def label_to_is_accident(tag: Optional[str]) -> Optional[int]:
    if not tag:
        return None
    norm = _norm_tag(tag)
    if norm == "accident":
        return 1
    if norm in {"left beat", "left defend", "right beat", "right defend"}:
        return 0
    return None


def iter_phrase_dirs(base_dir: Path) -> Iterable[Path]:
    for child in sorted(base_dir.iterdir()):
        if child.is_dir():
            yield child


def pick_txt_file(folder: Path) -> Optional[Path]:
    same = folder / f"{folder.name}.txt"
    if same.exists():
        return same
    txts = sorted(folder.glob("*.txt"))
    return txts[0] if txts else None


def pick_excel_file(folder: Path) -> Optional[Path]:
    same = folder / f"{folder.name}_keypoints.xlsx"
    if same.exists():
        return same
    xls = sorted(folder.glob("*keypoints.xlsx"))
    return xls[0] if xls else None


def pick_video_file(folder: Path) -> Optional[Path]:
    corrected = folder / f"{folder.name}_corrected.mp4"
    if corrected.exists():
        return corrected
    mp4s = sorted([p for p in folder.glob("*.mp4") if "_overlay" not in p.name])
    if mp4s:
        return mp4s[0]
    avis = sorted(folder.glob("*.avi"))
    return avis[0] if avis else None


def parse_last_contact_before_hit(txt_path: Path) -> Optional[SelectedContact]:
    first_hit_time: Optional[float] = None
    contacts: List[Tuple[float, Optional[str]]] = []

    for raw in txt_path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = TIME_LINE_RE.match(raw.strip())
        if not m:
            continue
        t = float(m.group(1))
        desc = m.group(2).strip()
        lower = desc.lower()
        if "blade-to-blade contact" in lower:
            tag_match = re.search(r"\[([^\]]+)\]\s*$", desc)
            tag = _norm_tag(tag_match.group(1)) if tag_match else None
            contacts.append((t, tag))
        if first_hit_time is None and lower.startswith("hit:"):
            first_hit_time = t

    if not contacts:
        return None

    if first_hit_time is None:
        chosen_time, chosen_tag = max(contacts, key=lambda item: item[0])
    else:
        prior = [item for item in contacts if item[0] <= first_hit_time + 1e-9]
        chosen_time, chosen_tag = max(prior or contacts, key=lambda item: item[0])

    return SelectedContact(
        contact_time_s=chosen_time,
        contact_label_raw=chosen_tag,
        first_hit_time_s=first_hit_time,
        total_contacts=len(contacts),
    )


def get_fps(folder: Path) -> float:
    video = pick_video_file(folder)
    if video is None:
        return 30.0
    cap = cv2.VideoCapture(str(video))
    fps = cap.get(cv2.CAP_PROP_FPS)
    cap.release()
    if fps is None or fps <= 1 or math.isnan(float(fps)):
        return 30.0
    return float(fps)


def load_norm_constant(folder: Path) -> float:
    path = folder / "analysis_result.json"
    if not path.exists():
        return 1.0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return float(data.get("normalisation_constant") or 1.0)
    except Exception:
        return 1.0


def load_side_arrays(excel_path: Path, norm_constant: float) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    lx = pd.read_excel(excel_path, sheet_name="Left_X", header=None).iloc[1:].to_numpy(dtype=float)
    ly = pd.read_excel(excel_path, sheet_name="Left_Y", header=None).iloc[1:].to_numpy(dtype=float)
    rx = pd.read_excel(excel_path, sheet_name="Right_X", header=None).iloc[1:].to_numpy(dtype=float)
    ry = pd.read_excel(excel_path, sheet_name="Right_Y", header=None).iloc[1:].to_numpy(dtype=float)

    lx *= norm_constant
    ly *= norm_constant
    rx *= norm_constant
    ry *= norm_constant

    for arr in (lx, ly, rx, ry):
        arr[arr <= 0] = np.nan
    return lx, ly, rx, ry


def _window_mean(arr: np.ndarray, i0: int, i1: int) -> float:
    lo = max(0, i0)
    hi = min(len(arr), i1 + 1)
    if hi <= lo:
        return float("nan")
    seg = arr[lo:hi]
    valid = np.isfinite(seg)
    if not np.any(valid):
        return float("nan")
    return float(np.nansum(seg) / np.sum(valid))


def _front_wrist_xy(x: np.ndarray, y: np.ndarray, side: str) -> Tuple[np.ndarray, np.ndarray]:
    if side == "left":
        choose10 = np.nan_to_num(x[:, 10], nan=-1e9) > np.nan_to_num(x[:, 9], nan=-1e9)
    else:
        choose10 = np.nan_to_num(x[:, 10], nan=1e9) < np.nan_to_num(x[:, 9], nan=1e9)
    idx = np.arange(len(x))
    wrist_idx = np.where(choose10, 10, 9)
    return x[idx, wrist_idx], y[idx, wrist_idx]


def compute_accident_features(
    lx: np.ndarray,
    ly: np.ndarray,
    rx: np.ndarray,
    ry: np.ndarray,
    fps: float,
    contact_time_s: float,
) -> Dict[str, float]:
    n_frames = len(lx)
    event_frame = int(round(contact_time_s * fps))
    event_frame = max(0, min(n_frames - 1, event_frame))

    dt = 1.0 / fps if fps > 0 else 1.0 / 30.0
    lwx, lwy = _front_wrist_xy(lx, ly, "left")
    rwx, rwy = _front_wrist_xy(rx, ry, "right")

    left_wrist_fwd = np.gradient(lwx, dt)
    right_wrist_fwd = -np.gradient(rwx, dt)
    left_wrist_speed = np.sqrt(np.gradient(lwx, dt) ** 2 + np.gradient(lwy, dt) ** 2)
    right_wrist_speed = np.sqrt(np.gradient(rwx, dt) ** 2 + np.gradient(rwy, dt) ** 2)

    left_wrist_fwd_pre = _window_mean(left_wrist_fwd, event_frame - 8, event_frame - 4)
    right_wrist_fwd_pre = _window_mean(right_wrist_fwd, event_frame - 8, event_frame - 4)
    left_wrist_speed_post = _window_mean(left_wrist_speed, event_frame + 2, event_frame + 6)
    right_wrist_speed_post = _window_mean(right_wrist_speed, event_frame + 2, event_frame + 6)

    post_candidates = [v for v in [left_wrist_speed_post, right_wrist_speed_post] if math.isfinite(v)]
    return {
        "event_frame": float(event_frame),
        "left_wrist_fwd_pre": left_wrist_fwd_pre,
        "right_wrist_fwd_pre": right_wrist_fwd_pre,
        "sym_min_wrist_fwd_pre": float(np.nanmin([left_wrist_fwd_pre, right_wrist_fwd_pre])),
        "left_wrist_speed_post": left_wrist_speed_post,
        "right_wrist_speed_post": right_wrist_speed_post,
        "sym_max_wrist_speed_post": (float(max(post_candidates)) if post_candidates else float("nan")),
    }


def predict_is_accident(feat: Dict[str, float]) -> Tuple[int, str, float, float]:
    wrist_pre = float(feat["sym_min_wrist_fwd_pre"])
    wrist_post = float(feat["sym_max_wrist_speed_post"])
    post_imputed = wrist_post if math.isfinite(wrist_post) else POST_SPEED_IMPUTE

    wrist_margin = wrist_pre - MIN_WRIST_FWD_PRE_THRESHOLD
    post_margin = post_imputed - MAX_WRIST_SPEED_POST_THRESHOLD
    predicted = int(wrist_margin >= 0 and post_margin >= 0)

    min_margin = min(wrist_margin, post_margin / 100.0)
    if predicted and min_margin >= 2.0:
        confidence = "high"
    elif predicted and min_margin >= 0.0:
        confidence = "medium"
    else:
        confidence = "low"

    return predicted, confidence, wrist_margin, post_margin


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source-dir", type=Path, default=Path("new_data/double_hit"))
    ap.add_argument(
        "--predictions-csv",
        type=Path,
        default=Path("new_data/accident_predictions.csv"),
        help="Output CSV with per-folder accident predictions.",
    )
    args = ap.parse_args()

    rows: List[Dict[str, object]] = []
    for folder in iter_phrase_dirs(args.source_dir):
        txt = pick_txt_file(folder)
        excel = pick_excel_file(folder)
        if txt is None or excel is None:
            continue

        selected = parse_last_contact_before_hit(txt)
        if selected is None:
            continue

        fps = get_fps(folder)
        norm_constant = load_norm_constant(folder)
        lx, ly, rx, ry = load_side_arrays(excel, norm_constant)
        feat = compute_accident_features(lx, ly, rx, ry, fps, selected.contact_time_s)
        predicted, confidence, wrist_margin, post_margin = predict_is_accident(feat)

        label_is_accident = label_to_is_accident(selected.contact_label_raw)
        row: Dict[str, object] = {
            "folder": folder.name,
            "contact_time_s": selected.contact_time_s,
            "contact_label_raw": selected.contact_label_raw,
            "label_is_accident": label_is_accident,
            "predicted_is_accident": predicted,
            "confidence": confidence,
            "correct_vs_label": int(predicted == label_is_accident) if label_is_accident is not None else None,
            "wrist_pre_margin": wrist_margin,
            "post_speed_margin": post_margin,
            "post_speed_was_imputed": int(not math.isfinite(float(feat["sym_max_wrist_speed_post"]))),
            "fps": fps,
            "first_hit_time_s": selected.first_hit_time_s,
            "total_contacts_in_txt": selected.total_contacts,
        }
        row.update(feat)
        rows.append(row)

    out_df = pd.DataFrame(rows).sort_values("folder")
    args.predictions_csv.parent.mkdir(parents=True, exist_ok=True)
    out_df.to_csv(args.predictions_csv, index=False)

    eval_df = out_df[out_df["label_is_accident"].notna()].copy()
    summary = {
        "source_dir": str(args.source_dir),
        "predictions_csv": str(args.predictions_csv),
        "rows_total": int(len(out_df)),
        "rows_eval_labeled": int(len(eval_df)),
        "accuracy_vs_label": float(eval_df["correct_vs_label"].mean()) if len(eval_df) else float("nan"),
        "balanced_accuracy_vs_label": (
            float(
                0.5
                * (
                    eval_df.loc[eval_df["label_is_accident"] == 1, "correct_vs_label"].mean()
                    + eval_df.loc[eval_df["label_is_accident"] == 0, "correct_vs_label"].mean()
                )
            )
            if len(eval_df)
            else float("nan")
        ),
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
