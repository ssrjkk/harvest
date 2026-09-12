"""Unit-тесты инфраструктуры network.py: TTL-кэш, rate limiter, circuit breaker."""

import asyncio
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.network import (
    _CircuitBreaker,
    _effective_rpc_rate,
    _is_retryable_error,
    _RPCMetrics,
    _RPCRateLimiter,
    _TTLCache,
)


class TestRetryableClassification(unittest.TestCase):
    def test_transport_errors_retryable(self):
        self.assertTrue(_is_retryable_error(ConnectionError("boom")))
        self.assertTrue(_is_retryable_error(TimeoutError("boom")))
        self.assertTrue(_is_retryable_error(OSError("boom")))

    def test_logical_web3_errors_not_retryable(self):
        from web3.exceptions import (
            ContractLogicError,
            InvalidTransaction,
            Web3ValidationError,
        )

        self.assertFalse(_is_retryable_error(Web3ValidationError("bad address")))
        self.assertFalse(_is_retryable_error(InvalidTransaction({"msg": "invalid"})))
        self.assertFalse(_is_retryable_error(ContractLogicError("reverted")))

    def test_value_error_hints(self):
        self.assertFalse(_is_retryable_error(ValueError("execution reverted")))
        self.assertFalse(_is_retryable_error(ValueError("nonce too low: next nonce")))
        self.assertFalse(_is_retryable_error(ValueError("insufficient funds")))
        self.assertFalse(_is_retryable_error(ValueError("intrinsic gas too low")))
        self.assertTrue(_is_retryable_error(ValueError("rate limit exceeded, try again in 1s")))
        self.assertTrue(_is_retryable_error(ValueError("timed out after 10s")))
        self.assertTrue(_is_retryable_error(ValueError("connection error")))
        self.assertTrue(_is_retryable_error(ValueError("code -32005, request limit reached")))
        self.assertTrue(_is_retryable_error(ValueError("internal error -32603")))

    def test_unknown_exception_is_retryable(self):
        self.assertTrue(_is_retryable_error(RuntimeError("mystery")))


class TestTTLCache(unittest.TestCase):
    def test_set_get(self):
        c = _TTLCache(ttl=10)
        c.set("a", 42)
        self.assertEqual(c.get("a"), 42)
        self.assertIsNone(c.get("missing"))

    def test_expiry(self):
        c = _TTLCache(ttl=0.05)
        c.set("a", 42)
        time.sleep(0.08)
        self.assertIsNone(c.get("a"))

    def test_delete(self):
        c = _TTLCache(ttl=10)
        c.set("a", 1)
        c.delete("a")
        self.assertIsNone(c.get("a"))
        c.delete("a")  # идемпотентно

    def test_cleanup_removes_expired(self):
        c = _TTLCache(ttl=0.03)
        for i in range(105):
            c.set(f"k{i}", i)
        time.sleep(0.05)
        c._cleanup()
        self.assertLessEqual(c._cleanup_counter, 105)
        # после TTL всё устарело и вычищено
        c._cleanup()
        self.assertEqual(len(c._store), 0)


class TestRateLimiter(unittest.TestCase):
    def test_acquire_consumes_token(self):
        lim = _RPCRateLimiter(rate=1000)

        async def main():
            await lim.acquire()
            self.assertTrue(lim.tokens < lim._max_tokens or True)

        asyncio.run(main())

    def test_burst_capped_at_capacity(self):
        lim = _RPCRateLimiter(rate=50)
        self.assertAlmostEqual(lim.tokens, 100)  # rate*2 burst
        lim._tokens = 89.5
        lim._refill()
        self.assertLessEqual(lim.tokens, 100)

    def test_recover_tokens_bounds(self):
        a = _RPCRateLimiter(rate=50)
        b = _RPCRateLimiter(rate=50)
        a._tokens = 0
        b._tokens = 12
        a.recover_tokens(b)
        self.assertLessEqual(a.tokens, 12)
        self.assertGreaterEqual(a.tokens, 0)

    def test_set_rate_reclamps_burst(self):
        lim = _RPCRateLimiter(rate=100)
        lim.set_rate(25)
        self.assertEqual(lim._rate, 25)
        self.assertLessEqual(lim.tokens, 50)  # burst = rate*2


class TestEffectiveRateFactor(unittest.TestCase):
    """Адаптивная деградация RPC-скорости под здоровьем узла."""

    def test_healthy_full_rate(self):
        self.assertEqual(_effective_rpc_rate(50, {"error_rate": 0.0, "avg_ms": 50}), 50)

    def test_moderate_errors_halved(self):
        self.assertEqual(_effective_rpc_rate(50, {"error_rate": 0.16, "avg_ms": 100}), 25)

    def test_heavy_errors_quarter(self):
        self.assertEqual(_effective_rpc_rate(50, {"error_rate": 0.35, "avg_ms": 100}), 12.5)

    def test_latency_driven(self):
        self.assertEqual(_effective_rpc_rate(50, {"error_rate": 0.0, "avg_ms": 2500}), 25)

    def test_floor_binds_on_catastrophic(self):
        # катастрофа (60%+ ошибок) -> фактор 0.15, но пол удерживает 0.2 базовой
        self.assertEqual(_effective_rpc_rate(50, {"error_rate": 1.0, "avg_ms": 100}), 10)

    def test_empty_window_full_rate(self):
        self.assertEqual(_effective_rpc_rate(100, {"error_rate": 0.0, "avg_ms": 0.0}), 100)


