# -*- coding: utf-8 -*-
"""Антирейд: следит за входами во ВСЕХ чатах, где есть бот (не зависит от +надзор).
Если за несколько минут вошло слишком много людей, бот пишет главным админам в ЛС и в чат стаффа
(со скрытым созывом всех админов) и даёт кнопки: закрыть чат, сменить ссылку, забанить вошедших.
Настройки: /antiraid (главные админы, в ЛС с ботом или в чате стаффа)."""
import asyncio
import html
import json
import logging
import math
import os
import time
from collections import deque
from zoneinfo import ZoneInfo

from aiogram import BaseMiddleware, F, Router
from aiogram.filters import Command
from aiogram.types import (CallbackQuery, ChatMemberUpdated, ChatJoinRequest, ChatPermissions, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)

log = logging.getLogger("antiraid")
router = Router()

_bot = _get_db = _is_main_admin = _pings = None
_staff_chat = 0
_owner = 0

DEFAULTS = {"enabled": "1", "joins": "8", "minutes": "3", "scale": "4", "cooldown": "10", "automute": "0"}

_DDL = """
CREATE TABLE IF NOT EXISTS ar_cfg(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS ar_chats(chat_id BIGINT PRIMARY KEY, title TEXT, last_join TIMESTAMPTZ, joins_total INT DEFAULT 0, alerts INT DEFAULT 0);
CREATE TABLE IF NOT EXISTS ar_alerts(id SERIAL PRIMARY KEY, chat_id BIGINT, title TEXT, ts TIMESTAMPTZ DEFAULT now(),
                                     joins INT, users TEXT, perms TEXT, closed BOOLEAN DEFAULT FALSE);
"""

_events = {}     # chat_id -> deque[(ts, uid)]
_users = {}      # chat_id -> {uid: (name, username, is_bot, kind)}
_seen = {}       # (chat_id, uid) -> ts   (защита от двойного учёта: сервисное сообщение + chat_member)
_last_alert = {}  # chat_id -> ts
_raid_until = {}  # chat_id -> ts, пока идёт рейд
_members = {}    # chat_id -> (count, ts)


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


async def cfg(key) -> str:
    r = await q("SELECT value FROM ar_cfg WHERE key=%s", (key,), one=True)
    return r[0] if r else DEFAULTS[key]


async def set_cfg(key, value):
    await q("INSERT INTO ar_cfg(key,value) VALUES(%s,%s) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value", (key, str(value)))


# ---------------------------------------------------------------- вспомогательное
def _is_group(chat) -> bool:
    return getattr(chat, "type", "") in ("group", "supergroup")


def _umention(uid, name) -> str:
    return f'<a href="tg://user?id={uid}">{html.escape(name or str(uid))[:40]}</a>'


async def _recipients() -> list:
    ids = set()
    if _owner:
        ids.add(_owner)
    try:
        for r in (await q("SELECT admin_id FROM main_admins", many=True) or []):
            ids.add(int(r[0]))
    except Exception as e:
        log.warning("main_admins: %s", e)
    return list(ids)


async def _is_staff(uid) -> bool:
    return bool(uid == _owner or await _is_main_admin(uid))


async def _member_count(cid) -> int:
    c = _members.get(cid)
    if c and time.time() - c[1] < 600:
        return c[0]
    try:
        n = await _bot.get_chat_member_count(cid)
    except Exception:
        n = c[0] if c else 0
    _members[cid] = (n, time.time())
    return n


async def _threshold(cid) -> int:
    base = int(await cfg("joins"))
    scale = int(await cfg("scale"))
    if scale > 0:
        base = max(base, math.ceil(await _member_count(cid) * scale / 100))
    return base


