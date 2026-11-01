# -*- coding: utf-8 -*-
"""Надзор, этап D: команды справочника Iris, которых не хватало (для одного чата):
наказания и сроки по умолчанию, разделы ДК и меню мдк, фильтры ссылок/капса/стикеров, антиспам, чистка,
тг-админы, журнал входов/выходов, личная статистика, чеки, МРП-экспорт/глобальные МРП, кинг/пиу и др."""
import asyncio
import logging
import random
import re
import secrets
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

from aiogram import F
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

import iris_mod as m
from iris_mod import (cmd, q, reply, mention, esc, resolve_target, member_name, get_cfg, set_cfg, fmt_dur,
                      parse_dur, get_rank, logact)

log = logging.getLogger("iris_full")
_MSK = timezone(timedelta(hours=3))
_STARTED = time.time()

m.DEFAULT_DK.update({
    "восстановить": 0, "самоснятие": 1, "мойонлайн": 0, "проверкамута": 1, "гбуправление": 1, "команды": 5,
    "чатссылка": 4, "тгадмин": 4, "обновитьчат": 4, "журналвходов": 1, "фильтры": 4, "антиспам": 4,
    "чистка": 4, "списокчистки": 2, "мойстат": 0, "население": 0, "актив_бота": 0, "кинг": 0, "чек": 0,
    "гмрп": 5, "логдк": 4,
})

# разделы ДК (как в Iris) -> коды команд бота
SECTIONS = {
    "модерирование": ["модер", "снять", "понизить", "повысить", "кто_админ", "модерлог", "снятьвсех", "кторанг", "восстановить", "самоснятие", "мойонлайн", "завещание", "наследство"],
    "наказания": ["варн", "мут", "кик", "бан", "разбан", "снятьварн", "варны", "банлист", "причина", "амнистия", "проверкамута"],
    "триггеры": ["триггер"],
    "доступ команд": ["дк", "лдк", "сброс", "логдк", "команды"],
    "чистка": ["пург", "смс", "кикнеактив", "чистка", "списокчистки"],
    "настройка чата": ["настройка", "приветствие", "правила_set", "антирейд", "ссылки", "защитавхода", "слоумод", "фильтры", "антиспам", "чатссылка", "тгадмин", "обновитьчат", "автозаявки", "автокик", "журналвходов"],
    "сетка": ["сетка", "сеткаинфо", "глобан"],
    "анкета": ["анкета", "кто_я", "ачивки"],
    "статистика": ["стата", "топ", "чатинфо", "онлайн", "стачас", "топупом", "население", "мойстат", "актив_бота", "импортстаты"],
    "голосование": ["голосование", "гб", "гк", "гбуправление"],
    "антиспам-база": ["скам"],
    "бонусы": ["анонимка", "банхаммер", "золото"],
    "развлекательные": ["утилиты", "ид", "кинг", "чек"],
    "дуэли": ["дуэль", "золотаядуэль"],
    "кланы": ["клан"],
    "кружки": ["кружок"],
    "отношения": [],
    "браки": ["брак", "поженить", "развести", "брачныенастройки"],
    "репутация": ["репа"],
    "награды": ["награда", "награды"],
    "закладки": ["закладка"],
    "заметки": ["заметка"],
    "таймеры": ["таймер"],
    "карта": ["карта"],
    "биржа": ["биржа"],
    "другое": ["экономика", "ферма", "передать", "рулетка", "кубы", "эконастройка", "выдать", "рп", "мрп", "гмрп", "созвать", "вип", "випнастройка", "випрп"],
}
m.DK_SECTIONS.update(SECTIONS)
_PROTECT = {"дк", "лдк", "сброс", "команды", "логдк"}   # от самоблокировки

_DDL = """
CREATE TABLE IF NOT EXISTS im_jl(id SERIAL PRIMARY KEY, chat_id BIGINT, user_id BIGINT, kind TEXT, inviter BIGINT, ts TIMESTAMPTZ DEFAULT now());
CREATE INDEX IF NOT EXISTS im_jl_ix ON im_jl(chat_id,kind,ts);
CREATE TABLE IF NOT EXISTS im_gmrp(cmd TEXT PRIMARY KEY, verb TEXT);
CREATE TABLE IF NOT EXISTS im_cheque(code TEXT PRIMARY KEY, chat_id BIGINT, owner BIGINT, amount BIGINT, for_user BIGINT, used_by BIGINT, ts TIMESTAMPTZ DEFAULT now());
"""


async def init_tables():
    await q(_DDL)


def _sec_name(args):
    t = " ".join(args).lower().strip()
    if t in SECTIONS:
        return t
    for k in SECTIONS:
        if k.startswith(t) and t:
            return k
    return None


# ================================================================ наказания: сроки по умолчанию
async def _set_dur(msg, args, key, label):
    d = parse_dur(args[0]) if args else None
    if d is None:
        return await reply(msg, f"Пример: <code>{label} 7д</code> (30м, 2ч, 3д, 1н, 1мес, навсегда)")
    await set_cfg(msg.chat.id, **{key: d or None})
    await reply(msg, f"{label.capitalize()}: <b>{fmt_dur(d)}</b>")


@cmd(["бан период"], "настройка")
async def c_ban_period(msg, args, rank):
    await _set_dur(msg, args, "ban_default", "бан период")


@cmd(["мут период"], "настройка")
async def c_mute_period(msg, args, rank):
    d = parse_dur(args[0]) if args else None
    if not d:
        return await reply(msg, "Пример: <code>мут период 1д</code> (срок мута по умолчанию, по умолчанию неделя)")
    await set_cfg(msg.chat.id, mute_default=d)
    await reply(msg, f"Срок мута по умолчанию: <b>{fmt_dur(d)}</b>")


@cmd(["варны период"], "настройка")
async def c_warn_period(msg, args, rank):
    d = parse_dur(args[0]) if args else None
    if d is None:
        return await reply(msg, "Пример: <code>варны период 30д</code> (срок жизни варна; навсегда — не сгорают)")
    await set_cfg(msg.chat.id, warn_period=d or None)
    await reply(msg, f"Срок жизни варна: <b>{fmt_dur(d)}</b>")


