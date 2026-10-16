"""Тесты HierarchicalLimiter: бюджеты, penalty box, evict."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from portal.shield.config import LimitsConfig
from portal.shield.limits import HierarchicalLimiter


def _cfg(**kw):
    base = dict(read_rps=2.0, read_burst=2, write_rps=2.0, write_burst=2,
                window_s=60.0, penalty_base_s=60.0, penalty_max_s=600.0,
                penalty_violations=2, max_subjects=100,
                global_rps=1000.0, global_burst=10000)
    base.update(kw)
    return LimitsConfig(**base)


class TestLimiter(unittest.IsolatedAsyncioTestCase):
    async def test_burst_then_deny(self):
        lim = HierarchicalLimiter(_cfg())
        # Бюджет read: max(2*60, 2) = 120.
        allowed = 0
        first_deny = None
        for _ in range(125):
            r = await lim.check("u", "read")
            if r.allowed:
                allowed += 1
            elif first_deny is None:
                first_deny = r
        self.assertEqual(allowed, 120)
        self.assertIsNotNone(first_deny)
        self.assertEqual(first_deny.reason, "rate_limit")

    async def test_penalty_escalates_and_blocks(self):
        lim = HierarchicalLimiter(_cfg())
        for _ in range(120):
            await lim.check("u", "read")
        r1 = await lim.check("u", "read")  # deny #1
        self.assertFalse(r1.allowed)
        r2 = await lim.check("u", "read")  # deny #2 -> penalty
        self.assertFalse(r2.allowed)
        self.assertEqual(r2.reason, "penalty_box")
        self.assertGreater(r2.retry_after_s, 50.0)
        r3 = await lim.check("u", "read")
        self.assertEqual(r3.reason, "penalty_box")

    async def test_read_write_budgets_split(self):
        lim = HierarchicalLimiter(_cfg())
        for _ in range(120):
            self.assertTrue((await lim.check("u", "read")).allowed)
        # write-бюджет отдельный: ещё не тронут.
        self.assertTrue((await lim.check("u", "write")).allowed)

    async def test_global_shedder(self):
        lim = HierarchicalLimiter(_cfg(global_rps=1.0, global_burst=1))
        self.assertTrue((await lim.check("a", "read")).allowed)
        r = await lim.check("b", "read")
        self.assertFalse(r.allowed)
        self.assertEqual(r.reason, "global_overload")

    async def test_evict_and_stats(self):
        lim = HierarchicalLimiter(_cfg())
        await lim.check("ghost", "read")
        st = await lim.stats()
        self.assertEqual(st["subjects"], 1)
        self.assertEqual(await lim.evict(older_than_s=-1.0), 1)
        st = await lim.stats()
        self.assertEqual(st["subjects"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
