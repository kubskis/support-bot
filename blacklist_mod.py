# -*- coding: utf-8 -*-
"""Чёрный список игроков.
Публикатор в меню секреток нажимает «🚫 Внести в ЧС» → присылает ник Roblox (или ссылку на профиль) → пишет причину → подтверждает.
Бот ведёт ОДИНАКОВЫЙ список в двух темах чата стаффа («Поддержка» и «Информация»): одно и то же сообщение, которое он сам обновляет.
В каждой записи: ссылка на профиль Roblox, ник (нажатие копирует), причина, кто внёс и когда.
Убрать запись: /blremove ник (главный админ или тот, кто внёс)."""
import asyncio
import html
import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import aiohttp
from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

try:
    from aiogram.types import LinkPreviewOptions
except Exception:  # старый aiogram
    LinkPreviewOptions = None

log = logging.getLogger("blacklist")
router = Router()
MSK = ZoneInfo("Europe/Moscow")

_bot = _get_db = _is_publisher = _is_main_admin = _lookup = None
_chat = 0
_topics = []

PAGE_ENTRIES = 8
PAGE_CHARS = 3300

_DDL = """
CREATE TABLE IF NOT EXISTS bl_entries(id SERIAL PRIMARY KEY, roblox_id BIGINT, nick TEXT, display TEXT, reason TEXT,
                                      added_by BIGINT, added_name TEXT, added_at TIMESTAMPTZ DEFAULT now(), active BOOLEAN DEFAULT TRUE,
                                      removed_by BIGINT, removed_at TIMESTAMPTZ);
CREATE TABLE IF NOT EXISTS bl_msgs(topic_id INT, idx INT, message_id BIGINT, PRIMARY KEY(topic_id, idx));
"""

_lock = asyncio.Lock()


class BlStates(StatesGroup):
    nick = State()
    reason = State()
    confirm = State()


# ---------------------------------------------------------------- БД
def _q(sql, args=(), one=False, many=False):
    with _get_db() as conn:
        with conn.cursor() as c:
            c.execute(sql, args)
            if one:
                return c.fetchone()
            if many:
                return c.fetchall()
            return None


async def q(sql, args=(), one=False, many=False):
    return await asyncio.to_thread(_q, sql, args, one, many)


async def init_tables():
    await q(_DDL)


# ---------------------------------------------------------------- оформление списка
def _profile(rid) -> str:
    return f"https://www.roblox.com/users/{rid}/profile"


def _entry_html(i, e) -> str:
    _, rid, nick, display, reason, by, by_name, at = e
    when = at.astimezone(MSK).strftime("%d.%m.%Y")
    shown = f" ({html.escape(display)})" if display and display != nick else ""
    return (f"<blockquote><b>{i}.</b> 🔗 <a href=\"{_profile(rid)}\">Профиль Roblox</a> · 👤 <code>{html.escape(nick)}</code>{shown}\n"
            f"📝 <b>Причина:</b> {html.escape(reason or '—')}\n"
            f"✍️ <b>Внёс:</b> <a href=\"tg://user?id={by}\">{html.escape(by_name or str(by))[:40]}</a> · {when}</blockquote>")


async def _pages() -> list:
    rows = await q("SELECT id, roblox_id, nick, display, reason, added_by, added_name, added_at FROM bl_entries WHERE active ORDER BY id", many=True) or []
    now = datetime.now(MSK).strftime("%d.%m %H:%M")
    head = (f"🚫 <b>Чёрный список игроков</b> · записей: <b>{len(rows)}</b>\n"
            "<i>Нажмите на ник, чтобы скопировать, или откройте профиль по ссылке. Список ведёт бот, вручную его не правьте.</i>")
    if not rows:
        return [head + "\n\n<i>Пока пусто.</i>\n\n" + f"<i>Обновлено {now} МСК</i>"]
    pages, cur, size = [], [], 0
    for n, e in enumerate(rows, 1):
        block = _entry_html(n, e)
        if cur and (len(cur) >= PAGE_ENTRIES or size + len(block) > PAGE_CHARS):
            pages.append(cur)
            cur, size = [], 0
        cur.append(block)
        size += len(block)
    if cur:
        pages.append(cur)
    out = []
    for i, blocks in enumerate(pages, 1):
        title = head if i == 1 else f"🚫 <b>Чёрный список</b> · продолжение ({i}/{len(pages)})"
        foot = f"\n\n<i>Обновлено {now} МСК" + (f" · страница {i}/{len(pages)}" if len(pages) > 1 else "") + "</i>"
        out.append(title + "\n\n" + "\n".join(blocks) + foot)
    return out


def _lp():
    return {"link_preview_options": LinkPreviewOptions(is_disabled=True)} if LinkPreviewOptions else {"disable_web_page_preview": True}


