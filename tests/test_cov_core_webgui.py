"""Добор покрытия core/web_gui.py до 100% (дополнение к tests/test_web_gui.py).

Подход: httpx AsyncClient с ASGITransport(app) + override _verify_request_auth->None,
_state с фейками (FakeDB/FakeNetwork/FakeWalletManager/FakeScheduler/
FakeGroupManager/FakeFaucet), monkeypatch load_config/resolve_master_key/
enforce_license_async/asyncio.sleep через unittest.mock.patch.
Стиль unittest (IsolatedAsyncioTestCase для async).
"""
import asyncio
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

import core.web_gui as web_gui
from core.web_gui import _sessions, _state, app


# ---------------------------------------------------------------- фейки ---
class FakeTask:
    def __init__(self, task_id, name=""):
        self.task_id = task_id
        self.name = name
        self.enabled = True

    def to_dict(self):
        return {"task_id": self.task_id, "name": self.name, "enabled": self.enabled}


class FakeDB:
    def __init__(self, wallets=None, stats=None):
        self._wallets = wallets if wallets is not None else [{"address": "0x1"}]
        self._stats = stats if stats is not None else {
            "total_wallets": 1, "total_actions": 10, "success_actions": 9}
        self.closed = False
        self._gm = FakeGroupManager()

    async def get_stats(self):
        return dict(self._stats)

    async def get_all_wallets(self):
        return list(self._wallets)

    async def get_top_wallets(self, n=10):
        return [{"address": "0x1"}]

    async def get_cycle_stats(self):
        return {"cycles": 1}

    async def get_all_addresses(self):
        return [w["address"] for w in self._wallets]

    async def init(self):
        return None

    async def close(self):
        self.closed = True

    def get_group_manager(self):
        return self._gm

    async def _connect(self):
        return object()


class FakeNetwork:
    def __init__(self, balance=1.0, diag=None):
        self._balance = balance
        self._diag = diag
        self.closed = False

    async def init(self):
        return None

    async def close(self):
        self.closed = True

    def diagnostics(self):
        if self._diag is not None:
            return dict(self._diag)
        return {"rpc_urls": [], "active": "", "chain_id": 1, "avg_ms": 1.0,
                "calls": 0, "errors": 0, "cb_state": "CLOSED",
                "cb_failures": 0, "rpc_rate": 0.0, "nodes": []}

    async def get_balance(self, addr):
        return self._balance


class FakeWalletManager:
    def __init__(self, db=None):
        self._db = db

    async def create_wallets(self, count):
        return [{"address": f"0x{i:040x}"} for i in range(count)]


class FakeScheduler:
    def __init__(self, fail_add=False):
        self.is_running = False
        self._tasks = {}
        self._fail_add = fail_add
        self.started = False
        self.stopped = False

    async def load(self):
        return None

    async def start(self):
        self.is_running = True
        self.started = True

    async def stop(self):
        self.is_running = False
        self.stopped = True

    def set_run_callback(self, cb):
        self._cb = cb

    def get_all_tasks(self):
        return [t.to_dict() for t in self._tasks.values()]

    def add_task(self, task_id, name="", interval_hours=0,
                 interval_minutes=0, run_at_time=None, cycles=1):
        if self._fail_add:
            raise RuntimeError("add failed")
        t = FakeTask(task_id, name)
        self._tasks[task_id] = t
        return t

    def update_task(self, task_id, **kw):
        t = self._tasks.get(task_id)
        if not t:
            return None
        for k, v in kw.items():
            setattr(t, k, v)
        return t

    def remove_task(self, task_id):
        if task_id in self._tasks:
            del self._tasks[task_id]
            return True
        return False


class FakeGroupManager:
    def __init__(self):
        self.groups = [{"id": 1, "name": "g"}]
        self._db = None

    def set_db(self, db):
        self._db = db

    async def get_all_groups(self):
        return list(self.groups)

    async def create_group(self, name, color="#007bff"):
        if name == "dup":
            raise ValueError("already exists")
        if name == "boom":
            raise RuntimeError("db boom")
        g = {"id": 2, "name": name, "color": color}
        return g

    async def delete_group(self, gid):
        return True

    async def add_bulk_to_group(self, addresses, gid):
        return len(addresses)

    async def remove_wallet_from_group(self, addr, gid):
        return True

    async def get_group_addresses(self, gid):
        return ["0x1"]

    async def get_wallet_groups(self, addr):
        return [{"id": 1}]


class FakeFaucet:
    def __init__(self, ok=True, exc=None):
        self._ok = ok
        self._exc = exc

    async def ensure_balance(self, network, address):
        if self._exc:
            raise self._exc
        return self._ok


class FakeWS:
    def __init__(self, cookies=None, recv_seq=None):
        self.cookies = cookies or {}
        self.sent = []
        self.accepted = False
        self.closed = None
        self._recv = list(recv_seq) if recv_seq is not None else []
        from fastapi import WebSocketDisconnect
        self._Disc = WebSocketDisconnect

    async def accept(self):
        self.accepted = True

    async def send_text(self, msg):
        self.sent.append(msg)

    async def receive_text(self):
        if self._recv:
            item = self._recv.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item
        raise self._Disc(code=1008)

    async def close(self, code=1008, reason=""):
        self.closed = (code, reason)


def _save_state():
    return dict(_state)


def _restore_state(saved):
    for k in saved:
        _state[k] = saved[k]
    _state["clients"] = saved.get("clients", [])
    _state["logs"] = saved.get("logs", [])


