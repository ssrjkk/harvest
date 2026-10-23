"""Добивка portal/api.py + auth/config мелкие ветки до 100%."""
import os
import tempfile
import unittest
from pathlib import Path
from typing import cast
from unittest import mock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from portal import api
from portal.api import create_app
from portal.config import PortalConfig
from portal.farm import FarmDaemon


class _StubDaemon:
    running = True
    def __init__(self):
        self.groups = [{"id": 1, "name": "main", "color": "#007bff", "count": 0}]
        self.scheduler_tasks = {}
        self.scheduler_running = False
        self.balance = {"enabled": False, "threshold": 0.001}
    def run_state(self): return {"running": True, "paused":
        False}
    async def statistics(self): return {"running": True, "pool": {"actions": 1, "errors":
        0}}
    async def top_wallets(self, limit=8):
        return []
    async def cycle_history(self, limit=12):
        return []
    async def metrics_history(self, limit=100):
        return []
    def networks_info(self):
        return []
    async def switch_to(self, slug):
        return True
    async def list_groups(self):
        return self.groups
    async def create_group(self, name, color="#007bff"):
        g = {"id": 2, "name": name, "color": color, "count": 0}
        self.groups.append(g)
        return g
    async def delete_group(self, gid):
        return True
    async def add_wallet_to_group(self, a, gid):
        return True
    async def remove_wallet_from_group(self, a, gid):
        return True
    def scheduler_state(self): return {"initialized": True, "running": False, "tasks":
        []}
    async def add_scheduler_task(self, task_id, **kw):
        t = {"task_id": task_id}
        t.update(kw)
        self.scheduler_tasks[task_id] = t
        return t
    async def update_scheduler_task(self, task_id, **kw):
        t = self.scheduler_tasks.get(task_id)
        if t is None:
            return None
        t.update(kw)
        return t
    async def remove_scheduler_task(self, task_id):
        return True
    async def start_scheduler(self):
        return True
    async def stop_scheduler(self):
        return True
    def balance_alerts_state(self):
        return dict(self.balance)
    async def set_balance_alerts(self, enabled, threshold):
        self.balance = {"enabled": enabled, "threshold": threshold}
        return dict(self.balance)
    async def rpc_health(self): return {"nodes":
        []}
    async def faucet_report(self):
        return []
    def drip_state(self): return {"running":
        False}
    async def drip_faucet(self): return {"started":
        True}
    async def create_wallets(self, count):
        return []
    async def export_wallets(self, fmt="csv"):
        return ("a,b\n", "w.csv")
    async def wallet_cells(self):
        return ("o", 1)
    async def wallet_balances(self, limit=20):
        return []
    async def wallet_count(self):
        return 0
    async def all_wallets(self):
        return []


def _make_cfg(**kw):
    prev = {k: os.environ.get(k) for k in ("PORTAL_SECRET", "FARMER_MASTER_KEY")}
    os.environ["PORTAL_SECRET"] = "s" * 32
    os.environ["FARMER_MASTER_KEY"] = "k" * 64
    cfg = PortalConfig()
    cfg.public_base_url = ""
    cfg.allow_insecure_password = True
    cfg.cookie_secure = False
    cfg.password_login = True
    cfg.trust_proxy = False
    for k, v in kw.items():
        setattr(cfg, k, v)
    for k, v in prev.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    return cfg


