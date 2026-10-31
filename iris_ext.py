# -*- coding: utf-8 -*-
"""Надзор, этап A: слоумод, передача создателя и завещание, голосование +гк, срок и продление браков,
режим развода, сетка из ЛС, защита входа (капча, только Premium, минимальный возраст аккаунта),
удаление служебных сообщений о входах и выходах."""
import asyncio
import logging
import re
import time
from datetime import datetime, timedelta, timezone

from aiogram import BaseMiddleware, F
from aiogram.types import CallbackQuery, ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup, Message

import iris_mod as m
from iris_mod import cmd, q, reply, mention, esc, resolve_target, member_name, get_cfg, set_cfg, fmt_dur, parse_dur

log = logging.getLogger("iris_ext")
m.DEFAULT_DK.update({"гк": 0, "слоумод": 4, "наследство": 0, "завещание": 5, "защитавхода": 4, "брачныенастройки": 4})

_DDL = """
CREATE TABLE IF NOT EXISTS im_captcha(chat_id BIGINT, user_id BIGINT, msg_id BIGINT, due TIMESTAMPTZ, done BOOLEAN DEFAULT FALSE, PRIMARY KEY(chat_id,user_id));
ALTER TABLE im_marriage ADD COLUMN IF NOT EXISTS until TIMESTAMPTZ;
ALTER TABLE im_marriage ADD COLUMN IF NOT EXISTS warned BOOLEAN DEFAULT FALSE;
"""


async def init_tables():
    await q(_DDL)


# ---------------------------------------------------------------- слоумод
_last = {}


async def _slow_hook(msg, cfg, rank):
    s = cfg.get("slow")
    if not s:
        return False
    key = (msg.chat.id, msg.from_user.id)
    now = time.time()
    if now - _last.get(key, 0) < s:
        try:
            await msg.delete()
        except Exception:
            pass
        return True
    _last[key] = now
    if len(_last) > 20000:
        _last.clear()
    return False


m.AUTOMOD.append(_slow_hook)


@cmd(["слоумод"], "слоумод")
async def c_slow(msg, args, rank):
    if not args:
        s = (await get_cfg(msg.chat.id)).get("slow")
        return await reply(msg, f"Слоумод: {fmt_dur(s) if s else 'выключен'}. Пример: <code>слоумод 30с</code> или <code>слоумод выкл</code>")
    if args[0].lower() in ("выкл", "0", "off", "нет"):
        await set_cfg(msg.chat.id, slow=None)
        return await reply(msg, "Слоумод выключен.")
    d = parse_dur(args[0])
    if not d:
        return await reply(msg, "Пример: <code>слоумод 30с</code> (от 1 секунды до 1 часа)")
    d = max(1, min(d, 3600))
    await set_cfg(msg.chat.id, slow=d)
    await reply(msg, f"🐢 Слоумод: одно сообщение в {fmt_dur(d)} (для участников без ранга; боту нужно право удалять сообщения).")


# ---------------------------------------------------------------- создатель, завещание
async def _set_rank(cid, uid, rank, by):
    await q("""INSERT INTO im_ranks(chat_id,user_id,rank,by_id) VALUES(%s,%s,%s,%s)
               ON CONFLICT(chat_id,user_id) DO UPDATE SET rank=EXCLUDED.rank, by_id=EXCLUDED.by_id, ts=now()""", (cid, uid, rank, by))


@cmd(["!передать создателя", "передать создателя"], "завещание")
async def c_give_creator(msg, args, rank):
    if rank < 5:
        return await reply(msg, "Только создатель (ранг 5).")
    uid, name, _ = await resolve_target(msg, args)
    if not uid or uid == msg.from_user.id:
        return await reply(msg, "Укажите нового создателя: <code>!передать создателя @ник</code>")
    await _set_rank(msg.chat.id, uid, 5, msg.from_user.id)
    await _set_rank(msg.chat.id, msg.from_user.id, 4, msg.from_user.id)
    await m.logact(msg.chat.id, msg.from_user.id, "give_creator", uid)
    await reply(msg, f"👑 {mention(uid, name)} теперь создатель в «Надзоре» (ранг 5). Вы получили ранг 4.\n"
                     "Владение чатом в самом Telegram от этого не меняется.")


