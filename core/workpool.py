"""Воркер-пул с очередью задач (Producer-Consumer).

Замена нижнего уровня фарминга: вместо `asyncio.gather` (все кошельки чанка
стартуют сразу, ждём самого медленного) — фиксированный пул корутин-воркеров,
которые вытягивают задачи из asyncio.PriorityQueue. Это даёт:
  - плавный (не-сибил) вход кошельков через staggered start из профиля;
  - локальный backpressure: скорость диктуют воркеры, а не длина чанка;
  - приоритеты (кран/send срочнее обычного фарма);
  - динамический скейлинг воркеров по здоровью RPC (health_fn).

Контракт worker_func:
    async def worker_func(wallet: dict, all_addresses: list, cycle_number: int) -> tuple[str, int]
Возвращает (address, count_actions) — так пул однозначно мэпит результат на чанк.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

_GET_TIMEOUT = 0.05
_MONITOR_INTERVAL = 5.0
_HEALTH_MIN = 0.15
_HEALTH_MAX = 1.0


class WorkerPool:
    """Очередь задач + пул воркеров с динамическим скейлингом под здоровьем RPC."""

    def __init__(
        self,
        max_workers: int,
        worker_func: Callable[[dict, list, int], Awaitable[tuple[str, int]]],
        health_fn: Callable[[], float] | None = None,
        monitor_interval: float = _MONITOR_INTERVAL,
    ) -> None:
        """
        Args:
            max_workers: потолок числа воркеров (динамический скейлинг не выше него).
            worker_func: корутина обработки одного кошелька (см. контракт в docstring).
            health_fn: возвращает 0.15..1.0 — во сколько раз масштабировать пул.
            monitor_interval: как часто пересчитывать число воркеров (сек).
        """
        self.max_workers = max(1, int(max_workers))
        self.worker_func = worker_func
        self.health_fn = health_fn or (lambda: _HEALTH_MAX)

        # PriorityQueue: (priority, seq, (wallet, all_addresses, cycle_number)).
        # seq делает кортежи сравнимыми и FIFO-устойчивыми при одинаковом приоритете.
        self._queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._seq = 0
        # results keyed by address: порядок завершения воркеров не важен,
        # пул мэпит по адресу (как с gather, но без гонок порядка).
        self.results: dict[str, int] = {}

        self._workers: set[asyncio.Task] = set()
        # «На пенсию»: воркеры, которых монитор вывел из пула. Проверяется после
        # завершения текущей задачи (или очередного таймаута queue.get, когда
        # воркер снова свободен) — никогда не рвём транзакции на полуслове.
        self._retire: set[asyncio.Task] = set()
        self._stopping = False
        self._monitor_task: asyncio.Task | None = None
        self.monitor_interval = max(0.1, float(monitor_interval))
        # Последнее применённое здоровье и достигнутое число воркеров (для live_stats).
        self.last_health: float = _HEALTH_MAX
        self.current_workers: int = 0

    # ---------------- очередь/задачи ----------------

    async def submit(self, wallet: dict, all_addresses: list, cycle_number: int = 0, urgent: bool = False) -> None:
        """Ставит кошелёк в очередь. urgent приоритетнее обычной задачи."""
        self._seq += 1
        priority = 0 if urgent else 10
        await self._queue.put((priority, self._seq, (wallet, all_addresses, cycle_number)))

    async def join(self) -> dict[str, int]:
        """Ждёт, пока очередь опустеет. Возвращает ключ:адрес -> число действий."""
        await self._queue.join()
        return self.results

    # ---------------- воркеры ----------------

    async def _worker_loop(self) -> None:
        """Один воркер: тянет задачи из очереди, обрабатывает, всегда делает task_done."""
        task = asyncio.current_task()
        while not self._stopping and task not in self._retire:
            try:
                _prio, _seq, (wallet, all_addresses, cycle_number) = await asyncio.wait_for(
                    self._queue.get(), timeout=_GET_TIMEOUT
                )
            except TimeoutError:
                continue
            except asyncio.CancelledError:
                raise
            try:
                address, count = await self.worker_func(wallet, all_addresses, cycle_number)
                self.results[address] = count
            except asyncio.CancelledError:
                # Отмена воркера (stop) прилетает извне — воркер не управляет
                # отменой сам, только гасится, когда свободен (см. _retire).
                raise
            except Exception as e:  # noqa: BLE001 — воркер обязан не уронить пул
                logger.error(f"Воркер: ошибка обработки кошелька: {e}")
                address = wallet.get("address", "")
                if address:
                    self.results[address] = 0
            finally:
                self._queue.task_done()

    async def _monitor(self) -> None:
        """Динамический скейлинг: приводим число воркеров к target = max_workers*health.

        Сворачивание пула НЕ рвёт активные транзакции: переросшие воркеры
        помечаются «на пенсию» (_retire) и останавливаются сами после завершения
        текущей задачи (или ближайшего таймаута queue.get, когда снова свободны).
        Никто не отменяет середину подписания/send/ждущего nonce: retire-метка
        читается только в свободной точке воркера, а отмен извне нет вовсе.
        """
        while not self._stopping:
            try:
                factor = self.health_fn()
                if factor is None:
                    factor = _HEALTH_MAX
                factor = max(_HEALTH_MIN, min(_HEALTH_MAX, float(factor)))
                self.last_health = factor
                target = max(1, int(self.max_workers * factor))

                while len(self._workers) < target:
                    task = asyncio.create_task(self._worker_loop())
                    task.add_done_callback(self._on_worker_done)
                    self._workers.add(task)

                # Перерост: увольняем лишних — помечаем «на пенсию», а НЕ cancel.
                while len(self._workers) - len(self._retire) > target:
                    # Выбираем не-пенсионного воркера (кто-то из оставшихся).
                    candidate = next((t for t in self._workers if t not in self._retire), None)
                    if candidate is None:
                        break
                    self._retire.add(candidate)
                self.current_workers = len(self._workers) - len(self._retire)
            except asyncio.CancelledError:
                return
            except Exception as e:  # noqa: BLE001
                logger.error(f"Монитор пула: {e}")
            await asyncio.sleep(self.monitor_interval)

    def _on_worker_done(self, task: asyncio.Task) -> None:
        """Убирает завершившийся воркер из учёта (и из пенсионеров)."""
        self._workers.discard(task)
        self._retire.discard(task)

    async def start(self) -> None:
        """Запускает монитор (он сам создаёт и масштабирует воркеров)."""
        if self._monitor_task is None and not self._stopping:
            self._monitor_task = asyncio.create_task(self._monitor())

    async def stop(self) -> None:
        """Мягкий stop: текущие задачи докручиваются, новые воркеры не создаются.

        Останавливаем пул через _stopping: воркеры завершают текущую задачу
        (докручивают nonce/подпись/отправку), затем выходят из цикла. Активные
        транзакции не отменяются; простаивающие воркеры покидают пул на
        ближайшем таймауте queue.get.
        """
        self._stopping = True
        if self._monitor_task is not None:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
            self._monitor_task = None
        # Простаивающие (на queue.get) завершаются по _stopping сразу; занятые —
        # докручивают текущий кошелёк и выходят после task_done.
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        self._retire.clear()
