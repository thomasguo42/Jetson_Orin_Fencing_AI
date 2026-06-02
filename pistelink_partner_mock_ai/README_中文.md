# PisteLink Mock AI 服务使用说明

本文档供合作方在本地 MCU / 后端环境中测试 PisteLink v1.1 AI 对接流程。

这个测试包是一个 **Mock AI 服务**。它只模拟 AI 应用对外暴露的 Unix socket 协议和文件落地行为，不包含真实摄像头采集、视觉分析、模型推理或真实裁判算法。

## 1. 测试目标

请用本 Mock 服务验证以下内容：

- 后端能连接 AI Unix socket：`/run/pistelink/ai.sock`
- 后端能完成 `hello` / `hello_ack` 握手
- MCU 的 `0x50` 能触发后端发送 `match_pre_start`
- 后端收到 `camera_ready` 后能进入正式比赛流程
- 后端能发送 `match_begin_ack`、`voice_end`
- MCU hit 信号能被后端转成 `signal source:"hit"`
- MCU `0x52` 结束信号能被后端转成 `signal source:"light", terminal:true`
- `final_lights` 同时包含 A/B 双方最终灯态
- 后端收到 `match_result` 后能回填或修正 `json.txt`
- 后端只上传 / 完成判定 `segment_*.mp4` 和 `json.txt`，忽略 `ai/` 中间产物
- 异常路径能工作：`camera_error`、AI 结果超时、`match_cancel`、socket 断开重连

本 Mock 不能验证真实 AI 视频裁判准确率，也不能验证真实摄像头帧时间戳精度。真实 AI 测试需要在我方真实 AI 设备上完成。

## 2. 文件说明

```text
pistelink_partner_mock_ai/
  mock_ai_service.py          # Mock AI Unix socket 服务端
  run_mock_ai.sh              # 启动脚本
  README_中文.md              # 本说明
  tools/
    send_mock_match.py        # 可选：不用 MCU/后端时的本地自测客户端
```

运行时每场比赛会生成：

```text
/var/lib/pistelink/matches/<match_id>/
  segment_<match_id>.mp4
  ai/
    frame_timestamps.jsonl
    signal_frame_mapping.json
    mock_ai_messages.ndjson
    mock_analysis_result.json
```

说明：

- `segment_<match_id>.mp4` 是黑色视频占位文件，用于测试后端路径、完成判定和上传流程。
- `frame_timestamps.jsonl` 是合成帧时间戳，每行格式为 `{"frame":0,"ts":...,"mono_ns":...}`。
- `signal_frame_mapping.json` 记录每个 `signal_ts` 映射到哪个合成帧。
- `mock_ai_messages.ndjson` 记录本场收发的 socket 消息，排障时请回传。
- `mock_analysis_result.json` 是 Mock 判定摘要，不代表真实 AI 裁判结果。

## 3. 环境要求

建议在与后端相同的 Jetson / Linux 设备上运行：

- Python 3.8 或更高版本
- 支持 Unix Domain Socket 的 Linux 环境
- 推荐安装 `gst-launch-1.0` 或 `ffmpeg`，用于生成可播放 MP4
- 后端进程应能访问 `/run/pistelink/ai.sock`

按协议约定，建议使用 `nvidia` 用户运行后端和本 Mock 服务。socket 文件权限为 `0600`，如果后端不是同一用户或 root，可能会连接失败。

## 4. 部署目录

把整个目录放到测试设备，例如：

```bash
/home/nvidia/pistelink_partner_mock_ai
```

进入目录：

```bash
cd /home/nvidia/pistelink_partner_mock_ai
chmod +x run_mock_ai.sh mock_ai_service.py tools/send_mock_match.py
```

## 5. 准备运行目录

按 v1.1 协议，推荐使用正式路径：

```bash
sudo mkdir -p /run/pistelink
sudo mkdir -p /var/lib/pistelink/matches
sudo chown nvidia:nvidia /run/pistelink
sudo chmod 700 /run/pistelink
sudo chown -R nvidia:nvidia /var/lib/pistelink
```

如果你们测试设备没有 `nvidia` 用户，请把上面的用户替换为实际运行后端和 Mock 服务的用户，并确保后端和 Mock 服务使用同一个用户或 root。

## 6. 正式联调启动方式

在一个终端启动 Mock AI 服务：

```bash
cd /home/nvidia/pistelink_partner_mock_ai
./run_mock_ai.sh
```

