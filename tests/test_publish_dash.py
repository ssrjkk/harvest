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
        self.assertEqual(wc["v"], 2)
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
            # Набор страниц/скриптов — как в настоящем docs/: inject_test_counts
            # берёт glob по *.html и assets/*.js, а не хардкод-список.
            for name in ("index.html", "field.html", "demo.html", "docs.html"):
                (root / name).write_text("1335 TESTS · 1335 · 54 файла", encoding="utf-8")
            for name in ("common.js", "field.js", "demo.js"):
                (root / "assets" / name).write_text(
                    "1335 TESTS · 1335 tests · 54 files", encoding="utf-8")
            # Страница без счётчиков должна пройти нетронутой.
            (root / "terminal.html").write_text("<title>harvest</title>", encoding="utf-8")
            n = publish_dash.inject_test_counts(root, 1400, 55)
            self.assertGreater(n, 0)
            for name in ("index.html", "field.html", "demo.html", "docs.html"):
                html = (root / name).read_text(encoding="utf-8")
                self.assertIn("1400 TESTS", html)
                self.assertIn("1400 · 55 файлов", html)
                self.assertNotIn("1335", html)
            for name in ("common.js", "field.js", "demo.js"):
                js = (root / "assets" / name).read_text(encoding="utf-8")
                self.assertIn("1400 tests · 55 files", js)
                self.assertNotIn("1335", js)
            self.assertEqual(
                (root / "terminal.html").read_text(encoding="utf-8"), "<title>harvest</title>")

    def test_site_files_cover_all_docs_pages(self):
        """Новая страница в docs/ не должна молча остаться неопубликованной."""
        docs = Path(publish_dash.DOCS)
        pages = {p.name for p in docs.glob("*.html")}
        self.assertTrue(pages)
        missing = sorted(pages - set(publish_dash.SITE_FILES))
        self.assertEqual(missing, [], f"страницы вне SITE_FILES: {missing}")

    def test_site_files_all_exist(self):
        """Опечатка в SITE_FILES — это тихая потеря файла в gh-pages."""
        docs = Path(publish_dash.DOCS)
        missing = [n for n in publish_dash.SITE_FILES if not (docs / n).exists()]
        self.assertEqual(missing, [], f"нет в docs/: {missing}")

    def test_empty_blob_never_published(self):
        """Блоб без кошельков в gh-pages тихо оставил бы бота без кошельков."""
        f = publish_dash._blob_from_sync
        # Норма: есть кошельки — публикуем.
        good = {"v": 1, "alg": "AES-256-GCM", "nonce": "00" * 12, "ct": "aa" * 8}
        self.assertEqual(f({"ok": True, "blob": good, "count": 1000}), (good, 1000))
        # Пустой результат при ok=True — не та БД / не тот master.key.
        self.assertEqual(f({"ok": True, "blob": good, "count": 0}), (None, 0))
        self.assertEqual(f({"ok": True, "blob": good}), (None, 0))
        self.assertEqual(f({"ok": True, "blob": good, "count": None}), (None, 0))
        # Ошибка sync и мусор на входе — тоже пропуск.
        self.assertEqual(f({"ok": False, "reason": "no WALLETS_KEY"}), (None, 0))
        self.assertEqual(f({"ok": True, "blob": "не словарь", "count": 5}), (None, 0))
        self.assertEqual(f(None), (None, 0))
        self.assertEqual(f("строка"), (None, 0))

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

    def test_collect_details_no_secrets(self):
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "t.db")
            _make_db(db, wallets=5)
            state = self._collect(db)
        wc = state["wallet_cells"]
        self.assertEqual(wc["v"], 2)
        self.assertEqual(len(wc["details"]), 5)
        blob = json.dumps(state)
        self.assertNotIn("0x0000", blob)
        self.assertNotIn("enc:pk", blob)
        d0 = wc["details"][0]
        self.assertEqual(d0, {"n": 1, "a": 0, "t": 0, "c": 1, "e": ""})

    def test_details_scrub_addresses(self):
        import publish_dash as _pd

        self.assertEqual(_pd._scrub_addrs("to=0x" + "ab" * 20 + ", amount=0.1"), "to=0x…, amount=0.1")
        self.assertEqual(_pd._scrub_addrs(""), "")
        self.assertEqual(_pd._scrub_addrs("plain error"), "plain error")

    def test_collect_details_last_error_scrubbed(self):
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "t.db")
            _make_db(db, wallets=2)
            con = sqlite3.connect(db)
            con.execute(
                "CREATE TABLE actions_log (id INTEGER PRIMARY KEY AUTOINCREMENT, address TEXT, "
                "action_type TEXT, tx_hash TEXT, success INTEGER, timestamp TEXT, details TEXT)"
            )
            addr = "0x%s" % ("11" * 20)
            con.execute(
                "INSERT INTO actions_log (address, action_type, success, details) VALUES (?, ?, ?, ?)",
                (f"0x{'00' * 20}", "transfer", 0, f"to={addr}, amount=0.5"),
            )
            con.commit()
            con.close()
            state = self._collect(db)
        d = state["wallet_cells"]["details"][0]
        self.assertIn("0x…", d["e"])
        self.assertNotIn(addr, json.dumps(state))


