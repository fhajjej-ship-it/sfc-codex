"""Temporary Git repos + fake GitHub/Codex only. No network or live writes."""
from __future__ import annotations

import copy
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from sfc_lib.common import Halt, Stopped, common_dir, exclusive, git, read_json, run, validate_config, write_json
from sfc_lib.factory import Factory
from sfc_lib.github import GitHub
from sfc_lib.local_merge import LocalMerge
from sfc_lib.watch import Watch
import test_factory as fixtures
from test_pickup import PickupGitHub, ticket
import sfc

BASE_CONFIG = fixtures.BASE_CONFIG
FakeGitHub = fixtures.FakeGitHub


class AtomicGitHub(PickupGitHub):
    """Emulate GitHub's atomic beforeOid contract with real git ref transactions."""
    move = FakeGitHub.move
    def __init__(self, root, remote, cards=None):
        super().__init__(root, cards)
        self.remote = remote
        self.items[0].update(id="issue-node", repository={"nameWithOwner": "example/app"})
        self.attempts = []
        self.applied = []
        self.before_write = None
        self.after_write = None
        self.lost_response = False
        self.recognize = True
        self.close_issue = True
        self.protected = None
        self.rules = 0
        self.unsupported = False
        self.denied = False
        self.edit_snapshot = None

    def oid(self, ref):
        return git(self.remote, "rev-parse", ref)

    def local_merge_capabilities(self):
        if self.unsupported:
            raise Halt("Server lacks atomic updateRefs/beforeOid protocol")

    def local_merge_checks(self, head):
        return copy.deepcopy(self.ci)

    def local_merge_snapshot(self, number, issue, base, head):
        def ref(name):
            if run(['git', 'show-ref', '--verify', '--quiet', name], self.remote, check=False).returncode:
                return None
            oid = self.oid(name)
            return {"id": name, "prefix": "refs/heads/", "name": name.removeprefix("refs/heads/"),
                    "target": {"__typename": "Commit", "oid": oid,
                               "tree": {"oid": self.oid(oid + "^{tree}")}},
                    "branchProtectionRule": self.protected, "rules": {"totalCount": self.rules}}
        pr = self.pr(number)
        head_ref, base_ref = ref(head), ref(base)
        repo = {"id": "repo-node", "nameWithOwner": "example/app"}
        item = next(c for c in self.items if c["number"] == issue)
        pr.update(id=f"pr-node-{number}", headRefOid=head_ref['target']['oid'] if head_ref else pr['headRefOid'],
                  baseRefOid=base_ref['target']['oid'] if base_ref else None,
                  headRepository=repo.copy(), baseRepository=repo.copy(),
                  mergeCommit=pr.get("mergeCommit"),
                  closingIssuesReferences={"totalCount": 1, "nodes": [{"id": item["id"]}]})
        snapshot = {**repo, "isArchived": False, "viewerPermission": "WRITE",
                    "head": head_ref, "base": base_ref, "pullRequest": pr,
                    "issue": {k: item[k] for k in ("id", "number", "url", "state", "repository")}}
        if self.edit_snapshot:
            self.edit_snapshot(snapshot)
        return snapshot

    def atomic_fast_forward(self, intent):
        # Journal must exist on disk before even a failing remote mutation.
        number = intent["card_identity"]["number"]
        journal = json.loads((self.root / ".sfc/state.json").read_text())["cards"][str(number)]["local_merge"]
        assert journal["id"] == intent["id"] and journal["phase"] == "intent"
        self.attempts.append(copy.deepcopy(intent))
        if self.before_write:
            self.before_write(intent)
        if self.denied:
            raise Halt("permission denied")
        if run(["git", "merge-base", "--is-ancestor", intent["base"], intent["head"]],
               self.remote, check=False).returncode:
            raise Halt("non-fast-forward")
        transaction = (f"start\nverify {intent['head_ref']} {intent['head']}\n"
                       f"update {intent['base_ref']} {intent['head']} {intent['base']}\nprepare\ncommit\n")
        run(["git", "update-ref", "--stdin"], self.remote, input=transaction)
        self.applied.append(copy.deepcopy(intent))
        self.prs[intent["pr"]]["mergeCommit"] = {"oid": intent["head"]}
        if self.recognize:
            self.prs[intent["pr"]]["state"] = "MERGED"
        if self.close_issue:
            next(c for c in self.items if c["number"] == number)["state"] = "CLOSED"
        if self.after_write:
            self.after_write(intent)
        if self.lost_response:
            raise Halt("response lost after server applied transaction")


class PriorityAtomicGitHub(AtomicGitHub):
    """Use the paginated pickup transport and the atomic local-ref adapter together."""
    move = PickupGitHub.move


