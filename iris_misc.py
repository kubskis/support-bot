# -*- coding: utf-8 -*-
"""Надзор, этап C (для одного чата): СКАМ-список с автобаном, ачивки, статистика по часам,
топ упоминаний, управление рангами (снять вышедших/всех, кто назначил), ЛДК-сервис."""
import logging
import re
from datetime import timedelta, timezone

import iris_mod as m
from iris_mod import cmd, q, reply, mention, esc, resolve_target, member_name, get_cfg, set_cfg

log = logging.getLogger("iris_misc")
_MSK = timezone(timedelta(hours=3))

m.DEFAULT_DK.update({"скам": 4, "ачивки": 0, "стачас": 0, "топупом": 0, "кторанг": 0, "снятьвсех": 5})

_DDL = """
CREATE TABLE IF NOT EXISTS im_scam(chat_id BIGINT, user_id BIGINT, reason TEXT, by_id BIGINT, ts TIMESTAMPTZ DEFAULT now(), PRIMARY KEY(chat_id,user_id));
CREATE TABLE IF NOT EXISTS im_hours(chat_id BIGINT, hour INT, msgs BIGINT DEFAULT 0, PRIMARY KEY(chat_id,hour));
CREATE TABLE IF NOT EXISTS im_mentions(chat_id BIGINT, user_id BIGINT, cnt BIGINT DEFAULT 0, PRIMARY KEY(chat_id,user_id));
"""


async def init_tables():
    await q(_DDL)


# ---------------------------------------------------------------- СКАМ-список
_scam_cache = {}   # chat_id -> set(uid), перечитываем при изменениях


async def _scam_set(cid):
    s = _scam_cache.get(cid)
    if s is None:
        rows = await q("SELECT user_id FROM im_scam WHERE chat_id=%s", (cid,), many=True) or []
        s = _scam_cache[cid] = {r[0] for r in rows}
    return s


async def _scam_join(cid, user, cfg, msg):
    if user.id in await _scam_set(cid):
        try:
            await m.do_ban(cid, user.id, 0, "СКАМ-список", m._bot.id)
            await m._bot.send_message(cid, f"🚫 {mention(user.id, user.full_name)} из СКАМ-списка — заблокирован при входе.", parse_mode="HTML")
        except Exception as e:
            log.warning("scam join: %s", e)
        return True
    return False


async def _scam_msg(msg, cfg, rank):
    if rank == 0 and msg.from_user and msg.from_user.id in await _scam_set(msg.chat.id):
        try:
            await m.do_ban(msg.chat.id, msg.from_user.id, 0, "СКАМ-список", m._bot.id)
            await msg.delete()
        except Exception:
            pass
        return True
    return False


m.JOIN_HOOKS.append(_scam_join)
m.AUTOMOD.append(_scam_msg)


@cmd(["+скам"], "скам")
async def c_scam_add(msg, args, rank):
    uid, name, rest = await resolve_target(msg, args)
    if not uid:
        return await reply(msg, "Пример: <code>+скам @ник причина</code> (или ответом на сообщение).")
    if await m.get_rank(msg.chat.id, uid) > 0:
        return await reply(msg, "Модераторов в СКАМ-список добавлять нельзя.")
    reason = " ".join(rest)[:200] or "—"
    await q("""INSERT INTO im_scam(chat_id,user_id,reason,by_id) VALUES(%s,%s,%s,%s)
               ON CONFLICT(chat_id,user_id) DO UPDATE SET reason=EXCLUDED.reason, by_id=EXCLUDED.by_id""",
            (msg.chat.id, uid, reason, msg.from_user.id))
    (await _scam_set(msg.chat.id)).add(uid)
    try:
        await m.do_ban(msg.chat.id, uid, 0, "СКАМ-список", msg.from_user.id)
    except Exception:
        pass
    await m.logact(msg.chat.id, msg.from_user.id, "скам+", uid, reason)
    await reply(msg, f"🚫 {mention(uid, name)} добавлен в СКАМ-список и заблокирован. Причина: {esc(reason)}")


@cmd(["-скам"], "скам")
async def c_scam_del(msg, args, rank):
    uid, name, rest = await resolve_target(msg, args)
    if not uid:
        return await reply(msg, "Пример: <code>-скам @ник</code>")
    await q("DELETE FROM im_scam WHERE chat_id=%s AND user_id=%s", (msg.chat.id, uid))
    (await _scam_set(msg.chat.id)).discard(uid)
    try:
        await m.do_unban(msg.chat.id, uid, msg.from_user.id)
    except Exception:
        pass
    await reply(msg, f"{mention(uid, name)} убран из СКАМ-списка и разбанен.")


