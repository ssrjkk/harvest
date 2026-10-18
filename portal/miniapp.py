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
    if len(_choke) > 20_000:
        for kk in [kk for kk, v in _choke.items() if not any(now - t < _CHOKE_WINDOW for t in v)]:
            _choke.pop(kk, None)
    return True


def _gchoke_ok(bucket: str) -> bool:
    now = time.time()
    rec = _gchoke.setdefault(bucket, [])
    rec[:] = [t for t in rec if now - t < _GCHOKE_WINDOW]
    if len(rec) >= _GCHOKE_LIMIT:
        return False
    rec.append(now)
    if len(_gchoke) > 5_000:
        for kk in [kk for kk, v in list(_gchoke.items()) if not any(now - t < _GCHOKE_WINDOW for t in v)]:
            _gchoke.pop(kk, None)
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
_confirm_used: dict[str, float] = {}


def issue_confirm(secret: str, tg_id: int, action: str) -> str:
    """Токен на опасное действие: HMAC-подпись + jti, одноразовый."""
    return auth.sign_token(secret, f"tma-confirm:{tg_id}:{action}", "", ttl=int(_CONFIRM_TTL))


def _prune_confirms(now: float) -> None:
    # Чистим только просроченные (TTL 5 мин + запас), никогда clear-all.
    cutoff = now - (_CONFIRM_TTL + 60.0)
    for tok in [t for t, ts in _confirm_used.items() if ts < cutoff]:
        _confirm_used.pop(tok, None)
    # Аварийный клапан: если всё равно >10k (атака), удаляем самые старые.
    if len(_confirm_used) > 10_000:
        for tok, _ts in sorted(_confirm_used.items(), key=lambda kv: kv[1])[: len(_confirm_used) - 10_000]:
            _confirm_used.pop(tok, None)


