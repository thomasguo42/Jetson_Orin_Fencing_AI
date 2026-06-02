#!/usr/bin/env python3
"""Small PisteLink backend simulator for dry-run/service checks."""

from __future__ import annotations

import argparse
import json
import socket
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional


PROTOCOL_VERSION = 1


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def now_mono_ns() -> int:
    return time.monotonic_ns()


class Client:
    def __init__(self, socket_path: Path, timeout_s: float):
        self.socket_path = socket_path
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(str(socket_path))
        self.sock.settimeout(timeout_s)
        self.next_id = 1
        self.buffer = bytearray()

    def send(self, msg_type: str, payload: Optional[Dict[str, Any]] = None, match_id: Optional[str] = None) -> None:
        message: Dict[str, Any] = {
            "v": PROTOCOL_VERSION,
            "type": msg_type,
            "id": self.next_id,
            "ts": now_ms(),
            "ts_mono_ns": now_mono_ns(),
        }
        self.next_id += 1
        if match_id is not None:
            message["match_id"] = match_id
        if payload is not None:
            message["payload"] = payload
        self.sock.sendall(json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n")

    def recv(self) -> Dict[str, Any]:
        while True:
            newline = self.buffer.find(b"\n")
            if newline >= 0:
                raw = bytes(self.buffer[:newline])
                del self.buffer[: newline + 1]
                if raw.strip():
                    return json.loads(raw.decode("utf-8"))
            chunk = self.sock.recv(4096)
            if not chunk:
                raise EOFError("AI service closed the socket")
            self.buffer.extend(chunk)

    def close(self) -> None:
        self.sock.close()


def wait_for(client: Client, wanted_type: str, match_id: Optional[str] = None) -> Dict[str, Any]:
    while True:
        message = client.recv()
        print(json.dumps(message, ensure_ascii=False, indent=2))
        if message.get("type") in {"camera_error", "error"}:
            raise RuntimeError(f"AI service returned {message.get('type')}: {message.get('payload')}")
        if message.get("type") == "ping":
            client.send("pong", {"ref_id": message.get("id")}, match_id=message.get("match_id"))
            continue
        if message.get("type") == wanted_type and (match_id is None or message.get("match_id") == match_id):
            return message


def main() -> int:
    parser = argparse.ArgumentParser(description="Simulate the PisteLink backend for one phrase")
    parser.add_argument("--socket-path", default="/tmp/pistelink/ai.sock")
    parser.add_argument("--match-root", default="/tmp/pistelink/matches")
    parser.add_argument("--match-id", default=f"sim_{uuid.uuid4().hex[:8]}")
    parser.add_argument("--winner", choices=["A", "B", "double", "none"], default="A")
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args()

    match_id = args.match_id
    match_dir = Path(args.match_root) / match_id
    match_dir.mkdir(parents=True, exist_ok=True)

    client = Client(Path(args.socket_path), timeout_s=args.timeout)
    try:
        client.send("hello", {"role": "backend", "app": "simulate_pistelink_backend", "version": "0.1.0", "protocol_v": 1})
        wait_for(client, "hello_ack")

        client.send(
            "match_pre_start",
            {
                "match_id": match_id,
                "match_dir": str(match_dir),
                "weapon": 3,
                "sensor": 0,
                "side_map": {"A": "left", "B": "right"},
            },
            match_id=match_id,
        )
        wait_for(client, "camera_ready", match_id=match_id)

        client.send("match_begin_ack", {"begin_ts": now_ms()}, match_id=match_id)
        time.sleep(0.1)
        client.send("voice_end", {"voice_end_ts": now_ms()}, match_id=match_id)
        time.sleep(0.2)

        if args.winner in {"A", "double"}:
            client.send("signal", {"source": "hit", "fight": 8, "signal_ts": now_ms(), "terminal": False}, match_id=match_id)
        if args.winner in {"B", "double"}:
            client.send("signal", {"source": "hit", "fight": 9, "signal_ts": now_ms(), "terminal": False}, match_id=match_id)
        time.sleep(0.15)
        client.send(
            "signal",
            {
                "source": "light",
                "signal_ts": now_ms(),
                "terminal": True,
                "final_lights": {
                    "A": args.winner in {"A", "double"},
                    "B": args.winner in {"B", "double"},
                },
            },
            match_id=match_id,
        )
        wait_for(client, "match_result", match_id=match_id)
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
