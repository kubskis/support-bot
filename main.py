import asyncio
import logging
import os
import html
import re
import psycopg2
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
OWNER_ID = int(os.getenv("ADMIN_ID", "0"))  # Главный создатель бота

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
    conn = None
    cursor = None
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
                status TEXT DEFAULT 'pending',
                category TEXT DEFAULT 'general'
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
            CREATE TABLE IF NOT EXISTS ratings (
                ticket_id INTEGER PRIMARY KEY,
                user_id BIGINT,
                admin_id BIGINT,
                score INTEGER,
                review TEXT
            );
            CREATE TABLE IF NOT EXISTS main_admins (
                admin_id BIGINT PRIMARY KEY
            );
        """)
        cursor.execute("""
            ALTER TABLE tickets ADD COLUMN IF NOT EXISTS category TEXT DEFAULT 'general';
        """)
        conn.commit()
        
        if OWNER_ID:
            cursor.execute("INSERT INTO main_admins (admin_id) VALUES (%s) ON CONFLICT (admin_id) DO NOTHING", (OWNER_ID,))
            conn.commit()

        print("База данных успешно инициализирована.")
    except Exception as e:
        print(f"Внимание: ошибка при инициализации БД: {e}")
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

init_db()

def is_main_admin(user_id: int) -> bool:
    if OWNER_ID and user_id == OWNER_ID:
        return True
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT admin_id FROM main_admins WHERE admin_id = %s", (user_id,))
        row = cursor.fetchone()
        return row is not None
    except Exception:
        return False
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

async def is_support_member(user_id: int) -> bool:
    if is_main_admin(user_id):
        return True
    try:
        member = await bot.get_chat_member(chat_id=ADMIN_CHAT_ID, user_id=user_id)
        if member.status in ["creator", "administrator", "member"]:
            return True
    except Exception:
        pass
    return False

def get_setting(key, default="false"):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM settings WHERE key = %s", (key,))
        row = cursor.fetchone()
        return row[0] if row else default
    except Exception:
        return default
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

def set_setting(key, value):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (key, value)
        )
        conn.commit()
    except Exception as e:
        print(f"Ошибка сохранения настройки {key}: {e}")
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

def register_user(user_id):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("INSERT INTO users (user_id) VALUES (%s) ON CONFLICT (user_id) DO NOTHING", (user_id,))
        conn.commit()
    except Exception as e:
        print(f"Ошибка регистрации пользователя: {e}")
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

def get_all_users_count():
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM users")
        res = cursor.fetchone()[0]
        return res
    except Exception:
        return 0
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

def get_tickets_stats():
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM tickets")
        total = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM tickets WHERE status = 'closed'")
        closed = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM tickets WHERE status = 'rejected'")
        rejected = cursor.fetchone()[0]
        
        cursor.execute("SELECT COALESCE(AVG(score), 0) FROM ratings WHERE user_id != %s", (OWNER_ID,))
        avg_rating = cursor.fetchone()[0]
        
        return total, closed, rejected, round(float(avg_rating), 2)
    except Exception:
        return 0, 0, 0, 0.0
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

def get_admin_list_stats():
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT admin_id, COUNT(*) as closed_count, COALESCE(AVG(score), 0) as avg_score
            FROM tickets t
            LEFT JOIN ratings r ON t.ticket_id = r.ticket_id
            WHERE t.admin_id IS NOT NULL AND t.status = 'closed'
            GROUP BY t.admin_id
        """)
        rows = cursor.fetchall()
        return rows
    except Exception:
        return []
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

def create_ticket(user_id, category='general'):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("INSERT INTO tickets (user_id, status, category) VALUES (%s, 'pending', %s) RETURNING ticket_id", (user_id, category))
        ticket_id = cursor.fetchone()[0]
        conn.commit()
        return ticket_id
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

def map_message(group_msg_id, user_id, ticket_id):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO message_map (group_message_id, user_id, ticket_id) VALUES (%s, %s, %s) "
            "ON CONFLICT (group_message_id) DO UPDATE SET user_id = EXCLUDED.user_id, ticket_id = EXCLUDED.ticket_id",
            (group_msg_id, user_id, ticket_id)
        )
        conn.commit()
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

def get_user_by_group_msg(group_msg_id):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT user_id, ticket_id FROM message_map WHERE group_message_id = %s", (group_msg_id,))
        row = cursor.fetchone()
        return row
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

def get_ticket_info(ticket_id):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT user_id, admin_id, status FROM tickets WHERE ticket_id = %s", (ticket_id,))
        row = cursor.fetchone()
        return row
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

def get_active_ticket(user_id):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT ticket_id, admin_id, status FROM tickets WHERE user_id = %s AND status IN ('pending', 'active') ORDER BY ticket_id DESC LIMIT 1", (user_id,))
        row = cursor.fetchone()
        return row
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

def activate_ticket(ticket_id, admin_id):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE tickets SET status = 'active', admin_id = %s WHERE ticket_id = %s", (admin_id, ticket_id))
        conn.commit()
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

def close_ticket_db(ticket_id, status='closed'):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE tickets SET status = %s WHERE ticket_id = %s", (status, ticket_id))
        conn.commit()
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

def is_banned(user_id):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT reason FROM banned_users WHERE user_id = %s", (user_id,))
        row = cursor.fetchone()
        return row
    except Exception:
        return None
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

def ban_user_db(user_id, reason):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO banned_users (user_id, reason) VALUES (%s, %s) "
            "ON CONFLICT (user_id) DO UPDATE SET reason = EXCLUDED.reason",
            (user_id, reason)
        )
        conn.commit()
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

def save_rating_db(ticket_id, user_id, admin_id, score, review=None):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO ratings (ticket_id, user_id, admin_id, score, review) VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (ticket_id) DO UPDATE SET score = EXCLUDED.score, review = EXCLUDED.review",
            (ticket_id, user_id, admin_id, score, review)
        )
        conn.commit()
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

