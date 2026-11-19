"""Release source reuse through a real temporary merge and fake publication."""
import json
from pathlib import Path
from threading import Barrier
import unittest
from unittest.mock import patch

import test_factory as fixtures
from test_local_merge import AtomicGitHub
from test_releases import Remote, config
from test_review_policy import PolicyProcesses, capabilities
from sfc_lib import review_policy
from sfc_lib.common import Halt, git, write_json
from sfc_lib.factory import Factory
from sfc_lib.releases import Releases


class ReleaseRemote(Remote):
    def changes(self, commits):
        return {
            'commits': [{'commit': oid, 'pull_requests': [101]} for oid in commits],
            'pull_requests': [{'number': 101, 'merge_commit': commits[-1], 'issues': [1],
                               'title': 'Correct the fixture value', 'public': True}],
            'issues': [1], 'unlinked_commits': [],
        }


class WorkAndReleaseProcesses(PolicyProcesses):
    def __init__(self, stop):
        super().__init__(stop)
        self.release_prompts = []

    def execute(self, argv, cwd, log, timeout, prompt=None, **kwargs):
        if prompt and 'supervisor-validated RELEASE workflow' in prompt:
            self.release_prompts.append(prompt)
            return fixtures.FakeProcesses.execute(self, argv, cwd, log, timeout, prompt, **kwargs)
        if prompt and 'Exact scope (echo unchanged): ' not in prompt:
            return fixtures.FakeProcesses.execute(self, argv, cwd, log, timeout, prompt, **kwargs)
        return super().execute(argv, cwd, log, timeout, prompt, **kwargs)


