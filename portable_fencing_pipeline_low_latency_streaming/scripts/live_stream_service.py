#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import site
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import cv2
import numpy as np

BUNDLE_ROOT = Path(__file__).resolve().parent.parent
TENSORRT_PYTHON_PATH = Path("/usr/lib/python3.10/dist-packages")
if TENSORRT_PYTHON_PATH.exists():
    site.addsitedir(str(TENSORRT_PYTHON_PATH))
VPI_PYTHON_PATH = Path("/opt/nvidia/vpi3/lib/aarch64-linux-gnu/python")
if VPI_PYTHON_PATH.exists():
    sys.path.append(str(VPI_PYTHON_PATH))
if str(BUNDLE_ROOT) not in sys.path:
    sys.path.append(str(BUNDLE_ROOT))

from ultralytics import YOLO

from scripts import debug_referee_fps30 as fps30_referee  # type: ignore
from scripts import reprocess_phrase_limb_interp_jumpsafe_experimental as pipeline  # type: ignore
from src.referee import analysis  # type: ignore


_MODEL_CACHE: Dict[str, YOLO] = {}


def _log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _write_message(payload: Dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    sys.stdout.flush()


def _read_json_line() -> Optional[Dict[str, Any]]:
    line = sys.stdin.buffer.readline()
    if not line:
        return None
    return json.loads(line.decode("utf-8"))


def _read_exact(size: int) -> bytes:
    remaining = size
    chunks: List[bytes] = []
    while remaining > 0:
        chunk = sys.stdin.buffer.read(remaining)
        if not chunk:
            raise EOFError(f"Unexpected EOF while reading {size} bytes")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _decode_frame(payload: bytes, encoding: str, *, width: int, height: int) -> np.ndarray:
    encoding = (encoding or "jpeg").lower()
    if encoding in {"jpeg", "jpg", "png"}:
        arr = np.frombuffer(payload, dtype=np.uint8)
        frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if frame is None:
            raise ValueError(f"Failed to decode {encoding} frame")
        return frame
    if encoding == "raw_bgr24":
        expected_size = int(width) * int(height) * 3
        if len(payload) != expected_size:
            raise ValueError(
                f"Expected {expected_size} bytes for raw_bgr24 frame, received {len(payload)}"
            )
        return np.frombuffer(payload, dtype=np.uint8).reshape((int(height), int(width), 3))
    raise ValueError(f"Unsupported frame encoding '{encoding}'")


def _default_output_dir(phrase_dir: Path) -> Path:
    return pipeline.BUNDLE_ROOT / "runtime_outputs" / "live_streaming_sessions" / phrase_dir.name


def _build_live_args(
    *,
    phrase_dir: Path,
    output_dir: Path,
    model_path: Path,
    yolo_conf: float,
    yolo_imgsz: int,
    yolo_half: bool,
    yolo_verbose: bool,
    bootstrap_frames: int,
    fisheye_backend: str,
) -> argparse.Namespace:
    return argparse.Namespace(
        phrase_dir=phrase_dir,
        model_path=model_path,
        yolo_conf=yolo_conf,
        yolo_imgsz=yolo_imgsz,
        yolo_half=yolo_half,
        yolo_verbose=yolo_verbose,
        bootstrap_frames=bootstrap_frames,
        fisheye_backend=fisheye_backend,
        frame_prefetch=4,
        video_reader="opencv",
        write_corrected_video=False,
        write_yolo_overlay=False,
        write_repaired_overlay=False,
        write_bootstrap_debug=False,
        write_repaired_excel=False,
        write_fps30_debug_log=False,
        sam_threshold=0.15,
        sam_mask_threshold=0.5,
        output_dir=output_dir,
        repair_only=False,
        copy_inputs=False,
        progress=False,
    )


def _load_cached_model(model_path: Path) -> YOLO:
    key = str(model_path.resolve())
    model = _MODEL_CACHE.get(key)
    if model is None:
        _log(f"[LOCAL_ANALYZER] Loading YOLO model: {key}")
        model = YOLO(key)
        _MODEL_CACHE[key] = model
    return model


def _warm_model(
    model: YOLO,
    *,
    width: int,
    height: int,
    yolo_conf: float,
    yolo_imgsz: int,
    yolo_half: bool,
    yolo_verbose: bool,
) -> None:
    warm_frame = np.zeros((int(height), int(width), 3), dtype=np.uint8)
    predict_kwargs = analysis.ultralytics_predict_kwargs(
        verbose=yolo_verbose,
        conf=yolo_conf,
        imgsz=yolo_imgsz,
        half=yolo_half,
    )
    model(warm_frame, **predict_kwargs)


class LiveTrackingSession:
    def __init__(
        self,
        *,
        width: int,
        height: int,
        model_path: Path,
        yolo_conf: float,
        yolo_imgsz: int,
        yolo_half: bool,
        yolo_verbose: bool,
        bootstrap_frames: int,
        fisheye_backend: str,
    ) -> None:
        self.raw_width = int(width)
        self.raw_height = int(height)
        self.model_path = model_path.resolve()
        self.yolo_conf = float(yolo_conf)
        self.yolo_imgsz = int(yolo_imgsz)
        self.yolo_half = bool(yolo_half)
        self.yolo_verbose = bool(yolo_verbose)
        self.bootstrap_frames = max(1, int(bootstrap_frames))
        self.fisheye_backend = str(fisheye_backend)

        self.model = _load_cached_model(self.model_path)
        if self.fisheye_backend == "none":
            self.frame_transform = None
            self.frame_width = self.raw_width
            self.frame_height = self.raw_height
        else:
            fisheye_corrector = pipeline.FisheyeFrameCorrector(
                self.raw_width,
                self.raw_height,
                backend=self.fisheye_backend,
            )
            self.frame_transform = fisheye_corrector.correct
            self.frame_width = int(fisheye_corrector.crop_w)
            self.frame_height = int(fisheye_corrector.crop_h)
        self.predict_kwargs = analysis.ultralytics_predict_kwargs(
            verbose=self.yolo_verbose,
            conf=self.yolo_conf,
            imgsz=self.yolo_imgsz,
            half=self.yolo_half,
        )
        self.tracker = pipeline.JumpSafeTwoFencerTracker(
            frame_w=self.frame_width,
            frame_h=self.frame_height,
        )
        self.tracks_per_frame: List[List[Dict[str, Any]]] = []
        self.bootstrap_locator: Optional[Dict[str, Any]] = None
        self.initial_detection_bboxes: Optional[
            Tuple[Tuple[float, float, float, float], Tuple[float, float, float, float]]
        ] = None
        self.initial_frame_index = 0
        self.bootstrap_buffer: List[np.ndarray] = []
        self.bootstrap_frame_indices: List[int] = []
        self.bootstrap_prefetched_detections: Dict[int, List[np.ndarray]] = {}
        self.bootstrap_ready = False
        self.received_frames = 0
        self.noncontiguous_frames = False
        self.last_capture_frame_number: Optional[int] = None

    def push_frame(self, frame_bgr: np.ndarray, frame_number: int) -> None:
        expected = self.received_frames
        if frame_number != expected:
            self.noncontiguous_frames = True
        self.last_capture_frame_number = int(frame_number)

        corrected = self.frame_transform(frame_bgr) if self.frame_transform is not None else frame_bgr
        seq_frame_idx = self.received_frames
        self.received_frames += 1

        if not self.bootstrap_ready:
            self.bootstrap_buffer.append(corrected)
            self.bootstrap_frame_indices.append(seq_frame_idx)
            if len(self.bootstrap_buffer) >= self.bootstrap_frames:
                self._flush_bootstrap_buffer(force=False)
            return

        self._process_tracking_frame(seq_frame_idx, corrected)

    def finalize(
        self,
        *,
        phrase_dir: Path,
        output_dir: Path,
        txt_path: Path,
        video_path: Path,
    ) -> Dict[str, Any]:
        self._flush_bootstrap_buffer(force=True)
        if not self.tracks_per_frame:
            raise RuntimeError("No tracks were produced from the live frame stream")

        output_dir.mkdir(parents=True, exist_ok=True)
        staged_video = pipeline._stage_input_file(video_path, output_dir / video_path.name, copy_inputs=False)
        staged_txt = pipeline._stage_input_file(txt_path, output_dir / txt_path.name, copy_inputs=False)

        left_x, left_y, right_x, right_y, norm_constant, video_angle = analysis.process_video_and_extract_data(
            self.tracks_per_frame,
            interpolate_max_gap=pipeline.BASE_INTERPOLATE_MAX_GAP,
        )

        before_left = pipeline.detect_limb_anomalies(left_x, left_y, side_label="left")
        before_right = pipeline.detect_limb_anomalies(right_x, right_y, side_label="right")
        left_x_fixed, left_y_fixed, left_repair = pipeline.repair_limb_runs(
            left_x,
            left_y,
            before_left,
            side_label="left",
        )
        right_x_fixed, right_y_fixed, right_repair = pipeline.repair_limb_runs(
            right_x,
            right_y,
            before_right,
            side_label="right",
        )
        after_left = pipeline.detect_limb_anomalies(left_x_fixed, left_y_fixed, side_label="left")
        after_right = pipeline.detect_limb_anomalies(right_x_fixed, right_y_fixed, side_label="right")

        phrase = pipeline.parse_txt_file(str(staged_txt), video_path=str(staged_video))
        raw_decision = pipeline.referee_decision(
            phrase,
            left_x,
            left_y,
            right_x,
            right_y,
            normalisation_constant=norm_constant,
        )
        repaired_decision = pipeline.referee_decision(
            phrase,
            left_x_fixed,
            left_y_fixed,
            right_x_fixed,
            right_y_fixed,
            normalisation_constant=norm_constant,
        )

        final_analysis_result_path = output_dir / "analysis_result.json"
        final_fps30_decision = fps30_referee.run_referee_on_keypoints(
            txt_path=staged_txt,
            video_path=staged_video,
            left_x=left_x_fixed,
            left_y=left_y_fixed,
            right_x=right_x_fixed,
            right_y=right_y_fixed,
            normalisation_constant=norm_constant,
            decision_output_path=final_analysis_result_path,
            debug_output_path=None,
            debug_logging=False,
        )

        result = {
            "input_phrase_dir": str(phrase_dir),
            "model_path": str(self.model_path),
            "repair_only": False,
            "source_excel": None,
            "fisheye_backend": self.fisheye_backend,
            "yolo_imgsz": self.yolo_imgsz,
            "yolo_half": self.yolo_half,
            "corrected_video_artifact": None,
            "yolo_detection_confidence": self.yolo_conf,
            "tracking_indices": None,
            "manual_indices_present": False,
            "init_mode": "bootstrap" if self.initial_detection_bboxes is not None else "auto",
            "bootstrap_fencer_init": self.bootstrap_locator,
            "bootstrap_outputs": None,
            "bootstrap_used": bool(self.initial_detection_bboxes is not None),
            "bootstrap_frame_index": self.initial_frame_index if self.initial_detection_bboxes is not None else None,
            "tracker_mode": "jump_safe_live_stream",
            "yolo_all_people_overlay": None,
            "repaired_excel": None,
            "repaired_overlay": None,
            "normalisation_constant": norm_constant,
            "video_angle": video_angle,
            "frames_analyzed": len(self.tracks_per_frame),
            "before_anomalies": {"left": before_left, "right": before_right},
            "after_anomalies": {"left": after_left, "right": after_right},
            "repair_report": {"left": left_repair, "right": right_repair},
            "original_analysis_result": None,
            "reextracted_decision": analysis.sanitize_for_json(raw_decision),
            "repaired_decision": analysis.sanitize_for_json(repaired_decision),
            "final_fps30_decision": final_fps30_decision,
            "final_analysis_result_json": str(final_analysis_result_path),
            "fps30_debug_log": None,
            "live_stream_metadata": {
                "received_frames": self.received_frames,
                "capture_noncontiguous": self.noncontiguous_frames,
                "last_capture_frame_number": self.last_capture_frame_number,
            },
        }
        result_path = output_dir / "analysis_result_limb_interp_experimental.json"
        result_path.write_text(
            json.dumps(analysis.sanitize_for_json(result), indent=2),
            encoding="utf-8",
        )

        summary = {
            "before_left_anomalies": before_left["flagged_count"],
            "after_left_anomalies": after_left["flagged_count"],
            "before_right_anomalies": before_right["flagged_count"],
            "after_right_anomalies": after_right["flagged_count"],
            "original_winner": None,
            "reextracted_winner": raw_decision.get("winner"),
            "repaired_winner": repaired_decision.get("winner"),
            "final_fps30_winner": final_fps30_decision.get("winner"),
            "final_analysis_result_json": str(final_analysis_result_path),
            "result_json": str(result_path),
            "live_frames_received": self.received_frames,
        }
        return {
            "summary": analysis.sanitize_for_json(summary),
            "result": analysis.sanitize_for_json(result),
            "result_path": str(result_path),
            "final_analysis_result_path": str(final_analysis_result_path),
            "output_dir": str(output_dir),
        }

    def _flush_bootstrap_buffer(self, *, force: bool) -> None:
        if self.bootstrap_ready:
            return
        if not self.bootstrap_buffer:
            return
        if not force and len(self.bootstrap_buffer) < self.bootstrap_frames:
            return

        locator, prefetched = self._locate_front_fencers_from_buffer(self.bootstrap_buffer)
        self.bootstrap_locator = locator
        self.bootstrap_prefetched_detections = prefetched
        if locator.get("left") is not None and locator.get("right") is not None:
            self.initial_frame_index = int(locator.get("frame_idx") or 0)
            self.initial_detection_bboxes = (
                tuple(locator["left"]["box_xyxy"]),  # type: ignore[index]
                tuple(locator["right"]["box_xyxy"]),  # type: ignore[index]
            )

        buffered_frames = list(self.bootstrap_buffer)
        self.bootstrap_ready = True
        self.bootstrap_buffer = []
        self.bootstrap_frame_indices = []
        for frame_idx, frame in enumerate(buffered_frames):
            detections = self.bootstrap_prefetched_detections.get(frame_idx)
            self._process_tracking_frame(frame_idx, frame, detections=detections)
        self._backfill_preinit_tracks()

    def _locate_front_fencers_from_buffer(
        self,
        frames_bgr: List[np.ndarray],
    ) -> Tuple[Dict[str, Any], Dict[int, List[np.ndarray]]]:
        scanned_frames = len(frames_bgr)
        raw_candidate_count = 0
        accepted_candidate_count = 0
        clusters: List[Dict[str, object]] = []
        prefetched_detections: Dict[int, List[np.ndarray]] = {}

        if scanned_frames == 0:
            return ({
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
            }, prefetched_detections)

        if pipeline._model_supports_batched_bootstrap(self.model):
            predict_kwargs = analysis.ultralytics_predict_kwargs(
                verbose=self.yolo_verbose,
                conf=self.yolo_conf,
                imgsz=self.yolo_imgsz,
                half=self.yolo_half,
                batch=len(frames_bgr),
            )
            results = self.model(frames_bgr, **predict_kwargs)
        else:
            results = [
                self.model(
                    frame,
                    **analysis.ultralytics_predict_kwargs(
                        verbose=self.yolo_verbose,
                        conf=self.yolo_conf,
                        imgsz=self.yolo_imgsz,
                        half=self.yolo_half,
                    ),
                )[0]
                for frame in frames_bgr
            ]

        height, width = frames_bgr[0].shape[:2]
        for frame_idx, result in enumerate(results):
            detections: List[np.ndarray] = []
            if result.keypoints is not None:
                raw_kpts = result.keypoints.xy
                if hasattr(raw_kpts, "cpu"):
                    raw_kpts = raw_kpts.cpu().numpy()
                else:
                    raw_kpts = np.array(raw_kpts)
                for det_idx in range(raw_kpts.shape[0]):
                    detections.append(np.array(raw_kpts[det_idx], copy=True))
            prefetched_detections[frame_idx] = detections

            raw_candidate_count += len(detections)
            for det_idx, det_kpts in enumerate(detections):
                candidate = pipeline._bootstrap_candidate_from_detection(
                    det_kpts,
                    frame_idx=frame_idx,
                    detection_idx=det_idx,
                    width=width,
                    height=height,
                )
                if candidate is None:
                    continue
                accepted_candidate_count += 1
                pipeline._append_bootstrap_candidate(
                    clusters,
                    candidate,
                    width=width,
                    height=height,
                )

        left_clusters = [cluster for cluster in clusters if cluster["side"] == "left"]
        right_clusters = [cluster for cluster in clusters if cluster["side"] == "right"]
        left_summaries = [
            pipeline._summarize_bootstrap_cluster(cluster, scanned_frames)
            for cluster in left_clusters
        ]
        right_summaries = [
            pipeline._summarize_bootstrap_cluster(cluster, scanned_frames)
            for cluster in right_clusters
        ]
        best_pair = pipeline._choose_bootstrap_pair(left_clusters, right_clusters, scanned_frames)

        locator: Dict[str, Any] = {
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
        return locator, prefetched_detections

    def _snapshot_frame_tracks(self, observed_frame_idx: int) -> List[Dict[str, Any]]:
        frame_tracks: List[Dict[str, Any]] = []
        for tid in (0, 1):
            track = self.tracker.tracks.get(tid)
            observed = track is not None and track.last_seen == observed_frame_idx
            kpts = self.tracker.get_track(tid) if observed else None
            bbox = analysis.bbox_from_keypoints(kpts) if kpts is not None else None
            frame_tracks.append(
                {
                    "track_id": tid,
                    "keypoints": kpts.copy() if kpts is not None else None,
                    "box": np.array(bbox, dtype=float) if bbox else None,
                    "observed": observed,
                }
            )
        return frame_tracks

    def _target_det_score(
        self,
        target_box: Tuple[float, float, float, float],
        det_box: Optional[Tuple[int, int, int, int]],
    ) -> float:
        if det_box is None:
            return -1.0
        target_i = tuple(int(round(v)) for v in target_box)
        iou = analysis.bbox_iou(target_i, det_box)
        tcx, tcy = analysis.bbox_center(target_i)
        dcx, dcy = analysis.bbox_center(det_box)
        center_dist = math.hypot(tcx - dcx, tcy - dcy) / max(self.tracker.frame_diag, 1.0)
        return float(iou - 0.25 * center_dist)

    def _match_initial_boxes_to_detection_indices(
        self,
        detections: List[np.ndarray],
        target_boxes: Tuple[Tuple[float, float, float, float], Tuple[float, float, float, float]],
    ) -> Optional[Tuple[int, int]]:
        det_boxes: List[Optional[Tuple[int, int, int, int]]] = [
            analysis.bbox_from_keypoints(det) for det in detections
        ]
        if len(det_boxes) < 2:
            return None

        left_target, right_target = target_boxes
        left_ranked = sorted(
            ((idx, self._target_det_score(left_target, det_box)) for idx, det_box in enumerate(det_boxes)),
            key=lambda item: item[1],
            reverse=True,
        )
        right_ranked = sorted(
            ((idx, self._target_det_score(right_target, det_box)) for idx, det_box in enumerate(det_boxes)),
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

        return int(left_idx), int(right_idx)

    def _recover_initial_keypoints_from_crops(
        self,
        frame_bgr: np.ndarray,
        target_boxes: Tuple[Tuple[float, float, float, float], Tuple[float, float, float, float]],
    ) -> Optional[Tuple[np.ndarray, np.ndarray]]:
        frame_h, frame_w = frame_bgr.shape[:2]

        def _recover_one(target_box: Tuple[float, float, float, float]) -> Optional[np.ndarray]:
            x1, y1, x2, y2 = [float(v) for v in target_box]
            pad = max(48, int(round(max(x2 - x1, y2 - y1) * 0.35)))
            ix1 = max(0, int(math.floor(x1)) - pad)
            iy1 = max(0, int(math.floor(y1)) - pad)
            ix2 = min(frame_w, int(math.ceil(x2)) + pad)
            iy2 = min(frame_h, int(math.ceil(y2)) + pad)
            if ix2 <= ix1 or iy2 <= iy1:
                return None

            crop = frame_bgr[iy1:iy2, ix1:ix2]
            if crop.size == 0:
                return None

            crop_results = self.model(crop, **self.predict_kwargs)
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
                det_box = analysis.bbox_from_keypoints(global_kpts)
                score = self._target_det_score(target_box, det_box)
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
        self,
        frame_bgr: np.ndarray,
        detections: List[np.ndarray],
        det_meta: List[Dict[str, Any]],
        costs: Dict[Tuple[int, int], float],
    ) -> Tuple[List[np.ndarray], List[Dict[str, Any]], Dict[Tuple[int, int], float], Dict[int, np.ndarray]]:
        augmented = list(detections)
        augmented_meta = list(det_meta)
        augmented_costs = dict(costs)
        recovered_by_tid: Dict[int, np.ndarray] = {}
        if not self.tracker.initialized:
            return augmented, augmented_meta, augmented_costs, recovered_by_tid

        frame_h, frame_w = frame_bgr.shape[:2]
        for tid in (0, 1):
            track = self.tracker.tracks.get(tid)
            if track is None or track.bbox is None:
                continue

            best_existing_cost = float("inf")
            for d_idx, _det_kpts in enumerate(augmented):
                cost = augmented_costs.get((tid, d_idx))
                if cost is not None:
                    best_existing_cost = min(best_existing_cost, cost)

            if best_existing_cost <= self.tracker.reacquire_cost_threshold:
                continue

            x1, y1, x2, y2 = track.bbox
            pred_cx, pred_cy = track.predict_center()
            box_w = max(float(x2 - x1), 1.0)
            box_h = max(float(y2 - y1), 1.0)
            half_w = max(110.0, box_w * 2.1)
            half_h = max(140.0, box_h * 1.5)
            ix1 = max(0, int(math.floor(pred_cx - half_w)))
            iy1 = max(0, int(math.floor(pred_cy - half_h)))
            ix2 = min(frame_w, int(math.ceil(pred_cx + half_w)))
            iy2 = min(frame_h, int(math.ceil(pred_cy + half_h)))
            if ix2 <= ix1 or iy2 <= iy1:
                continue

            crop = frame_bgr[iy1:iy2, ix1:ix2]
            if crop.size == 0:
                continue

            crop_results = self.model(crop, **self.predict_kwargs)
            if len(crop_results) == 0 or crop_results[0].keypoints is None:
                continue

            crop_kpts = crop_results[0].keypoints.xy
            if hasattr(crop_kpts, "cpu"):
                crop_kpts = crop_kpts.cpu().numpy()
            else:
                crop_kpts = np.array(crop_kpts)

            best_det = None
            best_det_meta = None
            best_cost = float("inf")
            for det_kpts in crop_kpts:
                global_kpts = np.array(det_kpts, copy=True)
                valid = (global_kpts[:, 0] > 0) & (global_kpts[:, 1] > 0)
                global_kpts[valid, 0] += ix1
                global_kpts[valid, 1] += iy1
                meta = self.tracker.build_detection_meta(global_kpts)
                if meta["bbox"] is None:
                    continue
                cost = self.tracker._soft_match_cost(tid, track, global_kpts, meta)
                if cost is None:
                    continue
                if cost < best_cost:
                    best_cost = cost
                    best_det = global_kpts
                    best_det_meta = meta

            if best_det is None or best_cost > self.tracker.max_candidate_cost:
                continue

            best_bbox = best_det_meta["bbox"] if best_det_meta is not None else analysis.bbox_from_keypoints(best_det)
            if best_bbox is None:
                continue

            recovered_by_tid[tid] = best_det
            duplicate = False
            for existing_meta in augmented_meta:
                det_bbox = existing_meta["bbox"]
                if det_bbox is None:
                    continue
                if analysis.bbox_iou(best_bbox, det_bbox) > 0.7:
                    duplicate = True
                    break
            if duplicate:
                continue

            augmented.append(best_det)
            augmented_meta.append(best_det_meta if best_det_meta is not None else self.tracker.build_detection_meta(best_det))
            new_idx = len(augmented) - 1
            for active_tid in (0, 1):
                active_track = self.tracker.tracks.get(active_tid)
                if active_track is None:
                    continue
                cost = self.tracker._soft_match_cost(active_tid, active_track, best_det, augmented_meta[new_idx])
                if cost is not None:
                    augmented_costs[(active_tid, new_idx)] = cost

        return augmented, augmented_meta, augmented_costs, recovered_by_tid

    def _process_tracking_frame(
        self,
        frame_idx: int,
        frame_bgr: np.ndarray,
        *,
        detections: Optional[List[np.ndarray]] = None,
    ) -> None:
        detections = [np.array(det, copy=True) for det in detections] if detections is not None else None
        if detections is None:
            detections = []
            results = self.model(frame_bgr, **self.predict_kwargs)
            if len(results) > 0 and results[0].keypoints is not None:
                raw_kpts = results[0].keypoints.xy
                if hasattr(raw_kpts, "cpu"):
                    raw_kpts = raw_kpts.cpu().numpy()
                else:
                    raw_kpts = np.array(raw_kpts)
                for idx in range(raw_kpts.shape[0]):
                    detections.append(raw_kpts[idx])

        det_meta = self.tracker.build_detection_meta_list(detections)
        costs: Dict[Tuple[int, int], float] = {}
        if self.tracker.initialized and detections:
            costs = self.tracker.build_costs(detections, det_meta)

        recovered_by_tid: Dict[int, np.ndarray] = {}
        if frame_idx > 0 and self.tracker.initialized:
            detections, det_meta, costs, recovered_by_tid = self._augment_detections_with_track_crops(
                frame_bgr,
                detections,
                det_meta,
                costs,
            )

        forced_init_pending = (
            not self.tracker.initialized
            and frame_idx < self.initial_frame_index
            and self.initial_detection_bboxes is not None
        )
        if forced_init_pending:
            self.tracks_per_frame.append(
                [
                    {"track_id": tid, "keypoints": None, "box": None, "observed": False}
                    for tid in (0, 1)
                ]
            )
            return

        forced_init_applied = False
        if frame_idx == self.initial_frame_index and not self.tracker.initialized and self.initial_detection_bboxes is not None:
            chosen_indices = self._match_initial_boxes_to_detection_indices(detections, self.initial_detection_bboxes)
            recovered_keypoints = None
            if chosen_indices is None:
                recovered_keypoints = self._recover_initial_keypoints_from_crops(frame_bgr, self.initial_detection_bboxes)
                if recovered_keypoints is not None:
                    forced = self.tracker.initialize_with_keypoints(recovered_keypoints[0], recovered_keypoints[1])
                    forced_init_applied = bool(forced)
            else:
                forced = self.tracker.initialize_with_detection_indices(
                    detections,
                    int(chosen_indices[0]),
                    int(chosen_indices[1]),
                )
                forced_init_applied = bool(forced)

        if forced_init_applied:
            self.tracker.frame_idx += 1
        else:
            self.tracker.update(detections, det_meta=det_meta, costs=costs)
            current_frame_idx = self.tracker.frame_idx - 1
            for tid, recovered_kpts in recovered_by_tid.items():
                track = self.tracker.tracks.get(tid)
                if track is not None and track.last_seen == current_frame_idx:
                    continue
                if track is None:
                    self.tracker.tracks[tid] = analysis._TrackState(tid, recovered_kpts, current_frame_idx)
                else:
                    self.tracker._overwrite_track(track, recovered_kpts)
                self.tracker.lost_tracks[tid] = None
                self.tracker.miss_count[tid] = 0

        current_frame_idx = self.tracker.frame_idx - 1
        self.tracks_per_frame.append(self._snapshot_frame_tracks(current_frame_idx))

    def _backfill_preinit_tracks(self) -> None:
        if self.initial_frame_index <= 0:
            return
        if self.initial_frame_index >= len(self.tracks_per_frame):
            return

        seed_tracks = self.tracks_per_frame[self.initial_frame_index]
        seed_map = {track["track_id"]: track for track in seed_tracks}
        left_seed = seed_map.get(0, {}).get("keypoints")
        right_seed = seed_map.get(1, {}).get("keypoints")
        if left_seed is None or right_seed is None:
            return

        backfill_tracker = self.tracker.__class__(self.frame_width, self.frame_height, max_miss=self.tracker.max_miss)
        if not backfill_tracker.initialize_with_keypoints(np.array(left_seed, copy=True), np.array(right_seed, copy=True)):
            return

        for backfill_frame_idx in range(self.initial_frame_index - 1, -1, -1):
            detections = [
                np.array(det_kpts, copy=True)
                for det_kpts in self.bootstrap_prefetched_detections.get(backfill_frame_idx, [])
            ]
            observed_frame_idx = backfill_tracker.frame_idx
            det_meta = backfill_tracker.build_detection_meta_list(detections) if detections else None
            costs = backfill_tracker.build_costs(detections, det_meta) if detections else None
            backfill_tracker.update(detections, det_meta=det_meta, costs=costs)
            frame_tracks: List[Dict[str, Any]] = []
            for tid in (0, 1):
                track = backfill_tracker.tracks.get(tid)
                observed = track is not None and track.last_seen == observed_frame_idx
                kpts = backfill_tracker.get_track(tid) if observed else None
                bbox = analysis.bbox_from_keypoints(kpts) if kpts is not None else None
                frame_tracks.append(
                    {
                        "track_id": tid,
                        "keypoints": kpts.copy() if kpts is not None else None,
                        "box": np.array(bbox, dtype=float) if bbox else None,
                        "observed": observed,
                    }
                )
            self.tracks_per_frame[backfill_frame_idx] = frame_tracks


def _build_client_payload(job_result: Dict[str, Any], *, processing_mode: str) -> Dict[str, Any]:
    result = dict(job_result["result"]["final_fps30_decision"])
    if "natural_language_reason" not in result and "reason" in result:
        result["natural_language_reason"] = result["reason"]
    result["processing_mode"] = processing_mode
    result["analysis_output_dir"] = job_result["output_dir"]
    result["analysis_result_json"] = job_result["final_analysis_result_path"]
    result["experimental_result_json"] = job_result["result_path"]
    result["analysis_summary"] = job_result["summary"]
    return analysis.sanitize_for_json(result)


def _run_offline_fallback(
    *,
    phrase_dir: Path,
    output_dir: Path,
    model_path: Path,
    yolo_conf: float,
    yolo_imgsz: int,
    yolo_half: bool,
    yolo_verbose: bool,
    bootstrap_frames: int,
    fisheye_backend: str,
    preloaded_model: Optional[YOLO],
) -> Dict[str, Any]:
    args = _build_live_args(
        phrase_dir=phrase_dir,
        output_dir=output_dir,
        model_path=model_path,
        yolo_conf=yolo_conf,
        yolo_imgsz=yolo_imgsz,
        yolo_half=yolo_half,
        yolo_verbose=yolo_verbose,
        bootstrap_frames=bootstrap_frames,
        fisheye_backend=fisheye_backend,
    )
    job_result = pipeline.run_phrase_job(args, preloaded_model=preloaded_model)
    return _build_client_payload(job_result, processing_mode="offline_fallback")


def _phrase_dir_from_message(args: argparse.Namespace, start_message: Dict[str, Any]) -> Path:
    phrase_value = start_message.get("phrase_dir")
    if phrase_value:
        return Path(str(phrase_value)).resolve()
    if args.phrase_dir is None:
        raise RuntimeError("session_start is missing phrase_dir and no --phrase-dir fallback was provided")
    return args.phrase_dir.resolve()


def _output_dir_from_message(
    args: argparse.Namespace,
    start_message: Dict[str, Any],
    *,
    phrase_dir: Path,
) -> Path:
    output_value = start_message.get("output_dir")
    if output_value:
        return Path(str(output_value)).resolve()
    if args.output_dir is not None:
        return args.output_dir.resolve()
    return _default_output_dir(phrase_dir)


def _run_one_session(args: argparse.Namespace, start_message: Dict[str, Any]) -> int:
    if start_message.get("type") != "session_start":
        raise RuntimeError(f"Expected session_start, received {start_message.get('type')}")

    phrase_dir = _phrase_dir_from_message(args, start_message)
    output_dir = _output_dir_from_message(args, start_message, phrase_dir=phrase_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    session = LiveTrackingSession(
        width=int(start_message["width"]),
        height=int(start_message["height"]),
        model_path=args.model_path.resolve(),
        yolo_conf=args.yolo_conf,
        yolo_imgsz=args.yolo_imgsz,
        yolo_half=args.yolo_half,
        yolo_verbose=args.yolo_verbose,
        bootstrap_frames=args.bootstrap_frames,
        fisheye_backend=args.fisheye_backend,
    )
    _write_message({"type": "ready"})

    overflowed = False
    total_frames = 0
    txt_path: Optional[Path] = None
    signal_filename: Optional[str] = None
    while True:
        message = _read_json_line()
        if message is None:
            raise EOFError("EOF before session_end")
        msg_type = message.get("type")

        if msg_type == "frame":
            frame_bytes = _read_exact(int(message["size"]))
            frame = _decode_frame(
                frame_bytes,
                str(message.get("encoding", "jpeg")),
                width=session.raw_width,
                height=session.raw_height,
            )
            session.push_frame(frame, int(message["frame_number"]))
            continue

        if msg_type == "session_end":
            total_frames = int(message.get("total_frames") or 0)
            overflowed = bool(message.get("overflowed"))
            signal_filename = str(message["signal_filename"])
            signal_bytes = _read_exact(int(message["signal_size"]))
            txt_path = phrase_dir / signal_filename
            txt_path.write_bytes(signal_bytes)
            break

        if msg_type == "cancel_session":
            _write_message({"type": "cancelled"})
            return 0

        raise RuntimeError(f"Unsupported message type '{msg_type}'")

    if signal_filename is None or txt_path is None:
        raise RuntimeError("Missing signal file payload at session end")

    input_video = pipeline._find_input_video(phrase_dir)
    frame_count_mismatch = False
    expected_last_frame: Optional[int] = None
    if total_frames > 0:
        expected_last_frame = total_frames - 1
        frame_count_mismatch = (
            session.received_frames != total_frames
            or session.last_capture_frame_number != expected_last_frame
        )

    live_degraded = overflowed or session.noncontiguous_frames or frame_count_mismatch
    if live_degraded:
        _log(
            "[LOCAL_ANALYZER] Falling back to offline processing "
            f"(overflowed={overflowed}, noncontiguous={session.noncontiguous_frames}, "
            f"received_frames={session.received_frames}, capture_total_frames={total_frames}, "
            f"last_capture_frame_number={session.last_capture_frame_number}, "
            f"expected_last_frame={expected_last_frame})"
        )
        payload = _run_offline_fallback(
            phrase_dir=phrase_dir,
            output_dir=output_dir,
            model_path=args.model_path.resolve(),
            yolo_conf=args.yolo_conf,
            yolo_imgsz=args.yolo_imgsz,
            yolo_half=args.yolo_half,
            yolo_verbose=args.yolo_verbose,
            bootstrap_frames=args.bootstrap_frames,
            fisheye_backend=args.fisheye_backend,
            preloaded_model=session.model,
        )
        payload["capture_total_frames"] = total_frames
        payload["live_received_frames"] = session.received_frames
        payload["live_last_capture_frame_number"] = session.last_capture_frame_number
        payload["live_noncontiguous_frames"] = session.noncontiguous_frames
        payload["live_overflowed"] = overflowed
        payload["live_frame_count_mismatch"] = frame_count_mismatch
        _write_message({"type": "result", "result": payload})
        return 0

    try:
        job_result = session.finalize(
            phrase_dir=phrase_dir,
            output_dir=output_dir,
            txt_path=txt_path,
            video_path=input_video,
        )
        payload = _build_client_payload(job_result, processing_mode="live_streaming")
        payload["capture_total_frames"] = total_frames
        payload["live_received_frames"] = session.received_frames
        payload["live_last_capture_frame_number"] = session.last_capture_frame_number
        _write_message({"type": "result", "result": payload})
        return 0
    except Exception as exc:
        _log(f"[LOCAL_ANALYZER] Live finalize failed, retrying offline: {exc}")
        payload = _run_offline_fallback(
            phrase_dir=phrase_dir,
            output_dir=output_dir,
            model_path=args.model_path.resolve(),
            yolo_conf=args.yolo_conf,
            yolo_imgsz=args.yolo_imgsz,
            yolo_half=args.yolo_half,
            yolo_verbose=args.yolo_verbose,
            bootstrap_frames=args.bootstrap_frames,
            fisheye_backend=args.fisheye_backend,
            preloaded_model=session.model,
        )
        payload["capture_total_frames"] = total_frames
        payload["live_finalize_error"] = str(exc)
        _write_message({"type": "result", "result": payload})
        return 0


def _run_single_session_loop(args: argparse.Namespace) -> int:
    start_message = _read_json_line()
    if start_message is None:
        raise RuntimeError("Expected session_start message on stdin")
    try:
        return _run_one_session(args, start_message)
    except Exception as exc:
        _write_message({"type": "error", "error_message": str(exc)})
        raise


def _run_persistent_loop(args: argparse.Namespace) -> int:
    model = _load_cached_model(args.model_path.resolve())
    warmed_shapes: Set[Tuple[int, int]] = set()
    _write_message({"type": "service_ready"})
    while True:
        message = _read_json_line()
        if message is None:
            return 0

        msg_type = str(message.get("type") or "")
        try:
            if msg_type == "warmup":
                width = int(message["width"])
                height = int(message["height"])
                shape_key = (width, height)
                if shape_key not in warmed_shapes:
                    _log(f"[LOCAL_ANALYZER] Warmup inference for {width}x{height}")
                    _warm_model(
                        model,
                        width=width,
                        height=height,
                        yolo_conf=args.yolo_conf,
                        yolo_imgsz=args.yolo_imgsz,
                        yolo_half=args.yolo_half,
                        yolo_verbose=args.yolo_verbose,
                    )
                    warmed_shapes.add(shape_key)
                _write_message({"type": "warmup_complete", "width": width, "height": height})
                continue

            if msg_type == "shutdown":
                _write_message({"type": "shutdown_complete"})
                return 0

            if msg_type == "session_start":
                _run_one_session(args, message)
                continue

            raise RuntimeError(f"Unsupported persistent message type '{msg_type}'")
        except Exception as exc:
            _write_message({"type": "error", "error_message": str(exc)})
            _log(f"[LOCAL_ANALYZER] Persistent loop error: {exc}")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local stdin/stdout live analysis service.")
    parser.add_argument("--phrase-dir", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--model-path", type=Path, default=pipeline.DEFAULT_MODEL_PATH)
    parser.add_argument("--yolo-conf", type=float, default=0.15)
    parser.add_argument("--yolo-imgsz", type=int, default=512)
    parser.add_argument("--yolo-half", action="store_true")
    parser.add_argument("--yolo-verbose", action="store_true")
    parser.add_argument("--bootstrap-frames", type=int, default=pipeline.BOOTSTRAP_FRAMES_DEFAULT)
    parser.add_argument(
        "--fisheye-backend",
        choices=pipeline.FISHEYE_BACKENDS,
        default="none",
    )
    parser.add_argument("--persistent", action="store_true")
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()
    if args.persistent:
        return _run_persistent_loop(args)
    return _run_single_session_loop(args)


if __name__ == "__main__":
    raise SystemExit(main())
