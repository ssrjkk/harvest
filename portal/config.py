"""Конфигурация портала: env-переменные > portal_config.yaml.

Секреты (токен бота, Google credentials, мастер-ключ) — строго через env:
    PORTAL_SECRET           — секрет подписи сессий/мини-апп (или portal_secret.key)
    PORTAL_PORT / HOST      — порт и адрес сервера (default bind 127.0.0.1)
    PORTAL_DB               — путь к БД фармера
    PORTAL_FARM_CONFIG      — какой конфиг фармера крутим (config.yaml)
    PORTAL_LINKS            — путь к links.json (ссылки владельца)
    GOOGLE_CLIENT_ID        — OAuth 2.0 Client ID (Google Cloud Console)
    GOOGLE_CLIENT_SECRET    — Client Secret
    GOOGLE_ALLOW_EMAILS     — разрешённые email (default-deny; только эти)
    TELEGRAM_BOT_TOKEN      — токен бота (BotFather)
    TELEGRAM_ALLOW_IDS      — разрешённые Telegram user_id (default-deny)
    FARMER_MASTER_KEY       — мастер-ключ фармера (пароль для веба)
    PORTAL_OPEN_ACCESS=1    — сознательно разрешить любой Google-аккаунт/Telegram
    PORTAL_PASSWORD_LOGIN=1 — разрешить вход по мастер-ключу (default 1)
    PORTAL_ALLOW_PASSWORD_HTTP=1 — разрешить пароль поверх HTTP (ТОЛЬКО dev)
    PORTAL_COOKIE_SECURE    — Secure-кука (авто по https base_url)
    PORTAL_TRUST_PROXY=1    — доверять X-Forwarded-For/Proto (за реальным прокси)
"""

from __future__ import annotations

import os
import re
import secrets
from typing import Any
from urllib.parse import urlencode

import yaml

# Мастер-ключ фармера — строго 64 hex (как в core/database.py:resolve).
# Если в FARMER_MASTER_KEY задан что-то иное — напечатать warning, чтобы
# лёгкие пароли/случайные строки не молча использовались как PIN.
_MASTER_KEY_HEX_RE = re.compile(r"^[0-9a-fA-F]{64}$")


class MasterKeyStatus:
    """Результат проверки мастер-ключа при старте портала."""

    __slots__ = ("ok", "reason")

    def __init__(self, ok: bool, reason: str = "") -> None:
        self.ok = ok
        self.reason = reason


def validate_master_key(master_key: str) -> MasterKeyStatus:
    """Валидация FARMER_MASTER_KEY.

    Ожидаем строго 64 hex (ключ шифрования БД). Допустимая деградация —
    короткий PIN (для веб-входа), но тогда текущий ключ БД всё равно должен
    подходить и мы предупреждаем о слабом PIN.
    """
    if not master_key:
        return MasterKeyStatus(False, "FARMER_MASTER_KEY не задан")
    if _MASTER_KEY_HEX_RE.match(master_key):
        return MasterKeyStatus(True)
    if len(master_key) >= 8:
        return MasterKeyStatus(
            True,
            "FARMER_MASTER_KEY не похож на 64-hex мастер-ключ БД "
            "(используется как PIN веб-входа; слабый PIN легко перебирать)",
        )
    return MasterKeyStatus(False, "FARMER_MASTER_KEY слишком короткий (< 8 символов)")


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes"}


def _resolve_secret() -> str:
    """Секрет сессий. Отдаём приоритет env; иначе берём из файла;
    иначе генерируем и сохраняем (переживает рестарт сервера)."""
    env_secret = os.environ.get("PORTAL_SECRET", "").strip()
    if env_secret:
        return env_secret
    path = os.environ.get("PORTAL_SECRET_FILE", "portal_secret.key")
    if os.path.exists(path):
        try:
            value = open(path, encoding="utf-8").read().strip()
        except OSError:
            value = ""
        if len(value) >= 16:
            return value
    value = secrets.token_urlsafe(32)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(value + "\n")
    except OSError:
        return ""
    return value


