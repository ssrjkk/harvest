"""Приёмка ТЗ этап 2 (п.1-3, 6-8): рендер, клетки, колбэки, токен, 409, экспорт.

Покрывает реализованное в portal/bot_render.py, portal/wallet_cells.py
и служебное в portal/bot_telegram.py (redaction, cooldown, conflict).
"""

import logging
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from portal import bot_render as R
from portal.wallet_cells import build_cells, cell_state, render_grid

try:
    import portal.bot_telegram as bt
    from portal.config import PortalConfig

    HAS_BOT = True
except Exception:  # noqa: BLE001
    HAS_BOT = False


def _state(**kw):
    base = {
        "running": True, "paused": False, "network": "vibe/vibe",
        "chain_id": 46630, "wallet_count": 50, "cycle": 5,
        "cycles_total": "∞", "errors": 0, "health_factor": 1.0,
    }
    base.update(kw)
    return base


class TestRenderHome(unittest.TestCase):
    def test_snapshot_novice(self):
        t = R.render_home(_state(), mode="novice", cells="o" * 20, cells_total=20)
        self.assertIn("harvest // ПОЛЕ", t)
        self.assertIn("[RUN]", t)
        self.assertIn("vibe/vibe · 46630", t)
        self.assertIn("кошельки  50", t)
        self.assertIn("цикл 5 / ∞", t)
        self.assertIn("обновлено", t)
        self.assertLessEqual(len(t), 4096)

    def test_snapshot_pro(self):
        t = R.render_home(
            _state(workers=8, rpc_ok="3/3", gas_limit=100000, cmd="python auto.py --wallets 50"),
            mode="pro", cells="e" * 5, cells_total=5,
        )
        self.assertIn("harvest // ПОЛЕ", t)
        self.assertIn("workers 8", t)
        self.assertIn("python auto.py", t)
        self.assertLessEqual(len(t), 4096)

    def test_modes_differ(self):
        self.assertNotEqual(
            R.render_home(_state(), mode="novice"),
            R.render_home(_state(), mode="pro"),
        )

    def test_pause_mark(self):
        t = R.render_home(_state(paused=True))
        self.assertIn("[PAUSE]", t)
        t2 = R.render_home(_state(running=False))
        self.assertIn("[STOP]", t2)

    def test_limit(self):
        self.assertTrue(R.ensure_limit("x" * 5000).endswith("…"))
        self.assertLessEqual(len(R.ensure_limit("x" * 5000)), 4096)


class TestEscape(unittest.TestCase):
    def test_evil_network_escaped(self):
        t = R.render_home(_state(network="<b>&evil</b>"))
        self.assertIn("&lt;b&gt;", t)
        self.assertNotIn("<b>&evil</b>", t)

    def test_short_addr(self):
        self.assertEqual(R.short_addr("0xb3E4aaaabbbbccccddddeeeeffffB029E"), "0xb3E4…B029E")
        self.assertEqual(R.short_addr("0xabc"), "0xabc")

    def test_sparkline(self):
        self.assertEqual(R.sparkline([]), "—")
        s = R.sparkline([0, 1, 2, 3, 4, 5, 6, 7])
        self.assertEqual(len(s), 8)
        self.assertIn(s[0], "▁▂▃▄▅▆▇█")

    def test_progress(self):
        self.assertIn("62%", R.progress_cells(62, 100))
        self.assertIn("▓", R.progress_cells(62, 100))
        self.assertIn("░", R.progress_cells(62, 100))


class TestCallbacks(unittest.TestCase):
    def test_v1_format_and_limit(self):
        for data in (
            R.cb("home", "show"), R.cb("net", "start", "vibevibe"),
            R.cb("wiz", "net", "robinhood"), R.cb("export", "yes", "json"),
            R.cb("field", "refresh"),
        ):
            self.assertTrue(data.startswith("v1:"))
            self.assertLessEqual(len(data.encode("utf-8")), 64)

    def test_parse_v1(self):
        self.assertEqual(R.parse_cb("v1:net:start:vibevibe"), ("net", "start", "vibevibe"))

    def test_parse_stale_version_returns_none(self):
        # Устаревшая версия → вызывающий перерисовывает «Поле» (п.3 ТЗ).
        self.assertIsNone(R.parse_cb("v0:home:show"))
        self.assertIsNone(R.parse_cb("v2:net:start:x"))

    def test_parse_legacy_compat(self):
        screen, _act, _arg = R.parse_cb("menu:home")
        self.assertEqual(screen, "menu:home")

    def test_all_legacy_buttons_short(self):
        for data in (
            "menu:home", "network:list", "network:stop", "network:start:vibevibe",
            "export:csv", "export:yes:json", "wallets:page:1", "stats", "status",
            "doctor", "history", "ref", "links", "farm:start", "monitor:toggle",
        ):
            self.assertLessEqual(len(data.encode("utf-8")), 64, data)


