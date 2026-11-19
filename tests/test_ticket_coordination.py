"""Offline concurrent creators and pickup: real git/worktrees, fake transports."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import sfc
from sfc_lib.common import Halt, exclusive, git, read_json, write_json
from sfc_lib.factory import Factory
from sfc_lib.priority import create_ticket
from sfc_lib.ticket_coordination import creation_lock, readiness, ticket_directory
import test_priority as priority_fixtures
from test_factory import BASE_CONFIG, FakeGitHub


class CoordinationTests(unittest.TestCase):
    setUp = priority_fixtures.PriorityTests.setUp
    create = priority_fixtures.PriorityTests.create

    def linked(self):
        (self.root / 'tracked').write_text('fixture')
        git(self.root, 'add', 'tracked')
        git(self.root, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.test', 'commit', '-m', 'fixture')
        linked = self.root / 'linked'
        git(self.root, 'worktree', 'add', '-b', 'linked', linked)
        return linked

    def card(self):
        content = copy.deepcopy(self.gh.items['item-1']['content'])
        return {**content, 'item_id': 'item-1', 'status': 'Backlog', 'priority': 'P2', 'labels': ['enhancement']}

    def factory(self, root=None):
        gh = FakeGitHub(root or self.root, [self.card()])
        gh.metadata = self.gh.metadata
        return Factory(root or self.root, copy.deepcopy(BASE_CONFIG), gh)

    def assert_waiting(self, factory):
        for backlog in (False, True):
            factory.include_backlog, factory.issue_scope = backlog, (1,)
            cards = factory.gh.cards()
            cards[0]['status'] = 'Ready'  # Even manual promotion cannot bypass readiness.
            selected, waits = factory.plan(cards)
            self.assertEqual(selected, [])
            self.assertIn('Ticket creation unverified', str(waits))
            factory.process(cards[0])
            factory.reconcile(cards)
        self.assertEqual(factory.gh.writes, [])
        self.assertEqual(factory.gh.activity_writes, [])
        self.assertEqual(factory.gh.notes, [])
        self.assertEqual(factory.state['cards'], {})

    def cli_args(self, root=None, request='finding-1'):
        root = root or self.root
        write_json(root / '.sfc/config.json', self.gh.c)
        body = root / 'body.md'
        body.write_text(self.args['body'])
        return ['--root', str(root), 'create-ticket', '--request-id', request,
                '--title', self.args['title'], '--body-file', str(body), '--priority', 'P2',
                '--type', 'Feature', '--reason', self.args['reason'], '--unknowns', self.args['unknowns']]

    def test_script_entrypoint_creates_with_supervisor_lock_held(self):
        argv = self.cli_args()
        # Execute the actual script __main__ in another OS process; only GH is fake.
        scripts = Path(sfc.__file__).parent
        tests = Path(__file__).parent
        harness = '''import runpy, sys
sys.path[:0] = sys.argv[1:3]
script, args = sys.argv[3], sys.argv[4:]
from test_priority import TicketGitHub
import sfc_lib.github
sfc_lib.github.GitHub = lambda root, config: TicketGitHub(root)
sys.argv = [script, *args]
runpy.run_path(script, run_name="__main__")
'''
        with exclusive(self.root):
            result = subprocess.run([sys.executable, '-c', harness, str(scripts), str(tests),
                                     str(scripts / 'sfc.py'), *argv], capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(json.loads(result.stdout)['complete'])
            with self.assertRaisesRegex(Halt, 'Another SFC supervisor'):
                with exclusive(self.root):
                    pass
        receipt = read_json(ticket_directory(self.root) / 'finding-1.json')
        self.assertEqual(receipt['state'], 'complete')

    def test_verified_creation_with_marker_prefix_in_prose_is_eligible(self):
        result = self.create(body='## Acceptance criteria\n- Document the `sfc:ticket:` request marker.')
        self.assertTrue(result['complete'])
        self.assertIsNone(readiness(self.root, self.card(), 'example/app', 'project-id'))
        factory = self.factory()
        factory.include_backlog, factory.issue_scope = True, (1,)
        cards = factory.gh.cards()
        cards[0]['createdAt'] = '2026-01-01T00:00:00Z'
        selected, waits = factory.plan(cards)
        self.assertEqual([card['number'] for card in selected], [1])
        self.assertFalse(waits)

    def test_ordinary_issue_can_discuss_marker_prefix_without_creation_receipt(self):
        card = {'number': 1, 'body': '## Acceptance criteria\n- Document the `sfc:ticket:` request marker.'}
        self.assertIsNone(readiness(self.root, card, 'example/app', 'project-id'))

    def test_malformed_marker_comments_remain_ineligible(self):
        self.create()
        card = self.card()
        for marker in ('<!-- sfc:ticket: -->', '<!-- sfc:ticket:bad/id -->',
                       '<!-- sfc:ticket:finding-1', '<!--sfc:ticket:finding-1-->'):
            with self.subTest(marker=marker):
                malformed = card | {'body': '## Acceptance criteria\n- Check marker\n' + marker}
                self.assertIn('malformed/ambiguous', readiness(self.root, malformed, 'example/app', 'project-id'))
                duplicate = card | {'body': card['body'] + '\n' + marker}
                self.assertIn('malformed/ambiguous', readiness(self.root, duplicate, 'example/app', 'project-id'))

    def test_simultaneous_linked_creators_and_repeated_ids_preserve_receipt(self):
        linked = self.linked()
        entered, release = threading.Event(), threading.Event()
        errors = []
        def pause(gh):
            if gh.snapshots == 3:  # Both remote fields set; final readback not complete.
                entered.set()
                if not release.wait(10):
                    raise RuntimeError('test synchronization timed out')
        self.gh.on_snapshot = pause
        def creator():
            try:
                self.create()
            except BaseException as exc:
                errors.append(exc)
        thread = threading.Thread(target=creator)
        thread.start()
        try:
            self.assertTrue(entered.wait(10))
            path = ticket_directory(linked) / 'finding-1.json'
            before = path.read_bytes()
            for request in ('finding-1', 'another-request'):
                with self.assertRaisesRegex(Halt, 'busy'):
                    create_ticket(self.gh, linked, **(self.args | {'request_id': request}))
            self.assertEqual(path.read_bytes(), before)
            self.assert_waiting(self.factory(linked))
            self.assertFalse((linked / '.sfc/state.json').exists())
        finally:
            release.set()
            thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        before = path.read_bytes()
        with self.assertRaisesRegex(Halt, 'already recorded'):
            create_ticket(self.gh, linked, **self.args)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.gh.writes.count('create'), 1)
        self.assertIsNone(readiness(linked, self.card(), 'example/app', 'project-id'))
        self.create(request_id='another-request')
        self.assertEqual(len(list(ticket_directory(self.root).glob('*.json'))), 2)
        self.assertEqual(path.read_bytes(), before)

    def test_partial_lost_response_blocks_pickup_after_fields_are_remote(self):
        self.gh.fail = 'after-priority-field'
        with self.assertRaisesRegex(Halt, 'Incomplete ticket'):
            self.create()
        self.assertEqual(self.gh.items['item-1']['values']['priority-field'], 'priority-P2')
        self.assert_waiting(self.factory())
        receipt = ticket_directory(self.root) / 'finding-1.json'
        before = receipt.read_bytes()
        with self.assertRaisesRegex(Halt, 'already recorded'):
            self.create()
        self.assertEqual(receipt.read_bytes(), before)

    def test_incomplete_creation_blocks_direct_claim_and_saved_pr_reconciliation(self):
        self.gh.fail = 'after-priority-field'
        with self.assertRaises(Halt):
            self.create()
        f = self.factory()
        f.include_backlog, f.issue_scope = True, (1,)
        f.run_dir = self.root / '.sfc/runs/fixture'
        f.state['outcome'] = 'running'
        self.assertFalse(f.claim_priority(self.card()))
        self.assertEqual(f.state['cards'], {})
        f.state['cards']['1'] = {'pr': 101, 'activity': {'state': 'Running'}}
        with patch.object(f.gh, 'pr', side_effect=AssertionError('must not reconcile unverified creation')):
            f.reconcile([self.card() | {'status': 'Building'}])
        self.assertEqual(f.gh.writes + f.gh.activity_writes, [])

    def test_abrupt_process_exit_leaves_intent_and_no_eligible_ticket(self):
        # Simulate death rather than an exception handled by creation recovery.
        pid = os.fork()
        if pid == 0:
            def crash(gh):
                if gh.snapshots == 3:
                    os._exit(23)
            self.gh.on_snapshot = crash
            self.create()
            os._exit(24)
        _, status = os.waitpid(pid, 0)
        self.assertEqual(os.waitstatus_to_exitcode(status), 23)
        path = ticket_directory(self.root) / 'finding-1.json'
        receipt = read_json(path)
        self.assertEqual(receipt['state'], 'setting_field')
        # Reconstruct remote identity from saved pre-write snapshot; remote fields
        # might both be set, but neither values nor released flock prove readiness.
        c = {**receipt['before']['content'], 'item_id': receipt['item_id'],
             'status': 'Ready', 'priority': 'P2', 'labels': ['enhancement']}
        gh = FakeGitHub(self.root, [c])
        gh.metadata = self.gh.metadata
        f = Factory(self.root, copy.deepcopy(BASE_CONFIG), gh)
        selected, waits = f.plan(gh.cards())
        self.assertFalse(selected)
        self.assertIn(str(path), str(waits))
        with creation_lock(self.root):
            pass  # OS released the lock on death, but intent still latches.
        with self.assertRaisesRegex(Halt, 'already recorded'):
            self.create()

    def test_readiness_missing_conflicting_corrupt_and_mismatched_evidence(self):
        self.create()
        path = ticket_directory(self.root) / 'finding-1.json'
        good = read_json(path)
        for change in ({'state': 'incomplete'}, {'project': 'other'}, {'item_id': 'other'},
                       {'issue': {'number': 99}}, {'verified': {}}):
            write_json(path, good | change)
            self.assert_waiting(self.factory())
        path.unlink()
        self.assert_waiting(self.factory())
        path.write_text('{')
        self.assert_waiting(self.factory())
        write_json(path, good)
        legacy = self.root / '.sfc/tickets/finding-1.json'
        write_json(legacy, good)
        self.assert_waiting(self.factory())
        legacy.unlink()
        card = self.card()
        card['body'] += '\n<!-- sfc:ticket:second -->'
        self.assertIn('ambiguous', readiness(self.root, card, 'example/app', 'project-id'))
        # Identity still catches a known partial even if its marker is removed.
        write_json(path, good | {'state': 'incomplete'})
        card['body'] = '## Acceptance criteria\n- Manual edit'
        self.assertIn('unverified', readiness(self.root, card, 'example/app', 'project-id'))

    def test_legacy_linked_receipt_prevents_new_post_without_migration(self):
        linked = self.linked()
        self.create()
        path = ticket_directory(self.root) / 'finding-1.json'
        legacy = self.root / '.sfc/tickets/finding-1.json'
        legacy.parent.mkdir(parents=True)
        path.rename(legacy)
        before = legacy.read_bytes()
        with self.assertRaisesRegex(Halt, 'already recorded'):
            create_ticket(self.gh, linked, **self.args)
        self.assertEqual(legacy.read_bytes(), before)
        self.assertIsNone(readiness(linked, self.card(), 'example/app', 'project-id'))
        self.assertEqual(self.gh.writes.count('create'), 1)

    def test_setup_and_onboarding_cli_cannot_write_during_creation(self):
        linked = self.linked()
        write_json(linked / '.sfc/config.json', self.gh.c)
        commands = [['priority-setup', '--apply'], ['type-setup', '--apply'], ['dashboard', '--apply'],
                    ['onboard', '--owner', 'example', '--project', '1', '--base', 'main', '--verify', 'true']]
        with creation_lock(self.root), patch.object(sfc, 'GitHub', return_value=self.gh), \
                contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            for command in commands:
                self.assertEqual(sfc.main(['--root', str(linked), *command]), 1)
        self.assertEqual(self.gh.writes + self.gh.mutations, [])
        self.assertFalse((linked / '.sfc/state.json').exists())

    def test_creator_cannot_enter_setup_lock_and_supervisor_lock_stays_exclusive(self):
        linked = self.linked()
        with exclusive(self.root), creation_lock(self.root):
            with self.assertRaisesRegex(Halt, 'busy'):
                create_ticket(self.gh, linked, **self.args)
            with self.assertRaisesRegex(Halt, 'Another SFC supervisor'):
                with exclusive(linked):
                    pass
        self.assertEqual(self.gh.writes, [])
