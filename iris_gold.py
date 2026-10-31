# -*- coding: utf-8 -*-
"""Надзор, этап B: VIP и эксклюзивные РП, золото за Telegram Stars, золотая дуэль, биржа золота,
бонусы (анонимка, банхаммер)."""
import asyncio
import logging
import math
import os
import random
from datetime import datetime, timedelta, timezone
from io import BytesIO

from aiogram import BaseMiddleware, F
from aiogram.types import (BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice,
                           Message, PreCheckoutQuery)

import iris_mod as m
from iris_mod import cmd, q, reply, mention, esc, resolve_target, member_name, get_cfg, set_cfg, fmt_dur

log = logging.getLogger("iris_gold")
GOLD = "🥇"
COIN = "🪙"
_MSK = timezone(timedelta(hours=3))
# звёзды -> золото (бонус за крупные пакеты)
GOLD_PACKS = {10: 10, 50: 55, 100: 115, 500: 600}
DUEL_FEE = 0.05

m.DEFAULT_DK.update({"вип": 0, "випнастройка": 4, "випрп": 0, "золото": 0, "золотаядуэль": 0, "биржа": 0, "анонимка": 0, "банхаммер": 0})

_DDL = """
CREATE TABLE IF NOT EXISTS im_vip(chat_id BIGINT, user_id BIGINT, until TIMESTAMPTZ, PRIMARY KEY(chat_id,user_id));
CREATE TABLE IF NOT EXISTS im_gold(user_id BIGINT PRIMARY KEY, gold BIGINT DEFAULT 0);
CREATE TABLE IF NOT EXISTS im_gold_pay(charge_id TEXT PRIMARY KEY, user_id BIGINT, stars INT, gold INT, ts TIMESTAMPTZ DEFAULT now());
CREATE TABLE IF NOT EXISTS im_gduels(chat_id BIGINT, msg_id BIGINT, a BIGINT, b BIGINT, stake BIGINT, done BOOLEAN DEFAULT FALSE, PRIMARY KEY(chat_id,msg_id));
CREATE TABLE IF NOT EXISTS im_market(id INT PRIMARY KEY, price DOUBLE PRECISION, pool BIGINT DEFAULT 0);
CREATE TABLE IF NOT EXISTS im_gold_hist(ts TIMESTAMPTZ DEFAULT now(), price DOUBLE PRECISION);
INSERT INTO im_market(id,price,pool) VALUES(1,100,0) ON CONFLICT DO NOTHING;
"""


async def init_tables():
    await q(_DDL)


# ---------------------------------------------------------------- золото
async def gold_bal(uid):
    r = await q("SELECT gold FROM im_gold WHERE user_id=%s", (uid,), one=True)
    return int(r[0]) if r else 0


async def gold_add(uid, n):
    await q("INSERT INTO im_gold(user_id,gold) VALUES(%s,%s) ON CONFLICT(user_id) DO UPDATE SET gold=im_gold.gold+%s", (uid, n, n))


async def gold_spend(uid, n):
    r = await q("UPDATE im_gold SET gold=gold-%s WHERE user_id=%s AND gold>=%s RETURNING gold", (n, uid, n), one=True)
    return bool(r)


@cmd(["золото", "моё золото", "мое золото"], "золото")
async def c_gold(msg, args, rank):
    uid, name, _ = await resolve_target(msg, args)
    if not uid:
        uid, name = msg.from_user.id, msg.from_user.full_name
    await reply(msg, f"{GOLD} {mention(uid, name)}: <b>{await gold_bal(uid)}</b> золота\n"
                     "Пополнить за Telegram Stars: <code>пополнить золото</code>. Обмен на монеты: <code>курс</code>")


_bot_username = None


@cmd(["пополнить золото", "купить золото за звезды", "купить золото за звёзды", "донат золото"], "золото")
async def c_gold_buy(msg, args, rank):
    global _bot_username
    if not _bot_username:
        _bot_username = (await m._bot.get_me()).username
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⭐ Купить золото в ЛС бота", url=f"https://t.me/{_bot_username}?start=gold")]])
    await reply(msg, f"{GOLD} Золото покупается за Telegram Stars в личных сообщениях с ботом (оплата безопасная, через Telegram).", reply_markup=kb)


