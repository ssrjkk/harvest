"""Логирование с ротацией, цветным консольным выводом и опциональным JSON-форматом."""

import json
import logging
import sys
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from colorama import Fore, Style, init

from core.utils import restrict_file_permissions

init(autoreset=True)


class RestrictedRotatingFileHandler(RotatingFileHandler):
    """RotatingFileHandler, который после создания и ротации файла ограничивает
    права доступа (только владелец) — как у master.key / БД. Иначе лог остаётся
    читаемым для других локальных пользователей системы."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._restrict()

    def doRollover(self) -> None:
        super().doRollover()
        self._restrict()

    def _restrict(self) -> None:
        try:
            restrict_file_permissions(self.baseFilename)
        except OSError:
            pass


class ColoredFormatter(logging.Formatter):
    COLORS = {
        "DEBUG": Fore.CYAN,
        "INFO": Fore.GREEN,
        "WARNING": Fore.YELLOW,
        "ERROR": Fore.RED,
        "CRITICAL": Fore.RED + Style.BRIGHT,
    }

    def format(self, record) -> str:
        saved = record.levelname
        color = self.COLORS.get(saved, "")
        if color:
            record.levelname = f"{color}{saved}{Style.RESET_ALL}"
        result = super().format(record)
        record.levelname = saved
        return result


class JSONFormatter(logging.Formatter):
    """Structured JSON formatter для машинного парсинга логов."""

    def format(self, record) -> str:
        log_entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info and record.exc_info[0]:
            log_entry["exception"] = self.formatException(record.exc_info)
        if hasattr(record, "extra_data"):
            log_entry["extra"] = record.extra_data
        return json.dumps(log_entry, ensure_ascii=False)


def setup_logging(config: dict) -> logging.Logger:
    log_config = config.get("logging", {})
    log_file = log_config.get("file", "logs/farm.log")
    level_raw = log_config.get("level") or "INFO"
    if not isinstance(level_raw, str):
        logging.getLogger(__name__).warning("logging.level должен быть строкой, получено %r — берём INFO", level_raw)
        level_raw = "INFO"
    log_level = level_raw.upper()
    max_bytes = log_config.get("max_bytes", 10 * 1024 * 1024)
    backup_count = log_config.get("backup_count", 5)
    console = log_config.get("console", True)
    use_colors = log_config.get("colors", True)
    use_json = log_config.get("json_format", False)

    log_path = Path(log_file)
    if log_path.parent and str(log_path.parent) != ".":
        log_path.parent.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(log_level)
    for h in root.handlers[:]:
        root.removeHandler(h)

    file_fmt: logging.Formatter
    if use_json:
        file_fmt = JSONFormatter()
    else:
        file_fmt = logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

    file_handler = RestrictedRotatingFileHandler(
        log_file, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
    )
    file_handler.setFormatter(file_fmt)
    root.addHandler(file_handler)

    if console:
        console_handler = logging.StreamHandler(sys.stdout)
        if use_json:
            console_handler.setFormatter(JSONFormatter())
        elif use_colors:
            console_handler.setFormatter(
                ColoredFormatter("%(asctime)s - %(levelname)s - %(message)s", datefmt="%H:%M:%S")
            )
        else:
            console_handler.setFormatter(file_fmt)
        root.addHandler(console_handler)

    return root
