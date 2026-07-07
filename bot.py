"""Telegram approval bot for grok-cookie-mcp.

Owner-only aiogram bot that drives Grok via the same helpers as server.py.

UX:
    - persistent bottom menu (ReplyKeyboardMarkup)
    - series header card with bulk buttons (approve-all / render-approved)
    - wizard-style scene navigation (⬅ / ✅ / ➡)
    - one video button that calls Grok Agent mode (video comes back voiced)

Callback data schema (pipe-separated, keep sid last-ish for length safety):
    m|<cmd>                     top-level menu buttons
    s|<cmd>|<sid>               series-level buttons
    w|<cmd>|<sid>|<ep>|<sn>     wizard scene actions
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
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

sys.path.insert(0, str(Path(__file__).parent))

from server import (  # noqa: E402
    OUTPUT_DIR,
    SERIES_DIR,
    collect_grok_text,
    collect_media_urls,
    create_and_wait_media,
    deep_values,
    download_asset,
    extract_video_ids,
    find_scene,
    get_media_post,
    grok_chat,
    grok_chat_via_ui,
    guess_extension,
    list_series_files,
    load_cookie_file,
    load_series,
    plan_series,
    save_series,
    send_agent_prompt,
    start_agent_conversation,
    start_video_generation,
    video_payload,
    wait_for_video_url,
    _now_iso,
)

log = logging.getLogger("grok-approval-bot")


def _read_env_file() -> None:
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
BOT_TOKEN = os.environ.get("GROK_TG_BOT_TOKEN")
OWNER_CHAT_ID = os.environ.get("GROK_TG_CHAT_ID")
DEFAULT_ACCOUNT = os.environ.get("GROK_APPROVAL_ACCOUNT", "1")


# ---------------------------------------------------------------------------
# state
# ---------------------------------------------------------------------------

# per-chat pointer to the currently open series
CURRENT_SERIES: dict[int, str] = {}

# per-chat wizard cursor: chat -> (series_id, ep, sn)
WIZARD_CURSOR: dict[int, tuple[str, int, int]] = {}

# in-flight per-scene tasks (regen / video), prevent double-clicks
IN_FLIGHT: dict[str, asyncio.Task[Any]] = {}

# per-chat pending edit target: {chat_id: (series_id, ep, sn)}
PENDING_EDIT: dict[int, tuple[str, int, int]] = {}

# per-chat active cookie account name
ACTIVE_ACCOUNT: dict[int, str] = {}

# quota_info cache: {account_name: (data_json, fetched_at_unix)}
QUOTA_CACHE: dict[str, tuple[dict[str, Any], float]] = {}


def scene_key(series_id: str, ep: int, sn: int) -> str:
    return f"{series_id}#{ep}.{sn}"


def account_for(chat_id: int) -> str:
    return ACTIVE_ACCOUNT.get(chat_id) or DEFAULT_ACCOUNT


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


async def guard(evt: Message | CallbackQuery) -> bool:
    chat_id = evt.chat.id if isinstance(evt, Message) else (evt.message.chat.id if evt.message else 0)
    if is_owner(chat_id):
        return True
    if isinstance(evt, Message):
        await evt.answer("Not authorized.")
    else:
        await evt.answer("Not authorized.", show_alert=True)
    return False


# ---------------------------------------------------------------------------
# keyboards
# ---------------------------------------------------------------------------

BTN_NEW = "🎬 Новый сериал"
BTN_LIST = "📋 Мои сериалы"
BTN_CURRENT = "🔄 Открыть текущий"
BTN_ACCOUNTS = "⚙️ Аккаунты"
BTN_HELP = "❓ Помощь"

MENU_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_NEW), KeyboardButton(text=BTN_LIST)],
        [KeyboardButton(text=BTN_CURRENT), KeyboardButton(text=BTN_ACCOUNTS)],
        [KeyboardButton(text=BTN_HELP)],
    ],
    resize_keyboard=True,
    is_persistent=True,
)


def series_header_kb(state: dict[str, Any]) -> InlineKeyboardMarkup:
    sid = state["id"]
    rows: list[list[InlineKeyboardButton]] = []
    rows.append([
        InlineKeyboardButton(text="▶️ Пройти по сценам", callback_data=f"s|wiz|{sid}"),
    ])
    rows.append([
        InlineKeyboardButton(text="✅ Одобрить все", callback_data=f"s|appall|{sid}"),
        InlineKeyboardButton(text="🎬 Рендер одобренных", callback_data=f"s|renderall|{sid}"),
    ])
    rows.append([
        InlineKeyboardButton(text="🔄 Обновить", callback_data=f"s|refresh|{sid}"),
        InlineKeyboardButton(text="🗑 Удалить", callback_data=f"s|del|{sid}"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def wizard_kb(state: dict[str, Any], ep: int, sn: int) -> InlineKeyboardMarkup:
    sid = state["id"]
    scene = find_scene(state, ep, sn)
    status = scene.get("status", "planned")

    prev_ep, prev_sn = neighbor(state, ep, sn, -1)
    next_ep, next_sn = neighbor(state, ep, sn, +1)

    nav_row: list[InlineKeyboardButton] = []
    if prev_ep is not None:
        nav_row.append(InlineKeyboardButton(text="⬅️", callback_data=f"w|prev|{sid}|{ep}|{sn}"))
    nav_row.append(InlineKeyboardButton(text=f"{ep}·{sn}", callback_data=f"w|noop|{sid}|{ep}|{sn}"))
    if next_ep is not None:
        nav_row.append(InlineKeyboardButton(text="➡️", callback_data=f"w|next|{sid}|{ep}|{sn}"))

    rows: list[list[InlineKeyboardButton]] = [nav_row]

    if status == "planned":
        rows.append([
            InlineKeyboardButton(text="✅ Одобрить", callback_data=f"w|appr|{sid}|{ep}|{sn}"),
            InlineKeyboardButton(text="♻️ Регенерировать", callback_data=f"w|regen|{sid}|{ep}|{sn}"),
        ])
        rows.append([
            InlineKeyboardButton(text="✏️ Правки", callback_data=f"w|edit|{sid}|{ep}|{sn}"),
            InlineKeyboardButton(text="🎬 Видео (Agent+voice)", callback_data=f"w|video|{sid}|{ep}|{sn}"),
        ])
    elif status == "approved":
        rows.append([
            InlineKeyboardButton(text="🎬 Видео (Agent+voice)", callback_data=f"w|video|{sid}|{ep}|{sn}"),
            InlineKeyboardButton(text="↩️ Снять одобрение", callback_data=f"w|unappr|{sid}|{ep}|{sn}"),
        ])
        rows.append([
            InlineKeyboardButton(text="✏️ Правки", callback_data=f"w|edit|{sid}|{ep}|{sn}"),
        ])
    elif status == "video_ready":
        rows.append([
            InlineKeyboardButton(text="🔁 Пересобрать видео", callback_data=f"w|video|{sid}|{ep}|{sn}"),
        ])
        rows.append([
            InlineKeyboardButton(text="✏️ Правки", callback_data=f"w|edit|{sid}|{ep}|{sn}"),
            InlineKeyboardButton(text="↩️ В planned", callback_data=f"w|unappr|{sid}|{ep}|{sn}"),
        ])
    elif status in {"regenerating", "video_error"}:
        rows.append([
            InlineKeyboardButton(text="♻️ Регенерировать", callback_data=f"w|regen|{sid}|{ep}|{sn}"),
            InlineKeyboardButton(text="🎬 Видео", callback_data=f"w|video|{sid}|{ep}|{sn}"),
        ])
        rows.append([
            InlineKeyboardButton(text="✏️ Правки", callback_data=f"w|edit|{sid}|{ep}|{sn}"),
        ])

    rows.append([
        InlineKeyboardButton(text="📄 К сериалу", callback_data=f"s|refresh|{sid}"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ---------------------------------------------------------------------------
# helpers over series state
# ---------------------------------------------------------------------------

def all_scenes(state: dict[str, Any]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for episode in state.get("episodes", []):
        ep = int(episode.get("episode_number", 0))
        for scene in episode.get("scenes", []):
            out.append((ep, int(scene.get("scene_number", 0))))
    return out


def neighbor(state: dict[str, Any], ep: int, sn: int, delta: int) -> tuple[int | None, int | None]:
    scenes = all_scenes(state)
    try:
        idx = scenes.index((ep, sn))
    except ValueError:
        return None, None
    j = idx + delta
    if 0 <= j < len(scenes):
        return scenes[j]
    return None, None


def scene_status_icon(status: str) -> str:
    return {
        "planned": "⏳",
        "approved": "✅",
        "regenerating": "♻️",
        "video_ready": "🎬",
        "video_error": "⚠️",
    }.get(status, "•")


def progress_line(state: dict[str, Any]) -> str:
    total = len(all_scenes(state))
    approved = 0
    videos = 0
    for episode in state.get("episodes", []):
        for scene in episode.get("scenes", []):
            st = scene.get("status", "planned")
            if st in {"approved", "video_ready"}:
                approved += 1
            if st == "video_ready":
                videos += 1
    return f"✅ {approved}/{total}   🎬 {videos}/{total}"


def format_series_header(state: dict[str, Any]) -> str:
    lines = [
        f"<b>{state.get('title') or state.get('topic') or 'Untitled'}</b>",
        f"<code>{state['id']}</code>",
    ]
    if state.get("logline"):
        lines.append(f"<i>{state['logline']}</i>")
    lines.append(f"\n{progress_line(state)}")
    if state.get("plan_parse_error"):
        lines.append(f"⚠️ parse: <code>{str(state['plan_parse_error'])[:120]}</code>")
    scenes = all_scenes(state)
    if scenes:
        lines.append("")
        for episode in state.get("episodes", []):
            ep = int(episode.get("episode_number", 0))
            title = episode.get("title") or ""
            lines.append(f"<b>E{ep}</b> {title}")
            for scene in episode.get("scenes", []):
                sn = int(scene.get("scene_number", 0))
                st = scene.get("status", "planned")
                icon = scene_status_icon(st)
                desc = (scene.get("description") or "").strip()
                lines.append(f"  {icon} S{sn}. {desc[:100]}")
    return "\n".join(lines)


def format_wizard(state: dict[str, Any], ep: int, sn: int) -> str:
    scenes = all_scenes(state)
    idx = scenes.index((ep, sn)) + 1 if (ep, sn) in scenes else 0
    scene = find_scene(state, ep, sn)
    st = scene.get("status", "planned")
    lines = [
        f"<b>{state.get('title') or state.get('topic')}</b>  ({idx}/{len(scenes)})",
        f"E{ep} · S{sn} · {scene_status_icon(st)} {st}",
        "",
        f"<i>{(scene.get('description') or '')[:250]}</i>",
        "",
        f"<code>{(scene.get('prompt') or '')[:900]}</code>",
    ]
    if scene.get("video_url"):
        lines.append("")
        lines.append(f"🎬 <code>{scene.get('video_post_id', '')}</code>")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

async def send_series_header(bot: Bot, chat_id: int, state: dict[str, Any], reply_to: int | None = None) -> None:
    await bot.send_message(
        chat_id,
        format_series_header(state),
        reply_markup=series_header_kb(state),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def send_wizard(bot: Bot, chat_id: int, state: dict[str, Any], ep: int, sn: int) -> None:
    WIZARD_CURSOR[chat_id] = (state["id"], ep, sn)
    await bot.send_message(
        chat_id,
        format_wizard(state, ep, sn),
        reply_markup=wizard_kb(state, ep, sn),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def edit_wizard(cb: CallbackQuery, state: dict[str, Any], ep: int, sn: int) -> None:
    WIZARD_CURSOR[cb.message.chat.id] = (state["id"], ep, sn)
    try:
        await cb.message.edit_text(
            format_wizard(state, ep, sn),
            reply_markup=wizard_kb(state, ep, sn),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except Exception:
        # if the message is a photo/media, fall back to a fresh send
        await send_wizard(cb.bot, cb.message.chat.id, state, ep, sn)


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
        "Внизу — постоянное меню. Или команды:\n"
        "/series &lt;topic&gt; · /list · /open &lt;id&gt; · /show\n",
        parse_mode=ParseMode.HTML,
        reply_markup=MENU_KB,
    )


@dp.message(Command("menu"))
async def cmd_menu(msg: Message) -> None:
    if not await guard(msg):
        return
    await msg.answer("Меню:", reply_markup=MENU_KB)


@dp.message(F.text == BTN_HELP)
async def btn_help(msg: Message) -> None:
    if not await guard(msg):
        return
    await msg.answer(
        "🎬 <b>Как это работает:</b>\n\n"
        "1) <b>⚙️ Аккаунты</b> — выбери активный cookie-акк, посмотри остаточную квоту.\n"
        "2) <b>🎬 Новый сериал</b> → присылай тему следующим сообщением, Grok Agent пишет план.\n"
        "3) На карточке сериала — <i>Пройти по сценам</i>: wizard ⬅/➡, ✅ Одобрить · ♻️ Регенерировать · ✏️ Правки.\n"
        "4) <b>🎬 Видео (Agent+voice)</b> — рендер сцены через агентский режим, приходит уже с озвучкой.\n"
        "5) На карточке сериала: <i>Одобрить все</i> и <i>Рендер одобренных</i> — массовые действия.\n\n"
        "Стейт живёт в <code>series/&lt;id&gt;.json</code>, видео — в <code>output/</code>.",
        parse_mode=ParseMode.HTML,
    )


# ---------------------------------------------------------------------------
# accounts + quota
# ---------------------------------------------------------------------------

def _pick_number_pairs(data: Any, prefix: str = "") -> list[tuple[str, Any]]:
    """Pull integer/float fields anywhere in the quota tree so we can pretty-print."""
    out: list[tuple[str, Any]] = []
    if isinstance(data, dict):
        for k, v in data.items():
            name = f"{prefix}{k}" if not prefix else f"{prefix}.{k}"
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                out.append((name, v))
            elif isinstance(v, (dict, list)):
                out.extend(_pick_number_pairs(v, name))
            elif isinstance(v, str) and v and ("Time" in k or "time" in k or "reset" in k.lower()):
                out.append((name, v))
    elif isinstance(data, list):
        for i, item in enumerate(data):
            out.extend(_pick_number_pairs(item, f"{prefix}[{i}]"))
    return out


def _format_quota(data: Any) -> str:
    pairs = _pick_number_pairs(data)
    if not pairs:
        return "<i>quota_info вернул пусто</i>"
    lines: list[str] = []
    seen = set()
    for k, v in pairs:
        short = k.split(".")[-1]
        if short in seen:
            continue
        seen.add(short)
        lines.append(f"<code>{k}</code>: <b>{v}</b>")
    return "\n".join(lines[:24])


async def _fetch_quota(account: str) -> dict[str, Any]:
    from server import grok_request
    cookies = load_cookie_file(account)
    return await grok_request("POST", "/rest/media/imagine/quota_info", cookies, {})


def _accounts_kb(chat_id: int, accounts: list[str]) -> InlineKeyboardMarkup:
    active = account_for(chat_id)
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for name in accounts:
        label = ("✅ " if name == active else "🔘 ") + name
        row.append(InlineKeyboardButton(text=label, callback_data=f"a|switch|{name}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([
        InlineKeyboardButton(text="🔄 Обновить квоту", callback_data=f"a|refresh|{active}"),
        InlineKeyboardButton(text="📊 Все квоты", callback_data="a|refreshall|_"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _accounts_text(chat_id: int, accounts: list[str]) -> str:
    active = account_for(chat_id)
    lines = [
        "<b>⚙️ Аккаунты</b>",
        f"Активный: <code>{active}</code>",
        "",
    ]
    for name in accounts:
        icon = "✅" if name == active else "🔘"
        cached = QUOTA_CACHE.get(name)
        if cached:
            data, ts = cached
            age = int(asyncio.get_event_loop().time() - ts)
            summary = _format_quota(data)
            lines.append(f"{icon} <b>{name}</b>  <i>({age}s назад)</i>")
            lines.append(summary)
        else:
            lines.append(f"{icon} <b>{name}</b>  <i>квота не запрошена</i>")
        lines.append("")
    return "\n".join(lines).strip()


async def _send_accounts_panel(bot: Bot, chat_id: int, edit_message_id: int | None = None) -> None:
    from server import list_accounts as _la
    accounts = _la()
    if not accounts:
        await bot.send_message(chat_id, "Нет cookie-файлов в <code>cookies/</code>. Положи туда JSON от Cookie-Editor.", parse_mode=ParseMode.HTML)
        return
    text = _accounts_text(chat_id, accounts)
    kb = _accounts_kb(chat_id, accounts)
    if edit_message_id is not None:
        try:
            await bot.edit_message_text(text, chat_id=chat_id, message_id=edit_message_id, reply_markup=kb, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
            return
        except Exception:
            pass
    await bot.send_message(chat_id, text, reply_markup=kb, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


@dp.callback_query(F.data.startswith("a|"))
async def cb_accounts(cb: CallbackQuery) -> None:
    if not await guard(cb):
        return
    _, cmd, name = cb.data.split("|", 2)
    from server import list_accounts as _la
    accounts = _la()

    if cmd == "switch":
        if name not in accounts:
            await cb.answer(f"Нет такого: {name}", show_alert=True)
            return
        ACTIVE_ACCOUNT[cb.message.chat.id] = name
        await cb.answer(f"→ {name}")
        try:
            data = await _fetch_quota(name)
            QUOTA_CACHE[name] = (data, asyncio.get_event_loop().time())
        except Exception as exc:
            log.warning("quota fetch failed for %s: %s", name, exc)
        await _send_accounts_panel(cb.bot, cb.message.chat.id, edit_message_id=cb.message.message_id)
        return

    if cmd == "refresh":
        target = name if name in accounts else account_for(cb.message.chat.id)
        await cb.answer(f"↻ {target}")
        try:
            data = await _fetch_quota(target)
            QUOTA_CACHE[target] = (data, asyncio.get_event_loop().time())
        except Exception as exc:
            await cb.bot.send_message(cb.message.chat.id, f"quota failed ({target}): <code>{str(exc)[:300]}</code>", parse_mode=ParseMode.HTML)
        await _send_accounts_panel(cb.bot, cb.message.chat.id, edit_message_id=cb.message.message_id)
        return

    if cmd == "refreshall":
        await cb.answer("Опрашиваю все аккаунты…")
        for acc in accounts:
            try:
                data = await _fetch_quota(acc)
                QUOTA_CACHE[acc] = (data, asyncio.get_event_loop().time())
            except Exception as exc:
                log.warning("quota fetch failed for %s: %s", acc, exc)
        await _send_accounts_panel(cb.bot, cb.message.chat.id, edit_message_id=cb.message.message_id)
        return

    await cb.answer(f"unknown a-cmd: {cmd}", show_alert=True)


@dp.message(F.text == BTN_NEW)
async def btn_new(msg: Message) -> None:
    if not await guard(msg):
        return
    PENDING_EDIT[msg.chat.id] = ("__new__", 0, 0)
    await msg.answer(
        "🧠 Пришли тему нового сериала одним сообщением. Например:\n"
        "<i>Yellow rubber duck world tour</i>",
        parse_mode=ParseMode.HTML,
    )


@dp.message(F.text == BTN_LIST)
async def btn_list(msg: Message) -> None:
    if not await guard(msg):
        return
    await _render_list(msg)


@dp.message(F.text == BTN_CURRENT)
async def btn_current(msg: Message) -> None:
    if not await guard(msg):
        return
    sid = CURRENT_SERIES.get(msg.chat.id)
    if not sid:
        await msg.answer("Нет открытого сериала. Жми <b>📋 Мои сериалы</b>.", parse_mode=ParseMode.HTML)
        return
    try:
        state = load_series(sid)
    except FileNotFoundError:
        CURRENT_SERIES.pop(msg.chat.id, None)
        await msg.answer("Текущий сериал исчез с диска.")
        return
    await send_series_header(msg.bot, msg.chat.id, state)


@dp.message(F.text == BTN_ACCOUNTS)
async def btn_accounts(msg: Message) -> None:
    if not await guard(msg):
        return
    await _send_accounts_panel(msg.bot, msg.chat.id)


@dp.message(Command("list"))
async def cmd_list(msg: Message) -> None:
    if not await guard(msg):
        return
    await _render_list(msg)


async def _render_list(msg: Message) -> None:
    items = list_series_files()
    if not items:
        await msg.answer("Пока пусто. Жми <b>🎬 Новый сериал</b>.", parse_mode=ParseMode.HTML, reply_markup=MENU_KB)
        return
    rows: list[list[InlineKeyboardButton]] = []
    for it in items[-20:]:
        sid = it.get("id") or Path(it.get("path", "")).stem
        topic = (it.get("topic") or sid)[:32]
        rows.append([InlineKeyboardButton(text=f"📄 {topic}", callback_data=f"s|open|{sid}")])
    await msg.answer(
        "<b>Твои сериалы:</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@dp.message(Command("open"))
async def cmd_open(msg: Message) -> None:
    if not await guard(msg):
        return
    parts = (msg.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await msg.answer("Usage: /open &lt;series_id&gt;", parse_mode=ParseMode.HTML)
        return
    await _open_series(msg.bot, msg.chat.id, parts[1].strip())


async def _open_series(bot: Bot, chat_id: int, sid: str) -> None:
    try:
        state = load_series(sid)
    except FileNotFoundError:
        await bot.send_message(chat_id, f"Нет сериала <code>{sid}</code>", parse_mode=ParseMode.HTML)
        return
    CURRENT_SERIES[chat_id] = state["id"]
    await send_series_header(bot, chat_id, state)


@dp.message(Command("show"))
async def cmd_show(msg: Message) -> None:
    if not await guard(msg):
        return
    parts = (msg.text or "").split(maxsplit=1)
    sid = parts[1].strip() if len(parts) > 1 else CURRENT_SERIES.get(msg.chat.id)
    if not sid:
        await msg.answer("Нет активного. /open &lt;id&gt; или <b>📋 Мои сериалы</b>", parse_mode=ParseMode.HTML)
        return
    await _open_series(msg.bot, msg.chat.id, sid)


@dp.message(Command("series"))
async def cmd_series(msg: Message) -> None:
    if not await guard(msg):
        return
    parts = (msg.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await msg.answer("Usage: /series &lt;topic&gt;", parse_mode=ParseMode.HTML)
        return
    await _plan_series_flow(msg, parts[1].strip())


async def _plan_series_flow(msg: Message, topic: str) -> None:
    await msg.answer(
        f"🧠 Планирую: <i>{topic[:200]}</i>\nGrok думает 30-90 сек…",
        parse_mode=ParseMode.HTML,
    )
    acc = account_for(msg.chat.id)
    try:
        cookies = load_cookie_file(acc)
    except Exception as exc:
        await msg.answer(f"cookie file not found ({acc}): {exc}")
        return
    try:
        state = await plan_series(cookies, topic)
    except Exception as exc:
        log.exception("plan_series failed")
        await msg.answer(f"plan_series failed: <code>{str(exc)[:400]}</code>", parse_mode=ParseMode.HTML)
        return
    CURRENT_SERIES[msg.chat.id] = state["id"]
    await send_series_header(msg.bot, msg.chat.id, state)


# ---------------------------------------------------------------------------
# free-form text (routes: new-series topic input, edit-prompt input)
# ---------------------------------------------------------------------------

MENU_TEXTS = {BTN_NEW, BTN_LIST, BTN_CURRENT, BTN_ACCOUNTS, BTN_HELP}


@dp.message(F.text & ~F.text.startswith("/"))
async def on_text(msg: Message) -> None:
    if not await guard(msg):
        return
    if (msg.text or "") in MENU_TEXTS:
        return  # handled by dedicated F.text handlers above
    pending = PENDING_EDIT.pop(msg.chat.id, None)
    if not pending:
        return
    sid, ep, sn = pending
    if sid == "__new__":
        await _plan_series_flow(msg, (msg.text or "").strip())
        return
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
    await msg.reply(f"✏️ Prompt E{ep}·S{sn} обновлён.")
    await send_wizard(msg.bot, msg.chat.id, state, ep, sn)


# ---------------------------------------------------------------------------
# series-level callbacks (s|<cmd>|<sid>)
# ---------------------------------------------------------------------------

@dp.callback_query(F.data.startswith("s|"))
async def cb_series(cb: CallbackQuery) -> None:
    if not await guard(cb):
        return
    _, cmd, sid = cb.data.split("|", 2)
    try:
        state = load_series(sid)
    except FileNotFoundError:
        await cb.answer("Сериал не найден", show_alert=True)
        return

    if cmd == "open":
        await cb.answer()
        CURRENT_SERIES[cb.message.chat.id] = state["id"]
        await send_series_header(cb.bot, cb.message.chat.id, state)
        return

    if cmd == "refresh":
        await cb.answer("↻")
        try:
            await cb.message.edit_text(
                format_series_header(state),
                reply_markup=series_header_kb(state),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except Exception:
            await send_series_header(cb.bot, cb.message.chat.id, state)
        return

    if cmd == "wiz":
        scenes = all_scenes(state)
        if not scenes:
            await cb.answer("В сериале нет сцен", show_alert=True)
            return
        # start at first non-video-ready scene, else first
        start = next((s for s in scenes if find_scene(state, *s).get("status") not in {"video_ready"}), scenes[0])
        await cb.answer()
        await send_wizard(cb.bot, cb.message.chat.id, state, *start)
        return

    if cmd == "appall":
        touched = 0
        for episode in state.get("episodes", []):
            for scene in episode.get("scenes", []):
                if scene.get("status") == "planned":
                    scene["status"] = "approved"
                    scene["approved_at"] = _now_iso()
                    touched += 1
        state["updated_at"] = _now_iso()
        save_series(state)
        await cb.answer(f"Одобрено: {touched}")
        try:
            await cb.message.edit_text(
                format_series_header(state),
                reply_markup=series_header_kb(state),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except Exception:
            pass
        return

    if cmd == "renderall":
        await cb.answer("Ставлю в очередь…")
        asyncio.create_task(_render_all_approved(cb.bot, cb.message.chat.id, state["id"]))
        return

    if cmd == "del":
        path = Path(list_series_files()[0]["path"]).with_name(f"{state['id']}.json") if False else None
        from server import series_path as _sp
        try:
            _sp(state["id"]).unlink(missing_ok=True)
        except Exception as exc:
            await cb.answer(f"delete failed: {exc}", show_alert=True)
            return
        CURRENT_SERIES.pop(cb.message.chat.id, None)
        await cb.answer("Удалено")
        try:
            await cb.message.edit_text(f"🗑 Удалён <code>{state['id']}</code>", parse_mode=ParseMode.HTML)
        except Exception:
            pass
        return

    await cb.answer(f"unknown cmd: {cmd}", show_alert=True)


# ---------------------------------------------------------------------------
# wizard-level callbacks (w|<cmd>|<sid>|<ep>|<sn>)
# ---------------------------------------------------------------------------

@dp.callback_query(F.data.startswith("w|"))
async def cb_wizard(cb: CallbackQuery) -> None:
    if not await guard(cb):
        return
    _, cmd, sid, ep_s, sn_s = cb.data.split("|", 4)
    ep, sn = int(ep_s), int(sn_s)
    try:
        state = load_series(sid)
    except FileNotFoundError:
        await cb.answer("Сериал не найден", show_alert=True)
        return

    if cmd == "noop":
        await cb.answer()
        return

    if cmd in {"prev", "next"}:
        target = neighbor(state, ep, sn, -1 if cmd == "prev" else +1)
        if target == (None, None):
            await cb.answer("Край", show_alert=False)
            return
        await cb.answer()
        await edit_wizard(cb, state, *target)
        return

    if cmd == "appr":
        scene = find_scene(state, ep, sn)
        scene["status"] = "approved"
        scene["approved_at"] = _now_iso()
        state["updated_at"] = _now_iso()
        save_series(state)
        await cb.answer("Одобрено ✅")
        nxt = neighbor(state, ep, sn, +1)
        if nxt != (None, None):
            await edit_wizard(cb, state, *nxt)
        else:
            await edit_wizard(cb, state, ep, sn)
        return

    if cmd == "unappr":
        scene = find_scene(state, ep, sn)
        scene["status"] = "planned"
        state["updated_at"] = _now_iso()
        save_series(state)
        await cb.answer("Снято одобрение")
        await edit_wizard(cb, state, ep, sn)
        return

    if cmd == "edit":
        PENDING_EDIT[cb.message.chat.id] = (sid, ep, sn)
        await cb.answer()
        await cb.message.reply(
            f"✏️ Пришли новый prompt для E{ep}·S{sn} одним сообщением.",
        )
        return

    if cmd == "regen":
        key = scene_key(sid, ep, sn)
        if key in IN_FLIGHT and not IN_FLIGHT[key].done():
            await cb.answer("Уже пересобирается", show_alert=True)
            return
        await cb.answer("♻️ Grok думает…")
        IN_FLIGHT[key] = asyncio.create_task(_do_regen(cb, sid, ep, sn))
        return

    if cmd == "video":
        key = scene_key(sid, ep, sn)
        if key in IN_FLIGHT and not IN_FLIGHT[key].done():
            await cb.answer("Видео уже в работе", show_alert=True)
            return
        await cb.answer("🎬 Agent запускает видео с озвучкой…")
        IN_FLIGHT[key] = asyncio.create_task(_do_agent_video(cb.bot, cb.message.chat.id, sid, ep, sn))
        return

    await cb.answer(f"unknown wcmd: {cmd}", show_alert=True)


# ---------------------------------------------------------------------------
# actions
# ---------------------------------------------------------------------------

async def _do_regen(cb: CallbackQuery, sid: str, ep: int, sn: int) -> None:
    try:
        state = load_series(sid)
        scene = find_scene(state, ep, sn)
        old = scene.get("prompt", "")
        style = (state.get("settings") or {}).get("style") or (state.get("consistency") or {}).get("style") or ""
        instruction = (
            "Rewrite this single Grok Imagine scene prompt with a fresh interpretation. "
            "Keep the same subject and aspect ratio; vary the composition, lighting or motion. "
            "Reply with ONLY the new prompt text — no JSON, no bullets, no preamble."
            f"\n\nStyle: {style or 'cinematic'}"
            f"\n\nExisting prompt:\n{old}"
        )
        cookies = load_cookie_file(account_for(cb.message.chat.id))
        try:
            new_text, _cid, _ = await grok_chat(cookies, instruction)
        except Exception:
            new_text, _cid, _ = await grok_chat_via_ui(cookies, instruction, mode="agent", timeout=180)
        new_prompt = (new_text or "").strip()
        if new_prompt.startswith("```"):
            new_prompt = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", new_prompt).strip()
        if not new_prompt:
            await cb.bot.send_message(cb.message.chat.id, "♻️ regen: пустой ответ, ничего не меняю")
            return
        scene["prompt"] = new_prompt
        scene["status"] = "planned"
        state["updated_at"] = _now_iso()
        save_series(state)
        await edit_wizard(cb, state, ep, sn)
    except Exception as exc:
        log.exception("regen failed")
        try:
            await cb.bot.send_message(cb.message.chat.id, f"regen failed: <code>{str(exc)[:400]}</code>", parse_mode=ParseMode.HTML)
        except Exception:
            pass


def _extract_agent_post_ids(events: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Walk Agent event stream, collect any post ids referenced.

    Grok Agent internally spawns media posts (image+video) as it works. Their ids
    show up as ``postId``, ``videoPostId``, ``parentPostId`` — grab all of them so
    we can poll each for a ready video URL.
    """
    video_ids: list[str] = []
    other_ids: list[str] = []
    for event in events:
        for node in deep_values(event):
            for key in ("videoPostId",):
                v = node.get(key)
                if isinstance(v, str) and v and v not in video_ids:
                    video_ids.append(v)
            for key in ("postId", "parentPostId", "id"):
                v = node.get(key)
                if isinstance(v, str) and re.fullmatch(r"[a-f0-9-]{20,}", v) and v not in other_ids:
                    other_ids.append(v)
    return {"video": video_ids, "other": other_ids}


