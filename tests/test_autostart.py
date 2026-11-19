"""Fake host lifecycle replays through the real launcher and watcher."""
from contextlib import redirect_stdout
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import sfc
from sfc_lib import autostart
from sfc_lib.common import Halt, Processes, common_dir, exclusive, git, read_json, write_json
from sfc_lib.factory import Factory
from sfc_lib.watch import status as watch_status
import test_watch as watch_fixtures
import test_factory as fixtures


class AutoStartTests(unittest.TestCase):
    tearDown = fixtures.IntegrationTests.tearDown

    def setUp(self):
        watch_fixtures.WatchTests.setUp(self)
        self.root = self.root.resolve()
        self.c['watch'] = {**self.options, 'excluded_issues': [15], 'hold_label': 'custom-hold'}
        write_json(self.root / '.sfc/config.json', self.c)
        self.gh.dashboard = lambda: {'ready': True}
        self.event = {'hook_event_name': 'SessionStart', 'source': 'startup', 'session_id': 'chat-one',
                      'cwd': str(self.root), 'permission_mode': 'default'}

    def make_factory(self, root):
        return Factory(root, github=self.gh, processes=self.proc)

    def authorize(self, **kwargs):
        with exclusive(self.root):
            return autostart.configure(self.make_factory(self.root), enabled=True, **kwargs)

    def launch(self, **kwargs):
        return autostart.launch(self.event, environ={}, factory_type=self.make_factory,
                                clock=self.clock, wait=self.clock.wait, **kwargs)

    def auth(self):
        return read_json(common_dir(self.root) / 'sfc-autostart.json')

    def watcher(self):
        return read_json(common_dir(self.root) / 'sfc-watch.json')

    def finish(self, card):
        self.gh.move(card, 'Done')
        next(c for c in self.gh.items if c['number'] == card['number'])['state'] = 'CLOSED'

    def test_old_authorization_starts_without_acquiring_recovery_permission(self):
        from test_pickup import ticket

        auth = self.authorize()
        auth['scope']['watch'].pop('max_recoveries_per_ticket')
        write_json(common_dir(self.root) / 'sfc-autostart.json', auth)
        self.gh.items = [ticket()]
        with patch.object(Factory, 'process', side_effect=self.finish) as process:
            self.assertEqual(self.launch()['outcome'], 'watch-polls-limit')
        process.assert_called_once()
        self.assertEqual(self.watcher()['settings']['max_recoveries_per_ticket'], 0)
        self.assertNotIn('blocked_recovery_kinds', self.watcher()['policy'])
        self.assertEqual(self.auth()['scope'], auth['scope'])

    def test_old_cancelled_authorization_resumes_original_window(self):
        self.authorize()
        self.gh.items = []
        cancelled = threading.Event()
        def cancel_at_next_poll():
            self.clock.value = self.watcher()['next_poll']
            cancelled.set()
        self.clock.on_wait = cancel_at_next_poll
        self.assertEqual(self.launch(cancelled=cancelled.is_set)['outcome'], 'stopped')
        auth = self.auth()
        before = self.watcher()
        auth['scope']['watch'].pop('max_recoveries_per_ticket')
        before['settings'].pop('max_recoveries_per_ticket')
        before['policy'].pop('max_recoveries_per_ticket')
        before['policy'].pop('blocked_recovery_kinds')
        common = common_dir(self.root)
        write_json(common / 'sfc-autostart.json', auth)
        write_json(common / 'sfc-watch.json', before)
        self.clock.on_wait = None
        cancelled.clear()
        self.event['source'] = 'resume'
        self.assertEqual(self.launch(cancelled=cancelled.is_set)['outcome'], 'watch-polls-limit')
        after = self.watcher()
        for key in ('id', 'deadline', 'settings', 'policy', 'claims', 'cycles', 'no_progress'):
            self.assertEqual(after[key], before[key], key)
        self.assertGreater(after['polls'], before['polls'])

    def test_recovery_scope_cannot_change_before_watcher_lock(self):
        self.authorize()
        watch = Factory.watch

        def change_scope(factory, **kwargs):
            auth = self.auth()
            auth['scope']['watch'].pop('max_recoveries_per_ticket')
            write_json(common_dir(self.root) / 'sfc-autostart.json', auth)
            return watch(factory, **kwargs)

        with patch.object(Factory, 'watch', change_scope):
            self.assertEqual(self.launch()['state'], 'waiting')
        self.assertFalse((common_dir(self.root) / 'sfc-watch.json').exists())
        self.assertEqual(self.gh.writes, [])

    def test_unset_disabled_and_fake_untrusted_host_do_not_launch(self):
        before = (self.root / '.sfc/config.json').read_bytes()
        self.assertEqual(self.launch()['state'], 'disabled')
        with exclusive(self.root):
            autostart.configure(self.make_factory(self.root), enabled=False)
        self.assertEqual(self.launch()['state'], 'disabled')
        self.authorize()
        # A skipped hook has no invocation. SFC does not infer trust from JSON.
        def fake_host(trusted):
            return self.launch() if trusted else {'state': 'skipped-by-host'}
        self.assertEqual(fake_host(False)['state'], 'skipped-by-host')
        self.assertFalse((common_dir(self.root) / 'sfc-watch.json').exists())
        self.assertEqual(self.gh.writes + self.proc.events, [])
        self.assertEqual((self.root / '.sfc/config.json').read_bytes(), before)

    def check_start(self, source):
        from test_pickup import ticket
        self.event['source'] = source
        self.c['merge_policy'] = 'auto'
        write_json(self.root / '.sfc/config.json', self.c)
        self.authorize()
        self.gh.items = [ticket(1, status='Backlog'), ticket(15, priority='P0'),
                         ticket(3, labels=['custom-hold'])]
        with patch.object(Factory, 'process', side_effect=self.finish) as process:
            self.assertEqual(self.launch()['outcome'], 'watch-polls-limit')
        self.assertEqual(process.call_count, 1)
        self.assertFalse(self.watcher()['allow_merge'])
        self.assertEqual(self.watcher()['settings']['excluded_issues'], [15])
        self.assertEqual({n for n, _ in self.gh.writes}, {1})
        self.assertEqual(self.auth()['state'], 'finished')

    def test_startup_uses_real_watcher_preserving_scope_and_merge_authority(self):
        self.check_start('startup')

    def test_resume_uses_real_watcher_preserving_scope_and_merge_authority(self):
        self.check_start('resume')

    def test_duplicate_same_chat_cannot_renew_budgets_but_new_chat_can(self):
        self.authorize()
        with patch.object(Factory, 'process', side_effect=self.finish):
            self.launch()
        first = self.watcher()
        events = copy.deepcopy(self.proc.events)
        from sfc_lib.setup_review import review
        caps = review(self.root, self.c, automatic=self.auth(), dashboard=self.gh.dashboard())['capabilities']
        self.assertEqual((caps['watcher']['status'], caps['pickup']['status']), ('active', 'active'))
        self.assertIn('new eligible chat', caps['watcher']['reason'])
        for source in ('startup', 'resume'):
            self.event['source'] = source
            self.assertEqual(self.launch()['state'], 'waiting')
            self.assertEqual(self.watcher(), first)
        self.event['session_id'] = 'genuinely-new-chat'
        self.assertEqual(self.launch()['outcome'], 'watch-polls-limit')
        self.assertNotEqual(self.watcher()['id'], first['id'])
        self.assertEqual(read_json(self.root / '.sfc/watch-sessions' / (first['id'] + '.json')), first)
        self.assertEqual(self.proc.events, events)
        self.assertEqual(len(self.auth()['hosts']), 2)

    def test_reviewed_bounded_watcher_rejects_replaced_repository_before_new_session(self):
        identity = {'host': 'github.com', 'repository': 'example/app', 'repository_id': 'repo', 'project_id': 'p'}
        self.gh.onboarding_identity = lambda: identity
        self.authorize(reviewed_identity=identity)
        self.assertEqual(self.launch()['outcome'], 'watch-polls-limit')
        first = self.watcher()
        self.event['session_id'] = 'different-chat'
        self.gh.onboarding_identity = lambda: {**identity, 'repository_id': 'replacement-repo'}
        with patch.object(Factory, 'process') as worker:
            result = self.launch()
        self.assertEqual(result['state'], 'waiting')
        self.assertIn('identity changed', result['detail'])
        self.assertEqual(self.watcher(), first)
        worker.assert_not_called()

    def test_simultaneous_chats_and_linked_worktrees_share_lock_and_canonical_root(self):
        self.authorize()
        linked = self.root / '.sfc/worktrees/linked'
        git(self.root, 'worktree', 'add', '-b', 'linked-test', linked)
        (linked / 'subdirectory').mkdir()
        self.event['cwd'] = str(linked / 'subdirectory')
        self.assertEqual(autostart.configured_root(self.event['cwd']), self.root)
        before = self.auth()
        with exclusive(linked):
            result = self.launch()
        self.assertIn('Another SFC supervisor', result['detail'])
        self.assertEqual(self.auth(), before)
        entered, release = threading.Event(), threading.Event()
        results = []
        def process(card):
            entered.set()
            if not release.wait(5):
                raise RuntimeError('fixture wait expired')
            self.finish(card)
        with patch.object(Factory, 'process', side_effect=process):
            runner = threading.Thread(target=lambda: results.append(self.launch()))
            runner.start()
            try:
                self.assertTrue(entered.wait(5))
                self.assertIn('Another SFC supervisor', self.launch()['detail'])
            finally:
                release.set()
                runner.join(5)
        self.assertFalse(runner.is_alive())
        self.assertEqual(results[0]['outcome'], 'watch-polls-limit')
        self.assertEqual(self.watcher()['root'], str(self.root))
        self.assertEqual(self.watcher()['claims'], 1)

    def test_cancellation_resumes_original_budget_and_never_relaunches_after_expiry(self):
        self.authorize()
        cancelled = threading.Event()
        self.gh.items = []
        self.clock.on_wait = cancelled.set
        self.assertEqual(self.launch(cancelled=cancelled.is_set)['state'], 'waiting')
        first = self.watcher()
        self.assertEqual(self.auth()['state'], 'cancelled')
        self.assertIn('host interrupted', watch_status(self.root, self.clock)['explanation'])
        state = read_json(self.root / '.sfc/state.json')
        self.assertEqual(state['explanation'], watch_status(self.root, self.clock)['explanation'])
        cancelled.clear()
        self.clock.on_wait = None
        self.event['source'] = 'resume'
        self.launch(cancelled=cancelled.is_set)
        second = self.watcher()
        self.assertEqual(second['id'], first['id'])
        self.assertEqual(second['deadline'], first['deadline'])
        self.assertEqual(second['polls'], 3)
        self.assertEqual(second['claims'], first['claims'])
        self.assertIn('poll budget is exhausted', watch_status(self.root, self.clock)['explanation'])
        self.assertEqual(self.launch()['state'], 'waiting')
        self.assertEqual(self.watcher(), second)

    def test_operator_stop_latches_even_for_a_new_chat_until_explicit_rearm(self):
        self.authorize()
        self.gh.items = []
        self.clock.on_wait = lambda: (common_dir(self.root) / 'sfc-stop').touch()
        self.launch()
        self.assertEqual(self.auth()['state'], 'stopped')
        first = self.watcher()
        self.event['session_id'] = 'new-chat'
        self.launch()
        self.assertEqual(self.watcher(), first)
        self.clock.on_wait = None
        old_id = self.auth()['id']
        self.authorize()
        self.assertFalse((common_dir(self.root) / 'sfc-stop').exists())
        self.launch()
        self.assertNotEqual(self.watcher()['id'], first['id'])
        self.assertEqual(len(list((common_dir(self.root) / 'sfc-autostart-history').glob(old_id + '-*.json'))), 1)

    def test_cancellation_at_idle_boundary_resumes_original_spent_budget(self):
        self.authorize()
        cancelled = threading.Event()
        def cancel_at_next_poll():
            self.clock.value = self.watcher()['next_poll']
            cancelled.set()
        self.clock.on_wait = cancel_at_next_poll
        with patch.object(Factory, 'process', side_effect=self.finish) as process:
            self.assertEqual(self.launch(cancelled=cancelled.is_set)['outcome'], 'stopped')
        self.assertEqual(process.call_count, 1)
        first = self.watcher()
        self.assertEqual(first['polls'], 2)  # Completion-triggered refill also reads/spends a poll.
        self.assertEqual(first['claims'], 1)
        self.assertEqual(first['cycles'], 1)
        self.assertEqual(self.auth()['state'], 'cancelled')
        self.assertEqual(autostart.status(self.root)['last_event']['state'], 'cancelled')
        cancelled.clear()
        self.clock.on_wait = None
        self.event['source'] = 'resume'
        with patch.object(Factory, 'process') as process:
            self.assertEqual(self.launch(cancelled=cancelled.is_set)['outcome'], 'watch-polls-limit')
        process.assert_not_called()
        second = self.watcher()
        for key in ('id', 'deadline', 'claims', 'cycles', 'no_progress', 'settings', 'allow_merge'):
            self.assertEqual(second[key], first[key])
        self.assertEqual(second['polls'], 3)
        self.assertEqual(self.auth()['state'], 'finished')
        self.assertEqual(self.launch()['state'], 'waiting')
        self.assertEqual(self.watcher(), second)

    def test_operator_stop_overrides_host_cancellation_at_idle_boundary(self):
        self.proc = Processes(self.root / '.sfc/stop')
        for stop_path in (common_dir(self.root) / 'sfc-stop', self.root / '.sfc/stop'):
            with self.subTest(stop_path=stop_path.name):
                self.authorize()
                self.gh.items = []
                cancelled = threading.Event()
                def cancel_and_stop_at_next_poll():
                    self.clock.value = self.watcher()['next_poll']
                    cancelled.set()
                    stop_path.touch()
                self.clock.on_wait = cancel_and_stop_at_next_poll
                self.assertEqual(self.launch(cancelled=cancelled.is_set)['outcome'], 'stopped')
                self.assertEqual(self.auth()['state'], 'stopped')
                self.assertEqual(autostart.status(self.root)['last_event']['state'], 'stopped')
                first = self.watcher()
                self.assertFalse(first['interrupted'])
                explanation = watch_status(self.root, self.clock)['explanation']
                self.assertIn('Stop requested', explanation)
                self.assertNotIn('host interrupted', explanation)
                state = read_json(self.root / '.sfc/state.json')
                summary = read_json(self.root / '.sfc/runs' / state['run_id'] / 'summary.json')
                self.assertEqual(state['explanation'], explanation)
                self.assertEqual(summary['explanation'], explanation)
                cancelled.clear()
                self.clock.on_wait = None
                # Even removing the flag cannot erase the durable operator stop.
                stop_path.unlink()
                self.event['source'] = 'resume'
                self.event['session_id'] += '-new'
                self.assertEqual(self.launch()['state'], 'waiting')
                self.assertEqual(self.watcher(), first)

    def test_cancelled_active_child_is_owned_and_resume_keeps_claim_spending(self):
        self.authorize()
        self.proc = Processes(self.root / '.sfc/stop')
        cancelled = threading.Event()
        marker, log = self.root / 'active-child', self.root / '.sfc/active-child.log'
        def active(factory, card):
            self.proc.execute([sys.executable, '-c',
                               "from pathlib import Path; import time; Path('active-child').touch(); time.sleep(20)"],
                              self.root, log, 5)
        def cancel():
            import time
            deadline = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            cancelled.set()
        thread = threading.Thread(target=cancel)
        with patch.object(Factory, 'process', active):
            thread.start()
            try:
                self.launch(cancelled=cancelled.is_set)
            finally:
                thread.join(6)
        self.assertFalse(thread.is_alive())
        first = self.watcher()
        self.assertEqual(self.auth()['state'], 'cancelled')
        self.assertEqual(first['claims'], 1)
        self.assertEqual(self.proc.children, set())
        self.assertTrue(log.exists())
        self.assertFalse((self.root / '.sfc/stop').exists())
        cancelled.clear()
        self.gh.items = []
        self.event['source'] = 'resume'
        self.launch(cancelled=cancelled.is_set)
        self.assertEqual(self.watcher()['id'], first['id'])
        self.assertEqual(self.watcher()['deadline'], first['deadline'])
        self.assertEqual(self.watcher()['claims'], first['claims'])

    def test_cancelled_session_expiry_never_launches_or_renews_work(self):
        self.authorize()
        self.gh.items = []
        cancelled = threading.Event()
        self.clock.on_wait = cancelled.set
        self.launch(cancelled=cancelled.is_set)
        first = self.watcher()
        self.clock.value = first['deadline'] + 1
        self.clock.on_wait = None
        self.event['source'] = 'resume'
        self.assertEqual(self.launch()['outcome'], 'watch-time-limit')
        self.assertEqual(self.watcher()['id'], first['id'])
        for key in ('polls', 'claims', 'cycles', 'no_progress', 'deadline'):
            self.assertEqual(self.watcher()[key], first[key])
        self.assertEqual(self.proc.events, [])

    def test_preview_status_and_explicit_disable_preserve_other_settings(self):
        self.authorize()
        before = (self.root / '.sfc/config.json').read_bytes()
        auth = self.auth()
        for command in ('autostart', 'autostart-status'):
            with patch.object(sfc, 'Factory', side_effect=self.make_factory), redirect_stdout(io.StringIO()):
                self.assertEqual(sfc.main(['--root', str(self.root), command]), 0)
            self.assertEqual(self.auth(), auth)
        with patch.object(sfc, 'Factory', side_effect=self.make_factory), redirect_stdout(io.StringIO()):
            self.assertEqual(sfc.main(['--root', str(self.root), 'autostart', '--disable']), 0)
        self.assertFalse(self.auth()['enabled'])
        self.assertEqual(self.launch()['state'], 'disabled')
        history = common_dir(self.root) / 'sfc-autostart-history'
        first = {p.name: p.read_bytes() for p in history.glob('*.json')}
        self.authorize()
        self.assertEqual({p.name: p.read_bytes() for p in history.glob('*.json') if p.name in first}, first)
        self.assertEqual(len(list(history.glob('*.json'))), len(first) + 1)
        self.assertEqual((self.root / '.sfc/config.json').read_bytes(), before)

    def test_stop_arriving_during_scope_validation_is_never_cleared(self):
        self.authorize()
        def stop_during_read():
            (common_dir(self.root) / 'sfc-stop').touch()
            return {'ready': True}
        self.gh.dashboard = stop_during_read
        self.assertEqual(self.launch()['state'], 'waiting')
        self.assertTrue((common_dir(self.root) / 'sfc-stop').exists())
        self.assertEqual(self.auth()['state'], 'stopped')
        self.assertFalse((common_dir(self.root) / 'sfc-watch.json').exists())
        self.assertEqual(self.gh.writes + self.proc.events, [])

    def test_uncertain_start_and_malformed_receipts_never_dispatch(self):
        self.authorize()
        auth = self.auth()
        auth['state'] = 'starting'  # Simulated abrupt host death after intent.
        write_json(common_dir(self.root) / 'sfc-autostart.json', auth)
        self.assertEqual(self.launch()['state'], 'waiting')
        self.assertEqual(self.auth(), auth)
        auth['id'] = '../escape'
        write_json(common_dir(self.root) / 'sfc-autostart.json', auth)
        self.assertEqual(self.launch()['state'], 'waiting')
        with self.assertRaisesRegex(Halt, 'Malformed'):
            self.authorize()
        self.assertEqual(self.gh.writes + self.proc.events, [])

    def test_failure_incomplete_configuration_and_board_drift_fail_before_workers(self):
        for drift in ('missing-config', 'config', 'board', 'dashboard', 'incomplete-auth'):
            with self.subTest(drift=drift):
                write_json(self.root / '.sfc/config.json', self.c)
                self.gh.dashboard = lambda: {'ready': True}
                original = self.gh.metadata
                self.authorize()
                if drift == 'missing-config':
                    (self.root / '.sfc/config.json').unlink()
                elif drift == 'config':
                    write_json(self.root / '.sfc/config.json', {**self.c, 'max_workers': 1})
                elif drift == 'board':
                    self.gh.metadata = lambda: {'id': 'different-board'}
                elif drift == 'dashboard':
                    self.gh.dashboard = lambda: {'ready': False}
                else:
                    auth = self.auth()
                    del auth['metadata']
                    write_json(common_dir(self.root) / 'sfc-autostart.json', auth)
                self.assertEqual(self.launch()['state'], 'waiting')
                self.assertEqual(self.gh.writes + self.proc.events, [])
                if drift in ('board', 'dashboard', 'incomplete-auth'):
                    self.assertEqual(self.auth()['state'], 'failed')
                    self.event['session_id'] += '-new'
                    self.assertEqual(self.launch()['state'], 'waiting')
                self.gh.metadata = original

    def test_worker_nonstartup_headless_and_plan_contexts_are_ignored_before_config_reads(self):
        self.authorize()
        before = self.auth()
        for key, value in [('source', 'compact'), ('source', 'clear'), ('source', 'fork'),
                           ('hook_event_name', 'SubagentStart'), ('agent_id', 'child'),
                           ('permission_mode', 'unknown'), ('permission_mode', 'plan'),
                           ('permission_mode', None), ('session_id', '')]:
            event = {**self.event, key: value}
            self.assertEqual(autostart.launch(event, environ={})['state'], 'ignored')
        for marker in ('SFC_WORKER', 'SFC_TEST_CONTEXT'):
            # Both packaged launch markers must suppress even legacy admission,
            # before touching configuration or considering stored authority.
            with patch.object(autostart, 'paths', side_effect=AssertionError('ineligible context read configuration')):
                self.assertEqual(autostart.launch(self.event, environ={marker: '1'})['state'], 'ignored')
        self.assertEqual(self.auth(), before)
        self.assertEqual(self.gh.writes + self.proc.events, [])

    def test_explicit_authorization_is_required_in_every_active_permission_mode(self):
        for mode in ('default', 'acceptEdits', 'dontAsk', 'bypassPermissions'):
            event = {**self.event, 'permission_mode': mode}
            self.assertTrue(autostart.eligible(event, {}))
            self.assertEqual(autostart.launch(event, environ={})['state'], 'disabled')
            self.assertEqual(autostart.launch(event, environ={'SFC_WORKER': '1'})['state'], 'ignored')
        self.assertEqual(self.gh.writes + self.proc.events, [])

    def test_manual_watcher_choice_and_existing_attempt_history_are_preserved(self):
        self.authorize()
        state = {'version': 1, 'cards': {'1': {'attempts': 2, 'history': [{'detail': 'prior failure'}],
                                             'status': 'Blocked', 'handoff': 'human'}}}
        write_json(self.root / '.sfc/state.json', state)
        self.gh.items = []
        self.launch()
        self.assertEqual(read_json(self.root / '.sfc/state.json')['cards'], state['cards'])
        first = self.watcher()
        manual_factory = self.make_factory(self.root)
        with patch.object(manual_factory, 'process', side_effect=self.finish):
            manual_factory.watch(start=True, clock=self.clock, wait=self.clock.wait, overrides=self.options)
        manual = self.watcher()
        self.assertNotEqual(first['id'], manual['id'])
        self.event['session_id'] = 'new-chat'
        self.assertIn('manual watcher', self.launch()['detail'])
        self.assertEqual(self.watcher(), manual)

    def test_merge_authority_is_explicit_and_cannot_change_between_read_and_lock(self):
        with self.assertRaises(Halt):
            self.authorize(allow_merge=True)
        self.c['merge_policy'] = 'auto'
        write_json(self.root / '.sfc/config.json', self.c)
        self.authorize(allow_merge=True)
        self.gh.items = []
        self.launch()
        self.assertTrue(self.watcher()['allow_merge'])
        self.assertEqual(self.c['auto_max_lines'], 400)
        self.assertEqual(self.c['max_attempts'], 2)
        self.authorize(allow_merge=True)
        original = Factory.watch
        def raced(factory, **kwargs):
            self.authorize(allow_merge=False)
            return original(factory, **kwargs)
        with patch.object(Factory, 'watch', raced):
            self.assertEqual(self.launch()['state'], 'waiting')
        self.assertEqual(self.auth()['state'], 'armed')


