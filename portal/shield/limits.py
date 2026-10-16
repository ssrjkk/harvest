"""Иерархический троттлинг: global bucket + per-actor окна + penalty box.

- Уровень 1 (global): общий token bucket всего сервиса (load shedding).
- Уровень 2 (per-actor): раздельные квоты read (дешёвые) и write (дорогие)
  в скользящем окне `window_s`.
- Penalty box: N подряд нарушений подряд → блок с экспоненциальным ростом
  (`base * 2^k`, кап `max`). Успешное окно без нарушений сбрасывает счётчик.

Возврат `Check`: allowed + retry_after_s + причина. Всё под одним локом,
состояние субъектов ограничено `max_subjects` (вытеснение самых старых).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from .config import LimitsConfig
from .store import AsyncStore, MemoryStore


@dataclass(frozen=True)
class Check:
    allowed: bool
    retry_after_s: float = 0.0
    reason: str = ""
    penalty_until: float = 0.0


@dataclass
class _Subject:
    read_marks: list[float] = field(default_factory=list)
    write_marks: list[float] = field(default_factory=list)
    violations: int = 0
    blocked_until: float = 0.0
    last_violation: float = 0.0
    last_seen: float = 0.0


class HierarchicalLimiter:
    """Глобальный bucket + окна субъектов + penalty box."""

    def __init__(self, config: LimitsConfig, store: AsyncStore | None = None) -> None:
        config.validate()
        self.cfg = config
        self._store = store or MemoryStore()
        self._lock = asyncio.Lock()
        self._subjects: dict[str, _Subject] = {}
        self._global_tokens = float(config.global_burst)
        self._global_last = time.monotonic()
        self.dropped_global = 0
        self.dropped_actor = 0
        self.penalty_blocks = 0

    def _subject(self, key: str, now: float) -> _Subject:
        st = self._subjects.get(key)
        if st is None:
            if len(self._subjects) >= self.cfg.max_subjects:
                oldest = min(self._subjects, key=lambda k: self._subjects[k].last_seen)
                del self._subjects[oldest]
            st = _Subject(last_seen=now)
            self._subjects[key] = st
        st.last_seen = now
        return st

    @staticmethod
    def _prune(marks: list[float], now: float, window: float) -> list[float]:
        return [t for t in marks if t >= now - window]

    async def check(self, subject: str, kind: str = "read") -> Check:
        """Проверить квоту. kind: 'read' | 'write'. Потокобезопасно."""
        cfg = self.cfg
        now = time.monotonic()
        budget = cfg.read_rps if kind == "read" else cfg.write_rps
        burst = cfg.read_burst if kind == "read" else cfg.write_burst
        async with self._lock:
            # Уровень 1: глобальный bucket.
            dt = now - self._global_last
            self._global_last = now
            self._global_tokens = min(
                float(cfg.global_burst), self._global_tokens + dt * cfg.global_rps)
            if self._global_tokens < 1.0:
                self.dropped_global += 1
                return Check(False, 1.0 / cfg.global_rps, "global_overload")
            # Уровень 2: penalty box субъекта.
            st = self._subject(subject, now)
            if now < st.blocked_until:
                self.dropped_actor += 1
                return Check(False, st.blocked_until - now, "penalty_box",
                             penalty_until=st.blocked_until)
            marks = st.read_marks if kind == "read" else st.write_marks
            marks = self._prune(marks, now, cfg.window_s)
            if kind == "read":
                st.read_marks = marks
            else:
                st.write_marks = marks
            allowed_n = max(int(budget * cfg.window_s), burst)
            if len(marks) >= allowed_n:
                return self._violate(st, now)
            # Тихое окно сбрасывает серию нарушений.
            if st.violations and now - st.last_violation > cfg.window_s:
                st.violations = 0
            marks.append(now)
            self._global_tokens -= 1.0
            return Check(True, 0.0, "")

    def _violate(self, st: _Subject, now: float) -> Check:
        st.violations += 1
        st.last_violation = now
        self.dropped_actor += 1
        if st.violations >= self.cfg.penalty_violations:
            k = st.violations - self.cfg.penalty_violations
            block = min(self.cfg.penalty_base_s * (2.0 ** k), self.cfg.penalty_max_s)
            st.blocked_until = now + block
            self.penalty_blocks += 1
            return Check(False, block, "penalty_box", penalty_until=st.blocked_until)
        return Check(False, self.cfg.window_s, "rate_limit")

    async def evict(self, older_than_s: float | None = None) -> int:
        """Убрать давно молчавшие субъекты (защита от раздувания памяти)."""
        if older_than_s is None:
            older_than_s = self.cfg.subject_ttl_s
        now = time.monotonic()
        async with self._lock:
            dead = [k for k, st in self._subjects.items()
                    if now - st.last_seen > older_than_s]
            for k in dead:
                del self._subjects[k]
            return len(dead)

    async def stats(self) -> dict[str, int | float]:
        async with self._lock:
            return {
                "subjects": len(self._subjects),
                "dropped_global": self.dropped_global,
                "dropped_actor": self.dropped_actor,
                "penalty_blocks": self.penalty_blocks,
            }