def consume_confirm(secret: str, token: str, tg_id: int, action: str) -> bool:
    now = time.time()
    if not token or token in _confirm_used:
        return False
    payload = auth.read_token(secret, token)
    if payload is None:
        return False
    if payload.get("sub") != f"tma-confirm:{tg_id}:{action}":
        return False
    _confirm_used[token] = now
    _prune_confirms(now)
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
        except (TypeError, ValueError):
            return web.json_response({"error": "bad limit"}, status=400)
        if not 1 <= limit <= 50:
            return web.json_response({"error": "bad limit"}, status=400)
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
            return web.json_response({"error": "unknown action"}, status=400)
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
                try:
                    wallets = int(body.get("wallets", 50))
                    cycles = int(body.get("cycles", 5))
                except (TypeError, ValueError):
                    return web.json_response({"error": "bad numbers"}, status=400)
                cmd = _bot_ops.build_command(
                    slug, wallets, cycles, not bool(body.get("faucet", True)))
                cmd = _bot_ops.apply_profile_flags(cmd, str(body.get("profile", "normal")))
                return web.json_response({"ok": True, "cmd": cmd})
            if kind in {"cfg", "cfg_send"}:
                slug = str(body.get("net", "vibevibe"))
                try:
                    wallets = int(body.get("wallets", 50))
                except (TypeError, ValueError):
                    return web.json_response({"error": "bad numbers"}, status=400)
                filename, content = _bot_ops.build_config_yaml(slug, wallets)
                if kind == "cfg_send":
                    from aiogram import Bot
                    from aiogram.types import BufferedInputFile

                    bot = Bot(token=cfg.telegram_token)
                    try:
                        async with bot:
                            await bot.send_document(
                                chat_id=tg_id,
                                document=BufferedInputFile(
                                    content.encode("utf-8"), filename=filename),
                                caption="Mini App: готовый YAML.",
                            )
                    except Exception:  # noqa: BLE001
                        logger.warning("miniapp cfg_send упал", exc_info=True)
                        return web.json_response({"error": "failed"}, status=500)
                    logger.info("[AUDIT] miniapp cfg_send user=%s net=%s", tg_id, slug)
                    return web.json_response({"ok": True, "sent": "chat"})
                return web.json_response({"ok": True, "filename": filename, "yaml": content})
            if kind == "check":
                ok, lines = _bot_ops.check_config_text(str(body.get("yaml", "")))
                return web.json_response({"ok": ok, "lines": lines})
        except (ValueError, TypeError) as e:
            return web.json_response({"error": str(e)[:200]}, status=400)
        return web.json_response({"error": "unknown kind"}, status=400)

    _EXPORT_FORMATS = {"csv", "json", "enc-csv", "enc-json"}

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
        if fmt not in _EXPORT_FORMATS:
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
        if fmt not in _EXPORT_FORMATS:
            return web.json_response({"error": "bad format"}, status=400)
        if not consume_confirm(cfg.secret, str(body.get("confirm_token", "")), tg_id,
                               f"export:{fmt}"):
            return _deny()
        from portal import bot_telegram as _bt

        ok, _wait = _bt._flood_ok(tg_id, "export")
        if not ok:
            return web.json_response({"error": "cooldown"}, status=429)
        # Шифрованный вариант: пароль — только в памяти, в логи не пишем
        # (тот же trust, что у EncWiz бота: сервер шифрует в tmp и шлёт .enc
        # в чат с protect + автоудалением).
        password = None
        if fmt.startswith("enc-"):
            password = body.get("password", "")
            if not isinstance(password, str) or not 4 <= len(password) <= 256:
                return web.json_response({"error": "bad password"}, status=400)
            fmt = fmt[4:]
        try:
            from aiogram import Bot

            bot = Bot(token=cfg.telegram_token)
            async with bot:
                if password is None:
                    await _bt.send_wallets_file(bot, tg_id, daemon, fmt)
                else:
                    await _bt._send_encrypted(bot, tg_id, daemon, fmt, password)
        except Exception:  # noqa: BLE001
            logger.warning("miniapp export упал", exc_info=True)
            return web.json_response({"error": "failed"}, status=500)
        logger.info("[AUDIT] miniapp export user=%s fmt=%s", tg_id, fmt if password is None else f"enc-{fmt}")
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

    async def api_alerts_get(request: web.Request) -> web.Response:
        ctx = await _auth(request)
        if ctx is None:
            return _deny()
        _cfg, daemon, _tg, _f = ctx
        try:
            return web.json_response(daemon.balance_alerts_state())
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "failed"}, status=500)

    async def api_alerts_set(request: web.Request) -> web.Response:
        ctx = await _auth(request)
        if ctx is None:
            return _deny()
        _cfg, daemon, tg_id, _f = ctx
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "bad request"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"error": "bad request"}, status=400)
        if not _mut_ok(tg_id, "alerts"):
            return web.json_response({"error": "rate"}, status=429)
        try:
            threshold = float(body.get("threshold", 0.001))
        except (TypeError, ValueError):
            return web.json_response({"error": "bad threshold"}, status=400)
        if not 0 <= threshold <= 100:
            return web.json_response({"error": "bad threshold"}, status=400)
        enabled = bool(body.get("enabled", True))
        if str(body.get("enabled", "")).lower() in {"off", "0", "false", "disable"}:
            enabled = False
        try:
            res = await daemon.set_balance_alerts(enabled, threshold)
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "failed"}, status=500)
        logger.info("[AUDIT] miniapp alerts user=%s enabled=%s threshold=%s", tg_id, enabled, threshold)
        return web.json_response({"ok": True, **res})

    async def api_rpc(request: web.Request) -> web.Response:
        ctx = await _auth(request)
        if ctx is None:
            return _deny()
        _cfg, daemon, _tg, _f = ctx
        try:
            return web.json_response(await daemon.rpc_health())
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "failed"}, status=500)

    async def api_faucet(request: web.Request) -> web.Response:
        ctx = await _auth(request)
        if ctx is None:
            return _deny()
        _cfg, daemon, tg_id, _f = ctx
        if not _mut_ok(tg_id, "faucet"):
            return web.json_response({"error": "rate"}, status=429)
        try:
            report = await daemon.faucet_report()
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "failed"}, status=500)
        live = sum(1 for p in report if isinstance(p, dict) and p.get("alive"))
        return web.json_response({"ok": True, "live": live, "strategies": report,
                                  "drip": daemon.drip_state()})

    async def api_faucet_drip(request: web.Request) -> web.Response:
        ctx = await _auth(request)
        if ctx is None:
            return _deny()
        _cfg, daemon, tg_id, _f = ctx
        if not _mut_ok(tg_id, "faucet"):
            return web.json_response({"error": "rate"}, status=429)
        try:
            res = await daemon.drip_faucet()
        except RuntimeError as e:
            return web.json_response({"error": str(e)}, status=503)
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "failed"}, status=500)
        logger.info("[AUDIT] miniapp drip user=%s started=%s", tg_id, res.get("started"))
        return web.json_response(res, status=202 if res.get("started") else 409)

    async def api_net_switch(request: web.Request) -> web.Response:
        ctx = await _auth(request)
        if ctx is None:
            return _deny()
        _cfg, daemon, tg_id, _f = ctx
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "bad request"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"error": "bad request"}, status=400)
        slug = str(body.get("slug", "")).strip()[:64]
        if not slug:
            return web.json_response({"error": "bad slug"}, status=400)
        if not _mut_ok(tg_id, "net_switch"):
            return web.json_response({"error": "rate"}, status=429)
        try:
            ok = await daemon.switch_to(slug)
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "failed"}, status=500)
        if not ok:
            return web.json_response({"error": "unknown network"}, status=404)
        logger.info("[AUDIT] miniapp net_switch user=%s slug=%s", tg_id, slug)
        return web.json_response({"ok": True, **daemon.run_state()})

    app.router.add_get("/app/", app_page)
    app.router.add_get("/api/miniapp/state", api_state)
    app.router.add_get("/api/miniapp/history", api_history)
    app.router.add_get("/api/miniapp/scheduler", api_scheduler)
    app.router.add_get("/api/miniapp/networks", api_networks)
    app.router.add_get("/api/miniapp/alerts", api_alerts_get)
    app.router.add_post("/api/miniapp/alerts", api_alerts_set)
    app.router.add_get("/api/miniapp/rpc", api_rpc)
    app.router.add_get("/api/miniapp/faucet", api_faucet)
    app.router.add_post("/api/miniapp/faucet/drip", api_faucet_drip)
    app.router.add_post("/api/miniapp/network/switch", api_net_switch)
    app.router.add_post("/api/miniapp/farm", api_farm)
    app.router.add_post("/api/miniapp/command", api_command)
    app.router.add_post("/api/miniapp/export/token", api_export_token)
    app.router.add_post("/api/miniapp/export/request", api_export_request)
    app.router.add_get("/api/miniapp/settings", api_settings_get)
    app.router.add_post("/api/miniapp/settings", api_settings_set)
