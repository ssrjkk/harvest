"""Тесты ops-логики бота (portal/bot_ops.py): конструкторы /cmd и /cfg,
проверка YAML /check, справочник и форматирование в стиле сайта."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from portal import bot_ops as ops


class TestParseHelpers(unittest.TestCase):
    def test_parse_count(self):
        self.assertEqual(ops.parse_count("50"), 50)
        for bad in ["", "xx", "0", "-1", "5001", "1.5", None]:
            self.assertIsNone(ops.parse_count(bad))  # type: ignore[arg-type]

    def test_parse_cycles(self):
        self.assertEqual(ops.parse_cycles("0"), 0)
        self.assertEqual(ops.parse_cycles("100000"), 100000)
        for bad in ["", "xx", "-1", "100001", None]:
            self.assertIsNone(ops.parse_cycles(bad))  # type: ignore[arg-type]

    def test_known_slug(self):
        self.assertTrue(ops.known_slug("vibe"))
        self.assertTrue(ops.known_slug("ARC"))
        self.assertFalse(ops.known_slug("nope"))
        self.assertFalse(ops.known_slug(""))


class TestBuildCommand(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(
            ops.build_command("vibevibe"),
            "python auto.py --config config_vibevibe.yaml --wallets 50 --cycles 5",
        )

    def test_full_args_and_alias(self):
        self.assertEqual(
            ops.build_command("vibe", 10, 2, True),
            "python auto.py --config config_vibevibe.yaml --wallets 10 --cycles 2 --skip-faucet",
        )
        self.assertIn("config.simple.yaml", ops.build_command("simple", 1, 0))

    def test_bad_args_raise(self):
        for args in [("nope", 50, 5), ("vibe", 0, 5), ("vibe", 5001, 5), ("vibe", 50, -1)]:
            with self.assertRaises(ValueError, msg=str(args)):
                ops.build_command(*args)

    def test_parse_usage_and_errors(self):
        self.assertIn("/cmd", ops.parse_cmd_args("/cmd"))
        out = ops.parse_cmd_args("/cmd vibe 50 5")
        self.assertIn("python auto.py", out)
        self.assertIn("config_vibevibe.yaml", out)
        self.assertIn("[err]", ops.parse_cmd_args("/cmd vibe xx 5"))
        self.assertIn("[err]", ops.parse_cmd_args("/cmd nope 1 1"))


class TestBuildConfigYaml(unittest.TestCase):
    def test_patches_count_and_validates(self):
        name, text = ops.build_config_yaml("arc", 7)
        self.assertEqual(name, "config_arc.yaml")
        self.assertIn("count: 7", text)
        self.assertIn("chain_id: 5042002", text)

    def test_unknown_and_range(self):
        with self.assertRaises(ValueError):
            ops.build_config_yaml("nope", 10)
        with self.assertRaises(ValueError):
            ops.build_config_yaml("vibe", 0)

    def test_missing_and_broken_files(self):
        import unittest.mock as mock

        with mock.patch.dict(ops._CMD_NETS, {"ghost": "ghost.yaml"}):
            with self.assertRaises(ValueError):
                ops.build_config_yaml("ghost", 5)
        with tempfile.TemporaryDirectory() as td:
            bad = os.path.join(td, "bad.yaml")
            Path(bad).write_text("- just\n- a list\n", encoding="utf-8")
            with mock.patch.dict(ops._CMD_NETS, {"bad": "bad.yaml"}):
                with mock.patch.object(ops, "_ROOT", td):
                    with self.assertRaises(ValueError):
                        ops.build_config_yaml("bad", 5)
            fail = os.path.join(td, "fail.yaml")
            Path(fail).write_text(
                "network:\n  rpc_url: https://x.example/\n  chain_id: 1\n"
                "wallets:\n  count: 5\n",
                encoding="utf-8",
            )
            with mock.patch.dict(ops._CMD_NETS, {"fail": "fail.yaml"}):
                with mock.patch.object(ops, "_ROOT", td):
                    with self.assertRaises(ValueError):
                        ops.build_config_yaml("fail", 5)
            nowal = os.path.join(td, "nowal.yaml")
            Path(nowal).write_text(
                "network:\n  rpc_url: https://x.example/\n  chain_id: 1\n",
                encoding="utf-8",
            )
            with mock.patch.dict(ops._CMD_NETS, {"nowal": "nowal.yaml"}):
                with mock.patch.object(ops, "_ROOT", td):
                    with self.assertRaises(ValueError):
                        ops.build_config_yaml("nowal", 5)


class TestCheckConfig(unittest.TestCase):
    GOOD = (
        "network:\n  rpc_url: https://rpc.testnet.chain.robinhood.com\n  chain_id: 46630\n"
        "wallets:\n  count: 5\nfaucet:\n  enabled: true\n  min_balance: 0.005\n"
        "  target_balance: 0.05\nactions:\n  - type: transfer\n    target: random_wallet\n"
        "farming:\n  actions_per_cycle: [3, 8]\nthreading:\n  max_workers: 20\n"
        "database:\n  path: farming_state.db\nadvanced:\n  gas_limit: 100000\n"
    )

    def test_good_config_ok(self):
        ok, lines = ops.check_config_text(self.GOOD)
        self.assertTrue(ok)
        self.assertTrue(any("[ok]" in line for line in lines))

    def test_bad_config_reports_without_echo(self):
        bad = self.GOOD.replace("chain_id: 46630", "chain_id: 0")
        ok, lines = ops.check_config_text(bad)
        self.assertFalse(ok)
        self.assertTrue(any("[err]" in line for line in lines))
        # Входной текст не цитируется обратно.
        self.assertNotIn("target_balance", "\n".join(lines))

    def test_malformed_and_huge(self):
        ok, _ = ops.check_config_text("network: [unclosed")
        self.assertFalse(ok)
        ok, _ = ops.check_config_text("- just\n- a\n- list\n")
        self.assertFalse(ok)
        ok, lines = ops.check_config_text("x" * (16 * 1024 + 1))
        self.assertFalse(ok)
        self.assertIn("16", lines[0])

    def test_secrets_redacted(self):
        evil = self.GOOD.replace(
            "https://rpc.testnet.chain.robinhood.com",
            "http://user:pass@evil.example/rpc?api_key=SECRET",
        )
        ok, lines = ops.check_config_text(evil)
        self.assertFalse(ok)
        blob = "\n".join(lines)
        self.assertNotIn("SECRET", blob)
        self.assertNotIn("user:pass", blob)


class TestOpsFormat(unittest.TestCase):
    def _data(self, **kw):
        d = {
            "running": True, "wallet_count": 12, "health_factor": 0.9,
            "pool": {"actions": 80, "errors": 20, "cycles": 4, "processed": 12,
                     "dropped": 0, "dyn_workers": 6, "paused": False},
            "db": {"cycles": 4},
        }
        d.update(kw)
        return d

    def test_status_markers_and_escape(self):
        s = ops.fmt_status(self._data())
        self.assertIn("[RUN]", s)
        self.assertIn("80.0%", s)
        self.assertIn("HARVEST // ПУЛЬТ", s)
        evil = self._data(wallet_count="<b>x</b>")
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", ops.fmt_status(evil))

    def test_status_stopped(self):
        s = ops.fmt_status(self._data(running=False, pool={}))
        self.assertIn("[STOP]", s)
        self.assertIn("—", s)

    def test_status_running_paused(self):
        d = self._data()
        d["pool"]["paused"] = True
        self.assertIn("[RUN+PAUSE]", ops.fmt_status(d))

    def test_stats_doctor_history(self):
        self.assertIn("[RUN]", ops.fmt_stats(self._data()))
        d = ops.fmt_doctor(self._data())
        self.assertIn("[ok]", d)
        d2 = ops.fmt_doctor({"running": False, "pool": {}, "db": {}})
        self.assertIn("[err]", d2)
        h = ops.fmt_history([{"id": 1, "errors": 0, "started_at": "2026-01-01",
                              "duration_s": 9, "wallets": 3, "actions_ok": 5}])
        self.assertIn("[ok] #1", h)
        self.assertIn("Пусто", ops.fmt_history([]))

    def test_networks_marks(self):
        s = ops.fmt_networks([
            {"slug": "a", "name": "A", "running": True, "tagline": "t"},
            {"slug": "b", "name": "B", "current": True},
            {"slug": "c", "name": "C"},
        ])
        self.assertIn("[RUN]", s)
        self.assertIn("[CUR]", s)
        self.assertIn("[···]", s)

    def test_wallets_spoiler_and_escape(self):
        w = [{"address": "0xabc", "total_actions": 2,
              "private_key": "<key>", "mnemonic": "<m>"}]
        s, pages = ops._fmt_wallets_page(w, 1)
        self.assertEqual(pages, 1)
        self.assertIn("<spoiler>", s)
        self.assertIn("&lt;key&gt;", s)

    def test_ref_text(self):
        self.assertIn("HARVEST // СПРАВКА", ops.REF_TEXT)
        self.assertLess(len(ops.REF_TEXT), 4000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
