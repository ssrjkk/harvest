"""Точка входа для Waifly/Pterodactyl (Python egg): запускает портал.

Waifly egg запускает python <entry>. Здесь читаем порт из окружения Waifly
(PORT / SERVER_PORT), поднимаем портал (веб + бот) на 0.0.0.0.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ.setdefault("PORTAL_HOST", "0.0.0.0")
os.environ.setdefault("PORTAL_PORT", os.environ.get("PORT", os.environ.get("SERVER_PORT", "8080")))
os.environ.setdefault("PORTAL_FARM_CONFIG", "config_vibevibe.yaml")
os.environ.setdefault("PORTAL_DB", "farming_state.db")

from portal.__main__ import main  # noqa: E402

if __name__ == "__main__":
    asyncio.run(main())
