# PisteLink ↔ AI 应用 接口协议与协作说明

- 版本：v1.1
- 日期：2026-05-25
- 依据：`软件需求说明书.md` v0.10 附录 A；甲方《AI应用接口协议对接评审与时间对齐建议》
- v1.0 → v1.1 变更见 §0

---

## 0. v1.1 变更摘要

本版基于甲方对 v1.0 的评审意见调整，主要变更：

1. **回合结束信号补全双方最终灯态**：`signal`（`source:"light"`）增加 `final_lights`（A、B 各一布尔）与 `terminal:true`。v1.0 仅反映了一方灯态，本版修正为双方。
2. **A/B ↔ left/right 映射**：`match_pre_start` 新增 `side_map`，固定 `A=left, B=right`。
3. **json.txt 先写后改**：0x52 到达即写 json.txt（电信号 + 临时结果），AI 的 `match_result` 到达后回填/修正；AI 超时（默认 8s）则以 `result_code=0` finalize。详见 §13。
4. **fight 取值表订正**：删除 `fight=10`（MCU 不上报该值）；平局 = 时间窗内分别收到 `8` 和 `9`。最终结果汇总码 `result_code=10`（平局）保留。
5. **可选 monotonic 时间戳**：信封新增可选 `ts_mono_ns`，`signal` payload 新增可选 `signal_mono_ns`，用于本机进程间高精度时间差。
6. **camera_ready 可携带录像元数据**：允许 AI 回带第一帧时间、FPS、分辨率、帧时间戳 sidecar 路径等可选字段（§8.1）。
7. **AI 分析中间产物目录**：允许 AI 在 `matches/<match_id>/ai/` 子目录写中间产物；该子目录**不上传**、不计入完成判定（§12.1）。
8. **lockout 说明**：MCU 无时间概念、不提供 lockout 起始/时长，由 AI 自行处理（§7.4.3）。
9. **不下发 offset**：`signal` 只携带已修正的 `signal_ts`，**不**单独下发 `video_sync_offset_ms`（该值是客户设定的静态误差，后端只提供设置接口，对外仅给修正结果）。

向后兼容：以上除 0x52 字段语义补全与 fight 表订正外，均为新增可选字段，AI 侧可渐进采纳。

---

## 1. 概述

PisteLink 后端与 AI 应用运行在同一台 Jetson Orin Nano 上，通过 **Unix Domain Socket（UDS）** 通信。两方职责边界：

| 职责 | PisteLink 后端 | AI 应用 |
|---|---|---|
| 串口通信（MCU） | 负责 | 不涉及 |
| 摄像头采集与录像 | 不涉及 | **独占（永远由 AI 持有）** |
| 得分分析/仲裁 | 不涉及 | 负责 |
| 音频播放 | 负责 | 不涉及 |
| 电信号文件（json.txt）写入 | 负责 | 不涉及 |
| 视频文件写入 | 不涉及 | 负责 |
| FTP 上传 | 负责（按用户操作） | 不涉及 |

> 摄像头由 AI 应用永久独占，后端不打开摄像头。后端只负责通知 AI 工作、接收 AI 结果，并按用户操作把视频/json.txt 上传到服务器。

---

## 2. 传输层

| 项 | 取值 |
|---|---|
| 套接字类型 | `SOCK_STREAM`（TCP 语义，可靠有序） |
| 地址 | `/run/pistelink/ai.sock`（AF_UNIX） |
| 角色 | **AI 应用 = 服务端**（bind + listen）；**后端 = 客户端**（connect） |
| 并发连接 | 服务端最多接受 1 路连接；新连接到来时断开旧连接 |
| 字符编码 | UTF-8 |
| 文件权限 | socket 文件由 AI 创建，权限 `0600`，属主 `nvidia:nvidia` |
| 目录 | `/run/pistelink/` 由部署脚本预先创建，`0700 nvidia:nvidia` |

**AI 应用需要做的**：启动后创建 `/run/pistelink/ai.sock`，`bind()` + `listen()`，等待后端连接。

---

## 3. 数据帧

**NDJSON**（Newline Delimited JSON）：每条消息一个 JSON 对象，单行，`\n`（0x0A）结尾。

