import asyncio
import logging
import os
import html
import re
import time
from datetime import datetime, timezone, timedelta
from contextlib import contextmanager
from typing import Any, Callable, Dict, Awaitable

import psycopg2
from psycopg2 import pool
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router, BaseMiddleware
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter, TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton, ForceReply, TelegramObject
)

# ----------------------------------------------------------------------
# НАСТРОЙКИ И ОКРУЖЕНИЕ
# ----------------------------------------------------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_CHAT_ID = -1003945292994  # ID группы поддержки
OWNER_ID = int(os.getenv("ADMIN_ID", "0"))  # Главный создатель бота
MSK_TZ = timezone(timedelta(hours=3))

if not BOT_TOKEN:
    raise ValueError("ОШИБКА: Токен бота не найден! Укажите BOT_TOKEN в Environment Variables.")
if not DATABASE_URL:
    raise ValueError("ОШИБКА: Строка подключения к БД не найдена! Укажите DATABASE_URL.")

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()

# Кастомные TGP цифры
NUM_1 = "<tg-emoji emoji-id='5285184495857715032'>🔥</tg-emoji>"
NUM_2 = "<tg-emoji emoji-id='5287236502382724614'>🔥</tg-emoji>"
NUM_3 = "<tg-emoji emoji-id='5287361241117911628'>🔥</tg-emoji>"
NUM_4 = "<tg-emoji emoji-id='5287402803516427365'>🔥</tg-emoji>"
NUM_5 = "<tg-emoji emoji-id='5285444878250033485'>🔥</tg-emoji>"
NUM_6 = "<tg-emoji emoji-id='5285078586259161715'>🔥</tg-emoji>"
NUM_7 = "<tg-emoji emoji-id='5287334204298773700'>🔥</tg-emoji>"
NUM_8 = "<tg-emoji emoji-id='5287337962395158575'>🔥</tg-emoji>"
NUM_9 = "<tg-emoji emoji-id='5287610387875775295'>🔥</tg-emoji>"

def get_user_mention(user):
    safe_name = html.escape(user.full_name)
    return f'<a href="tg://user?id={user.id}">{safe_name}</a>'

def is_night_time() -> bool:
    """Проверяет, попадает ли текущее время в диапазон 22:00 - 10:00 по МСК"""
    now_msk = datetime.now(MSK_TZ)
    hour = now_msk.hour
    return hour >= 22 or hour < 10

def get_night_notice() -> str:
    if is_night_time():
        return "\n\n<tg-emoji emoji-id='5348110675891731556'>🔥</tg-emoji> <i>Обратите внимание: сейчас ночное время (с 22:00 до 10:00 МСК). Ваша заявка принята, но ответ модераторов может поступить утром!</i>"
    return ""

# ----------------------------------------------------------------------
# АНТИФЛУД МИДЛВАРЬ (THROTTLING MIDDLEWARE)
# ----------------------------------------------------------------------
class ThrottlingMiddleware(BaseMiddleware):
    def __init__(self, limit: float = 1.0):
        self.limit = limit
        self.users: Dict[int, float] = {}

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        user = data.get("event_from_user")
        if user:
            user_id = user.id
            current_time = time.time()
            last_time = self.users.get(user_id, 0.0)

            if current_time - last_time < self.limit:
                if isinstance(event, CallbackQuery):
                    try:
                        # Всплывающее системное окно не поддерживает HTML-теги
                        await event.answer("⚠️ Не спамьте кнопками! Подождите секунду.", show_alert=False)
                    except Exception:
                        pass
                return

            self.users[user_id] = current_time

        return await handler(event, data)

dp.message.middleware(ThrottlingMiddleware(limit=0.8))
dp.callback_query.middleware(ThrottlingMiddleware(limit=0.8))
dp.include_router(router)

# ----------------------------------------------------------------------
# БАЗА ДАННЫХ (Supabase / PostgreSQL с пулом соединений)
# ----------------------------------------------------------------------
db_pool = psycopg2.pool.ThreadedConnectionPool(
    minconn=1,
    maxconn=10,
    dsn=DATABASE_URL,
    sslmode="require"
)

@contextmanager
def get_db():
    conn = db_pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        db_pool.putconn(conn)

def _init_db_sync():
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY
                );
                CREATE TABLE IF NOT EXISTS tickets (
                    ticket_id SERIAL PRIMARY KEY,
                    user_id BIGINT,
                    admin_id BIGINT DEFAULT NULL,
                    status TEXT DEFAULT 'pending',
                    category TEXT DEFAULT 'general',
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                    reminded_unassigned BOOLEAN DEFAULT FALSE,
                    reminded_idle BOOLEAN DEFAULT FALSE
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
                    ticket_id INTEGER,
                    card_message_id BIGINT
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
                CREATE TABLE IF NOT EXISTS admin_agents (
                    admin_id BIGINT PRIMARY KEY,
                    agent_number INTEGER UNIQUE
                );
            """)
            cursor.execute("""
                ALTER TABLE tickets ADD COLUMN IF NOT EXISTS category TEXT DEFAULT 'general';
                ALTER TABLE tickets ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP;
                ALTER TABLE tickets ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP;
                ALTER TABLE tickets ADD COLUMN IF NOT EXISTS reminded_unassigned BOOLEAN DEFAULT FALSE;
                ALTER TABLE tickets ADD COLUMN IF NOT EXISTS reminded_idle BOOLEAN DEFAULT FALSE;
                ALTER TABLE pending_rejections ADD COLUMN IF NOT EXISTS card_message_id BIGINT;
            """)
            if OWNER_ID:
                cursor.execute(
                    "INSERT INTO main_admins (admin_id) VALUES (%s) ON CONFLICT (admin_id) DO NOTHING",
                    (OWNER_ID,)
                )
                cursor.execute(
                    "INSERT INTO admin_agents (admin_id, agent_number) VALUES (%s, 1) ON CONFLICT (admin_id) DO NOTHING",
                    (OWNER_ID,)
                )
    logging.info("База данных успешно инициализирована.")

_init_db_sync()

# Асинхронные обёртки для запросов к БД
async def is_main_admin(user_id: int) -> bool:
    if OWNER_ID and user_id == OWNER_ID:
        return True
    def _query():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT admin_id FROM main_admins WHERE admin_id = %s", (user_id,))
                return cur.fetchone() is not None
    try:
        return await asyncio.to_thread(_query)
    except Exception:
        return False

async def is_support_member(user_id: int) -> bool:
    if await is_main_admin(user_id):
        return True
    try:
        member = await bot.get_chat_member(chat_id=ADMIN_CHAT_ID, user_id=user_id)
        if member.status in ["creator", "administrator", "member"]:
            return True
    except Exception:
        pass
    return False

async def get_or_create_agent_number(admin_id: int) -> int:
    if OWNER_ID and admin_id == OWNER_ID:
        return 1

    def _query():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT agent_number FROM admin_agents WHERE admin_id = %s", (admin_id,))
                row = cur.fetchone()
                if row:
                    return row[0]
                
                cur.execute("SELECT COALESCE(MAX(agent_number), 1) + 1 FROM admin_agents")
                next_number = cur.fetchone()[0]
                cur.execute(
                    "INSERT INTO admin_agents (admin_id, agent_number) VALUES (%s, %s) ON CONFLICT (admin_id) DO NOTHING RETURNING agent_number",
                    (admin_id, next_number)
                )
                res = cur.fetchone()
                return res[0] if res else next_number

    return await asyncio.to_thread(_query)

async def get_setting(key: str, default: str = "false") -> str:
    def _query():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT value FROM settings WHERE key = %s", (key,))
                row = cur.fetchone()
                return row[0] if row else default
    try:
        return await asyncio.to_thread(_query)
    except Exception:
        return default

async def set_setting(key: str, value: str):
    def _query():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO settings (key, value) VALUES (%s, %s) "
                    "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                    (key, value)
                )
    await asyncio.to_thread(_query)

async def register_user(user_id: int):
    def _query():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO users (user_id) VALUES (%s) ON CONFLICT (user_id) DO NOTHING", (user_id,))
    try:
        await asyncio.to_thread(_query)
    except Exception as e:
        logging.error(f"Ошибка регистрации пользователя: {e}")

async def get_all_users_count() -> int:
    def _query():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM users")
                return cur.fetchone()[0]
    try:
        return await asyncio.to_thread(_query)
    except Exception:
        return 0

async def get_tickets_stats():
    def _query():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM tickets")
                total = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM tickets WHERE status = 'closed'")
                closed = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM tickets WHERE status = 'rejected'")
                rejected = cur.fetchone()[0]
                cur.execute("SELECT COALESCE(AVG(score), 0) FROM ratings WHERE user_id != %s", (OWNER_ID,))
                avg_rating = cur.fetchone()[0]
                return total, closed, rejected, round(float(avg_rating), 2)
    try:
        return await asyncio.to_thread(_query)
    except Exception:
        return 0, 0, 0, 0.0

async def get_admin_list_stats():
    def _query():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT t.admin_id, COUNT(*) as closed_count, COALESCE(AVG(r.score), 0) as avg_score, a.agent_number
                    FROM tickets t
                    LEFT JOIN ratings r ON t.ticket_id = r.ticket_id
                    LEFT JOIN admin_agents a ON t.admin_id = a.admin_id
                    WHERE t.admin_id IS NOT NULL AND t.status = 'closed'
                    GROUP BY t.admin_id, a.agent_number
                """)
                return cur.fetchall()
    try:
        return await asyncio.to_thread(_query)
    except Exception:
        return []

