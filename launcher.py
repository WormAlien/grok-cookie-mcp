"""Cookie Session Launcher — paste cookie JSON, get a working browser session.

Opens Chrome with cookies injected via CDP. No API key needed.
Windows-only: uses `start` + `shell=True` to launch Chrome with debug port.
"""

import asyncio
import json
import os
import random
import subprocess
import tempfile
from pathlib import Path

import httpx
import websockets
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

app = FastAPI(title="Cookie Launcher")

# CORS для dashboard
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DEBUG_PORT = 9222


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


async def get_cdp_ws_url() -> str | None:
    """Try to connect to already-running Chrome CDP. Returns page-level WebSocket."""
    try:
        async with httpx.AsyncClient() as client:
            # Get list of pages (not browser-level endpoint)
            resp = await client.get(
                f"http://localhost:{DEBUG_PORT}/json",
                timeout=httpx.Timeout(2),
            )
            pages = resp.json()
            if pages:
                # Return first page's WebSocket (page-level, not browser-level)
                return pages[0]["webSocketDebuggerUrl"]
    except Exception:
        pass
    return None


def launch_chrome_window(debug_port: int) -> bool:
    """Launch Chrome with debugging port. Returns True if CDP becomes available."""
    chrome_path = find_chrome()
    if not chrome_path:
        raise RuntimeError("Chrome not found")

    # Windows: must use 'start' with shell=True, otherwise Chrome exits immediately
    user_data = tempfile.mkdtemp(prefix="cookie-session-")
    cmd = (
        f'start "" "{chrome_path}"'
        f" --remote-debugging-port={debug_port}"
        f' --user-data-dir="{user_data}"'
        f" --no-first-run --no-default-browser-check --disable-sync"
        f" about:blank"
    )
    subprocess.run(cmd, shell=True)
    return True


async def inject_cookies_and_navigate(ws_url: str, cookies: list[dict], target_domain: str):
    """Inject cookies via CDP and navigate to target."""
    async with websockets.connect(ws_url) as ws:
        # Enable Network domain
        await ws.send(json.dumps({"id": 1, "method": "Network.enable"}))
        await ws.recv()

        # Inject cookies first (works on about:blank with Network.enable)
        for i, cookie in enumerate(cookies):
            domain = cookie.get("domain", f".{target_domain}")
            if not domain.startswith("."):
                domain = f".{target_domain}"

            await ws.send(json.dumps({
                "id": i + 10,
                "method": "Network.setCookie",
                "params": {
                    "name": cookie["name"],
                    "value": cookie["value"],
                    "domain": domain,
                    "path": cookie.get("path", "/"),
                    "secure": cookie.get("secure", True),
                    "httpOnly": cookie.get("httpOnly", False),
                    "sameSite": "Lax" if cookie.get("sameSite") in (None, "unspecified") else cookie["sameSite"],
                },
            }))
            resp = await ws.recv()
            result = json.loads(resp)
            ok = result.get("result", {}).get("success", False)
            print(f"  Cookie {cookie['name']}: {'OK' if ok else f'FAILED: {resp[:120]}'}", flush=True)

        # Now navigate to target with cookies already set
        await ws.send(json.dumps({
            "id": 999,
            "method": "Page.navigate",
            "params": {"url": f"https://{target_domain}/imagine"},
        }))
        await ws.recv()
        print(f"  Navigated to https://{target_domain}/imagine", flush=True)


async def launch_browser(cookies: list[dict], target: str = "grok.com") -> str:
    # Always launch isolated Chrome with random port
    debug_port = random.randint(9300, 9400)
    launch_chrome_window(debug_port)

    # Give Chrome time to start before connecting CDP
    await asyncio.sleep(2)

    # Wait for CDP
    for i in range(20):
        await asyncio.sleep(0.5)
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"http://localhost:{debug_port}/json",
                    timeout=httpx.Timeout(2),
                )
                pages = resp.json()
                # Find first page (not background_page, not service_worker)
                page = next((p for p in pages if p.get("type") == "page"), None)
                if page:
                    ws_url = page["webSocketDebuggerUrl"]
                    print(f"[launcher] Connecting to: {ws_url}", flush=True)
                    await inject_cookies_and_navigate(ws_url, cookies, target)
                    return f"Isolated Chrome on port {debug_port}, cookies injected"
        except Exception as e:
            print(f"[launcher] CDP attempt {i}: {e}", flush=True)

    raise RuntimeError(f"Chrome CDP not available on port {debug_port}")


# --- HTML UI ---

