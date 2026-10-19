"""Общие настройки тестовой сессии.

PORTAL_SECRET_FILE уводим из рабочего каталога: `PortalConfig()` без
`PORTAL_SECRET` в env пишет секрет в `portal_secret.key` относительно CWD —
то есть прогон тестов засорял корень репозитория служебным файлом.
Тесты, которым нужен конкретный путь, задают `PORTAL_SECRET_FILE` сами.
"""

import os

import pytest


@pytest.fixture(autouse=True, scope="session")
def _portal_secret_file_outside_repo(tmp_path_factory):
    """Держит portal_secret.key во временной папке, а не в корне проекта."""
    path = tmp_path_factory.mktemp("portal-secret") / "portal_secret.key"
    prev = os.environ.get("PORTAL_SECRET_FILE")
    os.environ["PORTAL_SECRET_FILE"] = str(path)
    try:
        yield path
    finally:
        if prev is None:
            os.environ.pop("PORTAL_SECRET_FILE", None)
        else:
            os.environ["PORTAL_SECRET_FILE"] = prev
