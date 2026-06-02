#!/usr/bin/env python3
import argparse
import json
import time
from pathlib import Path

import cv2
from ultralytics import YOLO


def load_frames(video_path: Path, max_frames: int | None = None):
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"Could not open video: {video_path}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    frames = []
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            frames.append(frame)
            if max_frames is not None and len(frames) >= max_frames:
                break
    finally:
        cap.release()
    if not frames:
        raise RuntimeError(f"No frames decoded from {video_path}")
    return frames, fps, width, height


def run_pure_benchmark(model: YOLO, frames, imgsz: int, conf: float, device, half: bool, warmup: int):
    for frame in frames[:warmup]:
        model.predict(frame, imgsz=imgsz, conf=conf, device=device, half=half, verbose=False)

    infer_total = 0.0
    persons_per_frame = []
    keypoint_instances = []
    for frame in frames:
        t0 = time.perf_counter()
        results = model.predict(frame, imgsz=imgsz, conf=conf, device=device, half=half, verbose=False)
        infer_total += time.perf_counter() - t0
        r = results[0]
        num_people = 0 if r.boxes is None else len(r.boxes)
        num_keypoints = 0 if r.keypoints is None else len(r.keypoints.data)
        persons_per_frame.append(int(num_people))
        keypoint_instances.append(int(num_keypoints))

    frame_count = len(frames)
    return {
        "frames": frame_count,
        "mean_infer_ms": infer_total / frame_count * 1000.0,
        "fps_infer_only": frame_count / infer_total if infer_total > 0 else 0.0,
        "nonzero_detection_frames": int(sum(1 for v in persons_per_frame if v > 0)),
        "mean_people_per_frame": float(sum(persons_per_frame) / frame_count),
        "max_people_in_frame": int(max(persons_per_frame) if persons_per_frame else 0),
        "mean_keypoint_instances_per_frame": float(sum(keypoint_instances) / frame_count),
    }


def run_overlay_export(
    model: YOLO,
    frames,
    fps: float,
    width: int,
    height: int,
    imgsz: int,
    conf: float,
    device,
    half: bool,
    output_video: Path,
):
    output_video.parent.mkdir(parents=True, exist_ok=True)
    writer = cv2.VideoWriter(str(output_video), cv2.VideoWriter_fourcc(*"mp4v"), fps, (width, height))
    if not writer.isOpened():
        raise RuntimeError(f"Could not open output video for writing: {output_video}")

    infer_total = 0.0
    plot_total = 0.0
    people_counts = []
    try:
        for frame in frames:
            t0 = time.perf_counter()
            results = model.predict(frame, imgsz=imgsz, conf=conf, device=device, half=half, verbose=False)
            infer_total += time.perf_counter() - t0

            r = results[0]
            people_counts.append(0 if r.boxes is None else len(r.boxes))

            t1 = time.perf_counter()
            overlay = r.plot()
            writer.write(overlay)
            plot_total += time.perf_counter() - t1
    finally:
        writer.release()

    frame_count = len(frames)
    total = infer_total + plot_total
    return {
        "frames": frame_count,
        "mean_infer_ms": infer_total / frame_count * 1000.0,
        "mean_plot_write_ms": plot_total / frame_count * 1000.0,
        "mean_total_ms": total / frame_count * 1000.0,
        "fps_total": frame_count / total if total > 0 else 0.0,
        "nonzero_detection_frames": int(sum(1 for v in people_counts if v > 0)),
        "mean_people_per_frame": float(sum(people_counts) / frame_count),
        "max_people_in_frame": int(max(people_counts) if people_counts else 0),
    }


def main():
    parser = argparse.ArgumentParser(description="Benchmark YOLO pose model and export overlay video.")
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--overlay-video", type=Path, default=None)
    parser.add_argument("--imgsz", type=int, default=640)
    parser.add_argument("--conf", type=float, default=0.15)
    parser.add_argument("--device", default=0)
    parser.add_argument("--half", action="store_true")
    parser.add_argument("--warmup", type=int, default=10)
    parser.add_argument("--max-frames", type=int, default=None)
    args = parser.parse_args()

    frames, fps, width, height = load_frames(args.video, args.max_frames)
    model = YOLO(str(args.model))

    pure = run_pure_benchmark(
        model=model,
        frames=frames,
        imgsz=args.imgsz,
        conf=args.conf,
        device=args.device,
        half=args.half,
        warmup=args.warmup,
    )

    overlay = None
    if args.overlay_video is not None:
        overlay = run_overlay_export(
            model=model,
            frames=frames,
            fps=fps,
            width=width,
            height=height,
            imgsz=args.imgsz,
            conf=args.conf,
            device=args.device,
            half=args.half,
            output_video=args.overlay_video,
        )

    summary = {
        "video": str(args.video),
        "model": str(args.model),
        "frame_count": len(frames),
        "fps": fps,
        "imgsz": args.imgsz,
        "conf": args.conf,
        "half": bool(args.half),
        "pure_benchmark": pure,
        "overlay_export": overlay,
        "overlay_video": str(args.overlay_video) if args.overlay_video else None,
    }
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