@cmd(["проверить мут", "проверка мута"], "проверкамута")
async def c_check_mute(msg, args, rank):
    uid, name, _ = await resolve_target(msg, args)
    if not uid:
        uid, name = msg.from_user.id, msg.from_user.full_name
    r = await q("""SELECT until,reason FROM im_punish WHERE chat_id=%s AND user_id=%s AND kind='mute' AND active
                   AND (until IS NULL OR until>now()) ORDER BY ts DESC LIMIT 1""", (msg.chat.id, uid), one=True)
    if not r:
        return await reply(msg, f"{mention(uid, name)}: мута нет.")
    left = "навсегда" if not r[0] else "до " + r[0].astimezone(_MSK).strftime("%d.%m.%Y %H:%M") + " МСК"
    await reply(msg, f"🔇 {mention(uid, name)} в муте {left}." + (f"\nПричина: {esc(r[1])}" if r[1] else ""))


@cmd(["снять варны"], "снятьварн")
async def c_unwarn_n(msg, args, rank):
    n, rest = 1, list(args)
    for i, t in enumerate(rest):
        if t.isdigit() and len(t) <= 2:
            n = max(1, int(t))
            rest.pop(i)
            break
    uid, name, _ = await resolve_target(msg, rest)
    if not uid:
        return await reply(msg, "Пример: <code>снять варны 2 @ник</code>")
    w = await m.active_warns(msg.chat.id, uid)
    if not w:
        return await reply(msg, "Активных варнов нет.")
    for item in w[-n:]:
        await q("UPDATE im_punish SET active=FALSE WHERE id=%s", (item[0],))
    await logact(msg.chat.id, msg.from_user.id, "unwarn", uid, str(n))
    await reply(msg, f"✅ Снято варнов: {min(n, len(w))}. У {mention(uid, name)} осталось: {max(len(w) - n, 0)}")


@cmd(["снять варн номер"], "снятьварн")
async def c_unwarn_num(msg, args, rank):
    num, rest = None, list(args)
    for i, t in enumerate(rest):
        if t.isdigit() and len(t) <= 2:
            num = int(t)
            rest.pop(i)
            break
    uid, name, _ = await resolve_target(msg, rest)
    if not uid or not num:
        return await reply(msg, "Пример: <code>снять варн номер 2 @ник</code> (номер из списка «варны @ник»)")
    w = await m.active_warns(msg.chat.id, uid)
    if num < 1 or num > len(w):
        return await reply(msg, f"У {mention(uid, name)} варнов: {len(w)}.")
    await q("UPDATE im_punish SET active=FALSE WHERE id=%s", (w[num - 1][0],))
    await reply(msg, f"✅ Варн №{num} снят с {mention(uid, name)}.")


@cmd(["восстановить создателя"], "восстановить")
async def c_restore_creator(msg, args, rank):
    try:
        admins = await m._bot.get_chat_administrators(msg.chat.id)
    except Exception as e:
        return await reply(msg, m.human_error(e))
    cr = next((a for a in admins if a.status == "creator"), None)
    if not cr:
        return await reply(msg, "Создателя чата не вижу: возможно, он скрыт как анонимный админ. Если так — выключите анонимность в Telegram и повторите.")
    await q("""INSERT INTO im_ranks(chat_id,user_id,rank,by_id) VALUES(%s,%s,5,%s)
               ON CONFLICT(chat_id,user_id) DO UPDATE SET rank=5""", (msg.chat.id, cr.user.id, msg.from_user.id))
    await reply(msg, f"👑 Создателю {mention(cr.user.id, cr.user.full_name)} возвращён ранг 5.")


@cmd(["!снимаю полномочия", "снимаю полномочия"], "самоснятие")
async def c_resign(msg, args, rank):
    await q("DELETE FROM im_ranks WHERE chat_id=%s AND user_id=%s", (msg.chat.id, msg.from_user.id))
    await logact(msg.chat.id, msg.from_user.id, "resign", msg.from_user.id)
    await reply(msg, f"{mention(msg.from_user.id, msg.from_user.full_name)} сложил(а) полномочия модератора.")


@cmd(["+мой онлайн"], "мойонлайн")
async def c_myonline_on(msg, args, rank):
    await q("UPDATE im_members SET profile=coalesce(profile,'{}'::jsonb)||'{\"show_online\":\"1\"}'::jsonb WHERE chat_id=%s AND user_id=%s",
            (msg.chat.id, msg.from_user.id))
    await reply(msg, "🟢 Теперь в «кто админ» виден ваш онлайн.")


@cmd(["-мой онлайн"], "мойонлайн")
async def c_myonline_off(msg, args, rank):
    await q("UPDATE im_members SET profile=coalesce(profile,'{}'::jsonb)-'show_online' WHERE chat_id=%s AND user_id=%s",
            (msg.chat.id, msg.from_user.id))
    await reply(msg, "Ваш онлайн в «кто админ» скрыт.")


# ---- управление голосованиями за бан
@cmd(["гб список"], "гб")
async def c_gb_list(msg, args, rank):
    rows = await q("SELECT target,need,voters FROM im_votes WHERE chat_id=%s AND NOT done ORDER BY msg_id DESC LIMIT 10", (msg.chat.id,), many=True)
    if not rows:
        return await reply(msg, "Активных голосований нет.")
    out = ["<b>Голосования за бан</b>"]
    for t, need, v in rows:
        out.append(f"{mention(t, await member_name(msg.chat.id, t))} — {len(v or [])}/{need}")
    await reply(msg, "\n".join(out))


@cmd(["гб стоп"], "гбуправление")
async def c_gb_stop(msg, args, rank):
    r = await q("UPDATE im_votes SET done=TRUE WHERE chat_id=%s AND NOT done RETURNING 1", (msg.chat.id,), many=True)
    await reply(msg, f"Остановлено голосований: {len(r or [])}.")


@cmd(["гб инфо"], "гб")
async def c_gb_info(msg, args, rank):
    r = await q("SELECT target,need,voters FROM im_votes WHERE chat_id=%s AND NOT done ORDER BY msg_id DESC LIMIT 1", (msg.chat.id,), one=True)
    if not r:
        return await reply(msg, "Активных голосований нет.")
    t, need, v = r
    names = ", ".join(esc(await member_name(msg.chat.id, x)) for x in (v or [])) or "пока никто"
    await reply(msg, f"🗳 Голосование за бан {mention(t, await member_name(msg.chat.id, t))}: {len(v or [])}/{need}\nЗа: {names}")