class TestTokenRedaction(unittest.TestCase):
    def test_pattern(self):
        evil = "https://api.telegram.org/bot123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11/getMe"
        clean = R.redact_token(evil)
        self.assertNotIn("ABC-DEF1234", clean)
        self.assertIn("bot<TOKEN>", clean)

    def test_traceback_text(self):
        tb = 'File "x.py", line 1\nTelegramAPIError: https://api.telegram.org/bot999:secret-token_here/x'
        self.assertNotIn("secret-token_here", R.redact_token(tb))

    def test_plain_text_untouched(self):
        self.assertEqual(R.redact_token("harvest // ПОЛЕ [RUN]"), "harvest // ПОЛЕ [RUN]")


@unittest.skipUnless(HAS_BOT, "aiogram не установлен")
class TestRedactingFilter(unittest.TestCase):
    def test_log_record_redacted(self):
        bt.install_token_redaction()
        with self.assertLogs("portal.bot_telegram", level="WARNING") as cm:
            logging.getLogger("portal.bot_telegram").warning(
                "fail https://api.telegram.org/bot123:ABC-secret/getUpdates"
            )
        self.assertNotIn("ABC-secret", "\n".join(cm.output))
        self.assertIn("bot<TOKEN>", "\n".join(cm.output))


class TestWalletCells(unittest.TestCase):
    def test_states(self):
        self.assertEqual(cell_state(0, 0, -1, 3), "i")
        self.assertEqual(cell_state(5, 5, 3, 3), "o")
        self.assertEqual(cell_state(0, 0, 3, 3), "w")
        self.assertEqual(cell_state(2, 5, 3, 3), "e")
        self.assertEqual(cell_state(4, 4, 1, 3), "r")

    def test_build_matches_publish(self):
        rows = [(0, 0, None), (5, 5, 3), (2, 5, 3)]
        self.assertEqual(build_cells(rows, 3), "ioe")

    def test_grid_rows_and_overflow(self):
        g = render_grid("o" * 25, 25)
        self.assertEqual(len(g.split("\n")), 3)  # 10/10/5→добито до 10
        g2 = render_grid("o" * 100, 150)
        self.assertIn("+50", g2)

    def test_grid_glyphs(self):
        g = render_grid("iwoer", 5)
        for gl in ("·", "■", "▣", "x", "~"):
            self.assertIn(gl, g)


