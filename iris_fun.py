# -*- coding: utf-8 -*-
"""Надзор, этап 3: кланы, кружки, репутация, награды, заметки, закладки, таймеры, карта,
свои РП (МРП), позвать/созвать, мини-игры и утилиты, автозаявки, воркер таймеров и автокика."""
import asyncio
import logging
import random
import re
import time
from datetime import datetime, timedelta, timezone

from aiogram import F
from aiogram.types import ChatJoinRequest

import iris_mod as m
from iris_mod import cmd, q, reply, mention, esc, resolve_target, member_name, get_cfg, set_cfg, fmt_dur, parse_dur

log = logging.getLogger("iris_fun")
COIN = "🪙"

m.DEFAULT_DK.update({
    "клан": 0, "кружок": 0, "репа": 0, "награда": 3, "награды": 0, "заметка": 0, "закладка": 0, "таймер": 0,
    "карта": 0, "мрп": 4, "созвать": 3, "утилиты": 0, "импорт": 5, "автозаявки": 4, "автокик": 4, "ид": 0,
})

_DDL = """
CREATE TABLE IF NOT EXISTS im_group(id SERIAL PRIMARY KEY, chat_id BIGINT, kind TEXT, name TEXT, owner BIGINT, open BOOLEAN DEFAULT TRUE, bank BIGINT DEFAULT 0);
CREATE UNIQUE INDEX IF NOT EXISTS im_group_u ON im_group(chat_id,kind,lower(name));
CREATE TABLE IF NOT EXISTS im_group_m(group_id INT, user_id BIGINT, main BOOLEAN DEFAULT FALSE, pending BOOLEAN DEFAULT FALSE, PRIMARY KEY(group_id,user_id));
CREATE TABLE IF NOT EXISTS im_rep(chat_id BIGINT, user_id BIGINT, rep INT DEFAULT 0, PRIMARY KEY(chat_id,user_id));
CREATE TABLE IF NOT EXISTS im_rep_log(chat_id BIGINT, from_id BIGINT, to_id BIGINT, ts TIMESTAMPTZ DEFAULT now());
CREATE TABLE IF NOT EXISTS im_awards(id SERIAL PRIMARY KEY, chat_id BIGINT, user_id BIGINT, level INT, text TEXT, by_id BIGINT, ts TIMESTAMPTZ DEFAULT now());
CREATE TABLE IF NOT EXISTS im_notes(id SERIAL PRIMARY KEY, chat_id BIGINT, user_id BIGINT, kind TEXT, text TEXT, ref BIGINT, ts TIMESTAMPTZ DEFAULT now());
CREATE TABLE IF NOT EXISTS im_timers(id SERIAL PRIMARY KEY, chat_id BIGINT, user_id BIGINT, due TIMESTAMPTZ, text TEXT, sent BOOLEAN DEFAULT FALSE);
"""


async def init_tables():
    await q(_DDL)


# ---------------------------------------------------------------- кланы и кружки
CLAN_PRICE = 500


async def _grp(cid, kind, name):
    return await q("SELECT id,name,owner,open FROM im_group WHERE chat_id=%s AND kind=%s AND lower(name)=lower(%s)", (cid, kind, name), one=True)


async def _my_groups(cid, kind, uid):
    return await q("""SELECT g.id,g.name,g.owner,gm.main FROM im_group g JOIN im_group_m gm ON gm.group_id=g.id
                      WHERE g.chat_id=%s AND g.kind=%s AND gm.user_id=%s AND NOT gm.pending""", (cid, kind, uid), many=True)