@cmd(["+завещание"], "завещание")
async def c_will_set(msg, args, rank):
    uid, name, _ = await resolve_target(msg, args)
    if rank < 5 or not uid or uid == msg.from_user.id:
        return await reply(msg, "Пример (ранг 5): <code>+завещание @наследник</code>")
    await set_cfg(msg.chat.id, heir={"uid": uid, "from": msg.from_user.id})
    await reply(msg, f"📜 Наследник: {mention(uid, name)}. Он сможет принять права, если вы не будете в чате более 30 дней.")


@cmd(["-завещание"], "завещание")
async def c_will_del(msg, args, rank):
    if rank < 5:
        return
    await set_cfg(msg.chat.id, heir=None)
    await reply(msg, "Завещание отменено.")


@cmd(["моё завещание", "мое завещание", "завещание"], "наследство")
async def c_will_show(msg, args, rank):
    h = (await get_cfg(msg.chat.id)).get("heir")
    if not h:
        return await reply(msg, "Завещание не составлено.")
    await reply(msg, f"📜 Наследник: {mention(h['uid'], await member_name(msg.chat.id, h['uid']))}")


@cmd(["вступить в наследство"], "наследство")
async def c_inherit(msg, args, rank):
    cfg = await get_cfg(msg.chat.id)
    h = cfg.get("heir")
    if not h or h["uid"] != msg.from_user.id:
        return await reply(msg, "Вы не указаны наследником в этом чате.")
    r = await q("SELECT last_seen FROM im_members WHERE chat_id=%s AND user_id=%s", (msg.chat.id, h["from"]), one=True)
    if r and (datetime.now(timezone.utc) - r[0]) < timedelta(days=30):
        left = 30 - (datetime.now(timezone.utc) - r[0]).days
        return await reply(msg, f"Создатель был активен недавно. Наследство откроется через {left} дн. его отсутствия.")
    await _set_rank(msg.chat.id, msg.from_user.id, 5, msg.from_user.id)
    await _set_rank(msg.chat.id, h["from"], 4, msg.from_user.id)
    await set_cfg(msg.chat.id, heir=None)
    await m.logact(msg.chat.id, msg.from_user.id, "inherit", h["from"])
    await reply(msg, f"👑 {mention(msg.from_user.id, msg.from_user.full_name)} принял(а) права создателя в «Надзоре».")


# ---------------------------------------------------------------- голосование +гк
_votes = {}       # (chat_id, msg_id) -> dict
_VOTE_CODES = {"бан", "мут", "кик", "варн", "разбан", "снятьварн"}


@cmd(["+гк"], "гк")
async def c_gk(msg, args, rank):
    nums, rest = [], list(args)
    while rest and rest[0].isdigit() and len(nums) < 2:
        nums.append(int(rest.pop(0)))
    need = max(2, min(nums[0] if nums else 5, 50))
    minrank = nums[1] if len(nums) > 1 and nums[1] <= 5 else 0
    line = " ".join(rest)
    if not line:
        return await reply(msg, "Пример: ответом <code>+гк 5 мут 1ч спам</code> — голосование за любую из команд: бан, мут, кик, варн.")
    if m.ALIAS_FN:
        line = m.ALIAS_FN(line, msg)
    hit = m._match(line)
    if not hit or hit[1] not in _VOTE_CODES:
        return await reply(msg, "В голосовании можно использовать только: бан, мут, кик, варн, разбан, -варн.")
    fn, code, a = hit
    uid, name, _ = await resolve_target(msg, a)
    if not uid:
        return await reply(msg, "Укажите цель ответом на её сообщение или @ником.")
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"👍 Голосовать (0/{need})", callback_data="imgk:y")]])
    sent = await reply(msg, f"🗳 <b>Голосование</b>: {esc(line)}\nЦель: {mention(uid, name)}\nНужно голосов: <b>{need}</b>" + (f" (ранг от {minrank})" if minrank else ""), reply_markup=kb)
    _votes[(msg.chat.id, sent.message_id)] = {"need": need, "minrank": minrank, "fn": fn, "args": a, "msg": msg, "voters": set(), "ts": time.time(), "line": line, "uid": uid, "name": name}
    for k in [k for k, v in _votes.items() if time.time() - v["ts"] > 7200]:
        _votes.pop(k, None)


