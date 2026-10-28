"""tools/make_lock.py: генерация и проверка requirements-lock.txt."""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import make_lock as ml


class TestMakeLock(unittest.TestCase):
    def test_parse_sources_unique_names(self):
        reqs = ml._parse_sources()
        names = [n for n, _ in reqs]
        self.assertIn("aiogram", [n.lower() for n in names])
        self.assertIn("web3", [n.lower() for n in names])
        self.assertEqual(len(names), len({n.lower() for n in names}))
        specs = dict((n.lower(), s) for n, s in reqs)
        self.assertTrue(specs["aiogram"].startswith(">="))

    def test_build_uses_installed_versions(self):
        with mock.patch("importlib.metadata.version", return_value="1.2.3"):
            locked = ml.build()
        self.assertTrue(locked)
        self.assertTrue(all(v == "1.2.3" for v in locked.values()))

    def test_build_skips_missing(self):
        import contextlib
        import io

        def _boom(name):
            raise ImportError("nope")

        buf = io.StringIO()
        with mock.patch("importlib.metadata.version", side_effect=_boom):
            with contextlib.redirect_stdout(buf):
                locked = ml.build()
        self.assertEqual(locked, {})
        self.assertIn("warn", buf.getvalue())

    def test_check_fresh_and_stale(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            lock = Path(td) / "requirements-lock.txt"
            lock.write_text("aiogram==3.31.0\nweb3==8.0.0\n", encoding="utf-8")
            with mock.patch.object(ml, "LOCK", lock):
                with mock.patch.object(
                    ml, "_parse_sources",
                    return_value=[("aiogram", ">=3.10,<4"), ("web3", ">=8.0.0,<9")],
                ):
                    self.assertEqual(ml._check(), 0)
                with mock.patch.object(
                    ml, "_parse_sources",
                    return_value=[("aiogram", ">=99"), ("web3", ">=8.0.0,<9")],
                ):
                    self.assertEqual(ml._check(), 1)
                with mock.patch.object(
                    ml, "_parse_sources", return_value=[("nope", ">=1")],
                ):
                    self.assertEqual(ml._check(), 1)

    def test_check_missing_lock(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(ml, "LOCK", Path(td) / "nope.txt"):
                self.assertEqual(ml.main(["--check"]), 1)

    def test_main_build_writes_lock(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            lock = Path(td) / "requirements-lock.txt"
            with mock.patch.object(ml, "LOCK", lock):
                with mock.patch.object(
                    ml, "_parse_sources", return_value=[("aiogram", ">=3.10,<4")],
                ):
                    with mock.patch("importlib.metadata.version", return_value="3.31.0"):
                        self.assertEqual(ml.main([]), 0)
            text = lock.read_text(encoding="utf-8")
            self.assertIn("aiogram==3.31.0", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