# ================================================================ ДК: разделы, меню, журнал
@cmd(["+дк"], "дк")
async def c_dk_sec_on(msg, args, rank):
    sec = _sec_name(args)
    if not sec:
        return await reply(msg, "Пример: <code>+дк наказания</code>. Разделы: " + ", ".join(SECTIONS))
    await q("DELETE FROM im_dk WHERE chat_id=%s AND code=ANY(%s)", (msg.chat.id, SECTIONS[sec]))
    await logact(msg.chat.id, msg.from_user.id, "dk", None, f"+раздел {sec}")
    await reply(msg, f"Раздел «{esc(sec)}» включён (ранги по умолчанию).")


@cmd(["-дк"], "дк")
async def c_dk_sec_off(msg, args, rank):
    sec = _sec_name(args)
    if not sec:
        return await reply(msg, "Пример: <code>-дк браки</code>. Разделы: " + ", ".join(SECTIONS))
    for code in SECTIONS[sec]:
        if code in _PROTECT:
            continue
        await q("INSERT INTO im_dk(chat_id,code,rank) VALUES(%s,%s,6) ON CONFLICT(chat_id,code) DO UPDATE SET rank=6", (msg.chat.id, code))
    await logact(msg.chat.id, msg.from_user.id, "dk", None, f"-раздел {sec}")
    await reply(msg, f"Раздел «{esc(sec)}» выключен.")


@cmd(["+дк в лс", "-дк в лс"], "дк")
async def c_dk_dm(msg, args, rank):
    await reply(msg, "Команды в личке бота не поддерживаются: «Надзор» работает только в чатах.")


@cmd(["+команды"], "команды")
async def c_cmds_on(msg, args, rank):
    await set_cfg(msg.chat.id, nocmd=None)
    await reply(msg, "Обработка команд в чате включена.")


@cmd(["-команды"], "команды")
async def c_cmds_off(msg, args, rank):
    await set_cfg(msg.chat.id, nocmd=True)
    await reply(msg, "Команды в чате отключены. Вернуть: <code>+команды</code>")


@cmd(["лог дк"], "логдк")
async def c_dk_log(msg, args, rank):
    uid, name, rest = await resolve_target(msg, args)
    if uid:
        rows = await q("SELECT by_id,details,ts FROM im_log WHERE chat_id=%s AND action='dk' AND by_id=%s ORDER BY id DESC LIMIT 15", (msg.chat.id, uid), many=True)
    elif rest:
        rows = await q("SELECT by_id,details,ts FROM im_log WHERE chat_id=%s AND action='dk' AND details ILIKE %s ORDER BY id DESC LIMIT 15",
                       (msg.chat.id, f"%{rest[0]}%"), many=True)
    else:
        rows = await q("SELECT by_id,details,ts FROM im_log WHERE chat_id=%s AND action='dk' ORDER BY id DESC LIMIT 15", (msg.chat.id,), many=True)
    if not rows:
        return await reply(msg, "Изменений ДК не найдено.")
    out = ["<b>Лог ДК</b>"]
    for by, d, ts in rows:
        out.append(f"{ts.astimezone(_MSK):%d.%m %H:%M} {esc(await member_name(msg.chat.id, by))}: {esc(d or '')}")
    await reply(msg, "\n".join(out))


def _dk_menu_kb(rows_state):
    names = list(SECTIONS)
    kb, line = [], []
    for i, n in enumerate(names):
        line.append(InlineKeyboardButton(text=("✅ " if rows_state.get(n, True) else "⛔ ") + n.capitalize(), callback_data=f"imx:dk:{i}"))
        if len(line) == 2:
            kb.append(line)
            line = []
    if line:
        kb.append(line)
    return InlineKeyboardMarkup(inline_keyboard=kb)


async def _dk_state(cid):
    ov = dict(await q("SELECT code,rank FROM im_dk WHERE chat_id=%s", (cid,), many=True) or [])
    st = {}
    for n, codes in SECTIONS.items():
        cs = [c for c in codes if c not in _PROTECT]
        st[n] = not (cs and all(ov.get(c, m.DEFAULT_DK.get(c, 4)) >= 6 for c in cs))
    return st


@cmd(["мдк"], "дк")
async def c_mdk(msg, args, rank):
    await reply(msg, "<b>Меню доступа команд</b>\nНажмите раздел, чтобы включить/выключить его целиком.\nТонкая настройка: <code>!дк бан 4</code>",
                reply_markup=_dk_menu_kb(await _dk_state(msg.chat.id)))


@m.router.callback_query(F.data.startswith("imx:dk:"))
async def cb_mdk(cb: CallbackQuery):
    cid = cb.message.chat.id
    if not await m.allowed(cid, cb.from_user.id, "дк"):
        return await cb.answer("Недостаточно прав.", show_alert=True)
    idx = int(cb.data.split(":")[2])
    names = list(SECTIONS)
    if idx >= len(names):
        return await cb.answer()
    n = names[idx]
    st = await _dk_state(cid)
    if st.get(n, True):
        for c in SECTIONS[n]:
            if c not in _PROTECT:
                await q("INSERT INTO im_dk(chat_id,code,rank) VALUES(%s,%s,6) ON CONFLICT(chat_id,code) DO UPDATE SET rank=6", (cid, c))
    else:
        await q("DELETE FROM im_dk WHERE chat_id=%s AND code=ANY(%s)", (cid, SECTIONS[n]))
    await logact(cid, cb.from_user.id, "dk", None, f"меню: {n}")
    try:
        await cb.message.edit_reply_markup(reply_markup=_dk_menu_kb(await _dk_state(cid)))
    except Exception:
        pass
    await cb.answer("Готово")


# ================================================================ настройка чата
@cmd(["+чат ссылка"], "чатссылка")
async def c_link_on(msg, args, rank):
    await set_cfg(msg.chat.id, chatlink=True)
    await reply(msg, "Команда <code>чат ссылка</code> теперь доступна всем.")


@cmd(["-чат ссылка"], "чатссылка")
async def c_link_off(msg, args, rank):
    await set_cfg(msg.chat.id, chatlink=None)
    await reply(msg, "Выдача ссылки на чат отключена.")


@cmd(["чат ссылка"], "кто_я")
async def c_link_show(msg, args, rank):
    cfg = await get_cfg(msg.chat.id)
    if not cfg.get("chatlink") and rank < 1:
        return
    try:
        ch = await m._bot.get_chat(msg.chat.id)
        link = f"https://t.me/{ch.username}" if ch.username else (await m._bot.create_chat_invite_link(msg.chat.id, name="Надзор")).invite_link
    except Exception as e:
        return await reply(msg, m.human_error(e))
    await reply(msg, f"🔗 {link}")


