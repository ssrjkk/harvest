"""Глобальная версия приложения."""

VERSION = "2.4.1"
"""Возвращайте version в --version и в баннер."""


def version_line() -> str:
    return f"harvest v{VERSION}"
