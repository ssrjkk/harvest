"""Самодиагностика окружения (--doctor / пункт меню D).

Проверяет: конфиг, master-ключ, базу данных, RPC (сеть, цепочку, задержку),
кран и лицензионный гейт. Работает и с rich-UI (ui не None), и без него.
"""

import logging

logger = logging.getLogger(__name__)


async def doctor(ui, config: dict) -> bool:
    from core.config_validate import validate_config
    from core.crypto import MasterKeyError, resolve_master_key
    from core.database import Database, _redact_rpc_url
    from core.faucet import Faucet
    from core.license import LicenseManager
    from core.network import NetworkManager
    from core.performance import summarize

    def emit(label: str, ok: bool, detail: str = "") -> None:
        from core.ui import _safe

        mark = _safe("\u2713") if ok else _safe("\u2717")
        tail = f" — {detail}" if detail else ""
        text = f"  {mark} {label}{tail}"
        if ui is not None:
            ui.print(text, style="bold green" if ok else "bold red")
        else:
            print(text)

    all_ok = True

    # 1. Конфиг
    try:
        validate_config(config)
        emit("Конфиг", True)
    except Exception as e:
        all_ok = False
        emit("Конфиг", False, str(e))

    # 1а. Производительность (информационно): пиковое использование ядер
    try:
        perf = summarize(config)
        emit(
            "Производительность",
            True,
            f"ядра CPU={perf['cpu_cores']} | workers={perf['max_workers']} | "
            f"RPC-потоков={perf['rpc_threads']} | gen_workers={perf['gen_workers']} | "
            f"chunk={perf['chunk_size']}",
        )
    except Exception as e:
        all_ok = False
        emit("Производительность", False, str(e))

    # 2. Master-ключ
    key = None
    try:
        key = resolve_master_key(config)
        emit("Master-ключ", True, "шифрование доступно")
    except MasterKeyError as e:
        all_ok = False
        emit("Master-ключ", False, str(e))

    # 3. Лицензия (информационно)
    lm = LicenseManager(config)
    if lm.enabled:
        emit("Лицензия", True, "запрос пароля активен (license-hub на GitHub)")
    else:
        emit("Лицензия", True, "gate выключен (enabled: false)")

    # 4. База данных
    db = None
    try:
        db = Database(config["database"]["path"], master_key=key)
        await db.init()
        st = await db.get_stats()
        emit(
            "База данных",
            True,
            f"{st['total_wallets']} кошельков, {st['total_actions']} действий",
        )
    except Exception as e:
        all_ok = False
        emit("База данных", False, str(e))

    # 5. RPC: доступность, цепочка, задержка
    net = None
    mem = None
    try:
        used_db = db
        if used_db is None:
            # БД не открылась — для RPC-проверок достаточно пустой in-memory
            mem = Database(":memory:")
            await mem.init()
            used_db = mem
        assert used_db is not None
        net = NetworkManager(config, used_db)
        latency = await net.latency_probe()
        if latency is None:
            all_ok = False
            emit("RPC", False, f"{_redact_rpc_url(config['network']['rpc_url']) or ''} недоступен")
        else:
            chain = config["network"].get("chain_id")
            try:
                real = await net.run_in_executor(lambda: net.w3.eth.chain_id)
            except Exception as e:
                emit("RPC", False, f"не удалось получить chain_id: {e}")
                all_ok = False
            else:
                if chain is not None and int(real) != int(chain):
                    all_ok = False
                    emit("RPC", False, f"chain_id {real} != ожидаемый {chain}")
                else:
                    emit("RPC", True, f"{latency * 1000:.0f} мс, chain_id={real}")
    except Exception as e:
        all_ok = False
        emit("RPC", False, str(e))

    # 6. Кран
    try:
        if config.get("faucet", {}).get("enabled", True):
            fa = Faucet(config)
            try:
                live = await fa.validate()
                if live <= 0:
                    emit("Кран", False, "все стратегии недоступны")
                    all_ok = False
                else:
                    emit("Кран", True, f"{live} стратегий в работе")
            finally:
                await fa.close()
        else:
            emit("Кран", True, "отключён в конфиге (--skip-faucet)")
    except Exception as e:
        all_ok = False
        emit("Кран", False, str(e))

    # Закрытие каждого ресурса независимо: сбой одного не должен оставлять
    # незакрытыми остальные (TCP-сокеты и executor без shutdown держат процесс).
    for closer, name in (
        (db.close if db is not None else None, "БД"),
        (mem.close if mem is not None else None, "in-memory БД"),
        (net.close if net is not None else None, "NetworkManager"),
    ):
        if closer is None:
            continue
        try:
            await closer()
        except Exception as e:
            logger.warning(f"Doctor: ошибка закрытия {name}: {e}")
    return all_ok