class PriorityLocalMergeTests(unittest.TestCase):
    tearDown = fixtures.IntegrationTests.tearDown

    def setUp(self):
        fixtures.IntegrationTests.setUp(self)
        self.c.update(merge_policy="auto", merge_mode="local-verification", max_cycles=1)
        self.gh = PriorityAtomicGitHub(self.root, self.remote,
                                     [ticket(status="Backlog"), ticket(15, priority="P0")])
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        self.factory.update(self.gh.items[0], attempts=1)

    def run_pickup(self, allow_merge=True, issues=(1,), dry_run=False):
        return self.factory.run(allow_merge=allow_merge, include_backlog=True,
                                issues=issues, dry_run=dry_run)

    def test_scoped_backlog_claim_traverses_lanes_and_exact_local_merge(self):
        self.gh.github_status['item-1'] = 'in-progress'
        self.run_pickup()
        record = self.factory.record(self.gh.items[0])
        self.assertEqual(self.proc.lanes, ["build", "qa", "security", "review"])
        self.assertEqual(record["attempts"], 1)
        self.assertEqual(record["claim"]["state"], "verified")
        self.assertEqual(record["local_merge"]["phase"], "done")
        self.assertEqual(self.gh.github_status['item-1'], 'done')
        self.assertEqual(self.gh.oid("main"), record["head"])
        self.assertEqual(len(self.gh.attempts), 1)
        self.assertEqual({n for n, _ in self.gh.writes}, {1})
        self.assertEqual({item for item, _ in self.gh.activity_writes}, {"item-1"})
        self.assertEqual(self.gh.items[1]["status"], "Ready")
        self.assertEqual(self.factory.state["run_options"]["issues"], [1])

    def assert_human_handoff(self, allow_merge):
        config_before = copy.deepcopy(self.c)
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        self.run_pickup(allow_merge=allow_merge)
        record = self.factory.record(self.gh.items[0])
        self.assertEqual(self.proc.lanes, ["build", "qa", "security", "review"])
        self.assertEqual(record["claim"]["state"], "verified")
        self.assertEqual(record["attempts"], 1)
        self.assertTrue(record["handoff"])
        self.assertEqual(self.gh.items[0]["status"], "Blocked")
        self.assertEqual(self.gh.attempts + self.gh.merges, [])
        self.assertEqual(self.c, config_before)

    def test_priority_opt_in_preserves_human_merge_policy(self):
        self.c["merge_policy"] = "human"
        self.assert_human_handoff(allow_merge=True)

    def test_priority_opt_in_does_not_grant_run_merge_authority(self):
        self.assert_human_handoff(allow_merge=False)

    def test_lost_local_merge_response_respects_restart_scope_and_keeps_claim(self):
        self.gh.lost_response = True
        with self.assertRaisesRegex(Halt, "no retry"):
            self.run_pickup()
        saved = copy.deepcopy(self.factory.record(self.gh.items[0]))
        writes = copy.deepcopy(self.gh.writes + self.gh.activity_writes)
        lanes = self.proc.lanes.copy()
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        preview = self.run_pickup(issues=(15,), dry_run=True)
        self.assertEqual(preview["selected"], [])
        self.assertIn("local merge journal outside", str(preview["waiting"]))
        with self.assertRaisesRegex(Halt, "local merge journal outside"):
            self.run_pickup(issues=(15,))
        self.assertEqual(self.gh.writes + self.gh.activity_writes, writes)
        self.assertEqual(self.factory.record(self.gh.items[0]), saved)
        self.assertEqual(self.proc.lanes, lanes)
        # Explicitly restore the original authorized scope: readback can finish
        # Done, but may neither repeat the ref mutation nor restart the worker.
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        self.run_pickup()
        record = self.factory.record(self.gh.items[0])
        self.assertEqual(record["local_merge"]["phase"], "done")
        self.assertEqual(record["claim"], saved["claim"])
        self.assertEqual(record["attempts"], saved["attempts"])
        self.assertEqual(self.proc.lanes, lanes)
        self.assertEqual(len(self.gh.attempts), 1)
        self.assertEqual(self.gh.items[1]["status"], "Ready")


