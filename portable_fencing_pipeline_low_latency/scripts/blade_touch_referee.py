#!/usr/bin/env python3
"""Blade-touch referee experiment harness.

Loads blade_touch_data samples, extracts keypoint-derived features around the final
blade contact, evaluates rule-based heuristics, and trains a small ML model that
assigns right-of-way to the fencer whose attack is favored at the last contact.
"""
from __future__ import annotations

import argparse
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import GradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, classification_report
from sklearn.model_selection import StratifiedKFold
from sklearn.preprocessing import StandardScaler

FPS = 15.0
BLEND_WINDOW = 5  # frames for baseline statistics
VELOCITY_LAG = 2  # frames when estimating velocity near contact
MOMENTUM_WINDOW = 6  # frames (~0.4s) for local momentum features


@dataclass
class PhraseSample:
    name: str
    folder: Path
    txt_path: Path
    excel_path: Path
    winner: str  # 'left' or 'right'
    contact_time: float
    contact_frame: int
    frame_count: int
    features: Dict[str, float]


KEYPOINT_INDEXES = {
    'front_foot': 16,
    'back_foot': 15,
    'front_knee': 14,
    'back_knee': 13,
    'front_wrist': 10,
    'back_wrist': 9,
}
CORE_POINTS = [5, 6, 11, 12]  # shoulders + hips for COM


WINNER_PATTERN = re.compile(r"(confirmed result|manual selection) winner:\s*(left|right)", re.IGNORECASE)
BLADE_PATTERN = re.compile(r"([0-9]+(?:\.[0-9]+)?)s\s*\|\s*.*blade-to-blade", re.IGNORECASE)


def _normalise_sheet_name(name: str) -> str:
    return name.strip().lower()


def load_keypoints(excel_path: Path) -> Dict[str, np.ndarray]:
    xls = pd.ExcelFile(excel_path)
    sheets = {_normalise_sheet_name(name): name for name in xls.sheet_names}
    required = ['left_x', 'left_y', 'right_x', 'right_y']
    missing = [s for s in required if s not in sheets]
    if missing:
        raise ValueError(f"Missing sheets {missing} in {excel_path}")

    data = {}
    for logical_name in required:
        df = xls.parse(sheets[logical_name])
        ordered_cols = sorted(df.columns, key=lambda col: int(col.split('_')[-1]))
        arr = df[ordered_cols].to_numpy(dtype=float)
        filled = pd.DataFrame(arr).interpolate(limit_direction='both').ffill().bfill().fillna(0.0)
        arr = filled.to_numpy()
        data[logical_name] = arr
    return data


def parse_txt(txt_path: Path) -> Tuple[float, str]:
    last_blade = None
    winner = None
    with txt_path.open('r', encoding='utf-8') as handle:
        for line in handle:
            if 'blade-to-blade' in line.lower():
                match = BLADE_PATTERN.search(line)
                if match:
                    last_blade = float(match.group(1))
            if 'winner' in line.lower():
                match = WINNER_PATTERN.search(line)
                if match:
                    winner = match.group(2).lower()
    if last_blade is None:
        raise ValueError(f"No blade contact found in {txt_path}")
    if winner is None:
        raise ValueError(f"No winner information in {txt_path}")
    return last_blade, winner


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def _baseline_indices(contact_idx: int) -> slice:
    end = _clamp(contact_idx, 1, BLEND_WINDOW)
    return slice(0, end)


def _extract_series(xdata: np.ndarray, kp_idx: int) -> np.ndarray:
    if kp_idx >= xdata.shape[1]:
        raise IndexError(f"Keypoint {kp_idx} not present (shape {xdata.shape})")
    return xdata[:, kp_idx]


