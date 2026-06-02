# 本地流式裁判分析说明

本文档说明 `jetson_orin_nano_bundle` 和 `portable_fencing_pipeline_low_latency_streaming` 中当前正在使用的本地流式分析实现。

这里描述的是现在实际运行的实现，不是早期的远程树莓派裁判方案。

## 目标

本地流式分析器的目标是：在比赛片段还在录制时，就开始做视觉分析。

也就是说，不再是“先录完 AVI 和 TXT，再启动整条离线流水线”，而是：

1. Jetson 本地录制比赛片段
2. 每抓到一帧就实时送给本地分析器
3. 比赛结束后再发送最终 TXT 信号日志
4. 分析器快速完成收尾并返回判决结果

系统仍然把本地保存的 AVI 和 TXT 作为最终事实来源。

## 主要组件

### Jetson 端 UI 与录像器

主入口是 [control_fencing.py](/home/thomas/fencing/jetson_orin_nano_bundle/control_fencing.py)。

关键位置：

- 流式分析环境变量配置：
  [control_fencing.py](/home/thomas/fencing/jetson_orin_nano_bundle/control_fencing.py#L121)
- 本地分析器预热：
  [control_fencing.py](/home/thomas/fencing/jetson_orin_nano_bundle/control_fencing.py#L243)
- 原生 GStreamer 相机采集：
  [control_fencing.py](/home/thomas/fencing/jetson_orin_nano_bundle/control_fencing.py#L1581)
- 录像线程和队列：
  [control_fencing.py](/home/thomas/fencing/jetson_orin_nano_bundle/control_fencing.py#L1754)
- 片段准备：
  [control_fencing.py](/home/thomas/fencing/jetson_orin_nano_bundle/control_fencing.py#L2476)
- 片段停止与收尾：
  [control_fencing.py](/home/thomas/fencing/jetson_orin_nano_bundle/control_fencing.py#L2624)
- UI 开始比赛流程：
  [control_fencing.py](/home/thomas/fencing/jetson_orin_nano_bundle/control_fencing.py#L2898)

### 本地会话管理器

Jetson 端连接本地分析服务的桥接层是 [local_streaming_manager.py](/home/thomas/fencing/jetson_orin_nano_bundle/local_streaming_manager.py#L478)。

它负责：

- 连接共享的持久化分析器子进程
- 为一条比赛片段启动会话
- 把视频帧送入该会话
- 在结束时发送最终 TXT
- 等待最终返回结果

### 流式分析服务

分析服务本体在 [live_stream_service.py](/home/thomas/fencing/portable_fencing_pipeline_low_latency_streaming/scripts/live_stream_service.py#L1)。

它是一个本地 stdin/stdout 子进程，内部复用了复制出来的低延迟分析流水线。

关键位置：

- 原始帧解码：
  [live_stream_service.py](/home/thomas/fencing/portable_fencing_pipeline_low_latency_streaming/scripts/live_stream_service.py#L63)
- 模型缓存与预热：
  [live_stream_service.py](/home/thomas/fencing/portable_fencing_pipeline_low_latency_streaming/scripts/live_stream_service.py#L123)
- 实时跟踪会话：
  [live_stream_service.py](/home/thomas/fencing/portable_fencing_pipeline_low_latency_streaming/scripts/live_stream_service.py#L153)
- 收尾路径：
  [live_stream_service.py](/home/thomas/fencing/portable_fencing_pipeline_low_latency_streaming/scripts/live_stream_service.py#L229)
- 持久化服务循环：
  [live_stream_service.py](/home/thomas/fencing/portable_fencing_pipeline_low_latency_streaming/scripts/live_stream_service.py#L1050)

## 启用方式

本地流式分析由环境变量和 UI 模式共同决定。

必须设置：

```bash
export REFEREE_USE_LOCAL_STREAMING_ANALYZER=true
export REFEREE_SEND_TO_SERVER=true
export REFEREE_LOCAL_ANALYZER_FISHEYE_BACKEND=none
```

为什么还需要 `REFEREE_SEND_TO_SERVER=true`：

- 当前 UI 逻辑里，本地分析器挂在过去“远程裁判”那条分支上。
- 所以如果你想启用流式分析，UI 中必须选择 `Send to server`。
- 如果选 `Local only`，这条流式分析路径会被关闭。

对应逻辑见：

- [control_fencing.py](/home/thomas/fencing/jetson_orin_nano_bundle/control_fencing.py#L2519)

## 端到端运行流程

### 1. 程序启动

程序启动时会依次执行：

1. 打开 Arduino 串口
2. 建立 Arduino 时间同步
3. 打开 USB 相机
4. 预热持久化本地分析器

预热逻辑在 [warm_local_analyzer_service()](/home/thomas/fencing/jetson_orin_nano_bundle/control_fencing.py#L243)。

预热的作用：

- 启动或复用一个长期存活的分析器子进程
- 只加载一次 YOLO/TensorRT 模型
- 按当前相机分辨率做一次 warmup 推理
- 后续每条比赛片段复用这个服务

这样就不用每次片段开始时重新加载模型。

### 2. 开始比赛片段

当用户点击 `Start Bout` 时：

1. UI 执行 `_run_start_sequence()`：
   [control_fencing.py](/home/thomas/fencing/jetson_orin_nano_bundle/control_fencing.py#L2994)
2. `prepare_phrase_artifacts()` 创建：
   - `recordings/<timestamp>_phraseNN/`
   - `<base>.txt`
   - 一个 `LocalStreamingSessionManager`
3. 录像器被置为工作状态，同时开启：
   - 本地 AVI 写入
   - 实时视频帧推送给分析器
4. 向 Arduino 发送开始命令 `s`

如果分析器会话成功启动，日志中会看到：

- `Local analyzer session started`
- `Camera: Recording + Local analysis`

### 3. 相机采集路径

当前 live 相机采集优先使用原生 GStreamer，而不是 OpenCV V4L2。

实现位置：

- [control_fencing.py](/home/thomas/fencing/jetson_orin_nano_bundle/control_fencing.py#L1596)

当前采集管线：

```text
v4l2src device=/dev/videoX do-timestamp=true
  ! image/jpeg,width=1280,height=720,framerate=30/1
  ! jpegdec
  ! videoconvert
  ! video/x-raw,format=BGR
  ! fdsink fd=1 sync=false
```

录像器从这个子进程的 stdout 读取原始 BGR 帧。

之所以改成这条路径，是因为当前 Jetson 上的 OpenCV 没有启用 GStreamer，而旧的 OpenCV V4L2 采集路径在实战中会把 FPS 压低。

### 4. 录像器线程模型

录像器有三个线程：

- 抓帧线程：
  [control_fencing.py](/home/thomas/fencing/jetson_orin_nano_bundle/control_fencing.py#L2171)
- AVI 写入线程：
  [control_fencing.py](/home/thomas/fencing/jetson_orin_nano_bundle/control_fencing.py#L2248)
- 分析发送线程：
  [control_fencing.py](/home/thomas/fencing/jetson_orin_nano_bundle/control_fencing.py#L2214)

这点很关键。

抓帧线程只做：

1. 读一帧相机图像
2. 记录时间戳
3. 把帧送到 AVI 写入队列
4. 把帧送到分析队列
5. 更新帧计数

抓帧线程不会：

- 直接做推理
- 直接阻塞等待分析器 IPC
- 直接写 AVI

正是这层解耦，才使得 live 录制恢复到接近 30 FPS。

### 5. 帧发送到分析器

录制过程中，`_record_frame_locked()` 会：

- 把时间戳追加到 `recorded_frame_timestamps_ns`
- 把帧送到本地 AVI 写入队列
- 把帧送到分析队列

对应代码：

- [control_fencing.py](/home/thomas/fencing/jetson_orin_nano_bundle/control_fencing.py#L2309)

分析队列是有上限的。如果满了，录像器会打印警告并丢弃 live 分析帧，以优先保护录制 FPS：

- [control_fencing.py](/home/thomas/fencing/jetson_orin_nano_bundle/control_fencing.py#L2207)

这是一个有意设计的取舍：

- 优先保证录像质量和时序
- 如果分析器跟不上，live 分析允许退化或回退

### 6. 本地会话管理器行为

`LocalStreamingSessionManager` 在后台线程里管理一条比赛片段会话：

- [local_streaming_manager.py](/home/thomas/fencing/jetson_orin_nano_bundle/local_streaming_manager.py#L515)

它会向分析器发送：

1. `session_start`
2. 多个 `frame`
3. 一个附带最终 TXT 内容的 `session_end`

如果内部队列溢出：

- 会把会话标记为 overflowed
- 分析器之后会为了正确性回退到离线处理

### 7. 流式协议

Jetson 和本地分析器之间使用 stdin/stdout JSON 加二进制帧负载的协议。

服务级消息：

- `service_ready`
- `warmup`
- `warmup_complete`
- `shutdown`
- `shutdown_complete`

会话级消息：

- `session_start`
- `frame`
- `session_end`
- `cancel_session`
- `result`
- `cancelled`
- `error`

当前帧传输使用的是原始 `raw_bgr24`，而不是 JPEG。这样做是为了减少本地传输开销。

原始帧解码见：

- [live_stream_service.py](/home/thomas/fencing/portable_fencing_pipeline_low_latency_streaming/scripts/live_stream_service.py#L63)

### 8. 分析器实时处理

每条会话由 `LiveTrackingSession` 处理：

- [live_stream_service.py](/home/thomas/fencing/portable_fencing_pipeline_low_latency_streaming/scripts/live_stream_service.py#L153)

每收到一帧，它会执行：

1. 可选的鱼眼矫正
2. 前 N 帧 bootstrap 缓冲
3. YOLO 姿态推理
4. jump-safe 双人跟踪
5. 保存每帧的跟踪结果

当前默认设置：

- `fisheye_backend = none`

原因是此前 OpenCV 鱼眼矫正占用了大量运行时间，而当前验证阶段并不需要它。

### 9. 比赛结束

当 Arduino 进入 `DISPLAYING_RESULTS`，Jetson 会调用 `stop_phrase_recording()`：

- [control_fencing.py](/home/thomas/fencing/jetson_orin_nano_bundle/control_fencing.py#L2624)

该函数会：

1. 停止录制
2. 计算实际 FPS
3. 根据录制时保存的时间戳回填 TXT 中的帧号
4. 把最终 TXT 内容通过 `session_end` 发给分析器
5. 保留本地的片段目录、AVI、TXT

### 10. 收尾和最终结果

分析器收到 `session_end` 后会：

1. 把 TXT 写回到片段目录
2. 完成 live 跟踪状态收尾
3. 将轨迹转成归一化关键点序列
4. 执行肢体异常检测和修复
5. 执行最终 FPS30 裁判逻辑
6. 写出 `analysis_result.json`
7. 把结果返回给 Jetson

关键代码：

- 收尾：
  [live_stream_service.py](/home/thomas/fencing/portable_fencing_pipeline_low_latency_streaming/scripts/live_stream_service.py#L229)
- `session_end` 处理：
  [live_stream_service.py](/home/thomas/fencing/portable_fencing_pipeline_low_latency_streaming/scripts/live_stream_service.py#L967)

Jetson 随后会在比赛片段目录下写出 `<base>_result.json`，并更新 UI。

## 为什么流式分析能降低延迟

如果不用流式：

1. 先录制 AVI
2. 比赛停止
3. 再启动整条分析流水线
4. 等待全部处理结束

如果用流式：

1. 一边录制 AVI
2. 一边提前完成大部分跟踪工作
3. 比赛停止后
4. 只做剩余的收尾和裁判逻辑

因为整条流水线里最重的是跟踪和推理，不是最终裁判，所以最大的收益来自“录制和分析重叠执行”。

## 回退机制

系统设计上优先保证正确性。如果 live 分析退化，会自动回退。

触发条件包括：

- 本地分析队列溢出
- 帧号不连续
- live 收尾失败

对应处理逻辑：

- [live_stream_service.py](/home/thomas/fencing/portable_fencing_pipeline_low_latency_streaming/scripts/live_stream_service.py#L986)
- [live_stream_service.py](/home/thomas/fencing/portable_fencing_pipeline_low_latency_streaming/scripts/live_stream_service.py#L1019)

如果发生回退：

- 分析器会对保存下来的 AVI/TXT 重新跑离线流程
- 客户端仍然能得到最终结果
- `processing_mode` 会标明实际走的是哪条路径

典型值：

- `live_streaming`
- `offline_fallback`

## 每条比赛片段会写出哪些文件

Jetson 比赛目录中：

- `<base>.avi`
- `<base>.txt`
- `<base>_result.json`

示例：

- [20260411_215639_phrase01](/home/thomas/fencing/jetson_orin_nano_bundle/recordings/20260411_215639_phrase01)

流式分析 bundle 的输出目录中：

- 分析阶段使用的 AVI/TXT
- `analysis_result.json`
- `analysis_result_limb_interp_experimental.json`

示例：

- [20260411_215639_phrase01](/home/thomas/fencing/portable_fencing_pipeline_low_latency_streaming/runtime_outputs/live_streaming_sessions/20260411_215639_phrase01)

## 重要环境变量

核心流式分析变量：

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

常用录像/调试变量：

- `FENCING_CAMERA_INDEX`
- `FENCING_CAMERA_DEVICE`
- `FENCING_CAMERA_WIDTH`
- `FENCING_CAMERA_HEIGHT`
- `FENCING_CAMERA_FPS`
- `FENCING_CONSOLE_DETAIL_LOGS`
- `FENCING_OVERLAY_FRAME_COUNTER`

## 典型启动方式

```bash
cd /home/thomas/fencing/jetson_orin_nano_bundle
export REFEREE_USE_LOCAL_STREAMING_ANALYZER=true
export REFEREE_SEND_TO_SERVER=true
export REFEREE_LOCAL_ANALYZER_FISHEYE_BACKEND=none
./run_control_fencing.sh
```

然后在 UI 中：

- 选择 `Send to server`

在当前实现里，这个选择的含义是：

- 如果 `REFEREE_USE_LOCAL_STREAMING_ANALYZER=true`，就走本地 live 分析器
- 不一定真的走远程服务器

## 建议关注的控制台日志

常用前缀：

- `[LOCAL_ANALYZER]`
- `[LOCAL_ANALYZER_STREAM]`
- `[CAMERA]`
- `[STREAMING]`
- `[SIGNAL]`
- `[PHRASE_LOG]`
- `[STATE]`

健康启动通常会看到：

- `Camera initialized ... source /dev/videoX via native GStreamer`
- `Persistent local analyzer is warm`

健康的比赛流程通常会看到：

- `Local analyzer session started`
- `Recording armed -> ... (streaming=yes, ...)`
- `Camera captured N frames @ ... FPS`
- `Streaming session ended (N frames sent)`
- `Local analyzer result saved to ..._result.json`

## 当前仍然存在但不阻塞的事项

截至当前版本，主流程已经可以正常工作，但还有一些收尾问题：

- 如果系统未安装 `espeak`，语音提示会失败
- 某些日志仍然沿用了旧名称，比如 `Pi judge`
- 某些片段开始日志可能仍显示 `frame 000001` 而不是 `000000`

这些问题不会阻塞 live 流式分析，但为了清晰和可维护性，后续仍建议清理。