def _mk_group(kind, word, many):
    """word: 'клан'/'кружок'; many: можно ли состоять в нескольких."""
    plural = "кланы" if kind == "clan" else "кружки"
    ico = "🏰" if kind == "clan" else "⭕"

    @cmd([f"создать {word}"], word)
    async def _create(msg, args, rank):
        name = " ".join(args)[:30]
        if not name:
            return await reply(msg, f"Пример: <code>создать {word} Название</code>")
        cid, uid = msg.chat.id, msg.from_user.id
        if not many and await _my_groups(cid, kind, uid):
            return await reply(msg, "Вы уже состоите в клане. Сначала выйдите.")
        if await _grp(cid, kind, name):
            return await reply(msg, "Такое название уже занято.")
        if kind == "clan":
            import iris_eco
            if await iris_eco._off(msg, "no_eco") is False and not await iris_eco.spend(cid, uid, CLAN_PRICE):
                return await reply(msg, f"Создание клана стоит {CLAN_PRICE} {COIN}.")
        r = await q("INSERT INTO im_group(chat_id,kind,name,owner) VALUES(%s,%s,%s,%s) RETURNING id", (cid, kind, name, uid), one=True)
        await q("INSERT INTO im_group_m(group_id,user_id,main) VALUES(%s,%s,TRUE)", (r[0], uid))
        await reply(msg, f"{ico} {word.capitalize()} «{esc(name)}» создан. Вы — основатель.")

    @cmd([f"{word} вступить", f"вступить в {word}"], word)
    async def _join(msg, args, rank):
        name = " ".join(args)
        g = await _grp(msg.chat.id, kind, name)
        if not g:
            return await reply(msg, "Не найдено. Список: <code>%s</code>" % plural)
        uid = msg.from_user.id
        if not many and await _my_groups(msg.chat.id, kind, uid):
            return await reply(msg, "Вы уже состоите в клане.")
        if await q("SELECT 1 FROM im_group_m WHERE group_id=%s AND user_id=%s", (g[0], uid), one=True):
            return await reply(msg, "Вы уже участник или заявка отправлена.")
        pend = not g[3]
        first = not await _my_groups(msg.chat.id, kind, uid)
        await q("INSERT INTO im_group_m(group_id,user_id,main,pending) VALUES(%s,%s,%s,%s)", (g[0], uid, first and not pend, pend))
        if pend:
            await reply(msg, f"Заявка отправлена. Основатель {mention(g[2], await member_name(msg.chat.id, g[2]))} может принять: <code>{word} принять</code> (ответом).")
        else:
            await reply(msg, f"{ico} Вы вступили в «{esc(g[1])}».")

    @cmd([f"{word} принять"], word)
    async def _accept(msg, args, rank):
        uid, name, rest = await resolve_target(msg, args)
        if not uid:
            return await reply(msg, "Укажите участника.")
        gs = await q("SELECT id,name FROM im_group WHERE chat_id=%s AND kind=%s AND owner=%s", (msg.chat.id, kind, msg.from_user.id), many=True)
        n = 0
        for gid, _ in gs:
            r = await q("UPDATE im_group_m SET pending=FALSE WHERE group_id=%s AND user_id=%s AND pending RETURNING 1", (gid, uid), one=True)
            n += 1 if r else 0
        await reply(msg, "Принят(а)." if n else "Заявок от этого участника нет.")

    @cmd([f"{word} выйти", f"выйти из {word}"], word)
    async def _leave(msg, args, rank):
        gs = await _my_groups(msg.chat.id, kind, msg.from_user.id)
        name = " ".join(args).lower()
        for gid, gname, owner, _ in gs:
            if not name or gname.lower() == name:
                if owner == msg.from_user.id:
                    return await reply(msg, f"Основатель не может выйти. Распустите: <code>{word} распустить</code>")
                await q("DELETE FROM im_group_m WHERE group_id=%s AND user_id=%s", (gid, msg.from_user.id))
                return await reply(msg, "Вы вышли.")
        await reply(msg, "Вы там не состоите.")

    @cmd([f"{word} распустить"], word)
    async def _disband(msg, args, rank):
        gs = await q("SELECT id,name FROM im_group WHERE chat_id=%s AND kind=%s AND owner=%s", (msg.chat.id, kind, msg.from_user.id), many=True)
        name = " ".join(args).lower()
        for gid, gname in gs:
            if not name or gname.lower() == name:
                await q("DELETE FROM im_group_m WHERE group_id=%s", (gid,))
                await q("DELETE FROM im_group WHERE id=%s", (gid,))
                return await reply(msg, f"{word.capitalize()} «{esc(gname)}» распущен.")
        await reply(msg, "Вы не основатель.")

    @cmd([f"{word} тип"], word)
    async def _type(msg, args, rank):
        if not args or args[0].lower() not in ("открытый", "закрытый"):
            return await reply(msg, f"Пример: <code>{word} тип закрытый</code>")
        op = args[0].lower() == "открытый"
        await q("UPDATE im_group SET open=%s WHERE chat_id=%s AND kind=%s AND owner=%s", (op, msg.chat.id, kind, msg.from_user.id))
        await reply(msg, f"Тип: {args[0].lower()}")

    @cmd([f"{word} исключить"], word)
    async def _kickm(msg, args, rank):
        uid, name, _ = await resolve_target(msg, args)
        if not uid:
            return await reply(msg, "Укажите участника.")
        gs = await q("SELECT id FROM im_group WHERE chat_id=%s AND kind=%s AND owner=%s", (msg.chat.id, kind, msg.from_user.id), many=True)
        for (gid,) in gs:
            await q("DELETE FROM im_group_m WHERE group_id=%s AND user_id=%s AND user_id<>%s", (gid, uid, msg.from_user.id))
        await reply(msg, "Исключён(а).")

    @cmd([f"{word} основа", f"{word} основной"], word)
    async def _main(msg, args, rank):
        g = await _grp(msg.chat.id, kind, " ".join(args))
        if not g:
            return await reply(msg, "Не найдено.")
        await q("UPDATE im_group_m SET main=FALSE WHERE user_id=%s AND group_id IN (SELECT id FROM im_group WHERE chat_id=%s AND kind=%s)", (msg.from_user.id, msg.chat.id, kind))
        await q("UPDATE im_group_m SET main=TRUE WHERE group_id=%s AND user_id=%s", (g[0], msg.from_user.id))
        await reply(msg, f"Основной: «{esc(g[1])}»")

    @cmd([f"{word} созвать", f"созвать {word}"], word)
    async def _call(msg, args, rank):
        gs = await _my_groups(msg.chat.id, kind, msg.from_user.id)
        name = " ".join(args).lower()
        for gid, gname, owner, _ in gs:
            if not name or gname.lower() == name:
                rows = await q("SELECT user_id FROM im_group_m WHERE group_id=%s AND NOT pending", (gid,), many=True)
                return await _mention_chunks(msg, [u for (u,) in rows if u != msg.from_user.id], f"{ico} {esc(gname)}")
        await reply(msg, "Вы там не состоите.")

    @cmd([f"{word} состав", f"{word} инфо"], word)
    async def _info(msg, args, rank):
        gs = await _my_groups(msg.chat.id, kind, msg.from_user.id)
        g = await _grp(msg.chat.id, kind, " ".join(args) if args else (gs[0][1] if gs else ""))
        if not g:
            return await reply(msg, "Укажите название или вступите.")
        rows = await q("SELECT user_id FROM im_group_m WHERE group_id=%s AND NOT pending", (g[0],), many=True)
        names = [esc(await member_name(msg.chat.id, u)) for (u,) in rows]
        await reply(msg, f"{ico} <b>{esc(g[1])}</b> ({'открытый' if g[3] else 'закрытый'})\nУчастников: {len(names)}\n" + ", ".join(names[:50]))

    @cmd([plural], word)
    async def _list(msg, args, rank):
        rows = await q("""SELECT g.name,g.owner,count(gm.user_id) FROM im_group g LEFT JOIN im_group_m gm ON gm.group_id=g.id AND NOT gm.pending
                          WHERE g.chat_id=%s AND g.kind=%s GROUP BY g.id ORDER BY 3 DESC LIMIT 25""", (msg.chat.id, kind), many=True)
        if not rows:
            return await reply(msg, f"{plural.capitalize()} пока нет. Создать: <code>создать {word} Название</code>")
        await reply(msg, f"<b>{plural.capitalize()}</b>\n" + "\n".join(f"{ico} {esc(n)} — {c} уч." for n, o, c in rows))


