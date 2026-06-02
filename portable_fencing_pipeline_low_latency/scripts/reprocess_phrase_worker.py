#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Dict, Optional

import reprocess_phrase_limb_interp_jumpsafe_experimental as pipeline


JSON_PREFIX = "WORKER_JSON "


def _emit(payload: Dict[str, Any]) -> None:
    print(f"{JSON_PREFIX}{json.dumps(payload, separators=(',', ':'))}", flush=True)


def _coerce_job_args(base_args: argparse.Namespace, overrides: Dict[str, Any]) -> argparse.Namespace:
    merged = copy.deepcopy(vars(base_args))
    merged.update(overrides)

    for key in ("phrase_dir", "model_path", "output_dir"):
        value = merged.get(key)
        if value is not None and not isinstance(value, Path):
            merged[key] = Path(value)

    phrase_dir = merged.get("phrase_dir")
    if phrase_dir is None:
        raise ValueError("Job is missing required field 'phrase_dir'")

    return argparse.Namespace(**merged)


def _load_or_get_model(
    model_cache: Dict[str, pipeline.YOLO],
    model_path: Path,
) -> pipeline.YOLO:
    resolved = str(model_path.resolve())
    model = model_cache.get(resolved)
    if model is None:
        model = pipeline.YOLO(resolved)
        model_cache[resolved] = model
    return model


def build_worker_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Persistent worker for low-latency phrase processing.",
        parents=[pipeline.build_arg_parser(require_phrase_dir=False, add_help=False)],
    )
    parser.add_argument(
        "--preload-model",
        action="store_true",
        help="Preload the configured model at worker start.",
    )
    return parser


def main() -> int:
    parser = build_worker_parser()
    args = parser.parse_args()

    model_cache: Dict[str, pipeline.YOLO] = {}
    if args.preload_model and not args.repair_only:
        _load_or_get_model(model_cache, args.model_path.resolve())

    _emit(
        {
            "status": "ready",
            "pid": os.getpid(),
            "model_path": str(args.model_path.resolve()),
            "repair_only": bool(args.repair_only),
        }
    )

    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue

        try:
            payload = json.loads(line)
        except Exception as exc:
            _emit({"status": "error", "error": f"Invalid JSON: {exc}"})
            continue

        command = payload.get("command")
        if command == "quit":
            _emit({"status": "bye"})
            break
        if command == "ping":
            _emit({"status": "ok"})
            continue

        start = time.perf_counter()
        try:
            job_args = _coerce_job_args(args, payload)
            preloaded_model: Optional[pipeline.YOLO] = None
            if not job_args.repair_only:
                preloaded_model = _load_or_get_model(model_cache, job_args.model_path.resolve())

            job_result = pipeline.run_phrase_job(job_args, preloaded_model=preloaded_model)
            elapsed = time.perf_counter() - start
            _emit(
                {
                    "status": "completed",
                    "phrase_dir": str(job_args.phrase_dir.resolve()),
                    "elapsed_seconds": elapsed,
                    "summary": job_result["summary"],
                    "output_dir": job_result["output_dir"],
                    "result_path": job_result["result_path"],
                    "final_analysis_result_path": job_result["final_analysis_result_path"],
                }
            )
        except Exception as exc:
            _emit(
                {
                    "status": "error",
                    "phrase_dir": str(payload.get("phrase_dir")) if payload.get("phrase_dir") else None,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "traceback": traceback.format_exc(),
                }
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