def compute_fencer_features(
    xdata: np.ndarray,
    ydata: np.ndarray,
    contact_idx: int,
    direction: float,
) -> Dict[str, float]:
    n_frames = xdata.shape[0]
    idx = _clamp(contact_idx, 0, n_frames - 1)
    prev_idx = _clamp(idx - VELOCITY_LAG, 0, n_frames - 1)
    base_slice = _baseline_indices(idx)

    feat: Dict[str, float] = {}

    def _series(kp_name: str, axis: str = 'x') -> np.ndarray:
        arr = xdata if axis == 'x' else ydata
        return _extract_series(arr, KEYPOINT_INDEXES[kp_name])

    front = _series('front_foot', 'x')
    back = _series('back_foot', 'x')
    front_knee = _series('front_knee', 'x')
    front_wrist = _series('front_wrist', 'x')
    com = []
    for kp_idx in CORE_POINTS:
        com.append(xdata[:, kp_idx])
    com = np.nanmean(np.vstack(com), axis=0)

    def _metric(series: np.ndarray, name: str):
        now = float(series[idx])
        prev = float(series[prev_idx])
        base = float(np.nanmean(series[base_slice]))
        feat[f'{name}_now'] = now
        feat[f'{name}_progress'] = direction * (now - base)
        delta = now - prev if idx != prev_idx else 0.0
        feat[f'{name}_velocity'] = direction * delta * FPS / max(1, idx - prev_idx)

    _metric(front, 'front')
    _metric(back, 'back')
    _metric(front_knee, 'front_knee')
    _metric(front_wrist, 'front_wrist')

    feat['stance_now'] = float(front[idx] - back[idx])
    feat['stance_progress'] = direction * ((front[idx] - back[idx]) - (np.nanmean(front[base_slice]) - np.nanmean(back[base_slice])))
    feat['com_progress'] = direction * (float(com[idx]) - float(np.nanmean(com[base_slice])))
    feat['com_velocity'] = direction * (float(com[idx]) - float(com[prev_idx])) * FPS / max(1, idx - prev_idx)

    front_y = _series('front_foot', 'y')
    feat['front_height_change'] = float(np.nanmean(front_y[base_slice]) - front_y[idx])
    feat['weapon_lead'] = direction * (float(front_wrist[idx]) - float(front[idx]))
    feat['weapon_lead_progress'] = direction * ((float(front_wrist[idx]) - float(front[idx])) - (np.nanmean(front_wrist[base_slice]) - np.nanmean(front[base_slice])))
    feat['weapon_vs_com'] = direction * (float(front_wrist[idx]) - float(com[idx]))

    # Attack onset estimation based on cumulative forward progress
    progress_series = direction * (front[:idx + 1] - float(np.nanmean(front[base_slice])))
    final_progress = feat['front_progress']
    if final_progress > 0:
        threshold = max(0.02, 0.2 * final_progress)
    else:
        threshold = 0.05
    attack_start = idx
    for i, value in enumerate(progress_series):
        if value >= threshold:
            attack_start = i
            break
    frames_since_attack = max(1, idx - attack_start)
    feat['attack_start_frame'] = attack_start
    feat['attack_lead_time'] = frames_since_attack / FPS
    feat['attack_progress_rate'] = final_progress / frames_since_attack

    window_start = _clamp(idx - MOMENTUM_WINDOW, 0, idx)
    local_segments = front[window_start:idx + 1]
    if len(local_segments) >= 2:
        diffs = direction * np.diff(local_segments)
        feat['front_velocity_mean_window'] = float(diffs.mean() * FPS)
        feat['front_velocity_peak_window'] = float(diffs.max() * FPS)
    else:
        feat['front_velocity_mean_window'] = 0.0
        feat['front_velocity_peak_window'] = 0.0
    return feat


def build_sample(folder: Path) -> PhraseSample:
    txt_files = sorted(folder.glob('*.txt'))
    if not txt_files:
        raise FileNotFoundError(f"No txt file in {folder}")
    txt_path = txt_files[0]
    excel_files = sorted(folder.glob('*keypoints.xlsx'))
    if not excel_files:
        raise FileNotFoundError(f"No keypoint Excel file in {folder}")
    excel_path = excel_files[0]

    contact_time, winner = parse_txt(txt_path)
    data = load_keypoints(excel_path)
    left_x = data['left_x']
    left_y = data['left_y']
    right_x = data['right_x']
    right_y = data['right_y']

    contact_frame = int(round(contact_time * FPS))
    frame_count = left_x.shape[0]

    left_feat = compute_fencer_features(left_x, left_y, contact_frame, direction=+1.0)
    right_feat = compute_fencer_features(right_x, right_y, contact_frame, direction=-1.0)

    features = {f'left_{k}': v for k, v in left_feat.items()}
    features.update({f'right_{k}': v for k, v in right_feat.items()})

    # Interaction terms
    features['front_gap'] = float(right_feat['front_now'] - left_feat['front_now'])
    features['front_gap_change'] = (right_feat['front_progress'] - left_feat['front_progress'])
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

    return PhraseSample(
        name=folder.name,
        folder=folder,
        txt_path=txt_path,
        excel_path=excel_path,
        winner=winner,
        contact_time=contact_time,
        contact_frame=contact_frame,
        frame_count=frame_count,
        features=features,
    )


