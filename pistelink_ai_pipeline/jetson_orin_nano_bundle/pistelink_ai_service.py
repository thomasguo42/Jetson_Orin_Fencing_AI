"""PisteLink AI Unix-socket service for the copied low-latency pipeline."""

from __future__ import annotations

import argparse
import grp
import os
import pwd
import shutil
import socket
import stat
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from pistelink_analysis_adapter import PisteLinkAnalyzerSession, default_analyzer_config
from pistelink_camera_recorder import PisteLinkCameraRecorder, default_camera_settings, transcode_avi_to_mp4
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

    def serve_forever(self) -> None:
        self._prepare_socket_path()
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

    def _prepare_socket_path(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        if self.socket_path.exists():
            mode = self.socket_path.stat().st_mode
            if not stat.S_ISSOCK(mode):
                raise RuntimeError(f"socket path exists and is not a socket: {self.socket_path}")
            self.socket_path.unlink()

    def _secure_socket_file(self) -> None:
        os.chmod(self.socket_path, 0o600)
        try:
            uid = pwd.getpwnam("nvidia").pw_uid
            gid = grp.getgrnam("nvidia").gr_gid
            os.chown(self.socket_path, uid, gid)
        except Exception:
            pass

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
            with self._client_lock:
                if self._client is client:
                    self._client = None
                if self._client_socket is conn:
                    self._client_socket = None
            self._close_socket(conn)
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
            fps_nominal, configured_width, configured_height = default_camera_settings()
            ctx.analyzer = PisteLinkAnalyzerSession(self.analyzer_config, ctx.match_dir, ctx.match_id)
            if not ctx.analyzer.start(fps_nominal, configured_width, configured_height):
                raise RuntimeError("local analyzer did not become ready")
            if not self._is_current_active_match(ctx):
                self._cancel_context_locked(ctx, "match_replaced_before_camera_start")
                return

            ctx.camera = PisteLinkCameraRecorder()
            ctx.recording_start_ts = epoch_ms()
            if not ctx.camera.start(ctx.avi_path, ctx.analyzer.manager):
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
                if ctx.analyzer is not None and needs_visual_row:
                    try:
                        signal_filename = str(ctx.signal_txt_path.relative_to(ctx.match_dir))
                        analysis_result = ctx.analyzer.end(
                            signal_data=txt_data.encode("utf-8"),
                            signal_filename=signal_filename,
                            total_frames=len(frames),
                        )
                    except Exception as exc:
                        processing_error = str(exc)
                elif ctx.analyzer is not None:
                    ctx.analyzer.cancel("visual_analysis_not_required")

                transcode_thread.join()
                if transcode_holder.get("error"):
                    transcode_error = str(transcode_holder["error"])
                    video_path = ctx.avi_path
                else:
                    video_path = Path(transcode_holder.get("path") or ctx.mp4_path)
                    _unlink_quietly(ctx.avi_path)

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
            result_payload = self._build_match_result(
                ctx=ctx,
                analysis_result=None,
                video_path=video_path if video_path.exists() else ctx.avi_path,
                transcode_error=transcode_error,
                processing_error=str(exc),
            )

        self._send("match_result", result_payload, match_id=ctx.match_id)
        with self._state_lock:
            if self._active_match is ctx:
                self._active_match = None

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
        if match_id and ctx.match_id != match_id:
            print(f"[PISTELINK] ignoring message for {match_id!r}; active match is {ctx.match_id!r}")
            return None
        return ctx

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


def _run_transcode(input_avi: Path, output_mp4: Path, holder: Dict[str, Any]) -> None:
    try:
        holder["path"] = str(transcode_avi_to_mp4(input_avi, output_mp4))
    except Exception as exc:
        holder["error"] = str(exc)


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
