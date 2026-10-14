"""Тесты Telegram-бота: HTML-экранирование динамических значений статистики."""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from portal.bot_telegram import _fmt_networks, _fmt_stats, _networks_kb, build_dispatcher
    from portal.config import PortalConfig

    HAS_BOT = True
except Exception:  # noqa: BLE001
    HAS_BOT = False


@unittest.skipUnless(HAS_BOT, "aiogram не установлен")
class TestFmtStatsEscape(unittest.TestCase):
    def test_plain_values_render(self):
        s = _fmt_stats({"running": True, "pool": {"dyn_workers": 8, "health": 1.0, "cycles": 3}, "db": {"cycles": 2}})
        self.assertIn("▶ РАБОТАЕТ", s)
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
        self.assertIn("▶️", s)
        self.assertIn("vibe/vibe", s)
        self.assertIn("▫️", s)
        self.assertIn("Robinhood Chain", s)

    def test_current_not_running_mark(self):
        s = _fmt_networks([{"slug": "x", "name": "X", "tagline": "", "current": True, "running": False}])
        self.assertIn("✅", s)
        self.assertNotIn("▶️", s)

    def test_markup_escaped(self):
        s = _fmt_networks([{"slug": "x", "name": "<b>evil</b>", "tagline": "<i>t</i>"}])
        self.assertIn("&lt;b&gt;evil&lt;/b&gt;", s)
        self.assertIn("&lt;i&gt;t&lt;/i&gt;", s)
        self.assertNotIn("<b>evil</b>", s)

    def test_empty(self):
        s = _fmt_networks([])
        self.assertIn("Тестнет-сети", s)

    def test_kb_payloads(self):
        kb = _networks_kb(_NETWORKS)
        starts = [b.callback_data for row in kb.inline_keyboard[:-1] for b in row]
        self.assertEqual(starts, ["network:start:vibevibe", "network:start:robinhood"])
        last = [b.callback_data for b in kb.inline_keyboard[-1]]
        self.assertEqual(last, ["network:stop", "network:list"])

    def test_kb_empty_networks(self):
        kb = _networks_kb([])
        self.assertEqual(len(kb.inline_keyboard), 1)
        self.assertEqual(
            [b.callback_data for b in kb.inline_keyboard[0]], ["network:stop", "network:list"]
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
class TestWalletsCreate(unittest.IsolatedAsyncioTestCase):
    def test_empty_kb(self):
        from portal.bot_telegram import _wallets_empty_kb

        kb = _wallets_empty_kb()
        self.assertEqual(
            [b.callback_data for b in kb.inline_keyboard[0]], ["wallets:create"]
        )

    async def test_wallets_empty_renders_create(self):
        dp = build_dispatcher(_cfg(), _NetDaemon())
        msg = _M(uid=123, text="/wallets")
        await dp.message.handlers[1].callback(msg)
        self.assertEqual(len(msg.answers), 1)
        self.assertIn("Создать", msg.answers[0][0][0])
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
