"""B站直播弹幕实时展示 - FastAPI 后端"""
from __future__ import annotations

import asyncio
import hashlib
import json
import struct
import time
import urllib.parse
from contextlib import asynccontextmanager
from functools import reduce
from pathlib import Path
from typing import Any

import brotli
import httpx
import websockets
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

WBI_MIXIN = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]


def wbi_sign(params: dict, img_key: str, sub_key: str) -> dict:
    mixin = reduce(lambda s, i: s + (img_key + sub_key)[i], WBI_MIXIN, "")[:32]
    params = {**params, "wts": int(time.time())}
    query = "&".join(
        f"{k}={urllib.parse.quote(str(v), safe='')}"
        for k, v in sorted(params.items())
    )
    params["w_rid"] = hashlib.md5((query + mixin).encode()).hexdigest()
    return params


async def get_wbi_keys(client: httpx.AsyncClient) -> tuple[str, str]:
    r = await client.get("https://api.bilibili.com/x/web-interface/nav")
    j = r.json()
    img = j["data"]["wbi_img"]["img_url"].rsplit("/", 1)[1].split(".")[0]
    sub = j["data"]["wbi_img"]["sub_url"].rsplit("/", 1)[1].split(".")[0]
    return img, sub


async def init_buvid(client: httpx.AsyncClient) -> None:
    spi = (await client.get("https://api.bilibili.com/x/frontend/finger/spi")).json()
    client.cookies.set("buvid3", spi["data"]["b_3"], domain=".bilibili.com")
    client.cookies.set("buvid4", spi["data"]["b_4"], domain=".bilibili.com")

ROOT = Path(__file__).parent
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
ROOM_ID: int = int(CONFIG["room_id"])
PORT: int = int(CONFIG.get("port", 22333))

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

# ---- websocket 客户端集合 ----
clients: set[WebSocket] = set()
avatar_cache: dict[int, str] = {}
emoji_map: dict[str, str] = {}  # "[花]" -> "https://..."


async def fetch_avatar(client: httpx.AsyncClient, uid: int) -> str:
    if uid in avatar_cache:
        return avatar_cache[uid]
    url = f"https://api.live.bilibili.com/live_user/v1/Master/info?uid={uid}"
    try:
        r = await client.get(url, timeout=5)
        face = r.json().get("data", {}).get("info", {}).get("face", "")
    except Exception:
        face = ""
    avatar_cache[uid] = face
    return face


async def fetch_emoji_map(http: httpx.AsyncClient, room_id: int) -> dict[str, str]:
    url = "https://api.live.bilibili.com/xlive/web-ucenter/v2/emoticon/GetEmoticons"
    try:
        r = await http.get(url, params={"room_id": room_id})
        data = r.json()
        result: dict[str, str] = {}
        for pkg in data.get("data", []):
            for em in pkg.get("emoticons", []):
                result[em["name"]] = em["url"]
        return result
    except Exception:
        return {}


# ---- B站直播 WS 协议 ----
def encode_packet(op: int, body: bytes) -> bytes:
    header = struct.pack(">IHHII", 16 + len(body), 16, 1, op, 1)
    return header + body


def parse_packets(data: bytes) -> list[tuple[int, bytes]]:
    """返回 [(op, body), ...]，处理压缩。"""
    out: list[tuple[int, bytes]] = []
    offset = 0
    while offset < len(data):
        if offset + 16 > len(data):
            break
        pkt_len, hdr_len, ver, op, _seq = struct.unpack(
            ">IHHII", data[offset : offset + 16]
        )
        body = data[offset + hdr_len : offset + pkt_len]
        if ver == 3:  # brotli 压缩，内含多个子包
            decompressed = brotli.decompress(body)
            out.extend(parse_packets(decompressed))
        elif ver == 2:  # zlib（旧版）
            import zlib
            out.extend(parse_packets(zlib.decompress(body)))
        else:
            out.append((op, body))
        offset += pkt_len
    return out


