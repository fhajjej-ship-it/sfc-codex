"""Accounting uses real supervisor entry points and isolated fake adapters."""
import concurrent.futures
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import sfc
from sfc_lib.common import Halt, Processes, Stopped, TimedOut, common_dir, read_json, write_json
from sfc_lib import delivery
from sfc_lib.factory import Factory
from benchmark_delivery import CASES, PACK, PREFLIGHT, SMOKE, Workers, benchmark, scenario
import test_factory as fixtures
import test_verification_reuse as reuse_fixtures


class DeliveryTests(unittest.TestCase):
    setUp = fixtures.IntegrationTests.setUp
    tearDown = fixtures.IntegrationTests.tearDown

    def test_normal_run_retains_report_and_offline_cli_needs_no_adapters(self):
        self.c['merge_policy'] = 'auto'
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        result = self.factory.run(allow_merge=True)
        saved = read_json(result['delivery_report']['path'])
        self.assertEqual(saved['run_id'], self.factory.run_dir.name)
        self.assertEqual(len(saved['current_run_execution_ids']), 6)
        self.assertEqual(len(saved['verification_references']), 2)
        self.assertTrue(all(r['reuse_miss_reason'] for r in saved['verification_references']))
        self.assertEqual(saved['tokens']['input_tokens']['unknown_executions'], 4)
        self.assertIsNone(saved['currency_estimate'])
        self.assertIsNone(saved['human_intervention_seconds'])
        self.assertGreater(saved['summed_worker_seconds'], 0)
        self.assertEqual(set(saved['stages']), set(delivery.STAGES))
        self.assertGreater(saved['current_run_administrative_count'], 0)
        self.assertTrue(all('argv' in r for r in saved['administrative_commands']))
        self.assertFalse(any('stdout' in r or 'env' in r or 'stdin' in r for r in saved['administrative_commands']))
        status_output = io.StringIO()
        with patch('sfc.Factory', return_value=self.factory), redirect_stdout(status_output):
            self.assertEqual(sfc.main(['--root', str(self.root), 'status']), 0)
        self.assertEqual(json.loads(status_output.getvalue())['delivery_report'], result['delivery_report'])
        output = io.StringIO()
        with patch('sfc.Factory', side_effect=AssertionError('offline must not instantiate adapters')), redirect_stdout(output):
            self.assertEqual(sfc.main(['--root', str(self.root), 'delivery-report']), 0)
        self.assertEqual(json.loads(output.getvalue()), delivery.report(self.root))

    def test_duplicate_worker_events_usage_and_command_identity(self):
        path = self.root / 'stream.jsonl'
        events = [
            {'type': 'thread.started', 'thread_id': 'thread-a'}, {'type': 'turn.started'},
            {'type': 'item.started', 'item': {'id': 'item-1', 'type': 'command_execution', 'command': 'check'}},
            {'type': 'item.completed', 'item': {'id': 'item-1', 'type': 'command_execution', 'command': 'check', 'exit_code': 0}},
            {'type': 'turn.completed', 'usage': {'input_tokens': 10, 'output_tokens': 3, 'cached_input_tokens': 4}},
        ]
        path.write_text('\n'.join(json.dumps(e) for e in events + events[-2:]) + '\nnot json\n')
        commands, usage, complete, unknown = delivery.stream(path)
        self.assertEqual(len(commands), 1)
        self.assertEqual(usage, {'input_tokens': 10, 'output_tokens': 3, 'cached_input_tokens': 4})
        self.assertTrue(complete)
        self.assertFalse(any(unknown.values()))
        with path.open('a') as handle:
            handle.write(json.dumps({'type': 'turn.started'}) + '\n')
        self.assertFalse(delivery.stream(path)[2])

    def test_native_zero_filled_turn_is_unknown_and_retains_measured_turn_subtotals(self):
        path = self.root / 'stream.jsonl'
        unavailable = dict.fromkeys((*delivery.TOKENS, 'cache_write_input_tokens', 'reasoning_output_tokens'), 0)
        observed = {'input_tokens': 10, 'output_tokens': 3, 'cached_input_tokens': 0}

        def completion(usage):
            return json.dumps({'type': 'turn.completed', 'usage': usage}) + '\n'

        path.write_text(completion(unavailable))
        self.assertEqual(delivery.stream(path)[1:], (dict.fromkeys(delivery.TOKENS, 0), True,
                                                    dict.fromkeys(delivery.TOKENS, False)))
        gaps = []
        self.assertEqual(delivery.stream(path, native=True, gaps=gaps)[1:],
                         (dict.fromkeys(delivery.TOKENS), False, dict.fromkeys(delivery.TOKENS, True)))
        self.assertEqual(gaps, ['native review token counters unavailable (zero-filled CLI usage)'])

        started = json.dumps({'type': 'turn.started'}) + '\n'
        path.write_text(started * 2 + completion(observed))
        self.assertEqual(delivery.stream(path, native=True)[1:],
                         (observed, True, dict.fromkeys(delivery.TOKENS, False)))
        with path.open('a') as handle:
            handle.write(started + completion(unavailable))
        self.assertEqual(delivery.stream(path, native=True)[1:],
                         (observed, False, dict.fromkeys(delivery.TOKENS, True)))

    def test_delivery_report_uses_recorded_native_adapter_for_zero_usage(self):
        f = self.factory
        f.run_dir = self.root / '.sfc/runs/native-usage'
        log = f.run_dir / 'review.log'
        log.parent.mkdir(parents=True)
        log.write_text(json.dumps({'type': 'turn.completed', 'usage': dict.fromkeys(delivery.TOKENS, 0)}) + '\n')
        with patch.object(self.proc, 'execute', return_value=0):
            f.execute(fixtures.issue(), 'review', ['codex', 'exec', 'review', '--json',
                      '--output-last-message', str(log.parent / 'result.json'), '-'], self.root, log, 10,
                      worker_events=True, accounting={'adapter': {'provider': 'codex-native'}})
        f.state.update(run_id=f.run_dir.name)
        f.save()

        report = delivery.report(self.root)
        self.assertEqual(report['executions'][0]['tokens'], dict.fromkeys(delivery.TOKENS))
        self.assertFalse(report['executions'][0]['usage_complete'])
        self.assertTrue(any('native review token counters unavailable' in gap for gap in report['unknowns']))
        self.assertEqual(report['tokens']['input_tokens'], {'known': 0, 'unknown_executions': 1})

    def test_concurrent_process_wall_time_is_not_summed_worker_time(self):
        f = self.factory
        f.run_dir = self.root / '.sfc/runs/concurrent'
        barrier = threading.Barrier(2)
        observed = []

        def process(argv, cwd, log, timeout, prompt=None, **kwargs):
            Path(log).parent.mkdir(parents=True, exist_ok=True)
            Path(log).write_text('fixture\n')
            barrier.wait(timeout=10)
            time.sleep(0.05)  # Ensure measured process overlap exceeds clock/IO resolution.
            observed.append(str(log))
            return 0

        with patch.object(self.proc, 'execute', side_effect=process):
            start = delivery.now()
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(f.execute, fixtures.issue(n), 'build', ['codex', 'exec'], self.root,
                                       f.run_dir / f'{n}.log', 10, worker_events=True) for n in (1, 2)]
                for future in futures:
                    self.assertEqual(future.result(), 0)
            f.state.update(run_id='concurrent', started=start, finished=delivery.now())
            f.save()
        value = delivery.report(self.root)
        self.assertEqual(len(value['current_run_execution_ids']), 2)
        self.assertGreater(value['summed_worker_seconds'], value['stages']['build']['wall_seconds'])
        self.assertEqual(len(observed), 2)

    def test_partial_turn_usage_retains_known_tokens_and_marks_only_missing_fields(self):
        original = self.proc.execute

        def execute(argv, cwd, log, timeout, prompt=None, **kwargs):
            code = original(argv, cwd, log, timeout, prompt, **kwargs)
            if argv[0] == 'codex':
                events = [
                    {'type': 'thread.started', 'thread_id': str(log)},
                    {'type': 'turn.started'},
                    {'type': 'turn.completed', 'usage': {
                        'input_tokens': 10, 'output_tokens': 2, 'cached_input_tokens': 4}},
                    {'type': 'turn.started'},
                    {'type': 'turn.completed', 'usage': {'input_tokens': 5, 'output_tokens': 1}},
                ]
                Path(log).write_text('\n'.join(json.dumps(e) for e in events))
            return code

        with patch.object(self.proc, 'execute', side_effect=execute):
            self.factory.run()
        value = delivery.report(self.root)
        self.assertEqual(value['tokens']['input_tokens'], {'known': 60, 'unknown_executions': 0})
        self.assertEqual(value['tokens']['output_tokens'], {'known': 12, 'unknown_executions': 0})
        self.assertEqual(value['tokens']['cached_input_tokens'], {'known': 16, 'unknown_executions': 4})

    def test_inspection_report_pins_intersections_and_preserves_unknown_costs(self):
        run = self.root / '.sfc/runs/overlap'
        log = run / 'worker.log'
        log.parent.mkdir(parents=True)
        log.write_text(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 10}}) + '\n')
        prefix = '2026-11-13T12:00:'

        def observation(identity, stage, start, end, **extra):
            row = {'id': identity, 'run_id': run.name, 'issue': 1, 'head': 'candidate', 'base': 'base',
                   'config_digest': 'config', 'task_digest': 'task', 'kind': 'process',
                   'stage': stage, 'started': prefix + start + '+00:00', 'exit_code': 0,
                   **extra}
            if end:
                row.update(finished=prefix + end + '+00:00', elapsed_seconds=float(end) - float(start))
            if row.get('worker'):
                row.update(log=str(log), log_sha256=delivery.checksum(log))
            write_json(run / 'delivery' / identity / 'completed.json', row)

        observation('inspection', 'review', '01', '05', lane='inspection', worker=True)
        observation('failed-check', 'checks', '02', '04', exit_code=1)
        observation('other-candidate', 'checks', '01', '05', head='different')
        observation('other-task', 'checks', '01', '05', task_digest='different')
        observation('other-base', 'checks', '01', '05', base='different')
        observation('other-config', 'checks', '01', '05', config_digest='different')
        observation('other-card', 'checks', '01', '05', issue=2)
        observation('final', 'review', '06', '07', lane='review', worker=True, inspection_sha256='original')
        # A launched process without completion retains its unknown time and usage.
        observation('interrupted', 'review', '08', None, lane='inspection', worker=True,
                    exit_code=None, launch_observed=True)
        observation('unlaunched', 'review', '08', None, lane='inspection', worker=True, exit_code=None)
        self.factory.state.update(run_id=run.name)
        self.factory.save()
        value = delivery.report(self.root)
        measured = value['inspection_overlap']
        self.assertEqual(measured['wall_seconds'], 2)
        self.assertEqual(measured['unknown_intervals'], 1)
        self.assertEqual(measured['inspection']['calls'], 2)
        self.assertEqual(measured['inspection']['unconfirmed_launches'], 1)
        self.assertEqual(measured['inspection']['unknown_durations'], 1)
        self.assertEqual(measured['final_assessment']['compute_seconds'], 1)
        self.assertEqual(measured['final_assessment']['tokens']['input_tokens']['known'], 10)
        self.assertEqual(measured['final_assessment']['tokens']['output_tokens']['unknown_executions'], 1)
        self.assertEqual(value['unknown_compute_durations'], 1)
        self.assertEqual(value, delivery.report(self.root))

    def test_failed_and_hard_interrupted_attempts_are_not_lost_or_retried(self):
        f = self.factory
        f.run_dir = self.root / '.sfc/runs/interrupted'
        with patch.object(self.proc, 'execute', side_effect=Stopped('fixture stop')):
            with self.assertRaises(Stopped):
                f.execute(fixtures.issue(), 'checks', ['sh', '-c', 'false'], self.root, f.run_dir / 'check.log', 10)
        folder = f.run_dir / 'delivery/hard-interruption'
        write_json(folder / 'reserved.json', {'id': folder.name, 'run_id': f.run_dir.name,
                   'issue': 1, 'stage': 'build', 'kind': 'process', 'worker': True, 'started': delivery.now()})
        f.state.update(run_id=f.run_dir.name)
        f.save()
        first = delivery.report(self.root)
        second = delivery.report(self.root)
        self.assertEqual(first, second)
        self.assertEqual(len(first['executions']), 2)
        self.assertEqual(first['stages']['build']['unknown_durations'], 1)
        self.assertIn('Stopped', str(first))
        self.assertEqual(f.record(fixtures.issue())['attempts'], 0)

    def test_normal_scheduler_accounts_concurrent_workers(self):
        self.gh.items.append(fixtures.issue(2))
        barrier = threading.Barrier(2)

        def overlap(lane, cwd):
            if lane == 'build':
                barrier.wait(timeout=10)
                time.sleep(0.05)

        self.proc.on_lane = overlap
        self.factory.run()
        value = delivery.report(self.root)
        self.assertEqual(self.proc.lanes.count('build'), 2)
        self.assertEqual(len([r for r in value['executions'] if r.get('worker')]), 8)
        self.assertGreater(value['stages']['build']['summed_process_seconds'],
                           value['stages']['build']['wall_seconds'])
        self.assertEqual({r['issue'] for r in value['executions']}, {1, 2})

    def test_queued_ticket_without_dispatch_accumulates_observed_queue_time(self):
        self.gh.items[0]['body'] += '\n## Blocked by\n- #9\n'
        self.gh.extra_issues[9] = fixtures.issue(9)
        self.factory.run()
        value = delivery.report(self.root)
        self.assertEqual(value['executions'], [])
        self.assertGreater(value['stages']['queueing']['wall_seconds'], 0)

    def test_queue_does_not_carry_ready_into_a_later_backlog_run(self):
        f = self.factory

        def tick(n):
            return f'2026-01-01T00:00:{n:02d}+00:00'

        def observe(n, status):
            with patch('sfc_lib.delivery.now', return_value=tick(n)):
                delivery.observe_queue(f, [fixtures.issue(status=status)])

        f.run_dir = self.root / '.sfc/runs/ready'
        f.state.update(run_id=f.run_dir.name, started=tick(0), outcome='running')
        observe(2, 'Ready')
        observe(8, 'Backlog')
        observe(10, 'Ready')
        f.state.update(finished=tick(15))
        write_json(f.run_dir / 'summary.json', f.state)

        f.run_dir = self.root / '.sfc/runs/backlog'
        f.state.update(run_id=f.run_dir.name, started=tick(20), finished=None)
        observe(25, 'Backlog')
        f.state.update(finished=tick(40))
        f.save()

        self.assertEqual(delivery.report(self.root)['stages']['queueing']['wall_seconds'], 0)
        first = delivery.report(self.root, run_id='ready')
        self.assertEqual(first['stages']['queueing']['wall_seconds'], (8 - 2) + (15 - 10))

    def test_frozen_release_excludes_later_execution_and_clips_completion(self):
        f = self.factory
        f.run_dir = self.root / '.sfc/runs/release'
        f.state.update(run_id=f.run_dir.name)
        f.save()

        def tick(n):
            return f'2026-01-01T00:00:{n:02d}+00:00'

        def receipt(identifier, issue, started, finished, **extra):
            path = f.run_dir / 'delivery' / identifier
            row = {'id': identifier, 'run_id': f.run_dir.name, 'issue': issue,
                   'started': tick(started), 'finished': tick(finished), 'stage': 'build',
                   'kind': 'process', 'worker': True, 'exit_code': 0,
                   'elapsed_seconds': finished - started, **extra}
            if row.get('worker'):
                log = path / 'worker.log'
                log.parent.mkdir(parents=True)
                log.write_text(json.dumps({'type': 'turn.completed', 'usage': {
                    'input_tokens': 10, 'output_tokens': 2, 'cached_input_tokens': 1}}))
                row.update(log=str(log), log_sha256=delivery.checksum(log))
            write_json(path / 'reserved.json', row)
            write_json(path / 'completed.json', row)

        receipt('frozen-work', 1, 1, 6)
        receipt('completion', 2, 17, 21, stage='publication', kind='publication-completion', worker=False)
        write_json(common_dir(self.root) / 'sfc-releases/1.0.0.json', {
            'workflow_kind': 'release', 'version': '1.0.0', 'state': 'done', 'card': {'number': 2},
            'changes': {'issues': [1]}, 'created': tick(10), 'delivery_verified_at': tick(20)})
        first = delivery.report(self.root)['releases'][0]
        receipt('reopened-before-verification', 1, 11, 16)
        receipt('reopened-after-verification', 1, 30, 50)
        receipt('later-release-command', 2, 35, 37, stage='publication')
        current = delivery.report(self.root)
        self.assertEqual(current['releases'][0], first)
        self.assertCountEqual(first['execution_ids'], ['frozen-work', 'completion'])
        self.assertEqual(first['stages']['build']['wall_seconds'], 5)
        self.assertEqual(first['stages']['publication']['wall_seconds'], 3)
        self.assertEqual(first['tokens']['input_tokens']['known'], 10)
        self.assertEqual(current['tokens']['input_tokens']['known'], 40)

    def test_confirmed_child_timeout_counts_once_even_without_completion_receipt(self):
        f = self.factory
        f.run_dir = self.root / '.sfc/runs/timeout'
        f.state.update(run_id=f.run_dir.name)
        f.save()
        f.processes = Processes(self.root / '.sfc/stop')
        log = f.run_dir / 'worker.log'
        with self.assertRaises(TimedOut):
            f.execute(fixtures.issue(), 'build', [sys.executable, '-u', '-c',
                      'import time; print("child launched"); time.sleep(60)'],
                      self.root, log, 1, worker_events=True)
        self.assertIn('child launched', log.read_text())
        self.assertFalse(f.processes.children)
        first = delivery.report(self.root)
        row, = first['executions']
        self.assertEqual(first['observed_process_count'], 1)
        self.assertEqual(first['tokens']['input_tokens']['unknown_executions'], 1)
        self.assertEqual(row['error_type'], 'TimedOut')
        self.assertNotIn('exit_code', row)
        self.assertTrue(row['launch_observed'])
        Path(row['evidence']).unlink()  # Keep only the durable launch and reservation.
        recovered = delivery.report(self.root)
        self.assertEqual(recovered['current_run_execution_ids'], [row['id']])
        self.assertEqual(recovered['observed_process_count'], 1)
        self.assertEqual(recovered['stages']['build']['unknown_durations'], 1)

    def test_bad_worker_event_marks_gap_and_preserves_original_scheduler_halt(self):
        f = self.factory
        original = Halt('fixture scheduler halted')

        def execute(argv, cwd, log, timeout, prompt=None, **kwargs):
            Path(log).write_text('\n'.join(json.dumps(event) for event in [
                {'type': 'thread.started', 'thread_id': 'valid-thread'},
                {'type': 'item.started', 'item': {'type': 'command_execution', 'id': ['invalid']}},
                {'type': 'turn.completed', 'turn_id': {'invalid': 1}, 'usage': {}},
                {'type': 'turn.completed', 'turn_id': 'valid-turn', 'usage': {'input_tokens': 10}},
            ]))
            return 0

        def schedule(factory, watcher):
            with patch.object(factory.processes, 'execute', side_effect=execute):
                factory.execute(fixtures.issue(), 'build', ['codex', 'exec'], self.root,
                                factory.run_dir / 'worker.log', 10, worker_events=True)
            raise original

        with patch('sfc_lib.scheduler.schedule', side_effect=schedule), self.assertRaises(Halt) as caught:
            f.run()
        self.assertIs(caught.exception, original)
        summary = read_json(f.run_dir / 'summary.json')
        self.assertEqual(summary['outcome'], 'halted')
        saved = read_json(summary['delivery_report']['path'])
        self.assertTrue(any('malformed worker telemetry at line 2' in gap for gap in saved['unknowns']))
        self.assertEqual(saved['tokens']['input_tokens'], {'known': 10, 'unknown_executions': 1})
        self.assertEqual(delivery.report(self.root)['tokens'], saved['tokens'])

    def test_report_builder_failure_keeps_original_halt_and_records_gap(self):
        f = self.factory
        original = Halt('fixture scheduler halted')
        with patch('sfc_lib.scheduler.schedule', side_effect=original), \
                patch('sfc_lib.delivery.report', side_effect=TypeError('fixture malformed receipt')), \
                self.assertRaises(Halt) as caught:
            f.run()
        self.assertIs(caught.exception, original)
        summary = read_json(f.run_dir / 'summary.json')
        gap = summary['delivery_report']
        self.assertEqual(gap, read_json(gap['path']))
        self.assertTrue(gap['reporting_gap'])
        self.assertEqual(gap['unknowns'], ['Delivery report unavailable: TypeError'])

    def test_capture_write_failure_does_not_replace_original_command_error(self):
        f = self.factory
        f.run_dir = self.root / '.sfc/runs/capture-gap'
        f.state.update(run_id=f.run_dir.name)
        original = TimedOut('fixture original error')
        keep = delivery.write_json

        def write(path, value):
            if path.name == 'completed.json':
                raise OSError('fixture accounting write failed')
            return keep(path, value)

        with patch.object(delivery, 'write_json', side_effect=write), \
                patch.object(self.proc, 'execute', side_effect=original), \
                self.assertRaises(TimedOut) as caught:
            f.execute(fixtures.issue(), 'checks', ['sh', '-c', 'false'], self.root, f.run_dir / 'check.log', 10)
        self.assertIs(caught.exception, original)
        f.save()
        self.assertTrue(any('observation completion unavailable' in gap for gap in delivery.report(self.root)['unknowns']))


    def test_model_settings_are_requested_not_invented_effective_settings(self):
        self.assertEqual(delivery.settings(['codex', 'exec'])['model'], None)
        actual = delivery.settings(['codex', 'exec', '-m', 'fixture-model', '-c', 'model_reasoning_effort="high"',
                                    '-c', 'service_tier="fast"'])
        self.assertEqual(actual['model'], 'fixture-model')
        self.assertEqual(actual['reasoning_effort'], 'high')
        self.assertEqual(actual['service_tier'], 'fast')
        self.assertIsNone(actual['provider'])

    def test_available_usage_receipt_copies_restart_and_tampering(self):
        original = self.proc.execute

        def execute(argv, cwd, log, timeout, prompt=None, **kwargs):
            code = original(argv, cwd, log, timeout, prompt, **kwargs)
            if argv[0] == 'codex':
                events = [{'type': 'thread.started', 'thread_id': str(log)}, {'type': 'turn.started'},
                          {'type': 'turn.completed', 'usage': {'input_tokens': 10, 'output_tokens': 2}}]
                Path(log).write_text('\n'.join(json.dumps(e) for e in events))
            return code

        with patch.object(self.proc, 'execute', side_effect=execute):
            self.factory.run()
        value = delivery.report(self.root)
        self.assertEqual(value['tokens']['input_tokens'], {'known': 40, 'unknown_executions': 0})
        self.assertEqual(value['tokens']['cached_input_tokens'], {'known': 0, 'unknown_executions': 4})
        first = next(row for row in value['executions'] if row.get('worker'))
        write_json(self.factory.run_dir / 'issue-1/copied/receipt.json',
                   {'lane': 'build', 'log': first['log'], 'elapsed_seconds': 999})
        self.assertEqual(len(delivery.report(self.root)['executions']), len(value['executions']))
        self.factory.run()
        resumed = delivery.report(self.root)
        self.assertEqual(resumed['tokens']['input_tokens'], {'known': 0, 'unknown_executions': 0})
        self.assertEqual(resumed['summed_worker_seconds'], 0)
        Path(first['log']).write_text('tampered')
        changed = delivery.report(self.root, run_id=value['run_id'])
        self.assertTrue(any('changed log hash' in gap for gap in changed['unknowns']))