@cmd(["!сброс ссылок"], "чатссылка")
async def c_links_reset(msg, args, rank):
    try:
        link = await m._bot.export_chat_invite_link(msg.chat.id)
    except Exception as e:
        return await reply(msg, m.human_error(e))
    try:
        await m._bot.send_message(msg.from_user.id, f"Новая основная ссылка: {link}")
        tail = "Новая ссылка отправлена вам в личные сообщения."
    except Exception:
        tail = "Новую ссылку можно получить командой «чат ссылка» (бот не может написать вам первым — запустите его в личке)."
    await reply(msg, "♻️ Прежняя основная ссылка отозвана. " + tail)


_TG_RIGHTS = dict(can_manage_chat=True, can_delete_messages=True, can_restrict_members=True, can_pin_messages=True,
                  can_invite_users=True, can_manage_topics=True)


@cmd(["+тг админ"], "тгадмин")
async def c_tgadmin_on(msg, args, rank):
    uid, name, _ = await resolve_target(msg, args)
    if not uid:
        return await reply(msg, "Пример: <code>+тг админ @ник</code>")
    try:
        await m._bot.promote_chat_member(msg.chat.id, uid, **_TG_RIGHTS)
    except Exception as e:
        return await reply(msg, m.human_error(e))
    await reply(msg, f"🛡 {mention(uid, name)} назначен администратором Telegram (удаление, блокировка, закреп, приглашения).")


@cmd(["-тг админ"], "тгадмин")
async def c_tgadmin_off(msg, args, rank):
    uid, name, _ = await resolve_target(msg, args)
    if not uid:
        return await reply(msg, "Пример: <code>-тг админ @ник</code>")
    try:
        await m._bot.promote_chat_member(msg.chat.id, uid, **{k: False for k in _TG_RIGHTS})
    except Exception as e:
        return await reply(msg, m.human_error(e))
    await reply(msg, f"{mention(uid, name)} больше не администратор Telegram.")


@cmd(["тг админ", "тг админы"], "тгадмин")
async def c_tgadmin_list(msg, args, rank):
    try:
        admins = await m._bot.get_chat_administrators(msg.chat.id)
    except Exception as e:
        return await reply(msg, m.human_error(e))
    out = [f"<b>Администраторы Telegram</b> ({len(admins)}/50)"]
    for a in admins:
        if a.user.is_bot:
            continue
        out.append(f"{'👑' if a.status == 'creator' else '🛡'} {mention(a.user.id, a.user.full_name)}")
    await reply(msg, "\n".join(out))


@cmd(["обновить чат"], "обновитьчат")
async def c_refresh(msg, args, rank):
    try:
        admins = await m._bot.get_chat_administrators(msg.chat.id)
        me = next((a for a in admins if a.user.id == m._bot.id), None)
        cr = next((a for a in admins if a.status == "creator"), None)
    except Exception as e:
        return await reply(msg, m.human_error(e))
    if cr:
        await q("""INSERT INTO im_ranks(chat_id,user_id,rank) VALUES(%s,%s,5) ON CONFLICT(chat_id,user_id) DO UPDATE SET rank=5""", (msg.chat.id, cr.user.id))
    for k in [k for k in m._not_creator if k[0] == msg.chat.id]:
        m._not_creator.pop(k, None)
    m._enabled_cache.pop(msg.chat.id, None)
    miss = []
    if me:
        for label, attr in (("блокировка", "can_restrict_members"), ("удаление сообщений", "can_delete_messages"),
                            ("закрепление", "can_pin_messages"), ("приглашение", "can_invite_users"), ("назначение админов", "can_promote_members")):
            if not getattr(me, attr, False):
                miss.append(label)
    else:
        miss = ["бот не администратор"]
    await reply(msg, "🔄 Чат обновлён: кэши сброшены" + (", создатель найден" if cr else "") + f", админов: {len(admins)}.\n"
                + ("⚠️ У бота не хватает прав: " + ", ".join(miss) if miss else "✅ Права бота в порядке."))


# ================================================================ журнал входов/выходов
async def _jl_join(cid, u, cfg, msg):
    try:
        inv = msg.from_user.id if msg and msg.from_user and msg.from_user.id != u.id else None
        await q("INSERT INTO im_jl(chat_id,user_id,kind,inviter) VALUES(%s,%s,'join',%s)", (cid, u.id, inv))
    except Exception:
        log.debug("jl join", exc_info=True)
    return False


async def _jl_leave(msg, cfg):
    try:
        if msg.left_chat_member and not msg.left_chat_member.is_bot:
            await q("INSERT INTO im_jl(chat_id,user_id,kind) VALUES(%s,%s,'leave')", (msg.chat.id, msg.left_chat_member.id))
    except Exception:
        log.debug("jl leave", exc_info=True)


m.JOIN_HOOKS.insert(0, _jl_join)
m.SERVICE_HOOKS.append(_jl_leave)


async def _jl_list(msg, kinds, title):
    cid = msg.chat.id
    rows = await q("SELECT user_id,kind,ts FROM im_jl WHERE chat_id=%s AND kind=ANY(%s) AND ts>now()-interval '1 day' ORDER BY ts DESC LIMIT 40",
                   (cid, kinds), many=True)
    if not rows:
        return await reply(msg, f"За сутки {title} нет (журнал ведётся с момента обновления бота).")
    out = [f"<b>{title.capitalize()} за сутки</b>"]
    for u, k, ts in rows:
        out.append(f"{'➕' if k == 'join' else '➖'} {esc(await member_name(cid, u))} — {ts.astimezone(_MSK):%H:%M}")
    await reply(msg, "\n".join(out))


@cmd(["входы"], "журналвходов")
async def c_joins(msg, args, rank):
    await _jl_list(msg, ["join"], "входов")


@cmd(["выходы"], "журналвходов")
async def c_leaves(msg, args, rank):
    await _jl_list(msg, ["leave"], "выходов")


@cmd(["входы-выходы", "входы выходы", "+входы-выходы"], "журналвходов")
async def c_jl_all(msg, args, rank):
    await _jl_list(msg, ["join", "leave"], "входов и выходов")