class LocalMergeTests(unittest.TestCase):
    # Reuse only the real temporary-repository setup, not its test methods.
    tearDown = fixtures.IntegrationTests.tearDown
    prepare_review = fixtures.IntegrationTests.prepare_review

    def setUp(self):
        fixtures.IntegrationTests.setUp(self)
        self.c.update(merge_policy="auto", merge_mode="local-verification")
        self.gh = AtomicGitHub(self.root, self.remote)
        self.factory = Factory(self.root, self.c, self.gh, self.proc)

    def review(self):
        card, wt = self.prepare_review()
        self.factory.allow_merge = True
        self.factory.review(card, wt)
        return card, wt

    def restart(self):
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        self.factory.run_dir = self.factory.dir / "runs/restarted"
        self.factory.allow_merge = True
        return self.factory

    def assert_no_merge(self):
        self.assertEqual(self.gh.applied, [])
        self.assertNotEqual(self.gh.items[0]["status"], "Done")
        self.assertEqual(self.gh.merges, [])  # No squash/expected-head fallback.

    def test_success_lands_exact_commit_and_tree_with_real_checks_and_lanes(self):
        original = self.gh.oid("main")
        self.assertEqual(self.factory.run(allow_merge=True)["outcome"], "drained")
        r = self.factory.record(self.gh.items[0])
        intent = r["local_merge"]
        self.assertEqual(intent["phase"], "done")
        self.assertEqual(intent["base"], original)
        self.assertEqual(self.gh.oid("main"), r["head"])
        self.assertEqual(self.gh.oid("main^{tree}"), intent["tree"])
        self.assertEqual(self.proc.lanes, ["build", "qa", "security", "review"])
        self.assertEqual(self.gh.items[0]["status"], "Done")
        self.assertEqual(self.gh.items[0]["state"], "CLOSED")
        self.assertEqual(len(self.gh.attempts), 1)
        self.assertTrue(Path(intent["worktree"]).is_dir())
        self.assertTrue(all(v["exit_code"] == 0 for v in intent["verification"]["checks"]))
        self.assertIn("local-gate-", intent["verification"]["worktree"])
        self.assertEqual([e[0] for e in self.proc.events],
                         ["build", "check", "qa", "security", "review", "check"])
        checks = [e for e in self.proc.events if e[0] == "check"]
        self.assertEqual([e[2:] for e in checks],
                         [(intent["head"], self.c["verify_commands"][0], 0)] * 2)
        self.assertEqual(checks[0][1], r["worktree"])
        self.assertEqual(checks[1][1], intent["verification"]["worktree"])
        for lane in ("qa", "security", "review"):
            receipt = intent["receipts"][lane]
            self.assertEqual(receipt["head"], intent["head"])
            self.assertEqual(receipt["base"], original)
            self.assertEqual(receipt["config_digest"], intent["config_digest"])
            self.assertEqual(receipt["task_digest"], intent["task_digest"])
        self.assertEqual(self.gh.merges, [])

    def test_factory_state_replacement_and_run_summary_are_private(self):
        path = self.factory.state_path
        self.factory.save()
        path.chmod(0o644)  # Existing checkout with state from an older version.
        run = self.factory.run(allow_merge=True)
        folder = Path(run['run_dir'])
        self.assertEqual(run['outcome'], 'drained')
        self.assertEqual(path.stat().st_mode & 0o077, 0)
        self.assertEqual(folder.stat().st_mode & 0o077, 0)
        summary = folder / 'summary.json'
        self.assertEqual(summary.stat().st_mode & 0o077, 0)
        self.assertEqual(summary.read_bytes(), path.read_bytes())

    def test_no_opt_in_no_migration_and_human_or_run_authority_required(self):
        for mode, policy, authority in (("local-verification", "human", True),
                                         ("local-verification", "auto", False),
                                         (None, "auto", True)):
            with self.subTest(mode=mode, policy=policy):
                # New factory/config per case; old evidence must be revalidated.
                config = copy.deepcopy(BASE_CONFIG)
                config["merge_policy"] = policy
                if mode:
                    config["merge_mode"] = mode
                before = copy.deepcopy(config)
                validate_config(config)
                self.assertEqual(config, before)
                if mode is None:
                    self.gh.strict = False
                f = Factory(self.root, config, self.gh, self.proc)
                f.run(allow_merge=authority)
                self.assertEqual(self.gh.attempts, [])
                self.assert_no_merge()
                # Restore the board for the next case, preserve the existing claim.
                self.gh.items[0]["status"] = "Review"

    def test_empty_commands_and_invalid_modes_rejected(self):
        for replacement in ([], [""], None):
            with self.assertRaises(Halt):
                Factory(self.root, {**self.c, "verify_commands": replacement}, self.gh, self.proc)
        for value in (None, "local", True, ""):
            with self.assertRaises(Halt):
                validate_config({**self.c, "merge_mode": value})

    def test_failed_project_verification_never_writes(self):
        self.c["verify_commands"] = ["exit 1"]
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        self.factory.run(allow_merge=True)
        self.assert_no_merge()
        self.assertEqual(self.proc.lanes, ["build", "build"])

    def assert_failed_lane(self, lane):
        self.proc.fail_lane = lane
        self.factory.run(allow_merge=True)
        self.assert_no_merge()
        self.assertEqual(self.gh.attempts, [])
        self.assertEqual(self.gh.items[0]["status"], "Blocked")

    def test_failed_qa_verdict_blocks_merge(self):
        self.assert_failed_lane("qa")

    def test_failed_security_verdict_blocks_merge(self):
        self.assert_failed_lane("security")

    def test_failed_final_review_verdict_blocks_merge(self):
        self.assert_failed_lane("review")

    def test_each_independent_failed_lane_never_writes(self):
        card, wt = self.prepare_review()
        self.factory.allow_merge = True
        self.factory.lane(card, "review", wt, self.factory.record(card)["base"])
        original = copy.deepcopy(self.factory.record(card))
        gate = LocalMerge(self.factory)
        for lane in ("qa", "security", "review"):
            for key, value in (("head", "0" * 40), ("base", "0" * 40),
                               ("task_digest", "changed"), ("config_digest", "changed")):
                self.factory.state["cards"]["1"] = copy.deepcopy(original)
                self.factory.record(card)["receipts"][lane][key] = value
                with self.subTest(lane=lane, key=key), self.assertRaises(Halt):
                    gate.gate(card, wt, original["head"], original["base"])
            for verdict in ("fail", "blocked"):
                self.factory.state["cards"]["1"] = copy.deepcopy(original)
                self.factory.record(card)["receipts"][lane]["result"]["verdict"] = verdict
                with self.assertRaises(Halt):
                    gate.gate(card, wt, original["head"], original["base"])
        self.assert_no_merge()
        self.assertEqual(self.gh.attempts, [])

    def test_gate_repeats_real_checks_and_retains_failed_scratch(self):
        card, wt = self.prepare_review()
        self.factory.allow_merge = True
        original = self.proc.execute
        def execute(argv, cwd, *args, **kwargs):
            if "local-gate-" in str(cwd):
                # A real command failure only in the final candidate worktree.
                return original(["/bin/sh", "-c", "exit 9"], cwd, *args, **kwargs)
            return original(argv, cwd, *args, **kwargs)
        with patch.object(self.proc, "execute", side_effect=execute):
            self.factory.review(card, wt)
        self.assert_no_merge()
        self.assertTrue(Path(self.factory.record(card)["local_merge_gate"]["worktree"]).is_dir())
        self.assertEqual(self.factory.record(card)["attempts"], 1)
        self.assertEqual([e[0] for e in self.proc.events],
                         ["build", "check", "qa", "security", "review", "check"])
        record = self.factory.record(card)
        final = self.proc.events[-1]
        self.assertEqual(final[1:3], (record["local_merge_gate"]["worktree"], record["head"]))
        self.assertEqual(final[4], 9)
        self.assertNotIn("verification", record)  # Failed evidence cannot be reused.
        receipts = [json.loads(p.read_text()) for p in self.factory.run_dir.glob("issue-1/verify-*/receipt.json")]
        failed = next(r for r in receipts if not r["passed"])
        self.assertEqual(failed["worktree"], final[1])
        self.assertEqual(failed["checks"][0]["exit_code"], 9)
        self.assertTrue(Path(failed["checks"][0]["log"]).is_file())

    def test_absent_checks_only_with_explicit_choice(self):
        self.gh.ci = []
        self.c["max_cycles"] = 1
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        self.factory.run(allow_merge=True)
        self.assert_no_merge()
        self.c["allow_no_checks"] = True
        self.c["max_cycles"] = 8
        self.restart().run(allow_merge=True)
        self.assertEqual(len(self.gh.applied), 1)

    def test_existing_failed_or_pending_checks_even_with_empty_opt_in(self):
        card, wt = self.prepare_review()
        self.factory.allow_merge = True
        self.factory.c["allow_no_checks"] = True
        for check in ({"state": "FAILURE"}, {"state": "PENDING"},
                      {"status": "COMPLETED", "conclusion": "FAILURE"}):
            self.gh.ci = [check]
            self.factory.review(card, wt)
            self.assert_no_merge()

    def test_unsupported_capabilities_and_permission_failure_do_not_fallback(self):
        self.gh.unsupported = True
        card, wt = self.prepare_review()
        self.factory.allow_merge = True
        with self.assertRaisesRegex(Halt, "lacks atomic"):
            self.factory.review(card, wt)
        self.assertEqual(self.gh.attempts, [])
        self.gh.unsupported = False
        self.gh.denied = True
        with self.assertRaisesRegex(Halt, "no retry"):
            self.factory.review(card, wt)
        self.assertEqual(len(self.gh.attempts), 1)
        with self.assertRaisesRegex(Halt, "not confirmed"):
            self.restart().reconcile(self.gh.cards())
        self.assertEqual(len(self.gh.attempts), 1)
        self.assert_no_merge()

    def test_sensitive_labels_paths_and_size_still_require_human(self):
        card, wt = self.prepare_review()
        self.factory.allow_merge = True
        original = copy.deepcopy(self.gh.prs[101])
        variants = ({"additions": 401}, {"files": [{"path": "auth/token.py"}]})
        for variant in variants:
            self.gh.prs[101] = {**original, **variant}
            self.gh.items[0]["status"] = card["status"] = "Review"
            self.factory.review(card, wt)
            self.assertEqual(card["status"], "Blocked")
            self.assertEqual(self.gh.attempts, [])
        # An actual sensitive label goes through fresh task + renewed lane pins.
        self.gh.items[0]["labels"].append("security")
        self.gh.items[0]["status"] = "QA"
        card = self.gh.cards()[0]
        self.factory.qa(card, wt)
        self.factory.review(card, wt)
        self.assertEqual(card["status"], "Blocked")
        self.assert_no_merge()

    def test_human_only_label_never_merges(self):
        self.gh.items[0]["labels"].append("human-only")
        self.factory.run(allow_merge=True)
        self.assertEqual(self.gh.attempts, [])
        self.assertEqual(self.gh.items[0]["status"], "Blocked")

    def test_done_write_without_readback_latches_and_is_not_repeated(self):
        original = self.gh.move
        def ignored(card, stage):
            if stage != "Done":
                original(card, stage)
        with patch.object(self.gh, "move", side_effect=ignored), self.assertRaisesRegex(Halt, "Done transition not verified"):
            self.review()
        with patch.object(self.gh, "move", side_effect=AssertionError("no retry")), \
                self.assertRaisesRegex(Halt, "Ambiguous Done"):
            self.restart().reconcile(self.gh.cards())
        self.assertEqual(len(self.gh.attempts), 1)

    def test_completion_uses_live_base_ref_when_merged_pr_base_oid_is_historical(self):
        def historical(snapshot):
            if snapshot["pullRequest"]["state"] == "MERGED":
                snapshot["pullRequest"]["baseRefOid"] = self.gh.applied[-1]["base"]
        self.gh.edit_snapshot = historical
        self.review()
        self.assertEqual(self.gh.items[0]["status"], "Done")

    def test_config_or_task_changes_at_final_preflight_prevent_ref_write(self):
        card, wt = self.prepare_review()
        self.factory.allow_merge = True
        original = self.factory.verify
        def changed_config(card, worktree, base):
            passed = original(card, worktree, base)
            if "local-gate-" in str(worktree):
                write_json(self.factory.dir / "config.json", {**self.c, "auto_max_lines": 1})
            return passed
        with patch.object(self.factory, "verify", side_effect=changed_config), \
                self.assertRaisesRegex(Halt, "configuration changed"):
            self.factory.review(card, wt)
        (self.factory.dir / "config.json").unlink()
        snapshots = []
        def changed_task(snapshot):
            snapshots.append(snapshot)
            if len(snapshots) == 2:
                self.gh.items[0]["labels"].append("security")
        self.gh.edit_snapshot = changed_task
        self.factory.review(card, wt)
        self.assertEqual(card["status"], "QA")
        self.assertEqual(self.gh.attempts, [])

    def test_partial_remote_state_after_response_is_not_done_or_retried(self):
        self.gh.after_write = lambda i: git(self.remote, "update-ref", i["head_ref"], i["base"], i["head"])
        with self.assertRaisesRegex(Halt, "not confirmed"):
            self.review()
        with self.assertRaises(Halt):
            self.restart().reconcile(self.gh.cards())
        self.assertEqual(len(self.gh.attempts), 1)
        self.assertEqual(self.gh.items[0]["status"], "Review")

    def test_dirty_source_and_dirty_final_candidate_are_rejected(self):
        card, wt = self.prepare_review()
        self.factory.allow_merge = True
        self.factory.lane(card, "review", wt, self.factory.record(card)["base"])
        Path(wt, "untracked.txt").write_text("unknown")
        with self.assertRaisesRegex(Halt, "clean"):
            self.factory.review(card, wt)
        Path(wt, "untracked.txt").unlink()
        original = self.factory.verify
        def dirty(card, worktree, base):
            passed = original(card, worktree, base)
            if "local-gate-" in str(worktree):
                Path(worktree, "unknown.txt").write_text("unknown")
            return passed
        with patch.object(self.factory, "verify", side_effect=dirty), self.assertRaisesRegex(Halt, "clean"):
            self.factory.review(card, wt)
        self.assertEqual(self.gh.attempts, [])
        self.assert_no_merge()

    def test_base_movement_during_gate_returns_for_integration_and_all_new_lanes(self):
        card, wt = self.prepare_review()
        self.factory.allow_merge = True
        original = self.factory.verify
        def advance(card, worktree, base):
            passed = original(card, worktree, base)
            if "local-gate-" in str(worktree):
                Path(self.root, "new-base.txt").write_text("base")
                git(self.root, "add", "new-base.txt")
                git(self.root, "commit", "-m", "advance")
                git(self.root, "push", "origin", "main")
            return passed
        with patch.object(self.factory, "verify", side_effect=advance):
            self.factory.review(card, wt)
        self.assertEqual(card["status"], "QA")
        self.assert_no_merge()
        self.factory.qa(card, wt)
        self.factory.review(card, wt)
        self.assertEqual(len(self.gh.applied), 1)
        self.assertEqual(self.proc.lanes[-3:], ["qa", "security", "review"])

    def test_head_race_immediately_before_atomic_write_changes_neither_base_nor_other_refs(self):
        before = self.gh.oid("main")
        self.gh.before_write = lambda i: git(self.remote, "update-ref", i["head_ref"], i["base"], i["head"])
        with self.assertRaisesRegex(Halt, "no retry"):
            self.review()
        self.assertEqual(self.gh.oid("main"), before)
        self.assert_no_merge()
        with self.assertRaises(Halt):
            self.restart().run(allow_merge=True)
        self.assertEqual(len(self.gh.attempts), 1)

    def test_base_race_immediately_before_atomic_write_rejects_both_updates(self):
        def race(i):
            # A distinct descendant of the old base, not the approved head.
            tree = self.gh.oid(i["base"] + "^{tree}")
            sibling = run(["git", "commit-tree", tree, "-p", i["base"], "-m", "other writer"], self.root).stdout.strip()
            # Make the temp object available to the fake server.
            git(self.root, "push", "origin", sibling + ":refs/heads/other-writer")
            git(self.remote, "update-ref", i["base_ref"], sibling, i["base"])
        self.gh.before_write = race
        with self.assertRaisesRegex(Halt, "no retry"):
            self.review()
        self.assert_no_merge()
        i = self.gh.attempts[0]
        self.assertEqual(self.gh.oid(i["head_ref"]), i["head"])
        self.assertNotEqual(self.gh.oid("main"), i["head"])

    def test_lost_response_restart_proves_remote_outcome_without_another_mutation(self):
        self.gh.lost_response = True
        with self.assertRaisesRegex(Halt, "no retry"):
            card, wt = self.review()
        r = self.factory.record(self.gh.items[0])
        self.assertEqual(r["local_merge"]["phase"], "ambiguous")
        self.assertTrue(Path(r["local_merge"]["worktree"]).is_dir())
        self.assertEqual(self.gh.items[0]["status"], "Review")
        self.restart().reconcile(self.gh.cards())
        self.assertEqual(len(self.gh.attempts), 1)
        self.assertEqual(self.gh.items[0]["status"], "Done")
        self.assertEqual(self.factory.record(self.gh.items[0])["local_merge"]["phase"], "done")

    def test_unrecognized_pr_or_open_issue_never_done_readback_can_finish_later(self):
        self.gh.recognize = self.gh.close_issue = False
        with self.assertRaisesRegex(Halt, "not confirmed"):
            self.review()
        self.assertEqual(self.gh.items[0]["status"], "Review")
        self.gh.prs[101]["state"] = "MERGED"
        with self.assertRaisesRegex(Halt, "not confirmed"):
            self.restart().reconcile(self.gh.cards())
        self.gh.items[0]["state"] = "CLOSED"
        self.restart().reconcile(self.gh.cards())
        self.assertEqual(self.gh.items[0]["status"], "Done")
        self.assertEqual(len(self.gh.attempts), 1)

    def test_partial_done_response_is_read_back_and_never_repeated(self):
        original = self.gh.move
        def lost(card, stage):
            original(card, stage)
            if stage == "Done":
                raise Halt("lost stage response")
        with patch.object(self.gh, "move", side_effect=lost), self.assertRaisesRegex(Halt, "lost stage"):
            self.review()
        with patch.object(self.gh, "move", side_effect=AssertionError("never repeat move")):
            self.restart().reconcile(self.gh.cards())
        self.assertEqual(self.factory.record(self.gh.items[0])["local_merge"]["phase"], "done")
        self.assertEqual(len(self.gh.attempts), 1)

    def test_stop_after_intent_never_dispatches_or_retries(self):
        original = self.factory.event
        def stop(card, detail):
            original(card, detail)
            if "atomic local merge requested" in detail:
                self.proc.stop_path.touch()
        with patch.object(self.factory, "event", side_effect=stop), self.assertRaises(Stopped):
            self.review()
        self.assertEqual(self.gh.attempts, [])
        self.proc.stop_path.unlink()
        with self.assertRaisesRegex(Halt, "not confirmed"):
            self.restart().reconcile(self.gh.cards())
        self.assertEqual(self.gh.attempts, [])

    def test_identity_and_incomplete_rules_never_write(self):
        card, wt = self.prepare_review()
        self.factory.allow_merge = True
        mutations = [lambda s: s.update(nameWithOwner="other/repo"),
                     lambda s: s.update(viewerPermission="READ"),
                     lambda s: s["pullRequest"].update(id=""),
                     lambda s: s["pullRequest"].update(headRepository={"id": "fork", "nameWithOwner": "other/repo"}),
                     lambda s: s["pullRequest"].update(baseRefName="other"),
                     lambda s: s["head"].update(name="other"),
                     lambda s: s["base"].pop("rules"),
                     lambda s: s["issue"].update(id="other"),
                     lambda s: s["pullRequest"]["closingIssuesReferences"].update(totalCount=101)]
        for mutation in mutations:
            self.gh.edit_snapshot = mutation
            with self.subTest(mutation=mutation), self.assertRaises(Halt):
                self.factory.review(card, wt)
        self.assertEqual(self.gh.attempts, [])

    def test_protected_or_ruled_refs_require_human_or_strict_mode(self):
        card, wt = self.prepare_review()
        self.factory.allow_merge = True
        for protected, rules in (({"id": "rule"}, 0), (None, 1)):
            self.gh.protected, self.gh.rules = protected, rules
            with self.assertRaisesRegex(Halt, "protected/ruled"):
                self.factory.review(card, wt)
        self.assertEqual(self.gh.attempts, [])

    def test_external_merge_without_local_journal_cannot_use_legacy_done_shortcut(self):
        card, wt = self.prepare_review()
        self.gh.prs[101]["state"] = "MERGED"
        self.gh.items[0]["state"] = "CLOSED"
        with self.assertRaisesRegex(Halt, "No saved reviewed ticket identity"):
            self.factory.reconcile(self.gh.cards())
        self.assertEqual(self.gh.items[0]["status"], "Review")

    def test_two_cards_serialize_merges_and_refresh_second_base(self):
        self.gh.items.append(fixtures.issue(number=2, id="issue-node-2", repository={"nameWithOwner": "example/app"}))
        self.proc.on_lane = lambda lane, cwd: (Path(cwd, Path(cwd).name + ".txt").write_text("feature")
                                              if lane == "build" else None)
        self.assertEqual(self.factory.run(allow_merge=True)["outcome"], "drained")
        self.assertEqual(len(self.gh.applied), 2)
        first, second = self.gh.applied
        self.assertEqual(second["base"], first["head"])
        self.assertEqual(self.gh.oid("main"), second["head"])
        self.assertEqual([c["status"] for c in self.gh.items], ["Done", "Done"])

    def test_manual_card_movement_after_ref_write_is_preserved(self):
        self.gh.after_write = lambda i: self.gh.items[0].update(status="Blocked")
        with self.assertRaisesRegex(Halt, "Card moved"):
            self.review()
        self.assertEqual(self.gh.items[0]["status"], "Blocked")
        self.assertEqual(len(self.gh.applied), 1)
        self.assertEqual(self.factory.record(self.gh.items[0])["local_merge"]["phase"], "responded")

    def test_sensitive_rename_source_is_found_in_real_candidate_diff(self):
        (self.root / "auth").mkdir()
        Path(self.root, "auth/old.txt").write_text("sensitive")
        git(self.root, "add", "auth/old.txt")
        git(self.root, "commit", "-m", "sensitive original")
        git(self.root, "push", "origin", "main")
        def rename(lane, cwd):
            if lane == "build":
                Path(cwd, "auth/old.txt").rename(Path(cwd, "ordinary.txt"))
        self.proc.on_lane = rename
        self.factory.run(allow_merge=True)
        self.assertEqual(self.gh.items[0]["status"], "Blocked")
        self.assertEqual(self.gh.attempts, [])

    def test_wrong_candidate_ancestry_returns_to_qa_without_writing(self):
        card, wt = self.prepare_review()
        r = self.factory.record(card)
        tree = git(self.root, "rev-parse", r["base"] + "^{tree}")
        sibling = run(["git", "commit-tree", tree, "-p", r["base"], "-m", "divergent base"],
                      self.root).stdout.strip()
        self.factory.update(card, base=sibling)
        self.assertTrue(self.factory.verify(card, wt, sibling))
        for lane in ("qa", "security", "review"):
            self.factory.lane(card, lane, wt, sibling)
        self.factory.allow_merge = True
        LocalMerge(self.factory).gate(card, wt, r["head"], sibling)
        self.assertEqual(card["status"], "QA")
        self.assertEqual(self.gh.attempts, [])


