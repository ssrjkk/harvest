"""Тесты воркер-пула: маппинг результатов, приоритеты, динамический скейлинг,
мягкий stop (активные транзакции докручиваются), контракт worker_func.

Staggered start (start_delay) покрыт отдельно в tests/test_behavior.py: профиль
детерминированно задаёт задержку, а пул применяет её в _run_one (core/pool.py).
"""

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.workpool import WorkerPool  # noqa: E402


def _wallet(i: int) -> dict:
    return {
        "address": f"0x{i + 1:040x}",
        "private_key": "ab" * 32,
        "mnemonic": "",
        "total_actions": 0,
        "updated_at": 0,
    }


def _wallets(n: int) -> list[dict]:
    return [_wallet(i) for i in range(n)]


def _addrs(wallets: list[dict]) -> list[str]:
    return [w["address"] for w in wallets]


class TestWorkerPoolOrderMapping(unittest.IsolatedAsyncioTestCase):
    """Результат мэпится по адресу, а не по порядку завершения воркеров."""

    async def test_results_keyed_by_address(self):
        wallets = _wallets(8)
        addrs = _addrs(wallets)
        calls = []

        async def worker(wallet, all_addresses, cycle_number):
            calls.append(wallet["address"])
            await asyncio.sleep(0.005)
            return wallet["address"], 1

        pool = WorkerPool(max_workers=4, worker_func=worker, monitor_interval=0.02)
        try:
            await pool.start()
            for i in range(2):
                for w in wallets:
                    await pool.submit(w, addrs, cycle_number=i)
            out = await pool.join()
            for w in wallets:
                self.assertEqual(out[w["address"]], 1, "адрес должен иметь результат ровно 1")
            self.assertEqual(len(calls), 16, "все задачи обработаны")
        finally:
            await pool.stop()

    async def test_urgent_priority_runs_first(self):
        """Single-worker: urgent-задача обрабатывается раньше обычных (FIFO по seq)."""
        wallets = _wallets(4)
        addrs = _addrs(wallets)
        order = []

        async def worker(wallet, all_addresses, cycle_number):
            order.append(wallet["address"])
            await asyncio.sleep(0.0)
            return wallet["address"], 1

        pool = WorkerPool(max_workers=1, worker_func=worker, monitor_interval=10.0)
        try:
            # Наполняем очередь напрямую, чтобы единственный воркер гарантированно
            # увидел её уже заполненной (иначе гонка: воркер берёт задачу до submit).
            pool._queue.put_nowait((10, 1, (wallets[0], addrs, 1)))
            pool._queue.put_nowait((10, 2, (wallets[1], addrs, 1)))
            pool._queue.put_nowait((10, 3, (wallets[2], addrs, 1)))
            pool._queue.put_nowait((0, 4, (wallets[3], addrs, 1)))
            await pool.start()
            out = await pool.join()
            self.assertEqual(order[0], wallets[3]["address"], "urgent должен обработаться первым")
            self.assertEqual(len(order), 4)
            self.assertEqual(set(out.values()), {1})
        finally:
            await pool.stop()


class TestWorkerPoolScale(unittest.IsolatedAsyncioTestCase):
    """Динамический скейлинг под здоровьем RPC (health_fn)."""

    async def test_health_factor_scales_workers(self):
        wallets = _wallets(16)
        addrs = _addrs(wallets)
        current_health = [1.0]

        async def worker(wallet, all_addresses, cycle_number):
            await asyncio.sleep(0.01)
            return wallet["address"], 1

        def health():
            return current_health[0]

        pool = WorkerPool(max_workers=8, worker_func=worker, health_fn=health, monitor_interval=0.05)
        try:
            await pool.start()
            for w in wallets:
                await pool.submit(w, addrs, 1)
            await asyncio.sleep(0.2)
            full = pool.current_workers
            self.assertGreater(full, 0)
            self.assertEqual(full, 8, "при здоровье 1.0 пул раздувается до потолка")

            # Здоровье падает в 4 раза — воркеры расформировываются «на пенсию».
            current_health[0] = 0.25
            await asyncio.sleep(0.3)
            self.assertLess(pool.current_workers, full)
            self.assertGreaterEqual(pool.current_workers, 1)

            # Здоровье восстанавливается — пул снова растёт.
            current_health[0] = 1.0
            await asyncio.sleep(0.3)
            self.assertGreaterEqual(pool.current_workers, full // 2)
        finally:
            await pool.stop()


class TestWorkerPoolShutdown(unittest.IsolatedAsyncioTestCase):
    """Stop не рвёт активные задачи: занятые воркеры докручиваются."""

    async def test_stop_lets_active_workers_finish(self):
        wallets = _wallets(2)
        addrs = _addrs(wallets)
        finished = []
        started = asyncio.Event()

        async def worker(wallet, all_addresses, cycle_number):
            started.set()
            await asyncio.sleep(0.2)
            finished.append(wallet["address"])
            return wallet["address"], 1

        pool = WorkerPool(max_workers=1, worker_func=worker, monitor_interval=10.0)
        try:
            await pool.start()
            await pool.submit(wallets[0], addrs, 1)
            await started.wait()
            await pool.stop()  # должен дождаться завершения активного воркера
            self.assertIn(wallets[0]["address"], finished)
        finally:
            await pool.stop()


class TestWorkerPoolContract(unittest.IsolatedAsyncioTestCase):
    """Контракт worker_func: (wallet: dict, all_addresses: list, cycle_number: int) -> (address, count)."""

    async def test_contract_signature(self):
        how_many = {"called": 0}

        async def worker(wallet, all_addresses, cycle_number):
            how_many["called"] += 1
            self.assertIsInstance(wallet, dict)
            self.assertIsInstance(all_addresses, list)
            self.assertIsInstance(cycle_number, int)
            return wallet["address"], 1

        pool = WorkerPool(max_workers=2, worker_func=worker, monitor_interval=10.0)
        try:
            await pool.start()
            w = _wallet(1)
            await pool.submit(w, [w["address"]], cycle_number=3)
            out = await pool.join()
            self.assertEqual(out[w["address"]], 1)
            self.assertEqual(how_many["called"], 1)
        finally:
            await pool.stop()


if __name__ == "__main__":
    unittest.main()
