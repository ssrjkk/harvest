"""Easy core coverage: config_validate, crypto, faucet, performance, version, wallet, behavior."""

import asyncio
import json
import os
import random
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.behavior import (
    WalletProfile,
    _as_range,
    _draw_bounded,
    cfg_hi,
    hour_window_factor,
    profile_for,
    time_of_day_factor,
)
from core.config_validate import ConfigError, validate_config
from core.crypto import MasterKeyError, decrypt_seed, resolve_master_key
from core.faucet import Faucet
from core.performance import _configured
from core.version import version_line
from core.wallet import WalletManager


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


class TestConfigRpcListBadElement(unittest.TestCase):
    def test_rpc_list_with_non_string_rejected(self):
        cfg = good_config()
        cfg["network"]["rpc_url"] = ["https://x", 123]
        with self.assertRaises(ConfigError) as ctx:
            validate_config(cfg)
        self.assertIn("rpc_url", str(ctx.exception))


class TestCryptoEasy(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("FARMER_MASTER_KEY", None)

    def test_resolve_master_key_read_error(self):
        os.environ.pop("FARMER_MASTER_KEY", None)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FARMER_MASTER_KEY", None)
            with mock.patch("core.crypto.Path") as MockPath:
                inst = mock.MagicMock()
                inst.exists.return_value = True
                inst.read_bytes.side_effect = OSError("disk fail")
                MockPath.return_value = inst
                with self.assertRaises(MasterKeyError):
                    resolve_master_key({"database": {"master_key": "master.key"}})

    def test_decrypt_seed_none_key_enc_value(self):
        with self.assertRaises(MasterKeyError):
            decrypt_seed(None, "enc:00112233445566778899aabbcc")


class TestFaucetEasy(unittest.TestCase):
    def test_chainstack_probe_no_key_false(self):
        f = Faucet(
            {"faucet": {"strategies": [{"type": "chainstack", "url": "https://mcp.example/mcp"}]}, "proxy": {}}
        )
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CHAINSTACK_API_KEY", None)
            ok, reason = asyncio.run(f._chainstack_probe(mock.MagicMock(), "https://mcp.example/mcp"))
        self.assertFalse(ok)
        self.assertIn("CHAINSTACK_API_KEY", reason)

    def test_probe_timeout_reason(self):
        f = Faucet(
            {"faucet": {"strategies": [{"type": "direct", "url": "https://example.com/claim"}]}, "proxy": {}}
        )
        sess = mock.MagicMock()
        sess.get.side_effect = TimeoutError("boom")
        sess.closed = False
        f._session = sess
        live = asyncio.run(f.validate())
        self.assertEqual(live, 0)
        self.assertEqual(len(f._last_probe), 1)
        self.assertFalse(f._last_probe[0]["alive"])
        self.assertIn("timeout", f._last_probe[0]["reason"].lower())

    def test_probe_gather_keyboard_interrupt(self):
        f = Faucet(
            {"faucet": {"strategies": [{"type": "direct", "url": "https://example.com/claim"}]}, "proxy": {}}
        )
        sess = mock.MagicMock()
        sess.closed = False
        f._session = sess

        async def _fake_gather(*coros, **kwargs):
            for c in coros:
                try:
                    c.close()
                except Exception:
                    pass
            return [KeyboardInterrupt()]

        with mock.patch("core.faucet.asyncio.gather", new=_fake_gather):
            live = asyncio.run(f.validate())
        self.assertEqual(live, 0)
        self.assertFalse(f._last_probe[0]["alive"])


class TestPerformanceEasy(unittest.TestCase):
    def test_configured_garbage_returns_zero(self):
        self.assertEqual(_configured({"threading": {"max_workers": "oops"}}, "max_workers"), 0)


class TestVersionEasy(unittest.TestCase):
    def test_version_line(self):
        self.assertEqual(version_line(), "harvest v2.3.0")


class TestWalletEasy(unittest.TestCase):
    def test_generate_wallet_enable_raises_still_works(self):
        with mock.patch(
            "core.wallet.Account.enable_unaudited_hdwallet_features", side_effect=Exception("boom")
        ):
            with mock.patch("core.wallet.Account.create_with_mnemonic") as m_cwm:
                fake = mock.MagicMock()
                fake.address = "0x" + "aa" * 20
                fake.key.hex.return_value = "bb" * 32
                m_cwm.return_value = (fake, "abandon " * 11 + "about")
                wm = WalletManager({"threading": {"gen_workers": 1}}, mock.MagicMock())
                w = wm.generate_wallet()
        self.assertIn("address", w)
        self.assertIn("private_key", w)
        self.assertIn("mnemonic", w)

    def test_generate_wallet_no_mnemonic_attr(self):
        fake_acc_cls = mock.MagicMock(spec=["create", "enable_unaudited_hdwallet_features"])
        fake_inst = mock.MagicMock()
        fake_inst.address = "0x" + "11" * 20
        fake_inst.key.hex.return_value = "cc" * 32
        fake_inst.mnemonic = "test mnemonic words"
        fake_acc_cls.create.return_value = fake_inst
        fake_acc_cls.enable_unaudited_hdwallet_features.return_value = None
        with mock.patch("core.wallet.Account", fake_acc_cls):
            wm = WalletManager({"threading": {"gen_workers": 1}}, mock.MagicMock())
            w = wm.generate_wallet()
        self.assertEqual(w["address"], "0x" + "11" * 20)

    def test_export_to_file(self):
        wallets = [
            {"address": "0x" + "aa" * 20, "private_key": "0x" + "bb" * 32, "mnemonic": "word " * 12}
        ]
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "w.json")
            ret = WalletManager.export_to_file(wallets, path)
            self.assertEqual(ret, path)
            self.assertTrue(os.path.exists(path))
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            self.assertEqual(data[0]["address"], wallets[0]["address"])