async def create_ticket(user_id: int, category: str = 'general') -> int:
    def _query():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO tickets (user_id, status, category, created_at, updated_at) "
                    "VALUES (%s, 'pending', %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP) RETURNING ticket_id",
                    (user_id, category)
                )
                return cur.fetchone()[0]
    return await asyncio.to_thread(_query)

async def touch_ticket(ticket_id: int):
    def _query():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE tickets SET updated_at = CURRENT_TIMESTAMP, reminded_idle = FALSE WHERE ticket_id = %s",
                    (ticket_id,)
                )
    await asyncio.to_thread(_query)

async def map_message(group_msg_id: int, user_id: int, ticket_id: int):
    def _query():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO message_map (group_message_id, user_id, ticket_id) VALUES (%s, %s, %s) "
                    "ON CONFLICT (group_message_id) DO UPDATE SET user_id = EXCLUDED.user_id, ticket_id = EXCLUDED.ticket_id",
                    (group_msg_id, user_id, ticket_id)
                )
    await asyncio.to_thread(_query)

async def get_user_by_group_msg(group_msg_id: int):
    def _query():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT user_id, ticket_id FROM message_map WHERE group_message_id = %s", (group_msg_id,))
                return cur.fetchone()
    return await asyncio.to_thread(_query)

async def get_ticket_messages(ticket_id: int) -> list:
    def _query():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT group_message_id FROM message_map WHERE ticket_id = %s", (ticket_id,))
                return [row[0] for row in cur.fetchall()]
    return await asyncio.to_thread(_query)

async def get_ticket_info(ticket_id: int):
    def _query():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT user_id, admin_id, status FROM tickets WHERE ticket_id = %s", (ticket_id,))
                return cur.fetchone()
    return await asyncio.to_thread(_query)

async def get_active_ticket(user_id: int):
    def _query():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT ticket_id, admin_id, status FROM tickets "
                    "WHERE user_id = %s AND status IN ('pending', 'active') "
                    "ORDER BY ticket_id DESC LIMIT 1",
                    (user_id,)
                )
                return cur.fetchone()
    return await asyncio.to_thread(_query)

async def activate_ticket(ticket_id: int, admin_id: int):
    def _query():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE tickets SET status = 'active', admin_id = %s, updated_at = CURRENT_TIMESTAMP, reminded_idle = FALSE WHERE ticket_id = %s",
                    (admin_id, ticket_id)
                )
    await asyncio.to_thread(_query)

async def close_ticket_db(ticket_id: int, status: str = 'closed'):
    def _query():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE tickets SET status = %s, updated_at = CURRENT_TIMESTAMP WHERE ticket_id = %s", (status, ticket_id))
    await asyncio.to_thread(_query)

async def is_banned(user_id: int):
    def _query():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT reason FROM banned_users WHERE user_id = %s", (user_id,))
                return cur.fetchone()
    try:
        return await asyncio.to_thread(_query)
    except Exception:
        return None

async def ban_user_db(user_id: int, reason: str):
    def _query():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO banned_users (user_id, reason) VALUES (%s, %s) "
                    "ON CONFLICT (user_id) DO UPDATE SET reason = EXCLUDED.reason",
                    (user_id, reason)
                )
    await asyncio.to_thread(_query)

async def unban_user_db(user_id: int):
    def _query():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM banned_users WHERE user_id = %s", (user_id,))
    await asyncio.to_thread(_query)

async def save_rating_db(ticket_id: int, user_id: int, admin_id: int, score: int, review: str = None):
    def _query():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO ratings (ticket_id, user_id, admin_id, score, review) VALUES (%s, %s, %s, %s, %s) "
                    "ON CONFLICT (ticket_id) DO UPDATE SET score = EXCLUDED.score, review = EXCLUDED.review",
                    (ticket_id, user_id, admin_id, score, review)
                )
    await asyncio.to_thread(_query)

async def add_pending_rejection(prompt_message_id: int, ticket_id: int, card_message_id: int):
    def _query():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO pending_rejections (prompt_message_id, ticket_id, card_message_id) VALUES (%s, %s, %s) "
                    "ON CONFLICT (prompt_message_id) DO UPDATE SET ticket_id = EXCLUDED.ticket_id, card_message_id = EXCLUDED.card_message_id",
                    (prompt_message_id, ticket_id, card_message_id)
                )
    await asyncio.to_thread(_query)

async def get_pending_rejection(prompt_message_id: int):
    def _query():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT ticket_id, card_message_id FROM pending_rejections WHERE prompt_message_id = %s",
                    (prompt_message_id,)
                )
                return cur.fetchone()
    return await asyncio.to_thread(_query)

async def delete_pending_rejection(prompt_message_id: int):
    def _query():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM pending_rejections WHERE prompt_message_id = %s", (prompt_message_id,))
    await asyncio.to_thread(_query)

# ----------------------------------------------------------------------
# АВТООЧИСТКА ЗАКРЫТЫХ ТИКЕТОВ ЧЕРЕЗ 1 ЧАС
# ----------------------------------------------------------------------
async def schedule_ticket_cleanup(ticket_id: int, card_message_id: int = None):
    await asyncio.sleep(3600)
    try:
        msg_ids = await get_ticket_messages(ticket_id)
        if card_message_id and card_message_id not in msg_ids:
            msg_ids.append(card_message_id)

        for m_id in msg_ids:
            try:
                await bot.delete_message(chat_id=ADMIN_CHAT_ID, message_id=m_id)
            except TelegramBadRequest:
                pass
            except Exception:
                pass
            await asyncio.sleep(0.05)
        logging.info(f"Тикет №{ticket_id} успешно очищен из чата поддержки через 1 час.")
    except Exception as e:
        logging.error(f"Ошибка при очистке тикета №{ticket_id}: {e}")

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
    question_content = State()
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

async def main_keyboard(user_id: int):
    keyboard = [
        [KeyboardButton(text=BTN_COMPLAINT), KeyboardButton(text=BTN_APPEAL)],
        [KeyboardButton(text=BTN_FRIENDS), KeyboardButton(text=BTN_QUESTION)],
        [KeyboardButton(text=BTN_HELPER_APPLY), KeyboardButton(text=BTN_SECRET_APPLY)],
        [KeyboardButton(text=BTN_REFRESH)]
    ]
    if await is_main_admin(user_id):
        keyboard.append([KeyboardButton(text=BTN_ADMIN_PANEL)])

    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True, persistent=True)

