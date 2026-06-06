"""PisteLink entry point — wires serial, AI, audio, storage, upload, and API."""

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from .ai_io import AIClient
from .api import app as _api_app, add_log, add_signal_sync, clear_signal_buffer, ws_push_sync, state as api_state
from .audio import AudioPlayer
from .config import get_config
from .models import CurrentMatch, MatchState, Signal, temp_result_from_lights
from .serial_io import SerialReader, parse_light_signals
from .storage import create_match_dir, match_dir, remove_match_dir, write_json_txt
from .uploader import Uploader

logger = logging.getLogger(__name__)

# FastAPI application (routes, WebSocket and static hosting are defined in
# api.py). Re-exported here with an explicit type so it is discoverable as
# `backend.main:app` — both by `uvicorn backend.main:app --reload` and by IDE
# FastAPI run configurations that scan main.py for the application instance.
app: FastAPI = _api_app

match = CurrentMatch()
serial: SerialReader | None = None
ai_client: AIClient | None = None
audio: AudioPlayer | None = None
uploader: Uploader | None = None

_settle_timeout_task: asyncio.Task | None = None
_timed_out_matches: dict[str, tuple[CurrentMatch, int]] = {}
_MAX_TIMED_OUT_BACKFILLS = 16
_REASON_AUDIO_KEY_RE = re.compile(r"^[a-z0-9_]+$")


def _cancel_settle_timeout():
    """Cancel the pending AI-result timeout, if any."""
    global _settle_timeout_task
    if _settle_timeout_task is not None:
        _settle_timeout_task.cancel()
        _settle_timeout_task = None


def _snapshot_match(src: CurrentMatch) -> CurrentMatch:
    snap = CurrentMatch()
    snap.match_id = src.match_id
    snap.state = src.state
    snap.begin_ts = src.begin_ts
    snap.voice_end_ts = src.voice_end_ts
    snap.weapon = src.weapon
    snap.sensor = src.sensor
    snap.signals = [
        Signal(fight=s.fight, source=s.source, signal_ts=s.signal_ts)
        for s in src.signals
    ]
    snap.result_audio_announced = src.result_audio_announced
    snap.result_audio_done = src.result_audio_done
    snap.result_audio_current = src.result_audio_current
    snap.result_audio_queue = list(src.result_audio_queue)
    snap.ai_result_received = src.ai_result_received
    return snap


def _remember_timed_out_match(match_id: str, match_snapshot: CurrentMatch,
                              offset_ms: int):
    if not match_id:
        return
    _timed_out_matches[match_id] = (match_snapshot, offset_ms)
    while len(_timed_out_matches) > _MAX_TIMED_OUT_BACKFILLS:
        oldest = next(iter(_timed_out_matches))
        _timed_out_matches.pop(oldest, None)


def _result_code(payload: dict) -> int:
    try:
        return int(payload.get("result_code", 0))
    except (TypeError, ValueError):
        return 0


def _apply_corrected_signals(target: CurrentMatch, payload: dict) -> int:
    """Apply optional AI-corrected hit timestamps before json.txt backfill.

    The v1.1 protocol allows AI to provide corrected signal timestamps but does
    not prescribe a field name. Keep this intentionally narrow: only explicit
    corrected_* lists are accepted, and entries update by list index.
    """
    corrections = payload.get("corrected_signals")
    if corrections is None:
        corrections = payload.get("corrected_list")
    if not isinstance(corrections, list):
        return 0

    changed = 0
    for entry in corrections:
        if not isinstance(entry, dict):
            continue
        try:
            index = int(entry.get("index"))
        except (TypeError, ValueError):
            continue
        if index < 0 or index >= len(target.signals):
            continue
        ts = entry.get("timeStamp", entry.get("signal_ts"))
        try:
            signal_ts = int(ts)
        except (TypeError, ValueError):
            continue
        target.signals[index].signal_ts = signal_ts
        changed += 1
    return changed


def _video_path_in_match_dir(match_id: str, video_path: str) -> bool:
    try:
        path = Path(video_path).expanduser()
        if not path.is_absolute():
            return False
        resolved_path = path.resolve(strict=False)
        resolved_dir = match_dir(match_id).resolve(strict=False)
        resolved_path.relative_to(resolved_dir)
        return True
    except (OSError, ValueError):
        return False


def _validate_match_result_video_path(match_id: str, payload: dict):
    video_path = str(payload.get("video_path") or "")
    if video_path and not _video_path_in_match_dir(match_id, video_path):
        add_log("warning",
                f"E_VIDEO_OUT_OF_DIR: video path outside match dir: {video_path}")


