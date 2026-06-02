"""CLI helper to send fencing phrase data to the remote referee service."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict

import requests


def _pretty_print(data: Any) -> None:
    try:
        print(json.dumps(data, indent=2, sort_keys=True))
    except (TypeError, ValueError):
        print(data)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Send phrase data to the AI referee service")
    parser.add_argument("server", help="Base URL of the referee service, e.g. http://server:8000")
    parser.add_argument("video", nargs="?", help="Path to the .avi file to upload")
    parser.add_argument("signal", nargs="?", help="Path to the .txt electric signal file")
    parser.add_argument(
        "--include-keypoints",
        action="store_true",
        help="Request raw keypoint data in the response (large payload)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=300.0,
        help="HTTP request timeout in seconds (default: 300s)",
    )
    parser.add_argument(
        "--health",
        action="store_true",
        help="Only query the /health endpoint and exit",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    base_url = args.server.rstrip("/")

    if args.health:
        try:
            response = requests.get(f"{base_url}/health", timeout=args.timeout)
            response.raise_for_status()
            _pretty_print(response.json())
        except requests.RequestException as exc:  # pragma: no cover - CLI diagnostics
            print(f"Health check failed: {exc}", file=sys.stderr)
            sys.exit(1)
        return

    if not args.video or not args.signal:
        print("Video and signal paths are required unless --health is used", file=sys.stderr)
        sys.exit(2)

    video_path = Path(args.video)
    signal_path = Path(args.signal)

    if not video_path.is_file():
        print(f"Video file not found: {video_path}", file=sys.stderr)
        sys.exit(2)
    if not signal_path.is_file():
        print(f"Signal file not found: {signal_path}", file=sys.stderr)
        sys.exit(2)

    files: Dict[str, Any] = {
        "video": (video_path.name, video_path.open("rb"), "video/x-msvideo"),
        "signal": (signal_path.name, signal_path.open("rb"), "text/plain"),
    }

    data: Dict[str, str] = {}
    if args.include_keypoints:
        data["include_keypoints"] = "true"

    try:
        response = requests.post(
            f"{base_url}/analyze",
            files=files,
            data=data,
            timeout=args.timeout,
        )
        response.raise_for_status()
    except requests.HTTPError as exc:
        resp = exc.response
        if resp is None and "response" in locals():
            resp = response
        if resp is not None and resp.headers.get("content-type", "").startswith("application/json"):
            _pretty_print(resp.json())
        elif resp is not None:
            print(resp.text, file=sys.stderr)
        else:
            print(str(exc), file=sys.stderr)
        sys.exit(resp.status_code if resp is not None else 1)
    except requests.RequestException as exc:
        print(f"Request failed: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        for file_tuple in files.values():
            file_tuple[1].close()

    if response.headers.get("content-type", "").startswith("application/json"):
        _pretty_print(response.json())
    else:
        print(response.text)


if __name__ == "__main__":
    main()
