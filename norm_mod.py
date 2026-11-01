# -*- coding: utf-8 -*-
"""Норматив по поиску секреток для искателей стаффа.

Правила:
  • 1 день — 1 секретка у каждого искателя; не нашёл за день — 1 предупреждение (если общая норма дня не выполнена);
  • общая норма дня = 60% от числа искателей (с округлением вверх); выполнена — личные предупреждения за день не выдаются;
  • ни одной секретки 3 дня подряд — отдельное предупреждение (если в эти дни не было предупреждений за день);
  • 3 предупреждения — главным админам приходят кнопки «Вылет / Простить»;
  • отгулы: болезнь, нет випки (до 7 дней), поездка/нет интернета — по заявке и с подтверждением главного админа.

Данные берутся автоматически: secret_publishers (искатели) и secret_posts (опубликованные секретки, дата по МСК).
"""
import asyncio
import html
import logging
import math
import os
import re
from datetime import date, datetime, timedelta, timezone

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

log = logging.getLogger("norm")
router = Router()
MSK = timezone(timedelta(hours=3))

_bot = None
_get_db = None
_is_main_admin = None
_is_publisher = None
_mention = None
_admin_chat = None
_topic = None

DEFAULTS = {"enabled": "1", "percent": "60", "start": "2026-11-02", "max_warns": "5", "win_from": "6", "win_to": "23"}
REASONS = {"sick": "🤒 Болезнь / плохое самочувствие", "vip": "🔑 Нет випки (до 7 дней)", "trip": "✈️ Поездка / нет интернета"}

_DDL = """
CREATE TABLE IF NOT EXISTS norm_cfg(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS norm_days(day DATE PRIMARY KEY, finders INT, required INT, found INT, met BOOLEAN, ts TIMESTAMPTZ DEFAULT now());
CREATE TABLE IF NOT EXISTS norm_warns(id SERIAL PRIMARY KEY, user_id BIGINT, day DATE, kind TEXT, note TEXT, by_id BIGINT,
                                      active BOOLEAN DEFAULT TRUE, ts TIMESTAMPTZ DEFAULT now());
CREATE INDEX IF NOT EXISTS norm_warns_u ON norm_warns(user_id, active);
CREATE TABLE IF NOT EXISTS norm_excuse(id SERIAL PRIMARY KEY, user_id BIGINT, from_day DATE, to_day DATE, reason TEXT,
                                       status TEXT DEFAULT 'pending', decided_by BIGINT, ts TIMESTAMPTZ DEFAULT now());
ALTER TABLE norm_excuse ADD COLUMN IF NOT EXISTS from_ts TIMESTAMPTZ;
ALTER TABLE norm_excuse ADD COLUMN IF NOT EXISTS to_ts TIMESTAMPTZ;
ALTER TABLE norm_excuse ADD COLUMN IF NOT EXISTS ended_at TIMESTAMPTZ;
UPDATE norm_excuse SET from_ts=(from_day::timestamp AT TIME ZONE 'Europe/Moscow'), to_ts=((to_day+1)::timestamp AT TIME ZONE 'Europe/Moscow') WHERE from_ts IS NULL AND from_day IS NOT NULL;
CREATE TABLE IF NOT EXISTS norm_state(user_id BIGINT PRIMARY KEY, since DATE, alerted BOOLEAN DEFAULT FALSE);
CREATE TABLE IF NOT EXISTS norm_flags(key TEXT PRIMARY KEY);
"""


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
    await q(_DDL)


async def cfg(key):
    r = await q("SELECT value FROM norm_cfg WHERE key=%s", (key,), one=True)
    return r[0] if r else DEFAULTS[key]


async def set_cfg(key, value):
    await q("INSERT INTO norm_cfg(key,value) VALUES(%s,%s) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value", (key, str(value)))


def today_msk() -> date:
    return datetime.now(MSK).date()


async def _start() -> date:
    return date.fromisoformat(await cfg("start"))


async def window() -> tuple:
    """(час начала, час конца) приёма секреток по МСК: засчитываются секретки с a:00 до b:00."""
    return int(await cfg("win_from")), int(await cfg("win_to"))


_LOC = "(created_at AT TIME ZONE 'Europe/Moscow')"
_WIN_SQL = f"EXTRACT(HOUR FROM {_LOC}) >= %s AND EXTRACT(HOUR FROM {_LOC}) < %s"


async def window_label() -> str:
    a, b = await window()
    return f"{a:02d}:00–{b:02d}:00 МСК"


async def last_closed_day() -> date:
    """Последний полностью закрытый день: сегодня — после конца окна (+5 минут), иначе вчера."""
    a, b = await window()
    now = datetime.now(MSK)
    return now.date() if now.hour * 60 + now.minute >= b * 60 + 5 else now.date() - timedelta(days=1)


def _fmt(d: date) -> str:
    return d.strftime("%d.%m")


async def flag_once(key) -> bool:
    """True, если флаг ещё не стоял (и ставит его)."""
    r = await q("INSERT INTO norm_flags(key) VALUES(%s) ON CONFLICT DO NOTHING RETURNING 1", (key,), one=True)
    return bool(r)


# ---------------------------------------------------------------- данные дня
async def finders():
    return [r[0] for r in (await q("SELECT user_id FROM secret_publishers ORDER BY user_id", many=True) or [])]


