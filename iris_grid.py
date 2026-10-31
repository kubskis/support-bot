# -*- coding: utf-8 -*-
"""Надзор, этап 3: сетка чатов (общие модераторы, глобальные баны и муты)."""
import logging
from datetime import datetime, timedelta, timezone

import iris_mod as m
from iris_mod import cmd, q, reply, mention, esc, resolve_target, member_name, parse_dur, fmt_dur

log = logging.getLogger("iris_grid")
m.DEFAULT_DK.update({"сетка": 5, "сеткаинфо": 1, "глобан": 5})

_DDL = """
CREATE TABLE IF NOT EXISTS im_grid(id SERIAL PRIMARY KEY, name TEXT UNIQUE, owner BIGINT);
CREATE TABLE IF NOT EXISTS im_grid_chats(grid_id INT, chat_id BIGINT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS im_grid_mods(grid_id INT, user_id BIGINT, rank INT, PRIMARY KEY(grid_id,user_id));
CREATE TABLE IF NOT EXISTS im_gban(grid_id INT, user_id BIGINT, until TIMESTAMPTZ, reason TEXT, PRIMARY KEY(grid_id,user_id));
"""


async def init_tables():
    await q(_DDL)


async def grid_of(cid):
    r = await q("SELECT g.id,g.name,g.owner FROM im_grid g JOIN im_grid_chats c ON c.grid_id=g.id WHERE c.chat_id=%s", (cid,), one=True)
    return r


async def grid_chats(gid):
    return [c for (c,) in await q("SELECT chat_id FROM im_grid_chats WHERE grid_id=%s", (gid,), many=True)]


async def grid_rank(cid, uid):
    """Ранг глобального модератора сетки в этом чате (0 если нет)."""
    r = await q("""SELECT max(gm.rank) FROM im_grid_mods gm JOIN im_grid_chats gc ON gc.grid_id=gm.grid_id
                   WHERE gc.chat_id=%s AND gm.user_id=%s""", (cid, uid), one=True)
    return int(r[0]) if r and r[0] else 0


async def on_join_check(cid, uid):
    """True, если участник в глобальном бане сетки — тогда его банят сразу."""
    g = await grid_of(cid)
    if not g:
        return False
    r = await q("SELECT until,reason FROM im_gban WHERE grid_id=%s AND user_id=%s AND (until IS NULL OR until>now())", (g[0], uid), one=True)
    if not r:
        return False
    try:
        await m.do_ban(cid, uid, 0, f"глобан сетки «{g[1]}»: {r[1] or ''}", m._bot.id)
    except Exception:
        log.exception("gban on join")
    return True


m.GRID_HOOKS.update({"rank": grid_rank, "join": on_join_check})


@cmd(["создать сетку"], "сетка")
async def c_create(msg, args, rank):
    name = (args[0] if args else "")[:24]
    if not name:
        return await reply(msg, "Пример: <code>создать сетку Моясетка</code> (до 24 символов, без пробелов)")
    if await grid_of(msg.chat.id):
        return await reply(msg, "Этот чат уже в сетке.")
    if await q("SELECT 1 FROM im_grid WHERE lower(name)=lower(%s)", (name,), one=True):
        return await reply(msg, "Название занято.")
    r = await q("INSERT INTO im_grid(name,owner) VALUES(%s,%s) RETURNING id", (name, msg.from_user.id), one=True)
    await q("INSERT INTO im_grid_chats VALUES(%s,%s)", (r[0], msg.chat.id))
    await reply(msg, f"🕸 Сетка «{esc(name)}» создана, чат подключён. Добавить другие чаты: <code>установить сетку {esc(name)}</code> в каждом (нужен ранг 5 там и там).")


