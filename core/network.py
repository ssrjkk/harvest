"""Работа с RPC.

Асинхронные вызовы оборачивают синхронные web3-запросы через ThreadPoolExecutor,
чтобы event loop не блокировался и все wallet-задачи крутились параллельно.
Один общий экземпляр NetworkManager на весь пул.

Nonce management: берём из БД → инкрементируем → сохраняем,
чтобы параллельные транзакции одного адреса не конфликтовали.
TTL-кэш для gas_price/balance/nonce снижает число RPC-вызовов при масштабе.
"""

import asyncio
import gc
import inspect
import logging
import random
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any, TypeVar
from urllib.parse import urlsplit

from eth_account import Account
from web3 import Web3
from web3.exceptions import (
    ABIFunctionNotFound,
    BadFunctionCallOutput,
    BlockNotFound,
    CannotHandleRequest,
    ContractCustomError,
    ContractLogicError,
    ContractPanicError,
    InvalidAddress,
    InvalidTransaction,
    MethodNotSupported,
    MultipleFailedRequests,
    PersistentConnectionClosedOK,
    PersistentConnectionError,
    ProviderConnectionError,
    ReadBufferLimitReached,
    RequestTimedOut,
    StaleBlockchain,
    TimeExhausted,
    TooManyRequests,
    TransactionIndexingInProgress,
    TransactionNotFound,
    TransactionTypeMismatch,
    Web3AssertionError,
    Web3AttributeError,
    Web3TypeError,
    Web3ValidationError,
    Web3ValueError,
)

try:
    from web3.middleware import geth_poa_middleware as _poa_middleware
except ImportError:
    try:
        from web3.middleware import ExtraDataToPOAMiddleware as _poa_middleware
    except ImportError:
        _poa_middleware = None

from core.database import Database, _redact_rpc_url
from core.performance import effective_rpc_threads

logger = logging.getLogger(__name__)

_RPC_RETRIES = 3

_T = TypeVar("_T")

# Транспортные/инфраструктурные ошибки web3: ретрая почти всегда помогает.
_RETRYABLE_WEB3 = (
    BlockNotFound,
    CannotHandleRequest,
    MultipleFailedRequests,
    PersistentConnectionClosedOK,
    PersistentConnectionError,
    ProviderConnectionError,
    ReadBufferLimitReached,
    RequestTimedOut,
    StaleBlockchain,
    TimeExhausted,
    TooManyRequests,
    TransactionIndexingInProgress,
    TransactionNotFound,
)

# Логические ошибки транзакций/контрактов: ретрая никогда не поможет,
# только жжёт попытки, бэк-офф и срабатывания circuit breaker.
_NON_RETRYABLE_WEB3 = (
    ABIFunctionNotFound,
    BadFunctionCallOutput,
    ContractCustomError,
    ContractLogicError,
    ContractPanicError,
    InvalidAddress,
    InvalidTransaction,
    MethodNotSupported,
    TransactionTypeMismatch,
    Web3AssertionError,
    Web3AttributeError,
    Web3TypeError,
    Web3ValidationError,
    Web3ValueError,
)

# Текстовые маркеры из обёрнутых JSON-RPC-пакетов (ValueError с response-данными).
_NEVER_RETRY_HINTS = (
    "insufficient funds",
    "nonce too low",
    "nonce to low",
    "nonce is too low",
    "already known",
    "revert",
    "execution reverted",
    "intrinsic gas too low",
    "invalid nonce",
    "gas required exceeds allowance",
    "doesn't have enough funds",
    "sender doesn't have enough",
    "unknown account",
    "invalid argument",
    "invalid sender",
)

_RETRY_HINTS = (
    "rate limit",
    "too many requests",
    "limit exceeded",
    "timed out",
    "timeout",
    "connection",
    "temporarily",
    "try again",
    "server error",
    "internal error",
    "-32005",
    "-32603",
)


