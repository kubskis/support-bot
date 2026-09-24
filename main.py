import asyncio
import logging
import sqlite3
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
BOT_TOKEN = "8969042562:AAGd6yW3Fn7IFHQ9EREYH5gwXTpHSHaFZes"
ADMIN_CHAT_ID = -1003945292994  # ID вашей группы поддержки

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)

def get_user_mention(user):
    return f"[{user.full_name}]({user.url})"

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

    friends_nickname = State()
    friends_played_before = State()

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
        await message.answer(f"❌ Вы заблокированы в поддержке.\n**Причина:** {banned[0]}", parse_mode="Markdown")
        return

    await state.clear()
    welcome_text = (
        "🎪 **Добро пожаловать в поддержку Tower Of Hell Secrets (@ToHSecretss)!** 🎪\n\n"
        "🤖 Нажмите на нужную кнопку на клавиатуре ниже, чтобы отправить заявку или задать вопрос."
    )
    await message.answer(welcome_text, reply_markup=main_keyboard(), parse_mode="Markdown")

# ----------------------------------------------------------------------
# ОБРАБОТКА НАЖАТИЙ НА КНОПКИ КЛАВИАТУРЫ
# ----------------------------------------------------------------------
@router.message(F.text == BTN_COMPLAINT, F.chat.type == "private")
async def start_complaint(message: Message, state: FSMContext):
    if is_banned(message.from_user.id): return
    await state.clear()
    await state.set_state(Form.complaint_reason)
    await message.answer("1️⃣ **Суть нарушения:**\nОпишите подробно, что именно сделал игрок.")

@router.message(F.text == BTN_APPEAL, F.chat.type == "private")
async def start_appeal(message: Message, state: FSMContext):
    if is_banned(message.from_user.id): return
    await state.clear()
    await state.set_state(Form.appeal_nickname)
    await message.answer("1️⃣ **Ваш ник:**\nУкажите ваш основной ник в игре (не display name).")

@router.message(F.text == BTN_FRIENDS, F.chat.type == "private")
async def start_friends(message: Message, state: FSMContext):
    if is_banned(message.from_user.id): return
    await state.clear()
    await state.set_state(Form.friends_nickname)
    await message.answer("⚠️ **Сначала отправьте запрос в друзья на ник `Farm_Arlekino`!**\n\n1️⃣ **Укажите ваш ник в игре:**")

@router.message(F.text == BTN_QUESTION, F.chat.type == "private")
async def start_question(message: Message, state: FSMContext):
    if is_banned(message.from_user.id): return
    await state.clear()
    await state.set_state(Form.question_text)
    await message.answer("❓ **Задайте ваш вопрос одним сообщением:**")

# ----------------------------------------------------------------------
# СБОР ДАННЫХ В ФОРМАХ (FSM)
# ----------------------------------------------------------------------
@router.message(Form.complaint_reason)
async def process_c_reason(message: Message, state: FSMContext):
    await state.update_data(c_reason=message.text)
    await state.set_state(Form.complaint_nickname)
    await message.answer("2️⃣ **Ник нарушителя:**\nУкажите основной ник (не display name).")

@router.message(Form.complaint_nickname)
async def process_c_nickname(message: Message, state: FSMContext):
    await state.update_data(c_nickname=message.text)
    await state.set_state(Form.complaint_photo)
    await message.answer("3️⃣ **Доказательства (Фото/Скриншот):**\n⚠️ Фото НЕ ОБРЕЗАТЬ! Плашка уровней справа должна быть видна.")

@router.message(Form.complaint_photo, F.photo)
async def process_c_photo(message: Message, state: FSMContext):
    await state.update_data(c_photo=message.photo[-1].file_id)
    await state.set_state(Form.complaint_server)
    await message.answer("4️⃣ **На каком сервере произошло нарушение?** (1, 2, 3 или 4):")

@router.message(Form.complaint_server)
async def process_c_server(message: Message, state: FSMContext):
    data = await state.get_data()
    ticket_id = create_ticket(message.from_user.id)
    user_mention = get_user_mention(message.from_user)
    
    admin_text = (
        f"🚨 **#Жалоба | Заявка №{ticket_id}**\n"
        f"👤 От: {user_mention} (ID: `{message.from_user.id}`)\n\n"
        f"1️⃣ **Суть:** {data['c_reason']}\n"
        f"2️⃣ **Ник нарушителя:** `{data['c_nickname']}`\n"
        f"4️⃣ **Сервер:** {message.text}\n\n"
        f"🔘 *Нажмите «Принять заявку», чтобы начать диалог.*"
    )
    sent = await bot.send_photo(
        ADMIN_CHAT_ID, 
        photo=data['c_photo'], 
        caption=admin_text, 
        reply_markup=take_ticket_kb(ticket_id),
        parse_mode="Markdown"
    )
    map_message(sent.message_id, message.from_user.id, ticket_id)
    
    await message.answer(f"✅ Ваша жалоба отправлена администрации! Номер заявки: **№{ticket_id}**.", parse_mode="Markdown")
    await state.clear()

