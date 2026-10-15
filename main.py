import asyncio
import io
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
import psycopg2.pool
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router, BaseMiddleware
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter, TelegramBadRequest
from aiogram.filters import Command, CommandStart, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton, TelegramObject,
    LinkPreviewOptions, InputMediaPhoto, InputMediaVideo, BotCommand, BotCommandScopeChat, 
    BotCommandScopeAllGroupChats, BotCommandScopeAllPrivateChats, FSInputFile
)

# Модуль юзербота для отправки с Telegram Premium (Telethon)
from telethon import TelegramClient as UserClient
from telethon.sessions import StringSession
from telethon.tl.types import MessageEntityCustomEmoji
from telethon.errors import FloodWaitError, MessageNotModifiedError, MessageIdInvalidError

# ----------------------------------------------------------------------
# НАСТРОЙКИ И ОКРУЖЕНИЕ
# ----------------------------------------------------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_CHAT_ID = -1003995930989  # ID группы поддержки
TOPIC_SUPPORT_ID = 2            # ID темы "Поддержка"
TOPIC_SECRETS_ID = 4            # ID темы "Секретки"
STAFF_DELAY_SECONDS = 30        # 1-я стадия: только стафф (группа)
PRELINK_DELAY_SECONDS = 30      # 2-я стадия: стафф + «Предссылки» (личка)
CHANNEL_DELAY_SECONDS = STAFF_DELAY_SECONDS + PRELINK_DELAY_SECONDS  # 3-я стадия: канал (через 60 сек. после группы)
OWNER_ID = int(os.getenv("ADMIN_ID", "0"))  # Главный создатель бота
MSK_TZ = timezone(timedelta(hours=3))
PHOTO_FILENAME = "supportphoto1.jpg"

# НАСТРОЙКИ PREMIUM-ЮЗЕРБОТА
USER_SESSION_STRING = (os.getenv("USER_SESSION_STRING") or "").strip()
TELEGRAM_API_HASH = (os.getenv("TELEGRAM_API_HASH") or "").strip()
try:
    TELEGRAM_API_ID = int((os.getenv("TELEGRAM_API_ID") or "0").strip())
except ValueError:
    TELEGRAM_API_ID = 0

# Конфигурация базы данных
DATABASE_URL = os.getenv("DATABASE_URL")
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME", "postgres")
DB_USER = os.getenv("DB_USER", "postgres")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
DB_SSLMODE = os.getenv("DB_SSLMODE", "prefer")

if not BOT_TOKEN:
    raise ValueError("ОШИБКА: Токен бота не найден! Укажите BOT_TOKEN в Environment Variables.")
if not DATABASE_URL and not DB_HOST:
    raise ValueError("ОШИБКА: Не указаны параметры подключения к базе данных!")

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()

# Инициализация юзербота через Telethon
userbot: UserClient | None = None
if USER_SESSION_STRING and TELEGRAM_API_ID and TELEGRAM_API_HASH:
    userbot = UserClient(
        StringSession(USER_SESSION_STRING),
        api_id=TELEGRAM_API_ID,
        api_hash=TELEGRAM_API_HASH
    )
else:
    logging.warning(
        "Юзербот отключён: задайте переменные окружения USER_SESSION_STRING, TELEGRAM_API_ID и TELEGRAM_API_HASH."
    )

_BG_TASKS: set = set()

def spawn(coro):
    """create_task, но с удержанием ссылки (иначе задачу может собрать GC) и логом ошибок."""
    task = asyncio.create_task(coro)
    _BG_TASKS.add(task)

    def _done(t):
        _BG_TASKS.discard(t)
        if not t.cancelled() and t.exception() is not None:
            logging.error(f"Фоновая задача упала: {t.exception()!r}")
    task.add_done_callback(_done)
    return task

creation_lock = asyncio.Lock()
publish_lock = asyncio.Lock()
saboteur_lock = asyncio.Lock()
farm_lock = asyncio.Lock()

_FOOTER_LINKS = (
    "🤍Наш <a href='https://t.me/SecretsToH'>чат</a> | "
    "Наш <a href='https://t.me/ToHSecretss'>канал</a> | "
    "Наша <a href='https://t.me/ToHSecrets_bot'>поддержка</a>🤍"
)

DEFAULT_FARM_LIVE_ACTIVE = (
    "🌾 <b>Фарм серверы</b>\n\n"
    "[Список_Серверов]\n\n"
    + _FOOTER_LINKS
)
DEFAULT_FARM_LIVE_ITEM = (
    "🔹 <b>Сервер #[Номер_Сервера]</b>\n"
    "• Искатель: [Искатель]\n"
    "• Ник в Roblox: [Ник_Роблокс]\n"
    "• Секций: [Секции]\n"
    "• В друзья: [Друзья]\n"
    "• Ссылка: [Ссылка]"
)
MUTATOR_TIMER_SECONDS = 490  # 8:10
MUTATOR_PHOTO = "mutator.jpg"
DEFAULT_MUTATOR_ACTIVE = (
    "🧬 <b>Бейдж мутатор</b>\n\n"
    "🔗 [Ссылка]"
)
DEFAULT_MUTATOR_EXPIRED = (
    "🧬 <b>Бейдж мутатор</b>\n\n"
    "Время вышло!"
)
DEFAULT_FARM_LIVE_CLOSED = (
    "🌾 <b>Фарм завершён!</b>\n\n"
    "Все фарм-серверы закрыты. Следите за новыми в канале🤍\n\n"
    + _FOOTER_LINKS
)
processed_media_groups = set()

# Кастомные TGP цифры
NUM_1 = "<tg-emoji emoji-id='5285184495857715032'>1️⃣</tg-emoji>"
NUM_2 = "<tg-emoji emoji-id='5287236502382724614'>2️⃣</tg-emoji>"
NUM_3 = "<tg-emoji emoji-id='5287361241117911628'>3️⃣</tg-emoji>"
NUM_4 = "<tg-emoji emoji-id='5287402803516427365'>4️⃣</tg-emoji>"
NUM_5 = "<tg-emoji emoji-id='5285444878250033485'>5️⃣</tg-emoji>"
NUM_6 = "<tg-emoji emoji-id='5285078586259161715'>6️⃣</tg-emoji>"
NUM_7 = "<tg-emoji emoji-id='5287334204298773700'>7️⃣</tg-emoji>"
NUM_8 = "<tg-emoji emoji-id='5287337962395158575'>8️⃣</tg-emoji>"
NUM_9 = "<tg-emoji emoji-id='5287610387875775295'>9️⃣</tg-emoji>"

# Кастомные TGP иконки
ICON_CHECK = "<tg-emoji emoji-id='5346300789558101141'>✅</tg-emoji>"
ICON_BALL = "<tg-emoji emoji-id='5348292816864833864'>🔮</tg-emoji>"
ICON_PHOTO = "<tg-emoji emoji-id='5891119667388354506'>📷</tg-emoji>"
ICON_TIMER = "<tg-emoji emoji-id='5348236797606379943'>⏱</tg-emoji>"
ICON_USERS = "<tg-emoji emoji-id='5942877472163892475'>👥</tg-emoji>"
ICON_PENCIL = "<tg-emoji emoji-id='5879841310902324730'>✏️</tg-emoji>"
ICON_HORN = "<tg-emoji emoji-id='5771695636411847302'>📢</tg-emoji>"

ICON_CROSS = "<tg-emoji emoji-id='5348362704572664028'>❌</tg-emoji>"
ICON_LOCK = "<tg-emoji emoji-id='5879895758202735862'>🔒</tg-emoji>"
ICON_STAR = "<tg-emoji emoji-id='5348446245981536690'>⭐</tg-emoji>"
ICON_WARN = "<tg-emoji emoji-id='5348110675891731556'>⚠️</tg-emoji>"
ICON_TIME = "<tg-emoji emoji-id='5345780046248298257'>🌙</tg-emoji>"

# ----------------------------------------------------------------------
# ТГП-ЭМОДЗИ: единый реестр (обычный эмодзи -> ID премиум-эмодзи)
# ----------------------------------------------------------------------
EMOJI_IDS = {
    "✅": "5346300789558101141", "❌": "5348362704572664028", "⚠": "5348110675891731556",
    "⭐": "5348446245981536690", "🔒": "5879895758202735862", "⏱": "5348236797606379943",
    "⏰": "5348236797606379943", "🌙": "5345780046248298257", "👥": "5942877472163892475",
    "✏": "5879841310902324730", "🔮": "5348292816864833864", "📷": "5891119667388354506",
    "📢": "5771695636411847302", "📣": "5771695636411847302", "🔄": "5877410604225924969",
    "⚙": "5877260593903177342", "📊": "5913702317667913862", "🎫": "5346024704765347251",
    "👑": "5807868868886009920", "➕": "5346004239246183111", "➖": "5782743443348394021",
    "🛠": "5988023995125993550", "🎉": "5994502837327892086", "🔍": "5345840270279724328",
    "❓": "5220053623211305785", "🔙": "5352759161945867747", "🟢": "5980930633298350051",
    "🔴": "5980953710157632545", "👤": "5879770735999717115", "🌾": "5201950868634616095",
    "🧪": "5897607722295103204", "🔔": "5909201569898827582", "🗑": "5879896690210639947",
    "🕵": "5226658625782558732", "📋": "5345802826754835536", "📭": "5967456680940671207",
    "🧬": "5355270175920763448", "⚡": "5219943216781995020", "🚨": "5323679617716863034",
    "👯\u200d♀": "5942877472163892475", "📡": "5258500422393415126", "📌": "5796440171364749940",
    "🔹": "5915980888012755151", "🏆": "5345892905103932200", "➡": "5832251986635920010",
    "😡": "5924666978332578279", "📲": "6005775159384870794", "🌐": "5879585266426973039",
    "📩": "5348348681504441752", "👆": "5348221662141638097", "👨\u200d💻": "6197025088093556943",
    "🌟": "5323333018151045958", "🥇": "5440539497383087970", "🥈": "5447203607294265305",
    "🥉": "5453902265922376865", "📥": "5877307202888273539", "⏩": "5346172687863529237",
    "👁": "5960714428394507968", "📅": "5967412305338568701", "🗓": "5967382867632722656",
    "♾": "5908972566537572230", "🏃": "5348110675891731556", "✍": "5877214659227946561",
    "💬": "5818740758257077530", "🚫": "5872829476143894491", "👍": "5992199545151295755",
    "📝": "5960551395730919906", "⏳": "5875291072225087249",
}
_VS16 = "\ufe0f"
_ID_GLYPH = {}
for _k, _v in EMOJI_IDS.items():
    _ID_GLYPH.setdefault(_v, _k)
_EMOJI_ALT = "|".join(
    "".join(re.escape(_ch) + _VS16 + "?" for _ch in _k)
    for _k in sorted(EMOJI_IDS, key=len, reverse=True)
)
_EMOJI_RE = re.compile(_EMOJI_ALT)
_LEAD_EMOJI_RE = re.compile(r"^\s*(" + _EMOJI_ALT + r")\s*")
_PROTECTED_RE = re.compile(
    r"<tg-emoji\b[^>]*>.*?</tg-emoji>|<(code|pre)\b[^>]*>.*?</\1>|<[^>]+>", re.DOTALL
)


def _emoji_key(e: str) -> str:
    return e.replace(_VS16, "")


def tge(emoji: str) -> str:
    """Готовый тег тгп-эмодзи для обычного эмодзи (если ID нет, вернёт его как есть)."""
    i = EMOJI_IDS.get(_emoji_key(emoji))
    return f'<tg-emoji emoji-id="{i}">{emoji}</tg-emoji>' if i else emoji


_ENT_TAG_RE = re.compile(r"<(?:b|strong|i|em|u|ins|s|strike|del|a|code|pre|blockquote|tg-emoji|tg-spoiler|span)\b", re.I)
_MAX_ENTITIES = 90  # у Telegram лимит ~100 элементов форматирования на сообщение, оставляем запас


def _emojify_piece(piece: str, left: list) -> str:
    def _rep(x):
        if left[0] <= 0:
            return x.group(0)
        left[0] -= 1
        return tge(x.group(0))
    out, pos = [], 0
    for m in _PROTECTED_RE.finditer(piece):
        out.append(_EMOJI_RE.sub(_rep, piece[pos:m.start()]))
        out.append(m.group(0))
        pos = m.end()
    out.append(_EMOJI_RE.sub(_rep, piece[pos:]))
    return "".join(out)


def emojify(text: str) -> str:
    """Заменяет обычные эмодзи в HTML-тексте на тгп. Готовые теги, <code>, <pre> и атрибуты не трогает.
    Следит, чтобы в сообщении не набралось больше ~100 элементов форматирования."""
    if not text:
        return text
    return _emojify_piece(text, [max(0, _MAX_ENTITIES - len(_ENT_TAG_RE.findall(text)))])


# --- Маркеры «не заменять эмодзи» (шаблоны, рассылки): невидимые символы-скобки ---
_NT_OPEN, _NT_CLOSE = "\u2063", "\u2064"
_NT_RE = re.compile(_NT_OPEN + r"(.*?)(?:" + _NT_CLOSE + r"|\Z)", re.DOTALL)


def NT(text):
    """Помечает текст (например, шаблон), в котором автозамену эмодзи делать нельзя."""
    if not text or not isinstance(text, str):
        return text
    if text.startswith(_NT_OPEN) and text.endswith(_NT_CLOSE) and text.count(_NT_OPEN) == 1:
        return text
    return _NT_OPEN + text + _NT_CLOSE


def strip_nt(text):
    if not isinstance(text, str):
        return text
    return text.replace(_NT_OPEN, "").replace(_NT_CLOSE, "")


def emojify_marked(text: str) -> str:
    """Автозамена эмодзи вне блоков NT(...); сами маркеры убираются."""
    if not text:
        return text
    left = [max(0, _MAX_ENTITIES - len(_ENT_TAG_RE.findall(strip_nt(text))))]
    out, pos = [], 0
    for m in _NT_RE.finditer(text):
        out.append(_emojify_piece(text[pos:m.start()], left))
        out.append(m.group(1))
        pos = m.end()
    out.append(_emojify_piece(text[pos:], left))
    return strip_nt("".join(out))


# --- Иконки на кнопках (icon_custom_emoji_id, Bot API 9.4) ---
# Работают, если у владельца бота есть Telegram Premium и aiogram достаточно свежий.
# Если что-то из этого не так, кнопки автоматически возвращаются к обычным эмодзи.
BUTTON_ICONS_ON = os.getenv("TGP_EMOJI_BUTTONS", "1") != "0"   # поле дойдёт до Telegram и в старом aiogram (extra=allow)
TEXT_EMOJI_ON = os.getenv("TGP_EMOJI_TEXT", "1") != "0"        # автозамена эмодзи в HTML-текстах


def _split_lead_emoji(text: str):
    m = _LEAD_EMOJI_RE.match(text)
    if not m:
        return None, text
    return m.group(1), text[m.end():]


def IB(text: str, **kw) -> InlineKeyboardButton:
    """Inline-кнопка: ведущий эмодзи в тексте становится тгп-иконкой кнопки."""
    if BUTTON_ICONS_ON and isinstance(text, str):
        emo, rest = _split_lead_emoji(text)
        if emo and rest:
            return InlineKeyboardButton(text=rest, icon_custom_emoji_id=EMOJI_IDS[_emoji_key(emo)], **kw)
    return InlineKeyboardButton(text=text, **kw)


def KB(text: str, **kw) -> KeyboardButton:
    """Кнопка обычной клавиатуры: то же, что IB. Обработчики ищут текст через contains, эмодзи им не нужен."""
    if BUTTON_ICONS_ON and isinstance(text, str):
        emo, rest = _split_lead_emoji(text)
        if emo and rest:
            return KeyboardButton(text=rest, icon_custom_emoji_id=EMOJI_IDS[_emoji_key(emo)], **kw)
    return KeyboardButton(text=text, **kw)


def _btn_has_icon(b) -> bool:
    return bool(getattr(b, "icon_custom_emoji_id", None))


def _markup_has_icons(markup) -> bool:
    rows = getattr(markup, "inline_keyboard", None) or getattr(markup, "keyboard", None) or []
    return any(_btn_has_icon(b) for row in rows for b in row)


def _strip_icons(markup):
    """Копия клавиатуры без иконок: у inline-кнопок эмодзи возвращается в текст."""
    def fix(b, inline):
        if not _btn_has_icon(b):
            return b
        data = b.model_dump(exclude_none=True)
        icon = data.pop("icon_custom_emoji_id", None)
        if inline and icon and _ID_GLYPH.get(icon):
            data["text"] = f"{_ID_GLYPH[icon]} {data['text']}"
        return type(b)(**data)
    if getattr(markup, "inline_keyboard", None) is not None:
        return markup.model_copy(update={"inline_keyboard": [[fix(b, True) for b in row] for row in markup.inline_keyboard]})
    return markup.model_copy(update={"keyboard": [[fix(b, False) for b in row] for row in markup.keyboard]})


from aiogram.client.session.middlewares.base import BaseRequestMiddleware

_TG_BENIGN = ("not modified", "message to edit not found", "message can't be edited", "chat not found",
              "message to delete not found", "not enough rights", "have no rights", "user not found",
              "query is too old", "message to reply not found", "forbidden", "bot was blocked")


def _text_mode(obj) -> str:
    """'html' - текст уже в HTML; 'plain' - обычный текст без разметки (можно безопасно перевести в HTML); 'skip' - не трогать."""
    if "parse_mode" not in getattr(type(obj), "model_fields", {}):
        return "skip"          # например, всплывающие окна (answerCallbackQuery) эмодзи не поддерживают
    if getattr(obj, "entities", None) or getattr(obj, "caption_entities", None):
        return "skip"
    pm = getattr(obj, "parse_mode", None)
    if str(getattr(pm, "value", pm)) == "HTML":
        return "html"
    if pm is None or type(pm).__name__ == "Default":
        return "plain"
    return "skip"


def _conv_str(value, mode: str):
    """Возвращает (новый_текст, была_ли_замена_эмодзи, нужно_ли_поставить_HTML)."""
    if not isinstance(value, str) or not value:
        return value, False, False
    if not TEXT_EMOJI_ON or mode == "skip":
        return strip_nt(value), False, False
    if mode == "html":
        new = emojify_marked(value)
        return new, new != strip_nt(value), False
    if _NT_OPEN in value or not _EMOJI_RE.search(value):
        return strip_nt(value), False, False
    return emojify(html.escape(value, quote=False)), True, True


def _conv_media(m, plain: bool = False):
    cap = getattr(m, "caption", None)
    if not isinstance(cap, str) or not cap:
        return m, False
    if plain:
        new, used, set_html = strip_nt(cap), False, False
    else:
        new, used, set_html = _conv_str(cap, _text_mode(m))
    if new == cap and not set_html:
        return m, used
    upd = {"caption": new}
    if set_html:
        upd["parse_mode"] = "HTML"
    return m.model_copy(update=upd), used


def _convert_request(method, plain: bool = False):
    """Готовит запрос: автозамена эмодзи в text/caption, чистка маркеров NT, иконки кнопок.
    plain=True даёт «безопасную» версию без замен и без иконок."""
    upd, used_text = {}, False
    mode = _text_mode(method)
    for f in ("text", "caption"):
        v = getattr(method, f, None)
        if isinstance(v, str) and v:
            if plain:
                new, used, set_html = strip_nt(v), False, False
            else:
                new, used, set_html = _conv_str(v, mode)
            if new != v:
                upd[f] = new
                used_text = used_text or used
            if set_html:
                upd["parse_mode"] = "HTML"
    media = getattr(method, "media", None)
    if isinstance(media, (list, tuple)):
        pairs = [_conv_media(m, plain) for m in media]
        if any(p[0] is not m for p, m in zip(pairs, media)):
            upd["media"] = [p[0] for p in pairs]
        used_text = used_text or any(p[1] for p in pairs)
    elif media is not None and hasattr(media, "caption"):
        nm, used = _conv_media(media, plain)
        if nm is not media:
            upd["media"] = nm
        used_text = used_text or used
    markup = getattr(method, "reply_markup", None)
    if markup is not None and (plain or not BUTTON_ICONS_ON) and _markup_has_icons(markup):
        upd["reply_markup"] = _strip_icons(markup)
    return (method.model_copy(update=upd) if upd else method), used_text


class TgpMiddleware(BaseRequestMiddleware):
    """Подменяет обычные эмодзи в HTML-текстах на тгп и следит за иконками на кнопках.
    Если Telegram отверг сообщение из-за этого, отправляет его без замен и выключает сломавшуюся часть."""
    async def __call__(self, make_request, bot, method):
        global BUTTON_ICONS_ON, TEXT_EMOJI_ON
        try:
            new_method, used_text = _convert_request(method)
        except Exception as conv_err:
            logging.warning(f"Автозамена эмодзи пропущена: {conv_err}")
            new_method, used_text = method, False
        used_icons = BUTTON_ICONS_ON and _markup_has_icons(getattr(new_method, "reply_markup", None))
        try:
            return await make_request(bot, new_method)
        except TelegramBadRequest as e:
            low = str(e).lower()
            if (not used_text and not used_icons) or any(x in low for x in _TG_BENIGN):
                raise
            try:
                result = await make_request(bot, _convert_request(method, plain=True)[0])
            except Exception:
                raise e
            about_buttons = "icon" in low or "button" in low
            about_text = "emoji" in low or "entit" in low or "parse" in low
            if used_icons and BUTTON_ICONS_ON and (about_buttons or not about_text):
                BUTTON_ICONS_ON = False
                logging.warning(f"Иконки на кнопках отключены: Telegram их не принял ({e}). Нужен Premium у владельца бота.")
            if used_text and TEXT_EMOJI_ON and (about_text or not about_buttons):
                TEXT_EMOJI_ON = False
                logging.warning(f"Автозамена эмодзи в текстах отключена: Telegram не принял сообщение ({e}).")
            return result


bot.session.middleware(TgpMiddleware())


def get_user_mention(user):
    safe_name = html.escape(user.full_name)
    return f'<a href="tg://user?id={user.id}">{safe_name}</a>'

def is_night_time() -> bool:
    now_msk = datetime.now(MSK_TZ)
    return now_msk.hour >= 22 or now_msk.hour < 10

def is_quiet_hours() -> bool:
    """Тихие часы напоминаний для админов: 00:00-08:00 МСК."""
    return datetime.now(MSK_TZ).hour < 8

def get_night_notice() -> str:
    if is_night_time():
        return f"\n\n{ICON_TIME} <i>Обратите внимание: сейчас ночное время (с 22:00 до 10:00 МСК). Ваша заявка принята, но ответ модераторов может поступить утром!</i>"
    return ""

def normalize_nick(raw: str) -> str:
    """Убирает пробелы по краям и ведущие @ (в поддержку уходит ник без @)."""
    return (raw or "").strip().lstrip("@").strip()

async def lookup_roblox(raw_nick: str):
    """Возвращает (статус, ник_без_@, инфо): 'ok' | 'notfound' | 'error' (Roblox API недоступен)."""
    nick = normalize_nick(raw_nick)
    if not re.fullmatch(r"[A-Za-z0-9_]{3,20}", nick):
        return "notfound", nick, None
    url = "https://users.roblox.com/v1/usernames/users"
    payload = {"usernames": [nick], "excludeBannedUsers": False}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    user_list = data.get("data", [])
                    if user_list:
                        u = user_list[0]
                        return "ok", nick, {"id": u["id"], "name": u["name"], "displayName": u["displayName"]}
                    return "notfound", nick, None
                logging.warning(f"Roblox API вернул {resp.status} при проверке {nick}")
    except Exception as e:
        logging.error(f"Ошибка проверки Roblox профиля {nick}: {e}")
    return "error", nick, None

NICK_NOT_FOUND_TEXT = (
    f"{ICON_CROSS} <b>Аккаунт с ником <code>{{nick}}</code> не найден в Roblox.</b>\n\n"
    "Проверьте написание и отправьте правильный ник. Ник пишется латиницей, цифрами и «_» (от 3 до 20 символов). "
    "Символ <b>@</b> в начале ставить не нужно."
)

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

async def build_admin_hidden_pings() -> str:
    """Собирает ID админов и зашивает их в невидимые спецсимволы tg://user?id= для пуш-созыва."""
    def _get_staff_ids():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT admin_id FROM main_admins
                    UNION
                    SELECT admin_id FROM admin_agents
                """)
                return [row[0] for row in cur.fetchall() if row[0]]

    try:
        staff_ids = await asyncio.to_thread(_get_staff_ids)
    except Exception as e:
        logging.error(f"Ошибка получения списка стаффа: {e}")
        staff_ids = []

    if OWNER_ID and OWNER_ID not in staff_ids:
        staff_ids.append(OWNER_ID)

    return "".join([f'<a href="tg://user?id={uid}">\u2060</a>' for uid in staff_ids])

_member_cache: Dict[int, tuple] = {}

async def in_admin_chat(uid: int) -> bool:
    """Состоит ли человек в группе админов. Результат кэшируется на 10 минут; при сбое API считаем, что состоит (чтобы не потерять людей по ошибке)."""
    if OWNER_ID and uid == OWNER_ID:
        return True
    c = _member_cache.get(uid)
    if c and time.time() - c[1] < 600:
        return c[0]
    try:
        m = await bot.get_chat_member(ADMIN_CHAT_ID, uid)
        ok = m.status not in ("left", "kicked")
    except TelegramBadRequest as e:
        ok = "not found" not in str(e).lower() and "participant" not in str(e).lower()
    except Exception as e:
        logging.warning(f"Не удалось проверить участие {uid} в админ-группе: {e}")
        return True
    _member_cache[uid] = (ok, time.time())
    return ok

async def filter_in_admin_chat(rows: list, key=lambda r: r[0]) -> list:
    return [r for r in rows if await in_admin_chat(key(r))]

async def get_staff_ids() -> list:
    def _q():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT admin_id FROM main_admins
                    UNION
                    SELECT admin_id FROM admin_agents
                """)
                return [row[0] for row in cur.fetchall() if row[0]]
    try:
        ids = await asyncio.to_thread(_q)
    except Exception as e:
        logging.error(f"Ошибка получения списка стаффа: {e}")
        ids = []
    if OWNER_ID and OWNER_ID not in ids:
        ids.append(OWNER_ID)
    return [u for u in ids if await in_admin_chat(u)]

_mention_cache: Dict[int, tuple] = {}

async def _mention_html(uid: int) -> str:
    """Видимая отметка: @username, а если его нет, то имя-ссылка (text mention)."""
    cached = _mention_cache.get(uid)
    if cached and time.time() - cached[1] < 3600:
        return cached[0]
    m = f'<a href="tg://user?id={uid}">админ</a>'
    try:
        ci = await bot.get_chat(uid)
        if ci.username:
            m = f"@{ci.username}"
        elif ci.full_name:
            m = f'<a href="tg://user?id={uid}">{html.escape(ci.full_name)}</a>'
    except Exception:
        pass
    _mention_cache[uid] = (m, time.time())
    return m

async def call_staff(text_html: str, exclude_ids=None):
    """Созыв стаффа в тему «Секретки»: видимые отметки + личные сообщения + (опц.) команда для бота-зазывалы."""
    if TEST_MODE_ON:
        logging.info("[Тест-режим] Созыв стаффа пропущен.")
        return
    exclude = set(exclude_ids or [])
    staff = await get_staff_ids()
    first_msg_id = None

    # 1. видимые отметки в группе (невидимые символы Telegram часто не пингуют). По 5 отметок в сообщении.
    mentions = [await _mention_html(u) for u in staff if u not in exclude]  # публикатора не отмечаем
    chunks = [mentions[i:i + 5] for i in range(0, len(mentions), 5)] or [[]]
    for idx, chunk in enumerate(chunks):
        body = (f"🔔 {text_html}\n\n" if idx == 0 else "") + ("👥 " + " ".join(chunk) if chunk else "")
        try:
            sent = await bot.send_message(
                chat_id=ADMIN_CHAT_ID, text=body.strip(), message_thread_id=TOPIC_SECRETS_ID,
                parse_mode="HTML", link_preview_options=LinkPreviewOptions(is_disabled=True)
            )
            if first_msg_id is None:
                first_msg_id = sent.message_id
        except Exception as e:
            logging.warning(f"Ошибка созыва в тему секреток: {e}")

    # 2. личные сообщения каждому из стаффа (самый надёжный пуш)
    if (await get_setting("call_dm", "false")) == "true":
        link = ""
        if first_msg_id and str(ADMIN_CHAT_ID).startswith("-100"):
            link = f'\n\n<a href="https://t.me/c/{str(ADMIN_CHAT_ID)[4:]}/{TOPIC_SECRETS_ID}/{first_msg_id}">➡️ Перейти к посту в теме «Секретки»</a>'
        for uid in staff:
            if uid in exclude:
                continue
            try:
                await bot.send_message(uid, f"🔔 {text_html}{link}", parse_mode="HTML", link_preview_options=LinkPreviewOptions(is_disabled=True))
            except Exception as e:
                logging.info(f"Личный пинг не доставлен {uid}: {e}")
            await asyncio.sleep(0.05)

    # 3. команда для стороннего бота-зазывалы: боты не видят сообщения друг друга, поэтому её пишет юзербот
    cmd = (await get_setting("call_command", "")) or ""
    if cmd.strip() and userbot:
        try:
            if not userbot.is_connected():
                await userbot.connect()
            await userbot.send_message(ADMIN_CHAT_ID, cmd.strip(), reply_to=TOPIC_SECRETS_ID)
        except Exception as e:
            logging.warning(f"Юзербот не смог отправить команду зазывалы: {e}")

