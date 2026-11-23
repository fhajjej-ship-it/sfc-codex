"""Bounded foreground sessions: temporary git repositories and fake time/adapters."""
from __future__ import annotations

import copy
from contextlib import redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import signal
import threading
import time
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import sfc
from sfc_lib.common import Halt, Stopped, Processes, common_dir, exclusive, git, read_json, validate_config, write_json
from sfc_lib.factory import Factory
from sfc_lib.watch import Watch, settings, status
from sfc_lib.rate_limit import RateLimited, ReadConnectionReset
from test_pickup import PickupGitHub, ticket
from test_priority import TicketGitHub
from sfc_lib.priority import create_ticket
from sfc_lib.ticket_coordination import ticket_directory
from test_local_merge import PriorityAtomicGitHub
import test_factory as fixtures


class Clock:
    def __init__(self):
        self.value = 1000.0
        self.on_wait = None
        self.active = lambda: False
        self.tick_active = False

    def __call__(self):
        return self.value

    def wait(self, seconds):
        assert 0 < seconds <= 0.1
        # Legacy lane tests model worker time as instantaneous except for their
        # explicit clock changes. Rolling-admission tests opt into active ticks.
        if threading.current_thread() is threading.main_thread() and self.active() and not self.tick_active:
            time.sleep(0.001)
            return
        self.value += seconds
        if self.on_wait:
            self.on_wait()


