"""Тесты экспорта кошельков портала: FarmDaemon.export_wallets, /api/wallets/export, бот.

Покрывают:
  * FarmDaemon.export_wallets — CSV (utf-8-sig, формульные инъекции) и JSON;
  * wallet_count() в statistics() и напрямую;
  * /api/wallets/export — auth, HTTPS-гейт, format, 503 без БД;
  * send_wallets_file / _fmt_stats — кошельки в боте.
"""

import asyncio
import csv
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import cast
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from yarl import URL

from portal import api, auth
from portal.api import create_app
from portal.config import PortalConfig
from portal.farm import FarmDaemon, _csv_safe


def _authed(client: TestClient, app: web.Application) -> None:
    cfg = app[api.KEY_CFG]
    token = auth.sign_token(cfg.secret, "master", "master")
    client.session.cookie_jar.update_cookies({api.COOKIE_NAME: token}, URL("http://127.0.0.1"))


def _make_cfg(
    public_base_url: str = "",
    *,
    allow_insecure: bool = True,
    cookie_secure: bool = False,
) -> PortalConfig:
    prev = {k: os.environ.get(k) for k in ("PORTAL_SECRET", "FARMER_MASTER_KEY")}
    os.environ["PORTAL_SECRET"] = "s" * 32
    os.environ["FARMER_MASTER_KEY"] = "k" * 64
    cfg = PortalConfig()
    cfg.public_base_url = public_base_url
    cfg.allow_insecure_password = allow_insecure
    cfg.cookie_secure = cookie_secure
    cfg.password_login = True
    for k, v in prev.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    return cfg


class _ExportDaemon:
    running = False

    def run_state(self) -> dict:
        return {"running": False, "paused": False}

    async def export_wallets(self, fmt: str = "csv") -> tuple[str, str]:
        if fmt == "json":
            return json.dumps([{"address": "0x1", "private_key": "abc"}]), "wallets.json"
        return "address,private_key\n0x1,abc\n", "wallets.csv"

    async def statistics(self) -> dict:
        return {"running": False, "pool": {}, "health_factor": 1.0, "db": {}, "wallet_count": 1}

    async def top_wallets(self, limit=8) -> list:
        return [{"address": "0x1", "actions": 1}]

    async def cycle_history(self, limit=12) -> list:
        return []


class _RaiseDaemon(_ExportDaemon):
    async def export_wallets(self, fmt: str = "csv") -> tuple[str, str]:
        raise RuntimeError("БД фермы не подключена")


class PortalExportTestBase(unittest.TestCase):
    def setUp(self) -> None:
        api._login_fails.clear()
        api._global_login_fails.clear()
        api._choke_buckets.clear()
        api._global_choke.clear()
        self._td = tempfile.TemporaryDirectory()
        api.REVOKED_FILE = str(Path(self._td.name) / "revoked.json")
        api._revoked = None

    def tearDown(self) -> None:
        self._td.cleanup()
        api._revoked = None

    async def _client(self, daemon=None, base: str = "", **kwargs) -> tuple[TestClient, web.Application]:
        cfg = _make_cfg(base, **kwargs)
        app = create_app(cfg, cast(FarmDaemon, daemon or _ExportDaemon()))
        client = TestClient(TestServer(app))
        await client.start_server()
        return client, app


