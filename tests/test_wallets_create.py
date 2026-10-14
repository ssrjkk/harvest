"""Создание кошельков: FarmDaemon.create_wallets + POST /api/wallets."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import cast
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aiohttp.test_utils import TestClient, TestServer

from portal.api import create_app
from portal.config import PortalConfig
from portal.farm import FarmDaemon


def _async_result(result=None):
    async def _fn(*args, **kwargs):
        return result

    return _fn


class _StubDaemon:
    config = {"wallets": {"count": 3}}

    def run_state(self) -> dict:
        return {"running": False, "paused": False}

    async def create_wallets(self, count):
        return [{"address": f"0x{i:040x}"} for i in range(count)]


class _NoDbDaemon(_StubDaemon):
    async def create_wallets(self, count):
        raise RuntimeError("БД фермы не подключена")


def _make_cfg() -> PortalConfig:
    prev = {k: os.environ.get(k) for k in ("PORTAL_SECRET", "FARMER_MASTER_KEY")}
    os.environ["PORTAL_SECRET"] = "s" * 32
    os.environ["FARMER_MASTER_KEY"] = "k" * 64
    try:
        cfg = PortalConfig()
        cfg.allow_insecure_password = True
        cfg.cookie_secure = False
        return cfg
    finally:
        for k, v in prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class TestFarmWalletBalances(unittest.IsolatedAsyncioTestCase):
    async def test_ok_and_rpc_error(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = FarmDaemon("config.yaml", str(Path(td) / "t.db"))
            daemon.db = mock.Mock()
            daemon.db.get_all_wallets = _async_result(
                [{"address": "0xa"}, {"address": ""}, {"address": "0xb"}]
            )
            net = mock.Mock()
            net.get_balance = mock.AsyncMock(side_effect=[1.25, RuntimeError("rpc down")])
            daemon.pool = mock.Mock()
            daemon.pool.network = net
            out = await daemon.wallet_balances()
            self.assertEqual(
                out,
                [{"address": "0xa", "balance": 1.25}, {"address": "0xb", "balance": None}],
            )

    async def test_no_db_or_pool(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = FarmDaemon("config.yaml", str(Path(td) / "t.db"))
            daemon.db = None
            daemon.pool = mock.Mock()
            with self.assertRaises(RuntimeError):
                await daemon.wallet_balances()
            daemon.db = mock.Mock()
            daemon.pool = None
            with self.assertRaises(RuntimeError):
                await daemon.wallet_balances()


class TestFarmCreateWallets(unittest.IsolatedAsyncioTestCase):
    async def test_ok_and_clamped(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = FarmDaemon("config.yaml", str(Path(td) / "t.db"))
            daemon.db = mock.Mock()
            daemon.config = {}
            with mock.patch("core.wallet.WalletManager") as wm_cls:
                inst = wm_cls.return_value
                inst.create_wallets = mock.AsyncMock(return_value=[{"address": "0x1"}] * 50)
                out = await daemon.create_wallets(999)
                self.assertEqual(len(out), 50)
                wm_cls.assert_called_once_with({}, daemon.db)

    async def test_no_db_raises(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = FarmDaemon("config.yaml", str(Path(td) / "t.db"))
            daemon.db = None
            daemon.config = {}
            with self.assertRaises(RuntimeError):
                await daemon.create_wallets(3)

    async def test_minimum_one(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = FarmDaemon("config.yaml", str(Path(td) / "t.db"))
            daemon.db = mock.Mock()
            daemon.config = {}
            with mock.patch("core.wallet.WalletManager") as wm_cls:
                inst = wm_cls.return_value
                inst.create_wallets = mock.AsyncMock(return_value=[])
                await daemon.create_wallets(0)
                self.assertEqual(inst.create_wallets.await_args[0][0], 1)


class TestApiWalletsCreate(unittest.IsolatedAsyncioTestCase):
    async def _client(self, daemon=None):
        app = create_app(_make_cfg(), cast(FarmDaemon, daemon or _StubDaemon()))
        client = TestClient(TestServer(app))
        await client.start_server()
        return client

    async def test_unauth_401(self):
        client = await self._client()
        async with client:
            resp = await client.post("/api/wallets", json={"count": 2})
            self.assertEqual(resp.status, 401)

    async def test_ok(self):
        client = await self._client()
        async with client:
            await client.post("/api/login/password", json={"password": "k" * 64})
            resp = await client.post("/api/wallets", json={"count": 2})
            self.assertEqual(resp.status, 200)
            body = await resp.json()
            self.assertTrue(body["ok"])
            self.assertEqual(body["created"], 2)

    async def test_bad_counts(self):
        client = await self._client()
        async with client:
            await client.post("/api/login/password", json={"password": "k" * 64})
            for payload, status in (({"count": 0}, 400), ({"count": 51}, 400), ({"count": "x"}, 400), ({}, 200)):
                resp = await client.post("/api/wallets", json=payload)
                self.assertEqual(resp.status, status, payload)

    async def test_no_json(self):
        client = await self._client()
        async with client:
            await client.post("/api/login/password", json={"password": "k" * 64})
            resp = await client.post("/api/wallets", data=b"{broken", headers={"Content-Type": "application/json"})
            self.assertEqual(resp.status, 400)

    async def test_no_db_500(self):
        client = await self._client(_NoDbDaemon())
        async with client:
            await client.post("/api/login/password", json={"password": "k" * 64})
            resp = await client.post("/api/wallets", json={"count": 2})
            self.assertEqual(resp.status, 500)


if __name__ == "__main__":
    unittest.main(verbosity=2)
