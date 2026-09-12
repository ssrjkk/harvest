"""Асинхронный пул фарминга для всех кошельков.

Инфраструктура (network, vibevibe, db, faucet, executor) создаётся один раз
и используется всеми Farmer'ами. Ограничение параллелизма — через semaphore.

Chunked processing: при тысячах кошельков обработка батчами по chunk_size
позволяет отслеживать прогресс и не держать все результаты в памяти одновременно.

Crash recovery: состояние цикла сохраняется в state.json. При restart'е
продолжаем с того места, где остановились (последний обработанный чанк).
"""

import asyncio
import json
import logging
import random
import time
from pathlib import Path

from core.actions import ActionExecutor
from core.behavior import WalletProfile, profile_for
from core.database import Database
from core.farmer import Farmer
from core.faucet import Faucet
from core.network import NetworkManager
from core.performance import effective_chunk_size, effective_max_workers
from core.utils import restrict_file_permissions
from core.vibevibe import VibeVibeInterface
from core.workpool import WorkerPool

logger = logging.getLogger(__name__)

_STATE_FILE = "state.json"


class _CycleState:
    """Состояние текущего цикла для crash recovery."""

    def __init__(self, state_path: str = _STATE_FILE) -> None:
        self._path = Path(state_path)
        self.cycle_number: int = 0
        self.processed_addresses: set[str] = set()
        self.started_at: float = 0.0  # time.time() — wall clock для crash recovery
        # Конкурентные mark_done (чанк обрабатывается параллельно) могут вызвать
        # несколько save() разом. Лок гарантирует порядок замен файла: последняя
        # запись всегда содержит самое свежее состояние (иначе старая перезапишет
        # новую и при крэше потеряются только что отмеченные адреса).
        self._save_lock = asyncio.Lock()

    async def load(self) -> bool:
        """Загружает состояние из файла. Возвращает True если есть saved state."""
        if not self._path.exists():
            return False
        try:

            def _read():
                return json.loads(self._path.read_text(encoding="utf-8"))

            data = await asyncio.to_thread(_read)
            self.cycle_number = data.get("cycle_number", 0)
            self.processed_addresses = set(data.get("processed_addresses", []))
            self.started_at = data.get("started_at", 0.0)
            if self.processed_addresses:
                logger.info(f"Crash recovery: цикл {self.cycle_number}, {len(self.processed_addresses)} уже обработано")
            return bool(self.processed_addresses)
        except Exception:
            return False

    async def save(self) -> None:
        """Сохраняет состояние в файл. При ошибке — логируем, не крашим цикл."""
        data = {
            "cycle_number": self.cycle_number,
            "processed_addresses": list(self.processed_addresses),
            "started_at": self.started_at,
        }

        def _write() -> None:
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            # Данные не секретны (адреса), но режим 0600 закрывает файл
            # для других локальных пользователей ещё до replace.
            restrict_file_permissions(str(tmp))
            tmp.replace(self._path)

        async with self._save_lock:
            try:
                # Файл на масштабе может быть большим — пишем не блокируя event loop
                await asyncio.to_thread(_write)
            except OSError as e:
                logger.warning(f"Crash state save failed: {e}")

    async def mark_done(self, address: str) -> None:
        """Отмечает адрес как обработанный."""
        self.processed_addresses.add(address)
        # Автосохранение каждые 50 адресов
        if len(self.processed_addresses) % 50 == 0:
            await self.save()

    async def clear(self) -> None:
        """Очищает состояние (конец цикла)."""
        self.processed_addresses.clear()
        self.cycle_number = 0
        self.started_at = 0.0
        if self._path.exists():

            def _unlink() -> None:
                try:
                    self._path.unlink()
                except OSError as e:
                    logger.warning(f"Crash state clear failed: {e}")

            try:
                await asyncio.to_thread(_unlink)
            except Exception:
                pass