class PortalConfig:
    def __init__(self) -> None:
        self.host: str = os.environ.get("PORTAL_HOST", "127.0.0.1")
        self.port: int = int(os.environ.get("PORTAL_PORT", "8080"))
        self.secret: str = _resolve_secret()
        self.db_path: str = os.environ.get("PORTAL_DB", "farming_state.db")
        self.farm_config: str = os.environ.get("PORTAL_FARM_CONFIG", "config.yaml")
        self.links_path: str = os.environ.get("PORTAL_LINKS", "portal/links.json")
        self.public_base_url: str = os.environ.get("PORTAL_BASE_URL", "").rstrip("/")

        self.google_client_id: str = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
        self.google_client_secret: str = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
        # default-deny: вход через Google только для перечисленных email.
        self.google_allow_emails: list[str] = [
            e.strip().lower() for e in os.environ.get("GOOGLE_ALLOW_EMAILS", "").split(",") if e.strip()
        ]
        self.google_redirect_uri: str = os.environ.get("GOOGLE_REDIRECT_URI", "").strip()

        self.telegram_token: str = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        # default-deny: бот отвечает только перечисленным Telegram user_id.
        self.telegram_allow_ids: list[int] = [
            int(i) for i in os.environ.get("TELEGRAM_ALLOW_IDS", "").split(",") if i.strip().isdigit()
        ]
        self.master_key: str = os.environ.get("FARMER_MASTER_KEY", "").strip()

        self.open_access: bool = _env_flag("PORTAL_OPEN_ACCESS", default=False)
        self.password_login: bool = _env_flag("PORTAL_PASSWORD_LOGIN", default=True)
        self.allow_insecure_password: bool = _env_flag("PORTAL_ALLOW_PASSWORD_HTTP", default=False)

        _sec = os.environ.get("PORTAL_COOKIE_SECURE", "").strip().lower()
        self.cookie_secure: bool = _sec in {"1", "true", "yes"} if _sec else self.public_base_url.startswith("https://")
        self.trust_proxy: bool = _env_flag("PORTAL_TRUST_PROXY", default=False)

        self._load_yaml_overrides()

    def _load_yaml_overrides(self) -> None:
        """portal_config.yaml может переопределять несекретные поля."""
        path = os.environ.get("PORTAL_CONFIG", "portal_config.yaml")
        if not os.path.exists(path):
            return
        with open(path, encoding="utf-8") as fh:
            data: dict[str, Any] = yaml.safe_load(fh) or {}
        self.host = str(data.get("host", self.host))
        self.port = int(data.get("port", self.port))
        self.public_base_url = str(data.get("public_base_url", self.public_base_url))
        if not self.db_path or self.db_path == "farming_state.db":
            self.db_path = str(data.get("db", self.db_path))
        if not self.farm_config or self.farm_config == "config.yaml":
            self.farm_config = str(data.get("farm_config", self.farm_config))
        if not self.links_path or self.links_path == "portal/links.json":
            self.links_path = str(data.get("links", self.links_path))

    @property
    def ready(self) -> bool:
        """Без секрета не подписываем сессии — портал не стартует."""
        return bool(self.secret)

    def telegram_enabled(self) -> bool:
        return bool(self.telegram_token)

    def google_enabled(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret)

    def google_email_allowed(self, email: str) -> bool:
        """Default-deny + PORTAL_OPEN_ACCESS для сознательного открытия."""
        if email in self.google_allow_emails:
            return True
        if self.open_access:
            return True
        return False

    def telegram_user_allowed(self, tg_id: int) -> bool:
        if tg_id in self.telegram_allow_ids:
            return True
        if self.open_access:
            return True
        return False

    def google_auth_url(self, state: str, code_challenge: str = "", nonce: str = "") -> str:
        redirect = self.google_redirect_uri or f"{self.public_base_url}/auth/google/callback"
        params: dict[str, str] = {
            "client_id": self.google_client_id,
            "response_type": "code",
            "redirect_uri": redirect,
            "scope": "openid email profile",
            "state": state,
        }
        if code_challenge:
            params["code_challenge"] = code_challenge
            params["code_challenge_method"] = "S256"
        if nonce:
            # nonce выживает редирект и возвращается в id_token: отсутствие/
            # несовпадение claim'а — признак переигранного токена (replay).
            params["nonce"] = nonce
        return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params)

    def to_public(self) -> dict:
        return {
            "google": self.google_enabled(),
            "telegram": self.telegram_enabled(),
            "base_url": self.public_base_url,
            "open_access": self.open_access,
        }


def load_links(path: str) -> dict:
    """Ссылки владельца из links.json (заполнить реальными).

    Сантизация: принимаем только http/https URL и непустые label — чтобы
    ссылки нельзя было накачать ни javascript:-, ни HTML-инъекциями.
    Формат: {"title": ..., "items": [{"label": ..., "url": ...}, ...]}
    """
    import json

    def _ok(item: Any) -> bool:
        if not isinstance(item, dict):
            return False
        url = str(item.get("url", ""))
        label = str(item.get("label", "") or "")
        return url.startswith(("https://", "http://")) and bool(label[:200])

    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        clean = {
            "title": str(data.get("title", "Наши ссылки"))[:200],
            "items": [i for i in data.get("items", []) if _ok(i)],
        }
        return clean
    except (OSError, ValueError):
        return {"title": "Наши ссылки", "items": []}
