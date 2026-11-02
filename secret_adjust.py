# -*- coding: utf-8 -*-
"""Ручная выдача и снятие секреток в статистике — только владелец бота (ADMIN_ID).
Кнопки видны в панели секреток всем главным админам, но для остальных отвечают отказом.

• Выдать: создаёт N записей в secret_posts (без поста в канале, помечены «ручная выдача», скрыты от публикаторов).
  Можно задним числом: сегодня, вчера или любая дата (ДД.ММ или ДД.ММ.ГГГГ). Время записи — 12:00 МСК.
• Убрать: удаляет N записей этого человека (старые или новые) только из статистики.
После любого изменения пересчитываются итоги уже закрытых дней, поэтому рекорд общего числа за день тоже обновляется.
Сообщения в канале не трогаются."""
import asyncio
import html
import logging
from datetime import date, datetime, timedelta, timezone

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

log = logging.getLogger("secret_adjust")
router = Router()

MSK = timezone(timedelta(hours=3))
_owner = 0
_get_db = None
_resolve = None      # resolve_user_refs(text) -> (ids, failed)
_label = None        # get_user_label(uid) -> str
MAX_N = 200
MANUAL_CHANNEL = "manual"
MANUAL_TYPE = None          # без типа: в статистике типов не участвуют


class SAdj(StatesGroup):
    user = State()
    count = State()
    day = State()
    side = State()
    confirm = State()


def _kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _cancel_row():
    return [InlineKeyboardButton(text="✖️ Отмена", callback_data="sadj_cancel")]


def _db_run(fn):
    with _get_db() as conn:
        with conn.cursor() as cur:
            return fn(cur)


async def _run(fn):
    return await asyncio.to_thread(_db_run, fn)


async def _deny(cb: CallbackQuery) -> bool:
    if cb.from_user.id != _owner:
        await cb.answer("Эти кнопки доступны только владельцу бота", show_alert=True)
        return True
    return False


async def _stat(uid) -> int:
    def f(cur):
        cur.execute("SELECT COUNT(*) FROM secret_posts WHERE user_id = %s", (uid,))
        return cur.fetchone()[0]
    return await _run(f)


def _fmt_day(d: date) -> str:
    return d.strftime("%d.%m.%Y")


def _parse_day(txt: str):
    """ДД.ММ или ДД.ММ.ГГГГ → date (не в будущем). None, если не разобрано."""
    txt = (txt or "").strip()
    today = datetime.now(MSK).date()
    for fmt in ("%d.%m.%Y", "%d.%m"):
        try:
            d = datetime.strptime(txt, fmt).date()
        except ValueError:
            continue
        if fmt == "%d.%m":
            d = d.replace(year=today.year)
            if d > today:
                d = d.replace(year=today.year - 1)
        if d > today:
            return None
        return d
    return None


# ---------------------------------------------------------------- меню
@router.callback_query(F.data == "sadj_menu")
async def sadj_menu(cb: CallbackQuery, state: FSMContext):
    if await _deny(cb):
        return
    await state.clear()
    await cb.message.answer(
        "🛠 <b>Секретки вручную</b>\n\n"
        "➕ <b>Выдать</b> — добавить человеку секретки в статистику (без поста в канале), можно задним числом.\n"
        "➖ <b>Убрать</b> — вычесть секретки из статистики (например, тестовые).",
        parse_mode="HTML",
        reply_markup=_kb([[InlineKeyboardButton(text="➕ Выдать секретки", callback_data="sadj_add"),
                           InlineKeyboardButton(text="➖ Убрать секретки", callback_data="sadj_del")],
                          _cancel_row()]))
    await cb.answer()


@router.callback_query(F.data == "sadj_cancel")
async def sadj_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await cb.message.edit_text("Отменено.")
    except Exception:
        pass
    await cb.answer()


@router.callback_query(F.data.in_({"sadj_add", "sadj_del"}))
async def sadj_start(cb: CallbackQuery, state: FSMContext):
    if await _deny(cb):
        return
    mode = "add" if cb.data == "sadj_add" else "del"
    await state.clear()
    await state.update_data(mode=mode)
    await state.set_state(SAdj.user)
    what = "выдать секретки" if mode == "add" else "убрать секретки из статистики"
    await cb.message.answer(f"Кому {what}?\nПришлите <b>ID</b> или <b>@ник</b> одного человека.",
                            parse_mode="HTML", reply_markup=_kb([_cancel_row()]))
    await cb.answer()


