"""Priority scheduler/claim contracts using temporary repos and a fake GraphQL transport."""
import copy
from contextlib import nullcontext, redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import sfc
from sfc_lib import pickup
from sfc_lib.common import Halt, STATUSES, git, read_json, write_json
from sfc_lib.factory import Factory
from sfc_lib.github import GitHub
from sfc_lib.ticket_validation import lint_card
from test_factory import BASE_CONFIG, FakeGitHub, issue
import test_factory as factory_fixtures
from test_priority import priority_field


def ticket(number=1, **kwargs):
    return issue(number, **{'id': f'issue-{number}', 'repository': {'nameWithOwner': 'example/app'},
                            'createdAt': '2026-01-01T00:00:00Z', 'priority': 'P2', **kwargs})


class PickupGitHub(FakeGitHub):
    metadata = GitHub.metadata
    project_id = GitHub.project_id

    def __init__(self, root, cards):
        super().__init__(root, cards)
        self.c = copy.deepcopy(BASE_CONFIG)
        self.project, self._meta = self.c['project'], None
        self.fields = [{'id': 'stage', 'name': 'SFC Status', 'dataType': 'SINGLE_SELECT',
                        'options': [{'id': 'stage-' + s, 'name': s} for s in STATUSES]}, priority_field()]
        self.page_size = 100
        self.reads = []
        self.extra_issues = {}
        self.extra_items = []
        self.values_override = None
        self.fail_claim = None
        self.on_move = None
        self.on_read = None

    def call(self, *args, **kwargs):
        if args[:2] == ('project', 'view'):
            return {'id': 'p'}
        return super().call(*args, **kwargs)

    def issue_data(self, c):
        return {k: copy.deepcopy(c.get(k)) for k in
                ('id', 'number', 'title', 'body', 'url', 'state', 'createdAt', 'repository')}

    def page(self, nodes, after):
        start = int(after or 0)
        end = start + self.page_size
        return {'nodes': copy.deepcopy(nodes[start:end]),
                'pageInfo': {'hasNextPage': end < len(nodes), 'endCursor': str(end)}}

    def graphql(self, query, variables):
        assert not query.lstrip().startswith('mutation'), 'Unexpected GraphQL mutation'
        self.reads.append((query, copy.deepcopy(variables)))
        if self.on_read:
            self.on_read(query, variables)
        if 'fields(first:100,after:$after)' in query:
            return {'node': {'fields': self.page(self.fields, variables['after'])}}
        result = {}
        if 'fieldValues(first:100,after:$after)' in query:
            c = next(c for c in self.items if c['item_id'] == variables['item'])
            values = [
                {'field': {'id': 'stage'}, 'optionId': 'stage-' + c['status']},
                {'field': {'id': 'priority-field'}, 'optionId': 'priority-' + str(c.get('priority'))}]
            if self.values_override:
                values = self.values_override(c, values)
            result['node'] = {'id': c['item_id'], 'project': {'id': 'p'}, 'content': self.issue_data(c),
                              'fieldValues': self.page(values, variables['after'])}
        if 'items(first:100,after:$after)' in query:
            items = [{'id': c['item_id'], 'content': {'__typename': 'Issue', 'id': c['id'], 'number': c['number'], 'state': c['state'],
                       'repository': c['repository']}} for c in self.items] + self.extra_items
            return {'node': {'items': self.page(items, variables['after'])}}
        if 'issue(number:$number)' in query:
            n = variables['number']
            c = next((c for c in self.items if c['number'] == n), self.extra_issues.get(n))
            if c is None:
                result['repository'] = {'nameWithOwner': 'example/app', 'issue': None}
            else:
                result['repository'] = {'nameWithOwner': 'example/app', 'issue': {
                    **self.issue_data(c), 'labels': self.page([{'id': 'label-' + name, 'name': name}
                                                              for name in c['labels']], variables['after'])}}
        if result:
            return result
        raise AssertionError(query)

    def move(self, card, status):
        if card['status'] in ('Backlog', 'Ready') and status in ('Building', 'QA'):
            saved = read_json(self.root / '.sfc/state.json')['cards'][str(card['number'])]['claim']
            assert saved['state'] == 'intent' and saved['target'] == status
            if self.fail_claim == 'before':
                self.writes.append((card['number'], status))
                raise Halt('Request failed before response; outcome unknown')
            if self.fail_claim == 'drop':
                self.writes.append((card['number'], status))
                card['status'] = status  # Adapter response alone is insufficient.
                return
        super().move(card, status)
        if self.on_move:
            self.on_move(card, status)
        if self.fail_claim == 'after' and status in ('Building', 'QA'):
            raise Halt('Response lost after server write')


class PickupTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='sfc-pickup-')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        git(self.root, 'init')
        self.gh = PickupGitHub(self.root, [ticket(status='Backlog')])
        self.f = Factory(self.root, copy.deepcopy(BASE_CONFIG), self.gh)

    def preview(self, issues=(1,)):
        return self.f.run(dry_run=True, include_backlog=True, issues=issues)

    def test_default_ready_uses_priority_backlog_ignored(self):
        self.gh.items = [ticket(5), ticket(2, status='Backlog'), ticket(4, priority='P0'), ticket(1)]
        selected, _ = self.f.plan(self.gh.cards())
        self.assertEqual([c['number'] for c in selected], [4, 1])
        self.assertFalse(self.f.run(dry_run=True)['options']['include_backlog'])
        self.assertEqual(self.gh.writes, [])

    def test_priority_order_age_offsets_stable_ties_and_capacity(self):
        self.f.c['max_workers'] = 20
        self.gh.items = [ticket(8, priority='P3'), ticket(7, priority='P2'),
                         ticket(5, priority='P1', createdAt='2026-01-02T00:00:00Z'),
                         ticket(3, priority='P1'), ticket(2, priority='P1'),
                         ticket(4, priority='P1', createdAt='2026-01-01T00:30:00+01:00'),
                         ticket(6, priority='P0', createdAt='2026-11-08T00:00:00Z')]
        result = self.preview(range(1, 9))
        self.assertEqual(result['selected'], [6, 4, 2, 3, 5, 7, 8])
        self.assertEqual(result['order'][0]['createdAt'], '2026-11-08T00:00:00Z')
        self.f.c['max_workers'] = 2
        result = self.preview(range(1, 9))
        self.assertEqual(result['selected'], [6, 4])
        self.assertTrue(any('capacity' in str(w) for w in result['waiting']))

    def test_saved_work_competes_by_validated_priority(self):
        self.gh.items = [ticket(5, priority='P0'), ticket(2, status='QA', priority=None), ticket(3, status='Building')]
        for n in (2, 3):
            self.f.state['cards'][str(n)] = {'worktree': 'saved'}
        result = self.preview((2, 3, 5))
        self.assertEqual(result['selected'], [5, 3])
        self.assertIn('Missing/unmapped Priority', str(result['waiting']))

    def test_resumed_stages_wait_on_invalid_or_unresolved_prerequisites(self):
        base = issue()['body']
        cases = [('open', '- #9', '#9'), ('missing', '- #999', '#999'),
                 ('self', '- #1', 'itself'), ('malformed', '- other/app#9', 'Malformed'),
                 ('cycle', '- #3', '#3'), ('foreign', '- #8', 'identity')]
        for stage in ('Building', 'QA', 'Review'):
            for kind, dependency, reason in cases:
                with self.subTest(stage=stage, kind=kind):
                    self.gh.items = [ticket(1, status=stage, body=base + '## Blocked by\n' + dependency),
                                     ticket(2), ticket(3, body=base + '## Blocked by\n- #1')]
                    self.gh.extra_issues = {9: ticket(9), 8: ticket(8, state='CLOSED',
                                               repository={'nameWithOwner': 'other/app'})}
                    self.f.state['cards']['1'] = {'worktree': 'saved'}
                    result = self.preview((1, 2))
                    self.assertEqual(result['selected'], [2])
                    wait = next(w for w in result['waiting'] if w['issue'] == 1)
                    self.assertIn(reason, str(wait['reasons']))
        self.assertEqual(self.gh.writes + self.gh.activity_writes, [])

    def test_invalid_inputs_cycles_and_unreadable_dependencies_are_explained(self):
        base = issue()['body']
        self.gh.items = [ticket(1, status='Backlog', body='No criteria'), ticket(2, priority=None),
                         ticket(3, status='Unknown'), ticket(4, body=base + '## Blocked by\n- #4'),
                         ticket(5, body=base + '## Blocked by\n- other/repo#7'),
                         ticket(6, body=base + '## Blocked by\n- #7'),
                         ticket(7, body=base + '## Blocked by\n- #6'),
                         ticket(8, body=base + '## Blocked by\n- #999'),
                         ticket(9, body=base + '## Blocked by\n- needs approval'),
                         ticket(10, createdAt=None)]
        result = self.preview(range(1, 11))
        self.assertEqual(result['selected'], [])
        self.assertEqual(len(result['waiting']), 10)
        for reason in ('Acceptance criteria', 'Priority', 'Unknown', 'itself', 'Malformed', '#7', '#6', '#999', 'timestamp'):
            self.assertIn(reason, str(result['waiting']))
        self.assertEqual(self.gh.writes, [])

    def test_closed_prerequisites_are_verified_in_same_repo(self):
        self.gh.items[0]['body'] += '## Blocked by\n- #9'
        self.gh.extra_issues[9] = ticket(9, state='CLOSED')
        self.assertEqual(self.preview()['selected'], [1])
        self.gh.extra_issues[9]['repository'] = {'nameWithOwner': 'other/app'}
        self.assertEqual(self.preview()['selected'], [])
        self.assertIn('identity', str(self.preview()['waiting']))
        for query, variables in self.gh.reads:
            if 'issue(number:$number)' in query:
                self.assertEqual((variables['owner'], variables['repo']), ('example', 'app'))

    def test_dependency_conversion_error_is_reported_without_lookup(self):
        limit = getattr(sys, 'get_int_max_str_digits', lambda: 0)()
        digits = '9' * (limit + 1 if limit else 4301)
        body = issue()['body'] + '\n## Blocked by\n- #'
        card = ticket(body=body + digits)
        problem = 'Malformed dependency: issue number is too large to parse'
        # Older Python builds or hosts with the conversion cap disabled still
        # exercise the same conversion-failure path deterministically.
        conversion = nullcontext() if limit else patch('sfc_lib.ticket_validation.int',
                                                       side_effect=ValueError('fixture conversion cap'), create=True)
        with conversion:
            self.assertEqual(lint_card(card), [problem])
            self.assertIn(problem, pickup.eligibility(self.gh, card, lint_card))
        self.assertEqual(self.gh.reads, [])

        self.gh.extra_issues[19] = ticket(19, state='CLOSED')
        self.assertEqual(pickup.eligibility(self.gh, ticket(body=body + '19'), lint_card), [])
        lookups = [(query, variables) for query, variables in self.gh.reads if 'issue(number:$number)' in query]
        self.assertEqual([variables['number'] for _, variables in lookups], [19])

    def test_dependency_rejects_mixed_script_digits_without_lookup(self):
        body = issue()['body'] + '\n## Blocked by\n- #'
        for digits in ('1٩', '١9', '1９'):
            with self.subTest(digits=digits):
                card = ticket(body=body + digits)
                self.assertIn('Malformed dependency', lint_card(card)[0])
                self.assertTrue(pickup.eligibility(self.gh, card, lint_card))
        self.assertEqual(self.gh.reads, [])

    def test_missing_ambiguous_values_never_use_backlog_or_priority_fallback(self):
        for kind in ('missing-stage', 'duplicate-stage', 'missing-priority', 'duplicate-priority', 'unmapped-priority'):
            def values(c, nodes):
                if kind == 'missing-stage': return nodes[1:]
                if kind == 'duplicate-stage': return nodes + [nodes[0]]
                if kind == 'missing-priority': return nodes[:1]
                if kind == 'duplicate-priority': return nodes + [nodes[1]]
                return [nodes[0], {'field': {'id': 'priority-field'}, 'optionId': 'manual-option'}]
            self.gh.values_override = values
            with self.subTest(kind=kind):
                self.assertEqual(self.preview()['selected'], [])
                self.assertTrue(self.preview()['waiting'])
        self.assertEqual(self.gh.writes, [])

    def test_full_pagination_items_fields_values_labels_and_nonissues(self):
        self.gh.page_size = 1
        self.gh.items = [ticket(1), ticket(2, status='Backlog', labels=['qa', 'maintenance']), ticket(3)]
        self.gh.extra_items = [{'id': 'draft', 'content': {'__typename': 'DraftIssue'}},
                               {'id': 'pr', 'content': {'__typename': 'PullRequest'}},
                               {'id': 'foreign', 'content': {'__typename': 'Issue', 'repository': {'nameWithOwner': 'other/app'}}}]
        self.f.c['max_workers'] = 3
        self.assertEqual(self.preview((1, 2, 3))['selected'], [1, 2, 3])
        self.assertEqual(len(pickup.cards(self.gh)[1]['labels']), 2)
        for fragment in ('fields(', 'items(', 'fieldValues(', 'labels('):
            self.assertTrue(any(fragment in q and v.get('after') for q, v in self.gh.reads), fragment)

    def test_incomplete_ambiguous_inventories_halt_without_writes(self):
        for variant in ('duplicate-field', 'duplicate-stage-options', 'bad-priority', 'missing-priority'):
            gh = PickupGitHub(self.root, [ticket(status='Backlog')])
            if variant == 'duplicate-field': gh.fields.append(copy.deepcopy(gh.fields[0]))
            if variant == 'duplicate-stage-options': gh.fields[0]['options'].append(copy.deepcopy(gh.fields[0]['options'][0]))
            if variant == 'bad-priority': gh.fields[1]['options'][0]['description'] = 'Reversed meanings'
            if variant == 'missing-priority': gh.fields.pop()
            with self.subTest(variant=variant), self.assertRaises(Halt):
                pickup.cards(gh)
            self.assertEqual(gh.writes, [])
        self.gh.page_size = 1
        original = self.gh.page
        def stuck(nodes, after):
            result = original(nodes, after)
            result['pageInfo'] = {'hasNextPage': True, 'endCursor': '1'}
            return result
        with patch.object(self.gh, 'page', side_effect=stuck), self.assertRaises(Halt):
            self.preview()
        self.assertEqual(self.gh.writes, [])

    def test_custom_options_use_explicit_mapping_and_detect_meaning_drift(self):
        self.gh.c['priority'] = {'field_id': 'priority-field', 'options': {p: 'priority-' + p for p in ('P0', 'P1', 'P2', 'P3')}}
        self.gh.fields[1]['name'] = 'Urgency convention'
        for option in self.gh.fields[1]['options']:
            option['name'] = 'Custom ' + option['id']
            option['description'] = 'Documented custom interpretation'
        self.assertEqual(self.preview()['selected'], [1])
        before = pickup.scope(self.gh)
        self.gh.fields[1]['options'][0]['description'] = 'Manual change'
        self.assertNotEqual(pickup.scope(self.gh), before)
        self.assertEqual(self.gh.writes, [])

    def test_explicit_scope_excludes_demo_unrelated_ready_and_blocked(self):
        self.gh.items += [ticket(15, priority='P0'), ticket(3), ticket(4, status='Blocked')]
        result = self.preview((1, 4, 9))
        self.assertEqual(result['selected'], [1])
        self.assertFalse(any(w['issue'] in (3, 15) for w in result['waiting']))
        self.assertIn('current stage: Blocked', str(result['waiting']))
        self.assertIn('Not a verified issue', str(result['waiting']))
        for kwargs in ({'include_backlog': True}, {'issues': [1]}, {'include_backlog': True, 'issues': [-1]}):
            with self.assertRaises(Halt): self.f.run(dry_run=True, **kwargs)

    def test_claim_method_requires_active_run_and_scope(self):
        card = pickup.cards(self.gh)[0]
        with self.assertRaisesRegex(Halt, 'active explicitly scoped run'):
            self.f.claim_priority(card)
        self.assertEqual(self.gh.writes, [])

    def test_saved_or_exhausted_execution_is_not_fresh_work(self):
        for saved in ({'worktree': 'saved'}, {'pr': 123}, {'attempts': 2}):
            self.f.state['cards']['1'] = saved
            result = self.preview()
            self.assertEqual(result['selected'], [])
            self.assertIn('recovery', str(result['waiting']))

    def test_project_or_origin_identity_drift_halts_reads(self):
        with patch.object(self.gh, 'verify_repository', side_effect=Halt('Wrong origin')):
            with self.assertRaisesRegex(Halt, 'Wrong origin'): self.preview()
        self.gh._meta = {'id': 'other', 'field': 'stage', 'options': {}}
        with self.assertRaisesRegex(Halt, 'Project identity'): self.preview()
        self.assertEqual(self.gh.writes, [])

    def test_cli_preview_and_diagnostic_receipts_are_read_only(self):
        write_json(self.root / '.sfc/config.json', self.f.c)
        write_json(self.root / '.sfc/state.json', {'cards': {}, 'error': 'Preserve this diagnostic'})
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        out, err = io.StringIO(), io.StringIO()
        with patch('sfc_lib.factory.GitHub', return_value=self.gh), redirect_stdout(out), redirect_stderr(err):
            code = sfc.main(['--root', str(self.root), 'run', '--dry-run', '--include-backlog', '--issue', '1'])
        self.assertEqual(code, 0, err.getvalue())
        result = json.loads(out.getvalue())
        self.assertEqual(result['selected'], [1])
        self.assertEqual(result['options'], {'include_backlog': True, 'issues': [1], 'allow_merge': False,
                                             'repository': 'example/app', 'project': BASE_CONFIG['project'],
                                             'status_field': BASE_CONFIG['status_field']})
        self.assertEqual({p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}, before)
        self.assertEqual(self.gh.writes + self.gh.activity_writes, [])


