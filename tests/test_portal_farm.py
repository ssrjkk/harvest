"""Тесты демона фермы портала: SingleInstance-лок на время фарма.

Проверяем, что:
  * start() НЕ запускает фарм, если другой процесс уже держит lock БД;
  * start() берёт lock, а после завершения фарма автоматически его отдаёт;
  * прокси к БД/планировщику/мониторам (группы, расписание, балансы, метрики)
    работают на настоящей SQLite-БД и не падают при сбоях RPC.
"""

import asyncio
import inspect
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.database import Database
from core.scheduler import Scheduler
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


class TestCycleWatchAlerts(unittest.IsolatedAsyncioTestCase):
    """Сторож циклов и pre-flight крана: пуши из реальных строк БД."""

    async def test_new_cycle_alerts_once_with_real_totals(self):
        seen = []

        async def alert(text: str) -> None:
            seen.append(text)

        daemon = FarmDaemon("config.yaml", ":memory:", alert=alert)

        class _Db:
            def __init__(self) -> None:
                self.calls = 0

            async def get_cycle_history(self, limit: int = 20):  # noqa: ARG002
                self.calls += 1
                if self.calls == 1:
                    return [{"id": 6, "wallets": 200, "wallets_ok": 200,
                             "actions_ok": 1000, "errors": 1}]
                return [{"id": 7, "wallets": 200, "wallets_ok": 200,
                         "actions_ok": 1420, "errors": 12}]

        daemon.db = _Db()  # type: ignore[assignment]
        task = asyncio.create_task(daemon._cycle_watch_loop(interval_s=0.01))
        await asyncio.sleep(0.08)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        hits = [s for s in seen if "Цикл #7" in s]
        self.assertEqual(len(hits), 1)
        self.assertIn("1 420", hits[0])
        self.assertIn("12", hits[0])

    async def test_faucet_dead_alerts_owner(self):
        seen = []

        async def alert(text: str) -> None:
            seen.append(text)

        daemon = FarmDaemon("config.yaml", ":memory:", alert=alert)
        daemon.config = {"faucet": {"enabled": True, "strategies": []}}
        live = await daemon._check_faucet_once()
        self.assertEqual(live, 0)
        self.assertTrue(any("Кран недоступен" in s for s in seen))

    async def test_no_db_no_crash(self):
        daemon = FarmDaemon("config.yaml", ":memory:", alert=None)
        daemon.db = None  # type: ignore[assignment]
        task = asyncio.create_task(daemon._cycle_watch_loop(interval_s=0.01))
        await asyncio.sleep(0.04)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        self.assertEqual(await daemon._check_faucet_once(), 0)


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


class _FakeNetwork:
    """Сеть без RPC: diagnostics/конкурентность/балансы из аргументов."""

    def __init__(self, diag=None, health: float = 1.0, balances=None, balance_error: bool = False):
        self._diag = diag or {"calls": 1}
        self._health = health
        self.balances = balances or {}
        self.balance_error = balance_error
        self.balance_calls: list[str] = []

    def diagnostics(self) -> dict:
        return self._diag

    def concurrency_factor(self) -> float:
        return self._health

    async def get_balance(self, address: str) -> float:
        self.balance_calls.append(address)
        if self.balance_error:
            raise RuntimeError("rpc down")
        return self.balances.get(address, 0.0)


class _ProxyPool:
    """Пул для прокси-методов демона: живая статистика по расписанию."""

    def __init__(self, network, stats: dict | None = None, fail_first: bool = False):
        self.network = network
        self._stats = {"actions": 0, "errors": 0, "processed": 0, "cycles": 0, "dyn_workers": 0.0}
        if stats:
            self._stats.update(stats)
        self._fail_first = fail_first
        self.calls = 0
        self.closed = False

    def live_stats(self) -> dict:
        self.calls += 1
        if self._fail_first:
            self._fail_first = False
            raise RuntimeError("stats exploded")
        return dict(self._stats)

    async def close(self) -> None:
        self.closed = True


class _FakeBalanceDb:
    """БД для монитора балансов: список кошельков или сбой чтения."""

    def __init__(self, wallets, read_error: bool = False):
        self._wallets = wallets
        self._read_error = read_error
        self.reads = 0

    async def get_all_wallets(self) -> list:
        self.reads += 1
        if self._read_error:
            raise RuntimeError("db locked")
        return list(self._wallets)


