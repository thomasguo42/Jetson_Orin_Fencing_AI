# PisteLink ↔ AI 应用 接口协议与协作说明

- 版本：v1.0
- 日期：2026-05-23
- 依据：`软件需求说明书.md` v0.10 附录 A

---

## 1. 概述

PisteLink 后端与 AI 应用运行在同一台 Jetson Orin Nano 上，通过 **Unix Domain Socket（UDS）** 通信。两方职责边界：

| 职责 | PisteLink 后端 | AI 应用 |
|---|---|---|
| 串口通信（MCU） | 负责 | 不涉及 |
| 摄像头采集与录像 | 不涉及 | 独占 |
| 得分分析/仲裁 | 不涉及 | 负责 |
| 音频播放 | 负责 | 不涉及 |
| 电信号文件（json.txt）写入 | 负责 | 不涉及 |
| 视频文件写入 | 不涉及 | 负责 |
| FTP 上传 | 负责 | 不涉及 |

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
  "match_id": "<string, optional>",
  "payload": { ... }
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `v` | int | 协议版本，当前固定为 `1` |
| `type` | string | 事件类型（见 §6、§7） |
| `id` | uint64 | 发送方单调递增序号；每次重连后从 0 开始 |
| `ts` | int64 | 发送时刻，Unix epoch 毫秒 |
| `match_id` | string | 本场比赛标识（= 后端收到 MCU 0x50 时刻的毫秒墙钟转字符串）；比赛未开始时省略此字段 |
| `payload` | object | 与 `type` 对应的业务数据；空对象时可省略整个 key |

**向前兼容规则**：未识别的 `type` → 记录日志并忽略该条；未识别的字段 → 直接忽略。加新 type 或新可选 payload 字段**不视为破坏性变更**。

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
    "sensor": 0
  }
}
```

| 字段 | 说明 |
|---|---|
| `match_id` | **本场比赛的唯一 ID**（= 收到 0x50 时刻的毫秒墙钟），AI 必须用它决定视频文件的落地路径 |
| `weapon` | 剑种：`1`=花剑，`2`=重剑，`3`=佩剑 |
| `sensor` | 传感器状态：`0`=正常，`1`=故障A，`2`=故障B |

**AI 收到后应做**：
1. 初始化摄像头
2. 开始录像，视频写入 `<storage_root>/matches/<match_id>/` 目录下
3. 完成后发 `camera_ready`（成功）或 `camera_error`（失败）

> 在 `match_pre_start` 之前，AI **不应**主动发送 `camera_ready`；如发送，后端会忽略。

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

**时机**：后端收到 MCU 的击打帧或 `0x52`（亮灯回合结束）后，经排重和时间戳修正后转发。

```json
{
  "v": 1,
  "type": "signal",
  "id": 4,
  "ts": 1778397808030,
  "match_id": "1778397800089",
  "payload": {
    "fight": 8,
    "source": "hit",
    "signal_ts": 1778397808086
  }
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `fight` | int | 信号类型，与 MCU 协议字节一致（见 §7.4.1） |
| `source` | string | `"hit"` = 来自击打帧；`"light"` = 来自 `0x52`（亮灯回合结束） |
| `signal_ts` | int64 | **已修正**的电信号时间戳（`recv_ts + video_sync_offset_ms`），用于对齐视频帧时间线 |

#### 7.4.1 fight 取值表

| 值 | 含义 |
|---|---|
| 1 | A方 对手护手盘击中 |
| 2 | B方 对手护手盘击中 |
| 3 | 剑身接触（剑刃互击） |
| 4 | A方 接地（仅重剑） |
| 5 | B方 接地（仅重剑） |
| 6 | A方 无效部位（仅花剑） |
| 7 | B方 无效部位（仅花剑） |
| 8 | **A方得分** |
| 9 | **B方得分** |
| 10 | **双方得分 / 互中** |

> 注意：`signal_ts` 是**修正后**的时间戳（`recv_ts + video_sync_offset_ms`），不是原始接收时刻。`video_sync_offset_ms` 由用户在 UI 中配置（范围 −500 ~ +500 ms），用于补偿电信号与视频帧之间的固定延迟差。

### 7.5 match_cancel（比赛取消/重置）

**时机**：后端收到 MCU 的 `0x51`。

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
| `match_pre_start` | MCU 0x50 | 每场比赛 1 次 |
| `match_begin_ack` | 收到 camera_ready | 每场比赛 1 次 |
| `voice_end` | start.mp3 播完 | 每场比赛 1 次 |
| `signal` | MCU 击打帧 / 0x52 | 比赛中高频 |
| `match_cancel` | MCU 0x51 | 异常时 |
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
  "match_id": "1778397800089",
  "payload": {}
}
```

**后端收到后**：记录 `beginTimeStamp = now()`，回复 `match_begin_ack`，然后播放 `start.mp3`。

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

| 字段 | 说明 |
|---|---|
| `code` | 错误码（AI 自定义），后端透传到日志 |
| `reason` | 人类可读的原因描述 |

**后端收到后**：丢弃本场缓冲数据和目录，播放"摄像头初始化失败"提示音（目前暂缺，仅记日志），回到空闲态。

### 8.3 match_result（比赛结果）

**时机**：AI 判定本场比赛结束（例如亮灯后完成得分仲裁）。

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
    "video_path": "/var/lib/pistelink/matches/1778397800089/segment_1778397800.mp4"
  }
}
```

