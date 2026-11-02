# -*- coding: utf-8 -*-
"""Антирейд: следит за входами и поведением новичков во ВСЕХ чатах, где есть бот (не зависит от +надзор).

Что умеет:
  • считает входы и ловит рейд по порогу; порог сам подстраивается под размер чата и время суток (по истории входов);
  • оценивает подозрительность вошедших (нет @username, новый аккаунт, бот, похожие имена) и срабатывает раньше, если подозрительных много;
  • ловит спам-атаку новичков (одинаковые сообщения от нескольких новичков за минуту);
  • при рейде включает «щит» (в чате только текст, таймер снимет сам) и просит новичков нажать кнопку (иначе кик);
  • пишет главным админам в ЛС и в чат стаффа (скрытый созыв), кнопки: закрыть чат, сменить ссылку, забанить вошедших, разбанить;
  • после рейда присылает итоговый отчёт;
  • проверяет права бота в чатах и предупреждает, чего не хватает.
Настройки и проверка: /antiraid (главные админы, ЛС с ботом или чат стаффа)."""
import asyncio
import html
import json
import logging
import math
import re
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from aiogram import BaseMiddleware, F, Router
from aiogram.filters import Command
from aiogram.types import (CallbackQuery, ChatJoinRequest, ChatMemberUpdated, ChatPermissions, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)

log = logging.getLogger("antiraid")
router = Router()
MSK = ZoneInfo("Europe/Moscow")

_bot = _get_db = _is_main_admin = _pings = None
_staff_chat = 0
_owner = 0

DEFAULTS = {"enabled": "1", "joins": "8", "minutes": "3", "scale": "4", "cooldown": "10", "adaptive": "1",
            "shield": "1", "shield_min": "10", "captcha": "1", "maxid": "0"}

FRESH_GAP = 150_000_000          # аккаунт «новый», если его ID не дальше этого от самого свежего ID, который видел бот (~2 месяца)
CAPTCHA_SECONDS = 60
NEWCOMER_SECONDS = 1800          # столько минут после входа человек считается новичком для антиспама
REQUIRED_RIGHTS = {"can_restrict_members": "ограничивать и банить", "can_invite_users": "приглашать (смена ссылки)",
                   "can_delete_messages": "удалять сообщения"}
FULL_PERMS = {"can_send_messages": True, "can_send_audios": True, "can_send_documents": True, "can_send_photos": True,
              "can_send_videos": True, "can_send_video_notes": True, "can_send_voice_notes": True, "can_send_polls": True,
              "can_send_other_messages": True, "can_add_web_page_previews": True, "can_invite_users": True}

_DDL = """
CREATE TABLE IF NOT EXISTS ar_cfg(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS ar_chats(chat_id BIGINT PRIMARY KEY, title TEXT, last_join TIMESTAMPTZ, joins_total INT DEFAULT 0, alerts INT DEFAULT 0);
CREATE TABLE IF NOT EXISTS ar_alerts(id SERIAL PRIMARY KEY, chat_id BIGINT, title TEXT, ts TIMESTAMPTZ DEFAULT now(),
                                     joins INT, users TEXT, perms TEXT, closed BOOLEAN DEFAULT FALSE);
ALTER TABLE ar_alerts ADD COLUMN IF NOT EXISTS shield_until TIMESTAMPTZ;
ALTER TABLE ar_alerts ADD COLUMN IF NOT EXISTS banned TEXT;
ALTER TABLE ar_alerts ADD COLUMN IF NOT EXISTS kind TEXT DEFAULT 'join';
CREATE TABLE IF NOT EXISTS ar_hourly(chat_id BIGINT, day DATE, hour INT, joins INT DEFAULT 0, PRIMARY KEY(chat_id, day, hour));
"""

_events = {}       # chat_id -> deque[(ts, uid)]
_info = {}         # chat_id -> {uid: dict(name, username, is_bot, kind, uid)}
_seen = {}         # (chat_id, uid) -> ts  (защита от двойного учёта)
_joined_at = {}    # (chat_id, uid) -> ts  (для антиспама новичков)
_msgs = {}         # chat_id -> deque[(ts, uid, fp, mid)]
_last_alert = {}   # chat_id -> ts
_raid_until = {}   # chat_id -> ts
_active = {}       # chat_id -> alert_id (идущий рейд)
_stats = {}        # alert_id -> dict
_pending = {}      # (chat_id, uid) -> message_id капчи
_members = {}      # chat_id -> (count, ts)
_hour_f = {}       # chat_id -> (factor, ts)
_maxid = 0


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
    global _maxid
    await q(_DDL)
    try:
        _maxid = int(await cfg("maxid"))
    except Exception:
        _maxid = 0


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


async def _notify(txt, kb=None, ping=False, dm=True):
    """Чат стаффа (+скрытый созыв) и ЛС всем главным админам."""
    try:
        extra = (await _pings() if (ping and _pings) else "")
        await _bot.send_message(_staff_chat, txt + extra, parse_mode="HTML", reply_markup=kb)
    except Exception as e:
        log.warning("антирейд: чат стаффа: %s", e)
    if dm:
        for uid in await _recipients():
            try:
                await _bot.send_message(uid, txt, parse_mode="HTML", reply_markup=kb)
            except Exception as e:
                log.info("антирейд: ЛС %s не доставлено: %s", uid, e)


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


