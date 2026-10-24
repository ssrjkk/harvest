"""Тесты поведенческих персон нагрузки (детерминированные профили кошельков).

Детерминированность: один адрес → один профиль. Разные адреса → разные
профили. Отключение (enabled: false) → нейтральный профиль без сюрпризов.
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.behavior import (
    WalletProfile,
    cfg_hi,
    day_of_week_factor,
    gas_multiplier,
    hour_window_factor,
    profile_for,
    time_of_day_factor,
)


def _cfg(**kw):
    cfg = {
        "farming": {"actions_per_cycle": [2, 6]},
        "behavior": {"enabled": True, **kw},
    }
    return cfg


ADDR_A = "0x" + "a" * 40
ADDR_B = "0x" + "b" * 40


class TestProfileDeterminism(unittest.TestCase):
    def test_same_address_same_profile(self):
        p1 = profile_for(ADDR_A, _cfg())
        p2 = profile_for(ADDR_A, _cfg())
        self.assertEqual(p1, p2)

    def test_different_addresses_differ(self):
        p1 = profile_for(ADDR_A, _cfg())
        p2 = profile_for(ADDR_B, _cfg())
        self.assertNotEqual(p1, p2)

    def test_config_change_changes_profile(self):
        p1 = profile_for(ADDR_A, _cfg(activity=[2.0, 2.0]))
        p2 = profile_for(ADDR_A, _cfg(activity=[0.1, 0.1]))
        self.assertNotEqual(p1.activity, p2.activity)


class TestProfileBounds(unittest.TestCase):
    def setUp(self):
        self.p = profile_for(ADDR_A, _cfg())

    def test_activity_in_range(self):
        self.assertGreaterEqual(self.p.activity, 0.1)
        self.assertLessEqual(self.p.activity, 3.0)

    def test_probabilities_clamped(self):
        for prob in (self.p.rest_prob, self.p.odd_amount_prob, self.p.faucet_skip_prob, self.p.burst_prob):
            self.assertGreaterEqual(prob, 0.0)
            self.assertLessEqual(prob, 1.0)

    def test_delay_scales_positive(self):
        self.assertGreater(self.p.delay_actions_scale, 0.0)
        self.assertGreater(self.p.delay_cycles_scale, 0.0)

    def test_amount_multipliers_positive(self):
        self.assertGreater(self.p.amount_min_mul, 0.0)
        self.assertGreater(self.p.amount_max_mul, 0.0)

    def test_actions_range_within_global(self):
        # Коридор числа действий не выходит за глобальный [2, 6].
        self.assertGreaterEqual(self.p.actions_lo, 2)
        self.assertLessEqual(self.p.actions_lo + self.p.actions_width - 1, 6)
        self.assertGreaterEqual(self.p.actions_width, 1)

    def test_gas_deviation_bounded(self):
        self.assertGreaterEqual(self.p.gas_deviation, 0.0)
        self.assertLessEqual(self.p.gas_deviation, 0.5)


class TestNeutralProfile(unittest.TestCase):
    def test_disabled_is_neutral(self):
        p = profile_for(ADDR_A, _cfg(enabled=False))
        self.assertTrue(p.neutral)
        self.assertEqual(p.activity, 1.0)
        self.assertEqual(p.action_multipliers, {})
        self.assertEqual(gas_multiplier(p), 1.0)

    def test_no_behavior_section_enabled_by_default(self):
        cfg = {"farming": {"actions_per_cycle": [1, 3]}}
        p = profile_for(ADDR_A, cfg)
        self.assertFalse(p.neutral)

    def test_gas_multiplier_clamped(self):
        p = profile_for(ADDR_A, _cfg(gas_deviation=0.5))
        for _ in range(50):
            self.assertGreaterEqual(gas_multiplier(p), 0.5)
            self.assertLessEqual(gas_multiplier(p), 2.0)

    def test_time_of_day_neutral_zero(self):
        self.assertEqual(time_of_day_factor(0.0), 1.0)

    def test_time_of_day_night_boost(self):
        import time as _time

        night = _time.struct_time((2026, 10, 16, 3, 0, 0, 3, 289, -1))
        with mock.patch("time.localtime", return_value=night):
            self.assertAlmostEqual(time_of_day_factor(1.0), 2.5)


class TestActionMultipliers(unittest.TestCase):
    def test_multipliers_cover_known_types(self):
        p = profile_for(ADDR_A, _cfg(action_mix=0.5))
        for atype in ("transfer", "vibevibe_swap", "vibevibe_mint", "flop_compute", "arc_trade"):
            self.assertIn(atype, p.action_multipliers)
            self.assertGreater(p.action_multipliers[atype], 0.0)

    def test_zero_mix_no_multipliers(self):
        p = profile_for(ADDR_A, _cfg(action_mix=0.0))
        self.assertEqual(p.action_multipliers, {})


class TestStartDelay(unittest.TestCase):
    def test_delay_clamped_low(self):
        p = profile_for(ADDR_A, _cfg(start_delay=[-1.0, 2.0]))
        self.assertGreaterEqual(p.start_delay, 0.0)
        self.assertLessEqual(p.start_delay, 2.0)

    def test_delay_clamped_high(self):
        p = profile_for(ADDR_A, _cfg(start_delay=[0.0, 999.0]))
        self.assertLessEqual(p.start_delay, 30.0)

    def test_delay_deterministic(self):
        p1 = profile_for(ADDR_A, _cfg(start_delay=[1.0, 5.0]))
        p2 = profile_for(ADDR_A, _cfg(start_delay=[1.0, 5.0]))
        self.assertEqual(p1.start_delay, p2.start_delay)

    def test_delay_default_within_range(self):
        p = profile_for(ADDR_A, _cfg())
        self.assertGreaterEqual(p.start_delay, 0.0)
        self.assertLessEqual(p.start_delay, 3.0)

    def test_delay_invalid_falls_back_zero(self):
        p = profile_for(ADDR_A, _cfg(start_delay="owo"))
        self.assertEqual(p.start_delay, 0.0)

    def test_delay_scalar_fixed_value(self):
        # Скаляр вместо диапазона (activity/rest_cycles/delay_scale/start_delay)
        # трактуется как фиксированное значение — раньше это ломало rng.uniform.
        p = profile_for(ADDR_A, _cfg(start_delay=2.0))
        self.assertAlmostEqual(p.start_delay, 2.0)

    def test_garbage_ranges_never_raise(self):
        # Мусор в behavior раньше ронял пул с ValueError (секция вообще
        # не валидируется): теперь — безопасный дефолт.
        p = profile_for(ADDR_A, _cfg(activity=["bad", "data"]))
        self.assertGreaterEqual(p.activity, 0.1)
        p = profile_for(ADDR_A, _cfg(start_delay=["x", "y"]))
        self.assertEqual(p.start_delay, 0.0)
        p = profile_for(ADDR_A, _cfg(activity=[1.5, 0.5]))
        self.assertGreaterEqual(p.activity, 0.1)
        # bool — не число: дефолт, а не activity=True→1.0 / краш.
        from core.behavior import _as_range as _ar

        self.assertEqual(_ar(True, (0.5, 1.5)), (0.5, 1.5))
        p = profile_for(ADDR_A, _cfg(activity=True, start_delay=True))
        self.assertEqual(p.start_delay, 0.0)
        self.assertGreaterEqual(p.activity, 0.5)
        self.assertLessEqual(p.activity, 1.5)

    def test_amount_corridor_never_inverted(self):
        for i in range(50):
            p = profile_for(f"0x{i:040x}", _cfg())
            self.assertLessEqual(p.amount_min_mul, p.amount_max_mul)

    def test_rest_can_hit_max(self):
        seen = {profile_for(f"0x{i:040x}", _cfg(rest_cycles=[1, 3])).rest_cycles[0] for i in range(300)}
        self.assertIn(3, seen)

    def test_delay_disabled_neutral(self):
        p = profile_for(ADDR_A, _cfg(enabled=False, start_delay=[50.0, 70.0]))
        self.assertEqual(p.start_delay, 0.0)


class TestScalarRanges(unittest.TestCase):
    """Скалярные значения диапазонов поведения не должны ломать профиль."""

    def test_activity_scalar(self):
        p = profile_for(ADDR_A, _cfg(activity=1.3))
        self.assertAlmostEqual(p.activity, 1.3)

    def test_rest_cycles_scalar(self):
        p = profile_for(ADDR_A, _cfg(rest_cycles=2))
        self.assertGreaterEqual(p.rest_cycles[0], 1)

    def test_delay_scale_scalar(self):
        p = profile_for(ADDR_A, _cfg(action_delay_scale=1.0, cycle_delay_scale=1.0))
        self.assertAlmostEqual(p.delay_actions_scale, 1.0)
        self.assertAlmostEqual(p.delay_cycles_scale, 1.0)


class TestRangeDraws(unittest.TestCase):
    """Пара [lo, hi] — розыгрыш на кошелёк; скаляр — как раньше (фиксировано)."""

    def test_scalar_stays_fixed(self):
        p = profile_for(
            ADDR_A,
            _cfg(rest_probability=0.05, burst_prob=0.07, odd_amount_prob=0.2,
                 faucet_skip_prob=0.1, daily_window=0.3, burst_multiplier=1.6),
        )
        self.assertAlmostEqual(p.rest_prob, 0.05)
        self.assertAlmostEqual(p.burst_prob, 0.07)
        self.assertAlmostEqual(p.odd_amount_prob, 0.2)
        self.assertAlmostEqual(p.faucet_skip_prob, 0.1)
        self.assertAlmostEqual(p.daily_window_strength, 0.3)
        self.assertAlmostEqual(p.burst_mult, 1.6)

    def test_range_draw_deterministic(self):
        kw = dict(rest_probability=[0.0, 1.0], burst_multiplier=[1.0, 5.0])
        p1 = profile_for(ADDR_A, _cfg(**kw))
        p2 = profile_for(ADDR_A, _cfg(**kw))
        self.assertEqual(p1.rest_prob, p2.rest_prob)
        self.assertEqual(p1.burst_mult, p2.burst_mult)

    def test_range_draw_varies_across_wallets(self):
        vals = {
            profile_for(f"0x{i:040x}", _cfg(odd_amount_prob=[0.0, 1.0])).odd_amount_prob
            for i in range(20)
        }
        self.assertGreater(len(vals), 1)

    def test_range_draw_clamped(self):
        p = profile_for(ADDR_A, _cfg(burst_multiplier=[10.0, 99.0], rest_probability=[-5.0, -1.0]))
        self.assertLessEqual(p.burst_mult, 5.0)
        self.assertGreaterEqual(p.rest_prob, 0.0)

    def test_cfg_hi(self):
        self.assertEqual(cfg_hi({"behavior": {"burst_multiplier": 1.6}}, "burst_multiplier", 1.6), 1.6)
        self.assertEqual(cfg_hi({"behavior": {"burst_multiplier": [1.3, 2.5]}}, "burst_multiplier", 1.6), 2.5)
        self.assertEqual(cfg_hi({}, "burst_multiplier", 1.6), 1.6)


class TestRhythmAxes(unittest.TestCase):
    """Новые оси: день недели и личное окно активности."""

    @staticmethod
    def _lt(hour: int, wday: int, minute: int = 0):
        import time as _t

        return _t.struct_time((2026, 10, 16, hour, minute, 0, wday, 289, -1))

    def test_weekend_zero_always_one(self):
        self.assertEqual(day_of_week_factor(0.0), 1.0)

    def test_weekend_saturday_reduces_skip(self):
        with mock.patch("time.localtime", return_value=self._lt(12, 5)):
            self.assertAlmostEqual(day_of_week_factor(1.0), 0.7)

    def test_weekend_monday_neutral(self):
        with mock.patch("time.localtime", return_value=self._lt(12, 0)):
            self.assertEqual(day_of_week_factor(1.0), 1.0)

    def test_hour_window_inside_outside(self):
        p = WalletProfile(day_start=9.0, day_width=8.0)
        with mock.patch("time.localtime", return_value=self._lt(12, 2)):
            self.assertEqual(hour_window_factor(p), 0.85)
        with mock.patch("time.localtime", return_value=self._lt(2, 2)):
            self.assertEqual(hour_window_factor(p), 1.8)

    def test_hour_window_wraps_midnight(self):
        p = WalletProfile(day_start=22.0, day_width=4.0)
        with mock.patch("time.localtime", return_value=self._lt(23, 2, 30)):
            self.assertEqual(hour_window_factor(p), 0.85)
        with mock.patch("time.localtime", return_value=self._lt(12, 2)):
            self.assertEqual(hour_window_factor(p), 1.8)

    def test_hour_window_neutral(self):
        self.assertEqual(hour_window_factor(WalletProfile(neutral=True)), 1.0)

    def test_rhythm_draws_bounded_deterministic_vary(self):
        kw = dict(weekend_strength=[0.0, 1.0], day_start=[0.0, 24.0], day_width=[1.0, 16.0])
        p1 = profile_for(ADDR_A, _cfg(**kw))
        p2 = profile_for(ADDR_A, _cfg(**kw))
        self.assertEqual((p1.weekend_strength, p1.day_start, p1.day_width),
                         (p2.weekend_strength, p2.day_start, p2.day_width))
        self.assertGreaterEqual(p1.weekend_strength, 0.0)
        self.assertLessEqual(p1.weekend_strength, 1.0)
        starts = {profile_for(f"0x{i:040x}", _cfg(**kw)).day_start for i in range(20)}
        self.assertGreater(len(starts), 1)

    def test_rhythm_defaults_off(self):
        p = profile_for(ADDR_A, _cfg())
        self.assertEqual(p.weekend_strength, 0.0)
        self.assertEqual(day_of_week_factor(p.weekend_strength), 1.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