async def broadcast(msg: dict[str, Any]) -> None:
    if not clients:
        return
    payload = json.dumps(msg, ensure_ascii=False)
    dead = []
    for ws in list(clients):
        try:
            await ws.send_text(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        clients.discard(ws)


async def handle_cmd(cmd_data: dict, http: httpx.AsyncClient) -> None:
    cmd = cmd_data.get("cmd", "")
    if "DANMU" in cmd or "DM" in cmd:
        print(f"[DANMU?] cmd={cmd} raw={json.dumps(cmd_data, ensure_ascii=False)}")
    if not cmd.startswith("DANMU_MSG"):
        return
    info = cmd_data.get("info", [])
    if len(info) < 3:
        return
    text = info[1] if isinstance(info[1], str) else ""
    user = info[2]
    uid = int(user[0]) if user else 0
    uname = user[1] if len(user) > 1 else ""

    # 表情包 dm_type=1 → info[0][13] 有 emoticon 对象
    emoticon_url = ""
    try:
        em = info[0][13]
        if isinstance(em, dict) and "url" in em:
            emoticon_url = em["url"]
    except Exception:
        pass

    # 标准 emoji（text 形如 [花]）→ 从 info[0][15].extra.emots 中提取 URL
    if not emoticon_url:
        try:
            extra = info[0][15]
            if isinstance(extra, dict) and "extra" in extra:
                extra_obj = json.loads(extra["extra"])
                emots = extra_obj.get("emots", {})
                if text in emots:
                    emoticon_url = emots[text].get("url", "")
        except Exception:
            pass

    # 尝试从消息体里直接拿头像（新协议）
    face = ""
    try:
        face = info[0][15]["user"]["base"]["face"]
    except Exception:
        pass
    if not face and uid:
        face = await fetch_avatar(http, uid)

    await broadcast({
        "uid": uid,
        "uname": uname,
        "face": face,
        "text": text,
        "emoticon_url": emoticon_url,
    })


async def heartbeat(ws) -> None:
    while True:
        await asyncio.sleep(30)
        await ws.send(encode_packet(2, b""))


async def danmu_worker() -> None:
    """连接B站直播弹幕服务器，永久重连。"""
    headers = {"User-Agent": UA}
    async with httpx.AsyncClient(headers=headers) as http:
        await init_buvid(http)
        img_key, sub_key = await get_wbi_keys(http)

        # 启动时拉取 emoji 映射表
        emoji_map.update(await fetch_emoji_map(http, ROOM_ID))

        while True:
            try:
                # 1) 取真实 room_id
                r = await http.get(
                    "https://api.live.bilibili.com/room/v1/Room/room_init",
                    params={"id": ROOM_ID},
                )
                real_room = r.json()["data"]["room_id"]

                # 2) 取 wss host + token (wbi 签名)
                params = wbi_sign({"id": real_room, "type": 0}, img_key, sub_key)
                r = await http.get(
                    "https://api.live.bilibili.com/xlive/web-room/v1/index/getDanmuInfo",
                    params=params,
                )
                resp = r.json()
                if resp.get("code") != 0:
                    raise RuntimeError(f"getDanmuInfo failed: {resp}")
                d = resp["data"]
                host = d["host_list"][0]
                token = d["token"]

                uri = f"wss://{host['host']}:{host['wss_port']}/sub"
                print(f"[danmu] 连接 {uri} (room={real_room})")

                async with websockets.connect(uri, max_size=None, ping_interval=None) as ws:
                    buvid = http.cookies.get("buvid3", "") or ""
                    auth = json.dumps({
                        "uid": 0,
                        "roomid": real_room,
                        "protover": 3,
                        "buvid": buvid,
                        "platform": "web",
                        "type": 2,
                        "key": token,
                    }).encode()
                    await ws.send(encode_packet(7, auth))

                    hb_task = asyncio.create_task(heartbeat(ws))
                    try:
                        async for raw in ws:
                            if isinstance(raw, str):
                                continue
                            for op, body in parse_packets(raw):
                                if op == 5:  # CMD
                                    try:
                                        cmd_data = json.loads(body.decode("utf-8"))
                                    except Exception:
                                        continue
                                    await handle_cmd(cmd_data, http)
                                elif op == 8:
                                    print("[danmu] 认证成功")
                    finally:
                        hb_task.cancel()
            except Exception as e:
                print(f"[danmu] 连接异常: {e}, 5秒后重连")
                await asyncio.sleep(5)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(danmu_worker())
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan)