def _backfill_timed_out_match(match_id: str, payload: dict) -> bool:
    item = _timed_out_matches.get(match_id)
    if item is None:
        return False

    snap, offset_ms = item
    result_code = _result_code(payload)
    changed = _apply_corrected_signals(snap, payload)
    write_json_txt(snap, result_code, offset_ms)
    _validate_match_result_video_path(match_id, payload)
    add_log("info",
            f"Late AI result backfilled for timed-out match {match_id}: "
            f"result_code={result_code}, corrected_signals={changed}")
    _timed_out_matches.pop(match_id, None)
    ws_push_sync({
        "type": "match_state", "state": "idle", "match_id": "",
        "finalized": match_id, "late_ai_backfill": True,
    })
    return True


def _bg(coro):
    """Schedule a coroutine as background task with error logging."""
    task = asyncio.create_task(coro)
    task.add_done_callback(_on_bg_done)


def _on_bg_done(task: asyncio.Task):
    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.exception("Background task failed: %s", e)


# ── MCU callbacks ─────────────────────────────────────────────────────

async def on_main_frame(cmd: int, frame: dict, recv_ts: int, recv_mono_ns: int = 0):
    if cmd == 0x50:  # Start match
        if match.state != MatchState.IDLE:
            add_log("warning",
                    f"0x50 received in state {match.state.value}, canceling match {match.match_id}")
            await _cancel_match()

        match.reset()
        clear_signal_buffer()  # new match → drop prior match's buffered signals
        match.match_id = str(int(time.time() * 1000))
        match.state = MatchState.PREPARING
        data = frame.get("data", b"")
        match.weapon = data[0] if len(data) > 0 else 0
        match.sensor = data[1] if len(data) > 1 else 0

        create_match_dir(match.match_id)
        add_log("info", f"Match start: {match.match_id} weapon={match.weapon}")
        ws_push_sync({
            "type": "match_state", "state": match.state.value,
            "match_id": match.match_id, "weapon": match.weapon, "sensor": match.sensor,
        })
        if ai_client and ai_client.connected:
            await ai_client.send("match_pre_start",
                {"weapon": match.weapon, "sensor": match.sensor,
                 "side_map": {"A": "left", "B": "right"}},
                match_id=match.match_id)

    elif cmd == 0x51:  # Cancel
        add_log("info", f"0x51 cancel: {match.match_id}")
        await _cancel_match()

    elif cmd == 0x52:  # Round end / light (= match end, valid or not)
        if match.state == MatchState.PLAYING and match.match_id:
            _on_round_end(frame.get("data", b""), recv_ts, recv_mono_ns)


async def on_hit_frame(digit: int, recv_ts: int, recv_mono_ns: int = 0):
    if match.state == MatchState.PLAYING and match.match_id:
        _send_hit_signal(digit, recv_ts, recv_mono_ns)


def _correct_signal(recv_ts: int, recv_mono_ns: int) -> tuple[int, int, int]:
    """Apply the static video-sync offset to the receive timestamps.

    Returns (offset_ms, signal_ts, signal_mono_ns). signal_mono_ns is 0 when no
    monotonic source was captured (e.g. debug-injected frames), so it is never
    sent as a meaningless value.
    """
    config = get_config()
    config.reload_if_stale()
    offset_ms = config.video_sync_offset_ms
    signal_ts = recv_ts + offset_ms
    signal_mono_ns = recv_mono_ns + offset_ms * 1_000_000 if recv_mono_ns else 0
    return offset_ms, signal_ts, signal_mono_ns


def _send_hit_signal(digit: int, recv_ts: int, recv_mono_ns: int):
    """Forward a hit frame (source=hit) to AI and record it in signals[]."""
    _, signal_ts, signal_mono_ns = _correct_signal(recv_ts, recv_mono_ns)

    match.signals.append(Signal(fight=digit, source="hit", signal_ts=signal_ts))
    add_signal_sync({
        "ts": signal_ts, "fight": digit, "source": "hit",
        "match_id": match.match_id,
    })
    if ai_client and ai_client.connected:
        payload = {"fight": digit, "source": "hit", "signal_ts": signal_ts,
                   "terminal": False}
        if signal_mono_ns:
            payload["signal_mono_ns"] = signal_mono_ns
        _bg(ai_client.send("signal", payload, match_id=match.match_id))


