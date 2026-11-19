"""Тесты portal.bot_health: живой сигнал, супервизор с backoff, сторож молчания.

Время, паузы и Telegram-рассылка подставные: прогон не ждёт реальных секунд и
не стучится в Telegram. Проверяем по отдельности каждая ветка честного статуса
— живость, молчание, рестарт, отказ токена — и то, что ни один сбой не остаётся
незаметным.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import unittest
from collections.abc import Callable
from unittest import mock

from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError

from portal import bot_health

FAKE_TOKEN = "123456789:AAHfake-token-hash-abcdefghij-klmnop"
TOKEN_URL = f"https://api.telegram.org/bot{FAKE_TOKEN}/getMe"


class _Method:
    """Мини-aiogram: у метода спрашивают только имя `.method`."""

    def __init__(self, name: object) -> None:
        self.method = name


class _Session:
    """Транспорт бота: помнит вызовы, умеет падать по запросу."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.error: Exception | None = None

    async def make_request(self, bot, method, *args, **kwargs):
        self.calls.append((method, args, kwargs))
        if self.error is not None:
            raise self.error
        return {"ok": True, "method": getattr(method, "method", None)}


class _Bot:
    def __init__(self) -> None:
        self.session = _Session()


class _StopLoop(Exception):
    """Маркер выхода из бесконечного цикла сторожа."""


class _Stepper:
    """Драйвер бесконечного цикла: шаги (now, hook), после исчерпания — стоп."""

    def __init__(self, steps: list[tuple[float, Callable[[], None] | None]]) -> None:
        self._steps = steps
        self._i = 0
        self.slept: list[float] = []

    async def sleep(self, delay: float) -> None:
        if self._i >= len(self._steps):
            raise _StopLoop
        self.slept.append(float(delay))

    def monotonic(self) -> float:
        now, hook = self._steps[self._i]
        self._i += 1
        if hook is not None:
            hook()
        return float(now)


class _Notifier:
    """Получатель алертов: копит тексты и по запросу падает на доставке."""

    def __init__(self, error: Exception | None = None) -> None:
        self.sent: list[str] = []
        self.error = error

    async def __call__(self, text: str) -> None:
        self.sent.append(text)
        if self.error is not None:
            raise self.error


async def _drain(coro) -> None:
    """Покрутить бесконечный цикл до стоп-маркера; прочие ошибки — наружу."""
    with contextlib.suppress(_StopLoop):
        await coro


class BotHealthCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        bot_health.reset()
        self.addCleanup(bot_health.reset)

    def _mark_live(self, at: float) -> None:
        bot_health.note_poll(now=at)

    def _st(self, now: float = 2000.0) -> dict:
        return bot_health.status(now)


class TestObservePolling(BotHealthCase):
    async def test_get_updates_counts_as_liveness(self):
        bot = _Bot()
        bot_health.observe_polling(bot)
        result = await bot.session.make_request(bot, _Method("getUpdates"), timeout=30)
        self.assertEqual(result["method"], "getUpdates")
        self.assertEqual(bot_health.status()["polls"], 1)
        self.assertTrue(bot_health.status()["polling_alive"])
        self.assertEqual(bot.session.calls[0][2], {"timeout": 30})  # kwargs доходят до оригинала

    async def test_other_methods_do_not_prove_liveness(self):
        bot = _Bot()
        bot_health.observe_polling(bot)
        await bot.session.make_request(bot, _Method("sendMessage"), chat_id=1)
        await bot.session.make_request(bot, _Method("getMe"))
        st = bot_health.status()
        self.assertEqual(st["polls"], 0)
        self.assertEqual(st["polling_alive"], False)
        self.assertEqual(len(bot.session.calls), 2)

    async def test_method_without_a_name_is_ignored(self):
        bot = _Bot()
        bot_health.observe_polling(bot)
        await bot.session.make_request(bot, object())
        self.assertEqual(bot_health.status()["polls"], 0)

    async def test_original_request_error_propagates(self):
        bot = _Bot()
        bot.session.error = ConnectionError("net down")
        bot_health.observe_polling(bot)
        with self.assertRaises(ConnectionError):
            await bot.session.make_request(bot, _Method("getUpdates"))
        self.assertEqual(bot_health.status()["polls"], 0)

    async def test_restart_installs_a_fresh_watcher(self):
        first, second = _Bot(), _Bot()
        bot_health.observe_polling(first)
        bot_health.observe_polling(second)  # новый Bot после рестарта
        await second.session.make_request(second, _Method("getUpdates"))
        self.assertEqual(bot_health.status()["polls"], 1)
        self.assertEqual(first.session.calls, [])


