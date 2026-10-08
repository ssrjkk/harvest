#!/usr/bin/env python3
"""Точка входа: современный интерфейс (Rich) для фармера HARVEST.

Меню: генерация кошельков, фарм (цикл / live-консоль), живой монитор,
кран, статистика, экспорт/импорт, диагностика RPC, бэкапы, лицензия.

Лицензия: доступ по паролю, управляется владельцем дистанционно через
license-hub (GitHub raw). Без авторизованного пароля программа не запускается.
"""

import asyncio
import json
import logging
import os
import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import yaml
from colorama import Fore, Style, init
from eth_account import Account

from core import ui
from core.config import load_config as _load_config
from core.crypto import MasterKeyError, resolve_master_key
from core.database import Database, _redact_rpc_url
from core.doctor import doctor
from core.faucet import Faucet
from core.license import LicenseError, enforce_license_async
from core.logger import setup_logging
from core.network import NetworkManager
from core.performance import effective_gen_workers, effective_max_workers
from core.pool import FarmerPool
from core.single_instance import SingleInstance, default_lock_path
from core.utils import truncate_address
from core.version import VERSION
from core.wallet import WalletManager

init(autoreset=True)

logger = logging.getLogger(__name__)
CONFIG_FILE = os.environ.get("FARMER_CONFIG", "config.yaml")

BANNER_TITLE = "HARVEST"
BANNER_SUB = (
    f"v{VERSION} · Testnet Automation · Robinhood Chain\n"
    "  RPC + ThreadPool + асинхронный пул — быстрый фарм\n"
    "  Доступ по паролю · дистанционное управление через license-hub"
)


def load_config(path: str | None = None) -> dict:
    config = _load_config(path or CONFIG_FILE)
    if config is None:
        sys.exit(1)
    return config


def menu(u: ui.UI) -> str:
    groups = [
        (
            "ФАРМ",
            [
                ("1", "Создать кошельки"),
                ("2", "Один цикл фарма"),
                ("3", "Фарм — live-консоль"),
                ("4", "Монитор балансов (live)"),
            ],
        ),
        ("СРЕДСТВА", [("5", "Кран — пополнить все")]),
        ("ДАННЫЕ", [("6", "Статистика и топ"), ("7", "Экспорт"), ("8", "Импорт"), ("C", "История циклов фарма")]),
        (
            "СЕРВИС",
            [
                ("G", "Веб-интерфейс (GUI)"),
                ("9", "Диагностика RPC"),
                ("D", "Полная диагностика (doctor)"),
                ("B", "Бэкапы БД"),
                ("L", "Лицензия"),
                ("S", "Сменить тестнет"),
                ("H", "Справка"),
                ("0", "Выход"),
            ],
        ),
    ]
    lines = []
    for head, items in groups:
        if u.use_rich:
            lines.append(f"  [bold cyan]{head}[/bold cyan]")
        else:
            lines.append(f"  {head}")
        for key, label in items:
            lines.append(u.menu_key(label, key))
        lines.append("")
    u.menu_panel("ГЛАВНОЕ МЕНЮ", "\n".join(lines))
    u.hint("Подсказка: номер + Enter · Ctrl+C в любом экране — выход")
    try:
        return input(f"\n  {Fore.CYAN}Введите: {Style.RESET_ALL}").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return "0"


# ------------------------------------------------------------------
# ЭКРАНЫ
# ------------------------------------------------------------------


async def screen_stats(u: ui.UI, db: Database) -> None:
    stats = await db.get_stats()
    u.print()
    u.table(
        "СТАТИСТИКА",
        ["Показатель", "Значение"],
        [
            ["Кошельков", stats["total_wallets"]],
            ["Всего действий", stats["total_actions"]],
            ["Успешных", stats["success_actions"]],
            [
                "Процент успеха",
                f"{stats['success_actions'] / max(stats['total_actions'], 1) * 100:.1f}%",
            ],
            ["Записей в логе", stats["total_log_entries"]],
        ],
    )
    top = await db.get_top_wallets(10)
    if top:
        u.table(
            "ТОП-АКТИВНОСТЬ (по actions)",
            ["#", "Адрес", "Действий"],
            [[i, truncate_address(w["address"]), w["total_actions"]] for i, w in enumerate(top[:10], 1)],
        )
        u.toast(
            f"Лидер: {truncate_address(top[0]['address'])} — {top[0]['total_actions']} действий",
            kind="info",
        )
    else:
        u.hint("  Кошельков пока нет — создайте через пункт 1.")

    cycles = await db.get_cycle_stats()
    u.table(
        "ЦИКЛЫ ФАРМА (журнал)",
        ["Показатель", "Значение"],
        [
            ["Завершено циклов", cycles["cycles"]],
            ["Кошельков обработано", cycles["wallets"]],
            ["Действий за циклы", cycles["actions"]],
            ["Время фарма", f"{cycles['duration_s'] / 3600:.1f} ч"],
            ["Последний цикл", str(cycles["last_started_at"] or "—").replace("T", " ")[:19]],
        ],
    )


