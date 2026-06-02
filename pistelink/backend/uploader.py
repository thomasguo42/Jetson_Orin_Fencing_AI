"""SFTP upload via asyncssh — serial FIFO queue, one session per upload."""

import asyncio
import json
import logging
import os
from pathlib import Path

import asyncssh

from .config import get_config
from .storage import match_dir, JSON_FILENAME, remove_match_dir, remove_ai_subdir

logger = logging.getLogger(__name__)

UPLOAD_BLOCK_BYTES = 64 * 1024  # FR-5.4: stream in 64 KiB blocks


def _connect_kwargs(section: dict) -> dict:
    """Build asyncssh.connect() kwargs from the [upload] config section.

    Host-key checking is disabled (known_hosts=None): this is an appliance that
    uploads to an operator-configured server, with no known_hosts database to
    manage. Authentication uses an SSH private key when `private_key` is set
    (public-key auth, the client's setup), otherwise falls back to password.
    """
    kwargs = dict(
        port=section.get("port", 22),
        username=section.get("username", ""),
        known_hosts=None,
        connect_timeout=section.get("timeout_s", 60),
    )
    private_key = section.get("private_key", "")
    if private_key:
        kwargs["client_keys"] = [private_key]
        passphrase = section.get("key_passphrase", "")
        if passphrase:
            kwargs["passphrase"] = passphrase
    else:
        # No key configured → password auth. (asyncssh would otherwise probe
        # ~/.ssh defaults, which don't exist on the appliance.)
        kwargs["password"] = section.get("password", "")
    return kwargs