class DeliveryReuseTests(unittest.TestCase):
    setUp = reuse_fixtures.ReuseTests.setUp
    tearDown = reuse_fixtures.ReuseTests.tearDown
    enable = reuse_fixtures.ReuseTests.enable
    verify = reuse_fixtures.ReuseTests.verify

    def test_restart_reuse_keeps_original_bytes_and_charges_zero_new_executions(self):
        self.enable()
        original = self.verify()
        original_path = original['checks'][0]['receipt_path']
        before = Path(original_path).read_bytes()
        f = Factory(self.root, self.config, self.factory.gh, self.processes)
        f.run_dir = self.root / '.sfc/runs/resumed'
        f.run_dir.mkdir(parents=True)
        f.state.update(run_id='resumed')
        self.assertTrue(f.verify(self.card, self.root, self.head))
        value = delivery.report(self.root, run_id='resumed')
        self.assertEqual(value['current_run_execution_ids'], [])
        self.assertEqual(len(value['executions']), 1)
        self.assertEqual(value['summed_worker_seconds'], 0)
        reused = [r for r in value['verification_references'] if r['run_id'] == 'resumed']
        self.assertEqual(reused[0]['reuse']['receipt_path'], original_path)
        self.assertEqual(Path(original_path).read_bytes(), before)
        self.assertEqual(value['tokens']['input_tokens']['unknown_executions'], 0)


