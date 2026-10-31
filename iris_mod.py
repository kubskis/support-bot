# -*- coding: utf-8 -*-
"""
Модуль «Надзор» — чат-менеджер в духе Iris для групп (этап 1).

Что внутри: ранги 0–5, бан/мут/кик/варн со сроками и причинами, голосование за бан,
ДК/ЛДК (доступ команд), триггеры, настройка чата (приветствие, правила, антирейд, фильтр ссылок),
чистка, статистика, анкета, базовые РП.

Подключение из main.py:  iris_mod.setup(dp, bot, get_db, is_main_admin, skip_chat_ids=[ADMIN_CHAT_ID])
Команды понимаются и текстом («бан 3д», «+модер 2»), и слэшем (/ban 3д).
Включение в чате: создатель пишет «+надзор» (или /nadzor).
"""
import asyncio
import html
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone

from aiogram import BaseMiddleware, F, Router
from aiogram.types import (CallbackQuery, ChatPermissions, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)

log = logging.getLogger("iris_mod")

_bot = None
_get_db = None
_is_main_admin = None
_skip = set()
_enabled_cache = {}          # chat_id -> (bool, ts)
_join_times = {}             # chat_id -> [ts,...] для антирейда
router = Router()
ALIAS_FN = None     # iris_alias.apply: синонимы команд
DYNAMIC = []        # async fn(msg, first, rank)->bool: динамические команды (свои РП)
GRID_HOOKS = {}     # rank / join — подключает iris_grid
STRICT_BANG = {"!кто", "!скажи", "!инфа", "!выбери", "!жребий", "!данет", "!пинг", "!погода", "!ид", "!id"}

# ---------------------------------------------------------------- БД
_DDL = """
CREATE TABLE IF NOT EXISTS im_chats(chat_id BIGINT PRIMARY KEY, enabled BOOLEAN DEFAULT TRUE, cfg JSONB DEFAULT '{}'::jsonb);
CREATE TABLE IF NOT EXISTS im_ranks(chat_id BIGINT, user_id BIGINT, rank INT, by_id BIGINT, ts TIMESTAMPTZ DEFAULT now(), PRIMARY KEY(chat_id,user_id));
CREATE TABLE IF NOT EXISTS im_punish(id SERIAL PRIMARY KEY, chat_id BIGINT, user_id BIGINT, kind TEXT, until TIMESTAMPTZ, reason TEXT, by_id BIGINT, ts TIMESTAMPTZ DEFAULT now(), active BOOLEAN DEFAULT TRUE);
CREATE INDEX IF NOT EXISTS im_punish_ix ON im_punish(chat_id,user_id,kind,active);
CREATE TABLE IF NOT EXISTS im_dk(chat_id BIGINT, code TEXT, rank INT, PRIMARY KEY(chat_id,code));
CREATE TABLE IF NOT EXISTS im_ldk(chat_id BIGINT, user_id BIGINT, code TEXT, PRIMARY KEY(chat_id,user_id,code));
CREATE TABLE IF NOT EXISTS im_triggers(id SERIAL PRIMARY KEY, chat_id BIGINT, event TEXT, priority INT DEFAULT 0, actions JSONB, reason TEXT, by_id BIGINT);
CREATE TABLE IF NOT EXISTS im_members(chat_id BIGINT, user_id BIGINT, name TEXT, username TEXT, msgs INT DEFAULT 0, first_seen TIMESTAMPTZ DEFAULT now(), last_seen TIMESTAMPTZ DEFAULT now(), profile JSONB DEFAULT '{}'::jsonb, PRIMARY KEY(chat_id,user_id));
CREATE INDEX IF NOT EXISTS im_members_un ON im_members(chat_id, lower(username));
CREATE TABLE IF NOT EXISTS im_daily(chat_id BIGINT, user_id BIGINT, day DATE, msgs INT DEFAULT 0, PRIMARY KEY(chat_id,user_id,day));
CREATE TABLE IF NOT EXISTS im_log(id SERIAL PRIMARY KEY, chat_id BIGINT, by_id BIGINT, action TEXT, target BIGINT, details TEXT, ts TIMESTAMPTZ DEFAULT now());
CREATE TABLE IF NOT EXISTS im_votes(chat_id BIGINT, msg_id BIGINT, target BIGINT, need INT, voters BIGINT[] DEFAULT '{}', done BOOLEAN DEFAULT FALSE, PRIMARY KEY(chat_id,msg_id));
"""


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
    await asyncio.to_thread(_q, _DDL)
    for modname in ("iris_eco", "iris_fun", "iris_grid", "iris_import"):
        try:
            mod = __import__(modname)
            await mod.init_tables()
        except Exception:
            log.exception("%s init", modname)


# ---------------------------------------------------------------- понятные ошибки
_ERR_RULES = [
    ("can't remove chat owner", "Это создатель чата — его нельзя заблокировать или ограничить."),
    ("can't restrict self", "Бот не может ограничить сам себя."),
    ("user is an administrator of the chat", "Пользователь — администратор чата, его нельзя заблокировать/ограничить. Сначала снимите с него админство в Telegram."),
    ("not enough rights to restrict", "Нельзя ограничить этого участника: либо у бота нет права «Блокировка пользователей», либо цель — администратор чата. Проверьте права бота и статус цели."),
    ("not enough rights to kick", "У бота нет права блокировать участников, либо цель — администратор чата."),
    ("not enough rights to delete", "У бота нет права удалять сообщения. Включите «Удаление сообщений» в правах администратора."),
    ("not enough rights", "У бота не хватает прав администратора. Выдайте право на нужное действие (блокировка, удаление сообщений, закрепление)."),
    ("chat_admin_required", "Боту нужны права администратора в этом чате."),
    ("need administrator rights", "Боту нужны права администратора в этом чате."),
    ("rights_forbidden", "Telegram запретил действие: у бота недостаточно прав в этом чате."),
    ("right_forbidden", "Telegram запретил действие: у бота недостаточно прав в этом чате."),
    ("participant_id_invalid", "Этот пользователь не найден в чате."),
    ("user_not_participant", "Этого пользователя нет в чате."),
    ("user not found", "Пользователь не найден. Укажите его ответом на сообщение, @ником или числовым id."),
    ("user_id_invalid", "Некорректный id пользователя."),
    ("peer_id_invalid", "Telegram не знает такого пользователя или чата (он ещё не писал боту)."),
    ("member not found", "Участник не найден в этом чате."),
    ("message to delete not found", "Сообщение уже удалено."),
    ("message can't be deleted", "Это сообщение нельзя удалить (слишком старое — старше 48 часов — или у бота нет прав)."),
    ("message to edit not found", "Сообщение для изменения не найдено."),
    ("message is not modified", "Изменений нет — всё уже так и стоит."),
    ("message to reply not found", "Сообщение, на которое нужно ответить, не найдено."),
    ("reply message not found", "Сообщение, на которое нужно ответить, не найдено."),
    ("bot was kicked", "Бота исключили из чата."),
    ("bot is not a member", "Бота нет в этом чате."),
    ("chat not found", "Чат не найден: бот не состоит в нём или код неверный."),
    ("chat_write_forbidden", "В этом чате боту запрещено писать сообщения."),
    ("have no rights to send", "В этом чате боту запрещено писать сообщения."),
    ("not enough rights to send", "В этом чате боту запрещено писать сообщения."),
    ("method is available for supergroup", "Эта функция доступна только в супергруппах. Преобразуйте группу в супергруппу (включите историю чата или темы)."),
    ("only supergroups", "Эта функция доступна только в супергруппах."),
    ("can't initiate conversation", "Бот не может первым написать этому пользователю: пусть сначала запустит бота."),
    ("bot was blocked by the user", "Пользователь заблокировал бота."),
    ("user is deactivated", "Аккаунт пользователя удалён."),
    ("too many requests", "Слишком много запросов к Telegram. Подождите немного и повторите."),
    ("timed out", "Telegram не ответил вовремя. Повторите команду."),
    ("network", "Сетевая ошибка при обращении к Telegram. Повторите позже."),
]


def human_error(e) -> str:
    """Telegram/БД-ошибка → понятное сообщение для чата."""
    raw = str(e)
    low = raw.lower()
    for key, text in _ERR_RULES:
        if key in low:
            extra = ""
            mm = re.search(r"retry after (\d+)", low)
            if key == "too many requests" and mm:
                extra = f" Повторите через {mm.group(1)} с."
            return f"⚠️ <b>Ошибка Телеграм:</b>\n\n💬 {text}{extra}"
    if any(k in low for k in ("psycopg2", "connection", "database", "relation ")):
        return "⚠️ <b>Ошибка базы данных:</b>\n\n💬 Не удалось сохранить или получить данные. Повторите позже; если не проходит — сообщите администратору бота."
    log.warning("необработанная ошибка: %s", raw)
    short = esc(raw.replace("Telegram server says - ", "")[:160])
    return f"⚠️ <b>Ошибка:</b>\n\n💬 Команда не выполнена. Причина: {short}"


