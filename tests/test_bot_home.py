"""Приёмка v1 ТЗ этап 2 (п.2-6): Поле, навигация, мастер, режимы, экспорт, Live.

Через dispatcher: старые legacy-ветки не задеты (см. test_bot_telegram.py).
"""

import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from portal import bot_render as R

try:
    import portal.bot_settings as bs
    import portal.bot_telegram as bt
    from portal.bot_telegram import build_dispatcher
    from portal.config import PortalConfig

    HAS_BOT = True
except Exception:  # noqa: BLE001
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
        self.assertIn("HARVEST // ПОЛЕ", text)
        self.assertIn("[STOP]", text)
        self.assertIn("vibe/vibe · 46630", text)
        kb = call.message.edits[0][1]["reply_markup"]
        self.assertEqual(len(kb.inline_keyboard), 6)  # 5 рядов + обновить/Live
        payloads = [b.callback_data for row in kb.inline_keyboard for b in row]
        for p in payloads:
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
        self.assertIn("HARVEST // ПОЛЕ", call.message.edits[0][0][0])

    async def test_unknown_v1_redraws_home(self):
        dp = build_dispatcher(_cfg(), HomeDaemon())
        call = _C(data="v1:nope:nope")
        await dp.callback_query.handlers[0].callback(call, _State())
        self.assertIn("HARVEST // ПОЛЕ", call.message.edits[0][0][0])

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
        self.assertIn("HARVEST // ПОЛЕ", call2.message.edits[0][0][0])

    async def test_start_redraws(self):
        daemon = HomeDaemon()
        dp = build_dispatcher(_cfg(), daemon)
        call = _C(data="v1:run:start")
        await dp.callback_query.handlers[0].callback(call, _State())
        self.assertTrue(daemon.running)
        self.assertIn("[RUN]", call.message.edits[0][0][0])

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
        self.assertIn("HARVEST // ПОЛЕ", msg.answers[0][0][0])

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
