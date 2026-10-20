"""HTTP-API и статика портала (aiohttp).

Сессии — подписанный cookie (httponly, SameSite=Lax, __Host- при HTTPS).
Доступ к ферме — только авторизованным (default-deny). Mini App
авторизуется подписью Telegram (initData). Парольный вход — только по HTTPS
(если явно не разрешён dev-флагом).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import threading
import time
from pathlib import Path

from aiohttp import web

from core.logger import logsafe
from portal import auth
from portal.config import PortalConfig, load_links
from portal.farm import FarmDaemon
from portal.shield import Shield, idempotent, shield_middleware

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
COOKIE_NAME = "harvest_session"
SECURE_COOKIE_NAME = "__Host-harvest_session"
COOKIE_TTL = 7 * 24 * 3600
OAUTH_COOKIE = "oauth_state"
# Интервал push-кадров статуса в /ws (секунды).
WS_PUSH_S = 3.0

KEY_CFG = web.AppKey("cfg", PortalConfig)
KEY_DAEMON = web.AppKey("daemon", FarmDaemon)
KEY_WS_CLIENTS = web.AppKey("ws_clients", set)
KEY_SHIELD = web.AppKey("shield", Shield)

_SESSION_TG = "tg"

# Mini App обязан работать во view-webview Telegram (iframe на t.me/telegram.me).
# Рабочий интерфейс: фреймы разрешены только self + Telegram. Страница входа
# в фреймах не нужна вообще (frame-ancestors 'none') — против clickjacking.
_CSP_BASE = (
    "default-src 'self'; script-src 'self'; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "img-src 'self' data:; font-src 'self' https://fonts.gstatic.com; connect-src 'self'; "
    "base-uri 'self'; form-action 'self'; object-src 'none'"
)
_CSP_FRAME_APP = "frame-ancestors 'self' https://t.me https://telegram.me"
_CSP_FRAME_LOGIN = "frame-ancestors 'none'"
# Mini App: единственный внешний скрипт — telegram-web-app.js (п.7 ТЗ этапа 3).
_CSP_SCRIPT_APP = "script-src 'self' https://telegram.org"

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
    """Загружает отозванные jti с вычисткой истёкших записей. Никогда не падает.

    Битые записи пропускаем по одной, а не роняем весь список: одна мусорная
    строка (частичная запись, ручная правка) не должна «воскрешать» все ранее
    отозванные сессии до их естественного expiry.
    """
    try:
        data = json.loads(Path(REVOKED_FILE).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        now = time.time()
        clean: dict[str, int] = {}
        for k, v in data.items():
            try:
                exp = int(v)
            except (TypeError, ValueError):
                continue
            if exp > now:
                clean[str(k)] = exp
        return clean
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
# Отдельный глобальный предохранитель для choke-эндпоинтов (tg/init, google):
# раньше переиспользовал _GLOBAL_* от логина — смена одного меняла другое.
# Значения те же (600с/20), но константы независимы для раздельной настройки.
_CHOKE_GLOBAL_WINDOW = 600.0
_CHOKE_GLOBAL_MAX = 20
_MAX_PASSWORD_LEN = 512


def _audit(request: web.Request, msg: str) -> None:
    user = _session_user(request)
    sub = user.get("sub", "?") if user else "-"
    logger.info("[AUDIT] sub=%s ip=%s %s", logsafe(sub), logsafe(_client_ip(request)), logsafe(msg))


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
    rec[:] = [t for t in rec if now - t < _CHOKE_GLOBAL_WINDOW]
    if len(rec) >= _CHOKE_GLOBAL_MAX:
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
    # Технические токены (confirm/ws) сессией не являются — см. auth.is_session_sub.
    if not auth.is_session_sub(user.get("sub")):
        return None
    if _is_revoked(user.get("jti")):
        return None
    return user


def _require_user(request: web.Request) -> dict:
    user = _session_user(request)
    if user is None:
        # Default-deny: каждое отклонение пишем в аудит-журнал (без токена/деталей),
        # чтобы несанкционированные обращения не проходили молча.
        logger.warning("[AUDIT] deny unauth sub=- ip=%s path=%s", logsafe(_client_ip(request)), logsafe(request.path))
        raise web.HTTPUnauthorized()
    return user


def _guard_check(request: web.Request, obj: object) -> web.Response | None:
    """Строгая проверка свободного ввода через shield.sanitize (stored-XSS guard).

    Возвращает 400-ответ при отказе, иначе None. Хранимые имена/тексты без
    этого держались только на esc() у текущих потребителей — новый рендер
    без esc получил бы stored-XSS.
    """
    shield = request.app.get(KEY_SHIELD)
    guard = getattr(shield, "sanitize", None)
    if guard is None:
        return None
    try:
        guard.check(obj)
    except Exception as e:  # noqa: BLE001 — SanitizationError(ValueError)
        logger.warning("[AUDIT] sanitize-reject path=%s err=%s", logsafe(request.path), logsafe(e))
        return _json({"error": f"bad input: {e}"}, status=400)
    return None


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
    try:
        return web.Response(text=(STATIC_DIR / path).read_text(encoding="utf-8"), content_type="text/html")
    except FileNotFoundError:
        # Контекст не нужен: клиенту не положено знать про файловую систему.
        raise web.HTTPNotFound() from None
    except OSError as e:
        # Причину сохраняем в цепочке — в логах она полезна, наружу не уходит.
        raise web.HTTPInternalServerError() from e


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
        logger.warning("[AUDIT] login-limit ip=%s", logsafe(ip))
        return _json({"error": "Слишком много попыток. Подождите 10 минут."}, status=429)
    if not _login_allowed_global():
        logger.warning("[AUDIT] login-limit GLOBAL")
        return _json({"error": "Слишком много попыток. Подождите 10 минут."}, status=429)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _json({"error": "ожидается application/json"}, status=400)
    if not isinstance(body, dict):
        return _json({"error": "ожидается application/json"}, status=400)
    password = str(body.get("password", ""))
    if not password or len(password) > _MAX_PASSWORD_LEN:
        return _json({"error": "Неверный пароль"}, status=401)
    if not auth.check_master_key(cfg.master_key, password):
        _note_login_fail(ip)
        _note_global_login_fail()
        logger.warning("[AUDIT] login-fail ip=%s", logsafe(ip))
        return _json({"error": "Неверный пароль"}, status=401)
    logger.info("[AUDIT] login-ok (password) ip=%s", logsafe(ip))
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
    # __Host-кука обязана приходить с атрибутом Secure, иначе браузер отбрасывает
    # Set-Cookie с Max-Age=0 и «мёртвая» сессия остаётся в хранилище до expiry.
    resp.del_cookie(SECURE_COOKIE_NAME, path="/", secure=True)
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
        logger.warning("[AUDIT] google_cb choke ip=%s", logsafe(ip))
        resp = web.HTTPFound(location="/login?error=rate")
        resp.del_cookie(OAUTH_COOKIE, path="/", secure=cfg.cookie_secure)
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
        logger.warning("[AUDIT] oauth bad_state ip=%s", logsafe(_client_ip(request)))
        resp = web.HTTPFound(location="/login?error=bad_state")
        resp.del_cookie(OAUTH_COOKIE, path="/", secure=cfg.cookie_secure)
        raise resp
    redirect = cfg.google_redirect_uri or f"{cfg.public_base_url}/auth/google/callback"
    _shield = request.app.get(KEY_SHIELD)
    _breaker = _shield.breaker("google") if _shield is not None else None
    profile = await auth.google_exchange(
        code, cfg.google_client_id, cfg.google_client_secret, redirect,
        code_verifier=verifier, nonce=nonce, breaker=_breaker,
    )
    if profile is None:
        resp = web.HTTPFound(location="/login?error=google")
        resp.del_cookie(OAUTH_COOKIE, path="/", secure=cfg.cookie_secure)
        raise resp
    email = profile["email"]
    if not cfg.google_email_allowed(email):
        logger.warning("[AUDIT] oauth denied email=%s ip=%s", logsafe(email), logsafe(_client_ip(request)))
        resp = web.HTTPFound(location="/login?error=denied")
        resp.del_cookie(OAUTH_COOKIE, path="/", secure=cfg.cookie_secure)
        raise resp
    logger.info("[AUDIT] login-ok (google) email=%s ip=%s", logsafe(email), logsafe(_client_ip(request)))
    resp = web.HTTPFound(location="/")
    resp.del_cookie(OAUTH_COOKIE, path="/", secure=cfg.cookie_secure)
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


@idempotent("farm:action")
async def api_farm_action(request: web.Request) -> web.Response:
    _require_user(request)
    daemon: FarmDaemon = request.app[KEY_DAEMON]
    action = request.match_info.get("action", "")
    if action not in {"start", "stop", "pause", "resume"}:
        return _json({"error": "unknown action"}, status=400)
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
        await broadcast_ws(request.app, "farm", {"action": action, "stopping": True, **daemon.run_state()})
        return _json({"ok": True, "stopping": True, **daemon.run_state()})
    if action == "start":
        ok = await daemon.start()
    elif action == "stop":
        ok = await daemon.stop()
    elif action == "pause":
        ok = await daemon.pause()
    else:
        ok = await daemon.resume()
    await broadcast_ws(request.app, "farm", {"action": action, "ok": ok, **daemon.run_state()})
    return _json({"ok": ok, **daemon.run_state()})


async def api_networks(request: web.Request) -> web.Response:
    """Каталог тестнет-сетей с признаком текущей/запущенной."""
    _require_user(request)
    daemon: FarmDaemon = request.app[KEY_DAEMON]
    return _json({"networks": daemon.networks_info()})


@idempotent("network:farm")
async def api_network_farm(request: web.Request) -> web.Response:
    """Переключить сеть и запустить/остановить фарм по ней (одна кнопка)."""
    _require_user(request)
    daemon: FarmDaemon = request.app[KEY_DAEMON]
    slug = request.match_info.get("name", "")
    action = request.match_info.get("action", "")
    if action not in {"start", "stop"}:
        return _json({"error": "unknown action"}, status=400)
    _audit(request, f"network:{slug}:{action}")
    if action == "start":
        switched = await daemon.switch_to(slug)
        if not switched:
            return _json({"error": "unknown network"}, status=404)
        ok = await daemon.start()
    else:
        ok = await daemon.stop()
    await broadcast_ws(request.app, "network", {"network": slug, "action": action, "ok": ok, **daemon.run_state()})
    return _json({"ok": ok, "network": slug, **daemon.run_state()})


async def api_cycle_history(request: web.Request) -> web.Response:
    _require_user(request)
    daemon: FarmDaemon = request.app[KEY_DAEMON]
    return _json({"history": await daemon.cycle_history()})


async def api_guard(request: web.Request) -> web.Response:
    """Состояние защитного контура: счётчики, латентности, брейкеры."""
    _require_user(request)
    shield: Shield = request.app[KEY_SHIELD]
    return _json(await shield.snapshot())


async def api_metrics(request: web.Request) -> web.Response:
    _require_user(request)
    daemon: FarmDaemon = request.app[KEY_DAEMON]
    try:
        limit = int(request.query.get("limit", "100"))
    except (TypeError, ValueError):
        return _json({"error": "limit must be 1-1000"}, status=400)
    if not 1 <= limit <= 1000:
        return _json({"error": "limit must be 1-1000"}, status=400)
    return _json({"metrics": await daemon.metrics_history(limit)})


async def api_top_wallets(request: web.Request) -> web.Response:
    _require_user(request)
    daemon: FarmDaemon = request.app[KEY_DAEMON]
    return _json({"wallets": await daemon.top_wallets()})


async def api_wallets_export(request: web.Request) -> web.Response:
    """Скачивание ВСЕХ кошельков с ключами (CSV/JSON).

    Приватные ключи — самый чувствительный ресурс фермы: доступ только
    авторизованным И по HTTPS (как мастер-ключ входа). Открытый текст ключей
    поверх plain-HTTP запрещён, если dev-флаг не разрешил явно.
    """
    _require_user(request)
    cfg: PortalConfig = request.app[KEY_CFG]
    if not (_is_https(request) or cfg.allow_insecure_password):
        logger.warning("[AUDIT] wallets-export denied over HTTP ip=%s", logsafe(_client_ip(request)))
        return _json({"error": "Экспорт ключей доступен только по HTTPS"}, status=403)
    daemon: FarmDaemon = request.app[KEY_DAEMON]
    fmt = request.query.get("format", "")
    if not fmt and request.method == "POST":
        try:
            _body = await request.json()
            if isinstance(_body, dict):
                fmt = str(_body.get("format", ""))
        except Exception:  # noqa: BLE001
            fmt = ""
    if not fmt:
        fmt = "csv"
    if fmt not in {"csv", "json"}:
        return _json({"error": "format должен быть csv или json"}, status=400)
    _audit(request, f"wallets:export:{fmt}")
    try:
        content, filename = await daemon.export_wallets(fmt)
    except RuntimeError as e:
        return _json({"error": str(e)}, status=503)
    if fmt == "json":
        ctype = "application/json"
    else:
        ctype = "text/csv"
    resp = web.Response(
        text=content,
        content_type=ctype,
        charset="utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
    # Ключи не должны оседать в промежуточных кэшах/браузере.
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return resp


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
        logger.warning("[AUDIT] tg bad id ip=%s", logsafe(_client_ip(request)))
        return _json({"error": "unauthorized"}, status=401)
    if not cfg.telegram_user_allowed(tg_id):
        # Uniform-401: «неверная подпись / не в списке» неразличимы снаружи.
        logger.warning("[AUDIT] tg denied id=%s ip=%s", logsafe(tg_id), logsafe(_client_ip(request)))
        return _json({"error": "unauthorized"}, status=401)
    name = str(user.get("first_name", ""))[:200]
    sub = f"{_SESSION_TG}:{tg_id}"
    resp = _json({"ok": True, "name": user.get("first_name", "")})
    _set_session_cookie(resp, cfg, auth.sign_token(cfg.secret, sub, name))
    return resp


async def api_groups_list(request: web.Request) -> web.Response:
    """Список всех групп кошельков."""
    _require_user(request)
    daemon: FarmDaemon = request.app[KEY_DAEMON]
    return _json({"groups": await daemon.list_groups()})


async def api_ws_token(request: web.Request) -> web.Response:
    """Короткий (60с) WS-тикет в query. Сессия живёт в cookie, в query —
    только тикет: долгоживущий токен в access-логах/proxy не оседает."""
    user = _require_user(request)
    cfg: PortalConfig = request.app[KEY_CFG]
    sub = str(user.get("sub", ""))
    name = str(user.get("name", ""))
    return _json({"token": auth.issue_ws_ticket(cfg.secret, sub, name)})


@idempotent("wallets:create")
async def api_wallets_create(request: web.Request) -> web.Response:
    """Создать N кошельков (1..50 за вызов; бот/CLI — до 5000 батчами)."""
    _require_user(request)
    daemon: FarmDaemon = request.app[KEY_DAEMON]
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _json({"error": "ожидается application/json"}, status=400)
    try:
        count = int(body.get("count", 3))
    except (TypeError, ValueError):
        return _json({"error": "count must be 1..50"}, status=400)
    if not 1 <= count <= 50:
        return _json({"error": "count must be 1..50"}, status=400)
    try:
        created = await daemon.create_wallets(count)
    except RuntimeError as e:
        return _json({"error": str(e)}, status=503)
    _audit(request, f"wallets:create count={len(created)}")
    return _json({"ok": True, "created": len(created), **daemon.run_state()})


async def api_groups_create(request: web.Request) -> web.Response:
    """Создать новую группу кошельков."""
    _require_user(request)
    daemon: FarmDaemon = request.app[KEY_DAEMON]
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _json({"error": "ожидается application/json"}, status=400)
    name = str(body.get("name", "")).strip()
    if not name or len(name) > 100:
        return _json({"error": "name required (max 100 chars)"}, status=400)
    import re as _re

    color = str(body.get("color", "#007bff")).strip()[:20]
    if not _re.fullmatch(r"#[0-9a-fA-F]{6}", color):
        return _json({"error": "color must be #rrggbb"}, status=400)
    bad = _guard_check(request, {"name": name})
    if bad is not None:
        return bad
    try:
        group = await daemon.create_group(name, color)
        _audit(request, f"group:create id={group.get('id')}")
        return _json({"ok": True, "group": group})
    except ValueError as e:
        return _json({"error": str(e)}, status=409)


async def api_groups_delete(request: web.Request) -> web.Response:
    """Удалить группу кошельков."""
    _require_user(request)
    daemon: FarmDaemon = request.app[KEY_DAEMON]
    group_id = request.match_info.get("id", "")
    try:
        gid = int(group_id)
    except ValueError:
        return _json({"error": "invalid group id"}, status=400)
    ok = await daemon.delete_group(gid)
    if ok:
        _audit(request, f"group:delete id={gid}")
    return _json({"ok": ok})


async def api_groups_add_wallet(request: web.Request) -> web.Response:
    """Добавить кошелёк в группу."""
    _require_user(request)
    daemon: FarmDaemon = request.app[KEY_DAEMON]
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _json({"error": "ожидается application/json"}, status=400)
    address = str(body.get("address", "")).strip()
    group_id = body.get("group_id")
    if not address or group_id is None:
        return _json({"error": "address and group_id required"}, status=400)
    try:
        gid = int(group_id)
    except (TypeError, ValueError):
        return _json({"error": "invalid group_id"}, status=400)
    ok = await daemon.add_wallet_to_group(address, gid)
    return _json({"ok": ok})


async def api_groups_remove_wallet(request: web.Request) -> web.Response:
    """Удалить кошелёк из группы."""
    _require_user(request)
    daemon: FarmDaemon = request.app[KEY_DAEMON]
    group_id = request.match_info.get("id", "")
    address = request.match_info.get("address", "")
    try:
        gid = int(group_id)
    except ValueError:
        return _json({"error": "invalid group id"}, status=400)
    ok = await daemon.remove_wallet_from_group(address, gid)
    return _json({"ok": ok})


async def api_scheduler_state(request: web.Request) -> web.Response:
    """Состояние планировщика и список задач."""
    _require_user(request)
    daemon: FarmDaemon = request.app[KEY_DAEMON]
    return _json(daemon.scheduler_state())


@idempotent("scheduler:add")
async def api_scheduler_add_task(request: web.Request) -> web.Response:
    """Добавить задачу в планировщик."""
    _require_user(request)
    daemon: FarmDaemon = request.app[KEY_DAEMON]
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _json({"error": "ожидается application/json"}, status=400)
    if not isinstance(body, dict):
        return _json({"error": "ожидается application/json"}, status=400)
    task_id = str(body.get("task_id", "")).strip()
    if not task_id or len(task_id) > 100:
        return _json({"error": "task_id required (max 100 chars)"}, status=400)
    name = str(body.get("name", "")).strip()[:200]
    try:
        interval_hours = float(body.get("interval_hours", 0))
        interval_minutes = float(body.get("interval_minutes", 0))
    except (TypeError, ValueError):
        return _json({"error": "interval must be a number"}, status=400)
    run_at_time = body.get("run_at_time")
    try:
        cycles = int(body.get("cycles", 1))
    except (TypeError, ValueError):
        return _json({"error": "cycles must be 1-1000"}, status=400)
    if cycles < 1 or cycles > 1000:
        return _json({"error": "cycles must be 1-1000"}, status=400)
    bad = _guard_check(request, {"task_id": task_id, "name": name, "run_at_time": run_at_time})
    if bad is not None:
        return bad
    task = await daemon.add_scheduler_task(
        task_id,
        name=name,
        interval_hours=interval_hours,
        interval_minutes=interval_minutes,
        run_at_time=run_at_time,
        cycles=cycles,
    )
    _audit(request, f"scheduler:add task={task_id}")
    return _json({"ok": True, "task": task})


async def api_scheduler_update_task(request: web.Request) -> web.Response:
    """Обновить задачу планировщика."""
    _require_user(request)
    daemon: FarmDaemon = request.app[KEY_DAEMON]
    task_id = request.match_info.get("task_id", "")
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _json({"error": "ожидается application/json"}, status=400)
    if not isinstance(body, dict):
        return _json({"error": "ожидается application/json"}, status=400)
    updates: dict = {}
    if "name" in body:
        updates["name"] = str(body["name"]).strip()[:200]
    if "interval_hours" in body:
        try:
            updates["interval_hours"] = float(body["interval_hours"])
        except (TypeError, ValueError):
            return _json({"error": "interval_hours must be a number"}, status=400)
    if "interval_minutes" in body:
        try:
            updates["interval_minutes"] = float(body["interval_minutes"])
        except (TypeError, ValueError):
            return _json({"error": "interval_minutes must be a number"}, status=400)
    if "run_at_time" in body:
        updates["run_at_time"] = body["run_at_time"]
    if "cycles" in body:
        try:
            cycles = int(body["cycles"])
        except (TypeError, ValueError):
            return _json({"error": "cycles must be 1-1000"}, status=400)
        if cycles < 1 or cycles > 1000:
            return _json({"error": "cycles must be 1-1000"}, status=400)
        updates["cycles"] = cycles
    if "enabled" in body:
        updates["enabled"] = bool(body["enabled"])
    if updates:
        bad = _guard_check(request, {"task_id": task_id, **{k: v for k, v in updates.items() if isinstance(v, str)}})
        if bad is not None:
            return bad
    task = await daemon.update_scheduler_task(task_id, **updates)
    if task is None:
        return _json({"error": "task not found"}, status=404)
    _audit(request, f"scheduler:update task={task_id}")
    return _json({"ok": True, "task": task})


async def api_scheduler_delete_task(request: web.Request) -> web.Response:
    """Удалить задачу из планировщика."""
    _require_user(request)
    daemon: FarmDaemon = request.app[KEY_DAEMON]
    task_id = request.match_info.get("task_id", "")
    ok = await daemon.remove_scheduler_task(task_id)
    if ok:
        _audit(request, f"scheduler:delete task={task_id}")
    return _json({"ok": ok})


async def api_scheduler_start(request: web.Request) -> web.Response:
    """Запустить планировщик."""
    _require_user(request)
    daemon: FarmDaemon = request.app[KEY_DAEMON]
    await daemon.start_scheduler()
    _audit(request, "scheduler:start")
    return _json({"ok": True})


async def api_scheduler_stop(request: web.Request) -> web.Response:
    """Остановить планировщик."""
    _require_user(request)
    daemon: FarmDaemon = request.app[KEY_DAEMON]
    ok = await daemon.stop_scheduler()
    if ok:
        _audit(request, "scheduler:stop")
    return _json({"ok": ok})


async def api_balance_alerts(request: web.Request) -> web.Response:
    """Состояние мониторинга балансов."""
    _require_user(request)
    daemon: FarmDaemon = request.app[KEY_DAEMON]
    return _json(daemon.balance_alerts_state())


async def api_set_balance_alerts(request: web.Request) -> web.Response:
    """Включить/выключить мониторинг балансов."""
    _require_user(request)
    daemon: FarmDaemon = request.app[KEY_DAEMON]
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _json({"error": "ожидается application/json"}, status=400)
    if not isinstance(body, dict):
        return _json({"error": "ожидается application/json"}, status=400)
    enabled = bool(body.get("enabled", False))
    try:
        threshold = float(body.get("threshold", 0.001))
    except (TypeError, ValueError):
        return _json({"error": "threshold must be 0-100"}, status=400)
    if threshold < 0 or threshold > 100:
        return _json({"error": "threshold must be 0-100"}, status=400)
    result = await daemon.set_balance_alerts(enabled, threshold)
    _audit(request, f"balance_alerts:enabled={enabled} threshold={threshold}")
    return _json({"ok": True, **result})


async def api_rpc_health(request: web.Request) -> web.Response:
    """Диагностика RPC: задержки, ошибки, circuit breaker, здоровье нод."""
    _require_user(request)
    daemon: FarmDaemon = request.app[KEY_DAEMON]
    return _json(await daemon.rpc_health())


async def api_faucet(request: web.Request) -> web.Response:
    """Статус крана: разбор pre-flight по стратегиям (без траты квоты)."""
    _require_user(request)
    daemon: FarmDaemon = request.app[KEY_DAEMON]
    try:
        report = await daemon.faucet_report()
    except Exception as e:  # noqa: BLE001
        return _json({"error": str(e)[:200]}, status=500)
    live = sum(1 for p in report if isinstance(p, dict) and p.get("alive"))
    return _json({"live": live, "strategies": report, "drip": daemon.drip_state()})


async def api_faucet_drip(request: web.Request) -> web.Response:
    """Ручной долив всех кошельков (фоновая задача, single-flight)."""
    _require_user(request)
    daemon: FarmDaemon = request.app[KEY_DAEMON]
    try:
        res = await daemon.drip_faucet()
    except RuntimeError as e:
        return _json({"error": str(e)}, status=503)
    _audit(request, f"faucet:drip started={res.get('started')}")
    return _json(res, status=202 if res.get("started") else 409)


async def websocket_endpoint(request: web.Request) -> web.WebSocketResponse:
    """WebSocket для real-time обновлений (статус, логи, события).

    Аутентификация: cookie сессии ЛИБО короткий WS-тикет в query
    (POST /api/ws/token, живёт 60 сек). Долгоживущая сессия в query
    не принимается — она бы осела в access-логах/proxy/history.
    """
    cfg: PortalConfig = request.app[KEY_CFG]
    daemon: FarmDaemon = request.app[KEY_DAEMON]
    clients: set = request.app[KEY_WS_CLIENTS]

    # Auth ДО prepare: не занимаем слот ws_max_clients анонимами.
    token = request.query.get("token", "")
    cookie = _get_session_cookie(request)
    user = None
    if token:
        cand = auth.read_token(cfg.secret, token)
        # В query — только ws-тикеты, сессионные/confirm-токены отклоняем.
        if (
            cand is not None
            and isinstance(cand.get("sub"), str)
            and cand["sub"].startswith("ws:")
            and not _is_revoked(cand.get("jti"))
        ):
            user = cand
    elif cookie:
        cand = auth.read_token(cfg.secret, cookie)
        if (
            cand is not None
            and auth.is_session_sub(cand.get("sub"))
            and not _is_revoked(cand.get("jti"))
        ):
            user = cand

    if user is None:
        raise web.HTTPUnauthorized()

    # Кап ДО prepare: handshake неуспешных не тратит FD/слот.
    shield = request.app.get(KEY_SHIELD)
    if shield is not None and shield.enabled and len(clients) >= shield.config.ws_max_clients:
        await shield.audit_event(
            "WS_CAP", str(user.get("sub", "?")), {"clients": len(clients)})
        raise web.HTTPServiceUnavailable()

    ws = web.WebSocketResponse(heartbeat=30.0)
    await ws.prepare(request)
    clients.add(ws)
    logger.info("[WS] client connected (sub=%s), total=%d", logsafe(user.get("sub")), len(clients))

    try:
        # Пушим статус с фиксированным шагом: клиент рисует дашборд из этих
        # кадров и переключается на HTTP-опрос только при обрыве сокета.
        # Revoke проверяем в цикле: logout кикает живой WS, украденная сессия
        # не живёт в сокете до обрыва.
        while not ws.closed:
            if _is_revoked(user.get("jti")):
                await ws.close(code=4001, message=b"unauthorized")
                break
            stats = await daemon.statistics()
            await ws.send_json({"type": "status", **stats})
            try:
                msg = await ws.receive(timeout=WS_PUSH_S)
            except TimeoutError:
                continue
            if msg.type not in (web.WSMsgType.TEXT, web.WSMsgType.BINARY):
                break
    except (ConnectionResetError, RuntimeError):
        logger.info("[WS] client gone")
    except Exception as e:
        logger.warning("[WS] exception: %s", e)
    finally:
        clients.discard(ws)
        logger.info("[WS] client disconnected, total=%d", len(clients))

    return ws


async def broadcast_ws(app: web.Application, event_type: str, data: dict) -> None:
    """Рассылает событие всем подключённым WebSocket-клиентам."""
    clients: set = app.get(KEY_WS_CLIENTS, set())
    if not clients:
        return
    msg = json.dumps({"type": event_type, **data})
    dead = []
    for ws in clients:
        try:
            await ws.send_str(msg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        clients.discard(ws)


async def healthz(request: web.Request) -> web.Response:
    cfg: PortalConfig = request.app[KEY_CFG]
    # Не отдаём daemon_running анонимам: статус фермера уходит только
    # авторизованным через /api/stats. Здесь — минимальный чек-поинт.
    return web.json_response({"status": "ok", "ready": cfg.ready})


def create_app(cfg: PortalConfig, daemon: FarmDaemon, shield: Shield | None = None) -> web.Application:
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
            logger.exception("Unhandled error %s %s", logsafe(request.method), logsafe(request.path))
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
                logger.warning("[AUDIT] csrf-origin origin=%s != base=%s", logsafe(origin), logsafe(base))
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
            # Авторизованные данные (/api/stats, /api/links, HTML) не должны
            # оседать в промежуточных кэшах: наивные кэши не учитывают Set-Cookie
            # в cache-key и могли бы отдать чужой сессии данные фермы.
            if not request.path.startswith("/static"):
                resp.headers.setdefault("Cache-Control", "no-store")
            frame = _CSP_FRAME_LOGIN if request.path == "/login" else _CSP_FRAME_APP
            csp = f"{_CSP_BASE}; {frame}"
            if request.path == "/app/" or request.path.startswith("/app/"):
                # Mini App: разрешаем только telegram-web-app.js снаружи (п.7 ТЗ).
                csp = csp.replace("script-src 'self'",
                                  _CSP_SCRIPT_APP, 1)
            resp.headers.setdefault("Content-Security-Policy", csp)
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

    _shield = shield if shield is not None else Shield.from_env()
    middlewares: list = [shield_middleware(_shield), security_headers, error_handler, origin_guard]
    if cfg.miniapp_public_only:
        # П.8 ТЗ: публично видны только Mini App и его API; остальное — 404.
        @web.middleware
        async def miniapp_only(request: web.Request, handler):
            p = request.path
            if p == "/app/" or p.startswith(("/app/", "/api/miniapp/", "/static/", "/healthz")):
                return await handler(request)
            logger.warning("[AUDIT] miniapp-only 404 ip=%s path=%s", logsafe(_client_ip(request)), logsafe(p))
            raise web.HTTPNotFound()

        middlewares.insert(0, miniapp_only)
    app = web.Application(
        client_max_size=1024 * 1024,
        middlewares=middlewares,
    )
    app[KEY_CFG] = cfg
    app[KEY_DAEMON] = daemon
    app[KEY_WS_CLIENTS] = set()
    app[KEY_SHIELD] = _shield

    async def _shield_start(_app: web.Application) -> None:
        await _app[KEY_SHIELD].start()

    async def _shield_stop(_app: web.Application) -> None:
        await _app[KEY_SHIELD].stop()

    app.on_startup.append(_shield_start)
    app.on_cleanup.append(_shield_stop)

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
    app.router.add_get("/api/networks", api_networks)
    app.router.add_post("/api/farm/network/{name}/{action}", api_network_farm)
    app.router.add_get("/api/cycle-history", api_cycle_history)
    app.router.add_get("/api/metrics", api_metrics)
    app.router.add_get("/api/guard", api_guard)
    app.router.add_get("/api/top-wallets", api_top_wallets)
    app.router.add_post("/api/wallets", api_wallets_create)
    app.router.add_post("/api/wallets/export", api_wallets_export)
    app.router.add_get("/api/groups", api_groups_list)
    app.router.add_post("/api/groups", api_groups_create)
    app.router.add_delete("/api/groups/{id}", api_groups_delete)
    app.router.add_post("/api/groups/wallet", api_groups_add_wallet)
    app.router.add_delete("/api/groups/{id}/wallet/{address}", api_groups_remove_wallet)
    app.router.add_get("/api/scheduler", api_scheduler_state)
    app.router.add_post("/api/scheduler/tasks", api_scheduler_add_task)
    app.router.add_put("/api/scheduler/tasks/{task_id}", api_scheduler_update_task)
    app.router.add_delete("/api/scheduler/tasks/{task_id}", api_scheduler_delete_task)
    app.router.add_post("/api/scheduler/start", api_scheduler_start)
    app.router.add_post("/api/scheduler/stop", api_scheduler_stop)
    app.router.add_get("/api/balance-alerts", api_balance_alerts)
    app.router.add_post("/api/balance-alerts", api_set_balance_alerts)
    app.router.add_get("/api/rpc-health", api_rpc_health)
    app.router.add_get("/api/faucet", api_faucet)
    app.router.add_post("/api/faucet/drip", api_faucet_drip)
    app.router.add_post("/api/ws/token", api_ws_token)
    app.router.add_get("/ws", websocket_endpoint)
    app.router.add_post("/api/tg/init", api_tg_init)
    from portal.miniapp import register_miniapp

    register_miniapp(app, cfg_key=KEY_CFG, daemon_key=KEY_DAEMON)
    app.router.add_static("/static", STATIC_DIR)
    return app
