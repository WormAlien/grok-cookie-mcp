# grok-cookie-mcp — architecture

Unofficial local MCP server that exposes the Grok Imagine web app as tools,
using the user's own browser cookies. No xAI API key. Runs on Windows.

Repo: https://github.com/WormAlien/grok-cookie-mcp

## Why it exists

- Grok Imagine (`grok.com/imagine`) has richer video/agent features than the
  public xAI Imagine API, but the fronted-only endpoints are guarded by
  Cloudflare + `x-statsig-id` anti-bot.
- This server drives those endpoints from a real Chrome CDP session using
  your logged-in cookies, so we can:
  - generate images / videos / image-to-video
  - use projects (canvas) and Imagine Agent
  - store completed media and pipe it to Telegram
- It is per-user, per-account, cookie-scoped. It is not a public relay.

## Data flow (top-level)

```
+----------------+       +-----------------+       +---------------+
|  MCP client    | ----> | server.py (MCP) | ----> | Chrome (CDP)  |
|  (Claude etc.) |       | tools listed in |       |  page.fetch() |
+----------------+       |  list_tools()   |       +-------+-------+
                         |                 |               |
                         |                 |               v
                         |                 |       +---------------+
                         |                 |       | grok.com REST |
                         |                 |       | (anti-bot ok) |
                         |                 |       +-------+-------+
                         |                 |               |
                         |                 | <--- streaming JSON
                         |                 |
                         | download(mp4)   |
                         |    +----+-------+
                         |    |            |
                         v    v            v
                    +------------+   +------------------+
                    | output/    |   | Telegram Bot API |
                    | *.mp4/jpg  |   | (send file)      |
                    +------------+   +------------------+
```

## Repo layout

```
app/grok-cookie-mcp/
├─ server.py            # MCP server (all Grok tools)
├─ bot.py               # aiogram Telegram approval bot (imports server.py)
├─ bot.bat              # convenience launcher for bot.py
├─ launcher.py          # FastAPI Chrome-with-cookies launcher UI (:8765)
├─ launch.bat           # convenience launcher for launcher.py
├─ pyproject.toml       # deps
├─ uv.lock              # locked deps
├─ cookies.example.json # Cookie-Editor format template
├─ cookies.json         # default account (gitignored)
├─ cookies/             # additional accounts, <name>.json (gitignored)
├─ output/              # downloaded media (gitignored)
├─ .env                 # GROK_TG_BOT_TOKEN + GROK_TG_CHAT_ID (gitignored)
├─ README.md
└─ ARCHITECTURE.md      # you are here
```

## Auth model

- Cookies come from `Cookie-Editor` (Chrome extension) — array JSON of cookie
  objects, or a flat name→value dict.
- Server reads:
  1. `cookies` in the tool arguments (if passed), else
  2. `cookies/<account>.json` when `account` argument given, else
  3. `cookies.json` next to server.py.
- Required cookies for a working session: `sso`, `sso-rw`, `cf_clearance`,
  `grok_device_id`. `__cf_bm` refreshes on demand.
- Multi-account: any number of files in `cookies/<name>.json`. Tools accept
  `account: "1"` etc. `list_accounts` enumerates what's on disk.
- Companion dashboard (`Autoreger_Clean` at `http://localhost:8200/__switch`)
  writes those files via `POST /__switch/api/grok/sessions`. localStorage
  storage was replaced by disk to avoid keeping cookies in the browser.

## Chrome / CDP layer

- `ensure_browser(cookies)` starts an isolated Chrome (`--user-data-dir=$tmp`)
  with a random port between 9401–9499. It navigates to `grok.com/imagine`
  and injects cookies via `Network.setCookie` before load.
- `Page.addScriptToEvaluateOnNewDocument` installs `STATSIG_SNIFFER_JS`, which
  monkey-patches `window.fetch` and captures the `x-statsig-id` header from
  the first real Grok request. That id is the anti-bot passport for our own
  browser-level requests.
- `browser_fetch(path, cookies, payload, stream=?)` performs the actual
  request from inside the Grok page context using `window.fetch`, attaching
  the harvested `x-statsig-id` and a fresh `x-xai-request-id`. Streaming
  responses come back as line-delimited JSON.

Without the sniffed statsig id we get `403 anti-bot`.
With it, all REST endpoints seen in the UI work.

## MCP tool catalog

Media generation (uses `browser_fetch`):
- `check_quota`
- `generate_image(prompt)` — via `POST /rest/media/post/create`
- `generate_video(prompt, aspect_ratio, resolution, duration, ...)` —
  creates a parent post, then starts real video via
  `POST /rest/app-chat/conversations/new` with `modelName: "imagine-video-gen"`,
  polls `POST /rest/media/post/get` until `mediaUrl` is populated.
