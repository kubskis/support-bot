import asyncio
import logging
import os
import html
import re
import time
from datetime import datetime, timezone, timedelta
from contextlib import contextmanager
from typing import Any, Callable, Dict, List, Awaitable

import aiohttp
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
    InlineKeyboardMarkup, InlineKeyboardButton, TelegramObject,
    LinkPreviewOptions, InputMediaPhoto, BotCommand, BotCommandScopeChat, 
    BotCommandScopeAllGroupChats, BotCommandScopeAllPrivateChats
)

# ----------------------------------------------------------------------
# НАСТРОЙКИ И ОКРУЖЕНИЕ
# ----------------------------------------------------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_CHAT_ID = -1003945292994
OWNER_ID = int(os.getenv("ADMIN_ID", "0"))
MSK_TZ = timezone(timedelta(hours=3))

if not BOT_TOKEN:
    raise ValueError("ОШИБКА: Токен бота не найден! Укажите BOT_TOKEN в Environment Variables.")
if not DATABASE_URL:
    raise ValueError("ОШИБКА: Строка подключения к БД не найдена! Укажите DATABASE_URL.")

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()

creation_lock = asyncio.Lock()
publish_lock = asyncio.Lock()
processed_media_groups = set()

# Кастомные TGP цифры
NUM_1 = "<tg-emoji emoji-id='5285184495857715032'>🔥</tg-emoji>"
NUM_2 = "<tg-emoji emoji-id='5287236502382724614'>🔥</tg-emoji>"
NUM_3 = "<tg-emoji emoji-id='5287361241117911628'>🔥</tg-emoji>"
NUM_4 = "<tg-emoji emoji-id='5287402803516427365'>🔥</tg-emoji>"
NUM_5 = "<tg-emoji emoji-id='5285444878250033485'>🔥</tg-emoji>"

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
ICON_TIME = "<tg-emoji emoji-id='5348236797606379943'>🔥</tg-emoji>"

def get_user_mention(user):
    safe_name = html.escape(user.full_name)
    return f'<a href="tg://user?id={user.id}">{safe_name}</a>'

def is_night_time() -> bool:
    now_msk = datetime.now(MSK_TZ)
    return now_msk.hour >= 22 or now_msk.hour < 10

def get_night_notice() -> str:
    if is_night_time():
        return f"\n\n{ICON_TIME} <i>Обратите внимание: сейчас ночное время (с 22:00 до 10:00 МСК). Ваша заявка принята, но ответ модераторов может поступить утром!</i>"
    return ""

async def check_roblox_username(username: str) -> dict | None:
    url = "https://users.roblox.com/v1/usernames/users"
    payload = {"usernames": [username], "excludeBannedUsers": False}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=4)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    user_list = data.get("data", [])
                    if user_list:
                        return {
                            "id": user_list[0]["id"],
                            "name": user_list[0]["name"],
                            "displayName": user_list[0]["displayName"]
                        }
    except Exception as e:
        logging.error(f"Ошибка проверки Roblox профиля {username}: {e}")
    return None

# ----------------------------------------------------------------------
# АНТИФЛУД МИДЛВАРЬ
# ----------------------------------------------------------------------
class ThrottlingMiddleware(BaseMiddleware):
    def __init__(self, limit: float = 0.5):
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

dp.message.middleware(ThrottlingMiddleware(limit=0.5))
dp.callback_query.middleware(ThrottlingMiddleware(limit=0.5))
dp.include_router(router)

# ----------------------------------------------------------------------
# БАЗА ДАННЫХ
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
                CREATE TABLE IF NOT EXISTS user_notes (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT,
                    note TEXT,
                    author_name TEXT,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
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
                    last_actor TEXT DEFAULT 'user',
                    reminded_unassigned BOOLEAN DEFAULT FALSE,
                    reminded_idle BOOLEAN DEFAULT FALSE,
                    last_idle_remind TIMESTAMP WITH TIME ZONE DEFAULT NULL,
                    last_unassigned_remind TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                    card_text TEXT DEFAULT NULL
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
                    card_message_id BIGINT,
                    card_text TEXT DEFAULT NULL
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
                CREATE TABLE IF NOT EXISTS special_secret_types (
                    name TEXT PRIMARY KEY
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
                ALTER TABLE tickets ADD COLUMN IF NOT EXISTS last_actor TEXT DEFAULT 'user';
                ALTER TABLE tickets ADD COLUMN IF NOT EXISTS reminded_unassigned BOOLEAN DEFAULT FALSE;
                ALTER TABLE tickets ADD COLUMN IF NOT EXISTS reminded_idle BOOLEAN DEFAULT FALSE;
                ALTER TABLE tickets ADD COLUMN IF NOT EXISTS last_idle_remind TIMESTAMP WITH TIME ZONE DEFAULT NULL;
                ALTER TABLE tickets ADD COLUMN IF NOT EXISTS last_unassigned_remind TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP;
                ALTER TABLE tickets ADD COLUMN IF NOT EXISTS card_text TEXT DEFAULT NULL;
                ALTER TABLE pending_rejections ADD COLUMN IF NOT EXISTS card_message_id BIGINT;
                ALTER TABLE pending_rejections ADD COLUMN IF NOT EXISTS card_text TEXT DEFAULT NULL;
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

            cursor.execute("""
                DELETE FROM ratings 
                WHERE user_id IN (SELECT admin_id FROM main_admins)
                   OR user_id IN (SELECT admin_id FROM admin_agents)
                   OR user_id = %s
            """, (OWNER_ID,))

            cursor.execute("DELETE FROM secret_types WHERE name = 'Тропы'")

            default_types = [
                ("Лапка", "лапки"),
                ("Сердечко", "сердечка"),
                ("Тропы", "троп"),
                ("Телевизор", "троп"),
                ("Сейф", "пружинку")
            ]
            for s_name, s_dec in default_types:
                cursor.execute("""
                    INSERT INTO secret_types (name, declined) VALUES (%s, %s)
                    ON CONFLICT (name) DO UPDATE SET declined = EXCLUDED.declined
                """, (s_name, s_dec))

            cursor.execute("INSERT INTO special_secret_types (name) VALUES ('Фарм') ON CONFLICT (name) DO NOTHING")
            cursor.execute("INSERT INTO settings (key, value) VALUES ('timer_seconds', '510') ON CONFLICT (key) DO NOTHING")

            default_active_template = (
                "❕Секретка❕\n"
                "Тип: [Тип_Секретки]\n\n"
                "Правила:\n"
                "1. Не ускорять\n"
                "2. Выйти с сервера после получения [Склоненный_Тип]\n"
                "3. Не покупать негативные мутаторы\n"
                "4. Не подниматься выше уровня над секреткой и не идти к воротам ускорения\n"
                "При несоблюдении правил, вы получите бан.\n"
                "Обжаловать бан можно в <a href='https://t.me/ToHSecrets_bot'>поддержке</a>!\n\n"
                "Ссылка: [Ссылка]\n\n"
                "🤍Наш <a href='https://t.me/SecretsToH'>чат</a> | Наш <a href='https://t.me/ToHSecretss'>канал</a> | Наша <a href='https://t.me/ToHSecrets_bot'>поддержка</a>🤍"
            )
            cursor.execute("""
                INSERT INTO settings (key, value) VALUES ('template_active', %s)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
            """, (default_active_template,))

            default_farm_template = (
                "❕Фарм-Секретка❕\n\n"
                "Тип: [Тип_Особенной_Секретки]\n\n"
                "Правила:\n"
                "1. Не ускорять\n"
                "2. Выйти с сервера после получения монеток (можете перезайти, но выйти нужно, не занимаем очередь)\n"
                "3. Не покупать негативные мутаторы\n"
                "При несоблюдении правил, вы получите бан.\n"
                "Обжаловать бан можно в <a href='https://t.me/ToHSecrets_bot'>поддержке</a>!\n\n"
                "Ссылка: [Ссылка]\n\n"
                "🤍Наш <a href='https://t.me/SecretsToH'>чат</a> | Наш <a href='https://t.me/ToHSecretss'>канал</a> | Наша <a href='https://t.me/ToHSecrets_bot'>поддержка</a>🤍"
            )
            cursor.execute("""
                INSERT INTO settings (key, value) VALUES ('template_farm', %s)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
            """, (default_farm_template,))

            default_expired_template = (
                "❕Секретка❕\n"
                "Тип: [Тип_Секретки]\n\n"
                "Время вышло! В канале еще будут секретки и вы успеете попасть на них🤍\n\n"
                "🤍Наш <a href='https://t.me/SecretsToH'>чат</a> | "
                "Наш <a href='https://t.me/ToHSecretss'>канал</a> | "
                "Наша <a href='https://t.me/ToHSecrets_bot'>поддержка</a>🤍"
            )
            cursor.execute("""
                INSERT INTO settings (key, value) VALUES ('template_expired', %s)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
            """, (default_expired_template,))

    logging.info("База данных успешно инициализирована.")

_init_db_sync()

async def setup_bot_commands(bot: Bot):
    try:
        default_commands = [
            BotCommand(command="start", description="🔄 Главное меню поддержки"),
        ]
        await bot.set_my_commands(default_commands, scope=BotCommandScopeAllPrivateChats())

        admin_commands = [
            BotCommand(command="mystats", description="📊 Моя статистика агента"),
            BotCommand(command="secstats", description="🔮 Моя статистика публикатора"),
            BotCommand(command="supptop", description="🏆 Топ агентов поддержки (Гл. Админ)"),
            BotCommand(command="sectop", description="🏆 Топ искателей за всё время (Гл. Админ)"),
            BotCommand(command="opentickets", description="📋 Неразобранные тикеты (Гл. Админ)"),
            BotCommand(command="note", description="📌 Добавить заметку игроку (Reply)"),
            BotCommand(command="deletenote", description="🗑 Удалить заметку игрока (Гл. Админ)"),
            BotCommand(command="ban", description="🔨 Заблокировать игрока"),
            BotCommand(command="unban", description="🔓 Разблокировать игрока"),
            BotCommand(command="news", description="📢 Рассылка новости всем (Гл. Админ)"),
        ]
        await bot.set_my_commands(admin_commands, scope=BotCommandScopeAllGroupChats())
        await bot.set_my_commands(admin_commands, scope=BotCommandScopeChat(chat_id=ADMIN_CHAT_ID))
    except Exception as e:
        logging.warning(f"Ошибка настройки подсказок команд: {e}")

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
        if member.status in ["creator", "administrator", "member", "restricted"]:
            return True
    except Exception:
        return True
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

async def get_user_notes(user_id: int) -> list:
    def _query():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id, note, author_name FROM user_notes WHERE user_id = %s ORDER BY id ASC", (user_id,))
                return cur.fetchall()
    try:
        return await asyncio.to_thread(_query)
    except Exception:
        return []

async def add_user_note(user_id: int, note: str, author_name: str) -> int:
    def _query():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO user_notes (user_id, note, author_name, created_at)
                    VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
                    RETURNING id
                """, (user_id, note, author_name))
                return cur.fetchone()[0]
    return await asyncio.to_thread(_query)

async def delete_user_note_by_id(note_id: int, user_id: int) -> bool:
    def _query():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM user_notes WHERE id = %s AND user_id = %s RETURNING id", (note_id, user_id))
                return cur.fetchone() is not None
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
    except Exception:
        pass

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
                cur.execute("""
                    SELECT COALESCE(AVG(score), 0) 
                    FROM ratings 
                    WHERE user_id != %s 
                      AND user_id NOT IN (SELECT admin_id FROM main_admins)
                      AND user_id NOT IN (SELECT admin_id FROM admin_agents)
                """, (OWNER_ID,))
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
                    SELECT t.admin_id, COUNT(*) as closed_count,
                           COALESCE(AVG(CASE 
                               WHEN r.user_id != %s 
                                AND r.user_id NOT IN (SELECT admin_id FROM main_admins) 
                                AND r.user_id NOT IN (SELECT admin_id FROM admin_agents)
                               THEN r.score 
                               ELSE NULL 
                           END), 0) as avg_score,
                           a.agent_number
                    FROM tickets t
                    LEFT JOIN ratings r ON t.ticket_id = r.ticket_id
                    LEFT JOIN admin_agents a ON t.admin_id = a.admin_id
                    WHERE t.admin_id IS NOT NULL AND t.status = 'closed'
                    GROUP BY t.admin_id, a.agent_number
                """, (OWNER_ID,))
                return cur.fetchall()
    try:
        return await asyncio.to_thread(_query)
    except Exception:
        return []

async def create_ticket(user_id: int, category: str = 'general', card_text: str = None) -> int:
    def _query():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO tickets (user_id, status, category, card_text, last_actor, created_at, updated_at, last_unassigned_remind, last_idle_remind) "
                    "VALUES (%s, 'pending', %s, %s, 'user', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, NULL) RETURNING ticket_id",
                    (user_id, category, card_text)
                )
                return cur.fetchone()[0]
    return await asyncio.to_thread(_query)

