"""Cross-ticket acceptance of immutable executions; only isolated fake projects."""
import copy
import os
import sys
from pathlib import Path
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from sfc_lib import verification as v
from sfc_lib.common import git, Halt, validate_config
from sfc_lib.factory import Factory
import test_lane_evidence as lanes
import test_releases as releases
from test_verification_reuse import contract


class HandoffTests(unittest.TestCase):
    setUp = lanes.LaneEvidenceTests.setUp
    tearDown = lanes.LaneEvidenceTests.tearDown

    def enable(self, **kwargs):
        self.config['verification_reuse'] = [{**contract(self.config['verify_commands'][0]),
            'scope': 'repository', 'context': 'source-only', **kwargs}]
        self.factory = Factory(self.root, self.config, github=self.factory.gh, processes=self.processes)
        self.factory.run_dir = self.root / '.sfc/runs/handoff'

    def verify(self, card=None, base=None):
        card = card or self.card
        self.assertTrue(self.factory.verify(card, self.root, base or self.head))
        receipt = copy.deepcopy(self.factory.record(card)['verification'])
        v.validate_evidence(receipt, self.root, self.factory, card, base or self.head)
        return receipt

    def release_card(self):
        return {**self.card, 'number': 999, 'id': 'release-node', 'item_id': 'release-item',
                'url': 'https://github.com/o/r/issues/999', 'body': 'Different release acceptance criteria'}

    def test_new_ticket_and_config_reuse_original_without_transferring_authority(self):
        self.enable()
        original = self.verify()
        raw = Path(original['receipt_path']).read_bytes()
        self.factory.config_digest = 'different-release-configuration'
        current = self.verify(self.release_card(), base='different-boundary')
        check = current['checks'][0]
        self.assertEqual(check['reuse'], original['checks'][0])
        self.assertEqual(check['acceptance']['base'], 'different-boundary')
        self.assertNotEqual(current['task_digest'], original['task_digest'])
        self.assertEqual(Path(original['receipt_path']).read_bytes(), raw)
        self.assertNotIn('merge', check['acceptance'])

    def test_combined_source_does_not_reuse_individual_pr_tests(self):
        self.enable()
        self.verify()
        (self.root / 'combined.py').write_text('value = 2\n')
        git(self.root, 'add', 'combined.py')
        git(self.root, 'commit', '-m', 'combined change')
        self.assertNotIn('reuse', self.verify(self.release_card())['checks'][0])

    def test_scoped_prose_change_reuses_but_runtime_text_code_and_modes_invalidate(self):
        for name in ('source.py', 'runtime.txt', 'guide.md'):
            (self.root / name).write_text('initial\n')
        git(self.root, 'add', '.')
        git(self.root, 'commit', '-m', 'inputs')
        self.enable(source_inputs=['source.py', 'runtime.txt'])
        original = self.verify()
        (self.root / 'guide.md').write_text('corrected prose\n')
        git(self.root, 'add', 'guide.md')
        git(self.root, 'commit', '-m', 'prose')
        self.assertEqual(self.verify()['checks'][0]['reuse'], original['checks'][0])
        for name in ('runtime.txt', 'source.py'):
            (self.root / name).write_text('behavioral change\n')
            git(self.root, 'add', name)
            git(self.root, 'commit', '-m', 'behavior')
            self.assertNotIn('reuse', self.verify()['checks'][0])
        (self.root / 'source.py').chmod(0o755)
        git(self.root, 'add', 'source.py')
        git(self.root, 'commit', '-m', 'mode')
        self.assertNotIn('reuse', self.verify()['checks'][0])

    def test_documentation_partition_reuses_only_the_unaffected_check(self):
        self.config['verify_commands'] = [v.RUNTIME_SUITE, v.REMAINING_SUITE, 'python3 scripts/validate_plugin.py']
        (self.root / 'docs').mkdir()
        guide = self.root / 'docs/CONFIGURATION.md'
        guide.write_text('Original prose\n')
        git(self.root, 'add', '.')
        git(self.root, 'commit', '-m', 'initial documentation')
        self.enable(documentation_independent=True, fixed_environment={'PATH': os.defpath})
        with patch.object(self.processes, 'execute', return_value=0) as execute:
            # The fake process writes a real log so evidence checks still exercise the filesystem.
            def execute_check(argv, worktree, log, *args, **kwargs):
                Path(log).write_text('passed\n')
                return 0
            execute.side_effect = execute_check
            original = self.verify()
            guide.write_text('Corrected prose\n')
            git(self.root, 'commit', '-am', 'correct prose')
            with patch.dict(os.environ, {'PATH': os.environ['PATH'] + ':/unused-host-tools'}):
                updated = self.verify(self.release_card())
            self.assertEqual(updated['checks'][0]['reuse'], original['checks'][0])
            self.assertNotIn('reuse', updated['checks'][1])
            self.assertNotIn('reuse', updated['checks'][2])
            self.assertEqual(execute.call_count, 5)
            (self.root / 'docs/runtime.json').write_text('{}\n')
            git(self.root, 'add', '.')
            git(self.root, 'commit', '-m', 'add non-prose input')
            self.assertNotIn('reuse', self.verify()['checks'][0])

    def test_tampered_index_receipt_log_and_absent_contract_fall_back(self):
        for kind in ('index', 'receipt', 'log', 'contract'):
            with self.subTest(kind=kind):
                self.tearDown()
                self.setUp()
                self.enable()
                original = self.verify()
                if kind == 'index':
                    for path in v.index_directory(self.root).glob('*.json'):
                        path.write_text('{bad json')
                elif kind == 'receipt':
                    Path(original['receipt_path']).write_text('{}')
                elif kind == 'log':
                    Path(original['checks'][0]['log']).write_text('tampered')
                else:
                    self.factory.c.pop('verification_reuse')
                self.assertNotIn('reuse', self.verify(self.release_card())['checks'][0])

    def test_incomplete_and_failed_verification_never_enter_index(self):
        self.config['verify_commands'] = [self.config['verify_commands'][0], 'exit 1']
        self.enable()
        self.assertFalse(self.factory.verify(self.card, self.root, self.head))
        self.assertFalse(v.index_directory(self.root).exists())

    def test_repository_contract_requires_explicit_source_only_context(self):
        self.enable()
        self.config['verification_reuse'][0].pop('context')
        with self.assertRaises(Halt):
            validate_config(self.config)

    def test_ignored_inputs_and_leading_space_names_are_bound_without_trimming(self):
        (self.root / '.git/info/exclude').write_text('*.json\n')
        (self.root / ' hidden.json').write_text('original')
        (self.root / 'hidden.json').write_text('other')
        self.enable()
        original = self.verify()
        self.assertEqual(set(original['checks'][0]['binding']['untracked_inputs']),
                         {' hidden.json', 'hidden.json'})
        (self.root / ' hidden.json').write_text('changed')
        self.assertNotIn('reuse', self.verify(self.release_card())['checks'][0])

    def test_path_executable_retargeting_invalidates_with_unchanged_path_string(self):
        folder = self.root / '.sfc/tools'
        folder.mkdir(parents=True)
        for name in ('a', 'b'):
            (folder / name).write_text('#!/bin/sh\necho ' + name + '\n')
            (folder / name).chmod(0o755)
        link = folder / 'tool'
        link.symlink_to(folder / 'a')
        self.enable(path_tools=['tool'])
        with patch.dict(os.environ, {'PATH': str(folder) + os.pathsep + os.environ['PATH']}):
            self.verify()
            link.unlink()
            link.symlink_to(folder / 'b')
            self.assertNotIn('reuse', self.verify(self.release_card())['checks'][0])

    def test_new_acceptance_cannot_retroactively_satisfy_historical_merge(self):
        self.enable()
        original = self.verify()
        accepted = self.verify(self.release_card())
        with self.assertRaisesRegex(ValueError, 'acceptance completed after'):
            v.validate_evidence(accepted, self.root, self.factory, self.release_card(), self.head,
                                since=original['time'])