class TestStateAndStatus(BotHealthCase):
    async def test_fresh_poll_is_alive(self):
        bot_health.note_start(now=1000.0)
        self._mark_live(1900.0)
        st = self._st(2000.0)
        self.assertTrue(st["polling_alive"])
        self.assertEqual(st["last_poll_age_s"], 100.0)
        self.assertEqual(st["started_age_s"], 1000.0)

    async def test_stale_poll_is_deaf(self):
        bot_health.note_start(now=1000.0)
        self._mark_live(1100.0)
        st = self._st(2000.0)
        self.assertFalse(st["polling_alive"])
        self.assertEqual(st["last_poll_age_s"], 900.0)

    async def test_fresh_window_is_tunable(self):
        bot_health.note_start(now=1000.0)
        self._mark_live(1100.0)
        with mock.patch.dict(os.environ, {"PORTAL_BOT_FRESH_S": "1000"}):
            self.assertTrue(bot_health.status(2000.0)["polling_alive"])

    async def test_garbage_fresh_value_falls_back_to_default(self):
        bot_health.note_start(now=1000.0)
        self._mark_live(1900.0)
        with mock.patch.dict(os.environ, {"PORTAL_BOT_FRESH_S": "abc"}):
            with self.assertLogs("portal.bot_health", level="WARNING") as cm:
                st = bot_health.status(2000.0)
        self.assertIn("PORTAL_BOT_FRESH_S", "\n".join(cm.output))
        self.assertTrue(st["polling_alive"])

    async def test_empty_fresh_value_uses_default(self):
        bot_health.note_start(now=1000.0)
        self._mark_live(1900.0)
        with mock.patch.dict(os.environ, {"PORTAL_BOT_FRESH_S": " "}):
            self.assertTrue(bot_health.status(2000.0)["polling_alive"])

    async def test_fatal_drowns_even_a_fresh_poll(self):
        bot_health.note_start(now=1000.0)
        self._mark_live(1900.0)
        bot_health.note_fatal(TelegramUnauthorizedError(_Method("getMe"), "Unauthorized"))
        st = self._st(2000.0)
        self.assertFalse(st["polling_alive"])
        self.assertIn("TelegramUnauthorizedError", st["fatal"])
        self.assertEqual(st["fatal"], st["last_error"])

    async def test_never_started_has_no_ages(self):
        st = self._st(2000.0)
        self.assertIsNone(st["last_poll_age_s"])
        self.assertIsNone(st["started_age_s"])
        self.assertIsNone(st["next_retry_in_s"])
        self.assertFalse(st["polling_alive"])
        self.assertIsNone(st["fatal"])

    async def test_failure_records_reason_attempt_and_next_retry(self):
        bot_health.note_start(now=1000.0)
        bot_health.note_failure(ConnectionError("read timeout"), 8.0, 3, now=2000.0)
        st = self._st(2000.0)
        self.assertEqual(st["attempt"], 3)
        self.assertEqual(st["restarts"], 1)
        self.assertEqual(st["next_retry_in_s"], 8.0)
        self.assertIn("ConnectionError: read timeout", st["last_error"])

    async def test_failure_redacts_the_bot_token(self):
        bot_health.note_failure(TelegramNetworkError(_Method("getMe"), TOKEN_URL), 1.0, 1, now=2000.0)
        err = self._st(2000.0)["last_error"]
        self.assertNotIn(FAKE_TOKEN, err)
        self.assertNotIn(FAKE_TOKEN, str(err.encode()))
        self.assertIn("bot<TOKEN>", err)

    async def test_negative_delay_clamps_the_retry_clock(self):
        bot_health.note_failure(ConnectionError("x"), -5.0, 1, now=2000.0)
        self.assertEqual(self._st(2000.0)["next_retry_in_s"], 0.0)

    async def test_poll_closes_the_failure_streak(self):
        bot_health.note_failure(ConnectionError("x"), 1.0, 2, now=2000.0)
        self._mark_live(2005.0)
        st = self._st(2005.0)
        self.assertEqual(st["attempt"], 0)
        self.assertIsNone(st["last_error"])
        self.assertEqual(st["polls"], 1)

    async def test_reset_wipes_everything(self):
        bot_health.note_start(now=1000.0)
        bot_health.note_failure(ConnectionError("x"), 1.0, 1, now=2000.0)
        self._mark_live(2001.0)
        bot_health.note_fatal(ConnectionError("dead"))
        bot_health.reset()
        st = self._st(2002.0)
        self.assertEqual(
            [st["polls"], st["restarts"], st["attempt"], st["last_poll_age_s"], st["started_age_s"]],
            [0, 0, 0, None, None],
        )
        self.assertIsNone(st["fatal"])
        self.assertIsNone(st["last_error"])
        self.assertIsNone(st["next_retry_in_s"])


