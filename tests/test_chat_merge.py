"""Chat merge contract: real temporary Git, fake GitHub and native Codex only."""
import copy
import json
import shutil
from pathlib import Path
import unittest
from unittest.mock import patch

import test_factory as fixtures
from test_local_merge import AtomicGitHub
from test_pickup import ticket
from test_review_policy import PolicyProcesses, capabilities
from sfc_lib import chat_merge, chat_tracking, pickup, review_policy
from sfc_lib.common import Halt, NotDispatched, Stopped, common_dir, exclusive, git, read_json, run, write_json


class ChatGitHub(AtomicGitHub):
    repository_id = 'repo-node'
    default_branch = 'main'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        head, base = self.oid('topic'), self.oid('main')
        self.merge_tree = self.oid(head + '^{tree}')
        self.merge_parents = [base, head]
        self.merge_oid = git(self.root, 'commit-tree', self.merge_tree, '-p', base, '-p', head, '-m', 'Test merge')
        self.merge_ci = []
        self.other_prs = []
        self.excluded_ci = {'head': [], 'merge': []}

    def local_merge_snapshot(self, *args):
        snapshot = super().local_merge_snapshot(*args)
        snapshot['defaultBranchRef'] = {'name': self.default_branch}
        snapshot['pullRequest']['potentialMergeCommit'] = {
            'oid': self.merge_oid, 'tree': {'oid': self.merge_tree},
            'parents': {'totalCount': 2, 'nodes': [{'oid': oid} for oid in self.merge_parents]}}
        return snapshot

    def delivery_pr(self, number):
        result = self.pr(number)
        result.pop('statusCheckRollup', None)
        return result

    @staticmethod
    def rollup(rows, sha):
        if not rows:
            return None
        nodes = []
        for index, row in enumerate(rows):
            kind = 'StatusContext' if 'state' in row else 'CheckRun'
            node = {'__typename': kind, 'id': row.get('id', sha + '-' + str(index)), **copy.deepcopy(row)}
            node['context' if kind == 'StatusContext' else 'name'] = row.get('context' if kind == 'StatusContext' else 'name', 'check-' + str(index))
            nodes.append(node)
        state = 'SUCCESS' if chat_merge.checks_pass({'statusCheckRollup': nodes}, False) else 'FAILURE'
        return {'state': state, 'contexts': {'nodes': nodes, 'pageInfo': {'hasNextPage': False, 'endCursor': None}}}

    def chat_delivery_snapshot(self, number, issue, base, head, item_id, field_id, *, check_ids=()):
        snapshot = self.local_merge_snapshot(number, issue, base, head)
        card = next(value for value in self.items if value['number'] == issue)
        meta = self.metadata()
        content = {k: copy.deepcopy(card[k]) for k in ('id', 'number', 'title', 'body', 'url', 'state', 'repository')}
        snapshot['issue'].update(copy.deepcopy(content))
        snapshot['issue']['labels'] = {'nodes': [{'id': 'label-' + name, 'name': name} for name in card['labels']],
                                       'pageInfo': {'hasNextPage': False, 'endCursor': None}}
        snapshot['tracking_item'] = {'id': item_id, 'project': {'id': meta['id']}, 'content': content,
                                     'fieldValues': {'nodes': [{'field': {'id': meta['field']},
                                                               'optionId': meta['options'][card['status']]}],
                                                     'pageInfo': {'hasNextPage': False, 'endCursor': None}}}
        snapshot['routing_field'] = {'id': meta['field'], 'name': self.c['status_field'],
                                     'options': [{'name': name, 'id': value} for name, value in meta['options'].items()]}
        if snapshot['head'] is not None:
            snapshot['head']['target']['statusCheckRollup'] = self.rollup(self.ci, snapshot['head']['target']['oid'])
        snapshot['pullRequest']['potentialMergeCommit']['statusCheckRollup'] = self.rollup(self.merge_ci, self.merge_oid)
        selected = snapshot['pullRequest']
        snapshot['openDeliveryPRs'] = {'nodes': [{k: selected[k] for k in ('id', 'number', 'url', 'headRefOid', 'baseRefName')}] + copy.deepcopy(self.other_prs),
                                     'pageInfo': {'hasNextPage': False, 'endCursor': None}}
        all_nodes = {}
        for key, sha, rows in (('head', snapshot['pullRequest']['headRefOid'], self.ci), ('merge', self.merge_oid, self.merge_ci)):
            complete = self.rollup(rows + self.excluded_ci[key], sha)
            for node in (complete['contexts']['nodes'] if complete else []):
                all_nodes[node['id']] = node
        snapshot['known_checks'] = [copy.deepcopy(all_nodes.get(identity)) for identity in check_ids]
        return snapshot

    def local_merge_checks(self, head, *, identities=False):
        key = 'merge' if head == self.merge_oid else 'head'
        rows = (self.merge_ci if key == 'merge' else self.ci) + self.excluded_ci[key]
        if identities:
            result = self.rollup(rows, head)
            return result['contexts']['nodes'] if result else []
        return copy.deepcopy(rows)

    def onboarding_identity(self):
        return {'host': 'github.com', 'repository_id': self.repository_id,
                'repository': self.c['repo'], 'project_id': 'p'}

    def graphql(self, query, variables):
        if 'ref(qualifiedName:$base)' in query:
            return {'repository': {'defaultBranchRef': {'name': self.default_branch},
                                   'ref': {'branchProtectionRule': self.protected, 'rules': {'totalCount': self.rules}}}}
        return super().graphql(query, variables)

    def atomic_fast_forward(self, intent):
        assert read_json(self.journal)['chat_merge']['id'] == intent['id']
        self.attempts.append(copy.deepcopy(intent))
        if self.before_write:
            self.before_write(intent)
        transaction = (f"start\nverify {intent['head_ref']} {intent['head']}\n"
                       f"update {intent['base_ref']} {intent['head']} {intent['base']}\nprepare\ncommit\n")
        run(['git', 'update-ref', '--stdin'], self.remote, input=transaction)
        self.applied.append(copy.deepcopy(intent))
        self.prs[intent['pr']].update(state='MERGED', mergeCommit={'oid': intent['head']})
        if self.close_issue:
            self.items[0]['state'] = 'CLOSED'
        if self.lost_response:
            raise Halt('Fixture lost the atomic write response')


