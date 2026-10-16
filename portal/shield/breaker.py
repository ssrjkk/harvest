"""Circuit Breaker + ретраи для исходящих интеграций (БД, внешние API, RPC).

Состояния: Closed → Open → Half-Open. Открытие после `failure_threshold`
подряд идущих ошибок; через `recovery_timeout_s` — ограниченное число
проб Half-Open; успех проб закрывает контур. `run_with_retry` добавляет
ретраи с экспоненциальным backoff и полным джиттером.

CancelledError никогда не считается ошибкой контура и пробрасывается.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from .config import BreakerConfig

T = TypeVar("T")

CLOSED = "closed"
OPEN = "open"
HALF_OPEN = "half-open"


class CircuitOpenError(RuntimeError):
    """Контур открыт: вызов отклонён без обращения к downstream."""


class CircuitBreaker:
    """Потокобезопасный брейкер одного downstream-ресурса."""

    def __init__(self, config: BreakerConfig, name: str = "default") -> None:
        config.validate()
        self.cfg = config
        self.name = name
        self._lock = asyncio.Lock()
        self._state = CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self._probes_in_flight = 0
        self.opens = 0
        self.rejected = 0

    async def state(self) -> str:
        async with self._lock:
            await self._maybe_half_open_locked(time.monotonic())
            return self._state

    async def _maybe_half_open_locked(self, now: float) -> None:
        if self._state == OPEN and now - self._opened_at >= self.cfg.recovery_timeout_s:
            self._state = HALF_OPEN
            self._probes_in_flight = 0

    async def call(self, fn: Callable[[], Awaitable[T]]) -> T:
        """Выполнить через контур. Raises CircuitOpenError при открытом."""
        now = time.monotonic()
        async with self._lock:
            await self._maybe_half_open_locked(now)
            if self._state == OPEN:
                self.rejected += 1
                raise CircuitOpenError(f"circuit {self.name} is open")
            if self._state == HALF_OPEN:
                if self._probes_in_flight >= self.cfg.half_open_probes:
                    self.rejected += 1
                    raise CircuitOpenError(f"circuit {self.name} probe budget spent")
                self._probes_in_flight += 1
        try:
            result = await fn()
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._record_failure()
            raise
        await self._record_success()
        return result

    async def _record_failure(self) -> None:
        async with self._lock:
            if self._state == HALF_OPEN:
                self._state = OPEN
                self._opened_at = time.monotonic()
                self.opens += 1
                return
            self._failures += 1
            if self._failures >= self.cfg.failure_threshold:
                self._state = OPEN
                self._opened_at = time.monotonic()
                self.opens += 1

    async def _record_success(self) -> None:
        async with self._lock:
            self._failures = 0
            self._state = CLOSED
            self._probes_in_flight = 0

    async def snapshot(self) -> dict[str, Any]:
        async with self._lock:
            return {
                "name": self.name,
                "state": self._state,
                "failures": self._failures,
                "opens": self.opens,
                "rejected": self.rejected,
            }


def backoff_delay(attempt: int, base_s: float, cap_s: float) -> float:
    """Экспоненциальный backoff с полным джиттером: uniform(0, min(cap, base*2^k))."""
    return random.uniform(0.0, min(cap_s, base_s * (2.0 ** max(attempt, 0))))


async def run_with_retry(  # noqa: UP047 — классический TypeVar валиден и shared с call()
    fn: Callable[[], Awaitable[T]],
    attempts: int = 3,
    base_s: float = 0.5,
    cap_s: float = 8.0,
    breaker: CircuitBreaker | None = None,
    retryable: Callable[[BaseException], bool] | None = None,
) -> T:
    """Выполнить с ретраями. Последняя ошибка пробрасывается наружу."""
    last: BaseException | None = None
    tries = max(1, int(attempts))
    for k in range(tries):
        try:
            if breaker is not None:
                return await breaker.call(fn)
            return await fn()
        except asyncio.CancelledError:
            raise
        except CircuitOpenError:
            raise
        except Exception as e:  # noqa: BLE001
            last = e
            if retryable is not None and not retryable(e):
                raise
            if k < tries - 1:
                await asyncio.sleep(backoff_delay(k, base_s, cap_s))
    assert last is not None
    raise last
