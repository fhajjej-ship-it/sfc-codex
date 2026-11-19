"""Read back human and protected GitHub merge outcomes before completion."""
from __future__ import annotations

import copy
import datetime as dt
import re

from . import pickup, review_policy
from .activity import ActivityError
from .common import Halt, git, now, run
from .evidence_summary import verification_check_counts

IDENTITY = ('number', 'id', 'item_id', 'url', 'repository')


class ProtectedMergeUncertain(ActivityError):
    """A protected merge outcome owns recovery until readback proves completion."""


class ExternalMerge:
    def __init__(self, factory):
        self.f = factory

    def capture(self, card, pr, head, base, *, open_read_started, review_boundary):
        f, r = self.f, self.f.record(card)
        review = {}
        if review_policy.settings(f.c) is not None:
            validated_at = now()
            if not review_policy.fresh_receipts(f, card, at=validated_at):
                raise Halt(f'Review evidence expired before {review_boundary}')
            review = {'review_route': copy.deepcopy(r['review_route']), 'review_validated_at': validated_at,
                      'open_read_started': open_read_started}
        return {**review,
            'card_identity': {k: card.get(k) for k in IDENTITY},
            'pr': pr['number'], 'pr_url': pr['url'], 'pr_id': pr.get('id'), 'head': head, 'base': base,
            'head_ref': 'refs/heads/' + r['branch'], 'base_ref': 'refs/heads/' + f.c['base_branch'],
            'task_digest': f.task_digest(card), 'config_digest': f.config_digest,
            'source_review_config_sha256': review_policy.source_review_config(f.c),
            'scope': copy.deepcopy(card.get('_pickup_scope') or {'project': f.gh.metadata()}),
            'receipts': copy.deepcopy(r['receipts']), 'verification': copy.deepcopy(r['verification'])}

    def remember(self, card, pr, head, base, *, open_read_started):
        f, r = self.f, self.f.record(card)
        proof = self.capture(card, pr, head, base, open_read_started=open_read_started,
                             review_boundary='the human merge handoff')
        previous = r.get('reviewed_completion')
        if 'review_validated_at' in proof and isinstance(previous, dict):
            first = previous.get('review_validated_at')
            first_open = previous.get('open_read_started')
            if (isinstance(first, str) and first and isinstance(first_open, str) and first_open and
                    {**previous, 'review_validated_at': proof['review_validated_at'],
                     'open_read_started': open_read_started} == proof):
                proof.update(review_validated_at=first, open_read_started=first_open)
        f.update(card, reviewed_completion=proof)

    def protected(self, card, pr, head, base, tree, *, open_read_started):
        from .blocked_recovery import admitted, require_retained_evidence
        f = self.f
        intent = self.capture(card, pr, head, base, open_read_started=open_read_started,
                              review_boundary='GitHub merge dispatch')
        if not intent['pr_id']:
            raise Halt('Protected merge requires a verified PR identity')
        intent.update(kind='protected-squash', stage='Review', phase='observing',
                      merge_response='pending', expected_tree=tree, merge_requested=now())
        recovery = admitted(f.record(card))
        if recovery:
            intent['recovery_id'] = recovery['id']
        # Once saved, all continuation uses the same readback path as external
        # completion. Only this initial call can dispatch the merge.
        f.update(card, external_completion=intent)
        try:
            require_retained_evidence(f, recovery)
            f.ensure_running()
            if not review_policy.fresh_receipts(f, card):
                raise Halt('Review evidence expired before GitHub merge dispatch')
        except Halt:
            # We know this invocation did not call the mutation. Preserve
            # the attempted intent separately from uncertain remote outcomes.
            with f.mutex:
                record = f.record(card)
                record['undispatched_protected_merge'] = {**intent, 'phase': 'not-dispatched'}
                record.pop('external_completion')
                f.save()
            raise
        try:
            try:
                f.gh.merge(pr['number'], head)
            except Exception as exc:
                intent.update(merge_response='ambiguous', merge_response_error=str(exc))
            else:
                intent['merge_response'] = 'acknowledged'
            f.update(card, external_completion=intent)
            self.reconcile(card)
        except Exception as exc:
            raise ProtectedMergeUncertain('Protected GitHub merge completion unresolved; retained journal for readback') from exc

    def proof(self, card):
        f, r = self.f, self.f.record(card)
        proof = r.get('reviewed_completion')
        if 'reviewed_completion' in r:
            if not isinstance(proof, dict):
                raise Halt('Saved reviewed completion receipt incomplete; inspect local evidence')
            return copy.deepcopy(proof)
        # Compatibility for saved priority/watcher human handoffs: the verified
        # claim supplies the original issue/item/scope; lane receipts supply task
        # and configuration pins. Never infer ownership from a current card.
        claim = r.get('claim', {})
        before = claim.get('before', {})
        if (claim.get('state') != 'verified' or not before.get('_pickup_scope') or
                not r.get('handoff') or r['handoff'] != r.get('head') or
                not isinstance(r.get('verification'), dict) or not isinstance(r.get('receipts'), dict) or
                any(not r.get(k) for k in ('head', 'base', 'branch', 'pr'))):
            raise Halt('No saved reviewed ticket identity; inspect handoff/claim evidence manually')
        return {'card_identity': {k: before.get(k) for k in IDENTITY},
                'pr': r['pr'], 'pr_url': f"https://github.com/{f.c['repo']}/pull/{r['pr']}",
                'head': r['head'], 'base': r['base'], 'head_ref': 'refs/heads/' + r['branch'],
                'base_ref': 'refs/heads/' + f.c['base_branch'],
                'task_digest': r.get('verification', {}).get('task_digest'),
                'config_digest': r.get('verification', {}).get('config_digest'),
                'scope': copy.deepcopy(before['_pickup_scope']),
                'receipts': copy.deepcopy(r.get('receipts')), 'verification': copy.deepcopy(r.get('verification'))}

    def current(self, card, intent, *, preflight=False):
        f, r = self.f, self.f.record(card)
        f.ensure_running()
        required = {'card_identity', 'scope', 'config_digest', 'task_digest', 'head', 'base',
                    'pr', 'head_ref', 'base_ref', 'pr_url', 'receipts', 'verification'}
        if (not isinstance(intent, dict) or not required <= intent.keys() or
                not isinstance(intent['card_identity'], dict) or set(intent['card_identity']) != set(IDENTITY) or
                not isinstance(intent['scope'], dict) or not isinstance(intent['scope'].get('project'), dict) or
                not isinstance(intent['receipts'], dict) or not isinstance(intent['verification'], dict)):
            raise Halt('External completion receipt incomplete; inspect retained evidence')
        recovery = None
        if intent.get('kind') == 'protected-squash':
            tree = intent.get('expected_tree')
            if not intent.get('pr_id') or not isinstance(tree, str) or not re.fullmatch('[0-9a-f]{40}', tree):
                raise Halt('Protected merge completion lacks an exact PR or integration tree')
            from .blocked_recovery import admitted, same_window_scope
            recovery = admitted(r)
            if intent.get('recovery_id') != (recovery or {}).get('id') or (recovery and (
                    not f.watcher or card['number'] not in f.issue_scope or
                    not same_window_scope(f, recovery, f.watcher.session))):
                raise Halt('Protected merge completion is outside its saved recovery scope')
        if not all(intent['card_identity'].values()):
            raise Halt('Saved reviewed ticket identity incomplete; inspect retained handoff evidence')
        if f.include_backlog and card['number'] not in f.issue_scope:
            raise Halt('External completion outside explicit run issue scope')
        scope = pickup.scope(f.gh) if f.include_backlog else pickup.routing_scope(f.gh)[0]
        pinned = intent['scope'] if f.include_backlog else {'project': intent['scope']['project']}
        if scope != pinned:
            raise Halt('External completion Project/routing scope changed')
        current = pickup.card(f.gh, card['item_id'], scope)
        after = pickup.scope(f.gh) if f.include_backlog else pickup.routing_scope(f.gh)[0]
        identity = intent['card_identity']
        if (after != scope or not all(identity.values()) or
                identity != {k: current.get(k) for k in IDENTITY} or
                current['repository']['nameWithOwner'] != f.c['repo'] or
                f.creation_problem(current) or (f.watcher and f.watcher.excludes(current))):
            raise Halt('External completion membership/identity/creation/hold changed; preserve human edits')
        claim = r.get('claim')
        if claim and (claim.get('state') != 'verified' or
                      identity != {k: claim.get('before', {}).get(k) for k in IDENTITY}):
            raise Halt('External completion claim identity is unresolved')
        if f.include_backlog and not claim:
            raise Halt('External completion needs the saved verified scoped claim')
        if (intent['config_digest'] != f.config_digest or intent['task_digest'] != f.task_digest(current) or
                r.get('head') != intent['head'] or r.get('base') != intent['base'] or
                r.get('pr') != intent['pr'] or 'refs/heads/' + r.get('branch', '') != intent['head_ref'] or
                'refs/heads/' + f.c['base_branch'] != intent['base_ref'] or
                r.get('receipts') != intent['receipts'] or r.get('verification') != intent['verification'] or
                (r.get('handoff') and r['handoff'] != intent['head']) or
                ('review_route' in intent and r.get('review_route') != intent['review_route'])):
            raise Halt('External completion task/head/base/config/review evidence changed; inspect saved receipts')
        if not preflight or 'review_route' not in intent:
            since, until = intent.get('merge_window_start'), intent.get('merge_window_end')
            if 'review_route' in intent and (not since or not until):
                raise Halt('External completion lacks a verified merge time window')
            if not f.pinned(current, intent['head'], intent['base'], frozen_at=until, since=since,
                            validate_imports=True):
                raise Halt('External completion task/head/base/config/review evidence changed; inspect saved receipts')
            f.verification_evidence(current, intent['head'], intent['base'], at=until, since=since)
        expected = intent.get('stage', 'Blocked' if r.get('handoff') else 'Review')
        if (expected not in ('Blocked', 'Review') or
                current['status'] not in (expected, 'Done') or
                (current['status'] == 'Done' and not intent.get('done_requested'))):
            raise Halt('External completion stage changed; preserve manual edits')
        if recovery:
            from .blocked_recovery import require_retained_evidence
            require_retained_evidence(f, recovery)
        f.ensure_running()
        return current

    def landed(self, card, intent):
        f = self.f
        snapshot = f.gh.local_merge_snapshot(intent['pr'], card['number'], intent['base_ref'], intent['head_ref'])
        intent.update(observed=snapshot, observed_at=now())
        f.update(card, external_completion=intent)
        try:
            pr, issue, base = snapshot['pullRequest'], snapshot['issue'], snapshot['base']
            repo = {'id': snapshot['id'], 'nameWithOwner': f.c['repo']}
            closing = pr['closingIssuesReferences']
            closing_ids = [v['id'] for v in closing['nodes']]
            valid = (snapshot['nameWithOwner'] == f.c['repo'] and snapshot['id'] and
                     snapshot['isArchived'] is False and pr['id'] and pr['number'] == intent['pr'] and
                     (not intent.get('pr_id') or pr['id'] == intent['pr_id']) and
                     pr['url'] == intent['pr_url'] and pr['headRepository'] == repo and pr['baseRepository'] == repo and
                     pr['headRefName'] == intent['head_ref'].removeprefix('refs/heads/') and
                     pr['baseRefName'] == f.c['base_branch'] and pr['headRefOid'] == intent['head'] and
                     pr['isDraft'] is False and pr['state'] == 'MERGED' and
                     all(issue[k] == intent['card_identity'][k] for k in ('id', 'number', 'url', 'repository')) and
                     issue['state'] == 'CLOSED' and card['state'] == 'CLOSED' and
                     type(closing['totalCount']) is int and closing['totalCount'] == len(closing_ids) and
                     all(isinstance(v, str) and v for v in closing_ids) and
                     len(set(closing_ids)) == len(closing_ids) and card['id'] in closing_ids and
                     base['id'] and base['prefix'] + base['name'] == intent['base_ref'] and
                     base['target']['__typename'] == 'Commit')
            merged, remote = pr['mergeCommit']['oid'], base['target']['oid']
            valid = valid and all(re.fullmatch('[0-9a-f]{40}', sha) for sha in
                                 (merged, remote, intent['head'], intent['base'], base['target']['tree']['oid']))
        except (KeyError, TypeError, AttributeError):
            valid = False
        if not valid:
            raise Halt('External PR/issue completion identity or merge recognition incomplete; inspect retained snapshot')
        if f.fetch() != remote or git(f.root, 'rev-parse', remote + '^{tree}') != base['target']['tree']['oid']:
            raise Halt('External completion live base changed/disagrees; observe again without a merge attempt')
        def ancestor(a, b):
            return run(['git', 'merge-base', '--is-ancestor', a, b], f.root, check=False).returncode == 0
        parents = git(f.root, 'show', '-s', '--format=%P', merged).split()
        if intent.get('kind') == 'protected-squash':
            tree = intent['expected_tree']
            shape = 'squash' if parents == [intent['base']] else None
        else:
            tree = git(f.root, 'rev-parse', intent['head'] + '^{tree}')
            # Human merges have no verified scratch integration tree.
            shape = ('exact-head' if merged == intent['head'] else
                     'squash' if parents == [intent['base']] else
                     'merge' if parents == [intent['base'], intent['head']] else None)
            if not ancestor(intent['base'], intent['head']):
                shape = None
        if (not shape or not ancestor(merged, remote) or
                git(f.root, 'rev-parse', merged + '^{tree}') != tree):
            raise Halt('External merge shape/tree is not the reviewed integration; manual reconciliation required')
        from .factory import checks_pass
        checks = f.gh.local_merge_checks(intent['head'])
        if not isinstance(checks, list) or not checks_pass({'statusCheckRollup': checks}, f.c['allow_no_checks']):
            raise Halt('External completion GitHub checks missing/incomplete/failing; inspect reviewed head checks')
        if intent.get('merge_oid', merged) != merged or intent.get('tree', tree) != tree:
            raise Halt('External completion merge/tree identity changed')
        if 'review_route' in intent:
            try:
                merged_at = dt.datetime.fromisoformat(pr['mergedAt'].replace('Z', '+00:00'))
                open_at = dt.datetime.fromisoformat(intent['open_read_started'])
                handoff_at = dt.datetime.fromisoformat(intent['review_validated_at'])
                observed = dt.datetime.fromisoformat(intent['observed_at'])
                if not open_at <= handoff_at <= observed or merged_at > observed:
                    raise ValueError('merge outside the reviewed handoff window')
                # A GitHub timestamp can be rounded down to a whole second.
                # The last observed open PR narrows the earliest possible merge.
                earliest = max(open_at, merged_at)
                # The next second is excluded. Datetimes represent microseconds.
                latest = min(observed, merged_at + dt.timedelta(microseconds=999999))
                if 'merge_window_end' in intent:
                    latest = min(latest, dt.datetime.fromisoformat(intent['merge_window_end']))
                if earliest > latest or latest < handoff_at:
                    raise ValueError('merge outside the reviewed handoff window')
            except (AttributeError, KeyError, TypeError, ValueError) as exc:
                raise Halt('External completion lacks a verified merge time after the reviewed handoff') from exc
            recorded = merged_at.isoformat()
            if intent.get('merged_at', recorded) != recorded:
                raise Halt('External completion merge timestamp changed')
            intent.update(merged_at=recorded, merge_window_start=earliest.isoformat(),
                          merge_window_end=latest.isoformat())
        intent.update(merge_oid=merged, tree=tree, shape=shape, checks=checks)
        f.update(card, external_completion=intent)

    def reconcile(self, card):
        f, r = self.f, self.f.record(card)
        if 'local_merge' in r:
            raise Halt('Atomic merge journal takes precedence; never replace it with external completion')
        intent = None
        try:
            intent = r['external_completion'] if 'external_completion' in r else self.proof(card)
            current = self.current(card, intent, preflight=True)
            intent.setdefault('stage', current['status'])
            intent.setdefault('phase', 'observing')
            f.update(card, external_completion=intent)
            self.landed(current, intent)
            current = self.current(card, intent)
            if current['state'] != 'CLOSED':
                raise Halt('External completion issue reopened before Done; preserve current stage')
            activity = r.get('activity', {})
            if activity.get('pending') or activity.get('error'):
                raise Halt('External completion activity write unresolved; inspect retained receipt')
            scope, values = f.gh.activity_snapshot(current)
            if ((activity and (scope != activity.get('scope') or current['item_id'] != activity.get('item_id'))) or
                    values != activity.get('values', ['', ''])):
                raise Halt('External completion activity edited; preserve human values before Done')
            if intent.get('recovery_id'):
                current = self.current(card, intent)
                if current['state'] != 'CLOSED':
                    raise Halt('Recovered merge issue reopened before Done')
            if current['status'] != 'Done':
                if intent.get('done_requested'):
                    raise Halt('Ambiguous external Done transition; inspect board, no automatic retry')
                intent.update(phase='confirmed', done_requested=now())
                f.update(card, external_completion=intent)
                f.move(current, 'Done', f"Verified external merge of PR #{intent['pr']} on intended base")
                current = self.current(card, intent)
            else:
                f.sync_done_status(current, guard=f.ensure_running)
            if current['status'] != 'Done' or current['state'] != 'CLOSED':
                raise Halt('External completion Done readback incomplete; inspect retained intent')
            def completed():
                observed = self.current(card, intent)
                if observed['status'] != 'Done' or observed['state'] != 'CLOSED':
                    raise Halt('External Completed activity needs verified Done and closed issue')
            receipt = r.get('activity', {})
            if (intent.get('completed_requested') and receipt.get('state') == 'Completed' and
                    receipt.get('action') == 'done' and not receipt.get('pending') and not receipt.get('error')):
                # A crash after verified activity but before terminal persistence
                # needs only readback, not a second Completed mutation.
                completed()
                scope, values = f.gh.activity_snapshot(current)
                if scope != receipt.get('scope') or values != receipt.get('values'):
                    raise Halt('Completed activity readback changed; preserve human edits')
            else:
                intent.setdefault('completed_requested', now())
                f.update(card, external_completion=intent)
                f.activity.report(current, 'Completed', 'done', guard=f.ensure_running, prewrite=completed,
                                  check_counts=verification_check_counts(intent["verification"]))
            intent.update(phase='done', confirmed_at=now())
            with f.mutex:
                r.pop('external_completion_error', None)
                from .blocked_recovery import finish
                finish(f, current, save=False)
                f.update(card, external_completion=intent, status='Done')
            card.update(status='Done', state='CLOSED')
        except Halt as exc:
            f.update(card, external_completion_error=str(exc))
            f.event(card, 'External completion gap: ' + str(exc))
            # Reuse the existing two-field activity surface, with fixed public
            # wording. Never annotate a changed/held/removed task, or overwrite
            # an uncertain Done/Completed/activity write with a new status.
            activity = r.get('activity', {})
            if (intent and intent.get('kind') != 'protected-squash' and not intent.get('done_requested') and
                    not activity.get('pending') and not activity.get('error')):
                try:
                    current = self.current(card, intent)
                except Halt:
                    pass
                else:
                    f.activity.report(current, 'Waiting', 'external_merge', guard=f.ensure_running,
                                      prewrite=lambda: self.current(card, intent))
            raise
