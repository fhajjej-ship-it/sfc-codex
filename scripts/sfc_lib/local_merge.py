"""Opt-in, supervisor-only fast-forward gate and readback recovery.

GitHub updateRefs compares BOTH refs atomically. The head update is a no-op,
not a lease obtained by a prior read. No merge API or push fallback is safe.
"""
from __future__ import annotations

import copy
from contextlib import nullcontext
from pathlib import Path
import re
import uuid
from time import monotonic, sleep

from .common import Halt, bounded_io, git, now, run
from . import pickup, review_policy
from .evidence_summary import verification_check_counts


class LocalMerge:
    def __init__(self, factory, *, clock=None, wait=None):
        self.f = factory
        self.clock = clock or (factory.watcher.clock if factory.watcher else monotonic)
        self.wait = wait or (factory.watcher.wait if factory.watcher else sleep)

    def snapshot(self, card, intent):
        f = self.f
        return f.gh.local_merge_snapshot(intent["pr"], card["number"],
                                         intent["base_ref"], intent["head_ref"])

    def identity(self, card, intent, snapshot, *, completion=False):
        """Fail closed on absent/partial identity or rules inventories."""
        f = self.f
        try:
            pr, issue = snapshot["pullRequest"], snapshot["issue"]
            repo = f.c["repo"]
            valid = (snapshot["nameWithOwner"] == repo and snapshot["id"] and
                     snapshot["isArchived"] is False and
                     snapshot["viewerPermission"] in ("WRITE", "MAINTAIN", "ADMIN") and
                     card["repository"]["nameWithOwner"] == repo and
                     issue["id"] == card["id"] and issue["number"] == card["number"] and
                     issue["url"] == card["url"] and issue["repository"]["nameWithOwner"] == repo and
                     pr["number"] == intent["pr"] and pr["id"] and
                     pr["url"] == intent["pr_url"] and
                     pr["headRefName"] == intent["head_ref"].removeprefix("refs/heads/") and
                     pr["baseRefName"] == f.c["base_branch"] and
                     pr["headRepository"] == {"id": snapshot["id"], "nameWithOwner": repo} and
                     pr["baseRepository"] == pr["headRepository"])
            # After a merge, the PR retains its immutable head/merge OIDs even
            # if GitHub deletes the topic ref. Completion performs no ref write.
            refs = ('base',) if completion and pr['state'] == 'MERGED' else ('head', 'base')
            for key in refs:
                ref = snapshot[key]
                valid = valid and (ref["id"] and ref["prefix"] + ref["name"] == intent[key + "_ref"] and
                                   ref["target"]["__typename"] == "Commit" and
                                   re.fullmatch(r"[0-9a-f]{40}", ref["target"]["oid"]) and
                                   re.fullmatch(r"[0-9a-f]{40}", ref["target"]["tree"]["oid"]))
                # Avoid even an administrator's implicit rule bypass. This mode
                # is deliberately limited to refs with no active rules.
                if not completion and (ref["branchProtectionRule"] is not None or
                        type(ref["rules"]["totalCount"]) is not int or ref["rules"]["totalCount"] != 0):
                    raise Halt("Local verification refuses protected/ruled refs; use human or github-protected mode")
            if intent.get("repository_id") and (snapshot["id"] != intent["repository_id"] or
                    pr["id"] != intent["pr_id"] or
                    any(snapshot[k]["id"] != intent[k + "_id"] for k in refs)):
                valid = False
            closing = pr["closingIssuesReferences"]
            valid = valid and (completion or (type(closing["totalCount"]) is int and
                               closing["totalCount"] == len(closing["nodes"]) and
                               card["id"] in [v["id"] for v in closing["nodes"]]))
        except (KeyError, TypeError, AttributeError):
            valid = False
        if not valid:
            raise Halt("Local merge repository/PR/ref/issue identity or complete schema could not be verified")
        return pr, issue

    def clean(self, wt, head):
        if git(wt, "rev-parse", "HEAD") != head or git(wt, "status", "--porcelain", "--untracked-files=all"):
            raise Halt("Local merge requires a clean unchanged candidate worktree")
        for name in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REBASE_HEAD"):
            path = Path(git(wt, "rev-parse", "--git-path", name))
            if (path if path.is_absolute() else Path(wt) / path).exists():
                raise Halt("Local merge candidate has unfinished integration")

    def eligible(self, card, wt, pr, head, base, *, verify_evidence=True):
        from .factory import check_explanation, checks_pass, policy_reasons
        f = self.f
        if not f.allow_merge or f.c["merge_policy"] != "auto" or f.c.get("merge_mode") != "local-verification":
            raise Halt("Local merge needs explicit project opt-in and run merge authority")
        if not f.pinned(card, head, base):
            raise Halt("Local merge rejected missing, failed or stale independent lane evidence")
        if verify_evidence:
            f.verification_evidence(card, head, base)
        self.clean(wt, head)
        # Local inventories include BOTH sides of renames and deleted paths;
        # remote PR inventory alone must not conceal a sensitive old pathname.
        paths = git(wt, "diff", "--no-renames", "--name-only", "-z", base, head).split("\0")
        lines, binary = 0, False
        for row in git(wt, "diff", "--no-renames", "--numstat", "-z", base, head).split("\0"):
            if not row:
                continue
            added, deleted, _ = row.split("\t", 2)
            if added == "-" or deleted == "-":
                binary = True
            else:
                lines += int(added) + int(deleted)
        policy_pr = {**pr, "additions": max(lines, pr["additions"] + pr["deletions"]),
                     "deletions": 0, "files": pr["files"] + [{"path": p} for p in paths if p]}
        reasons = policy_reasons(f.c, card, policy_pr)
        if review_policy.human_required(f, card):
            reasons.append('review risk policy requires human review')
        if binary:
            reasons.append("binary diff needs human review")
        if not any(paths):
            reasons.append("candidate has no change against base")
        if reasons:
            f.move(card, "Blocked", "; ".join(reasons))
            f.activity.report(card, "Waiting", "policy", progress=review_policy.activity_progress(f, card))
            return False
        checks = f.gh.local_merge_checks(head)
        if not isinstance(checks, list):
            raise Halt("GitHub check inventory unavailable; absence was not established")
        candidate_checks = {**pr, "statusCheckRollup": checks}
        if not checks_pass(candidate_checks, f.c["allow_no_checks"]):
            f.event(card, "Waiting for successful GitHub checks; no local merge attempted")
            f.activity.report(card, "Waiting", "ci", reasons=[check_explanation(candidate_checks)],
                              progress=review_policy.activity_progress(f, card))
            return False
        return True

    def gate(self, card, wt, head, base):
        from .blocked_recovery import RecoveryPinMismatch, admitted, require_live, require_pins
        f, r = self.f, self.f.record(card)
        f.ensure_running()
        if r.get("local_merge"):
            raise Halt("Existing local merge journal must be reconciled; never retry its write")
        pr = f.assert_remote(card, wt)
        if not self.eligible(card, wt, pr, head, base, verify_evidence=False):
            return
        if run(["git", "merge-base", "--is-ancestor", base, head], wt, check=False).returncode:
            if admitted(r):
                raise RecoveryPinMismatch('Recovered candidate would require source integration')
            f.move(card, "QA", "Local fast-forward requires current base in reviewed head; integrate and repeat all lanes")
            return
        if head == base:
            raise Halt("Local merge requires a distinct candidate commit")
        f.gh.verify_repository()
        f.gh.local_merge_capabilities()
        intent = {"id": uuid.uuid4().hex, "pr": pr["number"], "pr_url": pr["url"],
                  "head_ref": "refs/heads/" + r["branch"], "base_ref": "refs/heads/" + f.c["base_branch"],
                  "head": head, "base": base, "tree": git(wt, "rev-parse", "HEAD^{tree}")}
        if intent["head_ref"] == intent["base_ref"]:
            raise Halt("Local merge head and base refs must differ")
        snapshot = self.snapshot(card, intent)
        remote_pr, issue = self.identity(card, intent, snapshot)
        if remote_pr["state"] != "OPEN" or remote_pr["isDraft"] is not False or issue["state"] != "OPEN":
            raise Halt("Local merge requires an open ready PR and open issue")
        if not self.matches_before(snapshot, intent):
            if admitted(r):
                raise RecoveryPinMismatch('Recovered merge refs changed before local verification')
            f.move(card, "QA", "Local merge refs changed; integrate current base and repeat independent evidence")
            return
        intent.update(repository_id=snapshot["id"], pr_id=remote_pr["id"],
                      head_id=snapshot["head"]["id"], base_id=snapshot["base"]["id"])
        scratch = f.dir / "worktrees" / f"local-gate-{card['number']}-{intent['id'][:8]}"
        f.update(card, local_merge_gate={"worktree": str(scratch), "head": head, "base": base})
        git(f.root, "worktree", "add", "--detach", scratch, head)
        # No synthetic merge tree: the exact reviewed commit is the candidate.
        passed = f.verify(card, scratch, base)
        require_live(f, card)
        if not passed:
            f.fail(card, {"verdict": "fail", "summary": "Local candidate verification failed"})
            return
        self.clean(scratch, head)
        if not f.fresh_task(card):
            return
        pr = f.assert_remote(card, wt)
        live_base = f.fetch()
        if live_base != base:
            require_pins(f, card, wt, live_base, integration_worktree=scratch)
            f.move(card, "QA", "Base moved during local gate; integrate and repeat all lanes")
            return
        if not self.eligible(card, wt, pr, head, base):
            return
        snapshot = self.snapshot(card, intent)
        remote_pr, issue = self.identity(card, intent, snapshot)
        if not self.matches_before(snapshot, intent):
            if admitted(r):
                raise RecoveryPinMismatch('Recovered merge refs changed during local verification')
            f.move(card, "QA", "Local merge refs changed; integrate and repeat all lanes")
            return
        if remote_pr["state"] != "OPEN" or remote_pr["isDraft"] is not False or issue["state"] != "OPEN":
            raise Halt("PR or issue completion state changed during local gate")
        f.ensure_running()
        if not f.fresh_task(card):
            return
        self.clean(wt, head)
        self.clean(scratch, head)
        f.verification_evidence(card, head, base)
        if not f.watcher:
            intent['completion_scope'] = (copy.deepcopy(card['_pickup_scope']) if f.include_backlog
                                          else pickup.routing_scope(f.gh)[0])
        if not f.recheck_merge_reviews(card, wt, head, base):
            return
        intent.update(phase="intent", time=now(), worktree=str(scratch),
                      config_digest=f.config_digest, task_digest=f.task_digest(card),
                      source_review_config_sha256=review_policy.source_review_config(f.c),
                      card_identity={k: card[k] for k in ("id", "item_id", "url", "repository", "number")},
                      verification=copy.deepcopy(r["verification"]), receipts=copy.deepcopy(r["receipts"]))
        if f.c.get('review_policy') is not None:
            # Live provider/route checks already passed. Pin that exact route
            # and check the review ages again at the recorded dispatch time.
            intent['review_route'] = copy.deepcopy(r['review_route'])
            intent['review_validated_at'] = intent['time']
            if not self.completion_evidence(card, intent):
                raise Halt('Review evidence expired or changed before the atomic merge')
        require_live(f, card)
        require_pins(f, card, wt, base, integration_worktree=scratch)
        f.ensure_running()
        # Persist full pins/evidence BEFORE the only remote ref write. The
        # journal latches even if stopped or killed before dispatch.
        f.update(card, local_merge=intent)
        f.event(card, f"atomic local merge requested for reviewed head {head} and tested base {base}")
        f.ensure_running()
        # Persisting the journal can outlast a review receipt. Keep the original
        # evidence timestamp for recovery and check current age before dispatch.
        if not review_policy.fresh_receipts(f, card):
            raise Halt('Review evidence expired before atomic local merge dispatch')
        try:
            f.gh.atomic_fast_forward(intent)
        except Exception as exc:
            intent.update(phase="ambiguous", error=str(exc), time=now())
            f.update(card, local_merge=intent)
            raise Halt("Atomic local merge did not return a confirmed response; retained journal, no retry") from exc
        intent.update(phase="responded", time=now())
        if f.watcher:
            f.watcher.acknowledge(card, intent)
        f.update(card, local_merge=intent)
        self.reconcile(card)

    @staticmethod
    def matches_before(snapshot, intent):
        return (snapshot["head"]["target"]["oid"] == intent["head"] and
                snapshot["head"]["target"]["tree"]["oid"] == intent["tree"] and
                snapshot["base"]["target"]["oid"] == intent["base"] and
                snapshot["pullRequest"]["headRefOid"] == intent["head"] and
                snapshot["pullRequest"]["baseRefOid"] == intent["base"])

    def reconcile(self, card):
        """Read only until completion is proven; never resend a ref mutation."""
        owned = card
        f, intent = self.f, self.f.record(card)['local_merge']
        # The timeout callback must not launch commands: running() validates the
        # saved config via git, which would recursively request this allowance.
        # Keep that validation in the separate reconciliation guards below.
        allowance = None
        if f.watcher and intent.get('watch_completion'):
            allowance = lambda: f.watcher.completion_remaining(card, intent)
        with bounded_io(allowance) if allowance else nullcontext():
            card = self.current_card(card)
            self._reconcile(card)
            if 'status' in owned:  # Watch cutoff recovery may pass only the pinned identity.
                owned.update(status='Done', state='CLOSED')

    def observe_completion(self, card, intent, kind, observed):
        if self.f.watcher and intent.get('watch_completion'):
            intent.setdefault('completion_observations', []).append({
                'kind': kind, 'time': now(), 'clock': self.f.watcher.clock(),
                'after_cutoff': self.f.watcher.clock() >= intent['watch_completion']['work_deadline'],
                'observed': copy.deepcopy(observed)})
            self.f.update(card, local_merge=intent)

    def current_card(self, card):
        if self.f.watcher:
            return self.watch_authority(card)
        f, r = self.f, self.f.record(card)
        intent = r['local_merge']
        self.running(card, intent)
        if (not f.allow_merge or f.c['merge_policy'] != 'auto' or
                f.c.get('merge_mode') != 'local-verification' or
                (f.include_backlog and card['number'] not in f.issue_scope)):
            raise Halt('Local merge completion outside authorized run scope/auto-merge authority')
        scope = pickup.scope(f.gh) if f.include_backlog else pickup.routing_scope(f.gh)[0]
        if scope != intent.get('completion_scope', scope):
            raise Halt('Local merge completion routing scope changed')
        current = pickup.card(f.gh, card['item_id'], scope)
        after = pickup.scope(f.gh) if f.include_backlog else pickup.routing_scope(f.gh)[0]
        if (after != scope or f.creation_problem(current) or
                intent['card_identity'] != {k: current.get(k) for k in intent['card_identity']} or
                f.task_digest(current) != intent['task_digest'] or current['status'] not in ('Review', 'Done') or
                (current['status'] == 'Done' and not intent.get('done_requested')) or
                r.get('head') != intent['head'] or r.get('pr') != intent['pr'] or
                'refs/heads/' + r.get('branch', '') != intent['head_ref'] or
                r.get('receipts') != intent['receipts'] or r.get('verification') != intent['verification'] or
                r.get('base') != intent['base'] or not self.completion_evidence(current, intent) or
                (f.include_backlog and r.get('claim', {}).get('state') != 'verified')):
            raise Halt('Card moved or local merge completion task/stage/identity/evidence changed')
        self.running(card, intent)
        return current

    def completion_evidence(self, card, intent):
        f = self.f
        frozen_at = None
        if 'review_route' in intent:
            # An in-flight transaction cannot obtain new review authority.
            # Recheck the pinned bytes, with freshness evaluated at dispatch.
            if f.record(card).get('review_route') != intent['review_route']:
                return False
            frozen_at = intent.get('review_validated_at')
            if not isinstance(frozen_at, str) or not frozen_at:
                return False
        if not f.pinned(card, intent['head'], intent['base'], frozen_at=frozen_at):
            return False
        f.verification_evidence(card, intent['head'], intent['base'], freshness=frozen_at is None)
        return True

    def completed_authority(self, card):
        current = self.current_card(card)
        if current['status'] != 'Done' or current['state'] != 'CLOSED':
            raise Halt('Completed activity requires the matching closed issue still in Done; preserve manual edits')

    def running(self, card, intent):
        completion = intent if self.f.watcher and intent.get('watch_completion') else None
        self.f.ensure_running(completion=completion)
        if completion:
            return self.f.watcher.completion_remaining(card, intent)

    def _reconcile(self, card):
        f, r = self.f, self.f.record(card)
        intent = r["local_merge"]
        if (intent["config_digest"] != f.config_digest or intent["task_digest"] != f.task_digest(card) or
                intent["card_identity"] != {k: card.get(k) for k in intent["card_identity"]} or
                r.get("head") != intent["head"] or r.get("pr") != intent["pr"] or
                "refs/heads/" + r.get("branch", "") != intent["head_ref"] or
                "refs/heads/" + f.c["base_branch"] != intent["base_ref"]):
            raise Halt("Local merge journal pins changed; human reconciliation required")
        while True:
            snapshot = self.snapshot(card, intent)
            self.observe_completion(card, intent, 'merge', snapshot)
            intent.update(observed=snapshot, observed_at=now())
            f.update(card, local_merge=intent)
            pr, issue = self.identity(card, intent, snapshot)
            # Only acknowledged, exact atomic outcomes may receive a short
            # read-only recognition window. Old bases, identity/rule drift and
            # ambiguous writes never enter this path.
            pending = (intent['phase'] == 'responded' and
                       snapshot['base']['target']['oid'] == intent['head'] and
                       snapshot['base']['target']['tree']['oid'] == intent['tree'] and
                       snapshot['head']['target']['oid'] == intent['head'] and
                       snapshot['head']['target']['tree']['oid'] == intent['tree'] and
                       pr['headRefOid'] == intent['head'] and pr['isDraft'] is False and
                       ((pr['state'] == 'OPEN' and issue['state'] in ('OPEN', 'CLOSED')) or
                        (pr['state'] == 'MERGED' and (pr['mergeCommit'] or {}).get('oid') == intent['head'] and
                         issue['state'] == 'OPEN')))
            if not pending or intent.get('confirmation_reads', 0) >= 5:
                break
            if f.fetch() != intent['head']:
                break
            intent.update(confirmation_reads=intent.get('confirmation_reads', 0) + 1,
                          observed=snapshot, observed_at=now())
            f.update(card, local_merge=intent)
            end = self.clock() + 1
            while True:
                delay = min(0.1, end - self.clock())
                if delay <= 0:
                    break
                self.running(card, intent)
                self.wait(delay)
            self.current_card(card)
        intent["observed"] = snapshot
        intent["observed_at"] = now()
        f.update(card, local_merge=intent)
        pr, issue = self.identity(card, intent, snapshot)
        remote_base = snapshot["base"]["target"]["oid"]
        landed = (snapshot["head"]["target"]["oid"] == intent["head"] and
                  snapshot["head"]["target"]["tree"]["oid"] == intent["tree"] and
                  pr["headRefOid"] == intent["head"] and
                  pr["state"] == "MERGED" and (pr["mergeCommit"] or {}).get("oid") == intent["head"] and
                  issue["state"] == "CLOSED")
        if landed:
            # A subsequent ordinary writer may advance the intended base. Only
            # accept it when the approved commit remains reachable, with its
            # exact tree, and the fetch agrees with the remote snapshot.
            landed = (f.fetch() == remote_base and
                      git(f.root, "rev-parse", remote_base + "^{tree}") == snapshot['base']['target']['tree']['oid'] and
                      git(f.root, "rev-parse", intent["head"] + "^{tree}") == intent["tree"] and
                      run(["git", "merge-base", "--is-ancestor", intent["head"], remote_base],
                          f.root, check=False).returncode == 0)
        if not landed:
            intent["phase"] = "unresolved"
            f.update(card, local_merge=intent)
            raise Halt("Local merge outcome/PR/issue not confirmed on intended base; retained for readback, no retry")
        current = self.current_card(card)
        if (current is None or intent["card_identity"] !=
                {k: current.get(k) for k in intent["card_identity"]} or
                f.task_digest(current) != intent["task_digest"] or current["state"] != "CLOSED"):
            raise Halt("Local merge Project card/issue changed; human reconciliation required")
        card = current
        from .blocked_recovery import admitted, require_retained_evidence
        require_retained_evidence(f, admitted(r))
        # A lost response or incomplete Done write keeps all refs/worktrees for
        # inspection. Stage writes are also latched, never blindly repeated.
        if card["status"] != "Done":
            if intent.get("done_requested"):
                raise Halt("Ambiguous Done transition; inspect board before manual recovery")
            if card["status"] != "Review":
                raise Halt("Card moved during local merge; human reconciliation required")
            intent.update(phase="confirmed", done_requested=now())
            f.update(card, local_merge=intent)
            completion = intent if f.watcher and intent.get('watch_completion') else None
            f.move(card, "Done", f"Confirmed PR #{pr['number']} and issue closed on intended base",
                   completion=completion)
            observed = self.current_card(card)
            self.observe_completion(card, intent, 'Done', observed)
            if (observed is None or observed["status"] != "Done" or observed["state"] != "CLOSED" or
                    intent["card_identity"] != {k: observed.get(k) for k in intent["card_identity"]} or
                    f.task_digest(observed) != intent["task_digest"]):
                raise Halt("Local merge Done transition not verified; inspect journal without retry")
        else:
            f.sync_done_status(card, guard=lambda: self.running(card, intent))
        # Completed activity must finish before the journal becomes terminal.
        # Its independent pending/error latch is never cleared on recovery.
        f.activity.report(card, "Completed", "done",
                          guard=lambda: self.running(card, intent),
                          prewrite=lambda: self.completed_authority(card),
                          check_counts=verification_check_counts(intent["verification"]))
        intent.update(phase="done", confirmed_at=now())
        from .blocked_recovery import finish
        with f.mutex:
            finish(f, card, save=False)
            f.update(card, local_merge=intent)

    def watch_authority(self, card):
        f, r = self.f, self.f.record(card)
        intent = r['local_merge']
        self.running(card, intent)
        if intent.get('watch_completion'):
            scope = pickup.scope(f.gh)
            if scope != intent['watch_completion']['scope']:
                raise Halt('Watcher completion Project/field scope changed; inspect retained journal')
            current = pickup.card(f.gh, card['item_id'], scope)
        else:
            current = next((c for c in f.queue_cards() if c['number'] == card['number']), None)
        if current is None or f.watcher.excludes(current):
            raise Halt('Local merge journal outside watcher membership/hold/exclusion policy; no recovery write')
        if not f.allow_merge or f.c['merge_policy'] != 'auto':
            raise Halt('Watcher local journal recovery requires separate auto-merge authority')
        if intent.get('watch_completion'):
            if f.creation_problem(current):
                raise Halt('Watcher completion ticket creation evidence unresolved; inspect retained journal')
            if (intent['card_identity'] != {k: current.get(k) for k in intent['card_identity']} or
                    f.task_digest(current) != intent['task_digest'] or
                    r.get('receipts') != intent['receipts'] or r.get('verification') != intent['verification'] or
                    r.get('base') != intent['base'] or
                    not self.completion_evidence(current, intent) or
                    r.get('claim', {}).get('state') != 'verified'):
                raise Halt('Watcher completion identity/task/evidence pins changed; inspect retained journal')
        self.running(card, intent)
        return current