class DeliveryScenariosTests(unittest.TestCase):
    def test_saved_draft_route_checks_assets_and_recovers_without_publication(self):
        from sfc_lib.efficiency_benchmark import DRAFT_BOUNDARY, compare, evaluate
        from test_releases import config
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            saved = config()
            saved.update(merge_mode='local-verification', merge_policy='auto', worker_timeout=120,
                         verify_commands=['python3 -m unittest discover -s tests -v',
                                          'python3 scripts/validate_plugin.py'])
            saved['release'].update(package_commands=['python3 scripts/package_plugin.py'],
                                    verify_commands=['python3 scripts/verify_release_package.py'],
                                    artifacts=['dist/sfc-{version}.zip', 'dist/sfc-{version}.zip.sha256'])
            saved['release']['authority']['mode'] = 'draft'
            config_path = folder / 'project/.sfc/config.json'
            write_json(config_path, saved)
            output = folder / 'comparison'
            result = compare(Path(__file__).resolve().parents[1], output, cases=['interrupted-release'],
                             profile='fresh', saved_config=config_path, draft_only=True)
            self.assertEqual(result['completion_boundary'], DRAFT_BOUNDARY)
            pair = result['profiles']['fresh']['pairs']['interrupted-release']
            for arm in ('baseline', 'candidate'):
                outcome = pair[arm]['outcome']
                self.assertEqual(outcome['publish_count'], 0)
                self.assertEqual(outcome['observed'], 'verified-draft')
                self.assertEqual(outcome['recovery']['additional_workers'], 0)
                self.assertEqual(outcome['recovery']['additional_remote_writes'], [])
                worker_logs = [Path(p['log']) for p in pair[arm]['processes'] if p['worker']]
                prompts = [log.with_name('prompt.md').read_text() for log in worker_logs]
                release_prompt = next(p for p in prompts if 'unpublished GitHub draft' in p)
                self.assertIn('pending separate publication authority', release_prompt)
                self.assertNotIn('Verify the requested GitHub publication', release_prompt)
            archive = Path(pair['candidate']['outcome']['downloaded_install']['artifact'])
            archive.with_suffix('.zip.sha256').write_text('tampered')
            with self.assertRaisesRegex(Halt, 'unpublished asset set'):
                evaluate(output / 'qualification-input.json')

    def test_normal_comparison_arms_share_release_qa_instructions(self):
        from sfc_lib.efficiency_benchmark import compare, evaluate, reference

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'paired'
            compare(Path(__file__).resolve().parents[1], output, cases=['ordinary-feature'], profile='fresh',
                    worker_timeout_seconds=300)
            paths = [output / 'runs/ordinary-feature' / arm / 'repo/AGENTS.md' for arm in ('released', 'fresh')]
            instruction = paths[0].read_bytes()
            self.assertEqual(instruction, paths[1].read_bytes())
            self.assertIn(b'frozen source range and all included commits', instruction)
            self.assertIn(b'extracted CLI checks', instruction)
            self.assertIn(b'concrete discrepancy', instruction)
            self.assertNotIn(b'json.dumps', instruction)
            self.assertNotIn(b'v0.1.17', instruction)
            plan_path = output / 'comparison-plan.json'
            plan = read_json(plan_path)
            self.assertEqual((plan['worker_timeout_seconds'], plan['maximum_worker_seconds']), (300, 4200))
            progress = read_json(output / 'comparison-progress.json')
            self.assertTrue(progress['complete'])
            self.assertEqual(progress['input'], reference((output / 'qualification-input.json').resolve()))
            self.assertEqual(progress['rows'], read_json(output / 'qualification-input.json')['rows'])
            for path in paths:
                arm = path.parent.parent
                result = read_json(arm / 'scenario-outcome.json')
                self.assertEqual(result['observed'], 'done')
                self.assertIn("Installed command --status returned 'Feature enabled'",
                              Path(result['downloaded_install']['log']).read_text())
                self.assertEqual(read_json(arm / 'scenario.json')['fixture_worker_timeout_seconds'], 300)
                events = map(json.loads, (arm / 'adapter-executions.jsonl').read_text().splitlines())
                self.assertEqual([row['timeout'] for row in events if row.get('worker')], [300] * 7)

            manifest_path = output / 'qualification-input.json'
            manifest = read_json(manifest_path)
            for changes, reason in (({'worker_timeout_seconds': 301}, 'Benchmark worker timeout'),
                                    ({'maximum_worker_seconds': 4201}, 'planned worker time budget'),
                                    ({'worker_timeout_seconds': 299, 'maximum_worker_seconds': 4186},
                                     'predeclared worker limits')):
                with self.subTest(changes=changes), self.assertRaisesRegex(Halt, reason):
                    write_json(plan_path, {**plan, **changes})
                    write_json(manifest_path, {**manifest, 'plan': reference(plan_path)})
                    evaluate(manifest_path)

    def test_historical_worker_timeout_plan_still_evaluates(self):
        from sfc_lib.efficiency_benchmark import compare, evaluate, reference

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'paired'
            compare(Path(__file__).resolve().parents[1], output, cases=['security-sensitive'], profile='fresh')
            plan_path = output / 'comparison-plan.json'
            plan = read_json(plan_path)
            self.assertEqual(plan['worker_timeout_seconds'], 120)
            plan.pop('maximum_worker_seconds')
            write_json(plan_path, plan)
            manifest_path = output / 'qualification-input.json'
            write_json(manifest_path, {**read_json(manifest_path), 'plan': reference(plan_path)})
            self.assertFalse(evaluate(manifest_path)['profiles']['fresh']['qualified'])

    def test_legacy_release_bindings_match_original_receipts_and_fail_on_drift(self):
        from benchmark_delivery import legacy_release_binding
        from sfc_lib.efficiency_benchmark import compare, evaluate, reference
        from sfc_lib.releases import digest

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'paired'
            report = compare(Path(__file__).resolve().parents[1], output, cases=['documentation-correction'],
                             profile='fresh', legacy_release_bindings=True)
            self.assertIn('fixture-supplied', report['baseline_release_evidence'])
            released = output / 'runs/documentation-correction/released'
            current = released.with_name('fresh')
            bindings = sorted(released.glob('repo/.sfc/runs/*/issue-2/*/legacy-release-bindings.json'))
            self.assertEqual(len(bindings), 3)
            self.assertEqual(list(current.rglob('legacy-release-bindings.json')), [])
            for path in bindings:
                data = read_json(path)
                self.assertEqual(data['digests'], {k: digest(v) for k, v in data['inputs'].items()})
                self.assertIn(str(path.resolve()), path.with_name('prompt.md').read_text())
            manifest = output / 'qualification-input.json'
            value = read_json(manifest)
            row = next(row for row in value['rows'] if row['arm'] == 'released')
            scenario_path = Path(row['scenario']['path'])
            scenario = read_json(scenario_path)
            self.assertTrue(scenario.pop('legacy_release_bindings'))
            write_json(scenario_path, scenario)
            row['scenario'] = reference(scenario_path)
            write_json(manifest, value)
            with self.assertRaisesRegex(Halt, 'evidence adapter differs'):
                evaluate(manifest)

            journal = read_json(released / 'repo/.git/sfc-releases/1.0.0.json')
            area = Path(journal['candidate_area'])
            prompt = bindings[0].with_name('prompt.md').read_text()
            path = Path(journal['project_logs'][0]['log'])
            path.write_text('changed receipt log')
            with self.assertRaisesRegex(Halt, 'verification log changed'):
                legacy_release_binding(bindings[0].parent, area, prompt)

    def test_fixture_preflight_still_blocks_corrupt_package_before_release_lanes(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / 'corrupt'
            original = Workers.execute

            def corrupt(worker, argv, cwd, log, timeout, prompt=None, **kwargs):
                code = original(worker, argv, cwd, log, timeout, prompt, **kwargs)
                if argv[-1] == PACK and code == 0:
                    (Path(cwd) / 'dist/delivery-fixture.zip').write_bytes(b'corrupt ZIP')
                return code

            with patch.object(Workers, 'execute', corrupt), self.assertRaises(Halt):
                scenario(folder, CASES[0], profile='fresh', normal_profile=True, worker_cap=7,
                         fixture_worker_timeout=120)
            root = folder / 'repo'
            journal = read_json(root / '.git/sfc-releases/1.0.0.json')
            self.assertEqual(journal['preflight_checks']['state'], 'failed')
            self.assertIn('BadZipFile', Path(journal['preflight_checks']['checks'][0]['log']).read_text())
            self.assertNotIn('project_check_state', journal)
            self.assertNotIn('publish', journal['effects'])
            self.assertEqual(list((root / '.sfc/runs').glob('*/issue-2/qa-*/worker.log')), [])

    def test_normal_profile_verifies_the_downloaded_installed_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / 'version'
            case = next(case for case in CASES if case['name'] == 'package-version-correction')
            outcome = scenario(folder, case, profile='fresh', normal_profile=True, worker_cap=7,
                               fixture_worker_timeout=120)
            self.assertEqual(outcome['observed'], 'done')
            self.assertEqual(outcome['before_release_version'], '0.9.0')
            self.assertEqual(outcome['release_version'], '1.0.0')
            self.assertGreater(outcome['delivery_seconds'], outcome['publication_verified_seconds'])
            installed = outcome['downloaded_install']
            self.assertIn("Installed command --version returned '1.0.0'", Path(installed['log']).read_text())
            self.assertIn(installed['artifact_sha256'], Path(installed['log']).read_text())
            root = folder / 'repo'
            journal = read_json(root / '.git/sfc-releases/1.0.0.json')
            self.assertEqual([row['command'] for row in journal['preflight_checks']['checks']], [PREFLIGHT])
            self.assertEqual([row['command'] for row in journal['artifact_checks']['checks']], [SMOKE])
            self.assertIn('Archive inventory and CRC verified',
                          Path(journal['preflight_checks']['checks'][0]['log']).read_text())
            stale = subprocess.run([sys.executable, 'scripts/package_fixture.py', 'smoke',
                                    str(folder / 'before-package.zip')], cwd=root, capture_output=True,
                                   text=True, timeout=10)
            self.assertNotEqual(stale.returncode, 0)
            self.assertIn('Installed bytes differ from pinned source: VERSION', stale.stderr)

    def test_six_bounded_normal_workflows_and_publication_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            value = benchmark(Path(tmp) / 'evidence')
            self.assertEqual(len(value['scenarios']), 6)
            for row in value['scenarios']:
                if row['case'] == 'security-sensitive':
                    self.assertEqual(row['observed'], 'human-handoff')
                    continue
                self.assertEqual(row['observed'], 'done')
                self.assertEqual(row['publish_count'], 1)
                scenario_root = Path(row['report']).parent
                frozen = read_json(scenario_root / 'repo/.git/sfc-releases/1.0.0.json')
                self.assertIsNotNone(frozen['boundary'])
                self.assertEqual(frozen['changes']['unlinked_commits'], [])
                self.assertNotIn('purpose is not inferred', frozen['public_notes']['text'])
                saved = read_json(row['report'])
                self.assertEqual(saved['releases'][0]['work_tickets'], [1])
                self.assertEqual(saved['releases'][0]['release_ticket']['number'], 2)
                self.assertIsNotNone(saved['releases'][0]['verified_at'])
                self.assertIsNone(saved['releases'][0]['ready_to_verified_seconds'])
                self.assertGreater(saved['releases'][0]['ready_observed_to_verified_seconds'], 0)
                self.assertGreater(saved['releases'][0]['stages']['publication']['wall_seconds'], 0)
                linked_workers = [r for r in saved['executions'] if r.get('worker') and
                                  r['id'] in saved['releases'][0]['execution_ids']]
                self.assertEqual(saved['releases'][0]['tokens']['input_tokens'],
                                 {'known': 0, 'unknown_executions': len(linked_workers)})
                self.assertGreater(len(linked_workers), 0)
                if row['case'] == 'package-version-correction':
                    self.assertEqual(row['before_release_version'], '0.9.0')
                    self.assertEqual(row['release_version'], '1.0.0')
                    fixture = Path(row['report']).parent
                    self.assertEqual((fixture / 'before-package-version.txt').read_text(), '0.9.0\n')
                    package = saved['releases'][0]['check_receipts']['package']
                    self.assertEqual(package['checks'][0]['command'], 'mkdir -p dist && cp VERSION dist/package-version.txt')
                if row['case'] == 'interrupted-release':
                    self.assertEqual(saved['summed_worker_seconds'], 0)
                    self.assertEqual(saved['tokens']['input_tokens']['unknown_executions'], 0)
                    self.assertGreater(saved['releases'][0]['summed_worker_seconds'], 0)
            self.assertEqual({r['case'] for r in value['scenarios']}, {c['name'] for c in CASES})


if __name__ == '__main__':
    unittest.main()
