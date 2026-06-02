#!/usr/bin/env python3
"""Classify blade-contact benefit side and rebuild collapsed benefit folders.

This script is designed for phrase folders under `new_data/double_hit` that already
contain:
  - `<phrase>_keypoints.xlsx`
  - `analysis_result.json` (for normalization constant)
  - `<phrase>.txt` with blade-contact timeline lines

Primary prediction signal:
  combined_ahead = delta_lr_ahead_norm_evt + pre_ahead_weight * delta_lr_ahead_norm_pre
  where each ahead term is front-wrist lead relative to torso, normalized by body height.

Decision:
  - Predict left when combined_ahead is below score_threshold.
  - Predict right otherwise.

This uses both the contact frame and the short pre-contact window. In the current
dataset that is more reliable than using the contact frame alone with a torso fallback.

It can also rebuild:
  - `new_data/left_benefit`
  - `new_data/right_benefit`
by copying non-accident folders using TXT labels:
  [left beat], [left defend]  -> left_benefit
  [right beat], [right defend] -> right_benefit
  [accident] -> excluded
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import math
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import cv2
import numpy as np
import pandas as pd


CONTACT_RE = re.compile(
    r"^\s*([0-9]+(?:\.[0-9]+)?)s\s*\|\s*Off-Target:\s*Blade-to-blade contact\.[^\[]*(?:\[([^\]]+)\])?",
    re.IGNORECASE,
)


def _norm_tag(tag: str) -> str:
    return " ".join(tag.strip().lower().replace("_", " ").split())


def label_to_benefit(tag: Optional[str]) -> Optional[str]:
    if not tag:
        return None
    t = _norm_tag(tag)
    if t in {"left defend", "left beat"}:
        return "left"
    if t in {"right defend", "right beat"}:
        return "right"
    if t == "accident":
        return "accident"
    return None


def iter_phrase_dirs(base_dir: Path) -> Iterable[Path]:
    for child in sorted(base_dir.iterdir()):
        if child.is_dir():
            yield child


def pick_txt_file(folder: Path) -> Optional[Path]:
    txts = sorted(folder.glob("*.txt"))
    if not txts:
        return None
    same = folder / f"{folder.name}.txt"
    if same.exists():
        return same
    return txts[0]


def parse_last_contact(txt_path: Path) -> Optional[Tuple[float, Optional[str]]]:
    last_time = None
    last_labeled_tag: Optional[str] = None
    for line in txt_path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = CONTACT_RE.search(line)
        if not m:
            continue
        last_time = float(m.group(1))
        tag = m.group(2)
        if tag:
            last_labeled_tag = _norm_tag(tag)
    if last_time is None:
        return None
    return last_time, last_labeled_tag


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
    if avis:
        return avis[0]
    return None


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


def get_runtime_phrase_fps(folder: Path, txt_path: Path) -> float:
    """Mirror debug_referee_fps30 phrase FPS inference without top-level circular import."""
    import debug_referee_fps30 as drf  # Local import to avoid circular import at module load time.

    with contextlib.redirect_stdout(io.StringIO()):
        return float(drf.infer_phrase_fps(folder, txt_path=txt_path))


def load_norm_constant(folder: Path) -> float:
    p = folder / "analysis_result.json"
    if not p.exists():
        return 1.0
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
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


def _window_mean(arr: np.ndarray, frame_idx: int, radius: int = 1) -> float:
    lo = max(0, frame_idx - radius)
    hi = min(len(arr), frame_idx + radius + 1)
    if hi <= lo:
        return float("nan")
    seg = arr[lo:hi]
    valid = np.isfinite(seg)
    if not np.any(valid):
        return float("nan")
    return float(np.nansum(seg) / np.sum(valid))


def _safe_nanmean(arr: np.ndarray, axis: int) -> np.ndarray:
    counts = np.sum(~np.isnan(arr), axis=axis)
    sums = np.nansum(arr, axis=axis)
    out = np.divide(
        sums,
        counts,
        out=np.full_like(sums, np.nan, dtype=float),
        where=counts > 0,
    )
    return out


def _front_wrist_xy(x: np.ndarray, y: np.ndarray, side: str) -> Tuple[np.ndarray, np.ndarray]:
    # For left fencer, "front" wrist tends to have larger x.
    # For right fencer, "front" wrist tends to have smaller x.
    if side == "left":
        choose10 = np.nan_to_num(x[:, 10], nan=-1e9) > np.nan_to_num(x[:, 9], nan=-1e9)
    else:
        choose10 = np.nan_to_num(x[:, 10], nan=1e9) < np.nan_to_num(x[:, 9], nan=1e9)
    idx = np.arange(len(x))
    wx = x[idx, np.where(choose10, 10, 9)]
    wy = y[idx, np.where(choose10, 10, 9)]
    return wx, wy


def compute_contact_features(
    lx: np.ndarray,
    ly: np.ndarray,
    rx: np.ndarray,
    ry: np.ndarray,
    fps: float,
    contact_time_s: float,
) -> Dict[str, float]:
    n = len(lx)
    frame_idx = int(round(contact_time_s * fps))
    frame_idx = max(0, min(n - 1, frame_idx))

    # Torso centers.
    ltx = _safe_nanmean(np.stack([lx[:, 5], lx[:, 6], lx[:, 11], lx[:, 12]], axis=1), axis=1)
    lty = _safe_nanmean(np.stack([ly[:, 5], ly[:, 6], ly[:, 11], ly[:, 12]], axis=1), axis=1)
    rtx = _safe_nanmean(np.stack([rx[:, 5], rx[:, 6], rx[:, 11], rx[:, 12]], axis=1), axis=1)
    rty = _safe_nanmean(np.stack([ry[:, 5], ry[:, 6], ry[:, 11], ry[:, 12]], axis=1), axis=1)

    # Body heights for normalization.
    l_body_h = np.abs(_safe_nanmean(ly[:, [15, 16]], axis=1) - _safe_nanmean(ly[:, [5, 6]], axis=1))
    r_body_h = np.abs(_safe_nanmean(ry[:, [15, 16]], axis=1) - _safe_nanmean(ry[:, [5, 6]], axis=1))

    lwx, lwy = _front_wrist_xy(lx, ly, side="left")
    rwx, rwy = _front_wrist_xy(rx, ry, side="right")

    # Side-forward sign convention: left +x, right -x.
    l_ahead_norm = (lwx - ltx) / (l_body_h + 1e-6)
    r_ahead_norm = (rtx - rwx) / (r_body_h + 1e-6)

    dt = 1.0 / fps if fps > 0 else 1.0 / 30.0
    lvx = np.gradient(ltx, dt)
    lvy = np.gradient(lty, dt)
    rvx = np.gradient(rtx, dt)
    rvy = np.gradient(rty, dt)
    l_torso_fwd = lvx
    r_torso_fwd = -rvx
    l_torso_speed = np.sqrt(lvx**2 + lvy**2)
    r_torso_speed = np.sqrt(rvx**2 + rvy**2)

    lwvx = np.gradient(lwx, dt)
    lwvy = np.gradient(lwy, dt)
    rwvx = np.gradient(rwx, dt)
    rwvy = np.gradient(rwy, dt)
    l_wrist_speed = np.sqrt(lwvx**2 + lwvy**2)
    r_wrist_speed = np.sqrt(rwvx**2 + rwvy**2)
    l_wrist_fwd = lwvx
    r_wrist_fwd = -rwvx
    l_wrist_ratio = l_wrist_speed / (l_torso_speed + 1e-6)
    r_wrist_ratio = r_wrist_speed / (r_torso_speed + 1e-6)

    left_ahead_evt = _window_mean(l_ahead_norm, frame_idx, radius=1)
    right_ahead_evt = _window_mean(r_ahead_norm, frame_idx, radius=1)
    left_ahead_pre = _window_mean(l_ahead_norm, max(0, frame_idx - 4), radius=2)
    right_ahead_pre = _window_mean(r_ahead_norm, max(0, frame_idx - 4), radius=2)
    left_ahead_post = _window_mean(l_ahead_norm, min(n - 1, frame_idx + 4), radius=2)
    right_ahead_post = _window_mean(r_ahead_norm, min(n - 1, frame_idx + 4), radius=2)
    left_torso_evt = _window_mean(l_torso_fwd, frame_idx, radius=1)
    right_torso_evt = _window_mean(r_torso_fwd, frame_idx, radius=1)
    left_torso_pre = _window_mean(l_torso_fwd, max(0, frame_idx - 4), radius=2)
    right_torso_pre = _window_mean(r_torso_fwd, max(0, frame_idx - 4), radius=2)
    left_wrist_evt = _window_mean(l_wrist_speed, frame_idx, radius=1)
    right_wrist_evt = _window_mean(r_wrist_speed, frame_idx, radius=1)
    left_ratio_evt = _window_mean(l_wrist_ratio, frame_idx, radius=1)
    right_ratio_evt = _window_mean(r_wrist_ratio, frame_idx, radius=1)
    left_wrist_fwd_evt = _window_mean(l_wrist_fwd, frame_idx, radius=1)
    right_wrist_fwd_evt = _window_mean(r_wrist_fwd, frame_idx, radius=1)

    return {
        "frame_idx": float(frame_idx),
        "left_ahead_norm_pre": left_ahead_pre,
        "left_ahead_norm_evt": left_ahead_evt,
        "left_ahead_norm_post": left_ahead_post,
        "right_ahead_norm_pre": right_ahead_pre,
        "right_ahead_norm_evt": right_ahead_evt,
        "right_ahead_norm_post": right_ahead_post,
        "delta_lr_ahead_norm_pre": left_ahead_pre - right_ahead_pre,
        "delta_lr_ahead_norm": left_ahead_evt - right_ahead_evt,
        "delta_lr_ahead_norm_post": left_ahead_post - right_ahead_post,
        "left_torso_fwd_evt": left_torso_evt,
        "right_torso_fwd_evt": right_torso_evt,
        "delta_lr_torso_evt": left_torso_evt - right_torso_evt,
        "left_torso_fwd_pre": left_torso_pre,
        "right_torso_fwd_pre": right_torso_pre,
        "delta_lr_torso_pre": left_torso_pre - right_torso_pre,
        "left_wrist_speed_evt": left_wrist_evt,
        "right_wrist_speed_evt": right_wrist_evt,
        "delta_lr_wrist_speed_evt": left_wrist_evt - right_wrist_evt,
        "left_wrist_ratio_evt": left_ratio_evt,
        "right_wrist_ratio_evt": right_ratio_evt,
        "delta_lr_wrist_ratio_evt": left_ratio_evt - right_ratio_evt,
        "left_wrist_fwd_evt": left_wrist_fwd_evt,
        "right_wrist_fwd_evt": right_wrist_fwd_evt,
        "delta_lr_wrist_fwd_evt": left_wrist_fwd_evt - right_wrist_fwd_evt,
    }


@dataclass
class Prediction:
    predicted_side: str
    method: str
    confidence: str
    score_left: float
    prob_left: float


def predict_benefit_side(
    feat: Dict[str, float],
    pre_ahead_weight: float,
    score_threshold: float,
    ahead_margin: float,
    high_conf_margin: float,
    ahead_scale: float,
    post_ahead_weight: float = 0.0,
) -> Prediction:
    delta_ahead = float(feat["delta_lr_ahead_norm"])
    delta_ahead_pre = float(feat["delta_lr_ahead_norm_pre"])
    delta_ahead_post = float(feat.get("delta_lr_ahead_norm_post", float("nan")))
    delta_torso = float(feat["delta_lr_torso_evt"])

    legacy_pre_weight = 1.5
    legacy_threshold = -0.08

    if math.isfinite(delta_ahead) and math.isfinite(delta_ahead_pre) and math.isfinite(delta_ahead_post):
        combined_ahead = (
            delta_ahead
            + pre_ahead_weight * delta_ahead_pre
            + post_ahead_weight * delta_ahead_post
        )
        method = "ahead_evt_pre_post_score"
    elif math.isfinite(delta_ahead) and math.isfinite(delta_ahead_pre):
        combined_ahead = delta_ahead + legacy_pre_weight * delta_ahead_pre
        score_threshold = legacy_threshold
        method = "ahead_evt_pre_legacy_fallback"
    elif math.isfinite(delta_ahead) and math.isfinite(delta_ahead_post):
        combined_ahead = delta_ahead + post_ahead_weight * delta_ahead_post
        method = "ahead_evt_post_score"
    elif math.isfinite(delta_ahead):
        combined_ahead = delta_ahead
        method = "ahead_evt_only_nan_pre"
    elif math.isfinite(delta_ahead_pre):
        combined_ahead = legacy_pre_weight * delta_ahead_pre
        score_threshold = legacy_threshold
        method = "ahead_pre_only_nan_evt"
    elif math.isfinite(delta_ahead_post):
        combined_ahead = post_ahead_weight * delta_ahead_post
        method = "ahead_post_only_nan_evt"
    else:
        predicted = "left" if delta_torso < 0 else "right"
        return Prediction(
            predicted_side=predicted,
            method="torso_fallback_nan_ahead",
            confidence="low",
            score_left=float("nan"),
            prob_left=0.5,
        )

    # Positive score -> left benefit, negative -> right benefit.
    score_left = score_threshold - combined_ahead
    prob_left = 1.0 / (1.0 + math.exp(-score_left / max(ahead_scale, 1e-6)))

    predicted = "left" if score_left > 0 else "right"
    margin = abs(score_left)
    if margin >= high_conf_margin:
        confidence = "high"
    elif margin >= ahead_margin:
        confidence = "medium"
    else:
        confidence = "low"

    return Prediction(
        predicted_side=predicted,
        method=method,
        confidence=confidence,
        score_left=score_left,
        prob_left=prob_left,
    )


def reset_dest_folder(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    for child in list(path.iterdir()):
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()


def copy_phrase_folder(src: Path, dst_root: Path) -> Path:
    dst = dst_root / src.name
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
    return dst


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source-dir", type=Path, default=Path("new_data/double_hit"))
    ap.add_argument("--output-root", type=Path, default=Path("new_data"))
    ap.add_argument(
        "--predictions-csv",
        type=Path,
        default=Path("new_data/benefit_predictions.csv"),
        help="Output CSV with per-folder features and predicted benefit side.",
    )
    ap.add_argument(
        "--pre-ahead-weight",
        type=float,
        default=-1.0,
        help="Weight for the short pre-contact ahead asymmetry in the runtime benefit score.",
    )
    ap.add_argument(
        "--post-ahead-weight",
        type=float,
        default=0.75,
        help="Weight for the short post-contact ahead asymmetry in the runtime benefit score.",
    )
    ap.add_argument(
        "--score-threshold",
        type=float,
        default=0.14,
        help="Predict left benefit when combined_ahead is below this threshold.",
    )
    ap.add_argument(
        "--ahead-margin",
        type=float,
        default=0.08,
        help="Medium-confidence margin on the signed left/right benefit score.",
    )
    ap.add_argument(
        "--high-conf-margin",
        type=float,
        default=0.14,
        help="High-confidence margin on the signed left/right benefit score.",
    )
    ap.add_argument(
        "--ahead-scale",
        type=float,
        default=0.08,
        help="Scale for mapping the signed left/right benefit score to probability via logistic curve.",
    )
    ap.add_argument(
        "--rebuild-benefit-folders",
        action="store_true",
        help="Rebuild new_data/left_benefit and new_data/right_benefit from non-accident labeled clips.",
    )
    ap.add_argument(
        "--skip-copy",
        action="store_true",
        help="When rebuilding benefit folders, do not copy; only compute predictions.",
    )
    args = ap.parse_args()

    source_dir = args.source_dir
    if not source_dir.exists():
        raise SystemExit(f"Source dir not found: {source_dir}")

    left_benefit_dir = args.output_root / "left_benefit"
    right_benefit_dir = args.output_root / "right_benefit"
    if args.rebuild_benefit_folders and not args.skip_copy:
        reset_dest_folder(left_benefit_dir)
        reset_dest_folder(right_benefit_dir)

    rows: List[Dict[str, object]] = []
    copied = {"left": 0, "right": 0, "skipped_accident": 0, "skipped_unlabeled": 0}

    for folder in iter_phrase_dirs(source_dir):
        txt = pick_txt_file(folder)
        if txt is None:
            continue
        contact = parse_last_contact(txt)
        if contact is None:
            continue
        contact_time_s, raw_label = contact
        label_benefit = label_to_benefit(raw_label)

        excel = pick_excel_file(folder)
        if excel is None:
            continue

        fps = get_runtime_phrase_fps(folder, txt)
        norm_constant = load_norm_constant(folder)
        lx, ly, rx, ry = load_side_arrays(excel, norm_constant)
        feat = compute_contact_features(lx, ly, rx, ry, fps, contact_time_s)
        pred = predict_benefit_side(
            feat,
            pre_ahead_weight=args.pre_ahead_weight,
            score_threshold=args.score_threshold,
            ahead_margin=args.ahead_margin,
            high_conf_margin=args.high_conf_margin,
            ahead_scale=args.ahead_scale,
            post_ahead_weight=args.post_ahead_weight,
        )

        row: Dict[str, object] = {
            "folder": folder.name,
            "txt_path": str(txt),
            "excel_path": str(excel),
            "fps": fps,
            "contact_time_s": contact_time_s,
            "contact_label_raw": raw_label,
            "label_benefit": label_benefit,
            "predicted_benefit": pred.predicted_side,
            "prediction_method": pred.method,
            "confidence": pred.confidence,
            "score_left": pred.score_left,
            "prob_left": pred.prob_left,
            "correct_vs_label": (
                int(pred.predicted_side == label_benefit)
                if label_benefit in {"left", "right"}
                else None
            ),
        }
        row.update(feat)
        rows.append(row)

        if args.rebuild_benefit_folders:
            if label_benefit == "accident":
                copied["skipped_accident"] += 1
                continue
            if label_benefit not in {"left", "right"}:
                copied["skipped_unlabeled"] += 1
                continue
            if args.skip_copy:
                continue
            dst_root = left_benefit_dir if label_benefit == "left" else right_benefit_dir
            copy_phrase_folder(folder, dst_root)
            copied[label_benefit] += 1

    out_df = pd.DataFrame(rows).sort_values("folder")
    args.predictions_csv.parent.mkdir(parents=True, exist_ok=True)
    out_df.to_csv(args.predictions_csv, index=False)

    eval_df = out_df[out_df["label_benefit"].isin(["left", "right"])].copy()
    total_eval = len(eval_df)
    total_correct = int(eval_df["correct_vs_label"].sum()) if total_eval else 0
    overall_acc = (total_correct / total_eval) if total_eval else float("nan")

    by_conf = {}
    for conf, g in eval_df.groupby("confidence"):
        by_conf[conf] = {
            "count": int(len(g)),
            "accuracy": float(g["correct_vs_label"].mean()) if len(g) else float("nan"),
        }

    summary = {
        "source_dir": str(source_dir),
        "predictions_csv": str(args.predictions_csv),
        "rows_total": int(len(out_df)),
        "rows_eval_non_accident_labeled": int(total_eval),
        "accuracy_vs_label": overall_acc,
        "accuracy_by_confidence": by_conf,
        "copy_counts": copied if args.rebuild_benefit_folders else None,
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