# ---------------------------------------------------------------- утилиты
def esc(s):
    return html.escape(str(s or ""))


def mention(uid, name):
    return f'<a href="tg://user?id={uid}">{esc(name) or uid}</a>'


_DUR_RE = re.compile(r"^(\d+)\s*(с|сек|м|мин|ч|час|д|дн|день|дней|н|нед|мес|г|год|s|m|h|d|w)\w*$", re.I)
_UNIT = {"с": 1, "сек": 1, "s": 1, "м": 60, "мин": 60, "m": 60, "ч": 3600, "час": 3600, "h": 3600,
         "д": 86400, "дн": 86400, "день": 86400, "дней": 86400, "d": 86400,
         "н": 604800, "нед": 604800, "w": 604800, "мес": 2592000, "г": 31536000, "год": 31536000}
_FOREVER = ("навсегда", "вечно", "perm", "навечно")


def parse_dur(tok):
    """'3д' -> секунды; 'навсегда' -> 0; нет срока -> None"""
    t = tok.lower().strip()
    if t in _FOREVER:
        return 0
    m = _DUR_RE.match(t)
    if not m:
        return None
    n, u = int(m.group(1)), m.group(2).lower()
    # длинные приставки: 'мес' перед 'м'
    full = t[len(m.group(1)):].strip()
    for key in ("мес", "нед", "дн", "сек", "мин", "час", "год", "день", "дней"):
        if full.startswith(key):
            return n * _UNIT[key]
    return n * _UNIT.get(u, 0)


def fmt_dur(sec):
    if not sec:
        return "навсегда"
    for name, k in (("мес", 2592000), ("д", 86400), ("ч", 3600), ("мин", 60)):
        if sec >= k:
            return f"{sec // k} {name}"
    return f"{sec} с"


# ---------------------------------------------------------------- ранги / доступ
DEFAULT_DK = {
    "варн": 1, "мут": 1, "кик": 2, "бан": 2, "разбан": 2, "снятьварн": 1, "варны": 0, "банлист": 1, "причина": 1,
    "амнистия": 4, "модер": 4, "снять": 4, "понизить": 4, "повысить": 4, "кто_админ": 0, "модерлог": 2,
    "дк": 5, "лдк": 5, "триггер": 4, "настройка": 4, "приветствие": 4, "правила_set": 4, "антирейд": 4,
    "ссылки": 4, "пург": 3, "смс": 2, "кикнеактив": 4, "голосование": 0, "гб": 0,
    "рп": 0, "анкета": 0, "стата": 0, "топ": 0, "кто_я": 0, "онлайн": 0, "чатинфо": 0, "правила": 0,
    "сброс": 5, "вип": 4,
}


async def chat_enabled(chat_id):
    v = _enabled_cache.get(chat_id)
    if v and time.time() - v[1] < 30:
        return v[0]
    r = await q("SELECT enabled FROM im_chats WHERE chat_id=%s", (chat_id,), one=True)
    en = bool(r and r[0])
    _enabled_cache[chat_id] = (en, time.time())
    return en


async def get_cfg(chat_id):
    r = await q("SELECT cfg FROM im_chats WHERE chat_id=%s", (chat_id,), one=True)
    return (r[0] if r and r[0] else {}) or {}


async def set_cfg(chat_id, **kw):
    cfg = await get_cfg(chat_id)
    for k, v in kw.items():
        if v is None:
            cfg.pop(k, None)
        else:
            cfg[k] = v
    await q("INSERT INTO im_chats(chat_id,enabled,cfg) VALUES(%s,TRUE,%s) ON CONFLICT(chat_id) DO UPDATE SET cfg=EXCLUDED.cfg",
            (chat_id, json.dumps(cfg)))


async def get_rank(chat_id, uid):
    try:
        if await _is_main_admin(uid):
            return 5
    except Exception:
        pass
    r = await q("SELECT rank FROM im_ranks WHERE chat_id=%s AND user_id=%s", (chat_id, uid), one=True)
    gr = 0
    if GRID_HOOKS.get("rank"):
        try:
            gr = await GRID_HOOKS["rank"](chat_id, uid)
        except Exception:
            gr = 0
    if r:
        return max(r[0], gr)
    if gr:
        return gr
    try:
        m = await _bot.get_chat_member(chat_id, uid)
        if m.status == "creator":
            await q("INSERT INTO im_ranks(chat_id,user_id,rank) VALUES(%s,%s,5) ON CONFLICT DO NOTHING", (chat_id, uid))
            return 5
    except Exception:
        pass
    return 0


async def need_rank(chat_id, code):
    r = await q("SELECT rank FROM im_dk WHERE chat_id=%s AND code=%s", (chat_id, code), one=True)
    return r[0] if r else DEFAULT_DK.get(code, 4)


async def allowed(chat_id, uid, code, rank=None):
    if rank is None:
        rank = await get_rank(chat_id, uid)
    need = await need_rank(chat_id, code)
    if need >= 6:
        return False
    if rank >= need:
        return True
    r = await q("SELECT 1 FROM im_ldk WHERE chat_id=%s AND user_id=%s AND code=%s", (chat_id, uid, code), one=True)
    return bool(r)


async def logact(chat_id, by, action, target=None, details=""):
    try:
        await q("INSERT INTO im_log(chat_id,by_id,action,target,details) VALUES(%s,%s,%s,%s,%s)",
                (chat_id, by, action, target, details[:500]))
    except Exception:
        log.exception("im_log")


# ---------------------------------------------------------------- участники / цели
async def touch_member(msg: Message):
    u = msg.from_user
    if not u or u.is_bot:
        return
    name = u.full_name
    await q("""INSERT INTO im_members(chat_id,user_id,name,username,msgs) VALUES(%s,%s,%s,%s,1)
               ON CONFLICT(chat_id,user_id) DO UPDATE SET name=EXCLUDED.name, username=EXCLUDED.username,
               msgs=im_members.msgs+1, last_seen=now()""", (msg.chat.id, u.id, name, (u.username or "").lower()))
    await q("""INSERT INTO im_daily(chat_id,user_id,day,msgs) VALUES(%s,%s,CURRENT_DATE,1)
               ON CONFLICT(chat_id,user_id,day) DO UPDATE SET msgs=im_daily.msgs+1""", (msg.chat.id, u.id))


async def member_name(chat_id, uid):
    r = await q("SELECT name FROM im_members WHERE chat_id=%s AND user_id=%s", (chat_id, uid), one=True)
    return r[0] if r and r[0] else str(uid)


async def resolve_target(msg: Message, tokens):
    """Возвращает (uid, name, оставшиеся_токены). Приоритет: reply → text_mention → @username → число."""
    rest = list(tokens)
    rep = msg.reply_to_message
    if rep and rep.from_user and rep.from_user.id != _bot.id:
        u = rep.from_user
        return u.id, u.full_name, rest
    for ent in (msg.entities or []):
        if ent.type == "text_mention" and ent.user:
            return ent.user.id, ent.user.full_name, rest
    for i, t in enumerate(rest):
        if t.startswith("@") and len(t) > 2:
            r = await q("SELECT user_id,name FROM im_members WHERE chat_id=%s AND lower(username)=%s",
                        (msg.chat.id, t[1:].lower()), one=True)
            if r:
                return r[0], r[1], rest[:i] + rest[i + 1:]
            try:
                ch = await _bot.get_chat(t)
                return ch.id, ch.full_name or t, rest[:i] + rest[i + 1:]
            except Exception:
                return None, None, rest
        if t.isdigit() and len(t) >= 6:
            return int(t), await member_name(msg.chat.id, int(t)), rest[:i] + rest[i + 1:]
    return None, None, rest


# ---------------------------------------------------------------- наказания
def _until(sec):
    if not sec:
        return None
    return datetime.now(timezone.utc) + timedelta(seconds=sec)


async def do_ban(chat_id, uid, sec, reason, by):
    until = _until(sec)
    kw = {"until_date": until} if sec and 30 <= sec <= 366 * 86400 else {}
    await _bot.ban_chat_member(chat_id, uid, **kw)
    await q("UPDATE im_punish SET active=FALSE WHERE chat_id=%s AND user_id=%s AND kind='ban' AND active", (chat_id, uid))
    await q("INSERT INTO im_punish(chat_id,user_id,kind,until,reason,by_id) VALUES(%s,%s,'ban',%s,%s,%s)",
            (chat_id, uid, until, reason, by))
    await logact(chat_id, by, "ban", uid, f"{fmt_dur(sec)} {reason}")


async def do_mute(chat_id, uid, sec, reason, by):
    until = _until(sec or 7 * 86400)
    await _bot.restrict_chat_member(chat_id, uid, permissions=ChatPermissions(can_send_messages=False),
                                    until_date=until)
    await q("UPDATE im_punish SET active=FALSE WHERE chat_id=%s AND user_id=%s AND kind='mute' AND active", (chat_id, uid))
    await q("INSERT INTO im_punish(chat_id,user_id,kind,until,reason,by_id) VALUES(%s,%s,'mute',%s,%s,%s)",
            (chat_id, uid, until, reason, by))
    await logact(chat_id, by, "mute", uid, f"{fmt_dur(sec or 7 * 86400)} {reason}")


