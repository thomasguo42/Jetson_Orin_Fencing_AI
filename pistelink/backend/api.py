"""FastAPI app: REST endpoints, WebSocket push, static file serving."""

import asyncio
import os
import time
from collections import deque
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import get_config
from .storage import list_matches, get_match, remove_match_dir, disk_usage

app = FastAPI(
    title="PisteLink",
    description="PisteLink 击剑赛事记录系统 — 后端 API",
    version="0.1.0",
)

# Shared state — set by main.py before startup
state: dict = {}

# WS connected clients
_ws_clients: set[WebSocket] = set()

# Rolling log buffer for diagnostics panel
_log_buffer: deque = deque(maxlen=200)

# Rolling signal buffer for live match view
_signal_buffer: deque = deque(maxlen=50)


# ── Static files (frontend) ──────────────────────────────────────────

_frontend_dist = Path(os.environ.get("PISTELINK_FRONTEND_DIR", "frontend/dist"))
if _frontend_dist.exists():
    app.mount("/assets", StaticFiles(directory=_frontend_dist / "assets"), name="assets")


@app.get("/")
async def index():
    index_html = _frontend_dist / "index.html"
    if index_html.exists():
        return FileResponse(index_html)
    return JSONResponse({"ok": True, "version": "0.1.0"})


# ── Health ────────────────────────────────────────────────────────────

@app.get("/healthz")
async def healthz():
    config = get_config()
    storage_free = 0
    try:
        storage_free = disk_usage()["free_mb"]
    except Exception:
        pass

    serial_ok = bool(state.get("serial") and state["serial"].connected)
    ai_enabled = bool(config.get("ai", "enabled", True))
    ai_ok = bool(state.get("ai") and state["ai"].connected)

    return {
        "serial": "ok" if serial_ok else "error",
        "ai": "disabled" if not ai_enabled else ("ok" if ai_ok else "error"),
        "storage_free_mb": storage_free,
    }


# ── Status ────────────────────────────────────────────────────────────

@app.get("/api/status")
async def api_status():
    config = get_config()
    match = state.get("current_match")
    serial_state = state.get("serial")
    ai_state = state.get("ai")
    uploader = state.get("uploader")

    return {
        "match": {
            "state": match.state.value if match else "idle",
            "match_id": match.match_id if match else "",
            "weapon": match.weapon if match else 0,
            "sensor": match.sensor if match else 0,
            "signal_count": len(match.signals) if match else 0,
        },
        "serial": {
            "device": get_config().get("serial", "device"),
            "connected": serial_state.connected if serial_state else False,
            "reader_running": serial_state.running if serial_state else False,
            "last_frame_time": serial_state.last_frame_time if serial_state else 0,
            "crc_errors": serial_state.crc_errors if serial_state else 0,
            "connection_errors": serial_state.connection_errors if serial_state else 0,
            "dup_discarded": serial_state.dup_discarded if serial_state else 0,
            "last_error": serial_state.last_error if serial_state else "",
        },
        "ai": {
            "enabled": bool(config.get("ai", "enabled", True)),
            "connected": ai_state.connected if ai_state else False,
            "last_recv_time": ai_state.last_recv_time if ai_state else 0,
            "bytes_sent": ai_state.bytes_sent if ai_state else 0,
            "bytes_recv": ai_state.bytes_recv if ai_state else 0,
        },
        "upload": {
            "current": uploader.current_match_id if uploader else None,
        },
        "storage": disk_usage(),
        "recent_signals": list(_signal_buffer),
    }


# ── Matches ───────────────────────────────────────────────────────────

@app.get("/api/matches")
async def api_matches(page: int = 1, per_page: int = 50):
    items, total = list_matches(page, per_page)
    # Overlay uploader state
    uploader = state.get("uploader")
    current_upload = uploader.current_match_id if uploader else None
    for item in items:
        if item["match_id"] == current_upload:
            item["status"] = "uploading"
    return {"items": items, "total": total, "page": page, "per_page": per_page}


