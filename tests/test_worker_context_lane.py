"""Actual Factory lane integration with temporary packages and fake workers."""
import copy
from pathlib import Path
import shutil
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import test_lane_evidence as fixtures
from sfc_lib.common import Halt, git, write_json


class WorkerContextLaneTests(unittest.TestCase):
    tearDown = fixtures.LaneEvidenceTests.tearDown

    def setUp(self):
        fixtures.LaneEvidenceTests.setUp(self)
        original = Path(__file__).resolve().parents[1]
        self.package = self.root / ".sfc/package"
        shutil.copytree(original / "schemas", self.package / "schemas")
        for lane in ("build", "qa", "security", "review"):
            target = self.package / "skills" / f"sfc-{lane}"
            target.mkdir(parents=True)
            shutil.copyfile(original / "skills" / f"sfc-{lane}" / "SKILL.md", target / "SKILL.md")
        self.override = patch("sfc_lib.factory.PACKAGE", self.package)
        self.override.start()
        self.addCleanup(self.override.stop)
        self.assertTrue(self.factory.verify(self.card, self.root, self.head))

    def test_all_lanes_keep_exact_skill_bytes_project_reads_and_fresh_cli(self):
        for lane in ("build", "qa", "security", "review"):
            with self.subTest(lane=lane):
                result = self.factory.lane(self.card, lane, self.root, self.head)
                self.assertEqual(result["verdict"], "pass")
                skill = self.package / "skills" / f"sfc-{lane}" / "SKILL.md"
                prompt = self.processes.prompts[-1]
                argv = self.processes.argv[-1]
                self.assertIn(skill.read_text(), prompt)
                self.assertIn("Read this repository's applicable AGENTS.md instructions", prompt)
                self.assertIn(str(skill.parent), prompt)
                self.assertIn("--ephemeral", argv)
                self.assertNotIn("resume", argv)
                self.assertEqual(argv[argv.index("-s") + 1], "workspace-write" if lane == "build" else "read-only")
                dispatch = self.factory.record(self.card)["worker_output_budget"][lane]["dispatches"][-1]
                self.assertEqual(dispatch["worker_context"]["scope"], {
                    "source": str(self.package), "head": self.head, "base": self.head,
                    "task": self.factory.task_digest(self.card), "config": self.factory.config_digest})

    def test_changed_task_at_prelaunch_guard_halts_before_any_reservation(self):
        guard = self.factory.ensure_running

        def change():
            guard()
            self.card["body"] += "\nA different acceptance criterion.\n"

        with patch.object(self.factory, "ensure_running", side_effect=change), \
                self.assertRaisesRegex(Halt, "Worker context changed"):
            self.factory.lane(self.card, "qa", self.root, self.head)
        self.assertEqual(self.processes.prompts, [])
        self.assertNotIn("worker_output_budget", self.factory.record(self.card))

    def test_scoped_runtime_reaches_all_lanes_without_changing_capabilities(self):
        from sfc_lib.worker_runtime import arguments, receipt
        self.factory.c['worker_runtime'] = {'skill_catalog': False, 'memories': False}
        for lane in ('build', 'qa', 'security', 'review'):
            with self.subTest(lane=lane):
                self.factory.lane(self.card, lane, self.root, self.head)
                argv = self.processes.argv[-1]
                for value in arguments(self.factory.c)[1::2]:
                    self.assertIn(value, argv)
                self.assertFalse(any('mcp_servers' in value or 'plugins' in value for value in argv))
                self.assertNotIn('--ignore-user-config', argv)
                self.assertNotIn('--ignore-rules', argv)
                dispatch = self.factory.record(self.card)['worker_output_budget'][lane]['dispatches'][-1]
                self.assertEqual(dispatch['worker_runtime'], receipt(self.factory.c))
                if lane != 'build':
                    self.assertIn('do not reconstruct hash formulas', self.processes.prompts[-1])

    def test_changed_head_at_prelaunch_guard_halts_before_any_reservation(self):
        guard = self.factory.ensure_running

        def change():
            guard()
            git(self.root, "commit", "--allow-empty", "-m", "Move the pinned head")

        with patch.object(self.factory, "ensure_running", side_effect=change), \
                self.assertRaisesRegex(Halt, "Worker context changed"):
            self.factory.lane(self.card, "qa", self.root, self.head)
        self.assertEqual(self.processes.prompts, [])
        self.assertNotIn("worker_output_budget", self.factory.record(self.card))

    def test_changed_configuration_at_prelaunch_preserves_existing_authority_guard(self):
        guard = self.factory.ensure_running

        def change():
            modified = copy.deepcopy(self.config)
            modified["max_cycles"] += 1
            write_json(self.root / ".sfc/config.json", modified)
            guard()

        with patch.object(self.factory, "ensure_running", side_effect=change), \
                self.assertRaisesRegex(Halt, "configuration changed"):
            self.factory.lane(self.card, "qa", self.root, self.head)
        self.assertEqual(self.processes.prompts, [])
        self.assertNotIn("worker_output_budget", self.factory.record(self.card))

    def assert_package_change_during_worker_keeps_spending(self, path):
        execute = self.processes.execute

        def change(*args, **kwargs):
            result = execute(*args, **kwargs)
            path.write_bytes(path.read_bytes() + b"\nchanged after worker launch\n")
            return result

        with patch.object(self.processes, "execute", side_effect=change), \
                self.assertRaisesRegex(Halt, "Worker context changed"):
            self.factory.lane(self.card, "qa", self.root, self.head)
        record = self.factory.record(self.card)
        dispatches = record["worker_output_budget"]["qa"]["dispatches"]
        self.assertEqual(len(dispatches), 1)
        self.assertNotEqual(dispatches[0]["status"], "accepted")
        self.assertTrue(Path(dispatches[0]["log"]).is_file())
        self.assertTrue(Path(dispatches[0]["result_path"]).is_file())
        self.assertNotIn("qa", record.get("receipts", {}))

    def test_changed_skill_during_worker_keeps_raw_work_and_spent_reservation(self):
        self.assert_package_change_during_worker_keeps_spending(
            self.package / "skills/sfc-qa/SKILL.md")

    def test_changed_schema_during_worker_keeps_raw_work_and_spent_reservation(self):
        self.assert_package_change_during_worker_keeps_spending(
            self.package / "schemas/lane-result.json")

    def test_unavailable_package_uses_original_discovery_prompt_and_same_cli(self):
        skill = self.package / "skills/sfc-qa/SKILL.md"
        skill.unlink()
        self.assertEqual(self.factory.lane(self.card, "qa", self.root, self.head)["verdict"], "pass")
        prompt = self.processes.prompts[-1]
        self.assertIn(f"Read {skill} and this repository's applicable AGENTS.md instructions.", prompt)
        self.assertNotIn("BEGIN SFC LANE SKILL", prompt)
        self.assertIn("--ephemeral", self.processes.argv[-1])
        dispatch = self.factory.record(self.card)["worker_output_budget"]["qa"]["dispatches"][-1]
        self.assertIsNone(dispatch["worker_context"])


if __name__ == "__main__":
    unittest.main()
