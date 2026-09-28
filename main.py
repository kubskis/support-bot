import asyncio
import logging
import os
import html
import re
import time
from datetime import datetime, timezone, timedelta
from contextlib import contextmanager
from typing import Any, Callable, Dict, List, Awaitable

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
    InlineKeyboardMarkup, InlineKeyboardButton, ForceReply, TelegramObject,
    LinkPreviewOptions, InputMediaPhoto
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

# Кастомные TGP иконки
ICON_CHECK = "<tg-emoji emoji-id='5346300789558101141'>🔥</tg-emoji>"
ICON_BALL = "<tg-emoji emoji-id='5348127061191967344'>🔥</tg-emoji>"
ICON_PHOTO = "<tg-emoji emoji-id='5348207836641902790'>🔥</tg-emoji>"
ICON_TIMER = "<tg-emoji emoji-id='5348236797606379943'>🔥</tg-emoji>"
ICON_USERS = "<tg-emoji emoji-id='5346136537123801643'>🔥</tg-emoji>"
ICON_PENCIL = "<tg-emoji emoji-id='5348224471050238603'>🔥</tg-emoji>"
ICON_HORN = "<tg-emoji emoji-id='5348110675891731556'>🔥</tg-emoji>"

ICON_CROSS = "<tg-emoji emoji-id='5348362704572664028'>🔥</tg-emoji>"
ICON_LOCK = "<tg-emoji emoji-id='5348207836641902790'>🔥</tg-emoji>"
ICON_STAR = "<tg-emoji emoji-id='5348446245981536690'>🔥</tg-emoji>"
ICON_WARN = "<tg-emoji emoji-id='5348110675891731556'>🔥</tg-emoji>"

def get_user_mention(user):
    safe_name = html.escape(user.full_name)
    return f'<a href="tg://user?id={user.id}">{safe_name}</a>'

def is_night_time() -> bool:
    now_msk = datetime.now(MSK_TZ)
    hour = now_msk.hour
    return hour >= 22 or hour < 10

def get_night_notice() -> str:
    if is_night_time():
        return f"\n\n{ICON_WARN} <i>Обратите внимание: сейчас ночное время (с 22:00 до 10:00 МСК). Ваша заявка принята, но ответ модераторов может поступить утром!</i>"
    return ""

# ----------------------------------------------------------------------
# АНТИФЛУД МИДЛВАРЬ
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
# БАЗА ДАННЫХ (Supabase / PostgreSQL)
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
                CREATE TABLE IF NOT EXISTS secret_publishers (
                    user_id BIGINT PRIMARY KEY
                );
                CREATE TABLE IF NOT EXISTS secret_channels (
                    user_id BIGINT PRIMARY KEY,
                    channel_id TEXT
                );
                CREATE TABLE IF NOT EXISTS secret_types (
                    name TEXT PRIMARY KEY,
                    declined TEXT
                );
                CREATE TABLE IF NOT EXISTS secret_posts (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT,
                    channel_id TEXT,
                    message_id BIGINT,
                    secret_type TEXT,
                    base_text TEXT,
                    is_expired BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
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

            default_types = [("Лапка", "лапки"), ("Сердечко", "сердечка"), ("Тропы", "троп")]
            for s_name, s_dec in default_types:
                cursor.execute("INSERT INTO secret_types (name, declined) VALUES (%s, %s) ON CONFLICT (name) DO NOTHING", (s_name, s_dec))

            cursor.execute("INSERT INTO settings (key, value) VALUES ('timer_seconds', '510') ON CONFLICT (key) DO NOTHING")

            default_active_template = (
                "❕Секретка❕\n"
                "Секретка: [Тип_Секретки]\n\n"
                "Правила:\n"
                "1. Не ускорять\n"
                "2. Выйти с сервера после получения [Склоненный_Тип]\n"
                "3. Не покупать негативные мутаторы\n"
                "4. Не подниматься выше уровня над секреткой и не идти к воротам ускорения\n"
                "При несоблюдении правил, вы получите бан.\n"
                "Обжаловать бан можно в <a href='https://t.me/ToHSecrets_bot'>поддержке</a>!\n\n"
                "Секретка: [Ссылка]\n\n"
                "🤍Наш <a href='https://t.me/SecretsToH'>чат</a> | Наш <a href='https://t.me/ToHSecretss'>канал</a> | Наш <a href='https://t.me/ToHSecrets_bot'>бот</a>🤍"
            )
            cursor.execute("INSERT INTO settings (key, value) VALUES ('template_active', %s) ON CONFLICT (key) DO NOTHING", (default_active_template,))

            default_expired_template = (
                "❕Секретка❕\n"
                "Секретка: [Тип_Секретки]\n\n"
                "Секретка: Время вышло! В канале еще будут секретки и вы успеете попасть на них🤍\n\n"
                "🤍Наш <a href='https://t.me/SecretsToH'>чат</a> | "
                "Наш <a href='https://t.me/ToHSecretss'>канал</a> | "
                "Наш <a href='https://t.me/ToHSecrets_bot'>бот</a>🤍"
            )
            cursor.execute("INSERT INTO settings (key, value) VALUES ('template_expired', %s) ON CONFLICT (key) DO NOTHING", (default_expired_template,))

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

async def is_secret_publisher(user_id: int) -> bool:
    def _query():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT user_id FROM secret_publishers WHERE user_id = %s", (user_id,))
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

async def get_secret_types_dict() -> dict:
    def _query():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT name, declined FROM secret_types")
                return {row[0]: row[1] for row in cur.fetchall()}
    return await asyncio.to_thread(_query)

async def check_channel_rights(bot: Bot, user_id: int) -> str | None:
    def _query():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT channel_id FROM secret_channels WHERE user_id = %s", (user_id,))
                row = cur.fetchone()
                return row[0] if row else None
    ch_id = await asyncio.to_thread(_query)
    if not ch_id:
        return None

    try:
        b_mem = await bot.get_chat_member(chat_id=ch_id, user_id=bot.id)
        if b_mem.status not in ["administrator", "creator"]:
            return None
        u_mem = await bot.get_chat_member(chat_id=ch_id, user_id=user_id)
        if u_mem.status not in ["administrator", "creator"]:
            def _del():
                with get_db() as conn:
                    with conn.cursor() as cur:
                        cur.execute("DELETE FROM secret_channels WHERE user_id = %s", (user_id,))
            await asyncio.to_thread(_del)
            return None
        return ch_id
    except Exception:
        return None

async def expire_secret_post(bot: Bot, post_id: int, channel_id: str, message_id: int, s_type: str):
    def _mark():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE secret_posts SET is_expired = TRUE WHERE id = %s", (post_id,))
    await asyncio.to_thread(_mark)

    expired_tmpl = await get_setting("template_expired", "❕Секретка❕\nСекретка: [Тип_Секретки]\n\nВремя вышло!")
    final_text = expired_tmpl.replace("[Тип_Секретки]", s_type)
    try:
        await bot.edit_message_caption(chat_id=channel_id, message_id=message_id, caption=final_text, parse_mode="HTML")
    except Exception as e:
        logging.error(f"Не удалось обновить истекший пост №{post_id}: {e}")

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
    complaint_photos = State()
    
    appeal_nickname = State()
    appeal_place = State()
    appeal_reason = State()
    appeal_photos = State()
    
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

class SecretPublisherStates(StatesGroup):
    waiting_for_channel = State()
    waiting_for_type = State()
    waiting_for_photo = State()
    waiting_for_link = State()
    waiting_for_confirm = State()

class SecretAdminStates(StatesGroup):
    add_publisher_id = State()
    del_publisher_id = State()
    add_type_name = State()
    add_type_declined = State()
    set_timer = State()
    template_active = State()
    template_expired = State()

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
BTN_SECRETS = "🔮 Секретки"

async def main_keyboard(user_id: int):
    keyboard = [
        [KeyboardButton(text=BTN_COMPLAINT), KeyboardButton(text=BTN_APPEAL)],
        [KeyboardButton(text=BTN_FRIENDS), KeyboardButton(text=BTN_QUESTION)],
        [KeyboardButton(text=BTN_HELPER_APPLY), KeyboardButton(text=BTN_SECRET_APPLY)]
    ]
    
    if await is_secret_publisher(user_id):
        keyboard.append([KeyboardButton(text=BTN_SECRETS)])

    keyboard.append([KeyboardButton(text=BTN_REFRESH)])

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
        [InlineKeyboardButton(text="📊 Статистика поддержки", callback_data="adm_stats"),
         InlineKeyboardButton(text="👥 Статистика хелперов", callback_data="adm_list_stats")],
        [InlineKeyboardButton(text=f"👯‍♀️ Добавление в друзья: {friend_status}", callback_data="toggle_friend")],
        [InlineKeyboardButton(text=f"📝 Набор в хелперы: {helper_status}", callback_data="toggle_helper")],
        [InlineKeyboardButton(text=f"🔍 Искатели секреток: {secret_status}", callback_data="toggle_secret")],
        [InlineKeyboardButton(text="👑 Главные админы поддержки", callback_data="manage_main_admins")],
        [InlineKeyboardButton(text="✏️ Ник для друзей", callback_data="change_friend_nick")],
        [InlineKeyboardButton(text="🔮 Управление Секретками (Панель)", callback_data="adm_secret_panel")]
    ])

