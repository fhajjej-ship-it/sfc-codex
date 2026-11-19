"""Regression coverage for the paired corrected-head benchmark."""
from pathlib import Path
import tempfile
import unittest

import benchmark_corrections as benchmark
from sfc_lib.common import read_json


class CorrectionBenchmarkTests(unittest.TestCase):
    def test_documentation_pair_matches_task_and_packages_but_reuses_only_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'correction'
            report = benchmark.benchmark(output, ['documentation'])
            self.assertEqual(read_json(output / 'report.json'), report)
            arms = report['pairs']['documentation']['arms']
            baseline, selective = arms['whole-source'], arms['selective']
            self.assertEqual(baseline['corrected_tree'], selective['corrected_tree'])
            self.assertEqual(baseline['task_digest'], selective['task_digest'])
            self.assertEqual([c['reused'] for c in baseline['corrected_checks']], [False, False, False])
            self.assertEqual([c['reused'] for c in selective['corrected_checks']], [True, False, False])
            self.assertNotEqual(selective['initial_head'], selective['corrected_head'])
            self.assertEqual(selective['attempts_spent'], 1)
            self.assertEqual(selective['correction_counts'],
                             {'kept': 1, 'invalidated': 1, 'used_in_passing_final_review': 1})
            self.assertEqual(selective['initial_artifact']['installed_text'], 'Old wording\n')
            self.assertEqual(selective['corrected_artifact']['installed_text'], 'Corrected wording\n')
            self.assertIn("Installed command returned: 'fixed\\nOld wording\\n'",
                          Path(selective['initial_checks'][2]['log']['path']).read_text())
            self.assertIn("Installed command returned: 'fixed\\nCorrected wording\\n'",
                          Path(selective['corrected_checks'][2]['log']['path']).read_text())
            self.assertEqual(read_json(selective['original_review']['path'])['result']['verdict'], 'fail')
            self.assertEqual(read_json(selective['final_review']['path'])['result']['verdict'], 'pass')


if __name__ == '__main__':
    unittest.main()
