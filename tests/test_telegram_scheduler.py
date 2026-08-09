import tempfile
import time
import unittest
from pathlib import Path

from scheduler import (
    ScheduledTask,
    SchedulerError,
    SchedulerStore,
    TaskScheduler,
    run_python_script,
    validate_task,
)


class SchedulerTests(unittest.TestCase):
    def test_scheduler_core_is_not_owned_by_telegram(self):
        import scheduler

        self.assertEqual("scheduler", scheduler.__name__)

    def test_rejects_script_outside_allowed_roots(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "outside.py"
            script.write_text("print('x')", encoding="utf-8")
            with self.assertRaises(SchedulerError):
                validate_task(ScheduledTask("x", "T", "event", "new", script_path="outside.py"), root)

    def test_store_and_runner(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "data" / "work").mkdir(parents=True)
            script = root / "data" / "work" / "job.py"
            script.write_text("print('ok')", encoding="utf-8")
            store = SchedulerStore(root / "scheduler.sqlite3")
            task = ScheduledTask("t1", "Financeiro", "once", "new", script_path="data/work/job.py", run_at="2000-01-01T00:00:00+00:00")
            store.save(task, root)
            self.assertEqual(store.due("2026-01-01T00:00:00+00:00")[0].task_uid, "t1")
            self.assertTrue(run_python_script(script, root)["ok"])
            store.close()

    def test_rejects_scripts_in_protected_data_subtrees(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "data" / "config"
            script.mkdir(parents=True)
            file = script / "job.py"
            file.write_text("print('x')", encoding="utf-8")
            with self.assertRaises(SchedulerError):
                validate_task(ScheduledTask("x", "T", "event", "new", script_path="data/config/job.py"), root)

    def test_scheduler_runs_callback_and_stops(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = SchedulerStore(root / "scheduler.sqlite3")
            store.save(ScheduledTask("t1", "T", "once", "new", prompt="ver", run_at="2000-01-01T00:00:00+00:00"), root)
            seen = []
            scheduler = TaskScheduler(store, root, lambda task, run: seen.append((task.task_uid, run)), interval=0.01)
            scheduler.start()
            time.sleep(1.2)
            scheduler.stop()
            store.close()
            self.assertEqual(len(seen), 1)


if __name__ == "__main__":
    unittest.main()
