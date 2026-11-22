from __future__ import annotations

import copy
from contextlib import redirect_stdout
from functools import cache
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import sfc
from sfc_lib.common import Halt, Stopped, Processes, exclusive, git, now, run, validate_config, write_json
from sfc_lib.factory import (Factory, checks_pass, dependencies, lint_card, policy_reasons,
                             secret_problem)
from sfc_lib.github import GitHub

BASE_CONFIG = {"version": 1, "repo": "example/app", "base_branch": "main",
               "project": {"owner": "example", "number": 1}, "status_field": "SFC Status",
               "verify_commands": ["python3 -c \"from pathlib import Path; assert Path('value.txt').exists()\""],
               "merge_policy": "human", "auto_max_lines": 400, "allow_no_checks": False,
               "models": {}, "max_workers": 2, "max_cycles": 8, "max_no_progress": 3,
               "max_attempts": 2, "worker_timeout": 20, "verify_timeout": 20}


def issue(number=1, **kwargs):
    return {"number": number, "title": "Fix the value", "body": "## Acceptance criteria\n- The value is fixed\n",
            "status": "Ready", "state": "OPEN", "labels": ["bug"], "item_id": f"item-{number}",
            "url": f"https://github.com/example/app/issues/{number}",
            "id": f"issue-{number}", "repository": {"nameWithOwner": "example/app"},
            "createdAt": "2026-01-01T00:00:00Z", "priority": "P2", **kwargs}


class FakeGitHub:
    def __init__(self, root, cards=None):
        self.root = root
        self.items = cards or [issue()]
        self.prs = {}
        self.notes = []
        self.merges = []
        self.strict = True
        self.ci = [{"status": "COMPLETED", "conclusion": "SUCCESS"}]
        self.external_head = None
        self.writes = []
        self.body_updates = []
        self.activities = {}
        self.activity_writes = []
        self.github_status = {}
        self.github_status_writes = []

    def done_status_snapshot(self, card):
        item = card['item_id']
        if item not in self.github_status:
            return None
        assert next(v for v in self.items if v['item_id'] == item)['status'] == 'Done'
        options = {'Todo': 'todo', 'In Progress': 'in-progress', 'Done': 'done'}
        return {'project': 'p', 'item': item, 'field': 'status', 'options': options}, self.github_status[item]

    def write_done_status(self, scope):
        self.github_status_writes.append(scope['item'])
        self.github_status[scope['item']] = scope['options']['Done']

    def activity_snapshot(self, card):
        return {"project": "p", "fields": ["agent", "activity"]}, self.activities.get(card["item_id"], ["", ""]).copy()

    def write_activity(self, card, scope, values):
        self.activity_writes.append((card["item_id"], values.copy()))
        self.activities[card["item_id"]] = values.copy()

    def priority_setup(self, apply=False):
        assert not apply
        return {"ready": True}

    def type_setup(self, apply=False):
        assert not apply
        return {"ready": True}

    def metadata(self):
        return {"id": "p", "field": "f", "options": {}}

    def verify_repository(self):
        return None

    def call(self, *args):
        if args[:2] == ("repo", "view"):
            return {"nameWithOwner": "example/app"}
        if args[0] == "api" and args[1].endswith("/protection"):
            return {"required_status_checks": {"strict": self.strict}}
        raise AssertionError(args)

    def cards(self):
        return copy.deepcopy(self.items)

    def move(self, card, status):
        self.writes.append((card["number"], status))
        next(v for v in self.items if v["number"] == card["number"])["status"] = status
        card["status"] = status

    def issue_state(self, number):
        return next((v["state"] for v in self.items if v["number"] == number), "CLOSED")

    def comment(self, card, path):
        self.notes.append(Path(path).read_text())

    def pull_requests(self, branch):
        return [copy.deepcopy(v) for v in self.prs.values() if v["headRefName"] == branch]

    def create_pr(self, card, branch, body):
        number = 100 + card["number"]
        self.prs[number] = {"id": f"pr-node-{number}", "number": number,
                            "url": f"https://github.com/example/app/pull/{number}",
                            "state": "OPEN", "headRefName": branch, "baseRefName": "main", "isDraft": True,
                            "isCrossRepository": False, "mergeStateStatus": "CLEAN", "additions": 1, "deletions": 0,
                            "files": [{"path": "value.txt"}], "body": Path(body).read_text()}
        return {"number": number}

    def pr_state(self, number):
        return self.pr(number)["state"]

    def pr(self, number):
        value = copy.deepcopy(self.prs[number])
        value["headRefOid"] = self.external_head or git(self.root, "rev-parse", value["headRefName"])
        value["statusCheckRollup"] = self.ci
        return value

    def ready(self, number):
        self.prs[number]["isDraft"] = False

    def update_pr_body(self, number, body):
        self.body_updates.append((number, body))
        self.prs[number]["body"] = body

    def merge(self, number, sha):
        base = git(self.root, "rev-parse", "origin/main")
        tree = git(self.root, "rev-parse", sha + "^{tree}")
        squashed = git(self.root, "commit-tree", tree, "-p", base, "-m", f"Squash PR #{number}")
        git(self.root, "push", "origin", squashed + ":refs/heads/main")
        self.merges.append((number, sha))
        self.prs[number].update(state="MERGED", mergeCommit={"oid": squashed}, mergedAt=now())
        next(card for card in self.items if card["number"] == number - 100)["state"] = "CLOSED"

    def local_merge_snapshot(self, number, issue, base, head):
        pr = self.pr(number)
        repo = {"id": "repo-node", "nameWithOwner": "example/app"}
        item = next(card for card in self.items if card["number"] == issue)
        oid = git(self.root, "rev-parse", "origin/" + base.removeprefix("refs/heads/"))
        target = {"__typename": "Commit", "oid": oid, "tree": {"oid": git(self.root, "rev-parse", oid + "^{tree}")}}
        pr.update(headRepository=repo, baseRepository=repo,
                  closingIssuesReferences={"totalCount": 1, "nodes": [{"id": item["id"]}]})
        return {**repo, "isArchived": False, "base": {"id": "main", "prefix": "refs/heads/", "name": "main",
                                                     "target": target}, "pullRequest": pr,
                "issue": {key: item[key] for key in ("id", "number", "url", "state", "repository")}}

    def local_merge_checks(self, head):
        return copy.deepcopy(self.ci)


class FakeProcesses(Processes):
    def __init__(self, stop):
        super().__init__(stop)
        self.lanes = []
        self.fail_lane = None
        self.malformed = False
        self.on_lane = None
        self.events = []

    def execute(self, argv, cwd, log, timeout, prompt=None, *, observer=None, worker_events=False):
        if argv[0] != "codex":
            code = super().execute(argv, cwd, log, timeout, prompt, observer=observer, worker_events=worker_events)
            self.events.append(("check", str(cwd), git(cwd, "rev-parse", "HEAD"), argv[-1], code))
            return code
        lane = next(v for v in ("build", "qa", "security", "review") if f"an SFC {v} worker" in prompt)
        self.lanes.append(lane)
        self.events.append((lane, str(cwd), git(cwd, "rev-parse", "HEAD"), None, None))
        if observer:
            observer("started")
        if lane == "build":
            Path(cwd, "value.txt").write_text("fixed\n")
        if self.on_lane:
            self.on_lane(lane, cwd)
        result = {"verdict": "fail" if lane == self.fail_lane else "pass",
                  "summary": "Observed test defect" if lane == self.fail_lane else "Checked the requested behavior",
                  "findings": [], "evidence": ["value.txt inspected in the assigned checkout"]}
        output = Path(argv[argv.index("--output-last-message") + 1])
        write_json(output, {} if self.malformed else result)
        Path(log).write_text("Fake Codex invocation\n")
        if observer:
            observer("inactive")
        return 0