_mk_group("clan", "клан", False)
_mk_group("circle", "кружок", True)


async def _mention_chunks(msg, uids, title="", limit=100):
    uids = list(dict.fromkeys(uids))[:limit]
    if not uids:
        return await reply(msg, "Некого звать.")
    for i in range(0, len(uids), 5):
        part = uids[i:i + 5]
        names = [mention(u, "‌" + (await member_name(msg.chat.id, u))[:20]) for u in part]
        await m._bot.send_message(msg.chat.id, (title + "\n" if i == 0 and title else "") + " ".join(names), parse_mode="HTML")
        await asyncio.sleep(0.4)


@cmd(["созвать всех"], "созвать")
async def c_call_all(msg, args, rank):
    rows = await q("SELECT user_id FROM im_members WHERE chat_id=%s ORDER BY last_seen DESC LIMIT 100", (msg.chat.id,), many=True)
    await _mention_chunks(msg, [u for (u,) in rows if u != msg.from_user.id], "📢 " + esc(" ".join(args)))


@cmd(["созвать онлайн"], "созвать")
async def c_call_online(msg, args, rank):
    rows = await q("SELECT user_id FROM im_members WHERE chat_id=%s AND last_seen>now()-interval '15 minutes' LIMIT 100", (msg.chat.id,), many=True)
    await _mention_chunks(msg, [u for (u,) in rows if u != msg.from_user.id], "📢 " + esc(" ".join(args)))