async def get_user_label(uid: int) -> str:
    """@юзернейм, а если его нет, то имя (HTML-экранированное); если не найден, «Неизвестно»."""
    try:
        ci = await bot.get_chat(uid)
        if ci.username:
            return f"@{ci.username}"
        if ci.full_name:
            return html.escape(ci.full_name)
    except Exception:
        pass
    return "Неизвестно"

async def get_prelink_user_ids() -> list:
    def _q():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT user_id FROM prelink_users WHERE expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP ORDER BY added_at ASC")
                return [r[0] for r in cur.fetchall()]
    try:
        return await asyncio.to_thread(_q)
    except Exception as e:
        logging.error(f"Ошибка чтения списка предссылок: {e}")
        return []

async def get_prelink_expiry_map() -> dict:
    def _q():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT user_id, expires_at FROM prelink_users WHERE expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP")
                return {r[0]: r[1] for r in cur.fetchall()}
    try:
        return await asyncio.to_thread(_q)
    except Exception:
        return {}

def fmt_prelink_until(dt) -> str:
    if dt is None:
        return "навсегда"
    return "до " + dt.astimezone(MSK_TZ).strftime("%d.%m.%Y %H:%M") + " МСК"

async def send_prelinks(text: str, photo_id: str = None, exclude_ids=None, delay: int = STAFF_DELAY_SECONDS):
    """Рассылает пост в личку пользователям из списка «Предссылки» одновременно со стаффом (до публикации в канале)."""
    if TEST_MODE_ON:
        return
    exclude = set(exclude_ids or [])
    await asyncio.sleep(delay)  # сначала приоритет у стаффа
    for uid in await get_prelink_user_ids():
        if uid in exclude:
            continue
        try:
            if photo_id:
                await bot.send_photo(uid, photo=photo_id, caption=text, parse_mode="HTML")
            else:
                await bot.send_message(uid, text, parse_mode="HTML", link_preview_options=LinkPreviewOptions(is_disabled=True))
        except Exception as e:
            logging.warning(f"Предссылка не доставлена {uid}: {e}")
        await asyncio.sleep(0.05)

def _prelink_header(what: str) -> str:
    return (
        f"🔔 <b>ПРЕДССЫЛКА: {what}</b>\n"
        f"<i>В канале появится через {CHANNEL_DELAY_SECONDS - STAFF_DELAY_SECONDS} сек. Вы получили её раньше канала!</i>\n\n"
    )

_HTML_TOKEN_RE = re.compile(r"(<[^>]+>|&[#\w]+;)")

def html_visible_len(src: str) -> int:
    n = 0
    for tok in _HTML_TOKEN_RE.split(src):
        if not tok:
            continue
        if tok.startswith("<") and tok.endswith(">"):
            continue
        n += 1 if (tok.startswith("&") and tok.endswith(";")) else len(tok)
    return n

def html_cut(src: str, limit: int) -> str:
    """Обрезает HTML-текст до limit видимых символов, не ломая теги (закрывает открытые)."""
    if html_visible_len(src) <= limit:
        return src
    out, stack, used = [], [], 0
    budget = max(1, limit - 1)  # место под «…»
    for tok in _HTML_TOKEN_RE.split(src):
        if not tok:
            continue
        if tok.startswith("<") and tok.endswith(">"):
            m = re.match(r"</?\s*([a-zA-Z0-9-]+)", tok)
            name = m.group(1).lower() if m else ""
            if tok.startswith("</"):
                if name in stack:
                    while stack:
                        top = stack.pop()
                        out.append(f"</{top}>")
                        if top == name:
                            break
            elif not tok.endswith("/>"):
                stack.append(name)
                out.append(tok)
            continue
        if tok.startswith("&") and tok.endswith(";"):
            if used + 1 > budget:
                break
            out.append(tok)
            used += 1
            continue
        take = budget - used
        if len(tok) > take:
            out.append(tok[:take])
            used = budget
            break
        out.append(tok)
        used += len(tok)
        if used >= budget:
            break
    out.append("…")
    while stack:
        out.append(f"</{stack.pop()}>")
    return "".join(out)

def extract_text_with_custom_emojis(message: Message) -> str:
    """
    Использует нативный HTML-генератор aiogram.
    Корректно сохраняет все цитаты (<blockquote>), жирный, курсив,
    ссылки и Premium-эмодзи (<tg-emoji>) без поломки разметки.
    """
    return message.html_text or message.text or message.caption or ""