async def screen_history(u: ui.UI, db: Database) -> None:
    rows = await db.get_cycle_history(20)
    if not rows:
        u.print("  Циклов фарма ещё нет — запустите фарм (пункт 2 или 3).", style="yellow")
        return
    u.print()
    table_rows = []
    for i, r in enumerate(rows, 1):
        rpc_detail = (_redact_rpc_url(r["rpc_url"]) or "")[:28]
        if r.get("rpc_calls"):
            rate = (r["rpc_errors"] / max(r["rpc_calls"], 1)) * 100
            rpc_detail += f" | {r.get('rpc_latency_ms', 0):.0f}ms/{rate:.0f}%err"
        table_rows.append(
            [
                i,
                str(r["started_at"] or "").replace("T", " ")[:19],
                f"{r['duration_s']:.0f}",
                r["wallets"],
                r["wallets_ok"],
                r["actions_ok"],
                r["errors"],
                rpc_detail,
            ]
        )
    u.table(
        "ИСТОРИЯ ЦИКЛОВ (последние 20)",
        ["#", "Старт", "Длит(с)", "Кошельков", "ОК", "Действий", "Ошибки", "RPC"],
        table_rows,
    )


async def screen_make_wallets(u: ui.UI, config: dict, db: Database) -> None:
    raw = input(f"  {Fore.CYAN}Количество кошельков [20]: {Style.RESET_ALL}").strip()
    count = int(raw) if raw.isdigit() and int(raw) > 0 else 20
    wm = WalletManager(config, db)

    async def _create():
        return await wm.create_wallets(count)

    created = await u.spinner(f"Генерация {count} кошельков...", _create())
    u.out_ok(f"Создано {len(created)} кошельков")
    if config.get("database", {}).get("master_key"):
        u.typewrite("  Приватные ключи зашифрованы master-ключом в БД.")
    else:
        u.print("  Внимание: master-ключ не настроен.", style="yellow")


async def screen_farm_once(u: ui.UI, config: dict, db: Database) -> None:
    wallets = await db.get_all_wallets()
    if not wallets:
        u.print("  Нет кошельков. Создайте сначала (пункт 1).", style="red")
        return
    guard = SingleInstance(default_lock_path(config["database"]["path"]))
    if not guard.acquire():
        u.print(f"  Уже работает другой экземпляр фармера (pid={guard.holder_pid()}).", style="red")
        u.print("  На одной БД может работать только один пул — выходим.", style="yellow")
        return
    pool = FarmerPool(config, db)
    try:
        all_addresses = [w["address"] for w in wallets]
        u.typewrite(f"  Старт цикла: {len(wallets)} кошельков, workers={effective_max_workers(config)}")
        stop_event = asyncio.Event()
        results = await pool.run_once(wallets, all_addresses, stop_event=stop_event)
        ok = sum(1 for _, v in results if v > 0)
        acts = sum(v for _, v in results)
        u.out_ok(f"Цикл завершён: {ok}/{len(wallets)} кошельков, действий {acts}")
    finally:
        try:
            await pool.close()
        except Exception as e:
            logger.warning(f"Ошибка закрытия пула: {e}")
        try:
            guard.release()
        except Exception as e:
            logger.warning(f"Ошибка снятия single-instance lock: {e}")


