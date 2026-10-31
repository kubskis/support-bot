# -*- coding: utf-8 -*-
"""Надзор, этап 2: экономика (монеты), ферма, браки, дуэли, рулетка, кубы.
Подключается из iris_mod.setup(); команды регистрируются в общей таблице iris_mod.CMDS."""
import asyncio
import logging
import random
import time
from datetime import datetime, timedelta, timezone

from aiogram import F
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

import iris_mod as m
from iris_mod import cmd, q, reply, mention, esc, resolve_target, member_name, get_cfg, set_cfg, fmt_dur, parse_dur

log = logging.getLogger("iris_eco")
COIN = "🪙"
START_COINS = 100
FARM_CD = 4 * 3600

m.DEFAULT_DK.update({
    "экономика": 0, "ферма": 0, "передать": 0, "брак": 0, "дуэль": 0, "рулетка": 0, "кубы": 0,
    "поженить": 4, "развести": 4, "эконастройка": 4, "выдать": 5,
})

_DDL = """
CREATE TABLE IF NOT EXISTS im_wallet(chat_id BIGINT, user_id BIGINT, coins BIGINT DEFAULT 100, farm_ts TIMESTAMPTZ, won INT DEFAULT 0, lost INT DEFAULT 0, PRIMARY KEY(chat_id,user_id));
CREATE TABLE IF NOT EXISTS im_marriage(id SERIAL PRIMARY KEY, chat_id BIGINT, a BIGINT, b BIGINT, ts TIMESTAMPTZ DEFAULT now(), active BOOLEAN DEFAULT TRUE);
CREATE INDEX IF NOT EXISTS im_marriage_ix ON im_marriage(chat_id,active);
CREATE TABLE IF NOT EXISTS im_duels(chat_id BIGINT, msg_id BIGINT, a BIGINT, b BIGINT, stake BIGINT, done BOOLEAN DEFAULT FALSE, PRIMARY KEY(chat_id,msg_id));
"""


async def init_tables():
    await q(_DDL)


# ---------------------------------------------------------------- кошелёк
async def balance(cid, uid):
    await q("INSERT INTO im_wallet(chat_id,user_id,coins) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING", (cid, uid, START_COINS))
    r = await q("SELECT coins FROM im_wallet WHERE chat_id=%s AND user_id=%s", (cid, uid), one=True)
    return int(r[0])


async def spend(cid, uid, n):
    """Атомарно списать n; True если хватило."""
    await balance(cid, uid)
    r = await q("UPDATE im_wallet SET coins=coins-%s WHERE chat_id=%s AND user_id=%s AND coins>=%s RETURNING coins", (n, cid, uid, n), one=True)
    return bool(r)


async def add(cid, uid, n):
    await balance(cid, uid)
    await q("UPDATE im_wallet SET coins=coins+%s WHERE chat_id=%s AND user_id=%s", (n, cid, uid))


def _num(tokens, default=0):
    for t in tokens:
        if t.isdigit():
            return int(t)
    return default


async def _off(msg, flag):
    cfg = await get_cfg(msg.chat.id)
    return bool(cfg.get(flag))


@cmd(["баланс", "мои монеты", "кошелёк", "кошелек", "монеты"], "экономика")
async def c_balance(msg, args, rank):
    if await _off(msg, "no_eco"):
        return
    uid, name, _ = await resolve_target(msg, args)
    if not uid:
        uid, name = msg.from_user.id, msg.from_user.full_name
    b = await balance(msg.chat.id, uid)
    w = await q("SELECT won,lost FROM im_wallet WHERE chat_id=%s AND user_id=%s", (msg.chat.id, uid), one=True)
    await reply(msg, f"{COIN} {mention(uid, name)}: <b>{b}</b>\nДуэли: побед {w[0]}, поражений {w[1]}")


@cmd(["ферма", "фарм"], "ферма")
async def c_farm(msg, args, rank):
    if await _off(msg, "no_eco"):
        return
    cid, uid = msg.chat.id, msg.from_user.id
    await balance(cid, uid)
    r = await q("SELECT farm_ts FROM im_wallet WHERE chat_id=%s AND user_id=%s", (cid, uid), one=True)
    now = datetime.now(timezone.utc)
    if r[0] and (now - r[0]).total_seconds() < FARM_CD:
        left = int(FARM_CD - (now - r[0]).total_seconds())
        return await reply(msg, f"⏳ Ферма отдыхает. Приходите через {left // 3600} ч {left % 3600 // 60} мин.")
    gain = random.randint(30, 80)
    await q("UPDATE im_wallet SET coins=coins+%s, farm_ts=now() WHERE chat_id=%s AND user_id=%s", (gain, cid, uid))
    await reply(msg, f"🌾 Ферма принесла <b>{gain}</b> {COIN}. Баланс: {await balance(cid, uid)}")