class TestBehaviorEasy(unittest.TestCase):
    def test_draw_bounded_reversed_pair(self):
        rng = random.Random(0)
        cfg = {"behavior": {"k": [10, 2]}}
        v = _draw_bounded(rng, cfg, "k", 0.0, 0.0, 100.0)
        self.assertGreaterEqual(v, 2.0)
        self.assertLessEqual(v, 10.0)

    def test_draw_bounded_garbage(self):
        rng = random.Random(0)
        cfg = {"behavior": {"k": "oops"}}
        v = _draw_bounded(rng, cfg, "k", 5.0, 0.0, 10.0)
        self.assertEqual(v, 5.0)

    def test_cfg_hi_pair_garbage(self):
        self.assertEqual(cfg_hi({"behavior": {"x": ["a", "b"]}}, "x", 7.0), 7.0)

    def test_cfg_hi_scalar_garbage(self):
        self.assertEqual(cfg_hi({"behavior": {"x": "oops"}}, "x", 7.0), 7.0)

    def test_as_range_garbage(self):
        self.assertEqual(_as_range("owo", (1, 3)), (1.0, 3.0))

    def test_profile_scalar_actions_and_mix_and_gas(self):
        cfg = {
            "farming": {"actions_per_cycle": 5},
            "behavior": {"enabled": True, "action_mix": 1.5, "gas_deviation": "oops"},
        }
        p = profile_for("0x" + "a" * 40, cfg)
        self.assertEqual(p.actions_lo, 5)
        self.assertEqual(p.actions_width, 1)
        self.assertTrue(len(p.action_multipliers) > 0)
        self.assertGreaterEqual(p.gas_deviation, 0.0)
        self.assertLessEqual(p.gas_deviation, 0.06)

    def test_time_of_day_zero(self):
        self.assertEqual(time_of_day_factor(0), 1.0)

    def test_time_of_day_noon(self):
        import time as _t

        lt = _t.struct_time((2026, 10, 16, 12, 0, 0, 3, 289, -1))
        with mock.patch("time.localtime", return_value=lt):
            self.assertAlmostEqual(time_of_day_factor(1.0), 0.75)

    def test_hour_window_wide(self):
        p = WalletProfile(day_start=9.0, day_width=8.0)
        with mock.patch("builtins.min", return_value=30.0):
            self.assertEqual(hour_window_factor(p), 1.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
