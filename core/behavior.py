"""Индивидуальное поведение кошельков (анти-сибил).

Каждый кошелёк получает стабильный детерминированный профиль (по хэшу адреса):
своя активность, темп действий, паузы, суммы, газ, предпочтения по типам
действий. Даже в общем пуле кошельки ведут себя по-разному и не сливаются
в единый «ботовский» паттерн.

Профиль не зависит от времени запуска: перезапуск не меняет поведение.
Отключить — behavior.enabled: false (все множители нейтральны).
"""

import hashlib
import logging
import random
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

_ALL_ACTION_TYPES = (
    "transfer",
    "vibevibe_swap",
    "vibevibe_mint",
    "flop_compute",
    "flop_validate",
    "flop_stake",
    "arc_launch",
    "arc_trade",
    "arc_add_liquidity",
)


@dataclass
class WalletProfile:
    """Профиль поведения одного кошелька (все поля — множители/вероятности)."""

    activity: float = 1.0
    rest_prob: float = 0.0
    rest_cycles: tuple[int, int] = (1, 1)
    delay_actions_scale: float = 1.0
    delay_cycles_scale: float = 1.0
    actions_lo: int = 1
    actions_width: int = 1
    action_multipliers: dict[str, float] = field(default_factory=dict)
    amount_min_mul: float = 1.0
    amount_max_mul: float = 1.0
    gas_deviation: float = 0.0
    odd_amount_prob: float = 0.0
    faucet_skip_prob: float = 0.0
    daily_window_strength: float = 0.0
    burst_prob: float = 0.0
    burst_mult: float = 1.5
    start_delay: float = 0.0
    neutral: bool = False


def _cfg(config: dict, key: str, default):
    return (config.get("behavior") or {}).get(key, default)


def _as_range(value, default: tuple[float, float]) -> tuple[float, float]:
    """Нормализует диапазон из конфига в пару (lo, hi).

    Скаляр `activity: 1.5` осмыслен как фиксированное значение (1.5, 1.5),
    а не как `rng.uniform(1.5)`, который рушится TypeError. Мусор (строка
    и т.п.) — на дефолт: безопасное поведение, а не безвучная нулевая
    производительность кошелька.
    """
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return (float(value[0]), float(value[1]))
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        v = float(value)
        return (v, v)
    return (float(default[0]), float(default[1]))