class ScopedPlanningTests(unittest.TestCase):
    setUp = PickupTests.setUp
    preview = PickupTests.preview

    def tearDown(self):
        self.assertEqual(self.gh.writes + self.gh.activity_writes + self.gh.notes, [])

    def test_all_membership_pages_multiple_targets_capacity_and_missing_wait(self):
        self.gh.items = [ticket(n, body='Unrelated invalid criteria', priority=None)
                         for n in range(1, 206)]
        self.gh.items[0] = ticket(1)
        self.gh.items[-1] = ticket(205, priority='P0')
        self.f.c['max_workers'] = 1
        pickup.scope(self.gh)  # Exclude the adapter's initial metadata discovery.
        self.gh.reads.clear()
        with patch.object(self.gh, 'call', wraps=self.gh.call) as calls:
            result = self.preview((1, 205, 999))
        self.assertEqual(result['selected'], [205])
        self.assertIn('capacity', str(result['waiting']))
        self.assertIn('Not a verified issue', str(result['waiting']))
        self.assertEqual({w['issue'] for w in result['waiting']}, {1, 999})
        self.assertEqual(len(self.gh.reads), 7)  # Two scope, three membership pages, two combined details.
        self.assertEqual(calls.call_count, 2)
        for fragment, key, expected in (('items(', 'after', [None, '100', '200']),
                                         ('fieldValues(', 'item', ['item-1', 'item-205']),
                                         ('labels(', 'number', [1, 205])):
            self.assertEqual([v[key] for q, v in self.gh.reads if fragment in q], expected)

    def test_combined_card_read_rejects_disagreeing_issue_views(self):
        original = self.gh.graphql

        def disagree(query, variables):
            result = original(query, variables)
            if 'fieldValues(' in query and 'labels(' in query:
                result['repository']['issue']['title'] = 'Changed between views'
            return result

        with patch.object(self.gh, 'graphql', side_effect=disagree):
            with self.assertRaisesRegex(Halt, 'Project item and actual issue changed or disagree'):
                self.preview()

    def test_malformed_or_duplicate_lightweight_identities_halt_before_details(self):
        variants = {'number': None, 'bool': True, 'negative': -1, 'string': '2',
                    'id': None, 'repository': None, 'repo-name': '', 'repo-format': 'example',
                    'type': None, 'duplicate-number': 1, 'duplicate-id': 'issue-1',
                    'duplicate-item': 'item-1', 'missing-content': None, 'empty-content': {}}
        original = self.gh.graphql
        for kind, value in variants.items():
            self.gh.items = [ticket(1), ticket(2)]
            self.gh.reads.clear()
            def malformed(query, variables):
                result = original(query, variables)
                if 'items(' in query:
                    item = result['node']['items']['nodes'][1]
                    content = item['content']
                    if kind in ('number', 'bool', 'negative', 'string', 'duplicate-number'):
                        content['number'] = value
                    elif kind in ('id', 'duplicate-id'): content['id'] = value
                    elif kind == 'repository': content['repository'] = value
                    elif kind in ('repo-name', 'repo-format'): content['repository']['nameWithOwner'] = value
                    elif kind == 'type': content['__typename'] = value
                    elif kind == 'duplicate-item': item['id'] = value
                    elif kind == 'missing-content': del item['content']
                    else: item['content'] = value
                return result
            with self.subTest(kind=kind), patch.object(self.gh, 'graphql', side_effect=malformed):
                with self.assertRaises(Halt): self.preview()
            self.assertFalse(any('fieldValues(' in q or 'labels(' in q for q, _ in self.gh.reads))

    def test_inventory_target_must_match_detailed_identity_and_removed_target_waits(self):
        for key, value in (('id', 'changed-id'), ('number', 2),
                           ('repository', {'nameWithOwner': 'other/app'})):
            self.gh.items = [ticket(1)]
            self.gh.reads.clear()
            def change(query, variables):
                if 'fieldValues(' in query:
                    self.gh.items[0][key] = value
            self.gh.on_read = change
            with self.subTest(key=key), self.assertRaises(Halt):
                self.preview()
            self.assertFalse(any('labels(' in q and 'fieldValues(' not in q for q, _ in self.gh.reads))
        self.gh.on_read = None
        self.gh.items = [ticket(1)]
        original = self.gh.graphql
        def removed(query, variables):
            result = original(query, variables)
            return {'node': None} if 'fieldValues(' in query else result
        with patch.object(self.gh, 'graphql', side_effect=removed), self.assertRaises(Halt):
            self.preview()
        self.gh.items = [ticket(2)]
        result = self.preview()
        self.assertEqual(result['selected'], [])
        self.assertIn('Not a verified issue', str(result['waiting']))

    def test_membership_pagination_cannot_stop_early_or_repeat(self):
        self.gh.items = [ticket(1), ticket(2), ticket(3)]
        self.gh.page_size = 1
        original = self.gh.graphql
        for kind in ('missing-cursor', 'repeated-cursor', 'missing-page', 'duplicate-later-target'):
            self.gh.reads.clear()
            def incomplete(query, variables):
                result = original(query, variables)
                if 'items(' in query:
                    page = result['node']['items']
                    if kind == 'missing-cursor': page['pageInfo'].pop('endCursor')
                    elif kind == 'repeated-cursor': page['pageInfo'] = {'hasNextPage': True, 'endCursor': '1'}
                    elif kind == 'missing-page': page.pop('pageInfo')
                    elif variables['after']:
                        page['nodes'][0]['content'].update(id='issue-1', number=1)
                return result
            with self.subTest(kind=kind), patch.object(self.gh, 'graphql', side_effect=incomplete):
                with self.assertRaises((Halt, KeyError)): self.preview()
            self.assertFalse(any('fieldValues(' in q or 'labels(' in q for q, _ in self.gh.reads))

    def test_fresh_scope_after_details_detects_priority_and_routing_meaning_drift(self):
        for kind in ('priority', 'routing'):
            before = copy.deepcopy(self.gh.fields)
            def drift(query, variables):
                if 'labels(' in query:
                    if kind == 'priority': self.gh.fields[1]['options'][0]['description'] = 'Changed meaning'
                    else: self.gh.fields[0]['options'][0]['name'] = 'Changed stage'
            self.gh.on_read = drift
            with self.subTest(kind=kind), self.assertRaises(Halt): self.preview()
            self.gh.fields = before

    def test_outside_selection_dependencies_are_fresh_on_and_off_board(self):
        self.gh.items[0]['body'] += '## Blocked by\n- #9'
        for on_board in (False, True):
            dependency = ticket(9, state='CLOSED')
            self.gh.items = [self.gh.items[0]] + ([dependency] if on_board else [])
            self.gh.extra_issues = {} if on_board else {9: dependency}
            with self.subTest(on_board=on_board):
                self.assertEqual(self.preview()['selected'], [1])
                dependency['state'] = 'OPEN'
                self.assertIn('Open prerequisites: #9', str(self.preview()['waiting']))
                self.gh.items = self.gh.items[:1]
                self.gh.extra_issues = {}
                self.assertIn('unavailable', str(self.preview()['waiting']))
        self.assertTrue(any('labels(' in q and v['number'] == 9 for q, v in self.gh.reads))
        self.assertFalse(any('fieldValues(' in q and v['item'] == 'item-9' for q, v in self.gh.reads))