def _on_round_end(data: bytes, recv_ts: int, recv_mono_ns: int):
    """Handle 0x52: forward the terminal light signal to AI, then write json.txt
    immediately (electric signals + temp result) and arm the AI-result timeout.

    0x52 = match end (valid or not). It carries BOTH sides' final lights; it is
    NOT stored in list[] — only hit frames go there (§7.4.3 / §13).
    """
    global _settle_timeout_task

    a_lit, b_lit = parse_light_signals(data)
    final_lights = {"A": a_lit, "B": b_lit}
    offset_ms, signal_ts, signal_mono_ns = _correct_signal(recv_ts, recv_mono_ns)

    add_signal_sync({
        "ts": signal_ts, "source": "light",
        "final_lights": final_lights, "match_id": match.match_id,
    })
    if ai_client and ai_client.connected:
        payload = {"source": "light", "signal_ts": signal_ts,
                   "terminal": True, "final_lights": final_lights}
        if signal_mono_ns:
            payload["signal_mono_ns"] = signal_mono_ns
        _bg(ai_client.send("signal", payload, match_id=match.match_id))

    # 先写后改: persist json.txt now with the light-derived temp result.
    temp_result = temp_result_from_lights(a_lit, b_lit)
    write_json_txt(match, temp_result, offset_ms)
    match.state = MatchState.SETTLING
    add_log("info",
            f"Round end {match.match_id}: lights A={a_lit} B={b_lit}, "
            f"temp result={temp_result}, json.txt written, awaiting AI result")
    ws_push_sync({
        "type": "match_state", "state": match.state.value,
        "match_id": match.match_id, "final_lights": final_lights,
    })

    timeout_s = get_config().get("ai", "result_timeout_s", 8)
    _cancel_settle_timeout()
    _settle_timeout_task = asyncio.create_task(
        _settle_timeout(match.match_id, offset_ms, timeout_s, temp_result))
    _settle_timeout_task.add_done_callback(_on_bg_done)

    # A single final electric light is authoritative and does not require ROW
    # analysis, so announce it immediately. Final state still waits for AI so
    # json/video artifacts are backfilled normally.
    if temp_result in (8, 9) and audio:
        sound = _winner_audio(temp_result)
        if sound:
            _start_result_audio_sequence([sound])


def _winner_audio(result_code: int) -> str | None:
    """Winner-announcement sound for a result code (8=A, 9=B, 10=tie)."""
    return {8: "left.mp3", 9: "right.mp3", 10: "tie.mp3"}.get(result_code)


def _reason_audio(payload: dict) -> str | None:
    key = str(payload.get("reason_audio_key") or "").strip()
    if not key:
        return None
    if not _REASON_AUDIO_KEY_RE.fullmatch(key):
        add_log("warning", f"Ignoring invalid reason_audio_key: {key!r}")
        return None
    return f"zh/{key}.mp3"


def _start_result_audio_sequence(files: list[str]) -> bool:
    if not audio or not files:
        return False
    first, *rest = files
    match.result_audio_announced = True
    match.result_audio_done = False
    match.result_audio_current = first
    match.result_audio_queue = list(rest)
    audio.play(first)
    return True


def _append_result_audio(filename: str | None) -> bool:
    if not audio or not filename:
        return False
    if filename == match.result_audio_current or filename in match.result_audio_queue:
        return False
    if match.result_audio_done and not match.result_audio_current:
        match.result_audio_done = False
        match.result_audio_current = filename
        audio.play(filename)
        return True
    match.result_audio_queue.append(filename)
    return True


def _finish_or_continue_result_audio() -> bool:
    if match.result_audio_queue and audio:
        next_file = match.result_audio_queue.pop(0)
        match.result_audio_current = next_file
        match.result_audio_done = False
        audio.play(next_file)
        return True
    match.result_audio_current = ""
    match.result_audio_done = True
    return False


def _finalize_current_match_after_audio():
    finalized_id = match.match_id
    add_log("info", f"Match {finalized_id} finalized")
    ws_push_sync({
        "type": "match_state", "state": "idle", "match_id": "",
        "finalized": finalized_id,
    })
    match.reset()


