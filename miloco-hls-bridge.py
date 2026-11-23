#!/usr/bin/env python3
"""miloco WS → HLS 桥接（低开销常驻版）。

把 miloco 后端的摄像头 WebSocket 流（H.264 视频 + Opus/G711 音频）转成标准
HLS，浏览器（hls.js / Safari / iOS / 安卓）或任意 HLS 客户端直接消费。

端点
- /cam/<did>/index.m3u8    音视频流（H.264 + AAC）
- /audio/<did>/index.m3u8  纯音频流（AAC），供其它程序低开销接入
- 两条流共用同一路 WS 订阅和同一个 ffmpeg 进程（一次解码两路输出）

设计要点
- 固定地址：did 是小米设备永久 ID，重启/换 token 地址不变
- 按需拉流：哪个端点被请求才开对应输出；60s 无人观看自动收缩/断流，
  全空闲时 miloco 侧编码随之停止，CPU≈0
- 热路径（WS 收帧→剥头→写管道）全部在阻塞线程里，事件循环只管 HTTP，
  避免每帧 asyncio 调度开销；音频持续匀速喂数，不卡顿
- 自适应：token 每次连接前从 ~/.openclaw/miloco/config.json 现读；断线退避重连

运行：uv run --with aiohttp --with av --with websockets miloco-hls-bridge.py
依赖：ffmpeg 在 PATH 中。
"""

import asyncio
import json
import os
import subprocess
import threading
import time
from pathlib import Path

import av
from aiohttp import ClientSession, ClientTimeout, web
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect as sync_ws_connect

