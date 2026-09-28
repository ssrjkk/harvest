"""Тесты пула: сохранение crash-state при отмене и восстановление прогресса."""

import asyncio
import json
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


class TestCycleStateSaveDebounce(unittest.IsolatedAsyncioTestCase):
    """mark_done не переписывает state-файл O(n) на каждом 50-м адресе."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.dir = Path(self.td.name)

    def tearDown(self):
        self.td.cleanup()

    async def test_save_debounced_by_time_and_batch(self):
        from core.pool import _CycleState

        clock = {"t": 0.0}
        state = _CycleState(str(self.dir / "state.json"))
        state._now = lambda: clock["t"]

        for i in range(50):
            await state.mark_done(f"0x{i + 1:040x}")
        self.assertFalse(
            (self.dir / "state.json").exists(),
            "50 адресов за <2с — автосейв не дёргаем",
        )

        # Батч ≥50 И прошло ≥2с — только тогда пишем в файл.
        clock["t"] += 3.0
        await state.mark_done(f"0x{51:040x}")
        path = self.dir / "state.json"
        self.assertTrue(path.exists(), "батч ≥50 и ≥2с — сейв выполнен")
        first = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(len(first["processed_addresses"]), 51)

        # После свежего save счётчик обнулён: один адрес без батча не трогает файл,
        # даже если время прошло.
        clock["t"] += 5.0
        await state.mark_done(f"0x{52:040x}")
        second = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(len(second["processed_addresses"]), 51)
        self.assertEqual(len(state.processed_addresses), 52, "память свежее файла")

    async def test_clear_overwrites_state_when_unlink_fails(self):
        """Если end-of-cycle unlink не удался (Windows lock), файл перезаписывается
        пустым состоянием — иначе следующий запуск молча отфильтрует все кошельки."""
        from unittest.mock import patch

        from core.pool import _CycleState

        st = _CycleState(str(self.dir / "state.json"))
        st.cycle_number = 1
        st.processed_addresses.add("0x" + "1" * 40)
        await st.save()

        with patch("core.pool._unlink_state_file", return_value=False):
            await st.clear()

        # Файл существует (не удалился), но теперь «пустой»: load() не видит прогресса.
        self.assertTrue((self.dir / "state.json").exists())
        loaded = _CycleState(str(self.dir / "state.json"))
        self.assertFalse(await loaded.load())
        self.assertEqual(loaded.processed_addresses, set())
        self.assertEqual(loaded.cycle_number, 0)


if __name__ == "__main__":
    unittest.main()
