"""Защитный контур портала (антифрод, троттлинг, отказоустойчивость).

Точка замены на Redis в будущем — `store.AsyncStore` / `store.MemoryStore`.
Быстрый старт:
    from portal.shield import Shield, shield_middleware, idempotent
    shield = Shield.from_env()
    await shield.start()  # фоновый TTL-сборщик
    app.middlewares.append(shield_middleware(shield))
"""

from .audit import AuditLog, bind_correlation, correlation_id, mask, mask_text
from .breaker import CircuitBreaker, CircuitOpenError, backoff_delay, run_with_retry
from .config import BreakerConfig, GuardConfig, IdemConfig, LimitsConfig, RiskConfig, SanitizeConfig
from .idem import IdempotencyStore, IdemResult, make_key
from .limits import Check, HierarchicalLimiter
from .middleware import idempotent, shield_middleware
from .risk import HIGH, LOW, MEDIUM, Observation, RiskEngine, Verdict
from .sanitize import PayloadGuard, SanitizationError, clean_text
from .shield import Shield
from .store import AsyncStore, MemoryStore

__all__ = [
    "AuditLog", "bind_correlation", "correlation_id", "mask", "mask_text",
    "CircuitBreaker", "CircuitOpenError", "backoff_delay", "run_with_retry",
    "BreakerConfig", "GuardConfig", "IdemConfig", "LimitsConfig",
    "RiskConfig", "SanitizeConfig",
    "IdempotencyStore", "IdemResult", "make_key",
    "Check", "HierarchicalLimiter",
    "idempotent", "shield_middleware",
    "HIGH", "LOW", "MEDIUM", "Observation", "RiskEngine", "Verdict",
    "PayloadGuard", "SanitizationError", "clean_text",
    "Shield",
    "AsyncStore", "MemoryStore",
]
