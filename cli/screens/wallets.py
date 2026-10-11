"""Экраны кошельков: генерация, импорт, кран."""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from colorama import Fore, Style

from core import ui
from core.database import Database
from core.faucet import Faucet
from core.network import NetworkManager
from core.performance import effective_gen_workers, effective_max_workers
from core.wallet import WalletManager

from eth_account import Account


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
    candidates: list[tuple[str, str, str, str]] = []
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
            rows.append((addr, pk_clean, mnemonic))
        if rows:
            await db.save_wallets_batch(rows)
            imported = len(rows)
    msg = f"Импортировано {imported} кошельков из {path}"
    if skipped > 0:
        msg += f" (пропущено {skipped}: дубликаты/невалидные)"
    print(f"{Fore.GREEN}{msg}.{Style.RESET_ALL}")
