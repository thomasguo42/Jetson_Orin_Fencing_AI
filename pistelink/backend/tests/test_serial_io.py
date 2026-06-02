"""Unit tests for MCU frame parsing, CRC, and dedup (pure logic, runs on Windows)."""

import pytest

from backend.serial_io import (
    crc16_xmodem,
    parse_main_frame,
    parse_hit_frame,
    parse_light_signals,
    SerialReader,
)

# Real 0x50 frame captured in the Android reference log (jjLog.txt).
REAL_0X50 = bytes.fromhex("8e020000000000000001000000500300000000000006c2fd8e")


def _frame_body(raw: bytes) -> bytes:
    """Bytes between the two 0x8E delimiters."""
    return raw[1:-1]


# ── CRC ────────────────────────────────────────────────────────────────

def test_crc16_xmodem_known_vector():
    # CRC-16/XMODEM("123456789") == 0x31C3
    assert crc16_xmodem(b"123456789") == 0x31C3


def test_real_frame_crc_matches():
    body = _frame_body(REAL_0X50)
    crc_field = int.from_bytes(body[-2:], "big")
    assert crc_field == 0xC2FD
    # CRC covers leading 0x8E through data-length, excluding the CRC bytes.
    assert crc16_xmodem(b"\x8e" + body[:-2]) == crc_field


# ── Main frame parsing ───────────────────────────────────────────────────

def test_parse_real_0x50_frame():
    parsed = parse_main_frame(_frame_body(REAL_0X50))
    assert parsed is not None
    assert parsed["cmd"] == 0x50
    assert parsed["seq"] == 1
    # data content 03 00 00 00 00 00 → weapon=3, sensor=0 (matches Android log)
    assert parsed["data"][0] == 3
    assert parsed["data"][1] == 0


def test_parse_main_frame_rejects_bad_crc():
    body = bytearray(_frame_body(REAL_0X50))
    body[-1] ^= 0xFF  # corrupt CRC
    assert parse_main_frame(bytes(body)) is None


def test_parse_main_frame_rejects_short():
    assert parse_main_frame(b"\x02\x00") is None


def test_parse_main_frame_rejects_length_mismatch():
    body = bytearray(_frame_body(REAL_0X50))
    body.append(0x00)  # extra byte breaks declared length
    assert parse_main_frame(bytes(body)) is None


# ── Hit frame parsing ────────────────────────────────────────────────────

@pytest.mark.parametrize("char,expected", [
    (b"3", 3),   # blade contact
    (b"8", 8),   # A scores
    (b"9", 9),   # B scores
])
def test_parse_hit_frame_digits(char, expected):
    frame = b"AT" + char + b"YZ"
    assert parse_hit_frame(frame) == expected


def test_parse_hit_frame_no_digit_10():
    # Vendor-confirmed: there is no digit-10 hit frame (a tie is separate 8+9).
    assert parse_hit_frame(b"AT:YZ") is None   # ':' (0x3A) would decode to 10
    assert parse_hit_frame(b"AT0YZ") is None   # '0' is undefined


def test_parse_hit_frame_real():
    assert parse_hit_frame(bytes.fromhex("415433595a")) == 3  # AT3YZ
    assert parse_hit_frame(bytes.fromhex("415439595a")) == 9  # AT9YZ


def test_parse_hit_frame_rejects_bad_header():
    assert parse_hit_frame(b"XT3YZ") is None
    assert parse_hit_frame(b"AT3YX") is None
    assert parse_hit_frame(b"AT3Y") is None  # wrong length


# ── 0x52 dual final lights ───────────────────────────────────────────────

@pytest.mark.parametrize("data,expected", [
    (bytes([0x00, 0x03]), (True, False)),   # A valid score, B nothing → A lit
    (bytes([0x03, 0x00]), (False, True)),   # B valid score → B lit
    (bytes([0x00, 0x00]), (True, True)),    # both valid → tie lights
    (bytes([0x01, 0x02]), (False, False)),  # guard / invalid → neither lit
    (bytes([0x03, 0x03]), (False, False)),  # no hit either side
    (b"", (False, False)),                  # missing bytes default to not-lit
    (bytes([0x00]), (True, False)),         # only A byte present
])
def test_parse_light_signals(data, expected):
    assert parse_light_signals(data) == expected


# ── Dedup ────────────────────────────────────────────────────────────────

