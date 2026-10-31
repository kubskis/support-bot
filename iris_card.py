# -*- coding: utf-8 -*-
"""Надзор: карточка «кто я» с красивым графиком активности и кнопками."""
import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from io import BytesIO

from aiogram import F
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

import iris_mod as m
from iris_mod import cmd, q, reply, mention, esc, resolve_target, member_name, get_cfg

log = logging.getLogger("iris_card")
_MSK = timezone(timedelta(hours=3))
_FONTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
RANK_NAMES = {0: "Простой участник", 1: "Младший модератор", 2: "Модератор", 3: "Старший модератор", 4: "Администратор", 5: "Создатель"}


# ---------------------------------------------------------------- график
def _font(size, bold=False):
    from PIL import ImageFont
    p = os.path.join(_FONTS, "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf")
    try:
        return ImageFont.truetype(p, size)
    except Exception:
        try:
            return ImageFont.load_default(size=size)
        except TypeError:
            return ImageFont.load_default()


def _lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _short(n):
    n = int(n)
    if n < 1000:
        return str(n)
    s = f"{n / 1000:.1f}".rstrip("0").rstrip(".")
    return s + "k"


def render_chart(values, dates, title, subtitle=""):
    """Столбчатый график активности. dates — список date (по одному на столбец). Возвращает PNG-байты."""
    from PIL import Image, ImageDraw
    S = 2                                   # суперсэмплинг для гладких краёв
    W, H = 1100 * S, 540 * S
    img = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(img)
    top_c, bot_c = (17, 20, 40), (30, 27, 62)
    for y in range(H):                      # фон-градиент
        d.line([(0, y), (W, y)], fill=_lerp(top_c, bot_c, y / H))
    d.text((48 * S, 34 * S), title, fill=(255, 255, 255), font=_font(34 * S, True))
    if subtitle:
        d.text((48 * S, 86 * S), subtitle, fill=(160, 168, 200), font=_font(19 * S))

    L, R, T, B = 96 * S, 44 * S, 150 * S, 92 * S
    px0, px1, py0, py1 = L, W - R, T, H - B
    n = len(values)
    mx = max(values) if values and max(values) > 0 else 1
    raw = max(1, mx / 4)
    mag = 10 ** (len(str(int(raw))) - 1)
    step = next(k * mag for k in (1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10) if k * mag >= raw)
    step = int(step) if step >= 1 else 1
    top_v = step * 4                        # красивая верхняя граница сетки
    gl = _font(16 * S)
    for g in range(5):                      # сетка и подписи оси Y
        y = py1 - (py1 - py0) * g / 4
        d.line([(px0, y), (px1, y)], fill=(48, 53, 86), width=S)
        txt = _short(step * g)
        tw = d.textlength(txt, font=gl)
        d.text((px0 - 14 * S - tw, y - 10 * S), txt, fill=(122, 131, 168), font=gl)

    gap = 6 * S
    bw = (px1 - px0 - gap * (n - 1)) / n
    lo, hi = (92, 94, 255), (60, 205, 255)  # фиолетовый → голубой
    plo, phi = (255, 94, 160), (255, 170, 90)   # сегодняшний столбец: розовый → оранжевый
    peak_i = max(range(n), key=lambda i: values[i]) if n and max(values) > 0 else -1
    for i, v in enumerate(values):
        x0 = px0 + i * (bw + gap)
        x1 = x0 + bw
        h = (py1 - py0) * v / top_v
        if v <= 0:
            d.rounded_rectangle([x0, py1 - 5 * S, x1, py1], radius=2 * S, fill=(52, 57, 92))
            continue
        h = max(h, 8 * S)
        y0 = py1 - h
        a, b = (plo, phi) if i == n - 1 else (lo, hi)
        bar = Image.new("RGB", (max(int(bw), 1), max(int(h), 1)))
        bd = ImageDraw.Draw(bar)
        for yy in range(bar.height):
            bd.line([(0, yy), (bar.width, yy)], fill=_lerp(b, a, yy / max(bar.height - 1, 1)))
        mask = Image.new("L", bar.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, bar.width, bar.height + 10 * S], radius=int(min(8 * S, bw / 2)), fill=255)
        img.paste(bar, (int(x0), int(y0)), mask)
    # средняя линия
    avg = sum(values) / n if n else 0
    if avg > 0:
        ya = py1 - (py1 - py0) * avg / top_v
        x = px0
        while x < px1:
            d.line([(x, ya), (min(x + 12 * S, px1), ya)], fill=(255, 214, 102), width=S)
            x += 20 * S
        lab = f"в среднем {_short(round(avg))}/день"
        tw = d.textlength(lab, font=gl)
        d.rounded_rectangle([px0 + 8 * S, ya - 40 * S, px0 + tw + 30 * S, ya - 10 * S], radius=8 * S, fill=(30, 27, 62), outline=(255, 214, 102), width=S)
        d.text((px0 + 19 * S, ya - 37 * S), lab, fill=(255, 214, 102), font=gl)
    # подпись пика
    if peak_i >= 0:
        v = values[peak_i]
        x0 = px0 + peak_i * (bw + gap)
        lab = _short(v)
        tw = d.textlength(lab, font=gl)
        yy = py1 - (py1 - py0) * v / top_v - 28 * S
        d.text((x0 + bw / 2 - tw / 2, yy), lab, fill=(255, 255, 255), font=_font(17 * S, True))
    # подписи оси X
    for i, dt in enumerate(dates):
        if (i % 5 == 0 and n - 1 - i >= 3) or i == n - 1:
            x0 = px0 + i * (bw + gap)
            lab = dt.strftime("%d.%m")
            tw = d.textlength(lab, font=gl)
            d.text((x0 + bw / 2 - tw / 2, py1 + 16 * S), lab, fill=(150, 158, 196), font=gl)
    out = img.resize((W // S, H // S), Image.LANCZOS)
    buf = BytesIO()
    out.save(buf, "PNG", optimize=True)
    return buf.getvalue()


# ---------------------------------------------------------------- вспомогательное
def span(td):
    s = int(td.total_seconds())
    if s < 60:
        return "только что"
    mo, rem = divmod(s, 30 * 86400)
    d, rem = divmod(rem, 86400)
    h, rem = divmod(rem, 3600)
    mi = rem // 60
    if mo:
        return f"{mo} мес {d} дн"
    if d:
        return f"{d} дн {h} ч"
    if h:
        return f"{h} ч {mi} мин"
    return f"{mi} мин"


def ago(ts):
    s = (datetime.now(timezone.utc) - ts).total_seconds()
    return "только что" if s < 60 else span(timedelta(seconds=s)) + " назад"


async def _series(cid, uid, days=30):
    rows = await q("SELECT day,msgs FROM im_daily WHERE chat_id=%s AND user_id=%s AND day>CURRENT_DATE-%s", (cid, uid, days), many=True)
    mp = {r[0]: int(r[1]) for r in rows}
    today = datetime.now(_MSK).date()
    ds = [today - timedelta(days=days - 1 - i) for i in range(days)]
    return [mp.get(x, 0) for x in ds], ds


async def _tg_status(cid, uid):
    try:
        mem = await m._bot.get_chat_member(cid, uid)
    except Exception:
        return "Состоит в чате"
    st = mem.status
    if st == "creator":
        return "👑 Создатель чата"
    if st == "administrator":
        return "Телеграм-админ этого чата"
    if st == "left":
        return "❌ Не состоит в чате"
    if st == "kicked":
        return "🔨 Заблокирован в чате"
    if st == "restricted" and getattr(mem, "can_send_messages", True) is False:
        return "🔇 В муте"
    return "Состоит в чате"


def _kb(uid):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 Описание", callback_data=f"imw:about:{uid}"),
         InlineKeyboardButton(text="🏆 Награды", callback_data=f"imw:aw:{uid}")],
        [InlineKeyboardButton(text="👥 Кружки", callback_data=f"imw:circ:{uid}"),
         InlineKeyboardButton(text="🔖 Закладки", callback_data=f"imw:mark:{uid}")]])


