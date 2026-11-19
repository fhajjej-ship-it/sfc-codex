from __future__ import annotations

from contextlib import nullcontext
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import test_factory
from test_pickup import ticket, PickupGitHub
from sfc_lib.common import Halt, Stopped, read_json, write_json
from sfc_lib.factory import Factory
from sfc_lib.corrective_recovery import grant


class CorrectiveTests(unittest.TestCase):
    setUp = test_factory.IntegrationTests.setUp
    tearDown = test_factory.IntegrationTests.tearDown

    def rejected(self, once=False):
        self.gh = PickupGitHub(self.root, [ticket()])
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        self.proc.fail_lane = 'qa'
        execute = self.proc.execute
        def finding(argv, cwd, log, timeout, prompt=None, **kwargs):
            code = execute(argv, cwd, log, timeout, prompt, **kwargs)
            if argv[0] == 'codex':
                p = Path(argv[argv.index('--output-last-message') + 1])
                result = read_json(p)
                if result['verdict'] == 'fail':
                    result['findings'] = [{'file':'value.txt', 'line':1, 'severity':'medium',
                                           'message':'Fixture behavior requires correction'}]
                    write_json(p, result)
            return code
        fail = self.factory.fail
        def stop_after_finding(card, result):
            fail(card, result)
            (self.factory.dir/'stop').touch()
        with patch.object(self.proc, 'execute', side_effect=finding), (
                patch.object(self.factory,'fail',side_effect=stop_after_finding) if once else nullcontext()):
            self.assertEqual(self.factory.run(include_backlog=True, issues=(1,))['outcome'], 'stopped' if once else 'waiting')
        self.card = self.gh.cards()[0]
        self.assertEqual(self.card['status'], 'Building' if once else 'Blocked')
        self.r = self.factory.record(self.card)
        self.assertEqual(self.r['claim']['state'], 'verified')
        self.factory.save()
        (self.factory.dir/'stop').touch()
        self.old = copy.deepcopy(self.r['worker_output_budget'])
        self.proc.fail_lane = None

    def recover(self, apply=True, request='corrective-test'):
        return grant(self.factory, issue=1, request_id=request,
                     reason='User explicitly authorized one corrective Build', grant_one=apply)

    def resume(self, issues=(1,)):
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        return self.factory.run(include_backlog=True, issues=issues)

    def legacy_startup(self):
        self.rejected(once=True)
        folder = self.factory.dir / 'runs' / 'retained-startup' / 'issue-1' / 'build-01234567'
        folder.mkdir(parents=True)
        (folder / 'prompt.md').write_text('Retained startup prompt; no execution evidence\n')
        self.r['worker_output_budget']['build']['dispatches'].append({
            'status': 'legacy-unaccepted', 'folder': str(folder),
            'result_path': str(folder / 'result.json'), 'log': str(folder / 'worker.log')})
        self.factory.save()
        self.old = copy.deepcopy(self.r['worker_output_budget'])
        return folder

    def startup_grant(self, apply=True):
        return grant(self.factory, issue=1, request_id='explicit-absent-startup',
                     reason='Owner authorized exactly one Build and missing startup pins',
                     grant_one=apply, pin_legacy_startup=True)

    def test_explicit_startup_absence_requires_opt_in_then_one_build_preserves_history(self):
        folder = self.legacy_startup()
        before = self.factory.state_path.read_bytes()
        with self.assertRaisesRegex(Halt, 'retained original'):
            self.recover(False)
        preview = self.startup_grant(False)
        self.assertEqual(self.factory.state_path.read_bytes(), before)
        missing = {Path(e['path']).name for e in preview['grant']['binding']['evidence'] if e.get('missing')}
        self.assertEqual(missing, {'worker.log', 'result.json', 'dispatch.json', 'receipt.json'})
        self.startup_grant()
        prior = len(self.proc.lanes)
        self.assertEqual(self.resume()['outcome'], 'waiting')
        r = self.factory.record(self.card)
        self.assertEqual(self.proc.lanes[prior:], ['build', 'qa', 'security', 'review'])
        self.assertEqual(r['worker_output_budget']['build']['dispatches'][:-1], self.old['build']['dispatches'])
        self.assertEqual(r['corrective_builds'][0]['status'], 'spent')
        self.assertTrue(all(not (folder / name).exists() for name in missing))

    def test_late_startup_artifacts_or_prompt_changes_prevent_admission(self):
        folder = self.legacy_startup()
        self.startup_grant()
        original_prompt = (folder / 'prompt.md').read_bytes()
        before = len(self.proc.lanes)
        for name in ('worker.log', 'result.json', 'dispatch.json', 'receipt.json', 'prompt.md'):
            with self.subTest(name=name):
                (folder / name).write_text('Late evidence must not be accepted\n')
                with self.assertRaises(Halt):
                    self.resume()
                self.assertEqual(len(self.proc.lanes), before)
                self.assertEqual(self.factory.record(self.card)['corrective_builds'][0]['status'], 'available')
                if name == 'prompt.md': (folder / name).write_bytes(original_prompt)
                else: (folder / name).unlink()

    def test_startup_exception_cannot_adopt_other_issue_symlink_or_missing_accepted_log(self):
        folder = self.legacy_startup()
        budget = self.r['worker_output_budget']['build']
        from sfc_lib.worker_recovery import binding
        def check():
            return binding(self.factory, self.card, self.r['worktree'], self.r['head'], self.r['base'],
                           budget, pin_legacy_startup=True)
        (folder / 'worker.log').symlink_to(folder / 'missing-target')
        with self.assertRaises(Halt): check()
        (folder / 'worker.log').unlink()
        original = budget['dispatches'][-1]['folder']
        budget['dispatches'][-1]['folder'] = str(folder.parent.parent / 'issue-2' / folder.name)
        with self.assertRaises(Halt): check()
        budget['dispatches'][-1]['folder'] = original
        accepted = Path(budget['dispatches'][0]['log'])
        saved = accepted.read_bytes()
        accepted.unlink()
        with self.assertRaisesRegex(Halt, 'retained original'): check()
        accepted.write_bytes(saved)

    def test_preview_then_one_corrective_build_reuses_pr_and_preserves_attempts_and_gates(self):
        self.rejected()
        before = self.factory.state_path.read_bytes()
        writes = len(self.gh.writes)
        self.assertEqual(self.recover(False)['state'], 'preview')
        self.assertEqual(self.factory.state_path.read_bytes(), before)
        self.assertEqual(len(self.gh.writes), writes)
        self.recover()
        before_lanes = len(self.proc.lanes)
        self.assertEqual(self.resume()['outcome'], 'waiting')
        r = self.factory.record(self.card)
        self.assertEqual(self.proc.lanes[before_lanes:], ['build','qa','security','review'])
        self.assertEqual(r['attempts'], 2)
        self.assertEqual(r['corrective_builds'][0]['status'], 'spent')
        self.assertEqual(r['worker_output_budget']['build']['dispatches'][:-1], self.old['build']['dispatches'])
        self.assertEqual(r['worker_output_budget']['build']['dispatches'][-1]['corrective_request_id'], 'corrective-test')
        self.assertEqual(r['pr'],101)
        self.assertEqual(len(self.gh.prs),1)
        self.assertEqual(self.gh.merges,[])
        self.assertTrue(r['verification']['passed'])

    def advance_base(self):
        from sfc_lib.common import git
        git(self.root, 'checkout', 'main')
        Path(self.root, 'independent.txt').write_text('Independent base change\n')
        git(self.root, 'add', 'independent.txt')
        git(self.root, 'commit', '-m', 'Independent change')
        git(self.root, 'push', 'origin', 'main')
        return git(self.root, 'rev-parse', 'HEAD')

    def test_forward_base_is_pinned_without_rewriting_old_finding_and_gets_fresh_checks(self):
        self.rejected()
        old_base = self.r['base']
        old_feedback = self.r['feedback']
        current = self.advance_base()
        preview = self.recover(False)['grant']['binding']
        self.assertEqual(preview['base'], old_base)
        self.assertEqual(preview['live_base'], current)
        self.assertEqual(self.r['feedback'], old_feedback)
        self.recover()
        before = len(self.proc.lanes)
        self.resume()
        r = self.factory.record(self.card)
        self.assertEqual(self.proc.lanes[before:], ['build', 'qa', 'security', 'review'])
        self.assertEqual(r['base'], current)
        self.assertEqual(r['attempts'], 2)
        self.assertEqual(r['worker_output_budget']['build']['dispatches'][:-1], self.old['build']['dispatches'])
        for lane in ('qa', 'security', 'review'):
            self.assertEqual(r['receipts'][lane]['base'], current)
            self.assertEqual(r['receipts'][lane]['head'], r['head'])
        self.assertEqual(r['verification']['base'], current)

    def test_base_drift_after_grant_prevents_board_and_worker_writes(self):
        self.rejected(); self.recover()
        before, writes = len(self.proc.lanes), len(self.gh.writes)
        self.advance_base()
        with self.assertRaisesRegex(Halt, 'pins/history'):
            self.resume()
        self.assertEqual(len(self.proc.lanes), before)
        self.assertEqual(len(self.gh.writes), writes)

    def test_rewritten_base_history_cannot_receive_a_corrective_grant(self):
        from sfc_lib.common import git
        self.rejected()
        git(self.root, 'checkout', '--orphan', 'replacement')
        git(self.root, 'commit', '-m', 'Replacement history')
        git(self.root, 'push', '--force', 'origin', 'HEAD:main')
        before = self.factory.state_path.read_bytes()
        with self.assertRaisesRegex(Halt, 'base history changed'):
            self.recover()
        self.assertEqual(self.factory.state_path.read_bytes(), before)

    def test_base_drift_at_dispatch_keeps_grant_unspent(self):
        self.rejected(); self.recover()
        original = self.factory.__class__.build
        def drift(factory, card, wt):
            self.advance_base()
            return original(factory, card, wt)
        before = len(self.proc.lanes)
        with patch.object(Factory, 'build', drift):
            with self.assertRaisesRegex(Halt, 'pins/history'):
                self.resume()
        self.assertEqual(len(self.proc.lanes), before)
        self.assertEqual(self.factory.record(self.card)['corrective_builds'][0]['status'], 'available')

    def test_exhausted_interrupted_output_and_spent_supplement_do_not_block_new_corrective_authority(self):
        self.rejected()
        budget=self.r['worker_output_budget']['build']
        for n in range(2):
            folder=self.factory.dir/'runs'/f'old-{n}'/'issue-1'/'build-old'
            folder.mkdir(parents=True)
            (folder/'worker.log').write_text('Interrupted fixture\n')
            budget['dispatches'].append({'status':'reserved','result_path':str(folder/'result.json'),'log':str(folder/'worker.log')})
        self.r['worker_output_recovery']={'build':{'status':'spent','request_id':'old-output-recovery'}}
        self.factory.save()
        old=copy.deepcopy(budget)
        self.recover()
        self.assertEqual(self.resume()['outcome'],'waiting')
        r=self.factory.record(self.card)
        self.assertEqual(r['worker_output_budget']['build']['dispatches'][:-1],old['dispatches'])
        self.assertEqual(r['worker_output_recovery']['build']['status'],'spent')

    def test_corrective_failure_does_not_renew_authority_or_refund_attempts(self):
        self.rejected(); self.recover(); self.proc.fail_lane='build'
        before=len(self.proc.lanes)
        self.resume()
        self.assertEqual(self.proc.lanes[before:],['build'])
        r=self.factory.record(self.card)
        self.assertEqual(r['attempts'],3)
        self.assertEqual(r['corrective_builds'][0]['status'],'spent')
        self.assertEqual(self.gh.items[0]['status'],'Blocked')
        (self.factory.dir/'stop').touch()
        self.assertEqual(self.recover()['state'],'spent')

    def test_same_findings_cannot_get_second_grant_and_request_replay_is_read_only(self):
        self.rejected(); self.recover()
        before=self.factory.state_path.read_bytes()
        self.assertEqual(self.recover()['state'],'available')
        self.assertEqual(self.factory.state_path.read_bytes(),before)
        with self.assertRaisesRegex(Halt,'unspent'):self.recover(request='different')
        # Spending without a new finding never makes a second request eligible.
        self.r['corrective_builds'][0].update(status='spent',consumed_at='fixture')
        self.factory.save()
        with self.assertRaisesRegex(Halt,'already received'):self.recover(request='different')

    def test_task_and_evidence_drift_reject_before_board_or_worker_writes(self):
        self.rejected(); self.recover(); before=len(self.proc.lanes); writes=len(self.gh.writes)
        self.gh.items[0]['title']='Changed title'
        with self.assertRaisesRegex(Halt,'receipt pins|pins/history'):self.resume()
        self.assertEqual(len(self.proc.lanes),before); self.assertEqual(len(self.gh.writes),writes)
        self.gh.items[0]['title']=self.card['title']
        failed=next(d for d in self.r['worker_output_budget']['qa']['dispatches'] if read_json(d['result_path'])==json.loads(self.r['feedback']))
        Path(failed['log']).write_text('Changed historical evidence')
        with self.assertRaisesRegex(Halt,'pins/history'):self.resume()
        self.assertEqual(len(self.proc.lanes),before)

    def test_out_of_scope_and_unscoped_runs_cannot_admit_corrective_card(self):
        self.rejected(); self.recover(); before=len(self.proc.lanes)
        self.resume((2,))
        self.assertEqual(len(self.proc.lanes),before)
        self.assertEqual(self.gh.items[0]['status'],'Blocked')
        self.factory.run()
        self.assertEqual(len(self.proc.lanes),before)

    def test_lost_admission_response_latches_without_repeating_write(self):
        self.rejected(); self.recover(); move=self.gh.move
        def lost(card,status):
            move(card,status)
            raise Halt('Fixture lost response')
        with patch.object(self.gh,'move',side_effect=lost):
            with self.assertRaisesRegex(Halt,'lost response'):self.resume()
        writes=len(self.gh.writes)
        with self.assertRaisesRegex(Halt,'Unresolved corrective admission'):self.resume()
        self.assertEqual(len(self.gh.writes),writes)
        self.assertEqual(self.factory.record(self.card)['corrective_builds'][0]['status'],'available')

    def test_configuration_and_pr_head_drift_prevent_admission(self):
        self.rejected(); self.recover(); before=len(self.proc.lanes)
        self.c['auto_max_lines']=800
        with self.assertRaisesRegex(Halt,'receipt pins|pins/history'):self.resume()
        self.assertEqual(len(self.proc.lanes),before)
        self.c['auto_max_lines']=400
        self.gh.external_head='0'*40
        with self.assertRaisesRegex(Halt,'PR state'):self.resume()
        self.assertEqual(len(self.proc.lanes),before)

    def test_corrective_grant_keeps_size_gate_and_requires_all_fresh_lanes(self):
        self.c['auto_max_lines']=800
        self.rejected(); self.recover()
        self.gh.prs[101]['additions']=801
        self.resume()
        r=self.factory.record(self.card)
        self.assertEqual(self.gh.items[0]['status'],'Blocked')
        self.assertIn('801 changed lines',self.gh.notes[-1])
        self.assertIn('configured automatic merge limit: 800',self.gh.notes[-1])
        self.assertEqual(self.gh.merges,[])
        self.assertEqual(r['attempts'],2)
        self.assertEqual(set(r['receipts']),{'qa','security','review'})
        self.assertEqual(self.proc.lanes[-4:],['build','qa','security','review'])
        dispatch=r['worker_output_budget']['build']['dispatches'][-1]
        self.assertEqual(dispatch['status'],'accepted')
        self.assertTrue((Path(dispatch['result_path']).parent/'receipt.json').is_file())

    def test_crash_after_grant_intent_is_not_retried(self):
        self.rejected(); write=self.factory.save
        def ambiguous():
            write()
            raise Halt('Fixture lost local save acknowledgement')
        with patch.object(self.factory,'save',side_effect=ambiguous):
            with self.assertRaisesRegex(Halt,'lost local save'):self.recover()
        with self.assertRaisesRegex(Halt,'journal differs'):self.recover()
        before=len(self.proc.lanes)
        with self.assertRaisesRegex(Halt,'journal differs'):self.resume()
        self.assertEqual(len(self.proc.lanes),before)

    def test_spent_corrective_grant_cannot_fall_back_to_unspent_original_allowance(self):
        self.rejected(once=True); self.recover(); self.resume()
        r=self.factory.record(self.card)
        self.assertEqual(r['attempts'],1)
        self.assertEqual(r['corrective_builds'][0]['status'],'spent')
        self.gh.items[0]['status']='Building'
        before=len(self.proc.lanes)
        self.assertEqual(self.resume()['outcome'], 'waiting')
        self.assertEqual(len(self.proc.lanes),before)
        # Planning defers the reused handoff; the dispatch guard independently
        # rejects any direct attempt to launch another Build with this grant.
        with self.assertRaisesRegex(Halt,'Corrective Build allowance spent'):
            self.factory.lane(self.gh.items[0], 'build', Path(r['worktree']), r['base'])
        self.assertEqual(len(self.proc.lanes),before)

    def test_interrupted_after_corrective_build_resumes_read_only_lanes_without_another_build(self):
        self.rejected(); self.recover(); before=len(self.proc.lanes)
        with patch.object(Factory, 'qa', side_effect=Stopped('Fixture interruption after publication')):
            with self.assertRaises(Stopped):
                self.resume()
        self.assertEqual(self.gh.items[0]['status'], 'QA')
        self.assertEqual(self.proc.lanes[before:], ['build'])
        self.resume()
        self.assertEqual(self.proc.lanes[before:], ['build', 'qa', 'security', 'review'])
        self.assertEqual(self.factory.record(self.card)['attempts'], 2)

    def test_incomplete_ticket_creation_cannot_admit_or_move_a_corrective_card(self):
        self.rejected(); self.recover(); before=len(self.gh.writes)
        with patch.object(Factory,'creation_problem',return_value='Fixture incomplete creation'):
            with self.assertRaisesRegex(Halt,'creation evidence'):self.resume()
        self.assertEqual(len(self.gh.writes),before)
        self.assertEqual(self.gh.items[0]['status'],'Blocked')
