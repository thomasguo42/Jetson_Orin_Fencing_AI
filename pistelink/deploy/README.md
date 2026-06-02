# PisteLink 部署说明（新手向）

本文教你把 PisteLink **后端**（FastAPI + 已构建好的 Vue 网页）装到一台
Jetson Orin Nano 上，开机自启、对外提供 Web 界面。**假设你是第一次接触这套系统**，
每一步都说明「做什么、敲什么命令、怎么确认成功」，尽量不跳步。

照着从上到下做即可；遇到报错，先翻文末的 [排障](#排障) 一节。

---

## 0. 先搞清楚这套系统长什么样

PisteLink 在设备上其实是**三个独立部分**协作，本文只负责中间那个「后端」：

| 部分 | 谁负责 | 跑在哪 | 怎么通信 |
|------|--------|--------|----------|
| **MCU**（击剑主机） | 硬件 | 通过 USB 串口（CH340 芯片） | 串口字节流 → 后端 |
| **后端 + Web**（本文） | 你 | systemd 服务，监听 `127.0.0.1:8080` | 串口 / Unix socket / HTTP |
| **AI 进程**（录像+判定） | AI 团队 | 宿主机独立进程 | Unix socket `/run/pistelink/ai.sock` |

> **AI 进程不归本文管**。它由 AI 团队单独部署，和后端通过两处共享：
> `/run/pistelink/`（Unix socket）和 `/var/lib/pistelink/matches/`（比赛目录）。
> 后端启动后会一直尝试连 AI socket，**连不上只会提示 AI 离线、不影响后端自身运行**——
> 没接 MCU、没起 AI 的纯 Web 测试完全没问题。

**部署形态**：**主路径**走 **裸 systemd + conda + 国内镜像**（下方「主路径」），这是当前
生产用的方式。**Docker 形态为备用方案**——构建用的 npm / pip / apt 已默认走国内源，国内网络
即可构建（基础镜像另配 registry 镜像即可），见文末「备选：Docker」。两者**二选一**，别同时跑。
对应 SRS 附录 B。

### 名词速览

- **开发机**：你那台能上网、装了 Node 的电脑（Windows/Mac/Linux 都行）。只用来**构建前端**。
- **Jetson**：目标设备（Jetson Orin Nano），运行 Linux（aarch64 架构）。下面凡是写
  「（Jetson）」的命令，都在 Jetson 上敲。
- **`<jetson>`**：占位符，换成 Jetson 的 IP 或主机名。在 Jetson 上执行 `hostname -I`
  可看到它的 IP（如 `192.168.1.50`）。
- **conda env**：一个隔离的 Python 3.11 环境，后端的依赖都装在里面，路径
  `/opt/miniforge3/envs/pistelink`。

---

## 1. 这个目录里有什么

`deploy/` 下的文件及用途：

| 文件 | 用途 | 是否必需 |
|------|------|----------|
| `systemd/pistelink.service` | systemd 服务单元，开机自启后端 | **必需** |
| `install.sh` | 一键初始化主机（建目录 / 装 udev 规则 / 配置模板） | **必需** |
| `config.example.toml` | 配置模板，装到 `/etc/pistelink/config.toml` | **必需** |
| `udev/99-pistelink-mcu.rules` | 把 CH340 固定成 `/dev/ttyUSB-mcu` | **必需** |
| `udev/99-pistelink-audio.rules` | 把 USB 声卡固定成 ALSA 名 `pistelink` | **必需** |
| `tmpfiles/pistelink.conf` | 每次开机重建 `/run/pistelink/` | **必需** |
| `systemd/pistelink-kiosk.service` | 开机全屏 Chromium 显示界面（`install.sh` 加 `PISTELINK_KIOSK=1` 装） | 可选 |
| `Dockerfile` / `compose.yml` / `systemd/pistelink-stack.service` | Docker 形态 | 备选 |

---

## 2. 前置条件（先确认，省得后面卡住）

**开发机**（只用于步骤 0）：
- 能上网，装了 Node.js（`node -v` 能输出版本号）。

**Jetson**：
- 系统是 JetPack / L4T（基于 Ubuntu）。
- 能连**国内网络**即可（pip/apt 走国内镜像，**不需要翻墙**）。
- 有一个普通用户 `nvidia`（JetPack 默认就有）。后端将以这个用户运行。
  如果你的用户名不是 `nvidia`，后面所有 `nvidia` 都换成你的，并在跑 `install.sh`
  时加 `PISTELINK_OWNER=<你的用户名>`。
- 该用户要在 `dialout`（串口）和 `audio`（声音）组里。**先检查**：
  ```bash
  groups                      # 看输出里有没有 dialout 和 audio
  ```
  没有就加上（加完要重新登录或重启才生效）：
  ```bash
  sudo usermod -aG dialout,audio nvidia
  ```
- MCU 硬件接好（没有也行，可用 `tools/` 的虚拟串口测试，见 `tools/README.md`）。

---

## 主路径：裸 systemd + conda（无 Docker）

> 一共 7 步（0~6）。每步末尾的「✅ 怎么确认」用来判断该步是否成功，**确认通过再做下一步**。

### 步骤 0（开发机）：构建前端

后端要把构建好的网页静态文件托管出去，所以先在能上网的开发机上构建：

```bash
cd frontend
npm ci          # 按 package-lock.json 精确安装依赖（比 npm install 更稳）
npm run build   # 产出到 frontend/dist/
```

**✅ 怎么确认**：`frontend/dist/` 目录存在，里面有 `index.html` 和 `assets/`。

### 步骤 1（开发机 → Jetson）：把应用拷到 Jetson

把后端代码、音频、刚构建的前端、依赖清单、部署脚本、联调工具一起传到 Jetson 的
`/opt/pistelink/`（约定的安装目录）。在**开发机的仓库根目录**执行：

```bash
rsync -aR backend sound frontend/dist requirements.txt deploy tools \
      nvidia@<jetson>:/opt/pistelink/
```

- `<jetson>` 换成 Jetson 的 IP（前面 `hostname -I` 看到的）。
- 会让你输 Jetson 上 `nvidia` 用户的密码。
- **注意是 `-aR`（多了个 `R`）**：`-R` 会把命令里写的相对路径**原样重建**到目标下，
  所以 `frontend/dist` 会落成 `/opt/pistelink/frontend/dist`——这正是后端要找的位置
  （service 里 `PISTELINK_FRONTEND_DIR=/opt/pistelink/frontend/dist`）。
  若用普通 `-a`，`frontend/dist` 会变成 `/opt/pistelink/dist`，路径对不上，
  **后端能起、但网页打不开**（healthz 正常、访问 8080 是 404）。
- `tools/` 是无硬件联调工具（虚拟串口 + 模拟 MCU/AI），用法见 `tools/README.md`；
  纯生产环境可以不传，但建议带上，方便设备上自测。

**设备完全不能联网时**：用 U 盘/内网把这些目录拷到 Jetson，**保持
`/opt/pistelink/frontend/dist` 这个层级**（别把 `dist` 直接丢到 `/opt/pistelink/` 下）。

**✅ 怎么确认**：在 Jetson 上
```bash
ls /opt/pistelink/                      # 应看到 backend  sound  frontend  deploy  tools  requirements.txt
ls /opt/pistelink/frontend/dist/index.html   # 这个文件在 = 前端路径对了
```

> ⚠️ `rsync -a` 一般会保留可执行权限，但经 U 盘/Windows 中转后，`tools/*.sh`、
> `deploy/install.sh` 的执行位可能丢失。后面用到时如提示 `Permission denied`，
> 用 `bash 脚本名` 运行，或 `chmod +x 脚本名` 补回执行位即可。

### 步骤 2（Jetson）：装 Miniforge + Python 3.11

后端跑在一个独立的 Python 3.11 环境里。先装 Miniforge（一个轻量 conda）。

**2.1 下载 aarch64 安装包**（从清华镜像，无需翻墙）：

打开 `https://mirrors.tuna.tsinghua.edu.cn/github-release/conda-forge/miniforge/LatestRelease/`，
下载里面的 **`Miniforge3-Linux-aarch64.sh`**（注意一定是 `aarch64`，Jetson 是 ARM 架构）。
能联网的话直接在 Jetson 上：
```bash
wget https://mirrors.tuna.tsinghua.edu.cn/github-release/conda-forge/miniforge/LatestRelease/Miniforge3-Linux-aarch64.sh
```

**2.2 安装到 `/opt/miniforge3`**：
```bash
bash Miniforge3-Linux-aarch64.sh -b -p /opt/miniforge3
```
- `-b` 表示静默安装（不问问题），`-p` 指定安装目录。

**2.3 创建名为 `pistelink` 的 Python 3.11 环境**：
```bash
# 注意：用 -p 指定环境的【绝对路径】，并加 sudo。不要用 -n pistelink！
sudo /opt/miniforge3/bin/conda create -y -p /opt/miniforge3/envs/pistelink python=3.11
```

> **❗为什么必须 `-p` + `sudo`（这是最容易踩的坑）**：`/opt/miniforge3` 通常是 root
> 安装的，普通用户对它下面的 `envs/` 没有写权限。这时你若用 `conda create -n pistelink`，
> conda **不会报错**，而是悄悄把环境建到你家目录 `~/.conda/envs/pistelink`。
> 可步骤 6 的 systemd 服务里写死了要用 `/opt/miniforge3/envs/pistelink/bin/python`，
> 两边对不上，服务一启动就报 `status=203/EXEC`（找不到可执行文件）并被反复重启。
> 用 `sudo … -p <绝对路径>` 强制建到 `/opt`，路径才一致。
>
> （若你确实想把环境建在家目录，也行，但要相应改 service 的 `ExecStart`，见步骤 6。）

**✅ 怎么确认**：
```bash
ls /opt/miniforge3/envs/pistelink/bin/python    # 能列出这个文件就对了
```

### 步骤 3（Jetson）：装后端依赖

把后端要用的 Python 包装进刚建的环境（走清华 PyPI 镜像加速）：

```bash
# 环境是 root 建的（步骤 2 用了 sudo），所以这里 pip 也要 sudo。
sudo /opt/miniforge3/envs/pistelink/bin/pip install \
    -r /opt/pistelink/requirements.txt \
    -i https://pypi.tuna.tsinghua.edu.cn/simple
```

> 音频播放器 mpg123（后端用它放比赛提示音）由 **`install.sh` 自动安装**（步骤 4），这里不用手动装。

> 说明：`uvicorn[standard]`（含 websockets/uvloop/httptools）在 aarch64 上都有现成
> wheel，走镜像直接装、无需编译。`requirements.txt` 里**已显式钉死 `starlette` 版本**
> ——否则全新机器装出来会拉到 starlette 1.x（它移除了后端用到的 `add_event_handler`），
> 后端启动即 `status=1` 崩溃。

**✅ 怎么确认**（自检：把后端关键依赖都 import 一遍，不报错就齐了）：
```bash
/opt/miniforge3/envs/pistelink/bin/python -c "import fastapi, uvicorn, asyncssh, serial_asyncio; print('ok')"
```
打印 `ok` 即成功。若报 `No module named 'xxx'`，说明那个包没装上，回头检查步骤 3 的 pip 输出。

### 步骤 4（Jetson）：一键初始化主机

跑 `install.sh`，它会**幂等地**（重复跑无害）建好运行目录、装 udev 规则和配置模板：

```bash
sudo /opt/pistelink/deploy/install.sh
```

它具体做了这些事：
- 建目录：`/etc/pistelink`（配置，0750）、`/var/lib/pistelink`（比赛数据，0750）、
  `/run/pistelink`（AI socket 的父目录，0700），属主都是 `nvidia`。
- **装 mpg123**（放比赛提示音的播放器）——已装则跳过；无网络只告警不中断（届时没提示音）。
- **卸载 brltty**（盲文服务）——它的 udev 规则会抢 CH340 的 `1a86:7523`，导致串口出不来。
  脚本检测到就 `apt-get remove -y --purge brltty`（没装则跳过；无网络只告警不中断）。
- 装 udev 规则 `99-pistelink-mcu.rules`，把 CH340 串口固定成 `/dev/ttyUSB-mcu`
  （这样不管插哪个 USB 口、设备名都不变），并立即生效。
- 装 udev 规则 `99-pistelink-audio.rules`，把 USB 声卡固定成 ALSA 名 `pistelink`
  （配置里用 `plughw:CARD=pistelink,DEV=0`，不依赖 card 号/USB 口）。首次装规则后若
  `cat /proc/asound/cards` 没看到 `[pistelink]`，拔插一次 USB 声卡让它重新触发。
- 装 tmpfiles 配置，保证**每次开机**自动重建 `/run/pistelink/`。
- 把配置模板装成 `/etc/pistelink/config.toml`（**仅当它还不存在时**；已存在则不动你的配置）。
- 生成 SFTP 上传密钥 `~nvidia/.ssh/id_ed25519`（已存在则不动），并打印公钥供登记（详见步骤 5）。
- **（可选）kiosk**：加 `PISTELINK_KIOSK=1` 时，装并 enable 开机全屏界面的 user 单元（见 §5）。

> 用户名不是 `nvidia` 时这样跑：`sudo PISTELINK_OWNER=<你的用户名> /opt/pistelink/deploy/install.sh`
> 想同时装 kiosk：`sudo PISTELINK_KIOSK=1 /opt/pistelink/deploy/install.sh`

**✅ 怎么确认**：命令最后会打印 `Host setup done. Next — …`，且：
```bash
ls -ld /etc/pistelink /var/lib/pistelink /run/pistelink   # 三个目录都在
ls -l /etc/pistelink/config.toml                          # 配置已生成
```

### 步骤 5（Jetson）：填配置 + 放好 SFTP 上传密钥

这一步是**新手最容易漏**的：`install.sh` 只是放了一份**模板**，你得按实际情况改它。
用编辑器打开（需要 sudo，因为属主是 nvidia、权限 0600）：

```bash
sudo nano /etc/pistelink/config.toml
```

至少确认/修改这几项：

| 配置项 | 说明 |
|--------|------|
| `[serial] device` | 默认 `/dev/ttyUSB-mcu`（接真实 MCU 时不用改）。无硬件联调时临时改成虚拟串口路径，见 `tools/README.md`。 |
| `[upload] host` / `port` / `username` | 客户的 SFTP 服务器地址、端口、用户名（模板里已预填客户给的值，核对一下）。 |
| `[upload] private_key` | SFTP 用**公钥认证**，这里指向私钥文件，默认 `/home/nvidia/.ssh/id_ed25519`。 |
| `[upload] base_path` | 上传到服务器上的哪个目录，默认 `/incoming`。 |
| `[upload] post_upload_action` | 上传后怎么清理本地：`keep_all` 全留 / `delete_video_only` 只删视频 / `delete_all` 全删。 |
| `[http] host` | 默认 `127.0.0.1`（只本机可访问）。要让局域网其它机器访问才改 `0.0.0.0`（并自行加鉴权）。 |

> `[signal] video_sync_offset_ms`、`[upload] post_upload_action` 等大多数项**之后都能在
> Web 界面里改**，热生效，不必现在纠结。

**放好 SFTP 私钥**（上传功能要用；暂时不测上传可跳过，但服务正常起不受影响）：

SFTP 用「公钥认证」——你这台设备持有**私钥**，对应的**公钥**要登记在 SFTP 服务器上。

> `install.sh`（步骤 4）已经做过这件事：若 `/home/nvidia/.ssh/id_ed25519` 不存在，它会自动生成一对，并把**公钥**打印出来。**把那串公钥交给 SFTP 服务器管理员登记到 `authorized_keys` 即可**，下面两种情况按需处理。

- 想用客户/运维给的**指定密钥**（替换掉自动生成的那把）：把**私钥**放到 `/home/nvidia/.ssh/id_ed25519`：
  ```bash
  mkdir -p /home/nvidia/.ssh && chmod 700 /home/nvidia/.ssh
  # 把私钥文件拷过去（覆盖自动生成的）后：
  chmod 600 /home/nvidia/.ssh/id_ed25519      # 权限必须 600，否则 SSH 拒绝使用
  chown nvidia:nvidia /home/nvidia/.ssh/id_ed25519
  ```
- 想重新拿到自动生成的那把**公钥**去登记：
  ```bash
  cat /home/nvidia/.ssh/id_ed25519.pub        # 把这串【公钥】交给 SFTP 服务器管理员登记
  ```

**✅ 怎么确认**：`config.toml` 改完保存（nano 里 Ctrl-O 回车保存、Ctrl-X 退出）。
SFTP 是否真能连，等服务起来后可在 Web 界面「设置」页点「测试上传」验证。

### 步骤 6（Jetson）：安装并启动服务

把服务单元拷到 systemd 目录，设为开机自启并立即启动：

```bash
sudo cp /opt/pistelink/deploy/systemd/pistelink.service /etc/systemd/system/
sudo systemctl daemon-reload                 # 让 systemd 重新读取单元文件
sudo systemctl enable --now pistelink.service # enable=开机自启，--now=顺便现在就启动
```

看实时日志（`Ctrl-C` 退出查看，**不会**停掉服务）：
```bash
journalctl -u pistelink -f
```
正常的话你会看到 `Application startup complete` 和 `Uvicorn running on http://127.0.0.1:8080`。
（日志里 `AI socket not found … waiting for AI service` 是正常的——AI 进程没起；没接 MCU 时
`serial` 相关报错也属正常。）

**验证后端活着**（另开一个终端）：
```bash
curl -fsS http://127.0.0.1:8080/healthz
```
返回类似：
```json
{"serial":"error","ai":"error","storage_free_mb":12345}
```
- `storage_free_mb` 有数字 = HTTP 服务正常。
- `serial`：接了 MCU 且串口打开成功才是 `ok`，否则 `error`（无硬件时正常）。
- `ai`：AI 进程在跑且握手成功才是 `ok`，否则 `error`（AI 没起时正常）。

> **关于 `ExecStart` 路径**：`pistelink.service` 里写死的是
> `/opt/miniforge3/envs/pistelink/bin/python`。如果你步骤 2 把环境建到了别处
> （比如家目录），就编辑 `/etc/systemd/system/pistelink.service` 把那一行改成实际路径，
> 然后 `sudo systemctl daemon-reload && sudo systemctl restart pistelink`。
> 服务崩溃由 `Restart=always`（5 秒后重试）兜底。

**✅ 怎么确认**：`systemctl status pistelink` 显示 `active (running)`，且 `curl …/healthz`
返回上面那样的 JSON。**到此后端部署完成。**

---

## 3. 打开 Web 界面

默认 `[http].host = 127.0.0.1`，**只有 Jetson 本机能访问**：

- 在 Jetson 自己的浏览器里开 `http://127.0.0.1:8080`。
- 想从**别的电脑**访问，两种办法：
  1. SSH 端口转发（推荐，安全）：在你电脑上 `ssh -L 8080:127.0.0.1:8080 nvidia@<jetson>`，
     然后本机浏览器开 `http://127.0.0.1:8080`。
  2. 改 `config.toml` 的 `[http].host = "0.0.0.0"` 让它监听所有网卡（**注意：这等于把界面
     暴露到局域网，没有自带鉴权**，仅在可信网络或加了反向代理鉴权时这么做），重启服务后
     用 `http://<jetson>:8080` 访问。

界面四个页面：实时比赛 / 存储管理 / 诊断 / 设置。

---

## 4. 日常运维速查

```bash
sudo systemctl start   pistelink      # 启动
sudo systemctl stop    pistelink      # 停止
sudo systemctl restart pistelink      # 重启（改了 config.toml 后用）
systemctl status       pistelink      # 看状态（active/failed）
journalctl -u pistelink -f            # 实时日志
journalctl -u pistelink -e            # 看最近日志（含完整报错栈），翻到底
sudo systemctl reset-failed pistelink # 清掉反复重启的失败计数
```

**更新版本**（开发机改了代码后）：在开发机重跑步骤 0、1（rsync 覆盖），然后在 Jetson 上
```bash
sudo systemctl restart pistelink
```
若依赖（`requirements.txt`）有变动，重启前先重跑步骤 3 的 pip。

---

## 5. Kiosk 全屏显示（可选）

`systemd/pistelink-kiosk.service` 是一个 **user service**：等后端 `/healthz` 通过后，
自动全屏打开 `http://127.0.0.1:8080`。浏览器命令会自动在 `chromium-browser`/`chromium`
之间挑存在的那个，无需手改；**snap 版 chromium** 也支持——单元里已把 `/snap/bin` 加进
`PATH`（systemd 服务默认不含它，否则会找不到 `/snap/bin/chromium`）。

**推荐：交付时让一键部署带上 kiosk** —— 跑 `install.sh` 时加环境变量即可：
```bash
sudo PISTELINK_KIOSK=1 /opt/pistelink/deploy/install.sh
```
它会把单元装到 `~nvidia/.config/systemd/user/`、`enable-linger`、并 enable 该 user 单元
（装的时候若 `nvidia` 还没有图形会话，enable 可能延后，脚本会提示你到桌面会话里手动
`systemctl --user enable --now pistelink-kiosk.service`）。不加这个变量则默认**跳过** kiosk。

> ⚠️ **前提：桌面要开机自动登录 `nvidia`**。user service 在该用户登录前没有图形会话，
> 所以 kiosk 只有在「开机自动登录到桌面」时才会自动出现在本地屏幕上。这一步是显示管理器
> （GDM/LightDM 等）的设置，**install.sh 不替你改**——若还没开自动登录，按你的镜像单独配。

手动安装（不走一键部署）见单元文件**头部注释**。

---

## 6. 目录与权限（SRS B.4）

`install.sh` 已按下表建好，一般不用手动管。出权限问题时对照检查：

| 宿主路径 | 用途 | 权限 |
|----------|------|------|
| `/etc/pistelink/` | 配置 | `0750` nvidia:nvidia（`config.toml` `0600`；SFTP 私钥 `~nvidia/.ssh/id_ed25519` `0600`） |
| `/var/lib/pistelink/` | 比赛数据（与 AI 共享） | `0750` nvidia:nvidia |
| `/run/pistelink/` | AI socket 的父目录（socket 由 AI 创建） | `0700` nvidia:nvidia |
| `/dev/ttyUSB-mcu` | 串口 | `0660` dialout 组 |
| `/dev/snd/*` | 音频 | `0660` audio 组 |

后端以 `nvidia` 运行（service 里 `SupplementaryGroups=dialout audio`），据此能访问串口、
音频，以及 AI 创建的 `0600 nvidia` socket。

---

## 7. 网络与安全要点

- 默认 `[http].host = 127.0.0.1`：只绑回环，局域网访问不到，Kiosk 走 localhost 正常。
  要对外提供 Web，改 `0.0.0.0` 并在反向代理层加鉴权。
  - ⚠️ `host` 只能填**一个**值。若填某张网卡的**具体 IP**（例如 Tailscale 的
    `100.x.x.x`），后端就**只**监听那一个地址——此时本机 `127.0.0.1:8080` 和局域网都连不上
    （`ss -ltnp | grep 8080` 会显示 `LISTEN <那个IP>:8080`，回环不在内）。要本机 + 远程都能
    访问就用 `0.0.0.0`（监听所有网卡）；只想走某条链路又想保留本机访问，靠防火墙放行
    `lo` + 目标网卡、拒掉其它,而不是把 `host` 写死成单个 IP。
- 上传走 **SFTP（SSH，传输加密）**，公钥认证；私钥 `~nvidia/.ssh/id_ed25519` 必须 `0600`、
  仅 nvidia 可读。
- 当前**不校验服务器主机密钥**（`known_hosts=None`，信任你已配置的服务器）；如需更严，
  可改为校验 `known_hosts`。

---

## 排障

按现象对号入座。万能第一步：`journalctl -u pistelink -e` 看最近日志和**完整报错栈**。

- **`status=203/EXEC`（Failed to locate executable … python）**：环境路径和 service 的
  `ExecStart` 对不上。最常见原因是步骤 2 用了 `-n pistelink`，环境被建到了
  `~/.conda/envs/pistelink` 而非 `/opt/miniforge3/envs/`。
  - 确认环境真实位置：`ls /opt/miniforge3/envs/pistelink/bin/python`（不存在就是这个问题）。
  - 修法 A（推荐）：按步骤 2 用 `sudo … -p /opt/miniforge3/envs/pistelink` 重建。
  - 修法 B：把 `/etc/systemd/system/pistelink.service` 的 `ExecStart` 改成实际路径
    （如 `/home/nvidia/.conda/envs/pistelink/bin/python`），再
    `sudo systemctl daemon-reload && sudo systemctl restart pistelink`。
  - 修好后 `sudo systemctl reset-failed pistelink` 清掉重启计数。

- **`status=1`（启动就崩，日志有 Python Traceback）**：多半是依赖问题。
  - `No module named 'asyncssh'`：你拷到 `/opt/pistelink` 的是 SFTP 迁移**之前**的旧副本
    （旧 `requirements.txt` 装的是 aioftp 而非 asyncssh）。重新做步骤 1（rsync 覆盖）+ 步骤 3。
  - `'FastAPI' object has no attribute 'add_event_handler'`：starlette 被装成了 1.x。
    确认 `requirements.txt` 里钉了 `starlette` 版本，重跑步骤 3 的 pip。
  - 不确定缺哪个：跑步骤 3 末尾的自检 import 命令，缺啥一眼可见。

- **串口连不上 / `/dev/ttyUSB-mcu` 不存在**：
  > **先分层定位**——CH340 不通有四种独立原因，长得像但修法完全不同，**照 dmesg
  > 特征对号入座，别一上来就重装驱动或怀疑 brltty**：
  >
  > | dmesg / 日志特征 | 在哪一层 | 修法 |
  > |---|---|---|
  > | `error -110`(读描述符超时) / `error -71`(not accepting address) / `unable to enumerate`，设备号一路往上跳 | **① 物理/电气层**，根本没枚举上 | 换线、换口、去 hub、查供电（见下方专条） |
  > | `lsusb` 有 `1a86:7523`，但**没有任何 `/dev/ttyUSB*`** | **② 驱动层**，内核缺 ch341 | 编 WCH 驱动（见下方专条） |
  > | dmesg `interface 0 claimed by usb_ch341 while 'brltty' …` 紧跟 disconnect | **③ 抢占**，brltty 抢设备 | 删/掐 brltty（见下方专条） |
  > | 节点在，但日志 `[Errno 2] No such file` / `Permission denied` / `[Errno 5] EIO` | **④ 软件/配置层** | 改 `config.toml` 路径 / 加 `dialout` 组 / 该节点已成僵尸需重插（见下方各条） |
  >
  > 速判命令：`lsusb | grep 1a86`（在不在=过没过①）→ `ls -l /dev/ttyUSB-mcu`
  > （节点在不在=过没过②③）→ 看 pistelink 日志（④）。
  - `lsusb | grep 1a86`：确认 CH340 被系统识别（1a86 是 CH340 的厂商号）。
  - `ls -l /dev/ttyUSB-mcu`：看符号链接在不在。不在就 `sudo udevadm trigger` 让 udev
    规则生效。
  - **日志报 `could not open port /dev/xxx: [Errno 2] No such file or directory`**：
    `config.toml` 的 `[serial] device` 写成了不存在的路径（实测交付板曾被手填成
    `/dev/pistelink-mcu`）。核对成实际节点 `/dev/ttyUSB-mcu`（即 `config.example.toml`
    的默认值），`grep -A2 '\[serial\]' /etc/pistelink/config.toml` 一眼可查，改完
    `sudo systemctl restart pistelink`。
  - **日志报 `Permission denied`**（节点在、路径也对）：服务用户没在 `dialout` 组，
    或刚加组还没重启服务。`sudo usermod -aG dialout nvidia` 后 `systemctl restart
    pistelink`（systemd 重启时才重读补充组）。
  - **日志报 `[Errno 5] Input/output error`**（路径对、节点也在，但打不开）：节点是
    **僵尸**——设备曾枚举成功建了节点，之后又掉线/被抢，留下一个背后没有硬件的死节点。
    `fuser -v /dev/ttyCH341USB0` 通常查不到占用。**拔掉 USB 再插一次**让 udev 重建节点
    即可；若插回去 dmesg 开始刷 `error -110`，就转去看下面的「物理层枚举失败」专条。
  - `groups | grep dialout`：确认 `nvidia` 在 `dialout` 组（步骤 2 前置条件）。
  - **没硬件想测**：用 `tools/` 的虚拟串口，见 `tools/README.md`。
  - **`lsusb` 看得到 CH340，但 `/dev/ttyUSB*` 一个都没有**（内核没带 ch341 驱动）：
    见下面「CH340 在 Jetson 上不出串口节点」一节——这是 JetPack 内核的已知情况，
    需要手动编译 WCH 驱动 + 干掉抢设备的 brltty。

- **① 物理层枚举失败（`error -110` / `unable to enumerate`，最容易误判成驱动问题）**：
  dmesg 反复刷 `device descriptor read/64, error -110`、`Device not responding to setup
  address`、`device not accepting address N, error -71`、`attempt power cycle`、
  `unable to enumerate USB device`，且 USB 设备号一路往上跳（15→16→17…）。这说明设备
  **连枚举都没过**——内核读不到设备描述符、分不下地址，ch341 驱动根本还没轮到去绑，
  **`modprobe ch341` 没有任何意义**，`lsusb | grep 1a86` 这时也看不到设备。这是线/口/
  供电的硬件问题，按嫌疑从大到小逐个换、每换一项跑一次 `sudo dmesg | tail -6`：
  1. **换 USB 线**——头号嫌疑。劣质线 / 充电线（D+ D- 没接好）最典型就报 `error -110`，
     换一根**短的、确定能传数据**的线。
  2. **换 USB 口**，最好换到另一组 root hub；**别经 USB hub / 延长线**，CH340 直插板子。
  3. **查供电**：Jetson 欠流会掉 USB；裁判器若独立供电，确认它通电、没在复位。
  4. **重插牢**，检查 CH340 板上的 USB 座有没有松/虚焊。
  > 判定恢复：`lsusb | grep 1a86` 重新出现 `1a86:7523`、dmesg 不再刷 -110/-71。
  > 同一台板子先后踩过三层——曾是 `config.toml` 路径手填错（④）、曾是 brltty 抢占（③）、
  > 也曾是一根坏线（①）。**先看 dmesg 特征再动手**，能少走很多弯路。

- **②③ CH340 在 Jetson 上不出串口节点（内核缺 ch341 驱动 + brltty 抢占）**：
  JetPack/L4T 的 Tegra 内核**默认没把 `ch341` 模块编进去**，`modprobe ch341` 报
  `Module not found`，通用 `usbserial vendor=/product=` 绑定也被拒（dmesg 里
  `unknown parameter 'vendor' ignored`）。现象是 `lsusb` 能看到 `1a86:7523`，但
  插上去**完全没有 `/dev/ttyUSB*` 节点**。在 `5.15.148-tegra` 上实测可行的修法：

  1. 确认有内核头文件（编译驱动要用）：
     ```bash
     ls -d /lib/modules/$(uname -r)/build && echo OK
     ```
     没有就装：`sudo apt-get install -y nvidia-l4t-kernel-headers`。
  2. 编译并安装芯片厂商（沁恒 WCH）官方驱动：
     ```bash
     sudo apt-get install -y git build-essential
     git clone https://github.com/WCHSoftGroup/ch341ser_linux.git
     cd ch341ser_linux/driver
     make && sudo make install        # 产出 ch341.ko（gcc 次版本 warning 无害）
     sudo modprobe ch341
     ```
  3. **干掉 brltty**（关键！）——盲文服务的 udev 规则专门抢 `1a86:7523`，即使
     `brltty.service` 显示 inactive 也会抢，dmesg 会出现
     `interface 0 claimed by usb_ch341 while 'brltty' sets config #1` 紧跟 disconnect。
     **`install.sh` 已自动卸载 brltty**，正常不用手动做；若是没跑过脚本、或脚本当时无网络
     未删成，手动补一遍：
     ```bash
     sudo apt-get remove -y --purge brltty
     sudo udevadm control --reload-rules
     ```
     然后**拔掉 CH340 再插上**，`dmesg | tail` 应看到 `ch341 ... now attached`，且
     不再有 brltty claimed/disconnect。
  4. 让驱动开机自动加载：
     ```bash
     echo ch341 | sudo tee /etc/modules-load.d/ch341.conf
     ```
  5. **注意节点名**：WCH 驱动把设备命名为 `/dev/ttyCH341USB0`（不是 `ttyUSB0`），但
     项目的 `99-pistelink-mcu.rules` 仍会建好软链接 `/dev/ttyUSB-mcu -> ttyCH341USB0`，
     所以 `config.toml` 里照旧写 `device = "/dev/ttyUSB-mcu"` 即可。

  > ⚠️ 手动编的 `.ko` **只对当前内核版本有效**，Jetson 升级内核后要重新 `make`，否则
  > 串口会突然消失。交付设备一般会固定内核、不主动升级，所以这更多是保险——但**一次配好、
  > 之后零维护**：跑 `sudo ./deploy/ch341-dkms/setup-dkms.sh`（详见该目录），用 DKMS 把驱动
  > 注册起来，以后内核升级会自动重编 `ch341.ko`，不必再回头手动 `make`。驱动源码已随仓库
  > vendor 在 `deploy/CH341SER_LINUX/`（只含源码，产物被 .gitignore 挡掉），脚本默认用它，
  > 无需联网 clone。
  > 注意：JetPack 大版本若是**整盘刷机**而非 apt 升内核，DKMS 钩子不触发，需手动重跑该脚本一次。

- **没声音**：分三层查。
  1. **播放器**：`which mpg123` 确认装了（`install.sh` 步骤 4 会装；无网络时可能没装上）；
     找不到播放器时后端**静默跳过播放**，比赛流程不受影响（只是没提示音）。
  2. **声卡路由（最常见）**：后端以 systemd 系统服务跑，**没有 PulseAudio 会话**，mpg123 的
     默认输出会落到 HDMI——板子接的是 USB 音箱时就静音。`audio.py` 会把 `[audio] device`
     强制走 ALSA（`mpg123 -o alsa -a <device>`），所以 **`config.toml` 里必须填实际 ALSA 设备**，
     用 USB 声卡就填 `device = "plughw:CARD=pistelink,DEV=0"`（`pistelink` 是下面 udev 规则
     固定的 card 名），HDMI 则填 `plughw:0,3`。改完 **`systemctl restart pistelink`** 才生效
     （Python 不热加载）。验证服务实际用的命令：`journalctl -u pistelink -b | grep "Audio play"`。
  3. **USB 声卡名漂移**：`install.sh` 装的 `99-pistelink-audio.rules` 按 USB ID 把声卡固定成
     ALSA 名 `pistelink`，不依赖 card 号/USB 口。装规则后 `cat /proc/asound/cards` 若没看到
     `[pistelink]`，**拔插一次 USB 声卡**（或 `sudo modprobe -r snd_usb_audio && sudo modprobe snd_usb_audio`）
     让规则重新触发。规则默认按 `8087:1024`（本机那只 generic USB 声卡）匹配；换了别的声卡需先
     `cat /proc/asound/cardN/usbid` 取新 ID 改规则。
  - 不接显示器/不走 service 时单测出声：
    `env -i PATH=/usr/bin mpg123 -q -o alsa -a plughw:CARD=pistelink,DEV=0 /opt/pistelink/sound/start.mp3`

- **AI 一直连不上**：这是后端去连 AI 进程的 socket `/run/pistelink/ai.sock`（由 AI 进程
  创建）。确认 `/run/pistelink/` 存在（`install.sh` 的 tmpfiles 负责）且 **AI 进程在跑**。
  AI 没起时这个离线提示是正常的，不影响后端 Web。

- **pip 慢 / 失败**：确认带了 `-i https://pypi.tuna.tsinghua.edu.cn/simple`
  （或换阿里云 `https://mirrors.aliyun.com/pypi/simple`）。

---

## 备选：Docker（备用方案，已配国内源）

> 这是**备用**部署方式。**主路径（上面的「裸 systemd + conda」）才是当前生产在用的**，
> 没特别理由就别折腾 Docker。下面这套是给"以后想用 Docker"准备的，构建用的国内源都配好了，
> 一步步照做即可。**全程在 Jetson 上操作**（除了 D-2 拷源码那步在开发机）。

**它和主路径的关系**：干的活完全一样（同一个后端、同一套 host 目录），只是把后端**装进一个容器**里
跑，而不是用 conda 直接跑。两种方式**只能二选一**，因为都占用 `127.0.0.1:8080`。

### D-0：确认 Docker 装好了

```bash
docker --version            # 有版本号 = 已装（JetPack 一般自带）
docker compose version      # 要有这个（v2 插件版）。若只有带横杠的 docker-compose 也能用，下面按 v2 写
```

- 如果 `docker` 命令不存在：
  ```bash
  sudo apt-get update && sudo apt-get install -y docker.io docker-compose-plugin
  sudo systemctl enable --now docker
  ```
- 想以后敲 docker 不用每次加 `sudo`：`sudo usermod -aG docker $USER`，然后**注销重新登录一次**。
  （下面凡是 `docker ...` 命令，如果报权限错，就在前面加 `sudo`。）

### D-1：给基础镜像配国内加速器

构建时要从 Docker Hub 拉两个基础镜像（`node:20`、`python:3.11-slim`），国内直连很慢甚至拉不动，
先配个加速器：

```bash
sudo nano /etc/docker/daemon.json
```

填入下面内容（**若这个文件本来就有内容，只把 `registry-mirrors` 这一项合并进去，别整个覆盖**）：

```json
{
  "registry-mirrors": ["https://docker.m.daocloud.io"]
}
```

`Ctrl-O` 回车保存、`Ctrl-X` 退出，然后重启 docker 让它生效：

```bash
sudo systemctl restart docker
```

**✅ 怎么确认**：拉个基础镜像试试，能下下来就说明加速器通了：

```bash
docker pull python:3.11-slim-bookworm
```

> 如果 `restart docker` 后 docker 起不来，八成是 `daemon.json` 格式写错了（JSON 对逗号/引号很敏感）。
> 用 `sudo systemctl status docker` 看报错，把文件改对再重启。

### D-2：把【完整仓库】拷到 Jetson

Docker 是**自己从源码编译前端**的，所以 Jetson 上要有**完整项目源码**，不是主路径步骤 1 那份
只含 `frontend/dist` 的精简包。在**开发机**上把整个仓库拷过去（含 `frontend/` 源码、`backend/`、
`sound/`、`requirements.txt`、`deploy/`）：

```bash
# 在【开发机】上执行，<jetson> 换成 Jetson 的 IP
rsync -av --exclude node_modules --exclude .git ./ nvidia@<jetson>:/opt/pistelink/
```

> 不会用 rsync 也行：用 scp 或 U 盘，把整个项目文件夹放到 Jetson 的 `/opt/pistelink/` 下即可。

### D-3：做主机初始化（和主路径共用）

容器会把宿主机的 `/etc/pistelink`、`/var/lib/pistelink`、`/run/pistelink` 挂进去用，所以这些目录、
udev 规则、上传密钥**还是要先备好**。直接照上面的 **步骤 4** 和 **步骤 5** 各做一遍：

- **步骤 4**：`sudo /opt/pistelink/deploy/install.sh`（建目录、装 udev、生成上传密钥）
- **步骤 5**：填 `/etc/pistelink/config.toml`、把上传公钥登记到 SFTP 服务器

> conda 那两步（步骤 2、步骤 3）**Docker 形态不需要**，跳过。

### D-4：停掉裸 systemd 服务（避免抢端口）

如果之前按主路径起过 `pistelink.service`，先停掉并取消开机自启，否则两个都抢 `8080`：

```bash
sudo systemctl disable --now pistelink.service
```

（没起过就忽略这步。）

### D-5：没接 MCU / 音响的话，先改一下 compose

容器启动时会去映射串口和声卡设备；**这俩设备在 Jetson 上不存在的话，容器会直接起不来**。
如果你现在只想跑 Web、还没插 MCU，编辑 `deploy/compose.yml`，把用不到的设备行**行首加 `#` 注释掉**：

```yaml
    devices:
      # - /dev/ttyUSB-mcu:/dev/ttyUSB-mcu   # 没插 MCU 就注释掉
      # - /dev/snd:/dev/snd                 # 没有音响就注释掉
```

接了真实硬件正式跑时，再把 `#` 去掉。

### D-6：构建镜像

```bash
cd /opt/pistelink
docker compose -f deploy/compose.yml build
```

- 第一次构建要下基础镜像 + 装 npm/pip 依赖，**走国内源也得几分钟到十几分钟**，耐心等。
- npm / pip / apt 都已默认走国内源，无需额外配置。

**✅ 怎么确认**：构建完能看到镜像就对了：

```bash
docker images | grep pistelink     # 应出现 pistelink/backend   latest
```

### D-7：装并启动 Docker 服务（开机自启）

```bash
sudo cp /opt/pistelink/deploy/systemd/pistelink-stack.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pistelink-stack.service
```

这个服务开机会自动把容器拉起来；容器崩了也会自动重启。

**✅ 怎么确认**：

```bash
docker ps                              # 应看到 backend 容器在跑，几秒后 STATUS 变 healthy
curl http://127.0.0.1:8080/healthz     # 返回 ok 即正常
```

浏览器打开 Web 界面的方式和主路径一样（见上面「3. 打开 Web 界面」）。

### Docker 形态日常运维

```bash
# 看实时日志
cd /opt/pistelink/deploy && docker compose logs -f

# 停 / 启 / 重启
sudo systemctl stop pistelink-stack.service
sudo systemctl start pistelink-stack.service
sudo systemctl restart pistelink-stack.service

# 改了后端/前端代码后，重新构建再上线
cd /opt/pistelink && docker compose -f deploy/compose.yml build
sudo systemctl restart pistelink-stack.service
```

> 只改了 `/etc/pistelink/config.toml`（比如换上传服务器）**不用重新构建**，重启服务即可——配置是
> 挂载进容器的，不在镜像里。

### 切回主路径（裸 systemd）

```bash
sudo systemctl disable --now pistelink-stack.service    # 停 Docker 形态
sudo systemctl enable --now pistelink.service           # 起回裸 systemd
```

### 镜像源覆盖（一般用不到）

默认就是国内源，不改也能构建。万一要换（比如在国际网络构建、想指回官方源），把
`deploy/.env.example` 复制成 `deploy/.env`，改里面的 `NPM_REGISTRY` / `PIP_INDEX_URL` /
`APT_MIRROR`。注意：compose **只有从 `deploy/` 目录运行时**才会读这个 `.env`（stack 服务正是在
`deploy/` 下跑的）；你手动构建要么 `cd /opt/pistelink/deploy && docker compose build`，要么用
`--build-arg` 直接传。

### Docker 排障

- **拉基础镜像 / build 失败**：D-1 加速器没配好或该站临时不可用。换个加速器地址重试，并确认
  `daemon.json` 格式没错、docker 已重启。
- **容器起不来、`docker ps` 看不到**：`cd /opt/pistelink/deploy && docker compose logs` 看报错。
  最常见两个原因：① 没做 D-3（缺 `/etc/pistelink/config.toml`）；② D-5 没把不存在的串口/声卡设备注释掉。
- **网页打不开 / 提示 8080 被占**：多半是 `pistelink.service`（裸）还在跑。
  `sudo systemctl disable --now pistelink.service` 后再重启 Docker 服务。
- **改完 `daemon.json` 后 docker 起不来**：JSON 写错了。`sudo systemctl status docker` 看错误，
  改对再 `sudo systemctl restart docker`。