async def finds_on(d: date) -> dict:
    a, b = await window()
    rows = await q(f"SELECT user_id, COUNT(*) FROM secret_posts WHERE {_LOC}::date=%s AND {_WIN_SQL} GROUP BY user_id", (d, a, b), many=True)
    return {u: int(n) for u, n in (rows or [])}


_END_EFF = "LEAST(to_ts, COALESCE(ended_at, to_ts))"


async def excused_on(d: date) -> set:
    """День — отгул, если отгул покрывает не меньше половины времени приёма секреток в этот день."""
    a, b = await window()
    ws = datetime(d.year, d.month, d.day, tzinfo=MSK) + timedelta(hours=a)
    we = datetime(d.year, d.month, d.day, tzinfo=MSK) + timedelta(hours=b)
    half = (we - ws).total_seconds() / 2
    rows = await q(f"""SELECT DISTINCT user_id FROM norm_excuse WHERE status='ok' AND from_ts IS NOT NULL
                       AND EXTRACT(EPOCH FROM (LEAST({_END_EFF}, %s) - GREATEST(from_ts, %s))) >= %s""", (we, ws, half), many=True)
    return {r[0] for r in (rows or [])}


def fmt_dt(dt_) -> str:
    return dt_.astimezone(MSK).strftime("%d.%m.%Y %H:%M")


def fmt_hours(h: float) -> str:
    if h >= 48:
        return f"{h / 24:g} дн. ({h:g} ч)"
    return f"{h:g} ч"


async def sync_state():
    """У каждого искателя есть дата «с какого дня учитываем». Первый запуск — со стартового дня, новичкам — со следующего."""
    fs = await finders()
    if not fs:
        return
    have = {r[0] for r in (await q("SELECT user_id FROM norm_state", many=True) or [])}
    first_ever = not have
    since = (await _start()) - timedelta(days=1) if first_ever else today_msk()
    for u in fs:
        if u not in have:
            await q("INSERT INTO norm_state(user_id,since) VALUES(%s,%s) ON CONFLICT DO NOTHING", (u, since))


async def eligible_on(d: date) -> list:
    """Искатели, которые учитываются в этот день (не в отгуле и работают в рейтинге с этого дня)."""
    await sync_state()
    exc = await excused_on(d)
    rows = await q("SELECT s.user_id FROM norm_state s JOIN secret_publishers p ON p.user_id=s.user_id WHERE s.since<%s", (d,), many=True)
    return [r[0] for r in (rows or []) if r[0] not in exc]


def required_for(n_active: int, percent: int) -> int:
    return (n_active * percent + 99) // 100 if n_active else 0


async def active_warns(uid) -> int:
    return (await q("SELECT COUNT(*) FROM norm_warns WHERE user_id=%s AND active", (uid,), one=True))[0]


async def add_warn(uid, d, kind, note, by=None):
    await q("INSERT INTO norm_warns(user_id,day,kind,note,by_id) VALUES(%s,%s,%s,%s,%s)", (uid, d, kind, note, by))
    n = await active_warns(uid)
    limit = int(await cfg("max_warns"))
    try:
        await _bot.send_message(uid, f"⚠️ <b>Предупреждение по нормативу</b> ({n}/{limit})\n{html.escape(note)}", parse_mode="HTML")
    except Exception:
        pass
    if n >= limit:
        r = await q("UPDATE norm_state SET alerted=TRUE WHERE user_id=%s AND NOT alerted RETURNING 1", (uid,), one=True)
        if r:
            await _alert_limit(uid, n)
    return n


async def _dm_main_admins(text, kb=None):
    """Дублирует важное сообщение в личку владельцу и всем главным админам."""
    ids = set()
    try:
        owner = int(os.getenv("ADMIN_ID", "0"))
        if owner:
            ids.add(owner)
    except Exception:
        pass
    try:
        for r in (await q("SELECT admin_id FROM main_admins", many=True) or []):
            ids.add(int(r[0]))
    except Exception as e:
        log.warning("main_admins: %s", e)
    for i in ids:
        try:
            await _bot.send_message(i, text, parse_mode="HTML", reply_markup=kb)
        except Exception as e:
            log.info("лс главному админу %s не доставлено: %s", i, e)


async def _alert_limit(uid, n):
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🚪 Вылет", callback_data=f"nrm:out:{uid}"),
        InlineKeyboardButton(text="🤝 Простить", callback_data=f"nrm:fg:{uid}")]])
    try:
        await _bot.send_message(
            _admin_chat, f"🚨 <b>{n} предупреждения по нормативу</b> у {await _mention(uid)} (<code>{uid}</code>).\nРешение главных админов:",
            parse_mode="HTML", reply_markup=kb, message_thread_id=_topic)
    except Exception as e:
        log.warning("alert: %s", e)
    await _dm_main_admins(f"🚨 <b>{n} предупреждения по нормативу</b> у {await _mention(uid)} (<code>{uid}</code>).\nРешение:", kb)


