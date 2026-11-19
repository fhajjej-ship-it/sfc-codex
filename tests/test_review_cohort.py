import copy
from contextlib import ExitStack
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from package_plugin import contents
from sfc_lib import efficiency, profile_qualification, review_cohort, review_inspection, review_policy
from sfc_lib.common import git, load_config, write_json
from sfc_lib.factory import Factory
from sfc_lib.review_benchmark import CRITERIA, INSPECTION_CRITERIA
from sfc_lib.verification import checksum, digest
import test_efficiency_profile as profiles


class ReviewCohortTests(unittest.TestCase):
    setUp = profiles.ProfileTests.setUp
    tearDown = profiles.ProfileTests.tearDown

    def prepare(self):
        profiles.ProfileTests.layout(self)
        module = self.root / review_cohort.MODULE
        module.parent.mkdir(parents=True)
        module.write_text('def lint_card(card):\n    return []\n')
        (self.root / 'AGENTS.md').write_text('Verify the project checks.\n')
        git(self.root, 'add', '.')
        git(self.root, 'commit', '-m', 'upstream source')
        self.upstream = git(self.root, 'rev-parse', 'HEAD')
        self.config.update(model='test-model', reasoning_effort='high', service_tier='fast')
        stack = ExitStack()
        self.addCleanup(stack.close)
        bundle = stack.enter_context(tempfile.TemporaryDirectory())
        self.bundle = Path(bundle).resolve()
        self.path = self.bundle / review_cohort.CERTIFICATE
        stack.enter_context(patch.object(review_cohort, 'PACKAGE', self.bundle))
        cli = {'version': 'codex-cli test-fixture', 'sha256': 'a' * 64, 'provider_sha256': 'f' * 64}
        stack.enter_context(patch.object(review_cohort, 'cli_identity', return_value=cli))
        self.runtime = stack.enter_context(patch.object(review_policy, 'runtime_binding', return_value='b' * 64))
        normal = efficiency.propose(self.root, self.config)['config']
        self.policy = {'mode': 'risk-based', 'providers': {'review': 'codex-native'}, 'behavioral_contract': {
            'paths': [review_cohort.MODULE], 'check_ids': [1],
            'instructions': {'AGENTS.md': checksum(self.root / 'AGENTS.md')}}}
        self.certificate = {
            'version': 1, 'evaluation': 'paired-real-model-focused-upstream', 'upstream_base': self.upstream,
            'policy': self.policy, 'profile_sha256': review_cohort.profile_binding(self.root, normal),
            'runtime_sha256': self.runtime.return_value, 'cli': cli, 'report_sha256': 'c' * 64,
            'adjudication_sha256': 'd' * 64, 'full_activation_sha256': 'e' * 64,
            'criteria_sha256': digest(CRITERIA),
            'metrics': {'retained_cases': 1, 'seeded_cases': 1, 'clean_cases': 2,
                        'detection_recall': 1, 'acceptance_coverage': 1, 'baseline_false_positives': 0,
                        'candidate_false_positives': 0, 'median_review_ratio': 0.8,
                        'median_end_to_end_ratio': 0.8, 'max_clean_end_to_end_ratio': 0.8},
        }
        write_json(self.path, self.certificate)

    def test_normal_profile_selects_bundled_evidence_for_setup_and_migration(self):
        self.prepare()
        fresh = efficiency.propose(self.root, self.config)
        self.assertEqual(fresh['qualification']['review']['status'], 'selected')
        self.assertEqual(fresh['config']['review_policy'], {**self.policy, 'cohort': checksum(self.path)})
        self.assertNotIn('inspection', fresh['config']['review_policy'])
        efficiency.validate_selected_qualifications(self.root, fresh)
        self.assertEqual(contents(self.bundle)[review_cohort.CERTIFICATE], self.path.read_bytes())
        write_json(self.root / '.sfc/config.json', self.config)
        preview = efficiency.migration(self.root)
        self.assertEqual(preview['qualification']['review']['status'], 'selected')
        self.assertTrue(efficiency.migration(self.root, apply=True, expected_digest=preview['config_digest'],
                                             expected_proposed_digest=preview['proposed_digest'])['applied'])
        self.assertEqual(load_config(self.root), fresh['config'])
        self.assertEqual(efficiency.status(fresh['config'], root=self.root)['qualification']['review']['status'],
                         'selected')

    def test_explicit_policy_and_mismatched_qualification_keep_baseline(self):
        self.prepare()
        explicit = {**self.config, 'review_policy': {'mode': 'baseline'}}
        self.assertEqual(efficiency.propose(self.root, explicit)['config']['review_policy'], explicit['review_policy'])
        for change in ({'model': 'other-model'}, {'worker_runtime': {}},
                       {'verify_commands': list(reversed(efficiency.COMMANDS))}):
            with self.subTest(change=change):
                self.assertNotIn('review_policy', efficiency.propose(self.root, {**self.config, **change})['config'])
        for module, key in ((review_policy, 'runtime_binding'), (review_cohort, 'cli_identity')):
            with patch.object(module, key, return_value='drift'):
                self.assertNotIn('review_policy', efficiency.propose(self.root, self.config)['config'])
        different_provider = {**self.certificate['cli'], 'provider_sha256': '0' * 64}
        with patch.object(review_cohort, 'cli_identity', return_value=different_provider):
            self.assertNotIn('review_policy', efficiency.propose(self.root, self.config)['config'])
        invalid = copy.deepcopy(self.certificate)
        invalid['metrics']['detection_recall'] = 0.9
        write_json(self.path, invalid)
        self.assertNotIn('review_policy', efficiency.propose(self.root, self.config)['config'])

    def test_revalidates_certificate_and_upstream_scope_per_candidate(self):
        self.prepare()
        proposal = efficiency.propose(self.root, self.config)
        config = proposal['config']
        factory = Factory(self.root, config, self.factory.gh, self.processes)
        module = self.root / review_cohort.MODULE
        module.write_text('def lint_card(card):\n    return list()\n')
        git(self.root, 'commit', '-am', 'first scoped change')
        base = git(self.root, 'rev-parse', 'HEAD')
        module.write_text('def lint_card(card):\n    return [str(card)]\n')
        git(self.root, 'commit', '-am', 'second scoped change')
        route = review_policy.plan(factory, self.card, self.root, base)
        self.assertEqual(route['obligations'], ['qa', 'review'])
        self.assertEqual(route['base'], base)
        self.assertEqual(route['head'], git(self.root, 'rev-parse', 'HEAD'))
        self.assertEqual(route['task_digest'], factory.task_digest(self.card))
        sensitive = {**self.card, 'labels': ['security']}
        self.assertIn('security', review_policy.plan(factory, sensitive, self.root, base)['obligations'])
        with self.path.open('ab') as stream:
            stream.write(b'\n')
        self.assertFalse(review_policy.promoted(factory, wt=self.root, base=base)[0])
        self.assertFalse(profile_qualification.review_setup_available(
            proposal['qualification_manifests']['review'], proposal['qualification']['review'], self.root, config)[0])
        write_json(self.path, self.certificate)
        (self.root / 'tests/test_fixture.py').write_text('# changed test contract\n')
        git(self.root, 'commit', '-am', 'unqualified source drift')
        self.assertFalse(review_policy.promoted(factory, wt=self.root, base=git(self.root, 'rev-parse', 'HEAD'))[0])

    def test_provider_binding_requires_the_qualified_route_and_effective_settings(self):
        expected = {'model': 'test-model', 'model_reasoning_effort': 'high', 'service_tier': 'fast'}
        auth = {'authMethod': 'chatgpt', 'requiresOpenaiAuth': True}
        effective = {**expected, 'chatgpt_base_url': 'https://chatgpt.com/backend-api/'}
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(review_cohort.sha256(review_cohort.provider_binding(effective, auth, expected)))
            for override in ({'model_provider': 'custom'}, {'openai_base_url': 'https://example.invalid/v1'},
                             {'chatgpt_base_url': 'https://example.invalid/api'}, {'model': 'other-model'},
                             {'service_tier': 'default'}):
                with self.subTest(override=override), self.assertRaisesRegex(ValueError, 'provider route'):
                    review_cohort.provider_binding({**effective, **override}, auth, expected)
            with self.assertRaisesRegex(ValueError, 'provider route'):
                review_cohort.provider_binding(effective, {**auth, 'authMethod': 'apikey'}, expected)
            with patch.dict(os.environ, {'OPENAI_BASE_URL': 'https://example.invalid/v1'}), \
                    self.assertRaisesRegex(ValueError, 'provider route'):
                review_cohort.provider_binding(effective, auth, expected)

    def test_bundled_inspection_qualifies_each_profile_against_its_controls(self):
        self.prepare()
        release = {'enabled': True, 'adapter': 'github-releases', 'package_commands': ['package-fixture'],
                   'verify_commands': ['verify-fixture'], 'artifacts': ['dist/test.zip']}
        config = {**self.config, 'release': release}
        self.certificate['inspection'] = {
            profile: {
                'criteria_sha256': digest(INSPECTION_CRITERIA), 'report_sha256': '1' * 64,
                'adjudication_sha256': '2' * 64, 'controls_sha256': review_cohort.inspection_controls(config, profile),
                'metrics': {'case_counts': {kind: 1 for kind in INSPECTION_CRITERIA['required_kinds']},
                            'minimum_clean_check_seconds': 6, 'median_end_to_end_ratio': 0.8,
                            'max_clean_end_to_end_ratio': 0.9, 'detection_recall': 1, 'acceptance_coverage': 1,
                            'baseline_false_positives': 0, 'candidate_false_positives': 0},
            } for profile in ('source', 'release')}
        write_json(self.path, self.certificate)
        fresh = efficiency.propose(self.root, self.config)
        self.assertEqual(fresh['qualification']['review']['inspection'], {
            'source': 'selected', 'release': 'pending release configuration'})
        self.assertFalse(review_inspection.qualified(Factory(self.root, fresh['config'], self.factory.gh), 'release'))

        selected = efficiency.propose(self.root, config)
        chosen = selected['config']
        self.assertEqual(chosen['review_policy']['inspection']['enabled'], ['source', 'release'])
        efficiency.validate_selected_qualifications(self.root, selected)
        self.assertTrue(review_inspection.qualified(Factory(self.root, chosen, self.factory.gh), 'release',
                                                    wt=self.root, revision=self.upstream))
        write_json(self.root / '.sfc/config.json', config)
        preview = efficiency.migration(self.root)
        self.assertTrue(efficiency.migration(self.root, apply=True, expected_digest=preview['config_digest'],
                                             expected_proposed_digest=preview['proposed_digest'])['applied'])
        self.assertEqual(load_config(self.root), chosen)
        explicit = {**chosen, 'review_policy': {**chosen['review_policy'], 'inspection': {'enabled': []}}}
        self.assertEqual(efficiency.propose(self.root, explicit)['config']['review_policy'], explicit['review_policy'])
        changed = {**chosen, 'release': {**release, 'verify_commands': ['other-check']}}
        factory = Factory(self.root, changed, self.factory.gh)
        self.assertTrue(review_inspection.qualified(factory, 'source', wt=self.root, revision=self.upstream))
        self.assertFalse(review_inspection.qualified(factory, 'release', wt=self.root, revision=self.upstream))

        self.certificate['inspection']['release']['metrics']['max_clean_end_to_end_ratio'] = 1.1
        write_json(self.path, self.certificate)
        fallback = efficiency.propose(self.root, config)
        self.assertEqual(fallback['config']['review_policy']['inspection']['enabled'], ['source'])
        self.assertEqual(fallback['qualification']['review']['status'], 'selected')
