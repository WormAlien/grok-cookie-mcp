"""Sniffer for reverse-engineering Grok's file-upload endpoint(s).

Opens the same Chrome+cookies session as the MCP server, subscribes to CDP
Network events for the imagine page, logs every POST going to grok.com or
assets.grok.com to a jsonl file. User then drags a file into the chat manually;
we read the log to identify the upload endpoint.

Usage:
    uv run python scripts/sniff_upload.py --account 1 --out sniff-upload.jsonl
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

import websockets

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server import cdp_call, ensure_browser, load_cookie_file  # noqa: E402


INTERESTING_HOST_SUBSTRINGS = (
    "grok.com/rest/",
    "grok.com/api/",
    "assets.grok.com/",
    "grok-attachments",
    "storage.googleapis.com",
    "amazonaws.com",
    "azureedge",
    "presigned",
    "upload",
)


def is_interesting(url: str) -> bool:
    u = url.lower()
    return any(s in u for s in INTERESTING_HOST_SUBSTRINGS)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", default="1")
    ap.add_argument("--out", default="sniff-upload.jsonl")
    ap.add_argument("--include-get", action="store_true", help="also log GETs (default: POST/PUT only)")
    args = ap.parse_args()

    cookies = load_cookie_file(args.account)
    ws_url = await ensure_browser(cookies)
    print(f"[+] chrome up. cdp={ws_url}")

    out = Path(args.out)
    out.write_text("", encoding="utf-8")
    print(f"[+] logging to {out.resolve()}")
    print("[+] Открой Grok в браузере, зайди в chat, перетащи файл. Всё запишу.")
    print("[+] Ctrl+C — стоп")
    print("---")

    async with websockets.connect(ws_url, max_size=None) as ws:
        await cdp_call(ws, 1, "Network.enable", {"maxTotalBufferSize": 100_000_000})

        pending: dict[str, dict] = {}
        next_id = 100

        async def fetch_body(request_id: str) -> str | None:
            nonlocal next_id
            next_id += 1
            try:
                res = await cdp_call(ws, next_id, "Network.getRequestPostData", {"requestId": request_id})
                return res.get("postData")
            except Exception as exc:
                return f"<no body: {exc}>"

        async def fetch_response_body(request_id: str) -> str | None:
            nonlocal next_id
            next_id += 1
            try:
                res = await cdp_call(ws, next_id, "Network.getResponseBody", {"requestId": request_id})
                body = res.get("body")
                if res.get("base64Encoded") and body:
                    return f"<base64 {len(body)} chars>"
                if body and len(body) > 4000:
                    return body[:4000] + f"... (+{len(body) - 4000} more)"
                return body
            except Exception as exc:
                return f"<no response body: {exc}>"

        while True:
            raw = await ws.recv()
            try:
                data = json.loads(raw)
            except Exception:
                continue

            method = data.get("method")
            params = data.get("params", {})

            if method == "Network.requestWillBeSent":
                req = params.get("request", {})
                url = req.get("url", "")
                http_method = req.get("method", "GET")
                if not is_interesting(url):
                    continue
                if http_method not in ("POST", "PUT", "PATCH") and not args.include_get:
                    continue
                pending[params["requestId"]] = {
                    "requestId": params["requestId"],
                    "ts": datetime.utcnow().isoformat(),
                    "url": url,
                    "method": http_method,
                    "headers": req.get("headers", {}),
                    "hasPostData": req.get("hasPostData", False),
                    "postData": req.get("postData"),
                    "resourceType": params.get("type"),
                    "initiator": params.get("initiator", {}).get("type"),
                }
                # kick off body fetch if not inlined
                if pending[params["requestId"]]["hasPostData"] and not pending[params["requestId"]]["postData"]:
                    body = await fetch_body(params["requestId"])
                    if isinstance(body, str) and len(body) > 4000:
                        pending[params["requestId"]]["postDataTruncated"] = True
                        pending[params["requestId"]]["postData"] = body[:4000] + f"... (+{len(body)-4000})"
                    else:
                        pending[params["requestId"]]["postData"] = body
                short = url.replace("https://grok.com", "").replace("https://assets.grok.com", "@assets")
                print(f"[REQ] {http_method} {short}")

            elif method == "Network.responseReceived":
                rid = params["requestId"]
                if rid not in pending:
                    continue
                resp = params.get("response", {})
                pending[rid]["responseStatus"] = resp.get("status")
                pending[rid]["responseHeaders"] = resp.get("headers", {})
                pending[rid]["responseMimeType"] = resp.get("mimeType")

            elif method == "Network.loadingFinished":
                rid = params["requestId"]
                if rid not in pending:
                    continue
                entry = pending.pop(rid)
                # try to grab response body
                entry["responseBody"] = await fetch_response_body(rid)
                with out.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
                status = entry.get("responseStatus")
                short = entry["url"].replace("https://grok.com", "").replace("https://assets.grok.com", "@assets")
                print(f"[RES {status}] {entry['method']} {short}")

            elif method == "Network.loadingFailed":
                rid = params["requestId"]
                if rid in pending:
                    entry = pending.pop(rid)
                    entry["failed"] = params.get("errorText")
                    with out.open("a", encoding="utf-8") as fh:
                        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[+] stopped")
