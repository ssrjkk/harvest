"""Тесты CircuitBreaker и run_with_retry (backoff + jitter)."""

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from portal.shield.breaker import CircuitBreaker, CircuitOpenError, backoff_delay, run_with_retry
from portal.shield.config import BreakerConfig


def _cfg(**kw):
    base = dict(failure_threshold=2, recovery_timeout_s=0.05,
                half_open_probes=1, retry_attempts=3,
                backoff_base_s=0.01, backoff_cap_s=0.05)
    base.update(kw)
    return BreakerConfig(**base)


class TestBreaker(unittest.IsolatedAsyncioTestCase):
    async def test_opens_and_rejects(self):
        br = CircuitBreaker(_cfg())

        async def boom():
            raise RuntimeError("down")

        for _ in range(2):
            with self.assertRaises(RuntimeError):
                await br.call(boom)
        self.assertEqual(await br.state(), "open")
        with self.assertRaises(CircuitOpenError):
            await br.call(boom)
        snap = await br.snapshot()
        self.assertEqual(snap["opens"], 1)
        self.assertGreaterEqual(snap["rejected"], 1)

    async def test_half_open_recovers(self):
        br = CircuitBreaker(_cfg())
        calls = 0

        async def flaky():
            nonlocal calls
            calls += 1
            if calls <= 2:
                raise RuntimeError("down")
            return "up"

        for _ in range(2):
            with self.assertRaises(RuntimeError):
                await br.call(flaky)
        self.assertEqual(await br.state(), "open")
        await asyncio.sleep(0.07)
        self.assertEqual(await br.call(flaky), "up")
        self.assertEqual(await br.state(), "closed")

    async def test_cancelled_not_counted(self):
        br = CircuitBreaker(_cfg())

        async def cancelled():
            raise asyncio.CancelledError

        with self.assertRaises(asyncio.CancelledError):
            await br.call(cancelled)
        self.assertEqual(await br.state(), "closed")

        async def ok():
            return 1

        self.assertEqual(await br.call(ok), 1)

    async def test_retryable_predicate(self):
        calls = 0

        async def boom():
            nonlocal calls
            calls += 1
            raise ValueError("fatal")

        with self.assertRaises(ValueError):
            await run_with_retry(boom, attempts=5, retryable=lambda e: False)
        self.assertEqual(calls, 1)

    async def test_retry_eventually_succeeds(self):
        calls = 0

        async def flaky():
            nonlocal calls
            calls += 1
            if calls < 3:
                raise RuntimeError("flap")
            return "ok"

        self.assertEqual(await run_with_retry(flaky, attempts=5, base_s=0.001, cap_s=0.005), "ok")
        self.assertEqual(calls, 3)

    def test_backoff_bounds(self):
        for k in range(6):
            d = backoff_delay(k, 0.5, 8.0)
            self.assertGreaterEqual(d, 0.0)
            self.assertLessEqual(d, min(8.0, 0.5 * (2.0 ** k)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
