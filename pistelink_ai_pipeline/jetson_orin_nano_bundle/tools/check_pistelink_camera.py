#!/usr/bin/env python3
"""Record a short clip through the PisteLink camera wrapper."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pistelink_camera_recorder import PisteLinkCameraRecorder, transcode_avi_to_mp4


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify camera recording and frame timestamps")
    parser.add_argument("--seconds", type=float, default=2.0)
    parser.add_argument("--output-dir", default="/tmp/pistelink_camera_check")
    parser.add_argument("--transcode", action="store_true")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    avi_path = output_dir / "camera_check.avi"
    mp4_path = output_dir / "camera_check.mp4"
    frame_ts_path = output_dir / "frame_timestamps.jsonl"

    camera = PisteLinkCameraRecorder()
    try:
        camera.start(avi_path, streaming_manager=None)
        first_frame = camera.wait_for_first_frame(timeout_s=3.0)
        time.sleep(max(0.0, args.seconds))
        camera.stop()
        frames = camera.write_frame_timestamps(frame_ts_path)

        payload = {
            "ok": bool(frames),
            "width": camera.width,
            "height": camera.height,
            "fps_nominal": camera.fps,
            "frame_count": len(frames),
            "avi_path": str(avi_path),
            "frame_timestamps_path": str(frame_ts_path),
            "first_frame": None if first_frame is None else first_frame.__dict__,
            "last_frame": None if not frames else frames[-1].__dict__,
        }
        if len(frames) >= 2:
            elapsed_s = (frames[-1].ts - frames[0].ts) / 1000.0
            payload["measured_fps_epoch_ms"] = (len(frames) - 1) / elapsed_s if elapsed_s > 0 else None
        if args.transcode:
            payload["mp4_path"] = str(transcode_avi_to_mp4(avi_path, mp4_path))
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if frames else 1
    finally:
        camera.release()


if __name__ == "__main__":
    raise SystemExit(main())
