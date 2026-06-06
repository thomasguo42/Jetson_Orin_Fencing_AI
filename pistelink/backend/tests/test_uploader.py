"""Unit tests for the SFTP uploader (uploader.py).

A fake asyncssh connection + SFTP client (no network) verifies that files are
streamed, progress is reported, and the three post-upload cleanup policies
behave — including the ai/ subdir removal on delete_video_only (§12.1).
"""

import asyncio
import json

import pytest

import backend.config as config_mod
from backend.config import Config
from backend import storage, uploader
from backend.uploader import Uploader


class _RemoteFile:
    """Async context manager mimicking asyncssh's SFTPClientFile (write side),
    backed by a bytearray so explicit-offset writes (resume) are faithful."""

    def __init__(self, buf):
        self._buf = buf

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def write(self, data, offset=None):
        if offset is None:
            offset = len(self._buf)
        end = offset + len(data)
        if len(self._buf) < end:
            self._buf.extend(b"\0" * (end - len(self._buf)))
        self._buf[offset:end] = data
        return len(data)


class _Attrs:
    def __init__(self, size):
        self.size = size


class FakeSFTPClient:
    def __init__(self):
        self.made_dirs = []
        self.removed_dirs = []
        self.existing_dirs = set()
        self.uploads = {}  # remote_path -> bytearray of file contents
        self.renames = []  # (old, new) pairs

    async def isdir(self, path):
        return path in self.existing_dirs

    async def mkdir(self, path):
        self.made_dirs.append(path)
        self.existing_dirs.add(path)

    async def rmdir(self, path):
        self.removed_dirs.append(path)
        self.existing_dirs.discard(path)

    async def stat(self, path):
        if path not in self.uploads:
            raise FileNotFoundError(path)
        return _Attrs(len(self.uploads[path]))

    def open(self, path, mode="r"):
        buf = self.uploads.get(path)
        if buf is None or "w" in mode:  # wb truncates/creates; r+b keeps content
            buf = bytearray()
            self.uploads[path] = buf
        return _RemoteFile(buf)

    async def posix_rename(self, oldpath, newpath):
        self.renames.append((oldpath, newpath))
        if oldpath in self.uploads:  # commit: .part becomes the final name
            self.uploads[newpath] = self.uploads.pop(oldpath)

    async def remove(self, path):
        self.uploads.pop(path, None)

    async def rename(self, oldpath, newpath):
        await self.posix_rename(oldpath, newpath)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeConn:
    def __init__(self, sftp):
        self._sftp = sftp

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def start_sftp_client(self):
        return self._sftp


@pytest.fixture
def up_env(tmp_path, monkeypatch):
    cfg = Config(str(tmp_path / "none.toml"))      # missing file → DEFAULTS
    cfg._data["storage"]["root"] = str(tmp_path)
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text(
        "1.2.3.4 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeTestKey\n",
        encoding="utf-8",
    )
    cfg._data["upload"].update({
        "host": "1.2.3.4", "port": 22, "username": "u", "password": "p",
        "known_hosts": str(known_hosts), "base_path": "/upload",
        "post_upload_action": "keep_all",
    })
    monkeypatch.setattr(config_mod, "_config", cfg)

    sftp = FakeSFTPClient()
    monkeypatch.setattr(uploader.asyncssh, "connect",
                        lambda *a, **k: FakeConn(sftp))
    return {"cfg": cfg, "sftp": sftp, "root": tmp_path}


def _make_match(mid="1000", with_json=True, with_ai=True):
    d = storage.match_dir(mid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "seg.mp4").write_bytes(b"x" * 100)
    if with_json:
        (d / "json.txt").write_text("{}", encoding="utf-8")
    if with_ai:
        (d / "ai").mkdir()
        (d / "ai" / "frame_timestamps.jsonl").write_text("{}", encoding="utf-8")
    return d


def _run_upload(up_env, mid="1000"):
    progress = []

    async def on_progress(match_id, phase, sent, total, error):
        progress.append((match_id, phase, sent, total, error))

    up = Uploader(on_progress=on_progress)
    asyncio.run(up._upload_one(mid))
    return progress


# ── streaming + progress ──────────────────────────────────────────────────

def test_uploads_video_and_json_with_progress(up_env):
    _make_match()
    progress = _run_upload(up_env)

    sftp = up_env["sftp"]
    assert sftp.made_dirs == ["/upload/1000"]
    assert "/upload/1000/seg.mp4" in sftp.uploads
    assert "/upload/1000/json.txt" in sftp.uploads
    assert len(sftp.uploads["/upload/1000/seg.mp4"]) == 100  # all bytes streamed
    phases = {p[1] for p in progress}
    assert {"connect", "video", "json"} <= phases