class TestSilenceMeasurement(BotHealthCase):
    async def test_alive_bot_is_not_silent(self):
        bot_health.note_start(now=1000.0)
        self._mark_live(1990.0)
        self.assertEqual(bot_health.silent_for_s(2000.0), 0.0)

    async def test_silence_counts_from_the_fresher_signal(self):
        bot_health.note_start(now=1000.0)
        self._mark_live(1500.0)
        self.assertEqual(bot_health.silent_for_s(2000.0), 500.0)  # от последнего опроса, не от старта

    async def test_stall_without_any_poll_counts_from_start(self):
        bot_health.note_start(now=1500.0)
        self.assertEqual(bot_health.silent_for_s(2000.0), 500.0)

    async def test_no_attempts_at_all_is_not_measurable(self):
        self.assertIsNone(bot_health.silent_for_s(2000.0))

    async def test_restart_resets_the_silence_clock(self):
        bot_health.note_start(now=1000.0)
        bot_health.note_failure(ConnectionError("net down"), 2.0, 1, now=1200.0)
        bot_health.note_start(now=1990.0)
        self.assertEqual(bot_health.silent_for_s(2000.0), 10.0)


class TestDoctorSuffix(BotHealthCase):
    async def test_alive_without_restarts(self):
        bot_health.note_start(now=1000.0)
        self._mark_live(1900.0)
        self.assertEqual(bot_health.doctor_suffix(2000.0), "поллинг жив, опрос 100с назад")

    async def test_alive_after_restarts_shows_the_counter(self):
        bot_health.note_start(now=1000.0)
        bot_health.note_failure(ConnectionError("net"), 1.0, 1, now=1500.0)
        self._mark_live(1900.0)
        self.assertEqual(
            bot_health.doctor_suffix(2000.0),
            "поллинг жив, опрос 100с назад · рестартов 1",
        )

    async def test_silent_without_a_streak(self):
        bot_health.note_start(now=1000.0)
        self.assertEqual(bot_health.doctor_suffix(1500.0), "поллинг молчит 500с")

    async def test_silent_mid_streak_names_attempt_wait_and_failure(self):
        bot_health.note_start(now=1000.0)
        bot_health.note_failure(ConnectionError("net down"), 8.0, 3, now=2000.0)
        self.assertEqual(
            bot_health.doctor_suffix(2000.0),
            "поллинг молчит 1000с, попытка 3, рестарт через 8с · сбой: ConnectionError: net down",
        )

    async def test_fatal_names_the_reason_and_the_stop(self):
        bot_health.note_fatal(TelegramUnauthorizedError(_Method("getMe"), "Unauthorized"))
        text = bot_health.doctor_suffix(2000.0)
        self.assertIn("поллинг убит отказом токена (TelegramUnauthorizedError: ", text)
        self.assertIn("рестарты прекращены", text)

    async def test_fatal_text_never_leaks_the_token(self):
        bot_health.note_fatal(TelegramNetworkError(_Method("getMe"), TOKEN_URL))
        self.assertNotIn(FAKE_TOKEN, bot_health.doctor_suffix(2000.0))

    async def test_never_started_admits_it(self):
        self.assertEqual(bot_health.doctor_suffix(2000.0), "поллинг ещё не запускался")


