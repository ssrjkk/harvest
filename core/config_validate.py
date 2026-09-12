"""Валидация конфигурации: проверка ключей, диапазонов, типов.

Вызывается при старте для поимки ошибок конфигурации до запуска фарма.
"""

from typing import Any
from urllib.parse import urlsplit

from web3 import Web3


class ConfigError(Exception):
    pass


# Дефолты, используемые в нескольких модулях (единый источник истины)
DEFAULT_MAX_WORKERS = 20

# Все поддерживаемые типы действий (сетево-agnostic + network-specific)
VALID_ACTION_TYPES = frozenset(
    {
        # Базовые (все сети)
        "transfer",
        "vibevibe_swap",
        "vibevibe_mint",
        # Flop Labs
        "flop_compute",
        "flop_validate",
        "flop_stake",
        # Arc Testnet (Minara.Fun)
        "arc_launch",
        "arc_trade",
        "arc_add_liquidity",
    }
)

# Подмножество: контрактные действия (обязаны иметь contract и method)
_CONTRACT_ACTION_TYPES = frozenset(t for t in VALID_ACTION_TYPES if t != "transfer")


def _check_range(
    name: str,
    value: Any,
    min_val: float = 0,
    max_val: float | None = None,
    errors: list[str] | None = None,
) -> None:
    """Проверяет, что value — число в диапазоне [min_val, max_val].

    С errors=None — бросает ConfigError (точечный вызов); с errors — накапливает
    сообщение, не прерывая проверку остальных секций (как все проверки в файле).
    """
    msg: str | None = None
    if not isinstance(value, (int, float)):
        msg = f"{name}: ожидается число, получено {type(value).__name__}"
    elif value < min_val:
        msg = f"{name}: {value} < {min_val}"
    elif max_val is not None and value > max_val:
        msg = f"{name}: {value} > {max_val}"
    if msg is not None:
        if errors is None:
            raise ConfigError(msg)
        errors.append(msg)


def _check_range_pair(name: str, value: Any, errors: list[str] | None = None) -> None:
    """Проверяет, что value — список [min, max] где min <= max, или одно число."""
    msg: str | None = None
    if isinstance(value, (list, tuple)):
        if len(value) != 2:
            msg = f"{name}: ожидается [min, max], получено {value}"
        else:
            lo, hi = value
            if not isinstance(lo, (int, float)) or not isinstance(hi, (int, float)):
                msg = f"{name}: min/max должны быть числами"
            elif lo > hi:
                msg = f"{name}: min ({lo}) > max ({hi})"
    elif not isinstance(value, (int, float)):
        msg = f"{name}: ожидается [min, max] или число, получено {type(value).__name__}"
    if msg is not None:
        if errors is None:
            raise ConfigError(msg)
        errors.append(msg)


def _check_https_or_localhost(name: str, url: str, errors: list[str]) -> None:
    """RPC/faucet URL обязан идти по TLS, кроме loopback (dev-нода)."""
    if not isinstance(url, str) or not url:
        return
    if url.startswith("http://"):
        host = (urlsplit(url).hostname or "").lower()
        if host not in ("127.0.0.1", "localhost", "::1"):
            errors.append(f"{name}: http:// без TLS ({host}) — трафик подписей/балансов в открытом виде")


def _check_rpc_url(name: str, rpc: Any, errors: list[str]) -> None:
    if isinstance(rpc, list):
        for u in rpc:
            _check_https_or_localhost(f"{name}[{u}]", u, errors)
    elif isinstance(rpc, str):
        _check_https_or_localhost(name, rpc, errors)


def _check_log_level(name: str, value: Any, errors: list[str]) -> None:
    """logging.level должен быть валидным именем уровня, иначе setup_logging падает."""
    if not isinstance(value, str) or not value:
        return
    valid = {"CRITICAL", "FATAL", "ERROR", "WARN", "WARNING", "INFO", "DEBUG", "NOTSET"}
    if value.upper() not in valid:
        errors.append(f"{name}: '{value}' не уровень логирования (CRITICAL/ERROR/WARNING/INFO/DEBUG)")


def _check_path(name: str, value: Any, errors: list[str]) -> None:
    """Пути из конфига не должны выходить из рабочей директории через '..'."""
    if not isinstance(value, str) or not value:
        return
    if any(seg == ".." for seg in value.replace("\\", "/").split("/")):
        errors.append(f"{name}: недопустимый путь (содержит '..'): {value}")