class FarmerPool:
    def __init__(self, config: dict, db: Database, network: NetworkManager | None = None) -> None:
        self.config = config
        self.db = db
        # Внешний network может быть передан из auto-режима (общий для крана и пула)
        self.network = network if network is not None else NetworkManager(config, db)
        self.vibevibe = VibeVibeInterface(self.network, config)
        self.faucet = Faucet(config)
        self.executor = ActionExecutor(self.network, self.vibevibe, db, config)
        self.max_workers = effective_max_workers(config)
        # Динамическая конкуренция: каждый чанк пересчитывает активный размер
        # по здоровью RPC (concurrency_factor). Значение до последнего чанка
        # отдаём в live_stats, чтобы было видно, как пул сам себя тормозит/разгоняет.
        self._last_workers = self.max_workers
        self._health_factor = 1.0
        self.timeout = config["threading"].get("timeout_per_wallet", 0)
        if not self.timeout or self.timeout <= 0:
            farm = config["farming"]
            max_actions = (
                max(farm["actions_per_cycle"]) if isinstance(farm.get("actions_per_cycle"), (list, tuple)) else 8
            )
            max_delay = (
                max(farm["delay_between_actions"])
                if isinstance(farm.get("delay_between_actions"), (list, tuple))
                else 20
            )
            # Профили поведения дают всплески действий и растянутые задержки:
            # закладываем запас по burst/scale, чтобы не ловить ложные таймауты.
            beh = config.get("behavior") or {}
            if beh.get("enabled", True):
                burst = max(1.0, float(beh.get("burst_multiplier", 1.6)))
                scale_hi = 1.0
                raw_scale = beh.get("action_delay_scale", (1.0, 1.5))
                if isinstance(raw_scale, (list, tuple)) and len(raw_scale) == 2:
                    scale_hi = max(1.0, float(raw_scale[1]))
                max_actions_eff = max_actions * max(2.0, burst)
                max_delay_eff = max_delay * scale_hi * 1.2
            else:
                max_actions_eff = max_actions
                max_delay_eff = max_delay
            self.timeout = int(max_actions_eff * (max_delay_eff + 10) + 30)
        self.chunk_size = effective_chunk_size(config)
        # state.json привязываем к файлу БД, а не к CWD: два конфига/процесса
        # в одной папке не должны перетирать crash-состояние друг друга.
        state_file = config.get("state_file") or f"{config['database'].get('path', 'farming_state.db')}.state.json"
        self._state = _CycleState(state_file)
        self.executor.writer.start()
        # Пауза пула (горячая клавиша P в live-консоли): по умолчанию активна.
        self._pause_event = asyncio.Event()
        self._pause_event.set()
        # Live-метрики (для консоли): ошибки/действия/обработано.
        self.error_count = 0
        self.action_count = 0
        self.processed_count = 0
        self.cycle_count = 0
        self._faucet_validated = False
        self._closed = False
        # Индивидуальные профили поведения (анти-сибил): детерминированно от адреса.
        self._profiles: dict[str, WalletProfile] = {}
        self._rest_until: dict[str, int] = {}
        self._cycle_seq = 0

    def _profile(self, address: str) -> WalletProfile:
        profile = self._profiles.get(address)
        if profile is None:
            profile = profile_for(address, self.config)
            self._profiles[address] = profile
        return profile

    @property
    def paused(self) -> bool:
        return not self._pause_event.is_set()

    def pause(self) -> None:
        self._pause_event.clear()

    def resume(self) -> None:
        self._pause_event.set()

    def live_stats(self) -> dict:
        return {
            "errors": self.error_count,
            "actions": self.action_count,
            "processed": self.processed_count,
            "cycles": self.cycle_count,
            "paused": self.paused,
            "dropped": getattr(self.executor.writer, "dropped_count", 0),
            "dyn_workers": self._last_workers,
            "health": round(self._health_factor, 2),
        }

    async def _run_one(
        self,
        wallet: dict,
        all_addresses: list,
        cycle_number: int = 0,
    ) -> tuple[str, int]:
        address = wallet["address"]
        # «Отдых» по профилю: кошелёк пропускает несколько циклов подряд.
        if cycle_number > 0 and cycle_number <= self._rest_until.get(address, 0):
            logger.debug(f"  {address[:10]} отдыхает (профиль)")
            return address, 0
        profile = self._profile(address)
        if not profile.neutral and profile.rest_prob > 0 and random.random() < profile.rest_prob:
            n = random.randint(*profile.rest_cycles)
            self._rest_until[address] = cycle_number + n
            logger.info(f"  {address[:10]} ушёл на отдых ({n} цикл(ов), профиль)")
            return address, 0
        # Staggered-вход: профиль задаёт задержку старта кошелька, чтобы pool
        # «расползался вширь», а не стартовал всем фронтом и не ломал ноду.
        if profile.start_delay > 0:
            await asyncio.sleep(profile.start_delay)
        farmer = Farmer(
            wallet,
            self.config,
            self.network,
            self.executor,
            self.faucet,
            all_addresses,
            profile=profile,
        )
        addr = address[:10]
        try:
            result = await asyncio.wait_for(farmer.run_cycle(), timeout=self.timeout)
            if result > 0:
                logger.info(f"  {addr} +{result} действий")
            return address, result
        except TimeoutError:
            logger.error(f"Таймаут для кошелька {addr}")
            self.error_count += 1
            return address, 0
        except Exception as e:
            logger.error(f"Ошибка в кошельке {addr}: {e}")
            self.error_count += 1
            return address, 0

    async def _run_chunk_with_pool(
        self,
        worker_pool: WorkerPool,
        chunk: list[dict],
        all_addresses: list,
        cycle_number: int,
    ) -> dict[str, int]:
        """Один чанк кошельков уходит в очередь воркер-пула целиком.

        Воркеры вытягивают их по одному со staggered-стартом (спят start_delay
        из профиля перед обработкой) и динамическим масштабированием по здоровью
        RPC. Ждём полного исчерпания очереди и возвращаем {address: done_actions}.
        """
        for wallet in chunk:
            await worker_pool.submit(wallet, all_addresses, cycle_number)
        await worker_pool.join()
        return {w["address"]: worker_pool.results.get(w["address"], 0) for w in chunk}

    async def run_once(
        self,
        wallets: list,
        all_addresses: list,
        cycle_number: int = 0,
        stop_event: asyncio.Event | None = None,
    ) -> list[tuple[str, int]]:
        t0 = time.monotonic()
        total = len(wallets)
        final: list[tuple[str, int]] = []
        # Сквозной номер цикла для профилей «отдыха» (в одиночном прогоне cycle_number=0).
        self._cycle_seq += 1
        effective_cycle = cycle_number if cycle_number > 0 else self._cycle_seq

        # Кран: один раз проверим живые стратегии, чтобы воркеры не долбили мёртвые.
        if not self._faucet_validated:
            live = await self.faucet.validate()
            self._faucet_validated = True
            if live <= 0:
                logger.warning("Кран: ни одна стратегия недоступна — фарм продолжится без пополнения")

        # Crash recovery: загружаем состояние и фильтруем уже обработанные.
        # Если номер цикла не совпадает (файл от другого запуска/конфига) —
        # сбрасываем, чтобы его адреса не смешались с текущим циклом.
        restored = await self._state.load()
        if restored and self._state.cycle_number == cycle_number:
            done_in_cycle = self._state.processed_addresses
            wallets = [w for w in wallets if w["address"] not in done_in_cycle]
            skipped = total - len(wallets)
            if skipped > 0:
                logger.info(f"Crash recovery: пропускаю {skipped} уже обработанных")
            if not wallets:
                logger.info("Все кошельки уже обработаны в этом цикле")
                await self._state.clear()
                return final
        elif restored:
            logger.warning("Crash state: несовпадение номера цикла, сбрасываю")
            await self._state.clear()

        self._state.cycle_number = cycle_number
        self._state.started_at = time.time()
        completed = True

        # Один воркер-пул на весь прогон: живёт между чанками, монитор
        # подстраивает число воркеров под здоровье RPC во время работы.
        # Фоновый монитор RPC (пинг нод) тоже начинается здесь: health_fn
        # (concurrency_factor) и ротация на лучшую ноду работают сразу.
        self.network.start_monitor()
        worker_pool = WorkerPool(
            max_workers=self.max_workers,
            worker_func=self._run_one,
            health_fn=self.network.concurrency_factor,
        )
        await worker_pool.start()

        try:
            for chunk_start in range(0, len(wallets), self.chunk_size):
                if stop_event is not None and stop_event.is_set():
                    logger.info("Остановка по запросу — прерываю обработку")
                    completed = False
                    break
                # Пауза пула (P) — ждём возобновления, не нагружая RPC
                while self.paused and not (stop_event is not None and stop_event.is_set()):
                    await asyncio.sleep(0.2)
                if stop_event is not None and stop_event.is_set():
                    logger.info("Остановка по запросу во время паузы — прерываю обработку")
                    completed = False
                    break
                chunk = wallets[chunk_start : chunk_start + self.chunk_size]
                # Воркер-пул: кошельки идут в очередь, воркеры вытягивают их по
                # одному. Вход плавный (staggered start из профиля), приоритеты
                # (кран/send срочнее) поддерживаются, число воркеров подстраивается
                # под здоровье RPC — вместо дружного старта всего чанка сразу.
                results = await self._run_chunk_with_pool(worker_pool, chunk, all_addresses, effective_cycle)
                self._health_factor = worker_pool.last_health
                self._last_workers = worker_pool.current_workers
                marked_this_chunk = 0
                for w in chunk:
                    r = results.get(w["address"], 0)
                    final.append((w["address"], r))
                    self.action_count += r
                    # Crash recovery: отмечаем только адреса, реально прошедшие цикл.
                    # Неуспех (0 действий) — кошелёк может восстановиться.
                    if r > 0:
                        await self._state.mark_done(w["address"])
                        marked_this_chunk += 1
                    self.processed_count += 1
                # Чанковая точка crash-state: закрывает окно между автосейвами
                # mark_done (каждые 50) — при жёстком убийстве процесса теряется
                # максимум кусок последнего незаконченного чанка.
                if marked_this_chunk:
                    await self._state.save()
                done = min(chunk_start + self.chunk_size, len(wallets))
                if done < len(wallets) and not (stop_event is not None and stop_event.is_set()):
                    logger.info(f"  Пул: {done}/{len(wallets)} кошельков обработано...")
        except BaseException:
            # Отмена (Ctrl+C) или исключение внутри чанка: сохраняем прогресс,
            # чтобы crash recovery при следующем запуске продолжил с этого места,
            # а не откатил уже обработанные адреса после последнего автосейва.
            completed = False
            try:
                await self._state.save()
            except Exception:
                pass
            # Сбрасываем журнал действий при любом выходе через исключение:
            # иначе записи batchwriter'а (флаш раз в 2 сек) теряются.
            try:
                await self.executor.flush()
            except Exception as e:
                logger.warning(f"Не удалось сбросить журнал действий при прерывании: {e}")
            raise
        finally:
            # Останавливаем воркер-пул: текущие задачи докручиваются (nonce не рвём),
            # простаивающие воркеры завершаются. Никогда не маскируем оригинальную
            # причину (исключение/отмену) ошибкой стопа.
            try:
                await worker_pool.stop()
            except Exception as e:
                logger.warning(f"Стоп воркер-пула: {e}")

        # Сохраняем финальное (или прерванное остановкой) состояние
        await self._state.save()

        elapsed = time.monotonic() - t0
        success_total = sum(r[1] for r in final)
        processed = len(wallets)
        logger.info(f"Пул завершён за {elapsed:.1f} сек. Успешных: {success_total}/{processed}")
        await self.executor.flush()
        if completed:
            # Весь список пройден — цикл завершён, состояние можно стереть.
            await self._state.clear()
            # Журнал циклов: пишем только полные циклы (обрыв — отдельная история).
            try:
                keep = self.config.get("advanced", {}).get("cycle_history_keep", 2000)
                rpc_m = self.network.take_metrics()
                await self.db.record_cycle(
                    mode="cycle",
                    duration_s=elapsed,
                    wallets=processed,
                    wallets_ok=sum(1 for _, v in final if v > 0),
                    actions_ok=success_total,
                    errors=self.error_count,
                    rpc_url=self.network.rpc_url,
                    rpc_calls=rpc_m.get("calls", 0),
                    rpc_errors=rpc_m.get("errors", 0),
                    rpc_latency_ms=int(rpc_m.get("avg_latency_ms", 0)),
                    keep=keep,
                )
            except Exception as e:
                logger.warning(f"Не удалось записать историю цикла: {e}")
        else:
            # Прерывание (stop) — НЕ стираем: иначе прогресс потеряется при рестарте.
            # Обработанные адреса в state.json, следующий запуск продолжит с них.
            logger.info(
                f"Цикл прерван на {len(self._state.processed_addresses)} адресах — "
                "crash-state сохранён, продолжение при следующем запуске"
            )
        # Очищаем кэш приватных ключей после цикла (не держим ключи в памяти на масштабе)
        self.network.clear_accounts_cache()
        return final

    async def close(self) -> None:
        # Владельцы ресурса разные (run_forever и экран живого фарма), держим close идемпотентным.
        if self._closed:
            return
        self._closed = True
        try:
            await self.executor.writer.stop()
        except Exception:
            pass
        try:
            await self.faucet.close()
        except Exception:
            pass
        await self.network.close()

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        min_d, max_d = self.config["farming"]["delay_between_cycles"]
        cycle_count = 0
        try:
            while not stop_event.is_set():
                cycle_count += 1
                self.cycle_count = cycle_count
                logger.info(f"{'=' * 50}")
                logger.info(f"=== Цикл {cycle_count} ===")
                wallets = await self.db.get_all_wallets()
                if not wallets:
                    logger.warning("Нет кошельков. Создайте их в меню (пункт 1).")
                    await self._interruptible_sleep(60, stop_event)
                    continue
                all_addresses = [w["address"] for w in wallets]
                await self.run_once(
                    wallets,
                    all_addresses,
                    cycle_number=cycle_count,
                    stop_event=stop_event,
                )
                # run_once уже очистил кэш ключей; здесь логируем RPC-метрики.
                self.network.log_diagnostics()
                prune_every = self.config["threading"].get("prune_log_every_cycles", 0)
                if prune_every and cycle_count % prune_every == 0:
                    try:
                        keep = self.config["database"].get("log_keep", 200000)
                        await self.db.prune_actions_log(keep_latest=keep)
                    except Exception as e:
                        logger.error(f"Ошибка prune лога: {e}")
                wait_time = random.uniform(min_d, max_d)
                logger.info(f"Ожидание {wait_time:.0f} сек до следующего цикла...")
                await self._interruptible_sleep(wait_time, stop_event)
        finally:
            await self.close()

    async def _interruptible_sleep(self, seconds: float, stop_event: asyncio.Event) -> None:
        elapsed = 0.0
        step = 2.0
        while elapsed < seconds and not stop_event.is_set():
            while self.paused and not stop_event.is_set():
                await asyncio.sleep(0.2)
            await asyncio.sleep(min(step, seconds - elapsed))
            elapsed += min(step, seconds - elapsed)
