"""Ensure the shared seed supplies independent writable Git fixtures."""
import unittest

import test_factory as fixtures


class FactorySeedTests(unittest.TestCase):
    def test_seed_copies_keep_worktrees_objects_and_remotes_isolated(self):
        def new_fixture():
            fixture = fixtures.IntegrationTests()
            fixture.setUp()
            self.addCleanup(fixture.tearDown)
            return fixture

        first, second = new_fixture(), new_fixture()
        git = fixtures.git
        initial = git(first.root, 'rev-parse', 'HEAD')
        for fixture in (first, second):
            self.assertEqual(git(fixture.root, 'remote', 'get-url', 'origin'), str(fixture.remote))
            self.assertEqual(git(fixture.root, 'rev-parse', 'refs/remotes/origin/main'), initial)
            self.assertEqual(git(fixture.remote, 'rev-parse', 'refs/heads/main'), initial)
            self.assertEqual(git(fixture.root, 'status', '--porcelain'), '')
            self.assertFalse((fixture.root / '.git/objects/info/alternates').exists())
            self.assertFalse((fixture.remote / 'objects/info/alternates').exists())
        for left, right in ((first.root / '.git/objects', second.root / '.git/objects'),
                            (first.remote / 'objects', second.remote / 'objects')):
            objects = [path for path in left.rglob('*') if path.is_file()]
            self.assertTrue(objects)
            for path in objects:
                copy = right / path.relative_to(left)
                self.assertFalse(path.samefile(copy))

        (first.root / 'value.txt').write_text('first fixture only\n')
        git(first.root, 'commit', '-am', 'Change one fixture')
        git(first.root, 'push', 'origin', 'main')
        self.assertNotEqual(git(first.remote, 'rev-parse', 'refs/heads/main'), initial)

        third = new_fixture()
        for fixture in (second, third):
            self.assertEqual((fixture.root / 'value.txt').read_text(), 'original\n')
            self.assertEqual(git(fixture.root, 'rev-parse', 'HEAD'), initial)
            self.assertEqual(git(fixture.remote, 'rev-parse', 'refs/heads/main'), initial)