@cmd(["созвать модеров"], "созвать")
async def c_call_mods(msg, args, rank):
    rows = await q("SELECT user_id FROM im_ranks WHERE chat_id=%s AND rank>0", (msg.chat.id,), many=True)
    await _mention_chunks(msg, [u for (u,) in rows], "🛡 " + esc(" ".join(args)), 20)


# ---------------------------------------------------------------- репутация
async def _rep_change(msg, delta):
    if not msg.reply_to_message or not msg.reply_to_message.from_user:
        return
    to = msg.reply_to_message.from_user
    me = msg.from_user.id
    if to.id == me or to.is_bot:
        return
    cid = msg.chat.id
    r = await q("SELECT 1 FROM im_rep_log WHERE chat_id=%s AND from_id=%s AND to_id=%s AND ts>now()-interval '1 hour'", (cid, me, to.id), one=True)
    if r:
        return await reply(msg, "Менять репутацию одному и тому же участнику можно раз в час.")
    await q("INSERT INTO im_rep_log(chat_id,from_id,to_id) VALUES(%s,%s,%s)", (cid, me, to.id))
    await q("INSERT INTO im_rep(chat_id,user_id,rep) VALUES(%s,%s,%s) ON CONFLICT(chat_id,user_id) DO UPDATE SET rep=im_rep.rep+%s", (cid, to.id, delta, delta))
    cur = (await q("SELECT rep FROM im_rep WHERE chat_id=%s AND user_id=%s", (cid, to.id), one=True))[0]
    await reply(msg, f"{'👍' if delta > 0 else '👎'} Репутация {mention(to.id, to.full_name)}: <b>{cur}</b> ({'+' if delta > 0 else ''}{delta})")


def _mk_rep():
    for sign, d in (("+", 1), ("-", -1)):
        for n in (1, 2, 3, 5):
            @cmd([f"{sign}{n}"] + ([sign, "спасибо", "👍"] if (n == 1 and sign == "+") else []) + (["👎"] if (n == 1 and sign == "-") else []), "репа")
            async def _h(msg, args, rank, d=d * n):
                await _rep_change(msg, d)


_mk_rep()


@cmd(["рейтинг", "репа", "репутация"], "репа")
async def c_rep(msg, args, rank):
    uid, name, _ = await resolve_target(msg, args)
    if uid:
        r = await q("SELECT rep FROM im_rep WHERE chat_id=%s AND user_id=%s", (msg.chat.id, uid), one=True)
        return await reply(msg, f"Репутация {mention(uid, name)}: <b>{r[0] if r else 0}</b>")
    rows = await q("SELECT user_id,rep FROM im_rep WHERE chat_id=%s ORDER BY rep DESC LIMIT 15", (msg.chat.id,), many=True)
    if not rows:
        return await reply(msg, "Репутации пока ни у кого нет. Ответьте «+» на сообщение.")
    await reply(msg, "<b>Рейтинг репутации</b>\n" + "\n".join(f"{i}. {esc(await member_name(msg.chat.id, u))} — {r}" for i, (u, r) in enumerate(rows, 1)))


# ---------------------------------------------------------------- награды
_AW = {1: "🥉", 2: "🥈", 3: "🥇", 4: "🏅", 5: "🎖", 6: "🏆", 7: "💎", 8: "👑"}


@cmd(["наградить"], "награда")
async def c_award(msg, args, rank):
    uid, name, rest = await resolve_target(msg, args)
    if not uid:
        return await reply(msg, "Пример: ответом <code>наградить 3 За активность</code>")
    lvl = next((int(t) for t in rest if t.isdigit() and 1 <= int(t) <= 8), 1)
    text = " ".join(t for t in rest if not (t.isdigit() and int(t) == lvl))[:100]
    await q("INSERT INTO im_awards(chat_id,user_id,level,text,by_id) VALUES(%s,%s,%s,%s,%s)", (msg.chat.id, uid, lvl, text, msg.from_user.id))
    await reply(msg, f"{_AW[lvl]} {mention(uid, name)} награждён(а)" + (f": {esc(text)}" if text else "!"))


@cmd(["награды"], "награды")
async def c_awards(msg, args, rank):
    uid, name, _ = await resolve_target(msg, args)
    if not uid:
        uid, name = msg.from_user.id, msg.from_user.full_name
    rows = await q("SELECT id,level,text FROM im_awards WHERE chat_id=%s AND user_id=%s ORDER BY id", (msg.chat.id, uid), many=True)
    if not rows:
        return await reply(msg, "Наград нет.")
    await reply(msg, f"<b>Награды {mention(uid, name)}</b>\n" + "\n".join(f"#{i} {_AW[l]} {esc(t or '')}" for i, l, t in rows))


