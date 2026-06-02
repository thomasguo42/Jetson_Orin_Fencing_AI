"""PisteLink NDJSON-over-UDS helpers.

The partner protocol uses one JSON object per line over a Unix domain stream
socket.  This module keeps framing, message envelopes, and timestamp helpers in
one small place so the service code can focus on match state.
"""

from __future__ import annotations

import json
import socket
import time
from typing import Any, Dict, Optional


PROTOCOL_VERSION = 1
MAX_LINE_BYTES = 64 * 1024


class ProtocolError(RuntimeError):
    """Raised when a peer sends malformed protocol data."""


def epoch_ms() -> int:
    return time.time_ns() // 1_000_000


def monotonic_ns() -> int:
    return time.monotonic_ns()


def make_message(
    msg_type: str,
    msg_id: int,
    payload: Optional[Dict[str, Any]] = None,
    match_id: Optional[str] = None,
) -> Dict[str, Any]:
    message: Dict[str, Any] = {
        "v": PROTOCOL_VERSION,
        "type": msg_type,
        "id": msg_id,
        "ts": epoch_ms(),
        "ts_mono_ns": monotonic_ns(),
    }
    if match_id is not None:
        message["match_id"] = match_id
    if payload is not None:
        message["payload"] = payload
    return message


def validate_message(message: Dict[str, Any]) -> None:
    if not isinstance(message, dict):
        raise ProtocolError("message is not a JSON object")
    version = message.get("v", message.get("protocol_v"))
    if version != PROTOCOL_VERSION:
        raise ProtocolError(f"unsupported protocol version={version!r}")
    message["v"] = version
    if "ts_mono_ns" not in message and "mono_ns" in message:
        message["ts_mono_ns"] = message["mono_ns"]
    if not isinstance(message.get("type"), str):
        raise ProtocolError("missing string type")
    if not isinstance(message.get("id"), int):
        raise ProtocolError("missing integer id")
    if "payload" in message and not isinstance(message["payload"], dict):
        raise ProtocolError("payload must be a JSON object")


class NdjsonSocket:
    """Buffered NDJSON reader/writer around a connected socket."""

    def __init__(self, sock: socket.socket):
        self.sock = sock
        self._buffer = bytearray()
        self._discarding_oversized = False

    def read_message(self, timeout: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """Read one message.

        Returns None on timeout.  Raises EOFError when the peer closes the
        connection and ProtocolError for malformed or oversized frames.
        """

        self.sock.settimeout(timeout)
        while True:
            newline = self._buffer.find(b"\n")
            if self._discarding_oversized and newline >= 0:
                del self._buffer[: newline + 1]
                self._discarding_oversized = False
                raise ProtocolError("discarded oversized NDJSON frame")
            if self._discarding_oversized:
                self._buffer.clear()

            if newline >= 0:
                raw = bytes(self._buffer[:newline])
                del self._buffer[: newline + 1]
                if not raw.strip():
                    continue
                if len(raw) > MAX_LINE_BYTES:
                    raise ProtocolError("NDJSON frame exceeds 64 KiB")
                try:
                    message = json.loads(raw.decode("utf-8"))
                except json.JSONDecodeError as exc:
                    raise ProtocolError(f"invalid JSON frame: {exc}") from exc
                validate_message(message)
                return message

            try:
                chunk = self.sock.recv(4096)
            except socket.timeout:
                return None

            if not chunk:
                raise EOFError("peer closed connection")

            self._buffer.extend(chunk)
            if len(self._buffer) > MAX_LINE_BYTES:
                self._buffer.clear()
                self._discarding_oversized = True
                raise ProtocolError("NDJSON frame exceeds 64 KiB")

    def write_message(self, message: Dict[str, Any]) -> None:
        validate_message(message)
        raw = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(raw) > MAX_LINE_BYTES:
            raise ProtocolError("outbound NDJSON frame exceeds 64 KiB")
        self.sock.sendall(raw + b"\n")