# ---------------------------------------------------------------- учёт входов
async def record_join(chat, user, kind="join", adder=None):
    if not _is_group(chat) or not user or user.id == _bot.id:
        return
    if (await cfg("enabled")) != "1":
        return
    if adder is not None and adder.id != user.id and await _is_staff(adder.id):
        return                                    # людей добавил сам админ — это не рейд
    cid, now = chat.id, time.time()
    key = (cid, user.id)
    if now - _seen.get(key, 0) < 120:
        return
    _seen[key] = now
    if len(_seen) > 5000:
        for k in [k for k, t in _seen.items() if now - t > 300]:
            _seen.pop(k, None)
    window = int(await cfg("minutes")) * 60
    dq = _events.setdefault(cid, deque())
    dq.append((now, user.id))
    while dq and now - dq[0][0] > max(window, 900):
        dq.popleft()
    info = _users.setdefault(cid, {})
    info[user.id] = (user.full_name, user.username, bool(user.is_bot), kind)
    if len(info) > 200:
        for k in list(info)[:50]:
            info.pop(k, None)
    try:
        await q("""INSERT INTO ar_chats(chat_id,title,last_join,joins_total) VALUES(%s,%s,now(),1)
                   ON CONFLICT(chat_id) DO UPDATE SET title=EXCLUDED.title, last_join=now(), joins_total=ar_chats.joins_total+1""",
                (cid, chat.title or str(cid)))
    except Exception as e:
        log.warning("ar_chats: %s", e)
    in_win = [u for t, u in dq if now - t <= window]
    if len(set(in_win)) < await _threshold(cid):
        if now < _raid_until.get(cid, 0) and (await cfg("automute")) == "1":
            await _mute(cid, user.id)
        return
    if now - _last_alert.get(cid, 0) < int(await cfg("cooldown")) * 60:
        _raid_until[cid] = now + 600
        if (await cfg("automute")) == "1":
            await _mute(cid, user.id)
        return
    _last_alert[cid] = now
    _raid_until[cid] = now + 600
    uids = list(dict.fromkeys(in_win))
    asyncio.create_task(fire_alert(cid, chat.title or str(cid), uids, window // 60))


async def _mute(cid, uid):
    try:
        await _bot.restrict_chat_member(cid, uid, permissions=ChatPermissions(can_send_messages=False))
    except Exception as e:
        log.info("автомут %s в %s: %s", uid, cid, e)


# ---------------------------------------------------------------- тревога
def _alert_kb(aid, locked=False):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔓 Открыть чат" if locked else "🔒 Закрыть чат", callback_data=f"ar:{'unlock' if locked else 'lock'}:{aid}"),
         InlineKeyboardButton(text="🔗 Сменить ссылку", callback_data=f"ar:link:{aid}")],
        [InlineKeyboardButton(text="🚫 Забанить вошедших", callback_data=f"ar:ban:{aid}"),
         InlineKeyboardButton(text="✅ Отбой", callback_data=f"ar:close:{aid}")]])


async def _people_lines(cid, uids, limit=15):
    info = _users.get(cid, {})
    lines, bots, nouser = [], 0, 0
    for u in uids:
        name, uname, is_bot, kind = info.get(u, (str(u), None, False, "join"))
        bots += 1 if is_bot else 0
        nouser += 0 if uname else 1
    for u in uids[-limit:]:
        name, uname, is_bot, kind = info.get(u, (str(u), None, False, "join"))
        lines.append(f"• {_umention(u, name)}" + (f" @{uname}" if uname else "") + (" 🤖" if is_bot else "") + (" (заявка)" if kind == "req" else ""))
    return lines, bots, nouser


async def fire_alert(cid, title, uids, minutes, test=False):
    try:
        lines, bots, nouser = await _people_lines(cid, uids)
        thr = await _threshold(cid) if not test else int(await cfg("joins"))
        aid = 0
        if not test:
            r = await q("INSERT INTO ar_alerts(chat_id,title,joins,users) VALUES(%s,%s,%s,%s) RETURNING id", (cid, title, len(uids), json.dumps(uids)), one=True)
            aid = r[0]
            await q("UPDATE ar_chats SET alerts=alerts+1 WHERE chat_id=%s", (cid,))
        head = "🧪 <b>ТЕСТ антирейда</b> (настоящей тревоги нет)" if test else "🚨🚨 <b>РЕЙД В ЧАТЕ</b> 🚨🚨"
        txt = (f"{head}\n<b>{html.escape(title)}</b> (<code>{cid}</code>)\n"
               f"Вошло <b>{len(uids)}</b> за {minutes} мин. (порог {thr}). Ботов: {bots}, без @username: {nouser}.\n\n"
               + ("Последние вошедшие:\n" + "\n".join(lines) + "\n\n" if lines else "")
               + ("<b>Что сделать срочно:</b>\n1. Закрыть чат от сообщений\n2. Сменить пригласительные ссылки\n"
                  "3. Включить в чате одобрение заявок\n4. Забанить вошедших\n\nКнопки ниже." if not test else "Если вы видите это сообщение, оповещения работают."))
        kb = _alert_kb(aid) if not test else None
        ping = "" if test else (await _pings() if _pings else "")
        try:
            await _bot.send_message(_staff_chat, txt + ping, parse_mode="HTML", reply_markup=kb)
        except Exception as e:
            log.warning("антирейд: чат стаффа: %s", e)
        for uid in await _recipients():
            try:
                await _bot.send_message(uid, txt, parse_mode="HTML", reply_markup=kb)
            except Exception as e:
                log.info("антирейд: ЛС %s не доставлено: %s", uid, e)
        if not test and (await cfg("automute")) == "1":
            for u in uids:
                await _mute(cid, u)
    except Exception:
        log.exception("fire_alert")


# ---------------------------------------------------------------- кнопки тревоги
async def _alert(aid):
    return await q("SELECT chat_id, title, users, perms, closed FROM ar_alerts WHERE id=%s", (aid,), one=True)