async def _settle_timeout(match_id: str, offset_ms: int, timeout_s: float,
                          temp_result: int):
    """If no match_result arrives within timeout_s, finalize with result_code=0
    and announce the electrical light result (final_lights-derived)."""
    global _settle_timeout_task
    try:
        await asyncio.sleep(timeout_s)
    except asyncio.CancelledError:
        return
    _settle_timeout_task = None
    if match.state != MatchState.SETTLING or match.match_id != match_id:
        return
    result_audio_already_announced = match.result_audio_announced
    timeout_snapshot = _snapshot_match(match)
    write_json_txt(match, 0, offset_ms)  # result_code=0: AI timed out
    _remember_timed_out_match(match_id, timeout_snapshot, offset_ms)
    add_log("warning",
            f"AI result timeout ({timeout_s}s) for {match_id}, "
            f"finalized with result_code=0")
    ws_push_sync({"type": "ai_error", "code": "E_AI_RESULT_TIMEOUT",
                  "reason": f"no match_result within {timeout_s}s"})
    ws_push_sync({"type": "match_state", "state": "idle", "match_id": "",
                  "finalized": match_id})
    match.reset()
    # Match state is finalized; announce the electrical light result. If AI
    # answers late, §10.4/§13 still allow json.txt to be backfilled from the
    # snapshot kept above, but that must not replay result audio.
    sound = _winner_audio(temp_result)
    if sound and audio and not result_audio_already_announced:
        audio.play(sound)


async def _cancel_match(notify_ai: bool = True):
    """Cancel current match: clear audio, remove dir, reset state.

    notify_ai=True sends match_cancel to AI (0x51 / 0x50 preemption).
    notify_ai=False skips it: when AI itself raised camera_error it already
    knows the match failed and must not be echoed a match_cancel (FR-2.6 #4).
    """
    _cancel_settle_timeout()

    mid = match.match_id
    match.reset()
    if mid:
        _timed_out_matches.pop(mid, None)

    if audio:
        audio.clear()
    if mid:
        remove_match_dir(mid)
    if mid and notify_ai and ai_client and ai_client.connected:
        await ai_client.send("match_cancel", {}, match_id=mid)

    ws_push_sync({"type": "match_state", "state": match.state.value, "match_id": ""})


# ── AI callbacks ──────────────────────────────────────────────────────

async def on_ai_event(event_type: str, payload: dict, _match_id: str):
    if event_type == "camera_ready":
        if match.state != MatchState.PREPARING:
            add_log("warning", f"camera_ready in state {match.state.value}, ignoring")
            return

        match.begin_ts = int(time.time() * 1000)
        match.state = MatchState.PLAYING
        add_log("info", f"Camera ready, begin_ts={match.begin_ts}")
        # camera_ready may carry optional recording metadata (video_path,
        # first_frame_ts, fps_nominal, frame_timestamps_path, ...). It does not
        # drive the main flow, but the protocol (§8.1) says to record it — handy
        # for tracing frame timing when integrating with the real AI.
        if payload:
            add_log("info", f"Camera metadata: "
                    f"{json.dumps(payload, ensure_ascii=False)}")
        ws_push_sync({
            "type": "match_state", "state": match.state.value,
            "match_id": match.match_id, "begin_ts": match.begin_ts,
        })
        if ai_client and ai_client.connected:
            await ai_client.send("match_begin_ack",
                {"begin_ts": match.begin_ts}, match_id=match.match_id)
        if audio:
            audio.play("start.mp3")

    elif event_type == "camera_error":
        code = payload.get("code", "E_CAMERA_INIT")
        reason = payload.get("reason", "unknown")
        add_log("error", f"Camera init failed: {code} - {reason}")
        ws_push_sync({"type": "ai_error", "code": code, "reason": reason})
        await _cancel_match(notify_ai=False)  # AI already knows (FR-2.6 #4)
        # Announce the failure. Played after _cancel_match so its audio.clear()
        # (which drains the queue) does not immediately stop this prompt.
        if audio:
            audio.play("CameraFailure.mp3")

    elif event_type == "match_result":
        if match.state not in (MatchState.PLAYING, MatchState.SETTLING):
            late_match_id = str(_match_id or payload.get("match_id") or "")
            if late_match_id and _backfill_timed_out_match(late_match_id, payload):
                return
            # E_AI_RESULT_NO_MATCH: result arrived with no live match.
            add_log("warning", f"match_result in state {match.state.value}, ignoring")
            return

        _cancel_settle_timeout()  # AI answered in time; stop the timeout
        match.state = MatchState.SETTLING
        match.ai_result_received = True
        winner = payload.get("winner", "tie")
        result_code = _result_code(payload)
        reason_code = payload.get("reason_code")
        spoken_reason_zh = payload.get("spoken_reason_zh")
        reason_sound = _reason_audio(payload)
        corrected = _apply_corrected_signals(match, payload)

        # 回填: rewrite json.txt with the authoritative AI result code.
        config = get_config()
        write_json_txt(match, result_code, config.video_sync_offset_ms)
        _validate_match_result_video_path(match.match_id, payload)

        reason_log = f" reason_code={reason_code}" if reason_code else ""
        add_log("info",
                f"Match result: winner={winner} result_code={result_code} "
                f"corrected_signals={corrected}{reason_log}")
        state_event = {
            "type": "match_state", "state": match.state.value,
            "match_id": match.match_id, "winner": winner,
        }
        if reason_code:
            state_event["reason_code"] = reason_code
        if spoken_reason_zh:
            state_event["spoken_reason_zh"] = spoken_reason_zh
        ws_push_sync(state_event)
        audio_map = {"A": "left.mp3", "B": "right.mp3", "tie": "tie.mp3"}
        if audio:
            if match.result_audio_announced:
                if _append_result_audio(reason_sound):
                    add_log("info", f"Queued result reason audio: {reason_sound}")
                add_log("info", "Result audio already announced from final lights")
                if match.result_audio_done:
                    _finalize_current_match_after_audio()
            else:
                files = [audio_map.get(winner, "tie.mp3")]
                if reason_sound:
                    files.append(reason_sound)
                _start_result_audio_sequence(files)

    elif event_type == "ai_error":
        add_log("error", f"AI error: {payload.get('code', '?')} - {payload.get('reason', '')}")
        ws_push_sync({"type": "ai_error", **payload})