async def admin_panel_kb():
    f_active = await get_setting("friend_active", "false")
    h_active = await get_setting("helper_recruitment", "true")
    s_active = await get_setting("secret_recruitment", "true")
    
    friend_status = "🟢 Вкл" if f_active == "true" else "🔴 Выкл"
    helper_status = "🟢 Вкл" if h_active == "true" else "🔴 Выкл"
    secret_status = "🟢 Вкл" if s_active == "true" else "🔴 Выкл"
    
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Общая статистика", callback_data="adm_stats"),
         InlineKeyboardButton(text="👥 Статистика по админам", callback_data="adm_list_stats")],
        [InlineKeyboardButton(text=f"👯‍♀️ Добавление в друзья: {friend_status}", callback_data="toggle_friend")],
        [InlineKeyboardButton(text=f"📝 Набор в хелперы: {helper_status}", callback_data="toggle_helper")],
        [InlineKeyboardButton(text=f"🔍 Искатели секреток: {secret_status}", callback_data="toggle_secret")],
        [InlineKeyboardButton(text="👑 Управление главными админами", callback_data="manage_main_admins")],
        [InlineKeyboardButton(text="✏️ Изменить ник для друзей", callback_data="change_friend_nick")]
    ])

def take_ticket_kb(ticket_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🟢 Начать работу", callback_data=f"take_{ticket_id}"),
        InlineKeyboardButton(text="🔴 Отклонить тикет", callback_data=f"reject_{ticket_id}")
    ]])

def close_ticket_kb(ticket_id: int, admin_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔒 Закрыть тикет", callback_data=f"close_{ticket_id}_{admin_id}")
    ]])

def user_cancel_kb(ticket_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="❌ Закрыть заявку", callback_data=f"user_cancel_{ticket_id}")
    ]])

def rating_kb(ticket_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⭐ 1", callback_data=f"rate_{ticket_id}_1"),
        InlineKeyboardButton(text="⭐ 2", callback_data=f"rate_{ticket_id}_2"),
        InlineKeyboardButton(text="⭐ 3", callback_data=f"rate_{ticket_id}_3"),
        InlineKeyboardButton(text="⭐ 4", callback_data=f"rate_{ticket_id}_4"),
        InlineKeyboardButton(text="⭐ 5", callback_data=f"rate_{ticket_id}_5"),
    ]])

def helper_decision_kb(user_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🟢 Принять", callback_data=f"helper_accept_{user_id}"),
        InlineKeyboardButton(text="🔴 Отклонить", callback_data=f"helper_reject_{user_id}")
    ]])

def secret_decision_kb(user_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🟢 Принять", callback_data=f"secret_accept_{user_id}"),
        InlineKeyboardButton(text="🔴 Отклонить", callback_data=f"secret_reject_{user_id}")
    ]])

# ----------------------------------------------------------------------
# СТАРТ И МЕНЮ
# ----------------------------------------------------------------------
WELCOME_PHOTO_ID = "AgACAgEAAxkBAAEvN2dquXGC1nw3HqPjB8OP9hp-L17WGwACrwxrG6FryEX933FL88L6GwEAAwIAA3kAAz0E"

@router.message(CommandStart(), F.chat.type == "private")
async def start_cmd(message: Message, state: FSMContext):
    await register_user(message.from_user.id)
    banned = await is_banned(message.from_user.id)
    if banned:
        await message.answer(
            f"<tg-emoji emoji-id='5348362704572664028'>🔥</tg-emoji> Вы заблокированы в поддержке.\n<b>Причина:</b> {html.escape(banned[0])}",
            parse_mode="HTML"
        )
        return

    await state.clear()
    kb = await main_keyboard(message.from_user.id)

    welcome_caption = (
        '<tg-emoji emoji-id="5206693654990717395">🔥</tg-emoji>'
        '<b>Добро пожаловать в поддержку Tower Of Hell Secrets (@ToHSecretss)!</b>'
        '<tg-emoji emoji-id="5206693654990717395">🔥</tg-emoji>\n\n'
        '<tg-emoji emoji-id="5208629654449064934">🔥</tg-emoji>'
        '<i>Выберите нужный раздел на клавиатуре ниже</i>'
    )

    await message.answer_photo(
        photo=WELCOME_PHOTO_ID,
        caption=welcome_caption,
        reply_markup=kb,
        parse_mode="HTML"
    )

@router.message(F.text == BTN_REFRESH, F.chat.type == "private")
async def refresh_menu_handler(message: Message, state: FSMContext):
    await register_user(message.from_user.id)
    if await is_banned(message.from_user.id): return
    await state.clear()
    kb = await main_keyboard(message.from_user.id)
    await message.answer("<tg-emoji emoji-id='5346269127059196142'>🔥</tg-emoji> <b>Меню обновлено!</b>", reply_markup=kb, parse_mode="HTML")

@router.message(F.text == BTN_ADMIN_PANEL, F.chat.type == "private")
async def open_admin_panel(message: Message, state: FSMContext):
    if not await is_main_admin(message.from_user.id):
        return
    await state.clear()
    kb = await admin_panel_kb()
    await message.answer("<tg-emoji emoji-id='5348292765325212780'>🔥</tg-emoji> <b>Панель администратора</b>", reply_markup=kb, parse_mode="HTML")

@router.callback_query(F.data == "adm_stats")
async def callback_stats(call: CallbackQuery):
    if not await is_main_admin(call.from_user.id):
        await call.answer("❌ Нет доступа", show_alert=True)
        return
    users_count = await get_all_users_count()
    total, closed, rejected, avg_rating = await get_tickets_stats()

    stats_text = (
        "<tg-emoji emoji-id='5346267671065281783'>🔥</tg-emoji> <b>Общая статистика поддержки:</b>\n\n"
        f"<tg-emoji emoji-id='5346136537123801643'>🔥</tg-emoji> Активных пользователей: <code>{users_count}</code>\n"
        f"<tg-emoji emoji-id='5348348681504441752'>🔥</tg-emoji> Всего тикетов: <code>{total}</code>\n"
        f"<tg-emoji emoji-id='5346300789558101141'>🔥</tg-emoji> Успешно закрыто: <code>{closed}</code>\n"
        f"<tg-emoji emoji-id='5348362704572664028'>🔥</tg-emoji> Отклонено: <code>{rejected}</code>\n"
        f"<tg-emoji emoji-id='5348446245981536690'>🔥</tg-emoji> Общая оценка саппорта: <code>{avg_rating} / 5.0</code>"
    )
    kb = await admin_panel_kb()
    await call.message.edit_text(stats_text, reply_markup=kb, parse_mode="HTML")
    await call.answer()

@router.callback_query(F.data == "adm_list_stats")
async def callback_admin_list_stats(call: CallbackQuery):
    if not await is_main_admin(call.from_user.id):
        await call.answer("❌ Нет доступа", show_alert=True)
        return
    
    rows = await get_admin_list_stats()
    if not rows:
        text = "<tg-emoji emoji-id='5346136537123801643'>🔥</tg-emoji> <b>Статистика по администраторам:</b>\n\nПока нет закрытых тикетов у админов."
    else:
        text = "<tg-emoji emoji-id='5346136537123801643'>🔥</tg-emoji> <b>Статистика по администраторам:</b>\n\n"
        for admin_id, closed_cnt, avg_score, agent_no in rows:
            try:
                chat_member = await bot.get_chat(admin_id)
                name = chat_member.full_name
            except Exception:
                name = f"ID: {admin_id}"
            agent_str = f" [Агент #{agent_no}]" if agent_no else ""
            text += f"<tg-emoji emoji-id='5346136537123801643'>🔥</tg-emoji> <b>{html.escape(name)}</b>{agent_str} (<code>{admin_id}</code>)\n"
            text += f"   • Закрыто тикетов: <code>{closed_cnt}</code>\n"
            text += f"   • Средняя оценка: <code>{round(float(avg_score), 2)} / 5.0</code>\n\n"

    back_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_adm")]])
    await call.message.edit_text(text, reply_markup=back_kb, parse_mode="HTML")
    await call.answer()

@router.callback_query(F.data == "back_to_adm")
async def back_to_admin_panel(call: CallbackQuery):
    if not await is_main_admin(call.from_user.id): return
    kb = await admin_panel_kb()
    await call.message.edit_text("<tg-emoji emoji-id='5348292765325212780'>🔥</tg-emoji> <b>Панель администратора</b>", reply_markup=kb, parse_mode="HTML")
    await call.answer()

@router.callback_query(F.data == "manage_main_admins")
async def manage_main_admins_callback(call: CallbackQuery):
    if OWNER_ID and call.from_user.id != OWNER_ID:
        await call.answer("❌ Только создатель бота может управлять главными админами!", show_alert=True)
        return
    
    def _query():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT admin_id FROM main_admins")
                return cur.fetchall()
    try:
        res = await asyncio.to_thread(_query)
    except Exception:
        res = []

    text = "<tg-emoji emoji-id='5345880664447140673'>🔥</tg-emoji> <b>Главные администраторы бота:</b>\n\n"
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
    await call.message.answer("<tg-emoji emoji-id='5346004239246183111'>🔥</tg-emoji> Введите <b>Telegram ID</b> пользователя, которого хотите сделать главным администратором:", parse_mode="HTML")
    await call.answer()