async def screen_farm_live(u: ui.UI, config: dict, db: Database, stop_event: asyncio.Event) -> None:
    """Live-консоль фарма: обновляемая таблица кошельков в реальном времени."""
    guard = SingleInstance(default_lock_path(config["database"]["path"]))
    if not guard.acquire():
        u.print(f"  Уже работает другой экземпляр фармера (pid={guard.holder_pid()}).", style="red")
        u.print("  На одной БД может работать только один пул — выходим.", style="yellow")
        return
    # Один NetworkManager на пул и на latency-пробы (не плодим два RPC-провайдера).
    # net может остаться None, если конструктор сети упадёт ДО назначения: лок
    # уже захвачен выше — его освобождаем в except, иначе следующая попытка
    # фарма навсегда упрётся в «уже работает другой экземпляр».
    net: NetworkManager | None = None
    pool: FarmerPool | None = None
    try:
        net = NetworkManager(config, db)
        pool = FarmerPool(config, db, network=net)
        task = asyncio.create_task(pool.run_forever(stop_event))
    except Exception:
        if pool is not None:
            await pool.close()
        elif net is not None:
            await net.close()
        try:
            guard.release()
        except Exception as e:
            logger.warning(f"Ошибка снятия single-instance lock: {e}")
        raise
    state: dict[str, Any] = {
        "wallets": [],
        "latency": None,
        "t0": time.monotonic(),
        "lt": 0.0,
        "pool_err": "",
        "fatal": False,
    }

    def _on_task_done(t) -> None:
        if t.cancelled() or state["fatal"]:
            return
        err = t.exception()
        if err is not None:
            state["fatal"] = True
            state["pool_err"] = str(err)[:120]
            u.toast(f"Ошибка фарма: {err}", kind="err")
            stop_event.set()

    task.add_done_callback(_on_task_done)

    async def _producer():
        while not (stop_event.is_set() or task.done()):
            try:
                # Только адреса и счётчики — лидерборд НЕ должен дешифровать ключи.
                state["wallets"] = await db.get_top_wallets(15)
                state["enc"] = await db.any_seed_encrypted()
                now = time.monotonic()
                if now - state["lt"] >= 10:
                    state["latency"] = await net.latency_probe()
                    state["lt"] = now
            except Exception:
                pass
            await asyncio.sleep(2.0)

    prod = asyncio.create_task(_producer())

    def _render():
        rows = []
        max_a = max((w.get("total_actions", 0) for w in state["wallets"]), default=1)
        for i, w in enumerate(state["wallets"], 1):
            prog = u.bar(w.get("total_actions", 0) / max_a, 10)
            action_txt = (
                f"[{'green' if w.get('total_actions', 0) > 0 else 'yellow'}]{w['total_actions']}[/]"
                if u.use_rich
                else str(w["total_actions"])
            )
            rows.append(
                [
                    i,
                    truncate_address(w["address"]),
                    action_txt,
                    prog,
                    "enc" if state.get("enc") else "key",
                ]
            )
        return rows

    def _cap():
        for k in u.poll_keys():
            if k == "p":
                if pool.paused:
                    pool.resume()
                    u.toast("Пул продолжает работу", kind="info")
                else:
                    pool.pause()
                    u.toast("Пул на паузе", kind="warn")
            elif k == "q":
                stop_event.set()
        elapsed = int(time.monotonic() - state["t0"])
        ps = pool.live_stats()
        err = state["pool_err"] or (f"ошибки {ps['errors']}" if ps["errors"] else "ошибки 0")
        per = f"{ps['actions'] / elapsed * 60:.1f} действ/мин" if elapsed > 0 else "0 действ/мин"
        dot = u.dot(state["latency"])
        lat_txt = f"{state['latency']:.0f} мс" if state["latency"] is not None else "—"
        pause_mark = " [PAUSE]" if pool.paused else ""
        ok = ps["processed"] - ps["errors"]
        dyn = f"воркеры {ps['dyn_workers']} (health {ps['health']:.2f})"
        return (
            f"{u.frame()} живых {len(state['wallets'])} | действий {ps['actions']} | "
            f"ок {ok} из {ps['processed']} | {err} | RPC {dot} {lat_txt} | {per}{pause_mark} | {dyn}"
        )

    headers = ["#", "Адрес", "Действий", "Прогресс", "Ключ"]
    title = "LIVE ФАРМ · P/Q · Ctrl+C"

    try:
        await u.live_table(
            title,
            _render,
            headers,
            period=1.5,
            stop=lambda: stop_event.is_set() or task.done(),
            caption=_cap,
        )
    finally:
        prod.cancel()
        if not task.done():
            task.cancel()
        try:
            if pool is not None:
                await pool.close()
        except Exception as e:
            logger.warning(f"Ошибка закрытия пула: {e}")
        try:
            guard.release()
        except Exception as e:
            logger.warning(f"Ошибка снятия single-instance lock: {e}")
        ps = pool.live_stats()
    if state["pool_err"]:
        u.toast(f"Фарм прерван: {state['pool_err']}", kind="err")
    else:
        u.toast(
            f"Фарм завершён: {ps['processed']} кошельков, {ps['actions']} действий, ошибки {ps['errors']}",
            kind="ok",
        )


