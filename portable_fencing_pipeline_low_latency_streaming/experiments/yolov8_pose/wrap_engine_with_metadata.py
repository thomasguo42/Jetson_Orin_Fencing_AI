#!/usr/bin/env python3
import argparse
import json
import struct
from pathlib import Path

from ultralytics import YOLO


def main():
    parser = argparse.ArgumentParser(description="Wrap a bare TensorRT engine with Ultralytics metadata.")
    parser.add_argument("--pt-model", type=Path, required=True)
    parser.add_argument("--engine", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--imgsz", type=int, required=True)
    parser.add_argument("--batch", type=int, default=1)
    parser.add_argument("--dynamic", action="store_true")
    parser.add_argument("--nms", action="store_true")
    parser.add_argument("--end2end", action="store_true")
    args = parser.parse_args()

    model = YOLO(str(args.pt_model))
    stride = int(model.model.stride.max()) if hasattr(model.model, "stride") else 32
    metadata = {
        "description": "Ultralytics YOLO TensorRT engine",
        "author": "Ultralytics",
        "date": "",
        "version": "",
        "license": "AGPL-3.0 License",
        "docs": "https://docs.ultralytics.com",
        "stride": stride,
        "task": model.task,
        "batch": args.batch,
        "imgsz": [args.imgsz, args.imgsz],
        "names": model.model.names,
        "kpt_shape": getattr(model.model.model[-1], "kpt_shape", None),
        "args": {
            "dynamic": bool(args.dynamic),
            "nms": bool(args.nms),
        },
        "channels": 3,
        "end2end": bool(args.end2end),
    }

    raw_engine = args.engine.read_bytes()
    meta_bytes = json.dumps(metadata).encode("utf-8")
    payload = struct.pack("<I", len(meta_bytes)) + meta_bytes + raw_engine
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(payload)
    print(args.output)


if __name__ == "__main__":
    main()