# ---------------------------------------------------------------- итог дня
async def process_day(d: date):
    if await q("SELECT 1 FROM norm_days WHERE day=%s", (d,), one=True):
        return
    percent = int(await cfg("percent"))
    elig = await eligible_on(d)
    finds = await finds_on(d)
    total = sum(finds.values())
    req = required_for(len(elig), percent)
    met = total >= req
    warned, streak_warned = [], []
    for u in elig:
        if finds.get(u, 0) == 0 and not met:
            await add_warn(u, d, "day", f"{_fmt(d)}: не найдено ни одной секретки, общая норма дня не выполнена ({total}/{req}).")
            warned.append(u)
    # серия: 3 дня подряд без секреток и без «дневных» предупреждений
    d0 = d - timedelta(days=2)
    if d0 >= await _start():
        for u in elig:
            if u in warned:
                continue
            wa, wb = await window()
            cnt = (await q(f"SELECT COUNT(*) FROM secret_posts WHERE user_id=%s AND {_LOC}::date BETWEEN %s AND %s AND {_WIN_SQL}", (u, d0, d, wa, wb), one=True))[0]
            if cnt:
                continue
            if any(u in await excused_on(d0 + timedelta(days=i)) for i in range(3)):
                continue
            if await q("SELECT 1 FROM norm_warns WHERE user_id=%s AND day BETWEEN %s AND %s AND kind IN ('day','streak')", (u, d0, d), one=True):
                continue
            await add_warn(u, d, "streak", f"{_fmt(d0)}–{_fmt(d)}: ни одной секретки за 3 дня подряд.")
            streak_warned.append(u)
    await q("INSERT INTO norm_days(day,finders,required,found,met) VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING", (d, len(elig), req, total, met))
    lines = [f"📊 <b>Итог дня {_fmt(d)}</b> (приём {await window_label()})", f"Искателей в учёте: {len(elig)} · нужно секреток: {req} · найдено: {total}",
             "✅ Общая норма выполнена." if met else "❌ Общая норма не выполнена."]
    if warned:
        lines.append("⚠️ Предупреждения за день: " + ", ".join([await _mention(u) for u in warned]))
    if streak_warned:
        lines.append("⚠️ Предупреждения за 3 дня без секреток: " + ", ".join([await _mention(u) for u in streak_warned]))
    if met and not streak_warned:
        lines.append("Личных предупреждений за день нет.")
    try:
        await _bot.send_message(_admin_chat, "\n".join(lines), parse_mode="HTML", message_thread_id=_topic)
    except Exception as e:
        log.warning("итог дня: %s", e)


async def process_pending():
    start, last = await _start(), await last_closed_day()
    d = start
    while d <= last:
        await process_day(d)
        d += timedelta(days=1)


async def _remind(kind):
    d = today_msk()
    if d < await _start():
        return
    pool = await eligible_on(d)
    finds = await finds_on(d)
    total = sum(finds.values())
    req = required_for(len(pool), int(await cfg("percent")))
    if kind == "morning":
        txt = (f"🌅 <b>Новый день, норматив {_fmt(d)}</b>\nСекретки засчитываются с {await window_label()}.\n"
               f"Искателей в учёте: {len(pool)} · общая норма: <b>{req}</b> секр.\nКаждому — 1 секретка. Статус: /norm, таблица: /normtop")
    else:
        if total >= req:
            return
        lag = [u for u in pool if finds.get(u, 0) == 0]
        txt = (f"⏰ <b>Норма дня не выполнена:</b> {total}/{req}. Приём секреток закрывается в {(await window())[1]:02d}:00 МСК.\n"
               + ("Ещё не нашли: " + ", ".join([await _mention(u) for u in lag]) if lag else ""))
    try:
        await _bot.send_message(_admin_chat, txt, parse_mode="HTML", message_thread_id=_topic)
    except Exception as e:
        log.warning("напоминание: %s", e)


async def norm_worker():
    log.info("Воркер норматива запущен")
    await init_tables()
    while True:
        try:
            if (await cfg("enabled")) == "1":
                now = datetime.now(MSK)
                wa, wb = await window()
                await process_pending()
                if wa <= now.hour < wb and await flag_once(f"morning:{now.date()}"):
                    await _remind("morning")
                if wb - 3 <= now.hour < wb and await flag_once(f"evening:{now.date()}"):
                    await _remind("evening")
        except Exception:
            log.exception("norm worker")
        await asyncio.sleep(60)


# ---------------------------------------------------------------- права
async def _allowed(uid) -> bool:
    return bool(await _is_main_admin(uid) or await _is_publisher(uid))


def _name(u):
    return html.escape(u.full_name)


# ---------------------------------------------------------------- /norm
async def _streak_days(uid) -> int:
    n = 0
    d = today_msk() - timedelta(days=1)
    start = await _start()
    while n < 30 and d >= start:
        wa, wb = await window()
        if await q(f"SELECT 1 FROM secret_posts WHERE user_id=%s AND {_LOC}::date=%s AND {_WIN_SQL}", (uid, d, wa, wb), one=True):
            break
        if uid in await excused_on(d):
            break
        n += 1
        d -= timedelta(days=1)
    return n


