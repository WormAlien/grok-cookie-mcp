"""Grok MCP Server — cookie-based Grok Imagine tools."""

import asyncio
import json
import os
import random
import re
import subprocess
import tempfile
import uuid
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


def launch_chrome(debug_port: int) -> None:
    chrome_path = find_chrome()
    if not chrome_path:
        raise RuntimeError("Chrome not found")
    user_data = tempfile.mkdtemp(prefix="grok-mcp-")
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


async def ensure_browser(cookies: dict[str, str]) -> str:
    port = BROWSER_STATE.get("port")
    if port:
        page = await get_cdp_page(port)
        if page and page.get("webSocketDebuggerUrl"):
            BROWSER_STATE["ws_url"] = page["webSocketDebuggerUrl"]
            return page["webSocketDebuggerUrl"]

    debug_port = random.randint(9401, 9499)
    launch_chrome(debug_port)
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


async def inject_browser_cookies(ws_url: str, cookies: dict[str, str]) -> None:
    async with websockets.connect(ws_url) as ws:
        await cdp_call(ws, 1, "Network.enable")
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
    js = f"""
async () => {{
  const response = await fetch({js_literal(path)}, {{
    method: 'POST',
    credentials: 'include',
    headers: {{'content-type': 'application/json', 'accept': '*/*'}},
    body: JSON.stringify({js_literal(payload)})
  }});
  const text = await response.text();
  return {{ok: response.ok, status: response.status, text}};
}}
"""
    async with websockets.connect(ws_url, max_size=64 * 1024 * 1024) as ws:
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


def load_cookie_file() -> dict[str, str]:
    if not COOKIE_FILE.exists():
        return {}

    cookies = json.loads(COOKIE_FILE.read_text(encoding="utf-8"))
    if isinstance(cookies, dict):
        return {normalize_cookie_name(k): str(v) for k, v in cookies.items() if v}
    if isinstance(cookies, list):
        return {normalize_cookie_name(c["name"]): c["value"] for c in cookies if c.get("name") and c.get("value")}
    return {}


def normalize_cookie_name(name: str) -> str:
    return "sso-rw" if name == "sso_rw" else name


def resolve_cookies(arguments: dict[str, Any]) -> dict[str, str]:
    raw = arguments.get("cookies") or load_cookie_file()
    cookies = {normalize_cookie_name(k): str(v) for k, v in raw.items() if v}
    if not cookies:
        raise ValueError("Pass cookies or create cookies.json next to server.py")
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


def video_payload(arguments: dict[str, Any], parent_post_id: str | None = None) -> dict[str, Any]:
    mode = arguments.get("mode", "custom")
    prompt = arguments["prompt"]
    image_url = arguments.get("image_url")
    message = f"{image_url}  {prompt} --mode={mode}" if image_url else f"{prompt} --mode={mode}"
    config: dict[str, Any] = {
        "aspectRatio": arguments.get("aspect_ratio", "16:9"),
        "videoLength": int(arguments.get("duration", 6)),
        "isVideoEdit": bool(arguments.get("is_video_edit", False)),
        "resolutionName": arguments.get("resolution", "480p"),
    }
    if parent_post_id:
        config["parentPostId"] = parent_post_id
    payload = {
        "temporary": True,
        "modelName": "grok-3",
        "message": message,
        "toolOverrides": {"videoGen": True},
        "enableSideBySide": True,
        "responseMetadata": {
            "experiments": [],
            "modelConfigOverride": {"modelMap": {"videoGenModelConfig": config}},
        },
    }
    if arguments.get("extra"):
        payload.update(arguments["extra"])
    return payload


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
    "description": "Optional Grok cookies. If omitted, server reads cookies.json next to server.py.",
    "additionalProperties": {"type": "string"},
}


@server.list_tools()
async def list_tools() -> list[Tool]:
    common_generation_fields = {
        "prompt": {"type": "string"},
        "cookies": COOKIE_SCHEMA,
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
                "properties": {"conversation_id": {"type": "string"}, "cookies": COOKIE_SCHEMA},
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
                },
            },
        ),
        Tool(
            name="create_share_link",
            description="Create a share link for a Grok media post.",
            inputSchema={
                "type": "object",
                "properties": {"post_id": {"type": "string"}, "cookies": COOKIE_SCHEMA},
                "required": ["post_id"],
            },
        ),
        Tool(
            name="upscale_video",
            description="Ask Grok to upscale a generated video by video_id.",
            inputSchema={
                "type": "object",
                "properties": {"video_id": {"type": "string"}, "cookies": COOKIE_SCHEMA},
                "required": ["video_id"],
            },
        ),
        Tool(
            name="check_quota",
            description="Return Grok Imagine quota_info.",
            inputSchema={"type": "object", "properties": {"cookies": COOKIE_SCHEMA}},
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
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
        return json_text(result)

    if name == "generate_video":
        result = await create_and_wait_media(
            cookies,
            "MEDIA_POST_TYPE_VIDEO",
            arguments["prompt"],
            int(arguments.get("timeout", 300)),
        )
        post_id = result.get("post_id")
        if arguments.get("share") and post_id:
            share = await create_share_link(cookies, post_id)
            result["share"] = share
            result["public_video_url"] = public_video_url_from_share(share)
        return json_text(result)

    if name == "image_to_video":
        parent_post_id = arguments.get("parent_post_id")
        media_post: dict[str, Any] | None = None
        if not parent_post_id:
            if not arguments.get("image_url"):
                raise ValueError("image_to_video requires image_url or parent_post_id")
            media_post = await create_media_post(cookies, media_url=arguments["image_url"])
            parent_post_id = post_summary(media_post)["post_id"]
        cid, events = await run_streaming_generation(cookies, video_payload(arguments, parent_post_id), video=True)
        state = extract_video_state(events)
        if arguments.get("share") and (state.get("videoPostId") or state.get("postId")):
            share = await create_share_link(cookies, state.get("videoPostId") or state["postId"])
            state["share"] = share
            state["public_video_url"] = public_video_url_from_share(share)
        return json_text({
            "status": "complete" if state.get("video_url") else "incomplete",
            "conversation_id": cid,
            "parent_post_id": parent_post_id,
            "media_post": media_post,
            **state,
            "events": events,
        })

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

    raise ValueError(f"Unknown tool: {name}")


async def main():
    from mcp.server.stdio import stdio_server

    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
