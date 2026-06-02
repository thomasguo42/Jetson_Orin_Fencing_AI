# Local Streaming Analyzer

This document explains how the current local streaming path works in `jetson_orin_nano_bundle` and `portable_fencing_pipeline_low_latency_streaming`.

It describes the implementation that is running now, not the older remote-Pi design.

## Goal

The purpose of the local streaming analyzer is to start vision processing while the phrase is still being recorded.

Instead of waiting until the AVI and TXT are fully written and then starting the whole pipeline, the Jetson:

1. records the phrase locally,
2. streams each captured frame to a persistent local analyzer process,
3. sends the final TXT signal log when the phrase ends,
4. receives a final judging result with reduced post-stop latency.

The system still keeps the local AVI and TXT as the source of truth.

## Main Components

### Jetson UI and Recorder

The entrypoint is [control_fencing.py](/home/thomas/fencing/jetson_orin_nano_bundle/control_fencing.py).

Important areas:

- streaming configuration and environment variables:
  [control_fencing.py](/home/thomas/fencing/jetson_orin_nano_bundle/control_fencing.py#L121)
- local analyzer warmup:
  [control_fencing.py](/home/thomas/fencing/jetson_orin_nano_bundle/control_fencing.py#L243)
- native GStreamer camera capture:
  [control_fencing.py](/home/thomas/fencing/jetson_orin_nano_bundle/control_fencing.py#L1581)
- recorder threads and queues:
  [control_fencing.py](/home/thomas/fencing/jetson_orin_nano_bundle/control_fencing.py#L1754)
- phrase setup:
  [control_fencing.py](/home/thomas/fencing/jetson_orin_nano_bundle/control_fencing.py#L2476)
- phrase stop/finalize:
  [control_fencing.py](/home/thomas/fencing/jetson_orin_nano_bundle/control_fencing.py#L2624)
- UI start-button flow:
  [control_fencing.py](/home/thomas/fencing/jetson_orin_nano_bundle/control_fencing.py#L2898)

### Local Analyzer Session Manager

The Jetson-side bridge to the analyzer service is [local_streaming_manager.py](/home/thomas/fencing/jetson_orin_nano_bundle/local_streaming_manager.py#L478).

It is responsible for:

- connecting to a shared persistent analyzer subprocess,
- starting one phrase session,
- pushing frames to that session,
- sending `session_end` with the final TXT,
- waiting for the final result.

### Streaming Analyzer Service

The analyzer service itself is [live_stream_service.py](/home/thomas/fencing/portable_fencing_pipeline_low_latency_streaming/scripts/live_stream_service.py#L1).

It runs as a local stdin/stdout subprocess and reuses the copied low-latency pipeline.

Important areas:

- raw frame decoding:
  [live_stream_service.py](/home/thomas/fencing/portable_fencing_pipeline_low_latency_streaming/scripts/live_stream_service.py#L63)
- model cache and warmup:
  [live_stream_service.py](/home/thomas/fencing/portable_fencing_pipeline_low_latency_streaming/scripts/live_stream_service.py#L123)
- live tracking session:
  [live_stream_service.py](/home/thomas/fencing/portable_fencing_pipeline_low_latency_streaming/scripts/live_stream_service.py#L153)
- finalize path:
  [live_stream_service.py](/home/thomas/fencing/portable_fencing_pipeline_low_latency_streaming/scripts/live_stream_service.py#L229)
- persistent service loop:
  [live_stream_service.py](/home/thomas/fencing/portable_fencing_pipeline_low_latency_streaming/scripts/live_stream_service.py#L1050)

## Activation

The local streaming path is enabled by environment variables and by UI mode.

Required environment:

```bash
export REFEREE_USE_LOCAL_STREAMING_ANALYZER=true
export REFEREE_SEND_TO_SERVER=true
export REFEREE_LOCAL_ANALYZER_FISHEYE_BACKEND=none
```

Why `REFEREE_SEND_TO_SERVER=true` is still required:

- In the current UI logic, the local analyzer is attached to the same phrase-review branch that used to mean "remote judge".
- In the UI, this means you must select `Send to server` if you want live streaming analysis.
- `Local only` disables this path.

The relevant condition is in [control_fencing.py](/home/thomas/fencing/jetson_orin_nano_bundle/control_fencing.py#L2519).

## End-to-End Runtime Flow

### 1. App Startup

When the app starts:

1. the Arduino serial port is opened,
2. Arduino time sync is established,
3. the USB camera is opened,
4. the persistent local analyzer is warmed.

Warmup happens in [warm_local_analyzer_service()](/home/thomas/fencing/jetson_orin_nano_bundle/control_fencing.py#L243).

The analyzer warmup:

- starts or reuses one persistent subprocess,
- loads the YOLO/TensorRT model once,
- runs one warmup inference for the active camera resolution,
- keeps the service alive for later phrases.

This avoids paying model-load cost on every phrase.

### 2. Start Bout

When the user clicks `Start Bout`:

1. the app runs `_run_start_sequence()`:
   [control_fencing.py](/home/thomas/fencing/jetson_orin_nano_bundle/control_fencing.py#L2994)
2. `prepare_phrase_artifacts()` creates:
   - `recordings/<timestamp>_phraseNN/`
   - `<base>.txt`
   - a `LocalStreamingSessionManager`
3. the recorder is armed with both:
   - local AVI writing
   - live frame delivery to the analyzer
4. the start command `s` is sent to the Arduino.

If the analyzer session starts successfully, the app logs:

- `Local analyzer session started`
- `Camera: Recording + Local analysis`

### 3. Camera Capture Path

The live camera path now prefers native GStreamer instead of OpenCV V4L2.

Implementation:

- [control_fencing.py](/home/thomas/fencing/jetson_orin_nano_bundle/control_fencing.py#L1596)

Current pipeline shape:

```text
v4l2src device=/dev/videoX do-timestamp=true
  ! image/jpeg,width=1280,height=720,framerate=30/1
  ! jpegdec
  ! videoconvert
  ! video/x-raw,format=BGR
  ! fdsink fd=1 sync=false
```

The recorder reads raw BGR frames from the subprocess stdout.

This path replaced the older OpenCV V4L2 ingest because the OpenCV build on this Jetson does not have GStreamer enabled and was holding live capture below target FPS.

### 4. Recorder Threading Model

The recorder has three separate threads:

- capture thread:
  [control_fencing.py](/home/thomas/fencing/jetson_orin_nano_bundle/control_fencing.py#L2171)
- writer thread:
  [control_fencing.py](/home/thomas/fencing/jetson_orin_nano_bundle/control_fencing.py#L2248)
- analysis thread:
  [control_fencing.py](/home/thomas/fencing/jetson_orin_nano_bundle/control_fencing.py#L2214)

This separation is important.

The capture thread only:

1. reads one camera frame,
2. timestamps it,
3. enqueues it for the AVI writer,
4. enqueues it for live analysis,
5. updates frame counters.

The capture thread does not:

- run inference,
- block on analyzer IPC,
- write AVI frames directly.

That separation is what allowed live capture to recover to about 30 FPS.

### 5. Frame Handoff to Analyzer

During recording, `_record_frame_locked()`:

- appends the frame timestamp to `recorded_frame_timestamps_ns`
- enqueues the frame for local AVI writing
- enqueues the frame for analysis

Relevant code:

- [control_fencing.py](/home/thomas/fencing/jetson_orin_nano_bundle/control_fencing.py#L2309)

The analysis queue is bounded. If it fills up, the recorder logs a warning and drops live-analysis frames to protect recording FPS:

- [control_fencing.py](/home/thomas/fencing/jetson_orin_nano_bundle/control_fencing.py#L2207)

This is a deliberate tradeoff:

- recording quality and timing are protected first,
- live analysis may degrade or fall back if the analyzer cannot keep up.

### 6. Local Session Manager Behavior

`LocalStreamingSessionManager` runs one phrase session on a background thread:

- [local_streaming_manager.py](/home/thomas/fencing/jetson_orin_nano_bundle/local_streaming_manager.py#L515)

It uses a shared persistent client and sends:

1. `session_start`
2. many `frame` messages
3. one `session_end` message with the final TXT bytes

If the queue overflows:

- it marks the session as overflowed,
- the analyzer will later fall back to offline processing for correctness.

### 7. Streaming Protocol

The protocol between the Jetson and the local analyzer is stdin/stdout JSON plus binary frame payloads.

Service control messages:

- `service_ready`
- `warmup`
- `warmup_complete`
- `shutdown`
- `shutdown_complete`

Session messages:

- `session_start`
- `frame`
- `session_end`
- `cancel_session`
- `result`
- `cancelled`
- `error`

Frame payloads are currently sent as raw BGR24, not JPEG, for lower local transport overhead.

Raw frame decoding happens in:

- [live_stream_service.py](/home/thomas/fencing/portable_fencing_pipeline_low_latency_streaming/scripts/live_stream_service.py#L63)

### 8. Analyzer Live Processing

Each session is handled by `LiveTrackingSession`:

- [live_stream_service.py](/home/thomas/fencing/portable_fencing_pipeline_low_latency_streaming/scripts/live_stream_service.py#L153)

Per frame, it does:

1. optional fisheye transform
2. bootstrap buffering for the first N frames
3. YOLO pose inference
4. jump-safe two-fencer tracking
5. accumulation of per-frame tracks

Current default:

- `fisheye_backend = none`

That was chosen because OpenCV fisheye correction was dominating runtime and was not needed for the current bring-up path.

### 9. Phrase End

When the Arduino enters `DISPLAYING_RESULTS`, the Jetson calls `stop_phrase_recording()`:

- [control_fencing.py](/home/thomas/fencing/jetson_orin_nano_bundle/control_fencing.py#L2624)

That function:

1. stops recording,
2. computes measured FPS,
3. recalculates TXT log frame numbers using recorded timestamps,
4. sends `session_end` with the final TXT bytes,
5. keeps the phrase folder and AVI/TXT locally.

### 10. Finalize and Result

When the analyzer receives `session_end`, it:

1. writes the TXT into the phrase directory,
2. finalizes live tracking state,
3. converts tracks to normalized keypoint series,
4. runs limb anomaly detection and repair,
5. runs the final FPS30 referee pass,
6. writes `analysis_result.json`,
7. returns a result payload to the Jetson.

Relevant code:

- finalize:
  [live_stream_service.py](/home/thomas/fencing/portable_fencing_pipeline_low_latency_streaming/scripts/live_stream_service.py#L229)
- session end handling:
  [live_stream_service.py](/home/thomas/fencing/portable_fencing_pipeline_low_latency_streaming/scripts/live_stream_service.py#L967)

The Jetson then saves `<base>_result.json` in the phrase folder and updates the UI.

## Why Streaming Reduces Latency

Without streaming:

1. record AVI
2. stop phrase
3. start pipeline
4. wait for the entire pipeline

With streaming:

1. record AVI
2. run most tracking work while recording
3. stop phrase
4. do only the remaining finalize/judging work

The expensive part of the pipeline is the tracking/inference stage, not the final referee logic.

So the main latency win comes from overlapping frame processing with recording time.

## Fallback Behavior

The system is designed to protect correctness if live analysis is degraded.

Fallback triggers include:

- local analyzer queue overflow
- non-contiguous frame numbering
- live finalize failure

Fallback handling is in:

- [live_stream_service.py](/home/thomas/fencing/portable_fencing_pipeline_low_latency_streaming/scripts/live_stream_service.py#L986)
- [live_stream_service.py](/home/thomas/fencing/portable_fencing_pipeline_low_latency_streaming/scripts/live_stream_service.py#L1019)

If fallback happens:

- the analyzer reruns the offline phrase pipeline on the saved AVI/TXT,
- the client still gets a final result,
- `processing_mode` reflects what happened.

Typical values:

- `live_streaming`
- `offline_fallback`

## Files Written Per Phrase

In the Jetson phrase folder:

- `<base>.avi`
- `<base>.txt`
- `<base>_result.json`

Example:

- [20260411_215639_phrase01](/home/thomas/fencing/jetson_orin_nano_bundle/recordings/20260411_215639_phrase01)

In the streaming bundle output folder:

- staged phrase AVI/TXT
- `analysis_result.json`
- `analysis_result_limb_interp_experimental.json`

Example:

- [20260411_215639_phrase01](/home/thomas/fencing/portable_fencing_pipeline_low_latency_streaming/runtime_outputs/live_streaming_sessions/20260411_215639_phrase01)

## Important Environment Variables

Core streaming variables:

- `REFEREE_USE_LOCAL_STREAMING_ANALYZER`
- `REFEREE_SEND_TO_SERVER`
- `REFEREE_LOCAL_ANALYZER_ROOT`
- `REFEREE_LOCAL_ANALYZER_PYTHON`
- `REFEREE_LOCAL_ANALYZER_MODEL_PATH`
- `REFEREE_LOCAL_ANALYZER_FISHEYE_BACKEND`
- `REFEREE_LOCAL_ANALYZER_BOOTSTRAP_FRAMES`
- `REFEREE_LOCAL_ANALYZER_QUEUE_MAX`
- `REFEREE_LOCAL_ANALYZER_STARTUP_TIMEOUT`
- `REFEREE_LOCAL_ANALYZER_YOLO_CONF`
- `REFEREE_LOCAL_ANALYZER_YOLO_IMGSZ`
- `REFEREE_LOCAL_ANALYZER_YOLO_HALF`
- `REFEREE_LOCAL_ANALYZER_YOLO_VERBOSE`

Useful recorder/debug variables:

- `FENCING_CAMERA_INDEX`
- `FENCING_CAMERA_DEVICE`
- `FENCING_CAMERA_WIDTH`
- `FENCING_CAMERA_HEIGHT`
- `FENCING_CAMERA_FPS`
- `FENCING_CONSOLE_DETAIL_LOGS`
- `FENCING_OVERLAY_FRAME_COUNTER`

## Typical Launch

```bash
cd /home/thomas/fencing/jetson_orin_nano_bundle
export REFEREE_USE_LOCAL_STREAMING_ANALYZER=true
export REFEREE_SEND_TO_SERVER=true
export REFEREE_LOCAL_ANALYZER_FISHEYE_BACKEND=none
./run_control_fencing.sh
```

In the UI:

- select `Send to server`

That selection currently means:

- use the local live analyzer path if `REFEREE_USE_LOCAL_STREAMING_ANALYZER=true`
- not necessarily use a remote server

## Console Logs to Watch

Useful prefixes:

- `[LOCAL_ANALYZER]`
- `[LOCAL_ANALYZER_STREAM]`
- `[CAMERA]`
- `[STREAMING]`
- `[SIGNAL]`
- `[PHRASE_LOG]`
- `[STATE]`

Healthy startup usually includes:

- `Camera initialized ... source /dev/videoX via native GStreamer`
- `Persistent local analyzer is warm`

Healthy phrase flow usually includes:

- `Local analyzer session started`
- `Recording armed -> ... (streaming=yes, ...)`
- `Camera captured N frames @ ... FPS`
- `Streaming session ended (N frames sent)`
- `Local analyzer result saved to ..._result.json`

## Current Non-Blocking Issues

At the time of writing, the main live path is working, but a few cleanup items remain:

- `espeak` may be missing, so the spoken cue can fail
- some logs still use old wording such as `Pi judge`
- some phrase-start log lines may still show `frame 000001` instead of `000000`

These do not block live streaming analysis, but they should be cleaned up for polish and clarity.