def secret_admin_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Управление публикаторами", callback_data="sec_manage_pubs")],
        [InlineKeyboardButton(text="📊 Статистика постов (24ч)", callback_data="sec_stats")],
        [InlineKeyboardButton(text="📝 Редактировать шаблоны", callback_data="sec_templates")],
        [InlineKeyboardButton(text="➕ Добавить тип", callback_data="sec_add_type"),
         InlineKeyboardButton(text="🗑 Удалить тип", callback_data="sec_del_type")],
        [InlineKeyboardButton(text="⏱ Настроить таймер", callback_data="sec_set_timer")],
        [InlineKeyboardButton(text="🔙 Назад в админку", callback_data="back_to_adm")]
    ])

def publisher_menu_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📥 Отправить секретку", callback_data="pub_send_secret")],
        [InlineKeyboardButton(text="📋 Мои активные посты", callback_data="pub_my_posts")],
        [InlineKeyboardButton(text="📢 Привязать канал", callback_data="pub_bind_channel")]
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

def skip_photo_kb(target: str):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⏩ Пропустить прикрепление фото", callback_data=f"skip_photo_{target}")
    ]])

# ----------------------------------------------------------------------
# ОТПРАВКА БИЛЕТА С МЕДИА (ДО 5 ФОТО)
# ----------------------------------------------------------------------
async def dispatch_ticket_to_admin(user_id: int, user, category: str, text: str, photos: List[str]):
    ticket_id = await create_ticket(user_id, category)
    user_mention = get_user_mention(user)

    cat_titles = {
        "complaint": "🚨 #Жалоба",
        "appeal": "😡 #Обжалование",
        "question": "❓ #Вопрос",
        "friends": "👯‍♀️ #Друзья"
    }
    title = cat_titles.get(category, "📩 #Заявка")
    admin_card_text = f"{title} | <b>Заявка №{ticket_id}</b>\n👤 От: {user_mention} | ID: <code>{user_id}</code>\n\n{text}"

    if photos:
        if len(photos) == 1:
            sent = await bot.send_photo(
                ADMIN_CHAT_ID,
                photo=photos[0],
                caption=admin_card_text,
                reply_markup=take_ticket_kb(ticket_id),
                parse_mode="HTML"
            )
            await map_message(sent.message_id, user_id, ticket_id)
        else:
            media = [InputMediaPhoto(media=photos[0], caption=admin_card_text, parse_mode="HTML")]
            for p in photos[1:]:
                media.append(InputMediaPhoto(media=p))
            
            sent_group = await bot.send_media_group(ADMIN_CHAT_ID, media=media)
            for m in sent_group:
                await map_message(m.message_id, user_id, ticket_id)
            
            ctrl_msg = await bot.send_message(
                ADMIN_CHAT_ID,
                f"👆 <i>Управление заявкой №{ticket_id} (прикреплено {len(photos)} фото):</i>",
                reply_markup=take_ticket_kb(ticket_id),
                parse_mode="HTML"
            )
            await map_message(ctrl_msg.message_id, user_id, ticket_id)
    else:
        sent = await bot.send_message(
            ADMIN_CHAT_ID,
            admin_card_text,
            reply_markup=take_ticket_kb(ticket_id),
            parse_mode="HTML"
        )
        await map_message(sent.message_id, user_id, ticket_id)

    return ticket_id

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
            f"{ICON_CROSS} Вы заблокированы в поддержке.\n<b>Причина:</b> {html.escape(banned[0])}",
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
        f"{ICON_USERS} Активных пользователей: <code>{users_count}</code>\n"
        f"<tg-emoji emoji-id='5348348681504441752'>🔥</tg-emoji> Всего тикетов: <code>{total}</code>\n"
        f"{ICON_CHECK} Успешно закрыто: <code>{closed}</code>\n"
        f"{ICON_CROSS} Отклонено: <code>{rejected}</code>\n"
        f"{ICON_STAR} Общая оценка саппорта: <code>{avg_rating} / 5.0</code>"
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
        text = f"{ICON_USERS} <b>Статистика по администраторам:</b>\n\nПока нет закрытых тикетов у админов."
    else:
        text = f"{ICON_USERS} <b>Статистика по администраторам:</b>\n\n"
        for admin_id, closed_cnt, avg_score, agent_no in rows:
            try:
                chat_member = await bot.get_chat(admin_id)
                name = chat_member.full_name
            except Exception:
                name = f"ID: {admin_id}"
            agent_str = f" [Агент #{agent_no}]" if agent_no else ""
            text += f"{ICON_USERS} <b>{html.escape(name)}</b>{agent_str} (<code>{admin_id}</code>)\n"
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
        await message.answer(f"{ICON_CHECK} Пользователь <code>{new_id}</code> успешно назначен главным администратором!", reply_markup=kb, parse_mode="HTML")
    except ValueError:
        await message.answer(f"{ICON_CROSS} Неверный формат ID. Введите числовой Telegram ID:", parse_mode="HTML")

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
            await message.answer(f"{ICON_CROSS} Нельзя удалить создателя бота!", parse_mode="HTML")
            return
        def _query():
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM main_admins WHERE admin_id = %s", (del_id,))
        await asyncio.to_thread(_query)
        await state.clear()
        kb = await main_keyboard(message.from_user.id)
        await message.answer(f"{ICON_CHECK} Пользователь <code>{del_id}</code> снят с поста главного администратора.", reply_markup=kb, parse_mode="HTML")
    except ValueError:
        await message.answer(f"{ICON_CROSS} Неверный формат ID. Введите числовой Telegram ID:", parse_mode="HTML")

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
    await call.message.answer(f"{ICON_PENCIL} Введите новый ник для друзей. Текущий: <code>{html.escape(current_nick)}</code>", parse_mode="HTML")
    await call.answer()

