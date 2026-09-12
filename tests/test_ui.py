"""Тесты UI-фасада в plain-режиме: без rich, ASCII-фолбэк, чистота вывода."""

import contextlib
import io
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.ui import UI


class TestUITexture(unittest.TestCase):
    def setUp(self):
        import core.ui as ui_mod

        # Тесты проверяют ASCII-фолбэк plain-режима принудительно:
        # иначе они зависят от кодировки пайпа (cp1251/utf-8) окружения.
        self._saved_wide = ui_mod._WIDE
        self._saved_done = ui_mod.BAR_DONE
        self._saved_left = ui_mod.BAR_LEFT
        ui_mod._WIDE = False
        ui_mod.BAR_DONE = "="
        ui_mod.BAR_LEFT = "-"
        self.u = UI(use_rich=False)

    def tearDown(self):
        import core.ui as ui_mod

        ui_mod._WIDE = self._saved_wide
        ui_mod.BAR_DONE = self._saved_done
        ui_mod.BAR_LEFT = self._saved_left

    def test_bar_clamped(self):
        # plain: '=' закрыто, '-' открыто, справа проценты
        b0 = self.u.bar(0.0, width=10)
        self.assertTrue(b0.startswith("-"))
        self.assertTrue(b0.endswith("%"))
        self.assertNotIn("=", b0)
        b1 = self.u.bar(1.0, width=10)
        self.assertTrue(b1.startswith("="))
        self.assertIn("100.0%", b1)
        self.assertNotIn("=", self.u.bar(-1.0, width=5))
        self.assertNotIn("-", self.u.bar(2.0, width=5).split(" ")[0])

    def test_dot_by_latency(self):
        # коды ANSI: GREEN=32m, YELLOW=33m, RED=31m; глиф через _safe ('o')
        self.assertIn("31m", self.u.dot(None))
        self.assertIn("32m", self.u.dot(100))
        self.assertIn("32m", self.u.dot(400))
        self.assertIn("33m", self.u.dot(800))
        self.assertIn("31m", self.u.dot(2000))
        self.assertIn("o", self.u.dot(100))

    def test_frame(self):
        self.assertIn(self.u.frame(), "|/-\\")

    def test_poll_keys_plain(self):
        # в тестовом пайпе не-TTY: вернёт ""
        self.assertEqual(self.u.poll_keys(), "")

    def test_renders_without_rich_markup(self):
        # никаких rich-тегов в plain-выводе
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.u.banner("ПОЛИРОВКА", "subtitle")
            self.u.divider()
            self.u.table("T", ["A"], [["v"]])
            self.u.toast("сообщение системе", kind="ok")
            self.u.menu_key("Начать", "1")
        out = buf.getvalue()
        self.assertNotIn("[/", out)
        self.assertNotIn("[bold", out)
        self.assertNotIn("[dim", out)

    def test_gradient_banner_plain_width(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.u.banner("ТЕСТ БАННЕРА")
        out = buf.getvalue()
        self.assertIn("ТЕСТ БАННЕРА", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
