"""Тесты BatchWriter: батчинг, устойчивость к сбоям БД (drop против бесконечного роста)."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.batchwriter import BatchWriter


class FakeDB:
    def __init__(self, fail: bool = False):
        self._fail = fail
        self.fail_health = False
        self.actions_written: list = []
        self.health_written: dict = {}
        self.calls = 0

    def set_fail(self, fail: bool):
        self._fail = fail

    async def log_actions_batch(self, rows):
        self.calls += 1
        if self._fail:
            raise RuntimeError("БД недоступна (тест)")
        self.actions_written.extend(rows)

    async def update_wallet_health_batch(self, updates):
        if self._fail or self.fail_health:
            raise RuntimeError("БД недоступна (тест)")
        self.health_written.update(updates)


class TestBatchWriter(unittest.IsolatedAsyncioTestCase):
    async def test_flush_writes_and_resets(self):
        db = FakeDB()
        bw = BatchWriter(db, flush_every=0.05, max_buffer=100)
        await bw.add_action("0x1", "stake", "0xhash", True)
        await bw.flush()
        self.assertEqual(len(db.actions_written), 1)
        self.assertEqual(db.health_written, {"0x1": (1, 1)})
        # после flush буфер пуст
        await bw.flush()
        self.assertEqual(len(db.actions_written), 1)

    async def test_buffer_capacity_triggers_autoflush(self):
        db = FakeDB()
        bw = BatchWriter(db, flush_every=999, max_buffer=2)
        await bw.add_action("0x1", "a", "h", True)
        self.assertEqual(db.calls, 0)
        await bw.add_action("0x2", "a", "h", True)
        self.assertEqual(db.calls, 1)
        self.assertEqual(len(db.actions_written), 2)

    async def test_persistent_failure_drops_batch_after_3(self):
        db = FakeDB(fail=True)
        bw = BatchWriter(db, flush_every=999, max_buffer=500)
        for _ in range(5):
            await bw.add_action("0x1", "a", "h", True)
        for _ in range(4):
            await bw.flush()
        # сбой 1 и 2 возвращают данные в буфер; на 3-м устойчивый отказ
        # заставляет отбросить батч (db-вызовов было 3, последний flush ранний return)
        self.assertEqual(db.calls, 3)
        async with bw._lock:
            pending = len(bw._actions)
        self.assertEqual(pending, 0)

    async def test_recovery_after_failure(self):
        db = FakeDB(fail=True)
        bw = BatchWriter(db, flush_every=999, max_buffer=500)
        await bw.add_action("0x1", "a", "h", True)
        await bw.flush()  # сбой 1
        db.set_fail(False)
        await bw.flush()  # успех — данные не потеряны
        self.assertEqual(len(db.actions_written), 1)
        self.assertEqual(bw._consecutive_failures, 0)

    async def test_partial_failure_requeues_only_uncommitted(self):
        db = FakeDB()
        db.fail_health = True
        bw = BatchWriter(db, flush_every=999, max_buffer=500)
        await bw.add_action("0x1", "a", "h", True)
        await bw.flush()
        # actions уже закоммичены, упала только запись health:
        # requeue должен вернуть в буфер health, но НЕ actions (иначе дубли в логе).
        self.assertEqual(len(db.actions_written), 1)
        db.fail_health = False
        await bw.flush()
        self.assertEqual(len(db.actions_written), 1)
        self.assertEqual(db.health_written, {"0x1": (1, 1)})
        self.assertEqual(bw._consecutive_failures, 0)

    async def test_double_stop_idempotent(self):
        db = FakeDB()
        bw = BatchWriter(db)
        bw.start()
        await bw.add_action("0x1", "a", "h", True)
        await bw.stop()
        await bw.stop()
        self.assertEqual(len(db.actions_written), 1)

    async def test_cancel_during_flush_drops_inflight_avoids_duplicates(self):
        """Отмена (stop) во время записи НЕ возвращает батч в буфер.

        commit атомарен, и результат мог вернуться уже после отмены: requeue
        заставил бы финальный flush в stop() записать те же строки повторно —
        задублировались бы actions_log и инкременты health. Честнее потерять
        одну телеметрическую пачку при остановке, чем молча задублировать статистику.
        """
        import asyncio

        class CancelOnceDB(FakeDB):
            def __init__(self):
                super().__init__()
                self.cancel_next = False

            async def log_actions_batch(self, rows):
                if self.cancel_next:
                    self.cancel_next = False
                    raise asyncio.CancelledError
                return await super().log_actions_batch(rows)

        db = CancelOnceDB()
        bw = BatchWriter(db, flush_every=999, max_buffer=500)
        await bw.add_action("0x1", "a", "h", True)
        db.cancel_next = True
        with self.assertRaises(asyncio.CancelledError):
            await bw.flush()
        # In-flight пачка НЕ вернулась в буфер: stop() не перепишет её повторно.
        async with bw._lock:
            pending = len(bw._actions)
        self.assertEqual(pending, 0)
        # В БД тоже не попало (отмена прилетела до записи).
        self.assertEqual(len(db.actions_written), 0)
        # Данные после отмены пишутся штатно, дублей нет.
        db.cancel_next = False
        await bw.add_action("0x2", "b", "h", True)
        await bw.stop()
        self.assertEqual(len(db.actions_written), 1)
        self.assertEqual(db.health_written, {"0x2": (1, 1)})

    async def test_stop_flushes_despite_cooldown(self):
        """stop() сбрасывает кулдаун: остаток буфера уходит на диск, а не молча."""
        db = FakeDB(fail=True)
        bw = BatchWriter(db, flush_every=999, max_buffer=500)
        await bw.add_action("0x1", "a", "h", True)
        bw._cooldown_until = 999999.0
        db.set_fail(False)
        await bw.stop()
        self.assertEqual(len(db.actions_written), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