def profile_for(address: str, config: dict) -> WalletProfile:
    """Детерминированный профиль поведения для адреса."""
    beh = config.get("behavior") or {}
    if not beh.get("enabled", True):
        return WalletProfile(neutral=True)

    rng = random.Random(int(hashlib.sha256(address.lower().encode("utf-8")).hexdigest()[:16], 16))

    activity = max(0.1, min(3.0, rng.uniform(*_as_range(_cfg(config, "activity", (0.5, 1.5)), (0.5, 1.5)))))

    rl, rh = _as_range(_cfg(config, "rest_cycles", (1, 3)), (1, 3))
    rest_lo = max(1, int(rng.uniform(rl, rh)))
    rest_cycles = (rest_lo, max(rest_lo + 1, int(rng.uniform(rl, rh)) + 1))

    adl = _as_range(_cfg(config, "action_delay_scale", (0.7, 1.5)), (0.7, 1.5))
    delay_actions_scale = max(0.1, min(5.0, rng.uniform(*adl)))
    cdl = _as_range(_cfg(config, "cycle_delay_scale", (0.8, 1.8)), (0.8, 1.8))
    delay_cycles_scale = max(0.1, min(5.0, rng.uniform(*cdl)))

    # Индивидуальный «коридор» числа действий в цикле (уже глобального диапазона).
    raw_apt = config.get("farming", {}).get("actions_per_cycle", [1, 8])
    if isinstance(raw_apt, (list, tuple)) and len(raw_apt) == 2:
        gmin, gmax = int(raw_apt[0]), int(raw_apt[1])
    else:
        gmin = gmax = int(raw_apt)
    span = max(1, gmax - gmin + 1)
    width = max(1, int(round(span * rng.uniform(0.35, 0.7))))
    actions_lo = rng.randint(gmin, max(gmin, gmax - width + 1))
    actions_width = width

    mix = float(_cfg(config, "action_mix", 0.4))
    multipliers: dict[str, float] = {}
    if mix > 0:
        lo = 1.0 - min(mix, 1.0)
        hi = 1.0 + min(mix, 1.0)
        if mix >= 1.0:
            lo, hi = 0.3, 2.2
        for atype in _ALL_ACTION_TYPES:
            multipliers[atype] = max(0.1, rng.uniform(lo, hi))

    jitter = max(0.0, min(0.9, float(_cfg(config, "amount_jitter", 0.25))))
    amount_min_mul = max(0.1, 1.0 + rng.uniform(-jitter, jitter * 0.6))
    amount_max_mul = max(0.1, 1.0 + rng.uniform(-jitter * 0.4, jitter))

    gas_deviation = max(0.0, min(0.5, rng.uniform(0.0, float(_cfg(config, "gas_deviation", 0.06)))))
    odd_amount_prob = max(0.0, min(1.0, float(_cfg(config, "odd_amount_prob", 0.15))))
    faucet_skip_prob = max(0.0, min(1.0, float(_cfg(config, "faucet_skip_prob", 0.1))))
    daily_window_strength = max(0.0, min(1.0, float(_cfg(config, "daily_window", 0.3))))
    burst_prob = max(0.0, min(1.0, float(_cfg(config, "burst_prob", 0.05))))
    burst_mult = max(1.0, min(5.0, float(_cfg(config, "burst_multiplier", 1.6))))
    rest_prob = max(0.0, min(1.0, float(_cfg(config, "rest_probability", 0.05))))

    # Staggered start: детерминированный стартовый сдвиг кошелька (анти-сибил).
    # Воркер-пул не стартует все кошельки одновременно — каждый входит плавно.
    sd = _cfg(config, "start_delay", (0.0, 3.0))
    if isinstance(sd, (int, float)) and not isinstance(sd, bool):
        sd_range = (float(sd), float(sd))
    elif isinstance(sd, (list, tuple)) and len(sd) == 2:
        sd_range = (float(sd[0]), float(sd[1]))
    else:
        # Мусор/нечисловой тип — без стаггера (fix тест: "owo" -> 0.0).
        sd_range = (0.0, 0.0)
    start_delay = max(0.0, min(30.0, rng.uniform(*sd_range)))

    return WalletProfile(
        activity=activity,
        rest_prob=rest_prob,
        rest_cycles=rest_cycles,
        delay_actions_scale=delay_actions_scale,
        delay_cycles_scale=delay_cycles_scale,
        actions_lo=actions_lo,
        actions_width=actions_width,
        action_multipliers=multipliers,
        amount_min_mul=amount_min_mul,
        amount_max_mul=amount_max_mul,
        gas_deviation=gas_deviation,
        odd_amount_prob=odd_amount_prob,
        faucet_skip_prob=faucet_skip_prob,
        daily_window_strength=daily_window_strength,
        burst_prob=burst_prob,
        burst_mult=burst_mult,
        start_delay=start_delay,
    )


def time_of_day_factor(strength: float) -> float:
    """Множитель вероятности пропуска цикла по времени суток.

    strength=0 → всегда 1.0 (равномерно). Чем выше strength, тем охотнее
    кошелёк «молчит» ночью и активнее днём.
    """
    if strength <= 0:
        return 1.0
    hour = time.localtime().tm_hour
    if 7 <= hour <= 22:
        return 1.0 - 0.25 * strength
    return 1.0 + 1.5 * strength


def gas_multiplier(profile: WalletProfile) -> float:
    """Множитель gas price для транзакции кошелька (1.0 при нейтральном профиле)."""
    if profile.neutral or profile.gas_deviation <= 0:
        return 1.0
    return max(0.5, min(2.0, 1.0 + random.uniform(-profile.gas_deviation, profile.gas_deviation)))
