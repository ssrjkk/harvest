"""Тесты утилит: env-overrides, форматирование адресов, случайная задержка."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.utils import apply_env_overrides, resolve_bundled, truncate_address


class TestEnvOverrides(unittest.TestCase):
    ENVS = [
        "FARMER_RPC_URL",
        "FARMER_CHAIN_ID",
        "FARMER_WALLET_COUNT",
        "FARMER_WORKERS",
        "FARMER_MIN_BALANCE",
        "FARMER_TARGET_BALANCE",
        "FARMER_LOG_LEVEL",
        "FARMER_DB_PATH",
        "FARMER_GEN_WORKERS",
        "FARMER_RPC_THREADS",
        "FARMER_CHUNK_SIZE",
        "FARMER_FAUCET_MAX_CONCURRENT",
        "FARMER_RPC_RATE",
        "FARMER_RPC_RATE_FLOOR",
    ]

    def setUp(self):
        for k in self.ENVS:
            os.environ.pop(k, None)

    def tearDown(self):
        for k in self.ENVS:
            os.environ.pop(k, None)

    def test_int_override(self):
        os.environ["FARMER_WALLET_COUNT"] = "7"
        cfg = apply_env_overrides({})
        self.assertEqual(cfg["wallets"]["count"], 7)

    def test_float_override(self):
        os.environ["FARMER_MIN_BALANCE"] = "0.05"
        cfg = apply_env_overrides({})
        self.assertAlmostEqual(cfg["faucet"]["min_balance"], 0.05)

    def test_string_override(self):
        os.environ["FARMER_RPC_URL"] = "https://env/rpc"
        cfg = apply_env_overrides({})
        self.assertEqual(cfg["network"]["rpc_url"], "https://env/rpc")

    def test_rpc_url_list_via_semicolon(self):
        os.environ["FARMER_RPC_URL"] = "https://a/rpc ; https://b/rpc ; https://c/rpc"
        cfg = apply_env_overrides({})
        self.assertEqual(
            cfg["network"]["rpc_url"],
            ["https://a/rpc", "https://b/rpc", "https://c/rpc"],
        )

    def test_rpc_url_list_via_comma(self):
        os.environ["FARMER_RPC_URL"] = "https://a/rpc,https://b/rpc"
        cfg = apply_env_overrides({})
        self.assertEqual(cfg["network"]["rpc_url"], ["https://a/rpc", "https://b/rpc"])

    def test_rpc_url_single_kept_string(self):
        # Один эндпоинт без разделителей остаётся строкой (совместимость со схемой).
        os.environ["FARMER_RPC_URL"] = "https://single/rpc"
        cfg = apply_env_overrides({})
        self.assertEqual(cfg["network"]["rpc_url"], "https://single/rpc")

    def test_no_override_without_env(self):
        cfg = apply_env_overrides({"network": {"rpc_url": "x"}})
        self.assertEqual(cfg["network"]["rpc_url"], "x")

    def test_gen_workers_override(self):
        os.environ["FARMER_GEN_WORKERS"] = "8"
        cfg = apply_env_overrides({})
        self.assertEqual(cfg["threading"]["gen_workers"], 8)

    def test_rpc_threads_override(self):
        os.environ["FARMER_RPC_THREADS"] = "128"
        cfg = apply_env_overrides({})
        self.assertEqual(cfg["threading"]["rpc_threads"], 128)

    def test_chunk_size_override(self):
        os.environ["FARMER_CHUNK_SIZE"] = "250"
        cfg = apply_env_overrides({})
        self.assertEqual(cfg["threading"]["chunk_size"], 250)

    def test_faucet_max_concurrent_override(self):
        os.environ["FARMER_FAUCET_MAX_CONCURRENT"] = "20"
        cfg = apply_env_overrides({})
        self.assertEqual(cfg["faucet"]["max_concurrent"], 20)

    def test_rpc_rate_override(self):
        os.environ["FARMER_RPC_RATE"] = "200"
        cfg = apply_env_overrides({})
        self.assertEqual(cfg["cache"]["rpc_rate_limit"], 200)

    def test_rpc_rate_floor_float(self):
        os.environ["FARMER_RPC_RATE_FLOOR"] = "0.35"
        cfg = apply_env_overrides({})
        self.assertAlmostEqual(cfg["cache"]["rpc_rate_floor"], 0.35)

    def test_unicode_digits_kept_as_string(self):
        # Раунд 14: isdigit() принимает Unicode-цифры («١٢٣»), а int() на них
        # падает ValueError — раньше значение env ломало применение конфига.
        for bad in ("١٢٣", "１２３", "⁶⁶"):
            os.environ["FARMER_WALLET_COUNT"] = bad
            cfg = apply_env_overrides({})
            v = cfg["wallets"]["count"]
            self.assertIsInstance(v, str)
            self.assertEqual(v, bad)

    def test_negative_unicode_digits_kept_as_string(self):
        os.environ["FARMER_WALLET_COUNT"] = "-١٢٣"
        cfg = apply_env_overrides({})
        self.assertEqual(cfg["wallets"]["count"], "-١٢٣")


class TestResolveBundled(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self._old_meipass = getattr(sys, "_MEIPASS", None)
        if hasattr(sys, "_MEIPASS"):
            del sys.__dict__["_MEIPASS"]

    def tearDown(self):
        self._td.cleanup()
        if self._old_meipass is not None:
            sys.__dict__["_MEIPASS"] = self._old_meipass
        elif hasattr(sys, "_MEIPASS"):
            del sys.__dict__["_MEIPASS"]

    def test_existing_relative_kept(self):
        p = resolve_bundled("tests/test_utils.py")
        self.assertEqual(p, Path("tests/test_utils.py"))

    def test_absolute_kept(self):
        p = resolve_bundled(str(Path(Path(__file__).resolve().parent) / "test_utils.py"))
        self.assertEqual(p.is_absolute(), True)

    def test_meipass_fallback(self):
        bundled = self._td.name
        rel = "bundle/abi/vibevibe.json"  # в рабочем каталоге такого нет
        target = Path(bundled) / "bundle" / "abi" / "vibevibe.json"
        target.parent.mkdir(parents=True)
        target.write_text("{}", encoding="utf-8")
        sys.__dict__["_MEIPASS"] = bundled
        p = resolve_bundled(rel)
        self.assertEqual(p, target)

    def test_missing_returns_rel(self):
        p = resolve_bundled("nope/not_here.json")
        self.assertEqual(p, Path("nope/not_here.json"))


class TestTruncate(unittest.TestCase):
    def test_short(self):
        self.assertEqual(truncate_address("0x1234"), "0x1234")

    def test_normal(self):
        addr = "0x" + "a" * 40
        self.assertEqual(truncate_address(addr), "0xaaaaaa...aaaaaa")

    def test_empty(self):
        self.assertEqual(truncate_address(""), "0x...")
        self.assertEqual(truncate_address(None), "0x...")

    def test_not_hex(self):
        # короткие строки возвращаются как есть
        self.assertEqual(truncate_address("hello world"), "hello world")
        # длинные — обрезаются по краям и получают многоточие
        cut = truncate_address("z" * 30)
        self.assertIn("...", cut)
        self.assertTrue(cut.startswith("zzzzzzzz"))
        self.assertTrue(cut.endswith("zzzzzz"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