async def parse_telethon_custom_emojis(client: UserClient, text: str):
    """
    Преобразует теги <tg-emoji> в MessageEntityCustomEmoji Telethon
    с сохранением цитат, ссылок и всей HTML-разметки.
    Позиции эмодзи точные: каждое эмодзи на время разбора заменяется
    уникальным маркером той же длины (в UTF-16), затем маркер возвращается обратно.
    """
    text = strip_nt(text)
    pattern = re.compile(r"<tg-emoji emoji-id=['\"](\d+)['\"]>(.*?)</tg-emoji>", re.DOTALL)

    clean_html = ""
    last_idx = 0
    emoji_data = []  # (placeholder, inner_char, id)

    for match in pattern.finditer(text):
        clean_html += text[last_idx:match.start()]
        inner_char = match.group(2)
        units = max(1, len(inner_char.encode("utf-16-le")) // 2)
        idx = len(emoji_data)
        if idx < 6000:
            ph = chr(0xE000 + idx) + chr(0xF8FF) * (units - 1)
            clean_html += ph
            emoji_data.append((ph, inner_char, int(match.group(1))))
        else:
            clean_html += inner_char
        last_idx = match.end()
    clean_html += text[last_idx:]

    try:
        parsed_text, entities = await client._parse_message_text(clean_html, "html")
    except Exception as parse_err:
        logging.warning(f"Ошибка Telethon HTML парсера: {parse_err}, парсим без разметки")
        parsed_text = re.sub(r"<[^>]+>", "", clean_html)
        entities = []

    if entities is None:
        entities = []

    for ph, inner_char, e_id in emoji_data:
        pos = parsed_text.find(ph)
        if pos == -1:
            continue
        offset = len(parsed_text[:pos].encode("utf-16-le")) // 2
        entities.append(MessageEntityCustomEmoji(
            offset=offset,
            length=len(ph.encode("utf-16-le")) // 2,
            document_id=e_id
        ))
    for ph, inner_char, _ in emoji_data:
        parsed_text = parsed_text.replace(ph, inner_char, 1)

    entities.sort(key=lambda x: x.offset)
    return parsed_text, entities

# ----------------------------------------------------------------------
# АНТИФЛУД МИДЛВАРЬ
# ----------------------------------------------------------------------
class ThrottlingMiddleware(BaseMiddleware):
    def __init__(self, limit: float = 0.6):
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

            if len(self.users) > 1500:
                self.users = {uid: t for uid, t in self.users.items() if current_time - t < 60}

            if current_time - last_time < self.limit:
                if isinstance(event, CallbackQuery):
                    try:
                        await event.answer("⚠️ Не спамьте кнопками! Подождите секунду.", show_alert=False)
                    except Exception:
                        pass
                return

            self.users[user_id] = current_time

        return await handler(event, data)

# ----------------------------------------------------------------------
# ТЕХНИЧЕСКИЙ ПЕРЕРЫВ
# ----------------------------------------------------------------------
MAINTENANCE_ON = False  # загружается из БД при старте, переключается кнопкой в админ-панели
TEST_MODE_ON = False    # тест-режим: публикации идут в тест-канал, стаффу и предссылкам ничего не шлётся
TEST_CHANNEL_ID = ""    # числовой ID тест-канала (задаёт только владелец)
TEST_STARTED_AT = None  # когда включён тест режим (UTC), для сводки теста

class TestErrorCollector(logging.Handler):
    """Пока включён тест режим, запоминает ошибки из лога для итоговой сводки."""
    def __init__(self):
        super().__init__(level=logging.ERROR)
        self.records: list = []
    def emit(self, record):
        if TEST_MODE_ON and len(self.records) < 50:
            try:
                self.records.append(f"{record.name}: {record.getMessage()}"[:300])
            except Exception:
                pass

test_error_collector = TestErrorCollector()
logging.getLogger().addHandler(test_error_collector)
MAINTENANCE_TEXT = (
    "🛠 <b>Технический перерыв</b>\n\n"
    "Бот временно недоступен: идут технические работы. Пожалуйста, загляните немного позже!"
)

async def can_bypass_maintenance(user_id: int) -> bool:
    if OWNER_ID:
        return user_id == OWNER_ID
    return await is_main_admin(user_id)  # если ADMIN_ID не задан, не запираем главных админов

class MaintenanceMiddleware(BaseMiddleware):
    def __init__(self):
        self.last_notice: Dict[int, float] = {}

    async def __call__(self, handler, event, data):
        if not MAINTENANCE_ON:
            return await handler(event, data)
        user = data.get("event_from_user")
        # работает только для личных чатов: рабочий админ-чат и группы не затрагиваем
        chat = event.chat if isinstance(event, Message) else (event.message.chat if isinstance(event, CallbackQuery) and event.message else None)
        if chat is not None and chat.type != "private":
            return await handler(event, data)
        if user and await can_bypass_maintenance(user.id):
            return await handler(event, data)

        if isinstance(event, CallbackQuery):
            try:
                await event.answer("🛠 Технический перерыв. Бот временно недоступен, загляните позже.", show_alert=True)
            except Exception:
                pass
        elif isinstance(event, Message) and user:
            now = time.time()
            if now - self.last_notice.get(user.id, 0.0) > 60:   # не чаще раза в минуту
                self.last_notice[user.id] = now
                if len(self.last_notice) > 2000:
                    self.last_notice = {u: t for u, t in self.last_notice.items() if now - t < 60}
                try:
                    await event.answer(MAINTENANCE_TEXT, parse_mode="HTML")
                except Exception:
                    pass
        return  # хендлер не вызываем

dp.message.middleware(MaintenanceMiddleware())
dp.callback_query.middleware(MaintenanceMiddleware())
dp.message.middleware(ThrottlingMiddleware(limit=0.6))
dp.callback_query.middleware(ThrottlingMiddleware(limit=0.6))
dp.include_router(router)

# ----------------------------------------------------------------------
# БАЗА ДАННЫХ
# ----------------------------------------------------------------------
if DATABASE_URL:
    db_pool = psycopg2.pool.ThreadedConnectionPool(
        minconn=2,
        maxconn=20,
        dsn=DATABASE_URL,
        sslmode=DB_SSLMODE
    )
else:
    db_pool = psycopg2.pool.ThreadedConnectionPool(
        minconn=2,
        maxconn=20,
        host=DB_HOST,
        port=DB_PORT,
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
        sslmode=DB_SSLMODE
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
                -- ТАБЛИЦЫ ДЛЯ САБОТЕРА
                CREATE TABLE IF NOT EXISTS saboteur_posts (
                    id SERIAL PRIMARY KEY,
                    channel_id TEXT,
                    message_id BIGINT,
                    is_active BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS saboteur_servers (
                    id SERIAL PRIMARY KEY,
                    post_id INTEGER REFERENCES saboteur_posts(id) ON DELETE CASCADE,
                    user_id BIGINT,
                    roblox_nick TEXT,
                    tg_user TEXT,
                    link TEXT,
                    search_method TEXT,
                    accepts_friends TEXT,
                    is_active BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS farm_posts (
                    id SERIAL PRIMARY KEY,
                    channel_id TEXT,
                    message_id BIGINT DEFAULT 0,
                    is_active BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS farm_servers (
                    id SERIAL PRIMARY KEY,
                    post_id INTEGER REFERENCES farm_posts(id) ON DELETE CASCADE,
                    user_id BIGINT,
                    roblox_nick TEXT,
                    tg_user TEXT,
                    sections_count INTEGER,
                    link TEXT,
                    is_active BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
                ALTER TABLE farm_servers ADD COLUMN IF NOT EXISTS accepts_friends TEXT DEFAULT 'Не принимает';
                CREATE TABLE IF NOT EXISTS prelink_users (
                    user_id BIGINT PRIMARY KEY,
                    added_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
                ALTER TABLE prelink_users ADD COLUMN IF NOT EXISTS expires_at TIMESTAMP WITH TIME ZONE;
                ALTER TABLE prelink_users ADD COLUMN IF NOT EXISTS is_test BOOLEAN DEFAULT FALSE;
                CREATE TABLE IF NOT EXISTS mutator_badges (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT,
                    channel_id TEXT,
                    message_id BIGINT,
                    link TEXT,
                    is_expired BOOLEAN DEFAULT FALSE,
                    attempts INTEGER DEFAULT 0,
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
                ALTER TABLE tickets ADD COLUMN IF NOT EXISTS card_msg_id BIGINT DEFAULT NULL;
                ALTER TABLE tickets ADD COLUMN IF NOT EXISTS card_kind TEXT DEFAULT NULL;
                ALTER TABLE tickets ADD COLUMN IF NOT EXISTS link_msg_id BIGINT DEFAULT NULL;
                ALTER TABLE tickets ADD COLUMN IF NOT EXISTS user_close_offered BOOLEAN DEFAULT FALSE;
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
                ON CONFLICT (key) DO NOTHING
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
                ON CONFLICT (key) DO NOTHING
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
                ON CONFLICT (key) DO NOTHING
            """, (default_expired_template,))

            cursor.execute("INSERT INTO settings (key, value) VALUES ('template_saboteur_active', '') ON CONFLICT (key) DO NOTHING")
            cursor.execute("INSERT INTO settings (key, value) VALUES ('template_saboteur_item', '') ON CONFLICT (key) DO NOTHING")
            cursor.execute("INSERT INTO settings (key, value) VALUES ('template_saboteur_closed', '') ON CONFLICT (key) DO NOTHING")

            for _k, _v in (
                ("template_farm_live_active", DEFAULT_FARM_LIVE_ACTIVE),
                ("template_farm_live_item", DEFAULT_FARM_LIVE_ITEM),
                ("template_farm_live_closed", DEFAULT_FARM_LIVE_CLOSED),
                ("template_mutator_active", DEFAULT_MUTATOR_ACTIVE),
                ("template_mutator_expired", DEFAULT_MUTATOR_EXPIRED),
            ):
                cursor.execute(
                    "INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING",
                    (_k, _v)
                )

    logging.info("База данных успешно инициализирована.")

_init_db_sync()

async def setup_bot_commands(bot: Bot):
    try:
        default_commands = [
            BotCommand(command="start", description="🔄 Главное меню поддержки"),
        ]
        await bot.set_my_commands(default_commands, scope=BotCommandScopeAllPrivateChats())

        admin_commands = [
            BotCommand(command="topic_id", description="🔍 Узнать реальный ID текущей темы"),
            BotCommand(command="mystats", description="📊 Моя статистика агента"),
            BotCommand(command="secstats", description="🔮 Моя статистика публикатора"),
            BotCommand(command="supptop", description="🏆 Топ агентов поддержки (Гл. Админ)"),
            BotCommand(command="sectop", description="🏆 Топ искателей за всё время (Гл. Админ)"),
            BotCommand(command="opentickets", description="📋 Неразобранные тикеты (Гл. Админ)"),
            BotCommand(command="worktickets", description="🛠 Тикеты в работе (Гл. Админ)"),
            BotCommand(command="givetik", description="🔄 Передать тикет другому хелперу (/givetik №)"),
            BotCommand(command="note", description="📌 Добавить заметку игроку (Reply)"),
            BotCommand(command="deletenote", description="🗑 Удалить заметку игрока (Гл. Админ)"),
            BotCommand(command="ban", description="🔨 Заблокировать игрока"),
            BotCommand(command="unban", description="🔓 Разблокировать игрока"),
            BotCommand(command="news", description="📢 Рассылка новости всем (Гл. Админ)"),
        ]
        await bot.set_my_commands(admin_commands, scope=BotCommandScopeAllGroupChats())
        await bot.set_my_commands(admin_commands, scope=BotCommandScopeChat(chat_id=ADMIN_CHAT_ID))
        logging.info("Подсказки команд Telegram успешно зарегистрированы.")
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
    except Exception as e:
        logging.error(f"Ошибка проверки статуса участника {user_id}: {e}")
        return False
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

async def get_setting(key: str, default: str = "") -> str:
    val = await _get_setting_raw(key, default)
    # Шаблоны помечаются: автозамена эмодзи их не трогает (в Telethon и в Bot API маркеры убираются)
    return NT(val) if isinstance(key, str) and key.startswith("template_") else val

async def _get_setting_raw(key: str, default: str = "") -> str:
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
        return await filter_in_admin_chat(await asyncio.to_thread(_query))
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
                        "UPDATE tickets SET updated_at = CURRENT_TIMESTAMP, last_actor = %s, reminded_idle = FALSE, last_idle_remind = NULL, user_close_offered = FALSE WHERE ticket_id = %s",
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
                    "UPDATE tickets SET status = 'active', admin_id = %s, last_actor = 'admin', updated_at = CURRENT_TIMESTAMP, reminded_idle = FALSE, last_idle_remind = NULL, user_close_offered = FALSE WHERE ticket_id = %s",
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

def _ticket_url(msg_id) -> str:
    """Ссылка на сообщение в теме поддержки админ-чата."""
    if not msg_id or not str(ADMIN_CHAT_ID).startswith("-100"):
        return ""
    return f"https://t.me/c/{str(ADMIN_CHAT_ID)[4:]}/{TOPIC_SUPPORT_ID}/{msg_id}"

async def get_ticket_links(ticket_ids: list) -> dict:
    """{ticket_id: url} на карточку тикета (для старых тикетов берём самое раннее сообщение из message_map)."""
    if not ticket_ids:
        return {}
    def _q():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT t.ticket_id,
                           COALESCE(t.link_msg_id, (SELECT MIN(m.group_message_id) FROM message_map m WHERE m.ticket_id = t.ticket_id))
                    FROM tickets t WHERE t.ticket_id = ANY(%s)
                """, (list(ticket_ids),))
                return {r[0]: _ticket_url(r[1]) for r in cur.fetchall()}
    try:
        return await asyncio.to_thread(_q)
    except Exception as e:
        logging.error(f"Ошибка получения ссылок тикетов: {e}")
        return {}

def ticket_num_html(ticket_id: int, url: str) -> str:
    """«№123» со встроенной ссылкой на тикет (или просто жирный номер, если ссылки нет)."""
    return f'<a href="{url}"><b>№{ticket_id}</b></a>' if url else f"<b>№{ticket_id}</b>"

async def mark_ticket_card_closed(ticket_id: int, status_line: str):
    """Правит карточку тикета в админ-чате: добавляет статус и убирает кнопки."""
    def _q():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT card_msg_id, card_kind, card_text,
                           (SELECT MIN(m.group_message_id) FROM message_map m WHERE m.ticket_id = tickets.ticket_id)
                    FROM tickets WHERE ticket_id = %s
                """, (ticket_id,))
                return cur.fetchone()
    try:
        row = await asyncio.to_thread(_q)
    except Exception as e:
        logging.error(f"Тикет #{ticket_id}: не удалось найти карточку: {e}")
        return
    if not row:
        return
    msg_id, kind, card_text, first_mapped = row
    if not msg_id:
        msg_id, kind = first_mapped, None   # старый тикет: пробуем самое раннее сообщение
    if not msg_id:
        return
    base = card_text or ""
    try:
        if kind == "ctrl":
            await bot.edit_message_text(chat_id=ADMIN_CHAT_ID, message_id=msg_id, text=status_line, reply_markup=None, parse_mode="HTML")
        elif kind == "photo":
            await bot.edit_message_caption(chat_id=ADMIN_CHAT_ID, message_id=msg_id, caption=html_cut(base + "\n\n" + status_line, 1024), reply_markup=None, parse_mode="HTML")
        elif kind == "text":
            await bot.edit_message_text(chat_id=ADMIN_CHAT_ID, message_id=msg_id, text=html_cut(base + "\n\n" + status_line, 4096), reply_markup=None, parse_mode="HTML")
        else:
            await bot.edit_message_reply_markup(chat_id=ADMIN_CHAT_ID, message_id=msg_id, reply_markup=None)
    except Exception as e:
        logging.warning(f"Тикет #{ticket_id}: не удалось обновить карточку ({e}), пробую убрать только кнопки.")
        try:
            await bot.edit_message_reply_markup(chat_id=ADMIN_CHAT_ID, message_id=msg_id, reply_markup=None)
        except Exception:
            pass

async def user_close_ticket(ticket_id: int, by_offer: bool):
    """Закрытие тикета самим пользователем: БД, карточка и уведомление в админ-чат."""
    info = await get_ticket_info(ticket_id)
    await close_ticket_db(ticket_id, 'closed')
    status_line = f"🔒 <b>Заявка №{ticket_id} закрыта самим пользователем</b>" + (" (админ не отвечал более часа)." if by_offer else ".")
    await mark_ticket_card_closed(ticket_id, status_line)
    try:
        sent = await bot.send_message(
            ADMIN_CHAT_ID,
            f"🔒 Заявка №{ticket_id} закрыта пользователем" + (" (поддержка не отвечала более часа)." if by_offer else " (до взятия в работу)."),
            message_thread_id=TOPIC_SUPPORT_ID, parse_mode="HTML"
        )
        await map_message(sent.message_id, info[0] if info else 0, ticket_id)
    except Exception as e:
        logging.warning(f"Не удалось уведомить админ-чат о закрытии тикета №{ticket_id}: {e}")

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

_chan_cache: Dict[str, str] = {}

async def resolve_channel_id(raw) -> str:
    """Приводит любую запись канала (@name, name, -100..., разный регистр) к числовому ID, чтобы один и тот же канал всегда считался одним."""
    key = str(raw).strip()
    if key in _chan_cache:
        return _chan_cache[key]
    try:
        chat = await bot.get_chat(int(key) if re.fullmatch(r"-?\d+", key) else key)
        _chan_cache[key] = str(chat.id)
        return _chan_cache[key]
    except Exception as e:
        logging.warning(f"Не удалось определить ID канала {key}: {e}")
        return key.lower()

async def check_channel_rights(bot: Bot, user_id: int) -> str | None:
    if TEST_MODE_ON and TEST_CHANNEL_ID:
        return TEST_CHANNEL_ID   # тест-режим: все публикации только в тест-канал
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
        return await resolve_channel_id(ch_id)
    except Exception:
        return None

async def expire_secret_post(bot: Bot, post_id: int, channel_id: str, message_id: int, s_type: str):
    def _mark():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE secret_posts SET is_expired = TRUE WHERE id = %s", (post_id,))
    await asyncio.to_thread(_mark)

    if not message_id:
        logging.warning(f"Секретка №{post_id}: поста в канале нет (публикация не удалась), правка истёкшего поста пропущена.")
        return

    expired_tmpl = await get_setting("template_expired", "❕Секретка❕\nТип: [Тип_Секретки]\n\nВремя вышло!")
    final_text = expired_tmpl.replace("[Тип_Секретки]", s_type)
    
    try:
        if userbot:
            if not userbot.is_connected():
                await userbot.connect()

            target_peer = channel_id
            if isinstance(target_peer, str) and (target_peer.startswith("-100") or target_peer.lstrip("-").isdigit()):
                target_peer = int(target_peer)

            parsed_text, entities = await parse_telethon_custom_emojis(userbot, final_text)
            await userbot.edit_message(
                entity=target_peer,
                message=message_id,
                text=parsed_text,
                formatting_entities=entities,
                link_preview=False
            )
        else:
            await bot.edit_message_caption(
                chat_id=channel_id,
                message_id=message_id,
                caption=final_text,
                parse_mode="HTML"
            )
    except Exception as e:
        logging.error(f"Не удалось обновить истекший пост №{post_id}: {e}")

async def publish_to_channel_delayed(channel_id: str, photo_id: str, caption: str, post_id: int, delay: int = CHANNEL_DELAY_SECONDS):
    logging.info(f"[Секретка #{post_id}] Ожидание форы {delay} сек перед отправкой в {channel_id}...")
    await asyncio.sleep(delay)
    channel_msg_id = 0
    try:
        if userbot:
            if not userbot.is_connected():
                logging.info(f"[Секретка #{post_id}] Переподключение юзербота к Telegram...")
                await userbot.connect()

            logging.info(f"[Секретка #{post_id}] Скачивание фото {photo_id}...")
            file_info = await bot.get_file(photo_id)
            photo_buf = io.BytesIO()
            await bot.download_file(file_info.file_path, destination=photo_buf)
            photo_buf.seek(0)
            photo_buf.name = "secret.jpg"   # без записи на диск хоста

            logging.info(f"[Секретка #{post_id}] Парсинг разметки и кастомных эмодзи...")
            parsed_text, entities = await parse_telethon_custom_emojis(userbot, caption)

            target_peer = channel_id
            if isinstance(target_peer, str) and (target_peer.startswith("-100") or target_peer.lstrip("-").isdigit()):
                target_peer = int(target_peer)

            logging.info(f"[Секретка #{post_id}] Отправка через Telethon в {target_peer}...")
            sent_msg = await userbot.send_file(
                entity=target_peer,
                file=photo_buf,
                caption=parsed_text,
                formatting_entities=entities
            )
            channel_msg_id = sent_msg.id
            logging.info(f"Секретка #{post_id} успешно опубликована Premium-аккаунтом в канале {channel_id} (msg_id={channel_msg_id})!")
        else:
            logging.warning(f"[Секретка #{post_id}] Юзербот не активен, отправка стандартным ботом...")
            sent_channel = await bot.send_photo(
                chat_id=channel_id,
                photo=photo_id,
                caption=caption,
                parse_mode="HTML"
            )
            channel_msg_id = sent_channel.message_id
            logging.info(f"Секретка #{post_id} опубликована ботом в канале {channel_id}.")

        def _update_msg_id():
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE secret_posts SET message_id = %s WHERE id = %s",
                        (channel_msg_id, post_id)
                    )
        await asyncio.to_thread(_update_msg_id)
    except Exception as e:
        logging.error(f"ОШИБКА публикации в канал по посту #{post_id}: {e}", exc_info=True)
        if userbot and not channel_msg_id:
            # запасной путь: обычный бот (без премиум-эмодзи), чтобы пост не пропал
            try:
                sent_channel = await bot.send_photo(chat_id=channel_id, photo=photo_id, caption=caption, parse_mode="HTML")
                channel_msg_id = sent_channel.message_id
                def _upd_fb():
                    with get_db() as conn:
                        with conn.cursor() as cur:
                            cur.execute("UPDATE secret_posts SET message_id = %s WHERE id = %s", (channel_msg_id, post_id))
                await asyncio.to_thread(_upd_fb)
                logging.warning(f"Секретка #{post_id} опубликована запасным путём (ботом).")
            except Exception as e2:
                logging.error(f"Запасная публикация поста #{post_id} тоже не удалась: {e2}")

# ----------------------------------------------------------------------
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ САБОТЕРА
# ----------------------------------------------------------------------
async def get_active_saboteur_post(channel_id: str) -> dict | None:
    def _q():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id, channel_id, message_id FROM saboteur_posts WHERE is_active = TRUE ORDER BY id DESC")
                return cur.fetchall()
    target = await resolve_channel_id(channel_id)
    for pid, ch, mid in await asyncio.to_thread(_q):
        if await resolve_channel_id(ch) == target:
            return {"id": pid, "channel_id": ch, "message_id": mid}
    return None

async def get_active_saboteur_servers(post_id: int) -> list:
    def _q():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id, user_id, roblox_nick, tg_user, link, search_method, accepts_friends 
                    FROM saboteur_servers 
                    WHERE post_id = %s AND is_active = TRUE 
                    ORDER BY id ASC
                """, (post_id,))
                return cur.fetchall()
    return await asyncio.to_thread(_q)

async def count_user_active_saboteurs(user_id: int) -> int:
    def _q():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT COUNT(*) 
                    FROM saboteur_servers s
                    JOIN saboteur_posts p ON s.post_id = p.id
                    WHERE s.user_id = %s AND s.is_active = TRUE AND p.is_active = TRUE
                """, (user_id,))
                return cur.fetchone()[0]
    return await asyncio.to_thread(_q)

async def render_saboteur_post_text(post_id: int) -> str:
    servers = await get_active_saboteur_servers(post_id)
    if not servers:
        tmpl_closed = await get_setting("template_saboteur_closed", "🕵️ <b>Поиск саботеров завершен!</b>")
        return tmpl_closed

    tmpl_active = await get_setting("template_saboteur_active", "🕵️ <b>Поиск саботера</b>\n\n[Список_Серверов]")
    tmpl_item = await get_setting("template_saboteur_item", "🔹 <b>Сервер #[Номер_Сервера]:</b>\n• Искатель: [Искатель]\n• Ник в Roblox: [Ник_Роблокс]\n• Метод: [Метод]\n• В друзья: [Друзья]\n• Ссылка: [Ссылка]\n")

    items_text_list = []
    for idx, s_row in enumerate(servers, 1):
        s_id, u_id, r_nick, tg_u, lnk, meth, friends = s_row
        item_str = tmpl_item.replace("[Номер_Сервера]", str(idx))
        item_str = item_str.replace("[Ник_Роблокс]", r_nick)
        item_str = item_str.replace("[Искатель]", tg_u)
        item_str = item_str.replace("[Метод]", meth)
        item_str = item_str.replace("[Друзья]", friends)
        item_str = item_str.replace("[Ссылка]", lnk)
        items_text_list.append(item_str.strip())

    servers_block = "\n\n".join(items_text_list)
    return tmpl_active.replace("[Список_Серверов]", servers_block)

async def publish_or_update_saboteur_channel(channel_id: str, post_id: int):
    final_text = await render_saboteur_post_text(post_id)
    
    def _get_post_info():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id, channel_id, message_id FROM saboteur_posts WHERE id = %s", (post_id,))
                r = cur.fetchone()
                return {"id": r[0], "channel_id": r[1], "message_id": r[2]} if r else None
    current_post = await asyncio.to_thread(_get_post_info)

    target_peer = channel_id
    if isinstance(target_peer, str) and (target_peer.startswith("-100") or target_peer.lstrip("-").isdigit()):
        target_peer = int(target_peer)

    if userbot:
        if not userbot.is_connected():
            await userbot.connect()
        parsed_text, entities = await parse_telethon_custom_emojis(userbot, final_text)

        # Редактируем существующий пост (с отключением предпросмотра ссылок link_preview=False)[cite: 2]
        if current_post and current_post["message_id"] != 0:
            await userbot.edit_message(
                entity=target_peer,
                message=current_post["message_id"],
                text=parsed_text,
                formatting_entities=entities,
                link_preview=False
            )
            logging.info(f"Пост саботера #{post_id} успешно обновлен в канале {channel_id}.")
        else:
            sent_msg = await userbot.send_message(
                entity=target_peer,
                message=parsed_text,
                formatting_entities=entities,
                link_preview=False
            )
            def _upd_mid():
                with get_db() as conn:
                    with conn.cursor() as cur:
                        cur.execute("UPDATE saboteur_posts SET message_id = %s WHERE id = %s", (sent_msg.id, post_id))
            await asyncio.to_thread(_upd_mid)
            logging.info(f"Новый пост саботера #{post_id} опубликован в канале {channel_id} (msg_id={sent_msg.id}).")
    else:
        if current_post and current_post["message_id"] != 0:
            await bot.edit_message_text(
                chat_id=channel_id,
                message_id=current_post["message_id"],
                text=final_text,
                parse_mode="HTML",
                link_preview_options=LinkPreviewOptions(is_disabled=True)
            )
        else:
            sent_b = await bot.send_message(
                chat_id=channel_id,
                text=final_text,
                parse_mode="HTML",
                link_preview_options=LinkPreviewOptions(is_disabled=True)
            )
            def _upd_mid_b():
                with get_db() as conn:
                    with conn.cursor() as cur:
                        cur.execute("UPDATE saboteur_posts SET message_id = %s WHERE id = %s", (sent_b.message_id, post_id))
            await asyncio.to_thread(_upd_mid_b)

async def publish_saboteur_delayed(channel_id: str, post_id: int, delay: int = CHANNEL_DELAY_SECONDS):
    logging.info(f"[Саботер #{post_id}] Ожидание форы {delay} сек перед публикацией...")
    await asyncio.sleep(delay)
    async with saboteur_lock:
        try:
            await publish_or_update_saboteur_channel(channel_id, post_id)
        except Exception as e:
            logging.error(f"Ошибка отложенной публикации саботера #{post_id}: {e}", exc_info=True)

# ----------------------------------------------------------------------
# СОСТОЯНИЯ (FSM)
# ----------------------------------------------------------------------
class Form(StatesGroup):
    complaint_nicknames = State()
    complaint_place = State()
    complaint_time = State()
    complaint_reason = State()
    complaint_photos = State()
    
    appeal_nickname = State()
    appeal_place = State()
    appeal_time = State()
    appeal_reason = State()
    appeal_photos = State()
    
    friends_nickname = State()
    question_content = State()
    set_friend_nick = State()
    
    helper_name = State()
    helper_age = State()
    helper_time = State()
    helper_why = State()

    secret_name = State()
    secret_age = State()
    secret_vip = State()
    secret_time = State()
    secret_why = State()

    add_main_admin_id = State()
    del_main_admin_id = State()

class SecretPublisherStates(StatesGroup):
    waiting_for_channel = State()
    waiting_for_mode = State()
    waiting_for_type = State()
    waiting_for_farm_choice = State()
    waiting_for_photo = State()
    waiting_for_link = State()
    waiting_for_confirm = State()

class SaboteurPublisherStates(StatesGroup):
    waiting_for_nick = State()
    waiting_for_method = State()
    waiting_for_friends = State()
    waiting_for_link = State()
    waiting_for_confirm = State()

class MutatorPublisherStates(StatesGroup):
    waiting_for_link = State()
    waiting_for_confirm = State()

class FarmPublisherStates(StatesGroup):
    waiting_for_nick = State()
    waiting_for_sections = State()
    waiting_for_friends = State()
    waiting_for_link = State()
    waiting_for_confirm = State()

class SecretAdminStates(StatesGroup):
    add_publisher_id = State()
    del_publisher_id = State()
    add_type_name = State()
    add_type_declined = State()
    add_special_type_name = State()
    set_timer = State()
    template_active = State()
    template_farm = State()
    template_expired = State()
    template_saboteur_active = State()
    template_saboteur_item = State()
    template_saboteur_closed = State()
    template_farm_live_active = State()
    template_farm_live_item = State()
    template_farm_live_closed = State()
    template_mutator_active = State()
    template_mutator_expired = State()
    prelink_add = State()
    call_command = State()
    test_channel = State()
    prelink_duration = State()

# ----------------------------------------------------------------------
# КЛАВИАТУРЫ
# ----------------------------------------------------------------------
BTN_COMPLAINT = "🚨 Жалоба на игрока"
BTN_APPEAL = "😡 Обжалование бана"
BTN_FRIENDS = "👯‍♀️ Добавление в друзья (VIP)"
BTN_QUESTION = "❓ Задать вопрос"
BTN_HELPER_APPLY = "📝 Подать заявку на хелпера"
BTN_SECRET_APPLY = "🔍 Набор в искатели секреток"
BTN_ADMIN_PANEL = "⚙ Админ-панель"
BTN_REFRESH = "🔄 Перезагрузить меню"
BTN_SECRETS = "🔮 Секретки"

def btn_texts(btn: str) -> list:
    """Текст кнопки меню: с эмодзи и без (когда эмодзи вынесен в иконку кнопки)."""
    out = [btn]
    if " " in btn:
        out.append(btn.split(" ", 1)[1])
    return out

async def main_keyboard(user_id: int):
    keyboard = [
        [KB(text=BTN_COMPLAINT), KB(text=BTN_APPEAL)],
        [KB(text=BTN_FRIENDS), KB(text=BTN_QUESTION)],
        [KB(text=BTN_HELPER_APPLY), KB(text=BTN_SECRET_APPLY)]
    ]
    
    if await is_secret_publisher(user_id):
        keyboard.append([KB(text=BTN_SECRETS)])

    keyboard.append([KB(text=BTN_REFRESH)])

    if await is_main_admin(user_id):
        keyboard.append([KB(text=BTN_ADMIN_PANEL)])

    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True, persistent=True)

async def admin_panel_kb():
    f_active = await get_setting("friend_active", "false")
    h_active = await get_setting("helper_recruitment", "true")
    s_active = await get_setting("secret_recruitment", "true")
    
    friend_status = "🟢 Вкл" if f_active == "true" else "🔴 Выкл"
    helper_status = "🟢 Вкл" if h_active == "true" else "🔴 Выкл"
    secret_status = "🟢 Вкл" if s_active == "true" else "🔴 Выкл"
    maint_status = "🔴 ВКЛЮЧЁН" if MAINTENANCE_ON else "🟢 Выкл"
    test_status = "🧪 ВКЛЮЧЁН" if TEST_MODE_ON else "Выкл"
    
    return InlineKeyboardMarkup(inline_keyboard=[
        [IB(text="📊 Статистика поддержки", callback_data="adm_stats"),
         IB(text="👥 Статистика хелперов", callback_data="adm_list_stats")],
        [IB(text=f"👯‍♀️ Добавление в друзья: {friend_status}", callback_data="toggle_friend")],
        [IB(text=f"📝 Набор в хелперы: {helper_status}", callback_data="toggle_helper")],
        [IB(text=f"🔍 Искатели секреток: {secret_status}", callback_data="toggle_secret")],
        [IB(text="👑 Главные админы поддержки", callback_data="manage_main_admins")],
        [IB(text="✏ Ник для друзей", callback_data="change_friend_nick")],
        [IB(text="🔮 Управление Секретками (Панель)", callback_data="adm_secret_panel")],
        [IB(text=f"🛠 Тех. перерыв: {maint_status}", callback_data="toggle_maintenance")],
        [IB(text=f"🧪 Тест режим: {test_status}", callback_data="toggle_testmode"),
         IB(text="📡 Канал тест-режима", callback_data="set_test_channel")]
    ])

def secret_admin_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [IB(text="👥 Управление публикаторами", callback_data="sec_manage_pubs")],
        [IB(text="📊 Статистика постов (24ч)", callback_data="sec_stats")],
        [IB(text="📝 Редактировать шаблоны", callback_data="sec_templates")],
        [IB(text="➕ Добавить обычный тип", callback_data="sec_add_type"),
         IB(text="🗑 Удалить обычный тип", callback_data="sec_del_type")],
        [IB(text="➕ Добавить особый тип", callback_data="sec_add_spec_type"),
         IB(text="🗑 Удалить особый тип", callback_data="sec_del_spec_type")],
        [IB(text="⏱ Настроить таймер", callback_data="sec_set_timer")],
        [IB(text="🔔 Предссылки", callback_data="sec_prelinks")],
        [IB(text="📲 Личные пинги стаффу: вкл/выкл", callback_data="sec_toggle_calldm")],
        [IB(text="📣 Команда для бота-зазывалы", callback_data="sec_call_cmd")],
        [IB(text="🔙 Назад в админку", callback_data="back_to_adm")]
    ])

def publisher_menu_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [IB(text="📥 Отправить секретку", callback_data="pub_send_secret")],
        [IB(text="🕵️ Поиск саботера", callback_data="pub_send_saboteur")],
        [IB(text="🌾 Фарм сервер", callback_data="pub_send_farm")],
        [IB(text="🧬 Бейдж мутатор", callback_data="pub_send_mutator")],
        [IB(text="📋 Мои секретки", callback_data="pub_my_posts"),
         IB(text="📋 Мои сервера саботера", callback_data="pub_my_saboteurs")],
        [IB(text="📋 Мои сервера фарма", callback_data="pub_my_farms")],
        [IB(text="📢 Привязать канал", callback_data="pub_bind_channel")]
    ])

def take_ticket_kb(ticket_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[[
        IB(text="🟢 Начать работу", callback_data=f"take_{ticket_id}"),
        IB(text="🔴 Отклонить тикет", callback_data=f"reject_{ticket_id}")
    ]])

def close_ticket_kb(ticket_id: int, admin_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[[
        IB(text="🔒 Закрыть тикет", callback_data=f"close_{ticket_id}_{admin_id}")
    ]])

def user_cancel_kb(ticket_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[[
        IB(text="❌ Закрыть заявку", callback_data=f"user_cancel_{ticket_id}")
    ]])

def rating_kb(ticket_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[[
        IB(text="⭐ 1", callback_data=f"rate_{ticket_id}_1"),
        IB(text="⭐ 2", callback_data=f"rate_{ticket_id}_2"),
        IB(text="⭐ 3", callback_data=f"rate_{ticket_id}_3"),
        IB(text="⭐ 4", callback_data=f"rate_{ticket_id}_4"),
        IB(text="⭐ 5", callback_data=f"rate_{ticket_id}_5"),
    ]])

def helper_decision_kb(user_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[[
        IB(text="🟢 Принять", callback_data=f"helper_accept_{user_id}"),
        IB(text="🔴 Отклонить", callback_data=f"helper_reject_{user_id}")
    ]])

def secret_decision_kb(user_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[[
        IB(text="🟢 Принять", callback_data=f"secret_accept_{user_id}"),
        IB(text="🔴 Отклонить", callback_data=f"secret_reject_{user_id}")
    ]])

def skip_photo_kb(target: str):
    return InlineKeyboardMarkup(inline_keyboard=[[
        IB(text="⏩ Пропустить прикрепление фото", callback_data=f"skip_photo_{target}")
    ]])

def complaint_servers_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [IB(text="🌐 Сервер с секреткой", callback_data="cmp_srv_secret")],
        [IB(text="🕵 Поиск саботера", callback_data="cmp_srv_saboteur")],
        [IB(text="🌾 Фарм сервер", callback_data="cmp_srv_farm")]
    ])

def appeal_servers_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [IB(text="🌐 Сервер с секреткой", callback_data="app_srv_secret")],
        [IB(text="🕵️ Поиск саботера", callback_data="app_srv_saboteur")],
        [IB(text="🌾 Фарм сервер", callback_data="app_srv_farm")]
    ])

# ----------------------------------------------------------------------
# ОТПРАВКА БИЛЕТА С МЕДИА (В ТЕМУ ПОДДЕРЖКИ)
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

    def _is_vid(x): return isinstance(x, str) and x.startswith("VID:")
    def _fid(x): return x[4:] if _is_vid(x) else x
    def _input_media(x, **kw):
        return InputMediaVideo(media=_fid(x), **kw) if _is_vid(x) else InputMediaPhoto(media=_fid(x), **kw)

    async def _delete_ticket_row():
        def _d():
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM tickets WHERE ticket_id = %s", (ticket_id,))
        try:
            await asyncio.to_thread(_d)
        except Exception as _e:
            logging.error(f"Тикет #{ticket_id}: не удалось откатить запись: {_e}")

    long_card = html_visible_len(admin_card_text) > 1000
    card_html = html_cut(admin_card_text, 4000)
    try:
        if photos:
            if len(photos) == 1 and not long_card:
                if _is_vid(photos[0]):
                    sent = await bot.send_video(
                        ADMIN_CHAT_ID, video=_fid(photos[0]), caption=admin_card_text,
                        reply_markup=take_ticket_kb(ticket_id), message_thread_id=TOPIC_SUPPORT_ID, parse_mode="HTML"
                    )
                else:
                    sent = await bot.send_photo(
                        ADMIN_CHAT_ID, photo=photos[0], caption=admin_card_text,
                        reply_markup=take_ticket_kb(ticket_id), message_thread_id=TOPIC_SUPPORT_ID, parse_mode="HTML"
                    )
                await map_message(sent.message_id, user_id, ticket_id)
                _card_msg, _card_kind, _link_msg = sent.message_id, "photo", sent.message_id
            elif len(photos) == 1:
                # длинный текст: медиа отдельно, карточка отдельным сообщением
                if _is_vid(photos[0]):
                    sm = await bot.send_video(ADMIN_CHAT_ID, video=_fid(photos[0]), message_thread_id=TOPIC_SUPPORT_ID)
                else:
                    sm = await bot.send_photo(ADMIN_CHAT_ID, photo=photos[0], message_thread_id=TOPIC_SUPPORT_ID)
                await map_message(sm.message_id, user_id, ticket_id)
                sent = await bot.send_message(
                    ADMIN_CHAT_ID, card_html, reply_markup=take_ticket_kb(ticket_id),
                    message_thread_id=TOPIC_SUPPORT_ID, parse_mode="HTML"
                )
                await map_message(sent.message_id, user_id, ticket_id)
                _card_msg, _card_kind, _link_msg = sent.message_id, "text", sm.message_id
            else:
                if long_card:
                    media = [_input_media(p) for p in photos]
                else:
                    media = [_input_media(photos[0], caption=admin_card_text, parse_mode="HTML")]
                    for p in photos[1:]:
                        media.append(_input_media(p))
                sent_group = await bot.send_media_group(ADMIN_CHAT_ID, media=media, message_thread_id=TOPIC_SUPPORT_ID)
                for m in sent_group:
                    await map_message(m.message_id, user_id, ticket_id)
                if long_card:
                    ctrl_msg = await bot.send_message(
                        ADMIN_CHAT_ID, card_html, reply_markup=take_ticket_kb(ticket_id),
                        message_thread_id=TOPIC_SUPPORT_ID, parse_mode="HTML"
                    )
                    _kind = "text"
                else:
                    ctrl_msg = await bot.send_message(
                        ADMIN_CHAT_ID,
                        f"👆 <i>Управление заявкой №{ticket_id} (прикреплено файлов: {len(photos)}):</i>",
                        reply_markup=take_ticket_kb(ticket_id), message_thread_id=TOPIC_SUPPORT_ID, parse_mode="HTML"
                    )
                    _kind = "ctrl"
                await map_message(ctrl_msg.message_id, user_id, ticket_id)
                _card_msg, _card_kind, _link_msg = ctrl_msg.message_id, _kind, sent_group[0].message_id
        else:
            sent = await bot.send_message(
                ADMIN_CHAT_ID, card_html, reply_markup=take_ticket_kb(ticket_id),
                message_thread_id=TOPIC_SUPPORT_ID, parse_mode="HTML"
            )
            await map_message(sent.message_id, user_id, ticket_id)
            _card_msg, _card_kind, _link_msg = sent.message_id, "text", sent.message_id
    except Exception:
        await _delete_ticket_row()
        raise

    def _save_card_ref():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE tickets SET card_msg_id = %s, card_kind = %s, link_msg_id = %s WHERE ticket_id = %s",
                    (_card_msg, _card_kind, _link_msg, ticket_id)
                )
    try:
        await asyncio.to_thread(_save_card_ref)
    except Exception as e:
        logging.error(f"Тикет #{ticket_id}: не удалось сохранить ссылку на карточку: {e}")

    return ticket_id

# ----------------------------------------------------------------------
# СТАРТ И МЕНЮ
# ----------------------------------------------------------------------
@router.message(CommandStart(), F.chat.type == "private")
async def start_cmd(message: Message, state: FSMContext):
    try:
        await register_user(message.from_user.id)
    except Exception as e:
        logging.error(f"Ошибка при регистрации {message.from_user.id}: {e}")

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

    if os.path.exists(PHOTO_FILENAME):
        try:
            photo_file = FSInputFile(PHOTO_FILENAME)
            await message.answer_photo(
                photo=photo_file,
                caption=welcome_caption,
                reply_markup=kb,
                parse_mode="HTML"
            )
            return
        except Exception as e:
            logging.error(f"Не удалось отправить локальное фото {PHOTO_FILENAME}: {e}")

    await message.answer(
        text=welcome_caption,
        reply_markup=kb,
        parse_mode="HTML"
    )

@router.message(F.text.in_(btn_texts(BTN_REFRESH)), F.chat.type == "private")
async def refresh_menu_handler(message: Message, state: FSMContext):
    await register_user(message.from_user.id)
    if await is_banned(message.from_user.id): return
    await state.clear()
    kb = await main_keyboard(message.from_user.id)
    await message.answer("<tg-emoji emoji-id='5877410604225924969'>🔄</tg-emoji> <b>Меню обновлено!</b>", reply_markup=kb, parse_mode="HTML")

@router.message(F.text.in_(btn_texts(BTN_ADMIN_PANEL)), F.chat.type == "private")
async def open_admin_panel(message: Message, state: FSMContext):
    if not await is_main_admin(message.from_user.id):
        return
    await state.clear()
    kb = await admin_panel_kb()
    await message.answer("<tg-emoji emoji-id='5877260593903177342'>⚙️</tg-emoji> <b>Панель администратора</b>", reply_markup=kb, parse_mode="HTML")

@router.callback_query(F.data == "adm_stats")
async def callback_stats(call: CallbackQuery):
    if not await is_main_admin(call.from_user.id):
        await call.answer("❌ Нет доступа", show_alert=True)
        return
    users_count = await get_all_users_count()
    total, closed, rejected, avg_rating = await get_tickets_stats()

    stats_text = (
        "<tg-emoji emoji-id='5913702317667913862'>📊</tg-emoji> <b>Общая статистика поддержки:</b>\n\n"
        f"{ICON_USERS} Активных пользователей: <code>{users_count}</code>\n"
        f"<tg-emoji emoji-id='5346024704765347251'>🎫</tg-emoji> Всего тикетов: <code>{total}</code>\n"
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

    back_kb = InlineKeyboardMarkup(inline_keyboard=[[IB(text="🔙 Назад", callback_data="back_to_adm")]])
    await call.message.edit_text(text, reply_markup=back_kb, parse_mode="HTML")
    await call.answer()

@router.callback_query(F.data == "back_to_adm")
async def back_to_admin_panel(call: CallbackQuery):
    if not await is_main_admin(call.from_user.id): return
    kb = await admin_panel_kb()
    await call.message.edit_text("<tg-emoji emoji-id='5877260593903177342'>⚙️</tg-emoji> <b>Панель администратора</b>", reply_markup=kb, parse_mode="HTML")
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

    text = "<tg-emoji emoji-id='5807868868886009920'>👑</tg-emoji> <b>Главные администраторы бота:</b>\n\n"
    for r in res:
        text += f"• <code>{r[0]}</code> - {await get_user_label(r[0])}\n"
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [IB(text="➕ Добавить главного админа", callback_data="add_main_admin_start")],
        [IB(text="➖ Удалить главного админа", callback_data="del_main_admin_start")],
        [IB(text="🔙 Назад", callback_data="back_to_adm")]
    ])
    await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await call.answer()

@router.callback_query(F.data == "add_main_admin_start")
async def add_main_admin_start(call: CallbackQuery, state: FSMContext):
    if OWNER_ID and call.from_user.id != OWNER_ID:
        await call.answer("❌ Доступно только создателю бота!", show_alert=True)
        return
    await state.set_state(Form.add_main_admin_id)
    await call.message.answer("<tg-emoji emoji-id='5346004239246183111'>➕</tg-emoji> Введите <b>Telegram ID</b> пользователя, которого хотите сделать главным администратором:", parse_mode="HTML")
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
    await call.message.answer("<tg-emoji emoji-id='5782743443348394021'>➖</tg-emoji> Введите <b>Telegram ID</b> главного администратора, которого хотите снять:", parse_mode="HTML")
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
        await message.answer(f"{ICON_CROSS} Введите числовой Telegram ID:", parse_mode="HTML")

async def run_selfcheck() -> str:
    """Мини-тест систем бота. Каждая проверка независима: сбой одной не мешает остальным."""
    results = []   # (иконка, текст)
    def ok(t): results.append(("✅", t))
    def warn(t): results.append(("⚠️", t))
    def bad(t): results.append(("❌", t))

    # 1. База данных
    try:
        t0 = time.time()
        def _db():
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                    for tbl in ("tickets", "secret_posts", "saboteur_posts", "farm_posts", "mutator_badges", "prelink_users", "settings"):
                        cur.execute(f"SELECT 1 FROM {tbl} LIMIT 1")
        await asyncio.to_thread(_db)
        ok(f"База данных отвечает ({int((time.time() - t0) * 1000)} мс), ключевые таблицы на месте")
    except Exception as e:
        bad(f"База данных: {html.escape(str(e))[:150]}")

    # 2. Бот и админ-группа
    try:
        me = await bot.get_me()
        ok(f"Бот на связи: @{me.username}")
        try:
            m = await bot.get_chat_member(ADMIN_CHAT_ID, bot.id)
            if m.status in ("administrator", "creator"):
                ok("Бот — админ в группе админов")
            else:
                bad(f"Бот не админ в группе админов (статус: {m.status})")
        except Exception as e:
            bad(f"Группа админов недоступна: {html.escape(str(e))[:150]}")
    except Exception as e:
        bad(f"Бот: {html.escape(str(e))[:150]}")

    # 3. Юзербот
    ub_ok = False
    if not userbot:
        bad("Юзербот не настроен (нет USER_SESSION_STRING / TELEGRAM_API_ID / TELEGRAM_API_HASH): фарм и публикации через Premium не заработают")
    else:
        try:
            if not userbot.is_connected():
                await userbot.connect()
            if await userbot.is_user_authorized():
                u = await userbot.get_me()
                ub_ok = True
                ok(f"Юзербот авторизован: {html.escape(u.first_name or '')} (@{u.username or '—'})")
                if not getattr(u, "premium", False):
                    warn("У аккаунта юзербота нет Premium: кастомные эмодзи могут не отображаться")
            else:
                bad("Сессия юзербота недействительна, нужна новая строка сессии")
        except Exception as e:
            bad(f"Юзербот: {html.escape(str(e))[:150]}")

    # 4. Тест-канал
    if not TEST_CHANNEL_ID:
        bad("Тест-канал не задан")
    else:
        try:
            bm = await bot.get_chat_member(int(TEST_CHANNEL_ID), bot.id)
            if bm.status == "creator" or (bm.status == "administrator" and getattr(bm, "can_post_messages", True)):
                ok("Бот — админ тест-канала с правом публикации")
            else:
                bad(f"У бота нет права публиковать в тест-канале (статус: {bm.status})")
        except Exception as e:
            bad(f"Тест-канал (бот): {html.escape(str(e))[:150]}")
        if ub_ok:
            try:
                perms = await userbot.get_permissions(int(TEST_CHANNEL_ID), "me")
                if getattr(perms, "is_creator", False) or (perms.is_admin and getattr(perms, "post_messages", True)):
                    ok("Юзербот может публиковать в тест-канале")
                else:
                    bad("Юзербот не может публиковать в тест-канале (нужен админ с правом публикации)")
            except Exception as e:
                bad(f"Тест-канал (юзербот): {html.escape(str(e))[:150]}")

    # 5. Файлы
    for fn in ("mutator.jpg", PHOTO_FILENAME):
        if os.path.exists(fn):
            ok(f"Файл {fn} найден ({os.path.getsize(fn) // 1024} КБ)")
        else:
            warn(f"Файл {fn} не найден рядом с ботом")

    # 6. Шаблоны
    try:
        checks = [
            ("template_active", "Обычный активный пост", ["[Ссылка]"], 1024),
            ("template_farm", "Фарм-секретка", ["[Ссылка]"], 1024),
            ("template_expired", "Истекший пост", [], 1024),
            ("template_saboteur_active", "Саботёр: активный пост", ["[Список_Серверов]"], 4096),
            ("template_saboteur_item", "Саботёр: блок сервера", [], 4096),
            ("template_saboteur_closed", "Саботёр: закрытый пост", [], 4096),
            ("template_farm_live_active", "Фарм: активный пост", ["[Список_Серверов]"], 4096),
            ("template_farm_live_item", "Фарм: блок сервера", [], 4096),
            ("template_farm_live_closed", "Фарм: закрытый пост", [], 4096),
            ("template_mutator_active", "Мутатор: активный пост", ["[Ссылка]"], 1024),
            ("template_mutator_expired", "Мутатор: истекший пост", [], 1024),
        ]
        problems = []
        for key, title, req, limit in checks:
            val = strip_nt(await get_setting(key, ""))
            if not val.strip():
                problems.append(f"{title}: пустой")
                continue
            for tag in req:
                if tag not in val:
                    problems.append(f"{title}: нет тега {tag}")
            if limit == 1024 and userbot:
                limit = 2048   # подпись к фото у Premium-аккаунта юзербота
            vis = html_visible_len(val)   # видимые символы, без тегов и ID эмодзи
            if vis > limit - 100:
                problems.append(f"{title}: длинный ({vis} симв., лимит {limit})")
        if problems:
            warn("Шаблоны: " + "; ".join(problems))
        else:
            ok("Все 11 шаблонов заполнены, обязательные теги на месте, длина в пределах")
    except Exception as e:
        bad(f"Шаблоны: {html.escape(str(e))[:150]}")

    # 7. Фоновые воркеры
    try:
        alive = {t.get_coro().__name__ for t in asyncio.all_tasks() if not t.done() and hasattr(t.get_coro(), "__name__")}
        need = {"secret_timer_worker": "таймер секреток", "mutator_timer_worker": "таймер мутатора",
                "reminder_worker": "напоминания", "prelink_expiry_worker": "срок предссылок"}
        missing = [name for fn, name in need.items() if fn not in alive]
        if missing:
            bad("Не работают фоновые процессы: " + ", ".join(missing))
        else:
            ok("Фоновые процессы работают (таймеры, напоминания, предссылки)")
    except Exception as e:
        warn(f"Не удалось проверить фоновые процессы: {html.escape(str(e))[:100]}")

    # 8. Roblox API
    try:
        st, _n, _info = await lookup_roblox("Roblox")
        if st == "ok":
            ok("Roblox API отвечает (проверка ников работает)")
        else:
            warn("Roblox API сейчас не отвечает: ники будут приниматься без проверки")
    except Exception as e:
        warn(f"Roblox API: {html.escape(str(e))[:100]}")

    # 9. Стафф и предссылки
    try:
        staff = await get_staff_ids()
        pre = await get_prelink_user_ids()
        ok(f"Стаффа в группе для созыва: {len(staff)}; пользователей с предссылками: {len(pre)}")
    except Exception as e:
        warn(f"Стафф/предссылки: {html.escape(str(e))[:100]}")

    # 10. Тгп-эмодзи
    off = [n for n, flag in (("в текстах", TEXT_EMOJI_ON), ("на кнопках", BUTTON_ICONS_ON)) if not flag]
    if off:
        warn("Тгп-эмодзи отключены " + " и ".join(off) + " (Telegram не принял их, подробности в логах; нужен Premium у владельца бота)")
    else:
        ok("Тгп-эмодзи включены: в текстах и на кнопках")

    n_bad = sum(1 for i, _ in results if i == "❌")
    n_warn = sum(1 for i, _ in results if i == "⚠️")
    head = "🧪 <b>Самопроверка систем при включении тест-режима</b>\n\n"
    body = "\n".join(f"{i} {t}" for i, t in results)
    if n_bad:
        tail = f"\n\n🚨 <b>Критичных проблем: {n_bad}</b>" + (f", предупреждений: {n_warn}" if n_warn else "") + ". Лучше исправить до тестирования."
    elif n_warn:
        tail = f"\n\n⚠️ Предупреждений: {n_warn}. Тестировать можно."
    else:
        tail = "\n\n🟢 Всё в порядке, можно тестировать."
    return head + body + tail

async def build_test_summary(started_at) -> str:
    """Итоги тест-режима: что опубликовано в тест-канале, что истекло, какие были ошибки."""
    ch = TEST_CHANNEL_ID
    def _q():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT secret_type, is_expired FROM secret_posts WHERE channel_id = %s AND created_at >= %s", (ch, started_at))
                sec = cur.fetchall()
                cur.execute("""
                    SELECT COUNT(*), COALESCE(SUM(CASE WHEN s.is_active THEN 1 ELSE 0 END), 0)
                    FROM saboteur_servers s JOIN saboteur_posts p ON s.post_id = p.id
                    WHERE p.channel_id = %s AND s.created_at >= %s
                """, (ch, started_at))
                sab = cur.fetchone()
                cur.execute("SELECT COUNT(*) FROM saboteur_posts WHERE channel_id = %s AND created_at >= %s", (ch, started_at))
                sab_posts = cur.fetchone()[0]
                cur.execute("""
                    SELECT COUNT(*), COALESCE(SUM(CASE WHEN s.is_active THEN 1 ELSE 0 END), 0)
                    FROM farm_servers s JOIN farm_posts p ON s.post_id = p.id
                    WHERE p.channel_id = %s AND s.created_at >= %s
                """, (ch, started_at))
                farm = cur.fetchone()
                cur.execute("SELECT COUNT(*) FROM farm_posts WHERE channel_id = %s AND created_at >= %s", (ch, started_at))
                farm_posts = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*), COALESCE(SUM(CASE WHEN is_expired THEN 1 ELSE 0 END), 0) FROM mutator_badges WHERE channel_id = %s AND created_at >= %s", (ch, started_at))
                mut = cur.fetchone()
                return sec, sab, sab_posts, farm, farm_posts, mut
    try:
        sec, sab, sab_posts, farm, farm_posts, mut = await asyncio.to_thread(_q)
    except Exception as e:
        return f"🧪 <b>Сводка теста</b>\n\nНе удалось собрать данные: {html.escape(str(e))}"

    now = datetime.now(timezone.utc)
    dur = int((now - started_at).total_seconds())
    dur_s = f"{dur // 3600} ч. {(dur % 3600) // 60} мин." if dur >= 3600 else f"{dur // 60} мин. {dur % 60} сек."

    sec_exp = sum(1 for _, e in sec if e)
    types = {}
    for t, _e in sec:
        types[t] = types.get(t, 0) + 1
    types_s = ", ".join(f"{html.escape(str(t))}: {n}" for t, n in types.items()) or "—"

    errs = list(test_error_collector.records)
    lines = [
        "🧪 <b>Сводка тест-режима</b>",
        f"⏱ Длительность: <b>{dur_s}</b>",
        "",
        f"🔮 Секретки: <b>{len(sec)}</b> (истекло: {sec_exp}, ещё активны: {len(sec) - sec_exp})",
        f"   Типы: {types_s}",
        f"🕵️ Саботёр: постов <b>{sab_posts}</b>, серверов <b>{sab[0]}</b> (ещё активны: {int(sab[1])})",
        f"🌾 Фарм: постов <b>{farm_posts}</b>, серверов <b>{farm[0]}</b> (ещё активны: {int(farm[1])})",
        f"🧬 Мутатор: <b>{mut[0]}</b> (истекло по таймеру: {int(mut[1])}, ещё активны: {mut[0] - int(mut[1])})",
        "",
    ]
    if errs:
        lines.append(f"⚠️ <b>Ошибок в логе за тест: {len(errs)}</b>")
        for e in errs[:5]:
            lines.append(f"• <code>{html.escape(e)}</code>")
        if len(errs) > 5:
            lines.append(f"…и ещё {len(errs) - 5}")
    else:
        lines.append("✅ Ошибок публикации и таймеров в логе не было.")
    lines.append("\n<i>Незавершённые посты в тест-канале остаются, пока не истекут или не будут сняты.</i>")
    return "\n".join(lines)

def _is_owner(uid: int) -> bool:
    return bool(OWNER_ID) and uid == OWNER_ID

@router.callback_query(F.data == "set_test_channel")
async def set_test_channel_prompt(call: CallbackQuery, state: FSMContext):
    if not _is_owner(call.from_user.id):
        await call.answer("❌ Канал тест-режима может задавать только создатель бота!", show_alert=True)
        return
    await state.set_state(SecretAdminStates.test_channel)
    kb = InlineKeyboardMarkup(inline_keyboard=[[IB(text="❌ Отменить", callback_data="sec_tmpl_cancel")]])
    cur_ch = TEST_CHANNEL_ID or "не задан"
    await call.message.answer(
        "📡 <b>Канал тест-режима</b>\n\n"
        f"Сейчас: <code>{html.escape(cur_ch)}</code>\n\n"
        "Отправьте <b>@username</b> тестового канала (бот должен быть там админом с правом публикации). "
        "Это должен быть отдельный канал, не рабочий.",
        reply_markup=kb, parse_mode="HTML"
    )
    await call.answer()

@router.message(SecretAdminStates.test_channel, F.text)
async def set_test_channel_proc(message: Message, state: FSMContext):
    global TEST_CHANNEL_ID
    if not _is_owner(message.from_user.id): return
    raw = message.text.strip()
    try:
        chat = await bot.get_chat(int(raw) if re.fullmatch(r"-?\d+", raw) else raw)
        b_mem = await bot.get_chat_member(chat_id=chat.id, user_id=bot.id)
        if b_mem.status not in ("administrator", "creator"):
            await message.answer(f"{ICON_CROSS} Бот не админ в этом канале. Сделайте его админом и отправьте ещё раз.", parse_mode="HTML")
            return
    except Exception as e:
        await message.answer(f"{ICON_CROSS} Не удалось проверить канал: {html.escape(str(e))}", parse_mode="HTML")
        return
    new_id = str(chat.id)

    # защита: тест-канал не должен быть рабочим
    def _bound():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT DISTINCT channel_id FROM secret_channels")
                return [r[0] for r in cur.fetchall()]
    for ch in await asyncio.to_thread(_bound):
        if await resolve_channel_id(ch) == new_id:
            await message.answer(f"{ICON_CROSS} Это рабочий канал публикаторов. Для теста нужен другой канал.", parse_mode="HTML")
            return

    TEST_CHANNEL_ID = new_id
    await set_setting("test_channel_id", new_id)
    await state.clear()
    await message.answer(f"{ICON_CHECK} Канал тест-режима сохранён: <b>{html.escape(chat.title or raw)}</b> (<code>{new_id}</code>)", parse_mode="HTML")

@router.callback_query(F.data == "toggle_testmode")
async def toggle_testmode_callback(call: CallbackQuery):
    global TEST_MODE_ON, MAINTENANCE_ON, TEST_STARTED_AT
    if not _is_owner(call.from_user.id):
        await call.answer("❌ Тест режим может включать только создатель бота!", show_alert=True)
        return
    summary_text = None
    if not TEST_MODE_ON:
        if not TEST_CHANNEL_ID:
            await call.answer("❌ Сначала добавьте канал тест-режима (кнопка «📡 Канал тест-режима»).", show_alert=True)
            return
        TEST_MODE_ON = True
        MAINTENANCE_ON = True
        TEST_STARTED_AT = datetime.now(timezone.utc)
        test_error_collector.records.clear()
        await set_setting("test_started_at", TEST_STARTED_AT.isoformat())
        msg = "🧪 Тест режим ВКЛЮЧЁН. Публикации идут в тест-канал, бот закрыт для всех кроме вас. Самопроверка придёт в ЛС."
    else:
        started = TEST_STARTED_AT or (datetime.now(timezone.utc) - timedelta(hours=1))
        summary_text = await build_test_summary(started)   # собираем до сброса флага, чтобы поймать ошибки
        TEST_MODE_ON = False
        MAINTENANCE_ON = False
        TEST_STARTED_AT = None
        msg = "🟢 Тест режим выключен. Тех. перерыв снят, бот снова работает для всех. Сводка отправлена вам в личные сообщения."
    await set_setting("test_mode", "true" if TEST_MODE_ON else "false")
    await set_setting("maintenance_mode", "true" if MAINTENANCE_ON else "false")
    logging.warning(f"Тест режим {'ВКЛЮЧЁН' if TEST_MODE_ON else 'выключен'} пользователем {call.from_user.id}")
    await call.answer(msg[:200], show_alert=True)
    try:
        await call.message.edit_reply_markup(reply_markup=await admin_panel_kb())
    except Exception:
        pass
    if summary_text:
        try:
            await bot.send_message(call.from_user.id, summary_text, parse_mode="HTML")
        except Exception as e:
            logging.warning(f"Не удалось отправить сводку теста владельцу: {e}")
    if TEST_MODE_ON:
        async def _selfcheck_and_send(uid: int):
            try:
                report = await run_selfcheck()
            except Exception as e:
                report = f"🧪 Самопроверка не удалась: {html.escape(str(e))[:300]}"
            try:
                await bot.send_message(uid, report, parse_mode="HTML")
            except Exception as e:
                logging.warning(f"Не удалось отправить результат самопроверки: {e}")
        spawn(_selfcheck_and_send(call.from_user.id))

@router.callback_query(F.data == "toggle_maintenance")
async def toggle_maintenance_callback(call: CallbackQuery):
    global MAINTENANCE_ON
    if not await can_bypass_maintenance(call.from_user.id):
        await call.answer("❌ Тех. перерыв может включать только создатель бота!", show_alert=True)
        return
    if MAINTENANCE_ON and TEST_MODE_ON:
        await call.answer("🧪 Включён тест режим. Выключите его, и тех. перерыв снимется сам.", show_alert=True)
        return
    MAINTENANCE_ON = not MAINTENANCE_ON
    await set_setting("maintenance_mode", "true" if MAINTENANCE_ON else "false")
    logging.warning(f"Технический перерыв {'ВКЛЮЧЁН' if MAINTENANCE_ON else 'выключен'} пользователем {call.from_user.id}")
    await call.answer(
        "🛠 Перерыв ВКЛЮЧЁН: бот закрыт для всех, кроме вас." if MAINTENANCE_ON else "🟢 Перерыв выключен, бот работает для всех.",
        show_alert=True
    )
    kb = await admin_panel_kb()
    try:
        await call.message.edit_reply_markup(reply_markup=kb)
    except Exception:
        pass

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
        [IB(text="➕ Добавить публикатора", callback_data="sec_add_pub")],
        [IB(text="➖ Удалить публикатора", callback_data="sec_del_pub")],
        [IB(text="🔙 Назад", callback_data="adm_secret_panel")]
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
        kb = InlineKeyboardMarkup(inline_keyboard=[[IB(text="🔙 Назад", callback_data="adm_secret_panel")]])
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
        text = text[:3900].rsplit("\n", 1)[0] + "\n... <i>(список сокращен)</i>"

    kb = InlineKeyboardMarkup(inline_keyboard=[[IB(text="🔙 Назад", callback_data="adm_secret_panel")]])
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
        [IB(text="👁 Посмотреть используемые шаблоны", callback_data="sec_tmpl_view")],
        [IB(text="✏️ Обычный активный пост", callback_data="sec_tmpl_act")],
        [IB(text="🌾 Шаблон фарм-секретки", callback_data="sec_tmpl_farm")],
        [IB(text="✏ Истекший пост", callback_data="sec_tmpl_exp")],
        [IB(text="🕵️ Активный пост саботера", callback_data="sec_tmpl_sab_act")],
        [IB(text="🔹 Блок сервера саботера", callback_data="sec_tmpl_sab_item")],
        [IB(text="🔒 Закрытый пост саботера", callback_data="sec_tmpl_sab_close")],
        [IB(text="🌾 Активный пост фарма", callback_data="sec_tmpl_flive_act")],
        [IB(text="🔹 Блок сервера фарма", callback_data="sec_tmpl_flive_item")],
        [IB(text="🔒 Закрытый пост фарма", callback_data="sec_tmpl_flive_close")],
        [IB(text="🧬 Мутатор: активный пост", callback_data="sec_tmpl_mut_act")],
        [IB(text="🧬 Мутатор: истекший пост", callback_data="sec_tmpl_mut_exp")],
        [IB(text="🔙 Назад", callback_data="adm_secret_panel")]
    ])
    await call.message.edit_text(f"{ICON_PENCIL} <b>Редактирование шаблонов:</b>\n\nВыберите нужный шаблон:", reply_markup=kb, parse_mode="HTML")
    await call.answer()

@router.callback_query(F.data == "sec_tmpl_act")
async def sec_tmpl_act(call: CallbackQuery, state: FSMContext):
    if not await is_main_admin(call.from_user.id): return
    tmpl = await get_setting("template_active")
    await state.set_state(SecretAdminStates.template_active)
    await call.message.answer(f"{ICON_PENCIL} <b>Текущий шаблон активного поста:</b>\n\n{tmpl}\n\nОтправьте новый текст (доступны теги <code>[Тип_Секретки]</code>, <code>[Склоненный_Тип]</code>, <code>[Ссылка]</code>). Бот сохранит любые цитаты, форматирование и кастомные эмодзи:", parse_mode="HTML", reply_markup=TMPL_CANCEL_KB)
    await call.answer()

@router.message(SecretAdminStates.template_active)
async def sec_tmpl_act_proc(message: Message, state: FSMContext):
    if not await is_main_admin(message.from_user.id): return
    saved_text = extract_text_with_custom_emojis(message)
    await set_setting("template_active", saved_text)
    await state.clear()
    kb = await main_keyboard(message.from_user.id)
    await message.answer(f"{ICON_CHECK} Шаблон активного поста обновлен со всем форматированием!", reply_markup=kb, parse_mode="HTML")

@router.callback_query(F.data == "sec_tmpl_farm")
async def sec_tmpl_farm(call: CallbackQuery, state: FSMContext):
    if not await is_main_admin(call.from_user.id): return
    tmpl = await get_setting("template_farm")
    await state.set_state(SecretAdminStates.template_farm)
    await call.message.answer(f"{ICON_PENCIL} <b>Текущий шаблон фарм-секретки:</b>\n\n{tmpl}\n\nОтправьте новый текст (доступны теги <code>[Тип_Особенной_Секретки]</code>, <code>[Ссылка]</code>). Бот сохранит любые цитаты, форматирование и кастомные эмодзи:", parse_mode="HTML", reply_markup=TMPL_CANCEL_KB)
    await call.answer()

@router.message(SecretAdminStates.template_farm)
async def sec_tmpl_farm_proc(message: Message, state: FSMContext):
    if not await is_main_admin(message.from_user.id): return
    saved_text = extract_text_with_custom_emojis(message)
    await set_setting("template_farm", saved_text)
    await state.clear()
    kb = await main_keyboard(message.from_user.id)
    await message.answer(f"{ICON_CHECK} Шаблон фарм-секретки обновлен со всем форматированием!", reply_markup=kb, parse_mode="HTML")

@router.callback_query(F.data == "sec_tmpl_exp")
async def sec_tmpl_exp(call: CallbackQuery, state: FSMContext):
    if not await is_main_admin(call.from_user.id): return
    tmpl = await get_setting("template_expired")
    await state.set_state(SecretAdminStates.template_expired)
    await call.message.answer(f"{ICON_PENCIL} <b>Текущий шаблон истекшего поста:</b>\n\n{tmpl}\n\nОтправьте новый текст (доступен тег <code>[Тип_Секретки]</code>). Бот сохранит любые цитаты, форматирование и кастомные эмодзи:", parse_mode="HTML", reply_markup=TMPL_CANCEL_KB)
    await call.answer()

@router.message(SecretAdminStates.template_expired)
async def sec_tmpl_exp_proc(message: Message, state: FSMContext):
    if not await is_main_admin(message.from_user.id): return
    saved_text = extract_text_with_custom_emojis(message)
    await set_setting("template_expired", saved_text)
    await state.clear()
    kb = await main_keyboard(message.from_user.id)
    await message.answer(f"{ICON_CHECK} Шаблон истекшего поста обновлен со всем форматированием!", reply_markup=kb, parse_mode="HTML")

# Шаблоны саботера в админ-панели
@router.callback_query(F.data == "sec_tmpl_sab_act")
async def sec_tmpl_sab_act(call: CallbackQuery, state: FSMContext):
    if not await is_main_admin(call.from_user.id): return
    tmpl = await get_setting("template_saboteur_active", "Шаблон пока пуст.")
    await state.set_state(SecretAdminStates.template_saboteur_active)
    await call.message.answer(f"{ICON_PENCIL} <b>Текущий шаблон активного поста саботера:</b>\n\n{tmpl}\n\nОтправьте новый текст (обязателен тег <code>[Список_Серверов]</code>). Поддерживаются цитаты, жирный шрифт и Premium-эмодзи:", parse_mode="HTML", reply_markup=TMPL_CANCEL_KB)
    await call.answer()

@router.message(SecretAdminStates.template_saboteur_active)
async def sec_tmpl_sab_act_proc(message: Message, state: FSMContext):
    if not await is_main_admin(message.from_user.id): return
    saved_text = extract_text_with_custom_emojis(message)
    await set_setting("template_saboteur_active", saved_text)
    await state.clear()
    kb = await main_keyboard(message.from_user.id)
    await message.answer(f"{ICON_CHECK} Шаблон активного поста саботера сохранён!", reply_markup=kb, parse_mode="HTML")

@router.callback_query(F.data == "sec_tmpl_sab_item")
async def sec_tmpl_sab_item(call: CallbackQuery, state: FSMContext):
    if not await is_main_admin(call.from_user.id): return
    tmpl = await get_setting("template_saboteur_item", "Шаблон пока пуст.")
    await state.set_state(SecretAdminStates.template_saboteur_item)
    await call.message.answer(
        f"{ICON_PENCIL} <b>Текущий шаблон одного блока сервера саботера:</b>\n\n{tmpl}\n\n"
        "Отправьте новый текст (доступны теги: <code>[Номер_Сервера]</code>, <code>[Ник_Роблокс]</code>, <code>[Искатель]</code>, <code>[Метод]</code>, <code>[Друзья]</code>, <code>[Ссылка]</code>):",
        parse_mode="HTML", reply_markup=TMPL_CANCEL_KB
    )
    await call.answer()

@router.message(SecretAdminStates.template_saboteur_item)
async def sec_tmpl_sab_item_proc(message: Message, state: FSMContext):
    if not await is_main_admin(message.from_user.id): return
    saved_text = extract_text_with_custom_emojis(message)
    await set_setting("template_saboteur_item", saved_text)
    await state.clear()
    kb = await main_keyboard(message.from_user.id)
    await message.answer(f"{ICON_CHECK} Шаблон блока сервера саботера сохранён!", reply_markup=kb, parse_mode="HTML")

@router.callback_query(F.data == "sec_tmpl_sab_close")
async def sec_tmpl_sab_close(call: CallbackQuery, state: FSMContext):
    if not await is_main_admin(call.from_user.id): return
    tmpl = await get_setting("template_saboteur_closed", "Шаблон пока пуст.")
    await state.set_state(SecretAdminStates.template_saboteur_closed)
    await call.message.answer(f"{ICON_PENCIL} <b>Текущий шаблон закрытого поста саботера:</b>\n\n{tmpl}\n\nОтправьте новый текст (сообщение, на которое отредактируется пост после завершения всех серверов):", parse_mode="HTML", reply_markup=TMPL_CANCEL_KB)
    await call.answer()

@router.message(SecretAdminStates.template_saboteur_closed)
async def sec_tmpl_sab_close_proc(message: Message, state: FSMContext):
    if not await is_main_admin(message.from_user.id): return
    saved_text = extract_text_with_custom_emojis(message)
    await set_setting("template_saboteur_closed", saved_text)
    await state.clear()
    kb = await main_keyboard(message.from_user.id)
    await message.answer(f"{ICON_CHECK} Шаблон закрытого поста саботера сохранён!", reply_markup=kb, parse_mode="HTML")

@router.callback_query(F.data == "sec_tmpl_flive_act")
async def sec_tmpl_flive_act(call: CallbackQuery, state: FSMContext):
    if not await is_main_admin(call.from_user.id): return
    tmpl = await get_setting("template_farm_live_active", DEFAULT_FARM_LIVE_ACTIVE)
    await state.set_state(SecretAdminStates.template_farm_live_active)
    await call.message.answer(
        f"{ICON_PENCIL} <b>Текущий шаблон активного поста фарма:</b>\n\n{tmpl}\n\n"
        "Отправьте новый текст (обязателен тег <code>[Список_Серверов]</code>). Поддерживаются цитаты, жирный шрифт и Premium-эмодзи:",
        parse_mode="HTML", reply_markup=TMPL_CANCEL_KB
    )
    await call.answer()

@router.message(SecretAdminStates.template_farm_live_active)
async def sec_tmpl_flive_act_proc(message: Message, state: FSMContext):
    if not await is_main_admin(message.from_user.id): return
    await set_setting("template_farm_live_active", extract_text_with_custom_emojis(message))
    await state.clear()
    kb = await main_keyboard(message.from_user.id)
    await message.answer(f"{ICON_CHECK} Шаблон активного поста фарма сохранён!", reply_markup=kb, parse_mode="HTML")

@router.callback_query(F.data == "sec_tmpl_flive_item")
async def sec_tmpl_flive_item(call: CallbackQuery, state: FSMContext):
    if not await is_main_admin(call.from_user.id): return
    tmpl = await get_setting("template_farm_live_item", DEFAULT_FARM_LIVE_ITEM)
    await state.set_state(SecretAdminStates.template_farm_live_item)
    await call.message.answer(
        f"{ICON_PENCIL} <b>Текущий шаблон блока сервера фарма:</b>\n\n{tmpl}\n\n"
        "Отправьте новый текст (доступны теги: <code>[Номер_Сервера]</code>, <code>[Ник_Роблокс]</code>, "
        "<code>[Искатель]</code>, <code>[Секции]</code>, <code>[Друзья]</code>, <code>[Ссылка]</code>):",
        parse_mode="HTML", reply_markup=TMPL_CANCEL_KB
    )
    await call.answer()

@router.message(SecretAdminStates.template_farm_live_item)
async def sec_tmpl_flive_item_proc(message: Message, state: FSMContext):
    if not await is_main_admin(message.from_user.id): return
    await set_setting("template_farm_live_item", extract_text_with_custom_emojis(message))
    await state.clear()
    kb = await main_keyboard(message.from_user.id)
    await message.answer(f"{ICON_CHECK} Шаблон блока сервера фарма сохранён!", reply_markup=kb, parse_mode="HTML")

@router.callback_query(F.data == "sec_tmpl_flive_close")
async def sec_tmpl_flive_close(call: CallbackQuery, state: FSMContext):
    if not await is_main_admin(call.from_user.id): return
    tmpl = await get_setting("template_farm_live_closed", DEFAULT_FARM_LIVE_CLOSED)
    await state.set_state(SecretAdminStates.template_farm_live_closed)
    await call.message.answer(
        f"{ICON_PENCIL} <b>Текущий шаблон закрытого поста фарма:</b>\n\n{tmpl}\n\n"
        "Отправьте новый текст (на него отредактируется пост, когда все фарм серверы сняты):",
        parse_mode="HTML", reply_markup=TMPL_CANCEL_KB
    )
    await call.answer()

@router.message(SecretAdminStates.template_farm_live_closed)
async def sec_tmpl_flive_close_proc(message: Message, state: FSMContext):
    if not await is_main_admin(message.from_user.id): return
    await set_setting("template_farm_live_closed", extract_text_with_custom_emojis(message))
    await state.clear()
    kb = await main_keyboard(message.from_user.id)
    await message.answer(f"{ICON_CHECK} Шаблон закрытого поста фарма сохранён!", reply_markup=kb, parse_mode="HTML")

TMPL_CANCEL_KB = InlineKeyboardMarkup(inline_keyboard=[
    [IB(text="❌ Отменить", callback_data="sec_tmpl_cancel")]
])

@router.callback_query(F.data == "sec_tmpl_cancel")
async def sec_tmpl_cancel(call: CallbackQuery, state: FSMContext):
    if not await is_main_admin(call.from_user.id): return
    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[[IB(text="🔙 К шаблонам", callback_data="sec_templates")]])
    try:
        await call.message.edit_text(f"{ICON_CROSS} Редактирование отменено, шаблон не изменён.", reply_markup=kb, parse_mode="HTML")
    except Exception:
        await call.message.answer(f"{ICON_CROSS} Редактирование отменено, шаблон не изменён.", reply_markup=kb, parse_mode="HTML")
    await call.answer("Отменено")

@router.callback_query(F.data == "sec_tmpl_view")
async def sec_tmpl_view(call: CallbackQuery):
    if not await is_main_admin(call.from_user.id): return
    await call.answer()
    groups = [
        ("1/4 🔮 Секретки и особые", [
            ("Обычный активный пост", "template_active", ""),
            ("Шаблон фарм-секретки", "template_farm", ""),
            ("Истекший пост", "template_expired", ""),
        ]),
        ("2/4 🕵️ Саботер", [
            ("Активный пост", "template_saboteur_active", ""),
            ("Блок сервера", "template_saboteur_item", ""),
            ("Закрытый пост", "template_saboteur_closed", ""),
        ]),
        ("3/4 🌾 Фарм", [
            ("Активный пост", "template_farm_live_active", DEFAULT_FARM_LIVE_ACTIVE),
            ("Блок сервера", "template_farm_live_item", DEFAULT_FARM_LIVE_ITEM),
            ("Закрытый пост", "template_farm_live_closed", DEFAULT_FARM_LIVE_CLOSED),
        ]),
        ("4/4 🧬 Мутатор", [
            ("Активный пост", "template_mutator_active", DEFAULT_MUTATOR_ACTIVE),
            ("Истекший пост", "template_mutator_expired", DEFAULT_MUTATOR_EXPIRED),
        ]),
    ]
    back_kb = InlineKeyboardMarkup(inline_keyboard=[[IB(text="🔙 К шаблонам", callback_data="sec_templates")]])
    for gi, (title, items) in enumerate(groups):
        parts = []
        for name, key, default in items:
            val = (await get_setting(key, default)) or NT(default)
            parts.append(f"<b>▫️ {name}:</b>\n{val if strip_nt(val).strip() else '<i>— пусто —</i>'}")
        sep = "\n\n━━━━━━━━━━\n\n"
        full = f"<b>{title}</b>\n\n" + sep.join(parts)
        chunks = [full] if len(full) <= 4000 else [f"<b>{title}</b>\n\n{p}" for p in parts]
        for ci, chunk in enumerate(chunks):
            markup = back_kb if (gi == len(groups) - 1 and ci == len(chunks) - 1) else None
            try:
                await call.message.answer(chunk, parse_mode="HTML", reply_markup=markup, link_preview_options=LinkPreviewOptions(is_disabled=True))
            except Exception:
                await call.message.answer(html.escape(chunk[:3900]), parse_mode="HTML", reply_markup=markup)

@router.callback_query(F.data == "sec_tmpl_mut_act")
async def sec_tmpl_mut_act(call: CallbackQuery, state: FSMContext):
    if not await is_main_admin(call.from_user.id): return
    tmpl = await get_setting("template_mutator_active", DEFAULT_MUTATOR_ACTIVE)
    await state.set_state(SecretAdminStates.template_mutator_active)
    await call.message.answer(
        f"{ICON_PENCIL} <b>Текущий шаблон активного поста мутатора:</b>\n\n{tmpl}\n\n"
        "Отправьте новый текст (обязателен тег <code>[Ссылка]</code>). Поддерживаются цитаты, жирный шрифт и Premium-эмодзи:",
        parse_mode="HTML", reply_markup=TMPL_CANCEL_KB
    )
    await call.answer()

@router.message(SecretAdminStates.template_mutator_active)
async def sec_tmpl_mut_act_proc(message: Message, state: FSMContext):
    if not await is_main_admin(message.from_user.id): return
    saved_text = extract_text_with_custom_emojis(message)
    if "[Ссылка]" not in saved_text:
        await message.answer(f"{ICON_CROSS} В шаблоне нет тега <code>[Ссылка]</code>. Отправьте текст ещё раз или нажмите «Отменить».", parse_mode="HTML", reply_markup=TMPL_CANCEL_KB)
        return
    await set_setting("template_mutator_active", saved_text)
    await state.clear()
    kb = await main_keyboard(message.from_user.id)
    await message.answer(f"{ICON_CHECK} Шаблон активного поста мутатора сохранён!", reply_markup=kb, parse_mode="HTML")

@router.callback_query(F.data == "sec_tmpl_mut_exp")
async def sec_tmpl_mut_exp(call: CallbackQuery, state: FSMContext):
    if not await is_main_admin(call.from_user.id): return
    tmpl = await get_setting("template_mutator_expired", DEFAULT_MUTATOR_EXPIRED)
    await state.set_state(SecretAdminStates.template_mutator_expired)
    await call.message.answer(
        f"{ICON_PENCIL} <b>Текущий шаблон истекшего поста мутатора:</b>\n\n{tmpl}\n\n"
        "Отправьте новый текст (на него пост изменится через 8:10). Поддерживаются цитаты, жирный шрифт и Premium-эмодзи:",
        parse_mode="HTML", reply_markup=TMPL_CANCEL_KB
    )
    await call.answer()

@router.message(SecretAdminStates.template_mutator_expired)
async def sec_tmpl_mut_exp_proc(message: Message, state: FSMContext):
    if not await is_main_admin(message.from_user.id): return
    saved_text = extract_text_with_custom_emojis(message)
    await set_setting("template_mutator_expired", saved_text)
    await state.clear()
    kb = await main_keyboard(message.from_user.id)
    await message.answer(f"{ICON_CHECK} Шаблон истекшего поста мутатора сохранён!", reply_markup=kb, parse_mode="HTML")

# ----------------------------------------------------------------------
# ПРЕДССЫЛКИ: АДМИН-ПАНЕЛЬ
# ----------------------------------------------------------------------
async def _prelinks_screen():
    ids = await get_prelink_user_ids()
    labels = {uid: await get_user_label(uid) for uid in ids}
    expiry = await get_prelink_expiry_map()
    rows = []
    for uid in ids:
        plain = re.sub(r"<[^>]+>", "", html.unescape(labels[uid]))
        rows.append([IB(text=f"🗑 {plain} ({uid})"[:60], callback_data=f"sec_prelink_del_{uid}")])
    rows.append([IB(text="➕ Добавить ID", callback_data="sec_prelink_add")])
    rows.append([IB(text="🔙 Назад", callback_data="adm_secret_panel")])
    if ids:
        lst = "\n".join(f"• <a href='tg://openmessage?user_id={u}'>{u}</a> - {labels[u]} ({fmt_prelink_until(expiry.get(u))})" for u in ids)
    else:
        lst = "<i>— список пуст —</i>"
    text = (
        "🔔 <b>Предссылки</b>\n\n"
        f"Эти пользователи получают в личку ссылку одновременно со стаффом, раньше канала "
        f"(секретки, саботер, фарм сервер), в том числе когда публикуют сами. Бейдж мутатор выходит сразу, без форы.\n\n"
        f"{lst}\n\n"
        "<i>Чтобы убрать пользователя, нажмите на его ID. Пользователь должен хотя бы раз запустить бота.</i>"
    )
    return text, InlineKeyboardMarkup(inline_keyboard=rows)

@router.callback_query(F.data == "sec_prelinks")
async def sec_prelinks(call: CallbackQuery, state: FSMContext):
    if not await is_main_admin(call.from_user.id): return
    await state.clear()
    text, kb = await _prelinks_screen()
    await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await call.answer()

@router.callback_query(F.data == "sec_prelink_add")
async def sec_prelink_add(call: CallbackQuery, state: FSMContext):
    if not await is_main_admin(call.from_user.id): return
    await state.set_state(SecretAdminStates.prelink_add)
    kb = InlineKeyboardMarkup(inline_keyboard=[[IB(text="❌ Отменить", callback_data="sec_prelinks")]])
    await call.message.answer(
        "➕ Отправьте <b>Telegram ID</b> пользователя (можно несколько через пробел или с новой строки):",
        reply_markup=kb, parse_mode="HTML"
    )
    await call.answer()

@router.message(SecretAdminStates.prelink_add, F.text)
async def sec_prelink_add_proc(message: Message, state: FSMContext):
    if not await is_main_admin(message.from_user.id): return
    ids = list(dict.fromkeys(int(x) for x in re.findall(r"\d{5,15}", message.text)))
    if not ids:
        await message.answer(f"{ICON_CROSS} Не нашёл ни одного ID. Отправьте числовой ID или нажмите «Отменить».", parse_mode="HTML")
        return
    await state.update_data(prelink_ids=ids)
    await state.set_state(SecretAdminStates.prelink_duration)
    dur_rows = [
        [IB(text="📅 Неделя", callback_data="sec_prelink_dur_week"),
         IB(text="🗓 Месяц", callback_data="sec_prelink_dur_month"),
         IB(text="♾ Навсегда", callback_data="sec_prelink_dur_forever")],
    ]
    if message.from_user.id == OWNER_ID:
        dur_rows.append([IB(text="🧪 Тест на 1 минуту", callback_data="sec_prelink_dur_test")])
    dur_rows.append([IB(text="❌ Отменить", callback_data="sec_prelinks")])
    kb = InlineKeyboardMarkup(inline_keyboard=dur_rows)
    labels = [f"{u} - {await get_user_label(u)}" for u in ids]
    await message.answer(
        "⏳ <b>На какой срок выдать предссылки?</b>\n\n" + "\n".join(f"• {l}" for l in labels),
        reply_markup=kb, parse_mode="HTML"
    )

@router.callback_query(SecretAdminStates.prelink_duration, F.data.startswith("sec_prelink_dur_"))
async def sec_prelink_duration_proc(call: CallbackQuery, state: FSMContext):
    if not await is_main_admin(call.from_user.id): return
    mode = call.data.split("_")[-1]
    if mode == "test" and call.from_user.id != OWNER_ID:
        await call.answer("❌ Тестовый режим доступен только создателю бота.", show_alert=True)
        return
    data = await state.get_data()
    ids = data.get("prelink_ids") or []
    await state.clear()
    if not ids:
        await call.answer()
        return

    now = datetime.now(timezone.utc)
    if mode == "week":
        expires = now + timedelta(days=7)
    elif mode == "month":
        expires = now + timedelta(days=30)
    elif mode == "test":
        expires = now + timedelta(minutes=1)
    else:
        expires = None

    def _add():
        with get_db() as conn:
            with conn.cursor() as cur:
                for uid in ids:
                    cur.execute(
                        "INSERT INTO prelink_users (user_id, expires_at, is_test) VALUES (%s, %s, %s) "
                        "ON CONFLICT (user_id) DO UPDATE SET expires_at = EXCLUDED.expires_at, is_test = EXCLUDED.is_test",
                        (uid, expires, mode == "test")
                    )
    await asyncio.to_thread(_add)

    until = fmt_prelink_until(expires)
    notice = (
        "🔔 <b>Вы добавлены в Предссылки!</b>\n\n"
        "Теперь вы будете получать ссылки на серверы в личные сообщения раньше, чем они появятся в канале.\n"
        f"⏳ Доступ действует: <b>{until}</b>"
    )
    delivered, failed = 0, []
    for uid in ids:
        try:
            await bot.send_message(uid, notice, parse_mode="HTML")
            delivered += 1
        except Exception:
            failed.append(uid)

    text, kb = await _prelinks_screen()
    prefix = "🧪 Тест на 1 минуту. " if mode == "test" else ""
    report = f"{ICON_CHECK} {prefix}Выдано: {len(ids)} ({until}). Уведомление доставлено: {delivered}."
    if mode == "test":
        report += "\nЧерез минуту доступ снимется, и придёт уведомление об окончании."
    if failed:
        report += "\nНе доставлено (человек не запускал бота): " + ", ".join(str(f) for f in failed)
    await call.message.answer(report + "\n\n" + text, reply_markup=kb, parse_mode="HTML")
    await call.answer()

@router.callback_query(F.data.startswith("sec_prelink_del_"))
async def sec_prelink_del(call: CallbackQuery):
    if not await is_main_admin(call.from_user.id): return
    uid = int(call.data.split("_")[-1])
    def _del():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM prelink_users WHERE user_id = %s", (uid,))
    await asyncio.to_thread(_del)
    text, kb = await _prelinks_screen()
    await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await call.answer("Удалён")

@router.callback_query(F.data == "sec_toggle_calldm")
async def sec_toggle_calldm(call: CallbackQuery):
    if not await is_main_admin(call.from_user.id): return
    cur_v = await get_setting("call_dm", "false")
    new_v = "false" if cur_v == "true" else "true"
    await set_setting("call_dm", new_v)
    await call.answer("📲 Личные пинги стаффу: " + ("ВКЛ" if new_v == "true" else "ВЫКЛ"), show_alert=True)

@router.callback_query(F.data == "sec_call_cmd")
async def sec_call_cmd(call: CallbackQuery, state: FSMContext):
    if not await is_main_admin(call.from_user.id): return
    cur_cmd = await get_setting("call_command", "") or ""
    await state.set_state(SecretAdminStates.call_command)
    kb = InlineKeyboardMarkup(inline_keyboard=[[IB(text="❌ Отменить", callback_data="sec_tmpl_cancel")]])
    await call.message.answer(
        "📣 <b>Команда для бота-зазывалы</b>\n\n"
        f"Сейчас: <code>{html.escape(cur_cmd) if cur_cmd else 'не задана'}</code>\n\n"
        "Эту команду при каждом созыве будет писать в тему «Секретки» ваш юзербот (боты не видят сообщения друг друга, поэтому от лица бота она не сработает). "
        "Юзербот должен быть участником группы.\n\n"
        "Отправьте текст команды (например <code>/call</code>) или <code>-</code>, чтобы отключить:",
        reply_markup=kb, parse_mode="HTML"
    )
    await call.answer()

@router.message(SecretAdminStates.call_command, F.text)
async def sec_call_cmd_proc(message: Message, state: FSMContext):
    if not await is_main_admin(message.from_user.id): return
    v = message.text.strip()
    await set_setting("call_command", "" if v == "-" else v)
    await state.clear()
    await message.answer(f"{ICON_CHECK} Команда зазывалы " + ("отключена." if v == "-" else f"сохранена: <code>{html.escape(v)}</code>"), parse_mode="HTML")

@router.callback_query(F.data == "sec_add_type")
async def sec_add_type(call: CallbackQuery, state: FSMContext):
    if not await is_main_admin(call.from_user.id): return
    await state.set_state(SecretAdminStates.add_type_name)
    await call.message.answer(f"{ICON_BALL} Введите название нового типа (например, <code>Звезда</code>):", parse_mode="HTML")
    await call.answer()

@router.message(SecretAdminStates.add_type_name)
async def sec_add_type_name_proc(message: Message, state: FSMContext):
    if not await is_main_admin(message.from_user.id): return
    if not message.text or len(message.text.strip().encode()) > 40:
        await message.answer(f"{ICON_CROSS} Название должно быть текстом не длиннее 40 байт (около 20 русских букв). Попробуйте короче.", parse_mode="HTML")
        return
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
        if len(f"sec_rmtype_{k}".encode()) > 64: continue
        buttons.append([IB(text=f"🗑 {k}", callback_data=f"sec_rmtype_{k}")])
    buttons.append([IB(text="🔙 Назад", callback_data="adm_secret_panel")])
    await call.message.edit_text("🗑 Выберите обычный тип секретки для удаления:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
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

@router.callback_query(F.data == "sec_add_spec_type")
async def sec_add_spec_type_prompt(call: CallbackQuery, state: FSMContext):
    if not await is_main_admin(call.from_user.id): return
    await state.set_state(SecretAdminStates.add_special_type_name)
    await call.message.answer(f"{ICON_BALL} Введите название нового <b>особого типа</b> (например, <code>Фарм монет</code>):", parse_mode="HTML")
    await call.answer()

@router.message(SecretAdminStates.add_special_type_name)
async def sec_add_spec_type_proc(message: Message, state: FSMContext):
    if not await is_main_admin(message.from_user.id): return
    if not message.text or len(message.text.strip().encode()) > 40:
        await message.answer(f"{ICON_CROSS} Название должно быть текстом не длиннее 40 байт (около 20 русских букв). Попробуйте короче.", parse_mode="HTML")
        return
    t_name = message.text.strip()
    def _q():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO special_secret_types (name) VALUES (%s) ON CONFLICT (name) DO NOTHING", (t_name,))
    await asyncio.to_thread(_q)
    await state.clear()
    kb = await main_keyboard(message.from_user.id)
    await message.answer(f"{ICON_CHECK} Особый тип <b>{t_name}</b> успешно добавлен!", reply_markup=kb, parse_mode="HTML")

@router.callback_query(F.data == "sec_del_spec_type")
async def sec_del_spec_type(call: CallbackQuery):
    if not await is_main_admin(call.from_user.id): return
    spec_list = await get_special_secret_types()
    if not spec_list:
        await call.answer("Особых типов пока нет!", show_alert=True)
        return
    buttons = []
    for k in spec_list:
        if len(f"sec_rmspec_{k}".encode()) > 64: continue
        buttons.append([IB(text=f"🗑 {k}", callback_data=f"sec_rmspec_{k}")])
    buttons.append([IB(text="🔙 Назад", callback_data="adm_secret_panel")]
    )
    await call.message.edit_text("🗑 Выберите особый тип секретки для удаления:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await call.answer()

@router.callback_query(F.data.startswith("sec_rmspec_"))
async def sec_rmspec_proc(call: CallbackQuery):
    if not await is_main_admin(call.from_user.id): return
    t_name = call.data.split("_", 2)[2]
    def _q():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM special_secret_types WHERE name = %s", (t_name,))
    await asyncio.to_thread(_q)
    await call.message.edit_text(f"{ICON_CHECK} Особый тип <b>{t_name}</b> удален.", parse_mode="HTML")
    await call.answer()

# ----------------------------------------------------------------------
# ПУБЛИКАЦИЯ СЕКРЕТОК (30 СЕК ФОРА + СКРЫТЫЙ ПУШ-СОЗЫВ СТАФФА)
# ----------------------------------------------------------------------
@router.message(F.text.in_(btn_texts(BTN_SECRETS)), F.chat.type == "private")
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
        "<i>Убедитесь, что бот и ваш аккаунт добавлены туда администраторами с правом публикации!</i>",
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
                        (user_id, ch_store)
                    )
        ch_store = await resolve_channel_id(ch_input)
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
        await call.message.answer("📭 У вас нет активных опубликованных секреток.")
        await call.answer()
        return
    kb_bts = []
    for pid, stype, mid in posts:
        kb_bts.append([IB(text=f"📌 {stype} (ID: {mid}) — Завершить", callback_data=f"pub_close_{pid}")])
    await call.message.answer("📋 Ваши активные секретки:\nНажмите, чтобы досрочно удалить ссылку в канале:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_bts))
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
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [IB(text="🔮 Обычная секретка", callback_data="pub_mode_normal")],
        [IB(text="🌾 Фарм секретка", callback_data="pub_mode_farm")]
    ])
    await call.message.edit_text("<b>Выберите категорию публикации:</b>", reply_markup=kb, parse_mode="HTML")
    await state.set_state(SecretPublisherStates.waiting_for_mode)
    await call.answer()

@router.callback_query(SecretPublisherStates.waiting_for_mode, F.data == "pub_mode_normal")
async def pub_mode_normal(call: CallbackQuery, state: FSMContext):
    await state.update_data(is_farm=False)
    types_d = await get_secret_types_dict()
    if not types_d:
        await call.message.answer(f"{ICON_CROSS} В базе нет типов секреток. Обратитесь к создателю.", parse_mode="HTML")
        await call.answer()
        return
    buttons = []
    for s_name in types_d.keys():
        if len(f"pub_settype_{s_name}".encode()) > 64: continue
        buttons.append([IB(text=f"🔹 {s_name}", callback_data=f"pub_settype_{s_name}")])
    await call.message.edit_text("Выберите тип секретки:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await state.set_state(SecretPublisherStates.waiting_for_type)
    await call.answer()

@router.callback_query(SecretPublisherStates.waiting_for_mode, F.data == "pub_mode_farm")
async def pub_mode_farm(call: CallbackQuery, state: FSMContext):
    await state.update_data(is_farm=True)
    spec_list = await get_special_secret_types()
    if not spec_list:
        await call.message.answer(f"{ICON_CROSS} В базе нет особых типов секреток. Добавьте их в админ-панели!", parse_mode="HTML")
        await call.answer()
        return
    buttons = []
    for k in spec_list:
        if len(f"pub_farm_sel_{k}".encode()) > 64: continue
        buttons.append([IB(text=f"🌾 {k}", callback_data=f"pub_farm_sel_{k}")])
    await call.message.edit_text("🌾 <b>Выбран режим: Фарм секретка</b>\n\nВыберите тип фарма кнопкой ниже:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")
    await state.set_state(SecretPublisherStates.waiting_for_farm_choice)
    await call.answer()

@router.callback_query(SecretPublisherStates.waiting_for_farm_choice, F.data.startswith("pub_farm_sel_"))
async def pub_farm_choice_selected(call: CallbackQuery, state: FSMContext):
    f_type = call.data.split("_", 3)[3]
    await state.update_data(secret_type=f_type)
    await call.message.edit_text(f"Выбран тип: <b>{html.escape(f_type)}</b>\n\nТеперь отправьте <b>фотографию</b> секретки:", parse_mode="HTML")
    await state.set_state(SecretPublisherStates.waiting_for_photo)
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
    is_farm = data.get("is_farm", False)

    if is_farm:
        tmpl = await get_setting("template_farm")
        preview = (
            tmpl.replace("[Тип_Особенной_Секретки]", stype)
                .replace("[Ссылка]", clean)
        )
    else:
        types_d = await get_secret_types_dict()
        dec = types_d.get(stype, stype)
        tmpl = await get_setting("template_active")
        preview = (
            tmpl.replace("[Тип_Секретки]", stype)
                .replace("[Склоненный_Тип]", dec)
                .replace("[Ссылка]", clean)
        )
        
    conf_kb = InlineKeyboardMarkup(inline_keyboard=[[
        IB(text="✅ Опубликовать", callback_data="pub_conf_yes"),
        IB(text="❌ Отмена", callback_data="pub_conf_no")
    ]])
    await message.answer("Предпросмотр поста:")
    await message.answer_photo(photo=data["photo_id"], caption=preview, reply_markup=conf_kb, parse_mode="HTML")
    await state.set_state(SecretPublisherStates.waiting_for_confirm)

@router.callback_query(SecretPublisherStates.waiting_for_confirm, F.data.startswith("pub_conf_"))
async def pub_confirm(call: CallbackQuery, state: FSMContext):
    action = call.data.split("_")[2]
    
    if action == "yes":
        try:
            await call.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass

        async with publish_lock:
            data = await state.get_data()
            if not data or "secret_type" not in data:
                await call.answer()
                return

            ch_id = await check_channel_rights(bot, call.from_user.id)
            if not ch_id:
                try:
                    await call.message.edit_caption(
                        caption=f"{ICON_CROSS} Ошибка: нет доступа к привязанному каналу!", 
                        reply_markup=None, 
                        parse_mode="HTML"
                    )
                except Exception:
                    pass
                await state.clear()
                await call.answer()
                return

            stype = data["secret_type"]
            is_farm = data.get("is_farm", False)

            if is_farm:
                tmpl = await get_setting("template_farm")
                final_txt = (
                    tmpl.replace("[Тип_Особенной_Секретки]", stype)
                        .replace("[Ссылка]", data["link"])
                )
            else:
                types_d = await get_secret_types_dict()
                dec = types_d.get(stype, stype)
                tmpl = await get_setting("template_active")
                final_txt = (
                    tmpl.replace("[Тип_Секретки]", stype)
                        .replace("[Склоненный_Тип]", dec)
                        .replace("[Ссылка]", data["link"])
                )

            try:
                # 1. Карточка стаффа для админ-чата
                staff_caption = (
                    f"⚡️ <b>СЕКРЕТКА ДЛЯ СТАФФА (Фора {CHANNEL_DELAY_SECONDS} сек.)</b> ⚡️\n"
                    f"<i>Пост появится в канале через {CHANNEL_DELAY_SECONDS} секунд! Предссылки получат его через {STAFF_DELAY_SECONDS} сек.</i>\n"
                    f"👤 Опубликовал: {call.from_user.mention_html()}\n\n"
                    f"{final_txt}"
                )

                # 2. Мгновенно отправляем фото в тему "Секретки" (в тест-режиме стаффу ничего не шлём)
                if not TEST_MODE_ON:
                    await bot.send_photo(
                        chat_id=ADMIN_CHAT_ID,
                        photo=data["photo_id"],
                        caption=staff_caption,
                        message_thread_id=TOPIC_SECRETS_ID,
                        parse_mode="HTML"
                    )

                # 2.1 Предссылки (одновременно со стаффом)
                spawn(send_prelinks(
                    _prelink_header(f"секретка «{html.escape(stype)}»") + final_txt,
                    photo_id=data["photo_id"]
                ))

                # 3. Пуш-созыв стаффа
                call_msg = (
                    f"<b>ВНИМАНИЕ ВСЕМ АДМИНИСТРАТОРАМ!</b> 🔔\n\n"
                    f"Вышла новая секретка: <b>{html.escape(stype)}</b>!\n"
                    f"⏳ У вас есть <b>{STAFF_DELAY_SECONDS} секунд приоритета</b> (до рассылки предссылок), канал через {CHANNEL_DELAY_SECONDS} сек.!\n"
                    "🏃 Заходите скорее!"
                )
                await call_staff(call_msg, exclude_ids=[call.from_user.id])

                # 4. Сохраняем пост в БД
                def _save():
                    with get_db() as conn:
                        with conn.cursor() as cur:
                            cur.execute(
                                "INSERT INTO secret_posts (user_id, channel_id, message_id, secret_type, base_text, created_at) "
                                "VALUES (%s, %s, 0, %s, %s, CURRENT_TIMESTAMP) RETURNING id",
                                (call.from_user.id, ch_id, stype, final_txt)
                            )
                            return cur.fetchone()[0]
                
                post_id = await asyncio.to_thread(_save)

                # 5. Отложенная отправка в канал через 30 секунд
                spawn(
                    publish_to_channel_delayed(
                        channel_id=ch_id,
                        photo_id=data["photo_id"],
                        caption=final_txt,
                        post_id=post_id,
                        delay=CHANNEL_DELAY_SECONDS
                    )
                )

                try:
                    await call.message.edit_caption(
                        caption=(
                            f"{ICON_CHECK} <b>Секретка отправлена стаффу!</b>\n\n"
                            f"⏳ В тему секреток пост уже доставлен, админы созваны.\n"
                            f"📢 В канал <b>{ch_id}</b> выйдет ровно через {CHANNEL_DELAY_SECONDS} сек.\n"
                            f"⏱ Таймер жизни ссылки: <b>8:30</b> (в канале будет доступна 7:30)."
                        ),
                        reply_markup=None, 
                        parse_mode="HTML"
                    )
                except TelegramBadRequest:
                    pass

            except Exception as e:
                try:
                    await call.message.edit_caption(
                        caption=f"{ICON_CROSS} Ошибка публикации: {e}", 
                        reply_markup=None, 
                        parse_mode="HTML"
                    )
                except TelegramBadRequest:
                    pass
            finally:
                await state.clear()
    else:
        try:
            await call.message.edit_caption(
                caption=f"{ICON_CROSS} Публикация отменена.", 
                reply_markup=None, 
                parse_mode="HTML"
            )
        except TelegramBadRequest:
            pass
        await state.clear()

    await call.answer()

# ----------------------------------------------------------------------
# СИСТЕМА ПУБЛИКАЦИИ САБОТЕРА (ЖИВОЙ ПОСТ + ДИНАМИЧЕСКАЯ СКЛЕЙКА)
# ----------------------------------------------------------------------
@router.callback_query(F.data == "pub_send_saboteur")
async def pub_send_saboteur(call: CallbackQuery, state: FSMContext):
    if not await is_secret_publisher(call.from_user.id): return
    ch_id = await check_channel_rights(bot, call.from_user.id)
    if not ch_id:
        await call.message.answer(f"{ICON_CROSS} Сначала привяжите канал через меню секреток!", reply_markup=publisher_menu_kb(), parse_mode="HTML")
        await call.answer()
        return

    # Ограничение: 1 активный сервер на обычного человека. Главным админам - бесконечно
    if not await is_main_admin(call.from_user.id):
        user_active = await count_user_active_saboteurs(call.from_user.id)
        if user_active >= 1:
            await call.answer("❌ У вас уже есть активный сервер саботера! Завершите его, прежде чем добавлять новый.", show_alert=True)
            return

    await state.clear()
    await state.set_state(SaboteurPublisherStates.waiting_for_nick)
    await call.message.answer("🕵️ <b>Поиск саботера</b>\n\n1. Введите ваш <b>ник в Roblox</b>:", parse_mode="HTML")
    await call.answer()

@router.message(SaboteurPublisherStates.waiting_for_nick, F.text)
async def saboteur_nick_proc(message: Message, state: FSMContext):
    nick_input = message.text.strip()
    rbx_info = await check_roblox_username(nick_input)
    if rbx_info:
        official_name = rbx_info["name"]
        rbx_id = rbx_info["id"]
        profile_str = f"<a href='https://www.roblox.com/users/{rbx_id}/profile'>{official_name}</a>"
    else:
        official_name = nick_input
        profile_str = f"<code>{html.escape(nick_input)}</code>"

    await state.update_data(sab_nick=profile_str, sab_raw_nick=official_name)
    await state.set_state(SaboteurPublisherStates.waiting_for_method)
    await message.answer(
        f"{ICON_CHECK} Ник зафиксирован: {profile_str}\n\n"
        "2. Введите <b>метод поиска</b> (разрешены только цифры и символы <code>/ \\ | -</code>, например: <code>4/20</code> или <code>4/40</code>):",
        parse_mode="HTML",
        link_preview_options=LinkPreviewOptions(is_disabled=True)
    )

@router.message(SaboteurPublisherStates.waiting_for_method, F.text)
async def saboteur_method_proc(message: Message, state: FSMContext):
    m_text = message.text.strip()
    if not re.fullmatch(r"^[0-9/\\|\-\s]+$", m_text):
        await message.answer(
            f"{ICON_CROSS} <b>Некорректный формат метода!</b>\n\n"
            "Разрешено вводить только цифры и символы <code>/ \\ | -</code> (например: <code>4/20</code>, <code>4/40</code>, <code>2\\10</code>). Попробуйте снова:",
            parse_mode="HTML"
        )
        return

    await state.update_data(sab_method=m_text)
    await state.set_state(SaboteurPublisherStates.waiting_for_friends)

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        IB(text="🟢 Принимаю", callback_data="sab_fr_yes"),
        IB(text="🔴 Не принимаю", callback_data="sab_fr_no")
    ]])
    await message.answer(f"{ICON_CHECK} Метод: <code>{html.escape(m_text)}</code>\n\n3. Принимаете ли вы заявки в друзья на сервере?", reply_markup=kb, parse_mode="HTML")

@router.callback_query(SaboteurPublisherStates.waiting_for_friends, F.data.startswith("sab_fr_"))
async def saboteur_friends_proc(call: CallbackQuery, state: FSMContext):
    ans = "Принимает" if call.data == "sab_fr_yes" else "Не принимает"
    await state.update_data(sab_friends=ans)
    await state.set_state(SaboteurPublisherStates.waiting_for_link)
    await call.message.edit_text(f"Статус друзей: <b>{ans}</b>\n\n4. Теперь отправьте <b>ссылку на VIP-сервер</b> Roblox:", parse_mode="HTML")
    await call.answer()

@router.message(SaboteurPublisherStates.waiting_for_link, F.text)
async def saboteur_link_proc(message: Message, state: FSMContext):
    raw = message.text.strip()
    clean = re.sub(r'\s+', '', raw)
    clean_lower = clean.lower()

    is_roblox = "roblox.com" in clean_lower
    is_vip = ("privateserverlinkcode=" in clean_lower) or ("share?code=" in clean_lower)

    if not (is_roblox and is_vip):
        await message.answer(
            f"{ICON_CROSS} <b>Некорректная ссылка на VIP-сервер!</b>\n"
            "Отправьте корректную ссылку на Roblox VIP сервер.",
            parse_mode="HTML"
        )
        return

    await state.update_data(link=clean, sab_link=clean)
    data = await state.get_data()

    user = message.from_user
    tg_display = f"@{user.username}" if user.username else get_user_mention(user)
    await state.update_data(sab_tg=tg_display)

    preview_text = (
        "🕵️ <b>Предпросмотр данных вашего сервера:</b>\n\n"
        f"• Искатель: {tg_display}\n"
        f"• Ник в Roblox: {data['sab_nick']}\n"
        f"• Метод поиска: <code>{html.escape(data['sab_method'])}</code>\n"
        f"• Заявки в друзья: <b>{data['sab_friends']}</b>\n"
        f"• Ссылка: {clean}\n\n"
        "<i>Подтвердите публикацию:</i>"
    )

    conf_kb = InlineKeyboardMarkup(inline_keyboard=[[
        IB(text="✅ Опубликовать", callback_data="sab_conf_yes"),
        IB(text="❌ Отмена", callback_data="sab_conf_no")
    ]])
    await message.answer(preview_text, reply_markup=conf_kb, parse_mode="HTML", link_preview_options=LinkPreviewOptions(is_disabled=True))
    await state.set_state(SaboteurPublisherStates.waiting_for_confirm)

@router.callback_query(SaboteurPublisherStates.waiting_for_confirm, F.data.startswith("sab_conf_"))
async def saboteur_confirm_proc(call: CallbackQuery, state: FSMContext):
    action = call.data.split("_")[2]
    if action == "no":
        await call.message.edit_text(f"{ICON_CROSS} Добавление сервера саботера отменено.", parse_mode="HTML")
        await state.clear()
        await call.answer()
        return

    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    data = await state.get_data()
    ch_id = await check_channel_rights(bot, call.from_user.id)
    if not ch_id:
        await call.message.answer(f"{ICON_CROSS} Канал не привязан!", parse_mode="HTML")
        await state.clear()
        await call.answer()
        return

    async with saboteur_lock:
        active_post = await get_active_saboteur_post(ch_id)
        post_id = 0
        if not active_post:
            def _create_p():
                with get_db() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            "INSERT INTO saboteur_posts (channel_id, message_id, is_active) VALUES (%s, 0, TRUE) RETURNING id",
                            (ch_id,)
                        )
                        return cur.fetchone()[0]
            post_id = await asyncio.to_thread(_create_p)
        else:
            post_id = active_post["id"]

        def _save_srv():
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO saboteur_servers (post_id, user_id, roblox_nick, tg_user, link, search_method, accepts_friends, is_active)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, TRUE) RETURNING id
                    """, (post_id, call.from_user.id, data["sab_nick"], data["sab_tg"], data["sab_link"], data["sab_method"], data["sab_friends"]))
                    return cur.fetchone()[0]
        await asyncio.to_thread(_save_srv)

    # 1. Отправляем карточку стаффу в топик секреток
    spawn(send_prelinks(
        _prelink_header("поиск саботера") +
        f"• Ник Roblox: {data['sab_nick']}\n"
        f"• Метод: <code>{html.escape(data['sab_method'])}</code>\n"
        f"• В друзья: <b>{data['sab_friends']}</b>\n"
        f"• Ссылка: {data['sab_link']}"
    ))
    staff_sab_text = (
        f"⚡️ <b>ПОИСК САБОТЕРА ДЛЯ СТАФФА (Фора {CHANNEL_DELAY_SECONDS} сек.)</b> ⚡️\n"
        f"<i>Сервер появится в канале через {CHANNEL_DELAY_SECONDS} секунд! Предссылки получат его через {STAFF_DELAY_SECONDS} сек.</i>\n"
        f"👤 Опубликовал: {call.from_user.mention_html()}\n\n"
        f"• Ник Roblox: {data['sab_nick']}\n"
        f"• Метод: <code>{html.escape(data['sab_method'])}</code>\n"
        f"• В друзья: <b>{data['sab_friends']}</b>\n"
        f"• Ссылка: {data['sab_link']}"
    )
    try:
        if not TEST_MODE_ON:
          await bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=staff_sab_text,
            message_thread_id=TOPIC_SECRETS_ID,
            parse_mode="HTML",
            link_preview_options=LinkPreviewOptions(is_disabled=True)
        )
    except Exception as ex_st:
        logging.warning(f"Ошибка карточки стаффу (саботер): {ex_st}")
    await call_staff(f"<b>Новый сервер саботера!</b> У вас {STAFF_DELAY_SECONDS} сек. приоритета до предссылок (канал через {CHANNEL_DELAY_SECONDS} сек.)!", exclude_ids=[call.from_user.id])

    # 2. Запускаем отложенную публикацию/обновление в канал
    spawn(publish_saboteur_delayed(ch_id, post_id, delay=CHANNEL_DELAY_SECONDS))

    await call.message.edit_text(
        f"{ICON_CHECK} <b>Сервер саботера принят!</b>\n\n"
        f"⏳ Админы созваны, пост в канале <b>{ch_id}</b> выйдет/обновится ровно через {CHANNEL_DELAY_SECONDS} сек.\n"
        "Управлять своими серверами можно через меню <b>«Мои сервера саботера»</b>.",
        parse_mode="HTML"
    )
    await state.clear()
    await call.answer()

@router.callback_query(F.data == "pub_my_saboteurs")
async def pub_my_saboteurs(call: CallbackQuery):
    if not await is_secret_publisher(call.from_user.id): return
    def _q():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT s.id, s.roblox_nick, s.search_method, s.post_id 
                    FROM saboteur_servers s
                    JOIN saboteur_posts p ON s.post_id = p.id
                    WHERE s.user_id = %s AND s.is_active = TRUE AND p.is_active = TRUE
                    ORDER BY s.id ASC
                """, (call.from_user.id,))
                return cur.fetchall()
    srvs = await asyncio.to_thread(_q)
    if not srvs:
        await call.message.answer("📭 У вас сейчас нет активных серверов саботера.")
        await call.answer()
        return

    kb_bts = []
    for s_id, r_nick, meth, p_id in srvs:
        clean_n = re.sub(r'<[^>]+>', '', r_nick)
        kb_bts.append([IB(text=f"❌ Снять: {clean_n} ({meth})", callback_data=f"sab_del_{s_id}_{p_id}")])

    await call.message.answer("📋 Ваши активные серверы саботера:\nНажмите на кнопку, чтобы удалить сервер из канала:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_bts))
    await call.answer()

@router.callback_query(F.data.startswith("sab_del_"))
async def pub_saboteur_del_proc(call: CallbackQuery):
    if not await is_secret_publisher(call.from_user.id): return
    parts = call.data.split("_")
    s_id = int(parts[2])
    post_id = int(parts[3])

    async with saboteur_lock:
        def _del_and_check():
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute("UPDATE saboteur_servers SET is_active = FALSE WHERE id = %s", (s_id,))
                    cur.execute("SELECT COUNT(*) FROM saboteur_servers WHERE post_id = %s AND is_active = TRUE", (post_id,))
                    rem_count = cur.fetchone()[0]
                    cur.execute("SELECT channel_id FROM saboteur_posts WHERE id = %s", (post_id,))
                    ch = cur.fetchone()[0]
                    if rem_count == 0:
                        cur.execute("UPDATE saboteur_posts SET is_active = FALSE WHERE id = %s", (post_id,))
                    return rem_count, ch
        rem_count, ch_id = await asyncio.to_thread(_del_and_check)

        try:
            await publish_or_update_saboteur_channel(ch_id, post_id)
        except Exception as e:
            logging.error(f"Ошибка обновления поста саботера после удаления: {e}")

    await call.message.edit_text(f"{ICON_CHECK} Ваш сервер снят с поиска. Пост в канале обновлён!", parse_mode="HTML")
    await call.answer()

# ----------------------------------------------------------------------
# ФАРМ СЕРВЕР: ЖИВОЙ ПОСТ (отдельный движок, не зависит от саботера)
# ----------------------------------------------------------------------
async def get_active_farm_post(channel_id: str) -> dict | None:
    def _q():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id, channel_id, message_id FROM farm_posts WHERE is_active = TRUE ORDER BY id DESC")
                return cur.fetchall()
    target = await resolve_channel_id(channel_id)
    for pid, ch, mid in await asyncio.to_thread(_q):
        if await resolve_channel_id(ch) == target:
            return {"id": pid, "channel_id": ch, "message_id": mid}
    return None

async def get_farm_post_by_id(post_id: int) -> dict | None:
    def _q():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id, channel_id, message_id FROM farm_posts WHERE id = %s", (post_id,))
                row = cur.fetchone()
                return {"id": row[0], "channel_id": row[1], "message_id": row[2]} if row else None
    return await asyncio.to_thread(_q)

async def get_active_farm_servers(post_id: int) -> list:
    def _q():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id, user_id, roblox_nick, tg_user, sections_count, link, accepts_friends
                    FROM farm_servers
                    WHERE post_id = %s AND is_active = TRUE
                    ORDER BY id ASC
                """, (post_id,))
                return cur.fetchall()
    return await asyncio.to_thread(_q)

async def count_user_active_farms(user_id: int) -> int:
    def _q():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT COUNT(*)
                    FROM farm_servers s
                    JOIN farm_posts p ON s.post_id = p.id
                    WHERE s.user_id = %s AND s.is_active = TRUE AND p.is_active = TRUE
                """, (user_id,))
                return cur.fetchone()[0]
    return await asyncio.to_thread(_q)

async def render_farm_post_text(post_id: int) -> str:
    servers = await get_active_farm_servers(post_id)
    if not servers:
        return (await get_setting("template_farm_live_closed", DEFAULT_FARM_LIVE_CLOSED)) or DEFAULT_FARM_LIVE_CLOSED

    tmpl_active = (await get_setting("template_farm_live_active", DEFAULT_FARM_LIVE_ACTIVE)) or DEFAULT_FARM_LIVE_ACTIVE
    tmpl_item = (await get_setting("template_farm_live_item", DEFAULT_FARM_LIVE_ITEM)) or DEFAULT_FARM_LIVE_ITEM

    blocks = []
    for idx, (s_id, u_id, r_nick, tg_u, sections, lnk, friends) in enumerate(servers, 1):
        item = tmpl_item.replace("[Номер_Сервера]", str(idx))
        item = item.replace("[Ник_Роблокс]", r_nick or "")
        item = item.replace("[Искатель]", tg_u or "")
        item = item.replace("[Секции]", str(sections))
        item = item.replace("[Друзья]", friends or "Не принимает")
        item = item.replace("[Ссылка]", html.escape(lnk or "", quote=False))
        blocks.append(item.strip())

    return tmpl_active.replace("[Список_Серверов]", "\n\n".join(blocks))

async def alert_farm_failure(reason: str, publisher_mention: str = ""):
    """Вариант Б: запасного варианта нет, при сбое громко сообщаем в тему «Секретки»."""
    owner_ping = f'<a href="tg://user?id={OWNER_ID}">\u2060</a>' if OWNER_ID else ""
    who = f"👤 Публикатор: {publisher_mention}\n" if publisher_mention else ""
    text = (
        f"⚠️{owner_ping} <b>Фарм сервер НЕ опубликован / не обновлён в канале!</b>\n"
        f"{who}"
        f"<b>Причина:</b> <code>{html.escape(reason)}</code>"
    )
    try:
        await bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=text,
            message_thread_id=TOPIC_SECRETS_ID,
            parse_mode="HTML"
        )
    except Exception as ex:
        logging.error(f"Не удалось отправить алерт по фарму: {ex}")

async def publish_or_update_farm_channel(channel_id: str, post_id: int):
    """
    Публикует или обновляет live-пост фарма ТОЛЬКО через юзербота.
    Без запасного варианта через бота: при проблеме бросает исключение.
    """
    post = await get_farm_post_by_id(post_id)
    if not post:
        return

    servers = await get_active_farm_servers(post_id)
    # Пункт 3: серверов нет и поста в канале ещё нет, значит ничего не публикуем
    if not servers and not post["message_id"]:
        logging.info(f"[Фарм #{post_id}] Нет активных серверов и нет поста в канале: публикация пропущена.")
        return

    if not userbot:
        raise RuntimeError("юзербот не настроен (нет USER_SESSION_STRING / TELEGRAM_API_ID / TELEGRAM_API_HASH)")
    if not userbot.is_connected():
        await userbot.connect()
    if not await userbot.is_user_authorized():
        raise RuntimeError("сессия юзербота недействительна, нужна новая строка сессии")

    final_text = await render_farm_post_text(post_id)
    parsed_text, entities = await parse_telethon_custom_emojis(userbot, final_text)

    target_peer = channel_id
    if isinstance(target_peer, str) and (target_peer.startswith("-100") or target_peer.lstrip("-").isdigit()):
        target_peer = int(target_peer)

    async def _send_new():
        sent_msg = await userbot.send_message(
            entity=target_peer,
            message=parsed_text,
            formatting_entities=entities,
            link_preview=False
        )
        def _upd_mid():
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute("UPDATE farm_posts SET message_id = %s WHERE id = %s", (sent_msg.id, post_id))
        await asyncio.to_thread(_upd_mid)
        logging.info(f"[Фарм #{post_id}] Новый пост опубликован в {channel_id} (msg_id={sent_msg.id}).")

    async def _do():
        if post["message_id"]:
            try:
                await userbot.edit_message(
                    entity=target_peer,
                    message=post["message_id"],
                    text=parsed_text,
                    formatting_entities=entities,
                    link_preview=False
                )
                logging.info(f"[Фарм #{post_id}] Пост обновлён в {channel_id}.")
                return
            except MessageNotModifiedError:
                return
            except MessageIdInvalidError:
                # Пост удалили вручную: если серверы есть, публикуем заново
                logging.warning(f"[Фарм #{post_id}] Пост в канале удалён вручную.")
                if not servers:
                    return
                await _send_new()
                return
        await _send_new()

    try:
        await _do()
    except FloodWaitError as fw:
        logging.warning(f"[Фарм #{post_id}] FloodWait {fw.seconds} сек, повтор.")
        await asyncio.sleep(fw.seconds + 1)
        await _do()

async def publish_farm_delayed(channel_id: str, post_id: int, publisher_id: int, publisher_mention: str, delay: int = CHANNEL_DELAY_SECONDS):
    logging.info(f"[Фарм #{post_id}] Ожидание форы {delay} сек перед публикацией...")
    await asyncio.sleep(delay)
    async with farm_lock:
        try:
            await publish_or_update_farm_channel(channel_id, post_id)
        except Exception as e:
            reason = f"{type(e).__name__}: {e}"
            logging.error(f"Ошибка отложенной публикации фарма #{post_id}: {reason}", exc_info=True)
            await alert_farm_failure(reason, publisher_mention)
            try:
                await bot.send_message(
                    publisher_id,
                    "⚠️ Ваш фарм сервер принят, но в канал он не попал из-за технической ошибки. "
                    "Администрация уже уведомлена."
                )
            except Exception:
                pass

# ----------------------------------------------------------------------
# БЕЙДЖ МУТАТОР: ДИАЛОГ ПУБЛИКАТОРА, ПУБЛИКАЦИЯ И ТАЙМЕР
# ----------------------------------------------------------------------
async def render_mutator_active(link: str) -> str:
    tmpl = (await get_setting("template_mutator_active", DEFAULT_MUTATOR_ACTIVE)) or DEFAULT_MUTATOR_ACTIVE
    return tmpl.replace("[Ссылка]", html.escape(link, quote=False))

async def expire_mutator_badge(badge_id: int, channel_id: str, message_id: int) -> bool:
    """Редактирует пост на «истекший» шаблон. True, если пост больше трогать не нужно."""
    text = (await get_setting("template_mutator_expired", DEFAULT_MUTATOR_EXPIRED)) or DEFAULT_MUTATOR_EXPIRED
    try:
        if userbot:
            if not userbot.is_connected():
                await userbot.connect()
            target_peer = channel_id
            if isinstance(target_peer, str) and (target_peer.startswith("-100") or target_peer.lstrip("-").isdigit()):
                target_peer = int(target_peer)
            parsed_text, entities = await parse_telethon_custom_emojis(userbot, text)
            await userbot.edit_message(
                entity=target_peer, message=message_id, text=parsed_text,
                formatting_entities=entities, link_preview=False
            )
        else:
            try:
                await bot.edit_message_caption(
                    chat_id=channel_id, message_id=message_id, caption=text, parse_mode="HTML"
                )
            except TelegramBadRequest as tb:
                if "no caption" in str(tb).lower() or "there is no text" in str(tb).lower() or "message can't be edited" in str(tb).lower():
                    await bot.edit_message_text(
                        chat_id=channel_id, message_id=message_id, text=text,
                        parse_mode="HTML", link_preview_options=LinkPreviewOptions(is_disabled=True)
                    )
                else:
                    raise
        return True
    except MessageNotModifiedError:
        return True
    except MessageIdInvalidError:
        logging.warning(f"[Мутатор #{badge_id}] Сообщение {message_id} удалено в канале, закрываю запись.")
        return True
    except FloodWaitError as fw:
        logging.warning(f"[Мутатор #{badge_id}] FloodWait {fw.seconds} c, повторю позже.")
        await asyncio.sleep(min(fw.seconds, 30))
        return False
    except Exception as e:
        logging.error(f"[Мутатор #{badge_id}] Не удалось обновить пост на истекший: {e}")
        return False

async def mutator_timer_worker():
    logging.info("Фоновый воркер бейджей мутатора запущен.")
    while True:
        try:
            def _get():
                with get_db() as conn:
                    with conn.cursor() as cur:
                        cur.execute("""
                            SELECT id, channel_id, message_id, attempts
                            FROM mutator_badges
                            WHERE is_expired = FALSE
                              AND message_id != 0
                              AND created_at <= CURRENT_TIMESTAMP - (%s * INTERVAL '1 second')
                        """, (MUTATOR_TIMER_SECONDS,))
                        return cur.fetchall()
            rows = await asyncio.to_thread(_get)
            for b_id, ch_id, mid, attempts in rows:
                ok = await expire_mutator_badge(b_id, ch_id, mid)
                # при повторяющихся ошибках сдаёмся после 5 попыток, чтобы не спамить
                done = ok or (attempts + 1) >= 5
                def _upd(b_id=b_id, done=done):
                    with get_db() as conn:
                        with conn.cursor() as cur:
                            cur.execute(
                                "UPDATE mutator_badges SET attempts = attempts + 1, is_expired = %s WHERE id = %s",
                                (done, b_id)
                            )
                await asyncio.to_thread(_upd)
        except Exception as err:
            logging.error(f"Ошибка в mutator_timer_worker: {err}")
        await asyncio.sleep(5)

@router.callback_query(F.data == "pub_send_mutator")
async def pub_send_mutator(call: CallbackQuery, state: FSMContext):
    if not await is_secret_publisher(call.from_user.id): return
    ch_id = await check_channel_rights(bot, call.from_user.id)
    if not ch_id:
        await call.message.answer(f"{ICON_CROSS} Сначала привяжите канал через меню секреток!", reply_markup=publisher_menu_kb(), parse_mode="HTML")
        await call.answer()
        return
    await state.clear()
    await state.set_state(MutatorPublisherStates.waiting_for_link)
    await call.message.answer("🧬 <b>Бейдж мутатор</b>\n\nОтправьте <b>ссылку на VIP-сервер</b> Roblox:", parse_mode="HTML")
    await call.answer()

@router.message(MutatorPublisherStates.waiting_for_link, F.text)
async def mutator_link_proc(message: Message, state: FSMContext):
    clean = re.sub(r'\s+', '', message.text.strip())
    low = clean.lower()
    if not ("roblox.com" in low and ("privateserverlinkcode=" in low or "share?code=" in low)):
        await message.answer(
            f"{ICON_CROSS} <b>Некорректная ссылка на VIP-сервер!</b>\n"
            "Отправьте корректную ссылку на Roblox VIP сервер.",
            parse_mode="HTML"
        )
        return
    await state.update_data(mut_link=clean)
    preview = await render_mutator_active(clean)
    conf_kb = InlineKeyboardMarkup(inline_keyboard=[[
        IB(text="✅ Опубликовать", callback_data="mut_conf_yes"),
        IB(text="❌ Отмена", callback_data="mut_conf_no")
    ]])
    await message.answer("Предпросмотр поста:")
    await message.answer(preview, reply_markup=conf_kb, parse_mode="HTML", link_preview_options=LinkPreviewOptions(is_disabled=True))
    await state.set_state(MutatorPublisherStates.waiting_for_confirm)

@router.callback_query(MutatorPublisherStates.waiting_for_confirm, F.data.startswith("mut_conf_"))
async def mutator_confirm_proc(call: CallbackQuery, state: FSMContext):
    if call.data == "mut_conf_no":
        await call.message.edit_text(f"{ICON_CROSS} Публикация бейджа мутатора отменена.", parse_mode="HTML")
        await state.clear()
        await call.answer()
        return

    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    data = await state.get_data()
    await state.clear()
    if not data or "mut_link" not in data:
        await call.answer()
        return

    ch_id = await check_channel_rights(bot, call.from_user.id)
    if not ch_id:
        await call.message.answer(f"{ICON_CROSS} Канал не привязан!", parse_mode="HTML")
        await call.answer()
        return

    link = data["mut_link"]
    text = await render_mutator_active(link)
    try:
        # Без форы для стаффа: сразу в канал
        if userbot:
            if not userbot.is_connected():
                await userbot.connect()
            target_peer = ch_id
            if isinstance(target_peer, str) and (target_peer.startswith("-100") or target_peer.lstrip("-").isdigit()):
                target_peer = int(target_peer)
            parsed_text, entities = await parse_telethon_custom_emojis(userbot, text)
            if os.path.exists(MUTATOR_PHOTO):
                sent = await userbot.send_file(
                    target_peer, MUTATOR_PHOTO, caption=parsed_text, formatting_entities=entities
                )
            else:
                logging.warning(f"Файл {MUTATOR_PHOTO} не найден, мутатор публикуется без фото.")
                sent = await userbot.send_message(
                    entity=target_peer, message=parsed_text,
                    formatting_entities=entities, link_preview=False
                )
            msg_id = sent.id
        else:
            if os.path.exists(MUTATOR_PHOTO):
                sent = await bot.send_photo(
                    chat_id=ch_id, photo=FSInputFile(MUTATOR_PHOTO), caption=text, parse_mode="HTML"
                )
            else:
                logging.warning(f"Файл {MUTATOR_PHOTO} не найден, мутатор публикуется без фото.")
                sent = await bot.send_message(
                    chat_id=ch_id, text=text, parse_mode="HTML",
                    link_preview_options=LinkPreviewOptions(is_disabled=True)
                )
            msg_id = sent.message_id

        def _save():
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO mutator_badges (user_id, channel_id, message_id, link) VALUES (%s, %s, %s, %s)",
                        (call.from_user.id, ch_id, msg_id, link)
                    )
        await asyncio.to_thread(_save)

        await call.message.answer(
            f"{ICON_CHECK} <b>Бейдж мутатор опубликован в канале!</b>\n"
            f"⏱ Через <b>8:10</b> пост сам изменится на «истекший».",
            parse_mode="HTML"
        )
    except Exception as e:
        logging.error(f"Ошибка публикации бейджа мутатора: {e}", exc_info=True)
        await call.message.answer(f"{ICON_CROSS} Ошибка публикации в канал: {html.escape(str(e))}", parse_mode="HTML")
    await call.answer()

# ----------------------------------------------------------------------
# ФАРМ СЕРВЕР: ДИАЛОГ ИСКАТЕЛЯ
# ----------------------------------------------------------------------
@router.callback_query(F.data == "pub_send_farm")
async def pub_send_farm(call: CallbackQuery, state: FSMContext):
    if not await is_secret_publisher(call.from_user.id): return
    ch_id = await check_channel_rights(bot, call.from_user.id)
    if not ch_id:
        await call.message.answer(f"{ICON_CROSS} Сначала привяжите канал через меню секреток!", reply_markup=publisher_menu_kb(), parse_mode="HTML")
        await call.answer()
        return

    # Обычным публикаторам 1 активный сервер фарма, главным админам без ограничений
    if not await is_main_admin(call.from_user.id):
        if await count_user_active_farms(call.from_user.id) >= 1:
            await call.answer("❌ У вас уже есть активный фарм сервер! Снимите его, прежде чем добавлять новый.", show_alert=True)
            return

    await state.clear()
    await state.set_state(FarmPublisherStates.waiting_for_nick)
    await call.message.answer("🌾 <b>Фарм сервер</b>\n\n1. Введите ваш <b>ник в Roblox</b>:", parse_mode="HTML")
    await call.answer()

@router.message(FarmPublisherStates.waiting_for_nick, F.text)
async def farm_nick_proc(message: Message, state: FSMContext):
    nick_input = message.text.strip()
    rbx_info = await check_roblox_username(nick_input)
    if rbx_info:
        official_name = rbx_info["name"]
        rbx_id = rbx_info["id"]
        profile_str = f"<a href='https://www.roblox.com/users/{rbx_id}/profile'>{html.escape(official_name)}</a>"
    else:
        official_name = nick_input
        profile_str = f"<code>{html.escape(nick_input)}</code>"

    await state.update_data(farm_nick=profile_str, farm_raw_nick=official_name)
    await state.set_state(FarmPublisherStates.waiting_for_sections)
    await message.answer(
        f"{ICON_CHECK} Ник зафиксирован: {profile_str}\n\n"
        "2. Введите <b>количество секций</b> (целое число, например: <code>25</code>):",
        parse_mode="HTML",
        link_preview_options=LinkPreviewOptions(is_disabled=True)
    )

@router.message(FarmPublisherStates.waiting_for_sections, F.text)
async def farm_sections_proc(message: Message, state: FSMContext):
    t = message.text.strip()
    if not re.fullmatch(r"[0-9]{1,3}", t) or int(t) < 1:
        await message.answer(
            f"{ICON_CROSS} <b>Нужно целое положительное число!</b>\n"
            "Введите количество секций цифрами (например: <code>25</code>):",
            parse_mode="HTML"
        )
        return

    await state.update_data(farm_sections=int(t))
    await state.set_state(FarmPublisherStates.waiting_for_friends)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        IB(text="🟢 Принимаю", callback_data="farm_fr_yes"),
        IB(text="🔴 Не принимаю", callback_data="farm_fr_no")
    ]])
    await message.answer(
        f"{ICON_CHECK} Секций: <b>{int(t)}</b>\n\n3. Принимаете ли вы заявки в друзья на сервере?",
        reply_markup=kb, parse_mode="HTML"
    )

@router.callback_query(FarmPublisherStates.waiting_for_friends, F.data.startswith("farm_fr_"))
async def farm_friends_proc(call: CallbackQuery, state: FSMContext):
    ans = "Принимает" if call.data == "farm_fr_yes" else "Не принимает"
    await state.update_data(farm_friends=ans)
    await state.set_state(FarmPublisherStates.waiting_for_link)
    await call.message.edit_text(
        f"Статус друзей: <b>{ans}</b>\n\n4. Теперь отправьте <b>ссылку на VIP-сервер</b> Roblox:",
        parse_mode="HTML"
    )
    await call.answer()

@router.message(FarmPublisherStates.waiting_for_link, F.text)
async def farm_link_proc(message: Message, state: FSMContext):
    raw = message.text.strip()
    clean = re.sub(r'\s+', '', raw)
    clean_lower = clean.lower()

    is_roblox = "roblox.com" in clean_lower
    is_vip = ("privateserverlinkcode=" in clean_lower) or ("share?code=" in clean_lower)

    if not (is_roblox and is_vip):
        await message.answer(
            f"{ICON_CROSS} <b>Некорректная ссылка на VIP-сервер!</b>\n"
            "Отправьте корректную ссылку на Roblox VIP сервер.",
            parse_mode="HTML"
        )
        return

    await state.update_data(farm_link=clean)
    data = await state.get_data()

    user = message.from_user
    tg_display = f"@{user.username}" if user.username else get_user_mention(user)
    await state.update_data(farm_tg=tg_display)

    preview_text = (
        "🌾 <b>Предпросмотр данных вашего фарм сервера:</b>\n\n"
        f"• Искатель: {tg_display}\n"
        f"• Ник в Roblox: {data['farm_nick']}\n"
        f"• Секций: <b>{data['farm_sections']}</b>\n"
        f"• Заявки в друзья: <b>{data['farm_friends']}</b>\n"
        f"• Ссылка: {html.escape(clean, quote=False)}\n\n"
        "<i>Подтвердите публикацию:</i>"
    )

    conf_kb = InlineKeyboardMarkup(inline_keyboard=[[
        IB(text="✅ Опубликовать", callback_data="farm_conf_yes"),
        IB(text="❌ Отмена", callback_data="farm_conf_no")
    ]])
    await message.answer(preview_text, reply_markup=conf_kb, parse_mode="HTML", link_preview_options=LinkPreviewOptions(is_disabled=True))
    await state.set_state(FarmPublisherStates.waiting_for_confirm)

@router.callback_query(FarmPublisherStates.waiting_for_confirm, F.data.startswith("farm_conf_"))
async def farm_confirm_proc(call: CallbackQuery, state: FSMContext):
    action = call.data.split("_")[2]
    if action == "no":
        await call.message.edit_text(f"{ICON_CROSS} Добавление фарм сервера отменено.", parse_mode="HTML")
        await state.clear()
        await call.answer()
        return

    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    data = await state.get_data()
    if not data or "farm_link" not in data or "farm_friends" not in data:
        await call.answer()
        return

    ch_id = await check_channel_rights(bot, call.from_user.id)
    if not ch_id:
        await call.message.answer(f"{ICON_CROSS} Канал не привязан!", parse_mode="HTML")
        await state.clear()
        await call.answer()
        return

    async with farm_lock:
        # Повторная проверка лимита уже под локом (защита от двойного нажатия)
        if not await is_main_admin(call.from_user.id):
            if await count_user_active_farms(call.from_user.id) >= 1:
                await call.message.answer("❌ У вас уже есть активный фарм сервер!")
                await state.clear()
                await call.answer()
                return

        active_post = await get_active_farm_post(ch_id)
        if not active_post:
            def _create_p():
                with get_db() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            "INSERT INTO farm_posts (channel_id, message_id, is_active) VALUES (%s, 0, TRUE) RETURNING id",
                            (ch_id,)
                        )
                        return cur.fetchone()[0]
            post_id = await asyncio.to_thread(_create_p)
        else:
            post_id = active_post["id"]

        def _save_srv():
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO farm_servers (post_id, user_id, roblox_nick, tg_user, sections_count, link, accepts_friends, is_active)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, TRUE) RETURNING id
                    """, (post_id, call.from_user.id, data["farm_nick"], data["farm_tg"], data["farm_sections"], data["farm_link"], data["farm_friends"]))
                    return cur.fetchone()[0]
        await asyncio.to_thread(_save_srv)

    # 1. Карточка стаффу в тему «Секретки» + созыв
    spawn(send_prelinks(
        _prelink_header("фарм сервер") +
        f"• Ник Roblox: {data['farm_nick']}\n"
        f"• Секций: <b>{data['farm_sections']}</b>\n"
        f"• В друзья: <b>{data['farm_friends']}</b>\n"
        f"• Ссылка: {html.escape(data['farm_link'], quote=False)}"
    ))
    staff_text = (
        f"⚡️ <b>ФАРМ СЕРВЕР ДЛЯ СТАФФА (Фора {CHANNEL_DELAY_SECONDS} сек.)</b> ⚡️\n"
        f"<i>Сервер появится в канале через {CHANNEL_DELAY_SECONDS} секунд! Предссылки получат его через {STAFF_DELAY_SECONDS} сек.</i>\n"
        f"👤 Опубликовал: {call.from_user.mention_html()}\n\n"
        f"• Ник Roblox: {data['farm_nick']}\n"
        f"• Секций: <b>{data['farm_sections']}</b>\n"
        f"• В друзья: <b>{data['farm_friends']}</b>\n"
        f"• Ссылка: {html.escape(data['farm_link'], quote=False)}"
    )
    try:
        if not TEST_MODE_ON:
          await bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=staff_text,
            message_thread_id=TOPIC_SECRETS_ID,
            parse_mode="HTML",
            link_preview_options=LinkPreviewOptions(is_disabled=True)
        )
    except Exception as ex_st:
        logging.warning(f"Ошибка карточки стаффу (фарм): {ex_st}")
    await call_staff(f"<b>Новый фарм сервер!</b> У вас {STAFF_DELAY_SECONDS} сек. приоритета до предссылок (канал через {CHANNEL_DELAY_SECONDS} сек.)!", exclude_ids=[call.from_user.id])

    # 2. Отложенная публикация/обновление в канал
    spawn(
        publish_farm_delayed(ch_id, post_id, call.from_user.id, call.from_user.mention_html(), delay=CHANNEL_DELAY_SECONDS)
    )

    await call.message.edit_text(
        f"{ICON_CHECK} <b>Фарм сервер принят!</b>\n\n"
        f"⏳ Админы созваны, пост в канале <b>{ch_id}</b> выйдет/обновится ровно через {CHANNEL_DELAY_SECONDS} сек.\n"
        "Управлять своими серверами можно через меню <b>«Мои сервера фарма»</b>.",
        parse_mode="HTML"
    )
    await state.clear()
    await call.answer()

# ----------------------------------------------------------------------
# ФАРМ СЕРВЕР: МОИ СЕРВЕРА И СНЯТИЕ
# ----------------------------------------------------------------------
@router.callback_query(F.data == "pub_my_farms")
async def pub_my_farms(call: CallbackQuery):
    if not await is_secret_publisher(call.from_user.id): return
    def _q():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT s.id, s.roblox_nick, s.sections_count, s.post_id
                    FROM farm_servers s
                    JOIN farm_posts p ON s.post_id = p.id
                    WHERE s.user_id = %s AND s.is_active = TRUE AND p.is_active = TRUE
                    ORDER BY s.id ASC
                """, (call.from_user.id,))
                return cur.fetchall()
    srvs = await asyncio.to_thread(_q)
    if not srvs:
        await call.message.answer("📭 У вас сейчас нет активных фарм серверов.")
        await call.answer()
        return

    kb_bts = []
    for s_id, r_nick, sec_cnt, p_id in srvs:
        clean_n = re.sub(r'<[^>]+>', '', r_nick or "")
        kb_bts.append([IB(text=f"❌ Снять: {clean_n} ({sec_cnt} секций)", callback_data=f"farm_del_{s_id}_{p_id}")])

    await call.message.answer(
        "📋 Ваши активные фарм серверы:\nНажмите на кнопку, чтобы снять сервер из канала:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_bts)
    )
    await call.answer()

@router.callback_query(F.data.startswith("farm_del_"))
async def pub_farm_del_proc(call: CallbackQuery):
    if not await is_secret_publisher(call.from_user.id): return
    parts = call.data.split("_")
    s_id = int(parts[2])
    post_id = int(parts[3])
    uid = call.from_user.id

    async with farm_lock:
        def _del_and_check():
            with get_db() as conn:
                with conn.cursor() as cur:
                    # Снять можно только свой активный сервер
                    cur.execute(
                        "UPDATE farm_servers SET is_active = FALSE "
                        "WHERE id = %s AND post_id = %s AND user_id = %s AND is_active = TRUE RETURNING id",
                        (s_id, post_id, uid)
                    )
                    if not cur.fetchone():
                        return None
                    cur.execute("SELECT COUNT(*) FROM farm_servers WHERE post_id = %s AND is_active = TRUE", (post_id,))
                    rem_count = cur.fetchone()[0]
                    cur.execute("SELECT channel_id FROM farm_posts WHERE id = %s", (post_id,))
                    ch = cur.fetchone()[0]
                    if rem_count == 0:
                        cur.execute("UPDATE farm_posts SET is_active = FALSE WHERE id = %s", (post_id,))
                    return rem_count, ch

        res = await asyncio.to_thread(_del_and_check)
        if res is None:
            await call.answer("⚠️ Сервер уже снят или не найден!", show_alert=True)
            return
        rem_count, ch_id = res

        # Оставшиеся серверы перенумеруются при рендере; если осталось 0 и поста в канале
        # ещё нет, publish_or_update_farm_channel ничего не отправит (пункт 3)
        publish_error = None
        try:
            await publish_or_update_farm_channel(ch_id, post_id)
        except Exception as e:
            publish_error = f"{type(e).__name__}: {e}"
            logging.error(f"Ошибка обновления поста фарма после удаления: {publish_error}", exc_info=True)

    if publish_error:
        await alert_farm_failure(publish_error, call.from_user.mention_html())
        await call.message.edit_text(
            f"{ICON_CHECK} Сервер снят с поиска, но пост в канале не удалось обновить. Администрация уведомлена.",
            parse_mode="HTML"
        )
    else:
        await call.message.edit_text(f"{ICON_CHECK} Ваш фарм сервер снят. Пост в канале обновлён!", parse_mode="HTML")
    await call.answer()

# ----------------------------------------------------------------------
# СИСТЕМА НАБОРА В ХЕЛПЕРЫ И ИСКАТЕЛИ СЕКРЕТОВ
# ----------------------------------------------------------------------

# --- НАБОР В ХЕЛПЕРЫ (В ТЕМУ ПОДДЕРЖКИ) ---
@router.message(F.text.in_(btn_texts(BTN_HELPER_APPLY)), F.chat.type == "private")
async def start_helper_apply(message: Message, state: FSMContext):
    await register_user(message.from_user.id)
    if await is_banned(message.from_user.id): return
    if await get_setting("helper_recruitment", "true") != "true":
        await message.answer(f"{ICON_WARN} Набор в команду поддержки в данный момент закрыт.", parse_mode="HTML")
        return
    await state.clear()
    await state.set_state(Form.helper_name)
    await message.answer(f"<tg-emoji emoji-id='5988023995125993550'>🛠</tg-emoji> <b>Заявка в команду поддержки (Хелперы)</b>\n\n{NUM_1} Ваше имя:", parse_mode="HTML")

@router.message(Form.helper_name, F.text)
async def process_helper_name(message: Message, state: FSMContext):
    await state.update_data(helper_name=message.text.strip())
    await state.set_state(Form.helper_age)
    await message.answer(f"{NUM_2} Ваш возраст:", parse_mode="HTML")

@router.message(Form.helper_age, F.text)
async def process_helper_age(message: Message, state: FSMContext):
    await state.update_data(helper_age=message.text.strip())
    await state.set_state(Form.helper_time)
    await message.answer(f"{NUM_3} Сколько времени вы готовы уделять на ответы в поддержке?", parse_mode="HTML")

@router.message(Form.helper_time, F.text)
async def process_helper_time(message: Message, state: FSMContext):
    await state.update_data(helper_time=message.text.strip())
    await state.set_state(Form.helper_why)
    await message.answer(f"{NUM_4} Почему вы хотите именно к нам?", parse_mode="HTML")

@router.message(Form.helper_why, F.text)
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
        f"👤 Username: {('@' + user.username) if user.username else 'нет'}\n\n"
        f"1. <b>Имя:</b> {html.escape(data.get('helper_name', 'Не указано'))}\n"
        f"2. <b>Возраст:</b> {html.escape(data.get('helper_age', 'Не указан'))}\n"
        f"3. <b>Время на поддержку:</b> {html.escape(data.get('helper_time', 'Не указано'))}\n"
        f"4. <b>Почему к нам:</b> {html.escape(why_text)}"
    )

    sent = await bot.send_message(
        ADMIN_CHAT_ID, 
        admin_text, 
        reply_markup=helper_decision_kb(user.id), 
        message_thread_id=TOPIC_SUPPORT_ID,
        parse_mode="HTML"
    )
    await map_message(sent.message_id, user.id, 0)
    kb = await main_keyboard(user.id)
    night_txt = get_night_notice()
    await message.answer(f"{ICON_CHECK} Ваша анкета успешно отправлена администрации! Ожидайте ответа.{night_txt}", reply_markup=kb, parse_mode="HTML")
    await state.clear()

@router.callback_query(F.data.startswith("helper_accept_"))
async def helper_accept_callback(call: CallbackQuery):
    if not await is_main_admin(call.from_user.id) and not await is_support_member(call.from_user.id):
        await call.answer("❌ У вас нет прав!", show_alert=True)
        return
    user_id = int(call.data.split("_")[2])
    try:
        await bot.send_message(user_id, "<tg-emoji emoji-id='5994502837327892086'>🎉</tg-emoji> <b>Поздравляем! Ваша заявка на хелпера принята!</b> Администрация свяжется с вами в ближайшее время.", parse_mode="HTML")
    except Exception:
        pass
    await call.message.edit_text((call.message.html_text or '') + "\n\n🟢 <b>Статус:</b> Одобрено ✅", parse_mode="HTML")
    await call.answer("Заявка принята!")

@router.callback_query(F.data.startswith("helper_reject_"))
async def helper_reject_callback(call: CallbackQuery):
    if not await is_main_admin(call.from_user.id) and not await is_support_member(call.from_user.id):
        await call.answer("❌ У вас нет прав!", show_alert=True)
        return
    user_id = int(call.data.split("_")[2])
    try:
        await bot.send_message(user_id, f"{ICON_CROSS} К сожалению, ваша заявка на хелпера была отклонена.", parse_mode="HTML")
    except Exception:
        pass
    await call.message.edit_text((call.message.html_text or '') + "\n\n🔴 <b>Статус:</b> Отклонено ❌", parse_mode="HTML")
    await call.answer("Заявка отклонена!")

# --- НАБОР В ИСКАТЕЛИ СЕКРЕТОК (В ТЕМУ СЕКРЕТОК) ---
@router.message(F.text.in_(btn_texts(BTN_SECRET_APPLY)), F.chat.type == "private")
async def start_secret_apply(message: Message, state: FSMContext):
    await register_user(message.from_user.id)
    if await is_banned(message.from_user.id): return
    if await get_setting("secret_recruitment", "true") != "true":
        await message.answer(f"{ICON_WARN} Набор в искатели секреток в данный момент закрыт.", parse_mode="HTML")
        return
    await state.clear()
    await state.set_state(Form.secret_name)
    await message.answer(f"<tg-emoji emoji-id='5345840270279724328'>🔍</tg-emoji> <b>Заявка в искатели секреток</b>\n\n{NUM_1} Ваше имя:", parse_mode="HTML")

@router.message(Form.secret_name, F.text)
async def process_secret_name(message: Message, state: FSMContext):
    await state.update_data(secret_name=message.text.strip())
    await state.set_state(Form.secret_age)
    await message.answer(f"{NUM_2} Ваш возраст:", parse_mode="HTML")

@router.message(Form.secret_age, F.text)
async def process_secret_age(message: Message, state: FSMContext):
    await state.update_data(secret_age=message.text.strip())
    await state.set_state(Form.secret_vip)
    await message.answer(f"{NUM_3} Есть ли у вас вип сервер в Tower Of Hell?", parse_mode="HTML")

@router.message(Form.secret_vip, F.text)
async def process_secret_vip(message: Message, state: FSMContext):
    await state.update_data(secret_vip=message.text.strip())
    await state.set_state(Form.secret_time)
    await message.answer(f"{NUM_4} Сколько вы готовы уделять времени поиску секреток?", parse_mode="HTML")

@router.message(Form.secret_time, F.text)
async def process_secret_time(message: Message, state: FSMContext):
    await state.update_data(secret_time=message.text.strip())
    await state.set_state(Form.secret_why)
    await message.answer(f"{NUM_5} Почему вы хотите именно к нам?", parse_mode="HTML")

@router.message(Form.secret_why, F.text)
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
        f"👤 Username: {('@' + user.username) if user.username else 'нет'}\n\n"
        f"1. <b>Имя:</b> {html.escape(data.get('secret_name', 'Не указано'))}\n"
        f"2. <b>Возраст:</b> {html.escape(data.get('secret_age', 'Не указан'))}\n"
        f"3. <b>VIP сервер в ToH:</b> {html.escape(data.get('secret_vip', 'Не указано'))}\n"
        f"4. <b>Время на поиск:</b> {html.escape(data.get('secret_time', 'Не указано'))}\n"
        f"5. <b>Почему к нам:</b> {html.escape(why_text)}"
    )

    sent = await bot.send_message(
        ADMIN_CHAT_ID, 
        admin_text, 
        reply_markup=secret_decision_kb(user.id), 
        message_thread_id=TOPIC_SECRETS_ID,
        parse_mode="HTML"
    )
    await map_message(sent.message_id, user.id, 0)
    kb = await main_keyboard(user.id)
    night_txt = get_night_notice()
    await message.answer(f"{ICON_CHECK} Ваша заявка в искатели секреток успешно отправлена! Ожидайте ответа.{night_txt}", reply_markup=kb, parse_mode="HTML")
    await state.clear()

@router.callback_query(F.data.startswith("secret_accept_"))
async def secret_accept_callback(call: CallbackQuery):
    if not await is_main_admin(call.from_user.id) and not await is_support_member(call.from_user.id):
        await call.answer("❌ У вас нет прав!", show_alert=True)
        return
    user_id = int(call.data.split("_")[2])
    try:
        await bot.send_message(user_id, "<tg-emoji emoji-id='5994502837327892086'>🎉</tg-emoji> <b>Поздравляем! Ваша заявка в искатели секреток принята!</b> Администрация свяжется с вами в ближайшее время.", parse_mode="HTML")
    except Exception:
        pass
    await call.message.edit_text((call.message.html_text or '') + "\n\n🟢 <b>Статус:</b> Одобрено ✅", parse_mode="HTML")
    await call.answer("Заявка принята!")

@router.callback_query(F.data.startswith("secret_reject_"))
async def secret_reject_callback(call: CallbackQuery):
    if not await is_main_admin(call.from_user.id) and not await is_support_member(call.from_user.id):
        await call.answer("❌ У вас нет прав!", show_alert=True)
        return
    user_id = int(call.data.split("_")[2])
    try:
        await bot.send_message(user_id, f"{ICON_CROSS} К сожалению, ваша заявка в искатели секреток была отклонена.", parse_mode="HTML")
    except Exception:
        pass
    await call.message.edit_text((call.message.html_text or '') + "\n\n🔴 <b>Статус:</b> Отклонено ❌", parse_mode="HTML")
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

async def collect_media_photos(message: Message, state: FSMContext, key: str = "photos") -> list:
    data = await state.get_data()
    photos = data.get(key, [])
    if message.photo:
        photos.append(message.photo[-1].file_id)
        await state.update_data({key: photos})
    elif message.video:
        photos.append("VID:" + message.video.file_id)
        await state.update_data({key: photos})
    return photos

# --- 1. ЖАЛОБА ---
@router.message(F.text.in_(btn_texts(BTN_COMPLAINT)), F.chat.type == "private")
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

@router.message(Form.complaint_nicknames, F.text)
async def process_c_nicknames(message: Message, state: FSMContext):
    status, new_nick, rbx_info = await lookup_roblox(message.text)
    if status == "notfound":
        await message.answer(NICK_NOT_FOUND_TEXT.format(nick=html.escape(new_nick or message.text.strip()[:30])), parse_mode="HTML")
        return
    data = await state.get_data()
    nicks = data.get("nicks_list", [])
    profiles = data.get("roblox_profiles", [])
    
    if rbx_info:
        official_name = rbx_info["name"]
        rbx_id = rbx_info["id"]
        nicks.append(official_name)
        profiles.append(f"<a href='https://www.roblox.com/users/{rbx_id}/profile'>{official_name}</a> (ID: <code>{rbx_id}</code>)")
        verified_text = f"Профиль найден: <a href='https://www.roblox.com/users/{rbx_id}/profile'><b>{official_name}</b></a> (ID: <code>{rbx_id}</code>)"
    else:
        nicks.append(new_nick)
        profiles.append(f"<code>{html.escape(new_nick)}</code> (⚠️ <i>не удалось проверить в Roblox</i>)")
        verified_text = f"<code>{html.escape(new_nick)}</code> (⚠️ <i>Roblox сейчас не отвечает, ник не проверен</i>)"

    await state.update_data(nicks_list=nicks, roblox_profiles=profiles)

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        IB(text="➡️ Продолжить (перейти к сути)", callback_data="c_nicks_done")
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
        f"{NUM_5} {ICON_PHOTO} <b>Прикрепите доказательства (до 5 фото или видео):</b>\n\n"
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

@router.message(Form.complaint_photos, F.photo | F.video)
async def process_c_photo(message: Message, state: FSMContext):
    if await check_active_ticket(message):
        await state.clear()
        return

    photos = await collect_media_photos(message, state, "c_photos")
    if len(photos) > 5:
        await message.answer(f"{ICON_CROSS} Лимит превышен! Прикрепите не более 5 файлов (фото/видео).", parse_mode="HTML")
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
                f"Где произошло нарушение: {html.escape(data.get('c_place', ''))}\n"
                f"Время публикации поста: {html.escape(data.get('c_time', ''))}"
            )
            ticket_id = await dispatch_ticket_to_admin(message.from_user.id, message.from_user, "complaint", text, photos)
            await state.clear()
            night_txt = get_night_notice()
            await message.answer(f"{ICON_CHECK} Жалоба №{ticket_id} отправлена! (Прикреплено файлов: {len(photos)}){night_txt}", reply_markup=user_cancel_kb(ticket_id), parse_mode="HTML")
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
        await message.answer(f"{ICON_CHECK} Жалоба №{ticket_id} отправлена! (Прикреплено файлов: {len(photos)}){night_txt}", reply_markup=user_cancel_kb(ticket_id), parse_mode="HTML")

# --- 2. ОБЖАЛОВАНИЕ ---
@router.message(F.text.in_(btn_texts(BTN_APPEAL)), F.chat.type == "private")
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

@router.message(Form.appeal_nickname, F.text)
async def process_a_nickname(message: Message, state: FSMContext):
    status, nick_input, rbx_info = await lookup_roblox(message.text)
    if status == "notfound":
        await message.answer(NICK_NOT_FOUND_TEXT.format(nick=html.escape(nick_input or message.text.strip()[:30])), parse_mode="HTML")
        return
    
    if rbx_info:
        official_name = rbx_info["name"]
        rbx_id = rbx_info["id"]
        formatted_profile = f"<a href='https://www.roblox.com/users/{rbx_id}/profile'>{official_name}</a> (ID: <code>{rbx_id}</code>)"
        confirm_text = f"Профиль найден: <a href='https://www.roblox.com/users/{rbx_id}/profile'><b>{official_name}</b></a> (ID: <code>{rbx_id}</code>)"
    else:
        official_name = nick_input
        formatted_profile = f"<code>{html.escape(nick_input)}</code> (⚠️ <i>не удалось проверить в Roblox</i>)"
        confirm_text = f"<code>{html.escape(nick_input)}</code> (⚠️ <i>Roblox сейчас не отвечает, ник не проверен</i>)"

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
        f"{NUM_5} {ICON_PHOTO} <b>Прикрепите доказательства или скриншоты (до 5 фото или видео):</b>\n\n"
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

@router.message(Form.appeal_photos, F.photo | F.video)
async def process_a_photo(message: Message, state: FSMContext):
    if await check_active_ticket(message):
        await state.clear()
        return

    photos = await collect_media_photos(message, state, "a_photos")
    if len(photos) > 5:
        await message.answer(f"{ICON_CROSS} Лимит превышен! Прикрепите не более 5 файлов (фото/видео).", parse_mode="HTML")
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
            await message.answer(f"{ICON_CHECK} Обжалование №{ticket_id} отправлено! (Прикреплено файлов: {len(photos)}){night_txt}", reply_markup=user_cancel_kb(ticket_id), parse_mode="HTML")
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
        await message.answer(f"{ICON_CHECK} Обжалование №{ticket_id} отправлено! (Прикреплено файлов: {len(photos)}){night_txt}", reply_markup=user_cancel_kb(ticket_id), parse_mode="HTML")

# --- 3. ВОПРОС ---
@router.message(F.text.in_(btn_texts(BTN_QUESTION)), F.chat.type == "private")
async def start_question(message: Message, state: FSMContext):
    await register_user(message.from_user.id)
    if await is_banned(message.from_user.id) or await check_active_ticket(message): return
    await state.clear()
    await state.set_state(Form.question_content)
    await message.answer("<tg-emoji emoji-id='5220053623211305785'>❓</tg-emoji> Задайте ваш вопрос (можно отправить текст или до 5 фото с описанием):", parse_mode="HTML")

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
        
        caption_text = message.caption or ""
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
                if not caption_text.strip():
                    await state.update_data(q_photos=[])
                    await message.answer(
                        f"{ICON_CROSS} <b>К фото нужно добавить описание вопроса!</b>\n"
                        "Отправьте фото ещё раз с подписью (текстом) или просто напишите вопрос текстом.",
                        parse_mode="HTML"
                    )
                    return

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

        if not caption_text.strip():
            await state.update_data(q_photos=[])
            await message.answer(
                f"{ICON_CROSS} <b>К фото нужно добавить описание вопроса!</b>\n"
                "Отправьте фото ещё раз с подписью (текстом) или просто напишите вопрос текстом.",
                parse_mode="HTML"
            )
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

# --- 4. ДРУЗЬЯ ---
@router.message(F.text.in_(btn_texts(BTN_FRIENDS)), F.chat.type == "private")
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

@router.message(Form.friends_nickname, F.text)
async def process_friends_nickname(message: Message, state: FSMContext):
    status, f_nick, rbx_info = await lookup_roblox(message.text)
    if status == "notfound":
        await message.answer(NICK_NOT_FOUND_TEXT.format(nick=html.escape(f_nick or message.text.strip()[:30])), parse_mode="HTML")
        return
    async with creation_lock:
        if await check_active_ticket(message):
            await state.clear()
            return
        if rbx_info:
            f_nick = rbx_info["name"]
            text = f"Ник: <a href='https://www.roblox.com/users/{rbx_info['id']}/profile'>{html.escape(f_nick)}</a> (ID: <code>{rbx_info['id']}</code>)"
        else:
            text = f"Ник: <code>{html.escape(f_nick)}</code> (⚠️ <i>не проверен</i>)"
        ticket_id = await dispatch_ticket_to_admin(message.from_user.id, message.from_user, "friends", text, [])
        night_txt = get_night_notice()
        await message.answer(f"{ICON_CHECK} Заявка №{ticket_id} создана!{night_txt}", reply_markup=user_cancel_kb(ticket_id), parse_mode="HTML")
        await state.clear()

# ----------------------------------------------------------------------
# ОШИБКИ ПРИ «НЕ ТОМ» ТИПЕ СООБЩЕНИЯ В ДИАЛОГАХ ЗАЯВОК
# ----------------------------------------------------------------------
_TEXT_ONLY_STATES = (
    Form.complaint_nicknames, Form.complaint_reason, Form.complaint_time,
    Form.appeal_nickname, Form.appeal_time, Form.appeal_reason,
    Form.friends_nickname,
    Form.helper_name, Form.helper_age, Form.helper_time, Form.helper_why,
    Form.secret_name, Form.secret_age, Form.secret_vip, Form.secret_time, Form.secret_why,
)

@router.message(StateFilter(*_TEXT_ONLY_STATES), ~F.text, F.chat.type == "private")
async def wrong_type_need_text(message: Message):
    await message.answer(
        f"{ICON_CROSS} <b>Здесь нужен текстовый ответ.</b>\n"
        "Фото, видео и файлы на этом шаге не принимаются. Отправьте ответ обычным сообщением.",
        parse_mode="HTML"
    )

@router.message(Form.question_content, ~(F.text | F.photo), F.chat.type == "private")
async def wrong_type_question(message: Message):
    await message.answer(
        f"{ICON_CROSS} <b>Принимаются только текст или фото с описанием.</b>\n"
        "Видео и файлы в вопросе не поддерживаются.",
        parse_mode="HTML"
    )

@router.message(StateFilter(Form.complaint_photos, Form.appeal_photos), ~(F.photo | F.video), F.chat.type == "private")
async def wrong_type_proofs(message: Message):
    await message.answer(
        f"{ICON_CROSS} <b>На этом шаге нужны фото или видео-доказательства.</b>\n"
        "Отправьте их сюда либо нажмите кнопку «Пропустить» под предыдущим сообщением.",
        parse_mode="HTML"
    )

@router.message(StateFilter(Form.complaint_place, Form.appeal_place), F.chat.type == "private")
async def wrong_type_choose_server(message: Message):
    # текст кнопок главного меню должен обрабатываться своими хендлерами выше, сюда доходит остальное
    await message.answer(
        f"{ICON_CROSS} <b>Выберите вариант на кнопках под вопросом.</b>\n"
        "Текст и файлы на этом шаге не принимаются.",
        parse_mode="HTML"
    )

# ----------------------------------------------------------------------
# КНОПКИ УПРАВЛЕНИЯ ТИКЕТАМИ
# ----------------------------------------------------------------------
@router.callback_query(F.data.startswith("take_"))
async def take_ticket_handler(call: CallbackQuery):
    ticket_id = int(call.data.split("_")[1])

    if call.message.chat.id != ADMIN_CHAT_ID and not await is_support_member(call.from_user.id):
        await call.answer("❌ У вас нет прав хелпера!", show_alert=True)
        return

    ticket_info = await get_ticket_info(ticket_id)
    if not ticket_info:
        await call.answer("❌ Заявка не найдена в базе!", show_alert=True)
        return

    if ticket_info[2] not in ('pending', None, ''):
        await call.answer("❌ Заявка уже занята или обработана!", show_alert=True)
        return

    agent_number = await get_or_create_agent_number(call.from_user.id)
    await activate_ticket(ticket_id, call.from_user.id)
    await call.answer("✅ Заявка взята в работу!")

    try:
        await bot.send_message(
            ticket_info[0],
            f"👨‍💻 <b>Агент #{agent_number}</b> взял вашу заявку <b>№{ticket_id}</b> в работу!\nТеперь вы можете писать сюда сообщения.",
            parse_mode="HTML"
        )
    except Exception as e:
        logging.warning(f"Не удалось уведомить пользователя {ticket_info[0]}: {e}")

    base_text = ticket_info[3] if ticket_info[3] else (call.message.caption or call.message.text or f"Заявка №{ticket_id}")
    new_text = base_text + f"\n\n🟢 <b>В работе у:</b> {call.from_user.mention_html()} (Агент #{agent_number})"
    
    try:
        if call.message.photo or call.message.video:
            await call.message.edit_caption(caption=new_text, reply_markup=close_ticket_kb(ticket_id, call.from_user.id), parse_mode="HTML")
        else:
            await call.message.edit_text(text=new_text, reply_markup=close_ticket_kb(ticket_id, call.from_user.id), parse_mode="HTML")
    except Exception as e:
        logging.error(f"Ошибка обновления карточки при взятии: {e}")

@router.callback_query(F.data.startswith("reject_"))
async def reject_ticket_handler(call: CallbackQuery):
    ticket_id = int(call.data.split("_")[1])
    
    if call.message.chat.id != ADMIN_CHAT_ID and not await is_support_member(call.from_user.id):
        await call.answer("❌ У вас нет прав хелпера!", show_alert=True)
        return

    ticket_info = await get_ticket_info(ticket_id)
    if not ticket_info:
        await call.answer("❌ Заявка не найдена в базе!", show_alert=True)
        return

    if ticket_info[2] not in ('pending', None, ''):
        await call.answer("❌ Заявка уже обработана!", show_alert=True)
        return

    await call.answer()
    base_text = ticket_info[3] if ticket_info[3] else (call.message.caption or call.message.text or f"Заявка №{ticket_id}")

    prompt_msg = await bot.send_message(
        ADMIN_CHAT_ID,
        f"❓ <b>Укажите причину отказа для заявки №{ticket_id}:</b>\n<i>(Ответьте Reply на это сообщение)</i>",
        message_thread_id=TOPIC_SUPPORT_ID,
        parse_mode="HTML"
    )
    await add_pending_rejection(prompt_msg.message_id, ticket_id, call.message.message_id, base_text)
    await map_message(prompt_msg.message_id, 0, ticket_id)

@router.callback_query(F.data.startswith("close_"))
async def close_ticket_handler(call: CallbackQuery):
    parts = call.data.split("_")
    ticket_id = int(parts[1])
    assigned_admin_id = int(parts[2])

    ticket_info = await get_ticket_info(ticket_id)
    
    if not ticket_info or ticket_info[2] == 'closed':
        await call.answer("⚠️ Заявка уже закрыта!", show_alert=False)
        try:
            await call.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        return

    if call.from_user.id != assigned_admin_id and not await is_main_admin(call.from_user.id):
        if not await is_support_member(call.from_user.id):
            await call.answer("❌ Нет доступа!", show_alert=True)
            return
        await call.answer("❌ Закрыть тикет может только тот админ, который взял его в работу!", show_alert=True)
        return

    await call.answer("Заявка закрыта!")
    agent_no = await get_or_create_agent_number(call.from_user.id)

    await close_ticket_db(ticket_id, status='closed', admin_id=call.from_user.id)

    try:
        await bot.send_message(
            ticket_info[0],
            f"{ICON_LOCK} Ваша заявка <b>№{ticket_id}</b> закрыта поддержкой.\nОцените качество обслуживания:",
            parse_mode="HTML",
            reply_markup=rating_kb(ticket_id)
        )
    except Exception:
        pass

    base_text = ticket_info[3] or (call.message.caption or call.message.text or f"Заявка <b>№{ticket_id}</b>")
    status_text = f"\n\n🔒 <b>Заявка №{ticket_id} закрыта</b> администратором {call.from_user.mention_html()} (Агент #{agent_no})."
    new_content = base_text + status_text

    try:
        if call.message.photo or call.message.video:
            await call.message.edit_caption(caption=new_content, reply_markup=None, parse_mode="HTML")
        else:
            await call.message.edit_text(text=new_content, reply_markup=None, parse_mode="HTML")
    except Exception as e:
        logging.error(f"Ошибка редактирования карточки при закрытии: {e}")
        try:
            await call.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass

@router.callback_query(F.data.startswith("user_cancel_"))
async def user_cancel_ticket(call: CallbackQuery):
    ticket_id = int(call.data.split("_")[2])
    ticket_info = await get_ticket_info(ticket_id)
    if ticket_info and ticket_info[2] == 'pending':
        await call.answer()
        await user_close_ticket(ticket_id, by_offer=False)
        try:
            await call.message.edit_text(f"{ICON_CROSS} Заявка <b>№{ticket_id}</b> отменена вами.", parse_mode="HTML")
        except Exception:
            pass
    else:
        await call.answer("❌ Заявка уже взята в работу или закрыта, отмена недоступна.", show_alert=True)

def user_force_close_kb(ticket_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[[
        IB(text="🔒 Закрыть заявку самому", callback_data=f"ufc_{ticket_id}")
    ]])

@router.callback_query(F.data.startswith("ufc_"))
async def user_force_close(call: CallbackQuery):
    ticket_id = int(call.data.split("_")[1])
    info = await get_ticket_info(ticket_id)
    if not info or info[0] != call.from_user.id:
        await call.answer("❌ Это не ваша заявка.", show_alert=True)
        return
    if info[2] not in ('pending', 'active'):
        await call.answer("ℹ️ Заявка уже закрыта.", show_alert=True)
        try:
            await call.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        return
    await call.answer()
    await user_close_ticket(ticket_id, by_offer=True)
    try:
        await call.message.edit_text(
            f"{ICON_LOCK} Вы закрыли заявку <b>№{ticket_id}</b>.\nТеперь вы можете создать новую через меню.",
            parse_mode="HTML"
        )
    except Exception:
        pass

# ----------------------------------------------------------------------
# СИСТЕМА ОЦЕНОК
# ----------------------------------------------------------------------
@router.callback_query(F.data.startswith("rate_"))
async def process_rating(call: CallbackQuery):
    await call.answer()
    parts = call.data.split("_")
    ticket_id = int(parts[1])
    score = int(parts[2])
    user_id = call.from_user.id

    ticket_info = await get_ticket_info(ticket_id)
    admin_id = ticket_info[1] if ticket_info else 0

    user_is_staff = await is_support_member(user_id) or await is_main_admin(user_id)

    if user_is_staff:
        try:
            await call.message.edit_text(f"{ICON_STAR} [СТАФФ] Вы являетесь администратором/хелпером. Оценка не учитывается в статистике.", parse_mode="HTML")
        except TelegramBadRequest:
            pass
    else:
        await save_rating_db(ticket_id, user_id, admin_id, score)
        try:
            await call.message.edit_text(f"{ICON_STAR} Спасибо за оценку ({score}/5)! Ваше мнение учтено.", parse_mode="HTML")
        except TelegramBadRequest:
            pass

# ----------------------------------------------------------------------
# ДИАЛОГ В ЛС С ПОЛЬЗОВАТЕЛЕМ (ОТПРАВКА В ТЕМУ ПОДДЕРЖКИ)
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
        await touch_ticket(active_ticket[0], actor='user')
        user_mention = get_user_mention(message.from_user)
        text_to_group = f"📩 <b>Сообщение по заявке №{active_ticket[0]} от {user_mention} | ID: <code>{message.from_user.id}</code>:</b>\n\n{html.escape(message.text or message.caption or '')}"
        
        if message.photo:
            sent = await bot.send_photo(
                ADMIN_CHAT_ID, 
                photo=message.photo[-1].file_id, 
                caption=text_to_group, 
                message_thread_id=TOPIC_SUPPORT_ID,
                parse_mode="HTML"
            )
        else:
            sent = await bot.send_message(
                ADMIN_CHAT_ID, 
                text_to_group, 
                message_thread_id=TOPIC_SUPPORT_ID,
                parse_mode="HTML"
            )
        await map_message(sent.message_id, message.from_user.id, active_ticket[0])
        return

    kb = await main_keyboard(message.from_user.id)
    await message.answer(f"{ICON_WARN} Пожалуйста, выберите нужный пункт меню для обращения.", reply_markup=kb, parse_mode="HTML")

# ----------------------------------------------------------------------
# КОМАНДА ПРОВЕРКИ ТОЧНОГО ID ТЕМЫ
# ----------------------------------------------------------------------
@router.message(Command("topic_id"), F.chat.id == ADMIN_CHAT_ID)
async def check_topic_id_command(message: Message):
    t_id = message.message_thread_id
    await message.reply(
        f"🔍 <b>ID текущей темы в API:</b> <code>{t_id}</code>\n\n"
        f"<i>Сверьте это число с TOPIC_SUPPORT_ID или TOPIC_SECRETS_ID!</i>",
        parse_mode="HTML"
    )

# ----------------------------------------------------------------------
# КОМАНДЫ В АДМИН-ЧАТЕ
# ----------------------------------------------------------------------
async def _send_long(message: Message, text: str):
    """Режет длинный список на сообщения по строкам (лимит Telegram 4096)."""
    chunk = ""
    for line in text.split("\n"):
        if len(chunk) + len(line) + 1 > 3800:
            await message.answer(chunk, parse_mode="HTML", link_preview_options=LinkPreviewOptions(is_disabled=True))
            chunk = ""
        chunk += line + "\n"
    if chunk.strip():
        await message.answer(chunk, parse_mode="HTML", link_preview_options=LinkPreviewOptions(is_disabled=True))

def _fmt_minutes(mins) -> str:
    m = int(mins)
    return f"{m // 60} ч. {m % 60} мин." if m >= 60 else f"{m} мин."

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

    cat_names = {"complaint": "Жалоба", "appeal": "Обжалование", "question": "Вопрос", "friends": "Друзья"}
    links = await get_ticket_links([r[0] for r in pending_list])

    text = f"📋 <b>Список неразобранных заявок (ожидают хелперов): {len(pending_list)}</b>\n\n"
    for t_id, cat, u_id, mins in pending_list:
        cat_str = cat_names.get(cat, cat)
        text += f"• Заявка {ticket_num_html(t_id, links.get(t_id))} ({cat_str}) — ждет <code>{_fmt_minutes(mins)}</code> | Игрок: <code>{u_id}</code>\n"
    await _send_long(message, text)

async def restore_take_card(ticket_id: int, status_line: str):
    """Возвращает кнопку «Начать работу» на карточку тикета (после передачи)."""
    def _q():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT card_msg_id, card_kind, card_text FROM tickets WHERE ticket_id = %s", (ticket_id,))
                return cur.fetchone()
    row = await asyncio.to_thread(_q)
    msg_id, kind, card_text = row if row else (None, None, "")
    base = card_text or f"Заявка <b>№{ticket_id}</b>"
    kb = take_ticket_kb(ticket_id)
    if msg_id:
        try:
            if kind == "photo":
                await bot.edit_message_caption(chat_id=ADMIN_CHAT_ID, message_id=msg_id, caption=html_cut(base + "\n\n" + status_line, 1024), reply_markup=kb, parse_mode="HTML")
            elif kind == "ctrl":
                await bot.edit_message_text(chat_id=ADMIN_CHAT_ID, message_id=msg_id, text=f"👆 <i>Управление заявкой №{ticket_id}:</i>\n\n{status_line}", reply_markup=kb, parse_mode="HTML")
            else:
                await bot.edit_message_text(chat_id=ADMIN_CHAT_ID, message_id=msg_id, text=html_cut(base + "\n\n" + status_line, 4096), reply_markup=kb, parse_mode="HTML")
            return
        except TelegramBadRequest as e:
            if "not modified" in str(e).lower():
                return
            logging.warning(f"Тикет #{ticket_id}: не удалось вернуть кнопку на старую карточку ({e}), отправляю новую.")
        except Exception as e:
            logging.warning(f"Тикет #{ticket_id}: ошибка правки карточки ({e}), отправляю новую.")
    # запасной вариант: новая карточка с кнопкой
    sent = await bot.send_message(
        ADMIN_CHAT_ID, html_cut(base + "\n\n" + status_line, 4096), reply_markup=kb,
        message_thread_id=TOPIC_SUPPORT_ID, parse_mode="HTML"
    )
    info = await get_ticket_info(ticket_id)
    await map_message(sent.message_id, info[0] if info else 0, ticket_id)
    def _upd():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE tickets SET card_msg_id = %s, card_kind = 'text', link_msg_id = %s WHERE ticket_id = %s",
                            (sent.message_id, sent.message_id, ticket_id))
    await asyncio.to_thread(_upd)

@router.message(Command("givetik"), F.chat.id == ADMIN_CHAT_ID)
async def cmd_give_ticket(message: Message, command: CommandObject):
    uid = message.from_user.id
    is_main = await is_main_admin(uid)
    if not is_main and not await is_support_member(uid):
        return

    arg = (command.args or "").strip()
    m = re.search(r"\d+", arg)
    if not m:
        await message.answer("ℹ️ Использование: <code>/givetik номер_тикета</code>", parse_mode="HTML")
        return
    ticket_id = int(m.group())

    info = await get_ticket_info(ticket_id)
    if not info:
        await message.answer(f"❌ Заявка №{ticket_id} не найдена.", parse_mode="HTML")
        return
    user_id, adm_id, status, _card = info
    if status != 'active':
        await message.answer(f"❌ Заявка №{ticket_id} не в работе (статус: {html.escape(str(status))}), передавать нечего.", parse_mode="HTML")
        return
    if adm_id != uid and not is_main:
        await message.answer("❌ Передать тикет может только админ, который его ведёт, или главный админ.", parse_mode="HTML")
        return

    def _reset():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE tickets
                    SET status = 'pending', admin_id = NULL, last_actor = 'user',
                        updated_at = CURRENT_TIMESTAMP, last_unassigned_remind = CURRENT_TIMESTAMP,
                        reminded_idle = FALSE, last_idle_remind = NULL, user_close_offered = FALSE
                    WHERE ticket_id = %s AND status = 'active'
                """, (ticket_id,))
                return cur.rowcount
    if not await asyncio.to_thread(_reset):
        await message.answer(f"❌ Заявка №{ticket_id} уже изменилась, попробуйте ещё раз.", parse_mode="HTML")
        return

    try:
        await bot.send_message(
            user_id,
            f"🔄 Ваша заявка <b>№{ticket_id}</b> передана и ожидает нового хелпера.\n"
            "Пожалуйста, подождите, скоро вам ответят. Писать повторно не нужно.",
            parse_mode="HTML"
        )
    except Exception as e:
        logging.warning(f"Не удалось уведомить пользователя {user_id} о передаче тикета №{ticket_id}: {e}")

    try:
        await restore_take_card(ticket_id, f"🔄 <b>Передан:</b> ожидает нового хелпера (передал {message.from_user.mention_html()}).")
    except Exception as e:
        logging.error(f"Тикет #{ticket_id}: не удалось вернуть карточку после передачи: {e}")
        await message.answer(f"⚠️ Тикет передан, но карточку обновить не удалось: {html.escape(str(e))}", parse_mode="HTML")
        return

    await message.answer(f"✅ Заявка №{ticket_id} передана: теперь её может взять любой хелпер кнопкой «Начать работу».", parse_mode="HTML")

