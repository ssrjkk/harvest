"""Тесты Telegram-бота: HTML-экранирование динамических значений статистики."""

import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from portal.bot_telegram import CfgWizard, CmdWizard, _fmt_networks, _fmt_stats, _networks_kb, build_dispatcher
    from portal.config import PortalConfig

    HAS_BOT = True
except Exception:  # noqa: BLE001
    HAS_BOT = False


_GOOD_YAML = (
    "network:\n  rpc_url: https://rpc.testnet.chain.robinhood.com\n  chain_id: 46630\n"
    "wallets:\n  count: 5\nfaucet:\n  enabled: true\n  min_balance: 0.005\n"
    "  target_balance: 0.05\nactions:\n  - type: transfer\n    target: random_wallet\n"
    "farming:\n  actions_per_cycle: [3, 8]\nthreading:\n  max_workers: 20\n"
    "database:\n  path: farming_state.db\nadvanced:\n  gas_limit: 100000\n"
)


@unittest.skipUnless(HAS_BOT, "aiogram не установлен")
class TestFmtStatsEscape(unittest.TestCase):
    def test_plain_values_render(self):
        s = _fmt_stats({"running": True, "pool": {"dyn_workers": 8, "health": 1.0, "cycles": 3}, "db": {"cycles": 2}})
        self.assertIn("[RUN]", s)
        self.assertIn("8", s)

    def test_markup_in_values_escaped(self):
        # Раунд 14: значения pool/db теперь экранируются — Telegram-HTML
        # разметка из данных не исполняется.
        s = _fmt_stats(
            {
                "running": False,
                "pool": {"dyn_workers": "<script>alert(1)</script>", "health": 1.0},
                "db": {"cycles": "<b>x</b>"},
            }
        )
        self.assertIn("&lt;script&gt;", s)
        self.assertIn("&lt;b&gt;", s)
        self.assertNotIn("<script>alert(1)</script>", s)
        self.assertNotIn("<b>x</b>", s)


_NETWORKS = [
    {"slug": "vibevibe", "name": "vibe/vibe", "tagline": "Launchpad", "current": True, "running": True},
    {"slug": "robinhood", "name": "Robinhood Chain", "tagline": "Base L2", "current": False, "running": False},
]


@unittest.skipUnless(HAS_BOT, "aiogram не установлен")
class TestFmtNetworks(unittest.TestCase):
    def test_marks(self):
        s = _fmt_networks(_NETWORKS)
        self.assertIn("[RUN]", s)
        self.assertIn("vibe/vibe", s)
        self.assertIn("[···]", s)
        self.assertIn("Robinhood Chain", s)

    def test_current_not_running_mark(self):
        s = _fmt_networks([{"slug": "x", "name": "X", "tagline": "", "current": True, "running": False}])
        self.assertIn("[CUR]", s)
        self.assertNotIn("[RUN]", s)

    def test_markup_escaped(self):
        s = _fmt_networks([{"slug": "x", "name": "<b>evil</b>", "tagline": "<i>t</i>"}])
        self.assertIn("&lt;b&gt;evil&lt;/b&gt;", s)
        self.assertIn("&lt;i&gt;t&lt;/i&gt;", s)
        self.assertNotIn("<b>evil</b>", s)

    def test_empty(self):
        s = _fmt_networks([])
        self.assertIn("СЕТИ", s)

    def test_kb_payloads(self):
        kb = _networks_kb(_NETWORKS)
        starts = [b.callback_data for row in kb.inline_keyboard[:-1] for b in row]
        self.assertEqual(starts, ["network:start:vibevibe", "network:start:robinhood"])
        last = [b.callback_data for b in kb.inline_keyboard[-1]]
        self.assertEqual(last, ["network:stop", "network:list", "menu:home"])

    def test_kb_empty_networks(self):
        kb = _networks_kb([])
        self.assertEqual(len(kb.inline_keyboard), 1)
        self.assertEqual(
            [b.callback_data for b in kb.inline_keyboard[0]],
            ["network:stop", "network:list", "menu:home"],
        )


class _U:
    def __init__(self, uid):
        self.id = uid


class _M:
    def __init__(self, uid=None, text=""):
        self.from_user = _U(uid) if uid is not None else None
        self.text = text
        self.bot = mock.Mock()
        self.answers = []

    async def answer(self, *args, **kwargs):
        self.answers.append((args, kwargs))


class _CMessage:
    def __init__(self):
        self.bot = mock.Mock()
        self.answers = []
        self.edits = []

    async def answer(self, *args, **kwargs):
        self.answers.append((args, kwargs))

    async def edit_text(self, *args, **kwargs):
        self.edits.append((args, kwargs))


class _C:
    def __init__(self, uid=None, data="", message=None):
        self.from_user = _U(uid) if uid is not None else None
        self.data = data
        self.message = message or _CMessage()
        self.answered = []

    async def answer(self, *args, **kwargs):
        self.answered.append((args, kwargs))


