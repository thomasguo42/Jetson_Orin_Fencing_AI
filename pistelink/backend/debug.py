"""Debug endpoints: simulate MCU/AI events without real hardware.

Enabled only when PISTELINK_DEBUG=1 is set. Call the same callbacks used by
serial_io and ai_io, so the full business logic chain is exercised.
"""

import time

from fastapi import APIRouter

router = APIRouter(prefix="/api/debug", tags=["debug"])


@router.post("/match_start")
async def debug_match_start(body: dict = None):
    from .main import on_main_frame
    body = body or {}
    weapon = body.get("weapon", 1)
    sensor = body.get("sensor", 0)
    recv_ts = int(time.time() * 1000)
    frame = {"data": bytes([weapon, sensor])}
    await on_main_frame(0x50, frame, recv_ts)
    return {"ok": True}


@router.post("/match_cancel")
async def debug_match_cancel():
    from .main import on_main_frame
    recv_ts = int(time.time() * 1000)
    await on_main_frame(0x51, {}, recv_ts)
    return {"ok": True}


@router.post("/hit")
async def debug_hit(body: dict = None):
    from .main import on_hit_frame
    body = body or {}
    digit = body.get("digit", 8)
    recv_ts = int(time.time() * 1000)
    await on_hit_frame(digit, recv_ts)
    return {"ok": True}


@router.post("/light")
async def debug_light(body: dict = None):
    """Simulate 0x52 round-end. body: {"a": <byte>, "b": <byte>} where each
    byte is 0x00=valid score (light on) / 0x01 guard / 0x02 invalid / 0x03 none.
    Defaults to A scores (a=0), B nothing (b=3)."""
    from .main import on_main_frame
    body = body or {}
    a = body.get("a", 0x00)
    b = body.get("b", 0x03)
    recv_ts = int(time.time() * 1000)
    frame = {"data": bytes([a, b])}
    await on_main_frame(0x52, frame, recv_ts)
    return {"ok": True}


@router.post("/camera_ready")
async def debug_camera_ready():
    from .main import on_ai_event
    await on_ai_event("camera_ready", {}, "")
    return {"ok": True}


@router.post("/camera_error")
async def debug_camera_error(body: dict = None):
    from .main import on_ai_event
    body = body or {}
    payload = {"code": body.get("code", "E_CAMERA_INIT"),
               "reason": body.get("reason", "debug trigger")}
    await on_ai_event("camera_error", payload, "")
    return {"ok": True}


@router.post("/match_result")
async def debug_match_result(body: dict = None):
    from .main import on_ai_event
    body = body or {}
    payload = {
        "winner": body.get("winner", "A"),
        "result_code": body.get("result_code", 0),
        "video_path": body.get("video_path", ""),
    }
    await on_ai_event("match_result", payload, "")
    return {"ok": True}