@cmd(["установить сетку", "+сетка"], "сетка")
async def c_attach(msg, args, rank):
    name = args[0] if args else ""
    g = await q("SELECT id,name,owner FROM im_grid WHERE lower(name)=lower(%s)", (name,), one=True)
    if not g:
        return await reply(msg, "Сетка не найдена.")
    if g[2] != msg.from_user.id and not await m._is_main_admin(msg.from_user.id):
        # владелец сетки должен подтвердить: нужен его ранг 5 хотя бы в одном чате сетки
        ok = False
        for c in await grid_chats(g[0]):
            if await m.get_rank(c, msg.from_user.id) >= 5:
                ok = True
                break
        if not ok:
            return await reply(msg, "Подключить чат может владелец сетки или ранг 5 в одном из её чатов.")
    if await grid_of(msg.chat.id):
        return await reply(msg, "Этот чат уже в сетке.")
    await q("INSERT INTO im_grid_chats VALUES(%s,%s)", (g[0], msg.chat.id))
    await reply(msg, f"🕸 Чат подключён к сетке «{esc(g[1])}».")


@cmd(["удалить из сетки"], "сетка")
async def c_detach(msg, args, rank):
    await q("DELETE FROM im_grid_chats WHERE chat_id=%s", (msg.chat.id,))
    await reply(msg, "Чат вышел из сетки.")


@cmd(["удалить сетку"], "сетка")
async def c_drop(msg, args, rank):
    g = await grid_of(msg.chat.id)
    if not g or (g[2] != msg.from_user.id and not await m._is_main_admin(msg.from_user.id)):
        return await reply(msg, "Удалить сетку может только её владелец.")
    for t in ("im_grid_chats", "im_grid_mods", "im_gban"):
        await q(f"DELETE FROM {t} WHERE grid_id=%s", (g[0],))
    await q("DELETE FROM im_grid WHERE id=%s", (g[0],))
    await reply(msg, "Сетка удалена.")


@cmd(["чаты", "чаты фулл", "сетка"], "сеткаинфо")
async def c_chats(msg, args, rank):
    g = await grid_of(msg.chat.id)
    if not g:
        return await reply(msg, "Чат не в сетке.")
    out = [f"<b>🕸 Сетка «{esc(g[1])}»</b>"]
    for c in await grid_chats(g[0]):
        try:
            ch = await m._bot.get_chat(c)
            out.append(f"• {esc(ch.title)} (<code>{c}</code>)")
        except Exception:
            out.append(f"• <code>{c}</code>")
    await reply(msg, "\n".join(out))


async def _everywhere(msg, fn):
    g = await grid_of(msg.chat.id)
    chats = await grid_chats(g[0]) if g else [msg.chat.id]
    ok = 0
    for c in chats:
        try:
            await fn(c)
            ok += 1
        except Exception as e:
            log.warning("grid action in %s: %s", c, e)
    return g, ok, len(chats)


@cmd(["глобан"], "глобан")
async def c_gban(msg, args, rank):
    uid, name, rest = await resolve_target(msg, args)
    if not uid:
        return await reply(msg, "Укажите цель.")
    if await m.get_rank(msg.chat.id, uid) >= rank and rank < 6 and await m.get_rank(msg.chat.id, uid) > 0:
        return await reply(msg, "Нельзя: у цели ранг не ниже вашего.")
    sec, reason = await m._split_dur_reason(msg, rest, 0)
    g = await grid_of(msg.chat.id)
    if g:
        until = m._until(sec)
        await q("INSERT INTO im_gban VALUES(%s,%s,%s,%s) ON CONFLICT(grid_id,user_id) DO UPDATE SET until=EXCLUDED.until,reason=EXCLUDED.reason", (g[0], uid, until, reason))
    _, ok, tot = await _everywhere(msg, lambda c: m.do_ban(c, uid, sec, reason, msg.from_user.id))
    await reply(msg, f"🌐🔨 {mention(uid, name)} забанен в сетке ({ok}/{tot}) на {fmt_dur(sec)}" + (f"\nПричина: {esc(reason)}" if reason else ""))


@cmd(["глоразбан"], "глобан")
async def c_gunban(msg, args, rank):
    uid, name, _ = await resolve_target(msg, args)
    if not uid:
        return await reply(msg, "Укажите цель.")
    g = await grid_of(msg.chat.id)
    if g:
        await q("DELETE FROM im_gban WHERE grid_id=%s AND user_id=%s", (g[0], uid))
    _, ok, tot = await _everywhere(msg, lambda c: m.do_unban(c, uid, msg.from_user.id))
    await reply(msg, f"🌐✅ {mention(uid, name)} разбанен в сетке ({ok}/{tot}).")