class HookContractTests(unittest.TestCase):
    def test_separate_git_directory_resolves_actual_checkout_without_authorization(self):
        import tempfile
        with tempfile.TemporaryDirectory(prefix='sfc-separate-git-') as tmp:
            base = Path(tmp)
            checkout = base / 'project'
            checkout.mkdir()
            git(checkout, 'init', '--separate-git-dir', base / 'metadata')
            subdirectory = checkout / 'nested'
            subdirectory.mkdir()
            self.assertEqual(autostart.configured_root(subdirectory), checkout.resolve())

    def test_guard_entrypoint_reports_disabled_and_suppresses_worker_events(self):
        import tempfile
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix='sfc-hook-entry-') as tmp:
            target = Path(tmp)
            git(target, 'init')
            event = {'hook_event_name': 'SessionStart', 'source': 'startup', 'session_id': 'fake-user-chat',
                     'cwd': str(target), 'permission_mode': 'default'}
            env = dict(os.environ)
            env.pop('SFC_WORKER', None)
            argv = [sys.executable, str(root / 'hooks/guard.py'), '--autostart']
            result = subprocess.run(argv, input=json.dumps(event), env=env, capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0)
            self.assertIn('disabled', json.loads(result.stdout)['hookSpecificOutput']['additionalContext'])
            before = read_json(common_dir(target) / 'sfc-autostart-status.json')
            result = subprocess.run(argv, input=json.dumps(event), env={**env, 'SFC_WORKER': '1'},
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout, '')
            self.assertEqual(read_json(common_dir(target) / 'sfc-autostart-status.json'), before)

    def test_package_version_is_consistent(self):
        from sfc_lib import __version__
        root = Path(__file__).resolve().parents[1]
        self.assertRegex(__version__, r'^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$')
        for path in ('plugin.json', '.codex-plugin/plugin.json'):
            self.assertEqual(read_json(root / path)['version'], __version__)

    def test_bundled_hook_uses_supported_async_lifecycle_without_detaching(self):
        root = Path(__file__).resolve().parents[1]
        hooks = read_json(root / 'hooks/hooks.json')['hooks']['SessionStart']
        entry = next(h for g in hooks for h in g['hooks'] if h.get('command', '').endswith('--autostart'))
        self.assertTrue(entry['async'])
        self.assertGreater(entry['timeout'], 86400 + 30)
        self.assertTrue(entry['command'].endswith('--autostart'))
        self.assertNotIn('bypass', entry['command'])

    def test_worker_marker_reaches_actual_headless_child_and_owned_cancellation(self):
        import tempfile
        with tempfile.TemporaryDirectory(prefix='sfc-hook-child-') as tmp:
            root = Path(tmp)
            process = Processes(root / 'stop')
            log = root / 'worker.log'
            process.execute([sys.executable, '-c', "import os; print(os.environ['SFC_WORKER'])"], root, log, 5)
            self.assertEqual(log.read_text().strip(), '1')
            self.assertEqual(process.children, set())


if __name__ == '__main__':
    unittest.main()
