"""Unit tests for config loading, defaults, hot-reload, and round-trip write."""

import time

from backend.config import Config, DEFAULTS


def test_defaults_when_file_missing(tmp_path):
    cfg = Config(str(tmp_path / "nope.toml"))
    assert cfg.get("http", "host") == "127.0.0.1"
    assert cfg.get("upload", "post_upload_action") == "delete_video_only"
    assert cfg.get("ai", "enabled") is True
    assert cfg.video_sync_offset_ms == 0


def test_missing_config_logs_warning(tmp_path, caplog):
    Config(str(tmp_path / "missing.toml"))
    assert "Config file not found" in caplog.text


def test_file_overrides_defaults(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[signal]\nvideo_sync_offset_ms = 60\n', encoding="utf-8")
    cfg = Config(str(p))
    assert cfg.video_sync_offset_ms == 60
    # untouched sections keep defaults
    assert cfg.get("http", "port") == DEFAULTS["http"]["port"]


def test_hot_reload_picks_up_change(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[signal]\nvideo_sync_offset_ms = 0\n', encoding="utf-8")
    cfg = Config(str(p))
    assert cfg.video_sync_offset_ms == 0

    time.sleep(0.01)
    p.write_text('[signal]\nvideo_sync_offset_ms = -120\n', encoding="utf-8")
    # bump mtime explicitly in case the FS clock is coarse
    import os
    os.utime(p, (time.time() + 1, time.time() + 1))
    assert cfg.video_sync_offset_ms == -120


def test_update_and_write_roundtrip(tmp_path):
    p = tmp_path / "config.toml"
    cfg = Config(str(p))
    cfg.batch_update_and_write({"upload": {"host": "1.2.3.4", "port": 2121}})
    reloaded = Config(str(p))
    assert reloaded.get("upload", "host") == "1.2.3.4"
    assert reloaded.get("upload", "port") == 2121
    assert reloaded.get("upload", "post_upload_action") == "delete_video_only"