def add_pending_rejection(prompt_message_id, ticket_id):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO pending_rejections (prompt_message_id, ticket_id) VALUES (%s, %s) "
            "ON CONFLICT (prompt_message_id) DO UPDATE SET ticket_id = EXCLUDED.ticket_id",
            (prompt_message_id, ticket_id)
        )
        conn.commit()
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

def get_pending_rejection(prompt_message_id):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT ticket_id FROM pending_rejections WHERE prompt_message_id = %s", (prompt_message_id,))
        row = cursor.fetchone()
        return row[0] if row else None
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

def delete_pending_rejection(prompt_message_id):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM pending_rejections WHERE prompt_message_id = %s", (prompt_message_id,))
        conn.commit()
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

# ----------------------------------------------------------------------
# СОСТОЯНИЯ (FSM)
# ----------------------------------------------------------------------
class Form(StatesGroup):
    complaint_reason = State()
    complaint_nickname = State()
    complaint_photo = State()
    appeal_nickname = State()
    appeal_reason = State()
    friends_nickname = State()
    question_text = State()
    set_friend_nick = State()
    
    helper_age = State()
    helper_timezone = State()
    helper_experience = State()

    secret_age = State()
    secret_experience = State()
    secret_activity = State()

    add_main_admin_id = State()
    del_main_admin_id = State()

# ----------------------------------------------------------------------
# КЛАВИАТУРЫ
# ----------------------------------------------------------------------
BTN_COMPLAINT = "🚨 Жалоба на игрока"
BTN_APPEAL = "😡 Обжалование бана"
BTN_FRIENDS = "👯‍♀️ Добавление в друзья (VIP)"
BTN_QUESTION = "❓ Задать вопрос"
BTN_HELPER_APPLY = "📝 Подать заявку на хелпера"
BTN_SECRET_APPLY = "🔍 Набор в искатели секреток"
BTN_ADMIN_PANEL = "⚙️ Админ-панель"
BTN_REFRESH = "🔄 Перезагрузить меню"

def main_keyboard(user_id):
    keyboard = [
        [KeyboardButton(text=BTN_COMPLAINT), KeyboardButton(text=BTN_APPEAL)],
        [KeyboardButton(text=BTN_FRIENDS), KeyboardButton(text=BTN_QUESTION)],
        [KeyboardButton(text=BTN_HELPER_APPLY), KeyboardButton(text=BTN_SECRET_APPLY)],
        [KeyboardButton(text=BTN_REFRESH)]
    ]
    if is_main_admin(user_id):
        keyboard.append([KeyboardButton(text=BTN_ADMIN_PANEL)])

    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True, persistent=True)

def admin_panel_kb():
    friend_status = "🟢 Вкл" if get_setting("friend_active", "false") == "true" else "🔴 Выкл"
    helper_status = "🟢 Вкл" if get_setting("helper_recruitment", "true") == "true" else "🔴 Выкл"
    secret_status = "🟢 Вкл" if get_setting("secret_recruitment", "true") == "true" else "🔴 Выкл"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Общая статистика", callback_data="adm_stats"),
         InlineKeyboardButton(text="👥 Статистика по админам", callback_data="adm_list_stats")],
        [InlineKeyboardButton(text=f"👯‍♀️ Добавление в друзья: {friend_status}", callback_data="toggle_friend")],
        [InlineKeyboardButton(text=f"📝 Набор в хелперы: {helper_status}", callback_data="toggle_helper")],
        [InlineKeyboardButton(text=f"🔍 Искатели секреток: {secret_status}", callback_data="toggle_secret")],
        [InlineKeyboardButton(text="👑 Управление главными админами", callback_data="manage_main_admins")],
        [InlineKeyboardButton(text="✏️ Изменить ник для друзей", callback_data="change_friend_nick")]
    ])

def take_ticket_kb(ticket_id):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🟢 Начать работу", callback_data=f"take_{ticket_id}"),
        InlineKeyboardButton(text="🔴 Отклонить тикет", callback_data=f"reject_{ticket_id}")
    ]])

def close_ticket_kb(ticket_id, admin_id):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔒 Закрыть тикет", callback_data=f"close_{ticket_id}_{admin_id}")
    ]])

def user_cancel_kb(ticket_id):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="❌ Закрыть заявку", callback_data=f"user_cancel_{ticket_id}")
    ]])

def rating_kb(ticket_id):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⭐ 1", callback_data=f"rate_{ticket_id}_1"),
        InlineKeyboardButton(text="⭐ 2", callback_data=f"rate_{ticket_id}_2"),
        InlineKeyboardButton(text="⭐ 3", callback_data=f"rate_{ticket_id}_3"),
        InlineKeyboardButton(text="⭐ 4", callback_data=f"rate_{ticket_id}_4"),
        InlineKeyboardButton(text="⭐ 5", callback_data=f"rate_{ticket_id}_5"),
    ]])

def helper_decision_kb(user_id):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🟢 Принять", callback_data=f"helper_accept_{user_id}"),
        InlineKeyboardButton(text="🔴 Отклонить", callback_data=f"helper_reject_{user_id}")
    ]])

def secret_decision_kb(user_id):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🟢 Принять", callback_data=f"secret_accept_{user_id}"),
        InlineKeyboardButton(text="🔴 Отклонить", callback_data=f"secret_reject_{user_id}")
    ]])

# ----------------------------------------------------------------------
# СТАРТ И МЕНЮ
# ----------------------------------------------------------------------
@router.message(CommandStart(), F.chat.type == "private")
async def start_cmd(message: Message, state: FSMContext):
    register_user(message.from_user.id)
    banned = is_banned(message.from_user.id)
    if banned:
        await message.answer(f"❌ Вы заблокированы в поддержке.\n<b>Причина:</b> {html.escape(banned[0])}", parse_mode="HTML")
        return
    await state.clear()
    await message.answer(
        "🤍 <b>Добро пожаловать в поддержку Tower Of Hell Secrets (@ToHSecretss)!</b> 🤍\n\n"
        "Выберите нужный раздел на клавиатуре ниже:",
        reply_markup=main_keyboard(message.from_user.id),
        parse_mode="HTML"
    )

