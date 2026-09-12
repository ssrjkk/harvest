import asyncio
import logging
import unittest

from portal.__main__ import _log_bot_crash


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


if __name__ == "__main__":
    unittest.main()