@router.message(Form.set_friend_nick)
async def save_friend_nick(message: Message, state: FSMContext):
    if not await is_main_admin(message.from_user.id): return
    await set_setting("friend_nickname", message.text.strip())
    await state.clear()
    kb = await main_keyboard(message.from_user.id)
    await message.answer(f"{ICON_CHECK} Ник успешно изменен!", reply_markup=kb, parse_mode="HTML")

# ----------------------------------------------------------------------
# АДМИН-ПАНЕЛЬ: УПРАВЛЕНИЕ СЕКРЕТКАМИ
# ----------------------------------------------------------------------
@router.callback_query(F.data == "adm_secret_panel")
async def adm_secret_panel(call: CallbackQuery):
    if not await is_main_admin(call.from_user.id): return
    dur = await get_setting("timer_seconds", "510")
    dur_int = int(dur) if dur.isdigit() else 510
    m, s = dur_int // 60, dur_int % 60
    text = (
        f"{ICON_BALL} <b>Панель управления секретками</b>\n\n"
        f"{ICON_TIMER} Таймер удаления ссылки: <b>{m} мин. {s} сек.</b> (<code>{dur_int}</code> сек.)\n"
        f"Настройте нужные параметры ниже:"
    )
    await call.message.edit_text(text, reply_markup=secret_admin_kb(), parse_mode="HTML")
    await call.answer()

@router.callback_query(F.data == "sec_manage_pubs")
async def sec_manage_pubs(call: CallbackQuery):
    if not await is_main_admin(call.from_user.id): return
    def _query():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT user_id FROM secret_publishers ORDER BY user_id")
                return [r[0] for r in cur.fetchall()]
    pubs = await asyncio.to_thread(_query)
    text = f"{ICON_USERS} <b>Список публикаторов секреток:</b>\n\n"
    if pubs:
        for idx, p_id in enumerate(pubs, 1):
            name_display = "Неизвестно"
            try:
                chat_info = await bot.get_chat(p_id)
                if chat_info.username:
                    name_display = f"@{chat_info.username}"
                elif chat_info.full_name:
                    name_display = html.escape(chat_info.full_name)
            except Exception:
                pass
            text += f"{idx}. <a href='tg://openmessage?user_id={p_id}'>{p_id}</a> - {name_display}\n"
    else:
        text += "<i>Список пуст (кнопка 'Секретки' никому не видна).</i>\n"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить публикатора", callback_data="sec_add_pub")],
        [InlineKeyboardButton(text="➖ Удалить публикатора", callback_data="sec_del_pub")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="adm_secret_panel")]
    ])
    await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML", link_preview_options=LinkPreviewOptions(is_disabled=True))
    await call.answer()

@router.callback_query(F.data == "sec_add_pub")
async def sec_add_pub_prompt(call: CallbackQuery, state: FSMContext):
    if not await is_main_admin(call.from_user.id): return
    await state.set_state(SecretAdminStates.add_publisher_id)
    await call.message.answer(f"{ICON_USERS} Введите <b>Telegram ID</b> пользователя, которому хотите выдать доступ к секреткам:", parse_mode="HTML")
    await call.answer()

@router.message(SecretAdminStates.add_publisher_id)
async def sec_add_pub_proc(message: Message, state: FSMContext):
    if not await is_main_admin(message.from_user.id): return
    try:
        t_id = int(message.text.strip())
        def _q():
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute("INSERT INTO secret_publishers (user_id) VALUES (%s) ON CONFLICT (user_id) DO NOTHING", (t_id,))
        await asyncio.to_thread(_q)
        await state.clear()
        kb = await main_keyboard(message.from_user.id)
        await message.answer(f"{ICON_CHECK} Пользователь <code>{t_id}</code> добавлен в публикаторы! У него появится кнопка '🔮 Секретки'.", reply_markup=kb, parse_mode="HTML")
    except ValueError:
        await message.answer(f"{ICON_CROSS} Введите корректный числовой ID.", parse_mode="HTML")

@router.callback_query(F.data == "sec_del_pub")
async def sec_del_pub_prompt(call: CallbackQuery, state: FSMContext):
    if not await is_main_admin(call.from_user.id): return
    await state.set_state(SecretAdminStates.del_publisher_id)
    await call.message.answer(f"{ICON_USERS} Введите <b>Telegram ID</b> пользователя для отзыва прав публикатора:", parse_mode="HTML")
    await call.answer()

@router.message(SecretAdminStates.del_publisher_id)
async def sec_del_pub_proc(message: Message, state: FSMContext):
    if not await is_main_admin(message.from_user.id): return
    try:
        t_id = int(message.text.strip())
        def _q():
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM secret_publishers WHERE user_id = %s", (t_id,))
        await asyncio.to_thread(_q)
        await state.clear()
        kb = await main_keyboard(message.from_user.id)
        await message.answer(f"{ICON_CHECK} Пользователь <code>{t_id}</code> удален из публикаторов.", reply_markup=kb, parse_mode="HTML")
    except ValueError:
        await message.answer(f"{ICON_CROSS} Введите корректный числовой ID.", parse_mode="HTML")

