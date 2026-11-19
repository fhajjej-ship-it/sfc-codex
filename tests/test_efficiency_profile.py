import copy
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from sfc_lib import efficiency, fixture_trust, onboarding, profile_qualification, verification
from sfc_lib.common import Halt, exclusive, git, read_json, write_json
from sfc_lib.factory import Factory
import test_lane_evidence as lanes


class ProfileTests(unittest.TestCase):
    setUp = lanes.LaneEvidenceTests.setUp
    tearDown = lanes.LaneEvidenceTests.tearDown

    def layout(self):
        (self.root / '.gitignore').write_text('.sfc/\n__pycache__/\n')
        write_json(self.root / '.codex-plugin/plugin.json', {'name': 'sfc'})
        (self.root / 'scripts').mkdir()
        (self.root / 'scripts/validate_plugin.py').write_text('print("validated")\n')
        (self.root / 'scripts/verify_release_package.py').write_text('print("package")\n')
        (self.root / 'tests').mkdir()
        (self.root / 'tests/test_fixture.py').write_text('import unittest\nclass Check(unittest.TestCase):\n def test_ok(self): self.assertTrue(True)\n')
        git(self.root, 'add', '.')
        git(self.root, 'commit', '-m', 'supported layout')
        self.config['verify_commands'] = list(efficiency.COMMANDS)

    def add_partitioned_runner(self):
        source = Path(__file__).with_name('run_suite.py')
        (self.root / 'tests/run_suite.py').write_bytes(source.read_bytes())

    def test_new_and_migrated_profile_reuses_across_tickets_preserves_choices(self):
        self.layout()
        self.config['models'] = {'review': 'explicit-choice'}
        for migrated in (False, True):
            with self.subTest(migrated=migrated), tempfile.TemporaryDirectory() as library:
                if migrated:
                    self.tearDown()
                    self.setUp()
                    self.layout()
                    self.config['models'] = {'review': 'explicit-choice'}
                Path(library, 'runtime.py').write_text('value = 1\n')
                if migrated:
                    write_json(self.root / '.sfc/config.json', self.config)
                    preview = efficiency.migration(self.root)
                    self.assertFalse(preview['applied'])
                    self.assertTrue(efficiency.migration(self.root, apply=True,
                                    expected_digest=preview['config_digest'],
                                    expected_proposed_digest=preview['proposed_digest'])['applied'])
                    config = efficiency.load_config(self.root)
                    (self.root / '.sfc/config.json').unlink()
                else:
                    config = efficiency.propose(self.root, self.config)['config']
                    efficiency.install_runtimes(config)
                # Keep runtime isolation real; mutate only a fixture dependency.
                for policy in config['verification_reuse']:
                    policy['dependencies'] = [library]
                self.assertEqual(config['models'], self.config['models'])
                self.assertEqual(config['worker_runtime'], {'skill_catalog': False, 'memories': False})
                f = Factory(self.root, config, self.factory.gh, self.processes)
                f.run_dir = self.root / '.sfc/runs/profile'
                head = git(self.root, 'rev-parse', 'HEAD')
                self.assertTrue(f.verify(self.card, self.root, head))
                other = {**self.card, 'number': 100 if migrated else 99, 'id': 'new', 'item_id': 'new', 'body': 'release'}
                self.assertTrue(f.verify(other, self.root, head))
                receipt = f.record(other)['verification']
                self.assertTrue(all(c.get('reuse') for c in receipt['checks']))
                verification.validate_evidence(receipt, self.root, f, other, head)
                self.assertEqual(efficiency.status(config, state=f.state)['latest_ticket_checks'],
                                 {'executed': 2, 'reused': 2, 'failed': 0})
                Path(library, 'runtime.py').write_text('value = 2\n')
                self.assertTrue(f.verify(other, self.root, head))
                self.assertFalse(any(c.get('reuse') for c in f.record(other)['verification']['checks']))

    def test_unknown_layout_command_or_interpreter_keeps_conservative_behavior(self):
        proposal = efficiency.propose(self.root, self.config)
        self.assertEqual(proposal['profile'], 'unsupported')
        self.assertNotIn('verification_reuse', proposal['config'])
        self.assertEqual(proposal['config']['worker_runtime'], {'skill_catalog': False, 'memories': False})
        self.assertNotIn('worker_runtime', efficiency.propose(self.root, self.config, profile='off')['config'])
        self.layout()
        self.config['verify_commands'].append('unknown-tool check')
        proposal = efficiency.propose(self.root, self.config)
        self.assertEqual(len(proposal['config']['verification_reuse']), 2)
        self.assertTrue(any('Unsupported check' in gap for gap in proposal['gaps']))
        with patch.object(efficiency.shutil, 'which', return_value=None):
            self.assertEqual(efficiency.propose(self.root, self.config)['profile'], 'unsupported')

    def test_dependency_manifest_keeps_fresh_checks(self):
        self.layout()
        (self.root / 'requirements-test.txt').write_text('fixture-external-dependency\n')
        result = efficiency.propose(self.root, self.config)
        self.assertEqual(result['profile'], 'unsupported')
        self.assertNotIn('verification_reuse', result['config'])
        self.assertTrue(any('standard-library contract' in reason for reason in result['gaps']))

    def test_parallel_suite_keeps_whole_source_reuse(self):
        self.layout()
        self.config['verify_commands'][0] = efficiency.PARALLEL_SUITE
        proposal = efficiency.propose(self.root, self.config)
        self.assertEqual(len(proposal['config']['verification_reuse']), 1)
        self.add_partitioned_runner()
        proposal = efficiency.propose(self.root, self.config)
        policy = proposal['config']['verification_reuse'][0]
        self.assertEqual(policy['command'], efficiency.PARALLEL_SUITE)
        self.assertEqual(policy['scope'], 'repository')
        self.assertNotIn('source_inputs', policy)

    def test_documentation_profile_is_explicit_and_preserves_full_check_coverage(self):
        self.layout()
        self.config['verify_commands'][0] = efficiency.PARALLEL_SUITE
        with self.assertRaisesRegex(Halt, 'partitioned test runner'):
            efficiency.propose(self.root, self.config, profile='sfc-python-docs')
        self.add_partitioned_runner()
        standard = efficiency.propose(self.root, self.config)['config']
        self.assertEqual(standard['verify_commands'], self.config['verify_commands'])
        split = efficiency.propose(self.root, standard, profile='sfc-python-docs')['config']
        self.assertEqual(tuple(split['verify_commands']), efficiency.DOCUMENTATION_CHECKS)
        self.assertEqual([c.get('documentation_independent', False) for c in split['verification_reuse']],
                         [True, False, False])
        self.assertTrue(all('fixed_environment' in c for c in split['verification_reuse']))
        split['verify_commands'].remove(efficiency.REMAINING_SUITE)
        split['verification_reuse'].pop(1)
        with self.assertRaisesRegex(Halt, 'both SFC test groups'):
            verification.validate(split)

    def test_documentation_migration_rejects_check_ids_bound_to_old_commands(self):
        self.layout()
        self.add_partitioned_runner()
        correction = {'correction_reuse': {'correction_paths': ['docs/guide.md'], 'criteria': {
            '0123456789abcdef': {'source_inputs': ['tests/test_fixture.py'], 'check_ids': [1, 2]}}}}
        behavioral = {'mode': 'risk-based', 'providers': {'review': 'codex-native'},
                      'behavioral_contract': {'paths': ['tests/test_fixture.py'], 'check_ids': [1],
                                              'instructions': {}}}
        for policy in (correction, behavioral):
            with self.subTest(policy=policy):
                self.config['review_policy'] = policy
                efficiency.validate_config(self.config)
                with self.assertRaisesRegex(Halt, 'review contracts.*check IDs'):
                    efficiency.propose(self.root, self.config, profile='sfc-python-docs')

    def test_presplit_commands_require_explicit_documentation_profile(self):
        self.layout()
        self.add_partitioned_runner()
        self.config['verify_commands'] = list(efficiency.DOCUMENTATION_CHECKS)
        for profile in ('auto', 'sfc-python'):
            with self.subTest(profile=profile):
                proposal = efficiency.propose(self.root, self.config, profile=profile)
                self.assertEqual(proposal['profile'], 'sfc-python')
                self.assertEqual(proposal['config']['verify_commands'], self.config['verify_commands'])
                self.assertEqual([p['command'] for p in proposal['config']['verification_reuse']],
                                 [efficiency.COMMANDS[1]])
        standard = efficiency.propose(self.root, self.config)['config']
        proposal = efficiency.propose(self.root, standard, profile='sfc-python-docs')
        split = proposal['config']
        self.assertEqual(proposal['profile'], 'sfc-python-docs')
        policies = split['verification_reuse']
        self.assertEqual([p['command'] for p in policies], list(efficiency.DOCUMENTATION_CHECKS))
        fixed = policies[0]['fixed_environment']
        self.assertTrue(all(p['fixed_environment'] == fixed for p in policies))
        self.assertEqual(efficiency.propose(self.root, split, profile='sfc-python-docs')['changes'], [])

    def test_documentation_profile_rejects_custom_presplit_contracts(self):
        self.layout()
        self.add_partitioned_runner()
        self.config['verify_commands'] = list(efficiency.DOCUMENTATION_CHECKS)
        split = efficiency.propose(self.root, self.config, profile='sfc-python-docs')['config']
        remaining = copy.deepcopy(split['verification_reuse'][1])
        remaining.pop('fixed_environment')
        remaining['source_inputs'] = ['scripts/validate_plugin.py']
        self.config['verification_reuse'] = [remaining]
        efficiency.validate_config(self.config)
        with self.assertRaisesRegex(Halt, 'custom verification contracts'):
            efficiency.propose(self.root, self.config, profile='sfc-python-docs')

    def test_split_suite_contains_every_test_id_once(self):
        from collections import Counter
        from run_suite import selected_groups

        def ids_for(suite):
            return Counter(case for _, cases in selected_groups(suite) for case in cases)

        runtime, remaining = ids_for('runtime'), ids_for('remaining')
        self.assertTrue(runtime and remaining)
        self.assertFalse(runtime.keys() & remaining.keys())
        complete = ids_for('all')
        self.assertEqual(runtime + remaining, complete)
        self.assertTrue(all(count == 1 for count in complete.values()))

    def test_migration_rejects_stale_preview_without_writes(self):
        self.layout()
        write_json(self.root / '.sfc/config.json', self.config)
        with self.assertRaisesRegex(Halt, 'stale'):
            efficiency.migration(self.root, apply=True, expected_digest='not-current')
        self.assertEqual(efficiency.load_config(self.root), self.config)

    def test_existing_contracts_models_and_authority_are_preserved(self):
        self.layout()
        self.config['models'] = {'review': 'my-model'}
        before = copy.deepcopy(self.config)
        proposal = efficiency.propose(self.root, self.config)
        for key in before:
            self.assertEqual(proposal['config'][key], before[key])
        self.assertNotIn('review_policy', proposal['config'])
        self.assertEqual(efficiency.propose(self.root, proposal['config'])['changes'], [])

    def test_supported_profile_preserves_explicit_worker_context_choices(self):
        self.layout()
        for choice in ({}, {'memories': True}, {'skill_catalog': True, 'memories': False}):
            with self.subTest(choice=choice):
                self.config['worker_runtime'] = choice
                proposal = efficiency.propose(self.root, self.config)
                self.assertEqual(proposal['config']['worker_runtime'], choice)
                self.assertFalse(any('Scope worker context' in row for row in proposal['changes']))

    def evidence(self, name, value):
        path = self.root.resolve() / '.sfc' / name
        write_json(path, value)
        return {'path': str(path), 'sha256': verification.checksum(path)}

    def test_review_requires_all_requested_profiles_and_preserves_explicit_policy(self):
        self.layout()
        report = self.evidence('review-evaluation.json', {'execution': 'fake-adapters'})
        policy = {'inspection': {'enabled': ['source', 'release'], 'promotion': report}}
        manifest = self.evidence('review-qualification.json', {'version': 1, 'review_policy': policy})
        args = {'review_qualification': manifest['path']}
        failed = efficiency.propose(self.root, self.config, **args)
        self.assertEqual(failed['qualification']['review']['status'], 'retained')
        self.assertNotIn('review_policy', failed['config'])
        with patch('sfc_lib.review_inspection.qualified', side_effect=(True, False)):
            self.assertNotIn('review_policy', efficiency.propose(self.root, self.config, **args)['config'])
        with patch('sfc_lib.review_inspection.qualified', return_value=True) as gate:
            proposal = efficiency.propose(self.root, self.config, **args)
            self.assertEqual(proposal['config']['review_policy'], policy)
            self.assertEqual(proposal['qualification']['review']['evidence_sha256'], [report['sha256']])
            self.assertNotIn(report['path'], str(proposal['qualification']))
            gate.reset_mock()
            explicit = {**self.config, 'review_policy': {}}
            self.assertEqual(efficiency.propose(self.root, explicit, **args)['config']['review_policy'], {})
            gate.assert_not_called()

    def test_review_migration_pins_manifest_and_reports_stale_evidence(self):
        self.layout()
        write_json(self.root / '.sfc/config.json', self.config)
        report = self.evidence('review-evaluation.json', {'execution': 'fixture'})
        policy = {'inspection': {'enabled': ['source'], 'promotion': report}}
        value = {'version': 1, 'review_policy': policy}
        manifest = self.evidence('review-qualification.json', value)['path']
        args = {'review_qualification': manifest}
        with patch('sfc_lib.review_inspection.qualified', return_value=True):
            preview = efficiency.migration(self.root, **args)
            value['review_policy']['inspection']['promotion'] = {**report, 'path': Path(report['path']).name}
            write_json(manifest, value)
            changed = efficiency.migration(self.root, **args)
            self.assertEqual(preview['proposed_config_digest'], changed['proposed_config_digest'])
            self.assertNotEqual(preview['proposed_digest'], changed['proposed_digest'])
            with exclusive(self.root), self.assertRaisesRegex(Halt, 'stale'):
                efficiency.migration(self.root, apply=True, expected_digest=preview['config_digest'],
                                     expected_proposed_digest=preview['proposed_digest'], **args)
            self.assertEqual(efficiency.load_config(self.root), self.config)
            with exclusive(self.root):
                applied = efficiency.migration(self.root, apply=True, expected_digest=changed['config_digest'],
                                               expected_proposed_digest=changed['proposed_digest'], **args)
            self.assertTrue(applied['applied'])
            saved = efficiency.load_config(self.root)
            self.assertEqual(efficiency.status(saved, root=self.root)['qualification']['review']['status'], 'selected')
            write_json(report['path'], {'changed': True})
            status = efficiency.status(saved, root=self.root)
            self.assertEqual(status['qualification']['review']['status'], 'stale')
            self.assertNotIn(report['path'], str(status))
            self.assertEqual(efficiency.load_config(self.root), saved)

    def test_review_qualification_rechecks_before_saving(self):
        self.layout()
        report = self.evidence('review-evaluation.json', {'execution': 'fixture'})
        policy = {'inspection': {'enabled': ['source'], 'promotion': report}}
        manifest = self.evidence('review-qualification.json', {'version': 1, 'review_policy': policy})
        with patch('sfc_lib.review_inspection.qualified', return_value=True):
            proposal = efficiency.propose(self.root, self.config, review_qualification=manifest['path'])
            efficiency.validate_selected_qualifications(self.root, proposal)
            write_json(report['path'], {'changed': True})
            with self.assertRaisesRegex(Halt, 'Review qualification changed'):
                efficiency.validate_selected_qualifications(self.root, proposal)

    def test_fixture_trust_override_keeps_literal_root_and_requires_exact_scope(self):
        root = self.root / 'fixture.repo with "quotes"'
        override = fixture_trust.fixture_trust_override(root)
        self.assertEqual(override, 'projects={"' + str(root.parent.resolve()) +
                         '/fixture.repo with \\"quotes\\""={trust_level="trusted"}}')
        argv = ['codex', 'exec', '-c', override, '--json', '--ephemeral']
        self.assertTrue(fixture_trust.verify_fixture_trust(argv, root))
        self.assertFalse(fixture_trust.verify_fixture_trust(argv, self.root))
        self.assertFalse(fixture_trust.verify_fixture_trust(['codex', 'exec', '--json', '-c', override], root))
        for extra in (['-c', override], ['--config', 'projects.other.trust_level="trusted"'],
                      ['--config=projects={}'], ['-cprojects={}']):
            self.assertFalse(fixture_trust.verify_fixture_trust([*argv, *extra], root))

    def model_evidence(self):
        self.layout()
        self.config.update(model='gpt-6.1-sol', reasoning_effort='high', service_tier='fast')
        proposed = efficiency.propose(self.root, self.config)['config']
        route = profile_qualification.model_route_binding(proposed, self.root)
        source = self.evidence('model-comparison.json', {})
        adjudication = self.evidence('assessment.json', {})
        manifest = self.evidence('model-qualification.json',
                                 {'version': 1, 'input': source, 'adjudication': adjudication, 'target_route': route})
        baseline = {k: self.config[k] for k in profile_qualification.MODEL_FIELDS}
        environment = {'cli_version': 'fixture-only'}
        report = {'comparison_kind': 'model', 'input': source, 'model_environment': environment,
                  'plan': source, 'model_objective': {'name': 'latency'},
                  'profiles': {'fresh': {'qualified': True, 'configuration_route_sha256s': [route],
                                        'recommended_model_settings': {**baseline, 'service_tier': 'default'},
                                        'pairs': {'ordinary-feature': {
                                            'baseline': {'scenario': {'settings': baseline,
                                                                      'model_route_sha256': route}},
                                            'candidate': {'scenario': {'model_route_sha256': route}},
                                        }}}}}
        return Path(manifest['path']), report

    def test_model_selection_requires_coverage_route_and_provider_and_preserves_explicit_settings(self):
        manifest, report = self.model_evidence()
        args = {'model_qualification': manifest, 'explicit_model_settings': frozenset()}
        with patch('sfc_lib.efficiency_benchmark.evaluate', return_value=report), \
                patch.object(profile_qualification, 'model_environment', return_value=report['model_environment']) as provider:
            selected = efficiency.propose(self.root, self.config, **args)
            self.assertEqual(selected['config']['service_tier'], 'default')
            self.assertEqual(selected['qualification']['model']['status'], 'selected')
            objective = report.pop('model_objective')
            legacy = efficiency.propose(self.root, self.config, **args)
            self.assertEqual(legacy['config']['service_tier'], 'fast')
            self.assertIn('predeclared objective', legacy['qualification']['model']['reason'])
            report['model_objective'] = objective
            explicit = efficiency.propose(self.root, self.config, **{**args, 'explicit_model_settings': {'service_tier'}})
            self.assertEqual(explicit['config']['service_tier'], 'fast')
            self.assertIn('Explicit worker', explicit['qualification']['model']['reason'])
            changed = efficiency.propose(self.root, {**self.config, 'max_attempts': 3}, **args)
            self.assertEqual(changed['config']['service_tier'], 'fast')
            self.assertIn('route differs', changed['qualification']['model']['reason'])
            provider.return_value = {'cli_version': 'changed'}
            unavailable = efficiency.propose(self.root, self.config, **args)
            self.assertEqual(unavailable['config']['service_tier'], 'fast')
            self.assertIn('worker runtime differs', unavailable['qualification']['model']['reason'])
            report['profiles']['fresh']['qualified'] = False
            incomplete = efficiency.propose(self.root, self.config, **args)
            self.assertEqual(incomplete['config']['service_tier'], 'fast')
            self.assertIn('inconclusive', incomplete['qualification']['model']['reason'])

    def test_migration_requires_exact_evidence_preview_and_recorded_implicit_setting(self):
        manifest, report = self.model_evidence()
        write_json(self.root / '.sfc/config.json', self.config)
        with patch('sfc_lib.efficiency_benchmark.evaluate', return_value=report), \
                patch.object(profile_qualification, 'model_environment', return_value=report['model_environment']):
            legacy = efficiency.migration(self.root, model_qualification=manifest)
            self.assertEqual(legacy['proposed_config']['service_tier'], 'fast')
            onboarding.save(self.root, self.config, {}, False, False,
                            efficiency_plan={'implicit_model_settings': ['service_tier']})
            preview = efficiency.migration(self.root, model_qualification=manifest)
            self.assertEqual(preview['proposed_config']['service_tier'], 'default')
            value = read_json(manifest)
            value['input']['path'] = Path(value['input']['path']).name
            write_json(manifest, value)
            with exclusive(self.root), self.assertRaisesRegex(Halt, 'stale'):
                efficiency.migration(self.root, apply=True, model_qualification=manifest,
                                     expected_digest=preview['config_digest'],
                                     expected_proposed_digest=preview['proposed_digest'])
            self.assertEqual(efficiency.load_config(self.root), self.config)
            preview = efficiency.migration(self.root, model_qualification=manifest)
            with exclusive(self.root):
                result = efficiency.migration(self.root, apply=True, model_qualification=manifest,
                                              expected_digest=preview['config_digest'],
                                              expected_proposed_digest=preview['proposed_digest'])
            self.assertTrue(result['applied'])
            saved = efficiency.load_config(self.root)
            self.assertEqual(saved['service_tier'], 'default')
            status = efficiency.status(saved, root=self.root)
            self.assertEqual(status['qualification']['model']['status'], 'selected')
            self.assertNotIn(str(manifest), str(status))
            adjudication = Path(value['adjudication']['path'])
            original = adjudication.read_bytes()
            write_json(adjudication, {'changed': True})
            self.assertEqual(efficiency.status(saved, root=self.root)['qualification']['model']['status'], 'stale')
            adjudication.write_bytes(original)
            (self.root / '.codex').mkdir()
            (self.root / '.codex/config.toml').write_text('service_tier = "default"\n')
            self.assertEqual(efficiency.status(saved, root=self.root)['qualification']['model']['status'], 'stale')
            (self.root / '.codex/config.toml').unlink()
            write_json(manifest, {'version': 1})
            self.assertEqual(efficiency.status(saved, root=self.root)['qualification']['model']['status'], 'stale')

    def test_model_route_keeps_exact_release_commands_and_review_obligations(self):
        self.layout()
        release = {'package_commands': ['one command'], 'artifacts': ['dist/a'], 'version_file': 'VERSION'}
        config = {**self.config, 'release': release, 'review_policy': {'mandatory': ['security']}}
        original = profile_qualification.model_route_binding(config, self.root)
        self.assertNotEqual(original, profile_qualification.model_route_binding(
            {**config, 'release': {**release, 'package_commands': ['another command']}}, self.root))
        self.assertNotEqual(original, profile_qualification.model_route_binding(
            {**config, 'review_policy': {'mandatory': ['qa']}}, self.root))
        self.assertEqual(original, profile_qualification.model_route_binding(
            {**config, 'repo': 'test/another', 'project': {'owner': 'test', 'number': 2}}, self.root))

    def test_model_selection_requires_every_source_and_release_phase_to_match(self):
        config = {**self.config, 'release': {'enabled': True, 'package_commands': ['package']}}
        target = profile_qualification.model_route_binding(config, self.root)
        source = profile_qualification.model_route_binding(self.config, self.root)
        publishing = {'model_route_sha256': source, 'source_model_route_sha256': source,
                      'release_model_route_sha256': target}
        handoff = {**publishing, 'release_model_route_sha256': None}
        pairs = {case: {side: {'scenario': scenario.copy()} for side in ('baseline', 'candidate')}
                 for case, scenario in (('ordinary-feature', publishing), ('bug-fix', publishing),
                                        ('security-sensitive', handoff))}
        profile = {'pairs': pairs, 'configuration_route_sha256s': [target]}
        self.assertTrue(profile_qualification.matches_model_route(profile, config, self.root))
        candidate = pairs['bug-fix']['candidate']['scenario']
        for field, mismatched in (('release_model_route_sha256', None),
                                  ('release_model_route_sha256', 'different-release'),
                                  ('source_model_route_sha256', 'different-source'),
                                  ('model_route_sha256', target)):
            with self.subTest(field=field, mismatched=mismatched):
                candidate[field] = mismatched
                self.assertFalse(profile_qualification.matches_model_route(profile, config, self.root))
                candidate[field] = publishing[field]


if __name__ == '__main__':
    unittest.main()