@cmd(["-гк"], "гк")
async def c_gk_cancel(msg, args, rank):
    n = 0
    for k, v in list(_votes.items()):
        if k[0] == msg.chat.id and (v["msg"].from_user.id == msg.from_user.id or rank >= 4):
            _votes.pop(k)
            n += 1
    await reply(msg, f"Отменено голосований: {n}")


@m.router.callback_query(F.data == "imgk:y")
async def cb_gk(cb: CallbackQuery):
    key = (cb.message.chat.id, cb.message.message_id)
    v = _votes.get(key)
    if not v:
        return await cb.answer("Голосование завершено или устарело.", show_alert=True)
    if cb.from_user.id in v["voters"]:
        return await cb.answer("Вы уже голосовали.")
    if v["minrank"] and await m.get_rank(key[0], cb.from_user.id) < v["minrank"]:
        return await cb.answer("Нужен более высокий ранг.", show_alert=True)
    v["voters"].add(cb.from_user.id)
    n = len(v["voters"])
    if n < v["need"]:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"👍 Голосовать ({n}/{v['need']})", callback_data="imgk:y")]])
        try:
            await cb.message.edit_reply_markup(reply_markup=kb)
        except Exception:
            pass
        return await cb.answer("Голос принят")
    _votes.pop(key, None)
    try:
        await v["fn"](v["msg"], v["args"], 5)
        await cb.message.edit_text(f"✅ Голосование состоялось ({n}/{v['need']}): {esc(v['line'])} → {mention(v['uid'], v['name'])}", parse_mode="HTML")
    except Exception as e:
        await cb.message.edit_text(m.human_error(e), parse_mode="HTML")
    await cb.answer()


# ---------------------------------------------------------------- браки: срок, продление, режим развода
@cmd(["брак срок"], "брачныенастройки")
async def c_marry_term(msg, args, rank):
    if not args or not args[0].isdigit():
        return await reply(msg, "Пример: <code>брак срок 30</code> (дней; 0 — бессрочно)")
    d = int(args[0])
    await set_cfg(msg.chat.id, marry_days=d or None)
    await reply(msg, f"Срок брака: {d} дн." if d else "Браки бессрочные.")


@cmd(["брак цена продления"], "брачныенастройки")
async def c_marry_renew_price(msg, args, rank):
    if not args or not args[0].isdigit():
        return await reply(msg, "Пример: <code>брак цена продления 50</code>")
    await set_cfg(msg.chat.id, marry_renew=int(args[0]))
    await reply(msg, f"Продление брака: {args[0]} 🪙")


@cmd(["брак продлить", "продлить брак"], "брак")
async def c_marry_renew(msg, args, rank):
    import iris_eco
    cid = msg.chat.id
    cfg = await get_cfg(cid)
    days = int(cfg.get("marry_days") or 0)
    r = await q("SELECT id,until FROM im_marriage WHERE chat_id=%s AND active AND (a=%s OR b=%s)", (cid, msg.from_user.id, msg.from_user.id), one=True)
    if not r:
        return await reply(msg, "Вы не в браке.")
    if not days:
        return await reply(msg, "В этом чате браки бессрочные — продлевать не нужно.")
    price = int(cfg.get("marry_renew") or 0)
    if price and not await iris_eco.spend(cid, msg.from_user.id, price):
        return await reply(msg, f"Продление стоит {price} 🪙, у вас не хватает.")
    base = max(r[1] or datetime.now(timezone.utc), datetime.now(timezone.utc))
    new = base + timedelta(days=days)
    await q("UPDATE im_marriage SET until=%s, warned=FALSE WHERE id=%s", (new, r[0]))
    await reply(msg, f"💍 Брак продлён до {new.astimezone(timezone(timedelta(hours=3))).strftime('%d.%m.%Y')}.")


@cmd(["развод режим"], "брачныенастройки")
async def c_divorce_mode(msg, args, rank):
    mode = {"один": "one", "два": "two", "выкл": "off", "выключить": "off"}.get(args[0].lower() if args else "")
    if not mode:
        return await reply(msg, "Пример: <code>развод режим один</code> | <code>два</code> (нужно согласие обоих) | <code>выкл</code>")
    await set_cfg(msg.chat.id, divorce_mode=mode)
    await reply(msg, f"Режим развода: {args[0].lower()}")


