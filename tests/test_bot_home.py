"""Приёмка v1 ТЗ этап 2 (п.2-6): Поле, навигация, мастер, режимы, экспорт, Live.

Через dispatcher: старые legacy-ветки не задеты (см. test_bot_telegram.py).
"""

import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from portal import bot_ops as _ops
from portal import bot_render as R
from portal import bot_settings as _bss

try:
    import portal.bot_settings as bs
    import portal.bot_telegram as bt
    from portal.bot_telegram import build_dispatcher
    from portal.config import PortalConfig

    HAS_BOT = True
except Exception:  # noqa: BLE001
    bs = _bss
    HAS_BOT = False


class _U:
    def __init__(self, uid):
        self.id = uid


class _Chat:
    def __init__(self, cid=123):
        self.id = cid


class _M:
    def __init__(self, uid=123, text=""):
        self.from_user = _U(uid)
        self.text = text
        self.chat = _Chat(uid)
        self.bot = mock.Mock()
        self.bot.send_document = mock.AsyncMock(return_value=mock.Mock(message_id=11))
        self.bot.delete_message = mock.AsyncMock()
        self.answers = []
        self.deleted = False

    async def answer(self, *args, **kwargs):
        self.answers.append((args, kwargs))
        m = mock.Mock()
        m.message_id = 99
        return m

    async def delete(self):
        self.deleted = True


class _CMessage:
    def __init__(self):
        self.bot = mock.Mock()
        self.bot.send_document = mock.AsyncMock(return_value=mock.Mock(message_id=7))
        self.bot.delete_message = mock.AsyncMock()
        self.chat = _Chat()
        self.answers = []
        self.edits = []

    async def answer(self, *args, **kwargs):
        self.answers.append((args, kwargs))
        m = mock.Mock()
        m.message_id = 77
        return m

    async def edit_text(self, *args, **kwargs):
        self.edits.append((args, kwargs))


class _C:
    def __init__(self, uid=123, data=""):
        self.from_user = _U(uid)
        self.data = data
        self.message = _CMessage()
        self.answered = []

    async def answer(self, *args, **kwargs):
        self.answered.append((args, kwargs))


class _State:
    def __init__(self):
        self._state = None
        self._data = {}

    async def get_state(self):
        return self._state

    async def set_state(self, s):
        self._state = getattr(s, "state", s)

    async def update_data(self, **kw):
        self._data.update(kw)

    async def get_data(self):
        return dict(self._data)

    async def clear(self):
        self._state = None
        self._data = {}


class HomeDaemon:
    db_path = None  # настройки в памяти (портал-БД недоступна в тестах)

    def __init__(self):
        self.running = False
        self.paused = False
        self.stopped = 0
        self._wallets = [
            {"address": f"0xb3E4{'aa' * 18}B029E{i}", "private_key": "k",
             "mnemonic": "m", "total_actions": i}
            for i in range(3)
        ]

    def networks_info(self):
        return [
            {"slug": "vibevibe", "name": "vibe/vibe", "chain_id": 46630,
             "current": True, "running": self.running},
        ]

    async def statistics(self):
        return {"running": self.running, "network": "vibevibe",
                "pool": {"cycles": 5, "errors": 0, "paused": self.paused,
                         "dyn_workers": 4},
                "db": {"cycles": 5}, "health_factor": 1.0,
                "wallet_count": len(self._wallets)}

    async def wallet_cells(self):
        return "o" * len(self._wallets), len(self._wallets)

    async def wallet_count(self):
        return len(self._wallets)

    async def all_wallets(self):
        return list(self._wallets)

    async def create_wallets(self, count):
        made = [{"address": f"0x{i:040x}"} for i in range(count)]
        self._wallets.extend(made)
        return made

    async def cycle_history(self):
        return []

    async def export_wallets(self, fmt="csv"):
        if fmt == "json":
            return '[{"address": "0x1", "private_key": "abc"}]', "wallets.json"
        return "address,private_key\n0x1,abc\n", "wallets.csv"

    async def start(self):
        self.running = True
        return True

    async def stop(self):
        self.running = False
        self.stopped += 1
        return True

    async def pause(self):
        self.paused = True
        return True

    async def resume(self):
        self.paused = False
        return True

    async def wallet_balances(self):
        return [{"address": w["address"], "balance": 1.0} for w in self._wallets]

    async def rpc_health(self):
        return {"nodes": [{"url": "https://rpc.test", "ok": True, "latency_ms": 12}]}

    def scheduler_state(self):
        return {"running": False, "tasks": []}

    def balance_alerts_state(self):
        return {"enabled": False, "threshold": 0.001}

    async def set_balance_alerts(self, enabled, threshold):
        return {"enabled": bool(enabled), "threshold": float(threshold)}

    async def _check_faucet_once(self):
        return 1

    async def faucet_report(self):
        return [{"type": "chainstack", "url": "https://mcp.chainstack.com/mcp",
                 "alive": True, "reason": "MCP handshake ok"}]


def _cfg():
    cfg = PortalConfig()
    cfg.telegram_allow_ids = [123]
    return cfg


def _payloads(call):
    kb = call.message.edits[-1][1]["reply_markup"]
    return [b.callback_data for row in kb.inline_keyboard for b in row]