@cmd(["снять награду"], "награда")
async def c_unaward(msg, args, rank):
    if not args or not args[0].isdigit():
        return await reply(msg, "Пример: <code>снять награду 12</code> (номер из списка)")
    await q("DELETE FROM im_awards WHERE chat_id=%s AND id=%s", (msg.chat.id, int(args[0])))
    await reply(msg, "Награда снята.")


# ---------------------------------------------------------------- заметки и закладки
@cmd(["+заметка"], "заметка")
async def c_note_add(msg, args, rank):
    text = " ".join(args)[:1000]
    if not text:
        return await reply(msg, "Пример: <code>+заметка текст</code>")
    r = await q("INSERT INTO im_notes(chat_id,user_id,kind,text) VALUES(%s,%s,'note',%s) RETURNING id", (msg.chat.id, msg.from_user.id, text), one=True)
    await reply(msg, f"📝 Заметка #{r[0]} сохранена.")


@cmd(["заметки"], "заметка")
async def c_notes(msg, args, rank):
    rows = await q("SELECT id,text FROM im_notes WHERE chat_id=%s AND user_id=%s AND kind='note' ORDER BY id DESC LIMIT 20", (msg.chat.id, msg.from_user.id), many=True)
    await reply(msg, "<b>Ваши заметки</b>\n" + "\n".join(f"#{i} {esc(t[:60])}" for i, t in rows) if rows else "Заметок нет.")


@cmd(["текст заметки"], "заметка")
async def c_note_text(msg, args, rank):
    if not args or not args[0].isdigit():
        return await reply(msg, "Пример: <code>текст заметки 5</code>")
    r = await q("SELECT text FROM im_notes WHERE chat_id=%s AND user_id=%s AND id=%s AND kind='note'", (msg.chat.id, msg.from_user.id, int(args[0])), one=True)
    await reply(msg, esc(r[0]) if r else "Не найдено.")


@cmd(["-заметка"], "заметка")
async def c_note_del(msg, args, rank):
    if args and args[0].isdigit():
        await q("DELETE FROM im_notes WHERE chat_id=%s AND user_id=%s AND id=%s AND kind='note'", (msg.chat.id, msg.from_user.id, int(args[0])))
        await reply(msg, "Удалено.")


@cmd(["~заметка"], "заметка")
async def c_note_edit(msg, args, rank):
    if len(args) < 2 or not args[0].isdigit():
        return await reply(msg, "Пример: <code>~заметка 5 новый текст</code>")
    await q("UPDATE im_notes SET text=%s WHERE chat_id=%s AND user_id=%s AND id=%s AND kind='note'", (" ".join(args[1:])[:1000], msg.chat.id, msg.from_user.id, int(args[0])))
    await reply(msg, "Обновлено.")


def _link(chat_id, mid):
    s = str(chat_id)
    return f"https://t.me/c/{s[4:]}/{mid}" if s.startswith("-100") else None


@cmd(["+закладка"], "закладка")
async def c_mark_add(msg, args, rank):
    if not msg.reply_to_message:
        return await reply(msg, "Ответьте на сообщение: <code>+закладка</code>")
    rm = msg.reply_to_message
    prev = (rm.text or rm.caption or "")[:80]
    r = await q("INSERT INTO im_notes(chat_id,user_id,kind,text,ref) VALUES(%s,%s,'mark',%s,%s) RETURNING id", (msg.chat.id, msg.from_user.id, prev or " ".join(args), rm.message_id), one=True)
    await reply(msg, f"🔖 Закладка #{r[0]} сохранена.")


@cmd(["закладки"], "закладка")
async def c_marks(msg, args, rank):
    rows = await q("SELECT id,text,ref FROM im_notes WHERE chat_id=%s AND user_id=%s AND kind='mark' ORDER BY id DESC LIMIT 20", (msg.chat.id, msg.from_user.id), many=True)
    if not rows:
        return await reply(msg, "Закладок нет.")
    out = []
    for i, t, ref in rows:
        lk = _link(msg.chat.id, ref)
        out.append(f"#{i} " + (f'<a href="{lk}">{esc(t or "сообщение")}</a>' if lk else esc(t)))
    await reply(msg, "<b>Закладки</b>\n" + "\n".join(out))


@cmd(["-закладка"], "закладка")
async def c_mark_del(msg, args, rank):
    if args and args[0].isdigit():
        await q("DELETE FROM im_notes WHERE chat_id=%s AND user_id=%s AND id=%s AND kind='mark'", (msg.chat.id, msg.from_user.id, int(args[0])))
        await reply(msg, "Удалено.")


