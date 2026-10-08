"""Тесты валидатора конфигурации: типы, диапазоны, обязательные секции."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config_validate import (
    ConfigError,
    _check_https_or_localhost,
    _check_path,
    _check_range,
    _check_range_pair,
    config_warnings,
    validate_config,
)


def good_config() -> dict:
    return {
        "network": {"rpc_url": "https://example.com/rpc", "chain_id": 288},
        "wallets": {"count": 2, "seed": ""},
        "faucet": {
            "enabled": False,
            "min_balance": 0.005,
            "target_balance": 0.05,
            "retries": 3,
        },
        "actions": [
            {
                "type": "transfer",
                "target": "random_wallet",
                "weight": 1.0,
                "min_amount": 0.0001,
                "max_amount": 0.001,
            },
            {
                "type": "vibevibe_swap",
                "contract": "0x0000000000000000000000000000000000000001",
                "method": "swap",
                "weight": 2.0,
            },
        ],
        "farming": {
            "actions_per_cycle": [3, 8],
            "delay_between_actions": [5, 20],
            "delay_between_cycles": [3600, 7200],
            "skip_cycle_probability": 0.05,
        },
        "advanced": {"gas_limit": 100000, "min_gas_for_action": 0.003},
        "threading": {"max_workers": 20, "timeout_per_wallet": 0},
        "database": {"path": "farming_state.db", "master_key": "master.key"},
    }


class TestLoggingAndPaths(unittest.TestCase):
    def test_bad_log_level_rejected(self):
        cfg = good_config()
        cfg["logging"] = {"level": "BOGUS"}
        with self.assertRaises(ConfigError):
            validate_config(cfg)

    def test_lowercase_valid_log_level_ok(self):
        cfg = good_config()
        cfg["logging"] = {"level": "info"}
        validate_config(cfg)

    def test_path_traversal_database_rejected(self):
        cfg = good_config()
        cfg["database"] = {"path": "../../evil.db"}
        with self.assertRaises(ConfigError):
            validate_config(cfg)

    def test_path_traversal_logfile_rejected(self):
        cfg = good_config()
        cfg["logging"] = {"file": "logs/../../evil.log"}
        with self.assertRaises(ConfigError):
            validate_config(cfg)

    def test_absolute_db_path_ok(self):
        cfg = good_config()
        cfg["database"] = {"path": "C:/data/farming_state.db"}
        validate_config(cfg)


class TestPlaceholderWarnings(unittest.TestCase):
    """config_warnings() — fail-loud предупреждения о заглушках (не ошибки)."""

    def test_clean_config_no_warnings(self):
        self.assertEqual(config_warnings(good_config()), [])

    def test_zero_contract_warned(self):
        cfg = good_config()
        cfg["actions"][1]["contract"] = "0x0000000000000000000000000000000000000000"
        warnings = config_warnings(cfg)
        self.assertTrue(any("0x0" in w and "vibevibe_swap" in w for w in warnings))
        # Заглушка не мешает валидации — это замечание, а не ошибка.
        validate_config(cfg)

    def test_chain_id_placeholder_warned(self):
        cfg = good_config()
        cfg["network"]["chain_id"] = 99999
        warnings = config_warnings(cfg)
        self.assertTrue(any("99999" in w and "chain_id" in w for w in warnings))

    def test_transfer_not_warned(self):
        cfg = good_config()
        cfg["actions"] = [
            {"type": "transfer", "target": "random_wallet", "weight": 1.0,
             "min_amount": 0.0001, "max_amount": 0.001}
        ]
        self.assertEqual(config_warnings(cfg), [])


class TestConfigValidate(unittest.TestCase):
    def test_good(self):
        validate_config(good_config())

    def test_missing_sections(self):
        cfg = good_config()
        cfg.pop("network")
        with self.assertRaises(ConfigError):
            validate_config(cfg)

        cfg = good_config()
        cfg.pop("farming")
        with self.assertRaises(ConfigError):
            validate_config(cfg)

        cfg = good_config()
        cfg.pop("actions")
        with self.assertRaises(ConfigError):
            validate_config(cfg)

    def test_bad_rpc_type(self):
        cfg = good_config()
        cfg["network"]["rpc_url"] = 42
        with self.assertRaises(ConfigError):
            validate_config(cfg)

    def test_empty_rpc_list(self):
        cfg = good_config()
        cfg["network"]["rpc_url"] = []
        with self.assertRaises(ConfigError):
            validate_config(cfg)

    def test_rpc_list_ok(self):
        cfg = good_config()
        cfg["network"]["rpc_url"] = ["https://a/rpc", "https://b/rpc"]
        validate_config(cfg)

    def test_reversed_range(self):
        cfg = good_config()
        cfg["farming"]["delay_between_cycles"] = [100, 10]
        with self.assertRaises(ConfigError):
            validate_config(cfg)

    def test_bad_skip_probability(self):
        cfg = good_config()
        cfg["farming"]["skip_cycle_probability"] = 1.5
        with self.assertRaises(ConfigError):
            validate_config(cfg)

    def test_unknown_action(self):
        cfg = good_config()
        cfg["actions"].append({"type": "drain_account", "weight": 1})
        with self.assertRaises(ConfigError):
            validate_config(cfg)

    def test_min_amount_below_zero(self):
        cfg = good_config()
        cfg["actions"][0]["min_amount"] = -1
        with self.assertRaises(ConfigError):
            validate_config(cfg)

    def test_missing_database_path(self):
        cfg = good_config()
        cfg["database"]["path"] = ""
        with self.assertRaises(ConfigError):
            validate_config(cfg)

    def test_bad_workers(self):
        cfg = good_config()
        cfg["threading"]["max_workers"] = 0
        with self.assertRaises(ConfigError):
            validate_config(cfg)

    def test_gas_limit_ok_with_low_for_placeholder_contracts(self):
        # Заглушки (0x0) в runtime падают на transfer — им 21000 достаточно.
        cfg = good_config()
        cfg["advanced"]["gas_limit"] = 21000
        for a in cfg["actions"]:
            if a["type"] != "transfer":
                a["contract"] = "0x0000000000000000000000000000000000000000"
        validate_config(cfg)

    def test_gas_limit_too_low_for_real_contract(self):
        cfg = good_config()
        cfg["advanced"]["gas_limit"] = 21000
        with self.assertRaises(ConfigError):
            validate_config(cfg)

    def test_faucet_strategies_none(self):
        cfg = good_config()
        cfg["faucet"]["strategies"] = None
        with self.assertRaises(ConfigError):
            validate_config(cfg)

    def test_faucet_strategies_not_a_list(self):
        cfg = good_config()
        cfg["faucet"]["strategies"] = "not-a-list"
        with self.assertRaises(ConfigError):
            validate_config(cfg)

    def test_faucet_strategies_item_not_dict(self):
        cfg = good_config()
        cfg["faucet"]["strategies"] = ["direct", {"url": "https://faucet.example/claim"}]
        with self.assertRaises(ConfigError):
            validate_config(cfg)

    def test_faucet_strategy_url_required(self):
        cfg = good_config()
        cfg["faucet"]["strategies"] = [{"type": "direct"}]
        with self.assertRaises(ConfigError):
            validate_config(cfg)

    def test_faucet_strategy_plain_http_rejected(self):
        cfg = good_config()
        cfg["faucet"]["strategies"] = [{"type": "direct", "url": "http://faucet.example/claim"}]
        with self.assertRaises(ConfigError):
            validate_config(cfg)

    def test_faucet_strategies_valid(self):
        cfg = good_config()
        cfg["faucet"]["strategies"] = [
            {"type": "direct", "url": "https://faucet.example/claim"},
            {"type": "direct", "url": "http://127.0.0.1:8080/drip"},
        ]
        validate_config(cfg)


class TestMoreBranches(unittest.TestCase):
    def _bad(self, mutate):
        cfg = good_config()
        mutate(cfg)
        with self.assertRaises(ConfigError):
            validate_config(cfg)

    def test_actions_not_a_list(self):
        self._bad(lambda c: c.__setitem__("actions", {"type": "transfer"}))

    def test_action_item_not_dict(self):
        self._bad(lambda c: c["actions"].append("nope"))

    def test_action_missing_type(self):
        self._bad(lambda c: c["actions"].append({"weight": 1}))

    def test_action_weight_negative(self):
        self._bad(lambda c: c["actions"][0].__setitem__("weight", -1))

    def test_min_amount_not_number(self):
        self._bad(lambda c: c["actions"][0].__setitem__("min_amount", "x"))

    def test_max_amount_not_number(self):
        self._bad(lambda c: c["actions"][0].__setitem__("max_amount", "x"))

    def test_max_amount_negative(self):
        self._bad(lambda c: c["actions"][0].__setitem__("max_amount", -1))

    def test_min_greater_than_max(self):
        def mutate(c):
            c["actions"][0]["min_amount"] = 5
            c["actions"][0]["max_amount"] = 1

        self._bad(mutate)

    def test_transfer_missing_target(self):
        self._bad(lambda c: c["actions"][0].pop("target"))

    def test_transfer_bad_target(self):
        self._bad(lambda c: c["actions"][0].__setitem__("target", "not-an-address"))

    def test_contract_missing(self):
        self._bad(lambda c: c["actions"].append({"type": "vibevibe_swap", "weight": 1}))

    def test_contract_bad_address(self):
        self._bad(lambda c: c["actions"].append({"type": "vibevibe_swap", "contract": "0xzz", "weight": 1}))

    def test_contract_call_bad_method(self):
        self._bad(
            lambda c: c["actions"].append(
                {"type": "contract_call", "contract": "0x0000000000000000000000000000000000000001",
                 "method": "0xZZZZ", "weight": 1}
            )
        )

    def test_contract_action_missing_method(self):
        self._bad(
            lambda c: c["actions"].append(
                {"type": "vibevibe_swap", "contract": "0x0000000000000000000000000000000000000001", "weight": 1}
            )
        )

    def test_vibevibe_buy_method_not_required(self):
        cfg = good_config()
        cfg["actions"].append(
            {"type": "vibevibe_buy", "contract": "0x0000000000000000000000000000000000000001", "weight": 1}
        )
        validate_config(cfg)

    def test_wallets_bad_count(self):
        self._bad(lambda c: c["wallets"].__setitem__("count", -1))

    def test_faucet_min_greater_than_target(self):
        def mutate(c):
            c["faucet"]["min_balance"] = 1.0
            c["faucet"]["target_balance"] = 0.5

        self._bad(mutate)

    def test_threading_tuning_ranges(self):
        self._bad(lambda c: c["threading"].__setitem__("gen_workers", 33))
        self._bad(lambda c: c["threading"].__setitem__("rpc_threads", 257))
        self._bad(lambda c: c["threading"].__setitem__("chunk_size", -1))

    def test_cache_ranges(self):
        self._bad(lambda c: c.__setitem__("cache", {"rpc_rate_limit": 0}))
        self._bad(lambda c: c.__setitem__("cache", {"rpc_rate_floor": 5}))

    def test_license_cache_file_traversal(self):
        self._bad(lambda c: c.__setitem__("license", {"cache_file": "../evil"}))

    def test_config_warnings_skips_non_dict_action(self):
        cfg = good_config()
        cfg["actions"].append("garbage")
        # не падает, не-словарные действия пропускаются
        self.assertIsInstance(config_warnings(cfg), list)

    def test_check_range_direct_raises(self):
        with self.assertRaises(ConfigError):
            _check_range("x", "not-a-number")

    def test_check_range_pair_direct_raises(self):
        with self.assertRaises(ConfigError):
            _check_range_pair("x", "not-a-number")

    def test_check_range_pair_wrong_length(self):
        with self.assertRaises(ConfigError):
            _check_range_pair("x", [1, 2, 3])

    def test_check_range_pair_non_numbers(self):
        with self.assertRaises(ConfigError):
            _check_range_pair("x", ["a", "b"])

    def test_check_range_pair_negative_min(self):
        with self.assertRaises(ConfigError):
            _check_range_pair("x", [-1, 5])

    def test_check_range_pair_accepts_number(self):
        _check_range_pair("x", 5)  # одно число — допустимо

    def test_contract_call_selector_bad_hex(self):
        # длина 10 (0x+8), но не hex — должна сработать проверка _is_hex
        self._bad(
            lambda c: c["actions"].append(
                {"type": "contract_call", "contract": "0x0000000000000000000000000000000000000001",
                 "method": "0x1234567z", "weight": 1}
            )
        )

    def test_https_check_empty_url_noop(self):
        errors: list[str] = []
        _check_https_or_localhost("x", "", errors)
        self.assertEqual(errors, [])

    def test_check_path_empty_noop(self):
        errors: list[str] = []
        _check_path("x", "", errors)
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