async def hour_factor(cid) -> float:
    """Во сколько раз этот час в чате обычно оживлённее/тише среднего (по истории входов за 21 день, без сегодняшнего дня)."""
    if (await cfg("adaptive")) != "1":
        return 1.0
    c = _hour_f.get(cid)
    now = time.time()
    h = datetime.now(MSK).hour
    if c and now - c[1] < 600 and c[2] == h:
        return c[0]
    f = 1.0
    try:
        today = datetime.now(MSK).date()
        days = await q("SELECT COUNT(DISTINCT day) FROM ar_hourly WHERE chat_id=%s AND day<%s AND day>=%s", (cid, today, today - timedelta(days=21)), one=True)
        nd = int(days[0]) if days else 0
        if nd >= 3:
            tot = await q("SELECT COALESCE(SUM(joins),0) FROM ar_hourly WHERE chat_id=%s AND day<%s AND day>=%s", (cid, today, today - timedelta(days=21)), one=True)
            cur = await q("SELECT COALESCE(SUM(joins),0) FROM ar_hourly WHERE chat_id=%s AND hour=%s AND day<%s AND day>=%s", (cid, h, today, today - timedelta(days=21)), one=True)
            overall = float(tot[0]) / (nd * 24)
            if overall > 0:
                f = min(2.0, max(0.5, (float(cur[0]) / nd) / overall))
    except Exception as e:
        log.info("hour_factor: %s", e)
    _hour_f[cid] = (f, now, h)
    return f


async def _threshold(cid) -> int:
    base = int(await cfg("joins"))
    scale = int(await cfg("scale"))
    if scale > 0:
        base = max(base, math.ceil(await _member_count(cid) * scale / 100))
    return max(4, math.ceil(base * await hour_factor(cid)))


# ---------------------------------------------------------------- подозрительность
def _stem(s):
    return re.sub(r"[\d_\W]+", "", (s or "").lower())[:12]


def _score_all(cid, uids) -> dict:
    """uid -> (баллы, [метки]). 🆕 свежий аккаунт (+2), 🤖 бот (+2), без @username (+1), 👥 похожие имена (+1)."""
    info = _info.get(cid, {})
    stems = {}
    for u in uids:
        d = info.get(u)
        if d:
            st = _stem(d["username"] or d["name"])
            if len(st) >= 3:
                stems.setdefault(st, []).append(u)
    res = {}
    for u in uids:
        d = info.get(u) or {"name": str(u), "username": None, "is_bot": False}
        s, tags = 0, []
        if _maxid and u >= _maxid - FRESH_GAP:
            s += 2
            tags.append("🆕")
        if d["is_bot"]:
            s += 2
            tags.append("🤖")
        if not d["username"]:
            s += 1
        st = _stem(d["username"] or d["name"])
        if len(st) >= 3 and len(stems.get(st, [])) >= 3:
            s += 1
            tags.append("👥")
        res[u] = (s, tags)
    return res


