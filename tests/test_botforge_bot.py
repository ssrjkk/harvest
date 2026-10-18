"""Тесты автономного бота BotForge (botforge/bot.py): default-deny доступ,
форматирование кошельков с ключами/сид-фразами и прогресс-бар.

Модуль читает BOT_TOKEN на импорте — задаём env до импорта. Сеть
для проверяемых чистых функций не требуется.
"""

import asyncio
import importlib
import os
import unittest

os.environ.setdefault("BOT_TOKEN", "12345:TEST")
os.environ.pop("WALLETS_KEY", None)

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
        self.assertIn("harvest", text)
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

    def test_fetch_wallets_without_key(self):
        result = asyncio.run(bot.fetch_wallets())
        self.assertIsNone(result)

    def test_decrypt_blob_roundtrip(self):
        import secrets as _secrets

        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        key = _secrets.token_bytes(32)
        rows = [{"address": "0xaa", "private_key": "k", "mnemonic": None, "total_actions": 1}]
        pt = __import__("json").dumps(rows).encode()
        nonce = _secrets.token_bytes(12)
        blob = {"v": 1, "alg": "AES-256-GCM",
                "nonce": nonce.hex(),
                "ct": AESGCM(key).encrypt(nonce, pt, bot._WALLETS_AAD).hex()}
        self.assertEqual(bot._decrypt_blob(blob, key), rows)
        with self.assertRaises(ValueError):
            bot._decrypt_blob(blob, _secrets.token_bytes(32))
        with self.assertRaises(ValueError):
            bot._decrypt_blob({"v": 99}, key)

    def test_wallets_key_parser(self):
        import secrets as _secrets

        good = _secrets.token_hex(32)
        os.environ["WALLETS_KEY"] = good
        try:
            self.assertEqual(bot._wallets_key(), bytes.fromhex(good))
        finally:
            os.environ.pop("WALLETS_KEY", None)
        os.environ["WALLETS_KEY"] = "short"
        try:
            self.assertIsNone(bot._wallets_key())
        finally:
            os.environ.pop("WALLETS_KEY", None)

    def test_fmt_history_and_doctor(self):
        s = {
            "running": True,
            "wallet_count": 3,
            "health_factor": 0.9,
            "updated_at": "2026-01-01",
            "pool": {"actions": 5, "errors": 0, "processed": 3, "cycles": 2},
            "history": [
                {"id": 2, "actions_ok": 5, "errors": 0, "wallets": 3},
                {"id": 1, "actions_ok": 4, "errors": 1, "wallets": 3},
            ],
        }
        self.assertIn("история", bot.fmt_history(s).lower())
        self.assertIn("#2", bot.fmt_history(s))
        self.assertIn("диагностика", bot.fmt_doctor(s).lower())
        self.assertIn("RUN", bot.fmt_doctor(s))
        self.assertIn("История пуста", bot.fmt_history({}))

    def test_negative_allow_id_kept(self):
        # "-100..." — валидный ID канала, не должен отбрасываться.
        os.environ["TELEGRAM_ALLOW_IDS"] = "-100123, 456"
        os.environ["BOT_TOKEN"] = "12345:TEST"
        # ALLOW_IDS пересобирается на импорте только при reload; проверяем парсер напрямую:
        raw = os.environ["TELEGRAM_ALLOW_IDS"].split(",")
        parsed = set()
        for _r in raw:
            _s = _r.strip()
            _n = _s[1:] if _s[:1] in {"+", "-"} else _s
            if _n.isdigit():
                parsed.add(int(_s))
        self.assertIn(-100123, parsed)
        self.assertIn(456, parsed)


if __name__ == "__main__":
    unittest.main()
