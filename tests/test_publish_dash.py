"""Тесты публикации пульта (publish_dash.py).

Приёмка этапа 1: wallet_cells корректной длины из допустимых символов,
адресов в state.json нет вообще (только порядковые номера топа).
"""

import asyncio
import json
import os
import sqlite3
import subprocess
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

    def test_pnl_block_in_state(self):
        """Снапшот PnL попадает в state.json — агрегатами, без адресов."""
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "t.db")
            _make_db(db, wallets=3)
            con = sqlite3.connect(db)
            con.execute(
                "CREATE TABLE pnl_snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, actions_ok INTEGER, "
                "actions_fail INTEGER, farm_delta REAL, faucet_delta REAL, sample INTEGER, "
                "measured INTEGER, total_wallets INTEGER, window_actions INTEGER, "
                "by_type TEXT, by_reason TEXT)"
            )
            con.execute(
                "INSERT INTO pnl_snapshots (actions_ok, actions_fail, farm_delta, faucet_delta, "
                "sample, measured, total_wallets, window_actions, by_type, by_reason) "
                "VALUES (9, 1, -0.25, 0.5, 3, 3, 3, 10, ?, ?)",
                (
                    json.dumps([{"type": "transfer", "ok": 9, "fail": 1}]),
                    json.dumps([{"reason": "reverted", "count": 1}]),
                ),
            )
            con.commit()
            con.close()
            state = self._collect(db)
        pnl = state["pnl"]
        self.assertIsNotNone(pnl)
        self.assertEqual(pnl["actions_ok"], 9)
        self.assertEqual(pnl["actions_fail"], 1)
        self.assertAlmostEqual(pnl["farm_delta"], -0.25)
        self.assertAlmostEqual(pnl["faucet_delta"], 0.5)
        self.assertEqual(pnl["by_type"], [{"type": "transfer", "ok": 9, "fail": 1}])
        self.assertEqual(pnl["by_reason"], [{"reason": "reverted", "count": 1}])
        # адреса в публикуемое состояние не попадают
        blob = json.dumps(state)
        self.assertNotIn("enc:", blob)
        self.assertNotIn("private_key", blob)

    def test_pnl_absent_without_snapshots(self):
        """Без таблицы/снапшотов PnL в state.json — None, публикация не падает."""
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "t.db")
            _make_db(db, wallets=2)
            state = self._collect(db)
        self.assertIsNone(state["pnl"])

    def test_pnl_bad_json_tolerated(self):
        """Битый JSON в снапшоте не должен ронять публикацию."""
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "t.db")
            _make_db(db, wallets=2)
            con = sqlite3.connect(db)
            con.execute(
                "CREATE TABLE pnl_snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, actions_ok INTEGER, "
                "actions_fail INTEGER, farm_delta REAL, faucet_delta REAL, sample INTEGER, "
                "measured INTEGER, total_wallets INTEGER, window_actions INTEGER, "
                "by_type TEXT, by_reason TEXT)"
            )
            con.execute(
                "INSERT INTO pnl_snapshots (actions_ok, actions_fail, by_type, by_reason) "
                "VALUES (1, 0, 'не json', '{{{')"
            )
            con.commit()
            con.close()
            state = self._collect(db)
        self.assertEqual(state["pnl"]["by_type"], [])
        self.assertEqual(state["pnl"]["by_reason"], [])

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

    def test_farm_running_probe(self):
        from core.single_instance import SingleInstance, default_lock_path

        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "probe.db")
            Path(db).touch()
            self.assertFalse(publish_dash._farm_running(db))
            guard = SingleInstance(default_lock_path(db))
            self.assertTrue(guard.acquire())
            try:
                self.assertTrue(publish_dash._farm_running(db))
            finally:
                guard.release()
            self.assertFalse(publish_dash._farm_running(db))

    def test_network_slug_from_db_name(self):
        self.assertEqual(publish_dash._network_slug("farming_robinhood.db"), "robinhood")
        self.assertEqual(publish_dash._network_slug("/data/farming_arc.db"), "arc")
        self.assertEqual(publish_dash._network_slug("postgres://u@h/db"), "vibevibe")
        self.assertEqual(publish_dash._network_slug("postgres://u:p@h:5432/farming_arc"), "arc")
        self.assertEqual(publish_dash._network_slug("farming_vibevibe.db"), "vibevibe")

    def test_farm_running_probe_ignores_non_file_paths(self):
        # DSN — не файл: проба лока бессмысленна и не должна плодить каталоги.
        self.assertFalse(publish_dash._farm_running("postgres://u:p@h:5432/farming_arc"))

    def test_collect_running_and_network(self):
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "farming_robinhood.db")
            _make_db(db, wallets=3)
            state = self._collect(db)
            self.assertFalse(state["running"])
            self.assertEqual(state["network"], "robinhood")

    @staticmethod
    def _write_metrics(db: str, dyn, health, ts: str | None) -> None:
        con = sqlite3.connect(db)
        con.execute(
            "CREATE TABLE IF NOT EXISTS metrics_snapshots (id INTEGER PRIMARY KEY "
            "AUTOINCREMENT, ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP, actions INTEGER "
            "DEFAULT 0, errors INTEGER DEFAULT 0, processed INTEGER DEFAULT 0, "
            "cycles INTEGER DEFAULT 0, wallets INTEGER DEFAULT 0, "
            "dyn_workers REAL DEFAULT 0, health_factor REAL DEFAULT 1.0)"
        )
        con.execute(
            "INSERT INTO metrics_snapshots (dyn_workers, health_factor, ts) VALUES (?, ?, "
            "COALESCE(?, CURRENT_TIMESTAMP))",
            (dyn, health, ts),
        )
        con.commit()
        con.close()

    def test_collect_health_from_fresh_metrics(self):
        import datetime

        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "t.db")
            _make_db(db, wallets=3)
            fresh = (datetime.datetime.now(datetime.timezone.utc)
                     - datetime.timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M:%S")
            self._write_metrics(db, 7.0, 0.82, fresh)
            state = self._collect(db)
            self.assertEqual(state["health_factor"], 0.82)
            self.assertEqual(state["pool"]["dyn_workers"], 7.0)

    def test_collect_health_unknown_when_stale_or_absent(self):
        import datetime

        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "t.db")
            _make_db(db, wallets=3)
            state = self._collect(db)
            self.assertIsNone(state["health_factor"])
            self.assertIsNone(state["pool"]["dyn_workers"])
            stale = (datetime.datetime.now(datetime.timezone.utc)
                     - datetime.timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")
            self._write_metrics(db, 7.0, 0.82, stale)
            state = self._collect(db)
            self.assertIsNone(state["health_factor"])
            self.assertIsNone(state["pool"]["dyn_workers"])

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
            self.assertIn("description_en", net)
            self.assertIn("activities_en", net)

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


class TestPushFailureDiagnosis(unittest.TestCase):
    """Отказ push должен называть причину, а не молчать «exit status 128»."""

    def _exc(self, stderr: str):
        return subprocess.CalledProcessError(128, "git push", stderr=stderr)

    def test_auth_failure_named(self):
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            publish_dash._report_push_failure(
                self._exc("remote: Invalid username or token.\nfatal: Authentication failed")
            )
        out = buf.getvalue()
        self.assertIn("ОТКАЗ АВТОРИЗАЦИИ", out)
        self.assertIn("gh auth login", out)

    def test_other_failure_plain(self):
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            publish_dash._report_push_failure(self._exc("fatal: unable to access: network"))
        out = buf.getvalue()
        self.assertNotIn("ОТКАЗ АВТОРИЗАЦИИ", out)
        self.assertIn("git push не удался", out)


class TestBotforgeZip(unittest.TestCase):
    """Сборщик деплой-архива бота: состав, соответствие исходникам, детерминизм."""

    def test_build_matches_sources(self):
        import zipfile

        from tools import build_botforge_zip as bz

        with tempfile.TemporaryDirectory() as d:
            zpath = Path(d) / "b.zip"
            names = bz.build(zpath)
            self.assertEqual(names, ["main.py", "bot.py", "requirements.txt", "README.md"])
            with zipfile.ZipFile(zpath) as zf:
                bot = bz._canonical((bz.BFDIR / "bot.py").read_bytes())
                self.assertEqual(zf.read("main.py"), bot)
                self.assertEqual(zf.read("bot.py"), bot)
                for arc, src in (("requirements.txt", "requirements.txt"),
                                  ("README.md", "README.md")):
                    self.assertEqual(
                        zf.read(arc), bz._canonical((bz.BFDIR / src).read_bytes()))

    def test_build_is_deterministic(self):
        from tools import build_botforge_zip as bz

        with tempfile.TemporaryDirectory() as d:
            a = Path(d) / "a.zip"
            b = Path(d) / "b.zip"
            bz.build(a)
            bz.build(b)
            self.assertEqual(a.read_bytes(), b.read_bytes())

    def test_build_missing_source(self):
        from tools import build_botforge_zip as bz

        with tempfile.TemporaryDirectory() as d:
            fake = Path(d) / "bf"
            fake.mkdir()
            orig = bz.BFDIR
            bz.BFDIR = fake
            try:
                with self.assertRaises(FileNotFoundError):
                    bz.build(Path(d) / "x.zip")
            finally:
                bz.BFDIR = orig


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

    def test_master_key_probe_does_not_create_file(self):
        """Проба кандидатов не должна оставлять master.key в CWD.

        resolve_master_key() создаёт отсутствующий файл. Если бы проба его
        использовала, публикатор оставлял бы в рабочей папке случайный ключ —
        а ферма, запущенная оттуда, зашифровала бы новые кошельки чужим ключом.
        """
        import secrets as _secrets

        from core.crypto import encrypt_seed
        from tools import sync_wallets_blob as sm

        good = _secrets.token_bytes(32)
        with tempfile.TemporaryDirectory() as dbdir, tempfile.TemporaryDirectory() as cwd:
            db = os.path.join(dbdir, "t.db")
            con = sqlite3.connect(db)
            con.execute(
                "CREATE TABLE wallets (address TEXT PRIMARY KEY, private_key TEXT, "
                "mnemonic TEXT, total_actions INTEGER DEFAULT 0)"
            )
            con.execute(
                "INSERT INTO wallets VALUES (?,?,?,?)",
                ("0x" + "cd" * 20, encrypt_seed(good, "0xkey"), encrypt_seed(good, "w"), 1),
            )
            con.commit()
            con.close()
            # Рабочий ключ лежит только рядом с БД; в CWD его нет.
            Path(dbdir, "master.key").write_bytes(good)
            prev = os.getcwd()
            os.chdir(cwd)
            wkey = _secrets.token_hex(32)
            try:
                res = asyncio.run(sm.sync(db_path=db, key_hex=wkey))
            finally:
                os.chdir(prev)
            self.assertTrue(res["ok"], res)
            self.assertEqual(res["count"], 1)
            # Ключ в CWD не появился — побочного мусора нет.
            self.assertFalse(Path(cwd, "master.key").exists())

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