@router.message(Form.appeal_nickname)
async def process_a_nickname(message: Message, state: FSMContext):
    await state.update_data(a_nickname=message.text)
    await state.set_state(Form.appeal_reason)
    await message.answer("2️⃣ **Почему вы считаете, что должны быть разблокированы?**")

@router.message(Form.appeal_reason)
async def process_a_reason(message: Message, state: FSMContext):
    data = await state.get_data()
    ticket_id = create_ticket(message.from_user.id)
    user_mention = get_user_mention(message.from_user)
    
    admin_text = (
        f"😡 **#Обжалование | Заявка №{ticket_id}**\n"
        f"👤 От: {user_mention} (ID: `{message.from_user.id}`)\n\n"
        f"1️⃣ **Ник:** `{data['a_nickname']}`\n"
        f"2️⃣ **Причина:** {message.text}\n\n"
        f"🔘 *Нажмите «Принять заявку», чтобы начать диалог.*"
    )
    sent = await bot.send_message(
        ADMIN_CHAT_ID, 
        admin_text, 
        reply_markup=take_ticket_kb(ticket_id),
        parse_mode="Markdown"
    )
    map_message(sent.message_id, message.from_user.id, ticket_id)
    
    await message.answer(f"✅ Ваше обжалование отправлено на рассмотрение! Номер заявки: **№{ticket_id}**.", parse_mode="Markdown")
    await state.clear()

@router.message(Form.friends_nickname)
async def process_f_nickname(message: Message, state: FSMContext):
    await state.update_data(f_nickname=message.text)
    await state.set_state(Form.friends_played_before)
    await message.answer("2️⃣ **Играли ли вы раньше на наших VIP-серверах?**")

@router.message(Form.friends_played_before)
async def process_f_played(message: Message, state: FSMContext):
    data = await state.get_data()
    ticket_id = create_ticket(message.from_user.id)
    user_mention = get_user_mention(message.from_user)
    
    admin_text = (
        f"👯‍♀️ **#Друзья | Заявка №{ticket_id}**\n"
        f"👤 От: {user_mention} (ID: `{message.from_user.id}`)\n\n"
        f"1️⃣ **Ник:** `{data['f_nickname']}`\n"
        f"2️⃣ **Играл ли раньше:** {message.text}\n\n"
        f"🔘 *Нажмите «Принять заявку», чтобы начать диалог.*"
    )
    sent = await bot.send_message(
        ADMIN_CHAT_ID, 
        admin_text, 
        reply_markup=take_ticket_kb(ticket_id),
        parse_mode="Markdown"
    )
    map_message(sent.message_id, message.from_user.id, ticket_id)
    
    await message.answer(f"✅ Заявка отправлена! Номер заявки: **№{ticket_id}**.", parse_mode="Markdown")
    await state.clear()

@router.message(Form.question_text)
async def process_question(message: Message, state: FSMContext):
    ticket_id = create_ticket(message.from_user.id)
    user_mention = get_user_mention(message.from_user)
    
    admin_text = (
        f"❓ **#Вопрос | Заявка №{ticket_id}**\n"
        f"👤 От: {user_mention} (ID: `{message.from_user.id}`)\n\n"
        f"**Вопрос:** {message.text}\n\n"
        f"🔘 *Нажмите «Принять заявку», чтобы начать диалог.*"
    )
    sent = await bot.send_message(
        ADMIN_CHAT_ID, 
        admin_text, 
        reply_markup=take_ticket_kb(ticket_id),
        parse_mode="Markdown"
    )
    map_message(sent.message_id, message.from_user.id, ticket_id)
    
    await message.answer(f"✅ Ваш вопрос отправлен поддержке! Номер заявки: **№{ticket_id}**.", parse_mode="Markdown")
    await state.clear()

