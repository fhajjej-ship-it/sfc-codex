"""Onboarding acceptance through real CLI/setup with temporary repos/fake GitHub."""
import contextlib
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import sfc
from sfc_lib import autostart, efficiency, onboarding, review_policy, scheduler, verification
from sfc_lib.common import Halt, common_dir, exclusive, git, read_json, write_json
from sfc_lib.factory import Factory
from test_priority import TicketGitHub
from test_verification_reuse import contract


def release_source_data(root, variables, repository_id):
    owner, name, ref, path = (variables[k] for k in ('owner', 'name', 'ref', 'path'))
    base = git(root, 'rev-parse', '--verify', ref, check=False)
    target = None
    if base:
        assert ref.startswith('refs/heads/')
        entry = None
        if git(root, '--literal-pathspecs', 'ls-tree', '--full-tree', base, '--', path):
            blob = {'__typename': 'Blob', 'isBinary': False, 'isTruncated': False, 'text': None}
            try:
                blob['text'] = git(root, 'show', base + ':' + path)
            except UnicodeDecodeError:
                blob['isBinary'] = True
            entry = {'object': blob}
        target = {'name': ref.removeprefix('refs/heads/'), 'prefix': 'refs/heads/',
                  'target': {'__typename': 'Commit', 'oid': base, 'file': entry}}
    return {'repository': {'nameWithOwner': owner + '/' + name, 'id': repository_id, 'ref': target}}


class OnboardingGitHub(TicketGitHub):
    repository_id = 'repository-id'
    board_id = 'project-id'
    push_permission = True

    def graphql(self, query, variables):
        if 'file(path:$path)' in query:
            return release_source_data(self.root, variables, self.repository_id)
        return super().graphql(query, variables)

    def call(self, *args, input=None):
        if args == ('api', 'repos/example/app'):
            return {'full_name': self.c['repo'], 'node_id': self.repository_id,
                    'permissions': {'push': self.push_permission}}
        if args[:2] == ('repo', 'view'):
            assert args[2:] == (self.origin, '--json', 'id,nameWithOwner')
            return {'id': self.repository_id, 'nameWithOwner': self.c['repo']}
        if args[:2] == ('project', 'view'):
            assert args[2:] == (1, '--owner', 'example', '--format', 'json')
            return {'id': self.board_id}
        return super().call(*args, input=input)