async def do_unmute(chat_id, uid, by):
    await _bot.restrict_chat_member(chat_id, uid, permissions=ChatPermissions(
        can_send_messages=True, can_send_audios=True, can_send_documents=True, can_send_photos=True,
        can_send_videos=True, can_send_video_notes=True, can_send_voice_notes=True, can_send_polls=True,
        can_send_other_messages=True, can_add_web_page_previews=True))
    await q("UPDATE im_punish SET active=FALSE WHERE chat_id=%s AND user_id=%s AND kind='mute' AND active", (chat_id, uid))
    await logact(chat_id, by, "unmute", uid)


async def do_unban(chat_id, uid, by):
    await _bot.unban_chat_member(chat_id, uid, only_if_banned=True)
    await q("UPDATE im_punish SET active=FALSE WHERE chat_id=%s AND user_id=%s AND kind='ban' AND active", (chat_id, uid))
    await logact(chat_id, by, "unban", uid)


async def do_kick(chat_id, uid, by, reason="", quiet=False):
    await _bot.ban_chat_member(chat_id, uid)
    await _bot.unban_chat_member(chat_id, uid, only_if_banned=True)
    await logact(chat_id, by, "kick", uid, reason)


async def active_warns(chat_id, uid):
    return await q("""SELECT id,reason,until,ts FROM im_punish WHERE chat_id=%s AND user_id=%s AND kind='warn' AND active
                      AND (until IS NULL OR until>now()) ORDER BY ts""", (chat_id, uid), many=True)


async def do_warn(chat_id, uid, sec, reason, by):
    until = _until(sec) if sec else None
    await q("INSERT INTO im_punish(chat_id,user_id,kind,until,reason,by_id) VALUES(%s,%s,'warn',%s,%s,%s)",
            (chat_id, uid, until, reason, by))
    await logact(chat_id, by, "warn", uid, reason)
    cfg = await get_cfg(chat_id)
    limit = int(cfg.get("warn_limit", 3))
    n = len(await active_warns(chat_id, uid))
    if n >= limit:
        ban_sec = int(cfg.get("warn_ban", 86400))
        await do_ban(chat_id, uid, ban_sec, f"лимит варнов ({n}/{limit})", by)
        await q("UPDATE im_punish SET active=FALSE WHERE chat_id=%s AND user_id=%s AND kind='warn'", (chat_id, uid))
        await run_triggers(chat_id, "warnlimit", uid, None)
        return n, limit, True
    return n, limit, False


# ---------------------------------------------------------------- триггеры
EVENTS = {
    "ссылки": "links", "ссылка": "links", "стикеры": "stickers", "стикер": "stickers", "гифки": "gifs",
    "гиф": "gifs", "капс": "caps", "маты": "mat", "мат": "mat", "лимит": "warnlimit", "варны": "warnlimit",
    "боты": "bots", "бот": "bots", "рулетка": "roulette", "дуэли": "duels", "кубы": "dice", "антирейд": "raid",
}
_MAT = re.compile(r"\b(хуй|хуе|хуя|пизд|бляд|блять|ебан|ёбан|ебат|ебал|сука|сучар|мудак|пидор|пидар|залуп)\w*", re.I)
_LINK = re.compile(r"(https?://|t\.me/|www\.|\b[a-z0-9-]+\.(com|ru|org|net|io|me|su|xyz|info)\b)", re.I)


def detect(msg: Message):
    ev = []
    t = (msg.text or msg.caption or "")
    ents = list(msg.entities or []) + list(msg.caption_entities or [])
    if _LINK.search(t) or any(e.type in ("url", "text_link") for e in ents):
        ev.append("links")
    if msg.sticker:
        ev.append("stickers")
    if msg.animation:
        ev.append("gifs")
    letters = [c for c in t if c.isalpha()]
    if len(letters) >= 10 and sum(c.isupper() for c in letters) / len(letters) > 0.7:
        ev.append("caps")
    if _MAT.search(t):
        ev.append("mat")
    return ev


async def run_triggers(chat_id, event, uid, msg, by=0):
    rows = await q("SELECT id,priority,actions,reason FROM im_triggers WHERE chat_id=%s AND event=%s",
                   (chat_id, event), many=True)
    if not rows:
        return False
    top = max(r[1] for r in rows)
    done = False
    for _id, pr, actions, reason in rows:
        if pr != top:
            continue
        done = True
        for act in (actions or [])[:3]:
            kind, sec = act.get("k"), act.get("s", 0)
            try:
                if kind == "warn":
                    await do_warn(chat_id, uid, sec, reason or event, by or _bot.id)
                elif kind == "mute":
                    await do_mute(chat_id, uid, sec, reason or event, by or _bot.id)
                elif kind == "ban":
                    await do_ban(chat_id, uid, sec, reason or event, by or _bot.id)
                elif kind == "kick":
                    await do_kick(chat_id, uid, by or _bot.id, reason or event)
            except Exception as e:
                log.warning("trigger action %s: %s", kind, e)
    if done and msg is not None:
        try:
            await msg.delete()
        except Exception:
            pass
    return done


def parse_trigger(tokens):
    """+триггер ссылки 3 мут 1д / причина ; варн"""
    s = " ".join(tokens)
    reason = ""
    if "/" in s:
        s, reason = s.split("/", 1)
        reason = reason.strip()
    toks = s.split()
    if not toks:
        return None
    ev = EVENTS.get(toks[0].strip("()").lower())
    if not ev:
        return None
    toks = toks[1:]
    prio = 0
    if toks and toks[0].isdigit() and 0 <= int(toks[0]) <= 5:
        prio = int(toks[0])
        toks = toks[1:]
    acts, i = [], 0
    while i < len(toks) and len(acts) < 3:
        k = {"варн": "warn", "мут": "mute", "бан": "ban", "кик": "kick"}.get(toks[i].lower())
        if not k:
            i += 1
            continue
        sec = 0
        if i + 1 < len(toks):
            d = parse_dur(toks[i + 1])
            if d is not None:
                sec = d
                i += 1
        acts.append({"k": k, "s": sec})
        i += 1
    if not acts:
        return None
    return ev, prio, acts, reason


# ---------------------------------------------------------------- командная таблица
CMDS = []   # (список фраз, обработчик, dk-код)


def cmd(phrases, code):
    def deco(fn):
        CMDS.append(([p.lower() for p in phrases], fn, code))
        return fn
    return deco


async def reply(msg, text, **kw):
    try:
        return await msg.reply(text, parse_mode="HTML", disable_web_page_preview=True, **kw)
    except Exception:
        return await _bot.send_message(msg.chat.id, text, parse_mode="HTML", disable_web_page_preview=True)


async def _target_check(msg, my_rank, uid):
    if uid == _bot.id:
        await reply(msg, "Себя я наказывать не буду.")
        return False
    try:
        tm = await _bot.get_chat_member(msg.chat.id, uid)
        if tm.status == "creator":
            await reply(msg, "⚠️ <b>Нельзя:</b>\n\n💬 Это создатель чата — его нельзя наказать.")
            return False
    except Exception:
        pass
    tr = await get_rank(msg.chat.id, uid)
    if tr >= my_rank and tr > 0:
        await reply(msg, "Нельзя наказать участника с равным или более высоким рангом.")
        return False
    return True


async def _split_dur_reason(msg, rest, default_sec):
    sec = default_sec
    toks = list(rest)
    for i, t in enumerate(toks[:2]):
        d = parse_dur(t)
        if d is not None:
            sec = d
            toks.pop(i)
            break
    reason = " ".join(toks).strip()
    if "\n" in (msg.text or ""):
        reason = (msg.text.split("\n", 1)[1]).strip() or reason
    return sec, reason


@cmd(["бан", "ban"], "бан")
async def c_ban(msg, args, rank):
    uid, name, rest = await resolve_target(msg, args)
    if not uid:
        return await reply(msg, "Укажите цель: ответом на сообщение, @ником или id.")
    if not await _target_check(msg, rank, uid):
        return
    cfg = await get_cfg(msg.chat.id)
    sec, reason = await _split_dur_reason(msg, rest, int(cfg.get("ban_default", 0)))
    await do_ban(msg.chat.id, uid, sec, reason, msg.from_user.id)
    await reply(msg, f"🔨 {mention(uid, name)} забанен: <b>{fmt_dur(sec)}</b>" + (f"\nПричина: {esc(reason)}" if reason else ""))


@cmd(["разбан", "вернуть", "unban"], "разбан")
async def c_unban(msg, args, rank):
    uid, name, _ = await resolve_target(msg, args)
    if not uid:
        return await reply(msg, "Укажите цель.")
    await do_unban(msg.chat.id, uid, msg.from_user.id)
    await reply(msg, f"✅ {mention(uid, name)} разбанен.")


