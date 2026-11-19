"""Regression coverage for selected watcher recovery reads and summary writes."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from sfc_lib import blocked_recovery as recovery, pickup
from sfc_lib.common import Stopped, read_json
from sfc_lib.factory import Factory
from sfc_lib.rate_limit import ConnectionReset, ReadConnectionReset
from sfc_lib.watch import Watch
import test_blocked_recovery as fixtures
from test_pickup import ticket


class RecoveryAdmissionTests(unittest.TestCase):
    setUp = fixtures.BlockedRecoveryTests.setUp
    tearDown = fixtures.BlockedRecoveryTests.tearDown
    missing_checks = fixtures.BlockedRecoveryTests.missing_checks
    prepare_watch = fixtures.BlockedRecoveryTests.prepare_watch
    watch = fixtures.BlockedRecoveryTests.watch

    def test_selected_blocked_card_read_reset_leaves_peer_worker_eligible(self):
        self.c['max_workers'] = 1
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        self.gh.items[0]['priority'] = 'P0'
        self.missing_checks()
        self.gh.items.append(ticket(2, priority='P1'))
        read = pickup.card
        failures = []

        def live_card(gh, item_id, scope, **kwargs):
            if item_id == self.card['item_id'] and not kwargs.get('expected') and not failures:
                failures.append(self.clock())
                raise ReadConnectionReset('fixture selected Blocked card reset')
            return read(gh, item_id, scope, **kwargs)

        first_worker_lanes = len(self.proc.lanes)
        with patch.object(pickup, 'card', side_effect=live_card):
            self.watch()
        self.assertEqual(len(failures), 1)
        self.assertFalse(self.factory.processes.stop_path.exists())
        self.assertEqual(self.proc.lanes[first_worker_lanes], 'build')
        self.assertTrue(self.factory.state['cards']['2']['handoff'])
        self.assertTrue(self.factory.state['cards']['1']['handoff'])

    def summary_transport(self, landed, comment_pages, read_attempts, posts):
        original_comment = self.gh.comment
        original_graphql = self.gh.graphql

        def post(card, path):
            body = Path(path).read_text()
            if not body.startswith('SFC automatically verified'):
                return original_comment(card, path)
            posts.append(body)
            saved = read_json(self.factory.state_path)['cards']['1']['blocked_recoveries'][-1]
            self.assertEqual(saved.get('admission_report', {}).get('phase'), 'intent')
            if landed:
                comment_pages.append({'id': 'recovery-summary', 'body': body})
                original_comment(card, path)
            raise ConnectionReset('fixture lost summary response')

        def graphql(query, variables):
            if 'comments(first:100,after:$after)' not in query:
                return original_graphql(query, variables)
            read_attempts.append(variables['after'])
            self.assertEqual(variables['issue'], self.card['id'])
            start = int(variables['after'] or 0)
            return {'node': {'id': self.card['id'], 'number': self.card['number'],
                             'repository': self.card['repository'],
                             'comments': {'nodes': comment_pages[start:start + 1],
                                          'pageInfo': {'hasNextPage': start + 1 < len(comment_pages),
                                                       'endCursor': str(start + 1)}}}}

        return post, graphql

    def test_lost_summary_response_accepts_exact_comment_on_later_page(self):
        self.missing_checks()
        self.prepare_watch()
        pages = [{'id': 'unrelated-comment', 'body': 'Unrelated discussion'}]
        read_attempts, posts = [], []
        post, graphql = self.summary_transport(True, pages, read_attempts, posts)
        with (patch.object(self.gh, 'comment', side_effect=post),
              patch.object(self.gh, 'graphql', side_effect=graphql)):
            self.assertTrue(recovery.run(self.factory, self.card))
        self.assertEqual(read_attempts, [None, '1'])
        self.assertEqual(len(posts), 1)
        saved = read_json(self.factory.state_path)['cards']['1']['blocked_recoveries'][-1]
        self.assertEqual(saved['admission_report']['phase'], 'verified')
        self.assertEqual(saved['admission_report']['comment_id'], 'recovery-summary')

    def test_stop_after_summary_intent_before_post_can_resume_as_unsent(self):
        self.missing_checks()
        self.prepare_watch()
        ensure = self.factory.ensure_running
        stopped = []

        def stop_if_intent():
            record = self.factory.record(self.card)
            report = (record.get('blocked_recoveries') or [{}])[-1].get('admission_report', {})
            if report.get('phase') == 'intent' and not stopped:
                stopped.append(True)
                raise Stopped('fixture stop before comment send')
            ensure()

        with (patch.object(self.factory, 'ensure_running', side_effect=stop_if_intent),
              patch.object(self.gh, 'comment', wraps=self.gh.comment) as post):
            with self.assertRaises(Stopped):
                recovery.run(self.factory, self.card)
            action = self.factory.record(self.card)['blocked_recoveries'][-1]
            self.assertEqual(action['admission_report']['phase'], 'pending')
            post.assert_not_called()
            self.assertTrue(recovery.announce_admission(self.factory, self.card, action))
            self.assertEqual(action['admission_report']['phase'], 'verified')
            post.assert_called_once()

    def test_absent_summary_blocks_restart_until_exact_readback_without_resend(self):
        self.missing_checks()
        self.prepare_watch()
        pages, read_attempts, posts = [], [], []
        post, graphql = self.summary_transport(False, pages, read_attempts, posts)
        with (patch.object(self.gh, 'comment', side_effect=post),
              patch.object(self.gh, 'graphql', side_effect=graphql)):
            with self.assertRaises(recovery.RecoveryStageUncertain):
                recovery.run(self.factory, self.card)
            before = (list(self.proc.lanes), list(self.gh.activity_writes))
            self.assertEqual(read_attempts, [None])
            session = self.factory.watcher.session['id']
            self.factory = Factory(self.root, self.c, self.gh, self.proc)
            self.factory.watcher = Watch(self.factory, resume=session, clock=self.clock, wait=self.clock.wait)
            self.factory.watcher.begin(False)
            self.factory.include_backlog = True
            self.factory.issue_scope = (1,)
            self.card = pickup.cards(self.gh)[0]
            with self.assertRaises(recovery.RecoveryStageUncertain):
                recovery.reconcile(self.factory, [self.card])
            with self.assertRaises(recovery.RecoveryStageUncertain):
                self.factory._admitted_process(self.card)
            self.assertEqual((self.proc.lanes, self.gh.activity_writes), before)
            self.assertEqual(len(posts), 1)

            pages.append({'id': 'late-summary', 'body': posts[0]})
            self.gh.items[0]['labels'].append('sfc-hold')
            recovery.reconcile(self.factory, pickup.cards(self.gh))
            self.assertEqual((self.proc.lanes, self.gh.activity_writes), before)
            self.assertEqual(read_attempts, [None, None, None, None])
            action = self.factory.record(self.card)['blocked_recoveries'][-1]
            self.assertEqual(action['admission_report']['phase'], 'verified')
            self.assertEqual(action['admission_report']['comment_id'], 'late-summary')
            self.assertEqual(len(posts), 1)
