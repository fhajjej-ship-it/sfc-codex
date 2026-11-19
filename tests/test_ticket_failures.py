"""Ticket-local limits cannot kill an otherwise authorized observer."""
import copy
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

import test_factory as fixtures
import test_watch as watching
from test_pickup import ticket
from sfc_lib.common import Halt, Stopped, TimedOut
from sfc_lib.factory import Factory
from sfc_lib.watch import Watch


class TicketFailureTests(unittest.TestCase):
    setUp = watching.WatchTests.setUp
    tearDown = fixtures.IntegrationTests.tearDown
    watch = watching.WatchTests.watch
    finish = watching.WatchTests.finish
    session = watching.WatchTests.session

    def timeout_refills(self, verification=False):
        self.c.update(max_workers=2, max_no_progress=1, max_cycles=5)
        self.options['max_polls'] = 10
        self.gh.items = [ticket(1, priority='P0'), ticket(2, priority='P1'), ticket(3, priority='P2')]
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        slow_started, replacement_started = threading.Event(), threading.Event()
        original_execute = self.proc.execute
        calls, active, peaks = [], set(), []
        lock = threading.Lock()

        def execute(argv, cwd, log, *args, **kwargs):
            if Path(cwd).name == 'issue-1':
                Path(log).parent.mkdir(parents=True, exist_ok=True)
                Path(log).write_text('Owned timeout fixture; raw evidence retained\n')
                self.assertTrue(slow_started.wait(5))
                raise TimedOut('Owned verification timeout' if verification else 'Owned worker timeout')
            return original_execute(argv, cwd, log, *args, **kwargs)

        def work(card):
            n = card['number']
            with lock:
                active.add(n); calls.append(n); peaks.append(len(active))
            try:
                if n == 1:
                    wt = self.factory.worktree(card)
                    base = self.factory.record(card)['base']
                    if verification:
                        self.factory.verify(card, wt, base)
                    else:
                        self.factory.lane(card, 'build', wt, base)
                    self.fail('Timeout must not return a successful result')
                elif n == 2:
                    slow_started.set()
                    self.assertTrue(replacement_started.wait(5), 'Timeout held the free slot or killed its peer')
                    self.finish(card)
                else:
                    self.assertEqual(self.factory.state['cards']['1']['status'], 'Blocked')
                    self.assertIn(2, active)
                    replacement_started.set()
                    self.finish(card)
            finally:
                with lock:
                    active.remove(n)

        with patch.object(self.proc, 'execute', side_effect=execute), \
                patch.object(self.factory, '_process', side_effect=work):
            result = self.watch()
        self.assertEqual(result['outcome'], 'watch-polls-limit')
        self.assertEqual(calls, [1, 2, 3])
        self.assertEqual(max(peaks), 2)
        self.assertEqual([c['status'] for c in self.gh.items], ['Blocked', 'Done', 'Done'])
        r = self.factory.state['cards']['1']
        self.assertEqual(r['attempts'], 0)
        self.assertEqual(r['ticket_failure']['phase'], 'blocked')
        self.assertEqual(r['ticket_failure']['action'], 'timeout')
        self.assertTrue(Path(r['worktree']).is_dir())
        self.assertTrue(Path(r['ticket_failure']['receipt']).is_file())
        self.assertEqual(r['dispatch']['state'], 'finished')
        self.assertTrue(r['dispatch']['isolated_ticket_outcome'])
        self.assertFalse(self.factory.pinned(self.gh.items[0], r.get('head'), r.get('base')))
        if not verification:
            budget = r['worker_output_budget']['build']
            self.assertEqual(len(budget['dispatches']), 1)
            self.assertEqual(budget['dispatches'][0]['status'], 'reserved')
            self.assertIn('Owned timeout fixture', Path(budget['dispatches'][0]['log']).read_text())
            self.assertFalse(Path(budget['dispatches'][0]['result_path']).exists())
        self.assertEqual(self.session()['no_progress'], 0)
        self.assertEqual(self.session()['cycles'], 3)
        self.assertFalse(self.factory.processes.stop_path.exists())

    def test_owned_worker_timeout_blocks_only_ticket_and_refills_before_peer_finishes(self):
        self.timeout_refills()

    def test_owned_verification_timeout_blocks_only_ticket_and_keeps_observing(self):
        self.timeout_refills(verification=True)

    def test_findings_limits_are_per_ticket_and_future_arrivals_still_start(self):
        self.c.update(max_workers=1, max_no_progress=1, max_cycles=8)
        self.options['max_polls'] = 20
        self.gh.items = [ticket(1), ticket(2)]
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        calls = []
        def work(card):
            calls.append(card['number'])
            if card['number'] in (1, 2):
                self.factory.fail(card, {'verdict': 'fail', 'summary': 'Independent fixture findings'})
            else:
                self.finish(card)
        added = False
        def arrival():
            nonlocal added
            if not added and all(c['status'] == 'Blocked' for c in self.gh.items):
                added = True
                self.gh.items.append(ticket(3))
        self.clock.on_wait = arrival
        with patch.object(self.factory, '_process', side_effect=work):
            self.assertEqual(self.watch()['outcome'], 'watch-polls-limit')
        self.assertEqual(calls, [1, 1, 2, 2, 3])
        self.assertEqual([self.factory.state['cards'][str(n)]['attempts'] for n in (1, 2)], [2, 2])
        self.assertEqual([c['status'] for c in self.gh.items], ['Blocked', 'Blocked', 'Done'])
        self.assertEqual(self.session()['no_progress'], 0)
        self.assertEqual(self.session()['cycles'], 5)

    def test_output_exhaustion_does_not_launch_or_reset_failed_ticket(self):
        self.c.update(max_workers=1, max_no_progress=1)
        self.options['max_polls'] = 6
        self.gh.items = [ticket(1), ticket(2)]
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        original_budget = {'limit': 2, 'dispatches': [{'status': 'reserved'}, {'status': 'malformed'}]}
        calls = []
        def work(card):
            calls.append(card['number'])
            if card['number'] == 1:
                wt = self.factory.worktree(card)
                r = self.factory.record(card)
                r['worker_output_budget'] = {'build': copy.deepcopy(original_budget)}
                self.factory.save()
                self.factory.lane(card, 'build', wt, r['base'])
                self.fail('Exhausted output limit must not launch')
            else:
                self.finish(card)
        with patch.object(self.factory, '_process', side_effect=work):
            self.assertEqual(self.watch()['outcome'], 'watch-polls-limit')
        self.assertEqual(calls, [1, 2])
        r = self.factory.state['cards']['1']
        self.assertEqual(r['worker_output_budget']['build'], original_budget)
        self.assertEqual(r['ticket_failure']['action'], 'output_budget')
        self.assertEqual(self.proc.lanes, [])
        self.assertEqual(self.session()['no_progress'], 0)

    def test_timeout_after_lane_progress_does_not_refund_existing_observer_spending(self):
        self.c.update(max_workers=1, max_no_progress=2)
        self.options['max_polls'] = 6
        self.gh.items = [ticket(1), ticket(2)]
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        original_begin = Watch.begin
        def begin(watcher, allow_merge):
            original_begin(watcher, allow_merge)
            watcher.session['no_progress'] = 1  # Retained spending from an earlier interruption.
            watcher.save('retained fixture spending')
        def work(card):
            if card['number'] == 1:
                wt = self.factory.worktree(card)
                self.factory.update(card, receipts={'build': {'time': 'new accepted lane receipt'}})
                self.factory.verify(card, wt, self.factory.record(card)['base'])
            else:
                # Prior observer spending plus this one live reservation.
                self.assertEqual(self.session()['no_progress'], 2)
                self.finish(card)
        with patch.object(Watch, 'begin', new=begin), \
                patch.object(self.proc, 'execute', side_effect=TimedOut('owned check timeout')), \
                patch.object(self.factory, '_process', side_effect=work):
            self.watch()
        self.assertEqual([c['status'] for c in self.gh.items], ['Blocked', 'Done'])

    def test_timeout_with_uncertain_merge_remains_a_global_failure(self):
        self.c['max_workers'] = 1
        self.gh.items = [ticket(1), ticket(2)]
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        calls = []
        def work(card):
            calls.append(card['number'])
            self.factory.record(card)['local_merge'] = {'phase': 'unresolved'}
            raise TimedOut('Uncertain merge must not be hidden by ticket isolation')
        with patch.object(self.factory, '_process', side_effect=work), self.assertRaises(TimedOut):
            self.watch()
        self.assertEqual(calls, [1])
        self.assertNotIn('ticket_failure', self.factory.state['cards']['1'])
        self.assertEqual(self.gh.items[1]['status'], 'Ready')

    def test_operator_stop_precedes_ticket_timeout_isolation(self):
        self.c['max_workers'] = 1
        self.gh.items = [ticket(1), ticket(2)]
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        def work(card):
            self.factory.processes.stop_path.touch()
            raise TimedOut('Stop arrived while timeout was being handled')
        with patch.object(self.factory, '_process', side_effect=work), self.assertRaises(Stopped):
            self.watch()
        self.assertNotIn('ticket_failure', self.factory.state['cards']['1'])
        self.assertEqual(self.gh.items[1]['status'], 'Ready')

    def test_incomplete_failure_publication_latches_and_cannot_admit_other_work(self):
        self.c['max_workers'] = 1
        self.gh.items = [ticket(1), ticket(2)]
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        original_move = self.gh.move
        def move(card, stage):
            if stage == 'Blocked':
                raise Halt('Ambiguous blocked-stage write')
            return original_move(card, stage)
        with patch.object(self.gh, 'move', side_effect=move), \
                patch.object(self.factory, '_process', side_effect=TimedOut('owned timeout')), \
                self.assertRaisesRegex(Halt, 'Ambiguous'):
            self.watch()
        self.assertEqual(self.factory.state['cards']['1']['ticket_failure']['phase'], 'intent')
        self.assertEqual(self.gh.items[1]['status'], 'Ready')
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        with patch.object(self.factory, '_process') as worker, self.assertRaisesRegex(Halt, 'Unresolved ticket-failure'):
            self.watch()
        worker.assert_not_called()


if __name__ == '__main__':
    unittest.main()
