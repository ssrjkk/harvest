"""Догоняем покрытие до 100%: portal/api.py, portal/auth.py, portal/farm.py.

Только новые тесты (прод-код и существующие tests/test_portal_*.py не трогаем).
Дублируют стиль test_portal_api.py / test_portal_auth.py / test_portal_farm.py,
но добивают ранее не покрытые ветки.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from typing import cast
from unittest import mock
from urllib.parse import urlencode

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request
from yarl import URL

from portal import api, auth
from portal.api import create_app
from portal.config import PortalConfig
from portal.farm import FarmDaemon


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


class _FlexDaemon:
    """Гибкая заглушка: управляем running/pause/resume и сбоем stop()."""

    def __init__(self, running: bool = True, raise_on_stop: bool = False) -> None:
        self.running = running
        self.raise_on_stop = raise_on_stop
        self.paused = False
        self.stopped = 0

    def run_state(self) -> dict:
        return {"running": self.running, "paused": self.paused}

    async def statistics(self) -> dict:
        return {"running": self.running, "pool": {}, "health_factor": 0.0, "db": {}}

    async def start(self) -> bool:
        return True

    async def stop(self) -> bool:
        self.stopped += 1
        if self.raise_on_stop:
            raise RuntimeError("stop boom")
        return True

    async def pause(self) -> bool:
        return True

    async def resume(self) -> bool:
        return False

    async def top_wallets(self, limit=8) -> list:
        return [{"address": "0xabc", "actions": 3}]

    async def cycle_history(self, limit=12) -> list:
        return [{"id": 1}]


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _hmac_sha256(key: bytes, data: bytes) -> bytes:
    return hmac.new(key, data, hashlib.sha256).digest()


def _tg_sign(token: str, pairs: dict) -> str:
    items = dict(pairs)
    check = "\n".join(f"{k}={v}" for k, v in sorted(items.items()))
    secret = _hmac_sha256(b"WebAppData", token.encode())
    items["hash"] = _hmac_sha256(secret, check.encode()).hex()
    return urlencode(items)


def _tg_init_data(token: str, user_id=123, first_name="Test", auth_date=None) -> str:
    user = {"id": user_id, "first_name": first_name}
    return _tg_sign(
        token,
        {"user": json.dumps(user), "auth_date": str(auth_date if auth_date is not None else int(time.time()))},
    )


def _google_id_token(client_id: str, *, email="u@x.com", nonce="") -> str:
    payload = {
        "aud": client_id,
        "iss": "https://accounts.google.com",
        "email": email,
        "email_verified": True,
        "name": "X",
        "sub": "google-sub-1",
        "exp": int(time.time()) + 3600,
    }
    if nonce:
        payload["nonce"] = nonce
    enc = _b64url(json.dumps(payload).encode())
    return f"h.{enc}.s"


class _FakeResp:
    def __init__(self, status: int, payload=None) -> None:
        self.status = status
        self._payload = payload

    async def json(self):
        return self._payload


class _FakeSession:
    """Замена aiohttp.ClientSession для auth.google_exchange."""

    def __init__(self, resp) -> None:
        self._resp = resp

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def post(self, url, **kw):
        class _CM:
            def __init__(self, resp):
                self._resp = resp

            async def __aenter__(self):
                return self._resp

            async def __aexit__(self, *exc):
                return False

        return _CM(self._resp)


class _PortalApiBase:
    def setUp(self) -> None:
        api._login_fails.clear()
        api._global_login_fails.clear()
        api._choke_buckets.clear()
        api._global_choke.clear()
        self._td = tempfile.TemporaryDirectory()
        api.REVOKED_FILE = str(Path(self._td.name) / "revoked.json")
        api._revoked = None

    def tearDown(self) -> None:
        self._td.cleanup()
        api._revoked = None

    async def _client(self, base: str = "", **kwargs) -> tuple[TestClient, web.Application]:
        cfg = _make_cfg(base, **kwargs)
        app = create_app(cfg, cast(FarmDaemon, _StubDaemon()))
        client = TestClient(TestServer(app))
        await client.start_server()
        return client, app


class TestApiUnitBranches(_PortalApiBase, unittest.TestCase):
    """Прямые unit-проверки веток модуля portal.api."""

    def test_load_revoked_non_dict(self):
        p = str(Path(self._td.name) / "list.json")
        Path(p).write_text("[1, 2, 3]", encoding="utf-8")
        saved = api.REVOKED_FILE
        api.REVOKED_FILE = p
        try:
            self.assertEqual(api._load_revoked(), {})
        finally:
            api.REVOKED_FILE = saved

    def test_revoke_skipped_without_jti(self):
        api._revoked = {"old": 1}
        api._revoke(None, 123)
        api._revoke("", 123)
        self.assertEqual(api._revoked, {"old": 1})

    def test_is_revoked_skipped_without_jti(self):
        self.assertFalse(api._is_revoked(None))
        self.assertFalse(api._is_revoked(""))

    def test_revoke_prunes_expired_when_over_capacity(self):
        now = int(time.time())
        api._revoked = {f"j{i}": now - 10_000 for i in range(10_001)}
        api._revoke("fresh-jti", now + 1000)
        self.assertEqual(len(api._revoked), 1)
        self.assertIn("fresh-jti", api._revoked)

    def test_note_login_fail_prunes_stale_ips(self):
        stale = time.time() - 1000.0
        api._login_fails = {f"ip{i}": [stale] for i in range(10_001)}
        api._note_login_fail("fresh-ip")
        self.assertIn("fresh-ip", api._login_fails)
        self.assertLess(len(api._login_fails), 10_000)

    def test_choke_allowed_denies_over_limit(self):
        allowed = sum(1 for _ in range(api._CHOKE_LIMIT + 5) if api._choke_allowed("b1", "src1"))
        self.assertEqual(allowed, api._CHOKE_LIMIT)
        self.assertFalse(api._choke_allowed("b1", "src1"))

    def test_choke_allowed_prunes_stale_buckets(self):
        stale = time.time() - 1000.0
        api._choke_buckets = {(f"b{i}", "s") : [stale] for i in range(20_001)}
        self.assertTrue(api._choke_allowed("fresh", "src"))
        self.assertLess(len(api._choke_buckets), 20_000)

    def test_is_https_direct_and_trusted_proxy(self):
        cfg = _make_cfg()
        app = web.Application()
        app[api.KEY_CFG] = cfg
        req = make_mocked_request("GET", "/", app=app)
        self.assertFalse(api._is_https(req))
        self.assertTrue(api._is_https(req.clone(scheme="https")))
        cfg.trust_proxy = True
        req2 = make_mocked_request("GET", "/", headers={"X-Forwarded-Proto": "https, http"}, app=app)
        self.assertTrue(api._is_https(req2))

    def test_persist_revoked_replace_failure_logged(self):
        api._revoked = {"j1": 123}
        with mock.patch("core.utils.restrict_file_permissions", side_effect=OSError("perm")):
            with self.assertLogs("portal.api", level="WARNING") as cm:
                api._persist_revoked()
        self.assertTrue(any("Не удалось сохранить" in line for line in cm.output))

    def test_persist_revoked_unlink_failure_logged(self):
        api._revoked = {"j2": 123}
        with mock.patch("core.utils.restrict_file_permissions", side_effect=OSError("perm")), mock.patch(
            "os.unlink", side_effect=OSError("nope")
        ):
            with self.assertLogs("portal.api", level="WARNING") as cm:
                api._persist_revoked()
        self.assertTrue(any("Не удалось сохранить" in line for line in cm.output))


class TestApiClientBranches(_PortalApiBase, unittest.IsolatedAsyncioTestCase):
    """HTTP-ветки: невалидные куки, trust_proxy IP, разные лимиты, ошибки."""

    async def test_invalid_session_cookie_rejected(self):
        client, _app = await self._client()
        async with client:
            client.session.cookie_jar.update_cookies({api.COOKIE_NAME: "garbage-token"}, URL("http://127.0.0.1"))
            self.assertEqual((await client.get("/api/stats")).status, 401)

    async def test_client_ip_from_x_forwarded_for(self):
        client, _app = await self._client(trust_proxy=True)
        async with client:
            await client.post(
                "/api/login/password",
                json={"password": "bad"},
                headers={"X-Forwarded-For": "1.2.3.4, 9.9.9.9"},
            )
            self.assertIn("1.2.3.4", api._login_fails)

    async def test_client_ip_from_x_real_ip(self):
        client, _app = await self._client(trust_proxy=True)
        async with client:
            await client.post("/api/login/password", json={"password": "bad"}, headers={"X-Real-IP": "5.6.7.8"})
            self.assertIn("5.6.7.8", api._login_fails)

    async def test_login_rejected_without_master_key(self):
        client, _app = await self._client()
        cfg = _app[api.KEY_CFG]
        cfg.master_key = None
        async with client:
            resp = await client.post("/api/login/password", json={"password": "x"})
            self.assertEqual(resp.status, 403)

    async def test_login_global_limit_tripped(self):
        client, _app = await self._client()
        for _ in range(api._GLOBAL_MAX):
            api._note_global_login_fail()
        async with client:
            resp = await client.post("/api/login/password", json={"password": "k" * 64})
            self.assertEqual(resp.status, 429)

    async def test_google_start_disabled_400(self):
        client, _app = await self._client()
        async with client:
            resp = await client.get("/auth/google")
            self.assertEqual(resp.status, 400)

    async def test_google_callback_choked_redirects_rate(self):
        client, _app = await self._client()
        for _ in range(api._GLOBAL_MAX):
            api._choke_allowed_global("google_cb")
        async with client:
            resp = await client.get("/auth/google/callback?code=x&state=y", allow_redirects=False)
            self.assertEqual(resp.status, 302)
            self.assertIn("error=rate", resp.headers["Location"])

    async def test_google_callback_three_part_cookie_parsed(self):
        client, _app = await self._client()
        async with client:
            client.session.cookie_jar.update_cookies({api.OAUTH_COOKIE: "aaa.bbb.ccc"}, URL("http://127.0.0.1"))
            resp = await client.get("/auth/google/callback?code=x&state=short", allow_redirects=False)
            self.assertEqual(resp.status, 302)
            self.assertIn("error=bad_state", resp.headers["Location"])

    async def test_google_callback_two_part_cookie_parsed(self):
        client, _app = await self._client()
        async with client:
            client.session.cookie_jar.update_cookies({api.OAUTH_COOKIE: "aaa.bbb"}, URL("http://127.0.0.1"))
            resp = await client.get("/auth/google/callback?code=x&state=short", allow_redirects=False)
            self.assertEqual(resp.status, 302)
            self.assertIn("error=bad_state", resp.headers["Location"])

    async def test_farm_unknown_action_404(self):
        client, _app = await self._client()
        async with client:
            await client.post("/api/login/password", json={"password": "k" * 64})
            resp = await client.post("/api/farm/bogus")
            self.assertEqual(resp.status, 404)
            self.assertEqual(await resp.json(), {"error": "unknown"})

    async def test_cycle_history_and_top_wallets_authed(self):
        client, _app = await self._client()
        async with client:
            self.assertEqual((await client.get("/api/cycle-history")).status, 401)
            self.assertEqual((await client.get("/api/top-wallets")).status, 401)
            await client.post("/api/login/password", json={"password": "k" * 64})
            resp = await client.get("/api/cycle-history")
            self.assertEqual(resp.status, 200)
            body = await resp.json()
            self.assertEqual(len(body["history"]), 1)
            resp = await client.get("/api/top-wallets")
            self.assertEqual(resp.status, 200)
            body = await resp.json()
            self.assertEqual(len(body["wallets"]), 1)

    async def test_tg_init_invalid_json_401(self):
        client, _app = await self._client()
        cfg = _app[api.KEY_CFG]
        cfg.telegram_token = "123:abc"
        async with client:
            resp = await client.post(
                "/api/tg/init",
                data=b"{broken",
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(resp.status, 401)

    async def test_tg_init_success_sets_session(self):
        client, _app = await self._client()
        cfg = _app[api.KEY_CFG]
        cfg.telegram_token = "123:abc"
        cfg.telegram_allow_ids = [123]
        async with client:
            resp = await client.post("/api/tg/init", json={"init_data": _tg_init_data("123:abc")})
            self.assertEqual(resp.status, 200)
            body = await resp.json()
            self.assertEqual(body["name"], "Test")
            set_cookies = resp.headers.getall("Set-Cookie", [])
            self.assertTrue(any(c.startswith("harvest_session=") for c in set_cookies))

    async def test_tg_init_non_int_id_rejected(self):
        client, _app = await self._client()
        cfg = _app[api.KEY_CFG]
        cfg.telegram_token = "123:abc"
        async with client:
            resp = await client.post("/api/tg/init", json={"init_data": _tg_init_data("123:abc", user_id="not-int")})
            self.assertEqual(resp.status, 401)

    async def test_tg_init_denied_for_unknown_id(self):
        client, _app = await self._client()
        cfg = _app[api.KEY_CFG]
        cfg.telegram_token = "123:abc"
        cfg.telegram_allow_ids = [999]
        async with client:
            resp = await client.post("/api/tg/init", json={"init_data": _tg_init_data("123:abc", user_id=1)})
            self.assertEqual(resp.status, 403)


class TestGoogleCallbackFlow(_PortalApiBase, unittest.IsolatedAsyncioTestCase):
    """Полные ветки /auth/google/callback с замоканным network-обменом."""

    def _cfg_and_app(self, base: str = "https://portal.example"):
        cfg = _make_cfg(base)
        cfg.google_client_id = "gid.apps.googleusercontent.com"
        cfg.google_client_secret = "gsec"
        cfg.google_allow_emails = ["me@x.com"]
        app = create_app(cfg, cast(FarmDaemon, _StubDaemon()))
        return cfg, app

    async def test_google_callback_success(self):
        cfg, app = self._cfg_and_app()
        state = auth.csrf_state(cfg.secret)
        profile = {"email": "me@x.com", "name": "X", "sub": "google-sub-1"}
        with mock.patch("portal.api.auth.google_exchange", new=mock.AsyncMock(return_value=profile)):
            client = TestClient(TestServer(app))
            await client.start_server()
            async with client:
                client.session.cookie_jar.update_cookies(
                    {api.OAUTH_COOKIE: f"{state}.verifier.000nonce000"}, URL("http://127.0.0.1")
                )
                resp = await client.get(
                    f"/auth/google/callback?code=code1&state={state}", allow_redirects=False
                )
                self.assertEqual(resp.status, 302)
                self.assertEqual(resp.headers["Location"], "/")
                set_cookies = resp.headers.getall("Set-Cookie", [])
                self.assertTrue(any(c.startswith("harvest_session=") for c in set_cookies))

    async def test_google_callback_profile_none(self):
        cfg, app = self._cfg_and_app()
        state = auth.csrf_state(cfg.secret)
        with mock.patch("portal.api.auth.google_exchange", new=mock.AsyncMock(return_value=None)):
            client = TestClient(TestServer(app))
            await client.start_server()
            async with client:
                client.session.cookie_jar.update_cookies(
                    {api.OAUTH_COOKIE: f"{state}.verifier.000nonce000"}, URL("http://127.0.0.1")
                )
                resp = await client.get(
                    f"/auth/google/callback?code=code1&state={state}", allow_redirects=False
                )
                self.assertEqual(resp.status, 302)
                self.assertIn("error=google", resp.headers["Location"])

    async def test_google_callback_email_denied(self):
        cfg, app = self._cfg_and_app()
        state = auth.csrf_state(cfg.secret)
        profile = {"email": "other@x.com", "name": "O", "sub": "google-sub-2"}
        with mock.patch("portal.api.auth.google_exchange", new=mock.AsyncMock(return_value=profile)):
            client = TestClient(TestServer(app))
            await client.start_server()
            async with client:
                client.session.cookie_jar.update_cookies(
                    {api.OAUTH_COOKIE: f"{state}.verifier.000nonce000"}, URL("http://127.0.0.1")
                )
                resp = await client.get(
                    f"/auth/google/callback?code=code1&state={state}", allow_redirects=False
                )
                self.assertEqual(resp.status, 302)
                self.assertIn("error=denied", resp.headers["Location"])


class TestFarmActionsCoverage(_PortalApiBase, unittest.IsolatedAsyncioTestCase):
    """Ветки /api/farm/{action}: stop-пока-running, фоновый сбой, остальные действия."""

    async def test_farm_stop_running_schedules_async_stop(self):
        daemon = _FlexDaemon(running=True)
        cfg = _make_cfg()
        app = create_app(cfg, cast(FarmDaemon, daemon))
        client = TestClient(TestServer(app))
        await client.start_server()
        async with client:
            await client.post("/api/login/password", json={"password": "k" * 64})
            resp = await client.post("/api/farm/stop")
            self.assertEqual(resp.status, 200)
            body = await resp.json()
            self.assertTrue(body["ok"])
            self.assertTrue(body["stopping"])
        await client.close()

    async def test_farm_stop_task_failure_logged(self):
        daemon = _FlexDaemon(running=True, raise_on_stop=True)
        cfg = _make_cfg()
        app = create_app(cfg, cast(FarmDaemon, daemon))
        client = TestClient(TestServer(app))
        await client.start_server()
        async with client:
            await client.post("/api/login/password", json={"password": "k" * 64})
            with self.assertLogs("portal.api", level="ERROR") as cm:
                resp = await client.post("/api/farm/stop")
                await asyncio.sleep(0.05)
            self.assertEqual(resp.status, 200)
            self.assertTrue(any("[AUDIT] stop task failed" in line for line in cm.output))
        await client.close()

    async def test_farm_actions_when_not_running(self):
        daemon = _FlexDaemon(running=False)
        cfg = _make_cfg()
        app = create_app(cfg, cast(FarmDaemon, daemon))
        client = TestClient(TestServer(app))
        await client.start_server()
        async with client:
            await client.post("/api/login/password", json={"password": "k" * 64})
            resp = await client.post("/api/farm/stop")
            self.assertEqual(resp.status, 200)
            self.assertTrue((await resp.json())["ok"])
            self.assertGreaterEqual(daemon.stopped, 1)

            resp = await client.post("/api/farm/pause")
            self.assertTrue((await resp.json())["ok"])

            resp = await client.post("/api/farm/resume")
            self.assertFalse((await resp.json())["ok"])
        await client.close()


class TestAuthRemainingBranches(unittest.TestCase):
    """Не покрытые ветки portal/auth.py."""

    def test_read_token_without_dot(self):
        self.assertIsNone(auth.read_token("sec", "no-dot"))

    def test_read_token_unparseable_body(self):
        body = _b64url(b"not json")
        sig = _b64url(_hmac_sha256(b"sec", body.encode("ascii")))
        self.assertIsNone(auth.read_token("sec", f"{body}.{sig}"))

    def test_telegram_init_data_missing_hash(self):
        self.assertIsNone(auth.validate_telegram_init_data("t", "user=x&auth_date=1"))

    def test_telegram_init_data_bad_auth_date(self):
        data = _tg_sign("t", {"user": '{"id":1}', "auth_date": "not-a-number"})
        self.assertIsNone(auth.validate_telegram_init_data("t", data))

    def test_telegram_init_data_bad_user_json(self):
        data = _tg_sign("t", {"user": "{broken", "auth_date": str(int(time.time()))})
        self.assertIsNone(auth.validate_telegram_init_data("t", data))


class TestGoogleExchange(unittest.IsolatedAsyncioTestCase):
    """auth.google_exchange: все статусы/ветки с замоканной сетью."""

    def _profile(self, status, payload=None):
        return mock.patch("portal.auth.aiohttp.ClientSession", return_value=_FakeSession(_FakeResp(status, payload)))

    async def test_google_exchange_success(self):
        token = _google_id_token("gid", email="u@x.com", nonce="nonce1")
        with self._profile(200, {"id_token": token}):
            profile = await auth.google_exchange(
                "code1", "gid", "sec", "https://x/cb", code_verifier="ver1", nonce="nonce1"
            )
        self.assertEqual(profile["email"], "u@x.com")
        self.assertEqual(profile["sub"], "google-sub-1")

    async def test_google_exchange_success_without_verifier(self):
        token = _google_id_token("gid", email="u@x.com")
        with self._profile(200, {"id_token": token}):
            profile = await auth.google_exchange("code1", "gid", "sec", "https://x/cb")
        self.assertEqual(profile["email"], "u@x.com")

    async def test_google_exchange_http_error(self):
        with self._profile(400, None):
            self.assertIsNone(await auth.google_exchange("c", "gid", "sec", "https://x/cb"))

    async def test_google_exchange_missing_id_token(self):
        with self._profile(200, {"access_token": "x"}):
            self.assertIsNone(await auth.google_exchange("c", "gid", "sec", "https://x/cb"))

    async def test_google_exchange_bad_id_token(self):
        with self._profile(200, {"id_token": "aaa..bbb"}):
            self.assertIsNone(await auth.google_exchange("c", "gid", "sec", "https://x/cb"))


class _FakeDb:
    closed = False

    def __init__(self, *args, **kwargs) -> None:
        self.init_called = False

    async def init(self) -> None:
        self.init_called = True

    async def close(self) -> None:
        self.closed = True

    async def get_cycle_stats(self) -> dict:
        return {"cycles": 1}

    async def get_top_wallets(self, limit: int) -> list:
        return [{"address": "0xabc", "actions": 3}]

    async def get_cycle_history(self, limit: int) -> list:
        return [{"id": 1}]


def _db_mock():
    """Экземпляр AsyncMock, играющий роль Database (свободные атрибуты-awaitables)."""
    return mock.AsyncMock()


class _FakePool:
    _closed = False
    paused = False

    def __init__(self, *args, **kwargs) -> None:
        self.network = mock.Mock()
        self.network.concurrency_factor.return_value = 0.75

    async def run_forever(self, stop_event) -> None:
        await stop_event.wait()

    async def close(self) -> None:
        self._closed = True

    def pause(self) -> None:
        self.paused = True

    def resume(self) -> None:
        self.paused = False

    def live_stats(self) -> dict:
        return {"actions": 1}


class _FakeSingleInstance:
    def __init__(self, *args, **kwargs) -> None:
        self.released = False

    def acquire(self) -> bool:
        return True

    def release(self) -> None:
        self.released = True


class FarmDaemonTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = str(Path(self._tmp.name) / "farm.sqlite")

    @staticmethod
    def _patches():
        return (
            mock.patch("portal.farm.load_config", return_value={"database": {"master_key": "nope"}}),
            mock.patch("portal.farm.Database", return_value=_db_mock()),
            mock.patch("portal.farm.FarmerPool", return_value=_FakePool()),
            mock.patch("portal.farm.SingleInstance", _FakeSingleInstance),
            mock.patch("portal.farm.resolve_master_key", return_value=b"\x00" * 32),
        )


class TestFarmConnect(FarmDaemonTestBase, unittest.IsolatedAsyncioTestCase):
    async def test_connect_idempotent(self):
        with mock.patch("portal.farm.load_config", return_value={"x": 1}), mock.patch(
            "portal.farm.Database", return_value=_db_mock()
        ) as DB, mock.patch("portal.farm.FarmerPool") as FP, mock.patch(
            "portal.farm._db_master_key", return_value=b"\x00" * 32
        ):
            daemon = FarmDaemon("cfg.yaml", self.db_path, master_key="mk")
            await daemon.connect()
            self.assertIsNotNone(daemon.config)
            await daemon.connect()
            DB.assert_called_once_with(self.db_path, master_key=b"\x00" * 32)
            FP.assert_called_once_with(daemon.config, daemon.db)

    async def test_connect_config_missing_raises(self):
        with mock.patch("portal.farm.load_config", return_value=None):
            daemon = FarmDaemon("missing.yaml", self.db_path)
            with self.assertRaises(RuntimeError):
                await daemon.connect()

    async def test_connect_reuses_existing_db(self):
        with mock.patch("portal.farm.load_config", return_value={"x": 1}), mock.patch(
            "portal.farm.Database", return_value=_db_mock()
        ) as DB, mock.patch("portal.farm.FarmerPool") as FP, mock.patch(
            "portal.farm._db_master_key", return_value=b"\x01" * 32
        ):
            daemon = FarmDaemon("cfg.yaml", self.db_path)
            daemon.db = "pre-created"
            await daemon.connect()
            DB.assert_not_called()
            FP.assert_called_once()
            self.assertEqual(daemon.db, "pre-created")


class TestFarmStartStop(FarmDaemonTestBase, unittest.IsolatedAsyncioTestCase):
    async def test_start_false_when_already_running(self):
        daemon = FarmDaemon("cfg.yaml", self.db_path)
        daemon._task = asyncio.create_task(asyncio.sleep(10))
        try:
            self.assertFalse(await daemon.start())
        finally:
            daemon._task.cancel()
            await asyncio.gather(daemon._task, return_exceptions=True)

    async def test_start_rebuilds_pool_and_runs_then_stop(self):
        with mock.patch("portal.farm.load_config", return_value={"x": 1}), mock.patch(
            "portal.farm.Database", return_value=_db_mock()
        ), mock.patch("portal.farm.FarmerPool", return_value=_FakePool()), mock.patch(
            "portal.farm.SingleInstance", _FakeSingleInstance
        ), mock.patch("portal.farm._db_master_key", return_value=b"\x00" * 32):
            daemon = FarmDaemon("cfg.yaml", self.db_path)
            self.assertTrue(await daemon.start())
            self.assertTrue(daemon.running)
            self.assertIsNotNone(daemon._guard)
            self.assertTrue(await daemon.stop())
            self.assertFalse(daemon.running)
            self.assertIsNone(daemon._task)
            self.assertIsNone(daemon.pool)
            self.assertIsNone(daemon._guard)

    async def test_start_blocked_by_lock(self):
        with mock.patch("portal.farm.SingleInstance") as SI:
            SI.return_value = guard = _FakeSingleInstance()
            guard.acquire = lambda: False
            daemon = FarmDaemon("cfg.yaml", self.db_path)
            daemon.pool = _FakePool()
            with self.assertLogs("portal.farm", level="WARNING"):
                self.assertFalse(await daemon.start())
            self.assertIsNone(daemon._guard)
            self.assertFalse(daemon.running)

    async def test_run_cancelled(self):
        with mock.patch("portal.farm.load_config", return_value={"x": 1}), mock.patch(
            "portal.farm.Database", return_value=_db_mock()
        ), mock.patch("portal.farm.FarmerPool", return_value=_FakePool()), mock.patch(
            "portal.farm.SingleInstance", _FakeSingleInstance
        ), mock.patch("portal.farm._db_master_key", return_value=b"\x00" * 32):
            daemon = FarmDaemon("cfg.yaml", self.db_path)
            self.assertTrue(await daemon.start())
            task = daemon._task
            await asyncio.sleep(0)
            with self.assertLogs("portal.farm", level="INFO") as cm:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            self.assertTrue(any("отменён" in line for line in cm.output))
            self.assertIsNone(daemon.pool)

    async def test_run_exception(self):
        class _BoomPool:
            async def run_forever(self, stop_event):
                raise RuntimeError("погнал")

            async def close(self) -> None:
                pass

        with mock.patch("portal.farm.load_config", return_value={"x": 1}), mock.patch(
            "portal.farm.Database", return_value=_db_mock()
        ), mock.patch("portal.farm.FarmerPool", return_value=_BoomPool()), mock.patch(
            "portal.farm.SingleInstance", _FakeSingleInstance
        ), mock.patch("portal.farm._db_master_key", return_value=b"\x00" * 32):
            daemon = FarmDaemon("cfg.yaml", self.db_path)
            self.assertTrue(await daemon.start())
            with self.assertLogs("portal.farm", level="ERROR") as cm:
                await daemon._task
            self.assertTrue(any("упал" in line for line in cm.output))
            self.assertFalse(daemon.running)
            self.assertIsNone(daemon._guard)

    async def test_stop_false_when_not_running(self):
        daemon = FarmDaemon("cfg.yaml", self.db_path)
        self.assertFalse(await daemon.stop())


class TestFarmShutdown(FarmDaemonTestBase, unittest.IsolatedAsyncioTestCase):
    async def test_shutdown_pool_close_error_logged(self):
        class _RaisePool:
            async def close(self) -> None:
                raise RuntimeError("close fail")

        daemon = FarmDaemon("cfg.yaml", self.db_path)
        daemon.pool = _RaisePool()
        with self.assertLogs("portal.farm", level="WARNING") as cm:
            await daemon._shutdown_pool()
        self.assertTrue(any("закрытии пула" in line for line in cm.output))
        self.assertIsNone(daemon.pool)
        self.assertIsNone(daemon._guard)

    def test_release_guard_error_logged(self):
        class _BadGuard:
            def release(self) -> None:
                raise OSError("release fail")

        daemon = FarmDaemon("cfg.yaml", self.db_path)
        daemon._guard = _BadGuard()
        with self.assertLogs("portal.farm", level="WARNING") as cm:
            daemon._release_guard()
        self.assertTrue(any("снятии фарм-лока" in line for line in cm.output))
        self.assertIsNone(daemon._guard)


class TestFarmPauseResume(FarmDaemonTestBase, unittest.IsolatedAsyncioTestCase):
    async def test_pause_and_resume_when_active(self):
        daemon = FarmDaemon("cfg.yaml", self.db_path)
        pool = _FakePool()
        daemon.pool = pool
        daemon._task = asyncio.create_task(asyncio.sleep(10))
        try:
            self.assertTrue(await daemon.pause())
            self.assertTrue(pool.paused)
            self.assertFalse(await daemon.pause())
            self.assertTrue(await daemon.resume())
            self.assertFalse(pool.paused)
            self.assertFalse(await daemon.resume())
        finally:
            daemon._task.cancel()
            await asyncio.gather(daemon._task, return_exceptions=True)

    async def test_pause_resume_when_not_running(self):
        daemon = FarmDaemon("cfg.yaml", self.db_path)
        daemon.pool = _FakePool()
        self.assertFalse(await daemon.pause())
        self.assertFalse(await daemon.resume())


class TestFarmClose(FarmDaemonTestBase, unittest.IsolatedAsyncioTestCase):
    async def test_close_stops_and_closes_db(self):
        db = _FakeDb()
        daemon = FarmDaemon("cfg.yaml", self.db_path)
        daemon.db = db
        daemon.pool = _FakePool()
        daemon._stop = asyncio.Event()
        daemon._task = asyncio.create_task(daemon._stop.wait())
        await daemon.close()
        self.assertTrue(db.closed)
        self.assertIsNone(daemon.db)
        self.assertIsNone(daemon.pool)
        self.assertFalse(daemon.running)

    async def test_close_when_not_running(self):
        db = _FakeDb()
        daemon = FarmDaemon("cfg.yaml", self.db_path)
        daemon.db = db
        daemon.pool = _FakePool()
        await daemon.close()
        self.assertTrue(db.closed)
        self.assertIsNone(daemon.db)
        self.assertIsNone(daemon.pool)


class TestFarmStats(FarmDaemonTestBase, unittest.IsolatedAsyncioTestCase):
    async def test_statistics_no_pool(self):
        daemon = FarmDaemon("cfg.yaml", self.db_path)
        self.assertEqual(await daemon.statistics(), {"running": False})

    async def test_statistics_full(self):
        daemon = FarmDaemon("cfg.yaml", self.db_path)
        daemon.pool = _FakePool()
        daemon.db = _FakeDb()
        stats = await daemon.statistics()
        self.assertEqual(stats["pool"], {"actions": 1})
        self.assertEqual(stats["health_factor"], 0.75)
        self.assertEqual(stats["db"], {"cycles": 1})
        self.assertFalse(stats["running"])

    async def test_statistics_pool_without_db(self):
        daemon = FarmDaemon("cfg.yaml", self.db_path)
        daemon.pool = _FakePool()
        stats = await daemon.statistics()
        self.assertEqual(stats["db"], {})

    def test_run_state(self):
        daemon = FarmDaemon("cfg.yaml", self.db_path)
        self.assertEqual(daemon.run_state(), {"running": False, "paused": False})
        daemon.pool = _FakePool()
        daemon.pool.paused = True
        self.assertEqual(daemon.run_state(), {"running": False, "paused": True})


class TestFarmViews(FarmDaemonTestBase, unittest.IsolatedAsyncioTestCase):
    async def test_top_wallets_without_db(self):
        daemon = FarmDaemon("cfg.yaml", self.db_path)
        self.assertEqual(await daemon.top_wallets(), [])

    async def test_top_wallets_with_db(self):
        daemon = FarmDaemon("cfg.yaml", self.db_path)
        daemon.db = _FakeDb()
        self.assertEqual(await daemon.top_wallets(5), [{"address": "0xabc", "actions": 3}])

    async def test_cycle_history_without_db(self):
        daemon = FarmDaemon("cfg.yaml", self.db_path)
        self.assertEqual(await daemon.cycle_history(), [])

    async def test_cycle_history_with_db(self):
        daemon = FarmDaemon("cfg.yaml", self.db_path)
        daemon.db = _FakeDb()
        self.assertEqual(await daemon.cycle_history(3), [{"id": 1}])


if __name__ == "__main__":
    unittest.main()