class LocalMergeAdapterTests(unittest.TestCase):
    def adapter(self):
        with patch("sfc_lib.github.git", return_value="git@github.com:example/app.git"):
            return GitHub(Path.cwd(), BASE_CONFIG)

    def test_atomic_payload_has_both_expected_values_no_force_and_one_transaction(self):
        gh = self.adapter()
        intent = {"id": "request", "repository_id": "repo", "head_ref": "refs/heads/sfc/issue-1",
                  "base_ref": "refs/heads/main", "head": "a" * 40, "base": "b" * 40}
        with patch.object(gh, "graphql", return_value={"updateRefs": {"clientMutationId": "request"}}) as call:
            gh.atomic_fast_forward(intent)
        call.assert_called_once()
        payload = call.call_args.args[1]["input"]
        self.assertEqual(payload["refUpdates"], [
            {"name": intent["head_ref"], "beforeOid": intent["head"], "afterOid": intent["head"], "force": False},
            {"name": intent["base_ref"], "beforeOid": intent["base"], "afterOid": intent["head"], "force": False}])
        for response in ({}, {"updateRefs": None}, {"updateRefs": {"clientMutationId": "wrong"}}):
            with patch.object(gh, "graphql", return_value=response), self.assertRaises(Halt):
                gh.atomic_fast_forward(intent)

    def test_schema_capabilities_fail_closed(self):
        gh = self.adapter()
        schema = {"mutation": {"fields": [{"name": "updateRefs", "args": [{"name": "input"}]}]},
                  "update": {"inputFields": [{"name": n} for n in ("name", "beforeOid", "afterOid", "force")]},
                  "input": {"inputFields": [{"name": n} for n in ("repositoryId", "refUpdates", "clientMutationId")]}}
        with patch.object(gh, "graphql", return_value=schema):
            gh.local_merge_capabilities()
        for key in schema:
            broken = copy.deepcopy(schema)
            broken[key] = None
            with patch.object(gh, "graphql", return_value=broken), self.assertRaisesRegex(Halt, "no fallback"):
                gh.local_merge_capabilities()
        broken = copy.deepcopy(schema)
        broken["update"]["inputFields"] = [{"name": "afterOid"}]
        with patch.object(gh, "graphql", return_value=broken), self.assertRaises(Halt):
            gh.local_merge_capabilities()

    def test_check_inventory_is_paginated_and_exact_head_pinned(self):
        gh = self.adapter()
        sha = "a" * 40
        checks = [{"total_count": 2, "check_runs": [{"id": 1, "head_sha": sha, "status": "completed", "conclusion": "success"}]},
                  {"total_count": 2, "check_runs": [{"id": 2, "head_sha": sha, "status": "completed", "conclusion": "failure"}]}]
        statuses = [{"total_count": 1, "sha": sha, "statuses": [{"context": "legacy", "state": "success"}]}]
        with patch.object(gh, "call", side_effect=[checks, statuses]) as call:
            result = gh.local_merge_checks(sha)
        self.assertEqual(result[-2:], [{"status": "COMPLETED", "conclusion": "FAILURE"}, {"state": "SUCCESS"}])
        self.assertTrue(all("--paginate" in c.args for c in call.call_args_list))
        for broken in ([], [{"total_count": 3, "check_runs": []}],
                       [{"total_count": 1, "check_runs": [{"head_sha": "b" * 40}]}]):
            with patch.object(gh, "call", side_effect=[broken, statuses]), self.assertRaises(Halt):
                gh.local_merge_checks(sha)


