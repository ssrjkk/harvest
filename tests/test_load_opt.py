"""Нагрузочные тесты (opt-in, в обычном CI — skip).

Запуск:  HARVEST_LOAD=1 python -m pytest tests/test_load_opt.py -q
Масштаб: HARVEST_LOAD_WALLETS=1000 (по умолчанию 300).

Проверяет пропускную способность генерации кошельков, реакцию
WorkerPool на здоровье RPC и соблюдение бюджета _RPCRateLimiter.
Критерии приёмки — внутри тестов (см. assert'ы и вывод [load]).
"""

import asyncio
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.database import Database
from core.network import _RPCRateLimiter
from core.wallet import WalletManager
from core.workpool import WorkerPool

RUN_LOAD = os.environ.get("HARVEST_LOAD") == "1"


@unittest.skipUnless(RUN_LOAD, "нагрузочные: только с HARVEST_LOAD=1")
class TestWalletThroughput(unittest.IsolatedAsyncioTestCase):
    async def test_generation_throughput_and_encryption(self):
        n = int(os.environ.get("HARVEST_LOAD_WALLETS", "300"))
        with tempfile.TemporaryDirectory() as td:
            db = Database(str(Path(td) / "load.db"), master_key=os.urandom(32))
            await db.init()
            wm = WalletManager({"threading": {"gen_workers": 4}}, db)
            t0 = time.monotonic()
            wallets = await wm.create_wallets(n)
            dt = time.monotonic() - t0
            self.assertEqual(len(wallets), n)
            self.assertEqual(await db.count_wallets(), n)
            # Все seed-данные — в шифротексте, открытого текста в БД нет.
            self.assertTrue(await db.any_seed_encrypted())
            addrs = {w["address"] for w in wallets}
            self.assertEqual(len(addrs), n, "дубликаты адресов при массовой генерации")
            await db.close()
        rate = n / max(dt, 1e-9)
        print(f"\n[load] wallets: {n} за {dt:.1f}с = {rate:.1f}/с (цель: >5/с smoke, проектная ~60/с)")
        self.assertGreater(rate, 5.0, "генерация подозрительно медленная")


@unittest.skipUnless(RUN_LOAD, "нагрузочные: только с HARVEST_LOAD=1")
class TestPoolScaling(unittest.IsolatedAsyncioTestCase):
    async def test_scales_with_health(self):
        health = {"v": 1.0}

        async def fn(wallet, _addrs, _cycle):
            await asyncio.sleep(0.05)
            return wallet["address"], 1

        pool = WorkerPool(
            max_workers=12, worker_func=fn,
            health_fn=lambda: health["v"], monitor_interval=0.2,
        )
        await pool.start()
        total = 60
        for i in range(total):
            await pool.submit({"address": f"0x{i:040x}"}, [])
        await asyncio.sleep(0.6)
        full = pool.current_workers
        health["v"] = 0.3
        await asyncio.sleep(0.9)
        low = pool.current_workers
        res = await pool.join()
        await pool.stop()
        print(f"\n[load] workers: full={full} -> degraded={low} (цели 12 и 3)")
        self.assertEqual(len(res), total, "потеря задач при скейлинге")
        self.assertGreaterEqual(full, 10, "пул не развернулся на здоровой ноде")
        self.assertLessEqual(low, 6, f"пул не ужался при деградации: {low}")


@unittest.skipUnless(RUN_LOAD, "нагрузочные: только с HARVEST_LOAD=1")
class TestRateLimiterBudget(unittest.IsolatedAsyncioTestCase):
    async def test_acquire_respects_rate(self):
        lim = _RPCRateLimiter(rate=20.0)  # burst 40 токенов
        t0 = time.monotonic()
        for _ in range(60):
            await lim.acquire()
        dt = time.monotonic() - t0
        # 40 бесплатных + ~20 платных по 1/20с ≈ 1.0с.
        print(f"\n[load] rate-limiter: 60 acquire за {dt:.2f}с (ожидание ~1.0с)")
        self.assertGreaterEqual(dt, 0.5, "лимитер не держит бюджет (слишком быстро)")
        self.assertLess(dt, 5.0, "лимитер душит сильнее бюджета")


if __name__ == "__main__":
    unittest.main(verbosity=2)
