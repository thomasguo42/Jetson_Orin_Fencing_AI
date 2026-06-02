"""Lifecycle/orchestration tests for main.py (v1.1 protocol).

main.py is async and drives module globals (match, audio, ai_client). These
tests inject fakes for audio/AI and run each scenario via asyncio.run(), so no
pytest-asyncio dependency is needed. Serial/AI sockets are not involved — we
call the same callbacks the serial_io/ai_io layers (and the Debug API) use.
"""

import asyncio
import json

import pytest

import backend.config as config_mod
from backend.config import Config
from backend import main
from backend.models import MatchState
from backend import storage


class FakeAudio:
    """Records play() calls; never fires on_play_done (tests drive that)."""
    def __init__(self):
        self.played = []

    def play(self, filename):
        self.played.append(filename)

    def clear(self):
        pass


class FakeAI:
    """Records every message sent to the AI as (type, payload, match_id)."""
    connected = True

    def __init__(self):
        self.sent = []

    async def send(self, event_type, payload=None, match_id=None):
        self.sent.append((event_type, payload, match_id))

    def signals(self, source):
        return [p for t, p, _ in self.sent
                if t == "signal" and p and p.get("source") == source]


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Temp storage root, short AI timeout, and injected fake audio/AI."""
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        f'[storage]\nroot = "{tmp_path.as_posix()}"\n'
        f'[ai]\nresult_timeout_s = 0.15\n'
        f'[signal]\nvideo_sync_offset_ms = 0\n',
        encoding="utf-8",
    )
    cfg = Config(str(cfg_path))
    monkeypatch.setattr(config_mod, "_config", cfg)

    audio = FakeAudio()
    ai = FakeAI()
    monkeypatch.setattr(main, "audio", audio)
    monkeypatch.setattr(main, "ai_client", ai)
    main.match.reset()
    main._cancel_settle_timeout()
    yield {"cfg": cfg, "audio": audio, "ai": ai, "root": tmp_path}
    main.match.reset()
    main._cancel_settle_timeout()


def _json(root, match_id):
    return json.loads((root / "matches" / match_id / "json.txt").read_text("utf-8"))


# ── match_pre_start / side_map ────────────────────────────────────────────

def test_match_pre_start_sends_side_map(env):
    async def scenario():
        await main.on_main_frame(0x50, {"data": bytes([2, 0])}, 1000, 1)
    asyncio.run(scenario())

    pre = [(t, p) for t, p, _ in env["ai"].sent if t == "match_pre_start"]
    assert len(pre) == 1
    assert pre[0][1]["side_map"] == {"A": "left", "B": "right"}
    assert pre[0][1]["weapon"] == 2
    assert main.match.state == MatchState.PREPARING


# ── monotonic guard ───────────────────────────────────────────────────────

def test_hit_signal_monotonic_guard(env):
    env["cfg"]._data["signal"]["video_sync_offset_ms"] = 60

    async def scenario():
        await main.on_main_frame(0x50, {"data": bytes([2, 0])}, 1000, 1)
        await main.on_ai_event("camera_ready", {}, "")
        await main.on_hit_frame(8, 3000, 0)            # no monotonic source
        await main.on_hit_frame(9, 4000, 5_000_000_000)  # real monotonic source
        await asyncio.sleep(0)  # let _bg signal sends run
    asyncio.run(scenario())

    hits = env["ai"].signals("hit")
    assert hits[0]["signal_ts"] == 3060               # offset applied
    assert "signal_mono_ns" not in hits[0]            # mono=0 → not emitted
    assert hits[1]["signal_ts"] == 4060
    assert hits[1]["signal_mono_ns"] == 5_000_000_000 + 60 * 1_000_000


# ── 先写后改: 0x52 temp result, then match_result backfill ─────────────────

def test_round_end_writes_temp_then_backfills(env):
    async def scenario():
        await main.on_main_frame(0x50, {"data": bytes([1, 0])}, 1000, 1)
        await main.on_ai_event("camera_ready", {}, "")
        await main.on_hit_frame(3, 2000, 2)
        await main.on_hit_frame(8, 3000, 3)
        # 0x52: A valid (0x00), B nothing (0x03) → temp result 8
        await main.on_main_frame(0x52, {"data": bytes([0x00, 0x03])}, 4000, 4)
        await asyncio.sleep(0)
        mid = main.match.match_id
        assert main.match.state == MatchState.SETTLING
        # json.txt already on disk with the light-derived temp result
        d = _json(env["root"], mid)
        assert d["result"] == 8
        assert d["list"] == [{"timeStamp": 2000, "fight": 3},
                             {"timeStamp": 3000, "fight": 8}]
        # light forwarded with terminal + final_lights, never in list[]
        light = env["ai"].signals("light")[-1]
        assert light["terminal"] is True
        assert light["final_lights"] == {"A": True, "B": False}
        # Single-light result audio is queued immediately from final lights.
        assert env["audio"].played[-1] == "left.mp3"
        before_result = list(env["audio"].played)
        # AI result backfills (here same code), without duplicating winner audio.
        await main.on_ai_event(
            "match_result",
            {"winner": "A", "result_code": 8,
             "video_path": f"{env['root']}/matches/{mid}/segment.mp4"}, "")
        assert _json(env["root"], mid)["result"] == 8
        assert env["audio"].played == before_result
        # winner audio finished → finalize state
        await main.on_audio_done("left.mp3")
        assert main.match.state == MatchState.IDLE
    asyncio.run(scenario())


def test_round_end_tie_temp_result(env):
    async def scenario():
        await main.on_main_frame(0x50, {"data": bytes([2, 0])}, 1000, 1)
        await main.on_ai_event("camera_ready", {}, "")
        await main.on_main_frame(0x52, {"data": bytes([0x00, 0x00])}, 2000, 2)
        await asyncio.sleep(0)
        assert _json(env["root"], main.match.match_id)["result"] == 10
    asyncio.run(scenario())


# ── AI timeout: result_code=0 + light-derived winner audio ─────────────────

def test_ai_timeout_finalizes_zero_and_announces_lights(env):
    async def scenario():
        await main.on_main_frame(0x50, {"data": bytes([2, 0])}, 1000, 1)
        await main.on_ai_event("camera_ready", {}, "")
        await main.on_main_frame(0x52, {"data": bytes([0x03, 0x00])}, 2000, 2)  # B lit → 9
        await asyncio.sleep(0)
        mid = main.match.match_id
        assert _json(env["root"], mid)["result"] == 9  # temp result from lights
        await asyncio.sleep(0.3)  # exceed result_timeout_s
        d = _json(env["root"], mid)
        assert d["result"] == 0                         # AI undetermined
        assert env["audio"].played[-1] == "right.mp3"   # announce B (lights)
        assert env["audio"].played.count("right.mp3") == 1
        assert main.match.state == MatchState.IDLE
        # a late match_result is ignored (state is idle); json stays 0
        before = list(env["audio"].played)
        await main.on_ai_event("match_result",
                               {"winner": "B", "result_code": 9, "video_path": ""}, "")
        assert _json(env["root"], mid)["result"] == 0
        assert env["audio"].played == before
    asyncio.run(scenario())


def test_ai_timeout_no_lights_is_silent(env):
    async def scenario():
        await main.on_main_frame(0x50, {"data": bytes([2, 0])}, 1000, 1)
        await main.on_ai_event("camera_ready", {}, "")
        await main.on_main_frame(0x52, {"data": bytes([0x03, 0x03])}, 2000, 2)  # neither lit → 0
        await asyncio.sleep(0)
        await asyncio.sleep(0.3)
    # neither side lit → result 0 and no winner announcement
    asyncio.run(scenario())
    # collect non-start audio
    assert [f for f in env["audio"].played if f != "start.mp3"] == []


# ── camera_error: failure prompt + cleanup ────────────────────────────────

def test_camera_error_plays_failure_sound_and_cleans_up(env):
    async def scenario():
        await main.on_main_frame(0x50, {"data": bytes([1, 0])}, 1000, 1)
        mid = main.match.match_id
        assert storage.match_dir(mid).exists()
        await main.on_ai_event("camera_error",
                               {"code": "E_NO_CAMERA", "reason": "no /dev/video0"}, "")
        assert env["audio"].played[-1] == "CameraFailure.mp3"
        assert main.match.state == MatchState.IDLE
        assert not storage.match_dir(mid).exists()  # dir removed
    asyncio.run(scenario())