async def divorce_gate(msg, marriage_row):
    """Вызывается из iris_eco.c_divorce. True — развод обработан здесь (отключён или нужно согласие второго)."""
    mode = (await get_cfg(msg.chat.id)).get("divorce_mode", "one")
    if mode == "off":
        await reply(msg, "Развод в этом чате отключён. Расторгнуть брак может только модератор.")
        return True
    if mode == "two":
        a, b = marriage_row[1], marriage_row[2]
        other = b if a == msg.from_user.id else a
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💔 Согласен(на)", callback_data=f"imdv:y:{marriage_row[0]}:{other}"),
                                                    InlineKeyboardButton(text="💞 Нет", callback_data=f"imdv:n:{marriage_row[0]}:{other}")]])
        await reply(msg, f"{mention(other, await member_name(msg.chat.id, other))}, {mention(msg.from_user.id, msg.from_user.full_name)} просит развод. Вы согласны?", reply_markup=kb)
        return True
    return False


@m.router.callback_query(F.data.startswith("imdv:"))
async def cb_divorce(cb: CallbackQuery):
    _, ans, mid, other = cb.data.split(":")
    if cb.from_user.id != int(other):
        return await cb.answer("Это решение второго супруга.", show_alert=True)
    if ans == "y":
        await q("UPDATE im_marriage SET active=FALSE WHERE id=%s", (int(mid),))
        await cb.message.edit_text("💔 Брак расторгнут по согласию обоих.")
    else:
        await cb.message.edit_text("💞 Развод отклонён.")
    await cb.answer()


# ---------------------------------------------------------------- защита входа
_ANCHORS = [(0, (2013, 8)), (100e6, (2014, 9)), (300e6, (2015, 10)), (500e6, (2016, 6)), (1.0e9, (2017, 10)), (1.5e9, (2018, 9)),
            (2.0e9, (2019, 5)), (5.0e9, (2021, 8)), (6.0e9, (2022, 9)), (7.0e9, (2023, 9)), (8.0e9, (2024, 10)), (8.6e9, (2025, 9))]


def approx_age_months(uid):
    """Грубая оценка возраста аккаунта по числовому id (Telegram дату регистрации не отдаёт; погрешность до полугода)."""
    pts = [(i, y * 12 + mo) for i, (y, mo) in _ANCHORS]
    if uid >= pts[-1][0]:
        reg = pts[-1][1] + (uid - pts[-1][0]) / 3e8
    else:
        reg = pts[0][1]
        for (i0, t0), (i1, t1) in zip(pts, pts[1:]):
            if i0 <= uid < i1:
                reg = t0 + (t1 - t0) * (uid - i0) / (i1 - i0)
                break
    now = datetime.now(timezone.utc)
    return max(0, now.year * 12 + now.month - reg)


@cmd(["минакк"], "защитавхода")
async def c_min_age(msg, args, rank):
    if not args or not args[0].isdigit():
        return await reply(msg, "Пример: <code>минакк 6</code> (месяцев; 0 — выкл). Возраст оценивается по номеру аккаунта, это приблизительно (±полгода).")
    n = int(args[0])
    await set_cfg(msg.chat.id, min_age=n or None)
    await reply(msg, f"Минимальный возраст аккаунта: {n} мес. (приблизительно)" if n else "Фильтр по возрасту аккаунта выключен.")


@cmd(["+входы с тг прем", "+премвход"], "защитавхода")
async def c_prem_on(msg, args, rank):
    await set_cfg(msg.chat.id, prem_only=True)
    await reply(msg, "Входить смогут только пользователи с Telegram Premium.")


@cmd(["-входы с тг прем", "-премвход"], "защитавхода")
async def c_prem_off(msg, args, rank):
    await set_cfg(msg.chat.id, prem_only=None)
    await reply(msg, "Ограничение по Premium снято.")


@cmd(["капча"], "защитавхода")
async def c_captcha(msg, args, rank):
    a = (args[0].lower() if args else "")
    if a in ("выкл", "0", "off"):
        await set_cfg(msg.chat.id, captcha=None)
        return await reply(msg, "Капча выключена.")
    mins = int(a) if a.isdigit() else 5
    await set_cfg(msg.chat.id, captcha=max(1, min(mins, 60)))
    await reply(msg, f"🤖 Капча включена: новичок в муте, пока не нажмёт кнопку; {max(1, min(mins, 60))} мин на нажатие, иначе кик. Боту нужны права ограничивать и блокировать.")


