"""Настройки бота по user_id: режим (Новичок/Про) и язык (RU/EN), п.5 ТЗ.

Хранение — таблица bot_settings в файле БД портала (тот же SQLite, что
у FarmDaemon.db_path). core/ не трогаем: таблица создаётся из portal-слоя
через aiosqlite напрямую. По умолчанию — Новичок/RU. При недоступной БД —
память процесса (best-effort).
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

_TABLE = (
    "CREATE TABLE IF NOT EXISTS bot_settings ("
    "user_id INTEGER PRIMARY KEY, mode TEXT, lang TEXT, updated_at REAL)"
)

DEFAULT_MODE = "novice"
DEFAULT_LANG = "ru"
MODES = ("novice", "pro")
LANGS = ("ru", "en")

_mem: dict[tuple[str | None, int], dict[str, str]] = {}


async def _load(db_path: str | None, user_id: int) -> dict[str, str] | None:
    if not db_path:
        return _mem.get((None, user_id))
    try:
        import aiosqlite

        async with aiosqlite.connect(db_path) as db:
            await db.execute(_TABLE)
            async with db.execute(
                "SELECT mode, lang FROM bot_settings WHERE user_id=?", (user_id,)
            ) as cur:
                row = await cur.fetchone()
        if row:
            return {"mode": str(row[0]), "lang": str(row[1])}
        return None
    except Exception:  # noqa: BLE001
        logger.warning("bot_settings: чтение упало, память процесса", exc_info=True)
        return _mem.get((db_path, user_id))


async def get_settings(db_path: str | None, user_id: int) -> tuple[str, str]:
    """(mode, lang) с дефолтами и нормализацией битых значений."""
    row = await _load(db_path, user_id)
    mode = (row or {}).get("mode", DEFAULT_MODE)
    lang = (row or {}).get("lang", DEFAULT_LANG)
    if mode not in MODES:
        mode = DEFAULT_MODE
    if lang not in LANGS:
        lang = DEFAULT_LANG
    return mode, lang


async def _save(db_path: str | None, user_id: int, mode: str, lang: str) -> None:
    _mem[(db_path, user_id)] = {"mode": mode, "lang": lang}
    if not db_path:
        return
    try:
        import aiosqlite

        async with aiosqlite.connect(db_path) as db:
            await db.execute(_TABLE)
            await db.execute(
                "INSERT INTO bot_settings (user_id, mode, lang, updated_at)"
                " VALUES (?, ?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET"
                " mode=excluded.mode, lang=excluded.lang, updated_at=excluded.updated_at",
                (user_id, mode, lang, time.time()),
            )
            await db.commit()
    except Exception:  # noqa: BLE001
        logger.warning("bot_settings: запись упала, осталась память процесса", exc_info=True)


async def set_mode(db_path: str | None, user_id: int, mode: str) -> str:
    mode = mode if mode in MODES else DEFAULT_MODE
    _, lang = await get_settings(db_path, user_id)
    await _save(db_path, user_id, mode, lang)
    return mode


async def set_lang(db_path: str | None, user_id: int, lang: str) -> str:
    lang = lang if lang in LANGS else DEFAULT_LANG
    mode, _ = await get_settings(db_path, user_id)
    await _save(db_path, user_id, mode, lang)
    return lang
