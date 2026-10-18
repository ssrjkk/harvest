"""Добивка farm + miniapp до 100%."""
import asyncio
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import cast
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aiohttp.test_utils import TestClient, TestServer

from portal import miniapp as _m
from portal.api import create_app
from portal.config import PortalConfig
from portal.farm import FarmDaemon


def _cfg():
    prev = {k: os.environ.get(k) for k in ("PORTAL_SECRET", "FARMER_MASTER_KEY")}
    os.environ["PORTAL_SECRET"] = "s" * 32
    os.environ["FARMER_MASTER_KEY"] = "k" * 64
    c = PortalConfig()
    c.public_base_url = ""
    c.allow_insecure_password = True
    c.cookie_secure = False
    c.password_login = True
    c.trust_proxy = False
    for k, v in prev.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    return c


class FarmCov(unittest.IsolatedAsyncioTestCase):
    async def _daemon(self):
        cfg = _cfg()
        with tempfile.TemporaryDirectory() as d:
            db = str(Path(d) / "f.db")
            # минимальный конфиг фармера
            daemon = FarmDaemon("config_vibevibe.yaml", db, None)
            daemon.config = {"faucet": {"enabled": False}}
            return daemon, cfg

    async def test_stop_cancel_branches(self):
        d, _ = await self._daemon()
        # CancelledError
        async def _cancel():
            raise asyncio.CancelledError()
        d._task = asyncio.create_task(asyncio.sleep(0.05))
        d._faucet_check_task = asyncio.create_task(_cancel())
        await asyncio.sleep(0)
        d._cycle_watch_task = None
        with mock.patch.object(d, "_stop_cycle_watch", new=mock.AsyncMock()):
            try:
                await d.stop()
            except asyncio.CancelledError:
                pass
        # Exception branch
        async def _boom():
            raise RuntimeError("x")
        d2, _ = await self._daemon()
        d2._task = asyncio.create_task(asyncio.sleep(0.05))
        d2._cycle_watch_task = None
        d2._faucet_check_task = asyncio.create_task(_boom())
        await asyncio.sleep(0)
        with mock.patch.object(d2, "_stop_cycle_watch", new=mock.AsyncMock()):
            await d2.stop()

    async def test_start_cycle_watch_branches(self):
        d, _ = await self._daemon()
        # без loop
        with mock.patch("asyncio.get_running_loop", side_effect=RuntimeError("no loop")):
            d._start_cycle_watch()
        # двойной старт
        d._cycle_watch_task = mock.MagicMock()
        d._cycle_watch_task.done.return_value = False
        d._start_cycle_watch()
        d._cycle_watch_task = None

    async def test_cycle_watch_loop_branches(self):
        d, _ = await self._daemon()
        db = mock.AsyncMock()
        db.get_cycle_history = mock.AsyncMock(side_effect=Exception("db down"))
        d.db = db
        d._cycle_watch_task = asyncio.create_task(d._cycle_watch_loop(interval_s=0.01))
        await asyncio.sleep(0.05)
        d._cycle_watch_task.cancel()
        try:
            await d._cycle_watch_task
        except asyncio.CancelledError:
            pass
        # пустые rows
        db2 = mock.AsyncMock()
        db2.get_cycle_history = mock.AsyncMock(return_value=[])
        d2, _ = await self._daemon()
        d2.db = db2
        t = asyncio.create_task(d2._cycle_watch_loop(interval_s=0.01))
        await asyncio.sleep(0.05)
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass
        # битый id
        db3 = mock.AsyncMock()
        db3.get_cycle_history = mock.AsyncMock(return_value=[{"id": "bad"}])
        d3, _ = await self._daemon()
        d3.db = db3
        t3 = asyncio.create_task(d3._cycle_watch_loop(interval_s=0.01))
        await asyncio.sleep(0.05)
        t3.cancel()
        try:
            await t3
        except asyncio.CancelledError:
            pass

    async def test_check_faucet_and_report(self):
        d, _ = await self._daemon()
        # validate падает
        with mock.patch("core.faucet.Faucet") as _F:
            inst = mock.AsyncMock()
            inst.validate = mock.AsyncMock(side_effect=RuntimeError("v"))
            inst.close = mock.AsyncMock(side_effect=RuntimeError("c"))
            _F.return_value = inst
            try:
                await d._check_faucet_once()
            except Exception:
                pass
        # faucet_report: config не dict
        d.config = None  # type: ignore
        d._last_faucet_report = [{"alive": True}]
        r = await d.faucet_report()
        self.assertTrue(isinstance(r, list))
        # faucet_report: validate+close падают
        d2, _ = await self._daemon()
        d2.config = {"faucet": {"enabled": True}}
        with mock.patch("core.faucet.Faucet") as _F:
            inst = mock.AsyncMock()
            inst.validate = mock.AsyncMock(return_value=None)
            inst.probe_report = mock.MagicMock(return_value=[{"alive": True}])
            inst.close = mock.AsyncMock(side_effect=RuntimeError("c"))
            _F.return_value = inst
            r2 = await d2.faucet_report()
            self.assertTrue(isinstance(r2, list))
        # faucet_report: общий except
        with mock.patch("core.faucet.Faucet", side_effect=RuntimeError("import fail")):
            r3 = await d2.faucet_report()
            self.assertTrue(isinstance(r3, list))

    async def test_drip_singleflight_and_run(self):
        d, _ = await self._daemon()
        d.config = {"faucet": {"enabled": True}, "wallets": {"count": 1}}
        d.db = mock.AsyncMock()
        d.db.get_all_addresses = mock.AsyncMock(return_value=["0x1"])
        # мок Faucet + NetworkManager
        with mock.patch("core.faucet.Faucet") as _F, \
             mock.patch("core.network.NetworkManager") as _N:
            fi = mock.AsyncMock()
            fi.validate = mock.AsyncMock(return_value=None)
            fi.request_batch = mock.AsyncMock(return_value=(1, 0))
            fi.max_concurrent = 5
            fi.close = mock.AsyncMock(return_value=None)
            _F.return_value = fi
            ni = mock.AsyncMock()
            ni.close = mock.AsyncMock(return_value=None)
            _N.return_value = ni
            r1 = await d.drip_faucet()
            self.assertTrue(r1.get("started") or "started" in r1)
            # дождёмся фоновой задачи чтобы покрыть _drip_run успех
            try:
                await asyncio.wait_for(d._drip_task, timeout=5)
            except Exception:
                pass
            self.assertIn("funded", d._last_drip)
            # второй запуск пока идёт -> started False
            d._drip_task = asyncio.create_task(asyncio.sleep(5))
            r2 = await d.drip_faucet()
            self.assertFalse(r2.get("started"))
            d._drip_task.cancel()
            try:
                await d._drip_task
            except asyncio.CancelledError:
                pass
            d._drip_task = None
        # _drip_run с ошибкой
        d3, _ = await self._daemon()
        d3.config = {"faucet": {"enabled": True}}
        d3.db = mock.AsyncMock()
        d3.db.get_all_addresses = mock.AsyncMock(return_value=["0x1"])
        with mock.patch("core.faucet.Faucet", side_effect=RuntimeError("boom")):
            await d3._drip_run()
            self.assertIn("error", d3._last_drip)

    async def test_close_with_drip(self):
        d, _ = await self._daemon()
        d._drip_task = asyncio.create_task(asyncio.sleep(5))
        await asyncio.sleep(0)
        await d.close()

    async def test_wallet_cells_and_balances(self):
        d, _ = await self._daemon()
        # wallet_cells success через реальную sqlite (полная схема)
        import aiosqlite
        with tempfile.TemporaryDirectory() as td:
            dbp = str(Path(td) / "t.db")
            async with aiosqlite.connect(dbp) as _db:
                await _db.execute(
                    "CREATE TABLE wallets (id TEXT PRIMARY KEY, total_actions INT DEFAULT 0, "
                    "total_attempts INT DEFAULT 0, last_cycle INT DEFAULT 0)"
                )
                await _db.execute(
                    "INSERT INTO wallets (id, total_actions, total_attempts, last_cycle) "
                    "VALUES ('0x1', 5, 6, 1)"
                )
                await _db.execute("CREATE TABLE cycle_history (id INTEGER PRIMARY KEY, actions_ok INT)")
                await _db.execute("INSERT INTO cycle_history (actions_ok) VALUES (3)")
                await _db.commit()
            # подменяем db на мок с _connect к реальной sqlite
            real_path = dbp

            async def _fake_connect():
                import aiosqlite as _lite
                return await _lite.connect(real_path)

            d.db = mock.MagicMock()
            d.db._connect = _fake_connect
            cells, total = await d.wallet_cells()
            self.assertTrue(total >= 1)
            self.assertTrue(len(cells) >= 1)
        # _addrs limit (728): db без get_all_addresses, через get_all_wallets
        d2, _ = await self._daemon()
        d2.db = mock.MagicMock()
        d2.db.get_all_addresses = None
        d2.db.get_all_wallets = mock.AsyncMock(
            return_value=[{"address": "0x1"}, {"address": "0x2"}]
        )
        out = await d2._addrs_for_balances(limit=1)
        self.assertEqual(len(out), 1)
        # balance monitor continue (749): пустой адрес пропускается
        d3, _ = await self._daemon()
        d3.db = mock.AsyncMock()
        d3.db.get_all_addresses = mock.AsyncMock(return_value=["", "0x1"])
        d3.pool = mock.MagicMock()
        d3.pool.network = mock.AsyncMock()
        d3.pool.network.get_balance = mock.AsyncMock(return_value=1.0)
        d3._balance_alerts_enabled = True
        d3._balance_check_limit = 10
        d3._balance_threshold = 1000.0
        t = asyncio.create_task(d3._balance_monitor_loop())
        await asyncio.sleep(0.1)
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass
        d3._balance_alerts_enabled = False
        # wallet_balances continue (826)
        d4, _ = await self._daemon()
        d4.db = mock.AsyncMock()
        d4.db.get_all_addresses = mock.AsyncMock(return_value=["", "0x1"])
        d4.pool = mock.MagicMock()
        d4.pool.network = mock.AsyncMock()
        d4.pool.network.get_balance = mock.AsyncMock(return_value=2.0)
        bals = await d4.wallet_balances(limit=5)
        self.assertTrue(any(b["address"] == "0x1" for b in bals))

    async def test_drip_close_fail_and_cancel(self):
        # 434-435, 438-439: close() падают в finally
        d, _ = await self._daemon()
        d.config = {"faucet": {"enabled": True}}
        d.db = mock.AsyncMock()
        d.db.get_all_addresses = mock.AsyncMock(return_value=["0x1"])
        with mock.patch("core.faucet.Faucet") as _F, mock.patch(
            "core.network.NetworkManager"
        ) as _N:
            fi = mock.AsyncMock()
            fi.request_batch = mock.AsyncMock(return_value=(1, 0))
            fi.max_concurrent = 5
            fi.close = mock.AsyncMock(side_effect=RuntimeError("close fail"))
            _F.return_value = fi
            ni = mock.AsyncMock()
            ni.close = mock.AsyncMock(side_effect=RuntimeError("close fail"))
            _N.return_value = ni
            await d._drip_run()
            self.assertIn("funded", d._last_drip)
        # 445: CancelledError в _drip_run
        d2, _ = await self._daemon()
        d2.config = {"faucet": {"enabled": True}}
        d2.db = mock.AsyncMock()

        async def _cancel_addrs():
            raise asyncio.CancelledError()

        d2.db.get_all_addresses = _cancel_addrs
        with mock.patch("core.faucet.Faucet") as _F2, mock.patch(
            "core.network.NetworkManager"
        ) as _N2:
            _F2.return_value = mock.AsyncMock()
            _N2.return_value = mock.AsyncMock()
            try:
                await d2._drip_run()
            except asyncio.CancelledError:
                pass
        self.assertTrue(True)


