import copy
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import sfc
from sfc_lib.common import git, write_json
from test_factory import BASE_CONFIG, issue


class ReadOnlyGitHub:
    """Only board reads are available; any other adapter call fails the test."""

    def __init__(self, cards):
        self.items = copy.deepcopy(cards)
        self.reads = 0

    def priority_setup(self, apply=False):
        assert not apply
        return {"ready": False, "pending": "Explicit Priority setup required"}

    def type_setup(self, apply=False):
        assert not apply
        return {"ready": False, "pending": "Explicit work-type setup required"}

    def cards(self):
        self.reads += 1
        return copy.deepcopy(self.items)


class LintCLITests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="sfc-cli-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        git(self.root, "init")
        write_json(self.root / ".sfc/config.json", BASE_CONFIG)

    def snapshot(self):
        return {str(p.relative_to(self.root)): p.read_bytes()
                for p in self.root.rglob("*") if p.is_file()}

    def lint(self, cards, expected_status, expected_issues):
        github = ReadOnlyGitHub(cards)
        before = self.snapshot()
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("sfc_lib.factory.GitHub", return_value=github), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            status = sfc.main(["--root", str(self.root), "lint"])
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(json.loads(stdout.getvalue()), {"issues": expected_issues})
        self.assertEqual(github.reads, 1)
        self.assertEqual(github.items, cards)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(status, expected_status)

    def test_invalid_issue_fails_in_each_eligible_status(self):
        for status in ("Ready", "Building", "QA", "Review"):
            with self.subTest(status=status):
                self.lint([issue(status=status, body="Implement something")], 1,
                          [{"number": 1, "problems": ["missing ## Acceptance criteria"]}])

    def test_valid_eligible_issues_pass(self):
        cards = [issue(number=n, status=status) for n, status in
                 enumerate(("Ready", "Building", "QA", "Review"), 1)]
        self.lint(cards, 0, [{"number": n, "problems": []} for n in range(1, 5)])

    def test_empty_queue_passes(self):
        self.lint([], 0, [])

    def test_closed_issues_are_excluded_in_each_eligible_status(self):
        for status in ("Ready", "Building", "QA", "Review"):
            for body in (issue()["body"], "Implement something", "## Acceptance criteria\n"):
                with self.subTest(status=status, body=body):
                    self.lint([issue(status=status, state="CLOSED", body=body)], 0, [])

    def test_invalid_closed_and_valid_open_issues_pass(self):
        cards = []
        for number, status in enumerate(("Ready", "Building", "QA", "Review"), 1):
            cards.extend([issue(number=number, status=status, state="CLOSED",
                                body="Implement something"),
                          issue(number=number + 4, status=status)])
        self.lint(cards, 0, [{"number": n, "problems": []} for n in range(5, 9)])

    def test_invalid_open_issue_still_fails_among_closed_issues(self):
        for status in ("Ready", "Building", "QA", "Review"):
            with self.subTest(status=status):
                self.lint([issue(state="CLOSED", body="Implement something"),
                           issue(number=2, status=status, body="Implement something"),
                           issue(number=3, status="Review", state="CLOSED")], 1,
                          [{"number": 2, "problems": ["missing ## Acceptance criteria"]}])

    def test_mixed_valid_and_invalid_issues_fail(self):
        self.lint([issue(), issue(number=2, status="QA", body="Implement something"),
                   issue(number=3, status="Review")], 1,
                  [{"number": 1, "problems": []},
                   {"number": 2, "problems": ["missing ## Acceptance criteria"]},
                   {"number": 3, "problems": []}])

    def test_invalid_ineligible_issues_are_excluded(self):
        cards = [issue(number=n, status=status, body="Implement something")
                 for n, status in enumerate(("Backlog", "Blocked", "Done"), 1)]
        self.lint(cards, 0, [])
        self.lint(cards + [issue(number=4)], 0, [{"number": 4, "problems": []}])

    def test_other_reported_problems_also_fail(self):
        for body, problems in (
            ("## Acceptance criteria\n", ["acceptance criteria need observable bullet points"]),
            (issue()["body"] + "## Blocked by\n- #1\n", ["issue depends on itself"]),
            (issue()["body"] + "## Blocked by\n- other/repo#2\n",
             ["Malformed dependency: use one same-repository #issue per bullet; put notes under a separate ## heading"]),
        ):
            with self.subTest(body=body):
                self.lint([issue(body=body)], 1, [{"number": 1, "problems": problems}])

    def test_empty_task_bullets_fail_in_each_eligible_status(self):
        bodies = [f"## Acceptance criteria\n{indent}{marker} {checkbox}{trailing}"
                  for marker in ("-", "*") for checkbox in ("[ ]", "[x]", "[X]")
                  for indent in ("", "  ", "\t") for trailing in ("", "  ", "\t")]
        for status in ("Ready", "Building", "QA", "Review"):
            with self.subTest(status=status):
                cards = [issue(number=n, status=status, body=body)
                         for n, body in enumerate(bodies, 1)]
                self.lint(cards, 1, [{"number": card["number"], "problems":
                                     ["acceptance criteria need observable bullet points"]}
                                    for card in cards])

    def test_empty_bullets_with_following_content_fail(self):
        for content in ("- [ ]\nA following paragraph", "* [X]\n\nA paragraph",
                        "- [x]\n## Notes\n- A real bullet", "-\nA paragraph",
                        "- [ ]\n* [x]\n  - [X]\t"):
            with self.subTest(content=content):
                self.lint([issue(body="## Acceptance criteria\n" + content)], 1,
                          [{"number": 1, "problems":
                            ["acceptance criteria need observable bullet points"]}])

    def test_populated_and_mixed_task_bullets_pass(self):
        for bullet in ("- [ ] The value is fixed", "* [x] The value is fixed",
                       "  - [X]\tThe value is fixed\t", "* The value is fixed"):
            for prefix in ("", "- [ ]\n* [x]\n\t- [X]  \n"):
                with self.subTest(bullet=bullet, prefix=prefix):
                    self.lint([issue(body="## Acceptance criteria\n" + prefix + bullet)],
                              0, [{"number": 1, "problems": []}])

    def test_empty_task_bullets_on_excluded_issues_are_ignored(self):
        cards = [issue(number=n, status=status, state=state,
                       body="## Acceptance criteria\n- [ ]")
                 for n, (status, state) in enumerate(
                     [(status, "CLOSED") for status in ("Ready", "Building", "QA", "Review")]
                     + [(status, "OPEN") for status in ("Backlog", "Blocked", "Done")], 1)]
        self.lint(cards, 0, [])