@cmd(["скам", "скам список", "скамлист"], "скам")
async def c_scam_list(msg, args, rank):
    rows = await q("SELECT user_id,reason FROM im_scam WHERE chat_id=%s ORDER BY ts DESC LIMIT 50", (msg.chat.id,), many=True)
    if not rows:
        return await reply(msg, "СКАМ-список пуст.")
    out = ["<b>СКАМ-список</b>"]
    for u, r in rows:
        out.append(f"{mention(u, await member_name(msg.chat.id, u))} — {esc(r or '—')}")
    await reply(msg, "\n".join(out))


# ---------------------------------------------------------------- статистика по часам и упоминания
_MENT = re.compile(r"@([A-Za-z0-9_]{4,32})")


async def _stat_hook(msg, cfg, rank):
    try:
        cid = msg.chat.id
        h = msg.date.astimezone(_MSK).hour
        await q("INSERT INTO im_hours(chat_id,hour,msgs) VALUES(%s,%s,1) ON CONFLICT(chat_id,hour) DO UPDATE SET msgs=im_hours.msgs+1", (cid, h))
        txt = msg.text or msg.caption or ""
        names = {n.lower() for n in _MENT.findall(txt)}
        for n in list(names)[:5]:
            r = await q("SELECT user_id FROM im_members WHERE chat_id=%s AND lower(username)=%s", (cid, n), one=True)
            if r:
                await q("INSERT INTO im_mentions(chat_id,user_id,cnt) VALUES(%s,%s,1) ON CONFLICT(chat_id,user_id) DO UPDATE SET cnt=im_mentions.cnt+1", (cid, r[0]))
        for ent in (msg.entities or []):
            if ent.type == "text_mention" and ent.user:
                await q("INSERT INTO im_mentions(chat_id,user_id,cnt) VALUES(%s,%s,1) ON CONFLICT(chat_id,user_id) DO UPDATE SET cnt=im_mentions.cnt+1", (cid, ent.user.id))
    except Exception:
        log.debug("stat hook", exc_info=True)
    return False


m.AUTOMOD.append(_stat_hook)


@cmd(["стата по часам", "активность по часам", "часы"], "стачас")
async def c_hours(msg, args, rank):
    rows = dict(await q("SELECT hour,msgs FROM im_hours WHERE chat_id=%s", (msg.chat.id,), many=True) or [])
    if not rows:
        return await reply(msg, "Данных по часам пока нет — они копятся с этого момента.")
    mx = max(rows.values())
    out = ["<b>Активность по часам</b> (МСК)"]
    for h in range(24):
        v = rows.get(h, 0)
        out.append(f"<code>{h:02d}</code> {'█' * round(v / mx * 14) or '▏'} {v}")
    best = max(rows, key=rows.get)
    out.append(f"\nПик — <b>{best:02d}:00</b>")
    await reply(msg, "\n".join(out))


@cmd(["топ упоминаний", "кого упоминают"], "топупом")
async def c_top_mentions(msg, args, rank):
    rows = await q("SELECT user_id,cnt FROM im_mentions WHERE chat_id=%s ORDER BY cnt DESC LIMIT 15", (msg.chat.id,), many=True)
    if not rows:
        return await reply(msg, "Упоминаний пока нет.")
    out = ["<b>Топ упоминаний</b>"]
    for i, (u, c) in enumerate(rows, 1):
        out.append(f"{i}. {mention(u, await member_name(msg.chat.id, u))} — {c}")
    await reply(msg, "\n".join(out))


# ---------------------------------------------------------------- ачивки
_MSG_STEPS = [(100, "💬 Разговорчивый — 100 сообщений"), (1000, "🗣 Болтун — 1 000 сообщений"),
              (5000, "📣 Голос чата — 5 000 сообщений"), (20000, "👑 Легенда чата — 20 000 сообщений")]
_DAY_STEPS = [(30, "🌱 Месяц в чате"), (180, "🌳 Полгода в чате"), (365, "🏛 Год в чате")]


