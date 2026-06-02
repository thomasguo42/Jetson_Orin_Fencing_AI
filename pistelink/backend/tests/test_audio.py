"""Command-building logic for AudioPlayer (pure, no real playback)."""

import asyncio

import pytest

import backend.config as config_mod
import backend.audio as audio_mod
from backend.config import Config
from backend.audio import AudioPlayer


def _player_with_config(tmp_path, monkeypatch, device):
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(f'[audio]\ndevice = "{device}"\n', encoding="utf-8")
    monkeypatch.setattr(config_mod, "_config", Config(str(cfg_path)))
    return AudioPlayer()


def test_explicit_device_forces_alsa_for_mpg123(tmp_path, monkeypatch):
    monkeypatch.setattr(audio_mod, "_alsa_card_id_exists", lambda _card_id: True)
    p = _player_with_config(tmp_path, monkeypatch, "plughw:CARD=pistelink,DEV=0")
    p._player = "mpg123"
    assert p._build_cmd("/s/start.mp3") == [
        "mpg123", "-q", "-o", "alsa", "-a", "plughw:CARD=pistelink,DEV=0",
        "/s/start.mp3",
    ]


def test_missing_pistelink_card_falls_back_to_usb_audio(tmp_path, monkeypatch):
    monkeypatch.setattr(audio_mod, "_alsa_card_id_exists", lambda _card_id: False)
    monkeypatch.setattr(audio_mod, "_auto_alsa_device", lambda: "plughw:2,0")
    p = _player_with_config(tmp_path, monkeypatch, "plughw:CARD=pistelink,DEV=0")
    p._player = "mpg123"
    assert p._build_cmd("/s/start.mp3") == [
        "mpg123", "-q", "-o", "alsa", "-a", "plughw:2,0", "/s/start.mp3",
    ]


def test_auto_device_uses_detected_usb_audio(tmp_path, monkeypatch):
    monkeypatch.setattr(audio_mod, "_auto_alsa_device", lambda: "plughw:1,0")
    p = _player_with_config(tmp_path, monkeypatch, "auto")
    p._player = "gst-play-1.0"
    assert p._build_cmd("/s/start.mp3") == [
        "gst-play-1.0", "--quiet", "--audiosink", "alsasink device=plughw:1,0",
        "/s/start.mp3",
    ]


@pytest.mark.parametrize("device", ["default", "", "   "])
def test_default_or_blank_device_keeps_player_default(tmp_path, monkeypatch, device):
    p = _player_with_config(tmp_path, monkeypatch, device)
    p._player = "mpg123"
    assert p._build_cmd("/s/start.mp3") == ["mpg123", "-q", "/s/start.mp3"]


def test_device_only_applied_to_mpg123(tmp_path, monkeypatch):
    """Other players don't take mpg123's -a flag, so the device is left off."""
    monkeypatch.setattr(audio_mod, "_alsa_card_id_exists", lambda _card_id: True)
    p = _player_with_config(tmp_path, monkeypatch, "plughw:CARD=pistelink,DEV=0")
    p._player = "ffplay"
    assert "-a" not in p._build_cmd("/s/start.mp3")


def test_clear_during_subprocess_playback_does_not_raise(monkeypatch, caplog):
    class FakeProc:
        returncode = 0

        def __init__(self):
            self.communicate_started = asyncio.Event()
            self.release = asyncio.Event()
            self.killed = False

        async def communicate(self):
            self.communicate_started.set()
            await self.release.wait()
            return b"", b""

        def kill(self):
            self.killed = True
            self.release.set()

    fake_proc = FakeProc()

    async def fake_create_subprocess_exec(*_args, **_kwargs):
        return fake_proc

    async def scenario():
        monkeypatch.setattr(asyncio, "create_subprocess_exec",
                            fake_create_subprocess_exec)
        p = AudioPlayer()
        p._player = "gst-play-1.0"
        monkeypatch.setattr(p, "_build_cmd", lambda _path: ["gst-play-1.0", "file.mp3"])
        task = asyncio.create_task(p._play_subprocess("/s/start.mp3"))
        await fake_proc.communicate_started.wait()
        p.clear()
        await task
        return p

    player = asyncio.run(scenario())
    assert fake_proc.killed is True
    assert player._current_proc is None
    assert "Audio playback error" not in caplog.text