@cmd(["передать", "перевести"], "передать")
async def c_transfer(msg, args, rank):
    if await _off(msg, "no_eco"):
        return
    uid, name, rest = await resolve_target(msg, args)
    n = _num(rest)
    if not uid or n <= 0 or uid == msg.from_user.id:
        return await reply(msg, "Пример: ответом на сообщение <code>передать 50</code>")
    if not await spend(msg.chat.id, msg.from_user.id, n):
        return await reply(msg, "Недостаточно монет.")
    await add(msg.chat.id, uid, n)
    await m.logact(msg.chat.id, msg.from_user.id, "transfer", uid, str(n))
    await reply(msg, f"✅ {mention(msg.from_user.id, msg.from_user.full_name)} передал {mention(uid, name)}: <b>{n}</b> {COIN}")


@cmd(["!выдать"], "выдать")
async def c_give(msg, args, rank):
    uid, name, rest = await resolve_target(msg, args)
    n = _num(rest)
    if not uid or n <= 0:
        return await reply(msg, "Пример: <code>!выдать 500 @ник</code>")
    await add(msg.chat.id, uid, n)
    await m.logact(msg.chat.id, msg.from_user.id, "give", uid, str(n))
    await reply(msg, f"Выдано {mention(uid, name)}: {n} {COIN}")


@cmd(["топ богачей", "бтоп", "топ монет"], "экономика")
async def c_rich(msg, args, rank):
    rows = await q("SELECT user_id,coins FROM im_wallet WHERE chat_id=%s ORDER BY coins DESC LIMIT 15", (msg.chat.id,), many=True)
    if not rows:
        return await reply(msg, "Пока ни у кого нет монет.")
    out = [f"<b>Богатейшие {COIN}</b>"]
    for i, (u, c) in enumerate(rows, 1):
        out.append(f"{i}. {esc(await member_name(msg.chat.id, u))} — {c}")
    await reply(msg, "\n".join(out))


@cmd(["-экономика"], "эконастройка")
async def c_eco_off(msg, args, rank):
    await set_cfg(msg.chat.id, no_eco=True)
    await reply(msg, "Экономика отключена.")


@cmd(["+экономика"], "эконастройка")
async def c_eco_on(msg, args, rank):
    await set_cfg(msg.chat.id, no_eco=None)
    await reply(msg, "Экономика включена.")


# ---------------------------------------------------------------- азарт
async def _penalty(cid, uid, kind_cfg, by):
    """Применить исход проигрыша по настройке чата: 0 / мут / кик / бан."""
    cfg = await get_cfg(cid)
    pen = cfg.get(kind_cfg)
    if pen is None and kind_cfg == "roulette_pen":
        pen = {"k": "mute", "s": 300}
    if not pen or pen.get("k") in (None, "0"):
        return ""
    if await m.get_rank(cid, uid) > 0:
        return " (модератору наказание не применяется)"
    try:
        k, s = pen["k"], pen.get("s", 0)
        if k == "mute":
            await m.do_mute(cid, uid, s or 300, "игра", by)
            return f" 🔇 мут на {fmt_dur(s or 300)}"
        if k == "kick":
            await m.do_kick(cid, uid, by, "игра")
            return " 👢 исключён"
        if k == "ban":
            await m.do_ban(cid, uid, s, "игра", by)
            return f" 🔨 бан на {fmt_dur(s)}"
    except Exception as e:
        return " (⚠️ наказание не применено: " + m.human_error(e).split("💬 ", 1)[-1] + ")"
    return ""


def _parse_pen(args):
    if not args:
        return None
    k = {"0": "0", "нет": "0", "мут": "mute", "кик": "kick", "бан": "ban"}.get(args[0].lower())
    if not k:
        return None
    s = parse_dur(args[1]) if len(args) > 1 else 0
    return {"k": k, "s": s or 0}