@router.message(Form.add_main_admin_id, F.chat.type == "private")
async def process_add_main_admin(message: Message, state: FSMContext):
    if OWNER_ID and message.from_user.id != OWNER_ID:
        return
    try:
        new_id = int(message.text.strip())
        def _query():
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute("INSERT INTO main_admins (admin_id) VALUES (%s) ON CONFLICT (admin_id) DO NOTHING", (new_id,))
        await asyncio.to_thread(_query)
        await state.clear()
        kb = await main_keyboard(message.from_user.id)
        await message.answer(f"<tg-emoji emoji-id='5346300789558101141'>🔥</tg-emoji> Пользователь <code>{new_id}</code> успешно назначен главным администратором!", reply_markup=kb, parse_mode="HTML")
    except ValueError:
        await message.answer("<tg-emoji emoji-id='5348362704572664028'>🔥</tg-emoji> Неверный формат ID. Введите числовой Telegram ID:")

@router.callback_query(F.data == "del_main_admin_start")
async def del_main_admin_start(call: CallbackQuery, state: FSMContext):
    if OWNER_ID and call.from_user.id != OWNER_ID:
        await call.answer("❌ Доступно только создателю бота!", show_alert=True)
        return
    await state.set_state(Form.del_main_admin_id)
    await call.message.answer("<tg-emoji emoji-id='5346176879751612829'>🔥</tg-emoji> Введите <b>Telegram ID</b> главного администратора, которого хотите снять:", parse_mode="HTML")
    await call.answer()

@router.message(Form.del_main_admin_id, F.chat.type == "private")
async def process_del_main_admin(message: Message, state: FSMContext):
    if OWNER_ID and message.from_user.id != OWNER_ID:
        return
    try:
        del_id = int(message.text.strip())
        if OWNER_ID and del_id == OWNER_ID:
            await message.answer("<tg-emoji emoji-id='5348362704572664028'>🔥</tg-emoji> Нельзя удалить создателя бота!")
            return
        def _query():
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM main_admins WHERE admin_id = %s", (del_id,))
        await asyncio.to_thread(_query)
        await state.clear()
        kb = await main_keyboard(message.from_user.id)
        await message.answer(f"<tg-emoji emoji-id='5346300789558101141'>🔥</tg-emoji> Пользователь <code>{del_id}</code> снят с поста главного администратора.", reply_markup=kb, parse_mode="HTML")
    except ValueError:
        await message.answer("<tg-emoji emoji-id='5348362704572664028'>🔥</tg-emoji> Неверный формат ID. Введите числовой Telegram ID:")

@router.callback_query(F.data == "toggle_friend")
async def toggle_friend_callback(call: CallbackQuery):
    if not await is_main_admin(call.from_user.id): return
    current = await get_setting("friend_active", "false")
    new_val = "false" if current == "true" else "true"
    await set_setting("friend_active", new_val)
    await call.answer("Статус изменен")
    kb = await admin_panel_kb()
    await call.message.edit_reply_markup(reply_markup=kb)

@router.callback_query(F.data == "toggle_helper")
async def toggle_helper_callback(call: CallbackQuery):
    if not await is_main_admin(call.from_user.id): return
    current = await get_setting("helper_recruitment", "true")
    new_val = "false" if current == "true" else "true"
    await set_setting("helper_recruitment", new_val)
    await call.answer("Статус набора хелперов изменен")
    kb = await admin_panel_kb()
    await call.message.edit_reply_markup(reply_markup=kb)

@router.callback_query(F.data == "toggle_secret")
async def toggle_secret_callback(call: CallbackQuery):
    if not await is_main_admin(call.from_user.id): return
    current = await get_setting("secret_recruitment", "true")
    new_val = "false" if current == "true" else "true"
    await set_setting("secret_recruitment", new_val)
    await call.answer("Статус набора в искатели изменен")
    kb = await admin_panel_kb()
    await call.message.edit_reply_markup(reply_markup=kb)

@router.callback_query(F.data == "change_friend_nick")
async def change_friend_nick_callback(call: CallbackQuery, state: FSMContext):
    if not await is_main_admin(call.from_user.id): return
    await state.set_state(Form.set_friend_nick)
    current_nick = await get_setting("friend_nickname", "Не задан")
    await call.message.answer(f"<tg-emoji emoji-id='5348224471050238603'>🔥</tg-emoji> Введите новый ник для друзей. Текущий: <code>{html.escape(current_nick)}</code>", parse_mode="HTML")
    await call.answer()

@router.message(Form.set_friend_nick)
async def save_friend_nick(message: Message, state: FSMContext):
    if not await is_main_admin(message.from_user.id): return
    await set_setting("friend_nickname", message.text.strip())
    await state.clear()
    kb = await main_keyboard(message.from_user.id)
    await message.answer("<tg-emoji emoji-id='5346300789558101141'>🔥</tg-emoji> Ник успешно изменен!", reply_markup=kb)

# ----------------------------------------------------------------------
# СИСТЕМА НАБОРА В ХЕЛПЕРЫ И ИСКАТЕЛИ СЕКРЕТОВ
# ----------------------------------------------------------------------
@router.message(F.text == BTN_HELPER_APPLY, F.chat.type == "private")
async def start_helper_apply(message: Message, state: FSMContext):
    await register_user(message.from_user.id)
    if await is_banned(message.from_user.id): return
    if await get_setting("helper_recruitment", "true") != "true":
        await message.answer("<tg-emoji emoji-id='5346273864408120989'>🔥</tg-emoji> Набор в команду поддержки в данный момент закрыт.")
        return
    await state.clear()
    await state.set_state(Form.helper_age)
    await message.answer(f"<tg-emoji emoji-id='5346192285799302524'>🔥</tg-emoji> <b>Заявка в команду поддержки (Хелперы)</b>\n\n{NUM_1} Укажите ваш возраст:", parse_mode="HTML")

@router.message(Form.helper_age)
async def process_helper_age(message: Message, state: FSMContext):
    await state.update_data(helper_age=message.text)
    await state.set_state(Form.helper_timezone)
    await message.answer(f"{NUM_2} Укажите ваш часовой пояс (например, МСК, +2 от МСК):", parse_mode="HTML")

@router.message(Form.helper_timezone)
async def process_helper_timezone(message: Message, state: FSMContext):
    await state.update_data(helper_timezone=message.text)
    await state.set_state(Form.helper_experience)
    await message.answer(f"{NUM_3} Был ли у вас опыт работы в поддержке или на аналогичных проектах? Опишите кратко:", parse_mode="HTML")

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
        f"👶 <b>Возраст:</b> {html.escape(data.get('helper_age', 'Не указан'))}\n"
        f"🌍 <b>Часовой пояс:</b> {html.escape(data.get('helper_timezone', 'Не указан'))}\n"
        f"💼 <b>Опыт:</b> {html.escape(message.text or '')}"
    )

    sent = await bot.send_message(ADMIN_CHAT_ID, admin_text, reply_markup=helper_decision_kb(user.id), parse_mode="HTML")
    await map_message(sent.message_id, user.id, 0)
    kb = await main_keyboard(user.id)
    night_txt = get_night_notice()
    await message.answer(f"<tg-emoji emoji-id='5346300789558101141'>🔥</tg-emoji> Ваша анкета успешно отправлена администрации! Ожидайте ответа.{night_txt}", reply_markup=kb, parse_mode="HTML")
    await state.clear()

@router.callback_query(F.data.startswith("helper_accept_"))
async def helper_accept_callback(call: CallbackQuery):
    if not await is_support_member(call.from_user.id):
        await call.answer("❌ У вас нет прав!", show_alert=True)
        return
    user_id = int(call.data.split("_")[2])
    try:
        await bot.send_message(user_id, "<tg-emoji emoji-id='5346038654819123712'>🔥</tg-emoji> <b>Поздравляем! Ваша заявка на хелпера принята!</b> Админы свяжутся с вами в ближайшее время.", parse_mode="HTML")
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
        await bot.send_message(user_id, "<tg-emoji emoji-id='5346038654819123712'>🔥</tg-emoji> К сожалению, ваша заявка на хелпера была отклонена.", parse_mode="HTML")
    except Exception:
        pass
    await call.message.edit_text(call.message.text + "\n\n🔴 <b>Статус:</b> Отклонено ❌", parse_mode="HTML")
    await call.answer("Заявка отклонена!")

