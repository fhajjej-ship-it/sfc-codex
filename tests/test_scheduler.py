"""Actual rolling admission with fake adapters, clocks and event barriers."""
import copy
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

import test_watch as watch_fixtures
from test_pickup import ticket
import test_factory as factory_fixtures
from test_local_merge import PriorityAtomicGitHub
from sfc_lib import pickup
from sfc_lib.common import Halt, Stopped, read_json
from sfc_lib.factory import Factory


class SchedulerTests(unittest.TestCase):
    setUp = watch_fixtures.WatchTests.setUp
    tearDown = factory_fixtures.IntegrationTests.tearDown
    watch = watch_fixtures.WatchTests.watch
    finish = watch_fixtures.WatchTests.finish
    session = watch_fixtures.WatchTests.session

    def test_idle_watcher_reads_once_until_the_final_poll(self):
        self.options['max_polls'] = 2
        self.gh.items = [ticket(1, status='Blocked')]
        with patch.object(self.factory, 'queue_cards', wraps=self.factory.queue_cards) as read:
            result = self.watch()
        self.assertEqual(result['outcome'], 'watch-polls-limit')
        self.assertEqual(read.call_count, 3)
        self.assertEqual(self.gh.writes, [])
        self.assertEqual(self.proc.lanes, [])

    def test_final_watch_poll_picks_up_card_readied_during_reconciliation(self):
        self.options['max_polls'] = 1
        card = ticket(1, status='Blocked')
        self.gh.items = [card]
        reconcile = self.factory.reconcile

        def arrive(cards):
            self.assertEqual(cards[0]['status'], 'Blocked')
            card['status'] = 'Ready'
            return reconcile(cards)

        with patch.object(self.factory, 'reconcile', side_effect=arrive), \
             patch.object(self.factory, 'process', side_effect=self.finish) as worker:
            self.assertEqual(self.watch()['outcome'], 'watch-polls-limit')
        worker.assert_called_once()
        self.assertEqual(self.session()['polls'], 1)
        self.assertEqual(card['status'], 'Done')

    def test_final_watch_poll_refreshes_after_initial_ranking(self):
        self.options.update(max_polls=1, max_claims=1)
        arriving = ticket(2, status='Backlog', priority='P0')
        self.gh.items = [ticket(1, status='Ready', priority='P1'), arriving]
        plan = self.factory.plan

        def rank(cards, **options):
            result = plan(cards, **options)
            arriving['status'] = 'Ready'
            return result

        with patch.object(self.factory, 'plan', side_effect=rank), \
             patch.object(self.factory, 'process', side_effect=self.finish) as worker:
            self.watch()
        worker.assert_called_once()
        self.assertEqual(worker.call_args.args[0]['number'], 2)

    def test_idle_watch_reads_closed_prerequisites_once_per_poll_for_ranking(self):
        self.options['max_polls'] = 2
        card = ticket(1, status='Ready')
        numbers = (10, 11, 12)
        card['body'] += '\n## Blocked by\n' + ''.join(f'- #{n}\n' for n in numbers)
        self.gh.items = [card]
        self.gh.extra_issues = {n: ticket(n, state='CLOSED') for n in numbers}
        reads = []
        plan = self.factory.plan

        def count_prerequisites(cards, **options):
            start = len(self.gh.reads)
            selected, waiting = plan(cards, **options)
            if selected:
                reads.append([params['number'] for query, params in self.gh.reads[start:]
                              if 'issue(number:' in query and params.get('number') in numbers])
            return selected, waiting

        with patch.object(self.factory, 'plan', side_effect=count_prerequisites), \
             patch.object(self.factory, 'process', side_effect=self.finish) as worker:
            self.watch()
        worker.assert_called_once()
        self.assertEqual(reads, [list(numbers), []])

    def test_waiting_on_open_prerequisite_keeps_one_inventory_until_final_poll(self):
        self.options['max_polls'] = 2
        self.gh.items = [ticket(1, status='Ready')]
        self.gh.items[0]['body'] += '\n## Blocked by\n- #10\n'
        self.gh.extra_issues[10] = ticket(10, state='OPEN')
        with patch.object(self.factory, 'queue_cards', wraps=self.factory.queue_cards) as read, \
             patch.object(self.factory, 'process') as worker:
            self.assertEqual(self.watch()['outcome'], 'watch-polls-limit')
        worker.assert_not_called()
        self.assertEqual(read.call_count, 3)

    def test_reopened_cached_prerequisite_replans_peer_in_same_poll(self):
        self.options.update(max_polls=2, max_claims=1)
        self.gh.items = [ticket(1, status='Ready', priority='P0'), ticket(2, status='Ready', priority='P1')]
        self.gh.items[0]['body'] += '\n## Blocked by\n- #10\n'
        self.gh.extra_issues[10] = ticket(10, state='CLOSED')
        read = self.factory.queue_cards
        polls = []

        def refresh():
            cards = read()
            if self.session()['polls'] == 1 and self.factory.next_eligible:
                self.gh.extra_issues[10]['state'] = 'OPEN'
            return cards

        def finish(card):
            polls.append((card['number'], self.session()['polls']))
            self.finish(card)

        with patch.object(self.factory, 'queue_cards', side_effect=refresh), \
             patch.object(self.factory, 'process', side_effect=finish):
            self.watch()
        self.assertEqual(polls, [(2, 1)])
        self.assertEqual(self.gh.items[0]['status'], 'Ready')

    def assert_reopened_prerequisite_reranks_newly_ready_p0_before_p1(self, max_polls):
        self.options.update(max_polls=max_polls, max_claims=1)
        first = ticket(1, status='Ready', priority='P0')
        first['body'] += '\n## Blocked by\n- #10\n'
        newly_ready = ticket(3, status='Backlog', priority='P0')
        self.gh.items = [first, ticket(2, status='Ready', priority='P1'), newly_ready]
        self.gh.extra_issues[10] = ticket(10, state='CLOSED')
        original = pickup.prerequisite_problems

        def observe(gh, card, **options):
            if 'closed' not in options and card['number'] == 1:
                self.gh.extra_issues[10]['state'] = 'OPEN'
                newly_ready['status'] = 'Ready'
            return original(gh, card, **options)

        with patch.object(pickup, 'prerequisite_problems', side_effect=observe), \
             patch.object(self.factory, 'process', side_effect=self.finish) as worker:
            self.watch()
        worker.assert_called_once()
        self.assertEqual(worker.call_args.args[0]['number'], 3)

    def test_reopened_prerequisite_reranks_newly_ready_p0_before_p1(self):
        self.assert_reopened_prerequisite_reranks_newly_ready_p0_before_p1(2)

    def test_final_poll_reranks_newly_ready_p0_after_prerequisite_reopens(self):
        self.assert_reopened_prerequisite_reranks_newly_ready_p0_before_p1(1)

    def test_refreshed_queue_invalidates_closed_prerequisite_without_another_inventory(self):
        self.options.update(max_polls=2, max_claims=1)
        first = ticket(1, status='Ready', priority='P0')
        first['body'] += '\n## Blocked by\n- #10\n'
        prerequisite = ticket(10, state='CLOSED', status='Done')
        self.gh.items = [first, ticket(2, status='Ready', priority='P1'), prerequisite]
        read = self.factory.queue_cards
        inventories = []
        admitted = []

        def inventory():
            inventories.append(1)
            if len(inventories) == 2:
                prerequisite['state'] = 'OPEN'
            return read()

        def finish(card):
            admitted.append((card['number'], len(inventories)))
            self.finish(card)

        with patch.object(self.factory, 'queue_cards', side_effect=inventory), \
             patch.object(self.factory, 'process', side_effect=finish):
            self.watch()
        self.assertEqual(admitted, [(2, 2)])

    def test_changed_task_with_reopened_off_board_prerequisite_keeps_peer_in_same_poll(self):
        self.options.update(max_polls=2, max_claims=1)
        first = ticket(1, status='Ready', priority='P0')
        first['body'] += '\n## Blocked by\n- #10\n'
        self.gh.items = [first, ticket(2, status='Ready', priority='P1')]
        self.gh.extra_issues[10] = ticket(10, state='CLOSED')
        read = self.factory.queue_cards
        inventories = []
        admitted = []

        def inventory():
            cards = read()
            inventories.append(1)
            if len(inventories) == 2:
                self.gh.extra_issues[10]['state'] = 'OPEN'
                first['body'] += '\nA changed acceptance note.\n'
            return cards

        def finish(card):
            admitted.append((card['number'], self.session()['polls']))
            self.finish(card)

        with patch.object(self.factory, 'queue_cards', side_effect=inventory), \
             patch.object(self.factory, 'process', side_effect=finish):
            self.watch()
        self.assertEqual(admitted, [(2, 1)])

    def test_final_poll_replan_skips_rejected_ticket_and_admits_peer(self):
        self.options.update(max_polls=1, max_claims=1)
        first = ticket(1, status='Ready', priority='P0')
        self.gh.items = [first, ticket(2, status='Ready', priority='P1')]
        original = pickup.card
        changes = []

        def observe(gh, item_id, scope, *, expected=None):
            if expected is None and item_id == first['item_id']:
                changes.append(1)
                first['body'] += '\nA changed acceptance note.\n'
            return original(gh, item_id, scope, expected=expected)

        with patch.object(pickup, 'card', side_effect=observe), \
             patch.object(self.factory, 'process', side_effect=self.finish) as worker:
            self.watch()
        worker.assert_called_once()
        self.assertEqual(worker.call_args.args[0]['number'], 2)
        self.assertEqual(changes, [1])
        waiting = next(row for row in self.session()['waiting'] if row['issue'] == 1)
        self.assertIn('recheck it if another poll is permitted', waiting['explanation'])
        self.assertNotIn('reconciliation', waiting['explanation'])

    def assert_replanned_watcher_reports_exclusion(self, change, explanation, *, creation_problem=False):
        self.options.update(max_polls=1, max_claims=1)
        first = ticket(1, status='Ready', priority='P0')
        self.gh.items = [first, ticket(2, status='Ready', priority='P1')]
        original = pickup.card
        original_creation = self.factory.creation_problem
        changed = False

        def observe(gh, item_id, scope, *, expected=None):
            nonlocal changed
            if expected is None and item_id == first['item_id']:
                first.update(change)
                changed = True
            return original(gh, item_id, scope, expected=expected)

        def creation(card):
            if changed and creation_problem and card['number'] == 1:
                return 'Ticket creation needs verification'
            return original_creation(card)

        with patch.object(pickup, 'card', side_effect=observe), \
             patch.object(self.factory, 'creation_problem', side_effect=creation), \
             patch.object(self.factory, 'process', side_effect=self.finish) as worker:
            self.watch()
        worker.assert_called_once()
        self.assertEqual(worker.call_args.args[0]['number'], 2)
        waiting = next(row for row in self.session()['waiting'] if row['issue'] == 1)
        self.assertTrue(waiting['excluded'])
        self.assertIn(explanation, waiting['explanation'])

    def test_replanned_watcher_reports_hold_even_if_creation_needs_verification(self):
        self.assert_replanned_watcher_reports_exclusion(
            {'labels': ['bug', 'sfc-hold']}, 'held', creation_problem=True)

    def test_replanned_watcher_reports_closed_issue(self):
        self.assert_replanned_watcher_reports_exclusion({'state': 'CLOSED'}, 'is closed')

    def test_replanned_watcher_reports_done_stage(self):
        self.assert_replanned_watcher_reports_exclusion({'status': 'Done'}, 'already in Done')

    def assert_claims_p0_after_reconciliation(self, *, watching=False, competing=True):
        self.c.update(max_workers=1, max_cycles=1)
        arrived = ticket(2, status='Ready' if watching else 'Backlog', priority='P3' if watching else 'P0')
        self.gh.items = [ticket(1, status='Ready', priority='P3'), arrived] if competing else [arrived]
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        reconcile = self.factory.reconcile

        def reconcile_with_arrival(cards):
            arrived.update(status='Ready', priority='P0')
            return reconcile(cards)

        with patch.object(self.factory, 'reconcile', side_effect=reconcile_with_arrival), \
                patch.object(self.factory, 'process', side_effect=self.finish) as worker:
            if watching:
                self.watch()
            else:
                self.factory.run()
        worker.assert_called_once()
        self.assertEqual(worker.call_args.args[0]['number'], 2)
        self.assertEqual(self.gh.writes, [(2, 'Building'), (2, 'Done')])

    def test_finite_run_claims_newly_ready_p0_before_waiting_p3(self):
        self.assert_claims_p0_after_reconciliation()

    def test_watcher_claims_newly_promoted_p0_before_waiting_p3(self):
        self.assert_claims_p0_after_reconciliation(watching=True)

    def test_finite_run_claims_work_that_becomes_ready_before_exit(self):
        self.assert_claims_p0_after_reconciliation(competing=False)

    def test_pr_reconciliation_refreshes_queue_after_live_observation(self):
        self.gh.items = [ticket(1, status='Blocked')]
        self.factory.update(self.gh.items[0], pr=101)

        def observe_pr(number):
            self.assertEqual(number, 101)
            self.gh.items[0].update(status='Done', state='CLOSED')
            return 'OPEN'

        with patch.object(self.gh, 'pr_state', side_effect=observe_pr), \
                patch.object(self.factory, 'queue_cards', wraps=self.factory.queue_cards) as read:
            result = self.factory.run()
        self.assertEqual(result['outcome'], 'drained')
        self.assertEqual(read.call_count, 2)
        self.assertEqual(self.gh.writes, [])
        self.assertEqual(self.proc.lanes, [])

    def test_saved_p3_loses_actual_next_slot_to_fresh_p1_and_preserves_claim(self):
        self.c['max_workers'] = 1
        self.gh.items = [ticket(1, status='Backlog', priority='P3')]
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        with patch.object(self.factory, 'process', side_effect=Halt('interrupt')), self.assertRaises(Halt):
            self.watch()
        saved_session = self.session()
        saved = copy.deepcopy(self.factory.state['cards']['1'])
        self.gh.items.append(ticket(2, status='Backlog', priority='P1'))
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        order = []
        def worker(card):
            order.append(card['number'])
            if card['number'] == 2:
                self.assertEqual(self.factory.state['cards']['1'], saved)
            self.finish(card)
        self.options['max_polls'] = saved_session['settings']['max_polls']
        with patch.object(self.factory, 'process', side_effect=worker):
            self.watch(resume=saved_session['id'])
        self.assertEqual(order, [2, 1])
        self.assertEqual(self.gh.writes.count((1, 'Building')), 1)
        self.assertEqual(self.session()['deadline'], saved_session['deadline'])

    def refill(self, mode):
        self.c.update(max_workers=2, max_no_progress=1, max_cycles=6)
        self.options['max_polls'] = 20
        self.gh.items = [ticket(1, priority='P0'), ticket(2, priority='P1'), ticket(3, priority='P3')]
        if mode == 'scoped':
            for card in self.gh.items:
                card['status'] = 'Backlog'
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        b_started, c_started, b_finished = threading.Event(), threading.Event(), threading.Event()
        lock = threading.Lock()
        order, executing, peak = [], set(), []
        def worker(card):
            n = card['number']
            # Durable accounting must precede actual execution.
            record = read_json(self.factory.state_path)['cards'][str(n)]
            self.assertEqual(record['dispatch']['state'], 'launched')
            self.assertEqual(record['claim']['state'], 'verified')
            with lock:
                self.assertNotIn(n, executing)
                executing.add(n)
                order.append(n)
                peak.append(len(executing))
            try:
                if n == 1:
                    self.assertTrue(b_started.wait(5), 'Second slot was not filled')
                    # New highest-priority arrival after both slots are occupied.
                    self.gh.items.append(ticket(4, priority='P0', status='Backlog' if mode == 'scoped' else 'Ready'))
                elif n == 2:
                    b_started.set()
                    self.assertTrue(c_started.wait(5), 'Free slot was held behind slow peer')
                    b_finished.set()
                elif n == 4:
                    self.assertFalse(b_finished.is_set())
                    c_started.set()
                self.finish(card)
            finally:
                with lock:
                    executing.remove(n)
        with patch.object(self.factory, 'process', side_effect=worker):
            if mode == 'watch':
                self.watch()
            elif mode == 'scoped':
                self.factory.run(include_backlog=True, issues=(1, 2, 3, 4))
            else:
                self.factory.run()
        self.assertEqual(order[:3], [1, 2, 4])
        self.assertEqual(sorted(order), [1, 2, 3, 4])
        self.assertEqual(max(peak), 2)
        self.assertEqual(len([v for v in self.gh.writes if v[1] == 'Building']), 4)
        self.assertEqual(self.factory.state['no_progress'], 0)
        self.assertTrue(any(p['occupied'] for p in self.factory.state['plans']))

    def test_refill_watch_with_high_priority_arrival_before_slow_peer_finishes(self):
        self.refill('watch')

    def test_refill_ready_only_with_high_priority_arrival_before_slow_peer_finishes(self):
        self.refill('ready')

    def test_refill_scoped_backlog_with_high_priority_arrival_before_slow_peer_finishes(self):
        self.refill('scoped')

    def admission_change(self, mode, change, at_claim=False):
        self.c.update(max_workers=1, max_cycles=3)
        stage = 'Backlog' if mode == 'scoped' else 'Ready'
        self.gh.items = [ticket(1, status=stage, priority='P1'),
                         ticket(2, status=stage, priority='P2'),
                         ticket(3, status='Backlog', priority='P0')]
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        changed = False
        original = self.factory.claim_priority if at_claim else self.factory.plan
        def mutate(value):
            nonlocal changed
            if at_claim:
                if not changed:
                    self.gh.items[0].update(change)
                    changed = True
                return original(value)
            result = original(value)
            if not changed:
                self.assertEqual(result[0][0]['number'], 1)
                self.gh.items[0].update(change)
                changed = True
            return result
        order = []
        def worker(card):
            order.append(card['number'])
            self.finish(card)
        with patch.object(self.factory, 'claim_priority' if at_claim else 'plan', side_effect=mutate), \
                patch.object(self.factory, 'process', side_effect=worker):
            result = (self.factory.run(include_backlog=True, issues=(1, 2))
                      if mode == 'scoped' else self.factory.run())
        self.assertEqual(order, [2])
        self.assertEqual(result['outcome'], 'waiting')
        self.assertEqual(self.gh.writes, [(2, 'Building'), (2, 'Done')])
        self.assertEqual(self.gh.items[2]['status'], 'Backlog')
        self.assertEqual(self.factory.state['cycle'], 2 if at_claim else 1)
        self.assertEqual(self.factory.state['no_progress'], 0)
        self.assertIn('Admission deferred', str(self.factory.state['waiting']))
        self.assertNotIn('claim', self.factory.record(self.gh.items[0]))

    def test_ready_priority_change_during_idle_admission_keeps_other_work_running(self):
        self.admission_change('ready', {'priority': 'P3'})

    def test_scoped_priority_change_during_idle_admission_keeps_other_work_running(self):
        self.admission_change('scoped', {'priority': 'P3'})

    def test_ready_stage_change_during_idle_admission_keeps_other_work_running(self):
        self.admission_change('ready', {'status': 'Blocked'})

    def test_scoped_stage_change_during_idle_admission_keeps_other_work_running(self):
        self.admission_change('scoped', {'status': 'Blocked'})

    def test_ready_claim_revalidation_defers_only_changed_card_and_keeps_spending(self):
        self.admission_change('ready', {'priority': 'P3'}, at_claim=True)

    def test_scoped_claim_revalidation_defers_only_changed_card_and_keeps_spending(self):
        self.admission_change('scoped', {'priority': 'P3'}, at_claim=True)

    def test_finite_admission_churn_defers_each_card_once_and_later_run_rechecks(self):
        self.c.update(max_workers=1, max_cycles=2, max_no_progress=1)
        self.gh.items = [ticket(1, priority='P1'), ticket(2, priority='P2')]
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        original = self.factory.plan
        def change_each_selection(cards):
            result = original(cards)
            if result[0]:
                number = result[0][0]['number']
                next(c for c in self.gh.items if c['number'] == number)['title'] += ' changed'
            return result
        with patch.object(self.factory, 'plan', side_effect=change_each_selection) as plan, \
                patch.object(self.factory, 'process') as worker:
            self.assertEqual(self.factory.run()['outcome'], 'waiting')
        self.assertEqual(plan.call_count, 3)
        self.assertEqual(self.factory.admission_deferred, set())
        worker.assert_not_called()
        self.assertEqual(self.gh.writes + self.gh.activity_writes, [])
        self.assertEqual(self.factory.state['cards']['1'].get('dispatch'), None)
        self.assertEqual(self.factory.state['cards']['2'].get('dispatch'), None)
        order = []
        def worker(card):
            order.append(card['number'])
            self.finish(card)
        with patch.object(self.factory, 'process', side_effect=worker):
            self.factory.run()
        self.assertEqual(order, [1, 2])
        self.assertEqual(self.factory.state['cycle'], 2)
        self.assertEqual(self.factory.state['no_progress'], 0)

    def test_creation_becomes_unverified_at_admission_does_not_abandon_peer(self):
        self.c['max_workers'] = 1
        self.gh.items = [ticket(1, priority='P1'), ticket(2, priority='P2')]
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        original_plan = self.factory.plan
        selected = False
        def plan(cards):
            nonlocal selected
            result = original_plan(cards)
            selected = True
            return result
        original_creation = self.factory.creation_problem
        def creation(card):
            if selected and card['number'] == 1:
                return 'Ticket creation unverified'
            return original_creation(card)
        order = []
        def worker(card):
            order.append(card['number'])
            self.finish(card)
        with patch.object(self.factory, 'plan', side_effect=plan), \
                patch.object(self.factory, 'creation_problem', side_effect=creation), \
                patch.object(self.factory, 'process', side_effect=worker):
            self.assertEqual(self.factory.run()['outcome'], 'waiting')
        self.assertEqual(order, [2])
        self.assertEqual(self.factory.state['cycle'], 1)
        self.assertEqual(self.gh.writes, [(2, 'Building'), (2, 'Done')])

    def test_active_poll_reads_holds_priority_and_reopened_dependencies_without_extra_worker(self):
        self.c.update(max_workers=2, max_cycles=3, max_no_progress=1)
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        self.options.update(max_polls=20, excluded_issues=[6])
        self.clock.tick_active = True
        self.gh.items = [ticket(1, priority='P0'), ticket(2, priority='P0'),
                         ticket(3, priority='P1'), ticket(4, priority='P1'),
                         ticket(5, priority='P3'), ticket(6, priority='P0')]
        self.gh.items[3]['body'] += '## Blocked by\n- #9'
        self.gh.extra_issues[9] = ticket(9, state='CLOSED')
        both = threading.Barrier(2)
        release_a, release_b = threading.Event(), threading.Event()
        order = []
        def changes():
            if self.clock() < 1001 or not both.broken and both.n_waiting:
                return
            self.gh.items[2]['labels'].append('sfc-hold')
            self.gh.extra_issues[9]['state'] = 'OPEN'
            self.gh.items[4]['priority'] = 'P1'
            release_a.set()
        self.clock.on_wait = changes
        def worker(card):
            n = card['number']
            order.append(n)
            if n in (1, 2):
                both.wait(5)
                self.assertTrue((release_a if n == 1 else release_b).wait(5))
            else:
                self.assertEqual(n, 5)
                self.assertFalse(release_b.is_set())
                release_b.set()
            self.finish(card)
        with patch.object(self.factory, 'process', side_effect=worker):
            self.watch()
        self.assertEqual(order, [1, 2, 5])
        self.assertEqual(self.session()['cycles'], 3)
        self.assertGreaterEqual(self.session()['polls'], 4)
        self.assertEqual(self.gh.items[2]['labels'][-1], 'sfc-hold')
        self.assertEqual(self.gh.items[4]['priority'], 'P1')

    def test_missing_priority_setup_in_ready_run_is_read_only_halt(self):
        self.gh.fields.pop()
        with patch.object(self.factory, 'process') as worker, self.assertRaisesRegex(Halt, 'Priority field missing'):
            self.factory.run()
        worker.assert_not_called()
        self.assertEqual(self.gh.writes + self.gh.activity_writes, [])

    def test_preview_observes_occupied_slots_and_next_choice_during_work(self):
        self.c.update(max_workers=2, max_cycles=2)
        self.clock.tick_active = True
        self.gh.items = [ticket(1, priority='P0'), ticket(2, priority='P1'), ticket(3, priority='P2')]
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        both = threading.Barrier(2)
        started = threading.Event()
        release = threading.Event()
        snapshots = []
        def observe():
            if not started.is_set() or release.is_set():
                return
            preview = Factory(self.root, self.c, self.gh, self.proc)
            before = self.factory.state_path.read_bytes()
            snapshots.append(preview.watch(dry_run=True))
            self.assertEqual(self.factory.state_path.read_bytes(), before)
            self.assertEqual(preview.occupied, {})
            release.set()
        self.clock.on_wait = observe
        def worker(card):
            both.wait(5)
            started.set()
            self.assertTrue(release.wait(5))
            self.finish(card)
        with patch.object(self.factory, 'process', side_effect=worker):
            self.watch()
        self.assertEqual(len(snapshots), 1)
        preview = snapshots[0]
        self.assertEqual([v['number'] for v in preview['occupied']], [1, 2])
        self.assertEqual(preview['selected'], [])
        self.assertEqual(preview['next_eligible'][0]['number'], 3)
        self.assertIn('recorded', preview['occupancy_source'])
        self.assertIn('max_workers capacity', str(preview['waiting']))
        self.assertEqual(self.session()['occupied'], [])

    def test_two_active_cutoffs_keep_original_spending_and_do_not_admit_third(self):
        self.c.update(max_workers=2, max_cycles=8)
        self.clock.tick_active = True
        self.gh.items = [ticket(1), ticket(2), ticket(3)]
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        both = threading.Barrier(2)
        started = threading.Event()
        cutoff = threading.Event()
        def end_window():
            if not started.is_set() or cutoff.is_set():
                return
            self.clock.value = self.session()['deadline']
            cutoff.set()
        self.clock.on_wait = end_window
        def worker(card):
            both.wait(5)
            started.set()
            self.assertTrue(cutoff.wait(5))
            raise Stopped('original work deadline')
        with patch.object(self.factory, 'process', side_effect=worker) as process:
            self.assertEqual(self.watch()['outcome'], 'watch-time-limit')
        self.assertEqual(process.call_count, 2)
        before = self.session()
        self.assertEqual((before['cycles'], before['claims'], before['no_progress']), (2, 2, 2))
        self.assertEqual(self.gh.items[2]['status'], 'Ready')
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        with patch.object(self.factory, 'process') as process:
            self.assertEqual(self.watch(resume=before['id'])['outcome'], 'watch-time-limit')
        process.assert_not_called()
        after = self.session()
        for key in ('deadline', 'cycles', 'claims', 'no_progress'):
            self.assertEqual(after[key], before[key])
        self.assertTrue(all(r['dispatch']['state'] == 'launched' for r in self.factory.state['cards'].values()))

    def test_new_ci_wait_is_progress_and_leaves_capacity_for_another_ticket(self):
        self.c.update(max_workers=1, max_no_progress=1, merge_policy='auto')
        self.options['max_polls'] = 8
        self.gh.items = [ticket(1), ticket(2)]
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        original_pr = self.gh.pr
        def pr(number):
            value = original_pr(number)
            if number == 101:
                value['statusCheckRollup'] = [{'state': 'PENDING'}]
            return value
        with patch.object(self.gh, 'pr', side_effect=pr), \
                patch.object(self.factory, 'merge_gate', side_effect=lambda card, *args: self.finish(card)):
            self.watch(start=True, allow_merge=True)
        self.assertEqual([card['status'] for card in self.gh.items], ['Review', 'Done'])
        self.assertEqual(self.proc.lanes.count('build'), 2)
        self.assertEqual(self.proc.lanes.count('review'), 2)
        self.assertEqual(self.session()['no_progress'], 0)
        self.assertEqual(self.session()['cycles'], 2)

    def test_parallel_real_lanes_serialize_merges_and_reverify_moved_base(self):
        self.c.update(max_workers=2, max_cycles=10, max_no_progress=1,
                      merge_policy='auto', merge_mode='local-verification')
        self.options['max_polls'] = 30
        self.gh = PriorityAtomicGitHub(self.root, self.remote, [ticket(1), ticket(2)])
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        builds = threading.Barrier(2)
        reviews = threading.Barrier(2)
        def lane(name, cwd):
            if name == 'build' and self.proc.lanes.count('build') <= 2:
                # Independent changes must remain after integrating the peer;
                # identical value.txt edits correctly trip the empty-diff gate.
                Path(cwd, Path(cwd).name + '.txt').write_text('independent change\n')
                builds.wait(5)
            if name == 'review' and self.proc.lanes.count('review') <= 2:
                # Both initial reviews pin the old base before either merge;
                # the loser must integrate and obtain fresh independent lanes.
                reviews.wait(5)
        self.proc.on_lane = lane
        original = self.gh.atomic_fast_forward
        bases = []
        def atomic(intent):
            self.assertTrue(self.factory.merge_mutex.locked())
            bases.append(intent['base'])
            return original(intent)
        with patch.object(self.gh, 'atomic_fast_forward', side_effect=atomic):
            self.watch(start=True, allow_merge=True)
        self.assertEqual(len(self.gh.applied), 2)
        self.assertNotEqual(bases[0], bases[1])
        self.assertEqual([c['status'] for c in self.gh.items], ['Done', 'Done'])
        self.assertGreaterEqual(self.proc.lanes.count('qa'), 3)
        for card in self.gh.items:
            record = self.factory.record(card)
            self.assertEqual(record['local_merge']['phase'], 'done')
            self.assertTrue(self.factory.pinned(card, record['head'], record['base']))


if __name__ == '__main__':
    unittest.main()