INDEX_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Cookie Session Launcher</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: 'Segoe UI', system-ui, sans-serif;
    background: #0d0d0d; color: #e0e0e0;
    min-height: 100vh; display: flex; align-items: center; justify-content: center;
  }
  .container { max-width: 640px; width: 100%; padding: 24px; }
  h1 { font-size: 1.4em; margin-bottom: 8px; color: #fff; }
  .sub { color: #888; font-size: 0.85em; margin-bottom: 20px; }
  textarea {
    width: 100%; height: 280px; background: #1a1a1a; border: 1px solid #333;
    color: #e0e0e0; padding: 12px; font-family: 'Cascadia Code', 'Fira Code', monospace;
    font-size: 0.8em; border-radius: 8px; resize: vertical;
  }
  textarea:focus { outline: none; border-color: #4af; }
  .row { display: flex; gap: 8px; margin-top: 12px; }
  input {
    flex: 1; background: #1a1a1a; border: 1px solid #333; color: #e0e0e0;
    padding: 10px 12px; border-radius: 8px; font-size: 0.9em;
  }
  input:focus { outline: none; border-color: #4af; }
  button {
    padding: 10px 24px; background: #4af; color: #000; border: none;
    border-radius: 8px; font-weight: 600; cursor: pointer; font-size: 0.9em;
    white-space: nowrap;
  }
  button:hover { background: #6bf; }
  button:disabled { background: #333; color: #666; cursor: not-allowed; }
  .status { margin-top: 12px; padding: 10px; border-radius: 6px; font-size: 0.85em; display: none; }
  .status.ok { background: #1a3a1a; color: #4f8; display: block; }
  .status.err { background: #3a1a1a; color: #f66; display: block; }
  .status.info { background: #1a1a3a; color: #8af; display: block; }
  .hint { color: #666; font-size: 0.75em; margin-top: 16px; line-height: 1.5; }
  code { background: #222; padding: 1px 5px; border-radius: 3px; font-size: 0.9em; }
</style>
</head>
<body>
<div class="container">
  <h1>Cookie Session Launcher</h1>
  <div class="sub">Paste Cookie-Editor JSON → open Chrome with working session</div>

  <textarea id="cookies" placeholder='[{"domain":".grok.com","name":"sso","value":"eyJ...","secure":true}]'></textarea>

  <div class="row">
    <input id="target" value="grok.com" placeholder="grok.com">
    <button id="launch" onclick="launch()">Launch</button>
  </div>

  <div id="status" class="status"></div>

  <div class="hint">
    <strong>How to get cookies:</strong><br>
    1. Open target site in browser where you're logged in<br>
    2. Cookie-Editor extension → Export → copy JSON<br>
    3. Paste here → Launch<br>
    <br>
    Works for any site: <code>grok.com</code>, <code>cursor.com</code>, <code>x.ai</code>, etc.
  </div>
</div>

<script>
async function launch() {
  const btn = document.getElementById('launch');
  const status = document.getElementById('status');
  const raw = document.getElementById('cookies').value.trim();
  const target = document.getElementById('target').value.trim() || 'grok.com';

  if (!raw) { status.className = 'status err'; status.textContent = 'Paste cookie JSON first'; return; }

  let cookies;
  try { cookies = JSON.parse(raw); } catch(e) { status.className = 'status err'; status.textContent = 'Invalid JSON: ' + e.message; return; }
  if (!Array.isArray(cookies)) { status.className = 'status err'; status.textContent = 'JSON must be an array'; return; }

  btn.disabled = true;
  status.className = 'status info';
  status.textContent = 'Launching Chrome...';

  try {
    const resp = await fetch('/launch', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({cookies, target}),
    });
    const data = await resp.json();
    if (resp.ok) {
      status.className = 'status ok';
      status.textContent = data.message + ' — Chrome window opened, work in it';
    } else {
      status.className = 'status err';
      status.textContent = data.detail || 'Launch failed';
    }
  } catch(e) {
    status.className = 'status err';
    status.textContent = 'Connection failed: ' + e.message;
  }
  btn.disabled = false;
}
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
async def index():
    return INDEX_HTML


@app.post("/launch")
async def launch(data: dict):
    cookies = data.get("cookies", [])
    target = data.get("target", "grok.com")
    if not cookies:
        return {"detail": "No cookies provided"}, 400
    try:
        print(f"[launcher] Received {len(cookies)} cookies for {target}", flush=True)
        result = await launch_browser(cookies, target)
        print(f"[launcher] Success: {result}", flush=True)
        return {"message": result}
    except Exception as e:
        print(f"[launcher] Error: {e}", flush=True)
        import traceback
        traceback.print_exc()
        return {"detail": str(e)}, 500


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8765"))
    print(f"\n  Cookie Launcher: http://localhost:{port}\n")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