class ReleaseSourceCoverageTests(unittest.TestCase):
    tearDown = fixtures.IntegrationTests.tearDown

    def setUp(self, *, behavioral=False, release_after_merge=False, baseline=False):
        fixtures.IntegrationTests.setUp(self)
        self.c = config()
        self.c.update(merge_policy='auto', merge_mode='local-verification',
                      review_policy={'mode': 'baseline', 'routine_paths': ['value.txt']})
        if baseline:
            del self.c['review_policy']
            self.c['release']['authority']['mode'] = 'draft'
        if behavioral:
            self.c['review_policy'] = {'mode': 'risk-based', 'providers': {'review': 'codex-native'},
                'behavioral_contract': {'paths': ['value.txt'], 'check_ids': [1], 'instructions': {}}}
            for mock in (patch.object(review_policy, 'promoted', return_value=(True, 'test promotion')),
                         patch.object(review_policy, 'native_sessions', return_value=self.root / '.sfc/sessions')):
                mock.start()
                self.addCleanup(mock.stop)
        self.proc = WorkAndReleaseProcesses(self.root / '.sfc/stop')
        self.gh = AtomicGitHub(self.root, self.remote)
        if release_after_merge:
            release_settings = self.c.pop('release')
            write_json(self.root / '.sfc/config.json', self.c)
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        if behavioral:
            def cite_prior_qa(report):
                if self.proc.lanes[-1] == 'review':
                    qa = self.factory.record(self.gh.items[0])['receipts']['qa']
                    for row in report['coverage']:
                        row['evidence'] = [{'kind': 'prior-qa', 'id': row['criterion'], 'sha256': qa['receipt_sha256']}]
            self.proc.mutate = cite_prior_qa
        cap = patch('sfc_lib.review_policy.run', side_effect=capabilities)
        cap.start()
        self.addCleanup(cap.stop)
        self.boundary = git(self.remote, 'rev-parse', 'main')
        self.factory.run(allow_merge=True)
        record = self.factory.record(self.gh.items[0])
        self.completion = record['local_merge']
        self.assertEqual(self.completion['phase'], 'done')
        if release_after_merge:
            self.c = {**self.c, 'release': release_settings}
            write_json(self.root / '.sfc/config.json', self.c)
            self.factory = Factory(self.root, self.c, self.gh, self.proc)
        if baseline:
            release = self.c['release']
            authority = {**release['authority'], 'mode': 'publish'}
            notes = {key: authority[key] for key in ('repo', 'base_branch', 'host', 'destination')}
            notes.update(authorization_id='approved-after-merge',
                         entries=[{'commit': self.completion['head'], 'text': 'Correct the fixture value'}])
            self.c = {**self.c, 'release': {**release, 'authority': authority, 'approved_summaries': notes}}
            write_json(self.root / '.sfc/config.json', self.c)
            self.factory = Factory(self.root, self.c, self.gh, self.proc)
            self.factory.run_dir = self.root / '.sfc/runs/source-handoff-release'
            self.factory.run_dir.mkdir()
        self.release = ReleaseRemote(self.factory)
        self.release.tags['0.9.0'] = self.boundary
        self.release.rows['0.9.0'] = {'id': 1, 'name': '0.9.0', 'tag_name': '0.9.0', 'draft': False,
                                     'target_commitish': self.boundary, 'prerelease': False,
                                     'published_at': '2026-01-01T00:00:00Z', 'assets': []}
        self.release.latest_tag = '0.9.0'
        self.workflow = Releases(self.factory, self.release)

    def plan(self):
        def create(gh, root, **kwargs):
            gh.items.append(fixtures.issue(2, title=kwargs['title'], body=kwargs['body'], status='Backlog'))
            return {'issue': {'number': 2}}

        with patch('sfc_lib.priority.create_ticket', side_effect=create):
            self.workflow.request('1.0.0')
        self.gh.items[-1]['status'] = 'Ready'
        return self.release.card(2)

    def work_logs(self):
        gate = Path(self.completion['verification']['checks'][0]['log'])
        lane = Path(self.completion['receipts']['review']['verification']['checks'][0]['log'])
        self.assertNotEqual(gate, lane)
        return gate, lane

    def test_exact_source_handoff_reuses_work_proof_but_runs_all_release_lanes(self):
        card = self.plan()
        self.workflow.process(card)
        self.assertEqual(card['status'], 'Done')
        coverage = self.workflow.journal['source_coverage']
        self.assertEqual(coverage['head'], self.completion['head'])
        self.assertEqual(coverage['base'], self.boundary)
        self.assertEqual(set(coverage['lanes']), {'qa', 'security', 'review'})
        self.assertEqual(len(self.proc.release_prompts), 3)
        for lane in ('qa', 'security', 'review'):
            prompt = next(prompt for prompt in self.proc.release_prompts if f'an SFC {lane} worker' in prompt)
            self.assertIn(f'an SFC {lane} worker', prompt)
            self.assertIn('original independent work-ticket receipts', prompt)
            self.assertIn('"artifact_checks":', prompt)
            self.assertIn('"source_coverage":', prompt)
        self.assertTrue(any('Release QA owns package and installation behavior' in p for p in self.proc.release_prompts[:2]))
        self.assertTrue(any('Release security owns risks' in p for p in self.proc.release_prompts[:2]))
        self.assertIn('Final release review owns the actual source range', self.proc.release_prompts[2])
        qa_prompt = next(prompt for prompt in self.proc.release_prompts if 'an SFC qa worker' in prompt)
        packet = json.loads(qa_prompt.split('Supervisor-validated release evidence:\n')[1].splitlines()[0])
        summary = packet['source_coverage']
        self.assertEqual({key: value for key, value in summary.items() if key != 'lanes'},
                         {key: value for key, value in coverage.items() if key != 'lanes'})
        for lane, proof in coverage['lanes'].items():
            self.assertEqual(summary['lanes'][lane], {
                **{key: proof[key] for key in ('receipt', 'sha256', 'provider')},
                'verdict': 'pass',
                'covered_criteria': [row['criterion'] for row in proof['result']['coverage']
                                     if row['status'] == 'covered'],
            })
            self.assertTrue(Path(proof['receipt']).is_file())
            self.assertNotIn(proof['result']['summary'], qa_prompt)
        self.assertIn('Open the original source reports or project assertions when', qa_prompt)
        self.assertNotIn('This release has no validated source coverage', qa_prompt)
        self.assertEqual(self.release.writes.count('publish'), 1)
        changed = {**self.c, 'release': {**self.c['release'], 'preflight_commands': ['true']}}
        other = Factory(self.root, changed, self.gh, self.proc)
        self.assertIsNone(review_policy.release_source_coverage(other, self.workflow.journal))

    def test_changed_completion_check_log_requires_fresh_release_source_review(self):
        gate, _ = self.work_logs()
        gate.write_text('changed original final gate log')
        card = self.plan()
        self.workflow.process(card)
        self.assertEqual(card['status'], 'Done')
        self.assertNotIn('source_coverage', self.workflow.journal)
        self.assertTrue(all('original independent work-ticket receipts' not in prompt
                            for prompt in self.proc.release_prompts))
        qa_prompt = self.proc.release_prompts[0]
        self.assertIn('This release has no validated source coverage', qa_prompt)
        self.assertIn('Independently inspect the changed implementation and relevant project-check', qa_prompt)

    def test_changed_review_check_log_after_freeze_prevents_publication(self):
        gate, lane = self.work_logs()
        before = gate.read_bytes()
        card = self.plan()

        def damage_check(lane_name, cwd):
            if lane_name == 'review':
                self.assertIn('source_coverage', self.workflow.journal)
                lane.write_text('changed original check consumed by the work reviewer')

        self.proc.on_lane = damage_check
        with self.assertRaisesRegex(Halt, 'Pinned release source review evidence changed'):
            self.workflow.process(card)
        self.assertEqual(gate.read_bytes(), before)
        self.assertNotIn('publish', self.release.writes)


