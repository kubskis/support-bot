import asyncio
import logging
import os
import sqlite3
from aiohttp import web

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

# Настройка логирования
logging.basicConfig(level=logging.INFO)

# Конфигурация бота
BOT_TOKEN = "8526419531:AAETEDjGFAC2FW5hHyaPCfuNivTNEiJTgVw"
ADMIN_CHAT_ID = -1002364893721  # ID вашей группы/чата администраторов
DB_NAME = "support_bot.db"

# --- Инициализация базы данных ---
def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    # Таблица тикетов
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tickets (
            ticket_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            admin_msg_id INTEGER,
            status TEXT DEFAULT 'open'
        )
    """)
    # Таблица заблокированных пользователей
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS banned_users (
            user_id INTEGER PRIMARY KEY,
            reason TEXT
        )
    """)
    # Таблица всех пользователей бота (для рассылки при обновлениях)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY
        )
    """)
    conn.commit()
    conn.close()

init_db()

# --- Вспомогательные функции работы с БД ---
def save_user(user_id: int):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
    conn.commit()
    conn.close()

def get_all_users():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM users")
    rows = cursor.fetchall()
    conn.close()
    return [row[0] for row in rows]

def is_banned(user_id: int) -> bool:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM banned_users WHERE user_id = ?", (user_id,))
    result = cursor.fetchone()
    conn.close()
    return result is not None

def ban_user(user_id: int, reason: str = "Нарушение правил"):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO banned_users (user_id, reason) VALUES (?, ?)", (user_id, reason))
    conn.commit()
    conn.close()

def unban_user(user_id: int):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM banned_users WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

def create_ticket(user_id: int) -> int:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO tickets (user_id, status) VALUES (?, 'open')", (user_id,))
    ticket_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return ticket_id

def set_ticket_admin_msg(ticket_id: int, admin_msg_id: int):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("UPDATE tickets SET admin_msg_id = ? WHERE ticket_id = ?", (admin_msg_id, ticket_id))
    conn.commit()
    conn.close()

def get_ticket_by_admin_msg(admin_msg_id: int):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT ticket_id, user_id, status FROM tickets WHERE admin_msg_id = ?", (admin_msg_id,))
    row = cursor.fetchone()
    conn.close()
    return row

def close_ticket(ticket_id: int):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("UPDATE tickets SET status = 'closed' WHERE ticket_id = ?", (ticket_id,))
    conn.commit()
    conn.close()

# --- FSM Состояния ---
class TicketState(StatesGroup):
    waiting_for_message = State()

# --- Главное меню (Клавиатуры) ---
def get_main_keyboard():
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📝 Создать обращения / Задать вопрос")],
            [KeyboardButton(text="ℹ️ Информация / FAQ"), KeyboardButton(text="📋 Мои обращение")]
        ],
        resize_keyboard=True
    )
    return keyboard

def get_cancel_keyboard():
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="❌ Отмена")]
        ],
        resize_keyboard=True
    )
    return keyboard

def get_admin_ticket_inline(ticket_id: int, user_id: int):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🔒 Закрыть тикет", callback_data=f"close_{ticket_id}"),
                InlineKeyboardButton(text="🚫 Забанить", callback_data=f"ban_{user_id}")
            ]
        ]
    )

# --- Инициализация бота и диспетчера ---
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# --- Обработка команд пользователя ---
@dp.message(CommandStart())
async def start_handler(message: types.Message, state: FSMContext):
    await state.clear()
    save_user(message.from_user.id)
    
    if is_banned(message.from_user.id):
        await message.answer("❌ Вы заблокированы в системе поддержки.")
        return

    welcome_text = (
        f"👋 Здравствуйте, {message.from_user.first_name}!\n\n"
        "Добро пожаловать в службу поддержки **Tower Of Hell Secrets**.\n"
        "Здесь вы можете задать вопрос или сообщить о проблеме.\n\n"
        "Выберите действие ниже:"
    )
    await message.answer(welcome_text, reply_markup=get_main_keyboard(), parse_mode="Markdown")

@dp.message(F.text == "❌ Отмена")
async def cancel_handler(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Действие отменено.", reply_markup=get_main_keyboard())

@dp.message(F.text == "ℹ️ Информация / FAQ")
async def faq_handler(message: types.Message):
    faq_text = (
        "📌 **Часто задаваемые вопросы (FAQ):**\n\n"
        "1. **Как получить секреты/пасы?**\n"
        "— Все подробные инструкции доступны на нашем официальном канале.\n\n"
        "2. **Сколько времени отвечает поддержка?**\n"
        "— Администраторы отвечают по мере возможности (обычно от 5 минут до пары часов).\n\n"
        "3. **За что можно получить бан в поддержке?**\n"
        "— Спам, оскорбления, нецензурная лексика и флуд."
    )
    await message.answer(faq_text, parse_mode="Markdown")

@dp.message(F.text == "📝 Создать обращения / Задать вопрос")
async def create_ticket_prompt(message: types.Message, state: FSMContext):
    if is_banned(message.from_user.id):
        await message.answer("❌ Вы заблокированы и не можете создавать тикеты.")
        return

    await state.set_state(TicketState.waiting_for_message)
    await message.answer(
        "Напишите ваше сообщение, опишите проблему или вопрос одним сообщением.\n"
        "Вы также можете прикрепить фото или документ.",
        reply_markup=get_cancel_keyboard()
    )

@dp.message(TicketState.waiting_for_message)
async def process_ticket_message(message: types.Message, state: FSMContext):
    if is_banned(message.from_user.id):
        await state.clear()
        await message.answer("❌ Вы заблокированы.", reply_markup=get_main_keyboard())
        return

    ticket_id = create_ticket(message.from_user.id)
    user = message.from_user
    username = f"@{user.username}" if user.username else "Отсутствует"

    header = (
        f"📩 **Новый тикет #{ticket_id}**\n"
        f"От пользователя: {user.full_name} ({username})\n"
        f"ID: `{user.id}`\n\n"
        f"**Сообщение:**\n"
    )

    sent_msg = None
    if message.text:
        sent_msg = await bot.send_message(
            ADMIN_CHAT_ID,
            header + message.text,
            reply_markup=get_admin_ticket_inline(ticket_id, user.id),
            parse_mode="Markdown"
        )
    elif message.photo:
        sent_msg = await bot.send_photo(
            ADMIN_CHAT_ID,
            photo=message.photo[-1].file_id,
            caption=header + (message.caption or ""),
            reply_markup=get_admin_ticket_inline(ticket_id, user.id),
            parse_mode="Markdown"
        )
    elif message.document:
        sent_msg = await bot.send_document(
            ADMIN_CHAT_ID,
            document=message.document.file_id,
            caption=header + (message.caption or ""),
            reply_markup=get_admin_ticket_inline(ticket_id, user.id),
            parse_mode="Markdown"
        )

    if sent_msg:
        set_ticket_admin_msg(ticket_id, sent_msg.message_id)

    await state.clear()
    await message.answer(
        f"✅ Ваше обращение **#{ticket_id}** успешно отправлено поддержке!\n"
        "Ожидайте ответа прямо в этом чате.",
        reply_markup=get_main_keyboard(),
        parse_mode="Markdown"
    )

# --- Обработка ответов Администраторов ---
@dp.message(F.chat.id == ADMIN_CHAT_ID)
async def admin_reply_handler(message: types.Message):
    # Ответ через Reply на сообщение тикета
    if message.reply_to_message:
        reply_msg_id = message.reply_to_message.message_id
        ticket = get_ticket_by_admin_msg(reply_msg_id)

        if ticket:
            ticket_id, user_id, status = ticket
            if status == "closed":
                await message.reply("⚠️ Этот тикет уже закрыт.")
                return

            try:
                answer_prefix = f"💬 **Ответ от поддержки (Тикет #{ticket_id}):**\n\n"
                if message.text:
                    await bot.send_message(user_id, answer_prefix + message.text, parse_mode="Markdown")
                elif message.photo:
                    await bot.send_photo(user_id, message.photo[-1].file_id, caption=answer_prefix + (message.caption or ""), parse_mode="Markdown")
                elif message.document:
                    await bot.send_document(user_id, message.document.file_id, caption=answer_prefix + (message.caption or ""), parse_mode="Markdown")
                
                await message.reply(f"✅ Ответ успешно отправлен пользователю по тикету #{ticket_id}.")
            except Exception as e:
                await message.reply(f"❌ Не удалось доставить сообщение пользователю: {e}")

# --- Callback-кнопки для админов ---
@dp.callback_query(F.data.startswith("close_"))
async def callback_close_ticket(callback: types.CallbackQuery):
    ticket_id = int(callback.data.split("_")[1])
    close_ticket(ticket_id)
    await callback.message.edit_text(callback.message.text + "\n\n🔒 **Тикет закрыт.**", parse_mode="Markdown")
    await callback.answer("Тикет закрыт.")

@dp.callback_query(F.data.startswith("ban_"))
async def callback_ban_user(callback: types.CallbackQuery):
    user_id = int(callback.data.split("_")[1])
    ban_user(user_id)
    await callback.answer(f"Пользователь {user_id} забанен.", show_alert=True)

# --- Команды админов (Разбан/Бан) ---
@dp.command(Command("unban"))
async def cmd_unban(message: types.Message):
    if message.chat.id != ADMIN_CHAT_ID:
        return
    args = message.text.split()
    if len(args) > 1 and args[1].isdigit():
        target_id = int(args[1])
        unban_user(target_id)
        await message.reply(f"✅ Пользователь `{target_id}` успешно разбанен.", parse_mode="Markdown")
    else:
        await message.reply("Использование: `/unban <user_id>`", parse_mode="Markdown")

# --- Рассылка при перезапуске/обновлении бота ---
async def notify_users_on_startup(bot: Bot):
    users = get_all_users()
    if not users:
        logging.info("Список пользователей для рассылки пуст.")
        return

    update_message = (
        "⚡️ **Системное обновление сервиса**\n\n"
        "Мы обновили данные и оптимизировали работу бота. "
        "Чтобы все функции и интерфейс работали корректно, пожалуйста, перезапустите бота:\n\n"
        "👉 Нажмите **/start**"
    )

    logging.info(f"Начинаем рассылку для {len(users)} пользователей...")
    count = 0
    for user_id in users:
        try:
            await bot.send_message(user_id, update_message, parse_mode="Markdown")
            count += 1
            await asyncio.sleep(0.05)
        except (TelegramForbiddenError, TelegramBadRequest):
            pass
        except Exception as e:
            logging.error(f"Не удалось отправить сообщение пользователю {user_id}: {e}")

    logging.info(f"Рассылка завершена. Успешно доставлено: {count}/{len(users)}")

# --- Health Check веб-сервер для Render ---
async def handle_health_check(request):
    return web.Response(text="Bot Support ToH Secrets is running successfully!")

# --- Главная точка входа ---
async def main():
    # Запуск HTTP веб-сервера для Render (убирает ошибку No open ports)
    app = web.Application()
    app.router.add_get("/", handle_health_check)
    app.router.add_get("/health", handle_health_check)

    runner = web.AppRunner(app)
    await runner.setup()

    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    print(f"Бот поддержки Tower Of Hell Secrets успешно запущен! HTTP-сервер слушает порт {port}.")

    # Фоновая рассылка пользователям об обновлении
    asyncio.create_task(notify_users_on_startup(bot))

    # Запуск поллинга
    try:
        await dp.start_polling(bot)
    finally:
        await runner.cleanup()

if __name__ == "__main__":
    asyncio.run(main())
    
