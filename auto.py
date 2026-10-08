#!/usr/bin/env python3
"""Auto-режим: одна кнопка — генерация -> кран -> фарм -> экспорт.

Использование:
    python auto.py                              # 50 кошельков, бесконечно
    python auto.py --wallets 200 --cycles 5     # 200 кошельков, 5 циклов
    python auto.py --skip-faucet                # без крана (уже есть баланс)
    python auto.py --skip-farm                  # только генерация + кран + экспорт
    python auto.py --doctor                     # самодиагностика окружения
    python auto.py --schedule 6                 # фарм каждые 6 часов (по расписанию)
"""

import argparse
import asyncio
import logging
import os
import random
import signal
import sys
import time

from colorama import Fore, Style, init

from core.config import load_config
from core.crypto import MasterKeyError, resolve_master_key
from core.database import Database, _redact_rpc_url
from core.doctor import doctor
from core.exporter import decrypt_file, encrypt_file, export_csv, export_json
from core.faucet import Faucet
from core.license import LicenseError, enforce_license_async
from core.logger import setup_logging
from core.network import NetworkManager
from core.performance import summarize
from core.pool import FarmerPool
from core.single_instance import SingleInstance, default_lock_path
from core.version import version_line
from core.wallet import WalletManager

init(autoreset=True)

logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Robinhood Chain Farmer - Auto Mode")
    p.add_argument("--wallets", type=int, default=0, help="Количество кошельков (0=из конфига)")
    p.add_argument("--cycles", type=int, default=0, help="Циклов фарма (0=бесконечно)")
    p.add_argument(
        "--config",
        default=os.environ.get("FARMER_CONFIG", "config.yaml"),
        help="Конфиг (config_robinhood.yaml / config_flop.yaml / config_arc.yaml / config_vibevibe.yaml)",
    )
    p.add_argument("--skip-faucet", action="store_true", help="Пропустить кран (уже есть баланс)")
    p.add_argument("--skip-farm", action="store_true", help="Пропустить фарм")
    p.add_argument("--export", default="wallets_export", help="Имя файла экспорта")
    p.add_argument("--batch", type=int, default=50, help="Кошельков за раз в кране")
    p.add_argument(
        "--workers",
        type=int,
        default=0,
        help="Потоков фарма (threading.max_workers), 0=из конфига",
    )
    p.add_argument(
        "--rpc-threads",
        type=int,
        default=0,
        help="Потоков RPC (threading.rpc_threads), 0=авто",
    )
    p.add_argument(
        "--fast",
        action="store_true",
        help="Быстрый режим: минимальные задержки (для проверки)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Тестовый режим: симуляция без отправки транзакций",
    )
    p.add_argument("--encrypt", action="store_true", help="Зашифровать export-файлы (AES-256)")
    p.add_argument(
        "--decrypt",
        default=None,
        metavar="FILE",
        help="Расшифровать файл (.enc), пароль спросит (нет pipeline)",
    )
    p.add_argument("--version", action="store_true", help="Показать версию")
    p.add_argument(
        "--history", type=int, nargs="?", const=20, metavar="N", help="Показать последние N циклов фарма и выйти"
    )
    p.add_argument("--doctor", action="store_true", help="Самодиагностика окружения и выход")
    p.add_argument(
        "--schedule",
        type=float,
        default=0.0,
        metavar="HOURS",
        help="Запускать пайплайн по расписанию каждые N часов",
    )
    return p.parse_args()


def banner() -> None:
    print(f"""
{Fore.CYAN}{"=" * 60}
   HARVEST  -  AUTO MODE
   {version_line()}
{"=" * 60}{Style.RESET_ALL}""")


def progress(current: int, total: int, prefix: str = "") -> None:
    bar_len = 30
    filled = int(bar_len * current / total) if total > 0 else 0
    bar = "#" * filled + "-" * (bar_len - filled)
    pct = (current / total * 100) if total > 0 else 0
    line = f"  {prefix} [{bar}] {pct:5.1f}% {current}/{total}"
    print(f"\r{line}", end="", flush=True)
    if current >= total:
        print()