async def touch_ticket(ticket_id: int, actor: str = 'user'):
    def _query():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT last_actor FROM tickets WHERE ticket_id = %s", (ticket_id,))
                row = cur.fetchone()
                current_actor = row[0] if row else None

                if current_actor != actor:
                    cur.execute(
                        "UPDATE tickets SET updated_at = CURRENT_TIMESTAMP, last_actor = %s, reminded_idle = FALSE, last_idle_remind = NULL WHERE ticket_id = %s",
                        (actor, ticket_id)
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

async def get_ticket_info(ticket_id: int):
    def _query():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT user_id, admin_id, status, card_text FROM tickets WHERE ticket_id = %s", (ticket_id,))
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
                    "UPDATE tickets SET status = 'active', admin_id = %s, last_actor = 'admin', updated_at = CURRENT_TIMESTAMP, reminded_idle = FALSE, last_idle_remind = NULL WHERE ticket_id = %s",
                    (admin_id, ticket_id)
                )
    await asyncio.to_thread(_query)

async def close_ticket_db(ticket_id: int, status: str = 'closed', admin_id: int = None):
    def _query():
        with get_db() as conn:
            with conn.cursor() as cur:
                if admin_id:
                    cur.execute(
                        "UPDATE tickets SET status = %s, admin_id = %s, updated_at = CURRENT_TIMESTAMP WHERE ticket_id = %s",
                        (status, admin_id, ticket_id)
                    )
                else:
                    cur.execute(
                        "UPDATE tickets SET status = %s, updated_at = CURRENT_TIMESTAMP WHERE ticket_id = %s",
                        (status, ticket_id)
                    )
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

async def add_pending_rejection(prompt_message_id: int, ticket_id: int, card_message_id: int, card_text: str = None):
    def _query():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO pending_rejections (prompt_message_id, ticket_id, card_message_id, card_text) VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT (prompt_message_id) DO UPDATE SET ticket_id = EXCLUDED.ticket_id, card_message_id = EXCLUDED.card_message_id, card_text = EXCLUDED.card_text",
                    (prompt_message_id, ticket_id, card_message_id, card_text)
                )
    await asyncio.to_thread(_query)

async def get_pending_rejection(prompt_message_id: int):
    def _query():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT ticket_id, card_message_id, card_text FROM pending_rejections WHERE prompt_message_id = %s",
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

async def get_special_secret_types() -> list:
    def _query():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT name FROM special_secret_types ORDER BY name ASC")
                return [row[0] for row in cur.fetchall()]
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

    expired_tmpl = await get_setting("template_expired", "❕Секретка❕\nТип: [Тип_Секретки]\n\nВремя вышло!")
    final_text = expired_tmpl.replace("[Тип_Секретки]", s_type)
    try:
        await bot.edit_message_caption(chat_id=channel_id, message_id=message_id, caption=final_text, parse_mode="HTML")
    except Exception as e:
        logging.error(f"Не удалось обновить истекший пост №{post_id}: {e}")

# ----------------------------------------------------------------------
# КЛАВИАТУРЫ
# ----------------------------------------------------------------------
async def main_keyboard(user_id: int):
    keyboard = [
        [KeyboardButton(text="🚨 Жалоба на игрока"), KeyboardButton(text="😡 Обжалование бана")],
        [KeyboardButton(text="👯‍♀️ Добавление в друзья (VIP)"), KeyboardButton(text="❓ Задать вопрос")],
        [KeyboardButton(text="📝 Подать заявку на хелпера"), KeyboardButton(text="🔍 Набор в искатели секреток")]
    ]
    
    if await is_secret_publisher(user_id):
        keyboard.append([KeyboardButton(text="🔮 Секретки")])

    keyboard.append([KeyboardButton(text="🔄 Перезагрузить меню")])

    if await is_main_admin(user_id):
        keyboard.append([KeyboardButton(text="⚙️ Админ-панель")])

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
        [InlineKeyboardButton(text="✏ Ник для друзей", callback_data="change_friend_nick")],
        [InlineKeyboardButton(text="🔮 Управление Секретками (Панель)", callback_data="adm_secret_panel")]
    ])

def secret_admin_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Управление публикаторами", callback_data="sec_manage_pubs")],
        [InlineKeyboardButton(text="📊 Статистика постов (24ч)", callback_data="sec_stats")],
        [InlineKeyboardButton(text="📝 Редактировать шаблоны", callback_data="sec_templates")],
        [InlineKeyboardButton(text="➕ Добавить обычный тип", callback_data="sec_add_type"),
         InlineKeyboardButton(text="🗑 Удалить обычный тип", callback_data="sec_del_type")],
        [InlineKeyboardButton(text="➕ Добавить особый тип", callback_data="sec_add_spec_type"),
         InlineKeyboardButton(text="🗑 Удалить особый тип", callback_data="sec_del_spec_type")],
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

def complaint_servers_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌐 Сервер с секреткой", callback_data="cmp_srv_secret")],
        [InlineKeyboardButton(text="🕵 Поиск саботера", callback_data="cmp_srv_saboteur")],
        [InlineKeyboardButton(text="🌾 Фарм сервер", callback_data="cmp_srv_farm")]
    ])

def appeal_servers_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌐 Сервер с секреткой", callback_data="app_srv_secret")],
        [InlineKeyboardButton(text="🕵️ Поиск саботера", callback_data="app_srv_saboteur")],
        [InlineKeyboardButton(text="🌾 Фарм сервер", callback_data="app_srv_farm")]
    ])

