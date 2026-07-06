"""Grok MCP Server — cookie-based Grok Imagine tools."""

import asyncio
import json
import os
import random
import re
import subprocess
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import websockets
from mcp.server import Server
from mcp.types import TextContent, Tool

server = Server("grok-imagine")

GROK_API_BASE = "https://grok.com"
ASSETS_BASE = "https://assets.grok.com"
IMAGINE_PUBLIC_BASE = "https://imagine-public.x.ai/imagine-public"
COOKIE_FILE = Path(__file__).with_name("cookies.json")
COOKIES_DIR = Path(__file__).with_name("cookies")
_ENV_PATH = Path(__file__).with_name(".env")
if _ENV_PATH.exists():
    for _line in _ENV_PATH.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _k, _v = _line.split("=", 1)
        os.environ.setdefault(_k.strip(), _v.strip())
BROWSER_STATE: dict[str, Any] = {"port": None, "ws_url": None}


def find_chrome() -> str | None:
    candidates = [
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
    ]
    for path in candidates:
        if Path(path).exists():
            return path
    return None


CHROME_PROFILES_DIR = Path(__file__).with_name("chrome-profiles")


def chrome_profile_dir(profile_key: str) -> Path:
    CHROME_PROFILES_DIR.mkdir(exist_ok=True)
    safe = re.sub(r"[^a-zA-Z0-9._-]", "_", profile_key)[:60] or "default"
    path = CHROME_PROFILES_DIR / safe
    path.mkdir(exist_ok=True)
    return path


def launch_chrome(debug_port: int, profile_key: str) -> None:
    chrome_path = find_chrome()
    if not chrome_path:
        raise RuntimeError("Chrome not found")
    user_data = str(chrome_profile_dir(profile_key))
    cmd = (
        f'start "" "{chrome_path}"'
        f" --remote-debugging-port={debug_port}"
        f' --user-data-dir="{user_data}"'
        f" --no-first-run --no-default-browser-check --disable-sync"
        f" https://grok.com/imagine"
    )
    subprocess.run(cmd, shell=True)


async def get_cdp_page(debug_port: int) -> dict[str, Any] | None:
    try:
        async with httpx.AsyncClient(timeout=2) as client:
            resp = await client.get(f"http://127.0.0.1:{debug_port}/json")
            resp.raise_for_status()
            pages = resp.json()
            return next((p for p in pages if p.get("type") == "page"), None)
    except Exception:
        return None


def cookie_profile_key(cookies: dict[str, str]) -> str:
    return cookies.get("grok_device_id") or (cookies.get("sso") or "")[:24] or "default"


async def ensure_browser(cookies: dict[str, str]) -> str:
    profile_key = cookie_profile_key(cookies)
    if BROWSER_STATE.get("profile") != profile_key:
        BROWSER_STATE["port"] = None
        BROWSER_STATE["ws_url"] = None
    BROWSER_STATE["profile"] = profile_key

    port = BROWSER_STATE.get("port")
    if port:
        page = await get_cdp_page(port)
        if page and page.get("webSocketDebuggerUrl"):
            BROWSER_STATE["ws_url"] = page["webSocketDebuggerUrl"]
            return page["webSocketDebuggerUrl"]

    debug_port = random.randint(9401, 9499)
    launch_chrome(debug_port, profile_key)
    for _ in range(30):
        await asyncio.sleep(0.5)
        page = await get_cdp_page(debug_port)
        if page and page.get("webSocketDebuggerUrl"):
            ws_url = page["webSocketDebuggerUrl"]
            await inject_browser_cookies(ws_url, cookies)
            BROWSER_STATE.update({"port": debug_port, "ws_url": ws_url})
            return ws_url
    raise RuntimeError(f"Chrome CDP not available on port {debug_port}")


async def cdp_call(ws, msg_id: int, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    await ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
    while True:
        msg = json.loads(await ws.recv())
        if msg.get("id") == msg_id:
            if "error" in msg:
                raise RuntimeError(msg["error"])
            return msg.get("result", {})


STATSIG_SNIFFER_JS = r"""
(() => {
  if (window.__grokSnifferInstalled) return;
  window.__grokSnifferInstalled = true;
  const origFetch = window.fetch;
  window.fetch = function(input, init) {
    try {
      const headers = init && init.headers;
      if (headers) {
        const get = (name) => {
          if (headers instanceof Headers) return headers.get(name);
          if (Array.isArray(headers)) { const h = headers.find(h => (h[0]||'').toLowerCase() === name); return h ? h[1] : null; }
          for (const k of Object.keys(headers)) if (k.toLowerCase() === name) return headers[k];
          return null;
        };
        const s = get('x-statsig-id');
        if (s) window.__grokStatsigId = s;
      }
    } catch(e) {}
    return origFetch.apply(this, arguments);
  };
})();
"""


async def inject_browser_cookies(ws_url: str, cookies: dict[str, str]) -> None:
    async with websockets.connect(ws_url) as ws:
        await cdp_call(ws, 1, "Network.enable")
        await cdp_call(ws, 2, "Page.enable")
        await cdp_call(ws, 3, "Page.addScriptToEvaluateOnNewDocument", {"source": STATSIG_SNIFFER_JS})
        for i, (name, value) in enumerate(cookies.items(), start=10):
            await cdp_call(ws, i, "Network.setCookie", {
                "name": name,
                "value": value,
                "domain": ".grok.com" if name != "__cf_bm" else "grok.com",
                "path": "/",
                "secure": True,
                "httpOnly": name.startswith("__cf"),
                "sameSite": "Lax",
            })
        await cdp_call(ws, 999, "Page.navigate", {"url": "https://grok.com/imagine"})


def js_literal(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False).replace("</", "<\\/")


async def browser_fetch(path: str, cookies: dict[str, str], payload: dict[str, Any], stream: bool = False) -> Any:
    ws_url = await ensure_browser(cookies)
    async with websockets.connect(ws_url, max_size=64 * 1024 * 1024) as ws:
        await cdp_call(ws, 1900, "Runtime.evaluate", {"expression": STATSIG_SNIFFER_JS})
        for wait_id in range(50):
            statsig = await cdp_call(ws, 1950 + wait_id, "Runtime.evaluate", {
                "expression": "window.__grokStatsigId || null",
                "returnByValue": True,
            })
            if statsig.get("result", {}).get("value"):
                break
            if wait_id == 0:
                await cdp_call(ws, 1980, "Runtime.evaluate", {
                    "expression": "fetch('/rest/media/imagine/quota_info', {method:'POST', credentials:'include', headers:{'content-type':'application/json'}, body:'{}'}).catch(()=>null)",
                })
            await asyncio.sleep(0.4)

        js = f"""
async () => {{
  const headers = {{
    'accept': '*/*',
    'content-type': 'application/json',
    'x-xai-request-id': crypto.randomUUID(),
  }};
  if (window.__grokStatsigId) headers['x-statsig-id'] = window.__grokStatsigId;
  const response = await fetch({js_literal(path)}, {{
    method: 'POST',
    credentials: 'include',
    headers,
    referrer: 'https://grok.com/imagine',
    body: JSON.stringify({js_literal(payload)})
  }});
  const text = await response.text();
  return {{ok: response.ok, status: response.status, text, sentHeaders: headers}};
}}
"""
        result = await cdp_call(ws, 2000, "Runtime.evaluate", {
            "expression": f"({js})()",
            "awaitPromise": True,
            "returnByValue": True,
        })
    value = result.get("result", {}).get("value", {})
    if not value.get("ok"):
        raise RuntimeError(f"Browser fetch failed {value.get('status')}: {value.get('text', '')[:1000]}")
    if stream:
        events = []
        for line in value.get("text", "").splitlines():
            item = parse_stream_line(line)
            if item is not None:
                events.append(item)
        return events
    if not value.get("text"):
        return {}
    return json.loads(value["text"])


def parse_cookie_payload(data: Any) -> dict[str, str]:
    if isinstance(data, dict):
        return {normalize_cookie_name(k): str(v) for k, v in data.items() if v}
    if isinstance(data, list):
        return {normalize_cookie_name(c["name"]): c["value"] for c in data if c.get("name") and c.get("value")}
    return {}


def read_cookie_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    return parse_cookie_payload(json.loads(path.read_text(encoding="utf-8")))


def resolve_account_path(account: str) -> Path:
    candidate = COOKIES_DIR / f"{account}.json"
    if not candidate.exists():
        available = list_accounts()
        raise FileNotFoundError(f"cookies/{account}.json not found. Available accounts: {available}")
    return candidate


def list_accounts() -> list[str]:
    if not COOKIES_DIR.exists():
        return []
    return sorted(p.stem for p in COOKIES_DIR.glob("*.json"))


def load_cookie_file(account: str | None = None) -> dict[str, str]:
    if account:
        return read_cookie_file(resolve_account_path(account))
    return read_cookie_file(COOKIE_FILE)


def normalize_cookie_name(name: str) -> str:
    return "sso-rw" if name == "sso_rw" else name


def resolve_cookies(arguments: dict[str, Any]) -> dict[str, str]:
    account = arguments.get("account")
    if account:
        raw = load_cookie_file(account)
    else:
        raw = arguments.get("cookies") or load_cookie_file()
    cookies = {normalize_cookie_name(k): str(v) for k, v in raw.items() if v}
    if not cookies:
        raise ValueError(f"No cookies found. Available accounts: {list_accounts() or 'none'}. Pass account, pass cookies, or create cookies.json next to server.py")
    return cookies


def build_headers(cookies: dict[str, str], referer: str = "https://grok.com/imagine") -> dict[str, str]:
    cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items() if v)
    return {
        "Cookie": cookie_str,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Content-Type": "application/json",
        "Origin": "https://grok.com",
        "Referer": referer,
        "Priority": "u=1, i",
        "Sec-Ch-Ua": '"Chromium";v="143", "Google Chrome";v="143", "Not/A)Brand";v="99"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "x-statsig-id": cookies.get("grok_device_id", str(uuid.uuid4())),
        "x-xai-request-id": str(uuid.uuid4()),
    }


