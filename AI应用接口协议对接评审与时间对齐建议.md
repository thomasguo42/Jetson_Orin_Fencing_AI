# 对《PisteLink ↔ AI 应用接口协议》的技术回复与对接建议

- 回复对象：贵方提供的 `AI应用接口协议与协作说明.md` v1.0
- 回复目的：说明我方当前低延迟 AI 裁判管线的工作方式、贵方协议与我方系统的适配点、以及为了高精度时间映射需要贵方调整或确认的协议细节
- 我方当前 AI 管线：Jetson 本地低延迟流式分析管线
- 文档性质：这是对贵方协议的技术反馈和对接建议，不替代贵方原协议；后续可作为双方讨论 v1.1 协议和联调计划的依据

---

## 1. 回复摘要

贵方 `AI应用接口协议与协作说明.md` 的整体方向是正确的：它把 MCU 串口、电信号文件、音频播放和 FTP 上传交给 PisteLink 后端，把摄像头采集、视频写入、视觉分析和仲裁交给 AI 应用。这种职责划分符合我方当前系统向“AI 作为独立本地服务”演进的方向。

不过，从我方当前低延迟击剑分析管线的实际工作方式看，贵方 v1.0 协议还不能直接保证“高精度时间对齐”。协议里已有 `signal_ts = recv_ts + video_sync_offset_ms` 的设计，这对基础对齐有帮助，但还不够强。当前 AI 裁判真正需要的是：

```text
电信号事件时间 -> 实际视频帧编号
```

而不是只有：

```text
电信号事件时间 -> 某个墙钟毫秒时间
```

如果实现时只用 `begin_ts`、名义 30 FPS 和 `signal_ts` 来估算帧号，会比我方当前 Arduino 管线的帧时间戳映射更弱。为了达到当前系统需要的精度，建议贵方在协议中明确支持和传递足够的时间元数据，并允许 AI 保存每帧真实采集时间戳，用这些时间戳把 `signal_ts` 映射到帧编号。

### 1.1 我方对贵方协议的总体判断

可以对接，但建议不要按 v1.0 原样冻结。v1.0 已经把进程边界和消息方向设计清楚了，这部分适配良好；主要需要补强的是时间语义、左右映射、终止事件、lockout 语义和结果目录约束。只要这些点在协议层明确，我方现有低延迟管线可以比较顺利地包装成 PisteLink AI 服务。

### 1.2 希望贵方优先调整或确认的内容

以下项目建议作为双方对接前的优先事项：

1. 明确 `signal_ts` 的来源：它是后端收到 MCU 帧的时间，还是 MCU 实际检测事件的时间换算到 Jetson 后的时间。
2. 增加或预留 monotonic 时间字段，例如 `signal_mono_ns`，用于避免墙钟调整影响高精度映射。
3. 明确 A/B 与视频 `left/right` 的对应关系，并说明摄像头方向改变时如何配置。
4. 明确 `source:"light"` 是否代表 phrase 结束，以及 `fight=8/9/10` 与 `source:"light"` 冲突时以哪个为准。
5. 明确 lockout 开始时间和 lockout 时长，或者在首个有效 hit/light 事件中标记 `starts_lockout` / `lockout_ms`。
6. 在 `camera_ready` 中允许 AI 返回实际录像开始时间、第一帧时间、名义 FPS、视频路径和 frame timestamp sidecar 路径。
7. 允许 AI 在比赛目录中写入分析中间产物，例如 `analysis_result.json`、`signal_frame_mapping.json`、`frame_timestamps.jsonl`。
8. 明确不同剑种的裁判策略边界，尤其是重剑是否直接采用电信号结果，不进入 right-of-way 分析。

### 1.3 建议的协议调整优先级

| 优先级 | 希望贵方调整或确认的内容 | 原因 | 对我方接入的影响 |
|---|---|---|---|
| 必须 | 明确 A/B 与视频 `left/right` 的映射 | 我方裁判内部使用 left/right，贵方结果使用 A/B | 不明确会导致 winner 方向可能反 |
| 必须 | 明确 `signal_ts` 的来源和校正方式 | 电信号必须能映射到视频帧 | 不明确会降低 blade contact / hit 判断可信度 |
| 必须 | 明确 phrase 结束事件 | AI 需要知道何时停止录像并触发最终裁判 | 不明确会导致提前或延迟停止录像 |
| 必须 | 明确最终灯态和 `fight=8/9/10` 语义 | single hit、double hit、tie 的裁判路径不同 | 不明确会导致错误进入 ROW 分析或错误返回结果 |
| 必须 | 明确 lockout 开始时间和时长 | 当前 FPS30 裁判会使用 lockout 时间 | 不明确会影响 hit window 和 blade contact 筛选 |
| 强烈建议 | 增加 monotonic timestamp | 避免墙钟调整，便于高精度对齐 | 可以显著提高时间审计能力 |
| 强烈建议 | 允许 `camera_ready` 返回第一帧时间和视频路径 | `begin_ts` 不等于视频第 0 帧时间 | 可以避免用名义 FPS 估算帧号 |
| 强烈建议 | 允许 AI 写 `frame_timestamps.jsonl` 和 `signal_frame_mapping.json` | 方便复盘每个 signal 映射到了哪一帧 | 可以快速定位时间偏差问题 |
| 建议 | 在 `match_result` 允许可选 debug metadata | 不影响后端主流程，但方便联调 | 有助于减少双方排障成本 |
| 建议 | 明确不同 weapon 的判定边界 | 当前 AI 逻辑主要适合 right-of-way 场景 | 避免把佩剑逻辑错误应用到重剑 |

---

## 2. 当前低延迟管线如何工作

### 2.1 当前系统职责

当前主程序是：

```text
jetson_orin_nano_bundle/control_fencing.py
```

它目前同时承担以下职责：

- 连接 Arduino 串口
- 读取 Arduino 输出的 `STATE` / `SCORE` / `LOG` / `CONTACT`
- 创建每个 phrase 的本地目录
- 写入传统 `.txt` 电信号日志
- 控制摄像头采集和本地录像
- 把实时帧送入本地低延迟分析器
- 在 phrase 结束后把视频和 TXT 交给裁判分析
- 显示 UI、播放提示音、处理手动确认等