@cmd(["гломут"], "глобан")
async def c_gmute(msg, args, rank):
    uid, name, rest = await resolve_target(msg, args)
    if not uid:
        return await reply(msg, "Укажите цель.")
    sec, reason = await m._split_dur_reason(msg, rest, 7 * 86400)
    _, ok, tot = await _everywhere(msg, lambda c: m.do_mute(c, uid, sec, reason, msg.from_user.id))
    await reply(msg, f"🌐🔇 {mention(uid, name)} в муте по сетке ({ok}/{tot}) на {fmt_dur(sec)}")


@cmd(["глоразмут"], "глобан")
async def c_gunmute(msg, args, rank):
    uid, name, _ = await resolve_target(msg, args)
    if not uid:
        return await reply(msg, "Укажите цель.")
    _, ok, tot = await _everywhere(msg, lambda c: m.do_unmute(c, uid, msg.from_user.id))
    await reply(msg, f"🌐🔊 {mention(uid, name)} размучен по сетке ({ok}/{tot}).")


@cmd(["сетка кик"], "глобан")
async def c_gkick(msg, args, rank):
    uid, name, _ = await resolve_target(msg, args)
    if not uid:
        return await reply(msg, "Укажите цель.")
    _, ok, tot = await _everywhere(msg, lambda c: m.do_kick(c, uid, msg.from_user.id))
    await reply(msg, f"🌐👢 {mention(uid, name)} исключён из сетки ({ok}/{tot}).")


@cmd(["+глмодер", "+гладмин", "сетка !модер"], "сетка")
async def c_gmod_add(msg, args, rank):
    g = await grid_of(msg.chat.id)
    uid, name, rest = await resolve_target(msg, args)
    if not g or not uid:
        return await reply(msg, "Нужны сетка и цель.")
    lvl = next((int(t) for t in rest if t.isdigit() and 1 <= int(t) <= 4), 2)
    await q("INSERT INTO im_grid_mods VALUES(%s,%s,%s) ON CONFLICT(grid_id,user_id) DO UPDATE SET rank=EXCLUDED.rank", (g[0], uid, lvl))
    await reply(msg, f"🌐🛡 {mention(uid, name)} — глобальный модератор сетки, ранг {lvl} (действует во всех её чатах).")


@cmd(["-глмодер", "-гладмин"], "сетка")
async def c_gmod_del(msg, args, rank):
    g = await grid_of(msg.chat.id)
    uid, name, _ = await resolve_target(msg, args)
    if not g or not uid:
        return await reply(msg, "Нужны сетка и цель.")
    await q("DELETE FROM im_grid_mods WHERE grid_id=%s AND user_id=%s", (g[0], uid))
    await reply(msg, "Глобальный модератор снят.")


@cmd(["сетка модеры"], "сеткаинфо")
async def c_gmods(msg, args, rank):
    g = await grid_of(msg.chat.id)
    if not g:
        return await reply(msg, "Чат не в сетке.")
    rows = await q("SELECT user_id,rank FROM im_grid_mods WHERE grid_id=%s ORDER BY rank DESC", (g[0],), many=True)
    await reply(msg, "<b>Глобальные модераторы</b>\n" + ("\n".join(f"{r} — {mention(u, await member_name(msg.chat.id, u))}" for u, r in rows) if rows else "нет"))


@cmd(["сетка баны"], "сеткаинфо")
async def c_gbans(msg, args, rank):
    g = await grid_of(msg.chat.id)
    if not g:
        return await reply(msg, "Чат не в сетке.")
    rows = await q("SELECT user_id,reason,until FROM im_gban WHERE grid_id=%s AND (until IS NULL OR until>now())", (g[0],), many=True)
    await reply(msg, "<b>Глобальные баны</b>\n" + ("\n".join(f"{mention(u, await member_name(msg.chat.id, u))} — {esc(r or '—')}" for u, r, _ in rows) if rows else "нет"))
