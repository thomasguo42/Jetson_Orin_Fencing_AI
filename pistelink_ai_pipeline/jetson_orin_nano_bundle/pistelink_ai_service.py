"""PisteLink AI Unix-socket service for the copied low-latency pipeline."""

from __future__ import annotations

import argparse
import grp
import json
import os
import pwd
import re
import shutil
import socket
import stat
import subprocess
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from pistelink_analysis_adapter import PisteLinkAnalyzerSession, default_analyzer_config, warm_pistelink_analyzer
from pistelink_camera_recorder import PisteLinkCameraRecorder, default_camera_settings, transcode_avi_to_mp4
from local_streaming_manager import shutdown_shared_local_analyzer
from pistelink_protocol import NdjsonSocket, ProtocolError, epoch_ms, make_message
from pistelink_signal_adapter import (
    FrameTimestamp,
    build_legacy_txt,
    write_frame_timestamps,
    write_mapping,
)


DEFAULT_SOCKET_PATH = "/run/pistelink/ai.sock"
DEFAULT_MATCH_ROOT = "/var/lib/pistelink/matches"


@dataclass
class MatchContext:
    match_id: str
    match_dir: Path
    side_map: Dict[str, str]
    weapon: str = "sabre"
    sensor: Dict[str, Any] = field(default_factory=dict)
    match_begin_ts: Optional[int] = None
    voice_end_ts: Optional[int] = None
    recording_start_ts: Optional[int] = None
    signals: List[Dict[str, Any]] = field(default_factory=list)
    terminal_signal: Optional[Dict[str, Any]] = None
    analyzer: Optional[PisteLinkAnalyzerSession] = None
    analyzer_streaming_started: bool = False
    analyzer_ready_at_recording_start: bool = False
    analysis_start_frame: Optional[int] = None
    camera: Optional[PisteLinkCameraRecorder] = None
    finalizing: bool = False
    cancelled: bool = False

    @property
    def ai_dir(self) -> Path:
        return self.match_dir / "ai"

    @property
    def avi_path(self) -> Path:
        return self.match_dir / f"segment_{self.match_id}.avi"

    @property
    def mp4_path(self) -> Path:
        return self.match_dir / f"segment_{self.match_id}.mp4"

    @property
    def signal_txt_path(self) -> Path:
        return self.ai_dir / f"{self.match_id}_signals.txt"

    @property
    def frame_timestamps_path(self) -> Path:
        return self.ai_dir / "frame_timestamps.jsonl"

    @property
    def signal_mapping_path(self) -> Path:
        return self.ai_dir / "signal_frame_mapping.json"

    @property
    def service_log_path(self) -> Path:
        return self.ai_dir / "pistelink_service.log"

    @property
    def active_analysis_dir(self) -> Path:
        return self.ai_dir / "active_analysis_phrase"

    @property
    def active_signal_txt_path(self) -> Path:
        return self.active_analysis_dir / f"{self.match_id}_active_signals.txt"

    @property
    def active_frame_timestamps_path(self) -> Path:
        return self.active_analysis_dir / "active_frame_timestamps.jsonl"

    @property
    def active_video_path(self) -> Path:
        return self.active_analysis_dir / f"segment_{self.match_id}_active.mp4"


