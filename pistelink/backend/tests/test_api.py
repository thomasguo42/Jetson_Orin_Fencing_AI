"""Unit tests for the REST endpoints (api.py).

The endpoint coroutines are called directly (no httpx/TestClient dependency),
which exercises their logic and boundaries — not FastAPI's routing/serialization.
Shared state and config are injected via fixtures.
"""

import asyncio
import json

import pytest

import backend.config as config_mod
from backend.config import Config
from backend import api, storage
from backend.models import CurrentMatch


class FakeSerial:
    running = True
    last_frame_time = 0
    crc_errors = 0
    connection_errors = 0
    dup_discarded = 0


class FakeAI:
    connected = True
    last_recv_time = 0
    bytes_sent = 0
    bytes_recv = 0


class FakeUploader:
    def __init__(self):
        self.current_match_id = None
        self.enqueued = []
        self.cancelled = []

    def enqueue(self, match_id):
        self.enqueued.append(match_id)

    def cancel(self, match_id):
        self.cancelled.append(match_id)


@pytest.fixture
def app_env(tmp_path, monkeypatch):
    cfg = Config(str(tmp_path / "config.toml"))   # written on PUT
    cfg._data["storage"]["root"] = str(tmp_path)
    monkeypatch.setattr(config_mod, "_config", cfg)

    uploader = FakeUploader()
    state = {
        "current_match": CurrentMatch(),
        "serial": FakeSerial(),
        "ai": FakeAI(),
        "uploader": uploader,
    }
    monkeypatch.setattr(api, "state", state)
    return {"cfg": cfg, "root": tmp_path, "state": state, "uploader": uploader}


def _make_match(mid, *, mp4=True, js=True):
    d = storage.match_dir(mid)
    d.mkdir(parents=True, exist_ok=True)
    if mp4:
        (d / "seg.mp4").write_bytes(b"x")
    if js:
        (d / "json.txt").write_text("{}", encoding="utf-8")
    return d


def _body(resp):
    return json.loads(resp.body)


# ── status / health ───────────────────────────────────────────────────────

def test_status_shape(app_env):
    out = asyncio.run(api.api_status())
    assert out["match"]["state"] == "idle"
    assert set(out) >= {"match", "serial", "ai", "upload", "storage", "recent_signals"}


def test_clear_signal_buffer_scopes_recent_signals(app_env):
    # Previous match's hits sit in the global buffer …
    api._signal_buffer.append({"ts": 1, "fight": 8, "source": "hit", "match_id": "old"})
    api._signal_buffer.append({"ts": 2, "fight": 9, "source": "hit", "match_id": "old"})
    assert len(asyncio.run(api.api_status())["recent_signals"]) == 2

    # … a new match (0x50) must wipe them so recent_signals can't re-inflate score.
    api.clear_signal_buffer()
    assert asyncio.run(api.api_status())["recent_signals"] == []


def test_healthz_reflects_connections(app_env):
    out = asyncio.run(api.healthz())
    assert out["serial"] == "ok" and out["ai"] == "ok"

    app_env["state"]["serial"].running = False
    app_env["state"]["ai"].connected = False
    out = asyncio.run(api.healthz())
    assert out["serial"] == "error" and out["ai"] == "error"


# ── matches list ──────────────────────────────────────────────────────────

def test_matches_lists_and_overlays_uploading(app_env):
    _make_match("2000")               # complete (mp4 + json)
    _make_match("1000", mp4=False)    # uploaded (json only)
    app_env["uploader"].current_match_id = "2000"

    out = asyncio.run(api.api_matches())
    by_id = {i["match_id"]: i["status"] for i in out["items"]}
    assert out["total"] == 2
    assert by_id["2000"] == "uploading"   # overlaid from uploader state
    assert by_id["1000"] == "uploaded"


# ── upload endpoint ───────────────────────────────────────────────────────

def test_upload_enqueues_existing_match(app_env):
    _make_match("3000")
    out = asyncio.run(api.api_upload("3000"))
    assert out == {"ok": True, "match_id": "3000"}
    assert app_env["uploader"].enqueued == ["3000"]


def test_upload_unknown_match_404(app_env):
    resp = asyncio.run(api.api_upload("9999"))
    assert resp.status_code == 404
    assert _body(resp) == {"error": "not found"}


def test_upload_503_when_uploader_missing(app_env):
    _make_match("3000")
    app_env["state"]["uploader"] = None
    resp = asyncio.run(api.api_upload("3000"))
    assert resp.status_code == 503


# ── delete endpoint ───────────────────────────────────────────────────────

def test_delete_removes_existing_match(app_env):
    d = _make_match("4000")
    out = asyncio.run(api.api_delete("4000"))
    assert out == {"ok": True}
    assert not d.exists()


def test_delete_unknown_match_404(app_env):
    resp = asyncio.run(api.api_delete("9999"))
    assert resp.status_code == 404


# ── config get / put ──────────────────────────────────────────────────────

def test_config_get_returns_current(app_env):
    out = asyncio.run(api.api_config_get())
    assert out["config"]["storage"]["root"] == str(app_env["root"])


def test_config_put_persists(app_env):
    asyncio.run(api.api_config_put({"signal": {"video_sync_offset_ms": 50}}))
    reloaded = Config(str(app_env["root"] / "config.toml"))
    assert reloaded.video_sync_offset_ms == 50


def test_config_put_ignores_non_dict_values(app_env):
    # only dict sections are applied; scalars are dropped, no crash
    out = asyncio.run(api.api_config_put({"bogus": 123, "signal": {"video_sync_offset_ms": 7}}))
    assert out == {"ok": True}
    assert app_env["cfg"].video_sync_offset_ms == 7


def test_config_get_masks_upload_secrets(app_env):
    app_env["cfg"]._data["upload"].update({"password": "pw", "key_passphrase": "kp"})
    out = asyncio.run(api.api_config_get())
    assert out["config"]["upload"]["password"] == ""
    assert out["config"]["upload"]["key_passphrase"] == ""
    # masking the response must not mutate the stored config
    assert app_env["cfg"].get("upload", "password") == "pw"


def test_config_put_blank_secret_keeps_stored(app_env):
    app_env["cfg"]._data["upload"]["password"] = "stored"
    asyncio.run(api.api_config_put({"upload": {"host": "h", "password": ""}}))
    assert app_env["cfg"].get("upload", "host") == "h"
    assert app_env["cfg"].get("upload", "password") == "stored"  # blank → unchanged


def test_config_put_nonblank_secret_updates(app_env):
    app_env["cfg"]._data["upload"]["password"] = "old"
    asyncio.run(api.api_config_put({"upload": {"password": "new"}}))
    assert app_env["cfg"].get("upload", "password") == "new"


# ── SFTP test connection ──────────────────────────────────────────────────

def test_upload_test_no_host(app_env):
    out = asyncio.run(api.api_upload_test())
    assert out == {"ok": False, "error": "host not configured"}
