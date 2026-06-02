#!/usr/bin/env python3
"""Standalone PisteLink v1.1 mock AI service.

This program is intentionally a black-box protocol mock.  It implements the
public PisteLink <-> AI Unix socket contract, creates synthetic match artifacts,
and avoids importing or copying any production camera, model, analyzer, or
judging code.
"""

from __future__ import annotations

import argparse
import base64
import bisect
import json
import os
import pwd
import grp
import shutil
import socket
import stat
import subprocess
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


PROTOCOL_VERSION = 1
MAX_LINE_BYTES = 64 * 1024
DEFAULT_SOCKET_PATH = "/run/pistelink/ai.sock"
DEFAULT_MATCH_ROOT = "/var/lib/pistelink/matches"

DEFAULT_HEARTBEAT_INTERVAL_S = 2.0
DEFAULT_PEER_TIMEOUT_S = 6.0


TINY_BLACK_MP4_BASE64 = (
    "AAAAIGZ0eXBtcDQyAAAAAG1wNDJtcDQxaXNvbWlzbzIAAAPpbW9vdgAAAGxtdmhkAAAAAOY+7JrmPuya"
    "AAALuAAAC7gAAQAAAQAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAABAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAgAAAzh0cmFrAAAAXHRraGQAAAAH5j7smuY+7JoAAAABAAAAAAAAC7gAAA"
    "AAAAAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAABAAAAAAUAAAAC0AAAAAAAkZW"
    "R0cwAAABxlbHN0AAAAAAAAAAEAAAu4AAAAAAABAAAAAAJXbWRpYQAAACBtZGhkAAAAAOY+7JrmPuyaAAA"
    "LuAAAC7hVxAAAAAAALWhkbHIAAAAAAAAAAHZpZGUAAAAAAAAAAAAAAABWaWRlb0hhbmRsZXIAAAACAm1p"
    "bmYAAAAUdm1oZAAAAAEAAAAAAAAAAAAAACRkaW5mAAAAHGRyZWYAAAAAAAAAAQAAAAx1cmwgAAAAAQAAAc"
    "JzdGJsAAAA0nN0c2QAAAAAAAAAAQAAAMJhdmMxAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAAAAUAAtABIAAAA"
    "SAAAAAAAAAABAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAGP//AAAANWF2Y0MB9AAN/+EAHm"
    "f0AA2Q2WgUGfjcBagwMDIAAAMAAgAAAwB5HihVQAEABGjOMZIAAAAUYnRydAAAAAAAAgAAAAB7uAAAAB"
    "Njb2xybmNseAAGAAYABgAAAAAQcGFzcAAAAAEAAAABAAAAGHN0dHMAAAAAAAAAAQAAAB4AAABkAAAAFHN0"
    "c3MAAAAAAAAAAQAAAAEAAAAcc3RzYwAAAAAAAAABAAAAAQAAAB4AAAABAAAAjHN0c3oAAAAAAAAAAAAAAB"
    "4AAAPwAAAAJwAAACgAAAAoAAABkgAAACkAAAWYAAAAKQAAACgAAAApAAAAKQAAACkAAAApAAAAKQAAAC"
    "kAAAAqAAAAKgAAACoAAAAqAAAAKgAAACoAAAAqAAAAKgAAACoAAAAqAAAAKgAAACoAAAAqAAAAKgAAAC"
    "oAAAAUc3RjbwAAAAAAAAABAAAEEQAAAFl1ZHRhAAAAUW1ldGEAAAAAAAAAIWhkbHIAAAAAbWhscm1kaXI"
    "AAAAAAAAAAAAAAAAAAAAAJGlsc3QAAAAcqXRvbwAAABRkYXRhAAAAAQAAAAB4MjY0AAAAPXVkdGEAAAA1"
    "bWV0YQAAAAAAAAAhaGRscgAAAABtaGxybWRpcgAAAAAAAAAAAAAAAAAAAAAIaWxzdAAAD39tZGF0AAAAA"
    "gkQAAAAHmf0AA2Q2WgUGfjcBagwMDIAAAMAAgAAAwB5HihVQAAAAARozjGSAAACrgYF//+q3EXpvebZS"
    "LeWLNgg2SPu73gyNjQgLSBjb3JlIDE2MyByMzA2MCA1ZGI2YWE2IC0gSC4yNjQvTVBFRy00IEFWQyBjb2"
    "RlYyAtIENvcHlsZWZ0IDIwMDMtMjAyMSAtIGh0dHA6Ly93d3cudmlkZW9sYW4ub3JnL3gyNjQuaHRtbCAt"
    "IG9wdGlvbnM6IGNhYmFjPTAgcmVmPTEgZGVibG9jaz0wOjA6MCBhbmFseXNlPTA6MCBtZT1kaWEgc3Vi"
    "bWU9MCBwc3k9MSBwc3lfcmQ9MS4wMDowLjAwIG1peGVkX3JlZj0wIG1lX3JhbmdlPTE2IGNocm9tYV9t"
    "ZT0xIHRyZWxsaXM9MCA4eDhkY3Q9MCBjcW09MCBkZWFkem9uZT0yMSwxMSBmYXN0X3Bza2lwPTEgY2hy"
    "b21hX3FwX29mZnNldD02IHRocmVhZHM9MyBsb29rYWhlYWRfdGhyZWFkcz0zIHNsaWNlZF90aHJlYWRz"
    "PTEgc2xpY2VzPTMgbnI9MCBkZWNpbWF0ZT0xIGludGVybGFjZWQ9MCBibHVyYXlfY29tcGF0PTAgY29u"
    "c3RyYWluZWRfaW50cmE9MCBiZnJhbWVzPTAgd2VpZ2h0cD0wIGtleWludD0zMCBrZXlpbnRfbWluPTMg"
    "c2NlbmVjdXQ9MCBpbnRyYV9yZWZyZXNoPTAgcmNfbG9va2FoZWFkPTAgcmM9Y2JyIG1idHJlZT0wIGJp"
    "dHJhdGU9MTI4IHJhdGV0b2w9MS4wIHFjb21wPTAuNjAgcXBtaW49MCBxcG1heD04MSBxcHN0ZXA9NCB2"
    "YnZfbWF4cmF0ZT0xMjggdmJ2X2J1ZnNpemU9NzYgbmFsX2hyZD1ub25lIGZpbGxlcj0wIGlwX3JhdGlv"
    "PTEuNDAgYXE9MACAAAAAVWWIhBaJFAAEd3kvk8nk8nk8nk8nk8nk8nk8nk8nk8nq+vXr169evXr169ev"
    "Xr169evXr169evXr169evXr169evXr169evXr169evXr169evXr16+AAAABWZQKIiEFokUAAR3eS+Tye"
    "TyeTyeTyeTyeTyeTyeTyeTyer69evXr169evXr169evXr169evXr169evXr169evXr169evXr169evXr"
    "169evXr4AAABXZQFCIhBaJFAAEd3kvk8nk8nk8nk8nk8nk8nk8nk8nk8nq+vXr169evXr169evXr169e"
    "vXr169evXr169evXr169evXr169evXr169evXr169evXr16+AAAAAAgkwAAAABkGaIOgKMAAAAAdBAom"
    "iDoCjAAAACEEBQmiDoCjAAAAAAgkwAAAABkGaQHoCjAAAAAhBAomkB6AowAAAAAhBAUJpAegKMAAAAAI"
    "JMAAAAAZBmmAygKMAAAAIQQKJpgMoCjAAAAAIQQFCaYDKAowAAAACCTAAAAB9QZqAEq4CA/////////"
    "///////nEVWfVZ9Vn1WfVZ9Vn1WfVZ9Vn1WfVZ9Vn1WfVZ9Vn1WfVZ9Vn1WfVZ/n+f5/n+f5/n+f5/"
    "n+f5/n+f5/n+f5/n+d1WfVZ9Vn1WfVZ9Vn1WfVZ9Vn1WfVZ9Vn1WfVZ9Vn1WfVZ9Vn1WfVYAAAB/QQ"
    "KJqAEq4CB////////////////nEVWfVZ9Vn1WfVZ9Vn1WfVZ9Vn1WfVZ9Vn1WfVZ9Vn1WfVZ9Vn1W"
    "fVZ/n+f5/n+f5/n+f5/n+f5/n+f5/n+f5/n+d1WfVZ9Vn1WfVZ9Vn1WfVZ9Vn1WfVZ9Vn1WfVZ9Vn"
    "1WfVZ9Vn1WfVYAAAAIRBAUJqAEq4CB////////////////nNFxefFxefFxefFxefFxefFxefFxefFxe"
    "fFxefFxefFxefFxefFxefFxefFxefFxefFxefFxefFxefFxef5/n+f5/n+f5/n+f5/n+f5/n+f5/n+"
    "f5/n+f5/n+f5/n+f5/n+f5/n+f5/n+f5/n+f4AAAAACCTAAAAAHQZqgFKAowAAAAAhBAomqAUoCjAA"
    "AAAhBAUJqgFKAowAAAAIJMAAAAdZBmsAWrxX//1VVVVVVVVVVVVVVVVVVVVXxX//1VVVVVVVVVVVVVV"
    "VVVVVVXxX//1VVVVVVVVVVVVVVVVVVVVXxX//1VVVVVVVVVVVVVVVVVVVVXxX//1VVVVVVVVVVVVVVV"
    "VVVVVXxX//1VVVVVVVVVVVVVVVVVVVVXxX//1VVVVVVVVVVVVVVVVVVVVXxX//1VVVVVVVVVVVVVVVV"
    "VVVVXxX//1VVVVVVVVVVVVVVVVVVVVXxX//1VVVVVVVVVVVVVVVVVVVVXxX//1VVVVVVVVVVVVVVVVV"
    "VVVXxX//1VVVVVVVVVVVVVVVVVVVVXxX//1VVVVVVVVVVVVVVVVVVVVXxX//1VVVVVVVVVVVVVVVVVV"
    "VVXxX//1VVVVVVVVVVVVVVVVVVVVXxX//1VVVVVVVVVVVVVVVVVVVVXxX//1VVVVVVVVVVVVVVVVVVV"
    "VXxX//1VVVVVVVVVVVVVVVVVVVVXxX//1VVVVVVVVVVVVVVVVVVVVXxX//1VVVVVVVVVVVVVVVVVVVV"
    "Wf5/n+f5/n+f5/n+f5/n+f5/n+f5/n+f5/n+f5/n+f5/n+f5/n+f5/n+f5/n+f5/n+f5/n+f5/n+f5"
    "/n+f5/n+f5/n+f5/n+f5/n+AAAAdhBAomsAWrxX//1VVVVVVVVVVVVVVVVVVVVXxX//1VVVVVVVV"
    "VVVVVVVVVVVVXxX//1VVVVVVVVVVVVVVVVVVVVXxX//1VVVVVVVVVVVVVVVVVVVVXxX//1VVVVVVVVV"
    "VVVVVVVVVVVXxX//1VVVVVVVVVVVVVVVVVVVVXxX//1VVVVVVVVVVVVVVVVVVVVXxX//1VVVVVVVVVV"
    "VVVVVVVVVVXxX//1VVVVVVVVVVVVVVVVVVVVXxX//1VVVVVVVVVVVVVVVVVVVVXxX//1VVVVVVVVVVV"
    "VVVVVVVVVXxX//1VVVVVVVVVVVVVVVVVVVVXxX//1VVVVVVVVVVVVVVVVVVVVXxX//1VVVVVVVVVVVV"
    "VVVVVVVVXxX//1VVVVVVVVVVVVVVVVVVVVXxX//1VVVVVVVVVVVVVVVVVVVVXxX//1VVVVVVVVVVVVV"
    "VVVVVVVXxX//1VVVVVVVVVVVVVVVVVVVVXxX//1VVVVVVVVVVVVVVVVVVVVXxX//1VVVVVVVVVVVVVV"
    "VVVVVVWf5/n+f5/n+f5/n+f5/n+f5/n+f5/n+f5/n+f5/n+f5/n+f5/n+f5/n+f5/n+f5/n+f5/n+f"
    "5/n+f5/n+f5/n+f5/n+f5/n+f5/n+AAAAB2EEBQmsAWrxX//1VVVVVVVVVVVVVVVVVVVVXxX//1VV"
    "VVVVVVVVVVVVVVVVVVXxX//1VVVVVVVVVVVVVVVVVVVVXxX//1VVVVVVVVVVVVVVVVVVVVXxX//1VV"
    "VVVVVVVVVVVVVVVVVVXxX//1VVVVVVVVVVVVVVVVVVVVXxX//1VVVVVVVVVVVVVVVVVVVVXxX//1VV"
    "VVVVVVVVVVVVVVVVVVXxX//1VVVVVVVVVVVVVVVVVVVVXxX//1VVVVVVVVVVVVVVVVVVVVXxX//1VV"
    "VVVVVVVVVVVVVVVVVVXxX//1VVVVVVVVVVVVVVVVVVVVXxX//1VVVVVVVVVVVVVVVVVVVVXxX//1VV"
    "VVVVVVVVVVVVVVVVVVXxX//1VVVVVVVVVVVVVVVVVVVVXxX//1VVVVVVVVVVVVVVVVVVVVXxX//1VV"
    "VVVVVVVVVVVVVVVVVVXxX//1VVVVVVVVVVVVVVVVVVVVXxX//1VVVVVVVVVVVVVVVVVVVVXxX//1VV"
    "VVVVVVVVVVVVVVVVVVWf5/n+f5/n+f5/n+f5/n+f5/n+f5/n+f5/n+f5/n+f5/n+f5/n+f5/n+f5/"
    "n+f5/n+f5/n+f5/n+f5/n+f5/n+f5/n+f5/n+f5/n+AAAAACCTAAAAAHQZrgEqAowAAAAAhBAomuAS"
    "oCjAAAAAhBAUJrgEqAowAAAAIJMAAAAAZBmwA6gKMAAAAIQQKJsAOoCjAAAAAIQQFCbADqAowAAAAC"
    "CTAAAAAHQZsgFaAowAAAAAhBAomyAVoCjAAAAAhBAUJsgFaAowAAAAIJMAAAAAdBm0AXoCjAAAAACE"
    "ECibQBegKMAAAACEEBQm0AXoCjAAAAAgkwAAAAB0GbYBmgKMAAAAAIQQKJtgGaAowAAAAIQQFCbYBm"
    "gKMAAAACCTAAAAAHQZuAG6AowAAAAAhBAom4AboCjAAAAAhBAUJuAG6AowAAAAIJMAAAAAdBm6AdoC"
    "jAAAAACEECiboB2gKMAAAACEEBQm6AdoCjAAAAAgkwAAAAB0GbwB+gKMAAAAAIQQKJvAH6AowAAAAI"
    "QQFCbwB+gKMAAAACCTAAAAAHQZvgCGgKMAAAAAhBAom+AIaAowAAAAlBAUJvgCGgKMAAAAACCTAAAA"
    "AHQZoACKgKMAAAAAhBAomgAIqAowAAAAlBAUJoACKgKMAAAAACCTAAAAAHQZogCKgKMAAAAAhBAomi"
    "AIqAowAAAAlBAUJogCKgKMAAAAACCTAAAAAHQZpACKgKMAAAAAhBAomkAIqAowAAAAlBAUJpACKgKM"
    "AAAAACCTAAAAAHQZpgCKgKMAAAAAhBAommAIqAowAAAAlBAUJpgCKgKMAAAAACCTAAAAAHQZqACKgK"
    "MAAAAAhBAomoAIqAowAAAAlBAUJqACKgKMAAAAACCTAAAAAHQZqgCKgKMAAAAAhBAomqAIqAowAAAA"
    "lBAUJqgCKgKMAAAAACCTAAAAAHQZrACKgKMAAAAAhBAomsAIqAowAAAAlBAUJrACKgKMAAAAACCTAA"
    "AAAHQZrgCKgKMAAAAAhBAomuAIqAowAAAAlBAUJrgCKgKMAAAAACCTAAAAAHQZsACKgKMAAAAAhBAo"
    "mwAIqAowAAAAlBAUJsACKgKMAAAAACCTAAAAAHQZsgCKgKMAAAAAhBAomyAIqAowAAAAlBAUJsgCKg"
    "KMAAAAACCTAAAAAHQZtACKgKMAAAAAhBAom0AIqAowAAAAlBAUJtACKgKMAAAAACCTAAAAAHQZtgCK"
    "gKMAAAAAhBAom2AIqAowAAAAlBAUJtgCKgKMAAAAACCTAAAAAHQZuACKgKMAAAAAhBAom4AIqAowAA"
    "AAlBAUJuACKgKMAAAAACCTAAAAAHQZugCKgKMAAAAAhBAom6AIqAowAAAAlBAUJugCKgKMA="
)


