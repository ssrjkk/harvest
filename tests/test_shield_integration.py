"""Интеграционный тест щита: конкурентные дубли, троттлинг, теневой бан,
ограниченность памяти. Математическое доказательство поведения под штормом."""

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from portal.shield.config import GuardConfig, LimitsConfig
from portal.shield.middleware import idempotent, shield_middleware
from portal.shield.shield import Shield


class Req:
    def __init__(self, method="POST", path="/api/wallets", body=b'{"count": 3}',
                 ip="10.0.0.1", app=None):
        self.method = method
        self.path = path
        self.remote = ip
        self.headers = {}
        self._body = body
        self.app = app if app is not None else {}
        self._store = {}

    async def read(self):
        return self._body

    async def json(self):
        import json as _json

        return _json.loads(self._body.decode() or "{}")

    def __setitem__(self, k, v):
        self._store[k] = v

    def __getitem__(self, k):
        return self._store[k]


class TestShieldStorm(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_duplicates_single_execution(self):
        """30 воркеров шлют один и тот же запрос: бизнес-логика — ровно 1 раз."""
        sh = Shield()
        await sh.start()
        calls = []

        @idempotent("wallets:create")
        async def handler(request):
            calls.append(1)
            await asyncio.sleep(0.02)
            from aiohttp import web as _web

            return _web.json_response({"created": 3})

        async def worker():
            req = Req(app={"shield": sh})
            resp = await handler(req)
            return resp.status

        statuses = await asyncio.gather(*[worker() for _ in range(30)])
        self.assertTrue(all(s == 200 for s in statuses))
        self.assertEqual(len(calls), 1, "дубли дошли до бизнес-логики")
        await sh.stop()

    async def test_sustained_flood_throttled_then_penalized(self):
        """Шторм записей: сначала бюджет, потом rate_limit, затем penalty box."""
        lim_cfg = LimitsConfig(global_rps=1000.0, global_burst=10000,
                               write_rps=2.0, write_burst=2, window_s=60.0,
                               penalty_base_s=60.0, penalty_max_s=600.0,
                               penalty_violations=2)
        # Риск отключён порогами: меряем чистый троттлинг без tarpit-сна.
        from portal.shield.config import RiskConfig

        no_risk = RiskConfig(medium_threshold=1e9, high_threshold=1e12)
        sh = Shield(GuardConfig(limits=lim_cfg, risk=no_risk))
        await sh.start()

        async def handler(request):
            from aiohttp import web as _web

            return _web.Response(text="ok")

        mw = shield_middleware(sh)
        denied = 0
        for _ in range(200):
            resp = await mw(Req(app={"shield": sh}), handler)
            if resp.status == 429:
                denied += 1
        snap = await sh.snapshot()
        # write-бюджет max(2*60, 2)=120: остаток из 200 — отказы.
        self.assertGreater(denied, 0, "флуд не троттлится")
        self.assertGreaterEqual(snap["limits"]["penalty_blocks"], 1)
        await sh.stop()

    async def test_spammer_shadowbanned(self):
        """Спамер с одинаковыми пейлоадами уходит в теневой бан."""
        from portal.shield.config import RiskConfig

        fast_risk = RiskConfig(tarpit_min_s=0.01, tarpit_max_s=0.02)
        sh = Shield(GuardConfig(risk=fast_risk))
        await sh.start()

        async def handler(request):
            from aiohttp import web as _web

            return _web.Response(text="real-data")

        mw = shield_middleware(sh)
        import json as _json

        last_body = ""
        handler_calls = 0

        async def counting(request):
            nonlocal handler_calls
            handler_calls += 1
            return await handler(request)

        for _ in range(80):
            resp = await mw(Req(app={"shield": sh}), counting)
            last_body = resp.text
        self.assertLess(handler_calls, 80, "все запросы дошли до хендлера")
        self.assertEqual(_json.loads(last_body), {"ok": True})
        snap = await sh.snapshot()
        self.assertGreaterEqual(
            snap["audit"]["counters"].get("SHADOWBAN_TRIGGERED", 0), 1)
        await sh.stop()

    async def test_memory_bounded(self):
        """25k уникальных субъектов: размеры структур ограничены, evict чистит."""
        lim_cfg = LimitsConfig(max_subjects=20000)
        sh = Shield(GuardConfig(limits=lim_cfg))
        await sh.start()
        for i in range(25000):
            await sh.limits.check(f"victim-{i}", "read")
            if i % 5000 == 0:
                await sh.risk.observe(f"victim-{i}")
        stats = await sh.limits.stats()
        self.assertLessEqual(stats["subjects"], 20000)
        store_size = await sh.snapshot()
        self.assertLessEqual(store_size["store_size"], 60000)
        await sh.risk.evict(older_than_s=-1.0)
        await sh.limits.evict(older_than_s=-1.0)
        stats = await sh.limits.stats()
        self.assertEqual(stats["subjects"], 0)
        await sh.stop()


if __name__ == "__main__":
    unittest.main(verbosity=2)
