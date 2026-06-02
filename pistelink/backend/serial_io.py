"""MCU serial communication: frame parsing, CRC, dedup, dispatch."""

import asyncio
import logging
import sys
import time

import serial
import serial_asyncio

from .config import get_config

logger = logging.getLogger(__name__)

MAIN_FRAME_DELIM = 0x8E
MAIN_FRAME_START = 0x02
HIT_FRAME_LEN = 5
MAX_BUFFER_BYTES = 4096  # safety cap: discard if buffer exceeds this
# Real main frames are ~25 bytes (tiny control payloads). If a leading 0x8E has
# no closing 0x8E within this many bytes, it is spurious — resync past it rather
# than holding the buffer hostage until MAX_BUFFER_BYTES wipes everything behind it.
MAX_MAIN_FRAME_BYTES = 256


def crc16_xmodem(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = (crc << 1) ^ 0x1021
            else:
                crc <<= 1
            crc &= 0xFFFF
    return crc


def parse_main_frame(buffer: bytes) -> dict | None:
    """Parse a main frame buffer (bytes between two 0x8E markers)."""
    if len(buffer) < 17 or buffer[0] != MAIN_FRAME_START:
        return None

    sn = buffer[1:7].decode("ascii", errors="replace")
    ver = buffer[7]
    seq = buffer[8]
    cmd = buffer[12]

    data_len = int.from_bytes(buffer[-4:-2], "big")
    expected_len = 17 + data_len
    if len(buffer) != expected_len:
        return None

    data = buffer[13 : 13 + data_len]
    crc_received = int.from_bytes(buffer[-2:], "big")
    # CRC-16/XMODEM covers the leading 0x8E delimiter through the data-length
    # field, excluding the CRC bytes themselves (verified against real frames).
    if crc16_xmodem(bytes([MAIN_FRAME_DELIM]) + buffer[:-2]) != crc_received:
        return None

    return {"sn": sn, "ver": ver, "seq": seq, "cmd": cmd, "data": data}


def parse_light_signals(data: bytes) -> tuple[bool, bool]:
    """Decode a 0x52 (round-end / light) frame's dual final electric signals.

    The 0x52 data field carries BOTH sides' final signals (MCU protocol V2.0):
      byte0 = side A (甲方/left), byte1 = side B (乙方/right), byte2-5 reserved.
    Per-byte value: 0x00 = valid scoring hit (light ON) / 0x01 = hit guard /
      0x02 = invalid hit (pause) / 0x03 = no hit. Only 0x00 means the scoring
    light is lit. Returns (a_lit, b_lit). Missing bytes default to "not lit".
    """
    a_byte = data[0] if len(data) > 0 else 0x03
    b_byte = data[1] if len(data) > 1 else 0x03
    return (a_byte == 0x00, b_byte == 0x00)


def parse_hit_frame(data: bytes) -> int | None:
    """Try to parse a 5-byte hit frame. Returns digit (1-10) or None."""
    if len(data) != HIT_FRAME_LEN:
        return None
    if data[0] != 0x41 or data[1] != 0x54:  # 'A', 'T'
        return None
    if data[3] != 0x59 or data[4] != 0x5A:  # 'Y', 'Z'
        return None
    # Electric-signal byte: a single ASCII char '1'..'9' (0x31..0x39) → 1..9.
    # The protocol table also lists "10" (double score), but the MCU vendor
    # confirmed this is a documentation error — there is NO digit-10 hit frame.
    # A tie / double touch is reported as separate '8' and '9' frames within a
    # short time window; the per-digit dedup keeps one of each, so both A and B
    # score (see FR-1.4 / FR-6.1).
    digit = data[2] - 0x30
    if 1 <= digit <= 9:
        return digit
    return None


class SerialReader:
    def __init__(self, on_main_frame=None, on_hit_frame=None):
        self._on_main_frame = on_main_frame
        self._on_hit_frame = on_hit_frame
        self._last_seq: int | None = None
        self._running = False
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self.crc_errors: int = 0
        self.connection_errors: int = 0
        self.dup_discarded: int = 0
        self.last_frame_time: float = 0

    @property
    def running(self) -> bool:
        return self._running

    async def run(self):
        if sys.platform == "win32":
            logger.info("Windows — serial disabled (FR-1.1)")
            self._running = True
            while self._running:
                await asyncio.sleep(5)
            return

        self._running = True
        reconnect_delay = 2

        while self._running:
            config = get_config()
            device = config.get("serial", "device")
            baud = config.get("serial", "baud")
            try:
                self._reader, self._writer = await serial_asyncio.open_serial_connection(
                    url=device, baudrate=baud, bytesize=8, parity="N", stopbits=1
                )
                logger.info("Serial connected: %s @ %d", device, baud)
                reconnect_delay = 2
                await self._read_loop()
            except (OSError, serial.SerialException) as e:
                logger.error("Serial error (%s), reconnecting in %ds", e, reconnect_delay)
                self.connection_errors += 1
                await self._sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, 30)
            except asyncio.CancelledError:
                break

    async def _read_loop(self):
        buf = bytearray()
        while self._running:
            try:
                chunk = await self._reader.read(256)
            except (OSError, serial.SerialException):
                break
            if not chunk:
                break
            buf.extend(chunk)
            if len(buf) > MAX_BUFFER_BYTES:
                logger.warning("Serial buffer overflow (%d bytes), discarding", len(buf))
                buf.clear()
            buf = self._process_buffer(buf)

    def _process_buffer(self, buf: bytearray) -> bytearray:
        i = 0
        while i < len(buf):
            b = buf[i]

            if b == MAIN_FRAME_DELIM:
                end = buf.find(MAIN_FRAME_DELIM, i + 1)
                if end == -1:
                    # No closing delimiter yet. Normally wait for more data —
                    # unless we've already accumulated more than a whole frame's
                    # worth, in which case this 0x8E is spurious: skip it and
                    # resync so frames piled up behind it aren't lost to overflow.
                    if len(buf) - i > MAX_MAIN_FRAME_BYTES:
                        i += 1
                        continue
                    break
                frame_buf = bytes(buf[i + 1 : end])
                parsed = parse_main_frame(frame_buf)
                if parsed is not None:
                    self._dispatch_main(parsed)
                else:
                    self.crc_errors += 1
                i = end + 1
                continue

            if b == 0x41:  # 'A' — potential hit frame
                if i + HIT_FRAME_LEN > len(buf):
                    # Possibly a hit frame split across two reads — leave the
                    # partial for the next read instead of letting del buf[:i]
                    # drop it (mirrors the main-frame `end == -1` break). A stray
                    # tail 'A' just waits one read cycle.
                    break
                candidate = bytes(buf[i : i + HIT_FRAME_LEN])
                digit = parse_hit_frame(candidate)
                if digit is not None:
                    self._dispatch_hit(digit)
                    i += HIT_FRAME_LEN
                    continue
                i += 1
                continue

            i += 1

        if i > 0:
            del buf[:i]
        return buf

    def _dispatch_main(self, frame: dict):
        cmd = frame["cmd"]
        seq = frame["seq"]

        if self._last_seq is not None and seq == self._last_seq:
            self.dup_discarded += 1
            return
        self._last_seq = seq

        self.last_frame_time = time.monotonic()
        recv_ts = int(time.time() * 1000)
        recv_mono_ns = time.monotonic_ns()
        if self._on_main_frame:
            asyncio.create_task(self._on_main_frame(cmd, frame, recv_ts, recv_mono_ns))

    def _dispatch_hit(self, digit: int):
        # Hit frames are NOT retransmitted (only the 0x8E main frames are sent
        # 3×, by sequence number). Each ATxYZ on the wire is a distinct real
        # strike — record every one. No dedup. (Frame-split reassembly in
        # _process_buffer already guarantees one logical frame → one dispatch.)
        self.last_frame_time = time.monotonic()
        recv_ts = int(time.time() * 1000)
        recv_mono_ns = time.monotonic_ns()
        if self._on_hit_frame:
            asyncio.create_task(self._on_hit_frame(digit, recv_ts, recv_mono_ns))

    async def _sleep(self, seconds: float):
        """Sleep in 0.5s steps, aborting early if stop() is called."""
        while seconds > 0 and self._running:
            s = min(0.5, seconds)
            await asyncio.sleep(s)
            seconds -= s

    async def stop(self):
        self._running = False
        if self._writer:
            self._writer.close()
