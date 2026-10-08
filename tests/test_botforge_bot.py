"""Тесты автономного бота BotForge (botforge/bot.py): default-deny доступ,
форматирование кошельков с ключами/сид-фразами и прогресс-бар.

Модуль читает BOT_TOKEN на импорте — задаём env до импорта. duckdb/сеть
для проверяемых чистых функций не требуются.
"""

import importlib
import os
import unittest

os.environ.setdefault("BOT_TOKEN", "12345:TEST")
os.environ.pop("MOTHERDUCK_TOKEN", None)

bot = importlib.import_module("botforge.bot")


class TestAllowed(unittest.TestCase):
    def setUp(self):
        self._ids = bot.ALLOW_IDS
        self._open = bot.OPEN_ACCESS

    def tearDown(self):
        bot.ALLOW_IDS = self._ids
        bot.OPEN_ACCESS = self._open

    def test_default_deny_without_ids(self):
        bot.ALLOW_IDS = set()
        bot.OPEN_ACCESS = False
        self.assertFalse(bot.allowed(123))
        self.assertFalse(bot.allowed(None))

    def test_allow_listed_id(self):
        bot.ALLOW_IDS = {397602448}
        bot.OPEN_ACCESS = False
        self.assertTrue(bot.allowed(397602448))
        self.assertFalse(bot.allowed(999))

    def test_open_access(self):
        bot.ALLOW_IDS = set()
        bot.OPEN_ACCESS = True
        self.assertTrue(bot.allowed(1))
        self.assertFalse(bot.allowed(None))


class TestFormatting(unittest.TestCase):
    def test_progress_bar(self):
        self.assertIn("▰▰▰▰▰▱▱▱▱▱ 50% (5/10)", bot.progress_bar(5, 10))
        self.assertIn("0% (0/0)", bot.progress_bar("a", "<b>x</b>"))

    def test_fmt_status_escapes(self):
        text = bot.fmt_status(
            {"running": True, "wallet_count": 12, "health_factor": 0.9,
             "pool": {"actions": 5, "errors": 1, "processed": 4, "cycles": 2},
             "updated_at": "<script>"}
        )
        self.assertIn("HARVEST", text)
        self.assertIn("&lt;script&gt;", text)

    def test_fmt_wallets_page_spoilers(self):
        w = [
            {"address": "0xaa", "private_key": "0xkey", "mnemonic": "word1 word2", "total_actions": 3}
        ]
        text, pages = bot.fmt_wallets_page(w, 1)
        self.assertEqual(pages, 1)
        self.assertIn("<spoiler>0xkey</spoiler>", text)
        self.assertIn("<spoiler>word1 word2</spoiler>", text)
        self.assertIn("0xaa", text)

    def test_fmt_wallets_empty(self):
        text, pages = bot.fmt_wallets_page([], 1)
        self.assertEqual(pages, 1)
        self.assertIn("Кошельков пока нет", text)

    def test_wallets_kb(self):
        self.assertIsNone(bot.wallets_kb(1, 1))
        kb = bot.wallets_kb(1, 3)
        self.assertEqual(kb.inline_keyboard[0][0].callback_data, "wl:2")
        kb2 = bot.wallets_kb(3, 3)
        self.assertEqual(kb2.inline_keyboard[0][0].callback_data, "wl:2")

    def test_fetch_wallets_without_token(self):
        self.assertIsNone(bot.fetch_wallets())


if __name__ == "__main__":
    unittest.main()