def _is_retryable_error(exc: BaseException) -> bool:
    """Поможет ли ретрая. Логические ошибки НЕ ретраим — это не мощность, а мусор.

    Транспорт (Connection/OSError/Timeout) и инфраструктурные web3-ошибки —
    ретраим с бэк-оффом. Nonce/revert/недостаток средств и прочие состояния,
    которые не изменятся от повтора, поднимаем сразу: воркер быстрее освобождается
    для следующей задачи, circuit breaker не срабатывает зря, failover не крутится.
    """
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return True
    if isinstance(exc, _NON_RETRYABLE_WEB3):
        return False
    if isinstance(exc, _RETRYABLE_WEB3):
        return True
    msg = str(exc).lower()
    if any(h in msg for h in _NEVER_RETRY_HINTS):
        return False
    if any(h in msg for h in _RETRY_HINTS):
        return True
    # Неизвестная web3-обёртка (Web3RPCError и т.п.) без явного маркера:
    # консервативно ретраим — транспортные сбои встречаются чаще логических.
    return True


def _join_threads_bounded(threads: list, limit: float) -> None:
    """Дожидается закрытия потоков с общим бюджетом времени (сек).

    Выполняется в отдельном потоке, чтобы не блокировать event loop.
    """
    start = time.monotonic()
    for t in threads:
        t.join(max(0.0, limit - (time.monotonic() - start)))


class _TTLCache:
    """Простой TTL-кэш на asyncio event loop (single-thread safe)."""

    __slots__ = ("_store", "_ttl", "_cleanup_counter")

    def __init__(self, ttl: float) -> None:
        self._store: dict[str, tuple[object, float]] = {}
        self._ttl = ttl
        self._cleanup_counter = 0

    def get(self, key: str) -> Any | None:
        entry = self._store.get(key)
        if entry is not None and (time.monotonic() - entry[1]) < self._ttl:
            return entry[0]
        return None

    def set(self, key: str, value: object) -> None:
        self._store[key] = (value, time.monotonic())
        # Автоматическая очистка каждые 100 записей
        self._cleanup_counter += 1
        if self._cleanup_counter >= 100:
            self._cleanup()

    def delete(self, key: str) -> None:
        """Инвалидирует запись (удаляет ключ)."""
        self._store.pop(key, None)

    def _cleanup(self) -> None:
        """Удаляет просроченные записи."""
        now = time.monotonic()
        expired = [k for k, v in self._store.items() if now - v[1] >= self._ttl]
        for k in expired:
            del self._store[k]
        self._cleanup_counter = 0


class _RPCMetrics:
    """Счётчик RPC-вызовов: количество, ошибки, общее время + окно здоровья.

    Окно «здоровья» (последние N вызовов) питает адаптивную обратную связь:
    при росте доли ошибок или задержки run_in_executor плавно снижает
    скорость RPC (backpressure), чтобы не заваливать деградирующий узел.
    """

    __slots__ = ("calls", "errors", "total_time", "window", "_last_reset")
    _WINDOW_SIZE = 50

    def __init__(self) -> None:
        self.calls = 0
        self.errors = 0
        self.total_time = 0.0
        self.window: deque[tuple[float, bool]] = deque(maxlen=self._WINDOW_SIZE)
        self._last_reset = time.monotonic()

    def record(self, elapsed: float, error: bool = False) -> None:
        self.calls += 1
        self.total_time += elapsed
        self.window.append((elapsed, error))
        if error:
            self.errors += 1

    def health(self) -> dict:
        """Здоровье узла по последнему окну: доля ошибок и средняя задержка (мс)."""
        if not self.window:
            return {"error_rate": 0.0, "avg_ms": 0.0, "samples": 0}
        n = len(self.window)
        errs = sum(1 for _t, e in self.window if e)
        avg_ms = sum(t for t, _e in self.window) / n * 1000
        return {"error_rate": errs / n, "avg_ms": avg_ms, "samples": n}

    def snapshot(self) -> dict:
        """Возвращает текущие метрики и сбрасывает счётчики."""
        uptime = time.monotonic() - self._last_reset
        data = {
            "calls": self.calls,
            "errors": self.errors,
            "error_rate": self.errors / max(self.calls, 1),
            "avg_latency_ms": (self.total_time / max(self.calls, 1)) * 1000,
            "uptime_s": round(uptime, 1),
        }
        self.calls = 0
        self.errors = 0
        self.total_time = 0.0
        self._last_reset = time.monotonic()
        return data

    def log_summary(self, logger_obj=None) -> None:
        log = logger_obj or logger
        s = self.snapshot()
        if s["calls"] > 0:
            log.info(
                f"RPC metrics: {s['calls']} calls, {s['errors']} errors "
                f"({s['error_rate']:.1%}), avg {s['avg_latency_ms']:.0f}ms"
            )