class ProtocolError(RuntimeError):
    """Raised when one NDJSON frame violates the public protocol."""


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
    def __init__(self, sock: socket.socket):
        self.sock = sock
        self._buffer = bytearray()
        self._discarding_oversized = False

    def read_message(self, timeout: Optional[float]) -> Optional[Dict[str, Any]]:
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


@dataclass(frozen=True)
class FrameTimestamp:
    frame: int
    ts: int
    mono_ns: int


@dataclass
class MockConfig:
    socket_path: Path
    match_root: Path
    mode: str
    camera_ready_delay_ms: int
    result_delay_ms: int
    fps: float
    width: int
    height: int
    post_terminal_ms: int
    jitter_ms: int
    drop_every_nth_frame: int
    global_log_path: Optional[Path]
    heartbeat_interval_s: float = DEFAULT_HEARTBEAT_INTERVAL_S
    peer_timeout_s: float = DEFAULT_PEER_TIMEOUT_S


@dataclass
class MatchContext:
    match_id: str
    match_dir: Path
    side_map: Dict[str, str]
    weapon: Any = None
    sensor: Any = None
    match_begin_ts: Optional[int] = None
    voice_end_ts: Optional[int] = None
    recording_start_ts: Optional[int] = None
    first_frame_ts: Optional[int] = None
    first_frame_mono_ns: Optional[int] = None
    signals: List[Dict[str, Any]] = field(default_factory=list)
    terminal_signal: Optional[Dict[str, Any]] = None
    finalizing: bool = False
    cancelled: bool = False

    @property
    def ai_dir(self) -> Path:
        return self.match_dir / "ai"

    @property
    def mp4_path(self) -> Path:
        return self.match_dir / f"segment_{self.match_id}.mp4"

    @property
    def frame_timestamps_path(self) -> Path:
        return self.ai_dir / "frame_timestamps.jsonl"

    @property
    def signal_mapping_path(self) -> Path:
        return self.ai_dir / "signal_frame_mapping.json"

    @property
    def analysis_result_path(self) -> Path:
        return self.ai_dir / "mock_analysis_result.json"

    @property
    def message_log_path(self) -> Path:
        return self.ai_dir / "mock_ai_messages.ndjson"


