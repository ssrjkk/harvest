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

_RPC_MONITOR_INTERVAL = 30.0
_PING_TIMEOUT_MS = 2500.0
# Серия ошибок поллинга ресипта, после которой нода считается упавшей:
# переключаемся на другой эндпоинт и продолжаем ждать (поллинг идемпотентен).
_RPC_POLL_FAILOVER_THRESHOLD = 3

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
        if self._state == "HALF_OPEN":
            # Пробный вызов в HALF_OPEN провалился — нода ещё не восстановилась.
            # Возвращаемся в OPEN, чтобы после следующего recovery_timeout окна
            # снова войти в HALF_OPEN и дать новый пробный запрос. Иначе breaker
            # навсегда застревает в HALF_OPEN (единственный пробник исчерпан и
            # allow_request() до конца жизни процесса возвращает False).
            self._state = "OPEN"
            logger.warning(
                f"Circuit breaker HALF_OPEN trial failed, back to OPEN (next trial in {self._recovery_timeout}s)"
            )
        elif self._failure_count >= self._failure_threshold:
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

    def reset(self) -> None:
        """Возвращает breaker в CLOSED (используется при смене RPC-эндпоинта).

        Счётчик ошибок относится к текущему эндпоинту: после failover новый
        узел не должен наследовать OPEN-состояние старого (иначе здоровый
        эндпоинт остаётся заблокированным до recovery_timeout).
        """
        self._state = "CLOSED"
        self._failure_count = 0
        self._half_open_attempted = False

    def log_status(self) -> None:
        if self._state != "CLOSED":
            logger.info(f"Circuit breaker: state={self._state}, failures={self._failure_count}")


