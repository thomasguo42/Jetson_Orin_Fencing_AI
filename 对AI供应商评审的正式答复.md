# 对《AI应用接口协议对接评审与时间对齐建议》的正式答复

- 答复对象：贵方《AI应用接口协议对接评审与时间对齐建议》（针对我方 `AI应用接口协议与协作说明.md` v1.0）
- 我方：PisteLink 后端
- 配套交付：协议已更新为 **v1.1**（见 `AI应用接口协议与协作说明.md`，§0 列出全部变更）
- 结论：贵方评审意见我方基本采纳；少数无法满足项已说明原因并给出替代方案

---

## 一、总体表态

贵方对 v1.0 的方向认可、对"高精度时间映射"的关注我方完全同意。我方已据评审发布 v1.1，落实了 A/B 映射、终止信号、双方最终灯态、monotonic 时间戳、camera_ready 录像元数据、中间产物目录、json.txt schema 等。

需要先澄清两条**基础事实**，它们决定了部分意见的可行性：

1. **MCU 无时间概念**。当前单片机方案不提供事件发生时间，也不提供 lockout 计时。所有时间戳由后端在**收到 MCU 数据时**生成，叠加客户设定的**静态误差**修正。因此"高精度"只能做到"相对高精度"，无法保证绝对精确。这一点此前已与客户确认。
2. **回合结束自带双方最终灯态**。后端在回合结束时能同时拿到 A、B 两方的最终灯态，因此 v1.1 直接向 AI 提供语义化的 `final_lights`（双方各一布尔），不再要求 AI 关心底层信号编码。v1.0 仅反映了一方，本版修正为双方。

---

## 二、逐条答复贵方第 12 节的 12 个问题

| # | 贵方问题 | 我方答复 |
|---|---|---|
| 1 | A/B 到视频 left/right 的固定映射 | **A=left，B=right**，固定。v1.1 在 `match_pre_start` 增加 `side_map:{"A":"left","B":"right"}` |
| 2 | 摄像头是否永远 AI 独占？后端是否完全不开摄像头 | **是**。摄像头永久由 AI 独占，后端不打开摄像头，只通知 AI 工作、收结果、按用户操作上传 |
| 3 | `source:"light"` 是否一定表示 phrase 结束 | **是**。0x52 = 比赛结束（无论有效/无效）。v1.1 给 light 信号加 `terminal:true`，它是唯一的停录与最终裁判终止信号 |
| 4 | hit 与 light 的 fight 冲突以谁为准 | **以回合结束信号（`final_lights`）为准**，它是本回合最终灯态，权威性高于中间 hit |
| 5 | `fight=10` 是否一定代表最终双灯 | **`fight=10` 不存在**。MCU 击打帧无 10；平局 = 时间窗内分别收到 8 和 9。最终结果汇总码 `result_code=10`（平局）仍保留，但与 fight 帧无关。原协议文档此处有误，v1.1 已订正 |
| 6 | `video_sync_offset_ms` 是全局/每设备/每场 | 是单一全局配置（`config.toml`，UI 可改、热加载），对本设备生效，并随每场写入 json.txt 留档。**但它是客户设定的静态误差值，我方只提供设置接口；下发给 AI 的是已修正的 `signal_ts`，不再单独下发 offset** |
| 7 | 新 MCU 能否提供事件发生时的 MCU timestamp | **不能**。当前 MCU 无时间概念。后端以收到时刻 + 静态误差作为 `signal_ts` |
| 8 | 后端能否把 `backend_recv_ts`/`offset`/`mcu_event_us` 一并发 AI | **不下发**。MCU 无事件时间；offset 是客户静态值无需回传。下发的 `signal_ts` 已是最终修正值。我方另提供可选 `ts_mono_ns`/`signal_mono_ns` 供本机时间差分析 |
| 9 | 期望 AI 输出 AVI 还是 MP4 | **MP4**。AI 内部若先写 AVI 再转码对后端透明，但 `match_result.video_path` 必须指向最终 MP4 |
| 10 | 分析中间产物能否放 match 目录 | **可以，但必须分开**。统一放 `matches/<match_id>/ai/` 子目录；后端不上传、不计入完成判定，清理策略一并覆盖 |
| 11 | json.txt schema 是否提供给 AI | **提供**。见 v1.1 §13 |
| 12 | camera_error 后删目录前是否保留错误日志 | **保留**。后端记录 `code`/`reason` 错误日志后再删目录（错误码 `E_CAMERA_INIT`） |