@router.message(Command("worktickets"), F.chat.id == ADMIN_CHAT_ID)
async def cmd_work_tickets(message: Message):
    if not await is_main_admin(message.from_user.id):
        await message.answer("❌ Только главные администраторы могут просматривать список тикетов в работе!", parse_mode="HTML")
        return

    def _query():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT t.ticket_id, t.category, t.user_id, t.admin_id, a.agent_number, t.last_actor,
                           EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - t.updated_at))/60 as mins_idle
                    FROM tickets t
                    LEFT JOIN admin_agents a ON t.admin_id = a.admin_id
                    WHERE t.status = 'active'
                    ORDER BY t.ticket_id ASC
                """)
                return cur.fetchall()

    try:
        work_list = await asyncio.to_thread(_query)
    except Exception as e:
        await message.answer(f"❌ Ошибка получения тикетов из БД: {e}")
        return

    if not work_list:
        await message.answer("🟢 Сейчас нет тикетов в работе.", parse_mode="HTML")
        return

    cat_names = {"complaint": "Жалоба", "appeal": "Обжалование", "question": "Вопрос", "friends": "Друзья"}
    links = await get_ticket_links([r[0] for r in work_list])

    text = f"🛠 <b>Тикеты в работе: {len(work_list)}</b>\n\n"
    for t_id, cat, u_id, adm_id, agent_no, last_actor, mins in work_list:
        cat_str = cat_names.get(cat, cat)
        agent_str = f"Агент #{agent_no}" if agent_no else "Агент"
        adm_name = f'<a href="tg://user?id={adm_id}">{await get_user_label(adm_id)}</a> ({agent_str})' if adm_id else "не назначен"
        waits = "ждёт ответа админа" if last_actor == 'user' else "ждёт ответа игрока"
        text += (
            f"• Заявка {ticket_num_html(t_id, links.get(t_id))} ({cat_str}) — у {adm_name}\n"
            f"   Игрок: <code>{u_id}</code> | {waits} <code>{_fmt_minutes(mins)}</code>\n"
        )
    await _send_long(message, text)

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
                return closed_count, round(float(avg_score), 2), ranking

    closed_cnt, avg_sc, ranking = await asyncio.to_thread(_query_mystats)
    ranking = await filter_in_admin_chat(ranking)
    user_rank = "Без места"
    for idx, (a_id, _, _) in enumerate(ranking, 1):
        if a_id == admin_id:
            user_rank = f"#{idx} из {len(ranking)}"
            break

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
        top_list = await filter_in_admin_chat(await asyncio.to_thread(_query))
    except Exception as e:
        await message.answer(f"❌ Ошибка получения топа поддержки: {e}")
        return

    if not top_list:
        await message.answer("📭 В базе пока нет данных о закрытых тикетах агентов поддержки.", parse_mode="HTML")
        return

    medials = ["🥇", "🥈", "🥉"]
    text = "🏆 <b>Общий рейтинг команды поддержки:</b>\n\n"

    for idx, (adm_id, cnt, avg_sc, agent_no) in enumerate(top_list, 1):
        medal = medials[idx - 1] if idx <= 3 else f"<b>{idx}.</b>"
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
        # убираем тех, кто и не в группе админов, и уже не публикатор
        kept = []
        for row in top_list:
            if await in_admin_chat(row[0]) or await is_secret_publisher(row[0]):
                kept.append(row)
        top_list = kept
    except Exception as e:
        await message.answer(f"❌ Ошибка получения топа секреток: {e}")
        return

    if not top_list:
        await message.answer("📭 В базе пока нет опубликованных секреток.", parse_mode="HTML")
        return

    medials = ["🥇", "🥈", "🥉"]
    text = "🔮 <b>Общий рейтинг искателей секреток (за всё время):</b>\n\n"

    for idx, (uid, total_cnt, fav_type) in enumerate(top_list, 1):
        medal = medials[idx - 1] if idx <= 3 else f"<b>{idx}.</b>"
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

    args = message.text.split(maxsplit=1)
    has_text = len(args) > 1 and bool(args[1].strip())

    broadcast_text = args[1].strip() if has_text else None
    target_msg = message.reply_to_message if (not has_text and message.reply_to_message) else None

    if not has_text and not target_msg:
        await message.answer(
            f"{ICON_CROSS} <b>Использование команды:</b>\n"
            f"{NUM_1} <code>/news Текст вашей новости</code>\n"
            f"{NUM_2} Или ответьте (Reply) командой <code>/news</code> на пост/фото с медиа.",
            parse_mode="HTML"
        )
        return

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
            if has_text:
                await bot.send_message(chat_id=user_id, text=NT(broadcast_text), parse_mode="HTML")
            else:
                await target_msg.copy_to(chat_id=user_id)
            
            success += 1
            await asyncio.sleep(0.05)
        except TelegramForbiddenError:
            blocked += 1
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
            try:
                if has_text:
                    await bot.send_message(chat_id=user_id, text=NT(broadcast_text), parse_mode="HTML")
                else:
                    await target_msg.copy_to(chat_id=user_id)
                success += 1
            except Exception as ex_retry:
                logging.error(f"Ошибка повторной отправки пользователю {user_id}: {ex_retry}")
                errors += 1
        except TelegramBadRequest as ex_bad:
            logging.error(f"Невозможно доставить пользователю {user_id} (BadRequest): {ex_bad}")
            errors += 1
        except Exception as ex_other:
            logging.error(f"Неизвестная ошибка отправки пользователю {user_id}: {ex_other}")
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
# МОДЕРАЦИЯ И ДИАЛОГ В АДМИН-ЧАТЕ (ЕДИНЫЙ ХЭНДЛЕР С ПРОВЕРКОЙ ТОПИКОВ)
# ----------------------------------------------------------------------
@router.message(F.chat.id == ADMIN_CHAT_ID)
async def handle_admin_group_messages(message: Message):
    text = (message.text or message.caption or "").strip()
    if text.startswith("/"):
        return

    thread_id = message.message_thread_id

    # 1. ТЕМА СЕКРЕТОК (TOPIC_SECRETS_ID)
    if thread_id == TOPIC_SECRETS_ID:
        if not await is_main_admin(message.from_user.id):
            try:
                await message.delete()
            except TelegramBadRequest as ex:
                logging.warning(f"Telegram отклонил удаление сообщения в секретках: {ex}")
            except Exception as e:
                logging.error(f"Не удалось удалить сообщение в секретках: {e}")
        return

    # 2. ТЕМА ПОДДЕРЖКИ (TOPIC_SUPPORT_ID)
    if thread_id == TOPIC_SUPPORT_ID:
        is_real_reply = False
        if message.reply_to_message:
            if not getattr(message.reply_to_message, "forum_topic_created", None) and message.reply_to_message.message_id != thread_id:
                is_real_reply = True

        # А. Сообщение без реального Reply на тикет
        if not is_real_reply:
            if text.startswith("!"):
                return

            try:
                await message.delete()
            except TelegramBadRequest as ex:
                logging.warning(f"Telegram отклонил удаление сообщения в поддержке: {ex}")
            except Exception as e:
                logging.error(f"Не удалось удалить сообщение без Reply в поддержке: {e}")
            return

        # Б. Ответ с префиксом "!" — внутренняя заметка хелперов
        if text.startswith("!"):
            return

        # В. Ответ хелпера по тикету
        replied_msg_id = message.reply_to_message.message_id
        pending_data = await get_pending_rejection(replied_msg_id)

        # 1) Ответ на запрос причины отказа
        if pending_data:
            pending_ticket_id, card_message_id, saved_card_text = pending_data
            ticket_info = await get_ticket_info(pending_ticket_id)
            if ticket_info:
                reason = html.escape(text or "Без причины")
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

        # 2) Пересылка ответа клиенту в ЛС
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

                client_text = f"👨‍💻 <b>Ответ поддержки (Агент #{agent_no}):</b>\n\n{html.escape(text)}"
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
# ФОНОВЫЕ ВОРКЕРЫ: НАПОМИНАНИЯ, СЕКРЕТКИ И АВТОЗАКРЫТИЕ
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
                              AND message_id != 0
                              AND created_at <= CURRENT_TIMESTAMP - (%s * INTERVAL '1 second')
                        """, (dur,))
                        return cur.fetchall()

            expired_rows = await asyncio.to_thread(_get_expired_posts)
            for pid, ch_id, mid, stype in expired_rows:
                await expire_secret_post(bot, pid, ch_id, mid, stype)

        except Exception as err:
            logging.error(f"Ошибка в secret_timer_worker: {err}")

        await asyncio.sleep(15)