默认参数：

```text
socket: /run/pistelink/ai.sock
match_root: /var/lib/pistelink/matches
mode: final_lights
camera_ready_delay_ms: 150
result_delay_ms: 250
fps: 30
width: 1280
height: 720
```

看到类似输出即表示 AI socket 已经开始监听：

```text
[MOCK-AI] listening on /run/pistelink/ai.sock
[MOCK-AI] mode=final_lights match_root=/var/lib/pistelink/matches
```

然后启动你们的 PisteLink 后端。后端应作为客户端连接：

```text
/run/pistelink/ai.sock
```

## 7. 用 MCU / 后端做完整测试

请优先用你们真实 MCU 和真实后端测试，而不是用本目录里的 `tools/send_mock_match.py`。

推荐完整流程：

1. 启动 Mock AI 服务。
2. 启动 PisteLink 后端。
3. 确认后端已连接 AI socket，并完成 `hello` / `hello_ack`。
4. MCU 发送 `0x50` 开始比赛。
5. 后端创建 `matches/<match_id>/`，并向 AI 发送 `match_pre_start`。
6. Mock 返回 `camera_ready`。
7. 后端记录 `beginTimeStamp`，发送 `match_begin_ack`，播放 `start.mp3`。
8. `start.mp3` 播放结束后，后端发送 `voice_end`。
9. MCU 发送 hit 信号，后端转发 `signal source:"hit"`。
10. MCU 发送 `0x52` 回合结束，后端转发 `signal source:"light", terminal:true, final_lights:{...}`。
11. Mock 返回 `match_result`。
12. 后端回填或修正 `json.txt`，播放胜方语音，完成本场。

## 8. 必测用例

请至少覆盖以下用例。

| 用例 | MCU / 后端输入 | Mock 预期返回 |
|---|---|---|
| A 单灯 | terminal `final_lights.A=true, B=false` | `winner:"A", result_code:8` |
| B 单灯 | terminal `final_lights.A=false, B=true` | `winner:"B", result_code:9` |
| 双灯 | terminal `final_lights.A=true, B=true` | `winner:"tie", result_code:10` |
| 无灯/无有效灯态 | terminal `final_lights.A=false, B=false` | `winner:"tie", result_code:0` |
| 剑身接触 | hit `fight=3`，随后正常结束 | `signal_frame_mapping.json` 中保留该 hit 映射 |
| 取消比赛 | MCU `0x51`，后端发送 `match_cancel` | Mock 清理本场视频和 `ai/` 产物，不发送结果 |
| 摄像头错误 | Mock 用 `--mode camera_error` 启动 | 返回 `camera_error`，后端回到空闲 |
| AI 结果超时 | Mock 用 `--mode result_timeout` 启动 | 终止 light 后不返回 `match_result`，后端应按 8 秒超时处理 |
| socket 重连 | 关闭并重启 Mock | 后端应断线提示并按退避重连 |

## 9. 测试模式

默认模式按 `final_lights` 生成结果：

```bash
./run_mock_ai.sh --mode final_lights
```

可用模式：

| 模式 | 行为 |
|---|---|
| `final_lights` | 默认。按 terminal light 的 `final_lights` 返回结果 |
| `always_A` | 强制返回 `winner:"A", result_code:8` |
| `always_B` | 强制返回 `winner:"B", result_code:9` |
| `always_tie` | 强制返回 `winner:"tie", result_code:10` |
| `always_unjudged` | 强制返回 `winner:"tie", result_code:0` |
| `camera_error` | 收到 `match_pre_start` 后返回 `camera_error` |
| `result_timeout` | 收到 terminal light 后不返回 `match_result` |

示例：测试 camera error。

```bash
./run_mock_ai.sh --mode camera_error
```

示例：测试 AI 超时。

```bash
./run_mock_ai.sh --mode result_timeout
```

## 10. 时间戳和帧映射测试

Mock 会在 `camera_ready` 中返回：

```json
{
  "video_path": ".../segment_<match_id>.mp4",
  "recording_start_ts": 1778397803012,
  "first_frame_ts": 1778397803041,
  "first_frame_index": 0,
  "fps_nominal": 30.0,
  "width": 1280,
  "height": 720,
  "frame_timestamps_path": ".../ai/frame_timestamps.jsonl"
}
```

终止 light 后，Mock 会生成 `ai/signal_frame_mapping.json`，把每个 hit 和 terminal light 的 `signal_ts` 映射到最近的合成帧。