class PartnerMockAIService:
    def __init__(self, config: MockConfig):
        self.config = config
        self._server: Optional[socket.socket] = None
        self._client: Optional[NdjsonSocket] = None
        self._client_socket: Optional[socket.socket] = None
        self._client_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._active_match: Optional[MatchContext] = None
        self._next_outbound_id = 1
        self._last_sent = time.monotonic()
        self._last_received = time.monotonic()
        self._shutdown = threading.Event()

    def serve_forever(self) -> None:
        self._prepare_socket_path()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            self._server = server
            server.bind(str(self.config.socket_path))
            self._secure_socket_file()
            server.listen(1)
            server.settimeout(0.5)
            print(f"[MOCK-AI] listening on {self.config.socket_path}")
            print(f"[MOCK-AI] mode={self.config.mode} match_root={self.config.match_root}")
            while not self._shutdown.is_set():
                try:
                    conn, _addr = server.accept()
                except socket.timeout:
                    continue
                except OSError:
                    if self._shutdown.is_set():
                        break
                    raise
                self._replace_client(conn)

    def stop(self) -> None:
        self._shutdown.set()
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass

    def _prepare_socket_path(self) -> None:
        self.config.socket_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._secure_runtime_dir()
        if self.config.socket_path.exists():
            mode = self.config.socket_path.stat().st_mode
            if not stat.S_ISSOCK(mode):
                raise RuntimeError(f"socket path exists and is not a socket: {self.config.socket_path}")
            self.config.socket_path.unlink()
        self.config.match_root.mkdir(parents=True, exist_ok=True)

    def _secure_runtime_dir(self) -> None:
        try:
            os.chmod(self.config.socket_path.parent, 0o700)
        except Exception:
            pass
        try:
            uid = pwd.getpwnam("nvidia").pw_uid
            gid = grp.getgrnam("nvidia").gr_gid
            os.chown(self.config.socket_path.parent, uid, gid)
        except Exception:
            pass

    def _secure_socket_file(self) -> None:
        os.chmod(self.config.socket_path, 0o600)
        try:
            uid = pwd.getpwnam("nvidia").pw_uid
            gid = grp.getgrnam("nvidia").gr_gid
            os.chown(self.config.socket_path, uid, gid)
        except Exception:
            pass

    def _replace_client(self, conn: socket.socket) -> None:
        with self._client_lock:
            old_socket = self._client_socket
            self._client_socket = conn
        if old_socket is not None:
            self._close_socket(old_socket)
        thread = threading.Thread(target=self._handle_client, args=(conn,), daemon=True)
        thread.start()

    @staticmethod
    def _close_socket(sock: socket.socket) -> None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass

    def _handle_client(self, conn: socket.socket) -> None:
        client = NdjsonSocket(conn)
        with self._client_lock:
            self._client = client
            self._client_socket = conn
            self._next_outbound_id = 0
            self._last_sent = time.monotonic()
            self._last_received = time.monotonic()
        print("[MOCK-AI] backend connected")

        handshake_complete = False
        try:
            while not self._shutdown.is_set():
                try:
                    message = client.read_message(timeout=0.5)
                except EOFError:
                    break
                except OSError:
                    break
                except ProtocolError as exc:
                    print(f"[MOCK-AI] protocol error: {exc}")
                    self._log_note("protocol_error", {"reason": str(exc)})
                    continue

                now = time.monotonic()
                if message is None:
                    if handshake_complete and now - self._last_sent >= self.config.heartbeat_interval_s:
                        self._send("ping")
                    if now - self._last_received >= self.config.peer_timeout_s:
                        raise TimeoutError("backend heartbeat timed out")
                    continue

                self._last_received = now
                self._log_message("in", message)
                if not handshake_complete:
                    if message["type"] != "hello":
                        print(f"[MOCK-AI] closing connection: first message was {message['type']!r}, expected hello")
                        break
                    payload = message.get("payload") or {}
                    if payload.get("protocol_v") != PROTOCOL_VERSION:
                        print(f"[MOCK-AI] closing connection: unsupported protocol_v={payload.get('protocol_v')!r}")
                        break
                    handshake_complete = True
                self._dispatch(message)
        except TimeoutError as exc:
            print(f"[MOCK-AI] {exc}")
        finally:
            with self._client_lock:
                if self._client is client:
                    self._client = None
                if self._client_socket is conn:
                    self._client_socket = None
            self._close_socket(conn)
            print("[MOCK-AI] backend disconnected")

    def _dispatch(self, message: Dict[str, Any]) -> None:
        msg_type = message["type"]
        payload = message.get("payload") or {}

        if msg_type == "hello":
            self._send(
                "hello_ack",
                {
                    "role": "ai",
                    "app": "pistelink_partner_mock_ai",
                    "version": "0.1.0",
                    "protocol_v": PROTOCOL_VERSION,
                },
            )
            return
        if msg_type == "ping":
            self._send("pong", {"ref_id": message["id"]}, match_id=message.get("match_id"))
            return
        if msg_type == "pong":
            return
        if msg_type == "shutdown":
            self._shutdown.set()
            return
        if msg_type == "match_pre_start":
            self._on_match_pre_start(message, payload)
        elif msg_type == "match_begin_ack":
            self._on_match_begin_ack(message, payload)
        elif msg_type == "voice_end":
            self._on_voice_end(message, payload)
        elif msg_type == "signal":
            self._on_signal(message, payload)
        elif msg_type == "match_cancel":
            self._on_match_cancel(message, payload)
        else:
            print(f"[MOCK-AI] ignoring unsupported type {msg_type!r}")
            self._log_note("unknown_type", {"type": msg_type, "id": message.get("id")})

    def _on_match_pre_start(self, message: Dict[str, Any], payload: Dict[str, Any]) -> None:
        match_id = str(message.get("match_id") or payload.get("match_id") or "").strip()
        if not match_id:
            self._send(
                "camera_error",
                {"code": "CAM_INIT_FAIL", "reason": "match_pre_start missing match_id"},
            )
            return

        side_map = _normalise_side_map(payload.get("side_map"))
        ctx = MatchContext(
            match_id=match_id,
            match_dir=self.config.match_root / match_id,
            side_map=side_map,
            weapon=payload.get("weapon"),
            sensor=payload.get("sensor"),
        )
        ctx.match_dir.mkdir(parents=True, exist_ok=True)
        ctx.ai_dir.mkdir(parents=True, exist_ok=True)

        with self._state_lock:
            if self._active_match is not None and not self._active_match.finalizing:
                self._cancel_context_locked(self._active_match, "replaced_by_new_match")
            self._active_match = ctx

        self._log_note("match_pre_start_accepted", {"match_id": match_id, "mode": self.config.mode}, ctx)

        if self.config.mode == "camera_error":
            thread = threading.Thread(target=self._send_mock_camera_error, args=(ctx,), daemon=True)
        else:
            thread = threading.Thread(target=self._start_mock_recording, args=(ctx,), daemon=True)
        thread.start()

    def _send_mock_camera_error(self, ctx: MatchContext) -> None:
        _sleep_ms(self.config.camera_ready_delay_ms)
        if not self._is_current_active_match(ctx):
            return
        self._send(
            "camera_error",
            {
                "code": "CAM_INIT_FAIL",
                "reason": "mock camera_error mode; no real camera was opened",
            },
            match_id=ctx.match_id,
        )
        with self._state_lock:
            if self._active_match is ctx:
                self._active_match = None

    def _start_mock_recording(self, ctx: MatchContext) -> None:
        _sleep_ms(self.config.camera_ready_delay_ms)
        if not self._is_current_active_match(ctx):
            return

        ctx.recording_start_ts = epoch_ms()
        ctx.first_frame_ts = ctx.recording_start_ts + max(1, round(1000.0 / self.config.fps))
        ctx.first_frame_mono_ns = monotonic_ns() + int((ctx.first_frame_ts - ctx.recording_start_ts) * 1_000_000)

        self._send(
            "camera_ready",
            {
                "video_path": str(ctx.mp4_path),
                "recording_start_ts": ctx.recording_start_ts,
                "first_frame_ts": ctx.first_frame_ts,
                "first_frame_index": 0,
                "fps_nominal": self.config.fps,
                "width": self.config.width,
                "height": self.config.height,
                "frame_timestamps_path": str(ctx.frame_timestamps_path),
            },
            match_id=ctx.match_id,
        )

    def _on_match_begin_ack(self, message: Dict[str, Any], payload: Dict[str, Any]) -> None:
        ctx = self._match_for_message(message, payload)
        if ctx is None:
            return
        ctx.match_begin_ts = _payload_ts(payload, message, ("begin_ts", "ts"))

    def _on_voice_end(self, message: Dict[str, Any], payload: Dict[str, Any]) -> None:
        ctx = self._match_for_message(message, payload)
        if ctx is None:
            return
        ctx.voice_end_ts = _payload_ts(payload, message, ("voice_end_ts", "ts"))

    def _on_signal(self, message: Dict[str, Any], payload: Dict[str, Any]) -> None:
        ctx = self._match_for_message(message, payload)
        if ctx is None:
            return
        signal = _normalise_signal_payload(payload, message)
        if signal.get("source") == "light" or bool(signal.get("terminal")):
            ctx.terminal_signal = signal
            self._begin_finalize(ctx)
        else:
            ctx.signals.append(signal)

    def _on_match_cancel(self, message: Dict[str, Any], payload: Dict[str, Any]) -> None:
        ctx = self._match_for_message(message, payload)
        if ctx is None:
            return
        with self._state_lock:
            self._cancel_context_locked(ctx, str(payload.get("reason") or "match_cancel"))
            if self._active_match is ctx:
                self._active_match = None

    def _begin_finalize(self, ctx: MatchContext) -> None:
        with self._state_lock:
            if ctx.finalizing:
                return
            ctx.finalizing = True
        thread = threading.Thread(target=self._finalize_match, args=(ctx,), daemon=True)
        thread.start()

    def _finalize_match(self, ctx: MatchContext) -> None:
        try:
            _sleep_ms(self.config.result_delay_ms)
            if not self._is_current_or_finalizing_match(ctx):
                return

            if self.config.mode == "result_timeout":
                self._log_note(
                    "result_timeout_mode",
                    {"message": "terminal signal received, intentionally not sending match_result"},
                    ctx,
                )
                return

            frames = self._build_synthetic_frames(ctx)
            _write_frame_timestamps(ctx.frame_timestamps_path, frames)
            mappings = _build_signal_mappings(ctx.signals, ctx.terminal_signal, frames)
            _write_json(ctx.signal_mapping_path, mappings)
            _write_placeholder_mp4(ctx.mp4_path)

            winner, result_code, decision_source = self._decide_result(ctx)
            analysis_data = {
                "mock": True,
                "match_id": ctx.match_id,
                "decision_source": decision_source,
                "winner": winner,
                "result_code": result_code,
                "weapon": ctx.weapon,
                "sensor": ctx.sensor,
                "side_map": ctx.side_map,
                "match_begin_ts": ctx.match_begin_ts,
                "voice_end_ts": ctx.voice_end_ts,
                "recording_start_ts": ctx.recording_start_ts,
                "first_frame_ts": ctx.first_frame_ts,
                "terminal_signal": ctx.terminal_signal,
                "hit_signal_count": len(ctx.signals),
                "frame_count": len(frames),
            }
            _write_json(ctx.analysis_result_path, analysis_data)

            self._send(
                "match_result",
                {
                    "winner": winner,
                    "result_code": result_code,
                    "video_path": str(ctx.mp4_path),
                    "analysis_result_path": str(ctx.analysis_result_path),
                    "signal_frame_mapping_path": str(ctx.signal_mapping_path),
                    "processing_mode": "mock_final_lights",
                },
                match_id=ctx.match_id,
            )
        except Exception as exc:
            self._log_note(
                "finalize_exception",
                {"reason": str(exc), "traceback": traceback.format_exc()},
                ctx,
            )
            self._send(
                "match_result",
                {
                    "winner": "tie",
                    "result_code": 0,
                    "video_path": str(ctx.mp4_path),
                    "processing_mode": "mock_error",
                    "processing_error": str(exc),
                },
                match_id=ctx.match_id,
            )
        finally:
            with self._state_lock:
                if self._active_match is ctx and self.config.mode != "result_timeout":
                    self._active_match = None

    def _build_synthetic_frames(self, ctx: MatchContext) -> List[FrameTimestamp]:
        start_ts = ctx.first_frame_ts or ctx.recording_start_ts or ctx.voice_end_ts or ctx.match_begin_ts or epoch_ms()
        terminal_ts = _int_or_none((ctx.terminal_signal or {}).get("signal_ts"))
        signal_ts_values = [_int_or_none(signal.get("signal_ts")) for signal in ctx.signals]
        signal_ts_values = [value for value in signal_ts_values if value is not None]
        end_ts = max([start_ts + 1000, terminal_ts or start_ts] + signal_ts_values) + self.config.post_terminal_ms

        period_ms = 1000.0 / self.config.fps
        mono_anchor = ctx.first_frame_mono_ns or monotonic_ns()
        frames: List[FrameTimestamp] = []
        raw_index = 0
        last_ts = start_ts - 1
        while True:
            nominal = start_ts + round(raw_index * period_ms)
            if nominal > end_ts:
                break
            raw_index += 1
            if self.config.drop_every_nth_frame > 0 and raw_index % self.config.drop_every_nth_frame == 0:
                continue
            jitter = _deterministic_jitter_ms(len(frames), self.config.jitter_ms)
            ts = max(last_ts + 1, nominal + jitter)
            last_ts = ts
            mono_ns = mono_anchor + int((ts - start_ts) * 1_000_000)
            frames.append(FrameTimestamp(frame=len(frames), ts=ts, mono_ns=mono_ns))

        if not frames:
            frames.append(FrameTimestamp(frame=0, ts=start_ts, mono_ns=mono_anchor))
        return frames

    def _decide_result(self, ctx: MatchContext) -> Tuple[str, int, str]:
        if self.config.mode == "always_A":
            return "A", 8, "mock_forced_A"
        if self.config.mode == "always_B":
            return "B", 9, "mock_forced_B"
        if self.config.mode == "always_tie":
            return "tie", 10, "mock_forced_tie"
        if self.config.mode == "always_unjudged":
            return "tie", 0, "mock_forced_unjudged"

        lights = _final_lights_ab(ctx.terminal_signal, ctx.side_map)
        lit = {side for side, enabled in lights.items() if enabled}
        if len(lit) == 0:
            return "tie", 0, "mock_final_lights_no_touch"
        if len(lit) == 1:
            side = next(iter(lit))
            return side, 8 if side == "A" else 9, "mock_final_lights_single_touch"
        return "tie", 10, "mock_final_lights_double_touch"

    def _match_for_message(self, message: Dict[str, Any], payload: Dict[str, Any]) -> Optional[MatchContext]:
        match_id = str(message.get("match_id") or payload.get("match_id") or "")
        with self._state_lock:
            ctx = self._active_match
        if ctx is None:
            print(f"[MOCK-AI] ignoring {message.get('type')} with no active match")
            return None
        if match_id and ctx.match_id != match_id:
            print(f"[MOCK-AI] ignoring message for {match_id!r}; active match is {ctx.match_id!r}")
            return None
        return ctx

    def _is_current_active_match(self, ctx: MatchContext) -> bool:
        with self._state_lock:
            return self._active_match is ctx and not ctx.cancelled and not ctx.finalizing

    def _is_current_or_finalizing_match(self, ctx: MatchContext) -> bool:
        with self._state_lock:
            return self._active_match is ctx and not ctx.cancelled

    def _cancel_context_locked(self, ctx: MatchContext, reason: str) -> None:
        ctx.cancelled = True
        self._log_note("match_cancelled", {"match_id": ctx.match_id, "reason": reason}, ctx)
        _unlink_quietly(ctx.mp4_path)
        if ctx.ai_dir.exists():
            shutil.rmtree(ctx.ai_dir, ignore_errors=True)

    def _send(
        self,
        msg_type: str,
        payload: Optional[Dict[str, Any]] = None,
        match_id: Optional[str] = None,
    ) -> None:
        with self._client_lock:
            client = self._client
            if client is None:
                print(f"[MOCK-AI] dropped outbound {msg_type}: no backend connection")
                return
            msg_id = self._next_outbound_id
            self._next_outbound_id += 1
            message = make_message(msg_type, msg_id, payload=payload, match_id=match_id)
            try:
                client.write_message(message)
                self._last_sent = time.monotonic()
            except OSError as exc:
                print(f"[MOCK-AI] failed to send {msg_type}: {exc}")
                self._client = None
                return
        self._log_message("out", message)

    def _log_message(self, direction: str, message: Dict[str, Any]) -> None:
        ctx = self._context_for_log(message.get("match_id"))
        record = {
            "log_ts": epoch_ms(),
            "log_ts_mono_ns": monotonic_ns(),
            "direction": direction,
            "type": message.get("type"),
            "id": message.get("id"),
            "match_id": message.get("match_id"),
            "message": message,
        }
        self._write_log_record(record, ctx)

    def _log_note(self, event: str, detail: Dict[str, Any], ctx: Optional[MatchContext] = None) -> None:
        record = {
            "log_ts": epoch_ms(),
            "log_ts_mono_ns": monotonic_ns(),
            "direction": "note",
            "event": event,
            "match_id": ctx.match_id if ctx is not None else detail.get("match_id"),
            "detail": detail,
        }
        self._write_log_record(record, ctx)

    def _context_for_log(self, match_id: Any) -> Optional[MatchContext]:
        if match_id is None:
            return None
        with self._state_lock:
            ctx = self._active_match
        if ctx is not None and ctx.match_id == str(match_id):
            return ctx
        return None

    def _write_log_record(self, record: Dict[str, Any], ctx: Optional[MatchContext]) -> None:
        if self.config.global_log_path is not None:
            _append_json_line(self.config.global_log_path, record)
        if ctx is not None:
            _append_json_line(ctx.message_log_path, record)


