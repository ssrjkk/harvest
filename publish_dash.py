#!/usr/bin/env python3
"""Публикует state.json (метрики фермы) на GitHub Pages.

Читает публичные данные из БД портала (без приватных ключей) и обновляет
state.json в изолированной ветке gh-pages, где лежит собранная версия сайта.
Мастер-ветка от этого не растёт: в gh-pages всегда один коммит, он
перезаписывается через amend.

Запуск по расписанию (Windows Task Scheduler, задача HarvestDashPublish) каждые 30 минут:
    python publish_dash.py
Для push нужен токен: set GITHUB_TOKEN=ghp_...
"""

import asyncio
import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

from portal.networks import NETWORKS
from portal.wallet_cells import build_cells

ROOT = Path(__file__).resolve().parent
DOCS = ROOT / "docs"
DASH_BRANCH = "gh-pages"
SITE_DIR = ROOT / ".dash-site"
# Файлы сайта в ветке gh-pages (корень): лендинг + ассеты. state.json дописывается рядом.
SITE_FILES = ("index.html", "terminal.html", ".nojekyll", "robots.txt")
SITE_DIRS = ("assets",)
DB = os.environ.get("PORTAL_DB", "farming_vibevibe.db")


_CELLS_MAX = 400  # максимум клеток сетки; остальные — подписью «+N»


def _plural_files(n: int) -> str:
    """файл/файла/файлов для счётчика (публикация, чтобы не править руками)."""
    if n % 10 == 1 and n % 100 != 11:
        return "файл"
    if 2 <= n % 10 <= 4 and n % 100 not in (12, 13, 14):
        return "файла"
    return "файлов"


def collect_test_counts() -> tuple[int | None, int | None]:
    """Число (тестов, файлов) через pytest --collect-only.

    Для подстановки в gh-pages копии при публикации (числа в master —
    лишь fallback-снапшот, руками не правим). Ошибка/таймаут → (None, None).
    """
    import re

    try:
        r = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/", "--collect-only",
             "--ignore=tests/test_load_opt.py"],
            cwd=ROOT, capture_output=True, text=True, timeout=180,
        )
    except Exception:  # noqa: BLE001
        return None, None
    out = (r.stdout or "") + "\n" + (r.stderr or "")
    m = re.search(r"(\d+)\s+tests?\s+collected", out)
    if not m:
        return None, None
    files = len({line.split("::")[0] for line in out.splitlines()
                 if line.startswith("tests/")})
    return int(m.group(1)), files or None


def inject_test_counts(site_dir: Path, tests: int, files: int) -> int:
    """Подставляет свежие счётчики в gh-pages копии (index/terminal, site.js).

    Возвращает число замен. DOCUMENTATION/README
    в gh-pages не едут и остаются снапшотом.
    """
    import re

    targets = [site_dir / "index.html", site_dir / "terminal.html",
               site_dir / "assets" / "site.js"]
    patterns = [
        (re.compile(r"\d[\d\s]*\s+TESTS"), f"{tests} TESTS"),
        (re.compile(r"\d+\s*·\s*\d+\s+файл[а-я]*"), f"{tests} · {files} {_plural_files(files)}"),
        (re.compile(r"\d+\s+tests\s*·\s*\d+\s+files"), f"{tests} tests · {files} files"),
    ]
    made = 0
    for path in targets:
        if not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for rx, repl in patterns:
            text, n = rx.subn(repl, text)
            made += n
        try:
            path.write_text(text, encoding="utf-8")
        except OSError:
            pass
    return made


def _cell_state(actions: int, attempts: int, last_cycle: int, latest: int | None) -> str:
    """Один символ состояния кошелька: i idle, w working, o ok, e err, r rest.

    Единая реализация — portal/wallet_cells.py (сайт и бот используют её же).
    """
    from portal.wallet_cells import cell_state

    return cell_state(actions, attempts, last_cycle, latest)


async def collect() -> dict:
    async with aiosqlite.connect(DB) as db:
        async with db.execute(
            "SELECT id, actions_ok, errors, duration_s, wallets FROM cycle_history ORDER BY id DESC LIMIT 20"
        ) as cur:
            history = [
                {"id": r[0], "actions_ok": r[1], "errors": r[2], "duration_s": r[3], "wallets": r[4]}
                for r in await cur.fetchall()
            ]
        latest = history[0]["id"] if history else None
        async with db.execute("SELECT COUNT(*) FROM wallets") as cur:
            wallet_count = (await cur.fetchone())[0]
        # Топ — только порядковые номера и счётчики: адреса в публичную
        # ветку не публикуем (анти-сибил: не связывать кошельки между собой).
        async with db.execute(
            "SELECT total_actions FROM wallets ORDER BY total_actions DESC LIMIT 10"
        ) as cur:
            top = [{"n": i + 1, "actions": (r[0] or 0)} for i, r in enumerate(await cur.fetchall())]
        try:
            async with db.execute(
                "SELECT total_actions, total_attempts, last_cycle FROM wallets "
                "ORDER BY rowid LIMIT ?",
                (_CELLS_MAX,),
            ) as cur:
                sample = await cur.fetchall()
            cells = build_cells(
                [(a or 0, t or 0, c) for a, t, c in sample], latest
            )
        except Exception:  # noqa: BLE001 — старая схема без колонок: все idle
            cells = ""
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
                    "description", "rewards", "activities", "faucet", "explorer",
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
        "wallet_cells": {"v": 1, "n": wallet_count, "cells": cells},
        "history": history,
        "chart": {"actions": [], "errors": [], "processed": []},
        "updated_at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
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
    for d in SITE_DIRS:
        shutil.rmtree(wt / d, ignore_errors=True)
        shutil.copytree(DOCS / d, wt / d)
    # Счётчики тестов — генерацией при публикации (не руками): gh-pages копии
    # получают свежие числа, master остаётся fallback-снапшотом.
    counted = collect_test_counts()
    if counted[0] and counted[1]:
        n = inject_test_counts(wt, counted[0], counted[1])
        print(f"счётчики тестов: {counted[0]} · {counted[1]} ({n} замен)")
    else:
        print("счётчики тестов: пропуск (pytest недоступен)")
    (wt / "state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    git("add", "-f", *SITE_FILES, *SITE_DIRS, "state.json", cwd=wt)
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