@cmd(["кик тихо"], "кик")
async def c_kick_quiet(msg, args, rank):
    uid, name, rest = await resolve_target(msg, args)
    if not uid or not await _target_check(msg, rank, uid):
        return
    await do_kick(msg.chat.id, uid, msg.from_user.id)
    try:
        await msg.delete()
    except Exception:
        pass


@cmd(["кик", "kick"], "кик")
async def c_kick(msg, args, rank):
    uid, name, rest = await resolve_target(msg, args)
    if not uid:
        return await reply(msg, "Укажите цель.")
    if not await _target_check(msg, rank, uid):
        return
    await do_kick(msg.chat.id, uid, msg.from_user.id, " ".join(rest))
    await reply(msg, f"👢 {mention(uid, name)} исключён.")


@cmd(["-мут", "размут", "unmute"], "мут")
async def c_unmute(msg, args, rank):
    uid, name, _ = await resolve_target(msg, args)
    if not uid:
        return await reply(msg, "Укажите цель.")
    await do_unmute(msg.chat.id, uid, msg.from_user.id)
    await reply(msg, f"🔊 {mention(uid, name)} размучен.")


@cmd(["мут", "mute"], "мут")
async def c_mute(msg, args, rank):
    uid, name, rest = await resolve_target(msg, args)
    if not uid:
        return await reply(msg, "Укажите цель.")
    if not await _target_check(msg, rank, uid):
        return
    sec, reason = await _split_dur_reason(msg, rest, 7 * 86400)
    await do_mute(msg.chat.id, uid, sec, reason, msg.from_user.id)
    await reply(msg, f"🔇 {mention(uid, name)} в муте: <b>{fmt_dur(sec)}</b>" + (f"\nПричина: {esc(reason)}" if reason else ""))


@cmd(["-варн", "снять варн", "unwarn"], "снятьварн")
async def c_unwarn(msg, args, rank):
    uid, name, _ = await resolve_target(msg, args)
    if not uid:
        return await reply(msg, "Укажите цель.")
    w = await active_warns(msg.chat.id, uid)
    if not w:
        return await reply(msg, "Активных варнов нет.")
    await q("UPDATE im_punish SET active=FALSE WHERE id=%s", (w[-1][0],))
    await logact(msg.chat.id, msg.from_user.id, "unwarn", uid)
    await reply(msg, f"✅ Варн снят с {mention(uid, name)}. Осталось: {len(w) - 1}")


@cmd(["снять все варны", "-варны"], "снятьварн")
async def c_unwarn_all(msg, args, rank):
    uid, name, _ = await resolve_target(msg, args)
    if not uid:
        return await reply(msg, "Укажите цель.")
    await q("UPDATE im_punish SET active=FALSE WHERE chat_id=%s AND user_id=%s AND kind='warn'", (msg.chat.id, uid))
    await reply(msg, f"✅ Все варны сняты с {mention(uid, name)}.")


@cmd(["варны лимит"], "настройка")
async def c_warn_limit(msg, args, rank):
    if not args or not args[0].isdigit():
        return await reply(msg, "Пример: <code>варны лимит 3</code>")
    await set_cfg(msg.chat.id, warn_limit=max(1, min(int(args[0]), 20)))
    await reply(msg, f"Лимит варнов: {args[0]}")


@cmd(["варны чс"], "настройка")
async def c_warn_ban(msg, args, rank):
    d = parse_dur(args[0]) if args else None
    if d is None:
        return await reply(msg, "Пример: <code>варны чс 7д</code>")
    await set_cfg(msg.chat.id, warn_ban=d)
    await reply(msg, f"При лимите варнов — бан на {fmt_dur(d)}.")


@cmd(["варнлист"], "варн")
async def c_warnlist(msg, args, rank):
    rows = await q("""SELECT user_id,count(*) FROM im_punish WHERE chat_id=%s AND kind='warn' AND active
                      AND (until IS NULL OR until>now()) GROUP BY user_id ORDER BY 2 DESC LIMIT 30""", (msg.chat.id,), many=True)
    if not rows:
        return await reply(msg, "Варнов ни у кого нет.")
    lines = [f"{mention(u, await member_name(msg.chat.id, u))} — {n}" for u, n in rows]
    await reply(msg, "<b>Варнлист</b>\n" + "\n".join(lines))


@cmd(["варн", "warn"], "варн")
async def c_warn(msg, args, rank):
    uid, name, rest = await resolve_target(msg, args)
    if not uid:
        return await reply(msg, "Укажите цель.")
    if not await _target_check(msg, rank, uid):
        return
    sec, reason = await _split_dur_reason(msg, rest, 0)
    n, lim, banned = await do_warn(msg.chat.id, uid, sec, reason, msg.from_user.id)
    await reply(msg, f"⚠️ {mention(uid, name)}: варн {n}/{lim}" + (f"\nПричина: {esc(reason)}" if reason else "")
                + ("\n🔨 Лимит достигнут — бан." if banned else ""))


@cmd(["варны", "мои варны"], "варны")
async def c_warns(msg, args, rank):
    uid, name, _ = await resolve_target(msg, args)
    if not uid:
        uid, name = msg.from_user.id, msg.from_user.full_name
    w = await active_warns(msg.chat.id, uid)
    if not w:
        return await reply(msg, f"У {mention(uid, name)} нет варнов.")
    lines = [f"{i}. {esc(r or 'без причины')} — {t.strftime('%d.%m.%Y')}" for i, (_, r, _u, t) in enumerate(w, 1)]
    await reply(msg, f"<b>Варны {mention(uid, name)}</b>\n" + "\n".join(lines))


@cmd(["банлист"], "банлист")
async def c_banlist(msg, args, rank):
    rows = await q("""SELECT user_id,reason,until FROM im_punish WHERE chat_id=%s AND kind='ban' AND active
                      AND (until IS NULL OR until>now()) ORDER BY ts DESC LIMIT 40""", (msg.chat.id,), many=True)
    if not rows:
        return await reply(msg, "Банов нет.")
    lines = [f"{mention(u, await member_name(msg.chat.id, u))} — {esc(r or '—')} ({'навсегда' if not t else 'до ' + t.strftime('%d.%m %H:%M')})"
             for u, r, t in rows]
    await reply(msg, "<b>Банлист</b>\n" + "\n".join(lines))


@cmd(["муты"], "банлист")
async def c_mutes(msg, args, rank):
    rows = await q("""SELECT user_id,reason,until FROM im_punish WHERE chat_id=%s AND kind='mute' AND active
                      AND (until IS NULL OR until>now()) ORDER BY ts DESC LIMIT 40""", (msg.chat.id,), many=True)
    if not rows:
        return await reply(msg, "Мутов нет.")
    lines = [f"{mention(u, await member_name(msg.chat.id, u))} — {esc(r or '—')} (до {t.strftime('%d.%m %H:%M') if t else '∞'})"
             for u, r, t in rows]
    await reply(msg, "<b>Муты</b>\n" + "\n".join(lines))


@cmd(["причина"], "причина")
async def c_reason(msg, args, rank):
    uid, name, _ = await resolve_target(msg, args)
    if not uid:
        return await reply(msg, "Укажите цель.")
    r = await q("SELECT kind,reason,ts,by_id FROM im_punish WHERE chat_id=%s AND user_id=%s ORDER BY ts DESC LIMIT 1",
                (msg.chat.id, uid), one=True)
    if not r:
        return await reply(msg, "Наказаний не найдено.")
    await reply(msg, f"{mention(uid, name)}: {r[0]} — {esc(r[1] or 'без причины')} ({r[2].strftime('%d.%m.%Y %H:%M')}), выдал {mention(r[3], await member_name(msg.chat.id, r[3]))}")


@cmd(["!амнистия"], "амнистия")
async def c_amnesty(msg, args, rank):
    rows = await q("SELECT DISTINCT user_id FROM im_punish WHERE chat_id=%s AND kind='ban' AND active", (msg.chat.id,), many=True)
    n = 0
    for (u,) in rows:
        try:
            await do_unban(msg.chat.id, u, msg.from_user.id)
            n += 1
        except Exception:
            pass
    await reply(msg, f"🕊 Амнистия: разбанено {n}.")


# ---- ранги
def _rank_arg(rest, default):
    for i, t in enumerate(rest):
        if t.isdigit() and len(t) == 1 and 0 <= int(t) <= 5:
            return int(t), rest[:i] + rest[i + 1:]
    return default, rest