@cmd(["-входы"], "защитавхода")
async def c_hide_joins(msg, args, rank):
    await set_cfg(msg.chat.id, del_joins=True)
    await reply(msg, "Служебные сообщения о входах будут удаляться.")


@cmd(["+входы"], "защитавхода")
async def c_show_joins(msg, args, rank):
    await set_cfg(msg.chat.id, del_joins=None)
    await reply(msg, "Сообщения о входах остаются.")


@cmd(["-выходы"], "защитавхода")
async def c_hide_leaves(msg, args, rank):
    await set_cfg(msg.chat.id, del_leaves=True)
    await reply(msg, "Служебные сообщения о выходах будут удаляться.")


@cmd(["+выходы"], "защитавхода")
async def c_show_leaves(msg, args, rank):
    await set_cfg(msg.chat.id, del_leaves=None)
    await reply(msg, "Сообщения о выходах остаются.")


async def _join_guard(cid, u, cfg, msg):
    if cfg.get("del_joins") and msg is not None:
        try:
            await msg.delete()
        except Exception:
            pass
    if await m.get_rank(cid, u.id) > 0:
        return False
    if cfg.get("prem_only") and not getattr(u, "is_premium", False):
        try:
            await m.do_kick(cid, u.id, m._bot.id, "вход только с Premium")
        except Exception:
            pass
        return True
    ma = cfg.get("min_age")
    if ma and approx_age_months(u.id) < int(ma):
        try:
            await m.do_kick(cid, u.id, m._bot.id, "слишком новый аккаунт")
        except Exception:
            pass
        return True
    mins = cfg.get("captcha")
    if mins:
        try:
            await m._bot.restrict_chat_member(cid, u.id, permissions=ChatPermissions(can_send_messages=False))
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Я человек", callback_data=f"imcap:{u.id}")]])
            sent = await m._bot.send_message(cid, f"{mention(u.id, u.full_name)}, подтвердите, что вы не бот, в течение {mins} мин.", parse_mode="HTML", reply_markup=kb)
            await q("""INSERT INTO im_captcha(chat_id,user_id,msg_id,due) VALUES(%s,%s,%s,now()+make_interval(mins=>%s))
                       ON CONFLICT(chat_id,user_id) DO UPDATE SET msg_id=EXCLUDED.msg_id,due=EXCLUDED.due,done=FALSE""", (cid, u.id, sent.message_id, int(mins)))
        except Exception:
            log.exception("captcha")
            return False
        return True
    return False


m.JOIN_HOOKS.append(_join_guard)


async def _service_hook(msg, cfg):
    if msg.left_chat_member and cfg.get("del_leaves"):
        try:
            await msg.delete()
        except Exception:
            pass


m.SERVICE_HOOKS.append(_service_hook)


@m.router.callback_query(F.data.startswith("imcap:"))
async def cb_captcha(cb: CallbackQuery):
    uid = int(cb.data.split(":")[1])
    if cb.from_user.id != uid:
        return await cb.answer("Эта кнопка не для вас.", show_alert=True)
    cid = cb.message.chat.id
    r = await q("UPDATE im_captcha SET done=TRUE WHERE chat_id=%s AND user_id=%s AND NOT done RETURNING 1", (cid, uid), one=True)
    if not r:
        return await cb.answer("Уже подтверждено.")
    try:
        await m.do_unmute(cid, uid, m._bot.id)
    except Exception as e:
        return await cb.answer(m.human_error(e)[:180], show_alert=True)
    try:
        await cb.message.delete()
    except Exception:
        pass
    w = (await get_cfg(cid)).get("welcome")
    if w:
        await m._bot.send_message(cid, w.replace("{имя}", mention(uid, cb.from_user.full_name)).replace("{ссылка}", mention(uid, cb.from_user.full_name)), parse_mode="HTML")
    await cb.answer("Добро пожаловать!")


