"""Qualification cannot turn synthetic or incomplete evidence into measured gains."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from sfc_lib.common import Halt, write_json
from sfc_lib import efficiency_benchmark as bench


class QualificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.runtime = {}
        for name in ('released', 'current'):
            root = self.root / name
            script = root / 'scripts' / 'runtime.py'
            script.parent.mkdir(parents=True)
            script.write_text(name)
            self.runtime[name] = {'root': str(root), 'sha256': bench.runtime_digest(root),
                                  'commit': bench.BASELINE if name == 'released' else 'current-source'}

    def test_original_and_retry_count_once_while_unknown_usage_stays_unknown(self):
        evidence = []
        for index in (1, 2):
            log = self.root / f'run-{index}/issue-1/qa-fixture/worker.log'
            log.parent.mkdir(parents=True)
            log.write_text(json.dumps({'type': 'turn.completed',
                           'usage': {'input_tokens': 10, 'output_tokens': 3, 'cached_input_tokens': 4}})
                           if index == 2 else 'partial startup\n')
            evidence.extend([
                {'id': str(index), 'event': 'started', 'log': str(log), 'worker': True,
                 'argv': ['codex', 'exec', '-m', 'fixture', '-c', 'model_reasoning_effort="high"']},
                {'id': str(index), 'event': 'launched'},
                {'id': str(index), 'event': 'completed', 'elapsed_seconds': index,
                 'log_sha256': bench.checksum(log), **({'exit_code': 0} if index == 2 else {'error_type': 'TimedOut'})},
            ])
        trace = self.root / 'adapter-executions.jsonl'
        trace.write_text('\n'.join(json.dumps(row) for row in evidence + [evidence[-1]]))
        metrics = bench.arm_metrics({'adapter_trace': bench.reference(trace), 'exit_code': 0})
        self.assertEqual(metrics['retries'], {'issue-1/qa': 1})
        self.assertEqual(metrics['failed_execution_ids'], ['1'])
        self.assertEqual(metrics['worker_seconds'], 3)
        self.assertEqual(metrics['tokens']['input_tokens'], {'known': 10, 'unknown_executions': 1})
        self.assertEqual(metrics['tokens']['cached_input_tokens'], {'known': 4, 'unknown_executions': 1})
        self.assertIsNone(bench.normalized_tokens(metrics))
        self.assertIsNone(metrics['billing'])

    def test_quality_requires_critical_detection_complete_coverage_and_no_new_intervention(self):
        observed = {'outcome': {'observed': 'done', 'candidate_tree': 'same', 'human_handoffs': 0},
                    'arm_exit_code': 0}
        good = {'critical_expected': 1, 'critical_detected': 1, 'criteria_expected': 2, 'criteria_covered': 2,
                'false_positives': 0, 'human_interventions': 0}
        self.assertEqual(bench.quality_for('ordinary-feature', observed, observed, None)[0], 'inconclusive')
        self.assertEqual(bench.quality_for('ordinary-feature', observed, observed,
                         {'released': good, 'current': good})[0], 'met')
        for field, bad in (('critical_detected', 0), ('criteria_covered', 1),
                           ('false_positives', 1), ('human_interventions', 1)):
            with self.subTest(field=field):
                self.assertEqual(bench.quality_for('ordinary-feature', observed, observed,
                                 {'released': good, 'current': {**good, field: bad}})[0], 'missed')
        for field, bad in (('critical_detected', 0), ('criteria_covered', 1)):
            with self.subTest(baseline_field=field):
                self.assertEqual(bench.quality_for('ordinary-feature', observed, observed,
                                 {'released': {**good, field: bad}, 'current': good})[0], 'inconclusive')

    def test_native_usage_sentinel_cannot_enter_comparable_token_totals(self):
        log = self.root / 'issue-1/review-fixture/worker.log'
        log.parent.mkdir(parents=True)
        argv = ['codex', 'exec', '-c', 'model_reasoning_effort="high"', '-C', str(self.root), 'review',
                '--json', '--output-last-message', str(log.parent / 'result.json'), '-']
        trace = self.root / 'adapter-executions.jsonl'
        for usage, expected in ((dict.fromkeys(bench.delivery.TOKENS, 0), None),
                                ({'input_tokens': 10, 'output_tokens': 3, 'cached_input_tokens': 0}, 13)):
            with self.subTest(usage=usage):
                log.write_text(json.dumps({'type': 'turn.completed', 'usage': usage}) + '\n')
                events = [{'id': 'review', 'event': 'started', 'log': str(log), 'worker': True, 'argv': argv},
                          {'id': 'review', 'event': 'launched'},
                          {'id': 'review', 'event': 'completed', 'exit_code': 0, 'elapsed_seconds': 1,
                           'log_sha256': bench.checksum(log)}]
                trace.write_text('\n'.join(map(json.dumps, events)))
                result = bench.arm_metrics({'adapter_trace': bench.reference(trace), 'exit_code': 0})
                self.assertEqual(bench.normalized_tokens(result), expected)
                self.assertEqual(result['worker_seconds'], 1)

    def test_downloaded_install_proof_rejects_changed_archive_or_log(self):
        artifact = self.root / 'downloaded.zip'
        log = self.root / 'install.log'
        artifact.write_bytes(b'verified archive fixture')
        log.write_text('Installed command passed\n')
        outcome = self.root / 'outcome.json'
        value = {'downloaded_install': {'artifact': str(artifact), 'artifact_sha256': bench.checksum(artifact),
                                       'log': str(log), 'log_sha256': bench.checksum(log)}}
        write_json(outcome, value)
        row = {'outcome': bench.reference(outcome), 'exit_code': 0}
        self.assertEqual(bench.arm_metrics(row)['outcome'], value)
        for target in (artifact, log):
            original = target.read_bytes()
            target.write_bytes(b'changed')
            with self.assertRaisesRegex(Halt, 'Downloaded installation evidence changed'):
                bench.arm_metrics(row)
            target.write_bytes(original)

    def make_input(self, execution, cases=bench.CASES, kind='delivery'):
        path = self.root / 'qualification-input.json'
        write_json(path, {'version': 1, 'runtimes': self.runtime, 'execution': execution,
                          'evaluation_wall_seconds': 40, 'profiles': ['fresh'], 'comparison_kind': kind,
                          'rows': [{'case': case, 'arm': arm} for case in cases for arm in ('released', 'fresh')]})
        return path

    def test_installed_boundary_requires_actual_successful_matching_process(self):
        install = {'artifact': str(self.root / 'downloaded.zip'), 'log': str(self.root / 'install.log'),
                   'log_sha256': 'recorded-sha'}
        process = {'worker': False, 'log': install['log'], 'log_sha256': install['log_sha256'],
                   'real_process_launch_observed': True, 'exit_code': 0, 'cwd': str(self.root),
                   'argv': [sys.executable, 'scripts/package_fixture.py', 'smoke', install['artifact']]}
        measured = {'outcome': {'downloaded_install': install}, 'processes': [process]}
        bench.validate_installed_boundary(measured, self.root)
        for field, value in (('exit_code', 1), ('real_process_launch_observed', False),
                             ('cwd', str(self.root / 'another')), ('log_sha256', 'changed'),
                             ('argv', [sys.executable, 'unrelated.py'])):
            with self.subTest(field=field), self.assertRaisesRegex(Halt, 'matching command'):
                bench.validate_installed_boundary({**measured, 'processes': [{**process, field: value}]}, self.root)
        with self.assertRaisesRegex(Halt, 'one recorded command'):
            bench.validate_installed_boundary({**measured, 'processes': [process, process]}, self.root)

    @staticmethod
    def arm(case, baseline):
        handoff = case == 'security-sensitive'
        settings = {'model': 'fixture', 'reasoning_effort': 'high', 'service_tier': 'normal' if baseline else 'fast'}
        return {'scenario': {'source_tree': case, 'task_sha256': case, 'route_sha256': 'route',
                             'configuration_route_sha256': 'exact', 'settings': settings},
                'arm_exit_code': 0, 'outcome': {'observed': 'human-handoff' if handoff else 'done',
                   'candidate_tree': case, 'human_handoffs': int(handoff), 'delivery_seconds': 10 if baseline else 5},
                'tokens': {key: {'known': 50 if baseline else 35, 'unknown_executions': 0}
                           for key in ('input_tokens', 'output_tokens')},
                'processes': [{'worker': True, 'launch_observed': True, 'real_process_launch_observed': True,
                               'requested': settings}]}

    def adjudicate(self, source):
        good = {'critical_expected': 1, 'critical_detected': 1, 'criteria_expected': 2, 'criteria_covered': 2,
                'false_positives': 0, 'human_interventions': 0}
        path = self.root / 'adjudication.json'
        write_json(path, {'reviewer': 'fixture', 'input_sha256': bench.checksum(source),
                          'cases': {case: {'fresh': {'released': good, 'current': good}} for case in bench.CASES}})
        return path

    def test_fake_measurements_and_single_real_pair_cannot_qualify(self):
        for mode, cases in (('fake-adapters', bench.CASES),
                            ('real-Codex-with-fake-GitHub', bench.CASES[:1])):
            with self.subTest(mode=mode):
                source = self.make_input(mode, cases)
                with patch.object(bench, 'arm_metrics', side_effect=lambda r: self.arm(r['case'], r['arm'] == 'released')):
                    report = bench.evaluate(source, adjudication=self.adjudicate(source))['profiles']['fresh']
                self.assertEqual(report['targets']['routine_median_latency']['observed_reduction'], .5)
                self.assertEqual(report['targets']['routine_median_latency']['status'], 'inconclusive')
                self.assertEqual(report['quality'], 'inconclusive')
                self.assertIsNone(report['recommended_model_settings'])
                self.assertFalse(report['qualified'])

    def test_matched_simulated_real_evidence_checks_both_thresholds_without_applying(self):
        source = self.make_input('real-Codex-with-fake-GitHub', kind='model')
        with patch.object(bench, 'arm_metrics', side_effect=lambda r: self.arm(r['case'], r['arm'] == 'released')):
            report = bench.evaluate(source, adjudication=self.adjudicate(source))
        current = report['profiles']['fresh']
        self.assertEqual({v['status'] for v in current['targets'].values()}, {'met'})
        self.assertTrue(current['qualified'])
        self.assertEqual(current['recommended_model_settings']['service_tier'], 'fast')
        self.assertFalse(report['automatic_profile_change'])
        self.assertIsNone(report['money'])

    def test_changed_runtime_is_rejected_before_loading_arms(self):
        source = self.make_input('fake-adapters')
        (self.root / 'released/scripts/runtime.py').write_text('changed')
        with self.assertRaisesRegex(Halt, 'runtime changed'):
            bench.evaluate(source)

    def test_model_latency_objective_does_not_relabel_whole_delivery_targets(self):
        source = self.make_input('real-Codex-with-fake-GitHub', kind='model')
        data = json.loads(source.read_text())
        data['arm_runtimes'] = {'released': 'current', 'fresh': 'current'}
        data['model_objective'] = {'name': 'latency', 'metric': 'routine_median_latency', 'minimum_reduction': .05}
        data['model_environment'] = {'cli_version': 'fixture'}
        plan = self.root / 'plan.json'
        planned = {k: data.get(k) for k in
                   ('runtimes', 'profiles', 'arm_runtimes', 'comparison_kind', 'model_objective', 'model_environment')}
        planned.update(version=1, cases=list(bench.CASES),
                       baseline_settings=self.arm('ordinary-feature', True)['scenario']['settings'],
                       candidate_settings=self.arm('ordinary-feature', False)['scenario']['settings'])
        write_json(plan, planned)
        data['plan'] = bench.reference(plan)
        write_json(source, data)
        def measured(row, candidate_release_route='matched-release-route'):
            baseline = row['arm'] == 'released'
            value = self.arm(row['case'], baseline)
            value['scenario'].update(runtime_root=self.runtime['current']['root'],
                                     runtime_sha256=self.runtime['current']['sha256'],
                                     model_route_sha256='matched-source-route')
            if row['case'] != 'security-sensitive':
                value['scenario']['release_model_route_sha256'] = (
                    candidate_release_route if not baseline and row['case'] == 'ordinary-feature'
                    else 'matched-release-route')
            value['processes'][0]['argv'] = ['codex', 'exec', '--output-schema',
                                            str(Path(self.runtime['current']['root']) / 'schemas/lane-result.json')]
            value['outcome']['delivery_seconds'] = 10 if baseline else 9
            value['tokens'] = {key: {'known': 50, 'unknown_executions': 0}
                               for key in ('input_tokens', 'output_tokens')}
            return value
        with patch.object(bench, 'arm_metrics', side_effect=measured):
            result = bench.evaluate(source, adjudication=self.adjudicate(source))['profiles']['fresh']
        self.assertTrue(result['qualified'])
        self.assertEqual(result['model_objective']['status'], 'met')
        self.assertEqual({v['status'] for v in result['targets'].values()}, {'missed'})
        for route in (None, 'different-release-route'):
            with self.subTest(candidate_release_route=route), patch.object(
                    bench, 'arm_metrics', side_effect=lambda row: measured(row, route)):
                result = bench.evaluate(source, adjudication=self.adjudicate(source))['profiles']['fresh']
                self.assertFalse(result['pairs']['ordinary-feature']['matched'])
                self.assertEqual(result['pairs']['ordinary-feature']['quality'], 'inconclusive')
                self.assertFalse(result['qualified'])
                self.assertIsNone(result['recommended_model_settings'])
        planned['baseline_settings'] = {**planned['baseline_settings'], 'service_tier': 'unexpected'}
        write_json(plan, planned)
        data['plan'] = bench.reference(plan)
        write_json(source, data)
        with patch.object(bench, 'arm_metrics', side_effect=measured), self.assertRaisesRegex(Halt, 'Observed arm model'):
            bench.evaluate(source)
        data['model_objective']['minimum_reduction'] = .001
        write_json(source, data)
        with self.assertRaisesRegex(Halt, 'predeclared gate'):
            bench.evaluate(source)

    def test_model_objective_requires_real_same_runtime_comparison_before_dispatch(self):
        with self.assertRaisesRegex(Halt, 'same-runtime'):
            bench.compare(self.root, self.root / 'unused', model_objective='latency')
        settings = {'model': 'gpt-6.1-sol', 'reasoning_effort': 'high', 'service_tier': 'fast'}
        with self.assertRaisesRegex(Halt, 'Predeclare'):
            bench.compare(self.root, self.root / 'unused', baseline=settings,
                          candidate={**settings, 'service_tier': 'default'}, profile='fresh', model_only=True)

    def test_worker_timeout_validation_precedes_fixture_creation(self):
        output = self.root / 'unused'
        for invalid in (0, 301, True, 120.0, '120', None):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(Halt, 'integer from 1 to 300'):
                bench.compare(self.root, output, worker_timeout_seconds=invalid)
            self.assertFalse(output.exists())

    def test_missing_historical_checkout_explains_how_to_supply_it(self):
        import subprocess

        baseline = self.root / 'baseline-repository'
        baseline.mkdir()
        output = self.root / 'unused'
        with patch.object(bench.subprocess, 'check_output', side_effect=subprocess.CalledProcessError(1, 'git')) as run:
            with self.assertRaisesRegex(Halt, '--baseline-repository'):
                bench.compare(self.root, output, baseline_repository=baseline)
        self.assertEqual(run.call_args.kwargs['cwd'], baseline)
        self.assertFalse(output.exists())

    def test_current_snapshot_preserves_required_worker_schemas(self):
        root = self.root / 'current'
        for folder in bench.RUNTIME_DIRS:
            (root / folder).mkdir(exist_ok=True)
        for name in (*bench.WORKER_SCHEMAS, bench.COMPOSITE_SCHEMA):
            write_json(root / 'schemas' / name, {'type': 'object', 'required': ['fixture']})
        copy = self.root / 'snapshot'
        bench.snapshot_runtime(root, copy)
        self.assertEqual(bench.runtime_digest(root), bench.runtime_digest(copy))
        (copy / 'schemas/lane-result.json').unlink()
        with self.assertRaisesRegex(Halt, 'missing usable worker output schema: lane-result.json'):
            bench.validate_worker_schemas(copy)

    def test_delivery_comparison_stops_on_environment_drift_before_second_arm(self):
        import io
        from types import SimpleNamespace
        from zipfile import ZipFile

        root = self.root / 'current'
        for folder in bench.RUNTIME_DIRS:
            (root / folder).mkdir(exist_ok=True)
        for name in (*bench.WORKER_SCHEMAS, bench.COMPOSITE_SCHEMA):
            write_json(root / 'schemas' / name, {'type': 'object'})
        archive = io.BytesIO()
        with ZipFile(archive, 'w') as zipped:
            for name in bench.WORKER_SCHEMAS:
                zipped.writestr('schemas/' + name, '{"type":"object"}')
        settings = {'model': 'gpt-6.1-sol', 'reasoning_effort': 'high', 'service_tier': 'fast'}
        output = self.root / 'environment-trial'
        with patch.object(bench.subprocess, 'check_output', return_value=archive.getvalue()), \
                patch.object(bench, 'git', return_value='a' * 40), \
                patch.object(bench.subprocess, 'run', return_value=SimpleNamespace(returncode=1)) as child, \
                patch('sfc_lib.profile_qualification.model_environment', side_effect=['before', 'before', 'changed']):
            with self.assertRaisesRegex(Halt, 'environment changed'):
                bench.compare(root, output, cases=['ordinary-feature'], baseline=settings,
                              candidate=settings, profile='fresh')
        self.assertEqual(child.call_count, 1)
        self.assertEqual(child.call_args.kwargs['timeout'], 960)
        progress = json.loads((output / 'comparison-progress.json').read_text())
        self.assertEqual(len(progress['rows']), 1)
        self.assertFalse(progress['complete'])
        self.assertFalse((output / 'qualification-input.json').exists())

    def test_infrastructure_failure_is_not_a_model_quality_regression(self):
        good = self.arm('ordinary-feature', True)
        failed = {'outcome': {'observed': 'failed'}, 'arm_exit_code': 0,
                  'infrastructure_gaps': ['worker output schema unavailable']}
        result = bench.quality_for('ordinary-feature', good, failed, None)
        self.assertEqual(result[0], 'inconclusive')
        self.assertIn('infrastructure', result[1])

    def test_failed_outcome_stops_paid_comparison_even_when_harness_exits_zero(self):
        import io
        from types import SimpleNamespace
        from zipfile import ZipFile

        root = self.root / 'current'
        for folder in bench.RUNTIME_DIRS:
            (root / folder).mkdir(exist_ok=True)
        for name in (*bench.WORKER_SCHEMAS, bench.COMPOSITE_SCHEMA):
            write_json(root / 'schemas' / name, {'type': 'object'})
        archive = io.BytesIO()
        with ZipFile(archive, 'w') as zipped:
            for name in bench.WORKER_SCHEMAS:
                zipped.writestr('schemas/' + name, '{"type":"object"}')
        settings = {'model': 'gpt-6.1-sol', 'reasoning_effort': 'high', 'service_tier': 'fast'}
        def failed(cmd, **kwargs):
            destination = Path(cmd[cmd.index('--output') + 1])
            destination.mkdir(parents=True)
            write_json(destination / 'scenario-outcome.json',
                       {'observed': observed, 'error_type': 'TimedOut' if observed == 'failed' else None})
            return SimpleNamespace(returncode=0)

        for case, observed in [('ordinary-feature', 'failed'), ('ordinary-feature', 'human-handoff'),
                               ('security-sensitive', 'done')]:
            output = self.root / (case + '-' + observed)
            with self.subTest(case=case, observed=observed), \
                patch.object(bench.subprocess, 'check_output', return_value=archive.getvalue()), \
                patch.object(bench, 'git', return_value='a' * 40), \
                patch.object(bench.subprocess, 'run', side_effect=failed) as child, \
                patch('sfc_lib.profile_qualification.model_environment', return_value='unchanged'):
                with self.assertRaisesRegex(Halt, 'no further paid dispatches'):
                    bench.compare(root, output, cases=[case], baseline=settings,
                                  candidate=settings, profile='fresh', worker_timeout_seconds=300)
            self.assertEqual(child.call_count, 1)
            self.assertEqual(child.call_args.kwargs['timeout'], (4 if case == 'security-sensitive' else 7) * 300 + 120)
            progress = json.loads((output / 'comparison-progress.json').read_text())
            stopped = json.loads((output / 'comparison-stop.json').read_text())
            self.assertEqual(len(progress['rows']), 1)
            self.assertFalse(stopped['qualified'])
            self.assertEqual(stopped['outcome'], progress['rows'][0]['outcome'])
            self.assertFalse((output / 'qualification-input.json').exists())


if __name__ == '__main__':
    unittest.main()