async def build_card(cid, uid, name):
    r = await q("SELECT msgs,first_seen,last_seen,profile,username FROM im_members WHERE chat_id=%s AND user_id=%s", (cid, uid), one=True)
    if not r:
        return None
    msgs, first, last, prof, un = r
    prof = prof or {}
    now = datetime.now(timezone.utc)
    rk = await m.get_rank(cid, uid)
    day = (await q("SELECT coalesce(sum(msgs),0) FROM im_daily WHERE chat_id=%s AND user_id=%s AND day=CURRENT_DATE", (cid, uid), one=True))[0]
    week = (await q("SELECT coalesce(sum(msgs),0) FROM im_daily WHERE chat_id=%s AND user_id=%s AND day>CURRENT_DATE-7", (cid, uid), one=True))[0]
    month = (await q("SELECT coalesce(sum(msgs),0) FROM im_daily WHERE chat_id=%s AND user_id=%s AND day>CURRENT_DATE-30", (cid, uid), one=True))[0]
    place = (await q("SELECT count(*)+1 FROM im_members WHERE chat_id=%s AND msgs>%s", (cid, msgs), one=True))[0]
    shown = prof.get("nick") or name          # как у Ириса: вместо имени показывается ник, без (@юзернейма)
    L = [f"👤 Это пользователь <b>{mention(uid, shown)}</b>", await _tg_status(cid, uid), ""]
    L.append(f"⭐ [{rk}] Ранг: <b>{RANK_NAMES.get(rk, rk)}</b>")
    L.append(f"🏠 Гражданин этого чата {span(now - first)}")
    # экономика, брак, клубы, репутация — каждый блок необязателен
    try:
        mar = await q("SELECT a,b,ts FROM im_marriage WHERE chat_id=%s AND active AND (a=%s OR b=%s)", (cid, uid, uid), one=True)
        if mar:
            other = mar[1] if mar[0] == uid else mar[0]
            L.append(f"💞 В браке с {mention(other, await member_name(cid, other))} ({span(now - mar[2])})")
    except Exception:
        pass
    try:
        groups = await q("""SELECT g.kind,g.name FROM im_group g JOIN im_group_m gm ON gm.group_id=g.id
                            WHERE g.chat_id=%s AND gm.user_id=%s AND NOT gm.pending ORDER BY gm.main DESC""", (cid, uid), many=True)
        clan = [n for k, n in groups if k == "clan"]
        circ = [n for k, n in groups if k == "circle"]
        if clan:
            L.append(f"🏰 Клан: <b>{esc(clan[0])}</b>")
        if circ:
            L.append(f"⭕ Кружки: {esc(', '.join(circ[:3]))}" + (f" +{len(circ) - 3}" if len(circ) > 3 else ""))
    except Exception:
        pass
    line = []
    try:
        w = await q("SELECT coins FROM im_wallet WHERE chat_id=%s AND user_id=%s", (cid, uid), one=True)
        if w:
            line.append(f"🪙 {_short(w[0])}")
    except Exception:
        pass
    try:
        rp = await q("SELECT rep FROM im_rep WHERE chat_id=%s AND user_id=%s", (cid, uid), one=True)
        line.append(f"✨ Репутация: {rp[0] if rp else 0}")
    except Exception:
        pass
    if line:
        L.append("  |  ".join(line))
    warns = len(await m.active_warns(cid, uid))
    if warns:
        L.append(f"⚠️ Варны: {warns}")
    for key, ico, lab in (("title", "🎖", "Звание"), ("motto", "💬", "Девиз"),
                          ("gender", "👫", "Пол"), ("city", "📍", "Город"), ("bday", "🎂", "День рождения")):
        if prof.get(key):
            L.append(f"{ico} {lab}: {esc(prof[key])}")
    L += ["", f"📈 Место по активности: #{place}",
          f"Первое появление: {first.astimezone(_MSK).strftime('%d.%m.%Y')} ({span(now - first)})",
          f"Последний актив: {ago(last)}",
          f"Актив (д|н|м|весь): {_short(day)} | {_short(week)} | {_short(month)} | {_short(msgs)}"]
    try:
        aw = await q("SELECT level,text,ts FROM im_awards WHERE chat_id=%s AND user_id=%s ORDER BY ts DESC LIMIT 3", (cid, uid), many=True)
        if aw:
            ic = {1: "🥉", 2: "🥈", 3: "🥇", 4: "🏅", 5: "🎖", 6: "🏆", 7: "💎", 8: "👑"}
            L += ["", "🏆 <b>НАГРАДЫ:</b>"] + [f"{ic.get(l, '🏅')} {esc(t or 'без описания')}, {span(now - ts)}" for l, t, ts in aw]
    except Exception:
        pass
    return "\n".join(L), msgs


