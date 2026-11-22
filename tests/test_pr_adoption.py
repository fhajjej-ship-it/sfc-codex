"""Explicit PR adoption against temporary Git repositories and fake workers."""
import copy
from pathlib import Path
import unittest
from unittest.mock import patch

import test_factory as fixtures
from test_pickup import PickupGitHub, ticket
from sfc_lib.common import Halt, git
from sfc_lib.factory import Factory
from sfc_lib.pr_adoption import recover


class PRAdoptionTests(unittest.TestCase):
    setUp = fixtures.IntegrationTests.setUp
    tearDown = fixtures.IntegrationTests.tearDown

    def setup_pr(self, *, rejected_checks=False):
        self.c['max_attempts'] = 1
        self.gh = PickupGitHub(self.root, [ticket()])
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        original = self.proc.execute

        def check_failure(argv, cwd, log, *args, **kwargs):
            if argv[0] == 'codex' or not rejected_checks:
                return original(argv, cwd, log, *args, **kwargs)
            Path(log).write_text('Supervisor check failed\n')
            return 1

        with patch.object(self.proc, 'execute', side_effect=check_failure):
            self.factory.run(include_backlog=True, issues=(1,))
        self.old = copy.deepcopy(self.factory.state['cards']['1'])
        self.assertEqual(self.old['status'], 'Blocked')
        self.factory.dir.joinpath('stop').touch()
        self.candidate = self.advance(self.old['head'])

    def advance(self, parent):
        tree = git(self.root, 'rev-parse', parent + '^{tree}')
        candidate = git(self.root, 'commit-tree', tree, '-p', parent, '-m', 'Explicit candidate update')
        git(self.root, 'push', 'origin', f"{candidate}:refs/heads/{self.old['branch']}")
        self.gh.external_head = candidate
        return candidate

    def recover(self, *, apply=False):
        return recover(self.factory, issue=1, pr_number=self.old['pr'], candidate=self.candidate,
                       request_id='adopt-fixture', reason='Explicit fixture update', apply=apply)

    def test_adopts_failed_check_candidate_and_runs_all_gates_without_a_build(self):
        self.setup_pr(rejected_checks=True)
        before = self.factory.state_path.read_bytes()
        self.assertEqual(self.recover()['state'], 'preview')
        self.assertEqual(self.factory.state_path.read_bytes(), before)
        self.assertTrue(self.factory.dir.joinpath('stop').exists())
        self.assertEqual(git(self.old['worktree'], 'rev-parse', 'HEAD'), self.old['head'])
        self.assertEqual(self.recover(apply=True)['state'], 'awaiting-manual-merge')
        record = self.factory.state['cards']['1']
        self.assertEqual(record['head'], self.candidate)
        self.assertEqual(record['handoff'], self.candidate)
        self.assertEqual(record['attempts'], self.old['attempts'])
        self.assertEqual(record['worker_output_budget']['build'], self.old['worker_output_budget']['build'])
        self.assertEqual(self.proc.lanes, ['build', 'qa', 'security', 'review'])
        self.assertTrue(record['verification']['passed'])
        self.assertTrue(all(check.get('reuse') is None for check in record['verification']['checks']))
        self.assertEqual(record['status'], 'Blocked')
        self.assertFalse(self.gh.prs[101]['isDraft'])
        self.assertEqual(self.gh.merges, [])
        with self.assertRaisesRegex(Halt, 'already has a recovery receipt'):
            self.recover(apply=True)

    def test_preserves_prior_review_and_rejects_head_movement_before_handoff(self):
        self.setup_pr()
        original = self.proc.on_lane

        def move_again(lane, cwd):
            if original:
                original(lane, cwd)
            if lane == 'qa':
                self.advance(self.candidate)

        self.proc.on_lane = move_again
        with self.assertRaisesRegex(Halt, 'PR state, source, head or target changed'):
            self.recover(apply=True)
        record = self.factory.state['cards']['1']
        attempt = record['pr_adoptions'][-1]
        self.assertEqual(attempt['phase'], 'failed')
        self.assertEqual(attempt['previous_evidence']['reviewed_completion'], self.old['reviewed_completion'])
        self.assertEqual(record['status'], 'Blocked')
        self.assertNotIn('handoff', record)
        self.assertEqual(self.proc.lanes[-1], 'qa')
        self.assertEqual(self.gh.merges, [])

    def test_rejects_non_descendant_before_any_execution(self):
        self.setup_pr(rejected_checks=True)
        base = self.old['base']
        tree = git(self.root, 'rev-parse', base + '^{tree}')
        other = git(self.root, 'commit-tree', tree, '-p', base, '-m', 'Rewritten PR')
        git(self.root, 'push', '--force-with-lease', 'origin', f"{other}:refs/heads/{self.old['branch']}")
        self.candidate = self.gh.external_head = other
        with self.assertRaisesRegex(Halt, 'must descend'):
            self.recover()
        self.assertEqual(self.proc.lanes, ['build'])
        self.assertNotIn('pr_adoptions', self.factory.state['cards']['1'])

    def test_candidate_debit_survives_interrupted_journal_write(self):
        self.setup_pr(rejected_checks=True)
        with patch('sfc_lib.pr_adoption.write_json', side_effect=OSError('Interrupted journal write')):
            with self.assertRaisesRegex(OSError, 'Interrupted journal write'):
                self.recover(apply=True)
        restarted = Factory(self.root, self.c, self.gh, self.proc)
        with self.assertRaisesRegex(Halt, 'already has a recovery receipt'):
            recover(restarted, issue=1, pr_number=self.old['pr'], candidate=self.candidate,
                    request_id='different-id', reason='Do not refund the debit', apply=True)
        self.assertEqual(self.proc.lanes, ['build'])
        self.assertEqual(git(self.old['worktree'], 'rev-parse', 'HEAD'), self.old['head'])