class _RPCRateLimiter:
    """Token bucket rate limiter для RPC-вызовов.

    Предотвращает превышение лимитов RPC-провайдера (rate limit 429).
    Работает на asyncio (single-thread safe).
    """

    __slots__ = ("_rate", "_tokens", "_last_refill", "_max_tokens")

    def __init__(self, rate: float = 50.0) -> None:
        """
        Args:
            rate: максимальное количество вызовов в секунду.
        """
        self._rate = rate
        self._max_tokens = rate * 2  # burst до 2x
        self._tokens = self._max_tokens
        self._last_refill = time.monotonic()

    async def acquire(self) -> None:
        """Ждёт, пока не появится доступный токен."""
        while True:
            self._refill()
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return
            # Ждём до появления следующего токена
            wait = (1.0 - self._tokens) / self._rate
            await asyncio.sleep(wait)

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self._max_tokens, self._tokens + elapsed * self._rate)
        self._last_refill = now

    @property
    def tokens(self) -> float:
        """Текущее количество токенов (для восстановления при failover)."""
        return self._tokens

    def recover_tokens(self, other: "_RPCRateLimiter", fraction: float = 0.5) -> None:
        """Переносит дробь токенов из другого limiter (при failover)."""
        self._tokens = min(other.tokens, self._max_tokens * fraction)

    def set_rate(self, rate: float) -> None:
        """Меняет скорость on-the-fly (адаптивная деградация под здоровьем RPC)."""
        rate = max(rate, 0.1)
        self._rate = rate
        self._max_tokens = rate * 2
        if self._tokens > self._max_tokens:
            self._tokens = self._max_tokens


def _effective_rpc_rate(base: float, health: dict, floor: float = 0.2) -> float:
    """Скорость RPC под здоровьем узла: плавная деградация, не ниже base*floor.

    Пороги — эмпирические: умеренные (8-15% ошибок или 1-2с задержка),
    тяжёлые (>=30% или >=4с). При здоровом узле — полная скорость.
    """
    er = float(health.get("error_rate", 0.0))
    avg_ms = float(health.get("avg_ms", 0.0))
    if er >= 0.60 or avg_ms >= 8000:
        factor = 0.15
    elif er >= 0.30 or avg_ms >= 4000:
        factor = 0.25
    elif er >= 0.15 or avg_ms >= 2000:
        factor = 0.5
    elif er >= 0.08 or avg_ms >= 1000:
        factor = 0.75
    else:
        factor = 1.0
    return max(base * factor, base * floor)


