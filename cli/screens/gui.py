"""Экран запуска веб-интерфейса."""

import logging
import threading

from core import ui

logger = logging.getLogger(__name__)


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

        def run_gui():
            try:
                start_gui()
            except KeyboardInterrupt:
                pass
            except Exception as e:
                logger.error(f"GUI ошибка: {e}")

        gui_thread = threading.Thread(target=run_gui, daemon=True)
        gui_thread.start()

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