class WatchTests(unittest.TestCase):
    tearDown = fixtures.IntegrationTests.tearDown

    def setUp(self):
        fixtures.IntegrationTests.setUp(self)
        self.gh = PickupGitHub(self.root, [ticket(status='Backlog')])
        self.clock = Clock()
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        self.clock.active = lambda: bool(self.factory.occupied)
        self.options = {'poll_interval': 1, 'max_polls': 3, 'max_seconds': 60, 'max_claims': 10}

    def watch(self, **kwargs):
        return self.factory.watch(clock=self.clock, wait=self.clock.wait,
                                  overrides=self.options, **({'start': True} if not kwargs else kwargs))

    def finish(self, card):
        self.gh.move(card, 'Done')
        next(c for c in self.gh.items if c['number'] == card['number'])['state'] = 'CLOSED'

    def session(self):
        return read_json(common_dir(self.root) / 'sfc-watch.json')

    def test_pickup_off_observes_backlog_and_ready_without_claiming(self):
        self.gh.items = [ticket(1, status='Backlog'), ticket(2, status='Ready')]
        self.c['watch'] = {'pickup': False}
        write_json(self.root / '.sfc/config.json', self.c)
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        with patch.object(Factory, 'process', side_effect=self.finish) as process:
            result = self.watch()
            process.assert_not_called()
        self.assertEqual(result['outcome'], 'watch-polls-limit')
        self.assertEqual(self.session()['policy']['fresh_stages'], [])
        self.assertEqual((self.session()['polls'], self.session()['claims']), (3, 0))
        self.assertEqual(self.gh.writes, [])
        self.assertEqual([(w['issue'], w['reasons']) for w in self.factory.state['waiting']],
                         [(n, ['Automatic pickup is off for this watcher']) for n in (1, 2)])
        self.assertEqual(self.factory.run(dry_run=True, include_backlog=True, issues=[1, 2])['selected'], [1, 2])

    def test_pickup_off_preserves_an_existing_owned_claim(self):
        with patch.object(self.factory, 'process', side_effect=Halt('after verified claim')), self.assertRaises(Halt):
            self.watch()
        self.gh.items.append(ticket(2, status='Ready', priority='P0'))
        self.c['watch'] = {'pickup': False}
        write_json(self.root / '.sfc/config.json', self.c)
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        with patch.object(Factory, 'process', side_effect=self.finish) as process:
            self.watch()
        self.assertEqual([call.args[0]['number'] for call in process.call_args_list], [1])
        self.assertEqual(self.gh.writes.count((1, 'Building')), 1)
        self.assertEqual(self.session()['claims'], 0)

    def test_unavailable_queue_read_waits_for_fresh_poll_without_dispatch(self):
        original = self.factory.queue_cards
        attempts = []
        def cards():
            attempts.append(self.clock())
            if len(attempts) == 1:
                raise ReadConnectionReset('fixture outage')
            self.assertGreaterEqual(attempts[-1] - attempts[0], 1)
            return original()
        with patch.object(self.factory, 'queue_cards', side_effect=cards):
            self.watch()
        self.assertGreater(len(attempts), 1)
        self.assertEqual(self.session()['claims'], 1)
        self.assertEqual(self.proc.lanes.count('build'), 1)

    def test_unavailable_queue_refresh_never_admits_from_first_snapshot(self):
        original = self.factory.queue_cards
        calls = []
        def cards():
            calls.append(self.clock())
            if len(calls) == 2:
                raise ReadConnectionReset('fixture outage during refresh')
            if len(calls) == 3:
                self.assertEqual(self.proc.lanes, [])
                self.assertGreaterEqual(calls[-1] - calls[-2], 1)
            return original()
        with patch.object(self.factory, 'queue_cards', side_effect=cards):
            self.watch()
        self.assertEqual(self.session()['claims'], 1)
        self.assertEqual(self.proc.lanes.count('build'), 1)

    def creator(self):
        creator = TicketGitHub(self.root)
        original = creator.call
        def call(*args, **kwargs):
            if args[:2] == ('project', 'view'):
                return {'id': 'p'}
            return original(*args, **kwargs)
        creator.call = call
        args = dict(request_id='watch-arrival', title='Verified arrival',
                    body='## Acceptance criteria\n- Observable outcome', priority='P2',
                    work_type='Feature', reason='Fixture reproduction', unknowns='Customer frequency unknown')
        return creator, args

    def created_card(self, creator):
        content = copy.deepcopy(creator.items['item-1']['content'])
        content.pop('number')
        return ticket(1, **content, status='Backlog', labels=['enhancement'])

    def test_creation_readback_gap_waits_then_completes_on_next_poll(self):
        creator, args = self.creator()
        self.gh.items = []
        entered, release = threading.Event(), threading.Event()
        errors = []
        def pause(gh):
            if gh.snapshots == 3:
                self.gh.items = [self.created_card(creator)]
                entered.set()
                if not release.wait(10):
                    raise RuntimeError('fixture timed out')
        creator.on_snapshot = pause
        def create():
            try:
                create_ticket(creator, self.root, **args)
            except BaseException as exc:
                errors.append(exc)
        thread = threading.Thread(target=create)
        thread.start()
        try:
            self.assertTrue(entered.wait(10))
            def finish_readback():
                # First logical poll was read-only and spent no claims/work waves.
                self.assertEqual(self.gh.writes + self.gh.activity_writes, [])
                self.assertEqual(self.session()['cycles'], 0)
                self.assertEqual(self.session()['claims'], 0)
                self.assertIn('Ticket creation unverified', str(self.factory.state['waiting']))
                self.clock.on_wait = None
                release.set()
                thread.join(10)
                self.assertFalse(thread.is_alive())
            self.clock.on_wait = finish_readback
            with patch.object(self.factory, 'process', side_effect=self.finish) as worker:
                result = self.watch()
            self.assertEqual(result['outcome'], 'watch-polls-limit')
            self.assertEqual(worker.call_count, 1)
            self.assertEqual(self.session()['claims'], 1)
            self.assertEqual(self.session()['cycles'], 1)
            self.assertEqual(errors, [])
            self.assertEqual(read_json(ticket_directory(self.root) / 'watch-arrival.json')['state'], 'complete')
        finally:
            release.set()
            thread.join(10)

    def test_cli_creation_inside_active_worker_preserves_supervisor_state(self):
        creator, args = self.creator()
        self.gh.items = [ticket(2, status='Backlog')]
        write_json(self.root / '.sfc/config.json', self.c)
        body = self.root / 'arrival.md'
        body.write_text(args['body'])
        def worker(card):
            if card['number'] == 2:
                before = self.factory.state_path.read_bytes()
                config = (self.root / '.sfc/config.json').read_bytes()
                with patch.object(sfc, 'GitHub', return_value=creator), redirect_stdout(io.StringIO()):
                    code = sfc.main(['--root', str(self.root), 'create-ticket', '--request-id', args['request_id'],
                                     '--title', args['title'], '--body-file', str(body), '--priority', 'P2',
                                     '--type', 'Feature', '--reason', args['reason'], '--unknowns', args['unknowns']])
                self.assertEqual(code, 0)
                self.assertEqual(read_json(self.factory.state_path)['cards'], json.loads(before)['cards'])
                self.assertEqual((self.root / '.sfc/config.json').read_bytes(), config)
                with self.assertRaisesRegex(Halt, 'Another SFC supervisor'):
                    with exclusive(self.root):
                        pass
                self.gh.items.append(self.created_card(creator))
            self.finish(card)
        with patch.object(self.factory, 'process', side_effect=worker) as process:
            result = self.watch()
        self.assertEqual(result['outcome'], 'watch-polls-limit')
        self.assertEqual(process.call_count, 2)
        self.assertEqual(self.session()['claims'], 2)
        self.assertEqual(self.session()['cycles'], 2)

    def test_uncertain_creation_spends_only_poll_budget(self):
        creator, args = self.creator()
        creator.fail = 'after-priority-field'
        with self.assertRaisesRegex(Halt, 'Incomplete ticket'):
            create_ticket(creator, self.root, **args)
        self.gh.items = [self.created_card(creator)]
        with patch.object(self.factory, 'process') as worker:
            result = self.watch()
        worker.assert_not_called()
        self.assertEqual(result['outcome'], 'watch-polls-limit')
        self.assertEqual((self.session()['claims'], self.session()['cycles'], self.session()['no_progress']), (0, 0, 0))
        self.assertEqual(self.proc.lanes, [])
        self.assertEqual(self.gh.writes + self.gh.activity_writes, [])
        self.assertEqual(self.factory.state['cards'], {})
        self.assertIn('no automatic retry', str(self.factory.state['waiting']))

    def test_initially_empty_then_arrival_dispatches_without_new_prompt(self):
        self.gh.items = []
        def arrival():
            if self.clock() >= 1001 and not self.gh.items:
                self.gh.items.append(ticket(status='Backlog'))
        self.clock.on_wait = arrival
        with patch.object(self.factory, 'process', side_effect=self.finish) as worker:
            result = self.watch()
        self.assertEqual(worker.call_count, 1)
        self.assertEqual(self.gh.writes, [(1, 'Building'), (1, 'Done')])
        self.assertEqual(result['outcome'], 'watch-polls-limit')
        self.assertEqual(self.session()['claims'], 1)
        self.assertEqual(self.session()['cycles'], 1)

    def test_idle_polls_do_not_spend_workers_attempts_or_failure_waves(self):
        self.gh.items = []
        self.c.update(max_cycles=1, max_no_progress=1)
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        with patch.object(self.factory, 'process') as worker:
            self.assertEqual(self.watch()['outcome'], 'watch-polls-limit')
        worker.assert_not_called()
        self.assertEqual(self.proc.lanes, [])
        self.assertEqual((self.session()['claims'], self.session()['cycles'], self.session()['no_progress']), (0, 0, 0))

    def prepare_ci_wait(self):
        self.c.update(merge_policy='auto', max_workers=1)
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        self.gh.items[0]['status'] = 'Building'
        card, wt = fixtures.IntegrationTests.prepare_review(self)
        self.gh.ci = [{'status': 'IN_PROGRESS', 'conclusion': None}]
        self.factory.allow_merge = True
        self.factory.review(card, wt)
        self.assertEqual(self.factory.record(card)['activity']['action'], 'ci')
        return card, wt

    def test_pinned_ci_wait_spends_only_poll_and_time_budgets(self):
        card, _ = self.prepare_ci_wait()
        self.options['max_polls'] = self.c['max_no_progress'] + 2
        before = copy.deepcopy((self.gh.writes, self.gh.activity_writes, self.gh.body_updates,
                                self.proc.lanes, self.factory.record(card)['attempts']))
        with patch.object(self.factory, 'process', wraps=self.factory.process) as process:
            result = self.watch(start=True, allow_merge=True)
        self.assertEqual(result['outcome'], 'watch-polls-limit')
        process.assert_not_called()
        self.assertEqual(self.session()['polls'], self.options['max_polls'])
        self.assertEqual((self.session()['cycles'], self.session()['no_progress'], self.session()['claims']), (0, 0, 0))
        self.assertEqual((self.gh.writes, self.gh.activity_writes, self.gh.body_updates,
                          self.proc.lanes, self.factory.record(card)['attempts']), before)
        self.assertIn('GitHub checks', str(self.factory.state['waiting']))
        self.assertEqual(self.gh.merges, [])

    def test_delayed_ci_success_and_independent_arrival_with_one_worker(self):
        self.prepare_ci_wait()
        self.options['max_polls'] = 6
        original_pr = self.gh.pr
        def pr(number):
            value = original_pr(number)
            if number != 101 or self.clock() >= 1004:
                value['statusCheckRollup'] = [{'status': 'COMPLETED', 'conclusion': 'SUCCESS'}]
            return value
        def arrive():
            if self.clock() >= 1001 and len(self.gh.items) == 1:
                self.gh.items.append(ticket(2, status='Backlog'))
        self.clock.on_wait = arrive
        completed = []
        def merge_gate(card, wt, head, base):
            self.assertTrue(self.factory.pinned(card, head, base))
            self.assertEqual(pr(100 + card['number'])['statusCheckRollup'][0]['conclusion'], 'SUCCESS')
            completed.append(card['number'])
            self.finish(card)
        with patch.object(self.gh, 'pr', side_effect=pr), \
                patch.object(self.factory, 'merge_gate', side_effect=merge_gate):
            result = self.watch(start=True, allow_merge=True)
        self.assertEqual(result['outcome'], 'watch-polls-limit')
        self.assertEqual(completed, [2, 1])
        self.assertEqual(sorted(self.proc.lanes), sorted(['build', 'qa', 'security', 'review'] * 2))
        self.assertEqual(self.session()['cycles'], 2)
        self.assertEqual(self.session()['no_progress'], 0)
        self.assertEqual(self.session()['claims'], 1)
        self.assertEqual(self.gh.writes.count((1, 'Building')), 1)
        self.assertEqual(self.gh.writes.count((2, 'Building')), 1)

    def test_ci_wait_preview_is_read_only_and_requires_merge_opt_in(self):
        card, _ = self.prepare_ci_wait()
        before = copy.deepcopy(self.factory.state)
        saved = self.factory.state_path.read_bytes()
        writes = copy.deepcopy((self.gh.writes, self.gh.activity_writes, self.gh.body_updates))
        preview = self.factory.watch(dry_run=True, allow_merge=True)
        self.assertEqual(preview['selected'], [])
        self.assertIn('GitHub checks', str(preview['waiting']))
        self.assertEqual(self.factory.state, before)
        self.assertEqual(self.factory.state_path.read_bytes(), saved)
        self.assertEqual((self.gh.writes, self.gh.activity_writes, self.gh.body_updates), writes)
        # Watch without separate merge authority must still hand off to a human.
        self.watch()
        self.assertEqual(self.gh.items[0]['status'], 'Blocked')
        self.assertTrue(self.factory.record(card)['handoff'])
        self.assertEqual(self.gh.merges, [])

    def test_ci_wait_with_stale_review_still_dispatches_required_review(self):
        card, _ = self.prepare_ci_wait()
        self.factory.record(card)['receipts'].pop('review')
        self.factory.save()
        self.options['max_polls'] = 5
        result = self.watch(start=True, allow_merge=True)
        self.assertEqual(result['outcome'], 'watch-polls-limit')
        self.assertEqual(self.proc.lanes.count('review'), 2)
        self.assertEqual(self.session()['cycles'], 1)
        self.assertEqual(self.session()['no_progress'], 0)  # Fresh review evidence is meaningful progress.
        self.assertEqual(self.gh.merges, [])

    def test_live_priority_change_arrival_age_and_number_ties(self):
        self.c['max_workers'] = 1
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        self.gh.items = [ticket(1, priority='P2'), ticket(2, priority='P3')]
        self.options['max_polls'] = 10
        ordered = []
        def worker(card):
            ordered.append(card['number'])
            self.finish(card)
        def changes():
            if len(self.gh.items) == 2:
                self.gh.items[1]['priority'] = 'P0'
                self.gh.items.extend([ticket(4, priority='P1'), ticket(3, priority='P1'),
                                      ticket(5, priority='P1', createdAt='2025-01-01T00:00:00Z')])
        self.clock.on_wait = changes
        with patch.object(self.factory, 'process', side_effect=worker):
            self.watch()
        self.assertEqual(ordered, [1, 2, 5, 3, 4])

    def test_exclusions_holds_unknown_stage_and_manual_priority_preserved(self):
        self.gh.items = [ticket(15, priority='P0'), ticket(2, labels=['bug', 'sfc-hold']),
                         ticket(3, status='Unknown'), ticket(4, priority=None), ticket(5)]
        self.options['excluded_issues'] = [15]
        before = copy.deepcopy(self.gh.items)
        with patch.object(self.factory, 'process', side_effect=self.finish):
            self.watch()
        self.assertEqual(self.gh.items[:4], before[:4])
        self.assertEqual({n for n, _ in self.gh.writes}, {5})
        self.assertEqual(self.session()['policy']['excluded_issues'], [15])
        self.assertTrue(self.session()['policy']['future_tickets'])

    def test_closed_prerequisite_and_removed_hold_become_eligible_later(self):
        self.gh.items = [ticket(1, body=ticket()['body'] + '## Blocked by\n- #9'),
                         ticket(2, labels=['sfc-hold'])]
        self.gh.extra_issues[9] = ticket(9)
        def unblock():
            self.gh.extra_issues[9]['state'] = 'CLOSED'
            self.gh.items[1]['labels'] = []
        self.clock.on_wait = unblock
        with patch.object(self.factory, 'process', side_effect=self.finish) as worker:
            self.watch()
        self.assertEqual(worker.call_count, 2)
        self.assertEqual(self.session()['claims'], 2)

    def test_budget_validation_and_old_config_no_migration(self):
        original = copy.deepcopy(self.c)
        self.assertIs(validate_config(self.c), self.c)
        self.assertEqual(self.c, original)
        for key in ('poll_interval', 'max_seconds', 'max_polls', 'max_claims'):
            for invalid in (0, -1, True, 1.5, '2', None):
                with self.subTest(key=key, invalid=invalid), self.assertRaises(Halt):
                    settings({key: invalid})
        for invalid in ({'excluded_issues': [0]}, {'excluded_issues': '1'}, {'hold_label': ''}, {'enabled': True}):
            with self.subTest(invalid=invalid), self.assertRaises(Halt):
                validate_config({**self.c, 'watch': invalid})

    def test_time_and_claim_limits_bound_session_and_preserve_spent_resume(self):
        self.options.update(max_claims=1)
        self.gh.items.append(ticket(2))
        with patch.object(self.factory, 'process', side_effect=self.finish) as worker:
            self.assertEqual(self.watch()['outcome'], 'watch-claims-limit')
            old = self.session()
            self.factory = Factory(self.root, self.c, self.gh, self.proc)
            result = self.watch(resume=old['id'])
        self.assertEqual(result['outcome'], 'watch-claims-limit')
        self.assertEqual(worker.call_count, 1)
        self.assertEqual(self.session()['deadline'], old['deadline'])
        self.assertEqual(self.session()['claims'], 1)
        self.assertEqual(len([s for _, s in self.gh.writes if s == 'Building']), 1)
        self.gh.items = []
        self.options.update(max_seconds=1, poll_interval=30, max_claims=10)
        result = self.watch()
        self.assertEqual(result['outcome'], 'watch-time-limit')
        self.assertEqual(self.clock(), self.session()['deadline'])
        self.assertEqual(self.session()['polls'], 1)

    def test_cutoff_after_planning_does_not_claim_launch_or_spend_a_wave(self):
        original = self.factory.plan
        def cutoff(cards, **options):
            result = original(cards, **options)
            self.clock.value = self.session()['deadline']
            return result
        with patch.object(self.factory, 'plan', side_effect=cutoff), patch.object(self.factory, 'process') as worker:
            self.assertEqual(self.watch()['outcome'], 'watch-time-limit')
        worker.assert_not_called()
        self.assertEqual(self.gh.writes + self.gh.activity_writes, [])
        for key in ('claims', 'cycles', 'no_progress'):
            self.assertEqual(self.session()[key], 0)

    def test_no_progress_cap_persists_across_interruption(self):
        self.c['max_no_progress'] = 1
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        def die(card):
            raise Halt('interrupted after actual dispatch')
        with patch.object(self.factory, 'process', side_effect=die), self.assertRaises(Halt):
            self.watch()
        old = self.session()
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        with patch.object(self.factory, 'process') as worker:
            result = self.watch(resume=old['id'])
        worker.assert_not_called()
        self.assertEqual(result['outcome'], 'no-progress-limit')
        self.assertEqual(self.session()['cycles'], 1)

    def test_ambiguous_claim_latches_spending_and_never_reclaims_on_resume(self):
        self.gh.fail_claim = 'after'
        with self.assertRaisesRegex(Halt, 'Incomplete priority claim'):
            self.watch()
        old = self.session()
        self.assertEqual(old['claims'], 1)
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        self.gh.items.append(ticket(2))
        preview = self.factory.watch(dry_run=True, overrides=self.options)
        self.assertEqual(preview['selected'], [])
        self.assertIn('Unresolved priority claim', str(preview['waiting']))
        with patch.object(self.factory, 'process') as worker, self.assertRaisesRegex(Halt, 'Unresolved priority claim'):
            self.watch(resume=old['id'])
        worker.assert_not_called()
        self.assertEqual(self.gh.writes, [(1, 'Building')])
        self.assertEqual(self.session()['claims'], 1)

    def test_fresh_p0_precedes_owned_claim_without_duplicate_claim(self):
        self.c['max_workers'] = 1
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        def die(card):
            raise Halt('death after verified claim')
        with patch.object(self.factory, 'process', side_effect=die), self.assertRaises(Halt):
            self.watch()
        old = self.session()
        self.gh.items.append(ticket(2, priority='P0'))
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        order = []
        def worker(card):
            order.append(card['number'])
            self.finish(card)
        with patch.object(self.factory, 'process', side_effect=worker):
            self.watch(resume=old['id'])
        self.assertEqual(order, [2, 1])
        self.assertEqual(self.gh.writes.count((1, 'Building')), 1)
        self.assertEqual(self.session()['claims'], 2)

    def test_revalidation_at_claim_and_owned_dispatch_preserves_changed_hold(self):
        original = self.factory.claim_priority
        def hold(card):
            self.gh.items[0]['labels'].append('sfc-hold')
            return original(card)
        with patch.object(self.factory, 'claim_priority', side_effect=hold), patch.object(self.factory, 'process') as worker:
            self.watch()
        worker.assert_not_called()
        self.assertEqual(self.gh.writes, [])
        self.assertEqual(self.session()['claims'], 0)

    def test_real_lanes_human_handoff_does_not_block_later_independent_work(self):
        self.options['max_polls'] = 8
        def arrive():
            if len(self.gh.items) == 1:
                self.gh.items.append(ticket(2, status='Backlog'))
        self.clock.on_wait = arrive
        self.watch()
        self.assertEqual(sorted(self.proc.lanes), sorted(['build', 'qa', 'security', 'review'] * 2))
        self.assertEqual([c['status'] for c in self.gh.items], ['Blocked', 'Blocked'])
        self.assertEqual(self.gh.merges, [])
        self.assertTrue(all(self.factory.record(c).get('handoff') for c in self.gh.items))

    def test_real_failure_attempt_cap_and_no_idle_retry(self):
        self.proc.fail_lane = 'security'
        self.options['max_polls'] = 5
        self.watch()
        self.assertEqual(self.factory.record(self.gh.items[0])['attempts'], self.c['max_attempts'])
        self.assertEqual(self.proc.lanes.count('build'), 2)
        self.assertEqual(self.gh.items[0]['status'], 'Blocked')
        self.assertEqual(self.gh.writes.count((1, 'Building')), 2)  # Claim plus one failed-lane return.

    def test_oversized_reviewed_handoff_agrees_in_activity_status_and_preview(self):
        self.c['merge_policy'] = 'auto'
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        original = self.gh.pr
        with patch.object(self.gh, 'pr', side_effect=lambda number: {
                **original(number), 'additions': 800, 'deletions': 224}):
            result = self.watch(start=True, allow_merge=True)
        self.assertEqual(result['outcome'], 'watch-polls-limit')
        text = self.gh.activities['item-1'][1]
        self.assertIn('1,024 changed lines', text)
        self.assertIn('limit: 400', text)
        self.assertIn('human merge decision', text)
        suffix = ' Project verification: 1 executed, 0 reused.'
        self.assertTrue(text.endswith(suffix))
        reason = text.removesuffix(suffix)
        self.assertEqual(self.factory.state['waiting'][0]['explanation'], reason)
        writes = copy.deepcopy((self.gh.writes, self.gh.activity_writes))
        preview = self.factory.watch(dry_run=True, allow_merge=True)
        self.assertEqual(preview['waiting'][0]['explanation'], reason)
        self.assertEqual(preview['selected'], [])
        self.assertEqual((self.gh.writes, self.gh.activity_writes), writes)
        self.assertEqual(self.proc.lanes, ['build', 'qa', 'security', 'review'])
        self.assertEqual(self.gh.items[0]['status'], 'Blocked')
        self.assertEqual(self.factory.c['auto_max_lines'], 400)
        self.assertEqual(self.gh.merges, [])

    def test_stop_idle_is_prompt_and_preserves_claims(self):
        self.gh.items = []
        self.clock.on_wait = lambda: self.proc.stop_path.touch()
        with self.assertRaises(Stopped):
            self.watch()
        self.assertLess(self.clock(), 1000.2)
        self.assertEqual(self.session()['reason'], 'stopped')
        self.assertEqual(self.proc.lanes, [])

    def test_common_lock_excludes_run_and_watch_from_linked_checkout_through_idle(self):
        linked = self.root.parent / 'linked'
        git(self.root, 'worktree', 'add', '--detach', linked, 'HEAD')
        other = Factory(linked, self.c, self.gh, self.proc)
        self.gh.items = []
        checked = []
        def compete():
            if not checked:
                same_root = Factory(self.root, self.c, self.gh, self.proc)
                for factory, name in ((same_root, 'run'), (other, 'run'), (other, 'watch')):
                    with self.assertRaisesRegex(Halt, 'Another SFC supervisor'):
                        getattr(factory, name)(**({'start': True} if name == 'watch' else {}))
                checked.append(True)
        self.clock.on_wait = compete
        self.watch()
        self.assertEqual(checked, [True])
        self.assertEqual(self.session()['claims'], 0)

    def test_status_preview_cli_and_explicit_start_are_read_only(self):
        write_json(self.root / '.sfc/config.json', self.c)
        out = io.StringIO()
        with patch('sfc_lib.factory.GitHub', return_value=self.gh), redirect_stdout(out):
            code = sfc.main(['--root', str(self.root), 'watch', '--dry-run', '--exclude-issue', '15'])
        self.assertEqual(code, 0)
        value = json.loads(out.getvalue())
        self.assertEqual(value['selected'], [1])
        self.assertTrue(value['options']['future_tickets'])
        self.assertFalse((common_dir(self.root) / 'sfc-watch.json').exists())
        self.assertEqual(self.gh.writes, [])
        with self.assertRaisesRegex(Halt, 'explicit --start'):
            self.factory.watch()
        self.gh.items = []
        self.watch()
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        report = status(self.root, self.clock)
        self.assertIn('stale/inactive', report['freshness'])
        with exclusive(self.root):
            self.assertIn('unknown', status(self.root, self.clock)['freshness'])
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()})
        self.assertEqual(report['session']['reason'], 'watch-polls-limit')

    def test_resume_rejects_changed_scope_config_merge_authority_and_id(self):
        self.gh.items = []
        self.watch()
        saved = self.session()
        for params in ({'resume': 'wrong'}, {'resume': saved['id'], 'allow_merge': True}):
            with self.subTest(params=params), self.assertRaises(Halt):
                self.watch(**params)
        self.options['hold_label'] = 'other-hold'
        with self.assertRaisesRegex(Halt, 'scope/merge authority'):
            self.watch(resume=saved['id'])

    def test_work_cycle_budget_does_not_reset_on_resume(self):
        self.c['max_cycles'] = 1
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        self.gh.items.append(ticket(2))
        with patch.object(self.factory, 'process', side_effect=self.finish):
            self.assertEqual(self.watch()['outcome'], 'cycle-limit')
        old = self.session()
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        with patch.object(self.factory, 'process') as worker:
            self.assertEqual(self.watch(resume=old['id'])['outcome'], 'cycle-limit')
        worker.assert_not_called()
        self.assertEqual(self.session()['cycles'], 1)

    def test_owned_prerequisite_reopens_between_plan_and_dispatch_without_worker(self):
        self.gh.items[0]['body'] += '## Blocked by\n- #9'
        self.gh.extra_issues[9] = ticket(9, state='CLOSED')
        with patch.object(self.factory, 'process', side_effect=Halt('interrupted')), self.assertRaises(Halt):
            self.watch()
        old = self.session()
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        original = self.factory.plan
        def reopen(cards, **options):
            result = original(cards, **options)
            self.gh.extra_issues[9]['state'] = 'OPEN'
            return result
        before = copy.deepcopy(self.gh.writes + self.gh.activity_writes)
        with patch.object(self.factory, 'plan', side_effect=reopen), patch.object(self.factory, 'process') as worker:
            self.watch(resume=old['id'])
        worker.assert_not_called()
        self.assertEqual(self.gh.writes + self.gh.activity_writes, before)
        self.assertIn('Open prerequisites', str(self.factory.state['waiting']))

    def test_empty_queue_config_change_halts_instead_of_polling_old_policy(self):
        self.gh.items = []
        write_json(self.root / '.sfc/config.json', self.c)
        def change():
            write_json(self.root / '.sfc/config.json', {**self.c, 'max_workers': 1})
        self.clock.on_wait = change
        with self.assertRaisesRegex(Halt, 'configuration changed'):
            self.watch()
        self.assertEqual(self.session()['reason'], 'halted')
        self.assertEqual(self.proc.lanes, [])

    def test_common_ledger_is_authoritative_when_archive_copy_is_interrupted(self):
        watcher = Watch(self.factory, start=True, overrides=self.options, clock=self.clock, wait=self.clock.wait)
        with exclusive(self.root):
            watcher.begin(False)
            original = write_json
            def fail_archive(path, value):
                if 'watch-sessions' in Path(path).parts:
                    raise OSError('archive interrupted')
                original(path, value)
            with patch('sfc_lib.watch.write_json', side_effect=fail_archive), self.assertRaises(OSError):
                watcher.spend_claim()
            resumed = Watch(self.factory, resume=watcher.session['id'], clock=self.clock, wait=self.clock.wait)
            resumed.begin(False)
            self.assertEqual(resumed.session['claims'], 1)

    def test_requeued_saved_blocked_execution_does_not_reset_attempt_cap(self):
        self.factory.state['cards']['1'] = {'attempts': self.c['max_attempts'], 'status': 'Blocked',
                                           'history': [], 'worktree': 'saved'}
        self.factory.save()
        self.gh.items[0]['status'] = 'Building'
        with patch.object(self.factory, 'process') as worker:
            self.watch()
        worker.assert_not_called()
        self.assertIn('attempt limit', str(self.factory.state['waiting']))


