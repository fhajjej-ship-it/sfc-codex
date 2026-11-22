"""Explicit, one-use verification of an updated head on a retained PR."""
from __future__ import annotations

import copy
from pathlib import Path
import re
import uuid

from . import pickup, review_policy
from .blocked_recovery import admitted, binding, pending_stage
from .common import Halt, common_dir, git, now, read_json, run, write_json
from .external_merge import ExternalMerge
from .verification import preview_fingerprints


def live_candidate(f, card, wt, head, base, pr_id=None):
    live = pickup.cards(f.gh, [card['number']])
    if live != [card]:
        raise Halt('Adoption card, task or Project scope changed')
    pickup.prerequisites(f.gh, card)
    pr = f.assert_remote(card, wt, expected_head=head)
    if not pr.get('id') or (pr_id is not None and pr['id'] != pr_id):
        raise Halt('Adoption PR identity changed')
    branch = f.record(card)['branch']
    branch_ref, base_ref = 'refs/heads/' + branch, 'refs/heads/' + f.c['base_branch']
    refs = dict(line.split('\t', 1)[::-1] for line in
                git(wt, 'ls-remote', '--exit-code', 'origin', branch_ref, base_ref).splitlines())
    if refs.get(branch_ref) != head or refs.get(base_ref) != base:
        raise Halt('Adoption PR head or base changed; preserve the Blocked card')
    return pr


def clean_head(wt, head):
    if (git(wt, 'rev-parse', 'HEAD') != head or
            git(wt, 'status', '--porcelain', '--untracked-files=all')):
        raise Halt('Adoption requires an unchanged, clean owned worktree')


def inspect(f, card, pr_number, candidate):
    from .corrective_recovery import available
    from .ticket_recovery import pending

    r = f.record(card)
    if (card['state'] != 'OPEN' or card['status'] != 'Blocked' or r.get('status') != 'Blocked' or
            pr_number != r.get('pr') or not r.get('head') or candidate == r['head'] or
            r.get('handoff') not in (None, r['head']) or 'qa' in card['labels'] or
            any(label.casefold() == f.c.get('watch', {}).get('hold_label', 'sfc-hold').casefold()
                for label in card['labels'])):
        raise Halt('Adoption requires a newer commit on the saved open PR of an unheld Blocked card')
    if (pending(r) or available(r) or admitted(r) or pending_stage(r) or r.get('qa_repair') or
            r.get('done_status', {}).get('state') in ('pending', 'intent') or
            any(r.get(k, {}).get('phase') == 'intent' for k in
                ('initial_block_transition', 'qa_repair_transition', 'ticket_recovery_admission', 'corrective_admission')) or
            any(a.get('phase') in ('running', 'handoff-intent') for a in r.get('pr_adoptions', []))):
        raise Halt('Another recovery or pending write requires inspection before adoption')
    claim = r.get('claim', {}).get('observed', {})
    if (claim.get('_pickup_scope') != card.get('_pickup_scope') or not claim or
            f.task_digest(claim) != f.task_digest(card)):
        raise Halt('Adoption ticket or Project scope differs from the saved claim')
    with preview_fingerprints():
        pins = binding(f, card, allow_handoff=True)
    wt = Path(pins['worktree'])
    pr = live_candidate(f, card, wt, candidate, pins['base'])
    # Obtain the exact objects without moving a branch, checking out a file, or writing FETCH_HEAD.
    git(wt, 'fetch', '--no-tags', '--no-write-fetch-head', 'origin', candidate)
    if (git(wt, 'cat-file', '-t', candidate) != 'commit' or
            any(run(['git', 'merge-base', '--is-ancestor', ancestor, candidate], wt, check=False).returncode
                for ancestor in (pins['head'], pins['base']))):
        raise Halt('Adopted commit must descend from both the saved PR head and base')
    live_candidate(f, card, wt, candidate, pins['base'], pr['id'])
    return pins | {'candidate': candidate, 'pr_id': pr['id']}


