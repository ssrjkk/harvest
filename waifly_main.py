"""Точка входа для Waifly/Pterodactyl (Python egg): запускает портал.

Waifly egg запускает python <entry>. Здесь читаем порт из окружения Waifly
(PORT / SERVER_PORT), поднимаем портал (веб + бот) на 0.0.0.0.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _load_dotenv() -> None:
    """Подхватить .env рядом со скриптом (панели типа Pterodactyl не умеют
    экспортировать env-файлы сами — окружение там задаётся только стартовыми
    переменными). Без зависимостей: KEY=VALUE, # комментарии, кавычки."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in ("'", '"')
        ):
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv()

os.environ.setdefault("PORTAL_HOST", "0.0.0.0")
os.environ.setdefault("PORTAL_PORT", os.environ.get("PORT", os.environ.get("SERVER_PORT", "8080")))
os.environ.setdefault("PORTAL_FARM_CONFIG", "config_vibevibe.yaml")
os.environ.setdefault("PORTAL_DB", "farming_state.db")

from portal.__main__ import main  # noqa: E402

if __name__ == "__main__":
    asyncio.run(main())
