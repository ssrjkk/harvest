#!/usr/bin/env python3
"""Публикует state.json (метрики фермы) на GitHub Pages.

Читает публичные данные из БД портала (без приватных ключей) и обновляет
state.json в изолированной ветке gh-pages, где лежит собранная версия сайта.
Мастер-ветка от этого не растёт: в gh-pages всегда один коммит, он
перезаписывается через amend.

Запуск по расписанию (Windows Task Scheduler, задача HarvestDashPublish) каждые 30 минут:
    python publish_dash.py
Для push нужен токен: set GITHUB_TOKEN=ghp_...

exit 0 — опубликовано со свежим блобом кошельков; 2 — опубликовано без него
(нет WALLETS_KEY, sync не удался или кошельков 0), старый wallets.enc.json
в gh-pages при этом сохраняется; 1 — паблиш не удался.
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
              "terminal.html", "offline.html", "404.html",
              "manifest.webmanifest", "sw.js",
              ".nojekyll", "robots.txt")
SITE_DIRS = ("assets",)
DB = os.environ.get("PORTAL_DB", "farming_vibevibe.db")


_CELLS_MAX = 400  # максимум клеток сетки; остальные — подписью «+N»
_METRICS_STALE_S = 15 * 60  # снимок метрик старше — «неизвестно», а не цифра


def _farm_running(db_path: str) -> bool:
    """Жива ли ферма на этой БД: проба через тот же single-instance лок.

    Все точки входа (портал, auto.py, CLI) держат OS-лок из
    core.single_instance.default_lock_path на всё время работы, ОС снимает
    его при смерти процесса. Поэтому свидетельство только положительное:
    чужой держатель = процесс жив. Всё остальное (свободно, нет бэкенда,
    ошибка) — False, как раньше: лучше «стоп» при живой ферме, чем
    «работает» при мёртвой.

    Не-файловый путь (postgres-DSN и т.п.) — сразу False: лок на той же
    машине тут бессмыслен, а строить путь из DSN — плодить мусорные каталоги.
    """
    if "://" in str(db_path):
        return False
    try:
        from core.single_instance import SingleInstance, default_lock_path
    except Exception:  # noqa: BLE001 — урезанное окружение без core
        return False
    probe = SingleInstance(default_lock_path(db_path))
    try:
        if probe.acquire():
            probe.release()
            return False
        return True
    except Exception:  # noqa: BLE001 — страховка поверх acquire
        return False


def _network_slug(db_path: str) -> str:
    """Слаг сети по имени БД (канон — portal/networks.py).

    DSN разбирается через путь: postgres://…/farming_arc — тоже находится.
    Неизвестное имя — fallback vibevibe, как раньше.
    """
    raw = str(db_path)
    if "://" in raw:
        from urllib.parse import urlsplit

        base = urlsplit(raw).path.rsplit("/", 1)[-1]
    else:
        base = Path(raw).name
    for n in NETWORKS:
        db = str(n.get("db") or "")
        if base == db or (base and base == Path(db).stem):
            return str(n["slug"])
    return "vibevibe"


def _fresh_ts(raw) -> bool:
    """Снимок метрик свежий (не старше _METRICS_STALE_S)? Иначе — неизвестно.

    ts пишет SQLite (CURRENT_TIMESTAMP, UTC без зоны). Битое/будущее
    значение — тоже «неизвестно»: fail-closed в сторону «—», а не цифры.
    """
    if not raw:
        return False
    try:
        ts = datetime.strptime(str(raw).strip()[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    except (ValueError, TypeError):
        return False
    age = (datetime.now(UTC) - ts).total_seconds()
    return 0 <= age <= _METRICS_STALE_S


def _scrub_addrs(text: str) -> str:
    """Вычищает 0x-адреса из details перед публикацией (анти-сибил)."""
    import re

    return re.sub(r"0x[0-9a-fA-F]{40}", "0x…", text or "")[:120]


def _json_loads(raw) -> list:
    """JSON из БД → список. Битое значение не должно ломать публикацию."""
    import json

    try:
        val = json.loads(raw or "[]")
    except Exception:  # noqa: BLE001
        return []
    return val if isinstance(val, list) else []


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
            # PnL последнего прогона: агрегаты без адресов (см. core/pnl.py).
            pnl = None
            try:
                async with db.execute(
                    "SELECT created_at, actions_ok, actions_fail, farm_delta, faucet_delta, "
                    "sample, measured, total_wallets, window_actions, by_type, by_reason "
                    "FROM pnl_snapshots ORDER BY id DESC LIMIT 1"
                ) as cur:
                    prow = await cur.fetchone()
                if prow:
                    pnl = {
                        "created_at": str(prow[0] or ""),
                        "actions_ok": int(prow[1] or 0),
                        "actions_fail": int(prow[2] or 0),
                        "farm_delta": prow[3],
                        "faucet_delta": prow[4],
                        "sample": int(prow[5] or 0),
                        "measured": int(prow[6] or 0),
                        "total_wallets": int(prow[7] or 0),
                        "window_actions": int(prow[8] or 0),
                        "by_type": _json_loads(prow[9]),
                        "by_reason": _json_loads(prow[10]),
                    }
            except Exception:  # noqa: BLE001 — нет таблицы/снапшота: без PnL
                pnl = None
            # Живые health/dyn_workers — только из свежего снимка метрик
            # (демон портала пишет каждые 30 с). Нет таблицы, пусто или
            # протух — None: витрина покажет «—», а не 1.0/0.
            health_factor = None
            dyn_workers = None
            try:
                async with db.execute(
                    "SELECT dyn_workers, health_factor, ts FROM metrics_snapshots "
                    "ORDER BY id DESC LIMIT 1"
                ) as cur:
                    mrow = await cur.fetchone()
                if mrow is not None and mrow[1] is not None and _fresh_ts(mrow[2]):
                    dyn_workers = float(mrow[0] or 0)
                    health_factor = float(mrow[1])
            except Exception:  # noqa: BLE001 — нет таблицы: неизвестно
                pass
    except Exception as e:  # noqa: BLE001 — locked/corrupt: пустой state вместо краха
        raise RuntimeError(f"db read failed: {e}") from e

    return {
        # running — проба лока на той же машине (не идеально: ферма могла
        # стартовать/остановиться между публикациями, это последнее известное
        # состояние на момент снимка — см. updated_at), network — по имени БД.
        "running": _farm_running(DB),
        "network": _network_slug(DB),
        "networks": [
            {
                k: n[k]
                for k in (
                    "slug", "name", "chain_id", "currency", "tagline",
                    "tagline_en", "description", "description_en",
                    "rewards", "rewards_en", "activities", "activities_en",
                    "faucet", "explorer",
                )
            }
            for n in NETWORKS
        ],
        "pool": {
            "actions": r[0],
            "errors": r[1],
            "processed": r[3],
            "cycles": r[2],
            "dyn_workers": dyn_workers,
            "dropped": 0,
        },
        "health_factor": health_factor,
        "wallet_count": wallet_count,
        "db": {"cycles": r[2], "actions": r[0], "errors": r[1]},
        "top_wallets": top,
        "wallet_cells": {"v": 2, "n": wallet_count, "cells": cells, "details": details},
        "pnl": pnl,
        "history": history,
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
    except OSError as e:  # IOError — тот же OSError в py3
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
    # Счётчики тестов в витрину не вшиваем: цифры устаревают быстрее кода.
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
    except subprocess.CalledProcessError as e:
        _report_push_failure(e)
        git("fetch", "origin", DASH_BRANCH, cwd=wt, check=False)
        try:
            git("push", "--force-with-lease", "origin", f"HEAD:refs/heads/{DASH_BRANCH}", cwd=wt)
        except subprocess.CalledProcessError as e2:
            _report_push_failure(e2)
            raise
    print(f"опубликовано в GitHub Pages: {DASH_BRANCH}")


def _report_push_failure(exc: subprocess.CalledProcessError) -> None:
    """Читаемая причина отказа push.

    Отозванный GitHub-токен — самая частая причина, и без расшифровки stderr
    в логе остаётся только «exit status 128»: непонятно, что чинить.
    """
    err = (getattr(exc, "stderr", "") or "").lower()
    detail = (getattr(exc, "stderr", "") or "")[:200].replace("\n", " ")
    if "authentication failed" in err or "invalid username or token" in err or "403" in err:
        print(
            "ОТКАЗ АВТОРИЗАЦИИ GitHub: токен недействителен или отозван. "
            "Публикация невозможна, пока не восстановлен доступ. "
            "Проверь: 'gh auth status' (или 'D:\\gh.exe auth status'), "
            "затем 'gh auth login -h github.com'. "
            f"Ответ сервера: {detail}"
        )
    else:
        print(f"git push не удался: {detail}")


def _blob_from_sync(res: object) -> tuple[dict | None, int]:
    """Отчёт sync() → (blob, count). Пустой блоб публиковать нельзя.

    sync() отдаёт ok=True даже когда не расшифровал ни одного кошелька
    (не та БД, не тот master.key, пустая таблица). Публикация такого блоба
    тихо оставила бы облачного бота без кошельков, поэтому count<=0
    приравниваем к штатному пропуску: старый wallets.enc.json в gh-pages
    остаётся, бот продолжает работать с чуть устаревшими данными.
    """
    if not isinstance(res, dict) or not res.get("ok"):
        return None, 0
    count = res.get("count", 0) or 0
    if count <= 0:
        return None, 0
    blob = res.get("blob")
    if not isinstance(blob, dict):  # битый блоб — тоже пропуск, count не в счёт
        return None, 0
    return blob, count


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
            blob, blob_count = _blob_from_sync(res)
            if blob is None and isinstance(res, dict) and res.get("ok"):
                print("блоб кошельков пропущен: 0 кошельков — старый wallets.enc.json "
                      "оставлен (проверь PORTAL_DB и master.key)")
            elif blob is None:
                print(f"блоб кошельков пропущен: {res.get('reason') if isinstance(res, dict) else res}")
    try:
        publish(state, blob)
    except Exception as e:  # noqa: BLE001
        # Подробности (в т.ч. про отозванный токен) уже напечатаны выше.
        print(f"публикация не удалась: {e}")
        return 1
    if blob is None:
        print(f"опубликовано в GitHub Pages: {DASH_BRANCH} (без свежего блоба кошельков)")
        return 2
    print(f"опубликовано в GitHub Pages + блоб: {blob_count} кошельков")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
