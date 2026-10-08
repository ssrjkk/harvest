"""Тесты переключения сетей: /api/networks, /api/farm/network/*, switch_to.

Покрывают каталог portal/networks.py, FarmDaemon.switch_to/networks_info
и новые HTTP-эндпоинты (auth, неизвестные slug/action, старт/стоп).
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import cast
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aiohttp.test_utils import TestClient, TestServer
from yarl import URL

from portal import api
from portal.api import create_app
from portal.config import PortalConfig
from portal.farm import FarmDaemon
from portal.networks import NETWORKS, network_by_slug


def _cfg():
    prev = {k: os.environ.get(k) for k in ("PORTAL_SECRET", "FARMER_MASTER_KEY")}
    os.environ["PORTAL_SECRET"] = "s" * 32
    os.environ["FARMER_MASTER_KEY"] = "k" * 64
    cfg = PortalConfig()
    cfg.allow_insecure_password = True
    cfg.password_login = True
    for k, v in prev.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    return cfg


class _NetDaemon:
    def __init__(self):
        self.running = False
        self.last_slug = None

    def networks_info(self):
        return [
            {
                **n,
                "current": True,
                "running": self.running,
            }
            for n in NETWORKS
        ]

    async def switch_to(self, slug):
        self.last_slug = slug
        return slug == "vibevibe"

    async def start(self):
        self.running = True
        return True

    async def stop(self):
        self.running = False
        return True

    def run_state(self):
        return {"running": self.running, "paused": False}


class _ApiBase(unittest.TestCase):
    def setUp(self):
        api._login_fails.clear()
        api._global_login_fails.clear()
        api._choke_buckets.clear()
        api._global_choke.clear()
        self._td = tempfile.TemporaryDirectory()
        api.REVOKED_FILE = str(Path(self._td.name) / "revoked.json")
        api._revoked = None

    def tearDown(self):
        self._td.cleanup()
        api._revoked = None

    async def _client(self, daemon=None):
        cfg = _cfg()
        app = create_app(cfg, cast(FarmDaemon, daemon or _NetDaemon()))
        client = TestClient(TestServer(app))
        await client.start_server()
        # авторизуемся
        token = api.auth.sign_token(cfg.secret, "u", "U")
        client.session.cookie_jar.update_cookies(
            {api.COOKIE_NAME: token}, URL("http://127.0.0.1")
        )
        return client, app


class TestNetworksApi(_ApiBase, unittest.IsolatedAsyncioTestCase):
    async def test_networks_require_auth(self):
        cfg = _cfg()
        app = create_app(cfg, cast(FarmDaemon, _NetDaemon()))
        client = TestClient(TestServer(app))
        await client.start_server()
        async with client:
            self.assertEqual((await client.get("/api/networks")).status, 401)

    async def test_networks_list(self):
        client, _app = await self._client()
        async with client:
            r = await client.get("/api/networks")
            self.assertEqual(r.status, 200)
            body = await r.json()
            self.assertTrue(any(n["slug"] == "vibevibe" for n in body["networks"]))

    async def test_network_farm_start(self):
        daemon = _NetDaemon()
        client, _app = await self._client(daemon)
        async with client:
            r = await client.post("/api/farm/network/vibevibe/start")
            self.assertEqual(r.status, 200)
            body = await r.json()
            self.assertTrue(body["ok"])
            self.assertTrue(daemon.running)
            self.assertEqual(daemon.last_slug, "vibevibe")

    async def test_network_farm_stop(self):
        daemon = _NetDaemon()
        daemon.running = True
        client, _app = await self._client(daemon)
        async with client:
            r = await client.post("/api/farm/network/vibevibe/stop")
            self.assertEqual(r.status, 200)
            self.assertFalse((await r.json())["ok"] is None)
            self.assertFalse(daemon.running)

    async def test_network_farm_unknown_network(self):
        client, _app = await self._client()
        async with client:
            r = await client.post("/api/farm/network/nope/start")
            self.assertEqual(r.status, 404)

    async def test_network_farm_unknown_action(self):
        client, _app = await self._client()
        async with client:
            r = await client.post("/api/farm/network/vibevibe/bogus")
            self.assertEqual(r.status, 404)


class TestNetworksCatalog(unittest.TestCase):
    def test_network_by_slug_found(self):
        n = network_by_slug("vibevibe")
        self.assertIsNotNone(n)
        self.assertEqual(n["chain_id"], 46630)

    def test_network_by_slug_missing(self):
        self.assertIsNone(network_by_slug("does-not-exist"))

    def test_networks_are_unique_slugs(self):
        slugs = [n["slug"] for n in NETWORKS]
        self.assertEqual(len(slugs), len(set(slugs)))
        self.assertGreaterEqual(len(NETWORKS), 3)


class _FakeDb:
    def __init__(self, *a, **k):
        self.closed = False

    async def init(self):
        pass

    async def close(self):
        self.closed = True

    async def count_wallets(self):
        return 0

    async def get_cycle_stats(self):
        return {"cycles": 0}


class _FakePool:
    _closed = False
    paused = False

    def __init__(self, *a, **k):
        self.network = mock.Mock()
        self.network.concurrency_factor.return_value = 0.9

    async def run_forever(self, stop_event):
        await stop_event.wait()

    async def close(self):
        self._closed = True

    def live_stats(self):
        return {
            "actions": 0,
            "errors": 0,
            "processed": 0,
            "cycles": 0,
            "dropped": 0,
            "dyn_workers": 1,
            "health": 1.0,
            "paused": False,
        }


class _FakeSingleInstance:
    def __init__(self, *a, **k):
        self.released = False

    def acquire(self):
        return True

    def release(self):
        self.released = True


class TestSwitchTo(unittest.IsolatedAsyncioTestCase):
    def _daemon(self):
        daemon = FarmDaemon("config_vibevibe.yaml", "farming_vibevibe.db")
        daemon.db = _FakeDb()
        daemon.pool = _FakePool()
        return daemon

    async def test_networks_info(self):
        daemon = self._daemon()
        info = daemon.networks_info()
        self.assertEqual(len(info), len(NETWORKS))
        current = [n for n in info if n["current"]]
        self.assertEqual(len(current), 1)
        self.assertEqual(current[0]["slug"], "vibevibe")

    async def test_switch_to_valid(self):
        daemon = self._daemon()
        ok = await daemon.switch_to("robinhood")
        self.assertTrue(ok)
        self.assertEqual(daemon.current, "robinhood")
        self.assertTrue(daemon.farm_config.endswith("config_robinhood.yaml"))
        self.assertEqual(daemon.db_path, "farming_robinhood.db")
        self.assertIsNone(daemon.pool)
        self.assertIsNone(daemon.db)

    async def test_switch_to_invalid(self):
        daemon = self._daemon()
        self.assertFalse(await daemon.switch_to("nope"))
        self.assertEqual(daemon.current, "vibevibe")

    async def test_switch_to_db_close_failure_swallowed(self):
        daemon = self._daemon()

        class _BoomDb(_FakeDb):
            async def close(self):
                raise RuntimeError("db boom")

        daemon.db = _BoomDb()
        ok = await daemon.switch_to("arc")
        self.assertTrue(ok)
        self.assertEqual(daemon.current, "arc")
        self.assertIsNone(daemon.db)

    async def test_switch_to_stops_running_farm(self):
        with mock.patch("portal.farm.load_config", return_value={"x": 1}), mock.patch(
            "portal.farm.Database", return_value=_FakeDb()
        ), mock.patch("portal.farm.FarmerPool", return_value=_FakePool()), mock.patch(
            "portal.farm.SingleInstance", _FakeSingleInstance
        ), mock.patch("portal.farm._db_master_key", return_value=b"\x00" * 32):
            daemon = FarmDaemon("config_vibevibe.yaml", "farming_vibevibe.db")
            await daemon.connect()
            self.assertTrue(await daemon.start())
            self.assertTrue(daemon.running)
            ok = await daemon.switch_to("arc")
            self.assertTrue(ok)
            self.assertEqual(daemon.current, "arc")
            self.assertFalse(daemon.running)


if __name__ == "__main__":
    unittest.main()
