"""Рендер главного меню и обработка ввода."""

from colorama import Fore, Style

from core import ui


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