@router.message(Command("norm"))
async def cmd_norm(message: Message):
    uid = message.from_user.id
    if not await _allowed(uid):
        return
    d = today_msk()
    start = await _start()
    if (await cfg("enabled")) != "1":
        return await message.answer("Норматив сейчас выключен.")
    pool = await eligible_on(d)
    finds = await finds_on(d)
    total = sum(finds.values())
    req = required_for(len(pool), int(await cfg("percent")))
    mine = finds.get(uid, 0)
    n = await active_warns(uid)
    limit = int(await cfg("max_warns"))
    ex = await q(f"SELECT {_END_EFF}, reason FROM norm_excuse WHERE user_id=%s AND status='ok' AND {_END_EFF} > now() ORDER BY {_END_EFF} DESC LIMIT 1", (uid,), one=True)
    pend = await q("SELECT 1 FROM norm_excuse WHERE user_id=%s AND status='pending'", (uid,), one=True)
    lines = [f"📋 <b>Норматив секреток · {_fmt(d)}</b>", f"Приём секреток: {await window_label()}"]
    if d < start:
        lines.append(f"Начнётся с {start.strftime('%d.%m.%Y')}.")
    lines += [
        f"Общая норма дня: <b>{total}/{req}</b> {'✅' if total >= req else '⏳'}",
        f"Ваши секретки сегодня: <b>{mine}</b> {'✅' if mine else '—'}",
        f"Предупреждения: <b>{n}/{limit}</b>",
        f"Дней без секреток подряд: {await _streak_days(uid)}",
    ]
    if ex:
        lines.append(f"🏖 Отгул до {fmt_dt(ex[0])} МСК ({html.escape(ex[1] or '')})")
    elif pend:
        lines.append("🕓 Заявка на отгул ждёт решения админов.")
    lines.append("\nОтгул: /otgul · таблица стаффа: /normtop")
    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("normtop"))
async def cmd_normtop(message: Message):
    if not await _allowed(message.from_user.id):
        return
    d = today_msk()
    exc = await excused_on(d)
    fs = await finders()
    finds = await finds_on(d)
    pool = await eligible_on(d)
    req = required_for(len(pool), int(await cfg("percent")))
    total = sum(finds.values())
    lines = [f"📊 <b>Норматив {_fmt(d)}</b> — общая норма {total}/{req} {'✅' if total >= req else '⏳'}\n"]
    rows = []
    for u in fs:
        n = await active_warns(u)
        rows.append((u, finds.get(u, 0), n, u in exc))
    rows.sort(key=lambda r: (-r[1], r[2]))
    for u, f, n, e in rows[:60]:
        mark = "🏖" if e else ("✅" if f else "▫️")
        lines.append(f"{mark} {await _mention(u)} — {f} сегодня" + (f" · ⚠️{n}" if n else ""))
    await message.answer("\n".join(lines), parse_mode="HTML")


# ---------------------------------------------------------------- отгул
def _kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("otgul"))
async def cmd_otgul(message: Message):
    if not await _allowed(message.from_user.id):
        return
    await message.answer("Причина отгула от норматива:", reply_markup=_kb([[InlineKeyboardButton(text=t, callback_data=f"nrm:r:{k}")] for k, t in REASONS.items()]))


@router.callback_query(F.data.startswith("nrm:r:"))
async def cb_reason(cb: CallbackQuery):
    kind = cb.data.split(":")[2]
    opts = [1, 2, 3, 5, 7] + ([] if kind == "vip" else [10, 14])
    rows = [[InlineKeyboardButton(text=f"{n} дн.", callback_data=f"nrm:d:{kind}:{n}") for n in opts[i:i + 4]] for i in range(0, len(opts), 4)]
    await cb.message.edit_text(f"{REASONS.get(kind, '')}\nНа сколько дней?", reply_markup=_kb(rows))
    await cb.answer()


@router.callback_query(F.data.startswith("nrm:d:"))
async def cb_days(cb: CallbackQuery):
    _, _, kind, n = cb.data.split(":")
    n = int(n)
    if kind == "vip":
        n = min(n, 7)
    uid = cb.from_user.id
    if not await _allowed(uid):
        return await cb.answer("Нет доступа", show_alert=True)
    t0 = datetime.now(MSK)
    t1 = t0 + timedelta(days=n)
    rid = (await q("INSERT INTO norm_excuse(user_id,from_ts,to_ts,reason) VALUES(%s,%s,%s,%s) RETURNING id", (uid, t0, t1, REASONS[kind]), one=True))[0]
    await cb.message.edit_text(f"🕓 Заявка на отгул ({n} дн.) отправлена главным админам.")
    kb = _kb([[InlineKeyboardButton(text="✅ Одобрить", callback_data=f"nrm:ok:{rid}"), InlineKeyboardButton(text="❌ Отклонить", callback_data=f"nrm:no:{rid}")]])
    try:
        await _bot.send_message(_admin_chat, f"🏖 <b>Заявка на отгул от норматива</b>\n{await _mention(uid)}: {html.escape(REASONS[kind])}\n"
                                f"С {fmt_dt(t0)} по {fmt_dt(t1)} МСК ({n} дн.)", parse_mode="HTML", reply_markup=kb, message_thread_id=_topic)
    except Exception as e:
        log.warning("заявка: %s", e)
    await _dm_main_admins(f"🏖 <b>Заявка на отгул от норматива</b>\n{await _mention(uid)}: {html.escape(REASONS[kind])}\n"
                          f"С {fmt_dt(t0)} по {fmt_dt(t1)} МСК ({n} дн.)", kb)
    await cb.answer()


@router.callback_query(F.data.regexp(r"^nrm:(ok|no):\d+$"))
async def cb_decide(cb: CallbackQuery):
    if not await _is_main_admin(cb.from_user.id):
        return await cb.answer("Только главные админы", show_alert=True)
    _, act, rid = cb.data.split(":")
    ok = act == "ok"
    r = await q("UPDATE norm_excuse SET status=%s, decided_by=%s WHERE id=%s AND status='pending' RETURNING user_id, from_ts, to_ts",
                ("ok" if ok else "no", cb.from_user.id, int(rid)), one=True)
    if not r:
        return await cb.answer("Уже решено")
    await cb.message.edit_text(cb.message.html_text + f"\n\n{'✅ Одобрено' if ok else '❌ Отклонено'}: {html.escape(cb.from_user.full_name)}", parse_mode="HTML")
    try:
        await _bot.send_message(r[0], f"{'✅ Отгул одобрен' if ok else '❌ Отгул отклонён'} ({fmt_dt(r[1])} — {fmt_dt(r[2])} МСК).")
    except Exception:
        pass
    await cb.answer()


