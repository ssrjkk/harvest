"""Публикация живого пульта: собирает docs/state.json из БД фермера.

Читает ТОЛЬКО публичные агрегаты (адреса, счётчики, историю циклов).
Приватные ключи и сид-фразы НИКОГДА не попадают в state.json.

Использование (с машины фермы):
    python deploy/publish_dash.py --db farming_vibevibe.db --network vibevibe
    python deploy/publish_dash.py --db farming_state.db --network robinhood --running --push

Флаг --push делает: git add docs/state.json + commit + push в master.
Без --push только обновляет файл (коммит делаете сами).

Рекомендуемое расписание: каждые 15-60 минут (каждый коммит = 1 запись
в истории; чаще раза в 5 минут не стоит).
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS_DIR = os.path.join(REPO_ROOT, "docs")

NETWORKS = [
    {
        "slug": "robinhood",
        "name": "Robinhood Chain",
        "chain_id": 46630,
        "currency": "ETH",
        "tagline": "Основной тестнет",
        "description": "Переводы и свопы, плотный фарм.",
        "rewards": "—",
        "activities": ["transfer", "vibevibe_swap", "vibevibe_mint"],
    },
    {
        "slug": "vibevibe",
        "name": "vibe/vibe",
        "chain_id": 46630,
        "currency": "ETH",
        "tagline": "Launchpad, 5% сапплая тестнету",
        "description": "claimRewards и stake всегда; buy/sell — с ключом tradeSigner.",
        "rewards": "5% supply",
        "activities": ["transfer", "claimRewards", "stake", "buy/sell*"],
    },
    {
        "slug": "flop",
        "name": "Flop Labs",
        "chain_id": 99999,
        "currency": "FLOP",
        "tagline": "chain_id — заглушка",
        "description": "compute / validate / stake + transfer.",
        "rewards": "—",
        "activities": ["flop_compute", "flop_validate", "flop_stake"],
    },
    {
        "slug": "arc",
        "name": "Arc (Minara.Fun)",
        "chain_id": 5042002,
        "currency": "ARC",
        "tagline": "Minara",
        "description": "launch / trade / add_liquidity + transfer.",
        "rewards": "—",
        "activities": ["arc_launch", "arc_trade", "arc_add_liquidity"],
    },
]


def _cols(con: sqlite3.Connection, table: str) -> set:
    try:
        return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def build_state(db_path: str, network: str, running: bool, paused: bool) -> dict:
    state: dict = {
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "network": network,
        "running": running,
        "health_factor": 1.0,
        "wallet_count": 0,
        "pool": {"actions": 0, "errors": 0, "processed": 0, "cycles": 0, "paused": paused, "dropped": 0},
        "db": {"cycles": 0},
        "networks": NETWORKS,
        "chart": {"actions": [], "errors": [], "processed": []},
        "top_wallets": [],
        "history": [],
    }
    if not os.path.exists(db_path):
        print(f"warn: БД не найдена: {db_path} — публикую пустой снапшот", file=sys.stderr)
        return state
    # WAL может быть занят фармой: открываем read-only, без блокировок записи.
    uri = "file:" + os.path.abspath(db_path) + "?mode=ro"
    try:
        con = sqlite3.connect(uri, uri=True, timeout=5)
    except sqlite3.Error as e:
        print(f"warn: не смог открыть БД read-only: {e}", file=sys.stderr)
        return state
    try:
        wcols = _cols(con, "wallets")
        if wcols:
            try:
                state["wallet_count"] = con.execute("SELECT COUNT(*) FROM wallets").fetchone()[0] or 0
            except sqlite3.Error:
                pass
            if "total_actions" in wcols:
                try:
                    rows = con.execute(
                        "SELECT address, total_actions FROM wallets "
                        "ORDER BY total_actions DESC LIMIT 10"
                    ).fetchall()
                    state["top_wallets"] = [
                        {"address": a, "actions": t or 0} for a, t in rows
                    ]
                except sqlite3.Error:
                    pass
        ccols = _cols(con, "cycle_history")
        if ccols:
            have = {"started_at", "mode", "duration_s", "wallets", "wallets_ok", "actions_ok", "errors"}
            if have <= ccols:
                try:
                    rows = con.execute(
                        "SELECT started_at, mode, duration_s, wallets, wallets_ok, "
                        "actions_ok, errors FROM cycle_history "
                        "ORDER BY rowid DESC LIMIT 60"
                    ).fetchall()
                except sqlite3.Error:
                    rows = []
                hist = []
                for i, (started, mode, dur, wal, wok, aok, err) in enumerate(reversed(rows)):
                    hist.append(
                        {
                            "id": started or f"cycle-{len(rows) - i}",
                            "errors": err or 0,
                            "actions_ok": aok or 0,
                            "duration_s": dur or 0,
                            "wallets": wal or 0,
                        }
                    )
                    _ = (mode, wok)  # метки для будущих полей
                last20 = hist[-20:]
                state["history"] = last20
                state["chart"] = {
                    "actions": [h["actions_ok"] for h in hist],
                    "errors": [h["errors"] for h in hist],
                    "processed": [h["wallets"] for h in hist],
                }
                state["db"]["cycles"] = len(rows)
                state["pool"]["cycles"] = len(rows)
                state["pool"]["actions"] = sum(h["actions_ok"] for h in hist)
                state["pool"]["errors"] = sum(h["errors"] for h in hist)
                state["pool"]["processed"] = sum(h["wallets"] for h in hist)
    finally:
        con.close()
    return state


def atomic_write(path: str, data: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def git_push_state(out_path: str) -> int:
    rel = os.path.relpath(out_path, REPO_ROOT)
    cmds = [
        ["git", "add", rel],
        ["git", "commit", "-m", "dash: update state.json"],
        ["git", "push", "origin", "master"],
    ]
    for cmd in cmds:
        r = subprocess.run(cmd, cwd=REPO_ROOT)
        if r.returncode != 0:
            if cmd[1] == "commit":
                print("info: коммитить нечего (без изменений).")
                return 0
            return r.returncode
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Собрать docs/state.json для пульта сайта.")
    ap.add_argument("--db", default="farming_vibevibe.db", help="Путь к SQLite-БД фермера")
    ap.add_argument("--network", default="vibevibe", help="slug сети для пульта")
    ap.add_argument("--out", default=os.path.join(DOCS_DIR, "state.json"))
    ap.add_argument("--running", action="store_true", help="Ферма сейчас запущена")
    ap.add_argument("--paused", action="store_true", help="Ферма на паузе")
    ap.add_argument("--push", action="store_true", help="git add+commit+push docs/state.json")
    args = ap.parse_args()

    state = build_state(args.db, args.network, args.running, args.paused)
    atomic_write(args.out, state)
    print(f"ok: {args.out} (wallets={state['wallet_count']}, cycles={state['db']['cycles']})")
    if args.push:
        return git_push_state(args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