async def run_once(args: argparse.Namespace, stop_event: asyncio.Event | None = None) -> None:
    """Один проход пайплайна: генерация -> кран -> фарм -> экспорт."""
    config = load_config(args.config)
    if config is None:
        raise SystemExit(1)

    wallet_count = args.wallets if args.wallets > 0 else config["wallets"].get("count", 50)
    cycle_count = args.cycles

    # --skip-faucet: отключаем кран + force_farm чтобы фермер не пытался достать токены
    if args.skip_faucet:
        config.setdefault("advanced", {})["force_farm"] = True
        config.setdefault("faucet", {})["enabled"] = False

    # --fast: минимальные задержки для быстрой проверки пайплайна
    if args.fast:
        config["farming"]["delay_between_actions"] = [0.5, 1.5]
        config["farming"]["actions_per_cycle"] = [1, 2]
        config["farming"]["delay_between_cycles"] = [2, 5]
        config["threading"]["max_workers"] = 30

    # --dry-run: симуляция без отправки транзакций
    if args.dry_run:
        config.setdefault("advanced", {})["dry_run"] = True

    # Явный переразгон параллелизма (пиковая загрузка ядер) без правки конфига
    if args.workers > 0:
        config["threading"]["max_workers"] = args.workers
    if args.rpc_threads > 0:
        config["threading"]["rpc_threads"] = args.rpc_threads

    setup_logging(config)
    banner()

    # --- ЛИЦЕНЗИЯ: доступ только по паролю ---
    try:
        await enforce_license_async(config, interactive=(stop_event is None))
    except LicenseError as e:
        print(f"  {Fore.RED}{e}{Style.RESET_ALL}")
        print(f"  {Fore.YELLOW}Для получения пароля обратитесь к владельцу софта.{Style.RESET_ALL}")
        if stop_event is not None:
            print(f"  {Fore.YELLOW}Цикл пропущен; при следующем запуске проверка повторится{Style.RESET_ALL}")
            return
        raise SystemExit(2) from None

    try:
        db = Database(config["database"]["path"], master_key=resolve_master_key(config))
    except MasterKeyError as e:
        print(f"  {Fore.RED}{e}{Style.RESET_ALL}")
        raise SystemExit(1) from None
    await db.init()

    if stop_event is None:
        stop_event = asyncio.Event()
        signal.signal(signal.SIGINT, lambda s, f: stop_event.set())
        signal.signal(signal.SIGTERM, lambda s, f: stop_event.set())

    t0 = time.monotonic()
    perf = summarize(config)
    print(f"  Кошельков: {wallet_count}")
    print(f"  Циклов:    {'бесконечно' if cycle_count <= 0 else cycle_count}")
    print(f"  RPC:       {_redact_rpc_url(config['network']['rpc_url']) or ''}")
    print(
        f"  Ядра CPU:  {perf['cpu_cores']} | workers {perf['max_workers']} | "
        f"RPC-потоков {perf['rpc_threads']} | gen {perf['gen_workers']} | chunk {perf['chunk_size']}"
    )
    print(f"  Force:     {config.get('advanced', {}).get('force_farm', False)}")
    beh = config.get("behavior") or {}
    print(f"  Профили:   {'включены (анти-сибил)' if beh.get('enabled', True) else 'нейтральные'}")
    if config.get("advanced", {}).get("dry_run"):
        print(f"  {Fore.YELLOW}DRY-RUN: транзакции НЕ отправляются{Style.RESET_ALL}")

    # Один пул на одну БД: защита от случайного двойного фарма.
    guard = SingleInstance(default_lock_path(config["database"]["path"]))
    if not guard.acquire():
        print(f"  {Fore.YELLOW}Уже работает другой экземпляр фармера (pid={guard.holder_pid()}).{Style.RESET_ALL}")
        print("  Два пула на одной БД несовместимы — пропуск цикла.")
        await db.close()
        return

    network = None
    try:
        # === ФАЗА 1: ГЕНЕРАЦИЯ / ПОВТОРНОЕ ИСПОЛЬЗОВАНИЕ ===
        print(f"\n{Fore.CYAN}[1/4] Подготовка {wallet_count} кошельков...{Style.RESET_ALL}")
        t1 = time.monotonic()
        wm = WalletManager(config, db)
        wallet_rows: list[dict] = []
        if config["wallets"].get("generate_if_missing", True):
            existing = await db.get_all_wallets()
            raw_total = await db.count_wallets()
            if raw_total and not existing:
                # Строки в БД есть, но НИ ОДНА не расшифровалась текущим ключом:
                # это почти наверняка подменённый/неверный master-ключ, а не
                # «пустая база». Молчаливая генерация флота ВЫШЕ нечитаемых
                # шифрованных строк превратила бы потерю ключа в фальшивую
                # «OK: 50 кошельков» с новыми пустыми адресами. Ошибка ключа
                # обратима (верни старый ключ) — генерация новых — нет.
                print(
                    f"{Fore.RED}  ОШИБКА: в БД {raw_total} кошельков, но НИ ОДИН не расшифрован "
                    f"текущим master-ключом.{Style.RESET_ALL}"
                )
                print("  Это подмена/потеря database.master_key (master.key), а НЕ повод генерировать новые кошельки.")
                print("  Верни настоящий ключ БД (или master.key). Генерация отключена автоматически.")
                try:
                    guard.release()
                except Exception:
                    pass
                await db.close()
                return
            if existing:
                if len(existing) >= wallet_count:
                    wallet_rows = existing[:wallet_count]
                    print(f"  {Fore.GREEN}Использую {len(wallet_rows)} существующих кошельков из БД{Style.RESET_ALL}")
                else:
                    missing = wallet_count - len(existing)
                    new = await wm.create_wallets(missing)
                    wallet_rows = existing + new
                    print(f"  {Fore.GREEN}В БД {len(existing)}, создано ещё {len(new)}{Style.RESET_ALL}")
            else:
                wallet_rows = await wm.create_wallets(wallet_count)
                print(
                    f"  {Fore.GREEN}OK: {len(wallet_rows)} кошельков за "
                    f"{time.monotonic() - t1:.1f} сек{Style.RESET_ALL}"
                )
        else:
            # generate_if_missing=false: НЕ создаём новые кошельки — только уже сохранённые.
            wallet_rows = await db.get_all_wallets()
            if not wallet_rows:
                print(
                    f"  {Fore.YELLOW}Кошельков в БД нет, а генерация отключена "
                    f"(wallets.generate_if_missing=false) — фармить нечего{Style.RESET_ALL}"
                )
                return
        wallets = wallet_rows

        # === ФАЗА 2: КРАН ===
        if not args.skip_faucet:
            print(f"\n{Fore.CYAN}[2/4] Кран: пополнение {len(wallets)} кошельков...{Style.RESET_ALL}")
            if config.get("faucet", {}).get("enabled", True) and config.get("faucet", {}).get("strategies"):
                t2 = time.monotonic()
                network = NetworkManager(config, db)
                faucet = Faucet(config)
                try:
                    live = await faucet.validate()
                    if live <= 0:
                        print(
                            f"  {Fore.YELLOW}Кран: все стратегии недоступны, пропуск. "
                            f"Фарм пойдёт без пополнения (force_farm).{Style.RESET_ALL}"
                        )
                        config.setdefault("advanced", {})["force_farm"] = True
                    else:
                        addresses = [w["address"] for w in wallets]
                        funded, failed = await faucet.request_batch(
                            addresses,
                            args.batch,
                            network=network,
                            progress=lambda done, total: progress(done, total, prefix="  Кран"),
                            pause=3.0,
                            max_concurrent=faucet.concurrent_batch(args.batch),
                        )
                        print(
                            f"  {Fore.GREEN}OK={funded}, FAIL={failed}, "
                            f"{time.monotonic() - t2:.1f} сек{Style.RESET_ALL}"
                        )
                finally:
                    await faucet.close()
            else:
                print(f"  {Fore.YELLOW}Кран отключён/нет стратегий, пропуск. force_farm=True{Style.RESET_ALL}")
                config.setdefault("advanced", {})["force_farm"] = True
        else:
            print(f"\n{Fore.CYAN}[2/4] Кран: пропущен (--skip-faucet){Style.RESET_ALL}")

        # === ФАЗА 3: ФАРМ ===
        if not args.skip_farm:
            print(
                f"\n{Fore.CYAN}[3/4] Фарминг ({cycle_count if cycle_count > 0 else 'бесконечно'})...{Style.RESET_ALL}"
            )
            t3 = time.monotonic()
            if network is None:
                network = NetworkManager(config, db)
            pool = FarmerPool(config, db, network=network)
            try:
                if cycle_count <= 0:
                    await pool.run_forever(stop_event)
                else:
                    all_addresses = [w["address"] for w in wallets]
                    for cycle in range(1, cycle_count + 1):
                        results = await pool.run_once(
                            wallets,
                            all_addresses,
                            cycle_number=cycle,
                            stop_event=stop_event,
                        )
                        ok = sum(1 for _, v in results if v > 0)
                        acts = sum(v for _, v in results)
                        diag = pool.network.diagnostics()
                        rpc_tail = (
                            f" | RPC: {diag.get('calls', 0)} вызовов, {diag.get('errors', 0)} ошибок, "
                            f"rate={diag.get('rpc_rate', '?')}/с"
                        )
                        print(f"  Цикл {cycle}/{cycle_count}: {ok}/{len(wallets)} кошельков, {acts} действий{rpc_tail}")
                        if stop_event.is_set():
                            print(f"  {Fore.YELLOW}Остановлено по сигналу{Style.RESET_ALL}")
                            break
                        if cycle < cycle_count:
                            wait = min(random.uniform(2, 10), 15)  # авто-режим: ускоренные паузы
                            print(f"  Пауза {wait:.0f} сек...")
                            await asyncio.sleep(wait)
            finally:
                await pool.close()
            print(f"  {Fore.GREEN}Фарм: {time.monotonic() - t3:.1f} сек{Style.RESET_ALL}")
        else:
            print(f"\n{Fore.CYAN}[3/4] Фарм: пропущен (--skip-farm){Style.RESET_ALL}")

        # === ФАЗА 4: ЭКСПОРТ ===
        print(f"\n{Fore.CYAN}[4/4] Экспорт...{Style.RESET_ALL}")
        db_wallets = await db.get_all_wallets()
        export_data = db_wallets if db_wallets else wallets
        csv_path = f"{args.export}.csv"
        json_path = f"{args.export}.json"
        if config.get("advanced", {}).get("dry_run"):
            # Симуляция не имеет права перетирать реальный экспорт: в нём
            # приватные ключи и сид-фразы рабочего флота, копии которых нигде.
            csv_path = f"{args.export}_dryrun.csv"
            json_path = f"{args.export}_dryrun.json"
        export_csv(export_data, csv_path)
        export_json(export_data, json_path)
        print(f"  CSV:  {csv_path}")
        print(f"  JSON: {json_path}")
        print(f"  Записей: {len(export_data)}")
        print(f"  {Fore.YELLOW}Файлы содержат приватные ключи и сид-фразы!{Style.RESET_ALL}")

        # Шифрование export-файлов
        if args.encrypt:
            import getpass

            try:
                pwd = getpass.getpass("  Пароль для шифрования: ")
                pwd2 = getpass.getpass("  Повторите пароль: ") if pwd else ""
            except (EOFError, OSError):
                pwd = ""
                pwd2 = ""
            if pwd:
                if pwd != pwd2:
                    print(f"  {Fore.RED}Пароли не совпадают, шифрование отменено{Style.RESET_ALL}")
                else:
                    enc_csv = encrypt_file(csv_path, pwd)
                    enc_json = encrypt_file(json_path, pwd)
                    if enc_csv == csv_path or enc_json == json_path:
                        print(
                            f"  {Fore.RED}Шифрование не выполнено (нет cryptography?) — "
                            f"файлы остались ОТКРЫТЫМ текстом!{Style.RESET_ALL}"
                        )
                    else:
                        csv_path, json_path = enc_csv, enc_json
                        print(f"  {Fore.GREEN}Файлы зашифрованы (AES-256){Style.RESET_ALL}")
            else:
                print(f"  {Fore.YELLOW}Пустой пароль, шифрование отменено{Style.RESET_ALL}")

        # === ИТОГ ===
        stats = await db.get_stats()
        elapsed = time.monotonic() - t0
        print(f"\n{'=' * 60}")
        print(f"  {Fore.CYAN}ИТОГО{Style.RESET_ALL}")
        print(f"  Кошельков:  {stats['total_wallets']}")
        print(f"  Действий:   {stats['total_actions']}")
        print(f"  Успешных:   {stats['success_actions']}")
        print(f"  Экспорт:    {csv_path}, {json_path}")
        print(f"  Время:      {elapsed:.0f} сек ({elapsed / 60:.1f} мин)")
        print(f"{'=' * 60}")

    finally:
        # Закрытие не должно съедать single-instance lock: любой сбой cleanup
        # отдельно логируется, но release выполняется всегда.
        try:
            if network is not None:
                await network.close()
        except Exception as e:
            logger.warning(f"Ошибка закрытия NetworkManager: {e}")
        try:
            await db.close()
        except Exception as e:
            logger.warning(f"Ошибка закрытия БД: {e}")
        try:
            guard.release()
        except Exception as e:
            logger.warning(f"Ошибка снятия single-instance lock: {e}")