```
{...json...}\n{...json...}\n
```

- 单条最大 **64 KiB**
- JSON 内部不得包含字面换行符，换行用 `\n` 转义
- 任一方解析失败 → 丢弃该行并记录日志，**不关闭连接**

---

## 4. 统一信封

所有消息共享同一外层结构：

```json
{
  "v": 1,
  "type": "<event_type>",
  "id": <uint64>,
  "ts": <int64>,
  "ts_mono_ns": <int64, optional>,
  "match_id": "<string, optional>",
  "payload": { ... }
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `v` | int | 协议版本，当前固定为 `1` |
| `type` | string | 事件类型（见 §6、§7、§8） |
| `id` | uint64 | 发送方单调递增序号；每次重连后从 0 开始 |
| `ts` | int64 | 发送时刻，Unix epoch 毫秒 |
| `ts_mono_ns` | int64 | **可选**。发送时刻的 monotonic 纳秒（`time.monotonic_ns()`），用于本机进程间时间差分析，不受墙钟校时影响 |
| `match_id` | string | 本场比赛标识（= 后端收到 MCU 0x50 时刻的毫秒墙钟转字符串）；比赛未开始时省略此字段 |
| `payload` | object | 与 `type` 对应的业务数据；空对象时可省略整个 key |

**向前兼容规则**：未识别的 `type` → 记录日志并忽略该条；未识别的字段 → 直接忽略。加新 type 或新可选 payload 字段**不视为破坏性变更**。

> 时间精度声明：受程序运行、系统调度、串口传输等耗时影响，且当前 MCU 方案**无时间概念**，所有时间戳由后端在**收到 MCU 数据时**生成，再叠加客户设定的静态误差（见 §7.4.2）。这是"相对高精度"，无法保证绝对精确。`ts_mono_ns` 仅改善本机时间基准，不能替代真实事件时间。

---

## 5. 握手

连接建立后**必须**先完成握手，业务报文（`match_pre_start`、`signal` 等）只能在握手成功后发送。

### 5.1 流程

```
后端 connect ──────────────────────────────────────▶ AI (accept)
后端 ── hello ──▶ AI
后端 ◀── hello_ack ── AI
           （握手完成，进入可工作状态）