| 字段 | 说明 |
|---|---|
| `winner` | `"A"` = A方胜，`"B"` = B方胜，`"tie"` = 平局 |
| `result_code` | 最终结果代码（见下表） |
| `video_path` | 视频文件的**绝对路径**，**必须**位于 `<storage_root>/matches/<match_id>/` 下 |

**后端收到后**：
1. 验证 `video_path` 是否在本场比赛目录下；若不在则记告警 `E_VIDEO_OUT_OF_DIR`
2. 按 `winner` 选择播放 `left.mp3` / `right.mp3` / `tie.mp3`，播完再串行播 `end.mp3`
3. 将 `signals[]` 缓冲 + `result_code` 序列化为 `json.txt` 写入比赛目录

#### result_code 对照表

| 值 | 含义 |
|---|---|
| 8 | A方得分/获胜 |
| 9 | B方得分/获胜 |
| 10 | 双方得分 / 平局 |
| 其他 | 保留，出现新值时两边协商同步 |

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
                 {match_id:"1778397800089", weapon:2, sensor:0}

                 （AI 初始化摄像头，开始录像写入 matches/1778397800089/）

t=1778397803050  AI → 后端:  camera_ready  ◀──────  AI 就绪
                 {match_id:"1778397800089"}

t=1778397803077  后端记录 beginTimeStamp = 1778397803077

t=1778397803080  后端 → AI:  match_begin_ack  ──────▶
                 {match_id:"1778397800089", begin_ts:1778397803077}

                 （后端播放 start.mp3）

t=1778397806720  start.mp3 播放结束

t=1778397806720  后端 → AI:  voice_end  ──────▶
                 {match_id:"1778397800089", voice_end_ts:1778397806720}

                 ═══════ 比赛进行中 ═══════

t=1778397808086  后端收到 AT3YZ（剑身接触），经排重 + timestamp 修正后：

t=1778397808030  后端 → AI:  signal  ──────▶
                 {fight:3, source:"hit", signal_ts:1778397808086}

t=1778397811865  后端收到 AT8YZ（A得分），经排重 + timestamp 修正后：

t=1778397811808  后端 → AI:  signal  ──────▶
                 {fight:8, source:"hit", signal_ts:1778397811865}

                 ... 更多 hit/light 信号 ...

                 ═══════ AI 判定结束 ═══════

t=1778397812500  AI → 后端:  match_result  ◀──────
                 {winner:"A", result_code:8, video_path:"/var/lib/pistelink/matches/1778397800089/segment_1778397800.mp4"}

                 （后端播放 left.mp3 → end.mp3，写入 json.txt）

                 ═══════  一场结束，回到空闲态 ═══════
```

---

## 10. 异常路径

### 10.1 摄像头初始化失败

```
后端 → AI: match_pre_start ──▶
AI → 后端: camera_error ◀── {code:"CAM_INIT_FAIL", reason:"..."}

后端行为：
  1. 丢弃本场 signals[] 缓冲
  2. 删除本场比赛目录
  3. 记录 E_CAMERA_INIT 日志，前端弹告警
  4. 回到空闲态，等待下一次 0x50
```

### 10.2 比赛中途收到取消（0x51）

```
后端 → AI: match_cancel ──▶ {match_id:"..."}

AI 行为：
  - 停止录像
  - 丢弃/删除本场视频文件

后端行为：
  - 丢弃 signals[] 缓冲
  - 删除比赛目录
  - 推 match_state 到前端
  - 回到空闲态
```

### 10.3 比赛中途收到新的 0x50（抢占）

```
处理逻辑：等同于先 0x51 取消旧场，再 0x50 开新场。

后端：
  1. → AI: match_cancel（旧 match_id）
  2. 丢弃旧场缓冲和目录
  3. 分配新 match_id = 当前毫秒墙钟
  4. 创建新比赛目录
  5. → AI: match_pre_start（新 match_id）
