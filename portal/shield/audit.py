"""Наблюдаемость защитного контура: correlation_id, аудит, метрики, маскирование.

- `bind_correlation()` / `correlation_id()` — сквозной ID через contextvars.
- `mask()` — рекурсивное вычищение секретов из произвольных структур.
- `AuditLog` — потокобезопасное кольцо security-событий, счётчики,
  латентности (p50/p95 по последним замерам), интеграция с logging.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import re
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

_correlation: contextvars.ContextVar[str] = contextvars.ContextVar("shield_cid", default="-")

_SECRET_KEY_RE = re.compile(
    r"(password|passwd|secret|token|api[_-]?key|private|mnemonic|seed|auth|session|cookie)",
    re.IGNORECASE,
)
_LONG_HEX_RE = re.compile(r"\b(0x)?[0-9a-fA-F]{32,}\b")
_BEARER_RE = re.compile(r"\b(Bearer\s+)[A-Za-z0-9\-._~+/=]+", re.IGNORECASE)


def bind_correlation(cid: str | None = None) -> str:
    """Привязать correlation_id к текущему контексту. Возвращает его."""
    if not cid:
        cid = f"{int(time.monotonic() * 1000):x}-{id(_correlation):x}"[-12:]
    _correlation.set(cid)
    return cid


def correlation_id() -> str:
    return _correlation.get()


def mask_text(text: str) -> str:
    """Вычищает секреты из строки: Bearer-токены, длинные hex-строки."""
    text = _BEARER_RE.sub(r"\1…", text)

    def _hex(m: re.Match[str]) -> str:
        s = m.group(0)
        prefix = "0x" if s.lower().startswith("0x") else ""
        body = s[2:] if prefix else s
        return f"{prefix}{body[:4]}…<{len(body)}>"

    return _LONG_HEX_RE.sub(_hex, text)


def mask(obj: Any, _depth: int = 0) -> Any:
    """Рекурсивно маскирует секреты. Глубина ограничена (защита от рекурсий)."""
    if _depth > 6:
        return "…"
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(k, str) and _SECRET_KEY_RE.search(k):
                out[k] = "***"
            else:
                out[k] = mask(v, _depth + 1)
        return out
    if isinstance(obj, (list, tuple)):
        return [mask(v, _depth + 1) for v in obj][:50]
    if isinstance(obj, str):
        if len(obj) > 2000:
            obj = obj[:2000] + "…<truncated>"
        return mask_text(obj)
    return obj


@dataclass
class SecurityEvent:
    ts: float
    kind: str
    subject: str
    details: dict[str, Any] = field(default_factory=dict)
    correlation_id: str = "-"


class AuditLog:
    """Кольцо событий + счётчики + латентности. Всё под одним локом."""

    def __init__(self, ring: int = 2000, latency_keep: int = 500) -> None:
        self._lock = asyncio.Lock()
        self._events: deque[SecurityEvent] = deque(maxlen=max(1, ring))
        self._counters: Counter[str] = Counter()
        self._lat: dict[str, deque[float]] = {}
        self._latency_keep = max(50, latency_keep)

    async def event(self, kind: str, subject: str = "-",
                    details: dict[str, Any] | None = None) -> None:
        """Записать security-событие (FRAUD_DETECTED, RATE_LIMIT_EXCEEDED, ...)."""
        async with self._lock:
            self._counters[kind] += 1
            self._events.append(SecurityEvent(
                ts=time.monotonic(),
                kind=kind,
                subject=subject,
                details=mask(details or {}),
                correlation_id=correlation_id(),
            ))
        logger.warning("[AUDIT] %s sub=%s cid=%s", kind, subject, correlation_id())

    async def observe_latency(self, op: str, ms: float) -> None:
        async with self._lock:
            buf = self._lat.get(op)
            if buf is None:
                buf = deque(maxlen=self._latency_keep)
                self._lat[op] = buf
            buf.append(ms)

    async def snapshot(self) -> dict[str, Any]:
        """Снимок для /api/metrics: счётчики, p50/p95 латентностей, размер кольца."""
        async with self._lock:
            lat: dict[str, dict[str, float]] = {}
            for op, buf in self._lat.items():
                vals = sorted(buf)
                if not vals:
                    continue
                lat[op] = {
                    "p50": vals[len(vals) // 2],
                    "p95": vals[min(len(vals) - 1, int(len(vals) * 0.95))],
                    "n": len(vals),
                }
            return {
                "counters": dict(self._counters),
                "latency_ms": lat,
                "ring_size": len(self._events),
            }

    async def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        async with self._lock:
            items = list(self._events)[-max(1, limit):]
        return [
            {"ts": e.ts, "kind": e.kind, "subject": e.subject,
             "details": e.details, "cid": e.correlation_id}
            for e in items
        ]
