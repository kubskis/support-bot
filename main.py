import asyncio
import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Message,
)
from fastapi import FastAPI
import uvicorn

# Токен и ID главного администратора из переменных окружения
TOKEN = os.getenv("BOT_TOKEN")
SUPER_ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

# Включаем логирование
logging.basicConfig(level=logging.INFO)
router = Router()

# Часовой пояс МСК (UTC+3)
MSK_TZ = timezone(timedelta(hours=3))

# ==================== БАЗА ДАННЫХ (SQLite) ====================
def init_db():
    conn = sqlite3.connect("bot.db")
    cursor = conn.cursor()
    
    # Таблица привязанных каналов
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS channels (
            user_id INTEGER PRIMARY KEY,
            channel_id TEXT
        )
    """)
    
    # Таблица постов
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            channel_id TEXT,
            message_id INTEGER,
            secret_type TEXT,
            base_text TEXT,
            is_expired BOOLEAN DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # Таблица типов секреток
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS secret_types (
            name TEXT PRIMARY KEY,
            declined TEXT
        )
    """)
    
    # Таблица настроек и шаблонов
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    
    # Таблица дополнительных администраторов (полных)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS admins (
            user_id INTEGER PRIMARY KEY,
            username TEXT
        )
    """)

    # Новая таблица: Админы секреток (те, кто может искать и публиковать секретки)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS secret_admins (
            user_id INTEGER PRIMARY KEY,
            username TEXT
        )
    """)
    
    default_types = [
        ("Лапка", "лапки"),
        ("Сердечко", "сердечка"),
        ("Тропы", "троп")
    ]
    cursor.executemany("INSERT OR IGNORE INTO secret_types (name, declined) VALUES (?, ?)", default_types)
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('timer_seconds', '510')")
    
    # Дефолтный шаблон активного поста
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
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('template_active', ?)", (default_active_template,))

    # Дефолтный шаблон истекшего поста
    default_expired_template = (
        "❕Секретка❕\n"
        "Секретка: [Тип_Секретки]\n\n"
        "Секретка: Время вышло! В канале еще будут секретки и вы успеете попасть на них🤍\n\n"
        "🤍Наш <a href='https://t.me/SecretsToH'>чат</a> | "
        "Наш <a href='https://t.me/ToHSecretss'>канал</a> | "
        "Наш <a href='https://t.me/ToHSecrets_bot'>бот</a>🤍"
    )
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('template_expired', ?)", (default_expired_template,))
    
    conn.commit()
    conn.close()

init_db()

def is_admin(user_id: int) -> bool:
    if user_id == SUPER_ADMIN_ID:
        return True
    conn = sqlite3.connect("bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM admins WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row is not None

def is_secret_admin(user_id: int) -> bool:
    if is_admin(user_id):
        return True
    conn = sqlite3.connect("bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM secret_admins WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row is not None

def get_timer_duration() -> int:
    conn = sqlite3.connect("bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM settings WHERE key = 'timer_seconds'")
    row = cursor.fetchone()
    conn.close()
    return int(row[0]) if row else 510

def get_secret_types_dict() -> dict:
    conn = sqlite3.connect("bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT name, declined FROM secret_types")
    rows = cursor.fetchall()
    conn.close()
    return {row[0]: row[1] for row in rows}

def get_template(key: str) -> str:
    conn = sqlite3.connect("bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else ""

# Состояния FSM
class SecretForm(StatesGroup):
    waiting_for_type = State()
    waiting_for_photo = State()
    waiting_for_link = State()
    waiting_for_confirm = State()

class AdminStates(StatesGroup):
    waiting_for_new_type_name = State()
    waiting_for_new_type_declined = State()
    waiting_for_new_timer = State()
    waiting_for_admin_input = State()
    waiting_for_secret_admin_input = State()
    waiting_for_template_active = State()
    waiting_for_template_expired = State()

# Клавиатуры
def get_main_reply_keyboard(user_id: int):
    keyboard = []
    if is_secret_admin(user_id):
        keyboard.append([KeyboardButton(text="📥 Отправить секретку")])
        keyboard.append([KeyboardButton(text="📋 Мои посты")])
    
    if is_admin(user_id):
        keyboard.append([KeyboardButton(text="⚙️ Админ-панель")])
    
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True) if keyboard else None

# Главное меню админки (теперь с кнопкой перехода в подменю секреток)
def get_admin_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📊 Статистика за день", callback_data="adm_stats")],
            [InlineKeyboardButton(text="👥 Управление админами", callback_data="adm_manage_admins")],
            [InlineKeyboardButton(text="🔑 Админы секреток", callback_data="adm_manage_secret_admins")],
            [InlineKeyboardButton(text="⚙️ Управление секретками (Меню)", callback_data="open_secrets_panel")],
            [InlineKeyboardButton(text="🔙 Выход", callback_data="adm_exit")],
        ]
    )