class _NetDaemon:
    config = {"wallets": {"count": 3}}

    def __init__(self):
        self.running = False
        self.current = "vibevibe"
        self.started = 0
        self.stopped = 0
        self._wallets = []

    def networks_info(self):
        return [
            {"slug": "vibevibe", "name": "vibe/vibe", "tagline": "L", "current": True, "running": self.running},
            {"slug": "robinhood", "name": "RH", "tagline": "", "current": False, "running": False},
        ]

    async def all_wallets(self):
        return list(self._wallets)

    async def create_wallets(self, count):
        made = [{"address": f"0x{i:040x}"} for i in range(count)]
        self._wallets.extend(made)
        return made

    async def export_wallets(self, fmt: str = "csv"):
        if fmt == "json":
            return '[{"address": "0x1", "private_key": "abc"}]', "wallets.json"
        return "address,private_key\n0x1,abc\n", "wallets.csv"

    async def statistics(self):
        return {"running": self.running, "pool": {}, "db": {}, "wallet_count": len(self._wallets)}

    async def cycle_history(self):
        return []

    async def switch_to(self, slug):
        if slug == "vibevibe":
            self.current = slug
            return True
        return False

    async def start(self):
        self.started += 1
        self.running = True
        return True

    async def stop(self):
        self.stopped += 1
        self.running = False
        return True


def _cfg():
    cfg = PortalConfig()
    cfg.telegram_allow_ids = [123]
    return cfg


@unittest.skipUnless(HAS_BOT, "aiogram не установлен")
class TestNetworkCommand(unittest.IsolatedAsyncioTestCase):
    async def test_network_denied(self):
        dp = build_dispatcher(_cfg(), _NetDaemon())
        msg = _M(uid=999, text="/network")
        await dp.message.handlers[1].callback(msg)
        self.assertEqual(msg.answers, [])

    async def test_network_lists(self):
        dp = build_dispatcher(_cfg(), _NetDaemon())
        msg = _M(uid=123, text="/network")
        await dp.message.handlers[1].callback(msg)
        self.assertEqual(len(msg.answers), 1)
        text = msg.answers[0][0][0]
        self.assertIn("vibe/vibe", text)
        kb = msg.answers[0][1]["reply_markup"]
        payloads = [b.callback_data for row in kb.inline_keyboard for b in row]
        self.assertIn("network:start:robinhood", payloads)


@unittest.skipUnless(HAS_BOT, "aiogram не установлен")
class TestNetworkCallbacks(unittest.IsolatedAsyncioTestCase):
    async def test_list(self):
        dp = build_dispatcher(_cfg(), _NetDaemon())
        call = _C(uid=123, data="network:list")
        await dp.callback_query.handlers[0].callback(call)
        self.assertEqual(len(call.message.edits), 1)
        self.assertIn("vibe/vibe", call.message.edits[0][0][0])

    async def test_start_ok(self):
        daemon = _NetDaemon()
        dp = build_dispatcher(_cfg(), daemon)
        call = _C(uid=123, data="network:start:vibevibe")
        await dp.callback_query.handlers[0].callback(call)
        self.assertEqual(daemon.started, 1)
        self.assertTrue(any("запущен" in a[0][0] for a in call.message.answers))
        self.assertEqual(len(call.message.edits), 1)

    async def test_start_unknown(self):
        daemon = _NetDaemon()
        dp = build_dispatcher(_cfg(), daemon)
        call = _C(uid=123, data="network:start:nope")
        await dp.callback_query.handlers[0].callback(call)
        self.assertEqual(daemon.started, 0)
        self.assertTrue(any("Неизвестная сеть" in a[0][0] for a in call.message.answers))
        self.assertEqual(call.message.edits, [])

    async def test_stop(self):
        daemon = _NetDaemon()
        dp = build_dispatcher(_cfg(), daemon)
        call = _C(uid=123, data="network:stop")
        await dp.callback_query.handlers[0].callback(call)
        self.assertEqual(daemon.stopped, 1)
        self.assertTrue(any("остановлена" in a[0][0] for a in call.message.answers))

    async def test_denied(self):
        dp = build_dispatcher(_cfg(), _NetDaemon())
        call = _C(uid=999, data="network:list")
        await dp.callback_query.handlers[0].callback(call)
        self.assertEqual(call.message.edits, [])


@unittest.skipUnless(HAS_BOT, "aiogram не установлен")
class TestOpsCommands(unittest.IsolatedAsyncioTestCase):
    async def test_cmd_builds(self):
        dp = build_dispatcher(_cfg(), _NetDaemon())
        msg = _M(uid=123, text="/cmd vibe 10 2")
        await dp.message.handlers[1].callback(msg)
        self.assertEqual(len(msg.answers), 1)
        self.assertIn("config_vibevibe.yaml", msg.answers[0][0][0])
        self.assertIn("--wallets 10", msg.answers[0][0][0])

    async def test_cmd_usage_and_bad_slug(self):
        dp = build_dispatcher(_cfg(), _NetDaemon())
        msg = _M(uid=123, text="/cmd")
        await dp.message.handlers[1].callback(msg)
        self.assertIn("/cmd", msg.answers[0][0][0])
        msg2 = _M(uid=123, text="/cmd nope 1 1")
        await dp.message.handlers[1].callback(msg2)
        self.assertIn("[err]", msg2.answers[0][0][0])

    async def test_cfg_sends_document(self):
        dp = build_dispatcher(_cfg(), _NetDaemon())
        msg = _M(uid=123, text="/cfg arc 7")
        msg.bot.send_document = mock.AsyncMock()
        await dp.message.handlers[1].callback(msg)
        msg.bot.send_document.assert_awaited_once()
        kw = msg.bot.send_document.await_args.kwargs
        self.assertEqual(kw["document"].filename, "config_arc.yaml")
        body = kw["document"].data.decode("utf-8")
        self.assertIn("count: 7", body)
        self.assertIn("5042002", body)

    async def test_cfg_usage_and_bad_args(self):
        dp = build_dispatcher(_cfg(), _NetDaemon())
        msg = _M(uid=123, text="/cfg")
        await dp.message.handlers[1].callback(msg)
        self.assertIn("/cfg", msg.answers[0][0][0])
        msg2 = _M(uid=123, text="/cfg vibe xx")
        await dp.message.handlers[1].callback(msg2)
        self.assertIn("[err]", msg2.answers[0][0][0])
        msg3 = _M(uid=123, text="/cfg nope 5")
        await dp.message.handlers[1].callback(msg3)
        self.assertIn("[err]", msg3.answers[0][0][0])

    async def test_check_good_and_bad(self):
        dp = build_dispatcher(_cfg(), _NetDaemon())
        msg = _M(uid=123, text="/check\n" + _GOOD_YAML)
        await dp.message.handlers[1].callback(msg)
        self.assertIn("[ok]", msg.answers[0][0][0])
        msg2 = _M(uid=123, text="/check\nnetwork:\n  chain_id: 0\n")
        await dp.message.handlers[1].callback(msg2)
        self.assertIn("[err]", msg2.answers[0][0][0])

    async def test_check_needs_body(self):
        dp = build_dispatcher(_cfg(), _NetDaemon())
        msg = _M(uid=123, text="/check")
        await dp.message.handlers[1].callback(msg)
        self.assertIn("ПРОВЕРКА", msg.answers[0][0][0])

    async def test_ref_and_callbacks(self):
        dp = build_dispatcher(_cfg(), _NetDaemon())
        msg = _M(uid=123, text="/ref")
        await dp.message.handlers[1].callback(msg)
        self.assertIn("СПРАВКА", msg.answers[0][0][0])
        for data, needle in [
            ("cmd:run", "/cmd"), ("cfg:run", "/cfg"), ("ref", "СПРАВКА"),
        ]:
            call = _C(uid=123, data=data)
            await dp.callback_query.handlers[0].callback(call)
            got = (call.message.answers + call.message.edits)
            self.assertTrue(got, data)
            self.assertIn(needle, got[0][0][0])