# ── atomic upload (.part + rename) ─────────────────────────────────────────

def test_uploads_via_part_then_renames(up_env):
    _make_match()
    _run_upload(up_env)
    sftp = up_env["sftp"]
    # committed under the final names, with no .part left behind
    assert "/upload/1000/seg.mp4" in sftp.uploads
    assert "/upload/1000/json.txt" in sftp.uploads
    assert all(not p.endswith(".part") for p in sftp.uploads)
    # each file was streamed to <name>.part first, then renamed into place
    assert ("/upload/1000/seg.mp4.part", "/upload/1000/seg.mp4") in sftp.renames
    assert ("/upload/1000/json.txt.part", "/upload/1000/json.txt") in sftp.renames


def test_resume_continues_from_existing_part(up_env):
    d = _make_match(mid="1000")  # local seg.mp4 is 100 bytes
    sftp = up_env["sftp"]
    sftp.uploads["/upload/1000/seg.mp4.part"] = bytearray(40)  # interrupted attempt

    progress = []

    async def on_progress(*a):
        progress.append(a)

    up = Uploader(on_progress=on_progress)
    asyncio.run(up._upload_file(sftp, "1000", "video",
                                d / "seg.mp4", "/upload/1000/seg.mp4"))

    # committed full file, renamed into place
    assert len(sftp.uploads["/upload/1000/seg.mp4"]) == 100
    assert ("/upload/1000/seg.mp4.part", "/upload/1000/seg.mp4") in sftp.renames
    # progress started at the resumed offset (40), proving only 60 bytes re-sent
    assert progress[0][2] == 40 and progress[0][3] == 100


def test_resume_discards_oversized_part(up_env):
    d = _make_match(mid="1000")  # local seg.mp4 is 100 bytes
    sftp = up_env["sftp"]
    sftp.uploads["/upload/1000/seg.mp4.part"] = bytearray(500)  # stale, > local

    up = Uploader()
    asyncio.run(up._upload_file(sftp, "1000", "video",
                                d / "seg.mp4", "/upload/1000/seg.mp4"))

    # oversized .part dropped → fresh full upload of exactly 100 bytes
    assert len(sftp.uploads["/upload/1000/seg.mp4"]) == 100


# ── persistent queue / auto-resume (survives restart) ──────────────────────

def test_enqueue_persists_and_restore_requeues(up_env):
    _make_match(mid="1000")
    Uploader().enqueue("1000")
    pending = up_env["root"] / "upload_pending.json"
    assert pending.exists()
    assert json.loads(pending.read_text(encoding="utf-8")) == ["1000"]

    # a fresh uploader (simulating a restart) restores it into its queue
    up2 = Uploader()
    asyncio.run(up2.restore_pending())
    assert up2._queue.get_nowait() == "1000"


def test_restore_prunes_ids_without_dir(up_env):
    (up_env["root"] / "upload_pending.json").write_text(
        json.dumps(["9999"]), encoding="utf-8")  # no such match dir
    up = Uploader()
    asyncio.run(up.restore_pending())
    assert up._queue.empty()
    assert json.loads(
        (up_env["root"] / "upload_pending.json").read_text(encoding="utf-8")) == []


def test_successful_upload_clears_pending(up_env):
    _make_match(mid="1000")
    up = Uploader()
    up.enqueue("1000")
    asyncio.run(up._upload_one("1000"))
    assert json.loads(
        (up_env["root"] / "upload_pending.json").read_text(encoding="utf-8")) == []


def test_failed_upload_keeps_pending_for_retry(up_env, monkeypatch):
    _make_match(mid="1000")

    def boom(*a, **k):
        raise OSError("network down mid-transfer")
    monkeypatch.setattr(uploader.asyncssh, "connect", boom)

    up = Uploader()
    up.enqueue("1000")
    asyncio.run(up._upload_one("1000"))
    # kept so the next startup resumes it
    assert json.loads(
        (up_env["root"] / "upload_pending.json").read_text(encoding="utf-8")) == ["1000"]


def test_cannot_start_clears_pending(up_env):
    # A request that can't even begin (here: host not configured) is dropped from
    # the retry set — only interrupted in-flight transfers are kept for resume.
    up_env["cfg"]._data["upload"]["host"] = ""
    _make_match(mid="1000")
    up = Uploader()
    up.enqueue("1000")
    asyncio.run(up._upload_one("1000"))
    assert json.loads(
        (up_env["root"] / "upload_pending.json").read_text(encoding="utf-8")) == []