if __name__ == '__main__':
    unittest.main()


class WorkReleaseIntegrationTests(unittest.TestCase):
    setUp = releases.ReleaseTests.setUp
    plan = releases.ReleaseTests.plan
    process = releases.ReleaseTests.process

    def test_normal_release_reuses_combined_source_but_checks_package(self):
        # Test uses ordinary release admission and publication with fake GitHub.
        command = self.c['verify_commands'][0]
        self.c['verification_reuse'] = [{**contract(command), 'scope': 'repository', 'context': 'source-only', 'outputs': ['dist']}]
        self.f = Factory(self.root, self.c, self.gh, self.proc)
        self.f.run_dir = self.root / '.sfc/runs/work'
        self.r = releases.Releases(self.f, self.remote)
        self.remote.f = self.f
        work_card = self.gh.items[1]
        self.assertTrue(self.f.verify(work_card, self.root, self.initial))
        original = copy.deepcopy(self.f.record(work_card)['verification'])
        release_card = self.plan()
        self.process(release_card)
        self.assertEqual(release_card['status'], 'Done')
        calls = [e for e in self.proc.events if e[0] == 'check' and e[3] == command]
        self.assertEqual(len(calls), 1)
        current = self.f.record(release_card)['verification']
        self.assertEqual(current['checks'][0]['reuse'], original['checks'][0])
        self.assertTrue(any(e[3] == self.c['release']['verify_commands'][0] for e in self.proc.events))
        self.assertEqual(self.remote.writes.count('publish'), 1)