class TestBackoffPolicy(BotHealthCase):
    async def test_grows_exponentially_with_jitter_of_plus_minus_25(self):
        with mock.patch.dict(os.environ, {"PORTAL_BOT_BACKOFF_BASE_S": "2", "PORTAL_BOT_BACKOFF_CAP_S": "300"}):
            self.assertEqual(bot_health.backoff_delay(1, rng=lambda: 0.5), 2.0)
            self.assertEqual(bot_health.backoff_delay(2, rng=lambda: 0.5), 4.0)
            self.assertEqual(bot_health.backoff_delay(3, rng=lambda: 0.5), 8.0)
            self.assertEqual(bot_health.backoff_delay(3, rng=lambda: 0.0), 6.0)
            self.assertEqual(bot_health.backoff_delay(3, rng=lambda: 1.0), 10.0)

    async def test_never_exceeds_the_cap(self):
        with mock.patch.dict(os.environ, {"PORTAL_BOT_BACKOFF_BASE_S": "2", "PORTAL_BOT_BACKOFF_CAP_S": "300"}):
            self.assertEqual(bot_health.backoff_delay(20, rng=lambda: 1.0), 300.0)

    async def test_attempt_below_one_does_not_collapse_the_step(self):
        with mock.patch.dict(os.environ, {"PORTAL_BOT_BACKOFF_BASE_S": "2"}):
            self.assertEqual(bot_health.backoff_delay(0, rng=lambda: 0.5), 2.0)

    async def test_tuners_fall_back_when_env_is_junk(self):
        with mock.patch.dict(os.environ, {"PORTAL_BOT_BACKOFF_BASE_S": "нет", "PORTAL_BOT_BACKOFF_CAP_S": ""}):
            with self.assertLogs("portal.bot_health", level="WARNING") as cm:
                self.assertEqual(bot_health.backoff_delay(1, rng=lambda: 0.5), 2.0)
        self.assertIn("PORTAL_BOT_BACKOFF_BASE_S", "\n".join(cm.output))