class WatchMergeTests(unittest.TestCase):
    tearDown = fixtures.IntegrationTests.tearDown
    watch = WatchTests.watch
    session = WatchTests.session

    def setUp(self):
        WatchTests.setUp(self)
        self.c.update(merge_policy='auto', merge_mode='local-verification')
        self.gh = PriorityAtomicGitHub(self.root, self.remote, [ticket(status='Backlog')])
        self.factory = Factory(self.root, self.c, self.gh, self.proc)

    def test_acknowledged_delayed_recognition_reads_without_second_ref_write(self):
        self.gh.recognize = self.gh.close_issue = False
        self.clock.on_wait = lambda: self.recognize()
        self.watch(start=True, allow_merge=True)
        self.assertEqual(len(self.gh.attempts), 1)
        self.assertEqual(len(self.gh.applied), 1)
        self.assertEqual(self.gh.merges, [])
        r = self.factory.record(self.gh.items[0])
        self.assertEqual(r['local_merge']['phase'], 'done')
        self.assertEqual(r['local_merge']['confirmation_reads'], 1)
        self.assertEqual(self.gh.items[0]['status'], 'Done')

    def recognize(self):
        if self.gh.applied:
            self.gh.prs[101]['state'] = 'MERGED'
            self.gh.items[0]['state'] = 'CLOSED'

    def test_cutoff_after_acknowledged_merge_finishes_only_owned_completion(self):
        write_json(self.root / '.sfc/config.json', self.c)
        self.options['excluded_issues'] = [15]
        original = self.gh.local_merge_snapshot
        spent = []
        def cutoff(*args):
            if self.gh.applied and not spent:
                spent.append(self.session())
                self.clock.value = spent[0]['deadline']
                self.gh.items.extend([ticket(2, status='Backlog'), ticket(15, priority='P0')])
            return original(*args)
        with patch.object(self.gh, 'local_merge_snapshot', side_effect=cutoff):
            result = self.watch(start=True, allow_merge=True)
        self.assertEqual(result['outcome'], 'watch-time-limit')
        self.assertEqual(self.gh.items[0]['status'], 'Done')
        self.assertEqual(self.factory.record(self.gh.items[0])['activity']['state'], 'Completed')
        self.assertEqual(self.factory.record(self.gh.items[0])['local_merge']['phase'], 'done')
        self.assertEqual(self.proc.lanes, ['build', 'qa', 'security', 'review'])
        self.assertEqual(len(self.gh.attempts), 1)
        self.assertEqual({n for n, _ in self.gh.writes}, {1})
        for key in ('deadline', 'polls', 'claims', 'cycles', 'no_progress'):
            self.assertEqual(self.session()[key], spent[0][key])

    def test_issue_closure_can_lag_recognized_pr_with_only_read_observation(self):
        self.gh.close_issue = False
        self.clock.on_wait = lambda: self.recognize()
        self.watch(start=True, allow_merge=True)
        self.assertEqual(self.gh.items[0]['status'], 'Done')
        self.assertEqual(self.factory.record(self.gh.items[0])['local_merge']['confirmation_reads'], 1)
        self.assertEqual(len(self.gh.attempts), 1)

    def test_unresolved_confirmation_is_bounded_and_restart_never_rewrites(self):
        self.gh.recognize = self.gh.close_issue = False
        with self.assertRaisesRegex(Halt, 'not confirmed'):
            self.watch(start=True, allow_merge=True)
        self.assertEqual(self.factory.record(self.gh.items[0])['local_merge']['confirmation_reads'], 5)
        self.assertEqual(len(self.gh.attempts), 1)
        old = self.session()
        self.recognize()
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        self.watch(resume=old['id'], allow_merge=True)
        self.assertEqual(len(self.gh.attempts), 1)
        self.assertEqual(self.gh.items[0]['status'], 'Done')

    def test_lost_response_does_not_enter_confirmation_wait_or_repeat_mutation(self):
        self.gh.lost_response = True
        with self.assertRaisesRegex(Halt, 'no retry'):
            self.watch(start=True, allow_merge=True)
        old = self.session()
        self.assertEqual(self.clock(), 1000)
        claim = copy.deepcopy(self.factory.record(self.gh.items[0])['claim'])
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        self.watch(resume=old['id'], allow_merge=True)
        self.assertEqual(self.factory.record(self.gh.items[0])['claim'], claim)
        self.assertEqual(len(self.gh.attempts), 1)
        self.assertEqual(self.gh.items[0]['status'], 'Done')

    def test_excluded_held_or_removed_journal_prevents_any_recovery_write(self):
        self.gh.lost_response = True
        with self.assertRaises(Halt):
            self.watch(start=True, allow_merge=True)
        writes = copy.deepcopy(self.gh.writes + self.gh.activity_writes)
        old = self.session()
        for drift in ('exclude', 'hold', 'removed'):
            with self.subTest(drift=drift):
                original = copy.deepcopy(self.gh.items)
                self.factory = Factory(self.root, self.c, self.gh, self.proc)
                self.options['excluded_issues'] = [1] if drift == 'exclude' else []
                if drift == 'hold': self.gh.items[0]['labels'].append('sfc-hold')
                if drift == 'removed': self.gh.items = []
                preview = self.factory.watch(dry_run=True, overrides=self.options)
                self.assertEqual(preview['selected'], [])
                self.assertIn('local merge journal outside', str(preview['waiting']))
                with self.assertRaisesRegex(Halt, 'local merge journal outside'):
                    self.watch(start=True, allow_merge=True)
                self.assertEqual(self.gh.writes + self.gh.activity_writes, writes)
                self.assertEqual(len(self.gh.attempts), 1)
                self.gh.items = original

    def test_hold_during_confirmation_halts_without_done_write(self):
        self.gh.recognize = self.gh.close_issue = False
        self.clock.on_wait = lambda: self.gh.items[0].update(labels=['bug', 'sfc-hold'])
        # Either the worker's completion guard or the supervisor's refreshed
        # scope guard can observe the hold first. Both must halt without Done.
        with self.assertRaisesRegex(Halt, 'outside (watcher|explicit run issue scope)'):
            self.watch(start=True, allow_merge=True)
        self.assertNotIn((1, 'Done'), self.gh.writes)
        self.assertEqual(len(self.gh.attempts), 1)
        self.assertIn('sfc-hold', self.gh.items[0]['labels'])
        self.assertNotEqual(self.factory.record(self.gh.items[0])['local_merge']['phase'], 'done')

    def test_watch_activation_alone_never_merges(self):
        self.watch(start=True)
        self.assertEqual(self.gh.attempts, [])
        self.assertEqual(self.gh.items[0]['status'], 'Blocked')
        self.assertTrue(self.factory.record(self.gh.items[0])['handoff'])

    def test_human_policy_wins_over_watch_merge_flag(self):
        self.c['merge_policy'] = 'human'
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        self.watch(start=True, allow_merge=True)
        self.assertEqual(self.gh.attempts, [])
        self.assertEqual(self.gh.items[0]['status'], 'Blocked')

    def test_ambiguous_done_response_never_retries_completion_write(self):
        original = self.gh.move
        def drop_done(card, stage):
            if stage == 'Done':
                self.gh.writes.append((card['number'], stage))
                raise Halt('lost Done response')
            original(card, stage)
        with patch.object(self.gh, 'move', side_effect=drop_done), self.assertRaises(Halt):
            self.watch(start=True, allow_merge=True)
        old = self.session()
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        with self.assertRaisesRegex(Halt, 'Ambiguous Done'):
            self.watch(resume=old['id'], allow_merge=True)
        self.assertEqual(self.gh.writes.count((1, 'Done')), 1)
        self.assertEqual(len(self.gh.attempts), 1)

    def test_pending_journal_cannot_recover_without_separate_watch_merge_opt_in(self):
        self.gh.lost_response = True
        with self.assertRaises(Halt):
            self.watch(start=True, allow_merge=True)
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        with self.assertRaisesRegex(Halt, 'separate auto-merge authority'):
            self.watch(start=True, allow_merge=False)
        self.assertEqual(len(self.gh.attempts), 1)
        self.assertNotIn((1, 'Done'), self.gh.writes)

    def test_old_base_snapshot_after_acknowledgement_is_not_a_recognition_delay(self):
        base = self.gh.oid('main')
        tree = self.gh.oid(base + '^{tree}')
        def stale(snapshot):
            if self.gh.applied:
                snapshot['base']['target'].update(oid=base, tree={'oid': tree})
                snapshot['pullRequest']['state'] = 'OPEN'
        self.gh.edit_snapshot = stale
        with self.assertRaisesRegex(Halt, 'not confirmed'):
            self.watch(start=True, allow_merge=True)
        self.assertEqual(self.clock(), 1000)
        self.assertEqual(len(self.gh.attempts), 1)
        self.assertNotIn((1, 'Done'), self.gh.writes)


