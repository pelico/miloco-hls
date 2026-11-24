#!/usr/bin/env python3
"""miloco 摄像头控制台（单文件，仅依赖 aiohttp）。

miloco 把全部 MIoT 控制能力（总开关/云台/AI检测/巡航/隐私保护/烟火报警...）
暴露成了 HTTP API，但没有控制界面。本服务补上：
- spec 驱动的控制面板：摄像头有什么能力就渲染什么，无需按机型硬编码
- 实时预览：复用 miloco-hls-bridge 的 HLS 流（未运行则显示提示）
- 定时任务：存在本地 JSON，进程内每 30s 调度（替代 launchd 方案，可容器化）
- 薄代理层：解决浏览器跨域 + token 动态化（每次请求现读 miloco config）

端点（页面 + API 同源）：
  GET  /                                 控制台页面
  GET  /api/cameras                      摄像头列表
  GET  /api/devices/{did}/spec           能力规格（属性+动作）
  GET  /api/devices/{did}/props          当前全部属性值
  POST /api/devices/{did}/control        控制（透传 set_property/... /call_action）
  GET|POST /api/schedules                定时任务列表/新增
  PATCH|DELETE /api/schedules/{id}       启停/删除定时任务
  POST /api/schedules/{id}/run           立即执行一次

运行：uv run --with aiohttp miloco-console.py
环境变量：MILOCO_BASE / MILOCO_CONFIG / CONSOLE_HOST(默认0.0.0.0) /
         CONSOLE_PORT(默认18190) / CONSOLE_SCHEDULES(定时任务JSON路径)
容器化：无本地路径外的任何依赖，config.json 只读挂载即可，Dockerfile 两行。
"""

import asyncio
import json
import os
import secrets
import time
import uuid
from pathlib import Path

from aiohttp import ClientSession, ClientTimeout, web

MILOCO_BASE = os.environ.get("MILOCO_BASE", "http://127.0.0.1:1810").rstrip("/")
MILOCO_CONFIG = Path(
    os.environ.get("MILOCO_CONFIG", Path.home() / ".openclaw/miloco/config.json")
)
HOST = os.environ.get("CONSOLE_HOST", "0.0.0.0")
PORT = int(os.environ.get("CONSOLE_PORT", "18190"))
SCHEDULES_FILE = Path(
    os.environ.get(
        "CONSOLE_SCHEDULES", Path.home() / ".local/share/miloco-hls/schedules.json"
    )
)
BRIDGE_BASE_ENV = os.environ.get("BRIDGE_BASE", "")  # 留空则用 location.host:18180

SCHED_LOCK = asyncio.Lock()

# ---------- 访问密码 ----------
# 优先 CONSOLE_PASS 环境变量；否则读写 CONSOLE_PASS_FILE；都没有则自动生成并落盘
_PASS_FILE = Path(
    os.environ.get(
        "CONSOLE_PASS_FILE", Path.home() / ".local/share/miloco-hls/console.pass"
    )
)
CONSOLE_PASS = os.environ.get("CONSOLE_PASS")
if not CONSOLE_PASS:
    if _PASS_FILE.exists():
        CONSOLE_PASS = _PASS_FILE.read_text().strip()
    if not CONSOLE_PASS:
        CONSOLE_PASS = secrets.token_urlsafe(6)
        _PASS_FILE.parent.mkdir(parents=True, exist_ok=True)
        _PASS_FILE.write_text(CONSOLE_PASS)
        print(f"已生成访问密码: {CONSOLE_PASS}  (存于 {_PASS_FILE})", flush=True)
SESSIONS: set[str] = set()
COOKIE = "mcsid"


@web.middleware
async def auth_mw(request: web.Request, handler):
    if not CONSOLE_PASS or request.path == "/login":
        return await handler(request)
    if request.cookies.get(COOKIE) in SESSIONS:
        return await handler(request)
    if request.path.startswith("/api/"):
        return web.json_response({"code": 401, "message": "未登录"}, status=401)
    raise web.HTTPFound("/login")