@router.message(F.text == BTN_REFRESH, F.chat.type == "private")
async def refresh_menu_handler(message: Message, state: FSMContext):
    register_user(message.from_user.id)
    if is_banned(message.from_user.id): return
    await state.clear()
    await message.answer("🔄 <b>Меню обновлено!</b>", reply_markup=main_keyboard(message.from_user.id), parse_mode="HTML")

@router.message(F.text == BTN_ADMIN_PANEL, F.chat.type == "private")
async def open_admin_panel(message: Message, state: FSMContext):
    if not is_main_admin(message.from_user.id):
        return
    await state.clear()
    await message.answer("⚙️ <b>Панель администратора</b>", reply_markup=admin_panel_kb(), parse_mode="HTML")

@router.callback_query(F.data == "adm_stats")
async def callback_stats(call: CallbackQuery):
    if not is_main_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    users_count = get_all_users_count()
    total, closed, rejected, avg_rating = get_tickets_stats()

    stats_text = (
        "📊 <b>Общая статистика поддержки:</b>\n\n"
        f"👥 Активных пользователей: <code>{users_count}</code>\n"
        f"📬 Всего тикетов: <code>{total}</code>\n"
        f"✅ Успешно закрыто: <code>{closed}</code>\n"
        f"❌ Отклонено: <code>{rejected}</code>\n"
        f"⭐ Общая оценка саппорта: <code>{avg_rating} / 5.0</code>"
    )
    await call.message.edit_text(stats_text, reply_markup=admin_panel_kb(), parse_mode="HTML")
    await call.answer()

@router.callback_query(F.data == "adm_list_stats")
async def callback_admin_list_stats(call: CallbackQuery):
    if not is_main_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    
    rows = get_admin_list_stats()
    if not rows:
        text = "👥 <b>Статистика по администраторам:</b>\n\nПока нет закрытых тикетов у админов."
    else:
        text = "👥 <b>Статистика по администраторам:</b>\n\n"
        for admin_id, closed_cnt, avg_score in rows:
            try:
                chat_member = await bot.get_chat(admin_id)
                name = chat_member.full_name
            except Exception:
                name = f"ID: {admin_id}"
            text += f"👤 <b>{html.escape(name)}</b> (<code>{admin_id}</code>)\n"
            text += f"   • Закрыто тикетов: <code>{closed_cnt}</code>\n"
            text += f"   • Средняя оценка: <code>{round(float(avg_score), 2)} / 5.0</code>\n\n"

    back_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_adm")]])
    await call.message.edit_text(text, reply_markup=back_kb, parse_mode="HTML")
    await call.answer()

@router.callback_query(F.data == "back_to_adm")
async def back_to_admin_panel(call: CallbackQuery):
    if not is_main_admin(call.from_user.id): return
    await call.message.edit_text("⚙️ <b>Панель администратора</b>", reply_markup=admin_panel_kb(), parse_mode="HTML")
    await call.answer()

@router.callback_query(F.data == "manage_main_admins")
async def manage_main_admins_callback(call: CallbackQuery):
    if OWNER_ID and call.from_user.id != OWNER_ID:
        await call.answer("❌ Только создатель бота может управлять главными админами!", show_alert=True)
        return
    
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT admin_id FROM main_admins")
        res = cursor.fetchall()
    except Exception:
        res = []
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

    text = "👑 <b>Главные администраторы бота:</b>\n\n"
    for r in res:
        text += f"• <code>{r[0]}</code>\n"
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить главного админа", callback_data="add_main_admin_start")],
        [InlineKeyboardButton(text="➖ Удалить главного админа", callback_data="del_main_admin_start")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_adm")]
    ])
    await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await call.answer()

@router.callback_query(F.data == "add_main_admin_start")
async def add_main_admin_start(call: CallbackQuery, state: FSMContext):
    if OWNER_ID and call.from_user.id != OWNER_ID:
        await call.answer("❌ Доступно только создателю бота!", show_alert=True)
        return
    await state.set_state(Form.add_main_admin_id)
    await call.message.answer("➕ Введите <b>Telegram ID</b> пользователя, которого хотите сделать главным администратором:", parse_mode="HTML")
    await call.answer()

@router.message(Form.add_main_admin_id, F.chat.type == "private")
async def process_add_main_admin(message: Message, state: FSMContext):
    if OWNER_ID and message.from_user.id != OWNER_ID:
        return
    try:
        new_id = int(message.text.strip())
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("INSERT INTO main_admins (admin_id) VALUES (%s) ON CONFLICT (admin_id) DO NOTHING", (new_id,))
        conn.commit()
        cursor.close()
        conn.close()
        await state.clear()
        await message.answer(f"✅ Пользователь <code>{new_id}</code> успешно назначен главным администратором!", reply_markup=main_keyboard(message.from_user.id), parse_mode="HTML")
    except ValueError:
        await message.answer("❌ Неверный формат ID. Введите числовой Telegram ID:")

@router.callback_query(F.data == "del_main_admin_start")
async def del_main_admin_start(call: CallbackQuery, state: FSMContext):
    if OWNER_ID and call.from_user.id != OWNER_ID:
        await call.answer("❌ Доступно только создателю бота!", show_alert=True)
        return
    await state.set_state(Form.del_main_admin_id)
    await call.message.answer("➖ Введите <b>Telegram ID</b> главного администратора, которого хотите снять:", parse_mode="HTML")
    await call.answer()