@router.callback_query(F.data == "sec_stats")
async def sec_stats(call: CallbackQuery):
    if not await is_main_admin(call.from_user.id): return
    def _query():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT user_id, channel_id, message_id, secret_type, created_at
                    FROM secret_posts
                    WHERE created_at >= CURRENT_TIMESTAMP - INTERVAL '24 hours'
                    ORDER BY created_at DESC
                """)
                return cur.fetchall()
                
    posts = await asyncio.to_thread(_query)
    
    if not posts:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="adm_secret_panel")]])
        await call.message.edit_text(f"{ICON_BALL} <b>Статистика публикаций за последние 24 часа:</b>\n\n<i>За последние 24 часа постов не публиковалось.</i>", reply_markup=kb, parse_mode="HTML")
        await call.answer()
        return

    user_posts_map = {}
    for u_id, ch_id, mid, stype, c_time in posts:
        if u_id not in user_posts_map:
            user_posts_map[u_id] = []
        user_posts_map[u_id].append((ch_id, mid, stype, c_time))

    text = f"{ICON_BALL} <b>Статистика публикаций за последние 24 часа (МСК):</b>\n\n"
    
    pub_index = 1
    for u_id, p_list in user_posts_map.items():
        name_display = "Неизвестно"
        try:
            chat_info = await bot.get_chat(u_id)
            if chat_info.username:
                name_display = f"@{chat_info.username}"
            elif chat_info.full_name:
                name_display = html.escape(chat_info.full_name)
        except Exception:
            pass
            
        posts_count = len(p_list)
        text += f"{pub_index}. <a href='tg://openmessage?user_id={u_id}'>{u_id}</a> - {name_display} - <b>{posts_count} пост(ов)</b>:\n"
        
        for post_idx, (ch_id, mid, stype, c_time) in enumerate(p_list, 1):
            msk_time = c_time.astimezone(MSK_TZ)
            time_str = msk_time.strftime("%H:%M:%S %d.%m.%Y")
            
            clean_ch = ch_id.replace("@", "")
            if clean_ch.startswith("https://t.me/"):
                post_url = f"{clean_ch}/{mid}"
            else:
                post_url = f"https://t.me/{clean_ch}/{mid}"
                
            text += f"   {post_idx}. {time_str} ({html.escape(stype)}) — <a href='{post_url}'>ссылка</a>\n"
            
        text += "\n"
        pub_index += 1

    if len(text) > 4000:
        text = text[:3950] + "\n... <i>(список сокращен из-за лимита длины)</i>"

    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="adm_secret_panel")]])
    await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML", link_preview_options=LinkPreviewOptions(is_disabled=True))
    await call.answer()

@router.callback_query(F.data == "sec_set_timer")
async def sec_set_timer(call: CallbackQuery, state: FSMContext):
    if not await is_main_admin(call.from_user.id): return
    await state.set_state(SecretAdminStates.set_timer)
    await call.message.answer(f"{ICON_TIMER} Введите новое время таймера <b>в секундах</b> (например, <code>510</code> для 8.5 минут):", parse_mode="HTML")
    await call.answer()

@router.message(SecretAdminStates.set_timer)
async def sec_set_timer_proc(message: Message, state: FSMContext):
    if not await is_main_admin(message.from_user.id): return
    if not message.text.isdigit():
        await message.answer(f"{ICON_CROSS} Введите число секунд:", parse_mode="HTML")
        return
    await set_setting("timer_seconds", message.text.strip())
    await state.clear()
    kb = await main_keyboard(message.from_user.id)
    await message.answer(f"{ICON_CHECK} Время таймера установлено на <code>{message.text.strip()}</code> сек!", reply_markup=kb, parse_mode="HTML")

@router.callback_query(F.data == "sec_templates")
async def sec_templates(call: CallbackQuery):
    if not await is_main_admin(call.from_user.id): return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Активный пост", callback_data="sec_tmpl_act")],
        [InlineKeyboardButton(text="✏️ Истекший пост", callback_data="sec_tmpl_exp")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="adm_secret_panel")]
    ])
    await call.message.edit_text(f"{ICON_PENCIL} <b>Редактирование шаблонов:</b>\n\nВыберите нужный шаблон:", reply_markup=kb, parse_mode="HTML")
    await call.answer()

@router.callback_query(F.data == "sec_tmpl_act")
async def sec_tmpl_act(call: CallbackQuery, state: FSMContext):
    if not await is_main_admin(call.from_user.id): return
    tmpl = await get_setting("template_active")
    await state.set_state(SecretAdminStates.template_active)
    await call.message.answer(f"{ICON_PENCIL} <b>Текущий шаблон активного поста:</b>\n\n<pre>{html.escape(tmpl)}</pre>\n\nОтправьте новый текст (доступны теги <code>[Тип_Секретки]</code>, <code>[Склоненный_Тип]</code>, <code>[Ссылка]</code>):", parse_mode="HTML")
    await call.answer()

@router.message(SecretAdminStates.template_active)
async def sec_tmpl_act_proc(message: Message, state: FSMContext):
    if not await is_main_admin(message.from_user.id): return
    await set_setting("template_active", message.text)
    await state.clear()
    kb = await main_keyboard(message.from_user.id)
    await message.answer(f"{ICON_CHECK} Шаблон активного поста обновлен!", reply_markup=kb, parse_mode="HTML")

@router.callback_query(F.data == "sec_tmpl_exp")
async def sec_tmpl_exp(call: CallbackQuery, state: FSMContext):
    if not await is_main_admin(call.from_user.id): return
    tmpl = await get_setting("template_expired")
    await state.set_state(SecretAdminStates.template_expired)
    await call.message.answer(f"{ICON_PENCIL} <b>Текущий шаблон истекшего поста:</b>\n\n<pre>{html.escape(tmpl)}</pre>\n\nОтправьте новый текст (доступен тег <code>[Тип_Секретки]</code>):", parse_mode="HTML")
    await call.answer()

@router.message(SecretAdminStates.template_expired)
async def sec_tmpl_exp_proc(message: Message, state: FSMContext):
    if not await is_main_admin(message.from_user.id): return
    await set_setting("template_expired", message.text)
    await state.clear()
    kb = await main_keyboard(message.from_user.id)
    await message.answer(f"{ICON_CHECK} Шаблон истекшего поста обновлен!", reply_markup=kb, parse_mode="HTML")

@router.callback_query(F.data == "sec_add_type")
async def sec_add_type(call: CallbackQuery, state: FSMContext):
    if not await is_main_admin(call.from_user.id): return
    await state.set_state(SecretAdminStates.add_type_name)
    await call.message.answer(f"{ICON_BALL} Введите название нового типа (например, <code>Звезда</code>):", parse_mode="HTML")
    await call.answer()

@router.message(SecretAdminStates.add_type_name)
async def sec_add_type_name_proc(message: Message, state: FSMContext):
    if not await is_main_admin(message.from_user.id): return
    await state.update_data(new_t_name=message.text.strip())
    await state.set_state(SecretAdminStates.add_type_declined)
    await message.answer("Введите форму родительного падежа (например, <code>звезды</code>):", parse_mode="HTML")

@router.message(SecretAdminStates.add_type_declined)
async def sec_add_type_dec_proc(message: Message, state: FSMContext):
    if not await is_main_admin(message.from_user.id): return
    data = await state.get_data()
    t_name = data["new_t_name"]
    t_dec = message.text.strip()
    def _q():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO secret_types (name, declined) VALUES (%s, %s) ON CONFLICT (name) DO UPDATE SET declined = EXCLUDED.declined", (t_name, t_dec))
    await asyncio.to_thread(_q)
    await state.clear()
    kb = await main_keyboard(message.from_user.id)
    await message.answer(f"{ICON_CHECK} Тип секретки <b>{t_name}</b> успешно добавлен!", reply_markup=kb, parse_mode="HTML")

@router.callback_query(F.data == "sec_del_type")
async def sec_del_type(call: CallbackQuery):
    if not await is_main_admin(call.from_user.id): return
    types_d = await get_secret_types_dict()
    buttons = []
    for k in types_d.keys():
        buttons.append([InlineKeyboardButton(text=f"🗑 {k}", callback_data=f"sec_rmtype_{k}")])
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="adm_secret_panel")])
    await call.message.edit_text("🗑 Выберите тип секретки для удаления:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await call.answer()

@router.callback_query(F.data.startswith("sec_rmtype_"))
async def sec_rmtype_proc(call: CallbackQuery):
    if not await is_main_admin(call.from_user.id): return
    t_name = call.data.split("_", 2)[2]
    def _q():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM secret_types WHERE name = %s", (t_name,))
    await asyncio.to_thread(_q)
    await call.message.edit_text(f"{ICON_CHECK} Тип секретки <b>{t_name}</b> удален.", parse_mode="HTML")
    await call.answer()

# ----------------------------------------------------------------------
# ПУБЛИКАЦИЯ СЕКРЕТОК
# ----------------------------------------------------------------------
@router.message(F.text == BTN_SECRETS, F.chat.type == "private")
async def open_secrets_menu(message: Message, state: FSMContext):
    if not await is_secret_publisher(message.from_user.id):
        return
    await state.clear()
    await message.answer(
        f"{ICON_BALL} <b>Панель публикации секреток</b>\n\nВыберите нужное действие:",
        reply_markup=publisher_menu_kb(),
        parse_mode="HTML"
    )

@router.callback_query(F.data == "pub_bind_channel")
async def pub_bind_channel_prompt(call: CallbackQuery, state: FSMContext):
    if not await is_secret_publisher(call.from_user.id): return
    await state.set_state(SecretPublisherStates.waiting_for_channel)
    await call.message.answer(
        f"{ICON_HORN} Отправьте <b>@username</b> канала (например, <code>@my_channel</code>).\n"
        "<i>Убедитесь, что бот добавлен туда администратором с правом публикации!</i>",
        parse_mode="HTML"
    )
    await call.answer()

@router.message(SecretPublisherStates.waiting_for_channel)
async def pub_bind_channel_proc(message: Message, state: FSMContext):
    if not await is_secret_publisher(message.from_user.id): return
    ch_input = message.text.strip()
    user_id = message.from_user.id
    try:
        b_mem = await bot.get_chat_member(chat_id=ch_input, user_id=bot.id)
        if b_mem.status not in ["administrator", "creator"]:
            await message.answer(f"{ICON_CROSS} Бот не является админом в этом канале!", parse_mode="HTML")
            return
        u_mem = await bot.get_chat_member(chat_id=ch_input, user_id=user_id)
        if u_mem.status not in ["administrator", "creator"]:
            await message.answer(f"{ICON_CROSS} Вы не админ этого канала!", parse_mode="HTML")
            return
        def _q():
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO secret_channels (user_id, channel_id) VALUES (%s, %s) "
                        "ON CONFLICT (user_id) DO UPDATE SET channel_id = EXCLUDED.channel_id",
                        (user_id, ch_input)
                    )
        await asyncio.to_thread(_q)
        await state.clear()
        kb = await main_keyboard(user_id)
        await message.answer(f"{ICON_CHECK} Канал <b>{ch_input}</b> успешно привязан!", reply_markup=kb, parse_mode="HTML")
    except Exception as e:
        await message.answer(f"{ICON_CROSS} Ошибка проверки канала: {e}", parse_mode="HTML")

@router.callback_query(F.data == "pub_my_posts")
async def pub_my_posts(call: CallbackQuery):
    if not await is_secret_publisher(call.from_user.id): return
    def _q():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, secret_type, message_id FROM secret_posts "
                    "WHERE user_id = %s AND is_expired = FALSE ORDER BY id DESC",
                    (call.from_user.id,)
                )
                return cur.fetchall()
    posts = await asyncio.to_thread(_q)
    if not posts:
        await call.message.answer("📭 У вас нет активных опубликованных постов.")
        await call.answer()
        return
    kb_bts = []
    for pid, stype, mid in posts:
        kb_bts.append([InlineKeyboardButton(text=f"📌 {stype} (ID: {mid}) — Завершить", callback_data=f"pub_close_{pid}")])
    await call.message.answer("📋 Ваши активные посты:\nНажмите, чтобы досрочно удалить ссылку в канале:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_bts))
    await call.answer()

@router.callback_query(F.data.startswith("pub_close_"))
async def pub_close_post(call: CallbackQuery):
    if not await is_secret_publisher(call.from_user.id): return
    pid = int(call.data.split("_")[2])
    def _q():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT channel_id, message_id, secret_type, is_expired FROM secret_posts WHERE id = %s", (pid,))
                return cur.fetchone()
    row = await asyncio.to_thread(_q)
    if not row or row[3]:
        await call.answer("⚠️ Пост уже завершен или не найден!", show_alert=True)
        return
    await expire_secret_post(bot, pid, row[0], row[1], row[2])
    await call.message.edit_text(f"{ICON_CHECK} Пост закрыт досрочно (ссылка стёрта).", parse_mode="HTML")
    await call.answer()

@router.callback_query(F.data == "pub_send_secret")
async def pub_send_secret(call: CallbackQuery, state: FSMContext):
    if not await is_secret_publisher(call.from_user.id): return
    ch_id = await check_channel_rights(bot, call.from_user.id)
    if not ch_id:
        await call.message.answer(f"{ICON_CROSS} Сначала привяжите канал через меню секреток!", reply_markup=publisher_menu_kb(), parse_mode="HTML")
        await call.answer()
        return
    types_d = await get_secret_types_dict()
    if not types_d:
        await call.message.answer(f"{ICON_CROSS} В базе нет типов секреток. Обратитесь к создателю.", parse_mode="HTML")
        await call.answer()
        return
    buttons = []
    for s_name in types_d.keys():
        buttons.append([InlineKeyboardButton(text=f"🔹 {s_name}", callback_data=f"pub_settype_{s_name}")])
    await call.message.edit_text("Выберите тип секретки:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await state.set_state(SecretPublisherStates.waiting_for_type)
    await call.answer()

@router.callback_query(SecretPublisherStates.waiting_for_type, F.data.startswith("pub_settype_"))
async def pub_settype(call: CallbackQuery, state: FSMContext):
    stype = call.data.split("_", 2)[2]
    await state.update_data(secret_type=stype)
    await call.message.edit_text(f"Выбрано: <b>{stype}</b>\n\nТеперь отправьте <b>фотографию</b> секретки:", parse_mode="HTML")
    await state.set_state(SecretPublisherStates.waiting_for_photo)
    await call.answer()

@router.message(SecretPublisherStates.waiting_for_photo, F.photo)
async def pub_photo(message: Message, state: FSMContext):
    await state.update_data(photo_id=message.photo[-1].file_id)
    await message.answer("Отлично! Теперь отправьте <b>ссылку на VIP-сервер</b> Roblox:", parse_mode="HTML")
    await state.set_state(SecretPublisherStates.waiting_for_link)

@router.message(SecretPublisherStates.waiting_for_link, F.text)
async def pub_link(message: Message, state: FSMContext):
    raw = message.text.strip()
    clean = re.sub(r'\s+', '', raw)
    clean_lower = clean.lower()

    is_roblox = "roblox.com" in clean_lower
    is_vip = ("privateserverlinkcode=" in clean_lower) or ("share?code=" in clean_lower)

    if not (is_roblox and is_vip):
        await message.answer(
            f"{ICON_CROSS} <b>Некорректная ссылка на VIP-сервер!</b>\n\n"
            "Бот принимает ссылки на VIP-сервера Roblox:\n"
            "• С ПК: <code>...roblox.com/games/...?privateServerLinkCode=...</code>\n"
            "• С телефона: <code>...roblox.com/share?code=...</code>",
            parse_mode="HTML"
        )
        return

    await state.update_data(link=clean)
    data = await state.get_data()
    stype = data["secret_type"]
    types_d = await get_secret_types_dict()
    dec = types_d.get(stype, stype)
    tmpl = await get_setting("template_active")
    preview = (
        tmpl.replace("[Тип_Секретки]", stype)
            .replace("[Склоненный_Тип]", dec)
            .replace("[Ссылка]", clean)
    )
    conf_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Опубликовать", callback_data="pub_conf_yes"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="pub_conf_no")
    ]])
    await message.answer("Предпросмотр поста:")
    await message.answer_photo(photo=data["photo_id"], caption=preview, reply_markup=conf_kb, parse_mode="HTML")
    await state.set_state(SecretPublisherStates.waiting_for_confirm)

@router.callback_query(SecretPublisherStates.waiting_for_confirm, F.data.startswith("pub_conf_"))
async def pub_confirm(call: CallbackQuery, state: FSMContext):
    action = call.data.split("_")[2]
    if action == "yes":
        ch_id = await check_channel_rights(bot, call.from_user.id)
        if not ch_id:
            await call.message.edit_caption(caption=f"{ICON_CROSS} Ошибка: нет доступа к привязанному каналу!", reply_markup=None, parse_mode="HTML")
            await state.clear()
            await call.answer()
            return
        data = await state.get_data()
        stype = data["secret_type"]
        types_d = await get_secret_types_dict()
        dec = types_d.get(stype, stype)
        tmpl = await get_setting("template_active")
        final_txt = (
            tmpl.replace("[Тип_Секретки]", stype)
                .replace("[Склоненный_Тип]", dec)
                .replace("[Ссылка]", data["link"])
        )
        try:
            sent = await bot.send_photo(chat_id=ch_id, photo=data["photo_id"], caption=final_txt, parse_mode="HTML")
            def _save():
                with get_db() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            "INSERT INTO secret_posts (user_id, channel_id, message_id, secret_type, base_text) "
                            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
                            (call.from_user.id, ch_id, sent.message_id, stype, final_txt)
                        )
                        return cur.fetchone()[0]
            await asyncio.to_thread(_save)
            await call.message.edit_caption(caption=f"{ICON_CHECK} Пост успешно опубликован в канале!", reply_markup=None, parse_mode="HTML")
        except Exception as e:
            await call.message.edit_caption(caption=f"{ICON_CROSS} Ошибка публикации: {e}", reply_markup=None, parse_mode="HTML")
    else:
        await call.message.edit_caption(caption=f"{ICON_CROSS} Публикация отменена.", reply_markup=None, parse_mode="HTML")
    await state.clear()
    await call.answer()

# ----------------------------------------------------------------------
# СИСТЕМА НАБОРА В ХЕЛПЕРЫ И ИСКАТЕЛИ СЕКРЕТОВ
# ----------------------------------------------------------------------
@router.message(F.text == BTN_HELPER_APPLY, F.chat.type == "private")
async def start_helper_apply(message: Message, state: FSMContext):
    await register_user(message.from_user.id)
    if await is_banned(message.from_user.id): return
    if await get_setting("helper_recruitment", "true") != "true":
        await message.answer(f"{ICON_WARN} Набор в команду поддержки в данный момент закрыт.", parse_mode="HTML")
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
    await message.answer(f"{ICON_CHECK} Ваша анкета успешно отправлена администрации! Ожидайте ответа.{night_txt}", reply_markup=kb, parse_mode="HTML")
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
        await bot.send_message(user_id, f"{ICON_CROSS} К сожалению, ваша заявка на хелпера была отклонена.", parse_mode="HTML")
    except Exception:
        pass
    await call.message.edit_text(call.message.text + "\n\n🔴 <b>Статус:</b> Отклонено ❌", parse_mode="HTML")
    await call.answer("Заявка отклонена!")

@router.message(F.text == BTN_SECRET_APPLY, F.chat.type == "private")
async def start_secret_apply(message: Message, state: FSMContext):
    await register_user(message.from_user.id)
    if await is_banned(message.from_user.id): return
    if await get_setting("secret_recruitment", "true") != "true":
        await message.answer(f"{ICON_WARN} Набор в искатели секреток в данный момент закрыт.", parse_mode="HTML")
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
    await message.answer(f"{ICON_CHECK} Ваша заявка в искатели секреток успешно отправлена! Ожидайте ответа.{night_txt}", reply_markup=kb, parse_mode="HTML")
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
        await bot.send_message(user_id, f"{ICON_CROSS} К сожалению, ваша заявка в искатели секреток была отклонена.", parse_mode="HTML")
    except Exception:
        pass
    await call.message.edit_text(call.message.text + "\n\n🔴 <b>Статус:</b> Отклонено ❌", parse_mode="HTML")
    await call.answer("Заявка отклонена!")

# ----------------------------------------------------------------------
# СОЗДАНИЕ ТИКЕТОВ С МЕДИА-БУФЕРОМ (ДО 5 ФОТО)
# ----------------------------------------------------------------------
async def check_active_ticket(message: Message) -> bool:
    active = await get_active_ticket(message.from_user.id)
    if active:
        await message.answer(
            f"{ICON_WARN} У вас уже есть активный тикет <b>№{active[0]}</b>.\nДождитесь ответа или закройте его, прежде чем открывать новый.",
            parse_mode="HTML"
        )
        return True
    return False

# Сборщик медиа-группы
async def collect_media_photos(message: Message, state: FSMContext, key: str = "photos") -> list:
    data = await state.get_data()
    photos = data.get(key, [])
    if message.photo:
        photos.append(message.photo[-1].file_id)
        await state.update_data({key: photos})
    return photos

# --- 1. ЖАЛОБА ---
@router.message(F.text == BTN_COMPLAINT, F.chat.type == "private")
async def start_complaint(message: Message, state: FSMContext):
    await register_user(message.from_user.id)
    if await is_banned(message.from_user.id) or await check_active_ticket(message): return
    await state.clear()
    await state.set_state(Form.complaint_reason)
    await message.answer(f"{NUM_1} <b>Суть нарушения:</b>\nОпишите подробно, что произошло.", parse_mode="HTML")

@router.message(Form.complaint_reason)
async def process_c_reason(message: Message, state: FSMContext):
    await state.update_data(c_reason=message.text)
    await state.set_state(Form.complaint_nickname)
    await message.answer(f"{NUM_2} Укажите ник нарушителя:", parse_mode="HTML")

@router.message(Form.complaint_nickname)
async def process_c_nickname(message: Message, state: FSMContext):
    await state.update_data(c_nickname=message.text)
    await state.set_state(Form.complaint_photos)
    await message.answer(
        f"{NUM_3} {ICON_PHOTO} Отправьте скриншоты-доказательства (<b>до 5 фото</b>, плашка уровней должна быть видна):",
        reply_markup=skip_photo_kb("complaint"),
        parse_mode="HTML"
    )

@router.callback_query(F.data == "skip_photo_complaint", Form.complaint_photos)
async def skip_c_photos(call: CallbackQuery, state: FSMContext):
    if await check_active_ticket(call.message): return
    data = await state.get_data()
    text = f"Суть: {html.escape(data.get('c_reason', ''))}\nНарушитель: <code>{html.escape(data.get('c_nickname', ''))}</code>"
    ticket_id = await dispatch_ticket_to_admin(call.from_user.id, call.from_user, "complaint", text, [])
    night_txt = get_night_notice()
    await call.message.edit_text(f"{ICON_CHECK} Жалоба №{ticket_id} отправлена!{night_txt}", reply_markup=user_cancel_kb(ticket_id), parse_mode="HTML")
    await state.clear()
    await call.answer()

@router.message(Form.complaint_photos, F.photo)
async def process_c_photo(message: Message, state: FSMContext):
    photos = await collect_media_photos(message, state, "c_photos")
    if len(photos) > 5:
        await message.answer(f"{ICON_CROSS} Лимит превышен! Прикрепите не более 5 фото.", parse_mode="HTML")
        await state.clear()
        return

    if message.media_group_id:
        await asyncio.sleep(1.2)
        cur_data = await state.get_data()
        if cur_data.get("c_processed"):
            return
        await state.update_data(c_processed=True)
        photos = cur_data.get("c_photos", photos)

    if await check_active_ticket(message): return
    data = await state.get_data()
    text = f"Суть: {html.escape(data.get('c_reason', ''))}\nНарушитель: <code>{html.escape(data.get('c_nickname', ''))}</code>"
    ticket_id = await dispatch_ticket_to_admin(message.from_user.id, message.from_user, "complaint", text, photos)
    night_txt = get_night_notice()
    await message.answer(f"{ICON_CHECK} Жалоба №{ticket_id} отправлена! (Прикреплено фото: {len(photos)}){night_txt}", reply_markup=user_cancel_kb(ticket_id), parse_mode="HTML")
    await state.clear()

# --- 2. ОБЖАЛОВАНИЕ ---
@router.message(F.text == BTN_APPEAL, F.chat.type == "private")
async def start_appeal(message: Message, state: FSMContext):
    await register_user(message.from_user.id)
    if await is_banned(message.from_user.id) or await check_active_ticket(message): return
    await state.clear()
    await state.set_state(Form.appeal_nickname)
    await message.answer(f"{NUM_1} <b>Ваш ник в игре:</b>", parse_mode="HTML")

@router.message(Form.appeal_nickname)
async def process_a_nickname(message: Message, state: FSMContext):
    await state.update_data(a_nickname=message.text)
    await state.set_state(Form.appeal_place)
    await message.answer(
        f"{NUM_2} <b>Где вы были заблокированы?</b>\n"
        "<i>(Саботер | Сервер с секреткой (Укажите время поста на котором вас забанили, если это секретка) | Фарм сервер)</i>",
        parse_mode="HTML"
    )

@router.message(Form.appeal_place)
async def process_a_place(message: Message, state: FSMContext):
    await state.update_data(a_place=message.text)
    await state.set_state(Form.appeal_reason)
    await message.answer(f"{NUM_3} <b>Причина, по которой мы должны вас разблокировать:</b>", parse_mode="HTML")

@router.message(Form.appeal_reason)
async def process_a_reason(message: Message, state: FSMContext):
    await state.update_data(a_reason=message.text)
    await state.set_state(Form.appeal_photos)
    await message.answer(
        f"{NUM_4} {ICON_PHOTO} Прикрепите скриншоты или доказательства (<b>до 5 фото</b>), либо нажмите кнопку ниже:",
        reply_markup=skip_photo_kb("appeal"),
        parse_mode="HTML"
    )

@router.callback_query(F.data == "skip_photo_appeal", Form.appeal_photos)
async def skip_a_photos(call: CallbackQuery, state: FSMContext):
    if await check_active_ticket(call.message): return
    data = await state.get_data()
    text = (
        f"Ник: <code>{html.escape(data.get('a_nickname', ''))}</code>\n"
        f"Где забанен: {html.escape(data.get('a_place', ''))}\n"
        f"Причина разбана: {html.escape(data.get('a_reason', ''))}"
    )
    ticket_id = await dispatch_ticket_to_admin(call.from_user.id, call.from_user, "appeal", text, [])
    night_txt = get_night_notice()
    await call.message.edit_text(f"{ICON_CHECK} Обжалование №{ticket_id} отправлено!{night_txt}", reply_markup=user_cancel_kb(ticket_id), parse_mode="HTML")
    await state.clear()
    await call.answer()

@router.message(Form.appeal_photos, F.photo)
async def process_a_photo(message: Message, state: FSMContext):
    photos = await collect_media_photos(message, state, "a_photos")
    if len(photos) > 5:
        await message.answer(f"{ICON_CROSS} Лимит превышен! Прикрепите не более 5 фото.", parse_mode="HTML")
        await state.clear()
        return

    if message.media_group_id:
        await asyncio.sleep(1.2)
        cur_data = await state.get_data()
        if cur_data.get("a_processed"):
            return
        await state.update_data(a_processed=True)
        photos = cur_data.get("a_photos", photos)

    if await check_active_ticket(message): return
    data = await state.get_data()
    text = (
        f"Ник: <code>{html.escape(data.get('a_nickname', ''))}</code>\n"
        f"Где забанен: {html.escape(data.get('a_place', ''))}\n"
        f"Причина разбана: {html.escape(data.get('a_reason', ''))}"
    )
    ticket_id = await dispatch_ticket_to_admin(message.from_user.id, message.from_user, "appeal", text, photos)
    night_txt = get_night_notice()
    await message.answer(f"{ICON_CHECK} Обжалование №{ticket_id} отправлено! (Прикреплено фото: {len(photos)}){night_txt}", reply_markup=user_cancel_kb(ticket_id), parse_mode="HTML")
    await state.clear()

# --- 3. ВОПРОС ---
@router.message(F.text == BTN_QUESTION, F.chat.type == "private")
async def start_question(message: Message, state: FSMContext):
    await register_user(message.from_user.id)
    if await is_banned(message.from_user.id) or await check_active_ticket(message): return
    await state.clear()
    await state.set_state(Form.question_content)
    await message.answer(f"<tg-emoji emoji-id='5348435310994802643'>🔥</tg-emoji> Задайте ваш вопрос (можно отправить текст или до 5 фото с описанием):", parse_mode="HTML")

@router.message(Form.question_content, F.photo | F.text)
async def process_question(message: Message, state: FSMContext):
    if message.photo:
        photos = await collect_media_photos(message, state, "q_photos")
        if len(photos) > 5:
            await message.answer(f"{ICON_CROSS} Лимит превышен! Прикрепите не более 5 фото.", parse_mode="HTML")
            await state.clear()
            return
        
        caption_text = message.caption or "Без описания"
        data = await state.get_data()
        if "q_text" not in data and message.caption:
            await state.update_data(q_text=caption_text)

        if message.media_group_id:
            await asyncio.sleep(1.2)
            cur_data = await state.get_data()
            if cur_data.get("q_processed"):
                return
            await state.update_data(q_processed=True)
            photos = cur_data.get("q_photos", photos)
            caption_text = cur_data.get("q_text", caption_text)

        if await check_active_ticket(message): return
        ticket_id = await dispatch_ticket_to_admin(message.from_user.id, message.from_user, "question", html.escape(caption_text), photos)
    else:
        if await check_active_ticket(message): return
        ticket_id = await dispatch_ticket_to_admin(message.from_user.id, message.from_user, "question", html.escape(message.text or ''), [])

    night_txt = get_night_notice()
    await message.answer(f"{ICON_CHECK} Вопрос №{ticket_id} отправлен!{night_txt}", reply_markup=user_cancel_kb(ticket_id), parse_mode="HTML")
    await state.clear()

# --- 4. ДРУЗЬЯ ---
@router.message(F.text == BTN_FRIENDS, F.chat.type == "private")
async def start_friends(message: Message, state: FSMContext):
    await register_user(message.from_user.id)
    if await is_banned(message.from_user.id) or await check_active_ticket(message): return
    if await get_setting("friend_active", "false") != "true":
        await message.answer(f"{ICON_WARN} Раздел добавления в друзья временно закрыт на технические работы.", parse_mode="HTML")
        return
    await state.clear()
    friend_nick = await get_setting("friend_nickname", "Администратор")
    await state.set_state(Form.friends_nickname)
    await message.answer(f"{ICON_USERS} Добавьтесь в друзья к игроку: <code>{html.escape(friend_nick)}</code>\n\n{NUM_1} Укажите ваш ник в игре:", parse_mode="HTML")

@router.message(Form.friends_nickname)
async def process_friends_nickname(message: Message, state: FSMContext):
    if await check_active_ticket(message): return
    text = f"Ник: <code>{html.escape(message.text or '')}</code>"
    ticket_id = await dispatch_ticket_to_admin(message.from_user.id, message.from_user, "friends", text, [])
    night_txt = get_night_notice()
    await message.answer(f"{ICON_CHECK} Заявка №{ticket_id} создана!{night_txt}", reply_markup=user_cancel_kb(ticket_id), parse_mode="HTML")
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
                f"{ICON_LOCK} Ваша заявка <b>№{ticket_id}</b> закрыта поддержкой.\nОцените качество обслуживания:",
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
        await call.message.edit_text(f"{ICON_CROSS} Заявка <b>№{ticket_id}</b> отменена вами.", parse_mode="HTML")
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
        await call.message.edit_text(f"{ICON_STAR} [ТЕСТ АДМИНА] Оценка {score}/5 сохранена, но в общую статистику не пошла.", parse_mode="HTML")
    else:
        await call.message.edit_text(f"{ICON_STAR} Спасибо за оценку ({score}/5)! Ваше мнение учтено.", parse_mode="HTML")
    
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
    await message.answer(f"{ICON_WARN} Пожалуйста, выберите нужный пункт меню для обращения.", reply_markup=kb, parse_mode="HTML")

# ----------------------------------------------------------------------
# КОМАНДЫ В АДМИН-ЧАТЕ
# ----------------------------------------------------------------------
@router.message(Command("news"), F.chat.id == ADMIN_CHAT_ID)
async def cmd_news_broadcast(message: Message):
    if not await is_main_admin(message.from_user.id):
        await message.answer(f"{ICON_CROSS} Рассылку могут запускать только главные администраторы!", parse_mode="HTML")
        return

    target_msg = message.reply_to_message if message.reply_to_message else None
    broadcast_text = None

    if not target_msg:
        args = message.text.split(maxsplit=1)
        if len(args) < 2:
            await message.answer(
                f"{ICON_CROSS} <b>Использование команды:</b>\n"
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
        f"{ICON_CHECK} <b>Рассылка завершена!</b>\n\n"
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
            f"{ICON_CROSS} <b>Не удалось определить пользователя.</b>\n\n"
            "• Ответьте командой <code>/ban [причина]</code> на карточку тикета или сообщение игрока.\n"
            "• Либо введите напрямую: <code>/ban [ID] [причина]</code>",
            parse_mode="HTML"
        )
        return

    is_owner_target = (OWNER_ID and user_id == OWNER_ID)
    if is_owner_target and user_id != message.from_user.id:
        await message.answer(f"{ICON_CROSS} Нельзя заблокировать создателя бота!", parse_mode="HTML")
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
            f"{ICON_CROSS} <b>Ваш доступ к поддержке заблокирован.</b>\n<b>Причина:</b> {html.escape(reason)}",
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
            f"{ICON_CROSS} <b>Не удалось определить пользователя.</b>\n\n"
            "• Ответьте командой <code>/unban</code> на карточку тикета или сообщение игрока.\n"
            "• Либо введите напрямую: <code>/unban [user_id]</code>",
            parse_mode="HTML"
        )
        return

    await unban_user_db(user_id)
    await message.answer(f"✅ Пользователь с ID <code>{user_id}</code> успешно разблокирован.", parse_mode="HTML")

# ----------------------------------------------------------------------
# ОТВЕТ ХЕЛПЕРА В ГРУППЕ
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
                    f"{ICON_CROSS} Заявка <b>№{pending_ticket_id}</b> отклонена.\n<b>Причина:</b> {reason}",
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
# ФОНОВЫЕ ВОРКЕРЫ: НАПОМИНАНИЯ И ИСТЕЧЕНИЕ СЕКРЕТОК
# ----------------------------------------------------------------------
async def secret_timer_worker():
    logging.info("Фоновый воркер секреток запущен.")
    while True:
        try:
            timer_sec = await get_setting("timer_seconds", "510")
            dur = int(timer_sec) if timer_sec.isdigit() else 510

            def _get_expired_posts():
                with get_db() as conn:
                    with conn.cursor() as cur:
                        cur.execute("""
                            SELECT id, channel_id, message_id, secret_type
                            FROM secret_posts
                            WHERE is_expired = FALSE
                              AND created_at <= CURRENT_TIMESTAMP - (%s * INTERVAL '1 second')
                        """, (dur,))
                        return cur.fetchall()

            expired_rows = await asyncio.to_thread(_get_expired_posts)
            for pid, ch_id, mid, stype in expired_rows:
                await expire_secret_post(bot, pid, ch_id, mid, stype)

        except Exception as err:
            logging.error(f"Ошибка в secret_timer_worker: {err}")

        await asyncio.sleep(15)

async def reminder_worker():
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
    asyncio.create_task(secret_timer_worker())
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