class DiagnosticCLITests(unittest.TestCase):
    def test_status_and_dry_run_preserve_diagnostic_state(self):
        for outcome in (None, "halted", "stopped", "waiting"):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory(prefix="sfc-cli-test-") as tmp:
                root = Path(tmp)
                git(root, "init")
                write_json(root / ".sfc/config.json", BASE_CONFIG)
                state = {"run_id": "previous-run", "outcome": outcome,
                         "error": "previous diagnostic", "cards": {}}
                if outcome is not None:
                    write_json(root / ".sfc/state.json", state)
                    write_json(root / ".sfc/runs/previous-run/summary.json", state)
                    (root / ".sfc/runs/previous-run/worker.log").write_text("failure details\n")
                    (root / ".sfc/stop").touch()
                from test_pickup import PickupGitHub
                github = PickupGitHub(root, [issue()])
                before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
                for command in (["status"], ["run", "--dry-run"]):
                    stdout, stderr = io.StringIO(), io.StringIO()
                    with patch("sfc_lib.factory.GitHub", return_value=github), \
                            redirect_stdout(stdout), redirect_stderr(stderr):
                        self.assertEqual(sfc.main(["--root", str(root), *command]), 0)
                    self.assertEqual(stderr.getvalue(), "")
                    result = json.loads(stdout.getvalue())
                    if command == ["status"]:
                        self.assertEqual(result["run"]["error"],
                                         state["error"] if outcome is not None else None)
                        self.assertEqual(result["run"]["outcome"], outcome)
                        self.assertEqual(result["stop_requested"], outcome is not None)
                    else:
                        self.assertTrue(result["dry_run"])
                        self.assertEqual(result["selected"], [1])
                    self.assertEqual({p: p.read_bytes() for p in root.rglob("*") if p.is_file()}, before)
                self.assertTrue(any('items(first:100' in query for query, _ in github.reads))
                self.assertEqual(github.writes + github.activity_writes, [])
                self.assertEqual(github.items, [issue()])


if __name__ == "__main__":
    unittest.main()
