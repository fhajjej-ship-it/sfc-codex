"""Chat merge contract: real temporary Git, fake GitHub and native Codex only."""
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import test_factory as fixtures
from test_local_merge import AtomicGitHub
from test_pickup import ticket
from test_review_policy import PolicyProcesses, capabilities
from sfc_lib import chat_merge, chat_tracking, pickup, review_policy
from sfc_lib.common import Halt, Stopped, common_dir, exclusive, git, read_json, run, write_json


class ChatGitHub(AtomicGitHub):
    repository_id = 'repo-node'

    def onboarding_identity(self):
        return {'host': 'github.com', 'repository_id': self.repository_id,
                'repository': self.c['repo'], 'project_id': 'p'}

    def graphql(self, query, variables):
        if 'ref(qualifiedName:$base)' in query:
            return {'repository': {'ref': {'branchProtectionRule': self.protected, 'rules': {'totalCount': self.rules}}}}
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
        for context in (patch('sfc_lib.review_policy.run', side_effect=capabilities),
                        patch('sfc_lib.review_policy.native_sessions', return_value=self.root / '.sfc/sessions')):
            context.start(); self.addCleanup(context.stop)

    def call(self, apply=False):
        return chat_merge.delivery(self.gh, self.root, self.task, self.root, self.head, self.base,
                                   apply=apply, processes=self.processes)

    def reviewed(self):
        self.assertEqual(self.call()['state'], 'qualified')
        chat_tracking.update(self.root, self.c, task=self.task, event_id='review', stage='Review', summary='Independent pass')
        self.gh.items[0]['status'] = 'Review'

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

    def test_existing_supervisor_excludes_duplicate_preparation(self):
        with exclusive(self.root), self.assertRaises(Halt):
            self.call()
        self.assertNotIn('delivery_attempts', read_json(self.path))

    def test_unavailable_support_is_explicit_without_protection_changes(self):
        self.assertTrue(chat_merge.support(self.gh, self.c)['available'])
        self.gh.rules = 1
        self.assertFalse(chat_merge.support(self.gh, self.c)['available'])
        self.assertEqual(self.gh.rules, 1)
