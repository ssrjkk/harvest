"""Тесты SingleInstance: блокировка процесса, PID держателя, повторный запуск."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.single_instance import ProcessLockedError, SingleInstance, default_lock_path


class TestSingleInstance(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.lock_path = Path(self._tmp.name) / "farm.lock"

    def test_default_lock_path(self):
        self.assertEqual(default_lock_path("farming_state.db"), "farming_state.db.lock")

    def test_path_property(self):
        self.assertEqual(SingleInstance(self.lock_path).path, self.lock_path)

    def test_context_manager(self):
        with SingleInstance(self.lock_path) as guard:
            self.assertTrue(guard.acquire())
        # после выхода лок снят — второй может занять
        other = SingleInstance(self.lock_path)
        self.assertTrue(other.acquire())
        other.release()

    def test_context_manager_locked_raises(self):
        first = SingleInstance(self.lock_path)
        self.assertTrue(first.acquire())
        try:
            with self.assertRaises(ProcessLockedError) as cm:
                with SingleInstance(self.lock_path):
                    pass
            self.assertEqual(cm.exception.lock_path, self.lock_path)
            self.assertIn("экземпляр", str(cm.exception))
        finally:
            first.release()

    def test_process_locked_error_without_holder(self):
        err = ProcessLockedError(self.lock_path, None)
        self.assertIn("другой экземпляр", str(err))
        self.assertNotIn("pid=", str(err))

    def test_holder_pid_reads_foreign_file(self):
        self.lock_path.write_text("12345\n", encoding="utf-8")
        guard = SingleInstance(self.lock_path)
        self.assertEqual(guard.holder_pid(), "12345")

    def test_holder_pid_unreadable_returns_none(self):
        guard = SingleInstance(Path(self._tmp.name) / "missing" / "x.lock")
        self.assertIsNone(guard.holder_pid())

    def test_acquire_mkdir_failure_returns_false(self):
        # parent — файл, а не каталог: mkdir/open упадут с OSError
        self.lock_path.write_text("x", encoding="utf-8")
        guard = SingleInstance(self.lock_path / "sub.lock")
        self.assertFalse(guard.acquire())

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
