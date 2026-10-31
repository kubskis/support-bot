# -*- coding: utf-8 -*-
"""Надзор: перенос статистики из Iris по пересланному сообщению «кто я» с одобрением главного админа.

Поток: в группе «перенести стату» → кнопка в ЛС бота → человек пересылает карточку Iris →
бот разбирает цифры, проверяет, что сообщение переслано от бота Iris и что в карточке ссылка именно на него →
главным админам в ЛС приходит заявка с кнопками → после одобрения цифры добавляются.
"""
import logging
import os
import re
from datetime import datetime, timezone

from aiogram import BaseMiddleware, F
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import iris_mod as m
from iris_mod import cmd, q, reply, mention, esc

log = logging.getLogger("iris_import")
m.DEFAULT_DK.update({"импортстаты": 0})
_IRIS_NAMES = {x.strip().lower().lstrip("@") for x in os.getenv("IRIS_BOT_USERNAMES", "iris_cm_bot,iriscm_bot,iris_chat_manager_bot").split(",") if x.strip()}
_wait = {}          # user_id -> chat_id (ждём пересылку)
_bot_username = None
MAX_IMPORT = 5_000_000

_DDL = """
CREATE TABLE IF NOT EXISTS im_import(id SERIAL PRIMARY KEY, chat_id BIGINT, user_id BIGINT, msgs BIGINT, first_seen TIMESTAMPTZ,
  raw TEXT, check_note TEXT, status TEXT DEFAULT 'pending', by_admin BIGINT, ts TIMESTAMPTZ DEFAULT now());
"""


async def init_tables():
    await q(_DDL)


# ---------------------------------------------------------------- разбор карточки Iris
def _num(tok):
    t = tok.strip().lower().replace(" ", " ")
    mm = re.match(r"^([\d\s]+(?:[.,]\d+)?)\s*([kк]|тыс|m|м|млн)?$", t)
    if not mm:
        return None
    base = float(mm.group(1).replace(" ", "").replace(",", "."))
    suf = mm.group(2)
    if suf in ("k", "к", "тыс"):
        base *= 1000
    elif suf in ("m", "м", "млн"):
        base *= 1_000_000
    return int(round(base))


def parse_card(text):
    """Достаёт 'весь' счётчик сообщений и дату первого появления."""
    total = None
    mm = re.search(r"^\W*Актив\s*\([^)\n]*\)\s*:\s*([^\n]+)", text, re.I | re.M)
    if mm:
        parts = [p.strip() for p in mm.group(1).split("|") if p.strip()]
        if parts:
            total = _num(parts[-1])
    if total is None:
        mm = re.search(r"(?:Сообщений|Всего сообщений)[^:\n]*:\s*([\d\s.,kк]+)", text, re.I)
        if mm:
            total = _num(mm.group(1))
    first = None
    mm = re.search(r"Первое появление:\s*(\d{2})\.(\d{2})\.(\d{4})", text)
    if mm:
        try:
            first = datetime(int(mm.group(3)), int(mm.group(2)), int(mm.group(1)), tzinfo=timezone.utc)
        except ValueError:
            first = None
    return total, first


def card_user_ids(msg: Message):
    ids = set()
    for ent in (msg.entities or []) + (msg.caption_entities or []):
        if ent.type == "text_mention" and ent.user:
            ids.add(ent.user.id)
        elif ent.type == "text_link" and ent.url:
            mm = re.search(r"user\?id=(\d+)", ent.url) or re.search(r"tg://openmessage\?user_id=(\d+)", ent.url)
            if mm:
                ids.add(int(mm.group(1)))
    return ids


def forward_note(msg: Message):
    """('ok'|'bad'|'none', описание)"""
    fo = getattr(msg, "forward_origin", None)
    if not fo:
        return "none", "сообщение не переслано (скопировано вручную)"
    u = getattr(fo, "sender_user", None)
    if u and u.is_bot:
        un = (u.username or "").lower()
        if un in _IRIS_NAMES or "iris" in un:
            return "ok", f"переслано от @{u.username}"
        return "bad", f"переслано от другого бота @{u.username}"
    if u:
        return "bad", f"переслано от человека {u.full_name}, а не от бота"
    return "bad", "источник пересылки скрыт"