@cmd(["кто добавил"], "мойстат")
async def c_who_added(msg, args, rank):
    uid, name, _ = await resolve_target(msg, args)
    if not uid:
        uid, name = msg.from_user.id, msg.from_user.full_name
    r = await q("SELECT inviter,ts FROM im_jl WHERE chat_id=%s AND user_id=%s AND kind='join' ORDER BY id DESC LIMIT 1", (msg.chat.id, uid), one=True)
    if not r:
        return await reply(msg, f"Про вход {mention(uid, name)} у меня нет данных.")
    if not r[0]:
        return await reply(msg, f"{mention(uid, name)} вошёл(а) сам(а) ({r[1].astimezone(_MSK):%d.%m.%Y}).")
    await reply(msg, f"{mention(uid, name)} добавил(а) {mention(r[0], await member_name(msg.chat.id, r[0]))} ({r[1].astimezone(_MSK):%d.%m.%Y}).")


# ================================================================ фильтры сообщений
_URLRE = re.compile(r"(?:https?://|www\.)[^\s]+|\b[\w-]+\.(?:com|ru|org|net|io|me|su|xyz|info|dev|app|gg|tv|co|рф)\b[^\s]*", re.I)
_TME = re.compile(r"(?:t|telegram)\.me/(\+?[\w-]+)", re.I)


def _link_items(msg):
    """[(категория, текст)] — чаты / сайты / теги / боты."""
    txt = msg.text or msg.caption or ""
    items = []
    seen = set()
    urls = [u for u in _URLRE.findall(txt)]
    for e in list(msg.entities or []) + list(msg.caption_entities or []):
        if e.type == "text_link" and e.url:
            urls.append(e.url)
        elif e.type == "mention":
            tag = txt[e.offset:e.offset + e.length]
            items.append(("боты" if tag.lower().endswith("bot") else "теги", tag))
    for u in urls:
        if u in seen:
            continue
        seen.add(u)
        mt = _TME.search(u)
        if mt:
            name = mt.group(1)
            items.append(("боты" if name.lower().endswith("bot") else "чаты", u))
        else:
            items.append(("сайты", u))
    return items


async def _filters_hook(msg, cfg, rank):
    cid, uid = msg.chat.id, msg.from_user.id
    # --- фильтры типов сообщений
    nof = set(cfg.get("nof") or [])
    if nof:
        ev = set(m.detect(msg))
        if nof & ev:
            try:
                await msg.delete()
            except Exception:
                pass
            return True
    # --- фильтры ссылок
    lf = set(cfg.get("lf") or [])
    if lf and (msg.text or msg.caption):
        bad = [(c, t) for c, t in _link_items(msg) if c in lf]
        allow = [a.lower() for a in (cfg.get("link_allow") or [])]
        bad = [(c, t) for c, t in bad if not any(a in t.lower() for a in allow)]
        if bad:
            exempt = False
            days, need_rep = int(cfg.get("link_days", 0) or 0), int(cfg.get("link_rep", 0) or 0)
            if days:
                r = await q("SELECT 1 FROM im_members WHERE chat_id=%s AND user_id=%s AND first_seen<now()-make_interval(days=>%s)", (cid, uid, days), one=True)
                exempt = exempt or bool(r)
            if need_rep:
                r = await q("SELECT rep FROM im_rep WHERE chat_id=%s AND user_id=%s", (cid, uid), one=True)
                exempt = exempt or bool(r and r[0] >= need_rep)
            if not exempt:
                try:
                    await msg.delete()
                except Exception:
                    pass
                if await m.run_triggers(cid, "links", uid, None):
                    pass
                return True
    return False


m.AUTOMOD.append(_filters_hook)


async def _toggle_lf(msg, cat, on):
    cfg = await get_cfg(msg.chat.id)
    cur = set(cfg.get("lf") or [])
    (cur.discard if on else cur.add)(cat)       # «+чаты» — разрешить (убрать из запретов), «-чаты» — запретить
    await set_cfg(msg.chat.id, lf=sorted(cur) or None)
    await reply(msg, f"Ссылки/упоминания «{cat}»: {'разрешены' if on else 'запрещены (удаляются у участников ранга 0)'}.")


for _cat in ("чаты", "сайты", "теги", "боты"):
    def _mk(cat):
        @cmd([f"+{cat}"], "фильтры")
        async def _on(msg, args, rank):
            await _toggle_lf(msg, cat, True)

        @cmd([f"-{cat}"], "фильтры")
        async def _off(msg, args, rank):
            await _toggle_lf(msg, cat, False)
    _mk(_cat)


@cmd(["ссылки срок"], "фильтры")
async def c_link_days(msg, args, rank):
    if not args or not args[0].isdigit():
        return await reply(msg, "Пример: <code>ссылки срок 3</code> — ссылки разрешены тем, кто в чате ≥3 дней (0 — выкл).")
    await set_cfg(msg.chat.id, link_days=int(args[0]) or None)
    await reply(msg, f"Срок в чате для ссылок: {args[0]} дн." if int(args[0]) else "Исключение по сроку выключено.")


@cmd(["ссылки рейтинг"], "фильтры")
async def c_link_rep(msg, args, rank):
    if not args or not args[0].lstrip("-").isdigit():
        return await reply(msg, "Пример: <code>ссылки рейтинг 10</code> — ссылки разрешены при репутации ≥10 (0 — выкл).")
    await set_cfg(msg.chat.id, link_rep=int(args[0]) or None)
    await reply(msg, f"Рейтинг для ссылок: {args[0]}" if int(args[0]) else "Исключение по рейтингу выключено.")


@cmd(["!разрешить ссылку"], "фильтры")
async def c_link_allow(msg, args, rank):
    if not args:
        cfg = await get_cfg(msg.chat.id)
        return await reply(msg, "Разрешённые: " + (", ".join(esc(x) for x in (cfg.get("link_allow") or [])) or "нет") + "\nПример: <code>!разрешить ссылку t.me/mychat</code>")
    cfg = await get_cfg(msg.chat.id)
    lst = list(cfg.get("link_allow") or [])
    if args[0].lower() not in lst:
        lst.append(args[0].lower()[:100])
    await set_cfg(msg.chat.id, link_allow=lst[-100:])
    await reply(msg, f"Ссылка «{esc(args[0])}» разрешена.")


@cmd(["!удалить ссылку"], "фильтры")
async def c_link_del(msg, args, rank):
    cfg = await get_cfg(msg.chat.id)
    lst = list(cfg.get("link_allow") or [])
    if args and args[0].lower() in lst:
        lst.remove(args[0].lower())
        await set_cfg(msg.chat.id, link_allow=lst or None)
        return await reply(msg, "Убрана из разрешённых.")
    if msg.reply_to_message:
        try:
            await msg.reply_to_message.delete()
            await msg.delete()
        except Exception as e:
            await reply(msg, m.human_error(e))


