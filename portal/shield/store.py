"""Адаптер хранилища для защитного контура.

`AsyncStore` — минимально необходимый интерфейс (счётчики окон, KV с TTL,
очистка). `MemoryStore` — потокобезопасная in-memory реализация
(asyncio.Lock + монотонное время + TTL). Переход на Redis в будущем —
замена одного класса, реализующего `AsyncStore`.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections import OrderedDict

# Горизонт хранения меток окон (дольше окна лимитов быть не может).
_WINDOW_RETENTION_S = 86400.0


class AsyncStore(ABC):
    """Контракт хранилища. Все метки времени — time.monotonic()."""

    @abstractmethod
    async def window_add(self, key: str, now: float, window_s: float) -> int:
        """Добавить отметку в скользящее окно, вернуть число отметок в окне."""

    @abstractmethod
    async def kv_get(self, key: str, now: float) -> object | None:
        """Прочитать значение, либо None (нет/протухло)."""

    @abstractmethod
    async def kv_set(self, key: str, value: object, expires: float) -> None:
        """Записать значение с абсолютным дедлайном протухания."""

    @abstractmethod
    async def kv_delete(self, key: str) -> None:
        """Удалить ключ (идемпотентно)."""

    @abstractmethod
    async def evict_expired(self, now: float) -> int:
        """Удалить протухшие записи. Возвращает число удалённых."""

    @abstractmethod
    async def size(self) -> int:
        """Текущее число записей (для мониторинга утечек)."""


class MemoryStore(AsyncStore):
    """In-memory реализация. Окна — deque-подобные списки меток (срезом).

    Ограничение роста: `max_entries` — при переполнении вытесняются самые
    старые ключи (FIFO), счётчик вытеснений доступен через `evictions`.
    """

    def __init__(self, max_entries: int = 50000) -> None:
        self._lock = asyncio.Lock()
        self._windows: dict[str, list[float]] = {}
        self._kv: OrderedDict[str, tuple[object, float]] = OrderedDict()
        self._max_entries = max(100, int(max_entries))
        self.evictions = 0

    async def window_add(self, key: str, now: float, window_s: float) -> int:
        async with self._lock:
            marks = self._windows.get(key)
            if marks is None:
                marks = []
                self._windows[key] = marks
            cutoff = now - window_s
            kept = [t for t in marks if t >= cutoff]
            kept.append(now)
            self._windows[key] = kept
            return len(kept)

    async def kv_get(self, key: str, now: float) -> object | None:
        async with self._lock:
            item = self._kv.get(key)
            if item is None:
                return None
            value, expires = item
            if expires <= now:
                del self._kv[key]
                return None
            return value

    async def kv_set(self, key: str, value: object, expires: float) -> None:
        async with self._lock:
            if key not in self._kv and len(self._kv) + len(self._windows) >= self._max_entries:
                self._drop_oldest_locked()
            self._kv[key] = (value, expires)

    async def kv_delete(self, key: str) -> None:
        async with self._lock:
            self._kv.pop(key, None)

    async def evict_expired(self, now: float) -> int:
        removed = 0
        async with self._lock:
            dead_kv = [k for k, (_, exp) in self._kv.items() if exp <= now]
            for k in dead_kv:
                del self._kv[k]
            removed += len(dead_kv)
            dead_w: list[str] = []
            for k, marks in self._windows.items():
                kept = [t for t in marks if t > now - _WINDOW_RETENTION_S]
                if kept:
                    self._windows[k] = kept
                else:
                    dead_w.append(k)
            for k in dead_w:
                del self._windows[k]
            removed += len(dead_w)
        return removed

    async def size(self) -> int:
        async with self._lock:
            return len(self._kv) + len(self._windows)

    def _drop_oldest_locked(self) -> None:
        if self._kv:
            self._kv.popitem(last=False)
            self.evictions += 1
        elif self._windows:
            oldest = next(iter(self._windows))
            del self._windows[oldest]
            self.evictions += 1