# ---------------------------------------------------------------- команды в группе
async def _admin_ids():
    ids = set()
    try:
        rows = await q("SELECT admin_id FROM main_admins", many=True)
        ids |= {r[0] for r in rows if r[0]}
    except Exception:
        pass
    try:
        o = int(os.getenv("ADMIN_ID", "0"))
        if o:
            ids.add(o)
    except ValueError:
        pass
    return ids


@cmd(["перенести стату", "перенести статистику", "импорт статы", "перенос статы"], "импортстаты")
async def c_import_start(msg, args, rank):
    global _bot_username
    if not _bot_username:
        _bot_username = (await m._bot.get_me()).username
    exist = await q("SELECT status FROM im_import WHERE chat_id=%s AND user_id=%s AND status IN ('pending','approved')", (msg.chat.id, msg.from_user.id), one=True)
    if exist:
        return await reply(msg, "Заявка уже подана и ждёт решения." if exist[0] == "pending" else "Статистика уже была перенесена.")
    payload = "imp_" + str(msg.chat.id).replace("-", "n")
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📥 Перенести в ЛС бота", url=f"https://t.me/{_bot_username}?start={payload}")]])
    await reply(msg, "Откройте бота в личных сообщениях и перешлите ему вашу карточку «кто я» от Iris. Заявку подтвердит главный админ.", reply_markup=kb)


# ---------------------------------------------------------------- ЛС: приём пересылки
class ImportMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        if isinstance(event, Message) and event.chat.type == "private" and event.from_user:
            try:
                if await self._handle(event):
                    return None
            except Exception:
                log.exception("import middleware")
        return await handler(event, data)

    async def _handle(self, msg: Message) -> bool:
        uid = msg.from_user.id
        t = (msg.text or "").strip()
        mm = re.match(r"^/start\s+imp_(n?\d+)$", t)
        if mm:
            cid = int(mm.group(1).replace("n", "-"))
            if not await q("SELECT 1 FROM im_chats WHERE chat_id=%s AND enabled", (cid,), one=True):
                await msg.answer("Этот чат не подключён к «Надзору».")
                return True
            _wait[uid] = cid
            await msg.answer("📥 <b>Перенос статистики из Iris</b>\n\nОтправьте Iris в нужном чате <code>кто я</code>, затем <b>перешлите</b> его ответ сюда "
                             "(именно пересылкой, не копированием — так бот проверит, что это настоящий ответ Iris). Отмена: <code>отмена</code>.", parse_mode="HTML")
            return True
        if uid not in _wait:
            return False
        if t.lower() in ("отмена", "/cancel"):
            _wait.pop(uid, None)
            await msg.answer("Перенос отменён.")
            return True
        cid = _wait[uid]
        text = msg.text or msg.caption or ""
        total, first = parse_card(text)
        if total is None:
            await msg.answer("Не нашёл в сообщении строку «Актив (д|н|м|весь): …». Перешлите карточку «кто я» от Iris целиком.")
            return True
        if total <= 0 or total > MAX_IMPORT:
            await msg.answer("Число сообщений вне допустимых пределов.")
            return True
        notes = []
        fstate, fnote = forward_note(msg)
        notes.append(("✅ " if fstate == "ok" else "⚠️ ") + fnote)
        ids = card_user_ids(msg)
        if ids and uid not in ids:
            await msg.answer("❌ Эта карточка относится к другому пользователю. Нужна карточка о вас: отправьте Iris «кто я» от своего имени.")
            _wait.pop(uid, None)
            return True
        notes.append("✅ в карточке ссылка на вас" if uid in ids else "⚠️ в карточке нет ссылки на пользователя (личность не подтверждена)")
        if await q("SELECT 1 FROM im_import WHERE chat_id=%s AND user_id=%s AND status IN ('pending','approved')", (cid, uid), one=True):
            _wait.pop(uid, None)
            await msg.answer("Заявка по этому чату уже есть.")
            return True
        r = await q("INSERT INTO im_import(chat_id,user_id,msgs,first_seen,raw,check_note) VALUES(%s,%s,%s,%s,%s,%s) RETURNING id",
                    (cid, uid, total, first, text[:3000], "\n".join(notes)), one=True)
        _wait.pop(uid, None)
        await msg.answer("✅ Заявка отправлена главным админам. Когда её рассмотрят, я напишу вам.")
        await _notify_admins(r[0], cid, msg.from_user, total, first, notes, text)
        return True