class Uploader:
    def __init__(self, on_progress=None):
        self._on_progress = on_progress  # async callable(match_id, phase, bytes_sent, bytes_total, error)
        self._queue: asyncio.Queue = asyncio.Queue()
        self._running = False
        self._current: str | None = None  # match_id being uploaded (or None)
        # Persistent set of match_ids requested but not yet confirmed uploaded;
        # mirrored to <storage_root>/upload_pending.json so an upload interrupted
        # by a crash / power loss resumes on the next startup (restore_pending).
        self._pending: set[str] = set()

    @property
    def current_match_id(self) -> str | None:
        return self._current

    def _pending_path(self) -> Path:
        return Path(get_config().get("storage", "root")) / "upload_pending.json"

    def _persist_pending(self) -> None:
        path = self._pending_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.parent / (path.name + ".tmp")
            tmp.write_text(json.dumps(sorted(self._pending)), encoding="utf-8")
            os.replace(tmp, path)  # atomic; never a half-written queue file
        except OSError as e:
            logger.warning("Could not persist upload queue: %s", e)

    def _discard_pending(self, match_id: str) -> None:
        if match_id in self._pending:
            self._pending.discard(match_id)
            self._persist_pending()

    def enqueue(self, match_id: str):
        """Queue a match for upload and persist the request so it survives a
        restart until the upload is confirmed complete (or the match is deleted)."""
        if match_id not in self._pending:
            self._pending.add(match_id)
            self._persist_pending()
        self._queue.put_nowait(match_id)

    def cancel(self, match_id: str) -> None:
        """Drop a match from the persistent retry set — call when the match is
        deleted so a pending/failed upload isn't retried on the next startup."""
        self._discard_pending(match_id)

    async def restore_pending(self) -> None:
        """Re-enqueue uploads that were requested but never confirmed complete,
        so an upload interrupted by power loss resumes automatically on startup.
        Stale ids (match dir gone) are pruned from the persisted set."""
        try:
            ids = json.loads(self._pending_path().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(ids, list):
            return
        valid = []
        for mid in ids:
            mid = str(mid)
            try:
                if match_dir(mid).exists():
                    valid.append(mid)
            except ValueError:
                pass  # not a valid match_id → drop
        self._pending = set(valid)
        self._persist_pending()
        for mid in valid:
            self._queue.put_nowait(mid)
        if valid:
            logger.info("Restored %d pending upload(s): %s", len(valid), valid)

    async def run(self):
        self._running = True
        while self._running:
            try:
                match_id = await self._queue.get()
            except RuntimeError:
                break
            if match_id is None:
                break
            self._current = match_id
            await self._upload_one(match_id)
            self._current = None

    async def _upload_one(self, match_id: str):
        config = get_config()
        config.reload_if_stale()
        section = config.get_section("upload")

        host = section.get("host", "")
        if not host:
            logger.warning("SFTP host not configured, skipping upload")
            self._discard_pending(match_id)  # can't start; not a resumable attempt
            return

        port = section.get("port", 22)
        base_path = section.get("base_path", "/")
        post_action = section.get("post_upload_action", "delete_video_only")

        d = match_dir(match_id)
        if not d.exists():
            logger.warning("Match dir missing: %s", match_id)
            self._discard_pending(match_id)  # nothing to upload; stop retrying
            return

        # Find video file
        mp4_files = list(d.glob("*.mp4"))
        json_path = d / JSON_FILENAME
        if not json_path.exists():
            logger.warning("json.txt missing for %s, cannot upload", match_id)
            self._discard_pending(match_id)  # cannot upload; stop retrying
            return

        logger.info("Uploading %s to %s:%d", match_id, host, port)
        await self._notify(match_id, "connect", 0, 0, None)

        try:
            async with asyncssh.connect(host, **_connect_kwargs(section)) as conn:
                async with conn.start_sftp_client() as sftp:
                    remote_dir = f"{base_path.rstrip('/')}/{match_id}"

                    # FR-5.4: create only this one match-level dir; base_path must
                    # be pre-provisioned by the SFTP admin. Skip if it exists.
                    if not await sftp.isdir(remote_dir):
                        await sftp.mkdir(remote_dir)

                    # Upload video files (streamed, per-byte progress)
                    for mp4 in mp4_files:
                        await self._upload_file(sftp, match_id, "video",
                                                mp4, f"{remote_dir}/{mp4.name}")

                    # Upload json.txt
                    await self._upload_file(sftp, match_id, "json",
                                            json_path, f"{remote_dir}/{JSON_FILENAME}")

            logger.info("Upload complete: %s", match_id)

            # Post-upload cleanup
            if post_action == "delete_all":
                remove_match_dir(match_id)
            elif post_action == "delete_video_only":
                for mp4 in mp4_files:
                    mp4.unlink(missing_ok=True)
                remove_ai_subdir(match_id)  # §12.1: clean ai/ with the video
                logger.info("Video deleted for %s (keep json)", match_id)
            # keep_all: do nothing

            # Confirmed complete: drop from the persistent retry set.
            self._discard_pending(match_id)

        except Exception as e:
            # Keep it in the persistent set so the upload is retried on the next
            # startup (covers power loss / network drop mid-transfer); the UI also
            # shows it failed for an immediate manual retry.
            logger.error("Upload failed for %s: %s", match_id, e)
            await self._notify(match_id, "error", 0, 0, str(e))

    async def _upload_file(self, sftp, match_id, phase, local_path, remote_path):
        """Stream one file in 64 KiB blocks to a <name>.part temp, then rename it
        into place (FR-5.5). Two layers of crash safety:

        * Resume: if a .part from an interrupted attempt is already on the server,
          continue from its end (each block is written at an explicit offset)
          instead of re-sending the whole file — matters for large videos.
        * Atomic commit: the rename is the commit point, so a transfer cut short
          by power loss leaves only <name>.part, never a truncated file under the
          real name that downstream tools might treat as complete.
        """
        total = local_path.stat().st_size
        tmp_path = remote_path + ".part"
        offset = await self._resume_offset(sftp, tmp_path, total)

        sent = offset
        await self._notify(match_id, phase, sent, total, None)
        with open(local_path, "rb") as f:
            f.seek(offset)
            # r+b resumes an existing .part without truncating; wb starts fresh.
            async with sftp.open(tmp_path, "r+b" if offset else "wb") as remote:
                pos = offset
                while True:
                    block = f.read(UPLOAD_BLOCK_BYTES)
                    if not block:
                        break
                    await remote.write(block, pos)  # explicit offset (resume-safe)
                    pos += len(block)
                    sent = pos
                    await self._notify(match_id, phase, sent, total, None)
        await self._commit_remote(sftp, tmp_path, remote_path)

    @staticmethod
    async def _resume_offset(sftp, tmp_path, total) -> int:
        """Bytes already uploaded to tmp_path by a prior interrupted attempt.
        Returns a resume offset in (0, total]; 0 means start fresh. A .part that
        is larger than the local file (stale / mismatched) is removed so we
        re-upload cleanly."""
        try:
            existing = (await sftp.stat(tmp_path)).size or 0
        except (OSError, asyncssh.Error):
            return 0  # no .part yet
        if 0 < existing <= total:
            return existing
        if existing > total:
            try:
                await sftp.remove(tmp_path)
            except (OSError, asyncssh.Error):
                pass
        return 0

    @staticmethod
    async def _commit_remote(sftp, tmp_path, final_path):
        """Atomically move the uploaded .part onto its final name. Prefer the
        POSIX rename extension (atomic overwrite); fall back to remove+rename for
        servers without it."""
        try:
            await sftp.posix_rename(tmp_path, final_path)
        except (OSError, asyncssh.Error):
            try:
                await sftp.remove(final_path)
            except (OSError, asyncssh.Error):
                pass
            await sftp.rename(tmp_path, final_path)

    async def _notify(self, match_id, phase, sent, total, error):
        if self._on_progress:
            await self._on_progress(match_id, phase, sent, total, error)

    @staticmethod
    async def test_connection() -> dict:
        """Validate SFTP credentials without transferring match files (FR-6.4).

        Connects, opens an SFTP session, then creates and removes a 'test/'
        directory under base_path. Returns {"ok": bool, "error": str|None}.
        """
        config = get_config()
        config.reload_if_stale()
        s = config.get_section("upload")
        host = s.get("host", "")
        if not host:
            return {"ok": False, "error": "host not configured"}
        try:
            async with asyncssh.connect(host, **_connect_kwargs(s)) as conn:
                async with conn.start_sftp_client() as sftp:
                    probe = f"{s.get('base_path', '/').rstrip('/')}/test"
                    try:
                        await sftp.mkdir(probe)
                        await sftp.rmdir(probe)
                    except (OSError, asyncssh.Error) as e:
                        # base_path may be read-only; connection itself still proved good.
                        logger.info("SFTP test probe dir not writable: %s", e)
            return {"ok": True, "error": None}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    async def stop(self):
        self._running = False
        self._queue.put_nowait(None)
