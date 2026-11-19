"""Offline project comparison preflight and exact normal check contracts."""
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from benchmark_project_review import benchmark, prepare, reference
from benchmark_review import LocalActivity, real_arm
from sfc_lib import efficiency, review_policy
from sfc_lib.common import Halt, common_dir, git, read_json, write_json
from sfc_lib.factory import Factory
from sfc_lib.project_review_benchmark import isolated_config, load_trial, validate_fresh_checks, validate_sample
from sfc_lib.review_benchmark import EXECUTION_LIMITS, live_context, live_receipt
from sfc_lib.verification import index_directory, validate_evidence
from test_factory import BASE_CONFIG
from test_review_policy import PolicyProcesses

MEASURED_LIMITS = {'worker_timeout': 180, 'verify_timeout': 90, 'maximum_model_dispatches': 14}


class ProjectReviewBenchmarkTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        provider = patch('sfc_lib.review_policy.exec_provenance', return_value={'version': 'codex-cli fixture'})
        provider.start()
        self.addCleanup(provider.stop)
        self.folder = Path(temp.name).resolve()
        self.root = self.folder / 'source'
        self.root.mkdir()
        git(self.root, 'init', '-b', 'main')
        git(self.root, 'config', 'user.name', 'Fixture')
        git(self.root, 'config', 'user.email', 'fixture@example.invalid')
        (self.root / '.gitignore').write_text('.sfc/\n__pycache__/\n')
        write_json(self.root / '.codex-plugin/plugin.json', {'name': 'sfc'})
        (self.root / 'scripts').mkdir()
        (self.root / 'scripts/validate_plugin.py').write_text(
            'import json\nassert json.load(open(".codex-plugin/plugin.json"))["name"] == "sfc"\n')
        (self.root / 'tests').mkdir()
        (self.root / 'tests/test_behavior.py').write_text(
            'import unittest\nfrom behavior import VALUE\nclass Behavior(unittest.TestCase):\n'
            '    def test_normal_value(self): self.assertGreater(VALUE, 0)\n')
        self.base = self.commit(1)
        self.cases = []
        for value, kind in ((2, 'retained-real-finding'), (3, 'seeded-regression'), (4, 'clean')):
            git(self.root, 'reset', '--hard', self.base)
            oracle = self.folder / f'oracle-{value}.py'
            oracle.write_text('from behavior import VALUE\nassert VALUE in (1, 4)\n')
            self.cases.append({'case': f'case-{value}', 'kind': kind, 'base': self.base, 'head': self.commit(value),
                               'title': 'Update behavior', 'body': '## Acceptance criteria\n- Value remains positive.',
                               'labels': [], 'expected': [] if kind == 'clean' else ['unexpected-value'],
                               'oracle': reference(oracle)})
        config = {**BASE_CONFIG, 'verify_commands': list(efficiency.COMMANDS), 'model': 'fixture-model',
                  'reasoning_effort': 'high', 'service_tier': 'fast'}
        baseline = efficiency.propose(self.root, config)['config']
        self.assertIn('verification_reuse', baseline)
        write_json(self.root / '.sfc/config.json', baseline)
        self.target = {**baseline, 'review_policy': {'mode': 'risk-based', 'providers': {'review': 'codex-native'},
                       'behavioral_contract': {'paths': ['behavior.py'], 'check_ids': [1], 'instructions': {}}}}
        self.manifest = self.folder / 'manifest.json'
        self.target_file = self.folder / 'target.json'
        self.save_manifest()

    def commit(self, value):
        (self.root / 'behavior.py').write_text(f'VALUE = {value}\n')
        git(self.root, 'add', '.')
        git(self.root, 'commit', '-m', f'Fixture value {value}')
        return git(self.root, 'rev-parse', 'HEAD')

    def save_manifest(self, **fields):
        write_json(self.target_file, self.target)
        write_json(self.manifest, {'version': 1, 'source_root': str(self.root),
                                  'target_config': reference(self.target_file), 'cases': self.cases, **fields})

    def measurement_profile(self, **settings):
        self.target.update(settings)
        write_json(self.root / '.sfc/config.json', {k: v for k, v in self.target.items() if k != 'review_policy'})

    def test_preparation_preserves_source_and_executes_normal_bound_checks_in_isolation(self):
        before = git(self.root, 'worktree', 'list', '--porcelain')
        original_config = (self.root / '.sfc/config.json').read_bytes()
        target, plan, ref, samples = prepare(self.manifest, self.folder / 'output')
        self.assertEqual(target, self.target)
        self.assertEqual(load_trial(ref, target), plan)
        self.assertEqual(plan['version'], 1)
        self.assertEqual(plan['limits'], {**EXECUTION_LIMITS, 'maximum_model_dispatches': 24})
        self.assertLess(target['worker_timeout'], plan['limits']['worker_timeout'])
        self.assertEqual(git(self.root, 'worktree', 'list', '--porcelain'), before)
        self.assertEqual((self.root / '.sfc/config.json').read_bytes(), original_config)
        sample = samples[-1]
        root = Path(sample['worktree'])
        config = isolated_config(target, common_dir(self.root), common_dir(root))
        self.assertEqual(read_json(self.folder / 'output/repository/.sfc/config.json'), config)
        self.assertEqual(git(self.folder / 'output/repository', 'rev-parse', '--is-bare-repository'), 'false')
        guard = read_json(self.folder / 'output/guard-preflight.json')
        self.assertTrue(guard['read_allowed'] and guard['protected_push_denied'])
        self.assertEqual(config['verify_commands'], target['verify_commands'])
        self.assertEqual(Path(config['verification_reuse'][0]['runtime']['location']).parent.parent, common_dir(root))
        factory = Factory(root, config, github=object())
        factory.activity = LocalActivity()
        factory.run_dir = self.folder / 'run'
        self.assertTrue(factory.verify(sample['card'], root, sample['base']))
        validate_fresh_checks(config, factory.record(sample['card'])['verification'])
        Path(sample['project_oracle']['oracle']['path']).write_text('assert True\n')
        with self.assertRaisesRegex(ValueError, 'evidence changed'):
            validate_sample(sample, plan)

    def test_explicit_limits_bound_processes_and_raw_arm_record(self):
        self.measurement_profile(worker_timeout=200, verify_timeout=900)
        self.save_manifest(measurement_limits=MEASURED_LIMITS)
        target, plan, ref, samples = prepare(self.manifest, self.folder / 'output')
        self.assertEqual(plan['version'], 2)
        self.assertEqual(plan['manifest'], reference(self.manifest))
        self.assertEqual(plan['limits'], MEASURED_LIMITS)
        self.assertEqual(load_trial(ref, target), plan)
        sample = samples[-1]
        root = Path(sample['worktree'])
        settings = review_policy.model_settings(target)
        report = {'target_config': target, 'target_policy': target['review_policy'], 'model_settings': settings}
        calls = []

        class RecordingProcesses(PolicyProcesses):
            def execute(self, argv, cwd, log, timeout, *args, **kwargs):
                calls.append((argv[0] == 'codex', timeout))
                return super().execute(argv, cwd, log, timeout, *args, **kwargs)

        def factory(root, config, *, github):
            return Factory(root, config, github=github, processes=RecordingProcesses(root / '.sfc/stop'))

        provider = {'provider': 'codex-exec', 'version': 'codex-cli fixture', 'tool': '/fixture/codex',
                    'model': target['model']}
        with patch('sfc_lib.review_policy.exec_provenance', return_value=provider):
            value = real_arm(root, self.folder / 'arm', sample['card'], sample['base'], 'baseline',
                             settings, report['target_policy'], factory_type=factory, target_config=target,
                             project_plan=plan)
        self.assertEqual(calls, [(False, 90)] * 2 + [(True, 180)] * 3)
        self.assertEqual(value['config']['worker_timeout'], 200)
        self.assertEqual(value['execution_limits'], MEASURED_LIMITS)
        live_context(sample, value, 'baseline', report, plan)
        raw_path = Path(value['timing']['path'])
        raw = read_json(raw_path)
        self.assertEqual(raw['execution_limits'], MEASURED_LIMITS)
        for changed in (EXECUTION_LIMITS, {**MEASURED_LIMITS, 'worker_timeout': 179}):
            write_json(raw_path, {**raw, 'execution_limits': changed})
            with self.subTest(changed=changed), self.assertRaisesRegex(ValueError, 'timing or evidence changed'):
                live_context(sample, {**value, 'execution_limits': changed, 'timing': reference(raw_path)},
                             'baseline', report, plan)

    def test_explicit_limits_outside_budget_fail_before_preparation_or_dispatch(self):
        self.measurement_profile(worker_timeout=175, verify_timeout=85)
        output = self.folder / 'output'
        valid = {**MEASURED_LIMITS, 'worker_timeout': 175, 'verify_timeout': 85}
        for limits in (None, {}, {**valid, 'worker_timeout': 176}, {**valid, 'verify_timeout': 86},
                       {**valid, 'worker_timeout': 181}, {**valid, 'verify_timeout': True},
                       {**valid, 'maximum_model_dispatches': 25}, {**valid, 'maximum_model_dispatches': 17}):
            self.save_manifest(measurement_limits=limits)
            with self.subTest(limits=limits), patch('benchmark_project_review.real_arm') as dispatch:
                with self.assertRaisesRegex(ValueError, 'measurement limits'):
                    benchmark(self.manifest, output)
                self.assertFalse(output.exists())
                dispatch.assert_not_called()

    def test_explicit_plan_rejects_mismatched_missing_or_changed_measurement_declaration(self):
        self.measurement_profile(worker_timeout=200, verify_timeout=900)
        self.save_manifest(measurement_limits=MEASURED_LIMITS)
        target, plan, ref, _ = prepare(self.manifest, self.folder / 'output')
        altered = self.folder / 'alternate-plan.json'
        write_json(altered, {**plan, 'limits': {**MEASURED_LIMITS, 'worker_timeout': 179}})
        with self.assertRaisesRegex(ValueError, 'declared measurement manifest'):
            load_trial(reference(altered), target)
        write_json(altered, {k: v for k, v in plan.items() if k != 'manifest'})
        with self.assertRaisesRegex(ValueError, 'exact target config'):
            load_trial(reference(altered), target)
        self.save_manifest()
        with self.assertRaisesRegex(ValueError, 'evidence changed'):
            load_trial(ref, target)
        write_json(altered, {**plan, 'manifest': reference(self.manifest)})
        with self.assertRaisesRegex(ValueError, 'declared measurement manifest'):
            load_trial(reference(altered), target)

    def test_explicit_model_reservations_stop_at_declared_cap(self):
        self.measurement_profile(worker_timeout=200, verify_timeout=900)
        self.save_manifest(measurement_limits={**MEASURED_LIMITS, 'maximum_model_dispatches': 4})
        output = self.folder / 'output'
        dispatched = []

        def measure(root, folder, card, base, variant, *args, before_dispatch, **kwargs):
            before_dispatch(variant, 'qa')
            dispatched.append(card['number'])
            return {'outcome': 'fixture-only'}

        with patch('benchmark_project_review.real_arm', side_effect=measure):
            result = benchmark(self.manifest, output)
        self.assertFalse(result['promote'])
        self.assertEqual(dispatched, [1, 1, 2, 2])
        self.assertEqual(len((output / 'model-reservations.jsonl').read_text().splitlines()), 4)
        self.assertIn('finite model dispatch cap',
                      read_json(output / 'arms/3/baseline/comparison-failure.json')['detail'])

    def test_paired_run_retains_each_index_and_continues_all_arms(self):
        output = self.folder / 'output'
        arms = []

        def measure(root, folder, card, base, variant, *args, **kwargs):
            arms.append((card['number'], variant))
            self.assertFalse(index_directory(root).exists())
            index_directory(root).mkdir()
            folder.mkdir(parents=True)
            return {'outcome': 'fixture-only'}

        with patch('benchmark_project_review.real_arm', side_effect=measure):
            result = benchmark(self.manifest, output)
        self.assertFalse(result['promote'])
        self.assertEqual(arms, [(1, 'baseline'), (1, 'candidate'), (2, 'candidate'), (2, 'baseline'),
                                (3, 'baseline'), (3, 'candidate')])
        self.assertEqual(len(list((output / 'arms').glob('*/*/verification-index'))), 6)

    def test_target_command_or_missing_source_pin_fails_before_output_or_dispatch(self):
        self.target['verify_commands'] = ['true', self.target['verify_commands'][1]]
        self.target['verification_reuse'] = self.target['verification_reuse'][1:]
        self.save_manifest()
        output = self.folder / 'output'
        with patch('benchmark_project_review.real_arm') as dispatch:
            with self.assertRaisesRegex(ValueError, 'saved project config'):
                benchmark(self.manifest, output)
            self.assertFalse(output.exists())
            self.target = {**read_json(self.root / '.sfc/config.json'), 'review_policy': self.target['review_policy']}
            self.cases[0]['head'] = '0' * 40
            self.save_manifest()
            with self.assertRaisesRegex(Halt, 'git failed'):
                benchmark(self.manifest, output)
            self.assertFalse(output.exists())
            dispatch.assert_not_called()

    def test_oracle_that_does_not_reproduce_regression_fails_before_any_dispatch(self):
        path = Path(self.cases[1]['oracle']['path'])
        path.write_text('assert True\n')
        self.cases[1]['oracle'] = reference(path)
        self.save_manifest()
        with patch('benchmark_project_review.real_arm') as dispatch:
            with self.assertRaisesRegex(ValueError, 'did not reproduce'):
                benchmark(self.manifest, self.folder / 'output')
            dispatch.assert_not_called()

    def test_historical_review_replays_bound_checks_after_ticket_freshness_expires(self):
        target, _, _, samples = prepare(self.manifest, self.folder / 'comparison')
        sample = samples[-1]
        root = Path(sample['worktree'])
        config = isolated_config({**target, 'review_policy': {}}, common_dir(self.root), common_dir(root))
        card, base = sample['card'], sample['base']
        factory = Factory(root, config, github=object(), processes=PolicyProcesses(root / '.sfc/stop'))
        factory.activity = LocalActivity()
        factory.run_dir = self.folder / 'historical-check'
        self.assertTrue(factory.verify(card, root, base))
        evidence = factory.record(card)['verification']
        validate_fresh_checks(config, evidence)
        provider = {'provider': 'codex-exec', 'version': 'codex-cli fixture', 'tool': '/fixture/codex',
                    'model': config['model']}
        with patch('sfc_lib.review_policy.exec_provenance', return_value=provider):
            factory.lane(card, 'qa', root, base)
        receipt = factory.record(card)['receipts']['qa']

        class Later(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.now(tz) + timedelta(days=2)

        with patch('sfc_lib.verification.dt.datetime', Later):
            with self.assertRaisesRegex(ValueError, 'bindings changed or expired'):
                validate_evidence(evidence, root, factory, card, base)
            live_receipt(factory, card, root, receipt, {'cli_version': provider['version']}, set())
            changed = {**receipt, 'time': datetime.now().astimezone().isoformat()}
            with self.assertRaisesRegex(ValueError, 'receipt reference changed'):
                live_receipt(factory, card, root, changed, {'cli_version': provider['version']}, set())