async def screen_monitor(u: ui.UI, config: dict, db: Database, stop_event: asyncio.Event) -> None:
    """Живой монитор балансов (автообновление каждые 5 сек)."""
    net = NetworkManager(config, db)
    sem = asyncio.Semaphore(effective_max_workers(config))
    state: dict[str, Any] = {
        "rows": [],
        "total": 0.0,
        "funded": 0,
        "zero": 0,
        "latency": None,
        "paused": False,
    }

    async def _producer():
        while not (stop_event.is_set()):
            if state.get("paused"):
                await asyncio.sleep(0.5)
                continue
            addrs = await db.get_all_addresses()
            state["latency"] = await net.latency_probe()
            if not addrs:
                state["rows"] = [["—", "—", "нет кошельков"]]
            else:

                async def _one(addr: str):
                    async with sem:
                        bal = await net.get_balance(addr)
                        return truncate_address(addr), bal

                res = await asyncio.gather(*[_one(a) for a in addrs], return_exceptions=True)
                rows = []
                total = 0.0
                funded = 0
                for r in res:
                    if isinstance(r, BaseException):
                        rows.append([str(r).split(":")[0][:14], "ERR", "—"])
                        continue
                    addr, bal = r
                    total += bal if isinstance(bal, float) else 0
                    has = isinstance(bal, float) and bal > 0.001
                    funded += 1 if has else 0
                    if u.use_rich:
                        col = "green" if has else "red"
                        rows.append(
                            [
                                addr,
                                f"[{col}]{bal:.6f}[/{col}]",
                                f"[{col}]{'OK' if has else 'LOW'}[/{col}]",
                            ]
                        )
                    else:
                        rows.append([addr, f"{bal:.6f}", "OK" if has else "LOW"])
                state["rows"] = rows
                state["total"] = total
                state["funded"] = funded
                state["zero"] = len(addrs) - funded
            await asyncio.sleep(5.0)

    prod = asyncio.create_task(_producer())

    def _render():
        if not state["rows"]:
            return [["—", "—", "обновление..."]]
        return state["rows"]

    def _cap():
        for k in u.poll_keys():
            if k == "p":
                state["paused"] = not state["paused"]
                u.toast(
                    "Обновление остановлено" if state["paused"] else "Обновление продолжается",
                    kind="warn" if state["paused"] else "info",
                )
            elif k == "q":
                stop_event.set()
        dot = u.dot(state["latency"])
        lat_txt = f"{state['latency']:.0f} мс" if state["latency"] is not None else "—"
        pause_mark = " [PAUSE]" if state["paused"] else ""
        return (
            f"{u.frame()} баланс {state['total']:.6f} | заправлено {state['funded']} | "
            f"пусто {state['zero']} | RPC {dot} {lat_txt}{pause_mark}"
        )

    try:
        await u.live_table(
            "МОНИТОР КОШЕЛЬКОВ · P/Q · Ctrl+C",
            _render,
            ["Адрес", "Баланс", "Статус"],
            period=5.0,
            stop=lambda: stop_event.is_set(),
            caption=_cap,
        )
    finally:
        prod.cancel()
        await net.close()


async def screen_doctor(u: ui.UI, config: dict) -> None:
    """Полная самодиагностика окружения (конфиг, ключ, БД, RPC, кран, лицензия)."""
    u.panel(
        "ДИАГНОСТИКА",
        "  Проверяю конфиг, master-ключ, БД, сеть, кран и licence-гейт...",
        color="bright_blue",
    )
    await u.spinner("Диагностика...", doctor(u, config))
    u.toast("Самодиагностика завершена", kind="info")
    u.hint("  Всё работает — можно запускать фарм. Красные пункты — подсказка, что чинить.")


async def screen_rpc_diag(u: ui.UI, config: dict, db: Database) -> None:
    print(f"  Зонд RPC: {_redact_rpc_url(config['network']['rpc_url']) or ''}...")
    net = NetworkManager(config, db)
    try:
        probes = await net.probe_all()
        u.table(
            "ДИАГНОСТИКА RPC",
            ["Эндпоинт", "Задержка", "Статус"],
            [
                [
                    _redact_rpc_url(url) or url,
                    f"{ms:.0f} мс" if ms is not None else "—",
                    f"{u.dot(ms)} {'OK' if ms is not None else 'FAIL'}",
                ]
                for url, ms in probes
            ],
        )
        d = net.diagnostics([ms for _, ms in probes])
        cb = "ОТКРЫТ (блокирует вызовы)" if d["cb_state"] == "OPEN" else d["cb_state"]
        u.table(
            "МЕТРИКИ",
            ["Параметр", "Значение"],
            [
                ["Активный RPC", d["active"]],
                ["Chain ID", d["chain_id"]],
                [
                    "Вызовов / сессия",
                    f"{d['calls']} (среднее {d['avg_ms']:.0f} мс)" if d["avg_ms"] else str(d["calls"]),
                ],
                ["Ошибок", d["errors"]],
                ["Circuit breaker", cb],
                ["Сбоев подряд", d["cb_failures"]],
            ],
        )
    finally:
        await net.close()


