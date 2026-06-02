"""Command-building logic for AudioPlayer (pure, no real playback)."""

import pytest

import backend.config as config_mod
from backend.config import Config
from backend.audio import AudioPlayer


def _player_with_config(tmp_path, monkeypatch, device):
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(f'[audio]\ndevice = "{device}"\n', encoding="utf-8")
    monkeypatch.setattr(config_mod, "_config", Config(str(cfg_path)))
    return AudioPlayer()


def test_explicit_device_forces_alsa_for_mpg123(tmp_path, monkeypatch):
    p = _player_with_config(tmp_path, monkeypatch, "plughw:CARD=pistelink,DEV=0")
    p._player = "mpg123"
    assert p._build_cmd("/s/start.mp3") == [
        "mpg123", "-q", "-o", "alsa", "-a", "plughw:CARD=pistelink,DEV=0",
        "/s/start.mp3",
    ]


@pytest.mark.parametrize("device", ["default", "", "   "])
def test_default_or_blank_device_keeps_player_default(tmp_path, monkeypatch, device):
    p = _player_with_config(tmp_path, monkeypatch, device)
    p._player = "mpg123"
    assert p._build_cmd("/s/start.mp3") == ["mpg123", "-q", "/s/start.mp3"]


def test_device_only_applied_to_mpg123(tmp_path, monkeypatch):
    """Other players don't take mpg123's -a flag, so the device is left off."""
    p = _player_with_config(tmp_path, monkeypatch, "plughw:CARD=pistelink,DEV=0")
    p._player = "ffplay"
    assert "-a" not in p._build_cmd("/s/start.mp3")
