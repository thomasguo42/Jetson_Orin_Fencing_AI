#!/usr/bin/env python3
from __future__ import annotations

import csv
import gc
import json
import sys
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Tuple

ROOT = Path("/home/thomas/fencing")
BUNDLE = ROOT / "portable_fencing_pipeline_low_latency"
sys.path.append(str(BUNDLE))
sys.path.append(str(BUNDLE / "scripts"))

import torch

import reprocess_phrase_limb_interp_jumpsafe_experimental as pipeline


OUTPUT_ROOT = BUNDLE / "runtime_outputs" / "model_sweep_20260405"
MATRIX_SUMMARY_PATH = BUNDLE / "experiments" / "yolov8_pose" / "matrix_all_20260404" / "matrix_summary_all9.json"

MODEL_ENGINES: Dict[str, Path] = {
    "yolov8s-pose": BUNDLE / "experiments" / "yolov8_pose" / "matrix_all_20260404" / "yolov8s-pose" / "yolov8s-pose_fast_fp16_ultra.engine",
    "yolov8m-pose": BUNDLE / "experiments" / "yolov8_pose" / "matrix_all_20260404" / "yolov8m-pose" / "yolov8m-pose_fast_fp16_ultra.engine",
    "yolov8l-pose": BUNDLE / "experiments" / "yolov8_pose" / "matrix_all_20260404" / "yolov8l-pose" / "yolov8l-pose_fast_fp16_ultra.engine",
    "yolo11s-pose": BUNDLE / "experiments" / "yolov8_pose" / "matrix_all_20260404" / "yolo11s-pose" / "yolo11s-pose_fast_fp16_ultra.engine",
    "yolo11m-pose": BUNDLE / "experiments" / "yolov8_pose" / "matrix_all_20260404" / "yolo11m-pose" / "yolo11m-pose_fast_fp16_ultra.engine",
    "yolo11l-pose": BUNDLE / "experiments" / "yolov8_pose" / "matrix_all_20260404" / "yolo11l-pose" / "yolo11l-pose_fast_fp16_ultra.engine",
    "yolo26s-pose": BUNDLE / "experiments" / "yolov8_pose" / "matrix_all_20260404" / "yolo26s-pose" / "yolo26s-pose_fast_fp16_ultra.engine",
    "yolo26m-pose": BUNDLE / "experiments" / "yolov8_pose" / "matrix_all_20260404" / "yolo26m-pose" / "yolo26m-pose_fast_fp16_ultra.engine",
    "yolo26l-pose": BUNDLE / "experiments" / "yolov8_pose" / "matrix_all_20260404" / "yolo26l-pose" / "yolo26l-pose_fast_fp16_ultra.engine",
}


def discover_phrases() -> List[Path]:
    phrases = []
    for path in sorted(ROOT.iterdir()):
        if not path.is_dir() or not path.name.startswith("2026"):
            continue
        if (path / f"{path.name}.avi").exists() and (path / f"{path.name}.txt").exists():
            phrases.append(path)
    return phrases


def timed_call(timings: Dict[str, float], name: str, fn, *args, **kwargs):
    start = time.perf_counter()
    result = fn(*args, **kwargs)
    timings[name] = time.perf_counter() - start
    return result


@contextmanager
def patched_timer(target: object, attr: str, timings: Dict[str, float], name: str) -> Iterator[None]:
    original = getattr(target, attr)

    def wrapped(*args, **kwargs):
        start = time.perf_counter()
        result = original(*args, **kwargs)
        timings[name] = timings.get(name, 0.0) + (time.perf_counter() - start)
        return result

    setattr(target, attr, wrapped)
    try:
        yield
    finally:
        setattr(target, attr, original)


