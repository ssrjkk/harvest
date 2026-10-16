#!/usr/bin/env python3
"""harvest — thin entry point. All CLI logic lives in cli/ package."""

import asyncio
import sys

from colorama import Fore, Style, init

init(autoreset=True)

if __name__ == "__main__":
    if len(sys.argv) > 1:
        from auto import main_auto

        try:
            asyncio.run(main_auto())
        except KeyboardInterrupt:
            print(f"\n{Fore.YELLOW}Стоп.{Style.RESET_ALL}")
    else:
        from cli.session import main_async

        try:
            asyncio.run(main_async())
        except KeyboardInterrupt:
            print(f"\n{Fore.YELLOW}Завершение...{Style.RESET_ALL}")
        except Exception as e:
            print(f"\n{Fore.RED}Критическая ошибка: {e}{Style.RESET_ALL}")
            raise