@cmd(["передать золото"], "золото")
async def c_gold_give(msg, args, rank):
    uid, name, rest = await resolve_target(msg, args)
    n = next((int(t) for t in rest if t.isdigit()), 0)
    if not uid or n <= 0 or uid == msg.from_user.id:
        return await reply(msg, "Пример: ответом <code>передать золото 5</code>")
    if not await gold_spend(msg.from_user.id, n):
        return await reply(msg, "Недостаточно золота.")
    await gold_add(uid, n)
    await m.logact(msg.chat.id, msg.from_user.id, "gold_give", uid, str(n))
    await reply(msg, f"{GOLD} {mention(msg.from_user.id, msg.from_user.full_name)} передал {mention(uid, name)}: <b>{n}</b>")


class GoldPayMiddleware(BaseMiddleware):
    """Перехватывает ЛС-сценарии золота и платежи с payload «imgold:…», не мешая магазину и донатам."""

    async def __call__(self, handler, event, data):
        try:
            if isinstance(event, PreCheckoutQuery) and (event.invoice_payload or "").startswith("imgold:"):
                await self._pre(event)
                return None
            if isinstance(event, Message) and event.successful_payment and (event.successful_payment.invoice_payload or "").startswith("imgold:"):
                await self._paid(event)
                return None
            if isinstance(event, Message) and event.chat.type == "private" and (event.text or "").strip().lower() in ("/start gold", "/gold", "золото"):
                await self._menu(event)
                return None
        except Exception:
            log.exception("gold middleware")
        return await handler(event, data)

    async def _menu(self, msg):
        rows = [[InlineKeyboardButton(text=f"⭐ {stars} → {gold} {GOLD}", callback_data=f"imgp:{stars}")] for stars, gold in GOLD_PACKS.items()]
        await msg.answer(f"{GOLD} <b>Золото</b>\nВаш баланс: <b>{await gold_bal(msg.from_user.id)}</b>\n\nВыберите пакет — оплата Telegram Stars. "
                         "Золото общее для всех чатов с «Надзором»: ставки в золотой дуэли и обмен на монеты по курсу.",
                         parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

    async def _pre(self, q_):
        try:
            _, stars, uid = q_.invoice_payload.split(":")
            ok = q_.currency == "XTR" and int(stars) in GOLD_PACKS and int(stars) == int(q_.total_amount) and int(uid) == q_.from_user.id
        except Exception:
            ok = False
        await q_.answer(ok=ok, error_message=None if ok else "Счёт недействителен. Откройте меню золота заново.")

    async def _paid(self, msg):
        sp = msg.successful_payment
        _, stars, uid = sp.invoice_payload.split(":")
        stars, uid = int(stars), int(uid)
        gold = GOLD_PACKS.get(stars)
        if not gold or uid != msg.from_user.id:
            log.error("gold payment mismatch: %s", sp.invoice_payload)
            return
        r = await q("INSERT INTO im_gold_pay(charge_id,user_id,stars,gold) VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING 1",
                    (sp.telegram_payment_charge_id, uid, stars, gold), one=True)
        if r:
            await gold_add(uid, gold)
        await msg.answer(f"✅ Оплата получена: +{gold} {GOLD}. Баланс: {await gold_bal(uid)}")


@m.router.callback_query(F.data.startswith("imgp:"))
async def cb_gold_pack(cb: CallbackQuery):
    stars = int(cb.data.split(":")[1])
    gold = GOLD_PACKS.get(stars)
    if not gold:
        return await cb.answer("Пакет не найден.", show_alert=True)
    await m._bot.send_invoice(cb.from_user.id, title=f"{gold} золота", description=f"{gold} {GOLD} для золотых дуэлей и биржи «Надзора»",
                              payload=f"imgold:{stars}:{cb.from_user.id}", currency="XTR", prices=[LabeledPrice(label=f"{gold} золота", amount=stars)], provider_token="")
    await cb.answer()


# ---------------------------------------------------------------- золотая дуэль
@cmd(["золотая дуэль"], "золотаядуэль")
async def c_gduel(msg, args, rank):
    cfg = await get_cfg(msg.chat.id)
    if cfg.get("no_games"):
        return
    uid, name, rest = await resolve_target(msg, args)
    stake = next((int(t) for t in rest if t.isdigit()), 1)
    if not uid or uid == msg.from_user.id or uid == m._bot.id or not (1 <= stake <= 10000):
        return await reply(msg, "Пример: ответом <code>золотая дуэль 5</code> (ставка в золоте, 1–10000)")
    if await gold_bal(msg.from_user.id) < stake:
        return await reply(msg, "У вас не хватает золота.")
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⚔️ Принять", callback_data="imgd:y"), InlineKeyboardButton(text="🏳️ Отказ", callback_data="imgd:n")]])
    sent = await reply(msg, f"{GOLD}⚔️ {mention(msg.from_user.id, msg.from_user.full_name)} вызывает {mention(uid, name)} на золотую дуэль: ставка <b>{stake}</b> {GOLD}!", reply_markup=kb)
    await q("INSERT INTO im_gduels(chat_id,msg_id,a,b,stake) VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING", (msg.chat.id, sent.message_id, msg.from_user.id, uid, stake))


@m.router.callback_query(F.data.in_({"imgd:y", "imgd:n"}))
async def cb_gduel(cb: CallbackQuery):
    cid, mid = cb.message.chat.id, cb.message.message_id
    r = await q("UPDATE im_gduels SET done=TRUE WHERE chat_id=%s AND msg_id=%s AND NOT done RETURNING a,b,stake", (cid, mid), one=True)
    if not r:
        return await cb.answer("Дуэль уже завершена.")
    a, b, stake = r
    if cb.data == "imgd:n" and cb.from_user.id == a:
        await cb.message.edit_text("Вызов отозван.")
        return await cb.answer()
    if cb.from_user.id != b:
        await q("UPDATE im_gduels SET done=FALSE WHERE chat_id=%s AND msg_id=%s", (cid, mid))
        return await cb.answer("Это не ваш вызов.", show_alert=True)
    if cb.data == "imgd:n":
        await cb.message.edit_text(f"🏳️ {mention(b, await member_name(cid, b))} отказался от золотой дуэли.", parse_mode="HTML")
        return await cb.answer()
    if not await gold_spend(a, stake):
        await cb.message.edit_text("У вызвавшего не хватает золота — дуэль отменена.")
        return await cb.answer()
    if not await gold_spend(b, stake):
        await gold_add(a, stake)
        await cb.message.edit_text("У принявшего не хватает золота — дуэль отменена.")
        return await cb.answer()
    win, lose = random.sample([a, b], 2)
    prize = int(stake * 2 * (1 - DUEL_FEE))
    await gold_add(win, prize)
    await m.logact(cid, win, "gold_duel", lose, str(stake))
    await cb.message.edit_text(f"{GOLD}⚔️ Победил {mention(win, await member_name(cid, win))}! Выигрыш: <b>{prize}</b> {GOLD} (комиссия {int(DUEL_FEE * 100)}%).\n"
                               f"Проиграл {mention(lose, await member_name(cid, lose))}.", parse_mode="HTML")
    await cb.answer()


# ---------------------------------------------------------------- биржа золота
async def market():
    r = await q("SELECT price,pool FROM im_market WHERE id=1", one=True)
    return float(r[0]), int(r[1])


def render_line(values, labels, title, subtitle=""):
    from PIL import Image, ImageDraw
    import iris_card
    S = 2
    W, H = 1100 * S, 540 * S
    img = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(img)
    for y in range(H):
        d.line([(0, y), (W, y)], fill=iris_card._lerp((17, 20, 40), (30, 27, 62), y / H))
    d.text((48 * S, 34 * S), title, fill=(255, 255, 255), font=iris_card._font(34 * S, True))
    if subtitle:
        d.text((48 * S, 86 * S), subtitle, fill=(160, 168, 200), font=iris_card._font(19 * S))
    L, R, T, B = 96 * S, 44 * S, 150 * S, 92 * S
    x0, x1, y0, y1 = L, W - R, T, H - B
    lo, hi = min(values), max(values)
    pad = max((hi - lo) * 0.15, 1)
    lo, hi = lo - pad, hi + pad
    gl = iris_card._font(16 * S)
    for g in range(5):
        y = y1 - (y1 - y0) * g / 4
        d.line([(x0, y), (x1, y)], fill=(48, 53, 86), width=S)
        txt = f"{lo + (hi - lo) * g / 4:.0f}"
        d.text((x0 - 14 * S - d.textlength(txt, font=gl), y - 10 * S), txt, fill=(122, 131, 168), font=gl)
    n = len(values)
    pts = [(x0 + (x1 - x0) * i / max(n - 1, 1), y1 - (y1 - y0) * (v - lo) / (hi - lo)) for i, v in enumerate(values)]
    over = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    od = ImageDraw.Draw(over)
    od.polygon(pts + [(x1, y1), (x0, y1)], fill=(80, 150, 255, 60))
    img.paste(over, (0, 0), over)
    d = ImageDraw.Draw(img)
    d.line(pts, fill=(90, 205, 255), width=4 * S, joint="curve")
    lx, ly = pts[-1]
    d.ellipse([lx - 8 * S, ly - 8 * S, lx + 8 * S, ly + 8 * S], fill=(255, 170, 90), outline=(255, 255, 255), width=2 * S)
    for i in range(0, n, max(1, n // 6)):
        lab = labels[i]
        d.text((pts[i][0] - d.textlength(lab, font=gl) / 2, y1 + 16 * S), lab, fill=(150, 158, 196), font=gl)
    out = img.resize((W // S, H // S), Image.LANCZOS)
    buf = BytesIO()
    out.save(buf, "PNG", optimize=True)
    return buf.getvalue()


@cmd(["курс", "курс золота", "биржа"], "биржа")
async def c_rate(msg, args, rank):
    price, pool = await market()
    rows = await q("SELECT ts,price FROM im_gold_hist WHERE ts>now()-interval '48 hours' ORDER BY ts", many=True)
    day_ago = await q("SELECT price FROM im_gold_hist WHERE ts<now()-interval '24 hours' ORDER BY ts DESC LIMIT 1", one=True)
    ch = ""
    if day_ago and day_ago[0]:
        pct = (price / float(day_ago[0]) - 1) * 100
        ch = f" ({'+' if pct >= 0 else ''}{pct:.1f}% за сутки)"
    text = (f"📈 <b>Биржа золота</b>\nКурс: <b>{price:.1f}</b> {COIN} за 1 {GOLD}{ch}\nНа рынке: {pool} {GOLD}\n"
            "Купить: <code>биржа купить N</code> · Продать: <code>биржа продать N</code>")
    vals = [float(p) for _, p in rows] + [price]
    labs = [t.astimezone(_MSK).strftime("%d.%m %H:%M") for t, _ in rows] + [datetime.now(_MSK).strftime("%d.%m %H:%M")]
    if len(vals) >= 3:
        try:
            png = await asyncio.to_thread(render_line, vals, labs, "Курс золота, монет за 1", f"Сейчас {price:.1f}{ch}")
            return await msg.reply_photo(BufferedInputFile(png, "rate.png"), caption=text, parse_mode="HTML")
        except Exception:
            log.exception("rate chart")
    await reply(msg, text)


async def _impact(delta):
    await q("UPDATE im_market SET price=GREATEST(10, LEAST(5000, price*(1+%s))) WHERE id=1", (delta,))


@cmd(["биржа продать"], "биржа")
async def c_sell(msg, args, rank):
    import iris_eco
    n = next((int(t) for t in args if t.isdigit()), 0)
    if n <= 0:
        return await reply(msg, "Пример: <code>биржа продать 5</code>")
    price, _ = await market()
    if not await gold_spend(msg.from_user.id, n):
        return await reply(msg, "Недостаточно золота.")
    got = int(n * price * 0.98)
    await iris_eco.add(msg.chat.id, msg.from_user.id, got)
    await q("UPDATE im_market SET pool=pool+%s WHERE id=1", (n,))
    await _impact(-min(0.001 * n, 0.05))
    await reply(msg, f"📉 Продано {n} {GOLD} по {price:.1f}: получено <b>{got}</b> {COIN} (комиссия 2%).")


@cmd(["биржа купить"], "биржа")
async def c_buy(msg, args, rank):
    import iris_eco
    n = next((int(t) for t in args if t.isdigit()), 0)
    if n <= 0:
        return await reply(msg, "Пример: <code>биржа купить 5</code>")
    price, pool = await market()
    if pool < n:
        return await reply(msg, f"На рынке только {pool} {GOLD}. Золото попадает на рынок, когда игроки его продают.")
    cost = int(math.ceil(n * price * 1.02))
    if not await iris_eco.spend(msg.chat.id, msg.from_user.id, cost):
        return await reply(msg, f"Нужно {cost} {COIN}.")
    r = await q("UPDATE im_market SET pool=pool-%s WHERE id=1 AND pool>=%s RETURNING pool", (n, n), one=True)
    if not r:
        await iris_eco.add(msg.chat.id, msg.from_user.id, cost)
        return await reply(msg, "Золото уже раскупили.")
    await gold_add(msg.from_user.id, n)
    await _impact(min(0.001 * n, 0.05))
    await reply(msg, f"📈 Куплено {n} {GOLD} по {price:.1f}: списано <b>{cost}</b> {COIN} (комиссия 2%).")


# ---------------------------------------------------------------- VIP
async def vip_until(cid, uid):
    r = await q("SELECT until FROM im_vip WHERE chat_id=%s AND user_id=%s", (cid, uid), one=True)
    if not r:
        return None
    if r[0] is None:
        return "forever"
    return r[0] if r[0] > datetime.now(timezone.utc) else None


async def is_vip(cid, uid):
    return (await vip_until(cid, uid)) is not None


async def _grant_vip(cid, uid, days):
    cur = await vip_until(cid, uid)
    if days == 0 or cur == "forever":
        until = None
    else:
        base = cur if cur else datetime.now(timezone.utc)
        until = base + timedelta(days=days)
    await q("INSERT INTO im_vip(chat_id,user_id,until) VALUES(%s,%s,%s) ON CONFLICT(chat_id,user_id) DO UPDATE SET until=EXCLUDED.until", (cid, uid, until))
    return until


@cmd(["вип цена"], "випнастройка")
async def c_vip_price(msg, args, rank):
    if not args or not args[0].isdigit():
        return await reply(msg, "Пример: <code>вип цена 500</code> (10–10000 монет; 0 — продажа выключена)")
    n = int(args[0])
    if n and not (10 <= n <= 10000):
        return await reply(msg, "Цена VIP — от 10 до 10000 монет.")
    await set_cfg(msg.chat.id, vip_price=n or None)
    await reply(msg, f"Цена VIP: {n} {COIN}" if n else "Продажа VIP выключена.")


@cmd(["вип срок"], "випнастройка")
async def c_vip_term(msg, args, rank):
    if not args or not args[0].isdigit():
        return await reply(msg, "Пример: <code>вип срок 30</code> (дней; 0 — навсегда)")
    await set_cfg(msg.chat.id, vip_days=int(args[0]))
    await reply(msg, f"Срок покупки VIP: {args[0]} дн." if int(args[0]) else "VIP покупается навсегда.")


@cmd(["вип рп цена"], "випнастройка")
async def c_vip_rp_price(msg, args, rank):
    if not args or not args[0].isdigit():
        return await reply(msg, "Пример: <code>вип рп цена 20</code> — не-VIP смогут использовать эксклюзивные РП за эту цену (0 — только VIP)")
    await set_cfg(msg.chat.id, vip_rp_price=int(args[0]) or None)
    await reply(msg, f"Эксклюзивные РП для не-VIP: {args[0]} {COIN} за использование." if int(args[0]) else "Эксклюзивные РП — только для VIP.")


@cmd(["купить вип"], "вип")
async def c_vip_buy(msg, args, rank):
    import iris_eco
    cfg = await get_cfg(msg.chat.id)
    price = int(cfg.get("vip_price") or 0)
    if not price:
        return await reply(msg, "Продажа VIP в этом чате не включена. Модератор: <code>вип цена N</code>")
    days = int(cfg.get("vip_days", 30))
    if not await iris_eco.spend(msg.chat.id, msg.from_user.id, price):
        return await reply(msg, f"VIP стоит {price} {COIN}, у вас не хватает.")
    until = await _grant_vip(msg.chat.id, msg.from_user.id, days)
    await reply(msg, f"💎 VIP куплен {'навсегда' if not until else 'до ' + until.astimezone(_MSK).strftime('%d.%m.%Y')}!")


@cmd(["+вип"], "випнастройка")
async def c_vip_give(msg, args, rank):
    uid, name, rest = await resolve_target(msg, args)
    if not uid:
        return await reply(msg, "Пример: <code>+вип @ник 30</code> (дней; без числа — навсегда)")
    days = next((int(t) for t in rest if t.isdigit()), 0)
    until = await _grant_vip(msg.chat.id, uid, days)
    await reply(msg, f"💎 {mention(uid, name)} получил VIP {'навсегда' if not until else 'до ' + until.astimezone(_MSK).strftime('%d.%m.%Y')}.")


@cmd(["-вип"], "випнастройка")
async def c_vip_take(msg, args, rank):
    uid, name, _ = await resolve_target(msg, args)
    if not uid:
        return await reply(msg, "Укажите участника.")
    await q("DELETE FROM im_vip WHERE chat_id=%s AND user_id=%s", (msg.chat.id, uid))
    await reply(msg, f"VIP снят с {mention(uid, name)}.")


@cmd(["вип 0"], "вип")
async def c_vip_off(msg, args, rank):
    await q("DELETE FROM im_vip WHERE chat_id=%s AND user_id=%s", (msg.chat.id, msg.from_user.id))
    await reply(msg, "Ваш VIP отключён.")


@cmd(["кто вип"], "вип")
async def c_vip_list(msg, args, rank):
    rows = await q("SELECT user_id,until FROM im_vip WHERE chat_id=%s AND (until IS NULL OR until>now()) ORDER BY until NULLS LAST LIMIT 50", (msg.chat.id,), many=True)
    if not rows:
        return await reply(msg, "VIP-участников нет.")
    await reply(msg, "💎 <b>VIP чата</b>\n" + "\n".join(f"{esc(await member_name(msg.chat.id, u))} — {'навсегда' if t is None else 'до ' + t.astimezone(_MSK).strftime('%d.%m.%Y')}" for u, t in rows))


@cmd(["вип"], "вип")
async def c_vip_info(msg, args, rank):
    cfg = await get_cfg(msg.chat.id)
    cur = await vip_until(msg.chat.id, msg.from_user.id)
    price = cfg.get("vip_price")
    s = "💎 У вас " + ("VIP навсегда" if cur == "forever" else f"VIP до {cur.astimezone(_MSK).strftime('%d.%m.%Y')}") if cur else "У вас нет VIP."
    s += f"\nЦена: {price} {COIN}" + (f", срок {cfg.get('vip_days', 30)} дн." if cfg.get('vip_days', 30) else ", навсегда") if price else "\nПродажа VIP в чате выключена."
    s += "\nVIP открывает эксклюзивные РП (<code>рп вип</code>) и значок в «кто я»."
    await reply(msg, s)


# ---------------------------------------------------------------- эксклюзивные РП
_VIP_RP = {
    "связать": "связал(а)", "заставить": "заставил(а)", "повесить": "повесил(а) на люстру", "уничтожить": "уничтожил(а)", "продать": "продал(а) на рынке",
    "щекотать": "щекочет", "взорвать": "взорвал(а)", "шмальнуть": "шмальнул(а) в", "засосать": "поставил(а) засос", "лечь": "лёг(ла) рядом с",
    "унизить": "унизил(а)", "арестовать": "арестовал(а)", "наорать": "наорал(а) на", "рассмешить": "рассмешил(а)", "ушатать": "ушатал(а)",
    "порвать": "порвал(а)", "отрубить": "отрубил(а)", "отстрелить": "отстрелил(а) руку", "выкопать": "выкопал(а) яму для", "выпороть": "выпорол(а)",
    "закопать": "закопал(а)", "выпить": "выпил(а) с", "наказать": "наказал(а)", "предложить": "предложил(а) руку и сердце",
}


def _mk_vip_rp(name, verb):
    @cmd([name], "випрп")
    async def _h(msg, args, rank, verb=verb):
        cfg = await get_cfg(msg.chat.id)
        if cfg.get("rp_off"):
            return
        uid, tname, _ = await resolve_target(msg, args)
        if not uid:
            return
        me = msg.from_user.id
        if not await is_vip(msg.chat.id, me):
            price = int(cfg.get("vip_rp_price") or 0)
            if not price:
                return await reply(msg, "Это эксклюзивное РП — только для 💎 VIP (<code>купить вип</code>).")
            import iris_eco
            if not await iris_eco.spend(msg.chat.id, me, price):
                return await reply(msg, f"Эксклюзивное РП стоит {price} {COIN}.")
        await reply(msg, f"💎 {mention(me, msg.from_user.full_name)} {verb} {mention(uid, tname)}")


for _n, _v in _VIP_RP.items():
    _mk_vip_rp(_n, _v)


@cmd(["рп вип", "вип рп", "эксклюзивные рп"], "випрп")
async def c_vip_rp_list(msg, args, rank):
    await reply(msg, "💎 <b>Эксклюзивные РП</b> (ответом на сообщение):\n" + ", ".join(sorted(_VIP_RP)))


# ---------------------------------------------------------------- бонусы
@cmd(["анонимка"], "анонимка")
async def c_anon(msg, args, rank):
    import iris_eco
    text = " ".join(args).strip()
    if not text:
        return await reply(msg, "Пример: <code>анонимка текст</code> (можно ответом на сообщение)")
    cfg = await get_cfg(msg.chat.id)
    price = int(cfg.get("anon_price", 20))
    if price and not await iris_eco.spend(msg.chat.id, msg.from_user.id, price):
        return await reply(msg, f"Анонимка стоит {price} {COIN}.")
    await m.logact(msg.chat.id, msg.from_user.id, "anon", None, text[:200])
    try:
        await msg.delete()
    except Exception:
        pass
    kw = {"reply_to_message_id": msg.reply_to_message.message_id} if msg.reply_to_message else {}
    try:
        await m._bot.send_message(msg.chat.id, f"💌 <b>Анонимное сообщение</b>\n{esc(text)}", parse_mode="HTML", **kw)
    except Exception:
        await m._bot.send_message(msg.chat.id, f"💌 <b>Анонимное сообщение</b>\n{esc(text)}", parse_mode="HTML")


@cmd(["анонимка цена"], "випнастройка")
async def c_anon_price(msg, args, rank):
    if not args or not args[0].isdigit():
        return await reply(msg, "Пример: <code>анонимка цена 20</code>")
    await set_cfg(msg.chat.id, anon_price=int(args[0]))
    await reply(msg, f"Анонимка: {args[0]} {COIN}")


@cmd(["банхаммер"], "банхаммер")
async def c_hammer(msg, args, rank):
    import iris_eco
    uid, name, _ = await resolve_target(msg, args)
    if not uid or uid == msg.from_user.id:
        return await reply(msg, "Пример: ответом <code>банхаммер</code> — мут на 10 минут за монеты.")
    if await m.get_rank(msg.chat.id, uid) > 0:
        return await reply(msg, "Банхаммер не действует на модераторов.")
    cfg = await get_cfg(msg.chat.id)
    price = int(cfg.get("hammer_price", 50))
    if price and not await iris_eco.spend(msg.chat.id, msg.from_user.id, price):
        return await reply(msg, f"Банхаммер стоит {price} {COIN}.")
    try:
        await m.do_mute(msg.chat.id, uid, 600, f"банхаммер от {msg.from_user.full_name}", msg.from_user.id)
    except Exception as e:
        if price:
            await iris_eco.add(msg.chat.id, msg.from_user.id, price)
        return await reply(msg, m.human_error(e))
    await reply(msg, f"🔨 {mention(msg.from_user.id, msg.from_user.full_name)} ударил банхаммером {mention(uid, name)}: мут на 10 мин.")


@cmd(["банхаммер цена"], "випнастройка")
async def c_hammer_price(msg, args, rank):
    if not args or not args[0].isdigit():
        return await reply(msg, "Пример: <code>банхаммер цена 50</code> (0 — бесплатно)")
    await set_cfg(msg.chat.id, hammer_price=int(args[0]))
    await reply(msg, f"Банхаммер: {args[0]} {COIN}")


# ---------------------------------------------------------------- воркер курса
async def worker():
    while True:
        try:
            last = await q("SELECT ts FROM im_gold_hist ORDER BY ts DESC LIMIT 1", one=True)
            if not last or (datetime.now(timezone.utc) - last[0]).total_seconds() >= 3600:
                price, pool = await market()
                drift = (100 - price) / 100 * 0.02
                pressure = max(-0.03, min(0.03, (200 - pool) / 5000))
                new = max(10.0, min(5000.0, price * (1 + drift + pressure + random.gauss(0, 0.012))))
                await q("UPDATE im_market SET price=%s WHERE id=1", (new,))
                await q("INSERT INTO im_gold_hist(price) VALUES(%s)", (new,))
        except Exception:
            log.exception("gold worker")
        await asyncio.sleep(120)


def install(dp):
    mw = GoldPayMiddleware()
    dp.message.outer_middleware(mw)
    dp.pre_checkout_query.outer_middleware(mw)