class PureTests(unittest.TestCase):
    def test_activity_timezone_validation(self):
        validate_config(BASE_CONFIG.copy())
        for zone in ("UTC", "Europe/Stockholm"):
            self.assertEqual(validate_config({**BASE_CONFIG, "activity_timezone": zone})["activity_timezone"], zone)
        for zone in (None, "", " ", True, 4, [], {}, "Invalid/Zone", "/etc/localtime", "../UTC",
                     "Europe/Stockholm\nprivate text"):
            with self.subTest(zone=zone), self.assertRaisesRegex(Halt, "activity_timezone"):
                validate_config({**BASE_CONFIG, "activity_timezone": zone})

    def test_worker_setting_validation_and_inheritance(self):
        for key in ("model", "service_tier", "reasoning_effort"):
            self.assertEqual(validate_config({**BASE_CONFIG, key: None})[key], None)
            for value in ("", " ", True, 4, [], {}):
                with self.subTest(key=key, value=value), self.assertRaises(Halt):
                    validate_config({**BASE_CONFIG, key: value})
        with self.assertRaises(Halt):
            validate_config({**BASE_CONFIG, "reasoning_effort": "unrecognized"})
        validate_config({**BASE_CONFIG, "model": "custom-model", "reasoning_effort": "medium", "service_tier": "fast"})

    def test_required_checks_and_finite_limits(self):
        for replacement in ([], [""], ["x\0y"], "test"):
            with self.subTest(replacement=replacement), self.assertRaises(Halt):
                validate_config({**BASE_CONFIG, "verify_commands": replacement})
        for key in ("max_workers", "max_cycles", "max_attempts", "worker_timeout"):
            with self.subTest(key=key), self.assertRaises(Halt):
                validate_config({**BASE_CONFIG, key: 0})

    def test_dependency_section_and_criteria(self):
        self.assertEqual(dependencies("#99 in goal\n## Blocked by\n- #2\n- #3\n## Notes\n#4"), [2, 3])
        self.assertTrue(lint_card(issue(body="Make it good")))
        self.assertTrue(lint_card(issue(body="## Acceptance criteria\n\n## Other\n- no")))
        self.assertEqual(lint_card(issue()), [])
        with self.assertRaises(Halt):
            dependencies("## Blocked by\n- other/repo#3")

    def test_dispatch_rejects_the_same_strict_dependency_bodies_as_creation(self):
        base = issue()["body"]
        for section in ("## Blocked by\n- #2\nExplanatory prose",
                        "## Blocked by\n- #2\n## Blocked by",
                        "## Blocked by: notes\n- #2",
                        "## Blocked by\n- #0"):
            with self.subTest(section=section):
                with self.assertRaises(Halt):
                    dependencies(base + section)
                self.assertTrue(lint_card(issue(body=base + section)))
        self.assertEqual(dependencies("## Blocked by"), [])
        self.assertEqual(lint_card(issue(body=base + "## Blocked by")), [])

    def test_empty_acceptance_task_bullets_fail(self):
        for marker in ("-", "*"):
            for checkbox in ("[ ]", "[x]", "[X]"):
                for indent in ("", "  ", "\t"):
                    for trailing in ("", "  ", "\t"):
                        bullet = f"{indent}{marker} {checkbox}{trailing}"
                        for ending in ("", "\n", "\r\n"):
                            with self.subTest(bullet=bullet, ending=ending):
                                self.assertEqual(lint_card(issue(body=
                                    "## Acceptance criteria\n" + bullet + ending)),
                                    ["acceptance criteria need observable bullet points"])

    def test_acceptance_bullets_cannot_borrow_following_content(self):
        for bullet in ("-", "* \t", "- [ ]", "* [x]  ", "\t- [X]\t"):
            for following in ("\nA following paragraph", "\n\nA following paragraph",
                              "\n## Notes\n- A bullet in another section",
                              "\n- [ ]\n* [X]\t"):
                with self.subTest(bullet=bullet, following=following):
                    self.assertEqual(lint_card(issue(body=
                        "## Acceptance criteria\n" + bullet + following)),
                        ["acceptance criteria need observable bullet points"])

    def test_populated_acceptance_bullets_and_mixed_lists_pass(self):
        for marker in ("-", "*"):
            for checkbox in ("", "[ ] ", "[x] ", "[X]\t"):
                bullet = f"\t{marker}\t{checkbox}The value is fixed  \n"
                for empty_bullets in ("", "- [ ]\n* [x]\t\n  - [X]  \n"):
                    with self.subTest(bullet=bullet, empty_bullets=empty_bullets):
                        self.assertEqual(lint_card(issue(body=
                            "## Acceptance criteria\n" + empty_bullets + bullet)), [])

    def test_ci_is_fail_closed(self):
        self.assertFalse(checks_pass({}))
        self.assertTrue(checks_pass({}, True))
        for check in ({"state": "PENDING"}, {"status": "IN_PROGRESS"}, {"status": "COMPLETED", "conclusion": "FAILURE"}):
            self.assertFalse(checks_pass({"statusCheckRollup": [check]}))
        self.assertTrue(checks_pass({"statusCheckRollup": [{"state": "SUCCESS"}]}))

    def test_human_sensitive_and_large_policy(self):
        self.assertTrue(policy_reasons(BASE_CONFIG, issue(), {}))
        auto = {**BASE_CONFIG, "merge_policy": "auto"}
        self.assertEqual(policy_reasons(auto, issue(), {"files": [{"path": "src/foo.py"}]}), [])
        for path in ("auth/login.py", "src/payments/index.ts", "supabase/migrations/a.sql", ".github/workflows/x.yml", "AGENTS.md"):
            self.assertTrue(policy_reasons(auto, issue(), {"files": [{"path": path}]}), path)
        self.assertTrue(policy_reasons(auto, issue(), {"additions": 401}))

    def test_secrets_do_not_leak_in_diagnostic(self):
        fake_key = "sk-" + "a" * 30
        message = secret_problem(["src/a.py"], "+key='" + fake_key + "'")
        self.assertIsNotNone(message)
        self.assertNotIn(fake_key, message)
        self.assertIsNone(secret_problem([".env.example"], "+KEY=replace-me"))
        self.assertIsNotNone(secret_problem([".env.production"], ""))