# ----------------------------------------------------------------------
# КНОПКИ В ГРУППЕ: ПРИНЯТЬ И ЗАКРЫТЬ ЗАЯВКУ
# ----------------------------------------------------------------------
@router.callback_query(F.data.startswith("take_"))
async def take_ticket_handler(call: CallbackQuery):
    ticket_id = int(call.data.split("_")[1])
    admin_id = call.from_user.id
    admin_name = call.from_user.full_name

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
                f"👨‍💻 Администратор **{admin_name}** принял вашу заявку **№{ticket_id}**!\n\n"
                f"Теперь вы можете писать сюда сообщения напрямую — они сразу отправятся администратору.",
                parse_mode="Markdown"
            )
        except Exception:
            pass

    new_caption_or_text = (call.message.caption or call.message.text or "") + f"\n\n✅ **Взял в работу:** {call.from_user.mention_html()}"
    
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
                f"🔒 Ваша заявка **№{ticket_id}** закрыта администратором. Спасибо за обращение!\n\n"
                f"Если у вас возникнут новые вопросы, воспользуйтесь меню на клавиатуре ниже.",
                parse_mode="Markdown"
            )
        except Exception:
            pass

    status_text = f"\n\n🔒 **Заявка №{ticket_id} закрыта** администратором {call.from_user.mention_html()}."
    
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
        text_to_group = f"📩 **Сообщение по заявке №{active_ticket[0]} от {user_mention}:**\n\n{message.text or ''}"
        
        if message.photo:
            sent = await bot.send_photo(ADMIN_CHAT_ID, photo=message.photo[-1].file_id, caption=text_to_group, parse_mode="Markdown")
        else:
            sent = await bot.send_message(ADMIN_CHAT_ID, text_to_group, parse_mode="Markdown")
        
        map_message(sent.message_id, message.from_user.id, active_ticket[0])
        return

    await message.answer(
        "⚠️ **Общение не через формы запрещено.**\n"
        "Чтобы отправить заявку или задать вопрос, выберите раздел на клавиатуре ниже 👇",
        reply_markup=main_keyboard(),
        parse_mode="Markdown"
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
            await message.reply("⚠️ **Сначала нажмите кнопку «Принять заявку»**, чтобы отвечать на неё!")
            return
        elif assigned_admin != admin_id:
            await message.reply("❌ Эту заявку обрабатывает другой администратор! Вы не можете отправлять ответы в этот тикет.")
            return

    try:
        if message.photo:
            await bot.send_photo(user_id, photo=message.photo[-1].file_id, caption=f"👨‍💻 **Ответ поддержки (по заявке №{ticket_id}):**\n\n{message.caption or ''}", parse_mode="Markdown")
        else:
            await bot.send_message(user_id, f"👨‍💻 **Ответ поддержки (по заявке №{ticket_id}):**\n\n{message.text}", parse_mode="Markdown")
        
        await message.reply(
            f"✅ Ответ по заявке №{ticket_id} отправлен!", 
            reply_markup=close_ticket_kb(ticket_id, admin_id)
        )
    except Exception as e:
        await message.reply(f"❌ Не удалось отправить сообщение пользователю.\nОшибка: `{e}`", parse_mode="Markdown")

# ----------------------------------------------------------------------
# УНИВЕРСАЛЬНЫЕ КОМАНДЫ БАНА / РАЗБАНА В ГРУППЕ
# ----------------------------------------------------------------------
@router.message(Command("ban"), F.chat.id == ADMIN_CHAT_ID)
async def ban_command(message: Message):
    target_user_id = None
    args = message.text.split(maxsplit=2)
    reason = "Нарушение правил / спам"

    # Вариант 1: Через Reply на сообщение
    if message.reply_to_message:
        targetdata = get_user_by_group_msg(message.reply_to_message.message_id)
        if targetdata:
            target_user_id = targetdata[0]
            if len(args) > 1:
                reason = " ".join(args[1:])

    # Вариант 2: По ID (/ban 123456789 Причина)
    if not target_user_id and len(args) > 1 and args[1].isdigit():
        target_user_id = int(args[1])
        if len(args) > 2:
            reason = args[2]

    if not target_user_id:
        await message.reply(
            "⚠️ **Не удалось заблокировать.**\n\n"
            "Используйте одним из способов:\n"
            "1️⃣ Ответьте на сообщение заявки командой: `/ban Причина`\n"
            "2️⃣ Напишите команду с ID: `/ban 123456789 Причина`",
            parse_mode="Markdown"
        )
        return

    ban_user_db(target_user_id, reason)
    await message.reply(f"🚫 Пользователь `{target_user_id}` заблокирован в системе поддержки.\n**Причина:** {reason}", parse_mode="Markdown")
    
    try:
        await bot.send_message(target_user_id, f"❌ Вы заблокированы в поддержке.\n**Причина:** {reason}")
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
        await message.reply("⚠️ Укажите ID пользователя (`/unban 1234567`) или ответьте на его сообщение этой командой.")
        return

    unban_user_db(target_user_id)
    await message.reply(f"✅ Пользователь `{target_user_id}` разблокирован!")
    try:
        await bot.send_message(target_user_id, "✅ Ваш доступ к поддержке восстановлен!")
    except Exception:
        pass

# ----------------------------------------------------------------------
# ЗАПУСК БОТА
# ----------------------------------------------------------------------
async def main():
    print("Бот поддержки Tower Of Hell Secrets успешно запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