class NativeProcesses(PolicyProcesses):
    def execute(self, argv, cwd, log, timeout, prompt=None, **kwargs):
        if argv[0] == 'codex':
            scope = json.loads(prompt.split('\nExact scope: ')[1].splitlines()[0])
            criteria = json.loads(prompt.split('\nAcceptance criteria: ')[1].splitlines()[0])
            checks = json.loads(prompt.split('checks; do not repeat them: ')[1].splitlines()[0])
            prompt = ('an SFC review worker\nExact scope (echo unchanged): ' + json.dumps(scope) +
                      '\nAcceptance obligations: ' + json.dumps([{'id': c} for c in criteria]) +
                      '\nconfiguration:\n' + json.dumps({'checks': checks}))
        return super().execute(argv, cwd, log, timeout, prompt, **kwargs)


class ChatMergeTests(unittest.TestCase):
    tearDown = fixtures.IntegrationTests.tearDown

    def setUp(self):
        fixtures.IntegrationTests.setUp(self)
        self.base = git(self.root, 'rev-parse', 'HEAD')
        git(self.root, 'switch', '-c', 'topic')
        (self.root / 'value.txt').write_text('fixed\n')
        git(self.root, 'commit', '-am', 'Fix the value')
        self.head = git(self.root, 'rev-parse', 'HEAD')
        git(self.root, 'push', 'origin', 'topic')
        self.c.update(workflow_mode='chat', merge_policy='auto', auto_max_lines=800,
                      chat_merge_provider=chat_merge.PROVIDER,
                      chat_merge_authority={'repo': 'example/app', 'base_branch': 'main', 'host': 'github.com',
                                            'repository_id': 'repo-node', 'provider': chat_merge.PROVIDER,
                                            'scope': 'user-requested-tasks'})
        write_json(self.root / '.sfc/config.json', self.c)
        self.gh = ChatGitHub(self.root, self.remote, [ticket(status='QA')])
        self.gh.c = self.c
        body = self.root / '.sfc/body.txt'; body.write_text('Fixture PR')
        self.gh.create_pr(self.gh.items[0], 'topic', body)
        self.gh.prs[101]['isDraft'] = False
        self.task = 'chat-merge-contract'
        chat_tracking.start(self.root, self.c, task=self.task, issue=1, title='Fix value', summary='Correct the value',
                            acceptance=['The value is fixed'])
        chat_tracking.update(self.root, self.c, task=self.task, event_id='qa', stage='QA', summary='Observed checks',
                             pr=101, closes_issue=True)
        self.path = self.gh.journal = chat_tracking.task_path(self.root, self.task)
        record = read_json(self.path)
        card = pickup.cards(self.gh, [1])[0]
        record.update(issue={k: card[k] for k in ('id', 'number', 'url', 'item_id')},
                      project=card['_pickup_scope']['project'], board_stage='QA')
        write_json(self.path, record)
        self.processes = NativeProcesses(self.root / '.sfc/stop')
        self.processes.mutate = lambda result: [row.update(evidence='Inspected actual assertion and check log')
                                                for row in result['coverage']]
        # Protocol replays use fake GitHub/Codex and ordinary temporary Git.
        # Real process/guardian ownership has separate subprocess acceptance tests.
        for context in (patch('sfc_lib.lifeline.supported', return_value=False),
                        patch('sfc_lib.review_policy.run', side_effect=capabilities),
                        patch('sfc_lib.review_policy.native_sessions', return_value=self.root / '.sfc/sessions')):
            context.start(); self.addCleanup(context.stop)

    def call(self, apply=False):
        return chat_merge.delivery(self.gh, self.root, self.task, self.root, self.head, self.base,
                                   apply=apply, processes=self.processes)

    def reviewed(self):
        self.assertEqual(self.call()['state'], 'qualified')
        chat_tracking.update(self.root, self.c, task=self.task, event_id='review', stage='Review', summary='Independent pass')
        self.gh.items[0]['status'] = 'Review'
        # Seed the fake board's verified Review/attachment checkpoint.
        record = read_json(self.path)
        record['board_stage'] = 'Review'
        for event in record['events']:
            event['synced'] = 'fixture-checkpoint'
        record['native_pr_attachment'] = {'state': 'verified', 'binding': {
            'issue': self.gh.items[0]['id'], 'pull_request': 'pr-node-101', 'base': 'main',
            'url': 'https://github.com/example/app/pull/101'}}
        write_json(self.path, record)

    def test_a_check_omitted_by_rollup_still_gates_on_its_final_identity_read(self):
        self.gh.excluded_ci['head'] = [{'id': 'hidden-security', 'name': 'security', 'status': 'COMPLETED', 'conclusion': 'SUCCESS'}]
        self.reviewed()
        original = self.gh.chat_delivery_snapshot
        reads = 0
        def fail(*args, **kwargs):
            nonlocal reads
            reads += 1
            if reads == 4:
                self.gh.excluded_ci['head'][0]['conclusion'] = 'FAILURE'
            return original(*args, **kwargs)
        with patch.object(self.gh, 'chat_delivery_snapshot', side_effect=fail), self.assertRaisesRegex(Halt, 'GitHub checks'):
            self.call(apply=True)
        self.assertEqual(self.gh.attempts, [])
        self.assertNotIn('chat_merge', read_json(self.path))

    def test_newer_failed_test_merge_check_in_final_response_prevents_dispatch(self):
        self.reviewed(); self.gh.merge_ci = [{'id': 'merge-job', 'name': 'unit', 'status': 'COMPLETED', 'conclusion': 'SUCCESS'}]
        original = self.gh.chat_delivery_snapshot
        reads = 0
        def fail(*args, **kwargs):
            nonlocal reads
            reads += 1
            if reads == 4:
                self.gh.merge_ci[0]['conclusion'] = 'FAILURE'
            return original(*args, **kwargs)
        with patch.object(self.gh, 'chat_delivery_snapshot', side_effect=fail), self.assertRaisesRegex(Halt, 'GitHub checks'):
            self.call(apply=True)
        self.assertEqual(self.gh.attempts, [])
        self.assertNotIn('chat_merge', read_json(self.path))

    def test_final_ci_receipt_preserves_named_check_and_context_identities(self):
        self.gh.ci = [{'id': 'unit-run', 'name': 'unit', 'status': 'COMPLETED', 'conclusion': 'SUCCESS'},
                      {'id': 'lint-context', 'context': 'lint', 'state': 'SUCCESS'}]
        self.reviewed(); self.call(apply=True)
        receipt = read_json(self.path)['chat_merge']['ci']
        self.assertEqual(receipt['checks_source'], self.head)
        self.assertEqual([(row['id'], row.get('name', row.get('context'))) for row in receipt['checks']],
                         [('unit-run', 'unit'), ('lint-context', 'lint')])

    def test_other_newly_reachable_open_pr_heads_require_a_handoff(self):
        self.gh.other_prs = [{'id': 'other-pr', 'number': 102, 'url': 'https://github.com/example/app/pull/102',
                             'headRefOid': self.head, 'baseRefName': 'main'}]
        with self.assertRaisesRegex(Halt, 'indirectly merge'):
            self.call()
        self.assertNotIn('delivery_attempts', read_json(self.path))
        self.gh.other_prs[0]['headRefOid'] = self.base  # Already reachable; not imported by this write.
        self.assertEqual(self.call()['state'], 'qualified')

    def test_shallow_or_replaced_history_is_not_an_authority_inventory(self):
        shallow = common_dir(self.root) / 'shallow'; shallow.write_text(self.base + '\n')
        try:
            with self.assertRaisesRegex(Halt, 'complete unmodified Git history'):
                self.call()
        finally:
            shallow.unlink()
        replacement = git(self.root, 'commit-tree', self.base + '^{tree}', '-m', 'Replacement')
        git(self.root, 'replace', self.base, replacement)
        with self.assertRaisesRegex(Halt, 'complete unmodified Git history'):
            self.call()
        self.assertNotIn('delivery_attempts', read_json(self.path))

    def test_newer_failing_applicable_head_rollup_prevents_dispatch(self):
        self.reviewed()
        original = self.gh.chat_delivery_snapshot
        reads = 0
        def newer(*args, **kwargs):
            nonlocal reads
            reads += 1
            if reads == 4:
                self.gh.ci = [{'status': 'COMPLETED', 'conclusion': 'FAILURE'}]
            return original(*args, **kwargs)
        with patch.object(self.gh, 'chat_delivery_snapshot', side_effect=newer), self.assertRaisesRegex(Halt, 'GitHub checks'):
            self.call(apply=True)
        self.assertEqual(self.gh.attempts, [])
        self.assertNotIn('chat_merge', read_json(self.path))

    def test_prelaunch_io_cancel_is_known_unsent_and_preserves_allowance(self):
        self.reviewed()
        def no_launch(intent):
            self.processes.cancel_check = lambda: True
            with patch('sfc_lib.lifeline.spawn') as spawn:
                with self.assertRaises(NotDispatched):
                    run(['must-not-launch'], self.root)
                spawn.assert_not_called()
            raise NotDispatched('Fixture cancellation before launch')
        with patch.object(self.gh, 'atomic_fast_forward', side_effect=no_launch), self.assertRaises(NotDispatched):
            self.call(apply=True)
        record = read_json(self.path)
        self.assertEqual(record['chat_merge']['state'], 'not-dispatched')
        self.assertEqual(len(record['delivery_attempts']), 1)
        self.reviewed(); self.call(apply=True)
        self.assertEqual(len(read_json(self.path)['delivery_attempts']), 2)
        self.assertEqual(len(self.gh.attempts), 1)

    def test_color_config_cannot_hide_transient_credential_content(self):
        git(self.root, 'config', 'color.ui', 'always')
        git(self.root, 'config', 'color.diff', 'always')
        (self.root / 'value.txt').write_text('sk-' + 'x' * 30 + '\n')
        git(self.root, 'commit', '-am', 'Transient credential')
        (self.root / 'value.txt').write_text('fixed\n'); git(self.root, 'commit', '-am', 'Remove credential')
        self.head = git(self.root, 'rev-parse', 'HEAD'); git(self.root, 'push', 'origin', 'topic')
        with self.assertRaisesRegex(Halt, 'live-looking credential'):
            self.call()
        self.assertNotIn('delivery_attempts', read_json(self.path))

    def test_final_query_observes_draft_changes_after_the_last_card_read(self):
        self.reviewed()
        original = chat_tracking.verified_card
        reads = 0
        def change(*args):
            nonlocal reads
            card = original(*args); reads += 1
            if reads == 3:
                self.gh.prs[101]['isDraft'] = True
            return card
        with patch.object(chat_tracking, 'verified_card', side_effect=change), self.assertRaisesRegex(Halt, 'ready PR'):
            self.call(apply=True)
        self.assertEqual(self.gh.attempts, [])

    def test_combined_snapshot_refuses_partial_labels_and_changed_field_meanings(self):
        original = self.gh.chat_delivery_snapshot
        for mode in ('partial-labels', 'changed-field'):
            def change(*args, **kwargs):
                snapshot = original(*args, **kwargs)
                if mode == 'partial-labels':
                    snapshot['issue']['labels']['pageInfo']['hasNextPage'] = True
                else:
                    snapshot['routing_field']['options'][0]['name'] = 'changed meaning'
                return snapshot
            with self.subTest(mode=mode), patch.object(self.gh, 'chat_delivery_snapshot', side_effect=change), self.assertRaises(Halt):
                self.call()
        self.assertNotIn('delivery_attempts', read_json(self.path))

    def test_recovered_done_is_pending_if_the_issue_reopens_after_merge_readback(self):
        self.reviewed(); self.call(apply=True)
        self.gh.chat_pr = lambda number: self.gh.pr(number)
        def unknown_note(gh, path, record, event, *args, **kwargs):
            event['note'] = 'intent'; write_json(path, record)
            raise Halt('Fixture lost note response')
        with patch.object(chat_tracking, 'sync_pr_attachment'), patch.object(chat_tracking, 'sync_note', side_effect=unknown_note):
            self.assertEqual(chat_tracking.sync(self.gh, self.root, self.task)['state'], 'pending')
        original = chat_merge.merge_observation
        def reopen(*args):
            state = original(*args)
            self.gh.items[0]['state'] = 'OPEN'
            return state
        with patch.object(chat_tracking, 'sync_pr_attachment'), patch.object(chat_tracking, 'sync_note') as note, \
                patch.object(chat_merge, 'merge_observation', side_effect=reopen):
            result = chat_tracking.sync(self.gh, self.root, self.task)
            self.assertEqual(result['state'], 'pending')
            self.assertIn('reopened', result['detail'])
            note.assert_not_called()
        self.assertFalse(read_json(self.path)['events'][-1].get('synced'))

    def test_commit_message_closures_cannot_exceed_the_tracked_issue(self):
        for message in ('Fixes #2', 'Fixes:#0002', 'CLOSES: other/repo#1', 'Resolves https://github.com/example/app/issues/2',
                        'Fixes https://github.com/example/app/pull/2#issuecomment-3'):
            with self.subTest(message=message), self.assertRaisesRegex(Halt, 'Commit-message'):
                chat_merge.commit_closure_scope(message, 'example/app', 1, 'github.com')
        chat_merge.commit_closure_scope('Fixes #1\0Fixes example/app#1\0Fix scanner error\n\nRefs #27\0Fix https://docs.python.org/3/library/re.html\0Resolve parser bug', 'example/app', 1, 'github.com')
        git(self.root, 'commit', '--allow-empty', '-m', 'Fixes #2')
        self.head = git(self.root, 'rev-parse', 'HEAD'); git(self.root, 'push', 'origin', 'topic')
        with self.assertRaisesRegex(Halt, 'Commit-message'):
            self.call()
        self.assertNotIn('delivery_attempts', read_json(self.path))

    def test_a_repository_keeps_its_own_configured_line_limit(self):
        (self.root / 'value.txt').write_text('fixed\n' + 'line\n' * 800)
        git(self.root, 'commit', '-am', 'Large fixture candidate')
        self.head = git(self.root, 'rev-parse', 'HEAD'); git(self.root, 'push', 'origin', 'topic')
        with self.assertRaisesRegex(Halt, 'auto_max_lines'):
            self.call()
        self.c['auto_max_lines'] = 1000  # A different temporary project's explicit policy.
        write_json(self.root / '.sfc/config.json', self.c)
        self.assertEqual(self.call()['state'], 'qualified')

    def test_copied_worktree_grant_cannot_override_revoked_primary_authority(self):
        child = self.root.parent / 'linked'
        git(self.root, 'worktree', 'add', '--detach', child, self.head)
        (child / '.sfc').mkdir()
        shutil.copyfile(self.root / '.sfc/config.json', child / '.sfc/config.json')
        write_json(self.root / '.sfc/config.json', {**self.c, 'merge_policy': 'human', 'chat_merge_authority': None})
        with self.assertRaisesRegex(Halt, 'configuration changed'):
            chat_merge.delivery(self.gh, child, self.task, child, self.head, self.base, processes=self.processes)
        self.assertNotIn('delivery_attempts', read_json(self.path))

    def test_transient_sensitive_history_requires_handoff_even_when_reverted(self):
        (self.root / '.env').write_text('fixture credential file')
        git(self.root, 'add', '.env'); git(self.root, 'commit', '-m', 'Transient sensitive file')
        git(self.root, 'rm', '.env'); git(self.root, 'commit', '-m', 'Remove sensitive file')
        self.head = git(self.root, 'rev-parse', 'HEAD')
        git(self.root, 'push', 'origin', 'topic')
        with self.assertRaisesRegex(Halt, 'sensitive credential file'):
            self.call()
        self.assertNotIn('delivery_attempts', read_json(self.path))

    def test_transient_credential_content_is_checked_in_published_history(self):
        (self.root / 'value.txt').write_text('sk-' + 'x' * 30 + '\n')
        git(self.root, 'commit', '-am', 'Transient credential content')
        (self.root / 'value.txt').write_text('fixed\n')
        git(self.root, 'commit', '-am', 'Remove credential content')
        self.head = git(self.root, 'rev-parse', 'HEAD')
        git(self.root, 'push', 'origin', 'topic')
        with self.assertRaisesRegex(Halt, 'live-looking credential'):
            self.call()
        self.assertNotIn('delivery_attempts', read_json(self.path))

    def test_historical_nonascii_credential_paths_are_not_git_display_strings(self):
        path = self.root / 'café' / 'id_rsa'
        path.parent.mkdir(); path.write_text('fixture credential file')
        git(self.root, 'add', 'café/id_rsa'); git(self.root, 'commit', '-m', 'Transient unicode credential path')
        git(self.root, 'rm', 'café/id_rsa'); git(self.root, 'commit', '-m', 'Remove transient credential path')
        self.head = git(self.root, 'rev-parse', 'HEAD')
        git(self.root, 'push', 'origin', 'topic')
        with self.assertRaisesRegex(Halt, 'sensitive credential file'):
            self.call()
        self.assertNotIn('delivery_attempts', read_json(self.path))

    def test_historical_binary_content_requires_handoff_even_when_removed(self):
        path = self.root / 'payload.bin'; path.write_bytes(b'\x00' + b'fixture binary data')
        git(self.root, 'add', 'payload.bin'); git(self.root, 'commit', '-m', 'Transient binary')
        git(self.root, 'rm', 'payload.bin'); git(self.root, 'commit', '-m', 'Remove transient binary')
        self.head = git(self.root, 'rev-parse', 'HEAD')
        git(self.root, 'push', 'origin', 'topic')
        with self.assertRaisesRegex(Halt, 'Binary changes in published history'):
            self.call()
        self.assertNotIn('delivery_attempts', read_json(self.path))

    def test_completion_guard_preserves_a_manual_routing_change_it_observes(self):
        self.reviewed(); self.call(apply=True)
        self.gh.items[0]['status'] = 'Blocked'
        with self.assertRaisesRegex(Halt, 'routing changed'):
            chat_merge.completion_guard(self.root, read_json(self.path), self.gh)
        self.assertNotIn((1, 'Done'), self.gh.writes)

    def test_live_policy_change_during_final_snapshot_prevents_dispatch(self):
        self.reviewed()
        reads = 0
        def edit(snapshot):
            nonlocal reads
            reads += 1
            if reads == 4:
                self.gh.items[0]['labels'].append('human-only')
        self.gh.edit_snapshot = edit
        with self.assertRaisesRegex(Halt, 'issue policy'):
            self.call(apply=True)
        self.assertEqual(self.gh.attempts, [])

    def test_final_commands_cannot_replace_original_review_or_check_receipts(self):
        self.reviewed()
        proof = read_json(self.path)['delivery_attempts'][0]
        original = self.processes.execute
        def alter(*args, **kwargs):
            code = original(*args, **kwargs)
            Path(proof['checks'][0]['log']).write_text('replaced during final checks')
            return code
        with patch.object(self.processes, 'execute', side_effect=alter), self.assertRaisesRegex(Halt, 'proof changed'):
            self.call(apply=True)
        self.assertNotIn('chat_merge', read_json(self.path))
        self.assertEqual(self.gh.attempts, [])

    def test_atomic_capability_is_required_before_spending_and_before_dispatch(self):
        with patch.object(self.gh, 'local_merge_capabilities', side_effect=Halt('Atomic support unavailable')):
            with self.assertRaisesRegex(Halt, 'Atomic support'):
                self.call()
        self.assertNotIn('delivery_attempts', read_json(self.path))
        self.reviewed()
        with patch.object(self.gh, 'local_merge_capabilities', side_effect=Halt('Atomic support unavailable')):
            with self.assertRaisesRegex(Halt, 'Atomic support'):
                self.call(apply=True)
        self.assertNotIn('chat_merge', read_json(self.path))
        self.assertEqual(self.gh.attempts, [])

    def test_other_closing_issues_require_handoff_before_spending_or_writing(self):
        def extra(snapshot):
            refs = snapshot['pullRequest']['closingIssuesReferences']
            refs['nodes'].append({'id': 'human-only-issue'})
            refs['totalCount'] += 1
        self.gh.edit_snapshot = extra
        with self.assertRaisesRegex(Halt, 'sole closing reference'):
            self.call()
        self.assertNotIn('delivery_attempts', read_json(self.path))
        self.gh.edit_snapshot = None
        self.reviewed()
        self.gh.edit_snapshot = extra
        with self.assertRaisesRegex(Halt, 'sole closing reference'):
            self.call(apply=True)
        self.assertEqual(self.gh.attempts, [])

    def test_deleted_head_after_owned_merge_uses_immutable_pr_and_base_evidence(self):
        self.reviewed(); self.call(apply=True)
        git(self.remote, 'update-ref', '-d', 'refs/heads/topic')
        self.gh.protected = {'id': 'new-rule'}  # No further ref write is needed.
        self.assertEqual(self.call(apply=True)['state'], 'merged')
        self.assertEqual(len(self.gh.attempts), 1)
        self.gh.prs[101]['mergeCommit'] = {'oid': self.base}
        self.assertEqual(self.call(apply=True)['state'], 'uncertain')
        self.assertEqual(len(self.gh.attempts), 1)

    def test_deleted_head_before_dispatch_still_fails_closed(self):
        self.reviewed()
        git(self.remote, 'update-ref', '-d', 'refs/heads/topic')
        with self.assertRaisesRegex(Halt, 'identity'):
            self.call(apply=True)
        self.assertEqual(self.gh.attempts, [])

    def test_external_merge_after_known_unsent_attempt_completes_without_new_execution(self):
        self.reviewed()
        original = chat_merge.write_json
        def stop_before_send(path, record):
            original(path, record)
            if record.get('chat_merge', {}).get('state') == 'dispatching':
                (common_dir(self.root) / 'sfc-stop').touch()
        with patch.object(chat_merge, 'write_json', side_effect=stop_before_send), self.assertRaises(Halt):
            self.call(apply=True)
        saved = read_json(self.path)
        self.assertEqual(saved['chat_merge']['state'], 'not-dispatched')
        # A human can land a different candidate after the definitely-unsent attempt.
        self.gh.prs[101].update(state='MERGED', mergeCommit={'oid': self.base})
        self.gh.chat_pr = lambda number: self.gh.pr(number)
        self.gh.github_status['item-1'] = 'in-progress'
        with patch.object(chat_tracking, 'sync_pr_attachment'), patch.object(chat_tracking, 'sync_note'):
            result = chat_tracking.sync(self.gh, self.root, self.task)
            again = chat_tracking.sync(self.gh, self.root, self.task)
        self.assertEqual((result['state'], result['board_stage'], again['state']), ('synced', 'Done', 'synced'))
        self.assertEqual(self.gh.github_status['item-1'], 'done')
        retained = read_json(self.path)
        self.assertEqual(retained['chat_merge'], saved['chat_merge'])
        self.assertEqual(retained['delivery_attempts'], saved['delivery_attempts'])
        self.assertEqual(self.gh.attempts, [])

    def test_a_second_preparation_on_identical_inputs_keeps_the_selected_original_proof(self):
        normal = self.processes.mutate
        self.processes.mutate = lambda result: result.update(coverage=[])
        with self.assertRaises(Halt):
            self.call()
        self.processes.mutate = normal
        self.reviewed()
        self.call(apply=True)
        self.assertEqual(self.call(apply=True)['state'], 'merged')
        record = read_json(self.path)
        self.assertEqual(len(record['delivery_attempts']), 2)
        self.assertEqual(record['chat_merge']['preparation_id'], record['delivery_attempts'][1]['intent']['id'])

    def test_uncertain_tracking_write_requires_its_own_readback_before_merge(self):
        self.reviewed()
        record = read_json(self.path)
        record['events'][-1].pop('synced')
        write_json(self.path, record)
        with self.assertRaisesRegex(Halt, 'Sync pending'):
            self.call(apply=True)
        self.assertEqual(self.gh.attempts, [])

    def test_checks_native_review_and_both_refs_gate_the_only_merge_write(self):
        self.reviewed()
        proof = read_json(self.path)['delivery_attempts'][0]
        self.assertTrue(review_policy.native_proof(proof, self.root))
        self.assertEqual(self.call(apply=True)['state'], 'awaiting-recognition')
        self.assertEqual(git(self.remote, 'rev-parse', 'main'), self.head)
        self.assertEqual(self.call(apply=True)['state'], 'merged')
        self.assertEqual(len(self.gh.attempts), 1)
        self.assertEqual(self.processes.lanes, ['review'])
        self.assertEqual([event[0] for event in self.processes.events], ['check', 'check'])

    def test_lost_write_response_is_reconciled_without_repeating_any_execution(self):
        self.reviewed(); self.gh.lost_response = True
        with self.assertRaises(Halt):
            self.call(apply=True)
        events = self.processes.events.copy()
        self.assertEqual(self.call(apply=True)['state'], 'merged')
        self.assertEqual(self.processes.events, events)
        self.assertEqual(len(self.gh.attempts), 1)

    def test_applicable_test_merge_checks_take_precedence_over_head_checks(self):
        self.reviewed()
        self.gh.merge_ci = [{'status': 'COMPLETED', 'conclusion': 'FAILURE'}]
        with self.assertRaisesRegex(Halt, 'GitHub checks'):
            self.call(apply=True)
        self.assertNotIn('apply_reserved', read_json(self.path)['delivery_attempts'][0])
        self.gh.ci = [{'status': 'COMPLETED', 'conclusion': 'FAILURE'}]
        self.gh.merge_ci = [{'status': 'COMPLETED', 'conclusion': 'SUCCESS'}]
        self.call(apply=True)
        self.assertEqual(len(self.gh.attempts), 1)

    def test_pr_changed_after_ci_is_revalidated_before_dispatch(self):
        self.reviewed()
        reads = 0
        def edit(snapshot):
            nonlocal reads
            reads += 1
            if reads == 4:  # The final paired response observes the PR returning to draft.
                self.gh.prs[101]['isDraft'] = True
                snapshot['pullRequest']['isDraft'] = True
        self.gh.edit_snapshot = edit
        with self.assertRaisesRegex(Halt, 'ready PR'):
            self.call(apply=True)
        self.assertEqual(self.gh.attempts, [])

    def test_stale_ignored_inputs_cannot_make_the_fresh_final_checkout_pass(self):
        with (common_dir(self.root) / 'info/exclude').open('a') as handle:
            handle.write('\nignored-proof\n')
        (self.root / 'ignored-proof').write_text('stale')
        self.c['verify_commands'] = ["python3 -c \"from pathlib import Path; assert Path('ignored-proof').exists()\""]
        write_json(self.root / '.sfc/config.json', self.c)
        self.reviewed()
        with self.assertRaisesRegex(Halt, 'Final candidate verification failed'):
            self.call(apply=True)
        final = Path(read_json(self.path)['delivery_attempts'][0]['final_worktree'])
        self.assertFalse((final / 'ignored-proof').exists())
        self.assertTrue((self.root / 'ignored-proof').exists())
        self.assertEqual(self.gh.attempts, [])

    def test_closed_issue_recognition_is_required_and_reopening_blocks_completion(self):
        self.reviewed(); self.gh.close_issue = False
        self.call(apply=True)
        self.assertEqual(self.call(apply=True)['state'], 'awaiting-closure')
        self.gh.items[0]['state'] = 'CLOSED'
        self.assertEqual(self.call(apply=True)['state'], 'merged')
        self.gh.items[0]['state'] = 'OPEN'
        with self.assertRaisesRegex(Halt, 'reopened'):
            chat_merge.completion_guard(self.root, read_json(self.path), self.gh)
        self.assertEqual(len(self.gh.attempts), 1)

    def test_nondefault_bases_and_missing_native_review_are_explicit_before_spending(self):
        self.gh.default_branch = 'release'
        self.assertFalse(chat_merge.support(self.gh, self.c)['available'])
        with self.assertRaisesRegex(Halt, 'default branch'):
            self.call()
        self.gh.default_branch = 'main'
        with patch.object(review_policy, 'native_command', side_effect=Halt('Native unavailable')):
            self.assertFalse(chat_merge.support(self.gh, self.c)['available'])
            with self.assertRaisesRegex(Halt, 'Native unavailable'):
                self.call()
        self.assertNotIn('delivery_attempts', read_json(self.path))
        self.assertEqual(self.processes.events, [])

    def test_scope_changed_during_checks_does_not_dispatch_a_stale_native_review(self):
        original = self.processes.execute
        def change(argv, *args, **kwargs):
            code = original(argv, *args, **kwargs)
            self.gh.default_branch = 'release'
            return code
        with patch.object(self.processes, 'execute', side_effect=change), self.assertRaisesRegex(Halt, 'default branch'):
            self.call()
        self.assertEqual(self.processes.lanes, [])
        self.assertEqual(len(read_json(self.path)['delivery_attempts']), 1)

    def test_known_predispatch_abort_can_use_remaining_allowance_without_refunding(self):
        self.reviewed()
        original = chat_merge.write_json
        def stop_before_send(path, record):
            original(path, record)
            if record.get('chat_merge', {}).get('state') == 'dispatching':
                (common_dir(self.root) / 'sfc-stop').touch()
        with patch.object(chat_merge, 'write_json', side_effect=stop_before_send), self.assertRaises(Halt):
            self.call(apply=True)
        self.assertEqual(read_json(self.path)['chat_merge']['state'], 'not-dispatched')
        self.assertEqual(self.gh.attempts, [])
        (common_dir(self.root) / 'sfc-stop').unlink()  # Explicit test operator re-arm.
        self.reviewed(); self.call(apply=True)
        record = read_json(self.path)
        self.assertEqual(len(record['delivery_attempts']), 2)
        self.assertEqual(record['undispatched_merges'][0]['state'], 'not-dispatched')
        self.assertEqual(len(self.gh.attempts), 1)

    def test_a_legacy_or_unknown_intent_never_becomes_a_new_write(self):
        self.reviewed()
        record = read_json(self.path)
        record['chat_merge'] = {'state': 'intent', 'head': self.head, 'base': self.base}
        write_json(self.path, record)
        with self.assertRaisesRegex(Halt, 'only readback'):
            self.call()
        self.assertEqual(self.call(apply=True)['state'], 'uncertain')
        self.assertEqual(len(read_json(self.path)['delivery_attempts']), 1)
        self.assertEqual(self.gh.attempts, [])

    def test_final_ci_change_or_stop_at_readback_prevents_dispatch(self):
        self.reviewed()
        original = self.gh.chat_delivery_snapshot
        reads = 0
        def changed(*args, **kwargs):
            nonlocal reads
            reads += 1
            if reads == 4:
                self.gh.ci = [{'status': 'COMPLETED', 'conclusion': 'FAILURE'}]
            return original(*args, **kwargs)
        with patch.object(self.gh, 'chat_delivery_snapshot', side_effect=changed), self.assertRaisesRegex(Halt, 'GitHub checks'):
            self.call(apply=True)
        self.assertEqual(self.gh.attempts, [])
        self.assertNotIn('chat_merge', read_json(self.path))
        self.gh.ci = [{'status': 'COMPLETED', 'conclusion': 'SUCCESS'}]
        self.reviewed(); self.call(apply=True)
        self.assertEqual(len(read_json(self.path)['delivery_attempts']), 2)
        self.assertEqual(len(self.gh.attempts), 1)

    def test_stop_during_final_github_read_keeps_intent_without_dispatch(self):
        self.reviewed()
        original = self.gh.chat_delivery_snapshot
        reads = 0
        def stop(*args, **kwargs):
            nonlocal reads
            snapshot = original(*args, **kwargs); reads += 1
            if reads == 4:
                (common_dir(self.root) / 'sfc-stop').touch()
            return snapshot
        with patch.object(self.gh, 'chat_delivery_snapshot', side_effect=stop), self.assertRaisesRegex(Halt, 'stopped'):
            self.call(apply=True)
        self.assertEqual(self.gh.attempts, [])
        self.assertNotIn('chat_merge', read_json(self.path))

    def test_live_issue_criteria_are_required_even_when_the_chat_saved_a_subset(self):
        self.gh.items[0]['body'] += '- A newly added live obligation\n'
        self.assertEqual(self.call()['state'], 'qualified')
        coverage = read_json(self.path)['delivery_attempts'][0]['result']['coverage']
        self.assertEqual([row['criterion'] for row in coverage], ['The value is fixed', 'A newly added live obligation'])

    def test_merge_readback_accepts_a_verified_descendant_without_another_write(self):
        self.reviewed(); self.call(apply=True)
        descendant = git(self.root, 'commit-tree', self.head + '^{tree}', '-p', self.head, '-m', 'Next change')
        git(self.root, 'push', 'origin', descendant + ':refs/heads/next')
        git(self.remote, 'update-ref', 'refs/heads/main', descendant)
        self.assertEqual(self.call(apply=True)['state'], 'merged')
        self.assertEqual(len(self.gh.attempts), 1)

    def test_sync_cannot_complete_a_different_merge_after_an_uncertain_owned_intent(self):
        self.reviewed()
        self.gh.before_write = lambda intent: (_ for _ in ()).throw(Halt('Fixture rejected write'))
        with self.assertRaises(Halt):
            self.call(apply=True)
        self.gh.prs[101].update(state='MERGED', mergeCommit={'oid': self.base})
        self.gh.chat_pr = lambda number: self.gh.pr(number)
        with patch.object(chat_tracking, 'sync_pr_attachment'):
            result = chat_tracking.sync(self.gh, self.root, self.task)
        self.assertEqual(result['state'], 'pending')
        self.assertEqual(self.gh.items[0]['status'], 'Review')
        self.assertNotIn((1, 'Done'), self.gh.writes)

    def test_stop_preserves_a_landed_intent_without_publishing_done(self):
        self.reviewed(); self.call(apply=True)
        (common_dir(self.root) / 'sfc-stop').touch()
        self.gh.chat_pr = lambda number: self.gh.pr(number)
        with patch.object(chat_tracking, 'sync_pr_attachment'):
            result = chat_tracking.sync(self.gh, self.root, self.task)
        self.assertEqual(result['state'], 'pending')
        self.assertEqual(self.gh.items[0]['status'], 'Review')
        self.assertNotIn((1, 'Done'), self.gh.writes)

    def test_changed_base_in_the_final_race_latches_intent_without_a_second_write(self):
        self.reviewed()
        self.gh.before_write = lambda intent: git(self.remote, 'update-ref', 'refs/heads/main', self.head)
        with self.assertRaises(Halt):
            self.call(apply=True)
        self.assertEqual(self.call(apply=True)['state'], 'uncertain')
        self.assertEqual(len(self.gh.attempts), 1)

    def test_replaced_repository_cannot_use_the_old_grant(self):
        self.gh.repository_id = 'replacement'
        with self.assertRaisesRegex(Halt, 'repository identity'):
            self.call()
        self.assertEqual(self.gh.attempts, [])

    def test_head_movement_in_the_final_race_cannot_merge_unreviewed_code(self):
        self.reviewed()
        replacement = git(self.root, 'commit-tree', self.head + '^{tree}', '-p', self.head, '-m', 'Concurrent head')
        git(self.root, 'push', 'origin', replacement + ':refs/heads/replacement')
        self.gh.before_write = lambda intent: git(self.remote, 'update-ref', intent['head_ref'], replacement)
        with self.assertRaises(Halt):
            self.call(apply=True)
        self.assertEqual(git(self.remote, 'rev-parse', 'main'), self.base)
        self.assertEqual(self.call(apply=True)['state'], 'uncertain')
        self.assertEqual(len(self.gh.attempts), 1)

    def test_failed_final_checks_are_retained_and_cannot_be_retried_for_free(self):
        self.reviewed()
        def failed(argv, cwd, log, timeout, *args, **kwargs):
            Path(log).write_text('Observed final check failure')
            return 1
        with patch.object(self.processes, 'execute', side_effect=failed), self.assertRaises(Halt):
            self.call(apply=True)
        with self.assertRaisesRegex(Halt, 'already spent'):
            self.call(apply=True)
        proof = read_json(self.path)['delivery_attempts'][0]
        self.assertEqual(proof['final_checks'][0]['exit_code'], 1)
        self.assertEqual(self.gh.attempts, [])

    def test_native_findings_or_missing_coverage_never_qualify(self):
        self.processes.mutate = lambda result: result.update(coverage=[])
        with self.assertRaisesRegex(Halt, 'missing coverage'):
            self.call()
        self.assertEqual(read_json(self.path)['delivery_attempts'][0]['state'], 'reserved')
        self.assertEqual(self.gh.attempts, [])

    def test_environment_task_and_config_changes_invalidate_preparation(self):
        self.reviewed()
        with patch.dict('os.environ', {'SFC_DELIVERY_TEST_INPUT': 'changed'}), self.assertRaises(Halt):
            self.call(apply=True)
        self.gh.items[0]['body'] += '\nAdditional acceptance obligation\n'
        with self.assertRaises(Halt):
            self.call(apply=True)
        self.assertEqual(self.gh.attempts, [])

    def test_unrelated_clone_is_not_the_authorized_project_worktree(self):
        clone = self.root.parent / 'unrelated'
        git(self.root, 'clone', '--branch', 'topic', self.remote, clone)
        with self.assertRaisesRegex(Halt, 'different repository'):
            chat_merge.delivery(self.gh, self.root, self.task, clone, self.head, self.base, processes=self.processes)
        self.assertNotIn('delivery_attempts', read_json(self.path))

    def test_policy_labels_size_missing_checks_and_protection_preserve_handoffs(self):
        self.reviewed()
        for change, undo in (
                (lambda: self.gh.items[0]['labels'].append('human-only'), lambda: self.gh.items[0]['labels'].remove('human-only')),
                (lambda: self.gh.prs[101].update(additions=801), lambda: self.gh.prs[101].update(additions=1)),
                (lambda: setattr(self.gh, 'ci', []), lambda: setattr(self.gh, 'ci', [{'status': 'COMPLETED', 'conclusion': 'SUCCESS'}])),
                (lambda: setattr(self.gh, 'rules', 1), lambda: setattr(self.gh, 'rules', 0))):
            change()
            with self.assertRaises(Halt):
                self.call(apply=True)
            undo()
        self.assertEqual(self.gh.attempts, [])
        self.assertEqual(self.c['auto_max_lines'], 800)

    def test_tampered_logs_and_changed_task_do_not_reuse_review(self):
        self.reviewed()
        proof = read_json(self.path)['delivery_attempts'][0]
        Path(proof['checks'][0]['log']).write_text('replacement')
        with self.assertRaises(Halt):
            self.call(apply=True)
        self.assertEqual(self.gh.attempts, [])

    def test_interrupted_preparation_spends_its_allowance_and_cannot_reset(self):
        self.processes.interrupt = True
        for _ in range(2):
            with self.assertRaises(Stopped):
                self.call()
        with self.assertRaisesRegex(Halt, 'allowance exhausted'):
            self.call()
        self.assertEqual(len(read_json(self.path)['delivery_attempts']), 2)
        self.c['max_attempts'] = 9
        write_json(self.root / '.sfc/config.json', self.c)
        with self.assertRaisesRegex(Halt, 'allowance exhausted'):
            self.call()
        self.assertEqual(len(read_json(self.path)['delivery_attempts']), 2)

    def test_existing_supervisor_excludes_duplicate_preparation(self):
        with exclusive(self.root), self.assertRaises(Halt):
            self.call()
        self.assertNotIn('delivery_attempts', read_json(self.path))

    def test_unavailable_support_is_explicit_without_protection_changes(self):
        self.assertTrue(chat_merge.support(self.gh, self.c)['available'])
        self.gh.rules = 1
        self.assertFalse(chat_merge.support(self.gh, self.c)['available'])
        self.assertEqual(self.gh.rules, 1)
        missing = chat_merge.support(self.gh, {**self.c, 'verify_commands': []})
        self.assertFalse(missing['available'])
        self.assertIn('--verify', missing['reason'])