async def _wait_any_video(cookies: dict[str, str], post_ids: list[str], timeout: int) -> tuple[str | None, dict[str, Any]]:
    """Poll a list of candidate post ids until any of them has a video URL."""
    deadline_ticks = max(timeout // 5, 1)
    last_by_post: dict[str, dict[str, Any]] = {}
    for _ in range(deadline_ticks):
        for pid in post_ids:
            latest = await get_media_post(cookies, pid) or {}
            if latest:
                last_by_post[pid] = latest
            urls = collect_media_urls(latest) if latest else {"video_urls": [], "image_urls": [], "audio_urls": []}
            if urls["video_urls"]:
                return pid, {"status": "ready", "post": latest, **urls}
        await asyncio.sleep(5)
    # timeout — return the freshest snapshot we saw
    if last_by_post:
        pid, latest = next(iter(last_by_post.items()))
        urls = collect_media_urls(latest)
        return pid, {"status": "timeout", "post": latest, **urls}
    return None, {"status": "timeout"}


async def _do_agent_video(bot: Bot, chat_id: int, sid: str, ep: int, sn: int) -> None:
    """Send scene prompt to Grok Agent, wait for the resulting voiced video, deliver."""
    status_msg: Message | None = None
    try:
        state = load_series(sid)
        scene = find_scene(state, ep, sn)
        prompt = (scene.get("prompt") or "").strip()
        if not prompt:
            await bot.send_message(chat_id, f"E{ep}·S{sn}: пустой prompt")
            return
        cookies = load_cookie_file(account_for(chat_id))
        aspect = scene.get("aspect_ratio") or "2:3"
        duration = int(scene.get("duration") or (state.get("settings") or {}).get("duration") or 6)
        agent_prompt = (
            f"Turn this scene into a short {duration}s cinematic video with built-in voice-over narration. "
            f"Aspect ratio {aspect}. Keep the same subject and style across cuts.\n\nScene:\n{prompt}"
        )
        status_msg = await bot.send_message(chat_id, f"🎬 E{ep}·S{sn}: Agent запущен, ждём стрим…")
        cid, events = await start_agent_conversation(cookies, None, agent_prompt)
        pids = _extract_agent_post_ids(events)
        candidates = pids["video"] + pids["other"]
        if not candidates:
            # last-resort: give agent a nudge and re-read
            try:
                await send_agent_prompt(cookies, cid or "", "Please finalize the video and return the media post.")
            except Exception:
                pass
        candidates = list(dict.fromkeys(candidates))  # dedupe, preserve order
        if not candidates:
            await status_msg.edit_text(f"E{ep}·S{sn}: Agent не вернул postId. Может, модерация. Смотри series/{sid}.json")
            return

        await status_msg.edit_text(f"⏳ E{ep}·S{sn}: жду видео по {len(candidates)} постам…")
        winner_pid, result = await _wait_any_video(cookies, candidates, timeout=420)
        if result.get("status") != "ready":
            scene["status"] = "video_error"
            scene["agent_conversation_id"] = cid
            scene["agent_candidates"] = candidates
            state["updated_at"] = _now_iso()
            save_series(state)
            await status_msg.edit_text(f"E{ep}·S{sn}: Agent видео не готово (status={result.get('status')})")
            return

        video_urls = result.get("video_urls") or []
        if not video_urls:
            scene["status"] = "video_error"
            save_series(state)
            await status_msg.edit_text(f"E{ep}·S{sn}: нет video_url")
            return

        url = video_urls[0]
        await status_msg.edit_text(f"⬇️ E{ep}·S{sn}: качаю…")
        OUTPUT_DIR.mkdir(exist_ok=True)
        suffix = guess_extension(url, ".mp4")
        dest = OUTPUT_DIR / f"{winner_pid}_scene_{ep}_{sn}{suffix}"
        await download_asset(cookies, url, dest)
        await status_msg.edit_text(f"📤 E{ep}·S{sn}: отправляю в Telegram…")
        caption = f"E{ep} · S{sn} — {state.get('title') or state.get('topic')}"
        await bot.send_video(chat_id, FSInputFile(dest), caption=caption[:1024])
        try:
            await status_msg.delete()
        except Exception:
            pass

        scene["status"] = "video_ready"
        scene["agent_conversation_id"] = cid
        scene["video_post_id"] = winner_pid
        scene["video_url"] = url
        scene["video_file"] = str(dest)
        state["updated_at"] = _now_iso()
        save_series(state)
    except Exception as exc:
        log.exception("agent video failed")
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
            if status_msg is not None:
                await status_msg.edit_text(f"video failed E{ep}·S{sn}: <code>{str(exc)[:400]}</code>", parse_mode=ParseMode.HTML)
            else:
                await bot.send_message(chat_id, f"video failed E{ep}·S{sn}: <code>{str(exc)[:400]}</code>", parse_mode=ParseMode.HTML)
        except Exception:
            pass


async def _render_all_approved(bot: Bot, chat_id: int, sid: str) -> None:
    try:
        state = load_series(sid)
    except FileNotFoundError:
        await bot.send_message(chat_id, f"No such series: <code>{sid}</code>", parse_mode=ParseMode.HTML)
        return
    todo: list[tuple[int, int]] = []
    for episode in state.get("episodes", []):
        ep = int(episode.get("episode_number", 0))
        for scene in episode.get("scenes", []):
            if scene.get("status") == "approved":
                todo.append((ep, int(scene.get("scene_number", 0))))
    if not todo:
        await bot.send_message(chat_id, "Нет approved-сцен для рендера.")
        return
    await bot.send_message(chat_id, f"🎬 Рендер {len(todo)} сцен последовательно (Agent+voice)…")
    for ep, sn in todo:
        key = scene_key(sid, ep, sn)
        IN_FLIGHT[key] = asyncio.create_task(_do_agent_video(bot, chat_id, sid, ep, sn))
        try:
            await IN_FLIGHT[key]
        except Exception:
            pass
    await bot.send_message(chat_id, "✅ Рендер завершён.")


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
