"""Тесты PayloadGuard: размеры, глубина, управляющие символы, спам, дедуп."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from portal.shield.config import SanitizeConfig
from portal.shield.sanitize import PayloadGuard, SanitizationError, clean_text


def _cfg(**kw):
    base = dict(max_bytes=1024, max_depth=4, max_str=64, max_items=16,
                emoji_ratio=0.5, flood_k=4)
    base.update(kw)
    return SanitizeConfig(**base)


class TestSanitize(unittest.TestCase):
    def test_ok_payload(self):
        g = PayloadGuard(_cfg())
        g.check_bytes(b'{"a": 1}')
        g.check({"a": [1, 2, {"b": "текст 😀"}]})
        self.assertEqual(g.rejections, 0)

    def test_too_big(self):
        g = PayloadGuard(_cfg())
        with self.assertRaises(SanitizationError):
            g.check_bytes(b"x" * 1025)
        self.assertEqual(g.rejections, 1)

    def test_too_deep(self):
        g = PayloadGuard(_cfg())
        deep = {}
        cur = deep
        for _ in range(6):
            cur["x"] = {}
            cur = cur["x"]
        with self.assertRaises(SanitizationError):
            g.check(deep)

    def test_too_many_items_and_long_string(self):
        g = PayloadGuard(_cfg())
        with self.assertRaises(SanitizationError):
            g.check({"k" + str(i): i for i in range(17)})
        with self.assertRaises(SanitizationError):
            g.check("y" * 65)

    def test_null_and_invisible(self):
        g = PayloadGuard(_cfg())
        with self.assertRaises(SanitizationError):
            g.check("ab\x00cd")
        with self.assertRaises(SanitizationError):
            g.check("ab cd")

    def test_emoji_spam_vs_legit(self):
        g = PayloadGuard(_cfg())
        with self.assertRaises(SanitizationError):
            g.check("😀" * 10)
        g.check("Привет 😀 как дела, всё хорошо?")
        self.assertEqual(g.rejections, 1)

    def test_flood_duplicate(self):
        g = PayloadGuard(_cfg())
        self.assertFalse(g.flood_duplicate("u", b"same"))
        self.assertTrue(g.flood_duplicate("u", b"same"))
        self.assertFalse(g.flood_duplicate("other", b"same"))
        self.assertEqual(g.evict(), 2)

    def test_clean_text(self):
        self.assertEqual(clean_text("a\x00b c", max_len=100), "abc")
        self.assertEqual(clean_text("x" * 200, max_len=10), "x" * 10)
        self.assertEqual(clean_text("a\tb\nc"), "a\tb\nc")


if __name__ == "__main__":
    unittest.main(verbosity=2)