@cmd(["!рулетка", "рулетка"], "рулетка")
async def c_roulette(msg, args, rank):
    if await _off(msg, "no_games"):
        return
    uid = msg.from_user.id
    if random.randint(1, 6) == 1:
        extra = await _penalty(msg.chat.id, uid, "roulette_pen", m._bot.id)
        await reply(msg, f"💥 Выстрел! {mention(uid, msg.from_user.full_name)} не повезло.{extra}")
    else:
        await reply(msg, f"🔫 Щёлк… {mention(uid, msg.from_user.full_name)} живёт дальше.")


@cmd(["рулетка исход"], "эконастройка")
async def c_roulette_pen(msg, args, rank):
    p = _parse_pen(args)
    if not p:
        return await reply(msg, "Пример: <code>рулетка исход мут 10м</code> (0 / мут / кик / бан)")
    await set_cfg(msg.chat.id, roulette_pen=p)
    await reply(msg, "Исход рулетки сохранён.")


@cmd(["кубы"], "кубы")
async def c_dice(msg, args, rank):
    if await _off(msg, "no_games") or await _off(msg, "no_eco"):
        return
    cid, uid = msg.chat.id, msg.from_user.id
    n = _num(args, 10)
    if n <= 0 or n > 10000:
        return await reply(msg, "Ставка от 1 до 10000.")
    if not await spend(cid, uid, n):
        return await reply(msg, "Недостаточно монет.")
    d1, d2 = random.randint(1, 6), random.randint(1, 6)
    s = d1 + d2
    if s >= 9:
        await add(cid, uid, n * 2)
        res = f"победа! +{n} {COIN}"
    elif s == 7 or s == 8:
        await add(cid, uid, n)
        res = "ничья, ставка возвращена"
    else:
        res = f"проигрыш, −{n} {COIN}"
    await reply(msg, f"🎲 {d1} + {d2} = <b>{s}</b> — {res}\nБаланс: {await balance(cid, uid)}")


# ---------------------------------------------------------------- дуэли
@cmd(["дуэль", "золотая дуэль"], "дуэль")
async def c_duel(msg, args, rank):
    if await _off(msg, "no_games"):
        return
    cid = msg.chat.id
    uid, name, rest = await resolve_target(msg, args)
    if not uid or uid == msg.from_user.id or uid == m._bot.id:
        return await reply(msg, "Вызвать на дуэль: ответом на сообщение, <code>дуэль [ставка]</code>")
    stake = _num(rest, 0)
    if stake and await _off(msg, "no_eco"):
        stake = 0
    if stake and await balance(cid, msg.from_user.id) < stake:
        return await reply(msg, "У вас не хватает монет на такую ставку.")
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⚔️ Принять", callback_data="imd:y"),
                                                InlineKeyboardButton(text="🏳️ Отказ", callback_data="imd:n")]])
    sent = await reply(msg, f"⚔️ {mention(msg.from_user.id, msg.from_user.full_name)} вызывает {mention(uid, name)} на дуэль"
                       + (f" на <b>{stake}</b> {COIN}" if stake else "") + "!", reply_markup=kb)
    await q("INSERT INTO im_duels(chat_id,msg_id,a,b,stake) VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING", (cid, sent.message_id, msg.from_user.id, uid, stake))


@m.router.callback_query(F.data.in_({"imd:y", "imd:n"}))
async def cb_duel(cb: CallbackQuery):
    cid, mid = cb.message.chat.id, cb.message.message_id
    r = await q("SELECT a,b,stake,done FROM im_duels WHERE chat_id=%s AND msg_id=%s", (cid, mid), one=True)
    if not r or r[3]:
        return await cb.answer("Дуэль уже завершена.")
    a, b, stake, _ = r
    if cb.from_user.id == a and cb.data == "imd:n":
        await q("UPDATE im_duels SET done=TRUE WHERE chat_id=%s AND msg_id=%s", (cid, mid))
        await cb.message.edit_text("Вызов отозван.")
        return await cb.answer()
    if cb.from_user.id != b:
        return await cb.answer("Это не ваш вызов.", show_alert=True)
    await q("UPDATE im_duels SET done=TRUE WHERE chat_id=%s AND msg_id=%s", (cid, mid))
    if cb.data == "imd:n":
        await cb.message.edit_text(f"🏳️ {mention(b, await member_name(cid, b))} отказался от дуэли.", parse_mode="HTML")
        return await cb.answer()
    if stake:
        if not await spend(cid, a, stake):
            await cb.message.edit_text("У вызвавшего не хватает монет — дуэль отменена.")
            return await cb.answer()
        if not await spend(cid, b, stake):
            await add(cid, a, stake)
            await cb.message.edit_text("У принявшего не хватает монет — дуэль отменена.")
            return await cb.answer()
    win, lose = random.sample([a, b], 2)
    if stake:
        await add(cid, win, stake * 2)
    await q("UPDATE im_wallet SET won=won+1 WHERE chat_id=%s AND user_id=%s", (cid, win))
    await q("UPDATE im_wallet SET lost=lost+1 WHERE chat_id=%s AND user_id=%s", (cid, lose))
    extra = await _penalty(cid, lose, "duel_pen", m._bot.id)
    await cb.message.edit_text(f"⚔️ Победил {mention(win, await member_name(cid, win))}!\n"
                               f"Проиграл {mention(lose, await member_name(cid, lose))}.{extra}"
                               + (f"\nВыигрыш: <b>{stake * 2}</b> {COIN}" if stake else ""), parse_mode="HTML")
    await cb.answer()


