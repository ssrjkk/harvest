"""Тесты вспомогательных функций executors."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.actions import (
    _ADAPT_MIN_SAMPLES,
    _adaptive_scores,
    _is_valid_contract,
    _is_zero,
    _normalize_action,
)


class TestAdaptiveScores(unittest.TestCase):
    def test_no_history_keeps_config_weights(self):
        self.assertEqual(_adaptive_scores([1.0, 2.0], [[], []]), [1.0, 2.0])
        self.assertEqual(_adaptive_scores([0.5, 0.5], [[], []]), [0.5, 0.5])

    def test_high_success_amplifies(self):
        # 6 успехов из 10 => 1.0 * (0.5 + 0.6)
        rec = [True] * 6 + [False] * 4
        self.assertAlmostEqual(_adaptive_scores([1.0], [rec])[0], 1.1)

    def test_low_success_attenuates(self):
        rec = [False] * 2 + [True] * 1  # 1/3 => 1.0 * (0.5 + 0.333..)
        self.assertAlmostEqual(_adaptive_scores([1.0], [rec])[0], 5 / 6, places=5)

    def test_zero_success_after_min_samples_disables(self):
        under_threshold = [False] * (_ADAPT_MIN_SAMPLES - 1)
        self.assertGreater(_adaptive_scores([1.0], [under_threshold])[0], 0.0)
        over_threshold = [False] * _ADAPT_MIN_SAMPLES
        # С несколькими типами действий упавший обнуляется, живой остаётся.
        scores = _adaptive_scores([1.0, 1.0], [over_threshold, []])
        self.assertEqual(scores[0], 0.0)
        self.assertEqual(scores[1], 1.0)

    def test_all_disabled_falls_back_to_equal(self):
        outcomes = [[False] * _ADAPT_MIN_SAMPLES, [False] * _ADAPT_MIN_SAMPLES]
        self.assertEqual(_adaptive_scores([1.0, 1.0], outcomes), [1.0, 1.0])


class TestNormalizeAction(unittest.TestCase):
    def test_invalid_contract_action_becomes_transfer(self):
        out = _normalize_action({"type": "vibevibe_mint", "contract": "0x0000000000000000000000000000000000000000"})
        self.assertEqual(out["type"], "transfer")
        self.assertEqual(out["target"], "random_wallet")
        self.assertEqual(out["min_amount"], 0.0001)
        self.assertEqual(out["max_amount"], 0.001)

    def test_invalid_contract_keeps_configured_amounts(self):
        out = _normalize_action(
            {
                "type": "flop_validate",
                "contract": "0x0",
                "min_amount": 0.01,
                "max_amount": 0.02,
            }
        )
        self.assertEqual(out["type"], "transfer")
        self.assertEqual(out["min_amount"], 0.01)
        self.assertEqual(out["max_amount"], 0.02)

    def test_valid_contract_and_transfer_untouched(self):
        valid = {"type": "vibevibe_swap", "contract": "0x0000000000000000000000000000000000000001"}
        self.assertEqual(_normalize_action(valid), valid)
        tr = {"type": "transfer", "target": "random_wallet"}
        self.assertEqual(_normalize_action(tr), tr)

    def test_unknown_type_untouched(self):
        self.assertEqual(_normalize_action({"type": "unknown"}), {"type": "unknown"})


class TestIsZero(unittest.TestCase):
    def test_zero_addresses(self):
        self.assertTrue(_is_zero("0x0"))
        self.assertTrue(_is_zero("0x0000000000000000000000000000000000000000"))
        self.assertTrue(_is_zero("0X0000000000000000000000000000000000000000"))
        self.assertTrue(_is_zero(""))

    def test_nonzero_addresses(self):
        self.assertFalse(_is_zero("0x123"))
        self.assertFalse(_is_zero("0x1000000000000000000000000000000000000000"))

    def test_invalid_input(self):
        # Не пустой и не нулевой — это не "zero", а мусор; проверяем на уровне _is_valid_contract.
        self.assertFalse(_is_zero("0xzzz"))
        self.assertTrue(_is_zero(None))

    def test_is_valid_contract(self):
        checksum = "0x0000000000000000000000000000000000000001"
        self.assertTrue(_is_valid_contract(checksum))
        self.assertFalse(_is_valid_contract("0x0000000000000000000000000000000000000000"))
        self.assertFalse(_is_valid_contract(""))
        self.assertFalse(_is_valid_contract(None))
        self.assertFalse(_is_valid_contract("0xzzz"))
        self.assertFalse(_is_valid_contract("0x123"))


class TestAmountNormalization(unittest.TestCase):
    def test_inverted_range_swapped_on_init(self):
        from unittest.mock import MagicMock

        from core.actions import ActionExecutor

        config = {
            "actions": [
                {
                    "type": "transfer",
                    "target": "0x0000000000000000000000000000000000000001",
                    "min_amount": 0.005,
                    "max_amount": 0.001,
                }
            ],
            "advanced": {"gas_limit": 21000},
        }
        ex = ActionExecutor(MagicMock(), MagicMock(), MagicMock(), config)
        nom = ex.actions_conf[0]
        self.assertEqual(nom["min_amount"], 0.001)
        self.assertEqual(nom["max_amount"], 0.005)

    def test_ordered_range_untouched(self):
        from unittest.mock import MagicMock

        from core.actions import ActionExecutor

        config = {
            "actions": [
                {
                    "type": "transfer",
                    "target": "0x0000000000000000000000000000000000000001",
                    "min_amount": 0.001,
                    "max_amount": 0.005,
                }
            ],
            "advanced": {"gas_limit": 21000},
        }
        ex = ActionExecutor(MagicMock(), MagicMock(), MagicMock(), config)
        nom = ex.actions_conf[0]
        self.assertEqual(nom["min_amount"], 0.001)
        self.assertEqual(nom["max_amount"], 0.005)


if __name__ == "__main__":
    unittest.main()
