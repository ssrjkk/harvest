"""Сторожевой монитор портала: деградация фермы -> пуш-алерты владельцу.

Работает поверх FarmDaemon.statistics(): периодический срез, пороговые алерты
с дедупликацией по состоянию и сообщения о восстановлении. От aiogram не зависит:
уведомления уходят через notify-колбэк (в __main__ это Telegram-алерты владельцу).

Сигналы:
  * health_factor ниже порога min_consecutive тиков подряд  -> "сеть деградировала";
  * processed не растёт min_consecutive тиков при running   -> "ферма стоит";
  * ошибок за тик больше max_error_delta                    -> "всплеск ошибок";
  * переход running True -> False                           -> "остановилась".
  * heartbeat_hours > 0: периодический "пульс" владельцу.
Алерт шлётся один раз, при восстановлении условия — второе (короткое) сообщение.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)


def _heartbeat_text(data: dict) -> str:
    pool = data.get("pool") or {}
    db = data.get("db") or {}
    state = "▶ работает" if data.get("running") else "⏹ остановлена"
    if pool.get("paused"):
        state += " ⏸"
    return (
        f"❤️ Пульс: ферма {state}\n"
        f"циклы {pool.get('cycles', 0)} · действия {pool.get('actions', 0)} · "
        f"ошибки {pool.get('errors', 0)} · health {data.get('health_factor', 0.0):.2f} · "
        f"циклов в истории {db.get('cycles', 0)}"
    )


class Watchdog:
    def __init__(
        self,
        daemon,
        notify: Callable[[str], Awaitable[None]] | None = None,
        *,
        interval_s: float = 300.0,
        heartbeat_hours: float = 0.0,
        low_health: float = 0.5,
        max_error_delta: int = 100,
        min_consecutive: int = 2,
    ) -> None:
        self.daemon = daemon
        self.notify = notify
        self.interval_s = max(float(interval_s), 1.0)
        self.heartbeat_hours = max(float(heartbeat_hours), 0.0)
        self.low_health = float(low_health)
        self.max_error_delta = int(max_error_delta)
        self.min_consecutive = max(int(min_consecutive), 1)
        self._prev: dict | None = None
        self._prev_errors = 0
        self._streak: dict[str, int] = {}
        self._tripped: set[str] = set()
        self._heartbeat_ts = time.monotonic()

    async def run(self) -> None:
        """Бесконечный цикл мониторинга (останавливается через task.cancel())."""
        while True:
            try:
                await self.tick()
            except Exception:  # noqa: BLE001
                logger.warning("Watchdog: тик упал", exc_info=True)
            await asyncio.sleep(self.interval_s)

    async def tick(self) -> list[str]:
        """Один срез статистики; возвращает отправленные сообщения (для тестов)."""
        now = time.monotonic()
        sent: list[str] = []
        try:
            data = await self.daemon.statistics()
        except Exception:  # noqa: BLE001
            logger.warning("Watchdog: статистика фермы недоступна", exc_info=True)
            await self._send("⚠️ Watchdog: статистика фермы недоступна. /doctor", sent)
            return sent

        running = bool(data.get("running"))
        pool = data.get("pool") or {}
        health = float(data.get("health_factor") or 0.0)
        errors_total = int(pool.get("errors") or 0)
        processed = int(pool.get("processed") or 0)
        paused = bool(pool.get("paused"))

        if self._prev is None:
            self._prev = {"running": running, "processed": processed}
            self._prev_errors = errors_total
            self._heartbeat_ts = now
            return sent

        was_running = bool(self._prev["running"])

        if was_running and not running:
            await self._send("⚠️ Ферма остановилась.", sent)
            self._tripped.add("stopped")

        if not was_running and running:
            # Рестарт: новый базлайн, чтобы не алертить "стоит"
            # пока пул только инициализируется.
            self._prev["processed"] = processed
            self._prev_errors = errors_total
            self._streak.clear()
            self._heartbeat_ts = now
            return sent

        if running:
            # --- Сеть ---
            if health < self.low_health:
                self._streak["health"] = self._streak.get("health", 0) + 1
                if self._streak["health"] >= self.min_consecutive and "health" not in self._tripped:
                    await self._send(f"⚠️ Сеть деградировала: health {health:.2f}. /doctor", sent)
                    self._tripped.add("health")
            else:
                self._streak["health"] = 0
                if "health" in self._tripped:
                    await self._send("✅ Сеть восстановилась.", sent)
                    self._tripped.discard("health")

            # --- Прогресс (пауза не считается стояком) ---
            if not paused and processed <= self._prev["processed"]:
                self._streak["stall"] = self._streak.get("stall", 0) + 1
            else:
                self._streak["stall"] = 0
            if self._streak["stall"] >= self.min_consecutive and "stall" not in self._tripped:
                await self._send("⚠️ Ферма стоит: прогресс не растёт. /doctor", sent)
                self._tripped.add("stall")
            elif self._streak["stall"] == 0 and "stall" in self._tripped:
                await self._send("✅ Прогресс возобновился.", sent)
                self._tripped.discard("stall")

            # --- Всплеск ошибок ---
            delta = errors_total - self._prev_errors
            if "errors" not in self._tripped and delta > self.max_error_delta:
                await self._send(f"⚠️ Всплеск ошибок: +{delta} за тик. /doctor", sent)
                self._tripped.add("errors")
            elif "errors" in self._tripped and delta <= 0:
                await self._send("✅ Поток ошибок прекратился.", sent)
                self._tripped.discard("errors")
        else:
            self._streak.clear()

        # --- Heartbeat ---
        if self.heartbeat_hours > 0 and now - self._heartbeat_ts >= self.heartbeat_hours * 3600:
            self._heartbeat_ts = now
            await self._send(_heartbeat_text(data), sent)

        self._prev["running"] = running
        self._prev["processed"] = processed
        self._prev_errors = errors_total
        return sent

    async def _send(self, text: str, out: list[str]) -> None:
        out.append(text)
        if self.notify is None:
            return
        try:
            await self.notify(text)
        except Exception:  # noqa: BLE001
            logger.warning("Watchdog: не удалось отправить алерт", exc_info=True)
