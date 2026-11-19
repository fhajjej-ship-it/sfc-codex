"""Explanation rendering and unchanged gates in isolated repositories only."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import sfc
from sfc_lib.common import Halt, Processes, common_dir
from sfc_lib.factory import Factory, check_explanation, policy_explanations, policy_reasons
from sfc_lib.reasons import explain_wait, outcome_reason, reason, render, render_many
from sfc_lib.watch import session_explanation
import test_factory as fixtures


class ReasonTests(unittest.TestCase):
    def test_only_fixed_text_and_bounded_integers_can_be_rendered(self):
        for code, facts in (("size", {"changed": "private command"}),
                            ("size", {"limit": True}), ("size", {"pr": 10**13}),
                            ("human", {"path": "private/path"}),
                            ("dependency", {"issues": [1, "secret"]}),
                            ("dependency", {"issues": list(range(21))}),
                            ("private raw log", {})):
            with self.subTest(code=code, facts=facts), self.assertRaises(Halt):
                render(reason(code, **facts))
        self.assertNotIn("secret", explain_wait({"reasons": ["API private secret"]})["explanation"])
        self.assertIn("reconciliation", explain_wait({"reasons": ["API private secret"]})["explanation"])

    def test_policy_mapping_preserves_every_existing_policy_gate_and_withholds_paths(self):
        c = {**fixtures.BASE_CONFIG, "merge_policy": "auto"}
        base = {"number": 101, "additions": 1, "deletions": 0, "files": []}
        for labels, files, expected in ((["human-only"], [], "human-only"),
                                        (["billing"], [], "sensitive"),
                                        ([], [{"path": "auth/private-account-name.py"}], "sensitive")):
            card, pr = fixtures.issue(labels=labels), {**base, "files": files}
            before = policy_reasons(c, card, pr)
            text = render_many(policy_explanations(c, card, pr, True))
            self.assertTrue(before)
            self.assertEqual(policy_reasons(c, card, pr), before)
            self.assertIn(expected, text)
            self.assertIn("human", text)
            self.assertNotIn("private-account-name", text)
        self.assertFalse(policy_reasons(c, fixtures.issue(), base))
        self.assertIn("no automatic merge authority", render_many(policy_explanations(c, fixtures.issue(), base, False)))

    def test_check_states_and_spent_budgets_have_specific_safe_actions(self):
        for checks, expected in (([], "No GitHub checks"),
                                 ([{"state": "FAILURE", "description": "private"}], "failed"),
                                 ([{"status": "COMPLETED", "conclusion": "FAILURE"}], "failed"),
                                 ([{"status": "IN_PROGRESS"}], "pending")):
            text = render(check_explanation({"statusCheckRollup": checks}))
            self.assertIn(expected, text)
            self.assertNotIn("private", text)
        for outcome in ("watch-time-limit", "watch-polls-limit", "watch-claims-limit", "cycle-limit", "no-progress-limit"):
            self.assertIn("exhausted", outcome_reason(outcome))
        self.assertIn("original deadline and counters", outcome_reason("interrupted"))
        self.assertIn("before an explicitly authorized resume", outcome_reason("stopped"))
        session = {"deadline": 100, "settings": {"max_polls": 2, "max_claims": 3},
                   "limits": {"max_cycles": 4, "max_no_progress": 5},
                   "polls": 0, "claims": 0, "cycles": 0, "no_progress": 0,
                   "reason": "interrupted"}
        self.assertIn("original deadline", session_explanation(session, 99))
        self.assertIn("time budget is exhausted", session_explanation(session, 100))

    def test_terminal_preview_distinguishes_completed_and_exhausted_work(self):
        wait = {"issue": 1, "reasons": [
            "Saved completed/blocked execution or attempt limit; explicit recovery required"]}
        completed = explain_wait(wait, {"status": "Done", "attempts": 0}, 2)["explanation"]
        exhausted = explain_wait(wait, {"status": "Blocked", "attempts": 2}, 2)["explanation"]
        self.assertIn("recorded as completed", completed)
        self.assertNotIn("exhausted", completed)
        self.assertIn("Spent 2/2", exhausted)
        self.assertIn("exhausted", exhausted)


class ExplanationIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.IntegrationTests("runTest")
        self.fixture.setUp()
        self.f = self.fixture

    def tearDown(self):
        self.fixture.tearDown()

    def test_stopped_activity_gives_operator_flags_precedence_over_host_cancellation(self):
        f = self.f
        f.factory.processes = Processes(f.root / '.sfc/stop')
        processes = f.factory.processes
        processes.shared_stop_path = common_dir(f.root) / 'sfc-stop'
        processes.cancel_check = lambda: True
        card = f.gh.items[0]
        f.factory.stopped_activity(card)
        self.assertIn('host interrupted', f.gh.activities['item-1'][1])
        for stop_path in (processes.stop_path, processes.shared_stop_path):
            with self.subTest(stop_path=stop_path.name):
                stop_path.parent.mkdir(parents=True, exist_ok=True)
                stop_path.touch()
                f.factory.stopped_activity(card)
                self.assertIn('Stopped', f.gh.activities['item-1'][0])
                self.assertEqual(f.gh.activities['item-1'][1], outcome_reason('stopped'))
                self.assertNotIn('host interrupted', f.gh.activities['item-1'][1])
                self.assertTrue(stop_path.exists())
                stop_path.unlink()
        self.assertFalse(f.proc.lanes)
        self.assertFalse(f.gh.merges)

    def test_observed_1024_line_pr_retains_400_cap_and_all_review_gates(self):
        f = self.f
        f.c["merge_policy"] = "auto"
        f.factory = Factory(f.root, f.c, f.gh, f.proc)
        original = f.gh.pr
        with patch.object(f.gh, "pr", side_effect=lambda number: {**original(number), "additions": 800, "deletions": 224}):
            result = f.factory.run(allow_merge=True)
        self.assertEqual(result["outcome"], "waiting")
        self.assertEqual(f.proc.lanes, ["build", "qa", "security", "review"])
        self.assertEqual(f.gh.items[0]["status"], "Blocked")
        self.assertFalse(f.gh.merges)
        self.assertEqual(f.factory.c["auto_max_lines"], 400)
        text = f.gh.activities["item-1"][1]
        for value in ("PR #101", "1,024 changed lines", "limit: 400", "human merge decision", "limit stays unchanged"):
            self.assertIn(value, text)
        suffix = " Project verification: 1 executed, 0 reused."
        self.assertTrue(text.endswith(suffix))
        self.assertEqual(f.gh.notes[-1].strip(), "SFC: " + text.removesuffix(suffix))
        record = f.factory.record(fixtures.issue())
        self.assertEqual(record["handoff"], record["head"])
        self.assertTrue(f.factory.pinned(fixtures.issue(), record["head"], record["base"]))
        before = list(f.gh.activity_writes)
        output = io.StringIO()
        with patch.object(sfc, "Factory", return_value=f.factory), redirect_stdout(output):
            self.assertEqual(sfc.main(["--root", str(f.root), "status"]), 0)
        status = json.loads(output.getvalue())
        self.assertEqual(status["cards"][0]["recorded_activity"][1], text)
        self.assertEqual(f.gh.activity_writes, before)

    def test_dependency_preview_and_local_wait_agree_without_workers_or_writes(self):
        f = self.f
        f.gh.items[0]["body"] += "\n## Blocked by\n- #2\n"
        f.gh.items.append(fixtures.issue(2, status="Backlog"))
        preview = f.factory.run(dry_run=True)
        self.assertFalse(f.gh.writes)
        self.assertFalse(f.gh.activity_writes)
        self.assertFalse(preview["selected"])
        self.assertIn('Open prerequisites: #2', preview['waiting'][0]['reasons'])
        f.factory.run()
        self.assertEqual(preview["waiting"][0]["explanation"], f.factory.state['waiting'][0]['explanation'])
        self.assertIn("#2", f.factory.state['waiting'][0]['explanation'])
        self.assertEqual(f.gh.writes + f.gh.activity_writes + f.gh.notes, [])
        self.assertFalse(f.proc.lanes)
        self.assertEqual(f.gh.items[0]["status"], "Ready")

    def test_missing_failed_pending_checks_remain_gated_after_review(self):
        f = self.f
        f.c["merge_policy"] = "auto"
        f.factory = Factory(f.root, f.c, f.gh, f.proc)
        card, wt = f.prepare_review()
        f.factory.allow_merge = True
        for checks, expected in (([], "No GitHub checks"),
                                 ([{"state": "FAILURE", "description": "PRIVATE"}], "check failed"),
                                 ([{"status": "IN_PROGRESS"}], "still pending")):
            with self.subTest(checks=checks):
                f.gh.ci = checks
                f.factory.review(card, wt)
                self.assertIn(expected, f.gh.activities["item-1"][1])
                self.assertIn("Waiting", f.gh.activities["item-1"][0])
                self.assertNotIn("PRIVATE", str(f.gh.activity_writes))
                self.assertEqual(card["status"], "Review")
                self.assertFalse(f.gh.merges)
        self.assertEqual(f.proc.lanes, ["build", "qa", "security", "review"])

    def test_malformed_criteria_wait_before_claim_and_do_not_publish_issue_prose(self):
        f = self.f
        f.gh.items[0]["body"] = "private unrelated issue detail"
        f.factory.run()
        self.assertEqual(f.gh.items[0]["status"], "Ready")
        self.assertFalse(f.proc.lanes)
        text = f.factory.state['waiting'][0]['explanation']
        self.assertIn("Correct the issue", text)
        self.assertNotIn("private", text)
        self.assertEqual(f.gh.writes + f.gh.activity_writes + f.gh.notes, [])

    def test_malformed_output_and_exhausted_dispatch_budget_never_gain_a_retry(self):
        f = self.f
        f.c["max_attempts"] = 1
        f.factory = Factory(f.root, f.c, f.gh, f.proc)
        f.proc.malformed = True
        with self.assertRaisesRegex(Halt, "Malformed"):
            f.factory.run()
        self.assertIn("Worker output did not meet", f.gh.activities["item-1"][1])
        saved = f.factory.record(fixtures.issue())
        wt = saved["worktree"]
        folders = list(f.factory.run_dir.glob("issue-1/build-*"))
        self.assertTrue(folders)
        with self.assertRaisesRegex(Halt, "budget exhausted"):
            f.factory.run()
        self.assertEqual(f.proc.lanes, ["build"])
        self.assertIn("Spent 1/1", f.gh.activities["item-1"][1])
        self.assertIn("exhausted", f.gh.activities["item-1"][1])
        self.assertTrue(Path(wt).is_dir())
        self.assertTrue((folders[0] / "failure.json").exists())
        self.assertFalse(f.gh.notes)
        self.assertFalse(f.gh.merges)

    def test_worker_prose_stays_private_in_failed_and_blocked_handoffs(self):
        f = self.f
        f.factory.run_dir = f.root / ".sfc/test-run"
        f.factory.run_dir.mkdir(parents=True)
        f.gh.items[0]["status"] = "Building"
        card = f.gh.items[0]
        for verdict, expected in (("blocked", "Waiting"), ("fail", "Failed")):
            f.factory.fail(card, {"verdict": verdict, "summary": "PRIVATE token command log detail"})
            self.assertIn(expected, f.gh.activities["item-1"][0])
            self.assertNotIn("PRIVATE", f.gh.notes[-1])
        self.assertEqual(card["status"], "Blocked")
        self.assertIn("Spent 2/2", f.gh.activities["item-1"][1])
        self.assertIn("PRIVATE", f.factory.record(card)["feedback"])

    def test_exhausted_blocked_verdict_and_checks_report_spent_attempts(self):
        f = self.f
        f.factory.run_dir = f.root / ".sfc/test-run"
        f.factory.run_dir.mkdir(parents=True)
        card = f.gh.items[0]
        f.factory.record(card)["attempts"] = 1
        f.factory.activity.report(card, "Failed", "check_failed")
        f.factory.fail(card, {"verdict": "blocked", "summary": "PRIVATE failure"})
        text = f.gh.activities["item-1"][1]
        self.assertIn("Failed", f.gh.activities["item-1"][0])
        self.assertIn("Project checks failed", text)
        self.assertIn("Spent 2/2", text)
        self.assertIn("exhausted", text)
        self.assertEqual(f.gh.notes[-1].strip(), "SFC Blocked: " + text)
        self.assertEqual(card["status"], "Blocked")
        self.assertFalse(f.gh.merges)

    def test_changed_safe_details_refresh_and_human_edits_still_latch(self):
        f = self.f
        card = f.gh.items[0]
        a = f.factory.activity
        a.report(card, "Waiting", "human", reasons=[reason("size", changed=1024, limit=400, pr=101)])
        a.report(card, "Waiting", "human", reasons=[reason("size", changed=1025, limit=400, pr=101)])
        self.assertIn("1,025", f.gh.activities["item-1"][1])
        self.assertEqual(len(f.gh.activity_writes), 2)
        f.gh.activities["item-1"][1] = "Human edited this"
        with self.assertRaises(Halt):
            a.report(card, "Waiting", "ci")
        self.assertEqual(f.gh.activities["item-1"][1], "Human edited this")
        self.assertTrue(f.factory.record(card)["activity"]["error"])


if __name__ == "__main__":
    unittest.main()
