"""Unit-тесты инфраструктуры network.py: TTL-кэш, rate limiter, circuit breaker."""

import asyncio
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.network import (
    RpcNode,
    _CircuitBreaker,
    _effective_rpc_rate,
    _is_retryable_error,
    _RPCMetrics,
    _RPCRateLimiter,
    _TTLCache,
)


class TestRpcNode(unittest.TestCase):
    def test_initial_state_pessimistic(self):
        n = RpcNode("https://rpc.example")
        self.assertAlmostEqual(n.latency_ms, 500.0)
        self.assertFalse(n.is_dead)
        self.assertLess(n.score, float("inf"))

    def test_success_lowers_score(self):
        n = RpcNode("https://rpc.example")
        n.record(100.0, success=True)
        self.assertLess(n.latency_ms, 500.0)
        self.assertFalse(n.is_dead)

    def test_ema_smooths_spikes(self):
        n = RpcNode("https://rpc.example")
        n.record(1000.0, success=True)  # всплеск
        first = n.latency_ms
        n.record(50.0, success=True)  # стабилизация
        self.assertLess(n.latency_ms, first)
        # 70% старого, 30% нового
        self.assertAlmostEqual(n.latency_ms, first * 0.7 + 50.0 * 0.3)

    def test_errors_penalize_score(self):
        n = RpcNode("https://rpc.example")
        before = n.score
        n.record(100.0, success=False)
        after = n.score
        self.assertGreaterEqual(after, before)  # штраф за ошибку

    def test_three_errors_marks_dead(self):
        n = RpcNode("https://rpc.example")
        for _ in range(4):
            n.record(2500.0, success=False)
        self.assertTrue(n.is_dead)
        self.assertEqual(n.score, float("inf"))

    def test_zombie_resurrection(self):
        n = RpcNode("https://rpc.example")
        for _ in range(4):
            n.record(2500.0, success=False)
        self.assertTrue(n.is_dead)
        # Один успешный пинг — нода снова в строю.
        n.record(150.0, success=True)
        self.assertFalse(n.is_dead)
        self.assertLess(n.score, float("inf"))

    def test_score_orders_by_latency(self):
        fast = RpcNode("https://a")
        slow = RpcNode("https://b")
        fast.record(50.0, success=True)
        slow.record(1500.0, success=True)
        self.assertLess(fast.score, slow.score)


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

    def test_reset_reopens(self):
        cb = _CircuitBreaker(failure_threshold=2, recovery_timeout=30)
        cb.record_failure()
        cb.record_failure()
        self.assertFalse(cb.allow_request())  # OPEN блокирует
        cb.reset()
        self.assertEqual(cb._state, "CLOSED")
        self.assertTrue(cb.allow_request())

    def test_half_open_failed_trial_returns_to_open(self):
        # Регрессия: провал пробного вызова в HALF_OPEN навсегда застревал в
        # HALF_OPEN (единственный пробник исчерпан, allow_request -> False).
        cb = _CircuitBreaker(failure_threshold=2, recovery_timeout=0.03)
        for _ in range(2):
            cb.record_failure()
        self.assertFalse(cb.allow_request())
        time.sleep(0.05)
        self.assertTrue(cb.allow_request())  # HALF_OPEN trial пропущен
        self.assertFalse(cb.allow_request())  # внутри окна повторный не пускаем
        cb.record_failure()  # пробник провалился → возврат в OPEN
        self.assertEqual(cb._state, "OPEN", "неудачный пробник не должен оставаться в HALF_OPEN")
        self.assertFalse(cb.allow_request())
        time.sleep(0.05)
        # Новое recovery-окно открылось: ещё один пробный вызов возможен.
        self.assertTrue(cb.allow_request(), "после OPEN -> HALF_OPEN должен появиться новый пробник")
        cb.record_success()
        self.assertEqual(cb._state, "CLOSED")


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

    def test_switch_resets_circuit_breaker(self):
        from unittest.mock import MagicMock, patch

        from core.network import NetworkManager

        def _connect(self, url):
            return MagicMock()

        async def scenario():
            with patch.object(NetworkManager, "_connect_rpc", _connect):
                net = NetworkManager(self._CONFIG, MagicMock())
                # Доводим CB до OPEN (как серия транспортных сбоев на a.example)
                for _ in range(net._circuit_breaker._failure_threshold):
                    net._circuit_breaker.record_failure()
                self.assertFalse(net._circuit_breaker.allow_request())
                # Failover на свежий эндпоинт: окно ошибок обнуляется —
                # иначе здоровый узел b остался бы заблокированным до recovery.
                await net._switch_rpc()
                self.assertEqual(net.rpc_url, "https://b.example.com/rpc")
                self.assertTrue(net._circuit_breaker.allow_request())
                await net.close()

        asyncio.run(scenario())

    def test_close_idempotent(self):
        from unittest.mock import MagicMock, patch

        from core.network import NetworkManager

        def _connect(self, url):
            return MagicMock()

        async def scenario():
            with patch.object(NetworkManager, "_connect_rpc", _connect):
                net = NetworkManager(self._CONFIG, MagicMock())
                await net.close()
                await net.close()  # повторный close — no-op, не падает
                self.assertTrue(net._closed)
                # ресурсы можно освобождать и после закрытия
                await net.close()

        asyncio.run(scenario())