贵方 PisteLink 协议会把其中一部分职责移出 AI 应用。也就是说，对接贵方系统不是简单替换 Arduino 串口解析函数，而是改变系统边界：我方需要把现有视觉与裁判能力封装成一个独立 AI 服务，贵方后端负责 MCU 与比赛生命周期驱动。

### 2.2 当前 Arduino 信号输入格式

当前 Arduino 固件输出的关键信息包括：

```text
PHRASE_START_MS:<arduino_millis>
STATE:RECORDING
STATE:LOCKOUT_PERIOD
STATE:DISPLAYING_RESULTS
SCORE:F1_ON
SCORE:F2_ON
SCORE:F1_OFF
SCORE:F2_OFF
LOG:<timestamp>|Phrase recording started
LOG:<timestamp>|Lockout period started (0.200s window)
LOG:<timestamp>|HIT: Left scores on Right!
LOG:<timestamp>|HIT: Right scores on Left!
LOG:<timestamp>|HIT: Simultaneous valid hits!
LOG:<timestamp>|Off-Target: Blade-to-blade contact.
LOG:<timestamp>|Phrase recording ended
```

`control_fencing.py` 会把这些串口事件整理成每个 phrase 的 `.txt` 文件。后续裁判分析大量依赖这个 TXT 文件中的文本模式。

### 2.3 当前 phrase 目录和文件

当前系统为每个 phrase 创建类似这样的目录：

```text
jetson_orin_nano_bundle/recordings/<timestamp>_phraseNN/
  <timestamp>_phraseNN.avi
  <timestamp>_phraseNN.txt
  <timestamp>_phraseNN_result.json
```

其中：

- `.avi` 是 AI 录制的视频
- `.txt` 是 Arduino 信号日志
- `_result.json` 是最终裁判结果或本地分析结果

贵方协议当前建议目录为：

```text
/var/lib/pistelink/matches/<match_id>/
  segment_*.mp4
  json.txt
```

这两个目录结构并不冲突，但需要明确新 AI 服务最终应该把视频写到 PisteLink 指定目录，并把分析中间产物放在哪里。

### 2.4 当前本地低延迟流式分析器

当前最新的低延迟管线是：

```text
jetson_orin_nano_bundle/local_streaming_manager.py
portable_fencing_pipeline_low_latency_streaming/scripts/live_stream_service.py
portable_fencing_pipeline_low_latency_streaming/scripts/reprocess_phrase_limb_interp_jumpsafe_experimental.py
portable_fencing_pipeline_low_latency_streaming/scripts/debug_referee_fps30.py
```

运行方式大致是：

1. `control_fencing.py` 开始 phrase，创建目录、TXT 和视频文件。
2. `LocalStreamingSessionManager` 启动或复用一个长期运行的本地分析子进程。
3. 摄像头录制线程每采集一帧，就把原始 BGR 帧送给本地分析器。
4. `live_stream_service.py` 在 phrase 还没结束时就做 YOLO pose 和双人 tracking。
5. phrase 结束后，主程序把最终 TXT 信号日志发给分析器。
6. 分析器用已经算好的 tracks 加上 TXT 信号，运行 FPS30 裁判逻辑。
7. 如果实时帧丢失、数量不一致、非连续，分析器退回离线完整处理，保证结果正确性。

这套设计的核心优势是：视觉分析尽量提前做，phrase 停止后只需要做信号整合、修复和裁判，从而降低等待时间。

### 2.5 当前裁判逻辑依赖什么信号

最终裁判主要在 `debug_referee_fps30.py` 中完成。它依赖以下信息：

- phrase 开始时间
- blade-to-blade contact 时间
- hit 时间
- simultaneous hit 时间
- lockout 开始时间
- score summary，也就是双方灯是否亮
- 每个 hit 属于左方还是右方
- 视频真实 FPS 和帧时间
- 左右选手的关键点轨迹

当前裁判输入不是“纯 JSON 电信号”，而是传统 TXT 文本。例如：

```text
 0.733s | frame 000022 | Off-Target: Blade-to-blade contact.
 1.221s | frame 000037 | HIT: Left scores on Right!
 1.234s | frame 000038 | HIT: Right scores on Left!
Scores -> Fencer 1: HIT, Fencer 2: HIT
```

因此，接入贵方协议时我方有两条路线：

1. 短期路线：由我方把 PisteLink 的结构化 `signal` 事件转换成当前 TXT 格式。
2. 长期路线：改造 FPS30 裁判，让它直接接收结构化信号对象。

短期建议走第一条路线，因为风险小、便于和现有结果对比。

### 2.6 对贵方协议的直接含义

贵方不需要兼容我方旧 Arduino 串口文本格式，也不需要直接生成我方当前的 `.txt` 文件；这部分可以由我方 AI 服务内部 adapter 完成。贵方需要保证的是：结构化 `signal` 事件中包含足够稳定、明确、可审计的语义，让我方可以无歧义地生成当前裁判所需的等价信息。

对我方当前管线来说，最关键的不是消息载体是 TXT 还是 JSON，而是以下信息必须完整：

- 哪一方得分或哪一方亮灯
- blade-to-blade contact 的时间
- hit 的时间和所属一方
- double hit / simultaneous hit 的最终语义
- lockout 开始时间和时长
- phrase 结束时机
- A/B 与视频 left/right 的映射
- 电信号时间如何精确映射到视频帧

---

## 3. 贵方协议中适配良好的部分

### 3.1 职责划分清晰

贵方协议明确：

- PisteLink 后端负责 MCU 串口通信
- AI 应用不再接触 MCU
- AI 应用独占摄像头采集和录像
- AI 应用负责得分分析和仲裁
- PisteLink 后端负责音频播放、`json.txt` 写入和 FTP 上传

这对系统稳定性是有利的。当前 `control_fencing.py` 混合了 UI、串口、录像、分析、音频和远程上传。贵方协议可以让 AI 服务更专注，减少 UI、串口状态机、音频播放和上传逻辑对视觉分析的干扰。

### 3.2 Unix Domain Socket 合适

PisteLink 和 AI 应用在同一台 Jetson 上，使用 UDS 是合理选择：

