"""Тесты демона фермы портала: SingleInstance-лок на время фарма.

Проверяем, что:
  * start() НЕ запускает фарм, если другой процесс уже держит lock БД;
  * start() берёт lock, а после завершения фарма автоматически его отдаёт.
"""

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.single_instance import SingleInstance, default_lock_path
from portal.farm import FarmDaemon, _db_master_key


class _FakePool:
    """Минимальная заглушка пула: run_forever мгновенно завершается."""

    _closed = False
    paused = False

    async def run_forever(self, stop_event) -> None:
        return

    async def close(self) -> None:
        self._closed = True


class TestFarmDaemonSingleInstance(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = str(Path(self._tmp.name) / "farm.sqlite")

    def test_start_blocked_when_another_instance_holds_lock(self):
        lock = SingleInstance(default_lock_path(self.db_path))
        self.assertTrue(lock.acquire())
        daemon = FarmDaemon("config.yaml", self.db_path)
        daemon.pool = _FakePool()  # type: ignore[assignment]
        try:
            ok = asyncio.run(daemon.start())
            self.assertFalse(ok)
            self.assertFalse(daemon.running)
            self.assertIsNone(daemon._guard)
            self.assertIsNone(daemon._task)
        finally:
            lock.release()

    def test_start_takes_lock_and_auto_releases_on_completion(self):
        async def scenario():
            daemon = FarmDaemon("config.yaml", self.db_path)
            daemon.pool = _FakePool()  # type: ignore[assignment]
            ok = await daemon.start()
            assert ok
            # Пока фарм жив — lock занят, чужой экземпляр не пробьётся.
            foreign = SingleInstance(default_lock_path(self.db_path))
            blocked = not foreign.acquire()
            foreign.release()
            # Фарм мгновенно завершился — дожидаемся полной очистки.
            if daemon._task is not None and not daemon._task.done():
                await daemon._task
            return daemon, blocked

        daemon, blocked = asyncio.run(scenario())
        self.assertTrue(blocked)
        self.assertIsNone(daemon._guard)
        self.assertFalse(daemon.running)
        # После завершения фарма lock свободен.
        reacquire = SingleInstance(default_lock_path(self.db_path))
        self.assertTrue(reacquire.acquire())
        reacquire.release()


class _RaisingPool:
    """Пул, чей цикл падает — для проверки алерта владельцу при краше."""

    _closed = False
    paused = False

    async def run_forever(self, stop_event) -> None:
        raise RuntimeError("rpc exploded")

    async def close(self) -> None:
        self._closed = True


class TestFarmDaemonAlerts(unittest.TestCase):
    """Алерты владельцу: краш фермы и отказ запуска из-за чужого SingleInstance-лока."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = str(Path(self._tmp.name) / "farm.sqlite")

    @staticmethod
    def _run_until_done(daemon):
        async def scenario():
            ok = await daemon.start()
            assert ok
            if daemon._task is not None and not daemon._task.done():
                await daemon._task

        asyncio.run(scenario())

    def test_crash_alerts_owner_and_closes_pool(self):
        seen = []

        async def alert(text: str) -> None:
            seen.append(text)

        daemon = FarmDaemon("config.yaml", self.db_path, alert=alert)
        daemon.pool = _RaisingPool()  # type: ignore[assignment]
        pool = daemon.pool
        self._run_until_done(daemon)
        self.assertTrue(any("Ферма упала" in s and "rpc exploded" in s for s in seen))
        self.assertTrue(pool._closed)
        self.assertIsNone(daemon.pool)

    def test_alert_callback_exception_swallowed(self):
        async def bad_alert(text: str) -> None:
            raise RuntimeError("telegraph down")

        daemon = FarmDaemon("config.yaml", self.db_path, alert=bad_alert)
        daemon.pool = _RaisingPool()  # type: ignore[assignment]
        pool = daemon.pool
        # Сбой алерта не должен просочиться наружу — демон дочищает пул и выходит.
        self._run_until_done(daemon)
        self.assertTrue(pool._closed)

    def test_start_blocked_alerts_owner(self):
        lock = SingleInstance(default_lock_path(self.db_path))
        self.assertTrue(lock.acquire())
        seen = []

        async def alert(text: str) -> None:
            seen.append(text)

        daemon = FarmDaemon("config.yaml", self.db_path, alert=alert)
        daemon.pool = _FakePool()  # type: ignore[assignment]
        try:
            ok = asyncio.run(daemon.start())
            self.assertFalse(ok)
        finally:
            lock.release()
        self.assertTrue(any("другой экземпляр" in s for s in seen))


class TestDbMasterKey(unittest.TestCase):
    """Ключ БД демона фермы: PIN-пароль портала не должен ломать расшифровку.

    override (cfg.master_key) может быть коротким PIN веб-входа или hex-строкой
    неверной длины — в этих случаях ключ БД берётся из master.key файла,
    а env (с тем же PIN) игнорируется.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        path = Path(self._tmp.name) / "master.key"
        path.write_bytes(bytes.fromhex("ab" * 32))
        self.cfg = {"database": {"master_key": str(path)}}

    def tearDown(self):
        os.environ.pop("FARMER_MASTER_KEY", None)

    def test_valid_64hex_override_used(self):
        self.assertEqual(_db_master_key("cd" * 32, self.cfg), bytes.fromhex("cd" * 32))

    def test_pin_override_falls_back_to_file(self):
        # Не-hex или hex меньшей длины, чем 32 байта — это PIN веб-входа,
        # ключом БД быть не может: берём master.key из файла.
        self.assertEqual(_db_master_key("mypassword123", self.cfg), bytes.fromhex("ab" * 32))
        self.assertEqual(_db_master_key("a1b2c3d4e5f60708", self.cfg), bytes.fromhex("ab" * 32))

    def test_pin_override_ignores_bad_env(self):
        # Реальный сценарий портала: в env лежит тот же PIN — resolve_master_key
        # по env упал бы, но helper читает master.key файл и env не трогает.
        os.environ["FARMER_MASTER_KEY"] = "mypassword123"
        self.assertEqual(_db_master_key("mypassword123", self.cfg), bytes.fromhex("ab" * 32))

    def test_no_override_uses_env_then_file(self):
        os.environ["FARMER_MASTER_KEY"] = "cd" * 32
        self.assertEqual(_db_master_key(None, self.cfg), bytes.fromhex("cd" * 32))
        os.environ.pop("FARMER_MASTER_KEY", None)
        self.assertEqual(_db_master_key(None, self.cfg), bytes.fromhex("ab" * 32))

    def test_different_valid_override_wins(self):
        # 64-hex, но не совпадающий с файлом — всё ещё используется как override.
        # (ротация ключа: пользователь сам меняет ключ и seed-данные).
        self.assertEqual(_db_master_key("ef" * 32, self.cfg), bytes.fromhex("ef" * 32))


if __name__ == "__main__":
    unittest.main(verbosity=2)
