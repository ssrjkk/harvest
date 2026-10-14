"""Тесты bootstrap.py: установка только при смене манифестов."""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bootstrap import manifests_digest


def _write(root: Path, name: str, content: str = "x==1\n") -> None:
    (root / name).write_text(content, encoding="utf-8")


class TestManifestsDigest(unittest.TestCase):
    def test_stable_and_sensitive(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write(root, "requirements.txt", "a==1\n")
            _write(root, "requirements-portal.txt", "b==2\n")
            d1 = manifests_digest(str(root))
            self.assertIsNotNone(d1)
            self.assertEqual(d1, manifests_digest(str(root)))
            _write(root, "requirements-portal.txt", "b==3\n")
            self.assertNotEqual(d1, manifests_digest(str(root)))

    def test_missing_manifest_is_none(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(manifests_digest(str(Path(td))))


class TestMainFlow(unittest.TestCase):
    def test_skips_pip_when_stamp_matches(self):
        import tempfile

        import bootstrap

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write(root, "requirements.txt")
            _write(root, "requirements-portal.txt")
            (root / bootstrap.STAMP_FILE).write_text(
                manifests_digest(str(root)), encoding="utf-8"
            )
            with mock.patch("subprocess.run") as run:
                self.assertEqual(bootstrap.main(str(root)), 0)
                run.assert_not_called()

    def test_runs_pip_and_stamps_on_change(self):
        import tempfile

        import bootstrap

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write(root, "requirements.txt")
            _write(root, "requirements-portal.txt")
            proc = mock.Mock()
            proc.returncode = 0
            with mock.patch("subprocess.run", return_value=proc) as run:
                self.assertEqual(bootstrap.main(str(root)), 0)
                run.assert_called_once()
            stamp = (root / bootstrap.STAMP_FILE).read_text(encoding="utf-8")
            self.assertEqual(stamp, manifests_digest(str(root)))

    def test_pip_failure_propagates_and_no_stamp(self):
        import tempfile

        import bootstrap

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write(root, "requirements.txt")
            _write(root, "requirements-portal.txt")
            proc = mock.Mock()
            proc.returncode = 1
            with mock.patch("subprocess.run", return_value=proc):
                self.assertEqual(bootstrap.main(str(root)), 1)
            self.assertFalse((root / bootstrap.STAMP_FILE).exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