- 不需要 TCP 端口管理
- 没有网络时钟同步问题
- 可靠、有序
- 延迟低
- 权限可以通过 socket 文件控制

NDJSON 单行消息也适合调试，后期可以用 `socat` 或简单 Python 脚本模拟。

### 3.3 握手、心跳和重连规则合理

贵方协议有：

- `hello` / `hello_ack`
- `protocol_v`
- `ping` / `pong`
- 2 秒空闲心跳
- 6 秒无消息断连
- 后端指数退避重连

这些对长期运行的比赛系统是必要的。AI 作为服务端，后端作为客户端，也符合“AI 摄像头服务先启动并等待业务后端连接”的部署模型。

### 3.4 `match_id` 作为目录名是好的

贵方协议把 `match_id` 定义为后端收到 MCU `0x50` 的毫秒墙钟字符串，并要求视频写入：

```text
<storage_root>/matches/<match_id>/
```

这有利于：

- 后端、AI 和上传流程共享一个比赛目录
- 避免当前 `<timestamp>_phraseNN` 命名和后端比赛 ID 之间再做映射
- 日志、视频、结果天然归档到同一个目录

### 3.5 `video_sync_offset_ms` 的概念是正确的

协议承认电信号和视频之间可能存在固定延迟差，并提供：

```text
signal_ts = recv_ts + video_sync_offset_ms
```

这是一个好的起点。实际系统中，电路检测、MCU 发送、串口接收、后端解析、摄像头曝光、视频采集都会引入延迟。提供一个可配置 offset 可以校准固定偏差。

但它只能处理“固定偏差”，不能单独解决帧级精确映射问题。

---

## 4. 最大风险：电信号时间到视频帧的映射

### 4.1 为什么有 timestamp 还不够

贵方协议里的 `signal_ts` 代表某个电信号事件的时间，例如：

```json
{
  "fight": 3,
  "source": "hit",
  "signal_ts": 1778397808086
}
```

这个时间戳可以很准确地表示“后端认为事件发生在某个墙钟时间”。但是视觉裁判最终需要的是：

```text
这个 blade contact / hit 对应视频里的第几帧
```

这二者之间隔着摄像头采集链路：

- 摄像头曝光时间
- USB 或 V4L2 缓冲
- GStreamer / OpenCV 读取延迟
- 写视频线程排队
- 系统调度抖动
- 实际 FPS 偏离 30
- 个别帧丢失或延迟

因此，一个准确的电信号时间戳只是必要条件，不是充分条件。

### 4.2 危险的简单算法

最简单的做法是：

```text
frame = round((signal_ts - begin_ts) * 30 / 1000)
```

这个算法隐含假设：

- 视频第一帧刚好等于 `begin_ts`
- FPS 永远正好是 30
- 没有丢帧
- 没有采集抖动
- `signal_ts` 和视频采集时间没有动态偏移

这些假设在真实 Jetson 摄像头管线中不可靠。即使平均 FPS 接近 30，也可能某些帧间隔是 20 ms、40 ms 或 60 ms。对击剑来说，偏差几个 frame 就可能改变 blade contact 与 attack/lunge/arm extension 的相对位置。

### 4.3 当前管线已经做得更强

当前 `control_fencing.py` 会记录每个录制帧的 host timestamp，并在停止录制后重写 TXT 中的 frame 映射。也就是说，当前系统的思路是：

```text
Arduino 事件时间
  -> 估计到 host 时间
  -> 与实际录制帧 timestamps 对齐
  -> 写成 frame N
```

这比只用名义 FPS 更强。

新 PisteLink 协议应当保留并加强这个思路：

```text
PisteLink signal_ts
  -> AI 侧实际 frame_ts 列表
  -> nearest / containing / at_or_after frame
  -> 裁判使用该 frame
```

### 4.4 为什么 blade contact 特别敏感

当前 FPS30 裁判会把 blade contact 作为 right-of-way 判断的重要事件之一。它会分析：

- blade contact 前后双方手臂和身体状态
- contact 是否可能是 accident
- contact 后谁获得进攻收益
- contact 是否早于 hit
- contact 与 pause/retreat/lunge/arm extension 的关系

如果 blade contact 被映射到错误 frame，可能导致：

- contact 前后窗口取错
- accident 判断偏移
- benefit side 判断错误
- 误判谁在 blade contact 后获得主动权
- 与 hit 的先后关系被改变

这就是为什么“时间戳准确”还不够，必须“映射到视频帧准确”。

---

## 5. 建议贵方补充或调整的协议内容

### 5.1 增加 monotonic 时间戳

贵方当前协议所有时间戳都使用：

```text
time.time_ns() // 1_000_000
```

也就是 Unix epoch 毫秒墙钟。这适合日志、目录名和跨进程可读性，但不适合作为最高精度的时间基准，因为系统墙钟理论上可能被校时或调整。

建议贵方保留现有字段，同时增加可选 monotonic 字段：

```json
{
  "v": 1,
  "type": "signal",
  "id": 4,
  "ts": 1778397808030,
  "ts_mono_ns": 123456789000000,
  "match_id": "1778397800089",
  "payload": {
    "fight": 3,
    "source": "hit",
    "signal_ts": 1778397808086,
    "signal_mono_ns": 123456847000000
  }
}
```

建议语义：

- `ts`：发送消息的 epoch ms，用于日志和人读
- `ts_mono_ns`：发送消息的 monotonic ns，用于本机进程间时间分析
- `signal_ts`：校正后的电信号 epoch ms
- `signal_mono_ns`：校正后的电信号 monotonic ns

如果暂时不能提供 `signal_mono_ns`，至少应让 AI 使用自己的 frame epoch ms 列表和 `signal_ts` 做映射。

### 5.2 明确 `signal_ts` 的来源

贵方当前协议写的是：

```text
signal_ts = recv_ts + video_sync_offset_ms
```

建议贵方进一步区分：

- `mcu_event_ts`：MCU 认为事件发生的时间
- `backend_recv_ts`：后端收到该 MCU 帧的时间
- `signal_ts`：用于视频对齐的校正后时间
- `video_sync_offset_ms`：本次使用的校正 offset

示例：

