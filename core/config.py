"""Загрузка и валидация конфигурации (единая точка для всех entry points).

auto.py и main.py используют один и тот же код, чтобы поведение
(проверка существования, обязательных ключей, env-override, валидация)
не расходилось между режимами.
"""

from pathlib import Path

import yaml

from core.config_validate import ConfigError, validate_config
from core.utils import apply_env_overrides

# Обязательные секции верхнего уровня
_REQUIRED_SECTIONS = [
    "network",
    "wallets",
    "faucet",
    "actions",
    "farming",
    "threading",
    "database",
]


def load_config(path: str = "config.yaml") -> dict | None:
    """Загружает и валидирует конфиг. Возвращает dict или None при ошибке."""
    p = Path(path)
    if not p.exists():
        print(f"Файл {path} не найден!")
        return None
    try:
        with open(path, encoding="utf-8") as f:
            config = yaml.safe_load(f)
    except yaml.YAMLError as e:
        print(f"Ошибка чтения YAML из {path}: {e}")
        return None
    if not isinstance(config, dict):
        print(f"Файл {path} пуст или содержит некорректную структуру (ожидается объект)")
        return None

    # Env var override: FARMER_RPC_URL, FARMER_CHAIN_ID, etc.
    try:
        apply_env_overrides(config)
    except Exception as e:
        print(f"Ошибка применения env-override к {path}: {e}")
        return None

    missing = [k for k in _REQUIRED_SECTIONS if not config.get(k)]
    if missing:
        print(f"В конфиге отсутствуют ключи: {', '.join(missing)}")
        return None

    try:
        validate_config(config)
    except ConfigError as e:
        print(f"{e}")
        return None
    # Страховка от мусорных типов в некоторых секциях: валидатор всегда
    # должен говорить "конфиг невалиден", а не сыпать AttributeError/TypeError.
    except (AttributeError, TypeError, KeyError) as e:
        print(f"Конфиг {path} некорректен ({type(e).__name__}): {e}")
        return None

    return config