async def screen_backups(u: ui.UI, db: Database) -> None:
    while True:
        backups = db.list_backups()
        u.print()
        u.table(
            "БЭКАПЫ БАЗЫ ДАННЫХ",
            ["#", "Файл", "Размер"],
            [[i, b.name, f"{b.stat().st_size} Б"] for i, b in enumerate(backups)] or [["—", "нет бэкапов", "—"]],
        )
        u.print("  [1] сделать бэкап   [2] восстановить (индекс)   [3] удалить (индекс)   [0] назад")
        try:
            choice = input(f"  {Fore.CYAN}Действие: {Style.RESET_ALL}").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return
        if choice in ("0", ""):
            return
        if choice == "1":
            await u.spinner("Бэкап...", db.backup_now())
            u.toast("Бэкап создан", kind="ok")
        elif choice == "2":
            idx_raw = input("  Индекс для восстановления [0]: ").strip()
            idx = int(idx_raw) if idx_raw.isdigit() else 0
            ok = await db.restore_backup(idx)
            u.toast(
                "Восстановлено" if ok else "Восстановление не выполнено",
                kind="ok" if ok else "err",
            )
        elif choice == "3":
            idx_raw = input("  Индекс для удаления: ").strip()
            if idx_raw.isdigit() and await db.delete_backup(int(idx_raw)):
                u.toast("Бэкап удалён", kind="ok")
            else:
                u.print("  Неверный индекс", style="red")


async def screen_license(u: ui.UI, config: dict) -> None:
    from core.license import LicenseManager

    lm = LicenseManager(config)
    status = "ВКЛЮЧЕНА" if lm.enabled else "выключена"
    u.table(
        "ЛИЦЕНЗИЯ",
        ["Параметр", "Значение"],
        [
            ["Статус гейта", status],
            ["License-hub", lm.url or "(не задан)"],
            ["Офлайн-окно", f"{lm.grace_days} дн."],
            ["Кэш", lm.cache_file],
        ],
    )
    if lm.enabled:
        env_pw = os.environ.get("LICENSE_PASSWORD")
        ok, reason = await lm.require_activation(env_pw)
        u.print(
            f"  Проверка пароля: {'OK' if ok else reason}",
            style="green" if ok else "yellow",
        )
        if not ok:
            import getpass

            try:
                pw = getpass.getpass(f"  {Fore.CYAN}Новый пароль активации: {Style.RESET_ALL}").strip()
            except (EOFError, OSError):
                pw = ""
            if pw:
                ok2, r2 = await lm.require_activation(pw)
                u.print(
                    f"  {'Пароль принят' if ok2 else r2}",
                    style="green" if ok2 else "red",
                )
    u.print("\n  Как владелец меняет пароль на GitHub:")
    u.print("    1) python -m core.license setpass НОВЫЙ_ПАРОЛЬ")
    u.print('    2) обновить поле "pass" в license.json и запушить в репозиторий')
    u.print("    3) у клиентов применится новый пароль после следующего онлайн-запроса")


async def screen_help(u: ui.UI) -> None:
    items = [
        ("1", "Создать N кошельков (шифруются master-ключом)"),
        ("2", "Фарминг: один цикл по всем кошелькам"),
        ("3", "Фарминг: live-консоль (таблица в реальном времени)"),
        ("4", "Монитор: живые балансы всех кошельков"),
        ("5", "Кран: пополнить все кошельки"),
        ("6", "Статистика и топ-активность"),
        ("7", "Экспорт в CSV/JSON (файлы содержат ключи!)"),
        ("8", "Импорт из wallets.json"),
        ("9", "Диагностика RPC (зоны/метрики/circuit breaker)"),
        ("B", "Бэкапы: список / создать / восстановить / удалить"),
        ("L", "Лицензия: статус и смена пароля"),
        ("S", "Сменить тестнет (выбор сети заново)"),
        ("G", "Веб-интерфейс (GUI в браузере)"),
        ("H", "Эта справка"),
        ("0", "Выход из сети (назад к выбору тестнета)"),
    ]
    if u.use_rich:
        rows = "\n".join(f"  [bold green][{k}][/bold green]  {label}" for k, label in items)
        body = (
            rows + "\n\n"
            "Старт exe: выбор тестнета (Robinhood / Flop / Arc) -> [1] кошельки -> [3] фарм\n"
            "Каждый кошелёк фармит со своим профилем поведения (анти-сибил, core/behavior.py)\n"
            "Авто-режим: python auto.py (генерит -> кран -> фарм -> экспорт)\n"
            "Подсказки: стрелки не нужны — просто номер + Enter."
        )
    else:
        rows = "\n".join(f"  ({k})  {label}" for k, label in items)
        body = (
            rows + "\n\n"
            "Старт exe: выбор тестнета (Robinhood / Flop / Arc) -> [1] кошельки -> [3] фарм\n"
            "Каждый кошелёк фармит со своим профилем поведения (анти-сибил, core/behavior.py)\n"
            "Авто-режим: python auto.py (генерит -> кран -> фарм -> экспорт)\n"
            "Подсказки: стрелки не нужны — просто номер + Enter."
        )
    u.panel("СПРАВКА", body)