def _normalise_side_map(value: Any) -> Dict[str, str]:
    side_map = value if isinstance(value, dict) else {}
    a = str(side_map.get("A", "left")).lower()
    b = str(side_map.get("B", "right")).lower()
    if a not in {"left", "right"}:
        a = "left"
    if b not in {"left", "right"}:
        b = "right"
    return {"A": a, "B": b}


def _normalise_signal_payload(payload: Dict[str, Any], message: Dict[str, Any]) -> Dict[str, Any]:
    signal = dict(payload)
    signal["signal_ts"] = _int_or_none(signal.get("signal_ts")) or _int_or_none(message.get("ts")) or epoch_ms()
    signal["signal_mono_ns"] = _int_or_none(signal.get("signal_mono_ns")) or _int_or_none(message.get("ts_mono_ns"))
    if "terminal" not in signal:
        signal["terminal"] = signal.get("source") == "light"
    return signal


def _payload_ts(payload: Dict[str, Any], message: Dict[str, Any], keys: Sequence[str]) -> int:
    for key in keys:
        value = _int_or_none(payload.get(key))
        if value is not None:
            return value
    return _int_or_none(message.get("ts")) or epoch_ms()


def _final_lights_ab(terminal_signal: Optional[Dict[str, Any]], side_map: Dict[str, str]) -> Dict[str, bool]:
    result = {"A": False, "B": False}
    if not terminal_signal or not isinstance(terminal_signal.get("final_lights"), dict):
        return result
    raw = terminal_signal["final_lights"]
    for side in ("A", "B"):
        if side in raw:
            result[side] = bool(raw[side])

    inverse = {visual: ab for ab, visual in side_map.items() if visual in {"left", "right"}}
    for visual_side in ("left", "right"):
        if visual_side in raw and visual_side in inverse:
            result[inverse[visual_side]] = bool(raw[visual_side])
    return result


