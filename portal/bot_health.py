"""Живость Telegram-бота: учёт поллинга, супервизор с backoff, честный статус.

19.11.2026 сетевой сбой пришёлся ровно на старт процесса. aiogram делает
`await bot.me()` до входа в собственный retry-цикл
(aiogram/dispatcher/dispatcher.py:378), поэтому TelegramNetworkError вылетел
наружу из `dp.start_polling`: задача бота завершилась, портал остался жить с
глухим ботом, и молчание никто не замечал — `/doctor` тоже команда того же
мёртвого бота.

Отсюда четыре части:
  * `note_poll` / `observe_polling` — отмечают только успешные `getUpdates`:
    они одни доказывают, что поллинг крутится (отправка сообщения работает и
    при мёртвом поллинге, поэтому живостью не считается);
  * `supervise` — перезапускает бота с экспоненциальным backoff и джиттером;
    фатален только отказ токена, остальное — сеть, которую надо переждать;
  * `status` / `silent_for_s` / `doctor_suffix` — читают `/doctor`, `/api/stats`
    и алерты;
  * `monitor` — сторож: молчание дольше порога будит владельца одним алертом.

Модуль не импортирует `portal.bot_telegram` (иначе получится цикл): сам запуск
бота передаёт вызывающая сторона через `run`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram.exceptions import TelegramUnauthorizedError

from portal.bot_render import redact_token

logger = logging.getLogger("portal.bot_health")

# Дефолты пауз между попытками; перебиваются через PORTAL_BOT_BACKOFF_*.
BACKOFF_BASE_S = 2.0
BACKOFF_CAP_S = 300.0
BACKOFF_JITTER = 0.25
# getUpdates приходит каждые polling_timeout (~30 с), поэтому свежее 120 с —
# ещё живое молчание, старше — поллинг уже не крутится.
POLL_FRESH_S = 120.0
# С какого молчания сторож `monitor` считает аварию и будит владельца.
SILENT_ALERT_AFTER_S = 900.0
# Как часто сторож смотрит на живость: зазор между проверками.
MONITOR_INTERVAL_S = 60.0
# Тексты сторожа: молчание и возврат к жизни.
SILENT_ALERT = "⚠️ Telegram-бот молчит {age:.0f}с: поллинг не отвечает. Смотри PORTAL_LOG_FILE."
RECOVERED_ALERT = "✅ Telegram-бот снова отвечает."

_started_ts = 0.0
_last_poll_ts = 0.0
_next_retry_at = 0.0
_attempt = 0
_polls = 0
_restarts = 0
_last_error: str | None = None
_fatal: str | None = None


def _env_float(name: str, default: float) -> float:
    """Числовой тюнер из окружения; мусор в значении — молча берём дефолт."""
    raw = os.environ.get(name, "")
    if not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("%s=%r — не число, беру дефолт %.1f", name, raw[:40], default)
        return default


def reset() -> None:
    """Обнулить состояние (новый процесс, тесты)."""
    global _attempt, _fatal, _last_error, _last_poll_ts, _next_retry_at, _polls, _restarts, _started_ts
    _started_ts = 0.0
    _last_poll_ts = 0.0
    _next_retry_at = 0.0
    _attempt = 0
    _polls = 0
    _restarts = 0
    _last_error = None
    _fatal = None


def note_start(now: float | None = None) -> None:
    """Началась попытка поднять бота — с этого момента таймауты отсчитываем заново."""
    global _fatal, _started_ts
    _started_ts = time.monotonic() if now is None else float(now)
    _fatal = None


def note_poll(now: float | None = None) -> None:
    """Успешный getUpdates: бот слышит, серия падений закрыта."""
    global _attempt, _last_error, _last_poll_ts, _polls
    _last_poll_ts = time.monotonic() if now is None else float(now)
    _polls += 1
    _attempt = 0
    _last_error = None


def note_failure(exc: BaseException, delay: float, attempt: int, now: float | None = None) -> None:
    """Попытка упала: причина, номер и когда следующий рестарт."""
    global _attempt, _last_error, _next_retry_at, _restarts
    now = time.monotonic() if now is None else float(now)
    _attempt = int(attempt)
    _restarts += 1
    _next_retry_at = now + max(float(delay), 0.0)
    _last_error = f"{type(exc).__name__}: {redact_token(str(exc))[:200]}"


def note_fatal(exc: BaseException) -> None:
    """Рестарты бессмысленны: токен отвергнут."""
    global _fatal, _last_error
    _fatal = f"{type(exc).__name__}: {redact_token(str(exc))[:200]}"
    _last_error = _fatal


def observe_polling(bot: Any) -> None:
    """Засчитывать живость по успешным getUpdates этого бота.

    aiogram 3 не даёт хук на цикл поллинга — `start_polling` крутит внутреннюю
    задачу, и единственный наблюдаемый снаружи момент это ответ `getUpdates`
    внутри сессии.
    """
    session = bot.session
    original = session.make_request

    async def wrapped(bot_: Any, method: Any, *args: Any, **kwargs: Any) -> Any:
        result = await original(bot_, method, *args, **kwargs)
        if getattr(method, "method", "") == "getUpdates":
            note_poll()
        return result

    session.make_request = wrapped


def backoff_delay(attempt: int, rng: Callable[[], float] = random.random) -> float:
    """Пауза перед попыткой `attempt`: exp backoff от base до cap ± джиттер."""
    base = _env_float("PORTAL_BOT_BACKOFF_BASE_S", BACKOFF_BASE_S)
    cap = _env_float("PORTAL_BOT_BACKOFF_CAP_S", BACKOFF_CAP_S)
    # Свой расчёт, а не shield.backoff_delay: там полный джиттер умеет спать ~0,
    # а рестарт-циклу нужен пол под ногами. Cap — потолок уже после джиттера.
    raw = base * (2 ** (max(int(attempt), 1) - 1))
    return round(min(cap, raw * (1.0 + BACKOFF_JITTER * (rng() * 2.0 - 1.0))), 2)


def _age(now: float, since: float) -> float | None:
    """Возраст метки; 0.0 (метки не было) — None."""
    return None if not since else round(now - since, 1)


def status(now: float | None = None) -> dict:
    """Снимок живости: слышит ли бот, сколько молчит, где стоит супервизор."""
    now = time.monotonic() if now is None else float(now)
    fresh = _env_float("PORTAL_BOT_FRESH_S", POLL_FRESH_S)
    last_poll_age = _age(now, _last_poll_ts)
    alive = _fatal is None and last_poll_age is not None and last_poll_age <= fresh
    return {
        "polling_alive": alive,
        "last_poll_age_s": last_poll_age,
        "started_age_s": _age(now, _started_ts),
        "polls": _polls,
        "restarts": _restarts,
        "attempt": _attempt,
        "next_retry_in_s": None if not _next_retry_at else round(max(0.0, _next_retry_at - now), 1),
        "fatal": _fatal,
        "last_error": _last_error,
    }


def silent_for_s(now: float | None = None) -> float | None:
    """Сколько секунд нет живого поллинга; None — попыток ещё не было."""
    st = status(now)
    if st["polling_alive"]:
        return 0.0
    ages = [v for v in (st["last_poll_age_s"], st["started_age_s"]) if v is not None]
    return min(ages) if ages else None


def doctor_suffix(now: float | None = None) -> str:
    """Строка живости для `/doctor`, `/api/stats` и алертов: без оптимизма, как есть."""
    st = status(now)
    if st["fatal"]:
        return f"поллинг убит отказом токена ({st['fatal']}), рестарты прекращены"
    if not st["polling_alive"]:
        silent = silent_for_s(now)
        if silent is None:
            return "поллинг ещё не запускался"
        text = f"поллинг молчит {silent:.0f}с"
        if st["attempt"]:
            text += f", попытка {st['attempt']}, рестарт через {(st['next_retry_in_s'] or 0.0):.0f}с"
        return text + (f" · сбой: {st['last_error']}" if st["last_error"] else "")
    tail = f"поллинг жив, опрос {st['last_poll_age_s']:.0f}с назад"
    return f"{tail} · рестартов {st['restarts']}" if st["restarts"] else tail


async def supervise(
    cfg: Any,
    daemon: Any,
    *,
    run: Callable[..., Awaitable[None]],
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    rng: Callable[[], float] = random.random,
) -> None:
    """Держать бота живым: перезапуск с backoff вместо молчаливой смерти задачи.

    Штатное завершение `run` (портал останавливают) не перезапускается; отмена
    пробрасывается наружу, чтобы graceful shutdown не завис. Сетевые сбои —
    повод ждать и пробовать снова; ретраи прекращает только отказ токена, и
    тогда единственный честный след — журнал плюс `fatal` в статусе: тем же
    Telegram-каналом, который токен отверг, будить владельца невозможно.
    """
    attempt = 0
    while True:
        note_start()
        try:
            await run(cfg, daemon)
        except asyncio.CancelledError:
            logger.info("Супервизор бота остановлен вместе с порталом")
            raise
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, TelegramUnauthorizedError):
                note_fatal(exc)
                logger.error(
                    "Telegram-бот не авторизован (%s) — ретраи прекращены, проверь TELEGRAM_BOT_TOKEN",
                    _fatal,
                )
                return
            attempt += 1
            delay = backoff_delay(attempt, rng)
            note_failure(exc, delay, attempt)
            logger.warning("Бот упал (%s), рестарт #%d через %.0fс", _last_error, attempt, delay)
            await sleep(delay)
        else:
            logger.info("Поллинг завершился штатно — супервизор выходит из цикла")
            return


async def _alert(notify: Callable[[str], Awaitable[None]], text: str) -> None:
    """Алерт владельцу; чужой сбой доставки не должен ронять сторожа."""
    try:
        await notify(text)
    except Exception:  # noqa: BLE001
        logger.warning("Сторож бота: алерт не отправлен", exc_info=True)


async def monitor(
    notify: Callable[[str], Awaitable[None]],
    *,
    silent_after_s: float | None = None,
    interval_s: float = MONITOR_INTERVAL_S,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> None:
    """Сторож поллинга: молчание дольше порога — один алерт, возврат — второй.

    Опрашивает состояние раз в `interval_s`, а будит по порогу `silent_after_s`:
    иначе первая же проверка совпала бы с самим порогом и заметила аварию
    только через два порога. Алерт идёт через `notify` (sendMessage), который
    жив и при мёртвом `getUpdates`, — глухой бот всё-таки сам о себе сообщает.
    """
    if silent_after_s is None:
        silent_after_s = _env_float("PORTAL_BOT_SILENT_AFTER_S", SILENT_ALERT_AFTER_S)
    tripped = False
    while True:
        await sleep(interval_s)
        now = monotonic()
        st = status(now)
        if st["fatal"]:
            # Канал, которым шлём алерты, отвергнут вместе с токеном — будить нечем.
            continue
        if st["polling_alive"]:
            if tripped:
                tripped = False
                await _alert(notify, RECOVERED_ALERT)
            continue
        age = silent_for_s(now)
        if age is not None and age >= silent_after_s and not tripped:
            tripped = True
            await _alert(notify, SILENT_ALERT.format(age=age))