async def screen_gui(u: ui.UI, config: dict) -> None:
    """Запуск веб-интерфейса."""
    u.panel("ВЕБ-ИНТЕРФЕЙС", "Запуск GUI...")
    u.print()

    try:
        from core.web_gui import start_gui

        u.print("  Откройте браузер и перейдите по адресу:", style="cyan")
        u.print("  http://127.0.0.1:8080", style="bold green")
        u.print()
        u.print("  Нажмите Ctrl+C для остановки", style="yellow")
        u.print()

        # Запускаем GUI в отдельном потоке
        import threading

        def run_gui():
            try:
                start_gui()
            except KeyboardInterrupt:
                pass
            except Exception as e:
                logger.error(f"GUI ошибка: {e}")

        gui_thread = threading.Thread(target=run_gui, daemon=True)
        gui_thread.start()

        # Ждем ввода пользователя
        try:
            input("  Нажмите Enter для возврата в меню...")
        except (KeyboardInterrupt, EOFError):
            pass

    except ImportError as e:
        u.print("  Ошибка: не удалось загрузить веб-интерфейс", style="red")
        u.print(f"  {e}", style="red")
        u.print()
        u.print("  Установите зависимости: pip install fastapi uvicorn", style="yellow")
        input("  Нажмите Enter для возврата...")
    except Exception as e:
        u.print(f"  Ошибка запуска GUI: {e}", style="red")
        input("  Нажмите Enter для возврата...")


async def request_faucet_all(config: dict, db: Database) -> None:
    if not config.get("faucet", {}).get("enabled", True):
        print(f"{Fore.YELLOW}Кран отключён в конфиге.{Style.RESET_ALL}")
        return
    addrs = await db.get_all_addresses()
    if not addrs:
        print(f"{Fore.RED}Нет кошельков.{Style.RESET_ALL}")
        return
    faucet = Faucet(config)
    live = await faucet.validate()
    if live <= 0:
        await faucet.close()
        print(f"{Fore.YELLOW}Кран: все стратегии недоступны, пропуск.{Style.RESET_ALL}")
        return
    workers = effective_max_workers(config)
    net = NetworkManager(config, db)
    total = len(addrs)
    try:
        s, f = await faucet.request_batch(
            addrs,
            workers,
            network=net,
            progress=lambda cur, _tot: print(f"\r  Кран: {cur}/{total}", end="", flush=True),
            max_concurrent=faucet.concurrent_batch(workers),
        )
        print()
        print(f"\n{Fore.CYAN}Итого: OK={s}, FAIL={f}{Style.RESET_ALL}")
    finally:
        await net.close()
        await faucet.close()


async def import_wallets(config: dict, db: Database) -> None:
    path = config["wallets"].get("file", "wallets.json")
    if not Path(path).exists():
        print(f"{Fore.RED}Файл {path} не найден.{Style.RESET_ALL}")
        return
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"{Fore.RED}Ошибка чтения {path}: {e}{Style.RESET_ALL}")
        return
    if not isinstance(data, list):
        print(f"{Fore.RED}Неверный формат файла (ожидается список).{Style.RESET_ALL}")
        return
    imported = 0
    skipped = 0
    seen_addrs = set()
    seen_keys = set()
    # Пасс 1 (быстрые проверки): формат, типы, дубликаты — без криптографии.
    candidates: list[tuple[str, str, str, str]] = []  # addr, addr_lower, pk_clean, mnemonic
    for item in data:
        if not isinstance(item, dict):
            skipped += 1
            continue
        addr = item.get("address")
        pk = item.get("private_key")
        if not isinstance(addr, str) or not isinstance(pk, str) or not addr or not pk:
            skipped += 1
            continue
        addr_lower = addr.lower()
        pk_clean = pk.lower().removeprefix("0x")
        if len(pk_clean) != 64 or not all(c in "0123456789abcdef" for c in pk_clean):
            print(f"  {Fore.YELLOW}Невалидный ключ для {addr[:10]}, пропуск{Style.RESET_ALL}")
            skipped += 1
            continue
        if addr_lower in seen_addrs or pk_clean in seen_keys:
            skipped += 1
            continue
        seen_addrs.add(addr_lower)
        seen_keys.add(pk_clean)
        candidates.append((addr, addr_lower, pk_clean, item.get("mnemonic", "")))
    if candidates:
        # Пасс 2 (CPU-bound: вывод адреса из ключа) — распараллеливаем по ядрам.
        def _derive(pair: tuple[str, str, str, str]) -> tuple[str, str, str]:
            addr, addr_lower, pk_clean, _m = pair
            return addr, addr_lower, Account.from_key("0x" + pk_clean).address.lower()

        loop = asyncio.get_running_loop()
        with ThreadPoolExecutor(max_workers=effective_gen_workers(config), thread_name_prefix="import") as pool:
            derived = await asyncio.gather(
                *[loop.run_in_executor(pool, _derive, c) for c in candidates],
                return_exceptions=True,
            )
        rows = []
        for c, res in zip(candidates, derived, strict=True):
            addr, addr_lower, pk_clean, mnemonic = c
            if isinstance(res, BaseException):
                print(f"  {Fore.YELLOW}Невалидный ключ для {addr[:10]}, пропуск{Style.RESET_ALL}")
                skipped += 1
                continue
            if res[2] != addr_lower:
                print(f"  {Fore.YELLOW}Адрес {addr[:10]} не соответствует ключу, пропуск{Style.RESET_ALL}")
                skipped += 1
                continue
            # Ключ храним в нормализованном виде (bare hex, как у generate_wallet)
            rows.append((addr, pk_clean, mnemonic))
        if rows:
            await db.save_wallets_batch(rows)
            imported = len(rows)
    msg = f"Импортировано {imported} кошельков из {path}"
    if skipped > 0:
        msg += f" (пропущено {skipped}: дубликаты/невалидные)"
    print(f"{Fore.GREEN}{msg}.{Style.RESET_ALL}")


