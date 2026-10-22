"""portal.render_page/substitute_version: версия в статике из core.version."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.version import VERSION  # noqa: E402
from portal import render_page, substitute_version  # noqa: E402


class TestRenderPage(unittest.TestCase):
    def test_substitute_version(self):
        self.assertEqual(substitute_version("v{{HARVEST_VERSION}}!"), f"v{VERSION}!")
        self.assertEqual(substitute_version("без плейсхолдера"), "без плейсхолдера")

    def test_portal_pages_render_without_placeholder(self):
        root = Path(__file__).resolve().parent.parent
        for rel in ("portal/static/index.html",
                    "portal/static/login.html",
                    "portal/static/miniapp/app.html"):
            with self.subTest(rel=rel):
                text = render_page(root / rel)
                self.assertNotIn("{{HARVEST_VERSION}}", text)
                self.assertIn(VERSION, text)


if __name__ == "__main__":
    unittest.main()
