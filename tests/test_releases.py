"""Release durability regressions use the real scheduler and restart path.

Reservation recovery must cover interruptions before and after each factory
counter and release-journal persistence step at max_attempts=1. Preserve the
original debit and verify no duplicate packaging, paid lanes, upload or Done.
Reject counter, identity, scope and manual-stage drift; never infer a new
reservation from ambiguous legacy state. Retain the QA/Review transition cases.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from sfc_lib.common import Halt, Stopped, git, read_json, run, validate_config, write_json, common_dir
from sfc_lib import delivery
from sfc_lib.factory import Factory
from sfc_lib.releases import Releases, artifact_allowlist, checksum, digest
from sfc_lib.release_github import ReleaseGitHub
from sfc_lib.release_recovery import recover
from test_factory import BASE_CONFIG, FakeGitHub, FakeProcesses, issue


def config():
    return {**copy.deepcopy(BASE_CONFIG), 'release': {
        'enabled': True, 'adapter': 'github-releases',
        'package_commands': ["mkdir -p dist && cp value.txt dist/app.bin"],
        'verify_commands': ["cmp dist/app.bin value.txt"], 'artifacts': ['dist/app.bin'],
        'authority': {'authorization_id': 'explicit-user-choice', 'repo': 'example/app',
                      'base_branch': 'main', 'host': 'github.com', 'destination': 'github-releases', 'mode': 'publish'}}}


class Remote:
    def __init__(self, f):
        self.f = f
        self.tags = {}
        self.values = {}
        self.rows = {}
        self.assets = {}
        self.writes = []
        self.fail = None
        self.corrupt = False
        self.latest_tag = None
        self.field = {'id': 'target', 'name': 'Target release', 'dataType': 'TEXT'}
        self.factory_view = {'id': 'view', 'name': 'Factory', 'layout': 'BOARD_LAYOUT', 'filter': 'label:mine',
                             'configuration': {'visibleFields': {'nodes': [{'id': 'existing'}]}},
                             'verticalGroupByFields': {'nodes': [{'id': 'status'}]}}

    def identity(self):
        return {'repo': 'example/app', 'repository_id': 'repo-id', 'host': 'github.com',
                'project': 'p', 'base_branch': 'main', 'destination': 'github-releases'}

    def tag(self, version):
        return self.tags.get(version)

    def tag_sources(self, versions):
        return {version: self.tag(version) for version in versions}

    def pinned_card(self, identity):
        return self.card(identity['number'])

    def releases(self):
        return copy.deepcopy(list(self.rows.values()))

    def release_issues(self, version, request_id):
        marker = '<!-- sfc:ticket:' + request_id + ' -->'
        return [card['number'] for card in self.f.gh.items
                if marker in card['body'] or (card['title'] == 'Release ' + version and card['state'] == 'CLOSED')]

    def checks(self, candidate):
        return [{'status': 'COMPLETED', 'conclusion': 'SUCCESS'}]

    def release(self, version):
        return copy.deepcopy(self.rows.get(version))

    def release_by_id(self, release_id):
        version = next(key for key, value in self.rows.items() if value['id'] == release_id)
        return self.release(version)

    def latest(self, rows=None):
        if self.latest_tag is None:
            return None
        return {'prerelease': False, **copy.deepcopy(self.rows[self.latest_tag])}

    def changes(self, commits):
        return {'commits': [{'commit': c, 'pull_requests': [3] if n else []} for n, c in enumerate(commits)],
                'pull_requests': [{'number': 3, 'merge_commit': commits[-1], 'issues': [2],
                                   'title': 'feat: Review documents with linked evidence', 'public': True}] if len(commits) > 1 else [],
                'issues': [2] if len(commits) > 1 else [], 'unlinked_commits': commits[:1]}

    def mutate(self, key):
        self.writes.append(key)
        if self.fail == 'before-' + key:
            raise Halt('lost before')

    def finish(self, key):
        if self.fail == key:
            raise Halt('lost response')

    def create_tag(self, version, sha):
        self.mutate('tag')
        self.tags[version] = sha
        self.finish('tag')

    def create_draft(self, tag, sha, notes):
        self.mutate('draft')
        self.rows[tag] = {'id': len(self.rows) + 1, 'name': tag, 'tag_name': tag, 'target_commitish': sha,
                          'body': notes, 'draft': True, 'prerelease': '-' in tag, 'published_at': None, 'assets': []}
        self.finish('draft')
        return copy.deepcopy(self.rows[tag])

    def upload(self, release_id, name, path):
        self.mutate('upload-' + name)
        row = next(r for r in self.rows.values() if r['id'] == release_id)
        data = Path(path).read_bytes()
        asset_id = len(self.assets) + 1
        self.assets[asset_id] = data
        row['assets'].append({'id': asset_id, 'name': name, 'state': 'uploaded'})
        self.finish('upload-' + name)

    def download_to(self, asset, path):
        Path(path).write_bytes(b'wrong' if self.corrupt else self.assets[asset['id']])

    def publish(self, release_id, make_latest):
        self.mutate('publish')
        row = next(r for r in self.rows.values() if r['id'] == release_id)
        row.update(draft=False, published_at='2026-11-09T12:00:00Z')
        if make_latest == 'true':
            self.latest_tag = row['tag_name']
        self.finish('publish')

    def edit_description(self, release_id, text):
        self.mutate('description')
        next(r for r in self.rows.values() if r['id'] == release_id)['body'] = text
        self.finish('description')

    def fields(self):
        return copy.deepcopy(self.field)

    def create_field(self):
        self.field = {'id': 'target', 'name': 'Target release', 'dataType': 'TEXT'}

    def view(self):
        return copy.deepcopy(self.factory_view)

    def visible(self, view_id, fields):
        assert view_id == self.factory_view['id']
        self.factory_view['configuration']['visibleFields']['nodes'] = [{'id': f} for f in fields]

    def item(self, card, field):
        return {'identity': {k: card[k] for k in ('id', 'number', 'url', 'repository')},
                'value': self.values.get(card['number'])}

    def assign(self, card, field, version):
        self.values[card['number']] = version

    def filtered(self, version):
        return [{'id': f'item-{n}'} for n, v in self.values.items() if v == version]

    def card(self, number):
        return copy.deepcopy(next(c for c in self.f.gh.items if c['number'] == number))

    def close(self, card):
        self.mutate('close')
        next(c for c in self.f.gh.items if c['number'] == card['number'])['state'] = 'CLOSED'
        self.finish('close')


class ReleaseInspectionProcesses(FakeProcesses):
    def __init__(self, stop):
        super().__init__(stop)
        self.inspection_started = threading.Event()
        self.check_started = threading.Event()
        self.adverse = False
        self.assessment_barrier = None
        self.security_finding = False
        self.parallel_observer = None

    def execute(self, argv, cwd, log, timeout, prompt=None, **kwargs):
        lane = Path(log).parent.name.split('-')[0]
        if self.assessment_barrier is not None and lane in ('security', 'review'):
            self.assessment_barrier.wait(5)
            if self.parallel_observer:
                self.parallel_observer(lane, kwargs['observer'])
        if prompt and 'independent provisional inspector' in prompt:
            self.lanes.append('inspection')
            self.inspection_started.set()
            if not self.check_started.wait(5):
                raise AssertionError('Release checks did not overlap package inspection')
            result = {'verdict': 'pass', 'summary': 'Inspected package', 'findings': [],
                      'evidence': 'Compared the frozen package payload with the exact release source.'}
            if self.adverse:
                result.update(verdict='fail', findings=[{'file': 'value.txt', 'line': 1, 'severity': 'high',
                                                        'message': 'The tested payload omits a required value.'}])
        elif '--output-schema' in argv and argv[argv.index('--output-schema') + 1].endswith('release-review-result.json'):
            self.lanes.append('review')
            evidence = prompt.split('supervisor artifact/install/smoke checks:\n', 1)[1].splitlines()[0]
            descriptor = json.loads(evidence)['provisional_qa']
            result = {'verdict': 'pass', 'summary': 'Reviewed verified package', 'findings': [],
                      'evidence': 'Reviewed actual project and artifact verification logs.',
                      'release_qa': {**{k: descriptor[k] for k in ('inspection_sha256', 'verification_bindings_sha256')},
                                     'artifact_evidence': 'Compared frozen app.bin bytes with the verified artifact.',
                                     'installation_evidence': 'Checked the installed payload readback in artifact log.',
                                     'acceptance_evidence': 'Matched the complete included range to the release evidence.'}}
        elif self.security_finding and lane == 'security':
            self.lanes.append('security')
            result = {'verdict': 'fail', 'summary': 'Unsafe public metadata',
                      'findings': [{'file': 'value.txt', 'line': 1, 'severity': 'high',
                                    'message': 'The fixture exposes private release metadata.'}],
                      'evidence': 'Detected the fixture defect while final review was in flight.'}
        else:
            return super().execute(argv, cwd, log, timeout, prompt, **kwargs)
        write_json(argv[argv.index('--output-last-message') + 1], result)
        events = [{'type': 'thread.started', 'thread_id': str(uuid4())}, {'type': 'turn.started'},
                  {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': json.dumps(result)}},
                  {'type': 'turn.completed', 'usage': {'input_tokens': 10, 'output_tokens': 4}}]
        Path(log).write_text('\n'.join(json.dumps(event) for event in events) + '\n')
        return 0


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'app'
        self.root.mkdir()
        git(self.root, 'init', '-b', 'main')
        git(self.root, 'config', 'user.email', 'fixture@example.test')
        git(self.root, 'config', 'user.name', 'Fixture')
        (self.root / 'value.txt').write_text('initial\n')
        git(self.root, 'add', '.')
        git(self.root, 'commit', '-m', 'Initial unlinked commit')
        self.initial = git(self.root, 'rev-parse', 'HEAD')
        git(self.root, 'remote', 'add', 'origin', str(self.root))
        self.c = config()
        self.c['release']['approved_summaries'] = {
            'authorization_id': 'approved-initial-prose', 'repo': 'example/app', 'base_branch': 'main',
            'host': 'github.com', 'destination': 'github-releases',
            'entries': [{'commit': self.initial, 'text': 'Provide the initial document reader'}]}
        self.gh = FakeGitHub(self.root, [issue(1, title='Release 1.0.0', status='Ready'),
                                       issue(2, status='Done', state='CLOSED')])
        self.proc = FakeProcesses(self.root / '.sfc/stop')
        self.f = Factory(self.root, self.c, self.gh, self.proc)
        self.f.run_dir = self.root / '.sfc/runs/test'
        self.f.run_dir.mkdir(parents=True)
        self.remote = Remote(self.f)
        self.r = Releases(self.f, self.remote)

    def commit(self):
        (self.root / 'value.txt').write_text('new bytes\n')
        git(self.root, 'add', 'value.txt')
        git(self.root, 'commit', '-m', 'Merged change')
        return git(self.root, 'rev-parse', 'HEAD')

    def plan(self):
        boundary, candidate, changes = self.r.source()
        card = self.remote.card(1)
        self.r.path = self.r.directory / '1.0.0.json'
        self.r.journal = {'workflow_kind': 'release', 'version': '1.0.0', 'candidate': candidate,
                          'boundary': boundary, 'changes': changes, 'identity': self.remote.identity(),
                          'config_digest': self.f.config_digest, 'state': 'planned', 'effects': {},
                          'card': {k: card[k] for k in ('number', 'id', 'url', 'item_id', 'repository')},
                          'task_digest': self.f.task_digest(card)}
        self.r.save()
        self.f.update(card, workflow_kind='release', release_version='1.0.0')
        return card

    def process(self, card):
        with patch('sfc_lib.releases.ReleaseGitHub', return_value=self.remote):
            self.r.process(card)

    def test_versioned_artifacts_use_the_frozen_version_during_preparation_and_readback(self):
        self.c['release'].update(
            artifacts=['dist/app-{version}.zip', 'dist/app-{version}.zip.sha256', 'dist/notice.txt'],
            package_commands=[
                'mkdir -p dist && cp value.txt dist/app-1.2.3-rc.1.zip && '
                'shasum -a 256 dist/app-1.2.3-rc.1.zip > dist/app-1.2.3-rc.1.zip.sha256 && '
                'cp value.txt dist/notice.txt'
            ])
        self.f = Factory(self.root, self.c, self.gh, self.proc)
        self.f.run_dir = self.root / '.sfc/runs/test'
        self.remote.f = self.f
        self.r = Releases(self.f, self.remote)

        def create(gh, root, **kwargs):
            self.gh.items[0].update(title=kwargs['title'], body=kwargs['body'], status='Backlog')
            return {'issue': {'number': 1}}

        with patch('sfc_lib.priority.create_ticket', side_effect=create):
            self.r.request('v1.2.3-rc.1')
        card = self.remote.card(1)
        self.commit()
        with patch.object(self.r, 'verify_prepared'):
            area = self.r.prepare(card)
        self.assertEqual(git(area, 'rev-parse', 'HEAD'), self.initial)
        self.assertEqual([row['name'] for row in self.r.journal['artifacts']],
                         ['app-1.2.3-rc.1.zip', 'app-1.2.3-rc.1.zip.sha256', 'notice.txt'])

        resumed = Releases(self.f, self.remote)
        resumed.load(card)
        resumed.validate_artifacts()
        (area / 'dist/app-1.2.3-rc.1.zip.sha256').write_text('changed')
        with self.assertRaisesRegex(Halt, 'Tested candidate artifact bytes changed'):
            resumed.validate_artifacts()

    def test_artifact_templates_keep_exact_allowlist_rules(self):
        for paths in (
            ['dist/app-{other}.zip'],
            ['dist/app-{version!r}.zip'],
            ['dist/app-{version}*.zip'],
            ['dist/../app-{version}.zip'],
            ['/dist/app-{version}.zip'],
            ['dist/app-{version}.zip', 'other/app-{version}.zip'],
            ['dist/app-{version}.zip', 'dist/sfc-release-manifest.json'],
        ):
            with self.subTest(paths=paths), self.assertRaisesRegex(Halt, 'release.artifacts'):
                artifact_allowlist(paths)

        self.assertEqual(artifact_allowlist(['dist/app-{version}.zip'], '2.0.0'), ['dist/app-2.0.0.zip'])
        with self.assertRaisesRegex(Halt, 'Invalid frozen release version'):
            artifact_allowlist(['dist/app-{version}.zip'], '../../invalid')

    def test_expanded_artifact_collision_stops_before_creating_a_release_card(self):
        self.c['release']['artifacts'] = ['dist/app-{version}.zip', 'other/app-1.0.0.zip']
        self.f = Factory(self.root, self.c, self.gh, self.proc)
        self.r = Releases(self.f, self.remote)
        with patch('sfc_lib.priority.create_ticket') as create, self.assertRaisesRegex(Halt, 'release.artifacts'):
            self.r.request('v1.0.0')
        create.assert_not_called()
        self.assertFalse(self.r.directory.exists())
        self.assertEqual(self.remote.writes, [])

    def overlap_plan(self, *, project_check=None, representative=True, preflight=False):
        self.c['max_workers'] = 2
        self.c['review_policy'] = {'inspection': {'enabled': ['release']}}
        self.c['release']['verify_commands'] = ['mkdir installed && cp dist/app.bin installed/app.bin && '
                                                'cmp installed/app.bin value.txt']
        if preflight:
            self.c['release']['preflight_commands'] = ['cmp dist/app.bin value.txt']
        if project_check is not None:
            self.c['verify_commands'] = [project_check]
        self.proc = ReleaseInspectionProcesses(self.root / '.sfc/stop')
        self.f = Factory(self.root, self.c, self.gh, self.proc)
        self.f.run_dir = self.root / '.sfc/runs/test'
        self.remote = Remote(self.f)
        self.r = Releases(self.f, self.remote)
        verify = self.f.verify

        def concurrent_check(*args):
            if representative:
                self.assertTrue(self.proc.inspection_started.wait(5))
                self.proc.check_started.set()
            return verify(*args)

        mocks = [patch('sfc_lib.review_inspection.qualified', return_value=True),
                 patch('sfc_lib.review_policy.exec_provenance', side_effect=lambda model: {
                     'provider': 'codex-exec', 'version': 'fake fixture', 'tool': 'fixture', 'model': model}),
                 patch.object(self.f, 'verify', side_effect=concurrent_check)]
        if representative:
            mocks.append(patch('sfc_lib.review_inspection.representative_history', return_value=True))
        for mock in mocks:
            mock.start()
            self.addCleanup(mock.stop)
        return self.plan()

    def test_release_package_inspection_completes_only_with_separate_final_and_security(self):
        card = self.overlap_plan()
        self.proc.assessment_barrier = threading.Barrier(2)
        self.process(card)
        self.assertEqual(card['status'], 'Done')
        self.assertEqual(self.proc.lanes[0], 'inspection')
        self.assertCountEqual(self.proc.lanes[1:], ['security', 'review'])
        record = self.f.record(card)
        attempt = record['review_inspections'][0]
        self.assertEqual(attempt['state'], 'checks-passed')
        self.assertGreater(attempt['overlap_seconds'], 0)
        self.assertEqual(attempt['pins']['role'], 'release-package-qa')
        final = record['receipts']['review']
        intervals = [record['receipts'][lane]['execution_interval'] for lane in ('security', 'review')]
        self.assertLess(max(window[0] for window in intervals), min(window[1] for window in intervals))
        self.assertGreaterEqual(min(window[0] for window in intervals), attempt['check_finished'])
        self.assertEqual(self.f.aux_inspections, 0)
        self.assertFalse(self.f.parallel_release_assessments)
        descriptor = record['release_evidence']['provisional_qa']
        self.assertEqual(final['provisional_qa'], descriptor)
        self.assertEqual(final['inspection_sha256'], attempt['receipt']['receipt_sha256'])
        self.assertEqual(final['result']['release_qa']['verification_bindings_sha256'],
                         descriptor['verification_bindings_sha256'])
        manifest = json.loads((self.r.directory / '1.0.0/publication-manifest.json').read_text())
        self.assertEqual(set(manifest['verification']['independent_receipts_sha256']), {'security', 'review'})
        self.assertEqual(manifest['verification']['package_qa_assessment'], {
            'inspection_sha256': descriptor['inspection_sha256'], 'final_review_sha256': digest(final)})
        self.assertNotIn(str(self.root), json.dumps(manifest))
        self.f.state['run_id'] = self.f.run_dir.name
        self.f.save()
        accounting = delivery.report(self.root)
        for report in (accounting, accounting['releases'][0]):
            self.assertEqual(report['inspection_overlap']['inspection']['calls'], 1)
            self.assertGreater(report['inspection_overlap']['assessment_wall_seconds'], 0)
            final_call = report['inspection_overlap']['final_assessment']
            self.assertEqual(final_call['calls'], 1)
            self.assertEqual(final_call['tokens']['input_tokens']['known'], 10)
            self.assertGreater(final_call['compute_seconds'], 0)
        artifact = next(row for row in accounting['executions'] if row.get('release_phase') == 'artifact')
        self.assertEqual(artifact['base'], attempt['pins']['base'])

    def test_parallel_security_finding_blocks_release_despite_passing_review(self):
        card = self.overlap_plan()
        self.proc.assessment_barrier = threading.Barrier(2)
        self.proc.security_finding = True
        self.process(card)
        self.assertEqual(card['status'], 'Blocked')
        receipts = self.f.record(card)['receipts']
        self.assertEqual(receipts['security']['result']['verdict'], 'fail')
        self.assertEqual(receipts['review']['result']['verdict'], 'pass')
        self.assertFalse(self.remote.writes)
        self.assertEqual(self.f.aux_inspections, 0)

    def test_one_parallel_worker_finishing_does_not_mark_ticket_waiting(self):
        card = self.overlap_plan()
        self.proc.assessment_barrier = threading.Barrier(2)
        inactive = threading.Event()
        observations = []

        def observe(lane, callback):
            if lane == 'security':
                before = self.f.record(card).get('activity', {}).get('action')
                callback('inactive')
                after = self.f.record(card).get('activity', {}).get('action')
                observations.append((before, after))
                inactive.set()
            elif not inactive.wait(5):
                raise AssertionError('Security did not send its completion observation')

        self.proc.parallel_observer = observe
        self.process(card)
        self.assertEqual(card['status'], 'Done')
        self.assertEqual(len(observations), 1)
        self.assertEqual(*observations[0])

    def test_saved_parallel_review_finding_blocks_before_restarting_security(self):
        card = self.overlap_plan()
        with patch.object(Releases, 'assess_package', side_effect=Stopped('interrupted before assessment')), \
                self.assertRaises(Stopped):
            self.process(card)
        record = self.f.record(card)
        record['receipts'] = {'review': {'result': {'verdict': 'fail'}}}
        self.f.save()
        with patch.object(self.f, 'lane', side_effect=AssertionError('unexpected worker')):
            self.process(card)
        self.assertEqual(card['status'], 'Blocked')
        self.assertFalse(self.remote.writes)

    def test_parallel_rejection_recovers_blocked_write_by_readback(self):
        card = self.overlap_plan()
        self.proc.assessment_barrier = threading.Barrier(2)
        self.proc.security_finding = True
        move = self.gh.move

        def interrupt_after_write(item, status):
            move(item, status)
            if status == 'Blocked':
                raise KeyboardInterrupt('interrupted after Blocked')

        with patch.object(self.gh, 'move', side_effect=interrupt_after_write), self.assertRaises(KeyboardInterrupt):
            self.process(card)
        with patch.object(self.gh, 'move', wraps=move) as writes:
            self.assertTrue(self.r.reconcile(card))
            writes.assert_not_called()
        self.assertEqual(self.r.journal['state'], 'blocked')
        self.assertFalse(self.remote.writes)

    def test_changed_provisional_evidence_during_parallel_assessment_blocks_publication(self):
        card = self.overlap_plan()
        self.proc.assessment_barrier = threading.Barrier(2)
        changed = threading.Event()

        def observe(lane, _):
            if lane == 'security':
                attempt = self.f.record(card)['review_inspections'][0]
                Path(attempt['receipt']['log']).write_text('changed inspection evidence\n')
                changed.set()
            elif not changed.wait(5):
                raise AssertionError('Fixture log was not changed')

        self.proc.parallel_observer = observe
        with self.assertRaisesRegex(Halt, 'Provisional inspection changed'):
            self.process(card)
        self.assertFalse(self.remote.writes)
        self.assertEqual(self.f.aux_inspections, 0)
        self.assertFalse(self.f.parallel_release_assessments)

    def test_parallel_resume_keeps_pinned_review_when_provisional_window_has_aged(self):
        from sfc_lib import review_policy

        card = self.overlap_plan()
        lane = self.f.lane
        review_saved = threading.Event()

        def interrupt_security(item, name, area, base):
            if name == 'security':
                if not review_saved.wait(5):
                    raise AssertionError('Review receipt was never saved')
                raise Stopped('interrupted Security')
            result = lane(item, name, area, base)
            if name == 'review':
                review_saved.set()
            return result

        with patch.object(self.f, 'lane', side_effect=interrupt_security), self.assertRaises(Stopped):
            self.process(card)
        self.assertEqual(card['status'], 'QA')
        review = self.f.record(card)['receipts']['review']
        fresh = review_policy.receipt_fresh

        def expire(c, receipt, *, at=None, since=None):
            if receipt.get('kind') == 'review-inspection' and at is None:
                return False
            return fresh(c, receipt, at=at, since=since)

        with patch.object(review_policy, 'receipt_fresh', side_effect=expire):
            self.process(card)
        receipts = self.f.record(card)['receipts']
        self.assertEqual(card['status'], 'Done')
        self.assertNotIn('qa', receipts)
        self.assertEqual(receipts['review'], review)
        self.assertEqual(self.proc.lanes.count('review'), 1)

    def test_qualified_release_without_representative_inspection_history_keeps_full_route(self):
        card = self.overlap_plan(representative=False)
        self.process(card)
        self.assertEqual(card['status'], 'Done')
        self.assertEqual(self.proc.lanes, ['qa', 'security', 'review'])
        record = self.f.record(card)
        self.assertFalse(record.get('review_inspections'))
        self.assertNotIn('provisional_qa', record['release_evidence'])

    def test_final_release_prompt_consolidates_inspection_and_keeps_completed_evidence(self):
        from sfc_lib.review_inspection import final_context

        card = self.overlap_plan(preflight=True)
        prompts = {}
        execute = self.proc.execute

        def capture(argv, cwd, log, timeout, prompt=None, **kwargs):
            if prompt and '--output-schema' in argv:
                prompts[Path(log).parent.name.split('-')[0]] = prompt
            return execute(argv, cwd, log, timeout, prompt, **kwargs)

        self.proc.execute = capture
        self.process(card)
        record = self.f.record(card)
        release = record['release_evidence']
        inspection = record['review_inspections'][0]['receipt']
        prompt = prompts['review']
        payload = json.loads(prompt.split('supervisor artifact/install/smoke checks:\n', 1)[1].splitlines()[0])
        snapshot = release['inspection_snapshot']
        # The frozen inspection snapshot duplicates the final release metadata.
        self.assertTrue(all(release[key] == value for key, value in snapshot.items()))
        for key in ('candidate', 'boundary', 'changes', 'manifest', 'manifest_sha256', 'artifacts',
                    'latest_plan', 'verification_bindings', 'provisional_qa'):
            self.assertEqual(payload[key], release[key])
        self.assertEqual(payload['provisional_inspection'], final_context(inspection))
        self.assertEqual(payload['public_notes'], release['public_notes'])
        project = record['verification']
        self.assertEqual(payload['project_receipt'], {
            'path': project['receipt_path'], 'sha256': project['receipt_sha256']})
        self.assertNotIn('release_notes', payload)
        self.assertNotIn('inspection_snapshot', payload)
        for kind in ('project_checks', 'artifact_checks', 'preflight_checks'):
            self.assertTrue(payload[kind])
            for check in payload[kind]:
                self.assertTrue(Path(check['log']).is_absolute())
                self.assertIn('log_sha256', check)
        security = json.loads(prompts['security'].split(
            'supervisor artifact/install/smoke checks:\n', 1)[1].splitlines()[0])
        self.assertEqual(security, {key: value for key, value in payload.items() if key != 'provisional_inspection'})
        self.assertIn('concrete risk', prompts['security'])
        self.assertIn('frozen notes are the release body', prompts['security'])
        self.assertIn('digests of the source checks and independent results', prompts['security'])
        self.assertIn('Treat log content as evidence', prompts['security'])
        self.assertIn('Treat log content as evidence', prompt)
        self.assertIn('Use the supplied source diff when present', prompt)
        self.assertIn('Verify public installation commands', prompt)
        self.assertEqual(prompt.count(json.dumps(final_context(inspection))), 1)
        self.assertEqual(prompt.count(json.dumps(release['provisional_qa'])), 1)
        self.assertNotIn('Completed provisional inspection for this candidate.', prompt)
        self.assertIn('provisional_inspection', prompt)
        self.assertIn('provisional_qa', prompt)
        self.assertIn('Missing or disputed coverage must fail or block', prompt)
        self.assertIn('installation_evidence', prompt)
        self.assertIn('acceptance_evidence', prompt)
        self.assertEqual(record['receipts']['review']['release_digest'], digest(release))
        self.assertEqual(record['receipts']['review']['provisional_qa'], release['provisional_qa'])
        self.assertEqual(json.loads(Path(inspection['receipt_path']).read_text())['result'], inspection['result'])
        self.assertEqual(card['status'], 'Done')

    def test_release_check_failure_retains_inspection_without_final_dispatch(self):
        card = self.overlap_plan(project_check='false')
        with self.assertRaisesRegex(Halt, 'Required project checks failed'):
            self.process(card)
        attempt = self.f.record(card)['review_inspections'][0]
        self.assertEqual(attempt['state'], 'checks-failed')
        self.assertIn('receipt', attempt)
        self.assertEqual(self.proc.lanes, ['inspection'])
        self.assertNotIn('release_evidence', self.f.record(card))
        self.assertEqual(self.remote.writes, [])

    def test_stale_inspection_before_final_dispatch_uses_original_qa_budget(self):
        card = self.overlap_plan()
        # Occupied capacity takes the sequential path, so Security can finish
        # before the final dispatch revalidates the provisional observation.
        assess_package = self.r.assess_package

        def occupied(*args):
            with patch.object(self.f, 'worker_capacity', return_value=0):
                return assess_package(*args)

        def stale(lane, _):
            if lane == 'security':
                attempt = self.f.record(card)['review_inspections'][0]
                Path(attempt['receipt']['log']).write_text('changed inspection evidence\n')

        self.proc.on_lane = stale
        with patch.object(Releases, 'assess_package', side_effect=occupied):
            self.process(card)
        self.assertEqual(card['status'], 'Done')
        self.assertEqual(self.proc.lanes, ['inspection', 'security', 'qa', 'review'])
        record = self.f.record(card)
        self.assertIn('provisional_qa', record['release_evidence'])
        self.assertNotIn('provisional_qa', record['receipts']['review'])
        self.assertEqual(len(record['worker_output_budget']['qa']['dispatches']), 1)

    def test_adverse_package_inspection_blocks_without_a_second_qa_or_publication(self):
        card = self.overlap_plan(preflight=True)
        self.proc.adverse = True
        self.process(card)
        self.assertEqual(card['status'], 'Blocked')
        self.assertEqual(self.r.journal['state'], 'blocked')
        self.assertEqual(self.proc.lanes, ['inspection'])
        record = self.f.record(card)
        self.assertNotIn('provisional_qa', record['release_evidence'])
        receipt = record['review_inspections'][0]['receipt']
        self.assertEqual(receipt['result']['verdict'], 'fail')
        self.assertEqual(record['inspection_rejection']['inspection']['sha256'], receipt['receipt_sha256'])
        self.assertEqual(record['attempts'], 1)
        self.assertFalse(record.get('receipts'))
        self.assertEqual(self.remote.writes, [])

    def test_release_inspection_block_recovers_ambiguous_status_by_readback(self):
        card = self.overlap_plan()
        self.proc.adverse = True
        move = self.gh.move

        def interrupt_after_write(item, status):
            move(item, status)
            if status == 'Blocked':
                raise KeyboardInterrupt('interrupted after Blocked')

        with patch.object(self.gh, 'move', side_effect=interrupt_after_write), \
             self.assertRaisesRegex(KeyboardInterrupt, 'interrupted after Blocked'):
            self.process(card)
        with patch.object(self.gh, 'move', wraps=move) as writes:
            self.assertTrue(self.r.reconcile(card))
            writes.assert_not_called()
        self.assertEqual(self.r.journal['state'], 'blocked')
        self.assertEqual(self.proc.lanes, ['inspection'])
        self.assertEqual(self.f.record(card)['attempts'], 1)
        self.assertEqual(self.remote.writes, [])

    def test_tampered_composite_result_blocks_publication(self):
        card = self.overlap_plan()
        original = self.r._publish

        def tamper(item):
            receipt = self.f.record(card)['receipts']['review']
            Path(receipt['result_path']).write_text('{}\n')
            return original(item)

        with patch.object(self.r, '_publish', side_effect=tamper), \
             self.assertRaisesRegex(Halt, 'Missing/stale final release package QA evidence'):
            self.process(card)
        self.assertEqual(self.proc.lanes[0], 'inspection')
        self.assertCountEqual(self.proc.lanes[1:], ['security', 'review'])
        self.assertEqual(self.remote.writes, [])

    def test_published_composite_release_resumes_after_inspection_age_limit(self):
        from sfc_lib import review_policy

        card = self.overlap_plan()
        publish = self.remote.publish

        def interrupt(*args):
            publish(*args)
            raise Stopped('Interrupted after remote publication')

        with patch.object(self.remote, 'publish', side_effect=interrupt), self.assertRaises(Stopped):
            self.process(card)
        self.assertEqual(card['status'], 'Review')
        final = self.f.record(card)['receipts']['review']
        original_writes = list(self.remote.writes)
        original_checks = list(self.proc.events)
        self.f = Factory(self.root, self.c, self.gh, self.proc)
        self.f.run_dir = self.root / '.sfc/runs/resumed'
        self.remote.f = self.f
        self.r = Releases(self.f, self.remote)
        fresh = review_policy.receipt_fresh

        def expire(c, receipt, *, at=None, since=None):
            if receipt.get('kind') == 'review-inspection' and at is None:
                return False
            return fresh(c, receipt, at=at, since=since)

        with patch.object(review_policy, 'receipt_fresh', side_effect=expire):
            self.process(card)
        self.assertEqual(card['status'], 'Done')
        self.assertEqual(self.f.record(card)['receipts']['review'], final)
        self.assertEqual(self.remote.writes, [*original_writes, 'close'])
        self.assertEqual(self.proc.events, original_checks)
        self.assertEqual(self.proc.lanes[0], 'inspection')
        self.assertCountEqual(self.proc.lanes[1:], ['security', 'review'])

    def test_end_to_end_packages_actual_bytes_and_completes_once(self):
        self.commit()
        card = self.plan()
        self.process(card)
        self.assertEqual(card['status'], 'Done')
        self.assertEqual(self.proc.lanes, ['qa', 'security', 'review'])
        self.assertEqual(self.remote.values, {1: '1.0.0', 2: '1.0.0'})
        self.assertEqual(self.gh.items[1]['status'], 'Done')
        self.assertEqual(self.remote.tags['1.0.0'], self.r.journal['candidate'])
        self.assertFalse(self.remote.rows['1.0.0']['draft'])
        manifest = json.loads(next(data for i, data in self.remote.assets.items()
                                   if b'"schema": 1' in data))
        self.assertEqual(manifest['artifacts'][0]['sha256'], checksum(self.r.artifact_paths() / 'app.bin'))
        public = self.remote.rows['1.0.0']['body'] + json.dumps(manifest)
        self.assertNotIn(str(self.root), public)
        self.assertNotIn('mkdir', public)
        writes = copy.deepcopy(self.remote.writes)
        self.process(card)
        self.assertEqual(self.remote.writes, writes)
        self.assertEqual(self.gh.writes.count((1, 'Done')), 1)
        self.assertEqual(self.gh.prs, {})

    def test_normal_onboarding_lifetime_watcher_merges_backlog_and_publishes_new_version(self):
        import contextlib
        import io
        import subprocess
        import sfc
        from sfc_lib import autostart, lifeline
        from sfc_lib.common import read_json
        from sfc_lib.session_runtime import SessionRuntime, request_attachment
        from test_onboarding import release_source_data
        from test_pickup import PickupGitHub, ticket
        from test_priority import TicketGitHub
        from test_watch import Clock

        (self.root / 'VERSION').write_text('1.0.0\n')
        git(self.root, 'add', 'VERSION')
        git(self.root, 'commit', '--amend', '--no-edit')
        baseline = git(self.root, 'rev-parse', 'HEAD')
        bare = self.root.parent / 'origin.git'
        git(self.root, 'clone', '--bare', '.', bare)
        git(bare, 'config', 'user.email', 'fixture@example.test')
        git(bare, 'config', 'user.name', 'Fixture')
        git(self.root, 'remote', 'set-url', 'origin', bare)

        class Board(PickupGitHub):
            # Share the real creation protocol and the paginated pickup fake.
            def __init__(self, root, candidate):
                super().__init__(root, [candidate])
                self.merge_commit = None
                creator = self.creator = TicketGitHub(root)
                creator.issues[1] = {'node_id': self.items[0]['id'], 'number': 1}
                original_call = creator.call
                creator.call = lambda *a, **kw: {'id': 'p'} if a[:2] == ('project', 'view') else original_call(*a, **kw)
                self.fields = creator.fields
                self.configure_dashboard = creator.configure_dashboard
                self.dashboard = creator.dashboard
                self.dashboard_data = creator.dashboard_data
                self.factory_view = creator.factory_view
                self.values_override = lambda c, nodes: [
                    {'field': {'id': self.metadata()['field']}, 'optionId': self.metadata()['options'][c['status']]},
                    nodes[1]]

            def onboarding_identity(self):
                return {'host': 'github.com', 'repository': 'example/app',
                        'repository_id': 'repo-id', 'project_id': self.project_id()}

            def merge(self, number, sha):
                ref = 'refs/heads/' + self.prs[number]['headRefName']
                assert git(bare, 'rev-parse', ref) == sha
                assert git(bare, 'rev-parse', 'main') == baseline
                super().merge(number, sha)
                self.merge_commit = self.prs[number]['mergeCommit']['oid']

            def call(self, *args, input=None):
                if args == ('api', 'repos/example/app'):
                    return {'full_name': 'example/app', 'node_id': 'repo-id', 'permissions': {'push': True}}
                if args[0] == 'api' and ('/issues' in args[1] or '/labels' in args[1]):
                    return self.creator.call(*args, input=input)
                return super().call(*args)

            def graphql(self, query, variables):
                if 'file(path:$path)' in query:
                    return release_source_data(bare, variables, 'repo-id')
                if (query.lstrip().startswith('mutation') or 'projectItems(' in query or
                        ('fieldValues(' in query and variables['item'] in self.creator.items)):
                    result = self.creator.graphql(query, variables)
                    if 'updateProjectV2ItemFieldValue' in query:
                        item = self.creator.items[variables['input']['itemId']]
                        if len(item['values']) == 2:
                            c = item['content']
                            self.items.append(ticket(c['number'], **{k: v for k, v in c.items() if k != 'number'},
                                                    status='Backlog', labels=self.creator.issues[c['number']]['labels']))
                            # The same live item now uses the pickup adapter for status/readback.
                            del self.creator.items[item['id']]
                    return result
                return super().graphql(query, variables)

        class ReleaseRemote(Remote):
            def changes(self, commits):
                merge_commit = self.f.gh.merge_commit
                prs = [{'number': number, 'merge_commit': merge_commit, 'issues': [1],
                        'title': 'feat: Update packaged document reader', 'public': True}
                       for number, _ in self.f.gh.merges if merge_commit in commits]
                grouped = [{'commit': commit, 'pull_requests': [p['number'] for p in prs if p['merge_commit'] == commit]}
                           for commit in commits]
                return {'commits': grouped, 'pull_requests': prs, 'issues': [1] if prs else [],
                        'unlinked_commits': [entry['commit'] for entry in grouped if not entry['pull_requests']]}

        self.gh = Board(self.root, ticket(status='Backlog', priority='P0', labels=['bug', 'sfc-hold']))
        self.remote = ReleaseRemote(self.f)
        release_file = self.root / '.sfc/release-input.json'
        release = {**self.c['release'], 'trigger': 'version-file', 'version_file': 'VERSION'}
        release['approved_summaries']['entries'][0]['commit'] = baseline
        write_json(release_file, release)
        args = ['--root', str(self.root), 'onboard', '--repo', 'example/app', '--owner', 'example',
                '--project', '1', '--base', 'main', '--allow-merge',
                '--verify', 'test -s value.txt && test -s VERSION', '--watch-poll-interval', '1', '--watch-max-polls', '5',
                '--release-config', str(release_file), '--release-authorization', 'reviewed-publication']

        def setup_adapter(root, configuration):
            self.gh.c = self.gh.creator.c = configuration
            return self.gh

        def factory(root):
            self.f = Factory(root, github=self.gh, processes=self.proc)
            self.remote.f = self.f
            return self.f

        def build(lane, cwd):
            if lane == 'build':
                (cwd / 'VERSION').write_text('1.1.0\n')

        self.proc.on_lane = build
        clock = Clock()
        clock.active = lambda: bool(self.f.occupied)
        runtime = SessionRuntime(self.root, environ={}, factory_type=factory, clock=clock, wait=clock.wait)
        self.addCleanup(runtime.close)
        event = {'hook_event_name': 'SessionStart', 'source': 'startup', 'session_id': 'onboarded-watcher',
                 'cwd': str(self.root), 'permission_mode': 'default'}

        def version(argv, *a, **kw):
            if argv[:2] == ['codex', '--version']:
                return subprocess.CompletedProcess(argv, 0, 'fixture', '')
            return run(argv, *a, **kw)

        def run_command(argv, *, enabled=False, receipt=None, **kwargs):
            return subprocess.Popen(argv, **kwargs), None  # Native cleanup is tested separately.

        output = io.StringIO()
        with patch.object(sfc, 'GitHub', side_effect=setup_adapter), \
                patch.object(lifeline, 'supported', return_value=True), \
                patch('sfc_lib.session_runtime.supported', return_value=True), \
                patch.object(lifeline, 'spawn', side_effect=run_command), \
                patch('sfc_lib.factory.run', side_effect=version), \
                patch('sfc_lib.releases.ReleaseGitHub', return_value=self.remote):
            with contextlib.redirect_stdout(output):
                self.assertEqual(sfc.main([*args, '--preview']), 0)
            preview = json.loads(output.getvalue())
            self.assertEqual(preview['setup_review']['capabilities']['automatic_merge']['mode'], 'github-protected')
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(sfc.main([*args, '--expected-review', preview['setup_review']['review_digest']]), 0)
            auth_path = autostart.paths(self.root)[1]
            auth = read_json(auth_path)
            self.assertEqual((auth['version'], auth['state'], auth['scope']['allow_merge']), (2, 'armed', True))
            self.assertEqual(self.gh.writes + self.proc.events, [])
            self.assertEqual(request_attachment(event, environ={})['state'], 'pending')
            self.assertEqual(runtime.attach(event, background=False, native_thread=event['session_id'])['state'], 'pending')
            runtime.window()
            self.assertEqual(self.remote.tags, {'1.0.0': baseline})
            self.assertEqual(self.gh.items[0]['status'], 'Backlog')
            self.assertEqual(self.proc.lanes, ['qa', 'security', 'review'])
            self.gh.items[0]['labels'].remove('sfc-hold')
            runtime.window()

        current = read_json(auth_path)
        self.assertEqual(current['id'], auth['id'])
        self.assertEqual(len(current['windows']), 2)
        self.assertEqual(self.proc.lanes, ['qa', 'security', 'review', 'build', 'qa', 'security', 'review',
                                           'qa', 'security', 'review'])
        card = self.gh.items[0]
        ordinary = self.f.record(card)
        self.assertEqual((card['status'], card['state'], ordinary['claim']['state']), ('Done', 'CLOSED', 'verified'))
        (_, reviewed_head), = self.gh.merges
        self.assertEqual(ordinary['receipts']['review']['head'], reviewed_head)
        verification = ordinary['verification']
        self.assertEqual((verification['head'], verification['base']), (reviewed_head, baseline))
        self.assertTrue(verification['passed'])
        merged = git(bare, 'rev-parse', 'main')
        self.assertEqual(merged, self.gh.merge_commit)
        self.assertEqual(git(bare, 'rev-parse', merged + '^{tree}'), git(bare, 'rev-parse', reviewed_head + '^{tree}'))
        journal_dir = Releases(self.f, self.remote).directory
        journal = read_json(journal_dir / '1.1.0.json')
        self.assertEqual((journal['state'], journal['candidate'], journal['boundary']), ('done', merged, baseline))
        self.assertEqual(journal['changes']['issues'], [1])
        self.assertEqual(self.remote.tags, {'1.0.0': baseline, '1.1.0': merged})
        self.assertEqual(self.remote.writes.count('publish'), 2)
        self.assertFalse(self.remote.rows['1.1.0']['draft'])
        self.assertEqual(journal['effects']['publication']['state'], 'verified')
        expected = journal['effects']['asset-app.bin']['observed']['sha256']
        downloaded = journal_dir / '1.1.0/downloads/app.bin'
        self.assertEqual(checksum(downloaded), expected)
        self.assertEqual(downloaded.read_bytes(), b'fixed\n')
        release_card = self.remote.card(journal['card']['number'])
        self.assertEqual((release_card['status'], release_card['state']), ('Done', 'CLOSED'))
        self.assertEqual(self.f.record(release_card)['activity']['state'], 'Completed')
        self.assertEqual(self.gh.creator.writes.count('create'), 2)

    def test_initial_and_subsequent_ranges_include_unlinked_changes(self):
        cutoff = self.commit()
        boundary, candidate, changes = self.r.source()
        self.assertIsNone(boundary)
        self.assertEqual(len(changes['commits']), 2)
        self.assertEqual(changes['unlinked_commits'], [self.initial])
        self.remote.tags['0.9.0'] = self.initial
        self.remote.rows['0.9.0'] = {'id': 1, 'tag_name': '0.9.0', 'draft': False}
        boundary, candidate, changes = self.r.source()
        self.assertEqual(boundary, self.initial)
        self.assertEqual([m['commit'] for m in changes['commits']], [cutoff])

    def test_private_verification_bindings_resolve_manifest_hashes_without_public_inputs(self):
        self.c['release']['preflight_commands'] = ['cmp dist/app.bin value.txt']
        card = self.plan()
        self.process(card)
        ref = self.f.record(card)['release_evidence']['verification_bindings']
        path = Path(ref['path'])
        value = json.loads(path.read_text())
        self.assertEqual(checksum(path), ref['sha256'])
        manifest = json.loads((self.r.artifact_paths() / 'sfc-release-manifest.json').read_text())
        self.assertEqual(value['digests'], manifest['verification'])
        self.assertEqual({key: digest(item) for key, item in value['inputs'].items()}, value['digests'])
        self.assertNotIn('verification-bindings.json', [a['name'] for a in self.remote.rows['1.0.0']['assets']])
        responsibilities = {'qa': 'Release QA owns package and installation behavior',
                            'security': 'Release security owns risks',
                            'review': 'Final release review owns the actual source range'}
        for lane, focus in responsibilities.items():
            prompt = next(self.f.run_dir.glob(f'issue-1/{lane}-*/prompt.md')).read_text()
            self.assertIn('This is prepublication assessment', prompt)
            self.assertIn('Investigate concrete discrepancies', prompt)
            self.assertIn(focus, prompt)
            for other in responsibilities.values():
                if other != focus:
                    self.assertNotIn(other, prompt)
            if lane != 'qa':
                self.assertIn('do not reverse-engineer field combinations', prompt)
                self.assertIn('execution inputs match', prompt)
                self.assertIn(ref['sha256'], prompt)
                continue
            role = Path(__file__).resolve().parents[1] / 'skills/sfc-qa/RELEASE.md'
            self.assertIn(role.read_text(), prompt)
            self.assertNotIn((role.parent / 'SKILL.md').read_text(), prompt)
            qa = self.f.record(card)['worker_output_budget']['qa']['dispatches'][0]
            self.assertEqual(qa['worker_context']['lane'], 'qa')
            self.assertEqual(qa['worker_context']['skill_path'], str(role))
            self.assertEqual(qa['worker_context']['skill_sha256'], checksum(role))
            self.assertNotIn(ref['sha256'], prompt)
            self.assertNotIn(str(path), prompt)
            self.assertNotIn('receipt_sha256', prompt)
            packet = json.loads(prompt.split('Supervisor-validated release evidence:\n')[1].splitlines()[0])
            release = self.f.record(card)['release_evidence']
            for key in ('version', 'candidate', 'boundary', 'changes', 'manifest', 'manifest_sha256',
                        'artifacts', 'release_notes', 'public_notes'):
                self.assertEqual(packet.get(key), release.get(key))
            for kind, receipt in (('project_checks', self.f.record(card)['verification']),
                                  ('artifact_checks', release['artifact_checks']),
                                  ('preflight_checks', release['preflight_checks'])):
                self.assertTrue(receipt['checks'])
                self.assertEqual(packet[kind], [
                    {'id': index, 'command': check['command'], 'exit_code': 0, 'log': check['log'],
                     'log_sha256': check['log_sha256'], 'reused': False}
                    for index, check in enumerate(receipt['checks'], 1)])
            self.assertEqual(self.f.record(card)['receipts']['qa']['release_digest'], digest(release))

    def test_missing_release_qa_context_blocks_before_any_worker(self):
        from sfc_lib import worker_context

        card = self.plan()
        original = worker_context._read

        def read(package, parts):
            return None if parts == ('skills', 'sfc-qa', 'RELEASE.md') else original(package, parts)

        with patch.object(worker_context, '_read', side_effect=read), \
                self.assertRaisesRegex(Halt, 'Release QA requires pinned release instructions and schemas'):
            self.process(card)
        self.assertEqual(self.proc.lanes, [])
        self.assertNotIn('worker_output_budget', self.f.record(card))
        self.assertEqual(list(self.f.run_dir.glob('issue-1/qa-*')), [])
        self.assertEqual(self.remote.writes, [])

    def test_changed_private_verification_bindings_prevent_publication(self):
        card = self.plan()
        original = self.proc.on_lane
        def corrupt(lane, result):
            if original:
                original(lane, result)
            if lane == 'review':
                ref = self.f.record(card)['release_evidence']['verification_bindings']
                path = Path(ref['path'])
                value = json.loads(path.read_text())
                value['inputs']['project_receipt_sha256']['passed'] = False
                write_json(path, value)
        self.proc.on_lane = corrupt
        with self.assertRaisesRegex(Halt, 'verification bindings changed'):
            self.process(card)
        self.assertEqual(self.remote.writes, [])

    def test_later_merges_do_not_change_frozen_candidate(self):
        card = self.plan()
        old = self.r.journal['candidate']
        self.commit()
        self.process(card)
        self.assertEqual(self.remote.tags['1.0.0'], old)
        self.assertEqual(self.remote.assets[1], b'initial\n')

    def test_no_release_authority_blocks_before_any_release_mutation(self):
        self.c['release'].pop('authority')
        self.f = Factory(self.root, self.c, self.gh, self.proc)
        self.f.run_dir = self.root / '.sfc/runs/test'
        self.r = Releases(self.f, self.remote)
        card = self.plan()
        with self.assertRaisesRegex(Halt, 'authority'):
            self.process(card)
        self.assertEqual(self.remote.writes, [])
        self.assertEqual(card['status'], 'Review')

    def test_package_and_artifact_failures_never_publish_or_repeat(self):
        self.c['release']['package_commands'] = ['exit 9']
        self.f = Factory(self.root, self.c, self.gh, self.proc)
        self.f.run_dir = self.root / '.sfc/runs/test'
        self.r = Releases(self.f, self.remote)
        card = self.plan()
        with self.assertRaisesRegex(Halt, 'failed'):
            self.process(card)
        before = len(self.proc.events)
        with self.assertRaises(Halt):
            self.process(card)
        self.assertEqual(len(self.proc.events), before)
        self.assertEqual(self.remote.writes, [])

    def test_artifact_smoke_failure_blocks_publication(self):
        self.c['release']['verify_commands'] = ['exit 4']
        self.f = Factory(self.root, self.c, self.gh, self.proc)
        self.f.run_dir = self.root / '.sfc/runs/test'
        self.r = Releases(self.f, self.remote)
        with self.assertRaisesRegex(Halt, 'artifact check failed'):
            self.process(self.plan())
        self.assertEqual(self.remote.writes, [])

    def test_independent_qa_and_review_failure_prevent_publication(self):
        card = self.plan()
        self.proc.fail_lane = 'review'
        self.process(card)
        self.assertEqual(card['status'], 'Blocked')
        self.assertEqual(self.remote.writes, [])

    def test_lost_asset_response_is_read_back_without_duplicate(self):
        self.remote.fail = 'upload-app.bin'
        card = self.plan()
        self.process(card)
        self.assertEqual(card['status'], 'Done')
        self.assertEqual(self.remote.writes.count('upload-app.bin'), 1)

    def test_acknowledged_draft_can_be_verified_before_release_list_catches_up(self):
        card = self.plan()
        full_release = self.remote.release
        probes = []

        def release_by_tag(version):
            if version in self.remote.rows and 'upload-app.bin' not in self.remote.writes:
                return None
            return full_release(version)

        def release_by_id(release_id):
            probes.append(release_id)
            return copy.deepcopy(next(row for row in self.remote.rows.values() if row['id'] == release_id))

        with patch.object(self.remote, 'release', side_effect=release_by_tag), \
                patch.object(self.remote, 'release_by_id', side_effect=release_by_id):
            self.process(card)
        self.assertEqual(card['status'], 'Done')
        self.assertEqual(self.remote.writes.count('draft'), 1)
        self.assertEqual(probes[0], self.r.journal['release_id'])

    def test_publication_refreshes_authority_once_per_write(self):
        card = self.plan()
        publish = self.r._publish
        counts = []

        def measured(card):
            before = len(self.remote.writes)
            with patch.object(self.remote, 'identity', wraps=self.remote.identity) as identity:
                result = publish(card)
            counts.append((identity.call_count, len(self.remote.writes) - before))
            return result

        with patch.object(self.r, '_publish', side_effect=measured):
            self.process(card)
        self.assertEqual(card['status'], 'Done')
        self.assertEqual(len(counts), 1)
        reads, writes = counts[0]
        self.assertEqual(reads, writes + 1)

    def test_permission_revoked_after_upload_blocks_next_mutation(self):
        card = self.plan()
        identity = self.remote.identity

        def live_identity():
            if 'upload-app.bin' in self.remote.writes:
                raise Halt('push permission revoked')
            return identity()

        with patch.object(self.remote, 'identity', side_effect=live_identity):
            with self.assertRaisesRegex(Halt, 'push permission revoked'):
                self.process(card)
        self.assertEqual(self.remote.writes, ['tag', 'draft', 'upload-app.bin'])
        self.assertEqual(self.r.journal['effects']['asset-app.bin']['state'], 'verified')


    def test_uncertain_upload_stops_and_restart_never_reissues(self):
        self.remote.fail = 'before-upload-app.bin'
        card = self.plan()
        with self.assertRaisesRegex(Halt, 'uncertain'):
            self.process(card)
        self.remote.fail = None
        with self.assertRaisesRegex(Halt, 'readback only'):
            self.process(card)
        self.assertTrue(self.remote.rows['1.0.0']['draft'])
        self.assertEqual(self.remote.writes.count('upload-app.bin'), 1)
        self.assertNotIn('publish', self.remote.writes)

    def test_mismatched_download_hash_never_publishes(self):
        self.remote.corrupt = True
        with self.assertRaisesRegex(Halt, 'uncertain'):
            self.process(self.plan())
        self.assertNotIn('publish', self.remote.writes)

    def test_draft_policy_waits_without_repeated_paid_work(self):
        from sfc_lib.release_github import ReleasePushPermissionMissing

        self.c['release']['authority']['mode'] = 'draft'
        self.c['release'].update(version_file='VERSION', trigger='version-file')
        self.f = Factory(self.root, self.c, self.gh, self.proc)
        self.f.run_dir = self.root / '.sfc/runs/test'
        self.r = Releases(self.f, self.remote)
        card = self.plan()
        self.process(card)
        events = copy.deepcopy(self.proc.events)
        self.process(card)
        self.assertEqual(self.proc.events, events)
        self.assertEqual(self.r.journal['state'], 'draft-unpublished')
        self.assertEqual(card['status'], 'Review')
        self.assertNotIn('publish', self.remote.writes)
        denied = ReleasePushPermissionMissing(self.remote.identity())
        writes = list(self.remote.writes)
        self.f.record(card).pop('handoff')
        with patch.object(self.remote, 'identity', side_effect=denied):
            self.assertEqual(self.r.trigger(), {'state': 'pending', 'reason': str(denied)})
            self.assertTrue(self.r.reconcile(card))
            self.assertIn('unpublished', self.f.record(card)['handoff'])
            with patch.dict(self.f.record(card), {'attempts': 0}):
                with self.assertRaisesRegex(Halt, 'attempt reservation counter/scope changed'):
                    self.r.reconcile(card)
            self.r.journal['effects']['uncertain'] = {'state': 'intent'}
            self.r.save()
            with self.assertRaisesRegex(Halt, 'frozen release is unfinished'):
                self.r.trigger()
            with self.assertRaises(ReleasePushPermissionMissing):
                self.r.reconcile(card)
        self.assertEqual(self.remote.writes, writes)

    def test_publication_and_completion_lost_responses_do_not_duplicate(self):
        self.remote.fail = 'publish'
        card = self.plan()
        self.process(card)
        self.assertEqual(card['status'], 'Done')
        self.assertEqual(self.remote.writes.count('publish'), 1)

    def test_final_publication_preflight_preserves_observable_draft_edits(self):
        card = self.plan()
        effect = self.r.effect
        edits = {'body': 'Human draft edit', 'name': 'Human release title',
                 'tag_name': '2.0.0', 'target_commitish': 'f' * 40, 'id': 999,
                 'draft': False, 'published_at': '2026-11-10T12:00:00Z', 'prerelease': True}
        for field, value in edits.items():
            with self.subTest(field=field):
                snapshot = {}

                def concurrent(key, *args, **kwargs):
                    if key == 'publication':
                        # Edit after all asset checks, before effect's first read.
                        row = self.remote.rows['1.0.0']
                        snapshot.update(row=copy.deepcopy(row), writes=list(self.remote.writes))
                        row[field] = value
                    return effect(key, *args, **kwargs)

                with patch.object(self.r, 'effect', side_effect=concurrent):
                    with self.assertRaisesRegex(Halt, 'preflight changed'):
                        self.process(card)
                self.assertEqual(self.remote.rows['1.0.0'][field], value)
                self.assertEqual(self.remote.writes, snapshot['writes'])
                self.assertNotIn('publish', self.remote.writes)
                self.assertNotIn('publication', self.r.journal['effects'])
                self.assertEqual(card['status'], 'Review')
                self.remote.rows['1.0.0'] = snapshot['row']
        # Removing the edits permits the same reviewed work to finish once.
        self.process(card)
        self.assertEqual(self.remote.writes.count('publish'), 1)
        self.assertEqual(self.proc.lanes, ['qa', 'security', 'review'])
        self.assertEqual(card['status'], 'Done')

    def test_manual_target_and_view_edits_are_preserved(self):
        card = self.plan()
        self.r.assign_members(card)
        self.remote.values[1] = 'human choice'
        with self.assertRaisesRegex(Halt, 'externally edited'):
            self.r.assign_members(card)
        self.assertEqual(self.remote.values[1], 'human choice')

    def test_stale_config_and_task_do_not_reuse_evidence(self):
        card = self.plan()
        self.f.config_digest = 'changed'
        with self.assertRaisesRegex(Halt, 'configuration/identity changed'):
            self.process(card)
        self.f.config_digest = self.r.journal['config_digest']
        card['body'] += 'edited'
        with self.assertRaisesRegex(Halt, 'card/task changed'):
            self.process(card)

    def test_duplicate_and_conflicting_versions_do_not_write(self):
        card = self.plan()
        result = self.r.request('1.0.0')
        self.assertTrue(result['deduplicated'])
        self.assertEqual(self.remote.writes, [])
        with self.assertRaisesRegex(Halt, 'unfinished'):
            self.r.request('2.0.0')

    def test_existing_release_issue_prevents_creation_without_local_journal(self):
        self.gh.items[0]['state'] = 'CLOSED'
        with patch('sfc_lib.priority.create_ticket') as create:
            with self.assertRaisesRegex(Halt, 'already has a GitHub issue'):
                self.r.request('1.0.0')
        create.assert_not_called()
        self.assertFalse(self.r.path.exists())

    def corrected_candidate(self, *, changed_input=False):
        from test_pickup import PickupGitHub

        (self.root / 'README.md').write_text('Original package instructions\n')
        git(self.root, 'add', 'README.md')
        git(self.root, 'commit', '-m', 'Add release documentation')
        command = self.c['verify_commands'][0]
        self.c['verification_reuse'] = [{
            'command': command, 'max_age_seconds': 3600, 'environment': ['PATH'],
            'tools': [str(Path(sys.executable).resolve())], 'dependencies': [], 'artifacts': [],
            'external_state': 'none', 'worktree': 'independent', 'scope': 'repository',
            'context': 'source-only', 'source_inputs': ['value.txt'], 'outputs': ['dist'],
        }]
        self.c['verify_commands'].append('test -s value.txt')
        self.c['release']['package_commands'] = [
            'mkdir -p dist && cp value.txt dist/app.bin && '
            'if test -f README.md; then cat README.md >> dist/app.bin; fi'
        ]
        self.c['release']['verify_commands'] = ['test -s dist/app.bin']
        self.gh = PickupGitHub(self.root, self.gh.items)
        self.f = Factory(self.root, self.c, self.gh, self.proc)
        self.f.run_dir = self.root / '.sfc/runs/test'
        self.remote.f = self.f
        self.r = Releases(self.f, self.remote)
        card = self.plan()
        self.proc.fail_lane = 'qa'
        self.run_scheduler(self.f)
        card = self.remote.card(1)
        self.assertEqual(card['status'], 'Blocked')
        self.assertEqual(self.remote.writes, [])
        (self.root / 'README.md').write_text('Corrected package instructions\n')
        if changed_input:
            (self.root / 'value.txt').write_text('Corrected runtime behavior\n')
        git(self.root, 'add', 'README.md', 'value.txt')
        git(self.root, 'commit', '-m', 'Correct the release candidate')
        self.proc.fail_lane = None
        return card, git(self.root, 'rev-parse', 'HEAD')

    def restart_correction(self):
        self.f = Factory(self.root, self.c, self.gh, self.proc)
        self.f.run_dir = self.root / '.sfc/runs/after-correction'
        self.remote.f = self.f
        self.r = Releases(self.f, self.remote)

    def test_release_corrects_untouched_plan_and_uses_first_attempt_on_same_card(self):
        card = self.plan()
        original = read_json(self.r.path)
        candidate = self.commit()

        result = recover(self.r, '1.0.0', candidate)
        self.assertEqual((result['state'], result['attempt']), ('preview', 1))
        self.assertEqual(read_json(self.r.path), original)

        self.assertEqual(recover(self.r, '1.0.0', candidate, apply=True)['state'], 'planned')
        saved = read_json(self.r.path)
        self.assertEqual(saved['card'], original['card'])
        self.assertEqual(saved['candidate'], candidate)
        self.assertEqual(read_json(Path(saved['previous_plan']['path'])), original)
        self.assertEqual(self.f.record(card)['attempts'], 0)
        self.assertEqual(self.remote.writes, [])
        self.assertTrue(recover(self.r, '1.0.0', candidate, apply=True)['deduplicated'])

        self.process(card)
        self.assertEqual(self.remote.tags['1.0.0'], candidate)
        self.assertEqual(self.f.record(card)['attempts'], 1)
        self.assertEqual(self.remote.card(1)['status'], 'Done')

    def test_release_cannot_correct_planned_card_with_retained_write_intent(self):
        self.plan()
        self.r.journal['effects']['target-field'] = {'state': 'intent'}
        self.r.save()
        original = read_json(self.r.path)

        with self.assertRaisesRegex(Halt, 'untouched planned release'):
            recover(self.r, '1.0.0', self.commit(), apply=True)
        self.assertEqual(read_json(self.r.path), original)
        self.assertEqual(self.remote.writes, [])

    def test_release_corrects_untouched_backlog_plan_without_admission(self):
        self.gh.items[0]['status'] = 'Backlog'
        card = self.plan()
        original = read_json(self.r.path)
        candidate = self.commit()

        self.assertEqual(recover(self.r, '1.0.0', candidate)['state'], 'preview')
        self.assertEqual(read_json(self.r.path), original)
        self.assertEqual(recover(self.r, '1.0.0', candidate, apply=True)['state'], 'planned')
        saved = read_json(self.r.path)
        self.assertEqual(saved['card'], original['card'])
        self.assertEqual(saved['candidate'], candidate)
        self.assertEqual(read_json(Path(saved['previous_plan']['path'])), original)
        self.assertEqual(self.remote.card(1)['status'], 'Backlog')
        self.assertEqual(self.f.record(card)['attempts'], 0)
        self.assertEqual(self.remote.writes, [])
        self.assertEqual(self.gh.writes, [])

    def test_release_planned_correction_rejects_prior_attempt_or_evidence(self):
        card = self.plan()
        original = read_json(self.r.path)
        candidate = self.commit()
        record = self.f.record(card)
        before = copy.deepcopy(record)
        for changes in ({'attempts': 1}, {'status': 'Building'},
                        {'workflow_kind': 'implementation'}, {'release_version': '2.0.0'},
                        *({key: {}} for key in ('claim', 'release_attempt', 'worktree',
                                               'verification', 'release_evidence', 'receipts'))):
            with self.subTest(changes=changes):
                record.clear()
                record.update(before, **changes)
                for apply in (False, True):
                    with self.assertRaisesRegex(Halt, 'untouched planned release'):
                        recover(self.r, '1.0.0', candidate, apply=apply)
                self.assertEqual(read_json(self.r.path), original)
                self.assertEqual(self.remote.writes, [])
                self.assertEqual(self.gh.writes, [])

    def test_release_correction_reuses_check_and_reconciles_lost_stage_response(self):
        card, candidate = self.corrected_candidate()
        previous_check = self.f.record(card)['verification']['checks'][0]
        previous_artifact = self.r.artifact_paths() / 'app.bin'
        self.assertEqual(recover(self.r, '1.0.0', candidate)['state'], 'preview')
        self.assertNotIn('pending_correction', read_json(self.r.path))

        save = self.r.save

        def interrupted_readback():
            operation = self.r.journal['effects'].get('correction-Building', {})
            if operation.get('state') == 'verified':
                raise Stopped('Lost response before correction stage readback was saved')
            return save()

        with patch.object(self.r, 'save', side_effect=interrupted_readback):
            with self.assertRaises(Stopped):
                recover(self.r, '1.0.0', candidate, apply=True)
        self.assertEqual(self.remote.card(1)['status'], 'Building')
        self.assertEqual(self.f.record(card)['attempts'], 1)
        (self.root / 'later.txt').write_text('A later unrelated merge\n')
        git(self.root, 'add', 'later.txt')
        git(self.root, 'commit', '-m', 'Merge unrelated work after correction was frozen')
        self.restart_correction()
        with patch.object(self.gh, 'move', wraps=self.gh.move) as move:
            corrected = recover(self.r, '1.0.0', candidate, apply=True)
        move.assert_not_called()
        self.assertEqual(corrected['card']['item_id'], card['item_id'])
        self.assertEqual(self.f.record(card)['attempts'], 2)
        self.assertTrue(recover(self.r, '1.0.0', candidate, apply=True)['deduplicated'])

        self.restart_correction()
        current = self.remote.card(1)
        self.assertTrue(self.r.continuing_attempt(current))
        self.assertEqual(self.run_scheduler(self.f)['outcome'], 'drained')
        verification = self.f.record(current)['verification']
        self.assertEqual(verification['checks'][0]['reuse'], previous_check)
        self.assertNotIn('reuse', verification['checks'][1])
        self.assertEqual(self.proc.lanes, ['qa', 'qa', 'security', 'review'])
        self.assertEqual(self.remote.card(1)['status'], 'Done')
        self.assertEqual(self.remote.tags, {'1.0.0': candidate})
        self.assertEqual(self.remote.writes.count('publish'), 1)
        self.assertEqual(self.remote.assets[1], b'initial\nCorrected package instructions\n')
        self.assertEqual(previous_artifact.read_bytes(), b'initial\nOriginal package instructions\n')
        self.assertTrue(self.r.request('1.0.0')['deduplicated'])
        self.assertEqual(len(self.gh.items), 2)

    def test_release_correction_reruns_changed_input_and_rejects_existing_tag(self):
        card, candidate = self.corrected_candidate(changed_input=True)
        self.remote.tags['1.0.0'] = self.initial
        with self.assertRaisesRegex(Halt, 'no tag'):
            recover(self.r, '1.0.0', candidate, apply=True)
        self.assertEqual(self.f.record(card)['attempts'], 1)
        self.assertEqual(self.remote.card(1)['status'], 'Blocked')
        del self.remote.tags['1.0.0']
        recover(self.r, '1.0.0', candidate, apply=True)
        self.process(self.remote.card(1))
        self.assertNotIn('reuse', self.f.record(card)['verification']['checks'][0])
        self.assertEqual(self.remote.card(1)['status'], 'Done')

    def test_release_correction_preserves_debit_across_lost_local_journal_advance(self):
        command = 'grep -q corrected dist/app.bin'
        self.c['release']['verify_commands'] = [command]
        self.f = Factory(self.root, self.c, self.gh, self.proc)
        self.f.run_dir = self.root / '.sfc/runs/first'
        self.remote.f = self.f
        self.r = Releases(self.f, self.remote)
        card = self.plan()
        with self.assertRaisesRegex(Halt, 'artifact check failed'):
            self.process(card)
        self.assertEqual(self.remote.card(1)['status'], 'Building')
        (self.root / 'value.txt').write_text('corrected\n')
        git(self.root, 'add', 'value.txt')
        git(self.root, 'commit', '-m', 'Repair failed package check')
        candidate = git(self.root, 'rev-parse', 'HEAD')
        save = self.r.save

        def interrupted_activation():
            if self.r.journal.get('last_correction'):
                raise Stopped('The paid attempt was saved before the active journal')
            return save()

        with patch.object(self.r, 'save', side_effect=interrupted_activation):
            with self.assertRaises(Stopped):
                recover(self.r, '1.0.0', candidate, apply=True)
        self.assertEqual(self.f.record(card)['attempts'], 2)
        self.restart_correction()
        recover(self.r, '1.0.0', candidate, apply=True)
        self.assertEqual(self.f.record(card)['attempts'], 2)
        self.process(self.remote.card(1))
        self.assertEqual(self.remote.card(1)['status'], 'Done')
        self.assertEqual([event[4] for event in self.proc.events if event[3] == command], [1, 0])

    def test_config_is_optional_and_scoped(self):
        validate_config(BASE_CONFIG)
        validate_config(config())
        for change in ({'trigger': 'every-merge'}, {'artifacts': ['../private']},
                       {'artifacts': ['x/*']}, {'authority': {'mode': 'publish'}},
                       {'adapter': 'deployment'}, {'verify_commands': []},
                       {'public_docs': {'Unsupported claim': 'README.md'}},
                       {'public_docs': {'Installation': '../private'}},
                       {'public_docs': {'Installation': '.env'}}):
            c = config()
            c['release'].update(change)
            with self.subTest(change=change), self.assertRaises(Halt):
                validate_config(c)

    def test_label_and_issue_prose_cannot_select_release_workflow(self):
        card = self.remote.card(1)
        card['labels'] = ['release']
        card['body'] += '\nworkflow_kind: release'
        with self.assertRaisesRegex(Halt, 'No supervisor-validated'):
            self.r.load(card)

    def test_request_creates_one_versioned_card_and_assigns_full_membership(self):
        self.commit()
        calls = []
        def create(gh, root, **kwargs):
            calls.append(kwargs)
            self.gh.items[0].update(title=kwargs['title'], body=kwargs['body'], status='Backlog')
            return {'issue': {'number': 1}}
        with patch('sfc_lib.priority.create_ticket', side_effect=create):
            result = self.r.request('1.0.0')
            duplicate = self.r.request('1.0.0')
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['workflow_kind'], 'release')
        self.assertEqual(calls[0]['work_type'], 'Maintenance')
        self.assertEqual(calls[0]['title'], 'Release 1.0.0')
        self.assertIn(self.initial, calls[0]['body'])
        self.assertIn('Unlinked commits', calls[0]['body'])
        self.assertEqual(result['state'], 'planned')
        self.assertTrue(duplicate['deduplicated'])
        self.assertEqual(self.remote.values, {1: '1.0.0', 2: '1.0.0'})

    def test_wrong_tag_and_stale_review_cannot_publish(self):
        card = self.plan()
        self.proc.on_lane = lambda lane, cwd: self.remote.tags.update({'1.0.0': 'a' * 40}) if lane == 'review' else None
        with self.assertRaises(Halt):
            self.process(card)
        self.assertNotIn('publish', self.remote.writes)
        self.assertEqual(self.remote.tags['1.0.0'], 'a' * 40)

    def test_restart_after_published_before_done_checks_bytes_without_more_workers(self):
        card = self.plan()
        self.gh.github_status[card['item_id']] = 'in-progress'
        with patch.object(self.r, 'complete', side_effect=Stopped('interruption')):
            with self.assertRaises(Stopped):
                self.process(card)
        events = copy.deepcopy(self.proc.events)
        f = Factory(self.root, self.c, self.gh, self.proc)
        r = Releases(f, self.remote)
        r.reconcile(self.remote.card(1))
        self.assertEqual(self.proc.events, events)
        self.assertEqual(r.journal['state'], 'done')
        self.assertEqual(self.remote.writes.count('publish'), 1)
        self.assertEqual(self.gh.writes.count((1, 'Done')), 1)
        self.assertEqual(self.gh.github_status[card['item_id']], 'done')
        self.assertEqual(self.gh.github_status_writes, [card['item_id']])

    def test_restart_after_done_write_lost_before_completed_readback(self):
        card = self.plan()
        real_report = self.f.activity.report
        def interrupt(card, state, action, **kwargs):
            if state == 'Completed':
                raise Stopped('after Done')
            return real_report(card, state, action, **kwargs)
        with patch.object(self.f.activity, 'report', side_effect=interrupt):
            with self.assertRaises(Stopped):
                self.process(card)
        f = Factory(self.root, self.c, self.gh, self.proc)
        r = Releases(f, self.remote)
        r.reconcile(self.remote.card(1))
        self.assertEqual(r.journal['state'], 'done')
        self.assertEqual(self.gh.writes.count((1, 'Done')), 1)

    def test_manual_stage_movement_during_review_prevents_publication(self):
        card = self.plan()
        self.proc.on_lane = lambda lane, cwd: self.gh.items[0].update(status='Blocked') if lane == 'review' else None
        with self.assertRaisesRegex(Halt, 'moved/closed'):
            self.process(card)
        self.assertEqual(self.remote.writes, [])

    def test_artifact_mutation_in_review_invalidates_all_evidence(self):
        card = self.plan()
        def change(lane, cwd):
            if lane == 'review':
                (self.r.artifact_paths() / 'app.bin').write_bytes(b'untested')
        self.proc.on_lane = change
        with self.assertRaisesRegex(Halt, 'artifact bytes changed'):
            self.process(card)
        self.assertEqual(self.remote.writes, [])

    def test_ignored_filter_and_manual_view_edits_are_not_claimed_verified(self):
        card = self.plan()
        self.r.assign_members(card)
        self.remote.factory_view['filter'] = 'human:edit'
        with self.assertRaisesRegex(Halt, 'externally edited'):
            self.r.assign_members(card)
        self.assertEqual(self.remote.factory_view['filter'], 'human:edit')

    def test_stop_during_packaging_keeps_reservation_and_never_publishes(self):
        card = self.plan()
        with patch.object(self.proc, 'execute', side_effect=Stopped('owned stop')):
            with self.assertRaises(Stopped):
                self.process(card)
        self.assertEqual(self.r.journal['package_checks']['state'], 'reserved')
        attempts = self.f.record(card)['attempts']
        with self.assertRaisesRegex(Halt, 'previously failed/interrupted'):
            self.process(card)
        self.assertEqual(self.f.record(card)['attempts'], attempts)
        self.assertEqual(self.remote.writes, [])

    def test_interrupted_project_checks_are_not_repeated_on_resume(self):
        card = self.plan()
        with patch.object(self.f, 'verify', side_effect=Stopped('interrupted project check')) as verify:
            with self.assertRaises(Stopped):
                self.process(card)
            self.assertEqual(verify.call_count, 1)
        self.assertEqual(self.r.journal['project_check_state'], 'reserved')
        f = Factory(self.root, self.c, self.gh, self.proc)
        r = Releases(f, self.remote)
        with patch.object(f, 'verify') as verify:
            with self.assertRaisesRegex(Halt, 'project checks interrupted'):
                r.process(self.remote.card(1))
            verify.assert_not_called()
        self.assertTrue(Path(r.journal['candidate_area']).is_dir())
        self.assertEqual(self.remote.writes, [])

    def test_successful_project_receipt_recovers_reservation_without_rerun(self):
        card = self.plan()
        original_verify = self.f.verify
        def interrupt_after_receipt(*args):
            self.assertTrue(original_verify(*args))
            raise Stopped('lost after saved project receipt')
        with patch.object(self.f, 'verify', side_effect=interrupt_after_receipt):
            with self.assertRaises(Stopped):
                self.process(card)
        self.assertEqual(self.r.journal['project_check_state'], 'reserved')
        f = Factory(self.root, self.c, self.gh, self.proc)
        f.run_dir = self.root / '.sfc/runs/resumed'
        r = Releases(f, self.remote)
        with patch.object(f, 'verify') as verify:
            r.process(self.remote.card(1))
            verify.assert_not_called()
        self.assertEqual(r.journal['project_check_state'], 'passed')
        self.assertEqual(r.journal['state'], 'done')

    def assert_pending_trigger_allows_backlog(self, reason):
        import subprocess
        from test_pickup import PickupGitHub, ticket
        from test_watch import Clock

        self.c['release'].update(version_file='VERSION', trigger='version-file')
        gh = PickupGitHub(self.root, [ticket(40, status='Backlog')])
        f = Factory(self.root, self.c, gh, self.proc)
        clock = Clock()
        clock.active = lambda: bool(f.occupied)
        self.remote.f = f

        def execute(argv, *args, **kwargs):
            if argv[:2] == ['codex', '--version']:
                return subprocess.CompletedProcess(argv, 0, 'fixture', '')
            return run(argv, *args, **kwargs)

        with (patch('sfc_lib.factory.run', side_effect=execute),
              patch('sfc_lib.releases.ReleaseGitHub', return_value=self.remote),
              patch('sfc_lib.priority.create_ticket') as create):
            result = f.watch(start=True, clock=clock, wait=clock.wait,
                             overrides={'max_polls': 4, 'poll_interval': 1})
        create.assert_not_called()
        self.assertEqual(result['outcome'], 'watch-polls-limit')
        self.assertEqual(f.state['release_trigger']['state'], 'pending')
        self.assertIn(reason, f.state['release_trigger']['reason'])
        self.assertIn((40, 'Building'), gh.writes)
        self.assertEqual(self.proc.lanes, ['build', 'qa', 'security', 'review'])
        self.assertEqual(self.remote.writes, [])
        self.assertFalse(f.processes.stop_path.exists())

    def test_missing_version_file_leaves_ordinary_backlog_pickup_running(self):
        self.assert_pending_trigger_allows_backlog('version file is missing')

    def test_pickup_off_keeps_a_separately_authorized_release_eligible(self):
        from test_pickup import PickupGitHub, ticket

        self.c['watch'] = {'pickup': False}
        self.gh = PickupGitHub(self.root, [ticket(1, title='Release 1.0.0', status='Backlog'),
                                         ticket(2, status='Ready', priority='P0')])
        self.f = Factory(self.root, self.c, self.gh, self.proc)
        self.f.run_dir = self.root / '.sfc/runs/test'
        self.remote.f = self.f
        self.r = Releases(self.f, self.remote)
        self.plan()
        with patch('sfc_lib.releases.ReleaseGitHub', return_value=self.remote):
            preview = self.f.watch(dry_run=True)
        self.assertEqual(preview['selected'], [1])
        self.assertEqual([row['issue'] for row in preview['waiting']], [2])
        self.assertEqual(self.remote.writes + self.gh.writes, [])

    def test_reviewed_release_grant_cannot_follow_replaced_repository_or_project(self):
        identity = self.remote.identity()
        self.c['release']['authority'].update(repository_id=identity['repository_id'], project=identity['project'])
        for key in ('repository_id', 'project'):
            with self.subTest(key=key), patch.object(self.remote, 'identity',
                                                    return_value={**identity, key: 'replacement-node'}):
                with self.assertRaisesRegex(Halt, 'Reviewed release repository or Project identity changed'):
                    self.r.request('2.0.0')
        self.assertEqual(self.remote.writes, [])
        self.assertFalse(self.r.directory.exists())

    def test_invalid_version_leaves_ordinary_backlog_pickup_running(self):
        (self.root / 'VERSION').write_text('unreleased\n')
        git(self.root, 'add', 'VERSION')
        git(self.root, 'commit', '-m', 'Prepare a future release')
        self.assert_pending_trigger_allows_backlog('does not contain a supported release version')

    def test_invalid_final_cutoff_version_leaves_ordinary_backlog_pickup_running(self):
        _, initial_cutoff = self.version_candidates()
        git(self.root, 'switch', '-c', 'later-cutoff')
        (self.root / 'VERSION').write_text('unreleased\n')
        git(self.root, 'add', 'VERSION')
        git(self.root, 'commit', '-m', 'Prepare a future release')
        final_cutoff = git(self.root, 'rev-parse', 'HEAD')
        self.assertEqual(git(self.root, 'rev-parse', 'main'), initial_cutoff)
        with patch.object(Releases, 'source', return_value=(None, final_cutoff, {})) as source:
            self.assert_pending_trigger_allows_backlog('does not contain a supported release version')
        source.assert_called()
        self.assertFalse(self.r.directory.exists())

    def test_explicit_push_denial_is_pending_only_with_verified_scope_and_no_unfinished_release(self):
        from test_pickup import PickupGitHub

        self.version_candidates()
        identity = self.remote.identity()
        self.c['release']['authority'].update(repository_id=identity['repository_id'], project=identity['project'])
        gh = PickupGitHub(self.root, [])
        gh.origin = 'https://github.com/example/app'
        call = gh.call
        repository = {'full_name': 'example/app', 'node_id': identity['repository_id'], 'permissions': {'push': False}}

        def read(*args):
            return repository if args == ('api', 'repos/example/app') else call(*args)

        with (patch.object(gh, 'call', side_effect=read),
              patch.object(self.remote, 'identity', side_effect=ReleaseGitHub(gh).identity)):
            with patch.dict(repository, {'node_id': 'changed-repository'}):
                with self.assertRaisesRegex(Halt, 'Reviewed release repository or Project identity changed'):
                    self.r.trigger()
            with patch.dict(repository, {'permissions': {}}):
                with self.assertRaisesRegex(Halt, 'push permission unavailable'):
                    self.r.trigger()
            unfinished = self.r.directory / '1.0.0.json'
            write_json(unfinished, {'state': 'creating', 'identity': identity})
            with self.assertRaisesRegex(Halt, 'frozen release is unfinished'):
                self.r.trigger()
            unfinished.unlink()
            self.assert_pending_trigger_allows_backlog('push permission is required')
            self.assertEqual(list(self.r.directory.glob('*.json')), [])

    def test_automatic_release_initial_read_reset_leaves_watcher_eligible_to_poll(self):
        import subprocess
        from sfc_lib.rate_limit import ReadConnectionReset
        from test_pickup import PickupGitHub, ticket
        from test_watch import Clock

        self.c['release'].update(version_file='VERSION', trigger='version-file')
        gh = PickupGitHub(self.root, [ticket(40, status='Backlog')])
        clock = Clock()
        f = Factory(self.root, self.c, gh, self.proc)
        clock.active = lambda: bool(f.occupied)
        self.remote.f = f
        attempts = []
        identity = self.remote.identity()

        def read_identity():
            attempts.append(clock())
            if len(attempts) == 1:
                raise ReadConnectionReset('fixture outage')
            self.assertGreaterEqual(attempts[-1] - attempts[0], 1)
            return identity

        def execute(argv, *args, **kwargs):
            if argv[:2] == ['codex', '--version']:
                return subprocess.CompletedProcess(argv, 0, 'fixture', '')
            return run(argv, *args, **kwargs)

        with (patch('sfc_lib.factory.run', side_effect=execute),
              patch('sfc_lib.releases.ReleaseGitHub', return_value=self.remote),
              patch.object(self.remote, 'identity', side_effect=read_identity),
              patch('sfc_lib.priority.create_ticket') as create):
            result = f.watch(start=True, clock=clock, wait=clock.wait,
                             overrides={'max_polls': 4, 'poll_interval': 1})
        self.assertEqual(result['outcome'], 'watch-polls-limit')
        self.assertGreater(len(attempts), 1)
        create.assert_not_called()
        self.assertIn((40, 'Building'), gh.writes)
        self.assertEqual(self.proc.lanes, ['build', 'qa', 'security', 'review'])
        self.assertEqual(self.remote.writes, [])
        self.assertFalse(f.processes.stop_path.exists())

    def test_persistent_release_only_read_failure_does_not_spend_queue_polls(self):
        import subprocess
        from sfc_lib.rate_limit import ReadConnectionReset
        from test_pickup import PickupGitHub, ticket
        from test_watch import Clock

        self.c['release'].update(version_file='VERSION', trigger='version-file')
        clock = Clock()
        gh = PickupGitHub(self.root, [])
        gh.items = []
        f = Factory(self.root, self.c, gh, self.proc)
        clock.active = lambda: bool(f.occupied)
        self.remote.f = f
        inventory_times = []

        def arrival(query, _):
            if 'items(first:100,after:$after)' in query:
                inventory_times.append(clock())
                if clock() >= 1001 and not gh.items:
                    gh.items.append(ticket(40, status='Backlog'))

        def execute(argv, *args, **kwargs):
            if argv[:2] == ['codex', '--version']:
                return subprocess.CompletedProcess(argv, 0, 'fixture', '')
            return run(argv, *args, **kwargs)

        gh.on_read = arrival
        with (patch('sfc_lib.factory.run', side_effect=execute),
              patch('sfc_lib.releases.ReleaseGitHub', return_value=self.remote),
              patch.object(self.remote, 'identity', side_effect=ReadConnectionReset('release-only outage')) as read,
              patch('sfc_lib.priority.create_ticket') as create):
            result = f.watch(start=True, clock=clock, wait=clock.wait,
                             overrides={'max_polls': 4, 'poll_interval': 1})
        self.assertEqual(result['outcome'], 'watch-polls-limit')
        self.assertGreater(read.call_count, 1)
        self.assertIn(1000, inventory_times)
        self.assertIn((40, 'Building'), gh.writes)
        self.assertEqual(f.state['release_trigger']['state'], 'pending')
        self.assertFalse(f.processes.stop_path.exists())
        self.assertEqual(self.remote.writes, [])
        create.assert_not_called()

    def test_orphaned_release_creation_halts_before_queue_even_without_a_new_request(self):
        import subprocess
        from test_watch import Clock

        self.c['release'].update(version_file='VERSION', trigger='version-file')
        orphan = self.r.directory / '1.0.0.json'
        write_json(orphan, {'state': 'creating', 'version': '1.0.0', 'identity': self.remote.identity()})
        saved = orphan.read_bytes()
        absent = git(self.root, 'rev-parse', 'HEAD')
        same, later = self.version_candidates()
        write_json(self.r.directory / '2.0.0.json', {'state': 'done', 'version': '2.0.0',
                                                     'identity': self.remote.identity()})
        for release, candidate in (
                (self.c['release'], absent), (self.c['release'], same), (self.c['release'], later),
                ({**self.c['release'], 'trigger': 'manual'}, same), ({'enabled': False}, same)):
            with self.subTest(release=release.get('trigger', 'off'), candidate=candidate):
                clock = Clock()
                f = Factory(self.root, {**self.c, 'release': release}, self.gh, self.proc)
                with self.assertRaisesRegex(Halt, 'frozen release is unfinished'):
                    f.queue_cards()
                with (patch('sfc_lib.factory.run', return_value=subprocess.CompletedProcess([], 0)),
                      patch('sfc_lib.releases.ReleaseGitHub', return_value=self.remote),
                      patch.object(f, 'fetch', return_value=candidate),
                      patch.object(f, 'queue_cards', return_value=[]) as queue,
                      patch('sfc_lib.priority.create_ticket') as create):
                    with self.assertRaisesRegex(Halt, 'frozen release is unfinished'):
                        f.watch(start=True, clock=clock, wait=clock.wait,
                                overrides={'max_polls': 2, 'poll_interval': 1})
                self.assertTrue(f.processes.stop_path.exists())
                queue.assert_not_called()
                create.assert_not_called()
                self.assertEqual(orphan.read_bytes(), saved)
                self.assertEqual(self.remote.writes + self.gh.writes, [])

    def test_release_creation_can_be_reconciled_under_its_own_stop(self):
        import contextlib
        import io
        import subprocess
        import sfc
        from sfc_lib.ticket_coordination import ticket_directory
        from test_watch import Clock

        self.c['release'].update(version_file='VERSION', trigger='version-file')
        self.f = Factory(self.root, self.c, self.gh, self.proc)
        self.remote.f = self.f
        path = self.r.directory / '1.0.0.json'
        request = 'release-' + '1' * 24
        card = self.remote.card(1)
        card.update(status='Backlog', body=f'Retained release body\n<!-- sfc:ticket:{request} -->')
        self.gh.items[0] = card
        card_identity = {k: card[k] for k in ('id', 'number', 'url')}
        saved = {'state': 'creating', 'version': '1.0.0', 'owner_root': str(self.f.root),
                 'identity': self.remote.identity(), 'request_id': request, 'workflow_kind': 'release',
                 'config_digest': self.f.config_digest}
        write_json(path, saved)
        write_json(ticket_directory(self.root) / (request + '.json'),
                   {'state': 'complete', 'workflow_kind': 'release', 'issue': card_identity,
                    'request_id': request, 'repo': self.c['repo'], 'project': 'p', 'item_id': card['item_id'],
                    'verified': {'id': card['item_id'], 'project': {'id': 'p'}, 'content': card_identity},
                    'title': card['title'], 'body': card['body']})
        clock = Clock()
        output = io.StringIO()
        with (patch('sfc_lib.factory.run', return_value=subprocess.CompletedProcess([], 0)),
              patch('sfc_lib.releases.ReleaseGitHub', return_value=self.remote),
              patch.object(self.f, 'fetch', return_value=self.initial),
              patch.object(self.f, 'queue_cards') as queue,
              patch.object(self.remote, 'identity', wraps=self.remote.identity) as identity,
              patch('sfc_lib.priority.create_ticket') as create,
              patch.object(sfc, 'Factory', return_value=self.f), contextlib.redirect_stdout(output)):
            with self.assertRaisesRegex(Halt, 'frozen release is unfinished') as halted:
                self.f.watch(start=True, clock=clock, wait=clock.wait)
            cli = ['--root', str(self.root), 'release-request', '--version', '1.0.0', '--recover-creation']
            normal_reads = identity.call_count
            with contextlib.redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(sfc.main(cli[:-1]), 1)
            self.assertIn('Stop requested', errors.getvalue())
            self.assertEqual(identity.call_count, normal_reads)
            self.assertIn(' '.join(cli[2:]), str(halted.exception))
            shared = common_dir(self.root) / 'sfc-stop'
            shared.touch()
            with contextlib.redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(sfc.main(cli), 1)
            self.assertIn('operator or unrelated stop', errors.getvalue())
            self.assertEqual(identity.call_count, normal_reads)
            self.assertEqual(json.loads(path.read_text()), saved)
            shared.unlink()
            card_before_stop = self.remote.card

            def concurrent_stop(number):
                card = card_before_stop(number)
                shared.touch()
                return card

            with patch.object(self.remote, 'card', side_effect=concurrent_stop):
                with contextlib.redirect_stderr(io.StringIO()) as errors:
                    self.assertEqual(sfc.main(cli), 1)
                self.assertIn('operator or unrelated stop', errors.getvalue())
                self.assertEqual(json.loads(path.read_text()), saved)
                shared.unlink()
            normal_reads = identity.call_count
            with patch.object(self.f, 'config_digest', 'different'), contextlib.redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(sfc.main(cli), 1)
            self.assertIn('exact retained version, owning checkout and configuration', errors.getvalue())
            self.assertEqual(identity.call_count, normal_reads)
            code = sfc.main(cli)
            self.assertEqual(code, 0)
            queue.assert_not_called()
            create.assert_not_called()
            self.assertGreater(identity.call_count, 0)
        self.assertEqual(json.loads(output.getvalue())['state'], 'planned')
        self.assertEqual(json.loads(path.read_text())['state'], 'planned')
        self.assertTrue(self.f.processes.stop_path.exists())
        self.assertEqual(self.remote.writes + self.gh.writes, [])
        self.assertEqual(self.remote.values, {})

    def test_automatic_release_second_scope_read_is_still_read_only_preflight(self):
        from sfc_lib.rate_limit import ReadConnectionReset
        from sfc_lib.releases import ReleaseTriggerReadReset

        _, candidate = self.version_candidates()
        identity = self.remote.identity()
        with (patch.object(self.f, 'fetch', return_value=candidate),
              patch.object(self.remote, 'identity', side_effect=[identity, ReadConnectionReset('fixture outage')]),
              patch('sfc_lib.priority.create_ticket') as create):
            with self.assertRaises(ReleaseTriggerReadReset):
                self.r.trigger()
        create.assert_not_called()
        self.assertEqual(list(self.r.directory.glob('*.json')), [])
        self.assertEqual(self.remote.writes, [])

    def test_automatic_release_latest_read_reset_before_journal_is_read_only(self):
        from sfc_lib.rate_limit import ReadConnectionReset
        from sfc_lib.releases import ReleaseTriggerReadReset

        _, candidate = self.version_candidates()
        with (patch.object(self.f, 'fetch', return_value=candidate),
              patch.object(self.remote, 'latest', side_effect=ReadConnectionReset('fixture Latest outage')),
              patch('sfc_lib.priority.create_ticket') as create):
            with self.assertRaisesRegex(ReleaseTriggerReadReset, 'fixture Latest outage'):
                self.r.trigger()
        create.assert_not_called()
        self.assertEqual(list(self.r.directory.glob('*.json')), [])
        self.assertEqual(self.remote.writes + self.gh.writes, [])

    def test_automatic_release_read_reset_keeps_unfinished_journal_latched(self):
        import subprocess
        from sfc_lib.rate_limit import ReadConnectionReset
        from test_watch import Clock

        _, candidate = self.version_candidates()
        clock = Clock()
        path = self.r.directory / '2.0.0.json'
        with (patch('sfc_lib.factory.run', return_value=subprocess.CompletedProcess([], 0)),
              patch('sfc_lib.releases.ReleaseGitHub', return_value=self.remote),
              patch.object(self.f, 'fetch', return_value=candidate),
              patch.object(self.f, 'queue_cards') as queue,
              patch('sfc_lib.priority.create_ticket',
                    side_effect=ReadConnectionReset('fixture creation readback outage')) as create):
            with self.assertRaisesRegex(ReadConnectionReset, 'creation readback outage'):
                self.f.watch(start=True, clock=clock, wait=clock.wait)
            self.assertTrue(self.f.processes.stop_path.exists())
            saved = path.read_bytes()
            self.assertEqual(json.loads(saved)['state'], 'creating')

            with patch.object(self.remote, 'identity', side_effect=ReadConnectionReset('fixture initial read outage')):
                with self.assertRaisesRegex(Halt, 'frozen release is unfinished'):
                    self.f.watch(start=True, clock=clock, wait=clock.wait)
            self.assertTrue(self.f.processes.stop_path.exists())

        queue.assert_not_called()
        create.assert_called_once()
        self.assertEqual(path.read_bytes(), saved)
        self.assertEqual(self.remote.writes + self.gh.writes, [])

    def test_non_text_version_leaves_ordinary_backlog_pickup_running(self):
        (self.root / 'VERSION').write_bytes(b'\xff\n')
        git(self.root, 'add', 'VERSION')
        git(self.root, 'commit', '-m', 'Prepare a future release')
        self.assert_pending_trigger_allows_backlog('not valid plain text')

    def test_missing_release_summary_leaves_ordinary_backlog_pickup_running(self):
        self.version_candidates()
        self.c['release'].pop('approved_summaries')
        changes = self.remote.changes

        def private_changes(commits):
            value = changes(commits)
            for pr in value['pull_requests']:
                pr['public'] = False
            return value

        with patch.object(self.remote, 'changes', side_effect=private_changes):
            self.assert_pending_trigger_allows_backlog('approved public release summary')
        self.assertFalse(self.r.directory.exists())

    def test_published_version_leaves_ordinary_backlog_pickup_running(self):
        first, _ = self.version_candidates()
        self.remote.tags['2.0.0'] = first
        self.remote.rows['2.0.0'] = {'tag_name': '2.0.0', 'draft': False, 'published_at': '2026-11-16T00:00:00Z'}
        self.assert_pending_trigger_allows_backlog('current version is already published')

    def test_unpublished_draft_leaves_ordinary_backlog_pickup_running(self):
        self.version_candidates()
        write_json(self.r.directory / '1.0.0.json',
                   {'version': '1.0.0', 'identity': self.remote.identity(), 'state': 'draft-unpublished'})
        self.assert_pending_trigger_allows_backlog('draft is awaiting')

    def test_conflicting_tag_is_not_a_pending_trigger(self):
        self.version_candidates()
        self.remote.tags['2.0.0'] = self.initial
        with patch('sfc_lib.priority.create_ticket') as create, self.assertRaisesRegex(Halt, 'conflicting version/tag'):
            self.r.trigger()
        create.assert_not_called()
        self.assertEqual(self.remote.writes, [])

    def test_pending_draft_preserves_other_release_recovery_and_conflicts(self):
        self.version_candidates()
        identity = self.remote.identity()
        write_json(self.r.directory / '1.0.0.json',
                   {'version': '1.0.0', 'identity': identity, 'state': 'draft-unpublished'})
        unresolved = self.r.directory / '1.1.0.json'
        write_json(unresolved, {'version': '1.1.0', 'identity': identity, 'state': 'creating'})
        with patch('sfc_lib.priority.create_ticket') as create:
            with self.assertRaisesRegex(Halt, 'frozen release is unfinished'):
                self.r.trigger()
            unresolved.unlink()
            self.remote.tags['2.0.0'] = self.initial
            with self.assertRaisesRegex(Halt, 'conflicting version/tag'):
                self.r.trigger()
        create.assert_not_called()
        self.assertEqual(self.remote.writes, [])

    def test_version_file_trigger_is_explicit_and_deduplicated(self):
        self.assertIsNone(self.r.trigger())
        (self.root / 'VERSION').write_text('1.0.0\n')
        git(self.root, 'add', 'VERSION')
        git(self.root, 'commit', '-m', 'Chosen version')
        self.c['release'].update(version_file='VERSION', trigger='version-file')
        f = Factory(self.root, self.c, self.gh, self.proc)
        r = Releases(f, self.remote)
        def create(gh, root, **kwargs):
            self.gh.items[0].update(title=kwargs['title'], body=kwargs['body'], status='Backlog')
            return {'issue': {'number': 1}}
        with patch('sfc_lib.priority.create_ticket', side_effect=create) as creator:
            r.trigger()
            r.trigger()
        self.assertEqual(creator.call_count, 1)
        self.assertEqual(self.gh.items[0]['status'], 'Backlog')

    def version_candidates(self):
        candidates = []
        for version in ('1.0.0', '2.0.0'):
            (self.root / 'VERSION').write_text(version + '\n')
            git(self.root, 'add', 'VERSION')
            git(self.root, 'commit', '-m', 'Choose ' + version)
            candidates.append(git(self.root, 'rev-parse', 'HEAD'))
        self.c['release'].update(version_file='VERSION', trigger='version-file')
        self.f = Factory(self.root, self.c, self.gh, self.proc)
        self.r = Releases(self.f, self.remote)
        return candidates

    def test_trigger_reselects_file_version_if_base_moves_before_request(self):
        first, final = self.version_candidates()
        def create(gh, root, **kwargs):
            self.gh.items[0].update(title=kwargs['title'], body=kwargs['body'], status='Backlog')
            return {'issue': {'number': 1}}
        with patch.object(self.f, 'fetch', side_effect=[first, final, final]), \
                patch('sfc_lib.priority.create_ticket', side_effect=create) as creator:
            self.r.trigger()
        self.assertEqual(creator.call_count, 1)
        self.assertEqual(self.r.journal['version'], '2.0.0')
        self.assertEqual(self.r.journal['candidate'], final)
        self.assertFalse((self.r.directory / '1.0.0.json').exists())

    def test_trigger_rejects_version_change_during_final_candidate_selection(self):
        first, final = self.version_candidates()
        with patch.object(self.f, 'fetch', side_effect=[first, first, final]), \
                patch('sfc_lib.priority.create_ticket') as creator:
            with self.assertRaisesRegex(Halt, 'version changed during candidate selection'):
                self.r.trigger()
        creator.assert_not_called()
        self.assertFalse(self.r.directory.exists())

    def test_explicit_version_still_overrides_candidate_version_file(self):
        first, final = self.version_candidates()
        def create(gh, root, **kwargs):
            self.gh.items[0].update(title=kwargs['title'], body=kwargs['body'], status='Backlog')
            return {'issue': {'number': 1}}
        with patch.object(self.f, 'fetch', side_effect=[first, final]), \
                patch('sfc_lib.priority.create_ticket', side_effect=create):
            self.r.request('3.0.0')
        self.assertEqual(self.r.journal['version'], '3.0.0')
        self.assertEqual(self.r.journal['candidate'], final)

    def scheduler_release(self):
        from test_pickup import PickupGitHub
        self.c['max_attempts'] = 1
        self.gh = PickupGitHub(self.root, self.gh.items)
        self.f = Factory(self.root, self.c, self.gh, self.proc)
        self.remote = Remote(self.f)
        self.r = Releases(self.f, self.remote)
        self.plan()

    def run_scheduler(self, factory):
        import subprocess
        def fake_version(argv, *args, **kwargs):
            if argv[:2] == ['codex', '--version']:
                return subprocess.CompletedProcess(argv, 0, 'fixture', '')
            return run(argv, *args, **kwargs)
        with patch('sfc_lib.factory.run', side_effect=fake_version), \
                patch('sfc_lib.releases.ReleaseGitHub', return_value=self.remote):
            return factory.run()

    def test_automatic_trigger_allows_verified_publication_to_resume_completion(self):
        (self.root / 'VERSION').write_text('1.0.0\n')
        git(self.root, 'add', 'VERSION')
        git(self.root, 'commit', '-m', 'Choose release version')
        self.c['release'].update(version_file='VERSION', trigger='version-file')
        self.scheduler_release()
        with patch.object(Releases, 'complete', side_effect=Stopped('before completion')):
            with self.assertRaises(Stopped):
                self.run_scheduler(self.f)
        saved = self.r.directory / '1.0.0.json'
        self.assertEqual(json.loads(saved.read_text())['state'], 'publication-verified')
        events = copy.deepcopy(self.proc.events)

        f = Factory(self.root, self.c, self.gh, self.proc)
        self.assertEqual(self.run_scheduler(f)['outcome'], 'drained')
        self.assertEqual(json.loads(saved.read_text())['state'], 'done')
        self.assertEqual(self.gh.writes.count((1, 'Done')), 1)
        self.assertEqual(self.remote.writes.count('publish'), 1)
        self.assertEqual(self.proc.events, events)

    def test_scheduler_resumes_completed_upload_at_attempt_limit(self):
        self.scheduler_release()
        upload = self.remote.upload
        def interrupted(release_id, name, path):
            upload(release_id, name, path)
            if name == 'app.bin':
                raise Stopped('upload completed before interruption')
        with patch.object(self.remote, 'upload', side_effect=interrupted):
            with self.assertRaises(Stopped):
                self.run_scheduler(self.f)
        self.assertEqual(self.f.record(self.remote.card(1))['attempts'], 1)
        self.assertEqual(self.remote.card(1)['status'], 'Review')
        before = copy.deepcopy(self.proc.events)
        f = Factory(self.root, self.c, self.gh, self.proc)
        result = self.run_scheduler(f)
        self.assertEqual(result['outcome'], 'drained')
        self.assertEqual(self.remote.card(1)['status'], 'Done')
        self.assertEqual(f.record(self.remote.card(1))['attempts'], 1)
        self.assertEqual(self.remote.writes.count('upload-app.bin'), 1)
        self.assertEqual(self.gh.writes.count((1, 'Done')), 1)
        self.assertEqual(self.proc.events, before)

    def test_scheduler_resumes_prepared_build_without_spending_another_attempt(self):
        self.scheduler_release()
        prepare = Releases.prepare
        def interrupted(releases, card):
            prepare(releases, card)
            raise Stopped('prepared candidate before stage transition')
        with patch.object(Releases, 'prepare', autospec=True, side_effect=interrupted):
            with self.assertRaises(Stopped):
                self.run_scheduler(self.f)
        self.assertEqual(self.remote.card(1)['status'], 'Building')
        checks = copy.deepcopy(self.proc.events)
        f = Factory(self.root, self.c, self.gh, self.proc)
        self.assertEqual(self.run_scheduler(f)['outcome'], 'drained')
        self.assertEqual(self.remote.card(1)['status'], 'Done')
        self.assertEqual(f.record(self.remote.card(1))['attempts'], 1)
        self.assertEqual([e for e in self.proc.events if e[0] == 'check'], checks)
        self.assertEqual(self.proc.lanes, ['qa', 'security', 'review'])

    def interrupted_attempt(self, boundary):
        self.scheduler_release()
        snapshot = {}
        def stop():
            # Retain the actual durable files at this boundary. Restoring them
            # discards exception-handler writes, as an abrupt process death would.
            snapshot.update(factory=self.f.state_path.read_text(), journal=self.r.path.read_text(),
                            activity=copy.deepcopy(self.gh.activities))
            raise Stopped('interrupted reservation persistence: ' + boundary)
        if boundary.startswith('factory'):
            update = self.f.update
            def interrupted(card, **values):
                if 'attempts' in values and boundary == 'factory-before':
                    stop()
                update(card, **values)
                if 'attempts' in values:
                    stop()
            method = patch.object(self.f, 'update', side_effect=interrupted)
        else:
            save = Releases.save
            def interrupted(releases):
                pending = releases.journal['state'] == 'building'
                if pending and boundary == 'journal-before':
                    stop()
                save(releases)
                if pending:
                    stop()
            method = patch.object(Releases, 'save', autospec=True, side_effect=interrupted)
        with method:
            with self.assertRaises(Stopped):
                self.run_scheduler(self.f)
        self.f.state_path.write_text(snapshot['factory'])
        self.r.path.write_text(snapshot['journal'])
        self.gh.activities = snapshot['activity']
        self.assertEqual(self.proc.events, [])
        self.assertEqual(self.remote.writes, [])
        return snapshot

    def resume_attempt(self, boundary):
        self.interrupted_attempt(boundary)
        self.proc.stop_path.unlink(missing_ok=True)
        f = Factory(self.root, self.c, self.gh, self.proc)
        card = self.remote.card(1)
        attempts = 0 if boundary == 'factory-before' else 1
        self.assertEqual(f.record(card)['attempts'], attempts)
        self.assertEqual(json.loads(self.r.path.read_text())['state'],
                         'building' if boundary == 'journal-after' else 'planned')
        writes = copy.deepcopy(self.gh.writes)
        with patch('sfc_lib.releases.ReleaseGitHub', return_value=self.remote):
            f.reconcile([card])
            f.reconcile([card])
        self.assertEqual(f.record(card)['attempts'], attempts)
        self.assertEqual(self.gh.writes, writes)
        self.assertEqual(self.proc.events, [])
        self.assertEqual(self.remote.writes, [])
        self.assertEqual([c['number'] for c in f.plan([card])[0]], [1])
        self.assertEqual(self.run_scheduler(f)['outcome'], 'drained')
        self.assertEqual(self.remote.card(1)['status'], 'Done')
        self.assertEqual(f.record(card)['attempts'], 1)
        self.assertEqual([e[3] for e in self.proc.events if e[0] == 'check'],
                         self.c['release']['package_commands'] + self.c['verify_commands'] +
                         self.c['release']['verify_commands'])
        self.assertEqual(self.proc.lanes, ['qa', 'security', 'review'])
        self.assertEqual(self.remote.writes.count('upload-app.bin'), 1)
        self.assertEqual(len(self.remote.writes), len(set(self.remote.writes)))
        self.assertEqual(self.gh.writes.count((1, 'Done')), 1)
        events, remote_writes = copy.deepcopy(self.proc.events), list(self.remote.writes)
        self.assertEqual(self.run_scheduler(Factory(self.root, self.c, self.gh, self.proc))['outcome'], 'drained')
        self.assertEqual(self.proc.events, events)
        self.assertEqual(self.remote.writes, remote_writes)
        self.assertEqual(self.gh.writes.count((1, 'Done')), 1)

    def test_scheduler_resumes_before_attempt_counter_save(self):
        self.resume_attempt('factory-before')

    def test_scheduler_recovers_attempt_debit_before_building_journal(self):
        self.resume_attempt('factory-after')

    def test_scheduler_recovers_before_building_journal_save(self):
        self.resume_attempt('journal-before')

    def test_scheduler_resumes_after_building_journal_save(self):
        self.resume_attempt('journal-after')

    def test_attempt_recovery_rejects_counter_scope_identity_and_stage_drift(self):
        self.commit()
        snapshot = self.interrupted_attempt('factory-after')
        self.proc.stop_path.unlink(missing_ok=True)
        state, saved = json.loads(snapshot['factory']), json.loads(snapshot['journal'])
        record = state['cards']['1']
        cases = [('counter', {'attempts': n}, {}, {}) for n in (0, 2, True)] + [
            ('reservation', {'release_attempt': {**record['release_attempt'], 'scope': 'stale'}}, {}, {}),
            ('candidate', {}, {'candidate': self.initial}, {}),
            ('membership', {}, {'changes': {}}, {}),
            ('version', {}, {'version': '2.0.0'}, {}),
            ('configuration', {}, {'config_digest': 'stale'}, {}),
            ('task', {}, {'task_digest': 'stale'}, {}),
            ('owner', {}, {'owner_root': '/another-checkout'}, {}),
            ('identity', {}, {'card': {}}, {}),
            ('journal', {}, {'attempt_reservation': {}}, {}),
        ] + [('stage', {}, {}, {'status': stage}) for stage in ('Ready', 'QA', 'Review', 'Blocked', 'Done')]
        original = copy.deepcopy(self.gh.items[0])
        writes = copy.deepcopy(self.gh.writes)
        for name, record_changes, journal_changes, card_changes in cases:
            with self.subTest(name=name, changes=(record_changes, journal_changes, card_changes)):
                changed = copy.deepcopy(state)
                changed['cards']['1'].update(record_changes)
                write_json(self.f.state_path, changed)
                journal = {**saved, **journal_changes}
                write_json(self.r.path, journal)
                self.gh.items[0] = {**original, **card_changes}
                card = self.remote.card(1)
                f = Factory(self.root, self.c, self.gh, self.proc)
                with self.assertRaises(Halt), patch('sfc_lib.releases.ReleaseGitHub', return_value=self.remote):
                    f.reconcile([card])
                self.assertEqual(f.record(card)['attempts'], changed['cards']['1']['attempts'])
                self.assertEqual(json.loads(self.r.path.read_text()), journal)
                self.assertFalse(Releases(f, self.remote).continuing_attempt(card))
                self.assertEqual(self.proc.events, [])
                self.assertEqual(self.gh.writes, writes)
                self.assertEqual(self.remote.writes, [])

    def test_attempt_recovery_never_infers_reservation_from_legacy_planned_counter(self):
        snapshot = self.interrupted_attempt('factory-after')
        self.proc.stop_path.unlink(missing_ok=True)
        state = json.loads(snapshot['factory'])
        del state['cards']['1']['release_attempt']
        write_json(self.f.state_path, state)
        f = Factory(self.root, self.c, self.gh, self.proc)
        card = self.remote.card(1)
        with patch('sfc_lib.releases.ReleaseGitHub', return_value=self.remote):
            f.reconcile([card])
        self.assertEqual(json.loads(self.r.path.read_text())['state'], 'planned')
        self.assertFalse(Releases(f, self.remote).continuing_attempt(card))
        self.assertEqual(f.plan([card])[0], [])
        self.assertEqual(f.record(card)['attempts'], 1)
        self.assertEqual(self.proc.events, [])
        self.assertEqual(self.remote.writes, [])

    def test_active_attempt_reservation_requires_both_matching_records(self):
        snapshot = self.interrupted_attempt('journal-after')
        self.proc.stop_path.unlink(missing_ok=True)
        writes = copy.deepcopy(self.gh.writes)
        for missing in ('factory', 'journal'):
            with self.subTest(missing=missing):
                state, journal = json.loads(snapshot['factory']), json.loads(snapshot['journal'])
                if missing == 'factory':
                    del state['cards']['1']['release_attempt']
                else:
                    del journal['attempt_reservation']
                write_json(self.f.state_path, state)
                write_json(self.r.path, journal)
                f = Factory(self.root, self.c, self.gh, self.proc)
                card = self.remote.card(1)
                with self.assertRaises(Halt), patch('sfc_lib.releases.ReleaseGitHub', return_value=self.remote):
                    f.reconcile([card])
                self.assertFalse(Releases(f, self.remote).continuing_attempt(card))
                self.assertEqual(f.record(card)['attempts'], 1)
                self.assertEqual(self.gh.writes, writes)
                self.assertEqual(self.proc.events, [])
                self.assertEqual(self.remote.writes, [])

    def test_legacy_active_attempt_can_continue_without_another_debit(self):
        snapshot = self.interrupted_attempt('journal-after')
        state, journal = json.loads(snapshot['factory']), json.loads(snapshot['journal'])
        del state['cards']['1']['release_attempt']
        del journal['attempt_reservation']
        write_json(self.f.state_path, state)
        write_json(self.r.path, journal)
        f = Factory(self.root, self.c, self.gh, self.proc)
        self.assertEqual(self.run_scheduler(f)['outcome'], 'drained')
        self.assertEqual(f.record(self.remote.card(1))['attempts'], 1)
        self.assertEqual(self.proc.lanes, ['qa', 'security', 'review'])
        self.assertEqual(self.gh.writes.count((1, 'Done')), 1)

    def test_scheduler_resumes_qa_with_existing_attempt(self):
        self.scheduler_release()
        with patch.object(self.f, 'lane', side_effect=Stopped('before QA dispatch')):
            with self.assertRaises(Stopped):
                self.run_scheduler(self.f)
        self.assertEqual(self.remote.card(1)['status'], 'QA')
        checks = copy.deepcopy(self.proc.events)
        f = Factory(self.root, self.c, self.gh, self.proc)
        self.assertEqual(self.run_scheduler(f)['outcome'], 'drained')
        self.assertEqual(self.remote.card(1)['status'], 'Done')
        self.assertEqual(f.record(self.remote.card(1))['attempts'], 1)
        self.assertEqual([e for e in self.proc.events if e[0] == 'check'], checks)
        self.assertEqual(self.proc.lanes, ['qa', 'security', 'review'])

    def interrupted_stage(self, target, boundary):
        self.scheduler_release()
        source = {'QA': 'building', 'Review': 'qa'}[target]
        if boundary == 'intent':
            move = self.gh.move
            def interrupted(card, status):
                move(card, status)
                if status == target:
                    raise Stopped('stage written before readback')
            method = patch.object(self.gh, 'move', side_effect=interrupted)
        else:
            save = Releases.save
            def interrupted(releases):
                save(releases)
                j = releases.journal
                if (j['state'] == source and
                        j['effects'].get('stage-' + target, {}).get('state') == 'verified'):
                    raise Stopped('stage readback saved before journal advance')
            method = patch.object(Releases, 'save', autospec=True, side_effect=interrupted)
        with method:
            with self.assertRaises(Stopped):
                self.run_scheduler(self.f)
        card = self.remote.card(1)
        self.assertEqual(card['status'], target)
        self.assertEqual(json.loads(self.r.path.read_text())['state'], source)
        self.assertEqual(self.f.record(card)['attempts'], 1)

    def resume_interrupted_stage(self, target, boundary):
        self.interrupted_stage(target, boundary)
        events = copy.deepcopy(self.proc.events)
        writes = copy.deepcopy(self.gh.writes)
        lanes = list(self.proc.lanes)
        f = Factory(self.root, self.c, self.gh, self.proc)
        # Simulate the explicit restart's stop acknowledgement in this fixture.
        self.proc.stop_path.unlink(missing_ok=True)
        with patch('sfc_lib.releases.ReleaseGitHub', return_value=self.remote):
            f.reconcile([self.remote.card(1)])
            f.reconcile([self.remote.card(1)])
        card = self.remote.card(1)
        self.assertEqual(json.loads(self.r.path.read_text())['state'], target.lower())
        self.assertEqual(f.record(card)['status'], target)
        self.assertTrue(Releases(f, self.remote).continuing_attempt(card))
        self.assertEqual([c['number'] for c in f.plan([card])[0]], [1])
        self.assertEqual(self.gh.writes, writes)
        self.assertEqual(self.proc.events, events)
        self.assertEqual(self.proc.lanes, lanes)
        self.assertEqual(self.run_scheduler(f)['outcome'], 'drained')
        self.assertEqual(self.remote.card(1)['status'], 'Done')
        self.assertEqual(f.record(card)['attempts'], 1)
        self.assertEqual([e for e in self.proc.events if e[0] == 'check'],
                         [e for e in events if e[0] == 'check'])
        self.assertEqual(self.proc.lanes, ['qa', 'security', 'review'])
        self.assertEqual(self.gh.writes.count((1, target)), 1)
        self.assertEqual(self.gh.writes.count((1, 'Done')), 1)

    def test_scheduler_recovers_qa_transition_intent_at_attempt_limit(self):
        self.resume_interrupted_stage('QA', 'intent')

    def test_scheduler_recovers_qa_transition_readback_at_attempt_limit(self):
        self.resume_interrupted_stage('QA', 'verified')

    def test_scheduler_recovers_review_transition_intent_at_attempt_limit(self):
        self.resume_interrupted_stage('Review', 'intent')

    def test_scheduler_recovers_review_transition_readback_at_attempt_limit(self):
        self.resume_interrupted_stage('Review', 'verified')

    def test_stage_recovery_requires_owned_intent_and_preserves_manual_moves(self):
        self.interrupted_stage('QA', 'verified')
        self.proc.stop_path.unlink(missing_ok=True)
        saved = json.loads(self.r.path.read_text())
        unowned = copy.deepcopy(saved)
        del unowned['effects']['stage-QA']
        write_json(self.r.path, unowned)
        f = Factory(self.root, self.c, self.gh, self.proc)
        r = Releases(f, self.remote)
        writes = copy.deepcopy(self.gh.writes)
        r.reconcile(self.remote.card(1))
        self.assertEqual(json.loads(self.r.path.read_text())['state'], 'building')
        self.assertEqual(f.plan([self.remote.card(1)])[0], [])
        write_json(self.r.path, saved)
        for stage in ('Building', 'Review', 'Blocked'):
            with self.subTest(stage=stage):
                self.gh.items[0]['status'] = stage
                with self.assertRaisesRegex(Halt, 'readback only'):
                    r.reconcile(self.remote.card(1))
                self.assertEqual(self.remote.card(1)['status'], stage)
                self.assertEqual(json.loads(self.r.path.read_text())['state'], 'building')
        self.assertEqual(self.gh.writes, writes)
        self.assertEqual(f.record(self.remote.card(1))['attempts'], 1)

    def test_stage_recovery_rechecks_journal_identity_and_scope(self):
        self.interrupted_stage('Review', 'intent')
        self.proc.stop_path.unlink(missing_ok=True)
        saved = json.loads(self.r.path.read_text())
        writes = copy.deepcopy(self.gh.writes)
        for changes in ({'config_digest': 'stale'}, {'task_digest': 'stale'},
                        {'owner_root': '/another-checkout'}, {'card': {}},
                        {'effects': {**saved['effects'], 'stage-Review': {
                            **saved['effects']['stage-Review'], 'before': 'Building'}}}):
            with self.subTest(changes=changes):
                write_json(self.r.path, {**saved, **changes})
                f = Factory(self.root, self.c, self.gh, self.proc)
                with self.assertRaises(Halt):
                    Releases(f, self.remote).reconcile(self.remote.card(1))
                self.assertEqual(json.loads(self.r.path.read_text())['state'], 'qa')
                self.assertEqual(f.record(self.remote.card(1))['attempts'], 1)
        self.assertEqual(self.gh.writes, writes)

    def test_stage_recovery_does_not_retry_intent_without_remote_write(self):
        self.scheduler_release()
        move = self.gh.move
        def interrupted(card, status):
            if status == 'QA':
                raise Stopped('before stage write')
            return move(card, status)
        with patch.object(self.gh, 'move', side_effect=interrupted):
            with self.assertRaises(Stopped):
                self.run_scheduler(self.f)
        self.proc.stop_path.unlink(missing_ok=True)
        writes = copy.deepcopy(self.gh.writes)
        f = Factory(self.root, self.c, self.gh, self.proc)
        with self.assertRaisesRegex(Halt, 'readback only'):
            Releases(f, self.remote).reconcile(self.remote.card(1))
        self.assertEqual(self.gh.writes, writes)
        self.assertEqual(self.remote.card(1)['status'], 'Building')
        self.assertEqual(f.record(self.remote.card(1))['attempts'], 1)

    def test_scheduler_never_repeats_interrupted_packaging_at_attempt_limit(self):
        self.scheduler_release()
        with patch.object(self.proc, 'execute', side_effect=Stopped('during packaging')):
            with self.assertRaises(Stopped):
                self.run_scheduler(self.f)
        f = Factory(self.root, self.c, self.gh, self.proc)
        with patch.object(self.proc, 'execute') as execute:
            with self.assertRaisesRegex(Halt, 'previously failed/interrupted'):
                self.run_scheduler(f)
            execute.assert_not_called()
        self.assertEqual(f.record(self.remote.card(1))['attempts'], 1)
        self.assertEqual(self.remote.writes, [])

    def test_continuing_attempt_requires_unchanged_active_journal(self):
        self.scheduler_release()
        with patch.object(self.f, 'lane', side_effect=Stopped('before QA dispatch')):
            with self.assertRaises(Stopped):
                self.run_scheduler(self.f)
        card = self.remote.card(1)
        self.assertTrue(self.r.continuing_attempt(card))
        path = self.r.path
        saved = json.loads(path.read_text())
        for changes in ({'state': 'planned'}, {'state': 'blocked'}, {'state': 'done'},
                        {'config_digest': 'stale'}, {'task_digest': 'stale'},
                        {'owner_root': '/another-checkout'}, {'card': {}}):
            with self.subTest(changes=changes):
                write_json(path, {**saved, **changes})
                selected, waiting = self.f.plan([card])
                self.assertEqual(selected, [])
                self.assertIn('attempt limit', str(waiting))
        write_json(path, saved)
        for changes in ({'title': 'Manually edited'}, {'status': 'Building'}, {'status': 'Ready'}):
            with self.subTest(card=changes):
                self.assertFalse(self.r.continuing_attempt({**card, **changes}))
        self.f.update(card, workflow_kind='code')
        selected, _ = self.f.plan([card])
        self.assertEqual(selected, [])

    def test_scheduler_routes_release_through_shared_bounded_worker_slot(self):
        from test_pickup import PickupGitHub
        gh = PickupGitHub(self.root, self.gh.items)
        self.gh = gh
        self.f = Factory(self.root, self.c, gh, self.proc)
        self.remote = Remote(self.f)
        self.r = Releases(self.f, self.remote)
        self.plan()
        original_run = run
        import subprocess
        def fake_version(argv, *args, **kwargs):
            if argv[:2] == ['codex', '--version']:
                return subprocess.CompletedProcess(argv, 0, 'fixture', '')
            return original_run(argv, *args, **kwargs)
        with patch('sfc_lib.factory.run', side_effect=fake_version), patch('sfc_lib.releases.ReleaseGitHub', return_value=self.remote):
            result = self.f.run()
        self.assertEqual(result['outcome'], 'drained')
        self.assertEqual(self.gh.items[0]['status'], 'Done')
        self.assertEqual(self.proc.lanes, ['qa', 'security', 'review'])
        self.assertEqual(self.f.c['max_workers'], 2)

    def test_missing_github_checks_fail_closed(self):
        card = self.plan()
        self.remote.checks = lambda sha: []
        with self.assertRaisesRegex(Halt, 'checks missing/incomplete/failing'):
            self.process(card)
        self.assertEqual(self.remote.writes, [])

    def test_qa_failure_blocks_without_publication(self):
        card = self.plan()
        self.proc.fail_lane = 'qa'
        self.process(card)
        self.assertEqual(card['status'], 'Blocked')
        self.assertEqual(self.proc.lanes, ['qa'])
        self.assertEqual(self.remote.writes, [])

    def test_filter_must_actually_filter_and_not_ignore_query(self):
        card = self.plan()
        self.remote.filtered = lambda version: [{'id': 'item-1'}]
        with self.assertRaisesRegex(Halt, 'filter was ignored'):
            self.r.assign_members(card)

    def test_trigger_cannot_supply_shell_or_other_version_policy(self):
        for version in ('$(touch nope)', '../../x', '--target=main', '1.2', '1.2.3\nprivate'):
            with self.subTest(version=version), self.assertRaisesRegex(Halt, 'Choose an explicit'):
                self.r.request(version)
        self.assertFalse(self.r.directory.exists())
        self.assertEqual(self.remote.writes, [])

    def test_incomplete_release_creation_never_routes_to_code_build(self):
        self.r.directory.mkdir(parents=True)
        write_json(self.r.directory / '1.0.0.json', {'request_id': 'release-1-0-0', 'state': 'creating'})
        card = self.remote.card(1)
        card['body'] += '\n<!-- sfc:ticket:release-1-0-0 -->'
        with self.assertRaisesRegex(Halt, 'Incomplete release creation'):
            self.r.bind(card)

    def test_initial_existing_version_does_not_write(self):
        self.remote.tags['1.0.0'] = self.initial
        with self.assertRaisesRegex(Halt, 'Existing conflicting version/tag'):
            self.r.request('1.0.0')
        self.assertEqual(self.remote.writes, [])

    def test_new_publication_after_freeze_invalidates_candidate(self):
        self.commit()
        card = self.plan()
        def published(lane, cwd):
            if lane == 'review':
                self.remote.tags['0.9.0'] = self.initial
                self.remote.rows['0.9.0'] = {'id': 99, 'tag_name': '0.9.0', 'draft': False}
        self.proc.on_lane = published
        with self.assertRaisesRegex(Halt, 'source boundary changed'):
            self.process(card)
        self.assertEqual(self.remote.writes, [])

    def test_saved_artifact_receipt_requires_actual_complete_checks(self):
        card = self.plan()
        self.r.move(card, 'Building')
        self.r.prepare(card)
        self.r.journal['artifact_checks']['checks'] = []
        self.r.save()
        with self.assertRaisesRegex(Halt, 'previously failed/interrupted'):
            self.r.prepare(card)

    def test_interrupted_completed_upload_recovers_by_readback_without_more_paid_work(self):
        card = self.plan()
        upload = self.remote.upload
        def interrupted(release_id, name, path):
            upload(release_id, name, path)
            if name == 'app.bin':
                raise Stopped('upload completed before interruption')
        with patch.object(self.remote, 'upload', side_effect=interrupted):
            with self.assertRaises(Stopped):
                self.process(card)
        before = copy.deepcopy(self.proc.events)
        f = Factory(self.root, self.c, self.gh, self.proc)
        f.run_dir = self.f.run_dir
        r = Releases(f, self.remote)
        r.process(self.remote.card(1))
        self.assertEqual(self.remote.writes.count('upload-app.bin'), 1)
        self.assertEqual(self.proc.events, before)
        self.assertEqual(r.journal['state'], 'done')

    def test_unavailable_permission_preserves_pins_without_writing(self):
        card = self.plan()
        self.remote.identity = lambda: (_ for _ in ()).throw(Halt('push permission unavailable'))
        with self.assertRaisesRegex(Halt, 'permission unavailable'):
            self.process(card)
        self.assertEqual(self.remote.writes, [])

    def test_disabling_after_completed_release_does_not_reopen_or_recheck_work(self):
        card = self.plan()
        self.process(card)
        c = {**self.c, 'release': {'enabled': False}}
        f = Factory(self.root, c, self.gh, self.proc)
        r = Releases(f, self.remote)
        self.gh.items[0]['title'] += ' later editorial edit'
        writes = copy.deepcopy(self.remote.writes)
        r.reconcile(self.remote.card(1))
        self.assertEqual(self.remote.writes, writes)

    def test_uncertain_completed_activity_keeps_existing_latch_and_no_duplicate_done(self):
        card = self.plan()
        write_activity = self.gh.write_activity
        def lose_response(card, scope, values):
            write_activity(card, scope, values)
            if 'Completed' in values[0]:
                raise Halt('Completed response lost')
        with patch.object(self.gh, 'write_activity', side_effect=lose_response):
            with self.assertRaisesRegex(Halt, 'Activity reporting halted'):
                self.process(card)
        f = Factory(self.root, self.c, self.gh, self.proc)
        r = Releases(f, self.remote)
        with self.assertRaisesRegex(Halt, 'uncertain'):
            r.reconcile(self.remote.card(1))
        self.assertEqual(self.gh.writes.count((1, 'Done')), 1)
        self.assertNotEqual(r.journal['state'], 'done')


    def test_private_summaries_require_exact_stored_source_and_destination_authority(self):
        from sfc_lib.release_notes import freeze
        card = self.plan()
        j = self.r.journal
        j['changes']['pull_requests'] = [{'number': n, 'merge_commit': self.initial,
                                          'public': False, 'title': '', 'issues': []} for n in (85, 86, 88)]
        with self.assertRaisesRegex(Halt, 'configure release.approved_summaries'):
            freeze(self.root, j, {})
        approval = self.c['release']['approved_summaries']
        frozen = freeze(self.root, j, {}, approval)
        self.assertIn('Provide the initial document reader', frozen['text'])
        self.assertEqual(frozen['source']['approved_summaries']['entries'], approval['entries'])
        approval['entries'][0]['commit'] = 'f' * 40
        with self.assertRaisesRegex(Halt, 'No approved meaningful'):
            freeze(self.root, j, {}, approval)
        approval['repo'] = 'another/private-repo'
        with self.assertRaisesRegex(Halt, 'explicit text authority'):
            validate_config(self.c)
        self.assertEqual(self.remote.writes, [])
        self.assertEqual(card['status'], 'Ready')

    def test_private_summaries_freeze_before_independent_lanes_and_survive_restart(self):
        original = self.remote.changes
        def private(commits):
            changes = original(commits)
            for pr in changes['pull_requests']:
                pr.update(public=False, title='')
            return changes
        self.remote.changes = private
        self.commit()
        card = self.plan()
        with patch.object(self.f, 'lane', side_effect=Stopped('before QA')):
            with self.assertRaises(Stopped):
                self.process(card)
        frozen = copy.deepcopy(self.r.journal['public_notes'])
        self.assertIn('Provide the initial document reader', frozen['text'])
        with patch('sfc_lib.release_notes.freeze', side_effect=AssertionError('no new approval/generation')):
            self.process(self.remote.card(1))
        self.assertEqual(self.r.journal['public_notes'], frozen)
        self.assertEqual(self.remote.rows['1.0.0']['body'], frozen['text'])
        self.assertEqual(self.remote.latest_tag, '1.0.0')
        self.assertEqual(self.proc.lanes, ['qa', 'security', 'review'])

    def test_missing_private_summary_stops_before_packaging_or_paid_review(self):
        self.c['release'].pop('approved_summaries')
        self.f.config_digest = digest(self.c)
        card = self.plan()
        with self.assertRaisesRegex(Halt, 'No approved meaningful'):
            self.process(card)
        self.assertEqual(self.proc.events, [])
        self.assertEqual(self.remote.writes, [])

    def test_preparation_input_failure_leaves_no_creation_intent_to_repair(self):
        self.c['release'].pop('approved_summaries')
        self.f.config_digest = digest(self.c)
        with self.assertRaisesRegex(Halt, 'No approved meaningful'):
            self.r.request('1.0.0')
        self.assertFalse(self.r.path.exists())
        # A later local Latest-policy failure must likewise precede the durable
        # creation intent. No external request has yet been authorized/dispatched.
        self.c['release']['approved_summaries'] = {
            **{k: self.remote.identity()[k] for k in ('repo', 'base_branch', 'host', 'destination')},
            'authorization_id': 'approved-prose',
            'entries': [{'commit': self.initial, 'text': 'Provide the initial document reader'}]}
        self.f.config_digest = digest(self.c)
        self.r = Releases(self.f, self.remote)
        self.remote.rows['unversioned'] = {'id': 4, 'tag_name': 'unversioned', 'draft': False}
        with patch.object(self.r, 'source', return_value=(None, self.initial, self.remote.changes([self.initial]))):
            with self.assertRaisesRegex(Halt, 'ordering unknown'):
                self.r.request('1.0.0')
        self.assertFalse(self.r.path.exists())
        self.assertEqual(self.remote.writes, [])

    def test_latest_policy_handles_stable_backport_prerelease_and_explicit_never(self):
        from sfc_lib.release_latest import freeze
        self.remote.rows['v2.0.0'] = {'id': 7, 'tag_name': 'v2.0.0', 'draft': False, 'prerelease': False}
        self.remote.latest_tag = 'v2.0.0'
        for version, policy, choice in [('v2.0.1', 'newer-stable', 'true'),
                                         ('v1.9.1', 'newer-stable', 'false'),
                                         ('v3.0.0-rc.1', 'newer-stable', 'false'),
                                         ('v3.0.0', 'never', 'false')]:
            with self.subTest(version=version, policy=policy):
                plan = freeze(self.remote, version, policy)
                self.assertEqual(plan['make_latest'], choice)
                self.assertEqual(plan['prerelease'], '-' in version)
                self.assertEqual(plan['before'], {'id': 7, 'tag_name': 'v2.0.0'})
        self.remote.rows['unversioned'] = {'id': 8, 'tag_name': 'unversioned', 'draft': False}
        with self.assertRaisesRegex(Halt, 'ordering unknown'):
            freeze(self.remote, '3.0.0', 'newer-stable')
        self.assertEqual(freeze(self.remote, '3.0.0', 'never')['make_latest'], 'false')

    def test_v0116_promotes_over_v0115_by_frozen_policy(self):
        from sfc_lib.release_latest import freeze
        self.remote.rows['v0.1.15'] = {'id': 15, 'tag_name': 'v0.1.15', 'draft': False, 'prerelease': False}
        self.remote.latest_tag = 'v0.1.15'
        plan = freeze(self.remote, 'v0.1.16', 'newer-stable')
        self.assertEqual(plan['make_latest'], 'true')
        self.assertEqual(plan['before'], {'id': 15, 'tag_name': 'v0.1.15'})

    def test_nonlatest_publication_preserves_observed_latest(self):
        self.c['release']['latest_policy'] = 'never'
        self.remote.rows['0.9.0'] = {'id': 7, 'tag_name': '0.9.0', 'draft': False, 'prerelease': False}
        self.remote.tags['0.9.0'] = self.initial
        self.remote.latest_tag = '0.9.0'
        self.c['release']['approved_summaries']['entries'] = [
            {'commit': self.commit(), 'text': 'Improve the document reader output'}]
        self.f.config_digest = digest(self.c)
        card = self.plan()
        self.process(card)
        self.assertEqual(card['status'], 'Done')
        self.assertEqual(self.remote.latest_tag, '0.9.0')
        self.assertEqual(self.r.journal['latest_plan']['make_latest'], 'false')

    def test_first_stable_never_rejected_before_creation_or_packaging(self):
        self.c['release']['latest_policy'] = 'never'
        self.f.config_digest = digest(self.c)
        with self.assertRaisesRegex(Halt, 'no prior Latest.*newer-stable'):
            self.r.request('1.0.0')
        self.assertFalse(self.r.path.exists())
        self.assertEqual(self.remote.writes, [])
        self.assertEqual(self.proc.events, [])

    def test_saved_first_stable_never_plan_cannot_publish_with_production_transport(self):
        from types import SimpleNamespace
        # Exercise both endpoint behaviors from QA using production latest/publish.
        # A previously frozen unsupported choice must stop before the PATCH.
        self.plan()
        self.r.journal['latest_plan'] = {'policy': 'never', 'make_latest': 'false',
                                         'prerelease': False, 'before': None, 'inventory': []}
        self.r.save()
        adapter = ReleaseGitHub(SimpleNamespace(c=self.c))
        adapter.releases = self.remote.releases
        publish = self.remote.publish
        calls = []
        endpoint_error = False
        def api(path, method=None, body=None):
            calls.append((path, method))
            if path == 'releases/latest':
                if endpoint_error:
                    raise Halt('unknown Latest read')
                return self.remote.release('1.0.0')
            self.assertEqual((path, method), ('releases/1', 'PATCH'))
            publish(1, body['make_latest'])
        adapter.api = api
        with patch.object(self.remote, 'latest', side_effect=adapter.latest), \
                patch.object(self.remote, 'publish', side_effect=adapter.publish):
            for endpoint_error in (False, True):
                with self.subTest(endpoint_error=endpoint_error):
                    with self.assertRaisesRegex(Halt, 'no prior Latest.*newer-stable'):
                        self.process(self.remote.card(1))
                    self.assertNotIn('publish', self.remote.writes)
                    self.assertNotIn('publication', self.r.journal['effects'])
                    self.assertTrue(self.remote.rows['1.0.0']['draft'])
        self.assertEqual(calls, [])
        self.assertEqual(self.proc.lanes, ['qa', 'security', 'review'])

    def test_prerelease_publication_sets_flag_without_promoting_latest(self):
        self.c['release']['latest_policy'] = 'never'
        self.f.config_digest = digest(self.c)
        card = self.plan()
        version = '1.0.0-rc.1'
        self.r.journal['version'] = version
        self.r.path = self.r.directory / (version + '.json')
        self.r.save()
        self.f.update(card, release_version=version)
        self.process(card)
        self.assertEqual(card['status'], 'Done')
        self.assertTrue(self.remote.rows[version]['prerelease'])
        self.assertIsNone(self.remote.latest_tag)

    def test_latest_transport_reads_endpoint_and_does_not_hide_unknown_outcomes(self):
        from types import SimpleNamespace
        adapter = ReleaseGitHub(SimpleNamespace(c={'repo': 'example/app'}))
        adapter.releases = lambda: []
        adapter.api = lambda path: self.fail('No latest endpoint without a full release')
        self.assertIsNone(adapter.latest())
        adapter.releases = lambda: [{'draft': False, 'prerelease': False}]
        adapter.api = lambda path: {'path': path}
        self.assertEqual(adapter.latest(), {'path': 'releases/latest'})
        adapter.api = lambda path: (_ for _ in ()).throw(Halt('unknown Latest read'))
        with self.assertRaisesRegex(Halt, 'unknown Latest read'):
            adapter.latest()

    def test_old_generic_notes_cannot_pass_readiness(self):
        from sfc_lib.release_notes import freeze, validate
        self.plan()
        self.r.journal['public_notes'] = freeze(self.root, self.r.journal, {},
                                               self.c['release']['approved_summaries'])
        self.r.journal['public_notes']['source']['approved_summaries']['entries'] = []
        with self.assertRaisesRegex(Halt, 'No approved meaningful'):
            validate(self.r.journal)

    def test_wrong_latest_after_lost_publication_halts_without_blind_retry(self):
        card = self.plan()
        publish = self.remote.publish
        def incorrect(*args):
            publish(*args)
            self.remote.latest_tag = None
            raise Halt('lost response and wrong Latest')
        with patch.object(self.remote, 'publish', side_effect=incorrect):
            with self.assertRaisesRegex(Halt, 'uncertain'):
                self.process(card)
        op = self.r.journal['effects']['publication']
        self.assertEqual(op['state'], 'intent')
        self.assertEqual(card['status'], 'Review')
        with self.assertRaisesRegex(Halt, 'readback only'):
            self.process(self.remote.card(1))
        self.assertEqual(self.remote.writes.count('publish'), 1)
        self.remote.latest_tag = '1.0.0'  # External reconciliation, never a worker repair.
        self.process(self.remote.card(1))
        self.assertEqual(self.r.journal['state'], 'done')
        self.assertEqual(self.remote.writes.count('publish'), 1)
        self.assertEqual(self.proc.lanes, ['qa', 'security', 'review'])

    def test_interrupted_publication_reconciles_latest_without_another_write(self):
        card = self.plan()
        publish = self.remote.publish
        def interrupted(*args):
            publish(*args)
            raise Stopped('after publication, before readback')
        with patch.object(self.remote, 'publish', side_effect=interrupted):
            with self.assertRaises(Stopped):
                self.process(card)
        self.assertEqual(self.r.journal['effects']['publication']['state'], 'intent')
        self.assertEqual(self.remote.latest_tag, '1.0.0')
        restarted = Releases(self.f, self.remote)
        restarted.process(self.remote.card(1))
        self.assertEqual(restarted.journal['state'], 'done')
        self.assertEqual(self.remote.writes.count('publish'), 1)
        self.assertEqual(self.proc.lanes, ['qa', 'security', 'review'])

    def test_observable_latest_edit_before_publication_is_preserved(self):
        card = self.plan()
        publish = self.r.effect
        def edited(key, *args, **kwargs):
            if key == 'publication':
                self.remote.rows['9.0.0'] = {'id': 7, 'tag_name': '9.0.0', 'draft': False, 'prerelease': False}
                self.remote.latest_tag = '9.0.0'
            return publish(key, *args, **kwargs)
        with patch.object(self.r, 'effect', side_effect=edited):
            with self.assertRaisesRegex(Halt, 'preserve manual edit'):
                self.process(card)
        self.assertEqual(self.remote.latest_tag, '9.0.0')
        self.assertNotIn('publish', self.remote.writes)

    def test_latest_change_during_completion_prevents_done(self):
        card = self.plan()
        with patch.object(self.r, 'complete', side_effect=Stopped('before Done')):
            with self.assertRaises(Stopped):
                self.process(card)
        self.remote.latest_tag = None
        with self.assertRaisesRegex(Halt, 'Latest outcome changed'):
            self.r.complete(self.remote.card(1))
        self.assertNotIn('close', self.remote.writes)

    def test_inventory_heavy_notes_deduplicate_and_account_for_unknown_work(self):
        from sfc_lib.release_notes import freeze
        self.commit()
        self.plan()
        j = copy.deepcopy(self.r.journal)
        commits = [f'{i:040x}' for i in range(1, 43)]
        prs = [{'number': i, 'merge_commit': commits[i], 'issues': [], 'public': True,
                'title': ('feat: Review evidence faster' if i < 3 else f'fix: Correct result {i}')}
               for i in range(1, 20)]
        j['changes'] = {'commits': [{'commit': c, 'pull_requests': []} for c in commits],
                        'pull_requests': prs, 'issues': [], 'unlinked_commits': commits[:1]}
        frozen = freeze(self.root, j, {})
        text = frozen['text']
        self.assertEqual(text.count('Review evidence faster'), 1)
        self.assertIn('/pull/1)', text)
        self.assertIn('/pull/2)', text)
        self.assertIn('### Highlights', text)
        self.assertIn('### Fixes', text)
        self.assertIn('more change(s)', text)
        self.assertIn('purpose is not inferred', text)
        self.assertNotIn(commits[0], text)
        self.assertEqual(len(j['changes']['commits']), 42)
        self.assertNotIn('SFC', text)
        for pr in prs:
            pr['title'] = f'feat: Change {pr["number"]}'
        prs[-1]['title'] = 'fix: Omitted fix'
        balanced = freeze(self.root, j, {})['text']
        self.assertIn('### Fixes', balanced)
        self.assertIn('Omitted fix', balanced)
        self.assertEqual(sum(line.startswith('- ') for line in balanced.splitlines()), 12)

    def test_non_sfc_public_docs_use_only_pinned_content_and_no_private_evidence(self):
        from sfc_lib.release_notes import freeze
        (self.root / 'INSTALL.md').write_text('Requires Python 3.10.\n\n```sh\npip install example-app\n```\n')
        git(self.root, 'add', 'INSTALL.md')
        git(self.root, 'commit', '-m', 'Public installation documentation')
        self.plan()
        j = self.r.journal
        j['worker_prose'] = 'Claims zero vulnerabilities and ten times faster operation'
        j['private_command'] = 'secret --token=private /Users/private'
        frozen = freeze(self.root, j, {'Installation': 'INSTALL.md'})
        (self.root / 'INSTALL.md').write_text('Later unmerged command')
        self.assertEqual(frozen, freeze(self.root, j, {'Installation': 'INSTALL.md'}))
        self.assertIn('pip install example-app', frozen['text'])
        self.assertIn(j['candidate'] + '/INSTALL.md', frozen['text'])
        for unsupported in ('zero vulnerabilities', 'ten times', 'secret', '/Users', 'SFC', 'Later unmerged'):
            self.assertNotIn(unsupported, frozen['text'])
        for title in ('private token=abc', 'Run `secret-command`', '/Users/private/install', 'fake\n### section'):
            j['changes']['pull_requests'][0]['title'] = title
            with self.assertRaisesRegex(Halt, 'No approved meaningful'):
                freeze(self.root, j, {})
        self.assertNotIn('Installation', freeze(self.root, j, {}, self.c['release']['approved_summaries'])['text'])

    def test_notes_freeze_before_lanes_and_resume_without_generation(self):
        self.commit()
        card = self.plan()
        with patch.object(self.f, 'lane', side_effect=Stopped('before QA')):
            with self.assertRaises(Stopped):
                self.process(card)
        frozen = copy.deepcopy(self.r.journal['public_notes'])
        events = copy.deepcopy(self.proc.events)
        with patch('sfc_lib.release_notes.freeze', side_effect=AssertionError('must not generate again')):
            self.process(self.remote.card(1))
        self.assertEqual(self.r.journal['public_notes'], frozen)
        self.assertEqual(self.remote.rows['1.0.0']['body'], frozen['text'])
        self.assertEqual([e for e in self.proc.events if e[0] not in ('qa', 'security', 'review')],
                         [e for e in events if e[0] not in ('qa', 'security', 'review')])
        manifest = json.loads(next(data for data in self.remote.assets.values() if b'"schema": 1' in data))
        self.assertEqual(manifest['public_notes'], frozen)
        self.assertIn(self.initial, self.r.notes())
        self.assertNotIn('All included commits', frozen['text'])

    def test_missing_legacy_notes_halt_without_rewriting_frozen_manifest(self):
        card = self.plan()
        with patch.object(self.f, 'lane', side_effect=Stopped('before QA')):
            with self.assertRaises(Stopped):
                self.process(card)
        manifest = (self.r.artifact_paths() / 'sfc-release-manifest.json').read_bytes()
        self.r.journal.pop('public_notes')
        self.r.save()
        with patch('sfc_lib.release_notes.freeze', side_effect=AssertionError('legacy must stay fixed')):
            with self.assertRaisesRegex(Halt, 'Legacy unreviewed'):
                self.process(self.remote.card(1))
        self.assertEqual((self.r.artifact_paths() / 'sfc-release-manifest.json').read_bytes(), manifest)
        self.assertEqual(self.proc.lanes, [])
        self.assertEqual(self.remote.writes, [])

    def test_tampered_notes_fail_before_publication(self):
        card = self.plan()
        def tamper(lane, cwd):
            if lane == 'review':
                self.r.journal['public_notes']['text'] += 'Unsupported claim'
        self.proc.on_lane = tamper
        with self.assertRaisesRegex(Halt, 'notes/source evidence changed'):
            self.process(card)
        self.assertEqual(self.remote.writes, [])

    def test_unsupported_public_claim_is_reviewed_and_rejection_prevents_publication(self):
        self.commit()
        card = self.plan()
        self.r.journal['changes']['pull_requests'][0]['title'] = 'feat: Eliminate all vulnerabilities'
        self.r.save()
        execute = self.proc.execute

        def reject_claim(argv, cwd, log, timeout, prompt=None, **kwargs):
            code = execute(argv, cwd, log, timeout, prompt, **kwargs)
            if argv[0] == 'codex':
                self.assertIn('Eliminate all vulnerabilities', prompt)
                self.assertIn('"public_notes":', prompt)
                if 'an SFC security worker' in prompt:
                    self.assertIn('Reject unsupported claims', prompt)
                    self.assertIn('assess public metadata for leaks', prompt)
                    output = Path(argv[argv.index('--output-last-message') + 1])
                    write_json(output, {'verdict': 'fail', 'summary': 'Unsupported release claim',
                                        'findings': [{'file': 'value.txt', 'line': 1, 'severity': 'high',
                                                      'message': 'Source does not establish elimination of all vulnerabilities'}],
                                        'evidence': 'Pinned value.txt changes one value; no security evidence supports this title.'})
            return code

        with patch.object(self.proc, 'execute', side_effect=reject_claim):
            self.process(card)
        self.assertEqual(card['status'], 'Blocked')
        self.assertEqual(self.proc.lanes, ['qa', 'security'])
        self.assertEqual(self.remote.writes, [])

    def test_public_note_sources_reject_out_of_range_prs_and_symlink_docs(self):
        from sfc_lib.release_notes import freeze
        self.commit()
        self.plan()
        j = copy.deepcopy(self.r.journal)
        j['changes']['pull_requests'][0]['merge_commit'] = 'f' * 40
        with self.assertRaisesRegex(Halt, 'outside the frozen merged source range'):
            freeze(self.root, j, {})
        (self.root / 'INSTALL.md').symlink_to('value.txt')
        git(self.root, 'add', 'INSTALL.md')
        git(self.root, 'commit', '-m', 'Symlink fixture')
        self.plan()
        with self.assertRaisesRegex(Halt, 'pinned regular file'):
            freeze(self.root, self.r.journal, {'Installation': 'INSTALL.md'})

    def test_description_only_edits_keep_original_evidence_and_assets(self):
        from sfc_lib.release_description import edit
        from sfc_lib.release_notes import sha
        card = self.plan()
        self.process(card)
        original = self.r.path.read_bytes()
        assets, events = copy.deepcopy(self.remote.assets), copy.deepcopy(self.proc.events)
        body = self.remote.rows['1.0.0']['body']
        result = edit(self.r, '1.0.0', 'approved-prose-only', sha(body), 'Approved readable description')
        self.assertEqual(result['state'], 'verified')
        self.assertEqual(self.r.path.read_bytes(), original)
        self.assertEqual(self.remote.assets, assets)
        self.assertEqual(self.proc.events, events)
        edit(self.r, '1.0.0', 'approved-prose-only', sha(body), 'Approved readable description')
        self.assertEqual(self.remote.writes.count('description'), 1)
        self.remote.rows['1.0.0']['body'] = 'Human correction'
        with self.assertRaisesRegex(Halt, 'externally edited'):
            edit(self.r, '1.0.0', 'approved-prose-only', sha(body), 'Approved readable description')
        with self.assertRaisesRegex(Halt, 'edited externally'):
            edit(self.r, '1.0.0', 'new-approval', sha(body), 'Approved new description')
        self.assertEqual(self.remote.rows['1.0.0']['body'], 'Human correction')

    def test_description_edit_concurrent_human_edit_preserved(self):
        from sfc_lib.release_description import edit
        from sfc_lib.release_notes import sha
        self.process(self.plan())
        before = self.remote.rows['1.0.0']['body']
        read = self.remote.release
        reads = []
        def concurrent(version):
            reads.append(version)
            if len(reads) == 2:
                self.remote.rows[version]['body'] = 'Concurrent human edit'
            return read(version)
        with patch.object(self.remote, 'release', side_effect=concurrent):
            with self.assertRaisesRegex(Halt, 'changed after intent'):
                edit(self.r, '1.0.0', 'concurrent-edit', sha(before), 'Approved description')
        self.assertNotIn('description', self.remote.writes)
        self.assertEqual(self.remote.rows['1.0.0']['body'], 'Concurrent human edit')

    def test_description_edit_lost_response_recovers_by_readback(self):
        from sfc_lib.release_description import edit
        from sfc_lib.release_notes import sha
        self.process(self.plan())
        before = self.remote.rows['1.0.0']['body']
        mutate = self.remote.edit_description
        def interrupted(release_id, text):
            mutate(release_id, text)
            raise Stopped('description changed before interruption')
        with patch.object(self.remote, 'edit_description', side_effect=interrupted):
            with self.assertRaises(Stopped):
                edit(self.r, '1.0.0', 'interrupted-edit', sha(before), 'Approved description')
        edit(self.r, '1.0.0', 'interrupted-edit', sha(before), 'Approved description')
        self.assertEqual(self.remote.writes.count('description'), 1)

    def test_description_edit_preserves_human_readback_after_write(self):
        from sfc_lib.release_description import edit
        from sfc_lib.release_notes import sha
        self.process(self.plan())
        before = self.remote.rows['1.0.0']['body']
        mutate = self.remote.edit_description

        def concurrent(release_id, text):
            mutate(release_id, text)
            self.remote.rows['1.0.0']['body'] = 'Human edit after the write'

        with patch.object(self.remote, 'edit_description', side_effect=concurrent):
            with self.assertRaisesRegex(Halt, 'edit uncertain'):
                edit(self.r, '1.0.0', 'readback-race', sha(before), 'Approved description')
        with self.assertRaisesRegex(Halt, 'readback only'):
            edit(self.r, '1.0.0', 'readback-race', sha(before), 'Approved description')
        self.assertEqual(self.remote.rows['1.0.0']['body'], 'Human edit after the write')
        self.assertEqual(self.remote.writes.count('description'), 1)

    def test_manual_description_change_during_publication_halts_without_overwrite(self):
        card = self.plan()
        upload = self.remote.upload
        def interrupted(release_id, name, path):
            upload(release_id, name, path)
            raise Stopped('after first upload')
        with patch.object(self.remote, 'upload', side_effect=interrupted):
            with self.assertRaises(Stopped):
                self.process(card)
        self.remote.rows['1.0.0']['body'] = 'Human draft edit'
        before = copy.deepcopy(self.remote.writes)
        with self.assertRaisesRegex(Halt, 'changed externally'):
            self.process(self.remote.card(1))
        self.assertEqual(self.remote.writes, before)
        self.assertEqual(self.remote.rows['1.0.0']['body'], 'Human draft edit')


class TransportTests(unittest.TestCase):
    def release_routing_fixture(self):
        from sfc_lib.github import GitHub
        from test_pickup import PickupGitHub, ticket

        class Transport(PickupGitHub):
            routing_inventory = GitHub.routing_inventory
            origin = 'https://github.com/example/app.git'

            def __init__(self):
                super().__init__(Path('.'), [ticket(1, status='QA')])
                self.calls = []
                self.project_node = 'p'
                self.push = True
                self.repository_name = self.c['repo']
                self.metadata()
                self.calls.clear()

            def verify_repository(self):
                self.calls.append('repo view')

            def call(self, *args, **kwargs):
                self.calls.append(args[:2])
                if args[:2] == ('api', 'repos/example/app'):
                    return {'full_name': self.repository_name, 'node_id': 'repo-node',
                            'permissions': {'push': self.push}}
                return super().call(*args, **kwargs)

            def graphql(self, query, variables):
                self.calls.append('GraphQL')
                if 'fragment RoutingFields' in query:
                    project = {'id': self.project_node, 'number': self.project['number'],
                               'fields': self.page(self.fields, variables['after'])}
                    return {'repository': {'nameWithOwner': self.repository_name},
                            'repositoryOwner': {'login': self.project['owner'], 'projectV2': project}}
                return super().graphql(query, variables)

        return Transport()

    def test_release_reads_batch_scope_and_recheck_live_card(self):
        from sfc_lib import pickup

        gh = self.release_routing_fixture()
        adapter = ReleaseGitHub(gh)
        expected, = pickup.cards(gh, [1])
        original_count = len(gh.calls)
        gh.calls.clear()
        self.assertEqual(adapter.card(1), expected)
        self.assertEqual(original_count, 8)
        self.assertEqual(gh.calls, ['GraphQL'] * 4)

        gh.items[0]['status'] = 'Blocked'
        self.assertEqual(adapter.card(1)['status'], 'Blocked')
        gh.fields[0]['options'][0]['id'] = 'replacement'
        with self.assertRaisesRegex(Halt, 'Routing status option meanings changed'):
            adapter.card(1)

    def test_target_release_field_read_checks_frozen_routing(self):
        gh = self.release_routing_fixture()
        target = {'id': 'target', 'name': 'Target release', 'dataType': 'TEXT'}
        gh.fields.append(target)
        adapter = ReleaseGitHub(gh)

        self.assertEqual(adapter.fields(), target)
        gh.fields[0]['options'][0]['id'] = 'replacement'
        with self.assertRaisesRegex(Halt, 'Routing status option meanings changed'):
            adapter.fields()

    def test_release_identity_batches_reads_and_keeps_fresh_push_permission(self):
        from sfc_lib.release_github import ReleasePushPermissionMissing

        gh = self.release_routing_fixture()
        adapter = ReleaseGitHub(gh)
        self.assertEqual(adapter.identity()['project'], 'p')
        self.assertEqual(gh.calls, ['GraphQL', ('api', 'repos/example/app')])
        gh.push = False
        with self.assertRaises(ReleasePushPermissionMissing):
            adapter.identity()

    def test_batched_release_scope_checks_all_pages_and_rejects_identity_drift(self):
        gh = self.release_routing_fixture()
        adapter = ReleaseGitHub(gh)
        gh.page_size = 1
        self.assertEqual(adapter.identity()['project'], 'p')
        # One combined read and a continuation for the Priority field.
        self.assertEqual(gh.calls, ['GraphQL', 'GraphQL', ('api', 'repos/example/app')])
        gh.project_node = 'replaced-project'
        with self.assertRaisesRegex(Halt, 'Project identity changed'):
            adapter.identity()
        gh.project_node = 'p'
        gh.repository_name = 'example/other'
        with self.assertRaisesRegex(Halt, 'repository does not match'):
            adapter.identity()

    def test_change_titles_require_public_merged_membership_and_safe_metadata(self):
        from types import SimpleNamespace
        cutoff, later = 'a' * 40, 'b' * 40
        public_base = {'repo': {'full_name': 'example/app', 'private': False}, 'ref': 'main'}
        rows = [{'number': 1, 'merge_commit_sha': cutoff, 'merged_at': 'today',
                 'title': 'fix: Preserve saved results', 'base': public_base},
                {'number': 2, 'merge_commit_sha': later, 'merged_at': 'today',
                 'title': 'feat: Later unmerged work', 'base': public_base},
                {'number': 3, 'merge_commit_sha': cutoff, 'merged_at': None,
                 'title': 'Unmerged work', 'base': public_base},
                {'number': 4, 'merge_commit_sha': cutoff, 'merged_at': 'today',
                 'title': 'private token=example', 'base': public_base},
                {'number': 5, 'merge_commit_sha': cutoff, 'merged_at': 'today',
                 'title': 'Confidential customer details',
                 'base': {**public_base, 'repo': {'full_name': 'example/app', 'private': True}}}]
        gh = SimpleNamespace(c={'repo': 'example/app', 'base_branch': 'main'},
                             graphql=lambda *args: {'repository': {'pullRequest': {
                                 'closingIssuesReferences': {'nodes': [], 'pageInfo': {'hasNextPage': False}}}}})
        adapter = ReleaseGitHub(gh)
        adapter.pages = lambda path: rows
        changes = adapter.changes([cutoff])
        self.assertEqual([p['number'] for p in changes['pull_requests']], [1, 4, 5])
        self.assertEqual(changes['pull_requests'][0]['title'], 'fix: Preserve saved results')
        self.assertEqual([p['title'] for p in changes['pull_requests'][1:]], ['', ''])
        self.assertNotIn('token=', json.dumps(changes))
        self.assertNotIn('Confidential customer', json.dumps(changes))

    def test_binary_download_uses_owned_stop_and_timeout_process(self):
        from types import SimpleNamespace
        calls = []
        def execute(argv, cwd, log, timeout):
            calls.append((argv, cwd, log, timeout))
            Path(log).write_bytes(b'\x00\xff\x80zip')
            return 0
        with tempfile.TemporaryDirectory() as tmp:
            gh = SimpleNamespace(c={'repo': 'example/app'}, root=tmp)
            adapter = ReleaseGitHub(gh)
            adapter.processes = SimpleNamespace(execute=execute)
            file = Path(tmp) / 'download'
            adapter.download_to({'id': 3}, file)
            self.assertEqual(file.read_bytes(), b'\x00\xff\x80zip')
            self.assertEqual(calls[0][3], 120)
            self.assertIn('--hostname', calls[0][0])
            self.assertIn('repos/example/app/releases/assets/3', calls[0][0])

    def test_tag_read_dereferences_annotated_tags_and_requires_commit(self):
        from types import SimpleNamespace
        gh = SimpleNamespace(c={'repo': 'example/app'})
        adapter = ReleaseGitHub(gh)
        adapter.pages = lambda path: [{'ref': 'refs/tags/1.0.0', 'object': {'type': 'tag', 'sha': 'b' * 40}}]
        adapter.api = lambda path: {'object': {'type': 'commit', 'sha': 'a' * 40}}
        self.assertEqual(adapter.tag('1.0.0'), 'a' * 40)
        adapter.api = lambda path: {'object': {'type': 'tree', 'sha': 'a' * 40}}
        with self.assertRaisesRegex(Halt, 'does not point'):
            adapter.tag('1.0.0')

    def test_published_tag_sources_are_batched_and_annotated_tags_are_peeled(self):
        from types import SimpleNamespace
        calls = []

        def graphql(query, variables):
            calls.append(variables)
            return {'repository': {'nameWithOwner': 'example/app', **{
                alias: {'name': ref.removeprefix('refs/tags/'),
                        'prefix': 'refs/tags/',
                        'target': {'__typename': 'Tag' if ref.endswith('.0') else 'Commit', 'oid': 'a' * 40}}
                for alias, ref in variables.items() if alias.startswith('tag')}}}

        adapter = ReleaseGitHub(SimpleNamespace(c={'repo': 'example/app'}, graphql=graphql))
        adapter.api = lambda path: {'object': {'type': 'commit', 'sha': 'b' * 40}}
        self.assertEqual(adapter.tag_sources(['1.0.0', '1.0.1']), {'1.0.0': 'b' * 40, '1.0.1': 'a' * 40})
        self.assertEqual(calls, [{'owner': 'example', 'name': 'app',
                                 'tag0': 'refs/tags/1.0.0', 'tag1': 'refs/tags/1.0.1'}])

    def test_transport_mutations_are_separate_and_never_replace(self):
        from types import SimpleNamespace
        calls = []
        gh = SimpleNamespace(c={'repo': 'example/app'}, origin='https://github.com/example/app',
                             call=lambda *args, **kwargs: calls.append((args, kwargs)))
        adapter = ReleaseGitHub(gh)
        adapter.create_tag('1.0.0', 'a' * 40)
        adapter.create_draft('1.0.0', 'a' * 40, 'fixed notes')
        adapter.upload(9, 'app.zip', Path('/tmp/artifact.zip'))
        adapter.publish(9, 'true')
        self.assertEqual(len(calls), 4)
        self.assertEqual(json.loads(calls[0][1]['input'])['sha'], 'a' * 40)
        self.assertTrue(json.loads(calls[1][1]['input'])['draft'])
        self.assertFalse(json.loads(calls[3][1]['input'])['draft'])
        self.assertEqual(json.loads(calls[3][1]['input'])['make_latest'], 'true')
        self.assertIn('uploads.github.com', calls[2][0][1])
        self.assertFalse(any('DELETE' in args or '--clobber' in args for args, kwargs in calls))


if __name__ == '__main__':
    unittest.main()