# ---------------------------------------------------------------- учёт входов
async def record_join(chat, user, kind="join", adder=None):
    global _maxid
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
    _joined_at[key] = now
    if len(_seen) > 5000:
        for k in [k for k, t in _seen.items() if now - t > 300]:
            _seen.pop(k, None)
    if not user.is_bot and user.id > _maxid:
        _maxid = user.id
        try:
            await set_cfg("maxid", _maxid)
        except Exception:
            pass
    window = int(await cfg("minutes")) * 60
    dq = _events.setdefault(cid, deque())
    dq.append((now, user.id))
    while dq and now - dq[0][0] > max(window, 900):
        dq.popleft()
    info = _info.setdefault(cid, {})
    info[user.id] = {"name": user.full_name, "username": user.username, "is_bot": bool(user.is_bot), "kind": kind}
    if len(info) > 300:
        for k in list(info)[:100]:
            info.pop(k, None)
    try:
        await q("""INSERT INTO ar_chats(chat_id,title,last_join,joins_total) VALUES(%s,%s,now(),1)
                   ON CONFLICT(chat_id) DO UPDATE SET title=EXCLUDED.title, last_join=now(), joins_total=ar_chats.joins_total+1""",
                (cid, chat.title or str(cid)))
        await q("""INSERT INTO ar_hourly(chat_id,day,hour,joins) VALUES(%s,%s,%s,1)
                   ON CONFLICT(chat_id,day,hour) DO UPDATE SET joins=ar_hourly.joins+1""",
                (cid, datetime.now(MSK).date(), datetime.now(MSK).hour))
    except Exception as e:
        log.warning("ar_chats/hourly: %s", e)
    # идёт рейд — новичка сразу проверяем, считаем в отчёт
    if now < _raid_until.get(cid, 0) and cid in _active:
        _raid_until[cid] = max(_raid_until[cid], now + 600)
        st = _stats.get(_active[cid])
        if st is not None:
            st["joins"] += 1
            st["last"] = now
        if cid != _staff_chat:
            asyncio.create_task(_challenge(cid, user.id, user.full_name))
        return
    in_win = list(dict.fromkeys(u for t, u in dq if now - t <= window))
    thr = await _threshold(cid)
    reason = None
    if len(in_win) >= thr:
        reason = "count"
    elif len(in_win) >= max(4, math.ceil(thr / 2)):
        sc = _score_all(cid, in_win)
        sus = [u for u in in_win if sc[u][0] >= 3]
        if len(sus) >= 4 and len(sus) / len(in_win) >= 0.6:
            reason = "susp"
    if not reason:
        return
    if now - _last_alert.get(cid, 0) < int(await cfg("cooldown")) * 60:
        _raid_until[cid] = now + 600
        return
    _last_alert[cid] = now
    _raid_until[cid] = now + 600
    asyncio.create_task(fire_alert(cid, chat.title or str(cid), in_win, window // 60, kind="join", reason=reason))


def _fp(m) -> str:
    t = (m.text or m.caption or "").lower()
    t = re.sub(r"\d+", "#", t)
    t = re.sub(r"[^\w#@/. ]+", "", t)
    t = " ".join(t.split())[:60]
    return t or f"<{getattr(m, 'content_type', 'msg')}>"


async def record_message(msg):
    """Спам-атака новичков: одинаковые сообщения от нескольких недавно вошедших."""
    chat, u = msg.chat, msg.from_user
    if not u or not _is_group(chat) or u.is_bot:
        return
    now = time.time()
    t0 = _joined_at.get((chat.id, u.id))
    if not t0 or now - t0 > NEWCOMER_SECONDS:
        return
    if (await cfg("enabled")) != "1":
        return
    cid = chat.id
    dq = _msgs.setdefault(cid, deque())
    dq.append((now, u.id, _fp(msg), msg.message_id))
    while dq and now - dq[0][0] > 60:
        dq.popleft()
    by_fp = {}
    for t, uid, fp, mid in dq:
        by_fp.setdefault(fp, set()).add(uid)
    spam_users, sample = set(), ""
    for fp, us in by_fp.items():
        if len(us) >= 3:
            spam_users |= us
            sample = sample or fp
    if not spam_users and len({uid for _, uid, _, _ in dq}) >= 4 and len(dq) >= 8:
        spam_users = {uid for _, uid, _, _ in dq}
        sample = "(разные сообщения от многих новичков сразу)"
    if not spam_users:
        return
    ids = [mid for t, uid, fp, mid in dq if uid in spam_users]
    if (await cfg("shield")) == "1" and cid != _staff_chat:
        for mid in ids:
            try:
                await _bot.delete_message(cid, mid)
            except Exception:
                pass
        if cid in _active and _stats.get(_active[cid]) is not None:
            _stats[_active[cid]]["spam"] += len(ids)
    if now - _last_alert.get(cid, 0) < int(await cfg("cooldown")) * 60:
        _raid_until[cid] = now + 600
        for uid in spam_users:
            if cid != _staff_chat and cid in _active:
                asyncio.create_task(_challenge(cid, uid, (_info.get(cid, {}).get(uid) or {}).get("name", str(uid))))
        return
    _last_alert[cid] = now
    _raid_until[cid] = now + 600
    asyncio.create_task(fire_alert(cid, chat.title or str(cid), list(spam_users), 1, kind="spam", sample=sample, spam_msgs=len(ids)))


# ---------------------------------------------------------------- тревога
async def _alert_kb(aid, locked=False):
    row = await q("SELECT banned FROM ar_alerts WHERE id=%s", (aid,), one=True)
    rows = [[InlineKeyboardButton(text="🔓 Открыть чат" if locked else "🔒 Закрыть чат", callback_data=f"ar:{'unlock' if locked else 'lock'}:{aid}"),
             InlineKeyboardButton(text="🔗 Сменить ссылку", callback_data=f"ar:link:{aid}")],
            [InlineKeyboardButton(text="🚫 Забанить вошедших", callback_data=f"ar:ban:{aid}"),
             InlineKeyboardButton(text="✅ Отбой", callback_data=f"ar:close:{aid}")]]
    if row and row[0] and row[0] != "{}":
        rows.append([InlineKeyboardButton(text="↩️ Разбанить невиновных", callback_data=f"ar:ub:{aid}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _people_lines(cid, uids, limit=15):
    info = _info.get(cid, {})
    sc = _score_all(cid, uids)
    sus = sum(1 for u in uids if sc[u][0] >= 3)
    bots = sum(1 for u in uids if (info.get(u) or {}).get("is_bot"))
    nouser = sum(1 for u in uids if not (info.get(u) or {}).get("username"))
    fresh = sum(1 for u in uids if "🆕" in sc[u][1])
    similar = sum(1 for u in uids if "👥" in sc[u][1])
    lines = []
    for u in uids[-limit:]:
        d = info.get(u) or {"name": str(u), "username": None, "kind": "join"}
        lines.append(f"• {_umention(u, d['name'])}" + (f" @{d['username']}" if d["username"] else "") + (" " + "".join(sc[u][1]) if sc[u][1] else "")
                     + (" (заявка)" if d.get("kind") == "req" else ""))
    return lines, {"sus": sus, "bots": bots, "nouser": nouser, "fresh": fresh, "similar": similar}


async def _no_photo(uids, limit=20):
    sem = asyncio.Semaphore(5)

    async def one(u):
        async with sem:
            try:
                r = await asyncio.wait_for(_bot.get_user_profile_photos(u, limit=1), 5)
                return r.total_count == 0
            except Exception:
                return False
    res = await asyncio.gather(*[one(u) for u in uids[:limit]])
    return sum(1 for x in res if x)


async def fire_alert(cid, title, uids, minutes, kind="join", reason="", sample="", spam_msgs=0, test=False):
    try:
        lines, c = await _people_lines(cid, uids)
        nophoto = await _no_photo(uids) if not test else 0
        thr = await _threshold(cid) if not test else int(await cfg("joins"))
        aid = 0
        if not test:
            r = await q("INSERT INTO ar_alerts(chat_id,title,joins,users,kind) VALUES(%s,%s,%s,%s,%s) RETURNING id", (cid, title, len(uids), json.dumps(uids), kind), one=True)
            aid = r[0]
            await q("UPDATE ar_chats SET alerts=alerts+1 WHERE chat_id=%s", (cid,))
        if test:
            head, why = "🧪 <b>ТЕСТ антирейда</b> (настоящей тревоги нет)", ""
        elif kind == "spam":
            head = "🚨🚨 <b>СПАМ-АТАКА НОВИЧКОВ</b> 🚨🚨"
            why = f"{len(uids)} новичков шлют одинаковые сообщения за минуту. Пример: «{html.escape(sample)[:120]}»"
            if spam_msgs:
                why += f"\nСообщений: {spam_msgs}" + (" (бот их удаляет)" if (await cfg('shield')) == "1" else "")
        else:
            head = "🚨🚨 <b>РЕЙД В ЧАТЕ</b> 🚨🚨"
            why = f"Вошло <b>{len(uids)}</b> за {minutes} мин. (порог {thr})" + (" — сработало раньше порога: много подозрительных аккаунтов." if reason == "susp" else ".")
        txt = (f"{head}\n<b>{html.escape(title)}</b> (<code>{cid}</code>)\n{why}\n"
               f"Подозрительных: <b>{c['sus']}</b> из {len(uids)} · без @username: {c['nouser']} · новых аккаунтов 🆕: {c['fresh']} · ботов 🤖: {c['bots']} · "
               f"похожие имена 👥: {c['similar']}" + (f" · без видимого фото: {nophoto}" if nophoto else "") + "\n\n"
               + ("Последние:\n" + "\n".join(lines) + "\n\n" if lines else ""))
        if test:
            txt += "Если вы видите это сообщение, оповещения работают."
            await _notify(txt, None, ping=False)
            return
        auto = []
        if cid != _staff_chat:
            if (await cfg("shield")) == "1":
                auto.append(f"🛡 щит на {await cfg('shield_min')} мин: в чате только текст")
            if (await cfg("captcha")) == "1":
                auto.append(f"🧩 новички нажимают кнопку за {CAPTCHA_SECONDS} с, иначе кик")
        txt += ("<b>Бот уже делает:</b> " + "; ".join(auto) + ".\n\n" if auto else "") + \
               "<b>Вам:</b> смените ссылки, включите одобрение заявок, при необходимости забаньте вошедших. Кнопки ниже."
        await _notify(txt, await _alert_kb(aid), ping=True)
        old = _active.get(cid)
        st = _stats.pop(old, None) if old else None
        if old:
            await q("UPDATE ar_alerts SET closed=TRUE WHERE id=%s", (old,))
        _stats[aid] = st or {"joins": 0, "first": time.time(), "last": time.time(), "banned": 0, "pass": 0, "kick": 0, "spam": 0, "kind": kind, "title": title, "cid": cid}
        _stats[aid]["joins"] = max(_stats[aid]["joins"], len(uids))
        _stats[aid]["spam"] += spam_msgs
        _active[cid] = aid
        if cid != _staff_chat:
            await _engage(cid, aid, uids, old)
    except Exception:
        log.exception("fire_alert")


async def _engage(cid, aid, uids, old=None):
    """Щит и проверка новичков."""
    if (await cfg("shield")) == "1":
        try:
            minutes = int(await cfg("shield_min"))
            prev = await q("SELECT id, perms FROM ar_alerts WHERE chat_id=%s AND shield_until IS NOT NULL AND shield_until > now() ORDER BY id DESC LIMIT 1", (cid,), one=True)
            until = datetime.now(timezone.utc) + timedelta(minutes=minutes)
            if prev:                                 # щит уже стоит — продлеваем, исходные права берём из него
                await q("UPDATE ar_alerts SET perms=%s WHERE id=%s", (prev[1], aid))
                await q("UPDATE ar_alerts SET shield_until=NULL WHERE id=%s", (prev[0],))
            else:
                chat = await _bot.get_chat(cid)
                saved = chat.permissions.model_dump(exclude_none=True) if chat.permissions else {}
                await q("UPDATE ar_alerts SET perms=%s WHERE id=%s", (json.dumps(saved), aid))
                await _bot.set_chat_permissions(cid, ChatPermissions(can_send_messages=True))
            await q("UPDATE ar_alerts SET shield_until=%s WHERE id=%s", (until, aid))
        except Exception as e:
            await _notify(f"⚠️ <b>Щит не включился</b> в чате {cid}: {html.escape(str(e))[:150]}\nНужно право бота «Блокировать участников».", dm=False)
    if (await cfg("captcha")) == "1":
        info = _info.get(cid, {})
        for u in uids:
            asyncio.create_task(_challenge(cid, u, (info.get(u) or {}).get("name", str(u))))
            await asyncio.sleep(0.15)


async def _orig_perms(cid) -> dict:
    r = await q("SELECT perms FROM ar_alerts WHERE chat_id=%s AND perms IS NOT NULL ORDER BY id DESC LIMIT 1", (cid,), one=True)
    try:
        return json.loads(r[0]) if r and r[0] else dict(FULL_PERMS)
    except Exception:
        return dict(FULL_PERMS)


async def _challenge(cid, uid, name):
    if (cid, uid) in _pending or await _is_staff(uid):
        return
    try:
        until = datetime.now(timezone.utc) + timedelta(minutes=int(await cfg("shield_min")) + 5)
        await _bot.restrict_chat_member(cid, uid, permissions=ChatPermissions(can_send_messages=False), until_date=until)
    except Exception as e:
        log.info("капча: не удалось ограничить %s в %s: %s", uid, cid, e)
        return
    try:
        m = await _bot.send_message(cid, f"🛡 {_umention(uid, name)}, в чате проверка. Нажмите кнопку в течение {CAPTCHA_SECONDS} с, иначе вы будете удалены.",
                                    parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                                        InlineKeyboardButton(text="✅ Я человек", callback_data=f"ar:cap:{cid}:{uid}")]]))
        _pending[(cid, uid)] = m.message_id
    except Exception as e:
        log.info("капча: сообщение не отправлено: %s", e)
        return
    await asyncio.sleep(CAPTCHA_SECONDS)
    mid = _pending.pop((cid, uid), None)
    if mid is None:
        return
    try:
        await _bot.ban_chat_member(cid, uid, revoke_messages=True)
        await _bot.unban_chat_member(cid, uid, only_if_banned=True)
        st = _stats.get(_active.get(cid))
        if st is not None:
            st["kick"] += 1
    except Exception as e:
        log.info("капча: кик %s: %s", uid, e)
    try:
        await _bot.delete_message(cid, mid)
    except Exception:
        pass


@router.callback_query(F.data.regexp(r"^ar:cap:-?\d+:\d+$"))
async def cb_captcha(cb: CallbackQuery):
    _, _, cid, uid = cb.data.split(":")
    cid, uid = int(cid), int(uid)
    if cb.from_user.id != uid:
        return await cb.answer("Эта кнопка не для вас", show_alert=True)
    mid = _pending.pop((cid, uid), None)
    try:
        await _bot.restrict_chat_member(cid, uid, permissions=ChatPermissions(**(await _orig_perms(cid))))
    except Exception as e:
        log.info("капча: снять ограничение %s: %s", uid, e)
    st = _stats.get(_active.get(cid))
    if st is not None and mid is not None:
        st["pass"] += 1
    try:
        await cb.message.delete()
    except Exception:
        pass
    await cb.answer("Спасибо, проверка пройдена ✅")


# ---------------------------------------------------------------- действия админов
async def _alert(aid):
    return await q("SELECT chat_id, title, users, perms, closed, banned FROM ar_alerts WHERE id=%s", (aid,), one=True)


async def _note(cb: CallbackQuery, line: str, kb=None):
    try:
        await cb.message.edit_text(cb.message.html_text + f"\n\n{line}", parse_mode="HTML", reply_markup=kb or cb.message.reply_markup)
    except Exception as e:
        if "not modified" not in str(e).lower():
            log.info("note: %s", e)


@router.callback_query(F.data.regexp(r"^ar:(lock|unlock|link|ban|banyes|back|close|ub|uba):\d+$"))
async def cb_alert(cb: CallbackQuery):
    if not await _is_staff(cb.from_user.id):
        return await cb.answer("Только главные админы", show_alert=True)
    _, act, aid = cb.data.split(":")
    aid = int(aid)
    row = await _alert(aid)
    if not row:
        return await cb.answer("Тревога не найдена", show_alert=True)
    cid, title, users, perms, closed, banned = row
    who = html.escape(cb.from_user.full_name)
    if act == "lock":
        try:
            if not perms:
                chat = await _bot.get_chat(cid)
                saved = chat.permissions.model_dump(exclude_none=True) if chat.permissions else {}
                await q("UPDATE ar_alerts SET perms=%s WHERE id=%s", (json.dumps(saved), aid))
            await q("UPDATE ar_alerts SET shield_until=NULL WHERE chat_id=%s", (cid,))     # ручное закрытие: таймер не должен его снять
            await _bot.set_chat_permissions(cid, ChatPermissions(can_send_messages=False))
            await _note(cb, f"🔒 Чат закрыт ({who}).", await _alert_kb(aid, locked=True))
        except Exception as e:
            await _note(cb, f"❌ Не удалось закрыть чат: {html.escape(str(e))[:150]}")
    elif act == "unlock":
        try:
            p = await _orig_perms(cid) if not perms else json.loads(perms)
            await _bot.set_chat_permissions(cid, ChatPermissions(**p))
            await _note(cb, f"🔓 Чат открыт, права возвращены ({who}).", await _alert_kb(aid, locked=False))
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
            await cb.message.edit_reply_markup(reply_markup=await _alert_kb(aid))
        except Exception:
            pass
    elif act == "banyes":
        ok, fail = 0, 0
        done = json.loads(banned) if banned else {}
        info = _info.get(cid, {})
        for u in json.loads(users or "[]"):
            if await _is_staff(u):
                continue
            try:
                await _bot.ban_chat_member(cid, u, revoke_messages=True)
                done[str(u)] = (info.get(u) or {}).get("name", str(u))
                ok += 1
            except Exception:
                fail += 1
            await asyncio.sleep(0.2)
        await q("UPDATE ar_alerts SET banned=%s WHERE id=%s", (json.dumps(done, ensure_ascii=False), aid))
        st = _stats.get(aid)
        if st is not None:
            st["banned"] += ok
        await _note(cb, f"🚫 Забанено: {ok}" + (f", не удалось: {fail}" if fail else "") + f" ({who}).", await _alert_kb(aid))
    elif act == "ub":
        done = json.loads(banned) if banned else {}
        rows = [[InlineKeyboardButton(text=f"↩️ {n[:28]}", callback_data=f"ar:ubu:{aid}:{u}")] for u, n in list(done.items())[:20]]
        rows.append([InlineKeyboardButton(text="↩️ Разбанить всех", callback_data=f"ar:uba:{aid}")])
        rows.append([InlineKeyboardButton(text="🔙 Назад", callback_data=f"ar:back:{aid}")])
        try:
            await cb.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        except Exception:
            pass
    elif act == "uba":
        done = json.loads(banned) if banned else {}
        n = 0
        for u in list(done):
            try:
                await _bot.unban_chat_member(cid, int(u), only_if_banned=True)
                n += 1
            except Exception:
                pass
        await q("UPDATE ar_alerts SET banned=%s WHERE id=%s", ("{}", aid))
        await _note(cb, f"↩️ Разбанено: {n} ({who}). Им нужно снова зайти по ссылке.", await _alert_kb(aid))
    elif act == "close":
        await q("UPDATE ar_alerts SET closed=TRUE WHERE id=%s", (aid,))
        try:
            await cb.message.edit_text(cb.message.html_text + f"\n\n✅ Отбой ({who}).", parse_mode="HTML")
        except Exception:
            pass
        await _finish(cid, aid, by=cb.from_user.full_name)
    await cb.answer()


@router.callback_query(F.data.regexp(r"^ar:ubu:\d+:\d+$"))
async def cb_unban_one(cb: CallbackQuery):
    if not await _is_staff(cb.from_user.id):
        return await cb.answer("Только главные админы", show_alert=True)
    _, _, aid, uid = cb.data.split(":")
    row = await _alert(int(aid))
    if not row:
        return await cb.answer("Тревога не найдена", show_alert=True)
    cid, banned = row[0], json.loads(row[5]) if row[5] else {}
    try:
        await _bot.unban_chat_member(cid, int(uid), only_if_banned=True)
        banned.pop(uid, None)
        await q("UPDATE ar_alerts SET banned=%s WHERE id=%s", (json.dumps(banned, ensure_ascii=False), int(aid)))
        rows = [[InlineKeyboardButton(text=f"↩️ {n[:28]}", callback_data=f"ar:ubu:{aid}:{u}")] for u, n in list(banned.items())[:20]]
        rows.append([InlineKeyboardButton(text="🔙 Назад", callback_data=f"ar:back:{aid}")])
        await cb.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        await cb.answer("Разбанен. Пусть заходит по ссылке снова.", show_alert=True)
    except Exception as e:
        await cb.answer(f"Не вышло: {str(e)[:150]}", show_alert=True)


# ---------------------------------------------------------------- итог рейда
async def _finish(cid, aid, by=None):
    st = _stats.pop(aid, None)
    if _active.get(cid) == aid:
        _active.pop(cid, None)
    _raid_until[cid] = 0
    _last_alert[cid] = 0
    if not st:
        return
    mins = max(1, round((st["last"] - st["first"]) / 60))
    shield = await q("SELECT shield_until FROM ar_alerts WHERE chat_id=%s AND shield_until IS NOT NULL ORDER BY id DESC LIMIT 1", (cid,), one=True)
    txt = (f"📋 <b>Итог рейда</b> · {html.escape(st.get('title') or str(cid))}\n"
           f"Тип: {'спам-атака новичков' if st.get('kind') == 'spam' else 'массовый вход'} · длился ~{mins} мин.\n"
           f"Вошло: <b>{st['joins']}</b>\n"
           f"Прошли проверку кнопкой: {st['pass']} · удалены за непрохождение: {st['kick']}\n"
           f"Забанено админами: {st['banned']} · удалено спам-сообщений: {st['spam']}\n"
           + ("🛡 Щит ещё действует, снимется сам по таймеру.\n" if shield else "")
           + (f"Закрыл: {html.escape(by)}" if by else "Закрыт автоматически: новых входов не было 10 минут."))
    await _notify(txt, None, ping=False)


# ---------------------------------------------------------------- права бота
async def check_rights(cid):
    """(ok, [чего не хватает])"""
    try:
        m = await _bot.get_chat_member(cid, _bot.id)
    except Exception as e:
        return False, [f"бот не видит чат ({str(e)[:60]})"]
    if m.status in ("left", "kicked"):
        return False, ["бота нет в чате"]
    if m.status != "administrator":
        return False, ["бот не админ"]
    missing = [t for k, t in REQUIRED_RIGHTS.items() if not getattr(m, k, False)]
    return (not missing), missing


async def rights_report(only_problems=False):
    rows = await q("SELECT chat_id, title FROM ar_chats ORDER BY last_join DESC NULLS LAST", many=True) or []
    out, bad = [], 0
    for cid, title in rows:
        if cid == _staff_chat:
            continue
        ok, missing = await check_rights(cid)
        if ok:
            if not only_problems:
                out.append(f"✅ {html.escape(title or str(cid))}")
        else:
            bad += 1
            out.append(f"⚠️ {html.escape(title or str(cid))}: нужно — {', '.join(missing)}")
        await asyncio.sleep(0.2)
    return out, bad


@router.my_chat_member()
async def on_my_member(upd: ChatMemberUpdated):
    try:
        if not _is_group(upd.chat):
            return
        old, new = upd.old_chat_member.status, upd.new_chat_member.status
        if old == new and old != "administrator":
            return
        if new in ("left", "kicked"):
            await _notify(f"ℹ️ Бот убран из чата «{html.escape(upd.chat.title or str(upd.chat.id))}» — антирейд там больше не работает.", dm=False)
            return
        await q("INSERT INTO ar_chats(chat_id,title) VALUES(%s,%s) ON CONFLICT(chat_id) DO UPDATE SET title=EXCLUDED.title", (upd.chat.id, upd.chat.title or str(upd.chat.id)))
        ok, missing = await check_rights(upd.chat.id)
        t = html.escape(upd.chat.title or str(upd.chat.id))
        if ok:
            await _notify(f"✅ Бот в чате «{t}» с нужными правами: антирейд активен.", dm=False)
        else:
            await _notify(f"⚠️ Бот в чате «{t}», но антирейд работает не полностью: нужно — {', '.join(missing)}.\nВыдайте боту права администратора.", dm=False)
    except Exception:
        log.exception("antiraid my_chat_member")


# ---------------------------------------------------------------- источники событий
class _JoinMiddleware(BaseMiddleware):
    """Только наблюдает за входами и сообщениями новичков, дальше событие идёт как обычно."""
    async def __call__(self, handler, event, data):
        try:
            if getattr(event, "new_chat_members", None):
                for u in event.new_chat_members:
                    await record_join(event.chat, u, "join", event.from_user)
            elif getattr(event, "from_user", None) and _joined_at:
                await record_message(event)
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


# ---------------------------------------------------------------- воркер
async def worker():
    log.info("Воркер антирейда запущен")
    await init_tables()
    while True:
        try:
            # 1. снять щит по таймеру
            rows = await q("SELECT id, chat_id, perms FROM ar_alerts WHERE shield_until IS NOT NULL AND shield_until <= now()", many=True) or []
            for aid, cid, perms in rows:
                await q("UPDATE ar_alerts SET shield_until=NULL WHERE id=%s", (aid,))
                try:
                    p = json.loads(perms) if perms else dict(FULL_PERMS)
                    await _bot.set_chat_permissions(cid, ChatPermissions(**p))
                    await _notify(f"🛡 Щит снят в чате {cid}: права возвращены.", dm=False)
                except Exception as e:
                    await _notify(f"⚠️ Не удалось снять щит в чате {cid}: {html.escape(str(e))[:150]}. Верните права вручную.", dm=True)
            # 2. автозакрытие рейда
            now = time.time()
            for cid, aid in list(_active.items()):
                if now > _raid_until.get(cid, 0):
                    await q("UPDATE ar_alerts SET closed=TRUE WHERE id=%s", (aid,))
                    await _finish(cid, aid)
            # 3. чистка памяти
            for k in [k for k, t in _joined_at.items() if now - t > 7200]:
                _joined_at.pop(k, None)
            # 4. раз в сутки проверка прав
            n = datetime.now(MSK)
            if n.hour == 10 and await _flag(f"rights:{n.date()}"):
                out, bad = await rights_report(only_problems=True)
                if bad:
                    await _notify("🩺 <b>Антирейд: проблемы с правами бота</b>\n" + "\n".join(out), dm=True)
        except Exception:
            log.exception("antiraid worker")
        await asyncio.sleep(30)


async def _flag(key) -> bool:
    r = await q("SELECT 1 FROM ar_cfg WHERE key=%s", (key,), one=True)
    if r:
        return False
    await set_cfg(key, "1")
    return True


# ---------------------------------------------------------------- настройки /antiraid
def _on(v):
    return "🟢" if str(v) == "1" else "🔴"


async def _panel():
    en, joins, mins, scale, shield, smin, cap, adapt = [await cfg(k) for k in ("enabled", "joins", "minutes", "scale", "shield", "shield_min", "captcha", "adaptive")]
    chats = await q("SELECT title, last_join, joins_total, alerts FROM ar_chats ORDER BY last_join DESC NULLS LAST LIMIT 8", many=True) or []
    alerts = await q("SELECT title, ts, joins, closed, kind FROM ar_alerts ORDER BY id DESC LIMIT 5", many=True) or []
    lines = ["🛡 <b>Антирейд</b>", f"Состояние: {'🟢 включён' if en == '1' else '🔴 выключен'} (работает во всех чатах бота, +надзор не нужен)",
             f"Базовый порог: <b>{joins}</b> входов за <b>{mins}</b> мин." + (f", в больших чатах не меньше {scale}% участников" if int(scale) else ""),
             f"Подстройка под время суток: {_on(adapt)} · Щит ({smin} мин, только текст): {_on(shield)} · Проверка новичков кнопкой: {_on(cap)}",
             "Ловит и спам-атаки новичков (одинаковые сообщения). Оповещения: ЛС главным админам + чат стаффа со скрытым созывом."]
    if chats:
        lines.append("\n<b>Чаты под наблюдением</b> (где были входы):")
        for t, lj, tot, al in chats:
            lines.append(f"• {html.escape(t or '?')} — входов {tot}, тревог {al}")
    if alerts:
        lines.append("\n<b>Последние тревоги:</b>")
        for t, ts, j, cl, kd in alerts:
            lines.append(f"• {ts.astimezone(MSK).strftime('%d.%m %H:%M')} {html.escape(t or '?')} — {'спам' if kd == 'spam' else 'вход'} {j} {'✅' if cl else '⏳'}")

    def row(key, vals, cur, fmt):
        return [InlineKeyboardButton(text=("• " if str(cur) == str(v) else "") + fmt.format(v), callback_data=f"arc:{key}:{v}") for v in vals]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔴 Выключить" if en == "1" else "🟢 Включить", callback_data=f"arc:enabled:{0 if en == '1' else 1}")],
        row("joins", (5, 8, 12, 20, 30), joins, "{}"),
        row("minutes", (1, 2, 3, 5, 10), mins, "{} мин"),
        row("scale", (0, 3, 4, 6, 10), scale, "{}%"),
        [InlineKeyboardButton(text=f"{_on(shield)} Щит", callback_data=f"arc:shield:{0 if shield == '1' else 1}"),
         InlineKeyboardButton(text=f"{_on(cap)} Проверка кнопкой", callback_data=f"arc:captcha:{0 if cap == '1' else 1}"),
         InlineKeyboardButton(text=f"{_on(adapt)} По часам", callback_data=f"arc:adaptive:{0 if adapt == '1' else 1}")],
        row("shield_min", (5, 10, 15), smin, "щит {} мин"),
        [InlineKeyboardButton(text="🧪 Проверка оповещения", callback_data="arc:test:0"),
         InlineKeyboardButton(text="🩺 Права бота", callback_data="arc:rights:0")]])
    return "\n".join(lines) + "\n\n<i>Ряды: порог входов · окно · доля от размера чата (0 = выкл) · переключатели · длительность щита.</i>", kb


