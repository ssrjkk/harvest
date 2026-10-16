"""Тесты публикации пульта (publish_dash.py).

Приёмка этапа 1: wallet_cells корректной длины из допустимых символов,
адресов в state.json нет вообще (только порядковые номера топа).
"""

import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import publish_dash


def _make_db(path: str, wallets: int = 5) -> None:
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE wallets (address TEXT PRIMARY KEY, private_key TEXT, "
        "mnemonic TEXT, total_actions INTEGER DEFAULT 0, "
        "total_attempts INTEGER DEFAULT 0, last_cycle INTEGER)"
    )
    con.execute(
        "CREATE TABLE cycle_history (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, mode TEXT, duration_s REAL, "
        "wallets INTEGER, wallets_ok INTEGER, actions_ok INTEGER, errors INTEGER)"
    )
    for i in range(wallets):
        con.execute(
            "INSERT INTO wallets (address, private_key, mnemonic, total_actions, "
            "total_attempts, last_cycle) VALUES (?, ?, ?, ?, ?, ?)",
            (f"0x{i:040x}", f"enc:pk{i}", f"enc:mn{i}", i, i, 1 if i % 2 == 0 else None),
        )
    con.execute(
        "INSERT INTO cycle_history (mode, duration_s, wallets, wallets_ok, "
        "actions_ok, errors) VALUES ('auto', 8.5, 3, 3, 9, 0)"
    )
    con.commit()
    con.close()


class TestPublishDash(unittest.TestCase):
    def _collect(self, path: str) -> dict:
        old, publish_dash.DB = publish_dash.DB, path
        try:
            return asyncio.run(publish_dash.collect())
        finally:
            publish_dash.DB = old

    def test_cells_format_and_no_addresses(self):
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "t.db")
            _make_db(db, wallets=5)
            state = self._collect(db)
        wc = state["wallet_cells"]
        self.assertEqual(wc["v"], 1)
        self.assertEqual(wc["n"], 5)
        self.assertEqual(len(wc["cells"]), 5)
        self.assertTrue(set(wc["cells"]) <= set("iwoer"), wc["cells"])
        blob = json.dumps(state)
        self.assertNotIn("0x", blob)
        self.assertNotIn("enc:", blob)
        self.assertNotIn("private_key", blob)
        for i, w in enumerate(state["top_wallets"]):
            self.assertEqual(w["n"], i + 1)
            self.assertNotIn("address", w)

    def test_cells_capped_with_overflow_count(self):
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "t.db")
            _make_db(db, wallets=405)
            state = self._collect(db)
        wc = state["wallet_cells"]
        self.assertEqual(wc["n"], 405)
        self.assertEqual(len(wc["cells"]), 400)

    def test_cell_states(self):
        self.assertEqual(publish_dash._cell_state(0, 0, -1, 3), "i")
        self.assertEqual(publish_dash._cell_state(5, 5, 3, 3), "o")
        self.assertEqual(publish_dash._cell_state(0, 0, 3, 3), "w")
        self.assertEqual(publish_dash._cell_state(2, 5, 3, 3), "e")
        self.assertEqual(publish_dash._cell_state(4, 4, 1, 3), "r")

    def test_plural_files(self):
        self.assertEqual(publish_dash._plural_files(1), "файл")
        self.assertEqual(publish_dash._plural_files(54), "файла")
        self.assertEqual(publish_dash._plural_files(5), "файлов")
        self.assertEqual(publish_dash._plural_files(11), "файлов")

    def test_inject_counts(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "assets").mkdir()
            (root / "index.html").write_text(
                "1335 TESTS · 1335 · 54 файла", encoding="utf-8")
            (root / "assets" / "site.js").write_text(
                "1335 TESTS · 1335 tests · 54 files", encoding="utf-8")
            (root / "terminal.html").write_text(
                "<title>harvest</title>", encoding="utf-8")
            n = publish_dash.inject_test_counts(root, 1400, 55)
            self.assertGreater(n, 0)
            html = (root / "index.html").read_text(encoding="utf-8")
            self.assertIn("1400 TESTS", html)
            self.assertIn("1400 · 55 файлов", html)
            self.assertNotIn("1335", html)
            js = (root / "assets" / "site.js").read_text(encoding="utf-8")
            self.assertIn("1400 tests · 55 files", js)

    def test_collect_counts_matches_suite(self):
        n, m = publish_dash.collect_test_counts()
        self.assertIsNotNone(n)
        self.assertGreater(n, 1300)
        self.assertGreaterEqual(m, 54)

    def test_collect_networks_have_links(self):
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "t.db")
            _make_db(db, wallets=5)
            state = self._collect(db)
        for net in state["networks"]:
            self.assertIn("faucet", net)
            self.assertIn("explorer", net)
            self.assertNotIn("config", net)
            self.assertNotIn("db", net)


if __name__ == "__main__":
    unittest.main(verbosity=2)