async def run_schedule(args: argparse.Namespace, hours: float) -> None:
    """Фарм по расписанию: пайплайн каждые N часов с обратным отсчётом."""
    interval = hours * 3600
    stop = asyncio.Event()
    signal.signal(signal.SIGINT, lambda s, f: stop.set())
    signal.signal(signal.SIGTERM, lambda s, f: stop.set())
    tty = bool(getattr(sys.stdout, "isatty", lambda: False)())
    print(f"{Fore.CYAN}Расписание: запуск каждые {hours:g} ч.{Style.RESET_ALL}")
    n = 0
    while not stop.is_set():
        n += 1
        print(f"\n{Fore.CYAN}=== ПЛАНОВЫЙ ЦИКЛ #{n} ==={Style.RESET_ALL}")
        try:
            await run_once(args, stop)
        except SystemExit:
            raise
        except Exception as e:
            print(f"  {Fore.RED}Ошибка цикла: {e}{Style.RESET_ALL}")
        if stop.is_set():
            break
        left = interval
        print(f"  Следующий запуск через {hours:g} ч. (Ctrl+C — выход)")
        while left > 0 and not stop.is_set():
            step = min(30.0, left)
            await asyncio.sleep(step)
            left -= step
            if tty:
                hh, rem = divmod(int(left), 3600)
                mm, ss = divmod(rem, 60)
                sys.stdout.write(f"\r  До следующего запуска: {hh:02d}:{mm:02d}:{ss:02d} ")
                sys.stdout.flush()
    if tty:
        sys.stdout.write("\r\x1b[K")
        sys.stdout.flush()
    print(f"  {Fore.YELLOW}Расписание остановлено.{Style.RESET_ALL}")