这只能验证字段流转、路径和映射文件格式，不能代表真实摄像头时间戳精度。

## 11. 可选：不用 MCU/后端的自测

如果你们想先确认 Mock 本身能运行，可以用自带客户端发一场模拟比赛：

```bash
./run_mock_ai.sh \
  --socket-path /tmp/pistelink_mock/ai.sock \
  --match-root /tmp/pistelink_mock/matches
```

另开终端：

```bash
python3 tools/send_mock_match.py \
  --socket-path /tmp/pistelink_mock/ai.sock \
  --winner A
```

可选 winner：

```text
A, B, double, none
```

模拟取消：

```bash
python3 tools/send_mock_match.py \
  --socket-path /tmp/pistelink_mock/ai.sock \
  --winner A \
  --cancel-stage hit
```

注意：这个工具只用于自测 Mock，不代替 MCU / 后端联调。

## 12. 成功判定

一次基础联调通过应满足：

- 后端能连接 `/run/pistelink/ai.sock`
- 后端首包发送 `hello`
- Mock 首包回复 `hello_ack`
- 后端收到 MCU `0x50` 后发送 `match_pre_start`
- Mock 只在收到 `match_pre_start` 后发送 `camera_ready`
- 后端发送 `match_begin_ack` 和 `voice_end`
- hit 信号只作为 `signal source:"hit"` 转发，且 `fight` 不出现 `10`
- 回合结束只通过 `signal source:"light", terminal:true` 表达
- terminal light payload 中有 `final_lights.A` 和 `final_lights.B`
- Mock 返回 `match_result.video_path`，且路径在 `/var/lib/pistelink/matches/<match_id>/`
- `video_path` 指向 MP4 文件
- `ai/` 子目录不参与上传和完成判定
- `json.txt` 中 `list[]` 只包含 hit 信号，`0x52` 不进入 `list[]`
- AI 超时模式下，后端能按 `result_code=0` finalize

## 13. 需要回传给我们的材料

每轮测试请回传以下材料，尤其是失败或行为不一致时：

```text
<match_root>/<match_id>/ai/mock_ai_messages.ndjson
<match_root>/<match_id>/ai/signal_frame_mapping.json
<match_root>/<match_id>/ai/frame_timestamps.jsonl
<match_root>/<match_id>/ai/mock_analysis_result.json
<match_root>/<match_id>/json.txt
后端日志中该 match_id 附近的内容
比赛目录 ls -la 输出
```

如果是连接失败，请回传：

```bash
ls -ld /run/pistelink
ls -l /run/pistelink/ai.sock
id
ps aux | grep mock_ai_service
```

如果是 MP4 或上传失败，请回传：

```bash
ls -la /var/lib/pistelink/matches/<match_id>
file /var/lib/pistelink/matches/<match_id>/segment_<match_id>.mp4
```

如果设备上有 `gst-discoverer-1.0`，也请回传：

```bash
gst-discoverer-1.0 /var/lib/pistelink/matches/<match_id>/segment_<match_id>.mp4
```

## 14. 常见问题

### 后端连不上 socket

检查：

```bash
ls -ld /run/pistelink
ls -l /run/pistelink/ai.sock
id
```

确认后端用户有权限访问 socket。协议默认 socket 权限是 `0600`，因此后端和 Mock 服务建议使用同一个用户运行。

### 后端收不到 match_result

确认是否使用了：

```bash
./run_mock_ai.sh --mode result_timeout
```

该模式会故意不返回 `match_result`，用于测试后端 8 秒超时逻辑。

### 没有生成可播放 MP4

Mock 会优先用 `gst-launch-1.0` 生成 H.264 MP4，其次尝试 `ffmpeg`。如果两者都没有，会生成一个 MP4 容器占位文件，只适合测试路径和上传流程。

建议安装其中一个：

```bash
which gst-launch-1.0
which ffmpeg
```

### json.txt 不存在

`json.txt` 是后端职责，不是 AI 职责。Mock 只写 `segment_*.mp4` 和 `ai/` 中间产物。

## 15. 安全边界

本测试包只包含公开协议模拟和合成数据，可以用于你们本地 MCU / 后端联调。它不包含真实 AI 代码、模型、摄像头采集逻辑或内部裁判逻辑。真实 AI 端到端验证需要双方用联调日志继续对齐。