# ----------------------------------------------------------------------
# ОТПРАВКА БИЛЕТА
# ----------------------------------------------------------------------
async def dispatch_ticket_to_admin(user_id: int, user, category: str, text: str, photos: List[str]):
    user_mention = get_user_mention(user)

    cat_titles = {
        "complaint": "🚨 #Жалоба",
        "appeal": "😡 #Обжалование",
        "question": "❓ #Вопрос",
        "friends": "👯‍♀️ #Друзья"
    }
    title = cat_titles.get(category, "📩 #Заявка")
    
    notes_list = await get_user_notes(user_id)
    notes_header = ""
    if notes_list:
        lines = []
        for n_id, n_text, n_author in notes_list:
            lines.append(f"• <b>[#{n_id}]</b> <i>{html.escape(n_text)}</i> (от: {html.escape(n_author)})")
        joined = "\n".join(lines)
        notes_header = f"📌 <b>Постоянные заметки:</b>\n{joined}\n━━━━━━━━━━━━━━━━━━━━\n"

    ticket_id = await create_ticket(user_id, category, "")
    admin_card_text = f"{notes_header}{title} | <b>Заявка №{ticket_id}</b>\n👤 От: {user_mention} | ID: <code>{user_id}</code>\n\n{text}"

    def _save_card_txt():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE tickets SET card_text = %s WHERE ticket_id = %s", (admin_card_text, ticket_id))
    await asyncio.to_thread(_save_card_txt)

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
# СТАРТ И ОБРАБОТКА МЕНЮ (НАДЁЖНЫЕ РЕГУЛЯРНЫЕ ВЫРАЖЕНИЯ)
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

@router.message(F.text.regexp(r"(?i)перезагруз"), F.chat.type == "private")
async def refresh_menu_handler(message: Message, state: FSMContext):
    await register_user(message.from_user.id)
    if await is_banned(message.from_user.id): return
    await state.clear()
    kb = await main_keyboard(message.from_user.id)
    await message.answer("<tg-emoji emoji-id='5346269127059196142'>🔥</tg-emoji> <b>Меню обновлено!</b>", reply_markup=kb, parse_mode="HTML")

@router.message(F.text.regexp(r"(?i)админ-панел"), F.chat.type == "private")
async def open_admin_panel(message: Message, state: FSMContext):
    if not await is_main_admin(message.from_user.id):
        return
    await state.clear()
    kb = await admin_panel_kb()
    await message.answer("<tg-emoji emoji-id='5348292765325212780'>🔥</tg-emoji> <b>Панель администратора</b>", reply_markup=kb, parse_mode="HTML")

# --- 1. ЖАЛОБА ---
@router.message(F.text.regexp(r"(?i)жалоб"), F.chat.type == "private")
async def start_complaint(message: Message, state: FSMContext):
    await register_user(message.from_user.id)
    if await is_banned(message.from_user.id) or await check_active_ticket(message): return
    await state.clear()
    await state.set_state(Form.complaint_nicknames)
    await state.update_data(nicks_list=[], roblox_profiles=[])
    await message.answer(
        f"{NUM_1} <b>Укажите ник нарушителя:</b>\n"
        "<i>(Напишите точный игровой никнейм в Roblox)</i>",
        parse_mode="HTML"
    )

# --- 2. ОБЖАЛОВАНИЕ ---
@router.message(F.text.regexp(r"(?i)обжалован"), F.chat.type == "private")
async def start_appeal(message: Message, state: FSMContext):
    await register_user(message.from_user.id)
    if await is_banned(message.from_user.id) or await check_active_ticket(message): return
    await state.clear()
    await state.set_state(Form.appeal_nickname)
    await message.answer(
        f"{NUM_1} <b>Ваш ник в игре:</b>\n"
        "<i>(Укажите ваш точный никнейм в Roblox, на который был выдан бан)</i>",
        parse_mode="HTML"
    )

# --- 3. ВОПРОС ---
@router.message(F.text.regexp(r"(?i)вопрос"), F.chat.type == "private")
async def start_question(message: Message, state: FSMContext):
    await register_user(message.from_user.id)
    if await is_banned(message.from_user.id) or await check_active_ticket(message): return
    await state.clear()
    await state.set_state(Form.question_content)
    await message.answer(f"<tg-emoji emoji-id='5348435310994802643'>🔥</tg-emoji> Задайте ваш вопрос (можно отправить текст или до 5 фото с описанием):", parse_mode="HTML")

# --- 4. ДРУЗЬЯ ---
@router.message(F.text.regexp(r"(?i)друг"), F.chat.type == "private")
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

# --- 5. ХЕЛПЕР ---
@router.message(F.text.regexp(r"(?i)хелпер"), F.chat.type == "private")
async def start_helper_apply(message: Message, state: FSMContext):
    await register_user(message.from_user.id)
    if await is_banned(message.from_user.id): return
    if await get_setting("helper_recruitment", "true") != "true":
        await message.answer(f"{ICON_WARN} Набор в команду поддержки в данный момент закрыт.", parse_mode="HTML")
        return
    await state.clear()
    await state.set_state(Form.helper_name)
    await message.answer(f"<tg-emoji emoji-id='5346192285799302524'>🔥</tg-emoji> <b>Заявка в команду поддержки (Хелперы)</b>\n\n{NUM_1} Ваше имя:", parse_mode="HTML")

# --- 6. ИСКАТЕЛИ СЕКРЕТОК ---
@router.message(F.text.regexp(r"(?i)искател"), F.chat.type == "private")
async def start_secret_apply(message: Message, state: FSMContext):
    await register_user(message.from_user.id)
    if await is_banned(message.from_user.id): return
    if await get_setting("secret_recruitment", "true") != "true":
        await message.answer(f"{ICON_WARN} Набор в искатели секреток в данный момент закрыт.", parse_mode="HTML")
        return
    await state.clear()
    await state.set_state(Form.secret_name)
    await message.answer(f"<tg-emoji emoji-id='5345840270279724328'>🔥</tg-emoji> <b>Заявка в искатели секреток</b>\n\n{NUM_1} Ваше имя:", parse_mode="HTML")

# --- 7. СЕКРЕТКИ ---
@router.message(F.text.regexp(r"(?i)секретк"), F.chat.type == "private")
async def open_secrets_menu(message: Message, state: FSMContext):
    if not await is_secret_publisher(message.from_user.id):
        return
    await state.clear()
    await message.answer(
        f"{ICON_BALL} <b>Панель публикации секреток</b>\n\nВыберите нужное действие:",
        reply_markup=publisher_menu_kb(),
        parse_mode="HTML"
    )

# ----------------------------------------------------------------------
# ШАГИ ФОРМ
# ----------------------------------------------------------------------
@router.message(Form.complaint_nicknames, F.text)
async def process_c_nicknames(message: Message, state: FSMContext):
    new_nick = message.text.strip()
    data = await state.get_data()
    nicks = data.get("nicks_list", [])
    profiles = data.get("roblox_profiles", [])
    
    rbx_info = await check_roblox_username(new_nick)
    if rbx_info:
        official_name = rbx_info["name"]
        rbx_id = rbx_info["id"]
        nicks.append(official_name)
        profiles.append(f"<a href='https://www.roblox.com/users/{rbx_id}/profile'>{official_name}</a> (ID: <code>{rbx_id}</code>)")
        verified_text = f"Профиль найден: <a href='https://www.roblox.com/users/{rbx_id}/profile'><b>{official_name}</b></a> (ID: <code>{rbx_id}</code>)"
    else:
        nicks.append(new_nick)
        profiles.append(f"<code>{html.escape(new_nick)}</code> (⚠️ <i>не найден в Roblox</i>)")
        verified_text = f"<code>{html.escape(new_nick)}</code> (⚠️ <i>профиль не найден в Roblox</i>)"

    await state.update_data(nicks_list=nicks, roblox_profiles=profiles)

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="➡️ Продолжить (перейти к сути)", callback_data="c_nicks_done")
    ]])

    await message.answer(
        f"{ICON_CHECK} Добавлен нарушитель: {verified_text}\n\n"
        f"👤 Всего нарушителей в списке: <b>{len(nicks)}</b>\n\n"
        "<i>Если нарушителей несколько — отправьте следующий ник сообщением. "
        "Если нарушитель только один (или вы ввели всех) — нажмите кнопку ниже:</i>",
        reply_markup=kb,
        parse_mode="HTML",
        link_preview_options=LinkPreviewOptions(is_disabled=True)
    )

