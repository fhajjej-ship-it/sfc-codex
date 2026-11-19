"""External completion through the supervisor; temporary git and fake adapters only."""
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import test_local_merge as fixtures
from sfc_lib.common import Halt, git
from sfc_lib.factory import Factory


class ExternalMergeTests(unittest.TestCase):
    tearDown = fixtures.PriorityLocalMergeTests.tearDown

    def setUp(self):
        self.setup_case()

    def setup_case(self, mode='local-verification', ordinary=False, policy='human', large=False, allow_no_checks=False,
                   labels=None):
        fixtures.PriorityLocalMergeTests.setUp(self)
        if labels is not None:
            self.gh.items[0]['labels'] = labels.copy()
        self.c['merge_mode'] = mode
        self.gh.items[1]['status'] = 'Blocked'
        self.c['merge_policy'] = policy
        self.c['allow_no_checks'] = allow_no_checks
        if large:
            self.proc.on_lane = lambda lane, cwd: self.gh.prs[101].update(additions=444) if lane == 'review' else None
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        if ordinary:
            self.gh.items[0]['status'] = 'Ready'
            self.gh.move = fixtures.FakeGitHub.move.__get__(self.gh)
            self.factory.run(allow_merge=large)
        else:
            self.factory.run(include_backlog=True, issues=(1,), allow_merge=large)
        self.card = self.gh.items[0]
        self.saved = copy.deepcopy(self.factory.record(self.card))
        self.events = copy.deepcopy(self.proc.events)
        self.gh.edit_snapshot = self.external_snapshot
        self.merge_oid = None

    def external_snapshot(self, snapshot):
        snapshot['pullRequest']['mergeCommit'] = {'oid': self.merge_oid} if self.merge_oid else None

    def land(self, shape='ff'):
        head, base = self.saved['head'], self.saved['base']
        tree = git(self.root, 'rev-parse', head + '^{tree}')
        if shape == 'ff':
            self.merge_oid = head
        else:
            from sfc_lib.common import run
            parents = ['-p', base] + (['-p', head] if shape == 'merge' else [])
            self.merge_oid = run(['git', 'commit-tree', tree, *parents], self.root,
                                 input='External user merge\n').stdout.strip()
            git(self.root, 'push', 'origin', self.merge_oid + ':refs/heads/main')
        if shape == 'ff':
            git(self.remote, 'update-ref', 'refs/heads/main', head)
        self.gh.prs[self.saved['pr']]['state'] = 'MERGED'
        self.card['state'] = 'CLOSED'

    def restart(self, ordinary=False):
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        return self.factory.run() if ordinary else self.factory.run(include_backlog=True, issues=(1,))

    def test_saved_blocked_handoff_completes_without_worker_or_merge(self):
        self.gh.github_status['item-1'] = 'in-progress'
        self.land()
        self.restart()
        self.assertEqual(self.card['status'], 'Done')
        self.assertEqual(self.factory.record(self.card)['external_completion']['phase'], 'done')
        self.assertEqual(self.proc.events, self.events)
        self.assertEqual(self.gh.attempts + self.gh.merges, [])
        self.assertEqual(self.gh.writes.count((1, 'Done')), 1)
        self.restart()
        self.assertEqual(self.gh.writes.count((1, 'Done')), 1)
        self.assertEqual(self.gh.github_status['item-1'], 'done')
        self.assertEqual(self.gh.github_status_writes, ['item-1'])

    def fresh_case(self, **kwargs):
        self.tearDown()
        self.setup_case(**kwargs)

    def assert_safe_gap(self):
        with self.assertRaises(Halt):
            self.restart()
        self.assertNotIn((1, 'Done'), self.gh.writes)
        self.assertEqual(self.proc.events, self.events)
        self.assertEqual(self.gh.attempts + self.gh.merges, [])

    def test_large_auto_policy_handoff_can_complete_without_merge_authority(self):
        self.fresh_case(policy='auto', large=True)
        self.assertEqual(self.c['auto_max_lines'], 400)
        self.assertEqual(self.card['status'], 'Blocked')
        self.assertTrue(self.saved['handoff'])
        self.assertIn('diff exceeds auto_max_lines', str(self.saved['history']))
        self.land('squash')
        self.restart()
        self.assertEqual(self.card['status'], 'Done')
        self.assertEqual(self.proc.events, self.events)
        self.assertEqual(self.gh.attempts + self.gh.merges, [])

    def test_check_policy_is_preserved_for_external_completion(self):
        for index, checks in enumerate((None, [], [{'status': 'COMPLETED', 'conclusion': 'FAILURE'}])):
            with self.subTest(checks=checks):
                if index:
                    self.fresh_case()
                self.land()
                self.gh.ci = checks
                self.assert_safe_gap()
                self.assertIn('checks', self.factory.record(self.card)['external_completion_error'])

    def test_issue_reopens_between_merge_snapshot_and_done(self):
        self.land()
        from sfc_lib.external_merge import ExternalMerge
        original = ExternalMerge.landed
        def reopen(checker, card, intent):
            original(checker, card, intent)
            self.card['state'] = 'OPEN'
        with patch.object(ExternalMerge, 'landed', reopen):
            self.assert_safe_gap()
        self.assertIn('reopened', self.factory.record(self.card)['external_completion_error'])

    def test_restart_after_verified_completed_does_not_duplicate_activity_mutation(self):
        self.land()
        original = Factory.update
        def interrupted(factory, card, **values):
            intent = values.get('external_completion')
            if intent and intent.get('phase') == 'done':
                # Death before terminal persistence, after verified activity.
                # Restore the last durable journal so exception diagnostics
                # cannot accidentally persist the mutated in-memory terminal.
                durable = json.loads(factory.state_path.read_text())['cards']['1']['external_completion']
                factory.record(card)['external_completion'] = durable
                raise Halt('interrupted before terminal persistence')
            return original(factory, card, **values)
        with patch.object(Factory, 'update', interrupted):
            with self.assertRaisesRegex(Halt, 'terminal persistence'):
                self.restart()
        writes = copy.deepcopy((self.gh.writes, self.gh.activity_writes))
        self.assertNotEqual(json.loads(self.factory.state_path.read_text())['cards']['1']['external_completion']['phase'], 'done')
        with patch.object(self.gh, 'activity_snapshot', wraps=self.gh.activity_snapshot) as readback:
            self.restart()
        self.assertGreater(readback.call_count, 0)
        self.assertEqual(self.factory.record(self.card)['external_completion']['phase'], 'done')
        self.assertEqual((self.gh.writes, self.gh.activity_writes), writes)

    def test_human_activity_edit_or_uncertain_done_readback_stops_retries(self):
        for index, case in enumerate(('activity', 'done-readback')):
            with self.subTest(case=case):
                if index:
                    self.fresh_case()
                self.land()
                if case == 'activity':
                    self.gh.activities[self.card['item_id']] = ['Human note', 'Please wait']
                else:
                    original = self.gh.move
                    def ignored(card, status):
                        if status == 'Done':
                            self.gh.writes.append((card['number'], status))
                            return
                        original(card, status)
                    self.gh.move = ignored
                with self.assertRaises(Halt):
                    self.restart()
                writes = copy.deepcopy((self.gh.writes, self.gh.activity_writes))
                with self.assertRaises(Halt):
                    self.restart()
                self.assertEqual((self.gh.writes, self.gh.activity_writes), writes)
                self.assertNotEqual(self.factory.record(self.card)['external_completion']['phase'], 'done')
                if case == 'activity':
                    self.assertNotIn((1, 'Done'), self.gh.writes)

    def test_unresolved_external_journal_cannot_escape_scope_or_hold_on_restart(self):
        from test_watch import Clock
        for index, case in enumerate(('scope', 'held', 'removed', 'task', 'config')):
            with self.subTest(case=case):
                if index:
                    self.fresh_case()
                self.land()
                self.card['state'] = 'OPEN'
                with self.assertRaises(Halt):
                    self.restart()
                self.card['state'] = 'CLOSED'
                writes = copy.deepcopy((self.gh.writes, self.gh.activity_writes))
                if case == 'task':
                    self.card['body'] += '\nChanged task'
                if case == 'config':
                    self.c['auto_max_lines'] += 1
                if case == 'removed':
                    self.gh.items.remove(self.card)
                if case == 'held':
                    self.card['labels'].append('sfc-hold')
                self.factory = Factory(self.root, self.c, self.gh, self.proc)
                with self.assertRaises(Halt):
                    if case == 'scope':
                        self.factory.run(include_backlog=True, issues=(15,))
                    elif case == 'held':
                        clock = Clock()
                        self.factory.watch(start=True, clock=clock, wait=clock.wait, overrides={'max_polls': 1})
                    else:
                        self.restart()
                self.assertEqual((self.gh.writes, self.gh.activity_writes), writes)

    def test_later_planning_unblocks_a_matching_closed_dependency(self):
        self.land()
        self.restart()
        from test_pickup import ticket
        self.gh.items.append(ticket(2, status='Backlog', body='## Acceptance criteria\n- A result\n\n## Blocked by\n- #1'))
        self.factory.include_backlog = True
        self.factory.issue_scope = (1, 2)
        selected, waiting = self.factory.plan(self.factory.queue_cards())
        self.assertEqual([c['number'] for c in selected], [2])
        self.assertFalse(any(w['issue'] == 2 for w in waiting))
        self.assertEqual(self.proc.events, self.events)
        self.assertEqual(self.gh.attempts + self.gh.merges, [])

    def test_supported_shapes_in_both_modes_and_ordinary_runs(self):
        for shape, mode, ordinary in (
                (s, m, o) for s in ('ff', 'merge', 'squash')
                for m in ('local-verification', 'github-protected') for o in (False, True)):
            with self.subTest(shape=shape, mode=mode, ordinary=ordinary):
                self.fresh_case(mode=mode, ordinary=ordinary)
                self.land(shape)
                self.restart(ordinary=ordinary)
                self.assertEqual(self.card['status'], 'Done')
                self.assertEqual(self.proc.events, self.events)
                self.assertEqual(self.gh.attempts + self.gh.merges, [])

    def test_ordinary_handoff_has_its_own_saved_identity_without_scoped_claim(self):
        self.fresh_case(ordinary=True)
        # New Ready-only dispatches have a verified claim. Simulate the retained
        # ordinary handoff created before that scheduler, keeping its independent
        # saved completion identity and all original review/check evidence.
        record = self.factory.record(self.card)
        self.assertEqual(record['claim']['state'], 'verified')
        record.pop('claim')
        self.factory.save()
        self.saved = copy.deepcopy(record)
        self.assertNotIn('claim', self.factory.record(self.card))
        self.land()
        self.restart(ordinary=True)
        self.assertEqual(self.card['status'], 'Done')
        self.assertEqual(self.proc.events, self.events)

    def test_new_ready_handoff_retains_verified_claim_and_completes_without_worker(self):
        self.fresh_case(ordinary=True)
        self.assertEqual(self.factory.record(self.card)['claim']['state'], 'verified')
        self.land()
        self.restart(ordinary=True)
        self.assertEqual(self.card['status'], 'Done')
        self.assertEqual(self.proc.events, self.events)
        self.assertEqual(self.gh.attempts + self.gh.merges, [])

    def test_ordinary_handoff_with_unordered_labels_completes_once(self):
        for mode in ('local-verification', 'github-protected'):
            with self.subTest(mode=mode):
                labels = ['human-only', 'bug']
                self.fresh_case(ordinary=True, mode=mode, labels=labels)
                self.assertEqual(self.card['labels'], labels)
                self.assertTrue(self.factory.pinned(self.card, self.saved['head'], self.saved['base']))
                self.land()
                self.restart(ordinary=True)
                self.assertEqual(self.card['status'], 'Done')
                self.assertEqual(self.factory.record(self.card)['external_completion']['phase'], 'done')
                writes = copy.deepcopy((self.gh.writes, self.gh.activity_writes))
                self.restart(ordinary=True)
                self.assertEqual((self.gh.writes, self.gh.activity_writes), writes)
                self.assertEqual(self.gh.writes.count((1, 'Done')), 1)
                self.assertEqual(self.proc.events, self.events)
                self.assertEqual(self.gh.attempts + self.gh.merges, [])

    def test_ordinary_label_reordering_preserves_pins_but_membership_changes_do_not(self):
        for change, labels in (('reorder', ['bug', 'human-only']),
                               ('add', ['bug', 'human-only', 'maintenance']),
                               ('remove', ['bug'])):
            with self.subTest(change=change):
                self.fresh_case(ordinary=True, labels=['human-only', 'bug'])
                self.land()
                self.card['labels'] = labels
                if change == 'reorder':
                    self.assertTrue(self.factory.pinned(self.card, self.saved['head'], self.saved['base']))
                    self.restart(ordinary=True)
                    self.assertEqual(self.card['status'], 'Done')
                else:
                    self.assertFalse(self.factory.pinned(self.card, self.saved['head'], self.saved['base']))
                    with self.assertRaisesRegex(Halt, 'task/head/base/config/review evidence changed'):
                        self.restart(ordinary=True)
                    self.assertNotIn((1, 'Done'), self.gh.writes)
                    self.assertEqual(self.card['status'], 'Blocked')
                self.assertEqual(self.proc.events, self.events)
                self.assertEqual(self.gh.attempts + self.gh.merges, [])

    def test_legacy_saved_verified_handoff_uses_claim_and_pinned_evidence(self):
        self.factory.record(self.card).pop('reviewed_completion')
        self.factory.save()
        self.land()
        self.restart()
        self.assertEqual(self.card['status'], 'Done')
        self.assertNotIn('local_merge', self.factory.record(self.card))

    def test_later_base_advance_and_deleted_head_are_supported(self):
        self.land('merge')
        from sfc_lib.common import run
        advanced = run(['git', 'commit-tree', git(self.root, 'rev-parse', self.saved['head'] + '^{tree}'),
                        '-p', self.merge_oid], self.root, input='Later unrelated commit\n').stdout.strip()
        git(self.root, 'push', 'origin', advanced + ':main')
        original = self.gh.local_merge_snapshot
        def deleted(*args):
            snapshot = original(*args)
            snapshot['head'] = None  # PR identity retains reviewed head after branch deletion.
            return snapshot
        with patch.object(self.gh, 'local_merge_snapshot', side_effect=deleted):
            self.restart()
        self.assertEqual(self.card['status'], 'Done')

    def test_watch_observes_handoff_without_merge_permission_or_work_wave(self):
        from test_watch import Clock
        self.land()
        clock = Clock()
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        result = self.factory.watch(start=True, allow_merge=False, clock=clock, wait=clock.wait,
                                    overrides={'max_polls': 1})
        self.assertEqual(self.card['status'], 'Done')
        self.assertEqual(result['session']['cycles'], 0)
        self.assertEqual(result['session']['claims'], 0)
        self.assertEqual(self.proc.events, self.events)
        self.assertEqual(self.gh.attempts + self.gh.merges, [])

    def test_unrecognized_open_pr_waits_and_open_issue_is_visible_gap(self):
        self.land()
        self.gh.prs[101]['state'] = 'OPEN'
        self.restart()
        self.assertEqual(self.card['status'], 'Blocked')
        self.gh.prs[101]['state'] = 'MERGED'
        self.card['state'] = 'OPEN'
        self.assert_safe_gap()
        self.assertEqual(self.factory.record(self.card)['activity']['action'], 'external_merge')
        from sfc_lib.reasons import reason, render
        explanation = self.gh.activities[self.card['item_id']][1]
        self.assertEqual(explanation, render(reason('external_merge')))
        self.assertIn('reviewed head and issue-closure evidence', explanation)
        self.assertIn('No merge retry or issue-close write', explanation)
        self.assertIn('recognition incomplete', self.factory.state['error'])
        self.card['state'] = 'CLOSED'
        self.restart()
        self.assertEqual(self.card['status'], 'Done')

    def test_snapshot_identity_head_base_and_recognition_mismatches(self):
        changes = {
            'pr-number': lambda s: s['pullRequest'].update(number=999),
            'pr-node': lambda s: s['pullRequest'].update(id='other-pr'),
            'pr-url': lambda s: s['pullRequest'].update(url='other'),
            'repo': lambda s: s.update(nameWithOwner='other/repo'),
            'head-repo': lambda s: s['pullRequest']['headRepository'].update(id='other-repo'),
            'base-name': lambda s: s['pullRequest'].update(baseRefName='other'),
            'head-name': lambda s: s['pullRequest'].update(headRefName='other'),
            'head-sha': lambda s: s['pullRequest'].update(headRefOid='a' * 40),
            'base-ref': lambda s: s['base'].update(name='other'),
            'issue-node': lambda s: s['issue'].update(id='other'),
            'issue-closed': lambda s: s['issue'].update(state='OPEN'),
            'issue-repo': lambda s: s['issue']['repository'].update(nameWithOwner='other/repo'),
            'closing-other': lambda s: s['pullRequest'].update(closingIssuesReferences={'totalCount': 1, 'nodes': [{'id': 'other'}]}),
            'closing-truncated': lambda s: s['pullRequest']['closingIssuesReferences'].update(totalCount=101),
            'merge-missing': lambda s: s['pullRequest'].update(mergeCommit=None),
            'base-tree': lambda s: s['base']['target']['tree'].update(oid='a' * 40),
            'pr-open': lambda s: s['pullRequest'].update(state='OPEN'),
            'draft': lambda s: s['pullRequest'].update(isDraft=True),
            'partial': lambda s: s.pop('base'),
        }
        for index, (name, change) in enumerate(changes.items()):
            with self.subTest(name=name):
                if index:
                    self.fresh_case()
                # Real gh pr includes its node ID; match the atomic fake's ID.
                self.factory.record(self.card)['reviewed_completion']['pr_id'] = 'pr-node-101'
                self.factory.save()
                self.land()
                def edited(s):
                    self.external_snapshot(s)
                    change(s)
                self.gh.edit_snapshot = edited
                self.assert_safe_gap()
                self.assertTrue(self.factory.record(self.card)['external_completion_error'])

    def test_card_claim_task_scope_and_receipt_drift_never_write_done(self):
        changes = {
            'title': lambda: self.card.update(title='Changed ticket'),
            'body': lambda: self.card.update(body='Different task'),
            'item': lambda: self.card.update(item_id='other-item'),
            'id': lambda: self.card.update(id='other-issue'),
            'repo': lambda: self.card.update(repository={'nameWithOwner': 'other/repo'}),
            'stage': lambda: self.card.update(status='Ready'),
            'human-done': lambda: self.card.update(status='Done'),
            'claim': lambda: self.factory.record(self.card)['claim'].update(state='incomplete'),
            'claim-id': lambda: self.factory.record(self.card)['claim']['before'].update(id='other'),
            'no-claim': lambda: self.factory.record(self.card).pop('claim'),
            'receipts': lambda: self.factory.record(self.card)['receipts'].pop('review'),
            'log': lambda: Path(self.saved['verification']['checks'][0]['log']).unlink(),
            'base': lambda: self.factory.record(self.card).update(base='a' * 40),
            'head': lambda: self.factory.record(self.card).update(head='a' * 40),
            'pr': lambda: (self.gh.prs.update({999: {**self.gh.prs[101], 'number': 999}}),
                           self.factory.record(self.card).update(pr=999)),
            'handoff': lambda: self.factory.record(self.card).update(handoff='a' * 40),
            'routing': lambda: self.gh.fields[0]['options'][0].update(id='different'),
        }
        for index, (name, change) in enumerate(changes.items()):
            with self.subTest(name=name):
                if index:
                    self.fresh_case()
                self.land()
                change()
                self.factory.save()
                # Human Done, unresolved claims, or filtered foreign repositories
                # may be skipped before reconciliation; all must remain untouched.
                try:
                    self.restart()
                except Halt:
                    pass
                self.assertNotIn((1, 'Done'), self.gh.writes)
                self.assertEqual(self.proc.events, self.events)
                self.assertEqual(self.gh.attempts + self.gh.merges, [])

    def test_out_of_scope_held_excluded_and_removed_cards_stay_untouched(self):
        from test_watch import Clock
        for index, case in enumerate(('scope', 'held', 'excluded', 'removed')):
            with self.subTest(case=case):
                if index:
                    self.fresh_case()
                self.land()
                writes = copy.deepcopy((self.gh.writes, self.gh.activity_writes))
                self.factory = Factory(self.root, self.c, self.gh, self.proc)
                if case == 'scope':
                    self.factory.run(include_backlog=True, issues=(15,))
                elif case == 'removed':
                    self.gh.items.remove(self.card)
                    self.restart()
                else:
                    clock = Clock()
                    if case == 'held':
                        self.card['labels'].append('sfc-hold')
                    self.factory.watch(start=True, clock=clock, wait=clock.wait,
                                       overrides={'max_polls': 1, 'excluded_issues': [1] if case == 'excluded' else []})
                self.assertEqual((self.gh.writes, self.gh.activity_writes), writes)
                self.assertEqual(self.proc.events, self.events)

    def test_uncertain_done_write_is_observed_once_never_repeated(self):
        for index, applied in enumerate((False, True)):
            with self.subTest(applied=applied):
                if index:
                    self.fresh_case()
                self.land()
                move = self.gh.move
                calls = []
                def lost(card, status):
                    if status == 'Done':
                        calls.append(status)
                        saved = json.loads(self.factory.state_path.read_text())['cards']['1']['external_completion']
                        self.assertTrue(saved['done_requested'])
                        if applied:
                            move(card, status)
                        raise Halt('Done response lost')
                    return move(card, status)
                with patch.object(self.gh, 'move', side_effect=lost):
                    with self.assertRaisesRegex(Halt, 'response lost'):
                        self.restart()
                    if applied:
                        self.restart()
                        self.assertEqual(self.card['status'], 'Done')
                    else:
                        with self.assertRaisesRegex(Halt, 'Ambiguous external Done'):
                            self.restart()
                self.assertEqual(calls, ['Done'])
                self.assertEqual(self.proc.events, self.events)

    def test_completed_partial_response_latches_and_never_retries(self):
        self.land()
        original = self.gh.write_activity
        calls = []
        def lost(card, scope, values):
            if 'Completed' in values[0]:
                calls.append(values)
                original(card, scope, [values[0], 'partial'])
                raise Halt('Partial Completed response')
            original(card, scope, values)
        with patch.object(self.gh, 'write_activity', side_effect=lost):
            with self.assertRaises(Halt):
                self.restart()
            with self.assertRaises(Halt):
                self.restart()
        self.assertEqual(len(calls), 1)
        self.assertNotEqual(self.factory.record(self.card)['external_completion']['phase'], 'done')
        self.assertEqual(self.gh.writes.count((1, 'Done')), 1)

    def test_wrong_tree_changed_base_rebase_or_unreachable_merge_rejected(self):
        from sfc_lib.common import run
        for index, case in enumerate(('tree', 'base', 'rebase', 'unreachable')):
            with self.subTest(case=case):
                if index:
                    self.fresh_case()
                self.land()
                head, base = self.saved['head'], self.saved['base']
                tree = git(self.root, 'rev-parse', (base if case == 'tree' else head) + '^{tree}')
                parent = base
                if case in ('base', 'rebase'):
                    parent = run(['git', 'commit-tree', tree, '-p', base], self.root, input='Changed base\n').stdout.strip()
                self.merge_oid = run(['git', 'commit-tree', tree, '-p', parent], self.root, input='Unreviewed merge\n').stdout.strip()
                # Expose even an unreachable object to the local Git fixture.
                if case != 'unreachable':
                    git(self.remote, 'update-ref', 'refs/heads/main', base)
                    git(self.root, 'push', 'origin', self.merge_oid + ':main')
                self.assert_safe_gap()

    def test_existing_atomic_journal_retains_its_stronger_authority(self):
        self.land()
        record = self.factory.record(self.card)
        record['local_merge'] = {'phase': 'unresolved', 'id': 'original-atomic-journal'}
        self.factory.save()
        with patch('sfc_lib.local_merge.LocalMerge.reconcile', side_effect=Halt('atomic recovery only')) as recover:
            with self.assertRaisesRegex(Halt, 'atomic recovery only'):
                self.restart()
        recover.assert_called_once()
        self.assertEqual(self.factory.record(self.card)['local_merge'], record['local_merge'])
        self.assertNotIn('external_completion', self.factory.record(self.card))
        self.assertNotIn((1, 'Done'), self.gh.writes)

    def test_protected_human_merge_and_explicit_absent_check_policy(self):
        self.fresh_case(allow_no_checks=True)
        self.gh.protected, self.gh.rules = {'id': 'human-managed-rule'}, 1
        self.gh.ci = []
        self.land('merge')
        self.restart()
        self.assertEqual(self.card['status'], 'Done')
        self.assertEqual(self.gh.attempts + self.gh.merges, [])

    def test_incomplete_saved_receipts_and_empty_atomic_journal_fail_closed(self):
        for index, case in enumerate(('partial-review', 'empty-review', 'legacy-verification', 'empty-atomic')):
            with self.subTest(case=case):
                if index:
                    self.fresh_case()
                self.land()
                record = self.factory.record(self.card)
                if case == 'partial-review':
                    record['reviewed_completion'].pop('verification')
                elif case == 'empty-review':
                    record['reviewed_completion'] = {}
                elif case == 'legacy-verification':
                    record.pop('reviewed_completion')
                    record['verification'] = None
                else:
                    record['local_merge'] = {}
                self.factory.save()
                self.assert_safe_gap()

    def test_unverified_creation_is_not_annotated_or_recognized(self):
        self.land()
        writes = copy.deepcopy((self.gh.writes, self.gh.activity_writes))
        with patch.object(Factory, 'creation_problem', return_value='Creation evidence unverified'), \
                patch.object(self.gh, 'pr', side_effect=AssertionError('No PR reads for unverified creation')):
            self.restart()
        self.assertEqual((self.gh.writes, self.gh.activity_writes), writes)
        self.assertEqual(self.proc.events, self.events)

    def test_duplicate_closing_issue_identity_is_a_gap(self):
        self.land()
        def duplicate(snapshot):
            self.external_snapshot(snapshot)
            snapshot['pullRequest']['closingIssuesReferences'] = {
                'totalCount': 2, 'nodes': [{'id': self.card['id']}, {'id': self.card['id']}]}
        self.gh.edit_snapshot = duplicate
        self.assert_safe_gap()
