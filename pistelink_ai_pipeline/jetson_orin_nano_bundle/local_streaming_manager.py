from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, Optional, Set, Tuple

import cv2
import numpy as np

ECHO_LOCAL_ANALYZER_LOGS = os.environ.get("FENCING_ECHO_LOCAL_ANALYZER_LOGS", "true").lower() in {"1", "true", "yes"}


class _PersistentLocalAnalyzerClient:
    """Own a long-lived local analyzer subprocess and reuse it across phrases."""

    def __init__(
        self,
        *,
        bundle_root: Path,
        python_executable: Path,
        model_path: Optional[Path],
        fisheye_backend: str,
        yolo_conf: float,
        yolo_imgsz: int,
        yolo_half: bool,
        yolo_verbose: bool,
        bootstrap_frames: int,
        startup_timeout: float,
        result_timeout: float,
    ) -> None:
        self.bundle_root = bundle_root
        self.python_executable = python_executable
        self.model_path = model_path
        self.fisheye_backend = fisheye_backend
        self.yolo_conf = yolo_conf
        self.yolo_imgsz = yolo_imgsz
        self.yolo_half = yolo_half
        self.yolo_verbose = yolo_verbose
        self.bootstrap_frames = bootstrap_frames
        self.startup_timeout = startup_timeout
        self.result_timeout = result_timeout

        self._process: Optional[subprocess.Popen[bytes]] = None
        self._responses: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self._stderr_tail: Deque[str] = deque(maxlen=120)
        self._state_lock = threading.Lock()
        self._session_lock = threading.Lock()
        self._warmed_shapes: Set[Tuple[int, int]] = set()

    def ensure_started(self) -> None:
        with self._state_lock:
            if self._process is not None and self._process.poll() is None:
                return
            self._spawn_locked()

    def warmup(self, width: int, height: int) -> None:
        shape_key = (int(width), int(height))
        self.ensure_started()
        if shape_key in self._warmed_shapes:
            return
        with self._session_lock:
            self._clear_response_queue()
            self._write_json_line({"type": "warmup", "width": int(width), "height": int(height)})
            response = self._wait_for_message(timeout=self.startup_timeout)
            if response.get("type") == "error":
                raise self._build_error(str(response.get("error_message") or "Local analyzer warmup failed"))
            if response.get("type") != "warmup_complete":
                raise self._build_error(f"Unexpected warmup response: {response}")
            self._warmed_shapes.add(shape_key)

    def start_session(
        self,
        *,
        session_id: str,
        phrase_dir: Path,
        output_dir: Optional[Path],
        fps: float,
        width: int,
        height: int,
        expected_frames: int,
    ) -> None:
        self.ensure_started()
        self._session_lock.acquire()
        try:
            self._clear_response_queue()
            payload: Dict[str, Any] = {
                "type": "session_start",
                "session_id": session_id,
                "phrase_dir": str(phrase_dir),
                "fps": fps,
                "width": int(width),
                "height": int(height),
                "expected_frames": int(expected_frames),
            }
            if output_dir is not None:
                payload["output_dir"] = str(output_dir)
            self._write_json_line(payload)
            response = self._wait_for_message(timeout=self.startup_timeout)
            if response.get("type") == "error":
                raise self._build_error(str(response.get("error_message") or "Local analyzer startup failed"))
            if response.get("type") != "ready":
                raise self._build_error(f"Unexpected local analyzer startup response: {response}")
        except Exception:
            self._safe_release_session_lock()
            raise

    def send_frame(self, frame: Any, frame_number: int) -> None:
        if self._process is None or self._process.poll() is not None:
            raise self._build_error("Local analyzer process exited unexpectedly")

        if not isinstance(frame, np.ndarray):
            frame = np.asarray(frame)
        if frame.ndim != 3 or frame.shape[2] != 3:
            raise self._build_error(f"Expected HxWx3 frame, received shape {getattr(frame, 'shape', None)}")
        if frame.dtype != np.uint8:
            frame = frame.astype(np.uint8, copy=False)
        if not frame.flags.c_contiguous:
            frame = np.ascontiguousarray(frame)

        frame_payload = memoryview(frame).cast("B")
        self._write_json_line(
            {
                "type": "frame",
                "frame_number": int(frame_number),
                "encoding": "raw_bgr24",
                "width": int(frame.shape[1]),
                "height": int(frame.shape[0]),
                "size": int(frame.nbytes),
            }
        )
        self._write_bytes(frame_payload)

    def send_encoded_frame(
        self,
        payload: bytes,
        *,
        frame_number: int,
        encoding: str,
        width: int,
        height: int,
    ) -> None:
        if self._process is None or self._process.poll() is not None:
            raise self._build_error("Local analyzer process exited unexpectedly")
        self._write_json_line(
            {
                "type": "frame",
                "frame_number": int(frame_number),
                "encoding": str(encoding),
                "width": int(width),
                "height": int(height),
                "size": int(len(payload)),
            }
        )
        self._write_bytes(payload)

    def end_session(
        self,
        *,
        signal_data: bytes,
        signal_filename: str,
        total_frames: int,
        overflowed: bool,
    ) -> Dict[str, Any]:
        try:
            self._write_json_line(
                {
                    "type": "session_end",
                    "total_frames": int(total_frames),
                    "signal_filename": signal_filename,
                    "signal_size": len(signal_data),
                    "overflowed": bool(overflowed),
                }
            )
            self._write_bytes(signal_data)
            response = self._wait_for_message(timeout=self.result_timeout)
            if response.get("type") == "error":
                raise self._build_error(str(response.get("error_message") or "Local analyzer failed"))
            return response
        finally:
            self._safe_release_session_lock()

    def cancel_session(self, reason: str = "") -> None:
        try:
            self._write_json_line({"type": "cancel_session", "reason": reason or "user_cancelled"})
            try:
                self._wait_for_message(timeout=5.0)
            except Exception:
                pass
        finally:
            self._safe_release_session_lock()

    def close(self) -> None:
        with self._state_lock:
            self._stop_process_locked()

    def _spawn_locked(self) -> None:
        self._stop_process_locked()
        cmd = [
            str(self.python_executable),
            "-m",
            "scripts.live_stream_service",
            "--persistent",
            "--fisheye-backend",
            self.fisheye_backend,
            "--bootstrap-frames",
            str(self.bootstrap_frames),
            "--yolo-conf",
            str(self.yolo_conf),
            "--yolo-imgsz",
            str(self.yolo_imgsz),
        ]
        if self.model_path is not None:
            cmd.extend(["--model-path", str(self.model_path)])
        if self.yolo_half:
            cmd.append("--yolo-half")
        if self.yolo_verbose:
            cmd.append("--yolo-verbose")

        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        process = subprocess.Popen(
            cmd,
            cwd=str(self.bundle_root),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            env=env,
        )
        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None

        stdout_thread = threading.Thread(
            target=self._stdout_reader,
            args=(process.stdout, self._responses, self._stderr_tail),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=self._stderr_reader,
            args=(process.stderr, self._stderr_tail),
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()
        self._process = process

        self._clear_response_queue()
        response = self._wait_for_message(timeout=self.startup_timeout)
        if response.get("type") == "error":
            raise self._build_error(str(response.get("error_message") or "Local analyzer service startup failed"))
        if response.get("type") != "service_ready":
            raise self._build_error(f"Unexpected local analyzer service response: {response}")

    def _stop_process_locked(self) -> None:
        process = self._process
        if process is None:
            return
        if process.poll() is None:
            try:
                self._write_json_line({"type": "shutdown"})
                try:
                    self._wait_for_message(timeout=5.0)
                except Exception:
                    pass
            except Exception:
                pass
            try:
                process.wait(timeout=5.0)
            except Exception:
                process.kill()
                try:
                    process.wait(timeout=2.0)
                except Exception:
                    pass
        self._process = None
        self._clear_response_queue()
        self._warmed_shapes.clear()

    @staticmethod
    def _stdout_reader(
        handle: Any,
        responses: "queue.Queue[Dict[str, Any]]",
        stderr_tail: Deque[str],
    ) -> None:
        try:
            while True:
                line = handle.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                if not text.startswith("{"):
                    stderr_tail.append(f"[stdout] {text}")
                    if ECHO_LOCAL_ANALYZER_LOGS:
                        print(f"[LOCAL_ANALYZER_STREAM] [stdout] {text}", flush=True)
                    continue
                try:
                    payload = json.loads(text)
                except Exception:
                    stderr_tail.append(f"[stdout-invalid-json] {text[:400]}")
                    if ECHO_LOCAL_ANALYZER_LOGS:
                        print(f"[LOCAL_ANALYZER_STREAM] [stdout-invalid-json] {text[:400]}", flush=True)
                    continue
                responses.put(payload)
        finally:
            try:
                handle.close()
            except Exception:
                pass

    @staticmethod
    def _stderr_reader(handle: Any, stderr_tail: Deque[str]) -> None:
        try:
            while True:
                line = handle.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    stderr_tail.append(text)
                    if ECHO_LOCAL_ANALYZER_LOGS:
                        print(f"[LOCAL_ANALYZER_STREAM] {text}", flush=True)
        finally:
            try:
                handle.close()
            except Exception:
                pass

    def _write_json_line(self, payload: Dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None or process.stdin.closed:
            raise self._build_error("Local analyzer stdin is not available")
        process.stdin.write((json.dumps(payload, sort_keys=True) + "\n").encode("utf-8"))
        process.stdin.flush()

    def _write_bytes(self, payload: Any) -> None:
        process = self._process
        if process is None or process.stdin is None or process.stdin.closed:
            raise self._build_error("Local analyzer stdin is not available")
        process.stdin.write(payload)
        process.stdin.flush()

    def _wait_for_message(self, *, timeout: float) -> Dict[str, Any]:
        try:
            return self._responses.get(timeout=timeout)
        except queue.Empty as exc:
            raise self._build_error("Timed out waiting for response from local analyzer") from exc

    def _clear_response_queue(self) -> None:
        while True:
            try:
                self._responses.get_nowait()
            except queue.Empty:
                return

    def _safe_release_session_lock(self) -> None:
        if self._session_lock.locked():
            self._session_lock.release()

    def _build_error(self, message: str) -> RuntimeError:
        if not self._stderr_tail:
            return RuntimeError(message)
        return RuntimeError(f"{message}\nLocal analyzer stderr tail:\n" + "\n".join(self._stderr_tail))


_SHARED_CLIENT: Optional[_PersistentLocalAnalyzerClient] = None
_SHARED_CLIENT_KEY: Optional[Tuple[Any, ...]] = None
_SHARED_CLIENT_LOCK = threading.Lock()


def _shared_client_key(
    *,
    bundle_root: Path,
    python_executable: Path,
    model_path: Optional[Path],
    fisheye_backend: str,
    yolo_conf: float,
    yolo_imgsz: int,
    yolo_half: bool,
    yolo_verbose: bool,
    bootstrap_frames: int,
    startup_timeout: float,
    result_timeout: float,
) -> Tuple[Any, ...]:
    return (
        str(bundle_root.resolve()),
        str(python_executable.resolve()),
        str(model_path.resolve()) if model_path is not None else None,
        fisheye_backend,
        float(yolo_conf),
        int(yolo_imgsz),
        bool(yolo_half),
        bool(yolo_verbose),
        int(bootstrap_frames),
        float(startup_timeout),
        float(result_timeout),
    )


def _get_shared_client(
    *,
    bundle_root: Path,
    python_executable: Path,
    model_path: Optional[Path],
    fisheye_backend: str,
    yolo_conf: float,
    yolo_imgsz: int,
    yolo_half: bool,
    yolo_verbose: bool,
    bootstrap_frames: int,
    startup_timeout: float,
    result_timeout: float,
) -> _PersistentLocalAnalyzerClient:
    global _SHARED_CLIENT, _SHARED_CLIENT_KEY

    key = _shared_client_key(
        bundle_root=bundle_root,
        python_executable=python_executable,
        model_path=model_path,
        fisheye_backend=fisheye_backend,
        yolo_conf=yolo_conf,
        yolo_imgsz=yolo_imgsz,
        yolo_half=yolo_half,
        yolo_verbose=yolo_verbose,
        bootstrap_frames=bootstrap_frames,
        startup_timeout=startup_timeout,
        result_timeout=result_timeout,
    )
    with _SHARED_CLIENT_LOCK:
        if _SHARED_CLIENT is not None and _SHARED_CLIENT_KEY != key:
            _SHARED_CLIENT.close()
            _SHARED_CLIENT = None
            _SHARED_CLIENT_KEY = None
        if _SHARED_CLIENT is None:
            _SHARED_CLIENT = _PersistentLocalAnalyzerClient(
                bundle_root=bundle_root,
                python_executable=python_executable,
                model_path=model_path,
                fisheye_backend=fisheye_backend,
                yolo_conf=yolo_conf,
                yolo_imgsz=yolo_imgsz,
                yolo_half=yolo_half,
                yolo_verbose=yolo_verbose,
                bootstrap_frames=bootstrap_frames,
                startup_timeout=startup_timeout,
                result_timeout=result_timeout,
            )
            _SHARED_CLIENT_KEY = key
        return _SHARED_CLIENT


def ensure_local_analyzer_service(
    *,
    bundle_root: Path,
    python_executable: Path,
    model_path: Optional[Path],
    fisheye_backend: str,
    yolo_conf: float,
    yolo_imgsz: int,
    yolo_half: bool,
    yolo_verbose: bool,
    bootstrap_frames: int,
    startup_timeout: float,
    result_timeout: float,
    width: Optional[int] = None,
    height: Optional[int] = None,
) -> None:
    client = _get_shared_client(
        bundle_root=bundle_root,
        python_executable=python_executable,
        model_path=model_path,
        fisheye_backend=fisheye_backend,
        yolo_conf=yolo_conf,
        yolo_imgsz=yolo_imgsz,
        yolo_half=yolo_half,
        yolo_verbose=yolo_verbose,
        bootstrap_frames=bootstrap_frames,
        startup_timeout=startup_timeout,
        result_timeout=result_timeout,
    )
    client.ensure_started()
    if width is not None and height is not None:
        client.warmup(int(width), int(height))


def shutdown_shared_local_analyzer() -> None:
    global _SHARED_CLIENT, _SHARED_CLIENT_KEY
    with _SHARED_CLIENT_LOCK:
        if _SHARED_CLIENT is not None:
            _SHARED_CLIENT.close()
        _SHARED_CLIENT = None
        _SHARED_CLIENT_KEY = None


class LocalStreamingSessionManager:
    """Send one phrase to the shared local analyzer service."""

    is_local_analyzer = True
    result_label = "local analyzer"
    apply_label = "Local analyzer"

    def __init__(
        self,
        *,
        phrase_dir: Path,
        base_name: str,
        bundle_root: Path,
        python_executable: Path,
        output_dir: Optional[Path],
        model_path: Optional[Path],
        fisheye_backend: str,
        yolo_conf: float,
        yolo_imgsz: int,
        yolo_half: bool,
        yolo_verbose: bool,
        bootstrap_frames: int,
        queue_max: int,
        jpeg_quality: int,
        startup_timeout: float,
        result_timeout: float,
        frame_encoding: str = "raw_bgr24",
    ) -> None:
        self.phrase_dir = phrase_dir
        self.base_name = base_name
        self.bundle_root = bundle_root
        self.python_executable = python_executable
        self.output_dir = output_dir
        self.model_path = model_path
        self.fisheye_backend = fisheye_backend
        self.yolo_conf = yolo_conf
        self.yolo_imgsz = yolo_imgsz
        self.yolo_half = yolo_half
        self.yolo_verbose = yolo_verbose
        self.bootstrap_frames = bootstrap_frames
        self.queue_max = queue_max
        self.jpeg_quality = jpeg_quality
        self.frame_encoding = frame_encoding
        self.startup_timeout = startup_timeout
        self.result_timeout = result_timeout

        self._thread: Optional[threading.Thread] = None
        self._frame_queue: Optional[queue.Queue] = None
        self._active = False
        self._result: Optional[Dict[str, Any]] = None
        self._error: Optional[Exception] = None
        self._overflowed = False
        self._ready_event = threading.Event()
        self._ready_ok = False
        self._frame_gate_lock = threading.Lock()
        self._stream_paused = False
        self._frame_origin = 0
        self._queued_frame_count = 0
        self._noncontiguous_frame_numbers = False

    def start_session(
        self,
        session_id: str,
        fps: float,
        width: int,
        height: int,
        expected_frames: int = 0,
        start_paused: bool = False,
    ) -> bool:
        if not self.begin_session(
            session_id=session_id,
            fps=fps,
            width=width,
            height=height,
            expected_frames=expected_frames,
            start_paused=start_paused,
        ):
            return False

        return self.wait_until_ready(timeout=self.startup_timeout)

    def begin_session(
        self,
        session_id: str,
        fps: float,
        width: int,
        height: int,
        expected_frames: int = 0,
        start_paused: bool = False,
    ) -> bool:
        if self._active:
            print("[LOCAL_ANALYZER] Session already active")
            return False

        self._frame_queue = queue.Queue(maxsize=self.queue_max)
        self._active = True
        self._result = None
        self._error = None
        self._overflowed = False
        self._ready_ok = False
        self._ready_event.clear()
        with self._frame_gate_lock:
            self._stream_paused = bool(start_paused)
            self._frame_origin = 0
            self._queued_frame_count = 0
            self._noncontiguous_frame_numbers = False

        self._thread = threading.Thread(
            target=self._run_session,
            args=(session_id, fps, width, height, expected_frames),
            daemon=True,
        )
        self._thread.start()
        return True

    def wait_until_ready(self, timeout: float, *, fail_on_timeout: bool = True) -> bool:
        if not self._active:
            return False
        if not self._ready_event.wait(timeout=timeout):
            if fail_on_timeout:
                self._error = RuntimeError("Timed out waiting for local analyzer startup")
                self._active = False
            return False
        if self._error is not None:
            self._active = False
            return False
        return self._ready_ok

    def queue_frame(self, frame: Any, frame_number: int) -> bool:
        if not self._active or self._frame_queue is None:
            return False
        if self._overflowed:
            return False
        with self._frame_gate_lock:
            if self._stream_paused:
                return True
            analysis_frame_number = int(frame_number) - int(self._frame_origin)
            if analysis_frame_number < 0:
                return True
            if analysis_frame_number != self._queued_frame_count:
                self._noncontiguous_frame_numbers = True
            self._queued_frame_count += 1
        try:
            item = self._prepare_frame_item(frame, analysis_frame_number)
        except Exception as exc:
            if not self._overflowed:
                print(f"[LOCAL_ANALYZER] WARNING: failed to prepare live frame, falling back offline: {exc}")
            self._overflowed = True
            return False
        try:
            self._frame_queue.put(item, block=True, timeout=0.05)
            return True
        except queue.Full:
            if not self._overflowed:
                print("[LOCAL_ANALYZER] WARNING: queue full, live analysis will fall back to offline processing")
            self._overflowed = True
            return False

    def _prepare_frame_item(self, frame: Any, frame_number: int) -> Tuple[Any, int, str, int, int]:
        if not isinstance(frame, np.ndarray):
            frame = np.asarray(frame)
        if frame.ndim != 3 or frame.shape[2] != 3:
            raise ValueError(f"Expected HxWx3 frame, received shape {getattr(frame, 'shape', None)}")
        if frame.dtype != np.uint8:
            frame = frame.astype(np.uint8, copy=False)

        encoding = (self.frame_encoding or "raw_bgr24").strip().lower()
        height, width = int(frame.shape[0]), int(frame.shape[1])
        if encoding in {"jpeg", "jpg"}:
            ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, int(self.jpeg_quality)])
            if not ok:
                raise RuntimeError("cv2.imencode(.jpg) failed")
            return encoded.tobytes(), frame_number, "jpeg", width, height
        if encoding == "png":
            ok, encoded = cv2.imencode(".png", frame)
            if not ok:
                raise RuntimeError("cv2.imencode(.png) failed")
            return encoded.tobytes(), frame_number, "png", width, height
        if encoding in {"raw", "raw_bgr24"}:
            if not frame.flags.c_contiguous:
                frame = np.ascontiguousarray(frame)
            return frame, frame_number, "raw_bgr24", width, height
        raise ValueError(f"Unsupported local analyzer frame encoding '{self.frame_encoding}'")

    def end_session(self, signal_data: bytes, signal_filename: str, total_frames: int) -> None:
        if not self._active or self._frame_queue is None:
            print("[LOCAL_ANALYZER] end_session called but session not active")
            return
        if self._overflowed:
            dropped = self._drop_pending_frame_items()
            if dropped:
                print(f"[LOCAL_ANALYZER] Dropped {dropped} queued live-analysis frame(s) before offline fallback")
        try:
            self._frame_queue.put(
                ("END_SESSION", total_frames, signal_data, signal_filename),
                block=True,
                timeout=5.0,
            )
        except queue.Full:
            self._overflowed = True
            self._error = RuntimeError("Timed out queueing END_SESSION for local analyzer")

    def cancel_session(self, reason: str = "") -> None:
        if not self._active or self._frame_queue is None:
            return
        self._drop_all_pending_items()
        try:
            self._frame_queue.put(("CANCEL_SESSION", reason or "user_cancelled"), block=False)
        except queue.Full:
            self._overflowed = True

    def activate_frame_stream(self, start_frame_number: int) -> None:
        with self._frame_gate_lock:
            self._frame_origin = max(0, int(start_frame_number))
            self._queued_frame_count = 0
            self._noncontiguous_frame_numbers = False
            self._stream_paused = False
        print(f"[LOCAL_ANALYZER] Active frame stream starts at capture frame {self._frame_origin}", flush=True)

    def frame_origin(self) -> int:
        with self._frame_gate_lock:
            return int(self._frame_origin)

    def queued_frame_count(self) -> int:
        with self._frame_gate_lock:
            return int(self._queued_frame_count)

    def live_degraded(self, expected_total_frames: int) -> bool:
        with self._frame_gate_lock:
            return (
                bool(self._overflowed)
                or bool(self._noncontiguous_frame_numbers)
                or int(self._queued_frame_count) != int(expected_total_frames)
            )

    def _drop_all_pending_items(self) -> int:
        if self._frame_queue is None:
            return 0
        dropped = 0
        while True:
            try:
                self._frame_queue.get_nowait()
            except queue.Empty:
                return dropped
            dropped += 1

    def _drop_pending_frame_items(self) -> int:
        if self._frame_queue is None:
            return 0
        retained: list[Any] = []
        dropped = 0
        while True:
            try:
                item = self._frame_queue.get_nowait()
            except queue.Empty:
                break
            if isinstance(item, tuple) and item and isinstance(item[0], str):
                retained.append(item)
            else:
                dropped += 1
        for item in retained:
            try:
                self._frame_queue.put_nowait(item)
            except queue.Full:
                break
        return dropped

    def get_result(self, timeout: float = 300.0) -> Optional[Dict[str, Any]]:
        if self._thread:
            self._thread.join(timeout=timeout)
        if self._thread and self._thread.is_alive():
            raise TimeoutError("Timed out waiting for local analyzer result")
        if self._error is not None:
            raise self._error
        return self._result

    def is_active(self) -> bool:
        return self._active

    def _run_session(
        self,
        session_id: str,
        fps: float,
        width: int,
        height: int,
        expected_frames: int,
    ) -> None:
        client: Optional[_PersistentLocalAnalyzerClient] = None
        client_session_open = False
        try:
            client = _get_shared_client(
                bundle_root=self.bundle_root,
                python_executable=self.python_executable,
                model_path=self.model_path,
                fisheye_backend=self.fisheye_backend,
                yolo_conf=self.yolo_conf,
                yolo_imgsz=self.yolo_imgsz,
                yolo_half=self.yolo_half,
                yolo_verbose=self.yolo_verbose,
                bootstrap_frames=self.bootstrap_frames,
                startup_timeout=self.startup_timeout,
                result_timeout=self.result_timeout,
            )
            client.warmup(int(width), int(height))
            client.start_session(
                session_id=session_id,
                phrase_dir=self.phrase_dir,
                output_dir=self.output_dir,
                fps=fps,
                width=width,
                height=height,
                expected_frames=expected_frames,
            )
            client_session_open = True
            self._ready_ok = True
            self._ready_event.set()

            while True:
                if self._frame_queue is None:
                    raise RuntimeError("Local analyzer frame queue disappeared")
                try:
                    item = self._frame_queue.get(timeout=1.0)
                except queue.Empty:
                    continue

                if isinstance(item, tuple) and item and isinstance(item[0], str):
                    marker = item[0]
                    if marker == "CANCEL_SESSION":
                        client.cancel_session(item[1] if len(item) > 1 else "user_cancelled")
                        client_session_open = False
                        break
                    if marker == "END_SESSION":
                        total_frames = int(item[1])
                        signal_data = item[2]
                        signal_filename = item[3]
                        try:
                            final_response = client.end_session(
                                signal_data=signal_data,
                                signal_filename=signal_filename,
                                total_frames=total_frames,
                                overflowed=self._overflowed,
                            )
                        finally:
                            client_session_open = False
                        if final_response.get("type") == "result":
                            result = final_response.get("result")
                            if not isinstance(result, dict):
                                raise RuntimeError("Local analyzer returned a non-dict result")
                            self._result = result
                        elif final_response.get("type") != "cancelled":
                            raise RuntimeError(f"Unexpected local analyzer final response: {final_response}")
                        break

                frame_payload, frame_number, encoding, width, height = item
                if encoding == "raw_bgr24":
                    client.send_frame(frame_payload, int(frame_number))
                else:
                    client.send_encoded_frame(
                        frame_payload,
                        frame_number=int(frame_number),
                        encoding=encoding,
                        width=int(width),
                        height=int(height),
                    )
        except Exception as exc:
            if client_session_open and client is not None:
                try:
                    client.cancel_session("local_session_error")
                except Exception:
                    try:
                        client.close()
                    except Exception:
                        pass
            self._error = exc
        finally:
            self._ready_event.set()
            self._active = False
