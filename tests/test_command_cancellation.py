"""An overlapping check cancels only its own provisional model invocation."""
from concurrent.futures import ThreadPoolExecutor
import os
import signal
import subprocess
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from sfc_lib.common import CommandCancelled, Processes, command_cancellation


class CancellationTests(unittest.TestCase):
    def test_delivery_sigterm_cleans_up_its_active_child_and_restores_handlers(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); log = root / 'child.log'
            script = ("from pathlib import Path; import sys; from sfc_lib.common import Processes; "
                      "from sfc_lib.chat_merge import owned_processes; "
                      "p=Processes(Path(sys.argv[1])/'stop'); "
                      "context=owned_processes(p); context.__enter__(); "
                      "p.execute([sys.executable,'-u','-c','import os,time; print(os.getpid(),flush=True); time.sleep(30)'], "
                      "sys.argv[1],Path(sys.argv[1])/'child.log',10)")
            env = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'scripts'), 'SFC_WORKER': '1'}
            parent = subprocess.Popen([sys.executable, '-c', script, str(root)], env=env,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and (not log.exists() or not log.read_text().strip()):
                    time.sleep(.02)
                self.assertTrue(log.exists() and log.read_text().strip())
                child = int(log.read_text().splitlines()[0])
                parent.send_signal(signal.SIGTERM)
                parent.wait(timeout=7)
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    status = subprocess.run(['ps', '-p', str(child), '-o', 'stat='], capture_output=True, text=True).stdout.strip()
                    if not status or status.startswith('Z'):
                        break
                    time.sleep(.02)
                self.assertTrue(not status or status.startswith('Z'), 'Owned child outlived its delivery supervisor')
            finally:
                if parent.poll() is None:
                    parent.terminate()
                    try:
                        parent.wait(timeout=4)
                    except subprocess.TimeoutExpired:
                        parent.kill(); parent.wait()
        from sfc_lib.chat_merge import owned_processes
        process = Processes(Path(name) / 'unused-stop')
        old = signal.getsignal(signal.SIGTERM)
        with owned_processes(process):
            self.assertNotEqual(signal.getsignal(signal.SIGTERM), old)
        self.assertEqual(signal.getsignal(signal.SIGTERM), old)

    def test_children_receive_the_actual_checkout_pwd_not_the_callers(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).resolve()
            processes = Processes(root / 'stop')
            command = [sys.executable, '-c',
                       'import os; from pathlib import Path; assert Path(os.environ["PWD"]).resolve() == Path.cwd()']
            for inherited in (None, '/stale/caller/path'):
                with self.subTest(inherited=inherited), patch.dict(os.environ):
                    if inherited is None:
                        os.environ.pop('PWD', None)
                    else:
                        os.environ['PWD'] = inherited
                    self.assertEqual(processes.execute(command, root, root / 'pwd.log', 5), 0)

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