@unittest.skipUnless(HAS_BOT, "aiogram не установлен")
class TestExportCooldownAudit(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        bt._flood_last.clear()

    def tearDown(self):
        bt._flood_last.clear()

    def test_export_cooldown_is_60s(self):
        self.assertEqual(bt._flood_interval("export"), 60.0)
        self.assertEqual(bt._flood_interval("wallets"), bt._FLOOD_INTERVAL_S)
        ok, _ = bt._flood_ok(777001, "export")
        self.assertTrue(ok)
        ok, wait = bt._flood_ok(777001, "export")
        self.assertFalse(ok)
        self.assertGreaterEqual(wait, 59)

    async def test_export_yes_writes_audit(self):
        from portal.bot_telegram import build_dispatcher

        cfg = PortalConfig()
        cfg.telegram_allow_ids = [123]

        class _D:
            async def export_wallets(self, fmt="csv"):
                return "a,b\n1,2\n", "wallets.csv"

            async def statistics(self):
                return {"running": False, "pool": {}, "db": {}}

            async def cycle_history(self):
                return []

        dp = build_dispatcher(cfg, _D())

        class _Msg:
            bot = mock.Mock()
            bot.send_document = mock.AsyncMock(return_value=mock.Mock(message_id=1))
            bot.delete_message = mock.AsyncMock()

            async def answer(self, *a, **k):
                pass

            async def edit_text(self, *a, **k):
                pass

        call = mock.Mock()
        call.from_user.id = 123
        call.data = "export:yes:csv"
        call.message = _Msg()
        call.answer = mock.AsyncMock()
        with self.assertLogs("portal.bot_telegram", level="INFO") as cm:
            await dp.callback_query.handlers[0].callback(call)
        self.assertTrue(any("[AUDIT] export user=123 fmt=csv" in line for line in cm.output))


@unittest.skipUnless(HAS_BOT, "aiogram не установлен")
class TestConflict(unittest.TestCase):
    def test_backoff_grows(self):
        t = bt.ConflictTracker()
        d1 = t.hit()
        d2 = t.hit()
        self.assertGreater(d2, d1)
        self.assertEqual(t.status(), "CONFLICT")

    def test_single_notify(self):
        t = bt.ConflictTracker()
        for _ in range(4):
            t.hit()
            self.assertFalse(t.needs_notify())
        t.hit()
        self.assertTrue(t.needs_notify())
        self.assertFalse(t.needs_notify())  # одно уведомление (п.7 ТЗ)

    def test_resolve(self):
        t = bt.ConflictTracker()
        t.hit()
        t.resolve()
        self.assertEqual(t.status(), "ok")
        self.assertEqual(bt.bot_conflict_status(), "ok")  # глобальный чист после resolve


@unittest.skipUnless(HAS_BOT, "aiogram не установлен")
class TestStrangerSeesNothing(unittest.IsolatedAsyncioTestCase):
    async def test_chuzhoi_gets_no_data(self):
        from portal.bot_telegram import build_dispatcher

        cfg = PortalConfig()
        cfg.telegram_allow_ids = [123]
        dp = build_dispatcher(cfg, mock.Mock())

        class _M:
            from_user = mock.Mock()
            from_user.id = 999
            text = "/status"
            document = None
            bot = mock.Mock()
            answers = None

            async def answer(self, *a, **k):
                if self.answers is None:
                    self.answers = []
                self.answers.append((a, k))

        msg = _M()
        msg.from_user.id = 999
        await dp.message.handlers[1].callback(msg)
        self.assertTrue(msg.answers is None or msg.answers == [])

        call = mock.Mock()
        call.from_user.id = 999
        call.data = "menu:home"
        call.message = mock.Mock()
        call.message.answer = mock.AsyncMock()
        call.message.edit_text = mock.AsyncMock()
        call.answer = mock.AsyncMock()
        await dp.callback_query.handlers[0].callback(call)
        call.message.edit_text.assert_not_awaited()


@unittest.skipUnless(HAS_BOT, "aiogram не установлен")
class TestBotCommands(unittest.TestCase):
    def test_commands_ru(self):
        names = [c for c, _d in bt.BOT_COMMANDS_RU]
        for must in ("start", "cmd", "cfg", "check", "ref", "doctor",
                     "history", "export", "wallets", "balance", "profiles"):
            self.assertIn(must, names)


class TestRenderProfiles(unittest.TestCase):
    def test_home_distribution_bar(self):
        t = R.render_home(_state(), mode="novice", cells="o" * 10 + "e" * 5 + "i" * 5, cells_total=20)
        self.assertIn("harvest // ПОЛЕ", t)
        self.assertIn("▓", t)
        self.assertIn("в работе", t)
        self.assertIn("i 5 · w 0 · o 10 · e 5 · r 0", t)
        self.assertIn("(10/20)", t)
        self.assertIn("btn_terminal", R._STR)
        self.assertIn("терминал", R.tr("btn_terminal", "ru").lower())

    def test_home_empty_cells(self):
        t = R.render_home(_state(), mode="novice", cells="", cells_total=0)
        self.assertIn("(0/50)", t)

    def test_ru_and_en(self):
        s = {"n": 1000, "activity_lo": 0.3, "activity_hi": 2.0, "corridors": 18,
             "rest": 999, "burst": 998, "weekend": 500, "windows": 7}
        t = R.render_profiles(s, "ru")
        self.assertIn("ПРОФИЛИ", t)
        self.assertIn("1000", t)
        self.assertIn("18", t)
        t2 = R.render_profiles(s, "en")
        self.assertIn("PROFILES", t2)
        self.assertIn("corridors", t2)
        self.assertLessEqual(len(t2), 4096)

    def test_empty_and_no_addresses(self):
        t = R.render_profiles({}, "ru")
        self.assertIn("ПРОФИЛИ", t)
        self.assertNotIn("0x", t)
        self.assertIn("btn_profiles", R._STR)
        self.assertIn("Профили", R.tr("btn_profiles", "ru"))
        self.assertIn("Profiles", R.tr("btn_profiles", "en"))


class TestRenderEdges(unittest.TestCase):
    def test_bad_inputs(self):
        self.assertEqual(R.sparkline(["x"]), "—")
        self.assertEqual(R.sparkline([5, 5]), "▁▁")
        self.assertIn("(0/0)", R.progress_cells("x", None))
        self.assertIn("(0/0)", R.progress_cells(None, "y"))
        self.assertEqual(R.short_addr(""), "")
        self.assertEqual(R.short_addr(None), "")

    def test_cb_limits(self):
        with self.assertRaises(ValueError):
            R.cb("home", "show", "x" * 100)
        self.assertIsNone(R.parse_cb("v1:home"))
        self.assertIsNone(R.parse_cb(""))

    def test_other_screens(self):
        st = {"running": True, "paused": False, "network": "n", "chain_id": 1}
        self.assertIn("СТОП", R.render_stop_confirm(st))
        self.assertIn("60", R.render_danger("pro", "en"))
        self.assertIn("DANGER", R.render_danger("pro", "en"))
        self.assertIn(".enc", R.render_export_confirm("csv", enc=True, lang="en"))
        self.assertIn(" ПАРОЛЬ", R.render_enc_ask("ru"))
        self.assertIn("SCALE", R.render_scale(7, "en"))
        self.assertIn("BUILDER", R.render_builder("en"))
        self.assertIn("DONE", R.render_master_done("python auto.py", "en"))
        self.assertIn("АЛЕРТ", R.render_alert("bot123:SECRET boom"))
        self.assertNotIn("SECRET", R.render_alert("bot123:SECRET boom"))
        text, pages = R.render_wallets_list([], 1, 4, "en")
        self.assertEqual(pages, 1)
        self.assertIn("WALLETS", text)
        text, pages = R.render_wallets_list(
            [{"address": "0x" + "ab" * 20, "total_actions": 2}] * 5, 9, 4, "ru")
        self.assertEqual(pages, 2)
        self.assertIn("…", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
