# PisteLink AI 服务对接实现说明

本文档描述当前复制版管线中的 PisteLink 对接实现。原始管线未修改；所有新增代码都在：

`/home/thomas/fencing/pistelink_ai_pipeline/`

## 目录结构

- `jetson_orin_nano_bundle/`
  - `pistelink_ai_service.py`：Unix socket AI 服务入口。
  - `pistelink_protocol.py`：NDJSON 协议封包、解包、心跳和时间戳辅助。
  - `pistelink_camera_recorder.py`：复用现有 Jetson 相机录制器，并补充 Unix epoch-ms 帧时间戳。
  - `pistelink_analysis_adapter.py`：把 PisteLink match session 接入现有 local streaming analyzer。
  - `pistelink_signal_adapter.py`：把 PisteLink 信号转换成当前分析器需要的 Arduino 风格 TXT，同时输出信号到帧的映射 JSON。
  - `run_pistelink_ai_service.sh`：启动脚本。
  - `tools/simulate_pistelink_backend.py`：后端模拟器，用于 dry-run 协议验证。
- `portable_fencing_pipeline_low_latency_streaming/`
  - 当前低延迟视觉分析管线的复制版。

## 对接方式

AI 服务作为 Unix domain socket server 监听：

`/run/pistelink/ai.sock`

可通过环境变量覆盖：

- `PISTELINK_AI_SOCKET`
- `PISTELINK_MATCH_ROOT`
- `PISTELINK_ANALYZER_ROOT`
- `PISTELINK_ANALYZER_PYTHON`
- `PISTELINK_ANALYZER_MODEL_PATH`
- `PISTELINK_ANALYZER_FISHEYE_BACKEND`
- `PISTELINK_ANALYZER_RESULT_TIMEOUT`
- `PISTELINK_DEBUG_ARTIFACTS`：默认 `1`。双灯且非重剑时，AI 已发送 `match_result` 后，在后台生成调试 overlay 和 FPS30 debug log。
- `PISTELINK_DEBUG_ARTIFACT_TIMEOUT_S`：默认 `600`。
- `PISTELINK_DEBUG_ARTIFACT_DELAY_S`：默认 `0`。

启动命令：

```bash
cd /home/thomas/fencing/pistelink_ai_pipeline/jetson_orin_nano_bundle
./run_pistelink_ai_service.sh
```

Dry-run 协议验证：

```bash
cd /home/thomas/fencing/pistelink_ai_pipeline/jetson_orin_nano_bundle
./run_pistelink_ai_service.sh --dry-run --socket-path /tmp/pistelink/ai.sock --match-root /tmp/pistelink/matches
python3 tools/simulate_pistelink_backend.py --socket-path /tmp/pistelink/ai.sock --match-root /tmp/pistelink/matches --winner A
```

## 当前 match 流程

1. 后端发送 `hello`。
2. AI 回复 `hello_ack`，声明支持 `camera_ready`、帧时间戳、legacy TXT bridge 和 local streaming analyzer。
3. 后端发送 `match_pre_start`，包含 `match_id`、`weapon`、`sensor`、`side_map`。
4. AI 创建 match 目录和 `ai/` 子目录；如果 live streaming 可用，会把本地分析器 session 作为 streaming manager 挂到相机录制器上，但该 session 先处于 paused 状态，再启动相机录制。
5. AI 回复 `camera_ready`：
   - `video_path` 指向最终 MP4 路径。
   - `recording_start_ts` 为 Unix epoch ms。
   - 如果已收到首帧，附带 `first_frame_ts` 和 `first_frame_index`。
   - `frame_timestamps_path` 指向逐帧时间戳 sidecar。
6. 后端发送 `match_begin_ack` 和 `voice_end`。AI 在收到 `voice_end` 后打开 active analysis gate；此后进入 analyzer 的帧从 active frame `0` 重新编号，口令播放期间的帧不跑 YOLO。
7. 后端发送非终止 `signal`：
   - `source:"hit"`，`fight:3/8/9`。
   - AI 保存信号，不立即判定。
8. 后端发送终止 `signal`：
   - `source:"light"`，`terminal:true`，`final_lights`。
   - AI 停止录制，写出帧时间戳、legacy TXT、信号帧映射 JSON。
   - 双灯且非重剑时，AI 结束 live local streaming analyzer session 并读取 ROW 结果。
   - 单灯、无灯、重剑双灯时，AI 取消 live analyzer session，不等待视觉结果。
   - AI 把 AVI 转成 MP4。
   - AI 回复 `match_result`。
   - 双灯且非重剑时，AI 在 `match_result` 发出后才启动后台调试产物生成；该步骤不会阻塞返回结果或结果音频触发。

