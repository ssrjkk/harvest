"""Единый источник правды для значений параллелизма (пиковая нагрузка ядер).

Все модули (network / wallet / pool / doctor / auto / main) читают эффективные
значения отсюда. Когда конфиг не задаёт явное число (0/отсутствue), значения
автоподбираются под реальное железо (количество CPU-ядер) — машина мощнее,
воркеров больше, но всегда с потолками, чтобы не переопередить нагрузку
(GIL для CPU-bound, сокеты для I/O-bound).
"""

import os

from core.config_validate import DEFAULT_MAX_WORKERS

# Потолки автоподбора. Явный конфиг может разогнать выше (валидатор до 1000
# для max_workers и до 256 для rpc_threads), но автодефолты не превышают кап.
RPC_THREAD_CAP = 128
GEN_WORKERS_CAP = 8
CHUNK_FACTOR = 4
# Во сколько раз RPC-потоков больше фарм-воркеров (I/O-bound: каждый поток
# держит свой сокет, ядра почти не тратятся).
RPC_IO_FACTOR = 3


def cpu_count() -> int:
    return os.cpu_count() or 2


def _configured(config: dict, key: str) -> int:
    """Явное значение из конфига (0 и отсутствие = «авто»)."""
    try:
        return int(config.get("threading", {}).get(key, 0) or 0)
    except (TypeError, ValueError):
        return 0


def auto_max_workers() -> int:
    """Авто-воркеры фарма: 2 на ядро, минимум дефолт (20), потолок RPC_CAP."""
    return min(max(DEFAULT_MAX_WORKERS, cpu_count() * 2), RPC_THREAD_CAP)


def auto_rpc_threads() -> int:
    """Авто-RPC-потоки: 4 на ядро, минимум 8, потолок RPC_CAP."""
    return min(max(8, cpu_count() * 4), RPC_THREAD_CAP)


def auto_gen_workers() -> int:
    """Авто-workers генерации ключей (CPU-bound, PBKDF2 отпускает GIL частично)."""
    return min(GEN_WORKERS_CAP, max(2, cpu_count()))


def effective_max_workers(config: dict) -> int:
    """Одновременные фарм-воркеры (семерфов пула/монитора)."""
    configured = _configured(config, "max_workers")
    if configured > 0:
        return configured
    return auto_max_workers()


def effective_gen_workers(config: dict) -> int:
    """Потоки генерации/вывода ключей. Явный `threading.gen_workers` — иначе авто."""
    configured = _configured(config, "gen_workers")
    if configured > 0:
        return configured
    return auto_gen_workers()


def effective_rpc_threads(config: dict) -> int:
    """Потоки RPC-исполнителя (I/O-bound).

    Явный `threading.rpc_threads` — приоритет. Иначе масштабируем по фарм-
    воркерам (max_workers * RPC_IO_FACTOR), а если и их нет — по ядрам CPU.
    Всегда минимум 4 (нельзя задушить даже слабую машину).
    """
    configured = _configured(config, "rpc_threads")
    if configured > 0:
        return configured
    max_workers = _configured(config, "max_workers")
    if max_workers > 0:
        return max(4, min(max_workers * RPC_IO_FACTOR, RPC_THREAD_CAP))
    return auto_rpc_threads()


def effective_chunk_size(config: dict) -> int:
    """Батч кошельков на чанк пула (кратно воркерам, минимум 100)."""
    configured = _configured(config, "chunk_size")
    if configured > 0:
        return max(configured, 1)
    max_workers = effective_max_workers(config)
    return max(max_workers * CHUNK_FACTOR, 100)


def summarize(config: dict) -> dict:
    """Сводка для диагностики/шапки запуска, включая пометку авто/явно."""
    rpc_set = _configured(config, "rpc_threads")
    gen_set = _configured(config, "gen_workers")
    chunk_set = _configured(config, "chunk_size")
    mw_set = _configured(config, "max_workers")
    return {
        "cpu_cores": cpu_count(),
        "max_workers": effective_max_workers(config),
        "rpc_threads": effective_rpc_threads(config),
        "gen_workers": effective_gen_workers(config),
        "chunk_size": effective_chunk_size(config),
        "max_workers_auto": mw_set <= 0,
        "rpc_threads_auto": rpc_set <= 0,
        "gen_workers_auto": gen_set <= 0,
        "chunk_size_auto": chunk_set <= 0,
    }