@cmd(["+модер", "+админ", "!модер", "+moder"], "модер")
async def c_addmod(msg, args, rank):
    uid, name, rest = await resolve_target(msg, args)
    if not uid:
        return await reply(msg, "Укажите цель.")
    new, _ = _rank_arg(rest, 1)
    if new >= rank and rank < 5:
        return await reply(msg, "Нельзя выдать ранг выше или равный своему.")
    await q("""INSERT INTO im_ranks(chat_id,user_id,rank,by_id) VALUES(%s,%s,%s,%s)
               ON CONFLICT(chat_id,user_id) DO UPDATE SET rank=EXCLUDED.rank, by_id=EXCLUDED.by_id, ts=now()""",
            (msg.chat.id, uid, new, msg.from_user.id))
    await logact(msg.chat.id, msg.from_user.id, "rank", uid, str(new))
    await reply(msg, f"🛡 {mention(uid, name)} теперь модератор, ранг <b>{new}</b>.")


@cmd(["повысить"], "повысить")
async def c_promote(msg, args, rank):
    uid, name, _ = await resolve_target(msg, args)
    if not uid:
        return await reply(msg, "Укажите цель.")
    cur = await get_rank(msg.chat.id, uid)
    new = min(cur + 1, 5)
    if new >= rank and rank < 5:
        return await reply(msg, "Нельзя поднять до своего ранга или выше.")
    await q("""INSERT INTO im_ranks(chat_id,user_id,rank,by_id) VALUES(%s,%s,%s,%s)
               ON CONFLICT(chat_id,user_id) DO UPDATE SET rank=EXCLUDED.rank""", (msg.chat.id, uid, new, msg.from_user.id))
    await reply(msg, f"⬆️ {mention(uid, name)}: ранг {new}")


@cmd(["понизить"], "понизить")
async def c_demote(msg, args, rank):
    uid, name, _ = await resolve_target(msg, args)
    if not uid or not await _target_check(msg, rank, uid):
        return
    cur = await get_rank(msg.chat.id, uid)
    new = max(cur - 1, 0)
    await q("""INSERT INTO im_ranks(chat_id,user_id,rank,by_id) VALUES(%s,%s,%s,%s)
               ON CONFLICT(chat_id,user_id) DO UPDATE SET rank=EXCLUDED.rank""", (msg.chat.id, uid, new, msg.from_user.id))
    await reply(msg, f"⬇️ {mention(uid, name)}: ранг {new}")


@cmd(["снять", "разжаловать", "-модер", "-админ"], "снять")
async def c_unmod(msg, args, rank):
    uid, name, _ = await resolve_target(msg, args)
    if not uid or not await _target_check(msg, rank, uid):
        return
    await q("DELETE FROM im_ranks WHERE chat_id=%s AND user_id=%s", (msg.chat.id, uid))
    await logact(msg.chat.id, msg.from_user.id, "unrank", uid)
    await reply(msg, f"{mention(uid, name)} больше не модератор.")


@cmd(["кто админ", "кто модер", "модеры", "админы"], "кто_админ")
async def c_who_admin(msg, args, rank):
    rows = await q("SELECT user_id,rank FROM im_ranks WHERE chat_id=%s AND rank>0 ORDER BY rank DESC", (msg.chat.id,), many=True)
    if not rows:
        return await reply(msg, "Модераторов нет.")
    out = ["<b>Модераторы</b>"]
    for u, r in rows:
        out.append(f"{r} — {mention(u, await member_name(msg.chat.id, u))}")
    await reply(msg, "\n".join(out))


@cmd(["модер лог", "лог"], "модерлог")
async def c_modlog(msg, args, rank):
    rows = await q("SELECT by_id,action,target,details,ts FROM im_log WHERE chat_id=%s ORDER BY id DESC LIMIT 20", (msg.chat.id,), many=True)
    if not rows:
        return await reply(msg, "Лог пуст.")
    out = ["<b>Последние действия</b>"]
    for by, a, t, d, ts in rows:
        out.append(f"{ts.astimezone(timezone(timedelta(hours=3))).strftime('%d.%m %H:%M')} {esc(await member_name(msg.chat.id, by))} → {a} "
                   f"{esc(await member_name(msg.chat.id, t)) if t else ''} {esc(d)}")
    await reply(msg, "\n".join(out))


# ---- ДК / ЛДК
@cmd(["!дк"], "дк")
async def c_dk_set(msg, args, rank):
    if len(args) < 2 or not (args[-1].isdigit() or args[-1].lower() == "выкл"):
        return await reply(msg, "Пример: <code>!дк бан 4</code> (0 — всем, 6/выкл — отключено)")
    val = 6 if args[-1].lower() == "выкл" else max(0, min(int(args[-1]), 6))
    code = " ".join(args[:-1]).lower().replace(" ", "_") if " ".join(args[:-1]).lower() in ("кто админ", "кто я", "модер лог", "кик неактив") else args[0].lower()
    code = {"кто_админ": "кто_админ", "модер_лог": "модерлог", "кик_неактив": "кикнеактив", "кто_я": "кто_я"}.get(code, code)
    if code not in DEFAULT_DK:
        return await reply(msg, "Неизвестный код. Список: <code>дк</code>")
    await q("INSERT INTO im_dk(chat_id,code,rank) VALUES(%s,%s,%s) ON CONFLICT(chat_id,code) DO UPDATE SET rank=EXCLUDED.rank",
            (msg.chat.id, code, val))
    await logact(msg.chat.id, msg.from_user.id, "dk", None, f"{code}={val}")
    await reply(msg, f"ДК: <b>{esc(code)}</b> → {'отключена' if val >= 6 else 'ранг ' + str(val)}")


@cmd(["дк", "доступ команд", "мдк"], "дк")
async def c_dk_show(msg, args, rank):
    rows = dict(await q("SELECT code,rank FROM im_dk WHERE chat_id=%s", (msg.chat.id,), many=True) or [])
    out = ["<b>Доступ команд</b> (код — ранг; * изменено)"]
    line = []
    for code, d in sorted(DEFAULT_DK.items()):
        cur = rows.get(code, d)
        line.append(f"{code} {'выкл' if cur >= 6 else cur}{'*' if code in rows else ''}")
    out.append(" · ".join(line))
    out.append("\nИзменить: <code>!дк бан 4</code>. Личный доступ: <code>+лдк бан @ник</code>. Сброс: <code>!сброс дк</code>")
    await reply(msg, "\n".join(out))


@cmd(["!сброс дк", "!сброс команд"], "сброс")
async def c_dk_reset(msg, args, rank):
    await q("DELETE FROM im_dk WHERE chat_id=%s", (msg.chat.id,))
    await reply(msg, "ДК сброшены к значениям по умолчанию.")


@cmd(["+лдк"], "лдк")
async def c_ldk_add(msg, args, rank):
    uid, name, rest = await resolve_target(msg, args)
    if not uid or not rest or rest[0].lower() not in DEFAULT_DK:
        return await reply(msg, "Пример: <code>+лдк бан @ник</code>")
    await q("INSERT INTO im_ldk VALUES(%s,%s,%s) ON CONFLICT DO NOTHING", (msg.chat.id, uid, rest[0].lower()))
    await reply(msg, f"{mention(uid, name)} получил личный доступ к «{esc(rest[0])}».")


@cmd(["-лдк"], "лдк")
async def c_ldk_del(msg, args, rank):
    uid, name, rest = await resolve_target(msg, args)
    if not uid or not rest:
        return await reply(msg, "Пример: <code>-лдк бан @ник</code>")
    await q("DELETE FROM im_ldk WHERE chat_id=%s AND user_id=%s AND code=%s", (msg.chat.id, uid, rest[0].lower()))
    await reply(msg, "Личный доступ снят.")


@cmd(["лдк"], "лдк")
async def c_ldk_list(msg, args, rank):
    rows = await q("SELECT user_id,code FROM im_ldk WHERE chat_id=%s ORDER BY user_id", (msg.chat.id,), many=True)
    if not rows:
        return await reply(msg, "Личных доступов нет.")
    out = ["<b>ЛДК</b>"] + [f"{mention(u, await member_name(msg.chat.id, u))} — {c}" for u, c in rows]
    await reply(msg, "\n".join(out))


# ---- триггеры
@cmd(["+триггер"], "триггер")
async def c_trig_add(msg, args, rank):
    p = parse_trigger(args)
    if not p:
        return await reply(msg, "Пример: <code>+триггер ссылки 2 мут 1д / реклама</code>\nСобытия: ссылки, стикеры, гифки, капс, маты, лимит, боты")
    ev, prio, acts, reason = p
    await q("INSERT INTO im_triggers(chat_id,event,priority,actions,reason,by_id) VALUES(%s,%s,%s,%s,%s,%s)",
            (msg.chat.id, ev, prio, json.dumps(acts), reason, msg.from_user.id))
    await reply(msg, f"⚡ Триггер добавлен: {ev}, приоритет {prio}, действия: " + ", ".join(f"{a['k']} {fmt_dur(a['s']) if a['s'] else ''}".strip() for a in acts))


@cmd(["-триггер"], "триггер")
async def c_trig_del(msg, args, rank):
    if not args or not args[0].isdigit():
        return await reply(msg, "Пример: <code>-триггер 5</code> (номер из списка)")
    await q("DELETE FROM im_triggers WHERE chat_id=%s AND id=%s", (msg.chat.id, int(args[0])))
    await reply(msg, "Триггер удалён.")


