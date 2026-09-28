"""Утилиты: задержки, user-agent, форматирование адресов, env var override."""

import asyncio
import logging
import os
import random
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_ua: Any | None = None

_ENV_MAP = {
    "FARMER_RPC_URL": ("network", "rpc_url"),
    "FARMER_CHAIN_ID": ("network", "chain_id"),
    "FARMER_WALLET_COUNT": ("wallets", "count"),
    "FARMER_WORKERS": ("threading", "max_workers"),
    # Тюнинг пиковой загрузки ядер / RPC (задают те же ключи, что config)
    "FARMER_GEN_WORKERS": ("threading", "gen_workers"),
    "FARMER_RPC_THREADS": ("threading", "rpc_threads"),
    "FARMER_CHUNK_SIZE": ("threading", "chunk_size"),
    "FARMER_FAUCET_MAX_CONCURRENT": ("faucet", "max_concurrent"),
    "FARMER_RPC_RATE": ("cache", "rpc_rate_limit"),
    "FARMER_RPC_RATE_FLOOR": ("cache", "rpc_rate_floor"),
    "FARMER_MIN_BALANCE": ("faucet", "min_balance"),
    "FARMER_TARGET_BALANCE": ("faucet", "target_balance"),
    "FARMER_LOG_LEVEL": ("logging", "level"),
    "FARMER_DB_PATH": ("database", "path"),
}


def _coerce_env_value(raw: str) -> Any:
    """Преобразует строку env-переменной в число (int/float) или оставляет строкой.

    Только ASCII: isdigit()/float() принимают Unicode-цифры («١٢٣» → 123.0),
    что делает конфиг недетерминированным и непредсказуемым — не-ASCII
    значения оставляем строкой как есть.
    """
    if not raw.isascii():
        return raw
    if raw.isdigit() or (raw.startswith("-") and raw[1:].isdigit()):
        return int(raw)
    try:
        return float(raw)
    except ValueError:
        return raw


def _split_rpc_urls(raw: str) -> str | list[str]:
    """FARMER_RPC_URL допускает несколько эндпоинтов через `;` или `,`.

    Один эндпоинт остаётся строкой (совместимость с конфиг-схемой),
    несколько — списком, который парсит NetworkManager (network.py).
    """
    if not raw:
        return raw
    parts = [u.strip() for u in re.split(r"[;,]", raw) if u.strip()]
    return parts[0] if len(parts) == 1 else parts


def apply_env_overrides(config: dict) -> dict:
    for env_key, (section, key) in _ENV_MAP.items():
        raw = os.environ.get(env_key)
        if raw is not None:
            value = _split_rpc_urls(raw) if env_key == "FARMER_RPC_URL" else _coerce_env_value(raw)
            config.setdefault(section, {})[key] = value
    return config


def restrict_file_permissions(path: str) -> None:
    """Ограничивает права доступа к файлу (только владелец).

    Unix: chmod 600. Windows: icacls — убрать наследование прав и дать
    доступ только текущему пользователю. При неудаче логируем предупреждение
    (молчаливый no-op тут опасен — файл остаётся доступен другим локальным
    пользователям).
    """
    try:
        if os.name != "nt":
            import stat

            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
            return
        import getpass

        user = getpass.getuser()
        r = subprocess.run(
            ["icacls", path, "/inheritance:r", "/grant:r", f"{user}:(F)"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if r.returncode != 0:
            logger.warning(f"icacls {path} не применён (код {r.returncode}): {r.stderr.strip() or r.stdout.strip()}")
    except Exception as e:
        logger.warning(f"Не удалось ограничить права файла {path}: {e}")


def _get_ua() -> Any | None:
    """Ленивая инициализация UserAgent — не блокирует старт при недоступном источнике."""
    global _ua
    if _ua is None:
        try:
            from fake_useragent import UserAgent

            _ua = UserAgent()
        except Exception:
            _ua = None
    return _ua


async def asleep(min_sec: float, max_sec: float) -> float:
    """Асинхронная случайная задержка (не блокирует event loop)."""
    delay = random.uniform(min_sec, max_sec)
    await asyncio.sleep(delay)
    return delay


def get_random_user_agent() -> str:
    ua = _get_ua()
    if ua is None:
        return "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    try:
        return ua.random
    except Exception:
        return "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


def truncate_address(address: str | None, chars: int = 6) -> str:
    if not address:
        return "0x..."
    address = str(address)
    if len(address) < chars * 2 + 2:
        return address
    return f"{address[: chars + 2]}...{address[-chars:]}"


def resolve_bundled(rel: str) -> Path:
    """Резолвит путь к ресурсу, включая PyInstaller-бандл.

    В exe-сборке данные (abi/ и т.п., см. harvest.spec: datas) живут в
    _MEIPASS (onefile: временный каталог, onedir: _internal). Рабочий
    каталог при запуске exe может быть любым, так что сначала ищем ресурс
    рядом с процессом, и только потом возвращаем исходный относительный путь.
    """
    if not rel or Path(rel).is_absolute() or Path(rel).exists():
        return Path(rel)
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        cand = Path(meipass) / rel
        if cand.exists():
            return cand
    if getattr(sys, "frozen", False):
        cand = Path(sys.executable).resolve().parent / rel
        if cand.exists():
            return cand
    return Path(rel)