class OwnedTaskReadTests(unittest.TestCase):
    setUp = PickupTests.setUp

    def owned(self):
        self.f.include_backlog = True
        self.f.issue_scope = (1,)
        self.gh.items[0]['status'] = 'Review'
        return pickup.card(self.gh, 'item-1', pickup.scope(self.gh))

    def test_targeted_counts_are_constant_and_queue_inventory_is_preserved(self):
        from sfc_lib.watch import Watch
        for watching in (False, True):
            for size in (1, 25, 100, 101, 250):
                with self.subTest(watching=watching, size=size):
                    self.gh.items = [ticket(n) for n in range(1, size + 1)]
                    self.f.watcher = Watch(self.f) if watching else None
                    card = self.owned()  # Warm adapter metadata, as in the measured baseline.
                    self.gh.reads.clear()
                    with patch.object(self.gh, 'call', wraps=self.gh.call) as calls:
                        queue = self.f.queue_cards()
                    self.assertEqual(queue[0], card)
                    self.assertEqual(len(self.gh.reads), 2 + (size + 99) // 100 + (size if watching else 1))
                    self.assertEqual(calls.call_count, 2)  # Before/after Project identity reads.
                    self.assertEqual(len(queue), size if watching else 1)
                    expected = list(range(1, size + 1)) if watching else [1]
                    self.assertEqual([v['number'] for q, v in self.gh.reads if 'labels(' in q], expected)
                    self.assertEqual([v['item'] for q, v in self.gh.reads if 'fieldValues(' in q],
                                     ['item-' + str(n) for n in expected])
                    scope = self.f.issue_scope
                    self.gh.reads.clear()
                    with patch.object(self.gh, 'call', wraps=self.gh.call) as calls:
                        self.assertTrue(self.f.fresh_task(card))
                    self.assertEqual(len(self.gh.reads), 4)
                    self.assertEqual(calls.call_count, 2)
                    self.assertEqual(self.f.issue_scope, scope)
                    self.assertFalse(any('items(' in q for q, _ in self.gh.reads))
                    self.assertEqual([v['item'] for q, v in self.gh.reads if 'fieldValues(' in q], ['item-1'])
                    self.assertEqual([v['number'] for q, v in self.gh.reads if 'labels(' in q], [1])
                    self.assertEqual(self.gh.writes + self.gh.activity_writes, [])

    def test_target_fields_labels_and_both_scope_reads_remain_paginated(self):
        self.gh.page_size = 1
        self.gh.items[0]['labels'] = ['auth', 'bug', 'human-only']
        self.gh.items.append(ticket(2))
        card = self.owned()
        self.gh.reads.clear()
        with patch.object(self.f, 'task_digest', wraps=self.f.task_digest) as digest:
            self.assertTrue(self.f.fresh_task(card))
        self.assertEqual(digest.call_args_list[0].args[0], card)
        for fragment, cursors in (('fields(', [None, '1', None, '1']),
                                  ('fieldValues(', [None, '1']),
                                  ('labels(', [None, '1', '2'])):
            self.assertEqual([v['after'] for q, v in self.gh.reads if fragment in q], cursors)
        self.assertFalse(any('items(' in q for q, _ in self.gh.reads))
        self.assertEqual(self.gh.writes, [])

    def test_task_edits_still_return_for_qa(self):
        for key, value in (('body', 'Updated criteria'), ('title', 'Updated task'),
                           ('labels', ['bug', 'human-only'])):
            with self.subTest(key=key):
                card = self.owned()
                self.gh.items[0][key] = value
                with patch.object(self.f, 'move') as move:
                    self.assertFalse(self.f.fresh_task(card))
                move.assert_called_once_with(card, 'QA', 'Issue content changed; repeat QA')

    def test_closed_moved_or_changed_identity_blocks_before_writes(self):
        for key, value in (('state', 'CLOSED'), ('status', 'QA'), ('status', 'Unknown'),
                           ('id', 'replacement-issue'), ('number', 2),
                           ('url', 'https://github.com/example/app/issues/2')):
            with self.subTest(key=key, value=value):
                self.gh.items = [ticket()]
                card = self.owned()
                self.gh.items[0][key] = value
                with patch.object(self.f, 'move') as move, self.assertRaises(Halt):
                    self.f.fresh_task(card)
                move.assert_not_called()
        self.assertEqual(self.gh.writes, [])

    def test_target_removal_wrong_project_and_item_issue_disagreement_block(self):
        card = self.owned()
        original = self.gh.graphql
        for kind in ('removed', 'project', 'item', 'repository', 'disagreement'):
            def response(query, variables):
                result = original(query, variables)
                if 'fieldValues(' in query:
                    node = result['node']
                    if kind == 'removed': result['node'] = None
                    if kind == 'project': node['project']['id'] = 'another-project'
                    if kind == 'item': node['id'] = 'another-item'
                    if kind == 'repository': node['content']['repository']['nameWithOwner'] = 'other/app'
                    if kind == 'disagreement': node['content']['body'] += ' Item-only change'
                return result
            with self.subTest(kind=kind), patch.object(self.gh, 'graphql', side_effect=response):
                with patch.object(self.f, 'move') as move, self.assertRaises(Halt):
                    self.f.fresh_task(card)
                move.assert_not_called()
        self.assertEqual(self.gh.writes, [])

    def test_missing_or_ambiguous_stage_never_invents_backlog(self):
        card = self.owned()
        for kind in ('missing', 'duplicate', 'unmapped'):
            def values(c, nodes):
                if kind == 'missing': return nodes[1:]
                if kind == 'duplicate': return nodes + [nodes[0]]
                return [{'field': {'id': 'stage'}, 'optionId': 'unknown-option'}, nodes[1]]
            self.gh.values_override = values
            with self.subTest(kind=kind), self.assertRaisesRegex(Halt, 'moved externally'):
                self.f.fresh_task(card)
        self.assertEqual(self.gh.writes, [])

    def test_pinned_scope_and_before_after_meanings_cannot_drift(self):
        # Explicit mappings allow custom meanings, but every observed meaning
        # must still match the known card, both before and after its item read.
        self.gh.c['priority'] = {'field_id': 'priority-field', 'options': {
            p: 'priority-' + p for p in ('P0', 'P1', 'P2', 'P3')}}
        for kind in ('priority', 'routing', 'project', 'field'):
            for during in (False, True):
                with self.subTest(kind=kind, during=during):
                    fields = copy.deepcopy(self.gh.fields)
                    meta = self.gh._meta
                    card = self.owned()
                    def drift():
                        if kind == 'priority': self.gh.fields[1]['options'][0]['description'] += ' Changed'
                        if kind == 'routing': self.gh.fields[0]['options'][0]['name'] = 'Renamed stage'
                        if kind == 'project': self.gh._meta = {**self.gh._meta, 'id': 'other-project'}
                        if kind == 'field': self.gh.fields[0]['id'] = 'other-stage-field'
                    if during:
                        self.gh.on_read = lambda q, v: drift() if 'fieldValues(' in q else None
                    else:
                        drift()
                    with patch.object(self.f, 'move') as move, self.assertRaises(Halt):
                        self.f.fresh_task(card)
                    move.assert_not_called()
                    self.gh.on_read = None
                    self.gh.fields, self.gh._meta = fields, meta
        self.assertEqual(self.gh.writes, [])

    def test_incomplete_target_or_scope_pagination_blocks(self):
        card = self.owned()
        original = self.gh.graphql
        for fragment in ('fields(', 'fieldValues(', 'labels('):
            def response(query, variables):
                result = original(query, variables)
                if fragment in query:
                    page = (result['repository']['issue']['labels'] if fragment == 'labels(' else
                            result['node']['fieldValues' if fragment == 'fieldValues(' else 'fields'])
                    page['pageInfo'] = {'hasNextPage': True, 'endCursor': None}
                return result
            with self.subTest(fragment=fragment), patch.object(self.gh, 'graphql', side_effect=response):
                with self.assertRaisesRegex(Halt, 'pagination did not advance'):
                    self.f.fresh_task(card)
        self.assertEqual(self.gh.writes, [])

    def test_watcher_holds_exclusions_and_run_scope_still_block(self):
        from sfc_lib.watch import Watch
        for kind in ('hold', 'excluded', 'scope'):
            with self.subTest(kind=kind):
                self.gh.items = [ticket()]
                card = self.owned()
                self.f.watcher = Watch(self.f)
                if kind == 'hold': self.gh.items[0]['labels'].append('SFC-HOLD')
                if kind == 'excluded': self.f.watcher.settings['excluded_issues'] = [1]
                if kind == 'scope': self.f.issue_scope = (2,)
                with patch.object(self.f, 'move') as move, self.assertRaises(Halt):
                    self.f.fresh_task(card)
                move.assert_not_called()
        self.assertEqual(self.gh.writes, [])

    def test_all_modes_require_verified_priority_setup(self):
        card = self.owned()
        for pinned in (None, {}, {'project': card['_pickup_scope']['project']}):
            with self.subTest(pinned=pinned), self.assertRaisesRegex(Halt, 'scope changed or is missing'):
                self.f.fresh_task({**card, '_pickup_scope': pinned})
        self.f.include_backlog = False
        self.gh.fields.pop()
        with self.assertRaisesRegex(Halt, 'Priority field missing'):
            self.f.fresh_task(self.gh.cards()[0])



class PickupIntegrationTests(unittest.TestCase):
    setUp = factory_fixtures.IntegrationTests.setUp
    tearDown = factory_fixtures.IntegrationTests.tearDown

    def configure(self, cards=None):
        self.gh = PickupGitHub(self.root, cards or [ticket(status='Backlog')])
        self.c['max_cycles'] = 2
        self.factory = Factory(self.root, self.c, self.gh, self.proc)

    def run_pickup(self, issues=(1,)):
        return self.factory.run(include_backlog=True, issues=issues)

    def test_backlog_and_ready_claims_traverse_normal_lanes_and_keep_human_merge(self):
        self.configure([ticket(1, status='Backlog'), ticket(2), ticket(15, priority='P0'), ticket(3)])
        self.run_pickup((1, 2))
        self.assertEqual(self.proc.lanes.count('build'), 2)
        for lane in ('qa', 'security', 'review'):
            self.assertEqual(self.proc.lanes.count(lane), 2)
        self.assertEqual({n for n, status in self.gh.writes}, {1, 2})
        self.assertEqual({item for item, _ in self.gh.activity_writes}, {'item-1', 'item-2'})
        self.assertEqual(self.gh.merges, [])
        self.assertEqual(self.gh.items[2]['status'], 'Ready')
        self.assertEqual(self.gh.items[3]['status'], 'Ready')
        self.assertFalse(any(('labels(' in q and v['number'] in (3, 15)) or
                             ('fieldValues(' in q and v['item'] in ('item-3', 'item-15'))
                             for q, v in self.gh.reads))
        summary = read_json(self.factory.run_dir / 'summary.json')
        self.assertEqual(summary['run_options']['issues'], [1, 2])
        self.assertEqual([c['number'] for c in summary['plans'][0]['selected']], [1, 2])
        self.assertEqual(summary['cards']['1']['claim']['state'], 'verified')
        self.assertEqual(summary['cards']['1']['claim']['before']['status'], 'Backlog')
        self.assertEqual(summary['cards']['1']['claim']['observed']['status'], 'Building')
        self.assertTrue((self.root / '.sfc/worktrees/issue-1').is_dir())

    def test_unresolved_inventory_stops_before_claim_annotations_or_worker(self):
        self.configure([ticket(status='Backlog'), ticket(2, id=None)])
        with patch.object(self.factory, 'process') as worker, self.assertRaises(Halt):
            self.run_pickup()
        worker.assert_not_called()
        self.assertEqual(self.proc.lanes, [])
        self.assertEqual(self.gh.writes + self.gh.activity_writes + self.gh.notes, [])
        self.assertEqual(self.factory.state['cards'], {})

    def test_real_wave_claim_order_capacity_and_no_worker_outside_scope(self):
        self.configure([ticket(1, status='Backlog', priority='P3'),
                        ticket(2, status='Backlog', priority='P0'),
                        ticket(3, priority='P1'), ticket(15, priority='P0')])
        barrier = threading.Barrier(2)
        observed, lock = [], threading.Lock()
        def worker(card):
            with lock:
                observed.append(card['number'])
            barrier.wait(timeout=5)
        with patch.object(self.factory, 'process', side_effect=worker):
            self.run_pickup((1, 2, 3))
        self.assertEqual(self.gh.writes, [(2, 'Building'), (3, 'Building')])
        self.assertEqual(sorted(observed), [2, 3])
        self.assertEqual(self.gh.items[0]['status'], 'Backlog')
        self.assertEqual(self.gh.items[3]['status'], 'Ready')

    def test_custom_mapping_meaning_drift_before_claim_defers_without_write(self):
        self.configure()
        self.gh.c['priority'] = {'field_id': 'priority-field', 'options': {
            p: 'priority-' + p for p in ('P0', 'P1', 'P2', 'P3')}}
        original = self.factory.claim_priority
        def drift(card):
            self.gh.fields[1]['options'][2]['description'] = 'Changed custom meaning'
            return original(card)
        with patch.object(self.factory, 'claim_priority', side_effect=drift):
            self.run_pickup()
        self.assertEqual(self.gh.writes + self.gh.activity_writes, [])
        self.assertEqual(self.proc.lanes, [])

    def test_verified_claim_with_changed_task_before_worktree_does_not_resume(self):
        self.configure()
        with patch.object(self.factory, 'worktree', side_effect=Halt('Interruption')):
            with self.assertRaises(Halt): self.run_pickup()
        self.gh.items[0]['body'] += '- Changed task'
        previous = copy.deepcopy(self.gh.writes)
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        self.run_pickup()
        self.assertEqual(self.proc.lanes, [])
        self.assertEqual(self.gh.writes, previous)
        self.assertIn('recover explicitly', str(self.factory.state['waiting']))

    def test_qa_only_routing_from_backlog_has_no_build_or_pr(self):
        self.configure([ticket(status='Backlog', labels=['qa'])])
        self.run_pickup()
        self.assertEqual(self.proc.lanes, ['qa', 'security', 'review'])
        self.assertEqual(self.gh.writes[0], (1, 'QA'))
        self.assertEqual(self.gh.prs, {})
        self.assertEqual(self.gh.items[0]['status'], 'Done')

    def test_skipped_fresh_cards_have_no_mutations_or_worker(self):
        self.configure([ticket(status='Backlog', body='Missing criteria'), ticket(2, body='Missing criteria'),
                        ticket(15), ticket(3, status='Blocked')])
        self.run_pickup((1, 2, 3))
        self.assertEqual(self.proc.lanes, [])
        self.assertEqual(self.gh.writes + self.gh.activity_writes + self.gh.notes, [])
        self.assertFalse((self.root / '.sfc/worktrees').exists())

    def test_concurrent_changes_defer_without_claim_or_activity(self):
        changes = {'body': lambda gh: gh.items[0].update(body=issue()['body'] + '- Additional criterion'),
                   'title': lambda gh: gh.items[0].update(title='Changed task'),
                   'closed': lambda gh: gh.items[0].update(state='CLOSED'),
                   'stage': lambda gh: gh.items[0].update(status='Blocked'),
                   'priority': lambda gh: gh.items[0].update(priority='P0'),
                   'identity': lambda gh: gh.items[0].update(id='different'),
                   'labels': lambda gh: gh.items[0].update(labels=['qa']),
                   'meaning': lambda gh: gh.fields[1]['options'][0].update(description='Changed'),
                   'prerequisite': lambda gh: gh.extra_issues[9].update(state='OPEN')}
        for kind, change in changes.items():
            with self.subTest(kind=kind):
                # Each variant gets fresh isolated state and never creates a worker.
                self.configure([ticket(status='Backlog', body=issue()['body'] + '## Blocked by\n- #9')])
                self.gh.extra_issues[9] = ticket(9, state='CLOSED')
                original = self.factory.claim_priority
                def stale(card):
                    change(self.gh)
                    return original(card)
                with patch.object(self.factory, 'claim_priority', side_effect=stale):
                    if kind == 'meaning':
                        with self.assertRaises(Halt): self.run_pickup()
                    else:
                        self.run_pickup()
                self.assertEqual(self.gh.writes + self.gh.activity_writes, [])
                self.assertEqual(self.proc.lanes, [])

    def test_partial_lost_claims_latch_across_restart_and_default_run(self):
        for failure in ('before', 'after', 'drop'):
            with self.subTest(failure=failure):
                self.configure()
                self.factory.state['cards'] = {}
                self.factory.save()
                self.gh.fail_claim = failure
                with self.assertRaisesRegex(Halt, 'Incomplete priority claim'):
                    self.run_pickup()
                record = read_json(self.factory.state_path)['cards']['1']['claim']
                self.assertEqual(record['state'], 'incomplete')
                self.assertIn('observed_after_failure', record)
                self.assertEqual(self.proc.lanes, [])
                writes = copy.deepcopy(self.gh.writes)
                for opt_in in (True, False):
                    self.factory = Factory(self.root, self.c, self.gh, self.proc)
                    with self.assertRaisesRegex(Halt, 'Unresolved priority claim'):
                        if opt_in: self.run_pickup()
                        else: self.factory.run()
                self.assertEqual(self.gh.writes, writes)
                self.assertEqual(self.gh.activity_writes, [])
                self.assertEqual(self.proc.lanes, [])

    def test_abrupt_death_after_intent_never_invents_successful_claim(self):
        self.configure()
        card = pickup.cards(self.gh)[0]
        self.factory.update(card, claim={'state': 'intent', 'before': card, 'target': 'Building'})
        self.gh.items[0]['status'] = 'Building'  # Actual write may have completed before death.
        for opt_in in (True, False):
            self.factory = Factory(self.root, self.c, self.gh, self.proc)
            with self.assertRaisesRegex(Halt, 'Unresolved priority claim'):
                if opt_in: self.run_pickup()
                else: self.factory.run()
        self.assertEqual(self.proc.lanes, [])
        self.assertEqual(self.gh.writes + self.gh.activity_writes, [])
        self.assertEqual(self.factory.state['cards']['1']['claim']['state'], 'intent')

    def test_verified_claim_before_worktree_can_resume_after_interruption(self):
        self.configure()
        with patch.object(self.factory, 'worktree', side_effect=Halt('Interruption before worktree')):
            with self.assertRaisesRegex(Halt, 'Interruption'):
                self.run_pickup()
        self.assertEqual(self.factory.state['cards']['1']['claim']['state'], 'verified')
        self.assertEqual(self.proc.lanes, [])
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        self.run_pickup()
        self.assertEqual(self.proc.lanes, ['build', 'qa', 'security', 'review'])
        self.assertEqual(self.gh.writes.count((1, 'Building')), 1)
        self.assertEqual(self.gh.merges, [])

    def test_verified_claim_restart_waits_for_reopened_prerequisite_then_resumes(self):
        self.configure([ticket(status='Backlog', body=issue()['body'] + '## Blocked by\n- #9')])
        self.gh.extra_issues[9] = ticket(9, state='CLOSED')
        with patch.object(self.factory, 'worktree', side_effect=Halt('Interruption before worktree')):
            with self.assertRaisesRegex(Halt, 'Interruption'):
                self.run_pickup()
        saved = copy.deepcopy(self.factory.state['cards']['1'])
        self.assertEqual(saved['claim']['state'], 'verified')
        self.assertNotIn('worktree', saved)
        self.gh.extra_issues[9]['state'] = 'OPEN'
        writes = copy.deepcopy(self.gh.writes + self.gh.activity_writes + self.gh.notes)
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        preview = self.factory.run(dry_run=True, include_backlog=True, issues=(1,))
        self.assertEqual(preview['selected'], [])
        self.assertIn('Open prerequisites: #9', str(preview['waiting']))
        self.run_pickup()
        self.assertEqual(self.factory.state['outcome'], 'waiting')
        summary = read_json(self.factory.run_dir / 'summary.json')
        self.assertIn('Open prerequisites: #9', str(summary['waiting']))
        self.assertEqual(self.factory.state['cards']['1'], saved)
        self.assertEqual(self.gh.writes + self.gh.activity_writes + self.gh.notes, writes)
        self.assertEqual(self.proc.lanes, [])
        self.assertFalse((self.root / '.sfc/worktrees/issue-1').exists())
        self.gh.extra_issues[9]['state'] = 'CLOSED'
        self.run_pickup()
        self.assertEqual(self.proc.lanes, ['build', 'qa', 'security', 'review'])
        self.assertEqual(self.gh.writes.count((1, 'Building')), 1)

    def test_resumed_prerequisite_reopening_after_plan_defers_before_dispatch(self):
        self.configure([ticket(status='Backlog', body=issue()['body'] + '## Blocked by\n- #9')])
        self.gh.extra_issues[9] = ticket(9, state='CLOSED')
        with patch.object(self.factory, 'worktree', side_effect=Halt('Interruption before worktree')):
            with self.assertRaisesRegex(Halt, 'Interruption'):
                self.run_pickup()
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        writes = copy.deepcopy(self.gh.writes + self.gh.activity_writes + self.gh.notes)
        original = self.factory.plan
        def reopen(cards):
            result = original(cards)
            if self.gh.extra_issues[9]['state'] == 'CLOSED':
                self.assertEqual([c['number'] for c in result[0]], [1])
                self.gh.extra_issues[9]['state'] = 'OPEN'
            return result
        with patch.object(self.factory, 'plan', side_effect=reopen), patch.object(self.factory, 'process') as worker:
            self.run_pickup()
        worker.assert_not_called()
        self.assertEqual(self.proc.lanes, [])
        self.assertEqual(self.gh.writes + self.gh.activity_writes + self.gh.notes, writes)
        summary = read_json(self.factory.run_dir / 'summary.json')
        self.assertTrue(any('Admission deferred' in v['detail'] for v in summary['cards']['1']['history']))
        self.assertEqual(summary['cards']['1']['claim']['state'], 'verified')

    def test_readback_manual_change_or_prerequisite_reopen_prevents_worker(self):
        for kind in ('manual', 'prerequisite'):
            self.configure([ticket(status='Backlog', body=issue()['body'] + '## Blocked by\n- #9')])
            self.factory.state['cards'] = {}
            self.factory.save()
            self.gh.extra_issues[9] = ticket(9, state='CLOSED')
            def change(card, status):
                if kind == 'manual': self.gh.items[0]['priority'] = 'P3'
                else: self.gh.extra_issues[9]['state'] = 'OPEN'
            self.gh.on_move = change
            with self.subTest(kind=kind), self.assertRaisesRegex(Halt, 'Incomplete priority claim'):
                self.run_pickup()
            self.assertEqual(self.factory.state['cards']['1']['claim']['state'], 'incomplete')
            self.assertEqual(self.proc.lanes, [])
            self.assertEqual(self.gh.activity_writes, [])


if __name__ == '__main__':
    unittest.main()
