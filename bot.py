"""Telegram approval bot for grok-cookie-mcp.

Runs as a separate process alongside the MCP server. Talks to Grok through the
same helper functions in ``server.py`` (no duplicated cookie/CDP logic).

Flow (single owner, restricted by chat id):

    /series <topic>                   plan a series (Grok call, may take ~1 min)
    /list                             show series files on disk
    /open <id>                        pick a saved series
    /show <id>                        print status
    (inline buttons per scene)        approve / regenerate / edit
    approve  -> generate_video + post to Telegram
    regen    -> plan_series-like reroll of that single scene prompt
    edit     -> ForceReply -> new prompt goes into the scene

Persistence keeps its home in ``series/<id>.json``. The bot only wraps that state.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardRemove,
)

sys.path.insert(0, str(Path(__file__).parent))

from server import (  # noqa: E402
    OUTPUT_DIR,
    SERIES_DIR,
    build_plan_prompt,
    create_and_wait_media,
    download_asset,
    find_scene,
    grok_chat,
    grok_chat_via_ui,
    guess_extension,
    list_series_files,
    load_cookie_file,
    load_series,
    plan_series,
    save_series,
    start_video_generation,
    extract_video_ids,
    video_payload,
    wait_for_video_url,
    _now_iso,
)

log = logging.getLogger("grok-approval-bot")

BOT_TOKEN = os.environ.get("GROK_TG_BOT_TOKEN")
OWNER_CHAT_ID = os.environ.get("GROK_TG_CHAT_ID")
DEFAULT_ACCOUNT = os.environ.get("GROK_APPROVAL_ACCOUNT", "1")


def _read_env_file() -> None:
    """Same .env loader as server.py, in case bot is started without pre-loaded env."""
    env_path = Path(__file__).with_name(".env")
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


_read_env_file()
BOT_TOKEN = BOT_TOKEN or os.environ.get("GROK_TG_BOT_TOKEN")
OWNER_CHAT_ID = OWNER_CHAT_ID or os.environ.get("GROK_TG_CHAT_ID")


# ---------------------------------------------------------------------------
# state
# ---------------------------------------------------------------------------

# per-owner "current series" pointer. FSM would be overkill for a one-person bot.
CURRENT_SERIES: dict[int, str] = {}

# in-flight per-scene tasks so we can prevent double-clicks and cancel edits
IN_FLIGHT: dict[str, asyncio.Task[Any]] = {}

# per-chat pending edit target: {chat_id: (series_id, ep, scene)}
PENDING_EDIT: dict[int, tuple[str, int, int]] = {}


def scene_key(series_id: str, ep: int, sn: int) -> str:
    return f"{series_id}#{ep}.{sn}"


# ---------------------------------------------------------------------------
# access control
# ---------------------------------------------------------------------------

def is_owner(chat_id: int) -> bool:
    if not OWNER_CHAT_ID:
        return True
    try:
        return int(chat_id) == int(OWNER_CHAT_ID)
    except ValueError:
        return False


async def guard(msg: Message | CallbackQuery) -> bool:
    chat_id = msg.chat.id if isinstance(msg, Message) else (msg.message.chat.id if msg.message else 0)
    if not is_owner(chat_id):
        if isinstance(msg, Message):
            await msg.answer("Not authorized.")
        else:
            await msg.answer("Not authorized.", show_alert=True)
        return False
    return True


# ---------------------------------------------------------------------------
# rendering helpers
# ---------------------------------------------------------------------------

def _scene_status(scene: dict[str, Any]) -> str:
    st = scene.get("status", "planned")
    icons = {
        "planned": "⏳",
        "approved": "✅",
        "regenerating": "♻️",
        "video_ready": "🎬",
        "video_error": "⚠️",
    }
    return icons.get(st, "•")


def format_scene(state: dict[str, Any], ep: int, sn: int) -> str:
    scene = find_scene(state, ep, sn)
    lines = [
        f"<b>{state.get('title') or state.get('topic')}</b>",
        f"E{ep} · S{sn} · {_scene_status(scene)} {scene.get('status', 'planned')}",
        "",
        f"<i>{(scene.get('description') or '')[:250]}</i>",
        "",
        f"<code>{(scene.get('prompt') or '')[:900]}</code>",
    ]
    if scene.get("post_id"):
        lines.append(f"post_id: <code>{scene['post_id']}</code>")
    if scene.get("video_url"):
        lines.append(f"video: {scene['video_url']}")
    return "\n".join(lines)


def scene_kb(series_id: str, ep: int, sn: int, status: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if status in {"planned", "regenerating", "video_error"}:
        rows.append([
            InlineKeyboardButton(text="✅ Одобрить", callback_data=f"appr|{series_id}|{ep}|{sn}"),
            InlineKeyboardButton(text="♻️ Регенерировать", callback_data=f"regen|{series_id}|{ep}|{sn}"),
        ])
        rows.append([
            InlineKeyboardButton(text="✏️ Правки", callback_data=f"edit|{series_id}|{ep}|{sn}"),
        ])
    elif status == "approved":
        rows.append([
            InlineKeyboardButton(text="🎬 Сгенерировать видео", callback_data=f"video|{series_id}|{ep}|{sn}"),
            InlineKeyboardButton(text="↩️ Отменить одобрение", callback_data=f"unappr|{series_id}|{ep}|{sn}"),
        ])
    elif status == "video_ready":
        rows.append([
            InlineKeyboardButton(text="🔁 Перегенерировать видео", callback_data=f"video|{series_id}|{ep}|{sn}"),
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def send_scene_card(
    bot: Bot,
    chat_id: int,
    state: dict[str, Any],
    ep: int,
    sn: int,
    preview_path: Path | None = None,
) -> None:
    scene = find_scene(state, ep, sn)
    text = format_scene(state, ep, sn)
    kb = scene_kb(state["id"], ep, sn, scene.get("status", "planned"))
    if preview_path and preview_path.exists():
        await bot.send_photo(chat_id, FSInputFile(preview_path), caption=text[:1024], reply_markup=kb, parse_mode=ParseMode.HTML)
    else:
        await bot.send_message(chat_id, text, reply_markup=kb, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


# ---------------------------------------------------------------------------
# bot init
# ---------------------------------------------------------------------------

dp = Dispatcher(storage=MemoryStorage())


@dp.message(CommandStart())
async def cmd_start(msg: Message) -> None:
    if not await guard(msg):
        return
    await msg.answer(
        "🎬 <b>Grok Farm — approval bot</b>\n\n"
        "/series &lt;topic&gt; — spin up a plan\n"
        "/list — series on disk\n"
        "/open &lt;id&gt; — pick one\n"
        "/show — preview current series scenes\n"
        "/accounts — cookie files on disk\n",
        parse_mode=ParseMode.HTML,
    )


@dp.message(Command("accounts"))
async def cmd_accounts(msg: Message) -> None:
    if not await guard(msg):
        return
    from server import list_accounts as _la
    accs = _la()
    await msg.answer("Accounts: " + (", ".join(accs) if accs else "(none)"))


@dp.message(Command("list"))
async def cmd_list(msg: Message) -> None:
    if not await guard(msg):
        return
    items = list_series_files()
    if not items:
        await msg.answer("No series yet. Use /series &lt;topic&gt;.", parse_mode=ParseMode.HTML)
        return
    lines = ["<b>Series on disk:</b>"]
    for it in items[-20:]:
        lines.append(f"• <code>{it.get('id')}</code> — {it.get('topic')} ({it.get('scenes')} scenes)")
    lines.append("\n<i>Use</i> /open &lt;id&gt;")
    await msg.answer("\n".join(lines), parse_mode=ParseMode.HTML)


@dp.message(Command("open"))
async def cmd_open(msg: Message) -> None:
    if not await guard(msg):
        return
    parts = (msg.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await msg.answer("Usage: /open &lt;series_id&gt;", parse_mode=ParseMode.HTML)
        return
    sid = parts[1].strip()
    try:
        state = load_series(sid)
    except FileNotFoundError:
        await msg.answer(f"No such series: <code>{sid}</code>", parse_mode=ParseMode.HTML)
        return
    CURRENT_SERIES[msg.chat.id] = state["id"]
    await msg.answer(f"Opened <code>{state['id']}</code>. Use /show to see scenes.", parse_mode=ParseMode.HTML)


@dp.message(Command("show"))
async def cmd_show(msg: Message) -> None:
    if not await guard(msg):
        return
    parts = (msg.text or "").split(maxsplit=1)
    sid = parts[1].strip() if len(parts) > 1 else CURRENT_SERIES.get(msg.chat.id)
    if not sid:
        await msg.answer("No current series. /open &lt;id&gt; or pass id: /show &lt;id&gt;", parse_mode=ParseMode.HTML)
        return
    try:
        state = load_series(sid)
    except FileNotFoundError:
        await msg.answer(f"No such series: <code>{sid}</code>", parse_mode=ParseMode.HTML)
        return
    CURRENT_SERIES[msg.chat.id] = state["id"]
    header = f"<b>{state.get('title') or state.get('topic')}</b>\n<code>{state['id']}</code>"
    if state.get("logline"):
        header += f"\n<i>{state['logline']}</i>"
    await msg.answer(header, parse_mode=ParseMode.HTML)
    for episode in state.get("episodes", []):
        ep = int(episode["episode_number"])
        for scene in episode.get("scenes", []):
            await send_scene_card(msg.bot, msg.chat.id, state, ep, int(scene["scene_number"]))


@dp.message(Command("series"))
async def cmd_series(msg: Message) -> None:
    if not await guard(msg):
        return
    parts = (msg.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await msg.answer("Usage: /series &lt;topic&gt;", parse_mode=ParseMode.HTML)
        return
    topic = parts[1].strip()
    await msg.answer(f"🧠 Planning series: <i>{topic[:200]}</i>\nGrok call in progress, this can take 30-90 s…", parse_mode=ParseMode.HTML)
    try:
        cookies = load_cookie_file(DEFAULT_ACCOUNT)
    except Exception as exc:
        await msg.answer(f"cookie file not found: {exc}")
        return
    try:
        state = await plan_series(cookies, topic)
    except Exception as exc:
        log.exception("plan_series failed")
        await msg.answer(f"plan_series failed: <code>{str(exc)[:400]}</code>", parse_mode=ParseMode.HTML)
        return
    CURRENT_SERIES[msg.chat.id] = state["id"]
    if state.get("plan_parse_error"):
        await msg.answer(
            f"⚠️ Plan parse warning: <code>{str(state['plan_parse_error'])[:400]}</code>\nRaw text kept in series/{state['id']}.json",
            parse_mode=ParseMode.HTML,
        )
    header = f"<b>{state.get('title') or topic}</b>\n<code>{state['id']}</code>"
    if state.get("logline"):
        header += f"\n<i>{state['logline']}</i>"
    await msg.answer(header, parse_mode=ParseMode.HTML)
    if not state.get("episodes"):
        await msg.answer("No episodes parsed. Check the raw text in series file and re-plan.")
        return
    for episode in state["episodes"]:
        ep = int(episode["episode_number"])
        for scene in episode.get("scenes", []):
            await send_scene_card(msg.bot, msg.chat.id, state, ep, int(scene["scene_number"]))


# ---------------------------------------------------------------------------
# callback handlers
# ---------------------------------------------------------------------------

@dp.callback_query(F.data.startswith("appr|"))
async def cb_approve(cb: CallbackQuery) -> None:
    if not await guard(cb):
        return
    _, sid, ep, sn = cb.data.split("|")
    state = load_series(sid)
    scene = find_scene(state, int(ep), int(sn))
    scene["status"] = "approved"
    scene["approved_at"] = _now_iso()
    state["updated_at"] = _now_iso()
    save_series(state)
    await cb.answer("Одобрено")
    kb = scene_kb(sid, int(ep), int(sn), "approved")
    try:
        await cb.message.edit_reply_markup(reply_markup=kb)
    except Exception:
        pass


@dp.callback_query(F.data.startswith("unappr|"))
async def cb_unapprove(cb: CallbackQuery) -> None:
    if not await guard(cb):
        return
    _, sid, ep, sn = cb.data.split("|")
    state = load_series(sid)
    scene = find_scene(state, int(ep), int(sn))
    scene["status"] = "planned"
    state["updated_at"] = _now_iso()
    save_series(state)
    await cb.answer("Возвращено в planned")
    kb = scene_kb(sid, int(ep), int(sn), "planned")
    try:
        await cb.message.edit_reply_markup(reply_markup=kb)
    except Exception:
        pass


@dp.callback_query(F.data.startswith("edit|"))
async def cb_edit(cb: CallbackQuery) -> None:
    if not await guard(cb):
        return
    _, sid, ep, sn = cb.data.split("|")
    PENDING_EDIT[cb.message.chat.id] = (sid, int(ep), int(sn))
    await cb.answer()
    await cb.message.reply(
        f"✏️ Пришли новый prompt для сцены E{ep}·S{sn}.\n<i>Один следующий текстовый месседж заменит prompt целиком.</i>",
        parse_mode=ParseMode.HTML,
    )


@dp.callback_query(F.data.startswith("regen|"))
async def cb_regen(cb: CallbackQuery) -> None:
    if not await guard(cb):
        return
    _, sid, ep, sn = cb.data.split("|")
    key = scene_key(sid, int(ep), int(sn))
    if key in IN_FLIGHT and not IN_FLIGHT[key].done():
        await cb.answer("Уже пересобирается", show_alert=True)
        return
    await cb.answer("Пересобираю prompt…")
    IN_FLIGHT[key] = asyncio.create_task(_do_regen(cb, sid, int(ep), int(sn)))


async def _do_regen(cb: CallbackQuery, sid: str, ep: int, sn: int) -> None:
    try:
        state = load_series(sid)
        scene = find_scene(state, ep, sn)
        old = scene.get("prompt", "")
        style = (state.get("settings") or {}).get("style") or (state.get("consistency") or {}).get("style") or ""
        instruction = (
            "Rewrite this single Grok Imagine scene prompt with a fresh interpretation. "
            "Keep the same subject, aspect ratio and duration; vary the composition, lighting or motion. "
            "Reply with ONLY the new prompt text — no JSON, no bullet points."
            f"\n\nSubject/style constraint: {style or 'cinematic'}"
            f"\n\nExisting prompt:\n{old}"
        )
        cookies = load_cookie_file(DEFAULT_ACCOUNT)
        try:
            new_text, _cid, _ = await grok_chat(cookies, instruction)
        except Exception:
            new_text, _cid, _ = await grok_chat_via_ui(cookies, instruction, mode="agent", timeout=180)
        new_prompt = (new_text or "").strip()
        # strip surrounding quotes if any
        if new_prompt.startswith("```"):
            new_prompt = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", new_prompt).strip()
        if not new_prompt:
            await cb.message.reply("♻️ regen returned empty text, aborting")
            return
        scene["prompt"] = new_prompt
        scene["status"] = "planned"
        state["updated_at"] = _now_iso()
        save_series(state)
        await cb.message.reply(f"♻️ Новый prompt E{ep}·S{sn}:\n<code>{new_prompt[:900]}</code>", parse_mode=ParseMode.HTML)
        await send_scene_card(cb.bot, cb.message.chat.id, state, ep, sn)
    except Exception as exc:
        log.exception("regen failed")
        try:
            await cb.message.reply(f"regen failed: <code>{str(exc)[:400]}</code>", parse_mode=ParseMode.HTML)
        except Exception:
            pass


@dp.callback_query(F.data.startswith("video|"))
async def cb_video(cb: CallbackQuery) -> None:
    if not await guard(cb):
        return
    _, sid, ep, sn = cb.data.split("|")
    key = scene_key(sid, int(ep), int(sn))
    if key in IN_FLIGHT and not IN_FLIGHT[key].done():
        await cb.answer("Видео уже в работе", show_alert=True)
        return
    await cb.answer("Стартую видео (2-4 мин)…")
    IN_FLIGHT[key] = asyncio.create_task(_do_video(cb, sid, int(ep), int(sn)))


async def _do_video(cb: CallbackQuery, sid: str, ep: int, sn: int) -> None:
    chat_id = cb.message.chat.id
    try:
        state = load_series(sid)
        scene = find_scene(state, ep, sn)
        prompt = scene.get("prompt")
        if not prompt:
            await cb.bot.send_message(chat_id, f"E{ep}·S{sn}: no prompt to render")
            return
        cookies = load_cookie_file(DEFAULT_ACCOUNT)

        # 1. parent post (image) -- Grok needs it for video gen
        status_msg = await cb.bot.send_message(chat_id, f"🖼️ E{ep}·S{sn}: seeding parent image…")
        parent_post_id = scene.get("parent_post_id")
        if not parent_post_id:
            img = await create_and_wait_media(cookies, "MEDIA_POST_TYPE_IMAGE", prompt, timeout=180)
            parent_post_id = img.get("post_id")
            if not parent_post_id:
                await status_msg.edit_text(f"E{ep}·S{sn}: no parent post id, aborting")
                return
            scene["parent_post_id"] = parent_post_id
            save_series(state)

        # 2. real video
        await status_msg.edit_text(f"🎬 E{ep}·S{sn}: video gen started (parent {parent_post_id[:8]}…)")
        vp = video_payload(
            {
                "prompt": prompt,
                "aspect_ratio": scene.get("aspect_ratio") or "2:3",
                "resolution": "480p",
                "duration": int(scene.get("duration") or (state.get("settings") or {}).get("duration") or 6),
            },
            parent_post_id,
        )
        events = await start_video_generation(cookies, vp)
        ids = extract_video_ids(events)
        target = ids.get("videoPostId") or parent_post_id
        result = await wait_for_video_url(cookies, target, timeout=360)
        if result.get("status") != "ready":
            scene["status"] = "video_error"
            state["updated_at"] = _now_iso()
            save_series(state)
            await status_msg.edit_text(f"E{ep}·S{sn}: video not ready (status={result.get('status')})")
            return

        video_urls = result.get("video_urls") or []
        if not video_urls:
            scene["status"] = "video_error"
            save_series(state)
            await status_msg.edit_text(f"E{ep}·S{sn}: no video url returned")
            return
        url = video_urls[0]

        # 3. download + upload
        await status_msg.edit_text(f"⬇️ E{ep}·S{sn}: downloading video…")
        OUTPUT_DIR.mkdir(exist_ok=True)
        suffix = guess_extension(url, ".mp4")
        dest = OUTPUT_DIR / f"{target}_scene_{ep}_{sn}{suffix}"
        await download_asset(cookies, url, dest)
        await status_msg.edit_text(f"📤 E{ep}·S{sn}: uploading to Telegram…")
        caption = f"E{ep} · S{sn} — {state.get('title') or state.get('topic')}"
        await cb.bot.send_video(chat_id, FSInputFile(dest), caption=caption[:1024])
        try:
            await status_msg.delete()
        except Exception:
            pass

        scene["status"] = "video_ready"
        scene["video_post_id"] = target
        scene["video_url"] = url
        scene["video_file"] = str(dest)
        state["updated_at"] = _now_iso()
        save_series(state)
    except Exception as exc:
        log.exception("video failed")
        try:
            state = load_series(sid)
            scene = find_scene(state, ep, sn)
            scene["status"] = "video_error"
            scene["video_error"] = str(exc)[:500]
            state["updated_at"] = _now_iso()
            save_series(state)
        except Exception:
            pass
        try:
            await cb.bot.send_message(chat_id, f"video failed E{ep}·S{sn}: <code>{str(exc)[:400]}</code>", parse_mode=ParseMode.HTML)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# free-form text: only meaningful right after "✏️ Правки"
# ---------------------------------------------------------------------------

@dp.message(F.text & ~F.text.startswith("/"))
async def on_text(msg: Message) -> None:
    if not await guard(msg):
        return
    pending = PENDING_EDIT.pop(msg.chat.id, None)
    if not pending:
        return
    sid, ep, sn = pending
    try:
        state = load_series(sid)
    except FileNotFoundError:
        await msg.answer(f"No such series: <code>{sid}</code>", parse_mode=ParseMode.HTML)
        return
    scene = find_scene(state, ep, sn)
    scene["prompt"] = (msg.text or "").strip()
    scene["status"] = "planned"
    state["updated_at"] = _now_iso()
    save_series(state)
    await msg.reply(f"✏️ Prompt E{ep}·S{sn} updated.")
    await send_scene_card(msg.bot, msg.chat.id, state, ep, sn)


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

async def _run() -> None:
    if not BOT_TOKEN:
        raise SystemExit("GROK_TG_BOT_TOKEN not set (put it into .env)")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    SERIES_DIR.mkdir(exist_ok=True)
    OUTPUT_DIR.mkdir(exist_ok=True)
    bot = Bot(token=BOT_TOKEN)
    me = await bot.get_me()
    log.info("bot online as @%s", me.username)
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
