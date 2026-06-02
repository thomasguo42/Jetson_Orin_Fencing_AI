"""Match directory management, json.txt atomic write, disk space query."""

import json
import logging
import os
import shutil
from pathlib import Path

from .config import get_config
from .models import CurrentMatch

logger = logging.getLogger(__name__)

JSON_FILENAME = "json.txt"


def matches_root() -> Path:
    return Path(get_config().get("storage", "root")) / "matches"


def _validate_match_id(match_id: str):
    """match_id must be a pure-digit timestamp string to prevent path traversal."""
    if not match_id or not match_id.isdigit():
        raise ValueError(f"Invalid match_id: {match_id!r}")


def match_dir(match_id: str) -> Path:
    _validate_match_id(match_id)
    return matches_root() / match_id


def create_match_dir(match_id: str):
    d = match_dir(match_id)
    d.mkdir(parents=True, exist_ok=True)
    logger.info("Match dir created: %s", d)


def remove_match_dir(match_id: str):
    d = match_dir(match_id)
    if d.exists():
        shutil.rmtree(d)
        logger.info("Match dir removed: %s", d)


def remove_ai_subdir(match_id: str):
    """Remove the AI intermediate-products subdir (matches/<id>/ai/).

    Per protocol §12.1 the ai/ subdir is never uploaded and must be cleaned up
    together with the video on delete_video_only / delete_all.
    """
    d = match_dir(match_id) / "ai"
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
        logger.info("AI subdir removed: %s", d)


def write_json_txt(match: CurrentMatch, result_code: int, video_sync_offset_ms: int):
    """Write json.txt atomically (idempotent — called for the 0x52 temp result,
    the AI-result backfill, and the timeout finalize; see §13 先写后改)."""
    match_dir(match.match_id).mkdir(parents=True, exist_ok=True)
    data = {
        "beginTimeStamp": match.begin_ts,
        "voiceEndTime": match.voice_end_ts,
        "list": [
            {"timeStamp": s.signal_ts, "fight": s.fight}
            for s in match.signals
        ],
        "result": result_code,
        "video_sync_offset_ms": video_sync_offset_ms,
    }
    content = json.dumps(data, ensure_ascii=False, separators=(",", ":"))

    d = match_dir(match.match_id)
    tmp = d / f".{JSON_FILENAME}.tmp"
    dest = d / JSON_FILENAME
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, dest)
    logger.info("json.txt finalized: %s (result=%d)", dest, result_code)


def disk_usage() -> dict:
    """Return storage usage info."""
    root = matches_root()
    if not hasattr(os, "statvfs"):
        # Windows dev host: statvfs is POSIX-only. Production runs on Jetson.
        return {"total_mb": 0, "free_mb": 0, "used_mb": 0}
    try:
        stat = os.statvfs(root)
        total = stat.f_frsize * stat.f_blocks
        free = stat.f_frsize * stat.f_bavail
        used = total - free
        return {
            "total_mb": round(total / (1024 * 1024), 1),
            "free_mb": round(free / (1024 * 1024), 1),
            "used_mb": round(used / (1024 * 1024), 1),
        }
    except OSError:
        return {"total_mb": 0, "free_mb": 0, "used_mb": 0}


def _derive_status(dir_path: Path) -> str | None:
    """Derive match status from directory contents (FR-6.2)."""
    has_mp4 = any(dir_path.glob("*.mp4"))
    has_json = (dir_path / JSON_FILENAME).exists()
    if has_mp4 and has_json:
        return "complete"
    if not has_mp4 and has_json:
        return "uploaded"
    if has_mp4 and not has_json:
        return "incomplete"
    return None


def list_matches(page: int = 1, per_page: int = 50) -> tuple[list[dict], int]:
    """List match directories with status derived from contents (FR-6.2)."""
    root = matches_root()
    items = []
    try:
        for entry in sorted(root.iterdir(), reverse=True):
            if not entry.is_dir():
                continue
            mid = entry.name
            status = _derive_status(entry)
            if status is None:
                continue

            stat = entry.stat()
            items.append({
                "match_id": mid,
                "status": status,
                "video_size_mb": round(
                    sum((f.stat().st_size for f in entry.glob("*.mp4")), 0)
                    / (1024 * 1024), 1
                ),
                "created_at": int(stat.st_ctime * 1000),
            })
    except OSError:
        pass

    total = len(items)
    start = (page - 1) * per_page
    return items[start : start + per_page], total


def get_match(match_id: str) -> dict | None:
    """Get single match info."""
    d = match_dir(match_id)
    if not d.exists():
        return None
    status = _derive_status(d)
    if status is None:
        return None

    video_size = sum((f.stat().st_size for f in d.glob("*.mp4")), 0)
    return {
        "match_id": match_id,
        "status": status,
        "video_size_mb": round(video_size / (1024 * 1024), 1),
        "created_at": int(d.stat().st_ctime * 1000),
    }