@router.callback_query(F.data == "c_nicks_done", Form.complaint_nicknames)
async def process_c_nicks_done(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    nicks = data.get("nicks_list", [])
    if not nicks:
        await call.answer("Сначала введите хотя бы один ник!", show_alert=True)
        return
    await state.set_state(Form.complaint_reason)
    await call.message.edit_text(
        f"{NUM_2} <b>Опишите суть нарушения:</b>\n"
        "<i>(Что именно произошло: ускорение таймера, негативные мутаторы, срыв игры и т.д.)</i>",
        parse_mode="HTML"
    )
    await call.answer()

@router.message(Form.complaint_reason, F.text)
async def process_c_reason(message: Message, state: FSMContext):
    await state.update_data(c_reason=message.text.strip())
    await state.set_state(Form.complaint_place)
    await message.answer(
        f"{NUM_3} <b>Где произошло нарушение?</b>\n"
        "<i>Выберите нужный вариант на кнопках ниже:</i>",
        reply_markup=complaint_servers_kb(),
        parse_mode="HTML"
    )

@router.callback_query(F.data.startswith("cmp_srv_"), Form.complaint_place)
async def process_c_server_choice(call: CallbackQuery, state: FSMContext):
    srv_types = {
        "cmp_srv_secret": "Сервер с секреткой",
        "cmp_srv_saboteur": "Поиск саботера",
        "cmp_srv_farm": "Фарм сервер"
    }
    chosen_server = srv_types.get(call.data, "Не указан")
    await state.update_data(c_place=chosen_server)
    await state.set_state(Form.complaint_time)
    await call.message.edit_text(
        f"Выбран сервер: <b>{chosen_server}</b>\n\n"
        f"{NUM_4} {ICON_TIMER} <b>Укажите время публикации поста:</b>\n"
        "<i>(Укажите примерное или точное время публикации поста по МСК, например: <code>18:45</code> или <code>18:45:20 28.09</code>)</i>",
        parse_mode="HTML"
    )
    await call.answer()

@router.message(Form.complaint_time, F.text)
async def process_c_time(message: Message, state: FSMContext):
    await state.update_data(c_time=message.text.strip())
    await state.set_state(Form.complaint_photos)
    await message.answer(
        f"{NUM_5} {ICON_PHOTO} <b>Прикрепите доказательства (до 5 фото):</b>\n\n"
        "⚠️ <b>Важно:</b> фото строго запрещено обрезать! Плашка уровней и чат сервера должны быть отчётливо видны.",
        reply_markup=skip_photo_kb("complaint"),
        parse_mode="HTML"
    )

@router.callback_query(F.data == "skip_photo_complaint", Form.complaint_photos)
async def skip_c_photos(call: CallbackQuery, state: FSMContext):
    async with creation_lock:
        if await check_active_ticket(call.message):
            await state.clear()
            await call.answer()
            return
        data = await state.get_data()
        profiles_list = data.get("roblox_profiles", [])
        if profiles_list:
            nicks_formatted = ", ".join(profiles_list)
        else:
            nicks_formatted = ", ".join([f"<code>{html.escape(n)}</code>" for n in data.get("nicks_list", [])])

        text = (
            f"Нарушитель(и): {nicks_formatted}\n"
            f"Суть нарушения: {html.escape(data.get('c_reason', ''))}\n"
            f"Где произошло нарушение: {html.escape(data.get('c_place', ''))}\n"
            f"Время публикации поста: {html.escape(data.get('c_time', ''))}"
        )
        ticket_id = await dispatch_ticket_to_admin(call.from_user.id, call.from_user, "complaint", text, [])
        night_txt = get_night_notice()
        await call.message.edit_text(f"{ICON_CHECK} Жалоба №{ticket_id} отправлена!{night_txt}", reply_markup=user_cancel_kb(ticket_id), parse_mode="HTML")
        await state.clear()
        await call.answer()

@router.message(Form.complaint_photos, F.photo)
async def process_c_photo(message: Message, state: FSMContext):
    if await check_active_ticket(message):
        await state.clear()
        return

    photos = await collect_media_photos(message, state, "c_photos")
    if len(photos) > 5:
        await message.answer(f"{ICON_CROSS} Лимит превышен! Прикрепите не более 5 фото.", parse_mode="HTML")
        await state.clear()
        return

    mg_id = message.media_group_id
    if mg_id:
        await asyncio.sleep(1.2)
        async with creation_lock:
            if mg_id in processed_media_groups:
                return
            processed_media_groups.add(mg_id)
            if len(processed_media_groups) > 500:
                processed_media_groups.clear()

            cur_data = await state.get_data()
            photos = cur_data.get("c_photos", photos)

            if await check_active_ticket(message):
                await state.clear()
                return

            profiles_list = cur_data.get("roblox_profiles", [])
            if profiles_list:
                nicks_formatted = ", ".join(profiles_list)
            else:
                nicks_formatted = ", ".join([f"<code>{html.escape(n)}</code>" for n in cur_data.get("nicks_list", [])])

            text = (
                f"Нарушитель(и): {nicks_formatted}\n"
                f"Суть нарушения: {html.escape(cur_data.get('c_reason', ''))}\n"
                f"Где произошло нарушение: {html.escape(cur_data.get('c_place', ''))}\n"
                f"Время публикации поста: {html.escape(data.get('c_time', ''))}"
            )
            ticket_id = await dispatch_ticket_to_admin(message.from_user.id, message.from_user, "complaint", text, photos)
            await state.clear()
            night_txt = get_night_notice()
            await message.answer(f"{ICON_CHECK} Жалоба №{ticket_id} отправлена! (Прикреплено фото: {len(photos)}){night_txt}", reply_markup=user_cancel_kb(ticket_id), parse_mode="HTML")
            return

    async with creation_lock:
        if await check_active_ticket(message):
            await state.clear()
            return
        data = await state.get_data()
        profiles_list = data.get("roblox_profiles", [])
        if profiles_list:
            nicks_formatted = ", ".join(profiles_list)
        else:
            nicks_formatted = ", ".join([f"<code>{html.escape(n)}</code>" for n in data.get("nicks_list", [])])

        text = (
            f"Нарушитель(и): {nicks_formatted}\n"
            f"Суть нарушения: {html.escape(data.get('c_reason', ''))}\n"
            f"Где произошло нарушение: {html.escape(data.get('c_place', ''))}\n"
            f"Время публикации поста: {html.escape(data.get('c_time', ''))}"
        )
        ticket_id = await dispatch_ticket_to_admin(message.from_user.id, message.from_user, "complaint", text, photos)
        await state.clear()
        night_txt = get_night_notice()
        await message.answer(f"{ICON_CHECK} Жалоба №{ticket_id} отправлена! (Прикреплено фото: {len(photos)}){night_txt}", reply_markup=user_cancel_kb(ticket_id), parse_mode="HTML")

@router.message(Form.appeal_nickname, F.text)
async def process_a_nickname(message: Message, state: FSMContext):
    nick_input = message.text.strip()
    rbx_info = await check_roblox_username(nick_input)
    
    if rbx_info:
        official_name = rbx_info["name"]
        rbx_id = rbx_info["id"]
        formatted_profile = f"<a href='https://www.roblox.com/users/{rbx_id}/profile'>{official_name}</a> (ID: <code>{rbx_id}</code>)"
        confirm_text = f"Профиль найден: <a href='https://www.roblox.com/users/{rbx_id}/profile'><b>{official_name}</b></a> (ID: <code>{rbx_id}</code>)"
    else:
        official_name = nick_input
        formatted_profile = f"<code>{html.escape(nick_input)}</code> (⚠️ <i>профиль не найден в Roblox</i>)"
        confirm_text = f"<code>{html.escape(nick_input)}</code> (⚠️ <i>профиль не найден в Roblox</i>)"

    await state.update_data(a_nickname=official_name, a_profile=formatted_profile)
    await state.set_state(Form.appeal_place)
    
    await message.answer(
        f"{ICON_CHECK} Ник зафиксирован: {confirm_text}\n\n"
        f"{NUM_2} <b>На каком сервере произошла блокировка?</b>\n"
        "<i>Выберите нужный вариант на кнопках ниже:</i>",
        reply_markup=appeal_servers_kb(),
        parse_mode="HTML",
        link_preview_options=LinkPreviewOptions(is_disabled=True)
    )

@router.callback_query(F.data.startswith("app_srv_"), Form.appeal_place)
async def process_a_server_choice(call: CallbackQuery, state: FSMContext):
    srv_types = {
        "app_srv_secret": "Сервер с секреткой",
        "app_srv_saboteur": "Поиск саботера",
        "app_srv_farm": "Фарм сервер"
    }
    chosen_server = srv_types.get(call.data, "Не указан")
    await state.update_data(a_place=chosen_server)
    await state.set_state(Form.appeal_time)
    await call.message.edit_text(
        f"Выбран сервер: <b>{chosen_server}</b>\n\n"
        f"{NUM_3} {ICON_TIMER} <b>Укажите время публикации поста:</b>\n"
        "<i>(Укажите примерное или точное время публикации поста по МСК, например: <code>18:45</code> или <code>18:45:20 28.09</code>)</i>",
        parse_mode="HTML"
    )
    await call.answer()

@router.message(Form.appeal_time, F.text)
async def process_a_time(message: Message, state: FSMContext):
    await state.update_data(a_time=message.text.strip())
    await state.set_state(Form.appeal_reason)
    await message.answer(
        f"{NUM_4} <b>Почему мы должны снять с вас бан?</b>\n"
        "<i>(Подробно опишите вашу ситуацию и почему вы считаете блокировку ошибочной)</i>",
        parse_mode="HTML"
    )

@router.message(Form.appeal_reason, F.text)
async def process_a_reason(message: Message, state: FSMContext):
    await state.update_data(a_reason=message.text.strip())
    await state.set_state(Form.appeal_photos)
    await message.answer(
        f"{NUM_5} {ICON_PHOTO} <b>Прикрепите доказательства или скриншоты (до 5 фото):</b>\n"
        "<i>(Если у вас есть доказательства невиновности — отправьте скриншоты, либо нажмите кнопку ниже)</i>",
        reply_markup=skip_photo_kb("appeal"),
        parse_mode="HTML"
    )

@router.callback_query(F.data == "skip_photo_appeal", Form.appeal_photos)
async def skip_a_photos(call: CallbackQuery, state: FSMContext):
    async with creation_lock:
        if await check_active_ticket(call.message):
            await state.clear()
            await call.answer()
            return
        data = await state.get_data()
        profile_str = data.get("a_profile") or f"<code>{html.escape(data.get('a_nickname', ''))}</code>"
        text = (
            f"Ник: {profile_str}\n"
            f"Сервер бана: {html.escape(data.get('a_place', ''))}\n"
            f"Время публикации поста: {html.escape(data.get('a_time', ''))}\n"
            f"Причина разбана: {html.escape(data.get('a_reason', ''))}"
        )
        ticket_id = await dispatch_ticket_to_admin(call.from_user.id, call.from_user, "appeal", text, [])
        night_txt = get_night_notice()
        await call.message.edit_text(f"{ICON_CHECK} Обжалование №{ticket_id} отправлено!{night_txt}", reply_markup=user_cancel_kb(ticket_id), parse_mode="HTML")
        await state.clear()
        await call.answer()

@router.message(Form.appeal_photos, F.photo)
async def process_a_photo(message: Message, state: FSMContext):
    if await check_active_ticket(message):
        await state.clear()
        return

    photos = await collect_media_photos(message, state, "a_photos")
    if len(photos) > 5:
        await message.answer(f"{ICON_CROSS} Лимит превышен! Прикрепите не более 5 фото.", parse_mode="HTML")
        await state.clear()
        return

    mg_id = message.media_group_id
    if mg_id:
        await asyncio.sleep(1.2)
        async with creation_lock:
            if mg_id in processed_media_groups:
                return
            processed_media_groups.add(mg_id)
            if len(processed_media_groups) > 500:
                processed_media_groups.clear()

            cur_data = await state.get_data()
            photos = cur_data.get("a_photos", photos)

            if await check_active_ticket(message):
                await state.clear()
                return

            profile_str = cur_data.get("a_profile") or f"<code>{html.escape(cur_data.get('a_nickname', ''))}</code>"
            text = (
                f"Ник: {profile_str}\n"
                f"Сервер бана: {html.escape(cur_data.get('a_place', ''))}\n"
                f"Время публикации поста: {html.escape(data.get('a_time', ''))}\n"
                f"Причина разбана: {html.escape(data.get('a_reason', ''))}"
            )
            ticket_id = await dispatch_ticket_to_admin(message.from_user.id, message.from_user, "appeal", text, photos)
            await state.clear()
            night_txt = get_night_notice()
            await message.answer(f"{ICON_CHECK} Обжалование №{ticket_id} отправлено! (Прикреплено фото: {len(photos)}){night_txt}", reply_markup=user_cancel_kb(ticket_id), parse_mode="HTML")
            return

    async with creation_lock:
        if await check_active_ticket(message):
            await state.clear()
            return
        data = await state.get_data()
        profile_str = data.get("a_profile") or f"<code>{html.escape(data.get('a_nickname', ''))}</code>"
        text = (
            f"Ник: {profile_str}\n"
            f"Сервер бана: {html.escape(data.get('a_place', ''))}\n"
            f"Время публикации поста: {html.escape(data.get('a_time', ''))}\n"
            f"Причина разбана: {html.escape(data.get('a_reason', ''))}"
        )
        ticket_id = await dispatch_ticket_to_admin(message.from_user.id, message.from_user, "appeal", text, photos)
        await state.clear()
        night_txt = get_night_notice()
        await message.answer(f"{ICON_CHECK} Обжалование №{ticket_id} отправлено! (Прикреплено фото: {len(photos)}){night_txt}", reply_markup=user_cancel_kb(ticket_id), parse_mode="HTML")

@router.message(Form.question_content, F.photo | F.text)
async def process_question(message: Message, state: FSMContext):
    if await check_active_ticket(message):
        await state.clear()
        return

    mg_id = message.media_group_id

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

        if mg_id:
            await asyncio.sleep(1.2)
            async with creation_lock:
                if mg_id in processed_media_groups:
                    return
                processed_media_groups.add(mg_id)
                if len(processed_media_groups) > 500:
                    processed_media_groups.clear()

                cur_data = await state.get_data()
                photos = cur_data.get("q_photos", photos)
                caption_text = cur_data.get("q_text", caption_text)

                if await check_active_ticket(message):
                    await state.clear()
                    return

                ticket_id = await dispatch_ticket_to_admin(
                    message.from_user.id, message.from_user, "question", html.escape(caption_text), photos
                )
                await state.clear()
                night_txt = get_night_notice()
                await message.answer(f"{ICON_CHECK} Вопрос №{ticket_id} отправлен!{night_txt}", reply_markup=user_cancel_kb(ticket_id), parse_mode="HTML")
                return

        async with creation_lock:
            if await check_active_ticket(message):
                await state.clear()
                return
            ticket_id = await dispatch_ticket_to_admin(
                message.from_user.id, message.from_user, "question", html.escape(caption_text), photos
            )
            await state.clear()
            night_txt = get_night_notice()
            await message.answer(f"{ICON_CHECK} Вопрос №{ticket_id} отправлен!{night_txt}", reply_markup=user_cancel_kb(ticket_id), parse_mode="HTML")
            return
    else:
        async with creation_lock:
            if await check_active_ticket(message):
                await state.clear()
                return
            ticket_id = await dispatch_ticket_to_admin(
                message.from_user.id, message.from_user, "question", html.escape(message.text or ''), []
            )
            await state.clear()
            night_txt = get_night_notice()
            await message.answer(f"{ICON_CHECK} Вопрос №{ticket_id} отправлен!{night_txt}", reply_markup=user_cancel_kb(ticket_id), parse_mode="HTML")

@router.message(Form.friends_nickname)
async def process_friends_nickname(message: Message, state: FSMContext):
    async with creation_lock:
        if await check_active_ticket(message):
            await state.clear()
            return
        text = f"Ник: <code>{html.escape(message.text or '')}</code>"
        ticket_id = await dispatch_ticket_to_admin(message.from_user.id, message.from_user, "friends", text, [])
        night_txt = get_night_notice()
        await message.answer(f"{ICON_CHECK} Заявка №{ticket_id} создана!{night_txt}", reply_markup=user_cancel_kb(ticket_id), parse_mode="HTML")
        await state.clear()

@router.message(Form.helper_name)
async def process_helper_name(message: Message, state: FSMContext):
    await state.update_data(helper_name=message.text.strip())
    await state.set_state(Form.helper_age)
    await message.answer(f"{NUM_2} Ваш возраст:", parse_mode="HTML")

@router.message(Form.helper_age)
async def process_helper_age(message: Message, state: FSMContext):
    await state.update_data(helper_age=message.text.strip())
    await state.set_state(Form.helper_time)
    await message.answer(f"{NUM_3} Сколько времени вы готовы уделять на ответы в поддержке?", parse_mode="HTML")

@router.message(Form.helper_time)
async def process_helper_time(message: Message, state: FSMContext):
    await state.update_data(helper_time=message.text.strip())
    await state.set_state(Form.helper_why)
    await message.answer(f"{NUM_4} Почему вы хотите именно к нам?", parse_mode="HTML")

@router.message(Form.helper_why)
async def process_helper_why(message: Message, state: FSMContext):
    if not message.from_user.username:
        await message.answer(
            f"{ICON_WARN} <b>У вас не установлен @username в профиле Telegram!</b>\n\n"
            "Для отправки анкеты и связи с администрацией обязательно иметь юзернейм.\n"
            "Пожалуйста, установите его в настройках Telegram (Настройки ➔ Изменить профиль ➔ Имя пользователя) "
            "и затем <b>отправьте ответ на 4-й пункт ещё раз</b>.",
            parse_mode="HTML"
        )
        return

    why_text = message.text.strip()
    data = await state.get_data()
    user = message.from_user
    user_mention = get_user_mention(user)

    admin_text = (
        f"📝 <b>Новая заявка в команду поддержки (Хелперы)!</b>\n\n"
        f"👤 От: {user_mention}\n"
        f"🆔 ID: <code>{user.id}</code>\n"
        f"👤 Username: @{user.username}\n\n"
        f"1. <b>Имя:</b> {html.escape(data.get('helper_name', 'Не указано'))}\n"
        f"2. <b>Возраст:</b> {html.escape(data.get('helper_age', 'Не указан'))}\n"
        f"3. <b>Время на поддержку:</b> {html.escape(data.get('helper_time', 'Не указано'))}\n"
        f"4. <b>Почему к нам:</b> {html.escape(why_text)}"
    )

    sent = await bot.send_message(ADMIN_CHAT_ID, admin_text, reply_markup=helper_decision_kb(user.id), parse_mode="HTML")
    await map_message(sent.message_id, user.id, 0)
    kb = await main_keyboard(user.id)
    night_txt = get_night_notice()
    await message.answer(f"{ICON_CHECK} Ваша анкета успешно отправлена администрации! Ожидайте ответа.{night_txt}", reply_markup=kb, parse_mode="HTML")
    await state.clear()

@router.message(Form.secret_name)
async def process_secret_name(message: Message, state: FSMContext):
    await state.update_data(secret_name=message.text.strip())
    await state.set_state(Form.secret_age)
    await message.answer(f"{NUM_2} Ваш возраст:", parse_mode="HTML")

@router.message(Form.secret_age)
async def process_secret_age(message: Message, state: FSMContext):
    await state.update_data(secret_age=message.text.strip())
    await state.set_state(Form.secret_vip)
    await message.answer(f"{NUM_3} Есть ли у вас вип сервер в Tower Of Hell?", parse_mode="HTML")

@router.message(Form.secret_vip)
async def process_secret_vip(message: Message, state: FSMContext):
    await state.update_data(secret_vip=message.text.strip())
    await state.set_state(Form.secret_time)
    await message.answer(f"{NUM_4} Сколько вы готовы уделять времени поиску секреток?", parse_mode="HTML")

@router.message(Form.secret_time)
async def process_secret_time(message: Message, state: FSMContext):
    await state.update_data(secret_time=message.text.strip())
    await state.set_state(Form.secret_why)
    await message.answer(f"{NUM_5} Почему вы хотите именно к нам?", parse_mode="HTML")

@router.message(Form.secret_why)
async def process_secret_why(message: Message, state: FSMContext):
    if not message.from_user.username:
        await message.answer(
            f"{ICON_WARN} <b>У вас не установлен @username в профиле Telegram!</b>\n\n"
            "Для отправки анкеты и связи с администрацией обязательно иметь юзернейм.\n"
            "Пожалуйста, установите его в настройках Telegram (Настройки ➔ Изменить профиль ➔ Имя пользователя) "
            "и затем <b>отправьте ответ на 5-й пункт ещё раз</b>.",
            parse_mode="HTML"
        )
        return

    why_text = message.text.strip()
    data = await state.get_data()
    user = message.from_user
    user_mention = get_user_mention(user)

    admin_text = (
        f"🔍 <b>Новая заявка в искатели секреток!</b>\n\n"
        f"👤 От: {user_mention}\n"
        f"🆔 ID: <code>{user.id}</code>\n"
        f"👤 Username: @{user.username}\n\n"
        f"1. <b>Имя:</b> {html.escape(data.get('secret_name', 'Не указано'))}\n"
        f"2. <b>Возраст:</b> {html.escape(data.get('secret_age', 'Не указан'))}\n"
        f"3. <b>VIP сервер в ToH:</b> {html.escape(data.get('secret_vip', 'Не указано'))}\n"
        f"4. <b>Время на поиск:</b> {html.escape(data.get('secret_time', 'Не указано'))}\n"
        f"5. <b>Почему к нам:</b> {html.escape(why_text)}"
    )

    sent = await bot.send_message(ADMIN_CHAT_ID, admin_text, reply_markup=secret_decision_kb(user.id), parse_mode="HTML")
    await map_message(sent.message_id, user.id, 0)
    kb = await main_keyboard(user.id)
    night_txt = get_night_notice()
    await message.answer(f"{ICON_CHECK} Ваша заявка в искатели секреток успешно отправлена! Ожидайте ответа.{night_txt}", reply_markup=kb, parse_mode="HTML")
    await state.clear()

# ----------------------------------------------------------------------
# УНИВЕРСАЛЬНЫЙ ХЭНДЛЕР ЛИЧНЫХ СООБЩЕНИЙ
# ----------------------------------------------------------------------
@router.message(
    F.chat.type == "private",
    ~F.text.regexp(r"(?i)(жалоб|обжалован|вопрос|друг|хелпер|искател|секретк|перезагруз|админ-панел)")
)
async def user_private_message(message: Message, state: FSMContext):
    await register_user(message.from_user.id)
    if await is_banned(message.from_user.id): return

    current_state = await state.get_state()
    if current_state is not None:
        return

    active_ticket = await get_active_ticket(message.from_user.id)
    if active_ticket and active_ticket[2] == 'active':
        await touch_ticket(active_ticket[0], actor='user')
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
@router.message(Command("opentickets"), F.chat.id == ADMIN_CHAT_ID)
async def cmd_open_tickets(message: Message):
    if not await is_main_admin(message.from_user.id):
        await message.answer("❌ Только главные администраторы могут просматривать список открытых заявок!", parse_mode="HTML")
        return

    def _query():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT ticket_id, category, user_id,
                           EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - created_at))/60 as mins_passed
                    FROM tickets
                    WHERE status = 'pending'
                    ORDER BY ticket_id ASC
                """)
                return cur.fetchall()

    try:
        pending_list = await asyncio.to_thread(_query)
    except Exception as e:
        await message.answer(f"❌ Ошибка получения тикетов из БД: {e}")
        return

    if not pending_list:
        await message.answer("🟢 <b>Все заявки обработаны!</b> В данный момент нет открытых тикетов, ожидающих взятия.", parse_mode="HTML")
        return

    cat_names = {
        "complaint": "Жалоба",
        "appeal": "Обжалование",
        "question": "Вопрос",
        "friends": "Друзья"
    }

    text = f"📋 <b>Список неразобранных заявок (ожидают хелперов): {len(pending_list)}</b>\n\n"
    for t_id, cat, u_id, mins in pending_list:
        m = int(mins)
        time_str = f"{m // 60} ч. {m % 60} мин." if m >= 60 else f"{m} мин."
        cat_str = cat_names.get(cat, cat)
        text += f"• <b>Заявка №{t_id}</b> ({cat_str}) — ждет <code>{time_str}</code> | Игрок: <code>{u_id}</code>\n"

    await message.answer(text, parse_mode="HTML")

@router.message(Command("mystats"), F.chat.id == ADMIN_CHAT_ID)
async def cmd_my_stats(message: Message):
    if not await is_support_member(message.from_user.id) and not await is_main_admin(message.from_user.id):
        return

    admin_id = message.from_user.id
    agent_no = await get_or_create_agent_number(admin_id)

    def _query_mystats():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM tickets WHERE admin_id = %s AND status = 'closed'", (admin_id,))
                closed_count = cur.fetchone()[0]

                cur.execute("""
                    SELECT COALESCE(AVG(score), 0) 
                    FROM ratings 
                    WHERE admin_id = %s 
                      AND user_id != %s 
                      AND user_id NOT IN (SELECT admin_id FROM main_admins)
                      AND user_id NOT IN (SELECT admin_id FROM admin_agents)
                """, (admin_id, OWNER_ID))
                avg_score = cur.fetchone()[0]

                cur.execute("""
                    SELECT 
                        t.admin_id,
                        COUNT(t.ticket_id) AS cnt,
                        COALESCE(AVG(CASE 
                            WHEN r.user_id != %s 
                             AND r.user_id NOT IN (SELECT admin_id FROM main_admins) 
                             AND r.user_id NOT IN (SELECT admin_id FROM admin_agents)
                            THEN r.score 
                            ELSE NULL 
                        END), 0) AS avg_sc
                    FROM tickets t
                    LEFT JOIN ratings r ON t.ticket_id = r.ticket_id
                    WHERE t.admin_id IS NOT NULL AND t.status = 'closed'
                    GROUP BY t.admin_id
                    ORDER BY avg_sc DESC, cnt DESC
                """, (OWNER_ID,))
                
                ranking = cur.fetchall()
                rank = "Без места"
                for idx, (a_id, _, _) in enumerate(ranking, 1):
                    if a_id == admin_id:
                        rank = f"#{idx} из {len(ranking)}"
                        break
                        
                return closed_count, round(float(avg_score), 2), rank

    closed_cnt, avg_sc, user_rank = await asyncio.to_thread(_query_mystats)

    stats_text = (
        f"📊 <b>Ваша личная статистика агента:</b>\n\n"
        f"👤 <b>Агент:</b> {message.from_user.mention_html()} (Агент #{agent_no})\n"
        f"🔒 Закрыто тикетов: <code>{closed_cnt}</code>\n"
        f"⭐ Средняя оценка: <code>{avg_sc} / 5.0</code>\n"
        f"🏆 Место в рейтинге: <b>{user_rank}</b>"
    )
    await message.answer(stats_text, parse_mode="HTML")

@router.message(Command("secstats"), F.chat.id == ADMIN_CHAT_ID)
async def cmd_sec_stats(message: Message):
    user_id = message.from_user.id
    if not await is_secret_publisher(user_id) and not await is_main_admin(user_id):
        await message.answer("❌ У вас нет прав публикатора секреток!", parse_mode="HTML")
        return

    def _query_secstats():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM secret_posts WHERE user_id = %s", (user_id,))
                total_posts = cur.fetchone()[0]

                cur.execute("""
                    SELECT COUNT(*) FROM secret_posts 
                    WHERE user_id = %s AND created_at >= CURRENT_TIMESTAMP - INTERVAL '24 hours'
                """, (user_id,))
                posts_24h = cur.fetchone()[0]

                cur.execute("""
                    SELECT secret_type, COUNT(*) as cnt 
                    FROM secret_posts 
                    WHERE user_id = %s 
                    GROUP BY secret_type 
                    ORDER BY cnt DESC LIMIT 1
                """, (user_id,))
                fav_row = cur.fetchone()
                fav_type = f"{fav_row[0]} ({fav_row[1]} шт.)" if fav_row else "Нет данных"

                cur.execute("""
                    SELECT user_id, COUNT(*) as cnt 
                    FROM secret_posts 
                    GROUP BY user_id 
                    ORDER BY cnt DESC
                """)
                ranks = cur.fetchall()
                user_rank = "Без места"
                for idx, (p_id, _) in enumerate(ranks, 1):
                    if p_id == user_id:
                        user_rank = f"#{idx} из {len(ranks)}"
                        break

                return total_posts, posts_24h, fav_type, user_rank

    try:
        total_p, p_24, fav_t, rank_str = await asyncio.to_thread(_query_secstats)
    except Exception as e:
        await message.answer(f"❌ Ошибка получения статистики: {e}")
        return

    text = (
        f"🔮 <b>Ваша статистика публикатора секреток:</b>\n\n"
        f"👤 <b>Искатель:</b> {message.from_user.mention_html()}\n"
        f"📝 Всего опубликовано секреток: <code>{total_p}</code>\n"
        f"⏱ Публикаций за последние 24ч: <code>{p_24}</code>\n"
        f"🌟 Любимый тип секретки: <b>{fav_t}</b>\n"
        f"🏆 Место в топе искателей: <b>{rank_str}</b>"
    )
    await message.answer(text, parse_mode="HTML")

@router.message(Command("supptop"), F.chat.id == ADMIN_CHAT_ID)
async def cmd_supp_top(message: Message):
    if not await is_main_admin(message.from_user.id):
        await message.answer("❌ Только главные администраторы могут просматривать общий рейтинг поддержки!", parse_mode="HTML")
        return

    def _query():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT 
                        t.admin_id,
                        COUNT(t.ticket_id) AS cnt,
                        COALESCE(AVG(CASE 
                            WHEN r.user_id != %s 
                             AND r.user_id NOT IN (SELECT admin_id FROM main_admins) 
                             AND r.user_id NOT IN (SELECT admin_id FROM admin_agents)
                            THEN r.score 
                            ELSE NULL 
                        END), 0) AS avg_sc,
                        a.agent_number
                    FROM tickets t
                    LEFT JOIN ratings r ON t.ticket_id = r.ticket_id
                    LEFT JOIN admin_agents a ON t.admin_id = a.admin_id
                    WHERE t.admin_id IS NOT NULL AND t.status = 'closed'
                    GROUP BY t.admin_id, a.agent_number
                    ORDER BY avg_sc DESC, cnt DESC
                """, (OWNER_ID,))
                return cur.fetchall()

    try:
        top_list = await asyncio.to_thread(_query)
    except Exception as e:
        await message.answer(f"❌ Ошибка получения топа поддержки: {e}")
        return

    if not top_list:
        await message.answer("📭 В базе пока нет данных о закрытых тикетах агентов поддержки.", parse_mode="HTML")
        return

    medals = ["🥇", "🥈", "🥉"]
    text = "🏆 <b>Общий рейтинг команды поддержки:</b>\n\n"

    for idx, (adm_id, cnt, avg_sc, agent_no) in enumerate(top_list, 1):
        medal = medals[idx - 1] if idx <= 3 else f"<b>{idx}.</b>"
        agent_str = f" [Агент #{agent_no}]" if agent_no else ""
        try:
            chat_info = await bot.get_chat(adm_id)
            name = chat_info.full_name
        except Exception:
            name = f"ID: {adm_id}"

        avg_formatted = round(float(avg_sc), 2)
        text += (
            f"{medal} <b>{html.escape(name)}</b>{agent_str}\n"
            f"   ⭐ Оценка: <b>{avg_formatted} / 5.0</b> | 🔒 Закрыто тикетов: <code>{cnt}</code>\n\n"
        )

    await message.answer(text, parse_mode="HTML")