class TestSupervise(BotHealthCase):
    async def test_polling_that_ends_cleanly_is_not_restarted(self):
        calls = []

        async def run(cfg, daemon):
            calls.append(1)

        async def sleep(delay):
            raise AssertionError("при штатном выходе спать не нужно")

        with self.assertLogs("portal.bot_health", level="INFO") as cm:
            await bot_health.supervise(object(), object(), run=run, sleep=sleep)
        self.assertEqual(calls, [1])
        self.assertIn("штатно", "\n".join(cm.output))
        self.assertEqual(bot_health.status()["restarts"], 0)

    async def test_start_refusal_is_retried_until_it_lands(self):
        calls = []
        delays = []

        async def run(cfg, daemon):
            calls.append(len(calls) + 1)
            if len(calls) >= 3:
                return  # третий старт успешен, поллинг пошёл
            raise TelegramNetworkError(_Method("getMe"), "HTTP Client says - net down")

        async def sleep(delay):
            delays.append(delay)

        with mock.patch.dict(os.environ, {"PORTAL_BOT_BACKOFF_BASE_S": "2", "PORTAL_BOT_BACKOFF_CAP_S": "300"}):
            await bot_health.supervise(object(), object(), run=run, sleep=sleep, rng=lambda: 0.5)
        self.assertEqual(delays, [2.0, 4.0])
        self.assertEqual(len(calls), 3)
        self.assertEqual(bot_health.status()["restarts"], 2)

    async def test_a_polling_success_closes_the_streak(self):
        async def run(cfg, daemon):
            raise ConnectionError("net down")

        async def sleep(delay):
            raise _StopLoop

        with self.assertRaises(_StopLoop):
            await bot_health.supervise(object(), object(), run=run, sleep=sleep)
        st = bot_health.status()
        self.assertEqual(st["attempt"], 1)
        self.assertEqual(st["restarts"], 1)
        self.assertIn("ConnectionError: net down", st["last_error"])
        self.assertIsNone(st["fatal"])
        self.assertFalse(st["polling_alive"])

    async def test_rejected_token_stops_retries_and_alerts_nothing(self):
        calls = []
        notifier = _Notifier()

        async def run(cfg, daemon):
            calls.append(1)
            raise TelegramUnauthorizedError(_Method("getMe"), f"Unauthorized, token {FAKE_TOKEN}")

        async def sleep(delay):
            raise AssertionError("после отказа токена ретраев не будет")

        with self.assertLogs("portal.bot_health", level="ERROR") as cm:
            await bot_health.supervise(object(), object(), run=run, sleep=sleep)
        self.assertEqual(calls, [1])
        self.assertEqual(notifier.sent, [])
        self.assertEqual(bot_health.status()["restarts"], 0)
        log = "\n".join(cm.output)
        self.assertIn("ретраи прекращены", log)
        self.assertIn("TELEGRAM_BOT_TOKEN", log)
        self.assertNotIn(FAKE_TOKEN, log)
        self.assertIn("TelegramUnauthorizedError", bot_health.status()["fatal"])
        self.assertNotIn(FAKE_TOKEN, bot_health.status()["fatal"])

    async def test_cancellation_propagates_out_of_the_backoff(self):
        async def run(cfg, daemon):
            raise ConnectionError("net down")

        async def sleep(delay):
            raise asyncio.CancelledError

        with self.assertRaises(asyncio.CancelledError):
            await bot_health.supervise(object(), object(), run=run, sleep=sleep)

    async def test_cancellation_during_a_live_poll_ends_the_task(self):
        started = asyncio.Event()

        async def run(cfg, daemon):
            started.set()
            await asyncio.sleep(30)

        task = asyncio.create_task(bot_health.supervise(object(), object(), run=run))
        with self.assertLogs("portal.bot_health", level="INFO") as cm:
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertIn("остановлен вместе с порталом", "\n".join(cm.output))
        self.assertEqual(bot_health.status()["restarts"], 0)