class WatchCutoffTests(unittest.TestCase):
    tearDown = fixtures.IntegrationTests.tearDown
    watch = WatchTests.watch
    session = WatchTests.session

    def setUp(self):
        WatchMergeTests.setUp(self)
        # Exercise ensure_running's real config validation during bounded IO.
        write_json(self.root / '.sfc/config.json', self.c)

    def interrupt_final_config_validation(self, interrupt, error, message):
        import subprocess
        original = subprocess.run
        validations = []
        base = self.gh.oid('main')

        def validate(argv, *args, **kwargs):
            result = original(argv, *args, **kwargs)
            intent = self.factory.record(self.gh.items[0]).get('local_merge', {})
            if argv[:2] == ['git', 'check-ref-format'] and intent.get('phase') == 'intent':
                validations.append(self.clock())
                interrupt()
            return result

        with patch('sfc_lib.common.subprocess.run', side_effect=validate):
            with self.assertRaisesRegex(error, message):
                self.watch(start=True, allow_merge=True)
        self.assertEqual(validations, [1000.0])
        self.assertEqual(self.gh.attempts, [])
        self.assertEqual(self.gh.oid('main'), base)
        intent = self.factory.record(self.gh.items[0])['local_merge']
        self.assertEqual(intent['phase'], 'intent')
        self.assertNotIn('watch_completion', intent)
        self.assertEqual(self.gh.items[0]['status'], 'Review')
        self.assertEqual(self.gh.items[0]['state'], 'OPEN')

    def test_config_validation_crossing_work_cutoff_cannot_dispatch_ref_write(self):
        self.interrupt_final_config_validation(
            lambda: setattr(self.clock, 'value', self.session()['deadline'] + 0.5),
            Halt, 'No exact pre-cutoff acknowledged')
        self.assertEqual(self.session()['deadline'], 1060.0)

    def test_operator_stop_during_config_validation_cannot_dispatch_ref_write(self):
        self.interrupt_final_config_validation(
            lambda: self.proc.shared_stop_path.touch(), Stopped, '[Ss]top requested')

    def test_config_validation_exhausting_completion_cannot_write_done(self):
        import subprocess
        original = subprocess.run
        validations = []

        def validate(argv, *args, **kwargs):
            result = original(argv, *args, **kwargs)
            intent = self.factory.record(self.gh.items[0]).get('local_merge', {})
            if (argv[:2] == ['git', 'check-ref-format'] and
                    intent.get('phase') == 'confirmed' and intent.get('done_requested')):
                validations.append(self.clock())
                self.clock.value = intent['watch_completion']['deadline'] + 0.5
            return result

        with patch('sfc_lib.common.subprocess.run', side_effect=validate):
            with self.assertRaisesRegex(Halt, 'completion allowance exhausted'):
                self.watch(start=True, allow_merge=True)
        self.assertEqual(validations, [1000.0])
        self.assertNotIn((1, 'Done'), self.gh.writes)
        self.assertEqual(self.gh.items[0]['status'], 'Review')
        self.assertEqual(len(self.gh.attempts), 1)
        intent = self.factory.record(self.gh.items[0])['local_merge']
        self.assertIn('done_requested', intent)
        self.assertIn('exhausted_at', intent['watch_completion'])

    def pending(self):
        self.options['excluded_issues'] = [15]
        with patch('sfc_lib.local_merge.LocalMerge.reconcile', side_effect=Halt('pause after acknowledgement')):
            with self.assertRaisesRegex(Halt, 'pause after acknowledgement'):
                self.watch(start=True, allow_merge=True)
        self.saved = self.session()
        self.clock.value = self.saved['deadline']
        self.gh.items.extend([ticket(2, status='Backlog'), ticket(15, priority='P0')])
        self.before = copy.deepcopy(self.gh.writes + self.gh.activity_writes)
        self.events = copy.deepcopy(self.proc.events)

    def resume(self):
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        return self.watch(resume=self.saved['id'], allow_merge=True)

    def test_expired_resume_completes_without_renewing_or_admitting_work(self):
        self.pending()
        identity = copy.deepcopy(self.factory.record(self.gh.items[0])['local_merge']['card_identity'])
        result = self.resume()
        self.assertEqual(result['outcome'], 'watch-time-limit')
        self.assertEqual(self.gh.items[0]['status'], 'Done')
        self.assertEqual(self.factory.record(self.gh.items[0])['activity']['state'], 'Completed')
        self.assertEqual(self.proc.events, self.events)
        self.assertEqual(len(self.gh.attempts), 1)
        for key in ('deadline', 'polls', 'claims', 'cycles', 'no_progress'):
            self.assertEqual(self.session()[key], self.saved[key])
        self.assertEqual({n for n, _ in self.gh.writes}, {1})
        self.assertEqual(self.factory.record(self.gh.items[0])['local_merge']['card_identity'], identity)

    def test_cutoff_during_done_write_preserves_intent_and_finishes_readback(self):
        original = self.gh.move
        def cutoff(card, stage):
            if stage == 'Done':
                self.clock.value = self.session()['deadline']
            original(card, stage)
        with patch.object(self.gh, 'move', side_effect=cutoff):
            self.watch(start=True, allow_merge=True)
        record = self.factory.record(self.gh.items[0])
        self.assertEqual(record['local_merge']['phase'], 'done')
        self.assertIn('done_requested', record['local_merge'])
        self.assertEqual(record['activity']['state'], 'Completed')
        self.assertEqual(self.gh.writes.count((1, 'Done')), 1)
        observations = record['local_merge']['completion_observations']
        self.assertTrue(any(v['kind'] == 'merge' and not v['after_cutoff'] for v in observations))
        self.assertTrue(any(v['kind'] == 'Done' and v['after_cutoff'] for v in observations))

    def test_recognition_wait_crosses_cutoff_without_ref_retry(self):
        self.gh.recognize = self.gh.close_issue = False
        def recognize():
            self.clock.value = self.session()['deadline']
            WatchMergeTests.recognize(self)
            self.clock.on_wait = None
        self.clock.on_wait = recognize
        self.watch(start=True, allow_merge=True)
        self.assertEqual(self.gh.items[0]['status'], 'Done')
        self.assertEqual(len(self.gh.attempts), 1)

    def test_exhaustion_is_finite_and_resume_does_not_renew_allowance(self):
        self.pending()
        grant = copy.deepcopy(self.factory.record(self.gh.items[0])['local_merge']['watch_completion'])
        self.clock.value = grant['deadline']
        for _ in range(2):
            with self.assertRaisesRegex(Halt, 'completion allowance exhausted'):
                self.resume()
            self.assertEqual(self.gh.writes + self.gh.activity_writes, self.before)
            exhausted = self.factory.record(self.gh.items[0])['local_merge']['watch_completion']
            self.assertIn('exhausted_at', exhausted)
            self.assertEqual({k: v for k, v in exhausted.items() if k != 'exhausted_at'}, grant)
            self.assertEqual(self.session()['deadline'], self.saved['deadline'])
            self.clock.value = self.saved['deadline']  # Even a backwards clock cannot unlatch exhaustion.
        self.assertEqual(self.proc.events, self.events)

    def test_operator_stop_during_completion_prevents_done(self):
        self.pending()
        original = self.gh.local_merge_snapshot
        def stop(*args):
            self.proc.shared_stop_path.touch()
            return original(*args)
        with patch.object(self.gh, 'local_merge_snapshot', side_effect=stop):
            with self.assertRaisesRegex(Stopped, 'Operator stop'):
                self.resume()
        self.assertNotIn((1, 'Done'), self.gh.writes)
        self.assertEqual(self.proc.events, self.events)

    def test_saved_config_change_during_completion_prevents_done(self):
        self.pending()
        original = self.gh.local_merge_snapshot
        def change_config(*args):
            write_json(self.root / '.sfc/config.json', {**self.c, 'max_workers': 1})
            return original(*args)
        with patch.object(self.gh, 'local_merge_snapshot', side_effect=change_config):
            with self.assertRaisesRegex(Halt, 'configuration changed'):
                self.resume()
        self.assertEqual(self.gh.writes + self.gh.activity_writes, self.before)
        self.assertEqual(self.proc.events, self.events)
        self.assertEqual(len(self.gh.attempts), 1)

    def completion_rate_wait(self, change_after=None):
        self.pending()
        grant = copy.deepcopy(self.factory.record(self.gh.items[0])['local_merge']['watch_completion'])
        original = self.gh.local_merge_snapshot
        calls = []
        def snapshot(*args):
            gate = self.gh.read_wait
            gate.clock = self.clock
            def wait(seconds):
                self.clock.value += seconds
                if change_after is not None and self.clock() >= grant['work_deadline'] + change_after:
                    write_json(self.root / '.sfc/config.json', {**self.c, 'max_workers': 1})
            gate.wait = wait
            def operation():
                calls.append(self.clock())
                if len(calls) == 1:
                    raise RateLimited({'retry-after': '2'})
                return original(*args)
            return gate.call(operation, lambda: {})
        with patch.object(self.gh, 'local_merge_snapshot', side_effect=snapshot):
            if change_after is None:
                self.assertEqual(self.resume()['outcome'], 'watch-time-limit')
                self.assertEqual(calls[:2], [grant['work_deadline'], grant['work_deadline'] + 2])
                self.assertEqual(self.gh.items[0]['status'], 'Done')
            else:
                with self.assertRaisesRegex(Halt, 'configuration changed'):
                    self.resume()
                self.assertEqual(calls, [grant['work_deadline']])
                self.assertEqual(self.clock(), grant['work_deadline'] + change_after)
                self.assertEqual(self.gh.writes + self.gh.activity_writes, self.before)
                self.assertEqual(self.gh.items[0]['status'], 'Review')
        intent = self.factory.record(self.gh.items[0])['local_merge']
        self.assertEqual(intent['watch_completion'], grant)
        self.assertEqual(self.proc.events, self.events)
        self.assertEqual(len(self.gh.attempts), 1)
        for key in ('deadline', 'polls', 'claims', 'cycles', 'no_progress'):
            self.assertEqual(self.session()[key], self.saved[key])

    def test_completion_rate_wait_stops_on_fresh_config_drift(self):
        self.completion_rate_wait(change_after=1)

    def test_completion_rate_wait_checks_config_at_exact_retry_boundary(self):
        self.completion_rate_wait(change_after=2)

    def test_completion_rate_wait_preserves_original_grace_after_work_cutoff(self):
        self.completion_rate_wait()

    def test_changed_identity_evidence_hold_or_stage_cannot_complete(self):
        self.pending()
        original_items = copy.deepcopy(self.gh.items)
        original_state = copy.deepcopy(self.factory.state)
        for drift in ('hold', 'identity', 'task', 'evidence', 'stage', 'recognition', 'reachability'):
            with self.subTest(drift=drift):
                self.gh.items = copy.deepcopy(original_items)
                state = copy.deepcopy(original_state)
                if drift == 'hold': self.gh.items[0]['labels'].append('sfc-hold')
                if drift == 'identity': self.gh.items[0]['id'] = 'another-issue'
                if drift == 'task': self.gh.items[0]['body'] += '\nChanged task'
                if drift == 'evidence': state['cards']['1']['verification']['passed'] = False
                if drift == 'stage': self.gh.items[0]['status'] = 'Blocked'
                if drift == 'recognition': self.gh.prs[101]['state'] = 'CLOSED'
                if drift == 'reachability':
                    git(self.remote, 'update-ref', 'refs/heads/main', state['cards']['1']['base'])
                write_json(self.factory.state_path, state)
                with self.assertRaises(Halt):
                    self.resume()
                self.assertNotIn((1, 'Done'), self.gh.writes)
                self.assertEqual(self.proc.events, self.events)
                self.gh.prs[101]['state'] = 'MERGED'

    def test_ambiguous_done_and_activity_writes_keep_latches_on_expired_resume(self):
        self.pending()
        original_move = self.gh.move
        def lost_done(card, stage):
            if stage == 'Done':
                self.gh.writes.append((card['number'], stage))
                raise Halt('lost Done response')
            original_move(card, stage)
        with patch.object(self.gh, 'move', side_effect=lost_done):
            with self.assertRaisesRegex(Halt, 'lost Done'):
                self.resume()
        with self.assertRaisesRegex(Halt, 'Ambiguous Done'):
            self.resume()
        self.assertEqual(self.gh.writes.count((1, 'Done')), 1)
        self.assertIn('done_requested', self.factory.record(self.gh.items[0])['local_merge'])

    def test_lost_completed_activity_response_never_becomes_terminal_or_retries(self):
        self.pending()
        original = self.gh.write_activity
        def lost_activity(card, scope, values):
            original(card, scope, values)
            raise Halt('lost Completed response')
        with patch.object(self.gh, 'write_activity', side_effect=lost_activity):
            with self.assertRaisesRegex(Halt, 'Activity reporting halted'):
                self.resume()
        record = self.factory.record(self.gh.items[0])
        self.assertEqual(record['local_merge']['phase'], 'confirmed')
        self.assertTrue(record['activity']['pending'])
        writes = copy.deepcopy(self.gh.activity_writes)
        with self.assertRaisesRegex(Halt, 'Activity write unresolved'):
            self.resume()
        self.assertEqual(self.gh.activity_writes, writes)
        self.assertTrue(self.factory.record(self.gh.items[0])['activity']['pending'])

    def test_manual_stage_edit_before_completed_activity_is_preserved(self):
        self.pending()
        original = self.gh.activity_snapshot
        def edit(card):
            result = original(card)
            self.gh.items[0]['status'] = 'Blocked'
            return result
        with patch.object(self.gh, 'activity_snapshot', side_effect=edit):
            with self.assertRaisesRegex(Halt, 'Activity reporting halted'):
                self.resume()
        self.assertEqual(self.gh.items[0]['status'], 'Blocked')
        self.assertEqual(self.gh.activity_writes, [v for v in self.before if isinstance(v[0], str)])
        self.assertEqual(self.factory.record(self.gh.items[0])['local_merge']['phase'], 'confirmed')

    def test_late_or_ambiguous_merge_response_never_admits_completion(self):
        self.gh.after_write = lambda intent: setattr(self.clock, 'value', self.session()['deadline'])
        with self.assertRaisesRegex(Halt, 'pre-cutoff acknowledged'):
            self.watch(start=True, allow_merge=True)
        self.assertNotIn('watch_completion', self.factory.record(self.gh.items[0])['local_merge'])
        self.assertNotIn((1, 'Done'), self.gh.writes)
        self.assertEqual(len(self.gh.attempts), 1)

    def test_completion_api_timeout_is_clipped_and_exhaustion_stops_next_call(self):
        from sfc_lib.github import GitHub
        self.pending()
        adapter = object.__new__(GitHub)
        adapter.root, adapter.env = self.root, {}
        def bounded_snapshot(*args):
            with patch('sfc_lib.common.subprocess.run', return_value=SimpleNamespace(
                    stdout='{}', stderr='', returncode=0)) as request:
                adapter.call('api', 'offline-fixture')
                self.assertEqual(request.call_args.kwargs['timeout'], 30)
                self.clock.value += 31
                with self.assertRaisesRegex(Halt, 'completion allowance exhausted'):
                    adapter.call('api', 'offline-fixture')
                self.assertEqual(request.call_count, 1)
            raise Halt('completion allowance exhausted in offline IO fixture')
        with patch.object(self.gh, 'local_merge_snapshot', side_effect=bounded_snapshot):
            with self.assertRaisesRegex(Halt, 'completion allowance exhausted'):
                self.resume()
        self.assertNotIn((1, 'Done'), self.gh.writes)
        self.assertIn('exhausted_at', self.factory.record(self.gh.items[0])['local_merge']['watch_completion'])

    def test_old_terminal_activity_latch_is_not_cleared_or_retried(self):
        self.pending()
        record = self.factory.record(self.gh.items[0])
        record['local_merge']['phase'] = 'done'
        record['activity']['pending'] = True
        self.factory.save()
        with self.assertRaisesRegex(Halt, 'Unresolved activity write on terminal'):
            self.resume()
        self.assertEqual(self.gh.writes + self.gh.activity_writes, self.before)
        self.assertTrue(self.factory.record(self.gh.items[0])['activity']['pending'])


