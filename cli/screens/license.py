"""Экран лицензии."""

import getpass
import os

from colorama import Fore, Style

from core import ui


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