@cmd(["триггеры", "триги", "триггер"], "триггер")
async def c_trig_list(msg, args, rank):
    rows = await q("SELECT id,event,priority,actions,reason FROM im_triggers WHERE chat_id=%s ORDER BY id", (msg.chat.id,), many=True)
    if not rows:
        return await reply(msg, "Триггеров нет.")
    out = ["<b>Триггеры</b>"]
    for i, ev, pr, acts, rs in rows:
        out.append(f"#{i} {ev} (пр. {pr}): " + ", ".join(a["k"] + (" " + fmt_dur(a["s"]) if a["s"] else "") for a in acts) + (f" / {esc(rs)}" if rs else ""))
    await reply(msg, "\n".join(out))


# ---- настройка чата
@cmd(["+приветствие", "+привет"], "приветствие")
async def c_welcome_set(msg, args, rank):
    text = (msg.text or "").split(None, 1)[1] if len((msg.text or "").split(None, 1)) > 1 else ""
    if msg.reply_to_message and not args:
        text = msg.reply_to_message.html_text or ""
    if not text:
        return await reply(msg, "Пример: <code>+приветствие Привет, {имя}!</code>")
    await set_cfg(msg.chat.id, welcome=text)
    await reply(msg, "Приветствие сохранено.")


@cmd(["-приветствие"], "приветствие")
async def c_welcome_off(msg, args, rank):
    await set_cfg(msg.chat.id, welcome=None)
    await reply(msg, "Приветствие выключено.")


@cmd(["+правила"], "правила_set")
async def c_rules_set(msg, args, rank):
    parts = (msg.text or "").split(None, 1)
    text = parts[1] if len(parts) > 1 else (msg.reply_to_message.html_text if msg.reply_to_message else "")
    if not text:
        return await reply(msg, "Пример: <code>+правила 1. Не спамить…</code>")
    await set_cfg(msg.chat.id, rules=text)
    await reply(msg, "Правила сохранены.")


@cmd(["правила", "вызов правил"], "правила")
async def c_rules(msg, args, rank):
    cfg = await get_cfg(msg.chat.id)
    await reply(msg, cfg.get("rules") or "Правила не заданы.")


@cmd(["антирейд"], "антирейд")
async def c_antiraid(msg, args, rank):
    if not args or not args[0].isdigit():
        return await reply(msg, "Пример: <code>антирейд 10</code> (максимум входов в минуту; 0 — выкл)")
    await set_cfg(msg.chat.id, antiraid=int(args[0]) or None)
    await reply(msg, f"Антирейд: {args[0] if int(args[0]) else 'выкл'}")


@cmd(["-ссылки"], "ссылки")
async def c_links_off(msg, args, rank):
    await set_cfg(msg.chat.id, nolinks=True)
    await reply(msg, "Ссылки от участников (ранг 0) будут удаляться.")


@cmd(["+ссылки"], "ссылки")
async def c_links_on(msg, args, rank):
    await set_cfg(msg.chat.id, nolinks=None)
    await reply(msg, "Ссылки разрешены.")


@cmd(["-смс"], "смс")
async def c_delmsg(msg, args, rank):
    if msg.reply_to_message:
        try:
            await msg.reply_to_message.delete()
            await msg.delete()
        except Exception as e:
            await reply(msg, human_error(e))


@cmd(["!пург", "пург"], "пург")
async def c_purge(msg, args, rank):
    if not msg.reply_to_message:
        return await reply(msg, "Ответьте на сообщение, начиная с которого удалять.")
    a, b = msg.reply_to_message.message_id, msg.message_id
    ids = list(range(a, b + 1))[-200:]
    n = 0
    for i in range(0, len(ids), 100):
        try:
            await _bot.delete_messages(msg.chat.id, ids[i:i + 100])
            n += len(ids[i:i + 100])
        except Exception:
            pass
    await logact(msg.chat.id, msg.from_user.id, "purge", None, str(n))


@cmd(["кик неактив", "кик молчунов"], "кикнеактив")
async def c_kick_inactive(msg, args, rank):
    silent = (msg.text or "").lower().startswith("кик молч")
    days = int(args[0]) if args and args[0].isdigit() else 30
    if silent:
        rows = await q("SELECT user_id FROM im_members WHERE chat_id=%s AND msgs<=1 AND first_seen<now()-interval '%s days'" % ("%s", days), (msg.chat.id,), many=True)
    else:
        rows = await q("SELECT user_id FROM im_members WHERE chat_id=%s AND last_seen<now()-interval '%s days'" % ("%s", days), (msg.chat.id,), many=True)
    n = 0
    for (u,) in rows:
        if await get_rank(msg.chat.id, u) > 0:
            continue
        try:
            await do_kick(msg.chat.id, u, msg.from_user.id, "неактив")
            n += 1
        except Exception:
            pass
    await reply(msg, f"Исключено: {n}")


# ---- голосование за бан
@cmd(["гб"], "гб")
async def c_vote_ban(msg, args, rank):
    uid, name, rest = await resolve_target(msg, args)
    if not uid or not await _target_check(msg, 1, uid):
        return
    need = int(rest[0]) if rest and rest[0].isdigit() else 5
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="👍 За бан (0)", callback_data="imgb:y")]])
    sent = await reply(msg, f"🗳 Голосование за бан {mention(uid, name)}. Нужно голосов: <b>{need}</b>", reply_markup=kb)
    await q("INSERT INTO im_votes(chat_id,msg_id,target,need) VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING",
            (msg.chat.id, sent.message_id, uid, need))


@router.callback_query(F.data == "imgb:y")
async def cb_vote(cb: CallbackQuery):
    chat_id, mid = cb.message.chat.id, cb.message.message_id
    r = await q("SELECT target,need,voters,done FROM im_votes WHERE chat_id=%s AND msg_id=%s", (chat_id, mid), one=True)
    if not r or r[3]:
        return await cb.answer("Голосование завершено.")
    target, need, voters, _ = r
    if cb.from_user.id in voters:
        return await cb.answer("Вы уже голосовали.")
    voters = list(voters) + [cb.from_user.id]
    done = len(voters) >= need
    await q("UPDATE im_votes SET voters=%s, done=%s WHERE chat_id=%s AND msg_id=%s", (voters, done, chat_id, mid))
    if done:
        try:
            await do_ban(chat_id, target, 0, "по итогам голосования", _bot.id)
            await cb.message.edit_text(f"🔨 {mention(target, await member_name(chat_id, target))} забанен по голосованию ({len(voters)}/{need}).", parse_mode="HTML")
        except Exception as e:
            await cb.message.edit_text(human_error(e), parse_mode="HTML")
    else:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"👍 За бан ({len(voters)}/{need})", callback_data="imgb:y")]])
        await cb.message.edit_reply_markup(reply_markup=kb)
    await cb.answer("Голос принят")


# ---- статистика / анкета
_MSK = timezone(timedelta(hours=3))

# ---------------------------------------------------------------- графики активности (Pillow)
def make_chart(values, dates, title_num=None):
    import iris_card
    return iris_card.render_chart(values, dates, "Активность чата за 30 дней", f"Сообщений за период: {title_num if title_num is not None else sum(values)}")


async def _series(chat_id, uid=None, days=30):
    if uid:
        rows = await q("SELECT day,msgs FROM im_daily WHERE chat_id=%s AND user_id=%s AND day>CURRENT_DATE-%s", (chat_id, uid, days), many=True)
    else:
        rows = await q("SELECT day,sum(msgs) FROM im_daily WHERE chat_id=%s AND day>CURRENT_DATE-%s GROUP BY day", (chat_id, days), many=True)
    m = {r[0]: int(r[1]) for r in rows}
    today = datetime.now(_MSK).date()
    ds = [today - timedelta(days=days - 1 - i) for i in range(days)]
    return [m.get(x, 0) for x in ds], ds


async def reply_chart(msg, text, vals, days, num=None):
    from aiogram.types import BufferedInputFile
    try:
        png = await asyncio.to_thread(make_chart, vals, days, num)
        if len(text) <= 1000:
            return await msg.reply_photo(BufferedInputFile(png, "activity.png"), caption=text, parse_mode="HTML")
        await msg.reply_photo(BufferedInputFile(png, "activity.png"))
    except Exception:
        log.exception("chart")
    return await reply(msg, text)



def _ago(ts):
    s = int((datetime.now(timezone.utc) - ts).total_seconds())
    return "только что" if s < 60 else f"{s // 60} мин назад" if s < 3600 else f"{s // 3600} ч назад" if s < 86400 else f"{s // 86400} д назад"


_PROF = {"ник": ("nick", 30), "звание": ("title", 30), "девиз": ("motto", 100), "город": ("city", 40), "о себе": ("about", 800),
         "др": ("bday", 12), "пол": ("gender", 10)}


