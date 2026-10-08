"""Тесты живого мониторинга бота: LiveMonitor, _fmt_status, /status, /monitor.

Покрывают:
  * _fmt_status — богатый статус (успех %, средние, пауза);
  * LiveMonitor.start/stop/running/_loop/tick — периодический статус, алерт
    об остановке, ошибка отправки;
  * _toggle_monitor — включение/выключение (команда и кнопка);
  * /status и /monitor команды, callback status/monitor:toggle.
"""

import asyncio
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from portal.bot_telegram import (
        LiveMonitor,
        _fmt_status,
        _menu_kb,
        _toggle_monitor,
        build_dispatcher,
    )

    HAS_BOT = True
except Exception:  # noqa: BLE001
    HAS_BOT = False

from portal.config import PortalConfig


def _cfg():
    prev = {k: os.environ.get(k) for k in ("PORTAL_SECRET", "FARMER_MASTER_KEY")}
    os.environ["PORTAL_SECRET"] = "s" * 32
    os.environ["FARMER_MASTER_KEY"] = "k" * 64
    cfg = PortalConfig()
    cfg.telegram_allow_ids = [123]
    for k, v in prev.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    return cfg


@unittest.skipUnless(HAS_BOT, "aiogram не установлен")
class TestFmtStatus(unittest.TestCase):
    def test_status_running_with_metrics(self):
        s = _fmt_status(
            {
                "running": True,
                "health_factor": 0.9,
                "wallet_count": 12,
                "pool": {"actions": 80, "errors": 20, "processed": 50, "cycles": 4, "dyn_workers": 6},
                "db": {"cycles": 3},
            }
        )
        self.assertIn("▶ РАБОТАЕТ", s)
        self.assertIn("80.0%", s)  # 80/100
        self.assertIn("20.0", s)  # 80/4 за цикл
        self.assertIn("12", s)
        self.assertIn("0.90", s)

    def test_status_stopped_paused_no_actions(self):
        s = _fmt_status(
            {
                "running": False,
                "pool": {"paused": True, "actions": 0, "errors": 0, "cycles": 0},
                "db": {},
            }
        )
        self.assertIn("⏹ ОСТАНОВЛЕНА ⏸", s)
        self.assertIn("—", s)  # успешность и среднее без данных

    def test_status_escapes_values(self):
        s = _fmt_status(
            {
                "running": True,
                "wallet_count": "<b>x</b>",
                "pool": {"actions": 1, "errors": 0, "cycles": 1},
                "db": {},
            }
        )
        self.assertNotIn("<b>x</b>", s)
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", s)


@unittest.skipUnless(HAS_BOT, "aiogram не установлен")
class TestLiveMonitor(unittest.IsolatedAsyncioTestCase):
    def _monitor(self, stats_side_effect=None):
        daemon = mock.Mock()
        if stats_side_effect is None:
            stats_side_effect = [{"running": True, "pool": {"actions": 1, "errors": 0, "cycles": 1}, "db": {}}]
        daemon.statistics = mock.AsyncMock(side_effect=stats_side_effect)
        bot = mock.Mock()
        bot.send_message = mock.AsyncMock()
        mon = LiveMonitor(bot, 123, daemon, interval_s=5.0)
        return mon, bot, daemon

    async def test_start_stop(self):
        mon, bot, daemon = self._monitor()
        self.assertFalse(mon.running)
        await mon.start()
        self.assertTrue(mon.running)
        await mon.stop()
        self.assertFalse(mon.running)
        # повторный stop безопасен
        await mon.stop()

    async def test_start_when_already_running(self):
        mon, bot, daemon = self._monitor()
        await mon.start()
        await mon.start()
        self.assertTrue(mon.running)
        await mon.stop()

    async def test_tick_sends_status_on_first(self):
        mon, bot, daemon = self._monitor()
        text = await mon.tick()
        self.assertIn("живой статус", text)
        bot.send_message.assert_awaited_once()
        self.assertEqual(bot.send_message.await_args.kwargs["chat_id"], 123)

    async def test_tick_alerts_on_stop(self):
        mon, bot, daemon = self._monitor(
            stats_side_effect=[
                {"running": True, "pool": {"actions": 1, "errors": 0, "cycles": 1}, "db": {}},
                {"running": False, "pool": {"actions": 1, "errors": 0, "cycles": 1}, "db": {}},
            ]
        )
        await mon.tick()  # первый: статус
        text = await mon.tick()  # второй: алерт об остановке
        self.assertIn("Ферма остановилась", text)

    async def test_send_failure_swallowed(self):
        mon, bot, daemon = self._monitor()
        bot.send_message = mock.AsyncMock(side_effect=RuntimeError("tg down"))
        text = await mon.tick()
        self.assertIn("живой статус", text)

    async def test_loop_cancelled_by_stop(self):
        mon, bot, daemon = self._monitor()
        await mon.start()
        await mon.stop()  # set _stop → _loop выходит

    async def test_running_false_after_stop(self):
        mon, bot, daemon = self._monitor()
        self.assertFalse(mon.running)
        await mon.stop()  # no-op без задачи

    async def test_stop_swallows_cancelled_error(self):
        mon, bot, daemon = self._monitor()

        async def _boom():
            raise asyncio.CancelledError

        mon._task = asyncio.create_task(_boom())
        await asyncio.sleep(0)
        await mon.stop()
        self.assertFalse(mon.running)

    async def test_loop_timeout_continues(self):
        """Timeout в wait_for (_stop.wait()) — нормальное поведение: цикл продолжается."""
        mon, bot, daemon = self._monitor()
        mon.interval_s = 0.02  # быстрое срабатывание таймаута
        mon.tick = mock.AsyncMock(return_value="ok")
        await mon.start()
        await asyncio.sleep(0.1)  # несколько итераций с TimeoutError
        self.assertTrue(mon.running)
        self.assertGreaterEqual(mon.tick.await_count, 2)
        await mon.stop()
        self.assertFalse(mon.running)


