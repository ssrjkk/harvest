"""Тесты для планировщика (core/scheduler.py)."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from core.scheduler import ScheduleTask, Scheduler


def asyncio_run(fn):
    return asyncio.run(fn())


class TestScheduleTask(unittest.TestCase):
    def test_create_task(self):
        task = ScheduleTask("test_1", name="Test Task", interval_hours=2, cycles=3)
        self.assertEqual(task.task_id, "test_1")
        self.assertEqual(task.name, "Test Task")
        self.assertEqual(task.interval_hours, 2)
        self.assertEqual(task.cycles, 3)
        self.assertTrue(task.enabled)

    def test_total_interval_seconds(self):
        task = ScheduleTask("t", interval_hours=1, interval_minutes=30)
        self.assertEqual(task.total_interval_seconds, 5400)

    def test_to_dict(self):
        task = ScheduleTask("t1", name="My Task", interval_hours=1, cycles=2)
        d = task.to_dict()
        self.assertEqual(d["task_id"], "t1")
        self.assertEqual(d["name"], "My Task")
        self.assertEqual(d["interval_hours"], 1)
        self.assertEqual(d["cycles"], 2)

    def test_from_dict(self):
        d = {
            "task_id": "t2",
            "name": "Task 2",
            "enabled": False,
            "interval_hours": 3,
            "interval_minutes": 0,
            "run_at_time": None,
            "cycles": 5,
            "last_run": None,
            "next_run": None,
        }
        task = ScheduleTask.from_dict(d)
        self.assertEqual(task.task_id, "t2")
        self.assertFalse(task.enabled)
        self.assertEqual(task.cycles, 5)


class TestScheduler(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self.tmp.close()
        self.scheduler = Scheduler(self.tmp.name)

    def tearDown(self):
        Path(self.tmp.name).unlink(missing_ok=True)

    def test_add_task(self):
        task = self.scheduler.add_task("t1", name="Test", interval_hours=1, cycles=2)
        self.assertEqual(task.task_id, "t1")
        self.assertIn("t1", self.scheduler.tasks)

    def test_remove_task(self):
        self.scheduler.add_task("t1", name="Test")
        self.assertTrue(self.scheduler.remove_task("t1"))
        self.assertNotIn("t1", self.scheduler.tasks)
        self.assertFalse(self.scheduler.remove_task("nonexistent"))

    def test_update_task(self):
        self.scheduler.add_task("t1", name="Test", interval_hours=1)
        task = self.scheduler.update_task("t1", name="Updated", enabled=False)
        self.assertIsNotNone(task)
        self.assertEqual(task.name, "Updated")
        self.assertFalse(task.enabled)

    def test_update_nonexistent(self):
        self.assertIsNone(self.scheduler.update_task("nonexistent", name="X"))

    def test_save_and_load(self):
        self.scheduler.add_task("t1", name="Task 1", interval_hours=2, cycles=3)
        self.scheduler.add_task("t2", name="Task 2", interval_minutes=30)
        self.scheduler.save()

        scheduler2 = Scheduler(self.tmp.name)
        asyncio.run(scheduler2.load())
        self.assertEqual(len(scheduler2.tasks), 2)
        self.assertEqual(scheduler2.tasks["t1"].name, "Task 1")
        self.assertEqual(scheduler2.tasks["t2"].interval_minutes, 30)

    def test_load_nonexistent_file(self):
        scheduler = Scheduler("/nonexistent/path.json")
        asyncio.run(scheduler.load())
        self.assertEqual(len(scheduler.tasks), 0)

    def test_get_all_tasks(self):
        self.scheduler.add_task("t1", name="Task 1", interval_hours=1)
        tasks = self.scheduler.get_all_tasks()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["task_id"], "t1")
        self.assertIn("is_due", tasks[0])

    def test_set_run_callback(self):
        callback = AsyncMock()
        self.scheduler.set_run_callback(callback)
        self.assertEqual(self.scheduler._run_callback, callback)

    def test_start_stop(self):
        async def main():
            self.scheduler.set_run_callback(AsyncMock())
            await self.scheduler.start()
            self.assertTrue(self.scheduler.is_running)
            await self.scheduler.stop()
            self.assertFalse(self.scheduler.is_running)
        asyncio_run(main)

    def test_execute_task(self):
        async def main():
            callback = AsyncMock()
            self.scheduler.set_run_callback(callback)
            self.scheduler.add_task("t1", name="Test", interval_hours=0, interval_minutes=0, cycles=2)
            task = self.scheduler.tasks["t1"]
            await self.scheduler._execute_task(task)
            callback.assert_called_once_with(2)
            self.assertIsNotNone(task.last_run)
        asyncio_run(main)

    def test_execute_task_callback_error(self):
        async def main():
            callback = AsyncMock(side_effect=RuntimeError("fail"))
            self.scheduler.set_run_callback(callback)
            self.scheduler.add_task("t1", name="Test", cycles=1)
            task = self.scheduler.tasks["t1"]
            await self.scheduler._execute_task(task)
            callback.assert_called_once()
        asyncio_run(main)

    def test_is_due_with_interval(self):
        from datetime import datetime, timedelta
        self.scheduler.add_task("t1", interval_hours=0, interval_minutes=0)
        task = self.scheduler.tasks["t1"]
        task.last_run = (datetime.now() - timedelta(hours=1)).isoformat()
        task.next_run = (datetime.now() - timedelta(minutes=1)).isoformat()
        self.assertTrue(self.scheduler._is_due(task))

    def test_is_due_disabled(self):
        self.scheduler.add_task("t1", interval_hours=1)
        task = self.scheduler.tasks["t1"]
        task.enabled = False
        self.assertFalse(self.scheduler._is_due(task))

    def test_is_due_no_next_run(self):
        self.scheduler.add_task("t1", interval_hours=1)
        task = self.scheduler.tasks["t1"]
        task.next_run = None
        self.assertFalse(self.scheduler._is_due(task))

    def test_compute_next_run_with_time(self):
        self.scheduler.add_task("t1", run_at_time="23:59")
        task = self.scheduler.tasks["t1"]
        self.assertIsNotNone(task.next_run)

    def test_compute_next_run_invalid_time(self):
        self.scheduler.add_task("t1", run_at_time="invalid")
        task = self.scheduler.tasks["t1"]
        self.assertIsNone(task.next_run)

    def test_compute_next_run_no_interval(self):
        self.scheduler.add_task("t1", interval_hours=0, interval_minutes=0)
        task = self.scheduler.tasks["t1"]
        self.assertIsNone(task.next_run)

    def test_default_name(self):
        task = ScheduleTask("t1")
        self.assertEqual(task.name, "Task t1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