def _build_signal_mappings(
    signals: Sequence[Dict[str, Any]],
    terminal_signal: Optional[Dict[str, Any]],
    frames: Sequence[FrameTimestamp],
) -> List[Dict[str, Any]]:
    mappings: List[Dict[str, Any]] = []
    for signal in list(signals) + ([terminal_signal] if terminal_signal is not None else []):
        signal_ts = _int_or_none(signal.get("signal_ts")) or epoch_ms()
        frame = _nearest_frame(signal_ts, frames)
        mappings.append(
            {
                "source": signal.get("source") or "unknown",
                "fight": _int_or_none(signal.get("fight")),
                "signal_ts": signal_ts,
                "signal_mono_ns": _int_or_none(signal.get("signal_mono_ns")),
                "mapped_frame": frame.frame if frame is not None else None,
                "mapped_frame_ts": frame.ts if frame is not None else None,
                "delta_ms": signal_ts - frame.ts if frame is not None else None,
                "mapping_mode": "nearest_synthetic",
            }
        )
    return mappings


def _nearest_frame(signal_ts: int, frames: Sequence[FrameTimestamp]) -> Optional[FrameTimestamp]:
    if not frames:
        return None
    timestamps = [frame.ts for frame in frames]
    insert_at = bisect.bisect_left(timestamps, signal_ts)
    if insert_at <= 0:
        return frames[0]
    if insert_at >= len(frames):
        return frames[-1]
    before = frames[insert_at - 1]
    after = frames[insert_at]
    return before if abs(signal_ts - before.ts) <= abs(signal_ts - after.ts) else after


