# grok-cookie-mcp

Cookie-based MCP tools for Grok Imagine experiments.

This is an unofficial reverse-engineered local MCP server. It uses your own browser cookies from `grok.com`; it does not use an xAI API key.

## Status

Working:

- `check_quota`
- `generate_image` via `/rest/media/post/create`
- `create_media_post`
- `create_share_link`
- `upscale_video` wrapper
- `post/get` polling for media posts
- Cookie launcher UI for opening an isolated Chrome session

Experimental / not fully working yet:

- App-chat `/responses` requests can be rejected by Grok anti-bot rules.
- Full video generation is still under investigation. Direct `MEDIA_POST_TYPE_VIDEO` creates a post, but may not start the actual video render.
- Imagine Agent tools are present but depend on the app-chat path.

## Install

```bash
uv sync
```

## Cookies

Copy the example and paste your Cookie-Editor export values:

```bash
cp cookies.example.json cookies.json
```

Never commit `cookies.json`.

## Run MCP server

```bash
uv run python server.py
```

## Run cookie launcher

```bash
uv run python launcher.py
```

Open:

```text
http://127.0.0.1:8765
```

Paste Cookie-Editor JSON and launch an isolated Chrome session.

## Tools

- `check_quota` — read Grok Imagine quota info
- `generate_image` — create image media post and return image URLs
- `generate_video` — experimental video post creation and polling
- `image_to_video` — experimental, depends on video flow
- `create_media_post` — low-level media post create wrapper
- `create_share_link` — create media post share link
- `upscale_video` — call video upscale endpoint
- `imagine_agent_start` / `imagine_agent_send` / `imagine_agent_read` — experimental app-chat agent tools

## Security

This project is intended for local, authorized use with your own Grok account. Do not commit cookies, tokens, generated session data, or private outputs.