- `image_to_video(image_url | parent_post_id, prompt, ...)`
- `create_media_post`
- `create_share_link` + public URL builder
- `upscale_video`

Content management:
- `list_posts` (legacy, Mongo-removed by Grok on many accounts)
- `list_project_posts(project_id)` — via `canvas/get`
- `get_post`
- `like_post`
- `delete_post`
- `get_post_folders`
- `list_folders`

Projects (canvas):
- `list_projects` — `canvas/list`
- `create_project(name)` — `canvas/create`
- `get_project_conversations(project_id)` — `conversation/get`
- `create_project_node(project_id, node)` — `canvas/node/create`
- `set_project_thumbnail(project_id, post_id)`

Pipeline templates (Short Film, UGC Product Stories, Chibi, …):
- `list_templates`
- `get_template(template_id)`

Imagine Agent (streaming inside a project):
- `agent_start(prompt, project_id?)` — canvas-aware start
- `agent_send(conversation_id, prompt)`
- `agent_read(conversation_id)`
- Legacy: `imagine_agent_start / send / read`

Ops:
- `search_status` — media search index status
- `list_accounts` — accounts on disk
- `send_to_telegram(post_id, kind)` — downloads via account cookies, uploads
  to Telegram Bot API (video/photo/document)
- Every generation tool takes optional `send_to_telegram=true` to auto-push
  the finished media to the configured chat.

## Grok endpoints touched

Live REST endpoints (all POST unless noted):
```
/rest/media/imagine/quota_info
/rest/media/post/create
/rest/media/post/get
/rest/media/post/list        (deprecated on some accounts)
/rest/media/post/folders
/rest/media/post/like
/rest/media/post/delete
/rest/media/post/create-link
/rest/media/video/upscale
/rest/media/canvas/list
/rest/media/canvas/create
/rest/media/canvas/get
/rest/media/canvas/node/create
/rest/media/canvas/set-thumbnail
/rest/media/conversation/get
/rest/media/folder/list
/rest/media/pipeline/template/list
/rest/media/pipeline/template/get
/rest/media/search/status
/rest/app-chat/conversations/new
/rest/app-chat/conversations/{id}/responses
/rest/app-chat/conversations/{id}/load-responses
```

Static asset (auth-scoped, use `download_asset` with cookies):
```
https://assets.grok.com/users/{userId}/generated/{postId}/generated_video.mp4
https://assets.grok.com/users/{userId}/generated/{postId}/preview_image.jpg
```

Public share (after `create_share_link`):
```
https://imagine-public.x.ai/imagine-public/share-videos/{id}.mp4?cache=1
```

## Video generation payload (proven working)

`POST /rest/app-chat/conversations/new`:
```json
{
  "temporary": true,
  "modelName": "imagine-video-gen",
  "message": "<prompt> --mode=custom",
  "enableSideBySide": true,
  "responseMetadata": {
    "experiments": [],
    "modelConfigOverride": {
      "modelMap": {
        "videoGenModelConfig": {
          "parentPostId": "<from post/create>",
          "aspectRatio": "2:3",
          "videoLength": 6,
          "resolutionName": "480p"
        }
      }
    }
  }
}
```

Response is line-delimited JSON with `streamingVideoGenerationResponse`
carrying `progress`, `videoId`, `videoPostId`. When done, the finished mp4
appears under `assets.grok.com/users/{userId}/generated/{postId}/generated_video.mp4`.

## Telegram integration

- Config: `.env` sets `GROK_TG_BOT_TOKEN`, `GROK_TG_CHAT_ID`. Server auto-loads
  it at startup (`_ENV_PATH` block near the top of server.py).
- `send_generation_to_telegram` downloads each URL via `download_asset()`
  using account cookies, saves it into `output/<post_id>_<kind>_<i>.<ext>`,
  and calls `sendVideo` / `sendPhoto` on the Bot API with a caption.
- Bot: `@SuperGrokFarm_bot` — receives approvals, sends media to owner chat.

## Companion dashboard glue (Autoreger_Clean)

- `Autoreger_Clean/routing/transparent-proxy.js` on port 8200 serves the UI
  and exposes:
  ```
  GET    /__switch/api/grok/sessions
  POST   /__switch/api/grok/sessions   {name, cookies}
  DELETE /__switch/api/grok/sessions/<name>
  ```
  Files land directly in `D:\WORMALIENAIGIGANT\app\grok-cookie-mcp\cookies\`.
- `proxy-dashboard.html` "Grok" tab lets the user paste Cookie-Editor JSON,
  save it under an account name, launch an isolated Chrome via
  `http://localhost:8765/launch` (that's `launcher.py`).