async def _note(cb: CallbackQuery, line: str, kb=None):
    try:
        await cb.message.edit_text(cb.message.html_text + f"\n\n{line}", parse_mode="HTML", reply_markup=kb or cb.message.reply_markup)
    except Exception as e:
        if "not modified" not in str(e).lower():
            log.info("note: %s", e)


@router.callback_query(F.data.regexp(r"^ar:(lock|unlock|link|ban|banyes|back|close):\d+$"))
async def cb_alert(cb: CallbackQuery):
    if not await _is_staff(cb.from_user.id):
        return await cb.answer("Только главные админы", show_alert=True)
    _, act, aid = cb.data.split(":")
    aid = int(aid)
    row = await _alert(aid)
    if not row:
        return await cb.answer("Тревога не найдена", show_alert=True)
    cid, title, users, perms, closed = row
    who = html.escape(cb.from_user.full_name)
    if act == "lock":
        try:
            chat = await _bot.get_chat(cid)
            saved = chat.permissions.model_dump(exclude_none=True) if chat.permissions else {}
            await q("UPDATE ar_alerts SET perms=%s WHERE id=%s", (json.dumps(saved), aid))
            await _bot.set_chat_permissions(cid, ChatPermissions(can_send_messages=False))
            await _note(cb, f"🔒 Чат закрыт ({who}).", _alert_kb(aid, locked=True))
        except Exception as e:
            await _note(cb, f"❌ Не удалось закрыть чат: {html.escape(str(e))[:150]}")
    elif act == "unlock":
        try:
            p = json.loads(perms) if perms else {"can_send_messages": True, "can_send_audios": True, "can_send_documents": True,
                                                  "can_send_photos": True, "can_send_videos": True, "can_send_polls": True,
                                                  "can_send_other_messages": True, "can_add_web_page_previews": True}
            await _bot.set_chat_permissions(cid, ChatPermissions(**p))
            await _note(cb, f"🔓 Чат открыт, права возвращены ({who}).", _alert_kb(aid, locked=False))
        except Exception as e:
            await _note(cb, f"❌ Не удалось открыть чат: {html.escape(str(e))[:150]}")
    elif act == "link":
        try:
            link = await _bot.export_chat_invite_link(cid)
            await _note(cb, f"🔗 Основная ссылка заменена ({who}): {html.escape(link)}\n"
                            "Ссылки, созданные админами вручную, бот отозвать не может: отзовите их в «Пригласительные ссылки».")
        except Exception as e:
            await _note(cb, f"❌ Не удалось сменить ссылку (нужно право «Приглашать пользователей»): {html.escape(str(e))[:150]}")
    elif act == "ban":
        n = len(json.loads(users or "[]"))
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"⚠️ Да, забанить {n}", callback_data=f"ar:banyes:{aid}"),
                                                    InlineKeyboardButton(text="↩️ Нет", callback_data=f"ar:back:{aid}")]])
        try:
            await cb.message.edit_reply_markup(reply_markup=kb)
        except Exception:
            pass
    elif act == "back":
        try:
            await cb.message.edit_reply_markup(reply_markup=_alert_kb(aid))
        except Exception:
            pass
    elif act == "banyes":
        ok = fail = 0
        for u in json.loads(users or "[]"):
            if await _is_staff(u):
                continue
            try:
                await _bot.ban_chat_member(cid, u, revoke_messages=True)
                ok += 1
            except Exception:
                fail += 1
            await asyncio.sleep(0.2)
        await _note(cb, f"🚫 Забанено: {ok}" + (f", не удалось: {fail}" if fail else "") + f" ({who}).", _alert_kb(aid))
    elif act == "close":
        await q("UPDATE ar_alerts SET closed=TRUE WHERE id=%s", (aid,))
        _raid_until[cid] = 0
        _last_alert[cid] = 0
        try:
            await cb.message.edit_text(cb.message.html_text + f"\n\n✅ Отбой ({who}).", parse_mode="HTML")
        except Exception:
            pass
    await cb.answer()


# ---------------------------------------------------------------- источники событий
class _JoinMiddleware(BaseMiddleware):
    """Только наблюдает за сервисными сообщениями о входе, дальше событие идёт как обычно."""
    async def __call__(self, handler, event, data):
        try:
            if getattr(event, "new_chat_members", None):
                for u in event.new_chat_members:
                    await record_join(event.chat, u, "join", event.from_user)
        except Exception:
            log.exception("antiraid middleware")
        return await handler(event, data)


@router.chat_member()
async def on_chat_member(upd: ChatMemberUpdated):
    try:
        if upd.old_chat_member.status in ("left", "kicked") and upd.new_chat_member.status in ("member", "restricted"):
            await record_join(upd.chat, upd.new_chat_member.user, "join", upd.from_user)
    except Exception:
        log.exception("antiraid chat_member")