def benchmark_phrase(
    phrase_dir: Path,
    model_name: str,
    model_path: Path,
    preloaded_model: pipeline.YOLO,
    pure_metrics: Dict[str, Any],
) -> Dict[str, Any]:
    phrase_name = phrase_dir.name
    output_dir = OUTPUT_ROOT / model_name / phrase_name
    output_dir.mkdir(parents=True, exist_ok=True)

    input_video = pipeline._find_input_video(phrase_dir)
    txt_path = pipeline._find_file(phrase_dir, "*.txt")

    timings: Dict[str, float] = {}
    overall_start = time.perf_counter()

    with ExitStack() as stack:
        stack.enter_context(patched_timer(pipeline, "_build_fisheye_frame_corrector", timings, "fisheye_corrector_build"))
        stack.enter_context(patched_timer(pipeline, "_locate_front_fencers_with_yolo_bootstrap", timings, "bootstrap_locator"))
        stack.enter_context(patched_timer(pipeline, "extract_tracks_with_jump_safe_tracker", timings, "jump_safe_tracking"))
        stack.enter_context(patched_timer(pipeline, "process_video_and_extract_data", timings, "extract_keypoint_series"))

        extraction = timed_call(
            timings,
            "rerun_extraction",
            pipeline.rerun_extraction,
            input_video,
            txt_path,
            output_dir,
            model_path,
            progress=False,
            yolo_conf=0.15,
            yolo_verbose=False,
            yolo_imgsz=512,
            yolo_half=False,
            bootstrap_frames=pipeline.BOOTSTRAP_FRAMES_DEFAULT,
            fisheye_backend="vpi-cuda",
            frame_prefetch=4,
            video_reader="opencv",
            write_corrected_video=False,
            write_yolo_overlay=False,
            write_bootstrap_debug=False,
            preloaded_model=preloaded_model,
        )

    left_x = extraction["left_x"]
    left_y = extraction["left_y"]
    right_x = extraction["right_x"]
    right_y = extraction["right_y"]
    norm_constant = extraction["norm_constant"]

    before_left = timed_call(timings, "before_anomalies_left", pipeline.detect_limb_anomalies, left_x, left_y, side_label="left")
    before_right = timed_call(timings, "before_anomalies_right", pipeline.detect_limb_anomalies, right_x, right_y, side_label="right")
    left_x_fixed, left_y_fixed, left_repair = timed_call(
        timings, "repair_left", pipeline.repair_limb_runs, left_x, left_y, before_left, side_label="left"
    )
    right_x_fixed, right_y_fixed, right_repair = timed_call(
        timings, "repair_right", pipeline.repair_limb_runs, right_x, right_y, before_right, side_label="right"
    )
    after_left = timed_call(timings, "after_anomalies_left", pipeline.detect_limb_anomalies, left_x_fixed, left_y_fixed, side_label="left")
    after_right = timed_call(timings, "after_anomalies_right", pipeline.detect_limb_anomalies, right_x_fixed, right_y_fixed, side_label="right")

    phrase = timed_call(timings, "parse_phrase_txt", pipeline.parse_txt_file, str(txt_path), video_path=str(input_video))
    raw_decision = timed_call(
        timings, "raw_referee_decision", pipeline.referee_decision, phrase, left_x, left_y, right_x, right_y, normalisation_constant=norm_constant
    )
    repaired_decision = timed_call(
        timings,
        "repaired_referee_decision",
        pipeline.referee_decision,
        phrase,
        left_x_fixed,
        left_y_fixed,
        right_x_fixed,
        right_y_fixed,
        normalisation_constant=norm_constant,
    )
    final_analysis_result_path = output_dir / "analysis_result.json"
    final_fps30_decision = timed_call(
        timings,
        "final_fps30_referee",
        pipeline.fps30_referee.run_referee_on_keypoints,
        txt_path=txt_path,
        video_path=input_video,
        left_x=left_x_fixed,
        left_y=left_y_fixed,
        right_x=right_x_fixed,
        right_y=right_y_fixed,
        normalisation_constant=norm_constant,
        decision_output_path=final_analysis_result_path,
        debug_output_path=None,
        debug_logging=False,
    )
    timings["job_total"] = time.perf_counter() - overall_start

    result = {
        "model": model_name,
        "model_path": str(model_path),
        "phrase": phrase_name,
        "frames_analyzed": len(extraction["tracks_per_frame"]),
        "effective_fps": len(extraction["tracks_per_frame"]) / timings["job_total"] if timings["job_total"] else None,
        "init_mode": extraction["init_mode"],
        "bootstrap_used": extraction["bootstrap_used"],
        "bootstrap_frame_index": extraction["bootstrap_frame_index"],
        "normalisation_constant": norm_constant,
        "before_left_anomalies": before_left["flagged_count"],
        "after_left_anomalies": after_left["flagged_count"],
        "before_right_anomalies": before_right["flagged_count"],
        "after_right_anomalies": after_right["flagged_count"],
        "reextracted_winner": raw_decision.get("winner"),
        "repaired_winner": repaired_decision.get("winner"),
        "final_fps30_winner": final_fps30_decision.get("winner"),
        "final_reason": final_fps30_decision.get("reason"),
        "timings": timings,
        "pure_metrics": pure_metrics,
    }
    (output_dir / "benchmark_summary.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def aggregate(results: List[Dict[str, Any]], pure_metrics_by_model: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    by_model: Dict[str, List[Dict[str, Any]]] = {}
    for item in results:
        by_model.setdefault(item["model"], []).append(item)

    model_summary = []
    for model_name, runs in sorted(by_model.items()):
        avg = lambda key: sum(run["timings"].get(key, 0.0) for run in runs) / len(runs)
        avg_total = avg("job_total")
        avg_frames = sum(run["frames_analyzed"] for run in runs) / len(runs)
        model_summary.append(
            {
                "model": model_name,
                "num_phrases": len(runs),
                "avg_job_total_seconds": avg_total,
                "avg_effective_fps": (avg_frames / avg_total) if avg_total else None,
                "avg_rerun_extraction_seconds": avg("rerun_extraction"),
                "avg_bootstrap_locator_seconds": avg("bootstrap_locator"),
                "avg_jump_safe_tracking_seconds": avg("jump_safe_tracking"),
                "avg_extract_keypoint_series_seconds": avg("extract_keypoint_series"),
                "avg_before_anomalies_left_seconds": avg("before_anomalies_left"),
                "avg_before_anomalies_right_seconds": avg("before_anomalies_right"),
                "avg_repair_left_seconds": avg("repair_left"),
                "avg_repair_right_seconds": avg("repair_right"),
                "avg_after_anomalies_left_seconds": avg("after_anomalies_left"),
                "avg_after_anomalies_right_seconds": avg("after_anomalies_right"),
                "avg_parse_phrase_txt_seconds": avg("parse_phrase_txt"),
                "avg_raw_referee_decision_seconds": avg("raw_referee_decision"),
                "avg_repaired_referee_decision_seconds": avg("repaired_referee_decision"),
                "avg_final_fps30_referee_seconds": avg("final_fps30_referee"),
                "mean_before_left_anomalies": sum(run["before_left_anomalies"] for run in runs) / len(runs),
                "mean_after_left_anomalies": sum(run["after_left_anomalies"] for run in runs) / len(runs),
                "mean_before_right_anomalies": sum(run["before_right_anomalies"] for run in runs) / len(runs),
                "mean_after_right_anomalies": sum(run["after_right_anomalies"] for run in runs) / len(runs),
                "pure_metrics": pure_metrics_by_model.get(model_name),
            }
        )

    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "phrases": [str(p) for p in discover_phrases()],
        "models": model_summary,
        "runs": results,
    }