async def prelink_expiry_worker():
    logging.info("Фоновый воркер срока предссылок запущен.")
    while True:
        try:
            def _take():
                with get_db() as conn:
                    with conn.cursor() as cur:
                        cur.execute("""
                            DELETE FROM prelink_users
                            WHERE expires_at IS NOT NULL AND expires_at <= CURRENT_TIMESTAMP
                            RETURNING user_id, COALESCE(is_test, FALSE)
                        """)
                        return cur.fetchall()
            expired = await asyncio.to_thread(_take)
            for uid, is_test in expired:
                try:
                    await bot.send_message(
                        uid,
                        "⏳ <b>Срок вашего доступа к Предссылкам закончился.</b>\n\n"
                        "Вы больше не получаете ссылки раньше канала. "
                        "Чтобы продлить доступ, обратитесь к администратору.",
                        parse_mode="HTML"
                    )
                except Exception as e:
                    logging.warning(f"Не удалось уведомить {uid} об окончании предссылок: {e}")
        except Exception as err:
            logging.error(f"Ошибка в prelink_expiry_worker: {err}")
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
            quiet_hours = is_quiet_hours()  # админам ночью не напоминаем, напоминания сработают после 08:00 МСК

            def _offer_check():
                with get_db() as conn:
                    with conn.cursor() as cur:
                        cur.execute("""
                            SELECT ticket_id, user_id FROM tickets
                            WHERE status IN ('pending', 'active')
                              AND last_actor = 'user'
                              AND COALESCE(user_close_offered, FALSE) = FALSE
                              AND updated_at <= CURRENT_TIMESTAMP - INTERVAL '1 hour'
                        """)
                        return cur.fetchall()
            for o_id, o_user in await asyncio.to_thread(_offer_check):
                try:
                    await bot.send_message(
                        o_user,
                        f"⏳ По вашей заявке <b>№{o_id}</b> поддержка не отвечает уже более часа.\n"
                        "Если вопрос уже неактуален или вы хотите создать новую заявку, вы можете закрыть эту самостоятельно.",
                        parse_mode="HTML", reply_markup=user_force_close_kb(o_id)
                    )
                except Exception as e:
                    logging.warning(f"Не удалось предложить закрыть тикет №{o_id}: {e}")
                def _mark_offered(o_id=o_id):
                    with get_db() as conn:
                        with conn.cursor() as cur:
                            cur.execute("UPDATE tickets SET user_close_offered = TRUE WHERE ticket_id = %s", (o_id,))
                await asyncio.to_thread(_mark_offered)

            _links_un = await get_ticket_links([r[0] for r in unassigned_tickets] + [r[0] for r in idle_tickets])
            for t_id, cat, u_id, hrs in ([] if quiet_hours else unassigned_tickets):
                hours_str = f"{int(hrs)} ч." if hrs else "2+ ч."
                alert_text = (
                    f"⏰ <b>Внимание! Необработанная заявка!</b>\n\n"
                    f"Заявка {ticket_num_html(t_id, _links_un.get(t_id))} ({cat}) ожидает ответа уже более <b>{hours_str}</b>!\n"
                    f"👤 ID пользователя: <code>{u_id}</code>\n\n"
                    f"Пожалуйста, возьмите тикет в работу!"
                )
                try:
                    sent = await bot.send_message(
                        ADMIN_CHAT_ID, 
                        alert_text, 
                        message_thread_id=TOPIC_SUPPORT_ID,
                        parse_mode="HTML"
                    )
                    await map_message(sent.message_id, u_id, t_id)
                    def _update_last_remind():
                        with get_db() as conn:
                            with conn.cursor() as cur:
                                cur.execute("UPDATE tickets SET last_unassigned_remind = CURRENT_TIMESTAMP WHERE ticket_id = %s", (t_id,))
                    await asyncio.to_thread(_update_last_remind)
                except Exception as e:
                    logging.error(f"Ошибка отправки напоминания о заявке №{t_id}: {e}")

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
                    except TelegramForbiddenError:
                        logging.info(f"Тикет №{t_id}: пользователь заблокировал бота, напоминание отмечено выполненным.")
                        def _mark_blocked(t_id=t_id):
                            with get_db() as conn:
                                with conn.cursor() as cur:
                                    cur.execute("UPDATE tickets SET reminded_idle = TRUE, last_idle_remind = CURRENT_TIMESTAMP WHERE ticket_id = %s", (t_id,))
                        try:
                            await asyncio.to_thread(_mark_blocked)
                        except Exception:
                            pass
                    except Exception as e:
                        logging.error(f"Ошибка отправки напоминания пользователю по тикету №{t_id}: {e}")
                else:
                    if quiet_hours:
                        continue
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
                        f"Тикет {ticket_num_html(t_id, _links_un.get(t_id))} находится в работе у {admin_mention}.\n"
                        f"Пользователь ожидает вашего ответа (простой: <b>{int(mins)} минут</b>).\n"
                        f"Пожалуйста, ответьте игроку или закройте заявку!"
                    )
                    try:
                        sent = await bot.send_message(
                            ADMIN_CHAT_ID, 
                            admin_alert, 
                            message_thread_id=TOPIC_SUPPORT_ID,
                            parse_mode="HTML"
                        )
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
                    except Exception as e:
                        logging.error(f"Ошибка отправки напоминания хелперу по тикету №{t_id}: {e}")

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
                        message_thread_id=TOPIC_SUPPORT_ID,
                        parse_mode="HTML"
                    )
                except Exception as ex:
                    logging.error(f"Ошибка автозакрытия тикета №{t_id}: {ex}")

        except Exception as err:
            logging.error(f"Ошибка в цикле reminder_worker: {err}")

        await asyncio.sleep(60)