# ---------------------------------------------------------------- вылет / прощение
@router.callback_query(F.data.regexp(r"^nrm:(out|fg):\d+$"))
async def cb_limit(cb: CallbackQuery):
    if not await _is_main_admin(cb.from_user.id):
        return await cb.answer("Только главные админы", show_alert=True)
    _, act, uid = cb.data.split(":")
    uid = int(uid)
    if act == "out":
        await q("DELETE FROM secret_publishers WHERE user_id=%s", (uid,))
        res = "🚪 Вылет: убран из искателей"
    else:
        res = "🤝 Прощён, предупреждения сброшены"
    await q("UPDATE norm_warns SET active=FALSE WHERE user_id=%s", (uid,))
    await q("UPDATE norm_state SET alerted=FALSE WHERE user_id=%s", (uid,))
    await cb.message.edit_text(cb.message.html_text + f"\n\n{res} ({html.escape(cb.from_user.full_name)})", parse_mode="HTML")
    await cb.answer()


# ---------------------------------------------------------------- админка
@router.message(Command("normadmin"))
async def cmd_admin(message: Message):
    if not await _is_main_admin(message.from_user.id):
        return
    a = (message.text or "").split()[1:]
    help_ = ("<b>/normadmin</b>\n<code>on</code> / <code>off</code> — включить/выключить\n<code>percent 60</code> — процент общей нормы\n"
             "<code>start 2026-11-02</code> — день старта\n<code>warn UID +1</code> / <code>warn UID -1</code> — вручную\n"
             "<code>reset UID</code> — сбросить предупреждения\n<code>excuse UID ЧАСОВ [причина]</code> — выдать отгул\n"
             "<code>run</code> — пересчитать прошедшие дни\n<code>status</code> — настройки")
    if not a:
        return await message.answer(help_, parse_mode="HTML")
    c = a[0].lower()
    if c in ("on", "off"):
        await set_cfg("enabled", "1" if c == "on" else "0")
        return await message.answer(f"Норматив: {'включён' if c == 'on' else 'выключен'}.")
    if c == "percent" and len(a) > 1 and a[1].isdigit():
        await set_cfg("percent", max(1, min(int(a[1]), 100)))
        return await message.answer(f"Общая норма: {a[1]}% от числа искателей.")
    if c == "start" and len(a) > 1:
        try:
            date.fromisoformat(a[1])
        except ValueError:
            return await message.answer("Формат: 2026-11-02")
        await set_cfg("start", a[1])
        return await message.answer(f"Старт норматива: {a[1]}.")
    if c == "warn" and len(a) > 2 and a[1].isdigit():
        uid, delta = int(a[1]), int(a[2])
        if delta > 0:
            n = await add_warn(uid, today_msk(), "manual", "Предупреждение выдано вручную админом.", message.from_user.id)
        else:
            await q("UPDATE norm_warns SET active=FALSE WHERE id=(SELECT id FROM norm_warns WHERE user_id=%s AND active ORDER BY id DESC LIMIT 1)", (uid,))
            await q("UPDATE norm_state SET alerted=FALSE WHERE user_id=%s", (uid,))
            n = await active_warns(uid)
        return await message.answer(f"Предупреждений у {uid}: {n}")
    if c == "reset" and len(a) > 1 and a[1].isdigit():
        await q("UPDATE norm_warns SET active=FALSE WHERE user_id=%s", (int(a[1]),))
        await q("UPDATE norm_state SET alerted=FALSE WHERE user_id=%s", (int(a[1]),))
        return await message.answer("Предупреждения сброшены.")
    if c == "excuse" and len(a) > 2 and a[1].isdigit():
        hours = parse_hours(a[2])
        if not hours:
            return await message.answer("Формат: /normadmin excuse UID ЧАСОВ [причина], например 720 или 24*30")
        t1 = await grant_excuse(int(a[1]), hours, " ".join(a[3:]), message.from_user.id)
        return await message.answer(f"Отгул выдан на {fmt_hours(hours)}, до {fmt_dt(t1)} МСК.")
    if c == "run":
        await process_pending()
        return await message.answer("Пересчитано.")
    if c == "status":
        return await message.answer(f"Включён: {await cfg('enabled')}\nПроцент: {await cfg('percent')}\nСтарт: {await cfg('start')}\nЛимит предупреждений: {await cfg('max_warns')}\nИскателей: {len(await finders())}")
    await message.answer(help_, parse_mode="HTML")


def parse_hours(text) -> float:
    """«720», «1,5», «24*30» → число часов (0, если не разобрали). Допустимы только цифры и + - * / ( )."""
    t = (text or "").strip().replace(",", ".").replace("×", "*").replace("х", "*").replace("x", "*")
    if not t or "**" in t or "//" in t or not re.fullmatch(r"[0-9+\-*/(). ]{1,40}", t):
        return 0
    try:
        v = float(eval(t, {"__builtins__": {}}, {}))   # noqa: S307 — строка уже ограничена цифрами и знаками арифметики
    except Exception:
        return 0
    return v if 0 < v <= 24 * 366 else 0


