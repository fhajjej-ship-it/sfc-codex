"""Exercise release preflight through the scheduler with actual candidate ZIPs."""

from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from package_plugin import contents
from sfc_lib import __version__
from sfc_lib.common import Busy, Halt, Stopped, git, read_json, run
from sfc_lib.delivery import report
from sfc_lib.factory import Factory
from sfc_lib.releases import Releases, digest
from verify_release_package import validate_public_docs
from test_factory import FakeProcesses, issue
from test_pickup import PickupGitHub
from test_releases import Remote, config


class ReleasePreflightTests(unittest.TestCase):
    project_check = 'printf "%s\\n" "project suite invoked"'
    package_check = 'python3 scripts/package_plugin.py'
    preflight_check = 'python3 scripts/verify_release_package.py'

    def setup_release(self, *, bad_readme=False, private_reference=False, private_file=False, preflight=None,
                      broken_runtime=False):
        temporary = tempfile.TemporaryDirectory(prefix='sfc-release-preflight-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        git(self.root, 'init', '-b', 'main')
        git(self.root, 'config', 'user.email', 'fixture@example.test')
        git(self.root, 'config', 'user.name', 'Fixture')
        for name, data in contents().items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        if bad_readme:
            path = self.root / 'README.md'
            path.write_text(path.read_text().replace(f'sfc-{__version__}.zip', 'sfc-0.0.1.zip'))
        if private_reference:
            path = self.root / 'docs' / 'PRIVATE-FIXTURE.md'
            path.write_text('Raw maintainer timing receipt in `.sfc/findings/issue-28-build/raw.log`.\n')
        if private_file:
            (self.root / 'docs/evidence/private-fixture.json').write_text('{"private": true}\n')
        if broken_runtime:
            validator = self.root / 'scripts/sfc_lib/ticket_validation.py'
            validator.write_text(validator.read_text() + '\n\ndef lint_card(card):\n    return []\n')
        (self.root / 'value.txt').write_text('Fixture source for independent review.\n')
        (self.root / '.gitignore').write_text('/dist/\n/.sfc/\n__pycache__/\n')
        git(self.root, 'add', '.')
        git(self.root, 'commit', '-m', 'Initial package fixture')
        git(self.root, 'remote', 'add', 'origin', str(self.root))
        version = __version__
        archive = f'dist/sfc-{version}.zip'
        self.config = config()
        # Test actual semantic rejection, not runner-load timing during ZIP smoke checks.
        self.config.update(max_attempts=1, verify_commands=[self.project_check], verify_timeout=60)
        release = self.config['release']
        release.update(package_commands=[self.package_check], verify_commands=[],
                       preflight_commands=[preflight or self.preflight_check], artifacts=[archive, archive + '.sha256'])
        release['approved_summaries'] = {
            'authorization_id': 'fixture-summary', 'repo': 'example/app', 'base_branch': 'main',
            'host': 'github.com', 'destination': 'github-releases',
            'entries': [{'commit': git(self.root, 'rev-parse', 'HEAD'), 'text': 'Add the initial plugin package'}],
        }
        self.gh = PickupGitHub(self.root, [issue(1, title=f'Release {version}', status='Ready')])
        self.proc = FakeProcesses(self.root / '.sfc/stop')
        self.factory = Factory(self.root, self.config, self.gh, self.proc)
        self.remote = Remote(self.factory)
        self.releases = Releases(self.factory, self.remote)
        boundary, candidate, changes = self.releases.source()
        card = self.remote.card(1)
        self.releases.path = self.releases.directory / (version + '.json')
        self.releases.journal = {
            'workflow_kind': 'release', 'version': version, 'candidate': candidate, 'boundary': boundary,
            'changes': changes, 'identity': self.remote.identity(), 'config_digest': self.factory.config_digest,
            'state': 'planned', 'effects': {}, 'task_digest': self.factory.task_digest(card),
            'card': {k: card[k] for k in ('number', 'id', 'url', 'item_id', 'repository')},
        }
        self.releases.save()
        self.factory.update(card, workflow_kind='release', release_version=version)

    def run_scheduler(self, factory=None):
        def fake_version(argv, *args, **kwargs):
            if argv[:2] == ['codex', '--version']:
                return subprocess.CompletedProcess(argv, 0, 'fixture', '')
            return run(argv, *args, **kwargs)

        with patch('sfc_lib.factory.run', side_effect=fake_version), \
                patch('sfc_lib.releases.ReleaseGitHub', return_value=self.remote):
            return (factory or self.factory).run()

    def journal(self):
        return read_json(self.releases.path)

    def restart(self):
        return Factory(self.root, self.config, self.gh, self.proc)

    def assert_preflight_rejects(self, error):
        with patch.object(self.factory, 'verify', wraps=self.factory.verify) as verify:
            with self.assertRaisesRegex(Halt, 'preflight check failed'):
                self.run_scheduler()
            verify.assert_not_called()
        journal = self.journal()
        self.assertEqual(journal['state'], 'building')
        receipt = journal['preflight_checks']
        self.assertEqual(receipt['state'], 'failed')
        self.assertEqual(receipt['candidate'], journal['candidate'])
        self.assertEqual(receipt['artifact_digest'], digest(journal['artifacts']))
        self.assertGreater(receipt['elapsed_seconds'], 0)
        self.assertIn(error, Path(receipt['checks'][0]['log']).read_text())
        self.assertEqual([event[3] for event in self.proc.events], [self.package_check, self.preflight_check])
        self.assertEqual(self.proc.lanes, [])
        self.assertEqual(self.remote.writes, [])
        self.assertNotIn('project_check_state', journal)
        delivery = report(self.root)['releases'][0]
        self.assertEqual(delivery['check_receipts']['preflight'], receipt)
        self.assertEqual(delivery['preflight_outcome'], {'status': 'failed', 'project_check_commands_not_started': 1,
                         'independent_lanes_not_started': ['qa', 'security', 'review']})
        with self.assertRaisesRegex(Halt, 'preflight previously failed/interrupted'):
            self.run_scheduler(self.restart())
        self.assertEqual(self.journal()['preflight_checks'], receipt)
        self.assertEqual(len(self.proc.events), 2)

    def test_stale_quick_start_fails_before_project_verification_and_workers(self):
        self.setup_release(bad_readme=True)
        self.assert_preflight_rejects('Quick start download filenames do not match')

    def test_concrete_private_evidence_fails_before_project_verification_and_workers(self):
        self.setup_release(private_reference=True)
        self.assert_preflight_rejects('Concrete maintainer evidence reference')

    def test_concrete_mount_and_temporary_paths_fail_package_documentation_policy(self):
        for path in ('/Volumes/Test Drive/Projects/SFC/private/receipt.json',
                     '/private/var/folders/xx/test/T/sfc-delivery-fixture/receipt.json'):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, 'Concrete maintainer evidence reference'):
                validate_public_docs({'docs/example.md': f'Retained at `{path}`.\n'.encode()},
                                     __version__, f'sfc-{__version__}.zip')

    def test_allowlisted_source_inventory_does_not_make_raw_evidence_public(self):
        self.setup_release(private_file=True)
        self.assert_preflight_rejects('Maintainer evidence is forbidden in the release package')

    def test_broken_packaged_ticket_runtime_fails_before_project_verification_and_workers(self):
        self.setup_release(broken_runtime=True)
        self.assert_preflight_rejects('Packaged ticket validation accepted an empty checkbox')

    def stop_after_preflight(self):
        original = Releases.preflight

        def stop(releases, card, area):
            original(releases, card, area)
            raise Stopped('interruption after preflight')

        with patch.object(Releases, 'preflight', autospec=True, side_effect=stop):
            with self.assertRaisesRegex(Stopped, 'interruption after preflight'):
                self.run_scheduler()

    def test_valid_preflight_resumes_from_receipt_without_rerunning_and_publishes_exact_bytes(self):
        self.setup_release()
        self.stop_after_preflight()
        journal = self.journal()
        self.assertNotIn('project_check_state', journal)
        receipt = journal['preflight_checks']
        self.assertEqual(receipt['state'], 'passed')
        self.assertEqual(self.proc.lanes, [])
        self.assertEqual(self.run_scheduler(self.restart())['outcome'], 'drained')
        finished = self.journal()
        self.assertEqual(finished['state'], 'done')
        self.assertEqual(finished['preflight_checks'], receipt)
        self.assertEqual(finished['preflight_receipt_sha256'], digest(receipt))
        self.assertEqual(finished['check_plan']['project']['requires'], ['preflight'])
        self.assertEqual([event[3] for event in self.proc.events if event[0] == 'check'],
                         [self.package_check, self.preflight_check, self.project_check])
        self.assertEqual(self.proc.lanes, ['qa', 'security', 'review'])
        for asset in self.remote.rows[__version__]['assets']:
            if asset['name'] != 'sfc-release-manifest.json':
                self.assertEqual(self.remote.assets[asset['id']], (self.releases.artifact_paths() / asset['name']).read_bytes())

    def test_tampered_preflight_log_invalidates_receipt_without_repeating_commands(self):
        self.setup_release(preflight='true')
        self.stop_after_preflight()
        receipt = self.journal()['preflight_checks']
        Path(receipt['checks'][0]['log']).write_text('replaced preflight output')
        with self.assertRaisesRegex(Halt, 'Missing/changed pinned release preflight log'):
            self.run_scheduler(self.restart())
        journal = self.journal()
        self.assertEqual(journal['preflight_checks'], receipt)
        self.assertEqual(journal['preflight_outcome']['status'], 'invalidated')
        self.assertEqual([event[3] for event in self.proc.events], [self.package_check, 'true'])
        self.assertEqual(self.proc.lanes, [])
        self.assertEqual(self.remote.writes, [])

    def test_manifest_mutation_in_review_stops_before_any_publication_write(self):
        self.setup_release(preflight='true')

        def mutate(lane, worktree):
            if lane == 'review':
                (Path(worktree).parent / 'artifacts' / 'sfc-release-manifest.json').write_text('{}\n')

        self.proc.on_lane = mutate
        with self.assertRaisesRegex(Halt, 'Reviewed release manifest bytes changed'):
            self.run_scheduler()
        self.assertEqual(self.proc.lanes, ['qa', 'security', 'review'])
        self.assertEqual(self.remote.writes, [])

    def test_uncertain_preflight_is_not_repeated_and_parallel_supervisor_cannot_admit(self):
        self.setup_release(preflight='true')
        executing = threading.Event()
        release = threading.Event()
        errors = []
        execute = self.proc.execute

        def interrupted(argv, *args, **kwargs):
            if argv == ['sh', '-c', 'true']:
                executing.set()
                if not release.wait(15):
                    raise AssertionError('fixture did not release the running preflight')
                raise Stopped('interrupted preflight')
            return execute(argv, *args, **kwargs)

        def first_supervisor():
            try:
                self.run_scheduler()
            except Exception as exc:
                errors.append(exc)

        with patch.object(self.proc, 'execute', side_effect=interrupted):
            owner = threading.Thread(target=first_supervisor)
            owner.start()
            try:
                self.assertTrue(executing.wait(15))
                with self.assertRaises(Busy):
                    self.run_scheduler(self.restart())
            finally:
                release.set()
                owner.join(15)
        self.assertFalse(owner.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], Stopped)
        receipt = self.journal()['preflight_checks']
        self.assertEqual(receipt['state'], 'reserved')
        with self.assertRaisesRegex(Halt, 'preflight previously failed/interrupted'):
            self.run_scheduler(self.restart())
        self.assertEqual(self.journal()['preflight_checks'], receipt)
        self.assertEqual(self.factory.record(self.remote.card(1))['attempts'], 1)
        self.assertEqual(self.proc.lanes, [])
        self.assertEqual(self.remote.writes, [])


if __name__ == '__main__':
    unittest.main()