@router.message(F.text == BTN_SECRET_APPLY, F.chat.type == "private")
async def start_secret_apply(message: Message, state: FSMContext):
    await register_user(message.from_user.id)
    if await is_banned(message.from_user.id): return
    if await get_setting("secret_recruitment", "true") != "true":
        await message.answer("<tg-emoji emoji-id='5346273864408120989'>🔥</tg-emoji> Набор в искатели секреток в данный момент закрыт.")
        return
    await state.clear()
    await state.set_state(Form.secret_age)
    await message.answer(f"<tg-emoji emoji-id='5345840270279724328'>🔥</tg-emoji> <b>Заявка в искатели секреток</b>\n\n{NUM_1} Укажите ваш возраст:", parse_mode="HTML")

@router.message(Form.secret_age)
async def process_secret_age(message: Message, state: FSMContext):
    await state.update_data(secret_age=message.text)
    await state.set_state(Form.secret_experience)
    await message.answer(f"{NUM_2} Есть ли у вас опыт поиска секретов/пасхалок? Расскажите о своих успехах:", parse_mode="HTML")

@router.message(Form.secret_experience)
async def process_secret_experience(message: Message, state: FSMContext):
    await state.update_data(secret_experience=message.text)
    await state.set_state(Form.secret_activity)
    await message.answer(f"{NUM_3} Сколько времени вы готовы уделять игре и проекту ежедневно?", parse_mode="HTML")

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
        f"👶 <b>Возраст:</b> {html.escape(data.get('secret_age', 'Не указан'))}\n"
        f"💼 <b>Опыт:</b> {html.escape(data.get('secret_experience', 'Не указан'))}\n"
        f"⏱ <b>Онлайн:</b> {html.escape(message.text or '')}"
    )

    sent = await bot.send_message(ADMIN_CHAT_ID, admin_text, reply_markup=secret_decision_kb(user.id), parse_mode="HTML")
    await map_message(sent.message_id, user.id, 0)
    kb = await main_keyboard(user.id)
    night_txt = get_night_notice()
    await message.answer(f"<tg-emoji emoji-id='5346300789558101141'>🔥</tg-emoji> Ваша заявка в искатели секреток успешно отправлена! Ожидайте ответа.{night_txt}", reply_markup=kb, parse_mode="HTML")
    await state.clear()

@router.callback_query(F.data.startswith("secret_accept_"))
async def secret_accept_callback(call: CallbackQuery):
    if not await is_support_member(call.from_user.id):
        await call.answer("❌ У вас нет прав!", show_alert=True)
        return
    user_id = int(call.data.split("_")[2])
    try:
        await bot.send_message(user_id, "<tg-emoji emoji-id='5346038654819123712'>🔥</tg-emoji> <b>Поздравляем! Ваша заявка в искатели секреток принята!</b>", parse_mode="HTML")
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
        await bot.send_message(user_id, "<tg-emoji emoji-id='5348362704572664028'>🔥</tg-emoji> К сожалению, ваша заявка в искатели секреток была отклонена.", parse_mode="HTML")
    except Exception:
        pass
    await call.message.edit_text(call.message.text + "\n\n🔴 <b>Статус:</b> Отклонено ❌", parse_mode="HTML")
    await call.answer("Заявка отклонена!")

# ----------------------------------------------------------------------
# СОЗДАНИЕ ТИКЕТОВ
# ----------------------------------------------------------------------
async def check_active_ticket(message: Message) -> bool:
    active = await get_active_ticket(message.from_user.id)
    if active:
        await message.answer(
            f"<tg-emoji emoji-id='5348110675891731556'>🔥</tg-emoji> У вас уже есть активный тикет <b>№{active[0]}</b>.\nДождитесь ответа или закройте его, прежде чем открывать новый.",
            parse_mode="HTML"
        )
        return True
    return False

@router.message(F.text == BTN_COMPLAINT, F.chat.type == "private")
async def start_complaint(message: Message, state: FSMContext):
    await register_user(message.from_user.id)
    if await is_banned(message.from_user.id) or await check_active_ticket(message): return
    await state.clear()
    await state.set_state(Form.complaint_reason)
    await message.answer(f"{NUM_1} <b>Суть нарушения:</b>\nОпишите подробно, что именно произошло.", parse_mode="HTML")

@router.message(F.text == BTN_APPEAL, F.chat.type == "private")
async def start_appeal(message: Message, state: FSMContext):
    await register_user(message.from_user.id)
    if await is_banned(message.from_user.id) or await check_active_ticket(message): return
    await state.clear()
    await state.set_state(Form.appeal_nickname)
    await message.answer(f"{NUM_1} <b>Ваш ник в игре:</b>", parse_mode="HTML")

@router.message(F.text == BTN_FRIENDS, F.chat.type == "private")
async def start_friends(message: Message, state: FSMContext):
    await register_user(message.from_user.id)
    if await is_banned(message.from_user.id) or await check_active_ticket(message): return
    if await get_setting("friend_active", "false") != "true":
        await message.answer("<tg-emoji emoji-id='5346273864408120989'>🔥</tg-emoji> Раздел добавления в друзья временно закрыт на технические работы.")
        return
    await state.clear()
    friend_nick = await get_setting("friend_nickname", "Администратор")
    await state.set_state(Form.friends_nickname)
    await message.answer(f"<tg-emoji emoji-id='5346136537123801643'>🔥</tg-emoji> Добавьтесь в друзья к игроку: <code>{html.escape(friend_nick)}</code>\n\n{NUM_1} Укажите ваш ник в игре:", parse_mode="HTML")

@router.message(F.text == BTN_QUESTION, F.chat.type == "private")
async def start_question(message: Message, state: FSMContext):
    await register_user(message.from_user.id)
    if await is_banned(message.from_user.id) or await check_active_ticket(message): return
    await state.clear()
    await state.set_state(Form.question_content)
    await message.answer("<tg-emoji emoji-id='5348435310994802643'>🔥</tg-emoji> Задайте ваш вопрос (можно отправить текст или фото с описанием):", parse_mode="HTML")

@router.message(Form.friends_nickname)
async def process_friends_nickname(message: Message, state: FSMContext):
    if await check_active_ticket(message): return
    ticket_id = await create_ticket(message.from_user.id, "friends")
    user_mention = get_user_mention(message.from_user)
    admin_text = f"👯‍♀️ <b>#Друзья | Заявка №{ticket_id}</b>\n👤 От: {user_mention} | ID: <code>{message.from_user.id}</code>\n\nНик: <code>{html.escape(message.text or '')}</code>"
    sent = await bot.send_message(ADMIN_CHAT_ID, admin_text, reply_markup=take_ticket_kb(ticket_id), parse_mode="HTML")
    await map_message(sent.message_id, message.from_user.id, ticket_id)
    night_txt = get_night_notice()
    await message.answer(f"<tg-emoji emoji-id='5346300789558101141'>🔥</tg-emoji> Заявка №{ticket_id} создана!{night_txt}", reply_markup=user_cancel_kb(ticket_id), parse_mode="HTML")
    await state.clear()

@router.message(Form.question_content, F.photo | F.text)
async def process_question(message: Message, state: FSMContext):
    if await check_active_ticket(message): return
    ticket_id = await create_ticket(message.from_user.id, "question")
    user_mention = get_user_mention(message.from_user)
    text_content = html.escape(message.text or message.caption or "Без описания")
    admin_text = f"❓ <b>#Вопрос | Заявка №{ticket_id}</b>\n👤 От: {user_mention} | ID: <code>{message.from_user.id}</code>\n\n{text_content}"

    if message.photo:
        sent = await bot.send_photo(
            ADMIN_CHAT_ID,
            photo=message.photo[-1].file_id,
            caption=admin_text,
            reply_markup=take_ticket_kb(ticket_id),
            parse_mode="HTML"
        )
    else:
        sent = await bot.send_message(
            ADMIN_CHAT_ID,
            admin_text,
            reply_markup=take_ticket_kb(ticket_id),
            parse_mode="HTML"
        )

    await map_message(sent.message_id, message.from_user.id, ticket_id)
    night_txt = get_night_notice()
    await message.answer(f"<tg-emoji emoji-id='5346300789558101141'>🔥</tg-emoji> Вопрос №{ticket_id} отправлен!{night_txt}", reply_markup=user_cancel_kb(ticket_id), parse_mode="HTML")
    await state.clear()

