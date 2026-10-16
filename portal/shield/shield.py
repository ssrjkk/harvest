"""Фасад защитного контура: композиция движков + фоновый сборщик мусора.

`Shield` — единая точка: конфиг, RiskEngine, HierarchicalLimiter,
IdempotencyStore, PayloadGuard, AuditLog, реестр CircuitBreaker'ов.
`start()` запускает фоновую TTL-сборку, `stop()` останавливает.

Fail-open: при `enabled=False` все проверки пропускают (один warn в лог),
сервис продолжает работать — защита никогда не роняет прод с 500.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from .audit import AuditLog
from .breaker import CircuitBreaker
from .config import BreakerConfig, GuardConfig
from .idem import IdempotencyStore, make_key
from .limits import HierarchicalLimiter
from .risk import Observation, RiskEngine
from .sanitize import PayloadGuard
from .store import AsyncStore, MemoryStore

logger = logging.getLogger(__name__)


class Shield:
    """Композиция защитных движков с общим жизненным циклом."""

    def __init__(self, config: GuardConfig | None = None,
                 store: AsyncStore | None = None) -> None:
        self.config = config or GuardConfig()
        self.config.validate()
        self._store = store or MemoryStore()
        self.risk = RiskEngine(self.config.risk, self._store)
        self.limits = HierarchicalLimiter(self.config.limits, self._store)
        self.idem = IdempotencyStore(self.config.idem, self._store)
        self.sanitize = PayloadGuard(self.config.sanitize)
        self.audit = AuditLog(ring=self.config.audit_ring)
        self._breakers: dict[str, CircuitBreaker] = {}
        self._breaker_cfg = BreakerConfig(
            failure_threshold=self.config.breaker.failure_threshold,
            recovery_timeout_s=self.config.breaker.recovery_timeout_s,
            half_open_probes=self.config.breaker.half_open_probes,
            retry_attempts=self.config.breaker.retry_attempts,
            backoff_base_s=self.config.breaker.backoff_base_s,
            backoff_cap_s=self.config.breaker.backoff_cap_s,
        )
        self._evict_task: asyncio.Task | None = None
        self._stopping = False
        if not self.config.enabled:
            logger.warning("shield отключён конфигом (fail-open): проверки пропускаются")

    @classmethod
    def from_env(cls, prefix: str = "PORTAL_GUARD_") -> Shield:
        return cls(GuardConfig.from_env(prefix))

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    def breaker(self, name: str) -> CircuitBreaker:
        """Именованный брейкер downstream-ресурса (создаётся лениво)."""
        b = self._breakers.get(name)
        if b is None:
            b = CircuitBreaker(self._breaker_cfg, name=name)
            self._breakers[name] = b
        return b

    def idem_key(self, actor: str, action: str, payload: bytes | str) -> str:
        return make_key(actor, action, payload, self.config.idem.time_bucket_s)

    async def observe(self, subject: str, obs: Observation | None = None):
        """Прокси в RiskEngine.observe (с учётом отключения)."""
        if not self.config.enabled:
            from .risk import LOW, Verdict

            return Verdict(level=LOW, score=0.0), []
        return await self.risk.observe(subject, obs)

    async def audit_event(self, kind: str, subject: str = "-",
                          details: dict[str, Any] | None = None) -> None:
        await self.audit.event(kind, subject, details)

    async def start(self) -> None:
        """Запустить фоновую TTL-сборку мусора."""
        if self._evict_task is not None or self._stopping:
            return
        self._evict_task = asyncio.create_task(self._evict_loop())

    async def stop(self) -> None:
        self._stopping = True
        task, self._evict_task = self._evict_task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _evict_loop(self) -> None:
        try:
            while not self._stopping:
                await asyncio.sleep(self.config.evict_interval_s)
                now = time.monotonic()
                try:
                    await self._store.evict_expired(now)
                    await self.risk.evict()
                    await self.limits.evict()
                    await self.idem.evict()
                    self.sanitize.evict()
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    logger.warning("shield evict failed", exc_info=True)
        except asyncio.CancelledError:
            pass

    async def snapshot(self) -> dict[str, Any]:
        """Сводка состояния контура для /api/metrics."""
        audit = await self.audit.snapshot()
        limits = await self.limits.stats()
        idem = await self.idem.stats()
        breakers = {name: await b.snapshot() for name, b in self._breakers.items()}
        return {
            "enabled": self.config.enabled,
            "store_size": await self._store.size(),
            "audit": audit,
            "limits": limits,
            "idem": idem,
            "breakers": breakers,
        }