class TestFarmDaemonExport(unittest.TestCase):
    def test_export_wallets_csv(self):
        class _Db:
            async def get_all_wallets(self):
                return [
                    {"address": "0x1", "private_key": "aa", "mnemonic": "word", "total_actions": 3},
                    {"address": "0x2", "private_key": "=cmd", "mnemonic": "", "total_actions": 0},
                ]

        daemon = FarmDaemon("cfg.yaml", "db.sqlite")
        daemon.db = _Db()
        content, name = asyncio.run(daemon.export_wallets("csv"))
        self.assertEqual(name, "wallets.csv")
        rows = list(csv.reader(io.StringIO(content)))
        self.assertEqual(rows[0], ["address", "private_key", "mnemonic", "actions"])
        self.assertEqual(rows[1], ["0x1", "aa", "word", "3"])
        # формульная инъекция нейтрализована апострофом
        self.assertEqual(rows[2][1], "'=cmd")

    def test_export_wallets_json(self):
        class _Db:
            async def get_all_wallets(self):
                return [{"address": "0x1", "private_key": "aa"}]

        daemon = FarmDaemon("cfg.yaml", "db.sqlite")
        daemon.db = _Db()
        content, name = asyncio.run(daemon.export_wallets("json"))
        self.assertEqual(name, "wallets.json")
        data = json.loads(content)
        self.assertEqual(data[0]["private_key"], "aa")

    def test_export_wallets_without_db_raises(self):
        daemon = FarmDaemon("cfg.yaml", "db.sqlite")
        with self.assertRaises(RuntimeError):
            asyncio.run(daemon.export_wallets())

    def test_wallet_count(self):
        class _Db:
            async def count_wallets(self):
                return 42

        daemon = FarmDaemon("cfg.yaml", "db.sqlite")
        daemon.db = _Db()
        self.assertEqual(asyncio.run(daemon.wallet_count()), 42)

    def test_wallet_count_without_db(self):
        daemon = FarmDaemon("cfg.yaml", "db.sqlite")
        self.assertEqual(asyncio.run(daemon.wallet_count()), 0)

    def test_all_wallets(self):
        class _Db:
            async def get_all_wallets(self):
                return [{"address": "0x1", "private_key": "0xk", "mnemonic": "a", "total_actions": 3}]

        daemon = FarmDaemon("cfg.yaml", "db.sqlite")
        daemon.db = _Db()
        got = asyncio.run(daemon.all_wallets())
        self.assertEqual(
            got,
            [{"address": "0x1", "private_key": "0xk", "mnemonic": "a", "total_actions": 3}],
        )

    def test_all_wallets_without_db(self):
        daemon = FarmDaemon("cfg.yaml", "db.sqlite")
        self.assertEqual(asyncio.run(daemon.all_wallets()), [])

    def test_statistics_includes_wallet_count(self):
        class _Db:
            async def get_cycle_stats(self):
                return {"cycles": 1}

            async def count_wallets(self):
                return 7

        class _Pool:
            network = mock.Mock()
            network.concurrency_factor.return_value = 0.9

            def live_stats(self):
                return {"actions": 1}

        daemon = FarmDaemon("cfg.yaml", "db.sqlite")
        daemon.pool = _Pool()
        daemon.db = _Db()
        stats = asyncio.run(daemon.statistics())
        self.assertEqual(stats["wallet_count"], 7)

    def test_csv_safe(self):
        self.assertEqual(_csv_safe("=SUM(1)"), "'=SUM(1)")
        self.assertEqual(_csv_safe("-x"), "'-x")
        self.assertEqual(_csv_safe("0x123"), "0x123")


class TestWalletsExportApi(PortalExportTestBase, unittest.IsolatedAsyncioTestCase):
    async def test_export_requires_auth(self):
        client, _app = await self._client()
        async with client:
            self.assertEqual((await client.get("/api/wallets/export")).status, 401)

    async def test_export_csv_over_http_blocked_by_default(self):
        client, _app = await self._client(allow_insecure=False)
        async with client:
            _authed(client, _app)
            resp = await client.get("/api/wallets/export?format=csv")
            self.assertEqual(resp.status, 403)

    async def test_export_csv_over_https_allowed(self):
        client, _app = await self._client(allow_insecure=False)
        async with client:
            _authed(client, _app)
            cfg = _app[api.KEY_CFG]
            cfg.trust_proxy = True
            resp = await client.get("/api/wallets/export?format=csv", headers={"X-Forwarded-Proto": "https"})
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.headers["Content-Type"], "text/csv; charset=utf-8")
            self.assertIn("attachment", resp.headers["Content-Disposition"])
            self.assertEqual(resp.headers["Cache-Control"], "no-store, no-cache, must-revalidate")
            body = await resp.text()
            self.assertIn("0x1", body)

    async def test_export_json(self):
        client, _app = await self._client()
        async with client:
            _authed(client, _app)
            resp = await client.get("/api/wallets/export?format=json")
            self.assertEqual(resp.status, 200)
            self.assertTrue(resp.headers["Content-Type"].startswith("application/json"))
            data = await resp.json()
            self.assertEqual(data[0]["private_key"], "abc")

    async def test_export_bad_format(self):
        client, _app = await self._client()
        async with client:
            _authed(client, _app)
            resp = await client.get("/api/wallets/export?format=xml")
            self.assertEqual(resp.status, 400)

    async def test_export_without_db_returns_503(self):
        client, _app = await self._client(daemon=_RaiseDaemon())
        async with client:
            _authed(client, _app)
            resp = await client.get("/api/wallets/export?format=csv")
            self.assertEqual(resp.status, 503)