@cmd(["дуэль исход"], "эконастройка")
async def c_duel_pen(msg, args, rank):
    p = _parse_pen(args)
    if not p:
        return await reply(msg, "Пример: <code>дуэль исход мут 30м</code> (0 / мут / кик / бан)")
    await set_cfg(msg.chat.id, duel_pen=p)
    await reply(msg, "Исход дуэли для проигравшего сохранён.")


@cmd(["-игры"], "эконастройка")
async def c_games_off(msg, args, rank):
    await set_cfg(msg.chat.id, no_games=True)
    await reply(msg, "Игры (дуэли, рулетка, кубы) отключены.")


@cmd(["+игры"], "эконастройка")
async def c_games_on(msg, args, rank):
    await set_cfg(msg.chat.id, no_games=None)
    await reply(msg, "Игры включены.")


# ---------------------------------------------------------------- браки
async def _spouse(cid, uid):
    return await q("SELECT id,a,b,ts FROM im_marriage WHERE chat_id=%s AND active AND (a=%s OR b=%s)", (cid, uid, uid), one=True)


@cmd(["брак"], "брак")
async def c_marry(msg, args, rank):
    if await _off(msg, "no_marry"):
        return
    cid = msg.chat.id
    uid, name, _ = await resolve_target(msg, args)
    me = msg.from_user.id
    if not uid or uid == me or uid == m._bot.id:
        return await reply(msg, "Предложить брак: ответом на сообщение, <code>брак</code>")
    if await _spouse(cid, me):
        return await reply(msg, "Вы уже в браке. Сначала <code>развод</code>.")
    if await _spouse(cid, uid):
        return await reply(msg, "Этот участник уже состоит в браке.")
    price = int((await get_cfg(cid)).get("marry_price", 0))
    if price and await balance(cid, me) < price:
        return await reply(msg, f"Для брака нужно {price} {COIN}.")
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💍 Да", callback_data=f"imm:y:{me}:{uid}"),
                                                InlineKeyboardButton(text="💔 Нет", callback_data=f"imm:n:{me}:{uid}")]])
    await reply(msg, f"💍 {mention(me, msg.from_user.full_name)} предлагает брак {mention(uid, name)}!"
                + (f"\nСтоимость: {price} {COIN}" if price else ""), reply_markup=kb)


@m.router.callback_query(F.data.startswith("imm:"))
async def cb_marry(cb: CallbackQuery):
    _, ans, a, b = cb.data.split(":")
    a, b, cid = int(a), int(b), cb.message.chat.id
    if cb.from_user.id != b:
        return await cb.answer("Это предложение не вам.", show_alert=True)
    if ans == "n":
        await cb.message.edit_text(f"💔 {mention(b, await member_name(cid, b))} отказал(а).", parse_mode="HTML")
        return await cb.answer()
    if await _spouse(cid, a) or await _spouse(cid, b):
        await cb.message.edit_text("Кто-то из пары уже в браке.")
        return await cb.answer()
    price = int((await get_cfg(cid)).get("marry_price", 0))
    if price and not await spend(cid, a, price):
        await cb.message.edit_text("У предлагавшего не хватает монет.")
        return await cb.answer()
    await q("INSERT INTO im_marriage(chat_id,a,b) VALUES(%s,%s,%s)", (cid, a, b))
    await cb.message.edit_text(f"💒 {mention(a, await member_name(cid, a))} и {mention(b, await member_name(cid, b))} теперь в браке! Совет да любовь!", parse_mode="HTML")
    await cb.answer()


