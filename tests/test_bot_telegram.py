"""Тесты Telegram-бота: HTML-экранирование динамических значений статистики."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from portal.bot_telegram import _fmt_stats

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
