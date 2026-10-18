"""Тесты middleware и декоратора idempotent (заглушки вместо TestClient)."""

import sys
import unittest
from pathlib import Path

from aiohttp import web

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from portal.shield.config import GuardConfig, LimitsConfig, RiskConfig
from portal.shield.middleware import idempotent, shield_middleware
from portal.shield.shield import Shield


class Req:
    def __init__(self, method="GET", path="/api/stats", body=b"", app=None,
                 ip="9.9.9.9", headers=None):
        self.method = method
        self.path = path
        self.remote = ip
        self.headers = headers or {}
        self._body = body
        self.app = app or {}
        self._store = {}

    async def read(self):
        return self._body

    def __setitem__(self, k, v):
        self._store[k] = v

    def __getitem__(self, k):
        return self._store[k]


def _shield(**kw):
    cfg = GuardConfig(**kw) if kw else GuardConfig()
    return Shield(cfg)


class TestMiddleware(unittest.IsolatedAsyncioTestCase):
    async def test_passthrough(self):
        sh = _shield()
        await sh.start()
        calls = []

        async def handler(request):
            calls.append(1)
            return web.Response(text='{"ok": true}', content_type="application/json")

        mw = shield_middleware(sh)
        resp = await mw(Req(app={"shield": sh}), handler)
        self.assertEqual(resp.status, 200)
        self.assertEqual(calls, [1])
        await sh.stop()

    async def test_write_burst_denied_429(self):
        lim = LimitsConfig(global_rps=1000.0, global_burst=10000,
                           write_rps=1.0, write_burst=1, window_s=1.0,
                           penalty_violations=1000)
        sh = Shield(GuardConfig(limits=lim))
        await sh.start()

        async def handler(request):
            return web.Response(text="{}", content_type="application/json")

        mw = shield_middleware(sh)
        r1 = await mw(Req(method="POST", path="/api/x", app={"shield": sh}), handler)
        r2 = await mw(Req(method="POST", path="/api/x", app={"shield": sh}), handler)
        self.assertEqual(r1.status, 200)
        self.assertEqual(r2.status, 429)
        snap = await sh.snapshot()
        self.assertGreaterEqual(snap["audit"]["counters"].get("RATE_LIMIT_EXCEEDED", 0), 1)
        await sh.stop()

    async def test_shadowban_plausible_no_handler_call(self):
        risk = RiskConfig(medium_threshold=2.0, high_threshold=4.0,
                          tarpit_min_s=0.01, tarpit_max_s=0.02)
        sh = Shield(GuardConfig(risk=risk))
        await sh.start()
        calls = []

        async def handler(request):
            calls.append(1)
            return web.Response(text="secret-data")

        mw = shield_middleware(sh)
        bodies = []
        for _ in range(4):
            resp = await mw(Req(app={"shield": sh}), handler)
            bodies.append(resp.text if hasattr(resp, "text") else "")
        self.assertLess(len(calls), 4)
        last = bodies[-1]
        import json as _json

        self.assertEqual(_json.loads(last), {"ok": True})
        snap = await sh.snapshot()
        self.assertGreaterEqual(snap["audit"]["counters"].get("SHADOWBAN_TRIGGERED", 0), 1)
        await sh.stop()

    async def test_choke_paths_skip_risk(self):
        risk = RiskConfig(medium_threshold=2.0, high_threshold=4.0)
        sh = Shield(GuardConfig(risk=risk))
        await sh.start()
        calls = []

        async def handler(request):
            calls.append(1)
            return web.Response(text="login-page")

        mw = shield_middleware(sh)
        for _ in range(6):
            resp = await mw(
                Req(method="POST", path="/api/login/password", app={"shield": sh}), handler)
            self.assertEqual(resp.status, 200)
        self.assertEqual(len(calls), 6)
        await sh.stop()

    async def test_disabled_passthrough(self):
        sh = Shield(GuardConfig(enabled=False))

        async def handler(request):
            return web.Response(text="ok")

        mw = shield_middleware(sh)
        for _ in range(10):
            resp = await mw(Req(method="POST", path="/api/x", app={"shield": sh}), handler)
            self.assertEqual(resp.status, 200)

    async def test_client_ip_spoofing_blocked_without_trust(self):
        from portal.shield.middleware import _client_ip

        # Без trust_proxy заголовок игнорируется — IP из remote.
        req = Req(ip="1.2.3.4", headers={"X-Forwarded-For": "9.9.9.9"}, app={})
        self.assertEqual(_client_ip(req), "1.2.3.4")

    async def test_client_ip_trusted_with_proxy_flag(self):
        from portal.shield.middleware import _client_ip

        class _Cfg:
            trust_proxy = True

        # middleware ищет trust_proxy в любом значении app.
        req = Req(ip="1.2.3.4", headers={"X-Forwarded-For": "9.9.9.9, 8.8.8.8"},
                  app={"cfg": _Cfg()})
        self.assertEqual(_client_ip(req), "9.9.9.9")


class TestIdempotentDecorator(unittest.IsolatedAsyncioTestCase):
    async def test_replay_returns_identical_bytes(self):
        sh = _shield()
        await sh.start()
        calls = []

        @idempotent("wallets:create")
        async def handler(request):
            calls.append(1)
            await request.json()
            return web.json_response({"created": len(calls)})

        class JsonReq(Req):
            async def json(self):
                import json as _json

                return _json.loads(self._body.decode() or "{}")

        r1 = await handler(JsonReq(method="POST", path="/api/wallets",
                                   body=b'{"count": 3}', app={"shield": sh}))
        r2 = await handler(JsonReq(method="POST", path="/api/wallets",
                                   body=b'{"count": 3}', app={"shield": sh}))
        r3 = await handler(JsonReq(method="POST", path="/api/wallets",
                                   body=b'{"count": 4}', app={"shield": sh}))
        self.assertEqual(len(calls), 2)
        self.assertEqual(r1.body, r2.body)
        self.assertNotEqual(r1.body, r3.body)
        await sh.stop()

    async def test_no_shield_passthrough(self):
        calls = []

        @idempotent("x")
        async def handler(request):
            calls.append(1)
            return web.Response(text="ok")

        await handler(Req(app={}))
        await handler(Req(app={}))
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