@router.message(Form.del_main_admin_id, F.chat.type == "private")
async def process_del_main_admin(message: Message, state: FSMContext):
    if OWNER_ID and message.from_user.id != OWNER_ID:
        return
    try:
        del_id = int(message.text.strip())
        if OWNER_ID and del_id == OWNER_ID:
            await message.answer("❌ Нельзя удалить создателя бота!")
            return
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM main_admins WHERE admin_id = %s", (del_id,))
        conn.commit()
        cursor.close()
        conn.close()
        await state.clear()
        await message.answer(f"✅ Пользователь <code>{del_id}</code> снят с поста главного администратора.", reply_markup=main_keyboard(message.from_user.id), parse_mode="HTML")
    except ValueError:
        await message.answer("❌ Неверный формат ID. Введите числовой Telegram ID:")

@router.callback_query(F.data == "toggle_friend")
async def toggle_friend_callback(call: CallbackQuery):
    if not is_main_admin(call.from_user.id): return
    current = get_setting("friend_active", "false")
    new_val = "false" if current == "true" else "true"
    set_setting("friend_active", new_val)
    await call.answer("Статус изменен")
    await call.message.edit_reply_markup(reply_markup=admin_panel_kb())

@router.callback_query(F.data == "toggle_helper")
async def toggle_helper_callback(call: CallbackQuery):
    if not is_main_admin(call.from_user.id): return
    current = get_setting("helper_recruitment", "true")
    new_val = "false" if current == "true" else "true"
    set_setting("helper_recruitment", new_val)
    await call.answer("Статус набора хелперов изменен")
    await call.message.edit_reply_markup(reply_markup=admin_panel_kb())

@router.callback_query(F.data == "toggle_secret")
async def toggle_secret_callback(call: CallbackQuery):
    if not is_main_admin(call.from_user.id): return
    current = get_setting("secret_recruitment", "true")
    new_val = "false" if current == "true" else "true"
    set_setting("secret_recruitment", new_val)
    await call.answer("Статус набора в искатели изменен")
    await call.message.edit_reply_markup(reply_markup=admin_panel_kb())

@router.callback_query(F.data == "change_friend_nick")
async def change_friend_nick_callback(call: CallbackQuery, state: FSMContext):
    if not is_main_admin(call.from_user.id): return
    await state.set_state(Form.set_friend_nick)
    current_nick = get_setting("friend_nickname", "Не задан")
    await call.message.answer(f"✏️ Введите новый ник для друзей. Текущий: <code>{html.escape(current_nick)}</code>", parse_mode="HTML")
    await call.answer()

@router.message(Form.set_friend_nick)
async def save_friend_nick(message: Message, state: FSMContext):
    if not is_main_admin(message.from_user.id): return
    set_setting("friend_nickname", message.text.strip())
    await state.clear()
    await message.answer("✅ Ник успешно изменен!", reply_markup=main_keyboard(message.from_user.id))

# ----------------------------------------------------------------------
# СИСТЕМА НАБОРА В ХЕЛПЕРЫ И ИСКАТЕЛИ СЕКРЕТОВ
# ----------------------------------------------------------------------
@router.message(F.text == BTN_HELPER_APPLY, F.chat.type == "private")
async def start_helper_apply(message: Message, state: FSMContext):
    register_user(message.from_user.id)
    if is_banned(message.from_user.id): return
    if get_setting("helper_recruitment", "true") != "true":
        await message.answer("🛠 Набор в команду поддержки в данный момент закрыт.")
        return
    await state.clear()
    await state.set_state(Form.helper_age)
    await message.answer("📝 <b>Заявка в команду поддержки (Хелперы)</b>\n\n1️⃣ Укажите ваш возраст:", parse_mode="HTML")

@router.message(Form.helper_age)
async def process_helper_age(message: Message, state: FSMContext):
    await state.update_data(helper_age=message.text)
    await state.set_state(Form.helper_timezone)
    await message.answer("2️⃣ Укажите ваш часовой пояс (например, МСК, +2 от МСК):", parse_mode="HTML")

@router.message(Form.helper_timezone)
async def process_helper_timezone(message: Message, state: FSMContext):
    await state.update_data(helper_timezone=message.text)
    await state.set_state(Form.helper_experience)
    await message.answer("3️⃣ Был ли у вас опыт работы в поддержке или на аналогичных проектах? Опишите кратко:", parse_mode="HTML")

@router.message(Form.helper_experience)
async def process_helper_experience(message: Message, state: FSMContext):
    data = await state.get_data()
    user = message.from_user
    user_mention = get_user_mention(user)

    admin_text = (
        f"📝 <b>Новая заявка на хелпера!</b>\n\n"
        f"👤 От: {user_mention}\n"
        f"🆔 ID: <code>{user.id}</code>\n"
        f"👤 Username: @{user.username if user.username else 'отсутствует'}\n\n"
        f"👶 <b>Возраст:</b> {html.escape(data.get('helper_age'))}\n"
        f"🌍 <b>Часовой пояс:</b> {html.escape(data.get('helper_timezone'))}\n"
        f"💼 <b>Опыт:</b> {html.escape(message.text)}"
    )

    await bot.send_message(ADMIN_CHAT_ID, admin_text, reply_markup=helper_decision_kb(user.id), parse_mode="HTML")
    await message.answer("✅ Ваша анкета успешно отправлена администрации! Ожидайте ответа.", reply_markup=main_keyboard(user.id), parse_mode="HTML")
    await state.clear()

@router.callback_query(F.data.startswith("helper_accept_"))
async def helper_accept_callback(call: CallbackQuery):
    if not await is_support_member(call.from_user.id):
        await call.answer("❌ У вас нет прав!", show_alert=True)
        return
    user_id = int(call.data.split("_")[2])
    try:
        await bot.send_message(user_id, "🎉 <b>Поздравляем! Ваша заявка на хелпера принята!</b> Админы свяжутся с вами в ближайшее время.", parse_mode="HTML")
    except Exception:
        pass
    await call.message.edit_text(call.message.text + "\n\n🟢 <b>Статус:</b> Одобрено ✅", parse_mode="HTML")
    await call.answer("Заявка принята!")