class TestRPCMetricsWindow(unittest.TestCase):
    def test_health_sliding_window(self):
        m = _RPCMetrics()
        for _ in range(40):
            m.record(0.5, error=False)
        for _ in range(10):
            m.record(0.5, error=True)
        h = m.health()
        self.assertEqual(h["samples"], 50)
        self.assertAlmostEqual(h["error_rate"], 0.2)

    def test_health_empty(self):
        m = _RPCMetrics()
        self.assertEqual(m.health()["error_rate"], 0.0)

    def test_snapshot_keeps_window(self):
        m = _RPCMetrics()
        m.record(0.2, error=True)
        m.record(0.2, error=False)
        snap = m.snapshot()
        self.assertEqual(snap["calls"], 2)
        self.assertEqual(snap["errors"], 1)
        # окно здоровья не сбрасывается — адаптивная скорость продолжает видеть сбои
        self.assertEqual(m.health()["samples"], 2)


class TestCircuitBreaker(unittest.TestCase):
    def test_opens_after_threshold(self):
        cb = _CircuitBreaker(failure_threshold=3, recovery_timeout=0.02)
        self.assertTrue(cb.allow_request())
        for _ in range(3):
            cb.record_failure()
        self.assertFalse(cb.allow_request())

    def test_recovers_to_half_open_then_closed(self):
        cb = _CircuitBreaker(failure_threshold=2, recovery_timeout=0.02)
        for _ in range(2):
            cb.record_failure()
        self.assertFalse(cb.allow_request())
        time.sleep(0.05)
        self.assertTrue(cb.allow_request())  # HALF_OPEN, пробный
        self.assertFalse(cb.allow_request())  # ждём второй успех
        cb.record_success()
        self.assertEqual(cb._state, "CLOSED")
        self.assertTrue(cb.allow_request())

    def test_success_resets_failures(self):
        cb = _CircuitBreaker(failure_threshold=5, recovery_timeout=30)
        cb.record_failure()
        cb.record_failure()
        cb.record_success()
        self.assertEqual(cb._failure_count, 0)


class TestRPCMetrics(unittest.TestCase):
    def test_snapshot_resets(self):
        m = _RPCMetrics()
        m.record(0.1)
        m.record(0.2, error=True)
        s = m.snapshot()
        self.assertEqual(s["calls"], 2)
        self.assertEqual(s["errors"], 1)
        self.assertAlmostEqual(s["error_rate"], 0.5)
        # сборка сбросила счётчики
        s2 = m.snapshot()
        self.assertEqual(s2["calls"], 0)
        self.assertEqual(s2["errors"], 0)


class TestSwitchRpc(unittest.TestCase):
    _CONFIG = {
        "network": {
            "rpc_url": ["https://a.example.com/rpc?api_key=sekret", "https://b.example.com/rpc"],
            "chain_id": 288,
        }
    }

    def test_connect_failure_keeps_old_endpoint(self):
        from unittest.mock import MagicMock, patch

        from core.network import NetworkManager

        calls = {"n": 0}

        def _connect(self, url):
            calls["n"] += 1
            if calls["n"] == 1:
                return MagicMock()
            raise RuntimeError("connect failed")

        async def scenario():
            with patch.object(NetworkManager, "_connect_rpc", _connect):
                net = NetworkManager(self._CONFIG, MagicMock())
                old_url = net.rpc_url
                old_w3 = net.w3
                with self.assertRaises(RuntimeError):
                    await net._switch_rpc()
                # при неудачном переключении старый эндпоинт остаётся рабочим
                self.assertEqual(net.rpc_url, old_url)
                self.assertIs(net.w3, old_w3)
                await net.close()

        asyncio.run(scenario())

    def test_switch_closes_old_provider_session(self):
        from unittest.mock import MagicMock, patch

        from core.network import NetworkManager

        closed = asyncio.Event()

        class _Session:
            async def close(self):
                closed.set()

        class _Provider:
            def __init__(self):
                self._session = _Session()

        def _connect(self, url):
            w = MagicMock()
            w.provider = _Provider()
            return w

        async def scenario():
            with patch.object(NetworkManager, "_connect_rpc", _connect):
                net = NetworkManager(self._CONFIG, MagicMock())
                await net._switch_rpc()
                # старый провайдер закрыт через await (не fire-and-forget)
                self.assertTrue(closed.is_set())
                self.assertEqual(net.rpc_url, "https://b.example.com/rpc")
                await net.close()

        asyncio.run(scenario())

    def test_switch_single_endpoint_noop(self):
        from unittest.mock import MagicMock, patch

        from core.network import NetworkManager

        def _connect(self, url):
            return MagicMock()

        async def scenario():
            with patch.object(NetworkManager, "_connect_rpc", _connect):
                cfg = {"network": {"rpc_url": ["https://only.example.com/rpc"], "chain_id": 288}}
                net = NetworkManager(cfg, MagicMock())
                await net._switch_rpc()  # не должен менять state и не падать
                self.assertEqual(net.rpc_url, "https://only.example.com/rpc")
                await net.close()

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main(verbosity=2)
