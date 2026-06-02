#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import cv2

BUNDLE_ROOT = Path(__file__).resolve().parent.parent
if str(BUNDLE_ROOT) not in sys.path:
    sys.path.append(str(BUNDLE_ROOT))

from scripts import live_stream_service
from scripts import reprocess_phrase_limb_interp_jumpsafe_experimental as pipeline


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Replay a finished phrase through the live streaming analyzer.")
    parser.add_argument("--phrase-dir", type=Path, required=True)
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
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()
    phrase_dir = args.phrase_dir.resolve()
    input_video = pipeline._find_input_video(phrase_dir)
    txt_path = pipeline._find_file(phrase_dir, "*.txt")
    output_dir = args.output_dir.resolve() if args.output_dir else live_stream_service._default_output_dir(phrase_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    cap = cv2.VideoCapture(str(input_video))
    if not cap.isOpened():
        raise FileNotFoundError(f"Cannot open video: {input_video}")

    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    session = live_stream_service.LiveTrackingSession(
        width=width,
        height=height,
        model_path=args.model_path.resolve(),
        yolo_conf=args.yolo_conf,
        yolo_imgsz=args.yolo_imgsz,
        yolo_half=args.yolo_half,
        yolo_verbose=args.yolo_verbose,
        bootstrap_frames=args.bootstrap_frames,
        fisheye_backend=args.fisheye_backend,
    )

    frame_idx = 0
    try:
        while True:
            ok, frame = cap.read()
            if not ok or frame is None:
                break
            session.push_frame(frame, frame_idx)
            frame_idx += 1
    finally:
        cap.release()

    job_result = session.finalize(
        phrase_dir=phrase_dir,
        output_dir=output_dir,
        txt_path=txt_path,
        video_path=input_video,
    )
    payload = live_stream_service._build_client_payload(job_result, processing_mode="replay_live_streaming")
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