class RpcNode:
    """Оценка одного RPC-эндпоинта EMA-задержкой и долей ошибок.

    Хорошая нода — низкий score. Ошибки штрафуются множителем, серия из более
    трёх провалов помечает ноду мёртвой (исключается из ротации). Один успешный
    пинг снимает флаг (zombie-воскрешение) — нода прозрачно возвращается.
    """

    __slots__ = ("url", "latency_ms", "errors", "successes", "is_dead")

    _ZOMBIE_THRESHOLD = 3

    def __init__(self, url: str) -> None:
        self.url = url
        self.latency_ms: float = 500.0  # пессимистичный старт до первого замера
        self.errors: int = 0
        self.successes: int = 1
        self.is_dead: bool = False

    @property
    def score(self) -> float:
        if self.is_dead:
            return float("inf")
        error_rate = self.errors / max(1, self.errors + self.successes)
        return self.latency_ms * (1.0 + error_rate * 5.0)

    def record(self, latency_ms: float, success: bool) -> None:
        # EMA сглаживает спайки: 70% истории + 30% свежий замер.
        self.latency_ms = self.latency_ms * 0.7 + latency_ms * 0.3
        if success:
            self.successes = min(100, self.successes + 1)
            self.errors = max(0, self.errors - 1)
            # Zombie-воскрешение: один успешный пинг возвращает ноду в ротацию,
            # счётчик ошибок «прощается» (деградация была временной).
            self.is_dead = False
        else:
            self.errors = min(100, self.errors + 1)
            self.successes = max(0, self.successes - 1)
            if self.errors > self._ZOMBIE_THRESHOLD:
                self.is_dead = True


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
        # Оценка каждой ноды (EMA): используется для выбора лучшего эндпоинта
        # и урезания параллелизма (concurrency_factor) при деградации.
        self._nodes: list[RpcNode] = [RpcNode(url) for url in self._rpc_urls]
        self._monitor_task: asyncio.Task | None = None
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
        # EIP-1559: capability probe один раз, fee из feeHistory кэшируем как cash-бasis.
        # None → ещё не пробовали; True/False → поддержка сети (не rescan кэша).
        # Рубильник advanced.eip1559 (по умолчанию True): если на конкретной ноде
        # нестандартный gas-market даёт сбои 1559-полей — выключаем одной настройкой,
        # не правкой кода. False → ровно legacy-поведение, nonce не трогает.
        self._eip1559_capability: bool | None = None
        self._eip1559_enabled = bool(config.get("advanced", {}).get("eip1559", True))
        self._gas_priority_cache = _TTLCache(self._gas_ttl * 4)
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
        # Бюджет ожидания ресипта: воркер не должен висеть до 120с на медленной
        # сети/мёртвой трансляции — превышение = tx «в mempool», статус не сбой.
        self._receipt_timeout = float(config.get("advanced", {}).get("receipt_timeout", 30))

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
        # Владельцев ресурса несколько (пул, экраны, doctor): close идемпотентен,
        # повторный вызов ничего не делает (executor нельзя shutdown дважды).
        if getattr(self, "_closed", False):
            return
        self._closed = True
        if self._monitor_task is not None:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except (asyncio.CancelledError, Exception):
                pass
            self._monitor_task = None
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
        """Замеряет задержку (сек) до RPC-эндпоинта. None при недоступности.

        Проба — диагностика, а не бизнес-трафик: идёт мимо rate-limiter и
        метрик (не тратит токены, не искажает calls/avg_ms в diagnostics).
        """
        target = url or self.rpc_url
        try:
            w3 = self._probe_w3.get(target)
            if w3 is None:
                w3 = Web3(Web3.HTTPProvider(target, request_kwargs={"timeout": 10}))
                self._probe_w3[target] = w3
            t0 = time.monotonic()
            await asyncio.get_running_loop().run_in_executor(self._executor, lambda: w3.eth.block_number)
            return time.monotonic() - t0
        except Exception:
            return None

    async def probe_all(self) -> list[tuple[str, float | None]]:
        """Замер задержки по всем настроенным RPC-эндпоинтам (параллельно)."""
        urls = self._rpc_urls
        results = await asyncio.gather(*[self.latency_probe(url) for url in urls], return_exceptions=True)
        return [(url, r if isinstance(r, float) else None) for url, r in zip(urls, results, strict=True)]

    # ---------------- оценка нод и выбор лучшего RPC ----------------

    def _record_latency(self, url: str, latency_sec: float | None) -> None:
        """Пишет свежий замер в EMA-оценку ноды (None = сбой/таймаут)."""
        for node in self._nodes:
            if node.url == url:
                if latency_sec is None:
                    node.record(_PING_TIMEOUT_MS, success=False)
                else:
                    node.record(latency_sec * 1000.0, success=True)
                return

    def best_rpc_url(self) -> str:
        """URL лучшей живой ноды по скору. Если все мертвы — «наименее лагавший»."""
        alive = [n for n in self._nodes if not n.is_dead]
        pool = alive if alive else self._nodes
        best = min(pool, key=lambda n: n.score)
        return best.url

    def node_health(self) -> list[dict]:
        """Состояние всех эндпоинтов: адрес, задержка, скор, жив/мёртв (для UI)."""
        return [
            {
                "url": _redact_rpc_url(n.url) or n.url,
                "latency_ms": round(n.latency_ms, 1),
                "score": None if n.is_dead else round(n.score, 1),
                "dead": n.is_dead,
                "errors": n.errors,
            }
            for n in self._nodes
        ]

    def _node_poll_degraded(self) -> bool:
        """EMA-здоровье активной ноды для регулировки частоты поллинга.

        True, если текущая нода мертва, накопила лишние ошибки подряд или
        лагает сверх пинг-таймаута — тогда wait_for_receipt опрашивает её
        реже (экономит RPC-лимит и не досыпает спрос на больную ноду).
        """
        for node in self._nodes:
            if node.url == self.rpc_url:
                if node.is_dead:
                    return True
                if node.errors >= _RPC_POLL_FAILOVER_THRESHOLD:
                    return True
                return node.latency_ms > _PING_TIMEOUT_MS
        return False

    async def _monitor_rpc(self) -> None:
        """Фоновый пинг всех нод; худшая выбывает из ротации, лучшая выбирается.

        Пинг `eth_blockNumber` (лёгкий) раз в _RPC_MONITOR_INTERVAL сек. Мёртвая
        нода получает шанс воскреснуть при первом успехе; EMT-оценка не даёт
        ротатору дёргать URL из-за единичного таймаута.
        """
        while True:
            try:
                if self._nodes:
                    probes = await self.probe_all()
                    for url, latency in probes:
                        self._record_latency(url, latency)
                    best = self.best_rpc_url()
                    if best != self.rpc_url and len(self._rpc_urls) > 1:
                        logger.warning(
                            "RPC ротация по здоровью: %s -> %s",
                            _redact_rpc_url(self.rpc_url) or self.rpc_url,
                            _redact_rpc_url(best) or best,
                        )
                        try:
                            await self._switch_to(best)
                        except Exception as e:
                            logger.error(f"Ротация на лучший RPC не удалась: {e}")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Монитор RPC: {e}")
            await asyncio.sleep(_RPC_MONITOR_INTERVAL)

    async def _monitor_supervisor(self) -> None:
        """Сторож фонового монитора RPC.

        Если _monitor_rpc внезапно завершится (баг вне его try-блока, будущий
        рефакторинг, неожиданный возврат) — перезапускаем с экспоненциальным
        backoff, чтобы ротация по здоровью и накопление задержек не вымерли
        сами по себе. Отмена (close) прокидывается наверх без перезапуска,
        вложенная задача гасится, чтобы не остаться сиротой.
        """
        backoff = 1.0
        while True:
            task = asyncio.create_task(self._monitor_rpc())
            try:
                await task
                logger.critical(f"Монитор RPC неожиданно завершился; перезапуск через {backoff:.1f}с")
            except asyncio.CancelledError:
                task.cancel()
                # Дожидаемся завершения вложенной задачи, чтобы не оставить её
                # «pending-сиротой» при закрытии event loop (Task was destroyed..).
                await asyncio.gather(task, return_exceptions=True)
                raise
            except Exception as e:
                logger.critical(f"Монитор RPC умер ({e}); перезапуск через {backoff:.1f}с")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _RPC_MONITOR_INTERVAL)

    def monitor_started(self) -> bool:
        return self._monitor_task is not None and not self._monitor_task.done()

    def start_monitor(self) -> None:
        """Запускает фоновый монитор здоровья RPC (идемпотентно).

        С одним эндпоинтом монитор бессмыслен (не из кого выбирать) — не жжём
        RPC-лимит пингами; деградацию единственной ноды всё равно ловят
        метрики через concurrency_factor (адаптивная ставка/параллелизм).
        """
        if len(self._rpc_urls) <= 1:
            return
        if self.monitor_started():
            return
        self._monitor_task = asyncio.create_task(self._monitor_supervisor())

    def diagnostics(self, latency: list[float | None] | None = None) -> dict:
        """Сводка состояния RPC/метрик для экрана диагностики.

        URL-ы отдаются redacted (без credentials/query-token): этот словарь
        печатается в UI и может уйти в логи/панели — сырой URL с API-ключом
        не должен покидать менеджер.
        """
        m = getattr(self, "_metrics", None)
        cb = getattr(self, "_circuit_breaker", None)
        state = getattr(cb, "_state", "CLOSED")
        failures = getattr(cb, "_failure_count", 0)
        calls = getattr(m, "calls", 0)
        errors = getattr(m, "errors", 0) if m else 0
        total = getattr(m, "total_time", 0.0) if m else 0.0
        return {
            "rpc_urls": [_redact_rpc_url(u) or u for u in self._rpc_urls],
            "active": _redact_rpc_url(self.rpc_url) or self.rpc_url,
            "latency": latency,
            "nodes": self.node_health(),
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
        # Фиксируем активный эндпоинт ДО вызова: пока корутина спит в executor,
        # фоновый монитор/другой воркер может сделать failover, и свежая EMA
        # должна лечь в копилку НАШЕЙ ноды, а не сменившейся в это время.
        node_url = self.rpc_url
        try:
            result = await asyncio.get_running_loop().run_in_executor(self._executor, fn, *args)
            elapsed = time.monotonic() - t0
            self._metrics.record(elapsed)
            # Успешный вызов — свежий сигнал здоровья активной ноды: обновляет
            # EMA-латентность и «воскрешает» мёртвую ноду первым же успехом
            # (работает даже без фонового монитора, напр. с одним эндпоинтом).
            self._record_latency(node_url, elapsed)
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
        этот множитель каждый чанк и размер воркер-пула выбирает динамически.
        Учитываем и EMA-оценку нод: мёртвые/заторможенные эндпоинты режут
        параллелизм, пока не подтянется metric (или не воскреснет нода).
        """
        # Нода-победитель по EMA: её задержка — реальный потолок сегодня.
        if self._nodes:
            best = min(self._nodes, key=lambda n: (n.is_dead, n.score))
            node_ms = best.latency_ms if not best.is_dead else float("inf")
            if node_ms >= 2000:
                return 0.3
            if node_ms >= 1000:
                return 0.5
        h = self._metrics.health()
        if h["samples"] < 5:
            return 1.0
        return _effective_rpc_rate(1.0, h, self._rate_floor)

    def take_metrics(self) -> dict:
        """Снимает и сбрасывает RPC-метрики за цикл (для журнала цикла)."""
        return self._metrics.snapshot()

    async def _close_session(self, session: object) -> None:
        """Закрывает один HTTP-клиент (httpx.Client/AsyncClient и legacy)."""
        close_method = getattr(session, "close", None)
        if close_method is None:
            return
        try:
            result = close_method()
            if inspect.isawaitable(result):
                await result
        except Exception as e:
            logger.warning(f"Закрытие RPC-сессии не удалось: {e}")

    async def _close_w3_provider(self, w3: Web3 | None) -> None:
        """Закрывает сокеты HTTP-провайдера. Идемпотентен.

        web3>=7 перенёс сессии в HTTPProvider._request_session_manager
        (HTTPSessionManager.session_cache / _explicit_session) — без их закрытия
        keep-alive TCP/TLS-сокеты старого эндпоинта висели до GC, плодя дескрипторы
        на каждом failover. Для старых версий держим legacy-ветку (_session/_client).
        """
        provider = getattr(w3, "provider", None) if w3 is not None else None
        if provider is None:
            return
        mgr = getattr(provider, "_request_session_manager", None)
        if mgr is not None:
            cache = getattr(mgr, "session_cache", None)
            items = getattr(cache, "items", None)
            if items is not None:
                try:
                    for _endpoint, session in list(items()):
                        await self._close_session(session)
                except Exception as e:
                    logger.warning(f"Закрытие сессий RPC-кэша не удалось: {e}")
            explicit = getattr(mgr, "_explicit_session", None)
            if explicit is not None:
                await self._close_session(explicit)
            return
        for attr in ("_session", "_client"):
            session = getattr(provider, attr, None)
            if session is None:
                continue
            try:
                setattr(provider, attr, None)
            except Exception:
                pass
            await self._close_session(session)

    async def _switch_to(self, new_url: str) -> None:
        """Переключается на конкретный RPC-эндпоинт (общая логика failover/ротации)."""
        if self._rpc_lock is None:
            self._rpc_lock = asyncio.Lock()
        async with self._rpc_lock:
            if new_url == self.rpc_url:
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
                _redact_rpc_url(self.rpc_url) or self.rpc_url,
                _redact_rpc_url(new_url) or new_url,
            )
            old_w3 = self.w3
            self.rpc_url = new_url
            self.w3 = new_w3
            try:
                self._rpc_index = self._rpc_urls.index(new_url)
            except ValueError:
                pass
            # Circuit breaker считает ошибки ТЕКУЩЕГО эндпоинта: новый узел
            # стартует с чистого окна (иначе OPEN старого узла блокировал бы
            # и здоровый новый до recovery_timeout).
            self._circuit_breaker.reset()
            await self._close_w3_provider(old_w3)
            self._nonce_cache = _TTLCache(self._nonce_ttl)
            self._balance_cache = _TTLCache(self._balance_ttl)
            self._gas_price_cache = _TTLCache(self._gas_ttl)
            # Rate limiter: сохраняем дробь токенов, чтобы не дать burst на новом RPC
            old_limiter = self._rate_limiter
            self._rate_limiter = _RPCRateLimiter(self._rpc_rate)
            self._rate_limiter.recover_tokens(old_limiter)
            self._applied_rate = self._rpc_rate

    async def _switch_rpc(self) -> None:
        """Переключается на следующий RPC-эндпоинт (аварийный failover)."""
        if len(self._rpc_urls) <= 1:
            return
        next_url = self._rpc_urls[(self._rpc_index + 1) % len(self._rpc_urls)]
        if next_url == self.rpc_url:
            return
        await self._switch_to(next_url)

    async def run_retry(self, fn: Callable[..., _T], *args: Any) -> _T:
        """Запускает синхронный fn через executor с ретраями, failover и circuit breaker.

        Ретраим только транспортные/инфраструктурные ошибки; логические
        (nonce, revert, нехватка средств) поднимаем сразу — они не исправятся
        повтором, а только разогреют circuit breaker и зажгут воркер впустую.
        """
        last_err: BaseException | None = None
        for attempt in range(_RPC_RETRIES):
            if not self._circuit_breaker.allow_request():
                # Breaker OPEN: текущая нода стабильно падает. Если есть запасной
                # эндпоинт — сразу failover (а не стоим recovery_timeout впустую
                # и не режем каждый кошелёк цикла до результата 0).
                # _switch_to сбрасывает breaker — следующий круг потенциально
                # разрешён. Гарантируем прогресс: только когда реально сменили
                # эндпоинт (иначе best==current и крутимся бесконечно).
                if len(self._rpc_urls) > 1:
                    best = self.best_rpc_url()
                    if best != self.rpc_url:
                        await self._switch_to(best)
                        continue
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
                # Реальная ошибка текущего эндпоинта: штрафуем EMA-оценку ноды,
                # чтобы ротатор по здоровью увидел деградацию раньше следующего пинга.
                self._record_latency(self.rpc_url, None)
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
        balance_wei = await self.run_retry(lambda: self.w3.eth.get_balance(address))
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
        """Откатывает nonce, только если сеть его точно не заняла.

        После сбоя send_raw_transaction мы не знаем, ушёл ли tx в mempool
        (ошибка могла прийти уже после приёма). Уточняем по pending-счётчику:
        если он выше claimed nonce — nonce занят (другой заявкой или нашим tx),
        откат создал бы риск дубля/переиспользования. При недоступности RPC
        перевод НЕ освобождаем: повторная выдача того же nonce РАЗНЫМ
        транзакциям (коллизия) — тихая потеря средств, а дыра в последовательности
        не беспокоит: каждый nonce выдаётся один раз.
        """
        address = Web3.to_checksum_address(address)
        try:
            pending = await self.run_retry(lambda: self.w3.eth.get_transaction_count(address, "pending"))
        except Exception:
            return
        if pending <= nonce:
            await self.release_nonce(address, nonce)

    async def get_gas_price(self) -> int:
        cached = self._gas_price_cache.get("gas_price")
        if cached is not None:
            return cached
        price = await self.run_retry(lambda: self.w3.eth.gas_price)
        self._gas_price_cache.set("gas_price", price)
        return price

    # ---------------- EIP-1559 fee market (авто-детект с graceful fallback) ----------------

    async def get_fee_basis(
        self, gas_price_mult: float, priority_mult: float = 1.0
    ) -> dict | None:
        """Возвращает 1559-поля для tx: maxFeePerGas/maxPriorityFeePerGas.

        Авто-детект: при первом вызове пробуем eth_feeHistory + baseFeePerGas.
        Если сеть поддерживает 1559 — отдаём (max_fee, max_priority) с EMA-истории;
        иначе None → caller использует legacy gasPrice (ровно прежнее поведение).
        Capability кэшируется, чтобы probe не повторился на каждый tx.
        Nonce не затрагивается: меняются только поля газа.

        gas_price_mult — множитель к market fee (аналог gas_price_mult legacy).
        priority_mult — множитель к maxPriorityFeePerGas.
        """
        if not self._eip1559_enabled:
            # Рубильник advanced.eip1559=False → не пробуем 1559 вовсе,
            # legacy gasPrice ровно как раньше (nonce не трогает).
            return None
        if self._eip1559_capability is False:
            return None
        if self._gas_priority_cache.get("fee_basis") is not None:
            cached = self._gas_priority_cache.get("fee_basis")
            if cached is not None:
                max_fee, max_priority = cached
                return self._build_1559_fields(max_fee, max_priority, gas_price_mult, priority_mult)

        try:
            # Запрашиваем последние 2 блока — хватает для base_fee и priority
            fee_history = await self.run_retry(
                lambda: self.w3.eth.fee_history(2, "latest", [50.0])
            )
            base_fees = fee_history.get("baseFeePerGas", [])
            if not base_fees:
                raise ValueError("feeHistory не вернул baseFeePerGas")
            base_fee = int(max(base_fees))
            # Приоритет-плата: максимальная из истории (rewards) либо 1 gwei
            rewards = fee_history.get("reward", [])
            flat = [float(r[0]) for r in rewards if r and r[0] is not None] if rewards else []
            max_priority = int(max(flat)) if flat else Web3.to_wei(1, "gwei")
            max_priority = max(max_priority, Web3.to_wei(1, "gwei"))
            self._eip1559_capability = True
            self._gas_priority_cache.set("fee_basis", (base_fee, max_priority))
            return self._build_1559_fields(base_fee, max_priority, gas_price_mult, priority_mult)
        except Exception as e:
            logger.debug(f"EIP-1559 недоступен (legacy fallback): {e}")
            self._eip1559_capability = False
            return None

    def _build_1559_fields(
        self, base_fee: int, max_priority: int, gas_price_mult: float, priority_mult: float
    ) -> dict:
        max_fee = int((base_fee + max_priority) * gas_price_mult) + max_priority
        return {
            "maxFeePerGas": max_fee,
            "maxPriorityFeePerGas": int(max_priority * priority_mult),
        }

    def _secondary_live_node(self) -> RpcNode | None:
        """Лучшая ЖИВАЯ неактивная нода для read-only кросс-чека (None если одна)."""
        if len(self._rpc_urls) <= 1:
            return None
        others = [n for n in self._nodes if n.url != self.rpc_url and not n.is_dead]
        if not others:
            return None
        return min(others, key=lambda n: n.score)

    async def _crosscheck_receipt_status(
        self, tx_hash, primary_status: int
    ) -> int:
        """Пере-спрашивает ресипт на независимой вторичной ноде (read-only).

        Вызывается ТОЛЬКО на «дорогом» решении — когда primary вернула
        не-success (revert) или сама деградировала. Одна лживая/форкнутая нода
        не должна решать судьбу заявки: читаем статус с живой второй ноды.
        Не консенсус на happy-path — здоровый ресипт (status=1) не тратит
        второй RPC-вызов и не штрафует EMA.

        Возвращает консервативный итог: revert любой из нод перевешивает
        успех (дыра хуже редкого ложного отрицания). Nonce/бронь не
        затрагиваются — это чистое чтение из блокчейна.
        """
        secondary = self._secondary_live_node()
        if secondary is None:
            # Один эндпоинт — кросс-чек невозможен, доверяем тому, что есть
            # (ровно прежнее поведение, failover тут ни при чём).
            return primary_status
        try:
            t0 = time.monotonic()
            sec_w3 = self._connect_rpc(secondary.url)
            try:
                raw = await self.run_in_executor(
                    lambda: sec_w3.eth.get_transaction_receipt(tx_hash)
                )
            finally:
                await self._close_w3_provider(sec_w3)
            if raw is None:
                # Переголосовавшая нода ресипт ещё не видит — не доверяем
                # её «нет» на неопределённость: revert primary остаётся.
                self._record_latency(secondary.url, None)
                return primary_status
            sec_status = raw.get("status")
            latency = time.monotonic() - t0
            self._record_latency(secondary.url, latency)
            if sec_status == primary_status:
                return primary_status
            # Расхождение: одна из нод врёт/форкнута. Консервативно —
            # revert ЛЮБОЙ из нод перевешивает успех (контракт функции:
            # «дыра хуже редкого ложного отрицания»). Штрафуем узел,
            # показавший успех вопреки реверту.
            logger.warning(
                f"Кросс-чек ресипта: status расходятся (primary={primary_status}, "
                f"secondary={sec_status}, tx={tx_hash.hex()[:10]}) — консервативно revert"
            )
            if sec_status == 0:
                self._record_latency(self.rpc_url, None)
            else:
                # primary показал реверт, secondary — успех: первичная могла
                # выдать несуществующий успех на растущих незавершённых блоках —
                # штрафуем именно её (она остаётся активной для последующего трафика).
                self._record_latency(secondary.url, None)
            return 0
        except Exception as e:
            logger.debug(f"Кросс-чек ресипта не удался (вторичная): {e}")
            return primary_status

    async def wait_for_receipt(
        self, tx_hash: bytes, timeout: float | None = None
    ) -> dict | None:
        """Ждёт ресипт с адаптивным поллингом и поддержкой отмены.

        timeout=None → advanced.receipt_timeout (по умолчанию 30с). По
        истечении бюджета возвращает None вместо TimeoutError — транзакция
        принята в mempool, ресипт просто ещё не пришёл (медленная сеть):
        воркер не должен простаивать в 120-сек ступоре. Ресипт со status=0
        (reverted) вернётся как есть — разбор в send_transfer.
        """
        budget = timeout if timeout is not None else self._receipt_timeout
        start = time.monotonic()
        deadline = start + budget
        # Ресипт обычно приходит в первые секунды — поллим часто, а после
        # длительного ожидания снижаем частоту, чтобы не жечь RPC-лимит.
        poll_interval = 0.5
        warned_once = False
        consecutive_poll_errors = 0
        try:
            while True:
                try:
                    receipt = await self.run_in_executor(lambda: self.w3.eth.get_transaction_receipt(tx_hash))
                    if receipt is not None:
                        return receipt
                    consecutive_poll_errors = 0
                except Exception as e:
                    # Штрафуем EMA ноды: пусть ротатор/адаптивная ставка увидят
                    # деградацию раньше следующего пинга монитора (30с).
                    self._record_latency(self.rpc_url, None)
                    consecutive_poll_errors += 1
                    if not warned_once:
                        logger.warning(f"wait_for_receipt: ошибка RPC при поллинге (продолжаю): {e}")
                        warned_once = True
                    # Автохил: нода молчит на поллинге — переключаемся на другую.
                    # Иначе кошелёк простаивает до полного таймаута (120с), а
                    # поллинг get_transaction_receipt идемпотентен (повтор любого
                    # эндпоинта безопасен — ресипт читается из блокчейна, не из ноды).
                    if consecutive_poll_errors >= _RPC_POLL_FAILOVER_THRESHOLD:
                        if len(self._rpc_urls) > 1:
                            logger.warning(
                                "wait_for_receipt: %s ошибок поллинга подряд — RPC failover",
                                consecutive_poll_errors,
                            )
                        consecutive_poll_errors = 0
                        try:
                            await self._switch_rpc()
                        except Exception as switch_err:  # noqa: BLE001
                            logger.warning(f"wait_for_receipt: не удалось переключить RPC: {switch_err}")
                if time.monotonic() >= deadline:
                    logger.warning(
                        f"wait_for_receipt: ресипт не пришёл за {budget:.0f}с (tx={tx_hash.hex()[:10]}) — "
                        "считаем принятым в mempool"
                    )
                    return None
                await asyncio.sleep(poll_interval)
                elapsed = time.monotonic() - start
                # EMA-здоровье ноды правит ЖАДНОСТЬ поллинга: больная нода
                # опрашивается реже (экономит RPC-лимит, не досыпает спрос), а
                # здоровая — часто (ресипт приходит в первые секунды). Это
                # замыкает общий с наградой бюджет: тот. же EMA видит и старая
                # latency-призма, но здесь мы тратим не паралеллизм, а частоту.
                if self._node_poll_degraded():
                    # Сливают EMA-штраф мгновенно: не ждём elapsed>60, как раньше
                    poll_interval = max(poll_interval, 5.0)
                elif elapsed > 60:
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
        data_hex: str | None = None,
    ) -> str | None:
        sent = False
        nonce = None
        account = None
        try:
            account = self.get_account(private_key)
            to_address = Web3.to_checksum_address(to_address)
            nonce = await self.claim_nonce(account.address)
            legacy_gas = int(await self.get_gas_price() * gas_price_mult)
            # EIP-1559: dict с maxFeePerGas/maxPriorityFeePerGas при поддержке сетью,
            # иначе None → legacy gasPrice ровно как раньше (graceful fallback, nonce не трогает)
            fee_1559 = await self.get_fee_basis(gas_price_mult)
            fee_fields = fee_1559 if fee_1559 else {"gasPrice": legacy_gas}
            tx = {
                "nonce": nonce,
                "to": to_address,
                "value": self.w3.to_wei(amount_eth, "ether"),
                "gas": gas_limit,
                "chainId": self.chain_id,
                **fee_fields,
            }
            if data_hex:
                tx["data"] = data_hex
            signed = account.sign_transaction(tx)
            raw = signed.raw_transaction
            tx_hash = await self.send_raw_transaction(raw)
            sent = True
            receipt = await self.wait_for_receipt(tx_hash, timeout=self._receipt_timeout)
            if receipt is None:
                # Не дождались ресипта в пределах бюджета: tx принят в mempool,
                # nonce занят (не откатываем — он может уйти в сеть позже).
                logger.warning(f"Transfer unconfirmed: tx={tx_hash.hex()} (в mempool, нет ресипта)")
                return None
            # Дорогой исход: первичная нода говорит «реверт». Одно подозрительное
            # показание не должно решать судьбу заявки — пере-спрашиваем ресипт
            # на независимой вторичной ноде (read-only). Консервативная логика
            # _crosscheck_receipt_status: revert ЛЮБОЙ ноды перевешивает успех
            # (дыра хуже редкого ложного отрицания: нефинансовый результат
            # действия не требует повторной отправки, а false-success мог бы).
            # Nonce не трогаем.
            primary_status = int(receipt.get("status", 0))
            final_status = await self._crosscheck_receipt_status(tx_hash, primary_status)
            if final_status != 1:
                logger.warning(f"Transfer reverted: tx={tx_hash.hex()}")
                self.invalidate_balance(account.address)
                await self.release_nonce(account.address, nonce)
                return None
            self.invalidate_balance(account.address)
            return tx_hash.hex()
        except asyncio.CancelledError:
            # Отмена (таймаут кошелька, стоп пула): транзакция могла как уйти в сеть,
            # так и остаться неподписанной. Инвалидируем баланс (следующий отчёт
            # прочитает его заново), а nonce откатываем только через
            # rollback_nonce_if_free — при недоступности RPC он НЕ освободит nonce
            # (иначе один и тот же nonce ушёл бы двум разным транзакциям).
            if account is not None:
                self.invalidate_balance(account.address)
                if nonce is not None and not sent:
                    try:
                        await self.rollback_nonce_if_free(account.address, nonce)
                    except Exception:  # noqa: BLE001
                        pass
            raise
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
