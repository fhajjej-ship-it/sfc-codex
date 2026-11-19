"""Check that the documented packaging commands preserve source-only evidence."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from package_plugin import ROOT, contents
from sfc_lib import verification
from sfc_lib.common import git
from sfc_lib.factory import Factory
from test_factory import BASE_CONFIG, issue


class PackageBytecodeTests(unittest.TestCase):
    def test_packaging_and_install_verification_preserve_source_inputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            for name, data in contents().items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            (root / '.gitignore').write_bytes((ROOT / '.gitignore').read_bytes())
            git(root, 'init', '-b', 'main')
            git(root, 'add', '.')
            git(root, '-c', 'user.name=SFC package fixture', '-c', 'user.email=fixture@example.invalid',
                'commit', '-m', 'Pin portable package source')
            head = git(root, 'rev-parse', 'HEAD')
            command = 'python3 scripts/validate_plugin.py'
            factory = Factory(root, {**BASE_CONFIG, 'verify_commands': [command], 'verification_reuse': [{
                'command': command, 'max_age_seconds': 3600, 'environment': ['PATH'],
                'tools': [sys.executable], 'dependencies': [], 'artifacts': [], 'external_state': 'none',
                'worktree': 'independent', 'scope': 'repository', 'context': 'source-only', 'outputs': ['dist'],
            }]}, github=object())
            card = issue()
            before, reason = verification.binding(factory, card, root, head, command)
            self.assertIsNotNone(before, reason)
            self.assertEqual(before['untracked_inputs'], {})

            environment = os.environ.copy()
            for key in ('PYTHONDONTWRITEBYTECODE', 'PYTHONPYCACHEPREFIX', 'PYTHONPATH'):
                environment.pop(key, None)

            results = {}
            for script in ('package_plugin.py', 'verify_release_package.py'):
                with self.subTest(script=script):
                    run = subprocess.run([sys.executable, 'scripts/' + script], cwd=root, env=environment,
                                         capture_output=True, text=True, check=True, timeout=90)
                    results[script] = json.loads(run.stdout)
                    self.assertEqual(list(root.rglob('__pycache__')), [])
                    self.assertEqual(list(root.rglob('*.pyc')), [])
                    after, reason = verification.binding(factory, card, root, head, command)
                    self.assertEqual(after, before, reason)

            packaged = results['package_plugin.py']
            verified = results['verify_release_package.py']
            archive = Path(packaged['archive'])
            self.assertEqual(archive.parent, root / 'dist')
            self.assertEqual(packaged['sha256'], verification.checksum(archive))
            self.assertTrue(verified['valid'])
            self.assertEqual(verified['sha256'], packaged['sha256'])
            self.assertEqual([row['exit_code'] for row in verified['checks']], [0] * 7)
            smoke = verified['checks'][-1]
            self.assertEqual(smoke['command'][1:4], ['-I', '-B', '-c'])
            self.assertEqual(json.loads(smoke['stdout']), {
                'check': 'packaged-ticket-runtime',
                'source': 'extracted archive',
                'shared_validator_callers': ['factory', 'priority'],
                'populated_criterion': 'accepted',
                'empty_checkbox_before_next_section': 'rejected',
            })

            subprocess.run([sys.executable, '-c', 'import sys\nassert not sys.dont_write_bytecode\n'
                            'import package_plugin, verify_release_package\nassert not sys.dont_write_bytecode'],
                           cwd=root / 'scripts', env=environment, capture_output=True, check=True, timeout=10)


if __name__ == '__main__':
    unittest.main()