INDEX_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="referrer" content="no-referrer" />
<title>B站弹幕 - 房间 {room_id}</title>
<style>
  body {{ font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
          margin: 0; background: #1e1e22; color: #eee;
          height: 100vh; display: flex; flex-direction: column; }}
  header {{ padding: 10px 16px; background: #2a2a30; border-bottom: 1px solid #444;
            flex-shrink: 0; }}
  #status {{ font-size: 13px; color: #8aa; }}
  #pause-tip {{ font-size: 12px; color: #fc6; margin-left: 8px; display: none; }}
  #scroll {{ flex: 1; overflow-y: auto; scrollbar-width: none; }}
  #scroll::-webkit-scrollbar {{ display: none; }}
  ul {{ list-style: none; margin: 0; padding: 8px 12px; }}
  li {{ display: flex; align-items: center; padding: 6px 4px;
        border-bottom: 1px solid #333; }}
  img.avatar {{ width: 32px; height: 32px; border-radius: 50%;
                margin-right: 10px; background: #444; flex-shrink: 0; }}
  .uname {{ color: #7cd; font-weight: 600; margin-right: 8px; flex-shrink: 0; }}
  .text {{ word-break: break-all; }}
  .emoticon {{ max-height: 80px; vertical-align: middle; }}
</style>
</head>
<body>
<header>B站直播弹幕 · 房间 <b>{room_id}</b> · <span id="status">连接中…</span><span id="pause-tip">已暂停滚动（滚到底部恢复）</span></header>
<div id="scroll"><ul id="list"></ul></div>
<script>
  const scroll = document.getElementById('scroll');
  const list = document.getElementById('list');
  const status = document.getElementById('status');
  const pauseTip = document.getElementById('pause-tip');
  const FALLBACK = 'data:image/svg+xml;utf8,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 32 32%22><rect width=%2232%22 height=%2232%22 fill=%22%23555%22/></svg>';

  let autoScroll = true;
  scroll.addEventListener('scroll', () => {{
    const atBottom = scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 30;
    autoScroll = atBottom;
    pauseTip.style.display = atBottom ? 'none' : 'inline';
  }});

  function stickToBottom() {{
    if (autoScroll) scroll.scrollTop = scroll.scrollHeight;
  }}

  function connect() {{
    const ws = new WebSocket(`ws://${{location.host}}/ws`);
    ws.onopen = () => status.textContent = '已连接';
    ws.onclose = () => {{ status.textContent = '断线，3秒后重连'; setTimeout(connect, 3000); }};
    ws.onerror = () => status.textContent = '错误';
    ws.onmessage = (ev) => {{
      const m = JSON.parse(ev.data);
      const li = document.createElement('li');
      const img = document.createElement('img');
      img.className = 'avatar';
      img.src = m.face || FALLBACK;
      img.onerror = () => {{ img.src = FALLBACK; }};
      img.onload = stickToBottom;  // 头像加载后再次贴底
      const u = document.createElement('span');
      u.className = 'uname'; u.textContent = m.uname + '：';
      const t = document.createElement('span');
      t.className = 'text';
      if (m.emoticon_url) {{
        const e = document.createElement('img');
        e.className = 'emoticon';
        e.src = m.emoticon_url;
        e.alt = m.text;
        e.onload = stickToBottom;
        t.appendChild(e);
      }} else {{
        t.textContent = m.text;
      }}
      li.append(img, u, t);
      list.appendChild(li);
      while (list.children.length > 500) list.removeChild(list.firstChild);
      stickToBottom();
    }};
  }}
  connect();
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse(INDEX_HTML.format(room_id=ROOM_ID))


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    clients.add(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        clients.discard(ws)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