class WatchStopTests(unittest.TestCase):
    setUp = WatchTests.setUp
    tearDown = fixtures.IntegrationTests.tearDown
    watch = WatchTests.watch
    session = WatchTests.session

    def test_stop_active_worker_uses_owned_process_and_retains_evidence(self):
        process = Processes(self.root / '.sfc/stop')
        self.factory = Factory(self.root, self.c, self.gh, process)
        marker = self.root / 'active-marker'
        log = self.root / '.sfc/active.log'
        def active(card):
            process.execute([sys.executable, '-c',
                             "from pathlib import Path; import time; Path('active-marker').touch(); time.sleep(20)"],
                            self.root, log, 5)
        errors = []
        def stop():
            end = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < end:
                time.sleep(0.01)
            try:
                with redirect_stdout(io.StringIO()):
                    sfc.main(['--root', str(self.root), 'stop'])
            except Exception as exc:
                errors.append(exc)
        stopper = threading.Thread(target=stop)
        stopper.start()
        try:
            with patch.object(self.factory, 'process', side_effect=active), self.assertRaises(Stopped):
                self.watch()
        finally:
            stopper.join(timeout=6)
        self.assertFalse(stopper.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(log.exists())
        self.assertEqual(process.children, set())
        self.assertEqual(self.session()['reason'], 'stopped')
        self.assertEqual(self.factory.record(self.gh.items[0])['claim']['state'], 'verified')

    def test_chat_workflow_rejects_watcher_before_installing_signal_handlers(self):
        write_json(self.root / '.sfc/config.json', self.c)
        with patch('sfc.Factory') as factory, patch('sfc.signal.signal') as install_signal, \
                redirect_stderr(io.StringIO()) as error:
            self.assertEqual(sfc.main(['--root', str(self.root), 'watch', '--start']), 1)
        self.assertIn('your Codex chat', error.getvalue())
        factory.assert_not_called()
        install_signal.assert_not_called()
        self.assertFalse((common_dir(self.root) / 'sfc-stop').exists())

    def test_reaped_child_is_never_signalled_using_reused_numeric_pid(self):
        class Reaped:
            def __init__(self):
                self.returncode = None
                self.pid = 999999
                self._waitpid_lock = threading.Lock()
            def poll(self):
                return self.returncode
            def communicate(self, **kwargs):
                with self._waitpid_lock:
                    self.returncode = 0
                return '', ''
        proc = Processes(self.root / '.sfc/stop')
        with patch('sfc_lib.common.subprocess.Popen', return_value=Reaped()), \
                patch('sfc_lib.common.os.killpg') as kill:
            self.assertEqual(proc.execute(['fake'], self.root, self.root / 'child.log', 1), 0)
        kill.assert_not_called()