class _FakeState:
    """Подмена FSMContext для тестов мастера (без storage)."""

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


@unittest.skipUnless(HAS_BOT, "aiogram не установлен")
class TestOpsWizardFlow(unittest.IsolatedAsyncioTestCase):
    def _call(self, uid=123, data="", with_user=True):
        call = _C(uid=uid, data=data)
        if with_user:
            call.message.from_user = _U(uid)
        call.message.bot.send_document = mock.AsyncMock()
        return call

    async def test_cmd_wizard_full(self):
        dp = build_dispatcher(_cfg(), _NetDaemon())
        cb = dp.callback_query.handlers[0].callback
        st = _FakeState()
        call = self._call(data="cmd:run")
        await cb(call, st)
        self.assertIn("Шаг 1/4", call.message.answers[0][0][0])
        call = self._call(data="wiz:cmd:net:arc")
        await cb(call, st)
        self.assertEqual(await st.get_state(), CmdWizard.wallets.state)
        msg = _M(uid=123, text="50")
        # свободный ввод идёт через on_help (handlers[1]) со state
        await dp.message.handlers[1].callback(msg, st)
        self.assertEqual(await st.get_state(), CmdWizard.cycles.state)
        msg = _M(uid=123, text="5")
        await dp.message.handlers[1].callback(msg, st)
        self.assertEqual(await st.get_state(), CmdWizard.faucet.state)
        call = self._call(data="wiz:cmd:f:1")
        await cb(call, st)
        texts = [a[0][0] for a in call.message.answers]
        self.assertTrue(any("config_arc.yaml" in t for t in texts))
        self.assertTrue(call.message.bot.send_document.await_count)
        kw = call.message.bot.send_document.await_args.kwargs
        self.assertEqual(kw["document"].filename, "config_arc.yaml")
        self.assertIsNone(await st.get_state())

    async def test_cmd_wizard_bad_number_and_cancel(self):
        dp = build_dispatcher(_cfg(), _NetDaemon())
        cb = dp.callback_query.handlers[0].callback
        st = _FakeState()
        await cb(self._call(data="cmd:run"), st)
        await cb(self._call(data="wiz:cmd:net:vibe"), st)
        msg = _M(uid=123, text="много")
        await dp.message.handlers[1].callback(msg, st)
        self.assertIn("[err]", msg.answers[0][0][0])
        call = self._call(data="wiz:cancel")
        await cb(call, st)
        self.assertIsNone(await st.get_state())
        self.assertIn("ОТМЕНА", call.message.answers[0][0][0])

    async def test_cfg_wizard_full(self):
        dp = build_dispatcher(_cfg(), _NetDaemon())
        cb = dp.callback_query.handlers[0].callback
        st = _FakeState()
        await cb(self._call(data="cfg:run"), st)
        await cb(self._call(data="wiz:cfg:net:flop"), st)
        self.assertEqual(await st.get_state(), CfgWizard.wallets.state)
        msg = _M(uid=123, text="7")
        msg.bot.send_document = mock.AsyncMock()
        await dp.message.handlers[1].callback(msg, st)
        self.assertIsNone(await st.get_state())
        self.assertTrue(msg.bot.send_document.await_count)
        kw = msg.bot.send_document.await_args.kwargs
        self.assertEqual(kw["document"].filename, "config_flop.yaml")
        self.assertIn("count: 7", kw["document"].data.decode("utf-8"))

    async def test_document_yaml_checked(self):
        dp = build_dispatcher(_cfg(), _NetDaemon())
        msg = _M(uid=123, text="")
        msg.document = mock.Mock()
        msg.document.file_name = "farm.yaml"
        msg.document.file_size = 100

        async def _dl(doc, buf):
            buf.write(b"network:\n  chain_id: 0\n")

        msg.bot.download = mock.AsyncMock(side_effect=_dl)
        await dp.message.handlers[2].callback(msg)
        self.assertIn("[err]", msg.answers[0][0][0])

    async def test_document_wrong_ext(self):
        dp = build_dispatcher(_cfg(), _NetDaemon())
        msg = _M(uid=123, text="")
        msg.document = mock.Mock()
        msg.document.file_name = "photo.png"
        msg.document.file_size = 100
        await dp.message.handlers[2].callback(msg)
        self.assertIn("[err]", msg.answers[0][0][0])

    async def test_wizard_command_escapes(self):
        from portal.bot_telegram import CmdWizard

        dp = build_dispatcher(_cfg(), _NetDaemon())
        st = _FakeState()
        await dp.callback_query.handlers[0].callback(self._call(data="cmd:run"), st)
        await st.set_state(CmdWizard.wallets)
        msg = _M(uid=123, text="/ref")
        await dp.message.handlers[1].callback(msg, st)
        self.assertIsNone(await st.get_state())
        self.assertIn("СПРАВКА", msg.answers[0][0][0])