class TestWalletsBlob(unittest.TestCase):
    def test_no_key_skips(self):
        from tools import sync_wallets_blob as sm

        old = os.environ.pop("WALLETS_KEY", None)
        try:
            res = asyncio.run(sm.sync(db_path="whatever.db", key_hex=""))
            self.assertFalse(res["ok"])
            self.assertIn("WALLETS_KEY", res["reason"])
        finally:
            if old is not None:
                os.environ["WALLETS_KEY"] = old

    def test_missing_db(self):
        import secrets as _secrets

        from tools import sync_wallets_blob as sm

        res = asyncio.run(sm.sync(db_path="no-such.db", key_hex=_secrets.token_hex(32)))
        self.assertFalse(res["ok"])
        self.assertIn("не найдена", res["reason"])

    def test_roundtrip_and_wrong_key(self):
        import secrets as _secrets

        from tools import sync_wallets_blob as sm

        rows = [
            {"address": "0xaa", "private_key": "0xkey", "mnemonic": "w1 w2", "total_actions": 3},
            {"address": "0xbb", "private_key": "0xkey2", "mnemonic": None, "total_actions": 0},
        ]
        key = _secrets.token_bytes(32)
        blob = sm.encrypt_wallets(rows, key)
        self.assertEqual(blob["v"], 1)
        self.assertNotIn("0xkey", json.dumps(blob))
        self.assertEqual(sm.decrypt_blob(blob, key), rows)
        with self.assertRaises(ValueError):
            sm.decrypt_blob(blob, _secrets.token_bytes(32))
        bad = dict(blob)
        bad["ct"] = "00" * 8
        with self.assertRaises(ValueError):
            sm.decrypt_blob(bad, key)

    def test_bad_key_rejected(self):
        from tools import sync_wallets_blob as sm

        with self.assertRaises(ValueError):
            sm._parse_key("short")
        with self.assertRaises(ValueError):
            sm._parse_key("zz" * 32)

    def test_master_key_picks_working_candidate(self):
        import secrets as _secrets
        import sqlite3 as _sqlite3

        from core.crypto import encrypt_seed
        from tools import sync_wallets_blob as sm

        good = _secrets.token_bytes(32)
        bad = _secrets.token_bytes(32)
        with tempfile.TemporaryDirectory() as dbdir, tempfile.TemporaryDirectory() as cwd:
            db = os.path.join(dbdir, "t.db")
            con = _sqlite3.connect(db)
            con.execute(
                "CREATE TABLE wallets (address TEXT PRIMARY KEY, private_key TEXT, "
                "mnemonic TEXT, total_actions INTEGER DEFAULT 0)"
            )
            con.execute(
                "INSERT INTO wallets VALUES (?,?,?,?)",
                ("0x" + "ab" * 20, encrypt_seed(good, "0xkey"), encrypt_seed(good, "w"), 1),
            )
            con.commit()
            con.close()
            # Рядом с БД — чужой ключ (ловушка), в CWD — рабочий.
            Path(dbdir, "master.key").write_bytes(bad)
            Path(cwd, "master.key").write_bytes(good)
            prev = os.getcwd()
            os.chdir(cwd)
            wkey = _secrets.token_hex(32)
            try:
                res = asyncio.run(sm.sync(db_path=db, key_hex=wkey))
            finally:
                os.chdir(prev)
            self.assertTrue(res["ok"], res)
            self.assertEqual(res["count"], 1)
            rows = sm.decrypt_blob(res["blob"], bytes.fromhex(wkey))
            self.assertEqual(rows[0]["private_key"], "0xkey")


if __name__ == "__main__":
    unittest.main(verbosity=2)