# Подменю управления секретками
def get_secrets_admin_keyboard(timer_str: str):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"⏱ Таймер удаления: {timer_str}", callback_data="noop")],
            [InlineKeyboardButton(text="📝 Редактировать шаблоны", callback_data="adm_edit_templates")],
            [InlineKeyboardButton(text="➕ Добавить тип секретки", callback_data="adm_add_type")],
            [InlineKeyboardButton(text="🗑 Удалить тип секретки", callback_data="adm_del_type")],
            [InlineKeyboardButton(text="⏱ Изменить время таймера", callback_data="adm_set_timer")],
            [InlineKeyboardButton(text="🔙 Назад в админку", callback_data="adm_back_to_main")],
        ]
    )

def get_templates_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Шаблон активного поста", callback_data="tmpl_active")],
            [InlineKeyboardButton(text="✏️ Шаблон истекшего поста", callback_data="tmpl_expired")],
            [InlineKeyboardButton(text="🔙 Назад в меню секреток", callback_data="open_secrets_panel")],
        ]
    )

def get_types_keyboard():
    types_dict = get_secret_types_dict()
    buttons = []
    for s_name in types_dict.keys():
        buttons.append([InlineKeyboardButton(text=f"🔹 {s_name}", callback_data=f"type_{s_name}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_confirm_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Да", callback_data="confirm_yes"),
                InlineKeyboardButton(text="❌ Нет", callback_data="confirm_no"),
            ]
        ]
    )

async def expire_post(bot: Bot, channel_id: str, message_id: int, secret_type: str, post_db_id: int):
    conn = sqlite3.connect("bot.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE posts SET is_expired = 1 WHERE id = ?", (post_db_id,))
    conn.commit()
    conn.close()

    template = get_template("template_expired")
    expired_text = template.replace("[Тип_Секретки]", secret_type)

    try:
        await bot.edit_message_caption(
            chat_id=channel_id,
            message_id=message_id,
            caption=expired_text,
            parse_mode="HTML",
        )
    except Exception as e:
        logging.error(f"Не удалось обновить пост: {e}")

async def timer_task(bot: Bot, channel_id: str, message_id: int, secret_type: str, post_db_id: int):
    duration = get_timer_duration()
    await asyncio.sleep(duration)
    
    conn = sqlite3.connect("bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT is_expired FROM posts WHERE id = ?", (post_db_id,))
    row = cursor.fetchone()
    conn.close()

    if row and row[0] == 0:
        await expire_post(bot, channel_id, message_id, secret_type, post_db_id)

async def check_user_and_bot_rights(bot: Bot, user_id: int) -> str | None:
    conn = sqlite3.connect("bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT channel_id FROM channels WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()

    if not row:
        return None

    channel_id = row[0]

    try:
        bot_member = await bot.get_chat_member(chat_id=channel_id, user_id=bot.id)
        if bot_member.status not in ["administrator", "creator"]:
            return None

        user_member = await bot.get_chat_member(chat_id=channel_id, user_id=user_id)
        if user_member.status not in ["administrator", "creator"]:
            conn = sqlite3.connect("bot.db")
            cursor = conn.cursor()
            cursor.execute("DELETE FROM channels WHERE user_id = ?", (user_id,))
            conn.commit()
            conn.close()
            return None

        return channel_id
    except Exception:
        return None


# ==================== ОБРАБОТЧИКИ КОМАНД ====================

@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    welcome_text = (
        "Привет! Добро пожаловать в бота.\n"
        "Для начала работы привяжи свой канал с помощью команды `/addchannel @юз_канала`"
    )
    await message.answer(welcome_text, reply_markup=get_main_reply_keyboard(message.from_user.id))


@router.message(Command("addchannel"))
async def cmd_add_channel(message: Message, bot: Bot):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("❌ Укажи канал после команды.\nПример: `/addchannel @my_channel`", parse_mode="Markdown")
        return

    channel_input = args[1].strip()
    user_id = message.from_user.id

    try:
        bot_member = await bot.get_chat_member(chat_id=channel_input, user_id=bot.id)
        if bot_member.status not in ["administrator", "creator"]:
            await message.answer("❌ Бот не является администратором в этом канале! Добавь его с правом публикации сообщений.")
            return

        user_member = await bot.get_chat_member(chat_id=channel_input, user_id=user_id)
        if user_member.status not in ["administrator", "creator"]:
            await message.answer("❌ Ты не являешься администратором или создателем этого канала, поэтому не можешь его привязать!")
            return

        conn = sqlite3.connect("bot.db")
        cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO channels (user_id, channel_id) VALUES (?, ?)", 
                       (user_id, channel_input))
        conn.commit()
        conn.close()

        await message.answer(
            f"✅ Канал <b>{channel_input}</b> успешно привязан!\nТеперь тебе доступно меню снизу.",
            parse_mode="HTML",
            reply_markup=get_main_reply_keyboard(user_id)
        )

    except TelegramBadRequest:
        await message.answer("❌ Не удалось найти канал. Убедись, что бот добавлен в канал и указан правильный юзернейм.")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


