"""Офлайн end-to-end тесты HTTP-API портала (aiohttp TestClient, без сети)."""

import os
import tempfile
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
        for k in ("PORTAL_SECRET", "FARMER_MASTER_KEY", "PORTAL_COOKIE_SECURE", "PORTAL_TRUST_PROXY")
    }
    os.environ["PORTAL_SECRET"] = "s" * 32
    os.environ["FARMER_MASTER_KEY"] = "k" * 64
    os.environ.pop("PORTAL_COOKIE_SECURE", None)
    os.environ.pop("PORTAL_TRUST_PROXY", None)
    cfg = PortalConfig()
    cfg.public_base_url = public_base_url
    cfg.allow_insecure_password = allow_insecure
    cfg.cookie_secure = cookie_secure
    cfg.password_login = password
    cfg.trust_proxy = trust_proxy
    for k, v in prev.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    return cfg


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
        allowed = sum(1 for _ in range(api._GLOBAL_MAX + 5) if api._choke_allowed_global("tg_init"))
        self.assertEqual(allowed, api._GLOBAL_MAX)
        self.assertFalse(api._choke_allowed_global("tg_init"))

    def test_buckets_independent(self):
        for _ in range(api._GLOBAL_MAX):
            api._choke_allowed_global("tg_init")
        # Другой бакет ещё свободен (лимиты не пересекаются).
        self.assertTrue(api._choke_allowed_global("google_cb"))


if __name__ == "__main__":
    unittest.main()