class _CircuitBreaker:
    """Circuit breaker: останавливает retries при последовательных ошибках.

    Состояния:
    - CLOSED: нормальная работа, ошибки считаются
    - OPEN: блокирует вызовы на recovery_timeout секунд
    - HALF_OPEN: пропускает один пробный вызов

    При failure_threshold последовательных ошибках → OPEN.
    После recovery_timeout → HALF_OPEN. Успешный вызов → CLOSED.
    """

    __slots__ = (
        "_failure_threshold",
        "_recovery_timeout",
        "_state",
        "_failure_count",
        "_last_failure_time",
        "_half_open_attempted",
    )

    def __init__(self, failure_threshold: int = 5, recovery_timeout: float = 30.0) -> None:
        self._failure_threshold = failure_threshold
        self._recovery_timeout = recovery_timeout
        self._state = "CLOSED"
        self._failure_count = 0
        self._last_failure_time = 0.0
        self._half_open_attempted = False

    def record_success(self) -> None:
        if self._state == "HALF_OPEN":
            self._state = "CLOSED"
        self._failure_count = 0

    def record_failure(self) -> None:
        self._failure_count += 1
        self._last_failure_time = time.monotonic()
        if self._failure_count >= self._failure_threshold:
            self._state = "OPEN"
            logger.warning(
                f"Circuit breaker OPEN after {self._failure_count} failures, recovery in {self._recovery_timeout}s"
            )

    def allow_request(self) -> bool:
        if self._state == "OPEN":
            if time.monotonic() - self._last_failure_time >= self._recovery_timeout:
                self._state = "HALF_OPEN"
                self._half_open_attempted = False
            else:
                return False
        if self._state == "HALF_OPEN":
            if not self._half_open_attempted:
                self._half_open_attempted = True
                return True
            return False
        return True

    def log_status(self) -> None:
        if self._state != "CLOSED":
            logger.info(f"Circuit breaker: state={self._state}, failures={self._failure_count}")