@unittest.skipUnless(HAS_BOT, "aiogram не установлен")
class TestHome(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        bt._flood_last.clear()
        bs._mem.clear()

    def tearDown(self):
        bt._flood_last.clear()
        bs._mem.clear()
        for t in list(bt._live.values()):
            t.cancel()
        bt._live.clear()

    async def test_layout_and_callbacks(self):
        dp = build_dispatcher(_cfg(), HomeDaemon())
        call = _C(data="v1:home:show")
        await dp.callback_query.handlers[0].callback(call, _State())
        text = call.message.edits[0][0][0]
        self.assertIn("harvest //", text)
        self.assertIn("Остановлен", text)
        self.assertIn("vibe/vibe (46630)", text)
        kb = call.message.edits[0][1]["reply_markup"]
        self.assertEqual(len(kb.inline_keyboard), 3)  # run + терминал + обновить/меню
        payloads = [b.callback_data for row in kb.inline_keyboard for b in row]
        self.assertIn("v1:menu:show", payloads)
        was = [b.web_app.url for row in kb.inline_keyboard for b in row
               if getattr(b, "web_app", None)]
        self.assertIn("https://ssrjkk.github.io/harvest/", was)
        for p in payloads:
            if p is None:
                continue  # URL-кнопка (терминал) — без callback_data
            self.assertLessEqual(len(p.encode("utf-8")), 64, p)
        # Запуск и Стоп не рядом: стоит Запуск.
        self.assertIn("v1:run:start", payloads)
        self.assertNotIn("v1:run:stop", payloads)

    async def test_running_shows_stop(self):
        daemon = HomeDaemon()
        daemon.running = True
        dp = build_dispatcher(_cfg(), daemon)
        call = _C(data="v1:home:show")
        await dp.callback_query.handlers[0].callback(call, _State())
        payloads = _payloads(call)
        self.assertIn("v1:run:stop", payloads)
        self.assertNotIn("v1:run:start", payloads)

    async def test_nav_has_back_and_home(self):
        dp = build_dispatcher(_cfg(), HomeDaemon())
        cb = dp.callback_query.handlers[0].callback
        for data in ("v1:scale:show", "v1:builder:show", "v1:ref:show",
                     "v1:diag:show", "v1:danger:show", "v1:history:show",
                     "v1:wallets:page:1"):
            call = _C(data=data)
            await cb(call, _State())
            self.assertTrue(call.message.edits, data)
            payloads = _payloads(call)
            self.assertTrue(any(p == "v1:home:show" for p in payloads), data)

    async def test_stale_redraws_home(self):
        dp = build_dispatcher(_cfg(), HomeDaemon())
        call = _C(data="v0:home:show")
        await dp.callback_query.handlers[0].callback(call, _State())
        self.assertTrue(
            any(a[0] and "устарел" in str(a[0][0]).lower() for a in call.answered))
        self.assertIn("harvest //", call.message.edits[0][0][0])

    async def test_unknown_v1_redraws_home(self):
        dp = build_dispatcher(_cfg(), HomeDaemon())
        call = _C(data="v1:nope:nope")
        await dp.callback_query.handlers[0].callback(call, _State())
        self.assertIn("harvest //", call.message.edits[0][0][0])

    async def test_stop_confirm_flow(self):
        daemon = HomeDaemon()
        daemon.running = True
        dp = build_dispatcher(_cfg(), daemon)
        cb = dp.callback_query.handlers[0].callback
        call = _C(data="v1:run:stop")
        await cb(call, _State())
        self.assertIn("СТОП", call.message.edits[0][0][0])
        call2 = _C(data="v1:run:stop_yes")
        await cb(call2, _State())
        self.assertEqual(daemon.stopped, 1)
        self.assertIn("harvest //", call2.message.edits[0][0][0])

    async def test_start_redraws(self):
        daemon = HomeDaemon()
        dp = build_dispatcher(_cfg(), daemon)
        call = _C(data="v1:run:start")
        await dp.callback_query.handlers[0].callback(call, _State())
        self.assertTrue(daemon.running)
        self.assertIn("Активен", call.message.edits[0][0][0])

    async def test_live_toggle(self):
        dp = build_dispatcher(_cfg(), HomeDaemon())
        cb = dp.callback_query.handlers[0].callback
        call = _C(data="v1:home:live")
        await cb(call, _State())
        self.assertIn(123, bt._live)
        call2 = _C(data="v1:home:live")
        await cb(call2, _State())
        self.assertNotIn(123, bt._live)

    async def test_live_loop_autostop(self):
        daemon = HomeDaemon()
        msg = _CMessage()
        await bt._live_loop(123, msg, daemon, every=0.02, ttl=0.05)
        self.assertGreaterEqual(len(msg.edits), 2)
        self.assertNotIn(123, bt._live)

    async def test_start_command_resets_and_shows_home(self):
        dp = build_dispatcher(_cfg(), HomeDaemon())
        st = _State()
        await st.update_data(m_net="vibevibe")
        msg = _M(uid=123, text="/start")
        await dp.message.handlers[0].callback(msg, st)
        self.assertEqual(await st.get_data(), {})
        self.assertIn("harvest //", msg.answers[0][0][0])

    async def test_cancel_clears(self):
        dp = build_dispatcher(_cfg(), HomeDaemon())
        st = _State()
        await st.update_data(m_net="vibevibe")
        msg = _M(uid=123, text="/cancel")
        await dp.message.handlers[1].callback(msg, st)
        self.assertEqual(await st.get_data(), {})
        self.assertIn("ОТМЕНА", msg.answers[0][0][0])


@unittest.skipUnless(HAS_BOT, "aiogram не установлен")
class TestMaster(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        bt._flood_last.clear()

    def tearDown(self):
        bt._flood_last.clear()

    async def _run(self, profile="normal", faucet="1", wallets="50", cycles="5"):
        dp = build_dispatcher(_cfg(), HomeDaemon())
        cb = dp.callback_query.handlers[0].callback
        st = _State()
        call = _C(data="v1:master:cmd")
        await cb(call, st)
        self.assertIn("1/3", call.message.answers[0][0][0])
        call = _C(data="v1:master:net:vibevibe")
        await cb(call, st)
        call = _C(data=f"v1:master:w:{wallets}")
        await cb(call, st)
        call = _C(data=f"v1:master:c:{cycles}")
        await cb(call, st)
        call = _C(data=f"v1:master:profile:{profile}")
        await cb(call, st)
        call = _C(data=f"v1:master:faucet:{faucet}")
        await cb(call, st)
        return st, call

    async def test_full_normal(self):
        _st, call = await self._run()
        text = call.message.answers[-1][0][0]
        self.assertIn("ГОТОВО", text)
        self.assertIn("--wallets 50 --cycles 5", text)
        self.assertNotIn("--fast", text)
        self.assertNotIn("--dry-run", text)

    async def test_profiles_map_flags(self):
        _st, call = await self._run(profile="fast")
        self.assertIn("--fast", call.message.answers[-1][0][0])
        _st, call = await self._run(profile="quiet")
        self.assertIn("--dry-run", call.message.answers[-1][0][0])
        _st, call = await self._run(faucet="0")
        self.assertIn("--skip-faucet", call.message.answers[-1][0][0])

    async def test_yaml_and_verify(self):
        dp = build_dispatcher(_cfg(), HomeDaemon())
        cb = dp.callback_query.handlers[0].callback
        st, _call = await self._run()
        st2 = st
        call = _C(data="v1:master:verify")
        await cb(call, st2)
        self.assertIn("[ok]", call.message.answers[0][0][0])
        send_doc = mock.AsyncMock()
        call2 = _C(data="v1:master:yaml")
        call2.message.bot.send_document = send_doc
        await cb(call2, st2)
        send_doc.assert_awaited_once()
        self.assertEqual(send_doc.await_args.kwargs["document"].filename,
                         "config_vibevibe.yaml")

    async def test_custom_input(self):
        dp = build_dispatcher(_cfg(), HomeDaemon())
        cb = dp.callback_query.handlers[0].callback
        st = _State()
        await cb(_C(data="v1:master:cmd"), st)
        await cb(_C(data="v1:master:net:arc"), st)
        await cb(_C(data="v1:master:wcustom"), st)
        msg = _M(uid=123, text="77")
        await dp.message.handlers[1].callback(msg, st)
        self.assertIn("циклов", msg.answers[0][0][0])
        msg2 = _M(uid=123, text="много")
        st2 = _State()
        await st2.set_state(bt.MasterWiz.wallets)
        await dp.message.handlers[1].callback(msg2, st2)
        self.assertIn("[err]", msg2.answers[0][0][0])

    async def test_ttl_expiry(self):
        import time as _t

        dp = build_dispatcher(_cfg(), HomeDaemon())
        st = _State()
        await st.update_data(m_net="vibevibe", m_started=_t.monotonic() - 9999)
        call = _C(data="v1:master:w:10")
        await dp.callback_query.handlers[0].callback(call, st)
        self.assertEqual(await st.get_data(), {})

    async def test_restart_and_cancel(self):
        dp = build_dispatcher(_cfg(), HomeDaemon())
        cb = dp.callback_query.handlers[0].callback
        st, _c = await self._run()
        call = _C(data="v1:master:restart")
        await cb(call, st)
        self.assertIn("1/3", call.message.answers[0][0][0])
        call2 = _C(data="v1:master:cancel")
        await cb(call2, st)
        self.assertIn("ОТМЕНА", call2.message.answers[0][0][0])


class TestProfilesSummary(unittest.TestCase):
    def test_counts_and_bounds(self):
        addrs = [f"0x{i:040x}" for i in range(30)]
        beh = {"enabled": True, "rest_probability": [0.0, 1.0],
               "weekend_strength": [0.0, 1.0], "day_start": [0.0, 24.0]}
        s = _ops.profiles_summary(addrs, beh)
        self.assertEqual(s["n"], 30)
        self.assertLessEqual(s["activity_lo"], s["activity_hi"])
        self.assertGreaterEqual(s["corridors"], 1)
        for k in ("rest", "burst", "weekend", "windows"):
            self.assertGreaterEqual(s[k], 0)
            self.assertLessEqual(s[k], 30)

    def test_empty_and_deterministic(self):
        self.assertEqual(_ops.profiles_summary([], {})["n"], 0)
        addrs = [f"0x{i:040x}" for i in range(10)]
        self.assertEqual(_ops.profiles_summary(addrs, {}),
                         _ops.profiles_summary(addrs, {}))

    def test_no_addresses_in_values(self):
        s = _ops.profiles_summary(["0x" + "ab" * 20], {})
        blob = " ".join(str(v) for v in s.values())
        self.assertNotIn("0xab", blob)


@unittest.skipUnless(HAS_BOT, "aiogram не установлен")
class TestModesLang(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        bs._mem.clear()

    def tearDown(self):
        bs._mem.clear()

    async def test_toggle_mode_and_lang(self):
        dp = build_dispatcher(_cfg(), HomeDaemon())
        cb = dp.callback_query.handlers[0].callback
        call = _C(data="v1:danger:show")
        await cb(call, _State())
        self.assertIn("Новичок", call.message.edits[0][0][0])
        call2 = _C(data="v1:danger:mode")
        await cb(call2, _State())
        self.assertIn("Про", call2.message.edits[0][0][0])
        call3 = _C(data="v1:danger:lang")
        await cb(call3, _State())
        self.assertIn("EN", call3.message.edits[0][0][0])

    async def test_home_en(self):
        await bs.set_lang(None, 123, "en")
        dp = build_dispatcher(_cfg(), HomeDaemon())
        call = _C(data="v1:home:show")
        await dp.callback_query.handlers[0].callback(call, _State())
        self.assertIn("[ Start ]", str(call.message.edits[0][1]["reply_markup"].inline_keyboard))

    async def test_bad_values_normalized(self):
        self.assertEqual(await bs.get_settings(None, 999), ("novice", "ru"))
        await bs.set_mode(None, 999, "pro")
        await bs.set_lang(None, 999, "en")
        self.assertEqual(await bs.get_settings(None, 999), ("pro", "en"))


@unittest.skipUnless(HAS_BOT, "aiogram не установлен")
class TestKeysEncExport(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        bt._flood_last.clear()
        bs._mem.clear()

    def tearDown(self):
        bt._flood_last.clear()
        bs._mem.clear()

    async def test_wallets_list_hides_keys_short_addrs(self):
        dp = build_dispatcher(_cfg(), HomeDaemon())
        call = _C(data="v1:wallets:page:1")
        await dp.callback_query.handlers[0].callback(call, _State())
        text = call.message.edits[0][0][0]
        self.assertNotIn("private_key", text)
        self.assertNotIn("aa" * 18, text)
        self.assertIn("…", text)  # сокращённый адрес

    async def test_keys_confirm_flow(self):
        old, bt._EXPORT_AUTODELETE_S = bt._EXPORT_AUTODELETE_S, 0
        try:
            dp = build_dispatcher(_cfg(), HomeDaemon())
            cb = dp.callback_query.handlers[0].callback
            call = _C(data="v1:wallets:keysask:1")
            await cb(call, _State())
            self.assertIn("КЛЮЧИ", call.message.edits[0][0][0])
            msg = _CMessage()
            call2 = _C(data="v1:wallets:keysyes:1")
            call2.message = msg
            with self.assertLogs("portal.bot_telegram", level="INFO") as cm:
                await cb(call2, _State())
            self.assertTrue(any("[AUDIT] keys user=123" in line for line in cm.output))
            sent = msg.answers[0]
            self.assertTrue(sent[1].get("protect_content"))
            await asyncio.sleep(0.05)
            msg.bot.delete_message.assert_awaited_once()
        finally:
            bt._EXPORT_AUTODELETE_S = old

    async def test_keys_cooldown(self):
        dp = build_dispatcher(_cfg(), HomeDaemon())
        cb = dp.callback_query.handlers[0].callback
        await cb(_C(data="v1:wallets:keysyes:1"), _State())
        call2 = _C(data="v1:wallets:keysyes:1")
        await cb(call2, _State())
        self.assertTrue(any("жди" in a[0][0] or "wait" in a[0][0]
                            for a in call2.message.answers))

    async def test_enc_flow(self):
        dp = build_dispatcher(_cfg(), HomeDaemon())
        cb = dp.callback_query.handlers[0].callback
        st = _State()
        call = _C(data="v1:enc:ask:csv")
        await cb(call, st)
        self.assertIn("ПАРОЛЬ", call.message.answers[0][0][0])
        msg = _M(uid=123, text="s3cret-pw")
        with self.assertLogs("portal.bot_telegram", level="INFO") as cm:
            await dp.message.handlers[1].callback(msg, st)
        self.assertTrue(any("fmt=enc-csv" in line for line in cm.output))
        kw = msg.bot.send_document.await_args.kwargs
        self.assertTrue(kw["document"].filename.endswith(".enc"))
        self.assertTrue(kw.get("protect_content"))
        self.assertTrue(msg.deleted)  # сообщение с паролем потёрто
        # пароль нигде не залогирован
        self.assertFalse(any("s3cret-pw" in line for line in cm.output))

    async def test_exp_v1_cooldown(self):
        dp = build_dispatcher(_cfg(), HomeDaemon())
        cb = dp.callback_query.handlers[0].callback
        await cb(_C(data="v1:exp:yes:csv"), _State())
        call2 = _C(data="v1:exp:yes:csv")
        await cb(call2, _State())
        self.assertTrue(any("жди" in a[0][0] or "wait" in a[0][0]
                            for a in call2.message.answers))


@unittest.skipUnless(HAS_BOT, "aiogram не установлен")
class TestStatusCard(unittest.IsolatedAsyncioTestCase):
    def test_render_png(self):
        from portal import status_card as _card

        self.assertTrue(_card.available())
        png = _card.render_card(
            {"running": True, "paused": False, "network": "vibe/vibe",
             "chain_id": 46630, "wallet_count": 3, "cycle": 5,
             "errors": 0, "health_factor": 1.0, "updated": "10:00 UTC"},
            cells="oiwe" + "r" * 12, total=20)
        self.assertTrue(png.startswith(b"\x89PNG"))
        self.assertGreater(len(png), 2000)

    def test_render_empty(self):
        from portal import status_card as _card

        png = _card.render_card({})
        self.assertTrue(png.startswith(b"\x89PNG"))

    def test_render_with_pnl(self):
        """Карточка показывает PnL прогона и не ломает сетку."""
        from PIL import Image

        from portal import status_card as _card

        base = {"running": True, "network": "vibe/vibe", "chain_id": 46630,
                "wallet_count": 3, "cycle": 5, "errors": 2, "health_factor": 1.0,
                "updated": "10:00 UTC"}
        without = _card.render_card(base, cells="oiwe" + "r" * 12, total=20)
        with_pnl = _card.render_card(
            {**base, "pnl": {"actions_ok": 1284, "actions_fail": 37,
                             "farm_delta": -0.184213}},
            cells="oiwe" + "r" * 12, total=20)
        self.assertTrue(with_pnl.startswith(b"\x89PNG"))
        # с PnL карточка выше — две дополнительные строки
        h0 = Image.open(__import__("io").BytesIO(without)).size[1]
        h1 = Image.open(__import__("io").BytesIO(with_pnl)).size[1]
        self.assertGreater(h1, h0)

    def test_render_pnl_bad_values(self):
        """Битые значения PnL не должны ронять отрисовку карточки."""
        from portal import status_card as _card

        png = _card.render_card(
            {"network": "n", "health_factor": "xx", "updated": "t",
             "pnl": {"actions_ok": "нет", "actions_fail": None, "farm_delta": "не число"}},
            cells="o", total=10)
        self.assertTrue(png.startswith(b"\x89PNG"))

    def test_palette_and_font_fallback(self):
        from portal import status_card as _card

        with mock.patch("builtins.open", side_effect=OSError("x")):
            pal = _card._palette()
        self.assertEqual(pal["bg"], "#14110A")
        real_truetype = _card.ImageFont.truetype

        def _boom(path, *a, **k):
            if "DejaVu" in str(path) or "Windows" in str(path):
                raise OSError("x")
            return real_truetype(path, *a, **k)

        with mock.patch("PIL.ImageFont.truetype", side_effect=_boom):
            fnt = _card._font(20)
        self.assertIsNotNone(fnt)
        png = _card.render_card(
            {"running": False, "network": "n", "health_factor": "xx",
             "updated": "t"}, cells="o", total=10)
        self.assertTrue(png.startswith(b"\x89PNG"))

    async def test_card_button_sends_photo(self):
        dp = build_dispatcher(_cfg(), HomeDaemon())
        call = _C(data="v1:diag:card")
        call.message.answer_photo = mock.AsyncMock()
        await dp.callback_query.handlers[0].callback(call, _State())
        call.message.answer_photo.assert_awaited_once()
        self.assertTrue(call.message.answer_photo.await_args.args[0].filename
                        .endswith(".png"))


@unittest.skipUnless(HAS_BOT, "aiogram не установлен")
class TestRenderStrings(unittest.TestCase):
    def test_tr_fallback(self):
        self.assertEqual(R.tr("btn_home", "xx"), "[ Поле ]")
        self.assertEqual(R.tr("nope", "ru"), "nope")

    def test_danger_mentions_restart_limit(self):
        t = R.render_danger("novice", "ru")
        self.assertIn("60", t)
        self.assertIn("рестарт", t.lower())


class TestBotSettingsDb(unittest.IsolatedAsyncioTestCase):
    async def test_sqlite_roundtrip(self):
        import sqlite3
        import tempfile

        import portal.bot_settings as _bs

        with tempfile.TemporaryDirectory() as td:
            db = td + "/s.db"
            self.assertEqual(await _bs.get_settings(db, 7), ("novice", "ru"))
            self.assertEqual(await _bs.set_mode(db, 7, "pro"), "pro")
            self.assertEqual(await _bs.set_lang(db, 7, "en"), "en")
            self.assertEqual(await _bs.get_settings(db, 7), ("pro", "en"))
            con = sqlite3.connect(db)
            self.assertEqual(
                con.execute("SELECT mode, lang FROM bot_settings WHERE user_id=7"
                            ).fetchone(), ("pro", "en"))
            con.close()

    async def test_broken_rows_normalized(self):
        import sqlite3
        import tempfile

        import portal.bot_settings as _bs

        with tempfile.TemporaryDirectory() as td:
            db = td + "/s.db"
            con = sqlite3.connect(db)
            con.execute("CREATE TABLE bot_settings (user_id INTEGER PRIMARY KEY,"
                        " mode TEXT, lang TEXT, updated_at REAL)")
            con.execute("INSERT INTO bot_settings VALUES (9, 'xx', 'yy', 0)")
            con.commit()
            con.close()
            self.assertEqual(await _bs.get_settings(db, 9), ("novice", "ru"))
            self.assertEqual(await _bs.set_mode(db, 9, "xx"), "novice")

    async def test_unwritable_path_falls_back_to_memory(self):
        import portal.bot_settings as _bs

        bs._mem.clear()
        self.assertEqual(await _bs.set_mode("/no/such/dir/x.db", 3, "pro"), "pro")
        self.assertEqual(await _bs.get_settings("/no/such/dir/x.db", 3), ("pro", "ru"))
        bs._mem.clear()


@unittest.skipUnless(HAS_BOT, "aiogram не установлен")
class TestV1Gaps(unittest.IsolatedAsyncioTestCase):
    """Добор покрытия v1-веток (гейт CI 95%)."""

    def setUp(self):
        bt._flood_last.clear()
        bs._mem.clear()

    def tearDown(self):
        bt._flood_last.clear()
        bs._mem.clear()
        for t in list(bt._live.values()):
            t.cancel()
        bt._live.clear()

    async def test_net_pause_resume(self):
        dp = build_dispatcher(_cfg(), HomeDaemon())
        cb = dp.callback_query.handlers[0].callback
        call = _C(data="v1:net:show")
        await cb(call, _State())
        self.assertIn("СЕТИ", call.message.edits[0][0][0])
        for data, needle in (("v1:run:pause", "pause"), ("v1:run:resume", "resume")):
            call = _C(data=data)
            await cb(call, _State())
            self.assertTrue(any(needle in a[0][0] for a in call.message.answers))

    async def test_scale_add_paths(self):
        dp = build_dispatcher(_cfg(), HomeDaemon())
        cb = dp.callback_query.handlers[0].callback
        call = _C(data="v1:scale:add:xx")
        await cb(call, _State())
        self.assertTrue(any("+10" in a[0][0] for a in call.message.answers))
        call2 = _C(data="v1:scale:add:10")
        await cb(call2, _State())
        self.assertTrue(any("жди" in a[0][0] for a in call2.message.answers))

        class _Dead(HomeDaemon):
            async def create_wallets(self, count):
                raise RuntimeError("БД фермы не подключена")

        dp2 = build_dispatcher(_cfg(), _Dead())
        bt._flood_last.clear()
        call3 = _C(data="v1:scale:add:10")
        await dp2.callback_query.handlers[0].callback(call3, _State())
        self.assertTrue(any("не подключена" in a[0][0]
                            for a in call3.message.answers))

    async def test_master_stateless_and_bad(self):
        dp = build_dispatcher(_cfg(), HomeDaemon())
        cb = dp.callback_query.handlers[0].callback
        for data in ("v1:master:cmd", "v1:master:net:x", "v1:master:w:10",
                     "v1:master:w:xx", "v1:master:wcustom", "v1:master:c:2",
                     "v1:master:ccustom", "v1:master:profile:x",
                     "v1:master:faucet:x", "v1:master:yaml", "v1:master:verify",
                     "v1:master:restart", "v1:master:cancel",
                     "v1:enc:ask:csv", "v1:enc:cancel"):
            call = _C(data=data)
            await cb(call, None)
        call = _C(data="v1:master:net:nope")
        await cb(call, _State())
        self.assertIn("[err]", call.message.answers[0][0][0])
        for data in ("v1:master:w:99999", "v1:master:c:999999"):
            call = _C(data=data)
            st = _State()
            await st.update_data(m_started=__import__("time").monotonic())
            await cb(call, st)
            self.assertIn("[err]", call.message.answers[0][0][0])

    async def test_master_ccustom_text(self):
        dp = build_dispatcher(_cfg(), HomeDaemon())
        cb = dp.callback_query.handlers[0].callback
        st = _State()
        await cb(_C(data="v1:master:cmd"), st)
        await cb(_C(data="v1:master:net:vibevibe"), st)
        await cb(_C(data="v1:master:w:10"), st)
        await cb(_C(data="v1:master:ccustom"), st)
        msg = _M(uid=123, text="7")
        await dp.message.handlers[1].callback(msg, st)
        self.assertIn("3/3", msg.answers[0][0][0])
        msg2 = _M(uid=123, text="много")
        st2 = _State()
        await st2.set_state(bt.MasterWiz.cycles)
        await dp.message.handlers[1].callback(msg2, st2)
        self.assertIn("[err]", msg2.answers[0][0][0])
        msg3 = _M(uid=123, text="∞")
        st3 = _State()
        await st3.set_state(bt.MasterWiz.cycles)
        await dp.message.handlers[1].callback(msg3, st3)
        self.assertIn("3/3", msg3.answers[0][0][0])

    async def test_master_finish_and_yaml_fail(self):
        dp = build_dispatcher(_cfg(), HomeDaemon())
        cb = dp.callback_query.handlers[0].callback
        st = _State()
        await st.update_data(m_net="nope", m_wallets=10, m_cycles=1,
                             m_profile="normal", m_faucet=True,
                             m_started=__import__("time").monotonic())
        call = _C(data="v1:master:faucet:1")
        await cb(call, st)
        self.assertIn("[err]", call.message.answers[0][0][0])

        st2 = _State()
        await st2.update_data(m_net="vibevibe", m_wallets=10, m_cycles=1,
                              m_profile="normal", m_faucet=True,
                              m_started=__import__("time").monotonic(),
                              m_cmd="python auto.py --x", m_yaml="network: {}")
        with mock.patch.object(bt._bot_ops, "build_config_yaml",
                               side_effect=ValueError("bad")):
            call2 = _C(data="v1:master:yaml")
            await cb(call2, st2)
            self.assertIn("[err]", call2.message.answers[0][0][0])

    async def test_enc_text_paths(self):
        dp = build_dispatcher(_cfg(), HomeDaemon())
        st = _State()
        await st.set_state(bt.EncWiz.password)
        msg = _M(uid=123, text="")
        await dp.message.handlers[1].callback(msg, st)
        self.assertIn("[err]", msg.answers[0][0][0])
        msg2 = _M(uid=123, text="/start")
        st2 = _State()
        await st2.set_state(bt.EncWiz.password)
        await dp.message.handlers[1].callback(msg2, st2)
        self.assertEqual(await st2.get_data(), {})

    async def test_wallets_pages(self):
        dp = build_dispatcher(_cfg(), HomeDaemon())
        cb = dp.callback_query.handlers[0].callback
        for data in ("v1:wallets:page:xx", "v1:wallets:page:99",
                     "v1:wallets:keysask:xx", "v1:wallets:keysyes:xx"):
            call = _C(data=data)
            await cb(call, _State())
            self.assertTrue(call.message.edits or call.message.answers, data)

    async def test_exp_ask_and_card_unavailable(self):
        dp = build_dispatcher(_cfg(), HomeDaemon())
        cb = dp.callback_query.handlers[0].callback
        for fmt in ("csv", "json"):
            call = _C(data=f"v1:exp:ask:{fmt}")
            await cb(call, _State())
            text = call.message.edits[0][0][0]
            self.assertIn("подтверждение", text)
            self.assertIn(fmt.upper(), text)
        with mock.patch("portal.status_card.available", return_value=False):
            call2 = _C(data="v1:diag:card")
            await cb(call2, _State())
            self.assertIn("[err]", call2.message.answers[0][0][0])

    async def test_exp_yes_failure(self):
        dp = build_dispatcher(_cfg(), HomeDaemon())
        call = _C(data="v1:exp:yes:csv")
        with mock.patch("portal.bot_telegram.send_wallets_file",
                        new=mock.AsyncMock(side_effect=RuntimeError("x"))):
            await dp.callback_query.handlers[0].callback(call, _State())
        self.assertTrue(any("EXP-FAIL" in a[0][0] for a in call.message.answers))

    async def test_wallets_empty_and_paged(self):
        class _Empty(HomeDaemon):
            def __init__(self):
                self._wallets = []

            async def wallet_cells(self):
                return "", 0

        dp = build_dispatcher(_cfg(), _Empty())
        call = _C(data="v1:wallets:page:1")
        await dp.callback_query.handlers[0].callback(call, _State())
        kb = call.message.edits[0][1]["reply_markup"]
        payloads = [b.callback_data for row in kb.inline_keyboard for b in row]
        self.assertIn("wallets:create", payloads)

        class _Many(HomeDaemon):
            def __init__(self):
                self._wallets = [
                    {"address": f"0x{i:040x}", "total_actions": i} for i in range(5)]

            async def wallet_cells(self):
                return "o" * 5, 5

        dp2 = build_dispatcher(_cfg(), _Many())
        call2 = _C(data="v1:wallets:page:1")
        await dp2.callback_query.handlers[0].callback(call2, _State())
        kb = call2.message.edits[0][1]["reply_markup"]
        payloads = [b.callback_data for row in kb.inline_keyboard for b in row]
        self.assertIn("v1:wallets:page:2", payloads)

    async def test_scale_count_failure(self):
        class _NoCount(HomeDaemon):
            async def wallet_count(self):
                raise RuntimeError("x")

        dp = build_dispatcher(_cfg(), _NoCount())
        call = _C(data="v1:scale:show")
        await dp.callback_query.handlers[0].callback(call, _State())
        self.assertIn("—", call.message.edits[0][0][0])

    async def test_keys_no_chat(self):
        import portal.bot_telegram as _bt2

        old, _bt2._EXPORT_AUTODELETE_S = _bt2._EXPORT_AUTODELETE_S, 0
        try:
            msg = mock.Mock(spec=["answer", "bot"])
            msg.answer = mock.AsyncMock(return_value=mock.Mock(message_id=1))
            await _bt2._send_keys_temp(msg, "keys here")
        finally:
            _bt2._EXPORT_AUTODELETE_S = old

    async def test_enc_failures(self):
        import portal.bot_telegram as _bt2

        dp = build_dispatcher(_cfg(), HomeDaemon())
        st = _State()
        await st.set_state(_bt2.EncWiz.password)
        await st.update_data(enc_fmt="xml")
        msg = _M(uid=123, text="pw")
        with mock.patch.object(_bt2, "_send_encrypted",
                               new=mock.AsyncMock(side_effect=RuntimeError("x"))):
            await dp.message.handlers[1].callback(msg, st)
        self.assertTrue(any("ENC-FAIL" in a[0][0] for a in msg.answers))

        st2 = _State()
        await st2.set_state(_bt2.EncWiz.password)
        await st2.update_data(enc_fmt="csv")
        msg2 = _M(uid=123, text="")
        await dp.message.handlers[1].callback(msg2, st2)
        self.assertTrue(any("[err]" in a[0][0] for a in msg2.answers))

        with mock.patch("core.exporter.encrypt_file", return_value="/tmp/plain.csv"):
            with self.assertRaises(RuntimeError):
                await _bt2._send_encrypted(mock.Mock(), 123, HomeDaemon(),
                                           "csv", "pw")

    async def test_master_expired_and_alias(self):
        import time as _t

        dp = build_dispatcher(_cfg(), HomeDaemon())
        cb = dp.callback_query.handlers[0].callback
        st = _State()
        await cb(_C(data="v1:master:cmd"), st)
        call = _C(data="v1:master:net:vibe")
        await cb(call, st)
        self.assertIn("2/3", call.message.answers[0][0][0])

        old = {"m_started": _t.monotonic() - 9999}
        for data in ("v1:master:profile:quiet", "v1:master:yaml", "v1:master:verify"):
            stx = _State()
            await stx.update_data(**old, m_cmd="c", m_yaml="y")
            callx = _C(data=data)
            await cb(callx, stx)
            self.assertEqual(await stx.get_data(), {})

        stc = _State()
        callc = _C(data="v1:master:cancel")
        stc.clear = mock.AsyncMock(side_effect=RuntimeError("x"))  # type: ignore[method-assign]
        await cb(callc, stc)
        self.assertIn("ОТМЕНА", callc.message.answers[0][0][0])


@unittest.skipUnless(HAS_BOT, "aiogram не установлен")
class TestProfilesScreen(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        bt._flood_last.clear()
        bs._mem.clear()

    def tearDown(self):
        bt._flood_last.clear()
        bs._mem.clear()

    async def test_profiles_show_and_home_button(self):
        dp = build_dispatcher(_cfg(), HomeDaemon())
        cb = dp.callback_query.handlers[0].callback
        call = _C(data="v1:profiles:show")
        await cb(call, _State())
        self.assertIn("ПРОФИЛИ", call.message.edits[0][0][0])
        home = _C(data="v1:home:show")
        await cb(home, _State())
        payloads = [b for row in home.message.edits[0][1]["reply_markup"].inline_keyboard
                    for b in row]
        self.assertIn("v1:menu:show", [b.callback_data for b in payloads])

    async def test_menu_screen_lists_all(self):
        dp = build_dispatcher(_cfg(), HomeDaemon())
        cb = dp.callback_query.handlers[0].callback
        call = _C(data="v1:menu:show")
        await cb(call, _State())
        text = call.message.edits[0][0][0]
        self.assertIn("МЕНЮ", text)
        payloads = [b.callback_data for row in call.message.edits[0][1]["reply_markup"].inline_keyboard
                    for b in row]
        for p in ("v1:wallets:page:1", "v1:profiles:show", "v1:history:show",
                  "v1:builder:show", "v1:net:show", "v1:diag:show",
                  "v1:scale:show", "v1:ref:show", "v1:danger:show", "v1:home:live"):
            self.assertIn(p, payloads)

    async def test_profiles_text_command(self):
        dp = build_dispatcher(_cfg(), HomeDaemon())
        msg = _M(uid=123, text="/profiles")
        await dp.message.handlers[1].callback(msg, _State())
        self.assertIn("ПРОФИЛИ", msg.answers[0][0][0])

    async def test_diag_text_command(self):
        dp = build_dispatcher(_cfg(), HomeDaemon())
        msg = _M(uid=123, text="/diag")
        await dp.message.handlers[1].callback(msg, _State())
        self.assertTrue(any("bot:" in a[0][0] for a in msg.answers))

    async def test_menu_text_command(self):
        dp = build_dispatcher(_cfg(), HomeDaemon())
        msg = _M(uid=123, text="/menu")
        await dp.message.handlers[1].callback(msg, _State())
        self.assertIn("МЕНЮ", msg.answers[0][0][0])
        kb = msg.answers[0][1]["reply_markup"]
        payloads = [b.callback_data for row in kb.inline_keyboard for b in row]
        self.assertIn("v1:danger:show", payloads)
        self.assertIn("v1:profiles:show", payloads)

    async def test_pause_resume_stop_commands(self):
        daemon = HomeDaemon()
        dp = build_dispatcher(_cfg(), daemon)
        for cmd, expect in (("/pause", "пауз"), ("/resume", "продолж"), ("/stop", "стоп")):
            msg = _M(uid=123, text=cmd)
            await dp.message.handlers[1].callback(msg, _State())
            self.assertIn(expect, msg.answers[-1][0][0].lower())

    async def test_faucet_rpc_scheduler_alerts_commands(self):
        dp = build_dispatcher(_cfg(), HomeDaemon())
        for cmd, expect in (
            ("/faucet", "Кран"),
            ("/rpc", "RPC"),
            ("/scheduler", "PLAN"),
            ("/schedule", "PLAN"),
            ("/alerts", "Алерты"),
            ("/alerts 0.01", "Алерты"),
            ("/alerts off", "Алерты"),
            ("/cancel", "ОТМЕНА"),
        ):
            msg = _M(uid=123, text=cmd)
            await dp.message.handlers[1].callback(msg, _State())
            self.assertTrue(msg.answers, cmd)
            # Ожидаемый кусок есть в одном из ответов (не обязательно в
            # последнем — последним приходит меню). Сравнение без учёта
            # регистра: бот пишет заголовки капсом ("КРАН", "СТОП").
            texts = [a[0][0] for a in msg.answers]
            self.assertTrue(
                any(expect.lower() in t.lower() for t in texts),
                f"{cmd}: {expect} нет в {texts}",
            )
            # сброс антифлуда между командами
            import portal.bot_telegram as _bt

            _bt._flood_last.clear()

    async def test_balance_flood_and_empty_profiles(self):
        dp = build_dispatcher(_cfg(), HomeDaemon())
        msg = _M(uid=123, text="/balance")
        await dp.message.handlers[1].callback(msg, _State())
        self.assertTrue(msg.answers)
        # второй сразу — антифлуд
        msg2 = _M(uid=123, text="/balance")
        await dp.message.handlers[1].callback(msg2, _State())
        self.assertIn("жди", msg2.answers[0][0][0])
        import portal.bot_telegram as _bt

        _bt._flood_last.clear()
        # пустой флот — объяснение вместо нулей
        daemon = HomeDaemon()
        daemon._wallets = []
        dp2 = build_dispatcher(_cfg(), daemon)
        msg3 = _M(uid=123, text="/profiles")
        await dp2.message.handlers[1].callback(msg3, _State())
        self.assertIn("пуст", msg3.answers[0][0][0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