@unittest.skipUnless(HAS_BOT, "aiogram не установлен")
@unittest.skipUnless(HAS_BOT, "aiogram не установлен")
class TestAntiflood(unittest.IsolatedAsyncioTestCase):
    def tearDown(self):
        import portal.bot_telegram as bt

        bt._flood_last.clear()

    def test_second_call_blocked(self):
        import portal.bot_telegram as bt

        ok, _ = bt._flood_ok(777001, "export")
        self.assertTrue(ok)
        ok, wait = bt._flood_ok(777001, "export")
        self.assertFalse(ok)
        self.assertGreater(wait, 0)
        # Другая операция и другой пользователь — не затронуты.
        self.assertTrue(bt._flood_ok(777001, "wallets")[0])
        self.assertTrue(bt._flood_ok(777002, "export")[0])

    async def test_export_command_flooded(self):
        import time

        import portal.bot_telegram as bt

        dp = build_dispatcher(_cfg(), _NetDaemon())
        bt._flood_last[(123, "export")] = time.monotonic()
        msg = _M(uid=123, text="/export")
        await dp.message.handlers[1].callback(msg)
        self.assertIn("жди", msg.answers[0][0][0])

    async def test_wallets_create_callback_flooded(self):
        import time

        import portal.bot_telegram as bt

        dp = build_dispatcher(_cfg(), _NetDaemon())
        bt._flood_last[(123, "create")] = time.monotonic()
        call = _C(uid=123, data="wallets:create")
        await dp.callback_query.handlers[0].callback(call)
        self.assertTrue(any("жди" in a[0][0] for a in call.message.answers))


