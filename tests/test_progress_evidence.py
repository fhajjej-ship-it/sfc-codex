"""Exercise real supervisor progress publication against a fake GitHub board."""
from __future__ import annotations

from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from sfc_lib.common import Halt
from sfc_lib.evidence_summary import verification_check_counts
from sfc_lib.factory import Factory
import test_local_merge as merge_fixture
from test_verification_reuse import contract


PRIVATE_PATH = "/private/customer-check/sentinel-893"
PRIVATE_OUTPUT = "sentinel-private-output-893"


class ProgressEvidenceTests(unittest.TestCase):
    setUp = merge_fixture.LocalMergeTests.setUp
    tearDown = merge_fixture.LocalMergeTests.tearDown
    prepare_review = merge_fixture.LocalMergeTests.prepare_review

    def configure(self, *, reuse=True, **settings):
        reusable = self.c["verify_commands"][0]
        fresh = f"python3 -c \"print('{PRIVATE_OUTPUT} {PRIVATE_PATH}')\""
        self.c.update(auto_max_lines=800, verify_commands=[reusable, fresh], **settings)
        if reuse:
            self.c["verification_reuse"] = [contract(reusable)]
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        self.factory.activity.clock = lambda: 0.0

    def board_row(self, card):
        item = next(item for item in self.gh.items if item["number"] == card["number"])
        _, values = self.gh.activity_snapshot(card)
        return {"SFC Status": item["status"], "SFC Agent": values[0], "SFC Activity": values[1]}

    def observe_board(self, action):
        self.observed = [self.board_row(self.gh.items[0])]
        write = self.gh.write_activity

        def capture(card, scope, values):
            write(card, scope, values)
            self.observed.append(self.board_row(card))

        with patch.object(self.gh, "write_activity", side_effect=capture):
            result = action()
        return result

    def check_privacy(self):
        output = str(self.observed) + str(self.gh.body_updates)
        for private in (PRIVATE_PATH, PRIVATE_OUTPUT, str(self.root), "from pathlib import"):
            self.assertNotIn(private, output)

    def test_board_lifecycle_and_reused_final_gate(self):
        self.configure()
        result = self.observe_board(lambda: self.factory.run(allow_merge=True))
        self.assertEqual(result["outcome"], "drained")
        self.assertEqual(self.observed[0], {"SFC Status": "Ready", "SFC Agent": "", "SFC Activity": ""})
        self.assertTrue(any(row["SFC Status"] == "Building" and "Running" in row["SFC Agent"] and
                            "Build worker active" in row["SFC Activity"] for row in self.observed))
        checking = [row for row in self.observed if "Running project checks" in row["SFC Activity"]]
        self.assertTrue(any(row["SFC Status"] == "QA" for row in checking))
        reviewing = [row for row in self.observed if "Review worker assessing evidence" in row["SFC Activity"]]
        self.assertTrue(any(row["SFC Status"] == "Review" and
                            "2 executed, 0 reused" in row["SFC Activity"] for row in reviewing))
        completed = [row for row in self.observed if "Completed" in row["SFC Agent"]]
        self.assertEqual(completed, [self.observed[-1]])
        self.assertEqual(completed[0]["SFC Status"], "Done")
        self.assertIn("1 executed, 1 reused", completed[0]["SFC Activity"])
        self.assertEqual(len([event for event in self.proc.events if event[0] == "check"]), 3)
        self.assertEqual(len(self.gh.attempts), 1)
        record = self.factory.record(self.gh.items[0])
        self.assertEqual(record["local_merge"]["phase"], "done")
        self.assertFalse(record["activity"]["pending"])
        self.check_privacy()

    def test_public_counts_do_not_add_board_mutations(self):
        def run_merge(*, include_counts):
            self.configure()
            report = self.factory.activity.report

            def publish(*args, **kwargs):
                if not include_counts:
                    kwargs.pop("check_counts", None)
                return report(*args, **kwargs)

            with patch.object(self.factory.activity, "report", side_effect=publish):
                self.assertEqual(self.factory.run(allow_merge=True)["outcome"], "drained")
            record = self.factory.record(self.gh.items[0])
            actions = [(v["state"], v["action"]) for v in record["activity_history"]]
            self.assertEqual(len(self.gh.activity_writes), len(actions))
            return actions

        expected = run_merge(include_counts=False)
        self.assertTrue(expected)
        self.tearDown()
        self.setUp()
        self.assertEqual(run_merge(include_counts=True), expected)

    def test_pending_ci_keeps_review_waiting_with_action_and_verified_checks(self):
        self.configure()
        self.gh.ci = [{"status": "IN_PROGRESS"}]
        result = self.observe_board(lambda: self.factory.run(allow_merge=True))
        self.assertEqual(result["outcome"], "waiting")
        waiting = self.observed[-1]
        self.assertEqual(waiting["SFC Status"], "Review")
        self.assertIn("Waiting", waiting["SFC Agent"])
        self.assertIn("still pending", waiting["SFC Activity"])
        self.assertIn("2 executed, 0 reused", waiting["SFC Activity"])
        self.assertTrue(all("Completed" not in row["SFC Agent"] for row in self.observed))
        self.assertEqual(self.gh.attempts, [])
        self.assertEqual(self.gh.pr(101)["state"], "OPEN")
        self.check_privacy()

    def test_reviewed_handoff_and_pr_identify_reuse_without_claiming_delivery(self):
        self.configure(merge_policy="human")
        card, worktree = self.prepare_review()
        base = self.factory.record(card)["base"]
        self.assertTrue(self.factory.verify(card, worktree, base))
        self.observe_board(lambda: self.factory.review(card, worktree))
        waiting = self.observed[-1]
        self.assertEqual(waiting["SFC Status"], "Blocked")
        self.assertIn("Waiting", waiting["SFC Agent"])
        self.assertIn("human merge decision", waiting["SFC Activity"])
        self.assertIn("1 executed, 1 reused", waiting["SFC Activity"])
        body = self.gh.pr(101)["body"]
        lines = [line for line in body.splitlines() if line.startswith("- Configured check")]
        self.assertEqual(len(lines), 2)
        self.assertIn("Evidence: reused.", lines[0])
        self.assertIn("Evidence: executed.", lines[1])
        self.assertEqual(self.gh.pr(101)["state"], "OPEN")
        self.assertEqual(self.gh.attempts, [])
        self.assertTrue(all("Completed" not in row["SFC Agent"] for row in self.observed))
        self.check_privacy()

    def test_blocked_failure_and_later_manual_stage_keep_separate_meanings(self):
        self.configure(max_attempts=1)
        self.proc.fail_lane = "qa"
        self.observe_board(lambda: self.factory.run(allow_merge=True))
        self.failure_observed = self.observed.copy()
        failure = self.observed[-1]
        self.assertEqual(failure["SFC Status"], "Blocked")
        self.assertIn("Failed", failure["SFC Agent"])
        self.assertIn("Inspect retained lane findings", failure["SFC Activity"])
        self.assertEqual(self.gh.attempts, [])
        self.assertEqual(self.gh.pr(101)["state"], "OPEN")
        # Simulate an operator's edit in the fake board. The next supervisor
        # observation does not report a merge or replace the historical badge.
        self.gh.items[0]["status"] = "Done"
        self.observe_board(lambda: self.factory.run(allow_merge=True))
        self.assertEqual(self.observed, [{**failure, "SFC Status": "Done"}])
        self.assertEqual(self.gh.pr(101)["state"], "OPEN")
        self.assertEqual(self.gh.attempts, [])

    def test_malformed_check_provenance_or_counts_publish_nothing(self):
        valid = {"kind": "execution", "exit_code": 0}
        for check in ({"kind": "execution", "exit_code": False},
                      {"kind": "execution", "exit_code": 0, "reuse": {}},
                      {"exit_code": 0, "reuse": {**valid, "kind": "verification"}}):
            with self.subTest(check=check), self.assertRaises(Halt):
                verification_check_counts({"kind": "verification", "passed": True, "checks": [check]})
        for counts in ((True, 0), (-1, 1), (0, 0), (1, PRIVATE_PATH), (10**12 + 1, 0)):
            with self.subTest(counts=counts), self.assertRaises(Halt):
                self.factory.activity.report(self.gh.items[0], "Waiting", "next", check_counts=counts)
        self.assertEqual(self.gh.activity_writes, [])