def test_main_frame_seq_dedup():
    # No callbacks → exercise dedup counters without an event loop.
    reader = SerialReader()
    reader._dispatch_main({"cmd": 0x50, "seq": 7, "data": b""})
    reader._dispatch_main({"cmd": 0x50, "seq": 7, "data": b""})  # same seq → dropped
    assert reader.dup_discarded == 1
    reader._dispatch_main({"cmd": 0x52, "seq": 8, "data": b""})  # new seq → accepted
    assert reader.dup_discarded == 1


def test_hit_frames_not_deduped():
    # Hit frames are real strikes and are NOT retransmitted (only 0x8E main
    # frames are sent 3×). Every ATxYZ must be kept — even identical digits in
    # quick succession. dup_discarded counts only main-frame retransmits now.
    reader = SerialReader()
    reader._dispatch_hit(3)
    reader._dispatch_hit(3)   # identical, immediately after → still kept
    reader._dispatch_hit(3)
    assert reader.dup_discarded == 0


# ── Buffer framing: split frames across serial reads (粘包/拆包) ──────────────

def test_split_hit_frame_survives_buffer_boundary():
    """A hit frame cut between two serial reads must not be dropped.

    Regression: the partial leading bytes used to fall through to del buf[:i]
    and vanish, so the completing bytes in the next read no longer matched —
    the hit (and its json.txt/event-stream entry) was silently lost.
    """
    reader = SerialReader()
    hits = []
    reader._dispatch_hit = lambda d: hits.append(d)

    buf = bytearray(b"AT8")                  # first read: only 3 of 5 bytes
    buf = reader._process_buffer(buf)
    assert hits == []                        # not enough yet
    assert bytes(buf) == b"AT8"              # partial preserved, not discarded

    buf.extend(b"YZ")                        # second read completes the frame
    buf = reader._process_buffer(buf)
    assert hits == [8]                       # hit recovered
    assert bytes(buf) == b""


def test_full_hit_then_partial_next_frame():
    reader = SerialReader()
    hits = []
    reader._dispatch_hit = lambda d: hits.append(d)

    buf = bytearray(b"AT8YZAT9")             # one complete + start of the next
    buf = reader._process_buffer(buf)
    assert hits == [8]
    assert bytes(buf) == b"AT9"              # trailing partial kept for next read

    buf.extend(b"YZ")
    reader._process_buffer(buf)
    assert hits == [8, 9]


def test_main_and_hit_coalesced_in_one_read():
    """Multiple frames glued into one read still parse (true 粘包 case)."""
    reader = SerialReader()
    hits = []
    reader._dispatch_hit = lambda d: hits.append(d)
    mains = []
    reader._dispatch_main = lambda f: mains.append(f["cmd"])

    buf = bytearray(REAL_0X50 + b"AT8YZ" + REAL_0X50)
    leftover = reader._process_buffer(buf)
    assert hits == [8]
    assert mains == [0x50, 0x50]
    assert bytes(leftover) == b""


def test_stray_delimiter_does_not_hold_buffer_hostage():
    """A spurious 0x8E with no closing delimiter must not pin later frames until
    the 4096 overflow wipes them — resync past it and recover what's behind.
    """
    reader = SerialReader()
    hits = []
    reader._dispatch_hit = lambda d: hits.append(d)

    # One stray 0x8E, then a long run of hit frames and no closing delimiter.
    buf = bytearray(b"\x8e" + b"AT8YZ" * 60)   # 301 bytes, over the resync cap
    buf = reader._process_buffer(buf)
    assert hits == [8] * 60                     # hits behind the stray byte recovered
    assert bytes(buf) == b""                    # nothing left pinned


def test_short_incomplete_main_frame_still_waits():
    """Below the resync cap, a 0x8E with no closing delimiter is kept intact —
    it may be a real frame still arriving; don't drop it prematurely.
    """
    reader = SerialReader()
    hits = []
    reader._dispatch_hit = lambda d: hits.append(d)

    partial = b"\x8e" + b"AT8YZ" * 3            # 16 bytes, under the cap
    buf = reader._process_buffer(bytearray(partial))
    assert hits == []                           # still waiting
    assert bytes(buf) == partial                # preserved intact

    # More data arrives, still no closing 0x8E → now over the cap → resync,
    # and the bytes that were waiting are recovered too.
    buf.extend(b"AT8YZ" * 60)
    buf = reader._process_buffer(buf)
    assert hits == [8] * 63
    assert bytes(buf) == b""