def _flt(name, flag, on):
    @cmd([name], "фильтры")
    async def _h(msg, args, rank):
        cfg = await get_cfg(msg.chat.id)
        cur = set(cfg.get("nof") or [])
        (cur.discard if on else cur.add)(flag)
        await set_cfg(msg.chat.id, nof=sorted(cur) or None)
        lbl = {"caps": "капс", "stickers": "стикеры", "gifs": "гифки"}[flag]
        await reply(msg, f"{lbl.capitalize()}: {'разрешены' if on else 'запрещены (удаляются у участников ранга 0)'}.")


for _n, _f in (("капс", "caps"), ("стикеры", "stickers"), ("гифки", "gifs")):
    _flt("+" + _n, _f, True)
    _flt("-" + _n, _f, False)

# ---- антиспам (флуд)
_flood = defaultdict(lambda: deque(maxlen=12))
_same = {}


async def _antispam_hook(msg, cfg, rank):
    if not cfg.get("antispam"):
        return False
    key = (msg.chat.id, msg.from_user.id)
    now = time.time()
    dq = _flood[key]
    dq.append(now)
    txt = (msg.text or msg.caption or "").strip().lower()
    hit = None
    if len(dq) >= 6 and now - dq[-6] < 8:
        hit = "флуд"
    elif txt:
        lst = [t for t in _same.get(key, []) if now - t[0] < 60 and t[1] == txt] + [(now, txt)]
        _same[key] = lst
        if len(lst) >= 4:
            hit = "повтор сообщений"
    if len(_flood) > 20000:
        _flood.clear()
        _same.clear()
    if not hit:
        return False
    try:
        await msg.delete()
    except Exception:
        pass
    if hit == "флуд":
        dq.clear()
        try:
            await m.do_mute(msg.chat.id, msg.from_user.id, 600, "антиспам: " + hit, m._bot.id)
            await m._bot.send_message(msg.chat.id, f"🔇 {mention(msg.from_user.id, msg.from_user.full_name)} — мут 10 мин (антиспам: {hit}).", parse_mode="HTML")
        except Exception:
            pass
    return True


m.AUTOMOD.append(_antispam_hook)


@cmd(["+ирис антиспам", "+антиспам"], "антиспам")
async def c_as_on(msg, args, rank):
    await set_cfg(msg.chat.id, antispam=True)
    await reply(msg, "🛡 Антиспам включён: флуд (6 сообщений за 8 с → мут 10 мин) и повторы (4 одинаковых за минуту → удаление).")


@cmd(["-ирис антиспам", "-антиспам"], "антиспам")
async def c_as_off(msg, args, rank):
    await set_cfg(msg.chat.id, antispam=None)
    await reply(msg, "Антиспам выключен.")


# ================================================================ чистка
async def _pick(cid, sql, args_):
    return await q(sql, args_, many=True) or []


async def _kick_many(msg, rows, reason):
    n = 0
    for (u,) in rows[:300]:
        if await get_rank(msg.chat.id, u) > 0:
            continue
        try:
            await m.do_kick(msg.chat.id, u, msg.from_user.id, reason, quiet=True)
            n += 1
            await asyncio.sleep(0.05)
        except Exception:
            pass
    await reply(msg, f"Исключено: <b>{n}</b> ({esc(reason)}).")


def _arg_sec(args, default, unit=60):
    if args:
        t = args[0]
        d = int(t) * unit if t.isdigit() else parse_dur(t)
        if d:
            return d
    return default


@cmd(["кик актив"], "чистка")
async def c_kick_active(msg, args, rank):
    sec = _arg_sec(args, 600)
    rows = await _pick(msg.chat.id, "SELECT user_id FROM im_members WHERE chat_id=%s AND last_seen>now()-make_interval(secs=>%s)", (msg.chat.id, sec))
    await _kick_many(msg, rows, f"активные за {fmt_dur(sec)}")


@cmd(["кик новичков"], "чистка")
async def c_kick_new(msg, args, rank):
    sec = _arg_sec(args, 86400, 3600)
    rows = await _pick(msg.chat.id, "SELECT user_id FROM im_members WHERE chat_id=%s AND first_seen>now()-make_interval(secs=>%s)", (msg.chat.id, sec))
    await _kick_many(msg, rows, f"новички за {fmt_dur(sec)}")


@cmd(["кик по смс"], "чистка")
async def c_kick_sms(msg, args, rank):
    n = int(args[0]) if args and args[0].isdigit() else 3
    rows = await _pick(msg.chat.id, "SELECT user_id FROM im_members WHERE chat_id=%s AND msgs<=%s AND first_seen<now()-interval '1 day'", (msg.chat.id, n))
    await _kick_many(msg, rows, f"≤{n} сообщений")


@cmd(["кик удалённых", "кик удаленных", "кик удалённые"], "чистка")
async def c_kick_deleted(msg, args, rank):
    rows = await q("SELECT user_id FROM im_members WHERE chat_id=%s ORDER BY last_seen LIMIT 600", (msg.chat.id,), many=True) or []
    dead = []
    for (u,) in rows:
        try:
            cm = await m._bot.get_chat_member(msg.chat.id, u)
            if cm.status in ("member", "restricted") and (cm.user.first_name or "") in ("", "Deleted Account"):
                dead.append((u,))
        except Exception:
            pass
        await asyncio.sleep(0.04)
    await _kick_many(msg, dead, "удалённые аккаунты")


async def _list_clean(msg, sql, args_, title):
    rows = await q(sql, args_, many=True)
    if not rows:
        return await reply(msg, f"{title}: никого.")
    out = [f"<b>{title}</b> ({len(rows)})"] + [esc(n) for n, in rows[:40]]
    if len(rows) > 40:
        out.append(f"… и ещё {len(rows) - 40}")
    await reply(msg, "\n".join(out))


@cmd(["список неактив"], "списокчистки")
async def c_list_inactive(msg, args, rank):
    sec = _arg_sec(args, 30 * 86400, 86400)
    sec = max(sec, 86400)
    await _list_clean(msg, "SELECT name FROM im_members WHERE chat_id=%s AND last_seen<now()-make_interval(secs=>%s) ORDER BY last_seen LIMIT 500",
                      (msg.chat.id, sec), f"Неактивны более {fmt_dur(sec)}")