# ---------------------------------------------------------------- шаги
@router.message(SAdj.user, F.text)
async def sadj_user(message: Message, state: FSMContext):
    if message.from_user.id != _owner:
        return
    ids, failed = await _resolve(message.text)
    if not ids or len(ids) != 1:
        return await message.answer("Нужен один человек: пришлите один ID или один @ник.", reply_markup=_kb([_cancel_row()]))
    uid = ids[0]
    await state.update_data(uid=uid)
    await state.set_state(SAdj.count)
    label = html.unescape(await _label(uid))
    mode = (await state.get_data()).get("mode")
    now_stat = await _stat(uid)
    await message.answer(f"Человек: <b>{html.escape(label)}</b> ({uid})\nСейчас в статистике: <b>{now_stat}</b> секр.\n\n"
                         f"Сколько секреток {'выдать' if mode == 'add' else 'убрать'}? Пришлите число (1–{MAX_N}).",
                         parse_mode="HTML", reply_markup=_kb([_cancel_row()]))


@router.message(SAdj.count, F.text)
async def sadj_count(message: Message, state: FSMContext):
    if message.from_user.id != _owner:
        return
    txt = (message.text or "").strip()
    if not txt.isdigit() or not (1 <= int(txt) <= MAX_N):
        return await message.answer(f"Пришлите целое число от 1 до {MAX_N}.", reply_markup=_kb([_cancel_row()]))
    n = int(txt)
    await state.update_data(n=n)
    if (await state.get_data()).get("mode") == "add":
        await state.set_state(SAdj.day)
        today = datetime.now(MSK).date()
        return await message.answer(
            "За какой день засчитать?\nМожно выбрать кнопкой или написать дату: <code>ДД.ММ</code> или <code>ДД.ММ.ГГГГ</code>.",
            parse_mode="HTML",
            reply_markup=_kb([[InlineKeyboardButton(text=f"Сегодня {today.strftime('%d.%m')}", callback_data="sadj_day:0"),
                               InlineKeyboardButton(text=f"Вчера {(today - timedelta(days=1)).strftime('%d.%m')}", callback_data="sadj_day:1")],
                              _cancel_row()]))
    await state.set_state(SAdj.side)
    await message.answer("Какие убрать?",
                         reply_markup=_kb([[InlineKeyboardButton(text="🕰 Самые старые", callback_data="sadj_side:old"),
                                            InlineKeyboardButton(text="🆕 Самые новые", callback_data="sadj_side:new")],
                                           _cancel_row()]))


@router.callback_query(SAdj.day, F.data.startswith("sadj_day:"))
async def sadj_day_btn(cb: CallbackQuery, state: FSMContext):
    if await _deny(cb):
        return
    back = int(cb.data.split(":")[1])
    d = datetime.now(MSK).date() - timedelta(days=back)
    await state.update_data(day=d.isoformat())
    await state.set_state(SAdj.confirm)
    await _ask_confirm(cb.message, state)
    await cb.answer()


@router.message(SAdj.day, F.text)
async def sadj_day_text(message: Message, state: FSMContext):
    if message.from_user.id != _owner:
        return
    d = _parse_day(message.text)
    if not d:
        return await message.answer("Не разобрал дату. Напишите <code>ДД.ММ</code> или <code>ДД.ММ.ГГГГ</code>, не позже сегодняшнего дня.",
                                    parse_mode="HTML", reply_markup=_kb([_cancel_row()]))
    await state.update_data(day=d.isoformat())
    await state.set_state(SAdj.confirm)
    await _ask_confirm(message, state)


@router.callback_query(SAdj.side, F.data.in_({"sadj_side:old", "sadj_side:new"}))
async def sadj_side(cb: CallbackQuery, state: FSMContext):
    if await _deny(cb):
        return
    await state.update_data(side=cb.data.split(":")[1])
    await state.set_state(SAdj.confirm)
    await _ask_confirm(cb.message, state)
    await cb.answer()


