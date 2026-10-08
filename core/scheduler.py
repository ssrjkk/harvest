"""Планировщик задач: cron-подобный запуск фарма по расписанию.

Поддерживает интервалы (каждые N часов/минут) и фиксированное время суток.
Конфигурация хранится в JSON-файле, задачи выполняются в фоне.
"""

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SCHEDULE_FILE = "schedule.json"


class ScheduleTask:
    """Одна задача расписания."""

    def __init__(
        self,
        task_id: str,
        *,
        name: str = "",
        enabled: bool = True,
        interval_hours: float = 0,
        interval_minutes: float = 0,
        run_at_time: str | None = None,
        cycles: int = 1,
        last_run: str | None = None,
        next_run: str | None = None,
    ):
        self.task_id = task_id
        self.name = name or f"Task {task_id}"
        self.enabled = enabled
        self.interval_hours = interval_hours
        self.interval_minutes = interval_minutes
        self.run_at_time = run_at_time
        self.cycles = cycles
        self.last_run = last_run
        self.next_run = next_run

    @property
    def total_interval_seconds(self) -> float:
        return self.interval_hours * 3600 + self.interval_minutes * 60

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "name": self.name,
            "enabled": self.enabled,
            "interval_hours": self.interval_hours,
            "interval_minutes": self.interval_minutes,
            "run_at_time": self.run_at_time,
            "cycles": self.cycles,
            "last_run": self.last_run,
            "next_run": self.next_run,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ScheduleTask":
        return cls(**d)


class Scheduler:
    """Менеджер расписания: загрузка/сохранение задач, фоновое выполнение."""

    def __init__(self, schedule_path: str = SCHEDULE_FILE) -> None:
        self.path = Path(schedule_path)
        self.tasks: dict[str, ScheduleTask] = {}
        self._task: asyncio.Task | None = None
        self._running = False
        self._run_callback: Any = None
        self._stop_event = asyncio.Event()

    async def load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            for item in data.get("tasks", []):
                t = ScheduleTask.from_dict(item)
                self.tasks[t.task_id] = t
            logger.info("Scheduler: loaded %d tasks", len(self.tasks))
        except Exception as e:
            logger.warning("Scheduler: failed to load: %s", e)

    def save(self) -> None:
        try:
            data = {"tasks": [t.to_dict() for t in self.tasks.values()]}
            self.path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            logger.warning("Scheduler: failed to save: %s", e)

    def add_task(
        self,
        task_id: str,
        *,
        name: str = "",
        interval_hours: float = 0,
        interval_minutes: float = 0,
        run_at_time: str | None = None,
        cycles: int = 1,
    ) -> ScheduleTask:
        task = ScheduleTask(
            task_id,
            name=name,
            interval_hours=interval_hours,
            interval_minutes=interval_minutes,
            run_at_time=run_at_time,
            cycles=cycles,
        )
        self.tasks[task_id] = task
        self._compute_next_run(task)
        self.save()
        logger.info("Scheduler: added task %s (%s)", task_id, name)
        return task

    def remove_task(self, task_id: str) -> bool:
        if task_id in self.tasks:
            del self.tasks[task_id]
            self.save()
            return True
        return False

    def update_task(self, task_id: str, **kwargs: Any) -> ScheduleTask | None:
        task = self.tasks.get(task_id)
        if not task:
            return None
        for k, v in kwargs.items():
            if hasattr(task, k):
                setattr(task, k, v)
        self._compute_next_run(task)
        self.save()
        return task

    def _compute_next_run(self, task: ScheduleTask) -> None:
        now = datetime.now()
        if task.run_at_time:
            try:
                h, m = map(int, task.run_at_time.split(":"))
                candidate = now.replace(hour=h, minute=m, second=0, microsecond=0)
                if candidate <= now:
                    candidate = candidate.replace(day=candidate.day + 1)
                task.next_run = candidate.isoformat()
            except Exception:
                task.next_run = None
        elif task.total_interval_seconds > 0:
            if task.last_run:
                try:
                    last = datetime.fromisoformat(task.last_run)
                    from datetime import timedelta

                    task.next_run = (last + timedelta(seconds=task.total_interval_seconds)).isoformat()
                except Exception:
                    task.next_run = None
            else:
                task.next_run = now.isoformat()
        else:
            task.next_run = None

    def get_all_tasks(self) -> list[dict]:
        result = []
        for t in self.tasks.values():
            d = t.to_dict()
            d["is_due"] = self._is_due(t)
            result.append(d)
        return result

    def _is_due(self, task: ScheduleTask) -> bool:
        if not task.enabled or not task.next_run:
            return False
        try:
            return datetime.now() >= datetime.fromisoformat(task.next_run)
        except Exception:
            return False

    def set_run_callback(self, callback: Any) -> None:
        """Устанавливает async-функцию для запуска фарма: callback(cycles: int)."""
        self._run_callback = callback

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._stop_event.clear()
        self._task = asyncio.create_task(self._loop())
        logger.info("Scheduler started")

    async def stop(self) -> None:
        self._running = False
        self._stop_event.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("Scheduler stopped")

    @property
    def is_running(self) -> bool:
        return self._running

    async def _loop(self) -> None:
        while self._running:
            try:
                for task in list(self.tasks.values()):
                    if self._is_due(task):
                        await self._execute_task(task)
                await asyncio.wait_for(self._stop_event.wait(), timeout=10)
                break
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                logger.error("Scheduler loop error: %s", e)
                await asyncio.sleep(5)

    async def _execute_task(self, task: ScheduleTask) -> None:
        logger.info("Scheduler: executing task %s (%s)", task.task_id, task.name)
        task.last_run = datetime.now().isoformat()
        self._compute_next_run(task)
        self.save()
        if self._run_callback:
            try:
                await self._run_callback(task.cycles)
            except Exception as e:
                logger.error("Scheduler: task %s failed: %s", task.task_id, e)