@router.callback_query(F.data.startswith("helper_reject_"))
async def helper_reject_callback(call: CallbackQuery):
    if not await is_support_member(call.from_user.id):
        await call.answer("❌ У вас нет прав!", show_alert=True)
        return
    user_id = int(call.data.split("_")[2])
    try:
        await bot.send_message(user_id, "❌ К сожалению, ваша заявка на хелпера была отклонена.", parse_mode="HTML")
    except Exception:
        pass
    await call.message.edit_text(call.message.text + "\n\n🔴 <b>Статус:</b> Отклонено ❌", parse_mode="HTML")
    await call.answer("Заявка отклонена!")

@router.message(F.text == BTN_SECRET_APPLY, F.chat.type == "private")
async def start_secret_apply(message: Message, state: FSMContext):
    register_user(message.from_user.id)
    if is_banned(message.from_user.id): return
    if get_setting("secret_recruitment", "true") != "true":
        await message.answer("🛠 Набор в искатели секреток в данный момент закрыт.")
        return
    await state.clear()
    await state.set_state(Form.secret_age)
    await message.answer("🔍 <b>Заявка в искатели секреток</b>\n\n1️⃣ Укажите ваш возраст:", parse_mode="HTML")

@router.message(Form.secret_age)
async def process_secret_age(message: Message, state: FSMContext):
    await state.update_data(secret_age=message.text)
    await state.set_state(Form.secret_experience)
    await message.answer("2️⃣ Есть ли у вас опыт поиска секретов/пасхалок? Расскажите о своих успехах:", parse_mode="HTML")

@router.message(Form.secret_experience)
async def process_secret_experience(message: Message, state: FSMContext):
    await state.update_data(secret_experience=message.text)
    await state.set_state(Form.secret_activity)
    await message.answer("3️⃣ Сколько времени вы готовы уделять игре и проекту ежедневно?", parse_mode="HTML")

@router.message(Form.secret_activity)
async def process_secret_activity(message: Message, state: FSMContext):
    data = await state.get_data()
    user = message.from_user
    user_mention = get_user_mention(user)

    admin_text = (
        f"🔍 <b>Новая заявка в искатели секреток!</b>\n\n"
        f"👤 От: {user_mention}\n"
        f"🆔 ID: <code>{user.id}</code>\n"
        f"👤 Username: @{user.username if user.username else 'отсутствует'}\n\n"
        f"👶 <b>Возраст:</b> {html.escape(data.get('secret_age'))}\n"
        f"💼 <b>Опыт:</b> {html.escape(data.get('secret_experience'))}\n"
        f"⏱ <b>Онлайн:</b> {html.escape(message.text)}"
    )

    await bot.send_message(ADMIN_CHAT_ID, admin_text, reply_markup=secret_decision_kb(user.id), parse_mode="HTML")
    await message.answer("✅ Ваша заявка в искатели секреток успешно отправлена! Ожидайте ответа.", reply_markup=main_keyboard(user.id), parse_mode="HTML")
    await state.clear()

@router.callback_query(F.data.startswith("secret_accept_"))
async def secret_accept_callback(call: CallbackQuery):
    if not await is_support_member(call.from_user.id):
        await call.answer("❌ У вас нет прав!", show_alert=True)
        return
    user_id = int(call.data.split("_")[2])
    try:
        await bot.send_message(user_id, "🎉 <b>Поздравляем! Ваша заявка в искатели секреток принята!</b>", parse_mode="HTML")
    except Exception:
        pass
    await call.message.edit_text(call.message.text + "\n\n🟢 <b>Статус:</b> Одобрено ✅", parse_mode="HTML")
    await call.answer("Заявка принята!")

@router.callback_query(F.data.startswith("secret_reject_"))
async def secret_reject_callback(call: CallbackQuery):
    if not await is_support_member(call.from_user.id):
        await call.answer("❌ У вас нет прав!", show_alert=True)
        return
    user_id = int(call.data.split("_")[2])
    try:
        await bot.send_message(user_id, "❌ К сожалению, ваша заявка в искатели секреток была отклонена.", parse_mode="HTML")
    except Exception:
        pass
    await call.message.edit_text(call.message.text + "\n\n🔴 <b>Статус:</b> Отклонено ❌", parse_mode="HTML")
    await call.answer("Заявка отклонена!")

# ----------------------------------------------------------------------
# СОЗДАНИЕ ТИКЕТОВ
# ----------------------------------------------------------------------
async def check_active_ticket(message: Message) -> bool:
    active = get_active_ticket(message.from_user.id)
    if active:
        await message.answer(
            f"⚠️ У вас уже есть активный тикет <b>№{active[0]}</b>.\nДождитесь ответа или закройте его, прежде чем открывать новый.",
            parse_mode="HTML"
        )
        return True
    return False

@router.message(F.text == BTN_COMPLAINT, F.chat.type == "private")
async def start_complaint(message: Message, state: FSMContext):
    register_user(message.from_user.id)
    if is_banned(message.from_user.id) or await check_active_ticket(message): return
    await state.clear()
    await state.set_state(Form.complaint_reason)
    await message.answer("1️⃣ <b>Суть нарушения:</b>\nОпишите подробно, что именно произошло.", parse_mode="HTML")

@router.message(F.text == BTN_APPEAL, F.chat.type == "private")
async def start_appeal(message: Message, state: FSMContext):
    register_user(message.from_user.id)
    if is_banned(message.from_user.id) or await check_active_ticket(message): return
    await state.clear()
    await state.set_state(Form.appeal_nickname)
    await message.answer("1️⃣ <b>Ваш ник в игре:</b>", parse_mode="HTML")