```

### 10.4 AI socket 断开

- 后端检测到断开 → WS 推送 `ai_status=disconnected`
- 按指数退避重连（1s → 2s → 4s → ... → 30s 上限）
- **断开期间不缓冲事件**：AI 不在线意味着视频也无法采集，当前场次视为故障
- 重连成功后重新握手，等下一次 `0x50` 开新场

### 10.5 握手版本不匹配

```
后端 → AI: hello {protocol_v: 1}
AI → 后端: hello_ack {protocol_v: 2}   // 不一致！

后端：关闭连接，不再重试，WS 推送 E_AI_PROTO_VER 错误
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

- [ ] 收到 `match_pre_start` → 按 `match_id` 创建视频输出目录 → 初始化摄像头 → 发 `camera_ready` 或 `camera_error`
- [ ] 收到 `match_begin_ack` → 记录 `begin_ts`
- [ ] 收到 `voice_end` → 记录 `voice_end_ts`
- [ ] 收到 `signal` → 用于得分分析（`signal_ts` 已对齐视频帧时间线）
- [ ] 收到 `match_cancel` → 停止录像并清理视频文件
- [ ] 判定结束 → 发 `match_result`（video_path 必须在 match_id 对应目录下）

### 11.4 约束

- [ ] 视频文件**必须**写入 `<storage_root>/matches/<match_id>/` 目录（与后端 json.txt 同目录），`match_id` 取自 `match_pre_start` 信封
- [ ] `match_result` 必须在收到 `match_pre_start` 之后发送，否则被后端忽略
- [ ] `camera_ready` 必须在收到 `match_pre_start` 之后发送，否则被后端忽略
- [ ] socket 文件权限必须是 `0600 nvidia:nvidia`

---

## 12. 双方协作约定

### 12.1 共享目录

```
/var/lib/pistelink/
  matches/
    <match_id>/               # match_id = 0x50 接收毫秒时间戳
      segment_*.mp4           # AI 写入（路径通过 match_result.video_path 告知后端）
      json.txt                # 后端写入
```

> 比赛目录由**后端**在收到 0x50 时创建。AI 收到 `match_pre_start` 后直接把视频写进这个目录即可（目录已存在）。

### 12.2 时间戳体系

| 时间值 | 含义 | 由谁记录 | 用途 |
|---|---|---|---|
| `match_id` | 后端收到 0x50 的毫秒墙钟 | 后端 | 比赛唯一标识、目录名、信封 match_id |
| `beginTimeStamp` | 后端收到 `camera_ready` 的毫秒墙钟 | 后端 | json.txt 零点 |
| `voiceEndTime` | start.mp3 播放完成的毫秒墙钟 | 后端 | json.txt |
| `signal_ts` | recv_ts + video_sync_offset_ms | 后端（修正后发送给 AI） | AI 用于对齐视频帧 |
| 视频 frame_ts | 摄像头帧的时间戳 | AI | AI 内部使用 |

所有时间戳均为**同一台 Jetson 的毫秒墙钟**（`time.time_ns() // 1_000_000`），双方时钟同源，不需要 NTP 或对时。

### 12.3 接口变更流程

- 加新的 `type` 或给现有 payload 加可选字段 → 不视为破坏性变更，直接加
- 改字段语义、删字段、改类型 → 需要升级 `protocol_v`（1→2），**双方协商后同步上线**

### 12.4 排障

- 后端侧日志/WS 推送使用稳定错误码（`E_AI_PROTO_VER`、`E_AI_BAD_FRAME`、`E_VIDEO_OUT_OF_DIR` 等），AI 侧对应查附录 A.8
- 双方各自记录发/收的每条 message 的 type + id + ts 到 debug 日志（不记 payload，避免日志膨胀）

---

## 附录 A：错误码参考

| code | 含义 | 处置 |
|---|---|---|
| `E_AI_PROTO_VER` | 协议版本协商失败 | 后端关连接，不再重试 |
| `E_AI_BAD_FRAME` | NDJSON 解析失败 | 单条丢弃，连接保留 |
| `E_AI_UNKNOWN_TYPE` | 未识别 type | 单条忽略 |
| `E_AI_RESULT_NO_MATCH` | match_result 在 match_pre_start 前到达 | 忽略该条 |
| `E_VIDEO_OUT_OF_DIR` | 视频路径不在比赛目录下 | 仍按 video_path 归档，UI 标警告 |

## 附录 B：开发与测试建议

- **AI 侧可先用 socat 模拟后端**：
  ```bash
  socat UNIX-CLIENT:/run/pistelink/ai.sock STDIO
  # 手动输入 NDJSON 行，观察 AI 响应
  ```
- **后端侧可先用 Python 脚本模拟 AI 服务端**进行集成测试（无需真实 AI）
- Windows 开发主机不涉及 UDS（AI 进程只在 Jetson 上跑），后端单元测试用依赖注入的假 socket
