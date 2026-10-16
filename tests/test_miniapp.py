"""Приёмка ТЗ этап 3 (п.9): Mini App — авторизация, API, приватность, публикация."""

import asyncio
import hashlib
import hmac
import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aiohttp.test_utils import TestClient, TestServer

from portal import api
from portal.api import create_app
from portal.config import PortalConfig

TOKEN = "123456:TESTTOKEN"


def _sign(params: dict) -> str:
    items = {k: str(v) for k, v in params.items()}
    check = "\n".join(f"{k}={v}" for k, v in sorted(items.items()))
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    digest = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode({**params, "hash": digest})


def _init(user_id=123, name="Test", auth_date=None, token=TOKEN, **extra):
    user = {"id": user_id, "first_name": name}
    params = {"user": json.dumps(user), "auth_date": str(
        auth_date if auth_date is not None else int(time.time()))}
    params.update(extra)
    if token is None:
        q = dict(params)
        q.pop("hash", None)
        return urlencode(q)
    items = dict(params)
    check = "\n".join(f"{k}={v}" for k, v in sorted(items.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    digest = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode({**params, "hash": digest})


class _Daemon:
    db_path = None
    running = False

    def run_state(self):
        return {"running": self.running, "paused": False}

    async def statistics(self):
        return {"running": self.running, "network": "vibevibe",
                "pool": {"cycles": 5, "errors": 0, "paused": False, "dyn_workers": 4},
                "db": {"cycles": 5}, "health_factor": 1.0, "wallet_count": 3}

    async def wallet_cells(self):
        return "owo", 3

    async def cycle_history(self, limit=12):
        return [{"id": 1, "actions_ok": 5, "errors": 0, "duration_s": 10,
                 "wallets": 3, "started_at": "2026-01-01"}]

    def networks_info(self):
        return [{"slug": "vibevibe", "name": "vibe/vibe", "chain_id": 46630,
                 "currency": "ETH", "tagline": "t", "current": True, "running": False}]

    async def start(self):
        self.running = True
        return True

    async def stop(self):
        self.running = False
        return True

    async def pause(self):
        return True

    async def resume(self):
        return True

    async def export_wallets(self, fmt="csv"):
        return "address,private_key\n0x1,SECRETKEY\n", "wallets.csv"

    def scheduler_state(self):
        return {"running": False, "tasks": []}


def _mcfg(**env):
    prev = {k: os.environ.get(k) for k in
            ("PORTAL_SECRET", "FARMER_MASTER_KEY", "TELEGRAM_BOT_TOKEN",
             "TELEGRAM_ALLOW_IDS", "PORTAL_MINIAPP_PUBLIC")}
    os.environ["PORTAL_SECRET"] = "s" * 32
    os.environ["FARMER_MASTER_KEY"] = "k" * 64
    os.environ["TELEGRAM_BOT_TOKEN"] = TOKEN
    os.environ["TELEGRAM_ALLOW_IDS"] = "123"
    for k, v in env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    try:
        cfg = PortalConfig()
    finally:
        for k, v in prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return cfg


class MiniBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from portal import bot_telegram as _bt
        from portal import miniapp as _m

        _bt._flood_last.clear()
        _m._mut.clear()
        _m._choke.clear()
        _m._gchoke.clear()
        _m._confirm_used.clear()
        api._choke_buckets.clear()
        api._global_choke.clear()
        self._td = None

    async def _client(self, daemon=None, **env):
        cfg = _mcfg(**env)
        app = create_app(cfg, daemon or _Daemon())
        client = TestClient(TestServer(app))
        await client.start_server()
        self.addCleanup(client.close)
        return client

    def _h(self, init_data):
        return {"Authorization": f"tma {init_data}"}


class TestUniform401(MiniBase):
    async def test_no_header_everywhere(self):
        client = await self._client()
        for method, path in [
            ("GET", "/api/miniapp/state"), ("GET", "/api/miniapp/history"),
            ("GET", "/api/miniapp/networks"), ("GET", "/api/miniapp/scheduler"),
            ("POST", "/api/miniapp/farm"),
            ("POST", "/api/miniapp/command"), ("POST", "/api/miniapp/export/token"),
            ("POST", "/api/miniapp/export/request"), ("GET", "/api/miniapp/settings"),
            ("POST", "/api/miniapp/settings"),
        ]:
            resp = await client.request(method, path, json={})
            self.assertEqual(resp.status, 401, path)
            self.assertEqual(await resp.json(), {"error": "unauthorized"}, path)

    async def test_all_denies_identical(self):
        client = await self._client()
        bodies = []
        now = int(time.time())
        cases = [
            _init(user_id=123)[:-1] + "X",  # битый байт
            _init(user_id=123, token="999:OTHER"),  # чужой токен
            urlencode({"user": '{"id":123}', "auth_date": str(now)}),  # нет hash
            _init(user_id=123, auth_date=now - 99999),  # просрочено
            _init(user_id=123, auth_date=now + 3600),  # из будущего
            _init(user_id=999),  # не в allowlist
            urlencode({"auth_date": str(now), "hash": "x"}),  # нет user
        ]
        for init in cases:
            resp = await client.get("/api/miniapp/state", headers=self._h(init))
            self.assertEqual(resp.status, 401)
            bodies.append(await resp.json())
        # >16 КБ в заголовке режет сам HTTP-парсер (400, без деталей и данных) —
        # до приложения не доходит; проверка MAX_INITDATA — второй рубеж.
        resp = await client.get("/api/miniapp/state", headers=self._h("x" * 17_000))
        self.assertIn(resp.status, (400, 401))
        for b in bodies:
            self.assertEqual(b, {"error": "unauthorized"})

    async def test_compare_digest_used(self):
        client = await self._client()
        with mock.patch("portal.auth.hmac.compare_digest",
                        wraps=hmac.compare_digest) as cd:
            resp = await client.get("/api/miniapp/state", headers=self._h(_init()))
            self.assertEqual(resp.status, 200)
        self.assertTrue(cd.called)

    async def test_valid_passes_and_nostore(self):
        client = await self._client()
        resp = await client.get("/api/miniapp/state", headers=self._h(_init()))
        self.assertEqual(resp.status, 200)
        self.assertIn("no-store", resp.headers.get("Cache-Control", ""))
        body = await resp.json()
        self.assertIn("cells", body)
        text = await (await client.get(
            "/api/miniapp/state", headers=self._h(_init()))).text()
        self.assertNotIn("private_key", text)
        self.assertNotIn("0x", text)

    async def test_etag_304(self):
        client = await self._client()
        r1 = await client.get("/api/miniapp/state", headers=self._h(_init()))
        tag = r1.headers.get("ETag")
        self.assertTrue(tag)
        r2 = await client.get("/api/miniapp/state",
                              headers={**self._h(_init()), "If-None-Match": tag})
        self.assertEqual(r2.status, 304)

    async def test_logs_hold_no_secrets(self):
        client = await self._client()
        marker_user = {"id": 999, "first_name": "MarkerZZ9"}
        init = _sign({"user": json.dumps(marker_user),
                      "auth_date": str(int(time.time()))})
        with self.assertLogs("portal.miniapp", level="INFO") as cm:
            await client.get("/api/miniapp/state", headers=self._h(init))
        blob = "\n".join(cm.output)
        self.assertNotIn("MarkerZZ9", blob)
        self.assertNotIn(init[:40], blob)
        self.assertNotIn(TOKEN, blob)


class TestFarmCommand(MiniBase):
    async def test_stranger_runs_nothing(self):
        client = await self._client()
        daemon = client._server.app[api.KEY_DAEMON]
        resp = await client.post("/api/miniapp/farm", json={"action": "start"},
                                 headers=self._h(_init(user_id=999)))
        self.assertEqual(resp.status, 401)
        self.assertFalse(daemon.running)

    async def test_owner_farm(self):
        client = await self._client()
        daemon = client._server.app[api.KEY_DAEMON]
        resp = await client.post("/api/miniapp/farm", json={"action": "start"},
                                 headers=self._h(_init()))
        self.assertEqual(resp.status, 200)
        self.assertTrue(daemon.running)
        resp = await client.post("/api/miniapp/farm", json={"action": "nope"},
                                 headers=self._h(_init()))
        self.assertEqual(resp.status, 404)

    async def test_command_shared_validator(self):
        from portal import bot_ops as _ops

        client = await self._client()
        h = self._h(_init())
        resp = await client.post("/api/miniapp/command",
                                 json={"kind": "cmd", "net": "nope"},
                                 headers=h)
        self.assertEqual(resp.status, 400)
        resp = await client.post("/api/miniapp/command",
                                 json={"kind": "cmd", "net": "vibevibe",
                                       "wallets": 50, "cycles": 5,
                                       "profile": "fast", "faucet": True},
                                 headers=h)
        body = await resp.json()
        self.assertEqual(body["cmd"], _ops.apply_profile_flags(
            _ops.build_command("vibevibe", 50, 5, False), "fast"))
        resp = await client.post("/api/miniapp/command",
                                 json={"kind": "check", "yaml": "not: [valid"},
                                 headers=h)
        body = await resp.json()
        self.assertFalse(body["ok"])

    async def test_networks_no_addresses(self):
        client = await self._client()
        resp = await client.get("/api/miniapp/networks", headers=self._h(_init()))
        body = await resp.json()
        self.assertTrue(body["networks"])
        self.assertIn("presets", body)
        self.assertNotIn("0x", await resp.text())

    async def test_scheduler_read(self):
        client = await self._client()
        resp = await client.get("/api/miniapp/scheduler", headers=self._h(_init()))
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        self.assertIn("tasks", body)
        self.assertNotIn("0x", await resp.text())


class TestMiniGaps(MiniBase):
    async def test_mut_prune_and_global_choke(self):
        from portal import miniapp as _m

        old = [(i, [0.0] * 1) for i in range(20005)]
        for i, v in old:
            _m._mut[(i, "x")] = v
        self.assertTrue(_m._mut_ok(123, "y"))
        for _ in range(25):
            _m._gchoke_ok("tma")
        client = await self._client()
        resp = await client.get("/api/miniapp/state", headers=self._h(_init()))
        self.assertEqual(resp.status, 401)

    async def test_daemon_failures_fall_back(self):
        from portal import bot_settings as _bs

        class _Bad(_Daemon):
            async def statistics(self):
                raise RuntimeError("x")

        client = await self._client(daemon=_Bad())
        resp = await client.get("/api/miniapp/state", headers=self._h(_init()))
        self.assertEqual(resp.status, 200)

        class _BadNet(_Daemon):
            def networks_info(self):
                raise RuntimeError("x")

            async def wallet_cells(self):
                raise RuntimeError("x")

        client2 = await self._client(daemon=_BadNet())
        resp2 = await client2.get("/api/miniapp/state", headers=self._h(_init()))
        body = await resp2.json()
        self.assertEqual(body["cells"], "")

        with mock.patch.object(_bs, "get_settings", side_effect=RuntimeError("x")):
            client3 = await self._client()
            resp3 = await client3.get("/api/miniapp/state", headers=self._h(_init()))
            body3 = await resp3.json()
            self.assertEqual((body3["mode"], body3["lang"]), ("novice", "ru"))

        class _BadHealth(_Daemon):
            async def statistics(self):
                s = await super().statistics()
                s["health_factor"] = object()
                return s

        client4 = await self._client(daemon=_BadHealth())
        resp4 = await client4.get("/api/miniapp/state", headers=self._h(_init()))
        self.assertEqual((await resp4.json())["health_factor"], 0.0)

    async def test_zero_user_id_denied(self):
        client = await self._client()
        init = _sign({"user": json.dumps({"id": 0}),
                      "auth_date": str(int(time.time()))})
        resp = await client.get("/api/miniapp/state", headers=self._h(init))
        self.assertEqual(resp.status, 401)

    async def test_history_ok_and_farm_variants(self):
        from portal import miniapp as _m

        client = await self._client()
        h = self._h(_init())
        resp = await client.get("/api/miniapp/history", headers=h)
        self.assertEqual(resp.status, 200)
        resp = await client.post("/api/miniapp/farm", data=b"{x",
                                 headers={**h, "Content-Type": "application/json"})
        self.assertEqual(resp.status, 400)
        for action in ("pause", "resume", "stop"):
            resp = await client.post("/api/miniapp/farm", json={"action": action},
                                     headers=h)
            self.assertEqual(resp.status, 200)
        _m._mut.clear()
        for _ in range(35):
            _m._mut_ok(123, "farm")
        resp = await client.post("/api/miniapp/farm", json={"action": "start"},
                                 headers=h)
        self.assertEqual(resp.status, 429)

    async def test_stop_task_failure_logged(self):
        client = await self._client()
        daemon = client._server.app[api.KEY_DAEMON]
        daemon.running = True
        daemon.stop = mock.AsyncMock(side_effect=RuntimeError("boom"))
        with self.assertLogs("portal.miniapp", level="ERROR"):
            resp = await client.post("/api/miniapp/farm", json={"action": "stop"},
                                     headers=self._h(_init()))
            self.assertEqual(resp.status, 200)
            await asyncio.sleep(0.05)

    async def test_command_variants(self):
        from portal import miniapp as _m

        client = await self._client()
        h = self._h(_init())
        resp = await client.post("/api/miniapp/command", data=b"{x",
                                 headers={**h, "Content-Type": "application/json"})
        self.assertEqual(resp.status, 400)
        resp = await client.post("/api/miniapp/command",
                                 json={"kind": "cfg", "net": "vibevibe"},
                                 headers=h)
        body = await resp.json()
        self.assertTrue(body["filename"].endswith(".yaml"))
        self.assertIn("wallets", body["yaml"])
        resp = await client.post("/api/miniapp/command",
                                 json={"kind": "cfg", "wallets": "xx"},
                                 headers=h)
        self.assertEqual(resp.status, 400)
        _m._mut.clear()
        for _ in range(35):
            _m._mut_ok(123, "command")
        resp = await client.post("/api/miniapp/command", json={"kind": "cmd"},
                                 headers=h)
        self.assertEqual(resp.status, 429)

    async def test_export_token_request_variants(self):
        from portal import miniapp as _m

        client = await self._client()
        h = self._h(_init())
        resp = await client.post("/api/miniapp/export/token", data=b"{x",
                                 headers={**h, "Content-Type": "application/json"})
        self.assertEqual(resp.status, 200)
        resp = await client.post("/api/miniapp/export/token",
                                 json={"format": "csv"}, headers=self._h(_init("x")),
                                 )
        self.assertEqual(resp.status, 401)
        _m._mut.clear()
        for _ in range(35):
            _m._mut_ok(123, "export_token")
        resp = await client.post("/api/miniapp/export/token",
                                 json={"format": "csv"}, headers=h)
        self.assertEqual(resp.status, 429)
        resp = await client.post("/api/miniapp/export/request", data=b"{x",
                                 headers={**h, "Content-Type": "application/json"})
        self.assertEqual(resp.status, 400)
        resp = await client.post("/api/miniapp/export/request",
                                 json={"format": "xml", "confirm_token": "x"},
                                 headers=h)
        self.assertEqual(resp.status, 400)

    async def test_settings_failure(self):
        from portal import bot_settings as _bs

        client = await self._client()
        h = self._h(_init())
        with mock.patch.object(_bs, "set_mode", side_effect=RuntimeError("x")):
            resp = await client.post("/api/miniapp/settings", json={"mode": "pro"},
                                     headers=h)
            self.assertEqual(resp.status, 500)
        with mock.patch.object(_bs, "get_settings", side_effect=RuntimeError("x")):
            resp = await client.get("/api/miniapp/settings", headers=h)
            body = await resp.json()
            self.assertEqual((body["mode"], body["lang"]), ("novice", "ru"))

    async def test_scheduler_bad_shape(self):
        client = await self._client()
        daemon = client._server.app[api.KEY_DAEMON]
        daemon.scheduler_state = mock.Mock(return_value=["not", "a", "dict"])
        resp = await client.get("/api/miniapp/scheduler",
                                headers=self._h(_init()))
        self.assertEqual(resp.status, 500)

    async def test_settings_roundtrip(self):
        client = await self._client()
        h = self._h(_init())
        resp = await client.get("/api/miniapp/settings", headers=h)
        self.assertEqual((await resp.json())["mode"], "novice")
        resp = await client.post("/api/miniapp/settings",
                                 json={"mode": "pro", "lang": "en"}, headers=h)
        body = await resp.json()
        self.assertEqual((body["mode"], body["lang"]), ("pro", "en"))


class TestExportViaBot(MiniBase):
    async def test_full_flow_cooldown_audit(self):
        import portal.bot_telegram as _bt

        old, _bt._EXPORT_AUTODELETE_S = _bt._EXPORT_AUTODELETE_S, 0
        try:
            client = await self._client()
            h = self._h(_init())
            sent = []

            class _FakeBot:
                async def __aenter__(self):
                    return self

                async def __aexit__(self, *a):
                    return False

                async def send_document(self, chat_id, document, caption,
                                        protect_content=False):
                    sent.append((chat_id, document.filename, caption,
                                 protect_content))
                    m = mock.Mock()
                    m.message_id = 5
                    return m

                async def delete_message(self, chat_id, message_id):
                    sent.append(("deleted", chat_id, message_id))

            with mock.patch("aiogram.Bot", return_value=_FakeBot()):
                r = await client.post("/api/miniapp/export/token",
                                      json={"format": "csv"}, headers=h)
                token = (await r.json())["confirm_token"]
                # чужой токен подтверждения — тот же uniform-401
                r2 = await client.post(
                    "/api/miniapp/export/request",
                    json={"format": "csv", "confirm_token": token + "x"},
                    headers=h)
                self.assertEqual(r2.status, 401)
                with self.assertLogs("portal.miniapp", level="INFO") as cm:
                    r3 = await client.post(
                        "/api/miniapp/export/request",
                        json={"format": "csv", "confirm_token": token},
                        headers=h)
                self.assertEqual(r3.status, 200)
                self.assertEqual((await r3.json())["sent"], "chat")
                # повтор токена — одноразовый
                r4 = await client.post(
                    "/api/miniapp/export/request",
                    json={"format": "csv", "confirm_token": token},
                    headers=h)
                self.assertEqual(r4.status, 401)
            self.assertTrue(sent[0][3])  # protect_content
            self.assertEqual(sent[0][0], 123)
            blob = "\n".join(cm.output)
            self.assertIn("miniapp export user=123 fmt=csv", blob)
            self.assertNotIn("SECRETKEY", blob)
            # кулдаун общий с ботом: второй запрос — 429
            with mock.patch("aiogram.Bot", return_value=_FakeBot()):
                r = await client.post("/api/miniapp/export/token",
                                      json={"format": "csv"}, headers=h)
                token2 = (await r.json())["confirm_token"]
                r5 = await client.post(
                    "/api/miniapp/export/request",
                    json={"format": "csv", "confirm_token": token2},
                    headers=h)
                self.assertEqual(r5.status, 429)
        finally:
            _bt._EXPORT_AUTODELETE_S = old


class TestPublicMode(MiniBase):
    async def test_outside_paths_404(self):
        client = await self._client(PORTAL_MINIAPP_PUBLIC="1")
        for path, want in [("/", 404), ("/api/stats", 404), ("/login", 404),
                           ("/api/tg/init", 404)]:
            resp = await client.get(path)
            self.assertEqual(resp.status, want, path)
        resp = await client.get("/app/")
        self.assertEqual(resp.status, 200)
        resp = await client.get("/healthz")
        self.assertEqual(resp.status, 200)

    async def test_app_page(self):
        client = await self._client()
        resp = await client.get("/app/")
        self.assertEqual(resp.status, 200)
        text = await resp.text()
        self.assertIn("https://telegram.org/js/telegram-web-app.js", text)
        csp = resp.headers.get("Content-Security-Policy", "")
        self.assertIn("https://telegram.org", csp)
        self.assertIn("frame-ancestors", csp)


class TestMiniErrors(MiniBase):
    async def test_no_token_configured(self):
        from portal import miniapp as _m

        client = await self._client(TELEGRAM_BOT_TOKEN="")
        resp = await client.get("/api/miniapp/state", headers=self._h(_init()))
        self.assertEqual(resp.status, 401)
        self.assertEqual(await resp.json(), {"error": "unauthorized"})
        _m._choke.clear()
        _m._gchoke.clear()

    async def test_bad_user_id_type(self):
        client = await self._client()
        init = _sign({"user": json.dumps({"id": [1]}),
                      "auth_date": str(int(time.time()))})
        resp = await client.get("/api/miniapp/state", headers=self._h(init))
        self.assertEqual(resp.status, 401)

    async def test_choke(self):
        from portal import miniapp as _m

        client = await self._client()
        for _ in range(35):
            _m._choke_ok("tma", "127.0.0.1")
        resp = await client.get("/api/miniapp/state", headers=self._h(_init()))
        self.assertEqual(resp.status, 401)

    async def test_bad_bodies(self):
        client = await self._client()
        h = self._h(_init())
        resp = await client.post("/api/miniapp/farm", data=b"{broken",
                                 headers={**h, "Content-Type": "application/json"})
        self.assertEqual(resp.status, 400)
        resp = await client.post("/api/miniapp/command", json={"kind": "cmd",
                                 "net": "vibevibe", "wallets": "xx"},
                                 headers=h)
        self.assertEqual(resp.status, 400)
        resp = await client.post("/api/miniapp/command", json={"kind": "nope"},
                                 headers=h)
        self.assertEqual(resp.status, 404)
        resp = await client.post("/api/miniapp/export/token",
                                 json={"format": "xml"}, headers=h)
        self.assertEqual(resp.status, 400)
        resp = await client.post("/api/miniapp/settings", data=b"{broken",
                                 headers={**h, "Content-Type": "application/json"})
        self.assertEqual(resp.status, 400)

    async def test_mut_rate(self):
        from portal import miniapp as _m

        client = await self._client()
        h = self._h(_init())
        for _ in range(35):
            _m._mut_ok(123, "settings")
        resp = await client.post("/api/miniapp/settings", json={"mode": "pro"},
                                 headers=h)
        self.assertEqual(resp.status, 429)

    async def test_stop_task_and_history_limit(self):
        client = await self._client()
        daemon = client._server.app[api.KEY_DAEMON]
        daemon.running = True
        resp = await client.post("/api/miniapp/farm", json={"action": "stop"},
                                 headers=self._h(_init()))
        self.assertEqual((await resp.json())["stopping"], True)
        await asyncio.sleep(0.05)
        resp = await client.get("/api/miniapp/history?limit=abc",
                                headers=self._h(_init()))
        self.assertEqual(resp.status, 200)

    async def test_cfg_send_and_export_failure(self):
        from portal import bot_telegram as _bt

        _bt._flood_last.clear()
        client = await self._client()
        h = self._h(_init())

        class _Boom:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def send_document(self, *a, **k):
                raise RuntimeError("tg down")

        with mock.patch("aiogram.Bot", return_value=_Boom()):
            resp = await client.post("/api/miniapp/command",
                                     json={"kind": "cfg_send", "net": "vibevibe"},
                                     headers=h)
            self.assertEqual(resp.status, 500)
            resp = await client.post("/api/miniapp/export/token",
                                     json={"format": "csv"}, headers=h)
            token = (await resp.json())["confirm_token"]
            resp = await client.post("/api/miniapp/export/request",
                                     json={"format": "csv",
                                           "confirm_token": token},
                                     headers=h)
            self.assertEqual(resp.status, 500)

    async def test_scheduler_failure(self):
        client = await self._client()
        daemon = client._server.app[api.KEY_DAEMON]
        daemon.scheduler_state = mock.Mock(side_effect=RuntimeError("x"))
        resp = await client.get("/api/miniapp/scheduler",
                                headers=self._h(_init()))
        self.assertEqual(resp.status, 500)

    async def test_cfg_send_ok(self):
        client = await self._client()
        h = self._h(_init())

        class _Ok:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def send_document(self, *a, **k):
                m = mock.Mock()
                m.message_id = 1
                return m

        with mock.patch("aiogram.Bot", return_value=_Ok()):
            resp = await client.post("/api/miniapp/command",
                                     json={"kind": "cfg_send", "net": "vibevibe"},
                                     headers=h)
            self.assertEqual((await resp.json())["sent"], "chat")


if __name__ == "__main__":
    unittest.main()