```json
{
  "payload": {
    "fight": 3,
    "source": "hit",
    "backend_recv_ts": 1778397808094,
    "signal_ts": 1778397808086,
    "video_sync_offset_ms": -8,
    "mcu_event_us": 91234567
  }
}
```

如果新 MCU 能提供事件发生时的 MCU timestamp，应尽量传给后端，再由后端换算成 Jetson 时间。这样可以减少串口传输和后端调度抖动对事件时间的影响。

如果 MCU 只能提供事件类型，不能提供事件时间，那么 `backend_recv_ts + offset` 也可用，但需要承认它是较弱的时间源。

### 5.3 `camera_ready` 应携带录像时间元数据

贵方当前协议中，后端在收到 `camera_ready` 后记录 `beginTimeStamp`，再发 `match_begin_ack`。这个 `begin_ts` 是后端收到 AI ready 的时间，不一定等于：

- AI 开始录制的时间
- 第一帧曝光时间
- 第一帧进入 AI 进程的时间
- 视频文件第 0 帧的时间

建议贵方允许 `camera_ready` 增加：

```json
{
  "type": "camera_ready",
  "match_id": "1778397800089",
  "payload": {
    "video_path": "/var/lib/pistelink/matches/1778397800089/segment_1778397800.mp4",
    "recording_start_ts": 1778397803012,
    "recording_start_mono_ns": 123456700000000,
    "first_frame_ts": 1778397803041,
    "first_frame_mono_ns": 123456729000000,
    "first_frame_index": 0,
    "fps_nominal": 30.0,
    "width": 1280,
    "height": 720
  }
}
```

这样后端和 AI 都能清楚地区分：

- 比赛目录何时创建
- AI 何时开始录像
- 第一帧实际何时出现
- 后端何时认为比赛开始

### 5.4 AI 必须保存 frame timestamp sidecar

建议贵方协议明确允许并推荐 AI 在比赛目录保存每帧采集时间戳，例如：

```text
/var/lib/pistelink/matches/<match_id>/
  segment_1778397800.mp4
  frame_timestamps.jsonl
```

每行：

```json
{"frame":0,"ts":1778397803041,"mono_ns":123456729000000}
{"frame":1,"ts":1778397803074,"mono_ns":123456762000000}
{"frame":2,"ts":1778397803107,"mono_ns":123456795000000}
```

AI 内部裁判应使用这个 sidecar 做事件映射。这个文件也方便离线排查：

- signal_ts 是否落在视频范围内
- 映射到哪一帧
- 实际 FPS 是否稳定
- 是否丢帧
- 某个误判是否来自时间映射偏差

### 5.5 明确 event-to-frame 映射规则

建议贵方协议或双方实现文档明确使用哪种映射模式：

```text
nearest:      选择时间戳最近的一帧
containing:   选择事件所在时间区间对应的帧
at_or_after:  选择第一帧时间 >= 事件时间的帧
```

当前系统不同场景可能需要不同模式：

- blade contact：通常可用 `nearest` 或 `containing`
- hit：可能更适合 `containing`
- lockout start：需要和 hit 逻辑保持一致
- phrase start/end：通常用于裁剪范围，不应过度依赖单帧精度

建议至少允许 AI 在结果 JSON 中记录：

```json
{
  "signal_frame_mapping": [
    {
      "fight": 3,
      "signal_ts": 1778397808086,
      "mapped_frame": 152,
      "mapped_frame_ts": 1778397808083,
      "delta_ms": 3,
      "mode": "nearest"
    }
  ]
}
```

### 5.6 `video_sync_offset_ms` 应记录到每场结果中

`video_sync_offset_ms` 不应只存在于 UI 配置中。建议贵方在每个 `signal` 或至少每场 metadata 中记录实际使用的 offset。

原因：

- 后期复盘需要知道当时校准值
- 不同设备、摄像头、分辨率、采集后端可能 offset 不同
- 如果用户改过 offset，需要能追踪某场结果使用了哪个值

---

## 6. 与当前管线适配时需要明确的语义

### 6.1 A/B 与 left/right 的映射

当前 AI 裁判使用的是：

```text
left
right
```

贵方协议使用：

```text
A
B
```

并且 `fight` 定义为：

```text
8 = A 方得分
9 = B 方得分
10 = 双方得分 / 互中
```

必须明确：

- A 是视频左侧还是视频右侧
- A 是 piste 左侧还是 piste 右侧
- A 是否永远对应某个物理灯
- 摄像头方向反装时如何处理

建议在 `match_pre_start` payload 中增加：

```json
{
  "side_map": {
    "A": "left",
    "B": "right"
  }
}
```

或：

```json
{
  "piste_orientation": "camera_facing_scores_table",
  "a_video_side": "left"
}
```

如果不明确，AI 可能把 `fight=8` 转成错误的 winner。

### 6.2 `fight` 值到当前 TXT 的转换

短期适配时，可以把 `fight` 转换成当前 TXT 需要的文本。

假设 A=Left，B=Right：

| fight | 当前 TXT 等价事件 |
|---|---|
| 3 | `Off-Target: Blade-to-blade contact.` |
| 8 | `HIT: Left scores on Right!` |
| 9 | `HIT: Right scores on Left!` |
| 10 | `HIT: Left scores on Right!` + `HIT: Right scores on Left!` + `HIT: Simultaneous valid hits!` |

如果 A=Right，B=Left，则 8 和 9 的转换相反。

### 6.3 `source:"hit"` 和 `source:"light"` 的含义

协议中 `signal` 的 `source` 有：

```text
"hit"   = 来自击打帧
"light" = 来自 0x52（亮灯回合结束）
```

但 AI 需要知道哪一个事件表示“phrase 可以结束并开始裁判”。

建议补充：

- `source:"hit"` 是否可能出现多次
- `source:"light"` 是否一定是本回合最终灯态
- `source:"light"` 是否应触发停止录像
- `fight=8/9/10` 与 `source:"light"` 哪个更权威
- 如果 `hit` 和 `light` 内容冲突，应以哪个为准

为了当前管线，最好增加：

```json
{
  "payload": {
    "fight": 10,
    "source": "light",
    "terminal": true,
    "lockout_ms": 200
  }
}
```

这样 AI 不需要猜测何时停止录像。

