"""Corrected-head evidence at normal supervisor entry points, with local fakes."""
from concurrent.futures import ThreadPoolExecutor
import copy
from pathlib import Path
import shlex
import sys
import unittest
from unittest.mock import patch

import test_factory as factory_fixture
from test_review_policy import PolicyProcesses, capabilities
from test_verification_reuse import contract
from sfc_lib import delivery, review_policy
from sfc_lib.common import Halt, git, validate_config
from sfc_lib.factory import Factory
from sfc_lib.verification import checksum


class CorrectionTests(unittest.TestCase):
    tearDown = factory_fixture.IntegrationTests.tearDown
    prepare_review = factory_fixture.IntegrationTests.prepare_review

    def setUp(self):
        factory_fixture.IntegrationTests.setUp(self)
        self.cap = patch('sfc_lib.review_policy.run', side_effect=capabilities)
        self.cap.start()
        self.addCleanup(self.cap.stop)
        for name, text in {'guide.md': 'Old wording\n', 'runtime.txt': 'first\n',
                           'unclassified.txt': 'initial\n'}.items():
            (self.root / name).write_text(text)
        with (self.root / '.gitignore').open('a') as ignored:
            ignored.write('fixture-package.zip\n*.generated\n')
        git(self.root, 'add', '.')
        git(self.root, 'commit', '-m', 'Track documentation and behavior inputs')
        git(self.root, 'push', 'origin', 'main')
        self.dependency = self.root.parent / 'runtime-dependency.txt'
        self.dependency.write_text('first\n')
        self.gh.items[0]['body'] = ('## Acceptance criteria\n- The value is fixed\n'
                                    '- The package contains the updated guide\n')
        self.code_id, self.docs_id = [row['id'] for row in review_policy.criteria(self.gh.items[0])]

        def command(program):
            return shlex.join([sys.executable, '-c', program])

        self.c['verify_commands'] = [
            command("from pathlib import Path; assert Path('value.txt').read_text().startswith('fixed'); "
                    "assert Path('runtime.txt').read_text().strip() in ('first', 'second')"),
            command("import zipfile; z=zipfile.ZipFile('fixture-package.zip','w'); z.write('guide.md'); z.close()"),
            command("import zipfile; from pathlib import Path; "
                    "assert zipfile.ZipFile('fixture-package.zip').read('guide.md') == Path('guide.md').read_bytes()"),
        ]
        self.c['max_attempts'] = 4
        self.c['verification_reuse'] = [{
            **contract(self.c['verify_commands'][0]), 'scope': 'repository', 'context': 'source-only',
            'source_inputs': ['value.txt', 'runtime.txt'], 'dependencies': [str(self.dependency)],
            'outputs': ['fixture-package.zip'],
        }]
        self.c['review_policy'] = {'routine_paths': ['value.txt', 'guide.md', 'runtime.txt'],
            'correction_reuse': {'correction_paths': ['guide.md', 'value.txt', 'runtime.txt'],
                'criteria': {self.code_id: {'source_inputs': ['value.txt', 'runtime.txt'], 'check_ids': [1]}}}}
        self.proc = PolicyProcesses(self.root / '.sfc/stop')
        self.factory = Factory(self.root, self.c, self.gh, self.proc)

    def first_failure(self):
        card, wt = self.prepare_review()
        record = self.factory.record(card)
        original = copy.deepcopy(record['verification'])
        prior_qa = copy.deepcopy(record['receipts']['qa'])
        old = record['head']

        def fail(result):
            if result['scope']['obligation'] == 'review':
                result.update(verdict='fail', findings=[{'file': 'guide.md', 'line': 1,
                    'severity': 'medium', 'message': 'The guide still has old wording.'}])

        self.proc.mutate = fail
        self.factory.review(card, wt)
        record = self.factory.record(card)
        self.assertEqual(card['status'], 'Building')
        self.assertEqual(record['attempts'], 1)
        self.assertEqual(len(record['correction_origin']['pending_findings']), 1)
        self.assertNotIn('receipts', record)
        self.assertNotIn('verification', record)
        self.proc.mutate = lambda result: None
        return card, wt, old, original, prior_qa

    def correct(self, card, wt, extra=None):
        def build(lane, cwd):
            if lane == 'build':
                (cwd / 'guide.md').write_text('Corrected wording\n')
                if extra:
                    extra(cwd)

        self.proc.on_lane = build
        self.assertTrue(self.factory.build(card, wt))
        self.assertTrue(self.factory.qa(card, wt))
        return self.factory.record(card)

    def test_corrected_candidate_retains_only_unaffected_coverage_and_requires_final_resolution(self):
        card, wt, old, original, prior_qa = self.first_failure()
        raw_check = Path(original['checks'][0]['receipt_path']).read_bytes()
        raw_qa = Path(prior_qa['receipt_path']).read_bytes()
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        self.factory.run_dir = self.root / '.sfc/runs/resumed'
        record = self.correct(card, wt)
        head = record['head']
        self.assertNotEqual(head, old)
        checks = record['verification']['checks']
        self.assertEqual(checks[0]['reuse'], original['checks'][0])
        self.assertEqual(checks[0]['acceptance']['head'], head)
        for index in (1, 2):
            self.assertNotIn('reuse', checks[index])
            self.assertEqual(checks[index]['head'], head)
            self.assertNotEqual(checks[index]['id'], original['checks'][index]['id'])
        assessment = record['review_route']['correction']
        self.assertEqual(assessment['head'], head)
        self.assertEqual(assessment['kept'][self.code_id]['receipt_sha256'], prior_qa['receipt_sha256'])
        self.assertEqual(assessment['invalidated'], {self.docs_id: 'no explicit criterion contract'})
        self.assertEqual(Path(original['checks'][0]['receipt_path']).read_bytes(), raw_check)
        self.assertEqual(Path(prior_qa['receipt_path']).read_bytes(), raw_qa)
        self.assertEqual(record['attempts'], 1)
        self.assertEqual(len(record['worker_output_budget']['review']['dispatches']), 1)
        self.assertFalse(self.factory.pinned(card, head, record['base']))

        # An unqualified pass cannot make a pending finding disappear.
        with self.assertRaisesRegex(Halt, 'Malformed'):
            self.factory.review(card, wt)
        self.assertEqual(record['worker_output_budget']['review']['dispatches'][-1]['status'], 'malformed')
        self.assertEqual(card['status'], 'Review')
        self.assertFalse(self.gh.body_updates)

        def pass_final(result):
            self.assertEqual(result['scope']['correction'], assessment['receipt_sha256'])
            result['coverage'][0]['evidence'] = [{'kind': 'retained', 'id': self.code_id,
                                                  'sha256': prior_qa['receipt_sha256']}]
            result['coverage'][1]['evidence'] = [{'kind': 'check', 'id': '3', 'sha256': checksum(checks[2]['log'])}]
            finding = next(iter(assessment['pending_findings']))
            result['evidence'] += f'\nSFC_RESOLVED {finding} Compared corrected wording with installed package.'

        self.proc.mutate = pass_final
        with ThreadPoolExecutor(max_workers=2) as pool:
            calls = [pool.submit(self.factory.lane, card, 'review', wt, record['base']) for _ in range(2)]
            self.assertTrue(all(call.result()['verdict'] == 'pass' for call in calls))
        self.assertEqual(len(record['worker_output_budget']['review']['dispatches']), 3)
        self.assertTrue(self.factory.pinned(card, head, record['base']))
        self.factory.review(card, wt)
        self.assertEqual(record['handoff'], head)
        self.assertEqual(self.gh.merges, [])
        self.assertEqual(delivery.report(self.root, run_id='resumed')['correction_coverage'],
                         {'kept': 1, 'invalidated': 1, 'used_in_passing_final_review': 1})
        Path(prior_qa['result_path']).write_text('{}')
        self.assertFalse(self.factory.pinned(card, head, record['base']))

    def assert_scope_miss(self, extra):
        card, wt, _, original, _ = self.first_failure()
        record = self.correct(card, wt, extra)
        checks = record['verification']['checks']
        self.assertNotIn('reuse', checks[0])
        self.assertNotEqual(checks[0]['id'], original['checks'][0]['id'])
        assessment = record['review_route']['correction']
        self.assertFalse(assessment['kept'])
        self.assertIn(self.code_id, assessment['invalidated'])
        self.assertTrue(assessment['pending_findings'])

    def test_runtime_text_change_reruns_check_and_invalidates_coverage(self):
        self.assert_scope_miss(lambda cwd: (cwd / 'runtime.txt').write_text('second\n'))

    def test_semantic_code_change_reruns_check_and_invalidates_coverage(self):
        self.assert_scope_miss(lambda cwd: (cwd / 'value.txt').write_text('fixed differently\n'))

    def test_dependency_change_reruns_check_and_invalidates_coverage(self):
        self.assert_scope_miss(lambda cwd: self.dependency.write_text('second\n'))

    def test_mode_change_outside_check_inputs_is_a_conservative_miss(self):
        self.assert_scope_miss(lambda cwd: (cwd / 'guide.md').chmod(0o755))

    def test_rename_outside_check_inputs_is_a_conservative_miss(self):
        self.assert_scope_miss(lambda cwd: (cwd / 'unclassified.txt').rename(cwd / 'renamed.txt'))

    def test_symlink_outside_check_inputs_is_a_conservative_miss(self):
        def symlink(cwd):
            (cwd / 'unclassified.txt').unlink()
            (cwd / 'unclassified.txt').symlink_to('guide.md')
        self.assert_scope_miss(symlink)

    def test_new_generated_input_invalidates_even_an_unrelated_prose_correction(self):
        self.assert_scope_miss(lambda cwd: (cwd / 'runtime.generated').write_text('new input\n'))

    def test_task_edits_invalidate_coverage_without_dropping_findings(self):
        card, wt, _, original, _ = self.first_failure()
        self.gh.items[0]['body'] += '- Verify another acceptance obligation\n'
        card = self.gh.cards()[0]
        record = self.correct(card, wt)
        self.assertEqual(record['verification']['checks'][0]['reuse'], original['checks'][0])
        assessment = record['review_route']['correction']
        self.assertFalse(assessment['kept'])
        self.assertIn('task', assessment['invalidated'][self.code_id])
        self.assertTrue(assessment['pending_findings'])

    def test_prior_specialist_gap_requires_fresh_qa_for_the_disputed_criterion(self):
        def dispute(result):
            if result['scope']['obligation'] == 'security':
                result['coverage'][0].update(status='qa_required', reason='Check the runtime interaction again')

        self.proc.mutate = dispute
        card, wt, _, original, prior_qa = self.first_failure()
        record = self.correct(card, wt)
        assessment = record['review_route']['correction']
        self.assertNotIn(self.code_id, assessment['kept'])
        self.assertIn('fresh criterion coverage', assessment['invalidated'][self.code_id])
        request, = assessment['pending_requests']
        self.assertEqual(record['receipts']['qa']['result']['scope']['review_dependency'], request)
        self.assertEqual(record['verification']['checks'][0]['reuse'], original['checks'][0])
        self.assertTrue(review_policy.acceptance_complete(self.factory, card))
        old_qa_ref = {'kind': 'retained', 'id': self.code_id, 'sha256': prior_qa['receipt_sha256']}

        def cite_disputed(result):
            result['coverage'][0]['evidence'] = [old_qa_ref]
            finding, = assessment['pending_findings']
            result['evidence'] += f'\nSFC_RESOLVED {finding} Checked the corrected guide.'

        self.proc.mutate = cite_disputed
        with self.assertRaisesRegex(Halt, 'Malformed'):
            self.factory.review(card, wt)
        self.assertNotIn('review', record['receipts'])

    def test_invalid_path_or_unknown_check_id_cannot_configure_correction_reuse(self):
        for key, value in [('source_inputs', ['../runtime.txt']), ('check_ids', [4]), ('check_ids', [True])]:
            with self.subTest(key=key, value=value):
                config = copy.deepcopy(self.c)
                config['review_policy']['correction_reuse']['criteria'][self.code_id][key] = value
                with self.assertRaisesRegex(Halt, 'criterion'):
                    validate_config(config)


if __name__ == '__main__':
    unittest.main()