def recover(f, *, issue, pr_number, candidate, request_id, reason, apply=False):
    """Preview or spend one explicit check-and-review allowance under the supervisor lock."""
    if (min(issue, pr_number) <= 0 or not re.fullmatch('[0-9a-f]{40}', candidate) or
            not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,100}', request_id) or not reason.strip()):
        raise Halt('Adoption requires an issue, PR, full commit SHA, stable request ID and reason')
    f.gh.verify_repository()
    record = f.state['cards'].get(str(issue), {})
    journal = f.dir / 'recoveries' / (request_id + '.json')
    if journal.exists() or any(a['head'] == candidate for a in record.get('pr_adoptions', [])):
        raise Halt('Adoption request or commit already has a recovery receipt; inspect without retry')
    cards = pickup.cards(f.gh, [issue])
    if len(cards) != 1:
        raise Halt('Adoption issue or Project card is missing or ambiguous')
    card = cards[0]
    pins = inspect(f, card, pr_number, candidate)
    if not apply:
        return {'state': 'preview', 'issue': issue, 'pr': pr_number, 'previous_head': pins['head'],
                'head': candidate, 'base': pins['base'], 'allow_merge': False}
    shared_stop = common_dir(f.root) / 'sfc-stop'
    stop = f.dir / 'stop'
    if not (stop.exists() or shared_stop.exists()):
        raise Halt('Stop the supervisor before adopting an updated PR commit')
    f.processes.shared_stop_path = shared_stop
    wt, base = Path(pins['worktree']), pins['base']
    history_keys = ('verification', 'receipts', 'reviewed_completion', 'review_route', 'handoff',
                    'handoff_reasons', 'feedback', 'blocker', 'inspection_rejection', 'recovery_wait')
    prior = {key: copy.deepcopy(record[key]) for key in history_keys if key in record}
    attempt = {'version': 1, 'request_id': request_id, 'reason': reason.strip(), 'head': candidate,
               'phase': 'running', 'authorized_at': now(), 'pins': pins, 'previous_evidence': prior}
    # Persist the candidate-level debit before the request-level journal so a
    # crash between the two writes cannot admit the same head under a new ID.
    record.setdefault('pr_adoptions', []).append(attempt)
    f.save()
    if read_json(f.state_path)['cards'][str(issue)]['pr_adoptions'][-1] != attempt:
        raise Halt('Saved adoption allowance differs; inspect before recovery')
    write_json(journal, attempt)

    def finish(phase, **values):
        attempt.update(phase=phase, finished=now(), **values)
        f.save()
        write_json(journal, attempt)

    def current():
        f.ensure_running()
        clean_head(wt, candidate)
        pr = live_candidate(f, card, wt, candidate, base, pins['pr_id'])
        f.ensure_running()
        return pr

    def failed(lane):
        finish('failed', failed_lane=lane)
        current()
        action = 'check_failed' if lane == 'checks' else 'findings'
        f.activity.report(card, 'Failed', action, guard=f.ensure_running, prewrite=current)
        return {'state': 'blocked', 'issue': issue, 'pr': pr_number, 'head': candidate, 'failed_lane': lane}

    try:
        live_candidate(f, card, wt, candidate, base, pins['pr_id'])
        clean_head(wt, pins['head'])
        shared_stop.unlink(missing_ok=True)
        stop.unlink(missing_ok=True)
        f.ensure_running()
        git(wt, 'merge', '--ff-only', '--no-edit', candidate)
        clean_head(wt, candidate)
        for key in history_keys:
            record.pop(key, None)
        f.run_dir = f.dir / 'runs' / ('adopt-pr-' + uuid.uuid4().hex)
        f.run_dir.mkdir(parents=True, mode=0o700)
        f.update(card, head=candidate, worktree=str(wt))
        current()
        f.activity.report(card, 'Running', 'checks', guard=f.ensure_running, prewrite=current)
        if not f.verify(card, wt, base, force_check_ids=tuple(range(1, len(f.c['verify_commands']) + 1)),
                        report_activity=False):
            return failed('checks')
        current()
        policy = review_policy.settings(f.c)
        if policy is not None:
            from .review_retention import prepare
            route = review_policy.plan(f, card, wt, base, consider_inspection=False)
            prepare(f, card, wt, base, route)
            f.update(card, review_route=route)
        for lane in ('qa', 'security', 'review'):
            current()
            if f.lane(card, lane, wt, base, fresh=True)['verdict'] != 'pass':
                return failed(lane)
        open_read_started = now()
        pr = current()
        f.verification_evidence(card, candidate, base)
        if (not f.pinned(card, candidate, base) or
                policy is not None and not review_policy.specialist_dependencies_complete(f, card)):
            return failed('review-evidence')
        # No stage move, Build, push or merge. An uncertain handoff write is
        # retained for readback and never retried by a new adoption.
        attempt['phase'] = 'handoff-intent'
        f.save()
        write_json(journal, attempt)
        current()
        f.complete_pr_summary(card, pr, candidate, base)
        if current()['isDraft']:
            f.gh.ready(pr_number)
        pr = current()
        if pr['isDraft']:
            raise Halt('Adopted PR readiness could not be verified')
        ExternalMerge(f).remember(card, pr, candidate, base, open_read_started=open_read_started)
        from .factory import policy_explanations
        reasons = policy_explanations(f.c, card, pr, False)
        f.activity.report(card, 'Waiting', 'human', reasons=reasons, guard=f.ensure_running, prewrite=current)
        f.update(card, handoff=candidate, handoff_reasons=reasons)
        finish('finished')
        return {'state': 'awaiting-manual-merge', 'issue': issue, 'pr': pr_number,
                'head': candidate, 'allow_merge': False}
    except Exception:
        if attempt['phase'] == 'running':
            finish('failed')
        raise
