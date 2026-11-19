"""Ordinary run recognition uses temporary git repos and offline adapters only."""
import copy
import json
import unittest
from unittest.mock import patch

import test_local_merge as fixtures
from sfc_lib.common import Halt, Stopped, write_json
from sfc_lib.factory import Factory


class OrdinaryRecognitionTests(unittest.TestCase):
    # Inherit the temporary fixture and scoped run helper, not its test cases.
    def setUp(self):
        fixtures.PriorityLocalMergeTests.setUp(self)
        self.clock = 0.0
        self.on_wait = None
        self.frozen_events = None
        for name, value in (('monotonic', lambda: self.clock), ('sleep', self.wait)):
            timer = patch('sfc_lib.local_merge.' + name, value)
            timer.start()
            self.addCleanup(timer.stop)

    def wait(self, seconds):
        self.assertTrue(0 < seconds <= 0.1)
        saved = json.loads((self.root / '.sfc/state.json').read_text())['cards']['1']['local_merge']
        self.assertGreater(saved['confirmation_reads'], 0)  # Durable before observation.
        events = copy.deepcopy(self.proc.events)
        if self.frozen_events is None:
            self.frozen_events = events
        self.assertEqual(events, self.frozen_events)  # No worker/check reruns while waiting.
        self.clock += seconds
        if self.on_wait:
            action, self.on_wait = self.on_wait, None
            action()

    def recognize(self):
        self.gh.prs[101]['state'] = 'MERGED'
        self.gh.items[0]['state'] = 'CLOSED'

    def restart(self):
        self.proc.stop_path.unlink(missing_ok=True)
        self.factory = Factory(self.root, self.c, self.gh, self.proc)

    tearDown = fixtures.PriorityLocalMergeTests.tearDown
    run_pickup = fixtures.PriorityLocalMergeTests.run_pickup

    def test_delayed_recognition_and_closure_finish_in_same_scoped_run(self):
        self.gh.recognize = self.gh.close_issue = False
        reads = []
        def delayed(snapshot):
            if self.gh.applied:
                reads.append(snapshot)
                if len(reads) >= 2:
                    self.gh.prs[101]['state'] = 'MERGED'
                    snapshot['pullRequest']['state'] = 'MERGED'
                if len(reads) >= 3:
                    self.gh.items[0]['state'] = 'CLOSED'
                    snapshot['issue']['state'] = 'CLOSED'
        self.gh.edit_snapshot = delayed
        self.run_pickup()
        record = self.factory.record(self.gh.items[0])
        self.assertEqual(record['local_merge']['phase'], 'done')
        self.assertEqual(record['local_merge']['confirmation_reads'], 2)
        self.assertEqual(record['activity']['state'], 'Completed')
        self.assertEqual(len(self.gh.attempts), 1)
        self.assertEqual(self.proc.lanes, ['build', 'qa', 'security', 'review'])
        self.assertEqual([e[0] for e in self.proc.events].count('check'), 2)

    def test_default_ready_run_observes_delayed_recognition_and_closure(self):
        self.gh.items = self.gh.items[:1]
        self.gh.items[0]['status'] = 'Ready'
        self.gh.move = fixtures.FakeGitHub.move.__get__(self.gh)
        self.gh.recognize = self.gh.close_issue = False
        self.on_wait = self.recognize
        self.factory.run(allow_merge=True)
        record = self.factory.record(self.gh.items[0])
        self.assertEqual(record['local_merge']['confirmation_reads'], 1)
        self.assertEqual(record['activity']['state'], 'Completed')
        self.assertEqual(len(self.gh.attempts), 1)

    def test_exhaustion_and_restart_preserve_spending_and_never_repeat_writes(self):
        self.gh.recognize = self.gh.close_issue = False
        with self.assertRaisesRegex(Halt, 'not confirmed'):
            self.run_pickup()
        record = self.factory.record(self.gh.items[0])
        self.assertEqual(record['local_merge']['confirmation_reads'], 5)
        self.assertAlmostEqual(self.clock, 5)
        writes, events = copy.deepcopy(self.gh.writes), copy.deepcopy(self.proc.events)
        self.restart()
        with self.assertRaisesRegex(Halt, 'not confirmed'):
            self.run_pickup()
        self.assertAlmostEqual(self.clock, 5)
        self.assertEqual(self.gh.writes, writes)
        self.recognize()
        self.restart()
        self.run_pickup()
        self.assertEqual(self.proc.events, events)
        self.assertEqual(len(self.gh.attempts), 1)
        self.assertEqual(self.factory.record(self.gh.items[0])['local_merge']['confirmation_reads'], 5)
        self.assertEqual(self.gh.writes.count((1, 'Building')), 1)
        self.assertEqual(self.gh.writes.count((1, 'Done')), 1)

    def test_stop_spending_survives_restart_and_scope_or_authority_drift_halts(self):
        self.gh.recognize = self.gh.close_issue = False
        self.on_wait = self.proc.stop_path.touch
        with self.assertRaises(Stopped):
            self.run_pickup()
        self.assertEqual(self.factory.record(self.gh.items[0])['local_merge']['confirmation_reads'], 1)
        self.restart()
        with self.assertRaisesRegex(Halt, 'outside explicit run issue scope'):
            self.run_pickup(issues=(15,))
        with self.assertRaisesRegex(Halt, 'authority'):
            self.run_pickup(allow_merge=False)
        self.recognize()
        self.restart()
        self.run_pickup()
        self.assertEqual(len(self.gh.attempts), 1)
        self.assertEqual(self.factory.record(self.gh.items[0])['local_merge']['confirmation_reads'], 1)

    def test_task_identity_stage_rules_config_and_scope_drift_during_wait_halt(self):
        for drift in ('task', 'identity', 'stage', 'rules', 'config', 'scope', 'default-scope', 'authority', 'head', 'evidence'):
            with self.subTest(drift=drift):
                if drift != 'task':
                    self.tearDown()
                    self.setUp()
                self.gh.recognize = self.gh.close_issue = False
                if drift == 'default-scope':
                    self.gh.items = self.gh.items[:1]
                    self.gh.items[0]['status'] = 'Ready'
                    self.gh.move = fixtures.FakeGitHub.move.__get__(self.gh)
                write_json(self.root / '.sfc/config.json', self.c)
                def change():
                    self.recognize()
                    if drift == 'task': self.gh.items[0]['body'] += '\nChanged'
                    if drift == 'identity': self.gh.items[0]['id'] = 'other'
                    if drift == 'stage': self.gh.items[0]['status'] = 'Blocked'
                    if drift == 'rules': self.gh.rules = 1
                    if drift == 'authority': self.factory.allow_merge = False
                    if drift == 'head': self.factory.record(self.gh.items[0])['head'] = 'a' * 40
                    if drift == 'evidence': self.factory.record(self.gh.items[0])['receipts'] = {}
                    if drift == 'config': write_json(self.root / '.sfc/config.json', {**self.c, 'max_workers': 1})
                    if drift in ('scope', 'default-scope'): self.gh.fields[0]['options'][0]['id'] = 'other'
                self.on_wait = change
                with self.assertRaises(Halt):
                    if drift == 'default-scope': self.factory.run(allow_merge=True)
                    else: self.run_pickup()
                self.assertNotIn((1, 'Done'), self.gh.writes)
                self.assertEqual(len(self.gh.attempts), 1)

    def test_unsafe_snapshots_and_lost_responses_never_wait_or_retry(self):
        for drift in ('old-base', 'partial', 'head-tree', 'base-tree', 'head-oid', 'wrong-merge', 'lost'):
            with self.subTest(drift=drift):
                if drift != 'old-base':
                    self.tearDown()
                    self.setUp()
                self.gh.close_issue = False
                if drift == 'base-tree': self.gh.close_issue = True
                self.gh.lost_response = drift == 'lost'
                def unsafe(snapshot):
                    if not self.gh.applied:
                        return
                    if drift == 'old-base': snapshot['base']['target']['oid'] = self.gh.applied[0]['base']
                    if drift == 'partial': snapshot['head'].pop('rules')
                    if drift == 'head-tree': snapshot['head']['target']['tree']['oid'] = 'a' * 40
                    if drift == 'base-tree': snapshot['base']['target']['tree']['oid'] = 'a' * 40
                    if drift == 'head-oid': snapshot['head']['target']['oid'] = 'a' * 40
                    if drift == 'wrong-merge': snapshot['pullRequest']['mergeCommit']['oid'] = 'a' * 40
                self.gh.edit_snapshot = unsafe
                with self.assertRaises(Halt):
                    self.run_pickup()
                self.restart()
                with self.assertRaises(Halt):
                    self.run_pickup()
                self.assertEqual(self.clock, 0)
                self.assertEqual(len(self.gh.attempts), 1)
                self.assertNotIn((1, 'Done'), self.gh.writes)

    def test_uncertain_done_and_completed_writes_are_never_retried(self):
        for action in ('Done', 'Completed'):
            with self.subTest(action=action):
                if action != 'Done':
                    self.tearDown()
                    self.setUp()
                self.gh.close_issue = False
                self.on_wait = self.recognize
                target = 'move' if action == 'Done' else 'write_activity'
                original = getattr(self.gh, target)
                calls = []
                def lost(card, *args):
                    if (action == 'Done' and args[0] == 'Done' or
                            action == 'Completed' and 'Completed' in args[-1][0]):
                        calls.append(action)
                        raise Halt('lost completion response')
                    return original(card, *args)
                with patch.object(self.gh, target, side_effect=lost):
                    with self.assertRaises(Halt):
                        self.run_pickup()
                    self.restart()
                    with self.assertRaises(Halt):
                        self.run_pickup()
                self.assertEqual(calls, [action])
                self.assertEqual(len(self.gh.attempts), 1)
                self.assertNotEqual(self.factory.record(self.gh.items[0])['local_merge']['phase'], 'done')
