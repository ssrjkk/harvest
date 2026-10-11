"""Сессия CLI: выбор тестнета, инициализация, главный цикл меню."""

import asyncio
import logging
import os
import signal
import sys
import time
from pathlib import Path

import yaml
from colorama import Fore, Style

from core import ui
from core.config import load_config as _load_config
from core.crypto import MasterKeyError, resolve_master_key
from core.database import Database
from core.license import LicenseError, enforce_license_async
from core.logger import setup_logging
from core.version import VERSION
from core.wallet import WalletManager

from cli.menu import menu
from cli.screens.backups import screen_backups
from cli.screens.diagnostics import screen_doctor, screen_rpc_diag
from cli.screens.farm import screen_farm_live, screen_farm_once
from cli.screens.gui import screen_gui
from cli.screens.help import screen_help
from cli.screens.license import screen_license
from cli.screens.monitor import screen_monitor
from cli.screens.stats import screen_history, screen_stats
from cli.screens.wallets import import_wallets, request_faucet_all, screen_make_wallets

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


def _find_configs() -> list[tuple[str, str]]:
    """Ищет конфиги тестнетов рядом с exe и в рабочем каталоге."""
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


async def run_session(u: ui.UI, config_path: str) -> None:
    """Одна сессия на выбранном тестнете: лицензия, БД, меню."""
    config = load_config(config_path)
    setup_logging(config)
    net_name = (config.get("network") or {}).get("name") or Path(config_path).name
    u.banner(BANNER_TITLE, f"{BANNER_SUB}\n  Сеть: {net_name}  ·  {Path(config_path).name}")

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
