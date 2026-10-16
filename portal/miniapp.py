"""Mini App API (этап 3): initData в заголовке, uniform-401, токены подтверждения.

Авторизация — на КАЖДОМ запросе к /api/miniapp/*: подпись initData
проверяется тем же auth.validate_telegram_init_data, user_id сверяется
с TELEGRAM_ALLOW_IDS. Клиенту ничему не верим: initDataUnsafe/user_id
из тела не считаются подтверждением.

Ответ отказа везде одинаковый: 401 {"error": "unauthorized"} — без деталей
(подпись неверна / устарело / не в списке неразличимы снаружи).
initData и токен бота в логи не пишутся (редакция этапа 2).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from datetime import UTC, datetime

from aiohttp import web

from portal import auth
from portal import bot_ops as _bot_ops
from portal.config import PortalConfig
from portal.farm import FarmDaemon
from portal.shield import idempotent

logger = logging.getLogger(__name__)

# initData больше ~1 КБ не бывает; запас с головой, CPU-атаки режем.
MAX_INITDATA = 16_384
_UNAUTH = {"error": "unauthorized"}

# Мутации: 30/мин на (user_id, эндпоинт), словарь с чисткой по окну.
_MUT_WINDOW = 60.0
_MUT_LIMIT = 30
_mut: dict[tuple[int, str], list[float]] = {}

# Лёгкий per-IP choke неаутентифицированных запросов (как в api.py, своя копия:
# api импортирует этот модуль для роутов, обратный импорт невозможен).
_CHOKE_WINDOW = 60.0
_CHOKE_LIMIT = 30
_choke: dict[tuple[str, str], list[float]] = {}
_GCHOKE_WINDOW = 600.0
_GCHOKE_LIMIT = 20
_gchoke: dict[str, list[float]] = {}


def _mut_ok(tg_id: int, endpoint: str) -> bool:
    now = time.time()
    key = (tg_id, endpoint)
    rec = _mut.setdefault(key, [])
    rec[:] = [t for t in rec if now - t < _MUT_WINDOW]
    if len(rec) >= _MUT_LIMIT:
        return False
    rec.append(now)
    if len(_mut) > 20_000:
        for kk in [kk for kk, v in _mut.items() if not any(now - t < _MUT_WINDOW for t in v)]:
            _mut.pop(kk, None)
    return True


def _choke_ok(bucket: str, ip: str) -> bool:
    now = time.time()
    key = (bucket, ip)
    rec = _choke.setdefault(key, [])
    rec[:] = [t for t in rec if now - t < _CHOKE_WINDOW]
    if len(rec) >= _CHOKE_LIMIT:
        return False
    rec.append(now)
    return True


def _gchoke_ok(bucket: str) -> bool:
    now = time.time()
    rec = _gchoke.setdefault(bucket, [])
    rec[:] = [t for t in rec if now - t < _GCHOKE_WINDOW]
    if len(rec) >= _GCHOKE_LIMIT:
        return False
    rec.append(now)
    return True


def _bearer_init_data(request: web.Request) -> str:
    """initData из заголовка Authorization: tma <initData> (п.1 ТЗ)."""
    h = request.headers.get("Authorization", "")
    if h.startswith("tma "):
        return h[4:].strip()
    return ""


def _client_ip(request: web.Request, cfg: PortalConfig) -> str:
    if cfg.trust_proxy:
        xff = request.headers.get("X-Forwarded-For")
        if xff:
            return xff.split(",")[0].strip()
    return request.remote or "?"


async def tma_user(
    request: web.Request, cfg: PortalConfig
) -> tuple[int, str] | None:
    """Проверенный (tg_id, first_name) или None. Без деталей наружу."""
    raw = _bearer_init_data(request)
    if not raw or len(raw) > MAX_INITDATA:
        return None
    ip = _client_ip(request, cfg)
    if not _gchoke_ok("tma") or not _choke_ok("tma", ip):
        logger.warning("[AUDIT] tma choke ip=%s", ip)
        return None
    user = auth.validate_telegram_init_data(cfg.telegram_token, raw)
    if user is None:
        return None
    try:
        tg_id = int(user.get("id", 0))
    except (TypeError, ValueError):
        logger.warning("[AUDIT] tma bad id ip=%s", ip)
        return None
    if not tg_id or not cfg.telegram_user_allowed(tg_id):
        logger.warning("[AUDIT] tma denied id=%s ip=%s", tg_id, ip)
        return None
    return tg_id, str(user.get("first_name", ""))[:200]


def _deny() -> web.Response:
    return web.json_response(dict(_UNAUTH), status=401)


# ---------- одноразовые токены подтверждения (п.1, п.5 ТЗ) ----------

_CONFIRM_TTL = 300.0
_confirm_used: set[str] = set()


def issue_confirm(secret: str, tg_id: int, action: str) -> str:
    """Токен на опасное действие: HMAC-подпись + jti, одноразовый."""
    return auth.sign_token(secret, f"tma-confirm:{tg_id}:{action}", "", ttl=int(_CONFIRM_TTL))


def consume_confirm(secret: str, token: str, tg_id: int, action: str) -> bool:
    if not token or token in _confirm_used:
        return False
    payload = auth.read_token(secret, token)
    if payload is None:
        return False
    if payload.get("sub") != f"tma-confirm:{tg_id}:{action}":
        return False
    _confirm_used.add(token)
    if len(_confirm_used) > 10_000:
        _confirm_used.clear()
    return True


# ---------- сборка состояния (те же источники, что у бота и сайта) ----------


async def _state_payload(
    daemon: FarmDaemon, tg_id: int, db_path: str | None
) -> dict:
    try:
        stats = await daemon.statistics()
    except Exception:  # noqa: BLE001
        stats = {"running": False}
    pool = stats.get("pool", {}) or {}
    slug = str(stats.get("network") or "")
    name, chain = slug or "—", ""
    try:
        infos = daemon.networks_info() if hasattr(daemon, "networks_info") else []
    except Exception:  # noqa: BLE001
        infos = []
    if not isinstance(infos, list):
        infos = []
    for n in infos:
        if (slug and n.get("slug") == slug) or (not slug and n.get("current")):
            name = str(n.get("name") or n.get("slug") or "—")
            chain = n.get("chain_id", "")
            break
    try:
        cells, total = await daemon.wallet_cells()
    except Exception:  # noqa: BLE001
        try:
            total = int(stats.get("wallet_count") or 0)
        except (TypeError, ValueError):
            total = 0
        cells = ""
    try:
        from portal import bot_settings as _bs

        mode, lang = await _bs.get_settings(db_path, tg_id)
    except Exception:  # noqa: BLE001
        mode, lang = "novice", "ru"
    try:
        health = float(stats.get("health_factor", 0.0) or 0.0)
    except (TypeError, ValueError):
        health = 0.0
    return {
        "running": bool(stats.get("running")),
        "paused": bool(pool.get("paused")),
        "network": name,
        "chain_id": chain,
        "cycle": pool.get("cycles", 0),
        "cycles_total": None,
        "errors": pool.get("errors", 0),
        "health_factor": health,
        "workers": pool.get("dyn_workers", 0),
        "wallet_count": total,
        "cells": cells,
        "cells_total": total,
        "mode": mode,
        "lang": lang,
        "updated_at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
    }


def _etag_of(payload: dict) -> str:
    slim = {k: v for k, v in payload.items() if k != "updated_at"}
    raw = json.dumps(slim, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:32]


# ---------- хендлеры (cfg/daemon передаются замыканием из register) ----------


def register_miniapp(app: web.Application, *, cfg_key, daemon_key) -> None:
    """Роutes /app/ + /api/miniapp/*. Ключи app — из api.py (избегаем импорта api)."""

    async def _cfg_daemon(request: web.Request) -> tuple[PortalConfig, FarmDaemon]:
        return request.app[cfg_key], request.app[daemon_key]

    async def _auth(request: web.Request) -> tuple[PortalConfig, FarmDaemon, int, str] | None:
        cfg, daemon = await _cfg_daemon(request)
        if not cfg.telegram_token:
            return None
        got = await tma_user(request, cfg)
        if got is None:
            return None
        tg_id, first = got
        return cfg, daemon, tg_id, first

    async def app_page(request: web.Request) -> web.Response:
        from pathlib import Path as _Path

        page = _Path(__file__).parent / "static" / "miniapp" / "app.html"
        return web.Response(text=page.read_text(encoding="utf-8"), content_type="text/html")

    async def api_state(request: web.Request) -> web.Response:
        ctx = await _auth(request)
        if ctx is None:
            return _deny()
        cfg, daemon, tg_id, _first = ctx
        payload = await _state_payload(daemon, tg_id, getattr(daemon, "db_path", None))
        tag = _etag_of(payload)
        if request.headers.get("If-None-Match") == tag:
            return web.Response(status=304)
        resp = web.json_response(payload)
        resp.headers["ETag"] = tag
        return resp

    async def api_history(request: web.Request) -> web.Response:
        ctx = await _auth(request)
        if ctx is None:
            return _deny()
        _cfg, daemon, _tg, _f = ctx
        try:
            limit = int(request.query.get("limit", "20"))
        except ValueError:
            limit = 20
        limit = max(1, min(limit, 50))
        return web.json_response({"history": await daemon.cycle_history(limit)})

    async def api_networks(request: web.Request) -> web.Response:
        ctx = await _auth(request)
        if ctx is None:
            return _deny()
        from portal.networks import NETWORKS

        nets = [
            {k: n.get(k) for k in ("slug", "name", "chain_id", "currency", "tagline")}
            for n in NETWORKS
        ]
        return web.json_response({
            "networks": nets,
            "presets": {
                "wallets": [10, 50, 200],
                "cycles": [2, 5, 0],
                "profiles": ["quiet", "normal", "fast"],
            },
        })

    @idempotent("miniapp:farm")
    async def api_farm(request: web.Request) -> web.Response:
        ctx = await _auth(request)
        if ctx is None:
            return _deny()
        cfg, daemon, tg_id, _f = ctx
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "bad request"}, status=400)
        action = str(body.get("action", ""))
        if action not in {"start", "pause", "resume", "stop"}:
            return web.json_response({"error": "unknown"}, status=404)
        if not _mut_ok(tg_id, "farm"):
            return web.json_response({"error": "rate"}, status=429)
        logger.info("[AUDIT] miniapp farm user=%s action=%s", tg_id, action)
        if action == "stop" and daemon.running:
            task = asyncio.get_running_loop().create_task(daemon.stop())

            def _done(t: asyncio.Task) -> None:
                try:
                    t.result()
                except Exception:  # noqa: BLE001
                    logger.exception("[AUDIT] miniapp stop task failed")

            task.add_done_callback(_done)
            return web.json_response({"ok": True, "stopping": True, **daemon.run_state()})
        ok = await {"start": daemon.start, "stop": daemon.stop,
                    "pause": daemon.pause, "resume": daemon.resume}[action]()
        return web.json_response({"ok": ok, **daemon.run_state()})

    async def api_command(request: web.Request) -> web.Response:
        ctx = await _auth(request)
        if ctx is None:
            return _deny()
        cfg, daemon, tg_id, _f = ctx
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "bad request"}, status=400)
        kind = str(body.get("kind", "cmd"))
        if not _mut_ok(tg_id, "command"):
            return web.json_response({"error": "rate"}, status=429)
        # Тот же код и валидатор, что у /cmd /cfg /check бота (п.2 ТЗ).
        try:
            if kind == "cmd":
                slug = str(body.get("net", "vibevibe"))
                wallets = int(body.get("wallets", 50))
                cycles = int(body.get("cycles", 5))
                cmd = _bot_ops.build_command(
                    slug, wallets, cycles, not bool(body.get("faucet", True)))
                cmd = _bot_ops.apply_profile_flags(cmd, str(body.get("profile", "normal")))
                return web.json_response({"ok": True, "cmd": cmd})
            if kind in {"cfg", "cfg_send"}:
                slug = str(body.get("net", "vibevibe"))
                wallets = int(body.get("wallets", 50))
                filename, content = _bot_ops.build_config_yaml(slug, wallets)
                if kind == "cfg_send":
                    from aiogram import Bot
                    from aiogram.types import BufferedInputFile

                    bot = Bot(token=cfg.telegram_token)
                    async with bot:
                        await bot.send_document(
                            chat_id=tg_id,
                            document=BufferedInputFile(
                                content.encode("utf-8"), filename=filename),
                            caption="Mini App: готовый YAML.",
                        )
                    logger.info("[AUDIT] miniapp cfg_send user=%s net=%s", tg_id, slug)
                    return web.json_response({"ok": True, "sent": "chat"})
                return web.json_response({"ok": True, "filename": filename, "yaml": content})
            if kind == "check":
                ok, lines = _bot_ops.check_config_text(str(body.get("yaml", "")))
                return web.json_response({"ok": ok, "lines": lines})
        except (ValueError, TypeError) as e:
            return web.json_response({"error": str(e)[:200]}, status=400)
        return web.json_response({"error": "unknown"}, status=404)

    async def api_export_token(request: web.Request) -> web.Response:
        ctx = await _auth(request)
        if ctx is None:
            return _deny()
        cfg, _daemon, tg_id, _f = ctx
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        fmt = str(body.get("format", "csv"))
        if fmt not in {"csv", "json"}:
            return web.json_response({"error": "bad format"}, status=400)
        if not _mut_ok(tg_id, "export_token"):
            return web.json_response({"error": "rate"}, status=429)
        return web.json_response(
            {"confirm_token": issue_confirm(cfg.secret, tg_id, f"export:{fmt}")})

    @idempotent("miniapp:export")
    async def api_export_request(request: web.Request) -> web.Response:
        ctx = await _auth(request)
        if ctx is None:
            return _deny()
        cfg, daemon, tg_id, _f = ctx
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "bad request"}, status=400)
        fmt = str(body.get("format", "csv"))
        if fmt not in {"csv", "json"}:
            return web.json_response({"error": "bad format"}, status=400)
        if not consume_confirm(cfg.secret, str(body.get("confirm_token", "")), tg_id,
                               f"export:{fmt}"):
            return _deny()
        from portal import bot_telegram as _bt

        ok, _wait = _bt._flood_ok(tg_id, "export")
        if not ok:
            return web.json_response({"error": "cooldown"}, status=429)
        try:
            from aiogram import Bot

            bot = Bot(token=cfg.telegram_token)
            async with bot:
                await _bt.send_wallets_file(bot, tg_id, daemon, fmt)
        except Exception:  # noqa: BLE001
            logger.warning("miniapp export упал", exc_info=True)
            return web.json_response({"error": "failed"}, status=500)
        logger.info("[AUDIT] miniapp export user=%s fmt=%s", tg_id, fmt)
        return web.json_response({"ok": True, "sent": "chat"})

    async def api_settings_get(request: web.Request) -> web.Response:
        ctx = await _auth(request)
        if ctx is None:
            return _deny()
        _cfg, daemon, tg_id, _f = ctx
        try:
            from portal import bot_settings as _bs

            mode, lang = await _bs.get_settings(getattr(daemon, "db_path", None), tg_id)
        except Exception:  # noqa: BLE001
            mode, lang = "novice", "ru"
        return web.json_response({"mode": mode, "lang": lang})

    async def api_settings_set(request: web.Request) -> web.Response:
        ctx = await _auth(request)
        if ctx is None:
            return _deny()
        _cfg, daemon, tg_id, _f = ctx
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "bad request"}, status=400)
        if not _mut_ok(tg_id, "settings"):
            return web.json_response({"error": "rate"}, status=429)
        try:
            from portal import bot_settings as _bs

            db_path = getattr(daemon, "db_path", None)
            mode, lang = await _bs.get_settings(db_path, tg_id)
            if "mode" in body:
                mode = await _bs.set_mode(db_path, tg_id, str(body["mode"]))
            if "lang" in body:
                lang = await _bs.set_lang(db_path, tg_id, str(body["lang"]))
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "failed"}, status=500)
        logger.info("[AUDIT] miniapp settings user=%s mode=%s lang=%s", tg_id, mode, lang)
        return web.json_response({"ok": True, "mode": mode, "lang": lang})

    async def api_scheduler(request: web.Request) -> web.Response:
        """Состояние планировщика (P4: чтение; управление — портал/bot)."""
        ctx = await _auth(request)
        if ctx is None:
            return _deny()
        _cfg, daemon, _tg, _f = ctx
        try:
            state = daemon.scheduler_state()
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "failed"}, status=500)
        if not isinstance(state, dict):
            return web.json_response({"error": "failed"}, status=500)
        return web.json_response(state)

    app.router.add_get("/app/", app_page)
    app.router.add_get("/api/miniapp/state", api_state)
    app.router.add_get("/api/miniapp/history", api_history)
    app.router.add_get("/api/miniapp/scheduler", api_scheduler)
    app.router.add_get("/api/miniapp/networks", api_networks)
    app.router.add_post("/api/miniapp/farm", api_farm)
    app.router.add_post("/api/miniapp/command", api_command)
    app.router.add_post("/api/miniapp/export/token", api_export_token)
    app.router.add_post("/api/miniapp/export/request", api_export_request)
    app.router.add_get("/api/miniapp/settings", api_settings_get)
    app.router.add_post("/api/miniapp/settings", api_settings_set)
