import asyncio
import logging
import os
import html
import re
import psycopg2
from psycopg2.extras import RealDictCursor
from aiohttp import web

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton, ForceReply
)

# ----------------------------------------------------------------------
# НАСТРОЙКИ
# ----------------------------------------------------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_CHAT_ID = -1003945292994  # ID группы поддержки

if not BOT_TOKEN:
    raise ValueError("ОШИБКА: Токен бота не найден! Укажите BOT_TOKEN в Environment Variables на Render.")

if not DATABASE_URL:
    raise ValueError("ОШИБКА: Строка подключения к базе данных не найдена! Укажите DATABASE_URL в Environment Variables.")

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)

def get_user_mention(user):
    safe_name = html.escape(user.full_name)
    return f'<a href="tg://user?id={user.id}">{safe_name}</a>'

# ----------------------------------------------------------------------
# БАЗА ДАННЫХ (Supabase / PostgreSQL)
# ----------------------------------------------------------------------
def get_db_connection():
    return psycopg2.connect(DATABASE_URL, sslmode="require")

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY
        );
        CREATE TABLE IF NOT EXISTS tickets (
            ticket_id SERIAL PRIMARY KEY,
            user_id BIGINT,
            admin_id BIGINT DEFAULT NULL,
            status TEXT DEFAULT 'pending'
        );
        CREATE TABLE IF NOT EXISTS message_map (
            group_message_id BIGINT PRIMARY KEY,
            user_id BIGINT,
            ticket_id INTEGER
        );
        CREATE TABLE IF NOT EXISTS banned_users (
            user_id BIGINT PRIMARY KEY,
            reason TEXT
        );
        CREATE TABLE IF NOT EXISTS pending_rejections (
            prompt_message_id BIGINT PRIMARY KEY,
            ticket_id INTEGER
        );
    """)
    conn.commit()
    cursor.close()
    conn.close()

init_db()

def register_user(user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("INSERT INTO users (user_id) VALUES (%s) ON CONFLICT (user_id) DO NOTHING", (user_id,))
    conn.commit()
    cursor.close()
    conn.close()

def get_all_users():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM users")
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    return [row[0] for row in rows]

def create_ticket(user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("INSERT INTO tickets (user_id, status) VALUES (%s, 'pending') RETURNING ticket_id", (user_id,))
    ticket_id = cursor.fetchone()[0]
    conn.commit()
    cursor.close()
    conn.close()
    return ticket_id

def map_message(group_msg_id, user_id, ticket_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO message_map (group_message_id, user_id, ticket_id) VALUES (%s, %s, %s) "
        "ON CONFLICT (group_message_id) DO UPDATE SET user_id = EXCLUDED.user_id, ticket_id = EXCLUDED.ticket_id",
        (group_msg_id, user_id, ticket_id)
    )
    conn.commit()
    cursor.close()
    conn.close()

def get_user_by_group_msg(group_msg_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, ticket_id FROM message_map WHERE group_message_id = %s", (group_msg_id,))
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    return row

def get_ticket_info(ticket_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, admin_id, status FROM tickets WHERE ticket_id = %s", (ticket_id,))
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    return row

def get_active_ticket(user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT ticket_id, admin_id FROM tickets WHERE user_id = %s AND status = 'active' ORDER BY ticket_id DESC LIMIT 1", (user_id,))
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    return row

def activate_ticket(ticket_id, admin_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE tickets SET status = 'active', admin_id = %s WHERE ticket_id = %s", (admin_id, ticket_id))
    conn.commit()
    cursor.close()
    conn.close()

def close_ticket_db(ticket_id, status='closed'):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE tickets SET status = %s WHERE ticket_id = %s", (status, ticket_id))
    conn.commit()
    cursor.close()
    conn.close()

def is_banned(user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT reason FROM banned_users WHERE user_id = %s", (user_id,))
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    return row

def ban_user_db(user_id, reason):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO banned_users (user_id, reason) VALUES (%s, %s) "
        "ON CONFLICT (user_id) DO UPDATE SET reason = EXCLUDED.reason",
        (user_id, reason)
    )
    conn.commit()
    cursor.close()
    conn.close()

def unban_user_db(user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM banned_users WHERE user_id = %s", (user_id,))
    conn.commit()
    cursor.close()
    conn.close()

def add_pending_rejection(prompt_message_id, ticket_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO pending_rejections (prompt_message_id, ticket_id) VALUES (%s, %s) "
        "ON CONFLICT (prompt_message_id) DO UPDATE SET ticket_id = EXCLUDED.ticket_id",
        (prompt_message_id, ticket_id)
    )
    conn.commit()
    cursor.close()
    conn.close()

def get_pending_rejection(prompt_message_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT ticket_id FROM pending_rejections WHERE prompt_message_id = %s", (prompt_message_id,))
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    return row[0] if row else None

def delete_pending_rejection(prompt_message_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM pending_rejections WHERE prompt_message_id = %s", (prompt_message_id,))
    conn.commit()
    cursor.close()
    conn.close()

# ----------------------------------------------------------------------
# СОСТОЯНИЯ (FSM)
# ----------------------------------------------------------------------
class Form(StatesGroup):
    complaint_reason = State()
    complaint_nickname = State()
    complaint_photo = State()
    complaint_server = State()

    appeal_nickname = State()
    appeal_reason = State()

    question_text = State()

# ----------------------------------------------------------------------
# КЛАВИАТУРЫ
# ----------------------------------------------------------------------
BTN_COMPLAINT = "🚨 Жалоба на игрока"
BTN_APPEAL = "😡 Обжалование бана"
BTN_FRIENDS = "👯‍♀️ Добавление в друзья (VIP)"
BTN_QUESTION = "❓ Задать вопрос"

def main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_COMPLAINT), KeyboardButton(text=BTN_APPEAL)],
            [KeyboardButton(text=BTN_FRIENDS), KeyboardButton(text=BTN_QUESTION)]
        ],
        resize_keyboard=True,
        persistent=True
    )

def take_ticket_kb(ticket_id):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📥 Принять заявку", callback_data=f"take_{ticket_id}"),
        InlineKeyboardButton(text="❌ Отклонить заявку", callback_data=f"reject_{ticket_id}")
    ]])

def close_ticket_kb(ticket_id, admin_id):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔒 Завершить заявку", callback_data=f"close_{ticket_id}_{admin_id}")
    ]])

@router.message(CommandStart(), F.chat.type == "private")
async def start_cmd(message: Message, state: FSMContext):
    register_user(message.from_user.id)
    banned = is_banned(message.from_user.id)
    if banned:
        await message.answer(f"❌ Вы заблокированы в поддержке.\n<b>Причина:</b> {html.escape(banned[0])}", parse_mode="HTML")
        return

    await state.clear()
    welcome_text = (
        "🎪 <b>Добро пожаловать в поддержку Tower Of Hell Secrets (@ToHSecretss)!</b> 🎪\n\n"
        "🤖 Нажмите на нужную кнопку на клавиатуре ниже, чтобы отправить заявку или задать вопрос."
    )
    await message.answer(welcome_text, reply_markup=main_keyboard(), parse_mode="HTML")

# ----------------------------------------------------------------------
# ОБРАБОТКА НАЖАТИЙ НА КНОПКИ КЛАВИАТУРЫ
# ----------------------------------------------------------------------
@router.message(F.text == BTN_COMPLAINT, F.chat.type == "private")
async def start_complaint(message: Message, state: FSMContext):
    register_user(message.from_user.id)
    if is_banned(message.from_user.id): return
    await state.clear()
    await state.set_state(Form.complaint_reason)
    await message.answer("1️⃣ <b>Суть нарушения:</b>\nОпишите подробно, что именно сделал игрок.", parse_mode="HTML")

@router.message(F.text == BTN_APPEAL, F.chat.type == "private")
async def start_appeal(message: Message, state: FSMContext):
    register_user(message.from_user.id)
    if is_banned(message.from_user.id): return
    await state.clear()
    await state.set_state(Form.appeal_nickname)
    await message.answer("1️⃣ <b>Ваш ник:</b>\nУкажите ваш основной ник в игре (не display name).", parse_mode="HTML")

@router.message(F.text == BTN_FRIENDS, F.chat.type == "private")
async def start_friends_temp(message: Message, state: FSMContext):
    register_user(message.from_user.id)
    if is_banned(message.from_user.id): return
    await state.clear()
    await message.answer(
        "🛠 <b>Добавление в друзья временно недоступно.</b>\n\n"
        "В данный момент аккаунт находится на обслуживании. Если у вас возник вопрос по поводу VIP или добавления в друзья, пожалуйста, перейдите во вкладку <b>«❓ Задать вопрос»</b>.",
        reply_markup=main_keyboard(),
        parse_mode="HTML"
    )

@router.message(F.text == BTN_QUESTION, F.chat.type == "private")
async def start_question(message: Message, state: FSMContext):
    register_user(message.from_user.id)
    if is_banned(message.from_user.id): return
    await state.clear()
    await state.set_state(Form.question_text)
    await message.answer("❓ <b>Задайте ваш вопрос одним сообщением:</b>", parse_mode="HTML")

# ----------------------------------------------------------------------
# СБОР ДАННЫХ В ФОРМАХ (FSM)
# ----------------------------------------------------------------------
@router.message(Form.complaint_reason)
async def process_c_reason(message: Message, state: FSMContext):
    await state.update_data(c_reason=message.text)
    await state.set_state(Form.complaint_nickname)
    await message.answer("2️⃣ <b>Ник нарушителя:</b>\nУкажите основной ник (не display name).", parse_mode="HTML")

@router.message(Form.complaint_nickname)
async def process_c_nickname(message: Message, state: FSMContext):
    await state.update_data(c_nickname=message.text)
    await state.set_state(Form.complaint_photo)
    await message.answer("3️⃣ <b>Доказательства (Фото/Скриншот):</b>\n⚠️ Фото НЕ ОБРЕЗАТЬ! Плашка уровней справа должна быть видна.", parse_mode="HTML")

@router.message(Form.complaint_photo, F.photo)
async def process_c_photo(message: Message, state: FSMContext):
    await state.update_data(c_photo=message.photo[-1].file_id)
    await state.set_state(Form.complaint_server)
    await message.answer(
        "4️⃣ <b>На каком сервере произошло нарушение?</b>\n"
        "Укажите название или номер сервера (если название повторяется, добавьте уточняющий номер, например: <i>Tower 2</i> или <i>VIP</i>):",
        parse_mode="HTML"
    )

@router.message(Form.complaint_server)
async def process_c_server(message: Message, state: FSMContext):
    data = await state.get_data()
    ticket_id = create_ticket(message.from_user.id)
    user_mention = get_user_mention(message.from_user)
    
    c_reason = html.escape(data.get('c_reason', ''))
    c_nickname = html.escape(data.get('c_nickname', ''))
    c_server = html.escape(message.text or '')

    admin_text = (
        f"🚨 <b>#Жалоба | Заявка №{ticket_id}</b>\n"
        f"👤 От: {user_mention} | ID: <code>{message.from_user.id}</code>\n\n"
        f"1️⃣ <b>Суть:</b> {c_reason}\n"
        f"2️⃣ <b>Ник нарушителя:</b> <code>{c_nickname}</code>\n"
        f"4️⃣ <b>Сервер:</b> {c_server}\n\n"
        f"🔘 <i>Выберите действие ниже:</i>"
    )
    sent = await bot.send_photo(
        ADMIN_CHAT_ID, 
        photo=data['c_photo'], 
        caption=admin_text, 
        reply_markup=take_ticket_kb(ticket_id),
        parse_mode="HTML"
    )
    map_message(sent.message_id, message.from_user.id, ticket_id)
    
    await message.answer(f"✅ Ваша жалоба отправлена администрации! Номер заявки: <b>№{ticket_id}</b>.", parse_mode="HTML")
    await state.clear()

@router.message(Form.appeal_nickname)
async def process_a_nickname(message: Message, state: FSMContext):
    await state.update_data(a_nickname=message.text)
    await state.set_state(Form.appeal_reason)
    await message.answer("2️⃣ <b>Почему вы считаете, что должны быть разблокированы?</b>", parse_mode="HTML")

@router.message(Form.appeal_reason)
async def process_a_reason(message: Message, state: FSMContext):
    data = await state.get_data()
    ticket_id = create_ticket(message.from_user.id)
    user_mention = get_user_mention(message.from_user)
    
    a_nickname = html.escape(data.get('a_nickname', ''))
    a_reason = html.escape(message.text or '')

    admin_text = (
        f"😡 <b>#Обжалование | Заявка №{ticket_id}</b>\n"
        f"👤 От: {user_mention} | ID: <code>{message.from_user.id}</code>\n\n"
        f"1️⃣ <b>Ник:</b> <code>{a_nickname}</code>\n"
        f"2️⃣ <b>Причина:</b> {a_reason}\n\n"
        f"🔘 <i>Выберите действие ниже:</i>"
    )
    sent = await bot.send_message(
        ADMIN_CHAT_ID, 
        admin_text, 
        reply_markup=take_ticket_kb(ticket_id),
        parse_mode="HTML"
    )
    map_message(sent.message_id, message.from_user.id, ticket_id)
    
    await message.answer(f"✅ Ваше обжалование отправлено на рассмотрение! Номер заявки: <b>№{ticket_id}</b>.", parse_mode="HTML")
    await state.clear()

@router.message(Form.question_text)
async def process_question(message: Message, state: FSMContext):
    ticket_id = create_ticket(message.from_user.id)
    user_mention = get_user_mention(message.from_user)
    question = html.escape(message.text or '')
    
    admin_text = (
        f"❓ <b>#Вопрос | Заявка №{ticket_id}</b>\n"
        f"👤 От: {user_mention} | ID: <code>{message.from_user.id}</code>\n\n"
        f"<b>Вопрос:</b> {question}\n\n"
        f"🔘 <i>Выберите действие ниже:</i>"
    )
    sent = await bot.send_message(
        ADMIN_CHAT_ID, 
        admin_text, 
        reply_markup=take_ticket_kb(ticket_id),
        parse_mode="HTML"
    )
    map_message(sent.message_id, message.from_user.id, ticket_id)
    
    await message.answer(f"✅ Ваш вопрос отправлен поддержке! Номер заявки: <b>№{ticket_id}</b>.", parse_mode="HTML")
    await state.clear()

# ----------------------------------------------------------------------
# КНОПКИ В ГРУППЕ: ПРИНЯТЬ И ОТКЛОНИТЬ ЗАЯВКУ
# ----------------------------------------------------------------------
@router.callback_query(F.data.startswith("take_"))
async def take_ticket_handler(call: CallbackQuery):
    ticket_id = int(call.data.split("_")[1])
    admin_id = call.from_user.id

    ticket_info = get_ticket_info(ticket_id)
    if ticket_info and ticket_info[2] != 'pending':
        await call.answer("❌ Эта заявка уже обработана или закрыта!", show_alert=True)
        return

    activate_ticket(ticket_id, admin_id)

    if ticket_info:
        user_id = ticket_info[0]
        try:
            await bot.send_message(
                user_id, 
                f"👨‍💻 Администратор <b>{html.escape(call.from_user.full_name)}</b> принял вашу заявку <b>№{ticket_id}</b>!\n\n"
                f"Теперь вы можете писать сюда сообщения напрямую — они сразу отправятся администратору.",
                parse_mode="HTML"
            )
        except Exception:
            pass

    new_caption_or_text = (call.message.caption or call.message.text or "") + f"\n\n✅ <b>Взял в работу:</b> {call.from_user.mention_html()}"
    
    if call.message.photo:
        await call.message.edit_caption(caption=new_caption_or_text, reply_markup=close_ticket_kb(ticket_id, admin_id), parse_mode="HTML")
    else:
        await call.message.edit_text(text=new_caption_or_text, reply_markup=close_ticket_kb(ticket_id, admin_id), parse_mode="HTML")

    await call.answer(f"Вы успешно приняли заявку №{ticket_id}!")

@router.callback_query(F.data.startswith("reject_"))
async def reject_ticket_handler(call: CallbackQuery):
    ticket_id = int(call.data.split("_")[1])

    ticket_info = get_ticket_info(ticket_id)
    if ticket_info and ticket_info[2] != 'pending':
        await call.answer("❌ Эта заявка уже обработана или закрыта!", show_alert=True)
        return

    prompt_msg = await bot.send_message(
        ADMIN_CHAT_ID,
        f"❓ <b>Укажите причину отказа для заявки №{ticket_id}:</b>\n<i>(Ответьте/Reply именно на ЭТО сообщение текстом причины)</i>",
        reply_markup=ForceReply(selective=True),
        parse_mode="HTML"
    )
    add_pending_rejection(prompt_msg.message_id, ticket_id)
    await call.answer("Напишите причину отказа в ответ на новое сообщение бота!")

@router.callback_query(F.data.startswith("close_"))
async def close_ticket_handler(call: CallbackQuery):
    parts = call.data.split("_")
    ticket_id = int(parts[1])
    assigned_admin_id = int(parts[2])

    if call.from_user.id != assigned_admin_id:
        await call.answer("❌ Закрыть заявку может только тот администратор, который её принял!", show_alert=True)
        return

    ticket_info = get_ticket_info(ticket_id)
    if ticket_info:
        user_id = ticket_info[0]
        close_ticket_db(ticket_id)
        
        try:
            await bot.send_message(
                user_id, 
                f"🔒 Ваша заявка <b>№{ticket_id}</b> закрыта администратором. Спасибо за обращение!\n\n"
                f"Если у вас возникнут новые вопросы, воспользуйтесь меню на клавиатуре ниже.",
                parse_mode="HTML"
            )
        except Exception:
            pass

    status_text = f"\n\n🔒 <b>Заявка №{ticket_id} закрыта</b> администратором {call.from_user.mention_html()}."
    
    if call.message.photo:
        await call.message.edit_caption(caption=(call.message.caption or "") + status_text, parse_mode="HTML")
    else:
        await call.message.edit_text(text=(call.message.text or "") + status_text, parse_mode="HTML")

    await call.answer(f"Заявка №{ticket_id} успешно закрыта!")

# ----------------------------------------------------------------------
# ПЕРЕСЫЛКА СООБЩЕНИЙ ОТ ЮЗЕРА В ГРУППУ
# ----------------------------------------------------------------------
@router.message(F.chat.type == "private")
async def user_private_message(message: Message, state: FSMContext):
    register_user(message.from_user.id)
    if is_banned(message.from_user.id):
        return

    current_state = await state.get_state()
    if current_state is not None:
        return

    active_ticket = get_active_ticket(message.from_user.id)
    
    if active_ticket:
        user_mention = get_user_mention(message.from_user)
        user_text = html.escape(message.text or '')
        text_to_group = f"📩 <b>Сообщение по заявке №{active_ticket[0]} от {user_mention} | ID: <code>{message.from_user.id}</code>:</b>\n\n{user_text}"
        
        if message.photo:
            sent = await bot.send_photo(ADMIN_CHAT_ID, photo=message.photo[-1].file_id, caption=text_to_group, parse_mode="HTML")
        else:
            sent = await bot.send_message(ADMIN_CHAT_ID, text_to_group, parse_mode="HTML")
        
        map_message(sent.message_id, message.from_user.id, active_ticket[0])
        return

    await message.answer(
        "⚠️ <b>Общение не через формы запрещено.</b>\n"
        "Чтобы отправить заявку или задать вопрос, выберите раздел на клавиатуре ниже 👇",
        reply_markup=main_keyboard(),
        parse_mode="HTML"
    )

# ----------------------------------------------------------------------
# ОТВЕТ АДМИНА ИЗ ГРУППЫ ПОЛЬЗОВАТЕЛЮ ИЛИ ВВОД ПРИЧИНЫ ОТКАЗА
# ----------------------------------------------------------------------
@router.message(F.chat.id == ADMIN_CHAT_ID, F.reply_to_message)
async def admin_reply_in_group(message: Message):
    if message.text and message.text.startswith("/"):
        return

    replied_msg_id = message.reply_to_message.message_id

    pending_ticket_id = get_pending_rejection(replied_msg_id)
    if pending_ticket_id:
        ticket_info = get_ticket_info(pending_ticket_id)
        if ticket_info and ticket_info[2] == 'pending':
            user_id = ticket_info[0]
            reject_reason = html.escape(message.text or "Без указания причины")
            close_ticket_db(pending_ticket_id, status='rejected')
            delete_pending_rejection(replied_msg_id)

            try:
                await bot.send_message(
                    user_id,
                    f"❌ Ваша заявка <b>№{pending_ticket_id}</b> была отклонена администрацией.\n\n"
                    f"<b>Причина:</b> {reject_reason}",
                    parse_mode="HTML"
                )
            except Exception:
                pass

            await message.reply(
                f"🚫 Заявка <b>№{pending_ticket_id}</b> пользователя <code>{user_id}</code> успешно отклонена!\n"
                f"<b>Причина:</b> {reject_reason}",
                parse_mode="HTML"
            )
            return

    targetdata = get_user_by_group_msg(replied_msg_id)
    user_id, ticket_id = None, None
    if targetdata:
        user_id, ticket_id = targetdata[0], targetdata[1]
    else:
        reply_text = message.reply_to_message.text or message.reply_to_message.caption or ""
        match_id = re.search(r"ID:\s*(\d+)", reply_text)
        match_ticket = re.search(r"Заявка\s*№(\d+)", reply_text)
        if match_id: user_id = int(match_id.group(1))
        if match_ticket: ticket_id = int(match_ticket.group(1))

    if not user_id:
        return

    admin_id = message.from_user.id
    if ticket_id:
        ticket_info = get_ticket_info(ticket_id)
        if ticket_info:
            assigned_admin = ticket_info[1]
            if assigned_admin is None:
                await message.reply("⚠️ <b>Сначала нажмите кнопку «Принять заявку»</b>, чтобы отвечать на неё!", parse_mode="HTML")
                return
            elif assigned_admin != admin_id:
                await message.reply("❌ Эту заявку обрабатывает другой администратор! Вы не можете отправлять ответы в этот тикет.")
                return

    try:
        if message.photo:
            caption = html.escape(message.caption or '')
            await bot.send_photo(user_id, photo=message.photo[-1].file_id, caption=f"👨‍💻 <b>Ответ поддержки:</b>\n\n{caption}", parse_mode="HTML")
        else:
            text = html.escape(message.text or '')
            await bot.send_message(user_id, f"👨‍💻 <b>Ответ поддержки:</b>\n\n{text}", parse_mode="HTML")
        
        await message.reply(
            f"✅ Ответ отправлен пользователю <code>{user_id}</code>!", 
            reply_markup=close_ticket_kb(ticket_id, admin_id) if ticket_id else None,
            parse_mode="HTML"
        )
    except Exception as e:
        await message.reply(f"❌ Не удалось отправить сообщение пользователю.\nОшибка: <code>{html.escape(str(e))}</code>", parse_mode="HTML")

# ----------------------------------------------------------------------
# КОМАНДА РАССЫЛКИ /news <текст>
# ----------------------------------------------------------------------
@router.message(Command("news"), F.chat.id == ADMIN_CHAT_ID)
async def news_broadcast_command(message: Message):
    args = message.text.split(maxsplit=1) if message.text else []
    
    broadcast_text = args[1] if len(args) > 1 else (message.caption.split(maxsplit=1)[1] if message.caption and len(message.caption.split(maxsplit=1)) > 1 else None)
    
    if not broadcast_text and not message.reply_to_message:
        await message.reply(
            "📢 <b>Инструкция по рассылке:</b>\n\n"
            "• Напишите: <code>/news Ваш текст объявления</code>\n"
            "• Или ответьте (reply) на фото командой <code>/news Ваш текст</code>",
            parse_mode="HTML"
        )
        return

    all_users = get_all_users()
    if not all_users:
        await message.reply("❌ В базе данных нет зарегистрированных пользователей для рассылки.")
        return

    status_msg = await message.reply(f"🚀 Начинаю рассылку для {len(all_users)} пользователей...")
    
    success_count = 0
    failed_count = 0

    photo_id = None
    if message.photo:
        photo_id = message.photo[-1].file_id
    elif message.reply_to_message and message.reply_to_message.photo:
        photo_id = message.reply_to_message.photo[-1].file_id
        if not broadcast_text:
            broadcast_text = message.reply_to_message.caption or message.reply_to_message.text

    final_text = f"📢 <b>НОВОСТИ / ОБЪЯВЛЕНИЕ</b>\n\n{broadcast_text or ''}"

    for user_id in all_users:
        try:
            if photo_id:
                await bot.send_photo(user_id, photo=photo_id, caption=final_text, parse_mode="HTML")
            else:
                await bot.send_message(user_id, final_text, parse_mode="HTML")
            success_count += 1
            await asyncio.sleep(0.05)
        except Exception:
            failed_count += 1

    await status_msg.edit_text(
        f"✅ <b>Рассылка завершена!</b>\n\n"
        f"📊 <b>Результаты:</b>\n"
        f"• Доставлено: <b>{success_count}</b>\n"
        f"• Не доставлено: <b>{failed_count}</b>",
        parse_mode="HTML"
    )

# ----------------------------------------------------------------------
# УНИВЕРСАЛЬНЫЕ КОМАНДЫ БАНА / РАЗБАНА В ГРУППЕ
# ----------------------------------------------------------------------
@router.message(Command("ban"), F.chat.id == ADMIN_CHAT_ID)
async def ban_command(message: Message):
    target_user_id = None
    args = message.text.split(maxsplit=2)
    reason = "Нарушение правил / спам"

    if len(args) > 1 and args[1].isdigit():
        target_user_id = int(args[1])
        if len(args) > 2:
            reason = args[2]

    elif message.reply_to_message:
        targetdata = get_user_by_group_msg(message.reply_to_message.message_id)
        if targetdata:
            target_user_id = targetdata[0]
        else:
            orig_msg = message.reply_to_message
            orig_text = orig_msg.text or orig_msg.caption or ""
            
            if orig_msg.entities or orig_msg.caption_entities:
                entities = orig_msg.entities or orig_msg.caption_entities
                for entity in entities:
                    if entity.type == "text_link" and entity.url and "tg://user?id=" in entity.url:
                        target_user_id = int(entity.url.split("id=")[1])
                        break
            
            if not target_user_id:
                match = re.search(r"ID:?\s*(\d{8,11})", orig_text, re.IGNORECASE)
                if match:
                    target_user_id = int(match.group(1))

        if len(args) > 1:
            reason = " ".join(args[1:])

    if not target_user_id:
        await message.reply(
            "⚠️ <b>Не удалось определить ID пользователя.</b>\n\n"
            "Вы можете:\n"
            "1️⃣ Нажать на моноширинный ID в карточке заявки (он скопируется), после чего написать: <code>/ban 123456789 Причина</code>\n"
            "2️⃣ Или ответить командой <code>/ban Причина</code> прямо на сообщение заявки.",
            parse_mode="HTML"
        )
        return

    ban_user_db(target_user_id, reason)
    await message.reply(f"🚫 Пользователь <code>{target_user_id}</code> заблокирован в системе поддержки.\n<b>Причина:</b> {html.escape(reason)}", parse_mode="HTML")
    
    try:
        await bot.send_message(target_user_id, f"❌ Вы заблокированы в поддержке.\n<b>Причина:</b> {html.escape(reason)}", parse_mode="HTML")
    except Exception:
        pass

@router.message(Command("unban"), F.chat.id == ADMIN_CHAT_ID)
async def unban_command(message: Message):
    target_user_id = None
    args = message.text.split()

    if len(args) > 1 and args[1].isdigit():
        target_user_id = int(args[1])
    elif message.reply_to_message:
        targetdata = get_user_by_group_msg(message.reply_to_message.message_id)
        if targetdata:
            target_user_id = targetdata[0]
        else:
            orig_msg = message.reply_to_message
            orig_text = orig_msg.text or orig_msg.caption or ""
            match = re.search(r"ID:?\s*(\d{8,11})", orig_text, re.IGNORECASE)
            if match:
                target_user_id = int(match.group(1))

    if not target_user_id:
        await message.reply("⚠️ Укажите ID пользователя (<code>/unban 1234567</code>) или ответьте на его сообщение этой командой.", parse_mode="HTML")
        return

    unban_user_db(target_user_id)
    await message.reply(f"✅ Пользователь <code>{target_user_id}</code> разблокирован!", parse_mode="HTML")
    try:
        await bot.send_message(target_user_id, "✅ Ваш доступ к поддержке восстановлен!")
    except Exception:
        pass

# ----------------------------------------------------------------------
# HEALTH CHECK ВЕБ-СЕРВЕР ДЛЯ RENDER
# ----------------------------------------------------------------------
async def handle_health_check(request):
    return web.Response(text="Bot Support ToH Secrets is running successfully!")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_health_check)
    app.router.add_get("/health", handle_health_check)

    runner = web.AppRunner(app)
    await runner.setup()

    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"HTTP-сервер слушает порт {port}.")

# ----------------------------------------------------------------------
# ЗАПУСК БОТА
# ----------------------------------------------------------------------
async def main():
    asyncio.create_task(start_web_server())
    await bot.delete_webhook(drop_pending_updates=True)
    print("Бот поддержки Tower Of Hell Secrets успешно запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
    