@router.chat_join_request()
async def on_join_request(req: ChatJoinRequest):
    try:
        await record_join(req.chat, req.from_user, "req", None)
    except Exception:
        log.exception("antiraid join_request")


# ---------------------------------------------------------------- настройки /antiraid
def _on(v):
    return "🟢" if str(v) == "1" else "🔴"


async def _panel():
    en, joins, mins, scale, auto = [await cfg(k) for k in ("enabled", "joins", "minutes", "scale", "automute")]
    chats = await q("SELECT title, last_join, joins_total, alerts FROM ar_chats ORDER BY last_join DESC NULLS LAST LIMIT 8", many=True) or []
    alerts = await q("SELECT title, ts, joins, closed FROM ar_alerts ORDER BY id DESC LIMIT 5", many=True) or []
    lines = ["🛡 <b>Антирейд</b>", f"Состояние: {'🟢 включён' if en == '1' else '🔴 выключен'} (работает во всех чатах бота, +надзор не нужен)",
             f"Порог: <b>{joins}</b> входов за <b>{mins}</b> мин." + (f" (в больших чатах не меньше {scale}% от числа участников)" if int(scale) else ""),
             f"Автомут вошедших во время рейда: {'🟢 да' if auto == '1' else '🔴 нет'}",
             "Оповещения: ЛС главным админам + чат стаффа со скрытым созывом."]
    if chats:
        lines.append("\n<b>Чаты под наблюдением</b> (где были входы):")
        for t, lj, tot, al in chats:
            lines.append(f"• {html.escape(t or '?')} — входов {tot}, тревог {al}")
    if alerts:
        lines.append("\n<b>Последние тревоги:</b>")
        for t, ts, j, cl in alerts:
            lines.append(f"• {ts.astimezone(ZoneInfo('Europe/Moscow')).strftime('%d.%m %H:%M')} {html.escape(t or '?')} — {j} входов {'✅' if cl else '⏳'}")
    def row(key, vals, cur, fmt):
        return [InlineKeyboardButton(text=("• " if str(cur) == str(v) else "") + fmt.format(v), callback_data=f"arc:{key}:{v}") for v in vals]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔴 Выключить" if en == "1" else "🟢 Включить", callback_data=f"arc:enabled:{0 if en == '1' else 1}")],
        row("joins", (5, 8, 12, 20, 30), joins, "{}"),
        row("minutes", (1, 2, 3, 5, 10), mins, "{} мин"),
        row("scale", (0, 3, 4, 6, 10), scale, "{}%"),
        [InlineKeyboardButton(text=f"{_on(auto)} Автомут при рейде", callback_data=f"arc:automute:{0 if auto == '1' else 1}"),
         InlineKeyboardButton(text="🧪 Проверка", callback_data="arc:test:0")]])
    return "\n".join(lines) + "\n\n<i>Ряды кнопок: порог входов · окно в минутах · доля от размера чата (0 = выкл).</i>", kb


@router.message(Command("antiraid"))
async def cmd_antiraid(message: Message):
    if not await _is_staff(message.from_user.id):
        return
    if message.chat.type != "private" and message.chat.id != _staff_chat:
        return
    text, kb = await _panel()
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.regexp(r"^arc:(enabled|joins|minutes|scale|automute|test):\d+$"))
async def cb_cfg(cb: CallbackQuery):
    if not await _is_staff(cb.from_user.id):
        return await cb.answer("Только главные админы", show_alert=True)
    _, key, val = cb.data.split(":")
    if key == "test":
        asyncio.create_task(fire_alert(cb.message.chat.id, "Тестовый чат", [900000001, 900000002, 900000003], int(await cfg("minutes")), test=True))
        return await cb.answer("Тестовое оповещение отправлено в ЛС и в чат стаффа", show_alert=True)
    await set_cfg(key, val)
    text, kb = await _panel()
    try:
        await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        pass
    await cb.answer()


# ---------------------------------------------------------------- подключение
def setup(dp, bot, get_db, is_main_admin, staff_chat_id, owner_id, pings_fn):
    global _bot, _get_db, _is_main_admin, _staff_chat, _owner, _pings
    _bot, _get_db, _is_main_admin, _staff_chat, _owner, _pings = bot, get_db, is_main_admin, staff_chat_id, owner_id, pings_fn
    # наблюдатель за входами ставим первым, чтобы другие middleware не могли его пропустить
    try:
        dp.message.outer_middleware.middlewares.insert(0, _JoinMiddleware())
    except Exception:
        dp.message.outer_middleware(_JoinMiddleware())
    dp.include_router(router)
    try:
        dp.sub_routers.remove(router)
        dp.sub_routers.insert(0, router)
    except Exception:
        pass
