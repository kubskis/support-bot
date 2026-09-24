import asyncio
import logging
import os
import sqlite3
import html
from aiohttp import web

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton
)

# ----------------------------------------------------------------------
# НАСТРОЙКИ
# ----------------------------------------------------------------------
BOT_TOKEN = "8969042562:AAGH7ck7qqNN_75ohEDTARYcCtEMgd-cp8A"
ADMIN_CHAT_ID = -1002364893721  # ID вашей группы поддержки

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)

def get_user_mention(user):
    safe_name = html.escape(user.full_name)
    return f'<a href="tg://user?id={user.id}">{safe_name}</a>'

# ----------------------------------------------------------------------
# БАЗА ДАННЫХ (SQLite)
# ----------------------------------------------------------------------
def init_db():
    conn = sqlite3.connect("support_bot.db")
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tickets (
            ticket_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            admin_id INTEGER DEFAULT NULL,
            status TEXT DEFAULT 'pending'
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS message_map (
            group_message_id INTEGER PRIMARY KEY,
            user_id INTEGER,
            ticket_id INTEGER
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS banned_users (
            user_id INTEGER PRIMARY KEY,
            reason TEXT
        )
    """)
    conn.commit()
    conn.close()

init_db()

def create_ticket(user_id):
    conn = sqlite3.connect("support_bot.db")
    cursor = conn.cursor()
    cursor.execute("INSERT INTO tickets (user_id, status) VALUES (?, 'pending')", (user_id,))
    ticket_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return ticket_id

def map_message(group_msg_id, user_id, ticket_id):
    conn = sqlite3.connect("support_bot.db")
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO message_map VALUES (?, ?, ?)", (group_msg_id, user_id, ticket_id))
    conn.commit()
    conn.close()

def get_user_by_group_msg(group_msg_id):
    conn = sqlite3.connect("support_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, ticket_id FROM message_map WHERE group_message_id = ?", (group_msg_id,))
    row = cursor.fetchone()
    conn.close()
    return row

def get_ticket_info(ticket_id):
    conn = sqlite3.connect("support_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, admin_id, status FROM tickets WHERE ticket_id = ?", (ticket_id,))
    row = cursor.fetchone()
    conn.close()
    return row

def get_active_ticket(user_id):
    conn = sqlite3.connect("support_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT ticket_id, admin_id FROM tickets WHERE user_id = ? AND status = 'active' ORDER BY ticket_id DESC LIMIT 1", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row

def activate_ticket(ticket_id, admin_id):
    conn = sqlite3.connect("support_bot.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE tickets SET status = 'active', admin_id = ? WHERE ticket_id = ?", (admin_id, ticket_id))
    conn.commit()
    conn.close()

def close_ticket_db(ticket_id):
    conn = sqlite3.connect("support_bot.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE tickets SET status = 'closed' WHERE ticket_id = ?", (ticket_id,))
    conn.commit()
    conn.close()

def is_banned(user_id):
    conn = sqlite3.connect("support_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT reason FROM banned_users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row

def ban_user_db(user_id, reason):
    conn = sqlite3.connect("support_bot.db")
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO banned_users VALUES (?, ?)", (user_id, reason))
    conn.commit()
    conn.close()

def unban_user_db(user_id):
    conn = sqlite3.connect("support_bot.db")
    cursor = conn.cursor()
    cursor.execute("DELETE FROM banned_users WHERE user_id = ?", (user_id,))
    conn.commit()
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
        InlineKeyboardButton(text="📥 Принять заявку", callback_data=f"take_{ticket_id}")
    ]])

def close_ticket_kb(ticket_id, admin_id):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔒 Завершить заявку", callback_data=f"close_{ticket_id}_{admin_id}")
    ]])

@router.message(CommandStart(), F.chat.type == "private")
async def start_cmd(message: Message, state: FSMContext):
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
    if is_banned(message.from_user.id): return
    await state.clear()
    await state.set_state(Form.complaint_reason)
    await message.answer("1️⃣ <b>Суть нарушения:</b>\nОпишите подробно, что именно сделал игрок.", parse_mode="HTML")

@router.message(F.text == BTN_APPEAL, F.chat.type == "private")
async def start_appeal(message: Message, state: FSMContext):
    if is_banned(message.from_user.id): return
    await state.clear()
    await state.set_state(Form.appeal_nickname)
    await message.answer("1️⃣ <b>Ваш ник:</b>\nУкажите ваш основной ник в игре (не display name).", parse_mode="HTML")

@router.message(F.text == BTN_FRIENDS, F.chat.type == "private")
async def start_friends_temp(message: Message, state: FSMContext):
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
    await message.answer("4️⃣ <b>На каком сервере произошло нарушение?</b> (1, 2, 3 или 4):", parse_mode="HTML")

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
        f"👤 От: {user_mention} (ID: <code>{message.from_user.id}</code>)\n\n"
        f"1️⃣ <b>Суть:</b> {c_reason}\n"
        f"2️⃣ <b>Ник нарушителя:</b> <code>{c_nickname}</code>\n"
        f"4️⃣ <b>Сервер:</b> {c_server}\n\n"
        f"🔘 <i>Нажмите «Принять заявку», чтобы начать диалог.</i>"
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
        f"👤 От: {user_mention} (ID: <code>{message.from_user.id}</code>)\n\n"
        f"1️⃣ <b>Ник:</b> <code>{a_nickname}</code>\n"
        f"2️⃣ <b>Причина:</b> {a_reason}\n\n"
        f"🔘 <i>Нажмите «Принять заявку», чтобы начать диалог.</i>"
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
        f"👤 От: {user_mention} (ID: <code>{message.from_user.id}</code>)\n\n"
        f"<b>Вопрос:</b> {question}\n\n"
        f"🔘 <i>Нажмите «Принять заявку», чтобы начать диалог.</i>"
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
# КНОПКИ В ГРУППЕ: ПРИНЯТЬ И ЗАКРЫТЬ ЗАЯВКУ
# ----------------------------------------------------------------------
@router.callback_query(F.data.startswith("take_"))
async def take_ticket_handler(call: CallbackQuery):
    ticket_id = int(call.data.split("_")[1])
    admin_id = call.from_user.id

    ticket_info = get_ticket_info(ticket_id)
    if ticket_info and ticket_info[1] is not None:
        await call.answer("❌ Эту заявку уже принял другой администратор!", show_alert=True)
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
    if is_banned(message.from_user.id):
        return

    current_state = await state.get_state()
    if current_state is not None:
        return

    active_ticket = get_active_ticket(message.from_user.id)
    
    if active_ticket:
        user_mention = get_user_mention(message.from_user)
        user_text = html.escape(message.text or '')
        text_to_group = f"📩 <b>Сообщение по заявке №{active_ticket[0]} от {user_mention}:</b>\n\n{user_text}"
        
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
# ОТВЕТ АДМИНА ИЗ ГРУППЫ ПОЛЬЗОВАТЕЛЮ (Reply в чате)
# ----------------------------------------------------------------------
@router.message(F.chat.id == ADMIN_CHAT_ID, F.reply_to_message)
async def admin_reply_in_group(message: Message):
    if message.text and message.text.startswith("/"):
        return

    targetdata = get_user_by_group_msg(message.reply_to_message.message_id)
    if not targetdata:
        return

    user_id, ticket_id = targetdata[0], targetdata[1]
    admin_id = message.from_user.id

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
            await bot.send_photo(user_id, photo=message.photo[-1].file_id, caption=f"👨‍💻 <b>Ответ поддержки (по заявке №{ticket_id}):</b>\n\n{caption}", parse_mode="HTML")
        else:
            text = html.escape(message.text or '')
            await bot.send_message(user_id, f"👨‍💻 <b>Ответ поддержки (по заявке №{ticket_id}):</b>\n\n{text}", parse_mode="HTML")
        
        await message.reply(
            f"✅ Ответ по заявке №{ticket_id} отправлен!", 
            reply_markup=close_ticket_kb(ticket_id, admin_id)
        )
    except Exception as e:
        await message.reply(f"❌ Не удалось отправить сообщение пользователю.\nОшибка: <code>{html.escape(str(e))}</code>", parse_mode="HTML")

# ----------------------------------------------------------------------
# УНИВЕРСАЛЬНЫЕ КОМАНДЫ БАНА / РАЗБАНА В ГРУППЕ
# ----------------------------------------------------------------------
@router.message(Command("ban"), F.chat.id == ADMIN_CHAT_ID)
async def ban_command(message: Message):
    target_user_id = None
    args = message.text.split(maxsplit=2)
    reason = "Нарушение правил / спам"

    if message.reply_to_message:
        targetdata = get_user_by_group_msg(message.reply_to_message.message_id)
        if targetdata:
            target_user_id = targetdata[0]
            if len(args) > 1:
                reason = " ".join(args[1:])

    if not target_user_id and len(args) > 1 and args[1].isdigit():
        target_user_id = int(args[1])
        if len(args) > 2:
            reason = args[2]

    if not target_user_id:
        await message.reply(
            "⚠️ <b>Не удалось заблокировать.</b>\n\n"
            "Используйте одним из способов:\n"
            "1️⃣ Ответьте на сообщение заявки командой: <code>/ban Причина</code>\n"
            "2️⃣ Напишите команду с ID: <code>/ban 123456789 Причина</code>",
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
    