async def grok_request(
    method: str,
    path: str,
    cookies: dict[str, str],
    payload: dict[str, Any] | None = None,
    timeout: float = 60.0,
    referer: str = "https://grok.com/imagine",
) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        resp = await client.request(
            method,
            f"{GROK_API_BASE}{path}",
            headers=build_headers(cookies, referer),
            json=payload,
        )
        resp.raise_for_status()
        if not resp.content:
            return {}
        return resp.json()


async def grok_call(path: str, cookies: dict[str, str], payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Call any Grok REST endpoint through the browser fetch layer (bypasses anti-bot)."""
    return await browser_fetch(path, cookies, payload or {}, stream=False)


async def grok_stream_request(path: str, cookies: dict[str, str], payload: dict[str, Any], video: bool = False) -> list[dict[str, Any]]:
    try:
        return await browser_fetch(path, cookies, payload, stream=True)
    except Exception as browser_error:
        events: list[dict[str, Any]] = []
        headers = build_headers(cookies)
        if video:
            trace_id = uuid.uuid4().hex
            parent_id = uuid.uuid4().hex[:16]
            headers.update({
                "Referer": "https://grok.com/imagine",
                "baggage": f"sentry-environment=production,sentry-release=19b21d09e8a9dd440b9caae1bc973b88d50a73a6,sentry-public_key=b311e0f2690c81f25e2c4cf6d4f7ce1c,sentry-trace_id={trace_id},sentry-org_id=4508179396558848,sentry-transaction=%2Fc%2F%3Aslug*%3F,sentry-sampled=false",
                "sentry-trace": f"{trace_id}-{parent_id}-0",
                "traceparent": f"00-{trace_id}-{parent_id}-00",
            })
        try:
            async with httpx.AsyncClient(timeout=None, follow_redirects=True) as client:
                async with client.stream(
                    "POST",
                    f"{GROK_API_BASE}{path}",
                    headers=headers,
                    json=payload,
                ) as resp:
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        item = parse_stream_line(line)
                        if item is not None:
                            events.append(item)
            return events
        except Exception as http_error:
            raise RuntimeError(f"Browser fetch failed: {browser_error}; direct HTTP failed: {http_error}") from http_error


def parse_stream_line(line: str) -> dict[str, Any] | None:
    line = line.strip()
    if not line or line == "[DONE]":
        return None
    if line.startswith("data:"):
        line = line[5:].strip()
    if not line or line == "[DONE]":
        return None
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return None


def normalize_asset_url(url: str | None) -> str | None:
    if not url:
        return None
    if url.startswith("http://") or url.startswith("https://"):
        return url
    return f"{ASSETS_BASE}/{url.lstrip('/')}"


def deep_values(data: Any):
    if isinstance(data, dict):
        yield data
        for value in data.values():
            yield from deep_values(value)
    elif isinstance(data, list):
        for value in data:
            yield from deep_values(value)


def parse_jsonish(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def extract_image_urls(events: list[dict[str, Any]]) -> list[str]:
    urls: list[str] = []
    for event in events:
        candidates = [event]
        for node in deep_values(event):
            for key in ("cardAttachmentsJson", "cardAttachmentJson"):
                if key in node:
                    parsed = parse_jsonish(node[key])
                    if parsed is not node[key]:
                        candidates.append(parsed)
        for candidate in candidates:
            for node in deep_values(candidate):
                for key in ("imageUrl", "original", "url"):
                    value = node.get(key)
                    if isinstance(value, str) and is_image_url(value):
                        normalized = normalize_asset_url(value)
                        if normalized and "part-0" not in normalized and normalized not in urls:
                            urls.append(normalized)
    return urls


def is_image_url(value: str) -> bool:
    return any(marker in value.lower() for marker in (".png", ".jpg", ".jpeg", ".webp", "/images/"))


def extract_video_state(events: list[dict[str, Any]]) -> dict[str, Any]:
    state: dict[str, Any] = {"progress": 0}
    for event in events:
        for node in deep_values(event):
            video = node.get("streamingVideoGenerationResponse")
            if not isinstance(video, dict):
                continue
            if video.get("moderated"):
                state["moderated"] = True
            for key in (
                "progress",
                "videoPostId",
                "postId",
                "videoId",
                "videoUrl",
                "thumbnailImageUrl",
                "videoPrompt",
                "imageReference",
                "width",
                "height",
                "resolutionName",
            ):
                if video.get(key) is not None:
                    state[key] = video[key]
    if state.get("videoUrl"):
        state["video_url"] = normalize_asset_url(state["videoUrl"])
    if state.get("thumbnailImageUrl"):
        state["thumbnail_url"] = normalize_asset_url(state["thumbnailImageUrl"])
    return state


def extract_conversation_id(data: dict[str, Any]) -> str | None:
    for key in ("conversationId", "conversation_id", "id"):
        if data.get(key):
            return data[key]
    for node in deep_values(data):
        for key in ("conversationId", "conversation_id"):
            if node.get(key):
                return node[key]
    return None


def post_summary(result: dict[str, Any]) -> dict[str, Any]:
    post = result.get("post", result)
    return {
        "post_id": post.get("id"),
        "user_id": post.get("userId"),
        "thumbnail": normalize_asset_url(post.get("thumbnailImageUrl")),
        "post": post,
    }


async def create_media_post(
    cookies: dict[str, str],
    media_type: str | None = None,
    prompt: str | None = None,
    media_url: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if media_type:
        payload["mediaType"] = media_type
    if prompt:
        payload["prompt"] = prompt
    if media_url:
        payload["mediaUrl"] = media_url
    return await grok_request("POST", "/rest/media/post/create", cookies, payload)


def collect_media_urls(post: dict[str, Any]) -> dict[str, list[str]]:
    image_urls: list[str] = []
    video_urls: list[str] = []
    audio_urls: list[str] = []
    for node in deep_values(post):
        for key, bucket in (("thumbnailImageUrl", image_urls), ("mediaUrl", image_urls), ("url", image_urls), ("imageUrl", image_urls), ("original", image_urls)):
            value = node.get(key)
            if isinstance(value, str) and value and is_image_url(value):
                normalized = normalize_asset_url(value)
                if normalized and normalized not in bucket:
                    bucket.append(normalized)
        for key in ("videoUrl", "mediaUrl", "url", "original"):
            value = node.get(key)
            if isinstance(value, str) and value and is_video_url(value):
                normalized = normalize_asset_url(value)
                if normalized and normalized not in video_urls:
                    video_urls.append(normalized)
        for value in node.get("audioUrls", []) or []:
            if isinstance(value, str) and value not in audio_urls:
                audio_urls.append(normalize_asset_url(value) or value)
    return {"image_urls": image_urls, "video_urls": video_urls, "audio_urls": audio_urls}


def is_video_url(value: str) -> bool:
    lower = value.lower()
    return any(marker in lower for marker in (".mp4", ".webm", "/videos/", "generated_video"))


async def get_media_post(cookies: dict[str, str], post_id: str) -> dict[str, Any] | None:
    try:
        result = await grok_request("POST", "/rest/media/post/get", cookies, {"id": post_id}, timeout=30.0)
    except Exception:
        return None
    post = result.get("post", result)
    return post if isinstance(post, dict) else None


async def get_media_posts(cookies: dict[str, str], limit: int = 40, source: str = "MEDIA_POST_SOURCE_OWNED") -> dict[str, Any]:
    return await grok_request(
        "POST",
        "/rest/media/post/list",
        cookies,
        {"limit": limit, "filter": {"source": source, "safeForWork": False}},
        timeout=30.0,
    )


async def find_media_post(cookies: dict[str, str], post_id: str) -> dict[str, Any] | None:
    post = await get_media_post(cookies, post_id)
    if post:
        return post
    for source in ("MEDIA_POST_SOURCE_OWNED", "MEDIA_POST_SOURCE_LIKED"):
        try:
            result = await get_media_posts(cookies, 40, source)
        except Exception:
            continue
        for node in deep_values(result):
            if node.get("id") == post_id:
                return node
    return None


async def create_and_wait_media(
    cookies: dict[str, str],
    media_type: str,
    prompt: str,
    timeout: int = 180,
    poll_interval: int = 5,
) -> dict[str, Any]:
    result = await create_media_post(cookies, media_type=media_type, prompt=prompt)
    summary = post_summary(result)
    post = summary["post"]
    post_id = summary["post_id"]
    urls = collect_media_urls(post)
    wanted_key = "video_urls" if media_type == "MEDIA_POST_TYPE_VIDEO" else "image_urls"
    if media_type == "MEDIA_POST_TYPE_IMAGE" and urls["image_urls"]:
        return {"status": "ready", **summary, **urls}
    if media_type == "MEDIA_POST_TYPE_VIDEO" and urls["video_urls"]:
        return {"status": "ready", **summary, **urls}

    for _ in range(max(timeout // poll_interval, 1)):
        await asyncio.sleep(poll_interval)
        latest = await find_media_post(cookies, post_id) if post_id else None
        if not latest:
            continue
        urls = collect_media_urls(latest)
        if media_type == "MEDIA_POST_TYPE_IMAGE" and urls["image_urls"]:
            return {"status": "ready", **post_summary(latest), **urls}
        if media_type == "MEDIA_POST_TYPE_VIDEO" and urls["video_urls"]:
            return {"status": "ready", **post_summary(latest), **urls}
    return {"status": "timeout", **summary, **urls}


OUTPUT_DIR = Path(__file__).with_name("output")


async def download_asset(cookies: dict[str, str], url: str, dest: Path, chunk: int = 65536) -> Path:
    OUTPUT_DIR.mkdir(exist_ok=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    headers = build_headers(cookies, referer="https://grok.com/imagine")
    async with httpx.AsyncClient(timeout=None, follow_redirects=True) as client:
        async with client.stream("GET", url, headers=headers) as resp:
            resp.raise_for_status()
            with dest.open("wb") as fh:
                async for part in resp.aiter_bytes(chunk):
                    fh.write(part)
    return dest


def guess_extension(url: str, default: str) -> str:
    for suffix in (".mp4", ".webm", ".png", ".jpg", ".jpeg", ".webp", ".gif"):
        if suffix in url.lower():
            return suffix
    return default


async def download_media_assets(cookies: dict[str, str], post_id: str, urls: list[str], kind: str) -> list[Path]:
    saved: list[Path] = []
    for i, url in enumerate(urls):
        suffix = guess_extension(url, ".mp4" if kind == "video" else ".jpg")
        dest = OUTPUT_DIR / f"{post_id}_{kind}_{i}{suffix}"
        saved.append(await download_asset(cookies, url, dest))
    return saved


TELEGRAM_ENV_TOKEN = "GROK_TG_BOT_TOKEN"
TELEGRAM_ENV_CHAT = "GROK_TG_CHAT_ID"


def resolve_telegram(arguments: dict[str, Any]) -> tuple[str, str]:
    token = arguments.get("telegram_bot_token") or os.environ.get(TELEGRAM_ENV_TOKEN)
    chat_id = arguments.get("telegram_chat_id") or os.environ.get(TELEGRAM_ENV_CHAT)
    if not token or not chat_id:
        raise ValueError(f"Telegram not configured. Set env {TELEGRAM_ENV_TOKEN}/{TELEGRAM_ENV_CHAT} or pass telegram_bot_token/telegram_chat_id")
    return token, str(chat_id)


async def telegram_send_media(token: str, chat_id: str, file_path: Path, caption: str | None, media_kind: str) -> dict[str, Any]:
    endpoint = {
        "video": ("sendVideo", "video"),
        "photo": ("sendPhoto", "photo"),
        "document": ("sendDocument", "document"),
    }[media_kind]
    method, field = endpoint
    url = f"https://api.telegram.org/bot{token}/{method}"
    async with httpx.AsyncClient(timeout=None) as client:
        with file_path.open("rb") as fh:
            files = {field: (file_path.name, fh, "video/mp4" if media_kind == "video" else "application/octet-stream")}
            data: dict[str, str] = {"chat_id": chat_id}
            if caption:
                data["caption"] = caption[:1024]
            resp = await client.post(url, data=data, files=files)
        resp.raise_for_status()
        return resp.json()


async def send_generation_to_telegram(
    cookies: dict[str, str],
    result: dict[str, Any],
    kind: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    token, chat_id = resolve_telegram(arguments)
    urls_key = "video_urls" if kind == "video" else "image_urls"
    urls = result.get(urls_key) or []
    if not urls:
        raise ValueError(f"No {urls_key} to send")
    post_id = result.get("post_id") or result.get("video_post_id") or "grok"
    files = await download_media_assets(cookies, post_id, urls, kind)
    caption = arguments.get("telegram_caption") or arguments.get("prompt")
    telegram_kind = "video" if kind == "video" else "photo"
    sent = []
    for path in files:
        info = await telegram_send_media(token, chat_id, path, caption, telegram_kind)
        sent.append({"file": str(path), "response": info})
    return {"sent": sent}


async def list_projects(cookies: dict[str, str]) -> dict[str, Any]:
    return await grok_call("/rest/media/canvas/list", cookies, {})


async def create_project(cookies: dict[str, str], name: str) -> dict[str, Any]:
    return await grok_call("/rest/media/canvas/create", cookies, {"name": name})


async def get_project_conversations(cookies: dict[str, str], project_id: str) -> dict[str, Any]:
    return await grok_call("/rest/media/conversation/get", cookies, {"canvasId": project_id})


async def create_project_node(cookies: dict[str, str], project_id: str, node: dict[str, Any]) -> dict[str, Any]:
    payload = {"canvasId": project_id, **node}
    return await grok_call("/rest/media/canvas/node/create", cookies, payload)


async def set_project_thumbnail(cookies: dict[str, str], project_id: str, post_id: str) -> dict[str, Any]:
    return await grok_call("/rest/media/canvas/set-thumbnail", cookies, {"canvasId": project_id, "postId": post_id})


async def list_templates(cookies: dict[str, str]) -> dict[str, Any]:
    return await grok_call("/rest/media/pipeline/template/list", cookies, {})


async def get_template(cookies: dict[str, str], template_id: str) -> dict[str, Any]:
    return await grok_call("/rest/media/pipeline/template/get", cookies, {"templateId": template_id})


async def list_folders(cookies: dict[str, str]) -> dict[str, Any]:
    return await grok_call("/rest/media/folder/list", cookies, {})


async def get_post_folders(cookies: dict[str, str], post_id: str) -> dict[str, Any]:
    return await grok_call("/rest/media/post/folders", cookies, {"postId": post_id})


async def list_posts(cookies: dict[str, str], limit: int = 40, source: str = "MEDIA_POST_SOURCE_OWNED", safe: bool = False) -> dict[str, Any]:
    # Grok removed the Mongo-backed listing; leave a legacy call for accounts where it still works.
    return await grok_call(
        "/rest/media/post/list",
        cookies,
        {"limit": limit, "filter": {"source": source, "safeForWork": safe}},
    )


async def list_project_posts(cookies: dict[str, str], project_id: str) -> dict[str, Any]:
    return await grok_call("/rest/media/canvas/get", cookies, {"canvasId": project_id})


async def like_post(cookies: dict[str, str], post_id: str) -> dict[str, Any]:
    return await grok_call("/rest/media/post/like", cookies, {"id": post_id})


async def delete_post(cookies: dict[str, str], post_id: str) -> dict[str, Any]:
    return await grok_call("/rest/media/post/delete", cookies, {"id": post_id})


async def search_status(cookies: dict[str, str]) -> dict[str, Any]:
    return await grok_call("/rest/media/search/status", cookies, {})


async def start_agent_conversation(cookies: dict[str, str], project_id: str | None, first_prompt: str, extra: dict[str, Any] | None = None) -> tuple[str | None, list[dict[str, Any]]]:
    init_payload: dict[str, Any] = {"temporary": False}
    if project_id:
        init_payload["canvasId"] = project_id
    created = await browser_fetch("/rest/app-chat/conversations", cookies, init_payload, stream=False)
    cid = created.get("conversationId") if isinstance(created, dict) else None
    if not cid:
        raise RuntimeError(f"Agent conversation init failed: {created}")
    payload: dict[str, Any] = {
        "message": first_prompt,
        "modeId": "imagine-agent-mode-dev",
        "enableImageGeneration": True,
        "enableImageStreaming": True,
    }
    if extra:
        payload.update(extra)
    events = await browser_fetch(f"/rest/app-chat/conversations/{cid}/responses", cookies, payload, stream=True)
    return cid, events


async def send_agent_prompt(cookies: dict[str, str], conversation_id_value: str, message: str, extra: dict[str, Any] | None = None) -> Any:
    payload: dict[str, Any] = {
        "message": message,
        "modeId": "imagine-agent-mode-dev",
        "enableImageGeneration": True,
        "enableImageStreaming": True,
    }
    if extra:
        payload.update(extra)
    return await browser_fetch(f"/rest/app-chat/conversations/{conversation_id_value}/responses", cookies, payload, stream=True)


async def read_agent_conversation(cookies: dict[str, str], conversation_id_value: str) -> dict[str, Any]:
    return await grok_call(f"/rest/app-chat/conversations/{conversation_id_value}/load-responses", cookies, {})


SERIES_DIR = Path(__file__).with_name("series")


async def grok_chat(cookies: dict[str, str], prompt: str, mode_id: str = "fast") -> tuple[str, str | None, list[dict[str, Any]]]:
    created = await browser_fetch("/rest/app-chat/conversations", cookies, {"temporary": True}, stream=False)
    cid = created.get("conversationId") if isinstance(created, dict) else None
    if not cid:
        raise RuntimeError(f"Grok chat init failed: {created}")
    payload = {
        "message": prompt,
        "modeId": mode_id,
        "temporary": True,
        "disableSearch": False,
        "enableImageGeneration": False,
        "enableImageStreaming": False,
        "imageGenerationCount": 0,
        "returnImageBytes": False,
        "returnRawGrokInXaiRequest": False,
        "imageAttachments": [],
        "fileAttachments": [],
        "enableSideBySide": False,
        "sendFinalMetadata": True,
        "responseMetadata": {"experiments": []},
    }
    events = await browser_fetch(f"/rest/app-chat/conversations/{cid}/responses", cookies, payload, stream=True)
    tokens: list[str] = []
    cid: str | None = None
    for event in events:
        if not isinstance(event, dict):
            continue
        response = event.get("result", {}).get("response") if isinstance(event.get("result"), dict) else None
        if isinstance(response, dict):
            token = response.get("token")
            if isinstance(token, str) and not response.get("isThinking") and not response.get("messageStepId"):
                tokens.append(token)
    return "".join(tokens), cid, events


def extract_json_block(text: str) -> Any:
    start = min([i for i in (text.find("["), text.find("{")) if i >= 0], default=-1)
    if start < 0:
        raise ValueError(f"No JSON found in Grok response: {text[:400]}")
    depth = 0
    in_string = False
    escape = False
    opener = text[start]
    closer = "]" if opener == "[" else "}"
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            continue
        if ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return json.loads(text[start : i + 1])
    raise ValueError(f"Unbalanced JSON in Grok response: {text[start:start+400]}")


def series_path(series_id: str) -> Path:
    SERIES_DIR.mkdir(exist_ok=True)
    safe = re.sub(r"[^a-zA-Z0-9._-]", "_", series_id)[:60]
    return SERIES_DIR / f"{safe}.json"


def new_series_id(topic: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", topic.lower()).strip("-")[:32] or "series"
    stamp = uuid.uuid4().hex[:6]
    return f"{slug}-{stamp}"


def load_series(series_id: str) -> dict[str, Any]:
    path = series_path(series_id)
    if not path.exists():
        raise FileNotFoundError(f"Series {series_id} not found")
    return json.loads(path.read_text(encoding="utf-8"))


def save_series(state: dict[str, Any]) -> Path:
    path = series_path(state["id"])
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def list_series_files() -> list[dict[str, Any]]:
    if not SERIES_DIR.exists():
        return []
    items: list[dict[str, Any]] = []
    for path in sorted(SERIES_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            items.append({
                "id": data.get("id"),
                "topic": data.get("topic"),
                "episodes": len(data.get("episodes", [])),
                "scenes": sum(len(ep.get("scenes", [])) for ep in data.get("episodes", [])),
                "updated_at": data.get("updated_at"),
                "path": str(path),
            })
        except Exception as e:
            items.append({"path": str(path), "error": str(e)})
    return items


def build_plan_prompt(topic: str, episodes: int, scenes_per_episode: int, style: str | None, duration: int) -> str:
    style_line = f"Style/notes: {style}\n" if style else ""
    return (
        f"You are the ScriptAgent for a short video series generated with Grok Imagine.\n"
        f"Topic: {topic}\n"
        f"Episodes: {episodes}\n"
        f"Scenes per episode: {scenes_per_episode}\n"
        f"Target scene duration: {duration} seconds\n"
        f"{style_line}"
        f"Return ONLY valid JSON in this shape and nothing else:\n"
        "{\n"
        '  "title": string,\n'
        '  "logline": string,\n'
        '  "consistency": {"characters": [string], "setting": string, "style": string, "palette": string},\n'
        '  "episodes": [\n'
        "    {\n"
        '      "episode_number": number,\n'
        '      "title": string,\n'
        '      "summary": string,\n'
        '      "scenes": [\n'
        "        {\n"
        '          "scene_number": number,\n'
        '          "description": string,\n'
        '          "prompt": string,\n'
        '          "aspect_ratio": "2:3"|"3:2"|"1:1"|"9:16"|"16:9",\n'
        '          "duration": number\n'
        "        }\n"
        "      ]\n"
        "    }\n"
        "  ]\n"
        "}\n"
        "Every scene.prompt must be a self-contained Grok Imagine prompt with the character, action, camera hint, lighting, and style. No commentary outside JSON."
    )


async def plan_series(
    cookies: dict[str, str],
    topic: str,
    episodes: int = 1,
    scenes_per_episode: int = 3,
    style: str | None = None,
    duration: int = 6,
) -> dict[str, Any]:
    prompt = build_plan_prompt(topic, episodes, scenes_per_episode, style, duration)
    text, cid, _ = await grok_chat(cookies, prompt)
    plan = extract_json_block(text)
    if not isinstance(plan, dict):
        raise ValueError(f"Grok plan is not an object: {text[:400]}")
    series_id = new_series_id(topic)
    for episode in plan.get("episodes", []):
        for scene in episode.get("scenes", []):
            scene.setdefault("status", "planned")
    now = _now_iso()
    state = {
        "id": series_id,
        "topic": topic,
        "created_at": now,
        "updated_at": now,
        "planner_conversation_id": cid,
        "settings": {
            "episodes": episodes,
            "scenes_per_episode": scenes_per_episode,
            "duration": duration,
            "style": style,
        },
        "plan_raw_text": text,
        **plan,
    }
    save_series(state)
    return state


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def find_scene(state: dict[str, Any], episode_number: int, scene_number: int) -> dict[str, Any]:
    for episode in state.get("episodes", []):
        if int(episode.get("episode_number", 0)) == episode_number:
            for scene in episode.get("scenes", []):
                if int(scene.get("scene_number", 0)) == scene_number:
                    return scene
    raise ValueError(f"Scene {episode_number}.{scene_number} not found in series {state.get('id')}")


def update_scene_state(state: dict[str, Any], episode_number: int, scene_number: int, updates: dict[str, Any]) -> dict[str, Any]:
    scene = find_scene(state, episode_number, scene_number)
    scene.update(updates)
    state["updated_at"] = _now_iso()
    return scene


async def create_share_link(cookies: dict[str, str], post_id: str) -> dict[str, Any]:
    return await grok_request(
        "POST",
        "/rest/media/post/create-link",
        cookies,
        {"postId": post_id, "source": "post-page", "platform": "web"},
    )


async def upscale_video(cookies: dict[str, str], video_id: str) -> dict[str, Any]:
    return await grok_request("POST", "/rest/media/video/upscale", cookies, {"videoId": video_id}, timeout=120.0)


async def create_conversation(cookies: dict[str, str]) -> dict[str, Any]:
    try:
        return await grok_request("POST", "/rest/app-chat/conversations", cookies, {})
    except httpx.HTTPStatusError:
        return await grok_request("POST", "/rest/app-chat/conversations/new", cookies, {"temporary": True})


def conversation_id(result: dict[str, Any]) -> str:
    cid = extract_conversation_id(result)
    if not cid:
        raise ValueError(f"Conversation id not found in response: {result}")
    return cid


async def send_agent_message(
    conversation_id_value: str,
    message: str,
    cookies: dict[str, str],
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "message": message,
        "modeId": "imagine-agent-mode-dev",
        "enableImageGeneration": True,
        "enableImageStreaming": True,
    }
    if extra:
        payload.update(extra)
    return await grok_request(
        "POST",
        f"/rest/app-chat/conversations/{conversation_id_value}/responses",
        cookies,
        payload,
        timeout=120.0,
    )


def image_payload(arguments: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "temporary": True,
        "message": arguments["prompt"],
        "parentResponseId": arguments.get("parent_response_id"),
        "disableSearch": False,
        "enableImageGeneration": True,
        "imageAttachments": [],
        "returnImageBytes": False,
        "returnRawGrokInXaiRequest": False,
        "fileAttachments": [],
        "enableImageStreaming": True,
        "imageGenerationCount": min(int(arguments.get("count", 2)), 2),
        "forceConcise": False,
        "toolOverrides": {
            "gmailSearch": False,
            "googleCalendarSearch": False,
            "outlookSearch": False,
            "outlookCalendarSearch": False,
            "googleDriveSearch": False,
        },
        "enableSideBySide": True,
        "responseMetadata": {"experiments": []},
        "sendFinalMetadata": True,
        "request_metadata": {},
        "disableTextFollowUps": False,
        "disableMemory": False,
        "forceSideBySide": False,
        "isAsyncChat": False,
        "disableSelfHarmShortCircuit": False,
        "collectionIds": [],
        "disabledConnectorIds": [],
        "linkQuery": False,
        "deviceEnvInfo": {
            "darkModeEnabled": True,
            "devicePixelRatio": 1.75,
            "screenWidth": 2560,
            "screenHeight": 1440,
            "viewportWidth": 899,
            "viewportHeight": 726,
        },
        "modeId": arguments.get("mode", "fast"),
    }
    if arguments.get("aspect_ratio"):
        payload["aspect_ratio"] = arguments["aspect_ratio"]
    if arguments.get("enable_nsfw") is not None:
        payload["enable_nsfw"] = bool(arguments["enable_nsfw"])
    if arguments.get("extra"):
        payload.update(arguments["extra"])
    return payload


def video_payload(arguments: dict[str, Any], parent_post_id: str) -> dict[str, Any]:
    mode = arguments.get("mode", "custom")
    prompt = arguments["prompt"]
    image_url = arguments.get("image_url")
    message = f"{image_url}  {prompt} --mode={mode}" if image_url else f"{prompt} --mode={mode}"
    config: dict[str, Any] = {
        "parentPostId": parent_post_id,
        "aspectRatio": arguments.get("aspect_ratio", "2:3"),
        "videoLength": int(arguments.get("duration", 6)),
        "resolutionName": arguments.get("resolution", "480p"),
    }
    payload = {
        "temporary": True,
        "modelName": "imagine-video-gen",
        "message": message,
        "enableSideBySide": True,
        "responseMetadata": {
            "experiments": [],
            "modelConfigOverride": {"modelMap": {"videoGenModelConfig": config}},
        },
    }
    if arguments.get("extra"):
        payload.update(arguments["extra"])
    return payload


async def start_video_generation(cookies: dict[str, str], payload: dict[str, Any]) -> list[dict[str, Any]]:
    return await browser_fetch("/rest/app-chat/conversations/new", cookies, payload, stream=True)


def extract_video_ids(events: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {"progress": 0}
    for event in events:
        for node in deep_values(event):
            video = node.get("streamingVideoGenerationResponse")
            if not isinstance(video, dict):
                continue
            for key in ("progress", "videoPostId", "videoId", "parentPostId", "resolutionName", "width", "height", "moderated"):
                if video.get(key) is not None:
                    result[key] = video[key]
    return result


async def wait_for_video_url(cookies: dict[str, str], post_id: str, timeout: int, poll_interval: int = 5) -> dict[str, Any]:
    latest: dict[str, Any] = {}
    for _ in range(max(timeout // poll_interval, 1)):
        latest = await get_media_post(cookies, post_id) or {}
        urls = collect_media_urls(latest)
        if urls["video_urls"]:
            return {"status": "ready", **post_summary({"post": latest}), **urls}
        await asyncio.sleep(poll_interval)
    urls = collect_media_urls(latest) if latest else {"image_urls": [], "video_urls": [], "audio_urls": []}
    return {"status": "timeout", **post_summary({"post": latest}), **urls}


async def run_streaming_generation(cookies: dict[str, str], payload: dict[str, Any], video: bool = False) -> tuple[str | None, list[dict[str, Any]]]:
    created = await grok_request("POST", "/rest/app-chat/conversations", cookies, {"temporary": True})
    cid = conversation_id(created)
    events = await grok_stream_request(f"/rest/app-chat/conversations/{cid}/responses", cookies, payload, video=video)
    return cid, events


def public_video_url_from_share(share_result: dict[str, Any]) -> str | None:
    text = json.dumps(share_result)
    match = re.search(r"(?:post|share|id)[^a-zA-Z0-9_-]+([a-f0-9-]{20,})", text, re.I)
    if not match:
        return None
    return f"{IMAGINE_PUBLIC_BASE}/share-videos/{match.group(1)}.mp4?cache=1"


def json_text(data: dict[str, Any]) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps(data, ensure_ascii=False, indent=2))]


COOKIE_SCHEMA = {
    "type": "object",
    "description": "Optional Grok cookies. If omitted, server uses account or cookies.json.",
    "additionalProperties": {"type": "string"},
}
ACCOUNT_SCHEMA = {
    "type": "string",
    "description": "Optional Grok account name from cookies/<name>.json. Overrides cookies.json if provided.",
}


@server.list_tools()
async def list_tools() -> list[Tool]:
    common_generation_fields = {
        "prompt": {"type": "string"},
        "cookies": COOKIE_SCHEMA,
        "account": ACCOUNT_SCHEMA,
        "aspect_ratio": {"type": "string", "description": "Examples: 16:9, 9:16, 1:1, 2:3, 3:2"},
        "extra": {"type": "object", "description": "Optional payload fields merged into the Grok request."},
    }
    return [
        Tool(
            name="imagine_agent_start",
            description="Start a Grok Imagine Agent conversation and send the first prompt.",
            inputSchema={"type": "object", "properties": common_generation_fields, "required": ["prompt"]},
        ),
        Tool(
            name="imagine_agent_send",
            description="Send a follow-up prompt to an existing Grok Imagine Agent conversation.",
            inputSchema={
                "type": "object",
                "properties": {
                    "conversation_id": {"type": "string"},
                    "prompt": {"type": "string"},
                    "cookies": COOKIE_SCHEMA,
                    "account": ACCOUNT_SCHEMA,
                    "extra": {"type": "object"},
                },
                "required": ["conversation_id", "prompt"],
            },
        ),
        Tool(
            name="imagine_agent_read",
            description="Read responses from a Grok Imagine Agent conversation.",
            inputSchema={
                "type": "object",
                "properties": {"conversation_id": {"type": "string"}, "cookies": COOKIE_SCHEMA, "account": ACCOUNT_SCHEMA},
                "required": ["conversation_id"],
            },
        ),
        Tool(
            name="generate_image",
            description="Generate images through the working Grok media endpoint and return parsed image URLs.",
            inputSchema={
                "type": "object",
                "properties": {
                    **common_generation_fields,
                    "timeout": {"type": "integer", "default": 180},
                    "count": {"type": "integer", "default": 2},
                    "mode": {"type": "string", "default": "fast", "description": "fast or expert"},
                    "enable_nsfw": {"type": "boolean"},
                    "send_to_telegram": {"type": "boolean", "default": False},
                    "telegram_bot_token": {"type": "string"},
                    "telegram_chat_id": {"type": "string"},
                    "telegram_caption": {"type": "string"},
                },
                "required": ["prompt"],
            },
        ),
        Tool(
            name="generate_video",
            description="Generate video through the working Grok media endpoint and return parsed video URLs.",
            inputSchema={
                "type": "object",
                "properties": {
                    **common_generation_fields,
                    "duration": {"type": "integer", "default": 6},
                    "resolution": {"type": "string", "default": "480p", "description": "480p or 720p"},
                    "mode": {"type": "string", "default": "custom"},
                    "parent_post_id": {"type": "string"},
                    "upscale_720p": {"type": "boolean", "default": False},
                    "share": {"type": "boolean", "default": False},
                    "timeout": {"type": "integer", "default": 300},
                    "send_to_telegram": {"type": "boolean", "default": False},
                    "telegram_bot_token": {"type": "string"},
                    "telegram_chat_id": {"type": "string"},
                    "telegram_caption": {"type": "string"},
                },
                "required": ["prompt"],
            },
        ),
        Tool(
            name="image_to_video",
            description="Create or reuse an image post, then generate a video from it.",
            inputSchema={
                "type": "object",
                "properties": {
                    **common_generation_fields,
                    "image_url": {"type": "string"},
                    "parent_post_id": {"type": "string"},
                    "duration": {"type": "integer", "default": 6},
                    "resolution": {"type": "string", "default": "480p"},
                    "mode": {"type": "string", "default": "custom"},
                    "share": {"type": "boolean", "default": False},
                },
                "required": ["prompt"],
            },
        ),
        Tool(
            name="create_media_post",
            description="Create a Grok media post from mediaType/prompt/mediaUrl.",
            inputSchema={
                "type": "object",
                "properties": {
                    "media_type": {"type": "string"},
                    "prompt": {"type": "string"},
                    "media_url": {"type": "string"},
                    "cookies": COOKIE_SCHEMA,
                    "account": ACCOUNT_SCHEMA,
                },
            },
        ),
        Tool(
            name="create_share_link",
            description="Create a share link for a Grok media post.",
            inputSchema={
                "type": "object",
                "properties": {"post_id": {"type": "string"}, "cookies": COOKIE_SCHEMA, "account": ACCOUNT_SCHEMA},
                "required": ["post_id"],
            },
        ),
        Tool(
            name="upscale_video",
            description="Ask Grok to upscale a generated video by video_id.",
            inputSchema={
                "type": "object",
                "properties": {"video_id": {"type": "string"}, "cookies": COOKIE_SCHEMA, "account": ACCOUNT_SCHEMA},
                "required": ["video_id"],
            },
        ),
        Tool(
            name="check_quota",
            description="Return Grok Imagine quota_info.",
            inputSchema={"type": "object", "properties": {"cookies": COOKIE_SCHEMA, "account": ACCOUNT_SCHEMA}},
        ),
        Tool(
            name="list_accounts",
            description="List Grok accounts available in cookies/<name>.json.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="send_to_telegram",
            description="Download a Grok media post by post_id using account cookies and send it to Telegram as video/photo.",
            inputSchema={
                "type": "object",
                "properties": {
                    "post_id": {"type": "string"},
                    "kind": {"type": "string", "enum": ["video", "image"], "default": "video"},
                    "cookies": COOKIE_SCHEMA,
                    "account": ACCOUNT_SCHEMA,
                    "telegram_bot_token": {"type": "string", "description": f"Or set env {TELEGRAM_ENV_TOKEN}."},
                    "telegram_chat_id": {"type": "string", "description": f"Or set env {TELEGRAM_ENV_CHAT}."},
                    "telegram_caption": {"type": "string"},
                },
                "required": ["post_id"],
            },
        ),
        Tool(
            name="list_projects",
            description="List Grok Imagine projects (canvases) for the account.",
            inputSchema={"type": "object", "properties": {"cookies": COOKIE_SCHEMA, "account": ACCOUNT_SCHEMA}},
        ),
        Tool(
            name="create_project",
            description="Create a new Grok Imagine project (canvas).",
            inputSchema={
                "type": "object",
                "properties": {"name": {"type": "string"}, "cookies": COOKIE_SCHEMA, "account": ACCOUNT_SCHEMA},
                "required": ["name"],
            },
        ),
        Tool(
            name="get_project_conversations",
            description="List agent conversations attached to a project.",
            inputSchema={
                "type": "object",
                "properties": {"project_id": {"type": "string"}, "cookies": COOKIE_SCHEMA, "account": ACCOUNT_SCHEMA},
                "required": ["project_id"],
            },
        ),
        Tool(
            name="create_project_node",
            description="Attach a node (media post) to a project canvas.",
            inputSchema={
                "type": "object",
                "properties": {
                    "project_id": {"type": "string"},
                    "node": {"type": "object", "description": "Node payload merged with canvasId, e.g. {\"postId\": ..., \"x\":0,\"y\":0}"},
                    "cookies": COOKIE_SCHEMA,
                    "account": ACCOUNT_SCHEMA,
                },
                "required": ["project_id", "node"],
            },
        ),
        Tool(
            name="set_project_thumbnail",
            description="Set the thumbnail post for a project.",
            inputSchema={
                "type": "object",
                "properties": {"project_id": {"type": "string"}, "post_id": {"type": "string"}, "cookies": COOKIE_SCHEMA, "account": ACCOUNT_SCHEMA},
                "required": ["project_id", "post_id"],
            },
        ),
        Tool(
            name="list_templates",
            description="List Grok Imagine pipeline templates (Short Film, UGC Product Stories, etc.).",
            inputSchema={"type": "object", "properties": {"cookies": COOKIE_SCHEMA, "account": ACCOUNT_SCHEMA}},
        ),
        Tool(
            name="get_template",
            description="Get a Grok Imagine template summary by templateId.",
            inputSchema={
                "type": "object",
                "properties": {"template_id": {"type": "string"}, "cookies": COOKIE_SCHEMA, "account": ACCOUNT_SCHEMA},
                "required": ["template_id"],
            },
        ),
        Tool(
            name="list_folders",
            description="List Grok Imagine folders.",
            inputSchema={"type": "object", "properties": {"cookies": COOKIE_SCHEMA, "account": ACCOUNT_SCHEMA}},
        ),
        Tool(
            name="get_post_folders",
            description="List folders that a post belongs to.",
            inputSchema={
                "type": "object",
                "properties": {"post_id": {"type": "string"}, "cookies": COOKIE_SCHEMA, "account": ACCOUNT_SCHEMA},
                "required": ["post_id"],
            },
        ),
        Tool(
            name="list_posts",
            description="List Grok Imagine posts owned or liked by the account.",
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "default": 40},
                    "source": {"type": "string", "enum": ["MEDIA_POST_SOURCE_OWNED", "MEDIA_POST_SOURCE_LIKED"], "default": "MEDIA_POST_SOURCE_OWNED"},
                    "safe": {"type": "boolean", "default": False},
                    "cookies": COOKIE_SCHEMA,
                    "account": ACCOUNT_SCHEMA,
                },
            },
        ),
        Tool(
            name="list_project_posts",
            description="Get a Grok project canvas (nodes + attached posts).",
            inputSchema={
                "type": "object",
                "properties": {"project_id": {"type": "string"}, "cookies": COOKIE_SCHEMA, "account": ACCOUNT_SCHEMA},
                "required": ["project_id"],
            },
        ),
        Tool(
            name="get_post",
            description="Get a Grok media post by post_id.",
            inputSchema={
                "type": "object",
                "properties": {"post_id": {"type": "string"}, "cookies": COOKIE_SCHEMA, "account": ACCOUNT_SCHEMA},
                "required": ["post_id"],
            },
        ),
        Tool(
            name="like_post",
            description="Like a Grok media post.",
            inputSchema={
                "type": "object",
                "properties": {"post_id": {"type": "string"}, "cookies": COOKIE_SCHEMA, "account": ACCOUNT_SCHEMA},
                "required": ["post_id"],
            },
        ),
        Tool(
            name="delete_post",
            description="Delete a Grok media post.",
            inputSchema={
                "type": "object",
                "properties": {"post_id": {"type": "string"}, "cookies": COOKIE_SCHEMA, "account": ACCOUNT_SCHEMA},
                "required": ["post_id"],
            },
        ),
        Tool(
            name="search_status",
            description="Return Grok Imagine media search index status.",
            inputSchema={"type": "object", "properties": {"cookies": COOKIE_SCHEMA, "account": ACCOUNT_SCHEMA}},
        ),
        Tool(
            name="agent_start",
            description="Start Grok Imagine Agent conversation (optionally inside a project).",
            inputSchema={
                "type": "object",
                "properties": {
                    "prompt": {"type": "string"},
                    "project_id": {"type": "string"},
                    "extra": {"type": "object"},
                    "cookies": COOKIE_SCHEMA,
                    "account": ACCOUNT_SCHEMA,
                },
                "required": ["prompt"],
            },
        ),
        Tool(
            name="agent_send",
            description="Send a follow-up prompt to a Grok Imagine Agent conversation.",
            inputSchema={
                "type": "object",
                "properties": {
                    "conversation_id": {"type": "string"},
                    "prompt": {"type": "string"},
                    "extra": {"type": "object"},
                    "cookies": COOKIE_SCHEMA,
                    "account": ACCOUNT_SCHEMA,
                },
                "required": ["conversation_id", "prompt"],
            },
        ),
        Tool(
            name="agent_read",
            description="Load recent responses of a Grok Imagine Agent conversation.",
            inputSchema={
                "type": "object",
                "properties": {"conversation_id": {"type": "string"}, "cookies": COOKIE_SCHEMA, "account": ACCOUNT_SCHEMA},
                "required": ["conversation_id"],
            },
        ),
        Tool(
            name="plan_series",
            description="Ask Grok to plan a video series (episodes + scenes + prompts) and save it to series/<id>.json.",
            inputSchema={
                "type": "object",
                "properties": {
                    "topic": {"type": "string"},
                    "episodes": {"type": "integer", "default": 1},
                    "scenes_per_episode": {"type": "integer", "default": 3},
                    "duration": {"type": "integer", "default": 6, "description": "Target scene duration in seconds"},
                    "style": {"type": "string", "description": "Optional style / audience / tone notes"},
                    "cookies": COOKIE_SCHEMA,
                    "account": ACCOUNT_SCHEMA,
                },
                "required": ["topic"],
            },
        ),
        Tool(
            name="list_series",
            description="List series files stored in series/*.json.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="get_series",
            description="Return the full series JSON by id.",
            inputSchema={
                "type": "object",
                "properties": {"series_id": {"type": "string"}},
                "required": ["series_id"],
            },
        ),
        Tool(
            name="update_scene",
            description="Update fields on a specific scene (status, prompt, aspect_ratio, video_url, ...).",
            inputSchema={
                "type": "object",
                "properties": {
                    "series_id": {"type": "string"},
                    "episode_number": {"type": "integer"},
                    "scene_number": {"type": "integer"},
                    "updates": {"type": "object"},
                },
                "required": ["series_id", "episode_number", "scene_number", "updates"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    if name == "list_accounts":
        return json_text({"accounts": list_accounts(), "default_cookies_json_present": COOKIE_FILE.exists()})
    cookies = resolve_cookies(arguments)

    if name == "imagine_agent_start":
        created = await create_conversation(cookies)
        cid = conversation_id(created)
        sent = await send_agent_message(cid, arguments["prompt"], cookies, arguments.get("extra"))
        return json_text({"conversation_id": cid, "created": created, "response": sent})

    if name == "imagine_agent_send":
        sent = await send_agent_message(arguments["conversation_id"], arguments["prompt"], cookies, arguments.get("extra"))
        return json_text({"conversation_id": arguments["conversation_id"], "response": sent})

    if name == "imagine_agent_read":
        result = await grok_request(
            "POST",
            f"/rest/app-chat/conversations/{arguments['conversation_id']}/load-responses",
            cookies,
            {},
            timeout=120.0,
        )
        return json_text(result)

    if name == "generate_image":
        result = await create_and_wait_media(
            cookies,
            "MEDIA_POST_TYPE_IMAGE",
            arguments["prompt"],
            int(arguments.get("timeout", 180)),
        )
        if arguments.get("send_to_telegram") and result.get("status") == "ready":
            try:
                result["telegram"] = await send_generation_to_telegram(cookies, result, "image", arguments)
            except Exception as e:
                result["telegram_error"] = str(e)
        return json_text(result)

    if name == "generate_video":
        parent_post_id = arguments.get("parent_post_id")
        if not parent_post_id:
            created = await create_media_post(cookies, media_type="MEDIA_POST_TYPE_VIDEO", prompt=arguments["prompt"])
            parent_post_id = post_summary(created)["post_id"]
        events = await start_video_generation(cookies, video_payload(arguments, parent_post_id))
        ids = extract_video_ids(events)
        if ids.get("moderated"):
            return json_text({"status": "moderated", "parent_post_id": parent_post_id, **ids})
        target_post_id = ids.get("videoPostId") or parent_post_id
        result = await wait_for_video_url(cookies, target_post_id, int(arguments.get("timeout", 300)))
        result.update({"parent_post_id": parent_post_id, "video_post_id": target_post_id, "progress": ids.get("progress")})
        if arguments.get("share") and target_post_id and result.get("status") == "ready":
            share = await create_share_link(cookies, target_post_id)
            result["share"] = share
            result["public_video_url"] = public_video_url_from_share(share)
        if arguments.get("send_to_telegram") and result.get("status") == "ready":
            try:
                result["telegram"] = await send_generation_to_telegram(cookies, result, "video", arguments)
            except Exception as e:
                result["telegram_error"] = str(e)
        return json_text(result)

    if name == "image_to_video":
        parent_post_id = arguments.get("parent_post_id")
        media_post: dict[str, Any] | None = None
        if not parent_post_id:
            if not arguments.get("image_url"):
                raise ValueError("image_to_video requires image_url or parent_post_id")
            media_post = await create_media_post(cookies, media_url=arguments["image_url"])
            parent_post_id = post_summary(media_post)["post_id"]
        events = await start_video_generation(cookies, video_payload(arguments, parent_post_id))
        ids = extract_video_ids(events)
        target_post_id = ids.get("videoPostId") or parent_post_id
        result = await wait_for_video_url(cookies, target_post_id, int(arguments.get("timeout", 300)))
        result.update({"parent_post_id": parent_post_id, "video_post_id": target_post_id, "media_post": media_post, "progress": ids.get("progress")})
        if arguments.get("share") and target_post_id and result.get("status") == "ready":
            share = await create_share_link(cookies, target_post_id)
            result["share"] = share
            result["public_video_url"] = public_video_url_from_share(share)
        return json_text(result)

    if name == "create_media_post":
        result = await create_media_post(
            cookies,
            arguments.get("media_type"),
            arguments.get("prompt"),
            arguments.get("media_url"),
        )
        return json_text({"status": "created", **post_summary(result)})

    if name == "create_share_link":
        result = await create_share_link(cookies, arguments["post_id"])
        return json_text({"share": result, "public_video_url": public_video_url_from_share(result)})

    if name == "upscale_video":
        return json_text(await upscale_video(cookies, arguments["video_id"]))

    if name == "check_quota":
        result = await grok_request("POST", "/rest/media/imagine/quota_info", cookies, {})
        return json_text(result)

    if name == "send_to_telegram":
        kind = arguments.get("kind", "video")
        post = await get_media_post(cookies, arguments["post_id"])
        if not post:
            raise ValueError(f"Post {arguments['post_id']} not found")
        urls = collect_media_urls(post)
        wrap = {"post_id": arguments["post_id"], **urls, "post": post}
        result = await send_generation_to_telegram(cookies, wrap, kind, arguments)
        return json_text({"post_id": arguments["post_id"], **urls, **result})

    if name == "list_projects":
        return json_text(await list_projects(cookies))

    if name == "create_project":
        return json_text(await create_project(cookies, arguments["name"]))

    if name == "get_project_conversations":
        return json_text(await get_project_conversations(cookies, arguments["project_id"]))

    if name == "create_project_node":
        return json_text(await create_project_node(cookies, arguments["project_id"], arguments["node"]))

    if name == "set_project_thumbnail":
        return json_text(await set_project_thumbnail(cookies, arguments["project_id"], arguments["post_id"]))

    if name == "list_templates":
        return json_text(await list_templates(cookies))

    if name == "get_template":
        return json_text(await get_template(cookies, arguments["template_id"]))

    if name == "list_folders":
        return json_text(await list_folders(cookies))

    if name == "get_post_folders":
        return json_text(await get_post_folders(cookies, arguments["post_id"]))

    if name == "list_posts":
        return json_text(await list_posts(
            cookies,
            int(arguments.get("limit", 40)),
            arguments.get("source", "MEDIA_POST_SOURCE_OWNED"),
            bool(arguments.get("safe", False)),
        ))

    if name == "list_project_posts":
        return json_text(await list_project_posts(cookies, arguments["project_id"]))

    if name == "get_post":
        post = await get_media_post(cookies, arguments["post_id"])
        if not post:
            raise ValueError(f"Post {arguments['post_id']} not found")
        urls = collect_media_urls(post)
        return json_text({"post_id": arguments["post_id"], **urls, "post": post})

    if name == "like_post":
        return json_text(await like_post(cookies, arguments["post_id"]))

    if name == "delete_post":
        return json_text(await delete_post(cookies, arguments["post_id"]))

    if name == "search_status":
        return json_text(await search_status(cookies))

    if name == "agent_start":
        cid, events = await start_agent_conversation(cookies, arguments.get("project_id"), arguments["prompt"], arguments.get("extra"))
        return json_text({"conversation_id": cid, "events": events})

    if name == "agent_send":
        events = await send_agent_prompt(cookies, arguments["conversation_id"], arguments["prompt"], arguments.get("extra"))
        return json_text({"conversation_id": arguments["conversation_id"], "events": events})

    if name == "agent_read":
        return json_text(await read_agent_conversation(cookies, arguments["conversation_id"]))

    if name == "plan_series":
        state = await plan_series(
            cookies,
            arguments["topic"],
            int(arguments.get("episodes", 1)),
            int(arguments.get("scenes_per_episode", 3)),
            arguments.get("style"),
            int(arguments.get("duration", 6)),
        )
        return json_text({"id": state["id"], "path": str(series_path(state["id"])), "series": state})

    if name == "list_series":
        return json_text({"series": list_series_files()})

    if name == "get_series":
        return json_text(load_series(arguments["series_id"]))

    if name == "update_scene":
        state = load_series(arguments["series_id"])
        scene = update_scene_state(state, int(arguments["episode_number"]), int(arguments["scene_number"]), arguments["updates"])
        save_series(state)
        return json_text({"series_id": state["id"], "scene": scene, "updated_at": state["updated_at"]})

    raise ValueError(f"Unknown tool: {name}")


async def main():
    from mcp.server.stdio import stdio_server

    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
