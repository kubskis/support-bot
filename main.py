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
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))  # Твой личный Telegram ID для доступа к админке

if not BOT_TOKEN:
    raise ValueError("ОШИБКА: Токен бота не найден! Укажите BOT_TOKEN в Environment Variables.")
if not DATABASE_URL:
    raise ValueError("ОШИБКА: Строка подключения к БД не найдена! Укажите DATABASE_URL.")

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
    try:
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
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );
        """)
        conn.commit()
        cursor.close()
        conn.close()
        print("База данных успешно инициализирована.")
    except Exception as e:
        print(f"Внимание: ошибка при инициализации БД: {e}")

init_db()

def get_setting(key, default="false"):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM settings WHERE key = %s", (key,))
        row = cursor.fetchone()
        cursor.close()
        conn.close()
        return row[0] if row else default
    except Exception:
        return default

def set_setting(key, value):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (key, value)
        )
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        print(f"Ошибка сохранения настройки {key}: {e}")

def register_user(user_id):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("INSERT INTO users (user_id) VALUES (%s) ON CONFLICT (user_id) DO NOTHING", (user_id,))
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        print(f"Ошибка регистрации пользователя: {e}")

def get_all_users_count():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM users")
        res = cursor.fetchone()[0]
        cursor.close()
        conn.close()
        return res
    except Exception:
        return 0

def get_active_tickets_count():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM tickets WHERE status = 'active' OR status = 'pending'")
        res = cursor.fetchone()[0]
        cursor.close()
        conn.close()
        return res
    except Exception:
        return 0

def get_closed_tickets_stats():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        # Для простоты: общее число закрытых/отклоненных
        cursor.execute("SELECT COUNT(*) FROM tickets WHERE status IN ('closed', 'rejected')")
        total = cursor.fetchone()[0]
        cursor.close()
        conn.close()
        return total
    except Exception:
        return 0

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
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT reason FROM banned_users WHERE user_id = %s", (user_id,))
        row = cursor.fetchone()
        cursor.close()
        conn.close()
        return row
    except Exception:
        return None

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
    friends_nickname = State()
    question_text = State()
    
    # Анкета модератора
    mod_name = State()
    mod_age = State()
    mod_exp = State()
    mod_why_you = State()
    mod_why_us = State()

    # Анкета искателя секреток
    secret_name = State()
    secret_age = State()
    secret_exp = State()
    secret_vip = State()
    secret_why_you = State()
    secret_why_us = State()

    # Установка ники для друзей через админку
    set_friend_nick = State()

# ----------------------------------------------------------------------
# КЛАВИАТУРЫ
# ----------------------------------------------------------------------
BTN_COMPLAINT = "🚨 Жалоба на игрока"
BTN_APPEAL = "😡 Обжалование бана"
BTN_FRIENDS = "👯‍♀️ Добавление в друзья (VIP)"
BTN_QUESTION = "❓ Задать вопрос"
BTN_MOD = "🛡 Набор на модератора"
BTN_SECRET = "🔍 Набор на искателя секреток"
BTN_ADMIN_PANEL = "⚙️ Админ-панель"
BTN_REFRESH = "🔄 Перезагрузить меню"

def main_keyboard(user_id):
    keyboard = [
        [KeyboardButton(text=BTN_COMPLAINT), KeyboardButton(text=BTN_APPEAL)],
        [KeyboardButton(text=BTN_FRIENDS), KeyboardButton(text=BTN_QUESTION)]
    ]
    
    if get_setting("mod_active", "false") == "true":
        keyboard.append([KeyboardButton(text=BTN_MOD)])
    if get_setting("secret_active", "false") == "true":
        keyboard.append([KeyboardButton(text=BTN_SECRET)])
        
    keyboard.append([KeyboardButton(text=BTN_REFRESH)])

    # Кнопка админ-панели доступна только создателю
    if ADMIN_ID and user_id == ADMIN_ID:
        keyboard.append([KeyboardButton(text=BTN_ADMIN_PANEL)])

    return ReplyKeyboardMarkup(
        keyboard=keyboard,
        resize_keyboard=True,
        persistent=True
    )

def admin_panel_kb():
    mod_status = "🟢 Вкл" if get_setting("mod_active", "false") == "true" else "🔴 Выкл"
    secret_status = "🟢 Вкл" if get_setting("secret_active", "false") == "true" else "🔴 Выкл"
    friend_status = "🟢 Вкл" if get_setting("friend_active", "false") == "true" else "🔴 Выкл"

    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика", callback_data="adm_stats")],
        [InlineKeyboardButton(text=f"🛡 Набор модераторов: {mod_status}", callback_data="toggle_mod")],
        [InlineKeyboardButton(text=f"🔍 Искатели секреток: {secret_status}", callback_data="toggle_secret")],
        [InlineKeyboardButton(text=f"👯‍♀️ Добавление в друзья: {friend_status}", callback_data="toggle_friend")],
        [InlineKeyboardButton(text="✏️ Изменить ник для друзей", callback_data="change_friend_nick")]
    ])

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
        "🤍 <b>Добро пожаловать в поддержку Tower Of Hell Secrets (@ToHSecretss)!</b> 🤍\n\n"
        "Нажмите на нужную кнопку на клавиатуре ниже, чтобы отправить заявку или задать вопрос."
    )
    await message.answer(welcome_text, reply_markup=main_keyboard(message.from_user.id), parse_mode="HTML")

# ----------------------------------------------------------------------
# ОБРАБОТКА КНОПОК И МЕНЮ
# ----------------------------------------------------------------------
@router.message(F.text == BTN_REFRESH, F.chat.type == "private")
async def refresh_menu_handler(message: Message, state: FSMContext):
    register_user(message.from_user.id)
    banned = is_banned(message.from_user.id)
    if banned:
        await message.answer(f"❌ Вы заблокированы в поддержке.\n<b>Причина:</b> {html.escape(banned[0])}", parse_mode="HTML")
        return
    await state.clear()
    await message.answer(
        "🔄 <b>Меню успешно обновлено!</b>",
        reply_markup=main_keyboard(message.from_user.id),
        parse_mode="HTML"
    )

@router.message(F.text == BTN_ADMIN_PANEL, F.chat.type == "private")
async def open_admin_panel(message: Message, state: FSMContext):
    if not ADMIN_ID or message.from_user.id != ADMIN_ID:
        return
    await state.clear()
    await message.answer(
        "⚙️ <b>Панель администратора</b>\n\nВыберите нужный раздел или настройку:",
        reply_markup=admin_panel_kb(),
        parse_mode="HTML"
    )

# Коллбэки админ панели
@router.callback_query(F.data == "adm_stats")
async def callback_stats(call: CallbackQuery):
    if not ADMIN_ID or call.from_user.id != ADMIN_ID:
        await call.answer("Нет доступа", show_alert=True)
        return
    
    users_count = get_all_users_count()
    active_tickets = get_active_tickets_count()
    closed_tickets = get_closed_tickets_stats()

    stats_text = (
        "📊 <b>Статистика и аналитика поддержки:</b>\n\n"
        f"👥 Всего пользователей в базе: <code>{users_count}</code>\n"
        f"📬 Активных тикетов прямо сейчас: <code>{active_tickets}</code>\n"
        f"🔒 Всего закрытых заявок: <code>{closed_tickets}</code>"
    )
    await call.message.edit_text(stats_text, reply_markup=admin_panel_kb(), parse_mode="HTML")
    await call.answer()

@router.callback_query(F.data == "toggle_mod")
async def toggle_mod_callback(call: CallbackQuery):
    if not ADMIN_ID or call.from_user.id != ADMIN_ID: return
    current = get_setting("mod_active", "false")
    new_val = "false" if current == "true" else "true"
    set_setting("mod_active", new_val)
    
    status_str = "включен ✅" if new_val == "true" else "выключен ❌"
    await call.answer(f"Набор на модератора {status_str}")
    await call.message.edit_reply_markup(reply_markup=admin_panel_kb())

@router.callback_query(F.data == "toggle_secret")
async def toggle_secret_callback(call: CallbackQuery):
    if not ADMIN_ID or call.from_user.id != ADMIN_ID: return
    current = get_setting("secret_active", "false")
    new_val = "false" if current == "true" else "true"
    set_setting("secret_active", new_val)
    
    status_str = "включен ✅" if new_val == "true" else "выключен ❌"
    await call.answer(f"Набор на искателя секреток {status_str}")
    await call.message.edit_reply_markup(reply_markup=admin_panel_kb())

@router.callback_query(F.data == "toggle_friend")
async def toggle_friend_callback(call: CallbackQuery):
    if not ADMIN_ID or call.from_user.id != ADMIN_ID: return
    current = get_setting("friend_active", "false")
    new_val = "false" if current == "true" else "true"
    set_setting("friend_active", new_val)
    
    status_str = "включен ✅" if new_val == "true" else "выключен ❌"
    await call.answer(f"Добавление в друзья {status_str}")
    await call.message.edit_reply_markup(reply_markup=admin_panel_kb())

@router.callback_query(F.data == "change_friend_nick")
async def change_friend_nick_callback(call: CallbackQuery, state: FSMContext):
    if not ADMIN_ID or call.from_user.id != ADMIN_ID: return
    await state.set_state(Form.set_friend_nick)
    current_nick = get_setting("friend_nickname", "Не задан")
    await call.message.answer(
        f"✏️ Введите новый ник (не display name), на который игроки должны кидать заявку в друзья.\n"
        f"Текущий ник: <code>{html.escape(current_nick)}</code>",
        parse_mode="HTML"
    )
    await call.answer()

@router.message(Form.set_friend_nick)
async def save_friend_nick(message: Message, state: FSMContext):
    if not ADMIN_ID or message.from_user.id != ADMIN_ID: return
    new_nick = message.text.strip()
    set_setting("friend_nickname", new_nick)
    await state.clear()
    await message.answer(f"✅ Ник успешно изменен на: <code>{html.escape(new_nick)}</code>", parse_mode="HTML", reply_markup=main_keyboard(message.from_user.id))

# ----------------------------------------------------------------------
# ОСНОВНЫЕ РАЗДЕЛЫ И АНКЕТЫ
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
async def start_friends(message: Message, state: FSMContext):
    register_user(message.from_user.id)
    if is_banned(message.from_user.id): return
    await state.clear()
    
    if get_setting("friend_active", "false") != "true":
        await message.answer(
            "🛠 <b>Вкладка добавления в друзья находится на технических работах.</b>\n\n"
            "В данный момент функционал добавления в друзья обслуживается. Пожалуйста, попробуйте позже.",
            reply_markup=main_keyboard(message.from_user.id),
            parse_mode="HTML"
        )
        return

    friend_nick = get_setting("friend_nickname", "Администратор")
    await state.set_state(Form.friends_nickname)
    await message.answer(
        f"👯‍♀️ <b>Добавление в друзья (VIP)</b>\n\n"
        f"Отправьте запрос в друзья игроку: <code>{html.escape(friend_nick)}</code>\n\n"
        f"1️⃣ <b>Укажите ваш ник в игре</b> (не display name):",
        parse_mode="HTML"
    )

@router.message(Form.friends_nickname)
async def process_friends_nickname(message: Message, state: FSMContext):
    ticket_id = create_ticket(message.from_user.id)
    user_mention = get_user_mention(message.from_user)
    f_nickname = html.escape(message.text or '')

    admin_text = (
        f"👯‍♀️ <b>#ДобавлениеВДрузья | Заявка №{ticket_id}</b>\n"
        f"👤 От: {user_mention} | ID: <code>{message.from_user.id}</code>\n\n"
        f"1️⃣ <b>Ник игрока:</b> <code>{f_nickname}</code>\n\n"
        f"🔘 <i>Выберите действие ниже:</i>"
    )

    sent = await bot.send_message(
        ADMIN_CHAT_ID,
        admin_text,
        reply_markup=take_ticket_kb(ticket_id),
        parse_mode="HTML"
    )
    map_message(sent.message_id, message.from_user.id, ticket_id)
    await message.answer(f"✅ Ваша заявка на добавление в друзья отправлена! Номер заявки: <b>№{ticket_id}</b>.", parse_mode="HTML", reply_markup=main_keyboard(message.from_user.id))
    await state.clear()

@router.message(F.text == BTN_QUESTION, F.chat.type == "private")
async def start_question(message: Message, state: FSMContext):
    register_user(message.from_user.id)
    if is_banned(message.from_user.id): return
    await state.clear()
    await state.set_state(Form.question_text)
    await message.answer("❓ <b>Задайте ваш вопрос одним сообщением:</b>", parse_mode="HTML")

# --- АНКЕТА МОДЕРАТОРА ---
@router.message(F.text == BTN_MOD, F.chat.type == "private")
async def start_mod_form(message: Message, state: FSMContext):
    register_user(message.from_user.id)
    if is_banned(message.from_user.id): return
    if get_setting("mod_active", "false") != "true":
        return
    await state.clear()
    await state.set_state(Form.mod_name)
    await message.answer("🛡 <b>Набор на модератора</b>\n\n1️⃣ <b>Ваше имя:</b>", parse_mode="HTML")

@router.message(Form.mod_name)
async def mod_name_step(message: Message, state: FSMContext):
    await state.update_data(mod_name=message.text)
    await state.set_state(Form.mod_age)
    await message.answer("2️⃣ <b>Ваш возраст:</b>", parse_mode="HTML")

@router.message(Form.mod_age)
async def mod_age_step(message: Message, state: FSMContext):
    await state.update_data(mod_age=message.text)
    await state.set_state(Form.mod_exp)
    await message.answer("3️⃣ Были ли вы когда-то на такой должности или схожих, если да, то укажите ссылку на канал где вы работали:", parse_mode="HTML")

@router.message(Form.mod_exp)
async def mod_exp_step(message: Message, state: FSMContext):
    await state.update_data(mod_exp=message.text)
    await state.set_state(Form.mod_why_you)
    await message.answer("4️⃣ Почему мы должны взять именно вас?", parse_mode="HTML")

@router.message(Form.mod_why_you)
async def mod_why_you_step(message: Message, state: FSMContext):
    await state.update_data(mod_why_you=message.text)
    await state.set_state(Form.mod_why_us)
    await message.answer("5️⃣ Почему вы хотите именно к нам?", parse_mode="HTML")

@router.message(Form.mod_why_us)
async def mod_finish_step(message: Message, state: FSMContext):
    data = await state.get_data()
    ticket_id = create_ticket(message.from_user.id)
    user_mention = get_user_mention(message.from_user)

    admin_text = (
        f"🛡 <b>#АнкетаМодератор | Заявка №{ticket_id}</b>\n"
        f"👤 От: {user_mention} | ID: <code>{message.from_user.id}</code>\n\n"
        f"1️⃣ <b>Имя:</b> {html.escape(data.get('mod_name',''))}\n"
        f"2️⃣ <b>Возраст:</b> {html.escape(data.get('mod_age',''))}\n"
        f"3️⃣ <b>Опыт/Канал:</b> {html.escape(data.get('mod_exp',''))}\n"
        f"4️⃣ <b>Почему вы:</b> {html.escape(data.get('mod_why_you',''))}\n"
        f"5️⃣ <b>Почему к нам:</b> {html.escape(message.text or '')}\n\n"
        f"🔘 <i>Выберите действие ниже:</i>"
    )

    sent = await bot.send_message(ADMIN_CHAT_ID, admin_text, reply_markup=take_ticket_kb(ticket_id), parse_mode="HTML")
    map_message(sent.message_id, message.from_user.id, ticket_id)
    await message.answer(f"✅ Ваша анкета модератора отправлена! Номер заявки: <b>№{ticket_id}</b>.", parse_mode="HTML", reply_markup=main_keyboard(message.from_user.id))
    await state.clear()

# --- АНКЕТА ИСКАТЕЛЯ СЕКРЕТОК ---
@router.message(F.text == BTN_SECRET, F.chat.type == "private")
async def start_secret_form(message: Message, state: FSMContext):
    register_user(message.from_user.id)
    if is_banned(message.from_user.id): return
    if get_setting("secret_active", "false") != "true":
        return
    await state.clear()
    await state.set_state(Form.secret_name)
    await message.answer("🔍 <b>Набор на искателя секреток</b>\n\n1️⃣ <b>Ваше имя:</b>", parse_mode="HTML")

@router.message(Form.secret_name)
async def secret_name_step(message: Message, state: FSMContext):
    await state.update_data(secret_name=message.text)
    await state.set_state(Form.secret_age)
    await message.answer("2️⃣ <b>Ваш возраст:</b>", parse_mode="HTML")

@router.message(Form.secret_age)
async def secret_age_step(message: Message, state: FSMContext):
    await state.update_data(secret_age=message.text)
    await state.set_state(Form.secret_exp)
    await message.answer("3️⃣ Были ли вы когда-то на такой должности или схожих, если да, то укажите ссылку на канал где вы работали:", parse_mode="HTML")

@router.message(Form.secret_exp)
async def secret_exp_step(message: Message, state: FSMContext):
    await state.update_data(secret_exp=message.text)
    await state.set_state(Form.secret_vip)
    await message.answer("4️⃣ Есть ли у вас вип сервер в тавер оф хелл?", parse_mode="HTML")

@router.message(Form.secret_vip)
async def secret_vip_step(message: Message, state: FSMContext):
    await state.update_data(secret_vip=message.text)
    await state.set_state(Form.secret_why_you)
    await message.answer("5️⃣ Почему мы должны взять именно вас?", parse_mode="HTML")

@router.message(Form.secret_why_you)
async def secret_why_you_step(message: Message, state: FSMContext):
    await state.update_data(secret_why_you=message.text)
    await state.set_state(Form.secret_why_us)
    await message.answer("6️⃣ Почему вы хотите именно к нам?", parse_mode="HTML")

@router.message(Form.secret_why_us)
async def secret_finish_step(message: Message, state: FSMContext):
    data = await state.get_data()
    ticket_id = create_ticket(message.from_user.id)
    user_mention = get_user_mention(message.from_user)

    admin_text = (
        f"🔍 <b>#НаборСекретки | Заявка №{ticket_id}</b>\n"
        f"👤 От: {user_mention} | ID: <code>{message.from_user.id}</code>\n\n"
        f"1️⃣ <b>Имя:</b> {html.escape(data.get('secret_name',''))}\n"
        f"2️⃣ <b>Возраст:</b> {html.escape(data.get('secret_age',''))}\n"
        f"3️⃣ <b>Опыт/Канал:</b> {html.escape(data.get('secret_exp',''))}\n"
        f"4️⃣ <b>VIP-сервер:</b> {html.escape(data.get('secret_vip',''))}\n"
        f"5️⃣ <b>Почему вы:</b> {html.escape(data.get('secret_why_you',''))}\n"
        f"6️⃣ <b>Почему к нам:</b> {html.escape(message.text or '')}\n\n"
        f"🔘 <i>Выберите действие ниже:</i>"
    )

    sent = await bot.send_message(ADMIN_CHAT_ID, admin_text, reply_markup=take_ticket_kb(ticket_id), parse_mode="HTML")
    map_message(sent.message_id, message.from_user.id, ticket_id)
    await message.answer(f"✅ Ваша анкета искателя секреток отправлена! Номер заявки: <b>№{ticket_id}</b>.", parse_mode="HTML", reply_markup=main_keyboard(message.from_user.id))
    await state.clear()

# ----------------------------------------------------------------------
# СБОР ДАННЫХ В ФОРМАХ (Жалобы, Вопросы, Обжалования)
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
        "Укажите название или номер сервера:",
        parse_mode="HTML"
    )

@router.message(Form.complaint_server)
async def process_c_server(message: Message, state: FSMContext):
    data = await state.get_data()
    ticket_id = create_ticket(message.from_user.id)
    user_mention = get_user_mention(message.from_user)

    admin_text = (
        f"🚨 <b>#Жалоба | Заявка №{ticket_id}</b>\n"
        f"👤 От: {user_mention} | ID: <code>{message.from_user.id}</code>\n\n"
        f"1️⃣ <b>Суть:</b> {html.escape(data.get('c_reason',''))}\n"
        f"2️⃣ <b>Ник нарушителя:</b> <code>{html.escape(data.get('c_nickname',''))}</code>\n"
        f"4️⃣ <b>Сервер:</b> {html.escape(message.text or '')}\n\n"
        f"🔘 <i>Выберите действие ниже:</i>"
    )

    sent = await bot.send_photo(ADMIN_CHAT_ID, photo=data['c_photo'], caption=admin_text, reply_markup=take_ticket_kb(ticket_id), parse_mode="HTML")
    map_message(sent.message_id, message.from_user.id, ticket_id)
    await message.answer(f"✅ Жалоба отправлена! Номер: <b>№{ticket_id}</b>.", parse_mode="HTML", reply_markup=main_keyboard(message.from_user.id))
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

    admin_text = (
        f"😡 <b>#Обжалование | Заявка №{ticket_id}</b>\n"
        f"👤 От: {user_mention} | ID: <code>{message.from_user.id}</code>\n\n"
        f"1️⃣ <b>Ник:</b> <code>{html.escape(data.get('a_nickname',''))}</code>\n"
        f"2️⃣ <b>Причина:</b> {html.escape(message.text or '')}\n\n"
        f"🔘 <i>Выберите действие ниже:</i>"
    )

    sent = await bot.send_message(ADMIN_CHAT_ID, admin_text, reply_markup=take_ticket_kb(ticket_id), parse_mode="HTML")
    map_message(sent.message_id, message.from_user.id, ticket_id)
    await message.answer(f"✅ Обжалование отправлено! Номер: <b>№{ticket_id}</b>.", parse_mode="HTML", reply_markup=main_keyboard(message.from_user.id))
    await state.clear()

@router.message(Form.question_text)
async def process_question(message: Message, state: FSMContext):
    ticket_id = create_ticket(message.from_user.id)
    user_mention = get_user_mention(message.from_user)

    admin_text = (
        f"❓ <b>#Вопрос | Заявка №{ticket_id}</b>\n"
        f"👤 От: {user_mention} | ID: <code>{message.from_user.id}</code>\n\n"
        f"<b>Вопрос:</b> {html.escape(message.text or '')}\n\n"
        f"🔘 <i>Выберите действие ниже:</i>"
    )

    sent = await bot.send_message(ADMIN_CHAT_ID, admin_text, reply_markup=take_ticket_kb(ticket_id), parse_mode="HTML")
    map_message(sent.message_id, message.from_user.id, ticket_id)
    await message.answer(f"✅ Вопрос отправлен! Номер: <b>№{ticket_id}</b>.", parse_mode="HTML", reply_markup=main_keyboard(message.from_user.id))
    await state.clear()

# ----------------------------------------------------------------------
# КНОПКИ В ГРУППЕ: ПРИНЯТЬ, ОТКЛОНИТЬ, ЗАКРЫТЬ
# ----------------------------------------------------------------------
@router.callback_query(F.data.startswith("take_"))
async def take_ticket_handler(call: CallbackQuery):
    ticket_id = int(call.data.split("_")[1])
    admin_id = call.from_user.id
    ticket_info = get_ticket_info(ticket_id)
    if ticket_info and ticket_info[2] != 'pending':
        await call.answer("❌ Эта заявка уже обработана!", show_alert=True)
        return

    activate_ticket(ticket_id, admin_id)
    if ticket_info:
        try:
            await bot.send_message(
                ticket_info[0],
                f"👨‍💻 Администратор <b>{html.escape(call.from_user.full_name)}</b> принял вашу заявку <b>№{ticket_id}</b>!\n\n"
                f"Теперь вы можете писать сюда сообщения напрямую.",
                parse_mode="HTML"
            )
        except Exception:
            pass

    new_caption_or_text = (call.message.caption or call.message.text or "") + f"\n\n✅ <b>Взял в работу:</b> {call.from_user.mention_html()}"
    if call.message.photo:
        await call.message.edit_caption(caption=new_caption_or_text, reply_markup=close_ticket_kb(ticket_id, admin_id), parse_mode="HTML")
    else:
        await call.message.edit_text(text=new_caption_or_text, reply_markup=close_ticket_kb(ticket_id, admin_id), parse_mode="HTML")
    await call.answer(f"Вы приняли заявку №{ticket_id}!")

@router.callback_query(F.data.startswith("reject_"))
async def reject_ticket_handler(call: CallbackQuery):
    ticket_id = int(call.data.split("_")[1])
    ticket_info = get_ticket_info(ticket_id)
    if ticket_info and ticket_info[2] != 'pending':
        await call.answer("❌ Эта заявка уже обработана!", show_alert=True)
        return

    prompt_msg = await bot.send_message(
        ADMIN_CHAT_ID,
        f"❓ <b>Укажите причину отказа для заявки №{ticket_id}:</b>\n<i>(Ответьте Reply на это сообщение)</i>",
        reply_markup=ForceReply(selective=True),
        parse_mode="HTML"
    )
    add_pending_rejection(prompt_msg.message_id, ticket_id)
    await call.answer("Напишите причину отказа в ответ на новое сообщение!")

@router.callback_query(F.data.startswith("close_"))
async def close_ticket_handler(call: CallbackQuery):
    parts = call.data.split("_")
    ticket_id = int(parts[1])
    assigned_admin_id = int(parts[2])

    if call.from_user.id != assigned_admin_id and (not ADMIN_ID or call.from_user.id != ADMIN_ID):
        await call.answer("❌ Закрыть заявку может только принявший её администратор!", show_alert=True)
        return

    ticket_info = get_ticket_info(ticket_id)
    if ticket_info:
        close_ticket_db(ticket_id)
        try:
            await bot.send_message(
                ticket_info[0],
                f"🔒 Ваша заявка <b>№{ticket_id}</b> закрыта администратором. Спасибо за обращение!",
                parse_mode="HTML",
                reply_markup=main_keyboard(ticket_info[0])
            )
        except Exception:
            pass

    status_text = f"\n\n🔒 <b>Заявка №{ticket_id} закрыта</b> администратором {call.from_user.mention_html()}."
    if call.message.photo:
        await call.message.edit_caption(caption=(call.message.caption or "") + status_text, parse_mode="HTML")
    else:
        await call.message.edit_text(text=(call.message.text or "") + status_text, parse_mode="HTML")
    await call.answer(f"Заявка №{ticket_id} закрыта!")

# ----------------------------------------------------------------------
# ПЕРЕСЫЛКА СООБЩЕНИЙ ПОЛЬЗОВАТЕЛЯ И АДМИНА
# ----------------------------------------------------------------------
@router.message(F.chat.type == "private")
async def user_private_message(message: Message, state: FSMContext):
    register_user(message.from_user.id)
    if is_banned(message.from_user.id): return

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
        "⚠️ Пожалуйста, используйте кнопки на клавиатуре ниже для отправки заявок.",
        reply_markup=main_keyboard(message.from_user.id),
        parse_mode="HTML"
    )

@router.message(F.chat.id == ADMIN_CHAT_ID, F.reply_to_message)
async def admin_reply_in_group(message: Message):
    if message.text and message.text.startswith("/"):
        return

    replied_msg_id = message.reply_to_message.message_id
    pending_ticket_id = get_pending_rejection(replied_msg_id)
    
    if pending_ticket_id:
        ticket_info = get_ticket_info(pending_ticket_id)
        if ticket_info:
            user_id = ticket_info[0]
            reason = html.escape(message.text or "Причина не указана")
            close_ticket_db(pending_ticket_id, status='rejected')
            try:
                await bot.send_message(
                    user_id,
                    f"❌ Ваша заявка <b>№{pending_ticket_id}</b> была отклонена.\n<b>Причина:</b> {reason}",
                    parse_mode="HTML",
                    reply_markup=main_keyboard(user_id)
                )
            except Exception:
                pass

            orig_msg = message.reply_to_message
            status_text = f"\n\n❌ <b>Заявка отклонена</b>\n<b>Причина:</b> {reason}"
            if orig_msg.photo:
                await orig_msg.edit_caption(caption=(orig_msg.caption or "") + status_text, parse_mode="HTML")
            else:
                await orig_msg.edit_text(text=(orig_msg.text or "") + status_text, parse_mode="HTML")

        delete_pending_rejection(replied_msg_id)
        await message.answer("✅ Отказ отправлен, заявка закрыта.")
        return

    mapping = get_user_by_group_msg(replied_msg_id)
    if mapping:
        user_id, ticket_id = mapping[0], mapping[1]
        ticket_info = get_ticket_info(ticket_id)

        if ticket_info and ticket_info[2] == 'active':
            admin_name = html.escape(message.from_user.full_name)
            admin_text = message.text or message.caption or ""
            client_msg_text = f"👨‍💻 <b>Ответ от администратора {admin_name}:</b>\n\n{html.escape(admin_text)}"

            try:
                if message.photo:
                    await bot.send_photo(user_id, photo=message.photo[-1].file_id, caption=client_msg_text, parse_mode="HTML")
                else:
                    await bot.send_message(user_id, client_msg_text, parse_mode="HTML")
                await message.react([{"type": "emoji", "emoji": "👍"}])
            except Exception as e:
                await message.answer(f"❌ Ошибка отправки: {e}")
        else:
            await message.answer("❌ Тикет неактивен.")

# ----------------------------------------------------------------------
# КОМАНДЫ БАНА / РАЗБАНА
# ----------------------------------------------------------------------
@router.message(Command("ban"), F.chat.id == ADMIN_CHAT_ID)
async def cmd_ban(message: Message):
    args = message.text.split(maxsplit=2)
    if len(args) < 2:
        await message.answer("Использование: <code>/ban [user_id] [причина]</code>", parse_mode="HTML")
        return
    try:
        user_id = int(args[1])
        reason = args[2] if len(args) > 2 else "Нарушение правил"
        ban_user_db(user_id, reason)
        await message.answer(f"✅ Пользователь <code>{user_id}</code> заблокирован.", parse_mode="HTML")
    except ValueError:
        await message.answer("❌ Неверный формат.")

@router.message(Command("unban"), F.chat.id == ADMIN_CHAT_ID)
async def cmd_unban(message: Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Использование: <code>/unban [user_id]</code>", parse_mode="HTML")
        return
    try:
        user_id = int(args[1])
        unban_user_db(user_id)
        await message.answer(f"✅ Пользователь <code>{user_id}</code> разблокирован.", parse_mode="HTML")
    except ValueError:
        await message.answer("❌ Неверный формат.")

# ----------------------------------------------------------------------
# ВЕБ-СЕРВЕР И ЗАПУСК
# ----------------------------------------------------------------------
async def handle_ping(request):
    return web.Response(text="Bot is running!")

async def web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"Веб-сервер запущен на порту {port}")

async def main():
    await web_server()
    print("Бот запущен...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