# ==================== АДМИН-ПАНЕЛЬ И ПОДМЕНЮ ====================

@router.message(F.text == "⚙️ Админ-панель")
async def admin_panel(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    text = "⚙️ <b>Панель управления администратора</b>\n\nВыберите нужный раздел:"
    await message.answer(text, parse_mode="HTML", reply_markup=get_admin_keyboard())


# Переход в подменю управления секретками
@router.callback_query(F.data == "open_secrets_panel")
async def open_secrets_panel(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    current_timer = get_timer_duration()
    minutes = current_timer // 60
    seconds = current_timer % 60
    timer_str = f"{minutes} мин. {seconds} сек."

    text = (
        f"⚙️ <b>Управление секретками</b>\n\n"
        f"⏱ Текущий таймер: <b>{timer_str}</b>\n"
        f"Выберите настройку:"
    )
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=get_secrets_admin_keyboard(timer_str))
    await callback.answer()


# Возврат назад в главное меню админки
@router.callback_query(F.data == "adm_back_to_main")
async def adm_back_to_main(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    text = "⚙️ <b>Панель управления администратора</b>\n\nВыберите нужный раздел:"
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=get_admin_keyboard())
    await callback.answer()


@router.callback_query(F.data == "adm_stats")
async def adm_stats(callback: CallbackQuery, bot: Bot):
    if not is_admin(callback.from_user.id):
        return

    now_msk = datetime.now(MSK_TZ)
    yesterday_msk = now_msk - timedelta(days=1)
    since_str = yesterday_msk.strftime('%Y-%m-%d %H:%M:%S')
    
    conn = sqlite3.connect("bot.db")
    cursor = conn.cursor()
    cursor.execute("""
        SELECT user_id, COUNT(*) as post_count 
        FROM posts 
        WHERE created_at >= ? 
        GROUP BY user_id
    """, (since_str,))
    user_stats = cursor.fetchall()

    cursor.execute("""
        SELECT user_id, channel_id, message_id, secret_type, created_at 
        FROM posts 
        WHERE created_at >= ? 
        ORDER BY created_at DESC
    """, (since_str,))
    all_posts = cursor.fetchall()
    conn.close()

    if not user_stats:
        await callback.message.edit_text(
            "📊 <b>Статистика за последние 24 часа (МСК):</b>\n\n📭 За это время не было опубликовано ни одной секретки.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="adm_back_to_main")]])
        )
        await callback.answer()
        return

    stats_text = "📊 <b>Статистика постов за последние 24 часа (МСК):</b>\n\n"
    
    stats_text += "<b>👤 По количеству от администраторов:</b>\n"
    for u_id, count in user_stats:
        admin_name = f"ID {u_id}"
        try:
            chat_info = await bot.get_chat(u_id)
            if chat_info.username:
                admin_name = f"@{chat_info.username}"
            elif chat_info.first_name:
                admin_name = chat_info.first_name
        except Exception:
            pass
        stats_text += f"• {admin_name} (<code>{u_id}</code>): <b>{count}</b> пост(ов)\n"
    
    stats_text += "\n<b>📋 Список всех постов:</b>\n"
    for u_id, ch_id, msg_id, s_type, dt in all_posts:
        clean_ch = ch_id.replace("@", "")
        if clean_ch.startswith("https://t.me/"):
            post_link = f"{clean_ch}/{msg_id}"
        else:
            post_link = f"https://t.me/{clean_ch}/{msg_id}"
            
        admin_name = f"ID {u_id}"
        try:
            chat_info = await bot.get_chat(u_id)
            if chat_info.username:
                admin_name = f"@{chat_info.username}"
        except Exception:
            pass
            
        stats_text += f"• [{s_type}] {admin_name} (<code>{u_id}</code>) — <a href='{post_link}'>Открыть пост</a> ({dt})\n"

    if len(stats_text) > 4000:
        stats_text = stats_text[:3950] + "\n\n... (список сокращен из-за лимита длины)"

    await callback.message.edit_text(
        stats_text,
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="adm_back_to_main")]])
    )
    await callback.answer()


