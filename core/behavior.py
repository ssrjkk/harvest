"""Индивидуальные персоны нагрузки для кошельков (QA-персоны).

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
    weekend_strength: float = 0.0
    day_start: float = 9.0
    day_width: float = 8.0
    neutral: bool = False


def _cfg(config: dict, key: str, default):
    return (config.get("behavior") or {}).get(key, default)


def _draw_bounded(rng: random.Random, config: dict, key: str, default: float,
                  lo: float, hi: float) -> float:
    """Поковолочное значение knob'а: скаляр — как раньше (фиксировано),
    пара [lo, hi] — розыгрыш на кошелёк. Детерминировано адресом через rng."""
    raw = _cfg(config, key, default)
    if isinstance(raw, (list, tuple)) and len(raw) == 2:
        a, b = float(raw[0]), float(raw[1])
        if b < a:
            a, b = b, a
        return max(lo, min(hi, rng.uniform(a, b)))
    try:
        return max(lo, min(hi, float(raw)))
    except (TypeError, ValueError):
        return max(lo, min(hi, float(default)))


def cfg_hi(config: dict, key: str, default: float) -> float:
    """Верхняя граница knob'а для бюджетных расчётов (таймауты пула):
    скаляр — сам, пара — её максимум. Не роняет float() на списке."""
    raw = (config.get("behavior") or {}).get(key, default)
    if isinstance(raw, (list, tuple)) and len(raw) == 2:
        try:
            return float(max(raw[0], raw[1]))
        except (TypeError, ValueError):
            pass
    try:
        return float(raw)
    except (TypeError, ValueError):
        return float(default)


def _as_range(value, default: tuple[float, float]) -> tuple[float, float]:
    """Нормализует диапазон из конфига в пару (lo, hi).

    Скаляр `activity: 1.5` осмыслен как фиксированное значение (1.5, 1.5),
    а не как `rng.uniform(1.5)`, который рушится TypeError. Мусор (строка
    и т.п.) — на дефолт: безопасное поведение, а не безвучная нулевая
    производительность кошелька. Перевёрнутая пара [hi, lo] — swap:
    uniform() её молча терпит, а randint() роняет ValueError.
    """
    if isinstance(value, bool):
        return (float(default[0]), float(default[1]))
    if isinstance(value, (list, tuple)) and len(value) == 2:
        try:
            lo, hi = float(value[0]), float(value[1])
        except (TypeError, ValueError):
            return (float(default[0]), float(default[1]))
        return (lo, hi) if lo <= hi else (hi, lo)
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
    # randint — включительно: int(uniform(1,3)) давал только 1-2, 3 недостижима.
    rest_lo = max(1, rng.randint(int(rl), int(rh)))
    rest_cycles = (rest_lo, max(rest_lo + 1, rng.randint(int(rl), int(rh)) + 1))

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

    mix = _draw_bounded(rng, config, "action_mix", 0.4, 0.0, 2.0)
    multipliers: dict[str, float] = {}
    if mix > 0:
        lo = 1.0 - min(mix, 1.0)
        hi = 1.0 + min(mix, 1.0)
        if mix >= 1.0:
            lo, hi = 0.3, 2.2
        for atype in _ALL_ACTION_TYPES:
            multipliers[atype] = max(0.1, rng.uniform(lo, hi))

    jitter = _draw_bounded(rng, config, "amount_jitter", 0.25, 0.0, 0.9)
    amount_min_mul = max(0.1, 1.0 + rng.uniform(-jitter, jitter * 0.6))
    amount_max_mul = max(0.1, 1.0 + rng.uniform(-jitter * 0.4, jitter))
    # Независимые розыгрыши могли инвертировать коридор (мин > макс в ~21%
    # случаев): суммы ниже называли «максимумом». Swap чинит семантику.
    if amount_max_mul < amount_min_mul:
        amount_min_mul, amount_max_mul = amount_max_mul, amount_min_mul

    gas_deviation = _draw_bounded(rng, config, "gas_deviation", 0.06, 0.0, 0.5)
    if not isinstance(_cfg(config, "gas_deviation", 0.06), (list, tuple)):
        # Скаляр — старое поведение: розыгрыш в [0, v] (не фиксированное v).
        try:
            gv = max(0.0, min(0.5, float(_cfg(config, "gas_deviation", 0.06))))
        except (TypeError, ValueError):
            gv = 0.06
        gas_deviation = max(0.0, min(0.5, rng.uniform(0.0, gv)))
    odd_amount_prob = _draw_bounded(rng, config, "odd_amount_prob", 0.15, 0.0, 1.0)
    faucet_skip_prob = _draw_bounded(rng, config, "faucet_skip_prob", 0.1, 0.0, 1.0)
    daily_window_strength = _draw_bounded(rng, config, "daily_window", 0.3, 0.0, 1.0)
    burst_prob = _draw_bounded(rng, config, "burst_prob", 0.05, 0.0, 1.0)
    burst_mult = _draw_bounded(rng, config, "burst_multiplier", 1.6, 1.0, 5.0)
    rest_prob = _draw_bounded(rng, config, "rest_probability", 0.05, 0.0, 1.0)

    # Staggered start: детерминированный стартовый сдвиг кошелька (персона нагрузки).
    # Воркер-пул не стартует все кошельки одновременно — каждый входит плавно.
    sd = _cfg(config, "start_delay", (0.0, 3.0))
    if isinstance(sd, bool):
        sd_range = (0.0, 0.0)
    elif isinstance(sd, (int, float)) and not isinstance(sd, bool):
        sd_range = (float(sd), float(sd))
    elif isinstance(sd, (list, tuple)) and len(sd) == 2:
        try:
            a, b = float(sd[0]), float(sd[1])
        except (TypeError, ValueError):
            # ["x","y"] раньше роняли profile_for с ValueError (блокер:
            # behavior вообще не валидируется, пул крашился на старте).
            a = b = 0.0
        sd_range = (a, b) if a <= b else (b, a)
    else:
        # Мусор/нечисловой тип — без стаггера (fix тест: "owo" -> 0.0).
        sd_range = (0.0, 0.0)
    start_delay = max(0.0, min(30.0, rng.uniform(*sd_range)))

    weekend_strength = _draw_bounded(rng, config, "weekend_strength", 0.0, 0.0, 1.0)
    day_start = _draw_bounded(rng, config, "day_start", 9.0, 0.0, 24.0)
    day_width = _draw_bounded(rng, config, "day_width", 8.0, 1.0, 16.0)

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
        weekend_strength=weekend_strength,
        day_start=day_start,
        day_width=day_width,
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


def day_of_week_factor(strength: float) -> float:
    """Множитель пропуска по дню недели: в выходные люди активнее.

    strength=0 → всегда 1.0. Суббота/воскресенье снижают вероятность
    пропуска, будни — нейтральны.
    """
    if strength <= 0:
        return 1.0
    if time.localtime().tm_wday >= 5:
        return max(0.5, 1.0 - 0.3 * strength)
    return 1.0


def hour_window_factor(profile: WalletProfile) -> float:
    """Окно привычной активности кошелька [day_start, +day_width).

    Внутри окна пропуск чуть реже (0.85), снаружи — заметно чаще (1.8):
    «сова» и «жаворонок» не пересекаются. Нейтральный профиль — 1.0.
    """
    if profile.neutral:
        return 1.0
    now = time.localtime()
    hour = now.tm_hour + now.tm_min / 60.0
    start = profile.day_start % 24.0
    width = max(1.0, min(16.0, profile.day_width))
    if width >= 24.0:
        return 1.0
    end = start + width
    if end <= 24.0:
        inside = start <= hour < end
    else:
        inside = hour >= start or hour < (end - 24.0)
    return 0.85 if inside else 1.8


def gas_multiplier(profile: WalletProfile) -> float:
    """Множитель gas price для транзакции кошелька (1.0 при нейтральном профиле)."""
    if profile.neutral or profile.gas_deviation <= 0:
        return 1.0
    return max(0.5, min(2.0, 1.0 + random.uniform(-profile.gas_deviation, profile.gas_deviation)))