async def _prof_set(msg, key, val):
    """Атомарно: создаёт строку участника при необходимости и меняет одно поле анкеты."""
    cid, uid = msg.chat.id, msg.from_user.id
    if val:
        await q("""INSERT INTO im_members(chat_id,user_id,name,username,profile) VALUES(%s,%s,%s,%s,jsonb_build_object(%s::text,%s::text))
                   ON CONFLICT(chat_id,user_id) DO UPDATE
                   SET profile=coalesce(im_members.profile,'{}'::jsonb) || jsonb_build_object(%s::text,%s::text)""",
                (cid, uid, msg.from_user.full_name, (msg.from_user.username or "").lower(), key, val, key, val))
    else:
        await q("UPDATE im_members SET profile=coalesce(profile,'{}'::jsonb) - %s::text WHERE chat_id=%s AND user_id=%s", (key, cid, uid))
    chk = await q("SELECT profile->>%s::text FROM im_members WHERE chat_id=%s AND user_id=%s", (key, cid, uid), one=True)
    return chk[0] if chk else None


def _mk_prof(label, key, limit):
    @cmd([f"+{label}", f"+{label} "], "анкета")
    async def _h(msg, args, rank, key=key, limit=limit, label=label):
        val = " ".join(args)[:limit]
        if not val:
            return await reply(msg, f"Пример: <code>+{label} …</code>")
        saved = await _prof_set(msg, key, val)
        if saved == val:
            await reply(msg, f"Сохранено ✅ {label}: <b>{esc(val)}</b>")
        else:
            await reply(msg, "⚠️ Не удалось сохранить, попробуйте ещё раз.")

    @cmd([f"-{label}"], "анкета")
    async def _d(msg, args, rank, key=key, label=label):
        await _prof_set(msg, key, "")
        await reply(msg, f"Удалено: {label}.")


for _l, (_k, _lim) in _PROF.items():
    _mk_prof(_l, _k, _lim)


def _top_sql(period):
    return {"сутки": "day=CURRENT_DATE", "неделя": "day>CURRENT_DATE-7", "месяц": "day>CURRENT_DATE-30", "вся": "TRUE"}[period]


@cmd(["топ"], "топ")
async def c_top(msg, args, rank):
    per = (args[0].lower() if args else "сутки")
    if per not in ("сутки", "неделя", "месяц", "вся"):
        per = "сутки"
    rows = await q(f"SELECT user_id,sum(msgs) s FROM im_daily WHERE chat_id=%s AND {_top_sql(per)} GROUP BY user_id ORDER BY s DESC LIMIT 15", (msg.chat.id,), many=True)
    if not rows:
        return await reply(msg, "Данных пока нет.")
    medals = ["🥇", "🥈", "🥉"]
    out = [f"<b>Топ за период: {per}</b>"]
    for i, (u, s) in enumerate(rows):
        out.append(f"{medals[i] if i < 3 else str(i + 1) + '.'} {esc(await member_name(msg.chat.id, u))} — {s}")
    await reply(msg, "\n".join(out))


@cmd(["чат инфо", "чат стата", "чатинфо"], "чатинфо")
async def c_chatinfo(msg, args, rank):
    cid = msg.chat.id
    tot = await q("SELECT count(*),coalesce(sum(msgs),0) FROM im_members WHERE chat_id=%s", (cid,), one=True)
    today = await q("SELECT coalesce(sum(msgs),0),count(DISTINCT user_id) FROM im_daily WHERE chat_id=%s AND day=CURRENT_DATE", (cid,), one=True)
    week = await q("SELECT coalesce(sum(msgs),0) FROM im_daily WHERE chat_id=%s AND day>CURRENT_DATE-7", (cid,), one=True)
    try:
        cnt = await _bot.get_chat_member_count(cid)
    except Exception:
        cnt = "?"
    vals, dys = await _series(cid)
    await reply_chart(msg, f"<b>{esc(msg.chat.title)}</b>\nУчастников: {cnt} (в базе {tot[0]})\nСообщений всего: {tot[1]}\n"
                           f"Сегодня: {today[0]} от {today[1]} чел.\nЗа неделю: {week[0]}\n📊 Сообщения за 30 дней на графике", vals, dys, sum(vals))


@cmd(["кто онлайн", "онлайн"], "онлайн")
async def c_online(msg, args, rank):
    rows = await q("SELECT user_id,name FROM im_members WHERE chat_id=%s AND last_seen>now()-interval '10 minutes' ORDER BY last_seen DESC LIMIT 40", (msg.chat.id,), many=True)
    await reply(msg, "<b>Недавно писали</b>\n" + ("\n".join(esc(n) for _, n in rows) if rows else "никого"))


@cmd(["олды"], "стата")
async def c_olds(msg, args, rank):
    rows = await q("SELECT name,first_seen FROM im_members WHERE chat_id=%s ORDER BY first_seen LIMIT 15", (msg.chat.id,), many=True)
    await reply(msg, "<b>Олды</b>\n" + "\n".join(f"{esc(n)} — с {t.astimezone(_MSK).strftime('%d.%m.%Y')}" for n, t in rows))


@cmd(["новички"], "стата")
async def c_news(msg, args, rank):
    rows = await q("SELECT name,first_seen FROM im_members WHERE chat_id=%s ORDER BY first_seen DESC LIMIT 15", (msg.chat.id,), many=True)
    await reply(msg, "<b>Новички</b>\n" + "\n".join(f"{esc(n)} — {t.astimezone(_MSK).strftime('%d.%m.%Y')}" for n, t in rows))


# ---- базовые РП
_RP = {
    "обнять": "обнял(а)", "поцеловать": "поцеловал(а)", "ударить": "ударил(а)", "погладить": "погладил(а)",
    "пнуть": "пнул(а)", "укусить": "укусил(а)", "лизнуть": "лизнул(а)", "кусь": "сделал(а) кусь", "шлепнуть": "шлёпнул(а)",
    "обнимашки": "крепко обнял(а)", "подарить": "сделал(а) подарок", "понюхать": "понюхал(а)", "пожать руку": "пожал(а) руку",
    "помахать": "помахал(а)", "накормить": "накормил(а)", "напоить": "напоил(а)", "поздравить": "поздравил(а)",
    "извиниться": "извинился(ась) перед", "похвалить": "похвалил(а)", "ущипнуть": "ущипнул(а)", "обозвать": "обозвал(а)",
    "отшлёпать": "отшлёпал(а)", "расстрелять": "расстрелял(а)", "прогнать": "прогнал(а)", "спасти": "спас(ла)", "отравить": "отравил(а)",
    "связать": "связал(а)", "арестовать": "арестовал(а)", "рассмешить": "рассмешил(а)", "наорать": "наорал(а) на", "унизить": "унизил(а)",
}


def _mk_rp(name, verb):
    @cmd([name], "рп")
    async def _h(msg, args, rank, verb=verb):
        cfg = await get_cfg(msg.chat.id)
        if cfg.get("rp_off"):
            return
        uid, tname, _ = await resolve_target(msg, args)
        if not uid:
            return
        await reply(msg, f"{mention(msg.from_user.id, msg.from_user.full_name)} {verb} {mention(uid, tname)}")


for _n, _v in _RP.items():
    _mk_rp(_n, _v)


@cmd(["-рп"], "настройка")
async def c_rp_off(msg, args, rank):
    await set_cfg(msg.chat.id, rp_off=True)
    await reply(msg, "РП-команды отключены.")


@cmd(["+рп"], "настройка")
async def c_rp_on(msg, args, rank):
    await set_cfg(msg.chat.id, rp_off=None)
    await reply(msg, "РП-команды включены.")


@cmd(["рп список", "рп"], "рп")
async def c_rp_list(msg, args, rank):
    await reply(msg, "<b>РП-команды</b> (ответом на сообщение):\n" + ", ".join(sorted(_RP)))


# ---- справка
@cmd(["надзор", "помощь надзор", "команды надзор"], "кто_я")
async def c_help(msg, args, rank):
    await reply(msg, (
        "<b>Надзор — команды</b>\n"
        "<b>Наказания:</b> бан [срок] [причина], разбан, кик, кик тихо, мут [срок], -мут, варн, -варн, варны, варнлист, банлист, муты, причина, !амнистия, гб [N]\n"
        "<b>Ранги:</b> +модер [1-5], повысить, понизить, снять, кто админ, модер лог\n"
        "<b>ДК:</b> дк, !дк код ранг, +лдк/-лдк код @ник, лдк, !сброс дк\n"
        "<b>Триггеры:</b> +триггер ссылки 2 мут 1д / причина, триггеры, -триггер N\n"
        "<b>Чат:</b> +приветствие, +правила, правила, антирейд N, -ссылки/+ссылки, -смс, !пург, кик неактив [дней], кик молчунов, варны лимит N, варны чс срок, +рп/-рп\n"
        "<b>Стата:</b> кто я, топ [сутки|неделя|месяц|вся], чат инфо, кто онлайн, олды, новички, +ник/+звание/+девиз/+город/+о себе/+др/+пол\n"
        "<b>РП:</b> рп список (ответом на сообщение)\n"
        "<b>Экономика и игры:</b> баланс, ферма, передать N, топ богачей, дуэль [ставка], рулетка, кубы [ставка], брак, мой брак, браки, развод\n"
        "<b>Клубы:</b> создать клан/кружок, клан вступить, кланы, кружки, клан созвать\n"
        "<b>Прочее:</b> +, -, рейтинг, наградить, награды, +заметка, +закладка, +таймер, карта, +мрп, созвать всех/онлайн/модеров, !инфа, !выбери, !погода, !ид\n"
        "<b>Сетка:</b> создать сетку, установить сетку, чаты, глобан, глоразбан, гломут, +глмодер\n"
        "Команды можно писать по-разному: «я кто», «ты кто», «забань», «кикни», «замуть», «сколько монет» и т.д.\n"
        "Сроки: 30м, 2ч, 3д, 1н, 1мес, навсегда."))


