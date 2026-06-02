"""Unit tests for storage: json.txt serialization, status derivation, paths."""

import json

import pytest

import backend.config as config_mod
from backend.config import Config
from backend.models import CurrentMatch, Signal, temp_result_from_lights
from backend import storage


@pytest.fixture
def storage_root(tmp_path, monkeypatch):
    """Point the global config at a temp storage root."""
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(f'[storage]\nroot = "{tmp_path.as_posix()}"\n', encoding="utf-8")
    cfg = Config(str(cfg_path))
    monkeypatch.setattr(config_mod, "_config", cfg)
    return tmp_path


def test_write_json_txt_matches_reference_format(storage_root):
    match = CurrentMatch()
    match.match_id = "1778397800089"
    match.begin_ts = 1778397803077
    match.voice_end_ts = 1778397806720
    match.signals = [
        Signal(fight=3, source="hit", signal_ts=1778397808026),
        Signal(fight=9, source="hit", signal_ts=1778397811772),
    ]
    storage.create_match_dir(match.match_id)
    storage.write_json_txt(match, result_code=10, video_sync_offset_ms=60)

    data = json.loads((storage.match_dir(match.match_id) / "json.txt").read_text("utf-8"))
    assert data["beginTimeStamp"] == 1778397803077
    assert data["voiceEndTime"] == 1778397806720
    assert data["result"] == 10
    assert data["video_sync_offset_ms"] == 60
    assert data["list"] == [
        {"timeStamp": 1778397808026, "fight": 3},
        {"timeStamp": 1778397811772, "fight": 9},
    ]


def test_finalize_only_includes_hit_signals_via_caller(storage_root):
    # 0x52 ("light") signals are never appended to match.signals by main.py,
    # so the list only contains hit frames. Verify finalize copies as-is.
    match = CurrentMatch()
    match.match_id = "1778397800090"
    match.signals = [Signal(fight=8, source="hit", signal_ts=111)]
    storage.create_match_dir(match.match_id)
    storage.write_json_txt(match, result_code=8, video_sync_offset_ms=0)
    data = json.loads((storage.match_dir(match.match_id) / "json.txt").read_text("utf-8"))
    assert data["list"] == [{"timeStamp": 111, "fight": 8}]


def test_temp_result_from_lights():
    assert temp_result_from_lights(True, False) == 8    # only A
    assert temp_result_from_lights(False, True) == 9    # only B
    assert temp_result_from_lights(True, True) == 10    # both → tie
    assert temp_result_from_lights(False, False) == 0   # neither → await AI


def test_remove_ai_subdir(storage_root):
    storage.create_match_dir("7000")
    ai_dir = storage.match_dir("7000") / "ai"
    ai_dir.mkdir()
    (ai_dir / "frame_timestamps.jsonl").write_text("{}", encoding="utf-8")
    storage.remove_ai_subdir("7000")
    assert not ai_dir.exists()
    assert storage.match_dir("7000").exists()  # parent dir untouched


def test_derive_status(storage_root):
    root = storage.matches_root()
    root.mkdir(parents=True, exist_ok=True)

    complete = root / "1000"; complete.mkdir()
    (complete / "seg.mp4").write_bytes(b"x")
    (complete / "json.txt").write_text("{}", encoding="utf-8")

    uploaded = root / "2000"; uploaded.mkdir()
    (uploaded / "json.txt").write_text("{}", encoding="utf-8")

    incomplete = root / "3000"; incomplete.mkdir()
    (incomplete / "seg.mp4").write_bytes(b"x")

    empty = root / "4000"; empty.mkdir()

    assert storage._derive_status(complete) == "complete"
    assert storage._derive_status(uploaded) == "uploaded"
    assert storage._derive_status(incomplete) == "incomplete"
    assert storage._derive_status(empty) is None


def test_match_id_path_traversal_rejected(storage_root):
    for bad in ["../etc", "abc", "12/34", "", "12.5"]:
        with pytest.raises(ValueError):
            storage.match_dir(bad)


def test_list_matches_skips_empty_dirs(storage_root):
    root = storage.matches_root()
    root.mkdir(parents=True, exist_ok=True)
    good = root / "5000"; good.mkdir()
    (good / "json.txt").write_text("{}", encoding="utf-8")
    (root / "6000").mkdir()  # empty → excluded
    items, total = storage.list_matches()
    ids = [i["match_id"] for i in items]
    assert "5000" in ids
    assert "6000" not in ids
    assert total == 1