def _write_frame_timestamps(path: Path, frames: Sequence[FrameTimestamp]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for frame in frames:
            handle.write(
                json.dumps(
                    {"frame": frame.frame, "ts": frame.ts, "mono_ns": frame.mono_ns},
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
            handle.write("\n")


def _write_placeholder_mp4(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if _write_placeholder_with_gstreamer(path):
        return
    if _write_placeholder_with_ffmpeg(path):
        return
    path.write_bytes(base64.b64decode(TINY_BLACK_MP4_BASE64))


def _write_placeholder_with_gstreamer(path: Path) -> bool:
    gst = shutil.which("gst-launch-1.0")
    if gst is None:
        return False
    cmd = [
        gst,
        "-q",
        "-e",
        "videotestsrc",
        "num-buffers=10",
        "pattern=black",
        "!",
        "video/x-raw,width=320,height=180,framerate=10/1",
        "!",
        "videoconvert",
        "!",
        "video/x-raw,format=I420",
        "!",
        "x264enc",
        "tune=zerolatency",
        "speed-preset=ultrafast",
        "bitrate=128",
        "key-int-max=10",
        "!",
        "video/x-h264,profile=baseline",
        "!",
        "h264parse",
        "config-interval=-1",
        "!",
        "mp4mux",
        "faststart=true",
        "!",
        "filesink",
        f"location={path}",
    ]
    return _run_placeholder_command(cmd, path)


def _write_placeholder_with_ffmpeg(path: Path) -> bool:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        return False
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "color=c=black:s=320x180:r=10:d=1",
        "-an",
        "-c:v",
        "libx264",
        "-profile:v",
        "baseline",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        str(path),
    ]
    return _run_placeholder_command(cmd, path)


def _run_placeholder_command(cmd: Sequence[str], path: Path) -> bool:
    try:
        if path.exists():
            path.unlink()
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10.0)
    except Exception:
        return False
    return result.returncode == 0 and path.exists() and path.stat().st_size > 0


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _append_json_line(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(data, ensure_ascii=False, separators=(",", ":")))
        handle.write("\n")


def _sleep_ms(value: int) -> None:
    if value > 0:
        time.sleep(value / 1000.0)


def _deterministic_jitter_ms(index: int, magnitude: int) -> int:
    if magnitude <= 0:
        return 0
    pattern = (-magnitude, 0, magnitude, 0)
    return pattern[index % len(pattern)]


def _int_or_none(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _unlink_quietly(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except Exception:
        pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a standalone PisteLink v1.1 mock AI service")
    parser.add_argument("--socket-path", default=os.environ.get("PISTELINK_AI_SOCKET", DEFAULT_SOCKET_PATH))
    parser.add_argument("--match-root", default=os.environ.get("PISTELINK_MATCH_ROOT", DEFAULT_MATCH_ROOT))
    parser.add_argument(
        "--mode",
        choices=[
            "final_lights",
            "always_A",
            "always_B",
            "always_tie",
            "always_unjudged",
            "camera_error",
            "result_timeout",
        ],
        default=os.environ.get("PISTELINK_MOCK_MODE", "final_lights"),
    )
    parser.add_argument("--camera-ready-delay-ms", type=int, default=int(os.environ.get("PISTELINK_MOCK_CAMERA_READY_DELAY_MS", "150")))
    parser.add_argument("--result-delay-ms", type=int, default=int(os.environ.get("PISTELINK_MOCK_RESULT_DELAY_MS", "250")))
    parser.add_argument("--fps", type=float, default=float(os.environ.get("PISTELINK_MOCK_FPS", "30.0")))
    parser.add_argument("--width", type=int, default=int(os.environ.get("PISTELINK_MOCK_WIDTH", "1280")))
    parser.add_argument("--height", type=int, default=int(os.environ.get("PISTELINK_MOCK_HEIGHT", "720")))
    parser.add_argument("--post-terminal-ms", type=int, default=int(os.environ.get("PISTELINK_MOCK_POST_TERMINAL_MS", "500")))
    parser.add_argument("--jitter-ms", type=int, default=int(os.environ.get("PISTELINK_MOCK_JITTER_MS", "0")))
    parser.add_argument("--drop-every-nth-frame", type=int, default=int(os.environ.get("PISTELINK_MOCK_DROP_EVERY_NTH_FRAME", "0")))
    parser.add_argument(
        "--global-log-path",
        default=os.environ.get("PISTELINK_MOCK_GLOBAL_LOG", "/tmp/pistelink_mock_ai_messages.ndjson"),
        help="Set empty string to disable the global log. Per-match ai/mock_ai_messages.ndjson is still written.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.fps <= 0:
        raise SystemExit("--fps must be positive")
    global_log_path = Path(args.global_log_path).expanduser() if args.global_log_path else None
    config = MockConfig(
        socket_path=Path(args.socket_path).expanduser(),
        match_root=Path(args.match_root).expanduser(),
        mode=args.mode,
        camera_ready_delay_ms=max(0, args.camera_ready_delay_ms),
        result_delay_ms=max(0, args.result_delay_ms),
        fps=args.fps,
        width=args.width,
        height=args.height,
        post_terminal_ms=max(0, args.post_terminal_ms),
        jitter_ms=max(0, args.jitter_ms),
        drop_every_nth_frame=max(0, args.drop_every_nth_frame),
        global_log_path=global_log_path,
    )
    service = PartnerMockAIService(config)
    try:
        service.serve_forever()
    except KeyboardInterrupt:
        service.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