# ---------------------------------------------------------------- таймеры
@cmd(["+таймер"], "таймер")
async def c_timer_add(msg, args, rank):
    sec = parse_dur(args[0]) if args else None
    if not sec:
        return await reply(msg, "Пример: <code>+таймер 30м позвонить маме</code>")
    n = (await q("SELECT count(*) FROM im_timers WHERE chat_id=%s AND user_id=%s AND NOT sent", (msg.chat.id, msg.from_user.id), one=True))[0]
    if n >= 10:
        return await reply(msg, "Максимум 10 активных таймеров.")
    r = await q("INSERT INTO im_timers(chat_id,user_id,due,text) VALUES(%s,%s,now()+make_interval(secs=>%s),%s) RETURNING id",
                (msg.chat.id, msg.from_user.id, sec, " ".join(args[1:])[:300]), one=True)
    await reply(msg, f"⏰ Таймер #{r[0]} через {fmt_dur(sec)}.")


@cmd(["таймеры"], "таймер")
async def c_timers(msg, args, rank):
    rows = await q("SELECT id,due,text FROM im_timers WHERE chat_id=%s AND user_id=%s AND NOT sent ORDER BY due", (msg.chat.id, msg.from_user.id), many=True)
    msk = timezone(timedelta(hours=3))
    await reply(msg, "<b>Таймеры</b>\n" + "\n".join(f"#{i} {d.astimezone(msk).strftime('%d.%m %H:%M')} {esc(t)}" for i, d, t in rows) if rows else "Таймеров нет.")


@cmd(["-таймер"], "таймер")
async def c_timer_del(msg, args, rank):
    if args and args[0].isdigit():
        await q("UPDATE im_timers SET sent=TRUE WHERE chat_id=%s AND user_id=%s AND id=%s", (msg.chat.id, msg.from_user.id, int(args[0])))
        await reply(msg, "Таймер удалён.")


async def worker():
    """Раз в 30 с: сработавшие таймеры и автокик молчунов."""
    last_kick = 0
    while True:
        try:
            rows = await q("UPDATE im_timers SET sent=TRUE WHERE NOT sent AND due<=now() RETURNING chat_id,user_id,text", many=True)
            for cid, uid, text in rows or []:
                try:
                    await m._bot.send_message(cid, f"⏰ {mention(uid, await member_name(cid, uid))}: {esc(text) or 'время вышло!'}", parse_mode="HTML")
                except Exception:
                    pass
            if time.time() - last_kick > 3600:
                last_kick = time.time()
                chats = await q("SELECT chat_id,cfg FROM im_chats WHERE enabled", many=True)
                for cid, cfg in chats or []:
                    days = (cfg or {}).get("autokick_days")
                    if not days:
                        continue
                    us = await q("SELECT user_id FROM im_members WHERE chat_id=%s AND msgs<=1 AND first_seen<now()-make_interval(days=>%s)", (cid, int(days)), many=True)
                    for (u,) in us:
                        if await m.get_rank(cid, u) == 0:
                            try:
                                await m.do_kick(cid, u, m._bot.id, "автокик молчунов")
                            except Exception:
                                pass
        except Exception:
            log.exception("fun worker")
        await asyncio.sleep(30)


@cmd(["автокик молчунов"], "автокик")
async def c_autokick(msg, args, rank):
    d = int(args[0]) if args and args[0].isdigit() else 0
    await set_cfg(msg.chat.id, autokick_days=min(d, 7) or None)
    await reply(msg, f"Автокик молчунов: {'через %d дн.' % min(d, 7) if d else 'выкл'} (молчун — не более одного сообщения).")


# ---------------------------------------------------------------- карта
@cmd(["карта"], "карта")
async def c_map(msg, args, rank):
    rows = await q("SELECT lower(profile->>'city') c,count(*) FROM im_members WHERE chat_id=%s AND profile->>'city' IS NOT NULL GROUP BY 1 ORDER BY 2 DESC LIMIT 25", (msg.chat.id,), many=True)
    if not rows:
        return await reply(msg, "Города не указаны. Добавьте: <code>+город Москва</code>")
    await reply(msg, "<b>Карта участников</b>\n" + "\n".join(f"📍 {esc(c.title())} — {n}" for c, n in rows))


@cmd(["кто из"], "карта")
async def c_who_from(msg, args, rank):
    city = " ".join(args).lower()
    rows = await q("SELECT user_id,name FROM im_members WHERE chat_id=%s AND lower(profile->>'city')=%s LIMIT 50", (msg.chat.id, city), many=True)
    await reply(msg, f"<b>Из города {esc(city.title())}</b>\n" + (", ".join(esc(n) for _, n in rows) if rows else "никого"))