@router.message(F.text == BTN_FRIENDS, F.chat.type == "private")
async def start_friends(message: Message, state: FSMContext):
    register_user(message.from_user.id)
    if is_banned(message.from_user.id) or await check_active_ticket(message): return
    if get_setting("friend_active", "false") != "true":
        await message.answer("🛠 Раздел добавления в друзья временно закрыт на технические работы.")
        return
    await state.clear()
    friend_nick = get_setting("friend_nickname", "Администратор")
    await state.set_state(Form.friends_nickname)
    await message.answer(f"👯‍♀️ Добавьтесь в друзья к игроку: <code>{html.escape(friend_nick)}</code>\n\n1️⃣ Укажите ваш ник в игре:", parse_mode="HTML")

@router.message(F.text == BTN_QUESTION, F.chat.type == "private")
async def start_question(message: Message, state: FSMContext):
    register_user(message.from_user.id)
    if is_banned(message.from_user.id) or await check_active_ticket(message): return
    await state.clear()
    await state.set_state(Form.question_text)
    await message.answer("❓ Задайте ваш вопрос одним сообщением:", parse_mode="HTML")

@router.message(Form.friends_nickname)
async def process_friends_nickname(message: Message, state: FSMContext):
    if await check_active_ticket(message): return
    ticket_id = create_ticket(message.from_user.id, "friends")
    user_mention = get_user_mention(message.from_user)
    admin_text = f"👯‍♀️ <b>#Друзья | Заявка №{ticket_id}</b>\n👤 От: {user_mention} | ID: <code>{message.from_user.id}</code>\n\nНик: <code>{html.escape(message.text)}</code>"
    sent = await bot.send_message(ADMIN_CHAT_ID, admin_text, reply_markup=take_ticket_kb(ticket_id), parse_mode="HTML")
    map_message(sent.message_id, message.from_user.id, ticket_id)
    await message.answer(f"✅ Заявка №{ticket_id} создана!", reply_markup=user_cancel_kb(ticket_id), parse_mode="HTML")
    await state.clear()

@router.message(Form.question_text)
async def process_question(message: Message, state: FSMContext):
    if await check_active_ticket(message): return
    ticket_id = create_ticket(message.from_user.id, "question")
    user_mention = get_user_mention(message.from_user)
    admin_text = f"❓ <b>#Вопрос | Заявка №{ticket_id}</b>\n👤 От: {user_mention} | ID: <code>{message.from_user.id}</code>\n\n{html.escape(message.text)}"
    sent = await bot.send_message(ADMIN_CHAT_ID, admin_text, reply_markup=take_ticket_kb(ticket_id), parse_mode="HTML")
    map_message(sent.message_id, message.from_user.id, ticket_id)
    await message.answer(f"✅ Вопрос №{ticket_id} отправлен!", reply_markup=user_cancel_kb(ticket_id), parse_mode="HTML")
    await state.clear()

@router.message(Form.complaint_reason)
async def process_c_reason(message: Message, state: FSMContext):
    await state.update_data(c_reason=message.text)
    await state.set_state(Form.complaint_nickname)
    await message.answer("2️⃣ Укажите ник нарушителя:", parse_mode="HTML")

@router.message(Form.complaint_nickname)
async def process_c_nickname(message: Message, state: FSMContext):
    await state.update_data(c_nickname=message.text)
    await state.set_state(Form.complaint_photo)
    await message.answer("3️⃣ Отправьте скриншот-доказательство (плашка уровней должна быть видна):", parse_mode="HTML")

@router.message(Form.complaint_photo, F.photo)
async def process_c_photo(message: Message, state: FSMContext):
    if await check_active_ticket(message): return
    await state.update_data(c_photo=message.photo[-1].file_id)
    ticket_id = create_ticket(message.from_user.id, "complaint")
    data = await state.get_data()
    user_mention = get_user_mention(message.from_user)
    admin_text = f"🚨 <b>#Жалоба | Заявка №{ticket_id}</b>\n👤 От: {user_mention} | ID: <code>{message.from_user.id}</code>\nСуть: {html.escape(data.get('c_reason'))}\nНарушитель: <code>{html.escape(data.get('c_nickname'))}</code>"
    sent = await bot.send_photo(ADMIN_CHAT_ID, photo=data['c_photo'], caption=admin_text, reply_markup=take_ticket_kb(ticket_id), parse_mode="HTML")
    map_message(sent.message_id, message.from_user.id, ticket_id)
    await message.answer(f"✅ Жалоба №{ticket_id} отправлена!", reply_markup=user_cancel_kb(ticket_id), parse_mode="HTML")
    await state.clear()

@router.message(Form.appeal_nickname)
async def process_a_nickname(message: Message, state: FSMContext):
    await state.update_data(a_nickname=message.text)
    await state.set_state(Form.appeal_reason)
    await message.answer("2️⃣ Опишите, почему вы хотите разблокировку:", parse_mode="HTML")

@router.message(Form.appeal_reason)
async def process_a_reason(message: Message, state: FSMContext):
    if await check_active_ticket(message): return
    data = await state.get_data()
    ticket_id = create_ticket(message.from_user.id, "appeal")
    user_mention = get_user_mention(message.from_user)
    admin_text = f"😡 <b>#Обжалование | Заявка №{ticket_id}</b>\n👤 От: {user_mention} | ID: <code>{message.from_user.id}</code>\nНик: <code>{html.escape(data.get('a_nickname'))}</code>\nПричина: {html.escape(message.text)}"
    sent = await bot.send_message(ADMIN_CHAT_ID, admin_text, reply_markup=take_ticket_kb(ticket_id), parse_mode="HTML")
    map_message(sent.message_id, message.from_user.id, ticket_id)
    await message.answer(f"✅ Обжалование №{ticket_id} отправлено!", reply_markup=user_cancel_kb(ticket_id), parse_mode="HTML")
    await state.clear()