@unittest.skipUnless(HAS_BOT, "aiogram не установлен")
class TestToggleMonitor(unittest.IsolatedAsyncioTestCase):
    async def test_toggle_starts_and_stops(self):
        mon, bot, daemon = self._monitor_and_toggle()
        answers = []

        async def answer(text):
            answers.append(text)

        _toggle_monitor(bot, 123, daemon, mon, answer)
        await asyncio.sleep(0.05)
        self.assertTrue(mon.running)
        self.assertIn("включён", answers[0])

        _toggle_monitor(bot, 123, daemon, mon, answer)
        await asyncio.sleep(0.05)
        self.assertFalse(mon.running)
        self.assertIn("выключен", answers[-1])

    async def test_toggle_lazy_initializes(self):
        mon = LiveMonitor.__new__(LiveMonitor)
        mon.bot = None
        mon.tg_id = 0
        mon.daemon = None
        mon.interval_s = 5.0
        mon._task = None
        mon._stop = asyncio.Event()
        mon._was_running = None
        bot = mock.Mock()
        daemon = mock.Mock()
        answers = []

        async def answer(text):
            answers.append(text)

        _toggle_monitor(bot, 999, daemon, mon, answer)
        await asyncio.sleep(0.05)
        self.assertEqual(mon.bot, bot)
        self.assertEqual(mon.tg_id, 999)
        await mon.stop()

    def _monitor_and_toggle(self):
        daemon = mock.Mock()
        daemon.statistics = mock.AsyncMock(
            return_value={"running": True, "pool": {"actions": 1, "errors": 0, "cycles": 1}, "db": {}}
        )
        bot = mock.Mock()
        bot.send_message = mock.AsyncMock()
        mon = LiveMonitor(bot, 123, daemon, interval_s=5.0)
        return mon, bot, daemon


@unittest.skipUnless(HAS_BOT, "aiogram не установлен")
class TestBotCommandsMonitor(unittest.IsolatedAsyncioTestCase):
    def _dispatch(self):
        cfg = _cfg()
        daemon = mock.Mock()
        daemon.statistics = mock.AsyncMock(
            return_value={"running": True, "pool": {"actions": 1, "errors": 0, "cycles": 1}, "db": {}}
        )
        dp = build_dispatcher(cfg, daemon)
        return dp, daemon, cfg

    def _msg(self, text):
        msg = mock.Mock()
        msg.from_user.id = 123
        msg.text = text
        msg.answer = mock.AsyncMock()
        msg.bot = mock.Mock()
        msg.bot.send_message = mock.AsyncMock()
        return msg

    async def test_status_command(self):
        dp, daemon, cfg = self._dispatch()
        msg = self._msg("/status")
        await dp.message.handlers[1].callback(msg)
        msg.answer.assert_awaited_once()
        self.assertIn("живой статус", msg.answer.await_args.args[0])

    async def test_monitor_command_toggles(self):
        dp, daemon, cfg = self._dispatch()
        mon = dp.monitor
        msg = self._msg("/monitor")
        await dp.message.handlers[1].callback(msg)
        await asyncio.sleep(0.05)
        self.assertTrue(mon.running)
        await mon.stop()

    async def test_callback_status(self):
        dp, daemon, cfg = self._dispatch()
        call = mock.Mock()
        call.from_user.id = 123
        call.data = "status"
        call.message = mock.Mock()
        call.message.edit_text = mock.AsyncMock()
        call.message.answer = mock.AsyncMock()
        call.message.bot = mock.Mock()
        call.answer = mock.AsyncMock()
        await dp.callback_query.handlers[0].callback(call)
        call.message.edit_text.assert_awaited_once()
        self.assertIn("живой статус", call.message.edit_text.await_args.args[0])

    async def test_callback_monitor_toggle(self):
        dp, daemon, cfg = self._dispatch()
        mon = dp.monitor
        call = mock.Mock()
        call.from_user.id = 123
        call.data = "monitor:toggle"
        call.message = mock.Mock()
        call.message.answer = mock.AsyncMock()
        call.message.bot = mock.Mock()
        call.message.bot.send_message = mock.AsyncMock()
        call.answer = mock.AsyncMock()
        await dp.callback_query.handlers[0].callback(call)
        await asyncio.sleep(0.05)
        self.assertTrue(mon.running)
        await mon.stop()


@unittest.skipUnless(HAS_BOT, "aiogram не установлен")
class TestMenuMonitorFlag(unittest.TestCase):
    def test_menu_shows_monitor_state(self):
        cfg = _cfg()
        off = _menu_kb(cfg, monitor_on=False)
        self.assertIn("Мониторинг: ВЫКЛ", off.inline_keyboard[-1][0].text)
        on = _menu_kb(cfg, monitor_on=True)
        self.assertIn("Мониторинг: ВКЛ", on.inline_keyboard[-1][0].text)


if __name__ == "__main__":
    unittest.main()
