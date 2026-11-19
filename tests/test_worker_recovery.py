from __future__ import annotations

import copy
from contextlib import redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import test_factory
import sfc
from sfc_lib.common import Halt, Stopped, exclusive, git, write_json
from sfc_lib.factory import Factory
from sfc_lib.worker_recovery import recover


class WorkerRecoveryTests(unittest.TestCase):
    setUp = test_factory.IntegrationTests.setUp
    tearDown = test_factory.IntegrationTests.tearDown

    def exhausted(self):
        self.proc.malformed = True
        for _ in range(2):
            with self.assertRaisesRegex(Halt, 'Malformed build result'):
                self.resume()
            self.factory = Factory(self.root, self.c, self.gh, self.proc)
        self.proc.malformed = False
        self.card = self.gh.cards()[0]
        record = self.factory.record(self.card)
        self.assertEqual(record['claim']['state'], 'verified')
        self.factory.save()
        (self.factory.dir / 'stop').touch()
        self.old_budget = copy.deepcopy(record['worker_output_budget']['build'])
        self.originals = {Path(d[k]): Path(d[k]).read_bytes()
                          for d in self.old_budget['dispatches'] for k in ('result_path', 'log')}

    def recovery(self, apply=True, **kwargs):
        return recover(self.factory, issue=1, request_id=kwargs.get('request_id', 'operator-recovery-1'),
                       reason=kwargs.get('reason', 'User authorized one additional complete Build dispatch'),
                       grant_one=apply)

    def exhausted_interruptions(self):
        execute = self.proc.execute

        def interrupted(argv, cwd, log, timeout, prompt=None, **kwargs):
            if argv[0] != 'codex':
                return execute(argv, cwd, log, timeout, prompt, **kwargs)
            self.proc.lanes.append('build')
            Path(log).write_text('Fixture worker interrupted before producing its result\n')
            Path(cwd, 'value.txt').write_text('saved partial work\n')
            raise Stopped('Fixture host interruption')

        with patch.object(self.proc, 'execute', side_effect=interrupted):
            for _ in range(2):
                with self.assertRaises(Stopped):
                    self.resume()
                self.factory = Factory(self.root, self.c, self.gh, self.proc)
        self.card = self.gh.cards()[0]
        record = self.factory.record(self.card)
        self.assertEqual(record['claim']['state'], 'verified')
        self.factory.save()
        (self.factory.dir / 'stop').touch()
        self.old_budget = copy.deepcopy(record['worker_output_budget']['build'])
        self.missing_results = [Path(d['result_path']) for d in self.old_budget['dispatches']]
        self.originals = {Path(d['log']): Path(d['log']).read_bytes()
                          for d in self.old_budget['dispatches']}
        self.assertTrue(all(not p.exists() for p in self.missing_results))

    def test_interrupted_missing_results_are_pinned_as_absent_and_full_recovery_still_runs(self):
        self.exhausted_interruptions()
        before = self.factory.state_path.read_bytes()
        preview = self.recovery(False)
        self.assertEqual(self.factory.state_path.read_bytes(), before)
        absent = [e for e in preview['grant']['binding']['evidence'] if e.get('missing')]
        self.assertEqual({e['path'] for e in absent}, {str(p) for p in self.missing_results})
        self.recovery()
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        self.assertEqual(self.resume()['outcome'], 'waiting')
        record = self.factory.record(self.card)
        self.assertEqual(self.proc.lanes, ['build', 'build', 'build', 'qa', 'security', 'review'])
        self.assertEqual(record['worker_output_budget']['build']['dispatches'][:2], self.old_budget['dispatches'])
        self.assertEqual(record['worker_output_recovery']['build']['status'], 'spent')
        self.assertTrue(all(not p.exists() for p in self.missing_results))
        self.assertTrue(all(p.read_bytes() == data for p, data in self.originals.items()))
        self.assertTrue(record['verification']['passed'])

    def test_late_historical_output_invalidates_grant_without_spending_or_launching(self):
        self.exhausted_interruptions()
        self.recovery()
        self.missing_results[0].write_text('{"verdict":"pass"}')
        with self.assertRaisesRegex(Halt, 'Recovery pins/history/evidence changed'):
            self.resume()
        self.assertEqual(self.proc.lanes, ['build', 'build'])
        self.assertEqual(self.factory.record(self.card)['worker_output_recovery']['build']['status'], 'available')

    def test_missing_interrupted_log_is_not_recoverable(self):
        self.exhausted_interruptions()
        next(iter(self.originals)).unlink()
        with self.assertRaisesRegex(Halt, 'retained original'):
            self.recovery()

    def resume(self):
        return self.factory.run()

    def test_preview_and_grant_preserve_all_original_spending_and_artifacts(self):
        self.exhausted()
        before = self.factory.state_path.read_bytes()
        self.assertEqual(self.recovery(False)['state'], 'preview')
        self.assertEqual(self.factory.state_path.read_bytes(), before)
        self.assertFalse((self.factory.dir / 'recoveries').exists())
        result = self.recovery()
        self.assertEqual(result['state'], 'granted')
        self.assertEqual(self.factory.record(self.card)['worker_output_budget']['build'], self.old_budget)
        self.assertEqual(self.factory.record(self.card)['attempts'], 0)
        self.assertTrue(all(p.read_bytes() == data for p, data in self.originals.items()))
        self.assertEqual(self.proc.lanes, ['build', 'build'])
        self.assertEqual(self.gh.prs, {})

    def test_full_recovery_still_executes_checks_qa_security_review_and_human_handoff(self):
        self.exhausted()
        self.recovery()
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        self.assertEqual(self.resume()['outcome'], 'waiting')
        record = self.factory.record(self.card)
        self.assertEqual(self.proc.lanes, ['build', 'build', 'build', 'qa', 'security', 'review'])
        budget = record['worker_output_budget']['build']
        self.assertEqual(budget['limit'], 2)
        self.assertEqual(budget['dispatches'][:2], self.old_budget['dispatches'])
        self.assertEqual(budget['dispatches'][2]['recovery_request_id'], 'operator-recovery-1')
        self.assertEqual(record['worker_output_recovery']['build']['status'], 'spent')
        self.assertTrue(record['verification']['passed'])
        self.assertTrue(all(p.read_bytes() == data for p, data in self.originals.items()))
        self.assertEqual(self.gh.merges, [])
        self.assertEqual(self.gh.items[0]['status'], 'Blocked')
        self.assertEqual(len(self.gh.prs), 1)

    def test_one_additional_failure_stays_spent_across_restart(self):
        self.exhausted()
        self.recovery()
        self.proc.malformed = True
        with self.assertRaisesRegex(Halt, 'Malformed build result'):
            self.resume()
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        self.proc.malformed = False
        with self.assertRaisesRegex(Halt, 'no unused supplemental'):
            self.resume()
        self.assertEqual(self.proc.lanes, ['build'] * 3)
        self.assertEqual(self.gh.prs, {})
        self.assertEqual(self.gh.merges, [])

    def test_request_replay_never_writes_and_cannot_grant_twice(self):
        self.exhausted()
        self.recovery()
        before = self.factory.state_path.read_bytes()
        self.assertEqual(self.recovery()['state'], 'existing')
        self.assertEqual(self.factory.state_path.read_bytes(), before)
        with self.assertRaisesRegex(Halt, 'Only one supplemental'):
            self.recovery(request_id='a-new-id')
        with self.assertRaisesRegex(Halt, 'Only one supplemental'):
            self.recovery(reason='Changed reason')

    def test_changed_task_or_configuration_rejects_dispatch_without_consuming_grant(self):
        self.exhausted()
        self.recovery()
        self.gh.items[0]['title'] = 'Changed task'
        with self.assertRaisesRegex(Halt, 'Recovery pins/history/evidence changed'):
            self.resume()
        self.assertEqual(self.proc.lanes, ['build'] * 2)
        self.assertEqual(self.factory.record(self.card)['worker_output_recovery']['build']['status'], 'available')
        self.gh.items[0]['title'] = self.card['title']
        self.c['model'] = 'explicit-other-model'
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        with self.assertRaisesRegex(Halt, 'Recovery pins/history/evidence changed'):
            self.resume()
        self.assertEqual(self.proc.lanes, ['build'] * 2)

    def test_changed_worktree_or_history_rejects_dispatch(self):
        self.exhausted()
        self.recovery()
        worktree = Path(self.factory.record(self.card)['worktree'])
        (worktree / 'value.txt').write_text('unreviewed change\n')
        with self.assertRaisesRegex(Halt, 'Recovery pins/history/evidence changed'):
            self.resume()
        (worktree / 'value.txt').write_text('fixed\n')
        next(iter(self.originals)).write_text('changed historical evidence')
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        with self.assertRaisesRegex(Halt, 'Recovery pins/history/evidence changed'):
            self.resume()
        self.assertEqual(self.proc.lanes, ['build'] * 2)

    def test_untracked_worktree_files_prevent_grant(self):
        self.exhausted()
        (Path(self.factory.record(self.card)['worktree']) / 'unknown.txt').write_text('unknown')
        with self.assertRaisesRegex(Halt, 'untracked'):
            self.recovery()

    def test_missing_history_unverified_claim_and_wrong_stage_are_not_recoverable(self):
        self.exhausted()
        record = self.factory.record(self.card)
        record['claim']['state'] = 'intent'
        with self.assertRaisesRegex(Halt, 'verified claim'):
            self.recovery()
        record['claim']['state'] = 'verified'
        self.gh.items[0]['status'] = 'Backlog'
        with self.assertRaisesRegex(Halt, 'open Building'):
            self.recovery()
        self.gh.items[0]['status'] = 'Building'
        next(iter(self.originals)).unlink()
        with self.assertRaisesRegex(Halt, 'retained original'):
            self.recovery()

    def test_existing_pr_prevents_build_recovery(self):
        self.exhausted()
        with patch.object(self.gh, 'pull_requests', return_value=[{'number': 101, 'state': 'OPEN'}]):
            with self.assertRaisesRegex(Halt, 'existing PR'):
                self.recovery()

    def test_crash_after_state_write_before_journal_confirmation_cannot_dispatch_or_replay(self):
        self.exhausted()

        def interrupted(path, value):
            if value.get('state') == 'granted':
                raise OSError('fixture journal confirmation failure')
            return write_json(path, value)

        with patch('sfc_lib.worker_recovery.write_json', side_effect=interrupted):
            with self.assertRaisesRegex(OSError, 'confirmation failure'):
                self.recovery()
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        with self.assertRaisesRegex(Halt, 'journal is not verified'):
            self.recovery()
        with self.assertRaisesRegex(Halt, 'journal is not verified'):
            self.resume()
        self.assertEqual(self.proc.lanes, ['build'] * 2)
        self.assertEqual(self.factory.record(self.card)['worker_output_recovery']['build']['status'], 'available')

    def test_missing_or_edited_grant_journal_cannot_dispatch(self):
        self.exhausted()
        result = self.recovery()
        journal = Path(result['receipt'])
        journal.write_text('{}')
        with self.assertRaisesRegex(Halt, 'journal is not verified'):
            self.resume()
        journal.unlink()
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        with self.assertRaisesRegex(Halt, 'journal is unavailable'):
            self.resume()
        self.assertEqual(self.proc.lanes, ['build'] * 2)

    def test_live_task_close_and_stage_drift_after_selection_cannot_consume_grant(self):
        self.exhausted()
        self.recovery()
        original = self.factory.lane
        for key, value in (('title', 'Externally edited task'), ('state', 'CLOSED'), ('status', 'Blocked')):
            with self.subTest(key=key):
                self.gh.items[0] = copy.deepcopy(self.card)

                def drift(card, lane, worktree, base):
                    self.gh.items[0][key] = value
                    return original(card, lane, worktree, base)

                with patch.object(self.factory, 'lane', side_effect=drift):
                    with self.assertRaisesRegex(Halt, 'Live recovery issue/task/stage changed'):
                        self.resume()
                self.assertEqual(self.proc.lanes, ['build'] * 2)
                self.assertEqual(self.factory.record(self.card)['worker_output_recovery']['build']['status'], 'available')

    def test_reopened_prerequisite_prevents_consumption(self):
        self.exhausted()
        self.recovery()
        original = self.factory.lane
        def reopened_at_dispatch(card, lane, worktree, base):
            with patch('sfc_lib.worker_recovery.pickup.prerequisites', side_effect=Halt('Open prerequisites: #5')):
                return original(card, lane, worktree, base)
        with patch.object(self.factory, 'lane', side_effect=reopened_at_dispatch):
            with self.assertRaisesRegex(Halt, 'Open prerequisites'):
                self.resume()
        self.assertEqual(self.proc.lanes, ['build'] * 2)
        self.assertEqual(self.factory.record(self.card)['worker_output_recovery']['build']['status'], 'available')

    def test_cli_is_preview_by_default_and_refuses_a_busy_supervisor(self):
        self.exhausted()
        argv = ['--root', str(self.root), 'recover-worker-output', '--issue', '1',
                '--request-id', 'operator-recovery-1', '--reason', 'Explicit one-use recovery']
        with patch.object(sfc, 'Factory', return_value=self.factory), patch(
                'sfc_lib.worker_recovery.pickup.cards', return_value=self.gh.cards()), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(sfc.main(argv), 0)
            self.assertEqual(json.loads(output.getvalue())['state'], 'preview')
        with exclusive(self.root), redirect_stderr(io.StringIO()):
            self.assertEqual(sfc.main([*argv, '--grant-one']), 1)
        self.assertNotIn('worker_output_recovery', self.factory.record(self.card))


if __name__ == '__main__':
    unittest.main()
