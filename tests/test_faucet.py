"""Тесты Faucet: лимит параллелизма из конфига (без обращения к сети)."""

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.faucet import Faucet


class TestFaucetConcurrency(unittest.TestCase):
    def test_default_max_concurrent(self):
        f = Faucet({"faucet": {}, "proxy": {}})
        self.assertEqual(f.max_concurrent, 8)
        self.assertEqual(f.concurrent_batch(50), 8)
        self.assertEqual(f.concurrent_batch(3), 3)

    def test_configured_cap(self):
        f = Faucet({"faucet": {"max_concurrent": 20}, "proxy": {}})
        self.assertEqual(f.concurrent_batch(100), 20)

    def test_zero_workers_floor(self):
        f = Faucet({"faucet": {}, "proxy": {}})
        self.assertEqual(f.concurrent_batch(0), 1)

    def test_enabled_flag(self):
        self.assertFalse(Faucet({"faucet": {"enabled": False}, "proxy": {}}).enabled)
        self.assertTrue(Faucet({"faucet": {"enabled": True}, "proxy": {}}).enabled)

    def test_non_dict_strategies_filtered(self):
        f = Faucet(
            {
                "faucet": {"strategies": [1, "x", None, {"type": "direct", "url": "https://faucet.example/claim"}]},
                "proxy": {},
            }
        )
        self.assertEqual(f.strategies, [{"type": "direct", "url": "https://faucet.example/claim"}])

    def test_validate_with_garbage_strategies_offline(self):
        f = Faucet({"faucet": {"strategies": [1, "x", None]}, "proxy": {}})
        self.assertEqual(len(f.strategies), 0)
        result = asyncio.run(f.validate())
        self.assertEqual(result, 0)

    def test_inverted_delay_range_falls_back(self):
        f = Faucet({"faucet": {"delay_between_requests": [30, 5]}, "proxy": {}})
        self.assertEqual(f.delay_range, [5, 15])

    def test_garbage_delay_range_falls_back(self):
        f = Faucet({"faucet": {"delay_between_requests": "x"}, "proxy": {}})
        self.assertEqual(f.delay_range, [5, 15])

    def test_garbage_max_concurrent_falls_back(self):
        f = Faucet({"faucet": {"max_concurrent": "many"}, "proxy": {}})
        self.assertEqual(f.max_concurrent, 8)


class TestValidateReport(unittest.TestCase):
    def test_no_key_chainstack_reports_reason(self):
        import os
        from unittest import mock

        f = Faucet(
            {"faucet": {"strategies": [{"type": "chainstack", "url": "https://mcp.chainstack.com/mcp"}]},
             "proxy": {}}
        )
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CHAINSTACK_API_KEY", None)
            self.assertEqual(asyncio.run(f.validate()), 0)
        report = f.probe_report()
        self.assertEqual(len(report), 1)
        self.assertFalse(report[0]["alive"])
        self.assertIn("CHAINSTACK_API_KEY", report[0]["reason"])

    def test_probe_report_is_a_copy(self):
        f = Faucet({"faucet": {"strategies": []}, "proxy": {}})
        self.assertEqual(f.probe_report(), [])
        f._last_probe.append({"type": "x", "url": "", "alive": True, "reason": "ok"})
        rep = f.probe_report()
        rep.clear()
        self.assertEqual(len(f.probe_report()), 1)

    def test_zero_live_logs_error(self):
        f = Faucet(
            {"faucet": {"strategies": [{"type": "direct", "url": ""}]}, "proxy": {}}
        )
        with self.assertLogs("core.faucet", level="ERROR") as cm:
            self.assertEqual(asyncio.run(f.validate()), 0)
        self.assertTrue(any("0 живых стратегий" in line for line in cm.output))


if __name__ == "__main__":
    unittest.main(verbosity=2)