# ---------------------------------------------------------------- диспетчер
_CMD_SORTED = None


def _sorted_cmds():
    global _CMD_SORTED
    if _CMD_SORTED is None:
        flat = []
        for phrases, fn, code in CMDS:
            for p in phrases:
                flat.append((p.strip(), fn, code))
        flat.sort(key=lambda x: -len(x[0]))
        _CMD_SORTED = flat
    return _CMD_SORTED


def _normalize(text):
    t = (text or "").strip()
    if not t:
        return None
    first = t.split("\n", 1)[0]
    if first.startswith("/"):
        first = first[1:]
        first = re.sub(r"@\w+", "", first, count=1)
        # слэш-алиасы
        alias = {"ban": "бан", "unban": "разбан", "kick": "кик", "mute": "мут", "unmute": "-мут", "warn": "варн", "unwarn": "-варн",
                 "warns": "варны", "nadzor": "надзор", "rules": "правила", "top": "топ", "me": "кто я", "banlist": "банлист",
                 "moder": "+модер", "dk": "дк", "purge": "!пург", "who": "кто я", "rp": "рп список"}
        w = first.split(None, 1)
        head = alias.get(w[0].lower())
        if not head:
            return None
        first = head + (" " + w[1] if len(w) > 1 else "")
    return first


def _match(first):
    low = first.lower()
    for ph, fn, code in _sorted_cmds():
        if low == ph or low.startswith(ph + " "):
            return fn, code, first[len(ph):].split()
        # «! бан», «!бан» → бан (восклицательный знак необязателен)
        if ph in STRICT_BANG:
            continue
        if ph.startswith("!") and (low == ph[1:] or low.startswith(ph[1:] + " ")):
            return fn, code, first[len(ph) - 1:].split()
        if low.lstrip("!") == ph or low.lstrip("!").startswith(ph + " "):
            return fn, code, first.lstrip("!")[len(ph):].split()
    return None


async def _activate(msg: Message):
    cid = msg.chat.id
    try:
        m = await _bot.get_chat_member(cid, msg.from_user.id)
        ok = m.status == "creator" or (m.status == "administrator" and True) or await _is_main_admin(msg.from_user.id)
    except Exception:
        ok = await _is_main_admin(msg.from_user.id)
    if not ok:
        return await reply(msg, "Включить надзор может создатель или администратор чата.")
    await q("INSERT INTO im_chats(chat_id,enabled) VALUES(%s,TRUE) ON CONFLICT(chat_id) DO UPDATE SET enabled=TRUE", (cid,))
    _enabled_cache.pop(cid, None)
    await q("INSERT INTO im_ranks(chat_id,user_id,rank) VALUES(%s,%s,5) ON CONFLICT(chat_id,user_id) DO UPDATE SET rank=5", (cid, msg.from_user.id))
    await reply(msg, "🛡 <b>Надзор включён.</b> Вы — ранг 5. Для работы выдайте боту права администратора (бан, удаление, ограничение).\nСписок команд: <code>надзор</code>")


async def _deactivate(msg: Message):
    if not await get_rank(msg.chat.id, msg.from_user.id) >= 5:
        return
    await q("UPDATE im_chats SET enabled=FALSE WHERE chat_id=%s", (msg.chat.id,))
    _enabled_cache.pop(msg.chat.id, None)
    await reply(msg, "Надзор выключен.")


async def _on_message(msg: Message):
    cid = msg.chat.id
    # приветствие / антирейд на сервисных сообщениях
    if msg.new_chat_members:
        return await _on_join(msg)
    if not msg.from_user or msg.from_user.is_bot:
        return
    first = _normalize(msg.text) if msg.text else None
    if first and ALIAS_FN:
        try:
            first = ALIAS_FN(first, msg)
        except Exception:
            log.exception("alias")
    low = (first or "").lower()
    if low in ("+надзор", "надзор старт", "надзор вкл"):
        return await _activate(msg)
    if not await chat_enabled(cid):
        return
    if low in ("-надзор", "надзор выкл"):
        return await _deactivate(msg)
    await touch_member(msg)
    uid = msg.from_user.id
    rank = await get_rank(cid, uid)
    # командный разбор
    if first:
        m = _match(first)
        if m:
            fn, code, args = m
            if await allowed(cid, uid, code, rank):
                try:
                    await fn(msg, args, rank)
                except Exception as e:
                    log.exception("cmd %s", code)
                    try:
                        await reply(msg, human_error(e))
                    except Exception:
                        pass
            elif rank > 0 or code in ("дк", "лдк", "сброс"):
                await reply(msg, "Недостаточно прав для этой команды.")
            return
        for dyn in DYNAMIC:
            try:
                if await dyn(msg, first, rank):
                    return
            except Exception:
                log.exception("dynamic cmd")
    # автомодерация: модераторы (ранг ≥1) не трогаются
    if rank >= 1:
        return
    cfg = await get_cfg(cid)
    events = detect(msg)
    if cfg.get("nolinks") and "links" in events:
        try:
            await msg.delete()
        except Exception:
            pass
        return
    for ev in events:
        if await run_triggers(cid, ev, uid, msg):
            return


async def _on_join(msg: Message):
    cid = msg.chat.id
    if not await chat_enabled(cid):
        return
    cfg = await get_cfg(cid)
    lim = cfg.get("antiraid")
    now = time.time()
    lst = [t for t in _join_times.get(cid, []) if now - t < 60] + [now] * len(msg.new_chat_members)
    _join_times[cid] = lst
    for u in msg.new_chat_members:
        if u.is_bot:
            if await run_triggers(cid, "bots", msg.from_user.id if msg.from_user and msg.from_user.id != u.id else u.id, None):
                try:
                    await do_kick(cid, u.id, _bot.id, "гостевой бот")
                except Exception:
                    pass
            continue
        await q("""INSERT INTO im_members(chat_id,user_id,name,username) VALUES(%s,%s,%s,%s)
                   ON CONFLICT(chat_id,user_id) DO UPDATE SET name=EXCLUDED.name,username=EXCLUDED.username,last_seen=now()""",
                (cid, u.id, u.full_name, (u.username or "").lower()))
        if GRID_HOOKS.get("join"):
            try:
                if await GRID_HOOKS["join"](cid, u.id):
                    continue
            except Exception:
                log.exception("grid join")
        if lim and len(lst) > int(lim):
            try:
                await do_kick(cid, u.id, _bot.id, "антирейд")
            except Exception:
                pass
            continue
        w = cfg.get("welcome")
        if w:
            txt = w.replace("{имя}", mention(u.id, u.full_name)).replace("{ссылка}", mention(u.id, u.full_name))
            try:
                await _bot.send_message(cid, txt, parse_mode="HTML", disable_web_page_preview=True)
            except Exception:
                pass


class IrisMiddleware(BaseMiddleware):
    """Выполняется ДО остальных обработчиков, потом передаёт управление дальше."""

    async def __call__(self, handler, event, data):
        try:
            if isinstance(event, Message) and event.chat.type in ("group", "supergroup") and event.chat.id not in _skip:
                await _on_message(event)
        except Exception:
            log.exception("iris middleware")
        return await handler(event, data)


def setup(dp, bot, get_db, is_main_admin, skip_chat_ids=()):
    global _bot, _get_db, _is_main_admin, _skip
    _bot, _get_db, _is_main_admin = bot, get_db, is_main_admin
    _skip = set(skip_chat_ids)
    for modname in ("iris_eco", "iris_fun", "iris_grid", "iris_card", "iris_alias", "iris_import"):
        try:
            __import__(modname)   # регистрируют свои команды в CMDS
        except Exception:
            log.exception("%s import", modname)
    try:
        import iris_fun
        asyncio.create_task(iris_fun.worker())
    except Exception:
        log.exception("iris_fun worker")
    dp.message.outer_middleware(IrisMiddleware())
    try:
        import iris_import
        iris_import.install(dp)
    except Exception:
        log.exception("iris_import install")
    dp.include_router(router)
