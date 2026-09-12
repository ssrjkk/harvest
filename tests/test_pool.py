"""Тесты пула: сохранение crash-state при отмене и восстановление прогресса."""

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import load_config  # noqa: E402
from core.database import Database  # noqa: E402
from core.pool import FarmerPool  # noqa: E402


def _wallet(i: int) -> dict:
    return {
        "address": f"0x{i + 1:040x}",
        "private_key": "ab" * 32,
        "mnemonic": "",
        "total_actions": 0,
        "updated_at": 0,
    }


class TestPoolCrashRecovery(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.dir = Path(self.td.name)
        cfg = load_config("config.yaml") or {}
        cfg["database"] = {**cfg.get("database", {}), "path": str(self.dir / "state.db")}
        # Локальный несуществующий RPC: TCP-connect немедленно отклоняется,
        # и сетевые попытки не висят по таймауту реальных тестнетов.
        cfg["network"] = {**cfg.get("network", {}), "rpc_url": "http://127.0.0.1:1/rpc"}
        cfg["state_file"] = str(self.dir / "state.json")
        cfg["threading"] = {**cfg.get("threading", {}), "max_workers": 4}
        self.cfg = cfg
        self.wallets = [_wallet(i) for i in range(6)]
        self.addrs = [w["address"] for w in self.wallets]

    def tearDown(self):
        self.td.cleanup()

    async def _pool_db(self):
        db = Database(self.cfg["database"]["path"])
        await db.init()
        pool = FarmerPool(self.cfg, db)
        return db, pool

    async def test_cancel_saves_crash_state(self):
        db, pool = await self._pool_db()
        try:
            # Пропускаем сетевой pre-validate крана, чтобы отмена гарантированно
            # попадала внутрь обработки чанков (а не в фазу валидации).
            pool._faucet_validated = True
            task = asyncio.create_task(pool.run_once(self.wallets, self.addrs, cycle_number=1))
            await asyncio.sleep(0.15)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            # Состояние должно быть сохранено даже при отмене посреди цикла.
            state_path = Path(self.cfg["state_file"])
            self.assertTrue(state_path.exists(), "crash-state не сохранён при отмене")
            st = pool._state
            await st.load()
            self.assertEqual(st.cycle_number, 1)
            self.assertTrue(st.processed_addresses.issubset(set(self.addrs)))
        finally:
            await pool.close()
            await db.close()

    async def test_resume_skips_done_addresses(self):
        db, pool = await self._pool_db()
        try:
            pool._faucet_validated = True
            # Симулируем прерванный цикл: один адрес уже обработан.
            st = pool._state
            st.cycle_number = 1
            st.processed_addresses.add(self.wallets[0]["address"])
            await st.save()

            final = await pool.run_once(self.wallets, self.addrs, cycle_number=1)

            # После полного цикла c живым (или недоступным) RPC конкуренция
            # остаётся валидной: воркеры 1..max_workers, health 0.15..1.0.
            st = pool.live_stats()
            self.assertGreaterEqual(st["dyn_workers"], 1)
            self.assertLessEqual(st["dyn_workers"], 4)
            self.assertGreaterEqual(st["health"], 0.15)
            self.assertLessEqual(st["health"], 1.0)

            processed = {a for a, _ in final}
            self.assertTrue(self.wallets[0]["address"] not in processed)
            self.assertEqual(len(processed), len(self.wallets) - 1)
            # Полный цикл прошёл — состояние стёрто (файла больше нет).
            self.assertFalse(Path(self.cfg["state_file"]).exists())
        finally:
            await pool.close()
            await db.close()


if __name__ == "__main__":
    unittest.main()