# ---------------------------------------------------------------- МРП (свои РП)
@cmd(["+мрп"], "мрп")
async def c_mrp_add(msg, args, rank):
    if len(args) < 2:
        return await reply(msg, "Пример: <code>+мрп хлопнуть хлопнул(а)</code> (команда и глагол)")
    cfg = await get_cfg(msg.chat.id)
    mrp = cfg.get("mrp", {})
    if len(mrp) >= 50:
        return await reply(msg, "Максимум 50 своих РП.")
    mrp[args[0].lower()] = " ".join(args[1:])[:60]
    await set_cfg(msg.chat.id, mrp=mrp)
    await reply(msg, f"Добавлено: {esc(args[0])}")


@cmd(["-мрп"], "мрп")
async def c_mrp_del(msg, args, rank):
    cfg = await get_cfg(msg.chat.id)
    mrp = cfg.get("mrp", {})
    if args and args[0].lower() in mrp:
        mrp.pop(args[0].lower())
        await set_cfg(msg.chat.id, mrp=mrp or None)
        await reply(msg, "Удалено.")


@cmd(["мрп"], "рп")
async def c_mrp_list(msg, args, rank):
    mrp = (await get_cfg(msg.chat.id)).get("mrp", {})
    await reply(msg, "<b>Свои РП</b>\n" + (", ".join(sorted(mrp)) if mrp else "нет. Добавить: <code>+мрп команда глагол</code>"))


async def _mrp_dynamic(msg, first, rank):
    word = first.split(None, 1)[0].lower()
    cfg = await get_cfg(msg.chat.id)
    verb = (cfg.get("mrp") or {}).get(word)
    if not verb or cfg.get("rp_off"):
        return False
    uid, name, _ = await resolve_target(msg, first.split()[1:])
    if not uid:
        return False
    await reply(msg, f"{mention(msg.from_user.id, msg.from_user.full_name)} {esc(verb)} {mention(uid, name)}")
    return True


m.DYNAMIC.append(_mrp_dynamic)


# ---------------------------------------------------------------- утилиты и мини-игры
_YN = ["Да", "Нет", "Скорее да", "Скорее нет", "Определённо да", "Даже не думай", "Возможно", "Весьма сомнительно"]


@cmd(["!инфа"], "утилиты")
async def c_info(msg, args, rank):
    t = " ".join(args) or "это"
    await reply(msg, f"📊 Вероятность того, что {esc(t)}: <b>{random.randint(0, 100)}%</b>")


@cmd(["!выбери"], "утилиты")
async def c_choose(msg, args, rank):
    parts = [p.strip() for p in re.split(r"\s+или\s+|,", " ".join(args)) if p.strip()]
    if len(parts) < 2:
        return await reply(msg, "Пример: <code>выбери чай или кофе</code>")
    await reply(msg, f"🤔 Я выбираю: <b>{esc(random.choice(parts))}</b>")


@cmd(["!жребий"], "утилиты")
async def c_lot(msg, args, rank):
    rows = await q("SELECT user_id,name FROM im_members WHERE chat_id=%s AND last_seen>now()-interval '7 days'", (msg.chat.id,), many=True)
    if not rows:
        return await reply(msg, "Некого выбирать.")
    u, n = random.choice(rows)
    await reply(msg, f"🎯 Жребий пал на {mention(u, n)}")


@cmd(["!данет"], "утилиты")
async def c_yn(msg, args, rank):
    await reply(msg, f"🔮 {random.choice(_YN)}")


@cmd(["рандом", "!рандом"], "утилиты")
async def c_rand(msg, args, rank):
    nums = [int(t) for t in args if t.lstrip("-").isdigit()]
    lo, hi = (nums[0], nums[1]) if len(nums) >= 2 else (1, nums[0] if nums else 100)
    if lo > hi:
        lo, hi = hi, lo
    await reply(msg, f"🎲 {random.randint(lo, hi)}")


@cmd(["!кто"], "утилиты")
async def c_who(msg, args, rank):
    t = " ".join(args)
    if not t:
        return
    rows = await q("SELECT user_id,name FROM im_members WHERE chat_id=%s AND last_seen>now()-interval '7 days'", (msg.chat.id,), many=True)
    if rows:
        u, n = random.choice(rows)
        await reply(msg, f"Я думаю, {esc(t)} — {mention(u, n)}")