class MergeOnboardingTests(unittest.TestCase):
    tearDown = fixtures.IntegrationTests.tearDown
    setUp = fixtures.IntegrationTests.setUp

    def onboard(self, options):
        class SetupAdapter:
            def __init__(self, root, config):
                self.c = config
            def verify_repository(self):
                pass
            def metadata(self, create=False):
                return {"id": "project"}
            def configure_dashboard(self):
                return {"ready": True}
            dashboard = configure_dashboard
        output = io.StringIO()
        with patch.object(sfc, 'onboard_chat', sfc.onboard), patch("sfc.GitHub", SetupAdapter), \
                patch("sfc_lib.lifeline.supported", return_value=True), redirect_stdout(output):
            status = sfc.main(["--root", str(self.root), "onboard", "--repo", "example/app",
                               "--owner", "example", "--project", "1", "--base", "main",
                               "--verify", fixtures.BASE_CONFIG["verify_commands"][0], *options])
        self.assertEqual(status, 0)
        return json.loads(output.getvalue()), json.loads((self.root / ".sfc/config.json").read_text())

    def test_cli_default_is_auto_protected_and_requires_checks_without_starting(self):
        result, config = self.onboard([])
        self.assertEqual(result["merge_policy"], "auto")
        self.assertEqual(config["merge_policy"], "auto")
        self.assertEqual(result["merge_mode"], "github-protected")
        self.assertEqual(result["merge_method"], "squash")
        self.assertFalse(config["allow_no_checks"])
        self.assertTrue(result["autostart"]["enabled"])
        self.assertEqual(result["autostart"]["version"], 2)
        self.assertFalse(result["autostart"]["scope"]["allow_merge"])
        self.assertFalse((self.root / ".sfc/state.json").exists())
        self.assertFalse((common_dir(self.root) / "sfc-watch.json").exists())
        self.assertEqual(self.proc.lanes, [])

    def test_onboarding_help_describes_automatic_delivery_and_human_choice(self):
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit) as exit:
            sfc.main(["onboard", "--help"])
        self.assertEqual(exit.exception.code, 0)
        help_text = " ".join(output.getvalue().split())
        self.assertIn("automatic delivery by default", help_text)
        self.assertIn("human keeps an explicit merge gate", help_text)
        self.assertIn("--allow-merge", help_text)
        self.assertNotIn("--auto-start", help_text)

    def test_cli_explicit_human_policy_is_saved_and_blocks_authorized_auto_merge(self):
        result, config = self.onboard(["--merge-policy", "human"])
        self.assertEqual(result["merge_policy"], "human")
        self.assertEqual(config["merge_policy"], "human")
        factory = Factory(self.root, github=self.gh, processes=self.proc)
        self.assertEqual(factory.run(allow_merge=True)["outcome"], "waiting")
        self.assertEqual(self.proc.lanes, ["build", "qa", "security", "review"])
        self.assertEqual(self.gh.items[0]["status"], "Blocked")
        self.assertEqual(self.gh.merges, [])

    def test_default_auto_without_run_authority_keeps_human_handoff(self):
        self.onboard([])
        factory = Factory(self.root, github=self.gh, processes=self.proc)
        self.assertEqual(factory.run()["outcome"], "waiting")
        self.assertEqual(self.proc.lanes, ["build", "qa", "security", "review"])
        self.assertEqual(self.gh.items[0]["status"], "Blocked")
        self.assertEqual(self.gh.merges, [])
        self.assertTrue(factory.record(fixtures.issue())["handoff"])

    def test_default_auto_does_not_authorize_a_new_watcher_session(self):
        self.onboard([])
        factory = Factory(self.root, github=self.gh, processes=self.proc)
        with self.assertRaisesRegex(Halt, "explicit --start or --resume"):
            Watch(factory).begin(allow_merge=False)
        self.assertFalse((common_dir(self.root) / "sfc-watch.json").exists())
        self.assertEqual(self.proc.lanes, [])

    def test_default_auto_with_authority_merges_only_after_all_lanes_and_checks(self):
        self.onboard([])
        factory = Factory(self.root, github=self.gh, processes=self.proc)
        self.assertEqual(factory.run(allow_merge=True)["outcome"], "drained")
        self.assertEqual([event[0] for event in self.proc.events],
                         ["build", "check", "qa", "security", "review", "check"])
        self.assertEqual(len(self.gh.merges), 1)
        self.assertEqual(self.gh.items[0]["status"], "Done")

    def test_default_auto_failed_project_checks_never_dispatch_independent_lanes(self):
        self.onboard(["--verify", "exit 2"])
        factory = Factory(self.root, github=self.gh, processes=self.proc)
        factory.run(allow_merge=True)
        self.assertEqual(self.proc.lanes, ["build", "build"])
        self.assertEqual(self.gh.items[0]["status"], "Blocked")
        self.assertEqual(self.gh.merges, [])
        self.assertTrue(any(event[4] == 2 for event in self.proc.events if event[0] == "check"))

    def test_default_auto_missing_failed_or_pending_github_checks_never_merge(self):
        self.onboard([])
        factory = Factory(self.root, github=self.gh, processes=self.proc)
        factory.run()  # Produce actual pinned lane/check evidence without merge authority.
        record = factory.record(fixtures.issue())
        card = self.gh.items[0]
        worktree = Path(record["worktree"])
        for checks in ([], [{"state": "FAILURE"}], [{"state": "PENDING"}]):
            with self.subTest(checks=checks):
                self.gh.ci = checks
                factory.allow_merge = True
                with self.assertRaisesRegex(Halt, "missing checks or changed evidence"):
                    factory.merge_gate(card, worktree, record["head"], record["base"])
                self.assertEqual(self.gh.merges, [])

    def test_default_auto_stale_check_receipts_never_merge(self):
        self.onboard([])
        factory = Factory(self.root, github=self.gh, processes=self.proc)
        factory.run()
        record = factory.record(fixtures.issue())
        original = copy.deepcopy(record["verification"])
        factory.allow_merge = True
        for pin in ("head", "base", "task_digest", "config_digest"):
            with self.subTest(pin=pin):
                record["verification"] = {**original, pin: "stale"}
                with self.assertRaisesRegex(Halt, "stale or missing lane evidence"):
                    factory.merge_gate(self.gh.items[0], Path(record["worktree"]),
                                       record["head"], record["base"])
                self.assertEqual(self.gh.merges, [])

    def test_existing_human_config_and_spent_session_survive_reads_and_reonboarding(self):
        self.onboard(["--merge-policy", "human"])
        factory = Factory(self.root, github=self.gh, processes=self.proc)
        watcher = Watch(factory, start=True, clock=lambda: 1000)
        with exclusive(self.root):
            watcher.begin(allow_merge=False)
            watcher.session.update(polls=2, claims=1, cycles=1, no_progress=1)
            watcher.save("stopped")
        paths = [self.root / ".sfc/config.json", watcher.path,
                 *sorted((self.root / ".sfc/watch-sessions").glob("*.json"))]
        before = {path: path.read_bytes() for path in paths}
        with patch("sfc.GitHub", return_value=self.gh), \
                patch("sfc_lib.factory.GitHub", return_value=self.gh), \
                patch.object(self.gh, "dashboard", create=True, return_value={"ready": True}), \
                patch("sfc.run", return_value=subprocess.CompletedProcess([], 0, "fake tool", "")):
            for command in (["doctor"], ["status"], ["watch-status"], ["autostart-status"],
                            ["run", "--dry-run"]):
                with self.subTest(command=command), redirect_stdout(io.StringIO()):
                    self.assertEqual(sfc.main(["--root", str(self.root), *command]), 0)
                    self.assertEqual({path: path.read_bytes() for path in paths}, before)
            error = io.StringIO()
            with redirect_stderr(error):
                self.assertEqual(sfc.main(["--root", str(self.root), "onboard", "--owner", "example",
                                           "--project", "1", "--verify", "exit 0", "--auto-start"]), 1)
            self.assertIn("already configured", error.getvalue())
        self.assertEqual({path: path.read_bytes() for path in paths}, before)
        self.assertEqual(read_json(paths[0])["merge_policy"], "human")
        saved = read_json(watcher.path)
        self.assertFalse(saved["allow_merge"])
        resumed = Watch(Factory(self.root, github=self.gh, processes=self.proc),
                        resume=saved["id"], clock=lambda: 1100)
        with exclusive(self.root):
            resumed.begin(allow_merge=False)
        for key in ("id", "deadline", "settings", "policy", "polls", "claims", "cycles", "no_progress"):
            self.assertEqual(resumed.session[key], saved[key], key)
        with self.assertRaisesRegex(Halt, "cannot broaden authority"):
            Watch(factory, resume=saved["id"]).begin(allow_merge=True)
        self.assertEqual(self.proc.lanes, [])

    def test_cli_exposes_separate_opt_ins_and_refuses_existing_config(self):
        options = ["--merge-policy", "auto", "--merge-mode", "local-verification", "--allow-no-checks"]
        result, config = self.onboard(options)
        self.assertEqual(result["merge_mode"], "local-verification")
        self.assertEqual(result["merge_method"], "fast-forward")
        self.assertEqual(config["merge_policy"], "auto")
        self.assertTrue(config["allow_no_checks"])
        before = (self.root / ".sfc/config.json").read_bytes()
        with self.assertRaisesRegex(Halt, "already configured"):
            sfc.onboard(self.root, None)
        self.assertEqual((self.root / ".sfc/config.json").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
