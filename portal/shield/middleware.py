"""Подключение щита к aiohttp одной строкой.
`shield_middleware(shield)` — глобальный middleware: correlation_id,
глобальный и per-actor троттлинг (read/write по методу), скоринг риска
(tarpit / shadowban с правдоподобным ответом), замер латентности.

`@idempotent("wallets:create")` — декоратор мутирующих хендлеров:
повторный запрос с тем же телом в пределах TTL возвращает сохранённый
ответ, бизнес-логика второй раз не выполняется.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiohttp import web

from .audit import bind_correlation
from .risk import HIGH, MEDIUM

logger = logging.getLogger(__name__)

# Пути со своим legacy-троттлингом (login-choke): риск-скоринг их не трогает,
# чтобы не ломать диагностику 429; глобальный shedder и per-actor лимиты действуют.
_RISK_EXEMPT_PREFIXES = (
    "/api/login/", "/auth/", "/api/tg/init",
)


def _client_ip(request: web.Request) -> str:
    # Доверяем X-Forwarded-For только при явном PORTAL_TRUST_PROXY=1,
    # иначе злоумышленник подменяет IP одной строкой заголовка.
    try:
        cfg = request.app.get("cfg") if hasattr(request, "app") and request.app else None
        trust = bool(getattr(cfg, "trust_proxy", False)) if cfg is not None else False
        # legacy-ключ api.py: app[KEY_CFG]
        if not trust and hasattr(request, "app") and request.app:
            for _v in request.app.values():
                if hasattr(_v, "trust_proxy"):
                    trust = bool(_v.trust_proxy)
                    break
        if trust:
            return request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or (
                request.remote or "unknown"
            )
        return request.remote or "unknown"
    except Exception:  # noqa: BLE001 # pragma: no cover - defensive
        return "unknown"


def _actor_of(request: web.Request) -> str:
    """Актор для ключа идемпотентности: IP + хэш сессионной куки.

    Одна IP за NAT — разные пользователи: без привязки к сессии повтор
    одного пользователя вернул бы чужой закэшированный ответ.
    """
    ip = _client_ip(request)
    try:
        jar = request.cookies or {}
        sess = jar.get("harvest_session") or jar.get("__Host-harvest_session") or ""
    except Exception:  # noqa: BLE001
        sess = ""
    if sess:
        digest = hashlib.sha256(sess.encode("utf-8", "ignore")).hexdigest()[:16]
        return f"ip:{ip}:sess:{digest}"
    return f"ip:{ip}:anon"


def shield_middleware(shield) -> Any:
    """Фабрика middleware. `shield` — экземпляр Shield (duck-typed)."""

    @web.middleware
    async def _mw(request: web.Request, handler: Callable) -> web.StreamResponse:
        cid = request.headers.get("X-Request-ID") or None
        bind_correlation(cid)
        t0 = time.monotonic()
        op = "shield"
        try:
            if not shield.enabled:
                return await handler(request)
            ip = _client_ip(request)
            kind = "read" if request.method in ("GET", "HEAD", "OPTIONS") else "write"
            op = f"{request.method} {request.path}"
            chk = await shield.limits.check(f"ip:{ip}", kind)
            if not chk.allowed:
                await shield.audit_event(
                    "RATE_LIMIT_EXCEEDED", f"ip:{ip}",
                    {"op": op, "reason": chk.reason, "retry_after": round(chk.retry_after_s, 2)})
                status = 503 if chk.reason == "global_overload" else 429
                return web.json_response(
                    {"ok": False, "error": "busy" if status == 503 else "rate_limited",
                     "retry_after": round(chk.retry_after_s, 2)},
                    status=status,
                    headers={"Retry-After": str(int(chk.retry_after_s) + 1)},
                )
            (verdict, _reasons) = (await shield.observe(f"ip:{ip}")
                                   if not request.path.startswith(_RISK_EXEMPT_PREFIXES)
                                   else (None, []))
            if verdict is not None and verdict.level == MEDIUM:
                await shield.audit_event(
                    "TARPIT", f"ip:{ip}",
                    {"op": op, "score": round(verdict.score, 2), "sleep": verdict.tarpit_s})
                await asyncio.sleep(verdict.tarpit_s)
            elif verdict is not None and verdict.level == HIGH:
                await shield.audit_event(
                    "SHADOWBAN_TRIGGERED", f"ip:{ip}",
                    {"op": op, "score": round(verdict.score, 2)})
                return web.json_response({"ok": True})
            return await handler(request)
        finally:
            try:
                ms = (time.monotonic() - t0) * 1000.0
                await shield.audit.observe_latency(op, ms)
                if ms >= shield.config.slow_ms:
                    await shield.audit_event(
                        "SLOW_REQUEST", _client_ip(request),
                        {"op": op, "ms": round(ms, 1)})
            except Exception:  # noqa: BLE001
                logger.warning("shield telemetry failed", exc_info=True)

    return _mw


def idempotent(action: str):
    """Декоратор мутирующего хендлера: дедупликация по телу запроса.

    Ключ: actor (сессия/ip) + action + путь + хэш тела + временной бакет.
    Тело читается один раз и кладётся в `request["shield_body"]` для хендлера.
    """

    def deco(handler: Callable[..., Awaitable[web.StreamResponse]]):
        @functools.wraps(handler)
        async def wrapper(request: web.Request) -> web.StreamResponse:
            shield = request.app.get("shield")
            if shield is None or not shield.enabled:
                return await handler(request)
            actor = _actor_of(request)
            try:
                raw = await request.read()
            except Exception:  # noqa: BLE001
                return await handler(request)
            try:
                request["shield_body"] = raw
            except Exception:  # noqa: BLE001
                pass
            key = shield.idem_key(actor, f"{action}|{request.path}", raw)
            holder: dict[str, Any] = {}

            async def _once() -> dict[str, Any]:
                resp = await handler(request)
                body = resp.body
                if body is None and resp.text is not None:
                    body = resp.text.encode(resp.charset or "utf-8")
                holder["resp"] = resp
                return {
                    "status": resp.status,
                    "body": bytes(body) if body is not None else b"",
                    "content_type": resp.content_type,
                    "charset": resp.charset or "utf-8",
                }

            res = await shield.idem.run(key, _once)
            if not res.replayed:
                return holder["resp"]
            snap = res.result
            await shield.audit_event(
                "IDEMPOTENT_REPLAY", actor, {"action": action})
            return web.Response(
                status=snap["status"],
                body=snap["body"],
                content_type=snap["content_type"],
                charset=snap["charset"],
            )

        return wrapper

    return deco


__all__ = ["shield_middleware", "idempotent"]