---

## 三、对贵方"必须/强烈建议"项的处置

### 已采纳（v1.1 已落实）

| 贵方建议 | 落实情况 |
|---|---|
| 明确 A/B ↔ left/right | `match_pre_start.side_map`，A=left/B=right |
| 明确 phrase 结束事件 | light 信号 `terminal:true`（0x52） |
| 明确最终灯态 | light 信号携带 `final_lights`（双方各一布尔） |
| 明确 fight=8/9/10 语义 | 删 fight=10；平局=8+9 时间窗；result_code=10 仅作结果汇总 |
| 增加 monotonic timestamp | 信封 `ts_mono_ns`、signal `signal_mono_ns`（均可选） |
| camera_ready 回带录像元数据 | 允许 `recording_start_ts/first_frame_ts/first_frame_index/fps_nominal/width/height/frame_timestamps_path`（可选） |
| AI 写 frame_timestamps / 中间产物 | 允许，统一放 `ai/` 子目录 |
| match_result 增加可选 debug metadata | 允许 `analysis_result_path/signal_frame_mapping_path/processing_mode` |
| 每场记录使用的 offset | json.txt 写入 `video_sync_offset_ms` |
| 按 weapon 分流裁判 | 协议透传 weapon，裁判边界建议写入 §7.1（重剑采电信号、佩剑走 ROW、花剑区分部位），具体算法由 AI 实现 |

### 无法满足 / 调整的项

| 贵方建议 | 我方处置 | 原因 |
|---|---|---|
| 明确 lockout 起始时间与时长 | **无法提供**，由 AI 自行处理 | MCU 无时间概念，不上报 lockout |
| 拆分 `mcu_event_ts` / `backend_recv_ts` / `signal_ts` | 只提供修正后的 `signal_ts` | MCU 不提供事件时间；recv_ts 与 offset 不必下发 |
| signal payload 携带 `video_sync_offset_ms` | 不携带 | 客户静态误差，后端只给修正结果，AI 无需该值 |

> 关于时间映射精度：我方理解贵方核心诉求是"signal_ts 能映射到真实视频帧"。我方提供的 `signal_ts`（已修正）+ 可选 `signal_mono_ns` + 建议 AI 保存的 `frame_timestamps.jsonl`，已足够支撑贵方用真实帧时间戳做映射（而非名义 30FPS 估算）。但受 MCU 无事件时间这一硬约束，绝对精度不由协议保证，需端到端联调验证。

---

## 四、json.txt 写入与 0x52/超时的配合（回应贵方对"何时存文件"的关注）

我方采用**先写后改**（v1.1 §13）：

1. 收到 0x52 即写 json.txt（电信号 list + 由双方电信号推导的临时 result），电信号数据立即落盘，不依赖 AI。
2. 等待 `match_result` 最长 **8 秒（可配）**。
3. 收到结果 → 回填/修正 `result`（及 AI 修正的信号时间戳，若有）→ 播胜方语音 → finalize。
4. 超时未收到 → 以 `result_code=0`（AI 超时未判定）finalize，电信号完整保留。

这保证了"0x52 = 比赛结束，无论有效无效都存电信号文件"，同时给 AI 视频裁判结果留出回填窗口。

---

## 五、下一步建议

1. 贵方确认 v1.1 协议（重点：`side_map`、light 终止信号与双方电信号、result_code=0 超时码、`ai/` 子目录约束）。
2. 双方按贵方评审第 11 节阶段 1 推进：贵方先实现 UDS 桥接服务，复用现有 recorder/analyzer，把 v1.1 `signal` 转成现有 TXT 跑通端到端。
3. 我方后端同步落地 v1.1（0x52 双字节、json.txt 先写后改、side_map、monotonic、`ai/` 子目录、8s 超时）。
4. 用历史 phrase 与真实联调验证 `signal_ts → frame` 映射精度与裁判结果一致性。
