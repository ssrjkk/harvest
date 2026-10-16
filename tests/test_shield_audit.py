"""Тесты AuditLog: маскирование, счётчики, латентности, кольцо, correlation_id."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from portal.shield.audit import AuditLog, bind_correlation, correlation_id, mask, mask_text


class TestMask(unittest.TestCase):
    def test_secret_keys(self):
        out = mask({"api_key": "SECRET", "password": "p", "n": 1, "ok": True})
        self.assertEqual(out, {"api_key": "***", "password": "***", "n": 1, "ok": True})

    def test_bearer_and_hex(self):
        self.assertIn("…", mask_text("Authorization: Bearer abcdef12345XYZ"))
        masked = mask_text("tx 0x" + "ab" * 32 + " done")
        self.assertNotIn("ab" * 32, masked)
        self.assertIn("0xab", masked)

    def test_nested_and_depth_cap(self):
        deep = {"a": {"b": {"c": {"d": {"e": {"f": {"g": {"h": 1}}}}}}}}
        out = mask(deep)
        self.assertIn("…", str(out))
        big = mask({"items": list(range(100))})
        self.assertEqual(len(big["items"]), 50)
        long_s = mask("x" * 3000)
        self.assertTrue(long_s.endswith("…<truncated>"))

    def test_non_strings_passthrough(self):
        self.assertEqual(mask(5), 5)
        self.assertIsNone(mask(None))


class TestAuditLog(unittest.IsolatedAsyncioTestCase):
    async def test_events_counters_ring(self):
        log = AuditLog(ring=5)
        for i in range(8):
            await log.event("RATE_LIMIT_EXCEEDED", f"ip:{i}", {"token": "sekret"})
        snap = await log.snapshot()
        self.assertEqual(snap["counters"]["RATE_LIMIT_EXCEEDED"], 8)
        self.assertEqual(snap["ring_size"], 5)
        recent = await log.recent(limit=50)
        self.assertEqual(len(recent), 5)
        self.assertEqual(recent[0]["details"], {"token": "***"})

    async def test_latency_p50_p95(self):
        log = AuditLog()
        for i in range(1, 101):
            await log.observe_latency("op", float(i))
        snap = await log.snapshot()
        lat = snap["latency_ms"]["op"]
        self.assertEqual(lat["p50"], 51.0)
        self.assertEqual(lat["p95"], 96.0)
        self.assertEqual(lat["n"], 100)

    async def test_correlation(self):
        self.assertEqual(correlation_id(), "-")
        cid = bind_correlation("abc123")
        self.assertEqual(cid, "abc123")
        self.assertEqual(correlation_id(), "abc123")
        auto = bind_correlation(None)
        self.assertTrue(auto and auto != "-")


if __name__ == "__main__":
    unittest.main(verbosity=2)
