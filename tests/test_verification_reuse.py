"""Real commands in temporary repos, fake Codex/GitHub; no customer operations."""
from __future__ import annotations

import concurrent.futures
import copy
import os
from pathlib import Path
import shlex
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from sfc_lib.common import Halt, Stopped, TimedOut, git, validate_config
from sfc_lib.factory import Factory
from sfc_lib import verification as v
import test_lane_evidence as lanes
import test_local_merge as local


def contract(command):
    return {'command': command, 'max_age_seconds': 3600, 'environment': ['PATH'],
            'tools': [str(Path(sys.executable).resolve())], 'dependencies': [], 'artifacts': [],
            'external_state': 'none', 'worktree': 'independent'}


class ReuseTests(unittest.TestCase):
    setUp = lanes.LaneEvidenceTests.setUp
    tearDown = lanes.LaneEvidenceTests.tearDown

    def enable(self, **overrides):
        self.config['verification_reuse'] = [{**contract(self.config['verify_commands'][0]), **overrides}]
        self.factory = Factory(self.root, self.config, github=self.factory.gh, processes=self.processes)
        self.factory.run_dir = self.root / '.sfc/runs/fixture'

    def verify(self, wt=None, base=None):
        self.assertTrue(self.factory.verify(self.card, wt or self.root, base or self.head))
        return copy.deepcopy(self.factory.record(self.card)['verification'])

    def candidate(self):
        path = self.root / '.sfc/candidate'
        git(self.root, 'worktree', 'add', '--detach', path, self.head)
        return path

    def test_default_contract_absence_and_legacy_evidence_execute(self):
        a = self.verify()
        b = self.verify()
        self.assertNotIn('reuse', b['checks'][0])
        self.assertEqual(b['checks'][0]['reason'], 'command has no reuse contract')
        self.enable()
        self.factory.update(self.card, verification={**a, 'version': 0})
        self.assertNotIn('reuse', self.verify()['checks'][0])

    def test_cross_checkout_reuse_and_restart_retain_original_bytes(self):
        self.enable()
        original = self.verify()
        raw = Path(original['receipt_path']).read_bytes()
        candidate = self.candidate()
        self.factory = Factory(self.root, self.config, github=self.factory.gh, processes=self.processes)
        self.factory.run_dir = self.root / '.sfc/runs/restart'
        reused = self.verify(candidate)
        check = reused['checks'][0]
        self.assertEqual(check['reuse'], original['checks'][0])
        self.assertEqual(reused['worktree'], str(candidate.resolve()))
        self.assertEqual(Path(original['receipt_path']).read_bytes(), raw)
        v.validate_evidence(reused, candidate, self.factory, self.card, self.head)
        self.assertEqual(self.verify(candidate)['checks'][0]['reuse'], original['checks'][0])

    def test_changed_task_base_config_source_environment_tool_dependency_and_artifact_miss(self):
        for kind in ('task', 'base', 'config', 'source', 'environment', 'tool', 'dependency', 'artifact'):
            with self.subTest(kind=kind):
                self.tearDown()
                self.setUp()
                dep = self.root / '.sfc/dependency'
                dep.parent.mkdir(exist_ok=True)
                dep.write_text('one')
                artifact = self.root / '.sfc/artifact'
                artifact.write_text('one')
                tool = self.root / '.sfc/tool'
                tool.write_text('one')
                self.enable(dependencies=[str(dep)], artifacts=[str(artifact)], tools=[str(tool)])
                self.verify()
                if kind == 'task': self.card['body'] += 'new acceptance criterion'
                if kind == 'config': self.factory.config_digest = 'changed'
                if kind == 'base': self.head = '0' * 40
                if kind == 'source':
                    (self.root / 'new.txt').write_text('new')
                    git(self.root, 'add', 'new.txt')
                    git(self.root, 'commit', '-m', 'new source')
                if kind in ('dependency', 'artifact', 'tool'):
                    {'dependency': dep, 'artifact': artifact, 'tool': tool}[kind].write_text('two')
                with patch.dict(os.environ, {'PATH': os.environ['PATH'] + (':/changed' if kind == 'environment' else '')}):
                    self.assertNotIn('reuse', self.verify()['checks'][0])

    def test_ignored_nested_state_repositories_do_not_disable_reuse(self):
        # Generated fixtures can contain arbitrary/symlink inputs. Git emits
        # these nested repos even with a literal directory pathspec exclusion.
        for name in ('.sfc/generated/repo', 'dist/generated/repo'):
            nested = self.root / name
            nested.mkdir(parents=True)
            git(nested, 'init', '-q')
            (nested / 'dangling').symlink_to('absent')
        self.enable(outputs=['dist'])
        first = self.verify()
        self.assertIsNotNone(first['checks'][0]['binding'])
        (self.root / '.sfc/generated/repo/new-output').write_text('new fixture state')
        self.assertEqual(self.verify()['checks'][0]['reuse'], first['checks'][0])
        # Prefix lookalikes outside the excluded directories are still pinned.
        for name in ('.sfc-other', 'dist-other'):
            with self.subTest(name=name):
                external = self.root / name
                external.mkdir()
                (external / 'input').write_text('behavioral input')
                self.assertNotIn('reuse', self.verify()['checks'][0])
        (self.root / 'dist-other/unsupported').symlink_to('absent')
        self.assertIsNone(v.binding(self.factory, self.card, self.root, self.head,
                                   self.config['verify_commands'][0])[0])

    def test_expiry_failure_and_receipt_log_tampering_require_execution(self):
        self.enable()
        for kind in ('expired', 'receipt', 'log', 'failed'):
            with self.subTest(kind=kind):
                self.factory.update(self.card, verification={})
                old = self.verify()
                check = old['checks'][0]
                if kind == 'expired':
                    from datetime import datetime, timezone, timedelta
                    class Future(datetime):
                        @classmethod
                        def now(cls, tz=None): return datetime.now(timezone.utc) + timedelta(hours=2)
                    with patch.object(v.dt, 'datetime', Future):
                        self.assertNotIn('reuse', self.verify()['checks'][0])
                    continue
                if kind == 'receipt': Path(check['receipt_path']).write_text('{}')
                if kind == 'log': Path(check['log']).write_text('tampered')
                if kind == 'failed': self.factory.record(self.card)['verification']['passed'] = False
                self.assertNotIn('reuse', self.verify()['checks'][0])

    def test_historical_verification_checks_evidence_age_at_merge_time(self):
        from datetime import datetime, timedelta, timezone

        self.enable()
        receipt = self.verify()
        check = v.load(receipt['checks'][0])
        finished = datetime.fromisoformat(check['finished'])
        valid_at = (finished + timedelta(minutes=5)).isoformat()
        expired_at = (finished + timedelta(hours=2)).isoformat()

        class Future(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.now(timezone.utc) + timedelta(hours=3)

        with patch.object(v.dt, 'datetime', Future):
            v.validate_evidence(receipt, self.root, self.factory, self.card, self.head, at=valid_at)
            with self.assertRaisesRegex(ValueError, 'expired'):
                v.validate_evidence(receipt, self.root, self.factory, self.card, self.head, at=expired_at)
            with self.assertRaisesRegex(ValueError, 'expired'):
                v.validate_evidence(receipt, self.root, self.factory, self.card, self.head)

    def test_directory_root_mode_changes_invalidate_dependency_and_artifact(self):
        for field in ('dependencies', 'artifacts'):
            with self.subTest(field=field):
                directory = self.root / '.sfc' / field
                directory.mkdir(parents=True)
                (directory / 'unchanged').write_text('same bytes')
                directory.chmod(0o755)
                self.enable(**{field: [str(directory)]})
                original = self.verify()
                try:
                    directory.chmod(0o555)
                    current, _ = v.binding(self.factory, self.card, self.root, self.head,
                                           self.config['verify_commands'][0])
                    self.assertIsNone(v.matching(original['checks'][0], current, self.root)[0])
                    self.assertNotIn('reuse', self.verify()['checks'][0])
                finally:
                    directory.chmod(0o755)

    def test_special_tool_files_miss_without_blocking(self):
        fifo = self.root / '.sfc/tool-fifo'
        fifo.parent.mkdir(exist_ok=True)
        os.mkfifo(fifo)
        # The external timeout makes a regression fail rather than hang this suite.
        script = '''
import sys
from types import SimpleNamespace
from sfc_lib import verification as v
f = SimpleNamespace(processes=SimpleNamespace(stopped=lambda: False),
    c={'verification_reuse': [{'command': 'check', 'external_state': 'none',
    'worktree': 'independent', 'tools': [sys.argv[2]]}]})
assert v.binding(f, {}, sys.argv[1], 'base', 'check')[0] is None
'''
        for tool in ('/dev/zero', str(fifo), str(fifo.parent)):
            with self.subTest(tool=tool):
                result = subprocess.run([sys.executable, '-c', script, str(self.root), tool],
                                        env={**os.environ, 'PYTHONPATH': str(Path(v.__file__).parents[1])},
                                        capture_output=True, text=True, timeout=3)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_fingerprinting_stop_and_timeout_never_publish_success(self):
        self.enable()
        self.verify()
        real_hash = v.hashlib.sha256
        for failure in ('stop', 'timeout'):
            with self.subTest(failure=failure):
                clock = [0]
                def hashing(*args, **kwargs):
                    result = real_hash(*args, **kwargs)
                    class InterruptingHash:
                        def update(inner, data):
                            result.update(data)
                            # A tool chunk was read; interrupt before the next.
                            if failure == 'stop':
                                self.processes.stop_path.touch()
                            else:
                                clock[0] = self.config['verify_timeout'] + 1
                        def hexdigest(inner):
                            return result.hexdigest()
                    return InterruptingHash()
                try:
                    with patch.object(v.time, 'monotonic', side_effect=lambda: clock[0]):
                        with patch.object(v.hashlib, 'sha256', side_effect=hashing):
                            with self.assertRaises(Stopped if failure == 'stop' else TimedOut):
                                v.binding(self.factory, self.card, self.root, self.head,
                                          self.config['verify_commands'][0])
                    with patch.object(v, 'binding', side_effect=Stopped('fingerprint stop')):
                        with self.assertRaises(Stopped):
                            self.factory.verify(self.card, self.root, self.head)
                    self.assertEqual(self.factory.record(self.card)['verification'], {})
                finally:
                    self.processes.stop_path.unlink(missing_ok=True)

    def test_checksum_rejects_raced_in_fifo_descriptor(self):
        file = self.root / '.sfc/input'
        file.parent.mkdir(exist_ok=True)
        file.write_text('regular at initial stat')
        fifo = file.with_name('fifo')
        os.mkfifo(fifo)
        opened = os.open
        def replaced(path, flags):
            self.assertTrue(flags & os.O_NONBLOCK)
            return opened(fifo, flags)
        with patch.object(v.os, 'open', side_effect=replaced):
            with self.assertRaisesRegex(ValueError, 'nonregular'):
                v.checksum(file)

    def test_external_state_checkout_scope_and_unknown_bindings_never_reuse(self):
        for overrides in ({'external_state': 'network'}, {'worktree': 'unknown'},
                          {'dependencies': ['/nonexistent/sfc-dependency']}):
            with self.subTest(overrides=overrides):
                self.enable(**overrides)
                self.verify()
                self.assertNotIn('reuse', self.verify()['checks'][0])
        self.enable(worktree='path')
        self.verify()
        self.assertNotIn('reuse', self.verify(self.candidate())['checks'][0])

    def test_interruption_invalidates_old_evidence_and_restart_executes(self):
        self.enable()
        old = self.verify()
        self.card['body'] += 'changed'
        with patch.object(self.processes, 'execute', side_effect=Stopped('interrupted')):
            with self.assertRaises(Stopped): self.factory.verify(self.card, self.root, self.head)
        self.assertEqual(self.factory.record(self.card)['verification'], {})
        self.assertTrue(list(self.factory.run_dir.glob('issue-1/verify-*/interruption.json')))
        self.assertTrue(Path(old['receipt_path']).is_file())
        self.factory = Factory(self.root, self.config, github=self.factory.gh, processes=self.processes)
        self.factory.run_dir = self.root / '.sfc/runs/restart'
        self.assertNotIn('reuse', self.verify()['checks'][0])

    def test_concurrent_duplicate_attempt_executes_once(self):
        self.enable()
        execute = self.processes.execute
        count = []
        barrier = threading.Barrier(2)
        def counted(*args, **kwargs):
            count.append(1)
            return execute(*args, **kwargs)
        def attempt():
            barrier.wait()
            return self.factory.verify(self.card, self.root, self.head)
        with patch.object(self.processes, 'execute', side_effect=counted):
            with concurrent.futures.ThreadPoolExecutor(2) as pool:
                self.assertEqual(list(pool.map(lambda _: attempt(), range(2))), [True, True])
        self.assertEqual(len(count), 1)
        self.assertIn('reuse', self.factory.record(self.card)['verification']['checks'][0])

    def test_mutating_command_cannot_publish_success(self):
        self.config['verify_commands'] = ['echo changed >> .gitignore']
        self.enable()
        with self.assertRaisesRegex(Halt, 'bindings changed'):
            self.factory.verify(self.card, self.root, self.head)
        self.assertEqual(self.factory.record(self.card)['verification'], {})

    def test_index_flags_cannot_hide_source_changes_from_reuse(self):
        self.enable()
        self.verify()
        git(self.root, 'update-index', '--assume-unchanged', '.gitignore')
        with (self.root / '.gitignore').open('a') as stream:
            stream.write('hidden-source-change\n')
        self.assertEqual(git(self.root, 'status', '--porcelain'), '')
        self.assertNotIn('reuse', self.verify()['checks'][0])

    def test_per_command_partial_hit_and_sanitized_environment(self):
        first = self.config['verify_commands'][0]
        second = shlex.join([sys.executable, '-c', "import os; assert 'SFC_UNBOUND_TEST' not in os.environ"])
        self.config['verify_commands'] = [first, second]
        self.enable()
        self.config['verification_reuse'].append(contract(second))
        self.factory = Factory(self.root, self.config, self.factory.gh, self.processes)
        self.factory.run_dir = self.root / '.sfc/runs/fixture'
        with patch.dict(os.environ, {'SFC_UNBOUND_TEST': 'not inherited'}):
            self.verify()
            reused = self.verify()
        self.assertTrue(all(c.get('reuse') for c in reused['checks']))
        Path(reused['checks'][1]['log']).write_text('changed raw log')
        changed = self.verify()
        self.assertIn('reuse', changed['checks'][0])
        self.assertNotIn('reuse', changed['checks'][1])

    def test_unavailable_binding_keeps_the_command_environment_allowlist(self):
        self.config['verify_commands'] = [shlex.join([
            sys.executable, '-c', "import os; assert 'PATH' in os.environ; "
            "assert 'SFC_UNBOUND_TEST' not in os.environ"
        ])]
        self.enable()
        with patch.dict(os.environ, {'SFC_UNBOUND_TEST': 'synthetic excluded variable'}), \
                patch.object(v, 'binding', return_value=(None, 'fixture binding unavailable')):
            check = self.verify()['checks'][0]
        self.assertIsNone(check['binding'])
        self.assertNotIn('reuse', check)
        self.assertEqual(check['reason'], 'fixture binding unavailable')

    def test_policy_validation_rejects_incomplete_and_secret_environment(self):
        self.enable()
        for change in ({'environment': ['API_KEY']}, {'tools': []}, {'max_age_seconds': 0}):
            with self.subTest(change=change), self.assertRaises(Halt):
                validate_config({**self.config, 'verification_reuse': [{**contract(self.config['verify_commands'][0]), **change}]})


class FlowTests(unittest.TestCase):
    setUp = local.LocalMergeTests.setUp
    tearDown = local.LocalMergeTests.tearDown
    prepare_review = local.LocalMergeTests.prepare_review

    def enable(self):
        self.c['verification_reuse'] = [contract(self.c['verify_commands'][0])]
        self.factory = Factory(self.root, self.c, self.gh, self.proc)

    def test_representative_flow_reuses_checks_keeps_all_independent_lanes(self):
        self.enable()
        self.assertEqual(self.factory.run(allow_merge=True)['outcome'], 'drained')
        record = self.factory.record(self.gh.items[0])
        self.assertEqual(self.proc.lanes, ['build', 'qa', 'security', 'review'])
        self.assertEqual(len([e for e in self.proc.events if e[0] == 'check']), 1)
        self.assertEqual(record['local_merge']['phase'], 'done')
        check = record['verification']['checks'][0]
        self.assertIn('reuse', check)
        self.assertIn('issue-1', check['reuse']['worktree'])
        self.assertEqual(len(self.gh.attempts), 1)

    def test_reused_gate_rechecks_ci_and_merge_authority(self):
        for change in ('ci', 'authority', 'log'):
            with self.subTest(change=change):
                self.tearDown()
                self.setUp()
                self.enable()
                card, wt = self.prepare_review()
                self.factory.allow_merge = True
                original = self.factory.verify
                def verify(card, worktree, base):
                    passed = original(card, worktree, base)
                    if 'local-gate-' in str(worktree):
                        if change == 'ci': self.gh.ci = []
                        if change == 'authority': self.factory.allow_merge = False
                        if change == 'log':
                            Path(self.factory.record(card)['verification']['checks'][0]['log']).write_text('tampered')
                    return passed
                with patch.object(self.factory, 'verify', side_effect=verify):
                    if change == 'ci': self.factory.review(card, wt)
                    else:
                        with self.assertRaises(Halt): self.factory.review(card, wt)
                self.assertEqual(self.gh.attempts, [])

    def test_protected_gate_reuses_identical_source_and_keeps_all_lanes(self):
        self.enable()
        self.c['merge_mode'] = 'github-protected'
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        self.assertEqual(self.factory.run(allow_merge=True)['outcome'], 'drained')
        self.assertEqual(self.proc.lanes, ['build', 'qa', 'security', 'review'])
        self.assertEqual(len([e for e in self.proc.events if e[0] == 'check']), 1)
        self.assertEqual(len(self.gh.merges), 1)
        self.assertEqual(self.gh.attempts, [])

    def test_changed_binding_at_gate_executes_fresh_and_base_movement_still_blocks(self):
        self.enable()
        card, wt = self.prepare_review()
        self.factory.allow_merge = True
        original = self.factory.verify
        def verify(card, worktree, base):
            with patch.dict(os.environ, {'PATH': os.environ['PATH'] + ':/gate-environment'}):
                passed = original(card, worktree, base)
            if 'local-gate-' in str(worktree):
                # Real remote branch movement after checking, no customer repo.
                git(self.remote, 'update-ref', 'refs/heads/main', git(wt, 'rev-parse', 'HEAD'))
            return passed
        with patch.object(self.factory, 'verify', side_effect=verify):
            self.factory.review(card, wt)
        self.assertEqual(len([e for e in self.proc.events if e[0] == 'check']), 2)
        self.assertEqual(self.gh.attempts, [])
        self.assertEqual(card['status'], 'QA')
