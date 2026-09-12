"""Демон фермы: единый инстанс FarmerPool под управлением сервера.

start() — запускает фоновый цикл run_forever; stop() — штатно останавливает
(ожидаемый цикл прерывается, crash-state сохранится — это поведение ядра).
Важно: pool.close() необратим (останавливает BatchWriter/Network), поэтому
после ШТАТНОГО стопа пул остаётся живым и re-start работает мгновенно;
полное закрытие — только при фатальной ошибке или shutdown сервера.
statistics() собирает живой срез для бота и веба.
"""

from __future__ import annotations

import asyncio
import logging

from core.config import load_config
from core.crypto import resolve_master_key
from core.database import Database
from core.pool import FarmerPool
from core.single_instance import SingleInstance, default_lock_path

logger = logging.getLogger(__name__)


def _db_master_key(override: str | None, config: dict) -> bytes:
    """Ключ шифрования БД для демона фермы.

    override_master_key — это cfg.master_key портала: может быть и настоящим
    64-hex ключом БД, и коротким PIN-паролем веб-входа (валидатор это допускает).
    PIN не может быть ключом БД: bytes.fromhex упадёт с криптической ошибкой,
    а hex-строка неверной длины молча сломает расшифровку всех seed-данных.
    Ключом считаем только 64-hex; во всех остальных случаях читаем master.key,
    игнорируя env (в env лежит тот же PIN).
    """
    if override:
        raw = override[2:] if override.startswith(("0x", "0X")) else override
        try:
            key = bytes.fromhex(raw)
        except ValueError:
            key = b""
        if len(key) == 32:
            return key
        logger.warning(
            "master_key в PORTAL не является ключом БД (нужно 64 hex): он используется "
            "только как пароль веб-входа, ключ БД берётся из database.master_key"
        )
        return resolve_master_key(config, ignore_env=True)
    return resolve_master_key(config)


class FarmDaemon:
    def __init__(self, farm_config: str, db_path: str, master_key: str | None = None) -> None:
        self.farm_config = farm_config
        self.db_path = db_path
        self.override_master_key = master_key or None
        self.config: dict | None = None
        self.db: Database | None = None
        self.pool: FarmerPool | None = None
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        # SingleInstance-лок на время фарма: одна ферма на одной БД. Дашборд/
        # статистика работают и без лока — он нужен только пока крутится run_forever.
        self._guard: SingleInstance | None = None

    async def connect(self) -> None:
        """Инициализация БД и пула. Идемпотентна: повторный вызов безопасен."""
        if self.pool is not None:
            return
        self.config = load_config(self.farm_config)
        if self.config is None:
            raise RuntimeError(f"Конфиг фармера не найден: {self.farm_config}")
        key = _db_master_key(self.override_master_key, self.config)
        if self.db is None:
            self.db = Database(self.db_path, master_key=key)
            await self.db.init()
        self.pool = FarmerPool(self.config, self.db)

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def run_state(self) -> dict:
        return {
            "running": self.running,
            "paused": bool(self.pool and self.pool.paused),
        }

    async def start(self) -> bool:
        if self.running:
            return False
        if self.pool is None or self.pool._closed:
            # run_forever по завершении сам закрывает ресурсы пула (core/pool.py).
            # После стопа строим СВЕЖИЙ пул, иначе следующий фарм упадёт
            # на закрытых Network/BatchWriter.
            self.pool = None
            await self.connect()
        # Одна ферма на одной БД: если другой процесс (main.py/auto.py) уже
        # держит SingleInstance-лок, фарм не запускаем — дашборд продолжает жить.
        guard = SingleInstance(default_lock_path(self.db_path))
        if not guard.acquire():
            logger.warning(
                "Другой экземпляр фармера уже работает на этой БД (%s) — фарм не запущен",
                self.db_path,
            )
            return False
        self._guard = guard
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._run())
        return True

    async def _run(self) -> None:
        assert self.pool is not None
        try:
            await self.pool.run_forever(self._stop)
        except asyncio.CancelledError:
            logger.info("Демон фермы отменён — закрываю пул")
            await self._shutdown_pool()
            raise
        except Exception:  # noqa: BLE001
            logger.exception("Демон фермы упал — закрываю пул")
            await self._shutdown_pool()
        else:
            # Штатное завершение (stop_event): run_forever сам закрыл пул.
            # Закрываем ещё раз (идемпотентно) и снимаем фарм-лок.
            await self._shutdown_pool()

    async def stop(self) -> bool:
        if not self.running:
            return False
        self._stop.set()
        assert self._task is not None
        await self._task
        self._task = None
        return True

    async def _shutdown_pool(self) -> None:
        """Полное закрытие ресурсов пула (необратимо). БД остаётся для статистики."""
        if self.pool is not None:
            try:
                await self.pool.close()
            except Exception:  # noqa: BLE001
                logger.warning("Ошибка при закрытии пула", exc_info=True)
            self.pool = None
        self._release_guard()

    def _release_guard(self) -> None:
        """Снимает SingleInstance-лок фарма (идемпотентно)."""
        guard, self._guard = self._guard, None
        if guard is not None:
            try:
                guard.release()
            except Exception:  # noqa: BLE001
                logger.warning("Ошибка при снятии фарм-лока", exc_info=True)

    async def pause(self) -> bool:
        if self.running and self.pool is not None and not self.pool.paused:
            self.pool.pause()
            return True
        return False

    async def resume(self) -> bool:
        if self.running and self.pool is not None and self.pool.paused:
            self.pool.resume()
            return True
        return False

    async def close(self) -> None:
        if self.running:
            await self.stop()
        await self._shutdown_pool()
        if self.db is not None:
            await self.db.close()
            self.db = None

    async def statistics(self) -> dict:
        """Единый живой срез для бота, веба и мини-аппа."""
        # Путь конфига не отдаём: имя живого конфига — лишняя информация
        # для тех, кто прошёл аутентификацию (не секрет, но и не нужно).
        base = {"running": self.running}
        if self.pool is None:
            return base
        stats = self.pool.live_stats()
        health = self.pool.network.concurrency_factor()
        cycle = {}
        if self.db is not None:
            cycle = await self.db.get_cycle_stats()
        return {
            **base,
            "pool": stats,
            "health_factor": round(health, 2),
            "db": cycle,
        }

    async def top_wallets(self, limit: int = 8) -> list[dict]:
        if self.db is None:
            return []
        return await self.db.get_top_wallets(limit)

    async def cycle_history(self, limit: int = 12) -> list[dict]:
        if self.db is None:
            return []
        return await self.db.get_cycle_history(limit)
