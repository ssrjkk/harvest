"""Демон фермы: единый инстанс FarmerPool под управлением сервера.

start() — запускает фоновый цикл run_forever; stop() — штатно останавливает
(ожидаемый цикл прерывается, crash-state сохранится — это поведение ядра).
Важно: pool.close() необратим (останавливает BatchWriter/Network), поэтому
после ШТАТНОГО стопа пул остаётся живым и re-start работает мгновенно;
полное закрытие — только при фатальной ошибке или shutdown сервера.
statistics() собирает живой срез для бота и веба. export_wallets() отдаёт
все кошельки с расшифрованными ключами (CSV/JSON) — только авторизованным.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
from collections.abc import Awaitable, Callable

from core.config import load_config
from core.crypto import resolve_master_key
from core.database import Database
from core.pool import FarmerPool
from core.scheduler import Scheduler
from core.single_instance import SingleInstance, default_lock_path
from portal.networks import DEFAULT_NETWORK, NETWORKS, network_by_slug

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
    def __init__(
        self,
        farm_config: str,
        db_path: str,
        master_key: str | None = None,
        alert: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self.farm_config = farm_config
        self.db_path = db_path
        self.override_master_key = master_key or None
        self.alert_cb = alert
        self.config: dict | None = None
        self.db: Database | None = None
        self.pool: FarmerPool | None = None
        self.scheduler: Scheduler | None = None
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._balance_alerts_task: asyncio.Task | None = None
        self._balance_alerts_enabled: bool = False
        self._balance_threshold: float = 0.001
        self._metrics_task: asyncio.Task | None = None
        # Сериализатор управления: одновременные start()/stop() из разных хендлеров
        # (двойной клик, rest-api + бот-команда) иначе перезаписывали бы _task и
        # оставляли за спиной утечку второго пула. Все управляющие операции —
        # под одним локом.
        self._mgmt = asyncio.Lock()
        # SingleInstance-лок на время фарма: одна ферма на одной БД. Дашборд/
        # статистика работают и без лока — он нужен только пока крутится run_forever.
        self._guard: SingleInstance | None = None
        # Текущая сеть: определяется по имени конфига (config_*.yaml).
        self.current = DEFAULT_NETWORK
        for n in NETWORKS:
            if farm_config.endswith(n["config"]):
                self.current = n["slug"]
                break

    async def _alert(self, text: str) -> None:
        """Пуш владельцу (Telegram). No-op без колбэка; сбой алерта не роняет демона."""
        if self.alert_cb is None:
            return
        try:
            await self.alert_cb(text)
        except Exception:  # noqa: BLE001
            logger.warning("Не удалось отправить алерт владельцу", exc_info=True)

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
        if self._metrics_task is None:
            self._metrics_task = asyncio.create_task(self._metrics_recorder_loop())
        # Планировщик инициализируется лениво (при первом обращении или старте),
        # чтобы не загружать задачи, если пользователь не использует расписание.

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def networks_info(self) -> list[dict]:
        """Каталог сетей + признак текущей/запущенной (для UI и бота)."""
        out = []
        for n in NETWORKS:
            out.append(
                {
                    **n,
                    "current": n["slug"] == self.current,
                    "running": self.running and n["slug"] == self.current,
                }
            )
        return out

    async def switch_to(self, slug: str) -> bool:
        """Переключает активную сеть (останавливает фарм, меняет конфиг/БД).

        Новую сеть фармить можно через start() — данные каждой сети в своей БД.
        """
        n = network_by_slug(slug)
        if n is None:
            return False
        async with self._mgmt:
            if self.running:
                self._stop.set()
                assert self._task is not None
                await self._task
                self._task = None
            await self._shutdown_pool()
            if self.db is not None:
                try:
                    await self.db.close()
                except Exception:  # noqa: BLE001
                    pass
                self.db = None
            self.config = None
            self.farm_config = n["config"]
            self.db_path = n["db"]
            self.current = slug
            return True

    def run_state(self) -> dict:
        return {
            "running": self.running,
            "paused": bool(self.pool and self.pool.paused),
        }

    async def start(self) -> bool:
        async with self._mgmt:
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
                await self._alert("⚠️ Ферма не запустилась: другой экземпляр уже работает на этой БД.")
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
        except Exception as exc:  # noqa: BLE001
            logger.exception("Демон фермы упал — закрываю пул")
            await self._alert(
                f"⚠️ Ферма упала: {type(exc).__name__}: {str(exc)[:200]}\n/doctor — диагностика"
            )
            await self._shutdown_pool()
        else:
            # Штатное завершение (stop_event): run_forever сам закрыл пул.
            # Закрываем ещё раз (идемпотентно) и снимаем фарм-лок.
            await self._shutdown_pool()

    async def stop(self) -> bool:
        async with self._mgmt:
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
        async with self._mgmt:
            if self.running and self.pool is not None and not self.pool.paused:
                self.pool.pause()
                return True
            return False

    async def resume(self) -> bool:
        async with self._mgmt:
            if self.running and self.pool is not None and self.pool.paused:
                self.pool.resume()
                return True
            return False

    async def close(self) -> None:
        if self.running:
            await self.stop()
        if self._metrics_task is not None:
            self._metrics_task.cancel()
            try:
                await self._metrics_task
            except asyncio.CancelledError:
                pass
            self._metrics_task = None
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
            "network": self.current,
            "pool": stats,
            "health_factor": round(health, 2),
            "db": cycle,
            "wallet_count": await self.wallet_count(),
        }

    async def top_wallets(self, limit: int = 8) -> list[dict]:
        if self.db is None:
            return []
        return await self.db.get_top_wallets(limit)

    async def cycle_history(self, limit: int = 12) -> list[dict]:
        if self.db is None:
            return []
        return await self.db.get_cycle_history(limit)

    async def wallet_count(self) -> int:
        """Общее число строк кошельков в БД (включая нерасшифрованные)."""
        if self.db is None:
            return 0
        return await self.db.count_wallets()

    async def all_wallets(self) -> list[dict]:
        """Все кошельки с расшифрованными ключами и сид-фразами (для бота/UI)."""
        if self.db is None:
            return []
        return await self.db.get_all_wallets()

    async def rpc_health(self) -> dict:
        """Диагностика RPC: задержки, ошибки, circuit breaker (для UI)."""
        if self.pool is None:
            return {"error": "pool not initialized"}
        return self.pool.network.diagnostics()

    async def list_groups(self) -> list[dict]:
        """Все группы кошельков с количеством адресов."""
        if self.db is None:
            return []
        gm = self.db.get_group_manager()
        db_conn = await self.db._connect()
        gm.set_db(db_conn)
        return await gm.get_all_groups()

    async def create_group(self, name: str, color: str = "#007bff") -> dict:
        """Создать новую группу кошельков."""
        if self.db is None:
            raise RuntimeError("DB not connected")
        gm = self.db.get_group_manager()
        db_conn = await self.db._connect()
        gm.set_db(db_conn)
        return await gm.create_group(name, color)

    async def delete_group(self, group_id: int) -> bool:
        """Удалить группу кошельков."""
        if self.db is None:
            return False
        gm = self.db.get_group_manager()
        db_conn = await self.db._connect()
        gm.set_db(db_conn)
        return await gm.delete_group(group_id)

    async def add_wallet_to_group(self, address: str, group_id: int) -> bool:
        """Добавить кошелёк в группу."""
        if self.db is None:
            return False
        gm = self.db.get_group_manager()
        db_conn = await self.db._connect()
        gm.set_db(db_conn)
        return await gm.add_wallet_to_group(address, group_id)

    async def remove_wallet_from_group(self, address: str, group_id: int) -> bool:
        """Удалить кошелёк из группы."""
        if self.db is None:
            return False
        gm = self.db.get_group_manager()
        db_conn = await self.db._connect()
        gm.set_db(db_conn)
        return await gm.remove_wallet_from_group(address, group_id)

    async def init_scheduler(self) -> None:
        """Инициализирует планировщик (загрузка задач, установка callback)."""
        if self.scheduler is not None:
            return
        self.scheduler = Scheduler()
        await self.scheduler.load()
        self.scheduler.set_run_callback(self._scheduled_farm)
        await self.scheduler.start()

    async def _scheduled_farm(self, cycles: int) -> None:
        """Callback для планировщика: запуск фарма на N циклов."""
        if self.running:
            logger.info("Scheduled farm skipped: already running")
            return
        logger.info("Scheduled farm: starting %d cycles", cycles)
        await self.start()

    def scheduler_state(self) -> dict:
        """Состояние планировщика для UI."""
        if self.scheduler is None:
            return {"initialized": False, "running": False, "tasks": []}
        return {
            "initialized": True,
            "running": self.scheduler.is_running,
            "tasks": self.scheduler.get_all_tasks(),
        }

    async def add_scheduler_task(self, task_id: str, **kwargs) -> dict:
        """Добавить задачу в планировщик."""
        if self.scheduler is None:
            await self.init_scheduler()
        assert self.scheduler is not None
        task = self.scheduler.add_task(task_id, **kwargs)
        return task.to_dict()

    async def update_scheduler_task(self, task_id: str, **kwargs) -> dict | None:
        """Обновить задачу планировщика."""
        if self.scheduler is None:
            return None
        task = self.scheduler.update_task(task_id, **kwargs)
        return task.to_dict() if task else None

    async def remove_scheduler_task(self, task_id: str) -> bool:
        """Удалить задачу из планировщика."""
        if self.scheduler is None:
            return False
        return self.scheduler.remove_task(task_id)

    async def start_scheduler(self) -> bool:
        """Запустить планировщик."""
        if self.scheduler is None:
            await self.init_scheduler()
        assert self.scheduler is not None
        await self.scheduler.start()
        return True

    async def stop_scheduler(self) -> bool:
        """Остановить планировщик."""
        if self.scheduler is None:
            return False
        await self.scheduler.stop()
        return True

    def balance_alerts_state(self) -> dict:
        """Состояние мониторинга балансов."""
        return {
            "enabled": self._balance_alerts_enabled,
            "threshold": self._balance_threshold,
        }

    async def set_balance_alerts(self, enabled: bool, threshold: float) -> dict:
        """Включить/выключить мониторинг балансов."""
        self._balance_alerts_enabled = enabled
        self._balance_threshold = max(0.0, threshold)
        if enabled and self._balance_alerts_task is None:
            self._balance_alerts_task = asyncio.create_task(self._balance_monitor_loop())
        elif not enabled and self._balance_alerts_task is not None:
            self._balance_alerts_task.cancel()
            try:
                await self._balance_alerts_task
            except asyncio.CancelledError:
                pass
            self._balance_alerts_task = None
        return {"enabled": self._balance_alerts_enabled, "threshold": self._balance_threshold}

    async def _balance_monitor_loop(self) -> None:
        """Фоновый мониторинг балансов кошельков."""
        logger.info("Balance monitor started (threshold=%.6f)", self._balance_threshold)
        while self._balance_alerts_enabled:
            try:
                if self.pool is not None and self.db is not None:
                    wallets = await self.db.get_all_wallets()
                    low_balance = []
                    for w in wallets[:100]:
                        addr = w.get("address", "")
                        if not addr:
                            continue
                        try:
                            bal = await self.pool.network.get_balance(addr)
                            if bal < self._balance_threshold:
                                low_balance.append({"address": addr, "balance": bal})
                        except Exception:
                            pass
                    if low_balance:
                        logger.warning(
                            "Balance alert: %d wallets below %.6f",
                            len(low_balance),
                            self._balance_threshold,
                        )
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Balance monitor error: %s", e)
                await asyncio.sleep(30)
        logger.info("Balance monitor stopped")

    async def _metrics_recorder_loop(self) -> None:
        """Фоновая запись снимков метрик для временного ряда (дашборд)."""
        logger.info("Metrics recorder started")
        while not self._stop.is_set():
            try:
                if self.pool is not None and self.db is not None:
                    stats = self.pool.live_stats()
                    health = self.pool.network.concurrency_factor()
                    await self.db.record_metrics_snapshot(
                        actions=int(stats.get("actions", 0)),
                        errors=int(stats.get("errors", 0)),
                        processed=int(stats.get("processed", 0)),
                        cycles=int(stats.get("cycles", 0)),
                        wallets=await self.wallet_count(),
                        dyn_workers=float(stats.get("dyn_workers", 0)),
                        health_factor=float(health),
                    )
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Metrics recorder error: %s", e)
                await asyncio.sleep(15)
        logger.info("Metrics recorder stopped")

    async def metrics_history(self, limit: int = 100) -> list[dict]:
        """Временной ряд метрик для дашборда."""
        if self.db is None:
            return []
        return await self.db.get_metrics_history(limit)

    async def create_wallets(self, count: int) -> list[dict]:
        """Создать N кошельков (UI/бот). Требуется подключённая БД."""
        if self.db is None or self.config is None:
            raise RuntimeError("БД фермы не подключена")
        count = max(1, min(int(count), 50))
        from core.wallet import WalletManager

        async with self._mgmt:
            wm = WalletManager(self.config, self.db)
            return await wm.create_wallets(count)

    async def export_wallets(self, fmt: str = "csv") -> tuple[str, str]:
        """Экспорт ВСЕХ кошельков с расшифрованными ключами для скачивания/бота.

        Возвращает (содержимое, имя_файла). fmt: "csv" (utf-8-sig) или "json".
        Ключи дешифруются ключом БД — вызов доступен только авторизованным
        владельцам (проверка на уровне HTTP-хендлера).
        """
        if self.db is None:
            raise RuntimeError("БД фермы не подключена")
        wallets = await self.db.get_all_wallets()
        if fmt == "json":
            data = json.dumps(wallets, indent=2, ensure_ascii=False)
            return data, "wallets.json"
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["address", "private_key", "mnemonic", "actions"])
        for w in wallets:
            writer.writerow(
                [
                    _csv_safe(w.get("address", "")),
                    _csv_safe(w.get("private_key", "")),
                    _csv_safe(w.get("mnemonic", "")),
                    _csv_safe(w.get("total_actions", 0)),
                ]
            )
        return buf.getvalue(), "wallets.csv"


def _csv_safe(value) -> str:
    """Нейтрализует формульные инъекции (Excel/Sheets DDE) в CSV-экспорте."""
    s = str(value)
    if s[:1] in {"=", "+", "-", "@", "\t", "\r"}:
        return "'" + s
    return s