@cache
def _integration_seed():
    """Build the pristine repositories once per test process."""
    temporary = tempfile.TemporaryDirectory(prefix="sfc-test-seed-")
    root = Path(temporary.name) / "project with spaces"
    root.mkdir()
    remote = Path(temporary.name) / "origin.git"
    run(["git", "init", "--bare", str(remote)])
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "test@example.invalid")
    git(root, "config", "user.name", "SFC Test")
    Path(root, "value.txt").write_text("original\n")
    Path(root, ".gitignore").write_text(".sfc/\n__pycache__/\n")
    git(root, "add", ".")
    git(root, "commit", "-m", "initial")
    git(root, "remote", "add", "origin", remote)
    git(root, "push", "-u", "origin", "main")
    return temporary


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sfc-test-")
        self.root = Path(self.temp.name) / "project with spaces"
        self.remote = Path(self.temp.name) / "origin.git"
        seed = Path(_integration_seed().name)
        shutil.copytree(seed / self.root.name, self.root, symlinks=True)
        shutil.copytree(seed / self.remote.name, self.remote, symlinks=True)
        git(self.root, "remote", "set-url", "origin", self.remote)
        self.c = copy.deepcopy(BASE_CONFIG)
        from test_pickup import PickupGitHub
        self.gh = PickupGitHub(self.root, [issue()])
        # These fixtures also prepare individual lanes directly without claims.
        self.gh.move = lambda card, status: FakeGitHub.move(self.gh, card, status)
        self.proc = FakeProcesses(self.root / ".sfc/stop")
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        original = run
        self.patcher = patch("sfc_lib.factory.run", side_effect=lambda argv, *a, **kw:
                             subprocess.CompletedProcess(argv, 0, "codex-test", "") if argv[:2] == ["codex", "--version"] else original(argv, *a, **kw))
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.temp.cleanup()

    def run_with_worker_settings(self, settings, **run_options):
        self.c.update(settings)
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        original = self.proc.execute
        workers = []

        def capture(argv, cwd, log, timeout, prompt=None, **kwargs):
            if argv[:2] == ["codex", "exec"]:
                lane = next(v for v in ("build", "qa", "security", "review") if f"an SFC {v} worker" in prompt)
                workers.append((lane, argv))
            return original(argv, cwd, log, timeout, prompt, **kwargs)

        with patch.object(self.proc, "execute", side_effect=capture):
            self.assertEqual(self.factory.run(**run_options)["outcome"], "waiting")
        self.assertEqual([lane for lane, _ in workers], ["build", "qa", "security", "review"])
        return workers

    def test_worker_model_reasoning_and_fast_tier_reach_all_roles(self):
        workers = self.run_with_worker_settings({"model": "gpt-6.1-sol", "reasoning_effort": "high", "service_tier": "fast"})
        for lane, argv in workers:
            with self.subTest(lane=lane):
                self.assertEqual(argv[argv.index("-m") + 1], "gpt-6.1-sol")
                self.assertIn('model_reasoning_effort="high"', argv)
                self.assertIn('service_tier="fast"', argv)
                self.assertIn('approval_policy="never"', argv)
                self.assertEqual(argv[argv.index("-s") + 1], "workspace-write" if lane == "build" else "read-only")
        r = self.factory.record(issue())
        for key, value in (("model", "another-model"), ("reasoning_effort", "medium"), ("service_tier", None)):
            changed = Factory(self.root, {**self.c, key: value}, self.gh, self.proc)
            self.assertFalse(changed.pinned(issue(), r["head"], r["base"]), key)
        self.assertEqual(self.gh.merges, [])

    def test_waiting_routine_build_uses_medium_fast_with_high_standard_reviews(self):
        workers = self.run_with_worker_settings(
            {"model": "gpt-6.2-sol", "reasoning_effort": "high", "service_tier": "default",
             "worker_routing": {"routine_build_issues": [1]}},
            include_backlog=True, issues=[1], waiting=True)
        record = self.factory.record(issue())
        for lane, argv in workers:
            with self.subTest(lane=lane):
                effort, tier = ("medium", "fast") if lane == "build" else ("high", "default")
                self.assertEqual(argv[argv.index("-m") + 1], "gpt-6.2-sol")
                self.assertIn(f'model_reasoning_effort="{effort}"', argv)
                self.assertIn(f'service_tier="{tier}"', argv)
                dispatch = record["worker_output_budget"][lane]["dispatches"][0]
                self.assertEqual(dispatch["worker_settings"], {
                    "model": "gpt-6.2-sol", "reasoning_effort": effort, "service_tier": tier})
        self.assertEqual(self.gh.merges, [])

    def test_sol_worker_routing_requires_valid_profile_and_waiting_scope(self):
        from sfc_lib.worker_runtime import lane_settings

        config = {**self.c, "model": "gpt-6.2-sol", "reasoning_effort": "high", "service_tier": "default",
                  "worker_routing": {"routine_build_issues": [1]}}
        factory = Factory(self.root, config, self.gh, self.proc)
        for extra in ({"models": {"qa": "other-model"}}, {"service_tier": "fast"},
                      {"worker_routing": {"routine_build_issues": [True]}},
                      {"worker_routing": {"routine_build_issues": [1, 1]}}):
            with self.subTest(extra=extra), self.assertRaisesRegex(Halt, "worker_routing"):
                validate_config({**config, **extra})
        for scope in ({}, {"include_backlog": True, "issues": [1, 2]}):
            with self.subTest(scope=scope), self.assertRaisesRegex(Halt, "exactly one"):
                factory.run(waiting=True, **scope)
        self.assertEqual(lane_settings(config, "build", 1), {
            "model": "gpt-6.2-sol", "reasoning_effort": "medium", "service_tier": "default"})
        self.assertEqual(lane_settings(config, "build", 2, waiting=True), {
            "model": "gpt-6.2-sol", "reasoning_effort": "high", "service_tier": "fast"})

    def test_role_model_override_wins_over_worker_default(self):
        workers = self.run_with_worker_settings({"model": "gpt-6.1-sol", "reasoning_effort": "high", "service_tier": "fast",
                                                 "models": {"build": "custom-builder", "review": "custom-reviewer"}})
        for lane, argv in workers:
            expected = {"build": "custom-builder", "review": "custom-reviewer"}.get(lane, "gpt-6.1-sol")
            self.assertEqual(argv[argv.index("-m") + 1], expected)
            self.assertIn('model_reasoning_effort="high"', argv)
            self.assertIn('service_tier="fast"', argv)

    def test_existing_config_without_new_settings_keeps_cli_inheritance(self):
        for _, argv in self.run_with_worker_settings({}):
            self.assertNotIn("-m", argv)
            self.assertFalse(any(v.startswith(("model_reasoning_effort=", "service_tier=")) for v in argv))

    def test_explicit_null_settings_inherit_cli_with_optional_role_override(self):
        workers = self.run_with_worker_settings({"model": None, "reasoning_effort": None, "service_tier": None,
                                                 "models": {"qa": "custom-qa"}})
        for lane, argv in workers:
            if lane == "qa":
                self.assertEqual(argv[argv.index("-m") + 1], "custom-qa")
            else:
                self.assertNotIn("-m", argv)
            self.assertFalse(any(v.startswith(("model_reasoning_effort=", "service_tier=")) for v in argv))

    def test_complete_human_flow_and_external_merge_reconciliation(self):
        # An unrelated uncommitted user edit remains exactly as it was.
        Path(self.root, "personal.txt").write_text("do not touch\n")
        outcome = self.factory.run()
        self.assertEqual(outcome["outcome"], "waiting")
        self.assertEqual(self.proc.lanes, ["build", "qa", "security", "review"])
        self.assertEqual(self.gh.items[0]["status"], "Blocked")
        self.assertFalse(self.gh.prs[101]["isDraft"])
        self.assertEqual(self.gh.merges, [])
        self.assertEqual(Path(self.root, "value.txt").read_text(), "original\n")
        self.assertEqual(Path(self.root, "personal.txt").read_text(), "do not touch\n")
        r = self.factory.state["cards"]["1"]
        self.assertTrue(self.factory.pinned(issue(), r["head"], r["base"]))
        self.gh.prs[101]["state"] = "MERGED"
        self.gh.items[0]["state"] = "CLOSED"
        # A MERGED flag and head alone cannot prove the intended-base outcome.
        # Priority-aware Ready admission now saves the identity. The thin fake
        # still cannot establish the complete remote merge proof; fail closed.
        def unavailable_snapshot(*_):
            raise Halt('Complete remote snapshot unavailable in this fixture')
        self.gh.local_merge_snapshot = unavailable_snapshot
        with self.assertRaisesRegex(Halt, 'snapshot unavailable'):
            self.factory.run()
        self.assertEqual(self.gh.items[0]["status"], "Blocked")

    def test_auto_merge_requires_checks_and_head_pins(self):
        self.c["merge_policy"] = "auto"
        self.gh.github_status['item-1'] = 'in-progress'
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        self.assertEqual(self.factory.run(allow_merge=True)["outcome"], "drained")
        self.assertEqual(len(self.gh.merges), 1)
        self.assertEqual(self.gh.items[0]["status"], "Done")
        self.assertEqual(self.gh.github_status['item-1'], 'done')
        self.assertFalse(Path(self.factory.dir, "worktrees/issue-1").exists())
        self.assertEqual([e[0] for e in self.proc.events],
                         ["build", "check", "qa", "security", "review", "check"])
        record = self.factory.record(issue())
        checks = [e for e in self.proc.events if e[0] == "check"]
        self.assertEqual([e[2:] for e in checks],
                         [(record["head"], self.c["verify_commands"][0], 0)] * 2)
        self.assertEqual(checks[0][1], str(self.factory.dir / "worktrees/issue-1"))
        self.assertEqual(checks[1][1], record["verification"]["worktree"])
        self.assertIn("gate-1-", checks[1][1])

    def test_unconfirmed_protected_merge_precedes_ordinary_restart_activity(self):
        self.c['merge_policy'] = 'auto'
        self.factory = Factory(self.root, self.c, self.gh, self.proc)

        def lost(number, head):
            self.factory.activity.report(self.gh.items[0], 'Running', 'prepare')
            raise Halt('fixture response lost')

        with patch.object(self.gh, 'merge', side_effect=lost) as merge:
            with self.assertRaises(Halt):
                self.factory.run(allow_merge=True)
            record = self.factory.record(self.gh.items[0])
            self.assertEqual(record['activity']['state'], 'Running')
            board_writes = self.gh.writes.copy()
            activity_writes = self.gh.activity_writes.copy()
            self.factory = Factory(self.root, self.c, self.gh, self.proc)
            with self.assertRaises(Halt):
                self.factory.run(allow_merge=True)
        self.assertEqual(merge.call_count, 1)
        self.assertEqual(self.gh.writes, board_writes)
        self.assertEqual(self.gh.activity_writes, activity_writes)

    def test_supervised_build_prompt_and_skill_assign_verification_ownership(self):
        self.factory.run()
        prompt = next(self.factory.run_dir.glob("issue-1/build-*/prompt.md")).read_text()
        skill = Path(__file__).resolve().parents[1] / "skills/sfc-build/SKILL.md"
        for source in (prompt, skill.read_text()):
            text = " ".join(source.split())
            for clause in ("reproduce relevant failures", "meaningful focused checks",
                           "supervisor owns full execution", "before independent QA/security",
                           "final merge candidate", "Do not routinely execute those commands merely",
                           "Project instructions remain authoritative",
                           "honor explicit builder-specific or full-suite requirements",
                           "needed to reproduce the selected defect", "explain the reason",
                           "before overall factory completion", "not a sandbox or proof",
                           "deferred supervisor-owned full verification",
                           "Never claim deferred checks passed", "ready for independent QA"):
                with self.subTest(source="prompt" if source == prompt else "skill", clause=clause):
                    self.assertIn(clause, text)
        self.assertIn(json.dumps(self.c["verify_commands"]), prompt)
        self.assertIn("Standalone use retains the project's and user's existing verification obligations",
                      " ".join(skill.read_text().split()))
        for lane in ("qa", "security", "review"):
            other = next(self.factory.run_dir.glob(f"issue-1/{lane}-*/prompt.md")).read_text()
            self.assertNotIn("Supervised build verification ownership", other)
            self.assertIn("Supervisor project-check evidence", other)

    def test_reviewed_handoff_replaces_pending_summary_before_ready(self):
        self.gh.ci = []
        original_ready = self.gh.ready

        def ready(number):
            self.assertEqual(len(self.gh.body_updates), 1)
            self.assertIn("SFC verification completed", self.gh.prs[number]["body"])
            original_ready(number)

        with patch.object(self.gh, "ready", side_effect=ready):
            self.factory.run()
        r = self.factory.record(issue())
        body = self.gh.prs[101]["body"]
        self.assertTrue(body.startswith("Closes #1\n\nFix the value\n\n"))
        self.assertNotIn("SFC verification is in progress.", body)
        for value in (r["head"], r["base"], "QA: pass", "Security: pass", "Final review: pass",
                      "Local project checks", "exit 0", "GitHub CI", "No human approval or merge"):
            self.assertIn(value, body)
        self.assertIn("Configured check 1", body)
        self.assertNotIn(self.c["verify_commands"][0], body)
        self.assertNotIn(str(self.root), body)
        self.assertEqual(r["handoff"], r["head"])
        self.assertEqual(self.gh.merges, [])

    def prepare_review(self):
        self.factory.run_dir = self.factory.dir / "runs" / "summary-test"
        self.factory.run_dir.mkdir(parents=True)
        card = self.gh.cards()[0]
        self.factory.move(card, "Building", "test claim")
        wt = self.factory.worktree(card)
        self.assertTrue(self.factory.build(card, wt))
        self.assertTrue(self.factory.qa(card, wt))
        return card, wt

    def test_summary_preserves_surroundings_and_is_idempotent(self):
        # Private output must originate in the execution; changing an accepted
        # raw log now correctly invalidates its immutable evidence.
        self.c['verify_commands'][0] += "; printf 'Private raw output: test-only-password\\n'"
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        card, wt = self.prepare_review()
        r = self.factory.record(card)
        r["receipts"]["qa"]["result"]["summary"] = "Private worker output: test-only-password"
        prefix = "Human introduction\n\nCloses #1\n\n"
        suffix = "\n\n## Human notes\nKeep this wording.\n"
        self.gh.prs[101]["body"] = prefix + "SFC verification is in progress." + suffix
        self.factory.review(card, wt)
        body = self.gh.prs[101]["body"]
        self.assertTrue(body.startswith(prefix))
        self.assertTrue(body.endswith(suffix))
        self.assertNotIn("test-only-password", body)
        self.assertNotIn(str(self.factory.run_dir), body)
        self.factory.review(card, wt)
        self.assertEqual(self.gh.prs[101]["body"], body)
        self.assertEqual(len(self.gh.body_updates), 1)
        self.assertEqual(self.proc.lanes.count("review"), 1)

    def advance_review_base(self, card, wt):
        Path(self.root, "later-base.txt").write_text("A later base\n")
        git(self.root, "add", "later-base.txt")
        git(self.root, "commit", "-m", "advance fixture base")
        git(self.root, "push", "origin", "main")
        self.factory.move(card, "QA", "Recheck against later fixture base")
        self.assertTrue(self.factory.qa(card, wt))

    def test_summary_refreshes_unchanged_generated_text_for_new_head_and_base(self):
        card, wt = self.prepare_review()
        prefix, suffix = "Closes #1\n\nHuman intro\n\n", "\n\nHuman notes.\n"
        self.gh.prs[101]["body"] = prefix + "SFC verification is in progress." + suffix
        self.factory.review(card, wt)
        first = copy.deepcopy(self.factory.record(card))
        self.advance_review_base(card, wt)
        self.factory.review(card, wt)
        current = self.factory.record(card)
        body = self.gh.prs[101]["body"]
        self.assertNotEqual(first["head"], current["head"])
        self.assertNotEqual(first["base"], current["base"])
        self.assertIn(current["head"], body)
        self.assertIn(current["base"], body)
        self.assertNotIn(first["head"], body)
        self.assertNotIn(first["base"], body)
        self.assertEqual(current["handoff"], current["head"])
        self.assertTrue(body.startswith(prefix) and body.endswith(suffix))
        self.assertEqual(body.count("SFC verification completed."), 1)
        self.assertEqual(len(self.gh.body_updates), 2)
        self.factory.review(card, wt)
        self.assertEqual(len(self.gh.body_updates), 2)
        self.assertEqual(self.gh.merges, [])

    def test_summary_preserves_edited_or_ambiguous_completed_text(self):
        card, wt = self.prepare_review()
        self.factory.review(card, wt)
        generated = self.gh.prs[101]["body"]
        self.advance_review_base(card, wt)
        for custom in (generated.replace("No human approval", "Human edited: no human approval"),
                       generated + "\n\n" + generated):
            with self.subTest(body=custom):
                self.gh.prs[101]["body"] = custom
                self.factory.review(card, wt)
                self.assertEqual(self.gh.prs[101]["body"], custom)
                self.assertEqual(len(self.gh.body_updates), 1)

    def test_summary_preserves_custom_or_ambiguous_body(self):
        card, wt = self.prepare_review()
        bodies = ("Closes #1\n\nHuman verification notes only.\n",
                  "Closes #1\n\nSFC verification is in progress. Edited by human.\n",
                  "Closes #1\n\n> SFC verification is in progress.\n",
                  "Closes #1\n\nContext:\nSFC verification is in progress.\n",
                  "Closes #1\n\nSFC verification is in progress.\nAdditional context.\n",
                  "Closes #1\n\nSFC verification is in progress.\n\nSFC verification is in progress.\n",
                  "")
        for body in bodies:
            with self.subTest(body=body):
                self.gh.prs[101]["body"] = body
                self.factory.review(card, wt)
                self.assertEqual(self.gh.prs[101]["body"], body)
                self.assertEqual(self.gh.body_updates, [])

    def test_summary_rejects_missing_failed_or_stale_evidence(self):
        card, wt = self.prepare_review()
        r = self.factory.record(card)
        self.factory.lane(card, "review", wt, r["base"])
        original = copy.deepcopy(r)
        invalid = []
        for key in ("head", "base", "task_digest", "config_digest"):
            receipt = copy.deepcopy(original)
            receipt["verification"][key] = "stale"
            invalid.append(receipt)
            for lane in ("qa", "security", "review"):
                receipt = copy.deepcopy(original)
                receipt["receipts"][lane][key] = "stale"
                invalid.append(receipt)
        for lane in ("qa", "security", "review"):
            for change in ("missing", "failed", "findings"):
                receipt = copy.deepcopy(original)
                if change == "missing":
                    del receipt["receipts"][lane]
                elif change == "failed":
                    receipt["receipts"][lane]["result"]["verdict"] = "fail"
                else:
                    receipt["receipts"][lane]["result"]["findings"] = [
                        {"file": "value.txt", "line": 1, "severity": "high", "message": "Unresolved"}]
                invalid.append(receipt)
        for change in ("missing", "failed", "checks", "exit_code", "command", "log"):
            receipt = copy.deepcopy(original)
            if change == "missing":
                del receipt["verification"]
            elif change == "failed":
                receipt["verification"]["passed"] = False
            elif change == "checks":
                receipt["verification"]["checks"] = []
            else:
                receipt["verification"]["checks"][0][change] = 1 if change == "exit_code" else "missing"
            invalid.append(receipt)
        for index, receipt in enumerate(invalid):
            with self.subTest(case=index):
                self.factory.state["cards"]["1"] = receipt
                # Keep the rejected snapshot fixed; normal review may otherwise
                # legitimately rerun a stale final lane and repair that evidence.
                with patch.object(self.factory, "lane", return_value={"verdict": "pass"}):
                    try:
                        self.factory.review(card, wt)
                    except Halt:
                        pass
                self.assertEqual(self.gh.body_updates, [])
                self.assertTrue(self.gh.prs[101]["isDraft"])
                self.assertNotIn("handoff", self.factory.record(card))

    def test_summary_rejects_identity_changes_during_review(self):
        card, wt = self.prepare_review()
        original_card = copy.deepcopy(card)
        original_pr = copy.deepcopy(self.gh.prs[101])
        for target, key, value in (("issue", "id", "replacement"), ("issue", "item_id", "replacement"),
                                   ("issue", "url", "https://github.com/other/app/issues/1"),
                                   ("issue", "repository", {"nameWithOwner": "other/app"}),
                                   ("pr", "number", 102), ("pr", "headRefName", "other"),
                                   ("pr", "baseRefName", "other"), ("pr", "state", "CLOSED"),
                                   ("pr", "isCrossRepository", True), ("pr", "headRefOid", "0" * 40)):
            with self.subTest(target=target, key=key):
                self.gh.items[0] = copy.deepcopy(original_card)
                self.gh.prs[101] = copy.deepcopy(original_pr)
                self.gh.external_head = None
                self.factory.record(card)["receipts"].pop("review", None)

                def change(lane, cwd):
                    if lane == "review":
                        if key == "headRefOid":
                            self.gh.external_head = value
                        else:
                            (self.gh.items[0] if target == "issue" else self.gh.prs[101])[key] = value

                self.proc.on_lane = change
                with self.assertRaises(Halt):
                    self.factory.review(card, wt)
                self.assertEqual(self.gh.body_updates, [])
                self.assertTrue(self.gh.prs[101]["isDraft"])

    def test_summary_update_failure_preserves_draft_and_handoff_state(self):
        card, wt = self.prepare_review()
        with patch.object(self.gh, "update_pr_body", side_effect=Halt("write failed")) as update:
            with self.assertRaisesRegex(Halt, "write failed"):
                self.factory.review(card, wt)
        update.assert_called_once()
        self.assertNotIn("pr_summary", self.factory.record(card))
        self.assertTrue(self.gh.prs[101]["isDraft"])
        self.assertNotIn("handoff", self.factory.record(card))

    def test_summary_stop_immediately_before_update_prevents_write(self):
        card, wt = self.prepare_review()
        original_event = self.factory.event

        def stop(card, detail):
            original_event(card, detail)
            if "verification summary update requested" in detail:
                self.proc.stop_path.touch()

        with patch.object(self.factory, "event", side_effect=stop):
            with self.assertRaises(Stopped):
                self.factory.review(card, wt)
        self.assertEqual(self.gh.body_updates, [])
        self.assertTrue(self.gh.prs[101]["isDraft"])

    def test_summary_base_movement_during_review_requires_new_evidence(self):
        card, wt = self.prepare_review()

        def move_base(lane, cwd):
            if lane == "review":
                Path(self.root, "new-base.txt").write_text("new base")
                git(self.root, "add", "new-base.txt")
                git(self.root, "commit", "-m", "advance test base")
                git(self.root, "push", "origin", "main")

        self.proc.on_lane = move_base
        self.factory.review(card, wt)
        self.assertEqual(card["status"], "QA")
        self.assertEqual(self.gh.body_updates, [])
        self.assertTrue(self.gh.prs[101]["isDraft"])

    def test_summary_config_change_during_review_prevents_write(self):
        card, wt = self.prepare_review()

        def change(lane, cwd):
            if lane == "review":
                write_json(self.factory.dir / "config.json", {**self.c, "verify_commands": ["exit 1"]})

        self.proc.on_lane = change
        with self.assertRaisesRegex(Halt, "configuration changed"):
            self.factory.review(card, wt)
        self.assertEqual(self.gh.body_updates, [])
        self.assertTrue(self.gh.prs[101]["isDraft"])

    def assert_private_commands_not_published(self, commands, private_fragments):
        self.c["verify_commands"] = commands
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        original_execute = self.proc.execute
        executed = []

        def execute(argv, cwd, log, timeout, prompt=None, **kwargs):
            if argv[0] == "codex":
                return original_execute(argv, cwd, log, timeout, prompt, **kwargs)
            # These commands are synthetic private fixtures: record evidence,
            # never execute them or access any external service/path.
            executed.append(argv[-1])
            Path(log).write_text("Private raw output: fixture-log-value\n")
            return 0

        with patch.object(self.proc, "execute", side_effect=execute):
            card, wt = self.prepare_review()
            prefix = "Closes #1\n\nHuman introduction\n\n"
            suffix = "\n\nHuman notes remain.\n"
            self.gh.prs[101]["body"] = prefix + "SFC verification is in progress." + suffix
            self.factory.review(card, wt)
        self.assertEqual(executed, commands)
        body = self.gh.prs[101]["body"]
        self.assertTrue(body.startswith(prefix) and body.endswith(suffix))
        self.assertEqual(len(self.gh.body_updates), 1)
        for fragment in [*commands, *private_fragments, "fixture-log-value", str(self.root)]:
            self.assertNotIn(fragment, body)
        for index in range(1, len(commands) + 1):
            self.assertIn(f"Configured check {index}: pass (exit 0).", body)
        self.assertEqual([c["command"] for c in self.factory.record(card)["verification"]["checks"]], commands)
        self.assertFalse(self.gh.prs[101]["isDraft"])
        self.assertEqual(self.factory.record(card)["handoff"], self.factory.record(card)["head"])
        self.factory.review(card, wt)
        self.assertEqual(len(self.gh.body_updates), 1)
        self.assertEqual(self.gh.prs[101]["body"], body)
        self.assertEqual(self.gh.merges, [])

    def test_summary_identifies_public_checks_without_command_arguments(self):
        commands = ["python3 -m unittest discover -s tests -v", "python3 scripts/validate_plugin.py"]
        self.assert_private_commands_not_published(commands, [])
        body = self.gh.prs[101]["body"]
        self.assertIn("Python unittest suite (unittest discover)", body)
        self.assertIn("SFC plugin manifest and skill validation", body)

    def test_summary_omits_attached_curl_credentials(self):
        self.assert_private_commands_not_published(
            ["curl --fail -ufixture:synthetic-value localhost:8080/health"],
            ["synthetic-value", "fixture", "localhost"])

    def test_summary_omits_at_prefixed_absolute_evidence_path(self):
        self.assert_private_commands_not_published(
            ["curl --fail --data-binary @/Users/fixture/private/evidence/report.json localhost:8080/check"],
            ["/Users/fixture", "report.json", "localhost"])

    def test_summary_omits_all_command_text_without_guessing_private_syntax(self):
        self.assert_private_commands_not_published([
            "test -n sk-" + "a" * 30,
            "DATABASE_URL=postgresql://fixture_user:synthetic_password@localhost/test python3 -m unittest",
            "check --report /Users/fixture/private/evidence/report.json",
            "check -H 'Authorization: Bearer synthetic-private-value'",
            r"check --report 'C:\Users\fixture\report.json'",
            "echo completely_unlabelled_private_value",
            "check 'unterminated",
            "check $(cat private-file)",
        ], ["synthetic_password", "synthetic-private-value", "unlabelled_private_value",
            "private-file", "Authorization", "fixture"])

    def test_failed_security_bounces_and_stops_at_attempt_limit(self):
        self.proc.fail_lane = "security"
        self.factory.run()
        self.assertEqual(self.gh.items[0]["status"], "Blocked")
        self.assertEqual(self.factory.state["cards"]["1"]["attempts"], 2)
        self.assertEqual(len(self.gh.prs), 1)
        self.assertNotIn("review", self.proc.lanes)
        self.assertEqual(self.gh.merges, [])
        self.assertEqual(self.gh.body_updates, [])

    def test_malformed_worker_halts_and_resumes_saved_work(self):
        self.proc.malformed = True
        with self.assertRaises(Halt):
            self.factory.run()
        self.assertEqual(self.factory.state["outcome"], "halted")
        self.assertTrue(Path(self.factory.state["cards"]["1"]["worktree"]).exists())
        self.assertEqual(self.gh.items[0]["status"], "Building")
        self.proc.malformed = False
        self.factory.run()
        self.assertEqual(self.gh.items[0]["status"], "Blocked")
        self.assertEqual(len(self.gh.prs), 1)

    def test_run_error_lifecycle_preserves_archived_diagnostics(self):
        write_json(self.root / ".sfc/config.json", self.c)

        def status():
            stdout = io.StringIO()
            with patch("sfc_lib.factory.GitHub", return_value=self.gh), redirect_stdout(stdout):
                self.assertEqual(sfc.main(["--root", str(self.root), "status"]), 0)
            return json.loads(stdout.getvalue())["run"]

        def snapshot(folder):
            return {p: p.read_bytes() for p in folder.rglob("*") if p.is_file()}

        # Fail after Build and QA have produced real fixture receipts and logs.
        self.proc.on_lane = lambda lane, cwd: setattr(self.proc, "malformed", lane == "security")
        with self.assertRaisesRegex(Halt, "Malformed security result"):
            self.factory.run()
        failed = copy.deepcopy(self.factory.state)
        archived = snapshot(self.factory.run_dir)
        self.assertEqual(failed["outcome"], "halted")
        self.assertEqual(status()["error"], failed["error"])
        claim = failed["cards"]["1"]
        self.assertEqual(set(claim["receipts"]), {"qa"})
        worktree = Path(claim["worktree"])
        worktree_before = snapshot(worktree)

        # A rejected preflight has not started a new run and keeps the old error.
        with patch.object(self.gh, "verify_repository", side_effect=Halt("preflight failed")):
            with self.assertRaisesRegex(Halt, "preflight failed"):
                self.factory.run()
        self.assertEqual(json.loads(self.factory.state_path.read_text()), failed)

        # Resume from disk, checking the initial persisted state before any work.
        self.proc.on_lane = None
        self.proc.malformed = False
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        cards = self.gh.cards

        def observe_start():
            current = json.loads(self.factory.state_path.read_text())
            self.assertEqual(current["outcome"], "running")
            self.assertNotIn("error", current)
            self.assertEqual(current["cards"], failed["cards"])
            self.assertEqual(snapshot(worktree), worktree_before)
            self.gh.cards.side_effect = cards
            return cards()

        with patch.object(self.gh, "cards", side_effect=observe_start):
            self.assertEqual(self.factory.run()["outcome"], "waiting")
        self.assertNotEqual(self.factory.state["run_id"], failed["run_id"])
        self.assertIsNone(status()["error"])
        recovered = self.factory.state["cards"]["1"]
        for key in ("worktree", "branch", "attempts"):
            self.assertEqual(recovered[key], claim[key])
        self.assertEqual(recovered["history"][:len(claim["history"])], claim["history"])
        self.assertTrue(worktree.is_dir())
        archived.update(snapshot(self.factory.run_dir))

        # A queue containing only completed work drains without a stale error.
        self.gh.prs[101]["state"] = "MERGED"
        self.gh.items[0]["state"] = "CLOSED"
        self.gh.items[0]["status"] = "Done"
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        self.assertEqual(self.factory.run()["outcome"], "drained")
        self.assertNotIn("error", self.factory.state)
        self.assertIsNone(status()["error"])
        archived.update(snapshot(self.factory.run_dir))

        # A later failure owns its error; none of the earlier runs are rewritten.
        self.gh.items.append(issue(2))
        self.proc.malformed = True
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        with self.assertRaisesRegex(Halt, "Malformed build result"):
            self.factory.run()
        self.assertEqual(self.factory.state["outcome"], "halted")
        self.assertNotEqual(self.factory.state["error"], failed["error"])
        self.assertEqual(status()["error"], self.factory.state["error"])
        summary = json.loads((self.factory.run_dir / "summary.json").read_text())
        self.assertEqual(summary, self.factory.state)
        for path, content in archived.items():
            self.assertEqual(path.read_bytes(), content, str(path))

    def test_failed_verification_prevents_all_review_and_merge(self):
        self.c["verify_commands"] = ["exit 2"]
        self.c["merge_policy"] = "auto"
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        self.factory.run(allow_merge=True)
        self.assertEqual(self.gh.items[0]["status"], "Blocked")
        self.assertEqual(self.proc.lanes, ["build", "build"])
        self.assertEqual(self.gh.merges, [])
        self.assertEqual(self.gh.body_updates, [])
        self.assertEqual([e[0] for e in self.proc.events], ["build", "check", "build", "check"])
        self.assertEqual([e[4] for e in self.proc.events if e[0] == "check"], [2, 2])

    def test_incomplete_ci_waits_without_repeating_paid_review(self):
        self.c["merge_policy"] = "auto"
        self.gh.ci = [{"state": "PENDING"}]
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        result = self.factory.run(allow_merge=True)
        self.assertEqual(result["outcome"], "waiting")
        self.assertEqual(self.proc.lanes.count("review"), 1)
        self.assertEqual(self.gh.merges, [])

    def test_auto_policy_without_run_authority_never_merges(self):
        self.c["merge_policy"] = "auto"
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        self.factory.run()
        self.assertEqual(self.gh.merges, [])
        self.assertEqual(self.gh.items[0]["status"], "Blocked")

    def test_non_strict_base_protection_blocks_auto_merge(self):
        self.c["merge_policy"] = "auto"
        self.gh.strict = False
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        self.factory.run(allow_merge=True)
        self.assertEqual(self.gh.items[0]["status"], "Blocked")
        self.assertEqual(self.gh.merges, [])

    def test_qa_only_creates_no_pr_or_implementation(self):
        self.gh.items[0]["labels"] = ["qa"]
        self.gh.github_status['item-1'] = 'in-progress'
        self.factory.run()
        self.assertEqual(self.proc.lanes, ["qa", "security", "review"])
        self.assertEqual(self.gh.items[0]["status"], "Done")
        self.assertEqual(self.gh.github_status['item-1'], 'done')
        self.assertEqual(self.gh.github_status_writes, ['item-1'])
        self.assertEqual(self.factory.record(self.gh.items[0])['done_status']['state'], 'verified')
        self.assertEqual(self.gh.prs, {})

    def test_done_status_lost_response_uses_readback(self):
        from sfc_lib.rate_limit import ConnectionReset

        card = self.gh.items[0]
        self.gh.github_status[card['item_id']] = 'in-progress'
        write = self.gh.write_done_status

        def lost(scope):
            write(scope)
            raise ConnectionReset('lost after write')

        with patch.object(self.gh, 'write_done_status', side_effect=lost):
            self.factory.move(card, 'Done', 'Verified completion')
        restarted = Factory(self.root, self.c, self.gh, self.proc)
        restarted.sync_done_status(card, guard=restarted.ensure_running)
        self.assertEqual(self.gh.github_status_writes, [card['item_id']])
        self.assertEqual(restarted.record(card)['done_status']['state'], 'verified')

    def test_done_status_preserves_edit_after_intent_and_never_retries(self):
        card = self.gh.items[0]
        self.gh.github_status[card['item_id']] = 'in-progress'
        snapshot = self.gh.done_status_snapshot

        def edited(card):
            if self.factory.record(card).get('done_status', {}).get('state') == 'intent':
                self.gh.github_status[card['item_id']] = 'todo'
            return snapshot(card)

        with patch.object(self.gh, 'done_status_snapshot', side_effect=edited):
            with self.assertRaisesRegex(Halt, 'changed after intent'):
                self.factory.move(card, 'Done', 'Verified completion')
        restarted = Factory(self.root, self.c, self.gh, self.proc)
        with self.assertRaisesRegex(Halt, 'unresolved'):
            restarted.reconcile([card])
        self.assertEqual(self.gh.github_status_writes, [])
        self.assertEqual(self.gh.github_status[card['item_id']], 'todo')

    def test_done_status_can_resume_before_secondary_write_intent(self):
        card = self.gh.items[0]
        self.gh.github_status[card['item_id']] = 'in-progress'
        with patch.object(self.gh, 'done_status_snapshot', side_effect=Stopped('stopped before snapshot')):
            with self.assertRaisesRegex(Halt, 'stopped before snapshot'):
                self.factory.move(card, 'Done', 'Verified completion')
        restarted = Factory(self.root, self.c, self.gh, self.proc)
        self.assertEqual(restarted.record(card)['done_status']['state'], 'pending')
        restarted.reconcile([card])
        restarted.reconcile([card])
        self.assertEqual(self.gh.github_status_writes, [card['item_id']])
        self.assertEqual(restarted.record(card)['done_status']['state'], 'verified')

    def test_done_status_recovers_lost_primary_stage_response(self):
        from sfc_lib.rate_limit import ConnectionReset

        card = self.gh.items[0]
        self.gh.github_status[card['item_id']] = 'in-progress'
        move = self.gh.move

        def lost(card, stage):
            move(card, stage)
            raise ConnectionReset('lost stage response')

        with patch.object(self.gh, 'move', side_effect=lost):
            with self.assertRaises(ConnectionReset):
                self.factory.move(card, 'Done', 'Verified completion')
        restarted = Factory(self.root, self.c, self.gh, self.proc)
        self.assertEqual(restarted.record(card)['done_status']['state'], 'pending')
        restarted.reconcile(self.gh.cards())
        self.assertEqual(self.gh.writes, [(card['number'], 'Done')])
        self.assertEqual(self.gh.github_status_writes, [card['item_id']])

    def test_done_status_lost_secondary_response_without_write_stays_unresolved(self):
        from sfc_lib.rate_limit import ConnectionReset

        card = self.gh.items[0]
        self.gh.github_status[card['item_id']] = 'in-progress'
        with patch.object(self.gh, 'write_done_status', side_effect=ConnectionReset('uncertain')) as write:
            with self.assertRaisesRegex(Halt, 'not verified'):
                self.factory.move(card, 'Done', 'Verified completion')
            restarted = Factory(self.root, self.c, self.gh, self.proc)
            with self.assertRaisesRegex(Halt, 'unresolved'):
                restarted.reconcile(self.gh.cards())
        self.assertEqual(write.call_count, 1)
        self.assertEqual(self.gh.github_status[card['item_id']], 'in-progress')

    def test_dry_run_is_read_only_and_dependencies_gate_claims(self):
        self.gh.items = [issue(1, body="## Acceptance criteria\n- Works\n## Blocked by\n- #2"), issue(2)]
        result = self.factory.run(dry_run=True)
        self.assertEqual(result["selected"], [2])
        self.assertEqual([{k: v for k, v in w.items() if k != "explanation"} for w in result["waiting"]],
                         [{"issue": 1, "reasons": ["Open prerequisites: #2"], "excluded": False}])
        self.assertIn("#2", result["waiting"][0]["explanation"])
        self.assertIn("verified closure", result["waiting"][0]["explanation"])
        self.assertFalse(self.factory.dir.exists())
        self.assertEqual(self.gh.writes, [])

    def test_changed_pr_head_halts_before_qa(self):
        def change(lane, cwd):
            if lane == "build":
                self.gh.external_head = "0" * 40
        self.proc.on_lane = change
        with self.assertRaisesRegex(Halt, "PR state"):
            self.factory.run()
        self.assertEqual(self.gh.merges, [])
        self.assertEqual(self.proc.lanes, ["build"])

    def test_issue_edit_during_review_invalidates_evidence(self):
        def change(lane, cwd):
            if lane == "review":
                self.gh.items[0]["body"] += "\n- New criterion"
        self.proc.on_lane = change
        self.c["max_cycles"] = 1
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        self.factory.run()
        self.assertEqual(self.gh.items[0]["status"], "QA")
        self.assertEqual(self.gh.merges, [])
        self.assertEqual(self.gh.body_updates, [])

    def test_lock_excludes_competing_supervisors(self):
        with exclusive(self.root):
            with self.assertRaisesRegex(Halt, "Another SFC"):
                with exclusive(self.root):
                    pass

    def test_changed_configuration_invalidates_old_receipts(self):
        self.factory.run()
        r = self.factory.state["cards"]["1"]
        other = Factory(self.root, {**self.c, "verify_commands": ["exit 1"]}, self.gh, self.proc)
        self.assertFalse(other.pinned(issue(), r["head"], r["base"]))

    def test_stop_at_worker_exit_never_publishes(self):
        def stop_after_build(lane, cwd):
            if lane == "build":
                self.proc.stop_path.touch()
        self.proc.on_lane = stop_after_build
        with self.assertRaises(Stopped):
            self.factory.run()
        self.assertEqual(self.factory.state["outcome"], "stopped")
        self.assertEqual(self.gh.prs, {})
        self.assertEqual(git(self.root, "ls-remote", "origin", "refs/heads/sfc/issue-1"), "")

    def test_stop_after_review_never_marks_ready_or_merges(self):
        def stop_after_review(lane, cwd):
            if lane == "review":
                self.proc.stop_path.touch()
        self.proc.on_lane = stop_after_review
        self.c["merge_policy"] = "auto"
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        with self.assertRaises(Stopped):
            self.factory.run(allow_merge=True)
        self.assertTrue(self.gh.prs[101]["isDraft"])
        self.assertEqual(self.gh.merges, [])
        self.assertEqual(self.gh.body_updates, [])

    def test_seven_human_handoffs_are_progress_with_parallel_workers(self):
        self.gh.items = [issue(n) for n in range(1, 8)]
        result = self.factory.run()
        self.assertEqual(result["outcome"], "waiting")
        self.assertEqual(len(self.gh.prs), 7)
        self.assertTrue(all(v["status"] == "Blocked" for v in self.gh.items))
        self.assertEqual(self.proc.lanes.count("build"), 7)

    def test_base_movement_during_qa_requires_fresh_qa_and_review(self):
        changed = []
        def move_base(lane, cwd):
            if lane == "security" and not changed:
                Path(self.root, "new-base.txt").write_text("base moved")
                git(self.root, "add", "new-base.txt")
                git(self.root, "commit", "-m", "new base")
                git(self.root, "push", "origin", "main")
                changed.append(True)
        self.proc.on_lane = move_base
        result = self.factory.run()
        self.assertEqual(result["outcome"], "waiting")
        self.assertEqual(self.proc.lanes.count("qa"), 2)
        self.assertEqual(self.proc.lanes.count("review"), 1)
        r = self.factory.state["cards"]["1"]
        self.assertEqual(r["base"], git(self.root, "rev-parse", "main"))

    def test_stop_during_active_check_is_recorded_as_stopped(self):
        self.c["verify_commands"] = [shlex.join([sys.executable, '-c', 'import time; time.sleep(30)'])]
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        def trigger(lane, cwd):
            if lane == "build":
                threading.Timer(0.7, self.proc.stop_path.touch).start()
        self.proc.on_lane = trigger
        with self.assertRaises(Stopped):
            self.factory.run()
        self.assertEqual(self.factory.state["outcome"], "stopped")
        self.assertEqual(self.gh.merges, [])

    def test_qa_only_changed_task_cannot_be_reported_done(self):
        self.gh.items[0]["labels"] = ["qa"]
        self.c["max_cycles"] = 1
        def change(lane, cwd):
            if lane == "review":
                self.gh.items[0]["body"] += "\n- A new requested behavior"
        self.proc.on_lane = change
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        self.factory.run()
        self.assertEqual(self.gh.items[0]["status"], "QA")
        self.assertFalse(self.gh.notes)

    def test_qa_only_external_move_is_preserved(self):
        self.gh.items[0]["labels"] = ["qa"]
        def change(lane, cwd):
            if lane == "review":
                self.gh.items[0]["status"] = "Backlog"
        self.proc.on_lane = change
        with self.assertRaisesRegex(Halt, "moved externally"):
            self.factory.run()
        self.assertEqual(self.gh.items[0]["status"], "Backlog")

    def test_base_conflict_returns_to_builder_and_can_recover(self):
        changed = []
        def change(lane, cwd):
            if lane == "build" and not changed:
                Path(self.root, "value.txt").write_text("base changed too\n")
                git(self.root, "add", "value.txt")
                git(self.root, "commit", "-m", "conflicting base")
                git(self.root, "push", "origin", "main")
                changed.append(True)
        self.proc.on_lane = change
        result = self.factory.run()
        self.assertEqual(result["outcome"], "waiting")
        self.assertEqual(self.gh.items[0]["status"], "Blocked")
        self.assertEqual(self.proc.lanes.count("build"), 2)
        self.assertEqual(self.proc.lanes.count("review"), 1)
        r = self.factory.state["cards"]["1"]
        self.assertEqual(r["base"], git(self.root, "rev-parse", "main"))
        self.assertEqual(len(git(Path(r["worktree"]), "rev-list", "--parents", "-n", "1", "HEAD").split()), 3)


class HookTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1] / "hooks/guard.py"
        spec = importlib.util.spec_from_file_location("sfc_test_guard", path)
        cls.guard = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.guard)

    def check_command(self, command):
        return self.guard.check({"tool_name": "Bash", "cwd": "/tmp/project",
                                 "tool_input": {"command": command}}, Path("/tmp/project"), BASE_CONFIG)

    def test_credential_read_and_literal_write(self):
        self.assertIsNotNone(self.check_command("cat .env.local"))
        self.assertIsNone(self.check_command("cat .env.example"))
        self.assertIsNotNone(self.guard.check({"tool_name": "apply_patch", "cwd": "/tmp/project",
            "tool_input": {"command": "*** Begin Patch\n*** Add File: a.py\n+key='sk-" + "x" * 30 + "'\n*** End Patch"}}, Path("/tmp/project"), BASE_CONFIG))

    def test_worktree_and_delete_boundaries(self):
        self.assertIsNotNone(self.check_command("git worktree add -b x /tmp/elsewhere main"))
        self.assertIsNone(self.check_command("git worktree add -b x .sfc/worktrees/issue-1 main"))
        self.assertIsNotNone(self.check_command("rm -rf ../other"))
        self.assertIsNone(self.check_command("rm -f local.txt"))

    def test_protected_and_force_push(self):
        self.assertIsNotNone(self.check_command("git push origin HEAD:main"))
        self.assertIsNotNone(self.check_command("git push --force origin feature"))
        self.assertIsNotNone(self.check_command("git push"))
        self.assertIsNone(self.check_command("git push origin feature"))

    def test_apply_patch_cannot_escape_worktree(self):
        self.assertIsNotNone(self.guard.check({"tool_name": "apply_patch", "cwd": "/tmp/project",
            "tool_input": {"command": "*** Begin Patch\n*** Add File: ../escape.txt\n+bad\n*** End Patch"}}, Path("/tmp/project"), BASE_CONFIG))