async def show_cycle_history(db: Database, limit: int) -> None:
    """Печатает журнал завершённых циклов фарма (CLI-режим)."""
    rows = await db.get_cycle_history(limit)
    st = await db.get_cycle_stats()
    print(f"\n{Fore.CYAN}ЖУРНАЛ ЦИКЛОВ ФАРМА{Style.RESET_ALL}")
    if not rows:
        print(f"  {Fore.YELLOW}Циклов ещё нет — запустите фарм (python auto.py).{Style.RESET_ALL}")
        return
    hours = st["duration_s"] / 3600
    print(
        f"  Всего циклов: {st['cycles']} | кошельков обработано: {st['wallets']} | "
        f"действий: {st['actions']} | время: {hours:.1f} ч"
    )
    print()
    header = f"  {'#':>3}  {'Старт':<19} {'Длит(с)':>7} {'Кошельк':>7} {'ОК':>4} {'Действ':>6} {'Ошибки':>6}  RPC"
    print(f"{Fore.CYAN}{header}{Style.RESET_ALL}")
    for i, r in enumerate(rows, 1):
        started = str(r.get("started_at") or "").replace("T", " ")[:19]
        rpc = (_redact_rpc_url(r.get("rpc_url") or "") or "")[:48]
        print(
            f"  {i:>3}  {started:<19} {r['duration_s']:7.0f} {r['wallets']:>7} "
            f"{r['wallets_ok']:>4} {r['actions_ok']:>6} {r['errors']:>6}  {rpc}"
        )


