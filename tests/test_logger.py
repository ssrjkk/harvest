"""Тесты настройки логирования: НЕ-строковый level и регистр значений."""

import logging
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.logger import setup_logging


class TestLoggerLevel(unittest.TestCase):
    def setUp(self) -> None:
        root = logging.getLogger()
        self._old_level = root.level
        self._old_handlers = list(root.handlers)
        # Каталог удаляется в tearDown ПОСЛЕ закрытия хендлеров (файл лога
        # залочен открытым FileHandler на Windows до close()).
        self._td = tempfile.mkdtemp()
        self._log_file = str(Path(self._td) / "t.log")

    def tearDown(self) -> None:
        root = logging.getLogger()
        root.level = self._old_level
        for h in root.handlers[:]:
            if h not in self._old_handlers:
                root.removeHandler(h)
                try:
                    h.close()
                except Exception:  # noqa: BLE001
                    pass
        root.handlers[:] = self._old_handlers
        shutil.rmtree(self._td, ignore_errors=True)

    def test_level_none_does_not_crash(self):
        # Раунд 14: logging.level: null раньше падал в .upper() -> AttributeError.
        cfg = {"logging": {"file": self._log_file, "level": None, "console": True}}
        setup_logging(cfg)
        self.assertEqual(logging.getLogger().level, logging.INFO)

    def test_level_lowercase_accepted(self):
        cfg = {"logging": {"file": self._log_file, "level": "warning", "console": True}}
        setup_logging(cfg)
        self.assertEqual(logging.getLogger().level, logging.WARNING)

    def test_level_missing_defaults_to_info(self):
        cfg = {"logging": {"file": self._log_file}}
        setup_logging(cfg)
        self.assertEqual(logging.getLogger().level, logging.INFO)


if __name__ == "__main__":
    unittest.main(verbosity=2)