class NetworkManager:
    def __init__(self, config: dict, db: Database) -> None:
        net = config["network"]
        # Поддержка нескольких RPC: rpc_url может быть строкой или списком
        rpc_url = net["rpc_url"]
        if isinstance(rpc_url, list):
            self._rpc_urls = rpc_url
        else:
            self._rpc_urls = [rpc_url]
        self.rpc_url = self._rpc_urls[0]
        self.chain_id = net["chain_id"]
        self._rpc_index = 0
        self.w3 = self._connect_rpc(self.rpc_url)
        # Переиспользуемый провайдер для latency-замеров: HTTPProvider дорог в создании
        # и не закрывается в волюнтарном цикле, поэтому создаём один на эндпоинт.
        self._probe_w3: dict[str, Web3] = {}
        rpc_threads = effective_rpc_threads(config)
        self._executor = ThreadPoolExecutor(max_workers=rpc_threads)
        self._accounts: dict[str, Account] = {}
        self._db = db
        self._nonce_locks: dict[str, asyncio.Lock] = {}
        self._rpc_lock: asyncio.Lock | None = None  # ленивая инициализация
        # TTL-кэши: уменьшают число RPC-вызовов при масштабе
        cache_conf = config.get("cache", {})
        self._gas_ttl = cache_conf.get("gas_price_ttl", 15)
        self._balance_ttl = cache_conf.get("balance_ttl", 10)
        self._nonce_ttl = cache_conf.get("nonce_ttl", 30)
        self._gas_price_cache = _TTLCache(self._gas_ttl)
        self._balance_cache = _TTLCache(self._balance_ttl)
        self._nonce_cache = _TTLCache(self._nonce_ttl)
        # Rate limiter, метрики и circuit breaker
        self._rpc_rate = config.get("cache", {}).get("rpc_rate_limit", 50)
        self._rate_limiter = _RPCRateLimiter(self._rpc_rate)
        self._applied_rate = self._rpc_rate
        self._rate_floor = config.get("cache", {}).get("rpc_rate_floor", 0.2)
        self._metrics = _RPCMetrics()
        cb_conf = config.get("cache", {})
        self._circuit_breaker = _CircuitBreaker(
            failure_threshold=cb_conf.get("cb_failure_threshold", 5),
            recovery_timeout=cb_conf.get("cb_recovery_timeout", 30),
        )

    def _connect_rpc(self, url: str) -> Web3:
        if url.startswith("http://"):
            host = (urlsplit(url).hostname or "").lower()
            if host not in ("127.0.0.1", "localhost", "::1"):
                logger.warning(
                    "RPC по HTTP без TLS (%s): подписи/балансы/nonce передаются в открытом виде. "
                    "Для продакшена используйте https://",
                    _redact_rpc_url(url) or url,
                )
        w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 30}))
        if _poa_middleware is not None:
            try:
                w3.middleware_onion.inject(_poa_middleware, layer=0)
            except Exception:
                pass
        return w3

    def clear_accounts_cache(self) -> None:
        """Очищает кэш Account-объектов (содержат приватные ключи в памяти).

        Принудительный gc.collect() ускоряет освобождение памяти с ключами.
        """
        self._accounts.clear()
        gc.collect()

    def invalidate_balance(self, address: str) -> None:
        """Инвалидирует balance-кэш для адреса после транзакции.

        Иначе stale TTL-значение (10 сек) маскирует списание и проверка
        check_balance_before_action видит «старый» баланс.
        """
        try:
            address = Web3.to_checksum_address(address)
        except Exception:
            return
        self._balance_cache.delete(address)

    def log_diagnostics(self) -> None:
        """Логирует RPC-метрики и circuit breaker."""
        self._metrics.log_summary()
        self._circuit_breaker.log_status()

    async def close(self) -> None:
        # Закрываем все HTTP-провайдеры (TCP-сокеты): probe + основной RPC.
        for w3 in list(self._probe_w3.values()):
            await self._close_w3_provider(w3)
        self._probe_w3.clear()
        await self._close_w3_provider(getattr(self, "w3", None))
        # Не-daemon потоки executor блокируют выход процесса, если не дождаться их.
        # Ждём завершения выполняющихся запросов (каждый ограничен RPC-таймаутом 30с),
        # но с жёстким пределом — зависший HTTP не должен держать процесс вечно.
        self._executor.shutdown(wait=False, cancel_futures=True)
        threads = list(getattr(self._executor, "_threads", []))
        if threads:
            limit = 35
            await asyncio.get_running_loop().run_in_executor(None, _join_threads_bounded, threads, limit)

    async def latency_probe(self, url: str | None = None) -> float | None:
        """Замеряет задержку (сек) до RPC-эндпоинта. None при недоступности."""
        target = url or self.rpc_url
        try:
            w3 = self._probe_w3.get(target)
            if w3 is None:
                w3 = Web3(Web3.HTTPProvider(target, request_kwargs={"timeout": 10}))
                self._probe_w3[target] = w3
            t0 = time.monotonic()
            await self.run_in_executor(lambda: w3.eth.block_number)
            return time.monotonic() - t0
        except Exception:
            return None

    async def probe_all(self) -> list[tuple[str, float | None]]:
        """Замер задержки по всем настроенным RPC-эндпоинтам (параллельно)."""
        urls = self._rpc_urls
        results = await asyncio.gather(*[self.latency_probe(url) for url in urls], return_exceptions=True)
        return [(url, r if isinstance(r, float) else None) for url, r in zip(urls, results, strict=True)]

    def diagnostics(self, latency: list[float | None] | None = None) -> dict:
        """Сводка состояния RPC/метрик для экрана диагностики."""
        m = getattr(self, "_metrics", None)
        cb = getattr(self, "_circuit_breaker", None)
        state = getattr(cb, "_state", "CLOSED")
        failures = getattr(cb, "_failure_count", 0)
        calls = getattr(m, "calls", 0)
        errors = getattr(m, "errors", 0) if m else 0
        total = getattr(m, "total_time", 0.0) if m else 0.0
        return {
            "rpc_urls": self._rpc_urls,
            "active": self.rpc_url,
            "latency": latency,
            "avg_ms": (total / max(calls, 1)) * 1000 if calls else None,
            "errors": errors,
            "calls": calls,
            "cb_state": state,
            "cb_failures": failures,
            "rpc_rate": round(self._adaptive_rate(), 1),
            "chain_id": self.chain_id,
        }

    async def run_in_executor(self, fn: Callable[..., _T], *args: Any) -> _T:
        if not callable(fn):
            raise TypeError(f"run_in_executor: fn must be callable, got {type(fn).__name__}")
        # Адаптивная скорость: при деградации узла снижаем частоту вызовов плавно,
        # при восстановлении — возвращаем полную (все ядра/коннекты снова в работе).
        rate = self._adaptive_rate()
        if abs(rate - self._applied_rate) > 0.001:
            self._rate_limiter.set_rate(rate)
            self._applied_rate = rate
        await self._rate_limiter.acquire()
        t0 = time.monotonic()
        try:
            result = await asyncio.get_running_loop().run_in_executor(self._executor, fn, *args)
            self._metrics.record(time.monotonic() - t0)
            return result
        except Exception:
            self._metrics.record(time.monotonic() - t0, error=True)
            raise

    def _adaptive_rate(self) -> float:
        """Эффективная скорость RPC под здоровьем узла (обратная связь при сбоях)."""
        return _effective_rpc_rate(self._rpc_rate, self._metrics.health(), self._rate_floor)

    def concurrency_factor(self) -> float:
        """0.15..1.0 — во сколько раз снизить параллелизм по здоровью RPC.

        Когда узел деградирует, молотить его сотней воркеров контрпродуктивно
        (куча параллельных ретраев только усугубляет перегрузку). Пул читает
        этот множитель каждый чанк и размер семерфора выбирает динамически.
        """
        h = self._metrics.health()
        if h["samples"] == 0 or h["samples"] < 5:
            return 1.0
        return _effective_rpc_rate(1.0, h, self._rate_floor)

    def take_metrics(self) -> dict:
        """Снимает и сбрасывает RPC-метрики за цикл (для журнала цикла)."""
        return self._metrics.snapshot()

    async def _close_w3_provider(self, w3: Web3 | None) -> None:
        """Закрывает сокеты HTTP-провайдера. Идемпотентен."""
        provider = getattr(w3, "provider", None) if w3 is not None else None
        if provider is None:
            return
        for attr in ("_session", "_client"):
            session = getattr(provider, attr, None)
            if session is None:
                continue
            try:
                setattr(provider, attr, None)
            except Exception:
                pass
            close_method = getattr(session, "close", None)
            if close_method is None:
                continue
            try:
                result = close_method()
                if inspect.isawaitable(result):
                    await result
            except Exception as e:
                logger.warning(f"Закрытие RPC-сессии не удалось: {e}")

    async def _switch_rpc(self) -> None:
        """Переключается на следующий RPC-эндпоинт (если доступен)."""
        if self._rpc_lock is None:
            self._rpc_lock = asyncio.Lock()
        async with self._rpc_lock:
            if len(self._rpc_urls) <= 1:
                return
            self._rpc_index = (self._rpc_index + 1) % len(self._rpc_urls)
            new_url = self._rpc_urls[self._rpc_index]
            old_url = self.rpc_url
            if new_url == old_url:
                return
            # Сначала подключаемся к новому эндпоинту: если он не живой, старый
            # остаётся в рабочем состоянии, а состояние не «расщепляется».
            try:
                new_w3 = self._connect_rpc(new_url)
            except Exception as e:
                logger.error(f"Не удалось переключиться на {_redact_rpc_url(new_url) or new_url}: {e}")
                raise
            logger.warning(
                "RPC failover: %s -> %s",
                _redact_rpc_url(old_url) or old_url,
                _redact_rpc_url(new_url) or new_url,
            )
            old_w3 = self.w3
            self.rpc_url = new_url
            self.w3 = new_w3
            await self._close_w3_provider(old_w3)
            self._nonce_cache = _TTLCache(self._nonce_ttl)
            self._balance_cache = _TTLCache(self._balance_ttl)
            self._gas_price_cache = _TTLCache(self._gas_ttl)
            # Rate limiter: сохраняем дробь токенов, чтобы не дать burst на новом RPC
            old_limiter = self._rate_limiter
            self._rate_limiter = _RPCRateLimiter(self._rpc_rate)
            self._rate_limiter.recover_tokens(old_limiter)
            self._applied_rate = self._rpc_rate

    async def run_retry(self, fn: Callable[..., _T], *args: Any) -> _T:
        """Запускает синхронный fn через executor с ретраями, failover и circuit breaker.

        Ретраим только транспортные/инфраструктурные ошибки; логические
        (nonce, revert, нехватка средств) поднимаем сразу — они не исправятся
        повтором, а только разогреют circuit breaker и зажгут воркер впустую.
        """
        last_err: BaseException | None = None
        for attempt in range(_RPC_RETRIES):
            if not self._circuit_breaker.allow_request():
                logger.warning("Circuit breaker OPEN, skipping RPC call")
                raise last_err or ConnectionError("Circuit breaker OPEN")
            try:
                result = await self.run_in_executor(fn, *args)
                self._circuit_breaker.record_success()
                return result
            except Exception as e:
                last_err = e
                if not _is_retryable_error(e):
                    raise
                self._circuit_breaker.record_failure()
                await self._switch_rpc()
                if attempt < _RPC_RETRIES - 1:
                    base = min(2**attempt, 10)
                    jitter = random.uniform(base * 0.5, base * 1.5)
                    await asyncio.sleep(jitter)
        if last_err is not None:
            raise last_err
        raise ConnectionError("RPC failed after retries")

    def get_account(self, private_key: str) -> Account:
        # Кэш индексируем по АДРЕСУ, а не по приватному ключу: приватный ключ
        # не должен протекать в ключи dict (дамп памяти/crash dump легко
        # восстановил бы его как литерал в структуре данных).
        account = self.w3.eth.account.from_key(private_key)
        address = account.address.lower()
        existing = self._accounts.get(address)
        if existing is None:
            self._accounts[address] = account
            return account
        return existing

    async def get_balance(self, address: str, refresh: bool = False) -> float:
        """Баланс в ETH. refresh=True обходит TTL-кэш (важно сразу после транзакций/крана)."""
        address = Web3.to_checksum_address(address)
        if refresh:
            self._balance_cache.delete(address)
        cached = self._balance_cache.get(address)
        if cached is not None:
            return cached
        balance_wei = await self.run_retry(self.w3.eth.get_balance, address)
        balance = float(self.w3.from_wei(balance_wei, "ether"))
        self._balance_cache.set(address, balance)
        return balance

    async def claim_nonce(self, address: str) -> int:
        address = Web3.to_checksum_address(address)
        lock = self._nonce_locks.get(address)
        if lock is None:
            lock = self._nonce_locks[address] = asyncio.Lock()
        async with lock:
            network_nonce: int | None = self._nonce_cache.get(f"nw:{address}")
            if network_nonce is None:
                fetched_nonce = await self.run_retry(lambda: self.w3.eth.get_transaction_count(address, "pending"))
                network_nonce = int(fetched_nonce)
                self._nonce_cache.set(f"nw:{address}", network_nonce)
            db_nonce = await self._db.get_nonce(address)
            if db_nonce is not None and db_nonce > network_nonce:
                nonce = db_nonce
            else:
                nonce = network_nonce
            await self._db.set_nonce(address, nonce + 1)
            self._nonce_cache.set(f"nw:{address}", nonce + 1)
            return nonce

    async def release_nonce(self, address: str, nonce: int) -> None:
        """Возвращает nonce обратно, если он не был переиспользован другими заявками.

        Откатываем только когда наша заявка осталась последней (db == nonce+1);
        иначе два действующих nonce схлопнутся. Сбрасываем кэш — следующая
        заявка переспросит истинный pending-счётчик у RPC.
        """
        address = Web3.to_checksum_address(address)
        lock = self._nonce_locks.get(address)
        if lock is None:
            return
        async with lock:
            db_nonce = await self._db.get_nonce(address)
            if db_nonce is not None and db_nonce == nonce + 1:
                await self._db.set_nonce(address, nonce)
            self._nonce_cache.delete(f"nw:{address}")

    async def rollback_nonce_if_free(self, address: str, nonce: int) -> None:
        """Откатывает nonce, только если сеть его не заняла.

        После сбоя send_raw_transaction мы не знаем, ушёл ли tx в mempool
        (ошибка могла прийти уже после приёма). Уточняем по pending-счётчику:
        если он выше claimed nonce — nonce занят (другой заявкой или нашим tx),
        откат создал бы риск дубля/переиспользования. При недоступности RPC
        консервативно считаем nonce свободным — дыра хуже редкого повтора.
        """
        address = Web3.to_checksum_address(address)
        try:
            pending = await self.run_retry(lambda: self.w3.eth.get_transaction_count(address, "pending"))
        except Exception:
            pending = nonce
        if pending <= nonce:
            await self.release_nonce(address, nonce)

    async def get_gas_price(self) -> int:
        cached = self._gas_price_cache.get("gas_price")
        if cached is not None:
            return cached
        price = await self.run_retry(lambda: self.w3.eth.gas_price)
        self._gas_price_cache.set("gas_price", price)
        return price

    async def wait_for_receipt(self, tx_hash, timeout: int = 120) -> dict | None:
        """Ждёт ресипт с адаптивным поллингом и поддержкой отмены."""
        start = time.monotonic()
        deadline = start + timeout
        # Ресипт обычно приходит в первые секунды — поллим часто, а после
        # длительного ожидания снижаем частоту, чтобы не жечь RPC-лимит.
        poll_interval = 0.3
        warned_once = False
        try:
            while True:
                try:
                    receipt = await self.run_in_executor(lambda: self.w3.eth.get_transaction_receipt(tx_hash))
                    if receipt is not None:
                        return receipt
                except Exception as e:
                    if not warned_once:
                        logger.warning(f"wait_for_receipt: ошибка RPC при поллинге (продолжаю): {e}")
                        warned_once = True
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"wait_for_receipt timeout ({timeout}s) for tx")
                await asyncio.sleep(poll_interval)
                elapsed = time.monotonic() - start
                if elapsed > 60:
                    poll_interval = 5.0
                elif elapsed > 30:
                    poll_interval = 2.0
                elif elapsed > 10:
                    poll_interval = 1.0
        except asyncio.CancelledError:
            logger.debug(f"wait_for_receipt cancelled for {tx_hash.hex()[:10]}")
            raise

    async def send_raw_transaction(self, raw_tx) -> bytes:
        return await self.run_retry(lambda: self.w3.eth.send_raw_transaction(raw_tx))

    async def send_transfer(
        self,
        private_key: str,
        to_address: str,
        amount_eth: float,
        gas_limit: int = 21000,
        gas_price_mult: float = 1.0,
    ) -> str | None:
        sent = False
        nonce = None
        account = None
        try:
            account = self.get_account(private_key)
            to_address = Web3.to_checksum_address(to_address)
            nonce = await self.claim_nonce(account.address)
            gas_price = int(await self.get_gas_price() * gas_price_mult)
            tx = {
                "nonce": nonce,
                "to": to_address,
                "value": self.w3.to_wei(amount_eth, "ether"),
                "gas": gas_limit,
                "gasPrice": gas_price,
                "chainId": self.chain_id,
            }
            signed = account.sign_transaction(tx)
            raw = signed.raw_transaction
            tx_hash = await self.send_raw_transaction(raw)
            sent = True
            receipt = await self.wait_for_receipt(tx_hash)
            if receipt is not None and receipt.get("status") == 1:
                self.invalidate_balance(account.address)
                return tx_hash.hex()
            logger.warning(f"Transfer reverted: tx={tx_hash.hex()}")
            self.invalidate_balance(account.address)
            await self.release_nonce(account.address, nonce)
            return None
        except Exception as e:
            logger.error(f"Transfer error: {e}")
            # Гарантированно не отправлена — возвращаем nonce, чтобы не жечь дыры.
            # При таймауте ресипта (tx, возможно, в mempool) nonce НЕ трогаем.
            if nonce is not None and not sent and account is not None:
                try:
                    await self.rollback_nonce_if_free(account.address, nonce)
                except Exception:
                    pass
            return None