### 6.4 lockout 开始时间

当前 TXT 中有：

```text
Lockout period started (0.200s window)
```

FPS30 裁判会解析 lockout 时间。贵方 v1.0 协议没有单独的 `lockout_start` 事件。

建议补充以下任一方式：

方式 A：新增事件类型：

```json
{
  "type": "lockout_start",
  "match_id": "...",
  "payload": {
    "lockout_start_ts": 1778397811808,
    "lockout_ms": 200
  }
}
```

方式 B：在第一个有效 hit signal 中带上：

```json
{
  "payload": {
    "fight": 8,
    "source": "hit",
    "signal_ts": 1778397811808,
    "starts_lockout": true,
    "lockout_ms": 200
  }
}
```

方式 C：规定 AI 以首个 `fight` 为 8/9/10 的 `signal_ts` 作为 lockout start。

方式 C 最少改协议，但需要明确写进协议，否则实现容易分歧。

### 6.5 单灯、双灯和无效灯逻辑

当前系统用 scoreboard line 判断：

```text
Scores -> Fencer 1: HIT, Fencer 2: MISS
Scores -> Fencer 1: HIT, Fencer 2: HIT
```

FPS30 对 double hit 和 single hit 的处理不同。贵方协议应明确最终灯态如何表达。

建议 `source:"light"` 事件 payload 中增加：

```json
{
  "final_lights": {
    "A": true,
    "B": true
  }
}
```

或者定义 `fight=8/9/10` 的 `source:"light"` 一定代表最终灯态。

### 6.6 weapon 对裁判策略的影响

协议中 `weapon` 定义：

```text
1 = 花剑
2 = 重剑
3 = 佩剑
```

当前 FPS30 裁判偏向 sabre/right-of-way 场景。不同 weapon 的处理应该不同：

- 佩剑：需要 right-of-way 分析，当前管线最匹配。
- 花剑：也需要 right-of-way，但有效/无效部位语义不同。
- 重剑：通常不需要 right-of-way，双灯就是双中，单灯直接按信号结果。

建议我方 AI 服务在 `match_pre_start` 记录 weapon，并在结果决策中明确：

```text
weapon=2/epee 时，不应使用 sabre ROW 逻辑覆盖电信号结果。
weapon=3/sabre 时，使用当前 FPS30 ROW 分析。
weapon=1/foil 时，需要额外确认当前模型是否适合。
```

---

## 7. 我方推荐的接入架构

### 7.1 不建议直接大改现有 `control_fencing.py`

`control_fencing.py` 现在包含 UI、串口、录像、音频、上传、本地分析等多种职责。如果直接在这个文件里加入 PisteLink UDS 服务，会让状态机更复杂。

我方建议新建一个独立 AI 服务入口，例如：

```text
jetson_orin_nano_bundle/pistelink_ai_service.py
```

它只做 PisteLink 协议服务，不启动 Tk UI，不直接读 Arduino。

### 7.2 新 AI 服务的内部模块

建议拆分为：

```text
pistelink_ai_service.py
  - UDS bind/listen/accept
  - hello/heartbeat/reconnect
  - match lifecycle state machine

pistelink_protocol.py
  - envelope parsing
  - message validation
  - id/ts generation
  - ping/pong helpers

pistelink_signal_adapter.py
  - PisteLink signal -> internal structured signal
  - structured signal -> legacy TXT
  - A/B -> left/right mapping
  - signal_ts -> frame mapping

camera_recording_service.py
  - current CameraRecorder reuse or extraction
  - video output path control
  - frame timestamp sidecar

analysis_session_adapter.py
  - LocalStreamingSessionManager reuse
  - frame push
  - session_end with synthesized TXT
  - result normalization to PisteLink match_result
```

这会比把协议逻辑塞进现有 UI 文件更容易测试。

### 7.3 短期接入路径

短期我方建议最小化风险：

1. 复用当前摄像头 recorder。
2. 复用当前 `LocalStreamingSessionManager`。
3. 复用当前 `live_stream_service.py`。
4. 把贵方 PisteLink 的 `signal` 转成当前 TXT。
5. 让 FPS30 裁判保持现状。
6. 最终把 `left/right/tie` 转成 `A/B/tie`，发送 `match_result`。

这样可以快速拿现有录制和历史 phrase 做对照测试。

### 7.4 长期接入路径

长期建议消除 TXT 中间层：

1. 定义内部 `SignalEvent` dataclass。
2. 让 `debug_referee_fps30.py` 支持直接接收结构化 signals。
3. 让 `live_stream_service.py` 的 finalize 接受 signal object，而不是只接受 TXT 文件。
4. 把 frame mapping metadata 写入 `analysis_result.json`。
5. 保留 TXT 导出只作为调试和兼容产物。

这样会减少文本 regex 解析的脆弱性。

---

## 8. 建议的时间对齐实现细节

### 8.1 AI 侧记录每帧时间

每次 camera recorder 捕获 frame 时，记录：

```text
frame_index
epoch_ms
monotonic_ns
capture_backend_timestamp 如果底层可得
```

不要只在写视频成功后记录。最可靠的是在 frame 进入 AI 进程、准备写入视频和送分析器时记录同一个 index。

### 8.2 事件映射函数

内部实现一个统一函数：

```text
map_signal_to_frame(signal_ts, frame_timestamps, mode) -> FrameMapping
```

返回：

```json
{
  "signal_ts": 1778397808086,
  "mapped_frame": 152,
  "mapped_frame_ts": 1778397808083,
  "delta_ms": 3,
  "mode": "nearest",
  "confidence": "high"
}
```

如果 `abs(delta_ms)` 太大，应降级 confidence，例如：

```text
<= 20 ms: high
<= 50 ms: medium
> 50 ms: low，需要在结果中告警
```

### 8.3 写入 legacy TXT 时带 frame

短期生成当前 TXT 时，建议格式仍兼容现有 parser：

```text
 0.733s | frame 000022 | Off-Target: Blade-to-blade contact.
```

需要注意：当前 FPS30 TXT parser 主要使用行首的秒数来重新映射视频帧；`frame 000022` 更多是日志审计信息，并不是所有解析路径都会直接使用这个 frame token。因此短期 adapter 必须保证行首秒数字段本身就能映射到正确的视频帧，不能只依赖 `frame 000022` 文本。