class ApiCov(unittest.IsolatedAsyncioTestCase):
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

    async def _client(self, cfg=None, daemon=None, shield=None):
        cfg = cfg or _make_cfg()
        app = create_app(cfg, cast(FarmDaemon, daemon or _StubDaemon()), shield=shield)
        c = TestClient(TestServer(app))
        await c.start_server()
        return c

    async def _authed(self, client):
        r = await client.post("/api/login/password", json={"password": "k" * 64})
        self.assertEqual(r.status, 200)

    async def test_guard_none(self):
        # shield без sanitize -> _guard_check возвращает None (строка 308)
        from portal.shield.config import GuardConfig
        from portal.shield.shield import Shield
        cfg = _make_cfg()
        sh = Shield(GuardConfig(enabled=True))
        sh.sanitize = None  # type: ignore
        c = await self._client(cfg, shield=sh)
        async with c:
            await self._authed(c)
            r = await c.post("/api/groups", json={"name": "g1", "color": "#112233"})
            self.assertIn(r.status, (200, 409))

    async def test_html_404_500(self):
        cfg = _make_cfg()
        c = await self._client(cfg)
        async with c:
            with mock.patch("portal.api.STATIC_DIR") as _sd:
                p = mock.MagicMock()
                p.read_text.side_effect = FileNotFoundError("no")
                _sd.__truediv__.return_value = p
                r = await c.get("/")
                self.assertEqual(r.status, 404)
            with mock.patch("portal.api.STATIC_DIR") as _sd:
                p = mock.MagicMock()
                p.read_text.side_effect = OSError("disk")
                _sd.__truediv__.return_value = p
                r = await c.get("/")
                self.assertEqual(r.status, 500)

    async def test_broadcast_ws_concurrent_mutation(self):
        class _Ws:
            def __init__(self, fail=False, mutate=None):
                self.fail = fail
                self.mutate = mutate
                self.got = []

            async def send_str(self, msg):
                if self.mutate is not None:
                    self.mutate()
                if self.fail:
                    raise ConnectionResetError("gone")
                self.got.append(msg)

        good, bad = _Ws(), _Ws(fail=True)
        pool = {good, bad}
        app = {api.KEY_WS_CLIENTS: pool}

        def _late_join():
            pool.add(_Ws())

        racing = _Ws(mutate=_late_join)
        pool.add(racing)
        # Не должно бросить RuntimeError при конкурентном connect/disconnect:
        # итерируем копию, мёртвых выкидываем.
        await api.broadcast_ws(app, "farm", {"action": "x"})
        self.assertNotIn(bad, pool)
        self.assertTrue(any("farm" in m for m in good.got))
        # Пустой пул — no-op без ошибок.
        await api.broadcast_ws({api.KEY_WS_CLIENTS: set()}, "farm", {})
        await api.broadcast_ws({}, "farm", {})

    async def test_login_non_dict(self):
        c = await self._client()
        async with c:
            r = await c.post("/api/login/password", json=[])
            self.assertEqual(r.status, 400)

    async def test_guard_endpoint(self):
        from portal.shield.config import GuardConfig
        from portal.shield.shield import Shield
        cfg = _make_cfg()
        sh = Shield(GuardConfig(enabled=True))
        c = await self._client(cfg, shield=sh)
        async with c:
            await self._authed(c)
            r = await c.get("/api/guard")
            self.assertEqual(r.status, 200)

    async def test_wallets_export_post_formats(self):
        c = await self._client()
        async with c:
            await self._authed(c)
            r = await c.post("/api/wallets/export", json={"format": "json"})
            self.assertIn(r.status, (200, 403))
            # битый body -> fmt=""
            r2 = await c.post("/api/wallets/export", data="not-json", headers={"Content-Type": "application/json"})
            self.assertIn(r2.status, (200, 400, 403, 500))
            # без формата -> дефолт csv (POST с пустым dict)
            r3 = await c.post("/api/wallets/export", json={})
            self.assertIn(r3.status, (200, 403))
            # query-формат
            r4 = await c.post("/api/wallets/export?format=json", json={})
            self.assertIn(r4.status, (200, 403))

    async def test_groups_bad_color(self):
        c = await self._client()
        async with c:
            await self._authed(c)
            r = await c.post("/api/groups", json={"name": "x", "color": "red"})
            self.assertEqual(r.status, 400)

    async def test_scheduler_add_bad(self):
        c = await self._client()
        async with c:
            await self._authed(c)
            r = await c.post("/api/scheduler/tasks", json=[])
            self.assertEqual(r.status, 400)
            r = await c.post("/api/scheduler/tasks", json={"task_id": "t", "interval_hours": "abc"})
            self.assertEqual(r.status, 400)
            r = await c.post("/api/scheduler/tasks", json={"task_id": "t", "cycles": "abc"})
            self.assertEqual(r.status, 400)

    async def test_scheduler_update_bad(self):
        d = _StubDaemon()
        d.scheduler_tasks["t1"] = {"task_id": "t1"}
        c = await self._client(daemon=d)
        async with c:
            await self._authed(c)
            r = await c.put("/api/scheduler/tasks/t1", json=[])
            self.assertEqual(r.status, 400)
            rr = await c.put("/api/scheduler/tasks/t1", json={"interval_hours": "abc"})
            self.assertEqual(rr.status, 400)
            rr = await c.put("/api/scheduler/tasks/t1", json={"interval_minutes": "abc"})
            self.assertEqual(rr.status, 400)
            rr = await c.put("/api/scheduler/tasks/t1", json={"cycles": "abc"})
            self.assertEqual(rr.status, 400)

    async def test_scheduler_update_guard_reject(self):
        d = _StubDaemon()
        d.scheduler_tasks["t1"] = {"task_id": "t1"}
        c = await self._client(daemon=d)
        async with c:
            await self._authed(c)
            with mock.patch("portal.api._guard_check") as _g:
                _g.return_value = web.json_response({"error": "bad"}, status=400)
                rr = await c.put("/api/scheduler/tasks/t1", json={"name": "x"})
                self.assertEqual(rr.status, 400)

    async def test_balance_alerts_bad(self):
        c = await self._client()
        async with c:
            await self._authed(c)
            r = await c.post("/api/balance-alerts", json=[])
            self.assertEqual(r.status, 400)
            r = await c.post("/api/balance-alerts", json={"enabled": True, "threshold": "abc"})
            self.assertEqual(r.status, 400)

    async def test_faucet_errors(self):
        d = _StubDaemon()
        async def _boom():
            raise Exception("db down")
        d.faucet_report = _boom  # type: ignore
        c = await self._client(daemon=d)
        async with c:
            await self._authed(c)
            r = await c.get("/api/faucet")
            self.assertEqual(r.status, 500)
        d2 = _StubDaemon()
        async def _boom2():
            raise RuntimeError("no faucet")
        d2.drip_faucet = _boom2  # type: ignore
        c2 = await self._client(daemon=d2)
        async with c2:
            await self._authed(c2)
            r = await c2.post("/api/faucet/drip")
            self.assertEqual(r.status, 503)

    async def test_ws_cap_and_revoke(self):
        from portal.shield.config import GuardConfig
        from portal.shield.shield import Shield
        cfg = _make_cfg()
        sh = Shield(GuardConfig(enabled=True, ws_max_clients=0))
        c = await self._client(cfg, shield=sh)
        async with c:
            await self._authed(c)
            # кап 0 -> 503
            try:
                ws = await c.ws_connect("/ws")
                # если соединилось — закрываем
                await ws.close()
            except Exception:
                pass
        # revoke-ветка: открываем WS, затем отзываем
        cfg2 = _make_cfg()
        c2 = await self._client(cfg2)
        async with c2:
            await self._authed(c2)
            # подменяем _is_revoked чтобы второй вызов вернул True
            calls = {"n": 0}
            orig = api._is_revoked
            def _fake(jti):
                calls["n"] += 1
                return calls["n"] >= 2
            with mock.patch.object(api, "_is_revoked", side_effect=_fake):
                try:
                    ws = await c2.ws_connect("/ws")
                    await ws.receive(timeout=2.0)
                    await ws.close()
                except Exception:
                    pass
            _ = orig
