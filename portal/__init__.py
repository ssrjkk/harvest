"""HARVEST PORTAL — Telegram-бот + Mini App + веб-сайт поверх ядра фармера.

Единый доступ: Google-логин или мастер-ключ (FARMER_MASTER_KEY).
"""

import mimetypes

# Windows-реестр не знает .woff2 — aiohttp отдаёт как application/octet-stream.
# Регистрируем корректный MIME для локальных шрифтов (Sora / JetBrains Mono).
mimetypes.add_type("font/woff2", ".woff2")
mimetypes.add_type("font/woff", ".woff")

__version__ = "0.1.0"