MILOCO_BASE = os.environ.get("MILOCO_BASE", "http://127.0.0.1:1810").rstrip("/")
MILOCO_CONFIG = Path(
    os.environ.get("MILOCO_CONFIG", Path.home() / ".openclaw/miloco/config.json")
)
LISTEN_HOST = os.environ.get("BRIDGE_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("BRIDGE_PORT", "18180"))
CACHE_DIR = Path(os.environ.get("BRIDGE_CACHE", Path.home() / ".cache/miloco-hls"))
IDLE_TIMEOUT = float(os.environ.get("BRIDGE_IDLE", "60"))
CHANNEL = int(os.environ.get("BRIDGE_CHANNEL", "0"))
FFMPEG = os.environ.get("FFMPEG_BIN", "ffmpeg")
M3U8_WAIT = 20.0

log_lock = threading.Lock()


def log(msg: str) -> None:
    with log_lock:
        print(time.strftime("%m-%d %H:%M:%S"), msg, flush=True)


def miloco_token() -> str:
    with open(MILOCO_CONFIG) as f:
        return json.load(f)["server"]["token"]


def miloco_ws_url(kind: str, did: str, token: str) -> str:
    return (
        f"{MILOCO_BASE.replace('http', 'ws', 1)}/api/miot/ws/{kind}_stream"
        f"?camera_id={did}&channel={CHANNEL}&token={token}"
    )


# ---------- 音频解码 ----------

def _g711_table(alaw: bool) -> bytes:
    """标准 G.711 解码表（公式源自 Sun g711.c），输出 s16le。"""
    out = bytearray()
    for i in range(256):
        if alaw:
            v = i ^ 0x55
            t = (v & 0x0F) << 4
            seg = (v & 0x70) >> 4
            if seg == 0:
                t += 8
            elif seg == 1:
                t += 0x108
            else:
                t += 0x108
                t <<= seg - 1
            val = -t if v & 0x80 else t
        else:
            u = ~i & 0xFF
            t = ((u & 0x0F) << 3) + 0x84
            t <<= (u & 0x70) >> 4
            val = 0x84 - t if u & 0x80 else t - 0x84
        out += val.to_bytes(2, "little", signed=True)
    return bytes(out)


ALAW_PCM, ULAW_PCM = _g711_table(True), _g711_table(False)

_opus_dec: av.CodecContext | None = None
_opus_lock = threading.Lock()


def _plane_bytes(p) -> bytes:
    """兼容 av<19（to_bytes）与 av>=19（buffer_ptr）。"""
    if hasattr(p, "to_bytes"):
        return p.to_bytes()
    import ctypes

    return ctypes.string_at(p.buffer_ptr, p.buffer_size)


def opus_to_pcm(data: bytes) -> bytes:
    """裸 Opus 包 → s16le PCM（libopus 默认解为 48kHz 双声道）。"""
    global _opus_dec
    with _opus_lock:
        if _opus_dec is None:
            _opus_dec = av.CodecContext.create("libopus", "r")
        frames = _opus_dec.decode(av.Packet(data))
    return b"".join(_plane_bytes(p) for f in frames for p in f.planes)


def g711_to_pcm(data: bytes, alaw: bool) -> bytes:
    tbl = ALAW_PCM if alaw else ULAW_PCM
    return b"".join(tbl[b << 1 : (b << 1) + 2] for b in data)


# ---------- Ogg Opus 封装（音频直通，零编解码） ----------

def _ogg_crc_table() -> tuple:
    """Ogg CRC32：多项式 0x04c11db7，MSB-first，初值 0，无终值异或。"""
    table = []
    for i in range(256):
        r = i << 24
        for _ in range(8):
            r = ((r << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if r & 0x80000000 else (r << 1) & 0xFFFFFFFF
        table.append(r)
    return table


_OGG_CRC = _ogg_crc_table()


def ogg_page(header_type: int, granule: int, serial: int, seq: int,
             packets) -> bytes:
    """封装一页。packets 是若干完整包；lacing 用 <255 的段值标记包边界。"""
    if isinstance(packets, (bytes, bytearray)):
        packets = [packets]
    segs = []
    for p in packets:
        full, rem = divmod(len(p), 255)
        segs += [255] * full + [rem]  # 整除 255 时 rem=0 也是合法终止段
    if not segs or len(segs) > 255:
        raise ValueError("bad page segment count")
    payload = b"".join(packets)
    head = b"OggS" + bytes([0, header_type]) + granule.to_bytes(8, "little") \
        + serial.to_bytes(4, "little") + seq.to_bytes(4, "little") \
        + b"\x00\x00\x00\x00" + bytes([len(segs)]) + bytes(segs)
    crc = 0
    for b in head + payload:
        crc = ((crc << 8) & 0xFFFFFF00) ^ _OGG_CRC[((crc >> 24) ^ b) & 0xFF]
    return head[:22] + crc.to_bytes(4, "little") + head[26:] + payload


def opus_head_page(serial: int) -> bytes:
    # OpusHead: 版本1, 单声道, preskip 0, 48kHz, 无增益, 单流映射
    head = b"OpusHead" + bytes([1, 1]) + (0).to_bytes(2, "little") \
        + (48000).to_bytes(4, "little") + (0).to_bytes(2, "little") + bytes([0])
    return ogg_page(0x02, 0, serial, 0, head)


def opus_tags_page(serial: int) -> bytes:
    tags = b"OpusTags" + (5).to_bytes(4, "little") + b"miloco" \
        + (0).to_bytes(4, "little")
    return ogg_page(0x00, 0, serial, 1, tags)


def opus_pkt_samples(pkt: bytes) -> int | None:
    """按 RFC6716 TOC 精确计算一个 opus 包的采样数（@48kHz）。

    同一包内所有帧共享 config，时长 = 帧数 × config 时长。
    """
    if not pkt:
        return None
    cfg = pkt[0] >> 3
    code = pkt[0] & 3
    if cfg >= 32:
        return None
    if code == 0:
        n = 1
    elif code in (1, 2):
        n = 2
    else:  # code 3：帧数在第二字节低 6 位
        if len(pkt) < 2:
            return None
        n = pkt[1] & 0x3F
        if n == 0:
            return None
    if cfg < 12:
        ms = (10, 20, 40, 60)[cfg % 4]
    elif cfg < 16:
        ms = (10, 20)[cfg % 2]
    else:
        ms = (2.5, 5, 10, 20)[cfg % 4]
    return int(ms * 48) * n


class OggMuxer:
    """把裸 Opus 包连续装进 Ogg 页。granule 用首包实测采样数累计。"""

    PKTS_PER_PAGE = 4  # 每页约 160ms，兼顾延迟与开销

    def __init__(self):
        self.serial = int.from_bytes(os.urandom(4), "little") or 1
        self.seq = 2
        self.granule = 0
        self.samples_per_pkt = None
        self.buf: list[bytes] = []
        self.count = 0
        self.pkt_total = 0

    def start(self) -> bytes:
        return opus_head_page(self.serial) + opus_tags_page(self.serial)

    def feed(self, pkt: bytes, samples: int | None = None) -> bytes | None:
        """喂一个 opus 包；samples 缺省时用首包学习值。"""
        if self.samples_per_pkt is None:
            if samples is None:
                return None
            self.samples_per_pkt = samples
        self.granule += samples if samples is not None else self.samples_per_pkt
        self.pkt_total += 1
        self.buf.append(pkt)
        self.count += 1
        if self.count >= self.PKTS_PER_PAGE:
            self.count = 0
            out = ogg_page(0x00, self.granule, self.serial, self.seq,
                           self.buf)
            self.seq += 1
            self.buf = []
            return out
        return None

    def flush(self) -> bytes | None:
        if not self.buf:
            return None
        out = ogg_page(0x04, self.granule, self.serial, self.seq,
                       self.buf)
        self.seq += 1
        self.buf = []
        return out


# ---------- ffmpeg 流水线 ----------

HLS_FLAGS = "-hls_flags", "delete_segments+temp_file+independent_segments"


class FFmpegProc:
    """audio: None 或 {"kind","rate","ch"}；av/a_only 控制输出组合。"""

    def __init__(self, did: str, audio: dict | None, av: bool, a_only: bool):
        self.dir = CACHE_DIR / did
        self.dir.mkdir(parents=True, exist_ok=True)
        # 序列号每次重启从 0 开始，先清掉上一轮残留，避免孤儿片段堆积
        for old in self.dir.glob("s_*.ts"):
            old.unlink(missing_ok=True)
        for old in self.dir.glob("a_*.ts"):
            old.unlink(missing_ok=True)
        (self.dir / "index.m3u8").unlink(missing_ok=True)
        (self.dir / "a.m3u8").unlink(missing_ok=True)

        inputs, maps = [], []
        a_idx = 0
        if av:
            v_r, self.v_w = os.pipe()
            inputs += [
                "-thread_queue_size", "512",
                "-fflags", "+genpts", "-use_wallclock_as_timestamps", "1",
                "-f", "h264", "-i", f"pipe:{v_r}",
            ]
            maps += ["-map", "0:v:0"]
            a_idx = 1
            fds = [v_r]
        else:
            self.v_w = None
            fds = []
        if audio:
            a_r, self.a_w = os.pipe()
            fds.append(a_r)
            inputs += [
                "-thread_queue_size", "512",
                "-use_wallclock_as_timestamps", "1",
                "-f", "s16le", "-ar", str(audio["rate"]),
                "-ac", str(audio["ch"]), "-i", f"pipe:{a_r}",
            ]
            if av:
                maps += ["-map", f"{a_idx}:a:0", "-c:a", "aac", "-b:a", "48k"]
        else:
            self.a_w = None

        if av:
            maps += [
                "-c:v", "copy",
                "-f", "hls", "-hls_time", "2", "-hls_list_size", "4", *HLS_FLAGS,
                "-hls_segment_filename", str(self.dir / "s_%09d.ts"),
                str(self.dir / "index.m3u8"),
            ]
        if a_only and audio:
            maps += [
                "-map", f"{a_idx}:a:0", "-c:a", "aac", "-b:a", "48k",
                "-f", "hls", "-hls_time", "2", "-hls_list_size", "4", *HLS_FLAGS,
                "-hls_segment_filename", str(self.dir / "a_%09d.ts"),
                str(self.dir / "a.m3u8"),
            ]

        cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-nostats",
               *inputs, *maps]
        self.proc = subprocess.Popen(
            cmd, pass_fds=fds,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        for r in fds:
            os.close(r)
        self.alive = True
        threading.Thread(target=self._drain, daemon=True).start()
        mode = ("av" if av else "") + ("+audio" if a_only and audio else "")
        log(f"[{did}] ffmpeg 已启动 ({mode or '无输出?'})")

    def _drain(self) -> None:
        for line in self.proc.stderr:
            log(f"[ffmpeg {self.proc.pid}] {line.decode(errors='replace').rstrip()}")
        self.alive = False

    def stop(self) -> None:
        self.alive = False
        for name in ("v_w", "a_w"):
            fd = getattr(self, name, None)
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
                setattr(self, name, None)
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        log(f"[ffmpeg {self.proc.pid}] 已退出")


# ---------- 摄像头流水线 ----------

class CameraPipeline:
    """一台摄像头的拉流管理：视频线程 + 音频线程 + ffmpeg。

    av / audio 两个端点独立计时；输出组合变化时自动重建 ffmpeg。
    """

    def __init__(self, did: str):
        self.did = did
        self.av_access = 0.0
        self.a_access = 0.0
        self.ac_stop = asyncio.Event()   # 整条流水线停止（asyncio 侧）
        self.gen_stop = threading.Event()  # 当前代际线程退出
        self.task: asyncio.Task | None = None
        self.ff: FFmpegProc | None = None
        self.fails = 0
        # 线程间共享
        self.pipe_ready = threading.Event()
        self.audio_probe = threading.Event()
        self.audio_info: dict | None = None
        self.got_video = False

    # -- 访问计时 --

    def touch_av(self) -> None:
        self.av_access = time.monotonic()
        self.start()

    def touch_audio(self) -> None:
        self.a_access = time.monotonic()
        self.start()

    def _av_active(self) -> bool:
        return time.monotonic() - self.av_access < IDLE_TIMEOUT

    def _a_active(self) -> bool:
        return time.monotonic() - self.a_access < IDLE_TIMEOUT

    # -- 生命周期 --

    def start(self) -> None:
        if self.task is None or self.task.done():
            self.ac_stop.clear()
            self.task = asyncio.create_task(self._supervise())
            log(f"[{self.did}] 开始拉流")

    async def stop(self) -> None:
        self.ac_stop.set()
        self.gen_stop.set()
        if self.task and not self.task.done():
            try:
                await asyncio.wait_for(asyncio.shield(self.task), timeout=8)
            except Exception:
                pass

    async def _supervise(self) -> None:
        try:
            while not self.ac_stop.is_set():
                av_on, a_on = self._av_active(), self._a_active()
                if not av_on and not a_on:
                    log(f"[{self.did}] 无人观看，断流")
                    return
                cur = (av_on, a_on)
                try:
                    await self._stream_once(av_on, a_on, cur)
                    self.fails = 0
                except Exception as e:
                    self.fails += 1
                    log(f"[{self.did}] 流异常: {type(e).__name__}: {e}")
                finally:
                    await self._teardown()
                if self.ac_stop.is_set():
                    return
                wait = max(1, min(3 * self.fails, 15))
                log(f"[{self.did}] {wait}s 后重连")
                try:
                    await asyncio.wait_for(self.ac_stop.wait(), timeout=wait)
                    return
                except asyncio.TimeoutError:
                    pass
        finally:
            await self._teardown()

    async def _teardown(self) -> None:
        self.gen_stop.set()
        self.pipe_ready.clear()
        if self.ff is not None:
            await asyncio.get_running_loop().run_in_executor(None, self.ff.stop)
            self.ff = None

    # -- 单代际拉流 --

    async def _stream_once(self, av_on: bool, a_on: bool, cur: tuple) -> None:
        self.gen_stop = threading.Event()
        self.audio_info = None
        self.got_video = False
        self.pipe_ready = threading.Event()
        self.audio_probe = threading.Event()

        token = miloco_token()
        tv = threading.Thread(
            target=self._video_thread, args=(token, av_on), daemon=True
        )
        ta = threading.Thread(
            target=self._audio_thread, args=(token, a_on), daemon=True
        )
        if av_on:
            tv.start()
        if a_on:
            ta.start()
        if a_on:
            # 等音频编码探测（≤8s），决定 ffmpeg 音频输入参数
            await asyncio.to_thread(self.audio_probe.wait, 8)
        audio = self.audio_info

        if not av_on and not audio:
            raise RuntimeError("无音频可用，纯音频模式无法建立")

        self.ff = await asyncio.get_running_loop().run_in_executor(
            None, FFmpegProc, self.did, audio, av_on, a_on
        )
        self.pipe_ready.set()

        # 守望：ffmpeg 死 / 视频线程死 / 输出组合变化 → 退出本代际重建
        while not self.ac_stop.is_set() and not self.gen_stop.is_set():
            if not self.ff.alive:
                raise RuntimeError("ffmpeg 意外退出")
            if av_on and not tv.is_alive():
                raise RuntimeError("视频 WS 断开")
            if a_on and not ta.is_alive() and audio is not None:
                raise RuntimeError("音频 WS 断开")
            now = (self._av_active(), self._a_active())
            if now != cur:
                log(f"[{self.did}] 输出组合变化 {cur} → {now}，重建")
                return
            if not now[0] and not now[1]:
                return
            await asyncio.sleep(0.5)

    # -- 热路径线程 --

    def _video_thread(self, token: str, _av_on: bool) -> None:
        try:
            ws = sync_ws_connect(
                miloco_ws_url("video", self.did, token),
                open_timeout=15, max_size=2**22, close_timeout=5,
            )
            self._video_loop(ws)
        except Exception as e:
            log(f"[{self.did}] 视频线程异常退出: {type(e).__name__}: {e}")
        finally:
            log(f"[{self.did}] 视频线程退出")

    def _video_loop(self, ws) -> None:
        try:
            while not self.gen_stop.is_set():
                try:
                    msg = ws.recv(timeout=1)
                except TimeoutError:
                    continue
                except (ConnectionClosed, OSError):
                    break
                if not isinstance(msg, bytes) or len(msg) <= 16:
                    continue
                if msg[1:9] != b"\x00" * 8:  # 16B 头: [0]=关键帧 [1:9]=零填充
                    continue
                if msg[0] == 1:
                    self.got_video = True
                if not (self.got_video and self.pipe_ready.is_set()):
                    continue  # 丢等关键帧之前的帧（.mid-GOP 接入本来也不能解）
                try:
                    os.write(self.ff.v_w, msg[16:])
                except Exception:
                    break
        finally:
            try:
                ws.close()
            except Exception:
                pass

    def _audio_thread(self, token: str, _a_on: bool) -> None:
        try:
            ws = sync_ws_connect(
                miloco_ws_url("audio", self.did, token),
                open_timeout=8, max_size=2**22, close_timeout=5,
            )
            self._audio_loop(ws)
        except Exception as e:
            log(f"[{self.did}] 音频线程异常退出: {type(e).__name__}: {e}")
        finally:
            self.audio_probe.set()
            log(f"[{self.did}] 音频线程退出")

    def _audio_loop(self, ws) -> None:
        kind = rate = ch = None
        warned = False
        try:
            while not self.gen_stop.is_set():
                try:
                    msg = ws.recv(timeout=1)
                except TimeoutError:
                    continue
                except (ConnectionClosed, OSError):
                    break
                if isinstance(msg, str):
                    try:
                        codec = json.loads(msg).get("codec")
                    except Exception:
                        continue
                    if codec in ("opus", "g711a", "g711u"):
                        kind = codec
                        rate, ch = (48000, 2) if codec == "opus" else (8000, 1)
                        self.audio_info = {"kind": kind, "rate": rate, "ch": ch}
                        self.audio_probe.set()
                elif isinstance(msg, bytes):
                    if kind is None:  # 迟到者没等到 init，按帧长启发
                        kind = "g711a" if len(msg) <= 220 else "opus"
                        rate, ch = (48000, 2) if kind == "opus" else (8000, 1)
                        self.audio_info = {"kind": kind, "rate": rate, "ch": ch}
                        self.audio_probe.set()
                    if not self.pipe_ready.is_set():
                        continue
                    try:
                        pcm = (
                            opus_to_pcm(msg)
                            if kind == "opus"
                            else g711_to_pcm(msg, alaw=(kind == "g711a"))
                        )
                        os.write(self.ff.a_w, pcm)
                    except Exception as e:
                        if not warned:
                            warned = True
                            log(f"[{self.did}] 音频泵停止: {type(e).__name__}: {e}")
                        break
        finally:
            try:
                ws.close()
            except Exception:
                pass


# ---------- Web 服务 ----------

INDEX_TMPL = """<!doctype html><meta charset=utf-8>
<title>miloco HLS</title>
<style>body{{font-family:-apple-system,sans-serif;max-width:860px;margin:2rem auto;padding:0 1rem}}
.cam{{margin:1.5rem 0}}video{{width:100%;border-radius:8px;background:#000}}code{{color:#666;font-size:.85em}}</style>
<script src="https://cdn.jsdelivr.net/npm/hls.js@1.5.20"></script>
<script>function play(id,did){{const v=document.getElementById(id);
if(v.dataset.on){{v.pause();v.removeAttribute('src');v.load();v.dataset.on='';event.target.textContent='播放';return}}
const url='/cam/'+did+'/index.m3u8';if(Hls.isSupported()){{const h=new Hls();h.loadSource(url);h.attachMedia(v)}}
else{{v.src=url}}v.play();v.dataset.on='1';event.target.textContent='停止'}}</script>
<h1>miloco 摄像头</h1>{cams}
<p><code>/cam/&lt;did&gt;/index.m3u8</code> 音视频 · <code>/audio/&lt;did&gt;/index.m3u8</code> 纯音频</p>"""


class Bridge:
    def __init__(self, session: ClientSession):
        self.session = session
        self.pipes: dict[str, CameraPipeline] = {}
        self.cameras: list[dict] = []
        self.nocache = {"Cache-Control": "no-store"}

    def pipe(self, did: str) -> CameraPipeline:
        p = self.pipes.get(did)
        if p is None:
            p = self.pipes[did] = CameraPipeline(did)
        return p

    async def janitor(self) -> None:
        while True:
            await asyncio.sleep(5)
            for did, p in list(self.pipes.items()):
                if (
                    not p.ac_stop.is_set()
                    and p.task is not None
                    and not p.task.done()
                    and not p._av_active()
                    and not p._a_active()
                ):
                    await p.stop()

    async def refresh_cameras(self) -> None:
        while True:
            try:
                async with self.session.get(
                    f"{MILOCO_BASE}/api/miot/scope/cameras",
                    headers={"Authorization": f"Bearer {miloco_token()}"},
                    timeout=ClientTimeout(total=10),
                ) as r:
                    data = (await r.json()).get("data") or []
                    self.cameras = [
                        {"did": str(c["did"]), "name": c.get("name", c["did"])}
                        for c in data
                    ]
            except Exception as e:
                log(f"摄像头列表刷新失败: {e}")
            await asyncio.sleep(300)

    # -- 路由 --

    async def h_index(self, request):
        cams = "".join(
            f'<div class="cam"><h3>{c["name"]}</h3>'
            f'<video id="v{c["did"]}" controls muted playsinline></video>'
            f"<button onclick=\"play('v{c['did']}','{c['did']}')\">播放</button> "
            f'<code>/cam/{c["did"]}/index.m3u8</code></div>'
            for c in self.cameras
        )
        return web.Response(
            text=INDEX_TMPL.format(cams=cams or "<p>未发现摄像头</p>"),
            content_type="text/html",
        )

    async def _serve_m3u8(self, did: str, name: str, touch):
        p = self.pipe(did)
        touch(p)
        m3u8 = CACHE_DIR / did / name
        deadline = time.monotonic() + M3U8_WAIT
        while not m3u8.exists():
            if time.monotonic() > deadline or p.task.done():
                return web.Response(status=503, text="流不可用（摄像头离线？）")
            await asyncio.sleep(0.3)
        return web.FileResponse(m3u8, headers=self.nocache)

    async def h_cam_m3u8(self, request):
        return await self._serve_m3u8(
            request.match_info["did"], "index.m3u8", CameraPipeline.touch_av
        )

    async def h_audio_m3u8(self, request):
        return await self._serve_m3u8(
            request.match_info["did"], "a.m3u8", CameraPipeline.touch_audio
        )

    async def _serve_seg(self, did: str, seg: str, touch):
        if did in self.pipes:
            touch(self.pipes[did])
        f = CACHE_DIR / did / f"{seg}.ts"
        if not f.exists():
            return web.Response(status=404, text="gone")
        return web.FileResponse(f)

    async def h_cam_seg(self, request):
        return await self._serve_seg(
            request.match_info["did"],
            request.match_info["seg"],
            CameraPipeline.touch_av,
        )

    async def h_audio_seg(self, request):
        return await self._serve_seg(
            request.match_info["did"],
            request.match_info["seg"],
            CameraPipeline.touch_audio,
        )

    async def h_health(self, request):
        return web.json_response({"ok": True, "cameras": len(self.cameras)})

    # -- Ogg Opus 直通流（供 ffmpeg/STT 等程序低开销消费） --

    async def h_audio_ogg(self, request):
        did = request.match_info["did"]
        resp = web.StreamResponse(headers={
            "Content-Type": "application/ogg",
            "Cache-Control": "no-store",
            "Access-Control-Allow-Origin": "*",
        })
        await resp.prepare(request)
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        loop = asyncio.get_running_loop()

        def _put(item):
            # 事件循环侧执行：满时丢最旧，保证平滑及时消费
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(item)

        stop = threading.Event()
        t = threading.Thread(
            target=self._ogg_producer, args=(did, q, stop, loop, _put),
            daemon=True,
        )
        t.start()
        mux = OggMuxer()
        try:
            await resp.write(mux.start())
            while True:
                item = await asyncio.wait_for(q.get(), timeout=60)
                if item is None:
                    break
                out = mux.feed(*item)
                if out:
                    await resp.write(out)
            fin = mux.flush()
            if fin:
                await resp.write(fin)
        except (ConnectionResetError, asyncio.TimeoutError, asyncio.CancelledError):
            pass
        finally:
            stop.set()
            try:
                await resp.write_eof()
            except Exception:
                pass
            log(
                f"[{did}] ogg 直通流客户端断开 (packets={mux.pkt_total}, "
                f"granule={mux.granule/48000:.1f}s)"
            )
        return resp

    def _ogg_producer(self, did: str, q: asyncio.Queue, stop: threading.Event,
                      loop: asyncio.AbstractEventLoop, put):
        try:
            token = miloco_token()
            ws = sync_ws_connect(
                miloco_ws_url("audio", did, token),
                open_timeout=8, max_size=2**22, close_timeout=5,
            )
        except Exception as e:
            log(f"[{did}] ogg 源音频 WS 连接失败: {type(e).__name__}: {e}")
            loop.call_soon_threadsafe(put, None)
            return
        try:
            while not stop.is_set():
                try:
                    msg = ws.recv(timeout=1)
                except TimeoutError:
                    continue
                except (ConnectionClosed, OSError):
                    break
                if not isinstance(msg, bytes) or not msg:
                    continue
                samples = opus_pkt_samples(msg)
                if samples is None:
                    log(f"[{did}] 音频非 Opus 或包异常，ogg 直通退出")
                    break
                # 跨线程投递必须走 call_soon_threadsafe，否则不会唤醒
                # 事件循环，消费者会攒出数秒的突发
                loop.call_soon_threadsafe(put, (msg, samples))
        finally:
            try:
                ws.close()
            except Exception:
                pass
            loop.call_soon_threadsafe(put, None)


async def main() -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    session = ClientSession()
    bridge = Bridge(session)
    app = web.Application()
    app.router.add_get("/", bridge.h_index)
    app.router.add_get("/cam/{did}/index.m3u8", bridge.h_cam_m3u8)
    app.router.add_get("/cam/{did}/{seg}.ts", bridge.h_cam_seg)
    app.router.add_get("/audio/{did}/index.m3u8", bridge.h_audio_m3u8)
    app.router.add_get("/audio/{did}/{seg}.ts", bridge.h_audio_seg)
    app.router.add_get("/audio/{did}/stream.ogg", bridge.h_audio_ogg)
    app.router.add_get("/healthz", bridge.h_health)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, LISTEN_HOST, LISTEN_PORT).start()
    log(f"桥接服务已启动: http://{LISTEN_HOST}:{LISTEN_PORT}")
    asyncio.create_task(bridge.janitor())
    asyncio.create_task(bridge.refresh_cameras())
    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