@router.message(Command("sectop"), F.chat.id == ADMIN_CHAT_ID)
async def cmd_sec_top(message: Message):
    if not await is_main_admin(message.from_user.id):
        await message.answer("❌ Только главные администраторы могут просматривать общий рейтинг искателей секреток!", parse_mode="HTML")
        return

    def _query():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT 
                        sp.user_id,
                        COUNT(sp.id) as total_cnt
                    FROM secret_posts sp
                    GROUP BY sp.user_id
                    ORDER BY total_cnt DESC
                """)
                rows = cur.fetchall()

                full_data = []
                for uid, t_cnt in rows:
                    cur.execute("""
                        SELECT secret_type, COUNT(*) as scnt 
                        FROM secret_posts 
                        WHERE user_id = %s 
                        GROUP BY secret_type 
                        ORDER BY scnt DESC LIMIT 1
                    """, (uid,))
                    fav_row = cur.fetchone()
                    fav_str = f"{fav_row[0]} ({fav_row[1]} шт.)" if fav_row else "—"
                    full_data.append((uid, t_cnt, fav_str))

                return full_data

    try:
        top_list = await asyncio.to_thread(_query)
    except Exception as e:
        await message.answer(f"❌ Ошибка получения топа секреток: {e}")
        return

    if not top_list:
        await message.answer("📭 В базе пока нет опубликованных секреток.", parse_mode="HTML")
        return

    medals = ["🥇", "🥈", "🥉"]
    text = "🔮 <b>Общий рейтинг искателей секреток (за всё время):</b>\n\n"

    for idx, (uid, total_cnt, fav_type) in enumerate(top_list, 1):
        medal = medals[idx - 1] if idx <= 3 else f"<b>{idx}.</b>"
        try:
            chat_info = await bot.get_chat(uid)
            name = chat_info.full_name
        except Exception:
            name = f"ID: {uid}"

        text += (
            f"{medal} <b>{html.escape(name)}</b> (<code>{uid}</code>)\n"
            f"   📝 Всего постов: <code>{total_cnt}</code>\n"
            f"   🌟 Любимый тип: <i>{html.escape(fav_type)}</i>\n\n"
        )

    await message.answer(text, parse_mode="HTML")

@router.message(Command("note"), F.chat.id == ADMIN_CHAT_ID)
async def cmd_admin_note(message: Message):
    if not await is_support_member(message.from_user.id) and not await is_main_admin(message.from_user.id):
        return

    if not message.reply_to_message:
        await message.answer(
            "⚠️ <b>Как использовать команду:</b>\nОтветьте (Reply) на карточку заявки или сообщение игрока командой:\n<code>/note Текст заметки</code>",
            parse_mode="HTML"
        )
        return

    args = message.text.split(maxsplit=1)
    if len(args) < 2 or not args[1].strip():
        await message.answer(
            "⚠️ <b>Вы не указали текст заметки!</b>\nНапишите текст через пробел после команды, например:\n<code>/note Часто ускоряет таймер</code>",
            parse_mode="HTML"
        )
        return

    user_id = None
    ticket_id = None
    replied_msg = message.reply_to_message

    mapping = await get_user_by_group_msg(replied_msg.message_id)
    if mapping:
        user_id, ticket_id = mapping[0], mapping[1]

    raw_text = replied_msg.text or replied_msg.caption or ""
    if not user_id and raw_text:
        match_id = re.search(r"ID:?\s*(\d+)", raw_text, re.IGNORECASE)
        if match_id:
            user_id = int(match_id.group(1))

    if not ticket_id and raw_text:
        match_t = re.search(r"№(\d+)", raw_text)
        if match_t:
            t_id = int(match_t.group(1))
            t_info = await get_ticket_info(t_id)
            if t_info:
                ticket_id = t_id
                if not user_id:
                    user_id = t_info[0]

    if not user_id:
        await message.answer("❌ <b>Не удалось определить игрока!</b> Ответьте именно на карточку тикета (где есть ID) или на сообщение игрока.", parse_mode="HTML")
        return

    agent_no = await get_or_create_agent_number(message.from_user.id)
    author_str = f"Агент #{agent_no}"
    note_content = args[1].strip()

    try:
        note_id = await add_user_note(user_id, note_content, author_str)
    except Exception as e:
        logging.error(f"Ошибка сохранения заметки: {e}")
        await message.answer(f"❌ Ошибка сохранения в базе данных: {e}")
        return

    try:
        await message.react([{"type": "emoji", "emoji": "✍"}])
    except Exception:
        pass

    ticket_str = f" по заявке №{ticket_id}" if ticket_id else ""
    confirm_text = (
        f"📌 <b>Постоянная заметка [#{note_id}] сохранена{ticket_str}!</b>\n\n"
        f"👤 <b>Пользователь:</b> <code>{user_id}</code>\n"
        f"✍️ <b>Автор:</b> {message.from_user.mention_html()} ({author_str})\n"
        f"💬 <b>Заметка:</b> <i>{html.escape(note_content)}</i>\n\n"
        f"🔒 <i>Заметка закреплена и будет автоматически выводиться во всех будущих заявках этого игрока.</i>"
    )
    sent = await message.answer(confirm_text, parse_mode="HTML")
    if ticket_id:
        await map_message(sent.message_id, user_id, ticket_id)

@router.message(Command("deletenote"), F.chat.id == ADMIN_CHAT_ID)
async def cmd_delete_note(message: Message):
    if not await is_main_admin(message.from_user.id):
        await message.answer("❌ Только главные администраторы могут удалять заметки!", parse_mode="HTML")
        return

    user_id = None
    if message.reply_to_message:
        replied_msg = message.reply_to_message
        mapping = await get_user_by_group_msg(replied_msg.message_id)
        if mapping:
            user_id = mapping[0]

        raw_text = replied_msg.text or replied_msg.caption or ""
        if not user_id and raw_text:
            match_id = re.search(r"ID:?\s*(\d+)", raw_text, re.IGNORECASE)
            if match_id:
                user_id = int(match_id.group(1))

    if not user_id:
        await message.answer("❌ Не удалось определить пользователя. Ответьте этой командой (Reply) на тикет или сообщение игрока!", parse_mode="HTML")
        return

    user_notes = await get_user_notes(user_id)
    if not user_notes:
        await message.answer(f"📭 У пользователя <code>{user_id}</code> нет постоянных заметок.", parse_mode="HTML")
        return

    args = message.text.split(maxsplit=1)
    if len(args) < 2 or not args[1].strip().isdigit():
        lines = [f"• <b>[#{n_id}]</b> <i>{html.escape(n_txt)}</i>" for n_id, n_txt, _ in user_notes]
        notes_str = "\n".join(lines)
        await message.answer(
            f"ℹ️ <b>Укажите номер заметки для удаления!</b>\n\n"
            f"Список заметок пользователя <code>{user_id}</code>:\n{notes_str}\n\n"
            f"Использование: <code>/deletenote [номер]</code> (например: <code>/deletenote {user_notes[0][0]}</code>)",
            parse_mode="HTML"
        )
        return

    target_note_id = int(args[1].strip())
    success = await delete_user_note_by_id(target_note_id, user_id)
    if success:
        await message.answer(f"✅ Заметка <b>[#{target_note_id}]</b> по пользователю <code>{user_id}</code> успешно удалена!", parse_mode="HTML")
    else:
        await message.answer(f"❌ Заметка <b>[#{target_note_id}]</b> не найдена у пользователя <code>{user_id}</code>.", parse_mode="HTML")

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
# ОТВЕТ ХЕЛПЕРА В ГРУППЕ (ОТКАЗ И ДИАЛОГ)
# ----------------------------------------------------------------------
@router.message(F.chat.id == ADMIN_CHAT_ID, F.reply_to_message)
async def admin_reply_in_group(message: Message):
    if message.text and message.text.startswith("/"):
        return

    replied_msg_id = message.reply_to_message.message_id
    pending_data = await get_pending_rejection(replied_msg_id)

    if pending_data:
        pending_ticket_id, card_message_id, saved_card_text = pending_data
        ticket_info = await get_ticket_info(pending_ticket_id)
        if ticket_info:
            reason = html.escape(message.text or message.caption or "Без причины")
            agent_no = await get_or_create_agent_number(message.from_user.id)
            await close_ticket_db(pending_ticket_id, 'rejected', admin_id=message.from_user.id)
            
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
            
            if card_message_id:
                base = saved_card_text or ticket_info[3] or f"Заявка <b>№{pending_ticket_id}</b>"
                new_text = (
                    f"{base}\n\n"
                    f"🔴 <b>Заявка №{pending_ticket_id} отклонена</b> "
                    f"администратором {message.from_user.mention_html()} (Агент #{agent_no}).\n"
                    f"<b>Причина:</b> {reason}"
                )
                
                try:
                    await bot.edit_message_text(
                        chat_id=ADMIN_CHAT_ID,
                        message_id=card_message_id,
                        text=new_text,
                        reply_markup=None,
                        parse_mode="HTML"
                    )
                except TelegramBadRequest:
                    try:
                        await bot.edit_message_caption(
                            chat_id=ADMIN_CHAT_ID,
                            message_id=card_message_id,
                            caption=new_text,
                            reply_markup=None,
                            parse_mode="HTML"
                        )
                    except Exception as e:
                        logging.error(f"Не удалось обновить подпись карточки: {e}")
                except Exception as e:
                    logging.error(f"Не удалось обновить текст карточки: {e}")

        try:
            await bot.delete_message(chat_id=ADMIN_CHAT_ID, message_id=replied_msg_id)
        except Exception:
            pass

        await delete_pending_rejection(replied_msg_id)
        await message.answer(f"✅ Отказ по заявке №{pending_ticket_id} отправлен.")
        return

    mapping = await get_user_by_group_msg(replied_msg_id)
    if mapping:
        user_id, ticket_id = mapping[0], mapping[1]
        ticket_info = await get_ticket_info(ticket_id)

        if ticket_info and ticket_info[2] == 'active':
            if ticket_info[1] != message.from_user.id and not await is_main_admin(message.from_user.id):
                await message.answer("❌ Этот тикет ведет другой администратор. Вы не можете в него отвечать!")
                return

            await touch_ticket(ticket_id, actor='admin')
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
# ФОНОВЫЕ ВОРКЕРЫ
# ----------------------------------------------------------------------
async def secret_timer_worker():
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
    while True:
        try:
            def _check():
                with get_db() as conn:
                    with conn.cursor() as cur:
                        cur.execute("""
                            SELECT ticket_id, category, user_id, 
                                   EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - created_at))/3600 as hours_total
                            FROM tickets
                            WHERE status = 'pending' 
                              AND COALESCE(last_unassigned_remind, created_at) <= CURRENT_TIMESTAMP - INTERVAL '2 hours'
                        """)
                        unassigned = cur.fetchall()

                        cur.execute("""
                            SELECT t.ticket_id, t.admin_id, t.user_id,
                                   EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - t.updated_at))/60 as mins_passed,
                                   a.agent_number, t.last_actor, t.reminded_idle
                            FROM tickets t
                            LEFT JOIN admin_agents a ON t.admin_id = a.admin_id
                            WHERE t.status = 'active' 
                              AND (
                                  (t.reminded_idle = FALSE AND t.updated_at <= CURRENT_TIMESTAMP - INTERVAL '20 minutes')
                                  OR
                                  (t.reminded_idle = TRUE AND t.last_idle_remind IS NOT NULL AND t.last_idle_remind <= CURRENT_TIMESTAMP - INTERVAL '1 hour')
                              )
                        """)
                        idle = cur.fetchall()

                        cur.execute("""
                            SELECT ticket_id, user_id, admin_id, card_text
                            FROM tickets
                            WHERE status = 'active' 
                              AND last_actor = 'admin'
                              AND updated_at <= CURRENT_TIMESTAMP - INTERVAL '24 hours'
                        """)
                        expired_active = cur.fetchall()

                        return unassigned, idle, expired_active

            unassigned_tickets, idle_tickets, expired_active = await asyncio.to_thread(_check)

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
                    def _update_last_remind():
                        with get_db() as conn:
                            with conn.cursor() as cur:
                                cur.execute("UPDATE tickets SET last_unassigned_remind = CURRENT_TIMESTAMP WHERE ticket_id = %s", (t_id,))
                    await asyncio.to_thread(_update_last_remind)
                except Exception as e:
                    logging.error(f"Ошибка напоминания: {e}")

            for t_id, adm_id, u_id, mins, agent_no, last_act, was_reminded in idle_tickets:
                if last_act == 'admin':
                    client_alert = (
                        f"⏳ <b>Напоминание по заявке №{t_id}</b>\n\n"
                        "Поддержка ожидает вашего ответа. Если вопрос ещё актуален, пожалуйста, отправьте сообщение в этот чат.\n"
                        "<i>Обратите внимание: при отсутствии активности заявка может быть автоматически закрыта!</i>"
                    )
                    try:
                        await bot.send_message(u_id, client_alert, parse_mode="HTML")
                        def _mark_user_reminded():
                            with get_db() as conn:
                                with conn.cursor() as cur:
                                    cur.execute("""
                                        UPDATE tickets 
                                        SET reminded_idle = TRUE, last_idle_remind = CURRENT_TIMESTAMP 
                                        WHERE ticket_id = %s
                                    """, (t_id,))
                        await asyncio.to_thread(_mark_user_reminded)
                    except Exception:
                        pass
                else:
                    agent_str = f"Агент #{agent_no}" if agent_no else f"ID {adm_id}"
                    admin_mention = agent_str
                    try:
                        chat_member = await bot.get_chat(adm_id)
                        admin_mention = f'<a href="tg://user?id={adm_id}">{html.escape(chat_member.full_name)}</a> ({agent_str})'
                    except Exception:
                        pass

                    repeat_prefix = "Повторное напоминание" if was_reminded else "Напоминание"
                    admin_alert = (
                        f"⏳ <b>{repeat_prefix} по активному тикету!</b>\n\n"
                        f"Тикет <b>№{t_id}</b> находится в работе у {admin_mention}.\n"
                        f"Пользователь ожидает вашего ответа (простой: <b>{int(mins)} минут</b>).\n"
                        f"Пожалуйста, ответьте игроку или закройте заявку!"
                    )
                    try:
                        sent = await bot.send_message(ADMIN_CHAT_ID, admin_alert, parse_mode="HTML")
                        await map_message(sent.message_id, u_id, t_id)
                        def _mark_admin_reminded():
                            with get_db() as conn:
                                with conn.cursor() as cur:
                                    cur.execute("""
                                        UPDATE tickets 
                                        SET reminded_idle = TRUE, last_idle_remind = CURRENT_TIMESTAMP 
                                        WHERE ticket_id = %s
                                    """, (t_id,))
                        await asyncio.to_thread(_mark_admin_reminded)
                    except Exception:
                        pass

            for t_id, u_id, adm_id, c_txt in expired_active:
                try:
                    await close_ticket_db(t_id, status='closed', admin_id=adm_id)
                    await bot.send_message(
                        u_id,
                        f"{ICON_LOCK} Ваша заявка <b>№{t_id}</b> автоматически закрыта в связи с отсутствием активности в течение 24 часов.\n"
                        "Если у вас остались вопросы, вы всегда можете открыть новый тикет через меню!",
                        parse_mode="HTML",
                        reply_markup=rating_kb(t_id)
                    )
                    await bot.send_message(
                        ADMIN_CHAT_ID,
                        f"⏱ <b>Тикет №{t_id} автоматически закрыт</b> (пользователь не отвечал более 24 часов).",
                        parse_mode="HTML"
                    )
                except Exception as ex:
                    logging.error(f"Ошибка автозакрытия: {ex}")

        except Exception as err:
            logging.error(f"Ошибка в reminder_worker: {err}")

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

async def main():
    await web_server()
    await setup_bot_commands(bot)
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
