from __future__ import annotations

import copy
import json
from pathlib import Path
import shlex
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from sfc_lib.common import Halt, Processes, git, write_json
from sfc_lib.factory import Factory
from test_factory import BASE_CONFIG, issue, FakeGitHub


class RecordingProcesses(Processes):
    def __init__(self, stop):
        super().__init__(stop)
        self.prompts = []
        self.argv = []

    def execute(self, argv, cwd, log, timeout, prompt=None, **kwargs):
        if argv[0] != "codex":
            return super().execute(argv, cwd, log, timeout, prompt, **kwargs)
        self.prompts.append(prompt)
        self.argv.append(argv)
        output = Path(argv[argv.index("--output-last-message") + 1])
        write_json(output, {"verdict": "pass", "summary": "Independent assessment completed",
                            "findings": [], "evidence": ["Inspected changed behavior and pinned check logs"]})
        Path(log).write_text("Simulated read-only Codex lane\n")
        return 0


class LaneEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sfc-evidence-")
        self.root = Path(self.temp.name)
        git(self.root, "init", "-b", "main")
        git(self.root, "config", "user.email", "test@example.invalid")
        git(self.root, "config", "user.name", "SFC Test")
        (self.root / ".gitignore").write_text(".sfc/\n")
        git(self.root, "add", ".gitignore")
        git(self.root, "commit", "-m", "fixture")
        self.config = copy.deepcopy(BASE_CONFIG)
        self.config["verify_commands"] = [shlex.join([sys.executable, '-c', "print('project check passed')"])]
        self.processes = RecordingProcesses(self.root / ".sfc/stop")
        self.factory = Factory(self.root, self.config, github=FakeGitHub(self.root), processes=self.processes)
        self.factory.run_dir = self.root / ".sfc/runs/fixture"
        self.card = issue()
        self.head = git(self.root, "rev-parse", "HEAD")

    def tearDown(self):
        self.temp.cleanup()

    def test_read_only_lanes_receive_real_pinned_execution_logs(self):
        self.assertTrue(self.factory.verify(self.card, self.root, self.head))
        receipt = self.factory.record(self.card)["verification"]
        for lane in ("qa", "security", "review"):
            with self.subTest(lane=lane):
                result = self.factory.lane(self.card, lane, self.root, self.head)
                self.assertEqual(result["verdict"], "pass")
                prompt = self.processes.prompts[-1]
                packet = json.loads(prompt.split('configuration:\n', 1)[1].splitlines()[0])
                self.assertEqual(packet['receipt'], {'path': receipt['receipt_path'], 'sha256': receipt['receipt_sha256']})
                for pin in ('head', 'base', 'config_digest', 'task_digest'):
                    self.assertEqual(packet[pin], receipt[pin])
                check = receipt['checks'][0]
                self.assertEqual(packet['checks'][0]['log_sha256'], check['log_sha256'])
                self.assertEqual(packet['checks'][0]['original']['sha256'], check['receipt_sha256'])
                self.assertNotIn(json.dumps(receipt), prompt)
                self.assertIn(receipt["checks"][0]["log"], prompt)
                argv = self.processes.argv[-1]
                self.assertEqual(argv[argv.index("-s") + 1], "read-only")
        self.assertIn("project check passed", Path(receipt["checks"][0]["log"]).read_text())

    def test_missing_failed_or_stale_evidence_never_dispatches_a_model(self):
        self.assertTrue(self.factory.verify(self.card, self.root, self.head))
        original = copy.deepcopy(self.factory.record(self.card)["verification"])
        variants = [{}, {**original, "passed": False}, {**original, "head": "0" * 40},
                    {**original, "base": "0" * 40}, {**original, "config_digest": "different"},
                    {**original, "task_digest": "different"}, {**original, "checks": []},
                    {**original, "checks": [{**original["checks"][0], "exit_code": 1}]},
                    {**original, "checks": [{**original["checks"][0], "command": "different"}]}]
        for receipt in variants:
            with self.subTest(receipt=receipt):
                self.factory.record(self.card)["verification"] = receipt
                with self.assertRaisesRegex(Halt, "verification evidence"):
                    self.factory.lane(self.card, "qa", self.root, self.head)
        self.assertEqual(self.processes.prompts, [])

    def test_missing_raw_log_never_dispatches_a_model(self):
        self.assertTrue(self.factory.verify(self.card, self.root, self.head))
        receipt = self.factory.record(self.card)["verification"]
        Path(receipt["checks"][0]["log"]).unlink()
        with self.assertRaisesRegex(Halt, "verification evidence"):
            self.factory.lane(self.card, "review", self.root, self.head)
        self.assertEqual(self.processes.prompts, [])


if __name__ == "__main__":
    unittest.main()