对 side-hit 行还有一个实现细节：当前显式 side-hit regex 更容易解析这种格式：

```text
 0.733s | HIT: Left scores on Right!
```

如果生成这种格式：

```text
 0.733s | frame 000022 | HIT: Left scores on Right!
```

则应同步调整 parser，让它接受可选的 `frame N |` 片段。否则 side-hit 的显式左右事件可能无法被该 regex 捕获，最后只能退回 simultaneous-hit fallback。这个问题不需要贵方处理，但我方实现 PisteLink adapter 时必须处理。

其中 `0.733s` 可以来自：

```text
(mapped_frame_ts - first_frame_ts) / 1000
```

或：

```text
(signal_ts - phrase_zero_ts) / 1000
```

建议优先使用和 frame 映射一致的时间基准，并在 metadata 中记录原始 `signal_ts`。

### 8.4 避免只依赖视频容器 FPS

视频容器中的 FPS 常常只是 nominal fps。当前系统已经有 `video_timing.py` 去推断帧时间，但对于实时采集，AI 自己保存的 frame timestamps 更直接、更可信。

因此优先级建议：

1. AI 采集时保存的 per-frame timestamps
2. ffprobe 读取的视频 PTS
3. OpenCV nominal fps fallback

---

## 9. 建议的协议补充示例

### 9.1 `camera_ready`

```json
{
  "v": 1,
  "type": "camera_ready",
  "id": 1,
  "ts": 1778397803050,
  "ts_mono_ns": 123456738000000,
  "match_id": "1778397800089",
  "payload": {
    "video_path": "/var/lib/pistelink/matches/1778397800089/segment_1778397800.mp4",
    "recording_start_ts": 1778397803012,
    "recording_start_mono_ns": 123456700000000,
    "first_frame_ts": 1778397803041,
    "first_frame_mono_ns": 123456729000000,
    "first_frame_index": 0,
    "fps_nominal": 30.0,
    "width": 1280,
    "height": 720,
    "frame_timestamps_path": "/var/lib/pistelink/matches/1778397800089/frame_timestamps.jsonl"
  }
}
```

### 9.2 `match_pre_start`

```json
{
  "v": 1,
  "type": "match_pre_start",
  "id": 1,
  "ts": 1778397800100,
  "match_id": "1778397800089",
  "payload": {
    "weapon": 3,
    "sensor": 0,
    "storage_root": "/var/lib/pistelink",
    "match_dir": "/var/lib/pistelink/matches/1778397800089",
    "side_map": {
      "A": "left",
      "B": "right"
    }
  }
}
```

### 9.3 `signal`

```json
{
  "v": 1,
  "type": "signal",
  "id": 4,
  "ts": 1778397808030,
  "ts_mono_ns": 123456818000000,
  "match_id": "1778397800089",
  "payload": {
    "fight": 3,
    "source": "hit",
    "backend_recv_ts": 1778397808094,
    "signal_ts": 1778397808086,
    "signal_mono_ns": 123456874000000,
    "video_sync_offset_ms": -8,
    "terminal": false
  }
}
```

### 9.4 phrase 结束信号

```json
{
  "v": 1,
  "type": "signal",
  "id": 8,
  "ts": 1778397811865,
  "match_id": "1778397800089",
  "payload": {
    "fight": 10,
    "source": "light",
    "signal_ts": 1778397811808,
    "video_sync_offset_ms": -57,
    "terminal": true,
    "lockout_ms": 200,
    "final_lights": {
      "A": true,
      "B": true
    }
  }
}
```

### 9.5 `match_result`

建议贵方允许 AI 返回结果时增加可选 debug metadata，不影响后端现有字段：

```json
{
  "v": 1,
  "type": "match_result",
  "id": 20,
  "ts": 1778397812500,
  "match_id": "1778397800089",
  "payload": {
    "winner": "A",
    "result_code": 8,
    "video_path": "/var/lib/pistelink/matches/1778397800089/segment_1778397800.mp4",
    "analysis_result_path": "/var/lib/pistelink/matches/1778397800089/analysis_result.json",
    "signal_frame_mapping_path": "/var/lib/pistelink/matches/1778397800089/signal_frame_mapping.json",
    "processing_mode": "live_streaming"
  }
}
```

---

## 10. 测试建议

### 10.1 协议层测试

使用 fake PisteLink client 测试：

- 连接 UDS
- 发送 `hello`
- 校验 `hello_ack`
- 心跳超时
- 非法 JSON 不断连
- 未知 type 被忽略
- 断连后 AI 回到可连接状态

### 10.2 生命周期测试

模拟：

```text
match_pre_start
camera_ready
match_begin_ack
voice_end
signal fight=3
signal fight=8
signal source=light terminal=true
match_result
```

检查：

- 视频是否写到 match 目录
- frame timestamp sidecar 是否存在
- 合成 TXT 是否可被当前 FPS30 parser 解析
- `match_result.video_path` 是否在 match 目录内

### 10.3 时间映射测试

构造 frame timestamps：

```text
frame 0: 1000 ms
frame 1: 1033 ms
frame 2: 1067 ms
```

测试：

- signal at 1031 ms -> frame 1
- signal at 1049 ms -> nearest frame 1 or containing frame 1，取决于 mode
- signal before first frame -> 告警或 clamp
- signal after last frame -> 告警或 clamp
- frame 间隔异常 -> confidence 降级

### 10.4 与当前 Arduino 输出对照

选取已有 recordings 中的 phrase：

1. 从旧 TXT 解析出 hit/contact 事件。
2. 转成 PisteLink `signal` 序列。
3. 再由 adapter 生成新 TXT。
4. 对比旧 TXT 和新 TXT 的裁判结果是否一致。

这可以验证“结构化信号 -> legacy TXT -> FPS30”的短期接入方案是否安全。

### 10.5 真实端到端测试

在 Jetson 上跑完整流程：

1. PisteLink 发送 `match_pre_start`。
2. AI 开始录制并返回 `camera_ready`。
3. 后端发送 signals。
4. AI 实时 tracking。
5. terminal signal 到达后 AI 停止录制。
6. AI 发送 `match_result`。
7. 后端播放音频并写 `json.txt`。