# ---------------------------------------------------------------- сетка из ЛС
class PrivateGridMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        if isinstance(event, Message) and event.chat.type == "private" and event.from_user and event.text:
            try:
                if await self._handle(event):
                    return None
            except Exception:
                log.exception("private grid")
        return await handler(event, data)

    async def _handle(self, msg):
        t = msg.text.strip()
        low = t.lower()
        uid = msg.from_user.id
        if low.startswith("создать сетку"):
            name = t[len("создать сетку"):].strip()
            if not re.match(r"^\S{1,24}$", name):
                await msg.answer("Название — до 24 символов без пробелов. Пример: <code>создать сетку Моясетка</code>", parse_mode="HTML")
                return True
            if await q("SELECT 1 FROM im_grid WHERE lower(name)=lower(%s)", (name,), one=True):
                await msg.answer("Такое название уже занято.")
                return True
            if (await q("SELECT count(*) FROM im_grid WHERE owner=%s", (uid,), one=True))[0] >= 5:
                await msg.answer("Можно владеть не более чем 5 сетками.")
                return True
            await q("INSERT INTO im_grid(name,owner) VALUES(%s,%s)", (name, uid))
            await msg.answer(f"🕸 Сетка «{name}» создана. Подключите чаты: в каждом чате с «Надзором» напишите <code>установить сетку {name}</code>.", parse_mode="HTML")
            return True
        if low in ("мои сетки", "сетки"):
            rows = await q("""SELECT g.name,count(c.chat_id) FROM im_grid g LEFT JOIN im_grid_chats c ON c.grid_id=g.id WHERE g.owner=%s GROUP BY g.id""", (uid,), many=True)
            await msg.answer("🕸 Ваши сетки:\n" + "\n".join(f"• {n} — чатов: {c}" for n, c in rows) if rows else "У вас нет сеток. Создать: <code>создать сетку Имя</code>", parse_mode="HTML")
            return True
        if low.startswith("удалить сетку"):
            name = t[len("удалить сетку"):].strip()
            g = await q("SELECT id FROM im_grid WHERE lower(name)=lower(%s) AND owner=%s", (name, uid), one=True)
            if not g:
                await msg.answer("Сетка не найдена или вы не владелец.")
                return True
            for tb in ("im_grid_chats", "im_grid_mods", "im_gban"):
                await q(f"DELETE FROM {tb} WHERE grid_id=%s", (g[0],))
            await q("DELETE FROM im_grid WHERE id=%s", (g[0],))
            await msg.answer("Сетка удалена.")
            return True
        return False


# ---------------------------------------------------------------- воркер: браки и капча
async def worker():
    while True:
        try:
            # срок браков
            chats = await q("SELECT chat_id,cfg FROM im_chats WHERE enabled", many=True)
            for cid, cfg in chats or []:
                days = int((cfg or {}).get("marry_days") or 0)
                if not days:
                    continue
                await q("UPDATE im_marriage SET until=ts+make_interval(days=>%s) WHERE chat_id=%s AND active AND until IS NULL", (days, cid))
                soon = await q("UPDATE im_marriage SET warned=TRUE WHERE chat_id=%s AND active AND NOT warned AND until<now()+interval '3 days' RETURNING a,b,until", (cid,), many=True)
                for a, b, until in soon or []:
                    try:
                        await m._bot.send_message(cid, f"💍 {mention(a, await member_name(cid, a))} и {mention(b, await member_name(cid, b))}: брак истекает "
                                                       f"{until.astimezone(timezone(timedelta(hours=3))).strftime('%d.%m')}. Продлить: <code>брак продлить</code>", parse_mode="HTML")
                    except Exception:
                        pass
                gone = await q("UPDATE im_marriage SET active=FALSE WHERE chat_id=%s AND active AND until<now() RETURNING a,b", (cid,), many=True)
                for a, b in gone or []:
                    try:
                        await m._bot.send_message(cid, f"💔 Брак {mention(a, await member_name(cid, a))} и {mention(b, await member_name(cid, b))} истёк.", parse_mode="HTML")
                    except Exception:
                        pass
            # капча: просрочили — кик
            for cid, uid, mid in await q("UPDATE im_captcha SET done=TRUE WHERE NOT done AND due<now() RETURNING chat_id,user_id,msg_id", many=True) or []:
                try:
                    await m.do_kick(cid, uid, m._bot.id, "не прошёл капчу")
                    await m._bot.delete_message(cid, mid)
                except Exception:
                    pass
        except Exception:
            log.exception("ext worker")
        await asyncio.sleep(30)


def install(dp):
    dp.message.outer_middleware(PrivateGridMiddleware())