class TestBotExport(unittest.TestCase):
    def test_fmt_stats_shows_wallet_count(self):
        try:
            from portal.bot_telegram import _fmt_stats
        except Exception:  # noqa: BLE001
            self.skipTest("aiogram не установлен")
        s = _fmt_stats(
            {"running": True, "wallet_count": 55, "pool": {"dyn_workers": 4, "health": 1.0}, "db": {"cycles": 1}}
        )
        self.assertIn("55", s)

    def test_fmt_stats_escapes_wallet_count(self):
        try:
            from portal.bot_telegram import _fmt_stats
        except Exception:  # noqa: BLE001
            self.skipTest("aiogram не установлен")
        s = _fmt_stats(
            {"running": True, "wallet_count": "<b>x</b>", "pool": {}, "db": {}}
        )
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", s)

    def test_send_wallets_file(self):
        try:
            from portal.bot_telegram import send_wallets_file
        except Exception:  # noqa: BLE001
            self.skipTest("aiogram не установлен")

        class _Bot:
            def __init__(self):
                self.sent = []

            async def send_document(self, chat_id, document, caption):
                self.sent.append((chat_id, document.filename, caption))

        class _Daemon:
            async def export_wallets(self, fmt):
                return "address,private_key\n0x1,aa\n", f"wallets.{fmt}"

        async def scenario():
            bot = _Bot()
            daemon = _Daemon()
            await send_wallets_file(bot, 123, daemon, "csv")
            return bot

        bot = asyncio.run(scenario())
        self.assertEqual(bot.sent[0][0], 123)
        self.assertEqual(bot.sent[0][1], "wallets.csv")


class TestBotDispatchExport(unittest.IsolatedAsyncioTestCase):
    """Прямые ветки dispatcher'а: /export команда и export:* callback."""

    def _cfg(self):
        prev = {k: os.environ.get(k) for k in ("PORTAL_SECRET", "FARMER_MASTER_KEY")}
        os.environ["PORTAL_SECRET"] = "s" * 32
        os.environ["FARMER_MASTER_KEY"] = "k" * 64
        cfg = PortalConfig()
        cfg.telegram_allow_ids = [123]
        for k, v in prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return cfg

    def _dispatch(self):
        try:
            from portal.bot_telegram import build_dispatcher
        except Exception:  # noqa: BLE001
            self.skipTest("aiogram не установлен")
        cfg = self._cfg()
        daemon = mock.Mock()
        daemon.export_wallets = mock.AsyncMock(return_value=("a,b\n1,2\n", "wallets.csv"))
        return build_dispatcher(cfg, daemon), daemon

    async def test_export_command_sends_file(self):
        dp, daemon = self._dispatch()
        msg = mock.Mock()
        msg.from_user.id = 123
        msg.text = "/export"
        msg.bot = mock.Mock()
        msg.answer = mock.AsyncMock()
        with mock.patch("portal.bot_telegram.send_wallets_file", new=mock.AsyncMock()) as swf:
            await dp.message.handlers[1].callback(msg)
        swf.assert_awaited_once()
        self.assertEqual(swf.await_args.args[3], "csv")

    async def test_export_command_json_format(self):
        dp, daemon = self._dispatch()
        msg = mock.Mock()
        msg.from_user.id = 123
        msg.text = "/export json"
        msg.bot = mock.Mock()
        msg.answer = mock.AsyncMock()
        with mock.patch("portal.bot_telegram.send_wallets_file", new=mock.AsyncMock()) as swf:
            await dp.message.handlers[1].callback(msg)
        self.assertEqual(swf.await_args.args[3], "json")

    async def test_export_command_failure_answered(self):
        dp, daemon = self._dispatch()
        msg = mock.Mock()
        msg.from_user.id = 123
        msg.text = "/export"
        msg.bot = mock.Mock()
        msg.answer = mock.AsyncMock()
        with mock.patch(
            "portal.bot_telegram.send_wallets_file", new=mock.AsyncMock(side_effect=RuntimeError("boom"))
        ):
            await dp.message.handlers[1].callback(msg)
        msg.answer.assert_awaited_once()
        self.assertIn("Не удалось", msg.answer.await_args.args[0])

    async def test_export_callback_csv(self):
        dp, daemon = self._dispatch()
        call = mock.Mock()
        call.from_user.id = 123
        call.data = "export:csv"
        call.message.bot = mock.Mock()
        call.message.answer = mock.AsyncMock()
        call.answer = mock.AsyncMock()
        with mock.patch("portal.bot_telegram.send_wallets_file", new=mock.AsyncMock()) as swf:
            await dp.callback_query.handlers[0].callback(call)
        swf.assert_awaited_once()
        self.assertEqual(swf.await_args.args[3], "csv")

    async def test_export_callback_failure(self):
        dp, daemon = self._dispatch()
        call = mock.Mock()
        call.from_user.id = 123
        call.data = "export:json"
        call.message.bot = mock.Mock()
        call.message.answer = mock.AsyncMock()
        call.answer = mock.AsyncMock()
        with mock.patch(
            "portal.bot_telegram.send_wallets_file", new=mock.AsyncMock(side_effect=RuntimeError("boom"))
        ):
            await dp.callback_query.handlers[0].callback(call)
        call.message.answer.assert_awaited()


if __name__ == "__main__":
    unittest.main()
