import asyncio
import io
import logging
import os
import html
import re
import uuid
import base64
import hashlib
import hmac
import json
import secrets
from types import SimpleNamespace
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
    LabeledPrice, PreCheckoutQuery,
    Message, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton, TelegramObject,
    LinkPreviewOptions, InputMediaPhoto, InputMediaVideo, BotCommand, BotCommandScopeChat, 
    BotCommandScopeAllGroupChats, BotCommandScopeAllPrivateChats, FSInputFile
)

# Модуль юзербота для отправки с Telegram Premium (Telethon)
from telethon import TelegramClient as UserClient
from telethon.sessions import StringSession
from telethon.tl.types import MessageEntityCustomEmoji, User as TlUser
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.errors import FloodWaitError, MessageNotModifiedError, MessageIdInvalidError, MessageAuthorRequiredError

# ----------------------------------------------------------------------
# НАСТРОЙКИ И ОКРУЖЕНИЕ
# ----------------------------------------------------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_CHAT_ID = -1003995930989  # ID группы поддержки
TOPIC_SUPPORT_ID = 2            # ID темы "Поддержка"
TOPIC_SECRETS_ID = 4            # ID темы "Секретки"
TOPIC_APPLICATIONS_ID = 27565   # ID темы "Анкеты" (заявки хелперов и искателей)
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

_WORKER_ALERT: Dict[str, float] = {}

async def _supervised(fn):
    """Фоновый воркер, который сам перезапускается после падения. О падении сообщаем главным админам (раз в час на воркер)."""
    name = getattr(fn, "__name__", "worker")
    delay = 5
    while True:
        t0 = time.time()
        try:
            await fn()
            logging.warning(f"Воркер {name} завершился сам.")
            return
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logging.error(f"Воркер {name} упал: {e!r}", exc_info=True)
            now = time.time()
            if now - _WORKER_ALERT.get(name, 0) > 3600:
                _WORKER_ALERT[name] = now
                try:
                    await _notify_ops(
                        f"⚠️ <b>Фоновая задача «{html.escape(name)}» упала</b> и перезапускается.\n"
                        f"<code>{html.escape(repr(e)[:300])}</code>"
                    )
                    await alog(None, "incident", "", f"упал воркер {name}")
                except Exception:
                    pass
        delay = 5 if time.time() - t0 > 300 else min(delay * 2, 300)
        await asyncio.sleep(delay)

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
DEFAULT_FARM_LIVE_ITEM_FX = (
    "🔹 <b>Сервер #[Номер_Сервера]</b>\n"
    "• Искатель: [Искатель]\n"
    "• Ник в Roblox: [Ник_Роблокс]\n"
    "• Секций: [Секции]\n"
    "• В друзья: [Друзья]\n"
    "• Эффекты: разрешены\n"
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
RESEARCHER_PHOTO = "isled.jpg"   # фото для бейджа исследователя (если файла нет, пост уходит без фото)
DEFAULT_RESEARCHER_ACTIVE = (
    "🔍 <b>Бейдж исследователь</b>\n\n"
    "🔗 [Ссылка]"
)
DEFAULT_RESEARCHER_EXPIRED = (
    "🔍 <b>Бейдж исследователь</b>\n\n"
    "Пост закрыт!"
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
    "💎": "5807465992363710697",
    "⏸": "5771511103141975115", "💰": "5992430854909989581", "🏷": "5854776233950188167",
    "⏯": "5952055319059239589", "💳": "5927169041595634481", "🧾": "5875206779196935950",
    "🛑": "5843482192243594901", "🧩": "5845947563601041174", "✨": "5397981293512243749",
    "ℹ": "5843482192243594901", "↩": "5992199545151295755",
    "🎟": "5875180111744995604",
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

def get_night_notice_application() -> str:
    """Ночная приписка для анкет: чётко говорит, что решения ещё нет."""
    if is_night_time():
        return (
            f"\n\n{ICON_TIME} <i>Сейчас ночное время (с 22:00 до 10:00 МСК). "
            "Анкета получена, но решение ещё <b>не принято</b>: модераторы рассмотрят её утром.</i>"
        )
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

_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")

def _user_token_kind(tok: str, require_at: bool = False):
    """('id', int) | ('name', 'lower') | None. Принимает 123456789, @name, t.me/name, https://t.me/name."""
    t = tok.strip().strip(",;")
    if not t:
        return None
    m = re.fullmatch(r"(?:https?://)?(?:t\.me|telegram\.me)/([A-Za-z0-9_]{5,32})/?", t, re.I)
    if m:
        return ("name", m.group(1).lower())
    if t.startswith("@"):
        t = t[1:]
        return ("name", t.lower()) if _USERNAME_RE.match(t) else None
    if re.fullmatch(r"\d{5,15}", t):
        return ("id", int(t))
    if not require_at and _USERNAME_RE.match(t):
        return ("name", t.lower())
    return None

async def resolve_username(name: str) -> int | None:
    """@юзернейм -> Telegram ID: сначала база (кто пользовался ботом), затем юзербот, затем Bot API."""
    name = name.lower().lstrip("@")
    try:
        row = await _db_one("SELECT user_id FROM users WHERE LOWER(username) = %s LIMIT 1", (name,))
        if row:
            return int(row[0])
    except Exception as e:
        logging.warning(f"Поиск юзернейма в базе не удался: {e}")
    if userbot:
        try:
            if not userbot.is_connected():
                await userbot.connect()
            ent = await asyncio.wait_for(userbot.get_entity(name), timeout=10)
            if isinstance(ent, TlUser) and not ent.bot:
                try:
                    await _db_exec("UPDATE users SET username = %s WHERE user_id = %s", (name, ent.id))
                except Exception:
                    pass
                return int(ent.id)
        except Exception as e:
            logging.info(f"Юзербот не нашёл @{name}: {type(e).__name__}")
    try:
        ci = await bot.get_chat("@" + name)
        if ci.type == "private":
            return int(ci.id)
    except Exception:
        pass
    return None

async def resolve_user_refs(text: str, require_at: bool = False):
    """Разбирает текст со списком людей (ID, @ники, ссылки t.me через пробел, запятую или с новой строки).
    Возвращает (список ID без повторов, список не найденных значений)."""
    ids, failed = [], []
    for tok in re.split(r"[\s,;]+", text or ""):
        if not tok:
            continue
        kind = _user_token_kind(tok, require_at)
        if kind is None:
            failed.append(tok)
            continue
        uid = kind[1] if kind[0] == "id" else await resolve_username(kind[1])
        if uid is None:
            failed.append(tok)
        elif uid not in ids:
            ids.append(uid)
    return ids, failed

async def resolve_user_one(text: str) -> int:
    """Один человек по ID или @юзернейму. Если не найден, бросает ValueError (ловится существующими except ValueError)."""
    ids, failed = await resolve_user_refs((text or "").strip())
    if len(ids) != 1 or failed:
        raise ValueError("user not found")
    return ids[0]

USER_NOT_FOUND_TEXT = (
    "Не нашёл такого пользователя. Отправьте <b>числовой ID</b> или <b>@юзернейм</b>. "
    "По @нику я нахожу тех, кто пользовался ботом, и людей с публичным юзернеймом."
)

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
        if isinstance(event, Message) and event.successful_payment:
            return await handler(event, data)
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
        if isinstance(event, Message) and event.successful_payment:
            return await handler(event, data)   # оплата уже прошла: обязательно обработать
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

_UNAME_CACHE: Dict[int, str] = {}

class UsernameTrackerMiddleware(BaseMiddleware):
    """Запоминает @юзернеймы тех, кто пользуется ботом, чтобы админ мог вводить людей по @нику."""
    async def __call__(self, handler, event, data):
        u = data.get("event_from_user")
        if u and not u.is_bot:
            uname = (u.username or "").lower()
            if _UNAME_CACHE.get(u.id) != uname:
                spawn(_save_username(u.id, uname))
        return await handler(event, data)

async def _save_username(uid: int, uname: str):
    try:
        row = await _db_exec("UPDATE users SET username = %s WHERE user_id = %s RETURNING user_id", (uname or None, uid))
        if row:
            if len(_UNAME_CACHE) > 50000:
                _UNAME_CACHE.clear()
            _UNAME_CACHE[uid] = uname
    except Exception as e:
        logging.warning(f"Не удалось запомнить юзернейм {uid}: {e}")

dp.message.middleware(UsernameTrackerMiddleware())
dp.callback_query.middleware(UsernameTrackerMiddleware())
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
                ALTER TABLE users ADD COLUMN IF NOT EXISTS username TEXT;
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
                ALTER TABLE ratings ADD COLUMN IF NOT EXISTS review_at TIMESTAMP WITH TIME ZONE;
                CREATE TABLE IF NOT EXISTS complaint_targets (
                    id SERIAL PRIMARY KEY,
                    ticket_id INTEGER,
                    reporter_id BIGINT,
                    tkey TEXT,
                    nick TEXT,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS complaint_targets_tkey_idx ON complaint_targets (tkey);
                CREATE TABLE IF NOT EXISTS recruit_subs (
                    user_id BIGINT NOT NULL,
                    kind TEXT NOT NULL,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (user_id, kind)
                );
                CREATE TABLE IF NOT EXISTS faq_votes (
                    voter TEXT NOT NULL,
                    q TEXT NOT NULL,
                    v SMALLINT NOT NULL,
                    ts TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (voter, q)
                );
                CREATE TABLE IF NOT EXISTS publisher_nicks (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    nick TEXT NOT NULL,
                    nick_lower TEXT NOT NULL,
                    rbx_id BIGINT,
                    last_used TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (user_id, nick_lower)
                );
                CREATE TABLE IF NOT EXISTS post_log (
                    id BIGSERIAL PRIMARY KEY,
                    kind TEXT NOT NULL,
                    user_id BIGINT NOT NULL,
                    group_msg_id BIGINT NOT NULL,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS post_log_kind_ts_idx ON post_log (kind, created_at);
                CREATE TABLE IF NOT EXISTS channel_stats (
                    day DATE PRIMARY KEY,
                    subs INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ticket_messages (
                    id BIGSERIAL PRIMARY KEY,
                    ticket_id INTEGER NOT NULL,
                    sender TEXT NOT NULL,
                    sender_id BIGINT,
                    text TEXT DEFAULT '',
                    media TEXT DEFAULT '',
                    via TEXT DEFAULT 'bot',
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS ticket_messages_tid_idx ON ticket_messages (ticket_id, id);
                CREATE TABLE IF NOT EXISTS admin_log (
                    id BIGSERIAL PRIMARY KEY,
                    ts TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                    actor_id BIGINT,
                    action TEXT NOT NULL,
                    target TEXT DEFAULT '',
                    details TEXT DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS admin_log_ts_idx ON admin_log (ts DESC);
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
                ALTER TABLE farm_servers ADD COLUMN IF NOT EXISTS effects_allowed BOOLEAN DEFAULT FALSE;
                CREATE TABLE IF NOT EXISTS prelink_users (
                    user_id BIGINT PRIMARY KEY,
                    added_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
                ALTER TABLE prelink_users ADD COLUMN IF NOT EXISTS expires_at TIMESTAMP WITH TIME ZONE;
                ALTER TABLE prelink_users ADD COLUMN IF NOT EXISTS is_test BOOLEAN DEFAULT FALSE;
                ALTER TABLE prelink_users ADD COLUMN IF NOT EXISTS renew_reminded_for TIMESTAMP WITH TIME ZONE;
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
                CREATE TABLE IF NOT EXISTS shop_services (
                    id SERIAL PRIMARY KEY,
                    title TEXT NOT NULL,
                    description TEXT DEFAULT '',
                    kind TEXT DEFAULT 'custom',
                    is_active BOOLEAN DEFAULT TRUE,
                    sort INTEGER DEFAULT 0,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS shop_tariffs (
                    id SERIAL PRIMARY KEY,
                    service_id INTEGER REFERENCES shop_services(id) ON DELETE CASCADE,
                    label TEXT NOT NULL,
                    stars INTEGER NOT NULL,
                    days INTEGER,
                    is_active BOOLEAN DEFAULT TRUE,
                    sort INTEGER DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS shop_payments (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT,
                    service_id INTEGER,
                    tariff_id INTEGER,
                    service_title TEXT,
                    tariff_label TEXT,
                    stars INTEGER,
                    charge_id TEXT UNIQUE,
                    status TEXT DEFAULT 'paid',
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS shop_promos (
                    id SERIAL PRIMARY KEY,
                    code TEXT UNIQUE NOT NULL,
                    percent INTEGER NOT NULL,
                    service_id INTEGER REFERENCES shop_services(id) ON DELETE CASCADE,
                    expires_at TIMESTAMP WITH TIME ZONE,
                    max_uses INTEGER,
                    uses INTEGER DEFAULT 0,
                    is_active BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS shop_promo_users (
                    promo_id INTEGER REFERENCES shop_promos(id) ON DELETE CASCADE,
                    user_id BIGINT,
                    activated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                    used_at TIMESTAMP WITH TIME ZONE,
                    PRIMARY KEY (promo_id, user_id)
                );
                CREATE TABLE IF NOT EXISTS shop_promo_cooldowns (
                    promo_id INTEGER REFERENCES shop_promos(id) ON DELETE CASCADE,
                    user_id BIGINT,
                    until TIMESTAMP WITH TIME ZONE NOT NULL,
                    PRIMARY KEY (promo_id, user_id)
                );
                CREATE TABLE IF NOT EXISTS applications_pending (
                    user_id BIGINT,
                    kind TEXT,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (user_id, kind)
                );
                CREATE TABLE IF NOT EXISTS secret_apply_attempts (
                    user_id BIGINT,
                    epoch INTEGER,
                    cnt INTEGER DEFAULT 0,
                    PRIMARY KEY (user_id, epoch)
                );
                CREATE TABLE IF NOT EXISTS researcher_badges (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT,
                    channel_id TEXT,
                    message_id BIGINT,
                    link TEXT,
                    is_closed BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
            """)

            cursor.execute("""
                ALTER TABLE shop_services ADD COLUMN IF NOT EXISTS addon_enabled BOOLEAN DEFAULT FALSE;
                ALTER TABLE shop_services ADD COLUMN IF NOT EXISTS addon_title TEXT DEFAULT '';
                ALTER TABLE shop_services ADD COLUMN IF NOT EXISTS addon_question TEXT DEFAULT '';
                ALTER TABLE shop_services ADD COLUMN IF NOT EXISTS addon_stars INTEGER DEFAULT 0;
                ALTER TABLE shop_payments ADD COLUMN IF NOT EXISTS addon_title TEXT;
                ALTER TABLE shop_payments ADD COLUMN IF NOT EXISTS addon_stars INTEGER DEFAULT 0;
                ALTER TABLE shop_payments ADD COLUMN IF NOT EXISTS promo_code TEXT;
                ALTER TABLE shop_payments ADD COLUMN IF NOT EXISTS discount_pct INTEGER DEFAULT 0;
                ALTER TABLE shop_payments ADD COLUMN IF NOT EXISTS kind TEXT;
                ALTER TABLE shop_payments ADD COLUMN IF NOT EXISTS grant_days INTEGER;
                ALTER TABLE shop_promos ADD COLUMN IF NOT EXISTS for_user BIGINT;
                ALTER TABLE shop_promos ADD COLUMN IF NOT EXISTS tariff_id INTEGER REFERENCES shop_tariffs(id) ON DELETE CASCADE;
                ALTER TABLE shop_promo_users ADD COLUMN IF NOT EXISTS reminders INTEGER DEFAULT 0;
                ALTER TABLE secret_posts ADD COLUMN IF NOT EXISTS photo_id TEXT;
                ALTER TABLE secret_posts ADD COLUMN IF NOT EXISTS publish_due TIMESTAMP WITH TIME ZONE;
                ALTER TABLE saboteur_posts ADD COLUMN IF NOT EXISTS publish_due TIMESTAMP WITH TIME ZONE;
                ALTER TABLE farm_posts ADD COLUMN IF NOT EXISTS publish_due TIMESTAMP WITH TIME ZONE;
                CREATE TABLE IF NOT EXISTS referrals (
                    invitee_id BIGINT PRIMARY KEY,
                    referrer_id BIGINT NOT NULL,
                    status TEXT DEFAULT 'pending',
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                    subscribed_at TIMESTAMP WITH TIME ZONE,
                    confirmed_at TIMESTAMP WITH TIME ZONE,
                    checked_at TIMESTAMP WITH TIME ZONE
                );
                CREATE INDEX IF NOT EXISTS referrals_referrer_idx ON referrals (referrer_id, status);
                CREATE TABLE IF NOT EXISTS ref_users (
                    user_id BIGINT PRIMARY KEY,
                    rewards INTEGER DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS delayed_publishes (
                    token TEXT PRIMARY KEY,
                    kind TEXT,
                    user_id BIGINT,
                    user_name TEXT,
                    user_username TEXT,
                    data TEXT,
                    run_at TIMESTAMP WITH TIME ZONE,
                    chat_id BIGINT,
                    msg_id BIGINT,
                    status TEXT DEFAULT 'pending'
                );
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
                ALTER TABLE tickets ADD COLUMN IF NOT EXISTS nopost BOOLEAN DEFAULT FALSE;
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
                ("template_farm_live_item_fx", DEFAULT_FARM_LIVE_ITEM_FX),
                ("template_farm_live_closed", DEFAULT_FARM_LIVE_CLOSED),
                ("template_mutator_active", DEFAULT_MUTATOR_ACTIVE),
                ("template_mutator_expired", DEFAULT_MUTATOR_EXPIRED),
                ("template_researcher_active", DEFAULT_RESEARCHER_ACTIVE),
                ("template_researcher_expired", DEFAULT_RESEARCHER_EXPIRED),
            ):
                cursor.execute(
                    "INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING",
                    (_k, _v)
                )

            cursor.execute("INSERT INTO settings (key, value) VALUES ('shop_channel_url', 'https://t.me/priceToHSecretss') ON CONFLICT (key) DO NOTHING")
            cursor.execute("SELECT id FROM shop_services WHERE kind = 'prelinks' LIMIT 1")
            if not cursor.fetchone():
                cursor.execute(
                    "INSERT INTO shop_services (title, description, kind, is_active, sort) "
                    "VALUES ('Предссылки', 'Получайте ссылки на серверы в личные сообщения раньше, чем они появятся в канале.', 'prelinks', TRUE, 0) RETURNING id"
                )
                _sid = cursor.fetchone()[0]
                for _lbl, _stars, _days, _sort in (("Неделя", 50, 7, 1), ("Месяц", 110, 30, 2)):
                    cursor.execute(
                        "INSERT INTO shop_tariffs (service_id, label, stars, days, is_active, sort) VALUES (%s, %s, %s, %s, TRUE, %s)",
                        (_sid, _lbl, _stars, _days, _sort)
                    )
            else:
                # перенос заглушек первой версии: неделя 50 ⭐, месяц 110 ⭐, «Навсегда» не продаётся
                cursor.execute("""
                    UPDATE shop_tariffs SET stars = 50, is_active = TRUE
                    WHERE label = 'Неделя' AND stars = 100 AND is_active = FALSE
                      AND service_id IN (SELECT id FROM shop_services WHERE kind = 'prelinks')
                """)
                cursor.execute("""
                    UPDATE shop_tariffs SET stars = 110, is_active = TRUE
                    WHERE label = 'Месяц' AND stars = 300 AND is_active = FALSE
                      AND service_id IN (SELECT id FROM shop_services WHERE kind = 'prelinks')
                """)
                cursor.execute("""
                    DELETE FROM shop_tariffs
                    WHERE label = 'Навсегда' AND stars = 1000 AND is_active = FALSE AND days IS NULL
                      AND service_id IN (SELECT id FROM shop_services WHERE kind = 'prelinks')
                """)

    logging.info("База данных успешно инициализирована.")

_init_db_sync()

async def setup_bot_commands(bot: Bot):
    try:
        default_commands = [
            BotCommand(command="start", description="🔄 Главное меню поддержки"),
            BotCommand(command="promocode", description="🎟 Ввести промокод на скидку"),
            BotCommand(command="ref", description="👥 Пригласить друзей и получить предссылки"),
            BotCommand(command="paysupport", description="💬 Помощь по оплатам и возвратам"),
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

# ----------------------------------------------------------------------
# ЖУРНАЛ ДЕЙСТВИЙ КОМАНДЫ (виден главным админам в личном кабинете на сайте)
# ----------------------------------------------------------------------
# действие -> (подпись, группа для фильтра на сайте)
ALOG_INFO = {
    "ticket_take": ("Взял обращение в работу", "tickets"),
    "ticket_close": ("Закрыл обращение", "tickets"),
    "ticket_reject": ("Отклонил обращение", "tickets"),
    "ban": ("Заблокировал игрока", "people"),
    "unban": ("Разблокировал игрока", "people"),
    "helper_accept": ("Принял заявку в хелперы", "people"),
    "helper_reject": ("Отклонил заявку в хелперы", "people"),
    "secret_accept": ("Принял заявку в искатели", "people"),
    "secret_reject": ("Отклонил заявку в искатели", "people"),
    "pub_add": ("Выдал доступ публикатора", "people"),
    "pub_del": ("Забрал доступ публикатора", "people"),
    "admin_add": ("Назначил главного админа", "people"),
    "admin_del": ("Снял главного админа", "people"),
    "recruit_helper": ("Набор в хелперы", "settings"),
    "recruit_secret": ("Набор в искатели секреток", "settings"),
    "friend_toggle": ("Добавление в друзья", "settings"),
    "maintenance": ("Технический перерыв", "settings"),
    "testmode": ("Тест-режим", "settings"),
    "publish_pause": ("Публикации в канал", "settings"),
    "ticket_reply_web": ("Ответил игроку с сайта", "tickets"),
    "toh_update": ("Tower of Hell обновился", "system"),
    "welcome_edit": ("Изменил приветствие бота", "settings"),
    "toh_watch": ("Слежение за обновлением игры", "settings"),
    "event": ("Праздничный режим", "settings"),
    "incident": ("Сбой бота", "system"),
    "restart": ("Запуск бота", "system"),
}
ALOG_KEEP_DAYS = 180

async def alog(actor_id, action: str, target="", details=""):
    """Записывает действие в журнал. Никогда не бросает исключений: сбой журнала не должен ломать само действие."""
    try:
        await _db_exec(
            "INSERT INTO admin_log (actor_id, action, target, details) VALUES (%s, %s, %s, %s)",
            (int(actor_id) if actor_id else None, str(action)[:40], str(target)[:60], str(details)[:300])
        )
    except Exception as e:
        logging.warning(f"Журнал действий: не удалось записать {action}: {e}")

# ----------------------------------------------------------------------
# УВЕДОМЛЕНИЕ ОБ ОТКРЫТИИ НАБОРА (подписка с сайта или из бота)
# ----------------------------------------------------------------------
# вид набора -> (ключ настройки, название, ссылка на анкету)
RECRUIT_KINDS = {
    "helper": ("helper_recruitment", "Набор в команду поддержки (хелперы)", "go_helper"),
    "secret": ("secret_recruitment", "Набор в искатели секреток", "go_seeker"),
}
RECRUIT_NOTIFY_KEYS = {"notify_helpers": "helper", "notify_seekers": "secret"}

async def recruit_sub_has(uid: int, kind: str) -> bool:
    try:
        return bool(await _db_one("SELECT 1 FROM recruit_subs WHERE user_id = %s AND kind = %s", (uid, kind)))
    except Exception as e:
        logging.warning(f"Подписка на набор: не удалось прочитать {uid}: {e}")
        return False

async def recruit_sub_set(uid: int, kind: str, on: bool):
    if kind not in RECRUIT_KINDS:
        return
    if on:
        await _db_exec("INSERT INTO recruit_subs (user_id, kind) VALUES (%s, %s) ON CONFLICT (user_id, kind) DO NOTHING", (uid, kind))
    else:
        await _db_exec("DELETE FROM recruit_subs WHERE user_id = %s AND kind = %s", (uid, kind))

async def recruit_sub_count(kind: str) -> int:
    try:
        r = await _db_one("SELECT COUNT(*) FROM recruit_subs WHERE kind = %s", (kind,))
        return int(r[0]) if r else 0
    except Exception:
        return 0

def _recruit_closed_text(kind: str, subscribed: bool) -> str:
    base = f"{ICON_WARN} {RECRUIT_KINDS[kind][1]} в данный момент закрыт."
    if subscribed:
        base += "\n\n🔔 Я напишу вам, как только набор откроется (один раз)."
    return base

async def _recruit_closed_kb(uid: int, kind: str):
    has = await recruit_sub_has(uid, kind)
    return InlineKeyboardMarkup(inline_keyboard=[[
        IB(text="🔕 Не уведомлять" if has else "🔔 Сообщить, когда откроется",
           callback_data=f"rsub_{'off' if has else 'on'}_{kind}")
    ]])

# ----------------------------------------------------------------------
# ОБЩАЯ ОСТАНОВКА ПУБЛИКАЦИЙ В КАНАЛ (кнопка в панели секреток)
# Действует на всё, что бот и юзербот отправляют в канал новыми сообщениями:
# секретки, саботёр, фарм, мутатор, исследователь. Поддержка и сам бот работают как обычно.
# Уже опубликованные посты можно закрывать и редактировать (это правки, они канал не «поднимают»).
# ----------------------------------------------------------------------
PUBLISH_PAUSED = False            # загружается из БД при старте
PUBLISH_PAUSED_SINCE = 0.0
_PAUSE_REMIND = {"t": 0.0}
PAUSED_ALERT = "⏸ Публикации в канал сейчас приостановлены администрацией. Попробуйте позже."
PAUSED_HTML = ("⏸ <b>Публикации в канал сейчас приостановлены администрацией.</b>\n"
               "Пост не вышел. Попробуйте отправить его позже, когда публикации возобновят.")

def _pub_paused() -> bool:
    # в тест-режиме посты идут в тест-канал, и пауза не должна мешать проверке
    return bool(PUBLISH_PAUSED) and not TEST_MODE_ON

async def _pub_paused_alert(call) -> bool:
    """True, если публикации остановлены (и человеку показано окно). Черновик при этом не сбрасывается."""
    if not _pub_paused():
        return False
    try:
        await call.answer(PAUSED_ALERT, show_alert=True)
    except Exception:
        pass
    return True

async def _pause_notify_publisher(uid, what: str):
    if not uid:
        return
    try:
        await bot.send_message(
            uid,
            f"⏸ Публикации в канал были остановлены администрацией, поэтому {what} не вышел в канал. "
            "Ссылку уже могли получить стафф и предссылки. Отправьте пост заново, когда публикации возобновят.",
        )
    except Exception:
        pass

async def _pause_skip_secret(post_id: int):
    logging.warning(f"[Секретка #{post_id}] публикации остановлены админом: пост в канал не отправлен.")
    try:
        r = await _db_one("SELECT user_id FROM secret_posts WHERE id = %s", (post_id,))
    except Exception:
        r = None
    await _pause_notify_publisher(r[0] if r else 0, "ваш пост")

async def _pause_skip_new_post(kind: str, post_id: int, deactivate: bool):
    """Саботёр или фарм: нового сообщения в канале не будет. Сервера без поста в канале снимаем, чтобы они не висели
    «активными» и не мешали отправить новый после возобновления."""
    table = "saboteur_servers" if kind == "sab" else "farm_servers"
    logging.warning(f"[{'Саботёр' if kind == 'sab' else 'Фарм'} #{post_id}] публикации остановлены админом: новый пост не отправлен.")
    try:
        users = [r[0] for r in await _db_all(f"SELECT DISTINCT user_id FROM {table} WHERE post_id = %s AND is_active = TRUE", (post_id,))]
        if deactivate:
            await _db_exec(f"UPDATE {table} SET is_active = FALSE WHERE post_id = %s AND is_active = TRUE", (post_id,))
    except Exception as e:
        logging.error(f"Остановка публикаций: не удалось обработать {table} #{post_id}: {e}")
        users = []
    for u in users:
        await _pause_notify_publisher(u, "ваш сервер")

async def _notify_pause_change(user, paused: bool):
    """Остальным главным админам: кто и что переключил (чтобы пауза не осталась незамеченной)."""
    name = html.escape(getattr(user, "full_name", "") or str(user.id))
    text = f"{'⏸' if paused else '▶️'} {name} {'остановил' if paused else 'возобновил'} публикации в канал."
    for aid in await get_main_admin_ids():
        if aid == user.id:
            continue
        try:
            await bot.send_message(aid, text, parse_mode="HTML")
        except Exception:
            pass

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

# ---------- повторные жалобы на одного игрока ----------
def _ru_plural(n: int, forms: tuple) -> str:
    n = abs(int(n))
    if 11 <= n % 100 <= 14:
        return forms[2]
    if n % 10 == 1:
        return forms[0]
    if 2 <= n % 10 <= 4:
        return forms[1]
    return forms[2]

def _ago_ru(dt) -> str:
    try:
        sec = max(0, int((datetime.now(timezone.utc) - dt).total_seconds()))
    except Exception:
        return ""
    if sec < 3600:
        return f"{max(1, sec // 60)} мин. назад"
    if sec < 86400:
        return f"{sec // 3600} ч. назад"
    return f"{sec // 86400} дн. назад"

def _complaint_targets(text: str) -> list:
    """Нарушители из текста жалобы: [(ключ, ник)]. Ключ: id:<Roblox ID> или n:<ник строчными>."""
    m = re.search(r"Нарушитель\(и\):\s*([^\n]+)", text or "")
    if not m:
        return []
    line = m.group(1)
    out, seen = [], set()
    link_re = r"<a href='[^']*'>([^<]+)</a> \(ID: <code>(\d+)</code>\)"
    for name, rid in re.findall(link_re, line):
        k = f"id:{rid}"
        if k not in seen:
            seen.add(k)
            out.append((k, html.unescape(name).strip()))
    rest = re.sub(link_re, "", line)
    for name in re.findall(r"<code>([^<]+)</code>", rest):
        name = html.unescape(name).strip()
        k = "n:" + name.lower()
        if name and k not in seen:
            seen.add(k)
            out.append((k, name))
    return out[:5]

async def _complaint_history_line(targets: list) -> str:
    """Строка для карточки: на этого игрока уже жаловались (за последние 180 дней). Пусто, если жалоб не было."""
    keys = [k for k, _ in targets]
    if not keys:
        return ""
    rows = await _db_all(
        "SELECT tkey, ticket_id, reporter_id, created_at FROM complaint_targets "
        "WHERE tkey = ANY(%s) AND created_at > CURRENT_TIMESTAMP - INTERVAL '180 days' ORDER BY created_at DESC",
        (keys,)
    )
    by_key: Dict[str, dict] = {}
    for tkey, tid, rep_id, created in rows:
        d = by_key.setdefault(tkey, {"tickets": [], "reporters": set(), "last": created})
        if tid not in d["tickets"]:
            d["tickets"].append(tid)
        d["reporters"].add(rep_id)
    lines = []
    link_ids = []
    for k, _ in targets:
        if k in by_key:
            link_ids.extend(by_key[k]["tickets"][:3])
    links = await get_ticket_links(link_ids) if link_ids else {}
    for k, nick in targets:
        d = by_key.get(k)
        if not d:
            continue
        n, m_ = len(d["tickets"]), len(d["reporters"])
        refs = ", ".join(ticket_num_html(t, links.get(t, "")) for t in d["tickets"][:3])
        lines.append(
            f"⚠️ <b>{html.escape(nick)}</b>: уже {n} {_ru_plural(n, ('жалоба', 'жалобы', 'жалоб'))} "
            f"от {m_} {_ru_plural(m_, ('игрока', 'игроков', 'игроков'))}, последняя {_ago_ru(d['last'])} ({refs})"
        )
    if not lines:
        return ""
    return "🔁 <b>Повторные жалобы:</b>\n" + "\n".join(lines) + "\n\n"

async def _record_complaint_targets(ticket_id: int, reporter_id: int, targets: list):
    for k, nick in targets:
        await _db_exec(
            "INSERT INTO complaint_targets (ticket_id, reporter_id, tkey, nick) VALUES (%s, %s, %s, %s)",
            (ticket_id, reporter_id, k, nick[:60])
        )

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

_CAPTION_TYPES = {"photo", "video", "document", "audio", "animation", "voice"}

def _ctype(m: Message) -> str:
    """Тип сообщения обычной строкой ('photo', 'video'...): у Enum хэш не совпадает со строкой, поэтому приводим явно."""
    ct = m.content_type
    return str(getattr(ct, "value", ct))

async def relay_message(src: Message, chat_id: int, header_html: str, body: str = "", thread_id: int = None, reply_markup=None) -> list:
    """Пересылает сообщение ЛЮБОГО типа (текст, фото, видео, файл, голосовое, стикер, кружок...) с шапкой.
    Возвращает id отправленных сообщений. Текст и подпись не теряются: если подпись не помещается (1024), текст идёт отдельным сообщением."""
    kw = {"message_thread_id": thread_id} if thread_id else {}
    body = (body or "").strip()
    ids = []
    ctype = _ctype(src)
    if ctype == "text":
        full = header_html + (f"\n\n{html.escape(body)}" if body else "")
        m = await bot.send_message(chat_id, html_cut(full, 4000), parse_mode="HTML", reply_markup=reply_markup, **kw)
        return [m.message_id]
    if ctype in _CAPTION_TYPES:
        with_body = header_html + (f"\n\n{html.escape(body)}" if body else "")
        if html_visible_len(with_body) <= 1000:
            caption, extra = with_body, ""
        else:
            caption, extra = header_html, body
        m = await bot.copy_message(chat_id, from_chat_id=src.chat.id, message_id=src.message_id,
                                   caption=caption, parse_mode="HTML", reply_markup=None if extra else reply_markup, **kw)
        ids.append(m.message_id)
        if extra:
            m2 = await bot.send_message(chat_id, html_cut(html.escape(extra), 4000), parse_mode="HTML", reply_markup=reply_markup, **kw)
            ids.append(m2.message_id)
        return ids
    # стикер, кружок, геолокация, контакт и т. п.: шапка отдельно, само сообщение копией
    h = await bot.send_message(chat_id, header_html, parse_mode="HTML", **kw)
    ids.append(h.message_id)
    m = await bot.copy_message(chat_id, from_chat_id=src.chat.id, message_id=src.message_id, reply_markup=reply_markup, **kw)
    ids.append(m.message_id)
    return ids

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

async def tlog(ticket_id: int, sender: str, sender_id, text: str = "", media: str = "", via: str = "bot"):
    """Пишет сообщение обращения в базу (для архива на сайте). Никогда не бросает исключений."""
    try:
        await _db_exec(
            "INSERT INTO ticket_messages (ticket_id, sender, sender_id, text, media, via) VALUES (%s,%s,%s,%s,%s,%s)",
            (int(ticket_id), sender, int(sender_id) if sender_id else None, (text or "")[:4000], media[:20], via[:10]))
    except Exception as e:
        logging.warning(f"Архив обращений: не удалось записать сообщение по №{ticket_id}: {e}")

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
    await tlog(ticket_id, "system", admin_id, "closed" if status == "closed" else status)

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
            target_peer = channel_id
            if isinstance(target_peer, str) and (target_peer.startswith("-100") or target_peer.lstrip("-").isdigit()):
                target_peer = int(target_peer)

            parsed_text, entities = await parse_telethon_custom_emojis(userbot, final_text)
            await _ub_edit_text(f"[Секретка #{post_id}, истекла]", target_peer, message_id, parsed_text, entities)
        else:
            await bot.edit_message_caption(
                chat_id=channel_id,
                message_id=message_id,
                caption=final_text,
                parse_mode="HTML"
            )
    except MessageNotModifiedError:
        pass
    except Exception as e:
        logging.error(f"Не удалось обновить истекший пост №{post_id}: {e}")
        await _notify_owner(
            f"⚠️ <b>Не удалось закрыть секретку №{post_id} в канале</b>: ссылка в посте могла остаться доступной.\n"
            f"<code>{html.escape(f'{type(e).__name__}: {e}'[:200])}</code>"
        )

_DUE_TABLES = ("secret_posts", "saboteur_posts", "farm_posts")
PUBLISH_MAX_LATE = 300      # если бот был недоступен дольше, пост уже не нужен

def _set_due_sync(table: str, post_id: int, secs: float):
    assert table in _DUE_TABLES
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE {table} SET publish_due = CURRENT_TIMESTAMP + (%s * INTERVAL '1 second') WHERE id = %s", (float(secs), post_id))

def _clear_due_sync(table: str, post_id: int):
    assert table in _DUE_TABLES
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE {table} SET publish_due = NULL WHERE id = %s", (post_id,))

async def _set_due(table: str, post_id: int, secs: float):
    try:
        await asyncio.to_thread(_set_due_sync, table, post_id, secs)
    except Exception as e:
        logging.warning(f"Не удалось записать срок публикации ({table} #{post_id}): {e}")

async def _clear_due(table: str, post_id: int):
    try:
        await asyncio.to_thread(_clear_due_sync, table, post_id)
    except Exception as e:
        logging.warning(f"Не удалось снять срок публикации ({table} #{post_id}): {e}")

async def _with_due(table: str, post_id: int, delay: float, coro_fn):
    """Запоминает в БД «этот пост должен выйти», выполняет публикацию и снимает пометку.
    Если процесс остановили посреди ожидания, пометка остаётся, и при старте пост доопубликуется."""
    await _set_due(table, post_id, delay)
    cancelled = False
    try:
        await coro_fn()
    except asyncio.CancelledError:
        cancelled = True
        raise
    finally:
        if not cancelled:
            await _clear_due(table, post_id)

async def publish_to_channel_delayed(channel_id: str, photo_id: str, caption: str, post_id: int, delay: int = CHANNEL_DELAY_SECONDS):
    await _with_due("secret_posts", post_id, delay,
                    lambda: _publish_to_channel_delayed_inner(channel_id, photo_id, caption, post_id, delay))

async def _publish_to_channel_delayed_inner(channel_id: str, photo_id: str, caption: str, post_id: int, delay: int = CHANNEL_DELAY_SECONDS):
    logging.info(f"[Секретка #{post_id}] Ожидание форы {delay} сек перед отправкой в {channel_id}...")
    await asyncio.sleep(delay)
    if _pub_paused():
        await _pause_skip_secret(post_id)
        return
    channel_msg_id = 0
    try:
        if userbot:
            logging.info(f"[Секретка #{post_id}] Скачивание фото {photo_id}...")
            file_info = await bot.get_file(photo_id)
            photo_buf = io.BytesIO()
            await bot.download_file(file_info.file_path, destination=photo_buf)
            photo_bytes = photo_buf.getvalue()

            logging.info(f"[Секретка #{post_id}] Парсинг разметки и кастомных эмодзи...")
            parsed_text, entities = await parse_telethon_custom_emojis(userbot, caption)

            target_peer = channel_id
            if isinstance(target_peer, str) and (target_peer.startswith("-100") or target_peer.lstrip("-").isdigit()):
                target_peer = int(target_peer)

            logging.info(f"[Секретка #{post_id}] Отправка через Telethon в {target_peer}...")
            t_send = time.monotonic()
            sent_msg = await _ub_send_photo(f"[Секретка #{post_id}]", target_peer, photo_bytes, parsed_text, entities)
            logging.info(f"[Секретка #{post_id}] запрос юзербота занял {time.monotonic() - t_send:.1f} с")
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
                await _notify_owner(
                    f"⚠️ <b>Секретка №{post_id} не попала в канал.</b>\n"
                    f"Юзербот: <code>{html.escape(f'{type(e).__name__}: {e}'[:200])}</code>\n"
                    f"Запасной путь (бот): <code>{html.escape(f'{type(e2).__name__}: {e2}'[:200])}</code>"
                )
                try:
                    pub = await _db_one("SELECT user_id FROM secret_posts WHERE id = %s", (post_id,))
                    if pub and pub[0]:
                        await bot.send_message(
                            pub[0],
                            "⚠️ Ваша секретка дошла до стаффа и предссылок, но в канал не попала из-за технической ошибки. "
                            "Администрация уже уведомлена.",
                        )
                except Exception:
                    pass

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

# ----------------------------------------------------------------------
# УСТОЙЧИВЫЕ ЗАПРОСЫ ЮЗЕРБОТА: таймаут, переподключение, повтор, защита от дублей
# ----------------------------------------------------------------------
UB_REQUEST_TIMEOUT = 90     # дольше 60 с (порог автоожидания FloodWait в Telethon), но не 10 минут

async def _ub_reconnect():
    try:
        await asyncio.wait_for(userbot.disconnect(), 10)
    except Exception:
        pass
    try:
        await asyncio.wait_for(userbot.connect(), 20)
    except Exception as e:
        logging.warning(f"Юзербот: переподключение не удалось: {type(e).__name__}: {e}")

async def _ub_request(label: str, make_coro, attempts: int = 3, on_retry=None):
    """Выполняет запрос юзербота с таймаутом. При обрыве или зависании переподключает клиента и повторяет,
    при FloodWait ждёт и повторяет. Остальные ошибки (MessageNotModified и т.п.) отдаёт вызывающему."""
    last = None
    for i in range(1, attempts + 1):
        if i > 1 and on_retry is not None:
            try:
                found = await on_retry()
            except Exception:
                found = None
            if found is not None:
                logging.info(f"{label}: сообщение уже было отправлено до обрыва, повтор не нужен.")
                return found
        t0 = time.monotonic()
        try:
            if not userbot.is_connected():
                await asyncio.wait_for(userbot.connect(), 20)
            res = await asyncio.wait_for(make_coro(), UB_REQUEST_TIMEOUT)
            dt = time.monotonic() - t0
            if dt > 10:
                logging.warning(f"{label}: запрос к Telegram занял {dt:.0f} с (попытка {i})")
            return res
        except FloodWaitError as fw:
            last = fw
            if fw.seconds > 120:
                raise
            logging.warning(f"{label}: FloodWait {fw.seconds} с, жду и повторяю (попытка {i}/{attempts})")
            await asyncio.sleep(fw.seconds + 1)
        except (asyncio.TimeoutError, OSError) as e:
            last = e
            logging.warning(f"{label}: юзербот завис или потерял связь ({type(e).__name__}) за {time.monotonic() - t0:.0f} с, переподключаю (попытка {i}/{attempts})")
            await _ub_reconnect()
    raise last if last else RuntimeError(f"{label}: запрос юзербота не удался")

async def _ub_send_text(label: str, target_peer, parsed_text: str, entities):
    async def _find():
        msgs = await asyncio.wait_for(userbot.get_messages(target_peer, limit=5), 20)
        now = datetime.now(timezone.utc)
        for m in msgs:
            if getattr(m, "out", False) and (m.message or "") == parsed_text and m.date and (now - m.date).total_seconds() < 300:
                return m
        return None
    return await _ub_request(
        label,
        lambda: userbot.send_message(entity=target_peer, message=parsed_text, formatting_entities=entities, link_preview=False),
        on_retry=_find
    )

async def _ub_send_photo(label: str, target_peer, photo, parsed_text: str, entities):
    """photo: bytes или путь к файлу. При повторе после обрыва сначала проверяет, не ушло ли фото уже."""
    def _mk():
        if isinstance(photo, (bytes, bytearray)):
            b = io.BytesIO(bytes(photo))
            b.name = "secret.jpg"       # без записи на диск хоста
            return b
        return photo
    async def _find():
        msgs = await asyncio.wait_for(userbot.get_messages(target_peer, limit=5), 20)
        now = datetime.now(timezone.utc)
        for m in msgs:
            if getattr(m, "out", False) and getattr(m, "media", None) and (m.message or "") == parsed_text and m.date and (now - m.date).total_seconds() < 300:
                return m
        return None
    return await _ub_request(
        label,
        lambda: userbot.send_file(entity=target_peer, file=_mk(), caption=parsed_text, formatting_entities=entities),
        on_retry=_find
    )

async def _ub_edit_text(label: str, target_peer, message_id: int, parsed_text: str, entities):
    return await _ub_request(
        label,
        lambda: userbot.edit_message(entity=target_peer, message=message_id, text=parsed_text, formatting_entities=entities, link_preview=False)
    )

async def userbot_keepalive_worker():
    """Раз в 45 секунд «будит» соединение юзербота. Зависшее соединение переподключается сразу,
    а не при следующей проверке через 10 минут."""
    logging.info("Keepalive юзербота запущен.")
    await asyncio.sleep(45)
    while True:
        if userbot:
            try:
                if not userbot.is_connected():
                    await asyncio.wait_for(userbot.connect(), 20)
                await asyncio.wait_for(userbot.get_me(), 20)
            except Exception as e:
                logging.warning(f"Keepalive: юзербот не отвечает ({type(e).__name__}), переподключаю.")
                await _ub_reconnect()
        await asyncio.sleep(45)

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

    def _save_mid(mid):
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE saboteur_posts SET message_id = %s WHERE id = %s", (mid, post_id))

    if userbot:
        parsed_text, entities = await parse_telethon_custom_emojis(userbot, final_text)

        async def _send_new():
            if _pub_paused():
                await _pause_skip_new_post("sab", post_id, deactivate=not (current_post and current_post["message_id"]))
                return
            try:
                sent_msg = await _ub_send_text(f"[Саботёр #{post_id}]", target_peer, parsed_text, entities)
            except Exception as e:
                # запасной путь: обычный бот, чтобы пост не пропал (без премиум-эмодзи)
                logging.error(f"[Саботёр #{post_id}] юзербот не смог опубликовать ({type(e).__name__}: {e}), публикую ботом.")
                sent_b = await bot.send_message(
                    chat_id=channel_id, text=final_text, parse_mode="HTML",
                    link_preview_options=LinkPreviewOptions(is_disabled=True)
                )
                await asyncio.to_thread(_save_mid, sent_b.message_id)
                return
            await asyncio.to_thread(_save_mid, sent_msg.id)
            logging.info(f"Новый пост саботера #{post_id} опубликован в канале {channel_id} (msg_id={sent_msg.id}).")

        if current_post and current_post["message_id"] != 0:
            try:
                await _ub_edit_text(f"[Саботёр #{post_id}]", target_peer, current_post["message_id"], parsed_text, entities)
                logging.info(f"Пост саботера #{post_id} успешно обновлен в канале {channel_id}.")
            except MessageNotModifiedError:
                pass
            except MessageIdInvalidError:
                logging.warning(f"[Саботёр #{post_id}] пост в канале удалён вручную.")
                if await get_active_saboteur_servers(post_id):
                    await _send_new()
            except MessageAuthorRequiredError:
                # пост был отправлен ботом (запасной путь): редактируем тоже ботом
                await bot.edit_message_text(
                    chat_id=channel_id, message_id=current_post["message_id"], text=final_text,
                    parse_mode="HTML", link_preview_options=LinkPreviewOptions(is_disabled=True)
                )
        else:
            await _send_new()
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
            if _pub_paused():
                await _pause_skip_new_post("sab", post_id, deactivate=True)
                return
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
    await _with_due("saboteur_posts", post_id, delay, lambda: _publish_saboteur_delayed_inner(channel_id, post_id, delay))

async def _publish_saboteur_delayed_inner(channel_id: str, post_id: int, delay: int = CHANNEL_DELAY_SECONDS):
    logging.info(f"[Саботер #{post_id}] Ожидание форы {delay} сек перед публикацией...")
    t_sleep = time.monotonic()
    await asyncio.sleep(delay)
    overshoot = max(0.0, time.monotonic() - t_sleep - delay)      # бот «проспал» сверх срока (бот был занят)
    t_lock = time.monotonic()
    async with saboteur_lock:
        waited = time.monotonic() - t_lock                        # ждали, пока освободится очередь саботёра
        t_pub = time.monotonic()
        try:
            await publish_or_update_saboteur_channel(channel_id, post_id)
        except Exception as e:
            logging.error(f"Ошибка отложенной публикации саботера #{post_id}: {e}", exc_info=True)
    took = time.monotonic() - t_pub
    late = overshoot + waited + took
    logging.info(f"[Саботер #{post_id}] после форы прошло {late:.1f} с (проспал {overshoot:.1f}, очередь {waited:.1f}, запрос {took:.1f})")
    if late > 90:
        await _notify_owner(
            f"⚠️ <b>Пост саботёра #{post_id} вышел с опозданием {int(late)} с.</b>\n"
            f"Бот был занят: {overshoot:.0f} с, ждал очередь: {waited:.0f} с, запрос в Telegram: {took:.0f} с.\n"
            "Если «запрос» большой, виновата связь юзербота (бот её уже сам переподключает)."
        )

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
    secret_vip_date = State()
    secret_vip_until = State()
    secret_time = State()
    secret_why = State()

    rating_comment = State()

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
    waiting_for_delay = State()

class MutatorPublisherStates(StatesGroup):
    waiting_for_link = State()
    waiting_for_confirm = State()

class ResearcherPublisherStates(StatesGroup):
    waiting_for_link = State()
    waiting_for_confirm = State()
    waiting_for_delay = State()

class FarmPublisherStates(StatesGroup):
    waiting_for_nick = State()
    waiting_for_sections = State()
    waiting_for_friends = State()
    waiting_for_link = State()
    waiting_for_effects = State()
    waiting_for_confirm = State()
    waiting_for_delay = State()

class ShopAdminStates(StatesGroup):
    new_title = State()
    new_desc = State()
    edit_title = State()
    edit_desc = State()
    tar_label = State()
    tar_price = State()
    tar_days = State()
    tar_edit_price = State()
    tar_edit_label = State()
    chan_url = State()
    ad_title = State()
    ad_question = State()
    ad_price = State()
    promo_code = State()
    promo_pct = State()
    promo_wait = State()
    promo_dur_custom = State()
    promo_max_custom = State()
    promo_edit_limit = State()
    promo_extend = State()
    promo_user = State()

class ShopUserStates(StatesGroup):
    waiting_promo = State()
    waiting_donate = State()

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
    template_farm_live_item_fx = State()
    template_farm_live_closed = State()
    template_mutator_active = State()
    template_mutator_expired = State()
    template_researcher_active = State()
    template_researcher_expired = State()
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
BTN_SHOP = "💎 Платные услуги"
BTN_REF = "👥 Пригласить друзей"

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

    keyboard.append([KB(text=BTN_SHOP), KB(text=BTN_REF)] if _REF_CFG["ref"] else [KB(text=BTN_SHOP)])
    keyboard.append([KB(text=BTN_REFRESH)])

    if await is_main_admin(user_id):
        keyboard.append([KB(text=BTN_ADMIN_PANEL)])

    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True, persistent=True)

async def admin_panel_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [IB(text="📊 Статистика поддержки", callback_data="adm_stats"),
         IB(text="👥 Статистика хелперов", callback_data="adm_list_stats")],
        [IB(text="📝 Наборы и друзья", callback_data="adm_recruit")],
        [IB(text="🔮 Секретки", callback_data="adm_secret_panel")],
        [IB(text="📈 Аналитика канала", callback_data="adm_chan_stats")],
        [IB(text="💎 Платные услуги (Stars)", callback_data="shopa_menu")],
        [IB(text="👥 Рефералка", callback_data="refa_menu")],
        [IB(text="⚙️ Система и доступ", callback_data="adm_system")],
    ])

async def recruit_kb():
    f_active = await get_setting("friend_active", "false")
    h_active = await get_setting("helper_recruitment", "true")
    s_active = await get_setting("secret_recruitment", "true")
    def _st(v):
        return ("🟢", "Вкл") if v == "true" else ("🔴", "Выкл")
    (fc, fw), (hc, hw), (sc, sw) = _st(f_active), _st(h_active), _st(s_active)
    # кружок стоит первым: в таком случае он становится тгп-иконкой кнопки
    return InlineKeyboardMarkup(inline_keyboard=[
        [IB(text=f"{fc} Добавление в друзья: {fw}", callback_data="toggle_friend")],
        [IB(text=f"{hc} Набор в хелперы: {hw}", callback_data="toggle_helper")],
        [IB(text=f"{sc} Искатели секреток: {sw}", callback_data="toggle_secret")],
        [IB(text="✏ Ник для друзей", callback_data="change_friend_nick")],
        [IB(text="🔙 Назад", callback_data="back_to_adm")],
    ])

async def system_kb():
    maint_icon, maint_word = ("🔴", "ВКЛЮЧЁН") if MAINTENANCE_ON else ("🟢", "Выкл")
    test_word = "ВКЛЮЧЁН" if TEST_MODE_ON else "Выкл"
    ds_on = (await get_setting("daily_summary", "true")) == "true"
    return InlineKeyboardMarkup(inline_keyboard=[
        [IB(text=f"{maint_icon} Тех. перерыв: {maint_word}", callback_data="toggle_maintenance")],
        [IB(text=f"{'🟢' if ds_on else '🔴'} Ежедневная сводка: {'Вкл' if ds_on else 'Выкл'}", callback_data="toggle_daily_summary"),
         IB(text="📨 Сводка сейчас", callback_data="daily_summary_now")],
        [IB(text=f"🧪 Тест режим: {test_word}", callback_data="toggle_testmode"),
         IB(text="📡 Канал тест-режима", callback_data="set_test_channel")],
        [IB(text="👥 Публикаторы секреток", callback_data="sec_manage_pubs")],
        [IB(text="👋 Приветствие бота", callback_data="adm_welcome")],
        [IB(text="👑 Главные админы поддержки", callback_data="manage_main_admins")],
        [IB(text="🔙 Назад", callback_data="back_to_adm")],
    ])

def secret_admin_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [IB(text="▶️ Возобновить публикацию" if PUBLISH_PAUSED else "⏸ Остановить публикацию", callback_data="toggle_publish_pause")],
        [IB(text="🔔 Предссылки и уведомления", callback_data="adm_sec_people")],
        [IB(text="📝 Шаблоны постов", callback_data="sec_templates")],
        [IB(text="🧩 Типы секреток", callback_data="adm_sec_types")],
        [IB(text="🛑 Закрыть чужие посты", callback_data="sec_close_menu")],
        [IB(text="📊 Статистика постов (24ч)", callback_data="sec_stats"),
         IB(text="⏱ Таймер", callback_data="sec_set_timer")],
        [IB(text="🔙 Назад в админку", callback_data="back_to_adm")]
    ])

async def sec_people_kb():
    calldm = (await get_setting("call_dm", "false")) == "true"
    return InlineKeyboardMarkup(inline_keyboard=[
        [IB(text="🔔 Предссылки", callback_data="sec_prelinks")],
        [IB(text=f"{'🟢' if calldm else '🔴'} Личные пинги стаффу: {'Вкл' if calldm else 'Выкл'}", callback_data="sec_toggle_calldm")],
        [IB(text="📣 Команда для бота-зазывалы", callback_data="sec_call_cmd")],
        [IB(text="🔙 Назад", callback_data="adm_secret_panel")]
    ])

SEC_TYPES_KB = InlineKeyboardMarkup(inline_keyboard=[
    [IB(text="➕ Добавить обычный тип", callback_data="sec_add_type"),
     IB(text="🗑 Удалить обычный тип", callback_data="sec_del_type")],
    [IB(text="➕ Добавить особый тип", callback_data="sec_add_spec_type"),
     IB(text="🗑 Удалить особый тип", callback_data="sec_del_spec_type")],
    [IB(text="🔙 Назад", callback_data="adm_secret_panel")]
])

def publisher_menu_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [IB(text="📥 Отправить секретку", callback_data="pub_send_secret")],
        [IB(text="🕵️ Поиск саботера", callback_data="pub_send_saboteur")],
        [IB(text="🌾 Фарм сервер", callback_data="pub_send_farm")],
        [IB(text="🧬 Бейдж мутатор", callback_data="pub_send_mutator")],
        [IB(text="🔍 Бейдж исследователь", callback_data="pub_send_researcher")],
        [IB(text="📋 Мои секретки", callback_data="pub_my_posts"),
         IB(text="📋 Мои сервера саботера", callback_data="pub_my_saboteurs")],
        [IB(text="📋 Мои сервера фарма", callback_data="pub_my_farms")],
        [IB(text="📋 Мои бейджи мутатора", callback_data="pub_my_mutators"),
         IB(text="📋 Мои бейджи исследователя", callback_data="pub_my_researchers")],
        [IB(text="📢 Привязать канал", callback_data="pub_bind_channel")]
    ])

def take_ticket_kb(ticket_id: int, nopost: bool = False):
    rows = [[
        IB(text="🟢 Начать работу", callback_data=f"take_{ticket_id}"),
        IB(text="🔴 Отклонить тикет", callback_data=f"reject_{ticket_id}")
    ]]
    if nopost:
        rows.append([IB(text="🚫 Нет такого поста", callback_data=f"nopost_{ticket_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def close_ticket_kb(ticket_id: int, admin_id: int, nopost: bool = False):
    rows = [[IB(text="🔒 Закрыть тикет", callback_data=f"close_{ticket_id}_{admin_id}")]]
    if nopost:
        rows.append([IB(text="🚫 Нет такого поста", callback_data=f"nopost_{ticket_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

async def _ticket_nopost(ticket_id: int) -> bool:
    try:
        r = await _db_one("SELECT nopost FROM tickets WHERE ticket_id = %s", (ticket_id,))
        return bool(r and r[0])
    except Exception:
        return False

def solved_kb(ticket_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[[
        IB(text="✅ Вопрос решён", callback_data=f"solved_{ticket_id}")
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

SITE_URL = (os.getenv("SITE_URL") or "https://towerofsecrets.run.place").rstrip("/")

# Правила по типам серверов (то же, что на странице сайта /rules)
RULES_BY_TYPE = {
    "secret": (
        ["Не ускорять",
         "Выйти с сервера после получения эффекта",
         "Не покупать негативные мутаторы",
         "Не подниматься выше уровня над секреткой и не идти к воротам ускорения"],
        "Фарм-секретка: не ускорять; выйти с сервера после получения монеток (можно перезайти, но выйти нужно); не покупать негативные мутаторы."
    ),
    "saboteur": (
        ["Снять все эффекты (сияние, нимбы, черепок, призрак, лапки, сердечки; эффекты без следов можно)",
         "Не стоять в АФК больше 2 раундов",
         "Не подниматься выше ворот",
         "Не трогать рычаги, этим занимаются админы",
         "Вход с карминовым и фиолетовым нимбом запрещён",
         "Покупать при саботёре щит и прочее (плохие мутаторы нельзя)",
         "Не играть в мини-игры, пока не прошли башню",
         "Ускорение башни с саботёром или секреткой запрещено",
         "Всегда ускорять башню из 4 уровней, не ждать других"],
        ""
    ),
    "farm": (
        ["Снять все эффекты (сияние, нимбы, черепок, призрак, лапки, сердечки; эффекты без следов можно)",
         "Не стоять в АФК больше 2 раундов",
         "Ускорять не раньше чем через 3 минуты от начала раунда",
         "Не покупать негативные мутаторы",
         "По возможности покупать положительные мутаторы"],
        ""
    ),
    "researcher": (
        ["Проходить башню",
         "Не стоять в АФК",
         "После получения бейджа сразу выйти",
         "Не покупать негативные мутаторы"],
        ""
    ),
    "mutator": (
        ["Не проходить башню",
         "Не стоять в АФК",
         "После получения бейджа сразу выйти"],
        ""
    ),
}

def _rules_hint(key: str, kind: str) -> str:
    """Свёрнутая цитата с правилами выбранного типа сервера (для жалобы или обжалования)."""
    sec = RULES_BY_TYPE.get(key)
    if not sec:
        return ""
    rules, extra = sec
    body = "\n".join(f"{i}. {html.escape(r)}" for i, r in enumerate(rules, 1))
    if extra:
        body += "\n\n" + html.escape(extra)
    head = ("Проверьте, что нарушение есть в правилах этого типа:" if kind == "complaint"
            else "Правила этого типа, по ним обычно выдают бан:")
    return (f"\n\n📜 <b>{head}</b>\n<blockquote expandable>{body}</blockquote>\n"
            f'<a href="{SITE_URL}/rules#{key}">Полные правила на сайте</a>')

def complaint_servers_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [IB(text="🌐 Сервер с секреткой", callback_data="cmp_srv_secret")],
        [IB(text="🕵 Поиск саботера", callback_data="cmp_srv_saboteur")],
        [IB(text="🌾 Фарм сервер", callback_data="cmp_srv_farm")],
        [IB(text="🔎 Бейдж исследователь", callback_data="cmp_srv_researcher")],
        [IB(text="🧬 Бейдж мутатор", callback_data="cmp_srv_mutator")]
    ])

def appeal_servers_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [IB(text="🌐 Сервер с секреткой", callback_data="app_srv_secret")],
        [IB(text="🕵️ Поиск саботера", callback_data="app_srv_saboteur")],
        [IB(text="🌾 Фарм сервер", callback_data="app_srv_farm")],
        [IB(text="🔎 Бейдж исследователь", callback_data="app_srv_researcher")],
        [IB(text="🧬 Бейдж мутатор", callback_data="app_srv_mutator")]
    ])

# ----------------------------------------------------------------------
# ОТПРАВКА БИЛЕТА С МЕДИА (В ТЕМУ ПОДДЕРЖКИ)
# ----------------------------------------------------------------------
async def dispatch_ticket_to_admin(user_id: int, user, category: str, text: str, photos: List[str], post_info: dict = None):
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

    rep_line, targets = "", []
    if category == "complaint":
        try:
            targets = _complaint_targets(text)
            rep_line = await _complaint_history_line(targets)
        except Exception as e:
            logging.warning(f"Не удалось проверить повторные жалобы: {e}")
            rep_line, targets = "", []

    ticket_id = await create_ticket(user_id, category, "")
    nopost = bool(post_info and post_info.get("nopost"))
    take_kb = take_ticket_kb(ticket_id, nopost=nopost)
    if nopost:
        try:
            await _db_exec("UPDATE tickets SET nopost = TRUE WHERE ticket_id = %s", (ticket_id,))
        except Exception as e:
            logging.warning(f"Тикет #{ticket_id}: не записан признак «поста нет»: {e}")
    if targets:
        try:
            await _record_complaint_targets(ticket_id, user_id, targets)
        except Exception as e:
            logging.warning(f"Тикет #{ticket_id}: не удалось записать нарушителей жалобы: {e}")
    admin_card_text = f"{notes_header}{title} | <b>Заявка №{ticket_id}</b>\n👤 От: {user_mention} | ID: <code>{user_id}</code>\n\n{rep_line}{text}"

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
                    cur.execute("DELETE FROM complaint_targets WHERE ticket_id = %s", (ticket_id,))
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
                        reply_markup=take_kb, message_thread_id=TOPIC_SUPPORT_ID, parse_mode="HTML"
                    )
                else:
                    sent = await bot.send_photo(
                        ADMIN_CHAT_ID, photo=photos[0], caption=admin_card_text,
                        reply_markup=take_kb, message_thread_id=TOPIC_SUPPORT_ID, parse_mode="HTML"
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
                    ADMIN_CHAT_ID, card_html, reply_markup=take_kb,
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
                        ADMIN_CHAT_ID, card_html, reply_markup=take_kb,
                        message_thread_id=TOPIC_SUPPORT_ID, parse_mode="HTML"
                    )
                    _kind = "text"
                else:
                    ctrl_msg = await bot.send_message(
                        ADMIN_CHAT_ID,
                        f"👆 <i>Управление заявкой №{ticket_id} (прикреплено файлов: {len(photos)}):</i>",
                        reply_markup=take_kb, message_thread_id=TOPIC_SUPPORT_ID, parse_mode="HTML"
                    )
                    _kind = "ctrl"
                await map_message(ctrl_msg.message_id, user_id, ticket_id)
                _card_msg, _card_kind, _link_msg = ctrl_msg.message_id, _kind, sent_group[0].message_id
        else:
            sent = await bot.send_message(
                ADMIN_CHAT_ID, card_html, reply_markup=take_kb,
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

    ping_uid = (post_info or {}).get("ping")
    if ping_uid and int(ping_uid) != int(user_id) and not TEST_MODE_ON:
        try:   # публикатор найденного поста: отмечаем, чтобы было видно, чья это жалоба или обжалование
            kind_txt = "жалоба" if category == "complaint" else "обжалование"
            await bot.send_message(
                ADMIN_CHAT_ID,
                f"🔔 {await _mention_html(int(ping_uid))}, на ваш пост поступило: <b>{kind_txt}</b> №{ticket_id}. "
                f'<a href="{_ticket_url(_link_msg)}">Открыть заявку</a>',
                message_thread_id=TOPIC_SUPPORT_ID, parse_mode="HTML", link_preview_options=LinkPreviewOptions(is_disabled=True))
        except Exception as e:
            logging.warning(f"Тикет #{ticket_id}: не удалось отметить публикатора: {e}")

    return ticket_id

# ----------------------------------------------------------------------
# СТАРТ И МЕНЮ
# ----------------------------------------------------------------------
@router.message(CommandStart(), F.chat.type == "private")
async def start_cmd(message: Message, state: FSMContext, command: CommandObject):
    payload = (command.args or "").strip()
    # вход на сайт: t.me/бот?start=login_<код>. Не требует подписки, показывает только кнопку подтверждения
    if payload.lower().startswith("login_"):
        await _web_login_prompt(message, payload[6:])
        return
    # реферальная ссылка t.me/бот?start=ref_ID: записываем до проверки подписки
    if payload.lower().startswith("ref_"):
        await _ref_register(message.from_user.id, payload[4:])
    # ссылка с сайта t.me/бот?start=go_question: запоминаем, куда человек шёл, до проверки подписки
    go_key = _go_key(payload)
    if go_key:
        _go_remember(message.from_user.id, go_key)
    # без подписки на канал дальше не пускаем (кроме стаффа)
    if not await _gate_for_start(message):
        return
    if go_key:
        _GO_PENDING.pop(message.from_user.id, None)
        await _go_run(message, state, go_key)
        return
    await _start_welcome(message, state)
    # ссылка-приглашение с промокодом: t.me/бот?start=promo_КОД
    if payload.lower().startswith("promo_"):
        try:
            if not await is_banned(message.from_user.id):
                await _promo_redeem_reply(message, payload[6:])
        except Exception as e:
            logging.error(f"Ошибка активации промокода по ссылке: {e}", exc_info=True)

# ---------- переходы с сайта: t.me/бот?start=go_<раздел> ----------
# Сайт ведёт человека сразу в нужную анкету. Если он не подписан на канал, бот просит подписаться,
# а после «Я подписался» сам запускает ту же анкету (раздел запоминается на час).
_GO_TITLES = {
    "shop": "Платные услуги",
    "helper": "Заявка в хелперы",
    "seeker": "Заявка в искатели секреток",
    "complaint": "Жалоба на игрока",
    "appeal": "Обжалование бана",
    "question": "Вопрос поддержке",
    "notify_helpers": "Уведомление о наборе в хелперы",
    "notify_seekers": "Уведомление о наборе в искатели секреток",
}
_GO_PENDING: Dict[int, tuple] = {}
_GO_TTL = 3600

def _go_key(payload: str) -> str:
    p = (payload or "").strip().lower()
    if not p.startswith("go_"):
        return ""
    k = p[3:]
    if k in _GO_TITLES or re.fullmatch(r"shop_\d{1,9}", k):
        return k          # shop_12 = конкретная услуга магазина
    return ""

def _go_remember(uid: int, key: str):
    if len(_GO_PENDING) > 20000:
        _GO_PENDING.clear()
    _GO_PENDING[uid] = (key, time.time())

def _go_starter(key: str):
    # функции анкет определены ниже по файлу, поэтому берём их в момент вызова
    return {
        "helper": start_helper_apply,
        "seeker": start_secret_apply,
        "complaint": start_complaint,
        "appeal": start_appeal,
        "question": start_question,
    }.get(key)

async def _go_run(message: Message, state: FSMContext, key: str):
    """Запускает анкету, выбранную на сайте. message.from_user должен быть самим пользователем."""
    uid = message.from_user.id
    starter = _go_starter(key)
    if starter is None and not key.startswith("shop"):
        await _start_welcome(message, state)
        return
    try:
        await register_user(uid)
    except Exception as e:
        logging.error(f"Ошибка при регистрации {uid}: {e}")
    banned = await is_banned(uid)
    if banned:
        await message.answer(
            f"{ICON_CROSS} Вы заблокированы в поддержке.\n<b>Причина:</b> {html.escape(banned[0])}",
            parse_mode="HTML"
        )
        return
    await state.clear()
    try:
        # короткое сообщение заодно ставит главное меню (клавиатуру), чтобы оно не пропало
        await message.answer(
            f"🌐 Переход с сайта: <b>{_GO_TITLES.get(key) or _GO_TITLES[key.split('_')[0]]}</b>",
            reply_markup=await main_keyboard(uid), parse_mode="HTML"
        )
    except Exception as e:
        logging.warning(f"Переход с сайта: не удалось отправить меню {uid}: {e}")
    if key.startswith("shop"):
        await _go_shop(message, int(key[5:]) if key.startswith("shop_") else 0)
        return
    if key in RECRUIT_NOTIFY_KEYS:
        kind = RECRUIT_NOTIFY_KEYS[key]
        if await get_setting(RECRUIT_KINDS[kind][0], "true") == "true":
            # набор уже открыт: подписка не нужна, сразу ведём в анкету
            await message.answer("🟢 Набор уже открыт, вот анкета.")
            await (start_helper_apply if kind == "helper" else start_secret_apply)(message, state)
            return
        await recruit_sub_set(uid, kind, True)
        await message.answer(
            _recruit_closed_text(kind, True), reply_markup=await _recruit_closed_kb(uid, kind), parse_mode="HTML"
        )
        return
    await starter(message, state)

async def _go_shop(message: Message, sid: int):
    """Сайт → бот: открывает витрину магазина или конкретную услугу."""
    if sid:
        screen = await _shop_service_screen(message.from_user.id, sid)
        if screen:
            await message.answer(screen[0], reply_markup=screen[1], parse_mode="HTML")
            return
        await message.answer("Эта услуга сейчас недоступна. Вот актуальный список:")
    text, kb = await _shop_home_screen()
    await message.answer(text, reply_markup=kb, parse_mode="HTML")

# ---------- вход на сайт через бота (личный кабинет) ----------
async def _web_login_prompt(message: Message, code: str):
    ent = _LOGIN.get(code)
    if not ent or time.time() - ent["t"] > _LOGIN_TTL or ent["uid"] is not None:
        await message.answer("⌛ Эта ссылка для входа устарела. Вернитесь на сайт и нажмите «Войти» ещё раз.")
        return
    # сверка числа: на сайте показано одно число, здесь нужно выбрать именно его. Так чужую ссылку для входа
    # (например, присланную мошенником) подтвердить не получится: человек не видит число с чужой страницы.
    right = int(ent.get("num") or 0)
    opts = {right}
    while len(opts) < 4:
        opts.add(10 + secrets.randbelow(90))
    order = list(opts)
    secrets.SystemRandom().shuffle(order)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [IB(text=str(n), callback_data=f"weblogin_{code}_{n}") for n in order[:2]],
        [IB(text=str(n), callback_data=f"weblogin_{code}_{n}") for n in order[2:]],
        [IB(text="❌ Это не я", callback_data="weblogin_no")],
    ])
    await message.answer(
        "🔐 <b>Вход на сайт</b>\n\n"
        f"Запрос входа в личный кабинет на {html.escape(SITE_URL.replace('https://', ''))}.\n"
        "<b>Выберите число, которое сейчас показано на странице сайта.</b>\n\n"
        "Если вы сами не нажимали «Войти на сайте» или не видите там такого числа, нажмите «Это не я». "
        "Никому не показывайте эту ссылку и не подтверждайте вход по просьбе других людей.",
        reply_markup=kb, parse_mode="HTML"
    )
    try:
        if await _login_is_staff(message.from_user.id):
            await message.answer(
                "⚠️ <b>У вас есть роль в команде.</b> После входа на сайте откроется ваша рабочая статистика"
                " (для главных админов ещё и общая по проекту). Подтверждайте вход только со своего устройства. "
                f"Сессия команды действует {_SESS_DAYS_STAFF} дня.",
                parse_mode="HTML"
            )
    except Exception as e:
        logging.warning(f"Вход на сайт: не удалось проверить роль {message.from_user.id}: {e}")

@router.callback_query(F.data.startswith("weblogin_"))
async def web_login_confirm(call: CallbackQuery):
    payload = call.data[len("weblogin_"):]
    code, _, picked = payload.rpartition("_")
    if payload == "no":
        try:
            await call.message.edit_text("Вход отменён. Если это были не вы, ничего делать не нужно.")
        except Exception:
            pass
        await call.answer()
        return
    ent = _LOGIN.get(code)
    if not ent or time.time() - ent["t"] > _LOGIN_TTL or ent["uid"] is not None:
        await call.answer("Ссылка устарела. Нажмите «Войти» на сайте ещё раз.", show_alert=True)
        return
    if picked != str(ent.get("num")):
        _LOGIN.pop(code, None)      # одна попытка: после ошибки запрос входа аннулируется
        try:
            await call.message.edit_text("⛔ Число не совпало, вход отменён. Если это были не вы, ничего делать не нужно. "
                                         "Чтобы войти, нажмите «Войти» на сайте ещё раз.")
        except Exception:
            pass
        await call.answer()
        return
    ent["uid"] = call.from_user.id
    ent["name"] = (call.from_user.full_name or "")[:40]
    try:
        await call.message.edit_text("✅ Готово! Вернитесь на сайт, вход выполнится сам.")
    except Exception:
        pass
    await call.answer()

WELCOME_DEFAULT = (
    '<tg-emoji emoji-id="5206693654990717395">🔥</tg-emoji>'
    '<b>Добро пожаловать в поддержку Tower Of Hell Secrets (@ToHSecretss)!</b>'
    '<tg-emoji emoji-id="5206693654990717395">🔥</tg-emoji>\n\n'
    '<tg-emoji emoji-id="5208629654449064934">🔥</tg-emoji>'
    '<i>Выберите нужный раздел на клавиатуре ниже</i>'
)
WELCOME_MAX = 950   # подпись к фото ограничена 1024 знаками; запас на строку праздничного режима

async def get_welcome_html() -> str:
    """Свой текст приветствия (HTML) или пустая строка, если используется стандартный."""
    try:
        return ((await get_setting("welcome_text", "")) or "").strip()
    except Exception:
        return ""

class WelcomeAdminStates(StatesGroup):
    waiting_text = State()

def _welcome_menu_kb(custom: bool):
    rows = [[IB(text="✏️ Изменить текст", callback_data="welcome_edit")]]
    if custom:
        rows.append([IB(text="♻️ Вернуть стандартный", callback_data="welcome_reset")])
    rows.append([IB(text="🔙 Назад", callback_data="adm_system")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

async def _welcome_screen_text() -> tuple:
    custom = await get_welcome_html()
    head = ("👋 <b>Приветствие бота</b>\n\nТекст ниже показывается под фото при /start. "
            f"Фото берётся из файла <code>{PHOTO_FILENAME}</code> в репозитории, поменять его можно только заменой файла.\n"
            f"Сейчас: <b>{'свой текст' if custom else 'стандартный текст'}</b>.\n\n— — —\n")
    return head + (custom or WELCOME_DEFAULT), bool(custom)

@router.callback_query(F.data == "adm_welcome")
async def adm_welcome(call: CallbackQuery, state: FSMContext):
    if not await is_main_admin(call.from_user.id):
        await call.answer("❌ Нет доступа", show_alert=True); return
    await state.clear()
    text, custom = await _welcome_screen_text()
    try:
        await call.message.edit_text(text, reply_markup=_welcome_menu_kb(custom), parse_mode="HTML")
    except TelegramBadRequest:
        await call.message.edit_text("👋 <b>Приветствие бота</b>\n\nТекущий текст показать не удалось (ошибка разметки). Задайте новый или верните стандартный.",
                                     reply_markup=_welcome_menu_kb(True), parse_mode="HTML")
    await call.answer()

@router.callback_query(F.data == "welcome_edit")
async def welcome_edit(call: CallbackQuery, state: FSMContext):
    if not await is_main_admin(call.from_user.id):
        await call.answer("❌ Нет доступа", show_alert=True); return
    await state.set_state(WelcomeAdminStates.waiting_text)
    kb = InlineKeyboardMarkup(inline_keyboard=[[IB(text="Отмена", callback_data="adm_welcome")]])
    await call.message.edit_text(
        "✏️ Отправьте <b>новый текст приветствия одним сообщением</b>.\n\n"
        "• Жирный, курсив, ссылки и эмодзи сохранятся в том виде, как вы их оформили.\n"
        f"• Не больше <b>{WELCOME_MAX}</b> символов (подпись к фото ограничена Telegram).\n"
        "• Фото остаётся прежним.", reply_markup=kb, parse_mode="HTML")
    await call.answer()

@router.message(WelcomeAdminStates.waiting_text, F.chat.type == "private")
async def welcome_save(message: Message, state: FSMContext):
    if not await is_main_admin(message.from_user.id):
        await state.clear(); return
    if not message.text:
        await message.answer("Нужен текст одним сообщением (без фото и файлов). Отправьте текст или нажмите «Отмена» выше.")
        return
    if message.text.startswith("/"):
        await state.clear()
        await message.answer("Изменение приветствия отменено.")
        return
    plain_len = len(message.text)
    if plain_len > WELCOME_MAX:
        await message.answer(f"❌ Слишком длинно: {plain_len} символов, максимум {WELCOME_MAX}. Сократите текст и отправьте снова.")
        return
    new_html = message.html_text
    await set_setting("welcome_text", new_html)
    await state.clear()
    await alog(message.from_user.id, "welcome_edit", "", f"{plain_len} симв.")
    text, custom = await _welcome_screen_text()
    try:
        await message.answer("✅ Приветствие сохранено. Так оно выглядит:\n\n— — —\n" + new_html, parse_mode="HTML", reply_markup=_welcome_menu_kb(True))
    except TelegramBadRequest:
        await message.answer("✅ Сохранено, но показать предпросмотр не удалось. Проверьте через /start.", reply_markup=_welcome_menu_kb(True))

@router.callback_query(F.data == "welcome_reset")
async def welcome_reset(call: CallbackQuery, state: FSMContext):
    if not await is_main_admin(call.from_user.id):
        await call.answer("❌ Нет доступа", show_alert=True); return
    await set_setting("welcome_text", "")
    await alog(call.from_user.id, "welcome_edit", "", "возвращён стандартный")
    await call.answer("Вернул стандартный текст.")
    text, custom = await _welcome_screen_text()
    await call.message.edit_text(text, reply_markup=_welcome_menu_kb(custom), parse_mode="HTML")

async def _start_welcome(message: Message, state: FSMContext):
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

    custom = await get_welcome_html()
    welcome_caption = custom or WELCOME_DEFAULT
    welcome_caption += await _event_line()

    for caption in ([welcome_caption] if not custom else [welcome_caption, WELCOME_DEFAULT + await _event_line()]):
        # если собственный текст Telegram не принял (редкая ошибка разметки), показываем стандартный, а не молчим
        if os.path.exists(PHOTO_FILENAME):
            try:
                await message.answer_photo(photo=FSInputFile(PHOTO_FILENAME), caption=html_cut(caption, 1024), reply_markup=kb, parse_mode="HTML")
                return
            except TelegramBadRequest as e:
                logging.error(f"Приветствие: Telegram не принял фото/текст ({e})")
                continue
            except Exception as e:
                logging.error(f"Не удалось отправить локальное фото {PHOTO_FILENAME}: {e}")
        try:
            await message.answer(text=caption, reply_markup=kb, parse_mode="HTML")
            return
        except TelegramBadRequest as e:
            logging.error(f"Приветствие: Telegram не принял текст ({e})")
    await message.answer("Выберите нужный раздел на клавиатуре ниже.", reply_markup=kb)

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

@router.callback_query(F.data == "adm_recruit")
async def adm_recruit_screen(call: CallbackQuery):
    if not await is_main_admin(call.from_user.id): return
    await call.message.edit_text(
        "📝 <b>Наборы и друзья</b>\n\nВключайте и выключайте приём заявок. 🟢 открыт, 🔴 закрыт.\n"
        f"🔔 Ждут открытия: хелперы {await recruit_sub_count('helper')}, искатели {await recruit_sub_count('secret')}. "
        "Когда включите набор, им уйдёт разовое уведомление.",
        reply_markup=await recruit_kb(), parse_mode="HTML"
    )
    await call.answer()

@router.callback_query(F.data == "adm_system")
async def adm_system_screen(call: CallbackQuery):
    if not await is_main_admin(call.from_user.id): return
    await call.message.edit_text(
        "⚙️ <b>Система и доступ</b>\n\nТех. перерыв, тест режим, публикаторы и главные админы. Тех. режимы и главные админы доступны только создателю бота.",
        reply_markup=await system_kb(), parse_mode="HTML"
    )
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
        [IB(text="🔙 Назад", callback_data="adm_system")]
    ])
    await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await call.answer()

@router.callback_query(F.data == "add_main_admin_start")
async def add_main_admin_start(call: CallbackQuery, state: FSMContext):
    if OWNER_ID and call.from_user.id != OWNER_ID:
        await call.answer("❌ Доступно только создателю бота!", show_alert=True)
        return
    await state.set_state(Form.add_main_admin_id)
    await call.message.answer("<tg-emoji emoji-id='5346004239246183111'>➕</tg-emoji> Введите <b>ID или @юзернейм</b> пользователя, которого хотите сделать главным администратором:", parse_mode="HTML")
    await call.answer()

@router.message(Form.add_main_admin_id, F.chat.type == "private")
async def process_add_main_admin(message: Message, state: FSMContext):
    if OWNER_ID and message.from_user.id != OWNER_ID:
        return
    try:
        new_id = await resolve_user_one(message.text)
        def _query():
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute("INSERT INTO main_admins (admin_id) VALUES (%s) ON CONFLICT (admin_id) DO NOTHING", (new_id,))
        await asyncio.to_thread(_query)
        await alog(message.from_user.id, "admin_add", f"ID {new_id}")
        await state.clear()
        kb = await main_keyboard(message.from_user.id)
        await message.answer(f"{ICON_CHECK} Пользователь <code>{new_id}</code> успешно назначен главным администратором!", reply_markup=kb, parse_mode="HTML")
    except ValueError:
        await message.answer(f"{ICON_CROSS} " + USER_NOT_FOUND_TEXT, parse_mode="HTML")

@router.callback_query(F.data == "del_main_admin_start")
async def del_main_admin_start(call: CallbackQuery, state: FSMContext):
    if OWNER_ID and call.from_user.id != OWNER_ID:
        await call.answer("❌ Доступно только создателю бота!", show_alert=True)
        return
    await state.set_state(Form.del_main_admin_id)
    await call.message.answer("<tg-emoji emoji-id='5782743443348394021'>➖</tg-emoji> Введите <b>ID или @юзернейм</b> главного администратора, которого хотите снять:", parse_mode="HTML")
    await call.answer()

@router.message(Form.del_main_admin_id, F.chat.type == "private")
async def process_del_main_admin(message: Message, state: FSMContext):
    if OWNER_ID and message.from_user.id != OWNER_ID:
        return
    try:
        del_id = await resolve_user_one(message.text)
        if OWNER_ID and del_id == OWNER_ID:
            await message.answer(f"{ICON_CROSS} Нельзя удалить создателя бота!", parse_mode="HTML")
            return
        def _query():
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM main_admins WHERE admin_id = %s", (del_id,))
        await asyncio.to_thread(_query)
        await alog(message.from_user.id, "admin_del", f"ID {del_id}")
        await state.clear()
        kb = await main_keyboard(message.from_user.id)
        await message.answer(f"{ICON_CHECK} Пользователь <code>{del_id}</code> снят с поста главного администратора.", reply_markup=kb, parse_mode="HTML")
    except ValueError:
        await message.answer(f"{ICON_CROSS} " + USER_NOT_FOUND_TEXT, parse_mode="HTML")

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
    for fn in ("mutator.jpg", RESEARCHER_PHOTO, PHOTO_FILENAME):
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
            ("template_farm_live_item_fx", "Фарм: блок сервера (с эффектами)", [], 4096),
            ("template_farm_live_closed", "Фарм: закрытый пост", [], 4096),
            ("template_mutator_active", "Мутатор: активный пост", ["[Ссылка]"], 1024),
            ("template_mutator_expired", "Мутатор: истекший пост", [], 1024),
            ("template_researcher_active", "Исследователь: активный пост", ["[Ссылка]"], 1024),
            ("template_researcher_expired", "Исследователь: закрытый пост", [], 1024),
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
            ok(f"Все {len(checks)} шаблонов заполнены, обязательные теги на месте, длина в пределах")
    except Exception as e:
        bad(f"Шаблоны: {html.escape(str(e))[:150]}")

    # 7. Фоновые воркеры
    try:
        alive = {t.get_coro().__name__ for t in asyncio.all_tasks() if not t.done() and hasattr(t.get_coro(), "__name__")}
        need = {"secret_timer_worker": "таймер секреток", "mutator_timer_worker": "таймер мутатора",
                "reminder_worker": "напоминания", "prelink_expiry_worker": "срок предссылок",
                "userbot_watchdog_worker": "сторож юзербота", "promo_hold_worker": "закрепление промокодов",
                "health_watchdog_worker": "сторож здоровья бота",
                "prelink_renew_reminder_worker": "напоминания о продлении",
                "referral_worker": "рефералка", "userbot_keepalive_worker": "keepalive юзербота"}
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
                cur.execute("SELECT COUNT(*), COALESCE(SUM(CASE WHEN is_closed THEN 1 ELSE 0 END), 0) FROM researcher_badges WHERE channel_id = %s AND created_at >= %s", (ch, started_at))
                res = cur.fetchone()
                return sec, sab, sab_posts, farm, farm_posts, mut, res
    try:
        sec, sab, sab_posts, farm, farm_posts, mut, res = await asyncio.to_thread(_q)
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
        f"🔍 Исследователь: <b>{res[0]}</b> (закрыто: {int(res[1])}, ещё активны: {res[0] - int(res[1])})",
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
    await alog(call.from_user.id, "testmode", "", "включён" if TEST_MODE_ON else "выключен")
    logging.warning(f"Тест режим {'ВКЛЮЧЁН' if TEST_MODE_ON else 'выключен'} пользователем {call.from_user.id}")
    await call.answer(msg[:200], show_alert=True)
    try:
        await call.message.edit_reply_markup(reply_markup=await system_kb())
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
    await alog(call.from_user.id, "maintenance", "", "включён" if MAINTENANCE_ON else "выключен")
    logging.warning(f"Технический перерыв {'ВКЛЮЧЁН' if MAINTENANCE_ON else 'выключен'} пользователем {call.from_user.id}")
    await call.answer(
        "🛠 Перерыв ВКЛЮЧЁН: бот закрыт для всех, кроме вас." if MAINTENANCE_ON else "🟢 Перерыв выключен, бот работает для всех.",
        show_alert=True
    )
    kb = await system_kb()
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
    await alog(call.from_user.id, "friend_toggle", "", "включено" if new_val == "true" else "выключено")
    await call.answer("Статус изменен")
    kb = await recruit_kb()
    await call.message.edit_reply_markup(reply_markup=kb)

@router.callback_query(F.data == "toggle_helper")
async def toggle_helper_callback(call: CallbackQuery):
    if not await is_main_admin(call.from_user.id): return
    current = await get_setting("helper_recruitment", "true")
    new_val = "false" if current == "true" else "true"
    await set_setting("helper_recruitment", new_val)
    await alog(call.from_user.id, "recruit_helper", "", "открыт" if new_val == "true" else "закрыт")
    if new_val == "true":
        spawn(notify_recruit_open("helper", call.from_user.id))
    await call.answer("Статус набора хелперов изменен")
    kb = await recruit_kb()
    await call.message.edit_reply_markup(reply_markup=kb)

@router.callback_query(F.data == "toggle_secret")
async def toggle_secret_callback(call: CallbackQuery):
    if not await is_main_admin(call.from_user.id): return
    current = await get_setting("secret_recruitment", "true")
    new_val = "false" if current == "true" else "true"
    await set_setting("secret_recruitment", new_val)
    if new_val == "true":
        # новый открытый набор: у всех снова 3 попытки
        try:
            ep = int((await get_setting("secret_recruit_epoch", "0")) or 0)
        except ValueError:
            ep = 0
        await set_setting("secret_recruit_epoch", str(ep + 1))
        spawn(notify_recruit_open("secret", call.from_user.id))
    await alog(call.from_user.id, "recruit_secret", "", "открыт" if new_val == "true" else "закрыт")
    await call.answer("Статус набора в искатели изменен")
    kb = await recruit_kb()
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
async def _secret_panel_text() -> str:
    dur = await get_setting("timer_seconds", "510")
    dur_int = int(dur) if dur.isdigit() else 510
    m, s = dur_int // 60, dur_int % 60
    if PUBLISH_PAUSED:
        since = datetime.fromtimestamp(PUBLISH_PAUSED_SINCE, MSK_TZ).strftime("%d.%m %H:%M") if PUBLISH_PAUSED_SINCE else "?"
        state = (f"🔴 <b>Публикации в канал ОСТАНОВЛЕНЫ</b> (с {since} МСК). Новые секретки, саботёр, фарм и бейджи не выходят, "
                 "поддержка работает. Посты, которые уже в канале, можно закрывать.")
    else:
        state = "🟢 Публикации в канал идут. Кнопка «Остановить публикацию» блокирует любые новые сообщения бота в канале, например на время рекламы."
    return (
        f"{ICON_BALL} <b>Панель управления секретками</b>\n\n"
        f"{state}\n\n"
        f"{ICON_TIMER} Таймер удаления ссылки: <b>{m} мин. {s} сек.</b> (<code>{dur_int}</code> сек.)\n"
        f"Настройте нужные параметры ниже:"
    )

@router.callback_query(F.data == "adm_secret_panel")
async def adm_secret_panel(call: CallbackQuery):
    if not await is_main_admin(call.from_user.id): return
    await call.message.edit_text(await _secret_panel_text(), reply_markup=secret_admin_kb(), parse_mode="HTML")
    await call.answer()

@router.callback_query(F.data == "toggle_publish_pause")
async def toggle_publish_pause(call: CallbackQuery):
    global PUBLISH_PAUSED, PUBLISH_PAUSED_SINCE
    if not await is_main_admin(call.from_user.id): return
    new_state = not PUBLISH_PAUSED
    since = time.time() if new_state else 0.0
    try:
        await set_setting("publish_paused", "true" if new_state else "false")
        await set_setting("publish_paused_since", str(int(since)))
    except Exception as e:
        logging.error(f"Не удалось сохранить остановку публикаций: {e}")
        await call.answer("❌ Не удалось сохранить настройку (база не отвечает). Попробуйте ещё раз.", show_alert=True)
        return
    PUBLISH_PAUSED, PUBLISH_PAUSED_SINCE = new_state, since
    _PAUSE_REMIND["t"] = 0.0
    await alog(call.from_user.id, "publish_pause", "", "остановлены" if new_state else "возобновлены")
    logging.warning(f"Публикации в канал {'ОСТАНОВЛЕНЫ' if new_state else 'возобновлены'} пользователем {call.from_user.id}")
    spawn(_notify_pause_change(call.from_user, new_state))
    try:
        await call.message.edit_text(await _secret_panel_text(), reply_markup=secret_admin_kb(), parse_mode="HTML")
    except Exception:
        pass
    await call.answer("⏸ Публикации остановлены. Новые посты в канал не выйдут." if new_state else "▶️ Публикации возобновлены.", show_alert=True)

@router.callback_query(F.data == "adm_sec_people")
async def adm_sec_people(call: CallbackQuery):
    if not await is_main_admin(call.from_user.id): return
    await call.message.edit_text(
        "🔔 <b>Предссылки и уведомления</b>\n\nДоступ к Предссылкам и уведомления стаффа.",
        reply_markup=await sec_people_kb(), parse_mode="HTML"
    )
    await call.answer()

@router.callback_query(F.data == "adm_sec_types")
async def adm_sec_types(call: CallbackQuery):
    if not await is_main_admin(call.from_user.id): return
    await call.message.edit_text(
        "🧩 <b>Типы секреток</b>\n\nОбычные и особые типы, из которых публикаторы выбирают при отправке поста.",
        reply_markup=SEC_TYPES_KB, parse_mode="HTML"
    )
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
        [IB(text="🔙 Назад", callback_data="adm_system")]
    ])
    await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML", link_preview_options=LinkPreviewOptions(is_disabled=True))
    await call.answer()

@router.callback_query(F.data == "sec_add_pub")
async def sec_add_pub_prompt(call: CallbackQuery, state: FSMContext):
    if not await is_main_admin(call.from_user.id): return
    await state.set_state(SecretAdminStates.add_publisher_id)
    await call.message.answer(f"{ICON_USERS} Введите <b>ID или @юзернейм</b> пользователя, которому хотите выдать доступ к секреткам:", parse_mode="HTML")
    await call.answer()

@router.message(SecretAdminStates.add_publisher_id)
async def sec_add_pub_proc(message: Message, state: FSMContext):
    if not await is_main_admin(message.from_user.id): return
    try:
        t_id = await resolve_user_one(message.text)
        def _q():
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute("INSERT INTO secret_publishers (user_id) VALUES (%s) ON CONFLICT (user_id) DO NOTHING", (t_id,))
        await asyncio.to_thread(_q)
        await alog(message.from_user.id, "pub_add", f"ID {t_id}")
        await state.clear()
        kb = await main_keyboard(message.from_user.id)
        await message.answer(f"{ICON_CHECK} Пользователь <code>{t_id}</code> добавлен в публикаторы! У него появится кнопка '🔮 Секретки'.", reply_markup=kb, parse_mode="HTML")
    except ValueError:
        await message.answer(f"{ICON_CROSS} " + USER_NOT_FOUND_TEXT, parse_mode="HTML")

@router.callback_query(F.data == "sec_del_pub")
async def sec_del_pub_prompt(call: CallbackQuery, state: FSMContext):
    if not await is_main_admin(call.from_user.id): return
    await state.set_state(SecretAdminStates.del_publisher_id)
    await call.message.answer(f"{ICON_USERS} Введите <b>ID или @юзернейм</b> пользователя для отзыва прав публикатора:", parse_mode="HTML")
    await call.answer()

@router.message(SecretAdminStates.del_publisher_id)
async def sec_del_pub_proc(message: Message, state: FSMContext):
    if not await is_main_admin(message.from_user.id): return
    try:
        t_id = await resolve_user_one(message.text)
        def _q():
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM secret_publishers WHERE user_id = %s", (t_id,))
        await asyncio.to_thread(_q)
        await alog(message.from_user.id, "pub_del", f"ID {t_id}")
        await state.clear()
        kb = await main_keyboard(message.from_user.id)
        await message.answer(f"{ICON_CHECK} Пользователь <code>{t_id}</code> удален из публикаторов.", reply_markup=kb, parse_mode="HTML")
    except ValueError:
        await message.answer(f"{ICON_CROSS} " + USER_NOT_FOUND_TEXT, parse_mode="HTML")

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
            
            post_url = _channel_post_link(ch_id, mid)
            if not post_url:
                clean_ch = str(ch_id or "").replace("@", "")
                if clean_ch.startswith("https://t.me/"):
                    post_url = f"{clean_ch}/{mid}"
                elif clean_ch.lstrip("-").isdigit() and mid:
                    post_url = f"https://t.me/c/{clean_ch.lstrip('-')}/{mid}"
            if post_url:
                text += f"   {post_idx}. {time_str} ({html.escape(stype)}) — <a href='{post_url}'>ссылка</a>\n"
            else:
                text += f"   {post_idx}. {time_str} ({html.escape(stype)}) — <i>ссылки нет</i>\n"
            
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

TMPL_GROUPS = {
    "sec": ("🔮 Секретки и особые", [
        ("✏️ Обычный активный пост", "sec_tmpl_act"),
        ("🌾 Шаблон фарм-секретки", "sec_tmpl_farm"),
        ("✏ Истекший пост", "sec_tmpl_exp"),
    ]),
    "sab": ("🕵️ Саботёр", [
        ("🕵️ Активный пост", "sec_tmpl_sab_act"),
        ("🔹 Блок сервера", "sec_tmpl_sab_item"),
        ("🔒 Закрытый пост", "sec_tmpl_sab_close"),
    ]),
    "farm": ("🌾 Фарм", [
        ("🌾 Активный пост", "sec_tmpl_flive_act"),
        ("🔹 Блок сервера", "sec_tmpl_flive_item"),
        ("✨ Блок сервера (с эффектами)", "sec_tmpl_flive_itemfx"),
        ("🔒 Закрытый пост", "sec_tmpl_flive_close"),
    ]),
    "mut": ("🧬 Мутатор", [
        ("🧬 Активный пост", "sec_tmpl_mut_act"),
        ("🧬 Истекший пост", "sec_tmpl_mut_exp"),
    ]),
    "res": ("🔍 Исследователь", [
        ("🔍 Активный пост", "sec_tmpl_res_act"),
        ("🔍 Закрытый пост", "sec_tmpl_res_exp"),
    ]),
}

@router.callback_query(F.data == "sec_templates")
async def sec_templates(call: CallbackQuery):
    if not await is_main_admin(call.from_user.id): return
    rows = [[IB(text="👁 Посмотреть используемые шаблоны", callback_data="sec_tmpl_view")]]
    for key, (title, _items) in TMPL_GROUPS.items():
        rows.append([IB(text=title, callback_data=f"sec_tg_{key}")])
    rows.append([IB(text="🔙 Назад", callback_data="adm_secret_panel")])
    await call.message.edit_text(
        f"{ICON_PENCIL} <b>Шаблоны постов</b>\n\nВыберите раздел:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML"
    )
    await call.answer()

@router.callback_query(F.data.startswith("sec_tg_"))
async def sec_templates_group(call: CallbackQuery, state: FSMContext):
    if not await is_main_admin(call.from_user.id): return
    await state.clear()
    key = call.data[len("sec_tg_"):]
    grp = TMPL_GROUPS.get(key)
    if not grp:
        await call.answer()
        return
    title, items = grp
    rows = [[IB(text=t, callback_data=cb)] for t, cb in items]
    rows.append([IB(text="🔙 К шаблонам", callback_data="sec_templates")])
    await call.message.edit_text(
        f"{ICON_PENCIL} <b>{title}</b>\n\nКакой шаблон изменить?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML"
    )
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

@router.callback_query(F.data == "sec_tmpl_flive_itemfx")
async def sec_tmpl_flive_itemfx(call: CallbackQuery, state: FSMContext):
    if not await is_main_admin(call.from_user.id): return
    tmpl = await get_setting("template_farm_live_item_fx", DEFAULT_FARM_LIVE_ITEM_FX)
    await state.set_state(SecretAdminStates.template_farm_live_item_fx)
    await call.message.answer(
        f"{ICON_PENCIL} <b>Текущий шаблон блока сервера фарма С ЭФФЕКТАМИ:</b>\n\n{tmpl}\n\n"
        "Этот блок используется, когда публикатор ответил «Да» на вопрос про эффекты. Если ответил «Нет», берётся обычный блок сервера. "
        "Теги те же: <code>[Номер_Сервера]</code>, <code>[Ник_Роблокс]</code>, <code>[Искатель]</code>, "
        "<code>[Секции]</code>, <code>[Друзья]</code>, <code>[Ссылка]</code>. Отправьте новый текст:",
        parse_mode="HTML", reply_markup=TMPL_CANCEL_KB
    )
    await call.answer()

@router.message(SecretAdminStates.template_farm_live_item_fx)
async def sec_tmpl_flive_itemfx_proc(message: Message, state: FSMContext):
    if not await is_main_admin(message.from_user.id): return
    await set_setting("template_farm_live_item_fx", extract_text_with_custom_emojis(message))
    await state.clear()
    kb = await main_keyboard(message.from_user.id)
    await message.answer(f"{ICON_CHECK} Шаблон блока сервера фарма с эффектами сохранён!", reply_markup=kb, parse_mode="HTML")

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
        ("1/5 🔮 Секретки и особые", [
            ("Обычный активный пост", "template_active", ""),
            ("Шаблон фарм-секретки", "template_farm", ""),
            ("Истекший пост", "template_expired", ""),
        ]),
        ("2/5 🕵️ Саботер", [
            ("Активный пост", "template_saboteur_active", ""),
            ("Блок сервера", "template_saboteur_item", ""),
            ("Закрытый пост", "template_saboteur_closed", ""),
        ]),
        ("3/5 🌾 Фарм", [
            ("Активный пост", "template_farm_live_active", DEFAULT_FARM_LIVE_ACTIVE),
            ("Блок сервера", "template_farm_live_item", DEFAULT_FARM_LIVE_ITEM),
            ("Блок сервера (с эффектами)", "template_farm_live_item_fx", DEFAULT_FARM_LIVE_ITEM_FX),
            ("Закрытый пост", "template_farm_live_closed", DEFAULT_FARM_LIVE_CLOSED),
        ]),
        ("4/5 🧬 Мутатор", [
            ("Активный пост", "template_mutator_active", DEFAULT_MUTATOR_ACTIVE),
            ("Истекший пост", "template_mutator_expired", DEFAULT_MUTATOR_EXPIRED),
        ]),
        ("5/5 🔍 Исследователь", [
            ("Активный пост", "template_researcher_active", DEFAULT_RESEARCHER_ACTIVE),
            ("Закрытый пост", "template_researcher_expired", DEFAULT_RESEARCHER_EXPIRED),
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

@router.callback_query(F.data == "sec_tmpl_res_act")
async def sec_tmpl_res_act(call: CallbackQuery, state: FSMContext):
    if not await is_main_admin(call.from_user.id): return
    tmpl = await get_setting("template_researcher_active", DEFAULT_RESEARCHER_ACTIVE)
    await state.set_state(SecretAdminStates.template_researcher_active)
    await call.message.answer(
        f"{ICON_PENCIL} <b>Текущий шаблон активного поста исследователя:</b>\n\n{tmpl}\n\n"
        "Отправьте новый текст (обязателен тег <code>[Ссылка]</code>). Поддерживаются цитаты, жирный шрифт и Premium-эмодзи:",
        parse_mode="HTML", reply_markup=TMPL_CANCEL_KB
    )
    await call.answer()

@router.message(SecretAdminStates.template_researcher_active, F.text)
async def sec_tmpl_res_act_proc(message: Message, state: FSMContext):
    if not await is_main_admin(message.from_user.id): return
    saved_text = extract_text_with_custom_emojis(message)
    if "[Ссылка]" not in saved_text:
        await message.answer(f"{ICON_CROSS} В шаблоне нет тега <code>[Ссылка]</code>. Отправьте текст ещё раз или нажмите «Отменить».", parse_mode="HTML", reply_markup=TMPL_CANCEL_KB)
        return
    await set_setting("template_researcher_active", saved_text)
    await state.clear()
    kb = await main_keyboard(message.from_user.id)
    await message.answer(f"{ICON_CHECK} Шаблон активного поста исследователя сохранён!", reply_markup=kb, parse_mode="HTML")

@router.callback_query(F.data == "sec_tmpl_res_exp")
async def sec_tmpl_res_exp(call: CallbackQuery, state: FSMContext):
    if not await is_main_admin(call.from_user.id): return
    tmpl = await get_setting("template_researcher_expired", DEFAULT_RESEARCHER_EXPIRED)
    await state.set_state(SecretAdminStates.template_researcher_expired)
    await call.message.answer(
        f"{ICON_PENCIL} <b>Текущий шаблон закрытого поста исследователя:</b>\n\n{tmpl}\n\n"
        "Отправьте новый текст (на него пост изменится, когда искатель нажмёт «Закрыть пост»). Поддерживаются цитаты, жирный шрифт и Premium-эмодзи:",
        parse_mode="HTML", reply_markup=TMPL_CANCEL_KB
    )
    await call.answer()

@router.message(SecretAdminStates.template_researcher_expired, F.text)
async def sec_tmpl_res_exp_proc(message: Message, state: FSMContext):
    if not await is_main_admin(message.from_user.id): return
    saved_text = extract_text_with_custom_emojis(message)
    await set_setting("template_researcher_expired", saved_text)
    await state.clear()
    kb = await main_keyboard(message.from_user.id)
    await message.answer(f"{ICON_CHECK} Шаблон закрытого поста исследователя сохранён!", reply_markup=kb, parse_mode="HTML")

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
        "➕ Отправьте <b>ID или @юзернейм</b> пользователя (можно несколько через пробел или с новой строки):",
        reply_markup=kb, parse_mode="HTML"
    )
    await call.answer()

@router.message(SecretAdminStates.prelink_add, F.text)
async def sec_prelink_add_proc(message: Message, state: FSMContext):
    if not await is_main_admin(message.from_user.id): return
    ids, failed = await resolve_user_refs(message.text)
    if not ids:
        await message.answer(f"{ICON_CROSS} Не нашёл ни одного пользователя. Отправьте числовой ID или @юзернейм, либо нажмите «Отменить».", parse_mode="HTML")
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
    miss = ("\n\n⚠️ Не нашёл: " + ", ".join(html.escape(x) for x in failed) + ". Их не добавлю.") if failed else ""
    await message.answer(
        "⏳ <b>На какой срок выдать предссылки?</b>\n\n" + "\n".join(f"• {l}" for l in labels) + miss,
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
    try:
        await call.message.edit_reply_markup(reply_markup=await sec_people_kb())
    except Exception:
        pass

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
        f"{ICON_BALL} <b>Панель публикации секреток</b>\n\n"
        + ("⏸ <b>Публикации в канал сейчас приостановлены администрацией.</b> Отправить новый пост пока нельзя.\n\n" if _pub_paused() else "")
        + "Выберите нужное действие:",
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

async def notify_owner_closed(owner_id, actor_id: int, what: str):
    """Если пост закрыл главный админ, сообщаем хозяину поста."""
    if not owner_id or owner_id == actor_id:
        return
    try:
        await bot.send_message(owner_id, f"ℹ️ Администратор закрыл ваш пост: {what}.", parse_mode="HTML")
    except Exception:
        pass

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
    uid = call.from_user.id
    is_adm = await is_main_admin(uid)
    if not is_adm and not await is_secret_publisher(uid): return
    pid = int(call.data.split("_")[2])
    def _q():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT channel_id, message_id, secret_type, is_expired, user_id FROM secret_posts WHERE id = %s", (pid,))
                return cur.fetchone()
    row = await asyncio.to_thread(_q)
    if not row or row[3]:
        await call.answer("⚠️ Пост уже завершен или не найден!", show_alert=True)
        return
    if not is_adm and row[4] != uid:
        await call.answer("⚠️ Это не ваш пост!", show_alert=True)
        return
    await expire_secret_post(bot, pid, row[0], row[1], row[2])
    await call.message.edit_text(f"{ICON_CHECK} Пост закрыт досрочно (ссылка стёрта).", parse_mode="HTML")
    await notify_owner_closed(row[4], uid, f"секретка «{html.escape(str(row[2]))}»")
    await call.answer()

@router.callback_query(F.data == "pub_send_secret")
async def pub_send_secret(call: CallbackQuery, state: FSMContext):
    if not await is_secret_publisher(call.from_user.id): return
    if await _pub_paused_alert(call): return
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
    if action == "yes" and await _pub_paused_alert(call): return
    
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
                    _sc = await bot.send_photo(
                        chat_id=ADMIN_CHAT_ID,
                        photo=data["photo_id"],
                        caption=staff_caption,
                        message_thread_id=TOPIC_SECRETS_ID,
                        parse_mode="HTML"
                    )
                    await plog("secret", call.from_user.id, _sc.message_id)

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
                                "INSERT INTO secret_posts (user_id, channel_id, message_id, secret_type, base_text, photo_id, created_at) "
                                "VALUES (%s, %s, 0, %s, %s, %s, CURRENT_TIMESTAMP) RETURNING id",
                                (call.from_user.id, ch_id, stype, final_txt, data.get("photo_id"))
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
# СОХРАНЁННЫЕ НИКИ ПУБЛИКАТОРОВ (до 5 штук на человека)
# ----------------------------------------------------------------------
PN_LIMIT = 5
PN_PENDING: dict = {}   # token -> (uid, nick, rbx_id, ts)

def _pn_profile(nick: str, rbx_id) -> str:
    if rbx_id:
        return f"<a href='https://www.roblox.com/users/{rbx_id}/profile'>{html.escape(nick)}</a>"
    return f"<code>{html.escape(nick)}</code>"

async def _pn_list(uid: int) -> list:
    try:
        return await _db_all("SELECT id, nick, rbx_id FROM publisher_nicks WHERE user_id = %s ORDER BY last_used DESC", (uid,))
    except Exception as e:
        logging.warning(f"Не удалось получить сохранённые ники: {e}")
        return []

def _pn_kb(rows: list, kind: str):
    if not rows:
        return None
    kb = [[IB(text=f"👤 {r[1]}", callback_data=f"pn_use:{kind}:{r[0]}"),
           IB(text="🗑", callback_data=f"pn_del:{kind}:{r[0]}")] for r in rows]
    return InlineKeyboardMarkup(inline_keyboard=kb)

async def _pn_prompt(target: Message, uid: int, kind: str, title: str):
    rows = await _pn_list(uid)
    if rows:
        text = (f"{title}\n\n1. Введите ваш <b>ник в Roblox</b> или выберите сохранённый ниже "
                "(🗑 удаляет ник из списка):")
    else:
        text = f"{title}\n\n1. Введите ваш <b>ник в Roblox</b>:"
    await target.answer(text, reply_markup=_pn_kb(rows, kind), parse_mode="HTML")

async def _pn_offer_save(target: Message, uid: int, nick: str, rbx_id):
    """После нового проверенного ника спрашивает, сохранить ли его на будущее."""
    if not rbx_id:
        return
    try:
        if await _db_one("SELECT 1 FROM publisher_nicks WHERE user_id = %s AND nick_lower = %s", (uid, nick.lower())):
            await _db_exec("UPDATE publisher_nicks SET last_used = CURRENT_TIMESTAMP WHERE user_id = %s AND nick_lower = %s", (uid, nick.lower()))
            return
    except Exception as e:
        logging.warning(f"Сохранённые ники: {e}")
        return
    now = time.time()
    for k in [k for k, v in PN_PENDING.items() if now - v[3] > 3600]:
        PN_PENDING.pop(k, None)
    token = uuid.uuid4().hex[:8]
    PN_PENDING[token] = (uid, nick, rbx_id, now)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        IB(text="💾 Сохранить", callback_data=f"pn_save:{token}"),
        IB(text="Не сохранять", callback_data=f"pn_no:{token}"),
    ]])
    await target.answer(f"💾 Сохранить ник <b>{html.escape(nick)}</b> на будущее? В следующий раз его можно будет выбрать одной кнопкой.",
                        reply_markup=kb, parse_mode="HTML")

@router.callback_query(F.data.startswith("pn_save:"))
async def pn_save_cb(call: CallbackQuery):
    token = call.data.split(":", 1)[1]
    item = PN_PENDING.get(token)
    if not item or item[0] != call.from_user.id:
        await call.answer("Предложение устарело.", show_alert=True)
        try: await call.message.delete()
        except Exception: pass
        return
    uid, nick, rbx_id, _ = item
    rows = await _pn_list(uid)
    if len(rows) >= PN_LIMIT:
        oldest = rows[-1]
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            IB(text=f"Заменить «{oldest[1]}»", callback_data=f"pn_rep:{token}"),
            IB(text="Не сохранять", callback_data=f"pn_no:{token}"),
        ]])
        await call.message.edit_text(
            f"У вас уже {PN_LIMIT} сохранённых ников (максимум). Заменить самый давний — <b>{html.escape(oldest[1])}</b> — на <b>{html.escape(nick)}</b>?",
            reply_markup=kb, parse_mode="HTML")
        await call.answer()
        return
    await _db_exec("INSERT INTO publisher_nicks (user_id, nick, nick_lower, rbx_id) VALUES (%s,%s,%s,%s) "
                   "ON CONFLICT (user_id, nick_lower) DO UPDATE SET last_used = CURRENT_TIMESTAMP", (uid, nick, nick.lower(), rbx_id))
    PN_PENDING.pop(token, None)
    await call.message.edit_text(f"{ICON_CHECK} Ник <b>{html.escape(nick)}</b> сохранён.", parse_mode="HTML")
    await call.answer()

@router.callback_query(F.data.startswith("pn_rep:"))
async def pn_replace_cb(call: CallbackQuery):
    token = call.data.split(":", 1)[1]
    item = PN_PENDING.get(token)
    if not item or item[0] != call.from_user.id:
        await call.answer("Предложение устарело.", show_alert=True)
        return
    uid, nick, rbx_id, _ = item
    rows = await _pn_list(uid)
    if len(rows) >= PN_LIMIT:
        await _db_exec("DELETE FROM publisher_nicks WHERE id = %s AND user_id = %s", (rows[-1][0], uid))
    await _db_exec("INSERT INTO publisher_nicks (user_id, nick, nick_lower, rbx_id) VALUES (%s,%s,%s,%s) "
                   "ON CONFLICT (user_id, nick_lower) DO UPDATE SET last_used = CURRENT_TIMESTAMP", (uid, nick, nick.lower(), rbx_id))
    PN_PENDING.pop(token, None)
    await call.message.edit_text(f"{ICON_CHECK} Ник <b>{html.escape(nick)}</b> сохранён, самый давний удалён.", parse_mode="HTML")
    await call.answer()

@router.callback_query(F.data.startswith("pn_no:"))
async def pn_no_cb(call: CallbackQuery):
    PN_PENDING.pop(call.data.split(":", 1)[1], None)
    try: await call.message.delete()
    except Exception: pass
    await call.answer("Не сохраняю.")

@router.callback_query(F.data.startswith("pn_del:"))
async def pn_del_cb(call: CallbackQuery):
    try:
        _, kind, rid = call.data.split(":")
        rid = int(rid)
    except ValueError:
        await call.answer(); return
    await _db_exec("DELETE FROM publisher_nicks WHERE id = %s AND user_id = %s", (rid, call.from_user.id))
    rows = await _pn_list(call.from_user.id)
    try:
        await call.message.edit_reply_markup(reply_markup=_pn_kb(rows, kind))
    except Exception:
        pass
    await call.answer("Ник удалён из списка.")

@router.callback_query(F.data.startswith("pn_use:"))
async def pn_use_cb(call: CallbackQuery, state: FSMContext):
    try:
        _, kind, rid = call.data.split(":")
        rid = int(rid)
    except ValueError:
        await call.answer(); return
    cur = await state.get_state()
    want = {"sab": SaboteurPublisherStates.waiting_for_nick.state, "farm": FarmPublisherStates.waiting_for_nick.state}.get(kind)
    if cur != want:
        await call.answer("Эта кнопка уже неактуальна. Начните публикацию заново.", show_alert=True)
        return
    row = await _db_one("SELECT nick, rbx_id FROM publisher_nicks WHERE id = %s AND user_id = %s", (rid, call.from_user.id))
    if not row:
        await call.answer("Ник уже удалён.", show_alert=True); return
    await _db_exec("UPDATE publisher_nicks SET last_used = CURRENT_TIMESTAMP WHERE id = %s", (rid,))
    await call.answer()
    if kind == "sab":
        await _sab_nick_accept(call.message, state, row[0], row[1])
    else:
        await _farm_nick_accept(call.message, state, row[0], row[1])

# ----------------------------------------------------------------------
# СИСТЕМА ПУБЛИКАЦИИ САБОТЕРА (ЖИВОЙ ПОСТ + ДИНАМИЧЕСКАЯ СКЛЕЙКА)
# ----------------------------------------------------------------------
@router.callback_query(F.data == "pub_send_saboteur")
async def pub_send_saboteur(call: CallbackQuery, state: FSMContext):
    if not await is_secret_publisher(call.from_user.id): return
    if await _pub_paused_alert(call): return
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
    await _pn_prompt(call.message, call.from_user.id, "sab", "🕵️ <b>Поиск саботера</b>")
    await call.answer()

async def _sab_nick_accept(msg: Message, state: FSMContext, official_name: str, rbx_id):
    profile_str = _pn_profile(official_name, rbx_id)
    await state.update_data(sab_nick=profile_str, sab_raw_nick=official_name)
    await state.set_state(SaboteurPublisherStates.waiting_for_method)
    await msg.answer(
        f"{ICON_CHECK} Ник зафиксирован: {profile_str}\n\n"
        "2. Введите <b>метод поиска</b> (разрешены только цифры и символы <code>/ \\ | -</code>, например: <code>4/20</code> или <code>4/40</code>):",
        parse_mode="HTML",
        link_preview_options=LinkPreviewOptions(is_disabled=True)
    )

@router.message(SaboteurPublisherStates.waiting_for_nick, F.text)
async def saboteur_nick_proc(message: Message, state: FSMContext):
    nick_input = message.text.strip()
    rbx_info = await check_roblox_username(nick_input)
    name = rbx_info["name"] if rbx_info else nick_input
    rid = rbx_info["id"] if rbx_info else None
    await _sab_nick_accept(message, state, name, rid)
    await _pn_offer_save(message, message.from_user.id, name, rid)

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

# ----------------------------------------------------------------------
# ОТЛОЖЕННАЯ ПУБЛИКАЦИЯ (саботёр, фарм, исследователь): сразу / 1 / 3 / 5 минут
# ----------------------------------------------------------------------
_PENDING_DELAYED: dict = {}   # token -> (task, owner_id)
_DELAY_TITLES = {"sab": "сервер саботера", "farm": "фарм сервер", "res": "бейдж исследователя"}

def delay_kb(kind: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [IB(text="⚡ Сразу", callback_data=f"dly_{kind}_0")],
        [IB(text="⏱ Через 1 минуту", callback_data=f"dly_{kind}_1"),
         IB(text="⏱ Через 3 минуты", callback_data=f"dly_{kind}_3")],
        [IB(text="⏱ Через 5 минут", callback_data=f"dly_{kind}_5")],
        [IB(text="❌ Отмена", callback_data=f"dly_{kind}_x")],
    ])

async def _sab_core(user, data, ch_id) -> tuple:
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
                    """, (post_id, user.id, data["sab_nick"], data["sab_tg"], data["sab_link"], data["sab_method"], data["sab_friends"]))
                    return cur.fetchone()[0]
        await asyncio.to_thread(_save_srv)

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
        f"👤 Опубликовал: {user.mention_html()}\n\n"
        f"• Ник Roblox: {data['sab_nick']}\n"
        f"• Метод: <code>{html.escape(data['sab_method'])}</code>\n"
        f"• В друзья: <b>{data['sab_friends']}</b>\n"
        f"• Ссылка: {data['sab_link']}"
    )
    try:
        if not TEST_MODE_ON:
            _sm = await bot.send_message(
                chat_id=ADMIN_CHAT_ID, text=staff_sab_text, message_thread_id=TOPIC_SECRETS_ID,
                parse_mode="HTML", link_preview_options=LinkPreviewOptions(is_disabled=True)
            )
            await plog("saboteur", user.id, _sm.message_id)
    except Exception as ex_st:
        logging.warning(f"Ошибка карточки стаффу (саботер): {ex_st}")
    await call_staff(f"<b>Новый сервер саботера!</b> У вас {STAFF_DELAY_SECONDS} сек. приоритета до предссылок (канал через {CHANNEL_DELAY_SECONDS} сек.)!", exclude_ids=[user.id])
    spawn(publish_saboteur_delayed(ch_id, post_id, delay=CHANNEL_DELAY_SECONDS))
    return True, (
        f"{ICON_CHECK} <b>Сервер саботера принят!</b>\n\n"
        f"⏳ Админы созваны, пост в канале <b>{ch_id}</b> выйдет/обновится ровно через {CHANNEL_DELAY_SECONDS} сек.\n"
        "Управлять своими серверами можно через меню <b>«Мои сервера саботера»</b>."
    )

async def _farm_core(user, data, ch_id) -> tuple:
    async with farm_lock:
        if not await is_main_admin(user.id):
            if await count_user_active_farms(user.id) >= 1:
                return False, "❌ У вас уже есть активный фарм сервер!"
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
                        INSERT INTO farm_servers (post_id, user_id, roblox_nick, tg_user, sections_count, link, accepts_friends, effects_allowed, is_active)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, TRUE) RETURNING id
                    """, (post_id, user.id, data["farm_nick"], data["farm_tg"], data["farm_sections"], data["farm_link"], data["farm_friends"], bool(data.get("farm_effects"))))
                    return cur.fetchone()[0]
        await asyncio.to_thread(_save_srv)

    spawn(send_prelinks(
        _prelink_header("фарм сервер") +
        f"• Ник Roblox: {data['farm_nick']}\n"
        f"• Секций: <b>{data['farm_sections']}</b>\n"
        f"• В друзья: <b>{data['farm_friends']}</b>\n"
        f"• Эффекты: <b>{'разрешены' if data.get('farm_effects') else 'нет'}</b>\n"
        f"• Ссылка: {html.escape(data['farm_link'], quote=False)}"
    ))
    staff_text = (
        f"⚡️ <b>ФАРМ СЕРВЕР ДЛЯ СТАФФА (Фора {CHANNEL_DELAY_SECONDS} сек.)</b> ⚡️\n"
        f"<i>Сервер появится в канале через {CHANNEL_DELAY_SECONDS} секунд! Предссылки получат его через {STAFF_DELAY_SECONDS} сек.</i>\n"
        f"👤 Опубликовал: {user.mention_html()}\n\n"
        f"• Ник Roblox: {data['farm_nick']}\n"
        f"• Секций: <b>{data['farm_sections']}</b>\n"
        f"• В друзья: <b>{data['farm_friends']}</b>\n"
        f"• Эффекты: <b>{'разрешены' if data.get('farm_effects') else 'нет'}</b>\n"
        f"• Ссылка: {html.escape(data['farm_link'], quote=False)}"
    )
    try:
        if not TEST_MODE_ON:
            _sm = await bot.send_message(
                chat_id=ADMIN_CHAT_ID, text=staff_text, message_thread_id=TOPIC_SECRETS_ID,
                parse_mode="HTML", link_preview_options=LinkPreviewOptions(is_disabled=True)
            )
            await plog("farm", user.id, _sm.message_id)
    except Exception as ex_st:
        logging.warning(f"Ошибка карточки стаффу (фарм): {ex_st}")
    await call_staff(f"<b>Новый фарм сервер!</b> У вас {STAFF_DELAY_SECONDS} сек. приоритета до предссылок (канал через {CHANNEL_DELAY_SECONDS} сек.)!", exclude_ids=[user.id])
    spawn(publish_farm_delayed(ch_id, post_id, user.id, user.mention_html(), delay=CHANNEL_DELAY_SECONDS))
    return True, (
        f"{ICON_CHECK} <b>Фарм сервер принят!</b>\n\n"
        f"⏳ Админы созваны, пост в канале <b>{ch_id}</b> выйдет/обновится ровно через {CHANNEL_DELAY_SECONDS} сек.\n"
        "Управлять своими серверами можно через меню <b>«Мои сервера фарма»</b>."
    )

async def _res_core(user, data, ch_id) -> tuple:
    link = data["res_link"]
    text = await render_researcher_active(link)
    has_photo = os.path.exists(RESEARCHER_PHOTO)
    if not has_photo:
        logging.warning(f"Файл {RESEARCHER_PHOTO} не найден, бейдж исследователя публикуется без фото.")
    try:
        if userbot:
            target_peer = _channel_peer(ch_id)
            parsed_text, entities = await parse_telethon_custom_emojis(userbot, text)
            if has_photo:
                sent = await _ub_send_photo("[Исследователь]", target_peer, RESEARCHER_PHOTO, parsed_text, entities)
            else:
                sent = await _ub_send_text("[Исследователь]", target_peer, parsed_text, entities)
            msg_id = sent.id
        else:
            if has_photo:
                sent = await bot.send_photo(chat_id=ch_id, photo=FSInputFile(RESEARCHER_PHOTO), caption=text, parse_mode="HTML")
            else:
                sent = await bot.send_message(chat_id=ch_id, text=text, parse_mode="HTML", link_preview_options=LinkPreviewOptions(is_disabled=True))
            msg_id = sent.message_id

        def _save():
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO researcher_badges (user_id, channel_id, message_id, link) VALUES (%s, %s, %s, %s)",
                        (user.id, ch_id, msg_id, link)
                    )
        await asyncio.to_thread(_save)
        return True, (
            f"{ICON_CHECK} <b>Бейдж исследователь опубликован в канале!</b>\n"
            "Когда он перестанет быть актуальным, закройте его в «📋 Мои бейджи исследователя»."
        )
    except Exception as e:
        logging.error(f"Ошибка публикации бейджа исследователя: {e}", exc_info=True)
        return False, f"{ICON_CROSS} Ошибка публикации в канал: {html.escape(str(e))}"

async def _publish_core(kind: str, user, data: dict, ch_id) -> tuple:
    if _pub_paused():
        return False, PAUSED_HTML      # в т. ч. для отложенных постов, которые «созрели» уже во время паузы
    if kind == "sab":
        return await _sab_core(user, data, ch_id)
    if kind == "farm":
        return await _farm_core(user, data, ch_id)
    return await _res_core(user, data, ch_id)

class _StoredUser:
    """Минимальная замена aiogram User для публикации, восстановленной из БД после перезапуска."""
    def __init__(self, uid: int, full_name: str = "", username: str | None = None):
        self.id = uid
        self.full_name = full_name or str(uid)
        self.first_name = self.full_name
        self.username = username

    def mention_html(self, name: str | None = None) -> str:
        return f'<a href="tg://user?id={self.id}">{html.escape(name or self.full_name)}</a>'

def _dly_save_sync(token, kind, user, data, minutes, chat_id, msg_id):
    import json
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO delayed_publishes (token, kind, user_id, user_name, user_username, data, run_at, chat_id, msg_id, status) "
                "VALUES (%s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP + (%s * INTERVAL '1 minute'), %s, %s, 'pending')",
                (token, kind, user.id, getattr(user, "full_name", None), getattr(user, "username", None),
                 json.dumps(data, ensure_ascii=False, default=str), int(minutes), chat_id, msg_id)
            )

def _dly_set_status_sync(token, new, old=None):
    with get_db() as conn:
        with conn.cursor() as cur:
            if old:
                cur.execute("UPDATE delayed_publishes SET status = %s WHERE token = %s AND status = %s RETURNING token", (new, token, old))
                return cur.fetchone() is not None
            cur.execute("UPDATE delayed_publishes SET status = %s WHERE token = %s", (new, token))
            return True

async def _delayed_publish(kind: str, user, data: dict, wait_seconds: float, token: str, chat_id: int, msg_id: int):
    try:
        await asyncio.sleep(max(0.0, wait_seconds))
    except asyncio.CancelledError:
        return      # отмену пользователя уже записал обработчик; при остановке бота запись остаётся «pending»
    _PENDING_DELAYED.pop(token, None)
    try:
        taken = await asyncio.to_thread(_dly_set_status_sync, token, "running", "pending")
    except Exception as e:
        logging.error(f"Не удалось взять отложенную публикацию {token}: {e}")
        taken = True
    if not taken:
        return      # отменена или уже взята
    try:
        ch_id = await check_channel_rights(bot, user.id)
        if not ch_id:
            ok, text = False, f"{ICON_CROSS} Канал не привязан, пост не опубликован."
        else:
            ok, text = await _publish_core(kind, user, data, ch_id)
    except Exception as e:
        logging.error(f"Ошибка отложенной публикации ({kind}): {e}", exc_info=True)
        ok, text = False, f"{ICON_CROSS} Ошибка публикации: {html.escape(str(e))}"
    try:
        await asyncio.to_thread(_dly_set_status_sync, token, "done")
    except Exception:
        pass
    try:
        await bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=text, reply_markup=None, parse_mode="HTML")
    except Exception:
        try:
            await bot.send_message(chat_id, text, parse_mode="HTML")
        except Exception:
            pass

async def _restore_delayed_publishes():
    """После перезапуска бота возвращает в работу отложенные публикации (1/3/5 минут) и «фору» постов в канал."""
    import json
    try:
        def _load():
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM delayed_publishes WHERE status <> 'pending' AND run_at < CURRENT_TIMESTAMP - INTERVAL '2 days'")
                    cur.execute(
                        "SELECT token, kind, user_id, user_name, user_username, data, chat_id, msg_id, "
                        "EXTRACT(EPOCH FROM (run_at - CURRENT_TIMESTAMP)) FROM delayed_publishes WHERE status = 'pending'"
                    )
                    return cur.fetchall()
        rows = await asyncio.to_thread(_load)
    except Exception as e:
        logging.error(f"Не удалось прочитать отложенные публикации: {e}", exc_info=True)
        rows = []
    for token, kind, uid, uname, uusername, data_s, chat_id, msg_id, remaining in rows:
        remaining = float(remaining or 0)
        if remaining < -PUBLISH_MAX_LATE:
            await asyncio.to_thread(_dly_set_status_sync, token, "expired")
            try:
                await bot.send_message(
                    chat_id,
                    f"{ICON_CROSS} Бот перезапускался, и ваша отложенная публикация не успела выйти. "
                    "Ссылка могла устареть, поэтому пост не опубликован. Отправьте его заново.",
                    parse_mode="HTML"
                )
            except Exception:
                pass
            continue
        try:
            data = json.loads(data_s or "{}")
        except Exception:
            data = {}
        user = _StoredUser(uid, uname or "", uusername)
        task = spawn(_delayed_publish(kind, user, data, remaining, token, chat_id, msg_id))
        _PENDING_DELAYED[token] = (task, uid)
        logging.info(f"Восстановлена отложенная публикация {token} ({kind}), выйдет через {max(0, int(remaining))} сек.")

    # фора перед выходом в канал (секретки, саботёр, фарм)
    for table in _DUE_TABLES:
        try:
            def _q(t=table):
                with get_db() as conn:
                    with conn.cursor() as cur:
                        extra = " AND (message_id IS NULL OR message_id = 0)" if t == "secret_posts" else ""
                        sel = "id, channel_id, EXTRACT(EPOCH FROM (publish_due - CURRENT_TIMESTAMP))"
                        if t == "secret_posts":
                            sel += ", photo_id, base_text"
                        cur.execute(f"SELECT {sel} FROM {t} WHERE publish_due IS NOT NULL{extra}")
                        return cur.fetchall()
            found = await asyncio.to_thread(_q)
        except Exception as e:
            logging.error(f"Не удалось прочитать очередь публикаций {table}: {e}", exc_info=True)
            continue
        for row in found:
            post_id, ch_id, remaining = row[0], row[1], float(row[2] or 0)
            if remaining < -PUBLISH_MAX_LATE:
                await _clear_due(table, post_id)
                logging.warning(f"{table} #{post_id}: просрочена на {int(-remaining)} сек., публикация пропущена.")
                continue
            delay = max(0, int(remaining))
            if table == "secret_posts":
                if not row[3]:
                    await _clear_due(table, post_id)
                    continue
                spawn(publish_to_channel_delayed(ch_id, row[3], row[4] or "", post_id, delay=delay))
            elif table == "saboteur_posts":
                spawn(publish_saboteur_delayed(ch_id, post_id, delay=delay))
            else:
                def _pub():
                    with get_db() as conn:
                        with conn.cursor() as cur:
                            cur.execute("SELECT user_id FROM farm_servers WHERE post_id = %s ORDER BY id DESC LIMIT 1", (post_id,))
                            r = cur.fetchone()
                            return r[0] if r else 0
                pub_id = await asyncio.to_thread(_pub)
                spawn(publish_farm_delayed(ch_id, post_id, pub_id, f'<a href="tg://user?id={pub_id}">{pub_id}</a>', delay=delay))
            logging.info(f"Восстановлена публикация {table} #{post_id}, выйдет через {delay} сек.")

async def _ask_delay(call: CallbackQuery, state: FSMContext, kind: str, need_key: str):
    if await _pub_paused_alert(call): return
    data = await state.get_data()
    if not data or need_key not in data:
        await call.answer()
        return
    st = {"sab": SaboteurPublisherStates, "farm": FarmPublisherStates, "res": ResearcherPublisherStates}[kind].waiting_for_delay
    await state.set_state(st)
    await call.message.edit_text(
        f"⏱ <b>Когда опубликовать {_DELAY_TITLES[kind]}?</b>\n\n"
        "Выберите «Сразу» или отложите публикацию. До выхода поста ссылку никто не увидит, отложенный пост можно отменить.",
        reply_markup=delay_kb(kind), parse_mode="HTML"
    )
    await call.answer()

@router.callback_query(
    StateFilter(SaboteurPublisherStates.waiting_for_delay, FarmPublisherStates.waiting_for_delay, ResearcherPublisherStates.waiting_for_delay),
    F.data.startswith("dly_")
)
async def delay_choice_proc(call: CallbackQuery, state: FSMContext):
    parts = call.data.split("_")
    if len(parts) != 3 or parts[1] not in _DELAY_TITLES:
        await call.answer()
        return
    kind, val = parts[1], parts[2]
    if val == "x":
        await state.clear()
        await call.message.edit_text(f"{ICON_CROSS} Публикация отменена.", parse_mode="HTML")
        await call.answer()
        return
    if await _pub_paused_alert(call): return
    try:
        minutes = int(val)
    except ValueError:
        await call.answer()
        return
    data = await state.get_data()
    await state.clear()
    user = call.from_user
    ch_id = await check_channel_rights(bot, user.id)
    if not ch_id:
        await call.message.edit_text(f"{ICON_CROSS} Канал не привязан!", parse_mode="HTML")
        await call.answer()
        return
    if minutes == 0:
        await call.answer()
        ok, text = await _publish_core(kind, user, data, ch_id)
        await call.message.edit_text(text, parse_mode="HTML")
        return
    token = uuid.uuid4().hex[:10]
    try:
        await asyncio.to_thread(_dly_save_sync, token, kind, user, data, minutes, call.message.chat.id, call.message.message_id)
    except Exception as e:
        logging.error(f"Не удалось сохранить отложенную публикацию: {e}", exc_info=True)
    task = spawn(_delayed_publish(kind, user, data, minutes * 60, token, call.message.chat.id, call.message.message_id))
    _PENDING_DELAYED[token] = (task, user.id)
    word = "минуту" if minutes == 1 else ("минуты" if minutes < 5 else "минут")
    await call.message.edit_text(
        f"⏳ <b>Пост выйдет через {minutes} {word}.</b>\nДо этого момента ссылку никто не получит. Передумали, нажмите кнопку ниже.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[IB(text="❌ Отменить публикацию", callback_data=f"dlycancel_{token}")]]),
        parse_mode="HTML"
    )
    await call.answer()

@router.callback_query(F.data.startswith("dlycancel_"))
async def delay_cancel_proc(call: CallbackQuery):
    token = call.data.split("_", 1)[1]
    row = await _db_one("SELECT user_id, status FROM delayed_publishes WHERE token = %s", (token,))
    item = _PENDING_DELAYED.get(token)
    if not row and not item:
        await call.answer("Пост уже опубликован или отменён.", show_alert=True)
        return
    owner_id = row[0] if row else item[1]
    if call.from_user.id != owner_id and not await is_main_admin(call.from_user.id):
        await call.answer("Это не ваш пост.", show_alert=True)
        return
    if row:
        ok = await asyncio.to_thread(_dly_set_status_sync, token, "cancelled", "pending")
        if not ok:
            await call.answer("Пост уже опубликован или отменён.", show_alert=True)
            return
    item = _PENDING_DELAYED.pop(token, None)
    if item:
        item[0].cancel()
    await call.message.edit_text(f"{ICON_CROSS} Отложенная публикация отменена, пост не выйдет.", parse_mode="HTML")
    await call.answer("Отменено")

@router.callback_query(SaboteurPublisherStates.waiting_for_confirm, F.data.startswith("sab_conf_"))
async def saboteur_confirm_proc(call: CallbackQuery, state: FSMContext):
    action = call.data.split("_")[2]
    if action == "no":
        await call.message.edit_text(f"{ICON_CROSS} Добавление сервера саботера отменено.", parse_mode="HTML")
        await state.clear()
        await call.answer()
        return
    await _ask_delay(call, state, "sab", "sab_nick")

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
    uid = call.from_user.id
    is_adm = await is_main_admin(uid)
    if not is_adm and not await is_secret_publisher(uid): return
    parts = call.data.split("_")
    s_id = int(parts[2])
    post_id = int(parts[3])

    async with saboteur_lock:
        def _del_and_check():
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE saboteur_servers SET is_active = FALSE "
                        "WHERE id = %s AND post_id = %s AND (user_id = %s OR %s) AND is_active = TRUE RETURNING user_id",
                        (s_id, post_id, uid, is_adm)
                    )
                    _own = cur.fetchone()
                    if not _own:
                        return None
                    cur.execute("SELECT COUNT(*) FROM saboteur_servers WHERE post_id = %s AND is_active = TRUE", (post_id,))
                    rem_count = cur.fetchone()[0]
                    cur.execute("SELECT channel_id FROM saboteur_posts WHERE id = %s", (post_id,))
                    ch = cur.fetchone()[0]
                    if rem_count == 0:
                        cur.execute("UPDATE saboteur_posts SET is_active = FALSE WHERE id = %s", (post_id,))
                    return rem_count, ch, _own[0]
        _res = await asyncio.to_thread(_del_and_check)
        if _res is None:
            await call.answer("⚠️ Сервер уже снят, не найден или это не ваш сервер!", show_alert=True)
            return
        rem_count, ch_id, owner_uid = _res

        try:
            await publish_or_update_saboteur_channel(ch_id, post_id)
        except Exception as e:
            logging.error(f"Ошибка обновления поста саботера после удаления: {e}")

    await call.message.edit_text(f"{ICON_CHECK} Сервер снят с поиска. Пост в канале обновлён!", parse_mode="HTML")
    await notify_owner_closed(owner_uid, uid, "сервер саботера снят")
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
                    SELECT id, user_id, roblox_nick, tg_user, sections_count, link, accepts_friends, COALESCE(effects_allowed, FALSE)
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
    tmpl_item_plain = (await get_setting("template_farm_live_item", DEFAULT_FARM_LIVE_ITEM)) or DEFAULT_FARM_LIVE_ITEM
    # блок для серверов с разрешёнными эффектами; если шаблон пуст, берётся обычный
    tmpl_item_fx = (await get_setting("template_farm_live_item_fx", DEFAULT_FARM_LIVE_ITEM_FX)) or tmpl_item_plain

    blocks = []
    for idx, (s_id, u_id, r_nick, tg_u, sections, lnk, friends, fx) in enumerate(servers, 1):
        tmpl_item = tmpl_item_fx if fx else tmpl_item_plain
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
        if _pub_paused():
            await _pause_skip_new_post("farm", post_id, deactivate=not post["message_id"])
            return
        sent_msg = await _ub_send_text(f"[Фарм #{post_id}]", target_peer, parsed_text, entities)
        def _upd_mid():
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute("UPDATE farm_posts SET message_id = %s WHERE id = %s", (sent_msg.id, post_id))
        await asyncio.to_thread(_upd_mid)
        logging.info(f"[Фарм #{post_id}] Новый пост опубликован в {channel_id} (msg_id={sent_msg.id}).")

    async def _do():
        if post["message_id"]:
            try:
                await _ub_edit_text(f"[Фарм #{post_id}]", target_peer, post["message_id"], parsed_text, entities)
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
    await _with_due("farm_posts", post_id, delay,
                    lambda: _publish_farm_delayed_inner(channel_id, post_id, publisher_id, publisher_mention, delay))

async def _publish_farm_delayed_inner(channel_id: str, post_id: int, publisher_id: int, publisher_mention: str, delay: int = CHANNEL_DELAY_SECONDS):
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
            await _ub_edit_text("[Мутатор, истёк]", target_peer, message_id, parsed_text, entities)
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
    if await _pub_paused_alert(call): return
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
    if await _pub_paused_alert(call): return

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
                sent = await _ub_send_photo("[Мутатор]", target_peer, MUTATOR_PHOTO, parsed_text, entities)
            else:
                logging.warning(f"Файл {MUTATOR_PHOTO} не найден, мутатор публикуется без фото.")
                sent = await _ub_send_text("[Мутатор]", target_peer, parsed_text, entities)
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

@router.callback_query(F.data == "pub_my_mutators")
async def pub_my_mutators(call: CallbackQuery):
    if not await is_secret_publisher(call.from_user.id): return
    def _q():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, created_at FROM mutator_badges WHERE user_id = %s AND is_expired = FALSE AND message_id != 0 ORDER BY id DESC LIMIT 10",
                    (call.from_user.id,)
                )
                return cur.fetchall()
    rows = await asyncio.to_thread(_q)
    if not rows:
        await call.message.answer("У вас нет активных бейджей мутатора.", parse_mode="HTML")
        await call.answer()
        return
    kb_rows = []
    for b_id, created in rows:
        when = created.astimezone(MSK_TZ).strftime("%d.%m %H:%M") if created else ""
        kb_rows.append([IB(text=f"🔒 Завершить пост от {when} (№{b_id})", callback_data=f"mut_close_{b_id}")])
    await call.message.answer(
        "🧬 <b>Ваши активные бейджи мутатора:</b>\nНажмите, чтобы завершить пост досрочно.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows), parse_mode="HTML"
    )
    await call.answer()

@router.callback_query(F.data.startswith("mut_close_"))
async def mut_close_proc(call: CallbackQuery):
    uid = call.from_user.id
    is_adm = await is_main_admin(uid)
    if not is_adm and not await is_secret_publisher(uid): return
    try:
        b_id = int(call.data.split("_")[2])
    except (IndexError, ValueError):
        await call.answer()
        return
    def _get():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT channel_id, message_id, user_id FROM mutator_badges WHERE id = %s AND (user_id = %s OR %s) AND is_expired = FALSE", (b_id, uid, is_adm))
                return cur.fetchone()
    row = await asyncio.to_thread(_get)
    if not row:
        await call.answer("Этот пост уже завершён или это не ваш пост.", show_alert=True)
        return
    ch_id, mid, owner_uid = row
    ok = await expire_mutator_badge(b_id, ch_id, mid) if mid else True
    if not ok:
        await call.answer("Не удалось изменить пост в канале. Попробуйте ещё раз чуть позже.", show_alert=True)
        return
    def _upd():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE mutator_badges SET is_expired = TRUE WHERE id = %s", (b_id,))
    await asyncio.to_thread(_upd)
    try:
        old_kb = call.message.reply_markup.inline_keyboard if call.message.reply_markup else []
        new_rows = [r for r in old_kb if not any(b.callback_data == call.data for b in r)]
        if new_rows:
            await call.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=new_rows))
        else:
            await call.message.edit_text("Все бейджи мутатора завершены.", parse_mode="HTML")
    except Exception:
        pass
    await notify_owner_closed(owner_uid, uid, "бейдж мутатора")
    await call.answer("Пост завершён.")

# ----------------------------------------------------------------------
# АДМИНКА: ЗАКРЫТИЕ ЧУЖИХ ПОСТОВ (секретки, саботёр, фарм, мутатор, исследователь)
# ----------------------------------------------------------------------
_CLOSE_KINDS = {
    "sec": "🔮 Секретки",
    "sab": "🕵️ Саботёр",
    "farm": "🌾 Фарм серверы",
    "mut": "🧬 Мутатор",
    "res": "🔍 Исследователь",
}

@router.callback_query(F.data == "sec_close_menu")
async def sec_close_menu(call: CallbackQuery):
    if not await is_main_admin(call.from_user.id): return
    rows = [[IB(text=title, callback_data=f"sec_closelist_{k}")] for k, title in _CLOSE_KINDS.items()]
    rows.append([IB(text="🔙 Назад", callback_data="adm_secret_panel")])
    await call.message.edit_text(
        "🛑 <b>Закрыть чужие посты</b>\n\nВыберите, какие активные посты показать (по 15 последних):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML"
    )
    await call.answer()

@router.callback_query(F.data.startswith("sec_closelist_"))
async def sec_close_list(call: CallbackQuery):
    if not await is_main_admin(call.from_user.id): return
    kind = call.data.split("_", 2)[2]
    if kind not in _CLOSE_KINDS:
        await call.answer()
        return
    def _q():
        with get_db() as conn:
            with conn.cursor() as cur:
                if kind == "sec":
                    cur.execute("SELECT id, secret_type, user_id FROM secret_posts WHERE is_expired = FALSE AND message_id != 0 ORDER BY id DESC LIMIT 15")
                elif kind == "sab":
                    cur.execute("""SELECT s.id, s.roblox_nick, s.user_id, s.post_id FROM saboteur_servers s
                                   JOIN saboteur_posts p ON s.post_id = p.id
                                   WHERE s.is_active = TRUE AND p.is_active = TRUE ORDER BY s.id DESC LIMIT 15""")
                elif kind == "farm":
                    cur.execute("""SELECT s.id, s.roblox_nick, s.user_id, s.post_id FROM farm_servers s
                                   JOIN farm_posts p ON s.post_id = p.id
                                   WHERE s.is_active = TRUE AND p.is_active = TRUE ORDER BY s.id DESC LIMIT 15""")
                elif kind == "mut":
                    cur.execute("SELECT id, 'бейдж', user_id FROM mutator_badges WHERE is_expired = FALSE AND message_id != 0 ORDER BY id DESC LIMIT 15")
                else:
                    cur.execute("SELECT id, 'бейдж', user_id FROM researcher_badges WHERE is_closed = FALSE AND message_id != 0 ORDER BY id DESC LIMIT 15")
                return cur.fetchall()
    rows = await asyncio.to_thread(_q)
    back = [IB(text="🔙 Назад", callback_data="sec_close_menu")]
    if not rows:
        await call.message.edit_text(f"{_CLOSE_KINDS[kind]}: активных постов нет.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[back]), parse_mode="HTML")
        await call.answer()
        return
    kb_rows = []
    for r in rows:
        owner = re.sub(r"<[^>]+>", "", html.unescape(await get_user_label(r[2])))
        title = re.sub(r"<[^>]+>", "", str(r[1] or ""))
        if kind == "sec":
            kb_rows.append([IB(text=f"🔒 {title} — {owner}"[:60], callback_data=f"pub_close_{r[0]}")])
        elif kind == "sab":
            kb_rows.append([IB(text=f"❌ {title} — {owner}"[:60], callback_data=f"sab_del_{r[0]}_{r[3]}")])
        elif kind == "farm":
            kb_rows.append([IB(text=f"❌ {title} — {owner}"[:60], callback_data=f"farm_del_{r[0]}_{r[3]}")])
        elif kind == "mut":
            kb_rows.append([IB(text=f"🔒 №{r[0]} — {owner}"[:60], callback_data=f"mut_close_{r[0]}")])
        else:
            kb_rows.append([IB(text=f"🔒 №{r[0]} — {owner}"[:60], callback_data=f"res_close_{r[0]}")])
    kb_rows.append(back)
    await call.message.edit_text(
        f"🛑 <b>{_CLOSE_KINDS[kind]}</b>\nНажмите на пост, чтобы закрыть его в канале. Хозяин поста получит уведомление.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows), parse_mode="HTML"
    )
    await call.answer()

# ----------------------------------------------------------------------
# БЕЙДЖ ИССЛЕДОВАТЕЛЬ: БЕЗ ТАЙМЕРА (закрывается кнопкой искателя)
# ----------------------------------------------------------------------
async def render_researcher_active(link: str) -> str:
    tmpl = (await get_setting("template_researcher_active", DEFAULT_RESEARCHER_ACTIVE)) or DEFAULT_RESEARCHER_ACTIVE
    return tmpl.replace("[Ссылка]", html.escape(link, quote=False))

def _channel_peer(ch_id):
    if isinstance(ch_id, str) and (ch_id.startswith("-100") or ch_id.lstrip("-").isdigit()):
        return int(ch_id)
    return ch_id

async def close_researcher_post(badge_id: int, channel_id: str, message_id: int) -> bool:
    """Меняет пост на закрытый шаблон. True, если запись можно считать закрытой."""
    text = (await get_setting("template_researcher_expired", DEFAULT_RESEARCHER_EXPIRED)) or DEFAULT_RESEARCHER_EXPIRED
    try:
        if userbot:
            if not userbot.is_connected():
                await userbot.connect()
            parsed_text, entities = await parse_telethon_custom_emojis(userbot, text)
            await _ub_edit_text("[Исследователь, истёк]", _channel_peer(channel_id), message_id, parsed_text, entities)
        else:
            try:
                await bot.edit_message_caption(chat_id=channel_id, message_id=message_id, caption=text, parse_mode="HTML")
            except TelegramBadRequest as tb:
                low = str(tb).lower()
                if "no caption" in low or "there is no text" in low or "message can't be edited" in low:
                    await bot.edit_message_text(
                        chat_id=channel_id, message_id=message_id, text=text,
                        parse_mode="HTML", link_preview_options=LinkPreviewOptions(is_disabled=True)
                    )
                else:
                    raise
        return True
    except (MessageNotModifiedError, MessageIdInvalidError):
        return True
    except TelegramBadRequest as tb:
        if "not modified" in str(tb).lower() or "message to edit not found" in str(tb).lower():
            return True
        logging.error(f"[Исследователь #{badge_id}] Не удалось закрыть пост: {tb}")
        return False
    except Exception as e:
        logging.error(f"[Исследователь #{badge_id}] Не удалось закрыть пост: {e}")
        return False

@router.callback_query(F.data == "pub_send_researcher")
async def pub_send_researcher(call: CallbackQuery, state: FSMContext):
    if not await is_secret_publisher(call.from_user.id): return
    if await _pub_paused_alert(call): return
    ch_id = await check_channel_rights(bot, call.from_user.id)
    if not ch_id:
        await call.message.answer(f"{ICON_CROSS} Сначала привяжите канал через меню секреток!", reply_markup=publisher_menu_kb(), parse_mode="HTML")
        await call.answer()
        return
    await state.clear()
    await state.set_state(ResearcherPublisherStates.waiting_for_link)
    await call.message.answer("🔍 <b>Бейдж исследователь</b>\n\nОтправьте <b>ссылку на VIP-сервер</b> Roblox:", parse_mode="HTML")
    await call.answer()

@router.message(ResearcherPublisherStates.waiting_for_link, F.text)
async def researcher_link_proc(message: Message, state: FSMContext):
    clean = re.sub(r'\s+', '', message.text.strip())
    low = clean.lower()
    if not ("roblox.com" in low and ("privateserverlinkcode=" in low or "share?code=" in low)):
        await message.answer(
            f"{ICON_CROSS} <b>Некорректная ссылка на VIP-сервер!</b>\n"
            "Отправьте корректную ссылку на Roblox VIP сервер.",
            parse_mode="HTML"
        )
        return
    await state.update_data(res_link=clean)
    preview = await render_researcher_active(clean)
    conf_kb = InlineKeyboardMarkup(inline_keyboard=[[
        IB(text="✅ Опубликовать", callback_data="res_conf_yes"),
        IB(text="❌ Отмена", callback_data="res_conf_no")
    ]])
    await message.answer("Предпросмотр поста:")
    await message.answer(preview, reply_markup=conf_kb, parse_mode="HTML", link_preview_options=LinkPreviewOptions(is_disabled=True))
    await state.set_state(ResearcherPublisherStates.waiting_for_confirm)

@router.message(ResearcherPublisherStates.waiting_for_link)
async def researcher_link_wrong_type(message: Message):
    await message.answer(f"{ICON_CROSS} Отправьте ссылку на VIP-сервер текстом.", parse_mode="HTML")

@router.callback_query(ResearcherPublisherStates.waiting_for_confirm, F.data.startswith("res_conf_"))
async def researcher_confirm_proc(call: CallbackQuery, state: FSMContext):
    if call.data == "res_conf_no":
        await call.message.edit_text(f"{ICON_CROSS} Публикация бейджа исследователя отменена.", parse_mode="HTML")
        await state.clear()
        await call.answer()
        return
    await _ask_delay(call, state, "res", "res_link")

@router.callback_query(F.data == "pub_my_researchers")
async def pub_my_researchers(call: CallbackQuery):
    if not await is_secret_publisher(call.from_user.id): return
    def _q():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, link, created_at FROM researcher_badges WHERE user_id = %s AND is_closed = FALSE AND message_id != 0 ORDER BY id DESC LIMIT 10",
                    (call.from_user.id,)
                )
                return cur.fetchall()
    rows = await asyncio.to_thread(_q)
    if not rows:
        await call.message.answer("У вас нет активных бейджей исследователя.", parse_mode="HTML")
        await call.answer()
        return
    kb_rows = []
    for b_id, link, created in rows:
        when = created.astimezone(MSK_TZ).strftime("%d.%m %H:%M") if created else ""
        kb_rows.append([IB(text=f"🔒 Закрыть пост от {when} (№{b_id})", callback_data=f"res_close_{b_id}")])
    await call.message.answer(
        "🔍 <b>Ваши активные бейджи исследователя:</b>\nНажмите на пост, чтобы закрыть его в канале.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows), parse_mode="HTML"
    )
    await call.answer()

@router.callback_query(F.data.startswith("res_close_"))
async def res_close_proc(call: CallbackQuery):
    is_adm = await is_main_admin(call.from_user.id)
    if not is_adm and not await is_secret_publisher(call.from_user.id): return
    try:
        b_id = int(call.data.split("_")[2])
    except (IndexError, ValueError):
        await call.answer()
        return
    def _get():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT channel_id, message_id, user_id FROM researcher_badges WHERE id = %s AND (user_id = %s OR %s) AND is_closed = FALSE", (b_id, call.from_user.id, is_adm))
                return cur.fetchone()
    row = await asyncio.to_thread(_get)
    if not row:
        await call.answer("Этот пост уже закрыт.", show_alert=True)
        return
    ch_id, mid, owner_uid = row
    ok = await close_researcher_post(b_id, ch_id, mid)
    if not ok:
        await call.answer("Не удалось изменить пост в канале. Попробуйте ещё раз чуть позже.", show_alert=True)
        return
    def _upd():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE researcher_badges SET is_closed = TRUE WHERE id = %s", (b_id,))
    await asyncio.to_thread(_upd)
    # убираем кнопку закрытого поста из списка
    try:
        old_kb = call.message.reply_markup.inline_keyboard if call.message.reply_markup else []
        new_rows = [r for r in old_kb if not any(b.callback_data == call.data for b in r)]
        if new_rows:
            await call.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=new_rows))
        else:
            await call.message.edit_text("Все ваши бейджи исследователя закрыты.", parse_mode="HTML")
    except Exception:
        pass
    await notify_owner_closed(owner_uid, call.from_user.id, "бейдж исследователя")
    await call.answer("Пост закрыт.")

# ----------------------------------------------------------------------
# ФАРМ СЕРВЕР: ДИАЛОГ ИСКАТЕЛЯ
# ----------------------------------------------------------------------
@router.callback_query(F.data == "pub_send_farm")
async def pub_send_farm(call: CallbackQuery, state: FSMContext):
    if not await is_secret_publisher(call.from_user.id): return
    if await _pub_paused_alert(call): return
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
    await _pn_prompt(call.message, call.from_user.id, "farm", "🌾 <b>Фарм сервер</b>")
    await call.answer()

async def _farm_nick_accept(msg: Message, state: FSMContext, official_name: str, rbx_id):
    profile_str = _pn_profile(official_name, rbx_id)
    await state.update_data(farm_nick=profile_str, farm_raw_nick=official_name)
    await state.set_state(FarmPublisherStates.waiting_for_sections)
    await msg.answer(
        f"{ICON_CHECK} Ник зафиксирован: {profile_str}\n\n"
        "2. Введите <b>количество секций</b> (целое число, например: <code>25</code>):",
        parse_mode="HTML",
        link_preview_options=LinkPreviewOptions(is_disabled=True)
    )

@router.message(FarmPublisherStates.waiting_for_nick, F.text)
async def farm_nick_proc(message: Message, state: FSMContext):
    nick_input = message.text.strip()
    rbx_info = await check_roblox_username(nick_input)
    name = rbx_info["name"] if rbx_info else nick_input
    rid = rbx_info["id"] if rbx_info else None
    await _farm_nick_accept(message, state, name, rid)
    await _pn_offer_save(message, message.from_user.id, name, rid)

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
    user = message.from_user
    tg_display = f"@{user.username}" if user.username else get_user_mention(user)
    await state.update_data(farm_tg=tg_display)
    await state.set_state(FarmPublisherStates.waiting_for_effects)
    fx_kb = InlineKeyboardMarkup(inline_keyboard=[[
        IB(text="✅ Да, разрешаю", callback_data="farm_fx_yes"),
        IB(text="❌ Нет", callback_data="farm_fx_no")
    ]])
    await message.answer(
        f"{ICON_CHECK} Ссылка принята.\n\n5. Разрешаете ли вы использовать <b>эффекты</b> на вашем сервере?",
        reply_markup=fx_kb, parse_mode="HTML"
    )

@router.callback_query(FarmPublisherStates.waiting_for_effects, F.data.in_({"farm_fx_yes", "farm_fx_no"}))
async def farm_effects_proc(call: CallbackQuery, state: FSMContext):
    allowed = call.data == "farm_fx_yes"
    await state.update_data(farm_effects=allowed)
    data = await state.get_data()
    if not data or "farm_link" not in data or "farm_tg" not in data:
        await call.answer()
        return
    tg_display = data["farm_tg"]
    clean = data["farm_link"]

    preview_text = (
        "🌾 <b>Предпросмотр данных вашего фарм сервера:</b>\n\n"
        f"• Искатель: {tg_display}\n"
        f"• Ник в Roblox: {data['farm_nick']}\n"
        f"• Секций: <b>{data['farm_sections']}</b>\n"
        f"• Заявки в друзья: <b>{data['farm_friends']}</b>\n"
        f"• Эффекты: <b>{'разрешены' if allowed else 'нет'}</b>\n"
        f"• Ссылка: {html.escape(clean, quote=False)}\n\n"
        "<i>Подтвердите публикацию:</i>"
    )

    conf_kb = InlineKeyboardMarkup(inline_keyboard=[[
        IB(text="✅ Опубликовать", callback_data="farm_conf_yes"),
        IB(text="❌ Отмена", callback_data="farm_conf_no")
    ]])
    await call.message.edit_text(preview_text, reply_markup=conf_kb, parse_mode="HTML", link_preview_options=LinkPreviewOptions(is_disabled=True))
    await state.set_state(FarmPublisherStates.waiting_for_confirm)
    await call.answer()

@router.callback_query(FarmPublisherStates.waiting_for_confirm, F.data.startswith("farm_conf_"))
async def farm_confirm_proc(call: CallbackQuery, state: FSMContext):
    action = call.data.split("_")[2]
    if action == "no":
        await call.message.edit_text(f"{ICON_CROSS} Добавление фарм сервера отменено.", parse_mode="HTML")
        await state.clear()
        await call.answer()
        return
    data = await state.get_data()
    if not data or "farm_link" not in data or "farm_friends" not in data:
        await call.answer()
        return
    await _ask_delay(call, state, "farm", "farm_link")

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
    is_adm = await is_main_admin(call.from_user.id)
    if not is_adm and not await is_secret_publisher(call.from_user.id): return
    parts = call.data.split("_")
    s_id = int(parts[2])
    post_id = int(parts[3])
    uid = call.from_user.id

    async with farm_lock:
        def _del_and_check():
            with get_db() as conn:
                with conn.cursor() as cur:
                    # Снять можно свой активный сервер; главный админ может снять любой
                    cur.execute(
                        "UPDATE farm_servers SET is_active = FALSE "
                        "WHERE id = %s AND post_id = %s AND (user_id = %s OR %s) AND is_active = TRUE RETURNING user_id",
                        (s_id, post_id, uid, is_adm)
                    )
                    _own = cur.fetchone()
                    if not _own:
                        return None
                    cur.execute("SELECT COUNT(*) FROM farm_servers WHERE post_id = %s AND is_active = TRUE", (post_id,))
                    rem_count = cur.fetchone()[0]
                    cur.execute("SELECT channel_id FROM farm_posts WHERE id = %s", (post_id,))
                    ch = cur.fetchone()[0]
                    if rem_count == 0:
                        cur.execute("UPDATE farm_posts SET is_active = FALSE WHERE id = %s", (post_id,))
                    return rem_count, ch, _own[0]

        res = await asyncio.to_thread(_del_and_check)
        if res is None:
            await call.answer("⚠️ Сервер уже снят, не найден или это не ваш сервер!", show_alert=True)
            return
        rem_count, ch_id, owner_uid = res

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
        await call.message.edit_text(f"{ICON_CHECK} Фарм сервер снят. Пост в канале обновлён!", parse_mode="HTML")
    await notify_owner_closed(owner_uid, uid, "фарм сервер снят")
    await call.answer()

# ----------------------------------------------------------------------
# ПЛАТНЫЕ УСЛУГИ: ОПЛАТА TELEGRAM STARS
# ----------------------------------------------------------------------
SHOP_DEFAULT_CHANNEL = "https://t.me/priceToHSecretss"

async def shop_channel_url() -> str:
    v = (await get_setting("shop_channel_url", SHOP_DEFAULT_CHANNEL)) or SHOP_DEFAULT_CHANNEL
    return v if v.startswith("http") else SHOP_DEFAULT_CHANNEL

def _db_all(sql: str, params=()):
    def _q():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchall()
    return asyncio.to_thread(_q)

def _db_one(sql: str, params=()):
    def _q():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchone()
    return asyncio.to_thread(_q)

def _db_exec(sql: str, params=()):
    def _q():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                try:
                    return cur.fetchone()
                except psycopg2.ProgrammingError:
                    return None
    return asyncio.to_thread(_q)

async def shop_get_services(only_active: bool):
    sql = "SELECT id, title, description, kind, is_active FROM shop_services"
    if only_active:
        sql += " WHERE is_active = TRUE AND EXISTS (SELECT 1 FROM shop_tariffs t WHERE t.service_id = shop_services.id AND t.is_active = TRUE)"
    sql += " ORDER BY sort, id"
    return await _db_all(sql)

async def shop_get_service(sid: int):
    return await _db_one(
        "SELECT id, title, description, kind, is_active, addon_enabled, addon_title, addon_question, addon_stars "
        "FROM shop_services WHERE id = %s", (sid,)
    )

async def shop_get_tariffs(sid: int, only_active: bool):
    sql = "SELECT id, label, stars, days, is_active FROM shop_tariffs WHERE service_id = %s"
    if only_active:
        sql += " AND is_active = TRUE"
    sql += " ORDER BY sort, id"
    return await _db_all(sql, (sid,))

async def shop_get_tariff(tid: int):
    return await _db_one(
        "SELECT t.id, t.service_id, t.label, t.stars, t.days, t.is_active, s.title, s.kind, s.is_active, s.description, "
        "s.addon_enabled, s.addon_title, s.addon_question, s.addon_stars "
        "FROM shop_tariffs t JOIN shop_services s ON s.id = t.service_id WHERE t.id = %s", (tid,)
    )

def _tariff_addon(t):
    """Доп. услуга сервиса из строки shop_get_tariff: (title, question, stars) или None, если выключена/не настроена."""
    if t and t[10] and t[11] and t[12] and t[13] and int(t[13]) > 0 and t[7] != "prelinks":
        return t[11], t[12], int(t[13])
    return None

def _fmt_days(days) -> str:
    return "навсегда" if days is None else f"{days} дн."

# ----------------------------------------------------------------------
# ОБЯЗАТЕЛЬНАЯ ПОДПИСКА НА КАНАЛ + РЕФЕРАЛЬНАЯ СИСТЕМА + КОНКУРСЫ
# ----------------------------------------------------------------------
SUB_CHANNEL = os.getenv("SUB_CHANNEL", "@ToHSecretss")
SUB_CHANNEL_URL = "https://t.me/" + SUB_CHANNEL.lstrip("@")
REF_CONFIRM_MIN = 60        # через сколько минут после подписки приглашение подтверждается
REF_PENDING_DAYS = 7        # сколько дней ждём, пока приглашённый подпишется
REF_STEP = 10               # за каждых столько подтверждённых приглашённых
REF_REWARD_DAYS = 7         # выдаётся столько дней предссылок

# Включатели (по умолчанию ВЫКЛЮЧЕНЫ, включаются кнопками в админке: Рефералка)
_REF_CFG = {"ref": False, "gate": False}

async def _ref_cfg_load():
    try:
        _REF_CFG["ref"] = (await get_setting("ref_enabled", "false")) == "true"
        _REF_CFG["gate"] = (await get_setting("sub_gate_enabled", "false")) == "true"
    except Exception as e:
        logging.error(f"Не удалось прочитать настройки рефералки: {e}")
    logging.info(f"Рефералка: {'ВКЛ' if _REF_CFG['ref'] else 'выкл'}, обязательная подписка: {'ВКЛ' if _REF_CFG['gate'] else 'выкл'}.")

_SUB_CACHE: Dict[int, tuple] = {}
_SUB_ALERT = {"last": 0.0}
_STAFF_CACHE: Dict[int, tuple] = {}
_REF_SEEN: set = set()

async def _sub_infra_alert(reason: str):
    logging.error(f"Проверка подписки на {SUB_CHANNEL} не работает: {reason}")
    now = time.time()
    if OWNER_ID and now - _SUB_ALERT["last"] > 3600:
        _SUB_ALERT["last"] = now
        try:
            await bot.send_message(
                OWNER_ID,
                f"⚠️ <b>Не могу проверить подписку на {html.escape(SUB_CHANNEL)}.</b>\n<code>{html.escape(reason[:300])}</code>\n"
                "Бота я не закрыл: проверка временно пропускается. Убедитесь, что бот администратор канала.",
                parse_mode="HTML"
            )
        except Exception:
            pass

async def check_subscription(uid: int, fresh: bool = False):
    """True: подписан, False: нет, None: проверить не удалось (тогда пускаем, чтобы не запереть бота)."""
    now = time.time()
    c = _SUB_CACHE.get(uid)
    if c and not fresh and now - c[0] < (120 if c[1] else 5):
        return c[1]
    try:
        m = await bot.get_chat_member(SUB_CHANNEL, uid)
        ok = m.status in ("creator", "administrator", "member") or (m.status == "restricted" and bool(getattr(m, "is_member", True)))
    except TelegramRetryAfter:
        return None
    except TelegramBadRequest as e:
        low = str(e).lower()
        if "user not found" in low or "participant" in low:
            ok = False
        else:
            await _sub_infra_alert(str(e))
            return None
    except TelegramForbiddenError as e:
        await _sub_infra_alert(str(e))
        return None
    except Exception as e:
        logging.warning(f"Не удалось проверить подписку {uid}: {e}")
        return None
    if len(_SUB_CACHE) > 100000:
        _SUB_CACHE.clear()
    _SUB_CACHE[uid] = (now, ok)
    return ok

async def _is_staff_cached(uid: int) -> bool:
    now = time.time()
    c = _STAFF_CACHE.get(uid)
    if c and now - c[0] < 300:
        return c[1]
    try:
        v = bool(await is_support_member(uid))
    except Exception:
        v = False
    if len(_STAFF_CACHE) > 50000:
        _STAFF_CACHE.clear()
    _STAFF_CACHE[uid] = (now, v)
    return v

def _gate_text() -> str:
    return (
        "🔒 <b>Чтобы пользоваться ботом, подпишитесь на канал</b>\n\n"
        f"Канал: {html.escape(SUB_CHANNEL)}\nПосле подписки нажмите «Я подписался»."
    )

def _gate_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [IB(text="📢 Подписаться", url=SUB_CHANNEL_URL)],
        [IB(text="✅ Я подписался", callback_data="subchk")],
    ])

async def _ref_on_subscribed(uid: int):
    """Приглашённый подписался: приглашение переходит в «ждёт подтверждения»."""
    try:
        await _db_exec(
            "UPDATE referrals SET status = 'waiting', subscribed_at = CURRENT_TIMESTAMP "
            "WHERE invitee_id = %s AND status = 'pending' RETURNING referrer_id", (uid,)
        )
    except Exception as e:
        logging.error(f"Ошибка отметки подписки приглашённого {uid}: {e}")

class SubscriptionGateMiddleware(BaseMiddleware):
    """Без подписки на канал бот не работает (кроме /start, оплат и стаффа)."""
    def __init__(self):
        self.last_notice: Dict[int, float] = {}

    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if not _REF_CFG["gate"] or user is None or user.is_bot:
            return await handler(event, data)
        if isinstance(event, Message):
            if event.chat.type != "private" or event.successful_payment:
                return await handler(event, data)
            txt = (event.text or "").strip().lower()
            if txt.startswith(("/start", "/paysupport", "/refund")):
                return await handler(event, data)   # /start сам проверяет подписку после записи реферала
        elif isinstance(event, CallbackQuery):
            if event.data == "subchk" or (event.data or "").startswith("weblogin_") or event.message is None or event.message.chat.type != "private":
                return await handler(event, data)
        else:
            return await handler(event, data)
        if await _is_staff_cached(user.id):
            return await handler(event, data)
        ok = await check_subscription(user.id)
        if ok is not False:
            if ok and user.id not in _REF_SEEN:
                if len(_REF_SEEN) > 100000:
                    _REF_SEEN.clear()
                _REF_SEEN.add(user.id)
                spawn(_ref_on_subscribed(user.id))
            return await handler(event, data)
        # не подписан
        now = time.time()
        quiet = now - self.last_notice.get(user.id, 0.0) < 4
        if not quiet:
            self.last_notice[user.id] = now
            if len(self.last_notice) > 5000:
                self.last_notice = {u: t for u, t in self.last_notice.items() if now - t < 60}
        if isinstance(event, CallbackQuery):
            try:
                await event.answer("Сначала подпишитесь на канал.", show_alert=True)
            except Exception:
                pass
            if not quiet:
                try:
                    await bot.send_message(user.id, _gate_text(), reply_markup=_gate_kb(), parse_mode="HTML")
                except Exception:
                    pass
        elif not quiet:
            try:
                await event.answer(_gate_text(), reply_markup=_gate_kb(), parse_mode="HTML")
            except Exception:
                pass
        return

dp.message.middleware(SubscriptionGateMiddleware())
dp.callback_query.middleware(SubscriptionGateMiddleware())

async def _gate_for_start(message: Message) -> bool:
    """Для /start: пропускает подписанных и стафф, остальным показывает просьбу подписаться."""
    uid = message.from_user.id
    if not _REF_CFG["gate"]:
        return True
    if await _is_staff_cached(uid):
        return True
    ok = await check_subscription(uid, fresh=True)
    if ok is False:
        await message.answer(_gate_text(), reply_markup=_gate_kb(), parse_mode="HTML")
        return False
    if ok:
        _REF_SEEN.add(uid)
        await _ref_on_subscribed(uid)
    return True

@router.callback_query(F.data == "subchk")
async def sub_check_cb(call: CallbackQuery, state: FSMContext):
    uid = call.from_user.id
    ok = await check_subscription(uid, fresh=True)
    if ok is False:
        await call.answer("Вы ещё не подписаны на канал.", show_alert=True)
        return
    _REF_SEEN.add(uid)
    await _ref_on_subscribed(uid)
    try:
        await call.message.edit_text(f"{ICON_CHECK} <b>Подписка подтверждена, спасибо!</b>", parse_mode="HTML")
    except Exception:
        pass
    await call.answer()
    # человек пришёл с сайта в конкретный раздел: продолжаем с того места, где он остановился
    pend = _GO_PENDING.pop(uid, None)
    if pend and time.time() - pend[1] < _GO_TTL:
        try:
            fake = call.message.model_copy(update={"from_user": call.from_user}).as_(bot)
            await _go_run(fake, state, pend[0])
            return
        except Exception as e:
            logging.error(f"Не удалось продолжить переход с сайта ({pend[0]}) для {uid}: {e}", exc_info=True)
    try:
        await bot.send_message(uid, "Выберите нужный раздел на клавиатуре ниже.", reply_markup=await main_keyboard(uid))
    except Exception:
        pass

# ---------- реферальная система ----------
async def _ref_register(invitee: int, arg: str):
    """Первый вход по ссылке ref_<id>. Закрепляется за первым пригласившим навсегда."""
    if not _REF_CFG["ref"]:
        return          # рефералка выключена: ссылку принимаем как обычный /start, ничего не записываем
    try:
        m = re.fullmatch(r"\d{5,15}", (arg or "").strip())
        if not m:
            return
        ref = int(m.group(0))
        if ref == invitee:
            return
        if not await _db_one("SELECT 1 FROM users WHERE user_id = %s", (ref,)):
            return
        if await _db_one("SELECT 1 FROM referrals WHERE invitee_id = %s", (invitee,)):
            return
        sub = await check_subscription(invitee, fresh=True)
        status = "already_member" if sub else "pending"      # уже был в канале: никого не привёл
        await _db_exec(
            "INSERT INTO referrals (invitee_id, referrer_id, status) VALUES (%s, %s, %s) ON CONFLICT (invitee_id) DO NOTHING",
            (invitee, ref, status)
        )
        _REF_SEEN.discard(invitee)
    except Exception as e:
        logging.error(f"Ошибка записи реферала: {e}", exc_info=True)

async def _ref_counts(uid: int) -> dict:
    rows = await _db_all("SELECT status, COUNT(*) FROM referrals WHERE referrer_id = %s GROUP BY status", (uid,))
    d = {r[0]: int(r[1]) for r in rows}
    out = {k: d.get(k, 0) for k in ("confirmed", "waiting", "pending", "left", "already_member")}
    rw = await _db_one("SELECT rewards FROM ref_users WHERE user_id = %s", (uid,))
    out["rewards"] = int(rw[0]) if rw else 0
    return out

def _rank_rows(rows):
    """rows: (user_id, count, last_confirmed_at) -> отсортировано: больше приглашённых, при равенстве раньше набравший."""
    return sorted(rows, key=lambda r: (-int(r[1]), r[2] or datetime.max.replace(tzinfo=timezone.utc)))

async def _ref_top(limit=None):
    rows = _rank_rows(await _db_all(
        "SELECT referrer_id, COUNT(*), MAX(confirmed_at) FROM referrals WHERE status = 'confirmed' GROUP BY referrer_id"
    ))
    return rows[:limit] if limit else rows

def _bar(n: int, total: int = REF_STEP, width: int = 10) -> str:
    filled = min(width, int(width * n / total)) if total else 0
    return "▰" * filled + "▱" * (width - filled)

def _grant_prelink_days_sync(uid: int, days: int):
    """Прибавляет дни предссылок. Возвращает (новый срок | None, навсегда_уже_есть)."""
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT expires_at FROM prelink_users WHERE user_id = %s", (uid,))
            row = cur.fetchone()
            if row and row[0] is None:
                return None, True
            now = datetime.now(timezone.utc)
            base = max(now, row[0]) if row else now
            new_exp = base + timedelta(days=days)
            cur.execute(
                "INSERT INTO prelink_users (user_id, expires_at, is_test) VALUES (%s, %s, FALSE) "
                "ON CONFLICT (user_id) DO UPDATE SET expires_at = EXCLUDED.expires_at, is_test = FALSE",
                (uid, new_exp)
            )
            return new_exp, False

async def _ref_after_confirm(ref: int, new_n: int):
    """Сообщает пригласившему о новых подтверждённых и выдаёт награды за каждые REF_STEP."""
    c = await _ref_counts(ref)
    total = c["confirmed"]
    due = total // REF_STEP - c["rewards"]
    left = REF_STEP - total % REF_STEP
    lines = [f"✅ <b>Подтверждённых приглашённых: +{new_n}</b>", f"Всего: <b>{total}</b>. До следующей недели предссылок: <b>{left}</b>."]
    try:
        await bot.send_message(ref, "\n".join(lines), parse_mode="HTML", disable_notification=is_night_time())
    except Exception as e:
        logging.warning(f"Не удалось сообщить {ref} о новых приглашённых: {e}")
    if due > 0:
        await _db_exec(
            "INSERT INTO ref_users (user_id, rewards) VALUES (%s, %s) ON CONFLICT (user_id) DO UPDATE SET rewards = EXCLUDED.rewards",
            (ref, c["rewards"] + due)
        )
        new_exp, forever = None, False
        for _ in range(due):
            new_exp, forever = await asyncio.to_thread(_grant_prelink_days_sync, ref, REF_REWARD_DAYS)
            if forever:
                break
        if forever:
            text = f"🎁 <b>Вы пригласили {total} человек!</b> Награда за {REF_STEP} приглашённых, неделя предссылок, у вас уже навсегда, так что продлевать нечего. Спасибо!"
        else:
            text = (f"🎁 <b>Вы пригласили {total} человек, и это {due} нед. предссылок в подарок!</b>\n"
                    f"Доступ действует: <b>{fmt_prelink_until(new_exp)}</b>")
        try:
            await bot.send_message(ref, text, parse_mode="HTML")
        except Exception as e:
            logging.warning(f"Не удалось сообщить {ref} о награде: {e}")

async def _referral_pass():
    await _db_exec(f"UPDATE referrals SET status = 'expired' WHERE status = 'pending' AND created_at < CURRENT_TIMESTAMP - INTERVAL '{REF_PENDING_DAYS} days'")
    # 1) ждём подписки приглашённого
    for (uid,) in await _db_all(
        "SELECT invitee_id FROM referrals WHERE status = 'pending' "
        "AND (checked_at IS NULL OR checked_at < CURRENT_TIMESTAMP - INTERVAL '5 minutes') ORDER BY created_at LIMIT 100"
    ):
        ok = await check_subscription(uid, fresh=True)
        if ok:
            await _ref_on_subscribed(uid)
        else:
            await _db_exec("UPDATE referrals SET checked_at = CURRENT_TIMESTAMP WHERE invitee_id = %s", (uid,))
        await asyncio.sleep(0.05)
    # 2) подписался и прошёл час: подтверждаем, если всё ещё в канале
    gained: Dict[int, int] = {}
    for inv, ref in await _db_all(
        f"SELECT invitee_id, referrer_id FROM referrals WHERE status = 'waiting' "
        f"AND subscribed_at <= CURRENT_TIMESTAMP - INTERVAL '{REF_CONFIRM_MIN} minutes' ORDER BY subscribed_at LIMIT 100"
    ):
        ok = await check_subscription(inv, fresh=True)
        if ok is None:
            continue
        if ok:
            done = await _db_exec(
                "UPDATE referrals SET status = 'confirmed', confirmed_at = CURRENT_TIMESTAMP "
                "WHERE invitee_id = %s AND status = 'waiting' RETURNING referrer_id", (inv,)
            )
            if done:
                gained[ref] = gained.get(ref, 0) + 1
        else:
            await _db_exec("UPDATE referrals SET status = 'left' WHERE invitee_id = %s AND status = 'waiting'", (inv,))
        await asyncio.sleep(0.05)
    for ref, n in gained.items():
        await _ref_after_confirm(ref, n)

async def referral_worker():
    logging.info("Фоновый воркер рефералки запущен.")
    await asyncio.sleep(50)
    while True:
        try:
            if _REF_CFG["ref"]:
                await _referral_pass()
        except Exception as err:
            logging.error(f"Ошибка в referral_worker: {err}", exc_info=True)
        await asyncio.sleep(60)

async def _ref_user_screen(uid: int):
    uname = await _bot_username()
    link = f"https://t.me/{uname}?start=ref_{uid}" if uname else None
    c = await _ref_counts(uid)
    total = c["confirmed"]
    lines = ["👥 <b>Приглашайте друзей в канал</b>", ""]
    lines.append(f"Каждые <b>{REF_STEP}</b> приглашённых, которые подписались на {html.escape(SUB_CHANNEL)}, дают <b>неделю предссылок</b> в подарок.")
    lines.append("")
    lines.append(f"Подтверждено: <b>{total}</b>   {_bar(total % REF_STEP)} {total % REF_STEP}/{REF_STEP}")
    if c["waiting"]:
        lines.append(f"Ждут подтверждения (подписались, проверка через час): <b>{c['waiting']}</b>")
    if c["pending"]:
        lines.append(f"Перешли, но ещё не подписались: <b>{c['pending']}</b>")
    if c["already_member"]:
        lines.append(f"Уже были в канале (не засчитываются): {c['already_member']}")
    if c["rewards"]:
        lines.append(f"Получено недель предссылок: <b>{c['rewards']}</b>")
    if link:
        lines.append("")
        lines.append(f"Ваша ссылка:\n<code>{html.escape(link)}</code>")
    rows = []
    if link:
        from urllib.parse import quote
        rows.append([IB(text="📤 Поделиться ссылкой", url="https://t.me/share/url?url=" + quote(link) + "&text=" + quote("Заходи в бота и в канал Tower Of Hell Secrets!"))])
    rows.append([IB(text="🔄 Обновить", callback_data="ref_home")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)

@router.message(Command("ref"), F.chat.type == "private")
@router.message(F.text.in_(btn_texts(BTN_REF)), F.chat.type == "private")
async def ref_open(message: Message, state: FSMContext):
    await register_user(message.from_user.id)
    if await is_banned(message.from_user.id): return
    await state.clear()
    if not _REF_CFG["ref"]:
        await message.answer("👥 Реферальная система сейчас отключена. Следите за новостями в канале!")
        return
    text, kb = await _ref_user_screen(message.from_user.id)
    await message.answer(text, reply_markup=kb, parse_mode="HTML", link_preview_options=LinkPreviewOptions(is_disabled=True))

@router.callback_query(F.data == "ref_home")
async def ref_home_cb(call: CallbackQuery):
    if not _REF_CFG["ref"]:
        await call.answer("Реферальная система сейчас отключена.", show_alert=True)
        return
    text, kb = await _ref_user_screen(call.from_user.id)
    try:
        await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML", link_preview_options=LinkPreviewOptions(is_disabled=True))
    except TelegramBadRequest:
        pass
    await call.answer("Обновлено")

# ---------- админка: рефералка ----------
async def _ref_fmt_top(rows, limit=10) -> str:
    if not rows:
        return "пока никого"
    out = []
    for i, r in enumerate(rows[:limit], 1):
        out.append(f"{i}. {await get_user_label(r[0])} (<code>{r[0]}</code>) — <b>{int(r[1])}</b>")
    return "\n".join(out)

async def _refa_screen():
    tot = {r[0]: int(r[1]) for r in await _db_all("SELECT status, COUNT(*) FROM referrals GROUP BY status")}
    lines = ["👥 <b>Рефералка</b>", ""]
    lines.append(f"Рефералка: <b>{'включена' if _REF_CFG['ref'] else 'выключена'}</b> · обязательная подписка: <b>{'включена' if _REF_CFG['gate'] else 'выключена'}</b>")
    lines.append("")
    lines.append(f"Канал: {html.escape(SUB_CHANNEL)}. За каждые {REF_STEP} подтверждённых: {REF_REWARD_DAYS} дн. предссылок. Подтверждение через {REF_CONFIRM_MIN} мин. после подписки.")
    lines.append("")
    lines.append("<b>📊 Статистика</b>")
    lines.append(f"Подтверждено: <b>{tot.get('confirmed', 0)}</b>")
    lines.append(f"Ждут подтверждения: {tot.get('waiting', 0)} · не подписались: {tot.get('pending', 0)} · отписались: {tot.get('left', 0)} · уже были в канале: {tot.get('already_member', 0)}")
    lines.append("")
    lines.append("<b>🏆 Топ-10 по приглашённым</b>")
    lines.append(await _ref_fmt_top(await _ref_top(), 10))
    rows = [
        [IB(text=f"{'🟢' if _REF_CFG['ref'] else '🔴'} Рефералка: {'вкл' if _REF_CFG['ref'] else 'выкл'}", callback_data="refa_tog_ref")],
        [IB(text=f"{'🟢' if _REF_CFG['gate'] else '🔴'} Обязательная подписка: {'вкл' if _REF_CFG['gate'] else 'выкл'}", callback_data="refa_tog_gate")],
        [IB(text="🏆 Топ-30", callback_data="refa_top_all")],
        [IB(text="🔙 Назад", callback_data="back_to_adm")],
    ]
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)

async def _refa_show(call: CallbackQuery, answer_text: str | None = None):
    text, kb = await _refa_screen()
    try:
        await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except TelegramBadRequest:
        pass
    await call.answer(answer_text) if answer_text else await call.answer()

@router.callback_query(F.data == "refa_menu")
async def refa_menu(call: CallbackQuery, state: FSMContext):
    if not await is_main_admin(call.from_user.id): return
    await state.clear()
    await _refa_show(call)

@router.callback_query(F.data == "refa_top_all")
async def refa_top_all(call: CallbackQuery):
    if not await is_main_admin(call.from_user.id): return
    text = "🏆 <b>Топ-30 по приглашённым</b>\n\n" + await _ref_fmt_top(await _ref_top(), 30)
    kb = InlineKeyboardMarkup(inline_keyboard=[[IB(text="🔙 Назад", callback_data="refa_menu")]])
    await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await call.answer()

@router.callback_query(F.data.startswith("refa_tog_"))
async def refa_toggle(call: CallbackQuery):
    if not await is_main_admin(call.from_user.id): return
    which = call.data[len("refa_tog_"):]
    if which == "ref":
        new = not _REF_CFG["ref"]
        await set_setting("ref_enabled", "true" if new else "false")
        _REF_CFG["ref"] = new
    elif which == "gate":
        new = not _REF_CFG["gate"]
        await set_setting("sub_gate_enabled", "true" if new else "false")
        _REF_CFG["gate"] = new
    else:
        await call.answer()
        return
    await _refa_show(call, "Включено" if new else "Выключено")

# ---------- промокоды: общая логика ----------
_PROMO_CODE_RE = re.compile(r"^[A-Z0-9_-]{3,24}$")
_PROMO_FAILS: dict = {}

def _disc(amount, pct) -> int:
    """Цена со скидкой в звёздах (округление к ближайшему, минимум 1 ⭐)."""
    return max(1, (int(amount) * (100 - int(pct)) + 50) // 100)

def _promo_rate_limited(uid: int) -> bool:
    now = time.time()
    lst = [t for t in _PROMO_FAILS.get(uid, []) if now - t < 600]
    _PROMO_FAILS[uid] = lst
    return len(lst) >= 5

def _promo_fail(uid: int):
    _PROMO_FAILS.setdefault(uid, []).append(time.time())

PROMO_HOLD_HOURS = 12        # сколько часов скидка закреплена за человеком после активации
PROMO_REMIND_EVERY = 3       # напоминания на 3, 6 и 9 часу (на 12-м только сообщение, что промокод ушёл)
_PROMO_HOLD_SQL = f"u.activated_at > CURRENT_TIMESTAMP - INTERVAL '{PROMO_HOLD_HOURS} hours'"

_PROMO_VALID_SQL = (
    "p.is_active = TRUE AND (p.expires_at IS NULL OR p.expires_at > CURRENT_TIMESTAMP) "
    "AND (p.service_id IS NULL OR p.service_id = %s) AND (p.tariff_id IS NULL OR p.tariff_id = %s)"
)

async def _best_promo(uid: int, service_id: int, tariff_id: int):
    """Лучший непотраченный промокод пользователя для тарифа: (id, code, percent) или None."""
    return await _db_one(
        "SELECT p.id, p.code, p.percent FROM shop_promo_users u JOIN shop_promos p ON p.id = u.promo_id "
        "WHERE u.user_id = %s AND u.used_at IS NULL AND " + _PROMO_HOLD_SQL + " AND " + _PROMO_VALID_SQL + " "
        "ORDER BY p.percent DESC, p.id LIMIT 1", (uid, service_id, tariff_id)
    )

async def _promo_valid_for(uid: int, promo_id: int, service_id: int, tariff_id: int):
    return await _db_one(
        "SELECT p.id, p.code, p.percent FROM shop_promo_users u JOIN shop_promos p ON p.id = u.promo_id "
        "WHERE u.user_id = %s AND p.id = %s AND u.used_at IS NULL AND " + _PROMO_HOLD_SQL + " AND " + _PROMO_VALID_SQL,
        (uid, promo_id, service_id, tariff_id)
    )

def _promo_deadline(activated_at, expires_at):
    """До какого момента скидка реально доступна: раньше из «закрепление 12 ч» и «срок самого промокода»."""
    d = activated_at + timedelta(hours=PROMO_HOLD_HOURS)
    return min(d, expires_at) if expires_at is not None else d

def _promo_short_lived(expires_at) -> bool:
    """Промокод закончится раньше, чем пройдёт срок закрепления."""
    return expires_at is not None and expires_at - datetime.now(timezone.utc) < timedelta(hours=PROMO_HOLD_HOURS)

def _next_midnight_msk():
    """Ближайшие 00:00 по Москве (в UTC-aware виде)."""
    now_msk = datetime.now(MSK_TZ)
    nxt = (now_msk + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return nxt.astimezone(timezone.utc)

def _redeem_promo_sync(uid: int, code: str, dry: bool = False):
    """Проверка и активация промокода. dry=True только проверяет, ничего не занимая.
    Возвращает (статус, строка_промокода, доп): доп это activated_at для «already» и время конца блокировки для «cooldown»."""
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, percent, service_id, expires_at, max_uses, uses, is_active, for_user FROM shop_promos WHERE code = %s"
                + ("" if dry else " FOR UPDATE"),
                (code,)
            )
            r = cur.fetchone()
            if not r:
                return "notfound", None, None
            pid, pct, sid, exp, mx, uses, active, fuser = r
            if fuser is not None and fuser != uid:
                return "notfound", None, None      # личный промокод: для остальных его как будто нет
            if not active:
                return "inactive", r, None
            now = datetime.now(timezone.utc)
            if exp is not None and exp <= now:
                return "expired", r, None
            cur.execute("SELECT until FROM shop_promo_cooldowns WHERE promo_id = %s AND user_id = %s", (pid, uid))
            c = cur.fetchone()
            if c:
                if c[0] > now:
                    return "cooldown", r, c[0]
                if not dry:
                    cur.execute("DELETE FROM shop_promo_cooldowns WHERE promo_id = %s AND user_id = %s", (pid, uid))
            cur.execute("SELECT used_at, activated_at FROM shop_promo_users WHERE promo_id = %s AND user_id = %s", (pid, uid))
            u = cur.fetchone()
            if u:
                return ("used" if u[0] else "already"), r, u[1]
            if mx is not None and (uses or 0) >= mx:
                return "exhausted", r, None
            if dry:
                return "ok", r, None
            cur.execute("INSERT INTO shop_promo_users (promo_id, user_id, reminders) VALUES (%s, %s, 0)", (pid, uid))
            cur.execute("UPDATE shop_promos SET uses = COALESCE(uses, 0) + 1 WHERE id = %s", (pid,))
            return "ok", r, None

def _revoke_promo_sync(pid: int, uid: int, cooldown: bool = True) -> bool:
    """Снимает неоплаченную активацию старше PROMO_HOLD_HOURS, возвращает её в лимит и (по желанию) блокирует код для человека до 00:00 МСК."""
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM shop_promo_users WHERE promo_id = %s AND user_id = %s AND used_at IS NULL "
                f"AND activated_at <= CURRENT_TIMESTAMP - INTERVAL '{PROMO_HOLD_HOURS} hours' RETURNING 1",
                (pid, uid)
            )
            if not cur.fetchone():
                return False
            cur.execute("UPDATE shop_promos SET uses = GREATEST(COALESCE(uses, 0) - 1, 0) WHERE id = %s", (pid,))
            if cooldown:
                cur.execute(
                    "INSERT INTO shop_promo_cooldowns (promo_id, user_id, until) VALUES (%s, %s, %s) "
                    "ON CONFLICT (promo_id, user_id) DO UPDATE SET until = EXCLUDED.until",
                    (pid, uid, _next_midnight_msk())
                )
            return True

async def _promo_scope_title(service_id, promo_id=None) -> str:
    if service_id is None:
        return "все услуги"
    row = await _db_one("SELECT title FROM shop_services WHERE id = %s", (service_id,))
    title = row[0] if row else "удалённая услуга"
    if promo_id is not None:
        tr = await _db_one("SELECT t.label FROM shop_promos p JOIN shop_tariffs t ON t.id = p.tariff_id WHERE p.id = %s", (promo_id,))
        if tr:
            title += f", тариф «{tr[0]}»"
    return title

def _promo_status_message(res: str, extra=None, r=None) -> str:
    if res == "notfound":
        return f"{ICON_CROSS} Промокод не найден. Проверьте написание."
    if res in ("inactive", "expired"):
        return f"{ICON_CROSS} Этот промокод больше не действует."
    if res == "exhausted":
        return f"{ICON_CROSS} Лимит активаций этого промокода исчерпан."
    if res == "cooldown":
        when = extra.astimezone(MSK_TZ).strftime("%d.%m в %H:%M") if extra else "00:00"
        return (
            f"{ICON_CROSS} Этот промокод был закреплён за вами, но вы не оплатили покупку вовремя, и он вернулся в общий доступ. "
            f"Снова ввести его можно после {when} по МСК. Другие промокоды можно вводить сразу."
        )
    if res == "already":
        left = ""
        if extra:
            dl = _promo_deadline(extra, r[3] if r else None)
            h = (dl - datetime.now(timezone.utc)).total_seconds() / 3600
            left = f" Осталось примерно {max(1, int(h + 0.99))} ч."
        return f"{ICON_CHECK} Этот промокод уже закреплён за вами.{left} Скидка применится при оплате в «💎 Платные услуги»."
    if res == "used":
        return f"{ICON_CROSS} Вы уже использовали этот промокод."
    return f"{ICON_CROSS} Не удалось проверить промокод."

async def _promo_redeem_reply(message: Message, raw: str):
    """Шаг 1: проверяет код и показывает, что он даёт, с выбором «Активировать» / «Отказаться». Ничего не занимает."""
    uid = message.from_user.id
    code = (raw or "").strip().upper()
    if _promo_rate_limited(uid):
        await message.answer(f"{ICON_CROSS} Слишком много неверных попыток. Попробуйте через 10 минут.", parse_mode="HTML")
        return
    if not _PROMO_CODE_RE.match(code):
        # неверный формат подобрать нельзя, поэтому опечатка или нажатая кнопка меню не считаются попыткой подбора
        await message.answer(f"{ICON_CROSS} Промокод не найден. Он состоит из латинских букв и цифр, проверьте написание.", parse_mode="HTML")
        return
    res, r, extra = await asyncio.to_thread(_redeem_promo_sync, uid, code, True)
    if res != "ok":
        if res in ("notfound", "inactive", "expired"):
            _promo_fail(uid)
        await message.answer(_promo_status_message(res, extra, r), parse_mode="HTML")
        return
    scope = await _promo_scope_title(r[2], r[0])
    until = ("до " + r[3].astimezone(MSK_TZ).strftime("%d.%m.%Y %H:%M") + " МСК") if r[3] else "без срока"
    text = (
        f"🎟 <b>Промокод <code>{html.escape(code)}</code></b>\n\n"
        f"Скидка: <b>{r[1]}%</b>\n"
        f"Действует на: <b>{html.escape(scope)}</b>\n"
        f"Срок промокода: {until}\n"
        "Сработает на одну покупку.\n\n"
        + (
            f"Оплатите покупку в «💎 Платные услуги» до окончания срока промокода ({until.replace('до ', '')}), потом скидка пропадёт.\n\n"
            if _promo_short_lived(r[3]) else
            f"После активации скидка закрепляется за вами на <b>{PROMO_HOLD_HOURS} часов</b>: оплатите за это время в «💎 Платные услуги». "
            "Если не успеете, промокод вернётся в общий доступ.\n\n"
        ) +
        "Активировать?"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [IB(text="✅ Активировать", callback_data=f"promo_ok_{r[0]}")],
        [IB(text="❌ Отказаться", callback_data="promo_no")],
    ])
    await message.answer(text, reply_markup=kb, parse_mode="HTML")

@router.callback_query(F.data.startswith("promo_ok_"))
async def promo_confirm(call: CallbackQuery):
    uid = call.from_user.id
    if await is_banned(uid):
        await call.answer()
        return
    try:
        pid = int(call.data.split("_")[2])
    except (IndexError, ValueError):
        await call.answer()
        return
    row = await _db_one("SELECT code FROM shop_promos WHERE id = %s", (pid,))
    if not row:
        await call.message.edit_text(f"{ICON_CROSS} Промокод не найден.", parse_mode="HTML")
        await call.answer()
        return
    res, r, extra = await asyncio.to_thread(_redeem_promo_sync, uid, row[0])
    if res != "ok":
        await call.message.edit_text(_promo_status_message(res, extra, r), parse_mode="HTML")
        await call.answer()
        return
    scope = await _promo_scope_title(r[2], r[0])
    until = ("до " + r[3].astimezone(MSK_TZ).strftime("%d.%m.%Y %H:%M") + " МСК") if r[3] else "без срока"
    await call.message.edit_text(
        f"{ICON_CHECK} <b>Промокод активирован!</b>\n\n"
        f"Скидка: <b>{r[1]}%</b> на: <b>{html.escape(scope)}</b>\n"
        f"Срок промокода: {until}\n\n"
        + (
            f"Оплатите покупку в «💎 Платные услуги» до окончания срока промокода ({until.replace('до ', '')}), скидка применится автоматически и сработает на одну покупку. "
            "После окончания срока она пропадёт."
            if _promo_short_lived(r[3]) else
            f"Скидка закреплена за вами на <b>{PROMO_HOLD_HOURS} часов</b>. Оплатите за это время в «💎 Платные услуги», она применится автоматически и сработает на одну покупку. "
            "Если не успеете, промокод вернётся в общий доступ, а вам его можно будет ввести снова только после 00:00 по МСК."
        ),
        parse_mode="HTML"
    )
    await call.answer("Промокод активирован")

@router.callback_query(F.data == "promo_no")
async def promo_decline(call: CallbackQuery):
    try:
        await call.message.edit_text("Хорошо, промокод не активирован. Его можно ввести позже командой /promocode.")
    except Exception:
        pass
    await call.answer()

# --- фоновая работа: напоминания и откат неоплаченных активаций ---
async def _promo_hold_pass():
    rows = await _db_all(
        "SELECT u.promo_id, u.user_id, u.activated_at, COALESCE(u.reminders, 0), p.code, p.percent, p.service_id, p.expires_at, p.is_active "
        "FROM shop_promo_users u JOIN shop_promos p ON p.id = u.promo_id WHERE u.used_at IS NULL"
    )
    now = datetime.now(timezone.utc)
    last_reminder = PROMO_HOLD_HOURS // PROMO_REMIND_EVERY - 1      # для 12 и 3: напоминания 1, 2, 3 (на 3, 6, 9 часу)
    for pid, uid, act, rem, code, pct, sid, exp, active in rows:
        hours = (now - act).total_seconds() / 3600
        valid = bool(active) and (exp is None or exp > now)
        if hours >= PROMO_HOLD_HOURS:
            done = await asyncio.to_thread(_revoke_promo_sync, pid, uid, valid)
            if done and valid:
                try:
                    await bot.send_message(
                        uid,
                        f"⌛ <b>Промокод <code>{html.escape(code)}</code> больше не ваш.</b>\n\n"
                        f"Вы не оплатили покупку в течение {PROMO_HOLD_HOURS} часов, поэтому он вернулся в общий доступ. "
                        "Ввести его снова можно будет только после 00:00 по МСК. Другие промокоды можно вводить в любой момент.",
                        parse_mode="HTML", disable_notification=is_night_time()
                    )
                except Exception as e:
                    logging.warning(f"Не удалось уведомить {uid} о снятии промокода: {e}")
            continue
        if not valid:
            continue
        due = int(hours // PROMO_REMIND_EVERY)
        if 1 <= due <= last_reminder and due > rem:
            # сколько реально осталось: меньше из закрепления и срока самого промокода
            left_real = (_promo_deadline(act, exp) - now).total_seconds() / 3600
            if left_real < 0.5:
                continue          # скоро всё равно закончится сам, напоминать бессмысленно
            left = max(1, round(left_real))
            await _db_exec(
                "UPDATE shop_promo_users SET reminders = %s WHERE promo_id = %s AND user_id = %s AND used_at IS NULL",
                (due, pid, uid)
            )
            try:
                scope = await _promo_scope_title(sid, pid)
                await bot.send_message(
                    uid,
                    f"🎟 Промокод <code>{html.escape(code)}</code> (скидка <b>{pct}%</b> на: {html.escape(scope)}) закреплён за вами ещё примерно на <b>{left} ч.</b>\n"
                    + ("Оплатите покупку в «💎 Платные услуги» до окончания срока промокода, потом скидка пропадёт."
                     if _promo_short_lived(exp) else
                     "Оплатите покупку в «💎 Платные услуги», иначе он вернётся в общий доступ."),
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[[IB(text="💎 Платные услуги", callback_data="shop_home")]]),
                    parse_mode="HTML", disable_notification=is_night_time()
                )
            except Exception as e:
                logging.warning(f"Не удалось отправить напоминание о промокоде {uid}: {e}")

async def promo_hold_worker():
    logging.info("Фоновый воркер закрепления промокодов запущен.")
    await asyncio.sleep(30)
    while True:
        try:
            await _promo_hold_pass()
        except Exception as err:
            logging.error(f"Ошибка в promo_hold_worker: {err}", exc_info=True)
        await asyncio.sleep(60)

_PROMO_CANCEL_KB = InlineKeyboardMarkup(inline_keyboard=[[IB(text="❌ Отмена", callback_data="shop_promocancel")]])

@router.message(Command("promocode"), F.chat.type == "private")
async def cmd_promocode(message: Message, command: CommandObject, state: FSMContext):
    await register_user(message.from_user.id)
    if await is_banned(message.from_user.id): return
    await state.clear()
    arg = (command.args or "").strip()
    if arg:
        await _promo_redeem_reply(message, arg.split()[0])
        return
    await state.set_state(ShopUserStates.waiting_promo)
    await message.answer("🎟 Отправьте <b>промокод</b> одним сообщением:", reply_markup=_PROMO_CANCEL_KB, parse_mode="HTML")

@router.callback_query(F.data == "shop_promo")
async def shop_promo_button(call: CallbackQuery, state: FSMContext):
    if await is_banned(call.from_user.id):
        await call.answer()
        return
    await state.clear()
    await state.set_state(ShopUserStates.waiting_promo)
    await call.message.answer("🎟 Отправьте <b>промокод</b> одним сообщением:", reply_markup=_PROMO_CANCEL_KB, parse_mode="HTML")
    await call.answer()

@router.callback_query(F.data == "shop_promocancel")
async def shop_promo_cancel(call: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await call.message.edit_text("Ввод промокода отменён.")
    except Exception:
        pass
    await call.answer()

@router.message(ShopUserStates.waiting_promo, F.text)
async def shop_promo_input(message: Message, state: FSMContext):
    await state.clear()
    if message.text.strip().startswith("/"):
        await message.answer("Ввод промокода отменён.")
        return
    await _promo_redeem_reply(message, message.text)

# ---------- пользователь ----------
async def _shop_home_screen():
    services = await shop_get_services(True)
    rows = [[IB(text=f"💎 {sv[1]}", callback_data=f"shop_s_{sv[0]}")] for sv in services]
    if not services:
        text = "💎 <b>Платные услуги</b>\n\nСейчас платных услуг в боте нет. Актуальные цены и способы оплаты смотрите в нашем канале."
        rows.append([IB(text="💳 Другие методы оплаты", url=await shop_channel_url())])
    else:
        text = "💎 <b>Платные услуги</b>\n\nВыберите услугу. Оплата в боте проходит через Telegram Stars ⭐."
        rows.append([IB(text="🎟 Ввести промокод", callback_data="shop_promo")])
    rows.append([IB(text="💖 Поддержать проект", callback_data="donate_menu")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)

@router.message(F.text.in_(btn_texts(BTN_SHOP)), F.chat.type == "private")
async def shop_open(message: Message, state: FSMContext):
    await register_user(message.from_user.id)
    if await is_banned(message.from_user.id): return
    await state.clear()
    text, kb = await _shop_home_screen()
    await message.answer(text, reply_markup=kb, parse_mode="HTML")

@router.callback_query(F.data == "shop_home")
async def shop_home(call: CallbackQuery):
    text, kb = await _shop_home_screen()
    await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await call.answer()

async def _shop_service_screen(uid: int, sid: int):
    """Экран услуги: (текст, клавиатура) или None, если услуга недоступна."""
    sv = await shop_get_service(sid)
    tariffs = await shop_get_tariffs(sid, True) if sv else []
    if not sv or not sv[4] or not tariffs:
        return None
    lines = [f"💎 <b>{html.escape(sv[1])}</b>"]
    if sv[2]:
        lines.append("\n" + html.escape(sv[2]))
    if sv[3] == "prelinks":
        exp_map = await get_prelink_expiry_map()
        if uid in exp_map:
            lines.append(f"\n✅ Ваш доступ сейчас: <b>{fmt_prelink_until(exp_map[uid])}</b>. Покупка продлит его.")
    tpct = {}
    codes = {}
    for t in tariffs:
        pr_t = await _best_promo(uid, sid, t[0])
        tpct[t[0]] = pr_t[2] if pr_t else 0
        if pr_t:
            codes[pr_t[1]] = pr_t[2]
    lines.append("\n<b>Тарифы:</b>")
    for t in tariffs:
        pct = tpct[t[0]]
        if pct:
            lines.append(f"• {html.escape(t[1])}: <s>{t[2]}</s> <b>{_disc(t[2], pct)} ⭐</b>")
        else:
            lines.append(f"• {html.escape(t[1])}: {t[2]} ⭐")
    for code_, pct_ in codes.items():
        lines.append(f"\n🎟 Промокод <code>{html.escape(code_)}</code>: скидка <b>{pct_}%</b>")
    rows = [[IB(text=(f"{t[1]} — {_disc(t[2], tpct[t[0]])} ⭐ (−{tpct[t[0]]}%)" if tpct[t[0]] else f"{t[1]} — {t[2]} ⭐"), callback_data=f"shop_buy_{t[0]}")] for t in tariffs]
    rows.append([IB(text="🎟 Ввести промокод", callback_data="shop_promo")])
    rows.append([IB(text="💳 Другие методы оплаты", url=await shop_channel_url())])
    rows.append([IB(text="🔙 Назад", callback_data="shop_home")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)

@router.callback_query(F.data.startswith("shop_s_"))
async def shop_service_view(call: CallbackQuery):
    if await is_banned(call.from_user.id):
        await call.answer()
        return
    sid = int(call.data.split("_")[2])
    screen = await _shop_service_screen(call.from_user.id, sid)
    if not screen:
        await call.answer("Эта услуга сейчас недоступна.", show_alert=True)
        return
    await call.message.edit_text(screen[0], reply_markup=screen[1], parse_mode="HTML")
    await call.answer()

async def _user_has_forever_prelinks(uid: int) -> bool:
    row = await _db_one("SELECT expires_at FROM prelink_users WHERE user_id = %s", (uid,))
    return bool(row) and row[0] is None

async def _send_shop_invoice(chat_id: int, t, addon_taken: bool, promo=None):
    ad = _tariff_addon(t) if addon_taken else None
    desc = (f"{t[2]}" + (f". {t[9]}" if t[9] else ""))[:255] or t[2]
    # Stars (XTR): Telegram принимает в счёте ровно одну строку цены, поэтому допуслуга и скидка входят в общую сумму
    price_label = f"{t[6]}: {t[2]}"
    amount = int(t[3])
    if ad:
        desc = (f"{t[2]} + {ad[0]}" + (f". {t[9]}" if t[9] else ""))[:255]
        price_label = f"{t[2]} + {ad[0]}"
        amount += ad[2]
    promo_id = 0
    if promo:
        amount = _disc(amount, promo[2])
        promo_id = promo[0]
        price_label = f"{price_label} (-{promo[2]}%)"
        desc = (desc + f" Промокод {promo[1]}: скидка {promo[2]}%.")[:255]
    await bot.send_invoice(
        chat_id=chat_id,
        title=t[6][:32],
        description=desc,
        payload=f"shop:{t[0]}:{1 if ad else 0}:{promo_id}",
        currency="XTR",
        prices=[LabeledPrice(label=price_label[:60], amount=amount)],
        provider_token="",
    )

@router.callback_query(F.data.startswith("shop_buy_"))
async def shop_buy(call: CallbackQuery):
    if await is_banned(call.from_user.id):
        await call.answer()
        return
    tid = int(call.data.split("_")[2])
    t = await shop_get_tariff(tid)
    # t: id, service_id, label, stars, days, t_active, s_title, kind, s_active, s_desc, addon_enabled, addon_title, addon_question, addon_stars
    if not t or not t[5] or not t[8]:
        await call.answer("Этот тариф сейчас недоступен.", show_alert=True)
        return
    if t[7] == "prelinks" and await _user_has_forever_prelinks(call.from_user.id):
        await call.answer("У вас уже есть Предссылки навсегда, покупка не нужна.", show_alert=True)
        return
    promo = await _best_promo(call.from_user.id, t[1], t[0])
    pct = promo[2] if promo else 0
    ad = _tariff_addon(t)
    if ad:
        # допуслуга: сначала спрашиваем, потом счёт
        base = int(t[3])
        tot_no = _disc(base, pct) if pct else base
        tot_yes = _disc(base + ad[2], pct) if pct else base + ad[2]
        delta = tot_yes - tot_no
        promo_line = f"\n🎟 Промокод <code>{html.escape(promo[1])}</code>: скидка {pct}%" if promo else ""
        text = (
            f"💎 <b>{html.escape(t[6])}</b> — {html.escape(t[2])}: <b>{tot_no} ⭐</b>{promo_line}\n\n"
            f"{html.escape(ad[1])}\n\n"
            f"➕ <b>{html.escape(ad[0])}</b>: +{delta} ⭐ (итого {tot_yes} ⭐)"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [IB(text=f"✅ Да, {ad[0]} (+{delta} ⭐)"[:60], callback_data=f"shop_ad_{tid}_1")],
            [IB(text=f"➡️ Нет, только {t[2]} ({tot_no} ⭐)"[:60], callback_data=f"shop_ad_{tid}_0")],
            [IB(text="🔙 Назад", callback_data=f"shop_s_{t[1]}")],
        ])
        await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        await call.answer()
        return
    try:
        await _send_shop_invoice(call.from_user.id, t, False, promo)
    except Exception as e:
        logging.error(f"Не удалось выставить счёт Stars: {e}")
        await call.answer("Не удалось выставить счёт. Попробуйте позже.", show_alert=True)
        return
    await call.answer("Счёт отправлен ⬇️")

@router.callback_query(F.data.startswith("shop_ad_"))
async def shop_addon_choice(call: CallbackQuery):
    if await is_banned(call.from_user.id):
        await call.answer()
        return
    try:
        _, _, tid_s, choice = call.data.split("_")
        tid = int(tid_s)
    except ValueError:
        await call.answer()
        return
    t = await shop_get_tariff(tid)
    if not t or not t[5] or not t[8]:
        await call.answer("Этот тариф сейчас недоступен.", show_alert=True)
        return
    take = choice == "1"
    if take and not _tariff_addon(t):
        await call.answer("Эта опция сейчас недоступна. Откройте услугу заново.", show_alert=True)
        return
    promo = await _best_promo(call.from_user.id, t[1], t[0])
    try:
        await _send_shop_invoice(call.from_user.id, t, take, promo)
    except Exception as e:
        logging.error(f"Не удалось выставить счёт Stars (допуслуга): {e}")
        await call.answer("Не удалось выставить счёт. Попробуйте позже.", show_alert=True)
        return
    await call.answer("Счёт отправлен ⬇️")

@router.pre_checkout_query()
async def shop_pre_checkout(q: PreCheckoutQuery):
    try:
        if (q.invoice_payload or "").startswith("donate:") and q.currency == "XTR":
            try:
                want = int(q.invoice_payload.split(":")[1])
            except (IndexError, ValueError):
                want = -1
            if want == int(q.total_amount) and DONATE_MIN <= want <= DONATE_MAX:
                await q.answer(ok=True)
            else:
                await q.answer(ok=False, error_message="Сумма доната не совпадает. Откройте меню заново.")
            return
        if not (q.invoice_payload or "").startswith("shop:") or q.currency != "XTR":
            await q.answer(ok=False, error_message="Неизвестный счёт.")
            return
        parts = q.invoice_payload.split(":")
        tid = int(parts[1])
        addon_flag = len(parts) > 2 and parts[2] == "1"
        promo_id = int(parts[3]) if len(parts) > 3 else 0
        t = await shop_get_tariff(tid)
        if not t or not t[5] or not t[8]:
            await q.answer(ok=False, error_message="Эта услуга больше недоступна.")
            return
        expected = int(t[3])
        if addon_flag:
            ad = _tariff_addon(t)
            if not ad:
                await q.answer(ok=False, error_message="Эта опция больше недоступна. Откройте услугу заново.")
                return
            expected += ad[2]
        if promo_id:
            pr = await _promo_valid_for(q.from_user.id, promo_id, t[1], t[0])
            if not pr:
                await q.answer(ok=False, error_message="Промокод больше недействителен. Откройте услугу заново.")
                return
            expected = _disc(expected, pr[2])
        if expected != int(q.total_amount):
            await q.answer(ok=False, error_message="Цена изменилась. Откройте услугу заново.")
            return
        if t[7] == "prelinks" and await _user_has_forever_prelinks(q.from_user.id):
            await q.answer(ok=False, error_message="У вас уже есть Предссылки навсегда.")
            return
        await q.answer(ok=True)
    except Exception as e:
        logging.error(f"Ошибка pre_checkout: {e}")
        try:
            await q.answer(ok=False, error_message="Техническая ошибка, попробуйте позже.")
        except Exception:
            pass

async def get_main_admin_ids() -> list:
    ids = set()
    if OWNER_ID:
        ids.add(OWNER_ID)
    try:
        rows = await _db_all("SELECT admin_id FROM main_admins")
        ids.update(r[0] for r in rows if r[0])
    except Exception as e:
        logging.error(f"Не удалось прочитать главных админов: {e}")
    return list(ids)

async def notify_admins_payment(text: str):
    for aid in await get_main_admin_ids():
        try:
            await bot.send_message(aid, text, parse_mode="HTML")
        except Exception as e:
            logging.warning(f"Не удалось отправить уведомление об оплате админу {aid}: {e}")

async def grant_prelinks_days(uid: int, days) -> object:
    """Выдаёт/продлевает Предссылки. days=None: навсегда. Возвращает новую дату окончания (None = навсегда)."""
    row = await _db_one("SELECT expires_at FROM prelink_users WHERE user_id = %s", (uid,))
    now = datetime.now(timezone.utc)
    if days is None:
        new_exp = None
    else:
        base = now
        if row and row[0] is not None and row[0] > now:
            base = row[0]
        new_exp = base + timedelta(days=int(days))
        if row and row[0] is None:
            new_exp = None   # уже навсегда
    await _db_exec(
        "INSERT INTO prelink_users (user_id, expires_at, is_test) VALUES (%s, %s, FALSE) "
        "ON CONFLICT (user_id) DO UPDATE SET expires_at = EXCLUDED.expires_at, is_test = FALSE",
        (uid, new_exp)
    )
    return new_exp

# ---------- «Спасибо проекту»: добровольные донаты Stars ----------
DONATE_MIN, DONATE_MAX = 1, 10000
DONATE_PRESETS = (5, 10, 15)

def _donate_kb():
    row = [IB(text=f"{n} ⭐", callback_data=f"don_{n}") for n in DONATE_PRESETS]
    return InlineKeyboardMarkup(inline_keyboard=[row, [IB(text="✏️ Своя сумма", callback_data="donate_custom")],
                                                 [IB(text="🔙 Назад", callback_data="shop_home")]])

_DONATE_TEXT = ("💖 <b>Поддержать проект</b>\n\n"
                "Это добровольный донат Telegram Stars: он ничего не покупает и не даёт доступа, это просто «спасибо» команде. "
                "Выберите сумму или введите свою.")

@router.callback_query(F.data == "donate_menu")
async def donate_menu(call: CallbackQuery, state: FSMContext):
    if await is_banned(call.from_user.id):
        await call.answer(); return
    await state.clear()
    await call.message.edit_text(_DONATE_TEXT, reply_markup=_donate_kb(), parse_mode="HTML")
    await call.answer()

@router.message(Command("donate"), F.chat.type == "private")
async def cmd_donate(message: Message, state: FSMContext):
    if await is_banned(message.from_user.id): return
    await state.clear()
    await message.answer(_DONATE_TEXT, reply_markup=_donate_kb(), parse_mode="HTML")

async def _send_donate_invoice(chat_id: int, amount: int):
    await bot.send_invoice(
        chat_id=chat_id, title="Поддержка проекта", description="Добровольный донат команде. Спасибо! 💖",
        payload=f"donate:{amount}", currency="XTR", prices=[LabeledPrice(label="Донат", amount=amount)], provider_token="",
    )

@router.callback_query(F.data.regexp(r"^don_\d+$"))
async def donate_preset(call: CallbackQuery):
    if await is_banned(call.from_user.id):
        await call.answer(); return
    n = int(call.data.split("_")[1])
    if n not in DONATE_PRESETS:
        await call.answer(); return
    await call.answer()
    await _send_donate_invoice(call.message.chat.id, n)

@router.callback_query(F.data == "donate_custom")
async def donate_custom(call: CallbackQuery, state: FSMContext):
    if await is_banned(call.from_user.id):
        await call.answer(); return
    await state.set_state(ShopUserStates.waiting_donate)
    kb = InlineKeyboardMarkup(inline_keyboard=[[IB(text="🔙 Отмена", callback_data="donate_menu")]])
    await call.message.edit_text(f"✏️ Введите сумму в звёздах целым числом (от {DONATE_MIN} до {DONATE_MAX}).", reply_markup=kb, parse_mode="HTML")
    await call.answer()

@router.message(ShopUserStates.waiting_donate, F.text, F.chat.type == "private")
async def donate_custom_amount(message: Message, state: FSMContext):
    t = (message.text or "").strip().replace(" ", "")
    if not re.fullmatch(r"\d{1,5}", t) or not (DONATE_MIN <= int(t) <= DONATE_MAX):
        await message.answer(f"{ICON_CROSS} Нужно целое число от {DONATE_MIN} до {DONATE_MAX}. Попробуйте ещё раз:", parse_mode="HTML")
        return
    await state.clear()
    await _send_donate_invoice(message.chat.id, int(t))

async def _process_donation(message: Message, sp, is_test: bool):
    user = message.from_user
    charge_id = sp.telegram_payment_charge_id
    try:
        row = await _db_exec(
            "INSERT INTO shop_payments (user_id, service_id, tariff_id, service_title, tariff_label, stars, charge_id, status, kind) "
            "VALUES (%s, NULL, NULL, %s, %s, %s, %s, %s, 'donation') ON CONFLICT (charge_id) DO NOTHING RETURNING id",
            (user.id, "Поддержка проекта", "Донат", sp.total_amount, charge_id, "test" if is_test else "paid"))
    except Exception as e:
        logging.error(f"Не удалось записать донат {charge_id}: {e}")
        await notify_admins_payment(f"🚨 <b>Донат не записался в базу!</b>\n{get_user_mention(user)} (<code>{user.id}</code>), {sp.total_amount} ⭐\n"
                                    f"Платёж: <code>{html.escape(charge_id)}</code>")
        await message.answer("💖 Спасибо за поддержку!", parse_mode="HTML")
        return
    if not row:
        return
    await message.answer(f"💖 <b>Спасибо за поддержку!</b>\nВаш донат: <b>{sp.total_amount} ⭐</b>. Нам очень приятно.", parse_mode="HTML")
    who = f"{get_user_mention(user)}" + (f" (@{user.username})" if user.username else "") + f", ID <code>{user.id}</code>"
    await notify_admins_payment(f"💖 <b>{'🧪 ТЕСТ. ' if is_test else ''}Донат</b>\n{who}\nСумма: <b>{sp.total_amount} ⭐</b>\nПлатёж: <code>{html.escape(charge_id)}</code>")

@router.message(F.successful_payment)
async def shop_successful_payment(message: Message):
    await _process_shop_payment(message, message.successful_payment, False)

async def _process_shop_payment(message: Message, sp, is_test: bool):
    tag = "🧪 ТЕСТ. " if is_test else ""
    user = message.from_user
    payload = sp.invoice_payload or ""
    if payload.startswith("donate:"):
        await _process_donation(message, sp, is_test)
        return
    charge_id = sp.telegram_payment_charge_id
    addon_flag = False
    promo_id = 0
    try:
        _parts = payload.split(":")
        tid = int(_parts[1]) if payload.startswith("shop:") else 0
        addon_flag = len(_parts) > 2 and _parts[2] == "1"
        promo_id = int(_parts[3]) if len(_parts) > 3 else 0
    except (IndexError, ValueError):
        tid = 0
    t = await shop_get_tariff(tid) if tid else None
    # допуслуга: берём название из услуги, цену из реально оплаченной суммы
    addon_title = None
    addon_stars = 0
    if addon_flag and t:
        addon_title = t[11] or "Доп. услуга"
        _ad_cfg = _tariff_addon(t)
        addon_stars = _ad_cfg[2] if _ad_cfg else max(0, int(sp.total_amount) - int(t[3]))
    promo_code, promo_pct = None, 0
    if promo_id:
        try:
            _pr = await _db_one("SELECT code, percent FROM shop_promos WHERE id = %s", (promo_id,))
            if _pr:
                promo_code, promo_pct = _pr
        except Exception as e:
            logging.error(f"Не удалось прочитать промокод платежа: {e}")
    rec_kind = t[7] if t else None
    rec_grant = t[4] if (t and t[7] == "prelinks") else None
    svc_title = t[6] if t else "удалённая услуга"
    label = t[2] if t else "?"
    sid = t[1] if t else None

    # фиксируем платёж первым делом; дубль по charge_id пропускаем
    try:
        row = await _db_exec(
            "INSERT INTO shop_payments (user_id, service_id, tariff_id, service_title, tariff_label, stars, charge_id, status, addon_title, addon_stars, promo_code, discount_pct, kind, grant_days) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (charge_id) DO NOTHING RETURNING id",
            (user.id, sid, tid or None, svc_title, label, sp.total_amount, charge_id, "test" if is_test else "paid", addon_title, addon_stars, promo_code, promo_pct, rec_kind, rec_grant)
        )
    except Exception as e:
        logging.error(f"Не удалось записать платёж {charge_id}: {e}")
        await notify_admins_payment(
            f"🚨 <b>Оплата Stars не записалась в базу!</b>\nПользователь: {get_user_mention(user)} (<code>{user.id}</code>)\n"
            f"Сумма: {sp.total_amount} ⭐\nПлатёж: <code>{html.escape(charge_id)}</code>\nНужна ручная обработка."
        )
        await message.answer("✅ Оплата получена! Если доступ не появился, напишите администрации, мы всё проверим.", parse_mode="HTML")
        return
    if not row:
        return  # этот платёж уже обработан
    if promo_id:
        try:
            await _db_exec(
                "UPDATE shop_promo_users SET used_at = CURRENT_TIMESTAMP WHERE promo_id = %s AND user_id = %s AND used_at IS NULL",
                (promo_id, user.id)
            )
        except Exception as e:
            logging.error(f"Не удалось отметить промокод использованным: {e}")

    who = f"{get_user_mention(user)}" + (f" (@{user.username})" if user.username else "") + f", ID <code>{user.id}</code>"
    addon_note = f"\n➕ <b>Доп. услуга: {html.escape(addon_title)}</b> (+{addon_stars} ⭐) — <b>ВЗЯТА</b>" if addon_title else ""
    addon_user = f"\nДоп. услуга: <b>{html.escape(addon_title)}</b>." if addon_title else ""
    promo_note = f"\n🎟 Промокод: <b>{html.escape(promo_code)}</b> (−{promo_pct}%)" if promo_code else ""
    if t and t[7] == "prelinks":
        try:
            new_exp = await grant_prelinks_days(user.id, t[4])
            until = fmt_prelink_until(new_exp)
            await message.answer(
                f"✅ <b>Оплата получена!</b>\n\nПредссылки подключены. Теперь вы получаете ссылки на серверы в личные сообщения раньше канала.\n"
                f"⏳ Доступ действует: <b>{until}</b>",
                parse_mode="HTML"
            )
            await notify_admins_payment(
                f"💎 <b>{tag}Оплата Stars</b>\n{who}\nУслуга: <b>{html.escape(svc_title)}</b> — {html.escape(label)}\n"
                f"Сумма: <b>{sp.total_amount} ⭐</b>{promo_note}\nПредссылки выданы автоматически ({until}).\nПлатёж: <code>{html.escape(charge_id)}</code>"
            )
        except Exception as e:
            logging.error(f"Не удалось выдать Предссылки после оплаты {charge_id}: {e}", exc_info=True)
            await message.answer("✅ Оплата получена! Доступ будет выдан администрацией в ближайшее время.", parse_mode="HTML")
            await notify_admins_payment(
                f"🚨 <b>Оплата Предссылок прошла, но выдача не удалась!</b>\n{who}\n{html.escape(label)}, {sp.total_amount} ⭐\n"
                f"Выдайте вручную. Платёж: <code>{html.escape(charge_id)}</code>"
            )
    else:
        await message.answer(
            f"✅ <b>Оплата получена!</b>\n\nУслуга: <b>{html.escape(svc_title)}</b> — {html.escape(label)}.{addon_user}\n"
            "Администрация скоро свяжется с вами. Если не напишем в течение суток, обратитесь через «Задать вопрос».",
            parse_mode="HTML"
        )
        await notify_admins_payment(
            f"💎 <b>{tag}Оплата Stars</b>\n{who}\nУслуга: <b>{html.escape(svc_title)}</b> — {html.escape(label)}\n"
            f"Сумма: <b>{sp.total_amount} ⭐</b>{addon_note}{promo_note}\n⚠️ Услуга не привязана к боту: нужно выдать вручную.\nПлатёж: <code>{html.escape(charge_id)}</code>"
        )

@router.message(Command("testpay"), F.chat.type == "private")
async def cmd_testpay(message: Message, command: CommandObject):
    """Только для владельца: имитация успешной оплаты без звёзд (вся цепочка выдачи и уведомлений)."""
    if message.from_user.id != OWNER_ID:
        return
    args = (command.args or "").split()
    arg = args[0] if args else ""
    want_addon = "1" in args[1:]
    want_promo = "promo" in [a.lower() for a in args[1:]]
    if not arg.isdigit():
        rows = await _db_all(
            "SELECT t.id, s.title, t.label, t.stars, s.kind FROM shop_tariffs t JOIN shop_services s ON s.id = t.service_id ORDER BY s.sort, s.id, t.sort, t.id"
        )
        lines = [f"<code>/testpay {r[0]}</code> — {html.escape(r[1])}: {html.escape(r[2])}, {r[3]} ⭐" + (" (Предссылки, выдадутся вам)" if r[4] == "prelinks" else "") for r in rows]
        lines.append("\nС допуслугой: <code>/testpay НОМЕР 1</code> (если у услуги она включена).")
        lines.append("С вашим промокодом: добавьте слово <code>promo</code>, например <code>/testpay НОМЕР promo</code>.")
        await message.answer(
            "🧪 <b>Тест оплаты без звёзд</b>\nИмитирует успешную оплату выбранного тарифа: запись в историю, выдача (для Предссылок), уведомления вам и админам.\n\n"
            + ("\n".join(lines) if lines else "Тарифов пока нет."),
            parse_mode="HTML"
        )
        return
    t = await shop_get_tariff(int(arg))
    if not t:
        await message.answer(f"{ICON_CROSS} Тариф не найден. Список: /testpay", parse_mode="HTML")
        return
    ad = _tariff_addon(t)
    take = bool(want_addon and ad)
    if want_addon and not ad:
        await message.answer("У этой услуги нет включённой допуслуги, проверяю без неё.")
    total = int(t[3]) + (ad[2] if take else 0)
    promo = None
    if want_promo:
        promo = await _best_promo(message.from_user.id, t[1], t[0])
        if not promo:
            await message.answer("У вас нет активированного промокода для этой услуги (сначала /promocode КОД), проверяю без него.")
        else:
            total = _disc(total, promo[2])
    sp = SimpleNamespace(invoice_payload=f"shop:{t[0]}:{1 if take else 0}:{promo[0] if promo else 0}", total_amount=total, telegram_payment_charge_id="TEST-" + uuid.uuid4().hex[:12])
    await _process_shop_payment(message, sp, True)

@router.message(Command("paysupport"), F.chat.type == "private")
async def cmd_paysupport(message: Message):
    url = await shop_channel_url()
    await message.answer(
        "💬 <b>Поддержка по оплатам</b>\n\nВопросы по оплате и возвратам: нажмите «Задать вопрос» в меню бота "
        f"и укажите номер платежа или напишите нам. Цены и другие способы оплаты: {html.escape(url)}",
        parse_mode="HTML", link_preview_options=LinkPreviewOptions(is_disabled=True)
    )

async def revoke_prelinks_days(uid: int, days) -> tuple:
    """Снимает с пользователя дни Предссылок, купленные платежом. Возвращает (текст_для_владельца, изменён_ли_доступ)."""
    row = await _db_one("SELECT expires_at FROM prelink_users WHERE user_id = %s", (uid,))
    if not row:
        return "Предссылок у пользователя уже нет, снимать нечего.", False
    if row[0] is None:
        return "У пользователя Предссылки навсегда (выданы вручную), срок не менял.", False
    now = datetime.now(timezone.utc)
    new_exp = row[0] - timedelta(days=int(days))
    if new_exp <= now:
        await _db_exec("DELETE FROM prelink_users WHERE user_id = %s", (uid,))
        return "Предссылки сняты автоматически.", True
    await _db_exec("UPDATE prelink_users SET expires_at = %s WHERE user_id = %s", (new_exp, uid))
    return f"Срок Предссылок уменьшен на {int(days)} дн., теперь {fmt_prelink_until(new_exp)}.", True

@router.message(Command("refund"), F.chat.type == "private")
async def cmd_refund(message: Message, command: CommandObject):
    if message.from_user.id != OWNER_ID:
        return
    charge = (command.args or "").strip()
    if not charge:
        await message.answer("Использование: <code>/refund ID_платежа</code> (ID виден в уведомлении об оплате и в «Последних оплатах»).", parse_mode="HTML")
        return
    row = await _db_one("SELECT user_id, status, stars, kind, grant_days, tariff_id FROM shop_payments WHERE charge_id = %s", (charge,))
    if not row:
        await message.answer(f"{ICON_CROSS} Платёж не найден.", parse_mode="HTML")
        return
    if row[1] == "test":
        await message.answer("Это тестовый платёж, возвращать нечего.")
        return
    if row[1] == "refunded":
        await message.answer("Этот платёж уже возвращён.")
        return
    try:
        await bot.refund_star_payment(user_id=row[0], telegram_payment_charge_id=charge)
    except Exception as e:
        await message.answer(f"{ICON_CROSS} Возврат не удался: {html.escape(str(e))}", parse_mode="HTML")
        return
    await _db_exec("UPDATE shop_payments SET status = 'refunded' WHERE charge_id = %s", (charge,))

    # автоснятие Предссылок, купленных этим платежом
    kind, gdays, tid = row[3], row[4], row[5]
    if kind is None and tid:   # платежи, записанные до появления этих полей
        try:
            tr = await _db_one("SELECT s.kind, t.days FROM shop_tariffs t JOIN shop_services s ON s.id = t.service_id WHERE t.id = %s", (tid,))
            if tr:
                kind, gdays = tr
        except Exception as e:
            logging.error(f"Не удалось определить тип платежа для возврата: {e}")
    revoke_note, revoked = "", False
    if kind == "prelinks":
        if gdays is None:
            revoke_note = "Тариф был «навсегда»: снимите Предссылки вручную в админке."
        else:
            try:
                revoke_note, revoked = await revoke_prelinks_days(row[0], gdays)
            except Exception as e:
                logging.error(f"Не удалось снять Предссылки после возврата: {e}", exc_info=True)
                revoke_note = "Не удалось снять Предссылки автоматически: снимите вручную в админке."
    await message.answer(
        f"{ICON_CHECK} Возврат {row[2]} ⭐ выполнен." + (f"\n{html.escape(revoke_note)}" if revoke_note else ""),
        parse_mode="HTML"
    )
    try:
        await bot.send_message(
            row[0],
            f"↩️ Оплата {row[2]} ⭐ возвращена на ваш счёт Stars." + ("\nДоступ к Предссылкам, оплаченный этим платежом, снят." if revoked else ""),
            parse_mode="HTML"
        )
    except Exception:
        pass

# ---------- админка ----------
async def _shopa_menu_screen():
    services = await shop_get_services(False)
    rows = []
    for sv in services:
        mark = "🟢" if sv[4] else "⏸"
        rows.append([IB(text=f"{mark} {sv[1]}"[:60], callback_data=f"shopa_svc_{sv[0]}")])
    rows.append([IB(text="➕ Добавить услугу", callback_data="shopa_new")])
    rows.append([IB(text="🎟 Промокоды", callback_data="shopp_menu")])
    rows.append([IB(text="🔗 Ссылка на канал прайса", callback_data="shopa_chan"),
                 IB(text="🧾 Последние оплаты", callback_data="shopa_hist")])
    rows.append([IB(text="🔙 Назад в админку", callback_data="back_to_adm")])
    text = (
        "💎 <b>Платные услуги (Telegram Stars)</b>\n\n"
        "Здесь вы задаёте услуги и цены в звёздах. «Предссылки» выдаются автоматически после оплаты, "
        "остальные услуги только присылают вам уведомление об оплате.\n"
        "🟢 включена, ⏸ выключена или без активных тарифов."
    )
    return text, InlineKeyboardMarkup(inline_keyboard=rows)

@router.callback_query(F.data == "shopa_menu")
async def shopa_menu(call: CallbackQuery, state: FSMContext):
    if not await is_main_admin(call.from_user.id): return
    await state.clear()
    text, kb = await _shopa_menu_screen()
    await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await call.answer()

async def _shopa_service_screen(sid: int):
    sv = await shop_get_service(sid)
    if not sv:
        return None, None
    tariffs = await shop_get_tariffs(sid, False)
    lines = [
        f"💎 <b>{html.escape(sv[1])}</b> {'🟢' if sv[4] else '⏸ выключена'}",
        f"Тип: {'Предссылки (выдаются автоматически)' if sv[3] == 'prelinks' else 'обычная услуга (только уведомление)'}",
        f"Описание: {html.escape(sv[2]) if sv[2] else '—'}",
        "",
        "<b>Тарифы:</b>",
    ]
    rows = []
    if not tariffs:
        lines.append("<i>— пока нет —</i>")
    for t in tariffs:
        extra = f", {_fmt_days(t[3])}" if sv[3] == "prelinks" else ""
        lines.append(f"{'🟢' if t[4] else '⏸'} {html.escape(t[1])}: {t[2]} ⭐{extra}")
        rows.append([IB(text=f"{'🟢' if t[4] else '⏸'} {t[1]} — {t[2]} ⭐"[:60], callback_data=f"shopa_tf_{t[0]}")])
    rows.append([IB(text="➕ Добавить тариф", callback_data=f"shopa_ta_{sid}")])
    rows.append([IB(text="✏ Название", callback_data=f"shopa_et_{sid}"), IB(text="✏ Описание", callback_data=f"shopa_ed_{sid}")])
    rows.append([IB(text="⏯ Вкл/выкл услугу", callback_data=f"shopa_tg_{sid}")])
    if sv[3] != "prelinks":
        if sv[6] and sv[8]:
            ad_state = "🟢" if sv[5] else "⏸"
            ad_btn = f"{ad_state} Допуслуга: {sv[6]}"[:60]
        else:
            ad_btn = "➕ Допуслуга (не настроена)"
        rows.append([IB(text=ad_btn, callback_data=f"shopa_ao_{sid}")])
        rows.append([IB(text="🗑 Удалить услугу", callback_data=f"shopa_dl_{sid}")])
    rows.append([IB(text="🔙 К услугам", callback_data="shopa_menu")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)

async def _show_service(call_or_msg, sid: int, edit: bool = True):
    text, kb = await _shopa_service_screen(sid)
    if not text:
        return
    if edit and isinstance(call_or_msg, CallbackQuery):
        await call_or_msg.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    else:
        msg = call_or_msg.message if isinstance(call_or_msg, CallbackQuery) else call_or_msg
        await msg.answer(text, reply_markup=kb, parse_mode="HTML")

@router.callback_query(F.data.startswith("shopa_svc_"))
async def shopa_service(call: CallbackQuery, state: FSMContext):
    if not await is_main_admin(call.from_user.id): return
    await state.clear()
    await _show_service(call, int(call.data.split("_")[2]))
    await call.answer()

def _shopa_cancel_kb(back: str):
    return InlineKeyboardMarkup(inline_keyboard=[[IB(text="❌ Отменить", callback_data=back)]])

@router.callback_query(F.data == "shopa_new")
async def shopa_new(call: CallbackQuery, state: FSMContext):
    if not await is_main_admin(call.from_user.id): return
    await state.set_state(ShopAdminStates.new_title)
    await call.message.answer("➕ Отправьте <b>название услуги</b> (до 32 символов), например «Реклама в канале»:", reply_markup=_shopa_cancel_kb("shopa_menu"), parse_mode="HTML")
    await call.answer()

@router.message(ShopAdminStates.new_title, F.text)
async def shopa_new_title(message: Message, state: FSMContext):
    if not await is_main_admin(message.from_user.id): return
    title = message.text.strip()
    if not title or len(title) > 32:
        await message.answer(f"{ICON_CROSS} Название должно быть от 1 до 32 символов. Отправьте ещё раз.", parse_mode="HTML")
        return
    await state.update_data(title=title)
    await state.set_state(ShopAdminStates.new_desc)
    await message.answer("Отправьте <b>описание</b> услуги (до 600 символов) или «-», если описание не нужно:", reply_markup=_shopa_cancel_kb("shopa_menu"), parse_mode="HTML")

@router.message(ShopAdminStates.new_desc, F.text)
async def shopa_new_desc(message: Message, state: FSMContext):
    if not await is_main_admin(message.from_user.id): return
    desc = message.text.strip()
    if desc == "-":
        desc = ""
    if len(desc) > 600:
        await message.answer(f"{ICON_CROSS} Описание длиннее 600 символов. Сократите и отправьте ещё раз.", parse_mode="HTML")
        return
    data = await state.get_data()
    await state.clear()
    row = await _db_exec(
        "INSERT INTO shop_services (title, description, kind, is_active, sort) VALUES (%s, %s, 'custom', TRUE, (SELECT COALESCE(MAX(sort), 0) + 1 FROM shop_services)) RETURNING id",
        (data["title"], desc)
    )
    await message.answer(f"{ICON_CHECK} Услуга создана. Теперь добавьте тарифы (цены в звёздах).", parse_mode="HTML")
    await _show_service(message, row[0], edit=False)

@router.callback_query(F.data.startswith("shopa_et_"))
async def shopa_edit_title(call: CallbackQuery, state: FSMContext):
    if not await is_main_admin(call.from_user.id): return
    sid = int(call.data.split("_")[2])
    await state.set_state(ShopAdminStates.edit_title)
    await state.update_data(sid=sid)
    await call.message.answer("✏ Отправьте новое <b>название</b> (до 32 символов):", reply_markup=_shopa_cancel_kb(f"shopa_svc_{sid}"), parse_mode="HTML")
    await call.answer()

@router.message(ShopAdminStates.edit_title, F.text)
async def shopa_edit_title_proc(message: Message, state: FSMContext):
    if not await is_main_admin(message.from_user.id): return
    title = message.text.strip()
    if not title or len(title) > 32:
        await message.answer(f"{ICON_CROSS} Название должно быть от 1 до 32 символов.", parse_mode="HTML")
        return
    sid = (await state.get_data())["sid"]
    await state.clear()
    await _db_exec("UPDATE shop_services SET title = %s WHERE id = %s", (title, sid))
    await _show_service(message, sid, edit=False)

@router.callback_query(F.data.startswith("shopa_ed_"))
async def shopa_edit_desc(call: CallbackQuery, state: FSMContext):
    if not await is_main_admin(call.from_user.id): return
    sid = int(call.data.split("_")[2])
    await state.set_state(ShopAdminStates.edit_desc)
    await state.update_data(sid=sid)
    await call.message.answer("✏ Отправьте новое <b>описание</b> (до 600 символов) или «-», чтобы очистить:", reply_markup=_shopa_cancel_kb(f"shopa_svc_{sid}"), parse_mode="HTML")
    await call.answer()

@router.message(ShopAdminStates.edit_desc, F.text)
async def shopa_edit_desc_proc(message: Message, state: FSMContext):
    if not await is_main_admin(message.from_user.id): return
    desc = message.text.strip()
    if desc == "-":
        desc = ""
    if len(desc) > 600:
        await message.answer(f"{ICON_CROSS} Описание длиннее 600 символов.", parse_mode="HTML")
        return
    sid = (await state.get_data())["sid"]
    await state.clear()
    await _db_exec("UPDATE shop_services SET description = %s WHERE id = %s", (desc, sid))
    await _show_service(message, sid, edit=False)

@router.callback_query(F.data.startswith("shopa_tg_"))
async def shopa_toggle(call: CallbackQuery):
    if not await is_main_admin(call.from_user.id): return
    sid = int(call.data.split("_")[2])
    await _db_exec("UPDATE shop_services SET is_active = NOT is_active WHERE id = %s", (sid,))
    await _show_service(call, sid)
    await call.answer("Готово")

@router.callback_query(F.data.startswith("shopa_dl_"))
async def shopa_delete_ask(call: CallbackQuery):
    if not await is_main_admin(call.from_user.id): return
    sid = int(call.data.split("_")[2])
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        IB(text="🗑 Да, удалить", callback_data=f"shopa_dly_{sid}"),
        IB(text="❌ Нет", callback_data=f"shopa_svc_{sid}")
    ]])
    await call.message.edit_text("Удалить услугу вместе со всеми тарифами? История оплат останется.", reply_markup=kb, parse_mode="HTML")
    await call.answer()

@router.callback_query(F.data.startswith("shopa_dly_"))
async def shopa_delete(call: CallbackQuery):
    if not await is_main_admin(call.from_user.id): return
    sid = int(call.data.split("_")[2])
    await _db_exec("DELETE FROM shop_services WHERE id = %s AND kind <> 'prelinks'", (sid,))
    text, kb = await _shopa_menu_screen()
    await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await call.answer("Удалено")

# тарифы
@router.callback_query(F.data.startswith("shopa_ta_"))
async def shopa_tariff_add(call: CallbackQuery, state: FSMContext):
    if not await is_main_admin(call.from_user.id): return
    sid = int(call.data.split("_")[2])
    await state.set_state(ShopAdminStates.tar_label)
    await state.update_data(sid=sid)
    await call.message.answer(
        "➕ Отправьте <b>название тарифа</b> (до 30 символов), например «Неделя» или «1 день»:",
        reply_markup=_shopa_cancel_kb(f"shopa_svc_{sid}"), parse_mode="HTML"
    )
    await call.answer()

@router.message(ShopAdminStates.tar_label, F.text)
async def shopa_tariff_label(message: Message, state: FSMContext):
    if not await is_main_admin(message.from_user.id): return
    label = message.text.strip()
    if not label or len(label) > 30:
        await message.answer(f"{ICON_CROSS} Название тарифа: от 1 до 30 символов.", parse_mode="HTML")
        return
    await state.update_data(label=label)
    await state.set_state(ShopAdminStates.tar_price)
    await message.answer("Отправьте <b>цену в звёздах</b> целым числом (от 1 до 100000):", parse_mode="HTML")

def _parse_stars(text: str):
    t = (text or "").strip()
    if not t.isdigit():
        return None
    v = int(t)
    return v if 1 <= v <= 100000 else None

@router.message(ShopAdminStates.tar_price, F.text)
async def shopa_tariff_price(message: Message, state: FSMContext):
    if not await is_main_admin(message.from_user.id): return
    stars = _parse_stars(message.text)
    if stars is None:
        await message.answer(f"{ICON_CROSS} Нужно целое число от 1 до 100000.", parse_mode="HTML")
        return
    data = await state.get_data()
    sv = await shop_get_service(data["sid"])
    if not sv:
        await state.clear()
        return
    if sv[3] == "prelinks":
        await state.update_data(stars=stars)
        await state.set_state(ShopAdminStates.tar_days)
        await message.answer("На сколько <b>дней</b> выдавать Предссылки? Отправьте число (например, 7 или 30):", parse_mode="HTML")
        return
    await state.clear()
    await _db_exec(
        "INSERT INTO shop_tariffs (service_id, label, stars, days, is_active, sort) "
        "VALUES (%s, %s, %s, NULL, TRUE, (SELECT COALESCE(MAX(sort), 0) + 1 FROM shop_tariffs WHERE service_id = %s))",
        (data["sid"], data["label"], stars, data["sid"])
    )
    await _show_service(message, data["sid"], edit=False)

@router.message(ShopAdminStates.tar_days, F.text)
async def shopa_tariff_days(message: Message, state: FSMContext):
    if not await is_main_admin(message.from_user.id): return
    t = message.text.strip()
    if t.isdigit() and 1 <= int(t) <= 3650:
        days = int(t)
    else:
        await message.answer(f"{ICON_CROSS} Нужно число дней от 1 до 3650. Предссылки навсегда не продаются.", parse_mode="HTML")
        return
    data = await state.get_data()
    await state.clear()
    await _db_exec(
        "INSERT INTO shop_tariffs (service_id, label, stars, days, is_active, sort) "
        "VALUES (%s, %s, %s, %s, TRUE, (SELECT COALESCE(MAX(sort), 0) + 1 FROM shop_tariffs WHERE service_id = %s))",
        (data["sid"], data["label"], data["stars"], days, data["sid"])
    )
    await _show_service(message, data["sid"], edit=False)

@router.callback_query(F.data.startswith("shopa_tf_"))
async def shopa_tariff_menu(call: CallbackQuery, state: FSMContext):
    if not await is_main_admin(call.from_user.id): return
    await state.clear()
    tid = int(call.data.split("_")[2])
    t = await shop_get_tariff(tid)
    if not t:
        await call.answer("Тариф не найден.", show_alert=True)
        return
    extra = f"\nСрок выдачи: {_fmt_days(t[4])}" if t[7] == "prelinks" else ""
    text = f"✏ <b>{html.escape(t[6])}</b> — тариф «{html.escape(t[2])}»\nЦена: <b>{t[3]} ⭐</b>{extra}\nСтатус: {'🟢 включён' if t[5] else '⏸ выключен'}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [IB(text="💰 Изменить цену", callback_data=f"shopa_tp_{tid}"), IB(text="🏷 Название", callback_data=f"shopa_tl_{tid}")],
        [IB(text="⏯ Вкл/выкл", callback_data=f"shopa_tt_{tid}"), IB(text="🗑 Удалить", callback_data=f"shopa_td_{tid}")],
        [IB(text="🔙 К услуге", callback_data=f"shopa_svc_{t[1]}")],
    ])
    await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await call.answer()

@router.callback_query(F.data.startswith("shopa_tp_"))
async def shopa_tariff_price_ask(call: CallbackQuery, state: FSMContext):
    if not await is_main_admin(call.from_user.id): return
    tid = int(call.data.split("_")[2])
    await state.set_state(ShopAdminStates.tar_edit_price)
    await state.update_data(tid=tid)
    await call.message.answer("💰 Отправьте новую <b>цену в звёздах</b> (целое число). Тариф после этого включится:", reply_markup=_shopa_cancel_kb(f"shopa_tf_{tid}"), parse_mode="HTML")
    await call.answer()

@router.message(ShopAdminStates.tar_edit_price, F.text)
async def shopa_tariff_price_proc(message: Message, state: FSMContext):
    if not await is_main_admin(message.from_user.id): return
    stars = _parse_stars(message.text)
    if stars is None:
        await message.answer(f"{ICON_CROSS} Нужно целое число от 1 до 100000.", parse_mode="HTML")
        return
    tid = (await state.get_data())["tid"]
    await state.clear()
    row = await _db_exec("UPDATE shop_tariffs SET stars = %s, is_active = TRUE WHERE id = %s RETURNING service_id", (stars, tid))
    if row:
        await _show_service(message, row[0], edit=False)

@router.callback_query(F.data.startswith("shopa_tl_"))
async def shopa_tariff_label_ask(call: CallbackQuery, state: FSMContext):
    if not await is_main_admin(call.from_user.id): return
    tid = int(call.data.split("_")[2])
    await state.set_state(ShopAdminStates.tar_edit_label)
    await state.update_data(tid=tid)
    await call.message.answer("🏷 Отправьте новое <b>название тарифа</b> (до 30 символов):", reply_markup=_shopa_cancel_kb(f"shopa_tf_{tid}"), parse_mode="HTML")
    await call.answer()

@router.message(ShopAdminStates.tar_edit_label, F.text)
async def shopa_tariff_label_proc(message: Message, state: FSMContext):
    if not await is_main_admin(message.from_user.id): return
    label = message.text.strip()
    if not label or len(label) > 30:
        await message.answer(f"{ICON_CROSS} Название тарифа: от 1 до 30 символов.", parse_mode="HTML")
        return
    tid = (await state.get_data())["tid"]
    await state.clear()
    row = await _db_exec("UPDATE shop_tariffs SET label = %s WHERE id = %s RETURNING service_id", (label, tid))
    if row:
        await _show_service(message, row[0], edit=False)

@router.callback_query(F.data.startswith("shopa_tt_"))
async def shopa_tariff_toggle(call: CallbackQuery):
    if not await is_main_admin(call.from_user.id): return
    tid = int(call.data.split("_")[2])
    row = await _db_exec("UPDATE shop_tariffs SET is_active = NOT is_active WHERE id = %s RETURNING service_id", (tid,))
    if row:
        await _show_service(call, row[0])
    await call.answer("Готово")

@router.callback_query(F.data.startswith("shopa_td_"))
async def shopa_tariff_delete(call: CallbackQuery):
    if not await is_main_admin(call.from_user.id): return
    tid = int(call.data.split("_")[2])
    row = await _db_exec("DELETE FROM shop_tariffs WHERE id = %s RETURNING service_id", (tid,))
    if row:
        await _show_service(call, row[0])
    await call.answer("Удалено")

# допуслуга (предлагается после выбора тарифа)
async def _shopa_addon_screen(sid: int):
    sv = await shop_get_service(sid)
    if not sv:
        return None, None
    configured = bool(sv[6] and sv[7] and sv[8])
    if configured:
        text = (
            f"➕ <b>Допуслуга для «{html.escape(sv[1])}»</b> {'🟢 включена' if sv[5] else '⏸ выключена'}\n\n"
            f"Название: <b>{html.escape(sv[6])}</b>\n"
            f"Цена: <b>+{sv[8]} ⭐</b> к выбранному тарифу\n"
            f"Вопрос пользователю:\n<i>{html.escape(sv[7])}</i>\n\n"
            "После выбора тарифа бот задаёт этот вопрос с кнопками «Да (+цена)» и «Нет». "
            "В счёт идёт сумма тарифа и допуслуги, а в уведомлении об оплате будет видно, взял ли человек допуслугу."
        )
    else:
        text = (
            f"➕ <b>Допуслуга для «{html.escape(sv[1])}»</b>\n\nПока не настроена. Например, для рекламы это может быть "
            "«Закреп поста» с вопросом «Хотите закрепить пост в канале?». Нажмите «Настроить»: бот спросит название, вопрос и цену."
        )
    rows = [[IB(text="🛠 Настроить заново" if configured else "🛠 Настроить", callback_data=f"shopa_aw_{sid}")]]
    if configured:
        rows.append([IB(text="🏷 Название", callback_data=f"shopa_at_{sid}"), IB(text="❓ Вопрос", callback_data=f"shopa_aq_{sid}")])
        rows.append([IB(text="💰 Цена", callback_data=f"shopa_ap_{sid}"), IB(text="⏯ Вкл/выкл", callback_data=f"shopa_ag_{sid}")])
        rows.append([IB(text="🗑 Убрать допуслугу", callback_data=f"shopa_ax_{sid}")])
    rows.append([IB(text="🔙 К услуге", callback_data=f"shopa_svc_{sid}")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)

async def _show_addon(call_or_msg, sid: int, edit: bool = True):
    text, kb = await _shopa_addon_screen(sid)
    if not text:
        return
    if edit and isinstance(call_or_msg, CallbackQuery):
        await call_or_msg.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    else:
        msg = call_or_msg.message if isinstance(call_or_msg, CallbackQuery) else call_or_msg
        await msg.answer(text, reply_markup=kb, parse_mode="HTML")

async def _shopa_addon_ok_service(sid: int):
    sv = await shop_get_service(sid)
    return sv if sv and sv[3] != "prelinks" else None

@router.callback_query(F.data.startswith("shopa_ao_"))
async def shopa_addon_menu(call: CallbackQuery, state: FSMContext):
    if not await is_main_admin(call.from_user.id): return
    await state.clear()
    sid = int(call.data.split("_")[2])
    if not await _shopa_addon_ok_service(sid):
        await call.answer("Для этой услуги допуслуга недоступна.", show_alert=True)
        return
    await _show_addon(call, sid)
    await call.answer()

@router.callback_query(F.data.startswith("shopa_aw_"))
async def shopa_addon_wizard(call: CallbackQuery, state: FSMContext):
    if not await is_main_admin(call.from_user.id): return
    sid = int(call.data.split("_")[2])
    if not await _shopa_addon_ok_service(sid):
        await call.answer("Для этой услуги допуслуга недоступна.", show_alert=True)
        return
    await state.set_state(ShopAdminStates.ad_title)
    await state.update_data(sid=sid, wizard=True)
    await call.message.answer(
        "🏷 Отправьте <b>название допуслуги</b> (до 25 символов), например «Закреп поста»:",
        reply_markup=_shopa_cancel_kb(f"shopa_ao_{sid}"), parse_mode="HTML"
    )
    await call.answer()

@router.callback_query(F.data.startswith("shopa_at_"))
async def shopa_addon_title_ask(call: CallbackQuery, state: FSMContext):
    if not await is_main_admin(call.from_user.id): return
    sid = int(call.data.split("_")[2])
    await state.set_state(ShopAdminStates.ad_title)
    await state.update_data(sid=sid, wizard=False)
    await call.message.answer("🏷 Отправьте новое <b>название допуслуги</b> (до 25 символов):", reply_markup=_shopa_cancel_kb(f"shopa_ao_{sid}"), parse_mode="HTML")
    await call.answer()

@router.message(ShopAdminStates.ad_title, F.text)
async def shopa_addon_title_proc(message: Message, state: FSMContext):
    if not await is_main_admin(message.from_user.id): return
    title = message.text.strip()
    if not title or len(title) > 25:
        await message.answer(f"{ICON_CROSS} Название: от 1 до 25 символов.", parse_mode="HTML")
        return
    data = await state.get_data()
    sid = data["sid"]
    await _db_exec("UPDATE shop_services SET addon_title = %s WHERE id = %s", (title, sid))
    if data.get("wizard"):
        await state.set_state(ShopAdminStates.ad_question)
        await message.answer(
            "❓ Отправьте <b>вопрос пользователю</b> (до 300 символов), например «Хотите за доплату закрепить пост в канале?»:",
            reply_markup=_shopa_cancel_kb(f"shopa_ao_{sid}"), parse_mode="HTML"
        )
        return
    await state.clear()
    await _show_addon(message, sid, edit=False)

@router.callback_query(F.data.startswith("shopa_aq_"))
async def shopa_addon_question_ask(call: CallbackQuery, state: FSMContext):
    if not await is_main_admin(call.from_user.id): return
    sid = int(call.data.split("_")[2])
    await state.set_state(ShopAdminStates.ad_question)
    await state.update_data(sid=sid, wizard=False)
    await call.message.answer("❓ Отправьте новый <b>вопрос пользователю</b> (до 300 символов):", reply_markup=_shopa_cancel_kb(f"shopa_ao_{sid}"), parse_mode="HTML")
    await call.answer()

@router.message(ShopAdminStates.ad_question, F.text)
async def shopa_addon_question_proc(message: Message, state: FSMContext):
    if not await is_main_admin(message.from_user.id): return
    q = message.text.strip()
    if not q or len(q) > 300:
        await message.answer(f"{ICON_CROSS} Вопрос: от 1 до 300 символов.", parse_mode="HTML")
        return
    data = await state.get_data()
    sid = data["sid"]
    await _db_exec("UPDATE shop_services SET addon_question = %s WHERE id = %s", (q, sid))
    if data.get("wizard"):
        await state.set_state(ShopAdminStates.ad_price)
        await message.answer("💰 Отправьте <b>цену допуслуги в звёздах</b> (целое число), она добавляется к цене тарифа:", reply_markup=_shopa_cancel_kb(f"shopa_ao_{sid}"), parse_mode="HTML")
        return
    await state.clear()
    await _show_addon(message, sid, edit=False)

@router.callback_query(F.data.startswith("shopa_ap_"))
async def shopa_addon_price_ask(call: CallbackQuery, state: FSMContext):
    if not await is_main_admin(call.from_user.id): return
    sid = int(call.data.split("_")[2])
    await state.set_state(ShopAdminStates.ad_price)
    await state.update_data(sid=sid, wizard=False)
    await call.message.answer("💰 Отправьте новую <b>цену допуслуги в звёздах</b> (целое число):", reply_markup=_shopa_cancel_kb(f"shopa_ao_{sid}"), parse_mode="HTML")
    await call.answer()

@router.message(ShopAdminStates.ad_price, F.text)
async def shopa_addon_price_proc(message: Message, state: FSMContext):
    if not await is_main_admin(message.from_user.id): return
    stars = _parse_stars(message.text)
    if stars is None:
        await message.answer(f"{ICON_CROSS} Нужно целое число от 1 до 100000.", parse_mode="HTML")
        return
    data = await state.get_data()
    sid = data["sid"]
    wizard = data.get("wizard")
    await state.clear()
    # после настройки целиком допуслуга включается; при смене только цены статус сохраняется
    if wizard:
        await _db_exec("UPDATE shop_services SET addon_stars = %s, addon_enabled = TRUE WHERE id = %s", (stars, sid))
    else:
        await _db_exec("UPDATE shop_services SET addon_stars = %s WHERE id = %s", (stars, sid))
    await _show_addon(message, sid, edit=False)

@router.callback_query(F.data.startswith("shopa_ag_"))
async def shopa_addon_toggle(call: CallbackQuery):
    if not await is_main_admin(call.from_user.id): return
    sid = int(call.data.split("_")[2])
    await _db_exec("UPDATE shop_services SET addon_enabled = NOT COALESCE(addon_enabled, FALSE) WHERE id = %s", (sid,))
    await _show_addon(call, sid)
    await call.answer("Готово")

@router.callback_query(F.data.startswith("shopa_ax_"))
async def shopa_addon_clear(call: CallbackQuery):
    if not await is_main_admin(call.from_user.id): return
    sid = int(call.data.split("_")[2])
    await _db_exec("UPDATE shop_services SET addon_enabled = FALSE, addon_title = '', addon_question = '', addon_stars = 0 WHERE id = %s", (sid,))
    await _show_addon(call, sid)
    await call.answer("Допуслуга убрана")

# ---------- промокоды: админка ----------
def _promo_status_text(p) -> tuple:
    """p: id, code, percent, service_id, expires_at, max_uses, uses, is_active -> (эмодзи, слово)"""
    if not p[7]:
        return "⏸", "выключен"
    if p[4] is not None and p[4] <= datetime.now(timezone.utc):
        return "⌛", "истёк"
    if p[5] is not None and (p[6] or 0) >= p[5]:
        return "🈵", "лимит исчерпан"
    return "🟢", "действует"

_BOT_USERNAME = None

async def _bot_username():
    global _BOT_USERNAME
    if not _BOT_USERNAME:
        try:
            _BOT_USERNAME = (await bot.get_me()).username
        except Exception as e:
            logging.warning(f"Не удалось получить username бота: {e}")
            return None
    return _BOT_USERNAME

async def _promo_link(code: str):
    uname = await _bot_username()
    return f"https://t.me/{uname}?start=promo_{code}" if uname else None

async def _promo_get(pid: int):
    return await _db_one(
        "SELECT id, code, percent, service_id, expires_at, max_uses, uses, is_active, for_user FROM shop_promos WHERE id = %s", (pid,)
    )

async def _shopp_menu_screen():
    promos = await _db_all(
        "SELECT id, code, percent, service_id, expires_at, max_uses, uses, is_active, for_user FROM shop_promos ORDER BY id DESC LIMIT 30"
    )
    rows = []
    for p in promos:
        em, _w = _promo_status_text(p)
        rows.append([IB(f"{em} {'👤' if p[8] else ''}{p[1]} −{p[2]}% · {p[6] or 0}/{p[5] if p[5] is not None else '∞'}"[:60], callback_data=f"shopp_v_{p[0]}")])
    rows.append([IB(text="➕ Создать промокод", callback_data="shopp_new")])
    rows.append([IB(text="🔙 К услугам", callback_data="shopa_menu")])
    text = (
        "🎟 <b>Промокоды</b>\n\n"
        "Скидка в процентах на одну услугу или на все. У промокода есть срок, лимит активаций и процент. "
        "Пользователь вводит код командой /promocode или кнопкой в «💎 Платные услуги», скидка применяется автоматически и срабатывает на одну покупку. "
        f"После активации скидка закреплена за человеком на {PROMO_HOLD_HOURS} ч. (напоминания на 3, 6 и 9 часу): не оплатил, и активация возвращается в лимит. "
        "У каждого промокода есть ссылка-приглашение (переход по ней сразу активирует код). 👤 личный промокод работает только для одного человека.\n\n"
        + ("<i>Промокодов пока нет.</i>" if not promos else "🟢 действует, ⏸ выключен, ⌛ истёк, 🈵 лимит исчерпан. Показаны последние 30.")
    )
    return text, InlineKeyboardMarkup(inline_keyboard=rows)

@router.callback_query(F.data == "shopp_menu")
async def shopp_menu(call: CallbackQuery, state: FSMContext):
    if not await is_main_admin(call.from_user.id): return
    await state.clear()
    text, kb = await _shopp_menu_screen()
    try:
        await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await call.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await call.answer()

async def _shopp_view_screen(pid: int):
    p = await _promo_get(pid)
    if not p:
        return None, None
    em, word = _promo_status_text(p)
    scope = await _promo_scope_title(p[3], p[0])
    until = ("до " + p[4].astimezone(MSK_TZ).strftime("%d.%m.%Y %H:%M") + " МСК") if p[4] else "без срока"
    lim = p[5] if p[5] is not None else "без лимита"
    who_line = ""
    if p[8]:
        who_line = f"Только для: <b>{await get_user_label(p[8])}</b> (<code>{p[8]}</code>)\n"
    link = await _promo_link(p[1])
    link_line = f"\n\n🔗 Ссылка-приглашение (активирует код при переходе):\n<code>{html.escape(link)}</code>" if link else ""
    text = (
        f"🎟 <b>{html.escape(p[1])}</b> {em} {word}\n\n"
        f"Скидка: <b>{p[2]}%</b>\n"
        f"Действует на: <b>{html.escape(scope)}</b>\n"
        f"{who_line}"
        f"Срок: {until}\n"
        f"Активаций: <b>{p[6] or 0}</b> из {lim}"
        f"{link_line}"
    )
    send_row = [[IB(text="📨 Отправить человеку", callback_data=f"shopp_snd_{pid}")]] if p[8] else []
    kb = InlineKeyboardMarkup(inline_keyboard=send_row + [
        [IB(text="⏯ Вкл/выкл", callback_data=f"shopp_tg_{pid}")],
        [IB(text="🔢 Лимит", callback_data=f"shopp_lm_{pid}"), IB(text="⏱ Продлить", callback_data=f"shopp_ex_{pid}")],
        [IB(text="🗑 Удалить", callback_data=f"shopp_dl_{pid}")],
        [IB(text="🔙 К промокодам", callback_data="shopp_menu")],
    ])
    return text, kb

async def _shopp_show_view(call_or_msg, pid: int, edit: bool = True):
    text, kb = await _shopp_view_screen(pid)
    if not text:
        return
    if edit and isinstance(call_or_msg, CallbackQuery):
        await call_or_msg.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    else:
        msg = call_or_msg.message if isinstance(call_or_msg, CallbackQuery) else call_or_msg
        await msg.answer(text, reply_markup=kb, parse_mode="HTML")

@router.callback_query(F.data.startswith("shopp_v_"))
async def shopp_view(call: CallbackQuery, state: FSMContext):
    if not await is_main_admin(call.from_user.id): return
    await state.clear()
    await _shopp_show_view(call, int(call.data.split("_")[2]))
    await call.answer()

@router.callback_query(F.data.startswith("shopp_tg_"))
async def shopp_toggle(call: CallbackQuery):
    if not await is_main_admin(call.from_user.id): return
    pid = int(call.data.split("_")[2])
    await _db_exec("UPDATE shop_promos SET is_active = NOT is_active WHERE id = %s", (pid,))
    await _shopp_show_view(call, pid)
    await call.answer("Готово")

@router.callback_query(F.data.startswith("shopp_dl_"))
async def shopp_delete_ask(call: CallbackQuery):
    if not await is_main_admin(call.from_user.id): return
    pid = int(call.data.split("_")[2])
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        IB(text="🗑 Да, удалить", callback_data=f"shopp_dly_{pid}"),
        IB(text="❌ Нет", callback_data=f"shopp_v_{pid}")
    ]])
    await call.message.edit_text("Удалить промокод? Он пропадёт и у тех, кто его уже активировал, но ещё не использовал.", reply_markup=kb)
    await call.answer()

@router.callback_query(F.data.startswith("shopp_dly_"))
async def shopp_delete(call: CallbackQuery):
    if not await is_main_admin(call.from_user.id): return
    pid = int(call.data.split("_")[2])
    await _db_exec("DELETE FROM shop_promos WHERE id = %s", (pid,))
    text, kb = await _shopp_menu_screen()
    await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await call.answer("Удалено")

# --- создание ---
@router.callback_query(F.data == "shopp_new")
async def shopp_new(call: CallbackQuery, state: FSMContext):
    if not await is_main_admin(call.from_user.id): return
    await state.clear()
    await state.set_state(ShopAdminStates.promo_code)
    await call.message.answer(
        "🎟 Отправьте <b>код промокода</b>: латиница, цифры, «_» и «-», от 3 до 24 символов. Регистр не важен. "
        "Или отправьте «авто», я придумаю код сам.",
        reply_markup=_shopa_cancel_kb("shopp_menu"), parse_mode="HTML"
    )
    await call.answer()

@router.message(ShopAdminStates.promo_code, F.text)
async def shopp_code_proc(message: Message, state: FSMContext):
    if not await is_main_admin(message.from_user.id): return
    raw = message.text.strip()
    code = ("PROMO-" + uuid.uuid4().hex[:6].upper()) if raw.lower() in ("авто", "auto") else raw.upper()
    if not _PROMO_CODE_RE.match(code):
        await message.answer(f"{ICON_CROSS} Код: латиница, цифры, «_» и «-», от 3 до 24 символов.", parse_mode="HTML")
        return
    if await _db_one("SELECT 1 FROM shop_promos WHERE code = %s", (code,)):
        await message.answer(f"{ICON_CROSS} Такой код уже есть, выберите другой.", parse_mode="HTML")
        return
    await state.update_data(pcode=code)
    await state.set_state(ShopAdminStates.promo_pct)
    await message.answer(
        f"Код: <b>{html.escape(code)}</b>\n\nОтправьте <b>процент скидки</b> числом от 1 до 99:",
        reply_markup=_shopa_cancel_kb("shopp_menu"), parse_mode="HTML"
    )

@router.message(ShopAdminStates.promo_pct, F.text)
async def shopp_pct_proc(message: Message, state: FSMContext):
    if not await is_main_admin(message.from_user.id): return
    t = message.text.strip().rstrip("%").strip()
    if not t.isdigit() or not (1 <= int(t) <= 99):
        await message.answer(f"{ICON_CROSS} Нужно целое число от 1 до 99.", parse_mode="HTML")
        return
    await state.update_data(pct=int(t))
    await state.set_state(ShopAdminStates.promo_wait)
    services = await shop_get_services(False)
    rows = [[IB(text="🌐 Все услуги", callback_data="shopp_scope_all")]]
    for sv in services:
        rows.append([IB(text=f"💎 {sv[1]}"[:60], callback_data=f"shopp_scope_{sv[0]}")])
    rows.append([IB(text="❌ Отменить", callback_data="shopp_menu")])
    await message.answer(
        f"Скидка: <b>{int(t)}%</b>\n\nНа что действует промокод?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML"
    )

async def _shopp_wiz(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data or "pcode" not in data or "pct" not in data:
        await call.answer("Создание устарело, начните заново.", show_alert=True)
        return None
    return data

def _shopp_dur_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [IB(text="6 ч", callback_data="shopp_dur_6"), IB(text="1 день", callback_data="shopp_dur_24"), IB(text="3 дня", callback_data="shopp_dur_72")],
        [IB(text="7 дней", callback_data="shopp_dur_168"), IB(text="30 дней", callback_data="shopp_dur_720"), IB(text="♾ Без срока", callback_data="shopp_dur_0")],
        [IB(text="✏ Своё", callback_data="shopp_dur_c")],
        [IB(text="❌ Отменить", callback_data="shopp_menu")],
    ])

_SHOPP_DUR_TEXT = "⏱ <b>Сколько времени действует промокод</b> (до какого момента его можно активировать и использовать)?"

async def _shopp_ask_for(call: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [IB(text="👥 Для всех", callback_data="shopp_for_all")],
        [IB(text="👤 Личный (одному человеку)", callback_data="shopp_for_one")],
        [IB(text="❌ Отменить", callback_data="shopp_menu")],
    ])
    await call.message.edit_text("👥 <b>Кому доступен промокод?</b>\n\nЛичный промокод работает только для одного человека (по ID или @юзернейму) и на одну активацию.", reply_markup=kb, parse_mode="HTML")

@router.callback_query(F.data.startswith("shopp_scope_"))
async def shopp_scope(call: CallbackQuery, state: FSMContext):
    if not await is_main_admin(call.from_user.id): return
    if await _shopp_wiz(call, state) is None: return
    v = call.data[len("shopp_scope_"):]
    sid = 0 if v == "all" else int(v)
    await state.update_data(sid=sid, tid=0, fuser=None)
    tariffs = await shop_get_tariffs(sid, False) if sid else []
    if tariffs:
        rows = [[IB(text="🧩 Все тарифы услуги", callback_data="shopp_tar_all")]]
        for t in tariffs:
            rows.append([IB(text=f"{t[1]} ({t[2]} ⭐)"[:60], callback_data=f"shopp_tar_{t[0]}")])
        rows.append([IB(text="❌ Отменить", callback_data="shopp_menu")])
        await call.message.edit_text("🧩 <b>На какие тарифы действует промокод?</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML")
        await call.answer()
        return
    await _shopp_ask_for(call)
    await call.answer()

@router.callback_query(F.data.startswith("shopp_tar_"))
async def shopp_tariff(call: CallbackQuery, state: FSMContext):
    if not await is_main_admin(call.from_user.id): return
    data = await _shopp_wiz(call, state)
    if data is None: return
    v = call.data[len("shopp_tar_"):]
    tid = 0 if v == "all" else int(v)
    if tid:
        t = await shop_get_tariff(tid)
        if not t or t[1] != data.get("sid"):
            await call.answer("Тариф не найден, начните заново.", show_alert=True)
            return
    await state.update_data(tid=tid)
    await _shopp_ask_for(call)
    await call.answer()

@router.callback_query(F.data == "shopp_for_all")
async def shopp_for_all(call: CallbackQuery, state: FSMContext):
    if not await is_main_admin(call.from_user.id): return
    if await _shopp_wiz(call, state) is None: return
    await state.update_data(fuser=None)
    await call.message.edit_text(_SHOPP_DUR_TEXT, reply_markup=_shopp_dur_kb(), parse_mode="HTML")
    await call.answer()

@router.callback_query(F.data == "shopp_for_one")
async def shopp_for_one(call: CallbackQuery, state: FSMContext):
    if not await is_main_admin(call.from_user.id): return
    if await _shopp_wiz(call, state) is None: return
    await state.set_state(ShopAdminStates.promo_user)
    await call.message.answer(
        "👤 Отправьте <b>ID или @юзернейм</b> человека. Человек должен хотя бы раз запускать бота, иначе ему нельзя будет написать.",
        reply_markup=_shopa_cancel_kb("shopp_menu"), parse_mode="HTML"
    )
    await call.answer()

@router.message(ShopAdminStates.promo_user, F.text)
async def shopp_user_proc(message: Message, state: FSMContext):
    if not await is_main_admin(message.from_user.id): return
    try:
        uid = await resolve_user_one(message.text)
    except ValueError:
        await message.answer(f"{ICON_CROSS} " + USER_NOT_FOUND_TEXT, parse_mode="HTML")
        return
    known = await _db_one("SELECT 1 FROM users WHERE user_id = %s", (uid,))
    await state.update_data(fuser=uid)
    await state.set_state(ShopAdminStates.promo_wait)
    note = "" if known else "\n⚠️ Этот ID не найден среди пользователей бота: проверьте, что он верный. Промокод всё равно можно создать."
    await message.answer(
        f"Для: <b>{await get_user_label(uid)}</b> (<code>{uid}</code>){note}\n\n" + _SHOPP_DUR_TEXT,
        reply_markup=_shopp_dur_kb(), parse_mode="HTML"
    )

def _shopp_limit_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [IB(text="1", callback_data="shopp_max_1"), IB(text="5", callback_data="shopp_max_5"), IB(text="10", callback_data="shopp_max_10")],
        [IB(text="50", callback_data="shopp_max_50"), IB(text="100", callback_data="shopp_max_100"), IB(text="♾ Без лимита", callback_data="shopp_max_0")],
        [IB(text="✏ Своё", callback_data="shopp_max_c")],
        [IB(text="❌ Отменить", callback_data="shopp_menu")],
    ])

@router.callback_query(F.data.startswith("shopp_dur_"))
async def shopp_dur(call: CallbackQuery, state: FSMContext):
    if not await is_main_admin(call.from_user.id): return
    data = await _shopp_wiz(call, state)
    if data is None: return
    v = call.data[len("shopp_dur_"):]
    if v == "c":
        await state.set_state(ShopAdminStates.promo_dur_custom)
        await call.message.answer(
            "✏ Отправьте срок: число и единица. Например <code>12ч</code> (12 часов) или <code>5д</code> (5 дней). Без единицы считаются дни.",
            reply_markup=_shopa_cancel_kb("shopp_menu"), parse_mode="HTML"
        )
        await call.answer()
        return
    await state.update_data(hours=int(v))
    if data.get("fuser"):
        await _shopp_create(call, state, 1)      # личный промокод: ровно одна активация
        await call.answer()
        return
    await call.message.edit_text("🔢 <b>Сколько активаций</b> доступно (сколько разных людей смогут ввести код)?", reply_markup=_shopp_limit_kb(), parse_mode="HTML")
    await call.answer()

@router.message(ShopAdminStates.promo_dur_custom, F.text)
async def shopp_dur_custom(message: Message, state: FSMContext):
    if not await is_main_admin(message.from_user.id): return
    m = re.fullmatch(r"(\d{1,4})\s*([чhдd]?)", message.text.strip().lower())
    if not m:
        await message.answer(f"{ICON_CROSS} Пример: <code>12ч</code> или <code>5д</code>.", parse_mode="HTML")
        return
    n = int(m.group(1))
    hours = n if m.group(2) in ("ч", "h") else n * 24
    if not (1 <= hours <= 24 * 365):
        await message.answer(f"{ICON_CROSS} Срок: от 1 часа до 365 дней.", parse_mode="HTML")
        return
    await state.update_data(hours=hours)
    await state.set_state(ShopAdminStates.promo_wait)
    if (await state.get_data()).get("fuser"):
        await _shopp_create(message, state, 1)   # личный промокод: ровно одна активация
        return
    await message.answer("🔢 <b>Сколько активаций</b> доступно (сколько разных людей смогут ввести код)?", reply_markup=_shopp_limit_kb(), parse_mode="HTML")

async def _shopp_create(target, state: FSMContext, max_uses):
    data = await state.get_data()
    code, pct, sid, hours = data["pcode"], data["pct"], data.get("sid", 0), data.get("hours", 0)
    expires = (datetime.now(timezone.utc) + timedelta(hours=hours)) if hours else None
    try:
        row = await _db_exec(
            "INSERT INTO shop_promos (code, percent, service_id, expires_at, max_uses, for_user, tariff_id) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (code, pct, sid or None, expires, max_uses, data.get("fuser") or None, data.get("tid") or None)
        )
    except Exception as e:
        logging.error(f"Не удалось создать промокод: {e}")
        await state.clear()
        msg = target.message if isinstance(target, CallbackQuery) else target
        await msg.answer(f"{ICON_CROSS} Не удалось создать промокод (возможно, такой код уже есть).", parse_mode="HTML")
        return
    await state.clear()
    msg = target.message if isinstance(target, CallbackQuery) else target
    await msg.answer(f"{ICON_CHECK} Промокод создан!", parse_mode="HTML")
    await _shopp_show_view(msg, row[0], edit=False)

@router.callback_query(F.data.startswith("shopp_max_"))
async def shopp_max(call: CallbackQuery, state: FSMContext):
    if not await is_main_admin(call.from_user.id): return
    data = await _shopp_wiz(call, state)
    if data is None: return
    v = call.data[len("shopp_max_"):]
    if v == "c":
        await state.set_state(ShopAdminStates.promo_max_custom)
        await call.message.answer("✏ Отправьте число активаций (от 1 до 100000):", reply_markup=_shopa_cancel_kb("shopp_menu"), parse_mode="HTML")
        await call.answer()
        return
    n = int(v)
    await _shopp_create(call, state, n if n > 0 else None)
    await call.answer()

@router.message(ShopAdminStates.promo_max_custom, F.text)
async def shopp_max_custom(message: Message, state: FSMContext):
    if not await is_main_admin(message.from_user.id): return
    n = _parse_stars(message.text)   # то же правило: целое от 1 до 100000
    if n is None:
        await message.answer(f"{ICON_CROSS} Нужно целое число от 1 до 100000.", parse_mode="HTML")
        return
    await _shopp_create(message, state, n)

@router.callback_query(F.data.startswith("shopp_snd_"))
async def shopp_send_personal(call: CallbackQuery):
    if not await is_main_admin(call.from_user.id): return
    pid = int(call.data.split("_")[2])
    p = await _promo_get(pid)
    if not p or not p[8]:
        await call.answer("Это не личный промокод.", show_alert=True)
        return
    scope = await _promo_scope_title(p[3], p[0])
    until = ("до " + p[4].astimezone(MSK_TZ).strftime("%d.%m.%Y %H:%M") + " МСК") if p[4] else "без срока"
    link = await _promo_link(p[1])
    text = (
        "🎁 <b>Для вас персональный промокод!</b>\n\n"
        f"Код: <code>{html.escape(p[1])}</code>\n"
        f"Скидка: <b>{p[2]}%</b> на: <b>{html.escape(scope)}</b>\n"
        f"Срок: {until}\n\n"
        "Нажмите кнопку ниже или отправьте боту <code>/promocode " + html.escape(p[1]) + "</code>. "
        "Скидка применится автоматически при оплате в «💎 Платные услуги»."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[IB(text="🎟 Активировать", url=link)]]) if link else None
    try:
        await bot.send_message(p[8], text, reply_markup=kb, parse_mode="HTML")
    except Exception as e:
        logging.warning(f"Не удалось отправить личный промокод {p[8]}: {e}")
        await call.answer("Не удалось отправить: человек не запускал бота или заблокировал его.", show_alert=True)
        return
    await call.answer("Отправлено человеку ✅", show_alert=True)

# --- правка лимита и срока ---
@router.callback_query(F.data.startswith("shopp_lm_"))
async def shopp_limit_ask(call: CallbackQuery, state: FSMContext):
    if not await is_main_admin(call.from_user.id): return
    pid = int(call.data.split("_")[2])
    await state.set_state(ShopAdminStates.promo_edit_limit)
    await state.update_data(pid=pid)
    await call.message.answer("🔢 Отправьте новый лимит активаций (целое число) или <code>0</code>, чтобы убрать лимит:", reply_markup=_shopa_cancel_kb(f"shopp_v_{pid}"), parse_mode="HTML")
    await call.answer()

@router.message(ShopAdminStates.promo_edit_limit, F.text)
async def shopp_limit_proc(message: Message, state: FSMContext):
    if not await is_main_admin(message.from_user.id): return
    t = message.text.strip()
    if not t.isdigit() or int(t) > 100000:
        await message.answer(f"{ICON_CROSS} Нужно целое число от 0 до 100000.", parse_mode="HTML")
        return
    pid = (await state.get_data())["pid"]
    await state.clear()
    await _db_exec("UPDATE shop_promos SET max_uses = %s WHERE id = %s", (int(t) or None, pid))
    await _shopp_show_view(message, pid, edit=False)

@router.callback_query(F.data.startswith("shopp_ex_"))
async def shopp_extend_ask(call: CallbackQuery, state: FSMContext):
    if not await is_main_admin(call.from_user.id): return
    pid = int(call.data.split("_")[2])
    await state.set_state(ShopAdminStates.promo_extend)
    await state.update_data(pid=pid)
    await call.message.answer(
        "⏱ На сколько <b>дней</b> продлить промокод (1–365)? Отсчёт от текущего окончания, а если он уже истёк или был бессрочным, то от сейчас.",
        reply_markup=_shopa_cancel_kb(f"shopp_v_{pid}"), parse_mode="HTML"
    )
    await call.answer()

@router.message(ShopAdminStates.promo_extend, F.text)
async def shopp_extend_proc(message: Message, state: FSMContext):
    if not await is_main_admin(message.from_user.id): return
    t = message.text.strip()
    if not t.isdigit() or not (1 <= int(t) <= 365):
        await message.answer(f"{ICON_CROSS} Нужно целое число от 1 до 365.", parse_mode="HTML")
        return
    pid = (await state.get_data())["pid"]
    await state.clear()
    p = await _promo_get(pid)
    if not p:
        return
    now = datetime.now(timezone.utc)
    base = p[4] if (p[4] is not None and p[4] > now) else now
    await _db_exec("UPDATE shop_promos SET expires_at = %s WHERE id = %s", (base + timedelta(days=int(t)), pid))
    await _shopp_show_view(message, pid, edit=False)

# ссылка на канал прайса и история
@router.callback_query(F.data == "shopa_chan")
async def shopa_chan(call: CallbackQuery, state: FSMContext):
    if not await is_main_admin(call.from_user.id): return
    await state.set_state(ShopAdminStates.chan_url)
    await call.message.answer(
        f"🔗 Сейчас кнопка «Другие методы оплаты» ведёт на:\n{html.escape(await shop_channel_url())}\n\n"
        "Отправьте новую ссылку (начинается с https://):",
        reply_markup=_shopa_cancel_kb("shopa_menu"), parse_mode="HTML", link_preview_options=LinkPreviewOptions(is_disabled=True)
    )
    await call.answer()

@router.message(ShopAdminStates.chan_url, F.text)
async def shopa_chan_proc(message: Message, state: FSMContext):
    if not await is_main_admin(message.from_user.id): return
    url = message.text.strip()
    if not re.match(r"^https?://\S+$", url):
        await message.answer(f"{ICON_CROSS} Ссылка должна начинаться с https://", parse_mode="HTML")
        return
    await set_setting("shop_channel_url", url)
    await state.clear()
    text, kb = await _shopa_menu_screen()
    await message.answer(f"{ICON_CHECK} Ссылка сохранена.\n\n" + text, reply_markup=kb, parse_mode="HTML")

@router.callback_query(F.data == "shopa_hist")
async def shopa_history(call: CallbackQuery):
    if not await is_main_admin(call.from_user.id): return
    rows = await _db_all(
        "SELECT created_at, user_id, service_title, tariff_label, stars, status, charge_id, addon_title, promo_code FROM shop_payments ORDER BY id DESC LIMIT 15"
    )
    if not rows:
        body = "<i>Оплат пока не было.</i>"
    else:
        parts = []
        for created, uid, st, tl, stars, status, ch, adt, pcode in rows:
            when = created.astimezone(MSK_TZ).strftime("%d.%m %H:%M") if created else ""
            mark = "↩️" if status == "refunded" else ("🧪" if status == "test" else "✅")
            parts.append(f"{mark} {when} · <code>{uid}</code> · {html.escape(st or '')} / {html.escape(tl or '')}{(' + ' + html.escape(adt)) if adt else ''} · {stars} ⭐{(' · 🎟 ' + html.escape(pcode)) if pcode else ''}\n<code>{html.escape(ch or '')}</code>")
        body = "\n\n".join(parts)
    kb = InlineKeyboardMarkup(inline_keyboard=[[IB(text="🔙 Назад", callback_data="shopa_menu")]])
    await call.message.edit_text(
        "🧾 <b>Последние оплаты</b>\n\n" + body + "\n\n<i>Возврат: владелец отправляет боту /refund и ID платежа.</i>",
        reply_markup=kb, parse_mode="HTML"
    )
    await call.answer()

# ----------------------------------------------------------------------
# СИСТЕМА НАБОРА В ХЕЛПЕРЫ И ИСКАТЕЛИ СЕКРЕТОВ
# ----------------------------------------------------------------------

@router.callback_query(F.data.regexp(r"^rsub_(on|off)_(helper|secret)$"))
async def recruit_sub_callback(call: CallbackQuery):
    _, act, kind = call.data.split("_")
    uid = call.from_user.id
    if await is_banned(uid):
        await call.answer("Недоступно.", show_alert=True)
        return
    if act == "on":
        if await get_setting(RECRUIT_KINDS[kind][0], "true") == "true":
            await call.answer("Набор уже открыт! Нажмите кнопку подачи заявки в меню.", show_alert=True)
            return
        await recruit_sub_set(uid, kind, True)
        await call.answer("Готово, напишу, когда откроется.")
    else:
        await recruit_sub_set(uid, kind, False)
        await call.answer("Уведомление отменено.")
    try:
        await call.message.edit_text(
            _recruit_closed_text(kind, act == "on"), reply_markup=await _recruit_closed_kb(uid, kind), parse_mode="HTML"
        )
    except Exception:
        pass

async def notify_recruit_open(kind: str, admin_id: int = 0):
    """Набор открыли: один раз пишем подписчикам. Если набор успели снова закрыть, остальные подписки остаются до следующего раза."""
    if kind not in RECRUIT_KINDS:
        return
    setting, title, go = RECRUIT_KINDS[kind]
    try:
        rows = await _db_all("SELECT user_id FROM recruit_subs WHERE kind = %s ORDER BY created_at", (kind,))
    except Exception as e:
        logging.error(f"Подписки на набор: не удалось прочитать список ({kind}): {e}")
        return
    if not rows:
        return
    uname = await _bot_username()
    kb = None
    if uname:
        kb = InlineKeyboardMarkup(inline_keyboard=[[IB(text="📝 Подать заявку", url=f"https://t.me/{uname}?start={go}")]])
    text = (f"🟢 <b>{html.escape(title)} открыт!</b>\n\nМожно подавать заявку, нажмите кнопку ниже.\n"
            "<i>Это разовое уведомление. Чтобы получить следующее, подпишитесь снова на сайте или в боте.</i>")
    sent = failed = 0
    for i, (uid,) in enumerate(rows):
        if i % 20 == 0 and await get_setting(setting, "true") != "true":
            break                      # набор закрыли обратно: оставшихся не трогаем
        try:
            if await is_banned(uid):
                pass
            else:
                try:
                    await bot.send_message(uid, text, reply_markup=kb, parse_mode="HTML")
                except TelegramRetryAfter as e:
                    await asyncio.sleep(min(int(getattr(e, "retry_after", 3)) + 1, 30))
                    await bot.send_message(uid, text, reply_markup=kb, parse_mode="HTML")
                sent += 1
        except Exception:
            failed += 1                # заблокировал бота и т. п.
        try:
            await recruit_sub_set(uid, kind, False)
        except Exception:
            pass
        await asyncio.sleep(0.06)
    logging.info(f"Набор открыт ({kind}): уведомлено {sent}, не доставлено {failed}")
    if admin_id and (sent or failed):
        try:
            await bot.send_message(admin_id, f"🔔 Подписчикам набора разослано: {sent}" + (f" (не доставлено: {failed})" if failed else ""))
        except Exception:
            pass

# --- НАБОР В ХЕЛПЕРЫ (В ТЕМУ ПОДДЕРЖКИ) ---
@router.message(F.text.in_(btn_texts(BTN_HELPER_APPLY)), F.chat.type == "private")
async def start_helper_apply(message: Message, state: FSMContext):
    await register_user(message.from_user.id)
    if await is_banned(message.from_user.id): return
    if await get_setting("helper_recruitment", "true") != "true":
        has = await recruit_sub_has(message.from_user.id, "helper")
        await message.answer(
            _recruit_closed_text("helper", has), reply_markup=await _recruit_closed_kb(message.from_user.id, "helper"), parse_mode="HTML"
        )
        return
    if await app_pending_exists(message.from_user.id, "helper"):
        await message.answer(_APP_PENDING_TEXT, parse_mode="HTML")
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
    if await app_pending_exists(user.id, "helper"):
        await state.clear()
        await message.answer(_APP_PENDING_TEXT, reply_markup=await main_keyboard(user.id), parse_mode="HTML")
        return
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
        message_thread_id=TOPIC_APPLICATIONS_ID,
        parse_mode="HTML"
    )
    await map_message(sent.message_id, user.id, 0)
    await app_pending_set(user.id, "helper")
    kb = await main_keyboard(user.id)
    night_txt = get_night_notice_application()
    await message.answer(f"{ICON_CHECK} Ваша анкета успешно отправлена администрации! Ожидайте ответа.{night_txt}", reply_markup=kb, parse_mode="HTML")
    await state.clear()

async def app_pending_exists(uid: int, kind: str) -> bool:
    """Есть ли у человека анкета этого типа без решения (старше 7 суток не считаем: карточку могли потерять)."""
    def _q():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM applications_pending WHERE user_id = %s AND kind = %s AND created_at > CURRENT_TIMESTAMP - INTERVAL '7 days'",
                    (uid, kind)
                )
                return cur.fetchone() is not None
    try:
        return await asyncio.to_thread(_q)
    except Exception as e:
        logging.error(f"Ошибка проверки анкеты без решения: {e}")
        return False

async def app_pending_set(uid: int, kind: str):
    def _q():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO applications_pending (user_id, kind) VALUES (%s, %s) "
                    "ON CONFLICT (user_id, kind) DO UPDATE SET created_at = CURRENT_TIMESTAMP",
                    (uid, kind)
                )
    try:
        await asyncio.to_thread(_q)
    except Exception as e:
        logging.error(f"Ошибка записи анкеты без решения: {e}")

async def app_pending_clear(uid: int, kind: str):
    def _q():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM applications_pending WHERE user_id = %s AND kind = %s", (uid, kind))
    try:
        await asyncio.to_thread(_q)
    except Exception as e:
        logging.error(f"Ошибка очистки анкеты без решения: {e}")

_APP_PENDING_TEXT = f"{ICON_WARN} Ваша анкета уже отправлена и ждёт решения. Дождитесь ответа, подать вторую до этого нельзя."

async def _mark_application_decision(call: CallbackQuery, accepted: bool, kind: str = "", target_uid: int = 0) -> bool:
    """Дописывает в анкету, кто и когда принял решение. False, если решение уже есть."""
    cur_html = call.message.html_text or ""
    if "<b>Принял:</b>" in cur_html or "<b>Отклонил:</b>" in cur_html:
        await call.answer("Эта анкета уже обработана.", show_alert=True)
        return False
    who = get_user_mention(call.from_user)
    uname = f" (@{call.from_user.username})" if call.from_user.username else ""
    when = datetime.now(MSK_TZ).strftime("%d.%m %H:%M")
    if accepted:
        line = f"\n\n🟢 <b>Статус:</b> Одобрено ✅\n👤 <b>Принял:</b> {who}{uname} · {when} МСК"
    else:
        line = f"\n\n🔴 <b>Статус:</b> Отклонено ❌\n👤 <b>Отклонил:</b> {who}{uname} · {when} МСК"
    await call.message.edit_text(cur_html + line, parse_mode="HTML")
    if kind and target_uid:
        await app_pending_clear(target_uid, kind)
    await call.answer("Заявка принята!" if accepted else "Заявка отклонена!")
    return True

@router.callback_query(F.data.startswith("helper_accept_"))
async def helper_accept_callback(call: CallbackQuery):
    if not await is_main_admin(call.from_user.id) and not await is_support_member(call.from_user.id):
        await call.answer("❌ У вас нет прав!", show_alert=True)
        return
    user_id = int(call.data.split("_")[2])
    if not await _mark_application_decision(call, True, "helper", user_id): return
    await alog(call.from_user.id, "helper_accept", f"ID {user_id}")
    try:
        await bot.send_message(user_id, "<tg-emoji emoji-id='5994502837327892086'>🎉</tg-emoji> <b>Поздравляем! Ваша заявка на хелпера принята!</b> Администрация свяжется с вами в ближайшее время.", parse_mode="HTML")
    except Exception:
        pass

@router.callback_query(F.data.startswith("helper_reject_"))
async def helper_reject_callback(call: CallbackQuery):
    if not await is_main_admin(call.from_user.id) and not await is_support_member(call.from_user.id):
        await call.answer("❌ У вас нет прав!", show_alert=True)
        return
    user_id = int(call.data.split("_")[2])
    if not await _mark_application_decision(call, False, "helper", user_id): return
    await alog(call.from_user.id, "helper_reject", f"ID {user_id}")
    try:
        await bot.send_message(user_id, f"{ICON_CROSS} К сожалению, ваша заявка на хелпера была отклонена.", parse_mode="HTML")
    except Exception:
        pass

# --- НАБОР В ИСКАТЕЛИ СЕКРЕТОК (В ТЕМУ СЕКРЕТОК) ---
SECRET_APPLY_MAX_ATTEMPTS = 3

async def _recruit_epoch() -> int:
    try:
        return int((await get_setting("secret_recruit_epoch", "0")) or 0)
    except ValueError:
        return 0

async def secret_apply_attempts_used(uid: int) -> int:
    ep = await _recruit_epoch()
    def _q():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT cnt FROM secret_apply_attempts WHERE user_id = %s AND epoch = %s", (uid, ep))
                row = cur.fetchone()
                return row[0] if row else 0
    try:
        return await asyncio.to_thread(_q)
    except Exception as e:
        logging.error(f"Ошибка чтения попыток набора: {e}")
        return 0

async def secret_apply_attempt_add(uid: int):
    ep = await _recruit_epoch()
    def _q():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO secret_apply_attempts (user_id, epoch, cnt) VALUES (%s, %s, 1) "
                    "ON CONFLICT (user_id, epoch) DO UPDATE SET cnt = secret_apply_attempts.cnt + 1",
                    (uid, ep)
                )
    try:
        await asyncio.to_thread(_q)
    except Exception as e:
        logging.error(f"Ошибка записи попытки набора: {e}")

@router.message(F.text.in_(btn_texts(BTN_SECRET_APPLY)), F.chat.type == "private")
async def start_secret_apply(message: Message, state: FSMContext):
    await register_user(message.from_user.id)
    if await is_banned(message.from_user.id): return
    if await get_setting("secret_recruitment", "true") != "true":
        has = await recruit_sub_has(message.from_user.id, "secret")
        await message.answer(
            _recruit_closed_text("secret", has), reply_markup=await _recruit_closed_kb(message.from_user.id, "secret"), parse_mode="HTML"
        )
        return
    if not message.from_user.username:
        await message.answer(
            f"{ICON_WARN} <b>У вас не установлен @username в профиле Telegram!</b>\n\n"
            "Для подачи заявки и связи с администрацией он обязателен.\n"
            "Установите его (Настройки ➔ Изменить профиль ➔ Имя пользователя) и нажмите кнопку набора ещё раз.",
            parse_mode="HTML"
        )
        return
    if await app_pending_exists(message.from_user.id, "secret"):
        await message.answer(_APP_PENDING_TEXT, parse_mode="HTML")
        return
    if await secret_apply_attempts_used(message.from_user.id) >= SECRET_APPLY_MAX_ATTEMPTS:
        await message.answer(
            f"{ICON_WARN} Вы уже подавали заявки в этом наборе. Дождитесь ответа администрации.",
            parse_mode="HTML"
        )
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
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        IB(text="✅ Да", callback_data="secvip_yes"),
        IB(text="❌ Нету", callback_data="secvip_no"),
    ], [IB(text="📅 Скоро появится", callback_data="secvip_soon")]])
    await message.answer(f"{NUM_3} Есть ли у вас вип?", reply_markup=kb, parse_mode="HTML")

@router.message(Form.secret_vip)
async def process_secret_vip_need_buttons(message: Message):
    await message.answer(f"{ICON_WARN} Выберите ответ кнопкой под вопросом: «Да», «Нету» или «Скоро появится».", parse_mode="HTML")

@router.callback_query(Form.secret_vip, F.data.in_({"secvip_yes", "secvip_no", "secvip_soon"}))
async def process_secret_vip_choice(call: CallbackQuery, state: FSMContext):
    choice = call.data
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    if choice == "secvip_no":
        await secret_apply_attempt_add(call.from_user.id)
        await state.clear()
        kb = await main_keyboard(call.from_user.id)
        await call.message.answer(
            f"{ICON_CROSS} <b>К сожалению, заявка отклонена автоматически.</b>\n"
            "VIP сервер обязателен для искателя секреток. Когда он появится, подайте заявку заново.",
            reply_markup=kb, parse_mode="HTML"
        )
        await call.answer()
        return
    if choice == "secvip_yes":
        await state.update_data(secret_vip="Да")
        await state.set_state(Form.secret_vip_until)
        await call.message.answer("📅 До какой даты активен ваш VIP сервер? Напишите дату или срок (например, «до 20 ноября» или «ещё 2 недели»):", parse_mode="HTML")
    else:
        await state.update_data(secret_vip="Скоро появится")
        await state.set_state(Form.secret_vip_date)
        await call.message.answer("📅 Когда примерно у вас появится VIP сервер? Напишите дату или срок (например, «через неделю» или «15 ноября»):", parse_mode="HTML")
    await call.answer()

@router.message(Form.secret_vip_until, F.text)
async def process_secret_vip_until(message: Message, state: FSMContext):
    await state.update_data(secret_vip=f"Да (активен до: {message.text.strip()[:200]})")
    await state.set_state(Form.secret_time)
    await message.answer(f"{NUM_4} Сколько вы готовы уделять времени на поиск секреток?", parse_mode="HTML")

@router.message(Form.secret_vip_date, F.text)
async def process_secret_vip_date(message: Message, state: FSMContext):
    data = await state.get_data()
    await state.update_data(secret_vip=f"Скоро появится ({message.text.strip()[:200]})")
    await state.set_state(Form.secret_time)
    await message.answer(f"{NUM_4} Сколько вы готовы уделять времени на поиск секреток?", parse_mode="HTML")

@router.message(Form.secret_time, F.text)
async def process_secret_time(message: Message, state: FSMContext):
    await state.update_data(secret_time=message.text.strip())
    await state.set_state(Form.secret_why)
    await message.answer(f"{NUM_5} Почему вы выбрали именно наш канал?", parse_mode="HTML")

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
    if await app_pending_exists(user.id, "secret"):
        await state.clear()
        await message.answer(_APP_PENDING_TEXT, reply_markup=await main_keyboard(user.id), parse_mode="HTML")
        return
    user_mention = get_user_mention(user)

    admin_text = (
        f"🔍 <b>Новая заявка в искатели секреток!</b>\n\n"
        f"👤 От: {user_mention}\n"
        f"🆔 ID: <code>{user.id}</code>\n"
        f"👤 Username: {('@' + user.username) if user.username else 'нет'}\n\n"
        f"1. <b>Имя:</b> {html.escape(data.get('secret_name', 'Не указано'))}\n"
        f"2. <b>Возраст:</b> {html.escape(data.get('secret_age', 'Не указан'))}\n"
        f"3. <b>VIP:</b> {html.escape(data.get('secret_vip', 'Не указано'))}\n"
        f"4. <b>Время на поиск:</b> {html.escape(data.get('secret_time', 'Не указано'))}\n"
        f"5. <b>Почему к нам:</b> {html.escape(why_text)}"
    )

    sent = await bot.send_message(
        ADMIN_CHAT_ID, 
        admin_text, 
        reply_markup=secret_decision_kb(user.id), 
        message_thread_id=TOPIC_APPLICATIONS_ID,
        parse_mode="HTML"
    )
    await map_message(sent.message_id, user.id, 0)
    await app_pending_set(user.id, "secret")
    kb = await main_keyboard(user.id)
    night_txt = get_night_notice_application()
    await message.answer(f"{ICON_CHECK} Ваша заявка в искатели секреток успешно отправлена! Ожидайте ответа.{night_txt}", reply_markup=kb, parse_mode="HTML")
    await secret_apply_attempt_add(user.id)
    await state.clear()

@router.callback_query(F.data.startswith("secret_accept_"))
async def secret_accept_callback(call: CallbackQuery):
    if not await is_main_admin(call.from_user.id) and not await is_support_member(call.from_user.id):
        await call.answer("❌ У вас нет прав!", show_alert=True)
        return
    user_id = int(call.data.split("_")[2])
    if not await _mark_application_decision(call, True, "secret", user_id): return
    await alog(call.from_user.id, "secret_accept", f"ID {user_id}")
    try:
        await bot.send_message(user_id, "<tg-emoji emoji-id='5994502837327892086'>🎉</tg-emoji> <b>Поздравляем! Ваша заявка в искатели секреток принята!</b> Администрация свяжется с вами в ближайшее время.", parse_mode="HTML")
    except Exception:
        pass

@router.callback_query(F.data.startswith("secret_reject_"))
async def secret_reject_callback(call: CallbackQuery):
    if not await is_main_admin(call.from_user.id) and not await is_support_member(call.from_user.id):
        await call.answer("❌ У вас нет прав!", show_alert=True)
        return
    user_id = int(call.data.split("_")[2])
    if not await _mark_application_decision(call, False, "secret", user_id): return
    await alog(call.from_user.id, "secret_reject", f"ID {user_id}")
    try:
        await bot.send_message(user_id, f"{ICON_CROSS} К сожалению, ваша заявка в искатели секреток была отклонена.", parse_mode="HTML")
    except Exception:
        pass

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
        "cmp_srv_farm": "Фарм сервер",
        "cmp_srv_researcher": "Бейдж исследователь",
        "cmp_srv_mutator": "Бейдж мутатор"
    }
    chosen_server = srv_types.get(call.data, "Не указан")
    await state.update_data(c_place=chosen_server, c_kind=call.data[8:])
    await state.set_state(Form.complaint_time)
    await call.message.edit_text(
        f"Выбран сервер: <b>{chosen_server}</b>{_rules_hint(call.data[8:], 'complaint')}\n\n"
        f"{NUM_4} {_time_prompt()}",
        parse_mode="HTML", link_preview_options=LinkPreviewOptions(is_disabled=True)
    )
    await call.answer()

# ----------------------------------------------------------------------
# ВРЕМЯ ПОСТА В ЖАЛОБЕ / ОБЖАЛОВАНИИ: разбор, подтверждение «по МСК», поиск поста ±5 минут
# ----------------------------------------------------------------------
_MONTH_WORDS = {"янв": 1, "фев": 2, "мар": 3, "апр": 4, "май": 5, "мая": 5, "июн": 6, "июл": 7, "авг": 8, "сен": 9, "окт": 10, "ноя": 11, "дек": 12}
_MONTH_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"]
POST_TOL_MIN = 5   # допуск поиска поста: плюс-минус столько минут

def _month_from_word(w: str):
    w = (w or "").lower().replace("ё", "е")
    for k, v in _MONTH_WORDS.items():
        if w.startswith(k):
            return v
    return None

def parse_post_time(raw: str, now: datetime):
    """Понимает время и дату в любом порядке и с любыми разделителями: 18:45 5.02, 5 февраля 18:45, 18-45 05,02, вчера 18:45.
    Возвращает словарь или None, если время распознать нельзя. now: текущее время по МСК (aware)."""
    s = (raw or "").lower().replace("ё", "е")
    s = re.sub(r"\b(мск|msk|по москве|москва)\b", " ", s)
    s = re.sub(r"\b(года?|г)\b\.?", " ", s)
    s = re.sub(r"[;,]", " ", s)
    rel = None
    for word, off in (("позавчера", 2), ("вчера", 1), ("сегодня", 0)):
        if word in s:
            rel = off
            s = s.replace(word, " ")
            break
    hour = minute = None
    sec = None
    day = month = year = None

    m = re.search(r"(\d{1,2})\s*([а-я]{3,9})", s)
    if m and _month_from_word(m.group(2)):
        day, month = int(m.group(1)), _month_from_word(m.group(2))
        s = s[:m.start()] + " " + s[m.end():]
    else:
        m = re.search(r"([а-я]{3,9})\s*(\d{1,2})(?!\d)", s)
        if m and _month_from_word(m.group(1)):
            day, month = int(m.group(2)), _month_from_word(m.group(1))
            s = s[:m.start()] + " " + s[m.end():]
    m = re.search(r"(?<!\d)(\d{1,2})\s*:\s*(\d{2})(?:\s*:\s*(\d{2}))?(?!\d)", s)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
        sec = int(m.group(3)) if m.group(3) else None
        s = s[:m.start()] + " " + s[m.end():]
    else:
        m = re.search(r"(?<!\d)(\d{1,2})\s*ч\w*\s*(\d{1,2})?\s*(?:м\w*)?", s)
        if m:
            hour, minute = int(m.group(1)), int(m.group(2) or 0)
            s = s[:m.start()] + " " + s[m.end():]
    nums = [int(x) for x in re.findall(r"\d+", s)]
    longs = [x for x in re.findall(r"\d+", s) if len(x) > 2]
    if hour is None:
        # время без двоеточия: 18.45, 18-45, 18 45; дата может стоять перед ним или после
        small = re.findall(r"\d{1,2}(?!\d)", " ".join(re.findall(r"\d+", s))) if not longs else []
        small = [int(x) for x in re.findall(r"\b\d{1,2}\b", s)]
        if len(small) >= 2 and small[0] <= 23 and small[1] <= 59:
            hour, minute = small[0], small[1]
            nums = [int(x) for x in re.findall(r"\d+", s)][2:]
        elif len(small) >= 4 and small[-2] <= 23 and small[-1] <= 59:
            hour, minute = small[-2], small[-1]
            nums = [int(x) for x in re.findall(r"\d+", s)][:-2]
        else:
            return None
    if day is None:
        if len(nums) == 1 and nums[0] <= 31:
            day, month = nums[0], now.month
        elif len(nums) in (2, 3):
            a, b = nums[0], nums[1]
            day, month = a, b
            if month > 12 and day <= 12:
                day, month = b, a
            if len(nums) == 3:
                y = nums[2]
                year = y + 2000 if y < 100 else y
        elif len(nums) > 3:
            return None
    else:
        for x in nums:
            if x >= 100 or 20 <= x <= 99 and year is None and len(nums) == 1:
                year = x + 2000 if x < 100 else x
    if not (0 <= hour <= 23 and 0 <= minute <= 59 and (sec is None or 0 <= sec <= 59)):
        return None
    tz = now.tzinfo
    date_given = day is not None
    assumed = None
    try:
        if date_given:
            y = year or now.year
            dt = datetime(y, month, day, hour, minute, sec or 0, tzinfo=tz)
            if not year and dt > now + timedelta(days=30):   # дата «из будущего» на пару дней — опечатка (покажем предупреждение), на месяцы — прошлый год
                dt = dt.replace(year=y - 1)
        else:
            base = (now - timedelta(days=rel)).date() if rel is not None else now.date()
            dt = datetime(base.year, base.month, base.day, hour, minute, sec or 0, tzinfo=tz)
            if rel is None:
                assumed = "today"
                if dt > now + timedelta(minutes=2):
                    dt -= timedelta(days=1)
                    assumed = "yesterday"
            else:
                date_given = True
    except ValueError:
        return None
    return {"dt": dt, "date_given": date_given, "assumed": assumed, "has_sec": sec is not None, "future": dt > now + timedelta(minutes=5)}

def _fmt_pt(dt: datetime, with_sec: bool = False) -> str:
    return f"{dt.hour:02d}:{dt.minute:02d}" + (f":{dt.second:02d}" if with_sec else "") + f", {dt.day} {_MONTH_GEN[dt.month - 1]} {dt.year}"

def _fmt_pt_short(dt: datetime, with_sec: bool = False) -> str:
    return f"{dt.hour:02d}:{dt.minute:02d}" + (f":{dt.second:02d}" if with_sec else "") + f" {dt.day:02d}.{dt.month:02d}.{dt.year}"

def _time_prompt() -> str:
    now = datetime.now(MSK_TZ)
    return (
        f"{ICON_TIMER} <b>Укажите точное время публикации поста по МСК:</b>\n"
        "Пишите в формате <code>чч:мм дд.мм</code>, например <code>18:45 28.10</code>. Секунды можно не указывать.\n\n"
        "⚠️ Время на посте в Telegram показывается по <b>вашему</b> часовому поясу. Если он не московский, пересчитайте на Москву (UTC+3).\n"
        f"<i>Сейчас по МСК: {_fmt_pt(now)}</i>"
    )

def _time_confirm_kb(prefix: str, parsed: bool = True):
    return InlineKeyboardMarkup(inline_keyboard=[
        [IB(text="✅ Да, это время по МСК", callback_data=f"{prefix}time_ok")],
        [IB(text="✏️ Ввести заново", callback_data=f"{prefix}time_re")],
    ])

_POST_KIND_TITLES = {"secret": "секретка", "saboteur": "поиск саботёра", "farm": "фарм сервер", "researcher": "бейдж исследователя", "mutator": "бейдж мутатора"}

def _channel_post_link(channel_id, message_id) -> str:
    if not message_id:
        return ""
    cid = str(channel_id or "")
    if cid.startswith("@"):
        return f"https://t.me/{cid[1:]}/{message_id}"
    if cid.startswith("-100"):
        return f"https://t.me/c/{cid[4:]}/{message_id}"
    return ""

_KIND_PLACE = {"secret": "Сервер с секреткой", "saboteur": "Поиск саботера", "farm": "Фарм сервер",
               "researcher": "Бейдж исследователь", "mutator": "Бейдж мутатор"}
_KIND_BTN = {"secret": "Секретка", "saboteur": "Поиск саботёра", "farm": "Фарм сервер", "researcher": "Бейдж исследователя", "mutator": "Бейдж мутатора"}
_KIND_DESC = {
    "secret": "<b>Секретка</b>: секретки, где падают лапки и прочее, а также фарм-секретка, где башня наклонена или имеет Т/Г-форму.",
    "farm": "<b>Фарм сервер</b>: отдельный сервер, где уровни по 12–18 штук без лазеров.",
    "saboteur": "<b>Поиск саботёра</b>: сервер, где ищут секретку саботёра для получения карминового нимба.",
    "researcher": "<b>Бейдж исследователь</b>: сервер для получения бейджа исследователя.",
    "mutator": "<b>Бейдж мутатор</b>: сервер для получения бейджа мутатора.",
}

async def find_posts_other_kinds(dt: datetime, with_sec: bool, exclude: str) -> dict:
    """Посты ДРУГИХ типов в том же окне: {тип: [посты]}. Нужны, когда игрок перепутал тип (в одну минуту два поста не выходят)."""
    out = {}
    for k in _KIND_PLACE:
        if k == exclude:
            continue
        try:
            r = await find_posts_near(k, dt, with_sec)
        except Exception as e:
            logging.warning(f"Поиск поста другого типа ({k}): {e}")
            continue
        if r:
            out[k] = r
    return out

async def find_posts_near(kind: str, dt: datetime, with_sec: bool) -> list:
    """Посты выбранного типа в окне ±POST_TOL_MIN минут. Возвращает до 4 штук, ближайшие первыми."""
    lo = dt - timedelta(minutes=POST_TOL_MIN)
    hi = dt + timedelta(minutes=POST_TOL_MIN, seconds=0 if with_sec else 59)
    queries = {
        "secret": "SELECT user_id, created_at, channel_id, message_id FROM secret_posts WHERE created_at BETWEEN %s AND %s "
                  "ORDER BY ABS(EXTRACT(EPOCH FROM (created_at - %s))) LIMIT 4",
        "saboteur": "SELECT s.user_id, s.created_at, p.channel_id, p.message_id FROM saboteur_servers s JOIN saboteur_posts p ON p.id = s.post_id "
                    "WHERE s.created_at BETWEEN %s AND %s ORDER BY ABS(EXTRACT(EPOCH FROM (s.created_at - %s))) LIMIT 4",
        "farm": "SELECT s.user_id, s.created_at, p.channel_id, p.message_id FROM farm_servers s JOIN farm_posts p ON p.id = s.post_id "
                "WHERE s.created_at BETWEEN %s AND %s ORDER BY ABS(EXTRACT(EPOCH FROM (s.created_at - %s))) LIMIT 4",
        "researcher": "SELECT user_id, created_at, channel_id, message_id FROM researcher_badges WHERE created_at BETWEEN %s AND %s "
                      "ORDER BY ABS(EXTRACT(EPOCH FROM (created_at - %s))) LIMIT 4",
        "mutator": "SELECT user_id, created_at, channel_id, message_id FROM mutator_badges WHERE created_at BETWEEN %s AND %s "
                   "ORDER BY ABS(EXTRACT(EPOCH FROM (created_at - %s))) LIMIT 4",
    }
    q = queries.get(kind)
    if not q:
        return []
    rows = await _db_all(q, (lo, hi, dt))
    res = []
    for uid, ts, ch, mid in rows:
        gid = None
        if kind in ("secret", "saboteur", "farm"):
            try:
                g = await _db_one(
                    "SELECT group_msg_id FROM post_log WHERE kind = %s AND user_id = %s AND ABS(EXTRACT(EPOCH FROM (created_at - %s))) < 180 "
                    "ORDER BY ABS(EXTRACT(EPOCH FROM (created_at - %s))) LIMIT 1", (kind, uid, ts, ts))
                gid = g[0] if g else None
            except Exception as e:
                logging.warning(f"Поиск поста: журнал публикаций: {e}")
        group_link = f"https://t.me/c/{str(ADMIN_CHAT_ID)[4:]}/{TOPIC_SECRETS_ID}/{gid}" if gid and str(ADMIN_CHAT_ID).startswith("-100") else ""
        res.append({"user_id": int(uid) if uid else None, "ts": ts.astimezone(MSK_TZ), "group": group_link, "channel": _channel_post_link(ch, mid)})
    return res

async def plog(kind: str, user_id, group_msg_id):
    """Запоминает карточку публикации в теме «Секретки»: по ней потом находим пост и публикатора. Не бросает исключений."""
    try:
        if group_msg_id:
            await _db_exec("INSERT INTO post_log (kind, user_id, group_msg_id) VALUES (%s,%s,%s)", (kind, int(user_id), int(group_msg_id)))
    except Exception as e:
        logging.warning(f"Журнал публикаций: не записано: {e}")

async def _build_time_result(raw: str, parsed: dict, kind: str, note: str = "", others: dict = None) -> tuple:
    """Возвращает (html для карточки заявки, post_info для dispatch_ticket_to_admin).
    note: пометка о выборе игрока по типу; others: посты других типов в том же окне (если игрок настоял на своём типе)."""
    if not parsed:
        return (f"<b>{html.escape(raw)}</b>\n⚠️ <i>Время не распознано. Проверьте вручную.</i>", {"nopost": False})
    dt = parsed["dt"]
    line = f"<b>{_fmt_pt_short(dt, parsed['has_sec'])} (МСК)</b>"
    if parsed["assumed"]:
        line += " <i>(дату игрок не указал, считаем " + ("вчера" if parsed["assumed"] == "yesterday" else "сегодня") + ")</i>"
    if raw.strip():
        line += f"\n<i>Как написал игрок: «{html.escape(raw.strip()[:80])}»</i>"
    try:
        found = await find_posts_near(kind, dt, parsed["has_sec"])
    except Exception as e:
        logging.warning(f"Поиск поста по времени: {e}")
        return (line + "\n⚠️ <i>Автопоиск поста не сработал, проверьте вручную.</i>", {"nopost": False})
    title = _POST_KIND_TITLES.get(kind, kind)
    if note:
        line += f"\n{note}"
    if not found:
        line += (f"\n⚠️ <b>Пост типа «{html.escape(title)}» в это время (±{POST_TOL_MIN} мин) не найден.</b> "
                 "Возможно, игрок ошибся со временем или поясом. Поищите сами; если поста нет, нажмите «🚫 Нет такого поста» — игроку уйдёт готовый отказ.")
        for ok_, lst in (others or {}).items():
            c = lst[0]
            who = await _mention_html(c["user_id"]) if c["user_id"] else "неизвестно"
            lk = c["group"] or c["channel"]
            line += (f"\n↪️ <b>Но в это время вышел пост другого типа: «{html.escape(_POST_KIND_TITLES.get(ok_, ok_))}»</b> "
                     f"({c['ts'].strftime('%H:%M:%S')}, 👤 {who})" + (f' · <a href="{lk}">ссылка</a>' if lk else "")
                     + ". Игрок подтвердил свой тип, проверьте, не перепутал ли он.")
        return (line, {"nopost": True})
    line += f"\n🔎 <b>Найдено постов: {len(found)}</b> (±{POST_TOL_MIN} мин):"
    for c in found[:3]:
        who = await _mention_html(c["user_id"]) if c["user_id"] else "неизвестно"
        links = []
        if c["group"]:
            links.append(f'<a href="{c["group"]}">карточка в чате</a>')
        if c["channel"]:
            links.append(f'<a href="{c["channel"]}">пост в канале</a>')
        line += f"\n• {c['ts'].strftime('%H:%M:%S')} · 👤 {who}" + (" · " + " · ".join(links) if links else "")
    info = {"nopost": False}
    if len(found) == 1 and found[0]["user_id"]:
        info["ping"] = found[0]["user_id"]
    return (line, info)

async def _time_received(message: Message, state: FSMContext, prefix: str):
    raw = (message.text or "").strip()
    parsed = parse_post_time(raw, datetime.now(MSK_TZ))
    await state.update_data(**{f"{prefix}_time": raw, f"{prefix}_time_iso": parsed["dt"].isoformat() if parsed else "",
                               f"{prefix}_time_sec": bool(parsed and parsed["has_sec"])})
    now = datetime.now(MSK_TZ)
    if not parsed:
        await message.answer(
            f"🤔 Не получилось понять «{html.escape(raw[:80])}».\n\n"
            "Напишите, пожалуйста, <b>точное время</b> в формате <code>чч:мм дд.мм</code>, например <code>18:45 28.10</code>. "
            "Нужны часы, минуты и дата. Слова вроде «вечером» или «недавно» не подходят: по минуте мы ищем пост.\n"
            f"<i>Сейчас по МСК: {_fmt_pt(now)}</i>", parse_mode="HTML")
        return
    if parsed["future"]:
        await message.answer(
            f"⏳ Время <b>{_fmt_pt(parsed['dt'], parsed['has_sec'])}</b> ещё не наступило, пост с таким временем выйти не мог.\n\n"
            "Проверьте число, месяц и часы и напишите заново: <code>чч:мм дд.мм</code>.\n"
            f"<i>Сейчас по МСК: {_fmt_pt(now)}</i>", parse_mode="HTML")
        return
    else:
        extra = ""
        if parsed["assumed"] == "yesterday":
            extra = "\n📅 Дату вы не указали, а это время сегодня ещё не наступило, поэтому беру <b>вчера</b>. Если пост был в другой день, введите заново с датой."
        elif parsed["assumed"] == "today":
            extra = "\n📅 Дату вы не указали, беру <b>сегодня</b>. Если пост был в другой день, введите заново с датой."
        text = (f"🕒 Вы указали: <b>{_fmt_pt(parsed['dt'], parsed['has_sec'])}</b>{extra}\n\n"
                "❓ <b>Вы уверены, что это время по МСК (Москва, UTC+3)?</b>\n"
                "Время на посте в Telegram показывается по вашему часовому поясу. От точной минуты зависит, найдём ли мы пост.\n"
                f"<i>Сейчас по МСК: {_fmt_pt(now)}</i>")
    await message.answer(text, reply_markup=_time_confirm_kb(prefix, bool(parsed)), parse_mode="HTML")

async def _parsed_from_state(data: dict, prefix: str):
    raw = data.get(f"{prefix}_time", "")
    iso = data.get(f"{prefix}_time_iso", "")
    if not iso:
        return raw, None
    try:
        parsed = parse_post_time(raw, datetime.now(MSK_TZ))
        if parsed:
            parsed["dt"] = datetime.fromisoformat(iso)
            parsed["has_sec"] = bool(data.get(f"{prefix}_time_sec"))
        return raw, parsed
    except Exception:
        return raw, None

async def _time_ok(call: CallbackQuery, state: FSMContext, prefix: str, kind_decision: str = "") -> bool:
    """True: время принято, можно идти дальше. False: нужен ещё ответ игрока (непонятное время или уточнение типа поста)."""
    data = await state.get_data()
    raw, parsed = await _parsed_from_state(data, prefix)
    if not parsed:
        try:
            await call.message.edit_text(
                f"🤔 Время «{html.escape(raw[:80])}» не распознано. Напишите его точно в формате <code>чч:мм дд.мм</code>, например <code>18:45 28.10</code>.",
                parse_mode="HTML")
        except Exception:
            pass
        return False
    kind = data.get(f"{prefix}_kind", "")
    note, others = "", None
    try:
        found = await find_posts_near(kind, parsed["dt"], parsed["has_sec"]) if kind in _KIND_PLACE else [1]
    except Exception as e:
        logging.warning(f"Проверка типа поста: {e}")
        found = [1]
    if not found and not kind_decision and kind in _KIND_PLACE:
        others = await find_posts_other_kinds(parsed["dt"], parsed["has_sec"], kind)
        if others:
            # пост в это время есть, но другого типа: в одну минуту два поста не выходят, значит игрок мог перепутать тип
            lines = [f"❓ Вы выбрали тип «<b>{html.escape(_KIND_BTN[kind])}</b>», но в это время (±{POST_TOL_MIN} мин по МСК) такого поста нет. "
                     "Зато вышел пост другого типа: " + ", ".join(f"«<b>{html.escape(_KIND_BTN[k])}</b>»" for k in others) + ".", "",
                     "Чем отличаются типы:", "• " + _KIND_DESC[kind]]
            lines += ["• " + _KIND_DESC[k] for k in others]
            lines += ["", f"Вы уверены, что это именно «{html.escape(_KIND_BTN[kind])}»?"]
            rows = [[IB(text=f"✅ Да, это «{_KIND_BTN[kind]}»", callback_data=f"{prefix}kind_keep")]]
            for k in list(others)[:3]:
                rows.append([IB(text=f"🔁 Нет, это «{_KIND_BTN[k]}»", callback_data=f"{prefix}kind_to_{k}")])
            rows.append([IB(text="✏️ Изменить время", callback_data=f"{prefix}time_re")])
            try:
                await call.message.edit_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML")
            except Exception as e:
                logging.warning(f"Не удалось показать уточнение типа: {e}")
                await call.message.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML")
            return False
    if kind_decision == "keep":
        others = await find_posts_other_kinds(parsed["dt"], parsed["has_sec"], kind)
        if others:
            note = "🗣 <i>Игрок подтвердил выбранный тип после подсказки.</i>"
    elif kind_decision.startswith("to_"):
        new_kind = kind_decision[3:]
        if new_kind in _KIND_PLACE and new_kind != kind:
            old_kind = kind
            kind = new_kind
            await state.update_data(**{f"{prefix}_kind": kind, f"{prefix}_place": _KIND_PLACE[kind]})
            note = f"🔁 <i>Игрок сначала выбрал «{html.escape(_KIND_BTN.get(old_kind, old_kind))}», но после подсказки сменил тип на «{html.escape(_KIND_BTN[kind])}».</i>"
    txt, info = await _build_time_result(raw, parsed, kind, note=note, others=others)
    await state.update_data(**{f"{prefix}_time_txt": txt, f"{prefix}_post_info": info})
    try:
        await call.message.edit_text(f"{ICON_CHECK} Время принято: <b>{html.escape(raw[:80])}</b> (по МСК)."
                                     + (f"\nТип: <b>{html.escape(_KIND_PLACE[kind])}</b>." if kind_decision.startswith("to_") else ""), parse_mode="HTML")
    except Exception:
        pass
    return True

async def _time_again(call: CallbackQuery):
    try:
        await call.message.edit_text(_time_prompt(), parse_mode="HTML")
    except Exception:
        await call.message.answer(_time_prompt(), parse_mode="HTML")

async def _c_next(call: CallbackQuery, state: FSMContext):
    await state.set_state(Form.complaint_photos)
    await call.message.answer(
        f"{NUM_5} {ICON_PHOTO} <b>Прикрепите доказательства (до 5 фото или видео):</b>\n\n"
        "⚠️ <b>Важно:</b> фото строго запрещено обрезать! Плашка уровней и чат сервера должны быть отчётливо видны.",
        reply_markup=skip_photo_kb("complaint"), parse_mode="HTML")

async def _a_next(call: CallbackQuery, state: FSMContext):
    await state.set_state(Form.appeal_reason)
    await call.message.answer(
        f"{NUM_4} <b>Почему мы должны снять с вас бан?</b>\n"
        "<i>(Подробно опишите вашу ситуацию и почему вы считаете блокировку ошибочной)</i>",
        parse_mode="HTML")

@router.callback_query(F.data == "ctime_ok", Form.complaint_time)
async def ctime_ok(call: CallbackQuery, state: FSMContext):
    if await _time_ok(call, state, "c"):
        await _c_next(call, state)
    await call.answer()

@router.callback_query(F.data.regexp(r"^ckind_(keep|to_[a-z]+)$"), Form.complaint_time)
async def ckind_choice(call: CallbackQuery, state: FSMContext):
    decision = call.data[len("ckind_"):]
    if await _time_ok(call, state, "c", kind_decision=decision):
        await _c_next(call, state)
    await call.answer()

@router.callback_query(F.data == "ctime_re", Form.complaint_time)
async def ctime_re(call: CallbackQuery):
    await _time_again(call)
    await call.answer()

@router.callback_query(F.data == "atime_ok", Form.appeal_time)
async def atime_ok(call: CallbackQuery, state: FSMContext):
    if await _time_ok(call, state, "a"):
        await _a_next(call, state)
    await call.answer()

@router.callback_query(F.data.regexp(r"^akind_(keep|to_[a-z]+)$"), Form.appeal_time)
async def akind_choice(call: CallbackQuery, state: FSMContext):
    decision = call.data[len("akind_"):]
    if await _time_ok(call, state, "a", kind_decision=decision):
        await _a_next(call, state)
    await call.answer()

@router.callback_query(F.data == "atime_re", Form.appeal_time)
async def atime_re(call: CallbackQuery):
    await _time_again(call)
    await call.answer()

NOPOST_REASON = ("Указанный вами пост не найден: в это время по МСК такой публикации не было. "
                 "Проверьте время и подайте заявку заново. Время нужно писать точно и по МСК в формате чч:мм дд.мм (например 18:45 28.10).")

@router.callback_query(F.data.startswith("nopost_"))
async def nopost_handler(call: CallbackQuery):
    try:
        ticket_id = int(call.data.split("_")[1])
    except (IndexError, ValueError):
        await call.answer(); return
    if call.message.chat.id != ADMIN_CHAT_ID and not await is_support_member(call.from_user.id):
        await call.answer("❌ У вас нет прав хелпера!", show_alert=True)
        return
    info = await get_ticket_info(ticket_id)
    if not info:
        await call.answer("❌ Заявка не найдена в базе!", show_alert=True)
        return
    user_id, adm_id, status, card = info
    if status not in ("pending", "active"):
        await call.answer("❌ Заявка уже обработана!", show_alert=True)
        return
    if status == "active" and adm_id != call.from_user.id and not await is_main_admin(call.from_user.id):
        await call.answer("❌ Эту заявку ведёт другой админ.", show_alert=True)
        return
    await call.answer("Отказ отправлен игроку.")
    agent_no = await get_or_create_agent_number(call.from_user.id)
    await close_ticket_db(ticket_id, "rejected", admin_id=call.from_user.id)
    await alog(call.from_user.id, "ticket_reject", f"№{ticket_id}", "пост не найден (автоотказ)")
    await tlog(ticket_id, "admin", call.from_user.id, f"Причина отказа: {NOPOST_REASON}")
    try:
        await bot.send_message(user_id, f"{ICON_CROSS} Заявка <b>№{ticket_id}</b> отклонена.\n<b>Причина:</b> {html.escape(NOPOST_REASON)}",
                               parse_mode="HTML", reply_markup=await main_keyboard(user_id))
    except Exception as e:
        logging.warning(f"Не удалось отправить автоотказ пользователю {user_id}: {e}")
    base = card or call.message.caption or call.message.text or f"Заявка <b>№{ticket_id}</b>"
    new_text = (f"{base}\n\n🔴 <b>Заявка №{ticket_id} отклонена</b> администратором {call.from_user.mention_html()} (Агент #{agent_no}).\n"
                "<b>Причина:</b> пост не найден (автоответ).")
    try:
        if call.message.photo or call.message.video:
            await call.message.edit_caption(caption=html_cut(new_text, 1024), reply_markup=None, parse_mode="HTML")
        else:
            await call.message.edit_text(text=html_cut(new_text, 4096), reply_markup=None, parse_mode="HTML")
    except Exception as e:
        logging.error(f"Не удалось обновить карточку после автоотказа №{ticket_id}: {e}")

@router.message(Form.complaint_time, F.text)
async def process_c_time(message: Message, state: FSMContext):
    await _time_received(message, state, "c")


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
            f"Время публикации поста: {(data.get('c_time_txt') or html.escape(data.get('c_time', '')))}"
        )
        ticket_id = await dispatch_ticket_to_admin(call.from_user.id, call.from_user, "complaint", text, [], post_info=data.get("c_post_info"))
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
                f"Где произошло нарушение: {html.escape(cur_data.get('c_place', ''))}\n"
                f"Время публикации поста: {(cur_data.get('c_time_txt') or html.escape(cur_data.get('c_time', '')))}"
            )
            ticket_id = await dispatch_ticket_to_admin(message.from_user.id, message.from_user, "complaint", text, photos, post_info=cur_data.get("c_post_info"))
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
            f"Время публикации поста: {(data.get('c_time_txt') or html.escape(data.get('c_time', '')))}"
        )
        ticket_id = await dispatch_ticket_to_admin(message.from_user.id, message.from_user, "complaint", text, photos, post_info=data.get("c_post_info"))
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
        "app_srv_farm": "Фарм сервер",
        "app_srv_researcher": "Бейдж исследователь",
        "app_srv_mutator": "Бейдж мутатор"
    }
    chosen_server = srv_types.get(call.data, "Не указан")
    await state.update_data(a_place=chosen_server, a_kind=call.data[8:])
    await state.set_state(Form.appeal_time)
    await call.message.edit_text(
        f"Выбран сервер: <b>{chosen_server}</b>{_rules_hint(call.data[8:], 'appeal')}\n\n"
        f"{NUM_3} {_time_prompt()}",
        parse_mode="HTML", link_preview_options=LinkPreviewOptions(is_disabled=True)
    )
    await call.answer()

@router.message(Form.appeal_time, F.text)
async def process_a_time(message: Message, state: FSMContext):
    await _time_received(message, state, "a")

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
            f"Время публикации поста: {(data.get('a_time_txt') or html.escape(data.get('a_time', '')))}\n"
            f"Причина разбана: {html.escape(data.get('a_reason', ''))}"
        )
        ticket_id = await dispatch_ticket_to_admin(call.from_user.id, call.from_user, "appeal", text, [], post_info=data.get("a_post_info"))
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
                f"Время публикации поста: {(cur_data.get('a_time_txt') or html.escape(cur_data.get('a_time', '')))}\n"
                f"Причина разбана: {html.escape(cur_data.get('a_reason', ''))}"
            )
            ticket_id = await dispatch_ticket_to_admin(message.from_user.id, message.from_user, "appeal", text, photos, post_info=cur_data.get("a_post_info"))
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
            f"Время публикации поста: {(data.get('a_time_txt') or html.escape(data.get('a_time', '')))}\n"
            f"Причина разбана: {html.escape(data.get('a_reason', ''))}"
        )
        ticket_id = await dispatch_ticket_to_admin(message.from_user.id, message.from_user, "appeal", text, photos, post_info=data.get("a_post_info"))
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
    Form.secret_name, Form.secret_age, Form.secret_vip_date, Form.secret_vip_until, Form.secret_time, Form.secret_why,
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
    await alog(call.from_user.id, "ticket_take", f"№{ticket_id}")
    await tlog(ticket_id, "system", call.from_user.id, "taken")
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
    _np = await _ticket_nopost(ticket_id)
    
    try:
        if call.message.photo or call.message.video:
            await call.message.edit_caption(caption=new_text, reply_markup=close_ticket_kb(ticket_id, call.from_user.id, nopost=_np), parse_mode="HTML")
        else:
            await call.message.edit_text(text=new_text, reply_markup=close_ticket_kb(ticket_id, call.from_user.id, nopost=_np), parse_mode="HTML")
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
    await alog(call.from_user.id, "ticket_close", f"№{ticket_id}",
               "" if call.from_user.id == assigned_admin_id else "закрыл чужое обращение")

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

@router.callback_query(F.data.startswith("solved_"))
async def user_mark_solved(call: CallbackQuery):
    try:
        ticket_id = int(call.data.split("_")[1])
    except (IndexError, ValueError):
        await call.answer()
        return
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
    await close_ticket_db(ticket_id, 'closed')
    await mark_ticket_card_closed(ticket_id, f"✅ <b>Заявка №{ticket_id} закрыта: пользователь отметил вопрос решённым.</b>")
    try:
        sent = await bot.send_message(
            ADMIN_CHAT_ID, f"✅ Заявка №{ticket_id} закрыта: пользователь отметил, что вопрос решён.",
            message_thread_id=TOPIC_SUPPORT_ID, parse_mode="HTML"
        )
        await map_message(sent.message_id, info[0], ticket_id)
    except Exception as e:
        logging.warning(f"Не удалось уведомить админ-чат о решённой заявке №{ticket_id}: {e}")
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    try:
        user_kb = await main_keyboard(call.from_user.id)
        await bot.send_message(
            call.from_user.id,
            f"{ICON_CHECK} Рады, что вопрос решён! Заявка <b>№{ticket_id}</b> закрыта.\nОцените, пожалуйста, работу поддержки:",
            reply_markup=rating_kb(ticket_id), parse_mode="HTML"
        )
    except Exception as e:
        logging.warning(f"Не удалось отправить оценку по заявке №{ticket_id}: {e}")

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
            await call.message.edit_text(
                f"{ICON_STAR} Спасибо за оценку ({score}/5)! Ваше мнение учтено.\n"
                "Если хотите рассказать, что понравилось или что улучшить, нажмите кнопку ниже (это необязательно).",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[IB(text="💬 Написать отзыв", callback_data=f"rfb_{ticket_id}")]]),
                parse_mode="HTML"
            )
        except TelegramBadRequest:
            pass

_RFB_CANCEL_KB = InlineKeyboardMarkup(inline_keyboard=[[IB(text="Отмена", callback_data="rfbx")]])

@router.callback_query(F.data.regexp(r"^rfb_\d+$"))
async def rating_feedback_start(call: CallbackQuery, state: FSMContext):
    ticket_id = int(call.data.split("_")[1])
    row = await _db_one("SELECT user_id, review FROM ratings WHERE ticket_id = %s", (ticket_id,))
    if not row or row[0] != call.from_user.id:
        await call.answer("Оценка не найдена.", show_alert=True)
        return
    if row[1]:
        await call.answer("Отзыв уже отправлен, спасибо!", show_alert=True)
        return
    await state.clear()
    await state.set_state(Form.rating_comment)
    await state.update_data(rfb_ticket=ticket_id)
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await call.message.answer(
        "💬 Напишите одним сообщением, что понравилось или что стоит улучшить (до 500 символов).",
        reply_markup=_RFB_CANCEL_KB
    )
    await call.answer()

@router.callback_query(F.data == "rfbx")
async def rating_feedback_cancel(call: CallbackQuery, state: FSMContext):
    if await state.get_state() == Form.rating_comment.state:
        await state.clear()
    try:
        await call.message.edit_text("Хорошо, без отзыва. Спасибо за оценку!")
    except Exception:
        pass
    await call.answer()

@router.message(Form.rating_comment, F.text, F.chat.type == "private")
async def rating_feedback_save(message: Message, state: FSMContext):
    data = await state.get_data()
    ticket_id = data.get("rfb_ticket")
    await state.clear()
    txt = (message.text or "").strip()
    if not ticket_id or not txt or txt.startswith("/"):
        await message.answer("Хорошо, без отзыва. Спасибо за оценку!")
        return
    txt = txt[:500]
    row = await _db_one(
        "UPDATE ratings SET review = %s, review_at = CURRENT_TIMESTAMP "
        "WHERE ticket_id = %s AND user_id = %s AND (review IS NULL OR review = '') RETURNING score, admin_id",
        (txt, ticket_id, message.from_user.id)
    )
    if not row:
        await message.answer("Не удалось сохранить отзыв: оценка не найдена или отзыв уже отправлен.")
        return
    await message.answer("🙏 Спасибо! Ваш отзыв передан команде.")
    agent = await _db_one("SELECT agent_number FROM admin_agents WHERE admin_id = %s", (row[1],)) if row[1] else None
    agent_txt = f"Агент #{agent[0]}" if agent and agent[0] else "агент не указан"
    note = (
        f"💬 <b>Отзыв по заявке №{ticket_id}</b> · оценка {row[0]}/5 · {agent_txt}\n\n"
        f"<i>{html.escape(txt)}</i>"
    )
    for aid in await get_main_admin_ids():
        try:
            await bot.send_message(aid, note, parse_mode="HTML")
        except Exception as e:
            logging.warning(f"Не удалось отправить отзыв админу {aid}: {e}")

@router.message(Form.rating_comment, F.chat.type == "private")
async def rating_feedback_not_text(message: Message):
    await message.answer("Отправьте отзыв текстом или нажмите «Отмена».", reply_markup=_RFB_CANCEL_KB)

# ----------------------------------------------------------------------
# ДИАЛОГ В ЛС С ПОЛЬЗОВАТЕЛЕМ (ОТПРАВКА В ТЕМУ ПОДДЕРЖКИ)
# ----------------------------------------------------------------------
async def user_private_message(message: Message, state: FSMContext):
    await register_user(message.from_user.id)
    if await is_banned(message.from_user.id): return

    current_state = await state.get_state()
    if current_state is not None:
        return

    active_ticket = await get_active_ticket(message.from_user.id)
    if active_ticket and active_ticket[2] == 'active':
        await touch_ticket(active_ticket[0], actor='user')
        await tlog(active_ticket[0], "user", message.from_user.id, message.text or message.caption or "",
                   media=("" if message.text else _ctype(message)))
        user_mention = get_user_mention(message.from_user)
        header = f"📩 <b>Сообщение по заявке №{active_ticket[0]} от {user_mention} | ID: <code>{message.from_user.id}</code>:</b>"
        try:
            ids = await relay_message(message, ADMIN_CHAT_ID, header, message.text or message.caption or "", thread_id=TOPIC_SUPPORT_ID)
        except Exception as e:
            logging.error(f"Заявка №{active_ticket[0]}: не удалось переслать сообщение игрока ({_ctype(message)}): {e}")
            try:
                await bot.send_message(ADMIN_CHAT_ID, header + f"\n\n⚠️ Вложение ({_ctype(message)}) переслать не удалось. Игрок его отправил, напишите ему в личку, если нужно.\n"
                                       + html.escape(message.text or message.caption or ""), parse_mode="HTML", message_thread_id=TOPIC_SUPPORT_ID)
            except Exception:
                pass
            return
        for mid in ids:
            await map_message(mid, message.from_user.id, active_ticket[0])
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
    kb = take_ticket_kb(ticket_id, nopost=await _ticket_nopost(ticket_id))
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
    await tlog(ticket_id, "system", adm_id, "transferred")   # кто вёл и отдал; новый хелпер появится записью «взял»

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
        if len(parts) > 1:
            _ids, _bad = await resolve_user_refs(parts[1], require_at=True)
            if len(_ids) == 1 and not _bad:
                user_id = _ids[0]
                if len(parts) > 2:
                    reason = parts[2].strip()

    if not user_id:
        await message.answer(
            f"{ICON_CROSS} <b>Не удалось определить пользователя.</b>\n\n"
            "• Ответьте командой <code>/ban [причина]</code> на карточку тикета или сообщение игрока.\n"
            "• Либо введите напрямую: <code>/ban [ID или @юзернейм] [причина]</code>",
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
    await alog(message.from_user.id, "ban", f"ID {user_id}", reason)

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
        if len(args) > 1:
            _ids, _bad = await resolve_user_refs(args[1].split()[0], require_at=True)
            if len(_ids) == 1 and not _bad:
                user_id = _ids[0]

    if not user_id:
        await message.answer(
            f"{ICON_CROSS} <b>Не удалось определить пользователя.</b>\n\n"
            "• Ответьте командой <code>/unban</code> на карточку тикета или сообщение игрока.\n"
            "• Либо введите напрямую: <code>/unban [ID или @юзернейм]</code>",
            parse_mode="HTML"
        )
        return

    await unban_user_db(user_id)
    await alog(message.from_user.id, "unban", f"ID {user_id}")
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
                await alog(message.from_user.id, "ticket_reject", f"№{pending_ticket_id}", text or "Без причины")
                await tlog(pending_ticket_id, "admin", message.from_user.id, f"Причина отказа: {text or 'Без причины'}")

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

                client_head = f"👨‍💻 <b>Ответ поддержки (Агент #{agent_no}):</b>"
                try:
                    await relay_message(message, user_id, client_head, text, reply_markup=solved_kb(ticket_id))
                    await message.react([{"type": "emoji", "emoji": "👍"}])
                    await map_message(message.message_id, user_id, ticket_id)
                    await tlog(ticket_id, "admin", message.from_user.id, text or "", media=("" if message.text else _ctype(message)))
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

_userbot_alert = {"bad": False, "last": 0.0}

async def userbot_watchdog_worker():
    """Раз в 10 минут проверяет юзербота; при сбое сообщает владельцу (повтор не чаще раза в 6 часов)."""
    logging.info("Сторож юзербота запущен.")
    await asyncio.sleep(60)
    while True:
        try:
            if userbot:
                err = None
                try:
                    if not userbot.is_connected():
                        await userbot.connect()
                    if not await userbot.is_user_authorized():
                        err = "сессия недействительна, нужна новая USER_SESSION_STRING"
                    else:
                        await userbot.get_me()
                except Exception as e:
                    err = f"{type(e).__name__}: {e}"
                now = time.time()
                if err:
                    logging.error(f"Сторож: юзербот не работает: {err}")
                    if OWNER_ID and (not _userbot_alert["bad"] or now - _userbot_alert["last"] > 6 * 3600):
                        _userbot_alert["bad"], _userbot_alert["last"] = True, now
                        try:
                            await _notify_ops(
                                f"⚠️ <b>Юзербот не работает.</b>\n<code>{html.escape(err[:300])}</code>\n"
                                "Посты уйдут запасным путём (ботом) без премиум-эмодзи. Проверьте сессию и переменные окружения."
                            )
                            await alog(None, "incident", "", f"юзербот: {err[:150]}")
                        except Exception:
                            pass
                elif _userbot_alert["bad"]:
                    _userbot_alert["bad"] = False
                    if OWNER_ID:
                        try:
                            await _notify_ops("✅ Юзербот снова работает.")
                        except Exception:
                            pass
        except Exception as err2:
            logging.error(f"Ошибка в userbot_watchdog_worker: {err2}")
        await asyncio.sleep(600)

# ----------------------------------------------------------------------
# СТОРОЖ ЗДОРОВЬЯ БОТА: Bot API, база данных, «сколько бот был выключен»
# ----------------------------------------------------------------------
HEALTH_ALERT_AFTER = 300      # сек: о проблеме сообщаем, если она тянется дольше 5 минут
_health = {"bad_since": None, "reason": "", "alerted": False, "last_ok": None, "last_check": None}
_PROC_START = time.time()
_STARTED_AT = time.time()
_LOG_PRUNE = {"t": 0.0}
_ERR_TIMES: list = []          # моменты необработанных ошибок в обработчиках (для всплесков)
_ERR_SPIKE = {"last": 0.0}

async def _notify_ops(text: str) -> bool:
    """Служебное сообщение о сбое: владельцу и всем главным админам. True, если дошло хотя бы до одного."""
    ok = False
    for aid in await get_main_admin_ids():
        try:
            await bot.send_message(aid, text, parse_mode="HTML")
            ok = True
        except Exception as e:
            logging.warning(f"Не удалось написать {aid}: {e}")
    return ok

def _err_register(exc):
    """Считает ошибки обработчиков. Много ошибок подряд (например, после неудачного обновления) = сообщение главным админам."""
    now = time.time()
    _ERR_TIMES.append(now)
    del _ERR_TIMES[:-300]
    recent = [t for t in _ERR_TIMES if now - t < 300]
    if len(recent) >= 10 and now - _ERR_SPIKE["last"] > 1800:
        _ERR_SPIKE["last"] = now
        spawn(_notify_ops(
            f"🔥 <b>Всплеск ошибок:</b> {len(recent)} за 5 минут.\nПоследняя: <code>{html.escape(repr(exc)[:300])}</code>\n"
            "Если это началось после обновления, возможно, стоит откатить его."
        ))
        spawn(alog(None, "incident", "", f"всплеск ошибок: {len(recent)} за 5 мин"))

async def _health_check_once() -> str | None:
    try:
        await asyncio.wait_for(bot.get_me(), timeout=20)
    except Exception as e:
        return f"Telegram Bot API не отвечает ({type(e).__name__})"
    try:
        await asyncio.wait_for(_db_one("SELECT 1"), timeout=15)
    except Exception as e:
        return f"база данных не отвечает ({type(e).__name__})"
    return None

def _fmt_minutes(sec: float) -> str:
    m = max(1, int(round(sec / 60)))
    return f"{m} мин." if m < 120 else f"{m // 60} ч. {m % 60} мин."

async def _notify_owner(text: str) -> bool:
    if not OWNER_ID:
        return False
    try:
        await bot.send_message(OWNER_ID, text, parse_mode="HTML")
        return True
    except Exception as e:
        logging.warning(f"Не удалось написать владельцу: {e}")
        return False

async def health_watchdog_worker():
    """Раз в минуту проверяет бота и базу. Если сбой длится дольше 5 минут, пишет владельцу.
    Если сам процесс был выключен (деплой, падение), сообщает об этом при запуске."""
    logging.info("Сторож здоровья бота запущен.")
    await asyncio.sleep(20)
    try:
        last = await get_setting("last_heartbeat", "")
        if last:
            gap = time.time() - float(last)
            await alog(None, "restart", "", f"не работал ≈ {_fmt_minutes(gap)}" if gap > HEALTH_ALERT_AFTER else "перезапуск")
            if gap > HEALTH_ALERT_AFTER:
                started = datetime.fromtimestamp(float(last), MSK_TZ).strftime("%d.%m %H:%M")
                await _notify_ops(
                    f"ℹ️ <b>Бот снова запущен.</b>\nОн не работал примерно <b>{_fmt_minutes(gap)}</b> (с {started} по МСК). "
                    "Это перезапуск или сбой: отложенные публикации, которые не успели выйти, восстановлены."
                )
    except Exception as e:
        logging.warning(f"Сторож здоровья: не удалось прочитать прошлый сигнал: {e}")
    while True:
        try:
            reason = await _health_check_once()
            now = time.time()
            _health["last_check"] = now
            if reason:
                if _health["bad_since"] is None:
                    _health["bad_since"] = now
                _health["reason"] = reason
                logging.warning(f"Сторож здоровья: {reason}")
                if not _health["alerted"] and now - _health["bad_since"] >= HEALTH_ALERT_AFTER:
                    if await _notify_ops(f"⚠️ <b>Проблема с ботом уже {_fmt_minutes(now - _health['bad_since'])}:</b> {html.escape(reason)}."):
                        _health["alerted"] = True
                        await alog(None, "incident", "", reason)
            else:
                _health["last_ok"] = now
                if _health["bad_since"] is not None:
                    dur = now - _health["bad_since"]
                    if _health["alerted"] or dur >= HEALTH_ALERT_AFTER:
                        await _notify_ops(f"✅ <b>Бот снова в порядке.</b> Сбой длился {_fmt_minutes(dur)} ({html.escape(_health['reason'])}).")
                        await alog(None, "incident", "", f"восстановлено, сбой длился {_fmt_minutes(dur)}")
                    _health.update(bad_since=None, reason="", alerted=False)
                await set_setting("last_heartbeat", str(int(now)))
                if PUBLISH_PAUSED and now - max(_PAUSE_REMIND["t"], PUBLISH_PAUSED_SINCE) > 6 * 3600:
                    _PAUSE_REMIND["t"] = now
                    await _notify_ops(
                        f"⏸ <b>Публикации в канал остановлены уже {_fmt_minutes(now - PUBLISH_PAUSED_SINCE) if PUBLISH_PAUSED_SINCE else 'давно'}.</b>\n"
                        "Не забыли возобновить? Админ-панель ➔ Секретки ➔ «Возобновить публикацию»."
                    )
                if now - _LOG_PRUNE["t"] > 3600:
                    _LOG_PRUNE["t"] = now
                    await _db_exec(f"DELETE FROM admin_log WHERE ts < CURRENT_TIMESTAMP - INTERVAL '{int(ALOG_KEEP_DAYS)} days'")
        except Exception as e:
            logging.error(f"Ошибка в health_watchdog_worker: {e}", exc_info=True)
        await asyncio.sleep(60)

# ----------------------------------------------------------------------
# СЛЕЖЕНИЕ ЗА ОБНОВЛЕНИЯМИ TOWER OF HELL (оповещение стаффа в теме «Секретки», в канал не пишет)
# ----------------------------------------------------------------------
TOH_PLACE_ID = 1962086868
_TOH_UNIVERSE = {"id": None}
TOH_LIVE = {"playing": None, "visits": None, "updated": "", "checked": 0.0}

async def toh_fetch_info() -> dict | None:
    """Публичный API Roblox: дата обновления игры, онлайн, посещения."""
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            if not _TOH_UNIVERSE["id"]:
                async with session.get(f"https://apis.roblox.com/universes/v1/places/{TOH_PLACE_ID}/universe") as r:
                    if r.status != 200:
                        return None
                    _TOH_UNIVERSE["id"] = (await r.json()).get("universeId")
            async with session.get("https://games.roblox.com/v1/games", params={"universeIds": _TOH_UNIVERSE["id"]}) as r:
                if r.status != 200:
                    return None
                data = (await r.json()).get("data") or []
                if not data:
                    return None
                g = data[0]
                return {"updated": g.get("updated") or "", "playing": g.get("playing"), "visits": g.get("visits")}
    except Exception as e:
        logging.info(f"Roblox API (Tower of Hell) недоступен: {e}")
        return None

def _toh_fmt_date(iso: str) -> str:
    try:
        d = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(MSK_TZ)
        return d.strftime("%d.%m.%Y %H:%M МСК")
    except Exception:
        return iso

async def toh_check_once() -> str:
    """Один проход. Возвращает 'first' / 'same' / 'changed' / 'error'."""
    info = await toh_fetch_info()
    if not info or not info["updated"]:
        return "error"
    TOH_LIVE.update(playing=info["playing"], visits=info["visits"], updated=info["updated"], checked=time.time())
    last = (await get_setting("toh_last_updated", "")) or ""
    if not last:
        await set_setting("toh_last_updated", info["updated"])
        return "first"       # первое значение запоминаем, оповещения нет
    if last == info["updated"]:
        return "same"
    await set_setting("toh_last_updated", info["updated"])
    await call_staff(
        "🛠 <b>Tower of Hell обновился!</b>\n"
        f"Дата обновления в Roblox: <b>{_toh_fmt_date(info['updated'])}</b>.\n"
        "Секретки могут вести себя иначе, проверьте перед публикацией."
    )
    await alog(None, "toh_update", "tower_of_hell", info["updated"])
    return "changed"

async def toh_watch_worker():
    logging.info("Воркер слежения за обновлением Tower of Hell запущен.")
    await asyncio.sleep(75)
    while True:
        try:
            if (await get_setting("toh_watch", "true")) == "true":
                await toh_check_once()
        except Exception as err:
            logging.error(f"Ошибка в toh_watch_worker: {err}", exc_info=True)
        await asyncio.sleep(300)

@router.message(Command("tohwatch"), F.chat.type == "private")
async def cmd_tohwatch(message: Message):
    """/tohwatch — статус; /tohwatch on|off — включить или выключить оповещения об обновлении игры."""
    if not await is_main_admin(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) > 1 and parts[1].lower() in ("on", "off"):
        await set_setting("toh_watch", "true" if parts[1].lower() == "on" else "false")
        await alog(message.from_user.id, "toh_watch", "", parts[1].lower())
    on = (await get_setting("toh_watch", "true")) == "true"
    last = (await get_setting("toh_last_updated", "")) or ""
    await message.answer(
        "🛠 <b>Слежение за обновлением Tower of Hell</b>\n"
        f"Состояние: <b>{'включено' if on else 'выключено'}</b>\n"
        f"Последнее известное обновление игры: <b>{_toh_fmt_date(last) if last else 'ещё не определено'}</b>\n\n"
        "Выключить/включить: <code>/tohwatch off</code> / <code>/tohwatch on</code>",
        parse_mode="HTML")

# ----------------------------------------------------------------------
# АНАЛИТИКА КАНАЛА (через юзербота): подписчики, просмотры, лучшие часы
# ----------------------------------------------------------------------
_CHAN_CACHE = {"ts": 0.0, "data": None}
_DOW = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]

async def _chan_subs_now() -> int | None:
    full = await _ub_request("channel_stats subs", lambda: userbot(GetFullChannelRequest(SUB_CHANNEL)))
    return getattr(full.full_chat, "participants_count", None)

async def _chan_snapshot():
    """Раз в сутки (по МСК) записывает число подписчиков для графика роста."""
    if not userbot:
        return
    today = datetime.now(MSK_TZ).date()
    if await _db_one("SELECT 1 FROM channel_stats WHERE day = %s", (today,)):
        return
    n = await _chan_subs_now()
    if n:
        await _db_exec("INSERT INTO channel_stats (day, subs) VALUES (%s,%s) ON CONFLICT (day) DO UPDATE SET subs = EXCLUDED.subs", (today, int(n)))

async def channel_stats_worker():
    logging.info("Воркер аналитики канала запущен.")
    await asyncio.sleep(120)
    while True:
        try:
            await _chan_snapshot()
        except Exception as err:
            logging.warning(f"Аналитика канала: снимок не записан: {err}")
        await asyncio.sleep(3600)

async def channel_analytics(force: bool = False) -> dict:
    """Собирает сводку по каналу. Кэш 10 минут. Всегда возвращает словарь; при сбое там ключ 'error'."""
    now = time.time()
    if not force and _CHAN_CACHE["data"] and now - _CHAN_CACHE["ts"] < 600:
        return _CHAN_CACHE["data"]
    if not userbot:
        return {"error": "Юзербот не настроен: аналитика недоступна."}
    try:
        msgs = await _ub_request("channel_stats posts", lambda: userbot.get_messages(SUB_CHANNEL, limit=200))
        subs = await _chan_subs_now()
        if subs:
            await _db_exec("INSERT INTO channel_stats (day, subs) VALUES (%s,%s) ON CONFLICT (day) DO UPDATE SET subs = EXCLUDED.subs",
                           (datetime.now(MSK_TZ).date(), int(subs)))
    except Exception as e:
        return {"error": f"Не удалось получить данные канала: {type(e).__name__}: {e}"}
    posts = [m for m in msgs if getattr(m, "views", None) is not None and m.date]
    now_dt = datetime.now(timezone.utc)
    d30 = [m for m in posts if (now_dt - m.date).days < 30]
    d7 = [m for m in posts if (now_dt - m.date).days < 7]
    def avg(lst, attr):
        vals = [getattr(m, attr) or 0 for m in lst]
        return round(sum(vals) / len(vals)) if vals else 0
    # лучшие часы/дни: берём посты старше суток, чтобы свежие с малыми просмотрами не занижали средние
    mature = [m for m in posts if (now_dt - m.date).total_seconds() > 86400 and (now_dt - m.date).days < 90]
    by_h, by_d = {}, {}
    for m in mature:
        loc = m.date.astimezone(MSK_TZ)
        by_h.setdefault(loc.hour, []).append(m.views or 0)
        by_d.setdefault(loc.weekday(), []).append(m.views or 0)
    best_hours = sorted(((h, round(sum(v) / len(v)), len(v)) for h, v in by_h.items() if len(v) >= 2), key=lambda x: -x[1])[:3]
    best_days = sorted(((_DOW[d], round(sum(v) / len(v)), len(v)) for d, v in by_d.items() if len(v) >= 2), key=lambda x: -x[1])[:3]
    top = sorted(d30, key=lambda m: -(m.views or 0))[:3]
    uname = SUB_CHANNEL.lstrip("@")
    top_posts = [{"id": m.id, "views": m.views or 0, "forwards": m.forwards or 0,
                  "date": m.date.astimezone(MSK_TZ).strftime("%d.%m %H:%M"), "url": f"https://t.me/{uname}/{m.id}"} for m in top]
    # рост по снимкам
    growth = {}
    rows = await _db_all("SELECT day, subs FROM channel_stats ORDER BY day DESC LIMIT 40")
    if subs and rows:
        today = datetime.now(MSK_TZ).date()
        for days in (1, 7, 30):
            target = today - timedelta(days=days)
            older = [r for r in rows if r[0] <= target]
            if older:
                growth[days] = int(subs) - int(older[0][1])
    data = {"subs": subs, "growth": growth, "posts_30": len(d30), "posts_7": len(d7),
            "avg_views_30": avg(d30, "views"), "avg_fwd_30": avg(d30, "forwards"),
            "best_hours": best_hours, "best_days": best_days, "top": top_posts,
            "snap_days": len(rows), "updated": datetime.now(MSK_TZ).strftime("%d.%m %H:%M МСК")}
    _CHAN_CACHE.update(ts=now, data=data)
    return data

def _chan_text(d: dict) -> str:
    if d.get("error"):
        return f"📈 <b>Аналитика канала</b>\n\n{ICON_CROSS} {html.escape(d['error'])}"
    def sg(n): return f"{n:+d}"
    L = [f"📈 <b>Аналитика канала {html.escape(SUB_CHANNEL)}</b>", ""]
    L.append(f"👥 Подписчиков: <b>{d['subs'] if d['subs'] is not None else '—'}</b>")
    if d["growth"]:
        L.append("Рост: " + ", ".join(f"за {k} дн. <b>{sg(v)}</b>" for k, v in sorted(d["growth"].items())))
    else:
        L.append("Рост: данных пока мало, снимок делается раз в сутки.")
    L.append("")
    L.append(f"📝 Постов за 7 дней: <b>{d['posts_7']}</b>, за 30 дней: <b>{d['posts_30']}</b>")
    L.append(f"👁 Средние просмотры (30 дн.): <b>{d['avg_views_30']}</b>, пересылки: <b>{d['avg_fwd_30']}</b>")
    if d["best_hours"]:
        L.append("")
        L.append("🕒 <b>Лучшие часы публикации (МСК):</b>")
        for h, v, n in d["best_hours"]:
            L.append(f"• {h:02d}:00 — в среднем {v} просмотров ({n} постов)")
    if d["best_days"]:
        L.append("📅 Лучшие дни: " + ", ".join(f"{n} ({v})" for n, v, _ in d["best_days"]))
    if d["top"]:
        L.append("")
        L.append("🏆 <b>Лучшие посты за 30 дней:</b>")
        for t in d["top"]:
            L.append(f'• <a href="{t["url"]}">{t["date"]}</a>: {t["views"]} просмотров, {t["forwards"]} пересылок')
    L.append("")
    L.append(f"<i>Обновлено: {d['updated']}</i>")
    return "\n".join(L)

@router.callback_query(F.data.in_({"adm_chan_stats", "adm_chan_stats_refresh"}))
async def adm_chan_stats(call: CallbackQuery):
    if not await is_main_admin(call.from_user.id):
        await call.answer("❌ Нет доступа", show_alert=True)
        return
    await call.answer("Собираю данные…")
    d = await channel_analytics(force=(call.data == "adm_chan_stats_refresh"))
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [IB(text="🔄 Обновить", callback_data="adm_chan_stats_refresh")],
        [IB(text="🔙 Назад", callback_data="back_to_adm")]])
    try:
        await call.message.edit_text(_chan_text(d), reply_markup=kb, parse_mode="HTML", link_preview_options=LinkPreviewOptions(is_disabled=True))
    except Exception as e:
        logging.info(f"Аналитика канала: сообщение не обновлено ({e})")

# ----------------------------------------------------------------------
# ПРАЗДНИЧНЫЙ РЕЖИМ (Хэллоуин и другие даты): оформление сайта и строка в приветствии бота
# ----------------------------------------------------------------------
EVENTS = {
    "halloween": {"title": "Хэллоуин", "icon": "🎃",
                  "line": "🎃 <b>Хэллоуин в Tower of Secrets!</b> Ищите праздничные акции в «Платных услугах»."},
    "newyear": {"title": "Новый год", "icon": "🎄",
                "line": "🎄 <b>С наступающим Новым годом!</b> Ищите праздничные акции в «Платных услугах»."},
}
_EVENT_CACHE = {"t": 0.0, "name": ""}

async def current_event() -> str:
    """Имя активного праздничного режима или ''. Сам выключается по истечении срока. Кэш 20 с."""
    now = time.time()
    if now - _EVENT_CACHE["t"] < 20:
        return _EVENT_CACHE["name"]
    name = ""
    try:
        mode = (await get_setting("event_mode", "")) or ""
        if mode in EVENTS:
            until = float((await get_setting("event_until", "0")) or 0)
            if until and now > until:
                await set_setting("event_mode", "")
                logging.info(f"Праздничный режим «{mode}» закончился по сроку и выключен.")
            else:
                name = mode
    except Exception as e:
        logging.warning(f"Праздничный режим: не удалось прочитать настройки: {e}")
        name = _EVENT_CACHE["name"]
    _EVENT_CACHE.update(t=now, name=name)
    return name

async def _event_line() -> str:
    ev = await current_event()
    return ("\n\n" + EVENTS[ev]["line"]) if ev else ""

_RICH_TESTS = [
    ("markdown: заголовки, список, цитата",
     {"markdown": "# Заголовок 1\n\n## Заголовок 2\n\nОбычный текст, **жирный**, *курсив*, ~~зачёркнутый~~.\n\n- пункт один\n- пункт два\n\n1. первый\n2. второй\n\n> Цитата"}),
    ("markdown: таблица",
     {"markdown": "| Тип | Награда |\n|---|---|\n| Тропы | нимб |\n| Сейф | монеты |"}),
    ("html: сворачиваемый блок и разделитель",
     {"html": "<h2>Блок HTML</h2><p>Текст перед разделителем</p><hr><details><summary>Нажми, чтобы раскрыть</summary><p>Скрытый текст</p></details>"}),
    ("html: кастомный эмодзи (как в наших шаблонах)",
     {"html": "<p>Проверка эмодзи: <tg-emoji emoji-id=\"5346300789558101141\">✅</tg-emoji> и обычный ✅</p>"}),
]

async def _send_rich_raw(chat_id, rich_message: dict) -> str:
    """Сырой вызов Bot API sendRichMessage (не зависит от версии aiogram). Возвращает 'ok …' или текст ошибки."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendRichMessage"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as sess:
            async with sess.post(url, json={"chat_id": chat_id, "rich_message": rich_message}) as r:
                data = await r.json(content_type=None)
        if data.get("ok"):
            return f"ok, message_id={data.get('result', {}).get('message_id')}"
        return f"ОШИБКА {data.get('error_code')}: {data.get('description')}"
    except Exception as ex:
        return f"сбой запроса: {ex!r}"

@router.message(Command("richtest"), F.chat.type == "private")
async def cmd_richtest(message: Message):
    """/richtest [chat_id] — пробные «богатые» сообщения (Bot API 10.1+). Только владелец. Без аргумента — в личку."""
    if message.from_user.id != OWNER_ID:
        return
    parts = (message.text or "").split()[1:]
    target = message.chat.id
    if parts:
        try:
            target = int(parts[0]) if parts[0].lstrip("-").isdigit() else parts[0]
        except Exception:
            pass
    report = [f"Цель: <code>{html.escape(str(target))}</code>\n"]
    for title, payload in _RICH_TESTS:
        res = await _send_rich_raw(target, payload)
        report.append(f"• {html.escape(title)}: <code>{html.escape(res)}</code>")
        await asyncio.sleep(0.5)
    await message.answer("\n".join(report), parse_mode="HTML")

@router.message(Command("event"), F.chat.type == "private")
async def cmd_event(message: Message):
    """/event — статус; /event halloween [дней] | newyear [дней] — включить (по умолчанию на 7 дней); /event off — выключить."""
    if not await is_main_admin(message.from_user.id):
        return
    parts = (message.text or "").split()[1:]
    note = ""
    if parts:
        arg = parts[0].lower()
        if arg in ("off", "stop"):
            await set_setting("event_mode", "")
            await alog(message.from_user.id, "event", "", "off")
            note = "Праздничный режим выключен.\n\n"
        elif arg in EVENTS:
            days = 7
            if len(parts) > 1 and parts[1].isdigit() and 1 <= int(parts[1]) <= 60:
                days = int(parts[1])
            await set_setting("event_mode", arg)
            await set_setting("event_until", str(time.time() + days * 86400))
            await alog(message.from_user.id, "event", arg, f"{days} дн.")
            note = f"Включено: {EVENTS[arg]['icon']} {EVENTS[arg]['title']} на {days} дн.\n\n"
        else:
            note = f"{ICON_CROSS} Неизвестный режим.\n\n"
        _EVENT_CACHE["t"] = 0.0
        _SITE_FLAGS["t"] = 0.0
    ev = await current_event()
    status = f"{EVENTS[ev]['icon']} включён: <b>{EVENTS[ev]['title']}</b>" if ev else "выключен"
    until = ""
    if ev:
        try:
            until = ", до " + datetime.fromtimestamp(float(await get_setting("event_until", "0")), MSK_TZ).strftime("%d.%m.%Y %H:%M МСК")
        except Exception:
            pass
    await message.answer(
        f"{note}🎭 <b>Праздничный режим:</b> {status}{until}\n\n"
        "Меняет оформление сайта (цвета, баннер, значок) и добавляет строку в приветствие бота. Выключается сам по сроку.\n"
        "<code>/event halloween</code> — на 7 дней, <code>/event halloween 3</code> — на 3 дня\n"
        "<code>/event newyear</code>, <code>/event off</code>\n\n"
        "Праздничный промокод создаётся как обычно: «Платные услуги → Промокоды».",
        parse_mode="HTML")

@router.message(Command("monitor"), F.chat.type == "private")
async def cmd_monitor(message: Message):
    """Сводка состояния бота для главных админов."""
    if not await is_main_admin(message.from_user.id):
        return
    async def probe(coro, timeout=15):
        t0 = time.time()
        try:
            await asyncio.wait_for(coro, timeout=timeout)
            return f"ок, {int((time.time() - t0) * 1000)} мс"
        except Exception as e:
            return f"❌ {type(e).__name__}"
    now = time.time()
    lines = ["🩺 <b>Состояние бота</b>", ""]
    lines.append(f"• Telegram Bot API: {await probe(bot.get_me())}")
    lines.append(f"• База данных: {await probe(_db_one('SELECT 1'))}")
    if userbot:
        lines.append("• Юзербот: " + ("подключён" if userbot.is_connected() else "❌ не подключён"))
    else:
        lines.append("• Юзербот: не настроен")
    lines.append(f"• Работает без перезапуска: {_fmt_minutes(now - _STARTED_AT)}")
    if _health["bad_since"] is not None:
        lines.append(f"• ⚠️ Сейчас сбой уже {_fmt_minutes(now - _health['bad_since'])}: {html.escape(_health['reason'])}")
    else:
        lines.append("• Сторож здоровья: всё хорошо")
    lines.append(f"• Ошибок в обработчиках за час: {len([t for t in _ERR_TIMES if now - t < 3600])}")
    lines.append(f"• Фоновых задач работает: {len(_BG_TASKS)}")
    try:
        rows = await _db_all(
            "SELECT EXTRACT(EPOCH FROM ts), action, details FROM admin_log WHERE action IN ('incident', 'restart') "
            "AND ts > CURRENT_TIMESTAMP - INTERVAL '24 hours' ORDER BY id DESC LIMIT 5")
    except Exception:
        rows = []
    if rows:
        lines += ["", "<b>Происшествия за сутки:</b>"]
        for ts, act, det in rows:
            when = datetime.fromtimestamp(float(ts), MSK_TZ).strftime("%d.%m %H:%M")
            lines.append(f"• {when} {'запуск' if act == 'restart' else 'сбой'}: {html.escape(str(det or ''))[:120]}")
    lines += ["", "<i>Если бот выключится совсем, он не сможет написать сам. Для мгновенного сообщения подключите внешний монитор "
                  "(например UptimeRobot) на адрес /health сайта: он ответит ошибкой при сбое.</i>"]
    await message.answer("\n".join(lines), parse_mode="HTML")

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

PRELINK_RENEW_BEFORE_H = 24      # за сколько часов до конца напомнить о продлении
PRELINK_RENEW_MIN_AGE_H = 12     # не напоминать, пока с последней покупки не прошло столько часов

async def prelink_renew_reminder_worker():
    """Один раз за период доступа напоминает покупателю (оплата звёздами) за сутки до конца предссылок.
    Тем, кому доступ выдали вручную, напоминание не приходит."""
    logging.info("Воркер напоминаний о продлении предссылок запущен.")
    await asyncio.sleep(40)
    while True:
        try:
            def _find():
                with get_db() as conn:
                    with conn.cursor() as cur:
                        cur.execute(f"""
                            SELECT pu.user_id, pu.expires_at, sp.service_id
                            FROM prelink_users pu
                            JOIN LATERAL (
                                SELECT p.service_id, p.created_at FROM shop_payments p
                                JOIN shop_services s ON s.id = p.service_id
                                WHERE p.user_id = pu.user_id AND s.kind = 'prelinks' AND p.status = 'paid'
                                ORDER BY p.created_at DESC LIMIT 1
                            ) sp ON TRUE
                            WHERE pu.expires_at IS NOT NULL AND COALESCE(pu.is_test, FALSE) = FALSE
                              AND pu.expires_at > CURRENT_TIMESTAMP
                              AND pu.expires_at <= CURRENT_TIMESTAMP + INTERVAL '{PRELINK_RENEW_BEFORE_H} hours'
                              AND (pu.renew_reminded_for IS NULL OR pu.renew_reminded_for <> pu.expires_at)
                              AND sp.created_at <= CURRENT_TIMESTAMP - INTERVAL '{PRELINK_RENEW_MIN_AGE_H} hours'
                        """)
                        return cur.fetchall()
            for uid, exp, sid in await asyncio.to_thread(_find):
                marked = await _db_exec(
                    "UPDATE prelink_users SET renew_reminded_for = expires_at WHERE user_id = %s AND expires_at = %s "
                    "AND (renew_reminded_for IS NULL OR renew_reminded_for <> expires_at) RETURNING user_id", (uid, exp)
                )
                if not marked:
                    continue
                sv = await shop_get_service(sid)
                kb = None
                if sv and sv[4]:
                    kb = InlineKeyboardMarkup(inline_keyboard=[[IB(text="🔄 Продлить доступ", callback_data=f"shop_s_{sid}")]])
                try:
                    await bot.send_message(
                        uid,
                        f"⏳ <b>Ваш доступ к Предссылкам скоро закончится:</b> {fmt_prelink_until(exp)}.\n\n"
                        "Продлите заранее, и новый срок прибавится к текущему. Это единственное напоминание.",
                        reply_markup=kb, parse_mode="HTML", disable_notification=is_night_time()
                    )
                except Exception as e:
                    logging.warning(f"Не удалось напомнить {uid} о продлении предссылок: {e}")
        except Exception as err:
            logging.error(f"Ошибка в prelink_renew_reminder_worker: {err}", exc_info=True)
        await asyncio.sleep(600)

# ----------------------------------------------------------------------
# ЕЖЕДНЕВНАЯ СВОДКА ДЛЯ ГЛАВНЫХ АДМИНОВ
# ----------------------------------------------------------------------
async def build_daily_summary() -> str:
    """Короткая сводка за последние 24 часа. Каждая часть считается отдельно: сбой одного запроса не ломает остальные."""
    async def one(sql, params=()):
        try:
            return await _db_one(sql, params)
        except Exception as e:
            logging.warning(f"Сводка: запрос не удался: {e}")
            return None

    async def many(sql, params=()):
        try:
            return await _db_all(sql, params) or []
        except Exception as e:
            logging.warning(f"Сводка: запрос не удался: {e}")
            return []

    day = "CURRENT_TIMESTAMP - INTERVAL '24 hours'"
    now = datetime.now(MSK_TZ)
    lines = [f"📊 <b>Сводка за сутки</b> · {now.strftime('%d.%m.%Y %H:%M')} МСК", ""]

    cats = dict(await many(f"SELECT category, COUNT(*) FROM tickets WHERE created_at >= {day} GROUP BY category"))
    total_new = sum(int(v) for v in cats.values())
    names = (("complaint", "жалоб"), ("appeal", "обжалований"), ("question", "вопросов"), ("friends", "друзей"))
    parts = [f"{n} {int(cats[k])}" for k, n in names if cats.get(k)]
    lines.append(f"🎫 Новых обращений: <b>{total_new}</b>" + (f" ({', '.join(parts)})" if parts else ""))

    done = dict(await many(f"SELECT status, COUNT(*) FROM tickets WHERE status IN ('closed', 'rejected') AND updated_at >= {day} GROUP BY status"))
    lines.append(f"✅ Закрыто: <b>{int(done.get('closed', 0))}</b> · ❌ отклонено: <b>{int(done.get('rejected', 0))}</b>")

    wait = await one("SELECT COUNT(*), COALESCE(EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - MIN(created_at))) / 3600, 0) FROM tickets WHERE status = 'pending'")
    act = await one("SELECT COUNT(*) FROM tickets WHERE status = 'active'")
    if wait is not None:
        oldest = f", самое старое ждёт {int(float(wait[1]))} ч." if int(wait[0]) else ""
        lines.append(f"⏳ Ждут ответа: <b>{int(wait[0])}</b>{oldest} · в работе: <b>{int(act[0]) if act else 0}</b>")

    rt = await one(f"SELECT AVG(r.score), COUNT(*) FROM ratings r JOIN tickets t ON t.ticket_id = r.ticket_id WHERE t.status = 'closed' AND t.updated_at >= {day}")
    if rt and rt[1]:
        lines.append(f"⭐ Средняя оценка: <b>{round(float(rt[0]), 2)}</b> (оценок: {int(rt[1])})")

    best = await one(
        f"SELECT a.agent_number, COUNT(*) AS c FROM tickets t LEFT JOIN admin_agents a ON a.admin_id = t.admin_id "
        f"WHERE t.status = 'closed' AND t.admin_id IS NOT NULL AND t.updated_at >= {day} GROUP BY a.agent_number, t.admin_id ORDER BY c DESC LIMIT 1"
    )
    if best:
        who = f"Агент #{best[0]}" if best[0] else "агент без номера"
        lines.append(f"🏆 Больше всех закрыл: <b>{who}</b> ({int(best[1])})")

    fb_total = await one(f"SELECT COUNT(*) FROM ratings WHERE review IS NOT NULL AND review <> '' AND review_at >= {day}")
    if fb_total and int(fb_total[0]):
        lines.append("")
        lines.append(f"💬 <b>Отзывов за сутки: {int(fb_total[0])}</b> (сначала самые низкие оценки)")
        for tid, score, review in await many(
            f"SELECT ticket_id, score, review FROM ratings WHERE review IS NOT NULL AND review <> '' AND review_at >= {day} "
            f"ORDER BY score ASC, review_at DESC LIMIT 3"
        ):
            r = (review or "").replace("\n", " ")
            lines.append(f"• №{tid} ({score}/5): <i>{html.escape(r[:140])}{'…' if len(r) > 140 else ''}</i>")

    lines.append("")
    pub = []
    for label, sql in (
        ("секреток", f"SELECT COUNT(*) FROM secret_posts WHERE created_at >= {day}"),
        ("серверов саботёра", f"SELECT COUNT(*) FROM saboteur_servers WHERE created_at >= {day}"),
        ("фарм-серверов", f"SELECT COUNT(*) FROM farm_servers WHERE created_at >= {day}"),
        ("бейджей мутатор", f"SELECT COUNT(*) FROM mutator_badges WHERE created_at >= {day}"),
        ("бейджей исследователь", f"SELECT COUNT(*) FROM researcher_badges WHERE created_at >= {day}"),
    ):
        r = await one(sql)
        if r and int(r[0]):
            pub.append(f"{label} {int(r[0])}")
    lines.append("🔮 Опубликовано: " + (", ".join(pub) if pub else "ничего"))

    apps = dict(await many("SELECT kind, COUNT(*) FROM applications_pending GROUP BY kind"))
    lines.append(f"📝 Заявки ждут решения: хелперы <b>{int(apps.get('helper', 0))}</b>, искатели <b>{int(apps.get('secret', 0))}</b>")

    pay = await one(f"SELECT COUNT(*), COALESCE(SUM(stars), 0) FROM shop_payments WHERE status = 'paid' AND created_at >= {day}")
    if pay and int(pay[0]):
        lines.append(f"💎 Оплат за сутки: <b>{int(pay[0])}</b> на <b>{int(pay[1])} ⭐</b>")

    h_on = (await get_setting("helper_recruitment", "true")) == "true"
    s_on = (await get_setting("secret_recruitment", "true")) == "true"
    lines.append(f"🚪 Наборы: хелперы {'открыт' if h_on else 'закрыт'}, искатели {'открыт' if s_on else 'закрыт'}")
    return "\n".join(lines)

async def daily_summary_worker():
    """Раз в сутки (с 09:00 МСК) шлёт сводку главным админам. Выключается в «Система и доступ»."""
    logging.info("Воркер ежедневной сводки запущен.")
    while True:
        try:
            now = datetime.now(MSK_TZ)
            if 9 <= now.hour < 12 and (await get_setting("daily_summary", "true")) == "true":
                today = now.strftime("%Y-%m-%d")
                if (await get_setting("daily_summary_last", "")) != today:
                    await set_setting("daily_summary_last", today)     # сперва отмечаем день, чтобы не продублировать при сбое
                    text = await build_daily_summary()
                    for aid in await get_main_admin_ids():
                        try:
                            await bot.send_message(aid, text, parse_mode="HTML")
                        except Exception as e:
                            logging.warning(f"Не удалось отправить сводку админу {aid}: {e}")
        except Exception as e:
            logging.error(f"Ошибка в daily_summary_worker: {e}", exc_info=True)
        await asyncio.sleep(120)

@router.callback_query(F.data == "toggle_daily_summary")
async def toggle_daily_summary_cb(call: CallbackQuery):
    if not await is_main_admin(call.from_user.id):
        await call.answer("❌ Нет доступа", show_alert=True)
        return
    new_val = "false" if (await get_setting("daily_summary", "true")) == "true" else "true"
    await set_setting("daily_summary", new_val)
    await call.answer("Ежедневная сводка включена." if new_val == "true" else "Ежедневная сводка выключена.", show_alert=True)
    try:
        await call.message.edit_reply_markup(reply_markup=await system_kb())
    except Exception:
        pass

@router.callback_query(F.data == "daily_summary_now")
async def daily_summary_now_cb(call: CallbackQuery):
    if not await is_main_admin(call.from_user.id):
        await call.answer("❌ Нет доступа", show_alert=True)
        return
    await call.answer("Собираю сводку…")
    try:
        await bot.send_message(call.from_user.id, await build_daily_summary(), parse_mode="HTML")
    except Exception as e:
        logging.warning(f"Не удалось отправить сводку по запросу: {e}")

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
    _err_register(exc)
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

SITE_DIR = os.path.dirname(os.path.abspath(__file__))
# Сайт: страницы и файлы лежат рядом с main.py. Отдаём только безопасные расширения (код .py, .txt, .jpg бота не отдаём).
_SITE_EXT = {".html", ".css", ".js", ".svg", ".ico", ".png", ".webp"}
_SITE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}(\.[A-Za-z0-9]{2,5})?$")

def _site_file(name: str):
    """'rules' -> rules.html, 'style.css' -> style.css. Возвращает путь или None."""
    if not _SITE_NAME_RE.match(name):
        return None
    base, ext = os.path.splitext(name)
    fname = base + ".html" if not ext else name
    if os.path.splitext(fname)[1].lower() not in _SITE_EXT:
        return None
    path = os.path.join(SITE_DIR, fname)
    return path if os.path.isfile(path) else None

async def _site_404():
    page = os.path.join(SITE_DIR, "404.html")
    if os.path.isfile(page):
        return web.FileResponse(page, status=404, headers={"Cache-Control": "no-cache"})
    return web.Response(status=404, text="404 — страница не найдена")

async def handle_ping(request):
    """Главная: index.html рядом с main.py, иначе простая заглушка."""
    path = _site_file("index")
    if path:
        return web.FileResponse(path, headers={"Cache-Control": "no-cache"})
    return web.Response(text="Bot is running!")

async def handle_page(request):
    """Остальные страницы и файлы сайта: /rules, /privacy, /style.css ..."""
    path = _site_file(request.match_info["name"])
    if path:
        return web.FileResponse(path, headers={"Cache-Control": "no-cache"})
    return await _site_404()

_SITE_FLAGS = {"t": 0.0, "data": {}}

async def _site_flags() -> dict:
    """Публичные флаги для сайта (набор открыт/закрыт, рефералка). Кэш 30 с, чтобы сайт не нагружал базу."""
    now = time.time()
    if _SITE_FLAGS["data"] and now - _SITE_FLAGS["t"] < 30:
        return _SITE_FLAGS["data"]
    try:
        helpers = (await get_setting("helper_recruitment", "true")) == "true"
        seekers = (await get_setting("secret_recruitment", "true")) == "true"
        data = {"recruit": {"helpers": helpers, "seekers": seekers}, "ref": bool(_REF_CFG.get("ref")),
                "event": await current_event()}
        _SITE_FLAGS.update(t=now, data=data)
        return data
    except Exception as e:
        logging.warning(f"/api/status: не удалось прочитать настройки: {e}")
        return _SITE_FLAGS["data"] or {}

_SITE_SECRETS = {"t": 0.0, "data": None}
_SITE_SHOP = {"t": 0.0, "data": None}

async def _site_secrets() -> dict:
    """Лента секреток для сайта: только тип, возраст и статус. Ссылок и серверов тут нет и быть не должно. Кэш 15 с."""
    now = time.time()
    if _SITE_SECRETS["data"] is not None and now - _SITE_SECRETS["t"] < 15:
        return _SITE_SECRETS["data"]
    data = {"items": [], "farm": None, "saboteur": None}
    if TEST_MODE_ON:
        # в тест-режиме посты идут в тестовый канал, сайту их показывать не нужно
        _SITE_SECRETS.update(t=now, data=data)
        return data
    try:
        rows = await _db_all(
            "SELECT secret_type, EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - created_at)), is_expired "
            "FROM secret_posts WHERE message_id > 0 AND created_at > CURRENT_TIMESTAMP - INTERVAL '24 hours' "
            "ORDER BY created_at DESC LIMIT 8"
        )
        for stype, age, expired in rows:
            age = max(0, int(age or 0))
            data["items"].append({
                "type": str(stype or "Секретка")[:40],
                "age": age,
                # если пометка «истекла» почему-то не поставилась, через 30 минут пост всё равно считаем закрытым
                "open": (not expired) and age < 1800,
            })
    except Exception as e:
        logging.warning(f"/api/secrets: не удалось прочитать секретки: {e}")
        return _SITE_SECRETS["data"] or {"items": [], "farm": None, "saboteur": None, "error": True}
    for key, sql in (
        ("farm", "SELECT COUNT(*) FROM farm_servers s JOIN farm_posts p ON p.id = s.post_id WHERE s.is_active = TRUE AND p.is_active = TRUE"),
        ("saboteur", "SELECT COUNT(*) FROM saboteur_servers s JOIN saboteur_posts p ON p.id = s.post_id WHERE s.is_active = TRUE AND p.is_active = TRUE"),
    ):
        try:
            r = await _db_one(sql)
            data[key] = int(r[0]) if r else 0
        except Exception as e:
            logging.warning(f"/api/secrets: счётчик {key}: {e}")
    _SITE_SECRETS.update(t=now, data=data)
    return data

async def _site_shop() -> dict:
    """Витрина услуг для сайта: то же, что видит человек в боте (активные услуги и тарифы). Кэш 30 с."""
    now = time.time()
    if _SITE_SHOP["data"] is not None and now - _SITE_SHOP["t"] < 30:
        return _SITE_SHOP["data"]
    try:
        out = []
        for sv in await shop_get_services(True):
            tariffs = await shop_get_tariffs(sv[0], True)
            if not tariffs:
                continue
            out.append({
                "id": int(sv[0]),
                "title": str(sv[1] or "")[:80],
                "description": str(sv[2] or "")[:600],
                "kind": str(sv[3] or ""),
                "tariffs": [{"label": str(t[1] or "")[:60], "stars": int(t[2]), "days": (int(t[3]) if t[3] else None)} for t in tariffs],
            })
        data = {"services": out, "channel": await shop_channel_url()}
        _SITE_SHOP.update(t=now, data=data)
        return data
    except Exception as e:
        logging.warning(f"/api/shop: не удалось прочитать услуги: {e}")
        return _SITE_SHOP["data"] or {"services": [], "channel": SHOP_DEFAULT_CHANNEL, "error": True}

async def handle_api_secrets(request):
    return web.json_response(await _site_secrets(), headers={"Cache-Control": "no-store"})

async def handle_api_shop(request):
    return web.json_response(await _site_shop(), headers={"Cache-Control": "no-store"})

# ---------- личный кабинет на сайте ----------
# Вход без паролей: сайт выдаёт одноразовый код, человек подтверждает его в боте, сайт получает подписанную куку.
_LOGIN: Dict[str, dict] = {}
_LOGIN_TTL = 600
_LOGIN_RATE: Dict[str, list] = {}
_SESS_COOKIE = "tos_sess"
_SESS_DAYS = 30
_SESS_KEY = hashlib.sha256(("site-session:" + (os.getenv("SESSION_SECRET") or BOT_TOKEN or "dev")).encode()).digest()
_CAB_CACHE: Dict[int, tuple] = {}

_SESS_DAYS_STAFF = 3     # у команды сессия короче: на сайте открыта статистика и данные других людей

async def _login_is_staff(uid: int) -> bool:
    try:
        return bool(await is_main_admin(uid) or await _is_staff_cached(uid) or await is_secret_publisher(uid))
    except Exception:
        return True      # при сомнении считаем стаффом: сессия короче, это безопаснее

def _sess_make(uid: int, name: str, days: int = None) -> str:
    raw = json.dumps({"u": int(uid), "n": (name or "")[:40], "e": int(time.time()) + (days or _SESS_DAYS) * 86400}, separators=(",", ":"), ensure_ascii=False)
    payload = base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")
    sig = hmac.new(_SESS_KEY, payload.encode(), hashlib.sha256).hexdigest()[:32]
    return payload + "." + sig

def _sess_read(value) -> dict:
    try:
        payload, sig = str(value).rsplit(".", 1)
        good = hmac.new(_SESS_KEY, payload.encode(), hashlib.sha256).hexdigest()[:32]
        if not hmac.compare_digest(sig, good):
            return None
        d = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        if int(d.get("e", 0)) < time.time():
            return None
        return {"uid": int(d["u"]), "name": str(d.get("n", ""))}
    except Exception:
        return None

def _sess_from_request(request):
    v = request.cookies.get(_SESS_COOKIE)
    return _sess_read(v) if v else None

def _client_ip(request) -> str:
    # последний адрес в цепочке добавляет наш прокси, первый мог подставить сам клиент
    return (request.headers.get("X-Forwarded-For", "").split(",")[-1].strip() or request.remote or "?")[:64]

def _login_rate_ok(ip: str) -> bool:
    now = time.time()
    lst = [t for t in _LOGIN_RATE.get(ip, []) if now - t < 600]
    if len(lst) >= 10:
        _LOGIN_RATE[ip] = lst
        return False
    lst.append(now)
    _LOGIN_RATE[ip] = lst
    if len(_LOGIN_RATE) > 5000:
        for k in [k for k, v in _LOGIN_RATE.items() if not v or now - v[-1] > 600]:
            _LOGIN_RATE.pop(k, None)
    return True

_NOSTORE = {"Cache-Control": "no-store"}

async def handle_login_start(request):
    if not _login_rate_ok(_client_ip(request)):
        return web.json_response({"error": "rate"}, status=429, headers=_NOSTORE)
    now = time.time()
    for k in [k for k, v in _LOGIN.items() if now - v["t"] > _LOGIN_TTL]:
        _LOGIN.pop(k, None)
    uname = await _bot_username()
    if not uname or len(_LOGIN) > 3000:
        return web.json_response({"error": "unavailable"}, status=503, headers=_NOSTORE)
    code = secrets.token_urlsafe(9)
    sec = secrets.token_urlsafe(18)
    num = 10 + secrets.randbelow(90)      # число для сверки: сайт его показывает, в боте его нужно выбрать среди вариантов
    _LOGIN[code] = {"secret": sec, "t": now, "uid": None, "name": "", "num": num}
    return web.json_response(
        {"code": code, "secret": sec, "num": num, "link": f"https://t.me/{uname}?start=login_{code}", "ttl": _LOGIN_TTL},
        headers=_NOSTORE
    )

async def handle_login_check(request):
    try:
        body = await request.json()
        code = str(body.get("code", ""))[:40]
        sec = str(body.get("secret", ""))[:80]
    except Exception:
        return web.json_response({"status": "unknown"}, status=400, headers=_NOSTORE)
    ent = _LOGIN.get(code)
    if not ent or not hmac.compare_digest(ent["secret"], sec):
        return web.json_response({"status": "unknown"}, headers=_NOSTORE)
    if time.time() - ent["t"] > _LOGIN_TTL:
        _LOGIN.pop(code, None)
        return web.json_response({"status": "expired"}, headers=_NOSTORE)
    if ent["uid"] is None:
        return web.json_response({"status": "wait"}, headers=_NOSTORE)
    _LOGIN.pop(code, None)
    resp = web.json_response({"status": "ok"}, headers=_NOSTORE)
    secure = request.headers.get("X-Forwarded-Proto", "").lower() == "https" or request.secure
    days = _SESS_DAYS_STAFF if await _login_is_staff(ent["uid"]) else _SESS_DAYS
    resp.set_cookie(_SESS_COOKIE, _sess_make(ent["uid"], ent["name"], days), max_age=days * 86400,
                    httponly=True, samesite="Lax", secure=secure, path="/")
    return resp

async def handle_logout(request):
    resp = web.json_response({"ok": True}, headers=_NOSTORE)
    resp.del_cookie(_SESS_COOKIE, path="/")
    return resp

async def _cabinet_data(uid: int) -> dict:
    """Только данные самого человека: статусы обращений без текста, заявки, подписка, рефералы, Предссылки, оплаты."""
    now = time.time()
    c = _CAB_CACHE.get(uid)
    if c and now - c[0] < 10:
        return c[1]
    data = {}
    try:
        rows = await _db_all(
            "SELECT t.ticket_id, t.category, t.status, t.created_at, r.score FROM tickets t "
            "LEFT JOIN ratings r ON r.ticket_id = t.ticket_id WHERE t.user_id = %s ORDER BY t.ticket_id DESC LIMIT 15", (uid,)
        )
        data["tickets"] = [{"id": r[0], "category": r[1], "status": r[2], "created": int(r[3].timestamp()) if r[3] else 0, "rating": r[4]} for r in rows]
    except Exception as e:
        logging.warning(f"/api/me: обращения: {e}")
        data["tickets"] = []
    try:
        data["apps"] = [r[0] for r in await _db_all("SELECT kind FROM applications_pending WHERE user_id = %s", (uid,))]
    except Exception:
        data["apps"] = []
    try:
        sub = await check_subscription(uid)
        data["subscribed"] = sub if sub in (True, False) else None
    except Exception:
        data["subscribed"] = None
    try:
        ban = await is_banned(uid)
        data["banned"] = bool(ban)
        data["ban_reason"] = str(ban[0])[:200] if ban else ""
    except Exception:
        data["banned"] = False
        data["ban_reason"] = ""
    try:
        if _REF_CFG.get("ref"):
            cnt = await _ref_counts(uid)
            uname = await _bot_username()
            data["ref"] = {
                "link": f"https://t.me/{uname}?start=ref_{uid}" if uname else "",
                "confirmed": cnt.get("confirmed", 0), "waiting": cnt.get("waiting", 0),
                "pending": cnt.get("pending", 0), "rewards": cnt.get("rewards", 0),
            }
        else:
            data["ref"] = None
    except Exception as e:
        logging.warning(f"/api/me: рефералка: {e}")
        data["ref"] = None
    try:
        pl = await _db_one("SELECT expires_at FROM prelink_users WHERE user_id = %s AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)", (uid,))
        data["prelinks"] = {"active": True, "until": fmt_prelink_until(pl[0])} if pl else {"active": False, "until": ""}
    except Exception:
        data["prelinks"] = {"active": False, "until": ""}
    try:
        pay = await _db_all(
            "SELECT service_title, tariff_label, stars, created_at FROM shop_payments WHERE user_id = %s AND status = 'paid' ORDER BY id DESC LIMIT 5", (uid,)
        )
        data["payments"] = [{"title": str(p[0] or "")[:80], "label": str(p[1] or "")[:60], "stars": int(p[2] or 0), "created": int(p[3].timestamp()) if p[3] else 0} for p in pay]
    except Exception:
        data["payments"] = []
    if len(_CAB_CACHE) > 2000:
        _CAB_CACHE.clear()
    _CAB_CACHE[uid] = (now, data)
    return data

# ---------- статистика команды для личного кабинета ----------
# Роли определяются на сервере по тем же правилам, что и в боте (/mystats, /secstats, /supptop):
#   хелпер (участник админ-чата) видит свою статистику; публикатор секреток или главный админ видит статистику публикаций;
#   главный админ видит ещё полную статистику проекта и общий рейтинг команды.
_NAME_CACHE: Dict[int, tuple] = {}
_RANK_CACHE = {"t": 0.0, "data": []}
_ADMIN_STATS_CACHE = {"t": 0.0, "data": None}
_STAFF_SITE_CACHE: Dict[int, tuple] = {}

def _rater_filter() -> str:
    """Оценки считаются только от обычных игроков: свои оценки стафф не накручивает (как и в боте)."""
    owner = int(OWNER_ID or 0)
    return (f"r.user_id != {owner} AND r.user_id NOT IN (SELECT admin_id FROM main_admins) "
            "AND r.user_id NOT IN (SELECT admin_id FROM admin_agents)")

async def _plain_name(uid: int) -> str:
    c = _NAME_CACHE.get(uid)
    if c and time.time() - c[1] < 3600:
        return c[0]
    name = f"ID {uid}"
    try:
        ci = await bot.get_chat(uid)
        if ci.username:
            name = "@" + ci.username
        elif ci.full_name:
            name = ci.full_name
    except Exception:
        pass
    name = name[:40]
    if len(_NAME_CACHE) > 2000:
        _NAME_CACHE.clear()
    _NAME_CACHE[uid] = (name, time.time())
    return name

async def _support_ranking() -> list:
    """[(admin_id, закрыто, средняя оценка, номер агента, оценок)] от лучшего к худшему, только те, кто ещё в админ-чате. Кэш 60 с."""
    now = time.time()
    if _RANK_CACHE["data"] and now - _RANK_CACHE["t"] < 60:
        return _RANK_CACHE["data"]
    ok = _rater_filter()
    rows = await _db_all(
        "SELECT t.admin_id, COUNT(t.ticket_id), COALESCE(AVG(CASE WHEN " + ok + " THEN r.score END), 0), a.agent_number, "
        "COUNT(CASE WHEN " + ok + " THEN r.score END) "
        "FROM tickets t LEFT JOIN ratings r ON r.ticket_id = t.ticket_id LEFT JOIN admin_agents a ON a.admin_id = t.admin_id "
        "WHERE t.admin_id IS NOT NULL AND t.status = 'closed' GROUP BY t.admin_id, a.agent_number "
        "ORDER BY 3 DESC, 2 DESC, t.admin_id"
    )
    rows = await filter_in_admin_chat(rows)
    data = [(int(r[0]), int(r[1]), round(float(r[2]), 2), (int(r[3]) if r[3] else None), int(r[4])) for r in rows]
    _RANK_CACHE.update(t=now, data=data)
    return data

async def _site_helper_stats(uid: int) -> dict:
    ok = _rater_filter()
    cl = await _db_one(
        "SELECT COUNT(*), COUNT(*) FILTER (WHERE updated_at >= CURRENT_TIMESTAMP - INTERVAL '7 days'), "
        "COUNT(*) FILTER (WHERE updated_at >= CURRENT_TIMESTAMP - INTERVAL '30 days') "
        "FROM tickets WHERE admin_id = %s AND status = 'closed'", (uid,))
    act = await _db_one("SELECT COUNT(*) FROM tickets WHERE admin_id = %s AND status = 'active'", (uid,))
    rt = await _db_one("SELECT COALESCE(AVG(r.score), 0), COUNT(*) FROM ratings r WHERE r.admin_id = %s AND " + ok, (uid,))
    dist = dict(await _db_all("SELECT r.score, COUNT(*) FROM ratings r WHERE r.admin_id = %s AND " + ok + " GROUP BY r.score", (uid,)))
    revs = await _db_all(
        "SELECT r.ticket_id, r.score, r.review, COALESCE(EXTRACT(EPOCH FROM r.review_at), 0) FROM ratings r "
        "WHERE r.admin_id = %s AND r.review IS NOT NULL AND r.review <> '' AND " + ok + " ORDER BY r.review_at DESC NULLS LAST LIMIT 5", (uid,))
    ag = await _db_one("SELECT agent_number FROM admin_agents WHERE admin_id = %s", (uid,))
    agent = 1 if (OWNER_ID and uid == OWNER_ID) else (int(ag[0]) if ag and ag[0] else None)
    ranking = await _support_ranking()
    rank, of = None, len(ranking)
    for i, r in enumerate(ranking, 1):
        if r[0] == uid:
            rank = i
            break
    return {
        "agent": agent,
        "closed": int(cl[0]), "closed7": int(cl[1]), "closed30": int(cl[2]),
        "active": int(act[0]) if act else 0,
        "avg": round(float(rt[0]), 2), "rated": int(rt[1]),
        "dist": {str(k): int(dist.get(k, 0)) for k in (5, 4, 3, 2, 1)},
        "rank": rank, "of": of,
        "reviews": [{"ticket": int(x[0]), "score": int(x[1]), "text": str(x[2])[:300], "at": int(x[3])} for x in revs],
    }

_BADGE_TABLES = (("saboteur", "saboteur_servers"), ("farm", "farm_servers"), ("mutator", "mutator_badges"), ("researcher", "researcher_badges"))

async def _site_publisher_stats(uid: int) -> dict:
    tot = await _db_one(
        "SELECT COUNT(*), COUNT(*) FILTER (WHERE created_at >= CURRENT_TIMESTAMP - INTERVAL '24 hours') FROM secret_posts WHERE user_id = %s", (uid,))
    types = await _db_all("SELECT secret_type, COUNT(*) FROM secret_posts WHERE user_id = %s GROUP BY secret_type ORDER BY 2 DESC, 1 LIMIT 5", (uid,))
    ranks = await _db_all("SELECT user_id, COUNT(*) FROM secret_posts GROUP BY user_id ORDER BY 2 DESC, 1")
    rank = next((i for i, r in enumerate(ranks, 1) if r[0] == uid), None)
    other = {}
    for key, table in _BADGE_TABLES:
        try:
            r = await _db_one(
                f"SELECT COUNT(*), COUNT(*) FILTER (WHERE created_at >= CURRENT_TIMESTAMP - INTERVAL '24 hours') FROM {table} WHERE user_id = %s", (uid,))
            other[key] = {"total": int(r[0]), "day": int(r[1])}
        except Exception as e:
            logging.warning(f"Кабинет: статистика {table}: {e}")
            other[key] = {"total": 0, "day": 0}
    recent = await _db_all(
        "SELECT secret_type, EXTRACT(EPOCH FROM created_at), is_expired FROM secret_posts WHERE user_id = %s AND message_id > 0 "
        "ORDER BY created_at DESC LIMIT 8", (uid,))
    return {
        "total": int(tot[0]), "day": int(tot[1]), "rank": rank, "of": len(ranks),
        "types": [{"type": str(t[0] or "Секретка")[:40], "count": int(t[1])} for t in types],
        "other": other,
        "recent": [{"type": str(x[0] or "Секретка")[:40], "at": int(x[1]), "expired": bool(x[2])} for x in recent],
    }

async def _site_admin_stats() -> dict:
    """Полная статистика проекта для главных админов. Кэш 60 с (общий для всех админов)."""
    now = time.time()
    if _ADMIN_STATS_CACHE["data"] is not None:
        if now - _ADMIN_STATS_CACHE["t"] < 60:
            return _ADMIN_STATS_CACHE["data"]
        if now - _ADMIN_STATS_CACHE["t"] < 1800:
            # данные чуть устарели: отдаём сразу, а свежие досчитываем в фоне (страница не ждёт)
            if not _ADMIN_STATS_CACHE.get("busy"):
                _ADMIN_STATS_CACHE["busy"] = True
                async def _refresh():
                    try:
                        await _site_admin_stats_compute()
                    except Exception as e:
                        logging.warning(f"Статистика админки: фоновое обновление не удалось: {e}")
                    finally:
                        _ADMIN_STATS_CACHE["busy"] = False
                spawn(_refresh())
            return _ADMIN_STATS_CACHE["data"]
    return await _site_admin_stats_compute()

async def _site_admin_stats_compute() -> dict:
    ok = _rater_filter()
    out = {}
    total, closed, rejected, avg = await get_tickets_stats()
    out["tickets"] = {"total": int(total), "closed": int(closed), "rejected": int(rejected), "avg": float(avg)}
    out["users"] = int(await get_all_users_count())
    st = dict(await _db_all("SELECT status, COUNT(*) FROM tickets GROUP BY status"))
    out["tickets"]["pending"] = int(st.get("pending", 0))
    out["tickets"]["active"] = int(st.get("active", 0))
    old = await _db_one("SELECT COALESCE(EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - MIN(created_at))) / 3600, 0) FROM tickets WHERE status = 'pending'")
    out["tickets"]["oldest_wait_h"] = int(float(old[0])) if old else 0
    out["categories"] = {str(k): int(v) for k, v in await _db_all("SELECT category, COUNT(*) FROM tickets GROUP BY category")}
    new_by = dict(await _db_all(
        "SELECT TO_CHAR((created_at AT TIME ZONE 'Europe/Moscow')::date, 'YYYY-MM-DD'), COUNT(*) FROM tickets "
        "WHERE created_at >= CURRENT_TIMESTAMP - INTERVAL '16 days' GROUP BY 1"))
    done_by = dict(await _db_all(
        "SELECT TO_CHAR((updated_at AT TIME ZONE 'Europe/Moscow')::date, 'YYYY-MM-DD'), COUNT(*) FROM tickets "
        "WHERE status = 'closed' AND updated_at >= CURRENT_TIMESTAMP - INTERVAL '16 days' GROUP BY 1"))
    days = []
    for i in range(13, -1, -1):
        d = (datetime.now(MSK_TZ) - timedelta(days=i)).strftime("%Y-%m-%d")
        days.append({"d": d, "new": int(new_by.get(d, 0)), "closed": int(done_by.get(d, 0))})
    out["days"] = days
    dist = dict(await _db_all("SELECT r.score, COUNT(*) FROM ratings r WHERE " + ok + " GROUP BY r.score"))
    out["dist"] = {str(k): int(dist.get(k, 0)) for k in (5, 4, 3, 2, 1)}
    team = []
    for aid, cnt, avg_sc, agent, rated in await _support_ranking():
        team.append({"id": aid, "name": await _plain_name(aid), "agent": agent, "closed": cnt, "avg": avg_sc, "rated": rated})
    out["team"] = team
    pubs = []
    for uid_, cnt, d24 in await _db_all(
            "SELECT user_id, COUNT(*), COUNT(*) FILTER (WHERE created_at >= CURRENT_TIMESTAMP - INTERVAL '24 hours') "
            "FROM secret_posts GROUP BY user_id ORDER BY 2 DESC, 1 LIMIT 10"):
        pubs.append({"id": int(uid_), "name": await _plain_name(int(uid_)), "count": int(cnt), "day": int(d24)})
    out["publishers"] = pubs
    sp = await _db_one(
        "SELECT COUNT(*), COUNT(*) FILTER (WHERE created_at >= CURRENT_TIMESTAMP - INTERVAL '24 hours'), "
        "COUNT(*) FILTER (WHERE created_at >= CURRENT_TIMESTAMP - INTERVAL '7 days') FROM secret_posts")
    published = {"secrets": {"total": int(sp[0]), "day": int(sp[1]), "week": int(sp[2])}}
    for key, table in _BADGE_TABLES:
        try:
            r = await _db_one(f"SELECT COUNT(*), COUNT(*) FILTER (WHERE created_at >= CURRENT_TIMESTAMP - INTERVAL '24 hours') FROM {table}")
            published[key] = {"total": int(r[0]), "day": int(r[1])}
        except Exception as e:
            logging.warning(f"Кабинет: статистика {table}: {e}")
    out["published"] = published
    pay = {}
    for key, interval in (("week", "7 days"), ("month", "30 days")):
        try:
            r = await _db_one("SELECT COUNT(*), COALESCE(SUM(stars), 0) FROM shop_payments WHERE status = 'paid' "
                              f"AND created_at >= CURRENT_TIMESTAMP - INTERVAL '{interval}'")
            pay[key] = {"count": int(r[0]), "stars": int(r[1])}
        except Exception as e:
            logging.warning(f"Кабинет: оплаты: {e}")
            pay[key] = {"count": 0, "stars": 0}
    out["payments"] = pay
    try:
        out["apps"] = {str(k): int(v) for k, v in await _db_all("SELECT kind, COUNT(*) FROM applications_pending GROUP BY kind")}
    except Exception:
        out["apps"] = {}
    try:
        titles = _faq_items()
        rows = await _db_all(
            "SELECT q, COUNT(*) FILTER (WHERE v = 1), COUNT(*) FILTER (WHERE v = -1) FROM faq_votes GROUP BY q")
        faq = [{"q": q, "title": titles.get(q, q), "yes": int(y), "no": int(n)} for q, y, n in rows if q in titles]
        faq.sort(key=lambda x: (-x["no"], x["yes"], x["q"]))
        out["faq"] = faq[:12]
    except Exception as e:
        logging.warning(f"Кабинет: статистика FAQ: {e}")
        out["faq"] = []
    out["recruit_subs"] = {"helper": await recruit_sub_count("helper"), "secret": await recruit_sub_count("secret")}
    _ADMIN_STATS_CACHE.update(t=time.time(), data=out)
    return out

async def _site_staff(uid: int):
    """Блок «для команды»: None, если человек не стафф. Каждая часть независима: сбой одной не ломает остальные."""
    c = _STAFF_SITE_CACHE.get(uid)
    if c and time.time() - c[0] < 30:
        return c[1]
    is_main = await is_main_admin(uid)
    is_helper = is_main or await _is_staff_cached(uid)
    is_pub = is_main or await is_secret_publisher(uid)
    res = None
    if is_helper or is_pub or is_main:
        res = {"roles": [], "helper": None, "publisher": None, "admin": None}
        if is_helper:
            res["roles"].append("helper")
            try:
                res["helper"] = await _site_helper_stats(uid)
            except Exception as e:
                logging.warning(f"/api/me: статистика хелпера {uid}: {e}")
        if is_pub:
            res["roles"].append("publisher")
            try:
                res["publisher"] = await _site_publisher_stats(uid)
            except Exception as e:
                logging.warning(f"/api/me: статистика публикатора {uid}: {e}")
        if is_main:
            res["roles"].append("admin")
            try:
                res["admin"] = await _site_admin_stats()
            except Exception as e:
                logging.warning(f"/api/me: статистика админа {uid}: {e}")
    if len(_STAFF_SITE_CACHE) > 500:
        _STAFF_SITE_CACHE.clear()
    _STAFF_SITE_CACHE[uid] = (time.time(), res)
    return res

async def handle_api_me(request):
    sess = _sess_from_request(request)
    if not sess:
        return web.json_response({"error": "auth"}, status=401, headers=_NOSTORE)
    data = dict(await _cabinet_data(sess["uid"]))
    data["user"] = {"id": sess["uid"], "name": sess["name"]}
    try:
        data["staff"] = await _site_staff(sess["uid"])
    except Exception as e:
        logging.warning(f"/api/me: блок для команды: {e}")
        data["staff"] = None
    return web.json_response(data, headers=_NOSTORE)

# ---------- голосование «ответ помог?» на странице /faq ----------
_FAQ_RATE: Dict[str, list] = {}
_ADMLOG_RATE: Dict[str, list] = {}
_FAQ_CACHE = {"mt": 0.0, "items": {}}
_VOTER_COOKIE = "tos_v"

def _hit(bucket: dict, key: str, limit: int, window: int) -> bool:
    """Простой лимитер: не больше limit обращений за window секунд на ключ."""
    now = time.time()
    lst = [t for t in bucket.get(key, []) if now - t < window]
    if len(lst) >= limit:
        bucket[key] = lst
        return False
    lst.append(now)
    bucket[key] = lst
    if len(bucket) > 5000:
        for k in [k for k, v in bucket.items() if not v or now - v[-1] > window]:
            bucket.pop(k, None)
    return True

def _faq_items() -> dict:
    """{id вопроса: заголовок}: берём из faq.html, поэтому голосовать можно только за существующие вопросы."""
    path = os.path.join(SITE_DIR, "faq.html")
    try:
        mt = os.path.getmtime(path)
    except OSError:
        return {}
    if _FAQ_CACHE["items"] and _FAQ_CACHE["mt"] == mt:
        return _FAQ_CACHE["items"]
    try:
        with open(path, encoding="utf-8") as f:
            txt = f.read()
    except Exception:
        return _FAQ_CACHE["items"]
    items = {}
    for m in re.finditer(r'<details[^>]*\bdata-q="([a-z0-9-]{1,40})"[^>]*>\s*<summary>(.*?)</summary>', txt, re.S):
        items[m.group(1)] = re.sub(r"<[^>]+>", "", html.unescape(m.group(2))).strip()[:120]
    _FAQ_CACHE.update(mt=mt, items=items)
    return items

def _voter_token(request) -> str:
    v = request.cookies.get(_VOTER_COOKIE, "")
    return v if re.fullmatch(r"[0-9a-f]{32}", v or "") else ""

def _voter_key(tok: str) -> str:
    return hashlib.sha256((tok + "|faq").encode()).hexdigest()[:32]

async def handle_faq_vote(request):
    if not _hit(_FAQ_RATE, "ip:" + _client_ip(request), 120, 600):
        return web.json_response({"error": "rate"}, status=429, headers=_NOSTORE)
    try:
        body = await request.json()
        q = str(body.get("q", ""))[:40]
        v = int(body.get("v"))
    except Exception:
        return web.json_response({"error": "bad"}, status=400, headers=_NOSTORE)
    if q not in _faq_items() or v not in (-1, 0, 1):
        return web.json_response({"error": "bad"}, status=400, headers=_NOSTORE)
    tok = _voter_token(request)
    fresh = not tok
    if fresh:
        tok = secrets.token_hex(16)
    key = _voter_key(tok)
    if not _hit(_FAQ_RATE, "v:" + key, 40, 600):
        return web.json_response({"error": "rate"}, status=429, headers=_NOSTORE)
    try:
        if v == 0:
            await _db_exec("DELETE FROM faq_votes WHERE voter = %s AND q = %s", (key, q))
        else:
            await _db_exec(
                "INSERT INTO faq_votes (voter, q, v) VALUES (%s, %s, %s) "
                "ON CONFLICT (voter, q) DO UPDATE SET v = EXCLUDED.v, ts = CURRENT_TIMESTAMP", (key, q, v))
    except Exception as e:
        logging.warning(f"/api/faq/vote: {e}")
        return web.json_response({"error": "db"}, status=503, headers=_NOSTORE)
    resp = web.json_response({"ok": True, "v": v}, headers=_NOSTORE)
    if fresh:
        secure = request.headers.get("X-Forwarded-Proto", "").lower() == "https" or request.secure
        resp.set_cookie(_VOTER_COOKIE, tok, max_age=365 * 86400, httponly=True, samesite="Lax", secure=secure, path="/")
    return resp

async def handle_faq_votes(request):
    """Только свои голоса этого браузера. Чужие и общие счётчики публично не отдаются."""
    tok = _voter_token(request)
    mine = {}
    if tok:
        try:
            rows = await _db_all("SELECT q, v FROM faq_votes WHERE voter = %s", (_voter_key(tok),))
            known = _faq_items()
            mine = {str(q): int(v) for q, v in rows if q in known}
        except Exception as e:
            logging.warning(f"/api/faq/votes: {e}")
    return web.json_response({"mine": mine}, headers=_NOSTORE)

# ---------- журнал действий команды: только для главных админов ----------
# ----------------------------------------------------------------------
# АДМИНКА НА САЙТЕ (/admin): только главные админы. Права проверяются при КАЖДОМ запросе.
# ----------------------------------------------------------------------
_ADM_RATE: Dict[str, list] = {}
_ADM_WRITE_RATE: Dict[str, list] = {}

def _jerr(code: int, err: str, **extra):
    return web.json_response({"error": err, **extra}, status=code, headers=_NOSTORE)

async def _adm_auth(request, write: bool = False):
    """Возвращает (uid, None) или (None, готовый ответ с ошибкой)."""
    sess = _sess_from_request(request)
    if not sess:
        return None, _jerr(401, "auth")
    if not await is_main_admin(sess["uid"]):
        return None, _jerr(403, "forbidden")
    if not _hit(_ADM_RATE, str(sess["uid"]), 240, 60):
        return None, _jerr(429, "rate")
    if write:
        # защита от чужих сайтов: такой заголовок браузер не отправит на чужой домен без нашего разрешения
        if request.headers.get("X-Requested-With") != "tos":
            return None, _jerr(403, "csrf")
        if not _hit(_ADM_WRITE_RATE, str(sess["uid"]), 30, 60):
            return None, _jerr(429, "rate")
    return sess["uid"], None

def _plain_card(t) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", t or "")).strip()

_CAT_NAMES = {"complaint": "Жалоба", "appeal": "Обжалование", "question": "Вопрос", "friends": "Друзья", "general": "Заявка"}

async def handle_adm_overview(request):
    uid, err = await _adm_auth(request)
    if err: return err
    out = dict(await _site_admin_stats())
    out["flags"] = {
        "paused": bool(PUBLISH_PAUSED), "paused_since": int(PUBLISH_PAUSED_SINCE or 0),
        "maintenance": bool(MAINTENANCE_ON), "test_mode": bool(TEST_MODE_ON),
        "helpers_open": (await get_setting("helper_recruitment", "true")) == "true",
        "seekers_open": (await get_setting("secret_recruitment", "true")) == "true",
        "event": await current_event(),
        "toh": {"updated": (await get_setting("toh_last_updated", "")) or "", "playing": TOH_LIVE.get("playing")},
    }
    out["me"] = {"id": uid, "owner": bool(OWNER_ID and uid == OWNER_ID)}
    return web.json_response(out, headers=_NOSTORE)

async def handle_adm_tickets(request):
    uid, err = await _adm_auth(request)
    if err: return err
    q = request.query
    where, params = [], []
    st = q.get("status", "")
    if st in ("pending", "active", "closed", "rejected"):
        where.append("t.status = %s"); params.append(st)
    elif st == "open":
        where.append("t.status IN ('pending','active')")
    cat = q.get("cat", "")
    if cat in _CAT_NAMES:
        where.append("t.category = %s"); params.append(cat)
    adm = q.get("admin", "")
    if adm.isdigit():
        where.append("(t.admin_id = %s OR EXISTS (SELECT 1 FROM ticket_messages x WHERE x.ticket_id = t.ticket_id "
                     "AND x.sender = 'system' AND x.text = 'taken' AND x.sender_id = %s))")
        params += [int(adm), int(adm)]
    before = q.get("before", "")
    if before.isdigit():
        where.append("t.ticket_id < %s"); params.append(int(before))
    term = (q.get("q", "") or "").strip()[:80]
    if term:
        if term.lstrip("№#").isdigit():
            n = int(term.lstrip("№#"))
            where.append("(t.ticket_id = %s OR t.user_id = %s)"); params += [n, n]
        else:
            like = "%" + term.lstrip("@").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            where.append("(u.username ILIKE %s OR t.card_text ILIKE %s OR EXISTS (SELECT 1 FROM ticket_messages m WHERE m.ticket_id = t.ticket_id AND m.text ILIKE %s))")
            params += [like, like, like]
    try:
        rows = await _db_all(
            "SELECT t.ticket_id, t.user_id, u.username, t.status, t.category, t.admin_id, a.agent_number, "
            "EXTRACT(EPOCH FROM t.created_at), EXTRACT(EPOCH FROM t.updated_at), r.score, "
            "(SELECT COUNT(*) FROM ticket_messages m WHERE m.ticket_id = t.ticket_id AND m.sender <> 'system'), "
            "(SELECT ARRAY_AGG(m.sender_id ORDER BY m.id) FROM ticket_messages m WHERE m.ticket_id = t.ticket_id AND m.sender = 'system' AND m.text = 'taken') "
            "FROM tickets t LEFT JOIN users u ON u.user_id = t.user_id LEFT JOIN admin_agents a ON a.admin_id = t.admin_id "
            "LEFT JOIN ratings r ON r.ticket_id = t.ticket_id"
            + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY t.ticket_id DESC LIMIT 31", tuple(params))
    except Exception as e:
        logging.warning(f"/api/admin/tickets: {e}")
        return _jerr(503, "db")
    names = {}
    async def _nm(i):
        if i not in names:
            names[i] = await _plain_name(i)
        return names[i]
    items = []
    for r in rows[:30]:
        chain_ids = [int(x) for x in (r[11] or []) if x]
        if r[5] and (not chain_ids or chain_ids[-1] != int(r[5])):
            chain_ids.append(int(r[5]))      # старые обращения (до журнала) и текущий хелпер
        chain = []
        for cid in chain_ids:
            if not chain or chain[-1]["id"] != cid:
                chain.append({"id": cid, "name": await _nm(cid)})
        items.append({"id": int(r[0]), "user": int(r[1]) if r[1] else None, "uname": r[2] or "", "status": r[3], "cat": _CAT_NAMES.get(r[4], str(r[4] or "")),
                      "admin": int(r[5]) if r[5] else None, "agent": r[6], "created": int(r[7] or 0), "updated": int(r[8] or 0),
                      "score": r[9], "msgs": int(r[10] or 0), "chain": chain[:6]})
    return web.json_response({"items": items, "more": len(rows) > 30}, headers=_NOSTORE)

async def handle_adm_ticket(request):
    uid, err = await _adm_auth(request)
    if err: return err
    tid = request.match_info.get("id", "")
    if not tid.isdigit():
        return _jerr(404, "not_found")
    tid = int(tid)
    t = await _db_one(
        "SELECT t.ticket_id, t.user_id, u.username, t.status, t.category, t.admin_id, a.agent_number, "
        "EXTRACT(EPOCH FROM t.created_at), EXTRACT(EPOCH FROM t.updated_at), t.card_text, t.link_msg_id, t.card_msg_id "
        "FROM tickets t LEFT JOIN users u ON u.user_id = t.user_id LEFT JOIN admin_agents a ON a.admin_id = t.admin_id WHERE t.ticket_id = %s", (tid,))
    if not t:
        return _jerr(404, "not_found")
    rt = await _db_one("SELECT score, review FROM ratings WHERE ticket_id = %s", (tid,))
    msgs = await _db_all(
        "SELECT sender, sender_id, text, media, via, EXTRACT(EPOCH FROM created_at) FROM ticket_messages WHERE ticket_id = %s ORDER BY id LIMIT 500", (tid,))
    targets = await _db_all("SELECT nick FROM complaint_targets WHERE ticket_id = %s ORDER BY id", (tid,))
    names = {}
    for sid in {m[1] for m in msgs if m[0] in ("admin", "system") and m[1]} | ({t[5]} if t[5] else set()):
        names[int(sid)] = await _plain_name(int(sid))
    timeline = []
    for sender, sid, text, media, via, ts in msgs:
        if sender == "system":
            label = {"closed": "Обращение закрыто", "rejected": "Обращение отклонено", "taken": "Взял в работу",
                     "transferred": "Передал обращение другому хелперу"}.get(text, text)
            timeline.append({"kind": "system", "text": label, "at": int(ts), "who": names.get(int(sid), "") if sid else ""})
        else:
            timeline.append({"kind": sender, "text": text or "", "media": media or "", "via": via, "at": int(ts),
                             "who": names.get(int(sid), "") if (sender == "admin" and sid) else ""})
    link = _ticket_url(t[10] or t[11])
    chain_ids = []
    for sender, sid, text, *_ in msgs:
        if sender == "system" and text == "taken" and sid and (not chain_ids or chain_ids[-1] != int(sid)):
            chain_ids.append(int(sid))
    if t[5] and (not chain_ids or chain_ids[-1] != int(t[5])):
        chain_ids.append(int(t[5]))
    chain = []
    for cid in chain_ids[:8]:
        chain.append({"id": cid, "name": names.get(cid) or await _plain_name(cid)})
    out = {
        "chain": chain, "id": int(t[0]), "user": int(t[1]) if t[1] else None, "uname": t[2] or "", "status": t[3], "cat": _CAT_NAMES.get(t[4], str(t[4] or "")),
        "admin": int(t[5]) if t[5] else None, "admin_name": names.get(int(t[5]), "") if t[5] else "", "agent": t[6],
        "created": int(t[7] or 0), "updated": int(t[8] or 0), "card": _plain_card(t[9])[:3500], "link": link,
        "score": rt[0] if rt else None, "review": (rt[1] or "") if rt else "",
        "targets": [r[0] for r in targets][:10], "messages": timeline,
        "logged": bool(msgs),
    }
    return web.json_response(out, headers=_NOSTORE)

async def handle_adm_helpers(request):
    uid, err = await _adm_auth(request)
    if err: return err
    days = request.query.get("days", "30")
    cond = ""
    if days.isdigit() and 0 < int(days) <= 365:
        cond = f" AND t.updated_at >= CURRENT_TIMESTAMP - INTERVAL '{int(days)} days'"
    try:
        rows = await _db_all(
            "SELECT t.admin_id, a.agent_number, COUNT(*) FILTER (WHERE t.status = 'closed'), COUNT(*) FILTER (WHERE t.status = 'rejected'), "
            "COUNT(*) FILTER (WHERE t.status = 'active'), AVG(r.score), COUNT(r.score) "
            "FROM tickets t LEFT JOIN admin_agents a ON a.admin_id = t.admin_id LEFT JOIN ratings r ON r.ticket_id = t.ticket_id "
            "WHERE t.admin_id IS NOT NULL" + cond + " GROUP BY t.admin_id, a.agent_number ORDER BY 3 DESC, 1 LIMIT 100")
        fr = dict(await _db_all(
            "SELECT t.admin_id, AVG(EXTRACT(EPOCH FROM (m.ts - t.created_at))) FROM tickets t "
            "JOIN (SELECT ticket_id, MIN(created_at) ts FROM ticket_messages WHERE sender = 'admin' GROUP BY ticket_id) m ON m.ticket_id = t.ticket_id "
            "WHERE t.admin_id IS NOT NULL" + cond + " GROUP BY t.admin_id"))
    except Exception as e:
        logging.warning(f"/api/admin/helpers: {e}")
        return _jerr(503, "db")
    items = []
    for aid, agent, closed, rej, act, avg_sc, rated in rows:
        items.append({"id": int(aid), "name": await _plain_name(int(aid)), "agent": agent, "closed": int(closed), "rejected": int(rej),
                      "active": int(act), "avg": round(float(avg_sc), 2) if avg_sc is not None else None, "rated": int(rated),
                      "first_reply_min": round(float(fr[aid]) / 60, 1) if fr.get(aid) is not None else None})
    return web.json_response({"items": items}, headers=_NOSTORE)

async def _read_json(request) -> dict:
    try:
        d = await request.json()
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}

async def handle_adm_reply(request):
    uid, err = await _adm_auth(request, write=True)
    if err: return err
    tid = request.match_info.get("id", "")
    body = await _read_json(request)
    text = str(body.get("text", "")).strip()
    if not tid.isdigit() or not (1 <= len(text) <= 3500):
        return _jerr(400, "bad_request")
    tid = int(tid)
    info = await get_ticket_info(tid)
    if not info:
        return _jerr(404, "not_found")
    if info[2] != "active":
        return _jerr(409, "not_active")
    agent_no = await get_or_create_agent_number(uid)
    try:
        await bot.send_message(info[0], f"👨‍💻 <b>Ответ поддержки (Агент #{agent_no}):</b>\n\n{html.escape(text)}",
                               parse_mode="HTML", reply_markup=solved_kb(tid))
    except Exception as e:
        logging.warning(f"Админка: не удалось отправить ответ по №{tid}: {e}")
        return _jerr(502, "send_failed", detail=type(e).__name__)
    await touch_ticket(tid, actor="admin")
    await tlog(tid, "admin", uid, text, via="site")
    await alog(uid, "ticket_reply_web", f"№{tid}", text[:200])
    try:   # копия в тему поддержки, чтобы хелперы в группе видели ответ; на неё можно отвечать как на обычное сообщение
        sent = await bot.send_message(ADMIN_CHAT_ID, f"🌐 <b>Ответ с сайта по заявке №{tid}</b> (Агент #{agent_no}):\n\n{html.escape(text)}",
                                      message_thread_id=TOPIC_SUPPORT_ID, parse_mode="HTML")
        await map_message(sent.message_id, info[0], tid)
    except Exception as e:
        logging.info(f"Админка: копия ответа в группу не отправлена: {e}")
    return web.json_response({"ok": True}, headers=_NOSTORE)

async def handle_adm_close(request):
    uid, err = await _adm_auth(request, write=True)
    if err: return err
    tid = request.match_info.get("id", "")
    if not tid.isdigit():
        return _jerr(404, "not_found")
    tid = int(tid)
    info = await get_ticket_info(tid)
    if not info:
        return _jerr(404, "not_found")
    if info[2] != "active":
        return _jerr(409, "not_active")
    agent_no = await get_or_create_agent_number(uid)
    await close_ticket_db(tid, status="closed", admin_id=info[1] or uid)   # заслуга остаётся у хелпера, который вёл обращение
    await alog(uid, "ticket_close", f"№{tid}", "с сайта")
    try:
        await bot.send_message(info[0], f"{ICON_LOCK} Ваша заявка <b>№{tid}</b> закрыта поддержкой.\nОцените качество обслуживания:",
                               parse_mode="HTML", reply_markup=rating_kb(tid))
    except Exception:
        pass
    await mark_ticket_card_closed(tid, f"🔒 <b>Заявка №{tid} закрыта</b> главным админом через сайт (Агент #{agent_no}).")
    return web.json_response({"ok": True}, headers=_NOSTORE)

async def handle_adm_pause(request):
    global PUBLISH_PAUSED, PUBLISH_PAUSED_SINCE
    uid, err = await _adm_auth(request, write=True)
    if err: return err
    body = await _read_json(request)
    if not isinstance(body.get("on"), bool):
        return _jerr(400, "bad_request")
    new_state = body["on"]
    since = time.time() if new_state else 0.0
    try:
        await set_setting("publish_paused", "true" if new_state else "false")
        await set_setting("publish_paused_since", str(int(since)))
    except Exception as e:
        logging.error(f"Админка: не удалось сохранить паузу публикаций: {e}")
        return _jerr(503, "db")
    PUBLISH_PAUSED, PUBLISH_PAUSED_SINCE = new_state, since
    _PAUSE_REMIND["t"] = 0.0
    await alog(uid, "publish_pause", "", ("остановлены" if new_state else "возобновлены") + " (сайт)")
    from types import SimpleNamespace   # то, что нужно _notify_pause_change: id и full_name
    spawn(_notify_pause_change(SimpleNamespace(id=uid, full_name=await _plain_name(uid)), new_state))
    return web.json_response({"ok": True, "paused": new_state}, headers=_NOSTORE)

async def handle_adm_recruit(request):
    uid, err = await _adm_auth(request, write=True)
    if err: return err
    body = await _read_json(request)
    kind, op = body.get("kind"), body.get("open")
    if kind not in ("helper", "secret") or not isinstance(op, bool):
        return _jerr(400, "bad_request")
    key = "helper_recruitment" if kind == "helper" else "secret_recruitment"
    await set_setting(key, "true" if op else "false")
    if op and kind == "secret":
        try:
            ep = int((await get_setting("secret_recruit_epoch", "0")) or 0)
        except ValueError:
            ep = 0
        await set_setting("secret_recruit_epoch", str(ep + 1))
    if op:
        spawn(notify_recruit_open(kind, uid))
    await alog(uid, "recruit_helper" if kind == "helper" else "recruit_secret", "", ("открыт" if op else "закрыт") + " (сайт)")
    _SITE_FLAGS["t"] = 0.0
    return web.json_response({"ok": True}, headers=_NOSTORE)

async def handle_adm_event(request):
    uid, err = await _adm_auth(request, write=True)
    if err: return err
    body = await _read_json(request)
    mode = str(body.get("mode", ""))
    if mode and mode not in EVENTS:
        return _jerr(400, "bad_request")
    days = body.get("days", 7)
    if not isinstance(days, int) or not (1 <= days <= 60):
        days = 7
    await set_setting("event_mode", mode)
    if mode:
        await set_setting("event_until", str(time.time() + days * 86400))
    await alog(uid, "event", mode or "off", f"{days} дн. (сайт)" if mode else "выключен (сайт)")
    _EVENT_CACHE["t"] = 0.0
    _SITE_FLAGS["t"] = 0.0
    return web.json_response({"ok": True}, headers=_NOSTORE)

async def handle_adm_channel(request):
    uid, err = await _adm_auth(request)
    if err: return err
    d = await channel_analytics(force=request.query.get("force") == "1")
    return web.json_response(d, headers=_NOSTORE)

async def handle_admin_log(request):
    sess = _sess_from_request(request)
    if not sess:
        return web.json_response({"error": "auth"}, status=401, headers=_NOSTORE)
    if not await is_main_admin(sess["uid"]):          # права проверяем при каждом запросе, не по кэшу
        return web.json_response({"error": "forbidden"}, status=403, headers=_NOSTORE)
    if not _hit(_ADMLOG_RATE, str(sess["uid"]), 60, 60):
        return web.json_response({"error": "rate"}, status=429, headers=_NOSTORE)
    group = request.query.get("g", "")
    try:
        before = max(0, int(request.query.get("before", "0") or 0))
    except ValueError:
        before = 0
    where, params = [], []
    if group in ("tickets", "people", "settings", "system"):
        acts = [a for a, (_, g) in ALOG_INFO.items() if g == group]
        where.append("action IN (" + ",".join(["%s"] * len(acts)) + ")")
        params += acts
    if before:
        where.append("id < %s")
        params.append(before)
    sql = ("SELECT id, EXTRACT(EPOCH FROM ts), actor_id, action, target, details FROM admin_log"
           + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id DESC LIMIT 41")
    try:
        rows = await _db_all(sql, tuple(params))
    except Exception as e:
        logging.warning(f"/api/admin/log: {e}")
        return web.json_response({"error": "db"}, status=503, headers=_NOSTORE)
    more = len(rows) > 40
    items = []
    for rid, ts, actor, action, target, details in rows[:40]:
        label, grp = ALOG_INFO.get(action, (action, "other"))
        items.append({
            "id": int(rid), "at": int(ts),
            "who": (await _plain_name(int(actor))) if actor else "Система",
            "action": label, "group": grp,
            "target": str(target or "")[:60], "details": str(details or "")[:300],
        })
    return web.json_response({"items": items, "more": more}, headers=_NOSTORE)

async def handle_api_status(request):
    """Публичный статус для сайта. Только безопасные данные, без личной информации."""
    payload = {
        "ok": True,
        "bot": "up",
        "health": "ok" if _health["bad_since"] is None else "degraded",
        "time": int(time.time()),
    }
    payload.update(await _site_flags())
    return web.json_response(payload, headers={"Cache-Control": "no-store"})

async def handle_health(request):
    """Для внешнего монитора (UptimeRobot и т.п.): 200, пока всё хорошо, 503 при затяжном сбое."""
    now = time.time()
    if _health["bad_since"] is not None and now - _health["bad_since"] >= 120:
        return web.Response(status=503, text=f"unhealthy: {_health['reason']}")
    # сторож перестал отмечаться (зависла основная петля) — тоже сбой; первые 3 минуты после запуска не считаем
    last = _health.get("last_check")
    if now - _PROC_START > 180 and (last is None or now - last > 300):
        return web.Response(status=503, text="unhealthy: сторож здоровья не отвечает")
    return web.Response(text="ok")

async def web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/api/status", handle_api_status)
    app.router.add_get("/api/secrets", handle_api_secrets)
    app.router.add_get("/api/shop", handle_api_shop)
    app.router.add_get("/api/me", handle_api_me)
    app.router.add_post("/api/faq/vote", handle_faq_vote)
    app.router.add_get("/api/faq/votes", handle_faq_votes)
    app.router.add_get("/api/admin/log", handle_admin_log)
    app.router.add_get("/api/admin/overview", handle_adm_overview)
    app.router.add_get("/api/admin/tickets", handle_adm_tickets)
    app.router.add_get("/api/admin/ticket/{id}", handle_adm_ticket)
    app.router.add_get("/api/admin/helpers", handle_adm_helpers)
    app.router.add_get("/api/admin/channel", handle_adm_channel)
    app.router.add_post("/api/admin/ticket/{id}/reply", handle_adm_reply)
    app.router.add_post("/api/admin/ticket/{id}/close", handle_adm_close)
    app.router.add_post("/api/admin/pause", handle_adm_pause)
    app.router.add_post("/api/admin/recruit", handle_adm_recruit)
    app.router.add_post("/api/admin/event", handle_adm_event)
    app.router.add_post("/api/login/start", handle_login_start)
    app.router.add_post("/api/login/check", handle_login_check)
    app.router.add_post("/api/logout", handle_logout)
    app.router.add_get("/{name}", handle_page)   # ставим последним: /health и /api/status имеют приоритет
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 3000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logging.info(f"Health-check сервер запущен на порту {port}")

# Общий обработчик личных сообщений регистрируем ПОСЛЕДНИМ: aiogram берёт первый подходящий обработчик,
# и если он стоит раньше команд (/monitor, /event, /tohwatch), те никогда не срабатывают.
router.message.register(user_private_message, F.chat.type == "private")

async def main():
    global MAINTENANCE_ON, TEST_MODE_ON, TEST_CHANNEL_ID, TEST_STARTED_AT, PUBLISH_PAUSED, PUBLISH_PAUSED_SINCE
    MAINTENANCE_ON = (await get_setting("maintenance_mode", "false")) == "true"
    PUBLISH_PAUSED = (await get_setting("publish_paused", "false")) == "true"
    try:
        PUBLISH_PAUSED_SINCE = float(await get_setting("publish_paused_since", "0") or 0) if PUBLISH_PAUSED else 0.0
    except ValueError:
        PUBLISH_PAUSED_SINCE = 0.0
    if PUBLISH_PAUSED:
        logging.warning("Публикации в канал ОСТАНОВЛЕНЫ (включено ранее админом). Возобновить: админ-панель, Секретки.")
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
    await _ref_cfg_load()
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
            
    spawn(_supervised(reminder_worker))
    spawn(_supervised(daily_summary_worker))
    spawn(_supervised(secret_timer_worker))
    spawn(_supervised(mutator_timer_worker))
    spawn(_supervised(prelink_expiry_worker))
    spawn(_supervised(userbot_watchdog_worker))
    spawn(_supervised(promo_hold_worker))
    spawn(_supervised(health_watchdog_worker))
    spawn(_supervised(prelink_renew_reminder_worker))
    spawn(_supervised(referral_worker))
    spawn(_supervised(toh_watch_worker))
    spawn(_supervised(channel_stats_worker))
    spawn(_supervised(userbot_keepalive_worker))
    for _try in range(1, 7):   # Telegram может на минуту отдать Bad Gateway: ждём и повторяем
        try:
            me_bot = await bot.get_me()
            await bot.delete_webhook(drop_pending_updates=False)   # вебхук мешал бы polling
            logging.info(f"Бот @{me_bot.username} проверен, начинаю получать сообщения.")
            break
        except Exception as e:
            logging.error(f"НЕ УДАЛОСЬ связаться с Telegram Bot API (попытка {_try}/6; проверьте BOT_TOKEN/сеть): {e}")
            if "Unauthorized" in str(e):
                break
            await asyncio.sleep(min(5 * _try, 20))
    spawn(_restore_delayed_publishes())
    logging.info("Бот успешно запущен и готов к работе!")
    
    try:
        _fails = 0
        while True:
            _t0 = time.monotonic()
            try:
                # сессию бота не закрываем между попытками: сбой Telegram не должен ронять весь процесс
                await dp.start_polling(bot, close_bot_session=False)
                break   # штатная остановка
            except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
                raise
            except Exception as e:
                _fails = 1 if time.monotonic() - _t0 > 120 else _fails + 1
                _wait = min(5 * _fails, 60)
                logging.error(f"Polling прервался ({type(e).__name__}: {e}). Перезапуск через {_wait} с.")
                await asyncio.sleep(_wait)
    finally:
        if userbot and userbot.is_connected():
            await userbot.disconnect()
        try:
            await bot.session.close()
        except Exception:
            pass

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Бот остановлен.")
    finally:
        if not db_pool.closed:
            db_pool.closeall()
