"""Тесты сторожевого монитора портала (portal/guard.py).

Проверяем пороговые алерты, дедупликацию по состоянию, восстановление,
heartbeat, переходы running/stopped и устойчивость к сбоям (statistics/notify).
"""

import asyncio
import unittest
from unittest import mock

from portal.guard import Watchdog, _heartbeat_text


def _stats(
    running=True,
    health=0.9,
    processed=10,
    errors=0,
    cycles=2,
    actions=20,
    history_cycles=8,
    paused=False,
):
    return {
        "running": running,
        "health_factor": health,
        "pool": {
            "cycles": cycles,
            "actions": actions,
            "errors": errors,
            "processed": processed,
            "paused": paused,
        },
        "db": {"cycles": history_cycles},
    }


class _Daemon:
    def __init__(self, *data):
        self._queue = list(data)

    async def statistics(self):
        if len(self._queue) == 1:
            return self._queue[0]
        return self._queue.pop(0)


class TestHeartbeatText(unittest.TestCase):
    def test_running_healthy(self):
        text = _heartbeat_text(_stats(running=True))
        self.assertIn("▶ работает", text)
        self.assertIn("действия 20", text)

    def test_paused(self):
        text = _heartbeat_text(_stats(running=True, paused=True))
        self.assertIn("⏸", text)

    def test_stopped(self):
        text = _heartbeat_text(_stats(running=False))
        self.assertIn("⏹ остановлена", text)
        self.assertIn("health 0.90", text)