@router.message(Form.complaint_reason)
async def process_c_reason(message: Message, state: FSMContext):
    await state.update_data(c_reason=message.text)
    await state.set_state(Form.complaint_nickname)
    await message.answer(f"{NUM_2} Укажите ник нарушителя:", parse_mode="HTML")

@router.message(Form.complaint_nickname)
async def process_c_nickname(message: Message, state: FSMContext):
    await state.update_data(c_nickname=message.text)
    await state.set_state(Form.complaint_photo)
    await message.answer(f"{NUM_3} Отправьте скриншот-доказательство (плашка уровней должна быть видна):", parse_mode="HTML")

@router.message(Form.complaint_photo, F.photo)
async def process_c_photo(message: Message, state: FSMContext):
    if await check_active_ticket(message): return
    photo_id = message.photo[-1].file_id
    ticket_id = await create_ticket(message.from_user.id, "complaint")
    data = await state.get_data()
    user_mention = get_user_mention(message.from_user)
    admin_text = (
        f"🚨 <b>#Жалоба | Заявка №{ticket_id}</b>\n"
        f"👤 От: {user_mention} | ID: <code>{message.from_user.id}</code>\n"
        f"Суть: {html.escape(data.get('c_reason', ''))}\n"
        f"Нарушитель: <code>{html.escape(data.get('c_nickname', ''))}</code>"
    )
    sent = await bot.send_photo(ADMIN_CHAT_ID, photo=photo_id, caption=admin_text, reply_markup=take_ticket_kb(ticket_id), parse_mode="HTML")
    await map_message(sent.message_id, message.from_user.id, ticket_id)
    night_txt = get_night_notice()
    await message.answer(f"<tg-emoji emoji-id='5346300789558101141'>🔥</tg-emoji> Жалоба №{ticket_id} отправлена!{night_txt}", reply_markup=user_cancel_kb(ticket_id), parse_mode="HTML")
    await state.clear()

@router.message(Form.appeal_nickname)
async def process_a_nickname(message: Message, state: FSMContext):
    await state.update_data(a_nickname=message.text)
    await state.set_state(Form.appeal_reason)
    await message.answer(f"{NUM_2} Опишите, почему вы хотите разблокировку:", parse_mode="HTML")

@router.message(Form.appeal_reason)
async def process_a_reason(message: Message, state: FSMContext):
    if await check_active_ticket(message): return
    data = await state.get_data()
    ticket_id = await create_ticket(message.from_user.id, "appeal")
    user_mention = get_user_mention(message.from_user)
    admin_text = (
        f"😡 <b>#Обжалование | Заявка №{ticket_id}</b>\n"
        f"👤 От: {user_mention} | ID: <code>{message.from_user.id}</code>\n"
        f"Ник: <code>{html.escape(data.get('a_nickname', ''))}</code>\n"
        f"Причина: {html.escape(message.text or '')}"
    )
    sent = await bot.send_message(ADMIN_CHAT_ID, admin_text, reply_markup=take_ticket_kb(ticket_id), parse_mode="HTML")
    await map_message(sent.message_id, message.from_user.id, ticket_id)
    night_txt = get_night_notice()
    await message.answer(f"<tg-emoji emoji-id='5346300789558101141'>🔥</tg-emoji> Обжалование №{ticket_id} отправлено!{night_txt}", reply_markup=user_cancel_kb(ticket_id), parse_mode="HTML")
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
    ticket_info = await get_ticket_info(ticket_id)
    if ticket_info and ticket_info[2] != 'pending':
        await call.answer("❌ Заявка уже занята или обработана!", show_alert=True)
        return

    agent_number = await get_or_create_agent_number(call.from_user.id)
    await activate_ticket(ticket_id, call.from_user.id)

    if ticket_info:
        try:
            await bot.send_message(
                ticket_info[0],
                f"👨‍💻 <b>Агент #{agent_number}</b> взял вашу заявку <b>№{ticket_id}</b> в работу!\nТеперь вы можете писать сюда сообщения.",
                parse_mode="HTML"
            )
        except Exception:
            pass

    new_text = (call.message.caption or call.message.text or "") + f"\n\n🟢 <b>В работе у:</b> {call.from_user.mention_html()} (Агент #{agent_number})"
    if call.message.photo:
        await call.message.edit_caption(caption=new_text, reply_markup=close_ticket_kb(ticket_id, call.from_user.id), parse_mode="HTML")
    else:
        await call.message.edit_text(text=new_text, reply_markup=close_ticket_kb(ticket_id, call.from_user.id), parse_mode="HTML")
    await call.answer(f"Заявка №{ticket_id} принята (Вы Агент #{agent_number})!")

@router.callback_query(F.data.startswith("reject_"))
async def reject_ticket_handler(call: CallbackQuery):
    if not await is_support_member(call.from_user.id):
        await call.answer("❌ У вас нет прав хелпера!", show_alert=True)
        return

    ticket_id = int(call.data.split("_")[1])
    ticket_info = await get_ticket_info(ticket_id)
    if ticket_info and ticket_info[2] != 'pending':
        await call.answer("❌ Заявка уже обработана!", show_alert=True)
        return

    prompt_msg = await bot.send_message(
        ADMIN_CHAT_ID,
        f"❓ <b>Укажите причину отказа для заявки №{ticket_id}:</b>\n<i>(Ответьте Reply на это сообщение)</i>",
        reply_markup=ForceReply(selective=True),
        parse_mode="HTML"
    )
    await add_pending_rejection(prompt_msg.message_id, ticket_id, call.message.message_id)
    await map_message(prompt_msg.message_id, 0, ticket_id)
    await call.answer("Напишите причину отказа в ответ на сообщение бота!")