# ------------------------------------------------------------------
# ГЛАВНЫЙ ЦИКЛ
# ------------------------------------------------------------------


def _find_configs() -> list[tuple[str, str]]:
    """Ищет конфиги тестнетов рядом с exe и в рабочем каталоге: (путь, подпись)."""
    dirs: list[Path] = [Path.cwd()]
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        if exe_dir not in dirs:
            dirs.append(exe_dir)
    seen: set[str] = set()
    found: list[tuple[str, str]] = []
    for d in dirs:
        for p in sorted(d.glob("config*.yaml")):
            if p.name in seen:
                continue
            seen.add(p.name)
            label = p.name
            try:
                data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
                net_name = (data.get("network") or {}).get("name")
                if net_name:
                    label = f"{net_name}  ({p.name})"
            except Exception:
                pass
            found.append((str(p), label))
    return found


def screen_testnet_select(u: ui.UI, configs: list[tuple[str, str]]) -> str | None:
    """Стартовый экран: выбор тестнета. None — выход из программы."""
    while True:
        u.print()
        u.table(
            "ВЫБОР ТЕСТНЕТА",
            ["#", "Сеть (конфиг)"],
            [[str(i), label] for i, (_, label) in enumerate(configs, 1)],
        )
        u.hint("  0 — выход из программы")
        try:
            raw = input(f"  {Fore.CYAN}Тестнет [1]: {Style.RESET_ALL}").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return None
        if raw in ("0", "q", "exit", "выход"):
            return None
        if raw == "":
            raw = "1"
        if raw.isdigit() and 1 <= int(raw) <= len(configs):
            return configs[int(raw) - 1][0]
        u.print("  Неверный выбор.", style="red")


async def main_async() -> None:
    u = ui.UI()
    u.banner(BANNER_TITLE, BANNER_SUB)
    configs = _find_configs()
    if not configs:
        u.panel(
            "КОНФИГ НЕ НАЙДЕН",
            "  Положите config.yaml (или config_robinhood.yaml / config_flop.yaml /\n"
            "  config_arc.yaml) рядом с harvest.exe и запустите снова.\n"
            "  Шаблон: config.example.yaml — в папке _internal рядом с exe.",
            color="red",
        )
        return
    if len(configs) == 1:
        await run_session(u, configs[0][0])
        u.panel("ДО ВСТРЕЧИ", "  Спасибо за использование HARVEST!", color="cyan")
        return
    while True:
        path = screen_testnet_select(u, configs)
        if path is None:
            u.panel("ДО ВСТРЕЧИ", "  Спасибо за использование HARVEST!", color="cyan")
            return
        await run_session(u, path)


