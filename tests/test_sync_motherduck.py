"""Тесты синхронизации в облачную БД MotherDuck (sync_motherduck.py).

Сеть и duckdb не используются: подменяем модуль duckdb и БД. Проверяем
ветки «нет токена», «нет duckdb», полный путь синхронизации и обработку
ошибок в main().
"""

import os
import sys
import unittest
from unittest import mock

import sync_motherduck as sm

_FAKE_WALLET = {"address": "0xabc", "private_key": "0xkey", "mnemonic": "a b c", "total_actions": 5}


class TestSync(unittest.IsolatedAsyncioTestCase):
    async def test_skipped_without_token(self):
        env = {k: v for k, v in os.environ.items() if k != "MOTHERDUCK_TOKEN"}
        with mock.patch.dict(os.environ, env, clear=True):
            res = await sm.sync()
        self.assertEqual(res, {"skipped": "MOTHERDUCK_TOKEN не задан"})

    async def test_skipped_without_duckdb(self):
        with mock.patch.dict(os.environ, {"MOTHERDUCK_TOKEN": "tok"}):
            with mock.patch.dict(sys.modules, {"duckdb": None}):
                res = await sm.sync()
        self.assertEqual(res, {"skipped": "duckdb не установлен"})

    async def test_full_sync(self):
        fake_duck = mock.MagicMock()
        con = fake_duck.connect.return_value
        db = mock.MagicMock()
        db.get_all_wallets = mock.AsyncMock(return_value=[_FAKE_WALLET])
        with (
            mock.patch.object(sm, "CONFIG_FILE", "config.example.yaml"),
            mock.patch.object(sm, "resolve_master_key", return_value=b"k" * 32),
            mock.patch.object(sm, "Database", return_value=db),
            mock.patch.dict(sys.modules, {"duckdb": fake_duck}),
            mock.patch.dict(os.environ, {"MOTHERDUCK_TOKEN": "tok"}),
            mock.patch("publish_dash.collect", mock.AsyncMock(return_value={"pool": {}})),
        ):
            res = await sm.sync()
        self.assertEqual(res, {"wallets": 1, "state": "ok"})
        con.execute.assert_any_call("DELETE FROM wallets")
        con.executemany.assert_called_once()
        con.close.assert_called_once()

    async def test_conn_creates_tables(self):
        fake_duck = mock.MagicMock()
        with (
            mock.patch.dict(sys.modules, {"duckdb": fake_duck}),
            mock.patch.dict(os.environ, {"MOTHERDUCK_TOKEN": "tok"}),
        ):
            sm._conn()
        fake_duck.connect.assert_called_once()
        sqls = " ".join(str(c.args[0]) for c in fake_duck.connect.return_value.execute.call_args_list)
        self.assertIn("CREATE TABLE IF NOT EXISTS wallets", sqls)
        self.assertIn("CREATE TABLE IF NOT EXISTS state", sqls)


class TestMain(unittest.IsolatedAsyncioTestCase):
    async def test_main_success(self):
        with mock.patch.object(sm, "sync", mock.AsyncMock(return_value={"wallets": 3, "state": "ok"})):
            self.assertEqual(await sm.main(), 0)

    async def test_main_error(self):
        with mock.patch.object(sm, "sync", mock.AsyncMock(side_effect=RuntimeError("boom"))):
            self.assertEqual(await sm.main(), 1)


if __name__ == "__main__":
    unittest.main()