async def _no_auth():
    return None


class HttpxMixin:
    async def _request(self, method, path, **kw):
        cookies = kw.pop("cookies", None)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            if cookies:
                c.cookies.set("harvest_sid", cookies.get("harvest_sid", ""))
            return await c.request(method, path, **kw)


# ------------------------------------------------- auth helpers 68-96 ---
class TestAuthHelpers(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._saved = _save_state()
        self._saved_mk = web_gui.MASTER_KEY
        self._saved_sessions = dict(_sessions)

    def tearDown(self):
        _restore_state(self._saved)
        web_gui.MASTER_KEY = self._saved_mk
        _sessions.clear()
        _sessions.update(self._saved_sessions)

    def test_require_auth_both(self):
        web_gui.MASTER_KEY = ""
        self.assertFalse(web_gui._require_auth())
        web_gui.MASTER_KEY = "x"
        self.assertTrue(web_gui._require_auth())

    def test_check_session_none_and_missing(self):
        self.assertFalse(web_gui._check_session(None))
        self.assertFalse(web_gui._check_session("nope"))

    def test_check_session_expired(self):
        tok = "tok-exp"
        _sessions[tok] = time.time() - 5
        self.assertFalse(web_gui._check_session(tok))
        self.assertNotIn(tok, _sessions)

    def test_check_session_ok(self):
        tok = "tok-ok"
        _sessions[tok] = time.time() + 100
        self.assertTrue(web_gui._check_session(tok))

    def test_issue_session(self):
        tok, ttl = web_gui._issue_session()
        self.assertIn(tok, _sessions)
        self.assertEqual(ttl, web_gui.SESSION_TTL)

    async def test_verify_request_no_auth(self):
        web_gui.MASTER_KEY = ""
        req = MagicMock()
        req.cookies = {}
        self.assertIsNone(await web_gui._verify_request_auth(req))

    async def test_verify_request_ok(self):
        web_gui.MASTER_KEY = "s"
        tok, _ = web_gui._issue_session()
        req = MagicMock()
        req.cookies = {"harvest_sid": tok}
        self.assertIsNone(await web_gui._verify_request_auth(req))

    async def test_verify_request_unauth(self):
        web_gui.MASTER_KEY = "s"
        req = MagicMock()
        req.cookies = {}
        with self.assertRaises(Exception):
            await web_gui._verify_request_auth(req)

    async def test_verify_ws_no_auth(self):
        web_gui.MASTER_KEY = ""
        ws = FakeWS()
        self.assertIsNone(await web_gui._verify_ws_auth(ws))

    async def test_verify_ws_ok(self):
        web_gui.MASTER_KEY = "s"
        tok, _ = web_gui._issue_session()
        ws = FakeWS(cookies={"harvest_sid": tok})
        self.assertIsNone(await web_gui._verify_ws_auth(ws))

    async def test_verify_ws_fail(self):
        from fastapi import WebSocketDisconnect
        web_gui.MASTER_KEY = "s"
        ws = FakeWS(cookies={})
        with self.assertRaises(WebSocketDisconnect):
            await web_gui._verify_ws_auth(ws)
        self.assertIsNotNone(ws.closed)


# ------------------------------------------------- log handler 107-116 ---
class TestLogHandler(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._saved = _save_state()
        _state["logs"] = []
        _state["clients"] = []

    def tearDown(self):
        _restore_state(self._saved)

    async def test_emit_with_clients_ok(self):
        import logging
        c = AsyncMock()
        c.send_text = AsyncMock()
        _state["clients"] = [c]
        h = web_gui.WebSocketLogHandler()
        h.setFormatter(logging.Formatter("%(message)s"))
        rec = logging.LogRecord("t", logging.INFO, "", 0, "hello", (), None)
        h.emit(rec)
        await asyncio.sleep(0.05)
        self.assertIn("hello", _state["logs"][0])

    def test_emit_client_raises_sync(self):
        import logging
        bad = MagicMock()
        bad.send_text = MagicMock(side_effect=RuntimeError("x"))
        _state["clients"] = [bad]
        h = web_gui.WebSocketLogHandler()
        h.setFormatter(logging.Formatter("%(message)s"))
        rec = logging.LogRecord("t", logging.INFO, "", 0, "m2", (), None)
        h.emit(rec)  # inner except pass -> 107-116 except branch
        self.assertTrue(any("m2" in m for m in _state["logs"]))

    def test_emit_outer_except(self):
        import logging
        _state["logs"] = None  # append will fail -> outer except 115-116
        h = web_gui.WebSocketLogHandler()
        h.setFormatter(logging.Formatter("%(message)s"))
        rec = logging.LogRecord("t", logging.INFO, "", 0, "m3", (), None)
        h.emit(rec)  # must not raise


# ------------------------------------------------- broadcast 140-150 ---
class TestBroadcast(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._saved = _save_state()

    def tearDown(self):
        _restore_state(self._saved)

    async def test_status_send_fail(self):
        _state["db"] = FakeDB()
        _state["is_farming"] = False
        _state["scheduler"] = None
        _state["current_network"] = "default"
        _state["balance_alerts_enabled"] = False
        bad = AsyncMock()
        bad.send_text = AsyncMock(side_effect=RuntimeError("send fail"))
        good = AsyncMock()
        good.send_text = AsyncMock()
        _state["clients"] = [bad, good]
        await web_gui.broadcast_status()  # 140-141
        good.send_text.assert_called()

    async def test_status_with_scheduler_and_rate(self):
        sched = FakeScheduler()
        sched.is_running = True
        _state["db"] = FakeDB(stats={"total_wallets": 2, "total_actions": 4, "success_actions": 3})
        _state["scheduler"] = sched
        _state["is_farming"] = True
        _state["current_network"] = "net"
        _state["balance_alerts_enabled"] = True
        c = AsyncMock()
        c.send_text = AsyncMock()
        _state["clients"] = [c]
        await web_gui.broadcast_status()
        c.send_text.assert_called()

    async def test_event_send_fail(self):
        bad = AsyncMock()
        bad.send_text = AsyncMock(side_effect=RuntimeError("x"))
        _state["clients"] = [bad]
        await web_gui.broadcast_event("ev", {"a": 1})  # 149-150


# ------------------------------------------------- lifespan 161-169 ---
class TestLifespan(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._saved = _save_state()

    def tearDown(self):
        _restore_state(self._saved)

    async def test_lifespan_full_cleanup(self):
        sched = FakeScheduler()
        sched.is_running = True
        net = FakeNetwork()
        db = FakeDB()
        bt = asyncio.create_task(asyncio.sleep(60))
        ft = asyncio.create_task(asyncio.sleep(60))
        _state["scheduler"] = sched
        _state["balance_alerts_task"] = bt
        _state["farming_task"] = ft
        _state["network"] = net
        _state["db"] = db
        async with web_gui.lifespan(app):
            pass
        await asyncio.sleep(0.05)
        self.assertTrue(bt.done())
        self.assertTrue(ft.done())
        try:
            await bt
        except asyncio.CancelledError:
            pass
        try:
            await ft
        except asyncio.CancelledError:
            pass
        import logging
        # cleanup handler added by lifespan to avoid pollution
        for h in list(logging.getLogger().handlers):
            if isinstance(h, web_gui.WebSocketLogHandler):
                logging.getLogger().removeHandler(h)
                break


# ------------------------------------------------- scan ---
class TestScan(unittest.TestCase):
    def setUp(self):
        self._saved = _save_state()

    def tearDown(self):
        _restore_state(self._saved)

    def test_scan_empty_dir(self):
        import os
        import tempfile
        cwd = os.getcwd()
        td = tempfile.mkdtemp()
        try:
            os.chdir(td)
            names = web_gui._scan_networks()
            self.assertEqual(names, [])
            os.chdir(cwd)
        finally:
            try:
                os.chdir(cwd)
            except Exception:
                pass
            import shutil
            shutil.rmtree(td, ignore_errors=True)


# ------------------------------------------------- auth routes ---
class TestAuthRoutes(unittest.IsolatedAsyncioTestCase, HttpxMixin):
    def setUp(self):
        self._saved = _save_state()
        self._mk = web_gui.MASTER_KEY
        self._sess = dict(_sessions)
        app.dependency_overrides.clear()

    def tearDown(self):
        _restore_state(self._saved)
        web_gui.MASTER_KEY = self._mk
        _sessions.clear()
        _sessions.update(self._sess)
        app.dependency_overrides.clear()

    async def test_login_disabled(self):
        web_gui.MASTER_KEY = ""
        r = await self._request("POST", "/api/auth/login", json={"key": "anything"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])

    async def test_login_invalid(self):
        web_gui.MASTER_KEY = "secret"
        r = await self._request("POST", "/api/auth/login", json={"key": "wrong"})
        self.assertEqual(r.status_code, 401)
        self.assertFalse(r.json()["ok"])

    async def test_login_valid(self):
        web_gui.MASTER_KEY = "secret"
        r = await self._request("POST", "/api/auth/login", json={"key": "secret"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])
        self.assertIn("harvest_sid", r.cookies)

    async def test_check_no_auth_required(self):
        web_gui.MASTER_KEY = ""
        r = await self._request("GET", "/api/auth/check")
        d = r.json()
        self.assertTrue(d["ok"])
        self.assertFalse(d["auth_required"])

    async def test_check_auth_required_unauth_and_auth(self):
        web_gui.MASTER_KEY = "secret"
        r = await self._request("GET", "/api/auth/check")
        d = r.json()
        self.assertTrue(d["auth_required"])
        self.assertFalse(d["authenticated"])
        tok, _ = web_gui._issue_session()
        r2 = await self._request("GET", "/api/auth/check", cookies={"harvest_sid": tok})
        self.assertTrue(r2.json()["authenticated"])

    async def test_logout(self):
        web_gui.MASTER_KEY = "secret"
        tok, _ = web_gui._issue_session()
        r = await self._request("POST", "/api/auth/logout", cookies={"harvest_sid": tok})
        self.assertTrue(r.json()["ok"])
        self.assertNotIn(tok, _sessions)
        r2 = await self._request("POST", "/api/auth/logout")
        self.assertTrue(r2.json()["ok"])


# ------------------------------------------------- root 276 ---
class TestRoot(unittest.IsolatedAsyncioTestCase, HttpxMixin):
    def setUp(self):
        self._saved = _save_state()
        app.dependency_overrides.clear()

    def tearDown(self):
        _restore_state(self._saved)
        app.dependency_overrides.clear()

    async def test_root_exists(self):
        r = await self._request("GET", "/")
        self.assertEqual(r.status_code, 200)

    async def test_root_fallback(self):
        with patch.object(web_gui, "static_dir", Path("/nonexistent-xyz-123")):
            r = await self._request("GET", "/")
            self.assertEqual(r.status_code, 200)
            self.assertIn("Frontend not found", r.text)


# ------------------------------------------------- status/stats/rpc ---
class TestStatusStatsRpc(unittest.IsolatedAsyncioTestCase, HttpxMixin):
    def setUp(self):
        self._saved = _save_state()
        app.dependency_overrides[web_gui._verify_request_auth] = _no_auth

    def tearDown(self):
        _restore_state(self._saved)
        app.dependency_overrides.clear()

    async def test_status_zero_rate(self):
        _state["db"] = FakeDB(stats={"total_wallets": 0, "total_actions": 0, "success_actions": 0})
        _state["is_farming"] = False
        _state["config"] = None
        _state["scheduler"] = None
        _state["current_network"] = "default"
        _state["balance_alerts_enabled"] = False
        r = await self._request("GET", "/api/status")
        d = r.json()
        self.assertEqual(d["success_rate"], "0%")

    async def test_stats_exception(self):
        db = AsyncMock()
        db.get_stats = AsyncMock(side_effect=RuntimeError("db fail"))
        _state["db"] = db
        r = await self._request("GET", "/api/stats")
        self.assertFalse(r.json()["ok"])

    async def test_rpc_zero_calls(self):
        net = FakeNetwork(diag={"rpc_urls": [], "active": "", "chain_id": 1,
                                "avg_ms": 0.0, "calls": 0, "errors": 0,
                                "cb_state": "CLOSED", "cb_failures": 0,
                                "rpc_rate": 0.0, "nodes": []})
        _state["network"] = net
        r = await self._request("GET", "/api/rpc/health")
        d = r.json()
        self.assertTrue(d["ok"])
        self.assertEqual(d["rpc"]["error_rate"], "0%")

    async def test_rpc_exception(self):
        net = MagicMock()
        net.diagnostics = MagicMock(side_effect=RuntimeError("diag fail"))
        _state["network"] = net
        r = await self._request("GET", "/api/rpc/health")
        self.assertFalse(r.json()["ok"])


# ------------------------------------------------- init 311-343 ---
class TestInit(unittest.IsolatedAsyncioTestCase, HttpxMixin):
    def setUp(self):
        self._saved = _save_state()
        app.dependency_overrides[web_gui._verify_request_auth] = _no_auth

    def tearDown(self):
        _restore_state(self._saved)
        app.dependency_overrides.clear()

    async def test_init_master_key_error(self):
        _state["config"] = None
        with patch("core.web_gui.load_config", return_value={"database": {"path": ":m:"}, "faucet": {}}), \
             patch("core.web_gui.enforce_license_async", new=AsyncMock(return_value=None)), \
             patch("core.web_gui.resolve_master_key", side_effect=RuntimeError("bad key")):
            r = await self._request("POST", "/api/init")
            d = r.json()
            self.assertFalse(d["ok"])
            self.assertIn("Master key", d["message"])

    async def test_init_outer_exception(self):
        _state["config"] = None
        with patch("core.web_gui.load_config", side_effect=RuntimeError("cfg boom")):
            r = await self._request("POST", "/api/init")
            self.assertFalse(r.json()["ok"])

    async def test_init_faucet_enabled(self):
        _state["config"] = None
        _state["db"] = None
        _state["network"] = None
        _state["scheduler"] = None
        _state["group_manager"] = None
        _state["faucet"] = None
        _state["wallet_manager"] = None
        fake_db = FakeDB()
        fake_net = FakeNetwork()
        fake_sched = FakeScheduler()
        with patch("core.web_gui.load_config",
                   return_value={"database": {"path": ":m:"}, "faucet": {"enabled": True}}), \
             patch("core.web_gui.enforce_license_async", new=AsyncMock(return_value=None)), \
             patch("core.web_gui.resolve_master_key", return_value="k"), \
             patch("core.web_gui.Database", return_value=fake_db), \
             patch("core.web_gui.NetworkManager", return_value=fake_net), \
             patch("core.web_gui.WalletManager", return_value=MagicMock()), \
             patch("core.web_gui.Faucet", return_value=FakeFaucet()), \
             patch("core.web_gui.Scheduler", return_value=fake_sched):
            r = await self._request("POST", "/api/init")
            d = r.json()
            self.assertTrue(d["ok"])
            self.assertIsNotNone(_state["faucet"])
            # cleanup scheduler task effects
            _state["scheduler"] = None

    async def test_init_enforce_license_fail(self):
        _state["config"] = None
        with patch("core.web_gui.load_config", return_value={"database": {"path": "x"}}), \
             patch("core.web_gui.enforce_license_async", new=AsyncMock(side_effect=RuntimeError("lic"))):
            r = await self._request("POST", "/api/init")
            self.assertFalse(r.json()["ok"])


# ------------------------------------------------- wallets/faucet ---
class TestWalletsFaucet(unittest.IsolatedAsyncioTestCase, HttpxMixin):
    def setUp(self):
        self._saved = _save_state()
        app.dependency_overrides[web_gui._verify_request_auth] = _no_auth

    def tearDown(self):
        _restore_state(self._saved)
        app.dependency_overrides.clear()

    async def test_create_wallets_exception(self):
        wm = AsyncMock()
        wm.create_wallets = AsyncMock(side_effect=RuntimeError("wm boom"))
        _state["wallet_manager"] = wm
        r = await self._request("POST", "/api/wallets/create", json={"count": 1})
        self.assertFalse(r.json()["ok"])

    async def test_faucet_per_wallet_exception(self):
        _state["db"] = FakeDB(wallets=[{"address": "0xaaa"}, {"address": "0xbbb"}])
        _state["network"] = FakeNetwork()
        fc = AsyncMock()
        fc.ensure_balance = AsyncMock(side_effect=[RuntimeError("one fail"), True])
        _state["faucet"] = fc
        r = await self._request("POST", "/api/faucet/request")
        d = r.json()
        self.assertTrue(d["ok"])
        self.assertEqual(d["success"], 1)

    async def test_faucet_outer_exception(self):
        db = AsyncMock()
        db.get_all_wallets = AsyncMock(side_effect=RuntimeError("db boom"))
        _state["db"] = db
        _state["faucet"] = FakeFaucet()
        _state["network"] = FakeNetwork()
        r = await self._request("POST", "/api/faucet/request")
        self.assertFalse(r.json()["ok"])


# ------------------------------------------------- farming start/stop ---
class TestFarmingStartStop(unittest.IsolatedAsyncioTestCase, HttpxMixin):
    def setUp(self):
        self._saved = _save_state()
        app.dependency_overrides[web_gui._verify_request_auth] = _no_auth

    def tearDown(self):
        t = _state.get("farming_task")
        if t is not None and not getattr(t, "done", lambda: True)():
            try:
                t.cancel()
            except Exception:
                pass
        _restore_state(self._saved)
        app.dependency_overrides.clear()

    async def test_start_already_farming(self):
        _state["is_farming"] = True
        r = await self._request("POST", "/api/farming/start")
        self.assertFalse(r.json()["ok"])

    async def test_start_outer_exception(self):
        _state["is_farming"] = False
        db = AsyncMock()
        db.get_all_wallets = AsyncMock(side_effect=RuntimeError("boom"))
        _state["db"] = db
        _state["network"] = FakeNetwork()
        r = await self._request("POST", "/api/farming/start")
        self.assertFalse(r.json()["ok"])

    async def test_farming_loop_no_wallets_branch(self):
        # first call (pre-check) non-empty, loop calls: [] then [w] then stop
        db = AsyncMock()
        db.get_all_wallets = AsyncMock(side_effect=[
            [{"address": "0x1"}],  # pre-check in start_farming
            [],  # loop iter1 -> no wallets branch (414-416)
            [{"address": "0x1"}],  # loop iter2
        ])
        _state["db"] = db
        _state["network"] = FakeNetwork()
        _state["config"] = {"x": 1}
        _state["is_farming"] = False
        pool = AsyncMock()

        async def _run_once(*a, **k):
            _state["is_farming"] = False  # exit after iter2

        pool.run_once = _run_once
        with patch("core.web_gui.FarmerPool", return_value=pool), \
             patch("core.web_gui.asyncio.sleep", new=AsyncMock(return_value=None)):
            r = await self._request("POST", "/api/farming/start")
            self.assertTrue(r.json()["ok"])
            task = _state["farming_task"]
            if task is not None:
                try:
                    await asyncio.wait_for(task, timeout=5)
                except asyncio.TimeoutError:
                    task.cancel()

    async def test_farming_loop_cancelled(self):
        db = FakeDB(wallets=[{"address": "0x1"}])
        _state["db"] = db
        _state["network"] = FakeNetwork()
        _state["config"] = {"x": 1}
        _state["is_farming"] = False
        pool = AsyncMock()
        pool.run_once = AsyncMock(side_effect=asyncio.CancelledError())
        with patch("core.web_gui.FarmerPool", return_value=pool):
            r = await self._request("POST", "/api/farming/start")
            self.assertTrue(r.json()["ok"])
            task = _state["farming_task"]
            try:
                await asyncio.wait_for(task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
            self.assertFalse(_state["is_farming"])

    async def test_farming_loop_generic_error(self):
        db = FakeDB(wallets=[{"address": "0x1"}])
        _state["db"] = db
        _state["network"] = FakeNetwork()
        _state["config"] = {"x": 1}
        _state["is_farming"] = False
        pool = AsyncMock()
        pool.run_once = AsyncMock(side_effect=RuntimeError("pool boom"))
        with patch("core.web_gui.FarmerPool", return_value=pool):
            r = await self._request("POST", "/api/farming/start")
            self.assertTrue(r.json()["ok"])
            task = _state["farming_task"]
            try:
                await asyncio.wait_for(task, timeout=5)
            except asyncio.TimeoutError:
                if task is not None:
                    task.cancel()
            self.assertFalse(_state["is_farming"])

    async def test_stop_success(self):
        _state["is_farming"] = True
        _state["db"] = FakeDB()
        async def _sleep_forever():
            await asyncio.sleep(60)
        t = asyncio.create_task(_sleep_forever())
        await asyncio.sleep(0)
        _state["farming_task"] = t
        r = await self._request("POST", "/api/farming/stop")
        d = r.json()
        self.assertTrue(d["ok"])
        self.assertIsNone(_state["farming_task"])

    async def test_stop_exception(self):
        _state["is_farming"] = True
        _state["farming_task"] = None
        with patch("core.web_gui.broadcast_status", new=AsyncMock(side_effect=RuntimeError("bc"))):
            r = await self._request("POST", "/api/farming/stop")
            self.assertFalse(r.json()["ok"])
        _state["is_farming"] = False


# ------------------------------------------------- networks/switch 524-545 ---
class TestSwitch(unittest.IsolatedAsyncioTestCase, HttpxMixin):
    def setUp(self):
        self._saved = _save_state()
        app.dependency_overrides[web_gui._verify_request_auth] = _no_auth

    def tearDown(self):
        _restore_state(self._saved)
        app.dependency_overrides.clear()

    async def test_switch_success_default(self):
        import os
        import tempfile
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as td:
            os.chdir(td)
            try:
                Path("config.yaml").write_text("x: 1", encoding="utf-8")
                _state["is_farming"] = False
                _state["network"] = FakeNetwork()
                _state["db"] = FakeDB()
                _state["current_network"] = "old"
                with patch("core.web_gui.init_system", new=AsyncMock(return_value={"ok": True})):
                    r = await self._request("POST", "/api/networks/switch", json={"network": "default"})
                    d = r.json()
                    self.assertTrue(d["ok"])
                    self.assertEqual(_state["current_network"], "default")
            finally:
                os.chdir(cwd)

    async def test_switch_success_nondefault(self):
        import os
        import tempfile
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as td:
            os.chdir(td)
            try:
                Path("config_testnet.yaml").write_text("x: 1", encoding="utf-8")
                _state["is_farming"] = False
                _state["network"] = FakeNetwork()
                _state["db"] = FakeDB()
                with patch("core.web_gui.init_system", new=AsyncMock(return_value={"ok": True})):
                    r = await self._request("POST", "/api/networks/switch", json={"network": "testnet"})
                    self.assertTrue(r.json()["ok"])
                    self.assertEqual(_state["current_network"], "testnet")
            finally:
                os.chdir(cwd)

    async def test_switch_exception(self):
        import os
        import tempfile
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as td:
            os.chdir(td)
            try:
                Path("config_testnet.yaml").write_text("x: 1", encoding="utf-8")
                _state["is_farming"] = False
                bad_net = AsyncMock()
                bad_net.close = AsyncMock(side_effect=RuntimeError("close boom"))
                _state["network"] = bad_net
                _state["db"] = FakeDB()
                r = await self._request("POST", "/api/networks/switch", json={"network": "testnet"})
                self.assertFalse(r.json()["ok"])
            finally:
                os.chdir(cwd)


# ------------------------------------------------- scheduler 558-612 ---
class TestSchedulerCov(unittest.IsolatedAsyncioTestCase, HttpxMixin):
    def setUp(self):
        self._saved = _save_state()
        app.dependency_overrides[web_gui._verify_request_auth] = _no_auth

    def tearDown(self):
        _restore_state(self._saved)
        app.dependency_overrides.clear()

    async def test_create_not_init(self):
        _state["scheduler"] = None
        r = await self._request("POST", "/api/scheduler/tasks", json={"task_id": "a"})
        self.assertFalse(r.json()["ok"])

    async def test_create_exception(self):
        _state["scheduler"] = FakeScheduler(fail_add=True)
        r = await self._request("POST", "/api/scheduler/tasks",
                                json={"task_id": "a", "interval_hours": 1})
        self.assertFalse(r.json()["ok"])

    async def test_create_ok(self):
        _state["scheduler"] = FakeScheduler()
        r = await self._request("POST", "/api/scheduler/tasks",
                                json={"task_id": "a", "interval_hours": 1})
        self.assertTrue(r.json()["ok"])

    async def test_update_not_init(self):
        _state["scheduler"] = None
        r = await self._request("PUT", "/api/scheduler/tasks/t1", json={"name": "x"})
        self.assertFalse(r.json()["ok"])

    async def test_delete_not_init(self):
        _state["scheduler"] = None
        r = await self._request("DELETE", "/api/scheduler/tasks/t1")
        self.assertFalse(r.json()["ok"])

    async def test_start_not_init(self):
        _state["scheduler"] = None
        r = await self._request("POST", "/api/scheduler/start")
        self.assertFalse(r.json()["ok"])

    async def test_stop_not_init(self):
        _state["scheduler"] = None
        r = await self._request("POST", "/api/scheduler/stop")
        self.assertFalse(r.json()["ok"])

    async def test_start_ok(self):
        _state["scheduler"] = FakeScheduler()
        _state["db"] = FakeDB()
        r = await self._request("POST", "/api/scheduler/start")
        self.assertTrue(r.json()["ok"])

    async def test_stop_ok(self):
        s = FakeScheduler()
        s.is_running = True
        _state["scheduler"] = s
        _state["db"] = FakeDB()
        r = await self._request("POST", "/api/scheduler/stop")
        self.assertTrue(r.json()["ok"])


# ------------------------------------------------- scheduled farm 629-639 ---
class TestScheduledFarmCov(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._saved = _save_state()

    def tearDown(self):
        _restore_state(self._saved)

    async def test_break_mid_loop(self):
        db = FakeDB(wallets=[{"address": "0x1"}])
        _state["db"] = db
        _state["network"] = FakeNetwork()
        _state["config"] = {"x": 1}
        _state["is_farming"] = False
        pool = AsyncMock()

        async def _run_once(*a, **k):
            _state["is_farming"] = False

        pool.run_once = _run_once
        with patch("core.web_gui.FarmerPool", return_value=pool):
            await web_gui._scheduled_farm(5)  # 629 break on 2nd iter
        self.assertFalse(_state["is_farming"])

    async def test_no_wallets_break(self):
        _state["db"] = FakeDB(wallets=[])
        _state["network"] = FakeNetwork()
        _state["config"] = {"x": 1}
        _state["is_farming"] = False
        with patch("core.web_gui.FarmerPool", return_value=AsyncMock()):
            await web_gui._scheduled_farm(2)  # 633-634
        self.assertFalse(_state["is_farming"])

    async def test_run_exception(self):
        _state["db"] = FakeDB(wallets=[{"address": "0x1"}])
        _state["network"] = FakeNetwork()
        _state["config"] = {"x": 1}
        _state["is_farming"] = False
        pool = AsyncMock()
        pool.run_once = AsyncMock(side_effect=RuntimeError("pool fail"))
        with patch("core.web_gui.FarmerPool", return_value=pool):
            await web_gui._scheduled_farm(1)  # 638-639
        self.assertFalse(_state["is_farming"])


# ------------------------------------------------- balance alerts 660-703 ---
class TestBalanceAlertsCov(unittest.IsolatedAsyncioTestCase, HttpxMixin):
    def setUp(self):
        self._saved = _save_state()
        app.dependency_overrides[web_gui._verify_request_auth] = _no_auth

    def tearDown(self):
        t = _state.get("balance_alerts_task")
        if t is not None:
            try:
                if not t.done():
                    t.cancel()
            except Exception:
                pass
        _restore_state(self._saved)
        app.dependency_overrides.clear()

    async def test_disable_with_task(self):
        async def _never():
            await asyncio.sleep(60)
        t = asyncio.create_task(_never())
        await asyncio.sleep(0)
        _state["balance_alerts_task"] = t
        _state["balance_alerts_enabled"] = True
        r = await self._request("POST", "/api/balance-alerts",
                                json={"enabled": False, "threshold": 0.0})
        self.assertTrue(r.json()["ok"])
        self.assertIsNone(_state["balance_alerts_task"])

    async def test_enable_when_task_exists(self):
        async def _never():
            await asyncio.sleep(60)
        t = asyncio.create_task(_never())
        await asyncio.sleep(0)
        _state["balance_alerts_task"] = t
        with patch("core.web_gui.asyncio.sleep", new=AsyncMock(return_value=None)):
            r = await self._request("POST", "/api/balance-alerts",
                                    json={"enabled": True, "threshold": 1.0})
        self.assertTrue(r.json()["ok"])
        self.assertIs(_state["balance_alerts_task"], t)
        t.cancel()
        _state["balance_alerts_task"] = None

    async def test_enable_creates_task(self):
        _state["balance_alerts_task"] = None
        with patch("core.web_gui._balance_monitor_loop", new=AsyncMock(return_value=None)):
            r = await self._request("POST", "/api/balance-alerts",
                                    json={"enabled": True, "threshold": 0.5})
            self.assertTrue(r.json()["ok"])
            t = _state["balance_alerts_task"]
            self.assertIsNotNone(t)
            try:
                await asyncio.wait_for(t, timeout=5)
            except asyncio.TimeoutError:
                t.cancel()

    async def test_monitor_balance_exc_and_faucet_exc(self):
        db = FakeDB(wallets=[{"address": "0xlow"}, {"address": "0xok"}])
        net = MagicMock()
        async def _bal(addr):
            if addr == "0xlow":
                return 0.01
            raise RuntimeError("bal fail")  # 679-680
        net.get_balance = _bal
        fc = AsyncMock()
        fc.ensure_balance = AsyncMock(side_effect=RuntimeError("auto fail"))  # 696-697
        _state["db"] = db
        _state["network"] = net
        _state["faucet"] = fc
        _state["balance_threshold"] = 1.0
        _state["balance_alerts_enabled"] = True
        _state["clients"] = []

        calls = {"n": 0}

        async def _sleep(secs):
            calls["n"] += 1
            if calls["n"] >= 1:
                _state["balance_alerts_enabled"] = False
            return None

        with patch("core.web_gui.asyncio.sleep", new=_sleep):
            await web_gui._balance_monitor_loop()
        self.assertGreaterEqual(calls["n"], 1)

    async def test_monitor_no_resources(self):
        _state["network"] = None
        _state["db"] = None
        _state["faucet"] = None
        _state["balance_alerts_enabled"] = True

        async def _sleep(secs):
            _state["balance_alerts_enabled"] = False
            return None

        with patch("core.web_gui.asyncio.sleep", new=_sleep):
            await web_gui._balance_monitor_loop()

    async def test_monitor_cancelled(self):
        _state["network"] = FakeNetwork()
        _state["db"] = FakeDB()
        _state["faucet"] = FakeFaucet()
        _state["balance_alerts_enabled"] = True

        async def _sleep(secs):
            raise asyncio.CancelledError()

        with patch("core.web_gui.asyncio.sleep", new=_sleep):
            await web_gui._balance_monitor_loop()  # 701 break

    async def test_monitor_generic_error(self):
        db = AsyncMock()
        db.get_all_addresses = AsyncMock(side_effect=RuntimeError("addr boom"))
        _state["db"] = db
        _state["network"] = FakeNetwork()
        _state["faucet"] = FakeFaucet()
        _state["balance_alerts_enabled"] = True
        seq = {"n": 0}

        async def _sleep(secs):
            seq["n"] += 1
            if seq["n"] >= 2:
                _state["balance_alerts_enabled"] = False
            if secs == 15:
                return None
            return None

        with patch("core.web_gui.asyncio.sleep", new=_sleep):
            await web_gui._balance_monitor_loop()  # 701-703
        self.assertGreaterEqual(seq["n"], 1)


# ------------------------------------------------- groups 718-766 ---
class TestGroupsCov(unittest.IsolatedAsyncioTestCase, HttpxMixin):
    def setUp(self):
        self._saved = _save_state()
        app.dependency_overrides[web_gui._verify_request_auth] = _no_auth

    def tearDown(self):
        _restore_state(self._saved)
        app.dependency_overrides.clear()

    async def test_create_not_init(self):
        _state["group_manager"] = None
        r = await self._request("POST", "/api/groups", json={"name": "x"})
        self.assertFalse(r.json()["ok"])

    async def test_create_generic_exc(self):
        gm = FakeGroupManager()
        gm.create_group = AsyncMock(side_effect=RuntimeError("boom"))
        _state["group_manager"] = gm
        r = await self._request("POST", "/api/groups", json={"name": "boom"})
        self.assertFalse(r.json()["ok"])

    async def test_delete_not_init(self):
        _state["group_manager"] = None
        r = await self._request("DELETE", "/api/groups/1")
        self.assertFalse(r.json()["ok"])

    async def test_add_not_init(self):
        _state["group_manager"] = None
        r = await self._request("POST", "/api/groups/1/wallets", json={"addresses": ["0x1"]})
        self.assertFalse(r.json()["ok"])

    async def test_remove_not_init(self):
        _state["group_manager"] = None
        r = await self._request("DELETE", "/api/groups/1/wallets", json={"addresses": ["0x1"]})
        self.assertFalse(r.json()["ok"])

    async def test_remove_partial(self):
        gm = AsyncMock()
        gm.remove_wallet_from_group = AsyncMock(side_effect=[True, False])
        _state["group_manager"] = gm
        r = await self._request("DELETE", "/api/groups/1/wallets", json={"addresses": ["0x1", "0x2"]})
        d = r.json()
        self.assertTrue(d["ok"])
        self.assertEqual(d["removed"], 1)

    async def test_get_addrs_not_init(self):
        _state["group_manager"] = None
        r = await self._request("GET", "/api/groups/1/wallets")
        self.assertFalse(r.json()["ok"])

    async def test_get_wallet_groups_not_init(self):
        _state["group_manager"] = None
        r = await self._request("GET", "/api/wallets/0x1/groups")
        self.assertFalse(r.json()["ok"])


# ------------------------------------------------- ws 780-786 ---
class TestWSCov(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._saved = _save_state()
        self._mk = web_gui.MASTER_KEY
        web_gui.MASTER_KEY = ""

    def tearDown(self):
        _restore_state(self._saved)
        web_gui.MASTER_KEY = self._mk

    async def test_ws_with_logs(self):
        _state["db"] = None
        _state["is_farming"] = False
        _state["config"] = None
        _state["scheduler"] = None
        _state["current_network"] = "default"
        _state["balance_alerts_enabled"] = False
        _state["logs"] = ["log1", "log2"]
        _state["clients"] = []
        from fastapi import WebSocketDisconnect
        ws = FakeWS(recv_seq=[WebSocketDisconnect(code=1000)])
        await web_gui.websocket_endpoint(ws)
        self.assertTrue(ws.accepted)
        # status + 2 logs
        self.assertGreaterEqual(len(ws.sent), 3)
        self.assertNotIn(ws, _state["clients"])

    async def test_ws_generic_exception(self):
        _state["db"] = None
        _state["is_farming"] = False
        _state["config"] = None
        _state["scheduler"] = None
        _state["current_network"] = "default"
        _state["balance_alerts_enabled"] = False
        _state["logs"] = []
        _state["clients"] = []
        ws = FakeWS(recv_seq=[RuntimeError("recv boom")])
        await web_gui.websocket_endpoint(ws)  # 785-786
        self.assertNotIn(ws, _state["clients"])

    async def test_ws_send_fail_removed(self):
        _state["db"] = None
        _state["is_farming"] = False
        _state["config"] = None
        _state["scheduler"] = None
        _state["current_network"] = "default"
        _state["balance_alerts_enabled"] = False
        _state["logs"] = []
        _state["clients"] = []
        from fastapi import WebSocketDisconnect

        class FailSend(FakeWS):
            async def send_text(self, msg):
                raise RuntimeError("send boom")

        ws = FailSend(recv_seq=[WebSocketDisconnect(code=1000)])
        await web_gui.websocket_endpoint(ws)
        self.assertNotIn(ws, _state["clients"])


# ------------------------------------------------- start_gui 793-803 ---
class TestStartGui(unittest.TestCase):
    def setUp(self):
        self._mk = web_gui.MASTER_KEY

    def tearDown(self):
        web_gui.MASTER_KEY = self._mk

    def test_start_no_key(self):
        import sys
        web_gui.MASTER_KEY = ""
        fake = MagicMock()
        with patch.dict(sys.modules, {"uvicorn": fake}):
            web_gui.start_gui(host="127.0.0.1", port=18081)
            fake.run.assert_called_once()

    def test_start_with_key(self):
        import sys
        web_gui.MASTER_KEY = "k"
        fake = MagicMock()
        with patch.dict(sys.modules, {"uvicorn": fake}):
            web_gui.start_gui(host="127.0.0.1", port=18082)
            fake.run.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
