#!/usr/bin/env python3
"""Синхронизация кошельков и метрик фермы в облачную БД MotherDuck.

Ферма (ПК) пишет в облако полный список кошельков с расшифрованными
ключами и сид-фразами + последний state.json. Бот на BotForge читает
эти данные даже когда ПК выключен (см. botforge/bot.py).

Требуется env: MOTHERDUCK_TOKEN. Опционально: PORTAL_DB, PORTAL_CONFIG.
Запуск: python sync_motherduck.py (или автоматически из publish_dash.py).
"""

import asyncio
import json
import os
import sys
from pathlib import Path

import yaml

from core.crypto import resolve_master_key
from core.database import Database

ROOT = Path(__file__).resolve().parent
DB = os.environ.get("PORTAL_DB", "farming_vibevibe.db")
CONFIG_FILE = os.environ.get(
    "PORTAL_CONFIG", os.environ.get("PORTAL_FARM_CONFIG", "config.yaml")
)
MD_DB = "harvest"


def _conn():
    import duckdb

    tok = os.environ["MOTHERDUCK_TOKEN"]
    con = duckdb.connect(f"md:{MD_DB}?motherduck_token={tok}")
    con.execute(
        "CREATE TABLE IF NOT EXISTS wallets (address VARCHAR PRIMARY KEY, "
        "private_key VARCHAR, mnemonic VARCHAR, total_actions INTEGER)"
    )
    con.execute("CREATE TABLE IF NOT EXISTS state (key VARCHAR PRIMARY KEY, value VARCHAR)")
    return con


async def sync() -> dict:
    """Полная синхронизация: кошельки + state в MotherDuck. Возвращает сводку."""
    if not os.environ.get("MOTHERDUCK_TOKEN"):
        return {"skipped": "MOTHERDUCK_TOKEN не задан"}
    try:
        import duckdb  # noqa: F401
    except ImportError:
        return {"skipped": "duckdb не установлен"}

    cfg = yaml.safe_load((ROOT / CONFIG_FILE).read_text(encoding="utf-8"))
    key = resolve_master_key(cfg)
    db = Database(DB, master_key=key)
    wallets = await db.get_all_wallets()

    from publish_dash import collect

    state = await collect()

    con = _conn()
    con.execute("DELETE FROM wallets")
    con.executemany(
        "INSERT INTO wallets VALUES (?, ?, ?, ?)",
        [
            (w.get("address"), w.get("private_key"), w.get("mnemonic"), int(w.get("total_actions", 0)))
            for w in wallets
        ],
    )
    con.execute(
        "INSERT OR REPLACE INTO state VALUES ('latest', ?)",
        (json.dumps(state, ensure_ascii=False),),
    )
    con.close()
    return {"wallets": len(wallets), "state": "ok"}


async def main() -> int:
    try:
        summary = await sync()
    except Exception as e:  # noqa: BLE001
        print(f"MotherDuck sync не удался: {e}")
        return 1
    print(f"MotherDuck sync: {json.dumps(summary, ensure_ascii=False)}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