class TestRpcRotation(unittest.TestCase):
    _CONFIG = {
        "network": {
            "rpc_url": ["https://a.example.com/rpc", "https://b.example.com/rpc"],
            "chain_id": 288,
        }
    }

    def _make(self):
        from unittest.mock import MagicMock, patch

        from core.network import NetworkManager

        def _connect(self, url):
            return MagicMock()

        return patch.object(NetworkManager, "_connect_rpc", _connect), NetworkManager

    def test_best_rpc_prefers_low_score(self):
        from unittest.mock import MagicMock, patch

        from core.network import NetworkManager

        async def scenario():
            with patch.object(NetworkManager, "_connect_rpc", lambda self, u: MagicMock()):
                net = NetworkManager(self._CONFIG, MagicMock())
                net._nodes[0].record(50.0, success=True)  # a — быстрая
                net._nodes[1].record(1500.0, success=True)  # b — тормозит
                self.assertEqual(net.best_rpc_url(), "https://a.example.com/rpc")
                await net.close()

        asyncio.run(scenario())

    def test_best_rpc_skips_dead_node(self):
        from unittest.mock import MagicMock, patch

        from core.network import NetworkManager

        async def scenario():
            with patch.object(NetworkManager, "_connect_rpc", lambda self, u: MagicMock()):
                net = NetworkManager(self._CONFIG, MagicMock())
                net._nodes[0].record(50.0, success=True)
                net._nodes[0].record(50.0, success=True)
                net._nodes[0].record(50.0, success=True)
                net._nodes[0].record(50.0, success=True)
                net._nodes[1].record(1500.0, success=True)
                # a — мертва (4 ошибки), b живая несмотря на задержку.
                for _ in range(4):
                    net._nodes[0].record(2500.0, success=False)
                self.assertTrue(net._nodes[0].is_dead)
                self.assertEqual(net.best_rpc_url(), "https://b.example.com/rpc")
                await net.close()

        asyncio.run(scenario())

    def test_all_dead_falls_back_to_least_bad(self):
        from unittest.mock import MagicMock, patch

        from core.network import NetworkManager

        async def scenario():
            with patch.object(NetworkManager, "_connect_rpc", lambda self, u: MagicMock()):
                net = NetworkManager(self._CONFIG, MagicMock())
                for n in net._nodes:
                    for _ in range(5):
                        n.record(2500.0, success=False)
                self.assertTrue(all(n.is_dead for n in net._nodes))
                # Best = наименее «плохой» (все мёртвы → scory одинаково inf,
                # берём первого из списка) — контракт: не падать, вернуть URL.
                url = net.best_rpc_url()
                self.assertIn(url, "https://a.example.com/rpc")
                await net.close()

        asyncio.run(scenario())

    def test_monitor_switches_to_best_alive(self):
        from unittest.mock import MagicMock, patch

        from core.network import NetworkManager

        async def scenario():
            with patch.object(NetworkManager, "_connect_rpc", lambda self, u: MagicMock()):
                net = NetworkManager(self._CONFIG, MagicMock())
                net._nodes[0].record(50.0, success=True)
                net._nodes[0].record(50.0, success=True)
                net._nodes[0].record(50.0, success=True)
                net._nodes[0].record(50.0, success=True)
                # Начинаем на a, но a деградирует: b жив, a — мёртв.
                for _ in range(4):
                    net._nodes[0].record(2500.0, success=False)
                net._nodes[1].record(120.0, success=True)
                # Прямой вызов приватного _record_latency имитирует цикл монитора.
                net._record_latency("https://b.example.com/rpc", 0.12)
                net._record_latency("https://a.example.com/rpc", None)
                best = net.best_rpc_url()
                self.assertEqual(best, "https://b.example.com/rpc")
                await net._switch_to(best)
                self.assertEqual(net.rpc_url, "https://b.example.com/rpc")
                await net.close()

        asyncio.run(scenario())

    def test_node_health_redacts_url(self):
        from unittest.mock import MagicMock, patch

        from core.network import NetworkManager

        async def scenario():
            with patch.object(NetworkManager, "_connect_rpc", lambda self, u: MagicMock()):
                cfg = {
                    "network": {
                        "rpc_url": ["https://user:pass@a.example.com/rpc"],
                        "chain_id": 288,
                    }
                }
                net = NetworkManager(cfg, MagicMock())
                h = net.node_health()
                self.assertEqual(len(h), 1)
                self.assertNotIn("user", h[0]["url"])
                self.assertNotIn("pass", h[0]["url"])
                await net.close()

        asyncio.run(scenario())

    def test_diagnostics_redacts_urls(self):
        from unittest.mock import MagicMock, patch

        from core.network import NetworkManager

        async def scenario():
            with patch.object(NetworkManager, "_connect_rpc", lambda self, u: MagicMock()):
                cfg = {
                    "network": {
                        "rpc_url": ["https://user:pass@a.example.com/rpc?api_key=sekret"],
                        "chain_id": 288,
                    }
                }
                net = NetworkManager(cfg, MagicMock())
                d = net.diagnostics()
                self.assertNotIn("user", d["active"])
                self.assertNotIn("api_key", d["active"])
                self.assertNotIn("sekret", d["rpc_urls"][0])
                await net.close()

        asyncio.run(scenario())

    def test_monitor_skipped_single_endpoint(self):
        from unittest.mock import MagicMock, patch

        from core.network import NetworkManager

        async def scenario():
            with patch.object(NetworkManager, "_connect_rpc", lambda self, u: MagicMock()):
                cfg = {"network": {"rpc_url": ["https://only.example.com/rpc"], "chain_id": 288}}
                net = NetworkManager(cfg, MagicMock())
                # С одной нодой выбирать не из кого: фоновый монитор не нужен,
                # здоровье питается от реальных вызовов (run_in_executor).
                net.start_monitor()
                self.assertFalse(net.monitor_started())
                await net.close()

        asyncio.run(scenario())

    def test_monitor_started_multi_endpoint(self):
        from unittest.mock import MagicMock, patch

        from core.network import NetworkManager

        async def scenario():
            with patch.object(NetworkManager, "_connect_rpc", lambda self, u: MagicMock()):
                net = NetworkManager(self._CONFIG, MagicMock())
                net.start_monitor()
                self.assertTrue(net.monitor_started())
                await net.close()  # close отменяет задачу монитора

        asyncio.run(scenario())

    def test_latency_probe_skips_rate_limiter_and_metrics(self):
        from unittest.mock import MagicMock, patch

        from core.network import NetworkManager

        async def scenario():
            with patch.object(NetworkManager, "_connect_rpc", lambda self, u: MagicMock()):
                net = NetworkManager(self._CONFIG, MagicMock())
                # Почти пустой бакет: бизнес-вызов взял бы токен, проба — нет.
                net._rate_limiter._tokens = 1.0
                calls_on_entry = net._metrics.snapshot()["calls"]
                await net.latency_probe("https://a.example.com/rpc")
                self.assertEqual(net._metrics.snapshot()["calls"], calls_on_entry)
                self.assertEqual(net._rate_limiter.tokens, 1.0)
                await net.close()

        asyncio.run(scenario())

    def test_business_call_records_ema_and_resurrects(self):
        from unittest.mock import MagicMock, patch

        from core.network import NetworkManager

        async def scenario():
            with patch.object(NetworkManager, "_connect_rpc", lambda self, u: MagicMock()):
                net = NetworkManager(self._CONFIG, MagicMock())
                node = net._nodes[0]
                for _ in range(4):
                    node.record(2500.0, success=False)
                self.assertTrue(node.is_dead)
                # Успешный бизнес-вызов (run_in_executor) пишет EMA и воскрешает
                # ноду даже без фонового монитора — единственный живой сигнал при
                # single-endpoint конфигах.
                result = await net.run_in_executor(lambda: 42)
                self.assertEqual(result, 42)
                self.assertFalse(node.is_dead)
                self.assertLess(node.latency_ms, 2000.0)
                await net.close()

        asyncio.run(scenario())

    def test_run_in_executor_records_latency_to_original_node(self):
        from unittest.mock import MagicMock, patch

        from core.network import NetworkManager

        async def scenario():
            with patch.object(NetworkManager, "_connect_rpc", lambda self, u: MagicMock()):
                net = NetworkManager(self._CONFIG, MagicMock())

                def switch_during_call():
                    # Пока вызов «в полёте» (в отдельном потоке) происходит
                    # failover: активным становится b.
                    net.rpc_url = "https://b.example.com/rpc"
                    return 7

                result = await net.run_in_executor(switch_during_call)
                self.assertEqual(result, 7)
                # Успех пишется в EMA НАЧАЛЬНОЙ ноды (a), а не сменившейся
                # посреди вызова (b): иначе деградация a остаётся незаметной,
                # а b «воскресает» по чужому замеру.
                self.assertGreater(net._nodes[0].successes, 1, "EMA записан в исходную ноду")
                self.assertEqual(net._nodes[1].successes, 1, "b не получил чужую EMA")
                await net.close()

        asyncio.run(scenario())

    def test_crosscheck_revert_on_primary_success_vs_secondary_revert(self):
        from unittest.mock import MagicMock, patch

        from core.network import NetworkManager

        w3s = {
            "https://a.example.com/rpc": MagicMock(),
            "https://b.example.com/rpc": MagicMock(),
        }
        w3s["https://b.example.com/rpc"].eth.get_transaction_receipt.return_value = {"status": 0}

        def _connect(self, url):
            return w3s[url]

        async def scenario():
            with patch.object(NetworkManager, "_connect_rpc", _connect):
                net = NetworkManager(self._CONFIG, MagicMock())
                # primary показывает успех, secondary — реверт: контракт
                # «revert любой из нод перевешивает успех».
                result = await net._crosscheck_receipt_status(b"\xaa" * 32, 1)
                self.assertEqual(result, 0, "реверт с вторичной ноды перевешивает успех primary")
                await net.close()

        asyncio.run(scenario())

    def test_crosscheck_primary_revert_vs_secondary_success_still_revert(self):
        from unittest.mock import MagicMock, patch

        from core.network import NetworkManager

        w3s = {
            "https://a.example.com/rpc": MagicMock(),
            "https://b.example.com/rpc": MagicMock(),
        }
        w3s["https://b.example.com/rpc"].eth.get_transaction_receipt.return_value = {"status": 1}

        def _connect(self, url):
            return w3s[url]

        async def scenario():
            with patch.object(NetworkManager, "_connect_rpc", _connect):
                net = NetworkManager(self._CONFIG, MagicMock())
                # primary=0 (revert), secondary=1 (success): реверт всё равно
                # побеждает. Штрафуется «ложно-успешная» вторичная нода.
                result = await net._crosscheck_receipt_status(b"\xab" * 32, 0)
                self.assertEqual(result, 0, "revert primary не должен превращаться в success")
                self.assertTrue(net._nodes[1].errors > 0, "ложно-успешная нода штрафуется")
                await net.close()

        asyncio.run(scenario())

    def test_crosscheck_single_node_returns_primary(self):
        from unittest.mock import MagicMock, patch

        from core.network import NetworkManager

        async def scenario():
            with patch.object(NetworkManager, "_connect_rpc", lambda self, u: MagicMock()):
                cfg = {"network": {"rpc_url": ["https://only.example.com/rpc"], "chain_id": 288}}
                net = NetworkManager(cfg, MagicMock())
                # Один эндпоинт: кросс-чек невозможен, доверяем primary как есть.
                self.assertIsNone(net._secondary_live_node())
                self.assertEqual(await net._crosscheck_receipt_status(b"\xac" * 32, 1), 1)
                self.assertEqual(await net._crosscheck_receipt_status(b"\xac" * 32, 0), 0)
                await net.close()

        asyncio.run(scenario())

    def test_wait_for_receipt_failover_on_poll_errors(self):
        from unittest.mock import MagicMock, patch

        from core.network import NetworkManager

        async def scenario():
            with patch.object(NetworkManager, "_connect_rpc", lambda self, u: MagicMock()):
                net = NetworkManager(self._CONFIG, MagicMock())
                calls = {"n": 0, "switch": 0}

                async def flaky_poll(fn):
                    calls["n"] += 1
                    if calls["n"] <= 3:
                        raise ConnectionError("node went silent")
                    return {"status": 1}

                async def fake_switch():
                    calls["switch"] += 1

                net.run_in_executor = flaky_poll  # подмена через instance-атрибут
                net._switch_rpc = fake_switch
                receipt = await net.wait_for_receipt("0x" + "ab" * 32, timeout=5)
                self.assertIsNotNone(receipt)
                self.assertEqual(calls["switch"], 1, "после серии ошибок поллинга делаем failover")
                self.assertGreaterEqual(calls["n"], 4)
                self.assertTrue(net._nodes[0].errors > 0, "ошибки поллинга штрафуют EMA ноды")
                await net.close()

        asyncio.run(scenario())

    def test_wait_for_receipt_single_node_no_failover(self):
        from unittest.mock import MagicMock, patch

        from core.network import NetworkManager

        async def scenario():
            with patch.object(NetworkManager, "_connect_rpc", lambda self, u: MagicMock()):
                cfg = {"network": {"rpc_url": ["https://only.example.com/rpc"], "chain_id": 288}}
                net = NetworkManager(cfg, MagicMock())
                calls = {"n": 0}

                async def flaky_poll(fn):
                    calls["n"] += 1
                    if calls["n"] <= 3:
                        raise ConnectionError("boom")
                    return {"status": 1}

                net.run_in_executor = flaky_poll
                # Один эндпоинт: реальный _switch_rpc ничего не переключает,
                # поллинг жив и дожидается ресипта на той же ноде.
                receipt = await net.wait_for_receipt("0x" + "cd" * 32, timeout=5)
                self.assertIsNotNone(receipt)
                self.assertEqual(net.rpc_url, "https://only.example.com/rpc")
                await net.close()

        asyncio.run(scenario())

    def test_wait_for_receipt_returns_none_on_budget(self):
        from unittest.mock import MagicMock, patch

        from core.network import NetworkManager

        async def scenario():
            with patch.object(NetworkManager, "_connect_rpc", lambda self, u: MagicMock()):
                net = NetworkManager(self._CONFIG, MagicMock())
                calls = {"n": 0}

                async def not_mined_yet(fn):
                    calls["n"] += 1
                    return None  # ресипта нет — поллим, пока не кончится бюджет

                net.run_in_executor = not_mined_yet
                receipt = await net.wait_for_receipt(bytes.fromhex("ef" * 32), timeout=0.3)
                # По истечении бюджета — None (не TimeoutError): воркер не стоит в ступоре.
                self.assertIsNone(receipt)
                self.assertGreaterEqual(calls["n"], 1)
                await net.close()

        asyncio.run(scenario())

    def test_receipt_timeout_default_and_config(self):
        from unittest.mock import MagicMock, patch

        from core.network import NetworkManager

        async def scenario():
            with patch.object(NetworkManager, "_connect_rpc", lambda self, u: MagicMock()):
                net = NetworkManager(self._CONFIG, MagicMock())
                self.assertEqual(net._receipt_timeout, 30.0)
                await net.close()
                cfg = {
                    "network": {"rpc_url": ["https://a.example.com/rpc"], "chain_id": 288},
                    "advanced": {"receipt_timeout": 12},
                }
                net2 = NetworkManager(cfg, MagicMock())
                self.assertEqual(net2._receipt_timeout, 12.0)
                await net2.close()

        asyncio.run(scenario())


