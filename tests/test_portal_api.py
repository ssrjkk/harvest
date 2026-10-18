"""Офлайн end-to-end тесты HTTP-API портала (aiohttp TestClient, без сети)."""

import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from typing import cast
from urllib.parse import parse_qs, urlparse

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from yarl import URL

from portal import api, auth
from portal.api import create_app
from portal.config import PortalConfig
from portal.farm import FarmDaemon


class _StubDaemon:
    running = True

    def __init__(self) -> None:
        self.metrics_limits: list[int] = []
        self.groups: list[dict] = [{"id": 1, "name": "main", "color": "#007bff", "count": 0}]
        self.switched: list[str] = []
        self.scheduler_tasks: dict[str, dict] = {}
        self.scheduler_running = False
        self.balance = {"enabled": False, "threshold": 0.001}

    def run_state(self) -> dict:
        return {"running": True, "paused": False}

    async def statistics(self) -> dict:
        return {
            "running": True,
            "pool": {"actions": 7, "errors": 0},
            "health_factor": 1.0,
            "db": {"cycles": 2},
        }

    async def start(self) -> bool:
        return True

    async def stop(self) -> bool:
        return True

    async def pause(self) -> bool:
        return False

    async def resume(self) -> bool:
        return False

    async def top_wallets(self, limit=8) -> list:
        return [{"address": "0xabc", "actions": 3}]

    async def cycle_history(self, limit=12) -> list:
        return [{"id": 1, "actions_ok": 5, "errors": 0, "duration_s": 10.0, "wallets": 4}]

    async def metrics_history(self, limit: int = 100) -> list:
        self.metrics_limits.append(limit)
        return [{"id": 1, "actions": 3, "errors": 0}]

    def networks_info(self) -> list:
        return [{"slug": "vibevibe", "name": "VibeVibe", "current": True, "running": False}]

    async def switch_to(self, slug: str) -> bool:
        self.switched.append(slug)
        return slug == "vibevibe"

    async def list_groups(self) -> list:
        return self.groups

    async def create_group(self, name: str, color: str = "#007bff") -> dict:
        if any(g["name"] == name for g in self.groups):
            raise ValueError(f"Group '{name}' already exists")
        group = {"id": len(self.groups) + 1, "name": name, "color": color, "count": 0}
        self.groups.append(group)
        return group

    async def delete_group(self, group_id: int) -> bool:
        before = len(self.groups)
        self.groups = [g for g in self.groups if g["id"] != group_id]
        return len(self.groups) < before

    async def add_wallet_to_group(self, address: str, group_id: int) -> bool:
        return group_id == 1

    async def remove_wallet_from_group(self, address: str, group_id: int) -> bool:
        return group_id == 1

    def scheduler_state(self) -> dict:
        return {
            "initialized": True,
            "running": self.scheduler_running,
            "tasks": list(self.scheduler_tasks.values()),
        }

    async def add_scheduler_task(self, task_id: str, **kwargs) -> dict:
        task = {"task_id": task_id, "cycles": kwargs.get("cycles", 1)}
        task.update({k: v for k, v in kwargs.items() if k not in task})
        self.scheduler_tasks[task_id] = task
        return task

    async def update_scheduler_task(self, task_id: str, **kwargs) -> dict | None:
        task = self.scheduler_tasks.get(task_id)
        if task is None:
            return None
        task.update(kwargs)
        return task

    async def remove_scheduler_task(self, task_id: str) -> bool:
        return self.scheduler_tasks.pop(task_id, None) is not None

    async def start_scheduler(self) -> bool:
        self.scheduler_running = True
        return True

    async def stop_scheduler(self) -> bool:
        self.scheduler_running = False
        return True

    def balance_alerts_state(self) -> dict:
        return dict(self.balance)

    async def set_balance_alerts(self, enabled: bool, threshold: float) -> dict:
        self.balance = {"enabled": enabled, "threshold": max(0.0, threshold)}
        return dict(self.balance)

    async def rpc_health(self) -> dict:
        return {"nodes": [{"url": "https://rpc.test", "latency_ms": 42, "healthy": True}]}

    async def faucet_report(self) -> list:
        return [{"type": "chainstack", "url": "https://mcp.chainstack.com/mcp",
                 "alive": True, "reason": "MCP handshake ok"}]

    def drip_state(self) -> dict:
        return {"running": False, "at": None, "funded": 0, "failed": 0}

    async def drip_faucet(self) -> dict:
        st = self.drip_state()
        st["started"] = True
        return st

    async def create_wallets(self, count: int) -> list:
        return [{"address": f"0x{i:040x}"} for i in range(max(1, min(int(count), 50)))]

    async def export_wallets(self, fmt: str = "csv") -> tuple:
        if fmt == "json":
            return ('[{"address": "0x1"}]', "wallets.json")
        return ("address,private_key\n0x1,abc\n", "wallets.csv")

    async def wallet_cells(self) -> tuple:
        return ("o", 1)

    async def wallet_balances(self, limit: int = 20) -> list:
        return [{"address": "0xabc", "balance": 1.0}][: max(1, min(int(limit), 50))]

    async def wallet_count(self) -> int:
        return 1

    async def all_wallets(self) -> list:
        return [{"address": "0xabc", "private_key": "k", "total_actions": 1}]

    async def _check_faucet_once(self) -> int:
        return 1


