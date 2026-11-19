"""Read-cost regressions with real planner/adapter logic and fake remote IO."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_pickup import PickupGitHub, ticket
import test_activity as activity_fixtures
from sfc_lib import pickup
from sfc_lib.activity import Activity, ActivityError
from sfc_lib.common import Halt, git
from sfc_lib.factory import Factory
from sfc_lib.watch import Watch


class CompletedObservationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='sfc-observation-')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        git(self.root, 'init')
        self.gh = PickupGitHub(self.root, [ticket(1), ticket(2, state='CLOSED', status='Done')])
        self.f = Factory(self.root, self.gh.c, self.gh)
        self.f.watcher = Watch(self.f)
        self.f.include_backlog = True
        self.f.state['cards']['2'] = {'status': 'Done', 'activity': {'state': 'Completed', 'pending': False}}
        self.gh.metadata()

    def test_full_membership_but_no_terminal_detail_reads_at_scale(self):
        self.gh.items = [ticket(1)] + [ticket(n, state='CLOSED', status='Done') for n in range(2, 251)]
        self.f.state['cards'] = {str(n): copy.deepcopy(self.f.state['cards']['2']) for n in range(2, 251)}
        before = copy.deepcopy(self.f.state['cards'])
        self.gh.reads.clear()
        cards = self.f.queue_cards()
        self.assertEqual([c['number'] for c in cards], [1])
        self.assertEqual(len(self.gh.reads), 6)  # Three membership pages, two scope reads, one detail.
        self.assertEqual(self.f.state['cards'], before)
        self.assertEqual(self.gh.writes + self.gh.activity_writes, [])

    def test_reopen_is_seen_without_erasing_completed_spending_or_redispatch(self):
        self.f.state['cards']['2']['attempts'] = 2
        self.assertEqual(len(self.f.queue_cards()), 1)
        self.gh.items[1].update(state='OPEN', status='Ready', priority='P0')
        cards = self.f.queue_cards()
        self.assertEqual(len(cards), 2)
        selected, waiting = self.f.plan(cards)
        self.assertEqual([c['number'] for c in selected], [1])
        self.assertIn('explicit recovery', str(waiting))
        self.assertEqual(self.f.state['cards']['2']['attempts'], 2)
        self.assertEqual(self.gh.writes + self.gh.activity_writes, [])

    def test_uncertain_recovery_release_and_occupied_records_are_never_omitted(self):
        variations = [
            {'status': 'Review'}, {'workflow_kind': 'release'},
            {'activity': {'state': 'Running'}}, {'activity': {'pending': True}},
            {'activity': {'error': 'readback lost'}}, {'claim': {'state': 'intent'}},
            {'local_merge': {'phase': 'intent'}}, {'external_completion': {'phase': 'observed'}},
            {'ticket_failure': {'phase': 'intent'}}, {'handoff': {'reason': 'human'}},
            {'dispatch': {'state': 'launched'}}, {'ticket_recoveries': [{'status': 'available'}]},
            {'corrective_builds': [{'status': 'available'}]},
        ]
        original = copy.deepcopy(self.f.state['cards']['2'])
        for change in variations:
            with self.subTest(change=change):
                self.f.state['cards']['2'] = {**copy.deepcopy(original), **change}
                self.assertEqual(len(self.f.queue_cards()), 2)
        self.f.state['cards']['2'] = original
        self.f.occupied[2] = self.gh.items[1]
        self.assertEqual(len(self.f.queue_cards()), 2)

    def test_full_read_for_unknown_closed_and_explicit_selection(self):
        self.f.state['cards'].clear()
        self.assertEqual(len(self.f.queue_cards()), 2)
        self.assertEqual(len(pickup.cards(self.gh, (2,), completed={2})), 1)

    def test_omitted_members_still_validate_identity_state_and_pagination(self):
        for mode in ('state', 'duplicate', 'cursor'):
            with self.subTest(mode=mode):
                original = self.gh.graphql
                def corrupt(q, v):
                    data = original(q, v)
                    if 'items(first:100' in q:
                        page = data['node']['items']
                        if mode == 'state': page['nodes'][-1]['content']['state'] = None
                        if mode == 'duplicate': page['nodes'].append(copy.deepcopy(page['nodes'][-1]))
                        if mode == 'cursor': page['pageInfo'] = {'hasNextPage': True, 'endCursor': None}
                    return data
                with patch.object(self.gh, 'graphql', side_effect=corrupt), self.assertRaises(Halt):
                    self.f.queue_cards()
        self.assertEqual(self.gh.writes + self.gh.activity_writes, [])

    def test_omission_hint_does_not_survive_restart_or_other_repository(self):
        self.assertEqual(len(self.f.queue_cards()), 1)
        # No saved completion in a fresh factory: do not adopt another object's hint.
        fresh = Factory(self.root, self.gh.c, self.gh)
        fresh.watcher = Watch(fresh)
        self.assertEqual(len(fresh.queue_cards()), 2)
        self.gh.items[1]['repository'] = {'nameWithOwner': 'other/app'}
        self.assertEqual(len(self.f.queue_cards()), 1)


class ActivityObservationTests(unittest.TestCase):
    def adapter(self):
        fixtures = activity_fixtures.GitHubActivityContracts()
        gh = fixtures.adapter()
        snapshot = fixtures.snapshot()['node']
        calls, writes = [], []
        def query(q, v):
            calls.append((q, copy.deepcopy(v)))
            if q.lstrip().startswith('mutation'):
                writes.append(copy.deepcopy(v))
                values = snapshot['fieldValues']['nodes']
                for key in ('state', 'action'):
                    target = next(x for x in values if x['field']['id'] == v[key]['fieldId'])
                    target['text'] = v[key]['value']['text']
                return {key: {'projectV2Item': {'id': 'item-1'}} for key in ('state', 'action')}
            if 'fields(first:100' in q:
                return {'node': {'fields': copy.deepcopy(snapshot['fields'])}}
            if 'fieldValues(first:100)' in q:
                return {'node': copy.deepcopy(snapshot)}
            raise AssertionError('Unnecessary dashboard/view query: ' + q)
        gh.graphql = query
        return gh, snapshot, calls, writes

    def test_every_boundary_refreshes_fields_without_reading_saved_views(self):
        gh, snapshot, calls, writes = self.adapter()
        card = ticket(1, id='issue-id')
        record = {'activity': {'scope': {'project': 'project-id', 'fields': ['agent-field', 'activity-field']},
                               'item_id': 'item-1', 'values': ['current state', 'current action']}}
        activity = Activity(gh, lambda _: record, lambda: None)
        activity.report(card, 'Running', 'build')
        self.assertEqual(len(calls), 5)  # inventory/item, mutation, inventory/item
        self.assertEqual(len(writes), 1)
        self.assertFalse(record['activity']['pending'])
        self.assertFalse(any('views(' in q or 'visibleFields(' in q for q, _ in calls))
        snapshot['fields']['nodes'][0]['id'] = 'replacement-field'
        with self.assertRaises(ActivityError):
            activity.report(card, 'Waiting', 'assess')
        self.assertEqual(len(writes), 1)
        with self.assertRaises(ActivityError):
            Activity(gh, lambda _: record, lambda: None).report(card, 'Waiting', 'assess')
        self.assertEqual(len(writes), 1)  # restart preserves the latched failure

    def test_field_pagination_and_malformed_inventory_remain_fail_closed(self):
        for mode in ('pages', 'duplicate', 'wrong_type', 'cursor'):
            with self.subTest(mode=mode):
                gh, snapshot, calls, writes = self.adapter()
                original = gh.graphql
                fields = snapshot['fields']['nodes']
                def pages(q, v):
                    if 'fields(first:100' not in q: return original(q, v)
                    calls.append((q, copy.deepcopy(v)))
                    if mode == 'wrong_type':
                        fields[0]['dataType'] = 'NUMBER'
                    nodes = fields[:1] if not v['after'] else fields[1:]
                    if mode == 'duplicate' and v['after']: nodes = fields[:1]
                    return {'node': {'fields': {'nodes': copy.deepcopy(nodes), 'pageInfo': {
                        'hasNextPage': not v['after'], 'endCursor': None if mode == 'cursor' else 'next'}}}}
                gh.graphql = pages
                if mode == 'pages':
                    self.assertEqual(gh.activity_snapshot(ticket(1, id='issue-id'))[1], ['current state', 'current action'])
                    self.assertEqual(len(calls), 3)
                else:
                    with self.assertRaises(Halt): gh.activity_snapshot(ticket(1, id='issue-id'))
                self.assertEqual(writes, [])

    def test_lost_mutation_response_never_retries_on_resume(self):
        gh, snapshot, calls, writes = self.adapter()
        for value in snapshot['fieldValues']['nodes']: value['text'] = ''
        record = {}
        original = gh.graphql
        def lost(q, v):
            data = original(q, v)
            if q.lstrip().startswith('mutation'): raise Halt('response lost')
            return data
        gh.graphql = lost
        for _ in range(2):
            with self.assertRaises(ActivityError):
                Activity(gh, lambda _: record, lambda: None).report(ticket(1, id='issue-id'), 'Running', 'build')
        self.assertEqual(len(writes), 1)
        self.assertTrue(record['activity']['pending'])