class OnboardingAcceptanceTests(unittest.TestCase):
    setup_args = ('onboard', '--repo', 'example/app', '--owner', 'example', '--project', '1',
                  '--base', 'main', '--verify', 'python3 -m unittest')
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='sfc-onboarding-75-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        git(self.root, 'init', '-b', 'main')
        git(self.root, 'remote', 'add', 'origin', 'https://github.com/example/app')
        self.gh = OnboardingGitHub(self.root)
        self.gh.origin = self.gh.origin_url(self.root)
        support = patch('sfc_lib.lifeline.supported', return_value=True)
        support.start()
        self.addCleanup(support.stop)

    def cli(self, *args):
        output = io.StringIO()
        with patch.object(sfc, 'GitHub', return_value=self.gh), contextlib.redirect_stdout(output):
            code = sfc.main(['--root', str(self.root), *args])
        return code, json.loads(output.getvalue()) if output.getvalue().strip() else {}

    def start(self, *options, grouped=False):
        saved = self.gh.data['project']['views']['nodes'][0]
        saved['verticalGroupByFields']['nodes'] = [{'id': 'stage-field' if grouped else 'default-status'}]
        return self.cli(*self.setup_args, *options)[1]

    def start_reviewed(self, *options, grouped=False):
        digest = self.preview(*options)['setup_review']['review_digest']
        return self.start(*options, '--expected-review', digest, grouped=grouped)

    def repair_grouping(self):
        # Simulates the externally saved browser step; never a real board write.
        self.gh.data['project']['views']['nodes'][0]['verticalGroupByFields']['nodes'] = [{'id': 'stage-field'}]

    def preview(self, *options):
        return self.cli(*self.setup_args, '--preview', *options)[1]

    def release_file(self):
        path = self.root / 'release.json'
        write_json(path, {'adapter': 'github-releases', 'package_commands': ['python3 tools/package.py'],
                         'verify_commands': ['python3 tools/check_package.py'], 'artifacts': ['dist/app.zip'],
                         'version_file': 'VERSION', 'trigger': 'version-file'})
        return str(path)

    def test_unified_preview_is_read_only_and_exact_choices_are_pinned(self):
        self.commit_release_version()
        options = ('--allow-merge', '--release-config', self.release_file(),
                   '--release-authorization', 'owner-choice')
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        result = self.preview(*options)
        self.assertEqual({p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}, before)
        self.assertEqual(result['setup_review']['identity'], {'host': 'github.com', 'repository': 'example/app',
                                                             'repository_id': 'repository-id', 'project_id': 'project-id'})
        self.assertEqual(self.gh.mutations + self.gh.writes + self.gh.reads, [])
        caps = result['setup_review']['capabilities']
        self.assertEqual(set(caps), {'watcher', 'pickup', 'automatic_merge', 'github_releases',
                                    'test_result_reuse', 'review_efficiency'})
        self.assertEqual(caps['pickup']['stages'], ['Backlog', 'Ready'])
        self.assertTrue(caps['automatic_merge']['allow_merge'])
        self.assertEqual(caps['github_releases']['repository'], 'example/app')
        self.assertEqual(caps['github_releases']['trigger'], 'version-file')
        self.assertEqual(caps['github_releases']['artifacts'], ['dist/app.zip'])
        self.assertTrue(all(v['choices'] and v['fallback'] for v in caps.values()))
        digest = result['setup_review']['review_digest']
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.cli('onboard', '--repo', 'example/app', '--owner', 'example',
                '--project', '1', '--base', 'main', '--verify', 'python3 -m unittest',
                '--expected-review', digest, '--merge-policy', 'human')[0], 1)
        self.assertFalse((self.root / '.sfc/config.json').exists())
        result = self.start(*options, '--expected-review', digest, grouped=True)
        self.assertTrue(result['autostart']['scope']['allow_merge'])
        self.assertEqual(result['setup_review']['capabilities']['github_releases']['status'], 'active')
        self.assertFalse((self.root / '.sfc/state.json').exists())
        self.assertEqual(read_json(self.root / '.sfc/config.json')['release']['authority']['mode'], 'publish')

    def test_release_preview_discloses_missing_push_permission(self):
        options = ('--release-config', self.release_file(), '--release-authorization', 'owner-choice')
        self.gh.push_permission = False
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        preview = self.preview(*options)
        cap = preview['setup_review']['capabilities']['github_releases']
        self.assertEqual(cap['status'], 'pending')
        self.assertIn('push permission is required', cap['reason'])
        self.assertEqual({p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}, before)
        self.assertEqual(self.gh.mutations + self.gh.writes, [])

        # Access is part of the reviewed eligibility; changing it requires a new preview.
        digest = preview['setup_review']['review_digest']
        self.gh.push_permission = True
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.cli(*self.setup_args, *options, '--expected-review', digest)[0], 1)
        self.assertFalse((self.root / '.sfc/config.json').exists())
        self.gh.push_permission = False
        applied = self.start(*options, '--expected-review', digest, grouped=True)
        with patch.object(sfc, 'run', return_value=SimpleNamespace(returncode=0)):
            doctor = self.cli('doctor')[1]
        for result in (applied, doctor):
            self.assertEqual(result['setup_review']['capabilities']['github_releases'], cap)

    def test_release_readiness_checks_version_file_on_the_selected_merged_base(self):
        self.commit_release_version()
        committed = git(self.root, 'rev-parse', 'refs/heads/main')
        git(self.root, 'rm', 'VERSION')
        git(self.root, 'commit', '-m', 'No release version on merged base')
        # An unrelated local branch and working file must not make main ready.
        git(self.root, 'switch', '-c', 'work-in-progress', committed)
        options = ('--release-config', self.release_file(), '--release-authorization', 'owner-choice')
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        preview = self.preview(*options)
        cap = preview['setup_review']['capabilities']['github_releases']
        self.assertEqual(cap['status'], 'pending')
        self.assertIn('version file is missing', cap['reason'])
        self.assertIn('main', cap['fallback'])
        self.assertIn('VERSION', cap['fallback'])
        self.assertEqual({p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}, before)

        result = self.start(*options, '--expected-review', preview['setup_review']['review_digest'], grouped=True)
        self.assertEqual(result['setup_review']['capabilities']['github_releases'], cap)
        with patch.object(sfc, 'run', return_value=SimpleNamespace(returncode=0)):
            self.assertEqual(self.cli('doctor')[1]['setup_review']['capabilities']['github_releases'], cap)
        self.assertEqual(self.cli('setup-defaults')[1]['setup_review']['capabilities']['github_releases'], cap)
        self.assertEqual(read_json(self.root / '.sfc/config.json')['release']['trigger'], 'version-file')

        git(self.root, 'switch', 'main')
        self.commit_release_version('unreleased')
        cap = self.cli('setup-defaults')[1]['setup_review']['capabilities']['github_releases']
        self.assertEqual(cap['status'], 'pending')
        self.assertIn('does not contain a supported release version', cap['reason'])
        self.commit_release_version('v2.1.0-rc.1')
        self.assertEqual(self.cli('setup-defaults')[1]['setup_review']['capabilities']['github_releases']['status'], 'active')

    def test_release_readiness_reports_retained_creation_before_version_preflight(self):
        self.commit_release_version()
        options = ('--release-config', self.release_file(), '--release-authorization', 'owner-choice')
        self.start_reviewed(*options, grouped=True)
        path = common_dir(self.root) / 'sfc-releases' / '1.0.0.json'
        write_json(path, {'state': 'creating', 'version': '1.0.0'})
        before = path.read_bytes()
        with patch.object(sfc, 'run', return_value=SimpleNamespace(returncode=0)):
            for command in ('doctor', 'setup-defaults'):
                cap = self.cli(command)[1]['setup_review']['capabilities']['github_releases']
                self.assertEqual(cap['status'], 'pending')
                self.assertIn('release-request --version 1.0.0', cap['reason'] + cap['fallback'])
        self.assertEqual(path.read_bytes(), before)

    def commit_release_version(self, version='1.0.0'):
        git(self.root, 'config', 'user.name', 'Fixture')
        git(self.root, 'config', 'user.email', 'fixture@example.test')
        (self.root / 'VERSION').write_text(version + '\n')
        git(self.root, 'add', 'VERSION')
        git(self.root, 'commit', '-m', 'Set version on selected base')

    def test_same_release_file_preserves_reviewed_grant_after_preflight_normalization(self):
        self.commit_release_version()
        self.efficiency_layout()
        (self.root / 'scripts/verify_release_package.py').touch()
        source = self.release_file()
        verifier = 'python3 scripts/verify_release_package.py'
        original = {**read_json(source), 'verify_commands': [verifier]}
        write_json(source, original)
        self.start_reviewed('--no-auto-start', '--release-config', source,
                            '--release-authorization', 'owner-choice', grouped=True)
        config_path = self.root / '.sfc/config.json'
        saved = read_json(config_path)['release']
        self.assertEqual((saved['preflight_commands'], saved['verify_commands']), ([verifier], []))
        options = ('setup-defaults', '--release-config', source)
        code, preview = self.cli(*options)
        self.assertEqual(code, 0)
        self.assertEqual(preview['proposed_config']['release'], saved)
        cap = preview['setup_review']['capabilities']['github_releases']
        self.assertEqual((cap['status'], cap['effective_trigger']), ('active', 'version-file'))
        code, applied = self.cli(*options, '--apply', '--expected-digest', preview['config_digest'],
                                 '--expected-proposed-digest', preview['proposed_digest'])
        self.assertEqual(code, 0)
        self.assertEqual(read_json(config_path)['release'], saved)
        self.assertEqual(applied['setup_review']['capabilities']['github_releases'], cap)

        write_json(source, {**original, 'artifacts': ['dist/new.zip']})
        changed = self.cli(*options)[1]['proposed_config']['release']
        self.assertNotIn('authority', changed)
        self.assertEqual(changed['trigger'], 'manual')

    def test_new_grants_require_the_review_digest_before_setup_writes(self):
        release = ('--release-config', self.release_file(), '--release-authorization', 'owner-choice')
        for options in (('--allow-merge',), (*release, '--release-mode', 'publish'),
                        (*release, '--release-mode', 'draft')):
            with self.subTest(options=options), patch.object(self.gh, 'onboarding_identity') as read_scope, \
                    patch.object(efficiency, 'install_runtimes') as install, contextlib.redirect_stderr(io.StringIO()) as error:
                self.assertEqual(self.cli(*self.setup_args, *options)[0], 1)
                self.assertIn('--expected-review', error.getvalue())
                read_scope.assert_not_called()
                install.assert_not_called()
                self.assertFalse((self.root / '.sfc').exists())
                self.assertFalse(autostart.paths(self.root)[1].exists())
                self.assertEqual(self.gh.mutations + self.gh.writes, [])

    def test_stale_review_rejects_replaced_project_or_repository_before_mutation(self):
        options = ('--allow-merge', '--release-config', self.release_file(),
                   '--release-authorization', 'owner-choice')
        digest = self.preview(*options)['setup_review']['review_digest']
        for attribute in ('board_id', 'repository_id'):
            with self.subTest(attribute=attribute), patch.object(self.gh, attribute, 'replacement-node'), \
                    patch.object(efficiency, 'install_runtimes') as install, contextlib.redirect_stderr(io.StringIO()) as error:
                self.assertEqual(self.cli(*self.setup_args, *options, '--expected-review', digest)[0], 1)
                self.assertIn('Setup review changed', error.getvalue())
                install.assert_not_called()
                self.assertFalse((self.root / '.sfc').exists())
                self.assertFalse(autostart.paths(self.root)[1].exists())
                self.assertEqual(self.gh.mutations + self.gh.writes, [])

    def test_review_precedes_routing_setup_and_field_creation_targets_reviewed_node(self):
        stage = next(field for field in self.gh.fields if field['id'] == 'stage-field')
        self.gh.fields.remove(stage)
        options = ('--no-auto-start', '--release-config', self.release_file(),
                   '--release-authorization', 'owner-choice')
        digest = self.preview(*options)['setup_review']['review_digest']
        self.assertEqual(self.gh.reads + self.gh.mutations, [])
        graphql = self.gh.graphql
        writes = []

        def stage_creation(query, variables):
            if 'createProjectV2Field' not in query:
                return graphql(query, variables)
            writes.append(variables['input'])
            self.gh.fields.append(stage)
            return {'createProjectV2Field': {'projectV2Field': {'id': stage['id']}}}

        with patch.object(self.gh, 'graphql', side_effect=stage_creation), \
                patch.object(self.gh, 'configure_dashboard', return_value={'ready': True}):
            self.assertEqual(self.cli(*self.setup_args, *options, '--expected-review', digest)[0], 0)
        mutation, = writes
        self.assertEqual(mutation['projectId'], 'project-id')
        self.assertEqual([option['name'] for option in mutation['singleSelectOptions']],
                         [option['name'] for option in stage['options']])

    def test_project_drift_before_config_save_prevents_publication_grant(self):
        options = ('--release-config', self.release_file(), '--release-authorization', 'owner-choice')
        digest = self.preview(*options)['setup_review']['review_digest']
        metadata = self.gh.metadata

        def drift_after_metadata(**kwargs):
            record = metadata(**kwargs)
            self.gh.board_id = 'different-project-id'
            return record

        with patch.object(self.gh, 'metadata', side_effect=drift_after_metadata), \
                contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertEqual(self.cli(*self.setup_args, *options, '--expected-review', digest)[0], 1)
        self.assertIn('identity changed', error.getvalue())
        self.assertFalse((self.root / '.sfc').exists())
        self.assertFalse(autostart.paths(self.root)[1].exists())
        self.assertEqual(self.gh.mutations + self.gh.writes, [])

    def test_project_drift_during_dashboard_read_prevents_watcher_grant(self):
        self.repair_grouping()
        dashboard = self.gh.dashboard

        def drift_after_dashboard():
            report = dashboard()
            self.assertTrue(report['ready'])
            self.gh.board_id = 'different-project-id'
            return report

        with patch.object(self.gh, 'dashboard', side_effect=drift_after_dashboard), \
                contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertFalse(self.start_reviewed('--allow-merge', grouped=True))
        self.assertIn('identity changed', error.getvalue())
        receipt = read_json(self.root / '.sfc/onboarding.json')
        self.assertEqual(receipt['reviewed_identity']['project_id'], 'project-id')
        self.assertFalse(receipt['watch_applied'])
        self.assertFalse(autostart.paths(self.root)[1].exists())
        self.assertEqual(self.gh.mutations + self.gh.writes, [])

    def test_pending_finish_rechecks_reviewed_identity_at_watcher_grant(self):
        self.start_reviewed('--allow-merge')
        self.repair_grouping()
        receipt_path = self.root / '.sfc/onboarding.json'
        receipt = receipt_path.read_bytes()
        dashboard = self.gh.dashboard

        def drift_after_dashboard():
            report = dashboard()
            self.gh.repository_id = 'different-repository-id'
            return report

        with patch.object(self.gh, 'dashboard', side_effect=drift_after_dashboard), \
                contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertEqual(self.cli('onboard-finish')[0], 1)
        self.assertIn('identity changed', error.getvalue())
        self.assertEqual(receipt_path.read_bytes(), receipt)
        self.assertFalse(autostart.paths(self.root)[1].exists())
        self.assertEqual(self.gh.mutations + self.gh.writes, [])

    def test_pending_legacy_merge_choice_still_requires_exact_saved_routing(self):
        self.start_reviewed('--allow-merge')
        self.repair_grouping()
        path = self.root / '.sfc/onboarding.json'
        receipt = read_json(path)
        receipt.pop('reviewed_identity')
        write_json(path, receipt)
        field = next(field for field in self.gh.fields if field['id'] == 'stage-field')
        original_id = field['id']
        dashboard = self.gh.dashboard
        dashboard_reads = 0

        def drift_after_dashboard():
            nonlocal dashboard_reads
            report = dashboard()
            dashboard_reads += 1
            if dashboard_reads == 2:
                field['id'] = 'different-routing-field'
            return report

        with patch.object(self.gh, 'dashboard', side_effect=drift_after_dashboard), \
                contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertEqual(self.cli('onboard-finish')[0], 1)
        self.assertIn('Project routing changed', error.getvalue())
        self.assertEqual(read_json(path), receipt)
        self.assertFalse(autostart.paths(self.root)[1].exists())
        field['id'] = original_id
        self.gh._meta = None
        result = self.cli('onboard-finish')[1]
        self.assertTrue(result['autostart']['scope']['allow_merge'])
        self.assertTrue(read_json(path)['watch_applied'])

    def test_missing_version_file_is_visible_before_authorizing_and_keeps_other_setup_active(self):
        path = self.release_file()
        payload = read_json(path)
        payload.pop('version_file')
        write_json(path, payload)
        options = ('--release-config', path, '--allow-merge')
        for authority in ((), ('--release-authorization', 'owner-choice')):
            with self.subTest(authority=authority):
                preview = self.preview(*options, *authority)
                release = preview['setup_review']['capabilities']['github_releases']
                self.assertEqual((release['status'], release['trigger'], release['effective_trigger']),
                                 ('pending', 'version-file', 'manual'))
                self.assertIn('version_file', release['reason'])
                self.assertNotIn('authority', preview['proposed_config']['release'])
                self.assertFalse((self.root / '.sfc/config.json').exists())
        applied = self.start(*options, '--release-authorization', 'owner-choice',
                             '--expected-review', preview['setup_review']['review_digest'], grouped=True)
        caps = applied['setup_review']['capabilities']
        self.assertEqual(caps['github_releases'], release)
        self.assertEqual(caps['automatic_merge']['status'], 'active')
        self.assertEqual(self.cli('setup-defaults')[1]['setup_review']['capabilities']['github_releases'], release)

    def test_missing_release_authority_keeps_only_release_trigger_pending(self):
        result = self.start('--release-config', self.release_file(), grouped=True)
        caps = result['setup_review']['capabilities']
        self.assertEqual(caps['watcher']['status'], 'active')
        self.assertEqual(caps['github_releases']['status'], 'pending')
        self.assertEqual(caps['github_releases']['trigger'], 'version-file')
        self.assertEqual(caps['github_releases']['effective_trigger'], 'manual')
        self.assertNotIn('authority', read_json(self.root / '.sfc/config.json')['release'])
        self.assertEqual(caps['automatic_merge']['status'], 'pending')
        self.assertEqual(self.cli('setup-defaults')[1]['setup_review']['capabilities']['github_releases'],
                         caps['github_releases'])

    def test_partial_release_scope_keeps_other_reviewed_setup_available(self):
        path = self.release_file()
        payload = read_json(path)
        payload.pop('package_commands')
        write_json(path, payload)
        options = ('--release-config', path, '--release-authorization', 'owner-choice', '--allow-merge')
        preview = self.preview(*options)
        release = preview['setup_review']['capabilities']['github_releases']
        self.assertEqual((release['status'], release['trigger'], release['artifacts']),
                         ('pending', 'version-file', payload['artifacts']))
        self.assertIn('release.package_commands', release['reason'])
        self.assertNotIn('release', preview['proposed_config'])
        result = self.start(*options, '--expected-review', preview['setup_review']['review_digest'], grouped=True)
        self.assertEqual(result['setup_review']['capabilities']['automatic_merge']['status'], 'active')
        self.assertEqual(result['setup_review']['capabilities']['github_releases'], release)
        with patch.object(sfc, 'run', return_value=SimpleNamespace(returncode=0)):
            self.assertEqual(self.cli('doctor')[1]['setup_review']['capabilities']['github_releases'], release)

    def test_pickup_can_be_disabled_independently_and_survives_upgrade(self):
        preview = self.preview('--no-pickup')
        self.assertEqual(preview['setup_review']['capabilities']['pickup']['status'], 'off')
        result = self.start('--no-pickup', '--expected-review', preview['setup_review']['review_digest'], grouped=True)
        self.assertEqual(result['setup_review']['capabilities']['watcher']['status'], 'active')
        self.assertFalse(result['autostart']['scope']['watch']['pickup'])
        upgrade = self.cli('setup-defaults')[1]
        self.assertEqual(upgrade['setup_review']['capabilities']['pickup']['status'], 'off')
        self.assertFalse(upgrade['proposed_config']['watch']['pickup'])
        self.assertEqual(self.cli('setup-defaults', '--apply', '--expected-digest', upgrade['config_digest'],
                                  '--expected-proposed-digest', upgrade['proposed_digest'])[0], 0)
        with patch.object(sfc, 'run', return_value=SimpleNamespace(returncode=0)):
            caps = self.cli('doctor')[1]['setup_review']['capabilities']
        self.assertEqual((caps['watcher']['status'], caps['pickup']['status']), ('active', 'off'))

    def test_all_explicit_off_overrides_survive_doctor_and_upgrade(self):
        result = self.start('--no-auto-start', '--merge-policy', 'human', '--release-mode', 'off',
                            '--efficiency-profile', 'off', '--review-efficiency', 'baseline', grouped=True)
        self.assertTrue(all(v['status'] == 'off' for v in result['setup_review']['capabilities'].values()))
        before = (self.root / '.sfc/config.json').read_bytes()
        preview = self.cli('setup-defaults')[1]
        self.assertEqual((self.root / '.sfc/config.json').read_bytes(), before)
        self.assertEqual(preview['proposed_config']['release'], {'enabled': False})
        self.cli('setup-defaults', '--apply', '--expected-digest', preview['config_digest'],
                 '--expected-proposed-digest', preview['proposed_digest'])
        self.assertEqual((self.root / '.sfc/config.json').read_bytes(), before)
        with patch.object(sfc, 'run', return_value=SimpleNamespace(returncode=0)):
            result = self.cli('doctor')[1]
        self.assertTrue(all(v['status'] == 'off' for v in result['setup_review']['capabilities'].values()))

    def test_draft_mode_and_authority_are_explicit_and_upgrade_preserves_them(self):
        self.start_reviewed('--allow-merge', '--release-config', self.release_file(),
                   '--release-mode', 'draft', '--release-authorization', 'draft-only', grouped=True)
        auth_path = autostart.paths(self.root)[1]
        before = auth_path.read_bytes()
        preview = self.cli('setup-defaults')[1]
        self.cli('setup-defaults', '--apply', '--expected-digest', preview['config_digest'],
                 '--expected-proposed-digest', preview['proposed_digest'])
        self.assertEqual(auth_path.read_bytes(), before)
        self.assertEqual(read_json(self.root / '.sfc/config.json')['release']['authority']['mode'], 'draft')
        implicit_mode = self.cli('setup-defaults', '--release-config', self.release_file(),
                                 '--release-authorization', 'reviewed-draft')[1]
        self.assertEqual(implicit_mode['proposed_config']['release']['authority']['mode'], 'draft')
        # A separate explicit review can opt into publication without re-onboarding
        # or replacing the existing watcher authority.
        options = ('--release-config', self.release_file(), '--release-mode', 'publish',
                   '--release-authorization', 'reviewed-upgrade')
        preview = self.cli('setup-defaults', *options)[1]
        self.assertEqual(read_json(self.root / '.sfc/config.json')['release']['authority']['mode'], 'draft')
        self.cli('setup-defaults', *options, '--apply', '--expected-digest', preview['config_digest'],
                 '--expected-proposed-digest', preview['proposed_digest'])
        self.assertEqual(read_json(self.root / '.sfc/config.json')['release']['authority']['mode'], 'publish')
        self.assertEqual(auth_path.read_bytes(), before)

    def test_resupplying_unchanged_release_scope_keeps_authority_without_new_grant(self):
        self.commit_release_version()
        source = self.release_file()
        self.start_reviewed('--release-config', source, '--release-authorization', 'existing-release-grant',
                            grouped=True)
        config_path = self.root / '.sfc/config.json'
        before = read_json(config_path)['release']
        auth_path = autostart.paths(self.root)[1]
        watcher_authority = auth_path.read_bytes()
        options = ('setup-defaults', '--release-config', source)
        code, preview = self.cli(*options)
        self.assertEqual(code, 0)
        self.assertEqual(preview['proposed_config']['release'], before)
        self.assertFalse(preview['setup_review']['applied'])
        code, applied = self.cli(*options, '--apply', '--expected-digest', preview['config_digest'],
                                 '--expected-proposed-digest', preview['proposed_digest'])
        self.assertEqual(code, 0)
        self.assertEqual(applied['setup_review']['applied'], applied['applied'])
        self.assertEqual(read_json(config_path)['release'], before)
        self.assertEqual(auth_path.read_bytes(), watcher_authority)
        cap = applied['setup_review']['capabilities']['github_releases']
        self.assertEqual((cap['status'], cap['mode'], cap['effective_trigger']), ('active', 'publish', 'version-file'))
        self.assertEqual(cap['authority'], before['authority'])

        changed = read_json(Path(source))
        changed['artifacts'] = ['dist/changed.zip']
        write_json(Path(source), changed)
        code, preview = self.cli(*options)
        self.assertEqual(code, 0)
        proposed = preview['proposed_config']['release']
        self.assertNotIn('authority', proposed)
        self.assertEqual(proposed['trigger'], 'manual')
        self.assertEqual(read_json(config_path)['release'], before)

    def test_doctor_reports_missing_push_permission_as_release_pending(self):
        self.start_reviewed('--allow-merge', '--release-config', self.release_file(),
                            '--release-authorization', 'owner-grant', grouped=True)
        self.gh.push_permission = False
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        with patch.object(sfc, 'run', return_value=SimpleNamespace(returncode=0)):
            code, result = self.cli('doctor')
        self.assertEqual(code, 0)
        caps = result['setup_review']['capabilities']
        self.assertEqual((caps['watcher']['status'], caps['pickup']['status']), ('active', 'active'))
        self.assertEqual(caps['github_releases']['status'], 'pending')
        self.assertIn('push permission', caps['github_releases']['reason'])
        self.assertIn('credential with push permission', caps['github_releases']['fallback'])
        self.assertEqual({p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}, before)

    def test_release_upgrade_review_cannot_follow_replaced_repository(self):
        self.start(grouped=True)
        options = ('--release-config', self.release_file(), '--release-authorization', 'reviewed-upgrade')
        preview = self.cli('setup-defaults', *options)[1]
        config_path = self.root / '.sfc/config.json'
        before = config_path.read_bytes()
        self.gh.repository_id = 'replacement-repository'
        with contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertEqual(self.cli('setup-defaults', *options, '--apply',
                                      '--expected-digest', preview['config_digest'], '--expected-proposed-digest',
                                      preview['proposed_digest'])[0], 1)
        self.assertIn('preview is stale', error.getvalue())
        self.assertEqual(config_path.read_bytes(), before)

    def test_legacy_upgrade_reads_release_push_permission_without_watcher_receipts(self):
        release = read_json(Path(self.release_file()))
        release.update(enabled=True, authority={'authorization_id': 'legacy', 'repo': 'example/app',
                       'base_branch': 'main', 'host': 'github.com', 'destination': 'github-releases',
                       'mode': 'publish'})
        write_json(self.root / '.sfc/config.json', {**self.gh.c, 'release': release})
        self.assertFalse(autostart.paths(self.root)[1].exists())
        self.assertFalse((self.root / '.sfc/onboarding.json').exists())
        self.gh.push_permission = False
        code, preview = self.cli('setup-defaults')
        self.assertEqual(code, 0)
        release = preview['setup_review']['capabilities']['github_releases']
        self.assertEqual(release['status'], 'pending')
        self.assertIn('push permission is required', release['reason'])

    def test_upgrade_preview_and_completion_preserve_pending_draft_and_trigger(self):
        self.start(grouped=True)
        options = ('--release-config', self.release_file(), '--release-mode', 'draft')
        before = (self.root / '.sfc/config.json').read_bytes()
        auth_path = autostart.paths(self.root)[1]
        authority = auth_path.read_bytes()
        code, preview = self.cli('setup-defaults', *options)
        self.assertEqual(code, 0)
        self.assertEqual((self.root / '.sfc/config.json').read_bytes(), before)
        code, applied = self.cli('setup-defaults', *options, '--apply',
                                 '--expected-digest', preview['config_digest'],
                                 '--expected-proposed-digest', preview['proposed_digest'])
        self.assertEqual(code, 0)
        with patch.object(sfc, 'run', return_value=SimpleNamespace(returncode=0)):
            doctor = self.cli('doctor')[1]
        for result in (preview, applied, doctor, self.cli('setup-defaults')[1]):
            release = result['setup_review']['capabilities']['github_releases']
            self.assertEqual((release['status'], release['mode'], release['trigger'], release['effective_trigger']),
                             ('pending', 'draft', 'version-file', 'manual'))
        saved = read_json(self.root / '.sfc/config.json')['release']
        self.assertEqual(saved['trigger'], 'manual')
        self.assertNotIn('authority', saved)
        self.assertEqual(auth_path.read_bytes(), authority)
        self.assertEqual(doctor['capabilities']['watcher']['status'], 'needs-inspection')
        for result in (preview, applied, doctor):
            setup = result['setup_review']['capabilities']
            for key in ('watcher', 'pickup'):
                self.assertEqual(setup[key]['status'], 'pending')
                self.assertIn('re-arming', setup[key]['reason'])
        config_path = self.root / '.sfc/config.json'
        config = read_json(config_path)
        config['verify_timeout'] += 1
        write_json(config_path, config)
        with patch.object(sfc, 'run', return_value=SimpleNamespace(returncode=0)):
            after_unrelated_edit = self.cli('doctor')[1]
            config['release']['artifacts'] = ['dist/different.zip']
            write_json(config_path, config)
            after_scope_change = self.cli('doctor')[1]
        release = after_unrelated_edit['setup_review']['capabilities']['github_releases']
        self.assertEqual((release['mode'], release['trigger'], release['effective_trigger']),
                         ('draft', 'version-file', 'manual'))
        release = after_scope_change['setup_review']['capabilities']['github_releases']
        self.assertEqual((release['mode'], release['trigger'], release['effective_trigger']),
                         ('publish', 'manual', 'manual'))

    def test_upgrade_digest_binds_requested_mode_and_trigger_even_with_same_effective_release(self):
        self.start(grouped=True)
        config_path = self.root / '.sfc/config.json'
        before = config_path.read_bytes()
        source = self.release_file()
        draft = ('--release-config', source, '--release-mode', 'draft')
        publish = ('--release-config', source, '--release-mode', 'publish')
        expected = self.cli('setup-defaults', *draft)[1]
        apply = ('--apply', '--expected-digest', expected['config_digest'],
                 '--expected-proposed-digest', expected['proposed_digest'])
        for options, trigger in ((publish, 'version-file'), (draft, 'manual')):
            with self.subTest(options=options, trigger=trigger):
                write_json(source, {**read_json(source), 'trigger': trigger})
                other = self.cli('setup-defaults', *options)[1]
                self.assertEqual(other['proposed_config'], expected['proposed_config'])
                self.assertNotEqual(other['proposed_digest'], expected['proposed_digest'])
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(self.cli('setup-defaults', *options, *apply)[0], 1)
                self.assertEqual(config_path.read_bytes(), before)
                self.assertFalse((self.root / '.sfc/efficiency-migrations/current.json').exists())
        write_json(source, {**read_json(source), 'trigger': 'version-file'})
        self.assertEqual(self.cli('setup-defaults', *draft, *apply)[0], 0)
        before = config_path.read_bytes()
        revised = self.cli('setup-defaults', *publish)[1]
        code, result = self.cli('setup-defaults', *publish, '--apply', '--expected-digest', revised['config_digest'],
                                '--expected-proposed-digest', revised['proposed_digest'])
        self.assertEqual(code, 0)
        self.assertTrue(result['applied'])
        self.assertEqual(config_path.read_bytes(), before)
        with patch.object(sfc, 'run', return_value=SimpleNamespace(returncode=0)):
            doctor = self.cli('doctor')[1]
        release = doctor['setup_review']['capabilities']['github_releases']
        self.assertEqual((release['status'], release['mode'], release['trigger'], release['effective_trigger']),
                         ('pending', 'publish', 'version-file', 'manual'))

    def test_newest_explicit_efficiency_choice_survives_unrelated_config_changes(self):
        self.efficiency_layout()
        self.start('--efficiency-profile', 'off', '--verify', efficiency.COMMANDS[0], grouped=True)
        config_path = self.root / '.sfc/config.json'
        auth_path = autostart.paths(self.root)[1]
        authority = auth_path.read_bytes()

        def change_timeout():
            config = read_json(config_path)
            config['verify_timeout'] += 1
            write_json(config_path, config)

        def apply(command, *options):
            code, preview = self.cli(command, *options)
            self.assertEqual(code, 0)
            code, applied = self.cli(command, *options, '--apply', '--expected-digest', preview['config_digest'],
                                     '--expected-proposed-digest', preview['proposed_digest'])
            self.assertEqual(code, 0)
            return applied

        change_timeout()
        self.assertEqual(self.cli('setup-defaults')[1]['profile'], 'off')
        self.assertEqual(apply('setup-defaults')['profile'], 'off')
        self.assertNotIn('worker_runtime', read_json(config_path))
        self.assertNotIn('verification_reuse', read_json(config_path))
        with patch.object(sfc, 'run', return_value=SimpleNamespace(returncode=0)):
            doctor = self.cli('doctor')[1]
        self.assertEqual(doctor['setup_review']['capabilities']['test_result_reuse']['status'], 'off')
        self.assertNotIn('qualification', doctor['efficiency'])
        self.assertTrue(apply('efficiency-profile', '--profile', 'auto')['applied'])
        change_timeout()
        self.assertEqual(self.cli('setup-defaults')[1]['profile'], 'sfc-python')
        before = config_path.read_bytes()
        self.assertTrue(apply('efficiency-profile', '--profile', 'off')['applied'])
        self.assertEqual(config_path.read_bytes(), before)
        change_timeout()
        self.assertEqual(self.cli('setup-defaults')[1]['profile'], 'off')
        with patch.object(sfc, 'run', return_value=SimpleNamespace(returncode=0)):
            doctor = self.cli('doctor')[1]
        self.assertEqual(doctor['efficiency']['profile'], 'off')
        self.assertEqual(doctor['setup_review']['capabilities']['test_result_reuse']['status'], 'active')
        self.assertEqual(auth_path.read_bytes(), authority)

    def test_legacy_efficiency_migration_opt_out_survives_drift_and_new_release_choice(self):
        self.start(grouped=True)
        config_path = self.root / '.sfc/config.json'
        config = read_json(config_path)
        config.pop('worker_runtime')
        legacy = {'root': str(self.root.resolve()), 'applied': True, 'profile': 'off',
                  'proposed_config_digest': autostart.digest(config)}
        write_json(self.root / '.sfc/efficiency-migrations/current.json', legacy)
        config['verify_timeout'] += 1
        write_json(config_path, config)
        self.assertEqual(self.cli('setup-defaults')[1]['profile'], 'off')

        options = ('--release-config', self.release_file(), '--release-mode', 'draft')
        preview = self.cli('setup-defaults', *options)[1]
        code, result = self.cli('setup-defaults', *options, '--apply', '--expected-digest', preview['config_digest'],
                                '--expected-proposed-digest', preview['proposed_digest'])
        self.assertEqual(code, 0)
        self.assertTrue(result['applied'])
        config = read_json(config_path)
        self.assertNotIn('worker_runtime', config)
        config['verify_timeout'] += 1
        write_json(config_path, config)
        self.assertEqual(self.cli('setup-defaults')[1]['profile'], 'off')

    def test_unsupported_host_leaves_watcher_unavailable_without_blocking_configuration(self):
        with patch('sfc_lib.lifeline.supported', return_value=False):
            result = self.start('--release-mode', 'off', grouped=True)
            self.assertEqual(result['setup_review']['capabilities']['watcher']['status'], 'unavailable')
            self.assertEqual(result['setup_review']['capabilities']['github_releases']['status'], 'off')
            self.assertTrue((self.root / '.sfc/config.json').exists())
            self.assertFalse(autostart.paths(self.root)[1].exists())
            self.assertEqual(result['autostart']['state'], 'pending-host')
            self.assertIn('Project configuration is saved', result['autostart']['detail'])
            self.assertIn('autostart --enable for bounded watching', result['autostart']['detail'])
            self.assertEqual(result['capabilities']['watcher']['detail'], result['autostart']['detail'])
            self.assertEqual(self.cli('onboard-finish')[1]['autostart'], result['autostart'])

    def test_explicit_bounded_watching_supersedes_pending_lifetime_in_readback(self):
        with patch('sfc_lib.lifeline.supported', return_value=False):
            self.start('--release-mode', 'off', grouped=True)
            config = read_json(self.root / '.sfc/config.json')
            with patch.object(sfc, 'Factory', return_value=Factory(self.root, config, self.gh)):
                code, bounded = self.cli('autostart', '--enable')
            self.assertEqual(code, 0)
            self.assertEqual(bounded['version'], 1)
            auth_path = autostart.paths(self.root)[1]
            choice_path = self.root / '.sfc/onboarding.json'
            saved = {path: path.read_bytes() for path in (auth_path, choice_path)}
            with patch.object(sfc, 'run', return_value=SimpleNamespace(returncode=0)):
                doctor = self.cli('doctor')[1]
            code, finished = self.cli('onboard-finish')
            self.assertEqual(code, 0)
            self.assertEqual(finished['autostart'], bounded)
            for report in (doctor, finished, self.cli('setup-defaults')[1]):
                watcher = report['setup_review']['capabilities']['watcher']
                self.assertEqual(watcher['status'], 'active')
                self.assertEqual(watcher['mode'], 'bounded')
            self.assertEqual({path: path.read_bytes() for path in saved}, saved)
            for key, value in (('metadata', {**bounded['metadata'], 'id': 'different-project'}),
                               ('hosts', None), ('hosts', ['invalid-host-digest']),
                               ('baseline', 'invalid-baseline'), ('watcher', 'invalid-watcher')):
                with self.subTest(key=key, value=value):
                    changed = {**bounded, key: value}
                    write_json(auth_path, changed)
                    self.assertEqual(onboarding.current(self.root, config, self.gh.metadata())['state'],
                                     'needs-inspection')
                    with patch.object(sfc, 'run', return_value=SimpleNamespace(returncode=0)):
                        reports = (self.cli('doctor')[1], self.cli('setup-defaults')[1])
                    for report in reports:
                        watcher = report['setup_review']['capabilities']['watcher']
                        self.assertEqual(watcher['status'], 'pending')
                        self.assertIn('Inspect the saved watcher choice', watcher['reason'])
                    with self.assertRaisesRegex(Halt, 'changed'):
                        onboarding.finish(self.root, self.gh)
                    self.assertEqual(read_json(auth_path), changed)
            for field in ('hosts', 'watcher', 'baseline', 'state'):
                with self.subTest(missing=field):
                    write_json(auth_path, {k: v for k, v in bounded.items() if k != field})
                    self.assertEqual(onboarding.current(self.root, config, self.gh.metadata())['state'],
                                     'needs-inspection')
            auth_path.write_text('{')
            self.assertEqual(onboarding.current(self.root, config, self.gh.metadata())['state'], 'needs-inspection')
            self.assertEqual(auth_path.read_text(), '{')

    def test_enterprise_origin_leaves_only_github_publication_unavailable(self):
        git(self.root, 'remote', 'set-url', 'origin', 'https://enterprise.example.test/example/app')
        self.gh.origin = self.gh.origin_url(self.root)
        result = self.start_reviewed('--release-config', self.release_file(),
                            '--release-authorization', 'publication-choice', grouped=True)
        caps = result['setup_review']['capabilities']
        self.assertEqual(caps['watcher']['status'], 'active')
        self.assertEqual(caps['github_releases']['status'], 'unavailable')
        config = read_json(self.root / '.sfc/config.json')
        self.assertEqual(config['release']['trigger'], 'manual')
        self.assertEqual(caps['github_releases']['trigger'], 'version-file')
        self.assertEqual(caps['github_releases']['effective_trigger'], 'manual')
        factory = Factory(self.root, config, self.gh)
        factory.state.update(plans=[], outcome='running')
        with patch.object(factory, 'queue_cards', return_value=[]) as queue, \
                patch('sfc_lib.releases.ReleaseGitHub.identity',
                      side_effect=AssertionError('Unavailable releases must not preempt queue pickup')):
            scheduler.schedule(factory, None)
        self.assertTrue(queue.called)
        self.assertEqual(factory.state['outcome'], 'drained')
        self.assertFalse(factory.processes.stop_path.exists())

    def test_defaults_upgrade_preserves_custom_verification_contracts(self):
        self.efficiency_layout()
        (self.root / 'tests/run_suite.py').write_text('SFC_DOCS_SUITE_VERSION = 1\n')
        self.start(grouped=True)
        config_path = self.root / '.sfc/config.json'
        config = read_json(config_path)
        config['verify_commands'] = list(efficiency.COMMANDS)
        custom = contract(efficiency.COMMANDS[0])
        config['verification_reuse'] = [custom]
        config.pop('worker_runtime', None)
        write_json(config_path, config)
        before = config_path.read_bytes()
        auth_path = autostart.paths(self.root)[1]
        authority = auth_path.read_bytes()
        expected = efficiency.migration(self.root)['proposed_config']
        code, preview = self.cli('setup-defaults')
        self.assertEqual(code, 0)
        self.assertEqual(preview['proposed_config'], expected)
        self.assertEqual(config_path.read_bytes(), before)
        self.assertEqual(preview['profile'], 'sfc-python')
        self.assertEqual(preview['proposed_config']['verification_reuse'][0], custom)
        self.assertEqual(preview['proposed_config']['verify_commands'], config['verify_commands'])
        code, applied = self.cli('setup-defaults', '--apply', '--expected-digest', preview['config_digest'],
                                '--expected-proposed-digest', preview['proposed_digest'])
        self.assertEqual(code, 0)
        self.assertTrue(applied['applied'])
        self.assertTrue(applied['setup_review']['applied'])
        self.assertFalse(preview['setup_review']['applied'])
        self.assertEqual(read_json(config_path), expected)
        self.assertEqual(auth_path.read_bytes(), authority)

    def test_doctor_reports_saved_watcher_eligibility_without_writes(self):
        self.start_reviewed('--allow-merge', grouped=True)
        auth_path = autostart.paths(self.root)[1]
        original = read_json(auth_path)
        for state, enabled, status in (('armed', True, 'active'), ('starting', True, 'pending'),
                                       ('stopped', True, 'pending'), ('starting', False, 'off')):
            with self.subTest(state=state, enabled=enabled):
                write_json(auth_path, {**original, 'state': state, 'enabled': enabled})
                before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
                with patch.object(sfc, 'run', return_value=SimpleNamespace(returncode=0)):
                    result = self.cli('doctor')[1]
                caps = result['setup_review']['capabilities']
                self.assertEqual(caps['watcher']['status'], status)
                self.assertEqual(caps['pickup']['status'], status)
                self.assertEqual(caps['automatic_merge']['status'], 'active' if status == 'active' else 'pending')
                self.assertFalse(result['host_activation_verified'])
                self.assertEqual({p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}, before)

    def test_cancelled_bounded_readiness_checks_resumed_scope(self):
        from sfc_lib.watch import Watch

        self.start('--no-auto-start', grouped=True)
        common, auth_path, _ = autostart.paths(self.root)
        f = Factory(self.root, read_json(self.root / '.sfc/config.json'), self.gh)
        auth = autostart.configure(f, enabled=True, allow_merge=True)
        watcher = Watch(f, start=True)
        watcher.begin(False)
        window_path = common / 'sfc-watch.json'
        write_json(auth_path, {**auth, 'state': 'cancelled', 'watcher': watcher.session['id']})
        before = auth_path.read_bytes(), window_path.read_bytes()
        with self.assertRaisesRegex(Halt, 'scope/merge authority changed'):
            Watch(f, resume=watcher.session['id']).begin(True)
        with patch.object(sfc, 'run', return_value=SimpleNamespace(returncode=0)):
            for command in ('doctor', 'setup-defaults'):
                caps = self.cli(command)[1]['setup_review']['capabilities']
                for name in ('watcher', 'pickup', 'automatic_merge'):
                    self.assertEqual(caps[name]['status'], 'pending')
                    self.assertIn('authority changed', caps[name]['reason'])
        self.assertEqual((auth_path.read_bytes(), window_path.read_bytes()), before)

    def test_saved_dashboard_drift_blocks_watcher_admission_in_doctor_and_upgrade(self):
        self.start_reviewed('--allow-merge', grouped=True)
        self.gh.data['project']['views']['nodes'][0]['verticalGroupByFields']['nodes'] = []
        with patch.object(sfc, 'run', return_value=SimpleNamespace(returncode=0)):
            doctor = self.cli('doctor')[1]
        upgrade = self.cli('setup-defaults')[1]
        self.assertFalse(doctor['dashboard']['ready'])
        for report in (doctor, upgrade):
            caps = report['setup_review']['capabilities']
            for name in ('watcher', 'pickup', 'automatic_merge'):
                self.assertEqual(caps[name]['status'], 'pending')
            self.assertIn('dashboard', caps['watcher']['reason'])

    def test_auto_documentation_profile_requires_partitioned_standard_project(self):
        self.efficiency_layout()
        (self.root / 'tests/run_suite.py').write_text('SFC_DOCS_SUITE_VERSION = 1\n')
        result = self.start('--no-auto-start', '--verify', efficiency.COMMANDS[0], grouped=True)
        self.assertEqual(result['efficiency']['profile'], 'sfc-python')
        config = read_json(self.root / '.sfc/config.json')
        config['verify_commands'] = list(efficiency.COMMANDS)
        config.pop('verification_reuse', None)
        self.assertEqual(efficiency.recommended_profile(self.root, config), 'sfc-python-docs')
        proposal = efficiency.propose(self.root, config, efficiency.recommended_profile(self.root, config))
        self.assertEqual(proposal['config']['verify_commands'], list(efficiency.DOCUMENTATION_CHECKS))
        self.assertTrue(proposal['config']['verification_reuse'][0]['documentation_independent'])
        write_json(self.root / '.sfc/config.json', config)
        preview = self.cli('setup-defaults')[1]
        self.assertEqual(preview['profile'], 'sfc-python-docs')
        self.assertEqual(read_json(self.root / '.sfc/config.json'), config)
        self.cli('setup-defaults', '--apply', '--expected-digest', preview['config_digest'],
                 '--expected-proposed-digest', preview['proposed_digest'])
        applied = read_json(self.root / '.sfc/config.json')
        self.assertEqual(applied['verify_commands'], list(efficiency.DOCUMENTATION_CHECKS))
        self.assertTrue(applied['verification_reuse'][0]['documentation_independent'])

    def efficiency_layout(self):
        write_json(self.root / '.codex-plugin/plugin.json', {'name': 'sfc'})
        (self.root / 'scripts').mkdir()
        (self.root / 'scripts/validate_plugin.py').write_text('raise SystemExit(0)\n')
        (self.root / 'tests').mkdir()

    def test_efficiency_profile_reports_unavailable_qualification_and_saves_explicit_worker_choice(self):
        self.efficiency_layout()
        missing = self.root / '.sfc/private-model-input.json'
        result = self.start('--no-auto-start', '--verify', efficiency.COMMANDS[0],
                            '--model-qualification', str(missing), '--service-tier', 'fast', grouped=True)
        self.assertEqual(result['efficiency']['profile'], 'sfc-python')
        self.assertEqual([c['reuse_scope'] for c in result['efficiency']['checks']], ['off', 'repository'])
        self.assertEqual(result['efficiency']['qualification']['model']['status'], 'retained')
        self.assertIn('unavailable', result['efficiency']['qualification']['model']['reason'])
        self.assertNotIn(str(missing), json.dumps(result['efficiency']))
        self.assertEqual(result['worker_settings']['service_tier'], 'fast')
        saved = read_json(self.root / '.sfc/onboarding.json')['efficiency']
        self.assertEqual(saved['implicit_model_settings'], ['model', 'reasoning_effort'])

    def test_selected_review_qualification_is_revalidated_by_normal_readback(self):
        self.efficiency_layout()
        report = self.root / '.sfc/private-review-report.json'
        write_json(report, {'execution': 'fixture-only'})
        policy = {'inspection': {'enabled': ['source'], 'promotion': {
            'path': str(report.resolve()), 'sha256': verification.checksum(report)}}}
        manifest = self.root / '.sfc/review-qualification.json'
        write_json(manifest, {'version': 1, 'review_policy': policy})
        release = self.release_file()
        with patch('sfc_lib.review_inspection.qualified', return_value=True):
            result = self.start('--no-auto-start', '--review-qualification', str(manifest),
                                '--release-config', release, '--release-mode', 'draft', grouped=True)
            self.assertEqual(result['efficiency']['qualification']['review']['status'], 'selected')
            self.assertEqual(read_json(self.root / '.sfc/config.json')['review_policy'], policy)
            self.assertFalse(autostart.paths(self.root)[1].exists())
            self.assertFalse((self.root / '.sfc/state.json').exists())
            options = ('setup-defaults', '--release-config', release, '--release-mode', 'publish')
            config_path = self.root / '.sfc/config.json'
            before = config_path.read_bytes()
            code, preview = self.cli(*options)
            self.assertEqual(code, 0)
            code, applied = self.cli(*options, '--apply', '--expected-digest', preview['config_digest'],
                                     '--expected-proposed-digest', preview['proposed_digest'])
            self.assertEqual(code, 0)
            self.assertTrue(applied['applied'])
            self.assertEqual(config_path.read_bytes(), before)
            for upgrade in (preview, applied):
                self.assertEqual(upgrade['setup_review']['capabilities']['review_efficiency']['status'], 'active')
            self.assertEqual(self.cli('onboard-finish')[1]['efficiency']['qualification']['review']['status'], 'selected')
            write_json(report, {'changed': True})
            code, result = self.cli('onboard-finish')
            self.assertEqual(code, 0)
            self.assertEqual(result['efficiency']['qualification']['review']['status'], 'stale')
            self.assertNotIn(str(report.resolve()), json.dumps(result['efficiency']))
            self.assertEqual(read_json(self.root / '.sfc/config.json')['review_policy'], policy)

    def test_legacy_review_eligibility_reads_bound_runtime_evidence(self):
        from sfc_lib.review_benchmark import inspection_binding, inspection_policy

        self.start(grouped=True)
        (self.root / '.sfc/onboarding.json').unlink()
        config_path = self.root / '.sfc/config.json'
        report = self.root / 'legacy-inspection.json'
        config = read_json(config_path)
        config['review_policy'] = {'inspection': {'enabled': ['source']}}
        write_json(report, {'runtime_sha256': review_policy.runtime_binding(), 'binding': inspection_binding(config),
                            'target_policy': inspection_policy(config), 'model_settings': review_policy.model_settings(config),
                            'cli_version': 'fixture-codex'})
        config['review_policy']['inspection']['promotion'] = {
            'path': str(report), 'sha256': verification.checksum(report)}
        write_json(config_path, config)
        with (patch.object(sfc, 'run', return_value=SimpleNamespace(returncode=0)),
              patch('sfc_lib.review_policy.exec_provenance', return_value={'version': 'fixture-codex'}),
              patch('sfc_lib.review_benchmark.evaluate_inspection', return_value={'qualified': ['source']})):
            for command in ('doctor', 'setup-defaults'):
                result = self.cli(command)[1]['setup_review']['capabilities']['review_efficiency']
                self.assertEqual(result['status'], 'active', command)
            write_json(report, {'changed': True})
            for command in ('doctor', 'setup-defaults'):
                result = self.cli(command)[1]['setup_review']['capabilities']['review_efficiency']
                self.assertEqual(result['status'], 'pending', command)

    def test_efficiency_migration_uses_supervisor_lock_and_readback(self):
        self.start('--no-auto-start', '--verify', efficiency.COMMANDS[0], grouped=True)
        self.efficiency_layout()
        config_path = self.root / '.sfc/config.json'
        before = config_path.read_bytes()
        code, preview = self.cli('efficiency-profile')
        self.assertEqual(code, 0)
        self.assertEqual(config_path.read_bytes(), before)
        args = ('efficiency-profile', '--apply', '--expected-digest', preview['config_digest'],
                '--expected-proposed-digest', preview['proposed_digest'])
        stderr = io.StringIO()
        with exclusive(self.root), contextlib.redirect_stderr(stderr):
            self.assertEqual(sfc.main(['--root', str(self.root), *args]), 1)
        self.assertIn('Another SFC supervisor', stderr.getvalue())
        self.assertEqual(config_path.read_bytes(), before)
        code, applied = self.cli(*args)
        self.assertEqual(code, 0)
        self.assertTrue(applied['applied'])
        self.assertEqual(read_json(config_path), preview['proposed_config'])
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(sfc.main(['--root', str(self.root), *args]), 1)
        self.assertEqual(read_json(config_path), preview['proposed_config'])

    def test_fresh_user_all_capabilities_and_no_redundant_alias_decision(self):
        self.gh.repo_labels.append(dict(node_id='alias', name='enhancement',
                                       description='New feature or request', color='123456'))
        before = copy.deepcopy(self.gh.repo_labels)
        result = self.start(grouped=True)
        self.assertTrue(result['onboarding_complete'])
        self.assertEqual(set(result['capabilities']), {'repository_board', 'base', 'project_checks',
                         'stages', 'priority', 'work_types_labels', 'agent_activity', 'workers',
                         'merge_policy', 'hooks', 'mcp', 'watcher'})
        for key in ('stages', 'priority', 'work_types_labels', 'agent_activity'):
            self.assertEqual(result['capabilities'][key]['status'], 'verified')
        self.assertEqual(result['capabilities']['watcher']['status'], 'armed')
        for key in ('hooks', 'mcp'):
            self.assertEqual(result['capabilities'][key]['status'], 'unverified')
        self.assertFalse(result['host_activation_verified'])
        self.assertEqual(self.gh.repo_labels, before)
        self.assertEqual(self.gh.writes, [])
        self.assertFalse((common_dir(self.root) / 'sfc-watch.json').exists())
        self.assertFalse((self.root / '.sfc/state.json').exists())
        self.assertFalse(result['autostart']['scope']['allow_merge'])
        self.assertEqual(result['implementation']['version'], sfc.__version__)
        self.assertEqual(len(result['implementation']['sha256']), 4)

    def test_pending_choice_finishes_once_without_new_questions_or_workers(self):
        result = self.start_reviewed('--watch-max-seconds', '600', '--watch-exclude-issue', '8', '--allow-merge')
        self.assertFalse(result['onboarding_complete'])
        self.assertEqual(result['capabilities']['stages']['status'], 'pending')
        self.assertEqual(result['capabilities']['watcher']['status'], 'pending-setup')
        self.assertTrue(any('onboard-finish' in step for step in result['next_steps']))
        config = (self.root / '.sfc/config.json').read_bytes()
        self.assertFalse(self.cli('onboard-finish')[1]['onboarding_complete'])
        self.repair_grouping()
        code, result = self.cli('onboard-finish')
        self.assertEqual(code, 0)
        self.assertTrue(result['onboarding_complete'])
        self.assertEqual(result['autostart']['version'], 2)
        self.assertEqual(result['autostart']['scope']['watch']['max_seconds'], 600)
        self.assertEqual(result['autostart']['scope']['watch']['excluded_issues'], [8])
        self.assertTrue(result['autostart']['scope']['allow_merge'])
        self.assertTrue(read_json(self.root / '.sfc/onboarding.json')['watch_applied'])
        with patch.object(sfc, 'run', return_value=SimpleNamespace(returncode=0)):
            code, doctor = self.cli('doctor')
        self.assertEqual(code, 0)
        self.assertTrue(doctor['onboarding_complete'])
        self.assertEqual(doctor['setup_review']['capabilities']['automatic_merge']['status'], 'active')
        auth_path = autostart.paths(self.root)[1]
        auth = auth_path.read_bytes()
        self.cli('onboard-finish')
        self.assertEqual(auth_path.read_bytes(), auth)
        self.assertEqual((self.root / '.sfc/config.json').read_bytes(), config)
        self.assertFalse((common_dir(self.root) / 'sfc-watch.json').exists())

    def test_pending_bounded_choice_is_preserved(self):
        self.start('--no-session-lifetime')
        self.repair_grouping()
        result = self.cli('onboard-finish')[1]
        self.assertEqual(result['autostart']['version'], 1)
        self.assertFalse(result['autostart']['scope']['allow_merge'])

    def test_doctor_reports_pending_choice_without_enabling_or_claiming_host_readiness(self):
        self.start()
        self.repair_grouping()
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        with patch.object(sfc, 'GitHub', return_value=self.gh), \
                patch.object(sfc, 'run', return_value=SimpleNamespace(returncode=0)):
            result = sfc.doctor(self.root)
        self.assertTrue(result['ready'])
        self.assertFalse(result['onboarding_complete'])
        self.assertEqual(result['capabilities']['watcher']['status'], 'pending-setup')
        self.assertFalse(result['host_activation_verified'])
        self.assertEqual({p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}, before)

    def test_upgrade_uses_saved_pending_choice_and_rechecks_project_metadata(self):
        self.start_reviewed('--allow-merge')
        auth_path = autostart.paths(self.root)[1]
        preview = self.cli('setup-defaults')[1]['setup_review']['capabilities']
        for name in ('watcher', 'pickup', 'automatic_merge'):
            self.assertEqual(preview[name]['status'], 'pending')
        self.assertEqual(preview['watcher']['mode'], 'session-lifetime')
        self.assertTrue(preview['automatic_merge']['allow_merge'])
        self.assertFalse(auth_path.exists())

        self.repair_grouping()
        self.assertEqual(self.cli('onboard-finish')[0], 0)
        preview = self.cli('setup-defaults')[1]['setup_review']['capabilities']
        self.assertEqual(preview['watcher']['status'], 'active')
        auth = read_json(auth_path)
        auth['metadata']['field'] = 'former-field'
        write_json(auth_path, auth)
        before = auth_path.read_bytes()
        with patch.object(sfc, 'run', return_value=SimpleNamespace(returncode=0)):
            preview = self.cli('setup-defaults')[1]['setup_review']['capabilities']
            doctor = self.cli('doctor')[1]['setup_review']['capabilities']
        for name in ('watcher', 'pickup', 'automatic_merge'):
            self.assertEqual(preview[name]['status'], 'pending')
            self.assertEqual(preview[name], doctor[name])
        self.assertEqual(auth_path.read_bytes(), before)

    def test_expired_cancelled_bounded_window_is_unavailable_in_doctor_and_upgrade(self):
        from sfc_lib.watch import Watch

        self.start_reviewed('--no-session-lifetime', '--allow-merge', grouped=True)
        common, auth_path, _ = autostart.paths(self.root)
        auth = read_json(auth_path)
        f = Factory(self.root, read_json(self.root / '.sfc/config.json'), self.gh)
        watcher = Watch(f, start=True, clock=lambda: 1000)
        watcher.begin(True)
        watcher.save('stopped')
        write_json(auth_path, {**auth, 'state': 'cancelled', 'watcher': watcher.session['id']})
        window_path = common / 'sfc-watch.json'
        before = auth_path.read_bytes(), window_path.read_bytes()

        def capabilities(command, at):
            with patch.object(sfc, 'run', return_value=SimpleNamespace(returncode=0)), \
                 patch.object(sfc.setup_review, 'time', SimpleNamespace(time=lambda: at)):
                code, result = self.cli(command)
            self.assertEqual(code, 0)
            return result['setup_review']['capabilities']

        for command in ('doctor', 'setup-defaults'):
            with self.subTest(command=command):
                deadline = watcher.session['deadline']
                active = capabilities(command, deadline - 1)
                expired = capabilities(command, deadline)
                for name in ('watcher', 'pickup', 'automatic_merge'):
                    self.assertEqual(active[name]['status'], 'active')
                    self.assertEqual(expired[name]['status'], 'unavailable')
                    self.assertIn('bounded watcher window expired', expired[name]['reason'])
        self.assertEqual((auth_path.read_bytes(), window_path.read_bytes()), before)

        watcher.session['polls'] = watcher.settings['max_polls']
        watcher.save('cancelled')
        exhausted = window_path.read_bytes()
        for command in ('doctor', 'setup-defaults'):
            caps = capabilities(command, watcher.session['deadline'] - 1)
            for name in ('watcher', 'pickup', 'automatic_merge'):
                self.assertEqual(caps[name]['status'], 'unavailable')
                self.assertIn('polls', caps[name]['reason'])
        self.assertEqual(window_path.read_bytes(), exhausted)

    def test_doctor_reports_lifetime_window_eligibility_separately_from_observation(self):
        from sfc_lib.watch import Watch

        self.start_reviewed('--allow-merge', grouped=True)
        common, auth_path, _ = autostart.paths(self.root)
        auth = read_json(auth_path)
        f = Factory(self.root, read_json(self.root / '.sfc/config.json'), self.gh)
        watcher = Watch(f, start=True)
        watcher.lifetime = True
        watcher.lifetime_authorization = auth['id']
        watcher.connection = 'fixture-connection'
        watcher.begin(True)
        auth['watcher'] = watcher.session['id']
        auth['windows'].append(auth['watcher'])

        for state, reason, in_flight, spent, status in (
                ('window-finished', 'watch-polls-limit', False, 0, 'active'),
                ('suspended', 'stopped', False, 0, 'active'),
                ('suspended', 'stopped', True, 0, 'pending'),
                ('window-finished', 'watch-polls-limit', False, f.c['max_no_progress'], 'pending'),
                ('window-finished', 'halted', False, 0, 'pending')):
            with self.subTest(state=state, reason=reason, in_flight=in_flight, spent=spent):
                watcher.session.update(work_in_flight=in_flight, no_progress=spent)
                watcher.save(reason)
                write_json(auth_path, {**auth, 'state': state})
                before = auth_path.read_bytes(), (common / 'sfc-watch.json').read_bytes()
                with patch.object(sfc, 'run', return_value=SimpleNamespace(returncode=0)):
                    result = self.cli('doctor')[1]
                self.assertFalse(result['host_activation_verified'])
                self.assertEqual(result['capabilities']['watcher']['status'], 'recorded-' + state)
                for name in ('watcher', 'pickup', 'automatic_merge'):
                    self.assertEqual(result['setup_review']['capabilities'][name]['status'], status)
                self.assertEqual((auth_path.read_bytes(), (common / 'sfc-watch.json').read_bytes()), before)

    def test_retained_start_intent_requires_recovery_in_doctor_and_setup_review(self):
        self.start_reviewed('--allow-merge', grouped=True)
        common, auth_path, _ = autostart.paths(self.root)
        auth = {**read_json(auth_path), 'state': 'starting', 'connection': 'e' * 32}
        write_json(auth_path, auth)
        saved = auth_path.read_bytes()
        self.assertFalse((common / 'sfc-watch.json').exists())
        with self.assertRaisesRegex(Halt, 'incomplete intent'):
            autostart.lifetime_window(auth, None, 0, 3)
        with patch.object(sfc, 'run', return_value=SimpleNamespace(returncode=0)):
            for command in ('doctor', 'setup-defaults'):
                caps = self.cli(command)[1]['setup_review']['capabilities']
                for name in ('watcher', 'pickup', 'automatic_merge'):
                    with self.subTest(command=command, name=name):
                        self.assertEqual(caps[name]['status'], 'pending')
                        self.assertIn('autostart --enable --session-lifetime', caps[name]['reason'])
                        if name == 'automatic_merge':
                            self.assertIn('--session-lifetime --allow-merge', caps[name]['reason'])
        self.assertEqual(auth_path.read_bytes(), saved)
        self.assertFalse((common / 'sfc-watch.json').exists())

    def test_armed_lifetime_scope_pin_is_checked_in_doctor_and_upgrade(self):
        from sfc_lib.watch import Watch

        self.start('--no-auto-start', grouped=True)
        common, auth_path, _ = autostart.paths(self.root)
        f = Factory(self.root, read_json(self.root / '.sfc/config.json'), self.gh)
        watcher = Watch(f, start=True)
        watcher.begin(False)
        watcher.save('watch-polls-limit')
        auth = autostart.configure(f, enabled=True, lifetime=True, allow_merge=True)
        self.assertEqual(auth['baseline'], watcher.session['id'])

        def capabilities(command):
            with patch.object(sfc, 'run', return_value=SimpleNamespace(returncode=0)):
                return self.cli(command)[1]['setup_review']['capabilities']

        for command in ('doctor', 'setup-defaults'):
            caps = capabilities(command)
            self.assertTrue(all(caps[name]['status'] == 'active'
                                for name in ('watcher', 'pickup', 'automatic_merge')))

        watcher.begin(False)
        watcher.save('watch-polls-limit')
        window_path = common / 'sfc-watch.json'
        for state in ('armed', 'starting'):
            write_json(auth_path, {**auth, 'state': state})
            before = auth_path.read_bytes(), window_path.read_bytes()
            for command in ('doctor', 'setup-defaults'):
                caps = capabilities(command)
                for name in ('watcher', 'pickup', 'automatic_merge'):
                    self.assertEqual(caps[name]['status'], 'pending', (state, command, name))
                    reason = 'replaced the pinned window' if state == 'armed' else 'start intent is retained'
                    self.assertIn(reason, caps[name]['reason'])
            self.assertEqual((auth_path.read_bytes(), window_path.read_bytes()), before)

        watcher.lifetime = True
        watcher.lifetime_authorization = auth['id']
        watcher.connection = 'e' * 32
        watcher.begin(True)
        write_json(auth_path, {**auth, 'state': 'starting', 'connection': watcher.connection})
        for command in ('doctor', 'setup-defaults'):
            caps = capabilities(command)
            self.assertTrue(all(caps[name]['status'] == 'pending'
                                for name in ('watcher', 'pickup', 'automatic_merge')))

    def test_legacy_unset_watching_is_pending_and_explicit_disablement_is_off(self):
        config_path = self.root / '.sfc/config.json'
        write_json(config_path, self.gh.c)
        auth_path = autostart.paths(self.root)[1]
        for expected in ('pending', 'off'):
            if expected == 'off':
                autostart.configure(Factory(self.root, read_json(config_path), self.gh), enabled=False)
            else:
                self.assertFalse(auth_path.exists())
            for command in ('doctor', 'setup-defaults'):
                with patch.object(sfc, 'run', return_value=SimpleNamespace(returncode=0)):
                    code, result = self.cli(command)
                self.assertEqual(code, 0)
                caps = result['setup_review']['capabilities']
                for name in ('watcher', 'pickup'):
                    self.assertEqual(caps[name]['status'], expected, (command, name))
                    if expected == 'pending':
                        self.assertIn('autostart --enable', caps[name]['reason'])
            self.assertEqual(auth_path.exists(), expected == 'off')

    def test_doctor_cli_reports_requested_worker_settings_without_writes(self):
        config = {**self.gh.c, 'max_workers': 3, 'worker_timeout': 75, 'verify_timeout': 35,
                  'verify_commands': ['python3 -m unittest', 'python3 scripts/check.py']}
        del config['models']
        requested = {'model': 'fixture-model', 'reasoning_effort': 'medium', 'service_tier': None,
                     'models': {'build': 'fixture-build-model'}}
        inherited = {'model': None, 'reasoning_effort': None, 'service_tier': None, 'models': {}}
        for settings, expected in ((requested, requested), ({}, inherited)):
            with self.subTest(settings=settings):
                write_json(self.root / '.sfc/config.json', {**config, **settings})
                before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
                with patch.object(sfc, 'run', return_value=SimpleNamespace(returncode=0)):
                    code, result = self.cli('doctor')
                self.assertEqual(code, 0)
                self.assertTrue(result['ready'])
                self.assertTrue(result['onboarding_complete'])
                self.assertTrue(result['dashboard']['ready'])
                self.assertEqual(result['checks'][-1], {'check': 'GitHub Project status field', 'passed': True})
                self.assertIs(type(result['max_workers']), int)
                self.assertEqual(result['max_workers'], config['max_workers'])
                self.assertIs(type(result['verify_command_count']), int)
                self.assertEqual(result['verify_command_count'], 2)
                self.assertEqual((result['worker_timeout'], result['verify_timeout']), (75, 35))
                self.assertEqual(result['worker_settings'], expected)
                self.assertEqual({p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}, before)
                self.assertEqual(self.gh.writes + self.gh.mutations, [])

    def test_doctor_cli_counts_saved_documentation_independent_checks(self):
        config = {**self.gh.c, 'verify_commands': list(efficiency.DOCUMENTATION_CHECKS)}
        runtime = {**contract(efficiency.RUNTIME_SUITE), 'scope': 'repository', 'context': 'source-only'}
        remaining = contract(efficiency.REMAINING_SUITE)
        marked = {**runtime, 'documentation_independent': True}
        for case, contracts, expected in (
                ('absent', {}, 0),
                ('unmarked', {'verification_reuse': [runtime, remaining]}, 0),
                ('marked runtime', {'verification_reuse': [marked, remaining]}, 1)):
            with self.subTest(case=case):
                write_json(self.root / '.sfc/config.json', {**config, **contracts})
                with patch.object(sfc, 'run', return_value=SimpleNamespace(returncode=0)):
                    code, result = self.cli('doctor')
                self.assertEqual(code, 0)
                self.assertIs(type(result['documentation_independent_check_count']), int)
                self.assertEqual(result['documentation_independent_check_count'], expected)
                self.assertNotIn('latest_ticket_checks', result['efficiency'])

    def test_pending_off_choice_never_arms(self):
        self.start('--no-auto-start')
        self.repair_grouping()
        result = self.cli('onboard-finish')[1]
        self.assertTrue(result['onboarding_complete'])
        self.assertEqual(result['capabilities']['watcher']['status'], 'off')
        self.assertFalse(autostart.paths(self.root)[1].exists())

    def test_saved_stopped_watcher_is_not_reported_as_armed_or_reenabled(self):
        self.start(grouped=True)
        auth_path = autostart.paths(self.root)[1]
        auth = read_json(auth_path)
        auth['state'] = 'stopped'
        write_json(auth_path, auth)
        before = auth_path.read_bytes()
        result = self.cli('onboard-finish')[1]
        self.assertEqual(result['capabilities']['watcher']['status'], 'recorded-stopped')
        self.assertFalse(result['host_activation_verified'])
        self.assertEqual(auth_path.read_bytes(), before)

    def test_explicit_disable_after_arming_reports_off_without_reauthorizing(self):
        for options in ((), ('--no-session-lifetime',)):
            with self.subTest(options=options):
                case = OnboardingAcceptanceTests()
                case.setUp()
                self.addCleanup(case.doCleanups)
                case.start(*options, grouped=True)
                config = read_json(case.root / '.sfc/config.json')
                disabled = autostart.configure(Factory(case.root, config, case.gh), enabled=False)
                self.assertFalse(disabled['enabled'])
                self.assertEqual(disabled['state'], 'armed')  # Historical state survives disable.
                before = {p: p.read_bytes() for p in case.root.rglob('*') if p.is_file()}
                with patch.object(sfc, 'GitHub', return_value=case.gh), \
                        patch.object(sfc, 'run', return_value=SimpleNamespace(returncode=0)), \
                        patch.object(autostart, 'configure', side_effect=AssertionError('Must not reauthorize')):
                    results = [sfc.doctor(case.root), case.cli('onboard-finish')[1],
                               case.cli('onboard-finish')[1]]
                for result in results:
                    self.assertTrue(result['onboarding_complete'])
                    self.assertEqual(result['capabilities']['watcher']['status'], 'off')
                    self.assertFalse(result['host_activation_verified'])
                self.assertEqual({p: p.read_bytes() for p in case.root.rglob('*') if p.is_file()}, before)
                self.assertFalse((common_dir(case.root) / 'sfc-watch.json').exists())
                self.assertEqual(case.gh.writes, [])

    def test_completed_setup_uses_current_config_and_explicitly_refreshed_authorization(self):
        from types import SimpleNamespace
        for options in ((), ('--no-session-lifetime',)):
            with self.subTest(options=options):
                # Each mode gets a separate temporary repository and fake board.
                case = OnboardingAcceptanceTests()
                case.setUp()
                self.addCleanup(case.doCleanups)
                case.start(*options, grouped=True)
                config_path = case.root / '.sfc/config.json'
                config = read_json(config_path)
                config['model'] = 'explicit-worker-model'
                write_json(config_path, config)
                # Represents an explicit autostart update, never an onboarding retry.
                autostart.configure(Factory(case.root, config, case.gh), enabled=True,
                                    lifetime=not options)
                before = {p: p.read_bytes() for p in case.root.rglob('*') if p.is_file()}
                with patch.object(sfc, 'GitHub', return_value=case.gh), \
                        patch.object(sfc, 'run', return_value=SimpleNamespace(returncode=0)):
                    result = sfc.doctor(case.root)
                self.assertTrue(result['onboarding_complete'])
                self.assertEqual(result['capabilities']['watcher']['status'], 'armed')
                with patch.object(autostart, 'configure', side_effect=AssertionError('Must not reauthorize')):
                    result = onboarding.finish(case.root, case.gh)
                self.assertTrue(result['onboarding_complete'])
                self.assertFalse(result['host_activation_verified'])
                self.assertEqual({p: p.read_bytes() for p in case.root.rglob('*') if p.is_file()}, before)
                self.assertFalse((common_dir(case.root) / 'sfc-watch.json').exists())

    def test_completed_setup_rejects_stale_current_authorization_without_rearming(self):
        self.start(grouped=True)
        config_path = self.root / '.sfc/config.json'
        config = read_json(config_path)
        write_json(config_path, {**config, 'model': 'explicit-worker-model'})
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        result = onboarding.current(self.root, read_json(config_path), self.gh.metadata())
        self.assertEqual(result['state'], 'needs-inspection')
        with patch.object(autostart, 'configure', side_effect=AssertionError('Must not reauthorize')):
            result = onboarding.finish(self.root, self.gh)
        self.assertFalse(result['onboarding_complete'])
        self.assertEqual(result['capabilities']['watcher']['status'], 'needs-inspection')
        self.assertEqual({p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}, before)

    def test_completed_setup_checks_live_authorization_pins(self):
        self.start(grouped=True)
        auth_path = autostart.paths(self.root)[1]
        original = read_json(auth_path)
        for key, value in (('root', '/other-checkout'), ('common', '/other-git'),
                           ('config_digest', 'stale'), ('metadata', {'id': 'other-project'}),
                           ('scope', {**original['scope'], 'allow_merge': True})):
            with self.subTest(key=key):
                # The old onboarding receipt still matches; current authority does not.
                changed = copy.deepcopy(original)
                changed[key] = value
                if key == 'scope':
                    changed[key]['limits']['max_cycles'] += 1
                write_json(auth_path, changed)
                before = auth_path.read_bytes()
                result = onboarding.finish(self.root, self.gh)
                self.assertFalse(result['onboarding_complete'])
                self.assertEqual(result['capabilities']['watcher']['status'], 'needs-inspection')
                self.assertEqual(auth_path.read_bytes(), before)

    def test_explicit_off_remains_complete_after_config_update_without_authorization(self):
        self.start('--no-auto-start', grouped=True)
        config_path = self.root / '.sfc/config.json'
        config = read_json(config_path)
        write_json(config_path, {**config, 'model': 'explicit-worker-model'})
        result = onboarding.finish(self.root, self.gh)
        self.assertTrue(result['onboarding_complete'])
        self.assertEqual(result['capabilities']['watcher']['status'], 'off')
        self.assertFalse(autostart.paths(self.root)[1].exists())

    def test_config_project_stop_and_authorization_drift_do_not_enable(self):
        self.start()
        self.repair_grouping()
        config_path = self.root / '.sfc/config.json'
        config = read_json(config_path)
        write_json(config_path, {**config, 'max_cycles': config['max_cycles'] + 1})
        self.assertEqual(onboarding.current(self.root, read_json(config_path), self.gh.metadata())['state'],
                         'needs-inspection')
        with self.assertRaisesRegex(Halt, 'stale'):
            onboarding.finish(self.root, self.gh)
        write_json(config_path, config)
        metadata = self.gh.metadata()
        self.gh._meta = {**metadata, 'id': 'other-project'}
        with self.assertRaisesRegex(Halt, 'stale'):
            onboarding.finish(self.root, self.gh)
        self.gh._meta = metadata
        stop = common_dir(self.root) / 'sfc-stop'
        stop.touch()
        with self.assertRaisesRegex(Halt, 'Stop requested'):
            onboarding.finish(self.root, self.gh)
        self.assertTrue(stop.exists())
        stop.unlink()
        auth_path = autostart.paths(self.root)[1]
        write_json(auth_path, {'enabled': False, 'state': 'stopped'})
        before = auth_path.read_bytes()
        with self.assertRaisesRegex(Halt, 'changed'):
            onboarding.finish(self.root, self.gh)
        self.assertEqual(auth_path.read_bytes(), before)

    def test_lost_enable_response_is_not_retried(self):
        self.start()
        self.repair_grouping()
        configure = autostart.configure
        def lost(*args, **kwargs):
            configure(*args, **kwargs)
            raise Halt('Lost enable response')
        with patch.object(autostart, 'configure', side_effect=lost), self.assertRaisesRegex(Halt, 'Lost'):
            onboarding.finish(self.root, self.gh)
        auth_path = autostart.paths(self.root)[1]
        before = auth_path.read_bytes()
        with self.assertRaisesRegex(Halt, 'response was lost'):
            onboarding.finish(self.root, self.gh)
        self.assertEqual(auth_path.read_bytes(), before)

    def test_partial_dashboard_failure_preserves_choice_and_config(self):
        from types import SimpleNamespace
        args = SimpleNamespace(repo='example/app', owner='example', project=1, base='main',
                               status_field='SFC Status', verify=['python3 -m unittest'], merge_policy='auto')
        with patch.object(self.gh, 'configure_dashboard', side_effect=Halt('Partial dashboard write')), \
                patch.object(sfc, 'GitHub', return_value=self.gh), self.assertRaisesRegex(Halt, 'Partial'):
            sfc.onboard(self.root, args)
        receipt = read_json(self.root / '.sfc/onboarding.json')
        self.assertTrue(receipt['watch_requested'])
        self.assertFalse(receipt['watch_applied'])
        self.assertTrue((self.root / '.sfc/config.json').exists())
        self.assertFalse(autostart.paths(self.root)[1].exists())


if __name__ == '__main__':
    unittest.main()