LOGIN_PAGE = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>摄像头控制台 · 登录</title><style>
body{{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:#10141c;color:#e8ecf3;font:15px/1.6 system-ui,-apple-system,"PingFang SC",sans-serif}}
.card{{background:#1a2030;border:1px solid #2a3348;border-radius:12px;padding:28px 26px;width:300px}}
h1{{font-size:17px;margin:0 0 18px}}
input{{width:100%;box-sizing:border-box;background:#10141c;border:1px solid #2a3348;
border-radius:8px;color:#e8ecf3;padding:10px 12px;font-size:15px;outline:none}}
input:focus{{border-color:#4a7dff}}
button{{width:100%;margin-top:12px;background:#4a7dff;border:none;border-radius:8px;
color:#fff;padding:10px;font-size:15px;cursor:pointer}}
button:hover{{background:#3a6bef}}
.err{{color:#ff6b6b;font-size:13px;margin-top:10px;{show}}}
</style></head><body><form class="card" method="post" action="/login">
<h1>摄像头控制台</h1>
<input type="password" name="password" placeholder="访问密码" autofocus>
<button type="submit">进入</button>
<div class="err">密码错误，请重试</div>
</form></body></html>"""


async def h_login_get(request: web.Request) -> web.Response:
    if request.cookies.get(COOKIE) in SESSIONS:
        raise web.HTTPFound("/")
    return web.Response(text=LOGIN_PAGE.format(show="none"), content_type="text/html")


async def h_login_post(request: web.Request) -> web.Response:
    form = await request.post()
    if form.get("password") == CONSOLE_PASS:
        sid = secrets.token_urlsafe(32)
        SESSIONS.add(sid)
        resp = web.HTTPFound("/")
        resp.set_cookie(COOKIE, sid, max_age=30 * 86400, httponly=True, samesite="Lax")
        raise resp
    return web.Response(text=LOGIN_PAGE.format(show="block"), content_type="text/html")


def miloco_token() -> str:
    with open(MILOCO_CONFIG) as f:
        return json.load(f)["server"]["token"]


def miloco_headers() -> dict:
    return {"Authorization": f"Bearer {miloco_token()}"}


async def miloco_req(
    session: ClientSession, method: str, path: str, body: dict | None = None
) -> web.Response:
    """转发到 miloco API，出错统一 502 + JSON。"""
    url = f"{MILOCO_BASE}{path}"
    try:
        async with session.request(
            method, url, headers=miloco_headers(), json=body, timeout=ClientTimeout(total=15)
        ) as resp:
            data = await resp.json(content_type=None)
            return web.json_response(data, status=resp.status)
    except Exception as e:
        return web.json_response({"code": -1, "message": f"miloco 不可达: {e}"}, status=502)


# ---------- 定时任务 ----------

def _load_schedules() -> list[dict]:
    if SCHEDULES_FILE.exists():
        with open(SCHEDULES_FILE) as f:
            return json.load(f)
    return []


def _save_schedules(items: list[dict]) -> None:
    SCHEDULES_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = SCHEDULES_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False, indent=2))
    tmp.replace(SCHEDULES_FILE)


async def _exec_schedule(session: ClientSession, sch: dict) -> dict:
    """执行一条定时任务：on/off 即总开关，custom 设任意属性。"""
    did = sch["did"]
    if sch.get("action") in ("on", "off"):
        body = {"type": "set_property", "iid": "prop.2.1", "value": sch["action"] == "on"}
    else:
        raw = sch.get("value", "")
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            value = raw
        body = {"type": "set_property", "iid": sch.get("iid", ""), "value": value}
    resp = await miloco_req(session, "POST", f"/api/miot/devices/{did}/control", body)
    ok = resp.status == 200
    try:
        data = json.loads(resp.text)
        ok = ok and data.get("code") == 0
    except ValueError:
        pass
    print(time.strftime("%m-%d %H:%M:%S"), f"定时 did={did} -> {sch.get('action')} {'成功' if ok else '失败'}", flush=True)
    return {"ok": ok}


async def scheduler_loop(app: web.Application) -> None:
    """每 30s 扫描：今天没跑过且已到点 -> 执行（补跑错过的当日任务）。"""
    session: ClientSession = app["client"]
    while True:
        try:
            async with SCHED_LOCK:
                items = _load_schedules()
                if items:
                    now = time.localtime()
                    today = time.strftime("%Y-%m-%d", now)
                    hm = now.tm_hour * 60 + now.tm_min
                    dirty = False
                    for sch in items:
                        if not sch.get("enabled", True):
                            continue
                        if sch.get("last_run") == today:
                            continue
                        t_h, t_m = map(int, sch["time"].split(":"))
                        if hm >= t_h * 60 + t_m:
                            r = await _exec_schedule(session, sch)
                            sch["last_run"] = today
                            sch["last_ok"] = r["ok"]
                            dirty = True
                    if dirty:
                        _save_schedules(items)
        except Exception as e:
            print("调度器异常:", e, flush=True)
        await asyncio.sleep(30)


# ---------- HTTP 路由 ----------

async def h_index(_req: web.Request) -> web.Response:
    return web.Response(text=PAGE_HTML, content_type="text/html")


async def h_cameras(req: web.Request) -> web.Response:
    resp = await miloco_req(req.app["client"], "GET", "/api/miot/camera_list")
    try:
        data = json.loads(resp.text)
        cams = data.get("data") or []
        if isinstance(cams, list) and cams:
            # 合并 scope 的 lan_reachable，前端用它挑选默认预览的摄像头
            scope = await miloco_req(
                req.app["client"], "GET", "/api/miot/scope/cameras"
            )
            try:
                scon = json.loads(scope.text).get("data") or []
                lan = {c["did"]: c.get("lan_reachable") for c in scon}
                for c in cams:
                    c["lan_reachable"] = lan.get(c["did"])
            except (ValueError, KeyError, AttributeError):
                pass
        return web.json_response(data, status=resp.status)
    except ValueError:
        return resp


async def h_spec(req: web.Request) -> web.Response:
    did = req.match_info["did"]
    resp = await miloco_req(req.app["client"], "GET", f"/api/miot/devices/{did}/spec")
    return resp


async def h_props(req: web.Request) -> web.Response:
    did = req.match_info["did"]
    resp = await miloco_req(req.app["client"], "GET", f"/api/miot/devices/{did}/status")
    return resp


async def h_control(req: web.Request) -> web.Response:
    did = req.match_info["did"]
    body = await req.json()
    resp = await miloco_req(
        req.app["client"], "POST", f"/api/miot/devices/{did}/control", body
    )
    return resp


async def h_sched_list(_req: web.Request) -> web.Response:
    async with SCHED_LOCK:
        return web.json_response({"code": 0, "data": _load_schedules()})


async def h_sched_add(req: web.Request) -> web.Response:
    b = await req.json()
    sch = {
        "id": uuid.uuid4().hex[:8],
        "name": b.get("name") or "",
        "did": b["did"],
        "time": b["time"],  # "HH:MM"
        "action": b.get("action", "off"),  # on / off / custom
        "iid": b.get("iid", ""),
        "value": b.get("value", ""),
        "enabled": True,
        "last_run": None,
    }
    async with SCHED_LOCK:
        items = _load_schedules()
        items.append(sch)
        _save_schedules(items)
    return web.json_response({"code": 0, "data": sch})


async def h_sched_patch(req: web.Request) -> web.Response:
    sid = req.match_info["id"]
    b = await req.json()
    async with SCHED_LOCK:
        items = _load_schedules()
        for sch in items:
            if sch["id"] == sid:
                if "enabled" in b:
                    sch["enabled"] = bool(b["enabled"])
                    sch["last_run"] = None  # 重新启用允许今天再跑
                _save_schedules(items)
                return web.json_response({"code": 0, "data": sch})
    return web.json_response({"code": 404, "message": "任务不存在"}, status=404)


async def h_sched_del(req: web.Request) -> web.Response:
    sid = req.match_info["id"]
    async with SCHED_LOCK:
        items = [s for s in _load_schedules() if s["id"] != sid]
        _save_schedules(items)
    return web.json_response({"code": 0})


async def h_sched_run(req: web.Request) -> web.Response:
    sid = req.match_info["id"]
    async with SCHED_LOCK:
        items = _load_schedules()
        sch = next((s for s in items if s["id"] == sid), None)
    if not sch:
        return web.json_response({"code": 404, "message": "任务不存在"}, status=404)
    r = await _exec_schedule(req.app["client"], sch)
    return web.json_response({"code": 0 if r["ok"] else -1})


# ---------- 页面 ----------

PAGE_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>摄像头控制台</title>
<script src="https://cdn.jsdelivr.net/npm/hls.js@1/dist/hls.min.js"></script>
<style>
:root{--bg:#0f1115;--card:#181b22;--card2:#1f232c;--line:#2a2f3a;--tx:#e6e9ef;--tx2:#8b93a3;--ac:#4f8cff;--ok:#3fbf6f;--bad:#e05252}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--tx);font:14px/1.5 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif;padding:16px}
.wrap{max-width:1560px;margin:0 auto}
h1{font-size:18px;margin-bottom:14px;display:flex;align-items:center;gap:10px}
h1 .dot{width:9px;height:9px;border-radius:50%;background:var(--bad)}
h1 .dot.up{background:var(--ok)}
.layout{display:grid;grid-template-columns:250px 1fr;gap:16px}
@media(max-width:860px){.layout{grid-template-columns:1fr}}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px;margin-bottom:14px}
.camlist{display:flex;flex-direction:column;gap:6px}
.cam{padding:9px 11px;border-radius:8px;background:var(--card2);cursor:pointer;border:1px solid transparent}
.cam:hover{border-color:var(--line)}
.cam.sel{border-color:var(--ac);background:#20304d}
.cam .nm{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.cam .sub{color:var(--tx2);font-size:12px;display:flex;gap:8px;margin-top:2px;white-space:nowrap}
.on{color:var(--ok)}.off{color:var(--bad)}
#player{width:100%;aspect-ratio:16/9;background:#000;border-radius:8px;object-fit:contain}
.preview-hint{color:var(--tx2);font-size:12px;margin-top:6px}
.svc{border:1px solid var(--line);border-radius:10px;margin-bottom:10px;background:var(--card);overflow:hidden}
.svc>summary{cursor:pointer;padding:11px 14px;font-weight:600;list-style:none;display:flex;justify-content:space-between;align-items:center}
.svc>summary::-webkit-details-marker{display:none}
.svc>summary .cnt{color:var(--tx2);font-weight:400;font-size:12px}
.svc .body{padding:4px 14px 12px;border-top:1px solid var(--line)}
.row{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:9px 0;border-bottom:1px dashed var(--line)}
.row:last-child{border-bottom:none}
.row .lbl{color:var(--tx)}
.row .sub{color:var(--tx2);font-size:12px}
.sw{position:relative;width:44px;height:24px;flex:none}
.sw input{opacity:0;width:0;height:0}
.sw i{position:absolute;inset:0;background:#3a4050;border-radius:12px;transition:.15s;cursor:pointer}
.sw i:before{content:"";position:absolute;width:18px;height:18px;border-radius:50%;background:#fff;top:3px;left:3px;transition:.15s}
.sw input:checked+i{background:var(--ok)}
.sw input:checked+i:before{left:23px}
.val{display:flex;align-items:center;gap:8px}
.val output{min-width:44px;text-align:right;color:var(--ac);font-variant-numeric:tabular-nums}
input[type=range]{width:170px;accent-color:var(--ac)}
select,input[type=text],input[type=time],input[type=number]{background:var(--card2);border:1px solid var(--line);color:var(--tx);border-radius:6px;padding:6px 9px;font:inherit}
.btn{background:var(--card2);border:1px solid var(--line);color:var(--tx);border-radius:7px;padding:6px 12px;cursor:pointer;font:inherit}
.btn:hover{border-color:var(--ac)}
.btn.pri{background:var(--ac);border-color:var(--ac);color:#fff}
.btn.danger{color:var(--bad)}
.btn:disabled{opacity:.45;cursor:default}
.acts{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:8px}
.dpad{display:grid;grid-template-columns:repeat(3,52px);gap:6px;justify-content:center;padding:10px 0}
.dpad .btn{height:44px;font-size:16px}
.ro{color:var(--tx2);font-variant-numeric:tabular-nums}
.schrow{border:1px solid var(--line);border-radius:8px;background:var(--card2);padding:9px 11px;margin-bottom:8px}
.schrow .l1{display:flex;align-items:center;gap:9px}
.schrow .t{font-weight:700;font-size:15px;font-variant-numeric:tabular-nums}
.schrow .cam{font-weight:600;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.schrow .l2{display:flex;align-items:center;gap:8px;margin-top:7px;font-size:12px;color:var(--tx2)}
.schrow .l2 .sp{flex:1}
.tag{font-size:11px;padding:2px 8px;border-radius:10px;background:var(--card2);border:1px solid var(--line)}
.tag.ok{color:var(--ok)}.tag.bad{color:var(--bad)}.tag.dis{color:var(--tx2)}
.form{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:10px}
.toast{position:fixed;bottom:20px;left:50%;transform:translateX(-50%);background:var(--card2);border:1px solid var(--line);padding:9px 16px;border-radius:8px;opacity:0;transition:.25s;pointer-events:none;max-width:80vw}
.toast.show{opacity:1}
.toast.err{border-color:var(--bad);color:var(--bad)}
.muted{color:var(--tx2);font-size:12px}
.urlrow{display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px dashed var(--line)}
.urlrow:last-child{border:none}
.urlrow .lbl{color:var(--tx)}
.urlrow .sub{color:var(--tx2);font-size:12px;flex:1}
.urlrow .btn{flex:none}
h2{font-size:15px;margin:4px 0 10px}
#schedulecard input[type=time]{width:96px}
</style>
</head>
<body>
<div class="wrap">
<h1><span class="dot" id="milocoDot"></span>摄像头控制台 <span class="muted" id="connState"></span></h1>
<div class="layout">
  <div>
    <div class="card"><div class="camlist" id="camlist">加载中…</div></div>
    <div class="card" id="schedulecard">
      <h2>定时任务</h2>
      <div id="schlist"><span class="muted">加载中…</span></div>
      <div class="form">
        <input type="time" id="schTime">
        <select id="schCam"></select>
        <select id="schAct"><option value="off">关闭</option><option value="on">开启</option></select>
        <button class="btn pri" id="schAdd">添加</button>
      </div>
      <div class="muted" style="margin-top:8px">每天执行；错过的时间点在服务启动后自动补跑</div>
    </div>
  </div>
  <div>
    <div class="card">
      <h2 id="pvTitle">实时画面</h2>
      <video id="player" controls autoplay muted playsinline></video>
      <div class="preview-hint" id="pvHint"></div>
      <div id="pvRetry" style="margin-top:8px;display:none">
        <button class="btn" id="pvRetryBtn">重新尝试</button>
      </div>
      <h2 style="margin-top:14px">流地址</h2>
      <div id="urls"></div>
      <div class="muted" style="margin-top:6px">有观看时自动开播，无人观看 1 分钟后自动停止</div>
    </div>
    <div id="panels"></div>
  </div>
</div>
</div>
<div class="toast" id="toast"></div>
<script>
const $=s=>document.querySelector(s);
const DANGEROUS=/(重启|格式化|恢复出厂|删除|清空)/;
let DID=null, SPEC=null, PROPS={}, POLL=null, HLS=null;

function toast(msg, err){
  const t=$("#toast"); t.textContent=msg; t.className="toast show"+(err?" err":"");
  clearTimeout(t._h); t._h=setTimeout(()=>t.className="toast",2200);
}
async function api(path, opts){
  const r=await fetch(path, opts?{headers:{"Content-Type":"application/json"},...opts}:undefined);
  const j=await r.json().catch(()=>({code:-1,message:"响应解析失败"}));
  if(r.status>=400||j.code&&j.code!==0) throw new Error(j.message||("HTTP "+r.status));
  return j;
}
const ctrl=(did,body)=>api(`/api/devices/${did}/control`,{method:"POST",body:JSON.stringify(body)});
// miloco 控制响应：set_property → data.results[]，call_action → data.result{}
function ctrlResult(j){
  const d=(j&&j.data)||{};
  if(Array.isArray(d.results)) return d.results[0]||{};
  if(d.result) return d.result;
  if(Array.isArray(j&&j.results)) return j.results[0]||{};
  return j||{};
}
const esc=s=>String(s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

async function loadCameras(){
  const j=await api("/api/cameras");
  const cams=j.data||j;
  $("#milocoDot").classList.add("up"); $("#connState").textContent="miloco 已连接";
  const list=$("#camlist"); list.innerHTML="";
  const sel=$("#schCam"); sel.innerHTML="";
  for(const c of cams){
    const d=document.createElement("div");
    d.className="cam"+(c.did===DID?" sel":"");
    // 单一状态标识：离线 < 在线但画面不可用 < 在线可用
    const st=!c.online
      ?'<span class="off">○ 离线</span>'
      :(c.lan_reachable===false?'<span class="tag bad">画面不可用</span>':'<span class="on">● 在线</span>');
    d.innerHTML=`<div class="nm" title="${esc(c.name)}">${esc(c.name)}</div>`
      +`<div class="sub"><span>${esc(c.model||"")}</span>${st}</div>`;
    d.onclick=()=>selectCam(c.did);
    list.appendChild(d);
    const o=document.createElement("option"); o.value=c.did; o.textContent=c.name; sel.appendChild(o);
  }
  return cams;
}

/* ---- 预览 ---- */
function renderUrls(did){
  const base=location.protocol+"//"+location.hostname+":18180";
  const rows=[
    ["音视频（HLS）",`${base}/cam/${did}/index.m3u8`],
    ["纯音频（HLS）",`${base}/audio/${did}/index.m3u8`],
    ["音频（Opus，供语音识别）",`${base}/audio/${did}/stream.ogg`],
  ];
  const box=$("#urls"); box.innerHTML="";
  for(const [label,url] of rows){
    const d=document.createElement("div"); d.className="urlrow";
    const l=document.createElement("div");
    l.innerHTML=`<div class="lbl" style="font-weight:600">${label}</div><div class="sub" style="word-break:break-all">${esc(url)}</div>`;
    const b=document.createElement("button"); b.className="btn"; b.textContent="复制";
    b.onclick=async()=>{
      try{ await navigator.clipboard.writeText(url); toast("已复制"); }
      catch(e){
        const i=document.createElement("input"); i.value=url; document.body.appendChild(i);
        i.select(); document.execCommand("copy"); i.remove(); toast("已复制");
      }
    };
    d.append(l,b); box.appendChild(d);
  }
}
function startPreview(did, name){
  if(HLS){HLS.destroy();HLS=null;}
  const v=$("#player"), hint=$("#pvHint");
  $("#pvTitle").textContent="实时画面 · "+(name||did);
  $("#pvRetry").style.display="none";
  $("#pvRetryBtn").onclick=()=>startPreview(did,name);
  const base=location.protocol+"//"+location.hostname+":18180";
  const url=`${base}/cam/${did}/index.m3u8`;
  hint.textContent="";
  renderUrls(did);
  if(!window.Hls||!Hls.isSupported()){ v.src=url; return; }
  HLS=new Hls({
    liveDurationInfinity:true,
    manifestLoadingTimeOut:10000, manifestLoadingMaxRetry:3, manifestRetryPeriod:2000,
    fragLoadingTimeOut:25000,
  });
  HLS.loadSource(url); HLS.attachMedia(v);
  HLS.on(Hls.Events.ERROR,(e,data)=>{
    if(!data.fatal) return;
    // 重试耗尽：停止转圈，给出明确提示
    if(HLS){HLS.destroy();HLS=null;}
    v.removeAttribute("src"); v.load();
    hint.textContent="拿不到视频流：摄像头没有在推流（miloco 感知可能已关闭，或设备已离线）。控制功能不受影响。";
    $("#pvRetry").style.display="block";
  });
}

/* ---- 属性渲染 ---- */
function propMeta(iid){return (SPEC.properties||{})[iid]||{};}
function fmtVal(v){
  if(v===null||v===undefined) return "—";
  if(typeof v==="boolean") return v?"开":"关";
  return String(v);
}
function propRow(iid){
  const m=propMeta(iid), desc=m.description||iid;
  const name=desc.replace((m.service_description||"")+" ","");
  const val=PROPS[iid];
  const row=document.createElement("div"); row.className="row"; row.dataset.iid=iid;
  const left=document.createElement("div");
  left.innerHTML=`<div>${esc(name)}</div><div class="sub">${esc(iid)} · ${esc(m.format||"")}${m.writeable?"":" · 只读"}</div>`;
  row.appendChild(left);
  const right=document.createElement("div"); right.className="val";
  if(!val||val.code!==0){ const s=document.createElement("span"); s.className="ro"; s.textContent="不可读"; right.appendChild(s); }
  else if(m.format==="bool"&&m.writeable){
    const lab=document.createElement("label"); lab.className="sw";
    lab.innerHTML=`<input type="checkbox" ${val.value?"checked":""}><i></i>`;
    lab.querySelector("input").onchange=e=>setProp(iid,e.target.checked);
    right.appendChild(lab);
  } else if((m.format==="int"||m.format==="float")&&m.writeable&&m["value-range"]){
    const [mn,mx,st]=m["value-range"];
    const inp=document.createElement("input"); inp.type="range"; inp.min=mn; inp.max=mx; inp.step=st; inp.value=val.value;
    const out=document.createElement("output"); out.textContent=fmtVal(val.value);
    inp.onchange=()=>setProp(iid,parseFloat(inp.value));
    inp.oninput=()=>out.textContent=inp.value;
    right.append(inp,out);
  } else if(m.writeable&&m["value-list"]){
    const sel=document.createElement("select");
    for(const it of m["value-list"]){
      const o=document.createElement("option"); o.value=it.value; o.textContent=it.name||it.value;
      if(it.value===val.value)o.selected=true; sel.appendChild(o);
    }
    sel.onchange=()=>setProp(iid,parseFloat(sel.value));
    right.appendChild(sel);
  } else if(m.writeable&&(m.format==="string"||m.format==="int"||m.format==="float")){
    const inp=document.createElement("input"); inp.type=m.format==="string"?"text":"number"; inp.value=val.value; inp.style.width="120px";
    const b=document.createElement("button"); b.className="btn"; b.textContent="保存";
    b.onclick=()=>setProp(iid,m.format==="string"?inp.value:parseFloat(inp.value));
    right.append(inp,b);
  } else {
    const s=document.createElement("span"); s.className="ro"; s.textContent=fmtVal(val.value);
    right.appendChild(s);
  }
  row.appendChild(right);
  // 曾经设置被设备拒绝过的属性：置灰并标注「设备不支持」，控件禁用
  if(localStorage.getItem(deadKey(iid))){
    row.style.opacity=".55";
    const t=document.createElement("span"); t.className="tag dis"; t.textContent="设备不支持";
    right.prepend(t);
    right.querySelectorAll("input,select,button").forEach(e=>e.disabled=true);
  }
  return row;
}
async function setProp(iid,value){
  try{
    const j=await ctrl(DID,{type:"set_property",iid,value});
    const r=ctrlResult(j);
    if(r.code===0){ PROPS[iid]={code:0,value}; toast("已设置 "+iid); }
    else{
      localStorage.setItem(deadKey(iid),"1");  // 记住设备端拒绝的项，UI 置灰
      toast("设备拒绝了该设置（不支持）",true); refreshProps();
    }
  }catch(e){ toast(e.message,true); refreshProps(); }
}

/* ---- 动作渲染 ---- */
function isDir(a){return /向(上|下|左|右)/.test(a.description||"");}
function dirName(a){const m=(a.description||"").match(/向(上|下|左|右)/);return {"上":"▲","下":"▼","左":"◀","右":"▶"}[m[1]];}
async function doAction(iid,desc){
  if(DANGEROUS.test(desc)&&!confirm(`确认执行「${desc}」？`)) return;
  try{
    const j=await ctrl(DID,{type:"call_action",iid});
    const r=ctrlResult(j);
    r.code===0?toast("已执行: "+desc):toast("执行失败: "+(r.message||("code "+r.code)),true);
  }catch(e){toast(e.message,true);}
}
function actionBtns(list){
  const wrap=document.createElement("div");
  const dirs=list.filter(isDir), rest=list.filter(a=>!isDir(a));
  if(dirs.length){
    const pad=document.createElement("div"); pad.className="dpad";
    const cell=d=>{const b=document.createElement("button");b.className="btn";b.textContent=d;b.onclick=()=>doAction(dirs.find(x=>dirName(x)===d).iid,dirs.find(x=>dirName(x)===d).description);return b;};
    pad.append(document.createElement("span"),cell("▲"),document.createElement("span"),cell("◀"),document.createElement("span"),cell("▶"),document.createElement("span"),cell("▼"),document.createElement("span"));
    const note=document.createElement("div"); note.className="muted"; note.style.textAlign="center"; note.textContent="云台方向";
    wrap.append(pad,note);
  }
  const grid=document.createElement("div"); grid.className="acts";
  for(const a of rest){
    const b=document.createElement("button"); b.className="btn"+(DANGEROUS.test(a.description)?" danger":"");
    b.textContent=(a.description||a.iid).replace((a.service_description||"")+" ","");
    b.onclick=()=>doAction(a.iid,a.description||a.iid);
    grid.appendChild(b);
  }
  if(rest.length) wrap.appendChild(grid);
  return wrap;
}

/* ---- 面板组装 ---- */
function deadKey(iid){return "cam-dead:"+DID+":"+iid;}
function isCapFlag(m){
  // 只读布尔「能力位」（云台控制/xxx状态），纯内部标志不可操作
  return !m.writeable && m.format==="bool" && /(控制|状态)$/.test(m.description||"");
}
function renderPanels(){
  const root=$("#panels"); root.innerHTML="";
  if(!SPEC) return;
  const svcs={};
  for(const [iid,m] of Object.entries(SPEC.properties||{})){
    const sid=iid.split(".")[1];
    (svcs[sid]=svcs[sid]||{name:m.service_description||("服务"+sid),props:[],acts:[]});
    svcs[sid].props.push(iid);
  }
  for(const [iid,m] of Object.entries(SPEC.actions||{})){
    const sid=iid.split(".")[1];
    (svcs[sid]=svcs[sid]||{name:m.service_description||("服务"+sid),props:[],acts:[]});
    svcs[sid].acts.push({...m,iid});
  }
  const keys=Object.keys(svcs).sort((a,b)=>a-b);
  let shown=0;
  for(const sid of keys){
    const s=svcs[sid];
    const rows=[];
    for(const iid of s.props){
      const m=propMeta(iid), val=PROPS[iid];
      const readable=!!(val&&val.code===0);
      if(!m.writeable&&!readable) continue;            // 不可读也不可写：无意义
      if(isCapFlag(m)) continue;                        // 能力位：不可操作
      rows.push(iid);
    }
    if(!rows.length&&!s.acts.length) continue;          // 整个服务无有效项则不渲染
    shown++;
    const det=document.createElement("details"); det.className="svc";
    if(sid==="2") det.open=true; // 摄像机控制默认展开
    det.innerHTML=`<summary>${esc(s.name)}<span class="cnt">${rows.length+s.acts.length} 项</span></summary>`;
    const body=document.createElement("div"); body.className="body";
    for(const iid of rows) body.appendChild(propRow(iid));
    if(s.acts.length) body.appendChild(actionBtns(s.acts));
    det.appendChild(body); root.appendChild(det);
  }
  if(!shown) root.innerHTML='<div class="card muted">该设备无可用能力</div>';
}

/* ---- 属性轮询 ---- */
async function refreshProps(){
  if(!DID) return;
  try{
    const j=await api(`/api/devices/${DID}/props`);
    const props=(j.data&&j.data.properties)||[];
    for(const p of props) PROPS[p.iid]=p;
    // 只更新值显示，不重建控件（避免打断操作）
    document.querySelectorAll(".row[data-iid]").forEach(row=>{
      const iid=row.dataset.iid, p=PROPS[iid], m=propMeta(iid);
      if(!p||p.code!==0) return;
      if(m.format==="bool"&&!m.writeable){ const s=row.querySelector(".ro"); if(s) s.textContent=fmtVal(p.value); }
      else if(!(m.format==="bool"&&m.writeable)&&!(m.writeable&&m["value-range"])&&!(m.writeable&&m["value-list"])){
        const s=row.querySelector(".ro"); if(s) s.textContent=fmtVal(p.value);
      }
    });
  }catch(e){}
}
function restartPoll(){
  clearInterval(POLL);
  POLL=setInterval(()=>{if(!document.hidden)refreshProps();},10000);
}

/* ---- 定时任务 UI ---- */
async function loadSchedules(){
  const j=await api("/api/schedules");
  const items=j.data||[];
  const cams=await (await fetch("/api/cameras")).json();
  const cmap={}; for(const c of (cams.data||cams)) cmap[c.did]=c.name;
  const box=$("#schlist"); box.innerHTML=items.length?"":"<span class='muted'>暂无任务</span>";
  for(const s of items){
    const d=document.createElement("div"); d.className="schrow";
    // 第一行：开关 + 时间 + 摄像头 + 删除
    const l1=document.createElement("div"); l1.className="l1";
    const sw=document.createElement("label"); sw.className="sw"; sw.title=s.enabled?"点击停用":"点击启用";
    sw.innerHTML=`<input type="checkbox" ${s.enabled?"checked":""}><i></i>`;
    sw.querySelector("input").onchange=async e=>{
      e.target.disabled=true;
      try{
        await api(`/api/schedules/${s.id}`,{method:"PATCH",body:JSON.stringify({enabled:e.target.checked})});
        loadSchedules();
      }catch(err){ toast(err.message,true); e.target.disabled=false; }
    };
    const t=document.createElement("span"); t.className="t"; t.textContent=s.time;
    const cam=document.createElement("span"); cam.className="cam"; cam.textContent=cmap[s.did]||s.did;
    const del=document.createElement("button"); del.className="btn danger"; del.textContent="删除";
    del.onclick=async()=>{if(!confirm(`删除 ${s.time} ${cmap[s.did]||s.did} 的定时任务？`))return;await api(`/api/schedules/${s.id}`,{method:"DELETE"});loadSchedules();};
    l1.append(sw,t,cam,del);
    // 第二行：动作 + 状态 + 试运行
    const l2=document.createElement("div"); l2.className="l2";
    const act=s.action==="on"?"开启":s.action==="off"?"关闭":`${s.iid} = ${s.value}`;
    let tag;
    if(!s.enabled) tag='<span class="tag dis">已停用</span>';
    else if(s.last_ok===false) tag='<span class="tag bad">上次失败</span>';
    else if(s.last_run) tag='<span class="tag ok">已执行</span>';
    else tag='<span class="tag">到点执行</span>';
    const run=document.createElement("button"); run.className="btn"; run.textContent="执行";
    run.disabled=!s.enabled; run.title=s.enabled?"马上执行一次，不影响每天定时":"已停用，先打开上面的开关";
    run.onclick=async()=>{try{await api(`/api/schedules/${s.id}/run`,{method:"POST"});toast("已执行");loadSchedules();}catch(e){toast(e.message,true);}};
    l2.innerHTML=`<span>${act}</span>${tag}<span class="sp"></span>`;
    l2.appendChild(run);
    d.append(l1,l2); box.appendChild(d);
  }
}
$("#schAdd").onclick=async()=>{
  const time=$("#schTime").value; if(!time){toast("请选择时间",true);return;}
  await api("/api/schedules",{method:"POST",body:JSON.stringify({did:$("#schCam").value,time,action:$("#schAct").value})});
  toast("已添加"); loadSchedules();
};

/* ---- 启动 ---- */
async function selectCam(did){
  DID=did; PROPS={};
  document.querySelectorAll(".cam").forEach(e=>e.classList.remove("sel"));
  const j=await api(`/api/devices/${did}/spec`);
  const flat=j.data&&j.data.spec||j.spec||j;
  // API 返回扁平 dict（prop.x.y / action.x.y），归一化成 properties/actions
  SPEC={properties:{},actions:{}};
  for(const [k,v] of Object.entries(flat||{})){
    if(k.startsWith("prop.")) SPEC.properties[k]=v;
    else if(k.startsWith("action.")) SPEC.actions[k]=v;
  }
  const cams=await loadCameras();
  const cam=cams.find(c=>c.did===did);
  document.querySelectorAll(".cam").forEach((e,i)=>{if(cams[i]&&cams[i].did===did)e.classList.add("sel");});
  startPreview(did,cam&&cam.name);
  await refreshProps();  // 先取属性值，面板渲染时才知道可读状态
  renderPanels();
  restartPoll();
}
(async()=>{
  try{
    const cams=await loadCameras();
    // 默认选第一台「在线且 LAN 可达」的，避免落在无法出流的设备上
    const ok=cams.find(c=>c.online&&c.lan_reachable!==false&&c.lan_reachable!==null)||cams[0];
    if(ok) await selectCam(ok.did);
    await loadSchedules();
  }catch(e){
    $("#connState").textContent="miloco 连接失败";
    $("#camlist").innerHTML=`<div class="muted">${esc(e.message)}<br>请确认 miloco 正在运行</div>`;
  }
})();
</script>
</body>
</html>"""


def main() -> None:
    app = web.Application(middlewares=[auth_mw])
    app.router.add_get("/", h_index)
    app.router.add_get("/login", h_login_get)
    app.router.add_post("/login", h_login_post)
    app.router.add_get("/api/cameras", h_cameras)
    app.router.add_get("/api/devices/{did}/spec", h_spec)
    app.router.add_get("/api/devices/{did}/props", h_props)
    app.router.add_post("/api/devices/{did}/control", h_control)
    app.router.add_get("/api/schedules", h_sched_list)
    app.router.add_post("/api/schedules", h_sched_add)
    app.router.add_patch("/api/schedules/{id}", h_sched_patch)
    app.router.add_delete("/api/schedules/{id}", h_sched_del)
    app.router.add_post("/api/schedules/{id}/run", h_sched_run)

    async def on_start(app: web.Application) -> None:
        app["client"] = ClientSession()
        app["sched"] = asyncio.create_task(scheduler_loop(app))

    app.on_startup.append(on_start)

    async def on_clean(app: web.Application) -> None:
        app["sched"].cancel()
        await app["client"].close()

    app.on_cleanup.append(on_clean)
    print(f"控制台: http://{HOST}:{PORT}/  (miloco: {MILOCO_BASE})", flush=True)
    web.run_app(app, host=HOST, port=PORT, print=None)


if __name__ == "__main__":
    main()