# ----------------------------------------------------------------------
# КНОПКИ УПРАВЛЕНИЯ ТИКЕТАМИ
# ----------------------------------------------------------------------
@router.callback_query(F.data.startswith("take_"))
async def take_ticket_handler(call: CallbackQuery):
    if not await is_support_member(call.from_user.id):
        await call.answer("❌ У вас нет прав хелпера!", show_alert=True)
        return

    ticket_id = int(call.data.split("_")[1])
    ticket_info = get_ticket_info(ticket_id)
    if ticket_info and ticket_info[2] != 'pending':
        await call.answer("❌ Заявка уже занята или обработана!", show_alert=True)
        return

    activate_ticket(ticket_id, call.from_user.id)
    if ticket_info:
        try:
            await bot.send_message(
                ticket_info[0],
                f"👨‍💻 Администратор <b>{html.escape(call.from_user.full_name)}</b> взял вашу заявку <b>№{ticket_id}</b> в работу!\nТеперь вы можете писать сюда сообщения.",
                parse_mode="HTML"
            )
        except Exception:
            pass

    new_text = (call.message.caption or call.message.text or "") + f"\n\n🟢 <b>В работе у:</b> {call.from_user.mention_html()}"
    if call.message.photo:
        await call.message.edit_caption(caption=new_text, reply_markup=close_ticket_kb(ticket_id, call.from_user.id), parse_mode="HTML")
    else:
        await call.message.edit_text(text=new_text, reply_markup=close_ticket_kb(ticket_id, call.from_user.id), parse_mode="HTML")
    await call.answer(f"Заявка №{ticket_id} принята!")

@router.callback_query(F.data.startswith("reject_"))
async def reject_ticket_handler(call: CallbackQuery):
    if not await is_support_member(call.from_user.id):
        await call.answer("❌ У вас нет прав хелпера!", show_alert=True)
        return

    ticket_id = int(call.data.split("_")[1])
    ticket_info = get_ticket_info(ticket_id)
    if ticket_info and ticket_info[2] != 'pending':
        await call.answer("❌ Заявка уже обработана!", show_alert=True)
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

    if call.from_user.id != assigned_admin_id and not is_main_admin(call.from_user.id):
        if not await is_support_member(call.from_user.id):
            await call.answer("❌ Нет доступа!", show_alert=True)
            return
        await call.answer("❌ Закрыть тикет может только тот админ, который взял его в работу!", show_alert=True)
        return

    ticket_info = get_ticket_info(ticket_id)
    if ticket_info:
        close_ticket_db(ticket_id)
        try:
            await bot.send_message(
                ticket_info[0],
                f"🔒 Ваша заявка <b>№{ticket_id}</b> закрыта администрацией.\nОцените качество поддержки:",
                parse_mode="HTML",
                reply_markup=rating_kb(ticket_id)
            )
        except Exception:
            pass

    status_text = f"\n\n🔒 <b>Заявка №{ticket_id} закрыта</b> администратором {call.from_user.mention_html()}."
    if call.message.photo:
        await call.message.edit_caption(caption=(call.message.caption or "") + status_text, parse_mode="HTML")
    else:
        await call.message.edit_text(text=(call.message.text or "") + status_text, parse_mode="HTML")
    await call.answer("Заявка закрыта!")

@router.callback_query(F.data.startswith("user_cancel_"))
async def user_cancel_ticket(call: CallbackQuery):
    ticket_id = int(call.data.split("_")[2])
    ticket_info = get_ticket_info(ticket_id)
    if ticket_info and ticket_info[2] == 'pending':
        close_ticket_db(ticket_id, 'closed')
        await call.message.edit_text(f"❌ Заявка <b>№{ticket_id}</b> отменена вами.", parse_mode="HTML")
        await call.answer("Заявка отменена")
    else:
        await call.answer("❌ Заявка уже взята в работу или закрыта, отмена недоступна.", show_alert=True)

# ----------------------------------------------------------------------
# СИСТЕМА ОЦЕНОК
# ----------------------------------------------------------------------
@router.callback_query(F.data.startswith("rate_"))
async def process_rating(call: CallbackQuery, state: FSMContext):
    parts = call.data.split("_")
    ticket_id = int(parts[1])
    score = int(parts[2])
    user_id = call.from_user.id

    ticket_info = get_ticket_info(ticket_id)
    admin_id = ticket_info[1] if ticket_info else 0

    save_rating_db(ticket_id, user_id, admin_id, score)

    if is_main_admin(user_id):
        await call.message.edit_text(f"⭐ [ТЕСТ АДМИНА] Оценка {score}/5 сохранена, но в общую статистику не пошла.", parse_mode="HTML")
    else:
        await call.message.edit_text(f"⭐ Спасибо за оценку ({score}/5)! Ваше мнение учтено.", parse_mode="HTML")
    
    await call.answer("Оценка сохранена!")

# ----------------------------------------------------------------------
# ДИАЛОГ И БАНЫ
# ----------------------------------------------------------------------
@router.message(F.chat.type == "private")
async def user_private_message(message: Message, state: FSMContext):
    register_user(message.from_user.id)
    if is_banned(message.from_user.id): return

    current_state = await state.get_state()
    if current_state is not None:
        return

    active_ticket = get_active_ticket(message.from_user.id)
    if active_ticket and active_ticket[2] == 'active':
        user_mention = get_user_mention(message.from_user)
        text_to_group = f"📩 <b>Сообщение по заявке №{active_ticket[0]} от {user_mention} | ID: <code>{message.from_user.id}</code>:</b>\n\n{html.escape(message.text or message.caption or '')}"
        
        if message.photo:
            sent = await bot.send_photo(ADMIN_CHAT_ID, photo=message.photo[-1].file_id, caption=text_to_group, parse_mode="HTML")
        else:
            sent = await bot.send_message(ADMIN_CHAT_ID, text_to_group, parse_mode="HTML")
        map_message(sent.message_id, message.from_user.id, active_ticket[0])
        return

    await message.answer("⚠️ Пожалуйста, выберите нужный пункт меню для обращения.", reply_markup=main_keyboard(message.from_user.id))