@router.message(Command("antiraid"))
async def cmd_antiraid(message: Message):
    if not await _is_staff(message.from_user.id):
        return
    if message.chat.type != "private" and message.chat.id != _staff_chat:
        return
    text, kb = await _panel()
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.regexp(r"^arc:(enabled|joins|minutes|scale|shield|shield_min|captcha|adaptive|test|rights):\d+$"))
async def cb_cfg(cb: CallbackQuery):
    if not await _is_staff(cb.from_user.id):
        return await cb.answer("Только главные админы", show_alert=True)
    _, key, val = cb.data.split(":")
    if key == "test":
        asyncio.create_task(fire_alert(cb.message.chat.id, "Тестовый чат", [900000001, 900000002, 900000003], int(await cfg("minutes")), test=True))
        return await cb.answer("Тестовое оповещение отправлено в ЛС и в чат стаффа", show_alert=True)
    if key == "rights":
        await cb.answer("Проверяю…")
        out, bad = await rights_report()
        txt = "🩺 <b>Права бота по чатам</b>\n" + ("\n".join(out) if out else "Чатов пока нет: бот запоминает чат после первого входа в нём или добавления бота.")
        if bad:
            txt += f"\n\nПроблемных чатов: {bad}. Выдайте боту права: ограничивать и банить, приглашать, удалять сообщения."
        return await cb.message.answer(txt, parse_mode="HTML")
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
