"""An overlapping check cancels only its own provisional model invocation."""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from sfc_lib.common import CommandCancelled, Processes, command_cancellation


class CancellationTests(unittest.TestCase):
    def test_worker_python_imports_do_not_leave_bytecode_in_the_checkout(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / 'worker_fixture.py').write_text('value = 1\n')
            processes = Processes(root / 'stop')
            command = [sys.executable, '-c', 'import worker_fixture']
            with patch.dict(os.environ):
                os.environ.pop('PYTHONDONTWRITEBYTECODE', None)
                self.assertEqual(processes.execute(command, root, root / 'worker.log', 5,
                                                   worker_events=True), 0)
            self.assertFalse((root / '__pycache__').exists())

    def test_owned_child_is_cancelled_without_stopping_concurrent_or_later_commands(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            processes = Processes(root / 'stop')
            started, cancel = threading.Event(), threading.Event()
            def inspection():
                with command_cancellation(cancel.is_set):
                    return processes.execute([sys.executable, '-u', '-c',
                        'import time; print("inspection started"); time.sleep(30)'], root,
                        root / 'inspection.log', 40,
                        observer=lambda event: started.set() if event == 'started' else None)
            with ThreadPoolExecutor(max_workers=2) as pool:
                pending = pool.submit(inspection)
                self.assertTrue(started.wait(5))
                # Another command on the same adapter triggers cancellation and
                # still completes successfully in its own context.
                code = processes.execute([sys.executable, '-c', 'print("checks failed")'], root,
                    root / 'checks.log', 5, observer=lambda event: cancel.set() if event == 'started' else None)
                self.assertEqual(code, 0)
                with self.assertRaises(CommandCancelled):
                    pending.result(timeout=6)
            self.assertFalse(processes.stopped())
            self.assertFalse(processes.children)
            self.assertTrue((root / 'inspection.log').is_file())
            self.assertEqual(processes.execute([sys.executable, '-c', 'print("next ticket")'], root,
                                               root / 'next.log', 5), 0)

    def test_cancel_before_spawn_is_bounded_and_context_is_restored(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            processes = Processes(root / 'stop')
            with command_cancellation(lambda: True), self.assertRaises(CommandCancelled):
                processes.execute([sys.executable, '-c', 'raise AssertionError("must not launch")'],
                                  root, root / 'unlaunched.log', 5)
            self.assertFalse((root / 'unlaunched.log').exists())
            self.assertEqual(processes.execute([sys.executable, '-c', 'pass'], root, root / 'next.log', 5), 0)


if __name__ == '__main__':
    unittest.main()