class ProcessTests(unittest.TestCase):
    def test_stop_terminates_owned_process_group_and_retains_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            stop, log = Path(tmp, "stop"), Path(tmp, "worker.log")
            proc = Processes(stop)
            timer = threading.Timer(0.4, stop.touch)
            timer.start()
            started = time.monotonic()
            try:
                with self.assertRaises(Stopped):
                    proc.execute([sys.executable, "-c", "import time; print('started', flush=True); time.sleep(30)"], tmp, log, 20)
            finally:
                timer.join()
            self.assertLess(time.monotonic() - started, 5)
            self.assertEqual(proc.children, set())
            self.assertIn("started", log.read_text())

    def test_timeout_is_a_failure_not_a_user_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = Processes(Path(tmp, "stop"))
            with self.assertRaises(Halt) as caught:
                proc.execute([sys.executable, "-c", "import time; time.sleep(30)"], tmp, Path(tmp, "log"), 0.2)
            self.assertNotIsInstance(caught.exception, Stopped)


class GitHubContractTests(unittest.TestCase):
    def adapter(self):
        with patch("sfc_lib.github.git", return_value="git@github.com:example/app.git"):
            return GitHub(Path.cwd(), BASE_CONFIG)

    def test_done_status_resolves_only_the_distinct_standard_field_and_checks_card(self):
        from sfc_lib.common import STATUSES

        gh = self.adapter()
        card = issue(status='Done')
        stages = {stage: 'routing-' + stage for stage in STATUSES}
        options = {'Todo': 'todo', 'In Progress': 'active', 'Done': 'done'}
        fields = [{'id': key, 'name': name, 'dataType': 'SINGLE_SELECT',
                   'options': [{'name': n, 'id': value} for n, value in opts.items()]}
                  for key, name, opts in [('routing', 'SFC Status', stages), ('status', 'Status', options)]]
        gh._meta = {'id': 'project', 'field': 'routing', 'options': stages}
        item = {'id': card['item_id'], 'project': {'id': 'project'}, 'content': card,
                'values': {'routing': stages['Done'], 'status': 'active'}}
        with patch('sfc_lib.priority.inventory', return_value=fields), \
                patch('sfc_lib.priority.item_snapshot', return_value=item), patch.object(gh, 'call') as write:
            scope, before = gh.done_status_snapshot(card)
            self.assertEqual(before, 'active')
            gh.write_done_status(scope)
            write.assert_called_once_with('project', 'item-edit', '--id', card['item_id'], '--project-id',
                                          'project', '--field-id', 'status', '--single-select-option-id', 'done')
            item['values']['routing'] = stages['Review']
            with self.assertRaisesRegex(Halt, 'same ticket still in SFC Done'):
                gh.done_status_snapshot(card)
            fields[1]['options'].append({'name': 'Archived', 'id': 'archived'})
            self.assertIsNone(gh.done_status_snapshot(card))
            fields.pop()
            self.assertIsNone(gh.done_status_snapshot(card))

    def test_pr_state_uses_one_fresh_read_per_observation(self):
        gh = self.adapter()
        with patch.object(gh, "call", side_effect=[
                {"number": 1, "state": "OPEN"}, {"number": 1, "state": "MERGED"}]) as read:
            self.assertEqual(gh.pr_state(1), "OPEN")
            self.assertEqual(gh.pr_state(1), "MERGED")
        self.assertEqual([call.args for call in read.call_args_list],
                         [("pr", "view", 1, "--repo", "example/app", "--json", "number,state")] * 2)

    def test_pr_state_rejects_incomplete_or_wrong_pull_request(self):
        gh = self.adapter()
        for result in (None, {"number": 2, "state": "MERGED"},
                       {"number": True, "state": "MERGED"}, {"number": 1}):
            with self.subTest(result=result), patch.object(gh, "call", return_value=result):
                with self.assertRaisesRegex(Halt, "PR identity or state"):
                    gh.pr_state(1)

    def test_files_are_paginated_for_sensitive_path_policy(self):
        gh = self.adapter()
        with patch.object(gh, "call", side_effect=[{"number": 1}, [[{"filename": "src/a.py"}], [{"filename": "auth/key.py"}]]]) as mocked:
            result = gh.pr(1)
        self.assertEqual(len(result["files"]), 2)
        self.assertIn("--paginate", mocked.call_args_list[1].args)
        self.assertIn("body", mocked.call_args_list[0].args[-1].split(","))

    def test_pr_body_update_sends_only_body_as_json(self):
        gh = self.adapter()
        body = 'Closes #1\n\nLiteral `command` and $(text), "quotes".\n'
        with patch("sfc_lib.github.run", return_value=subprocess.CompletedProcess([], 0, "{}", "")) as mocked:
            gh.update_pr_body(101, body)
        self.assertEqual(mocked.call_count, 1)
        args, kwargs = mocked.call_args
        self.assertEqual(args, (
            ["gh", "api", "repos/example/app/pulls/101", "--method", "PATCH", "--input", "-"], gh.root))
        self.assertEqual(set(kwargs), {"env", "input", "check"})
        self.assertTrue(kwargs["env"] == gh.env, "GitHub transport environment changed")
        self.assertEqual(kwargs["input"], json.dumps({"body": body}))
        self.assertIs(kwargs["check"], False)

    def test_graphql_errors_halt_without_retries(self):
        gh = self.adapter()
        with patch("sfc_lib.github.run", return_value=subprocess.CompletedProcess([], 0, '{"errors":[{"message":"rate limit"}]}', "")) as mocked:
            with self.assertRaises(Halt):
                gh.call("api", "graphql")
        self.assertEqual(mocked.call_count, 1)

    def test_repository_identity_uses_origin_not_gh_default(self):
        gh = self.adapter()
        with patch.object(gh, "call", return_value={"nameWithOwner": "other/app"}) as mocked:
            with self.assertRaisesRegex(Halt, "origin repository"):
                gh.verify_repository()
        self.assertEqual(mocked.call_args.args[2], "https://github.com/example/app.git")
        self.assertNotIn("GH_REPO", gh.env)
        self.assertEqual(gh.env["GH_HOST"], "github.com")


if __name__ == "__main__":
    unittest.main()