async def grant_excuse(uid, hours, reason, by) -> datetime:
    t0 = datetime.now(MSK)
    t1 = t0 + timedelta(hours=hours)
    await q("INSERT INTO norm_excuse(user_id,from_ts,to_ts,reason,status,decided_by) VALUES(%s,%s,%s,%s,'ok',%s)",
            (uid, t0, t1, (reason or "").strip()[:100] or "по решению админа", by))
    try:
        await _bot.send_message(uid, f"🏖 Вам выдан отгул от норматива на {fmt_hours(hours)} (до {fmt_dt(t1)} МСК).")
    except Exception:
        pass
    return t1


class NormStates(StatesGroup):
    hours = State()


# ---------------------------------------------------------------- раздел админ-панели
_names = {}
PAGE = 15


async def _nm(uid) -> str:
    n = _names.get(uid)
    if n:
        return n
    try:
        ch = await _bot.get_chat(uid)
        n = html.escape(ch.full_name or str(uid))
        if ch.username:
            n += f" (@{ch.username})"
    except Exception:
        n = str(uid)
    _names[uid] = n
    return n


def _bar(done, total, width=10) -> str:
    if total <= 0:
        return "▰" * width
    k = min(width, int(round(width * done / total)))
    return "▰" * k + "▱" * (width - k)