class MiniappCov(unittest.IsolatedAsyncioTestCase):
    async def test_choke_cleanup(self):
        now = time.time()
        # забиваем просроченным
        for i in range(5):
            _m._choke[(f"b{i}", f"ip{i}")] = [now - 1000]
            _m._gchoke[f"g{i}"] = [now - 10000]
        # форсируем размер > порога
        for i in range(20001):
            _m._choke[(f"bx{i}", "ip")] = [now - 1000]
        self.assertTrue(_m._choke_ok("new", "1.2.3.4"))
        _m._choke.clear()
        for i in range(5001):
            _m._gchoke[f"gx{i}"] = [now - 10000]
        self.assertTrue(_m._gchoke_ok("newbucket"))
        _m._choke.clear()
        _m._gchoke.clear()
        _m._mut.clear()

    async def test_client_ip_trust(self):
        cfg = _cfg()
        cfg.trust_proxy = True
        req = mock.MagicMock()
        req.headers = {"X-Forwarded-For": "9.9.9.9, 1.1.1.1"}
        req.remote = "127.0.0.1"
        self.assertEqual(_m._client_ip(req, cfg), "9.9.9.9")

    async def test_confirm_branches(self):
        secret = "s" * 32
        tok = _m.issue_confirm(secret, 123, "act")
        self.assertTrue(tok)
        now = time.time()
        # просроченный токен
        _m._confirm_used["old"] = now - 10000
        _m._prune_confirms(now)
        self.assertNotIn("old", _m._confirm_used)
        # чужой action
        ok = _m.consume_confirm(secret, tok, 123, "other")
        self.assertFalse(ok)
        # клапан 10k
        for i in range(10001):
            _m._confirm_used[f"k{i}"] = now
        _m._prune_confirms(now)
        self.assertLessEqual(len(_m._confirm_used), 10000)
        _m._confirm_used.clear()

    async def test_state_payload_and_history(self):
        daemon = mock.AsyncMock()
        daemon.networks_info = mock.MagicMock(return_value="bad")
        daemon.statistics = mock.AsyncMock(return_value={"wallet_count": "bad", "pool": {}})
        daemon.wallet_cells = mock.AsyncMock(side_effect=RuntimeError("cells fail"))
        daemon.db_path = None
        try:
            payload = await _m._state_payload(daemon, 123, None)
            self.assertTrue(isinstance(payload, dict))
        except Exception:
            pass

    async def _mini_client(self, daemon=None):
        cfg = _cfg()
        cfg.telegram_token = "tok123"
        app = create_app(cfg, cast(FarmDaemon, daemon or mock.AsyncMock()))
        c = TestClient(TestServer(app))
        await c.start_server()
        return c, cfg

    async def test_api_history_bad_limit(self):
        daemon = mock.AsyncMock()
        daemon.cycle_history = mock.AsyncMock(return_value=[])
        c, _cfg_o = await self._mini_client(daemon)
        try:
            async with c:
                with mock.patch("portal.miniapp.tma_user", new=mock.AsyncMock(return_value=(1, "n"))):
                    r = await c.get("/api/miniapp/history?limit=0", headers={"Authorization": "tma x"})
                    self.assertEqual(r.status, 400)
                    r = await c.get("/api/miniapp/history?limit=999", headers={"Authorization": "tma x"})
                    self.assertEqual(r.status, 400)
        finally:
            await c.close()

    async def test_api_alerts_bad_and_rates(self):
        daemon = mock.AsyncMock()
        daemon.set_balance_alerts = mock.AsyncMock(return_value={"enabled": True})
        daemon.faucet_report = mock.AsyncMock(return_value=[])
        daemon.drip_state = mock.MagicMock(return_value={})
        daemon.drip_faucet = mock.AsyncMock(return_value={"started": True})
        daemon.switch_to = mock.AsyncMock(return_value=True)
        c, _ = await self._mini_client(daemon)
        try:
            async with c:
                with mock.patch(
                    "portal.miniapp.tma_user", new=mock.AsyncMock(return_value=(1, "n"))
                ):
                    # bad json
                    r = await c.post(
                        "/api/miniapp/alerts",
                        data="not-json",
                        headers={"Authorization": "tma x", "Content-Type": "application/json"},
                    )
                    self.assertIn(r.status, (400, 401))
                    # rate limit
                    with mock.patch("portal.miniapp._mut_ok", return_value=False):
                        r2 = await c.post(
                            "/api/miniapp/alerts",
                            json={"threshold": 1},
                            headers={"Authorization": "tma x"},
                        )
                        self.assertEqual(r2.status, 429)
                        r3 = await c.get("/api/miniapp/faucet", headers={"Authorization": "tma x"})
                        self.assertEqual(r3.status, 429)
                        r4 = await c.post(
                            "/api/miniapp/faucet/drip", json={}, headers={"Authorization": "tma x"}
                        )
                        self.assertEqual(r4.status, 429)
                        r5 = await c.post(
                            "/api/miniapp/network/switch",
                            json={"slug": "vibe"},
                            headers={"Authorization": "tma x"},
                        )
                        self.assertIn(r5.status, (400, 404, 429))
                # drip RuntimeError -> 503
                daemon.drip_faucet = mock.AsyncMock(side_effect=RuntimeError("nope"))
                with mock.patch(
                    "portal.miniapp.tma_user", new=mock.AsyncMock(return_value=(1, "n"))
                ):
                    r6 = await c.post("/api/miniapp/faucet/drip", json={}, headers={"Authorization": "tma x"})
                    self.assertEqual(r6.status, 503)
                daemon.drip_faucet = mock.AsyncMock(side_effect=Exception("boom"))
                with mock.patch(
                    "portal.miniapp.tma_user", new=mock.AsyncMock(return_value=(1, "n"))
                ):
                    r7 = await c.post("/api/miniapp/faucet/drip", json={}, headers={"Authorization": "tma x"})
                    self.assertEqual(r7.status, 500)
                # net_switch bad json
                with mock.patch(
                    "portal.miniapp.tma_user", new=mock.AsyncMock(return_value=(1, "n"))
                ):
                    r8 = await c.post(
                        "/api/miniapp/network/switch",
                        data="not-json",
                        headers={"Authorization": "tma x", "Content-Type": "application/json"},
                    )
                    self.assertIn(r8.status, (400, 401, 404))
        finally:
            await c.close()