async def _wait_for(check, timeout: float = 3.0) -> bool:
    """Ждёт, пока check() (обычный или корутина) не вернет истинно."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        res = check()
        if inspect.isawaitable(res):
            res = await res
        if res:
            return True
        await asyncio.sleep(0.02)
    return False


class FarmProxyTestBase(unittest.IsolatedAsyncioTestCase):
    """Демон с настоящей SQLite-БД; планировщик — с временным schedule.json.

    SCHEDULE_FILE по умолчанию «schedule.json» пишется в текущую папку,
    поэтому portal.farm.Scheduler подменяется конструктором с tmp-путём:
    тесты не оставляют файл в репозитории.
    """

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = str(Path(self._tmp.name) / "farm.sqlite")
        self.schedule_path = str(Path(self._tmp.name) / "schedule.json")
        self.db = Database(self.db_path, master_key=b"\x11" * 32)
        await self.db.init()
        self.addAsyncCleanup(self.db.close)
        self.daemon = FarmDaemon("config.yaml", self.db_path)
        self.daemon.db = self.db
        patcher = mock.patch("portal.farm.Scheduler", lambda *a, **k: Scheduler(self.schedule_path))
        patcher.start()
        self.addCleanup(patcher.stop)

    async def asyncTearDown(self) -> None:
        sched = self.daemon.scheduler
        if sched is not None:
            await sched.stop()


class TestRpcHealthProxy(FarmProxyTestBase):
    async def test_error_without_pool(self):
        self.assertEqual(await self.daemon.rpc_health(), {"error": "pool not initialized"})

    async def test_returns_pool_diagnostics(self):
        diag = {"calls": 7, "avg_ms": 12.5, "cb_state": "CLOSED"}
        self.daemon.pool = _ProxyPool(_FakeNetwork(diag=diag))
        self.assertEqual(await self.daemon.rpc_health(), diag)


class TestEmptyDbProxies(FarmProxyTestBase):
    """БД ещё не подключена — прокси дают пустые значения, а не 500."""

    async def test_reads_return_empty_without_db(self):
        self.daemon.db = None
        self.assertEqual(await self.daemon.top_wallets(5), [])
        self.assertEqual(await self.daemon.cycle_history(5), [])
        self.assertEqual(await self.daemon.wallet_count(), 0)
        self.assertEqual(await self.daemon.all_wallets(), [])
        self.assertEqual(await self.daemon.metrics_history(5), [])
        self.assertEqual(await self.daemon.list_groups(), [])
        self.assertFalse(await self.daemon.delete_group(1))
        self.assertFalse(await self.daemon.add_wallet_to_group("0xabc", 1))
        self.assertFalse(await self.daemon.remove_wallet_from_group("0xabc", 1))

    async def test_create_group_without_db_raises(self):
        self.daemon.db = None
        with self.assertRaises(RuntimeError):
            await self.daemon.create_group("main")

    async def test_statistics_without_pool_is_just_running_flag(self):
        self.assertEqual(await self.daemon.statistics(), {"running": False})


class TestGroupProxies(FarmProxyTestBase):
    """Группы кошельков через демона на реальной БД портала."""

    async def test_group_crud(self):
        created = await self.daemon.create_group("whales", "#ff0000")
        self.assertEqual(created["name"], "whales")
        self.assertEqual(created["color"], "#ff0000")
        self.assertEqual(created["count"], 0)

        addr = "0x" + "ab" * 20
        self.assertTrue(await self.daemon.add_wallet_to_group(addr, created["id"]))
        # INSERT OR IGNORE: повторное добавление не ошибка.
        self.assertTrue(await self.daemon.add_wallet_to_group(addr, created["id"]))
        groups = await self.daemon.list_groups()
        self.assertEqual([g["name"] for g in groups], ["whales"])
        self.assertEqual(groups[0]["count"], 1)

        self.assertTrue(await self.daemon.remove_wallet_from_group(addr, created["id"]))
        self.assertEqual((await self.daemon.list_groups())[0]["count"], 0)
        self.assertTrue(await self.daemon.delete_group(created["id"]))
        self.assertEqual(await self.daemon.list_groups(), [])

    async def test_duplicate_name_raises_value_error(self):
        await self.daemon.create_group("dup")
        with self.assertRaises(ValueError):
            await self.daemon.create_group("dup")


class TestSchedulerProxies(FarmProxyTestBase):
    async def test_state_before_init(self):
        self.assertEqual(
            self.daemon.scheduler_state(), {"initialized": False, "running": False, "tasks": []}
        )

    async def test_update_and_remove_before_init(self):
        self.assertIsNone(await self.daemon.update_scheduler_task("ghost", cycles=1))
        self.assertFalse(await self.daemon.remove_scheduler_task("ghost"))

    async def test_init_is_idempotent_and_stops(self):
        await self.daemon.init_scheduler()
        first = self.daemon.scheduler
        await self.daemon.init_scheduler()
        self.assertIs(self.daemon.scheduler, first)
        state = self.daemon.scheduler_state()
        self.assertTrue(state["initialized"])
        self.assertTrue(state["running"])
        self.assertEqual(state["tasks"], [])
        self.assertTrue(await self.daemon.stop_scheduler())
        self.assertFalse(self.daemon.scheduler.is_running)

    async def test_stop_scheduler_before_init(self):
        self.assertFalse(await self.daemon.stop_scheduler())

    async def test_start_scheduler_creates_it(self):
        self.assertTrue(await self.daemon.start_scheduler())
        self.assertTrue(self.daemon.scheduler.is_running)

    async def test_task_crud_persists_to_schedule_file(self):
        task = await self.daemon.add_scheduler_task("nightly", interval_hours=8, cycles=2)
        self.assertEqual(task["task_id"], "nightly")
        self.assertEqual(task["cycles"], 2)
        self.assertIsNotNone(self.daemon.scheduler)  # ленивая инициализация
        self.assertEqual(len(self.daemon.scheduler_state()["tasks"]), 1)

        updated = await self.daemon.update_scheduler_task("nightly", cycles=5)
        self.assertEqual(updated["cycles"], 5)
        self.assertIsNone(await self.daemon.update_scheduler_task("ghost", cycles=1))

        saved = json.loads(Path(self.schedule_path).read_text(encoding="utf-8"))
        self.assertEqual(saved["tasks"][0]["task_id"], "nightly")

        self.assertTrue(await self.daemon.remove_scheduler_task("nightly"))
        self.assertFalse(await self.daemon.remove_scheduler_task("nightly"))

    async def test_scheduled_farm_starts_daemon_when_idle(self):
        with mock.patch.object(FarmDaemon, "start", new_callable=mock.AsyncMock) as start:
            await self.daemon._scheduled_farm(3)
            start.assert_awaited_once()

    async def test_scheduled_farm_skipped_while_running(self):
        self.daemon._task = asyncio.create_task(asyncio.sleep(30))
        self.addCleanup(self.daemon._task.cancel)
        try:
            with mock.patch.object(FarmDaemon, "start", new_callable=mock.AsyncMock) as start:
                await self.daemon._scheduled_farm(3)
                start.assert_not_awaited()
        finally:
            self.daemon._task = None


class TestBalanceAlerts(FarmProxyTestBase):
    async def test_default_state(self):
        self.assertEqual(self.daemon.balance_alerts_state(), {"enabled": False, "threshold": 0.001})

    async def test_negative_threshold_clamped_to_zero(self):
        res = await self.daemon.set_balance_alerts(False, -5)
        self.assertEqual(res, {"enabled": False, "threshold": 0.0})

    async def test_monitor_detects_low_balance_and_stops(self):
        low = "0x" + "aa" * 20
        self.daemon.db = _FakeBalanceDb([{"address": low}, {"address": ""}])
        net = _FakeNetwork(balances={low: 0.0001})
        self.daemon.pool = _ProxyPool(net)
        res = await self.daemon.set_balance_alerts(True, 0.5)
        self.assertEqual(res, {"enabled": True, "threshold": 0.5})
        self.assertTrue(await _wait_for(lambda: low in net.balance_calls))
        # Пустой адрес пропускается, второй прогон кошелька не идёт.
        self.assertEqual(net.balance_calls, [low])
        # Повторное включение не плодит вторую задачу.
        task = self.daemon._balance_alerts_task
        await self.daemon.set_balance_alerts(True, 0.5)
        self.assertIs(self.daemon._balance_alerts_task, task)
        self.assertEqual(
            await self.daemon.set_balance_alerts(False, 0.5), {"enabled": False, "threshold": 0.5}
        )
        self.assertIsNone(self.daemon._balance_alerts_task)
        # Монитор перехватывает CancelledError и выходит через break,
        # поэтому задача завершается штатно (done), а не в статусе cancelled.
        self.assertTrue(task.done())

    async def test_monitor_survives_rpc_error(self):
        addr = "0x" + "bb" * 20
        self.daemon.db = _FakeBalanceDb([{"address": addr}])
        net = _FakeNetwork(balance_error=True)
        self.daemon.pool = _ProxyPool(net)
        await self.daemon.set_balance_alerts(True, 0.001)
        self.assertTrue(await _wait_for(lambda: addr in net.balance_calls))
        self.assertFalse(self.daemon._balance_alerts_task.done())  # сбой RPC не убил цикл
        await self.daemon.set_balance_alerts(False, 0.001)

    async def test_monitor_survives_db_error(self):
        self.daemon.db = _FakeBalanceDb([], read_error=True)
        self.daemon.pool = _ProxyPool(_FakeNetwork())
        with self.assertLogs("portal.farm", level="ERROR") as cm:
            await self.daemon.set_balance_alerts(True, 0.001)
            self.assertTrue(await _wait_for(lambda: self.daemon.db.reads >= 1))
            await self.daemon.set_balance_alerts(False, 0.001)
        self.assertTrue(any("Balance monitor error" in line for line in cm.output))

    async def test_monitor_idle_without_pool(self):
        self.daemon.pool = None
        await self.daemon.set_balance_alerts(True, 0.001)
        self.assertTrue(await _wait_for(lambda: self.daemon._balance_alerts_task is not None))
        self.assertFalse(self.daemon._balance_alerts_task.done())
        await self.daemon.set_balance_alerts(False, 0.001)
        self.assertIsNone(self.daemon._balance_alerts_task)

    async def test_monitor_truncates_over_limit(self):
        addrs = [f"0x{i:040d}" for i in range(101)]
        self.daemon.db = _FakeBalanceDb([{"address": a} for a in addrs])
        net = _FakeNetwork(balances={a: 9.0 for a in addrs})
        self.daemon.pool = _ProxyPool(net)
        with self.assertLogs("portal.farm", level="WARNING") as cm:
            await self.daemon.set_balance_alerts(True, 0.001)
            self.assertTrue(await _wait_for(lambda: len(net.balance_calls) >= 100))
            await self.daemon.set_balance_alerts(False, 0.001)
        self.assertTrue(any("первые 100 из 101" in line for line in cm.output))
        self.assertEqual(len(net.balance_calls), 100)


class TestWalletCells(FarmProxyTestBase):
    async def test_no_db(self):
        self.daemon.db = None
        self.assertEqual(await self.daemon.wallet_cells(), ("", 0))

    async def test_query_failure(self):
        class _BadConn:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        class _BadDb:
            async def _connect(self):
                raise RuntimeError("locked")

        self.daemon.db = _BadDb()
        self.assertEqual(await self.daemon.wallet_cells(), ("", 0))

    async def test_old_schema_falls_back_idle(self):
        class _Cur:
            def __init__(self, row):
                self._row = row

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def fetchone(self):
                return self._row

            def __aiter__(self):
                return self

            async def __anext__(self):
                raise StopAsyncIteration

        class _OldDb:
            async def _connect(self):
                return self

            def execute(self, sql, *a, **k):
                if "COUNT" in sql:
                    return _Cur((4,))
                raise RuntimeError("no such column")

        self.daemon.db = _OldDb()
        cells, total = await self.daemon.wallet_cells()
        self.assertEqual((cells, total), ("iiii", 4))


class TestMetricsRecorder(FarmProxyTestBase):
    async def test_snapshot_written_and_read_back(self):
        net = _FakeNetwork(health=0.75)
        self.daemon.pool = _ProxyPool(
            net, stats={"actions": 3, "errors": 1, "processed": 9, "cycles": 2, "dyn_workers": 4}
        )
        task = asyncio.create_task(self.daemon._metrics_recorder_loop())
        self.addCleanup(task.cancel)

        async def has_row():
            return bool(await self.daemon.metrics_history(10))

        self.assertTrue(await _wait_for(has_row, timeout=5.0))
        rows = await self.daemon.metrics_history(10)
        self.assertEqual(rows[0]["actions"], 3)
        self.assertEqual(rows[0]["errors"], 1)
        self.assertEqual(rows[0]["health_factor"], 0.75)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def test_recorder_logs_error_and_keeps_running(self):
        self.daemon.pool = _ProxyPool(_FakeNetwork(), fail_first=True)
        task = asyncio.create_task(self.daemon._metrics_recorder_loop())
        with self.assertLogs("portal.farm", level="ERROR") as cm:
            self.assertTrue(await _wait_for(lambda: self.daemon.pool.calls >= 1))
            self.assertFalse(task.done())
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(any("Metrics recorder error" in line for line in cm.output))

    async def test_recorder_exits_when_stop_set(self):
        self.daemon._stop.set()
        await asyncio.wait_for(self.daemon._metrics_recorder_loop(), timeout=2.0)

    async def test_close_cancels_recorder_and_releases_resources(self):
        self.daemon._metrics_task = asyncio.create_task(self.daemon._metrics_recorder_loop())
        pool = _ProxyPool(_FakeNetwork())
        self.daemon.pool = pool
        await self.daemon.close()
        self.assertIsNone(self.daemon._metrics_task)
        self.assertIsNone(self.daemon.pool)
        self.assertTrue(pool.closed)
        self.assertIsNone(self.daemon.db)


if __name__ == "__main__":
    unittest.main(verbosity=2)