@cmd(["список молчунов"], "списокчистки")
async def c_list_silent(msg, args, rank):
    await _list_clean(msg, "SELECT name FROM im_members WHERE chat_id=%s AND msgs<=1 ORDER BY first_seen LIMIT 500", (msg.chat.id,), "Молчуны (≤1 сообщения)")


@cmd(["список по смс"], "списокчистки")
async def c_list_sms(msg, args, rank):
    n = int(args[0]) if args and args[0].isdigit() else 3
    await _list_clean(msg, "SELECT name FROM im_members WHERE chat_id=%s AND msgs<=%s ORDER BY msgs, first_seen LIMIT 500", (msg.chat.id, n), f"≤{n} сообщений")


# ================================================================ анкета и личная статистика
@cmd(["+анкета"], "анкета")
async def c_prof_open(msg, args, rank):
    await q("UPDATE im_members SET profile=coalesce(profile,'{}'::jsonb)-'hidden' WHERE chat_id=%s AND user_id=%s", (msg.chat.id, msg.from_user.id))
    await reply(msg, "Ваша анкета открыта для всех.")


@cmd(["-анкета"], "анкета")
async def c_prof_hide(msg, args, rank):
    await q("UPDATE im_members SET profile=coalesce(profile,'{}'::jsonb)||'{\"hidden\":\"1\"}'::jsonb WHERE chat_id=%s AND user_id=%s", (msg.chat.id, msg.from_user.id))
    await reply(msg, "Ваша анкета скрыта: «кто я» на вас покажет только вам и модераторам.")


@cmd(["мои баны"], "мойстат")
async def c_my_bans(msg, args, rank):
    uid, name = msg.from_user.id, msg.from_user.full_name
    rows = await q("SELECT kind,reason,ts,until,active FROM im_punish WHERE chat_id=%s AND user_id=%s ORDER BY ts DESC LIMIT 10", (msg.chat.id, uid), many=True)
    if not rows:
        return await reply(msg, f"У {mention(uid, name)} наказаний нет.")
    lab = {"ban": "бан", "mute": "мут", "warn": "варн"}
    out = [f"<b>Наказания {mention(uid, name)}</b>"]
    for k, r, ts, un, act in rows:
        out.append(f"{ts.astimezone(_MSK):%d.%m.%Y} — {lab.get(k, k)}{' (действует)' if act and (un is None or un > datetime.now(timezone.utc)) else ''}: {esc(r or '—')}")
    await reply(msg, "\n".join(out))


@cmd(["мой спам"], "мойстат")
async def c_my_spam(msg, args, rank):
    cid, uid = msg.chat.id, msg.from_user.id
    sc = await q("SELECT reason FROM im_scam WHERE chat_id=%s AND user_id=%s", (cid, uid), one=True)
    w = len(await m.active_warns(cid, uid))
    mu = (await q("SELECT count(*) FROM im_punish WHERE chat_id=%s AND user_id=%s AND kind='mute' AND ts>now()-interval '30 days'", (cid, uid), one=True))[0]
    await reply(msg, f"<b>Мой спам-статус</b>\nСКАМ-список: {'да — ' + esc(sc[0] or '') if sc else 'нет'}\nАктивных варнов: {w}\nМутов за 30 дней: {mu}")


@cmd(["мой актив"], "мойстат")
async def c_my_active(msg, args, rank):
    uid, name, _ = await resolve_target(msg, args)
    if not uid:
        uid, name = msg.from_user.id, msg.from_user.full_name
    cid = msg.chat.id
    r = await q("""SELECT coalesce(sum(msgs) FILTER (WHERE day=CURRENT_DATE),0), coalesce(sum(msgs) FILTER (WHERE day>CURRENT_DATE-7),0),
                          coalesce(sum(msgs) FILTER (WHERE day>CURRENT_DATE-30),0), coalesce(sum(msgs),0)
                   FROM im_daily WHERE chat_id=%s AND user_id=%s""", (cid, uid), one=True)
    await reply(msg, f"<b>Актив {mention(uid, name)}</b>\nСегодня: {r[0]}\nНеделя: {r[1]}\nМесяц: {r[2]}\nВсего: {r[3]}")


@cmd(["!население", "население"], "население")
async def c_population(msg, args, rank):
    cid = msg.chat.id
    r = await q("""SELECT count(*), count(*) FILTER (WHERE last_seen>now()-interval '1 day'), count(*) FILTER (WHERE last_seen>now()-interval '7 days'),
                          count(*) FILTER (WHERE last_seen>now()-interval '30 days'), count(*) FILTER (WHERE first_seen>now()-interval '7 days'),
                          count(*) FILTER (WHERE msgs<=1)
                   FROM im_members WHERE chat_id=%s""", (cid,), one=True)
    lv = (await q("SELECT count(*) FROM im_jl WHERE chat_id=%s AND kind='leave' AND ts>now()-interval '30 days'", (cid,), one=True))[0]
    try:
        total = await m._bot.get_chat_member_count(cid)
    except Exception:
        total = "?"
    await reply(msg, f"<b>Население</b>\nВ чате (по Telegram): {total}\nВ базе бота: {r[0]}\nАктивны за сутки: {r[1]} · неделю: {r[2]} · месяц: {r[3]}\n"
                     f"Новых за неделю: {r[4]}\nМолчунов: {r[5]}\nВышло за 30 дней: {lv}")


@cmd(["!актив ириса", "актив надзора", "актив бота"], "актив_бота")
async def c_bot_active(msg, args, rank):
    r = await q("SELECT count(*) FROM im_chats WHERE enabled", one=True)
    mm = await q("SELECT count(*), coalesce(sum(msgs),0) FROM im_members", one=True)
    up = int(time.time() - _STARTED)
    await reply(msg, f"<b>Актив Надзора</b>\nРаботаю: {up // 86400} д {up // 3600 % 24} ч {up // 60 % 60} мин\nЧатов под надзором: {r[0]}\nУчастников в базе: {mm[0]}\nСообщений учтено: {mm[1]}")


# ================================================================ РП: уровни, экспорт, глобальные МРП
@cmd(["+рп 0", "+рп 13", "+рп 18"], "настройка")
async def c_rp_level(msg, args, rank):
    lvl = (msg.text or "").split()[-1].strip()
    lvl = next((x for x in ("18", "13", "0") if lvl.endswith(x)), "13")
    await set_cfg(msg.chat.id, rp_off=None, rp_level=int(lvl))
    await reply(msg, f"РП-команды включены. Уровень: <b>{lvl}+</b>" if lvl != "0" else "РП-команды включены для всех (уровень 0).")