@router.callback_query(F.data.startswith("close_"))
async def close_ticket_handler(call: CallbackQuery):
    parts = call.data.split("_")
    ticket_id = int(parts[1])
    assigned_admin_id = int(parts[2])

    if call.from_user.id != assigned_admin_id and not await is_main_admin(call.from_user.id):
        if not await is_support_member(call.from_user.id):
            await call.answer("❌ Нет доступа!", show_alert=True)
            return
        await call.answer("❌ Закрыть тикет может только тот админ, который взял его в работу!", show_alert=True)
        return

    ticket_info = await get_ticket_info(ticket_id)
    if ticket_info:
        await close_ticket_db(ticket_id)
        try:
            await bot.send_message(
                ticket_info[0],
                f"🔒 Ваша заявка <b>№{ticket_id}</b> закрыта поддержкой.\nОцените качество обслуживания:",
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

    asyncio.create_task(schedule_ticket_cleanup(ticket_id, call.message.message_id))

@router.callback_query(F.data.startswith("user_cancel_"))
async def user_cancel_ticket(call: CallbackQuery):
    ticket_id = int(call.data.split("_")[2])
    ticket_info = await get_ticket_info(ticket_id)
    if ticket_info and ticket_info[2] == 'pending':
        await close_ticket_db(ticket_id, 'closed')
        await call.message.edit_text(f"❌ Заявка <b>№{ticket_id}</b> отменена вами.", parse_mode="HTML")
        await call.answer("Заявка отменена")
        asyncio.create_task(schedule_ticket_cleanup(ticket_id, None))
    else:
        await call.answer("❌ Заявка уже взята в работу или закрыта, отмена недоступна.", show_alert=True)

# ----------------------------------------------------------------------
# СИСТЕМА ОЦЕНОК
# ----------------------------------------------------------------------
@router.callback_query(F.data.startswith("rate_"))
async def process_rating(call: CallbackQuery):
    parts = call.data.split("_")
    ticket_id = int(parts[1])
    score = int(parts[2])
    user_id = call.from_user.id

    ticket_info = await get_ticket_info(ticket_id)
    admin_id = ticket_info[1] if ticket_info else 0

    await save_rating_db(ticket_id, user_id, admin_id, score)

    if await is_main_admin(user_id):
        await call.message.edit_text(f"⭐ [ТЕСТ АДМИНА] Оценка {score}/5 сохранена, но в общую статистику не пошла.", parse_mode="HTML")
    else:
        await call.message.edit_text(f"⭐ Спасибо за оценку ({score}/5)! Ваше мнение учтено.", parse_mode="HTML")
    
    await call.answer("Оценка сохранена!")

# ----------------------------------------------------------------------
# ДИАЛОГ В ЛС С ПОЛЬЗОВАТЕЛЕМ
# ----------------------------------------------------------------------
@router.message(F.chat.type == "private")
async def user_private_message(message: Message, state: FSMContext):
    await register_user(message.from_user.id)
    if await is_banned(message.from_user.id): return

    current_state = await state.get_state()
    if current_state is not None:
        return

    active_ticket = await get_active_ticket(message.from_user.id)
    if active_ticket and active_ticket[2] == 'active':
        await touch_ticket(active_ticket[0])
        user_mention = get_user_mention(message.from_user)
        text_to_group = f"📩 <b>Сообщение по заявке №{active_ticket[0]} от {user_mention} | ID: <code>{message.from_user.id}</code>:</b>\n\n{html.escape(message.text or message.caption or '')}"
        
        if message.photo:
            sent = await bot.send_photo(ADMIN_CHAT_ID, photo=message.photo[-1].file_id, caption=text_to_group, parse_mode="HTML")
        else:
            sent = await bot.send_message(ADMIN_CHAT_ID, text_to_group, parse_mode="HTML")
        await map_message(sent.message_id, message.from_user.id, active_ticket[0])
        return

    kb = await main_keyboard(message.from_user.id)
    await message.answer("⚠️ Пожалуйста, выберите нужный пункт меню для обращения.", reply_markup=kb)

# ----------------------------------------------------------------------
# КОМАНДЫ В АДМИН-ЧАТЕ (СТРОГО ВЫШЕ ПЕРЕСЫЛЬЩИКА СООБЩЕНИЙ!)
# ----------------------------------------------------------------------
@router.message(Command("news"), F.chat.id == ADMIN_CHAT_ID)
async def cmd_news_broadcast(message: Message):
    if not await is_main_admin(message.from_user.id):
        await message.answer("❌ Рассылку могут запускать только главные администраторы!")
        return

    target_msg = message.reply_to_message if message.reply_to_message else None
    broadcast_text = None

    if not target_msg:
        args = message.text.split(maxsplit=1)
        if len(args) < 2:
            await message.answer(
                f"❌ <b>Использование команды:</b>\n"
                f"{NUM_1} <code>/news Текст вашей новости</code>\n"
                f"{NUM_2} Или ответьте (Reply) командой <code>/news</code> на готовый пост/фото/видео.",
                parse_mode="HTML"
            )
            return
        broadcast_text = args[1]

    def _get_all_users():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT user_id FROM users")
                return [row[0] for row in cur.fetchall()]

    try:
        users = await asyncio.to_thread(_get_all_users)
    except Exception as e:
        await message.answer(f"❌ Ошибка получения пользователей из БД: {e}")
        return

    if not users:
        await message.answer("📭 В базе данных пока нет пользователей для рассылки.")
        return

    status_msg = await message.answer(f"📢 <b>Рассылка запущена...</b>\nВсего получателей: <code>{len(users)}</code>", parse_mode="HTML")

    success = 0
    blocked = 0
    errors = 0

    for user_id in users:
        try:
            if target_msg:
                await target_msg.copy_to(chat_id=user_id)
            else:
                await bot.send_message(chat_id=user_id, text=broadcast_text, parse_mode="HTML")
            
            success += 1
            await asyncio.sleep(0.05)
        except TelegramForbiddenError:
            blocked += 1
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
            try:
                if target_msg:
                    await target_msg.copy_to(chat_id=user_id)
                else:
                    await bot.send_message(chat_id=user_id, text=broadcast_text, parse_mode="HTML")
                success += 1
            except Exception:
                errors += 1
        except Exception:
            errors += 1

    await status_msg.edit_text(
        "📊 <b>Рассылка завершена!</b>\n\n"
        f"✅ Успешно доставлено: <code>{success}</code>\n"
        f"🚫 Заблокировали бота: <code>{blocked}</code>\n"
        f"⚠️ Ошибок отправки: <code>{errors}</code>\n"
        f"👥 Всего в базе: <code>{len(users)}</code>",
        parse_mode="HTML"
    )

@router.message(Command("ban"), F.chat.id == ADMIN_CHAT_ID)
async def cmd_ban_reply(message: Message):
    if not await is_support_member(message.from_user.id) and not await is_main_admin(message.from_user.id):
        return

    user_id = None
    ticket_id = None
    reason = "Нарушение правил"

    if message.reply_to_message:
        replied_msg = message.reply_to_message
        
        mapping = await get_user_by_group_msg(replied_msg.message_id)
        if mapping:
            user_id, ticket_id = mapping[0], mapping[1]

        raw_text = replied_msg.text or replied_msg.caption or ""
        
        if not user_id and raw_text:
            match_id = re.search(r"ID:?\s*<[^>]+>?(\d+)<[^>]+>?", raw_text, re.IGNORECASE)
            if not match_id:
                match_id = re.search(r"ID:?\s*(\d+)", raw_text, re.IGNORECASE)
            if match_id:
                user_id = int(match_id.group(1))

        if not user_id and raw_text:
            match_ticket = re.search(r"№(\d+)", raw_text)
            if match_ticket:
                t_id = int(match_ticket.group(1))
                t_info = await get_ticket_info(t_id)
                if t_info:
                    user_id, ticket_id = t_info[0], t_id

        args = message.text.split(maxsplit=1)
        if len(args) > 1:
            reason = args[1].strip()
    else:
        parts = message.text.split(maxsplit=2)
        if len(parts) > 1 and parts[1].isdigit():
            user_id = int(parts[1])
            if len(parts) > 2:
                reason = parts[2].strip()

    if not user_id:
        await message.answer(
            "❌ <b>Не удалось определить пользователя.</b>\n\n"
            "• Ответьте командой <code>/ban [причина]</code> на карточку тикета или сообщение игрока.\n"
            "• Либо введите напрямую: <code>/ban [ID] [причина]</code>",
            parse_mode="HTML"
        )
        return

    is_owner_target = (OWNER_ID and user_id == OWNER_ID)
    if is_owner_target and user_id != message.from_user.id:
        await message.answer("❌ Нельзя заблокировать создателя бота!")
        return

    if not ticket_id:
        active = await get_active_ticket(user_id)
        if active:
            ticket_id = active[0]

    await ban_user_db(user_id, reason)

    if ticket_id:
        await close_ticket_db(ticket_id, 'rejected')
        asyncio.create_task(schedule_ticket_cleanup(ticket_id, None))

    try:
        await bot.send_message(
            user_id,
            f"❌ <b>Ваш доступ к поддержке заблокирован.</b>\n<b>Причина:</b> {html.escape(reason)}",
            parse_mode="HTML"
        )
    except Exception:
        pass

    ticket_note = f" (тикет №{ticket_id} закрыт)" if ticket_id else ""
    test_note = " <i>[Тест создателя]</i>" if is_owner_target else ""
    await message.answer(
        f"✅ Пользователь с ID <code>{user_id}</code> успешно заблокирован{ticket_note}.{test_note}\n"
        f"<b>Причина:</b> {html.escape(reason)}",
        parse_mode="HTML"
    )

@router.message(Command("unban"), F.chat.id == ADMIN_CHAT_ID)
async def cmd_unban(message: Message):
    if not await is_support_member(message.from_user.id) and not await is_main_admin(message.from_user.id): 
        return

    user_id = None

    if message.reply_to_message:
        replied_msg = message.reply_to_message
        mapping = await get_user_by_group_msg(replied_msg.message_id)
        if mapping:
            user_id = mapping[0]

        raw_text = replied_msg.text or replied_msg.caption or ""
        if not user_id and raw_text:
            match_id = re.search(r"ID:?\s*<[^>]+>?(\d+)<[^>]+>?", raw_text, re.IGNORECASE)
            if not match_id:
                match_id = re.search(r"ID:?\s*(\d+)", raw_text, re.IGNORECASE)
            if match_id:
                user_id = int(match_id.group(1))

        if not user_id and raw_text:
            match_ticket = re.search(r"№(\d+)", raw_text)
            if match_ticket:
                t_id = int(match_ticket.group(1))
                t_info = await get_ticket_info(t_id)
                if t_info:
                    user_id = t_info[0]
    else:
        args = message.text.split(maxsplit=1)
        if len(args) > 1 and args[1].isdigit():
            user_id = int(args[1])

    if not user_id:
        await message.answer(
            "❌ <b>Не удалось определить пользователя.</b>\n\n"
            "• Ответьте командой <code>/unban</code> на карточку тикета или сообщение игрока.\n"
            "• Либо введите напрямую: <code>/unban [user_id]</code>",
            parse_mode="HTML"
        )
        return

    await unban_user_db(user_id)
    await message.answer(f"✅ Пользователь с ID <code>{user_id}</code> успешно разблокирован.", parse_mode="HTML")

# ----------------------------------------------------------------------
# ОТВЕТ ХЕЛПЕРА В ГРУППЕ (СТРОГО ПОСЛЕ СЛУЖЕБНЫХ КОМАНД!)
# ----------------------------------------------------------------------
@router.message(F.chat.id == ADMIN_CHAT_ID, F.reply_to_message)
async def admin_reply_in_group(message: Message):
    if message.text and message.text.startswith("/"):
        return

    replied_msg_id = message.reply_to_message.message_id
    pending_data = await get_pending_rejection(replied_msg_id)

    if pending_data:
        pending_ticket_id, card_message_id = pending_data
        ticket_info = await get_ticket_info(pending_ticket_id)
        if ticket_info:
            reason = html.escape(message.text or message.caption or "Без причины")
            await close_ticket_db(pending_ticket_id, 'rejected')
            try:
                user_kb = await main_keyboard(ticket_info[0])
                await bot.send_message(
                    ticket_info[0],
                    f"❌ Заявка <b>№{pending_ticket_id}</b> отклонена.\n<b>Причина:</b> {reason}",
                    parse_mode="HTML",
                    reply_markup=user_kb
                )
            except Exception:
                pass
            
            try:
                if card_message_id:
                    await bot.edit_message_reply_markup(chat_id=ADMIN_CHAT_ID, message_id=card_message_id, reply_markup=None)
            except Exception as e:
                logging.error(f"Не удалось обновить карточку тикета: {e}")

        await delete_pending_rejection(replied_msg_id)
        await message.answer(f"✅ Отказ по заявке №{pending_ticket_id} отправлен.")
        asyncio.create_task(schedule_ticket_cleanup(pending_ticket_id, card_message_id))
        return

    mapping = await get_user_by_group_msg(replied_msg_id)
    if mapping:
        user_id, ticket_id = mapping[0], mapping[1]
        ticket_info = await get_ticket_info(ticket_id)

        if ticket_info and ticket_info[2] == 'active':
            if ticket_info[1] != message.from_user.id and not await is_main_admin(message.from_user.id):
                await message.answer("❌ Этот тикет ведет другой администратор. Вы не можете в него отвечать!")
                return

            await touch_ticket(ticket_id)
            agent_no = await get_or_create_agent_number(message.from_user.id)
            
            client_text = f"👨‍💻 <b>Ответ поддержки (Агент #{agent_no}):</b>\n\n{html.escape(message.text or message.caption or '')}"
            try:
                if message.photo:
                    await bot.send_photo(user_id, photo=message.photo[-1].file_id, caption=client_text, parse_mode="HTML")
                else:
                    await bot.send_message(user_id, client_text, parse_mode="HTML")
                await message.react([{"type": "emoji", "emoji": "👍"}])
                await map_message(message.message_id, user_id, ticket_id)
            except Exception as e:
                await message.answer(f"❌ Ошибка отправки: {e}")

# ----------------------------------------------------------------------
# ФОНОВЫЙ ВОРКЕР НАПОМИНАНИЙ (ТАЙМАУТЫ НА ОБРАБОТКУ)
# ----------------------------------------------------------------------
async def reminder_worker():
    """Фоновая проверка тикетов на зависание и отсутствие ответа"""
    logging.info("Фоновый воркер напоминаний запущен.")
    while True:
        try:
            def _check():
                with get_db() as conn:
                    with conn.cursor() as cur:
                        cur.execute("""
                            SELECT ticket_id, category, user_id, 
                                   EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - created_at))/3600 as hours_passed
                            FROM tickets
                            WHERE status = 'pending' 
                              AND reminded_unassigned = FALSE
                              AND created_at <= CURRENT_TIMESTAMP - INTERVAL '2 hours'
                        """)
                        unassigned = cur.fetchall()

                        cur.execute("""
                            SELECT t.ticket_id, t.admin_id, t.user_id,
                                   EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - t.updated_at))/60 as mins_passed,
                                   a.agent_number
                            FROM tickets t
                            LEFT JOIN admin_agents a ON t.admin_id = a.admin_id
                            WHERE t.status = 'active' 
                              AND t.reminded_idle = FALSE
                              AND t.updated_at <= CURRENT_TIMESTAMP - INTERVAL '20 minutes'
                        """)
                        idle = cur.fetchall()

                        return unassigned, idle

            unassigned_tickets, idle_tickets = await asyncio.to_thread(_check)

            for t_id, cat, u_id, hrs in unassigned_tickets:
                hours_str = f"{int(hrs)} ч." if hrs else "2+ ч."
                alert_text = (
                    f"⏰ <b>Внимание! Необработанная заявка!</b>\n\n"
                    f"Заявка <b>№{t_id}</b> ({cat}) ожидает ответа уже более <b>{hours_str}</b>!\n"
                    f"👤 ID пользователя: <code>{u_id}</code>\n\n"
                    f"Пожалуйста, возьмите тикет в работу!"
                )
                try:
                    sent = await bot.send_message(ADMIN_CHAT_ID, alert_text, parse_mode="HTML")
                    await map_message(sent.message_id, u_id, t_id)
                    def _mark():
                        with get_db() as conn:
                            with conn.cursor() as cur:
                                cur.execute("UPDATE tickets SET reminded_unassigned = TRUE WHERE ticket_id = %s", (t_id,))
                    await asyncio.to_thread(_mark)
                except Exception as e:
                    logging.error(f"Ошибка отправки напоминания о заявке №{t_id}: {e}")

            for t_id, adm_id, u_id, mins, agent_no in idle_tickets:
                agent_str = f"Агент #{agent_no}" if agent_no else f"ID {adm_id}"
                admin_mention = agent_str
                try:
                    chat_member = await bot.get_chat(adm_id)
                    admin_mention = f'<a href="tg://user?id={adm_id}">{html.escape(chat_member.full_name)}</a> ({agent_str})'
                except Exception:
                    pass

                alert_text = (
                    f"⏳ <b>Напоминание по активному тикету!</b>\n\n"
                    f"Тикет <b>№{t_id}</b> находится в работе у {admin_mention}, но диалог простаивает уже более <b>{int(mins)} минут</b>.\n"
                    f"Не забудьте ответить пользователю или закрыть заявку!"
                )
                try:
                    sent = await bot.send_message(ADMIN_CHAT_ID, alert_text, parse_mode="HTML")
                    await map_message(sent.message_id, u_id, t_id)
                    def _mark_idle():
                        with get_db() as conn:
                            with conn.cursor() as cur:
                                cur.execute("UPDATE tickets SET reminded_idle = TRUE WHERE ticket_id = %s", (t_id,))
                    await asyncio.to_thread(_mark_idle)
                except Exception as e:
                    logging.error(f"Ошибка отправки напоминания о простое тикета №{t_id}: {e}")

        except Exception as err:
            logging.error(f"Ошибка в цикле reminder_worker: {err}")

        await asyncio.sleep(60)

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
    port = int(os.getenv("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logging.info(f"Health-check сервер запущен на порту {port}")

async def main():
    await web_server()
    asyncio.create_task(reminder_worker())
    logging.info("Бот успешно запущен и готов к работе!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Бот остановлен.")
    finally:
        if not db_pool.closed:
            db_pool.closeall()