```

### 5.2 hello（后端 → AI）

```json
{
  "v": 1,
  "type": "hello",
  "id": 0,
  "ts": 1778397800000,
  "payload": {
    "role": "backend",
    "app": "pistelink",
    "version": "0.1.0",
    "protocol_v": 1
  }
}
```

### 5.3 hello_ack（AI → 后端）

```json
{
  "v": 1,
  "type": "hello_ack",
  "id": 0,
  "ts": 1778397800010,
  "payload": {
    "role": "ai",
    "app": "<你的应用名>",
    "version": "<你的版本号>",
    "protocol_v": 1
  }
}
```

### 5.4 握手规则

| 情况 | 后端行为 |
|---|---|
| `protocol_v` 不一致 | 关闭连接，**不再重试**，错误码 `E_AI_PROTO_VER` |
| 首包收到非 `hello_ack` | 关闭连接，按退避策略重连 |
| 6 秒内握手未完成 | 视为超时断开，按退避策略重连 |

---

## 6. 心跳

- 任一方若 **2 秒**内未发送任何报文 → 主动发 `ping`
- 接收方**立即**回 `pong`
- 任一方连续 **6 秒**未收到任何报文（不限 ping/pong） → 视为断开，关闭连接，后端进入重连

### ping

```json
{"v": 1, "type": "ping", "id": 42, "ts": 1778397805000}
```

### pong

```json
{"v": 1, "type": "pong", "id": 15, "ts": 1778397805010, "payload": {"ref_id": 42}}
```

`ref_id` = 所回复的 ping 的 `id`。

---

## 7. 事件：后端 → AI

### 7.1 match_pre_start（开始新比赛的预通知）

**时机**：后端收到 MCU 的 `0x50`（开始比赛）后**立即**发送。

```json
{
  "v": 1,
  "type": "match_pre_start",
  "id": 1,
  "ts": 1778397800100,
  "match_id": "1778397800089",
  "payload": {
    "weapon": 2,
    "sensor": 0,
    "side_map": { "A": "left", "B": "right" }
  }
}
```

| 字段 | 说明 |
|---|---|
| `match_id` | **本场比赛的唯一 ID**（= 收到 0x50 时刻的毫秒墙钟），AI 必须用它决定视频文件的落地路径 |
| `weapon` | 剑种：`1`=花剑，`2`=重剑，`3`=佩剑 |
| `sensor` | 传感器接线状态：`0`=正常，`1`=甲方(A)接线失败，`2`=乙方(B)接线失败 |
| `side_map` | **A/B 与视频左右的固定映射**，恒为 `{"A":"left","B":"right"}`。AI 据此把内部 left/right 与协议 A/B 对齐 |

**AI 收到后应做**：
1. 初始化摄像头
2. 开始录像，视频写入 `<storage_root>/matches/<match_id>/` 目录下
3. 完成后发 `camera_ready`（成功）或 `camera_error`（失败）

> 在 `match_pre_start` 之前，AI **不应**主动发送 `camera_ready`；如发送，后端会忽略。

> 剑种裁判边界（建议，由 AI 实现）：重剑（weapon=2）通常直接采用电信号结果，不做 right-of-way 分析；佩剑（weapon=3）走 right-of-way；花剑（weapon=1）需区分有效/无效部位。后端只透传 weapon，不约束 AI 的具体裁判算法。

### 7.2 match_begin_ack（确认比赛正式开始）

**时机**：后端收到 `camera_ready` 并记录 `beginTimeStamp` 之后。

```json
{
  "v": 1,
  "type": "match_begin_ack",
  "id": 2,
  "ts": 1778397803080,
  "match_id": "1778397800089",
  "payload": {
    "begin_ts": 1778397803077
  }
}
```

| 字段 | 说明 |
|---|---|
| `begin_ts` | 后端收到 `camera_ready` 时刻的毫秒墙钟，作为本场事件时间线的零点 |

> `begin_ts` 是后端收到 `camera_ready` 的时间，**不等于**视频第 0 帧时间。需要第 0 帧/录像起始的真实时间，请用 `camera_ready` 回带的录像元数据（§8.1）。

### 7.3 voice_end（开始语音播放完成）

**时机**：`start.mp3` 播放结束后。

```json
{
  "v": 1,
  "type": "voice_end",
  "id": 3,
  "ts": 1778397806720,
  "match_id": "1778397800089",
  "payload": {
    "voice_end_ts": 1778397806720
  }
}
```

### 7.4 signal（电信号事件）

**时机**：后端收到 MCU 的击打帧（ATxYZ）或 `0x52`（亮灯回合结束）后，经排重和时间戳修正后转发。

#### 7.4.1 击打信号（source = "hit"）

```json
{
  "v": 1,
  "type": "signal",
  "id": 4,
  "ts": 1778397808030,
  "ts_mono_ns": 123456818000000,
  "match_id": "1778397800089",
  "payload": {
    "fight": 8,
    "source": "hit",
    "signal_ts": 1778397808086,
    "signal_mono_ns": 123456874000000,
    "terminal": false
  }
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `fight` | int | 击打信号类型（见取值表） |
| `source` | string | `"hit"` = 来自击打帧 |
| `signal_ts` | int64 | **已修正**的电信号时间戳（`recv_ts + 静态误差`），用于对齐视频帧时间线 |
| `signal_mono_ns` | int64 | **可选**。已修正电信号的 monotonic 纳秒 |
| `terminal` | bool | 恒为 `false`（hit 不是回合终止事件） |

**fight 取值表**（描述发生的动作；数值与 json.txt 的 `fight` 一致）：

| 值 | 含义 |
|---|---|
| 1 | 击中对方 A 护手盘 |
| 2 | 击中对方 B 护手盘 |
| 3 | 剑身接触（剑刃互击） |
| 4 | A 触地（仅重剑） |
| 5 | B 触地（仅重剑） |
| 6 | A 无效击中（仅花剑） |
| 7 | B 无效击中（仅花剑） |
| 8 | **A 方得分** |
| 9 | **B 方得分** |

> **重要订正**：MCU 击打帧**不存在 `fight=10`**。平局表现为在一定时间窗内**分别收到 `8` 和 `9` 两帧**，由 AI（及后端电信号汇总）据此判定平局。v1.0 fight 表中的 `10` 已删除。最终结果汇总码 `result_code=10`（平局）见 §8.3，与 fight 帧无关。

#### 7.4.2 时间戳来源与修正

- MCU **无时间概念**，不提供事件发生时间。`signal_ts` 由后端在**收到该帧时**生成时间戳，再叠加**客户设定的静态误差**：`signal_ts = recv_ts + offset`。
- 该 `offset`（即 `video_sync_offset_ms`）是客户提供的固定校准值，后端只提供 UI 设置接口（范围 −500 ~ +500 ms）。**协议不单独下发 offset**：AI 收到的 `signal_ts` 已是修正后的最终值。
- AI 侧应使用自身保存的**真实帧时间戳**（`frame_timestamps.jsonl`，§8.1）把 `signal_ts` 映射到视频帧号，不要用 `begin_ts + 名义FPS` 估算。

#### 7.4.3 回合结束信号（source = "light"）

**时机**：一个回合结束（**比赛结束，无论有效或无效**）时由后端转发。AI 据此停录并触发最终裁判。

```json
{
  "v": 1,
  "type": "signal",
  "id": 8,
  "ts": 1778397811865,
  "match_id": "1778397800089",
  "payload": {
    "source": "light",
    "signal_ts": 1778397811808,
    "terminal": true,
    "final_lights": { "A": true, "B": false }
  }
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `source` | string | 恒为 `"light"`（回合结束/亮灯） |
| `signal_ts` | int64 | 已修正的接收时间戳 |
| `terminal` | bool | 恒为 `true`：本回合（本场）结束，AI 应停止录像并触发最终裁判 |
| `final_lights` | object | 双方最终亮灯：`true` = 该方有效得分亮灯，`false` = 未亮 |

**权威性约定**：
- `source:"light"`（`terminal:true`）是**本回合最终灯态**，是停录与最终裁判的唯一终止信号。
- 若 hit 信号与最终灯态在语义上冲突，**以本信号（`final_lights`）为准**。
- 后端据 `final_lights` 推导电信号层面结果用于 json.txt 临时结果（§13）；AI 的视频裁判结果以 `match_result` 为最终结论。

> **lockout**：当前 MCU 无时间概念，**不提供 lockout 起始时间与时长**。后端无法给出 lockout，相关逻辑由 AI 自行处理。

### 7.5 match_cancel（比赛取消/重置）

**时机**：后端收到 MCU 的 `0x51`（比赛被重置）。

```json
{
  "v": 1,
  "type": "match_cancel",
  "id": 10,
  "ts": 1778397815000,
  "match_id": "1778397800089",
  "payload": {}
}
```

> 0x51 = 比赛被重置，**无需保存电信号文件**。后端丢弃本场缓冲并删除目录，同时通知 AI 处理。

**AI 收到后应做**：停止当前录像，丢弃/清理本场视频文件，回到等待 `match_pre_start` 的状态。

### 7.6 shutdown（后端退出）

**时机**：后端正常退出前的最后一条消息。

```json
{"v": 1, "type": "shutdown", "id": 99, "ts": 1778397999999, "payload": {}}
```

### 7.7 事件汇总

| type | 触发条件 | 频率 |
|---|---|---|
| `hello` | 连接建立 | 每次连接 1 次 |
| `ping` / `pong` | 2s 无报文 | 空闲时周期性 |
| `match_pre_start` | 比赛开始 | 每场比赛 1 次 |
| `match_begin_ack` | 收到 camera_ready | 每场比赛 1 次 |
| `voice_end` | start.mp3 播完 | 每场比赛 1 次 |
| `signal` (hit) | 击打动作 | 比赛中高频 |
| `signal` (light) | 回合结束 | 每回合/每场 1 次，`terminal:true` |
| `match_cancel` | 比赛重置 | 异常/重置时 |
| `shutdown` | 后端进程退出 | 每次进程退出 1 次 |

---

## 8. 事件：AI → 后端

### 8.1 camera_ready（摄像头就绪）

**时机**：收到 `match_pre_start` 后，摄像头初始化成功、录像已开始。

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
    "first_frame_ts": 1778397803041,
    "first_frame_index": 0,
    "fps_nominal": 30.0,
    "width": 1280,
    "height": 720,
    "frame_timestamps_path": "/var/lib/pistelink/matches/1778397800089/ai/frame_timestamps.jsonl"
  }
}
```

| 字段 | 说明 |
|---|---|
| payload 全部字段 | **均为可选**。后端只记录到日志，不参与主流程；缺省时后端仍按 v1.0 流程工作 |

后端收到后：记录 `beginTimeStamp = now()`，回复 `match_begin_ack`，然后播放 `start.mp3`。

> 推荐 AI 保存每帧采集时间戳到 `matches/<match_id>/ai/frame_timestamps.jsonl`（每行 `{"frame":0,"ts":...,"mono_ns":...}`），并用它做 `signal_ts → frame` 映射。该文件属于中间产物，存放约束见 §12.1。

### 8.2 camera_error（摄像头初始化失败）

**时机**：收到 `match_pre_start` 后，摄像头初始化/录像启动失败。

```json
{
  "v": 1,
  "type": "camera_error",
  "id": 1,
  "ts": 1778397804000,
  "match_id": "1778397800089",
  "payload": {
    "code": "CAM_INIT_FAIL",
    "reason": "无法打开 /dev/video0"
  }
}
```

**后端收到后**：记录错误日志（保留 `code`/`reason`），丢弃本场缓冲数据并删除目录，回到空闲态。

### 8.3 match_result（比赛结果）

**时机**：AI 判定本场比赛结束（通常在收到 `terminal:true` 的 light 信号、完成视频裁判后）。

```json
{
  "v": 1,
  "type": "match_result",
  "id": 2,
  "ts": 1778397812500,
  "match_id": "1778397800089",
  "payload": {
    "winner": "A",
    "result_code": 8,
    "video_path": "/var/lib/pistelink/matches/1778397800089/segment_1778397800.mp4",
    "analysis_result_path": "/var/lib/pistelink/matches/1778397800089/ai/analysis_result.json",
    "signal_frame_mapping_path": "/var/lib/pistelink/matches/1778397800089/ai/signal_frame_mapping.json",
    "processing_mode": "live_streaming"
  }
}
```

| 字段 | 说明 |
|---|---|
| `winner` | `"A"` = A方胜，`"B"` = B方胜，`"tie"` = 平局 |
| `result_code` | 最终结果代码（见下表） |
| `video_path` | 视频文件**绝对路径**，**必须**位于 `<storage_root>/matches/<match_id>/` 下，且为 **MP4** |
| `analysis_result_path` / `signal_frame_mapping_path` / `processing_mode` | **可选** debug 元数据，后端透传/忽略，不影响主流程 |

**后端收到后**（json.txt 先写后改，详见 §13）：
1. 验证 `video_path` 在本场比赛目录下；若不在则记告警 `E_VIDEO_OUT_OF_DIR`
2. 按 `winner` 播放 `left.mp3` / `right.mp3` / `tie.mp3`，播完后 finalize
3. 用 `result_code` **回填/修正**已写入的 json.txt 结果

#### result_code 对照表

| 值 | 含义 |
|---|---|
| 0 | **未判定**：双方无有效灯态待 AI 裁决，或 AI 超时未返回（见 §13） |
| 8 | A方得分/获胜 |
| 9 | B方得分/获胜 |
| 10 | 双方得分 / 平局 |
| 其他 | 保留，出现新值时两边协商同步 |

> 视频格式约定为 **MP4**。若 AI 内部先写 AVI 再转码，对后端透明，但 `match_result.video_path` 必须指向最终 MP4。

### 8.4 事件汇总

| type | 触发条件 | 频率 |
|---|---|---|
| `hello_ack` | 收到 hello | 每次连接 1 次 |
| `ping` / `pong` | 2s 无报文 | 空闲时周期性 |
| `camera_ready` | 摄像头初始化成功 | 每场比赛 1 次 |
| `camera_error` | 摄像头初始化失败 | 异常时 |
| `match_result` | 比赛判定结束 | 每场比赛 1 次 |

---

## 9. 端到端时序（成功路径）

```
时间线（毫秒墙钟）
─────────────────────────────────────────────────────────────────────────────

t=1778397800089  后端收到 MCU 0x50
                 → match_id = "1778397800089"
                 → 创建目录 /var/lib/pistelink/matches/1778397800089/

t=1778397800100  后端 → AI:  match_pre_start  ──────▶  AI 收到，开始初始化摄像头
                 {match_id, weapon:2, sensor:0, side_map:{A:left,B:right}}

t=1778397803050  AI → 后端:  camera_ready  ◀──────  AI 就绪
                 {video_path, first_frame_ts, fps_nominal, frame_timestamps_path, ...}

t=1778397803077  后端记录 beginTimeStamp = 1778397803077

t=1778397803080  后端 → AI:  match_begin_ack  ──────▶  {begin_ts:1778397803077}

                 （后端播放 start.mp3）

t=1778397806720  start.mp3 播放结束
t=1778397806720  后端 → AI:  voice_end  ──────▶  {voice_end_ts:1778397806720}

                 ═══════ 比赛进行中 ═══════

t=1778397808086  后端收到 AT3YZ（剑身接触），排重 + 修正后：
                 后端 → AI:  signal  ──────▶  {fight:3, source:"hit", signal_ts, terminal:false}

t=1778397811808  后端收到 AT8YZ（A得分），排重 + 修正后：
                 后端 → AI:  signal  ──────▶  {fight:8, source:"hit", signal_ts, terminal:false}

                 ═══════ 回合结束 ═══════

t=1778397811865  回合结束（A 有效得分亮灯，B 未亮）
                 后端 → AI:  signal  ──────▶  {source:"light", terminal:true,
                                               final_lights:{A:true,B:false}}
                 后端：立即写 json.txt（电信号 + 由最终灯态推导的临时 result），
                       启动等待 match_result 的 8s 超时计时

                 ═══════ AI 判定结束 ═══════

t=1778397812500  AI → 后端:  match_result  ◀──────
                 {winner:"A", result_code:8, video_path:".../segment_1778397800.mp4"}
                 后端：回填 json.txt 的 result，播放 left.mp3，播完 finalize

                 ═══════  一场结束，回到空闲态 ═══════
```

---

## 10. 异常路径

### 10.1 摄像头初始化失败

```
后端 → AI: match_pre_start ──▶
AI → 后端: camera_error ◀── {code:"CAM_INIT_FAIL", reason:"..."}

后端：1. 记录 E_CAMERA_INIT 错误日志（保留 code/reason）
      2. 丢弃本场 signals[] 缓冲，删除比赛目录
      3. 前端弹告警，回到空闲态
```

### 10.2 比赛中途收到取消（0x51）

```
后端 → AI: match_cancel ──▶ {match_id:"..."}
AI：停止录像，丢弃/删除本场视频文件
后端：丢弃 signals[] 缓冲，删除比赛目录（不写 json.txt），推 match_state，回到空闲态
```

### 10.3 比赛中途收到新的 0x50（抢占）

```
等同于先 0x51 取消旧场，再 0x50 开新场：
后端 → AI: match_cancel（旧 match_id）→ 丢弃旧场 → 新 match_id → 创建新目录 → match_pre_start（新）
```

### 10.4 AI 结果超时（0x52 后未收到 match_result）

```
0x52 到达后，后端已写入 json.txt（含电信号 + 由 0x52 推导的临时 result）。
若 8s（可配）内未收到 match_result：
  后端以 result_code=0（AI 超时未判定）finalize，list[] 电信号完整保留，数据不丢。
随后到达的 match_result 若仍属本场，按 §13 规则回填。
```

### 10.5 AI socket 断开

- 后端检测到断开 → WS 推送 `ai_status=disconnected`
- 按指数退避重连（1s → 2s → 4s → ... → 30s 上限）
- **断开期间不缓冲事件**：AI 不在线意味着视频也无法采集，当前场次视为故障
- 重连成功后重新握手，等下一次 `0x50` 开新场

### 10.6 握手版本不匹配

```
后端 → AI: hello {protocol_v: 1}
AI → 后端: hello_ack {protocol_v: 2}   // 不一致！
后端：关闭连接，不再重试，WS 推送 E_AI_PROTO_VER
```

---

## 11. AI 应用实现清单

### 11.1 启动时

- [ ] 确保 `/run/pistelink/` 目录存在
- [ ] `socket()` → `bind("/run/pistelink/ai.sock")` → `listen()`
- [ ] 设置 socket 文件权限 `0600`，属主 `nvidia:nvidia`
- [ ] 等待后端 `connect()`

### 11.2 连接建立后

- [ ] 收到 `hello` → 校验 `protocol_v == 1` → 回复 `hello_ack`
- [ ] 实现心跳：2s 无报文发 ping，6s 无报文断连

### 11.3 比赛流程

- [ ] 收到 `match_pre_start` → 按 `match_id` 创建视频输出目录 → 初始化摄像头 → 发 `camera_ready`（建议回带录像元数据）或 `camera_error`
- [ ] 收到 `match_begin_ack` → 记录 `begin_ts`
- [ ] 收到 `voice_end` → 记录 `voice_end_ts`
- [ ] 收到 `signal`（hit）→ 用于得分分析；用真实 frame timestamps 映射 `signal_ts`
- [ ] 收到 `signal`（light, `terminal:true`）→ 停止录像、触发最终裁判，参考 `final_lights`
- [ ] 收到 `match_cancel` → 停止录像并清理视频文件
- [ ] 判定结束 → 发 `match_result`（video_path 必须在 match_id 目录下、MP4）

### 11.4 约束

- [ ] 视频文件**必须**写入 `<storage_root>/matches/<match_id>/` 目录，格式 **MP4**
- [ ] 中间产物写入 `matches/<match_id>/ai/` 子目录（§12.1），不要污染上传文件
- [ ] `match_result` / `camera_ready` 必须在收到 `match_pre_start` 之后发送，否则被后端忽略
- [ ] socket 文件权限必须是 `0600 nvidia:nvidia`

---

## 12. 双方协作约定

### 12.1 共享目录

```
/var/lib/pistelink/
  matches/
    <match_id>/                  # match_id = 0x50 接收毫秒时间戳
      segment_*.mp4              # AI 写入（路径通过 match_result.video_path 告知）— 上传
      json.txt                  # 后端写入 — 上传
      ai/                        # AI 中间产物子目录 — 不上传、不计入完成判定
        frame_timestamps.jsonl
        analysis_result.json
        signal_frame_mapping.json
```

> 比赛目录由**后端**在收到 0x50 时创建。AI 收到 `match_pre_start` 后：视频直接写进 `<match_id>/`，所有分析中间产物写进 `<match_id>/ai/` 子目录。
> 上传与清理：后端**只上传** `segment_*.mp4` 和 `json.txt`，忽略 `ai/`；完成判定（complete/uploaded/incomplete）只看 mp4 与 json.txt；清理策略（keep_all / delete_video_only / delete_all）对 `ai/` 子目录一并处理（删 video 或删 all 时连同 `ai/` 一起清）。

### 12.2 时间戳体系

| 时间值 | 含义 | 由谁记录 | 用途 |
|---|---|---|---|
| `match_id` | 后端收到 0x50 的毫秒墙钟 | 后端 | 比赛唯一标识、目录名、信封 match_id |
| `beginTimeStamp` | 后端收到 `camera_ready` 的毫秒墙钟 | 后端 | json.txt 零点 |
| `voiceEndTime` | start.mp3 播放完成的毫秒墙钟 | 后端 | json.txt |
| `signal_ts` | recv_ts + 静态误差(offset) | 后端（修正后发送给 AI） | AI 用于对齐视频帧 |
| 视频 frame_ts | 摄像头帧的时间戳 | AI（`ai/frame_timestamps.jsonl`） | AI 内部高精度帧映射 |

所有时间戳均为**同一台 Jetson 的毫秒墙钟**（`time.time_ns() // 1_000_000`），可选 `*_mono_ns` 为同机 monotonic 纳秒。双方时钟同源，不需要 NTP 或对时。

### 12.3 接口变更流程

- 加新的 `type` 或给现有 payload 加可选字段 → 不视为破坏性变更，直接加
- 改字段语义、删字段、改类型 → 需要升级 `protocol_v`（1→2），**双方协商后同步上线**

### 12.4 排障

- 后端侧日志/WS 推送使用稳定错误码（`E_AI_PROTO_VER`、`E_AI_BAD_FRAME`、`E_VIDEO_OUT_OF_DIR` 等），AI 侧对应查附录 A
- 双方各自记录发/收的每条 message 的 type + id + ts 到 debug 日志（不记 payload，避免日志膨胀）

---

## 13. json.txt 写入时机：先写后改

为保证"0x52 = 比赛结束，无论有效无效都存电信号文件"，并兼顾 AI 视频裁判结果，json.txt 采用**先写后改**：

1. **回合结束即写**：后端立即把本场 `signals[]`（仅 hit 信号）与由双方最终灯态推导的**临时 `result`** 写入 json.txt（原子写）。此刻电信号数据已落盘，不依赖 AI。
2. **等待 AI 结果**：启动 8s（可配）超时计时，等待 `match_result`。
3. **AI 结果到达**：用 `match_result.result_code` 回填/修正 json.txt 的 `result`；若 AI 还提供了修正后的信号时间戳，则一并更新 `list[]` 中对应 `timeStamp`。随后按 winner 播放胜方语音、播完 finalize。
4. **AI 超时**：以 `result_code=0`（AI 超时未判定）finalize；`list[]` 电信号完整保留，数据不丢。

> 临时 result 推导（按双方最终灯态）：仅 A 亮 → 8；仅 B 亮 → 9；双方都亮 → 10（平局）；双方都不亮 → 0（待 AI 判定）。

json.txt schema（供 AI 离线校验）：

```json
{
  "beginTimeStamp": 1778397803077,
  "voiceEndTime": 1778397806720,
  "list": [
    {"timeStamp": 1778397808086, "fight": 3},
    {"timeStamp": 1778397811808, "fight": 8}
  ],
  "result": 8,
  "video_sync_offset_ms": -8
}
```

| 字段 | 说明 |
|---|---|
| `beginTimeStamp` | 本场时间零点（收到 camera_ready 时刻） |
| `voiceEndTime` | start.mp3 播放完成时刻 |
| `list[]` | 仅 hit 信号（`{timeStamp(已修正), fight}`）；**0x52 不入 list** |
| `result` | 最终结果汇总码（8/9/10/0，见 §8.3） |
| `video_sync_offset_ms` | 本场使用的静态误差（留档，仅后端记录，不下发 AI） |

---

## 附录 A：错误码参考

| code | 含义 | 处置 |
|---|---|---|
| `E_AI_PROTO_VER` | 协议版本协商失败 | 后端关连接，不再重试 |
| `E_AI_BAD_FRAME` | NDJSON 解析失败 | 单条丢弃，连接保留 |
| `E_AI_UNKNOWN_TYPE` | 未识别 type | 单条忽略 |
| `E_AI_RESULT_NO_MATCH` | match_result 在 match_pre_start 前到达 | 忽略该条 |
| `E_AI_RESULT_TIMEOUT` | 0x52 后超时未收到 match_result | 以 result_code=0 finalize |
| `E_VIDEO_OUT_OF_DIR` | 视频路径不在比赛目录下 | 仍按 video_path 归档，UI 标警告 |

## 附录 B：开发与测试建议

- **AI 侧可先用 socat 模拟后端**：
  ```bash
  socat UNIX-CLIENT:/run/pistelink/ai.sock STDIO
  # 手动输入 NDJSON 行，观察 AI 响应
  ```
- **后端侧可先用 Python 脚本模拟 AI 服务端**进行集成测试（无需真实 AI）
- Windows 开发主机不涉及 UDS（AI 进程只在 Jetson 上跑），后端单元测试用依赖注入的假 socket
