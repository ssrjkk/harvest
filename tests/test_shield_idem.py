"""Тесты IdempotencyStore: гонки дублей, реплеи, TTL, evict."""

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from portal.shield.config import IdemConfig
from portal.shield.idem import IdempotencyStore, make_key


def _cfg(**kw):
    base = dict(ttl_s=3600.0, max_keys=100, time_bucket_s=300.0)
    base.update(kw)
    return IdemConfig(**base)


class TestIdem(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_duplicates_single_execution(self):
        store = IdempotencyStore(_cfg())
        calls = 0

        async def factory():
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.05)
            return {"n": calls}

        results = await asyncio.gather(*[store.run("k", factory) for _ in range(20)])
        self.assertEqual(calls, 1)
        self.assertTrue(all(r.result == {"n": 1} for r in results))
        self.assertEqual(sum(r.replayed for r in results), 19)

    async def test_replay_after_done(self):
        store = IdempotencyStore(_cfg())
        r1 = await store.run("k", lambda: _const(1))
        r2 = await store.run("k", lambda: _const(2))
        self.assertFalse(r1.replayed)
        self.assertTrue(r2.replayed)
        self.assertEqual(r2.result, 1)

    async def test_failure_deletes_key(self):
        store = IdempotencyStore(_cfg())
        calls = 0

        async def flaky():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("boom")
            return "ok"

        with self.assertRaises(RuntimeError):
            await store.run("k", flaky)
        res = await store.run("k", flaky)
        self.assertEqual(res.result, "ok")
        self.assertFalse(res.replayed)
        self.assertEqual(calls, 2)

    async def test_ttl_expiry_reexecutes(self):
        store = IdempotencyStore(_cfg(ttl_s=0.05))
        calls = 0

        async def factory():
            nonlocal calls
            calls += 1
            return calls

        await store.run("k", factory)
        await asyncio.sleep(0.08)
        res = await store.run("k", factory)
        self.assertFalse(res.replayed)
        self.assertEqual(calls, 2)

    async def test_make_key_deterministic_and_bucketed(self):
        a = make_key("u", "act", b"body", bucket_s=300.0, now=1000.0)
        b = make_key("u", "act", b"body", bucket_s=300.0, now=1100.0)
        c = make_key("u", "act", b"body", bucket_s=300.0, now=2000.0)
        d = make_key("u", "act", b"other", bucket_s=300.0, now=1000.0)
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertNotEqual(a, d)
        self.assertTrue(a.startswith("idem:"))

    async def test_evict_and_stats(self):
        store = IdempotencyStore(_cfg(ttl_s=0.05))

        async def factory():
            return 1

        await store.run("k1", factory)
        await store.run("k2", factory)
        st = await store.stats()
        self.assertEqual(st["executions"], 2)
        await asyncio.sleep(0.08)
        removed = await store.evict()
        self.assertGreaterEqual(removed, 2)


async def _const(v):
    return v


if __name__ == "__main__":
    unittest.main(verbosity=2)
