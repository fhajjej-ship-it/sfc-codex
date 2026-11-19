"""Exercise the isolated Python runtime with temporary Git repositories."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from sfc_lib import efficiency_runtime as runtime
from sfc_lib.common import git


class CheckRuntimeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='sfc-check-runtime-')
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.root = self.directory / 'project'
        self.root.mkdir()
        git(self.root, 'init', '-b', 'main')
        self.descriptor = runtime.plan(self.root)
        if self.descriptor is None:
            self.skipTest('A matching system Python 3 executable and standard library are required')
        self.location = Path(self.descriptor['location'])

    def test_plan_does_not_create_runtime_and_children_have_no_system_or_user_site(self):
        self.assertFalse(self.location.exists())
        observation = runtime.install(self.descriptor, root=self.root)
        self.assertEqual(observation, runtime.install(self.descriptor))
        self.assertEqual(observation, runtime.inspect(self.descriptor, root=self.root))
        user_base = self.directory / 'custom-user-base'
        user_site = user_base / 'lib' / runtime.versioned_python(self.descriptor) / 'site-packages'
        user_site.mkdir(parents=True)
        (user_site / 'usercustomize.py').write_text('raise RuntimeError("user site was imported")\n')
        env = {**observation['environment'], 'PYTHONUSERBASE': str(user_base)}
        self.assertEqual([env[k] for k in ('GIT_CONFIG_NOSYSTEM', 'GIT_CONFIG_GLOBAL', 'GIT_CONFIG_SYSTEM')],
                         ['1', os.devnull, os.devnull])
        env.pop('PYTHONNOUSERSITE')  # venv itself must isolate subprocesses
        child = 'import json, site, sys; print(json.dumps([site.ENABLE_USER_SITE, site.getsitepackages(), sys.path]))'
        parent = ('import json, subprocess, sys; child = sys.argv[1]; '
                  'print(json.dumps([json.loads(subprocess.check_output([name, "-c", child], text=True)) '
                  'for name in ("python3", sys.executable)]))')
        data = subprocess.check_output(['python3', '-c', parent, child], cwd=self.root, env=env, text=True, timeout=30)
        for enabled, sites, paths in json.loads(data):
            self.assertIs(enabled, False)
            self.assertTrue(sites)
            self.assertTrue(all(self.location in Path(site).resolve().parents for site in sites))
            self.assertNotIn(str(user_site), paths)
            self.assertFalse(any('site-packages' in path and self.location not in Path(path).resolve().parents
                                 for path in paths))

    def test_site_hook_or_added_executable_invalidates_runtime(self):
        runtime.install(self.descriptor)
        site = next(self.location.glob('lib/python*/site-packages'))
        hook = site / 'unbound.pth'
        hook.write_text('import nonexistent_site_module\n')
        with self.assertRaisesRegex(ValueError, 'unbound site'):
            runtime.inspect(self.descriptor, root=self.root)
        hook.unlink()
        (self.location / 'bin' / 'git').write_text('# unexpected tool\n')
        with self.assertRaisesRegex(ValueError, 'executable inventory changed'):
            runtime.inspect(self.descriptor, root=self.root)

    def test_linked_checkout_shares_runtime_but_other_repository_cannot_use_it(self):
        git(self.root, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.test',
            'commit', '--allow-empty', '-m', 'Initial source')
        linked = self.directory / 'linked'
        git(self.root, 'worktree', 'add', '--detach', linked, 'HEAD')
        runtime.install(self.descriptor)
        self.assertEqual(runtime.plan(linked), self.descriptor)
        runtime.inspect(self.descriptor, root=linked)
        other = self.directory / 'unrelated'
        other.mkdir()
        git(other, 'init', '-b', 'main')
        with self.assertRaisesRegex(ValueError, 'different repository'):
            runtime.inspect(self.descriptor, root=other)
        with self.assertRaisesRegex(ValueError, 'different repository'):
            runtime.install(self.descriptor, root=other)


if __name__ == '__main__':
    unittest.main()