@unittest.skipUnless(HAS_BOT, "aiogram не установлен")
class TestExportConfirm(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import portal.bot_telegram as bt

        bt._flood_last.clear()

    def tearDown(self):
        import portal.bot_telegram as bt

        bt._flood_last.clear()

    async def test_export_button_asks_confirm(self):
        dp = build_dispatcher(_cfg(), _NetDaemon())
        for fmt in ("csv", "json"):
            call = _C(uid=123, data=f"export:{fmt}")
            await dp.callback_query.handlers[0].callback(call)
            self.assertEqual(len(call.message.edits), 1, fmt)
            text = call.message.edits[0][0][0]
            self.assertIn("подтверждение", text)
            self.assertIn("приватные ключи", text)
            self.assertIn(fmt.upper(), text)
            kb = call.message.edits[0][1]["reply_markup"]
            payloads = [b.callback_data for row in kb.inline_keyboard for b in row]
            self.assertIn(f"export:yes:{fmt}", payloads)
            self.assertIn("menu:home", payloads)

    async def test_export_yes_sends_protected_and_deletes(self):
        import portal.bot_telegram as bt

        dp = build_dispatcher(_cfg(), _NetDaemon())
        call = _C(uid=123, data="export:yes:json")
        call.message.bot.send_document = mock.AsyncMock(
            return_value=mock.Mock(message_id=777)
        )
        call.message.bot.delete_message = mock.AsyncMock()
        old, bt._EXPORT_AUTODELETE_S = bt._EXPORT_AUTODELETE_S, 0
        try:
            await dp.callback_query.handlers[0].callback(call)
            await asyncio.sleep(0.05)
        finally:
            bt._EXPORT_AUTODELETE_S = old
        kw = call.message.bot.send_document.await_args.kwargs
        self.assertTrue(kw.get("protect_content"))
        call.message.bot.delete_message.assert_awaited_once_with(
            chat_id=123, message_id=777
        )

    async def test_export_yes_bad_fmt_ignored(self):
        dp = build_dispatcher(_cfg(), _NetDaemon())
        call = _C(uid=123, data="export:yes:xml")
        await dp.callback_query.handlers[0].callback(call)
        self.assertEqual(call.message.answers, [])
        self.assertEqual(call.message.edits, [])

    async def test_menu_home(self):
        # Legacy menu:home ведёт на новое Поле (п.2 ТЗ).
        dp = build_dispatcher(_cfg(), _NetDaemon())
        call = _C(uid=123, data="menu:home")
        await dp.callback_query.handlers[0].callback(call)
        self.assertEqual(len(call.message.edits), 1)
        self.assertIn("HARVEST // ПОЛЕ", call.message.edits[0][0][0])
        kb = call.message.edits[0][1]["reply_markup"]
        payloads = [b.callback_data for row in kb.inline_keyboard for b in row]
        self.assertIn("v1:run:start", payloads)
        self.assertIn("v1:danger:show", payloads)
        self.assertIn("v1:home:refresh", payloads)
        # Запуск и Стоп никогда не рядом (п.2 ТЗ).
        self.assertFalse("v1:run:start" in payloads and "v1:run:stop" in payloads)

    async def test_home_buttons_everywhere(self):
        """Каждый экран с edit_text даёт путь назад в menu:home."""
        from portal.bot_telegram import _menu_kb  # noqa: F401  (импорт — проверка __all__)

        daemon = _NetDaemon()
        daemon._wallets = [{"address": "0x1", "private_key": "k", "mnemonic": "m", "total_actions": 1}]
        dp = build_dispatcher(_cfg(), daemon)
        cb = dp.callback_query.handlers[0].callback
        for data in ("stats", "status", "doctor", "history", "links", "ref",
                     "network:list", "wallets:page:1"):
            call = _C(uid=123, data=data)
            await cb(call)
            self.assertEqual(len(call.message.edits), 1, data)
            kb = call.message.edits[0][1].get("reply_markup")
            self.assertIsNotNone(kb, data)
            payloads = [b.callback_data for row in kb.inline_keyboard for b in row]
            self.assertIn("menu:home", payloads, data)

    def test_refresh_home_kb_shape(self):
        from portal.bot_telegram import _refresh_home_kb

        for cb_name in ("stats", "status", "doctor", "history"):
            kb = _refresh_home_kb(cb_name)
            self.assertEqual(len(kb.inline_keyboard), 2)
            self.assertEqual(kb.inline_keyboard[0][0].callback_data, cb_name)
            self.assertEqual(kb.inline_keyboard[1][0].callback_data, "menu:home")

    def test_wallets_kb_has_home(self):
        from portal.bot_telegram import _wallets_empty_kb, _wallets_kb

        for kb in (_wallets_kb(1, 1), _wallets_kb(1, 3), _wallets_kb(2, 3), _wallets_empty_kb()):
            payloads = [b.callback_data for row in kb.inline_keyboard for b in row]
            self.assertIn("menu:home", payloads)


@unittest.skipUnless(HAS_BOT, "aiogram не установлен")
class TestWalletsCreate(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Антифлуд — процессный: чистим метки, чтобы тесты одного юзера
        # не троттлили друг друга (в проде между вызовами проходят секунды).
        import portal.bot_telegram as bt

        bt._flood_last.clear()

    def test_empty_kb(self):
        from portal.bot_telegram import _wallets_empty_kb

        kb = _wallets_empty_kb()
        self.assertEqual(
            [b.callback_data for b in kb.inline_keyboard[0]], ["wallets:create"]
        )
        payloads = [b.callback_data for row in kb.inline_keyboard for b in row]
        self.assertIn("menu:home", payloads)

    async def test_wallets_empty_renders_create(self):
        dp = build_dispatcher(_cfg(), _NetDaemon())
        msg = _M(uid=123, text="/wallets")
        await dp.message.handlers[1].callback(msg)
        self.assertEqual(len(msg.answers), 1)
        self.assertIn("СОЗДАТЬ", msg.answers[0][0][0])
        kb = msg.answers[0][1]["reply_markup"]
        payloads = [b.callback_data for row in kb.inline_keyboard for b in row]
        self.assertIn("wallets:create", payloads)

    async def test_create_ok(self):
        daemon = _NetDaemon()
        dp = build_dispatcher(_cfg(), daemon)
        call = _C(uid=123, data="wallets:create")
        await dp.callback_query.handlers[0].callback(call)
        self.assertTrue(any("Создано кошельков: 3" in a[0][0] for a in call.message.answers))
        self.assertEqual(len(daemon._wallets), 3)
        self.assertEqual(len(call.message.edits), 1)

    async def test_create_no_db(self):
        class _Dead:
            config = None

            async def create_wallets(self, count):
                raise RuntimeError("БД фермы не подключена")

        dp = build_dispatcher(_cfg(), _Dead())
        call = _C(uid=123, data="wallets:create")
        await dp.callback_query.handlers[0].callback(call)
        self.assertTrue(any("не подключена" in a[0][0] for a in call.message.answers))
        self.assertEqual(call.message.edits, [])

    async def test_create_bad_config_count_falls_back(self):
        daemon = _NetDaemon()
        daemon.config = {"wallets": {"count": object()}}
        dp = build_dispatcher(_cfg(), daemon)
        call = _C(uid=123, data="wallets:create")
        await dp.callback_query.handlers[0].callback(call)
        self.assertEqual(len(daemon._wallets), 3)
        self.assertTrue(any("Создано кошельков: 3" in a[0][0] for a in call.message.answers))


class _BalDaemon:
    async def wallet_balances(self):
        return [
            {"address": "0xabc", "balance": 1.5},
            {"address": "0xdef", "balance": None},
        ]


class _BalDead:
    async def wallet_balances(self):
        raise RuntimeError("Ферма не запущена")


@unittest.skipUnless(HAS_BOT, "aiogram не установлен")
class TestBalanceCommand(unittest.IsolatedAsyncioTestCase):
    def test_fmt(self):
        from portal.bot_telegram import _fmt_balances

        s = _fmt_balances([{"address": "0xabc", "balance": 1.5}, {"address": "0x<script>", "balance": None}])
        self.assertIn("0xabc", s)
        self.assertIn("1.5000 ETH", s)
        self.assertIn("Итого: 1.5000 ETH", s)
        self.assertIn("&lt;script&gt;", s)
        self.assertNotIn("<script>", s)

    def test_fmt_empty(self):
        from portal.bot_telegram import _fmt_balances

        self.assertIn("Кошельков пока нет", _fmt_balances([]))

    async def test_command_ok(self):
        dp = build_dispatcher(_cfg(), _BalDaemon())
        msg = _M(uid=123, text="/balance")
        await dp.message.handlers[1].callback(msg)
        self.assertEqual(len(msg.answers), 1)
        self.assertIn("1.5000 ETH", msg.answers[0][0][0])

    async def test_command_not_running(self):
        dp = build_dispatcher(_cfg(), _BalDead())
        msg = _M(uid=123, text="/balance")
        await dp.message.handlers[1].callback(msg)
        self.assertTrue(any("не запущена" in a[0][0] for a in msg.answers))


@unittest.skipUnless(HAS_BOT, "aiogram не установлен")
class TestLegacyGaps(unittest.IsolatedAsyncioTestCase):
    """Добор покрытия legacy-веток (гейт CI 95%)."""

    def setUp(self):
        import portal.bot_telegram as bt

        bt._flood_last.clear()
        self._conflict = (bt._conflict.consecutive, bt._conflict.notified,
                          bt._conflict.total)
        bt._conflict.resolve()
        bt._conflict.total = 0

    def tearDown(self):
        import portal.bot_telegram as bt

        bt._flood_last.clear()
        (c, n, t) = self._conflict
        bt._conflict.consecutive, bt._conflict.notified, bt._conflict.total = c, n, t

    def _state(self, **data):
        st = _FakeState()
        st._data.update(data)
        return st

    async def test_wiz_finish_errors(self):
        import portal.bot_telegram as bt

        dp = build_dispatcher(_cfg(), _NetDaemon())
        cb = dp.callback_query.handlers[0].callback
        call = _C(uid=123, data="wiz:cmd:f:1")
        await cb(call, self._state(net="nope", wallets=1, cycles=1))
        self.assertIn("[err]", call.message.answers[0][0][0])
        call2 = _C(uid=123, data="wiz:cfg:w:5")
        msg = _M(uid=123, text="")
        msg.bot.send_document = mock.AsyncMock()
        # finish_cfg через кнопку невозможен без net; err через build напрямую:
        with self.assertRaises(ValueError):
            import portal.bot_ops as _ops

            _ops.build_config_yaml("nope", 5)

    async def test_wiz_handle_direct(self):
        import portal.bot_telegram as bt

        msg = _M(uid=123, text="/cancel")
        self.assertTrue(await bt._wiz_handle_text(msg, _FakeState()))
        self.assertIn("ОТМЕНА", msg.answers[0][0][0])

        class _Boom:
            async def get_state(self):
                raise RuntimeError("x")

            async def clear(self):
                pass

        self.assertFalse(await bt._wiz_handle_text(_M(uid=123, text="5"), _Boom()))

        st = _FakeState()
        await st.set_state(__import__("portal.bot_telegram", fromlist=["CfgWizard"]).CfgWizard.wallets)
        msg2 = _M(uid=123, text="много")
        self.assertTrue(await bt._wiz_handle_text(msg2, st))
        self.assertIn("[err]", msg2.answers[0][0][0])
        self.assertFalse(await bt._wiz_handle_text(_M(uid=123, text="x"), _FakeState()))

    async def test_safe_edit_and_autodelete(self):
        import portal.bot_telegram as bt

        msg = _M(uid=123, text="x")

        async def _boom(*a, **k):
            raise RuntimeError("message is not modified by bot")

        async def _other(*a, **k):
            raise RuntimeError("gone")

        msg.edit_text = _boom  # type: ignore[attr-defined]
        self.assertFalse(await bt._safe_edit(msg, "t"))
        msg.edit_text = _other  # type: ignore[attr-defined]
        with self.assertRaises(RuntimeError):
            await bt._safe_edit(msg, "t")

        bot = mock.Mock()
        bot.delete_message = mock.AsyncMock(side_effect=RuntimeError("gone"))
        await bt._auto_delete_file(bot, 1, 2, 0)
        bot.delete_message.assert_awaited_once()

    def test_conflict_filter(self):
        import logging

        import portal.bot_telegram as bt

        rec = logging.LogRecord("aiogram.dispatcher", logging.ERROR, __file__, 1,
                                "Failed to fetch updates - TelegramConflictError: "
                                "Telegram server says - Conflict: terminated by "
                                "other getUpdates request", (), None)
        f = bt._ConflictLogFilter()
        self.assertTrue(f.filter(rec))
        rec2 = logging.LogRecord("n", logging.ERROR, __file__, 1, "plain", (), None)
        self.assertTrue(f.filter(rec2))

        class _Bad:
            def getMessage(self):
                raise RuntimeError("x")

        rec3 = logging.LogRecord("n", logging.ERROR, __file__, 1, "x", (), None)
        rec3.getMessage = _Bad().getMessage  # type: ignore[method-assign]
        self.assertTrue(f.filter(rec3))

    async def test_setup_outer_fail(self):
        import portal.bot_telegram as bt

        bot = mock.Mock()
        with mock.patch("aiogram.types.BotCommand", side_effect=RuntimeError("x")):
            await bt.setup_bot_commands(bot)  # не падает

    async def test_mode_lang_fallback(self):
        import portal.bot_settings as _bs
        import portal.bot_telegram as bt

        with mock.patch.object(_bs, "get_settings", side_effect=RuntimeError("db")):
            self.assertEqual(await bt._mode_lang(mock.Mock(), 123), ("novice", "ru"))

    async def test_home_state_fallbacks(self):
        import portal.bot_telegram as bt

        class _Dead:
            async def statistics(self):
                raise RuntimeError("down")

        st = await bt._home_state(_Dead())
        self.assertFalse(st["running"])
        self.assertEqual(st["network"], "—")

        class _BadNets:
            async def statistics(self):
                return {"running": True, "pool": {}, "db": {}, "wallet_count": "xx"}

            def networks_info(self):
                raise RuntimeError("x")

            async def wallet_cells(self):
                raise RuntimeError("x")

        st2 = await bt._home_state(_BadNets())
        self.assertTrue(st2["running"])

    async def test_live_loop_error(self):
        import portal.bot_telegram as bt

        msg = _M(uid=123, text="x")

        async def _boom(*a, **k):
            raise RuntimeError("gone")

        msg.edit_text = _boom  # type: ignore[attr-defined]
        await bt._live_loop(123, msg, _NetDaemon(), every=0.01, ttl=5)
        self.assertNotIn(123, bt._live)
        for t in list(bt._live.values()):
            t.cancel()
        bt._live.clear()

    async def test_stale_answer_fails_quietly(self):
        import portal.bot_telegram as bt

        call = _C(uid=123, data="v0:x")
        call.answer = mock.AsyncMock(side_effect=RuntimeError("gone"))
        await bt._stale_home(call, _NetDaemon())
        self.assertTrue(call.message.edits)

    async def test_on_help_document_and_cancel_fail(self):
        dp = build_dispatcher(_cfg(), _NetDaemon())
        msg = _M(uid=123, text="x")
        msg.document = mock.Mock()
        await dp.message.handlers[1].callback(msg)
        self.assertEqual(msg.answers, [])

        st = _FakeState()

        async def _boom():
            raise RuntimeError("x")

        st.clear = _boom  # type: ignore[method-assign]
        msg2 = _M(uid=123, text="/cancel")
        with self.assertLogs("portal.bot_telegram", level="WARNING"):
            await dp.message.handlers[1].callback(msg2, st)

    async def test_on_start_clear_fails(self):
        dp = build_dispatcher(_cfg(), _NetDaemon())
        st = _FakeState()

        async def _boom():
            raise RuntimeError("x")

        st.clear = _boom  # type: ignore[method-assign]
        msg = _M(uid=123, text="/start")
        await dp.message.handlers[0].callback(msg, st)
        self.assertTrue(msg.answers)

    async def test_legacy_callbacks_misc(self):
        dp = build_dispatcher(_cfg(), _NetDaemon())
        cb = dp.callback_query.handlers[0].callback
        for data in ("cmd:run", "cfg:run", "wiz:cancel", "cmd:usage",
                     "cfg:usage", "farm:start", "farm:pause", "farm:resume",
                     "network:stop", "zzz-unknown"):
            call = _C(uid=123, data=data)
            await cb(call)
            self.assertTrue(call.message.answers or call.message.edits, data)
        call = _C(uid=123, data="wiz:cmd:net:nope")
        await cb(call, _FakeState())
        self.assertIn("[err]", call.message.answers[0][0][0])
        for data in ("wiz:cmd:w:xx", "wiz:cmd:w:99999", "wiz:cmd:c:xx"):
            call = _C(uid=123, data=data)
            st = _FakeState()
            await cb(call, st)

    async def test_legacy_document_oversize_and_dl_fail(self):
        dp = build_dispatcher(_cfg(), _NetDaemon())
        msg = _M(uid=123, text="")
        msg.document = mock.Mock()
        msg.document.file_name = "farm.yaml"
        msg.document.file_size = 999999
        await dp.message.handlers[2].callback(msg)
        self.assertIn("16 КБ", msg.answers[0][0][0])
        msg2 = _M(uid=123, text="")
        msg2.document = mock.Mock()
        msg2.document.file_name = "farm.yaml"
        msg2.document.file_size = 10
        msg2.bot.download = mock.AsyncMock(side_effect=RuntimeError("io"))
        await dp.message.handlers[2].callback(msg2)
        self.assertIn("[err]", msg2.answers[0][0][0])

    async def test_callback_exception_generic(self):
        class _Dead:
            async def statistics(self):
                raise RuntimeError("db down")

        dp = build_dispatcher(_cfg(), _Dead())
        call = _C(uid=123, data="stats")
        await dp.callback_query.handlers[0].callback(call)
        self.assertTrue(any("Попробуйте позже" in a[0][0]
                            for a in call.message.answers))

    async def test_run_bot_conflict(self):
        import asyncio as _aio

        import portal.bot_telegram as bt
        from aiogram.exceptions import TelegramConflictError

        cfg = _cfg()
        cfg.telegram_token = "123:ABC"
        daemon = _NetDaemon()
        dp = build_dispatcher(cfg, daemon)
        with mock.patch.object(bt, "Dispatcher", return_value=dp), \
                mock.patch.object(bt, "Bot") as m_bot, \
                mock.patch.object(bt, "notify_owner",
                                  new=mock.AsyncMock()) as _no, \
                mock.patch.object(_aio, "sleep", new=mock.AsyncMock()) as _sl:
            async def _boom(_bot):
                raise TelegramConflictError(method="getUpdates",
                                            message="Conflict")

            dp.start_polling = mock.AsyncMock(side_effect=_boom)  # type: ignore[method-assign]
            m_bot.return_value.set_my_commands = mock.AsyncMock()
            m_bot.return_value.set_my_description = mock.AsyncMock()
            m_bot.return_value.set_my_short_description = mock.AsyncMock()
            bt._conflict.resolve()
            bt._conflict.consecutive = 4
            with self.assertRaises(TelegramConflictError):
                await bt.run_bot(cfg, daemon)
            _sl.assert_awaited()
            _no.assert_awaited_once()
            bt._conflict.resolve()

    async def test_wiz_valid_buttons_and_states(self):
        dp = build_dispatcher(_cfg(), _NetDaemon())
        cb = dp.callback_query.handlers[0].callback
        st = _FakeState()
        call = _C(uid=123, data="wiz:cmd:w:50")
        call.message.bot.send_document = mock.AsyncMock()
        await cb(call, st)
        self.assertIn("3/4", call.message.answers[0][0][0])
        st2 = _FakeState()
        await st2.update_data(net="vibevibe")
        call2 = _C(uid=123, data="wiz:cfg:w:50")
        call2.message.from_user = _U(123)
        call2.message.bot.send_document = mock.AsyncMock()
        await cb(call2, st2)
        call2.message.bot.send_document.assert_awaited_once()
        st3 = _FakeState()
        call3 = _C(uid=123, data="wiz:cmd:c:5")
        await cb(call3, st3)
        self.assertIn("4/4", call3.message.answers[0][0][0])
        for data in ("wiz:cmd:w:1", "wiz:cmd:c:1", "wiz:cmd:f:1"):
            calln = _C(uid=123, data=data)
            await cb(calln, None)

    async def test_wiz_finish_cfg_error_and_cycles_bad(self):
        import portal.bot_telegram as bt
        from portal.bot_telegram import CmdWizard

        dp = build_dispatcher(_cfg(), _NetDaemon())
        st = _FakeState()
        await st.update_data(net="nope")
        call = _C(uid=123, data="wiz:cfg:w:5")
        await dp.callback_query.handlers[0].callback(call, st)
        self.assertIn("[err]", call.message.answers[0][0][0])

        st2 = _FakeState()
        await st2.set_state(CmdWizard.cycles)
        msg = _M(uid=123, text="много")
        self.assertTrue(await bt._wiz_handle_text(msg, st2))
        self.assertIn("[err]", msg.answers[0][0][0])

    async def test_redact_notify_and_document_deny(self):
        import logging
        import sys

        import portal.bot_telegram as bt

        rec = logging.LogRecord("x", logging.WARNING, __file__, 1, "m", ("a",), None)
        rec.args = mock.Mock()
        rec.args.__iter__ = mock.Mock(side_effect=RuntimeError("x"))
        self.assertTrue(bt._RedactingFilter().filter(rec))
        rec2 = logging.LogRecord("x", logging.WARNING, __file__, 1, "m", (), None)

        class _BadStr:
            def __str__(self):
                raise RuntimeError("x")

        rec2.msg = _BadStr()
        self.assertTrue(bt._RedactingFilter().filter(rec2))

        cfg = _cfg()
        cfg.telegram_token = "123:ABC"
        with mock.patch.dict(sys.modules, {"portal.bot_render": None}):
            with mock.patch.object(bt, "Bot") as m_bot:
                bot = mock.Mock()
                m_bot.return_value = bot
                bot.__aenter__ = mock.AsyncMock(return_value=bot)
                bot.__aexit__ = mock.AsyncMock(return_value=False)
                bot.send_message = mock.AsyncMock()
                await bt.notify_owner(cfg, "hi")
                bot.send_message.assert_awaited()

        dp = build_dispatcher(_cfg(), _NetDaemon())
        msg = _M(uid=999, text="")
        msg.document = mock.Mock()
        await dp.message.handlers[2].callback(msg)
        self.assertEqual(msg.answers, [])

    async def test_legacy_export_yes_flood(self):
        import time

        import portal.bot_telegram as bt

        dp = build_dispatcher(_cfg(), _NetDaemon())
        bt._flood_last[(123, "export")] = time.monotonic()
        call = _C(uid=123, data="export:yes:csv")
        await dp.callback_query.handlers[0].callback(call)
        self.assertTrue(any("жди" in a[0][0] for a in call.message.answers))

    async def test_run_bot_notify_409_failure_quiet(self):
        import asyncio as _aio

        import portal.bot_telegram as bt
        from aiogram.exceptions import TelegramConflictError

        cfg = _cfg()
        cfg.telegram_token = "123:ABC"
        dp = build_dispatcher(cfg, _NetDaemon())
        with mock.patch.object(bt, "Dispatcher",
                               return_value=dp), \
                mock.patch.object(bt, "Bot") as m_bot, \
                mock.patch.object(bt, "notify_owner",
                                  new=mock.AsyncMock(
                                      side_effect=RuntimeError("down"))), \
                mock.patch.object(_aio, "sleep", new=mock.AsyncMock()):
            async def _boom(_bot):
                raise TelegramConflictError(method="getUpdates",
                                            message="Conflict")

            dp.start_polling = mock.AsyncMock(side_effect=_boom)  # type: ignore[method-assign]
            m_bot.return_value.set_my_commands = mock.AsyncMock()
            m_bot.return_value.set_my_description = mock.AsyncMock()
            m_bot.return_value.set_my_short_description = mock.AsyncMock()
            bt._conflict.resolve()
            bt._conflict.consecutive = 4
            with self.assertRaises(TelegramConflictError):
                await bt.run_bot(cfg, daemon := _NetDaemon())
            bt._conflict.resolve()


if __name__ == "__main__":
    unittest.main(verbosity=2)
