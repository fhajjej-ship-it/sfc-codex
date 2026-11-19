from __future__ import annotations

import copy
import json
from pathlib import Path
import re
import unittest
from unittest.mock import patch

import test_factory
from sfc_lib.common import Halt, Stopped
from sfc_lib.factory import Factory
from test_factory import issue


VALID = {"verdict": "pass", "summary": "Verified the export fix", "findings": [],
         "evidence": ["Inspected export.py", "Focused export fixture passed"]}


class WorkerResultTests(unittest.TestCase):
    setUp = test_factory.IntegrationTests.setUp
    tearDown = test_factory.IntegrationTests.tearDown

    def capture_result(self, result, interrupt=False):
        original = self.proc.execute

        def execute(argv, cwd, log, timeout, prompt=None, **kwargs):
            code = original(argv, cwd, log, timeout, prompt, **kwargs)
            if argv[0] == "codex":
                output = Path(argv[argv.index("--output-last-message") + 1])
                output.write_text(result if isinstance(result, str) else json.dumps(result))
                if interrupt:
                    raise Stopped("fixture interruption")
            return code
        return patch.object(self.proc, "execute", side_effect=execute)

    def test_observed_and_other_invalid_results_never_publish(self):
        self.factory.run_dir = self.root / ".sfc/runs/direct"
        head = self.factory.fetch()
        variants = [[*VALID["evidence"], ""], [], [" \t\n"], [4], [None]]
        for number, evidence in enumerate(variants, 1):
            with self.subTest(evidence=evidence):
                card = issue(number)
                with self.capture_result({**VALID, "evidence": evidence}):
                    with self.assertRaisesRegex(Halt, "Malformed build result"):
                        self.factory.lane(card, "build", self.root, head)
                self.assertEqual(self.gh.prs, {})
                self.assertEqual(self.gh.merges, [])
                self.assertEqual(Path(self.root, "value.txt").read_text(), "fixed\n")
                dispatch = self.factory.record(card)["worker_output_budget"]["build"]["dispatches"][0]
                self.assertEqual(json.loads(Path(dispatch["result_path"]).read_text())["evidence"], evidence)
                self.assertTrue(Path(dispatch["log"]).is_file())
                self.assertFalse(Path(dispatch["result_path"]).with_name("receipt.json").exists())

    def test_malformed_json_and_unresolved_findings_are_rejected(self):
        self.factory.run_dir = self.root / ".sfc/runs/direct"
        head = self.factory.fetch()
        for value in ("{broken", {**VALID, "findings": [{"file": "export.py", "line": 1,
                       "severity": "high", "message": "Still broken"}]}):
            with self.subTest(value=value), self.capture_result(value):
                with self.assertRaisesRegex(Halt, "Malformed build result"):
                    self.factory.lane(self.gh.items[0], "build", self.root, head)
        self.assertEqual(self.gh.prs, {})

    def test_missing_evidence_and_blank_summary_remain_invalid_for_all_verdicts(self):
        self.factory.run_dir = self.root / ".sfc/runs/direct"
        head = self.factory.fetch()
        for number, verdict in enumerate(("pass", "fail", "blocked"), 1):
            card = issue(number)
            for result in ({k: v for k, v in {**VALID, "verdict": verdict}.items() if k != "evidence"},
                           {**VALID, "verdict": verdict, "summary": " \t"}):
                with self.subTest(verdict=verdict, result=result), self.capture_result(result):
                    with self.assertRaisesRegex(Halt, "Malformed build result"):
                        self.factory.lane(card, "build", self.root, head)

    def test_meaningful_output_is_accepted_for_all_verdicts(self):
        self.factory.run_dir = self.root / ".sfc/runs/direct"
        head = self.factory.fetch()
        for number, verdict in enumerate(("pass", "fail", "blocked"), 1):
            result = {**VALID, "verdict": verdict}
            with self.subTest(verdict=verdict), self.capture_result(result):
                self.assertEqual(self.factory.lane(issue(number), "build", self.root, head), result)

    def test_evidence_text_wire_format_keeps_raw_result_and_receipt_identical(self):
        self.factory.run_dir = self.root / '.sfc/runs/direct-text'
        head = self.factory.fetch()
        for number, verdict in enumerate(('pass', 'fail', 'blocked'), 1):
            value = {**VALID, 'verdict': verdict,
                     'evidence': '\nInspected export.py; the focused export fixture passed.\nFull project checks remain supervisor-owned.\n'}
            card = issue(number)
            with self.subTest(verdict=verdict), self.capture_result(value):
                self.assertEqual(self.factory.lane(card, 'build', self.root, head), value)
            dispatch = self.factory.record(card)['worker_output_budget']['build']['dispatches'][-1]
            raw = json.loads(Path(dispatch['result_path']).read_text())
            receipt = json.loads(Path(dispatch['result_path']).with_name('receipt.json').read_text())
            self.assertEqual(raw, value)
            self.assertEqual(receipt['result'], raw)

    def test_blank_or_nontyped_evidence_text_remains_invalid_without_publication(self):
        self.factory.run_dir = self.root / '.sfc/runs/invalid-text'
        head = self.factory.fetch()
        for number, evidence in enumerate(('', ' \t\n', '\u00a0', 4, None, {}, True), 1):
            card = issue(number)
            with self.subTest(evidence=evidence), self.capture_result({**VALID, 'evidence': evidence}):
                with self.assertRaisesRegex(Halt, 'Malformed build result'):
                    self.factory.lane(card, 'build', self.root, head)
            self.assertEqual(self.gh.prs, {})

    def test_schema_and_prompt_express_nonblank_evidence(self):
        schema = json.loads((Path(__file__).parents[1] / "schemas/lane-result.json").read_text())
        evidence = schema["properties"]["evidence"]
        self.assertEqual(evidence.get('type'), 'string')
        self.assertEqual(evidence.get('minLength'), 1)
        for text in ("", " \t\n", "\u00a0"):
            self.assertIsNone(re.search(evidence['pattern'], text))
        self.assertIsNotNone(re.search(evidence['pattern'], VALID['evidence'][0]))
        with self.capture_result(VALID):
            self.factory.run()
        prompt = next(self.factory.dir.glob("runs/*/issue-1/build-*/prompt.md")).read_text()
        self.assertIn('one nonblank evidence text', prompt)
        self.assertIn("whitespace-only", prompt)

    def restart(self):
        self.factory = Factory(self.root, self.c, self.gh, self.proc)

    def test_repeated_malformed_output_exhausts_durable_cap_without_publication(self):
        invalid = {**VALID, "evidence": [*VALID["evidence"], ""]}
        archives = []
        for _ in range(self.c["max_attempts"]):
            with self.capture_result(invalid), self.assertRaisesRegex(Halt, "Malformed build result"):
                self.factory.run()
            folder = self.factory.run_dir
            archives.append({p: p.read_bytes() for p in folder.rglob("*") if p.is_file()})
            self.restart()
        claim = self.factory.record(self.gh.items[0])
        self.assertEqual(claim["attempts"], 0)  # A distinct budget, never a retry grant.
        worktree = Path(claim["worktree"])
        before = worktree.joinpath("value.txt").read_bytes()
        with self.capture_result(VALID), self.assertRaisesRegex(Halt, "output budget exhausted"):
            self.factory.run()
        self.assertEqual(self.proc.lanes, ["build", "build"])
        self.assertEqual(self.gh.prs, {})
        self.assertEqual(self.gh.merges, [])
        self.assertNotIn("Done", [status for _, status in self.gh.writes])
        self.assertEqual(worktree.joinpath("value.txt").read_bytes(), before)
        for archive in archives:
            self.assertTrue(all(p.read_bytes() == content for p, content in archive.items()))

    def test_interruption_reserves_budget_and_resume_keeps_valid_normal_gates(self):
        with self.capture_result(VALID, interrupt=True), self.assertRaises(Stopped):
            self.factory.run()
        claim = self.factory.record(self.gh.items[0])
        first = copy.deepcopy(claim["worker_output_budget"]["build"]["dispatches"][0])
        self.assertEqual(first["status"], "reserved")
        self.assertFalse(Path(first["result_path"]).with_name("receipt.json").exists())
        # A real explicit resume uses the retained claim/worktree and fresh checks.
        self.restart()
        with self.capture_result(VALID):
            self.factory.run()
        claim = self.factory.record(self.gh.items[0])
        self.assertEqual(claim["worker_output_budget"]["build"]["dispatches"][0], first)
        self.assertEqual(self.proc.lanes, ["build", "build", "qa", "security", "review"])
        self.assertEqual(claim["worktree"], first["worktree"])
        self.assertTrue(claim["verification"]["passed"])
        for lane in ("qa", "security", "review"):
            receipt = claim["receipts"][lane]
            self.assertEqual(receipt["head"], claim["head"])
            self.assertEqual(receipt["base"], claim["base"])
            self.assertEqual(receipt["config_digest"], self.factory.config_digest)
            self.assertEqual(receipt["task_digest"], self.factory.task_digest(self.gh.items[0]))
        self.assertEqual(len(self.gh.prs), 1)
        self.assertEqual(self.gh.merges, [])  # Human policy/run authority unchanged.
        self.assertEqual(self.gh.items[0]["status"], "Blocked")

    def test_interruption_cap_survives_task_and_config_changes(self):
        for _ in range(self.c["max_attempts"]):
            with self.capture_result(VALID, interrupt=True), self.assertRaises(Stopped):
                self.factory.run()
            self.restart()
        self.c["max_attempts"] = 20
        self.gh.items[0]["title"] = "Updated task"
        self.restart()
        with self.assertRaisesRegex(Halt, "output budget exhausted"):
            self.factory.run()
        self.assertEqual(self.proc.lanes, ["build", "build"])

    def test_success_does_not_refund_prior_malformed_spending(self):
        self.factory.run_dir = self.root / ".sfc/runs/direct"
        head = self.factory.fetch()
        card = self.gh.items[0]
        for result in ("{broken", VALID, "{broken"):
            with self.capture_result(result):
                if result == VALID:
                    self.factory.lane(card, "build", self.root, head)
                else:
                    with self.assertRaisesRegex(Halt, "Malformed build result"):
                        self.factory.lane(card, "build", self.root, head)
        self.assertNotIn("build", self.factory.record(card).get("receipts", {}))
        with self.assertRaisesRegex(Halt, "output budget exhausted"):
            self.factory.lane(card, "build", self.root, head)

    def legacy_failures(self):
        paths = []
        for number in range(2):
            folder = self.root / f".sfc/runs/legacy-{number}/issue-1/build-12345678"
            folder.mkdir(parents=True)
            output = folder / "result.json"
            output.write_text(json.dumps({**VALID, "evidence": [*VALID["evidence"], ""]}))
            (folder / "worker.log").write_text("retained legacy worker log\n")
            paths.append(output)
        return paths

    def test_legacy_failures_are_adopted_without_rewriting_or_fresh_budget(self):
        paths = self.legacy_failures()
        originals = [p.read_bytes() for p in paths]
        with self.assertRaisesRegex(Halt, "output budget exhausted"):
            self.factory.run()
        self.assertEqual(self.proc.lanes, [])
        budget = self.factory.record(self.gh.items[0])["worker_output_budget"]["build"]
        self.assertEqual([v["status"] for v in budget["dispatches"]], ["legacy-unaccepted"] * 2)
        self.assertEqual([p.read_bytes() for p in paths], originals)
        self.assertTrue(all(not p.with_name("receipt.json").exists() for p in paths))
        self.restart()
        with self.assertRaisesRegex(Halt, "output budget exhausted"):
            self.factory.run()
        self.assertEqual(self.proc.lanes, [])

    def test_legacy_resume_can_use_only_the_existing_remaining_allowance(self):
        self.c["max_attempts"] = 3
        paths = self.legacy_failures()
        self.restart()
        with self.capture_result(VALID):
            self.factory.run()
        budget = self.factory.record(self.gh.items[0])["worker_output_budget"]["build"]
        self.assertEqual(budget["limit"], 3)
        self.assertEqual([v["status"] for v in budget["dispatches"]],
                         ["legacy-unaccepted", "legacy-unaccepted", "accepted"])
        self.assertEqual(self.proc.lanes, ["build", "qa", "security", "review"])
        self.assertEqual(len(self.gh.prs), 1)
        self.assertTrue(all(not p.with_name("receipt.json").exists() for p in paths))

    def test_missing_budget_cannot_reinterpret_a_journaled_lane_as_legacy(self):
        self.factory.run_dir = self.root / ".sfc/runs/direct"
        head = self.factory.fetch()
        card = self.gh.items[0]
        with self.capture_result(VALID):
            self.factory.lane(card, "build", self.root, head)
        # Simulate an incomplete/lost state snapshot; successful raw artifacts remain.
        self.factory.record(card).pop("worker_output_budget")
        self.factory.save()
        self.restart()
        self.factory.run_dir = self.root / ".sfc/runs/resume"
        with self.assertRaisesRegex(Halt, "journal exists without its saved budget"):
            self.factory.lane(card, "build", self.root, head)
        self.assertEqual(self.proc.lanes, ["build"])


if __name__ == "__main__":
    unittest.main()