async def _ask_confirm(msg, state: FSMContext):
    d = await state.get_data()
    uid, n, mode = d["uid"], d["n"], d["mode"]
    label = html.unescape(await _label(uid))
    if mode == "add":
        day = date.fromisoformat(d["day"])
        text = (f"Выдать <b>{n}</b> секр. человеку <b>{html.escape(label)}</b> за <b>{_fmt_day(day)}</b>?\n"
                "Они попадут в общую статистику, но не в норму, рекорды и статистику по типам.")
    else:
        have = await _stat(uid)
        if n > have:
            n = have
            await state.update_data(n=n)
        side = "самые старые" if d.get("side") == "old" else "самые новые"
        text = (f"Убрать <b>{n}</b> секр. ({side}) у <b>{html.escape(label)}</b>?\n"
                f"Сейчас: {have}, станет: {have - n}.\nЭто только из статистики, сообщения в канале не трогаются.")
    await msg.answer(text, parse_mode="HTML",
                     reply_markup=_kb([[InlineKeyboardButton(text="✅ Подтвердить", callback_data="sadj_ok"),
                                        InlineKeyboardButton(text="✖️ Отмена", callback_data="sadj_cancel")]]))


@router.callback_query(SAdj.confirm, F.data == "sadj_ok")
async def sadj_ok(cb: CallbackQuery, state: FSMContext):
    if await _deny(cb):
        return
    d = await state.get_data()
    await state.clear()
    uid, n, mode = d["uid"], d["n"], d["mode"]
    affected = []
    if mode == "add":
        day = date.fromisoformat(d["day"])
        stamp = datetime(day.year, day.month, day.day, 12, 0, tzinfo=MSK)
        affected = [day]

        def f(cur):
            for _ in range(n):
                cur.execute("INSERT INTO secret_posts (user_id, channel_id, message_id, secret_type, base_text, is_expired, created_at) "
                            "VALUES (%s, %s, 0, %s, %s, TRUE, %s)", (uid, MANUAL_CHANNEL, MANUAL_TYPE, f"вручную от {cb.from_user.id}", stamp))
        await _run(f)
        res = f"✅ Выдано <b>{n}</b> секр. за {_fmt_day(day)}."
    else:
        side = "ASC" if d.get("side") == "old" else "DESC"

        def pick(cur):
            cur.execute(f"SELECT id, (created_at AT TIME ZONE 'Europe/Moscow')::date FROM secret_posts WHERE user_id = %s "
                        f"ORDER BY created_at {side}, id {side} LIMIT %s", (uid, n))
            return cur.fetchall()
        rows = await _run(pick)
        ids = [r[0] for r in rows]
        affected = [r[1] for r in rows]

        def delete(cur):
            if not ids:
                return 0
            cur.execute("DELETE FROM secret_posts WHERE id = ANY(%s)", (ids,))
            return cur.rowcount
        removed = await _run(delete)
        res = f"✅ Убрано <b>{removed}</b> секр. из статистики."

    try:
        import norm_mod
        await norm_mod.refresh_days(affected)
    except Exception as e:
        log.warning("пересчёт дней: %s", e)
    total = await _stat(uid)
    label = html.unescape(await _label(uid))
    text = f"{res}\nЧеловек: <b>{html.escape(label)}</b> ({uid}). Теперь в статистике: <b>{total}</b>."
    try:
        await cb.message.edit_text(text, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, parse_mode="HTML")
    await cb.answer()


# ---------------------------------------------------------------- подключение
async def init():
    """Миграция: у уже выданных вручную секреток убираем тип и пересчитываем итоги дней, где они были."""
    def f(cur):
        cur.execute("UPDATE secret_posts SET secret_type = NULL WHERE channel_id = %s AND secret_type IS NOT NULL", (MANUAL_CHANNEL,))
        cur.execute("SELECT DISTINCT (created_at AT TIME ZONE 'Europe/Moscow')::date FROM secret_posts WHERE channel_id = %s", (MANUAL_CHANNEL,))
        return [r[0] for r in cur.fetchall()]
    days = await _run(f)
    try:
        import norm_mod
        await norm_mod.refresh_days(days)
    except Exception as e:
        log.warning("миграция ручных секреток: %s", e)


def setup(dp, get_db, owner_id, resolve_user_refs, get_user_label):
    global _get_db, _owner, _resolve, _label
    _get_db, _owner, _resolve, _label = get_db, owner_id, resolve_user_refs, get_user_label
    dp.include_router(router)
    try:
        dp.sub_routers.remove(router)
        dp.sub_routers.insert(0, router)
    except Exception:
        pass