async def main_auto() -> None:
    args = parse_args()

    if args.encrypt and not getattr(sys.stdin, "isatty", lambda: False)():
        print(
            f"  {Fore.RED}--encrypt требует интерактивного ввода пароля, а stdin — "
            f"не терминал (pipeline/расписание). Раньше при этом шифрование молча "
            f"пропускалось, а export-файлы с ключами оставались ОТКРЫТЫМ текстом.{Style.RESET_ALL}"
        )
        raise SystemExit(2)

    if args.version:
        print(version_line())
        return

    if args.decrypt:
        import getpass

        pwd = getpass.getpass("  Пароль: ")
        if not pwd:
            print(f"  {Fore.YELLOW}Пустой пароль, отмена{Style.RESET_ALL}")
            return
        out = decrypt_file(args.decrypt, pwd)
        if out != args.decrypt:
            print(f"  {Fore.GREEN}Расшифровано: {out}{Style.RESET_ALL}")
        return

    if args.doctor:
        config = load_config(args.config)
        if config is None:
            raise SystemExit(1)
        print(f"{Fore.CYAN}Самодиагностика окружения...{Style.RESET_ALL}")
        ok = await doctor(None, config)
        print()
        raise SystemExit(0 if ok else 1)

    if args.history:
        config = load_config(args.config)
        if config is None:
            raise SystemExit(1)
        setup_logging(config)
        try:
            db = Database(config["database"]["path"], master_key=resolve_master_key(config))
        except MasterKeyError as e:
            print(f"  {Fore.RED}{e}{Style.RESET_ALL}")
            raise SystemExit(1) from None
        try:
            await db.init()
            await show_cycle_history(db, args.history)
        finally:
            await db.close()
        return

    if args.schedule and args.schedule > 0:
        if args.schedule < 0.05:
            print(
                f"  {Fore.RED}--schedule: минимум 0.05 ч (~3 мин) — пайплайн тяжёлый "
                f"для более частого запуска{Style.RESET_ALL}"
            )
            raise SystemExit(2)
        await run_schedule(args, float(args.schedule))
        return

    await run_once(args)


if __name__ == "__main__":
    try:
        asyncio.run(main_auto())
    except KeyboardInterrupt:
        print(f"\n{Fore.YELLOW}Стоп.{Style.RESET_ALL}")