## 时间对齐实现

现有 `CameraRecorder` 记录的是 `time.perf_counter_ns()`。PisteLink 信号时间是 Unix epoch ms。为了把二者对齐，新 wrapper 在创建 recorder 时记录一组锚点：

- `perf_anchor_ns = time.perf_counter_ns()`
- `epoch_anchor_ns = time.time_ns()`

每个录制帧的 epoch-ms 估算为：

`epoch_anchor_ns + (frame_perf_ns - perf_anchor_ns)`

这样 `signal_ts` 可以直接映射到最近的视频帧。映射结果写入：

`<match_dir>/ai/signal_frame_mapping.json`

帧时间戳写入：

`<match_dir>/ai/frame_timestamps.jsonl`

每行包含：

- `frame`
- `ts`：Unix epoch ms
- `mono_ns`：当前 recorder 记录的 perf-counter ns

用于判定的 analyzer TXT 与 archive TXT 分开：

- Archive TXT：`<match_dir>/ai/<match_id>_signals.txt`，保留完整录制时间轴，方便审计。
- Active analyzer TXT：`<match_dir>/ai/active_analysis_phrase/<match_id>_active_signals.txt`，只用于双灯 ROW 判定；`Phrase recording started` 为 `0.000s`。
- Active analyzer timestamps：`<match_dir>/ai/active_analysis_phrase/active_frame_timestamps.jsonl`，帧号从 active frame `0` 重新编号。

Active analyzer TXT 的事件时间不是直接使用 wall-clock 秒差，而是：

`(mapped_full_recording_frame - active_start_frame) / analyzer_fps`

这样即使相机实际采集因为负载降到例如 28.6 fps，而保存视频和 analyzer 仍按 30 fps 时间轴运行，hit/terminal 事件也会落在正确的 active analysis frame 上。

## Legacy TXT 兼容层

当前低延迟分析器仍读取 Arduino 风格 TXT，因此新增的 `pistelink_signal_adapter.py` 会生成：

`<match_dir>/<match_id>_signals.txt`

设计要点：

- TXT 中不写 `frame N` token，因为当前 `debug_referee_fps30.py` 的 hit regex 只稳定匹配 `time | HIT:` 格式。
- `fight:3` 转成 blade-to-blade contact。
- `fight:8` 按 `side_map.A` 转成 A 方命中。
- `fight:9` 按 `side_map.B` 转成 B 方命中。
- `final_lights` 是最终有效灯状态。
- 如果终止灯亮但前面缺少对应 hit 事件，会在终止时间补一条 hit line，避免分析器缺少 hit frame。
- `Scores -> Fencer 1/2` 保持当前管线语义：Fencer 1 = right，Fencer 2 = left。

## 判定策略

`match_result` 的 `winner/result_code` 规则：

- 只有 A 灯亮：`winner:"A"`，`result_code:8`。
- 只有 B 灯亮：`winner:"B"`，`result_code:9`。
- A/B 都亮：使用本地视觉分析器输出的 `left/right` winner，并通过 `side_map` 转回 `A/B`。
- 双灯但视觉分析器没有给出有效 winner：`winner:"tie"`，`result_code:10`。
- 无灯：`winner:"tie"`，`result_code:0`。

这符合当前管线的核心需求：单灯由电信号最终状态决定，双灯时由视觉 right-of-way 管线决定。

## 双灯调试产物

双灯且非重剑时，默认生成以下调试产物：

- `<match_dir>/ai/debug_artifacts/active_phrase/segment_<match_id>_active.mp4`：裁掉口令播放时间后的原始视频副本，第一帧约等于 `voice_end`。
- `<match_dir>/ai/debug_artifacts/active_phrase/<match_id>_active_signals.txt`：与 active video 对齐的 TXT，`Phrase recording started` 被重置为 `0.000s`。
- `<match_dir>/ai/debug_artifacts/keypoints_overlay_active.mp4`：最终 repaired keypoints overlay 的稳定链接。
- `<match_dir>/ai/debug_artifacts/debug.txt`：`debug_referee_fps30.py` 生成的详细 referee debug log 的稳定链接。
- `<match_dir>/ai/debug_artifacts/debug_artifacts.json`：本次调试任务状态、路径和裁剪 offset。