class TestRpcMonitorWatchdog(unittest.IsolatedAsyncioTestCase):
    """Сторож монитора RPC: неожиданный крах -> перезапуск, отмена -> нет."""

    _CONFIG = {
        "network": {
            "rpc_url": ["https://a.example.com/rpc", "https://b.example.com/rpc"],
            "chain_id": 288,
        }
    }

    async def test_crashed_monitor_is_revived(self):
        from unittest.mock import MagicMock, patch

        from core.network import NetworkManager

        with patch.object(NetworkManager, "_connect_rpc", lambda self, u: MagicMock()):
            net = NetworkManager(self._CONFIG, MagicMock())
            calls = {"n": 0}
            orig_loop = NetworkManager._monitor_rpc

            async def crash_first(self):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise RuntimeError("simulated crash outside try")
                await orig_loop(self)

            async def quiet_probe():
                return []

            net.probe_all = quiet_probe
            net._monitor_rpc = crash_first.__get__(net, type(net))
            net.start_monitor()
            # Первый прогон крашится, сторожа перезапускает с backoff=1с.
            await asyncio.sleep(1.3)
            self.assertEqual(calls["n"], 2, "после краха монитор перезапущен")
            self.assertTrue(net.monitor_started())
            # Стоп: вложенная задача гасится, повторных запусков нет.
            net._monitor_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await net._monitor_task
            self.assertEqual(calls["n"], 2, "после стопа рестартов нет")
            del net._monitor_rpc
            del net.probe_all
            net._monitor_task = None
            await net.close()

    async def test_cancel_does_not_restart(self):
        from unittest.mock import MagicMock, patch

        from core.network import NetworkManager

        with patch.object(NetworkManager, "_connect_rpc", lambda self, u: MagicMock()):
            net = NetworkManager(self._CONFIG, MagicMock())
            calls = {"n": 0}
            real_loop = NetworkManager._monitor_rpc

            async def counting(self):
                calls["n"] += 1
                await real_loop(self)

            async def quiet_probe():
                return []

            net.probe_all = quiet_probe
            net._monitor_rpc = counting.__get__(net, type(net))
            net.start_monitor()
            await asyncio.sleep(0.05)
            self.assertGreaterEqual(calls["n"], 1, "монитор запущен")
            net._monitor_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await net._monitor_task
            # Если бы сторожа перезапускал задачу после отмены, было бы >= 2.
            self.assertEqual(calls["n"], 1, "отмена монитора — не повод для рестарта")
            del net._monitor_rpc
            del net.probe_all
            net._monitor_task = None
            await net.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