class PisteLinkAIService:
    def __init__(
        self,
        *,
        socket_path: Path,
        match_root: Path,
        dry_run: bool = False,
        heartbeat_interval_s: float = 2.0,
        peer_timeout_s: float = 6.0,
    ):
        self.socket_path = socket_path
        self.match_root = match_root
        self.dry_run = dry_run
        self.heartbeat_interval_s = heartbeat_interval_s
        self.peer_timeout_s = peer_timeout_s
        self.analyzer_config = default_analyzer_config()
        self.live_streaming_enabled = _env_bool("PISTELINK_LIVE_STREAMING", True)
        self.analyzer_prewarm_enabled = _env_bool("PISTELINK_ANALYZER_PREWARM", True)
        self.analyzer_ready_wait_s = float(os.environ.get("PISTELINK_ANALYZER_READY_WAIT_S", "0.25"))
        self.debug_artifacts_enabled = _env_bool("PISTELINK_DEBUG_ARTIFACTS", True)
        self.debug_artifact_timeout_s = float(os.environ.get("PISTELINK_DEBUG_ARTIFACT_TIMEOUT_S", "600"))
        self.debug_artifact_delay_s = float(os.environ.get("PISTELINK_DEBUG_ARTIFACT_DELAY_S", "0"))

        self._server: Optional[socket.socket] = None
        self._client: Optional[NdjsonSocket] = None
        self._client_socket: Optional[socket.socket] = None
        self._client_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._active_match: Optional[MatchContext] = None
        self._next_outbound_id = 1
        self._last_sent = time.monotonic()
        self._last_received = time.monotonic()
        self._shutdown = threading.Event()
        self._prewarm_lock = threading.Lock()
        self._prewarm_thread: Optional[threading.Thread] = None
        self._prewarm_started = False
        self._prewarm_ready = False
        self._prewarm_error: Optional[str] = None

    def serve_forever(self) -> None:
        self._prepare_socket_path()
        self._start_analyzer_prewarm()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            self._server = server
            server.bind(str(self.socket_path))
            self._secure_socket_file()
            server.listen(1)
            server.settimeout(0.5)
            print(f"[PISTELINK] AI service listening on {self.socket_path}")
            while not self._shutdown.is_set():
                try:
                    conn, _addr = server.accept()
                except socket.timeout:
                    continue
                except OSError:
                    if self._shutdown.is_set():
                        break
                    raise
                self._replace_client(conn)

    def stop(self) -> None:
        self._shutdown.set()
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass

    def _start_analyzer_prewarm(self) -> None:
        if self.dry_run or not self.live_streaming_enabled or not self.analyzer_prewarm_enabled:
            return
        with self._prewarm_lock:
            if self._prewarm_started:
                return
            self._prewarm_started = True
            thread = threading.Thread(target=self._run_analyzer_prewarm, daemon=True)
            self._prewarm_thread = thread
            thread.start()

    def _run_analyzer_prewarm(self) -> None:
        fps, width, height = default_camera_settings()
        del fps
        try:
            print(f"[PISTELINK] Prewarming local analyzer for {width}x{height}", flush=True)
            warm_pistelink_analyzer(self.analyzer_config, width, height)
            with self._prewarm_lock:
                self._prewarm_ready = True
                self._prewarm_error = None
            print("[PISTELINK] Local analyzer prewarm complete", flush=True)
        except Exception as exc:
            with self._prewarm_lock:
                self._prewarm_ready = False
                self._prewarm_error = str(exc)
            print(f"[PISTELINK] Local analyzer prewarm failed: {exc}", flush=True)

    def _analyzer_prewarm_ready(self) -> bool:
        with self._prewarm_lock:
            return self._prewarm_ready

    def _analyzer_prewarm_status(self) -> Dict[str, Any]:
        with self._prewarm_lock:
            return {
                "started": self._prewarm_started,
                "ready": self._prewarm_ready,
                "error": self._prewarm_error,
            }

    def _should_start_live_streaming(self, ctx: MatchContext) -> bool:
        if self.dry_run or not self.live_streaming_enabled:
            return False
        weapon_code = _weapon_code(ctx.weapon)
        return weapon_code != 2

    def _prepare_socket_path(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        if self.socket_path.exists():
            mode = self.socket_path.stat().st_mode
            if not stat.S_ISSOCK(mode):
                raise RuntimeError(f"socket path exists and is not a socket: {self.socket_path}")
            self.socket_path.unlink()

    def _secure_socket_file(self) -> None:
        owner = os.environ.get("PISTELINK_AI_SOCKET_OWNER", "").strip()
        group = os.environ.get("PISTELINK_AI_SOCKET_GROUP", "").strip()
        mode_text = os.environ.get("PISTELINK_AI_SOCKET_MODE", "0660").strip()

        uid = -1
        gid = -1
        if owner:
            try:
                uid = pwd.getpwnam(owner).pw_uid
            except KeyError:
                print(f"[PISTELINK] socket owner not found: {owner!r}", flush=True)
        if group:
            try:
                gid = grp.getgrnam(group).gr_gid
            except KeyError:
                print(f"[PISTELINK] socket group not found: {group!r}", flush=True)
        else:
            try:
                gid = self.socket_path.parent.stat().st_gid
            except OSError:
                gid = -1

        if uid != -1 or gid != -1:
            try:
                os.chown(self.socket_path, uid, gid)
            except PermissionError:
                print("[PISTELINK] socket chown skipped: permission denied", flush=True)
            except OSError as exc:
                print(f"[PISTELINK] socket chown skipped: {exc}", flush=True)

        try:
            mode = int(mode_text, 8)
        except ValueError:
            mode = 0o660
        os.chmod(self.socket_path, mode)

    def _replace_client(self, conn: socket.socket) -> None:
        with self._client_lock:
            old_socket = self._client_socket
            self._client_socket = conn
        if old_socket is not None:
            self._close_socket(old_socket)
        thread = threading.Thread(target=self._handle_client, args=(conn,), daemon=True)
        thread.start()

    @staticmethod
    def _close_socket(sock: socket.socket) -> None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass

    def _handle_client(self, conn: socket.socket) -> None:
        client = NdjsonSocket(conn)
        with self._client_lock:
            self._client = client
            self._client_socket = conn
            self._next_outbound_id = 0
            self._last_sent = time.monotonic()
            self._last_received = time.monotonic()
        print("[PISTELINK] backend connected")

        handshake_complete = False
        try:
            while not self._shutdown.is_set():
                try:
                    message = client.read_message(timeout=0.5)
                except EOFError:
                    break
                except OSError:
                    break
                except ProtocolError as exc:
                    print(f"[PISTELINK] protocol error: {exc}")
                    continue

                now = time.monotonic()
                if message is None:
                    if now - self._last_sent >= self.heartbeat_interval_s:
                        self._send("ping", {"reason": "idle"})
                    if now - self._last_received >= self.peer_timeout_s:
                        raise TimeoutError("PisteLink backend heartbeat timed out")
                    continue

                self._last_received = now
                if not handshake_complete:
                    if message["type"] != "hello":
                        print(f"[PISTELINK] closing connection: first message was {message['type']!r}, expected hello")
                        break
                    payload = message.get("payload") or {}
                    if payload.get("protocol_v") != 1:
                        print(f"[PISTELINK] closing connection: unsupported hello protocol_v={payload.get('protocol_v')!r}")
                        break
                    handshake_complete = True
                self._dispatch(message)
        except TimeoutError as exc:
            print(f"[PISTELINK] {exc}")
        finally:
            should_cancel_active = False
            with self._client_lock:
                if self._client is client:
                    self._client = None
                    should_cancel_active = True
                if self._client_socket is conn:
                    self._client_socket = None
            self._close_socket(conn)
            if should_cancel_active:
                self._cancel_active_recording("backend_disconnected")
            print("[PISTELINK] backend disconnected")

    def _dispatch(self, message: Dict[str, Any]) -> None:
        msg_type = message["type"]
        payload = message.get("payload") or {}
        if msg_type == "hello":
            self._send(
                "hello_ack",
                {
                    "role": "ai",
                    "app": "pistelink_ai_low_latency",
                    "version": "0.1.0",
                    "protocol_v": 1,
                    "capabilities": [
                        "camera_ready",
                        "frame_timestamps",
                        "legacy_txt_bridge",
                        "local_streaming_analyzer",
                    ],
                },
            )
            return
        if msg_type == "ping":
            self._send("pong", {"ref_id": message["id"]}, match_id=message.get("match_id"))
            return
        if msg_type == "pong":
            return
        if msg_type == "shutdown":
            self._shutdown.set()
            return

        if msg_type == "match_pre_start":
            self._on_match_pre_start(message, payload)
        elif msg_type == "match_begin_ack":
            self._on_match_begin_ack(message, payload)
        elif msg_type == "voice_end":
            self._on_voice_end(message, payload)
        elif msg_type == "signal":
            self._on_signal(message, payload)
        elif msg_type == "match_cancel":
            self._on_match_cancel(message, payload)
        else:
            print(f"[PISTELINK] ignoring unsupported message type {msg_type!r}")

    def _on_match_pre_start(self, message: Dict[str, Any], payload: Dict[str, Any]) -> None:
        match_id = str(message.get("match_id") or payload.get("match_id") or "")
        if not match_id:
            self._send("camera_error", {"code": "CAM_INIT_FAIL", "reason": "match_pre_start missing match_id"})
            return

        side_map = payload.get("side_map") if isinstance(payload.get("side_map"), dict) else {}
        side_map = {
            "A": str(side_map.get("A", "left")).lower(),
            "B": str(side_map.get("B", "right")).lower(),
        }
        match_dir = Path(payload.get("match_dir") or (self.match_root / match_id)).expanduser()
        ctx = MatchContext(
            match_id=match_id,
            match_dir=match_dir,
            side_map=side_map,
            weapon=str(payload.get("weapon") or "sabre"),
            sensor=payload.get("sensor") if isinstance(payload.get("sensor"), dict) else {},
        )
        ctx.match_dir.mkdir(parents=True, exist_ok=True)
        ctx.ai_dir.mkdir(parents=True, exist_ok=True)

        with self._state_lock:
            if self._active_match is not None and not self._active_match.finalizing:
                self._cancel_context_locked(self._active_match, "replaced_by_new_match")
            self._active_match = ctx

        if self.dry_run:
            ready_payload = {
                "video_path": str(ctx.mp4_path),
                "recording_start_ts": epoch_ms(),
                "fps_nominal": 30.0,
                "width": 0,
                "height": 0,
                "frame_timestamps_path": str(ctx.frame_timestamps_path),
                "dry_run": True,
            }
            self._send("camera_ready", ready_payload, match_id=ctx.match_id)
            return

        thread = threading.Thread(target=self._start_match, args=(ctx,), daemon=True)
        thread.start()

    def _start_match(self, ctx: MatchContext) -> None:
        try:
            ctx.camera = PisteLinkCameraRecorder()
            streaming_manager = None
            analyzer_ready = False
            if self._should_start_live_streaming(ctx):
                try:
                    ctx.active_analysis_dir.mkdir(parents=True, exist_ok=True)
                    ctx.analyzer = PisteLinkAnalyzerSession(
                        self.analyzer_config,
                        ctx.match_dir,
                        ctx.match_id,
                        phrase_dir=ctx.active_analysis_dir,
                    )
                    if ctx.analyzer.begin_streaming(
                        ctx.camera.fps,
                        ctx.camera.width,
                        ctx.camera.height,
                        expected_frames=0,
                        start_paused=True,
                    ):
                        ctx.analyzer_streaming_started = True
                        analyzer_ready = ctx.analyzer.wait_until_ready(
                            timeout=max(0.0, self.analyzer_ready_wait_s),
                            fail_on_timeout=False,
                        )
                        if not ctx.analyzer.manager.is_active():
                            ctx.analyzer = None
                            ctx.analyzer_streaming_started = False
                            print("[PISTELINK] Live analyzer streaming not attached: session failed before recording", flush=True)
                            analyzer_ready = False
                            streaming_manager = None
                        else:
                            ctx.analyzer_ready_at_recording_start = analyzer_ready
                            streaming_manager = ctx.analyzer.manager
                            ready_label = "ready" if analyzer_ready else "warming"
                            print(f"[PISTELINK] Live analyzer streaming attached ({ready_label})", flush=True)
                    else:
                        ctx.analyzer = None
                        ctx.analyzer_streaming_started = False
                        print("[PISTELINK] Live analyzer streaming not attached: start returned false", flush=True)
                except Exception as exc:
                    ctx.analyzer = None
                    ctx.analyzer_streaming_started = False
                    print(f"[PISTELINK] Live analyzer streaming not attached: {exc}", flush=True)

            ctx.recording_start_ts = epoch_ms()
            if not ctx.camera.start(ctx.avi_path, streaming_manager):
                raise RuntimeError("camera recorder failed to start")
            first_frame = ctx.camera.wait_for_first_frame(timeout_s=2.0)
            if not self._is_current_active_match(ctx):
                self._cancel_context_locked(ctx, "match_replaced_after_camera_start")
                return

            ready_payload: Dict[str, Any] = {
                "video_path": str(ctx.mp4_path),
                "recording_start_ts": ctx.recording_start_ts,
                "fps_nominal": ctx.camera.fps,
                "width": ctx.camera.width,
                "height": ctx.camera.height,
                "frame_timestamps_path": str(ctx.frame_timestamps_path),
                "visual_streaming": {
                    "enabled": bool(streaming_manager),
                    "analyzer_ready_at_recording_start": analyzer_ready,
                    "prewarm": self._analyzer_prewarm_status(),
                },
            }
            if first_frame is not None:
                ready_payload["first_frame_ts"] = first_frame.ts
                ready_payload["first_frame_index"] = first_frame.frame

            self._send("camera_ready", ready_payload, match_id=ctx.match_id)
        except Exception as exc:
            should_report = self._is_current_active_match(ctx)
            self._log_context_error(ctx, "match_pre_start failed", exc)
            self._cleanup_failed_start(ctx)
            if should_report:
                self._send(
                    "camera_error",
                    {
                        "code": "CAM_INIT_FAIL",
                        "reason": str(exc),
                        "stage": "match_pre_start",
                    },
                    match_id=ctx.match_id,
                )

    def _is_current_active_match(self, ctx: MatchContext) -> bool:
        with self._state_lock:
            return self._active_match is ctx and not ctx.cancelled

    def _on_match_begin_ack(self, message: Dict[str, Any], payload: Dict[str, Any]) -> None:
        ctx = self._match_for_message(message, payload)
        if ctx is None:
            return
        ctx.match_begin_ts = _payload_ts(payload, message)

    def _on_voice_end(self, message: Dict[str, Any], payload: Dict[str, Any]) -> None:
        ctx = self._match_for_message(message, payload)
        if ctx is None:
            return
        ctx.voice_end_ts = _payload_ts(payload, message)
        if (
            ctx.analysis_start_frame is None
            and ctx.camera is not None
            and ctx.analyzer is not None
            and ctx.analyzer_streaming_started
        ):
            try:
                ctx.analysis_start_frame = ctx.camera.activate_analysis_from_now()
                print(
                    f"[PISTELINK] Active analysis window opened at camera frame {ctx.analysis_start_frame} "
                    f"(voice_end_ts={ctx.voice_end_ts})",
                    flush=True,
                )
            except Exception as exc:
                print(f"[PISTELINK] Failed to open active analysis window: {exc}", flush=True)

    def _on_signal(self, message: Dict[str, Any], payload: Dict[str, Any]) -> None:
        ctx = self._match_for_message(message, payload)
        if ctx is None:
            return

        signal_payload = _normalise_signal_payload(payload, message)

        if signal_payload.get("source") == "light" or bool(signal_payload.get("terminal")):
            ctx.terminal_signal = signal_payload
            self._begin_finalize(ctx)
        else:
            ctx.signals.append(signal_payload)

    def _on_match_cancel(self, message: Dict[str, Any], payload: Dict[str, Any]) -> None:
        ctx = self._match_for_message(message, payload)
        if ctx is None:
            return
        with self._state_lock:
            self._cancel_context_locked(ctx, str(payload.get("reason") or "match_cancel"))
            if self._active_match is ctx:
                self._active_match = None

    def _begin_finalize(self, ctx: MatchContext) -> None:
        with self._state_lock:
            if ctx.finalizing:
                return
            ctx.finalizing = True
        thread = threading.Thread(target=self._finalize_match, args=(ctx,), daemon=True)
        thread.start()

    def _finalize_match(self, ctx: MatchContext) -> None:
        analysis_result: Optional[Dict[str, Any]] = None
        frames: List[FrameTimestamp] = []
        txt_data = ""
        video_path: Path = ctx.mp4_path
        transcode_error: Optional[str] = None
        processing_error: Optional[str] = None
        result_sent = False
        needs_visual_row = False
        active_txt_data = ""
        active_start_frame = 0
        active_total_frames = 0
        analysis_fps = 30.0

        try:
            if self.dry_run:
                frames = []
                write_frame_timestamps(ctx.frame_timestamps_path, frames)
                txt_data, mappings = build_legacy_txt(
                    match_id=ctx.match_id,
                    side_map=ctx.side_map,
                    signals=ctx.signals,
                    terminal_signal=ctx.terminal_signal,
                    frames=frames,
                    match_begin_ts=ctx.match_begin_ts,
                    voice_end_ts=ctx.voice_end_ts,
                )
                ctx.signal_txt_path.write_text(txt_data, encoding="utf-8")
                write_mapping(ctx.signal_mapping_path, mappings)
            else:
                if ctx.camera is None:
                    raise RuntimeError("finalize requested without camera")
                ctx.camera.stop()
                camera_fps = ctx.camera.fps
                camera_width = ctx.camera.width
                camera_height = ctx.camera.height
                analysis_fps = camera_fps if camera_fps > 0 else 30.0
                frames = ctx.camera.write_frame_timestamps(ctx.frame_timestamps_path)
                ctx.camera.release()
                txt_data, mappings = build_legacy_txt(
                    match_id=ctx.match_id,
                    side_map=ctx.side_map,
                    signals=ctx.signals,
                    terminal_signal=ctx.terminal_signal,
                    frames=frames,
                    match_begin_ts=ctx.match_begin_ts,
                    voice_end_ts=ctx.voice_end_ts,
                )
                ctx.signal_txt_path.write_text(txt_data, encoding="utf-8")
                write_mapping(ctx.signal_mapping_path, mappings)

                transcode_holder: Dict[str, Any] = {}
                transcode_thread = threading.Thread(
                    target=_run_transcode,
                    args=(ctx.avi_path, ctx.mp4_path, transcode_holder),
                    daemon=True,
                )
                transcode_thread.start()

                final_lights = _final_lights_ab(ctx.terminal_signal, ctx.side_map)
                needs_visual_row = sum(1 for enabled in final_lights.values() if enabled) == 2
                needs_visual_row = needs_visual_row and _weapon_code(ctx.weapon) != 2
                if needs_visual_row:
                    try:
                        active_start_frame = _analysis_start_frame(ctx, frames)
                        ctx.analysis_start_frame = active_start_frame
                        active_total_frames = max(0, len(frames) - active_start_frame)
                        if active_total_frames <= 0:
                            raise RuntimeError(
                                f"active analysis window has no frames "
                                f"(start_frame={active_start_frame}, total_frames={len(frames)})"
                            )
                        ctx.active_analysis_dir.mkdir(parents=True, exist_ok=True)
                        write_frame_timestamps(
                            ctx.active_frame_timestamps_path,
                            _rebased_frame_timestamps(frames[active_start_frame:]),
                        )
                        active_txt_data, _active_mappings = build_legacy_txt(
                            match_id=ctx.match_id,
                            side_map=ctx.side_map,
                            signals=ctx.signals,
                            terminal_signal=ctx.terminal_signal,
                            frames=frames,
                            match_begin_ts=ctx.match_begin_ts,
                            voice_end_ts=ctx.voice_end_ts,
                            active_start_frame=active_start_frame,
                            frame_time_fps=analysis_fps,
                        )
                        ctx.active_signal_txt_path.write_text(active_txt_data, encoding="utf-8")
                        analysis_result = self._run_visual_analysis(
                            ctx=ctx,
                            txt_data=active_txt_data,
                            camera_fps=analysis_fps,
                            camera_width=camera_width,
                            camera_height=camera_height,
                            total_frames=active_total_frames,
                            active_start_frame=active_start_frame,
                        )
                    except Exception as exc:
                        processing_error = str(exc)
                else:
                    if ctx.analyzer is not None:
                        try:
                            ctx.analyzer.cancel("visual_analysis_not_required")
                        except Exception:
                            pass
                    result_payload = self._build_match_result(
                        ctx=ctx,
                        analysis_result=None,
                        video_path=ctx.mp4_path,
                        transcode_error=None,
                        processing_error=None,
                    )
                    result_payload["video_transcode_pending"] = True
                    if not self._context_cancelled(ctx):
                        self._send("match_result", result_payload, match_id=ctx.match_id)
                        result_sent = True

                transcode_thread.join()
                if transcode_holder.get("error"):
                    transcode_error = str(transcode_holder["error"])
                    video_path = ctx.avi_path
                else:
                    video_path = Path(transcode_holder.get("path") or ctx.mp4_path)
                    _unlink_quietly(ctx.avi_path)

            if not result_sent:
                result_payload = self._build_match_result(
                    ctx=ctx,
                    analysis_result=analysis_result,
                    video_path=video_path,
                    transcode_error=transcode_error,
                    processing_error=processing_error,
                )
        except Exception as exc:
            self._log_context_error(ctx, "finalize failed", exc)
            try:
                if ctx.camera is not None:
                    ctx.camera.release()
            except Exception:
                pass
            try:
                if ctx.analyzer is not None:
                    ctx.analyzer.cancel("finalize_error")
            except Exception:
                pass
            if not result_sent:
                result_payload = self._build_match_result(
                    ctx=ctx,
                    analysis_result=None,
                    video_path=video_path if video_path.exists() else ctx.avi_path,
                    transcode_error=transcode_error,
                    processing_error=str(exc),
                )

        if self._context_cancelled(ctx):
            self._clear_active_context(ctx)
            return

        debug_artifacts_requested = self._should_write_debug_artifacts(
            ctx,
            needs_visual_row,
            video_path,
            frames,
            active_txt_data,
            active_total_frames,
        )
        if debug_artifacts_requested:
            result_payload["debug_artifacts_pending"] = True
            result_payload["debug_artifacts_dir"] = str(self._debug_artifacts_root(ctx))

        if not result_sent and not self._context_cancelled(ctx):
            self._send("match_result", result_payload, match_id=ctx.match_id)
            result_sent = True
        if debug_artifacts_requested:
            self._start_debug_artifact_job(
                ctx,
                txt_data=active_txt_data,
                frames=list(frames),
                video_path=video_path,
                active_start_frame=active_start_frame,
                analysis_fps=analysis_fps,
            )
        self._clear_active_context(ctx)

    def _run_visual_analysis(
        self,
        *,
        ctx: MatchContext,
        txt_data: str,
        camera_fps: float,
        camera_width: int,
        camera_height: int,
        total_frames: int,
        active_start_frame: int,
    ) -> Optional[Dict[str, Any]]:
        signal_data = txt_data.encode("utf-8")
        signal_filename = str(ctx.active_signal_txt_path.relative_to(ctx.active_analysis_dir))
        live_error: Optional[Exception] = None

        if ctx.analyzer is not None and ctx.analyzer_streaming_started:
            try:
                if ctx.analyzer.live_degraded(total_frames):
                    self._ensure_active_analysis_video(ctx, active_start_frame=active_start_frame, analysis_fps=camera_fps)
                return ctx.analyzer.end(
                    signal_data=signal_data,
                    signal_filename=signal_filename,
                    total_frames=total_frames,
                )
            except Exception as exc:
                live_error = exc
                if ctx.analyzer_ready_at_recording_start:
                    raise
                print(f"[PISTELINK] Live analyzer failed, retrying offline: {exc}", flush=True)

        self._ensure_active_analysis_video(ctx, active_start_frame=active_start_frame, analysis_fps=camera_fps)
        offline_analyzer = PisteLinkAnalyzerSession(
            self.analyzer_config,
            ctx.match_dir,
            ctx.match_id,
            phrase_dir=ctx.active_analysis_dir,
        )
        ctx.analyzer = offline_analyzer
        ctx.analyzer_streaming_started = False
        if not offline_analyzer.start(camera_fps, camera_width, camera_height, expected_frames=total_frames):
            if live_error is not None:
                raise RuntimeError(f"live analyzer failed: {live_error}; offline analyzer did not become ready")
            raise RuntimeError("local analyzer did not become ready")
        try:
            return offline_analyzer.end(
                signal_data=signal_data,
                signal_filename=signal_filename,
                total_frames=total_frames,
            )
        except Exception as exc:
            if live_error is not None:
                raise RuntimeError(f"live analyzer failed: {live_error}; offline analyzer failed: {exc}") from exc
            raise

    def _build_match_result(
        self,
        *,
        ctx: MatchContext,
        analysis_result: Optional[Dict[str, Any]],
        video_path: Path,
        transcode_error: Optional[str],
        processing_error: Optional[str],
    ) -> Dict[str, Any]:
        winner, result_code, decision_source = self._decide_result(ctx, analysis_result)
        payload: Dict[str, Any] = {
            "winner": winner,
            "result_code": result_code,
            "video_path": str(video_path),
            "signal_frame_mapping_path": str(ctx.signal_mapping_path),
            "processing_mode": "final_lights_fallback",
        }
        payload["analysis_output_dir"] = str(ctx.analyzer.output_dir) if ctx.analyzer is not None else str(ctx.ai_dir)
        payload["frame_timestamps_path"] = str(ctx.frame_timestamps_path)
        payload["signal_txt_path"] = str(ctx.signal_txt_path)
        payload["decision_source"] = decision_source
        payload.update(_build_reason_metadata(
            ctx=ctx,
            winner=winner,
            decision_source=decision_source,
            analysis_result=analysis_result,
            processing_error=processing_error,
        ))
        if analysis_result is not None:
            payload["analysis_winner"] = analysis_result.get("winner")
            if analysis_result.get("reason"):
                payload["analysis_reason"] = analysis_result.get("reason")
            if analysis_result.get("analysis_result_json"):
                payload["analysis_result_path"] = analysis_result.get("analysis_result_json")
            if analysis_result.get("processing_mode"):
                payload["processing_mode"] = analysis_result.get("processing_mode")
        if transcode_error:
            payload["video_transcode_error"] = transcode_error
        if processing_error:
            payload["processing_error"] = processing_error
        return payload

    def _should_write_debug_artifacts(
        self,
        ctx: MatchContext,
        needs_visual_row: bool,
        video_path: Path,
        frames: List[FrameTimestamp],
        txt_data: str,
        total_frames: int,
    ) -> bool:
        if self.dry_run or not self.debug_artifacts_enabled or not needs_visual_row:
            return False
        if not txt_data or not frames or total_frames <= 0:
            return False
        return video_path.exists()

    def _debug_artifacts_root(self, ctx: MatchContext) -> Path:
        return ctx.ai_dir / "debug_artifacts"

    def _start_debug_artifact_job(
        self,
        ctx: MatchContext,
        *,
        txt_data: str,
        frames: List[FrameTimestamp],
        video_path: Path,
        active_start_frame: int,
        analysis_fps: float,
    ) -> None:
        thread = threading.Thread(
            target=self._run_debug_artifact_job,
            args=(ctx, txt_data, frames, Path(video_path), int(active_start_frame), float(analysis_fps)),
            daemon=True,
        )
        thread.start()

    def _run_debug_artifact_job(
        self,
        ctx: MatchContext,
        txt_data: str,
        frames: List[FrameTimestamp],
        video_path: Path,
        active_start_frame: int,
        analysis_fps: float,
    ) -> None:
        debug_root = self._debug_artifacts_root(ctx)
        phrase_dir = debug_root / "active_phrase"
        output_dir = debug_root / "analysis"
        active_video = phrase_dir / f"segment_{ctx.match_id}_active.mp4"
        active_txt = phrase_dir / f"{ctx.match_id}_active_signals.txt"
        debug_log = debug_root / "debug_artifacts.log"
        manifest_path = debug_root / "debug_artifacts.json"
        started_at = epoch_ms()
        offset_s = _active_start_offset_s(active_start_frame=active_start_frame, analysis_fps=analysis_fps)

        try:
            if self.debug_artifact_delay_s > 0:
                time.sleep(self.debug_artifact_delay_s)
            phrase_dir.mkdir(parents=True, exist_ok=True)
            output_dir.mkdir(parents=True, exist_ok=True)
            self._write_debug_manifest(
                manifest_path,
                {
                    "status": "running",
                    "match_id": ctx.match_id,
                    "started_at": started_at,
                    "active_start_offset_s": offset_s,
                    "source_video": str(video_path),
                    "active_video": str(active_video),
                    "active_txt": str(active_txt),
                    "analysis_dir": str(output_dir),
                },
            )
            print(f"[PISTELINK] Debug artifacts started -> {debug_root}", flush=True)

            active_txt.write_text(txt_data, encoding="utf-8")
            _write_active_video_clip(
                input_video=video_path,
                output_video=active_video,
                offset_s=offset_s,
                timeout_s=max(30.0, self.debug_artifact_timeout_s),
            )
            shutdown_shared_local_analyzer()
            stdout, stderr = self._run_debug_reprocess(phrase_dir=phrase_dir, output_dir=output_dir)
            debug_log.write_text(
                "STDOUT\n"
                "------\n"
                f"{stdout}\n\n"
                "STDERR\n"
                "------\n"
                f"{stderr}\n",
                encoding="utf-8",
            )

            overlay_path = output_dir / f"{active_video.stem}_limb_interp_overlay.mp4"
            fps30_debug_path = output_dir / "debug_referee_fps30.txt"
            result_path = output_dir / "analysis_result_limb_interp_experimental.json"
            stable_overlay = debug_root / "keypoints_overlay_active.mp4"
            stable_debug_txt = debug_root / "debug.txt"
            if overlay_path.exists():
                _replace_link(overlay_path, stable_overlay)
            if fps30_debug_path.exists():
                _replace_link(fps30_debug_path, stable_debug_txt)

            self._write_debug_manifest(
                manifest_path,
                {
                    "status": "complete",
                    "match_id": ctx.match_id,
                    "started_at": started_at,
                    "finished_at": epoch_ms(),
                    "active_start_offset_s": offset_s,
                    "source_video": str(video_path),
                    "active_video": str(active_video),
                    "active_txt": str(active_txt),
                    "analysis_dir": str(output_dir),
                    "overlay_video": str(overlay_path) if overlay_path.exists() else None,
                    "debug_txt": str(fps30_debug_path) if fps30_debug_path.exists() else None,
                    "stable_overlay_video": str(stable_overlay) if stable_overlay.exists() else None,
                    "stable_debug_txt": str(stable_debug_txt) if stable_debug_txt.exists() else None,
                    "result_json": str(result_path) if result_path.exists() else None,
                    "log": str(debug_log),
                },
            )
            print(
                f"[PISTELINK] Debug artifacts complete -> overlay={stable_overlay} debug_txt={stable_debug_txt}",
                flush=True,
            )
        except Exception as exc:
            try:
                debug_root.mkdir(parents=True, exist_ok=True)
                with debug_log.open("a", encoding="utf-8") as handle:
                    handle.write(f"debug artifact job failed: {exc}\n")
                    handle.write(traceback.format_exc())
                    handle.write("\n")
                self._write_debug_manifest(
                    manifest_path,
                    {
                        "status": "error",
                        "match_id": ctx.match_id,
                        "started_at": started_at,
                        "finished_at": epoch_ms(),
                        "active_start_offset_s": offset_s,
                        "source_video": str(video_path),
                        "active_video": str(active_video),
                        "active_txt": str(active_txt),
                        "analysis_dir": str(output_dir),
                        "error": str(exc),
                        "log": str(debug_log),
                    },
                )
            except Exception:
                pass
            self._log_context_error(ctx, "debug artifact job failed", exc)

    def _run_debug_reprocess(self, *, phrase_dir: Path, output_dir: Path) -> Tuple[str, str]:
        config = self.analyzer_config
        if config.model_path is None:
            raise RuntimeError("debug artifact job requires a YOLO model path")
        script_path = config.bundle_root / "scripts" / "reprocess_phrase_limb_interp_jumpsafe_experimental.py"
        if not script_path.exists():
            raise RuntimeError(f"debug artifact script not found: {script_path}")

        cmd = [
            str(config.python_executable),
            str(script_path),
            "--phrase-dir",
            str(phrase_dir),
            "--model-path",
            str(config.model_path),
            "--output-dir",
            str(output_dir),
            "--fisheye-backend",
            config.fisheye_backend,
            "--yolo-conf",
            str(config.yolo_conf),
            "--yolo-imgsz",
            str(config.yolo_imgsz),
            "--bootstrap-frames",
            str(config.bootstrap_frames),
            "--video-reader",
            "opencv",
            "--write-repaired-overlay",
            "--write-fps30-debug-log",
        ]
        if config.yolo_half:
            cmd.append("--yolo-half")
        if config.yolo_verbose:
            cmd.append("--yolo-verbose")

        result = subprocess.run(
            cmd,
            cwd=str(config.bundle_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=self.debug_artifact_timeout_s,
        )
        if result.returncode != 0:
            tail = (result.stderr or result.stdout or "unknown debug artifact error")[-4000:]
            raise RuntimeError(f"debug artifact analyzer failed: {tail}")
        return result.stdout or "", result.stderr or ""

    @staticmethod
    def _write_debug_manifest(path: Path, payload: Dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _ensure_active_analysis_video(self, ctx: MatchContext, *, active_start_frame: int, analysis_fps: float) -> None:
        if ctx.active_video_path.exists() and ctx.active_video_path.stat().st_size > 0:
            return
        _write_active_video_clip(
            input_video=ctx.avi_path,
            output_video=ctx.active_video_path,
            offset_s=_active_start_offset_s(active_start_frame=active_start_frame, analysis_fps=analysis_fps),
            timeout_s=max(30.0, self.debug_artifact_timeout_s),
        )

    def _decide_result(
        self,
        ctx: MatchContext,
        analysis_result: Optional[Dict[str, Any]],
    ) -> Tuple[str, int, str]:
        final_lights = _final_lights_ab(ctx.terminal_signal, ctx.side_map)
        lit = {side for side, enabled in final_lights.items() if enabled}

        if len(lit) == 0:
            return "tie", 0, "final_lights_no_touch"
        if len(lit) == 1:
            side = next(iter(lit))
            return side, _result_code_for_ab(side), "final_lights_single_touch"

        if _weapon_code(ctx.weapon) == 2:
            return "tie", 10, "final_lights_double_touch_epee"

        analysis_side = _analysis_winner_to_ab(analysis_result, ctx.side_map)
        if analysis_side in {"A", "B"}:
            return analysis_side, _result_code_for_ab(analysis_side), "local_streaming_analyzer"
        return "tie", 10, "final_lights_double_touch"

    def _match_for_message(self, message: Dict[str, Any], payload: Dict[str, Any]) -> Optional[MatchContext]:
        match_id = str(message.get("match_id") or payload.get("match_id") or "")
        with self._state_lock:
            ctx = self._active_match
        if ctx is None:
            print(f"[PISTELINK] ignoring {message.get('type')} with no active match")
            return None
        if ctx.cancelled:
            print(f"[PISTELINK] ignoring {message.get('type')} for cancelled match {ctx.match_id!r}")
            return None
        if match_id and ctx.match_id != match_id:
            print(f"[PISTELINK] ignoring message for {match_id!r}; active match is {ctx.match_id!r}")
            return None
        return ctx

    def _context_cancelled(self, ctx: MatchContext) -> bool:
        with self._state_lock:
            return ctx.cancelled or self._active_match is not ctx

    def _clear_active_context(self, ctx: MatchContext) -> None:
        with self._state_lock:
            if self._active_match is ctx:
                self._active_match = None

    def _cancel_active_recording(self, reason: str) -> None:
        with self._state_lock:
            ctx = self._active_match
            if ctx is None or ctx.finalizing:
                return
            self._cancel_context_locked(ctx, reason)
            if self._active_match is ctx:
                self._active_match = None

    def _cancel_context_locked(self, ctx: MatchContext, reason: str) -> None:
        ctx.cancelled = True
        try:
            if ctx.analyzer is not None:
                ctx.analyzer.cancel(reason)
        except Exception:
            pass
        try:
            if ctx.camera is not None:
                ctx.camera.release()
        except Exception:
            pass
        self._discard_context_artifacts(ctx)

    def _cleanup_failed_start(self, ctx: MatchContext) -> None:
        with self._state_lock:
            if self._active_match is ctx:
                self._active_match = None
        try:
            if ctx.analyzer is not None:
                ctx.analyzer.cancel("camera_start_error")
        except Exception:
            pass
        try:
            if ctx.camera is not None:
                ctx.camera.release()
        except Exception:
            pass

    def _discard_context_artifacts(self, ctx: MatchContext) -> None:
        for path in (ctx.avi_path, ctx.mp4_path, ctx.signal_txt_path, ctx.frame_timestamps_path, ctx.signal_mapping_path):
            try:
                if path.exists():
                    path.unlink()
            except Exception:
                pass
        try:
            if ctx.ai_dir.exists():
                shutil.rmtree(ctx.ai_dir)
        except Exception:
            pass

    def _send(
        self,
        msg_type: str,
        payload: Optional[Dict[str, Any]] = None,
        match_id: Optional[str] = None,
    ) -> None:
        with self._client_lock:
            client = self._client
            if client is None:
                print(f"[PISTELINK] dropped outbound {msg_type}: no backend connection")
                return
            msg_id = self._next_outbound_id
            self._next_outbound_id += 1
            message = make_message(msg_type, msg_id, payload=payload, match_id=match_id)
            try:
                client.write_message(message)
                self._last_sent = time.monotonic()
            except OSError as exc:
                print(f"[PISTELINK] failed to send {msg_type}: {exc}")
                self._client = None

    def _log_context_error(self, ctx: MatchContext, label: str, exc: BaseException) -> None:
        try:
            ctx.ai_dir.mkdir(parents=True, exist_ok=True)
            with ctx.service_log_path.open("a", encoding="utf-8") as handle:
                handle.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {label}: {exc}\n")
                handle.write(traceback.format_exc())
                handle.write("\n")
        except Exception:
            pass
        print(f"[PISTELINK] {label}: {exc}")


def _payload_ts(payload: Dict[str, Any], message: Dict[str, Any]) -> int:
    for key in ("ts", "begin_ts", "voice_end_ts", "signal_ts"):
        value = payload.get(key)
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                pass
    return int(message.get("ts") or epoch_ms())


def _normalise_signal_payload(payload: Dict[str, Any], message: Dict[str, Any]) -> Dict[str, Any]:
    signal_payload = dict(payload)
    try:
        signal_payload["signal_ts"] = int(signal_payload.get("signal_ts"))
    except (TypeError, ValueError):
        signal_payload["signal_ts"] = int(message.get("ts") or epoch_ms())
    try:
        signal_payload["signal_mono_ns"] = int(signal_payload.get("signal_mono_ns"))
    except (TypeError, ValueError):
        signal_payload["signal_mono_ns"] = message.get("ts_mono_ns", message.get("mono_ns"))
    return signal_payload


def _final_lights_ab(terminal_signal: Optional[Dict[str, Any]], side_map: Dict[str, str]) -> Dict[str, bool]:
    result = {"A": False, "B": False}
    if not terminal_signal or not isinstance(terminal_signal.get("final_lights"), dict):
        return result

    raw = terminal_signal["final_lights"]
    for side in ("A", "B"):
        if side in raw:
            result[side] = bool(raw[side])

    inverse = {v: k for k, v in side_map.items() if v in {"left", "right"}}
    for visual_side in ("left", "right"):
        if visual_side in raw and visual_side in inverse:
            result[inverse[visual_side]] = bool(raw[visual_side])
    return result


def _analysis_winner_to_ab(
    analysis_result: Optional[Dict[str, Any]],
    side_map: Dict[str, str],
) -> Optional[str]:
    if not analysis_result:
        return None
    winner = analysis_result.get("winner")
    if not isinstance(winner, str):
        return None
    winner = winner.lower()
    if winner in {"a", "b"}:
        return winner.upper()
    if winner in {"left", "right"}:
        for ab_side, visual_side in side_map.items():
            if visual_side == winner:
                return ab_side
    return None


def _result_code_for_ab(side: str) -> int:
    if side == "A":
        return 8
    if side == "B":
        return 9
    return 0


_SIDE_ZH = {
    "left": "左方",
    "right": "右方",
}

_SIDE_REASON_ZH = {
    "single_light": "{side}得分。单灯有效。",
    "opponent_pause": "{side}得分。对方停顿，{side}保有进攻权。",
    "opponent_retreat": "{side}得分。对方后退，{side}保有进攻权。",
    "opponent_late_lunge": "{side}得分。对方进攻过晚，失去优先权。",
    "opponent_arm_reset": "{side}得分。对方手臂动作中断，失去优先权。",
    "blade_favored": "{side}得分。剑身动作取得优先权。",
    "blade_override": "{side}得分。剑身动作改变了优先权。",
    "blade_unavailable_fallback": "{side}得分。剑身分析不可用，按优先权时间线判定。",
    "arm_first": "{side}得分。双方同时进攻，{side}先伸臂。",
    "lunge_first": "{side}得分。双方同时进攻，{side}先完成进攻动作。",
    "attack_first": "{side}得分。双方同时进攻，{side}启动更早。",
    "slow_start": "{side}得分。对方启动较慢。",
    "faster_attack": "{side}得分。{side}进攻更快。",
    "system_tiebreak_right": "右方得分。双方优先权事件同时结束，系统判右方。",
    "analysis_reason_unknown": "{side}得分。系统根据视觉分析判定优先权。",
}

_NEUTRAL_REASON_ZH = {
    "no_touch": "无有效触灯。",
    "epee_double_touch": "重剑双灯，双方得分。",
    "unclear_double_touch": "双方触灯，无法判定优先权。",
    "analysis_unavailable": "双方触灯，视觉分析不可用，按双灯处理。",
    "analysis_no_winner": "双方触灯，视觉分析未能给出有效胜方。",
}


def _build_reason_metadata(
    *,
    ctx: MatchContext,
    winner: str,
    decision_source: str,
    analysis_result: Optional[Dict[str, Any]],
    processing_error: Optional[str],
) -> Dict[str, Any]:
    visual_side = _winner_visual_side(winner, ctx.side_map)
    reason_code = _reason_code_for_result(
        decision_source=decision_source,
        analysis_result=analysis_result,
        winner_visual_side=visual_side,
        processing_error=processing_error,
    )
    spoken_reason = _spoken_reason_zh(reason_code, visual_side)
    audio_key = _reason_audio_key(reason_code, visual_side)

    payload: Dict[str, Any] = {
        "reason_code": reason_code,
        "reason_audio_key": audio_key,
    }
    if spoken_reason:
        payload["spoken_reason_zh"] = spoken_reason
        payload["natural_language_reason"] = spoken_reason
    return payload


def _reason_code_for_result(
    *,
    decision_source: str,
    analysis_result: Optional[Dict[str, Any]],
    winner_visual_side: Optional[str],
    processing_error: Optional[str],
) -> str:
    if decision_source == "final_lights_no_touch":
        return "no_touch"
    if decision_source == "final_lights_single_touch":
        return "single_light"
    if decision_source == "final_lights_double_touch_epee":
        return "epee_double_touch"
    if decision_source == "final_lights_double_touch":
        if processing_error:
            return "analysis_unavailable"
        if analysis_result is not None:
            return "analysis_no_winner"
        return "unclear_double_touch"
    if decision_source != "local_streaming_analyzer":
        return "analysis_reason_unknown" if winner_visual_side else "analysis_unavailable"
    return _reason_code_from_analysis(analysis_result, winner_visual_side)


def _reason_code_from_analysis(
    analysis_result: Optional[Dict[str, Any]],
    winner_visual_side: Optional[str],
) -> str:
    if not isinstance(analysis_result, dict) or winner_visual_side not in {"left", "right"}:
        return "analysis_no_winner"

    raw_reason = str(analysis_result.get("reason") or "").lower()
    if "blade contact analysis unavailable" in raw_reason:
        return "blade_unavailable_fallback"
    if "non-accident blade contact overrode" in raw_reason:
        return "blade_override"
    if "non-accident blade contact favored" in raw_reason:
        return "blade_favored"

    priority_events = analysis_result.get("priority_events")
    latest_event = priority_events.get("latest") if isinstance(priority_events, dict) else None
    if isinstance(latest_event, list):
        return "system_tiebreak_right"
    if isinstance(latest_event, dict):
        event_side = str(latest_event.get("side") or "").lower()
        event_type = str(latest_event.get("type") or "").lower()
        if event_side and event_side != winner_visual_side:
            if event_type == "pause":
                return "opponent_pause"
            if event_type == "retreat":
                return "opponent_retreat"
            if event_type == "harmful_lunge":
                return "opponent_late_lunge"
            if event_type == "harmful_arm_extension":
                return "opponent_arm_reset"

    if "near-hit arm extension" in raw_reason:
        return "arm_first"
    if "beneficial lunge" in raw_reason:
        return "lunge_first"
    if "weighted signal start is earlier" in raw_reason:
        return "attack_first"
    if "slow-start penalty" in raw_reason:
        return "slow_start"
    if "faster" in raw_reason:
        return "faster_attack"
    return "analysis_reason_unknown"


def _winner_visual_side(winner: str, side_map: Dict[str, str]) -> Optional[str]:
    normalized = str(winner or "").strip()
    if normalized in {"A", "B"}:
        visual_side = side_map.get(normalized)
        if visual_side in {"left", "right"}:
            return visual_side
    lowered = normalized.lower()
    if lowered in {"left", "right"}:
        return lowered
    return None


def _spoken_reason_zh(reason_code: str, visual_side: Optional[str]) -> str:
    if visual_side in {"left", "right"} and reason_code in _SIDE_REASON_ZH:
        return _SIDE_REASON_ZH[reason_code].format(side=_SIDE_ZH[visual_side])
    return _NEUTRAL_REASON_ZH.get(reason_code, "")


def _reason_audio_key(reason_code: str, visual_side: Optional[str]) -> str:
    if visual_side in {"left", "right"} and reason_code in _SIDE_REASON_ZH:
        return f"reason_{reason_code}_{visual_side}"
    return f"reason_{reason_code}"


def _weapon_code(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        pass
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"foil", "floret", "花剑"}:
            return 1
        if normalized in {"epee", "épée", "重剑"}:
            return 2
        if normalized in {"sabre", "saber", "佩剑"}:
            return 3
    return None


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _run_transcode(input_avi: Path, output_mp4: Path, holder: Dict[str, Any]) -> None:
    try:
        holder["path"] = str(transcode_avi_to_mp4(input_avi, output_mp4))
    except Exception as exc:
        holder["error"] = str(exc)


def _analysis_start_frame(ctx: MatchContext, frames: List[FrameTimestamp]) -> int:
    if not frames:
        return 0
    if ctx.analysis_start_frame is not None:
        return max(0, min(int(ctx.analysis_start_frame), len(frames)))
    if ctx.voice_end_ts is None:
        return 0
    for frame in frames:
        if frame.ts >= ctx.voice_end_ts:
            return max(0, min(int(frame.frame), len(frames)))
    return len(frames)


def _rebased_frame_timestamps(frames: List[FrameTimestamp]) -> List[FrameTimestamp]:
    if not frames:
        return []
    base_frame = int(frames[0].frame)
    return [
        FrameTimestamp(frame=max(0, int(frame.frame) - base_frame), ts=frame.ts, mono_ns=frame.mono_ns)
        for frame in frames
    ]


def _active_start_offset_s(*, active_start_frame: int, analysis_fps: float) -> float:
    if analysis_fps <= 0:
        analysis_fps = 30.0
    return max(0.0, int(active_start_frame) / float(analysis_fps))


_TXT_TIMESTAMP_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)s(\s*\|.*)$")


def _write_active_legacy_txt(txt_data: str, active_start_offset_s: float, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    lines: List[str] = []
    for line in txt_data.splitlines():
        match = _TXT_TIMESTAMP_RE.match(line)
        if match is None:
            lines.append(line)
            continue
        shifted = max(0.0, float(match.group(1)) - active_start_offset_s)
        lines.append(f"{shifted:7.3f}s{match.group(2)}")
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_active_video_clip(*, input_video: Path, output_video: Path, offset_s: float, timeout_s: float) -> None:
    output_video.parent.mkdir(parents=True, exist_ok=True)
    if output_video.exists():
        output_video.unlink()
    if offset_s <= 0.001:
        shutil.copy2(input_video, output_video)
        return

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is not None:
        cmd = [
            ffmpeg,
            "-y",
            "-i",
            str(input_video),
            "-ss",
            f"{offset_s:.3f}",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "28",
            "-movflags",
            "+faststart",
            "-an",
            str(output_video),
        ]
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout_s)
        if result.returncode == 0 and output_video.exists() and output_video.stat().st_size > 0:
            return

    _write_active_video_clip_with_opencv(input_video=input_video, output_video=output_video, offset_s=offset_s)


def _write_active_video_clip_with_opencv(*, input_video: Path, output_video: Path, offset_s: float) -> None:
    import cv2  # type: ignore

    capture = cv2.VideoCapture(str(input_video))
    if not capture.isOpened():
        raise RuntimeError(f"OpenCV could not open debug source video: {input_video}")
    try:
        fps = float(capture.get(cv2.CAP_PROP_FPS) or 30.0)
        width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        if width <= 0 or height <= 0:
            raise RuntimeError(f"OpenCV could not read debug source dimensions: {input_video}")

        start_frame = max(0, int(round(offset_s * fps)))
        capture.set(cv2.CAP_PROP_POS_FRAMES, start_frame)

        writer = cv2.VideoWriter(
            str(output_video),
            cv2.VideoWriter_fourcc(*"mp4v"),
            fps,
            (width, height),
        )
        if not writer.isOpened():
            raise RuntimeError(f"OpenCV could not open debug clip writer: {output_video}")
        try:
            written = 0
            while True:
                ok, frame = capture.read()
                if not ok:
                    break
                writer.write(frame)
                written += 1
        finally:
            writer.release()
        if written <= 0:
            raise RuntimeError(f"debug clip is empty after offset {offset_s:.3f}s: {input_video}")
    finally:
        capture.release()


def _replace_link(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        if target.exists() or target.is_symlink():
            target.unlink()
        target.symlink_to(source)
    except OSError:
        shutil.copy2(source, target)


def _unlink_quietly(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except Exception:
        pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the PisteLink AI low-latency service")
    parser.add_argument(
        "--socket-path",
        default=os.environ.get("PISTELINK_AI_SOCKET", DEFAULT_SOCKET_PATH),
        help="Unix socket path, default /run/pistelink/ai.sock",
    )
    parser.add_argument(
        "--match-root",
        default=os.environ.get("PISTELINK_MATCH_ROOT", DEFAULT_MATCH_ROOT),
        help="Directory containing per-match folders",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=os.environ.get("PISTELINK_DRY_RUN", "").lower() in {"1", "true", "yes", "on"},
        help="Exercise the protocol without opening the camera or analyzer",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    service = PisteLinkAIService(
        socket_path=Path(args.socket_path).expanduser(),
        match_root=Path(args.match_root).expanduser(),
        dry_run=bool(args.dry_run),
    )
    try:
        service.serve_forever()
    except KeyboardInterrupt:
        service.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
