"""Тесты валидатора конфигурации: типы, диапазоны, обязательные секции."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config_validate import ConfigError, config_warnings, validate_config


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