@cmd(["мрп экспорт"], "мрп")
async def c_mrp_export(msg, args, rank):
    mrp = (await get_cfg(msg.chat.id)).get("mrp") or {}
    if not mrp:
        return await reply(msg, "Своих РП нет.")
    await reply(msg, "<b>Экспорт МРП</b> (скопируйте в другой чат):\n<code>" + "\n".join(f"+мрп {esc(k)} {esc(v)}" for k, v in sorted(mrp.items())) + "</code>")


@cmd(["+гмрп"], "гмрп")
async def c_gmrp_add(msg, args, rank):
    if len(args) < 2:
        return await reply(msg, "Пример: <code>+гмрп хлопнуть хлопнул(а)</code>")
    await q("INSERT INTO im_gmrp(cmd,verb) VALUES(%s,%s) ON CONFLICT(cmd) DO UPDATE SET verb=EXCLUDED.verb", (args[0].lower(), " ".join(args[1:])[:60]))
    await reply(msg, f"Глобальный РП «{esc(args[0])}» добавлен.")


@cmd(["-гмрп"], "гмрп")
async def c_gmrp_del(msg, args, rank):
    if args:
        await q("DELETE FROM im_gmrp WHERE cmd=%s", (args[0].lower(),))
    await reply(msg, "Удалено.")


@cmd(["!гмрп", "гмрп"], "рп")
async def c_gmrp_list(msg, args, rank):
    rows = await q("SELECT cmd FROM im_gmrp ORDER BY cmd", many=True)
    await reply(msg, "<b>Глобальные РП</b>\n" + (", ".join(esc(c) for c, in rows) if rows else "нет"))


async def _gmrp_dynamic(msg, first, rank):
    word = first.split(None, 1)[0].lower()
    r = await q("SELECT verb FROM im_gmrp WHERE cmd=%s", (word,), one=True)
    if not r or (await get_cfg(msg.chat.id)).get("rp_off"):
        return False
    uid, name, _ = await resolve_target(msg, first.split()[1:])
    if not uid:
        return False
    await reply(msg, f"{mention(msg.from_user.id, msg.from_user.full_name)} {esc(r[0])} {mention(uid, name)}")
    return True


m.DYNAMIC.append(_gmrp_dynamic)


# ================================================================ развлечения: кинг, пиу
@cmd(["кинг"], "кинг")
async def c_king(msg, args, rank):
    cid = msg.chat.id
    cfg = await get_cfg(cid)
    today = time.strftime("%Y-%m-%d", time.gmtime(time.time() + 10800))
    k = cfg.get("king") or {}
    if k.get("d") != today:
        rows = await q("SELECT user_id FROM im_members WHERE chat_id=%s AND last_seen>now()-interval '7 days' ORDER BY random() LIMIT 1", (cid,), one=True)
        if not rows:
            return await reply(msg, "Некого короновать — в чате пока тихо.")
        k = {"d": today, "u": rows[0]}
        await set_cfg(cid, king=k)
    await reply(msg, f"👑 Кинг дня — {mention(k['u'], await member_name(cid, k['u']))}!")


@cmd(["пиу"], "кинг")
async def c_piu(msg, args, rank):
    uid, name, _ = await resolve_target(msg, args)
    me = mention(msg.from_user.id, msg.from_user.full_name)
    if not uid:
        return await reply(msg, f"{me}: пиу-пиу! 🔫 (выстрел в воздух)")
    res = random.choice(["попал(а) прямо в цель 🎯", "промахнулся(ась) 💨", "задел(а) плечо 🩹", "выстрелил(а) конфетти 🎉"])
    await reply(msg, f"{me} стреляет в {mention(uid, name)}: {res}")


# ================================================================ чеки (монеты)
@cmd(["чек", "!чек"], "чек")
async def c_cheque_make(msg, args, rank):
    import iris_eco
    cid, uid = msg.chat.id, msg.from_user.id
    n = next((int(t) for t in args if t.isdigit() and len(t) < 12), 0)
    if n <= 0:
        return await reply(msg, "Пример: <code>чек 500</code> — создать чек на 500 монет. Именной: <code>чек 500 @ник</code>. Активировать: <code>обналичить КОД</code>")
    target, tname, _ = await resolve_target(msg, [t for t in args if not t.isdigit() or len(t) >= 6])
    if not await iris_eco.spend(cid, uid, n):
        return await reply(msg, "Недостаточно монет.")
    code = secrets.token_hex(4).upper()
    await q("INSERT INTO im_cheque(code,chat_id,owner,amount,for_user) VALUES(%s,%s,%s,%s,%s)", (code, cid, uid, n, target))
    tail = f" для {mention(target, tname)}" if target else ""
    await reply(msg, f"🧾 Чек на <b>{n}</b> 🪙{tail}\nКод: <code>{code}</code>\nАктивировать: <code>обналичить {code}</code>")


@cmd(["обналичить", "!обналичить", "активировать чек"], "чек")
async def c_cheque_use(msg, args, rank):
    import iris_eco
    if not args:
        return await reply(msg, "Пример: <code>обналичить КОД</code>")
    code = args[0].upper()
    r = await q("UPDATE im_cheque SET used_by=%s WHERE code=%s AND chat_id=%s AND used_by IS NULL AND (for_user IS NULL OR for_user=%s) RETURNING amount",
                (msg.from_user.id, code, msg.chat.id, msg.from_user.id), one=True)
    if not r:
        return await reply(msg, "Чек не найден, уже использован или выписан на другого.")
    await iris_eco.add(msg.chat.id, msg.from_user.id, int(r[0]))
    await reply(msg, f"✅ Чек активирован: +{r[0]} 🪙")


@cmd(["чеки", "мои чеки"], "чек")
async def c_cheques(msg, args, rank):
    rows = await q("SELECT code,amount,for_user FROM im_cheque WHERE chat_id=%s AND owner=%s AND used_by IS NULL ORDER BY ts DESC LIMIT 15", (msg.chat.id, msg.from_user.id), many=True)
    if not rows:
        return await reply(msg, "Неиспользованных чеков нет.")
    await reply(msg, "<b>Мои чеки</b>\n" + "\n".join(f"<code>{c}</code> — {a} 🪙" + (" (именной)" if f else "") for c, a, f in rows))