def test_cancel_drops_pending(up_env):
    _make_match(mid="1000")
    up = Uploader()
    up.enqueue("1000")
    up.cancel("1000")
    assert json.loads(
        (up_env["root"] / "upload_pending.json").read_text(encoding="utf-8")) == []


# ── cleanup policies ──────────────────────────────────────────────────────

def test_delete_video_only_removes_video_and_ai_keeps_json(up_env):
    up_env["cfg"]._data["upload"]["post_upload_action"] = "delete_video_only"
    d = _make_match()
    _run_upload(up_env)
    assert not (d / "seg.mp4").exists()
    assert not (d / "ai").exists()       # §12.1: ai/ cleaned with the video
    assert (d / "json.txt").exists()


def test_delete_all_removes_dir(up_env):
    up_env["cfg"]._data["upload"]["post_upload_action"] = "delete_all"
    d = _make_match()
    _run_upload(up_env)
    assert not d.exists()


def test_keep_all_keeps_everything(up_env):
    up_env["cfg"]._data["upload"]["post_upload_action"] = "keep_all"
    d = _make_match()
    _run_upload(up_env)
    assert (d / "seg.mp4").exists()
    assert (d / "json.txt").exists()
    assert (d / "ai").exists()


# ── guard rails ───────────────────────────────────────────────────────────

def test_skip_when_host_not_configured(up_env):
    up_env["cfg"]._data["upload"]["host"] = ""
    _make_match()
    progress = _run_upload(up_env)
    assert up_env["sftp"].uploads == {}   # nothing transferred
    assert progress == []                 # no connect notification


def test_skip_when_json_missing(up_env):
    _make_match(with_json=False)
    _run_upload(up_env)
    assert up_env["sftp"].uploads == {}   # json.txt required before upload


# ── auth kwargs (public-key vs password) ──────────────────────────────────

def _known_hosts_file(tmp_path):
    path = tmp_path / "known_hosts"
    path.write_text(
        "upload.example.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeTestKey\n",
        encoding="utf-8",
    )
    return path


def test_connect_kwargs_uses_private_key_when_set(tmp_path):
    known_hosts = _known_hosts_file(tmp_path)
    kw = uploader._connect_kwargs({
        "port": 52182, "username": "video_upload",
        "private_key": "/etc/pistelink/id_upload", "key_passphrase": "secret",
        "password": "ignored",
        "known_hosts": str(known_hosts),
    })
    assert kw["client_keys"] == ["/etc/pistelink/id_upload"]
    assert kw["passphrase"] == "secret"
    assert kw["known_hosts"] == str(known_hosts)
    assert "password" not in kw          # key auth wins; password not sent


def test_connect_kwargs_falls_back_to_password(tmp_path):
    known_hosts = _known_hosts_file(tmp_path)
    kw = uploader._connect_kwargs({
        "username": "u", "password": "p", "private_key": "",
        "known_hosts": str(known_hosts),
    })
    assert kw["password"] == "p"
    assert kw["known_hosts"] == str(known_hosts)
    assert "client_keys" not in kw


def test_connect_kwargs_no_passphrase_omitted(tmp_path):
    known_hosts = _known_hosts_file(tmp_path)
    kw = uploader._connect_kwargs({
        "private_key": "/k", "key_passphrase": "",
        "known_hosts": str(known_hosts),
    })
    assert kw["client_keys"] == ["/k"]
    assert "passphrase" not in kw


def test_connect_kwargs_requires_known_hosts():
    with pytest.raises(ValueError, match="known_hosts"):
        uploader._connect_kwargs({
            "username": "u", "password": "p", "private_key": "",
        })


def test_connect_kwargs_requires_existing_known_hosts(tmp_path):
    missing = tmp_path / "missing_known_hosts"
    with pytest.raises(FileNotFoundError, match="known_hosts"):
        uploader._connect_kwargs({
            "username": "u", "password": "p", "private_key": "",
            "known_hosts": str(missing),
        })


# ── test_connection (FR-6.4) ──────────────────────────────────────────────

def test_test_connection_no_host(up_env):
    up_env["cfg"]._data["upload"]["host"] = ""
    result = asyncio.run(Uploader.test_connection())
    assert result == {"ok": False, "error": "host not configured"}


def test_test_connection_success(up_env):
    result = asyncio.run(Uploader.test_connection())
    assert result == {"ok": True, "error": None}
    # probes base_path with a throwaway dir, then removes it
    assert "/upload/test" in up_env["sftp"].made_dirs
    assert "/upload/test" in up_env["sftp"].removed_dirs
