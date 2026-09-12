"""Тесты индивидуального поведения кошельков (анти-сибил).

Детерминированность: один адрес → один профиль. Разные адреса → разные
профили. Отключение (enabled: false) → нейтральный профиль без сюрпризов.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.behavior import gas_multiplier, profile_for, time_of_day_factor


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


class TestActionMultipliers(unittest.TestCase):
    def test_multipliers_cover_known_types(self):
        p = profile_for(ADDR_A, _cfg(action_mix=0.5))
        for atype in ("transfer", "vibevibe_swap", "vibevibe_mint", "flop_compute", "arc_trade"):
            self.assertIn(atype, p.action_multipliers)
            self.assertGreater(p.action_multipliers[atype], 0.0)

    def test_zero_mix_no_multipliers(self):
        p = profile_for(ADDR_A, _cfg(action_mix=0.0))
        self.assertEqual(p.action_multipliers, {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