async def rebuild():
    """Приводит сообщения в обеих темах к актуальному списку: правит существующие, добавляет и убирает страницы."""
    async with _lock:
        try:
            pages = await _pages()
        except Exception:
            log.exception("bl pages")
            return
        for topic in _topics:
            have = {r[0]: r[1] for r in (await q("SELECT idx, message_id FROM bl_msgs WHERE topic_id=%s", (topic,), many=True) or [])}
            for idx, text in enumerate(pages):
                mid = have.get(idx)
                done = False
                if mid:
                    try:
                        await _bot.edit_message_text(text, chat_id=_chat, message_id=mid, parse_mode="HTML", **_lp())
                        done = True
                    except Exception as e:
                        if "not modified" in str(e).lower():
                            done = True
                        else:
                            log.info("bl edit %s/%s: %s", topic, idx, e)
                if not done:
                    try:
                        m = await _bot.send_message(_chat, text, parse_mode="HTML", message_thread_id=topic, **_lp())
                        await q("INSERT INTO bl_msgs(topic_id,idx,message_id) VALUES(%s,%s,%s) ON CONFLICT(topic_id,idx) DO UPDATE SET message_id=EXCLUDED.message_id", (topic, idx, m.message_id))
                        if idx == 0:
                            try:
                                await _bot.pin_chat_message(_chat, m.message_id, disable_notification=True)
                            except Exception:
                                pass
                    except Exception as e:
                        log.warning("bl send %s/%s: %s", topic, idx, e)
            for idx, mid in have.items():
                if idx >= len(pages):
                    try:
                        await _bot.delete_message(_chat, mid)
                    except Exception:
                        pass
                    await q("DELETE FROM bl_msgs WHERE topic_id=%s AND idx=%s", (topic, idx))
            await asyncio.sleep(0.3)


# ---------------------------------------------------------------- вспомогательное
_LINK_RE = re.compile(r"roblox\.com/(?:[a-zA-Z\-]{2,5}/)?users/(\d+)", re.I)


async def _by_id(rid):
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=6)) as s:
            async with s.get(f"https://users.roblox.com/v1/users/{rid}") as r:
                if r.status == 200:
                    j = await r.json()
                    return {"id": j["id"], "name": j["name"], "displayName": j.get("displayName") or j["name"]}
    except Exception as e:
        log.info("roblox by id: %s", e)
    return None


async def resolve(raw: str):
    """(info|None, ошибка). info = {id, name, displayName}"""
    raw = (raw or "").strip()
    m = _LINK_RE.search(raw)
    if m:
        info = await _by_id(int(m.group(1)))
        return (info, None) if info else (None, "Не удалось получить профиль по ссылке. Проверьте ссылку или пришлите ник.")
    status, nick, info = await _lookup(raw.lstrip("@"))
    if status == "ok":
        return info, None
    if status == "error":
        return None, "Roblox сейчас не отвечает. Попробуйте через минуту."
    return None, "Игрок с таким ником не найден. Пришлите ник латиницей (3–20 символов) или ссылку на профиль."


def _cancel_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✖️ Отмена", callback_data="bl_cancel")]])


async def _allowed(uid) -> bool:
    return bool(await _is_publisher(uid) or await _is_main_admin(uid))


# ---------------------------------------------------------------- внесение
@router.callback_query(F.data == "bl_add")
async def bl_add(cb: CallbackQuery, state: FSMContext):
    if not await _allowed(cb.from_user.id):
        return await cb.answer("Доступно только публикаторам", show_alert=True)
    await state.clear()
    await state.set_state(BlStates.nick)
    await cb.message.answer("🚫 <b>Внести игрока в чёрный список</b>\n\nПришлите <b>ник Roblox</b> игрока или ссылку на его профиль (roblox.com/users/…).",
                            parse_mode="HTML", reply_markup=_cancel_kb())
    await cb.answer()


@router.callback_query(F.data == "bl_cancel")
async def bl_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await cb.message.edit_text("Внесение в чёрный список отменено.")
    except Exception:
        pass
    await cb.answer()


@router.message(BlStates.nick, F.chat.type == "private")
async def bl_nick(message: Message, state: FSMContext):
    if not await _allowed(message.from_user.id):
        return
    txt = (message.text or "").strip()
    if txt.startswith("/"):
        await state.clear()
        return await message.answer("Внесение в чёрный список отменено.")
    info, err = await resolve(txt)
    if not info:
        return await message.answer(f"❌ {html.escape(err)}", parse_mode="HTML", reply_markup=_cancel_kb())
    dup = await q("SELECT reason, added_name, added_at FROM bl_entries WHERE active AND roblox_id=%s", (info["id"],), one=True)
    if dup:
        await state.clear()
        return await message.answer(f"ℹ️ <code>{html.escape(info['name'])}</code> уже в чёрном списке.\n📝 Причина: {html.escape(dup[0] or '—')}\n"
                                    f"✍️ Внёс: {html.escape(dup[1] or '?')} · {dup[2].astimezone(MSK).strftime('%d.%m.%Y')}", parse_mode="HTML")
    await state.update_data(rid=info["id"], nick=info["name"], display=info["displayName"])
    await state.set_state(BlStates.reason)
    await message.answer(f"Нашёл: <code>{html.escape(info['name'])}</code>"
                         + (f" ({html.escape(info['displayName'])})" if info["displayName"] != info["name"] else "")
                         + f"\n🔗 <a href=\"{_profile(info['id'])}\">Профиль Roblox</a>\n\n📝 Напишите <b>причину</b> внесения в ЧС (до 300 символов).",
                         parse_mode="HTML", reply_markup=_cancel_kb(), **_lp())


