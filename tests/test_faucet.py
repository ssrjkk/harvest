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


if __name__ == "__main__":
    unittest.main(verbosity=2)