@app.post("/api/matches/{match_id}/upload")
async def api_upload(match_id: str):
    match = get_match(match_id)
    if match is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    uploader = state.get("uploader")
    if uploader is None:
        return JSONResponse({"error": "uploader not ready"}, status_code=503)
    uploader.enqueue(match_id)
    return {"ok": True, "match_id": match_id}


@app.delete("/api/matches/{match_id}")
async def api_delete(match_id: str):
    match = get_match(match_id)
    if match is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    uploader = state.get("uploader")
    if uploader is not None:
        uploader.cancel(match_id)  # don't retry a deleted match on next startup
    remove_match_dir(match_id)
    return {"ok": True}


# ── Config ────────────────────────────────────────────────────────────

# Upload secrets are never echoed to the client. GET masks them to "" and PUT
# treats a blank value as "leave unchanged" — so a blank field on save keeps
# the stored secret, while a non-blank one updates it.
_UPLOAD_SECRET_KEYS = ("password", "key_passphrase")


@app.get("/api/config")
async def api_config_get():
    config = get_config()
    data = config.to_dict()  # fresh per-section copies; safe to mutate
    upload = data.get("upload")
    if isinstance(upload, dict):
        for key in _UPLOAD_SECRET_KEYS:
            if upload.get(key):
                upload[key] = ""
    return {"config": data}


@app.put("/api/config")
async def api_config_put(body: dict):
    config = get_config()
    updates = {k: v for k, v in body.items() if isinstance(v, dict)}
    upload = updates.get("upload")
    if isinstance(upload, dict):
        for key in _UPLOAD_SECRET_KEYS:
            if key in upload and not upload[key]:
                del upload[key]  # blank → keep the stored secret
    if updates:
        config.batch_update_and_write(updates)
    return {"ok": True}


# ── SFTP test connection (FR-6.4) ─────────────────────────────────────

@app.post("/api/upload/test")
async def api_upload_test():
    from .uploader import Uploader
    return await Uploader.test_connection()


# ── WebSocket ─────────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    _ws_clients.add(ws)

    # Send recent logs on connect
    for entry in list(_log_buffer):
        try:
            await ws.send_json(entry)
        except Exception:
            break

    try:
        while True:
            # Keep-alive: wait for client messages (we don't expect any)
            try:
                data = await asyncio.wait_for(ws.receive_text(), timeout=30)
                # Client can send ping, we ignore
            except asyncio.TimeoutError:
                await ws.send_json({"type": "ping"})
    except WebSocketDisconnect:
        pass
    finally:
        _ws_clients.discard(ws)


# ── Push helpers (called by other modules) ────────────────────────────

async def ws_push(event: dict):
    """Push an event to all connected WebSocket clients."""
    dead = set()
    for ws in _ws_clients:
        try:
            await ws.send_json(event)
        except Exception:
            dead.add(ws)
    _ws_clients.difference_update(dead)


def ws_push_sync(event: dict):
    """Schedule a WS push from a non-async context."""
    asyncio.ensure_future(ws_push(event))


def add_log(level: str, message: str):
    """Append to rolling log buffer and push via WS."""
    entry = {
        "type": "log_line",
        "ts": int(time.time() * 1000),
        "level": level,
        "message": message,
    }
    _log_buffer.append(entry)
    asyncio.ensure_future(ws_push(entry))


def add_signal_sync(signal_dict: dict):
    """Add signal to rolling buffer and push via WS."""
    _signal_buffer.append(signal_dict)
    asyncio.ensure_future(ws_push({"type": "signal", **signal_dict}))


def clear_signal_buffer():
    """Drop all buffered signals so recent_signals stays scoped to one match.

    The buffer is global and feeds /api/status's recent_signals (which seeds the
    live view and the client's score recompute). Cleared at match start, so a new
    match never hands back the previous match's hits and re-inflates the score.
    """
    _signal_buffer.clear()