async def _notify_admins(rid, cid, user, total, first, notes, raw):
    try:
        ch = await m._bot.get_chat(cid)
        title = ch.title
    except Exception:
        title = str(cid)
    cur = await q("SELECT msgs FROM im_members WHERE chat_id=%s AND user_id=%s", (cid, user.id), one=True)
    body = (f"📥 <b>Заявка на перенос статистики #{rid}</b>\nЧат: {esc(title)}\nУчастник: {mention(user.id, user.full_name)} (<code>{user.id}</code>)\n\n"
            f"Из Iris: сообщений <b>{total}</b>" + (f", первое появление {first.strftime('%d.%m.%Y')}" if first else "") +
            f"\nСейчас у нас: {cur[0] if cur else 0}\n\nПроверка:\n" + "\n".join(esc(n) for n in notes) +
            f"\n\n<blockquote>{esc(raw[:700])}</blockquote>")
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Одобрить", callback_data=f"imi:ok:{rid}"),
                                                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"imi:no:{rid}")]])
    for aid in await _admin_ids():
        try:
            await m._bot.send_message(aid, body, parse_mode="HTML", reply_markup=kb)
        except Exception as e:
            log.warning("не удалось отправить заявку админу %s: %s", aid, e)


@m.router.callback_query(F.data.startswith("imi:"))
async def cb_import(cb: CallbackQuery):
    if cb.from_user.id not in await _admin_ids():
        return await cb.answer("Только для главных админов.", show_alert=True)
    _, act, rid = cb.data.split(":")
    rid = int(rid)
    r = await q("UPDATE im_import SET status=%s, by_admin=%s WHERE id=%s AND status='pending' RETURNING chat_id,user_id,msgs,first_seen",
                ("approved" if act == "ok" else "rejected", cb.from_user.id, rid), one=True)
    if not r:
        return await cb.answer("Заявка уже обработана.", show_alert=True)
    cid, uid, total, first = r
    if act == "ok":
        await q("""INSERT INTO im_members(chat_id,user_id,name,msgs,first_seen) VALUES(%s,%s,%s,%s,coalesce(%s,now()))
                   ON CONFLICT(chat_id,user_id) DO UPDATE SET msgs=im_members.msgs+%s,
                   first_seen=LEAST(im_members.first_seen, coalesce(%s, im_members.first_seen))""",
                (cid, uid, str(uid), total, first, total, first))
        await m.logact(cid, cb.from_user.id, "import_stats", uid, str(total))
        verdict = f"✅ Одобрено ({total} сообщ.)"
        note = f"✅ Ваша статистика из Iris перенесена: +{total} сообщений."
    else:
        verdict = "❌ Отклонено"
        note = "❌ Заявку на перенос статистики отклонили."
    try:
        await cb.message.edit_text(cb.message.html_text + f"\n\n<b>{verdict}</b> — {cb.from_user.full_name}", parse_mode="HTML")
    except Exception:
        pass
    try:
        await m._bot.send_message(uid, note)
    except Exception:
        pass
    await cb.answer("Готово")


def install(dp):
    dp.message.outer_middleware(ImportMiddleware())