class BehavioralReleaseSourceCoverageTests(unittest.TestCase):
    tearDown = ReleaseSourceCoverageTests.tearDown
    plan = ReleaseSourceCoverageTests.plan

    def setUp(self):
        ReleaseSourceCoverageTests.setUp(self, behavioral=True, release_after_merge=True)

    def test_release_enabled_after_merge_revalidates_and_carries_prior_qa_chain(self):
        source_digest = self.completion['config_digest']
        self.assertNotEqual(source_digest, self.factory.config_digest)
        self.plan()
        with patch('sfc_lib.releases.ReleaseGitHub', return_value=self.release):
            self.factory.run(allow_merge=True)
        card = self.release.card(2)
        self.workflow.load(card)
        self.assertEqual(card['status'], 'Done')
        journal = self.workflow.journal
        coverage = journal['source_coverage']
        self.assertEqual(coverage['prior_qa'], self.completion['receipts']['review']['prior_qa'])
        self.assertEqual(set(coverage['lanes']), {'qa', 'review'})
        for lane, proof in coverage['lanes'].items():
            self.assertEqual(proof['sha256'], self.completion['receipts'][lane]['receipt_sha256'])
            self.assertEqual(proof['result']['scope']['config'], source_digest)
        self.assertEqual(journal['config_digest'], self.factory.config_digest)
        self.assertEqual(self.proc.lanes[:3], ['build', 'qa', 'review'])
        self.assertCountEqual(self.proc.lanes[3:5], ['qa', 'security'])
        self.assertEqual(self.proc.lanes[-1], 'review')
        self.assertEqual(self.release.writes.count('publish'), 1)
        self.assertEqual(review_policy.release_source_coverage(self.factory, journal), coverage)
        other = Factory(self.root, {**self.c, 'worker_timeout': self.c['worker_timeout'] + 1}, self.gh, self.proc)
        self.assertIsNone(review_policy.release_source_coverage(other, journal))
        Path(self.completion['receipts']['qa']['log']).write_text('changed prior QA log')
        self.assertIsNone(review_policy.release_source_coverage(self.factory, journal))


class BaselineReleaseSourceCoverageTests(unittest.TestCase):
    tearDown = ReleaseSourceCoverageTests.tearDown
    plan = ReleaseSourceCoverageTests.plan

    def setUp(self):
        ReleaseSourceCoverageTests.setUp(self, baseline=True)

    def test_default_route_can_carry_exact_reviews_after_release_approval(self):
        self.assertNotEqual(self.completion['config_digest'], self.factory.config_digest)
        card = self.plan()
        both_running = Barrier(2, timeout=5)

        def inspect(lane, cwd):
            if lane in ('qa', 'security'):
                both_running.wait()
            else:
                receipts = self.factory.record(card)['receipts']
                self.assertEqual([receipts[name]['result']['verdict'] for name in ('qa', 'security')],
                                 ['pass', 'pass'])

        self.proc.on_lane = inspect
        self.workflow.process(card)
        coverage = self.workflow.journal['source_coverage']
        self.assertEqual(coverage['head'], self.completion['head'])
        self.assertEqual(set(coverage['lanes']), {'qa', 'security', 'review'})
        self.assertEqual(len(self.proc.release_prompts), 3)
        self.assertTrue(all('original independent work-ticket receipts' in prompt for prompt in self.proc.release_prompts))
        self.assertEqual(card['status'], 'Done')
        other = Factory(self.root, {**self.c, 'worker_timeout': self.c['worker_timeout'] + 1}, self.gh, self.proc)
        self.assertIsNone(review_policy.release_source_coverage(other, self.workflow.journal))

    def test_modified_baseline_review_log_is_not_carried(self):
        Path(self.completion['receipts']['review']['log']).write_text('changed after merge')
        card = self.plan()
        self.workflow.process(card)
        self.assertNotIn('source_coverage', self.workflow.journal)
        self.assertIn('This release has no validated source coverage', self.proc.release_prompts[0])


if __name__ == '__main__':
    unittest.main()
