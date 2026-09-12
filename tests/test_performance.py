"""Тесты расчёта параллелизма: дефолты под ядра, явные конфиги, клампы."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.performance import (
    auto_max_workers,
    auto_rpc_threads,
    effective_chunk_size,
    effective_gen_workers,
    effective_max_workers,
    effective_rpc_threads,
    summarize,
)


def _cfg(**threading_overrides) -> dict:
    th = {"max_workers": 20}
    th.update(threading_overrides)
    return {"threading": th}


class TestPerformance(unittest.TestCase):
    def test_gen_workers_default_scales_to_cores(self):
        n = effective_gen_workers(_cfg())
        self.assertGreaterEqual(n, 2)
        self.assertLessEqual(n, 8)

    def test_gen_workers_explicit(self):
        self.assertEqual(effective_gen_workers(_cfg(gen_workers=8)), 8)

    def test_gen_workers_negative_falls_back(self):
        self.assertGreaterEqual(effective_gen_workers(_cfg(gen_workers=-3)), 2)

    def test_rpc_threads_default(self):
        self.assertEqual(effective_rpc_threads(_cfg(max_workers=10)), 30)  # 10*3

    def test_rpc_threads_explicit(self):
        self.assertEqual(effective_rpc_threads(_cfg(rpc_threads=200)), 200)

    def test_rpc_threads_floor(self):
        self.assertEqual(effective_rpc_threads(_cfg(max_workers=1)), 4)

    def test_rpc_threads_cap(self):
        self.assertEqual(effective_rpc_threads(_cfg(max_workers=1000)), 128)

    def test_rpc_threads_auto_by_cores(self):
        n = effective_rpc_threads(_cfg(max_workers=0))
        self.assertEqual(n, auto_rpc_threads())
        self.assertGreaterEqual(n, 8)
        self.assertLessEqual(n, 128)

    def test_chunk_size_default(self):
        self.assertEqual(effective_chunk_size(_cfg(max_workers=20)), 100)  # 20*4 < 100

    def test_chunk_size_scales(self):
        self.assertEqual(effective_chunk_size(_cfg(max_workers=60)), 240)

    def test_chunk_size_explicit(self):
        self.assertEqual(effective_chunk_size(_cfg(chunk_size=250)), 250)

    def test_chunk_size_zero_uses_default(self):
        self.assertEqual(effective_chunk_size(_cfg(chunk_size=0)), 100)

    def test_max_workers_auto(self):
        n = effective_max_workers(_cfg(max_workers=0))
        self.assertEqual(n, auto_max_workers())
        self.assertGreaterEqual(n, 20)
        self.assertLessEqual(n, 128)

    def test_max_workers_explicit(self):
        self.assertEqual(effective_max_workers(_cfg(max_workers=42)), 42)

    def test_summarize(self):
        s = summarize(_cfg(gen_workers=6, rpc_threads=40, chunk_size=300))
        self.assertEqual(s["gen_workers"], 6)
        self.assertEqual(s["rpc_threads"], 40)
        self.assertEqual(s["chunk_size"], 300)
        self.assertGreaterEqual(s["cpu_cores"], 1)
        self.assertEqual(s["max_workers"], 20)
        self.assertFalse(s["rpc_threads_auto"])
        self.assertFalse(s["max_workers_auto"])

    def test_summarize_auto_flags(self):
        s = summarize(_cfg(max_workers=0))
        self.assertTrue(s["max_workers_auto"])
        self.assertTrue(s["rpc_threads_auto"])
        self.assertTrue(s["gen_workers_auto"])
        self.assertTrue(s["chunk_size_auto"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
