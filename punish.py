# -*- coding: utf-8 -*-
"""Ручные наказания: бан, разбан, мут, размут.

Только главные админы. Работают в любом чате, где есть бот.
Команды: «бан [срок] [причина]», «разбан», «мут [срок] [причина]», «размут».
Цель: ответом на сообщение, text_mention, @ник или числовой ID.
Срок: 30с, 15мин, 2ч, 3д, 1нед, 2мес, 1г, «навсегда». По умолчанию: бан навсегда, мут 7 дней.
Наказания от антирейда работают через Bot API напрямую (см. antiraid.py), эти команды к ним не относятся.
"""
import html
import re
from datetime import datetime, timedelta, timezone

from aiogram import F, Router
from aiogram.types import ChatPermissions, Message

router = Router(name="punish")

_bot = None
_is_main_admin = None
_resolve_username = None

_BAN = ("бан", "ban")
_UNBAN = ("разбан", "вернуть", "unban")
_MUTE = ("мут", "mute")
_UNMUTE = ("-мут", "размут", "unmute")
_ALL = _BAN + _UNBAN + _MUTE + _UNMUTE
_RX = r"^/?(" + "|".join(re.escape(w) for w in sorted(_ALL, key=len, reverse=True)) + r")(@\w+)?(\s|$)"

_UNITS = {"с": 1, "сек": 1, "мин": 60, "ч": 3600, "час": 3600, "д": 86400, "дн": 86400, "день": 86400,
          "нед": 604800, "мес": 2592000, "г": 31536000, "год": 31536000}
_DUR_RE = re.compile(r"^(\d+)(сек|мин|час|день|дн|нед|мес|год|с|ч|д|г)$", re.IGNORECASE)
_FOREVER = ("навсегда", "forever", "perm")
_LIGHT_PERMS = ChatPermissions(
    can_send_messages=True, can_send_audios=True, can_send_documents=True, can_send_photos=True,
    can_send_videos=True, can_send_video_notes=True, can_send_voice_notes=True, can_send_polls=True,
    can_send_other_messages=True, can_add_web_page_previews=True)


def _parse_dur(tok: str):
    """Срок в секундах; 0 = навсегда; None, если это не срок."""
    t = tok.lower().strip()
    if t in _FOREVER:
        return 0
    m = _DUR_RE.match(t)
    if not m:
        return None
    return int(m.group(1)) * _UNITS.get(m.group(2).lower(), 0)


def _fmt(sec: int) -> str:
    if not sec:
        return "навсегда"
    for name, k in (("мес", 2592000), ("д", 86400), ("ч", 3600), ("мин", 60)):
        if sec >= k:
            return f"{sec // k} {name}"
    return f"{sec} с"


def _until(sec: int):
    return datetime.now(timezone.utc) + timedelta(seconds=sec) if sec else None


def _mention(uid: int, name: str) -> str:
    return f'<a href="tg://user?id={uid}">{html.escape(name or str(uid))}</a>'


async def _resolve(msg: Message, tokens: list):
    """(uid, имя, оставшиеся токены). Приоритет: ответ → text_mention → @ник → числовой ID."""
    rest = list(tokens)
    rep = msg.reply_to_message
    if rep and rep.from_user and not rep.from_user.is_bot:
        return rep.from_user.id, rep.from_user.full_name, rest
    for ent in (msg.entities or []):
        if ent.type == "text_mention" and ent.user:
            return ent.user.id, ent.user.full_name, rest
    for i, t in enumerate(rest):
        if t.startswith("@") and len(t) > 2:
            left = rest[:i] + rest[i + 1:]
            uid = await _resolve_username(t[1:]) if _resolve_username else None
            if uid:
                return uid, t, left
            try:
                ch = await _bot.get_chat(t)
                return ch.id, ch.full_name or t, left
            except Exception:
                return None, None, left
        if t.isdigit() and len(t) >= 6:
            return int(t), t, rest[:i] + rest[i + 1:]
    return None, None, rest


def _split_dur_reason(rest: list, default_sec: int):
    sec, toks = default_sec, list(rest)
    for i, t in enumerate(toks[:2]):
        d = _parse_dur(t)
        if d is not None:
            sec = d
            toks.pop(i)
            break
    return sec, " ".join(toks).strip()


async def _cannot_punish(msg: Message, uid: int) -> bool:
    if uid == _bot.id:
        return True
    if await _is_main_admin(uid):
        await msg.reply("⚠️ Главного админа наказывать нельзя.")
        return True
    try:
        st = (await _bot.get_chat_member(msg.chat.id, uid)).status
        if st == "creator":
            await msg.reply("⚠️ Это создатель чата, его нельзя наказать.")
            return True
    except Exception:
        pass
    return False


@router.message(F.text.regexp(re.compile(_RX, re.IGNORECASE)))
async def punish_cmd(msg: Message):
    if not await _is_main_admin(msg.from_user.id):
        return
    tokens = msg.text.split()
    head = tokens[0].lstrip("/").split("@")[0].lower()
    uid, name, rest = await _resolve(msg, tokens[1:])
    if not uid:
        await msg.reply("Укажите цель: ответом на сообщение, @ником или ID.")
        return
    try:
        if head in _UNBAN:
            await _bot.unban_chat_member(msg.chat.id, uid, only_if_banned=True)
            await msg.reply(f"✅ {_mention(uid, name)} разбанен.", parse_mode="HTML")
        elif head in _UNMUTE:
            await _bot.restrict_chat_member(msg.chat.id, uid, permissions=_LIGHT_PERMS)
            await msg.reply(f"🔊 {_mention(uid, name)} размучен.", parse_mode="HTML")
        elif head in _BAN:
            if await _cannot_punish(msg, uid):
                return
            sec, reason = _split_dur_reason(rest, 0)
            kw = {"until_date": _until(sec)} if sec and 30 <= sec <= 366 * 86400 else {}
            await _bot.ban_chat_member(msg.chat.id, uid, **kw)
            text = f"🔨 {_mention(uid, name)} забанен: <b>{_fmt(sec)}</b>"
            if reason:
                text += f"\nПричина: {html.escape(reason)}"
            await msg.reply(text, parse_mode="HTML")
        elif head in _MUTE:
            if await _cannot_punish(msg, uid):
                return
            sec, reason = _split_dur_reason(rest, 7 * 86400)
            await _bot.restrict_chat_member(msg.chat.id, uid, permissions=ChatPermissions(can_send_messages=False),
                                            until_date=_until(sec or 7 * 86400))
            text = f"🔇 {_mention(uid, name)} в муте: <b>{_fmt(sec or 7 * 86400)}</b>"
            if reason:
                text += f"\nПричина: {html.escape(reason)}"
            await msg.reply(text, parse_mode="HTML")
    except Exception as e:
        await msg.reply(f"❌ Не получилось: {html.escape(str(e))[:200]}", parse_mode="HTML")


def setup(dp, bot, is_main_admin, resolve_username=None):
    global _bot, _is_main_admin, _resolve_username
    _bot, _is_main_admin, _resolve_username = bot, is_main_admin, resolve_username
    dp.include_router(router)