class TestSilenceMonitor(BotHealthCase):
    async def test_silence_past_the_threshold_alarms_once(self):
        bot_health.note_start(now=1000.0)
        notifier = _Notifier()
        stepper = _Stepper([(2000.0, None), (2100.0, None), (2200.0, None)])
        await _drain(
            bot_health.monitor(
                notifier,
                silent_after_s=900.0,
                interval_s=60.0,
                sleep=stepper.sleep,
                monotonic=stepper.monotonic,
            )
        )
        self.assertEqual(
            notifier.sent,
            ["⚠️ Telegram-бот молчит 1000с: поллинг не отвечает. Смотри PORTAL_LOG_FILE."],
        )
        self.assertEqual(stepper.slept, [60.0, 60.0, 60.0])  # три шага, на четвёртом — стоп

    async def test_fresh_silence_stays_quiet(self):
        bot_health.note_start(now=1900.0)
        notifier = _Notifier()
        stepper = _Stepper([(2000.0, None)])
        await _drain(
            bot_health.monitor(
                notifier,
                silent_after_s=900.0,
                interval_s=60.0,
                sleep=stepper.sleep,
                monotonic=stepper.monotonic,
            )
        )
        self.assertEqual(notifier.sent, [])

    async def test_liveness_before_the_threshold_never_alarms(self):
        bot_health.note_start(now=1000.0)
        self._mark_live(1990.0)
        notifier = _Notifier()
        stepper = _Stepper([(2000.0, None)])
        await _drain(
            bot_health.monitor(
                notifier,
                silent_after_s=900.0,
                interval_s=60.0,
                sleep=stepper.sleep,
                monotonic=stepper.monotonic,
            )
        )
        self.assertEqual(notifier.sent, [])

    async def test_recovery_is_announced_once_after_the_alarm(self):
        bot_health.note_start(now=1000.0)
        notifier = _Notifier()
        stepper = _Stepper([(2000.0, None), (2010.0, lambda: self._mark_live(2010.0)), (2020.0, None)])
        await _drain(
            bot_health.monitor(
                notifier,
                silent_after_s=900.0,
                interval_s=60.0,
                sleep=stepper.sleep,
                monotonic=stepper.monotonic,
            )
        )
        self.assertEqual(
            notifier.sent,
            [
                "⚠️ Telegram-бот молчит 1000с: поллинг не отвечает. Смотри PORTAL_LOG_FILE.",
                "✅ Telegram-бот снова отвечает.",
            ],
        )

    async def test_silence_without_any_start_attempt_is_not_reported(self):
        notifier = _Notifier()
        stepper = _Stepper([(2000.0, None)])
        await _drain(
            bot_health.monitor(
                notifier,
                silent_after_s=900.0,
                interval_s=60.0,
                sleep=stepper.sleep,
                monotonic=stepper.monotonic,
            )
        )
        self.assertEqual(notifier.sent, [])

    async def test_fatal_token_silences_the_warden_too(self):
        bot_health.note_start(now=1000.0)
        bot_health.note_fatal(TelegramUnauthorizedError(_Method("getMe"), "Unauthorized"))
        notifier = _Notifier()
        stepper = _Stepper([(2000.0, None), (2100.0, None)])
        await _drain(
            bot_health.monitor(
                notifier,
                silent_after_s=900.0,
                interval_s=60.0,
                sleep=stepper.sleep,
                monotonic=stepper.monotonic,
            )
        )
        self.assertEqual(notifier.sent, [])

    async def test_threshold_and_interval_come_from_the_environment(self):
        bot_health.note_start(now=1000.0)
        notifier = _Notifier()
        stepper = _Stepper([(1500.0, None)])
        with mock.patch.dict(
            os.environ, {"PORTAL_BOT_SILENT_AFTER_S": "100", "PORTAL_WATCH_INTERVAL_S": "7"}
        ):
            await _drain(
                bot_health.monitor(
                    notifier,
                    interval_s=7.0,
                    sleep=stepper.sleep,
                    monotonic=stepper.monotonic,
                )
            )
        self.assertEqual(len(notifier.sent), 1)
        self.assertIn("молчит 500с", notifier.sent[0])
        self.assertEqual(stepper.slept, [7.0])

    async def test_garbage_threshold_falls_back_to_the_default(self):
        bot_health.note_start(now=1000.0)
        notifier = _Notifier()
        stepper = _Stepper([(1500.0, None)])
        with mock.patch.dict(os.environ, {"PORTAL_BOT_SILENT_AFTER_S": "полдня"}):
            with self.assertLogs("portal.bot_health", level="WARNING") as cm:
                await _drain(
                    bot_health.monitor(
                        notifier,
                        interval_s=60.0,
                        sleep=stepper.sleep,
                        monotonic=stepper.monotonic,
                    )
                )
        self.assertIn("PORTAL_BOT_SILENT_AFTER_S", "\n".join(cm.output))
        self.assertEqual(notifier.sent, [])  # 500с < дефолтных 900с — будить рано

    async def test_warden_survives_a_broken_alert_channel(self):
        bot_health.note_start(now=1000.0)
        notifier = _Notifier(error=ConnectionError("telegram unavailable"))
        stepper = _Stepper([(2000.0, None), (2100.0, None), (2200.0, None)])
        with self.assertLogs("portal.bot_health", level="WARNING") as cm:
            await _drain(
                bot_health.monitor(
                    notifier,
                    silent_after_s=900.0,
                    interval_s=60.0,
                    sleep=stepper.sleep,
                    monotonic=stepper.monotonic,
                )
            )
        self.assertIn("алерт не отправлен", "\n".join(cm.output))
        self.assertEqual(len(notifier.sent), 1)  # дедуп не сломался из-за сбоя доставки


if __name__ == "__main__":
    unittest.main()
