"""harvest PORTAL — Telegram-бот + Mini App + веб-сайт поверх ядра фармера.

Единый доступ: Google-логин или мастер-ключ (FARMER_MASTER_KEY).
"""

import mimetypes
from pathlib import Path

# Windows-реестр не знает .woff2 — aiohttp отдаёт как application/octet-stream.
# Регистрируем корректный MIME для локальных шрифтов (Sora / JetBrains Mono).
mimetypes.add_type("font/woff2", ".woff2")
mimetypes.add_type("font/woff", ".woff")

try:
    from core.version import VERSION as __version__
except Exception:  # noqa: BLE001 — core недоступен в урезанном окружении
    __version__ = "2.4.1"


def substitute_version(text: str) -> str:
    """Подставляет версию в текст HTML: `{{HARVEST_VERSION}}` -> номер из core."""
    return text.replace("{{HARVEST_VERSION}}", __version__)


def render_page(path: str | Path) -> str:
    """Текст HTML-страницы портала с подставленной версией (для verify/tools)."""
    return substitute_version(Path(path).read_text(encoding="utf-8"))