@cmd(["ачивки", "достижения", "мои ачивки"], "ачивки")
async def c_ach(msg, args, rank):
    uid, name, _ = await resolve_target(msg, args)
    if not uid:
        uid, name = msg.from_user.id, msg.from_user.full_name
    cid = msg.chat.id
    r = await q("SELECT msgs, EXTRACT(DAY FROM now()-first_seen) FROM im_members WHERE chat_id=%s AND user_id=%s", (cid, uid), one=True)
    if not r:
        return await reply(msg, "Этого участника я ещё не видел.")
    msgs, days = int(r[0] or 0), int(r[1] or 0)
    got = [t for n, t in _MSG_STEPS if msgs >= n] + [t for n, t in _DAY_STEPS if days >= n]
    if (await q("SELECT 1 FROM im_ranks WHERE chat_id=%s AND user_id=%s AND rank>0", (cid, uid), one=True)):
        got.append("🛡 Модератор")
    try:
        import iris_eco
        if await iris_eco.balance(cid, uid) >= 100000:
            got.append("💰 Стотысячник")
    except Exception:
        pass
    nxt = next(((n, t) for n, t in _MSG_STEPS if msgs < n), None)
    out = [f"<b>Достижения — {esc(name)}</b>", f"Сообщений: {msgs} · дней в чате: {days}", ""]
    out += got or ["Пока нет — всё впереди."]
    if nxt:
        out.append(f"\nДо следующего: {nxt[0] - msgs} сообщ. → {nxt[1]}")
    await reply(msg, "\n".join(out))


# ---------------------------------------------------------------- ранги: сервис
@cmd(["снять вышедших"], "снятьвсех")
async def c_rm_left(msg, args, rank):
    rows = await q("SELECT user_id FROM im_ranks WHERE chat_id=%s AND rank>0 AND rank<5", (msg.chat.id,), many=True) or []
    n = 0
    for (u,) in rows:
        try:
            st = (await m._bot.get_chat_member(msg.chat.id, u)).status
        except Exception:
            st = "left"
        if st in ("left", "kicked"):
            await q("DELETE FROM im_ranks WHERE chat_id=%s AND user_id=%s", (msg.chat.id, u))
            n += 1
    await reply(msg, f"Снято рангов у вышедших: <b>{n}</b>.")


@cmd(["!снять всех", "снять всех модеров"], "снятьвсех")
async def c_rm_all(msg, args, rank):
    r = await q("DELETE FROM im_ranks WHERE chat_id=%s AND rank>0 AND rank<%s AND user_id<>%s RETURNING 1",
                (msg.chat.id, rank, msg.from_user.id), many=True)
    await m.logact(msg.chat.id, msg.from_user.id, "снять всех", None, str(len(r or [])))
    await reply(msg, f"Снято рангов: <b>{len(r or [])}</b> (только ниже вашего).")


@cmd(["кто назначил", "кто выдал ранг"], "кторанг")
async def c_who_set(msg, args, rank):
    uid, name, _ = await resolve_target(msg, args)
    if not uid:
        return await reply(msg, "Пример: <code>кто назначил @ник</code>")
    r = await q("SELECT rank,by_id,ts FROM im_ranks WHERE chat_id=%s AND user_id=%s", (msg.chat.id, uid), one=True)
    if not r or not r[0]:
        return await reply(msg, f"У {mention(uid, name)} нет ранга.")
    by = await member_name(msg.chat.id, r[1]) if r[1] else "неизвестно"
    await reply(msg, f"{mention(uid, name)}: ранг <b>{r[0]}</b>, назначил {esc(by)}, {r[2].astimezone(_MSK):%d.%m.%Y %H:%M}.")


@cmd(["!все лдк", "все лдк"], "лдк")
async def c_ldk_all(msg, args, rank):
    rows = await q("SELECT user_id,code FROM im_ldk WHERE chat_id=%s ORDER BY user_id", (msg.chat.id,), many=True)
    if not rows:
        return await reply(msg, "Личных доступов нет.")
    by = {}
    for u, c in rows:
        by.setdefault(u, []).append(c)
    out = ["<b>Все ЛДК</b>"]
    for u, cs in by.items():
        out.append(f"{mention(u, await member_name(msg.chat.id, u))}: {', '.join(cs)}")
    await reply(msg, "\n".join(out))


@cmd(["!сброс лдк", "!сброс всех лдк"], "лдк")
async def c_ldk_reset(msg, args, rank):
    uid, name, _ = await resolve_target(msg, args)
    if uid:
        await q("DELETE FROM im_ldk WHERE chat_id=%s AND user_id=%s", (msg.chat.id, uid))
        return await reply(msg, f"ЛДК у {mention(uid, name)} сброшены.")
    await q("DELETE FROM im_ldk WHERE chat_id=%s", (msg.chat.id,))
    await reply(msg, "Все личные доступы сброшены.")