def _ikb(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _edit(cb: CallbackQuery, text, kb):
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception as e:
        if "not modified" not in str(e).lower():
            log.warning("panel edit: %s", e)
    await cb.answer()


async def _time_note(d) -> str:
    a, b = await window()
    now = datetime.now(MSK)
    if d < await _start():
        return f"Старт норматива: {(await _start()).strftime('%d.%m.%Y')} в {a:02d}:00 МСК."
    m = now.hour * 60 + now.minute
    if m < a * 60:
        return f"Приём откроется в {a:02d}:00 МСК."
    if m >= b * 60:
        return f"Приём закрыт, итог дня в {b:02d}:05 МСК." if m < b * 60 + 5 else "Приём закрыт, итог дня подведён."
    left = b * 60 - m
    return f"До закрытия приёма: {left // 60} ч {left % 60:02d} мин."


async def overview(page=0):
    d = today_msk()
    pct = int(await cfg("percent"))
    pool = set(await eligible_on(d))
    exc = await excused_on(d)
    fs = await finders()
    finds = await finds_on(d)
    total_all = sum(finds.get(u, 0) for u in fs)
    req = required_for(len(pool), pct)
    perc = int(total_all * 100 / req) if req else 100
    enabled = (await cfg("enabled")) == "1"
    lines = [f"📋 <b>Норматив секреток · {_fmt(d)}</b>" + ("" if enabled else " · 🔴 <b>ВЫКЛЮЧЕН</b>"),
             f"Приём секреток: {await window_label()} · {html.escape(await _time_note(d))}", "",
             f"<b>Общая норма:</b> {total_all}/{req} · {perc}%  {_bar(total_all, req)}",
             f"Искателей в учёте: {len(pool)} из {len(fs)} · отгул: {len([u for u in fs if u in exc])} · норма {pct}%", ""]
    rows = []
    for u in fs:
        n = await active_warns(u)
        rows.append((u, finds.get(u, 0), n, u in exc, u in pool))
    rows.sort(key=lambda r: (-r[1], r[2], r[0]))
    pages = max(1, math.ceil(len(rows) / PAGE))
    page = max(0, min(page, pages - 1))
    for u, f, n, e, inpool in rows[page * PAGE:(page + 1) * PAGE]:
        if e:
            mark, pc = "🏖", "отгул"
        elif not inpool:
            mark, pc = "🆕", "с завтра"
        else:
            mark, pc = ("✅" if f else "▫️"), f"{f * 100}% нормы"
        lines.append(f"{mark} {await _nm(u)} — <b>{f}</b> · {pc}" + (f" · ⚠️{n}/{await cfg('max_warns')}" if n else ""))
    if not rows:
        lines.append("Список искателей пуст.")
    if pages > 1:
        lines.append(f"\nСтраница {page + 1}/{pages}")
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"nrmp:{page - 1}"))
    nav.append(InlineKeyboardButton(text="🔄 Обновить", callback_data=f"nrmp:{page}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"nrmp:{page + 1}"))
    kb = _ikb([nav,
               [InlineKeyboardButton(text="⚠️ Предупреждения", callback_data="nrmw"), InlineKeyboardButton(text="📅 История", callback_data="nrmh")],
               [InlineKeyboardButton(text="🏖 Отгулы", callback_data="nrme"), InlineKeyboardButton(text="⚙️ Настройки", callback_data="nrms")],
               [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_adm")]])
    return "\n".join(lines), kb


async def _guard(cb: CallbackQuery) -> bool:
    if not await _is_main_admin(cb.from_user.id):
        await cb.answer("❌ Нет доступа", show_alert=True)
        return False
    return True


@router.callback_query(F.data == "adm_norm")
async def adm_norm(cb: CallbackQuery):
    if not await _guard(cb):
        return
    text, kb = await overview(0)
    await _edit(cb, text, kb)


@router.callback_query(F.data.regexp(r"^nrmp:\d+$"))
async def adm_norm_page(cb: CallbackQuery):
    if not await _guard(cb):
        return
    text, kb = await overview(int(cb.data.split(":")[1]))
    await _edit(cb, text, kb)


@router.callback_query(F.data == "nrmw")
async def adm_norm_warns(cb: CallbackQuery):
    if not await _guard(cb):
        return
    rows = await q("SELECT user_id, COUNT(*), MAX(day) FROM norm_warns WHERE active GROUP BY user_id ORDER BY COUNT(*) DESC, MAX(day) DESC LIMIT 15", many=True)
    lim = await cfg("max_warns")
    lines = ["⚠️ <b>Предупреждения по нормативу</b>\n"]
    btns = []
    if not rows:
        lines.append("Активных предупреждений нет.")
    for u, n, last in rows or []:
        notes = await q("SELECT day, kind, note FROM norm_warns WHERE user_id=%s AND active ORDER BY id DESC LIMIT 3", (u,), many=True)
        lines.append(f"<b>{await _nm(u)}</b> — {n}/{lim} (последнее {_fmt(last)})")
        for dd, kind, note in notes or []:
            lines.append(f"   • {html.escape(note or '')[:110]}")
        btns.append([InlineKeyboardButton(text=f"🗑 Сбросить: {(await _nm(u))[:24]}", callback_data=f"nrmr:{u}")])
    btns.append([InlineKeyboardButton(text="🔙 К таблице", callback_data="adm_norm")])
    await _edit(cb, "\n".join(lines), _ikb(btns))


@router.callback_query(F.data.regexp(r"^nrmr:\d+$"))
async def adm_norm_reset(cb: CallbackQuery):
    if not await _guard(cb):
        return
    uid = int(cb.data.split(":")[1])
    await q("UPDATE norm_warns SET active=FALSE WHERE user_id=%s", (uid,))
    await q("UPDATE norm_state SET alerted=FALSE WHERE user_id=%s", (uid,))
    await adm_norm_warns(cb)


@router.callback_query(F.data == "nrmh")
async def adm_norm_hist(cb: CallbackQuery):
    if not await _guard(cb):
        return
    rows = await q("SELECT day, finders, required, found, met FROM norm_days ORDER BY day DESC LIMIT 14", many=True)
    lines = ["📅 <b>История норматива</b> (последние дни)\n"]
    if not rows:
        lines.append("Итогов пока нет — первый подводится после закрытия приёма в первый день.")
    for dd, fn, rq, fd, met in rows or []:
        lines.append(f"{'✅' if met else '❌'} {dd.strftime('%d.%m')} — найдено {fd} из {rq} · искателей {fn}")
    wr = await q("SELECT COUNT(*) FROM norm_warns WHERE ts>now()-interval '7 days'", one=True)
    lines.append(f"\nПредупреждений за 7 дней: {wr[0]}")
    await _edit(cb, "\n".join(lines), _ikb([[InlineKeyboardButton(text="🔙 К таблице", callback_data="adm_norm")]]))


@router.callback_query(F.data == "nrme")
async def adm_norm_exc(cb: CallbackQuery, state: FSMContext = None):
    if not await _guard(cb):
        return
    if state:
        await state.clear()
    rows = await q(f"""SELECT id, user_id, from_ts, to_ts, ended_at, reason, status FROM norm_excuse
                       WHERE status IN ('ok','pending') AND from_ts IS NOT NULL AND ({_END_EFF} > now() OR status='pending')
                       ORDER BY id DESC LIMIT 20""", many=True)
    lines = ["🏖 <b>Отгулы от норматива</b>\n"]
    btns = [[InlineKeyboardButton(text="➕ Выдать отгул", callback_data="nrmg")]]
    if not rows:
        lines.append("Активных отгулов и заявок нет.")
    now = datetime.now(MSK)
    for eid, u, f, t, ended, r, st in rows or []:
        left = (min(t, ended or t) - now).total_seconds() / 3600
        tail = f"осталось {fmt_hours(round(left, 1))}" if st == "ok" else "ждёт решения"
        lines.append(f"{'✅' if st == 'ok' else '🕓'} {await _nm(u)} — до {fmt_dt(min(t, ended or t))} · {tail} · {html.escape(r or '')}")
        if st == "ok":
            btns.append([InlineKeyboardButton(text=f"⏹ Завершить: {(await _nm(u))[:26]}", callback_data=f"nrmx:{eid}")])
    btns.append([InlineKeyboardButton(text="🔙 К таблице", callback_data="adm_norm")])
    await _edit(cb, "\n".join(lines), _ikb(btns))


@router.callback_query(F.data.regexp(r"^nrmx:\d+$"))
async def adm_norm_end(cb: CallbackQuery, state: FSMContext = None):
    if not await _guard(cb):
        return
    r = await q(f"UPDATE norm_excuse SET ended_at=now() WHERE id=%s AND status='ok' AND {_END_EFF} > now() RETURNING user_id", (int(cb.data.split(":")[1]),), one=True)
    if r:
        try:
            await _bot.send_message(r[0], "🏁 Ваш отгул от норматива завершён досрочно. Норма снова учитывается.")
        except Exception:
            pass
    await adm_norm_exc(cb, state)


@router.callback_query(F.data == "nrmg")
async def adm_norm_grant(cb: CallbackQuery, state: FSMContext):
    if not await _guard(cb):
        return
    await state.clear()
    fs = await finders()
    btns, line = [], []
    for u in fs[:40]:
        line.append(InlineKeyboardButton(text=(await _nm(u))[:22], callback_data=f"nrmu:{u}"))
        if len(line) == 2:
            btns.append(line)
            line = []
    if line:
        btns.append(line)
    btns.append([InlineKeyboardButton(text="🔙 Назад", callback_data="nrme")])
    await _edit(cb, "➕ <b>Выдать отгул</b>\nВыберите искателя:", _ikb(btns))


@router.callback_query(F.data.regexp(r"^nrmu:\d+$"))
async def adm_norm_pick(cb: CallbackQuery, state: FSMContext):
    if not await _guard(cb):
        return
    uid = int(cb.data.split(":")[1])
    await state.set_state(NormStates.hours)
    await state.update_data(uid=uid)
    await cb.message.answer(f"Отгул для <b>{await _nm(uid)}</b>.\nНапишите <b>количество часов</b> числом (например <code>24</code>, месяц — <code>720</code>) "
                            "и, если нужно, причину через пробел: <code>72 болеет</code>.\nМожно выражением: <code>24*30</code>.",
                            parse_mode="HTML", reply_markup=_ikb([[InlineKeyboardButton(text="✖️ Отмена", callback_data="nrme")]]))
    await cb.answer()


@router.message(NormStates.hours, F.chat.type == "private")
async def adm_norm_hours(message: Message, state: FSMContext):
    if not await _is_main_admin(message.from_user.id):
        return
    txt = (message.text or "").strip()
    if txt.startswith("/"):
        await state.clear()
        return await message.answer("Выдача отгула отменена.")
    head, _, reason = txt.partition(" ")
    hours = parse_hours(head)
    if not hours:
        return await message.answer("Не понял число часов. Пример: <code>24</code>, <code>720</code> или <code>24*30</code>.", parse_mode="HTML")
    uid = (await state.get_data()).get("uid")
    await state.clear()
    t1 = await grant_excuse(uid, hours, reason, message.from_user.id)
    await message.answer(f"✅ Отгул выдан: <b>{await _nm(uid)}</b> на {fmt_hours(hours)}, до {fmt_dt(t1)} МСК.\nЗавершить досрочно можно в «🏖 Отгулы».",
                         parse_mode="HTML", reply_markup=_ikb([[InlineKeyboardButton(text="🏖 К отгулам", callback_data="nrme")]]))


async def _settings_view():
    on = (await cfg("enabled")) == "1"
    pct = await cfg("percent")
    a, b = await window()
    text = ("⚙️ <b>Настройки норматива</b>\n\n"
            f"Состояние: {'🟢 включён' if on else '🔴 выключен'}\nОбщая норма: <b>{pct}%</b> от числа искателей (округление вверх)\n"
            f"Приём секреток: <b>{a:02d}:00–{b:02d}:00 МСК</b>\nСтарт: {(await _start()).strftime('%d.%m.%Y')}\n"
            f"Вылет при предупреждениях: {await cfg('max_warns')}")
    rows = [[InlineKeyboardButton(text="🔴 Выключить" if on else "🟢 Включить", callback_data="nrmt:enabled:" + ("0" if on else "1"))],
            [InlineKeyboardButton(text=("• " if str(v) == pct else "") + f"{v}%", callback_data=f"nrmt:percent:{v}") for v in (40, 50, 60, 70, 80)],
            [InlineKeyboardButton(text=("• " if v == a else "") + f"с {v:02d}:00", callback_data=f"nrmt:win_from:{v}") for v in (0, 6, 8, 10)],
            [InlineKeyboardButton(text=("• " if v == b else "") + f"до {v:02d}:00", callback_data=f"nrmt:win_to:{v}") for v in (22, 23, 24)],
            [InlineKeyboardButton(text=("• " if int(await cfg("max_warns")) == v else "") + f"{v} предупр.", callback_data=f"nrmt:max_warns:{v}") for v in (3, 4, 5, 6, 7)],
            [InlineKeyboardButton(text="🔙 К таблице", callback_data="adm_norm")]]
    return text, _ikb(rows)


@router.callback_query(F.data == "nrms")
async def adm_norm_settings(cb: CallbackQuery):
    if not await _guard(cb):
        return
    text, kb = await _settings_view()
    await _edit(cb, text, kb)


@router.callback_query(F.data.regexp(r"^nrmt:(enabled|percent|win_from|win_to|max_warns):\d+$"))
async def adm_norm_set(cb: CallbackQuery):
    if not await _guard(cb):
        return
    _, key, val = cb.data.split(":")
    v = int(val)
    a, b = await window()
    if key == "win_from" and v >= b:
        return await cb.answer("Начало должно быть раньше конца", show_alert=True)
    if key == "win_to" and v <= a:
        return await cb.answer("Конец должен быть позже начала", show_alert=True)
    await set_cfg(key, v)
    text, kb = await _settings_view()
    await _edit(cb, text, kb)


# ---------------------------------------------------------------- подключение
def setup(dp, bot, get_db, is_main_admin, is_publisher, mention_fn, admin_chat_id, topic_id):
    global _bot, _get_db, _is_main_admin, _is_publisher, _mention, _admin_chat, _topic
    _bot, _get_db, _is_main_admin, _is_publisher, _mention, _admin_chat, _topic = bot, get_db, is_main_admin, is_publisher, mention_fn, admin_chat_id, topic_id
    dp.include_router(router)
    # наш роутер — первым, чтобы общие обработчики приватных сообщений не перехватывали ввод часов
    try:
        dp.sub_routers.remove(router)
        dp.sub_routers.insert(0, router)
    except Exception:
        log.warning("не удалось поднять приоритет роутера норматива")