@cmd(["развод", "!развод"], "брак")
async def c_divorce(msg, args, rank):
    r = await _spouse(msg.chat.id, msg.from_user.id)
    if not r:
        return await reply(msg, "Вы не в браке.")
    await q("UPDATE im_marriage SET active=FALSE WHERE id=%s", (r[0],))
    await reply(msg, "💔 Брак расторгнут.")


@cmd(["мой брак"], "брак")
async def c_mymarry(msg, args, rank):
    uid, name, _ = await resolve_target(msg, args)
    if not uid:
        uid = msg.from_user.id
    r = await _spouse(msg.chat.id, uid)
    if not r:
        return await reply(msg, "Не состоит в браке.")
    other = r[2] if r[1] == uid else r[1]
    days = (datetime.now(timezone.utc) - r[3]).days
    await reply(msg, f"💞 {mention(uid, await member_name(msg.chat.id, uid))} + {mention(other, await member_name(msg.chat.id, other))}\nВместе: {days} дн.")


@cmd(["браки"], "брак")
async def c_marriages(msg, args, rank):
    rows = await q("SELECT a,b,ts FROM im_marriage WHERE chat_id=%s AND active ORDER BY ts LIMIT 20", (msg.chat.id,), many=True)
    if not rows:
        return await reply(msg, "Браков пока нет.")
    out = ["<b>Браки</b>"]
    for a, b, ts in rows:
        out.append(f"💍 {esc(await member_name(msg.chat.id, a))} + {esc(await member_name(msg.chat.id, b))} — {(datetime.now(timezone.utc) - ts).days} дн.")
    await reply(msg, "\n".join(out))


@cmd(["топ браков"], "брак")
async def c_top_marry(msg, args, rank):
    return await c_marriages(msg, args, rank)


@cmd(["поженить пару", "поженить"], "поженить")
async def c_force_marry(msg, args, rank):
    if not msg.reply_to_message or not msg.reply_to_message.from_user:
        return await reply(msg, "Ответьте на сообщение одного и укажите второго: <code>поженить @ник</code>")
    a = msg.reply_to_message.from_user
    b_id, b_name, _ = await resolve_target(type("M", (), {"chat": msg.chat, "reply_to_message": None, "entities": msg.entities})(), args)
    if not b_id or b_id == a.id:
        return await reply(msg, "Укажите второго участника.")
    if await _spouse(msg.chat.id, a.id) or await _spouse(msg.chat.id, b_id):
        return await reply(msg, "Кто-то из пары уже в браке.")
    await q("INSERT INTO im_marriage(chat_id,a,b) VALUES(%s,%s,%s)", (msg.chat.id, a.id, b_id))
    await reply(msg, f"💒 {mention(a.id, a.full_name)} и {mention(b_id, b_name)} поженены!")


@cmd(["развести пару", "развести"], "развести")
async def c_force_divorce(msg, args, rank):
    uid, name, _ = await resolve_target(msg, args)
    r = await _spouse(msg.chat.id, uid) if uid else None
    if not r:
        return await reply(msg, "Брак не найден.")
    await q("UPDATE im_marriage SET active=FALSE WHERE id=%s", (r[0],))
    await reply(msg, "💔 Пара разведена.")


@cmd(["брак цена"], "эконастройка")
async def c_marry_price(msg, args, rank):
    if not args or not args[0].isdigit():
        return await reply(msg, "Пример: <code>брак цена 100</code> (0 — бесплатно)")
    await set_cfg(msg.chat.id, marry_price=int(args[0]) or None)
    await reply(msg, f"Цена брака: {args[0]} {COIN}")


@cmd(["-браки"], "эконастройка")
async def c_marry_off(msg, args, rank):
    await set_cfg(msg.chat.id, no_marry=True)
    await reply(msg, "Браки отключены.")


@cmd(["+браки"], "эконастройка")
async def c_marry_on(msg, args, rank):
    await set_cfg(msg.chat.id, no_marry=None)
    await reply(msg, "Браки включены.")


@cmd(["!сброс браков"], "сброс")
async def c_marry_reset(msg, args, rank):
    await q("UPDATE im_marriage SET active=FALSE WHERE chat_id=%s", (msg.chat.id,))
    await reply(msg, "Все браки сброшены.")
