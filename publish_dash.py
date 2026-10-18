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
SITE_FILES = ("index.html", "field.html", "demo.html", "docs.html",
              "terminal.html", "offline.html", "manifest.webmanifest", "sw.js",
              ".nojekyll", "robots.txt")
SITE_DIRS = ("assets",)
DB = os.environ.get("PORTAL_DB", "farming_vibevibe.db")


_CELLS_MAX = 400  # максимум клеток сетки; остальные — подписью «+N»


def _scrub_addrs(text: str) -> str:
    """Вычищает 0x-адреса из details перед публикацией (анти-сибил)."""
    import re

    return re.sub(r"0x[0-9a-fA-F]{40}", "0x…", text or "")[:120]


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
    """Подставляет свежие счётчики в gh-pages копии (все страницы + js).

    DOCUMENTATION/README в gh-pages не едут и остаются снапшотом.
    """
    import re

    targets = sorted(site_dir.glob("*.html")) + sorted((site_dir / "assets").glob("*.js"))
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
    try:
        async with aiosqlite.connect(DB) as db:
            try:
                async with db.execute(
                    "SELECT id, actions_ok, errors, duration_s, wallets FROM cycle_history ORDER BY id DESC LIMIT 20"
                ) as cur:
                    history = [
                        {"id": r[0], "actions_ok": r[1], "errors": r[2], "duration_s": r[3], "wallets": r[4]}
                        for r in await cur.fetchall()
                    ]
            except Exception:  # noqa: BLE001 — пустая БД / нет таблицы
                history = []
            latest = history[0]["id"] if history else None
            try:
                async with db.execute("SELECT COUNT(*) FROM wallets") as cur:
                    row = await cur.fetchone()
                    wallet_count = row[0] if row else 0
            except Exception:  # noqa: BLE001
                wallet_count = 0
            # Топ — только порядковые номера и счётчики: адреса в публичную
            # ветку не публикуем (анти-сибил: не связывать кошельки между собой).
            try:
                async with db.execute(
                    "SELECT total_actions FROM wallets ORDER BY total_actions DESC LIMIT 10"
                ) as cur:
                    top = [{"n": i + 1, "actions": (r[0] or 0)} for i, r in enumerate(await cur.fetchall())]
            except Exception:  # noqa: BLE001
                top = []
            try:
                async with db.execute(
                    "SELECT total_actions, total_attempts, last_cycle FROM wallets "
                    "ORDER BY rowid LIMIT ?",
                    (_CELLS_MAX,),
                ) as cur:
                    sample = await cur.fetchall()
                triples = [(a or 0, t or 0, c) for a, t, c in sample]
                cells = build_cells(triples, latest)
                # Детальный инспектор (без адресов/ключей): порядковый номер,
                # число действий/попыток, последний цикл и последняя ошибка —
                # для клика по клетке. Время следующего пробуждения
                # опубликовать нельзя: задержки разыгрываются случайно
                # на каждом цикле и нигде не хранятся.
                try:
                    async with db.execute(
                        "SELECT (SELECT substr(a.details, 1, 120) "
                        "FROM actions_log a WHERE a.address = w.address "
                        "AND a.success = 0 ORDER BY a.id DESC LIMIT 1) "
                        "FROM wallets w ORDER BY w.rowid LIMIT ?",
                        (_CELLS_MAX,),
                    ) as cur:
                        errs = [(r[0] or "") for r in await cur.fetchall()]
                except Exception:  # noqa: BLE001 — нет actions_log: без ошибок
                    errs = []
                # Анти-сибил: адреса из details вычищаем (to=0x… — это наши же
                # кошельки; публичная ветка не должна их связывать).
                details = [
                    {"n": i + 1, "a": a, "t": t, "c": c if c is not None else -1,
                     "e": _scrub_addrs(errs[i]) if i < len(errs) else ""}
                    for i, (a, t, c) in enumerate(triples)
                ]
            except Exception:  # noqa: BLE001 — старая схема без колонок: все idle
                cells = ""
                details = []
            try:
                async with db.execute(
                    "SELECT COALESCE(SUM(actions_ok),0), COALESCE(SUM(errors),0), COUNT(*), "
                    "COALESCE(SUM(wallets_ok),0) FROM cycle_history"
                ) as cur:
                    r = await cur.fetchone()
            except Exception:  # noqa: BLE001
                r = (0, 0, 0, 0)
    except Exception as e:  # noqa: BLE001 — locked/corrupt: пустой state вместо краха
        raise RuntimeError(f"db read failed: {e}") from e

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
        "wallet_cells": {"v": 2, "n": wallet_count, "cells": cells, "details": details},
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
        # Долгоживущий воркер: подтягиваем свежий gh-pages, иначе amend поверх stale.
        git("fetch", "origin", f"+refs/heads/{DASH_BRANCH}:refs/heads/{DASH_BRANCH}", check=False)
        return SITE_DIR
    try:
        git("worktree", "prune", check=False)
    except Exception:  # noqa: BLE001
        pass
    # locked-файл НЕ сносим: это штатная защита git от concurrent-использования.
    # Stale-лок после краха чистит сам `git worktree prune` выше.
    git("fetch", "origin", f"+refs/heads/{DASH_BRANCH}:refs/heads/{DASH_BRANCH}", check=False)
    if git("rev-parse", "--verify", "--quiet", f"refs/heads/{DASH_BRANCH}", check=False).returncode:
        git("worktree", "add", "--orphan", "-b", DASH_BRANCH, str(SITE_DIR))
    else:
        git("worktree", "add", str(SITE_DIR), DASH_BRANCH)
    return SITE_DIR


