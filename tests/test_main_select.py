"""Тесты стартового экрана выбора тестнета (main._find_configs)."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main as main_mod


class TestFindConfigs(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.dir = Path(self.td.name)
        self._old_cwd = Path.cwd()
        self._was_frozen = hasattr(sys, "frozen")
        self._old_frozen = getattr(sys, "frozen", False)
        os.chdir(self.dir)

    def tearDown(self):
        os.chdir(self._old_cwd)
        if self._was_frozen:
            sys.frozen = self._old_frozen
        elif hasattr(sys, "frozen"):
            delattr(sys, "frozen")
        self.td.cleanup()

    def _write_config(self, name: str, net_name: str) -> None:
        (self.dir / name).write_text(
            f'network:\n  name: "{net_name}"\n  rpc_url: http://127.0.0.1:1\n  chain_id: 1\n',
            encoding="utf-8",
        )

    def test_finds_and_labels_networks(self):
        self._write_config("config_robinhood.yaml", "Robinhood Chain Testnet")
        self._write_config("config_flop.yaml", "Flop Labs Testnet")
        self._write_config("config.yaml", "Main")
        found = main_mod._find_configs()
        self.assertEqual(len(found), 3)
        labels = [label for _, label in found]
        self.assertTrue(any("Robinhood Chain Testnet" in label for label in labels))
        self.assertTrue(any("Flop Labs Testnet" in label for label in labels))

    def test_broken_yaml_uses_filename(self):
        (self.dir / "config_bad.yaml").write_text("::: not yaml :::", encoding="utf-8")
        found = main_mod._find_configs()
        self.assertEqual(len(found), 1)
        self.assertIn("config_bad.yaml", found[0][1])

    def test_nothing_found(self):
        self.assertEqual(main_mod._find_configs(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
