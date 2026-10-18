"""Аутентификация портала: сессии (HMAC), мастер-ключ как пароль,
Google OAuth и валидация Telegram Mini App (WebAppInitData).

Всё, что можно — чистые функции без сети, покрыты тестами.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from urllib.parse import parse_qsl

import aiohttp

_SESSION_TTL = 7 * 24 * 3600

# Короткоживущий WS-тикет (query): в access-логах/proxy оседает только он,
# а не долгоживущая сессия. Неймспейсы sub: "tma-confirm:*" (miniapp),
# "ws:*" (сокет). HTTP-сессией считаются только остальные sub.
WS_TICKET_TTL = 60

# Провайдеры id_token Google (защита от подмены issuer).
_GOOGLE_ISS = {"https://accounts.google.com", "accounts.google.com"}
_ID_TOKEN_LEEWAY = 60

# Допуск часового скоса для auth_date из будущего (Mini App, п.1 ТЗ этапа 3).
_TMA_FUTURE_LEEWAY = 60

# 64 hex-символа — формат мастер-ключа БД. Hex нечувствителен к регистру:
# именно для него сравниваем в нижнем регистре. Для всего остального (PIN) —
# строго case-sensitive, чтобы не урезать энтропию алфавитных паролей вдвое.
_HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64url(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def _parse_id_token(id_token: str, expected_aud: str, expected_nonce: str = "") -> dict | None:
    """Декодирует payload Google id_token.

    Проверки: формат JWT, aud == наш client_id, iss == Google, exp/nbf/iat в
    пределах допуска. Если в запрос авторизации был передан nonce — должен
    вернуться в id_token (иначе token переигран). Без криптоподписи (токен
    приходит напрямую от Google по TLS).
    """
    if not id_token:
        return None
    try:
        _header, claims, _sig = id_token.split(".")
        payload = json.loads(_unb64url(claims).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if payload.get("aud") != expected_aud:
        return None
    if payload.get("iss") not in _GOOGLE_ISS:
        return None
    if expected_nonce and payload.get("nonce") != expected_nonce:
        return None
    now = time.time()
    exp = int(payload.get("exp", 0))
    if exp <= 0 or now > exp + _ID_TOKEN_LEEWAY:
        return None
    nbf = int(payload.get("nbf", 0))
    if nbf and now < nbf - _ID_TOKEN_LEEWAY:
        return None
    iat = int(payload.get("iat", 0))
    if iat and iat > now + _ID_TOKEN_LEEWAY:
        return None
    if not payload.get("email_verified"):
        return None
    return payload


def sign_token(secret: str, sub: str, name: str = "", ttl: int = _SESSION_TTL) -> str:
    """Подписанная сессия: payload.sub signed HMAC-SHA256(secret).

    jti — случайный идентификатор сессии: по нему logout-инвалидация
    (см. api._revoked) может аннулировать выдачу до истечения TTL.
    """
    payload = {
        "sub": sub,
        "name": name,
        "jti": secrets.token_hex(8),
        "exp": int(time.time()) + ttl,
    }
    body = _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = _b64url(hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest())
    return f"{body}.{sig}"


def read_token(secret: str, token: str) -> dict | None:
    """Возвращает payload, если подпись верна и срок не истёк."""
    try:
        body, sig = token.rsplit(".", 1)
    except ValueError:
        return None
    expected = _b64url(hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        payload = json.loads(_unb64url(body).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    try:
        exp = int(payload.get("exp", 0))
    except (TypeError, ValueError):
        return None
    if exp < time.time():
        return None
    return payload


def is_session_sub(sub: object) -> bool:
    """True, если sub — HTTP-сессия пользователя, а не технический токен.

    Технические неймспейсы тем же HMAC-ключом/форматом подписываются,
    но сессией не являются: "tma-confirm:*" (miniapp export), "ws:*" (сокет).
    Их принятие как сессии — privilege confusion (confirm-токен давал бы
    доступ ко всему web-API).
    """
    if not isinstance(sub, str) or not sub:
        return False
    return not (sub.startswith("tma-confirm:") or sub.startswith("ws:"))


def issue_ws_ticket(secret: str, sub: str, name: str = "") -> str:
    """Короткий (60с) тикет только для WS-handshake в query. Не сессия."""
    return sign_token(secret, f"ws:{sub}", name, ttl=WS_TICKET_TTL)


def check_master_key(actual: str, given: str) -> bool:
    """«Тот самый пароль» = мастер-ключ фармера. Сравнение в константное время.

    Для 64-hex мастер-ключа регистр не важен (hex case-insensitive). Для
    коротких PIN (разрешённая деградация веб-входа) сравнение строго
    case-sensitive: downcasing уполовинил бы пространство алфавитных паролей.
    """
    if not actual or not given:
        return False
    actual = actual.strip()
    given = given.strip()
    if _HEX64.match(actual) and _HEX64.match(given):
        return hmac.compare_digest(actual.lower(), given.lower())
    # compare_digest для str принимает только ASCII: кириллический/юникодный PIN
    # падал бы TypeError (500 вместо 401). Переводим в bytes (utf-8) — сравнение
    # остаётся константным по времени и корректным для любых символов.
    return hmac.compare_digest(actual.encode("utf-8"), given.encode("utf-8"))


def _hmac_sha256(key: bytes, data: bytes) -> bytes:
    return hmac.new(key, data, hashlib.sha256).digest()


def validate_telegram_init_data(bot_token: str, init_data: str, max_age: int = 10 * 60) -> dict | None:
    """Проверка данных мини-аппа, подписанных Telegram секретом бота.

    Возвращает dict с user (json) при валидной подписи, иначе None.
    Отбрасывает устаревшие подписи (по auth_date) — защита от повторного
    воспроизведения старого initData. Окно по умолчанию — 10 минут
    (Telegram рекомендует отсекать старые подписи в пределах минут:
    проскочивший QQuery в чужие руки не должен жить сутки).
    """
    if not init_data:
        return None
    items = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = items.pop("hash", None)
    if not received_hash:
        return None
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(items.items()))
    secret_key = _hmac_sha256(b"WebAppData", bot_token.encode("utf-8"))
    computed = _hmac_sha256(secret_key, check_string.encode("utf-8")).hex()
    if not hmac.compare_digest(computed, received_hash):
        return None
    try:
        auth_date = int(items.get("auth_date", "0"))
    except ValueError:
        return None
    now = time.time()
    if auth_date <= 0 or auth_date > now + _TMA_FUTURE_LEEWAY:
        # Подпись из будущего — тоже отказ (п.1 ТЗ этапа 3).
        return None
    if now - auth_date > max_age:
        return None
    user = items.get("user")
    try:
        return json.loads(user) if user else {}
    except ValueError:
        return None


async def google_exchange(
    code: str, client_id: str, client_secret: str, redirect_uri: str, code_verifier: str = "", nonce: str = "",
    breaker=None, timeout_s: float = 20.0,
) -> dict | None:
    """Обмен кода авторизации Google на данные профиля (email, name).

    Возвращает {"email": ..., "name": ..., "sub": ...} при успехе.
    Сетевой вызов идёт через CircuitBreaker (если передан) с ретраями
    и жёстким таймаутом: висящий Google не должен вешать хендлер.
    """
    from portal.shield.breaker import run_with_retry

    token_url = "https://oauth2.googleapis.com/token"
    data = {
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }
    if code_verifier:
        data["code_verifier"] = code_verifier

    async def _post():
        timeout = aiohttp.ClientTimeout(total=max(1.0, timeout_s))
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(token_url, data=data) as resp:
                if resp.status != 200:
                    return None
                return await resp.json()

    try:
        tokens = await run_with_retry(_post, attempts=3, breaker=breaker)
    except Exception:  # noqa: BLE001 — брейкер открыт / сеть легла: тихий отказ
        return None
    if not tokens:
        return None
    id_token = tokens.get("id_token")
    if not id_token:
        return None
    payload = _parse_id_token(id_token, client_id, expected_nonce=nonce)
    if payload is None:
        return None
    return {
        "email": payload.get("email", "").lower(),
        "name": payload.get("name", ""),
        "sub": payload.get("sub", ""),
    }


def csrf_state(secret: str) -> str:
    # HMAC-привязка к secret запуска: state = rand + sig(rand, secret).
    # Верификация сверяет подпись, а не только charset — подделка без secret не проходит.
    # Разделитель "~" (вне base64url и вне "." разделителя oauth-cookie).
    rand = secrets.token_urlsafe(18)
    sig = hmac.new(secret.encode("utf-8"), rand.encode("ascii"), hashlib.sha256).digest()
    sig_b64 = base64.urlsafe_b64encode(sig).rstrip(b"=").decode("ascii")
    return f"{rand}~{sig_b64}"


def verify_csrf_state(secret: str, state: str) -> bool:
    if not state or len(state) < 16 or len(state) > 256:
        return False
    if "~" in state:
        if state.count("~") != 1:
            return False
        rand, sig_b64 = state.split("~", 1)
        if not rand or not sig_b64:
            return False
        allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
        if not (set(rand) <= allowed and set(sig_b64) <= allowed):
            return False
        try:
            pad = "=" * (-len(sig_b64) % 4)
            sig = base64.urlsafe_b64decode(sig_b64 + pad)
        except Exception:  # noqa: BLE001
            return False
        exp = hmac.new(secret.encode("utf-8"), rand.encode("ascii"), hashlib.sha256).digest()
        return hmac.compare_digest(sig, exp)
    if "." in state:
        return False
    # Legacy-формат (только rand): принимаем по charset, итоговая привязка —
    # сравнение с cookie в хендлере. Новые state всегда с подписью.
    if set(state) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"):
        return len(state) >= 16
    return False


def pkce_pair() -> tuple[str, str]:
    """code_verifier / code_challenge (S256) для Google OAuth."""
    verifier = base64.urlsafe_b64encode(os.urandom(48)).rstrip(b"=").decode("ascii")
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    return verifier, challenge
