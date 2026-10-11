"""Экран бэкапов БД."""

from colorama import Fore, Style

from core import ui
from core.database import Database


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