实现上，AI 先发送 `match_result`，然后在后台调用复制版 analyzer 的
`scripts/reprocess_phrase_limb_interp_jumpsafe_experimental.py --write-repaired-overlay --write-fps30-debug-log`。
因此这些调试文件用于人工复盘，不参与当场判定延迟。

## 当前保留假设

- 相机仍使用现有 `control_fencing.py` 的相机选择和 GStreamer 配置。
- AI 服务继续拥有相机，后端不直接控制相机。
- 后端提供的 `signal_ts` 已经完成静态 offset 修正。
- `side_map` 默认 `A:left`、`B:right`。
- 当前实现接受 `match_dir`；如果后端不传，则使用 `PISTELINK_MATCH_ROOT/<match_id>`。
- 最终协议要求 MP4，因此服务用 ffmpeg 把当前 MJPG/AVI 转成 MP4。

## 仍需现场验证的点

- 实机启动时 `/run/pistelink` 的 owner/mode 是否符合双方部署约定。
- ffmpeg 是否存在；如果不存在，无法稳定产出协议要求的 MP4。
- 复制版 Jetson bundle 已包含原 bundle 的小型 `.venv`；启动脚本会优先使用本目录 `.venv/bin/python`，否则回退到系统 `python3`。
- Analyzer 目录原本的 `.venv` 是指向共享 1.9 GB 环境的 symlink，本次未复制大环境；实际部署建议通过 `PISTELINK_ANALYZER_PYTHON` 指向已验证的 analyzer Python，或单独给复制版创建 analyzer venv。
- 本地分析器模型路径默认优先使用复制版 analyzer 目录中的 engine；如果不存在，会回退到 `yolo26s-pose.pt` 或环境变量。
- 后端默认结果超时建议改为 30 秒。当前合作方后端在超时后会把 `json.txt` 回填为 `result_code=0` 并回到 idle，late `match_result` 会被忽略；双灯 ROW 实测若超过 30 秒，需要继续优化 AI 或进一步调大后端超时。
- Jetson 冷启动加载 TensorRT engine 可能超过 45 秒；AI service 的 `PISTELINK_ANALYZER_STARTUP_TIMEOUT` 默认改为 120 秒。这个预算只覆盖 `camera_ready` 前的 analyzer warmup / session start，不改变触灯后的 30 秒结果等待。
- PisteLink AI service 默认启用 `PISTELINK_ANALYZER_PREWARM=1` 和 `PISTELINK_LIVE_STREAMING=1`。服务启动后会在后台预热 persistent analyzer；match start 时若 analyzer session 可创建，会先挂载 paused streaming manager，等 `voice_end` 后才把 active frames 送入 live analyzer。`camera_ready.payload.visual_streaming.enabled=true` 和相机日志 `streaming=yes` 是现场确认 streaming manager 已挂载的两个信号。
- 新版 PisteLink live analyzer 只从 `voice_end` 后开始接收 active frames；日志中 `Active analysis window opened at camera frame N` 表示 pre-audio frames 已停止进入 YOLO。
- PisteLink live streaming 默认使用 `PISTELINK_ANALYZER_FRAME_ENCODING=jpeg`、`PISTELINK_ANALYZER_JPEG_QUALITY=80` 和 `PISTELINK_ANALYZER_QUEUE_MAX=720`。这样 1280x720/30fps 下较长 phrase 不会因为 raw frame 队列太小而触发 `queue full`，但如果 analyzer 速度低于 30fps，双灯结果仍会等待 live 队列追上。

## 本轮验证记录

- 新增 Python 模块已通过 `python3 -m py_compile`。
- 启动脚本已通过 `bash -n`。
- 已用 `./run_pistelink_ai_service.sh --dry-run` 验证复制版 bundle 自带启动脚本。
- Dry-run socket 验证已通过：
  - A 单灯：返回 `winner:"A"`、`result_code:8`。
  - B 单灯：返回 `winner:"B"`、`result_code:9`。
  - 双灯：无视觉分析时返回 `winner:"tie"`、`result_code:10`。
  - 无灯：返回 `winner:"tie"`、`result_code:0`。
- Synthetic timestamp mapping 验证已通过：`signal_ts` 会映射到最近帧，并输出 `delta_ms`。
- 真实相机已验证：
  - 设备：FYRGB，`/dev/video0`，MJPEG 1280x720/30 fps 可用。
  - GStreamer still capture 成功。
  - `PisteLinkCameraRecorder` 录制成功：1280x720、约 30 fps、逐帧 timestamp sidecar 正常写出。
  - 当前系统没有 `ffmpeg`，已加入 GStreamer MP4 转码 fallback；输出为 H.264/MP4。