重点检查：

- camera_ready 到第一帧的延迟
- signal_ts 到 mapped_frame_ts 的 delta
- 有无丢帧导致 fallback
- 结果延迟是否满足预期

---

## 11. 双方推荐落地顺序

### 阶段 1：协议桥接，不改裁判核心

- 新建 PisteLink AI service。
- 实现 UDS、握手、心跳。
- 复用当前 camera recorder。
- 复用当前 live streaming analyzer。
- 把 PisteLink signals 转成 legacy TXT。
- 返回 `match_result`。

目标：最短时间跑通端到端，并和当前 Arduino 管线做结果对比。

### 阶段 2：加强时间对齐

- 保存 `frame_timestamps.jsonl`。
- 实现 `signal_ts -> frame` 映射。
- 生成 `signal_frame_mapping.json`。
- 在 `analysis_result.json` 中记录映射质量。
- 对映射 delta 过大的事件给出 warning。

目标：达到或超过当前 Arduino 管线的时间映射精度。

### 阶段 3：结构化信号进入裁判

- 定义内部 structured signal schema。
- 改造 FPS30 parser 支持结构化输入。
- TXT 变为调试导出，而不是主输入。

目标：降低 regex 文本解析风险，提高长期可维护性。

### 阶段 4：按 weapon 分流

- sabre 使用当前 ROW/FPS30 分析。
- epee 使用电信号结果为主。
- foil 单独确认有效/无效部位和 ROW 逻辑。

目标：避免用同一套 sabre 假设处理所有剑种。

---

## 12. 需要贵方进一步确认的问题

1. A/B 到视频 left/right 的固定映射是什么？
2. 摄像头是否永远由 AI 独占？PisteLink 是否完全不打开摄像头？
3. `source:"light"` 是否一定表示 phrase 结束？
4. 如果 `source:"hit"` 和 `source:"light"` 的 fight 值冲突，以哪个为准？
5. `fight=10` 是否一定代表最终双灯，还是可能是中间 hit？
6. `video_sync_offset_ms` 是全局配置、每设备配置，还是每场可变？
7. 新 MCU 是否能提供事件发生时的 MCU timestamp？
8. 后端能否把 `backend_recv_ts`、`video_sync_offset_ms` 和 `mcu_event_us` 一并发给 AI？
9. 期望 AI 输出 AVI 还是 MP4？如果必须 MP4，是否允许 AI 先写 AVI 再转码？
10. AI 的分析中间产物是否允许放在 match 目录下？
11. `json.txt` 的具体 schema 是否会提供给 AI 用于离线验证？
12. 如果 AI 返回 `camera_error`，后端删除目录前是否需要保留错误日志？

---

## 13. 回复结论与下一步建议

贵方协议适合作为新系统基础，但为了适配我方当前低延迟裁判管线并达到高精度映射，建议贵方在 v1.1 或联调说明中补强三项关键内容：

1. 明确事件时间的来源，最好增加 monotonic timestamp 和 MCU event timestamp。
2. 明确要求 AI 保存每帧真实采集时间戳，并用它做 `signal_ts -> frame` 映射。
3. 明确 A/B 与 left/right、terminal signal、lockout start、final lights 的语义。

我方短期不会重写整个裁判核心。更稳妥的做法是：先把贵方 PisteLink 结构化信号稳定转换为我方当前 TXT 合同，复用现有 live streaming analyzer 和 FPS30 judge；等端到端结果稳定后，再把 TXT 输入替换成结构化 signal 输入。

建议双方下一步先确认第 12 节的问题，尤其是 A/B 映射、terminal signal、lockout 语义和 `signal_ts` 来源。确认后，我方可以按第 11 节阶段 1 先实现协议桥接服务，尽快进入端到端联调。

---

## 14. 本次逐点复核结论

本节是对以上建议的逐点复核。结论是：文档中的建议整体有效；如果贵方和我方按这些建议落实，可以解决“PisteLink 电信号如何接入当前 AI 管线”和“电信号如何高精度映射到视频帧”这两个核心问题。不过，这些建议不能单独保证所有 AI 裁判结果都正确；姿态识别、双人 tracking、摄像头画质、模型文件、具体剑种规则和裁判算法本身仍然需要独立验证。

### 14.1 时间映射相关建议复核

| 建议 | 是否有效 | 为什么有效 | 必要条件或 caveat |
|---|---|---|---|
| 明确 `signal_ts` 来源 | 有效且必须 | 我方需要知道该时间代表 MCU 真实检测时间、后端接收时间，还是校正后的对齐时间 | 如果只提供 backend receive time，仍可用，但精度受串口和后端调度 jitter 限制 |
| 增加 `backend_recv_ts`、`video_sync_offset_ms`、`mcu_event_us` | 有效 | 可以分解事件时间来源，便于审计误差来自 MCU、串口、后端还是校准 offset | 若 MCU 无法提供事件时间，应至少记录 backend receive time 和 offset |
| 增加 monotonic timestamp | 有效，但不是单独充分条件 | 同一台 Jetson 上 monotonic clock 更适合跨进程时间差计算，不受墙钟调整影响 | monotonic 只能改善时间基准，不能替代真实事件时间或 frame timestamp |
| `camera_ready` 返回录像开始和第一帧时间 | 有效 | `begin_ts` 是后端收到 ready 的时间，不等于视频第 0 帧时间 | 如果 `camera_ready` 发送时第一帧尚未到达，应在后续 metadata 或 sidecar 中补齐 |
| AI 保存 `frame_timestamps.jsonl` | 有效且必须 | 这是 `signal_ts -> frame` 高精度映射的核心依据 | timestamp 必须和实际写入视频、送入分析器的 frame index 一致 |
| 使用实际 frame timestamp 而不是名义 FPS | 有效且必须 | 可处理 FPS 抖动、采集延迟、轻微丢帧和容器 FPS 不准的问题 | 如果 frame timestamp 缺失，才退回 ffprobe PTS 或 nominal FPS |
| 明确 `nearest` / `containing` / `at_or_after` 映射模式 | 有效 | 不同事件的物理含义不同，映射策略应可解释、可复现 | 具体默认模式需要用历史 phrase 和真实联调数据校验 |
| 记录 `signal_frame_mapping.json` | 有效 | 可审计每个电信号最终映射到哪一帧、偏差多少 ms | 这是排障能力，不是协议主流程必需字段 |
| 保存每场使用的 `video_sync_offset_ms` | 有效 | 后期复盘必须知道当时使用了哪个校准值 | offset 只能修正固定偏差，不能修正随机 jitter |

