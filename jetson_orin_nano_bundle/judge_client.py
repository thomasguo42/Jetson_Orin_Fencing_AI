#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import requests


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Submit one fencing phrase to the Pi judging service.")
    parser.add_argument("--video", type=Path, required=True, help="Path to the phrase video on the client machine.")
    parser.add_argument("--txt", type=Path, required=True, help="Path to the phrase TXT on the client machine.")
    parser.add_argument("--server", default="http://192.168.50.2:8765/judge", help="Pi judge endpoint URL.")
    parser.add_argument("--request-id", default=None, help="Optional caller-supplied request ID.")
    parser.add_argument("--timeout", type=float, default=120.0, help="HTTP timeout in seconds.")
    return parser.parse_args()


def response_payload(response: requests.Response) -> object:
    try:
        return response.json()
    except Exception:
        return {"status_code": response.status_code, "body": response.text}


def submit_phrase(
    video_path: Path,
    txt_path: Path,
    server: str = "http://192.168.50.2:8765/judge",
    request_id: str | None = None,
    timeout: float = 120.0,
) -> requests.Response:
    with video_path.open("rb") as video_handle, txt_path.open("rb") as txt_handle:
        return requests.post(
            server,
            data={"request_id": request_id} if request_id else {},
            files={
                "video": (video_path.name, video_handle, "application/octet-stream"),
                "txt": (txt_path.name, txt_handle, "text/plain"),
            },
            timeout=timeout,
        )


def main() -> int:
    args = parse_args()
    video_path = args.video.expanduser().resolve()
    txt_path = args.txt.expanduser().resolve()
    if not video_path.exists():
        raise SystemExit(f"Video not found: {video_path}")
    if not txt_path.exists():
        raise SystemExit(f"TXT not found: {txt_path}")

    response = submit_phrase(
        video_path=video_path,
        txt_path=txt_path,
        server=args.server,
        request_id=args.request_id,
        timeout=args.timeout,
    )

    print(json.dumps(response_payload(response), indent=2))
    return 0 if response.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