def validate_config(config: dict) -> None:
    """Полная валидация конфигурации. Raises ConfigError при ошибке."""
    errors = []

    # --- network ---
    net = config.get("network")
    if not net:
        errors.append("network: отсутствует секция")
    else:
        rpc = net.get("rpc_url")
        if not rpc:
            errors.append("network.rpc_url: не задан")
        elif isinstance(rpc, list):
            if len(rpc) == 0 or not all(isinstance(u, str) and u for u in rpc):
                errors.append("network.rpc_url: должен быть строкой или непустым списком строк")
            else:
                _check_rpc_url("network.rpc_url", rpc, errors)
        elif not isinstance(rpc, str):
            errors.append(f"network.rpc_url: неверный тип {type(rpc).__name__}")
        else:
            _check_rpc_url("network.rpc_url", rpc, errors)
        _check_range("network.chain_id", net.get("chain_id", 0), 1, errors=errors)

    # --- wallets ---
    wallets = config.get("wallets")
    if wallets:
        count = wallets.get("count", 50)
        if not isinstance(count, int) or count < 0:
            errors.append(f"wallets.count: {count} (ожидается >= 0)")

    # --- faucet ---
    faucet = config.get("faucet")
    if faucet:
        mb = faucet.get("min_balance", 0.005)
        tb = faucet.get("target_balance", 0.05)
        _check_range("faucet.min_balance", mb, 0, errors=errors)
        _check_range("faucet.target_balance", tb, 0, errors=errors)
        if mb > tb:
            errors.append(f"faucet: min_balance ({mb}) > target_balance ({tb})")
        retries = faucet.get("retries", 3)
        _check_range("faucet.retries", retries, 1, 20, errors=errors)
        _check_range("faucet.max_concurrent", faucet.get("max_concurrent", 8), 1, 64, errors=errors)
        strategies = faucet.get("strategies", [])
        if strategies is None:
            errors.append("faucet.strategies: отсутствует список")
        elif not isinstance(strategies, list):
            errors.append(f"faucet.strategies: ожидается список, получено {type(strategies).__name__}")
        else:
            for i, s in enumerate(strategies):
                if not isinstance(s, dict):
                    errors.append(f"faucet.strategies[{i}]: ожидается объект (dict), получен {type(s).__name__}")
                    continue
                url = s.get("url", "")
                if not isinstance(url, str) or not url:
                    errors.append(f"faucet.strategies[{i}].url: обязателен")
                    continue
                _check_https_or_localhost(f"faucet.strategies[{i}].url", url, errors)

    # --- actions ---
    actions = config.get("actions")
    if not actions:
        errors.append("actions: нет действий (нужен хотя бы один)")
    elif not isinstance(actions, list):
        errors.append("actions: должен быть списком")
    else:
        for i, a in enumerate(actions):
            if not isinstance(a, dict):
                errors.append(f"actions[{i}]: ожидается объект (dict), получен {type(a).__name__}")
                continue
            if "type" not in a:
                errors.append(f"actions[{i}]: нет ключа 'type'")
            else:
                atype = a["type"]
                if atype not in VALID_ACTION_TYPES:
                    errors.append(
                        f"actions[{i}].type: '{atype}' неизвестен (допустимые: {', '.join(sorted(VALID_ACTION_TYPES))})"
                    )
            w = a.get("weight", 1.0)
            if not isinstance(w, (int, float)) or w < 0:
                errors.append(f"actions[{i}].weight: {w} (ожидается >= 0)")
            # Проверка диапазонов сумм
            min_amt = a.get("min_amount")
            max_amt = a.get("max_amount")
            if min_amt is not None:
                if not isinstance(min_amt, (int, float)):
                    errors.append(f"actions[{i}].min_amount: ожидается число, получено {type(min_amt).__name__}")
                elif min_amt < 0:
                    errors.append(f"actions[{i}].min_amount: {min_amt} < 0")
            if max_amt is not None:
                if not isinstance(max_amt, (int, float)):
                    errors.append(f"actions[{i}].max_amount: ожидается число, получено {type(max_amt).__name__}")
                elif max_amt < 0:
                    errors.append(f"actions[{i}].max_amount: {max_amt} < 0")
            if isinstance(min_amt, (int, float)) and isinstance(max_amt, (int, float)) and min_amt > max_amt:
                errors.append(f"actions[{i}]: min_amount ({min_amt}) > max_amount ({max_amt})")
            # transfer обязан указывать получателя (иначе каждая попытка фейл)
            if atype == "transfer":
                tgt = a.get("target")
                if not tgt:
                    errors.append(f"actions[{i}].target: обязателен для transfer (адрес или 'random_wallet')")
                elif tgt != "random_wallet" and not Web3.is_address(tgt):
                    errors.append(
                        f"actions[{i}].target: '{tgt}' не похож на адрес (нужен 0x+40hex или 'random_wallet')"
                    )
            # Контрактные действия обязаны иметь contract и method
            if atype in _CONTRACT_ACTION_TYPES:
                contract = a.get("contract", "")
                if not contract:
                    errors.append(f"actions[{i}].contract: обязателен для {atype}")
                elif contract != "0x0000000000000000000000000000000000000000" and not Web3.is_address(contract):
                    errors.append(f"actions[{i}].contract: '{contract}' не похож на адрес")
                method = a.get("method", "")
                if not method:
                    errors.append(f"actions[{i}].method: обязателен для {atype}")

    # --- farming ---
    farm = config.get("farming")
    if not farm:
        errors.append("farming: отсутствует секция")
    else:
        _check_range_pair("farming.actions_per_cycle", farm.get("actions_per_cycle", [3, 8]), errors)
        _check_range_pair("farming.delay_between_actions", farm.get("delay_between_actions", [5, 20]), errors)
        _check_range_pair(
            "farming.delay_between_cycles",
            farm.get("delay_between_cycles", [3600, 7200]),
            errors,
        )
        scp = farm.get("skip_cycle_probability", 0.05)
        _check_range("farming.skip_cycle_probability", scp, 0, 1, errors=errors)

    # --- advanced ---
    adv = config.get("advanced", {})
    gl = adv.get("gas_limit", 21000)
    _check_range("advanced.gas_limit", gl, 1, errors=errors)
    # При РЕАЛЬНЫХ контрактных действиях (не-заглушка 0x0) gas_limit должен быть
    # достаточным (min 50000). Заглушки в runtime падают на transfer (actions.py),
    # им 21000 достаточно — требоваться 50000 за них неправильно.
    _ZERO_ADDR = "0x0000000000000000000000000000000000000000"
    has_real_contract = any(
        isinstance(a, dict)
        and a.get("type", "") in _CONTRACT_ACTION_TYPES
        and a.get("contract", "") not in (None, "", _ZERO_ADDR)
        for a in (actions or [])
    )
    if has_real_contract and gl < 50000:
        errors.append(f"advanced.gas_limit: {gl} слишком мал для контрактных действий (нужно >= 50000)")
    mga = adv.get("min_gas_for_action", 0.003)
    _check_range("advanced.min_gas_for_action", mga, 0, errors=errors)

    # --- threading ---
    th = config.get("threading", {})
    _check_range("threading.max_workers", th.get("max_workers", DEFAULT_MAX_WORKERS), 1, 1000, errors=errors)
    _check_range("threading.timeout_per_wallet", th.get("timeout_per_wallet", 0), 0, errors=errors)
    if th.get("gen_workers"):
        _check_range("threading.gen_workers", th.get("gen_workers"), 1, 32, errors=errors)
    if th.get("rpc_threads"):
        _check_range("threading.rpc_threads", th.get("rpc_threads"), 1, 256, errors=errors)
    if th.get("chunk_size"):
        _check_range("threading.chunk_size", th.get("chunk_size"), 1, errors=errors)

    # --- cache (RPC-скорость и её адаптивный пол) ---
    cache = config.get("cache", {})
    if cache:
        _check_range("cache.rpc_rate_limit", cache.get("rpc_rate_limit", 50), 1, 10000, errors=errors)
        _check_range("cache.rpc_rate_floor", cache.get("rpc_rate_floor", 0.2), 0.05, 1, errors=errors)

    # --- database ---
    db = config.get("database")
    if not db or not db.get("path"):
        errors.append("database.path: не задан")
    else:
        _check_path("database.path", db.get("path"), errors)

    # --- logging ---
    log_cfg = config.get("logging")
    if log_cfg:
        _check_log_level("logging.level", log_cfg.get("level"), errors)
        if log_cfg.get("file") is not None:
            _check_path("logging.file", log_cfg.get("file"), errors)

    # --- license ---
    lic = config.get("license")
    if lic and lic.get("cache_file") is not None:
        _check_path("license.cache_file", lic.get("cache_file"), errors)

    if errors:
        msg = "Ошибки конфигурации:\n" + "\n".join(f"  - {e}" for e in errors)
        raise ConfigError(msg)
