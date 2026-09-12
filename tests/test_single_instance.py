"""Тесты SingleInstance: блокировка процесса, PID держателя, повторный запуск."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.single_instance import SingleInstance, default_lock_path


class TestSingleInstance(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.lock_path = Path(self._tmp.name) / "farm.lock"

    def test_default_lock_path(self):
        self.assertEqual(default_lock_path("farming_state.db"), "farming_state.db.lock")

    def test_acquire_release_reacquire(self):
        guard = SingleInstance(self.lock_path)
        self.assertTrue(guard.acquire())
        guard.release()
        guard2 = SingleInstance(self.lock_path)
        self.assertTrue(guard2.acquire())
        guard2.release()

    def test_second_acquire_blocked_until_release(self):
        first = SingleInstance(self.lock_path)
        self.assertTrue(first.acquire())
        second = SingleInstance(self.lock_path)
        self.assertFalse(second.acquire())
        first.release()
        self.assertTrue(second.acquire())
        second.release()

    def test_holder_pid_written(self):
        guard = SingleInstance(self.lock_path)
        self.assertTrue(guard.acquire())
        self.assertEqual(guard.holder_pid(), str(_my_pid()))
        guard.release()

    def test_double_acquire_same_object(self):
        guard = SingleInstance(self.lock_path)
        self.assertTrue(guard.acquire())
        self.assertTrue(guard.acquire())  # идемпотентно для одного объекта
        guard.release()  # повторный release не должен падать
        guard2 = SingleInstance(self.lock_path)
        self.assertTrue(guard2.acquire())
        guard2.release()

    def test_release_without_acquire_is_noop(self):
        guard = SingleInstance(self.lock_path)
        guard.release()  # не должно падать


def _my_pid() -> int:
    import os

    return os.getpid()


if __name__ == "__main__":
    unittest.main(verbosity=2)
