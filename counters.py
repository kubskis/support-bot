"""Дневные счётчики публикаций.

Таблица daily_counts хранит, сколько каждый человек провёл за каждый календарный день (МСК) по типам:
sec — секретки, sab — саботёр, farm — фарм, mut — мутатор, res — исследователь,
m_* — ручные зачёты из раздела «Вручную».

Бот пересчитывает последние 2 дня раз в 5 минут и весь архив при старте. Значит, сегодняшние цифры
отстают не больше чем на 5 минут. Статистика читает эту таблицу, а не объединяет исходные таблицы заново.
"""
import asyncio
import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

MSK = ZoneInfo("Europe/Moscow")
log = logging.getLogger("counters")
_get_db = None
REFRESH_SECONDS = 300

_SOURCES = {  # тип → таблица (все с колонкой created_at и user_id)
    "sec": "secret_posts",
    "sab": "saboteur_servers",
    "farm": "farm_servers",
    "mut": "mutator_badges",
    "res": "researcher_badges",
}
KINDS = ("sec", "sab", "farm", "mut", "res")

DDL = """
CREATE TABLE IF NOT EXISTS daily_counts(day DATE NOT NULL, kind TEXT NOT NULL, user_id BIGINT NOT NULL, n INT NOT NULL,
                                        PRIMARY KEY(day, kind, user_id));
"""


def today_msk() -> date:
    return datetime.now(MSK).date()


def day_from(period: str):
    """Начало периода для счётчиков: 'd' — сегодня, 'w' — последние 7 дней, 'a' — всё время (None)."""
    if period == "d":
        return today_msk()
    if period == "w":
        return today_msk() - timedelta(days=6)
    return None


def _rebuild_sync(d0):
    """Пересчитывает счётчики с дня d0 (None — весь архив). Всё в одной транзакции: читатели видят старые цифры до коммита."""
    with _get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(DDL)
            if d0:
                cur.execute("DELETE FROM daily_counts WHERE day >= %s", (d0,))
                wh, ps = " WHERE (created_at AT TIME ZONE 'Europe/Moscow')::date >= %s", (d0,)
                mwh, mps = " WHERE day >= %s", (d0,)
            else:
                cur.execute("DELETE FROM daily_counts")
                wh, ps = "", ()
                mwh, mps = "", ()
            for kind, table in _SOURCES.items():
                cur.execute(
                    f"INSERT INTO daily_counts(day, kind, user_id, n) "
                    f"SELECT (created_at AT TIME ZONE 'Europe/Moscow')::date, %s, user_id, COUNT(*) "
                    f"FROM {table}{wh} GROUP BY 1, 3", (kind,) + ps)
            try:
                cur.execute(
                    f"INSERT INTO daily_counts(day, kind, user_id, n) "
                    f"SELECT day, 'm_' || kind, user_id, COUNT(*) FROM manual_grants{mwh} GROUP BY 1, 2, 3", mps)
            except Exception as e:
                log.warning("counters: ручные зачёты не пересчитаны: %s", e)


def _read_sync(d0):
    with _get_db() as conn:
        with conn.cursor() as cur:
            if d0:
                cur.execute("SELECT user_id, kind, SUM(n) FROM daily_counts WHERE day >= %s GROUP BY 1, 2", (d0,))
            else:
                cur.execute("SELECT user_id, kind, SUM(n) FROM daily_counts GROUP BY 1, 2")
            rows = cur.fetchall()
    out = {}
    for uid, kind, n in rows:
        k = kind[2:] if kind.startswith("m_") else kind
        if k not in KINDS:
            continue
        out.setdefault(int(uid), {k2: 0 for k2 in KINDS})[k] += int(n)
    return out


async def per_user(period: str) -> dict:
    """{user_id: {sec, sab, farm, mut, res}} за период ('d', 'w', 'a')."""
    return await asyncio.to_thread(_read_sync, day_from(period))


async def refresh(d0=None):
    await asyncio.to_thread(_rebuild_sync, d0)


def setup(get_db):
    global _get_db
    _get_db = get_db


async def worker():
    """Полный пересчёт при старте, потом последние 2 дня каждые 5 минут."""
    try:
        await refresh(None)
        log.info("counters: архив пересчитан")
    except Exception:
        log.exception("counters: полный пересчёт не удался")
    while True:
        await asyncio.sleep(REFRESH_SECONDS)
        try:
            await refresh(today_msk() - timedelta(days=1))
        except Exception:
            log.exception("counters: пересчёт не удался")
