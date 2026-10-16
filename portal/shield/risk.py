"""Decay-скоринг риска субъекта (User/IP/Session) с тремя порогами реакции.

Модель: у каждого субъекта счёт, который экспоненциально затухает
со временем (`score *= 0.5 ** (dt / half_life)`), а аномалии начисляют
штрафные баллы с весами из конфига:
- слишком частые действия (bot-like timing);
- низкая дисперсия интервалов (метроном — признак скрипта);
- дублирующиеся пейлоады (спам одним текстом);
- перебор имён параметров (сканирование/фаззинг);
- флуд сверх квоты окна.

Пороги: LOW — штатно; MEDIUM — tarpit (замедление 1–3 с) + аудит;
HIGH — shadowban (тихая изоляция с правдоподобным ответом) + аудит.
Все метки времени — time.monotonic(). Состояние — в AsyncStore.
"""

from __future__ import annotations

import asyncio
import hashlib
import statistics
import time
from collections import deque
from dataclasses import dataclass, field

from .config import RiskConfig
from .store import AsyncStore, MemoryStore

LOW = "low"
MEDIUM = "medium"
HIGH = "high"

_CELL_INTERVALS = "intervals"
_CELL_PAYLOADS = "payloads"
_CELL_PARAMS = "params"
_CELL_HITS = "hits"


@dataclass
class SubjectState:
    score: float = 0.0
    last_ts: float = 0.0
    intervals: deque = field(default_factory=lambda: deque(maxlen=32))
    payloads: deque = field(default_factory=lambda: deque(maxlen=32))
    param_keys: set = field(default_factory=set)


@dataclass(frozen=True)
class Observation:
    """Что нарушитель сделал сейчас (для начисления сигналов)."""

    payload_hash: str = ""
    param_keys: frozenset = frozenset()


@dataclass(frozen=True)
class Verdict:
    level: str
    score: float
    tarpit_s: float = 0.0
    shadowban: bool = False


def _decay(score: float, dt: float, half_life: float) -> float:
    if dt <= 0:
        return score
    return score * (0.5 ** (dt / half_life))


class RiskEngine:
    """Скоринг с затуханием + пороговые реакции."""

    def __init__(self, config: RiskConfig, store: AsyncStore | None = None) -> None:
        config.validate()
        self.cfg = config
        self._store = store or MemoryStore()
        self._states: dict[str, SubjectState] = {}
        self._lock = asyncio.Lock()

    def _state(self, subject: str) -> SubjectState:
        st = self._states.get(subject)
        if st is None:
            st = SubjectState(last_ts=time.monotonic())
            self._states[subject] = st
        return st

    async def observe(self, subject: str, obs: Observation | None = None) -> tuple[Verdict, list[str]]:
        """Учесть событие субъекта, вернуть (вердикт, причины). Потокобезопасно."""
        cfg = self.cfg
        obs = obs or Observation()
        now = time.monotonic()
        async with self._lock:
            st = self._state(subject)
            st.score = _decay(st.score, now - st.last_ts, cfg.half_life_s)
            dt = now - st.last_ts
            st.last_ts = now

            gained = 0.0
            reasons: list[str] = []

            # 1. Слишком частые действия.
            if dt < cfg.min_interval_s:
                gained += cfg.w_fast_interval
                reasons.append(f"fast_interval dt={dt:.3f}s")
            if dt > 0:
                st.intervals.append(dt)
            # 2. Метроном: низкая дисперсия интервалов.
            if len(st.intervals) >= cfg.variance_window:
                window = list(st.intervals)[-cfg.variance_window:]
                mean = sum(window) / len(window)
                if mean > 0:
                    cv = (statistics.pstdev(window) / mean) if len(window) > 1 else 1.0
                    if cv < cfg.variance_max_cv:
                        gained += cfg.w_low_variance
                        reasons.append(f"low_variance cv={cv:.3f}")

            # 3. Дублирующийся пейлоад.
            if obs.payload_hash:
                if obs.payload_hash in st.payloads:
                    gained += cfg.w_dup_payload
                    reasons.append("dup_payload")
                st.payloads.append(obs.payload_hash)
                while len(st.payloads) > cfg.payload_window:
                    st.payloads.popleft()

            # 4. Перебор имён параметров.
            if obs.param_keys:
                before = len(st.param_keys)
                st.param_keys.update(obs.param_keys)
                if len(st.param_keys) > cfg.param_enum_cap > before:
                    gained += cfg.w_param_enum
                    reasons.append(f"param_enum keys={len(st.param_keys)}")

            st.score += gained

            # 5. Флуд: число обращений в окне (через store для персистентности).
            hits = await self._store.window_add(
                f"risk:{subject}:{_CELL_HITS}", now, cfg.flood_window_s)
            if hits > cfg.flood_max_hits:
                over = hits - cfg.flood_max_hits
                st.score += cfg.w_flood * min(over, 5)
                reasons.append(f"flood hits={hits}")

            level = LOW
            tarpit = 0.0
            shadow = False
            if st.score >= cfg.high_threshold:
                level, shadow = HIGH, True
            elif st.score >= cfg.medium_threshold:
                level = MEDIUM
                span = max(cfg.high_threshold - cfg.medium_threshold, 1e-9)
                k = min(max((st.score - cfg.medium_threshold) / span, 0.0), 1.0)
                tarpit = cfg.tarpit_min_s + k * (cfg.tarpit_max_s - cfg.tarpit_min_s)
            return Verdict(level=level, score=st.score,
                           tarpit_s=tarpit, shadowban=shadow and cfg.shadowban_enabled), reasons

    async def score_of(self, subject: str) -> float:
        """Текущий счёт с учётом затухания (без начисления)."""
        now = time.monotonic()
        async with self._lock:
            st = self._states.get(subject)
            if st is None:
                return 0.0
            return _decay(st.score, now - st.last_ts, self.cfg.half_life_s)

    async def reset(self, subject: str) -> None:
        async with self._lock:
            self._states.pop(subject, None)

    async def evict(self, older_than_s: float | None = None) -> int:
        """Убрать давно молчавшие субъекты (защита от раздувания памяти)."""
        if older_than_s is None:
            older_than_s = self.cfg.subject_ttl_s
        now = time.monotonic()
        async with self._lock:
            dead = [s for s, st in self._states.items() if now - st.last_ts > older_than_s]
            for s in dead:
                del self._states[s]
            return len(dead)

    @staticmethod
    def payload_hash_of(*parts: str) -> str:
        h = hashlib.sha256()
        for p in parts:
            h.update(p.encode("utf-8", "ignore"))
            h.update(b"\x00")
        return h.hexdigest()

    @staticmethod
    def tarpit_delay(score: float, medium: float, high: float,
                     tmin: float, tmax: float) -> float:
        span = max(high - medium, 1e-9)
        k = min(max((score - medium) / span, 0.0), 1.0)
        return tmin + k * (tmax - tmin)