# ── Audio callbacks ───────────────────────────────────────────────────

async def on_audio_done(filename: str):
    if filename == "start.mp3":
        if match.state != MatchState.PLAYING:
            return  # stale callback from a canceled match
        match.voice_end_ts = int(time.time() * 1000)
        if ai_client and ai_client.connected:
            await ai_client.send("voice_end",
                {"voice_end_ts": match.voice_end_ts}, match_id=match.match_id)

    elif filename == match.result_audio_current:
        if match.state != MatchState.SETTLING:
            return  # stale callback (match canceled / already finalized)
        if _finish_or_continue_result_audio():
            return
        if match.ai_result_received:
            # json.txt was already written (and backfilled) when match_result
            # arrived; the full result-audio sequence is done, so finalize state.
            _finalize_current_match_after_audio()


# ── Upload callbacks ──────────────────────────────────────────────────

async def on_upload_progress(match_id: str, phase: str, sent: int,
                              total: int, error: str | None):
    ws_push_sync({
        "type": "upload_progress", "match_id": match_id,
        "phase": phase, "bytes_sent": sent, "bytes_total": total,
        "error": error,
    })


# ── Startup / shutdown ────────────────────────────────────────────────

async def startup():
    global serial, ai_client, audio, uploader

    config = get_config()
    audio = AudioPlayer(on_play_done=on_audio_done)
    ai_client = AIClient(on_event=on_ai_event)
    serial = SerialReader(on_main_frame=on_main_frame, on_hit_frame=on_hit_frame)
    uploader = Uploader(on_progress=on_upload_progress)

    api_state["current_match"] = match
    api_state["serial"] = serial
    api_state["ai"] = ai_client
    api_state["uploader"] = uploader

    _bg(serial.run())
    if config.get("ai", "enabled", True):
        _bg(ai_client.run())
    else:
        add_log("info", "AI socket disabled by config")
        logger.info("AI socket disabled by config")
    _bg(audio.run())
    _bg(uploader.run())
    # Re-queue uploads that were requested but not confirmed before a restart
    # (e.g. power loss mid-transfer), so they resume without manual action.
    await uploader.restore_pending()

    add_log("info", "PisteLink started")
    logger.info("PisteLink backend started")


async def shutdown():
    add_log("info", "PisteLink shutting down")
    if serial:
        await serial.stop()
    if ai_client:
        await ai_client.stop()
    if audio:
        await audio.stop()
    if uploader:
        await uploader.stop()


# Register lifecycle handlers at import time so the background services start
# whether launched via `python -m backend.main` or `uvicorn backend.main:app`.
app.add_event_handler("startup", startup)
app.add_event_handler("shutdown", shutdown)

# Debug endpoints (PISTELINK_DEBUG=1 only)
if os.environ.get("PISTELINK_DEBUG") == "1":
    from .debug import router as _debug_router
    app.include_router(_debug_router)


# ── Main ──────────────────────────────────────────────────────────────

def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    config = get_config()
    host = config.get("http", "host", "127.0.0.1")
    port = config.get("http", "port", 8080)

    uvicorn.run(app, host=host, port=port, log_config=None)


if __name__ == "__main__":
    main()