def _make_cfg(
    public_base_url: str = "",
    *,
    allow_insecure: bool = True,
    cookie_secure: bool = False,
    password: bool = True,
    trust_proxy: bool = False,
) -> PortalConfig:
    prev = {
        k: os.environ.get(k)
        for k in (
            "PORTAL_SECRET",
            "FARMER_MASTER_KEY",
            "PORTAL_COOKIE_SECURE",
            "PORTAL_TRUST_PROXY",
            "TELEGRAM_BOT_TOKEN",
            "TELEGRAM_ALLOW_IDS",
            "GOOGLE_CLIENT_ID",
            "GOOGLE_CLIENT_SECRET",
            "GOOGLE_ALLOW_EMAILS",
            "GOOGLE_REDIRECT_URI",
            "PORTAL_OPEN_ACCESS",
            "PORTAL_PASSWORD_LOGIN",
            "PORTAL_ALLOW_PASSWORD_HTTP",
            "PORTAL_BASE_URL",
        )
    }
    os.environ["PORTAL_SECRET"] = "s" * 32
    os.environ["FARMER_MASTER_KEY"] = "k" * 64
    os.environ.pop("PORTAL_COOKIE_SECURE", None)
    os.environ.pop("PORTAL_TRUST_PROXY", None)
    # Тесты портального конфига не должны зависеть от окружения машины
    # (например, от реального TELEGRAM_BOT_TOKEN в shell).
    for _k in ("TELEGRAM_BOT_TOKEN", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"):
        os.environ.pop(_k, None)
    try:
        cfg = PortalConfig()
        cfg.public_base_url = public_base_url
        cfg.allow_insecure_password = allow_insecure
        cfg.cookie_secure = cookie_secure
        cfg.password_login = password
        cfg.trust_proxy = trust_proxy
        return cfg
    finally:
        for k, v in prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class PortalApiTest(unittest.TestCase):
    def setUp(self) -> None:
        api._login_fails.clear()
        api._global_login_fails.clear()
        api._choke_buckets.clear()
        api._global_choke.clear()
        # Изолируем персист-файл отзыва сессий: тесты не пишут в рабочую папку.
        self._td = tempfile.TemporaryDirectory()
        api.REVOKED_FILE = str(Path(self._td.name) / "revoked.json")
        api._revoked = None

    def tearDown(self) -> None:
        self._td.cleanup()

    async def _client(self, base: str = "", **kwargs) -> tuple[TestClient, web.Application]:
        cfg = _make_cfg(base, **kwargs)
        app = create_app(cfg, cast(FarmDaemon, _StubDaemon()))
        client = TestClient(TestServer(app))
        await client.start_server()
        return client, app


class TestAuthFlow(PortalApiTest, unittest.IsolatedAsyncioTestCase):
    async def test_unauthed_stats_401(self):
        client, _app = await self._client()
        async with client:
            resp = await client.get("/api/stats")
            self.assertEqual(resp.status, 401)

    async def test_password_login_and_access(self):
        client, _app = await self._client()
        async with client:
            wrong = await client.post("/api/login/password", json={"password": "bad"})
            self.assertEqual(wrong.status, 401)

            ok = await client.post("/api/login/password", json={"password": "k" * 64})
            self.assertEqual(ok.status, 200)

            stats = await client.get("/api/stats")
            self.assertEqual(stats.status, 200)
            body = await stats.json()
            self.assertTrue(body["running"])
            self.assertEqual(body["pool"]["actions"], 7)

    async def test_farm_action_requires_auth(self):
        client, _app = await self._client()
        async with client:
            resp = await client.post("/api/farm/start")
            self.assertEqual(resp.status, 401)
            await client.post("/api/login/password", json={"password": "k" * 64})
            resp = await client.post("/api/farm/start")
            self.assertEqual(resp.status, 200)
            body = await resp.json()
            self.assertTrue(body["ok"])

    async def test_rate_limit_after_failures(self):
        client, _app = await self._client()
        async with client:
            for _ in range(5):
                resp = await client.post("/api/login/password", json={"password": "bad"})
                self.assertEqual(resp.status, 401)
            resp = await client.post("/api/login/password", json={"password": "bad"})
            self.assertEqual(resp.status, 429)

    async def test_global_rate_limit_ignores_per_ip(self):
        # Распределённый перебор (смена IP) блокируется глобальным счётчиком.
        client, _app = await self._client()
        async with client:
            for _ in range(api._GLOBAL_MAX):
                await client.post("/api/login/password", json={"password": "bad"})
            resp = await client.post("/api/login/password", json={"password": "k" * 64})
            self.assertEqual(resp.status, 429)
            # Правильный пароль тоже подождёт — защита от брутфорса PIN.
            resp = await client.post("/api/login/password", json={"password": "k" * 64})
            self.assertEqual(resp.status, 429)

    async def test_oversized_password_rejected(self):
        # Ограничение длины пароля: огромные значения не принимаются как «верные».
        client, _app = await self._client()
        async with client:
            resp = await client.post("/api/login/password", json={"password": "k" * (api._MAX_PASSWORD_LEN + 10)})
            self.assertEqual(resp.status, 401)

    async def test_password_blocked_over_http_when_insecure_disallowed(self):
        cfg = _make_cfg(allow_insecure=False)
        app = create_app(cfg, cast(FarmDaemon, _StubDaemon()))
        client = TestClient(TestServer(app))
        await client.start_server()
        async with client:
            resp = await client.post("/api/login/password", json={"password": "k" * 64})
            self.assertEqual(resp.status, 403)

    async def test_password_blocked_when_disabled(self):
        cfg = _make_cfg(password=False)
        app = create_app(cfg, cast(FarmDaemon, _StubDaemon()))
        client = TestClient(TestServer(app))
        await client.start_server()
        async with client:
            resp = await client.post("/api/login/password", json={"password": "k" * 64})
            self.assertEqual(resp.status, 403)

    async def test_secure_cookie_uses_host_prefix(self):
        client, _app = await self._client(cookie_secure=True)
        async with client:
            resp = await client.post("/api/login/password", json={"password": "k" * 64})
            self.assertEqual(resp.status, 200)
            set_cookies = resp.headers.getall("Set-Cookie", [])
            self.assertTrue(any(h.startswith("__Host-harvest_session=") for h in set_cookies))
            self.assertFalse(any(h.startswith("harvest_session=") and not h.startswith("__Host-") for h in set_cookies))

    async def test_plain_cookie_rejected_in_secure_mode(self):
        # session cookie в Secure-режиме: plain-кука (без __Host-) НЕ авторизует —
        # downgrade-вектор для снятой с http-зеркала куки.
        cfg = _make_cfg(cookie_secure=True)
        app = create_app(cfg, cast(FarmDaemon, _StubDaemon()))
        client = TestClient(TestServer(app))
        await client.start_server()
        token = auth.sign_token(cfg.secret, "u", "U")
        async with client:
            client.session.cookie_jar.update_cookies({api.COOKIE_NAME: token}, URL("http://127.0.0.1"))
            self.assertEqual((await client.get("/api/stats")).status, 401)

    async def test_plain_cookie_not_used_when_secure_expected(self):
        # Secure-кука не должна отдаваться по http.
        client, _app = await self._client(cookie_secure=True)
        async with client:
            await client.post("/api/login/password", json={"password": "k" * 64})
            jar = client.session.cookie_jar.filter_cookies(URL("http://127.0.0.1"))
            self.assertNotIn(api.COOKIE_NAME, jar)
            self.assertNotIn(api.SECURE_COOKIE_NAME, jar)


class TestGuard(PortalApiTest, unittest.IsolatedAsyncioTestCase):
    async def test_origin_mismatch_blocked(self):
        client, _app = await self._client(base="https://portal.example")
        async with client:
            await client.post("/api/login/password", json={"password": "k" * 64})
            resp = await client.post("/api/farm/start", headers={"Origin": "https://evil.example"})
            self.assertEqual(resp.status, 403)
            # Даже CSRF-403 получает заголовки безопасности (security_headers —
            # внешний middleware): прокси/кэш не увидит ответ без CSP/nosniff.
            self.assertEqual(resp.headers.get("X-Content-Type-Options"), "nosniff")
            csp = resp.headers.get("Content-Security-Policy", "")
            self.assertIn("script-src 'self'", csp)
            self.assertIn("object-src 'none'", csp)

    async def test_origin_same_allowed(self):
        client, _app = await self._client(base="https://portal.example")
        async with client:
            await client.post("/api/login/password", json={"password": "k" * 64})
            resp = await client.post("/api/farm/start", headers={"Origin": "https://portal.example"})
            self.assertEqual(resp.status, 200)

    async def test_origin_mismatch_blocked_without_base_url(self):
        # Fail-closed CSRF даже без PORTAL_BASE_URL: origin запроса сверяем
        # с адресом самого запроса, чужой Origin не пропускаем.
        client, _app = await self._client()
        async with client:
            await client.post("/api/login/password", json={"password": "k" * 64})
            resp = await client.post("/api/farm/start", headers={"Origin": "https://evil.example"})
            self.assertEqual(resp.status, 403)

    async def test_origin_matches_request_host_allowed(self):
        # Без base_url валидный браузерный запрос (Origin = адрес сервера) проходит.
        client, _app = await self._client()
        async with client:
            await client.post("/api/login/password", json={"password": "k" * 64})
            origin = f"http://127.0.0.1:{client.server.port}"
            resp = await client.post("/api/farm/start", headers={"Origin": origin})
            self.assertEqual(resp.status, 200)


class TestMisc(PortalApiTest, unittest.IsolatedAsyncioTestCase):
    async def test_healthz_public_links_default_deny(self):
        client, _app = await self._client()
        async with client:
            self.assertEqual((await client.get("/healthz")).status, 200)
            # default-deny: ссылки владельца анонимам не отдаём
            self.assertEqual((await client.get("/api/links")).status, 401)
            await client.post("/api/login/password", json={"password": "k" * 64})
            resp = await client.get("/api/links")
            self.assertEqual(resp.status, 200)
            body = await resp.json()
            self.assertIn("items", body)

    async def test_healthz_no_farm_state_leak(self):
        # Публичный чек-поинт не раскрывает статус фермера:
        # daemon_running виден только авторизованным через /api/stats.
        client, _app = await self._client()
        async with client:
            body = await (await client.get("/healthz")).json()
            self.assertEqual(body["status"], "ok")
            self.assertIn("ready", body)
            self.assertNotIn("daemon_running", body)

    async def test_api_me_hides_open_access_from_anon(self):
        # open_access анонимам не раскрываем (энумерация конфигурации).
        client, _app = await self._client()
        async with client:
            body = await (await client.get("/api/me")).json()
            self.assertIn("authed", body)
            self.assertNotIn("open_access", body)

    async def test_stats_response_has_no_config_path(self):
        # statistics() не отдаёт имя живого конфига даже авторизованным.
        client, _app = await self._client()
        async with client:
            await client.post("/api/login/password", json={"password": "k" * 64})
            body = await (await client.get("/api/stats")).json()
            self.assertNotIn("config", body)

    async def test_tg_init_choked_by_rate(self):
        # Поток запросов на HMAC-эндпоинт режется per-IP лимитом.
        client, _app = await self._client()
        cfg = _app[api.KEY_CFG]
        cfg.telegram_token = "t:to-template"
        cfg.telegram_allow_ids = [1]
        async with client:
            statuses = []
            for _ in range(api._CHOKE_LIMIT + 1):
                resp = await client.post("/api/tg/init", json={"init_data": "bad&hash=00"})
                statuses.append(resp.status)
            self.assertEqual(statuses[-1], 429)
            self.assertTrue(all(s in (401, 429) for s in statuses))

    async def test_login_invalid_json_400(self):
        client, _app = await self._client()
        async with client:
            resp = await client.post(
                "/api/login/password",
                data=b"{broken",
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(resp.status, 400)

    async def test_tg_init_without_token_rejected(self):
        client, _app = await self._client()
        async with client:
            resp = await client.post("/api/tg/init", json={"init_data": "x=1&hash=000"})
            self.assertEqual(resp.status, 403)

    async def test_tg_init_get_method_not_allowed(self):
        # GET убран: initData (подпись Telegram) не должен попадать в URL/логи.
        client, _app = await self._client()
        async with client:
            resp = await client.get("/api/tg/init?init_data=x=1&hash=000")
            self.assertEqual(resp.status, 405)

    async def test_tg_init_oversized_init_data_rejected(self):
        client, _app = await self._client()
        cfg = _app[api.KEY_CFG]
        cfg.telegram_token = "t:to-template"
        cfg.telegram_allow_ids = [1]
        async with client:
            resp = await client.post("/api/tg/init", json={"init_data": "a" * 20_000})
            self.assertEqual(resp.status, 401)

    async def test_logout_clears_session(self):
        client, _app = await self._client()
        async with client:
            await client.post("/api/login/password", json={"password": "k" * 64})
            self.assertEqual((await client.get("/api/stats")).status, 200)
            await client.post("/api/logout")
            self.assertEqual((await client.get("/api/stats")).status, 401)

    async def test_logout_revokes_session_token(self):
        # Даже если злоумышленник сохранил куку до logout — jti отозван.
        client, _app = await self._client()
        async with client:
            await client.post("/api/login/password", json={"password": "k" * 64})
            jar = client.session.cookie_jar.filter_cookies(URL("http://127.0.0.1"))
            token = jar.get(api.COOKIE_NAME).value
            await client.post("/api/logout")
            client.session.cookie_jar.update_cookies({api.COOKIE_NAME: token}, URL("http://127.0.0.1"))
            self.assertEqual((await client.get("/api/stats")).status, 401)

    async def test_logout_revocation_survives_restart(self):
        # Reстарт портала не должен «воскрешать» куку после logout: список
        # отозванных jti персистится и восстанавливается с диска.
        client, _app = await self._client()
        async with client:
            await client.post("/api/login/password", json={"password": "k" * 64})
            jar = client.session.cookie_jar.filter_cookies(URL("http://127.0.0.1"))
            token = jar.get(api.COOKIE_NAME).value
            await client.post("/api/logout")
            self.assertEqual((await client.get("/api/stats")).status, 401)
        # «Рестарт»: память сбрасывается, файл отзыва остался на диске.
        api._revoked = None
        client2, _app2 = await self._client()
        async with client2:
            client2.session.cookie_jar.update_cookies({api.COOKIE_NAME: token}, URL("http://127.0.0.1"))
            self.assertEqual((await client2.get("/api/stats")).status, 401)

    async def test_csp_header(self):
        client, _app = await self._client()
        async with client:
            resp = await client.get("/")
            csp = resp.headers.get("Content-Security-Policy", "")
            self.assertIn("script-src 'self'", csp)
            self.assertIn("object-src 'none'", csp)

    async def test_login_frame_ancestors_none(self):
        # Страницу входа во фреймы пускать нельзя — анти-clickjacking.
        client, _app = await self._client()
        async with client:
            resp = await client.get("/login")
            csp = resp.headers.get("Content-Security-Policy", "")
            self.assertIn("frame-ancestors 'none'", csp)
            self.assertNotIn("https://t.me", csp)

    async def test_dashboard_frame_ancestors_allow_telegram(self):
        # Mini App живёт во view-webview Telegram: фреймы self + t.me/telegram.me.
        client, _app = await self._client()
        async with client:
            resp = await client.get("/")
            csp = resp.headers.get("Content-Security-Policy", "")
            self.assertIn("frame-ancestors 'self' https://t.me https://telegram.me", csp)
            self.assertNotIn("frame-ancestors 'none'", csp)

    async def test_unhandled_exception_returns_json_no_leak(self):
        # Неожиданное исключение: клиенту JSON без внутренних деталей, трейс — в журнал.
        cfg = _make_cfg()

        async def _boom(_request: web.Request) -> web.Response:
            raise RuntimeError("secret-internal-detail-42")

        app = create_app(cfg, cast(FarmDaemon, _StubDaemon()))
        app.router.add_get("/boom", _boom)
        client = TestClient(TestServer(app))
        await client.start_server()
        async with client:
            with self.assertLogs("portal.api", level="ERROR") as cm:
                resp = await client.get("/boom")
            self.assertEqual(resp.status, 500)
            self.assertEqual(await resp.json(), {"error": "internal error"})
            raw = await resp.text()
            self.assertNotIn("secret-internal-detail-42", raw)
            self.assertNotIn("Traceback", raw)
            self.assertTrue(any("Unhandled error" in line for line in cm.output))
        await client.close()

    async def test_http_exception_not_masked_by_error_handler(self):
        # HTTP-статусы (401/403/404…) не превращаются error_handler'ом в 500.
        cfg = _make_cfg()

        async def _unauth(_request: web.Request) -> web.Response:
            raise web.HTTPUnauthorized()

        app = create_app(cfg, cast(FarmDaemon, _StubDaemon()))
        app.router.add_get("/unauth", _unauth)
        client = TestClient(TestServer(app))
        await client.start_server()
        async with client:
            resp = await client.get("/unauth")
            self.assertEqual(resp.status, 401)
        await client.close()

    async def test_no_hsts_on_plain_http(self):
        # HSTS уместен только при фактическом HTTPS — на plain-HTTP его нет.
        client, _app = await self._client()
        async with client:
            resp = await client.get("/")
            self.assertNotIn("Strict-Transport-Security", resp.headers)

    async def test_hsts_by_trusted_proxy(self):
        # За доверенным прокси (X-Forwarded-Proto: https) HSTS выдаётся,
        # защищая от SSL-stripping и downgrade-кэша браузера.
        client, _app = await self._client(trust_proxy=True)
        async with client:
            resp = await client.get("/", headers={"X-Forwarded-Proto": "https"})
            self.assertEqual(resp.headers.get("Strict-Transport-Security"), "max-age=31536000")

    async def test_no_hsts_when_proxy_not_trusted(self):
        # Без PORTAL_TRUST_PROXY заголовок X-Forwarded-Proto игнорируется.
        client, _app = await self._client()
        async with client:
            resp = await client.get("/", headers={"X-Forwarded-Proto": "https"})
            self.assertNotIn("Strict-Transport-Security", resp.headers)

    async def test_denied_access_audit_logged(self):
        # Default-deny пишет [AUDIT]-строку в журнал — отклонения не проходят молча.
        client, _app = await self._client()
        async with client:
            with self.assertLogs("portal.api", level="WARNING") as cm:
                self.assertEqual((await client.get("/api/stats")).status, 401)
            self.assertTrue(any("[AUDIT] deny unauth" in line for line in cm.output))

    async def test_tg_init_post_rejected_without_token(self):
        client, _app = await self._client()
        async with client:
            resp = await client.post("/api/tg/init", json={"init_data": "x=1&hash=000"})
            self.assertEqual(resp.status, 403)

    async def test_oauth_bad_state_clears_cookie(self):
        client, _app = await self._client()
        async with client:
            resp = await client.get("/auth/google/callback?code=x&state=AAAA", allow_redirects=False)
            self.assertEqual(resp.status, 302)
            set_cookies = resp.headers.getall("Set-Cookie", [])
            self.assertTrue(any(c.lower().startswith("oauth_state") for c in set_cookies))


class TestBindingGuard(unittest.TestCase):
    def test_loopback_detection(self):
        from portal.__main__ import _is_loopback_address

        self.assertTrue(_is_loopback_address("127.0.0.1"))
        self.assertTrue(_is_loopback_address("127.250.1.1"))
        self.assertTrue(_is_loopback_address("::1"))
        self.assertTrue(_is_loopback_address("localhost"))
        self.assertFalse(_is_loopback_address("0.0.0.0"))
        self.assertFalse(_is_loopback_address("192.168.1.10"))
        self.assertFalse(_is_loopback_address("example.com"))


class TestOAuthNonce(PortalApiTest, unittest.IsolatedAsyncioTestCase):
    async def test_google_start_binds_nonce_in_url_and_cookie(self):
        # nonce уезжает в Google и в httponly-куку: в id_token он должен
        # совпасть, иначе профиль отвергается.
        client, _app = await self._client()
        cfg = _app[api.KEY_CFG]
        cfg.google_client_id = "gid.apps.googleusercontent.com"
        cfg.google_client_secret = "gsec"
        cfg.public_base_url = "https://portal.example"
        async with client:
            resp = await client.get("/auth/google", allow_redirects=False)
            self.assertEqual(resp.status, 302)
            qs = parse_qs(urlparse(resp.headers["Location"]).query)
            nonce_url = qs.get("nonce", [""])[0]
            self.assertTrue(nonce_url)
            oauth = [c for c in resp.headers.getall("Set-Cookie", []) if c.lower().startswith("oauth_state")]
            self.assertEqual(len(oauth), 1)
            value = oauth[0].split(";", 1)[0].split("=", 1)[1]
            parts = value.split(".")
            self.assertEqual(len(parts), 3)
            self.assertEqual(parts[2], nonce_url)
            self.assertTrue(all(p for p in parts))


class TestGlobalChoke(unittest.TestCase):
    """Глобальный предохранитель tg/init + google/callback: спуфный XFF не обходит."""

    def setUp(self) -> None:
        api._global_choke.clear()

    def tearDown(self) -> None:
        api._global_choke.clear()

    def test_global_bucket_trips_after_max(self):
        allowed = sum(1 for _ in range(api._CHOKE_GLOBAL_MAX + 5) if api._choke_allowed_global("tg_init"))
        self.assertEqual(allowed, api._CHOKE_GLOBAL_MAX)
        self.assertFalse(api._choke_allowed_global("tg_init"))

    def test_buckets_independent(self):
        for _ in range(api._CHOKE_GLOBAL_MAX):
            api._choke_allowed_global("tg_init")
        # Другой бакет ещё свободен (лимиты не пересекаются).
        self.assertTrue(api._choke_allowed_global("google_cb"))


def _raiser(exc: BaseException):
    """statistics() демона, всегда бросающий exc — для веток ошибок в /ws."""

    async def boom() -> dict:
        raise exc

    return boom


class TestWebSocket(PortalApiTest, unittest.IsolatedAsyncioTestCase):
    """Живая лента /ws: периодический пуш статуса + рассылка событий фермы."""

    async def _login(self, client: TestClient) -> None:
        resp = await client.post("/api/login/password", json={"password": "k" * 64})
        self.assertEqual(resp.status, 200)

    def _fast_push(self) -> None:
        self.addCleanup(setattr, api, "WS_PUSH_S", api.WS_PUSH_S)
        api.WS_PUSH_S = 0.05

    async def _frame(self, ws, want: str) -> dict:
        """Ждёт кадр нужного типа: статус-пуш идёт параллельно с событиями."""
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            msg = await ws.receive(timeout=1.0)
            if msg.type is not web.WSMsgType.TEXT:
                self.fail(f"socket closed before {want!r} frame: {msg.type}")
            frame = json.loads(msg.data)
            if frame.get("type") == want:
                return frame
        self.fail(f"no {want!r} frame within deadline")

    async def test_anonymous_socket_is_refused(self):
        from aiohttp.client_exceptions import WSServerHandshakeError

        client, _app = await self._client()
        async with client:
            with self.assertRaises(WSServerHandshakeError) as cm:
                await client.ws_connect("/ws")
            self.assertEqual(cm.exception.status, 401)

    async def test_status_frames_keep_coming(self):
        # Регрессия на «один кадр и молчание»: дашборд живёт из потока статуса.
        self._fast_push()
        client, _app = await self._client()
        async with client:
            await self._login(client)
            async with client.ws_connect("/ws") as ws:
                for _ in range(3):
                    frame = await self._frame(ws, "status")
                    self.assertEqual(frame["pool"]["actions"], 7)
                    self.assertEqual(frame["db"]["cycles"], 2)

    async def test_clients_set_empties_on_disconnect(self):
        self._fast_push()
        client, app = await self._client()
        async with client:
            await self._login(client)
            async with client.ws_connect("/ws") as ws:
                await self._frame(ws, "status")
                self.assertEqual(len(app[api.KEY_WS_CLIENTS]), 1)
            deadline = time.monotonic() + 1.0
            while app[api.KEY_WS_CLIENTS] and time.monotonic() < deadline:
                await asyncio.sleep(0.02)
            self.assertEqual(len(app[api.KEY_WS_CLIENTS]), 0)

    async def test_farm_action_is_broadcast(self):
        self._fast_push()
        client, _app = await self._client()
        async with client:
            await self._login(client)
            async with client.ws_connect("/ws") as ws:
                resp = await client.post("/api/farm/start")
                self.assertEqual(resp.status, 200)
                frame = await self._frame(ws, "farm")
                self.assertEqual(frame["action"], "start")
                self.assertTrue(frame["ok"])
                self.assertTrue(frame["running"])

    async def test_network_action_is_broadcast(self):
        self._fast_push()
        client, _app = await self._client()
        async with client:
            await self._login(client)
            async with client.ws_connect("/ws") as ws:
                resp = await client.post("/api/farm/network/vibevibe/stop")
                self.assertEqual(resp.status, 200)
                frame = await self._frame(ws, "network")
                self.assertEqual(frame["network"], "vibevibe")
                self.assertEqual(frame["action"], "stop")

    async def test_token_in_query_authenticates(self):
        # В query — только короткий WS-тикет (POST /api/ws/token, 60с):
        # долгоживущая сессия живёт в cookie и в URL не оседает.
        self._fast_push()
        client, app = await self._client()
        async with client:
            await self._login(client)
            resp = await client.post("/api/ws/token")
            self.assertEqual(resp.status, 200)
            token = (await resp.json())["token"]
            async with client.ws_connect("/ws?token=" + token) as ws:
                frame = await self._frame(ws, "status")
                self.assertEqual(frame["pool"]["actions"], 7)

    async def test_session_token_in_query_is_refused(self):
        # Сессия в query отклоняется (401), confirm-токен как cookie — тоже.
        from aiohttp.client_exceptions import WSServerHandshakeError

        client, app = await self._client()
        token = auth.sign_token(app[api.KEY_CFG].secret, "tester", "Тест")
        async with client:
            with self.assertRaises(WSServerHandshakeError) as cm:
                await client.ws_connect("/ws?token=" + token)
            self.assertEqual(cm.exception.status, 401)
            self.assertEqual(len(app[api.KEY_WS_CLIENTS]), 0)

    async def test_confirm_token_is_not_a_session(self):
        client, app = await self._client()
        async with client:
            confirm = auth.sign_token(app[api.KEY_CFG].secret, "tma-confirm:123:export:csv", "", ttl=300)
            client.session.cookie_jar.update_cookies(
                {"harvest_session": confirm}, URL("http://127.0.0.1"))
            resp = await client.get("/api/stats")
            self.assertEqual(resp.status, 401)

    async def test_broken_token_in_query_is_refused(self):
        from aiohttp.client_exceptions import WSServerHandshakeError

        client, app = await self._client()
        async with client:
            with self.assertRaises(WSServerHandshakeError) as cm:
                await client.ws_connect("/ws?token=%25%25not-a-token")
            self.assertEqual(cm.exception.status, 401)
            self.assertEqual(len(app[api.KEY_WS_CLIENTS]), 0)

    async def _expect_socket_death(self, exc: BaseException) -> tuple[set, int]:
        """Открывает сокет демоном, статистика которого бросает exc."""
        client, app = await self._client()
        app[api.KEY_DAEMON].statistics = _raiser(exc)
        async with client:
            await self._login(client)
            async with client.ws_connect("/ws") as ws:
                msg = await ws.receive(timeout=2.0)
        return app[api.KEY_WS_CLIENTS], msg.type

    async def test_connection_reset_ends_socket_and_cleans_up(self):
        clients, msg_type = await self._expect_socket_death(ConnectionResetError("gone"))
        self.assertIs(msg_type, web.WSMsgType.CLOSE)
        self.assertEqual(len(clients), 0)

    async def test_runtime_error_ends_socket_and_cleans_up(self):
        clients, msg_type = await self._expect_socket_death(RuntimeError("pool exploded"))
        self.assertIs(msg_type, web.WSMsgType.CLOSE)
        self.assertEqual(len(clients), 0)

    async def test_unexpected_error_is_logged_not_raised(self):
        # Неожиданная ошибка пула не должна уходить в aiohttp-трейс: handler
        # ловит её сам, логует и корректно закрывает сокет.
        client, app = await self._client()
        app[api.KEY_DAEMON].statistics = _raiser(ValueError("bad stats"))
        async with client:
            await self._login(client)
            with self.assertLogs("portal.api", level="WARNING") as cm:
                async with client.ws_connect("/ws") as ws:
                    msg = await ws.receive(timeout=2.0)
        self.assertIs(msg.type, web.WSMsgType.CLOSE)
        self.assertTrue(any("[WS] exception" in line for line in cm.output))
        self.assertEqual(len(app[api.KEY_WS_CLIENTS]), 0)

    async def test_broadcast_drops_dead_clients(self):
        class _Dead:
            def __init__(self) -> None:
                self.sent = 0

            async def send_str(self, _msg: str) -> None:
                self.sent += 1
                raise ConnectionResetError("socket gone")

        self._fast_push()
        client, app = await self._client()
        async with client:
            await self._login(client)
            async with client.ws_connect("/ws") as ws:
                await self._frame(ws, "status")
                dead = _Dead()
                app[api.KEY_WS_CLIENTS].add(dead)
                await api.broadcast_ws(app, "farm", {"action": "start", "ok": True})
                self.assertEqual(dead.sent, 1)
                self.assertNotIn(dead, app[api.KEY_WS_CLIENTS])
                # Живой клиент событие получил.
                frame = await self._frame(ws, "farm")
                self.assertEqual(frame["action"], "start")
                self.assertTrue(frame["ok"])


_BAD_JSON = {"data": "{", "headers": {"Content-Type": "application/json"}}


class TestNetworkRoutes(PortalApiTest, unittest.IsolatedAsyncioTestCase):
    """Каталог сетей и «переключить+запустить» одной кнопкой."""

    async def _login(self, client: TestClient) -> None:
        self.assertEqual((await client.post("/api/login/password", json={"password": "k" * 64})).status, 200)

    async def test_anonymous_networks_401(self):
        client, _app = await self._client()
        async with client:
            self.assertEqual((await client.get("/api/networks")).status, 401)
            self.assertEqual((await client.post("/api/farm/network/vibevibe/start")).status, 401)

    async def test_networks_catalog(self):
        client, app = await self._client()
        async with client:
            await self._login(client)
            body = await (await client.get("/api/networks")).json()
            self.assertEqual(body["networks"], app[api.KEY_DAEMON].networks_info())
            self.assertTrue(body["networks"][0]["current"])

    async def test_network_action_unknown_verb_400(self):
        client, _app = await self._client()
        async with client:
            await self._login(client)
            resp = await client.post("/api/farm/network/vibevibe/restart")
            self.assertEqual(resp.status, 400)
            self.assertEqual(await resp.json(), {"error": "unknown action"})

    async def test_network_start_rejects_unknown_network(self):
        client, app = await self._client()
        async with client:
            await self._login(client)
            resp = await client.post("/api/farm/network/nosuch/start")
            self.assertEqual(resp.status, 404)
            self.assertEqual(await resp.json(), {"error": "unknown network"})
            self.assertEqual(app[api.KEY_DAEMON].switched, ["nosuch"])

    async def test_network_start_and_stop(self):
        client, app = await self._client()
        async with client:
            await self._login(client)
            started = await (await client.post("/api/farm/network/vibevibe/start")).json()
            self.assertTrue(started["ok"])
            self.assertEqual(started["network"], "vibevibe")
            stopped = await (await client.post("/api/farm/network/vibevibe/stop")).json()
            self.assertTrue(stopped["ok"])
            self.assertEqual(app[api.KEY_DAEMON].switched, ["vibevibe"])


class TestMetricsEndpoint(PortalApiTest, unittest.IsolatedAsyncioTestCase):
    """limit временного ряда валидируется: мусор в query → 400, а не молча clamp."""

    async def _login(self, client: TestClient) -> None:
        self.assertEqual((await client.post("/api/login/password", json={"password": "k" * 64})).status, 200)

    async def test_limit_validated(self):
        client, app = await self._client()
        async with client:
            await self._login(client)
            daemon = app[api.KEY_DAEMON]
            resp = await client.get("/api/metrics")
            self.assertEqual(resp.status, 200)
            for query in ("?limit=99999", "?limit=0", "?limit=abc"):
                resp = await client.get("/api/metrics" + query)
                self.assertEqual(resp.status, 400, query)
            self.assertEqual(daemon.metrics_limits, [100])

    async def test_payload_shape(self):
        client, _app = await self._client()
        async with client:
            await self._login(client)
            body = await (await client.get("/api/metrics?limit=5")).json()
            self.assertEqual(body, {"metrics": [{"id": 1, "actions": 3, "errors": 0}]})


class TestGroupsEndpoints(PortalApiTest, unittest.IsolatedAsyncioTestCase):
    """CRUD групп кошельков: валидация тела, конфликт имени, привязка адресов."""

    async def _login(self, client: TestClient) -> None:
        self.assertEqual((await client.post("/api/login/password", json={"password": "k" * 64})).status, 200)

    async def test_anonymous_groups_401(self):
        client, _app = await self._client()
        async with client:
            self.assertEqual((await client.get("/api/groups")).status, 401)
            self.assertEqual((await client.post("/api/groups", json={"name": "x"})).status, 401)
            self.assertEqual((await client.delete("/api/groups/1")).status, 401)
            resp = await client.post("/api/groups/wallet", json={"address": "0x1", "group_id": 1})
            self.assertEqual(resp.status, 401)
            self.assertEqual((await client.delete("/api/groups/1/wallet/0x1")).status, 401)

    async def test_list_groups(self):
        client, _app = await self._client()
        async with client:
            await self._login(client)
            body = await (await client.get("/api/groups")).json()
            self.assertEqual(body, {"groups": [{"id": 1, "name": "main", "color": "#007bff", "count": 0}]})

    async def test_create_group_ok_and_conflict(self):
        client, _app = await self._client()
        async with client:
            await self._login(client)
            resp = await client.post("/api/groups", json={"name": "whales", "color": "#ff0000"})
            self.assertEqual(resp.status, 200)
            body = await resp.json()
            self.assertTrue(body["ok"])
            self.assertEqual(body["group"]["name"], "whales")
            self.assertEqual(body["group"]["color"], "#ff0000")

            dup = await client.post("/api/groups", json={"name": "whales"})
            self.assertEqual(dup.status, 409)
            self.assertIn("already exists", (await dup.json())["error"])

    async def test_create_group_validation(self):
        client, _app = await self._client()
        async with client:
            await self._login(client)
            bad_json = await client.post("/api/groups", **_BAD_JSON)
            self.assertEqual(bad_json.status, 400)
            self.assertEqual(await bad_json.json(), {"error": "ожидается application/json"})
            for payload in ({"name": ""}, {"name": "   "}, {"name": "n" * 101}, {}):
                resp = await client.post("/api/groups", json=payload)
                self.assertEqual(resp.status, 400, payload)
                self.assertEqual(await resp.json(), {"error": "name required (max 100 chars)"})

    async def test_create_group_sanitize_rejects(self):
        # Null-байты и эмодзи-спам в именах — 400 через shield.sanitize.
        client, _app = await self._client()
        async with client:
            await self._login(client)
            nul = await client.post("/api/groups", json={"name": "ab\x00cd"})
            self.assertEqual(nul.status, 400)
            self.assertIn("bad input", (await nul.json())["error"])
            spam = await client.post("/api/groups", json={"name": "🚀" * 20})
            self.assertEqual(spam.status, 400)

    async def test_delete_group(self):
        client, _app = await self._client()
        async with client:
            await self._login(client)
            self.assertEqual(await (await client.delete("/api/groups/1")).json(), {"ok": True})
            self.assertEqual(await (await client.delete("/api/groups/77")).json(), {"ok": False})
            bad = await client.delete("/api/groups/not-an-id")
            self.assertEqual(bad.status, 400)
            self.assertEqual(await bad.json(), {"error": "invalid group id"})

    async def test_add_wallet_to_group(self):
        client, _app = await self._client()
        async with client:
            await self._login(client)
            added = await client.post("/api/groups/wallet", json={"address": "0xaaa", "group_id": 1})
            self.assertEqual(await added.json(), {"ok": True})
            ghost = await client.post("/api/groups/wallet", json={"address": "0xaaa", "group_id": 9})
            self.assertEqual(await ghost.json(), {"ok": False})
            self.assertEqual((await client.post("/api/groups/wallet", **_BAD_JSON)).status, 400)
            for payload in ({"address": "0xaaa"}, {"group_id": 1}, {"address": " ", "group_id": 1}, {}):
                self.assertEqual((await client.post("/api/groups/wallet", json=payload)).status, 400, payload)
            bad_gid = await client.post("/api/groups/wallet", json={"address": "0xaaa", "group_id": "abc"})
            self.assertEqual(bad_gid.status, 400)
            self.assertEqual(await bad_gid.json(), {"error": "invalid group_id"})

    async def test_remove_wallet_from_group(self):
        client, _app = await self._client()
        async with client:
            await self._login(client)
            self.assertEqual(await (await client.delete("/api/groups/1/wallet/0xaaa")).json(), {"ok": True})
            self.assertEqual(await (await client.delete("/api/groups/5/wallet/0xaaa")).json(), {"ok": False})
            bad = await client.delete("/api/groups/abc/wallet/0xaaa")
            self.assertEqual(bad.status, 400)
            self.assertEqual(await bad.json(), {"error": "invalid group id"})


class TestSchedulerEndpoints(PortalApiTest, unittest.IsolatedAsyncioTestCase):
    """Планировщик: состояние, задачи, старт/стоп; cycles валидируется на входе."""

    async def _login(self, client: TestClient) -> None:
        self.assertEqual((await client.post("/api/login/password", json={"password": "k" * 64})).status, 200)

    async def test_anonymous_scheduler_401(self):
        client, _app = await self._client()
        async with client:
            self.assertEqual((await client.get("/api/scheduler")).status, 401)
            self.assertEqual((await client.post("/api/scheduler/tasks", json={"task_id": "t"})).status, 401)
            self.assertEqual((await client.put("/api/scheduler/tasks/t", json={})).status, 401)
            self.assertEqual((await client.delete("/api/scheduler/tasks/t")).status, 401)
            self.assertEqual((await client.post("/api/scheduler/start")).status, 401)
            self.assertEqual((await client.post("/api/scheduler/stop")).status, 401)

    async def test_add_update_delete_and_state(self):
        client, _app = await self._client()
        async with client:
            await self._login(client)
            empty = await (await client.get("/api/scheduler")).json()
            self.assertEqual(empty, {"initialized": True, "running": False, "tasks": []})

            added = await client.post(
                "/api/scheduler/tasks",
                json={"task_id": "nightly", "name": "Ночной цикл", "interval_hours": 8, "cycles": 3},
            )
            self.assertEqual(added.status, 200)
            body = await added.json()
            self.assertTrue(body["ok"])
            self.assertEqual(body["task"]["task_id"], "nightly")
            self.assertEqual(body["task"]["cycles"], 3)

            updated = await client.put(
                "/api/scheduler/tasks/nightly",
                json={
                    "name": "  Вечерний  ",
                    "interval_hours": 1.5,
                    "interval_minutes": 30,
                    "run_at_time": "23:00",
                    "cycles": 5,
                    "enabled": False,
                },
            )
            self.assertEqual(updated.status, 200)
            task = (await updated.json())["task"]
            self.assertEqual(task["name"], "Вечерний")
            self.assertEqual(task["interval_hours"], 1.5)
            self.assertEqual(task["cycles"], 5)
            self.assertFalse(task["enabled"])

            state = await (await client.get("/api/scheduler")).json()
            self.assertEqual(len(state["tasks"]), 1)

            self.assertEqual(await (await client.delete("/api/scheduler/tasks/nightly")).json(), {"ok": True})
            self.assertEqual(await (await client.delete("/api/scheduler/tasks/nightly")).json(), {"ok": False})

    async def test_add_task_validation(self):
        client, _app = await self._client()
        async with client:
            await self._login(client)
            self.assertEqual((await client.post("/api/scheduler/tasks", **_BAD_JSON)).status, 400)
            for payload in ({"task_id": ""}, {"task_id": "t" * 101}, {}):
                resp = await client.post("/api/scheduler/tasks", json=payload)
                self.assertEqual(resp.status, 400, payload)
                self.assertEqual(await resp.json(), {"error": "task_id required (max 100 chars)"})
            for cycles in (0, -1, 1001):
                resp = await client.post("/api/scheduler/tasks", json={"task_id": "t", "cycles": cycles})
                self.assertEqual(resp.status, 400, cycles)
                self.assertEqual(await resp.json(), {"error": "cycles must be 1-1000"})
            nul = await client.post("/api/scheduler/tasks", json={"task_id": "t", "name": "x\x00y"})
            self.assertEqual(nul.status, 400)
            self.assertIn("bad input", (await nul.json())["error"])

    async def test_update_task_validation(self):
        client, _app = await self._client()
        async with client:
            await self._login(client)
            self.assertEqual((await client.put("/api/scheduler/tasks/t", **_BAD_JSON)).status, 400)
            bad_cycles = await client.put("/api/scheduler/tasks/t", json={"cycles": 0})
            self.assertEqual(bad_cycles.status, 400)
            self.assertEqual(await bad_cycles.json(), {"error": "cycles must be 1-1000"})
            missing = await client.put("/api/scheduler/tasks/ghost", json={"name": "x"})
            self.assertEqual(missing.status, 404)
            self.assertEqual(await missing.json(), {"error": "task not found"})

    async def test_start_and_stop(self):
        client, app = await self._client()
        async with client:
            await self._login(client)
            self.assertEqual(await (await client.post("/api/scheduler/start")).json(), {"ok": True})
            self.assertTrue(app[api.KEY_DAEMON].scheduler_running)
            self.assertEqual(await (await client.post("/api/scheduler/stop")).json(), {"ok": True})
            self.assertFalse(app[api.KEY_DAEMON].scheduler_running)


class TestBalanceAlertsAndRpc(PortalApiTest, unittest.IsolatedAsyncioTestCase):
    """Мониторинг балансов и диагностика RPC: порог валидируется, health отдаётся как есть."""

    async def _login(self, client: TestClient) -> None:
        self.assertEqual((await client.post("/api/login/password", json={"password": "k" * 64})).status, 200)

    async def test_anonymous_401(self):
        client, _app = await self._client()
        async with client:
            self.assertEqual((await client.get("/api/balance-alerts")).status, 401)
            self.assertEqual((await client.post("/api/balance-alerts", json={"enabled": True})).status, 401)
            self.assertEqual((await client.get("/api/rpc-health")).status, 401)

    async def test_get_state_then_toggle(self):
        client, _app = await self._client()
        async with client:
            await self._login(client)
            state = await (await client.get("/api/balance-alerts")).json()
            self.assertEqual(state, {"enabled": False, "threshold": 0.001})
            resp = await client.post("/api/balance-alerts", json={"enabled": True, "threshold": 0.5})
            self.assertEqual(resp.status, 200)
            self.assertEqual(await resp.json(), {"ok": True, "enabled": True, "threshold": 0.5})
            self.assertEqual(
                await (await client.get("/api/balance-alerts")).json(),
                {"enabled": True, "threshold": 0.5},
            )

    async def test_toggle_defaults_and_validation(self):
        client, _app = await self._client()
        async with client:
            await self._login(client)
            toggled = await client.post("/api/balance-alerts", json={})
            self.assertEqual(
                await toggled.json(), {"ok": True, "enabled": False, "threshold": 0.001}
            )
            self.assertEqual((await client.post("/api/balance-alerts", **_BAD_JSON)).status, 400)
            for threshold in (-1, 101):
                resp = await client.post("/api/balance-alerts", json={"threshold": threshold})
                self.assertEqual(resp.status, 400, threshold)
                self.assertEqual(await resp.json(), {"error": "threshold must be 0-100"})

    async def test_rpc_health(self):
        client, app = await self._client()
        async with client:
            await self._login(client)
            body = await (await client.get("/api/rpc-health")).json()
            self.assertEqual(body, await app[api.KEY_DAEMON].rpc_health())
            self.assertTrue(body["nodes"][0]["healthy"])

    async def test_faucet_endpoint(self):
        client, _app = await self._client()
        async with client:
            self.assertEqual((await client.get("/api/faucet")).status, 401)
            await self._login(client)
            body = await (await client.get("/api/faucet")).json()
            self.assertEqual(body["live"], 1)
            self.assertEqual(body["strategies"][0]["type"], "chainstack")
            self.assertIn("reason", body["strategies"][0])
            self.assertIn("drip", body)

    async def test_faucet_drip(self):
        client, _app = await self._client()
        async with client:
            self.assertEqual((await client.post("/api/faucet/drip")).status, 401)
            await self._login(client)
            resp = await client.post("/api/faucet/drip")
            self.assertEqual(resp.status, 202)
            body = await resp.json()
            self.assertTrue(body["started"])


if __name__ == "__main__":
    unittest.main()
