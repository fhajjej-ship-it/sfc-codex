from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4
from zipfile import ZipFile

from benchmark_review import LocalActivity, measure_inspection_arm
from test_factory import BASE_CONFIG, FakeGitHub, issue
from test_review_policy import PolicyProcesses, capabilities
from sfc_lib import delivery, review_inspection, review_policy, review_retention
from sfc_lib.common import Halt, Processes, Stopped, git, run, write_json
from sfc_lib.factory import Factory
from sfc_lib.review_benchmark import EXECUTION_LIMITS, historical_source_route, inspection_arm, inspection_policy
from sfc_lib.verification import checksum, load


class InspectionProcesses(PolicyProcesses):
    def __init__(self, stop):
        super().__init__(stop)
        self.check_started = threading.Event()
        self.inspection_started = threading.Event()
        self.inspectors = []
        self.wait_for_check = True
        self.malformed_inspection = False
        self.adverse_inspection = False
        self.inspection_events = []

    def execute(self, argv, cwd, log, timeout, prompt=None, **kwargs):
        if prompt and 'independent provisional inspector' in prompt:
            self.inspectors.append((Path(cwd), argv, prompt))
            self.inspection_started.set()
            if self.wait_for_check and not self.check_started.wait(5):
                raise AssertionError('inspection and project checks did not run concurrently')
            result = {'verdict': 'pass', 'summary': 'Source inspected', 'findings': [],
                      'evidence': 'Examined source only, before check evidence was ready'}
            if self.adverse_inspection:
                result.update(verdict='fail', findings=[{'file': 'value.txt', 'line': 1,
                    'severity': 'high', 'message': 'Original value contract was removed'}])
            write_json(argv[argv.index('--output-last-message') + 1], result)
            events = [{'type': 'thread.started', 'thread_id': str(uuid4())}, {'type': 'turn.started'},
                      *self.inspection_events,
                      {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': json.dumps(result)}}]
            if not self.malformed_inspection:
                events.append({'type': 'turn.completed', 'usage': {'input_tokens': 10, 'output_tokens': 4}})
            Path(log).write_text('\n'.join(json.dumps(v) for v in events) + '\n')
            return 0
        return super().execute(argv, cwd, log, timeout, prompt, **kwargs)


class InspectionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='sfc-inspection-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        git(self.root, 'init', '-b', 'main')
        git(self.root, 'config', 'user.email', 'fixture@example.invalid')
        git(self.root, 'config', 'user.name', 'Fixture')
        (self.root / '.gitignore').write_text('.sfc/\n')
        (self.root / 'value.txt').write_text('old\n')
        git(self.root, 'add', '.gitignore', 'value.txt')
        git(self.root, 'commit', '-m', 'base')
        self.base = git(self.root, 'rev-parse', 'HEAD')
        (self.root / 'value.txt').write_text('new\n')
        git(self.root, 'commit', '-am', 'change')
        self.head = git(self.root, 'rev-parse', 'HEAD')
        self.config = {**BASE_CONFIG, 'review_policy': {'routine_paths': ['value.txt'],
                                                       'inspection': {'enabled': ['source', 'release']}}}
        self.card = issue(status='QA')
        self.gh = FakeGitHub(self.root, [self.card])
        self.proc = InspectionProcesses(self.root / '.sfc/stop')
        self.f = Factory(self.root, self.config, self.gh, self.proc)
        self.f.run_dir = self.root / '.sfc/runs/fixture'
        self.f.occupied[self.card['number']] = self.card
        cap = patch.object(review_policy, 'run', side_effect=capabilities)
        cap.start()
        self.addCleanup(cap.stop)

    def check(self):
        self.assertTrue(self.proc.inspection_started.wait(5))
        self.proc.check_started.set()
        return self.f.verify(self.card, self.root, self.base)

    def test_bounded_final_check_output_requires_original_log_bytes(self):
        check = self.root / 'check.py'
        check.write_text('print("verified")\n')
        git(self.root, 'add', 'check.py')
        git(self.root, 'commit', '-m', 'Add check')
        head = git(self.root, 'rev-parse', 'HEAD')
        log = self.root / '.sfc' / 'check.log'
        log.parent.mkdir(exist_ok=True)
        log.write_text('verified\n')
        receipt = {'command': 'python3 -B check.py', 'reuse': {
            'log': str(log), 'log_sha256': checksum(log)}}
        with patch.object(review_policy, 'run', wraps=run):
            compact = review_policy.compact_context
            evidence = {'checks': [receipt]}
            self.assertIn('"completed_check_output": {"1": "verified\\n"}',
                          compact(self.root, head, self.base, evidence, check_ids=[1], include_check_output=True))
            self.assertNotIn('completed_check_output', compact(self.root, head, self.base, evidence, check_ids=[1]))
            log.write_text('changed\n')
            context = compact(self.root, head, self.base, evidence, check_ids=[1], include_check_output=True)
            self.assertIn('check.py', context)
            self.assertNotIn('completed_check_output', context)
            log.write_text('x' * (review_policy.PROMPT_LOG_BYTES + 1))
            receipt['reuse']['log_sha256'] = checksum(log)
            self.assertNotIn('completed_check_output',
                             compact(self.root, head, self.base, evidence, check_ids=[1], include_check_output=True))

    def test_final_receipt_binds_completed_checks_and_provisional_inspection(self):
        f, card = self.f, self.card
        f.update(card, base=self.base, worktree=str(self.root), pr=101, branch='candidate')
        # Keep the fake model alive until an actual check process is executing;
        # phase overlap alone can include only supervisor preparation work.
        check_executing, inspected = threading.Event(), threading.Event()
        execute = self.proc.execute

        def concurrent_process(argv, cwd, log, timeout, prompt=None, **kwargs):
            provisional = 'independent provisional inspector' in (prompt or '')
            if provisional:
                self.proc.inspection_started.set()
                self.assertTrue(check_executing.wait(5))
                try:
                    return execute(argv, cwd, log, timeout, prompt, **kwargs)
                finally:
                    inspected.set()
            if argv[0] != 'codex':
                check_executing.set()
                self.assertTrue(inspected.wait(5))
            return execute(argv, cwd, log, timeout, prompt, **kwargs)

        self.proc.execute = concurrent_process
        verify = f.verify
        def check(*args):
            self.assertTrue(self.proc.inspection_started.wait(5))
            self.proc.check_started.set()
            return verify(*args)
        with patch.object(review_inspection, 'qualified', return_value=True), \
             patch.object(f, 'assert_remote'), patch.object(f, 'fetch', return_value=self.base), \
             patch.object(f, 'verify', side_effect=check):
            self.assertTrue(f.qa(card, self.root))
        record = f.record(card)
        attempt = record['review_inspections'][0]
        self.assertEqual(attempt['state'], 'checks-passed')
        self.assertGreater(attempt['overlap_seconds'], 0)
        self.assertNotIn('review', record['receipts'])
        self.assertFalse(f.pinned(card, self.head, self.base))
        cwd, argv, prompt = self.proc.inspectors[0]
        self.assertNotEqual(cwd, self.root)
        self.assertEqual(git(cwd, 'rev-parse', 'HEAD'), self.head)
        self.assertEqual(argv[argv.index('-s') + 1], 'read-only')
        self.assertNotIn(record['verification']['checks'][0]['log'], prompt)
        f.lane(card, 'review', self.root, self.base)
        self.assertIn(record['verification']['checks'][0]['log'], self.proc.prompts[-1])
        self.assertIn('Completed provisional inspection', self.proc.prompts[-1])
        self.assertEqual(record['receipts']['review']['inspection_sha256'], attempt['receipt']['receipt_sha256'])
        self.assertTrue(f.pinned(card, self.head, self.base))
        f.state['run_id'] = f.run_dir.name
        f.save()
        report = delivery.report(self.root)
        overlap = report['inspection_overlap']
        self.assertGreater(overlap['wall_seconds'], 0)
        self.assertEqual(overlap['inspection']['calls'], 1)
        self.assertEqual(overlap['final_assessment']['calls'], 1)
        self.assertGreater(overlap['final_assessment']['compute_seconds'], 0)
        self.assertEqual(overlap['inspection']['tokens']['input_tokens']['known'], 10)
        self.assertGreater(report['compute_seconds'], report['summed_worker_seconds'])
        self.assertEqual(report, delivery.report(self.root))
        Path(attempt['receipt']['log']).write_text('changed')
        self.assertFalse(f.pinned(card, self.head, self.base))

    def test_missing_promotion_and_full_capacity_keep_existing_check_path(self):
        self.assertTrue(self.f.overlap_review(self.card, self.root, self.base,
                                              lambda: self.f.verify(self.card, self.root, self.base)))
        self.f.occupied[2] = issue(2)
        with patch.object(review_inspection, 'qualified', return_value=True):
            self.assertTrue(self.f.overlap_review(self.card, self.root, self.base, lambda: True))
        self.assertEqual(self.proc.inspectors, [])
        self.assertEqual(self.f.record(self.card).get('review_inspections', []), [])

    def test_qualified_adverse_source_inspection_spends_one_normal_correction_attempt(self):
        card = self.card
        criterion = review_policy.criteria(card)[0]['id']
        config = {**self.config, 'review_policy': {
            **self.config['review_policy'], 'mode': 'risk-based',
            'providers': {'review': 'codex-native'},
            'behavioral_contract': {'paths': ['value.txt'], 'check_ids': [1], 'instructions': {}},
            'correction_reuse': {'correction_paths': ['value.txt'],
                                 'criteria': {criterion: {'source_inputs': ['value.txt'], 'check_ids': [1]}}},
        }}
        f = Factory(self.root, config, self.gh, self.proc)
        f.run_dir = self.root / '.sfc/runs/adverse'
        f.occupied[card['number']] = card
        f.update(card, base=self.base, worktree=str(self.root), pr=101, branch='candidate')
        self.proc.adverse_inspection = True
        verify = f.verify

        def check(*args):
            self.assertTrue(self.proc.inspection_started.wait(5))
            self.proc.check_started.set()
            return verify(*args)

        with patch.object(review_inspection, 'qualified', return_value=True), \
             patch.object(review_inspection, 'representative_history', return_value=True), \
             patch.object(review_policy, 'promoted', return_value=(True, 'fixture qualification')), \
             patch.object(f, 'assert_remote'), patch.object(f, 'fetch', return_value=self.base), \
             patch.object(f, 'verify', side_effect=check):
            self.assertFalse(f.qa(card, self.root))
            with self.assertRaisesRegex(Halt, 'already spent'):
                f.qa(card, self.root)
        record = f.record(card)
        rejected = record['inspection_rejection']
        origin = review_retention.origin(record)
        self.assertEqual(load(rejected)['kind'], 'inspection-rejection')
        self.assertEqual(rejected['inspection']['sha256'], record['review_inspections'][0]['receipt']['receipt_sha256'])
        self.assertEqual(origin['inspection_rejection']['sha256'], rejected['receipt_sha256'])
        self.assertEqual(len(origin['pending_findings']), 1)
        self.assertEqual(record['attempts'], 1)
        self.assertEqual(record['status'], 'Building')
        self.assertFalse(record.get('receipts'))
        self.assertFalse(f.pinned(card, self.head, self.base))
        self.assertEqual(self.proc.lanes, [])
        self.assertEqual(len(self.proc.inspectors), 1)

    def test_measured_rejection_requires_exact_successful_probe_and_defect_adjudication(self):
        card = self.card
        config = {**self.config, 'model': 'fixture-default', 'worker_timeout': 1200, 'verify_timeout': 900,
                  'review_policy': {
            **self.config['review_policy'], 'mode': 'risk-based',
            'providers': {'review': 'codex-native'},
            'behavioral_contract': {'paths': ['value.txt'], 'check_ids': [1], 'instructions': {}},
        }}
        f = Factory(self.root, config, github=object(), processes=self.proc)
        f.activity = LocalActivity()
        f.run_dir = self.root / '.sfc/runs/rejection-measurement'
        f.occupied[card['number']] = card
        self.proc.adverse_inspection = True
        command = 'grep -x new value.txt'
        probe = subprocess.run(['/bin/sh', '-c', command], cwd=self.root, capture_output=True, text=True, check=True)
        item = {'id': 'probe', 'type': 'command_execution', 'command': command}
        self.proc.inspection_events = [
            {'type': 'item.started', 'item': {**item, 'status': 'in_progress'}},
            {'type': 'item.completed', 'item': {**item, 'status': 'completed', 'exit_code': probe.returncode,
                                               'aggregated_output': probe.stdout}},
        ]

        def no_further_qa():
            self.fail('a decisive inspection should stop before another review worker')

        def check():
            self.assertTrue(self.proc.inspection_started.wait(5))
            self.proc.check_started.set()
            return f.verify(card, self.root, self.base)

        caps = {**EXECUTION_LIMITS, 'worker_timeout': 180}
        plan_sha = 'a' * 64
        reserved = []
        execute = f.execute

        def reserve(card, stage, argv, *args, **kwargs):
            if argv[0] == 'codex':
                reserved.append(kwargs['accounting']['lane'])
            return execute(card, stage, argv, *args, **kwargs)

        f.execute = reserve
        with patch.object(review_policy.shutil, 'which', return_value='/fixture/codex'):
            arm = measure_inspection_arm(f, card, self.root, self.base, 'candidate', check, no_further_qa,
                                        execution_limits=caps, measurement_plan_sha256=plan_sha, reserved_lanes=reserved)
        self.assertEqual(arm['outcome'], 'inspection-fail')
        self.assertIsNone(arm['assessment'])
        criterion = review_policy.criteria(card)[0]['id']
        pair = {'case': 'old-value', 'kind': 'retained-real-finding', 'head': self.head, 'base': self.base,
                'card': card, 'worktree': str(self.root), 'expected': ['old-value-removed'],
                'task_digest': f.task_digest(card), 'obligations': [criterion],
                'obligations_sha256': review_policy.digest(review_policy.criteria(card)), 'candidate': arm}
        report = {'target_config': f.c, 'target_policy': inspection_policy(f.c),
                  'model_settings': review_policy.model_settings(f.c), 'cli_version': 'codex-cli fixture',
                  'plan': {'sha256': plan_sha}}
        limits = {'cases': {'old-value': caps}}
        arm['coverage_adjudication'] = {criterion: {'kind': 'qa-command', 'id': command,
                                                    'reason': 'The direct probe confirms the original value is gone'}}
        arm['adjudications'] = [{'lane': 'inspection', 'index': 0, 'finding': 'old-value-removed',
                                 'reason': 'Confirmed against the pinned base and recorded candidate probe'}]
        observed = inspection_arm(report, pair, 'source', 'candidate', set(), limits=limits)
        self.assertEqual(observed['detected'], {'old-value-removed'})
        self.assertEqual(observed['dispatches'], ['inspection'])
        arm['measurement_plan_sha256'] = 'b' * 64
        with self.assertRaisesRegex(ValueError, 'exact measurement plan'):
            inspection_arm(report, pair, 'source', 'candidate', set(), limits=limits)
        arm['measurement_plan_sha256'] = plan_sha
        arm['coverage_adjudication'][criterion].update(kind='check', id=arm['checks']['checks'][0]['log_sha256'])
        with self.assertRaisesRegex(ValueError, 'requires a successful inspection probe'):
            inspection_arm(report, pair, 'source', 'candidate', set(), limits=limits)

    def test_failed_check_prelaunch_cancellation_preserves_actual_reservations(self):
        config = {**self.config, 'verify_commands': ['false'], 'worker_timeout': 1200, 'verify_timeout': 900,
                  'review_policy': {**self.config['review_policy'], 'mode': 'risk-based',
                                    'providers': {'review': 'codex-native'},
                                    'behavioral_contract': {'paths': ['value.txt'], 'check_ids': [1], 'instructions': {}}}}
        caps = EXECUTION_LIMITS
        plan_sha = 'a' * 64
        limits = {'cases': {'failed': caps}}
        for debit in (False, True):
            with self.subTest(debit=debit):
                processes = Processes(self.root / '.sfc/stop')
                f = Factory(self.root, config, github=object(), processes=processes)
                f.state = {'version': 1, 'cards': {}}
                f.activity = LocalActivity()
                f.run_dir = self.root / f'.sfc/runs/cancelled-{debit}'
                f.occupied[self.card['number']] = self.card
                cancellation = threading.Event()
                reserved = []
                execute = f.execute

                def prelaunch(card, stage, argv, *args, **kwargs):
                    if argv[0] == 'codex':
                        if debit:
                            reserved.append(kwargs['accounting']['lane'])
                        self.assertTrue(cancellation.wait(5))
                    return execute(card, stage, argv, *args, **kwargs)

                f.execute = prelaunch
                with patch.object(review_inspection, 'Event', return_value=cancellation):
                    arm = measure_inspection_arm(f, self.card, self.root, self.base, 'candidate',
                                                lambda: f.verify(self.card, self.root, self.base),
                                                lambda: self.fail('failed checks cannot launch assessment'),
                                                execution_limits=caps, measurement_plan_sha256=plan_sha,
                                                reserved_lanes=reserved)
                self.assertEqual(arm['outcome'], 'checks-failed')
                self.assertIsNone(arm['inspection']['cancellation']['log'])
                self.assertEqual(arm['reserved_lanes'], ['inspection'] if debit else [])
                criteria = review_policy.criteria(self.card)
                pair = {'case': 'failed', 'kind': 'failed-check', 'head': self.head, 'base': self.base,
                        'card': self.card, 'worktree': str(self.root), 'expected': ['check-failure'],
                        'task_digest': f.task_digest(self.card), 'obligations': [row['id'] for row in criteria],
                        'obligations_sha256': review_policy.digest(criteria), 'candidate': arm}
                report = {'target_config': f.c, 'target_policy': inspection_policy(f.c),
                          'model_settings': review_policy.model_settings(f.c), 'cli_version': 'codex-cli fixture',
                          'plan': {'sha256': plan_sha}}
                arm['coverage_adjudication'] = {row['id']: 'The actual required check failed' for row in criteria}
                observed = inspection_arm(report, pair, 'source', 'candidate', set(), limits=limits)
                self.assertEqual(observed['dispatches'], reserved)

    def test_behavioral_inspection_replaces_qa_only_with_bound_final_evidence(self):
        config = {**self.config, 'model': 'fixture-default', 'models': {'qa': 'fixture-qa'},
                  'review_policy': {**self.config['review_policy'], 'mode': 'risk-based',
                                    'providers': {'review': 'codex-native'},
                                    'behavioral_contract': {'paths': ['value.txt'], 'check_ids': [1],
                                                            'instructions': {}}}}
        f, card = Factory(self.root, config, self.gh, self.proc), self.card
        f.run_dir = self.root / '.sfc/runs/behavioral'
        f.occupied[card['number']] = card
        f.update(card, base=self.base, worktree=str(self.root), pr=101, branch='candidate')
        verify = f.verify

        def check(*args):
            self.assertTrue(self.proc.inspection_started.wait(5))
            self.proc.check_started.set()
            return verify(*args)

        with patch.object(review_inspection, 'qualified', return_value=True) as qualification, \
             patch.object(review_inspection, 'representative_history', return_value=True), \
             patch.object(review_policy, 'promoted', return_value=(True, 'fixture qualification')), \
             patch.object(review_policy, 'native_sessions', return_value=self.root / '.sfc/sessions'), \
             patch.object(review_policy.shutil, 'which', return_value='/fixture/codex'), \
             patch.object(f, 'assert_remote'), patch.object(f, 'fetch', return_value=self.base), \
             patch.object(f, 'verify', side_effect=check):
            self.assertTrue(f.qa(card, self.root))
            record = f.record(card)
            observation = record['review_inspections'][0]['receipt']
            self.assertEqual(observation['role'], 'behavioral-qa')
            self.assertEqual(observation['provider']['model'], 'fixture-qa')
            self.assertIn(review_policy.SOURCE_QA_EXAMINATION, self.proc.inspectors[-1][2])
            self.assertEqual(record['review_route']['provisional_qa'], observation['receipt_sha256'])
            self.assertEqual(self.proc.lanes, [])
            self.assertFalse(f.pinned(card, self.head, self.base))
            f.lane(card, 'review', self.root, self.base)
            self.assertEqual(self.proc.lanes, ['review'])
            self.assertIn('Source inspected', self.proc.prompts[-1])
            self.assertIn(observation['receipt_path'], self.proc.prompts[-1])
            self.assertNotIn('Examined source only', self.proc.prompts[-1])
            self.assertEqual(record['receipts']['review']['inspection_sha256'], observation['receipt_sha256'])
            self.assertTrue(f.pinned(card, self.head, self.base))

            class Later(datetime):
                @classmethod
                def now(cls, tz=None):
                    return datetime.now(tz) + timedelta(days=2)

            with patch('sfc_lib.review_policy.dt.datetime', Later):
                self.assertIsNone(review_inspection.final_reference(f, card, self.root, self.head, self.base))
                assessment = record['receipts']['review']
                replay = historical_source_route(f, card, self.root, self.base, assessment, observation, True)
                self.assertEqual(replay['obligations'], ['review'])
                self.assertEqual(replay['provisional_qa'], observation['receipt_sha256'])
                with self.assertRaisesRegex(ValueError, 'not eligible at the recorded final assessment'):
                    historical_source_route(f, card, self.root, self.base,
                        {**assessment, 'time': Later.now(timezone.utc).isoformat()}, observation, True)
            qualification.return_value = False
            self.assertFalse(f.pinned(card, self.head, self.base))
            self.assertEqual(review_policy.plan(f, card, self.root, self.base)['obligations'], ['qa', 'review'])
            qualification.return_value = True
            Path(observation['log']).write_text('changed')
            self.assertFalse(f.pinned(card, self.head, self.base))
            self.assertEqual(review_policy.plan(f, card, self.root, self.base)['obligations'], ['qa', 'review'])

    def test_failed_checks_cannot_supply_final_review(self):
        def failed():
            self.assertTrue(self.proc.inspection_started.wait(5))
            self.proc.check_started.set()
            return False
        with patch.object(review_inspection, 'qualified', return_value=True):
            self.assertFalse(self.f.overlap_review(self.card, self.root, self.base, failed))
        self.assertEqual(self.f.record(self.card)['review_inspections'][0]['state'], 'checks-failed')
        self.assertIsNone(review_inspection.final_reference(self.f, self.card, self.root, self.head, self.base))
        with self.assertRaisesRegex(Halt, 'verification evidence'):
            self.f.lane(self.card, 'review', self.root, self.base)

    def test_interrupted_checks_keep_inspection_reservation_spent_on_restart(self):
        self.proc.wait_for_check = False
        def interrupted():
            raise Stopped('test stop')
        with patch.object(review_inspection, 'qualified', return_value=True):
            with self.assertRaises(Stopped):
                self.f.overlap_review(self.card, self.root, self.base, interrupted)
            resumed = Factory(self.root, self.config, self.gh, self.proc)
            resumed.run_dir = self.root / '.sfc/runs/resumed'
            self.assertTrue(resumed.overlap_review(self.card, self.root, self.base, lambda: True))
        self.assertEqual(len(self.proc.inspectors), 1)
        self.assertEqual(resumed.record(self.card)['review_inspections'][0]['state'], 'checks-interrupted')
        self.assertIsNone(review_inspection.final_reference(resumed, self.card, self.root, self.head, self.base))

    def test_failed_dispatch_file_write_preserves_reservation_before_launch(self):
        self.proc.wait_for_check = False
        with patch.object(review_inspection, 'qualified', return_value=True), \
             patch.object(review_inspection, 'write_json', side_effect=OSError('dispatch file unavailable')):
            with self.assertRaisesRegex(OSError, 'dispatch file unavailable'):
                self.f.overlap_review(self.card, self.root, self.base, self.check)
        resumed = Factory(self.root, self.config, self.gh, self.proc)
        resumed.run_dir = self.root / '.sfc/runs/resumed'
        with patch.object(review_inspection, 'qualified', return_value=True):
            self.assertTrue(resumed.overlap_review(self.card, self.root, self.base, lambda: True))
        self.assertEqual(self.proc.inspectors, [])
        self.assertEqual(len(resumed.record(self.card)['review_inspections']), 1)
        self.assertEqual(resumed.record(self.card)['review_inspections'][0]['state'], 'reserved')

    def test_dirty_bytes_hidden_from_git_status_invalidate_overlap(self):
        def drift():
            self.assertTrue(self.check())
            git(self.root, 'update-index', '--assume-unchanged', 'value.txt')
            (self.root / 'value.txt').write_text('changed behind index\n')
            self.assertEqual(git(self.root, 'status', '--porcelain'), '')
            return True
        with patch.object(review_inspection, 'qualified', return_value=True), \
             self.assertRaisesRegex(Halt, 'Candidate changed'):
            self.f.overlap_review(self.card, self.root, self.base, drift)
        self.assertEqual(self.f.record(self.card)['review_inspections'][0]['state'], 'invalidated')

    def test_incomplete_inspection_falls_back_to_independent_final_review(self):
        self.proc.malformed_inspection = True
        with patch.object(review_inspection, 'qualified', return_value=True):
            self.assertTrue(self.f.overlap_review(self.card, self.root, self.base, self.check))
        self.assertEqual(self.f.record(self.card)['review_inspections'][0]['state'], 'unavailable')
        self.f.lane(self.card, 'review', self.root, self.base)
        self.assertNotIn('Completed provisional inspection', self.proc.prompts[-1])

    def test_disagreement_with_provisional_finding_preserves_outputs_and_blocks_acceptance(self):
        self.proc.adverse_inspection = True
        with patch.object(review_inspection, 'qualified', return_value=True):
            self.assertTrue(self.f.overlap_review(self.card, self.root, self.base, self.check))
        for change in (False, True):
            attempt = self.f.record(self.card)['review_inspections'][0]
            if change:
                Path(attempt['receipt']['log']).write_text('changed')
            with self.subTest(change=change), self.assertRaisesRegex(Halt, 'disagreed with a provisional finding'):
                self.f.lane(self.card, 'review', self.root, self.base)
        record = self.f.record(self.card)
        self.assertNotIn('review', record.get('receipts', {}))
        for dispatch in record['worker_output_budget']['review']['dispatches']:
            self.assertEqual(dispatch['status'], 'inspection-disagreement')
            self.assertTrue(Path(dispatch['result_path']).is_file())

    def test_release_snapshot_hashes_and_membership_pin_provisional_evidence(self):
        package = self.root / '.sfc/package.zip'
        package.parent.mkdir(exist_ok=True)
        package.write_bytes(b'frozen package')
        snapshot = {'membership': [1], 'artifacts': [{'path': str(package), 'sha256': checksum(package)}]}
        with patch.object(review_inspection, 'qualified', return_value=True):
            self.assertTrue(self.f.overlap_review(self.card, self.root, self.base, self.check, release_snapshot=snapshot))
        ref = review_inspection.final_reference(self.f, self.card, self.root, self.head, self.base,
                                                release_snapshot=snapshot)
        self.assertIsNotNone(ref)
        changed = {**snapshot, 'membership': [2]}
        self.assertIsNone(review_inspection.final_reference(self.f, self.card, self.root, self.head, self.base,
                                                             release_snapshot=changed))

    def test_package_inspection_inventory_lists_zip_members_without_extracting(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            archive = root / 'release.zip'
            with ZipFile(archive, 'w') as package:
                package.writestr('app/', '')
                package.writestr('app/INSTALL.md', 'instructions')
                package.writestr('app/start.py', 'program')
            malformed = root / 'malformed.zip'
            malformed.write_text('not an archive')
            large = root / 'many.zip'
            with ZipFile(large, 'w') as package:
                for index in range(257):
                    package.writestr(f'entry-{index}', '')
            refs = [{'path': str(path)} for path in (archive, malformed, large)]
            self.assertEqual(review_inspection.package_inventory(refs), {
                str(archive): ['app/INSTALL.md', 'app/start.py']})
            self.assertFalse((root / 'app').exists())