- 非 dry-run service-level 验证已通过：
  - `camera_ready` 返回 `width:1280`、`height:720`、`first_frame_ts`、`first_frame_index:0`。
  - 真实录制输出 `segment_<match_id>.avi` 和协议要求的 `segment_<match_id>.mp4`。
  - 示例 `/tmp/pistelink/matches/sim_58260f7a` 中，MP4 为 1280x720、30/1 fps、H.264、QuickTime/MP4 container。
  - `frame_timestamps.jsonl` 写出 14 帧。
  - `signal_frame_mapping.json` 中 hit 信号映射到 frame 9，delta 9 ms；terminal light 映射到 frame 13，delta 27 ms。
  - A 单灯返回 `winner:"A"`、`result_code:8`、`decision_source:"final_lights_single_touch"`，无 analyzer 误报。

## 本轮相机相关修正

- 修复 `PisteLinkCameraRecorder.start()`：现有 `CameraRecorder.start()` 成功时返回 `None`，wrapper 不能用 `bool(None)` 判断失败。
- 修复 wrapper 的 `width/height/fps` 属性读取，改为读取当前 recorder 实际属性。
- 修复 camera release：match 结束、取消、启动失败时释放 recorder，避免后续 match 占用设备。
- 调整 service 启动顺序：先用配置分辨率 warm up local analyzer，再打开并启动相机，避免 TensorRT warmup 期间 native GStreamer capture 因长时间抢占/阻塞而丢失。
- 增加 GStreamer 转 MP4 fallback：无 `ffmpeg` 时仍能生成协议要求的 MP4。
- 单灯/无灯 phrase 不再调用视觉 analyzer；双灯时才调用 right-of-way 分析。

## v1.1 协议一致性修正

- 信封改为 v1.1 规范字段：`v`、`ts_mono_ns`。
- 入站解析兼容旧字段：`protocol_v`、`mono_ns`，便于本地旧脚本和过渡测试。
- 每次新连接后 AI 侧 outbound `id` 从 0 重新开始。
- `hello.payload.protocol_v` 会校验为 1；不匹配时关闭当前连接。
- `hello_ack` payload 改为 `role/app/version/protocol_v`，并保留 `capabilities` 作为额外调试信息。
- `pong` payload 改为 `ref_id`。
- `camera_error` payload 改为 `code/reason`，保留 `stage` 作为额外诊断字段。
- `match_result` 改用 `signal_frame_mapping_path`。
- `match_cancel` 不再发送协议未定义的 ack；AI 停止录制并删除本场 AI/video 产物。
- `shutdown` 不再发送协议未定义的 ack。
- 未识别 type 改为记录并忽略，不再发送协议未定义的 `error` type。
- oversized NDJSON frame 会被丢弃并记录，不会卡住后续读取。
- `match_pre_start` 初始化改为后台线程执行，socket 主循环继续发送 heartbeat；实测 TensorRT warmup 期间每 2 秒发送 `ping`，不会因 6 秒无报文被后端断开。
- TXT 兼容文件移至 `matches/<match_id>/ai/`；复制版 analyzer 的 offline fallback 已允许从 `ai/*.txt` 查找信号文件。
- MP4 转码成功后删除临时 AVI，避免污染 match root。
- 单灯和无灯直接采用最终灯态；双灯才触发视觉 ROW。重剑（`weapon=2`）双灯直接返回 `tie/result_code=10`。

## v1.1 修正后验证记录

- v1.1 风格入站帧 `{"v":1,...}` 已通过 parser 验证。
- outbound `hello_ack` 已验证从 `id:0` 开始。
- Dry-run A 单灯通过，输出字段为 `v/ts_mono_ns/signal_frame_mapping_path`。
- Dry-run 双灯通过，返回 `winner:"tie"`、`result_code:10`。
- Dry-run cancel 通过，`match_cancel` 后删除本场 `ai/` 和 root 视频产物。
- 非 dry-run 单灯相机链路通过：
  - TensorRT warmup 期间服务仍发送 heartbeat。
  - `camera_ready` 包含 `first_frame_ts`。
  - 录制真实帧并生成 MP4。
  - TXT、frame timestamps、signal-frame mapping 均位于 `ai/`。