def collect_samples(data_dir: Path) -> List[PhraseSample]:
    samples = []
    for folder in sorted(data_dir.iterdir()):
        if not folder.is_dir():
            continue
        try:
            sample = build_sample(folder)
            samples.append(sample)
        except Exception as exc:
            print(f"[WARN] Skipping {folder.name}: {exc}")
    return samples


def rule_velocity(sample: PhraseSample) -> str:
    lf = sample.features
    left_score = lf['left_front_velocity'] + 0.6 * lf['left_front_progress'] + 0.3 * lf['left_com_velocity']
    right_score = lf['right_front_velocity'] + 0.6 * lf['right_front_progress'] + 0.3 * lf['right_com_velocity']
    if abs(left_score - right_score) < 1e-3:
        left_score += 0.2 * lf['left_stance_progress']
        right_score += 0.2 * lf['right_stance_progress']
    return 'left' if left_score > right_score else 'right'


def rule_front_pressure(sample: PhraseSample) -> str:
    lf = sample.features
    left_score = (
        0.7 * lf['left_front_progress']
        + 0.3 * lf['left_front_wrist_progress']
        + 0.4 * lf['left_stance_progress']
        + 0.2 * lf['left_front_knee_progress']
    )
    right_score = (
        0.7 * lf['right_front_progress']
        + 0.3 * lf['right_front_wrist_progress']
        + 0.4 * lf['right_stance_progress']
        + 0.2 * lf['right_front_knee_progress']
    )
    if abs(left_score - right_score) < 1e-3:
        left_score += 0.2 * lf['left_com_progress']
        right_score += 0.2 * lf['right_com_progress']
    return 'left' if left_score > right_score else 'right'


def rule_attack_priority(sample: PhraseSample) -> str:
    lf = sample.features
    left_started = lf['left_attack_start_frame']
    right_started = lf['right_attack_start_frame']
    if left_started != right_started:
        return 'left' if left_started < right_started else 'right'
    left_rate = lf['left_attack_progress_rate']
    right_rate = lf['right_attack_progress_rate']
    if abs(left_rate - right_rate) > 1e-3:
        return 'left' if left_rate > right_rate else 'right'
    return 'left' if lf['left_front_progress'] >= lf['right_front_progress'] else 'right'


def evaluate_rule(samples: List[PhraseSample], rule_fn) -> float:
    preds = [rule_fn(s) for s in samples]
    acc = accuracy_score([s.winner for s in samples], preds)
    return acc


def build_dataset(samples: List[PhraseSample]) -> Tuple[pd.DataFrame, np.ndarray, List[str]]:
    records = []
    for sample in samples:
        record = dict(sample.features)
        record['label'] = 1 if sample.winner == 'left' else 0
        record['name'] = sample.name
        records.append(record)
    df = pd.DataFrame(records)
    feature_cols = [c for c in df.columns if c not in {'label', 'name'}]
    X = df[feature_cols].to_numpy(dtype=float)
    y = df['label'].to_numpy(dtype=int)
    return df, X, y, feature_cols


def train_logistic(X: np.ndarray, y: np.ndarray, feature_names: List[str], cv_splits: int = 5):
    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)
    clf = LogisticRegression(max_iter=1000)
    skf = StratifiedKFold(n_splits=min(cv_splits, len(y)), shuffle=True, random_state=42)
    scores = []
    for train_idx, test_idx in skf.split(X_scaled, y):
        model = LogisticRegression(max_iter=1000)
        model.fit(X_scaled[train_idx], y[train_idx])
        preds = model.predict(X_scaled[test_idx])
        scores.append(accuracy_score(y[test_idx], preds))
    clf.fit(X_scaled, y)
    return scaler, clf, scores