@cmd(["кто я", "кто ты", "профиль", "моя анкета", "анкета", "моя стата"], "кто_я")
async def c_whoami(msg, args, rank):
    uid, name, _ = await resolve_target(msg, args)
    if not uid:
        uid, name = msg.from_user.id, msg.from_user.full_name
    cid = msg.chat.id
    card = await build_card(cid, uid, name)
    if not card:
        return await reply(msg, f"{mention(uid, name)}: нет данных.")
    text, total = card
    vals, ds = await _series(cid, uid)
    try:
        png = await asyncio.to_thread(render_chart, vals, ds, "Статистика активности за 30 дней",
                                      f"Сообщений: {_short(sum(vals))} за период · всего {_short(total)}")
        cap = text if len(text) <= 1000 else text[:990].rsplit("\n", 1)[0]
        return await msg.reply_photo(BufferedInputFile(png, "activity.png"), caption=cap, parse_mode="HTML", reply_markup=_kb(uid))
    except Exception:
        log.exception("card chart")
    await reply(msg, text, reply_markup=_kb(uid))


@m.router.callback_query(F.data.startswith("imw:"))
async def cb_card(cb: CallbackQuery):
    _, kind, uid = cb.data.split(":")
    uid, cid = int(uid), cb.message.chat.id
    name = await member_name(cid, uid)
    out = None
    if kind == "about":
        r = await q("SELECT profile FROM im_members WHERE chat_id=%s AND user_id=%s", (cid, uid), one=True)
        p = (r[0] if r else {}) or {}
        parts = []
        for key, lab in (("about", "О себе"), ("motto", "Девиз"), ("title", "Звание"), ("nick", "Ник"), ("city", "Город"), ("bday", "День рождения"), ("gender", "Пол")):
            if p.get(key):
                parts.append(f"<b>{lab}:</b> {esc(p[key])}")
        out = "\n".join(parts) or "Описание не заполнено. Заполнить: <code>+о себе текст</code>, <code>+девиз …</code>"
    elif kind == "aw":
        rows = await q("SELECT level,text,ts FROM im_awards WHERE chat_id=%s AND user_id=%s ORDER BY ts DESC LIMIT 15", (cid, uid), many=True)
        now = datetime.now(timezone.utc)
        out = ("🏆 <b>Награды</b>\n" + "\n".join(f"• {esc(t or 'без описания')}, {span(now - ts)}" for _, t, ts in rows)) if rows else "Наград нет."
    elif kind == "circ":
        rows = await q("""SELECT g.kind,g.name FROM im_group g JOIN im_group_m gm ON gm.group_id=g.id
                          WHERE g.chat_id=%s AND gm.user_id=%s AND NOT gm.pending""", (cid, uid), many=True)
        out = "\n".join(("🏰 Клан: " if k == "clan" else "⭕ Кружок: ") + esc(n) for k, n in rows) or "Не состоит ни в клане, ни в кружках."
    elif kind == "mark":
        if cb.from_user.id != uid:
            return await cb.answer("Закладки видны только их владельцу.", show_alert=True)
        rows = await q("SELECT id,text,ref FROM im_notes WHERE chat_id=%s AND user_id=%s AND kind='mark' ORDER BY id DESC LIMIT 15", (cid, uid), many=True)
        s = str(cid)
        out = ("🔖 <b>Закладки</b>\n" + "\n".join((f'#{i} <a href="https://t.me/c/{s[4:]}/{ref}">{esc(t or "сообщение")}</a>' if s.startswith("-100") else f"#{i} {esc(t)}") for i, t, ref in rows)) if rows else "Закладок нет."
    try:
        await m._bot.send_message(cid, f"{mention(uid, name)}\n\n{out}", parse_mode="HTML", disable_web_page_preview=True,
                                  reply_to_message_id=cb.message.message_id)
    except Exception:
        await m._bot.send_message(cid, f"{mention(uid, name)}\n\n{out}", parse_mode="HTML", disable_web_page_preview=True)
    await cb.answer()
