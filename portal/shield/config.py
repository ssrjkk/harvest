"""Типизированная конфигурация защитного контура (без магических чисел).

Значения по умолчанию безопасны для небольшого VPS. Всё переопределяется
env `PORTAL_GUARD_*` (см. from_env) или словарём (from_dict).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    return _env(name, "1" if default else "0").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class RiskConfig:
    half_life_s: float = 300.0
    w_fast_interval: float = 2.0
    w_low_variance: float = 8.0
    w_dup_payload: float = 12.0
    w_param_enum: float = 6.0
    w_flood: float = 15.0
    medium_threshold: float = 25.0
    high_threshold: float = 50.0
    min_interval_s: float = 0.2
    variance_window: int = 6
    variance_max_cv: float = 0.15
    flood_window_s: float = 60.0
    flood_max_hits: int = 40
    payload_window: int = 16
    param_enum_cap: int = 12
    tarpit_min_s: float = 1.0
    tarpit_max_s: float = 3.0
    shadowban_enabled: bool = True
    subject_ttl_s: float = 3600.0

    def validate(self) -> None:
        assert self.half_life_s > 0, "half_life_s > 0"
        assert self.medium_threshold < self.high_threshold, "medium < high"
        assert self.tarpit_min_s <= self.tarpit_max_s, "tarpit min <= max"


@dataclass(frozen=True)
class LimitsConfig:
    global_rps: float = 200.0
    global_burst: int = 400
    read_rps: float = 30.0
    read_burst: int = 60
    write_rps: float = 3.0
    write_burst: int = 6
    window_s: float = 60.0
    penalty_base_s: float = 60.0
    penalty_max_s: float = 3600.0
    penalty_violations: int = 3
    max_subjects: int = 20000
    subject_ttl_s: float = 3600.0

    def validate(self) -> None:
        assert self.global_rps > 0 and self.read_rps > 0 and self.write_rps > 0
        assert self.penalty_base_s <= self.penalty_max_s


@dataclass(frozen=True)
class IdemConfig:
    ttl_s: float = 3600.0
    max_keys: int = 10000
    time_bucket_s: float = 300.0

    def validate(self) -> None:
        assert self.ttl_s > 0 and self.max_keys > 0


@dataclass(frozen=True)
class SanitizeConfig:
    max_bytes: int = 65536
    max_depth: int = 8
    max_str: int = 4096
    max_items: int = 256
    emoji_ratio: float = 0.5
    flood_k: int = 8
    flood_max_subjects: int = 20000

    def validate(self) -> None:
        assert self.max_bytes > 0 and self.max_depth > 0


@dataclass(frozen=True)
class BreakerConfig:
    failure_threshold: int = 5
    recovery_timeout_s: float = 30.0
    half_open_probes: int = 2
    retry_attempts: int = 3
    backoff_base_s: float = 0.5
    backoff_cap_s: float = 8.0

    def validate(self) -> None:
        assert self.failure_threshold > 0 and self.recovery_timeout_s > 0


@dataclass(frozen=True)
class GuardConfig:
    enabled: bool = True
    evict_interval_s: float = 60.0
    audit_ring: int = 2000
    slow_ms: float = 1000.0
    ws_max_clients: int = 200
    risk: RiskConfig = field(default_factory=RiskConfig)
    limits: LimitsConfig = field(default_factory=LimitsConfig)
    idem: IdemConfig = field(default_factory=IdemConfig)
    sanitize: SanitizeConfig = field(default_factory=SanitizeConfig)
    breaker: BreakerConfig = field(default_factory=BreakerConfig)

    def validate(self) -> None:
        self.risk.validate()
        self.limits.validate()
        self.idem.validate()
        self.sanitize.validate()
        self.breaker.validate()

    @classmethod
    def from_env(cls, prefix: str = "PORTAL_GUARD_") -> GuardConfig:
        """Собирает конфиг из env PORTAL_GUARD_*. Неизвестные ключи игнорируются."""
        cfg = cls(
            enabled=_env_bool(prefix + "ENABLED", True),
            evict_interval_s=_env_float(prefix + "EVICT_INTERVAL", 60.0),
            audit_ring=_env_int(prefix + "AUDIT_RING", 2000),
            slow_ms=_env_float(prefix + "SLOW_MS", 1000.0),
            ws_max_clients=_env_int(prefix + "WS_MAX_CLIENTS", 200),
            risk=RiskConfig(
                half_life_s=_env_float(prefix + "RISK_HALF_LIFE", 300.0),
                medium_threshold=_env_float(prefix + "RISK_MEDIUM", 25.0),
                high_threshold=_env_float(prefix + "RISK_HIGH", 50.0),
                tarpit_min_s=_env_float(prefix + "TARPIT_MIN", 1.0),
                tarpit_max_s=_env_float(prefix + "TARPIT_MAX", 3.0),
                shadowban_enabled=_env_bool(prefix + "SHADOWBAN", True),
            ),
            limits=LimitsConfig(
                global_rps=_env_float(prefix + "GLOBAL_RPS", 200.0),
                read_rps=_env_float(prefix + "READ_RPS", 30.0),
                write_rps=_env_float(prefix + "WRITE_RPS", 3.0),
                penalty_base_s=_env_float(prefix + "PENALTY_BASE", 60.0),
                penalty_max_s=_env_float(prefix + "PENALTY_MAX", 3600.0),
            ),
            breaker=BreakerConfig(
                failure_threshold=_env_int(prefix + "CB_THRESHOLD", 5),
                recovery_timeout_s=_env_float(prefix + "CB_TIMEOUT", 30.0),
                retry_attempts=_env_int(prefix + "RETRY_ATTEMPTS", 3),
            ),
        )
        cfg.validate()
        return cfg