@router.callback_query(F.data == "adm_manage_admins")
async def adm_manage_admins(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != SUPER_ADMIN_ID:
        await callback.answer("❌ Только главный админ может управлять списком администраторов.", show_alert=True)
        return

    conn = sqlite3.connect("bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, username FROM admins")
    admins = cursor.fetchall()
    conn.close()

    text = "👥 <b>Управление администраторами</b>\n\nТекущие доп. админы:\n"
    buttons = []
    if admins:
        for a_id, a_username in admins:
            name_display = f"@{a_username}" if a_username else f"ID {a_id}"
            text += f"• {name_display} (<code>{a_id}</code>)\n"
            buttons.append([InlineKeyboardButton(text=f"🗑 Удалить админа: {name_display}", callback_data=f"deladmin_{a_id}")])
    else:
        text += "<i>Список пуст.</i>\n"

    text += "\nНажми кнопку ниже, чтобы добавить нового администратора."
    buttons.append([InlineKeyboardButton(text="➕ Добавить администратора", callback_data="add_admin_prompt")])
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="adm_back_to_main")])

    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()


@router.callback_query(F.data == "add_admin_prompt")
async def add_admin_prompt(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != SUPER_ADMIN_ID:
        return
    await callback.message.edit_text(
        "➕ Введите <b>Telegram ID</b> или <b>юзернейм (@username)</b> нового администратора:",
        parse_mode="HTML"
    )
    await state.set_state(AdminStates.waiting_for_admin_input)
    await callback.answer()


@router.message(AdminStates.waiting_for_admin_input, F.text)
async def process_new_admin(message: Message, state: FSMContext, bot: Bot):
    if message.from_user.id != SUPER_ADMIN_ID:
        return

    raw_input = message.text.strip()
    target_id = None
    target_username = None

    if raw_input.isdigit():
        target_id = int(raw_input)
        try:
            chat_info = await bot.get_chat(target_id)
            target_username = chat_info.username
        except Exception:
            pass
    elif raw_input.startswith("@"):
        username_clean = raw_input[1:]
        try:
            chat_info = await bot.get_chat(raw_input)
            target_id = chat_info.id
            target_username = username_clean
        except Exception:
            await message.answer("❌ Не удалось найти пользователя по такому юзернейму. Убедитесь, что он хоть раз запускал бота.")
            return
    else:
        await message.answer("❌ Неверный формат. Введите ID (цифры) или юзернейм (начинающийся с @). Попробуйте снова:")
        return

    conn = sqlite3.connect("bot.db")
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO admins (user_id, username) VALUES (?, ?)", (target_id, target_username))
    conn.commit()
    conn.close()

    await state.clear()
    await message.answer(f"✅ Пользователь с ID <code>{target_id}</code> успешно добавлен в список администраторов!", parse_mode="HTML", reply_markup=get_main_reply_keyboard(message.from_user.id))


@router.callback_query(F.data.startswith("deladmin_"))
async def process_delete_admin(callback: CallbackQuery):
    if callback.from_user.id != SUPER_ADMIN_ID:
        return
    admin_to_del = int(callback.data.split("_")[1])

    conn = sqlite3.connect("bot.db")
    cursor = conn.cursor()
    cursor.execute("DELETE FROM admins WHERE user_id = ?", (admin_to_del,))
    conn.commit()
    conn.close()

    await callback.message.edit_text(f"✅ Администратор с ID <code>{admin_to_del}</code> удален.", parse_mode="HTML")
    await callback.answer()


# ==================== УПРАВЛЕНИЕ АДМИНАМИ СЕКРЕТОК ====================

@router.callback_query(F.data == "adm_manage_secret_admins")
async def adm_manage_secret_admins(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return

    conn = sqlite3.connect("bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, username FROM secret_admins")
    secret_admins = cursor.fetchall()
    conn.close()

    text = "🔑 <b>Управление админами секреток</b>\n\nТекущие пользователи, которые могут публиковать секретки:\n"
    buttons = []
    if secret_admins:
        for a_id, a_username in secret_admins:
            name_display = f"@{a_username}" if a_username else f"ID {a_id}"
            text += f"• {name_display} (<code>{a_id}</code>)\n"
            buttons.append([InlineKeyboardButton(text=f"🗑 Удалить: {name_display}", callback_data=f"delsecadmin_{a_id}")])
    else:
        text += "<i>Список пуст.</i>\n"

    text += "\nНажми кнопку ниже, чтобы добавить нового админа секреток."
    buttons.append([InlineKeyboardButton(text="➕ Добавить админа секреток", callback_data="add_secret_admin_prompt")])
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="adm_back_to_main")])

    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()


