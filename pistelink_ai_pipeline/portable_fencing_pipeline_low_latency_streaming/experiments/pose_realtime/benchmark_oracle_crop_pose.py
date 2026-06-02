#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import sys
import time
import types
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import cv2
import numpy as np
import torch
import torch.nn as nn


REPO_ROOT = Path("/home/thomas/fencing")
BUNDLE_ROOT = REPO_ROOT / "portable_fencing_pipeline_low_latency"
EXPERIMENT_DIR = Path(__file__).resolve().parent
VENDOR_POSE = BUNDLE_ROOT / ".vendor_pose"
VPI_PYTHON_PATH = Path("/opt/nvidia/vpi3/lib/aarch64-linux-gnu/python")
MODELS_DIR = BUNDLE_ROOT / "model_variants" / "pose_benchmarks"

if str(EXPERIMENT_DIR) not in sys.path:
    sys.path.insert(0, str(EXPERIMENT_DIR))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if VPI_PYTHON_PATH.exists():
    sys.path.append(str(VPI_PYTHON_PATH))


def _install_xtcocotools_stub() -> None:
    if "xtcocotools" in sys.modules:
        return

    def _unsupported(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError(
            "xtcocotools functionality is not available in this experimental "
            "copy-branch stub. This code path should not be used for plain "
            "top-down inference."
        )

    pkg = types.ModuleType("xtcocotools")
    coco_mod = types.ModuleType("xtcocotools.coco")
    cocoeval_mod = types.ModuleType("xtcocotools.cocoeval")
    mask_mod = types.ModuleType("xtcocotools.mask")

    class COCO:  # pragma: no cover - compatibility shim
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            _unsupported(*args, **kwargs)

    class COCOeval:  # pragma: no cover - compatibility shim
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            _unsupported(*args, **kwargs)

    coco_mod.COCO = COCO
    cocoeval_mod.COCOeval = COCOeval
    def _mask_getattr(name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        return _unsupported

    pkg.__file__ = "<xtcocotools_stub>"
    coco_mod.__file__ = "<xtcocotools_stub>"
    cocoeval_mod.__file__ = "<xtcocotools_stub>"
    mask_mod.__file__ = "<xtcocotools_stub>"
    mask_mod.__getattr__ = _mask_getattr  # type: ignore[assignment]

    pkg.coco = coco_mod  # type: ignore[attr-defined]
    pkg.cocoeval = cocoeval_mod  # type: ignore[attr-defined]
    pkg.mask = mask_mod  # type: ignore[attr-defined]

    sys.modules["xtcocotools"] = pkg
    sys.modules["xtcocotools.coco"] = coco_mod
    sys.modules["xtcocotools.cocoeval"] = cocoeval_mod
    sys.modules["xtcocotools.mask"] = mask_mod


def _install_mmdet_stub() -> None:
    if "mmdet" in sys.modules:
        return

    pkg = types.ModuleType("mmdet")
    utils_mod = types.ModuleType("mmdet.utils")
    pkg.__file__ = "<mmdet_stub>"
    utils_mod.__file__ = "<mmdet_stub>"
    utils_mod.ConfigType = Dict[str, Any]  # type: ignore[attr-defined]
    utils_mod.reduce_mean = lambda x: x  # type: ignore[attr-defined]
    pkg.utils = utils_mod  # type: ignore[attr-defined]
    sys.modules["mmdet"] = pkg
    sys.modules["mmdet.utils"] = utils_mod


def _install_mmcv_ops_stub() -> None:
    if "mmcv.ops" in sys.modules:
        return

    ops_mod = types.ModuleType("mmcv.ops")
    ops_mod.__file__ = "<mmcv_ops_stub>"

    class _DummyOp(nn.Module):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__()

        def forward(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover - compatibility shim
            raise RuntimeError("mmcv.ops stub was invoked unexpectedly in the pose experiment")

    ops_mod.MultiScaleDeformableAttention = _DummyOp  # type: ignore[attr-defined]
    ops_mod.DeformConv2d = _DummyOp  # type: ignore[attr-defined]
    ops_mod.ModulatedDeformConv2d = _DummyOp  # type: ignore[attr-defined]
    sys.modules["mmcv.ops"] = ops_mod


if str(VENDOR_POSE) not in sys.path:
    sys.path.insert(0, str(VENDOR_POSE))
_install_xtcocotools_stub()
_install_mmdet_stub()
_install_mmcv_ops_stub()

from ultralytics import YOLO

from portable_fencing_pipeline_low_latency.scripts.reprocess_phrase_limb_interp_jumpsafe_experimental import (
    _build_fisheye_frame_corrector,
    _locate_front_fencers_with_yolo_bootstrap,
    _read_tracking_indices,
    extract_tracks_with_jump_safe_tracker,
)
from portable_fencing_pipeline_low_latency.src.referee import analysis


DEFAULT_ORACLE_MODEL = BUNDLE_ROOT / "models" / "yolo26x-pose.pt"
DEFAULT_ORACLE_IMGSZ = 640
DEFAULT_PAD_FRACTION = 0.25


RTMPOSE_MODELS: Dict[str, Dict[str, str]] = {
    "rtmpose-t": {
        "config": "mmpose/.mim/configs/body_2d_keypoint/rtmpose/coco/rtmpose-t_8xb256-420e_coco-256x192.py",
        "weights": "https://download.openmmlab.com/mmpose/v1/projects/rtmposev1/rtmpose-tiny_simcc-coco_pt-aic-coco_420e-256x192-e613ba3f_20230127.pth",
        "checkpoint_name": "rtmpose-t_8xb256-420e_coco-256x192.pth",
    },
    "rtmpose-s": {
        "config": "mmpose/.mim/configs/body_2d_keypoint/rtmpose/coco/rtmpose-s_8xb256-420e_coco-256x192.py",
        "weights": "https://download.openmmlab.com/mmpose/v1/projects/rtmposev1/rtmpose-s_simcc-coco_pt-aic-coco_420e-256x192-8edcf0d7_20230127.pth",
        "checkpoint_name": "rtmpose-s_8xb256-420e_coco-256x192.pth",
    },
}

COCO_KEYPOINT_NAMES = {
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
FOCUS_JOINTS = {
    "elbows": (7, 8),
    "wrists": (9, 10),
    "knees": (13, 14),
    "ankles": (15, 16),
}


@dataclass
class PosePrediction:
    keypoints: Optional[np.ndarray]
    scores: Optional[np.ndarray]


class PoseBackend:
    name: str

    def predict(self, crop_bgr: np.ndarray) -> PosePrediction:
        raise NotImplementedError


class UltralyticsCropPoseBackend(PoseBackend):
    def __init__(self, model_path: Path, *, conf: float, imgsz: int, half: bool):
        self.name = model_path.stem
        self.model = YOLO(str(model_path))
        self.predict_kwargs = analysis.ultralytics_predict_kwargs(
            verbose=False,
            conf=conf,
            imgsz=imgsz,
            half=half,
        )

    def predict(self, crop_bgr: np.ndarray) -> PosePrediction:
        results = self.model(crop_bgr, **self.predict_kwargs)
        if not results or results[0].keypoints is None:
            return PosePrediction(None, None)
        result = results[0]
        raw_kpts = result.keypoints.xy
        raw_scores = result.keypoints.conf if getattr(result.keypoints, "conf", None) is not None else None
        if hasattr(raw_kpts, "cpu"):
            raw_kpts = raw_kpts.cpu().numpy()
        else:
            raw_kpts = np.array(raw_kpts)
        if raw_scores is not None:
            raw_scores = raw_scores.cpu().numpy() if hasattr(raw_scores, "cpu") else np.array(raw_scores)
        if raw_kpts.shape[0] == 0:
            return PosePrediction(None, None)

        best_idx = 0
        best_area = -1.0
        for idx in range(raw_kpts.shape[0]):
            bbox = analysis.bbox_from_keypoints(raw_kpts[idx])
            area = float(analysis.bbox_area(bbox)) if bbox is not None else 0.0
            if area > best_area:
                best_area = area
                best_idx = idx
        scores = raw_scores[best_idx] if raw_scores is not None and len(raw_scores) > best_idx else None
        return PosePrediction(np.array(raw_kpts[best_idx], copy=True), np.array(scores, copy=True) if scores is not None else None)


class RTMPoseBackend(PoseBackend):
    def __init__(self, model_key: str):
        if model_key not in RTMPOSE_MODELS:
            raise KeyError(f"Unsupported RTMPose model: {model_key}")
        spec = RTMPOSE_MODELS[model_key]
        self.name = model_key
        self.config_path = VENDOR_POSE / spec["config"]
        self.checkpoint_path = MODELS_DIR / spec["checkpoint_name"]
        MODELS_DIR.mkdir(parents=True, exist_ok=True)
        if not self.checkpoint_path.exists():
            urllib.request.urlretrieve(spec["weights"], self.checkpoint_path)

        from mmpose.apis.inference import inference_topdown, init_model

        self._inference_topdown = inference_topdown
        original_torch_load = torch.load

        def _unsafe_compatible_load(*args: Any, **kwargs: Any) -> Any:
            kwargs.setdefault("weights_only", False)
            return original_torch_load(*args, **kwargs)

        torch.load = _unsafe_compatible_load  # type: ignore[assignment]
        try:
            self.model = init_model(
                str(self.config_path),
                checkpoint=str(self.checkpoint_path),
                device="cuda:0",
            )
        finally:
            torch.load = original_torch_load  # type: ignore[assignment]

    def predict(self, crop_bgr: np.ndarray) -> PosePrediction:
        results = self._inference_topdown(self.model, crop_bgr, bboxes=None)
        if not results:
            return PosePrediction(None, None)
        ds = results[0]
        pred_instances = ds.pred_instances
        keypoints = pred_instances.keypoints
        scores = pred_instances.keypoint_scores if "keypoint_scores" in pred_instances else None
        if hasattr(keypoints, "cpu"):
            keypoints = keypoints.cpu().numpy()
        else:
            keypoints = np.array(keypoints)
        if scores is not None:
            scores = scores.cpu().numpy() if hasattr(scores, "cpu") else np.array(scores)
        if keypoints.ndim == 3:
            keypoints = keypoints[0]
        if scores is not None and scores.ndim == 2:
            scores = scores[0]
        return PosePrediction(np.array(keypoints, copy=True), np.array(scores, copy=True) if scores is not None else None)


def find_phrase_video(phrase_dir: Path) -> Path:
    candidates: List[Path] = []
    for pattern in ("*.avi", "*.mp4", "*.mov", "*.mkv"):
        candidates.extend(sorted(phrase_dir.glob(pattern)))
    if not candidates:
        raise FileNotFoundError(f"No phrase video found in {phrase_dir}")
    return candidates[0]


def find_phrase_txt(phrase_dir: Path) -> Path:
    txts = sorted(phrase_dir.glob("*.txt"))
    if not txts:
        raise FileNotFoundError(f"No phrase txt found in {phrase_dir}")
    return txts[0]


def valid_point(pt: np.ndarray) -> bool:
    return (
        pt is not None
        and len(pt) >= 2
        and np.isfinite(float(pt[0]))
        and np.isfinite(float(pt[1]))
        and float(pt[0]) > 0.0
        and float(pt[1]) > 0.0
    )


def expand_bbox(
    bbox: Sequence[float],
    frame_w: int,
    frame_h: int,
    *,
    pad_fraction: float,
) -> Tuple[int, int, int, int]:
    x1, y1, x2, y2 = [float(v) for v in bbox]
    bw = max(1.0, x2 - x1)
    bh = max(1.0, y2 - y1)
    pad = max(24.0, max(bw, bh) * pad_fraction)
    ix1 = max(0, int(math.floor(x1 - pad)))
    iy1 = max(0, int(math.floor(y1 - pad)))
    ix2 = min(frame_w, int(math.ceil(x2 + pad)))
    iy2 = min(frame_h, int(math.ceil(y2 + pad)))
    return ix1, iy1, ix2, iy2


def bbox_from_track(track: Dict[str, Any]) -> Optional[Tuple[int, int, int, int]]:
    bbox = track.get("box")
    if bbox is not None:
        arr = np.array(bbox, dtype=float).tolist()
        return tuple(int(round(v)) for v in arr)  # type: ignore[return-value]
    kpts = track.get("keypoints")
    if kpts is None:
        return None
    return analysis.bbox_from_keypoints(np.array(kpts, copy=False))


def build_oracle_tracks(
    phrase_dir: Path,
    *,
    oracle_model_path: Path,
    oracle_imgsz: int,
    fisheye_backend: str,
    bootstrap_frames: int,
) -> Dict[str, Any]:
    input_video = find_phrase_video(phrase_dir)
    txt_path = find_phrase_txt(phrase_dir)
    frame_corrector = _build_fisheye_frame_corrector(input_video, backend=fisheye_backend)
    frame_transform = frame_corrector.correct

    model = YOLO(str(oracle_model_path))
    yolo_conf = 0.15
    yolo_half = False
    yolo_verbose = False
    tracking_indices = _read_tracking_indices(txt_path)
    init_mode = "manual" if tracking_indices is not None else "bootstrap"
    bootstrap_loc = None
    prefetched_detections = None
    initial_boxes = None
    initial_frame_index = 0

    if tracking_indices is None:
        bootstrap_loc, prefetched_detections = _locate_front_fencers_with_yolo_bootstrap(
            input_video,
            model,
            bootstrap_frames=bootstrap_frames,
            yolo_conf=yolo_conf,
            yolo_verbose=yolo_verbose,
            yolo_imgsz=oracle_imgsz,
            yolo_half=yolo_half,
            frame_transform=frame_transform,
        )
        if bootstrap_loc["left"] is not None and bootstrap_loc["right"] is not None:
            initial_frame_index = int(bootstrap_loc.get("frame_idx") or 0)
            initial_boxes = (
                tuple(bootstrap_loc["left"]["box_xyxy"]),  # type: ignore[index]
                tuple(bootstrap_loc["right"]["box_xyxy"]),  # type: ignore[index]
            )

    tracks_per_frame = extract_tracks_with_jump_safe_tracker(
        str(input_video),
        model,
        initial_detection_indices=tracking_indices,
        initial_detection_bboxes=initial_boxes,
        initial_frame_index=initial_frame_index,
        detection_conf=yolo_conf,
        detection_verbose=yolo_verbose,
        detection_imgsz=oracle_imgsz,
        detection_half=yolo_half,
        prefetched_detections=prefetched_detections,
        frame_transform=frame_transform,
    )
    _, _, _, _, norm_constant, video_angle = analysis.process_video_and_extract_data(
        tracks_per_frame,
        interpolate_max_gap=2,
    )
    return {
        "video_path": input_video,
        "txt_path": txt_path,
        "tracks_per_frame": tracks_per_frame,
        "frame_transform": frame_transform,
        "norm_constant": float(norm_constant),
        "video_angle": video_angle,
        "init_mode": init_mode,
        "bootstrap_locator": bootstrap_loc,
    }


def aggregate_metric(values: Iterable[float]) -> Optional[float]:
    vals = [float(v) for v in values if np.isfinite(v)]
    if not vals:
        return None
    return float(np.mean(vals))


def run_benchmark(
    phrase_dir: Path,
    output_dir: Path,
    *,
    candidate: str,
    oracle_model_path: Path,
    oracle_imgsz: int,
    fisheye_backend: str,
    bootstrap_frames: int,
    yolo_imgsz: int,
    yolo_half: bool,
    pad_fraction: float,
) -> Dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)

    t0 = time.perf_counter()
    oracle = build_oracle_tracks(
        phrase_dir,
        oracle_model_path=oracle_model_path,
        oracle_imgsz=oracle_imgsz,
        fisheye_backend=fisheye_backend,
        bootstrap_frames=bootstrap_frames,
    )
    oracle_secs = time.perf_counter() - t0

    if candidate in RTMPOSE_MODELS:
        t0 = time.perf_counter()
        backend: PoseBackend = RTMPoseBackend(candidate)
        candidate_init_secs = time.perf_counter() - t0
    elif candidate == "yolo26x-crop":
        t0 = time.perf_counter()
        backend = UltralyticsCropPoseBackend(
            oracle_model_path,
            conf=0.15,
            imgsz=yolo_imgsz,
            half=yolo_half,
        )
        candidate_init_secs = time.perf_counter() - t0
    elif candidate == "yolo26s-crop":
        t0 = time.perf_counter()
        backend = UltralyticsCropPoseBackend(
            BUNDLE_ROOT / "yolo26s-pose.pt",
            conf=0.15,
            imgsz=yolo_imgsz,
            half=yolo_half,
        )
        candidate_init_secs = time.perf_counter() - t0
    else:
        raise ValueError(f"Unsupported candidate: {candidate}")

    video_path = oracle["video_path"]
    frame_transform = oracle["frame_transform"]
    tracks_per_frame = oracle["tracks_per_frame"]
    norm_constant = float(oracle["norm_constant"])

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise FileNotFoundError(f"Cannot open phrase video: {video_path}")

    focus_errors: Dict[str, List[float]] = {name: [] for name in FOCUS_JOINTS}
    per_joint_errors: Dict[int, List[float]] = {idx: [] for idx in COCO_KEYPOINT_NAMES}
    valid_joint_counts: Dict[int, int] = {idx: 0 for idx in COCO_KEYPOINT_NAMES}
    pck05 = 0
    pck10 = 0
    total_compared_joints = 0
    crop_times: List[float] = []
    frame_pair_times: List[float] = []
    crops_attempted = 0
    crops_with_prediction = 0
    frames_processed = 0

    try:
        for frame_idx, frame_tracks in enumerate(tracks_per_frame):
            ok, frame = cap.read()
            if not ok or frame is None:
                break
            frame = frame_transform(frame)
            frame_h, frame_w = frame.shape[:2]
            frame_start = time.perf_counter()

            for track in frame_tracks:
                oracle_kpts = track.get("keypoints")
                bbox = bbox_from_track(track)
                if oracle_kpts is None or bbox is None:
                    continue
                oracle_kpts = np.array(oracle_kpts, copy=False)
                ix1, iy1, ix2, iy2 = expand_bbox(bbox, frame_w, frame_h, pad_fraction=pad_fraction)
                if ix2 <= ix1 or iy2 <= iy1:
                    continue
                crop = frame[iy1:iy2, ix1:ix2]
                if crop.size == 0:
                    continue

                crops_attempted += 1
                infer_start = time.perf_counter()
                pred = backend.predict(crop)
                crop_times.append(time.perf_counter() - infer_start)
                if pred.keypoints is None:
                    continue
                crops_with_prediction += 1

                candidate_kpts = np.array(pred.keypoints, copy=True)
                valid = (candidate_kpts[:, 0] > 0) & (candidate_kpts[:, 1] > 0)
                candidate_kpts[valid, 0] += ix1
                candidate_kpts[valid, 1] += iy1

                for joint_idx in COCO_KEYPOINT_NAMES:
                    if joint_idx >= len(candidate_kpts) or joint_idx >= len(oracle_kpts):
                        continue
                    if not valid_point(oracle_kpts[joint_idx]) or not valid_point(candidate_kpts[joint_idx]):
                        continue
                    err = float(np.linalg.norm(candidate_kpts[joint_idx] - oracle_kpts[joint_idx]) / max(norm_constant, 1e-6))
                    per_joint_errors[joint_idx].append(err)
                    valid_joint_counts[joint_idx] += 1
                    total_compared_joints += 1
                    if err <= 0.05:
                        pck05 += 1
                    if err <= 0.10:
                        pck10 += 1
                    for group_name, joints in FOCUS_JOINTS.items():
                        if joint_idx in joints:
                            focus_errors[group_name].append(err)
            frame_pair_times.append(time.perf_counter() - frame_start)
            frames_processed += 1
    finally:
        cap.release()

    summary = {
        "phrase_dir": str(phrase_dir),
        "candidate": candidate,
        "backend_name": backend.name,
        "oracle_model_path": str(oracle_model_path),
        "oracle_imgsz": oracle_imgsz,
        "candidate_init_seconds": float(candidate_init_secs),
        "oracle_build_seconds": float(oracle_secs),
        "frames_processed": frames_processed,
        "crops_attempted": crops_attempted,
        "crops_with_prediction": crops_with_prediction,
        "crop_success_rate": (float(crops_with_prediction) / crops_attempted) if crops_attempted else None,
        "mean_crop_inference_ms": aggregate_metric(v * 1000.0 for v in crop_times),
        "mean_frame_pair_pose_ms": aggregate_metric(v * 1000.0 for v in frame_pair_times),
        "mean_full_pair_pose_fps": (1000.0 / aggregate_metric(v * 1000.0 for v in frame_pair_times)) if frame_pair_times else None,
        "norm_constant": norm_constant,
        "video_angle": oracle["video_angle"],
        "oracle_init_mode": oracle["init_mode"],
        "total_compared_joints": total_compared_joints,
        "pck@0.05": (float(pck05) / total_compared_joints) if total_compared_joints else None,
        "pck@0.10": (float(pck10) / total_compared_joints) if total_compared_joints else None,
        "mean_focus_errors": {
            name: aggregate_metric(vals)
            for name, vals in focus_errors.items()
        },
        "mean_joint_errors": {
            COCO_KEYPOINT_NAMES[idx]: aggregate_metric(vals)
            for idx, vals in per_joint_errors.items()
        },
        "valid_joint_counts": {
            COCO_KEYPOINT_NAMES[idx]: int(count)
            for idx, count in valid_joint_counts.items()
        },
        "bootstrap_locator": analysis.sanitize_for_json(oracle["bootstrap_locator"]),
    }
    out_path = output_dir / f"{phrase_dir.name}_{candidate}_oracle_crop_benchmark.json"
    out_path.write_text(json.dumps(analysis.sanitize_for_json(summary), indent=2), encoding="utf-8")
    summary["output_json"] = str(out_path)
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phrase-dir", type=Path, required=True)
    parser.add_argument(
        "--candidate",
        choices=["rtmpose-s", "rtmpose-t", "yolo26x-crop", "yolo26s-crop"],
        required=True,
    )
    parser.add_argument("--output-dir", type=Path, default=BUNDLE_ROOT / "runtime_outputs" / "experiments")
    parser.add_argument("--oracle-model-path", type=Path, default=DEFAULT_ORACLE_MODEL)
    parser.add_argument("--oracle-imgsz", type=int, default=DEFAULT_ORACLE_IMGSZ)
    parser.add_argument("--yolo-imgsz", type=int, default=640)
    parser.add_argument("--yolo-half", action="store_true")
    parser.add_argument("--bootstrap-frames", type=int, default=8)
    parser.add_argument("--fisheye-backend", choices=("vpi-cuda", "vpi-vic", "opencv"), default="vpi-cuda")
    parser.add_argument("--pad-fraction", type=float, default=DEFAULT_PAD_FRACTION)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    summary = run_benchmark(
        phrase_dir=args.phrase_dir,
        output_dir=args.output_dir,
        candidate=args.candidate,
        oracle_model_path=args.oracle_model_path,
        oracle_imgsz=args.oracle_imgsz,
        fisheye_backend=args.fisheye_backend,
        bootstrap_frames=args.bootstrap_frames,
        yolo_imgsz=args.yolo_imgsz,
        yolo_half=args.yolo_half,
        pad_fraction=args.pad_fraction,
    )
    print(json.dumps(analysis.sanitize_for_json(summary), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