# ----------------------------------------------------------------------
# ВЕБ-СЕРВЕР И ЗАПУСК
# ----------------------------------------------------------------------
@router.errors()
async def global_error_handler(event):
    exc = event.exception
    upd = event.update
    text = str(exc).lower()
    logging.error(f"Необработанная ошибка: {exc!r}", exc_info=exc)
    if isinstance(exc, TelegramBadRequest) and ("not modified" in text or "query is too old" in text or "query id is invalid" in text):
        return True
    try:
        if upd.message and upd.message.chat.type == "private":
            await upd.message.answer("⚠️ Что-то пошло не так. Попробуйте ещё раз или нажмите «Перезагрузить меню».")
        elif upd.callback_query:
            await upd.callback_query.answer("⚠️ Что-то пошло не так. Попробуйте ещё раз.", show_alert=True)
    except Exception:
        pass
    global _LAST_ERR_ALERT
    now = time.time()
    if OWNER_ID and now - _LAST_ERR_ALERT > 60:
        _LAST_ERR_ALERT = now
        try:
            await bot.send_message(OWNER_ID, f"⚠️ Ошибка в боте:\n<code>{html.escape(repr(exc)[:600])}</code>", parse_mode="HTML")
        except Exception:
            pass
    return True

_LAST_ERR_ALERT = 0.0

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
    global MAINTENANCE_ON, TEST_MODE_ON, TEST_CHANNEL_ID, TEST_STARTED_AT
    MAINTENANCE_ON = (await get_setting("maintenance_mode", "false")) == "true"
    TEST_CHANNEL_ID = (await get_setting("test_channel_id", "")) or ""
    TEST_MODE_ON = (await get_setting("test_mode", "false")) == "true" and bool(TEST_CHANNEL_ID)
    if TEST_MODE_ON:
        logging.warning("Бот запущен в ТЕСТ-РЕЖИМЕ: публикации идут в тест-канал.")
        try:
            TEST_STARTED_AT = datetime.fromisoformat(await get_setting("test_started_at", ""))
        except Exception:
            TEST_STARTED_AT = datetime.now(timezone.utc)
    if MAINTENANCE_ON:
        logging.warning("Бот запущен в режиме ТЕХНИЧЕСКОГО ПЕРЕРЫВА (включён ранее).")
    await web_server()
    await setup_bot_commands(bot)
    
    if userbot:
        try:
            await userbot.connect()
            if not await userbot.is_user_authorized():
                logging.error("Юзербот не авторизован: сессия недействительна. Создайте новую USER_SESSION_STRING. Публикации через юзербота отключены.")
            else:
                me = await userbot.get_me()
                logging.info(f"Premium-юзербот успешно запущен под аккаунтом: {me.first_name} (@{me.username})")
        except Exception as e:
            logging.error(f"Не удалось запустить юзербота: {e}")
            
    spawn(reminder_worker())
    spawn(secret_timer_worker())
    spawn(mutator_timer_worker())
    spawn(prelink_expiry_worker())
    logging.info("Бот успешно запущен и готов к работе!")
    
    try:
        await dp.start_polling(bot)
    finally:
        if userbot and userbot.is_connected():
            await userbot.disconnect()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Бот остановлен.")
    finally:
        if not db_pool.closed:
            db_pool.closeall()