@router.callback_query(F.data == "add_secret_admin_prompt")
async def add_secret_admin_prompt(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    await callback.message.edit_text(
        "🔑 Введите <b>Telegram ID</b> или <b>юзернейм (@username)</b> пользователя, которому нужно дать доступ на публикацию секреток:",
        parse_mode="HTML"
    )
    await state.set_state(AdminStates.waiting_for_secret_admin_input)
    await callback.answer()


@router.message(AdminStates.waiting_for_secret_admin_input, F.text)
async def process_new_secret_admin(message: Message, state: FSMContext, bot: Bot):
    if not is_admin(message.from_user.id):
        return

    raw_input = message.text.strip()
    target_id = None
    target_username = None

    if raw_input.isdigit():
        target_id = int(raw_input)
        try:
            chat_info = await bot.get_chat(target_id)
            target_username = chat_info.username
        except Exception:
            pass
    elif raw_input.startswith("@"):
        username_clean = raw_input[1:]
        try:
            chat_info = await bot.get_chat(raw_input)
            target_id = chat_info.id
            target_username = username_clean
        except Exception:
            await message.answer("❌ Не удалось найти пользователя по такому юзернейму. Убедитесь, что он хоть раз запускал бота.")
            return
    else:
        await message.answer("❌ Неверный формат. Введите ID (цифры) или юзернейм (начинающийся с @). Попробуйте снова:")
        return

    conn = sqlite3.connect("bot.db")
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO secret_admins (user_id, username) VALUES (?, ?)", (target_id, target_username))
    conn.commit()
    conn.close()

    await state.clear()
    await message.answer(
        f"✅ Пользователь с ID <code>{target_id}</code> успешно добавлен в список админов секреток! Теперь у него появилась кнопка публикации.", 
        parse_mode="HTML", 
        reply_markup=get_main_reply_keyboard(message.from_user.id)
    )


@router.callback_query(F.data.startswith("delsecadmin_"))
async def process_delete_secret_admin(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    admin_to_del = int(callback.data.split("_")[1])

    conn = sqlite3.connect("bot.db")
    cursor = conn.cursor()
    cursor.execute("DELETE FROM secret_admins WHERE user_id = ?", (admin_to_del,))
    conn.commit()
    conn.close()

    await callback.message.edit_text(f"✅ Пользователь с ID <code>{admin_to_del}</code> убран из админов секреток.", parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "adm_edit_templates")
async def adm_edit_templates(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    await callback.message.edit_text(
        "📝 <b>Редактирование шаблонов сообщений</b>\n\n"
        "Вы можете настроить текст для активного поста и для поста после истечения времени таймера.\n\n"
        "💡 <b>Доступные теги для подстановки:</b>\n"
        "• <code>[Тип_Секретки]</code> — подставит название (например, <i>Лапка</i>)\n"
        "• <code>[Склоненный_Тип]</code> — подставит форму склонения (например, <i>лапки</i>)\n"
        "• <code>[Ссылка]</code> — подставит ссылку на вип-сервер (только для активного поста)\n\n"
        "Выберите шаблон для изменения:",
        parse_mode="HTML",
        reply_markup=get_templates_keyboard()
    )
    await callback.answer()


@router.callback_query(F.data == "tmpl_active")
async def tmpl_active_edit(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    current_tmpl = get_template("template_active")
    await callback.message.edit_text(
        f"📝 <b>Текущий шаблон активного поста:</b>\n\n<pre>{current_tmpl}</pre>\n\n"
        f"Отправьте новый текст шаблона с учетом тегов <code>[Тип_Секретки]</code>, <code>[Склоненный_Тип]</code> и <code>[Ссылка]</code>:",
        parse_mode="HTML"
    )
    await state.set_state(AdminStates.waiting_for_template_active)
    await callback.answer()


@router.message(AdminStates.waiting_for_template_active, F.text)
async def process_template_active(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    new_tmpl = message.text

    conn = sqlite3.connect("bot.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE settings SET value = ? WHERE key = 'template_active'", (new_tmpl,))
    conn.commit()
    conn.close()

    await state.clear()
    await message.answer("✅ Шаблон активного поста успешно обновлен!", reply_markup=get_main_reply_keyboard(message.from_user.id))


@router.callback_query(F.data == "tmpl_expired")
async def tmpl_expired_edit(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    current_tmpl = get_template("template_expired")
    await callback.message.edit_text(
        f"📝 <b>Текущий шаблон истекшего поста:</b>\n\n<pre>{current_tmpl}</pre>\n\n"
        f"Отправьте новый текст шаблона с учетом тега <code>[Тип_Секретки]</code>:",
        parse_mode="HTML"
    )
    await state.set_state(AdminStates.waiting_for_template_expired)
    await callback.answer()


@router.message(AdminStates.waiting_for_template_expired, F.text)
async def process_template_expired(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    new_tmpl = message.text

    conn = sqlite3.connect("bot.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE settings SET value = ? WHERE key = 'template_expired'", (new_tmpl,))
    conn.commit()
    conn.close()

    await state.clear()
    await message.answer("✅ Шаблон истекшего поста успешно обновлен!", reply_markup=get_main_reply_keyboard(message.from_user.id))


@router.callback_query(F.data == "adm_exit")
async def adm_exit(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("⚙️ Выход из админ-панели.")
    await callback.answer()


@router.callback_query(F.data == "adm_set_timer")
async def adm_set_timer(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    await callback.message.edit_text(
        "⏱ Введите новое время таймера **в секундах** (например, `510` для 8.5 минут):",
        parse_mode="Markdown"
    )
    await state.set_state(AdminStates.waiting_for_new_timer)
    await callback.answer()


@router.message(AdminStates.waiting_for_new_timer, F.text)
async def process_new_timer(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    
    if not message.text.isdigit():
        await message.answer("❌ Введите число (количество секунд). Попробуйте снова:")
        return

    new_seconds = int(message.text)
    conn = sqlite3.connect("bot.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE settings SET value = ? WHERE key = 'timer_seconds'", (str(new_seconds),))
    conn.commit()
    conn.close()

    await state.clear()
    await message.answer(f"✅ Время таймера успешно изменено на {new_seconds} секунд!", reply_markup=get_main_reply_keyboard(message.from_user.id))


@router.callback_query(F.data == "adm_add_type")
async def adm_add_type(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    await callback.message.edit_text("➕ Введите название нового типа секретки (например, <code>Звезда</code>):", parse_mode="HTML")
    await state.set_state(AdminStates.waiting_for_new_type_name)
    await callback.answer()


@router.message(AdminStates.waiting_for_new_type_name, F.text)
async def process_new_type_name(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    type_name = message.text.strip()
    await state.update_data(new_type_name=type_name)

    await message.answer(
        f"Теперь введите форму для правила склонения (например, для 'Звезда' родительный падеж — <code>звезды</code>):",
        parse_mode="HTML"
    )
    await state.set_state(AdminStates.waiting_for_new_type_declined)


@router.message(AdminStates.waiting_for_new_type_declined, F.text)
async def process_new_type_declined(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    declined_form = message.text.strip()
    data = await state.get_data()
    type_name = data["new_type_name"]

    conn = sqlite3.connect("bot.db")
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO secret_types (name, declined) VALUES (?, ?)", (type_name, declined_form))
    conn.commit()
    conn.close()

    await state.clear()
    await message.answer(f"✅ Тип секретки <b>{type_name}</b> успешно добавлен!", parse_mode="HTML", reply_markup=get_main_reply_keyboard(message.from_user.id))


@router.callback_query(F.data == "adm_del_type")
async def adm_del_type(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    types_dict = get_secret_types_dict()
    if not types_dict:
        await callback.message.edit_text("📭 Нет доступных типов секреток для удаления.")
        await callback.answer()
        return

    buttons = []
    for s_name in types_dict.keys():
        buttons.append([InlineKeyboardButton(text=f"🗑 Удалить: {s_name}", callback_data=f"deltype_{s_name}")])
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="open_secrets_panel")])

    await callback.message.edit_text("🗑 Выберите тип секретки для удаления:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()


@router.callback_query(F.data.startswith("deltype_"))
async def process_delete_type(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    type_to_del = callback.data.split("_", 1)[1]

    conn = sqlite3.connect("bot.db")
    cursor = conn.cursor()
    cursor.execute("DELETE FROM secret_types WHERE name = ?", (type_to_del,))
    conn.commit()
    conn.close()

    await callback.message.edit_text(f"✅ Тип секретки <b>{type_to_del}</b> удален.", parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "noop")
async def noop_handler(callback: CallbackQuery):
    await callback.answer()


# ==================== РАБОТА С ПОСТАМИ ====================

@router.message(F.text == "📥 Отправить секретку")
async def btn_send_secret(message: Message, state: FSMContext, bot: Bot):
    user_id = message.from_user.id
    if not is_secret_admin(user_id):
        return

    channel_id = await check_user_and_bot_rights(bot, user_id)

    if not channel_id:
        await message.answer(
            "❌ Канал не привязан или у тебя больше нет прав администратора в канале!\n"
            "Используй команду `/addchannel @юз_канала`",
            parse_mode="Markdown",
            reply_markup=get_main_reply_keyboard(user_id)
        )
        return

    types_dict = get_secret_types_dict()
    if not types_dict:
        await message.answer("❌ В базе данных нет доступных типов секреток. Добавь их через админ-панель.")
        return

    await state.clear()
    await message.answer("Выбери тип секретки:", reply_markup=get_types_keyboard())
    await state.set_state(SecretForm.waiting_for_type)


@router.message(F.text == "📋 Мои посты")
async def btn_my_posts(message: Message, bot: Bot):
    user_id = message.from_user.id
    if not is_secret_admin(user_id):
        return

    channel_id = await check_user_and_bot_rights(bot, user_id)
    if not channel_id:
        await message.answer(
            "❌ Сначала привяжи канал через `/addchannel @юз_канала`",
            parse_mode="Markdown",
            reply_markup=get_main_reply_keyboard(user_id)
        )
        return

    conn = sqlite3.connect("bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT id, secret_type, message_id FROM posts WHERE user_id = ? AND is_expired = 0", (user_id,))
    posts = cursor.fetchall()
    conn.close()

    if not posts:
        await message.answer("📭 У тебя нет активных опубликованных постов.")
        return

    keyboard_buttons = []
    for post in posts:
        post_id, s_type, msg_id = post
        keyboard_buttons.append([InlineKeyboardButton(text=f"📌 {s_type} (ID поста: {msg_id})", callback_data=f"expire_{post_id}")])

    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
    await message.answer("📋 Твои активные посты:\nНажми на пост, чтобы досрочно завершить его (удалить ссылку):", reply_markup=keyboard)


@router.callback_query(F.data.startswith("expire_"))
async def process_early_expire(callback: CallbackQuery, bot: Bot):
    post_db_id = int(callback.data.split("_")[1])

    conn = sqlite3.connect("bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT channel_id, message_id, secret_type, is_expired FROM posts WHERE id = ?", (post_db_id,))
    row = cursor.fetchone()
    conn.close()

    if not row:
        await callback.answer("❌ Пост не найден.", show_alert=True)
        return

    channel_id, message_id, secret_type, is_expired = row

    if is_expired:
        await callback.answer("⚠️ Этот пост уже завершен.", show_alert=True)
        return

    await expire_post(bot, channel_id, message_id, secret_type, post_db_id)
    await callback.message.edit_text("✅ Пост успешно закрыт досрочно (ссылка удалена в канале).")
    await callback.answer()


@router.callback_query(SecretForm.waiting_for_type, F.data.startswith("type_"))
async def process_type(callback: CallbackQuery, state: FSMContext):
    secret_type = callback.data.split("_", 1)[1]
    await state.update_data(secret_type=secret_type)

    await callback.message.edit_text(
        f"Выбрано: <b>{secret_type}</b>\n\nТеперь отправь <b>фотографию</b> секретки:",
        parse_mode="HTML",
    )
    await state.set_state(SecretForm.waiting_for_photo)
    await callback.answer()


@router.message(SecretForm.waiting_for_photo, F.photo)
async def process_photo(message: Message, state: FSMContext):
    photo_id = message.photo[-1].file_id
    await state.update_data(photo_id=photo_id)

    await message.answer("Отлично! Теперь отправь **ссылку**:")
    await state.set_state(SecretForm.waiting_for_link)

@router.message(SecretForm.waiting_for_photo)
async def process_photo_invalid(message: Message):
    await message.answer("Пожалуйста, отправь именно **фотографию**.")


@router.message(SecretForm.waiting_for_link, F.text)
async def process_link(message: Message, state: FSMContext):
    raw_text = message.text.strip()
    link = re.sub(r'\s+', '', raw_text)
    link_lower = link.lower()

    # --- ЗАЩИТА ССЫЛКИ ---
    if not (link_lower.startswith("https://roblox.com/") or link_lower.startswith("https://www.roblox.com/")):
        await message.answer("❌ Ссылка должна начинаться с https://roblox.com/ или https://www.roblox.com/!")
        return

    ends_with_digit = link[-1].isdigit()
    ends_with_type_server = link_lower.endswith("type=server")
    if not (ends_with_digit or ends_with_type_server):
        await message.answer("❌ Ссылка должна заканчиваться либо на цифру, либо на type=server!")
        return

    if re.search(r'[а-яА-ЯёЁ]', link):
        await message.answer("❌ Ссылка не должна содержать русские буквы!")
        return

    if "t.me" in link_lower:
        await message.answer("❌ Ссылка не может содержать t.me!")
        return

    if "@" in link:
        await message.answer("❌ Ссылка не может содержать символ @!")
        return
    # ---------------------

    await state.update_data(link=link)

    data = await state.get_data()
    secret_type = data["secret_type"]
    photo_id = data["photo_id"]
    
    types_dict = get_secret_types_dict()
    declined_type = types_dict.get(secret_type, secret_type)

    template = get_template("template_active")
    preview_text = (
        template
        .replace("[Тип_Секретки]", secret_type)
        .replace("[Склоненный_Тип]", declined_type)
        .replace("[Ссылка]", link)
    )

    await message.answer("Вот как будет выглядеть твой пост:")
    await message.answer_photo(
        photo=photo_id,
        caption=preview_text,
        parse_mode="HTML",
        reply_markup=get_confirm_keyboard(),
    )
    await state.set_state(SecretForm.waiting_for_confirm)

@router.message(SecretForm.waiting_for_link)
async def process_link_invalid(message: Message):
    await message.answer("Пожалуйста, отправь ссылку текстом.")


@router.callback_query(SecretForm.waiting_for_confirm, F.data.startswith("confirm_"))
async def process_confirmation(callback: CallbackQuery, state: FSMContext, bot: Bot):
    action = callback.data.split("_")[1]
    user_id = callback.from_user.id

    if action == "yes":
        channel_id = await check_user_and_bot_rights(bot, user_id)
        if not channel_id:
            await callback.message.edit_caption(
                caption="❌ Ошибка: канал не привязан или у тебя нет прав администратора!", reply_markup=None
            )
            await state.clear()
            await callback.answer()
            return

        data = await state.get_data()
        secret_type = data["secret_type"]
        photo_id = data["photo_id"]
        link = data["link"]
        
        types_dict = get_secret_types_dict()
        declined_type = types_dict.get(secret_type, secret_type)

        template = get_template("template_active")
        final_text = (
            template
            .replace("[Тип_Секретки]", secret_type)
            .replace("[Склоненный_Тип]", declined_type)
            .replace("[Ссылка]", link)
        )

        try:
            sent_msg = await bot.send_photo(
                chat_id=channel_id,
                photo=photo_id,
                caption=final_text,
                parse_mode="HTML",
            )

            conn = sqlite3.connect("bot.db")
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO posts (user_id, channel_id, message_id, secret_type, base_text, is_expired)
                VALUES (?, ?, ?, ?, ?, 0)
            """, (user_id, channel_id, sent_msg.message_id, secret_type, final_text))
            post_db_id = cursor.lastrowid
            conn.commit()
            conn.close()

            asyncio.create_task(timer_task(bot, channel_id, sent_msg.message_id, secret_type, post_db_id))

            await callback.message.edit_caption(
                caption="✅ Успешно отправлено в канал!", reply_markup=None
            )
        except Exception as e:
            await callback.message.edit_caption(
                caption=f"❌ Ошибка при отправке в канал: {e}", reply_markup=None
            )
    else:
        await callback.message.edit_caption(
            caption="❌ Отменено.", reply_markup=None
        )

    await state.clear()
    await callback.answer()


# ==================== НАСТРОЙКА FastAPI И ЗАПУСК ====================

app = FastAPI()

@app.get("/")
def index():
    return {"status": "Bot is alive!"}

async def start_telegram_bot():
    if not TOKEN:
        print("❌ ОШИБКА: Не задан токен бота! Укажи переменную окружения TOKEN.")
        return

    bot = Bot(token=TOKEN)
    dp = Dispatcher()
    dp.include_router(router)

    print("Бот запущен через polling в фоновом режиме...")
    await dp.start_polling(bot)

@app.on_event("startup")
async def on_startup():
    asyncio.create_task(start_telegram_bot())

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
    
