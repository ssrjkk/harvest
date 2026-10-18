"""Синхронизация кошельков в MotherDuck для облачного бота (botforge).

Запускается с ПК вместе с `publish_dash.py` (планировщик): читает wallets
из PORTAL_DB (расшифровка через FARMER_MASTER_KEY env или master.key рядом
с БД) и upsert'ит в `md:harvest.wallets(address, private_key, mnemonic,
total_actions)` — бот читает их из облака, когда ПК выключен.

ВНИМАНИЕ: приватные ключи и сид-фразы покидают машину — осознанный
трейд-офф дизайна (облачный бот отдаёт их владельцу из облака).
Без MOTHERDUCK_TOKEN — тихий пропуск (бота без кошельков, только статус).
Токен — только из env, в файлы/конфиги не пишется.

Использование:
    MOTHERDUCK_TOKEN=... PORTAL_DB=farming_run200.db python -m tools.sync_motherduck
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

_DB_ENV = "PORTAL_DB"
_TOKEN_ENV = "MOTHERDUCK_TOKEN"
_TABLE = "wallets"
_BATCH = 200


def _db_path() -> str:
    return os.environ.get(_DB_ENV, "farming_vibevibe.db")


def _token() -> str:
    return os.environ.get(_TOKEN_ENV, "").strip()


def _master_key(db_path: str) -> bytes:
    """Ключ БД: FARMER_MASTER_KEY env, иначе master.key рядом с БД/CWD."""
    from core.crypto import MasterKeyError, resolve_master_key

    cfg = {"database": {"master_key": str(Path(db_path).parent / "master.key")}}
    try:
        return resolve_master_key(cfg)
    except MasterKeyError:
        # Fallback на master.key в текущей папке (запуск из другого CWD).
        return resolve_master_key({"database": {"master_key": "master.key"}})


async def _read_wallets(db_path: str, key: bytes) -> list[tuple]:
    """Читает wallets read-only (WAL живой фермы не трогаем записью)."""
    import aiosqlite

    from core.crypto import _decrypt_seed

    rows: list[tuple] = []
    async with aiosqlite.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10) as db:
        try:
            cur = await db.execute(
                "SELECT address, private_key, mnemonic, "
                "COALESCE(total_actions, 0) FROM wallets ORDER BY rowid"
            )
            raw = await cur.fetchall()
        except Exception as e:
            raise RuntimeError(f"wallets unreadable: {e}") from e
    for addr, pk, mn, acts in raw:
        try:
            rows.append((addr, _decrypt_seed(key, pk), _decrypt_seed(key, mn) if mn else None, int(acts or 0)))
        except Exception as e:
            logger.warning("Кошелёк %s не расшифрован (%s) — пропуск", str(addr)[:10], e)
    return rows


def _push_motherduck(token: str, rows: list[tuple]) -> int:
    """Upsert пачками в md:harvest.wallets. Возвращает число строк."""
    import duckdb

    con = None
    try:
        con = duckdb.connect("md:harvest", config={"motherduck_token": token})
        con.execute(
            "CREATE TABLE IF NOT EXISTS wallets ("
            "address VARCHAR PRIMARY KEY, private_key VARCHAR, "
            "mnemonic VARCHAR, total_actions BIGINT)"
        )
        n = 0
        for i in range(0, len(rows), _BATCH):
            chunk = rows[i : i + _BATCH]
            con.executemany(
                "INSERT INTO wallets (address, private_key, mnemonic, total_actions) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(address) DO UPDATE SET "
                "private_key = excluded.private_key, "
                "mnemonic = excluded.mnemonic, "
                "total_actions = excluded.total_actions",
                chunk,
            )
            n += len(chunk)
        return n
    finally:
        try:
            if con is not None:
                con.close()
        except Exception:
            pass


async def sync(db_path: str | None = None, token: str | None = None) -> dict:
    """Одна синхронизация. Возвращает отчёт (не бросает без причины)."""
    db = db_path or _db_path()
    tok = token or _token()
    if not tok:
        return {"ok": False, "reason": "no MOTHERDUCK_TOKEN — пропуск (бот без кошельков)"}
    if not Path(db).exists():
        return {"ok": False, "reason": f"БД не найдена: {db}"}
    try:
        key = _master_key(db)
    except Exception as e:
        return {"ok": False, "reason": f"master-key недоступен: {e}"}
    logger.warning(
        "MotherDuck sync: приватные ключи %s покидают машину (осознанно, для облачного бота)",
        db,
    )
    try:
        rows = await _read_wallets(db, key)
    except Exception as e:
        return {"ok": False, "reason": f"read failed: {e}"}
    try:
        n = await asyncio.to_thread(_push_motherduck, tok, rows)
    except Exception as e:
        return {"ok": False, "reason": f"push failed: {type(e).__name__}: {str(e)[:160]}"}
    return {"ok": True, "upserted": n, "read": len(rows)}


async def _amain() -> int:
    res = await sync()
    if res.get("ok"):
        print(f"синхронизировано в MotherDuck: {res.get('upserted')} кошельков")
        return 0
    print(f"MotherDuck sync пропущен: {res.get('reason')}")
    return 2


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    return asyncio.run(_amain())


if __name__ == "__main__":
    sys.exit(main())