async def run_session(u: ui.UI, config_path: str) -> None:
    """Одна сессия на выбранном тестнете: лицензия, БД, меню."""
    config = load_config(config_path)
    setup_logging(config)
    net_name = (config.get("network") or {}).get("name") or Path(config_path).name
    u.banner(BANNER_TITLE, f"{BANNER_SUB}\n  Сеть: {net_name}  ·  {Path(config_path).name}")

    # --- ЛИЦЕНЗИЯ: доступ только по паролю ---
    try:
        lm = await enforce_license_async(config, interactive=True)
        if lm.enabled:
            u.toast("Доступ подтверждён", kind="ok")
    except LicenseError as e:
        u.panel(
            "ДОСТУП ЗАКРЫТ",
            f"  {e}\n\n  Для получения пароля обратитесь к владельцу софта.\n"
            f"  Требование пароля настраивается в license.json (GitHub).",
            color="red",
        )
        raise SystemExit(2) from None

    # --- БАЗА ДАННЫХ ---
    try:
        db = Database(config["database"]["path"], master_key=resolve_master_key(config))
    except MasterKeyError as e:
        u.print(f"  {e}", style="red")
        raise SystemExit(1) from None
    await db.init()
    stats = await db.get_stats()
    if stats["total_wallets"] == 0:
        u.panel(
            "БЫСТРЫЙ СТАРТ",
            "  1) Создать кошельки ........... [1]\n"
            "  2) Пополнить через кран ........ [5]\n"
            "  3) Запустить live-фарм ......... [3]\n"
            "\n  Каждый кошелёк получает свой профиль поведения (анти-сибил).\n"
            "  Сменить тестнет: [0] → вернуться к выбору сети.",
            color="green",
        )
    u.divider()

    stop_event = asyncio.Event()

    def signal_handler(sig, frame) -> None:
        u.print("\nПолучен сигнал остановки...", style="yellow")
        stop_event.set()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        while not stop_event.is_set():
            choice = menu(u)

            if choice == "0":
                u.panel(
                    "СЕССИЯ ЗАВЕРШЕНА",
                    f"  Сеть: {net_name}\n  Возврат к выбору тестнета...",
                    color="cyan",
                )
                break
            elif choice == "1":
                await screen_make_wallets(u, config, db)
            elif choice == "2":
                await screen_farm_once(u, config, db)
            elif choice == "3":
                await screen_farm_live(u, config, db, stop_event)
            elif choice == "4":
                await screen_monitor(u, config, db, stop_event)
            elif choice == "5":
                await request_faucet_all(config, db)
            elif choice == "6":
                await screen_stats(u, db)
            elif choice == "7":
                wallets = await db.get_all_wallets()
                if not wallets:
                    u.print("  Нет кошельков.", style="red")
                else:
                    base = config["wallets"].get("file", "wallets.json")
                    # Не перезаписывать существующий файл (его могли подготовить для импорта):
                    # если wallets.json уже есть — пишем во временный файл с timestamp.
                    existing = Path(base)
                    path = base
                    if existing.exists():
                        ts = time.strftime("%Y%m%d_%H%M%S")
                        path = str(existing.with_name(f"{existing.stem}_{ts}{existing.suffix}"))
                    path = WalletManager.export_to_file(wallets, path)
                    u.toast(f"Экспортировано {len(wallets)} кошельков в {path}", kind="ok")
                    u.print(
                        "  ВНИМАНИЕ: файл содержит приватные ключи! Храните безопасно.",
                        style="yellow",
                    )
            elif choice == "8":
                await import_wallets(config, db)
            elif choice == "c":
                await screen_history(u, db)
            elif choice == "g":
                await screen_gui(u, config)
            elif choice == "9":
                await screen_rpc_diag(u, config, db)
            elif choice == "d":
                await screen_doctor(u, config)
            elif choice == "b":
                await screen_backups(u, db)
            elif choice == "l":
                await screen_license(u, config)
            elif choice == "s":
                u.print("  Выход к выбору тестнета...", style="yellow")
                break
            elif choice == "h":
                await screen_help(u)
            else:
                u.print("  Неверный выбор.", style="red")

            if not stop_event.is_set():
                try:
                    input(f"\n{Fore.CYAN}Enter для продолжения...{Style.RESET_ALL}")
                except EOFError:
                    break

    finally:
        await db.close()


if __name__ == "__main__":
    # Если есть аргументы командной строки — auto-режим (argparse)
    # Иначе — интерактивное меню
    if len(sys.argv) > 1:
        # auto-режим: прокидываем аргументы в auto.py
        from auto import main_auto

        try:
            asyncio.run(main_auto())
        except KeyboardInterrupt:
            print(f"\n{Fore.YELLOW}Стоп.{Style.RESET_ALL}")
    else:
        try:
            asyncio.run(main_async())
        except KeyboardInterrupt:
            print(f"\n{Fore.YELLOW}Завершение...{Style.RESET_ALL}")
        except Exception as e:
            print(f"\n{Fore.RED}Критическая ошибка: {e}{Style.RESET_ALL}")
            raise
