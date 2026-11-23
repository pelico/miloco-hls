# miloco-hls

把 [miloco](https://github.com/openclaw)（小米摄像头管理后端）的私有 WebSocket 流桥接成标准 **HLS** 和 **Ogg Opus**，供浏览器、ffmpeg、STT 等程序直接消费。

## 特性

- **固定地址**：did 是小米设备永久 ID，重启/换 token 地址不变
- **按需拉流**：有消费者才唤醒摄像头，60 秒无人观看自动断流，空闲 CPU ≈ 0
- **零转码直通**：Ogg Opus 端点直接封装 miloco 的裸 Opus 包，不经过任何编解码
- **热路径线程化**：WS 收帧 → 剥头 → 写管道全部在阻塞线程，事件循环只管 HTTP
- **自动跟随**：miloco 启动桥接才启动，miloco 停止桥接跟着退出（launchd/systemd）

## 端点

| 地址 | 说明 |
|---|---|
| `http://<host>:18180/cam/<did>/index.m3u8` | 音视频流（H.264 + AAC，HLS） |
| `http://<host>:18180/audio/<did>/index.m3u8` | 纯音频流（AAC，HLS） |
| `http://<host>:18180/audio/<did>/stream.ogg` | 纯音频直通流（Ogg Opus，推荐给 ASR/STT） |
| `http://<host>:18180/healthz` | 健康检查 |

## 依赖

- Python 3.10+，`uv`
- `ffmpeg` 在 PATH 中

## 运行

```bash
uv run --with aiohttp --with av --with websockets miloco-hls-bridge.py
```

环境变量（均可选）：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `MILOCO_BASE` | `http://127.0.0.1:1810` | miloco 后端地址 |
| `MILOCO_CONFIG` | `~/.openclaw/miloco/config.json` | miloco 配置（读取 token） |
| `BRIDGE_HOST` / `BRIDGE_PORT` | `0.0.0.0` / `18180` | 桥接监听地址 |
| `BRIDGE_CACHE` | `~/.cache/miloco-hls` | HLS 片段缓存目录 |
| `BRIDGE_IDLE` | `60` | 无人观看断流秒数 |
| `BRIDGE_CHANNEL` | `0` | 摄像头通道号 |

## 部署

### macOS（launchd 跟随 miloco）

1. 复制 `miloco-hls-bridge.py` 和 `watchdog.sh` 到 `~/.local/share/miloco-hls/`
2. 按实际用户名修改 `deploy/macos-launchd/com.fengke.miloco-hls.plist` 中的路径，放到 `~/Library/LaunchAgents/`
3. `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.fengke.miloco-hls.plist`

原理：watchdog 每 5 秒监督 miloco 健康状态，miloco 掉线则杀掉桥接并退出；launchd 的
`WatchPaths` 监听 miloco 启动日志（`~/.openclaw/miloco/log/launchd-stdio.log`），
miloco 每次启动都会把桥接唤醒。miloco 不在运行时，系统无任何残留进程。

### Linux（Armbian 等，systemd）

```ini
# /etc/systemd/system/miloco-hls.service
[Unit]
Description=miloco WS to HLS/Ogg bridge
After=network-online.target

[Service]
ExecStart=/usr/local/bin/uv run --with aiohttp --with av --with websockets /opt/miloco-hls/miloco-hls-bridge.py
Environment=MILOCO_BASE=http://127.0.0.1:1810
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

## STT/ASR 接入示例

任何能吃 ffmpeg 流的框架都可以直接用 Ogg 直通端点，例如：

```bash
ffmpeg -re -i http://<host>:18180/audio/<did>/stream.ogg \
  -vn -acodec pcm_s16le -ar 16000 -ac 1 -f s16le -
```

实测拉流节奏稳定（页间隔中位 160ms、最大 215ms），granule 精确（无漂移）。