def _publish_lock_path() -> Path:
    return ROOT / ".dash-site.lock"


def _acquire_publish_lock():
    """Межпроцессный лок publish (flock/msvcrt). None — публикуем без лока."""
    try:
        import fcntl as _fcntl  # POSIX
    except ImportError:
        _fcntl = None  # Windows: ниже msvcrt
    try:
        fh = open(_publish_lock_path(), "w")
    except OSError as e:
        print(f"warn: лок publish не создан ({e}) — иду без лока")
        return None
    try:
        if _fcntl is not None:
            _fcntl.flock(fh.fileno(), _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        else:
            import msvcrt as _msvcrt

            _msvcrt.locking(fh.fileno(), _msvcrt.LK_NBLCK, 1)
        return fh
    except (OSError, IOError) as e:
        print(f"publish уже идёт в другом процессе — пропуск ({e})")
        try:
            fh.close()
        except Exception:  # noqa: BLE001
            pass
        return "busy"


def publish(state: dict, wallets_blob: dict | None = None) -> None:
    # Межпроцессный лок: два overlapping-запуска (таск 30 мин + collect до 180с)
    # иначе мутят одно worktree и ловят index.lock / проигранный lease.
    lock_fh = _acquire_publish_lock()
    if lock_fh == "busy":
        return
    try:
        _publish_inner(state, wallets_blob)
    finally:
        try:
            if lock_fh is not None:
                lock_fh.close()
        except Exception:  # noqa: BLE001
            pass


def _publish_inner(state: dict, wallets_blob: dict | None = None) -> None:
    # git уже авторизован (gh credential helper): push идёт в gh-pages.
    wt = site_worktree()
    for name in SITE_FILES:
        src = DOCS / name
        if not src.exists():
            print(f"warn: {name} нет в docs/ — пропускаю")
            continue
        try:
            shutil.copyfile(src, wt / name)
        except OSError as e:
            print(f"warn: не скопирован {name}: {e}")
    for d in SITE_DIRS:
        src = DOCS / d
        if not src.exists():
            print(f"warn: {d}/ нет в docs/ — пропускаю")
            continue
        # Windows: файл в worktree может держать лок (индексатор/редактор) —
        # пара попыток с паузой вместо молчаливого полупустого assets/.
        ok = False
        for attempt in (1, 2, 3):
            shutil.rmtree(wt / d, ignore_errors=True)
            try:
                shutil.copytree(src, wt / d)
                ok = True
                break
            except OSError as e:
                if attempt == 3:
                    print(f"warn: не скопирован {d}/: {e}")
                else:
                    import time as _time

                    _time.sleep(2 * attempt)
        if not ok:
            continue
    # Счётчики тестов — генерацией при публикации (не руками): gh-pages копии
    # получают свежие числа, master остаётся fallback-снапшотом.
    counted = collect_test_counts()
    if counted[0] and counted[1]:
        n = inject_test_counts(wt, counted[0], counted[1])
        print(f"счётчики тестов: {counted[0]} · {counted[1]} ({n} замен)")
    else:
        print("счётчики тестов: пропуск (pytest недоступен)")
    (wt / "state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    extra = ["state.json"]
    if wallets_blob is not None:
        # Шифротекст кошельков для облачного бота (ключа здесь нет — только ct).
        (wt / "wallets.enc.json").write_text(
            json.dumps(wallets_blob, ensure_ascii=False), encoding="utf-8")
        extra.append("wallets.enc.json")
    git("add", "-f", *SITE_FILES, *SITE_DIRS, *extra, cwd=wt)
    if git("diff", "--cached", "--quiet", cwd=wt, check=False).returncode == 0:
        print("нет изменений — пропускаю push")
        return
    if git("rev-parse", "--verify", "--quiet", "HEAD", cwd=wt, check=False).returncode:
        git("commit", "-m", "dash: publish state.json", cwd=wt)
    else:
        git("commit", "--amend", "--no-edit", cwd=wt)
    # force-with-lease может проиграть гонку с параллельным publish;
    # один retry с fetch перед второй попыткой.
    try:
        git("push", "--force-with-lease", "origin", f"HEAD:refs/heads/{DASH_BRANCH}", cwd=wt)
    except subprocess.CalledProcessError:
        git("fetch", "origin", DASH_BRANCH, cwd=wt, check=False)
        git("push", "--force-with-lease", "origin", f"HEAD:refs/heads/{DASH_BRANCH}", cwd=wt)
    print(f"опубликовано в GitHub Pages: {DASH_BRANCH}")


async def main() -> int:
    if not Path(DB).exists():
        print(f"БД не найдена: {DB} (путь к БД через PORTAL_DB или дефолт farming_vibevibe.db)")
        return 1
    try:
        state = await collect()
    except Exception as e:  # noqa: BLE001 — нет таблиц/БД locked: понятный exit, а не трейс
        print(f"collect не удался ({DB}): {e}")
        return 1
    # Блоб кошельков собираем ДО публикации — один коммит/пуш.
    # Без WALLETS_KEY — штатный пропуск (exit 2, не ошибка паблиша).
    # Старый wallets.enc.json в gh-pages при этом остаётся — бот продолжает
    # работать с чуть устаревшими данными, а не падает.
    blob = None
    blob_count = 0
    if not os.environ.get("WALLETS_KEY", "").strip():
        print("блоб кошельков пропущен: no WALLETS_KEY — пропуск (бот без свежих кошельков)")
    else:
        try:
            from tools.sync_wallets_blob import sync as sync_blob  # type: ignore
        except ImportError:
            try:
                from sync_wallets_blob import sync as sync_blob  # type: ignore  # legacy рядом с publish
            except ImportError as e:
                print(f"блоб кошельков пропущен (нет модуля sync_wallets_blob): {e}")
                sync_blob = None
        if sync_blob is not None:
            try:
                res = await sync_blob()
            except Exception as e:  # noqa: BLE001
                print(f"блоб кошельков пропущен: {e}")
                res = None
            if isinstance(res, dict) and res.get("ok"):
                blob, blob_count = res.get("blob"), res.get("count", 0)
            else:
                print(f"блоб кошельков пропущен: {res.get('reason') if isinstance(res, dict) else res}")
    try:
        publish(state, blob)
    except Exception as e:  # noqa: BLE001
        print(f"git push не удался: {e}")
        return 1
    if blob is None:
        print(f"опубликовано в GitHub Pages: {DASH_BRANCH} (без свежего блоба кошельков)")
        return 2
    print(f"опубликовано в GitHub Pages + блоб: {blob_count} кошельков")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