@cmd(["!скажи"], "утилиты")
async def c_say(msg, args, rank):
    t = " ".join(args)
    if t:
        await m._bot.send_message(msg.chat.id, esc(t), parse_mode="HTML")


@cmd(["шипперим", "пейринг"], "утилиты")
async def c_ship(msg, args, rank):
    rows = await q("SELECT user_id,name FROM im_members WHERE chat_id=%s AND last_seen>now()-interval '14 days'", (msg.chat.id,), many=True)
    if len(rows) < 2:
        return await reply(msg, "Мало участников.")
    (a, an), (b, bn) = random.sample(rows, 2)
    await reply(msg, f"💘 Шипперим: {mention(a, an)} + {mention(b, bn)} = {random.randint(30, 100)}% совместимости")


@cmd(["!пинг"], "утилиты")
async def c_ping(msg, args, rank):
    t0 = time.monotonic()
    s = await reply(msg, "🏓 Понг…")
    try:
        await s.edit_text(f"🏓 Понг! {int((time.monotonic() - t0) * 1000)} мс")
    except Exception:
        pass


@cmd(["бот"], "утилиты")
async def c_bot(msg, args, rank):
    await reply(msg, "🤖 На месте!")


@cmd(["!погода"], "утилиты")
async def c_weather(msg, args, rank):
    city = " ".join(args) or (await q("SELECT profile->>'city' FROM im_members WHERE chat_id=%s AND user_id=%s", (msg.chat.id, msg.from_user.id), one=True) or [None])[0]
    if not city:
        return await reply(msg, "Пример: <code>!погода Москва</code>")
    import aiohttp
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"https://wttr.in/{city}", params={"format": "%l: %c %t, %C, ветер %w, влажность %h", "lang": "ru"}, timeout=aiohttp.ClientTimeout(total=8)) as r:
                txt = (await r.text()).strip()
        await reply(msg, "🌤 " + esc(txt[:300]))
    except Exception:
        await reply(msg, "Не удалось получить погоду.")


@cmd(["!ид", "!id"], "ид")
async def c_id(msg, args, rank):
    uid, name, _ = await resolve_target(msg, args)
    out = [f"Чат: <code>{msg.chat.id}</code>"]
    out.append(f"{mention(uid, name)}: <code>{uid}</code>" if uid else f"Вы: <code>{msg.from_user.id}</code>")
    await reply(msg, "\n".join(out))


@cmd(["код чата"], "ид")
async def c_chat_code(msg, args, rank):
    await reply(msg, f"Код чата: <code>{msg.chat.id}</code>")


@cmd(["ники"], "утилиты")
async def c_nicks(msg, args, rank):
    rows = await q("SELECT user_id,profile->>'nick' FROM im_members WHERE chat_id=%s AND profile->>'nick' IS NOT NULL LIMIT 40", (msg.chat.id,), many=True)
    await reply(msg, "<b>Ники</b>\n" + "\n".join(f"{esc(await member_name(msg.chat.id, u))} — {esc(n)}" for u, n in rows) if rows else "Никто не задал ник: <code>+ник Имя</code>")


@cmd(["импорт команд из"], "импорт")
async def c_import(msg, args, rank):
    try:
        src = int(args[0])
    except Exception:
        return await reply(msg, "Пример: <code>импорт команд из -100123456789</code> (код чата: <code>код чата</code>)")
    if await m.get_rank(src, msg.from_user.id) < 5:
        return await reply(msg, "В исходном чате нужен ранг 5.")
    await q("DELETE FROM im_dk WHERE chat_id=%s", (msg.chat.id,))
    await q("INSERT INTO im_dk(chat_id,code,rank) SELECT %s,code,rank FROM im_dk WHERE chat_id=%s", (msg.chat.id, src))
    await reply(msg, "Настройки ДК импортированы.")


# ---------------------------------------------------------------- автозаявки
@cmd(["+автозаявки"], "автозаявки")
async def c_autoreq_on(msg, args, rank):
    await set_cfg(msg.chat.id, autoreq=True)
    await reply(msg, "Заявки на вступление будут приниматься автоматически (для чата с вступлением по заявкам; боту нужно право приглашать).")


@cmd(["-автозаявки"], "автозаявки")
async def c_autoreq_off(msg, args, rank):
    await set_cfg(msg.chat.id, autoreq=None)
    await reply(msg, "Автозаявки выключены.")


@m.router.chat_join_request()
async def on_join_request(req: ChatJoinRequest):
    try:
        if not await m.chat_enabled(req.chat.id):
            return
        if (await get_cfg(req.chat.id)).get("autoreq"):
            await req.approve()
    except Exception:
        log.exception("join request")