def train_gradient_boost(X: np.ndarray, y: np.ndarray, cv_splits: int = 5):
    skf = StratifiedKFold(n_splits=min(cv_splits, len(y)), shuffle=True, random_state=42)
    scores = []
    for train_idx, test_idx in skf.split(X, y):
        model = GradientBoostingClassifier(random_state=42)
        model.fit(X[train_idx], y[train_idx])
        preds = model.predict(X[test_idx])
        scores.append(accuracy_score(y[test_idx], preds))
    final_model = GradientBoostingClassifier(random_state=42)
    final_model.fit(X, y)
    return final_model, scores


def save_model(output_path: Path, scaler, model, features: List[str], metadata: Dict | None = None):
    payload = {
        'scaler': scaler,
        'model': model,
        'features': features,
        'metadata': metadata or {},
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(payload, output_path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, default=Path('data/blade_touch_data'))
    parser.add_argument('--model-path', type=Path, default=Path('results/blade_touch_referee_model.joblib'))
    parser.add_argument('--predictions-path', type=Path, default=Path('results/blade_touch_referee_predictions.json'))
    parser.add_argument('--features-path', type=Path, default=Path('results/blade_touch_features.parquet'))
    args = parser.parse_args()

    samples = collect_samples(args.data_dir)
    if not samples:
        raise SystemExit(f"No samples found in {args.data_dir}")
    print(f"Loaded {len(samples)} blade-touch phrases.")

    vel_acc = evaluate_rule(samples, rule_velocity)
    pressure_acc = evaluate_rule(samples, rule_front_pressure)
    attack_acc = evaluate_rule(samples, rule_attack_priority)
    print(f"Rule velocity accuracy: {vel_acc:.3f}")
    print(f"Rule pressure accuracy: {pressure_acc:.3f}")
    print(f"Rule attack priority accuracy: {attack_acc:.3f}")

    df, X, y, feature_cols = build_dataset(samples)
    scaler, clf, scores = train_logistic(X, y, feature_cols)
    print(f"Logistic regression CV accuracy: mean={np.mean(scores):.3f}, std={np.std(scores):.3f}")
    logistic_preds = clf.predict(scaler.transform(X))
    logistic_acc = accuracy_score(y, logistic_preds)
    print(f"Logistic training-set accuracy: {logistic_acc:.3f}")

    gb_model, gb_scores = train_gradient_boost(X, y)
    print(f"Gradient boosting CV accuracy: mean={np.mean(gb_scores):.3f}, std={np.std(gb_scores):.3f}")
    gb_preds = gb_model.predict(X)
    gb_acc = accuracy_score(y, gb_preds)
    print(f"Gradient boosting training accuracy: {gb_acc:.3f}")

    if np.mean(gb_scores) >= np.mean(scores):
        active_scaler = None
        active_model = gb_model
        active_preds = gb_preds
        active_name = 'GradientBoosting'
    else:
        active_scaler = scaler
        active_model = clf
        active_preds = logistic_preds
        active_name = 'LogisticRegression'

    print(f"Selected model: {active_name}")
    report = classification_report(y, active_preds, target_names=['right', 'left'])
    print(report)

    save_model(
        args.model_path,
        active_scaler,
        active_model,
        feature_cols,
        metadata={'model_type': active_name, 'cv_scores': scores if active_name.startswith('Logistic') else gb_scores},
    )
    print(f"Saved model to {args.model_path}")

    args.features_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(args.features_path, index=False)
    print(f"Wrote feature table to {args.features_path}")

    pred_rows = []
    for sample, pred in zip(samples, active_preds):
        pred_label = 'left' if pred == 1 else 'right'
        pred_rows.append({
            'name': sample.name,
            'contact_time': sample.contact_time,
            'winner': sample.winner,
            'predicted': pred_label,
        })
    args.predictions_path.parent.mkdir(parents=True, exist_ok=True)
    with args.predictions_path.open('w', encoding='utf-8') as handle:
        json.dump(pred_rows, handle, indent=2)
    print(f"Wrote predictions to {args.predictions_path}")


if __name__ == '__main__':
    main()