@router.message(BlStates.reason, F.chat.type == "private")
async def bl_reason(message: Message, state: FSMContext):
    if not await _allowed(message.from_user.id):
        return
    txt = (message.text or "").strip()
    if txt.startswith("/"):
        await state.clear()
        return await message.answer("Внесение в чёрный список отменено.")
    if len(txt) < 3:
        return await message.answer("Причина слишком короткая, опишите подробнее.", reply_markup=_cancel_kb())
    d = await state.get_data()
    await state.update_data(reason=txt[:300])
    await state.set_state(BlStates.confirm)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Внести в ЧС", callback_data="bl_ok"),
                                                InlineKeyboardButton(text="✖️ Отмена", callback_data="bl_cancel")]])
    await message.answer(f"Проверьте запись:\n\n👤 <code>{html.escape(d['nick'])}</code> · <a href=\"{_profile(d['rid'])}\">профиль</a>\n"
                         f"📝 {html.escape(txt[:300])}\n✍️ Внесёт: {html.escape(message.from_user.full_name)}\n\nЗапись появится в темах «Поддержка» и «Информация».",
                         parse_mode="HTML", reply_markup=kb, **_lp())


@router.callback_query(BlStates.confirm, F.data == "bl_ok")
async def bl_ok(cb: CallbackQuery, state: FSMContext):
    if not await _allowed(cb.from_user.id):
        return await cb.answer("Нет доступа", show_alert=True)
    d = await state.get_data()
    await state.clear()
    if not d.get("rid"):
        return await cb.answer("Данные потеряны, начните заново", show_alert=True)
    if await q("SELECT 1 FROM bl_entries WHERE active AND roblox_id=%s", (d["rid"],), one=True):
        return await cb.answer("Уже в списке", show_alert=True)
    await q("INSERT INTO bl_entries(roblox_id,nick,display,reason,added_by,added_name) VALUES(%s,%s,%s,%s,%s,%s)",
            (d["rid"], d["nick"], d["display"], d["reason"], cb.from_user.id, cb.from_user.full_name))
    try:
        await cb.message.edit_text(f"✅ <code>{html.escape(d['nick'])}</code> внесён в чёрный список. Список в темах обновляется.", parse_mode="HTML")
    except Exception:
        pass
    await cb.answer()
    await rebuild()


# ---------------------------------------------------------------- удаление
@router.message(Command("blremove"))
async def bl_remove(message: Message, command: CommandObject):
    uid = message.from_user.id
    if not await _allowed(uid):
        return
    arg = (command.args or "").strip()
    if not arg:
        return await message.answer("Использование: <code>/blremove ник</code> (или ссылка на профиль). Убрать запись может главный админ или тот, кто её внёс.", parse_mode="HTML")
    m = _LINK_RE.search(arg)
    row = (await q("SELECT id, nick, added_by FROM bl_entries WHERE active AND roblox_id=%s", (int(m.group(1)),), one=True) if m
           else await q("SELECT id, nick, added_by FROM bl_entries WHERE active AND lower(nick)=lower(%s)", (arg.lstrip("@"),), one=True))
    if not row:
        return await message.answer("Такого игрока нет в чёрном списке.")
    if row[2] != uid and not await _is_main_admin(uid):
        return await message.answer("Убрать запись может главный админ или тот, кто её внёс.")
    await q("UPDATE bl_entries SET active=FALSE, removed_by=%s, removed_at=now() WHERE id=%s", (uid, row[0]))
    await message.answer(f"✅ <code>{html.escape(row[1])}</code> убран из чёрного списка.", parse_mode="HTML")
    await rebuild()


# ---------------------------------------------------------------- подключение
def setup(dp, bot, get_db, is_publisher, is_main_admin, lookup_roblox, chat_id, topics):
    global _bot, _get_db, _is_publisher, _is_main_admin, _lookup, _chat, _topics
    _bot, _get_db, _is_publisher, _is_main_admin, _lookup, _chat, _topics = bot, get_db, is_publisher, is_main_admin, lookup_roblox, chat_id, list(topics)
    dp.include_router(router)
    try:
        dp.sub_routers.remove(router)
        dp.sub_routers.insert(0, router)
    except Exception:
        pass
