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
_call = None   # созыв стаффа в тему: async (topic_id, text_html, list_message_id)
_chat = 0
_topics = []

PAGE_ENTRIES = 8
PAGE_CHARS = 3300

_DDL = """
CREATE TABLE IF NOT EXISTS bl_entries(id SERIAL PRIMARY KEY, roblox_id BIGINT, nick TEXT, display TEXT, reason TEXT,
                                      added_by BIGINT, added_name TEXT, added_at TIMESTAMPTZ DEFAULT now(), active BOOLEAN DEFAULT TRUE,
                                      removed_by BIGINT, removed_at TIMESTAMPTZ);
CREATE TABLE IF NOT EXISTS bl_msgs(topic_id INT, idx INT, message_id BIGINT, PRIMARY KEY(topic_id, idx));
CREATE TABLE IF NOT EXISTS bl_flags(key TEXT PRIMARY KEY);
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


MAX_CHARS = 3900   # лимит Telegram 4096, с запасом


_who_cache = {}


async def _who(uid, fallback) -> str:
    """@юзернейм того, кто внёс (бот берёт его по ID); если юзернейма нет — имя."""
    if uid in _who_cache:
        return _who_cache[uid]
    name = fallback or str(uid)
    try:
        ch = await _bot.get_chat(uid)
        if getattr(ch, "username", None):
            name = f"@{ch.username}"
        elif getattr(ch, "full_name", None):
            name = ch.full_name
    except Exception:
        pass
    _who_cache[uid] = name
    return name


def _compact(i, e, who) -> str:
    _, rid, nick, display, reason, by, by_name, at = e
    return f"<b>{i}.</b> <code>{html.escape(nick)}</code> - {html.escape((reason or '—').replace(chr(10), ' '))[:90]} - {html.escape(who)}"


async def _render() -> str:
    """Один текст на весь список. Если не влезает в одно сообщение, показываем самые новые записи и пишем, сколько скрыто."""
    rows = await q("SELECT id, roblox_id, nick, display, reason, added_by, added_name, added_at FROM bl_entries WHERE active ORDER BY id", many=True) or []
    now = datetime.now(MSK).strftime("%d.%m %H:%M")
    head = (f"🚫 <b>Чёрный список игроков</b> · записей: <b>{len(rows)}</b>\n"
            "<i>Ник копируется по нажатию. Список ведёт бот.</i>\n\n")
    foot_t = f"\n<i>Обновлено {now} МСК</i>"
    if not rows:
        return head + "<i>Пока пусто.</i>" + foot_t
    blocks = [_compact(n, e, await _who(e[5], e[6])) for n, e in enumerate(rows, 1)]
    keep, size = [], len(head) + len(foot_t) + 80
    for blk in reversed(blocks):
        if size + len(blk) + 2 > MAX_CHARS:
            break
        keep.append(blk)
        size += len(blk) + 2
    keep.reverse()
    hidden = len(blocks) - len(keep)
    note = (f"\n<i>Скрыто старых записей: {hidden}.</i>" if hidden else "")
    return head + "\n".join(keep) + note + foot_t


def _lp():
    return {"link_preview_options": LinkPreviewOptions(is_disabled=True)} if LinkPreviewOptions else {"disable_web_page_preview": True}


async def rebuild():
    """Одно сообщение на тему: правим его. Новое создаём только если старое удалено. Лишние сообщения удаляем."""
    async with _lock:
        try:
            text = await _render()
        except Exception:
            log.exception("bl render")
            return
        for topic in _topics:
            row = await q("SELECT message_id FROM bl_msgs WHERE topic_id=%s AND idx=0", (topic,), one=True)
            mid = row[0] if row else None
            if mid:
                try:
                    await _bot.edit_message_text(text, chat_id=_chat, message_id=mid, parse_mode="HTML", **_lp())
                    await _drop_extra(topic)
                    continue
                except Exception as e:
                    el = str(e).lower()
                    if "not modified" in el:
                        await _drop_extra(topic)
                        continue
                    if "not found" not in el and "can't be edited" not in el:
                        log.warning("bl edit %s: %s (повтор позже)", topic, e)
                        continue                      # временная ошибка: не плодим новое сообщение
                    log.info("bl: старое сообщение в теме %s не найдено, создаём новое", topic)
            try:
                m = await _bot.send_message(_chat, text, parse_mode="HTML", message_thread_id=topic, **_lp())
                await q("INSERT INTO bl_msgs(topic_id,idx,message_id) VALUES(%s,0,%s) ON CONFLICT(topic_id,idx) DO UPDATE SET message_id=EXCLUDED.message_id", (topic, m.message_id))
                try:
                    await _bot.pin_chat_message(_chat, m.message_id, disable_notification=True)
                except Exception as e:
                    log.info("bl: закрепить не удалось: %s", e)
            except Exception as e:
                log.warning("bl send %s: %s", topic, e)
            await _drop_extra(topic)
            await asyncio.sleep(0.3)


async def _drop_extra(topic):
    """Удаляет всё, кроме основного сообщения темы (старые страницы из прошлых версий)."""
    for idx, mid in await q("SELECT idx, message_id FROM bl_msgs WHERE topic_id=%s AND idx<>0", (topic,), many=True) or []:
        try:
            await _bot.delete_message(_chat, mid)
        except Exception:
            pass
        await q("DELETE FROM bl_msgs WHERE topic_id=%s AND idx=%s", (topic, idx))


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
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(style="danger", text="✖️ Отмена", callback_data="bl_cancel")]])


async def _allowed(uid) -> bool:
    return bool(await _is_publisher(uid) or await _is_main_admin(uid))


# ---------------------------------------------------------------- проверка ника (для обжалований)
async def find_entry(rid, nick):
    """Активная запись ЧС по ID Roblox, а если ID нет — по нику. None, если игрока нет в списке."""
    row = None
    if rid:
        row = await q("SELECT nick, reason, added_at FROM bl_entries WHERE active AND roblox_id=%s ORDER BY id DESC LIMIT 1", (int(rid),), one=True)
    if not row and nick:
        row = await q("SELECT nick, reason, added_at FROM bl_entries WHERE active AND lower(nick)=lower(%s) ORDER BY id DESC LIMIT 1", (nick,), one=True)
    if not row:
        return None
    return {"nick": row[0], "reason": row[1], "added": row[2].astimezone(MSK).strftime("%d.%m.%Y")}


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
                                                InlineKeyboardButton(style="danger", text="✖️ Отмена", callback_data="bl_cancel")]])
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
    if _call:
        text = (f"В чёрный список внесён игрок <code>{html.escape(d['nick'])}</code>\n"
                f"Причина: {html.escape(d['reason'] or '—')}\n"
                f"Внёс: {html.escape(cb.from_user.full_name)}")
        for topic in _topics:
            row = await q("SELECT message_id FROM bl_msgs WHERE topic_id=%s AND idx=0", (topic,), one=True)
            try:
                await _call(topic, text, row[0] if row else None)
            except Exception as e:
                log.warning("bl созыв %s: %s", topic, e)


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



# ---------------------------------------------------------------- разовый импорт стартового списка
SEED_KEY = "seed_admin_list_2026_11_03"
SEED = [
    ("Koshka_Sendy598", None), ("cococola_519", None), ("bogdan60g72", None), ("COLLART08", None),
    ("fheifehifhiefhi", None), ("skebobik675242", None), ("gdgdg8732", None), ("liplipmaglaplik_9330", None),
    ("ennoughh", None), ("Alisartywi", None), ("aafkot", None), ("CAXAPOK_88765", None), ("itsmilana8785", None),
    ("kara_melka70", None), ("art_kott", None), ("melissa_play111", None), ("STITCH3638746", None),
    ("lol12345lollolll", None), ("zhans54564", None), ("gikap3", "много раз нарушила правила"),
    ("kisa_perer019", None), ("Milann_x0", None), ("Alimzhan9995", None), ("Eva_ardi29", None),
    ("aman_ro15", None), ("Zhenya1378", None), ("iriishkaal", None), ("cotik_366", None), ("Vcim76", None),
    ("ivvyxu085", None), ("CTEPAH_91", None), ("Fedkow24", None), ("Aika_0776", None),
]
SEED_DEFAULT_REASON = "ускорили"


async def run_seed(owner_id: int, force: bool = False):
    """Один раз добавляет стартовый список от главного админа и присылает отчёт. Повторно не запускается (кроме force)."""
    try:
        if not force and await q("SELECT 1 FROM bl_flags WHERE key=%s", (SEED_KEY,), one=True):
            return
        if not owner_id:
            log.error("run_seed: ADMIN_ID не задан, импорт пропущен")
            return
        added, skipped, bad = [], [], []
        name = "Главный админ"
        for nick, reason in SEED:
            status, got, info = await _lookup(nick)
            if status != "ok":
                bad.append(nick if status == "notfound" else f"{nick} (Roblox не ответил)")
                await asyncio.sleep(0.5)
                continue
            if await q("SELECT 1 FROM bl_entries WHERE active AND roblox_id=%s", (info["id"],), one=True):
                skipped.append(info["name"])
                continue
            await q("INSERT INTO bl_entries(roblox_id,nick,display,reason,added_by,added_name) VALUES(%s,%s,%s,%s,%s,%s)",
                    (info["id"], info["name"], info["displayName"], reason or SEED_DEFAULT_REASON, owner_id, name))
            added.append(info["name"])
            await asyncio.sleep(0.5)
        await q("INSERT INTO bl_flags(key) VALUES(%s) ON CONFLICT DO NOTHING", (SEED_KEY,))
        await rebuild()
        txt = (f"🚫 <b>Чёрный список: стартовый импорт</b>\n"
               f"Добавлено: <b>{len(added)}</b>" + (f", уже были: {len(skipped)}" if skipped else "") +
               (f"\n⚠️ Не найдены в Roblox: {', '.join(html.escape(x) for x in bad)}" if bad else "") +
               f"\n\nУдалить запись: <code>/blremove ник</code>. Удалять может главный админ или тот, кто внёс.")
        for uid in {owner_id}:
            try:
                await _bot.send_message(uid, txt, parse_mode="HTML")
            except Exception as e:
                log.info("отчёт импорта владельцу не доставлен: %s", e)
        try:
            await _bot.send_message(_chat, txt, parse_mode="HTML", message_thread_id=_topics[-1] if _topics else None)
        except Exception as e:
            log.info("отчёт импорта в чат: %s", e)
    except Exception as e:
        log.exception("run_seed")
        try:
            await _bot.send_message(owner_id, f"❌ <b>Импорт чёрного списка не выполнен:</b> {html.escape(str(e))[:300]}\nЗапустить вручную: /blseed", parse_mode="HTML")
        except Exception:
            pass

@router.message(Command("blseed"))
async def bl_seed_cmd(message: Message):
    """Ручной запуск стартового импорта (только главные админы, в ЛС с ботом)."""
    if not await _is_main_admin(message.from_user.id):
        return
    await message.answer("Запускаю импорт стартового списка…")
    await run_seed(message.from_user.id, force=True)


# ---------------------------------------------------------------- подключение
def setup(dp, bot, get_db, is_publisher, is_main_admin, lookup_roblox, chat_id, topics, call_fn=None):
    global _bot, _get_db, _is_publisher, _is_main_admin, _lookup, _chat, _topics, _call
    _call = call_fn
    _bot, _get_db, _is_publisher, _is_main_admin, _lookup, _chat, _topics = bot, get_db, is_publisher, is_main_admin, lookup_roblox, chat_id, list(topics)
    dp.include_router(router)
    try:
        dp.sub_routers.remove(router)
        dp.sub_routers.insert(0, router)
    except Exception:
        pass
