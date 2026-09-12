"""HTTP-API и статика портала (aiohttp).

Сессии — подписанный cookie (httponly, SameSite=Lax, __Host- при HTTPS).
Доступ к ферме — только авторизованным (default-deny). Mini App
авторизуется подписью Telegram (initData). Парольный вход — только по HTTPS
(если явно не разрешён dev-флагом).
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import secrets
import threading
import time
from pathlib import Path

from aiohttp import web

from portal import auth
from portal.config import PortalConfig, load_links
from portal.farm import FarmDaemon

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
COOKIE_NAME = "harvest_session"
SECURE_COOKIE_NAME = "__Host-harvest_session"
COOKIE_TTL = 7 * 24 * 3600
OAUTH_COOKIE = "oauth_state"

KEY_CFG = web.AppKey("cfg", PortalConfig)
KEY_DAEMON = web.AppKey("daemon", FarmDaemon)

_SESSION_TG = "tg"

# Mini App обязан работать во view-webview Telegram (iframe на t.me/telegram.me).
# Рабочий интерфейс: фреймы разрешены только self + Telegram. Страница входа
# в фреймах не нужна вообще (frame-ancestors 'none') — против clickjacking.
_CSP_BASE = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; font-src 'self'; connect-src 'self'; "
    "base-uri 'self'; form-action 'self'; object-src 'none'"
)
_CSP_FRAME_APP = "frame-ancestors 'self' https://t.me https://telegram.me"
_CSP_FRAME_LOGIN = "frame-ancestors 'none'"

# Противовес потере session-сторов: при logout помечаем jti выданной сессии
# как отозванную. Список отозванных ПЕРСИСТИТСЯ на диск (revoked_sessions.json):
# рестарт портала не «воскрешает» уже отозванные куки (до их естественного
# expiry). Записи с истёкшим exp вычищаются при загрузке/ревокации, поэтому
# файл не растёт бесконечно.
# Ограничение: файл на один инстанс портала (один фармер на инстанс);
# параллельный запуск нескольких порталов на один файл не поддерживается.
REVOKED_FILE = os.environ.get("PORTAL_REVOKED_FILE", "revoked_sessions.json")
_revoked: dict[str, int] | None = None
# RLock: _is_revoked/_revoke держат лок и внутри зовут _ensure_revoked(),
# который захватывает тот же самый лок (вложенный захват).
_revoked_lock = threading.RLock()


def _load_revoked() -> dict[str, int]:
    """Загружает отозванные jti с досуга истёкших записей. Никогда не падает."""
    try:
        data = json.loads(Path(REVOKED_FILE).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        now = time.time()
        return {str(k): int(v) for k, v in data.items() if int(v) > now}
    except Exception:  # noqa: BLE001
        return {}


def _ensure_revoked() -> dict[str, int]:
    global _revoked
    if _revoked is None:
        with _revoked_lock:
            if _revoked is None:
                _revoked = _load_revoked()
    return _revoked


def _persist_revoked() -> None:
    try:
        import tempfile

        from core.utils import restrict_file_permissions

        rev_dir = os.path.dirname(os.path.abspath(REVOKED_FILE))
        # Уникальное tmp-имя (mkstemp, O_EXCL): предсказуемый «.tmp» можно было
        # подменить symlink/junction локально-злонамеренным процессом.
        fd, tmp = tempfile.mkstemp(prefix="revoked.", suffix=".tmp", dir=rev_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(_revoked, f, ensure_ascii=False)
            restrict_file_permissions(tmp)
            os.replace(tmp, REVOKED_FILE)
        except Exception:  # noqa: BLE001
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except Exception:  # noqa: BLE001
        # Отказ персистенции не должен ломать logout: jti остаётся в памяти
        # (отзыв действует до ближайшего рестарта — как раньше).
        logger.warning("Не удалось сохранить список отозванных сессий", exc_info=True)


def _revoke(jti: str | None, exp: int) -> None:
    if not jti:
        return
    with _revoked_lock:
        d = _ensure_revoked()
        d[jti] = exp
        if len(d) > 10_000:
            now = time.time()
            for k in [k for k, e in d.items() if e < now]:
                d.pop(k, None)
        # Пишем под тем же локом: два параллельных logout не должны открывать
        # один и тот же tmp-файл одновременно.
        _persist_revoked()


def _is_revoked(jti: str | None) -> bool:
    if not jti:
        return False
    with _revoked_lock:
        exp = _ensure_revoked().get(jti, 0)
    return exp > time.time()


# ---------- Rate-limiter для входа по паролю ----------
_login_fails: dict[str, list[float]] = {}
_global_login_fails: list[float] = []
_LOGIN_WINDOW = 600.0
_LOGIN_MAX = 5
# Глобальный предохранитель для PIN-режима (короткий пароль): per-IP лимит
# (5/10мин) фиксится простой сменой источника — ботнет/прокси-список легко
# обходит его, перебирая пароль с разных адресов. Дополнительно держим
# ОБЩИЙ счётчик фейлов: после N неудач на всех адресах эндпоинт входа
# блокируется целиком на окно (не помогает даже смена IP).
_GLOBAL_WINDOW = 600.0
_GLOBAL_MAX = 20
_MAX_PASSWORD_LEN = 512


def _audit(request: web.Request, msg: str) -> None:
    user = _session_user(request)
    sub = user.get("sub", "?") if user else "-"
    logger.info("[AUDIT] sub=%s ip=%s %s", sub, _client_ip(request), msg)


def _login_allowed(ip: str) -> bool:
    now = time.time()
    rec = _login_fails.setdefault(ip, [])
    rec[:] = [t for t in rec if now - t < _LOGIN_WINDOW]
    return len(rec) < _LOGIN_MAX


def _login_allowed_global() -> bool:
    now = time.time()
    _global_login_fails[:] = [t for t in _global_login_fails if now - t < _GLOBAL_WINDOW]
    return len(_global_login_fails) < _GLOBAL_MAX


def _note_login_fail(ip: str) -> None:
    _login_fails.setdefault(ip, []).append(time.time())
    if len(_login_fails) > 10_000:
        now = time.time()
        for key in [k for k, v in _login_fails.items() if not any(now - t < _LOGIN_WINDOW for t in v)]:
            _login_fails.pop(key, None)


def _note_global_login_fail() -> None:
    _global_login_fails.append(time.time())


# ---------- Лёгкий per-IP лимит для неаутентифицированных CPU/сеть-обработчиков ----------
# tg/init (HMAC-проверка подписи) и google/callback (сетевой обмен с Google) не имеют
# собственного аутентифицируемого входа — их можно молотить потоком запросов с одного
# адреса. Лимит не даёт крутить вычисления/сетевые ходы бесконечно. Бакет чистит себя
# по окну, словарь не растёт (prune при переполнении).
_choke_buckets: dict[tuple[str, str], list[float]] = {}
_CHOKE_WINDOW = 60.0
_CHOKE_LIMIT = 30


def _choke_allowed(bucket: str, source: str) -> bool:
    now = time.time()
    key = (bucket, source)
    rec = _choke_buckets.setdefault(key, [])
    rec[:] = [t for t in rec if now - t < _CHOKE_WINDOW]
    if len(rec) >= _CHOKE_LIMIT:
        return False
    rec.append(now)
    if len(_choke_buckets) > 20_000:
        for kk in [kk for kk, v in _choke_buckets.items() if not any(now - t < _CHOKE_WINDOW for t in v)]:
            _choke_buckets.pop(kk, None)
    return True


# Глобальный предохранитель для эндпоинтов, защищённых только per-IP лимитами
# (tg/init, google callback): при PORTAL_TRUST_PROXY=1 IP берётся из
# X-Forwarded-For и легко спуфится (ботнет подставляет разные адреса в обход
# прокси/напрямую к порту). ОТДЕЛЬНЫЙ глобальный счётчик не даёт молотить
# эндпоинт даже со сменой источника — как _login_allowed_global для пароля.
_global_choke: dict[str, list[float]] = {}


def _choke_allowed_global(bucket: str) -> bool:
    now = time.time()
    rec = _global_choke.setdefault(bucket, [])
    rec[:] = [t for t in rec if now - t < _GLOBAL_WINDOW]
    if len(rec) >= _GLOBAL_MAX:
        return False
    rec.append(now)
    return True


def _cookie_name(cfg: PortalConfig) -> str:
    return SECURE_COOKIE_NAME if cfg.cookie_secure else COOKIE_NAME


def _get_session_cookie(request: web.Request) -> str | None:
    cfg: PortalConfig = request.app[KEY_CFG]
    # Только кука для текущего режима: в Secure-режиме plain-кука (без Secure,
    # __Host-префикса) НЕ авторизует — иначе её можно снять с http-зеркала
    # того же origin и переиграть по HTTPS (downgrade сессии).
    return request.cookies.get(_cookie_name(cfg))


def _set_session_cookie(response: web.Response, cfg: PortalConfig, token: str) -> None:
    response.set_cookie(
        _cookie_name(cfg),
        token,
        max_age=COOKIE_TTL,
        path="/",
        httponly=True,
        samesite="Lax",
        secure=cfg.cookie_secure,
    )


def _session_user(request: web.Request) -> dict | None:
    cookie = _get_session_cookie(request)
    if not cookie:
        return None
    user = auth.read_token(request.app[KEY_CFG].secret, cookie)
    if user is None:
        return None
    if _is_revoked(user.get("jti")):
        return None
    return user


def _require_user(request: web.Request) -> dict:
    user = _session_user(request)
    if user is None:
        # Default-deny: каждое отклонение пишем в аудит-журнал (без токена/деталей),
        # чтобы несанкционированные обращения не проходили молча.
        logger.warning("[AUDIT] deny unauth sub=- ip=%s path=%s", _client_ip(request), request.path)
        raise web.HTTPUnauthorized()
    return user


def _client_ip(request: web.Request) -> str:
    """Реальный IP клиента: через X-Forwarded-For/X-Real-IP только при trust_proxy."""
    cfg: PortalConfig = request.app[KEY_CFG]
    if cfg.trust_proxy:
        xff = request.headers.get("X-Forwarded-For")
        if xff:
            return xff.split(",")[0].strip()
        xri = request.headers.get("X-Real-IP")
        if xri:
            return xri.strip()
    return request.remote or "?"


def _is_https(request: web.Request) -> bool:
    cfg: PortalConfig = request.app[KEY_CFG]
    if request.scheme == "https":
        return True
    if cfg.trust_proxy and request.headers.get("X-Forwarded-Proto", "").split(",")[0].strip() == "https":
        return True
    return False


def _password_login_allowed(request: web.Request) -> tuple[bool, str]:
    """Парольным входам (мастер-ключ = ключ дешифровки) нужен TLS."""
    cfg: PortalConfig = request.app[KEY_CFG]
    if not cfg.password_login:
        return False, "Вход по паролю отключён в конфигурации"
    if not cfg.master_key:
        return False, "master key не задан"
    if _is_https(request) or cfg.allow_insecure_password:
        return True, ""
    return False, "Мастер-ключ принимается только по HTTPS"


def _effective_base(request: web.Request) -> str:
    """Ожидаемый Origin для CSRF-сравнения.

    Приоритет — явный PORTAL_BASE_URL (деплой за прокси/на поддомене). Если его нет,
    за основу берём адрес, по которому пришёл сам запрос: локальный bind без base_url
    всё равно корректно сравнивает Origin браузера, а «чужой» отбросит.
    """
    cfg: PortalConfig = request.app[KEY_CFG]
    if cfg.public_base_url:
        return cfg.public_base_url.rstrip("/")
    proto = "https" if _is_https(request) else "http"
    host = request.host or "127.0.0.1"
    return f"{proto}://{host}"


def _json(payload: dict, status: int = 200) -> web.Response:
    return web.json_response(payload, status=status)


def _html(path: str) -> web.Response:
    return web.Response(text=(STATIC_DIR / path).read_text(encoding="utf-8"), content_type="text/html")


async def index(request: web.Request) -> web.Response:
    return _html("index.html")


async def login_page(request: web.Request) -> web.Response:
    return _html("login.html")


async def api_me(request: web.Request) -> web.Response:
    user = _session_user(request)
    cfg: PortalConfig = request.app[KEY_CFG]
    # open_access анонимам не отдаём (конфиг-энумерация): логин-страница
    # опирается только на google/password, а флаг «любой аккаунт пройдёт»
    # стороннему наблюдению не нужен.
    return _json(
        {
            "authed": bool(user),
            "name": user.get("name", "") if user else "",
            "sub": user.get("sub", "") if user else "",
            "google": cfg.google_enabled() and bool(cfg.google_allow_emails or cfg.open_access),
            "password": bool(cfg.password_login and cfg.master_key),
        }
    )


async def api_login_password(request: web.Request) -> web.Response:
    cfg: PortalConfig = request.app[KEY_CFG]
    ip = _client_ip(request)
    allowed, reason = _password_login_allowed(request)
    if not allowed:
        return _json({"error": reason}, status=403)
    if not _login_allowed(ip):
        logger.warning("[AUDIT] login-limit ip=%s", ip)
        return _json({"error": "Слишком много попыток. Подождите 10 минут."}, status=429)
    if not _login_allowed_global():
        logger.warning("[AUDIT] login-limit GLOBAL")
        return _json({"error": "Слишком много попыток. Подождите 10 минут."}, status=429)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _json({"error": "ожидается application/json"}, status=400)
    password = str(body.get("password", ""))
    if not password or len(password) > _MAX_PASSWORD_LEN:
        return _json({"error": "Неверный пароль"}, status=401)
    if not auth.check_master_key(cfg.master_key, password):
        _note_login_fail(ip)
        _note_global_login_fail()
        logger.warning("[AUDIT] login-fail ip=%s", ip)
        return _json({"error": "Неверный пароль"}, status=401)
    logger.info("[AUDIT] login-ok (password) ip=%s", ip)
    resp = _json({"ok": True})
    _set_session_cookie(resp, cfg, auth.sign_token(cfg.secret, "master", "master"))
    return resp


async def api_logout(request: web.Request) -> web.Response:
    user = _session_user(request)
    if user:
        _revoke(user.get("jti"), int(user.get("exp", 0)))
        _audit(request, "logout")
    resp = _json({"ok": True})
    resp.del_cookie(COOKIE_NAME, path="/")
    resp.del_cookie(SECURE_COOKIE_NAME, path="/")
    return resp


async def api_google_start(request: web.Request) -> web.Response:
    cfg: PortalConfig = request.app[KEY_CFG]
    if not cfg.google_enabled():
        return _json({"error": "Google-вход не настроен"}, status=400)
    verifier, challenge = auth.pkce_pair()
    # state + verifier + nonce выживают редирект в одном cookie (разделитель dot).
    state = auth.csrf_state(cfg.secret)
    # nonce: Google вернёт его в id_token — проигрыш чужого/старого токена
    # с другим nonce отклоняется в _parse_id_token.
    nonce = secrets.token_urlsafe(32)
    resp = web.HTTPFound(location=cfg.google_auth_url(state, code_challenge=challenge, nonce=nonce))
    resp.set_cookie(
        OAUTH_COOKIE,
        f"{state}.{verifier}.{nonce}",
        max_age=600,
        path="/",
        httponly=True,
        samesite="Lax",
        secure=cfg.cookie_secure,
    )
    raise resp


async def api_google_callback(request: web.Request) -> web.Response:
    cfg: PortalConfig = request.app[KEY_CFG]
    ip = _client_ip(request)
    # Сетевой обмен с Google на каждый запрос: не даём молотить callback потоком.
    # Хосты тыкают с разных фейковых XFF — держим и глобальный счётчик.
    if not _choke_allowed_global("google_cb") or not _choke_allowed("google_cb", ip):
        logger.warning("[AUDIT] google_cb choke ip=%s", ip)
        resp = web.HTTPFound(location="/login?error=rate")
        resp.del_cookie(OAUTH_COOKIE, path="/")
        raise resp
    code = request.query.get("code", "")
    state = request.query.get("state", "")
    stored = request.cookies.get(OAUTH_COOKIE, "")
    verifier = ""
    nonce = ""
    # Формат: "state.verifier.nonce" (все три — base64url без точек).
    parts = stored.split(".")
    if len(parts) == 3:
        stored_state, verifier, nonce = parts
    elif len(parts) == 2:
        stored_state, verifier = parts
    else:
        stored_state = stored
    if not auth.verify_csrf_state(cfg.secret, state) or state != stored_state:
        logger.warning("[AUDIT] oauth bad_state ip=%s", _client_ip(request))
        resp = web.HTTPFound(location="/login?error=bad_state")
        resp.del_cookie(OAUTH_COOKIE, path="/")
        raise resp
    redirect = cfg.google_redirect_uri or f"{cfg.public_base_url}/auth/google/callback"
    profile = await auth.google_exchange(
        code, cfg.google_client_id, cfg.google_client_secret, redirect, code_verifier=verifier, nonce=nonce
    )
    if profile is None:
        resp = web.HTTPFound(location="/login?error=google")
        resp.del_cookie(OAUTH_COOKIE, path="/")
        raise resp
    email = profile["email"]
    if not cfg.google_email_allowed(email):
        logger.warning("[AUDIT] oauth denied email=%s ip=%s", email, _client_ip(request))
        resp = web.HTTPFound(location="/login?error=denied")
        resp.del_cookie(OAUTH_COOKIE, path="/")
        raise resp
    logger.info("[AUDIT] login-ok (google) email=%s ip=%s", email, _client_ip(request))
    resp = web.HTTPFound(location="/")
    resp.del_cookie(OAUTH_COOKIE, path="/")
    _set_session_cookie(resp, cfg, auth.sign_token(cfg.secret, profile["sub"], profile["name"] or email))
    raise resp


async def api_links(request: web.Request) -> web.Response:
    # default-deny: ссылки владельца не отдаём анонимам.
    _require_user(request)
    cfg: PortalConfig = request.app[KEY_CFG]
    return _json(load_links(cfg.links_path))


async def api_stats(request: web.Request) -> web.Response:
    _require_user(request)
    daemon: FarmDaemon = request.app[KEY_DAEMON]
    return _json(await daemon.statistics())


async def api_farm_action(request: web.Request) -> web.Response:
    _require_user(request)
    daemon: FarmDaemon = request.app[KEY_DAEMON]
    action = request.match_info.get("action", "")
    if action not in {"start", "stop", "pause", "resume"}:
        return _json({"error": "unknown"}, status=404)
    _audit(request, f"farm:{action}")
    if action == "stop" and daemon.running:
        # Остановка может занять время (ждём завершение цикла) — не блокируем HTTP.
        task = asyncio.get_running_loop().create_task(daemon.stop())

        def _stop_done(t: asyncio.Task) -> None:
            try:
                t.result()
            except Exception:  # noqa: BLE001
                logger.exception("[AUDIT] stop task failed")

        task.add_done_callback(_stop_done)
        return _json({"ok": True, "stopping": True, **daemon.run_state()})
    if action == "start":
        ok = await daemon.start()
    elif action == "stop":
        ok = await daemon.stop()
    elif action == "pause":
        ok = await daemon.pause()
    else:
        ok = await daemon.resume()
    return _json({"ok": ok, **daemon.run_state()})


async def api_cycle_history(request: web.Request) -> web.Response:
    _require_user(request)
    daemon: FarmDaemon = request.app[KEY_DAEMON]
    return _json({"history": await daemon.cycle_history()})


async def api_top_wallets(request: web.Request) -> web.Response:
    _require_user(request)
    daemon: FarmDaemon = request.app[KEY_DAEMON]
    return _json({"wallets": await daemon.top_wallets()})


async def api_tg_init(request: web.Request) -> web.Response:
    """Mini App логин: подпись Telegram initData вместо пароля.

    Только POST: initData содержит Telegram-подпись (hash) и данные пользователя —
    в query-string она попала бы в access-журнал, историю браузера и логи
    реверс-прокси. GET-вариант удалён намеренно (405).
    """
    cfg: PortalConfig = request.app[KEY_CFG]
    if not cfg.telegram_token:
        return _json({"error": "telegram disabled"}, status=403)
    try:
        init_data = str((await request.json()).get("init_data", ""))
    except Exception:  # noqa: BLE001
        init_data = ""
    if len(init_data) > 16_384:
        # initData Telegram — сотни байт. Ограничение режет память/CPU-атаки
        # огромными подписями на уязвимом эндпоинте без rate-limit.
        return _json({"error": "bad init_data"}, status=401)
    if not _choke_allowed_global("tg_init") or not _choke_allowed("tg_init", _client_ip(request)):
        return _json({"error": "Слишком много попыток"}, status=429)
    user = auth.validate_telegram_init_data(cfg.telegram_token, init_data)
    if user is None:
        return _json({"error": "bad init_data"}, status=401)
    try:
        tg_id = int(user.get("id", 0))
    except (TypeError, ValueError):
        logger.warning("[AUDIT] tg bad id=%r ip=%s", user.get("id"), _client_ip(request))
        return _json({"error": "bad init_data"}, status=401)
    if not cfg.telegram_user_allowed(tg_id):
        logger.warning("[AUDIT] tg denied id=%s ip=%s", tg_id, _client_ip(request))
        return _json({"error": "denied"}, status=403)
    name = html.escape(str(user.get("first_name", "")))[:200]
    sub = f"{_SESSION_TG}:{tg_id}"
    resp = _json({"ok": True, "name": user.get("first_name", "")})
    _set_session_cookie(resp, cfg, auth.sign_token(cfg.secret, sub, name))
    return resp


async def healthz(request: web.Request) -> web.Response:
    cfg: PortalConfig = request.app[KEY_CFG]
    # Не отдаём daemon_running анонимам: статус фермера уходит только
    # авторизованным через /api/stats. Здесь — минимальный чек-поинт.
    return web.json_response({"status": "ok", "ready": cfg.ready})


def create_app(cfg: PortalConfig, daemon: FarmDaemon) -> web.Application:
    @web.middleware
    async def error_handler(request: web.Request, handler):
        """Никаких внутренностей наружу при неожиданной ошибке.

        Полный traceback — только в журнал портала; клиенту отдаём JSON без
        деталей (не раскрываем пути, исключения, SQL, конфиг).
        """
        try:
            return await handler(request)
        except web.HTTPException:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("Unhandled error %s %s", request.method, request.path)
            return web.json_response({"error": "internal error"}, status=500)

    @web.middleware
    async def origin_guard(request: web.Request, handler):
        """Защита от CSRF: мутирующие запросы только с ожидаемого origin.

        Fail-closed: даже без PORTAL_BASE_URL чуждые Origin блокируются
        (за основу своей страницы берём адрес запроса). Запросы без Origin
        (curl, боты, тесты) проходят — SameSite=Lax не отправит куку
        на кросс-сайтный POST, а SameSite-субдомен закрыт сравнением Origin.
        """
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            origin = request.headers.get("Origin")
            base = _effective_base(request)
            if origin and origin.rstrip("/") != base:
                logger.warning("[AUDIT] csrf-origin origin=%s != base=%s", origin, base)
                raise web.HTTPForbidden()
        return await handler(request)

    @web.middleware
    async def security_headers(request: web.Request, handler):
        """Заголовки безопасности на ЛЮБОЙ ответ — включая ошибки внутренних
        middleware (CSRF-403 от origin_guard и т.п.), иначе прокси/кэш увидит
        ответ без CSP/nosniff и может закэшировать уязвимую версию."""

        def _apply(resp) -> web.Response:
            resp.headers.setdefault("X-Content-Type-Options", "nosniff")
            resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
            frame = _CSP_FRAME_LOGIN if request.path == "/login" else _CSP_FRAME_APP
            resp.headers.setdefault("Content-Security-Policy", f"{_CSP_BASE}; {frame}")
            # HSTS только когда соединение фактически HTTPS (прямо или через
            # доверенный прокси): на plain-HTTP веб-клиенты игнорируют заголовок,
            # а ошибочная выдача в будущем могла бы закэшировать downgrade-политику.
            if _is_https(request):
                resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
            # X-Frame-Options/frame-ancestors сознательно не задаём: Mini App
            # обязан работать во вью-вебпросмотре Telegram (iframe стороннего origin).
            return resp

        try:
            return _apply(await handler(request))
        except web.HTTPException as e:
            _apply(e)
            raise

    app = web.Application(client_max_size=1024 * 1024, middlewares=[security_headers, error_handler, origin_guard])
    app[KEY_CFG] = cfg
    app[KEY_DAEMON] = daemon

    app.router.add_get("/", index)
    app.router.add_get("/login", login_page)
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/api/me", api_me)
    app.router.add_post("/api/login/password", api_login_password)
    app.router.add_post("/api/logout", api_logout)
    app.router.add_get("/auth/google", api_google_start)
    app.router.add_get("/auth/google/callback", api_google_callback)
    app.router.add_get("/api/links", api_links)
    app.router.add_get("/api/stats", api_stats)
    app.router.add_post("/api/farm/{action}", api_farm_action)
    app.router.add_get("/api/cycle-history", api_cycle_history)
    app.router.add_get("/api/top-wallets", api_top_wallets)
    app.router.add_post("/api/tg/init", api_tg_init)
    app.router.add_static("/static", STATIC_DIR)
    return app
