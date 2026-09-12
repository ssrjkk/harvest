"""Буферизованная запись в БД для масштаба.

При сотнях/тысячах кошельков поштучные commits на общий SQLite-коннекшн
становятся узким местом. Этот буфер копит строки логов/инкрементов и
сбрасывает их батчами (executemany) — резко снижая число транзакций.
"""

import asyncio
import logging
import time

logger = logging.getLogger(__name__)


class BatchWriter:
    def __init__(
        self,
        db,
        flush_every: float = 2.0,
        max_buffer: int = 500,
        flush_on_action: bool = False,
    ) -> None:
        self.db = db
        self.flush_every = flush_every
        self.max_buffer = max_buffer
        self._flush_on_action = flush_on_action
        self._actions: list[tuple] = []
        self._health_updates: dict[str, tuple[int, int]] = {}
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        # Счётчик подряд идущих сбоев записи: при устойчивом отказе БД
        # буфер не должен расти бесконечно (аварий после восстановления —
        # дублироваться в actions_log).
        self._consecutive_failures = 0
        # Сколько записей потеряно из-за отказа БД и когда разрешена следующая попытка.
        self.dropped_count = 0
        self._cooldown_until = 0.0

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._flush_loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self.flush()

    async def add_action(
        self,
        address: str,
        action_type: str,
        tx_hash: str,
        success: bool,
        details: str = "",
    ) -> None:
        async with self._lock:
            self._actions.append((address, action_type, tx_hash, int(success), details))
            delta = 1 if success else 0
            entry = self._health_updates.get(address)
            if entry is None:
                self._health_updates[address] = (1, delta)
            else:
                self._health_updates[address] = (entry[0] + 1, entry[1] + delta)
            should_flush = self._flush_on_action or len(self._actions) >= self.max_buffer
        if should_flush:
            await self.flush()

    async def _flush_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.flush_every)
                await self.flush()
        except asyncio.CancelledError:
            pass

    async def flush(self) -> None:
        # После устойчивого отказа БД делаем паузу, чтобы не долбить каждые 2 сек
        # (критика в лог, requeue и повторная потеря) и дать БД восстановиться.
        if time.monotonic() < self._cooldown_until:
            return
        # Забираем данные из буфера под локом, но пишем В БД вне лока,
        # чтобы не блокировать add_action во время медленной записи.
        async with self._lock:
            actions = self._actions
            health = self._health_updates
            self._actions = []
            self._health_updates = {}
        try:
            # Пишем actions и health отдельными коммитами и «вычёркиваем» из
            # локальной копии ровно то, что уже легло в БД. Если упадёт только
            # второй коммит, первый НЕ должен перезаписаться повторно (дубли в логе).
            pending_actions = actions
            pending_health = health
            if pending_actions:
                await self.db.log_actions_batch(pending_actions)
                pending_actions = []
            if pending_health:
                await self.db.update_wallet_health_batch(pending_health)
                pending_health = {}
            self._consecutive_failures = 0
        except Exception as e:
            self._consecutive_failures += 1
            # Устойчивый отказ БД: не копить буфер вечно. После 3 сбоев подряд
            # отбрасываем батч, иначе рост памяти и дубли логов после восстановления.
            if self._consecutive_failures >= 3:
                self.dropped_count += len(pending_actions) + len(pending_health)
                self._cooldown_until = time.monotonic() + 30
                self._consecutive_failures = 0
                logger.critical(
                    f"BatchWriter: сброшено {len(pending_actions) + len(pending_health)} записей "
                    f"({self.dropped_count} всего потеряно), БД недоступна: {e}"
                )
                return
            # Сбой записи: возвращаем в буфер только НЕ записанную часть,
            # чтобы не задвоить уже закоммиченные rows.
            async with self._lock:
                if pending_actions:
                    self._actions = pending_actions + self._actions
                for addr, (total, ok) in pending_health.items():
                    cur = self._health_updates.get(addr, (0, 0))
                    self._health_updates[addr] = (cur[0] + total, cur[1] + ok)
            logger.error(f"BatchWriter flush error (будет повторено): {e}")
