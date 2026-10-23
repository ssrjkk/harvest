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
import time
from collections.abc import Awaitable, Callable

from core.config import load_config
from core.crypto import resolve_master_key
from core.database import Database
from core.pool import FarmerPool
from core.scheduler import Scheduler
from core.single_instance import SingleInstance, default_lock_path
from portal.networks import DEFAULT_NETWORK, NETWORKS, network_by_slug

logger = logging.getLogger(__name__)

# Кап создания кошельков за один HTTP-вызов (см. create_wallets).
# Бот/CLI держат до 5000 общим лимитом конструкторов (bot_ops.parse_count).
MAX_WALLETS_PER_REQUEST = 50
# Кап чтения балансов за вызов (витрина топа).
MAX_BALANCES_PER_REQUEST = 50


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
        self._faucet_check_task: asyncio.Task | None = None
        self._last_faucet_report: list[dict] = []
        self._drip_task: asyncio.Task | None = None
        self._last_drip: dict = {"running": False, "at": None, "funded": 0, "failed": 0}
        self._cycle_watch_task: asyncio.Task | None = None
        # Лимит циклов для запланированного прогона (0 = бесконечно).
        self._cycles: int = 0
        self._started_at = time.monotonic()
        self._balance_alerts_task: asyncio.Task | None = None
        self._balance_alerts_enabled: bool = False
        self._balance_threshold: float = 0.001
        # Монитор балансов проверяет первые N кошельков за тик (RPC-шторм
        # 100×get_balance/мин — осознанный кап; остаток без алертов, см. warning
        # в _balance_monitor_loop при усечении).
        self._balance_check_limit: int = 100
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
            if farm_config.endswith(str(n["config"])):
                self.current = str(n["slug"])
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
        # Метрики пишет только recorder (каждые 30 с): иначе пул дублировал бы
        # строки в конце каждого цикла с другой семантикой wallets (см. pool).
        self.pool._external_recorder = True
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
                # Дренаж ограничен abort_pending в чанке (взятые докручиваются,
                # не взятые сбрасываются): висим не дольше самого медленного
                # кошелька, а не всего чанка. HTTP-стоп при этом не блокируется
                # вовсе (fire-and-forget в api_farm_action).
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

    async def start(self, cycles: int = 0) -> bool:
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
            self._cycles = max(0, int(cycles))
            self._task = asyncio.create_task(self._run())
            self._start_cycle_watch()
            self._faucet_check_task = asyncio.get_running_loop().create_task(self._check_faucet_once())
            return True

    async def _run(self) -> None:
        assert self.pool is not None
        try:
            await self.pool.run_forever(self._stop, max_cycles=self._cycles)
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
            await self._stop_cycle_watch()
            if self._faucet_check_task is not None:
                self._faucet_check_task.cancel()
                try:
                    await self._faucet_check_task
                except asyncio.CancelledError:
                    pass
                except Exception:  # noqa: BLE001
                    logger.warning("faucet-check task failed", exc_info=True)
                self._faucet_check_task = None
            return True

    async def _shutdown_pool(self) -> None:
        """Полное закрытие ресурсов пула (необратимо). БД остаётся для статистики."""
        await self._stop_cycle_watch()
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

    def _start_cycle_watch(self) -> None:
        """Сторож завершённых циклов для пушей владельцу (роль уведомлялки)."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._cycle_watch_task is not None and not self._cycle_watch_task.done():
            return
        self._cycle_watch_task = loop.create_task(self._cycle_watch_loop())

    async def _stop_cycle_watch(self) -> None:
        task, self._cycle_watch_task = self._cycle_watch_task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _cycle_watch_loop(self, interval_s: float = 120.0) -> None:
        """Раз в interval_s смотрит новые строки cycle_history и пушит итог.

        Только факты из БД: номер цикла, кошельки, действия, ошибки.
        """
        last_seen: int | None = None
        try:
            while True:
                await asyncio.sleep(interval_s)
                if self.db is None:
                    continue
                try:
                    rows = await self.db.get_cycle_history(1)
                except Exception:  # noqa: BLE001
                    continue
                if not rows:
                    continue
                cur = rows[0]
                try:
                    cid = int(cur.get("id", 0) or 0)
                except (TypeError, ValueError):
                    continue
                if last_seen is None:
                    last_seen = cid
                    continue
                if cid <= last_seen:
                    continue
                last_seen = cid
                acts = int(cur.get("actions_ok") or 0)
                errs = int(cur.get("errors") or 0)
                wals = int(cur.get("wallets") or 0)

                def _f(n: int) -> str:
                    return f"{n:,}".replace(",", " ")

                await self._alert(
                    f"✅ Цикл #{cid} завершён: {_f(acts)} действий "
                    f"({_f(wals)} кошельков), ошибок: {_f(errs)}."
                )
        except asyncio.CancelledError:
            pass

    async def _check_faucet_once(self) -> int:
        """Quota-free pre-flight крана при старте фарма (роль уведомлялки).

        Возвращает число живых стратегий; 0 — пуш владельцу, что пополнения
        не будет и фарм идёт без топлива. Разбор — в self._last_faucet_report.
        """
        try:
            from core.faucet import Faucet

            if not isinstance(self.config, dict):
                self._last_faucet_report = []
                return 0
            faucet = Faucet(self.config)
            try:
                live = await faucet.validate()
                self._last_faucet_report = faucet.probe_report()
            finally:
                try:
                    await faucet.close()
                except Exception:  # noqa: BLE001
                    pass
        except Exception as e:  # noqa: BLE001
            logger.warning("Pre-flight крана не удался: %s", e)
            self._last_faucet_report = []
            return 0
        if live <= 0:
            detail = "; ".join(
                f"{p['type']}: {p['reason']}" for p in self._last_faucet_report
            )
            await self._alert(
                "🚨 Кран недоступен (0 живых стратегий): пополнения не будет, "
                f"фарм идёт без топлива. {detail}"
            )
        return live

    async def faucet_report(self) -> list[dict]:
        """Свежий разбор pre-flight крана (для /faucet и Mini App)."""
        try:
            from core.faucet import Faucet

            if not isinstance(self.config, dict):
                return list(getattr(self, "_last_faucet_report", []) or [])
            faucet = Faucet(self.config)
            try:
                await faucet.validate()
                self._last_faucet_report = faucet.probe_report()
            finally:
                try:
                    await faucet.close()
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            logger.warning("faucet_report не удался", exc_info=True)
        return list(getattr(self, "_last_faucet_report", []) or [])

    def drip_state(self) -> dict:
        """Состояние ручного долива (для UI-виджетов)."""
        d = dict(self._last_drip)
        d["running"] = bool(self._drip_task is not None and not self._drip_task.done())
        return d

    async def drip_faucet(self) -> dict:
        """Ручной долив всех кошельков (кнопка портала/Mini App).

        Single-flight: повторный запуск пока идёт — 409-style ответ
        {"started": False, "running": True}. Использует transient
        NetworkManager (пул не трогаем): faucet не занимает nonce.
        """
        if self.db is None or not isinstance(self.config, dict):
            raise RuntimeError("БД фермы не подключена")
        if self._drip_task is not None and not self._drip_task.done():
            st = self.drip_state()
            st["started"] = False
            return st
        self._last_drip = {"running": True, "at": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
                           "funded": 0, "failed": 0}
        self._drip_task = asyncio.get_running_loop().create_task(self._drip_run())
        st = self.drip_state()
        st["started"] = True
        return st

    async def _drip_run(self) -> None:
        """Фоновая доливка: ensure_balance по всем адресам батчами."""
        try:
            from core.faucet import Faucet
            from core.network import NetworkManager

            assert self.db is not None and isinstance(self.config, dict)
            faucet = Faucet(self.config)
            network = NetworkManager(self.config, self.db)
            try:
                addrs = await self.db.get_all_addresses()
                funded, failed = await faucet.request_batch(
                    addrs, 50, network=network,
                    max_concurrent=faucet.max_concurrent,
                )
            finally:
                try:
                    await faucet.close()
                except Exception:  # noqa: BLE001
                    pass
                try:
                    await network.close()
                except Exception:  # noqa: BLE001
                    pass
            self._last_drip = {"running": False,
                               "at": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
                               "funded": funded, "failed": failed}
            logger.info("[AUDIT] manual drip done funded=%s failed=%s", funded, failed)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning("manual drip упал: %s", e)
            self._last_drip = {"running": False,
                               "at": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
                               "funded": 0, "failed": 0, "error": str(e)[:160]}

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
        if self._balance_alerts_task is not None:
            self._balance_alerts_task.cancel()
            try:
                await self._balance_alerts_task
            except asyncio.CancelledError:
                pass
            self._balance_alerts_task = None
        if self._drip_task is not None and not self._drip_task.done():
            self._drip_task.cancel()
            try:
                await self._drip_task
            except asyncio.CancelledError:
                pass
            self._drip_task = None
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
            "pnl": await self.pnl_snapshot(),
        }

    async def top_wallets(self, limit: int = 8) -> list[dict]:
        if self.db is None:
            return []
        return await self.db.get_top_wallets(limit)

    async def cycle_history(self, limit: int = 12) -> list[dict]:
        if self.db is None:
            return []
        return await self.db.get_cycle_history(limit)

    async def pnl_snapshot(self) -> dict | None:
        """Агрегаты PnL последнего прогона (None — прогона ещё не было)."""
        if self.db is None:
            return None
        # БД может быть старым/заглушкой без метода — тогда просто без PnL,
        # а не падение всего /api/stats.
        getter = getattr(self.db, "get_last_pnl_snapshot", None)
        if getter is None:
            return None
        try:
            return await getter()
        except Exception as e:  # noqa: BLE001
            logger.debug(f"Снапшот PnL не прочитан: {e}")
            return None

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

    async def wallet_cells(self, limit: int = 100) -> tuple[str, int]:
        """Строка iwoer + всего кошельков (общий источник сайта и поля бота).

        Тот же запрос, что publish_dash.collect: последний цикл + первые N.
        """
        if self.db is None:
            return "", 0
        try:
            db = await self.db._connect()
            async with db.execute("SELECT COUNT(*) FROM wallets") as cur:
                total = (await cur.fetchone())[0]
            try:
                async with db.execute(
                    "SELECT id FROM cycle_history ORDER BY id DESC LIMIT 1"
                ) as cur:
                    row = await cur.fetchone()
                latest = row[0] if row else None
                async with db.execute(
                    "SELECT total_actions, total_attempts, last_cycle FROM wallets"
                    " ORDER BY rowid LIMIT ?",
                    (limit,),
                ) as cur:
                    sample = await cur.fetchall()
                from portal.wallet_cells import build_cells

                return (
                    build_cells([(a or 0, t or 0, c) for a, t, c in sample], latest),
                    total,
                )
            except Exception:  # noqa: BLE001 — старая схема: все idle
                return "i" * min(total, limit), total
        except Exception:  # noqa: BLE001
            logger.warning("wallet_cells упало", exc_info=True)
            return "", 0

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
        """Callback для планировщика: запуск фарма на N циклов (0 = без лимита)."""
        if self.running:
            logger.info("Scheduled farm skipped: already running")
            return
        logger.info("Scheduled farm: starting %d cycles", cycles)
        await self.start(cycles=cycles)

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

    async def _addrs_for_balances(self, limit: int = 0) -> list[str]:
        """Адреса без дешифровки ключей; fallback на get_all_wallets для кастомных БД."""
        assert self.db is not None
        try:
            meth = getattr(self.db, "get_all_addresses", None)
            if callable(meth):
                try:
                    addrs = await meth(limit) if limit else await meth()
                except TypeError:
                    # Моки без limit-сигнатуры.
                    addrs = await meth()
                if isinstance(addrs, list):
                    got = [a for a in addrs if isinstance(a, str)]
                    return got[:limit] if limit else got
        except Exception:  # noqa: BLE001
            # get_all_addresses нет/упал — пробуем legacy get_all_wallets.
            pass
        wallets = await self.db.get_all_wallets()
        # Пусть ошибка чтения всплывает в _balance_monitor_loop (лог + retry),
        # а не превращается в молчаливый пустой список.
        out: list[str] = []
        for w in wallets if isinstance(wallets, list) else []:
            a = w.get("address", "") if isinstance(w, dict) else ""
            if a:
                out.append(a)
            if limit and len(out) >= limit:
                break
        return out

    async def _balance_monitor_loop(self) -> None:
        """Фоновый мониторинг балансов кошельков."""
        logger.info("Balance monitor started (threshold=%.6f)", self._balance_threshold)
        while self._balance_alerts_enabled:
            try:
                if self.pool is not None and self.db is not None:
                    # Только адреса: не расшифровываем private_key/mnemonic ради баланса.
                    addrs = await self._addrs_for_balances()
                    if len(addrs) > self._balance_check_limit:
                        logger.warning(
                            "Balance monitor: проверяю первые %d из %d кошельков "
                            "(остаток без алертов)",
                            self._balance_check_limit,
                            len(addrs),
                        )
                    low_balance = []
                    for addr in addrs[: self._balance_check_limit]:
                        if not addr:
                            continue
                        try:
                            bal = await self.pool.network.get_balance(addr)
                            if bal is not None and bal < self._balance_threshold:
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
        """Создать N кошельков (UI/бот). Требуется подключённая БД.

        Кап 50 за вызов — защита HTTP/RPC от тяжёлой генерации. Бот/CLI
        оперируют до 5000 (батчами по 50): см. portal/bot_ops.py parse_count.
        Весь вызов под _mgmt: генерация идёт в executor-потоках (loop свободен),
        а запись — одной серией; между батчами бота лок отпускается, так что
        блокировка управления ограничена одним батчем, а не всем флотом.
        """
        if self.db is None or self.config is None:
            raise RuntimeError("БД фермы не подключена")
        count = max(1, min(int(count), MAX_WALLETS_PER_REQUEST))
        from core.wallet import WalletManager

        async with self._mgmt:
            wm = WalletManager(self.config, self.db)
            return await wm.create_wallets(count)

    async def wallet_balances(self, limit: int = 20) -> list[dict]:
        """Балансы первых N кошельков (для /balance). Ошибка RPC — balance None."""
        if self.db is None or self.pool is None:
            raise RuntimeError("Ферма не запущена")
        addrs = await self._addrs_for_balances(max(1, min(int(limit), MAX_BALANCES_PER_REQUEST)))
        # _addrs_for_balances(limit) уже усекает; на всякий случай режем ещё раз.
        addrs = addrs[: max(1, min(int(limit), MAX_BALANCES_PER_REQUEST))]
        out = []
        for addr in addrs:
            if not addr:
                continue
            try:
                bal = await self.pool.network.get_balance(addr)
            except Exception:  # noqa: BLE001
                bal = None
            out.append({"address": addr, "balance": bal})
        return out

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
