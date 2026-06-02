"""AI Unix Domain Socket client: NDJSON framing, handshake, heartbeat, dispatch."""

import asyncio
import json
import logging
import sys
import time
from asyncio import StreamReader, StreamWriter
from pathlib import Path

from .config import get_config

logger = logging.getLogger(__name__)

MAX_FRAME_BYTES = 64 * 1024
HEARTBEAT_IDLE_S = 2
HEARTBEAT_TIMEOUT_S = 6


def _socket_exists(socket_path: str) -> bool:
    try:
        Path(socket_path).stat()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return True


def _log_missing_socket(socket_path: str, delay: float,
                        already_logged: bool) -> bool:
    if not already_logged:
        logger.warning(
            "AI socket not found at %s; waiting for AI service "
            "(retrying in %ds)",
            socket_path,
            delay,
        )
        return True

    logger.debug(
        "AI socket still missing at %s; retrying in %ds",
        socket_path,
        delay,
    )
    return True


class AIClient:
    def __init__(self, on_event=None):
        self._on_event = on_event  # async callable(event_type, payload, match_id)
        self._running = False
        self._reader: StreamReader | None = None
        self._writer: StreamWriter | None = None
        self._send_id: int = 0
        self._last_recv_time: float = 0
        self._last_send_time: float = 0
        self._handshake_done: bool = False
        self._fatal_protocol_error: bool = False
        self._write_lock = asyncio.Lock()
        self.bytes_sent: int = 0
        self.bytes_recv: int = 0
        self.connected: bool = False

    @property
    def last_recv_time(self) -> float:
        return self._last_recv_time

    async def run(self):
        if sys.platform == "win32":
            logger.info("Windows — AI socket disabled (FR-2.1)")
            self._running = True
            while self._running:
                await asyncio.sleep(5)
            return

        config = get_config()
        socket_path = config.get("ai", "socket")
        rmin = config.get("ai", "reconnect_min_s")
        rmax = config.get("ai", "reconnect_max_s")

        self._running = True
        delay = rmin
        missing_socket_logged = False

        while self._running:
            if not _socket_exists(socket_path):
                missing_socket_logged = _log_missing_socket(
                    socket_path,
                    delay,
                    missing_socket_logged,
                )
                await self._sleep(delay)
                delay = min(delay * 2, rmax)
                continue

            try:
                await self._connect(socket_path)
                missing_socket_logged = False
                if self._fatal_protocol_error:
                    logger.error("AI protocol error is fatal; reconnect disabled")
                elif not self._handshake_done:
                    raise ConnectionError("AI handshake failed")
                else:
                    delay = rmin
                    await self._read_loop()
            except FileNotFoundError:
                missing_socket_logged = _log_missing_socket(
                    socket_path,
                    delay,
                    missing_socket_logged,
                )
            except (OSError, asyncio.IncompleteReadError, ConnectionError) as e:
                logger.error("AI socket error (%s), reconnecting in %ds", e, delay)
            except asyncio.CancelledError:
                break

            self.connected = False
            self._handshake_done = False
            if self._writer:
                self._writer.close()
                self._writer = None

            if self._fatal_protocol_error:
                self._running = False
                break

            await self._sleep(delay)
            delay = min(delay * 2, rmax)

    async def _connect(self, socket_path: str):
        logger.info("Connecting to AI at %s", socket_path)
        reader, writer = await asyncio.open_unix_connection(socket_path)
        self._reader = reader
        self._writer = writer
        self._send_id = 0
        self._handshake_done = False
        self.connected = True

        # Send hello
        config = get_config()
        await self._send_raw("hello", {
            "role": "backend",
            "app": "pistelink",
            "version": "0.1.0",
            "protocol_v": 1,
        })

        # Wait for hello_ack with timeout
        hello_event = await self._recv_event()
        if hello_event is None or hello_event[0] != "hello_ack":
            logger.error("Handshake failed: got %s", hello_event)
            writer.close()
            self.connected = False
            return

        _, payload, _ = hello_event
        ai_proto_v = payload.get("protocol_v", 0)
        if ai_proto_v != 1:
            logger.error("AI protocol version mismatch: ai=%d, us=1", ai_proto_v)
            writer.close()
            self.connected = False
            self._fatal_protocol_error = True
            if self._on_event:
                await self._on_event("ai_error", {
                    "code": "E_AI_PROTO_VER",
                    "reason": f"AI protocol v={ai_proto_v}, expected v=1",
                }, "")
            return

        self._handshake_done = True
        self._last_recv_time = time.monotonic()
        self._last_send_time = self._last_recv_time
        logger.info("AI handshake complete")

    async def _read_loop(self):
        self._last_recv_time = time.monotonic()
        self._last_send_time = self._last_recv_time

        while self._running:
            # Heartbeat: send ping if idle
            now = time.monotonic()
            if now - self._last_send_time >= HEARTBEAT_IDLE_S:
                await self._send_raw("ping", None)

            # Heartbeat: check timeout
            if now - self._last_recv_time >= HEARTBEAT_TIMEOUT_S:
                logger.warning("AI heartbeat timeout")
                break

            # Wake in time to send the next keepalive ping (idle interval), not
            # just at the timeout boundary. Budgeting readline against the full
            # timeout meant we'd sleep ~6 s, fire one ping, then immediately
            # declare timeout on the same iteration before the peer's pong could
            # arrive — so the connection died on every idle gap between matches.
            timeout = max(0.1, min(
                HEARTBEAT_IDLE_S - (now - self._last_send_time),
                HEARTBEAT_TIMEOUT_S - (now - self._last_recv_time),
            ))
            try:
                line = await asyncio.wait_for(
                    self._reader.readline(), timeout=timeout
                )
            except asyncio.TimeoutError:
                continue

            if not line:
                logger.info("AI socket closed by peer")
                break

            self._last_recv_time = time.monotonic()
            self.bytes_recv += len(line)

            if len(line) > MAX_FRAME_BYTES:
                logger.warning("Frame too large (%d bytes), discarding", len(line))
                continue

            try:
                msg = json.loads(line.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                logger.warning("NDJSON parse error")
                if self._on_event:
                    await self._on_event("ai_error", {
                        "code": "E_AI_BAD_FRAME",
                        "reason": "JSON parse failed",
                    }, "")
                continue

            await self._dispatch(msg)

    async def _dispatch(self, msg: dict):
        event_type = msg.get("type", "")
        payload = msg.get("payload", {}) or {}
        match_id = str(msg.get("match_id", ""))

        if event_type in ("ping", "pong"):
            # Reply to a ping with a pong referencing the ping's id (§6).
            if event_type == "ping":
                await self._send_raw("pong", {"ref_id": msg.get("id", 0)})
            return

        if event_type not in (
            "hello_ack", "camera_ready", "camera_error", "match_result"
        ):
            if event_type:
                logger.debug("Unknown AI event type: %s", event_type)
            return

        if self._on_event:
            await self._on_event(event_type, payload, match_id)

    async def send(self, event_type: str, payload: dict | None = None,
                   match_id: str | None = None):
        """Public send — enqueue a message to AI."""
        await self._send_raw(event_type, payload, match_id)

    async def _send_raw(self, event_type: str, payload: dict | None,
                        match_id: str | None = None):
        if not self._writer:
            return

        msg: dict = {
            "v": 1,
            "type": event_type,
            "id": self._send_id,
            "ts": int(time.time() * 1000),
            "ts_mono_ns": time.monotonic_ns(),  # §4: optional monotonic envelope ts
        }
        self._send_id += 1

        if match_id:
            msg["match_id"] = match_id
        if payload:
            msg["payload"] = payload

        line = json.dumps(msg, ensure_ascii=False, separators=(",", ":")) + "\n"
        data = line.encode("utf-8")

        async with self._write_lock:
            try:
                self._writer.write(data)
                await self._writer.drain()
                self._last_send_time = time.monotonic()
                self.bytes_sent += len(data)
            except OSError:
                pass  # Will be caught by read_loop

    async def _recv_event(self):
        """Read one event, used during handshake."""
        try:
            line = await asyncio.wait_for(
                self._reader.readline(), timeout=HEARTBEAT_TIMEOUT_S
            )
        except asyncio.TimeoutError:
            return None

        if not line:
            return None

        self.bytes_recv += len(line)
        try:
            msg = json.loads(line.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None

        return (msg.get("type", ""), msg.get("payload", {}) or {}, str(msg.get("match_id", "")))

    async def _sleep(self, seconds: float):
        """Sleep in 0.5s steps, aborting early if stop() is called."""
        while seconds > 0 and self._running:
            s = min(0.5, seconds)
            await asyncio.sleep(s)
            seconds -= s

    async def stop(self):
        if self._handshake_done and self._writer:
            try:
                await self._send_raw("shutdown", {})
            except Exception:
                pass
        self._running = False
        if self._writer:
            self._writer.close()
