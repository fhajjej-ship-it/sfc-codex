"""Retained recovery authority, with real temporary Git and fake workers/GitHub."""
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import test_factory as fixtures
import test_corrective_recovery as corrective_fixtures
from test_pickup import PickupGitHub, ticket
from test_watch import Clock
from sfc_lib.common import Halt, Stopped, TimedOut, git, read_json, write_json
from sfc_lib.factory import Factory
from sfc_lib.ticket_recovery import grant


class RetainedRecoveryTests(unittest.TestCase):
    setUp = fixtures.IntegrationTests.setUp
    tearDown = fixtures.IntegrationTests.tearDown
    rejected = corrective_fixtures.CorrectiveTests.rejected

    def timed_out(self):
        self.c['auto_max_lines'] = 800
        self.gh = PickupGitHub(self.root, [ticket()])
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        execute = self.proc.execute
        def timeout(argv, cwd, log, *args, **kwargs):
            if argv[0] == 'codex':
                Path(cwd, 'retained.txt').write_text('Retained interrupted work\n')
                Path(log).write_text('Owned interrupted Build\n')
                raise TimedOut('Fixture Build timed out')
            return execute(argv, cwd, log, *args, **kwargs)
        with patch.object(self.proc, 'execute', side_effect=timeout):
            with self.assertRaises(TimedOut):
                self.factory.run(include_backlog=True, issues=(1,))
        self.card = self.gh.cards()[0]
        self.r = self.factory.record(self.card)
        (self.factory.dir / 'stop').touch()
        self.old = copy.deepcopy(self.r['worker_output_budget'])

    def conflicted(self):
        self.c['auto_max_lines'] = 800
        self.rejected(once=True)
        (self.factory.dir / 'stop').unlink()
        Path(self.root, 'value.txt').write_text('Conflicting new base\n')
        git(self.root, 'add', 'value.txt')
        git(self.root, 'commit', '-m', 'Independent base change')
        git(self.root, 'push', 'origin', 'main')
        self.assertFalse(self.factory.qa(self.card, Path(self.r['worktree'])))
        self.assertEqual(self.r['attempts'], 2)
        self.assertEqual(self.card['status'], 'Blocked')
        (self.factory.dir / 'stop').touch()
        self.old = copy.deepcopy(self.r['worker_output_budget'])

    def recover(self, apply=True, request='retained-test'):
        return grant(self.factory, issue=1, request_id=request,
                     reason='User requested recovery of this saved ticket through the watcher', grant_one=apply)

    def model_rejected(self):
        self.gh = PickupGitHub(self.root, [ticket()])
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        execute = self.proc.execute
        message = "The 'unsupported-model' model is not supported when using Codex with a ChatGPT account."
        def reject(argv, cwd, log, *args, **kwargs):
            if argv[0] == 'codex':
                Path(cwd, 'retained.txt').write_text('Preserve saved implementation\n')
                Path(log).write_text('\n'.join(json.dumps(e) for e in (
                    {'type': 'thread.started', 'thread_id': 'fixture'},
                    {'type': 'item.completed', 'item': {'type': 'error', 'message':
                        'Configured service tier `priority` is not advertised as supported for model `unsupported-model` and will be omitted from requests.'}},
                    {'type': 'item.completed', 'item': {'type': 'error', 'message':
                        'Model metadata for `unsupported-model` not found. Defaulting to fallback metadata; this can degrade performance and cause issues.'}},
                    {'type': 'turn.started'},
                    {'type': 'error', 'message': message},
                    {'type': 'turn.failed', 'error': {'message': message}})) + '\n')
                return 1
            return execute(argv, cwd, log, *args, **kwargs)
        with patch.object(self.proc, 'execute', side_effect=reject):
            with self.assertRaisesRegex(Halt, 'build worker failed'):
                self.factory.run(include_backlog=True, issues=(1,))
        self.card = self.gh.cards()[0]
        self.r = self.factory.record(self.card)
        (self.factory.dir / 'stop').touch()
        self.old = copy.deepcopy(self.r['worker_output_budget'])

    def test_model_startup_requires_explicit_replacement_and_preserves_spent_build(self):
        self.model_rejected()
        with self.assertRaisesRegex(Halt, 'explicit replacement'):
            self.recover(False)
        self.c['model'] = 'unsupported-model'
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        with self.assertRaisesRegex(Halt, 'explicit replacement'):
            self.recover(False)
        self.c['model'] = 'gpt-6.1-sol'
        write_json(self.factory.dir / 'config.json', self.c)
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        before = self.factory.state_path.read_bytes()
        preview = self.recover(False)
        self.assertEqual(preview['grant']['kind'], 'model-startup')
        self.assertEqual(self.factory.state_path.read_bytes(), before)
        self.recover()
        self.watch()
        record = self.factory.record(self.card)
        self.assertEqual(self.proc.lanes, ['build', 'qa', 'security', 'review'])
        self.assertEqual(record['worker_output_budget']['build']['dispatches'][:-1], self.old['build']['dispatches'])
        self.assertEqual(record['ticket_recoveries'][0]['status'], 'spent')
        self.assertEqual(Path(record['worktree'], 'retained.txt').read_text(), 'Preserve saved implementation\n')
        self.assertEqual(self.c['merge_policy'], 'human')

    def test_model_startup_rejects_work_events_unknown_failure_and_dispatch_tampering(self):
        self.model_rejected()
        self.c['model'] = 'gpt-6.1-sol'
        write_json(self.factory.dir / 'config.json', self.c)
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        d = self.old['build']['dispatches'][-1]
        log = Path(d['log'])
        original = log.read_text()
        for text in (original + json.dumps({'type': 'item.started', 'item': {}}) + '\n',
                     original.replace('Model metadata for `unsupported-model`', 'Unexpected diagnostic for `unsupported-model`'),
                     original.replace('"type": "error", "message": "Model metadata', '"type": "agent_message", "message": "Model metadata'),
                     original.replace('not supported', 'temporarily unavailable')):
            log.write_text(text)
            with self.assertRaises(Halt):
                self.recover(False)
        log.write_text(original)
        path = log.parent / 'dispatch.json'
        original_dispatch = path.read_bytes()
        value = read_json(path)
        value['head'] = '0' * 40
        write_json(path, value)
        with self.assertRaisesRegex(Halt, 'dispatch/output'):
            self.recover(False)
        path.write_bytes(original_dispatch)
        self.recover()
        log.write_text(original + 'Late evidence\n')
        with self.assertRaisesRegex(Halt, 'pins/evidence'):
            self.watch()
        self.assertEqual(self.proc.lanes, [])

    def failed_checks(self):
        self.c['auto_max_lines'] = 800
        self.gh = PickupGitHub(self.root, [ticket()])
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        execute = self.proc.execute
        def fail_check(argv, cwd, log, *args, **kwargs):
            if argv[0] != 'codex':
                Path(log).write_text('Actual fixture project check rejected the candidate\n')
                return 1
            return execute(argv, cwd, log, *args, **kwargs)
        with patch.object(self.proc, 'execute', side_effect=fail_check):
            self.factory.run(include_backlog=True, issues=(1,))
        self.card = self.gh.cards()[0]
        self.r = self.factory.record(self.card)
        self.assertEqual(self.r['status'], 'Blocked')
        self.assertEqual(self.r['attempts'], 2)
        (self.factory.dir / 'stop').touch()
        self.old = copy.deepcopy(self.r['worker_output_budget'])

    def blocked_or_malformed(self, malformed=False):
        self.gh = PickupGitHub(self.root, [ticket()])
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        execute = self.proc.execute
        def blocked(argv, *args, **kwargs):
            code = execute(argv, *args, **kwargs)
            if argv[0] == 'codex':
                path = Path(argv[argv.index('--output-last-message') + 1])
                value = read_json(path)
                value['verdict'] = 'blocked'
                write_json(path, value)
            return code
        if malformed:
            self.proc.malformed = True
            with self.assertRaisesRegex(Halt, 'Malformed build'):
                self.factory.run(include_backlog=True, issues=(1,))
            self.proc.malformed = False
        else:
            with patch.object(self.proc, 'execute', side_effect=blocked):
                self.factory.run(include_backlog=True, issues=(1,))
        self.card = self.gh.cards()[0]
        self.r = self.factory.record(self.card)
        if malformed:
            old = []
            for n in range(2):
                folder = self.factory.dir / 'runs' / f'old-{n}' / 'issue-1' / 'build-old'
                folder.mkdir(parents=True)
                (folder / 'result.json').write_text('{}')
                (folder / 'worker.log').write_text('Prior malformed output\n')
                old.append({'status': 'legacy-unaccepted', 'result_path': str(folder / 'result.json'),
                            'log': str(folder / 'worker.log')})
            self.r['worker_output_budget']['build']['dispatches'][:0] = old
            self.r['worker_output_recovery'] = {'build': {'status': 'spent', 'request_id': 'prior-supplement'}}
            self.factory.save()
        (self.factory.dir / 'stop').touch()
        self.old = copy.deepcopy(self.r['worker_output_budget'])

    def watch(self):
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        clock = Clock()
        clock.active = lambda: bool(self.factory.occupied)
        return self.factory.watch(start=True, clock=clock, wait=clock.wait,
                                  overrides={'poll_interval': 1, 'max_polls': 3,
                                             'max_seconds': 60, 'max_claims': 10})

    def test_timeout_retains_dirty_untracked_work_and_old_spending_then_watcher_runs_all_lanes(self):
        self.timed_out()
        before = self.factory.state_path.read_bytes()
        writes = len(self.gh.writes)
        self.assertEqual(self.recover(False)['state'], 'preview')
        self.assertEqual(self.factory.state_path.read_bytes(), before)
        self.assertEqual(len(self.gh.writes), writes)
        self.recover()
        self.watch()
        r = self.factory.record(self.card)
        self.assertEqual(r['attempts'], 0)
        self.assertEqual(r['ticket_recoveries'][0]['status'], 'spent')
        self.assertEqual(self.proc.lanes, ['build', 'qa', 'security', 'review'])
        self.assertEqual(r['worker_output_budget']['build']['dispatches'][:-1], self.old['build']['dispatches'])
        self.assertEqual(r['worker_output_budget']['build']['dispatches'][-1]['ticket_recovery_id'], 'retained-test')
        self.assertEqual(Path(r['worktree'], 'retained.txt').read_text(), 'Retained interrupted work\n')
        self.assertTrue(r['verification']['passed'])
        self.assertEqual(self.c['auto_max_lines'], 800)

    def test_supervisor_conflict_with_exhausted_attempts_gets_one_build_and_fresh_reviews(self):
        self.conflicted()
        old_pr = self.r['pr']
        self.recover()
        count = len(self.proc.lanes)
        self.watch()
        r = self.factory.record(self.card)
        self.assertEqual(self.proc.lanes[count:], ['build', 'qa', 'security', 'review'])
        self.assertEqual(r['attempts'], 2)
        self.assertEqual(r['pr'], old_pr)
        self.assertEqual(r['ticket_recoveries'][0]['status'], 'spent')
        self.assertEqual(r['worker_output_budget']['build']['dispatches'][:-1], self.old['build']['dispatches'])
        self.assertEqual(set(r['receipts']), {'qa', 'security', 'review'})
        self.assertTrue(r['verification']['passed'])
        self.assertFalse(r.get('pending_base'))

    def test_dirty_file_and_late_missing_output_drift_reject_before_launch(self):
        self.timed_out(); self.recover()
        Path(self.r['worktree'], 'retained.txt').write_text('Changed later\n')
        with self.assertRaisesRegex(Halt, 'pins/evidence'):
            self.watch()
        self.assertEqual(self.proc.lanes, [])
        self.assertEqual(self.factory.record(self.card)['ticket_recoveries'][0]['status'], 'available')

    def test_base_or_pr_drift_rejects_before_build(self):
        self.conflicted(); self.recover()
        self.gh.external_head = '0' * 40
        before = len(self.proc.lanes)
        with self.assertRaisesRegex(Halt, 'PR state'):
            self.watch()
        self.assertEqual(len(self.proc.lanes), before)

    def test_failure_spends_only_one_recovery_and_does_not_retry(self):
        self.timed_out(); self.recover(); self.proc.fail_lane = 'build'
        self.watch()
        r = self.factory.record(self.card)
        self.assertEqual(self.proc.lanes, ['build'])
        self.assertEqual(r['attempts'], 1)
        self.assertEqual(r['status'], 'Blocked')
        self.assertEqual(r['ticket_recoveries'][0]['status'], 'spent')
        self.assertEqual(r['worker_output_budget']['build']['dispatches'][:-1], self.old['build']['dispatches'])

    def test_uncertain_admission_never_repeats_board_write(self):
        self.conflicted(); self.recover()
        move = self.gh.move
        def lost(card, status):
            move(card, status)
            raise Halt('Lost recovery admission response')
        before = len(self.proc.lanes)
        with patch.object(self.gh, 'move', side_effect=lost):
            with self.assertRaisesRegex(Halt, 'Lost recovery'):
                self.watch()
        writes = len(self.gh.writes)
        with self.assertRaisesRegex(Halt, 'Unresolved recovery admission'):
            self.watch()
        self.assertEqual(len(self.gh.writes), writes)
        self.assertEqual(len(self.proc.lanes), before)

    def test_missing_journal_is_not_dispatch_authority(self):
        self.timed_out(); self.recover()
        (self.factory.dir / 'recoveries' / 'retained-test.json').unlink()
        with self.assertRaises((Halt, FileNotFoundError)):
            self.watch()
        self.assertEqual(self.proc.lanes, [])

    def test_late_output_invalidates_the_pinned_absence(self):
        self.timed_out(); self.recover()
        original = self.r['worker_output_budget']['build']['dispatches'][0]
        Path(original['result_path']).write_text('{"verdict":"pass"}')
        with self.assertRaisesRegex(Halt, 'pins/evidence'):
            self.watch()
        self.assertEqual(self.proc.lanes, [])

    def test_recovery_cannot_ignore_hold_or_800_line_policy(self):
        self.timed_out(); self.recover()
        self.gh.items[0]['labels'].append('sfc-hold')
        self.watch()
        self.assertEqual(self.proc.lanes, [])
        self.gh.items[0]['labels'].remove('sfc-hold')
        create = self.gh.create_pr
        def large(card, branch, body):
            value = create(card, branch, body)
            self.gh.prs[value['number']]['additions'] = 801
            return value
        with patch.object(self.gh, 'create_pr', side_effect=large):
            self.watch()
        self.assertEqual(self.proc.lanes, ['build', 'qa', 'security', 'review'])
        self.assertEqual(self.factory.record(self.card)['status'], 'Blocked')
        self.assertIn('configured automatic merge limit: 800', self.gh.notes[-1])
        self.assertEqual(self.gh.merges, [])

    def test_consumed_grant_cannot_reserve_a_second_build(self):
        self.timed_out(); self.recover(); self.watch()
        r = self.factory.record(self.card)
        before = len(self.proc.lanes)
        with self.assertRaisesRegex(Halt, 'already reserved'):
            self.factory.lane(self.card, 'build', Path(r['worktree']), r['base'],
                              ticket_recovery_id='retained-test')
        self.assertEqual(len(self.proc.lanes), before)

    def test_failed_project_checks_recover_existing_pr_without_refunding_attempts(self):
        self.failed_checks(); grant = self.recover()
        self.assertEqual(grant['grant']['kind'], 'checks')
        self.assertTrue(any('verify-' in e['path'] for e in grant['grant']['binding']['evidence']))
        count = len(self.proc.lanes)
        self.watch()
        r = self.factory.record(self.card)
        self.assertEqual(self.proc.lanes[count:], ['build', 'qa', 'security', 'review'])
        self.assertEqual(r['attempts'], 2)
        self.assertEqual(r['pr'], 101)
        self.assertTrue(r['verification']['passed'])
        self.assertEqual(r['worker_output_budget']['build']['dispatches'][:-1], self.old['build']['dispatches'])

    def test_failed_check_log_drift_prevents_recovery_before_any_worker(self):
        self.failed_checks(); g = self.recover()['grant']
        log = next(e['path'] for e in g['binding']['evidence'] if e['path'].endswith('check-1.log'))
        Path(log).write_text('Changed failure history\n')
        count = len(self.proc.lanes)
        with self.assertRaisesRegex(Halt, 'pins/evidence'):
            self.watch()
        self.assertEqual(len(self.proc.lanes), count)

    def test_failed_check_recovery_integrates_a_new_pinned_base_before_build(self):
        self.failed_checks()
        Path(self.root, 'base-addition.txt').write_text('Independent already merged change\n')
        git(self.root, 'add', 'base-addition.txt'); git(self.root, 'commit', '-m', 'New base')
        git(self.root, 'push', 'origin', 'main')
        base = git(self.root, 'rev-parse', 'HEAD')
        self.recover(); self.watch()
        r = self.factory.record(self.card)
        self.assertEqual(r['base'], base)
        git(Path(r['worktree']), 'merge-base', '--is-ancestor', base, r['head'])
        self.assertTrue(r['verification']['passed'])
        self.assertEqual(r['attempts'], 2)

    def test_new_explicit_malformed_recovery_preserves_exhausted_original_and_spent_supplement(self):
        self.blocked_or_malformed(malformed=True)
        self.assertEqual(self.recover()['grant']['kind'], 'malformed')
        before = len(self.proc.lanes)
        self.watch()
        r = self.factory.record(self.card)
        self.assertEqual(r['worker_output_budget']['build']['dispatches'][:-1], self.old['build']['dispatches'])
        self.assertEqual(r['worker_output_budget']['build']['limit'], 2)
        self.assertEqual(r['worker_output_recovery']['build']['status'], 'spent')
        self.assertEqual(r['worker_output_budget']['build']['dispatches'][-1]['status'], 'accepted')
        self.assertEqual(self.proc.lanes[before:], ['build', 'qa', 'security', 'review'])

    def test_explicit_blocked_worker_recovery_reuses_partial_work_and_keeps_attempt(self):
        self.blocked_or_malformed()
        self.assertEqual(self.recover()['grant']['kind'], 'worker-blocked')
        count = len(self.proc.lanes); self.watch()
        self.assertEqual(self.proc.lanes[count:], ['build', 'qa', 'security', 'review'])
        self.assertEqual(self.factory.record(self.card)['attempts'], 1)

    def test_crash_before_dispatch_cannot_be_refunded_by_editing_work_and_renaming_request(self):
        self.blocked_or_malformed(malformed=True); self.recover()
        with patch.object(Factory, 'lane', side_effect=Stopped('Before dispatch fixture')):
            with self.assertRaises(Stopped):
                self.watch()
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        self.r = self.factory.record(self.card)
        self.assertEqual(self.r['ticket_recoveries'][0]['status'], 'spent')
        Path(self.r['worktree'], 'new-edit.txt').write_text('A new edit cannot refund spent authority\n')
        with self.assertRaisesRegex(Halt, 'already received'):
            self.recover(request='renamed-request')
