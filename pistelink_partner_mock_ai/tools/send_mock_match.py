#!/usr/bin/env python3
"""Send one synthetic backend match to the mock AI service."""

from __future__ import annotations

import argparse
import json
import socket
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional


PROTOCOL_VERSION = 1


def epoch_ms() -> int:
    return time.time_ns() // 1_000_000


def monotonic_ns() -> int:
    return time.monotonic_ns()


class Client:
    def __init__(self, socket_path: Path, timeout_s: float):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(str(socket_path))
        self.sock.settimeout(timeout_s)
        self.next_id = 0
        self.buffer = bytearray()

    def send(self, msg_type: str, payload: Optional[Dict[str, Any]] = None, match_id: Optional[str] = None) -> None:
        message: Dict[str, Any] = {
            "v": PROTOCOL_VERSION,
            "type": msg_type,
            "id": self.next_id,
            "ts": epoch_ms(),
            "ts_mono_ns": monotonic_ns(),
        }
        self.next_id += 1
        if match_id is not None:
            message["match_id"] = match_id
        if payload is not None:
            message["payload"] = payload
        self.sock.sendall(json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n")
        print("SEND", json.dumps(message, ensure_ascii=False))

    def recv(self) -> Dict[str, Any]:
        while True:
            newline = self.buffer.find(b"\n")
            if newline >= 0:
                raw = bytes(self.buffer[:newline])
                del self.buffer[: newline + 1]
                if raw.strip():
                    message = json.loads(raw.decode("utf-8"))
                    print("RECV", json.dumps(message, ensure_ascii=False))
                    return message
            chunk = self.sock.recv(4096)
            if not chunk:
                raise EOFError("mock AI service closed the socket")
            self.buffer.extend(chunk)

    def close(self) -> None:
        self.sock.close()


def wait_for(client: Client, wanted_type: str, match_id: Optional[str] = None, timeout_s: float = 20.0) -> Dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        message = client.recv()
        if message.get("type") == "ping":
            client.send("pong", {"ref_id": message.get("id")}, match_id=message.get("match_id"))
            continue
        if message.get("type") == wanted_type and (match_id is None or message.get("match_id") == match_id):
            return message
        if message.get("type") == "camera_error":
            return message
    raise TimeoutError(f"timed out waiting for {wanted_type}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Send one synthetic match to the PisteLink mock AI service")
    parser.add_argument("--socket-path", default="/tmp/pistelink_mock/ai.sock")
    parser.add_argument("--match-id", default=f"mock_client_{uuid.uuid4().hex[:8]}")
    parser.add_argument("--weapon", type=int, default=3)
    parser.add_argument("--winner", choices=["A", "B", "double", "none"], default="A")
    parser.add_argument("--cancel-stage", choices=["none", "ready", "hit"], default="none")
    parser.add_argument("--timeout", type=float, default=20.0)
    args = parser.parse_args()

    client = Client(Path(args.socket_path), timeout_s=args.timeout)
    try:
        match_id = args.match_id
        client.send(
            "hello",
            {"role": "backend", "app": "send_mock_match", "version": "0.1.0", "protocol_v": 1},
        )
        wait_for(client, "hello_ack", timeout_s=args.timeout)

        client.send(
            "match_pre_start",
            {"weapon": args.weapon, "sensor": 0, "side_map": {"A": "left", "B": "right"}},
            match_id=match_id,
        )
        ready = wait_for(client, "camera_ready", match_id=match_id, timeout_s=args.timeout)
        if ready.get("type") == "camera_error":
            return 2
        if args.cancel_stage == "ready":
            client.send("match_cancel", {"reason": "test_cancel_after_ready"}, match_id=match_id)
            time.sleep(0.2)
            return 0

        client.send("match_begin_ack", {"begin_ts": epoch_ms()}, match_id=match_id)
        time.sleep(0.05)
        client.send("voice_end", {"voice_end_ts": epoch_ms()}, match_id=match_id)
        time.sleep(0.1)

        if args.winner in {"A", "double"}:
            client.send(
                "signal",
                {"source": "hit", "fight": 8, "signal_ts": epoch_ms(), "signal_mono_ns": monotonic_ns(), "terminal": False},
                match_id=match_id,
            )
        if args.winner in {"B", "double"}:
            client.send(
                "signal",
                {"source": "hit", "fight": 9, "signal_ts": epoch_ms(), "signal_mono_ns": monotonic_ns(), "terminal": False},
                match_id=match_id,
            )
        if args.cancel_stage == "hit":
            client.send("match_cancel", {"reason": "test_cancel_after_hit"}, match_id=match_id)
            time.sleep(0.2)
            return 0

        time.sleep(0.1)
        client.send(
            "signal",
            {
                "source": "light",
                "signal_ts": epoch_ms(),
                "signal_mono_ns": monotonic_ns(),
                "terminal": True,
                "final_lights": {
                    "A": args.winner in {"A", "double"},
                    "B": args.winner in {"B", "double"},
                },
            },
            match_id=match_id,
        )
        try:
            result = wait_for(client, "match_result", match_id=match_id, timeout_s=args.timeout)
        except TimeoutError as exc:
            print(f"TIMEOUT {exc}")
            return 4
        return 0 if result.get("type") == "match_result" else 3
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