@router.message(F.chat.id == ADMIN_CHAT_ID, F.reply_to_message)
async def admin_reply_in_group(message: Message):
    if message.text and message.text.startswith("/"):
        return

    replied_msg_id = message.reply_to_message.message_id
    pending_ticket_id = get_pending_rejection(replied_msg_id)

    if pending_ticket_id:
        ticket_info = get_ticket_info(pending_ticket_id)
        if ticket_info:
            reason = html.escape(message.text or message.caption or "Без причины")
            close_ticket_db(pending_ticket_id, 'rejected')
            try:
                await bot.send_message(ticket_info[0], f"❌ Заявка <b>№{pending_ticket_id}</b> отклонена.\n<b>Причина:</b> {reason}", parse_mode="HTML", reply_markup=main_keyboard(ticket_info[0]))
            except Exception:
                pass
            orig = message.reply_to_message
            text_upd = (orig.caption or orig.text or "") + f"\n\n❌ <b>Отклонено:</b> {reason}"
            if orig.photo:
                await orig.edit_caption(caption=text_upd, parse_mode="HTML")
            else:
                await orig.edit_text(text=text_upd, parse_mode="HTML")
        delete_pending_rejection(replied_msg_id)
        await message.answer("✅ Отказ отправлен.")
        return

    mapping = get_user_by_group_msg(replied_msg_id)
    if mapping:
        user_id, ticket_id = mapping[0], mapping[1]
        ticket_info = get_ticket_info(ticket_id)

        if ticket_info and ticket_info[2] == 'active':
            if ticket_info[1] != message.from_user.id and not is_main_admin(message.from_user.id):
                await message.answer("❌ Этот тикет ведет другой администратор. Вы не можете в него отвечать!")
                return

            admin_name = html.escape(message.from_user.full_name)
            client_text = f"👨‍💻 <b>Ответ поддержки ({admin_name}):</b>\n\n{html.escape(message.text or message.caption or '')}"
            try:
                if message.photo:
                    await bot.send_photo(user_id, photo=message.photo[-1].file_id, caption=client_text, parse_mode="HTML")
                else:
                    await bot.send_message(user_id, client_text, parse_mode="HTML")
                await message.react([{"type": "emoji", "emoji": "👍"}])
            except Exception as e:
                await message.answer(f"❌ Ошибка отправки: {e}")

@router.message(Command("ban"), F.chat.id == ADMIN_CHAT_ID)
async def cmd_ban_reply(message: Message):
    if not await is_support_member(message.from_user.id) and not is_main_admin(message.from_user.id):
        return
    
    user_id = None
    ticket_id = None

    if message.reply_to_message:
        replied_msg = message.reply_to_message
        mapping = get_user_by_group_msg(replied_msg.message_id)
        if mapping:
            user_id, ticket_id = mapping[0], mapping[1]
        
        if not user_id and (replied_msg.text or replied_msg.caption):
            full_text = replied_msg.text or replied_msg.caption
            match = re.search(r"ID:\s*<code>(\d+)</code>", full_text)
            if match:
                user_id = int(match.group(1))
                active = get_active_ticket(user_id)
                if active:
                    ticket_id = active[0]

    # Если ID не найден через реплай, проверяем аргумент команды (например, /ban 123456789 причина)
    if not user_id:
        args = message.text.split(maxsplit=2)
        if len(args) > 1 and args[1].isdigit():
            user_id = int(args[1])
            active = get_active_ticket(user_id)
            if active:
                ticket_id = active[0]

    if not user_id:
        await message.answer("❌ Не удалось определить пользователя. Ответьте командой `/ban [причина]` на сообщение пользователя или карточку тикета, либо используйте `/ban ID причина`.", parse_mode="HTML")
        return

    args = message.text.split(maxsplit=2)
    # Определяем индекс для причины в зависимости от способа вызова
    reason_idx = 2 if (message.reply_to_message and not message.text.split(maxsplit=1)[1].startswith(str(user_id))) else 2
    
    # Более надежный срез текста для причины
    parts = message.text.split(maxsplit=1)
    reason = "Нарушение правил"
    if len(parts) > 1:
        # Если передан ID первым словом
        sub_parts = parts[1].split(maxsplit=1)
        if sub_parts[0].isdigit() and not message.reply_to_message:
            reason = sub_parts[1] if len(sub_parts) > 1 else "Нарушение правил"
        else:
            reason = parts[1]

    ban_user_db(user_id, reason)
    
    if ticket_id:
        close_ticket_db(ticket_id, 'rejected')

    try:
        await bot.send_message(user_id, f"❌ Ваш доступ к поддержке заблокирован.\n<b>Причина:</b> {html.escape(reason)}", parse_mode="HTML")
    except Exception:
        pass

    await message.answer(f"✅ Пользователь с ID <code>{user_id}</code> успешно заблокирован, тикет закрыт.", parse_mode="HTML")

@router.message(Command("unban"), F.chat.id == ADMIN_CHAT_ID)
async def cmd_unban(message: Message):
    if not await is_support_member(message.from_user.id) and not is_main_admin(message.from_user.id): 
        return
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Использование: <code>/unban [user_id]</code>", parse_mode="HTML")
        return
    try:
        user_id = int(args[1])
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM banned_users WHERE user_id = %s", (user_id,))
        conn.commit()
        cursor.close()
        conn.close()
        await message.answer(f"✅ Пользователь <code>{user_id}</code> разблокирован.", parse_mode="HTML")
    except ValueError:
        await message.answer("❌ Неверный формат ID.")

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

async def main():
    await web_server()
    print("Бот успешно запущен и готов к работе!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
    