def main() -> int:
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    pure_metrics_data = json.loads(MATRIX_SUMMARY_PATH.read_text(encoding="utf-8"))
    pure_metrics_by_model = {item["model"]: item for item in pure_metrics_data}
    phrases = discover_phrases()
    results: List[Dict[str, Any]] = []

    for model_name, model_path in MODEL_ENGINES.items():
        if not model_path.exists():
            raise FileNotFoundError(f"Missing engine for {model_name}: {model_path}")
        preload_start = time.perf_counter()
        model = pipeline.YOLO(str(model_path))
        preload_seconds = time.perf_counter() - preload_start
        print(f"MODEL {model_name} preload_seconds={preload_seconds:.3f}", flush=True)
        for phrase_dir in phrases:
            print(f"RUN {model_name} {phrase_dir.name}", flush=True)
            result = benchmark_phrase(
                phrase_dir=phrase_dir,
                model_name=model_name,
                model_path=model_path,
                preloaded_model=model,
                pure_metrics=pure_metrics_by_model.get(model_name, {}),
            )
            result["model_preload_seconds"] = preload_seconds
            results.append(result)
        del model
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()

    summary = aggregate(results, pure_metrics_by_model)
    summary_path = OUTPUT_ROOT / "model_sweep_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    csv_path = OUTPUT_ROOT / "model_sweep_runs.csv"
    rows: List[Dict[str, Any]] = []
    for run in results:
        row = {
            "model": run["model"],
            "phrase": run["phrase"],
            "frames_analyzed": run["frames_analyzed"],
            "effective_fps": run["effective_fps"],
            "job_total_seconds": run["timings"]["job_total"],
            "rerun_extraction_seconds": run["timings"]["rerun_extraction"],
            "bootstrap_locator_seconds": run["timings"].get("bootstrap_locator", 0.0),
            "jump_safe_tracking_seconds": run["timings"].get("jump_safe_tracking", 0.0),
            "extract_keypoint_series_seconds": run["timings"].get("extract_keypoint_series", 0.0),
            "repair_left_seconds": run["timings"]["repair_left"],
            "repair_right_seconds": run["timings"]["repair_right"],
            "final_fps30_referee_seconds": run["timings"]["final_fps30_referee"],
            "before_left_anomalies": run["before_left_anomalies"],
            "after_left_anomalies": run["after_left_anomalies"],
            "before_right_anomalies": run["before_right_anomalies"],
            "after_right_anomalies": run["after_right_anomalies"],
            "final_fps30_winner": run["final_fps30_winner"],
        }
        rows.append(row)
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    print(f"SUMMARY_PATH {summary_path}")
    print(f"CSV_PATH {csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
