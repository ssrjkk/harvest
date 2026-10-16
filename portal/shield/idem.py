"""Идемпотентность критических операций (защита от дублей и гонок).

Паттерн: `run(key, ttl, factory)` — параллельные вызовы с одним ключом
сериализуются per-key блокировкой; повтор в пределах TTL возвращает
сохранённый результат с флагом `replayed=True`, бизнес-логика второй раз
не выполняется. Падение factory удаляет ключ (следующий вызов — заново).

Детерминированный ключ: `make_key(actor, action, payload, bucket_s)` —
sha256 от `(actor, action, sha256(payload), time_bucket)`.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from .config import IdemConfig
from .store import AsyncStore, MemoryStore

PENDING = "pending"
DONE = "done"


@dataclass
class _Record:
    status: str
    result: Any = None


@dataclass(frozen=True)
class IdemResult:
    result: Any
    replayed: bool


def make_key(actor: str, action: str, payload: bytes | str,
             bucket_s: float = 300.0, now: float | None = None) -> str:
    """Детерминированный ключ идемпотентности с временным бакетом."""
    ts = time.monotonic() if now is None else now
    bucket = int(ts // max(bucket_s, 1.0))
    ph = hashlib.sha256(payload.encode("utf-8", "ignore")
                        if isinstance(payload, str) else bytes(payload)).hexdigest()
    raw = f"{actor}\x00{action}\x00{ph}\x00{bucket}".encode("utf-8", "ignore")
    return "idem:" + hashlib.sha256(raw).hexdigest()


class IdempotencyStore:
    """Per-key локи + TTL-результаты. Потокобезопасно."""

    def __init__(self, config: IdemConfig, store: AsyncStore | None = None) -> None:
        config.validate()
        self.cfg = config
        self._store = store or MemoryStore()
        self._guard = asyncio.Lock()
        self._locks: dict[str, asyncio.Lock] = {}
        self._lock_used: dict[str, float] = {}
        self.replays = 0
        self.executions = 0

    async def _key_lock(self, key: str) -> asyncio.Lock:
        async with self._guard:
            lock = self._locks.get(key)
            if lock is None:
                if len(self._locks) >= self.cfg.max_keys:
                    oldest = min(self._lock_used, key=lambda k: self._lock_used.get(k, 0.0),
                                 default=None)
                    if oldest is not None:
                        self._locks.pop(oldest, None)
                        self._lock_used.pop(oldest, None)
                lock = asyncio.Lock()
                self._locks[key] = lock
            self._lock_used[key] = time.monotonic()
            return lock

    async def run(self, key: str, factory: Callable[[], Awaitable[Any]],
                  ttl_s: float | None = None) -> IdemResult:
        """Выполнить factory один раз на ключ. Повторы возвращают результат."""
        ttl = ttl_s if ttl_s and ttl_s > 0 else self.cfg.ttl_s
        now = time.monotonic()
        seen = await self._store.kv_get(key, now)
        if isinstance(seen, _Record) and seen.status == DONE:
            self.replays += 1
            return IdemResult(seen.result, True)
        lock = await self._key_lock(key)
        async with lock:
            seen = await self._store.kv_get(key, time.monotonic())
            if isinstance(seen, _Record) and seen.status == DONE:
                self.replays += 1
                return IdemResult(seen.result, True)
            self.executions += 1
            try:
                result = await factory()
            except Exception:
                await self._store.kv_delete(key)
                raise
            await self._store.kv_set(key, _Record(DONE, result), time.monotonic() + ttl)
            return IdemResult(result, False)

    async def evict(self) -> int:
        """Прогнать TTL-сборку store + подчистить простаивающие локи."""
        now = time.monotonic()
        removed = await self._store.evict_expired(now)
        async with self._guard:
            idle = [k for k, ts in self._lock_used.items()
                    if now - ts > self.cfg.ttl_s and not self._locks[k].locked()]
            for k in idle:
                self._locks.pop(k, None)
                self._lock_used.pop(k, None)
            removed += len(idle)
        return removed

    async def stats(self) -> dict[str, int]:
        return {
            "locks": len(self._locks),
            "store_size": await self._store.size(),
            "replays": self.replays,
            "executions": self.executions,
        }