- On first load, the dashboard migrates any pre-existing
  `localStorage["grok-saved-sessions"]` to disk and clears localStorage.

## Content farm plan (roadmap)

The end goal is a "series factory":

1. `plan_series(topic, episodes)` — Grok chat (via us) writes an episode list
   with scene descriptions + per-scene prompt + consistency notes.
2. Each planned scene → `generate_image` preview + Telegram card with
   inline buttons: approve / regenerate / edit.  ✅ implemented in `bot.py`.
3. Approved scenes → `generate_video(parent_post_id, prompt)`.  ✅ from bot.
4. `concat_scenes(scene_ids)` — ffmpeg stitches, adds background music,
   optional Whisper subtitles.  ⏳ next.
5. Final mp4 → Telegram + `output/series/<id>.mp4`.

## Approval bot (`bot.py`)

Separate aiogram process (started via `bot.bat` or `uv run python bot.py`).
Uses the same `.env` as the MCP server, importing helpers directly from
`server.py` (no HTTP indirection, no duplicated Grok/CDP code).

Commands (owner-only, guarded by `GROK_TG_CHAT_ID`):
- `/series <topic>` — calls `plan_series` and posts a card per scene
- `/list` — enumerates `series/*.json`
- `/open <id>` — picks the active series for this chat
- `/show [id]` — re-renders scene cards for the active/named series
- `/accounts` — lists cookie files on disk

Each scene card carries inline buttons — `✅ Одобрить`, `♻️ Регенерировать`,
`✏️ Правки` (in `planned/regenerating/video_error`), then `🎬 Сгенерировать
видео` / `↩️ Отменить одобрение` (in `approved`), then `🔁 Перегенерировать
видео` (in `video_ready`).

Actions:
- **approve** — marks scene `status="approved"` and rewires buttons.
- **regenerate** — asks Grok to rewrite the single scene prompt (Agent mode
  via `grok_chat`, UI fallback via `grok_chat_via_ui`).
- **edit** — the next plain-text message from the owner replaces the prompt.
- **video** — seeds a parent post (`create_and_wait_media` on the prompt),
  runs `start_video_generation` + `wait_for_video_url`, downloads via
  `download_asset` under account cookies, then `sendVideo` to the same chat.

State: everything is persisted through `save_series(state)` — no bot-local
storage. In-flight regen/video tasks are tracked in-memory only, to prevent
double-clicks and let ordinary MCP tools inspect the state file at any time.

## Known limits

- The `x-statsig-id` sniffer relies on Grok making at least one native
  request while the page is loaded. `browser_fetch` warms it via
  `quota_info` if it isn't captured yet.
- Grok anti-bot is stricter on brand-new temp profiles than on your daily
  Chrome. Passing more cookies (`_ga`, `__stripe_mid`, `mp_...`) reduces
  friction; the launcher already relays every cookie the user paste-imports.
- `list_posts` is turned off account-side ("MongoDB-backed media post
  listing has been removed") — walk canvases/folders instead.
- Video generation currently uses the default `imagine-video-gen` model.
  Longer than the UI cap needs the split-and-stitch pattern (not
  implemented yet).

## References for further ideas

- `harry0703/MoneyPrinterTurbo` — MoviePy/ffmpeg concat + subtitle stack.
- `RayVentura/ShortGPT` — engine separation, EditingMarkup, TinyDB state.
- `jonsern-creator/grokyt-agent` — IdeaAgent → ScriptAgent → VideoAgent
  chain.
- `jdbsolution/GIExtStudio` — split-and-stitch to beat the 30-sec limit.
- `pattalkslaw-del/grok-imagine-toolkit` — character consistency patterns
  (R2V, I2V chain, multi-image edit, n=4 batch).
- `qwerfo/n8n-faceless-youtube-automation` — 25-node reference pipeline.

## Contact points inside code

- Anti-bot & CDP: `STATSIG_SNIFFER_JS`, `ensure_browser`, `browser_fetch`
- Media: `create_media_post`, `create_and_wait_media`, `wait_for_video_url`,
  `collect_media_urls`
- Video: `video_payload`, `start_video_generation`, `extract_video_ids`
- Agent: `start_agent_conversation`, `send_agent_prompt`, `read_agent_conversation`
- Projects: `list_projects`, `create_project`, `get_project_conversations`,
  `list_project_posts`, `create_project_node`, `set_project_thumbnail`
- Telegram: `download_asset`, `send_generation_to_telegram`,
  `telegram_send_media`
- Multi-account: `resolve_cookies`, `load_cookie_file`, `list_accounts`
