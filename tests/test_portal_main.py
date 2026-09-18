import asyncio
import logging
import types
import unittest
from unittest import mock

from portal.__main__ import _acute_alert, _log_bot_crash


class _SpyHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


class TestBotCrashHandling(unittest.IsolatedAsyncioTestCase):
    """Сбой Telegram-бота должен логироваться крупно, а не умирать молча.

    Тихий выход polling оставляет владельца без пульта при живом сервере:
    это первый симптом отказа, который режут из-за отсутствия callback'а.
    """

    async def test_crashed_bot_task_logs_error(self):
        async def _boom() -> None:
            raise RuntimeError("polling died")

        task = asyncio.create_task(_boom())
        await asyncio.sleep(0.01)
        self.assertTrue(task.done())
        with self.assertLogs("portal", level="ERROR") as cm:
            _log_bot_crash(task)
        self.assertTrue(any("Telegram-бот" in line for line in cm.output))

    async def test_cancelled_bot_task_quiet(self):
        async def _slow() -> None:
            await asyncio.sleep(60)

        task = asyncio.create_task(_slow())
        task.cancel()
        await asyncio.sleep(0.01)
        self.assertTrue(task.cancelled())
        # Отменённая задача не должна выдавать ERROR про падение бота.
        spy = _SpyHandler()
        logger = logging.getLogger("portal")
        logger.addHandler(spy)
        try:
            _log_bot_crash(task)
        finally:
            logger.removeHandler(spy)
        self.assertFalse(any("Telegram-бот" in r.getMessage() for r in spy.records))

    async def test_pending_bot_task_ignored(self):
        async def _slow() -> None:
            await asyncio.sleep(60)

        task = asyncio.create_task(_slow())
        _log_bot_crash(task)  # ещё не завершена — ничего не делаем
        task.cancel()
        await asyncio.sleep(0.01)

    async def test_crashed_bot_task_alerts_owner(self):
        async def _boom() -> None:
            raise RuntimeError("polling died")

        task = asyncio.create_task(_boom())
        await asyncio.sleep(0.01)
        self.assertTrue(task.done())
        cfg = types.SimpleNamespace(telegram_token="tk-1")
        calls = []
        _orig_create = asyncio.create_task

        async def _dummy(_cfg, _text) -> None:
            return None

        def _spy_create(coro, *args, **kwargs):
            calls.append(coro)
            coro.close()
            return _orig_create(asyncio.sleep(0), *args, **kwargs)

        with mock.patch("asyncio.create_task", new=_spy_create), mock.patch(
            "portal.bot_telegram.notify_owner", new=_dummy
        ):
            _log_bot_crash(task, cfg)
        self.assertTrue(calls)

    async def test_crashed_bot_task_alert_setup_failure_logged(self):
        async def _boom() -> None:
            raise RuntimeError("polling died")

        task = asyncio.create_task(_boom())
        await asyncio.sleep(0.01)
        cfg = types.SimpleNamespace(telegram_token="tk-1")

        async def _dummy(_cfg, _text) -> None:
            return None

        def _boom_task(coro, *args, **kwargs):
            coro.close()
            raise RuntimeError("loop down")

        with mock.patch("asyncio.create_task", side_effect=_boom_task), mock.patch(
            "portal.bot_telegram.notify_owner", new=_dummy
        ), self.assertLogs("portal", level="WARNING") as cm:
            _log_bot_crash(task, cfg)
        self.assertTrue(any("алерт" in m for m in cm.output))

    async def test_acute_alert_forwards_to_notify_owner(self):
        cfg = types.SimpleNamespace(farm="cfg")
        sent = []

        async def _fake_notify(_cfg, text) -> None:
            sent.append((_cfg, text))

        with mock.patch("portal.bot_telegram.notify_owner", new=_fake_notify):
            await _acute_alert(cfg, "hello")
        self.assertEqual(sent, [(cfg, "hello")])


if __name__ == "__main__":
    unittest.main()
