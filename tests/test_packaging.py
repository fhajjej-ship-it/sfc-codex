"""Host-compatible exports and explicit installation with a fake native CLI."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from package_plugin import ROOT, contents, package
from install_development_plugin import install
from sfc_lib import __version__
from sfc_lib.common import Busy, exclusive, git

DEVELOPMENT_VERSION = '0.0.0-dev.fixture'


class PackagingTests(unittest.TestCase):
    def test_compat_export_uses_only_native_resolved_path_and_retains_project_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            installed = Path(tmp).resolve() / 'native output with spaces'
            original = contents()
            exported = contents(codex_root=installed)
            self.assertNotIn('plugin.json', exported)
            self.assertIn('.codex-plugin/plugin.json', exported)
            server = json.loads(exported['.mcp.json'])['mcpServers']['sfc_session']
            self.assertEqual(server['args'], ['-B', str(installed / 'scripts/session_mcp.py')])
            self.assertNotIn('cwd', server)
            self.assertEqual(server['env_vars'], ['SFC_WORKER', 'SFC_TEST_CONTEXT'])
            for name in exported:
                if name != '.mcp.json':
                    self.assertEqual(exported[name], original[name], name)
            archive = Path(tmp) / 'candidate.zip'
            result = package(archive, exported)
            self.assertTrue(result['sha256'])
            with zipfile.ZipFile(archive) as z:
                self.assertNotIn('sfc/plugin.json', z.namelist())
                self.assertEqual(z.read('sfc/.mcp.json'), exported['.mcp.json'])
            with self.assertRaises(FileExistsError):
                package(archive, exported)
            installed.mkdir()
            for name, data in exported.items():
                target = installed / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            validated = subprocess.run([sys.executable, str(installed / 'scripts/validate_plugin.py')],
                                       capture_output=True, text=True, check=True, timeout=20)
            self.assertEqual(json.loads(validated.stdout)['format'], 'codex-resolved')
            wrong = json.loads(exported['.mcp.json'])
            wrong['mcpServers']['sfc_session']['args'] = ['/another/location/session_mcp.py']
            (installed / '.mcp.json').write_text(json.dumps(wrong))
            invalid = subprocess.run([sys.executable, str(installed / 'scripts/validate_plugin.py')],
                                     capture_output=True, text=True, timeout=20)
            self.assertNotEqual(invalid.returncode, 0)

    def test_relative_install_root_cannot_silently_bind_to_source_or_chat(self):
        with self.assertRaisesRegex(ValueError, 'absolute'):
            contents(codex_root='relative')

    def install_fixture(self, *, fail_at=None, wrong_final=False, fresh=False, registered=False, disabled=False,
                        version=__version__, public=False):
        temporary = tempfile.TemporaryDirectory(prefix='sfc-package-')
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        for name, data in contents().items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        # Development-install tests need a development package even when the
        # source under test is a stable release. Public install tests retain
        # the actual source version by default.
        for name in ('plugin.json', '.codex-plugin/plugin.json'):
            path = root / name
            manifest = json.loads(path.read_text())
            manifest['version'] = version
            path.write_text(json.dumps(manifest, indent=2) + '\n')
        runtime = root / 'scripts/sfc_lib/__init__.py'
        runtime.write_text(runtime.read_text().replace(__version__, version))
        native = root / 'opaque-native-location'
        calls = []
        stage = root / ('dist/native-install' if fresh or public else 'dist/development') / version
        adds = 0

        def cli(args):
            nonlocal adds
            calls.append(args)
            if fail_at and len(calls) == fail_at:
                raise RuntimeError('Unknown native outcome; inspect, never retry')
            if args[:2] == ['plugin', 'list']:
                return {'installed': [] if fresh and not adds else [
                    {'pluginId': 'sfc@sfc-local', 'version': version if adds else '0.1.13-dev.1', 'enabled': not disabled}]}
            if args[:3] == ['plugin', 'marketplace', 'list']:
                return {'marketplaces': [{'name': 'unrelated', 'root': '/retained-source'}] +
                        ([{'name': 'sfc-local', 'root': '/previous-sfc-source'}] if registered else [])}
            if args[:2] == ['plugin', 'remove']:
                if native.exists():
                    shutil.rmtree(native)  # Fake CLI owns only this temporary cache.
                return {'pluginId': 'sfc@sfc-local'}
            if args[:3] == ['plugin', 'marketplace', 'remove']:
                return {'marketplaceName': 'sfc-local'}
            if args[:3] == ['plugin', 'marketplace', 'add']:
                self.assertEqual(Path(args[3]), stage)
                return {'marketplaceName': 'sfc-local'}
            self.assertEqual(args, ['plugin', 'add', 'sfc@sfc-local'])
            adds += 1
            payload = stage / 'sfc'
            if adds == 1:
                self.assertEqual([str(p.relative_to(payload)) for p in payload.rglob('*') if p.is_file()],
                                 ['.codex-plugin/plugin.json'])
            shutil.copytree(payload, native)
            return {'pluginId': 'sfc@sfc-local', 'version': version,
                    'installedPath': str(native) + ('-changed' if wrong_final and adds == 2 else '')}
        return root, native, calls, cli

    def test_actual_prior_validator_is_rejected_before_any_native_replacement(self):
        fixture = ROOT / 'tests/fixtures/pre-native-validator'
        provenance = json.loads((fixture / 'provenance.json').read_text())
        import hashlib
        for allow_fresh in (False, True):
            with self.subTest(public_installer=allow_fresh):
                root, native, calls, cli = self.install_fixture(
                    version=provenance['source_version'], public=allow_fresh)
                for name, digest in provenance['files'].items():
                    data = (fixture / name).read_bytes()
                    self.assertEqual(hashlib.sha256(data).hexdigest(), digest)
                    (root / name).write_bytes(data)
                native.mkdir()
                sentinel = native / 'current-runtime.py'
                sentinel.write_bytes(b'preserve the installed runtime')
                with self.assertRaisesRegex(ValueError, 'native-format validation'):
                    install('fake-codex', root, call=cli, allow_fresh=allow_fresh)
                self.assertEqual(calls, [])
                self.assertEqual(sentinel.read_bytes(), b'preserve the installed runtime')
                self.assertFalse((root / 'dist').exists())

    def test_supported_prior_launcher_keeps_its_own_native_validator_contract(self):
        root, native, calls, cli = self.install_fixture(version='0.1.18', public=True)
        manifest = root / '.mcp.json'
        config = json.loads(manifest.read_text())
        config['mcpServers']['sfc_session']['args'].remove('-B')
        manifest.write_text(json.dumps(config))
        # Model the supported pre-bytecode-flag package and its strict validator.
        validator = root / 'scripts/validate_plugin.py'
        source = validator.read_text()
        self.assertIn("'args': ['-B', launch]", source)
        validator.write_text(source.replace("'args': ['-B', launch]", "'args': [launch]"))
        result = install('fake-codex', root, call=cli, allow_fresh=True)
        self.assertEqual(result['installedPath'], str(native))
        server = json.loads((native / '.mcp.json').read_text())['mcpServers']['sfc_session']
        self.assertEqual(server['args'], [str(native / 'scripts/session_mcp.py')])
        self.assertEqual(json.loads(Path(result['receipt']).read_text())['state'], 'verified')

    def test_two_phase_native_install_never_writes_the_real_cache_or_guesses_its_layout(self):
        root, native, calls, cli = self.install_fixture(version=DEVELOPMENT_VERSION)
        result = install('fake-codex', root, call=cli)
        self.assertEqual(result['installedPath'], str(native))
        receipt = json.loads(Path(result['receipt']).read_text())
        self.assertEqual(receipt['state'], 'verified')
        self.assertFalse(receipt['hook_trust_changed'])
        self.assertFalse(receipt['project_authorization_changed'])
        self.assertFalse((native / 'plugin.json').exists())
        server = json.loads((native / '.mcp.json').read_text())['mcpServers']['sfc_session']
        self.assertEqual(server['args'], ['-B', str(native / 'scripts/session_mcp.py')])
        before = len(calls)
        with self.assertRaisesRegex(ValueError, 'journal'):
            install('fake-codex', root, call=cli)
        self.assertEqual(len(calls), before)
        self.assertFalse((root / '.git/sfc-autostart.json').exists())

    def test_configured_project_update_refuses_busy_runner_and_holds_lock_through_replacement(self):
        root, native, calls, cli = self.install_fixture(version=DEVELOPMENT_VERSION)
        git(root, 'init')
        (root / '.sfc').mkdir()
        (root / '.sfc/config.json').write_text('{}')
        native.mkdir()
        sentinel = native / 'old-runtime.py'
        sentinel.write_text('retained until the supervisor stops')
        with exclusive(root), self.assertRaises(Busy):
            install('fake-codex', root, call=cli)
        self.assertEqual(calls, [])
        self.assertTrue(sentinel.exists())
        self.assertFalse((root / 'dist/development' / DEVELOPMENT_VERSION).exists())

        def locked_cli(args):
            with self.assertRaises(Busy):
                with exclusive(root):
                    pass
            return cli(args)

        result = install('fake-codex', root, call=locked_cli)
        self.assertEqual(json.loads(Path(result['receipt']).read_text())['state'], 'verified')

    def test_unknown_native_outcome_latches_without_retry(self):
        root, native, calls, cli = self.install_fixture(fail_at=4, version=DEVELOPMENT_VERSION)
        with self.assertRaisesRegex(RuntimeError, 'Unknown native outcome'):
            install('fake-codex', root, call=cli)
        self.assertEqual(len(calls), 4)
        path = root / 'dist/development' / DEVELOPMENT_VERSION / 'install-receipt.json'
        self.assertEqual(json.loads(path.read_text())['state'], 'register_intent')
        with self.assertRaisesRegex(ValueError, 'journal'):
            install('fake-codex', root, call=cli)
        self.assertEqual(len(calls), 4)

    def test_changed_native_location_halts_without_rewriting_or_reinstalling(self):
        root, native, calls, cli = self.install_fixture(wrong_final=True, version=DEVELOPMENT_VERSION)
        with self.assertRaisesRegex(ValueError, 'location changed'):
            install('fake-codex', root, call=cli)
        self.assertEqual(len([c for c in calls if c == ['plugin', 'add', 'sfc@sfc-local']]), 2)

    def test_development_installer_rejects_stable_package_before_native_operations(self):
        root, native, calls, cli = self.install_fixture(version='1.2.3')
        native.mkdir()
        sentinel = native / 'existing-runtime.py'
        sentinel.write_bytes(b'preserve the installed plugin')
        with self.assertRaisesRegex(ValueError, 'development installation'):
            install('fake-codex', root, call=cli)
        self.assertEqual(calls, [])
        self.assertEqual(sentinel.read_bytes(), b'preserve the installed plugin')
        self.assertFalse((root / 'dist').exists())

    def test_explicit_first_install_needs_no_previous_sfc_and_preserves_other_marketplaces(self):
        root, native, calls, cli = self.install_fixture(fresh=True)
        result = install('fake-codex', root, call=cli, allow_fresh=True)
        receipt = json.loads(Path(result['receipt']).read_text())
        self.assertEqual(receipt['state'], 'verified')
        self.assertNotIn(['plugin', 'marketplace', 'remove', 'unrelated'], calls)
        self.assertNotIn(['plugin', 'marketplace', 'remove', 'sfc-local'], calls)
        self.assertEqual(len([c for c in calls if c == ['plugin', 'remove', 'sfc@sfc-local']]), 1)
        self.assertFalse(receipt['hook_trust_changed'])
        self.assertFalse(receipt['project_authorization_changed'])
        self.assertFalse((root / '.sfc').exists())
        self.assertEqual(json.loads((native / '.mcp.json').read_text())['mcpServers']['sfc_session']['args'],
                         ['-B', str(native / 'scripts/session_mcp.py')])

    def test_first_install_can_replace_only_its_previously_registered_empty_marketplace(self):
        root, native, calls, cli = self.install_fixture(fresh=True, registered=True)
        install('fake-codex', root, call=cli, allow_fresh=True)
        removes = [c for c in calls if c[:3] == ['plugin', 'marketplace', 'remove']]
        self.assertEqual(removes, [['plugin', 'marketplace', 'remove', 'sfc-local']])

    def test_user_installer_does_not_implicitly_enable_existing_disabled_sfc(self):
        root, native, calls, cli = self.install_fixture(disabled=True)
        with self.assertRaisesRegex(ValueError, 'enabled'):
            install('fake-codex', root, call=cli, allow_fresh=True)
        self.assertEqual(len(calls), 1)
        self.assertFalse(native.exists())


if __name__ == '__main__':
    unittest.main()