如果以上时间相关项都落实，并且 `signal_ts` 的来源足够稳定，则可以达到或超过当前 Arduino 管线的时间映射精度。最关键的是：AI 必须用真实 frame timestamp 列表做映射，而不是用 `begin_ts + 30 FPS` 估算。

### 14.2 事件语义相关建议复核

| 建议 | 是否有效 | 为什么有效 | 必要条件或 caveat |
|---|---|---|---|
| 明确 A/B 与视频 left/right 映射 | 有效且必须 | 我方视觉和裁判内部使用 left/right，贵方协议和结果使用 A/B | 需要支持摄像头方向或场地布置变化 |
| `match_pre_start` 携带 `side_map` | 有效 | 每场比赛都能明确 A/B 与 AI left/right 的关系 | 如果 side_map 是全局配置，也应在每场 metadata 中记录当时配置 |
| 明确 `source:"hit"` 与 `source:"light"` 的权威性 | 有效且必须 | AI 需要知道哪些是中间事件，哪些是最终灯态 | 如果二者冲突，必须有固定优先级 |
| 增加 `terminal` 标记 | 有效 | AI 可明确停止录像并触发最终分析 | 如果不用 `terminal`，则必须定义 `source:"light"` 等价于 terminal |
| 增加 `final_lights` | 有效 | 当前裁判需要知道 single hit / double hit / tie 的最终灯态 | 也可由 `fight=8/9/10` 表达，但语义必须明确 |
| 明确 lockout start 和 lockout_ms | 有效且必须 | 当前 FPS30 裁判会使用 lockout 约束 hit/blade contact 关系 | 可新增事件，也可在首个有效 hit/light 中标记 |
| 明确 `voice_end` 与真正可比赛开始时间 | 有效 | 视频可能包含 start audio 前后的预录内容，AI 需要知道 active fencing window | 若裁判只使用 hit 前窗口，也仍建议记录，便于裁剪和排查 |
| 明确不同 weapon 的判定边界 | 有效 | 重剑、花剑、佩剑的裁判逻辑不同 | 当前 AI ROW 逻辑主要适合佩剑/right-of-way 场景 |

这些建议可以解决结构化电信号进入当前 AI 裁判时的语义歧义问题。若不解决这些问题，即使时间映射很准，也可能因为左右方向、终止条件或剑种规则错误而得到错误结果。

### 14.3 架构和兼容策略复核

| 建议 | 是否有效 | 为什么有效 | 必要条件或 caveat |
|---|---|---|---|
| 新建独立 PisteLink AI service | 有效 | 当前 `control_fencing.py` 职责过多，直接加入 UDS 会让状态机复杂化 | 需要复用 recorder 和 analyzer 代码，避免复制出第二套逻辑 |
| 短期使用 structured signal -> legacy TXT adapter | 有效 | 最小化对现有 FPS30 裁判的改动，便于用历史数据对照 | TXT 秒数字段必须准确；如果保留 `frame N` token，需要 parser 支持 |
| 长期改造裁判接收 structured signal | 有效 | 减少 regex 文本解析风险，提升可维护性 | 应在短期路径稳定后进行，避免一次性改动过大 |
| 复用 live streaming analyzer | 有效 | 当前低延迟优势来自 phrase 期间实时 tracking，应该保留 | 需要保证 PisteLink AI service 的录制帧与 analyzer frame index 一致 |
| 允许 AI 写分析中间产物 | 有效 | 便于调试和追踪结果来源 | 需要贵方后端允许 match 目录中存在额外 JSON/JSONL 文件 |
| `match_result` 增加可选 debug metadata | 有效 | 不影响主字段，又能减少联调排障成本 | 后端应忽略未知字段或按可选字段处理 |

因此，架构建议是有效的。短期 adapter 方案能快速接入，但要注意 TXT parser 当前不是所有路径都直接读取 `frame N` token，所以我方实现时需要保证秒数字段和 parser 行为一致。

### 14.4 仍然不能仅靠协议完全解决的问题

即使贵方按本文件建议补强协议，以下问题仍需要我方单独验证或实现，不能认为协议调整后自动解决：

1. YOLO pose 模型是否在部署机器上存在且路径正确。
2. 摄像头画面、曝光、分辨率、畸变和视角是否适合当前 tracking。
3. 双人 tracking 是否在遮挡、交叉、出画面时稳定。
4. 佩剑、花剑、重剑是否都应使用同一套 AI 裁判逻辑。
5. 当前 FPS30 裁判规则是否完全符合目标比赛规则。
6. 生成 MP4 是否会增加停止后的转码延迟；如果会，是否允许先写 AVI/MJPG 再转码。
7. 硬件电信号检测本身是否准确；AI 只能对收到的 signal 做映射和分析。
8. 低延迟模式下若实时帧丢失，是否正确 fallback 到离线完整处理。

换句话说，本文件的建议可以解决协议对接和高精度时间映射的关键问题，但不能替代视觉模型、硬件检测和裁判规则本身的验证。

### 14.5 复核后的最终判断

逐点复核后，文档建议是有效的，但需要用更精确的表述理解：

```text
协议补强 + AI 真实 frame timestamps + 明确事件语义
  -> 可以实现高精度电信号到视频帧映射
  -> 可以让 PisteLink 协议稳定适配当前低延迟 AI 管线

但：
高精度时间映射
  != 自动保证所有视觉识别、tracking 和裁判规则都正确
```

因此，和贵方沟通时应强调：我们要求补强这些协议字段，是为了让电信号与视频帧之间的映射可计算、可审计、可复现。实现这些字段后，时间对齐问题可以被系统性解决；剩余的视觉和裁判逻辑问题，则应通过端到端联调和历史 phrase 回放继续验证。