class TestWatchdog(unittest.IsolatedAsyncioTestCase):
    async def test_first_tick_sets_baseline_no_alerts(self):
        wd = Watchdog(_Daemon(_stats()), interval_s=1)
        sent = await wd.tick()
        self.assertEqual(sent, [])
        self.assertEqual(wd._prev["processed"], 10)

    async def test_statistics_unavailable_sends_alert(self):
        daemon = mock.Mock()
        daemon.statistics = mock.AsyncMock(side_effect=RuntimeError("db down"))
        wd = Watchdog(daemon)
        with self.assertLogs("portal.guard", level="WARNING") as cm:
            sent = await wd.tick()
        self.assertTrue(any("недоступна" in m for m in cm.output))
        self.assertEqual(len(sent), 1)
        self.assertIn("недоступна", sent[0])

    async def test_health_degrades_and_recovers(self):
        wd = Watchdog(
            _Daemon(
                _stats(health=0.9),
                _stats(health=0.4, processed=11),
                _stats(health=0.4, processed=12),
                _stats(health=0.9, processed=13),
            )
        )
        self.assertEqual(await wd.tick(), [])
        # 1-й плохой тик — ниже порога, но ещё не min_consecutive.
        self.assertEqual(await wd.tick(), [])
        # 2-й подряд — алерт.
        sent = await wd.tick()
        self.assertEqual(len(sent), 1)
        self.assertIn("Сеть деградировала", sent[0])
        # восстановление — короткое сообщение, без повторного алерта.
        sent = await wd.tick()
        self.assertEqual(len(sent), 1)
        self.assertIn("Сеть восстановилась", sent[0])

    async def test_stall_and_resume(self):
        wd = Watchdog(_Daemon(_stats(processed=10), _stats(processed=10), _stats(processed=10), _stats(processed=15)))
        await wd.tick()
        self.assertEqual(await wd.tick(), [])
        sent = await wd.tick()
        self.assertEqual(len(sent), 1)
        self.assertIn("Ферма стоит", sent[0])
        sent = await wd.tick()
        self.assertEqual(len(sent), 1)
        self.assertIn("Прогресс возобновился", sent[0])

    async def test_pause_is_not_stall(self):
        wd = Watchdog(_Daemon(_stats(processed=10), _stats(processed=10, paused=True), _stats(processed=10, paused=True)))
        await wd.tick()
        self.assertEqual(await wd.tick(), [])
        self.assertEqual(await wd.tick(), [])

    async def test_error_burst_then_quiet(self):
        wd = Watchdog(
            _Daemon(
                _stats(errors=0, processed=10),
                _stats(errors=150, processed=11),
                _stats(errors=150, processed=12),
                _stats(errors=160, processed=13),
            )
        )
        await wd.tick()
        sent = await wd.tick()
        self.assertEqual(len(sent), 1)
        self.assertIn("Всплеск ошибок: +150", sent[0])
        # Прирост ошибок прекратился — "восстановление".
        sent = await wd.tick()
        self.assertEqual(len(sent), 1)
        self.assertIn("Поток ошибок прекратился", sent[0])
        # Медленное накопление (< порога за тик) — тишина.
        sent = await wd.tick()
        self.assertEqual(sent, [])

    async def test_stopped_transition_alerts_once(self):
        wd = Watchdog(_Daemon(_stats(running=True), _stats(running=False), _stats(running=False)))
        await wd.tick()
        sent = await wd.tick()
        self.assertEqual(len(sent), 1)
        self.assertIn("Ферма остановилась", sent[0])
        # Уже остановлена — новых уведомлений нет.
        self.assertEqual(await wd.tick(), [])

    async def test_restart_sets_fresh_baseline(self):
        wd = Watchdog(_Daemon(_stats(running=True, processed=99), _stats(running=False), _stats(running=True, processed=0)))
        await wd.tick()
        await wd.tick()
        # Рестарт: тик только фиксирует базлайн, без шума "стоит".
        sent = await wd.tick()
        self.assertEqual(sent, [])
        self.assertEqual(wd._prev["processed"], 0)

    async def test_notify_receives_each_message(self):
        got = []

        async def _notify(text):
            got.append(text)

        wd = Watchdog(
            _Daemon(
                _stats(health=0.4, processed=10),
                _stats(health=0.4, processed=11),
                _stats(health=0.4, processed=12),
            ),
            notify=_notify,
        )
        await wd.tick()
        await wd.tick()
        await wd.tick()
        self.assertEqual(len(got), 1)
        self.assertIn("Сеть деградировала", got[0])

    async def test_notify_failure_is_swallowed(self):
        async def _bad(text):
            raise RuntimeError("telegraph down")

        wd = Watchdog(
            _Daemon(
                _stats(health=0.4, processed=10),
                _stats(health=0.4, processed=11),
                _stats(health=0.4, processed=12),
            ),
            notify=_bad,
        )
        with self.assertLogs("portal.guard", level="WARNING"):
            await wd.tick()
            await wd.tick()
            sent = await wd.tick()
        self.assertEqual(len(sent), 1)

    async def test_heartbeat_fires_after_interval(self):
        daemon = _Daemon(
            _stats(),
            _stats(processed=11),
        )
        wd = Watchdog(daemon, heartbeat_hours=1)
        with mock.patch("portal.guard.time.monotonic", side_effect=[100.0, 100.0 + 3601.0]):
            await wd.tick()
            sent = await wd.tick()
        self.assertEqual(len(sent), 1)
        self.assertIn("❤️ Пульс", sent[0])

    async def test_heartbeat_kept_silent_within_interval(self):
        wd = Watchdog(_Daemon(_stats(), _stats()), heartbeat_hours=24)
        with mock.patch("portal.guard.time.monotonic", side_effect=[100.0, 100.0 + 60.0]):
            await wd.tick()
            sent = await wd.tick()
        self.assertEqual(sent, [])

    async def test_run_loop_sleeps_and_tolerates_tick_failure(self):
        wd = Watchdog(mock.Mock(), interval_s=1)
        real_sleep = asyncio.sleep

        async def _raise_tick():
            raise RuntimeError("boom")

        async def _yield_sleep(*args, **kwargs):
            # уступаем циклу событий, но не ждём реального времени
            await real_sleep(0)

        wd.tick = _raise_tick
        with mock.patch("asyncio.sleep", new=_yield_sleep), self.assertLogs("portal.guard", level="WARNING"):
            task = asyncio.create_task(wd.run())
            await real_sleep(0)  # дать циклу выполнить первую итерацию
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task


if __name__ == "__main__":
    unittest.main()
