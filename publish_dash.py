#!/usr/bin/env python3
"""Публикует state.json (метрики фермы) на GitHub Pages.

Читает публичные данные из БД портала (без приватных ключей) и обновляет
state.json в изолированной ветке gh-pages, где лежит собранная версия сайта.
Мастер-ветка от этого не растёт: в gh-pages всегда один коммит, он
перезаписывается через amend.

Запуск по расписанию (Windows Task Scheduler) каждые 5 минут:
    python publish_dash.py
Для push нужен токен: set GITHUB_TOKEN=ghp_...
"""

import asyncio
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from portal.networks import NETWORKS

ROOT = Path(__file__).resolve().parent
DOCS = ROOT / "docs"
DASH_BRANCH = "gh-pages"
SITE_DIR = ROOT / ".dash-site"
SITE_FILES = ("index.html", "app.js")
DB = os.environ.get("PORTAL_DB", "farming_vibevibe.db")


async def collect() -> dict:
    async with aiosqlite.connect(DB) as db:
        async with db.execute(
            "SELECT id, actions_ok, errors, duration_s, wallets FROM cycle_history ORDER BY id DESC LIMIT 20"
        ) as cur:
            history = [
                {"id": r[0], "actions_ok": r[1], "errors": r[2], "duration_s": r[3], "wallets": r[4]}
                for r in await cur.fetchall()
            ]
        async with db.execute("SELECT COUNT(*) FROM wallets") as cur:
            wallet_count = (await cur.fetchone())[0]
        async with db.execute(
            "SELECT address, total_actions FROM wallets ORDER BY total_actions DESC LIMIT 10"
        ) as cur:
            top = [{"address": r[0], "actions": r[1]} for r in await cur.fetchall()]
        async with db.execute(
            "SELECT COALESCE(SUM(actions_ok),0), COALESCE(SUM(errors),0), COUNT(*), "
            "COALESCE(SUM(wallets_ok),0) FROM cycle_history"
        ) as cur:
            r = await cur.fetchone()

    return {
        "running": False,
        "network": "vibevibe",
        "networks": [
            {
                k: n[k]
                for k in (
                    "slug", "name", "chain_id", "currency", "tagline",
                    "description", "rewards", "activities",
                )
            }
            for n in NETWORKS
        ],
        "pool": {
            "actions": r[0],
            "errors": r[1],
            "processed": r[3],
            "cycles": r[2],
            "dyn_workers": 0,
            "dropped": 0,
        },
        "health_factor": 1.0,
        "wallet_count": wallet_count,
        "db": {"cycles": r[2], "actions": r[0], "errors": r[1]},
        "top_wallets": top,
        "history": history,
        "chart": {"actions": [], "errors": [], "processed": []},
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }


def git(*args: str, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    # git берётся из PATH, аргументы фиксированы вызывающим кодом (не из ввода).
    return subprocess.run(  # nosec B603 B607
        ["git", *args], cwd=cwd or ROOT, check=check, capture_output=True, text=True
    )


def site_worktree() -> Path:
    """Рабочее дерево сайта: ветка gh-pages, полностью отделённая от master."""
    if (SITE_DIR / ".git").exists():
        return SITE_DIR
    git("fetch", "origin", f"+refs/heads/{DASH_BRANCH}:refs/heads/{DASH_BRANCH}", check=False)
    if git("rev-parse", "--verify", "--quiet", f"refs/heads/{DASH_BRANCH}", check=False).returncode:
        git("worktree", "add", "--orphan", "-b", DASH_BRANCH, str(SITE_DIR))
    else:
        git("worktree", "add", str(SITE_DIR), DASH_BRANCH)
    return SITE_DIR


def publish(state: dict) -> None:
    # git уже авторизован (gh credential helper): push идёт в gh-pages.
    wt = site_worktree()
    for name in SITE_FILES:
        shutil.copyfile(DOCS / name, wt / name)
    (wt / "state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    git("add", "-f", *SITE_FILES, "state.json", cwd=wt)
    if git("diff", "--cached", "--quiet", cwd=wt, check=False).returncode == 0:
        print("нет изменений — пропускаю push")
        return
    if git("rev-parse", "--verify", "--quiet", "HEAD", cwd=wt, check=False).returncode:
        git("commit", "-m", "dash: publish state.json", cwd=wt)
    else:
        git("commit", "--amend", "--no-edit", cwd=wt)
    git("push", "--force-with-lease", "origin", f"HEAD:refs/heads/{DASH_BRANCH}", cwd=wt)
    print(f"опубликовано в GitHub Pages: {DASH_BRANCH}")


async def main() -> int:
    if not Path(DB).exists():
        print(f"БД не найдена: {DB} (путь к БД через PORTAL_DB или дефолт farming_vibevibe.db)")
        return 1
    state = await collect()
    try:
        publish(state)
    except Exception as e:  # noqa: BLE001
        print(f"git push не удался: {e}")
        return 1
    try:
        from sync_motherduck import sync

        await sync()
        print("синхронизировано в MotherDuck")
    except Exception as e:  # noqa: BLE001
        print(f"MotherDuck sync пропущен: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
