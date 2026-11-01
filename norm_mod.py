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
from datetime import date, datetime, timedelta, timezone

from aiogram import F, Router
from aiogram.filters import Command
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

DEFAULTS = {"enabled": "1", "percent": "60", "start": "2026-11-02", "max_warns": "3"}
REASONS = {"sick": "🤒 Болезнь / плохое самочувствие", "vip": "🔑 Нет випки (до 7 дней)", "trip": "✈️ Поездка / нет интернета"}

_DDL = """
CREATE TABLE IF NOT EXISTS norm_cfg(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS norm_days(day DATE PRIMARY KEY, finders INT, required INT, found INT, met BOOLEAN, ts TIMESTAMPTZ DEFAULT now());
CREATE TABLE IF NOT EXISTS norm_warns(id SERIAL PRIMARY KEY, user_id BIGINT, day DATE, kind TEXT, note TEXT, by_id BIGINT,
                                      active BOOLEAN DEFAULT TRUE, ts TIMESTAMPTZ DEFAULT now());
CREATE INDEX IF NOT EXISTS norm_warns_u ON norm_warns(user_id, active);
CREATE TABLE IF NOT EXISTS norm_excuse(id SERIAL PRIMARY KEY, user_id BIGINT, from_day DATE, to_day DATE, reason TEXT,
                                       status TEXT DEFAULT 'pending', decided_by BIGINT, ts TIMESTAMPTZ DEFAULT now());
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
    rows = await q("SELECT user_id, COUNT(*) FROM secret_posts WHERE (created_at AT TIME ZONE 'Europe/Moscow')::date=%s GROUP BY user_id", (d,), many=True)
    return {u: int(n) for u, n in (rows or [])}


async def excused_on(d: date) -> set:
    rows = await q("SELECT DISTINCT user_id FROM norm_excuse WHERE status='ok' AND from_day<=%s AND to_day>=%s", (d, d), many=True)
    return {r[0] for r in (rows or [])}


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
            cnt = (await q("SELECT COUNT(*) FROM secret_posts WHERE user_id=%s AND (created_at AT TIME ZONE 'Europe/Moscow')::date BETWEEN %s AND %s", (u, d0, d), one=True))[0]
            if cnt:
                continue
            if await q("SELECT 1 FROM norm_excuse WHERE user_id=%s AND status='ok' AND from_day<=%s AND to_day>=%s", (u, d, d0), one=True):
                continue
            if await q("SELECT 1 FROM norm_warns WHERE user_id=%s AND day BETWEEN %s AND %s AND kind IN ('day','streak')", (u, d0, d), one=True):
                continue
            await add_warn(u, d, "streak", f"{_fmt(d0)}–{_fmt(d)}: ни одной секретки за 3 дня подряд.")
            streak_warned.append(u)
    await q("INSERT INTO norm_days(day,finders,required,found,met) VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING", (d, len(elig), req, total, met))
    lines = [f"📊 <b>Итог дня {_fmt(d)}</b>", f"Искателей в учёте: {len(elig)} · нужно секреток: {req} · найдено: {total}",
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
    start, today = await _start(), today_msk()
    d = start
    while d < today:
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
        txt = f"🌅 <b>Новый день, норматив {_fmt(d)}</b>\nИскателей в учёте: {len(pool)} · общая норма: <b>{req}</b> секр.\nКаждому — 1 секретка. Статус: /norm, таблица: /normtop"
    else:
        if total >= req:
            return
        lag = [u for u in pool if finds.get(u, 0) == 0]
        txt = (f"⏰ <b>Норма дня не выполнена:</b> {total}/{req}. День заканчивается в 00:00 МСК.\n"
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
                if now.hour * 60 + now.minute >= 2:
                    await process_pending()
                if now.hour >= 10 and await flag_once(f"morning:{now.date()}"):
                    await _remind("morning")
                if now.hour >= 20 and await flag_once(f"evening:{now.date()}"):
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
        if await q("SELECT 1 FROM secret_posts WHERE user_id=%s AND (created_at AT TIME ZONE 'Europe/Moscow')::date=%s", (uid, d), one=True):
            break
        if await q("SELECT 1 FROM norm_excuse WHERE user_id=%s AND status='ok' AND from_day<=%s AND to_day>=%s", (uid, d, d), one=True):
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
    ex = await q("SELECT to_day, reason FROM norm_excuse WHERE user_id=%s AND status='ok' AND to_day>=%s ORDER BY to_day DESC LIMIT 1", (uid, d), one=True)
    pend = await q("SELECT 1 FROM norm_excuse WHERE user_id=%s AND status='pending'", (uid,), one=True)
    lines = [f"📋 <b>Норматив секреток · {_fmt(d)}</b>"]
    if d < start:
        lines.append(f"Начнётся с {start.strftime('%d.%m.%Y')}.")
    lines += [
        f"Общая норма дня: <b>{total}/{req}</b> {'✅' if total >= req else '⏳'}",
        f"Ваши секретки сегодня: <b>{mine}</b> {'✅' if mine else '—'}",
        f"Предупреждения: <b>{n}/{limit}</b>",
        f"Дней без секреток подряд: {await _streak_days(uid)}",
    ]
    if ex:
        lines.append(f"🏖 Отгул до {ex[0].strftime('%d.%m')} ({html.escape(ex[1] or '')})")
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
    uid, d = cb.from_user.id, today_msk()
    if not await _allowed(uid):
        return await cb.answer("Нет доступа", show_alert=True)
    rid = (await q("INSERT INTO norm_excuse(user_id,from_day,to_day,reason) VALUES(%s,%s,%s,%s) RETURNING id", (uid, d, d + timedelta(days=n - 1), REASONS[kind]), one=True))[0]
    await cb.message.edit_text(f"🕓 Заявка на отгул ({n} дн.) отправлена главным админам.")
    kb = _kb([[InlineKeyboardButton(text="✅ Одобрить", callback_data=f"nrm:ok:{rid}"), InlineKeyboardButton(text="❌ Отклонить", callback_data=f"nrm:no:{rid}")]])
    try:
        await _bot.send_message(_admin_chat, f"🏖 <b>Заявка на отгул от норматива</b>\n{await _mention(uid)}: {html.escape(REASONS[kind])}\n"
                                f"С {_fmt(d)} по {_fmt(d + timedelta(days=n - 1))} ({n} дн.)", parse_mode="HTML", reply_markup=kb, message_thread_id=_topic)
    except Exception as e:
        log.warning("заявка: %s", e)
    await cb.answer()


@router.callback_query(F.data.regexp(r"^nrm:(ok|no):\d+$"))
async def cb_decide(cb: CallbackQuery):
    if not await _is_main_admin(cb.from_user.id):
        return await cb.answer("Только главные админы", show_alert=True)
    _, act, rid = cb.data.split(":")
    ok = act == "ok"
    r = await q("UPDATE norm_excuse SET status=%s, decided_by=%s WHERE id=%s AND status='pending' RETURNING user_id, from_day, to_day",
                ("ok" if ok else "no", cb.from_user.id, int(rid)), one=True)
    if not r:
        return await cb.answer("Уже решено")
    await cb.message.edit_text(cb.message.html_text + f"\n\n{'✅ Одобрено' if ok else '❌ Отклонено'}: {html.escape(cb.from_user.full_name)}", parse_mode="HTML")
    try:
        await _bot.send_message(r[0], f"{'✅ Отгул одобрен' if ok else '❌ Отгул отклонён'} ({_fmt(r[1])}–{_fmt(r[2])}).")
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
             "<code>reset UID</code> — сбросить предупреждения\n<code>excuse UID дней [причина]</code> — выдать отгул\n"
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
    if c == "excuse" and len(a) > 2 and a[1].isdigit() and a[2].isdigit():
        d = today_msk()
        await q("INSERT INTO norm_excuse(user_id,from_day,to_day,reason,status,decided_by) VALUES(%s,%s,%s,%s,'ok',%s)",
                (int(a[1]), d, d + timedelta(days=int(a[2]) - 1), " ".join(a[3:])[:100] or "по решению админа", message.from_user.id))
        return await message.answer(f"Отгул выдан: {a[2]} дн.")
    if c == "run":
        await process_pending()
        return await message.answer("Пересчитано.")
    if c == "status":
        return await message.answer(f"Включён: {await cfg('enabled')}\nПроцент: {await cfg('percent')}\nСтарт: {await cfg('start')}\nЛимит предупреждений: {await cfg('max_warns')}\nИскателей: {len(await finders())}")
    await message.answer(help_, parse_mode="HTML")


# ---------------------------------------------------------------- подключение
def setup(dp, bot, get_db, is_main_admin, is_publisher, mention_fn, admin_chat_id, topic_id):
    global _bot, _get_db, _is_main_admin, _is_publisher, _mention, _admin_chat, _topic
    _bot, _get_db, _is_main_admin, _is_publisher, _mention, _admin_chat, _topic = bot, get_db, is_main_admin, is_publisher, mention_fn, admin_chat_id, topic_id
    dp.include_router(router)
