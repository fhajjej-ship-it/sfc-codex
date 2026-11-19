"""Fake-host/window tests plus harmless owned POSIX process fixtures."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import session_mcp
from sfc_lib import autostart, common
from sfc_lib.common import Busy, Halt, Processes, Stopped, TimedOut, common_dir, exclusive, git, read_json, write_json
from sfc_lib.factory import Factory
from sfc_lib.lifeline import supported
from sfc_lib.session_runtime import SessionRuntime
import test_autostart as autostart_fixtures
from test_pickup import ticket


@unittest.skipUnless(supported(), 'Lifetime candidate is macOS-only')
class SessionTests(unittest.TestCase):
    setUp = autostart_fixtures.AutoStartTests.setUp
    tearDown = autostart_fixtures.AutoStartTests.tearDown
    make_factory = autostart_fixtures.AutoStartTests.make_factory
    auth = autostart_fixtures.AutoStartTests.auth
    watcher = autostart_fixtures.AutoStartTests.watcher
    finish = autostart_fixtures.AutoStartTests.finish

    def authorize(self, **kwargs):
        with exclusive(self.root):
            return autostart.configure(self.make_factory(self.root), enabled=True, lifetime=True, **kwargs)

    def runtime(self, **kwargs):
        return SessionRuntime(self.root, environ={}, factory_type=self.make_factory,
                              clock=self.clock, wait=self.clock.wait, **kwargs)

    def attach(self, runtime):
        from sfc_lib.session_runtime import request_attachment
        request_attachment(self.event, environ={})
        return runtime.attach(self.event, background=False, native_thread=self.event['session_id'])

    def hook_arguments(self, name, event):
        # Match the native expander: exact placeholders retain their JSON type;
        # missing fields fail instead of becoming empty strings or null.
        hooks = read_json(Path(__file__).resolve().parents[1] / 'hooks/hooks.json')['hooks']
        hook, = [hook for group in hooks[name] for hook in group['hooks']
                 if hook.get('type') == 'mcp_tool' and hook.get('tool') ==
                 ('session_attach' if name == 'PreModelRequest' else 'session_attach_hook')]
        return {key: event[value[2:-1]] for key, value in hook['input'].items()}

    def test_packaged_child_hook_projection_cannot_reuse_parent_authority(self):
        from sfc_lib.session_runtime import request_attachment
        self.authorize()
        parent = {**self.event, 'hook_event_name': 'UserPromptSubmit', 'turn_id': 'parent-turn'}
        request_attachment(parent, environ={})
        before = self.auth()
        requests = common_dir(self.root) / 'sfc-session-requests'
        original = {p.name: p.read_bytes() for p in requests.iterdir()}
        for name in ('UserPromptSubmit', 'PreModelRequest'):
            with self.subTest(event=name):
                child = {**parent, 'hook_event_name': name, 'turn_id': 'child-turn', 'agent_id': 'child-agent'}
                self.assertEqual(request_attachment(child, environ={})['state'], 'ignored')
                projected = self.hook_arguments(name, child)
                runtime = self.runtime()
                result = runtime.attach(projected, background=False, native_thread=self.event['session_id'])
                self.assertEqual(result['state'], 'ignored')
                self.assertIsNone(runtime.host)
                self.assertIsNone(runtime.thread)
                self.assertEqual(self.auth(), before)
                self.assertEqual({p.name: p.read_bytes() for p in requests.iterdir()}, original)
                self.assertEqual(self.gh.writes + self.proc.events, [])

    def test_packaged_root_hook_projection_preserves_readiness_without_optional_fields(self):
        from sfc_lib.session_runtime import request_attachment
        self.authorize()
        parent = {**self.event, 'hook_event_name': 'UserPromptSubmit', 'turn_id': 'parent-turn'}
        self.assertNotIn('agent_id', parent)
        request_attachment(parent, environ={})
        for name in ('UserPromptSubmit', 'PreModelRequest'):
            with self.subTest(event=name):
                event = {**parent, 'hook_event_name': name}
                if name == 'PreModelRequest':
                    event.pop('permission_mode')
                runtime = self.runtime()
                self.assertEqual(runtime.attach(self.hook_arguments(name, event), background=False, native_thread=self.event['session_id'])['state'], 'pending')
                self.assertIsNotNone(runtime.host)
                self.assertEqual(self.gh.writes + self.proc.events, [])

    def test_fallback_requires_current_root_turn_proof(self):
        from sfc_lib.session_runtime import request_attachment
        self.authorize()
        request_attachment(self.event, environ={})
        for turn in (None, '', 5, 'unproved-turn'):
            with self.subTest(turn=turn):
                runtime = self.runtime()
                event = {**self.event, 'hook_event_name': 'PreModelRequest', 'turn_id': turn}
                self.assertEqual(runtime.attach(event, background=False, native_thread=self.event['session_id'])['state'], 'ignored')
                self.assertIsNone(runtime.host)
        self.assertEqual(self.gh.writes + self.proc.events, [])

    def test_direct_rpc_replay_requires_actual_native_thread_metadata(self):
        from sfc_lib.session_runtime import request_attachment
        self.authorize()
        request_attachment(self.event, environ={})
        # Codex supplies threadId in params._meta, outside tool arguments. A
        # child shares its parent's hook session_id, but has its own threadId.
        for meta, expected in ((None, 'ignored'), ({}, 'ignored'),
                               ({'threadId': 'child-thread'}, 'ignored'),
                               ({'threadId': 7}, 'ignored'), ([], 'ignored'),
                               ({'threadId': self.event['session_id']}, 'pending')):
            with self.subTest(meta=meta):
                runtime = self.runtime()
                params = {'name': 'session_attach', 'arguments': self.event}
                if meta is not None:
                    params['_meta'] = meta
                messages = [dict(jsonrpc='2.0', id=1, method='initialize', params={'protocolVersion': '2025-03-26'}),
                            dict(jsonrpc='2.0', method='notifications/initialized'),
                            dict(jsonrpc='2.0', id=2, method='tools/call', params=params)]
                sink = io.StringIO()
                with patch('sfc_lib.session_runtime.threading.Thread') as thread:
                    session_mcp.serve(runtime, io.StringIO(''.join(json.dumps(m) + '\n' for m in messages)), sink)
                    value = json.loads(json.loads(sink.getvalue().splitlines()[-1])['result']['content'][0]['text'])
                    self.assertEqual(value['state'], expected)
                    self.assertEqual(thread.called, expected == 'pending')
                    self.assertEqual(runtime.host is not None, expected == 'pending')
                self.assertEqual(self.gh.writes + self.proc.events, [])

    def test_initialize_and_pending_readiness_do_not_read_board_or_create_authority(self):
        runtime = self.runtime()
        messages = [dict(jsonrpc='2.0', id=1, method='tools/call', params={'name': 'session_attach', 'arguments': self.event}),
                    dict(jsonrpc='2.0', id=2, method='initialize', params={'protocolVersion': '2025-03-26'}),
                    dict(jsonrpc='2.0', method='notifications/initialized'),
                    dict(jsonrpc='2.0', id=3, method='tools/list'),
                    dict(jsonrpc='2.0', id=4, method='tools/call', params={'name': 'session_status'})]
        sink = io.StringIO()
        session_mcp.serve(runtime, io.StringIO(''.join(json.dumps(m) + '\n' for m in messages)), sink)
        replies = [json.loads(line) for line in sink.getvalue().splitlines()]
        self.assertIn('Pending', replies[0]['error']['message'])
        self.assertEqual({tool['name'] for tool in replies[2]['result']['tools']},
                         {'session_attach', 'session_attach_hook', 'session_status'})
        self.assertEqual(runtime.status()['state'], 'pending')
        self.assertFalse((common_dir(self.root) / 'sfc-autostart.json').exists())

    def test_native_attachment_hooks_return_event_json_while_status_keeps_its_contract(self):
        from sfc_lib.session_runtime import request_attachment
        self.authorize()
        hooks = read_json(Path(__file__).resolve().parents[1] / 'hooks/hooks.json')['hooks']
        for name in ('SessionStart', 'UserPromptSubmit'):
            with self.subTest(event=name):
                event = {**self.event, 'hook_event_name': name, 'turn_id': 'native-root-turn'}
                request_attachment(event, environ={})
                hook, = [h for group in hooks[name] for h in group['hooks'] if h.get('type') == 'mcp_tool']
                arguments = {k: event[v[2:-1]] for k, v in hook['input'].items()}
                messages = [dict(jsonrpc='2.0', id=1, method='initialize', params={'protocolVersion': '2025-03-26'}),
                            dict(jsonrpc='2.0', method='notifications/initialized'),
                            dict(jsonrpc='2.0', id=2, method='tools/call', params={
                                'name': hook['tool'], 'arguments': arguments,
                                '_meta': {'threadId': event['session_id']}}),
                            dict(jsonrpc='2.0', id=3, method='tools/call', params={'name': 'session_status'})]
                runtime, sink = self.runtime(), io.StringIO()
                with patch('sfc_lib.session_runtime.threading.Thread'):
                    session_mcp.serve(runtime, io.StringIO(''.join(json.dumps(m) + '\n' for m in messages)), sink)
                replies = [json.loads(line)['result'] for line in sink.getvalue().splitlines()]
                payload = json.loads(replies[1]['content'][0]['text'])
                self.assertEqual(payload['hookSpecificOutput']['hookEventName'], name)
                self.assertIn('pending', payload['hookSpecificOutput']['additionalContext'])
                self.assertNotIn('state', payload)
                status = json.loads(replies[2]['content'][0]['text'])
                self.assertEqual(status['state'], 'pending')
                self.assertEqual(self.gh.writes + self.proc.events, [])

    def test_hook_rpc_keeps_native_child_identity_guard(self):
        from sfc_lib.session_runtime import request_attachment
        self.authorize()
        event = {**self.event, 'hook_event_name': 'UserPromptSubmit', 'turn_id': 'root-turn'}
        request_attachment(event, environ={})
        runtime, sink = self.runtime(), io.StringIO()
        messages = [dict(jsonrpc='2.0', id=1, method='initialize', params={'protocolVersion': '2025-03-26'}),
                    dict(jsonrpc='2.0', method='notifications/initialized'),
                    dict(jsonrpc='2.0', id=2, method='tools/call', params={
                        'name': 'session_attach_hook', 'arguments': event, '_meta': {'threadId': 'child-native-thread'}})]
        with patch('sfc_lib.session_runtime.threading.Thread') as thread:
            session_mcp.serve(runtime, io.StringIO(''.join(json.dumps(m) + '\n' for m in messages)), sink)
            self.assertFalse(thread.called)
        payload = json.loads(json.loads(sink.getvalue().splitlines()[-1])['result']['content'][0]['text'])
        self.assertIn('ignored', payload['hookSpecificOutput']['additionalContext'])
        self.assertIsNone(runtime.host)
        self.assertEqual(self.gh.writes + self.proc.events, [])
        self.assertEqual(self.gh.writes + self.proc.events, [])

    def test_startup_race_then_documented_prompt_and_model_request_fallback(self):
        from sfc_lib.session_runtime import request_attachment
        self.authorize()
        runtime = self.runtime()
        self.assertEqual(runtime.attach(self.event, background=False, native_thread=self.event['session_id'])['state'], 'pending')
        self.assertIsNone(runtime.host)
        self.assertEqual(self.gh.writes + self.proc.events, [])
        request_attachment({**self.event, 'hook_event_name': 'UserPromptSubmit', 'turn_id': 'root-turn'}, environ={})
        fallback = {key: self.event[key] for key in ('cwd', 'session_id')}
        fallback.update(hook_event_name='PreModelRequest', turn_id='root-turn')
        self.assertEqual(runtime.attach(fallback, background=False, native_thread=self.event['session_id'])['state'], 'pending')
        self.assertIsNotNone(runtime.host)
        connection = runtime.connection
        self.assertEqual(runtime.attach(fallback, background=False, native_thread=self.event['session_id'])['connection'], connection)
        self.assertEqual(self.gh.writes + self.proc.events, [])

    def test_attachment_returns_while_observer_remains_alive_between_tool_responses(self):
        from sfc_lib.session_runtime import request_attachment
        self.authorize()
        self.gh.items = []
        runtime = self.runtime()
        request_attachment(self.event, environ={})
        entered, release = threading.Event(), threading.Event()
        def idle():
            entered.set()
            if not release.wait(5):
                raise RuntimeError('fixture wait expired')
        self.clock.on_wait = idle
        try:
            self.assertEqual(runtime.attach(self.event, native_thread=self.event['session_id'])['state'], 'pending')
            self.assertTrue(entered.wait(5))
            for _ in range(3):
                self.assertEqual(runtime.status()['state'], 'observing')
                self.assertTrue(runtime.thread.is_alive())
                self.assertEqual(runtime.attach(self.event, native_thread=self.event['session_id'])['work'], 'active')
            self.assertEqual(len(self.auth()['windows']), 0)
            self.assertEqual(self.gh.writes + self.proc.events, [])
        finally:
            runtime.closed.set()
            release.set()
            runtime.close()
        self.assertFalse(runtime.thread.is_alive())
        self.assertEqual(runtime.status()['state'], 'ended')

    def test_same_connection_observes_and_admits_work_after_original_hour(self):
        self.gh.items = []
        self.c['watch'].update(max_seconds=3600, max_polls=100, poll_interval=3600)
        write_json(self.root / '.sfc/config.json', self.c)
        self.authorize()
        runtime = self.runtime()
        self.assertEqual(self.attach(runtime)['state'], 'pending')
        connection = runtime.connection
        self.clock.on_wait = lambda: setattr(self.clock, 'value', self.clock.value + 3601)
        runtime.window()
        first = self.watcher()
        self.assertEqual(first['reason'], 'watch-time-limit')
        self.assertGreater(self.clock(), first['deadline'])
        self.assertEqual(self.gh.writes, [])
        self.gh.items = [ticket(status='Backlog'), ticket(15, priority='P0'), ticket(3, labels=['custom-hold'])]
        self.assertEqual(self.attach(runtime)['connection'], connection)
        with patch.object(Factory, 'process', side_effect=self.finish) as worker:
            runtime.window()
        second = self.watcher()
        self.assertEqual(worker.call_count, 1)
        self.assertEqual(second['claims'], 1)
        self.assertNotEqual(first['id'], second['id'])
        self.assertEqual(self.auth()['windows'], [first['id'], second['id']])
        self.assertEqual(read_json(self.root / '.sfc/watch-sessions' / (first['id'] + '.json')), first)
        self.assertFalse(second['allow_merge'])
        self.assertEqual({n for n, _ in self.gh.writes}, {1})

    def test_successive_windows_preserve_attempts_output_budget_and_uncertain_work(self):
        self.authorize()
        self.gh.items = []
        runtime = self.runtime()
        self.attach(runtime)
        f = self.make_factory(self.root)
        record = f.record(ticket())
        record.update(attempts=1, worker_output_budget={'build': {'limit': 2, 'dispatches': [
            {'id': 'prior-dispatch', 'status': 'unaccepted', 'result': 'retained-output.json'}]}},
                      history=[{'detail': 'prior failure'}])
        f.save()
        runtime.window()
        runtime.window()
        self.assertEqual(self.make_factory(self.root).record(ticket()), record)
        f = self.make_factory(self.root)
        f.update(ticket(), claim={'state': 'intent'})
        with self.assertRaisesRegex(Halt, 'Unresolved priority claim'):
            runtime.window()
        failed = self.auth()
        runtime.observe()
        self.assertEqual(runtime.status()['state'], 'paused')
        self.assertEqual(self.auth(), failed)
        self.assertEqual(self.gh.writes + self.proc.events, [])

    def test_observer_rolls_windows_and_admits_late_arrival_without_another_control_call(self):
        self.gh.items = []
        self.c['watch'].update(max_seconds=3600, max_polls=100, poll_interval=3600)
        write_json(self.root / '.sfc/config.json', self.c)
        self.authorize()
        runtime = self.runtime()
        self.attach(runtime)
        original_connection = runtime.connection

        def arrival_after_hour():
            self.clock.on_wait = None
            self.clock.value += 3601
            self.gh.items = [ticket(status='Backlog')]

        def complete_and_end(card):
            self.finish(card)
            runtime.closed.set()

        self.clock.on_wait = arrival_after_hour
        # Invoke the actual observer loop once. No tool call, prompt, or manual
        # window start intervenes between expiration and the later claim.
        with patch.object(Factory, 'process', side_effect=complete_and_end) as worker:
            runtime.observe()
        windows = self.auth()['windows']
        self.assertEqual(worker.call_count, 1)
        self.assertEqual(len(windows), 2)
        self.assertNotEqual(*windows)
        first = read_json(self.root / '.sfc/watch-sessions' / (windows[0] + '.json'))
        second = self.watcher()
        self.assertGreater(self.clock(), first['deadline'])
        self.assertEqual(first['reason'], 'watch-time-limit')
        self.assertEqual(first['claims'], 0)
        self.assertEqual(second['claims'], 1)
        self.assertEqual(second['lifetime']['connections'], [original_connection])
        self.assertEqual(runtime.connection, original_connection)
        self.assertEqual(runtime.close()['state'], 'ended')

    def test_duplicate_and_other_chat_common_lock_admission(self):
        self.authorize()
        runtime = self.runtime()
        self.attach(runtime)
        other = self.runtime()
        self.event['session_id'] = 'other-chat'
        self.attach(other)
        before = self.auth()
        with exclusive(self.root):
            with self.assertRaises(Busy):
                runtime.window()
            with self.assertRaises(Busy):
                other.window()
        self.assertEqual(self.auth(), before)
        with patch.object(Factory, 'process', side_effect=self.finish) as process:
            runtime.window()
            other.window()
        self.assertEqual(process.call_count, 1)
        self.assertEqual(len(self.auth()['windows']), 2)

    def test_explicit_reauthorization_can_reattach_same_connection_but_prompts_cannot_retry(self):
        from sfc_lib.session_runtime import request_attachment
        self.authorize()
        runtime = self.runtime()
        self.attach(runtime)
        auth = self.auth()
        auth['state'] = 'failed'
        write_json(common_dir(self.root) / 'sfc-autostart.json', auth)
        runtime.observe()
        self.assertEqual(runtime.status()['state'], 'paused')
        old = runtime.authorization
        self.assertEqual(self.attach(runtime)['state'], 'paused')
        self.assertEqual(runtime.authorization, old)
        self.authorize()
        request_attachment(self.event, environ={})
        self.assertEqual(runtime.attach(self.event, background=False, native_thread=self.event['session_id'])['state'], 'pending')
        self.assertNotEqual(runtime.authorization, old)
        self.gh.items = []
        runtime.window()
        self.assertEqual(self.auth()['state'], 'window-finished')

    def test_linked_checkout_uses_authorized_root_and_concurrent_owner_waits(self):
        from sfc_lib.session_runtime import request_attachment
        self.authorize()
        linked = self.root / '.sfc/worktrees/lifetime-linked'
        git(self.root, 'worktree', 'add', '-b', 'lifetime-peer', linked)
        peer = SessionRuntime(linked, environ={}, factory_type=self.make_factory,
                              clock=self.clock, wait=self.clock.wait)
        peer_event = {**self.event, 'cwd': str(linked), 'session_id': 'peer'}
        request_attachment(peer_event, environ={})
        self.assertEqual(peer.attach(peer_event, background=False, native_thread=peer_event['session_id'])['state'], 'pending')
        self.assertEqual(peer.root, self.root)
        runtime = self.runtime()
        self.attach(runtime)
        entered, release = threading.Event(), threading.Event()
        errors = []
        def process(card):
            entered.set()
            if not release.wait(5):
                raise RuntimeError('fixture wait expired')
            self.finish(card)
        def owner():
            try:
                runtime.window()
            except BaseException as exc:
                errors.append(exc)
        with patch.object(Factory, 'process', side_effect=process) as worker:
            thread = threading.Thread(target=owner)
            thread.start()
            try:
                self.assertTrue(entered.wait(5))
                with self.assertRaises(Busy):
                    peer.window()
            finally:
                release.set()
                thread.join(5)
        self.assertEqual(errors, [])
        self.assertFalse(thread.is_alive())
        self.assertEqual(worker.call_count, 1)
        self.assertEqual(self.watcher()['claims'], 1)

    def test_idle_connection_handoff_resumes_exact_deadline_and_spending(self):
        self.authorize()
        self.gh.items = []
        a = self.runtime()
        self.attach(a)
        self.clock.on_wait = a.closed.set
        with self.assertRaises(Stopped):
            a.window()
        before = self.watcher()
        self.assertEqual(self.auth()['state'], 'suspended')
        self.clock.on_wait = None
        b = self.runtime()
        self.event['session_id'] = 'other-native-chat'
        self.attach(b)
        b.window()
        after = self.watcher()
        self.assertEqual(before['id'], after['id'])
        self.assertEqual(before['deadline'], after['deadline'])
        self.assertGreater(after['polls'], before['polls'])
        self.assertEqual(self.auth()['windows'], [before['id']])

    def test_operator_stop_precedes_handoff_and_never_rearms(self):
        self.authorize()
        self.gh.items = []
        runtime = self.runtime()
        self.attach(runtime)
        def interrupt():
            (common_dir(self.root) / 'sfc-stop').touch()
            runtime.closed.set()
        self.clock.on_wait = interrupt
        with self.assertRaises(Stopped):
            runtime.window()
        first = self.watcher()
        self.assertEqual(self.auth()['state'], 'stopped')
        b = self.runtime()
        self.attach(b)
        with self.assertRaises(Stopped):
            b.window()
        self.assertEqual(self.watcher(), first)
        self.assertEqual(self.gh.writes + self.proc.events, [])

    def test_disabled_or_config_drift_during_idle_pauses_without_overwriting_authority(self):
        for drift in ('disable', 'config'):
            with self.subTest(drift=drift):
                self.authorize()
                self.gh.items = []
                runtime = self.runtime()
                self.attach(runtime)
                def change():
                    self.clock.on_wait = None
                    if drift == 'disable':
                        auth = self.auth()
                        auth['enabled'] = False
                        write_json(common_dir(self.root) / 'sfc-autostart.json', auth)
                    else:
                        c = read_json(self.root / '.sfc/config.json')
                        c['worker_timeout'] += 1
                        write_json(self.root / '.sfc/config.json', c)
                self.clock.on_wait = change
                runtime.observe()
                self.assertEqual(runtime.status()['state'], 'paused')
                if drift == 'disable':
                    self.assertFalse(self.auth()['enabled'])
                before = self.watcher()
                runtime.observe()
                self.assertEqual(self.watcher(), before)
                self.assertEqual(self.gh.writes + self.proc.events, [])

    def test_old_lifetime_authorization_observes_without_new_recovery_permission(self):
        auth = self.authorize()
        auth['scope']['watch'].pop('max_recoveries_per_ticket')
        write_json(common_dir(self.root) / 'sfc-autostart.json', auth)
        self.gh.items = []
        runtime = self.runtime()
        self.assertEqual(self.attach(runtime)['state'], 'pending')
        try:
            runtime.window()
            self.assertEqual(self.watcher()['settings']['max_recoveries_per_ticket'], 0)
            self.assertNotIn('blocked_recovery_kinds', self.watcher()['policy'])
            runtime.window()
            self.assertEqual(self.auth()['scope'], auth['scope'])
            self.assertNotIn('blocked_recovery_kinds', self.watcher()['policy'])
        finally:
            runtime.close()

    def test_legacy_authorization_is_never_migrated_and_hook_is_short_for_lifetime(self):
        with exclusive(self.root):
            old = autostart.configure(self.make_factory(self.root), enabled=True)
        runtime = self.runtime()
        self.assertEqual(self.attach(runtime)['state'], 'disabled')
        self.assertEqual(self.auth(), old)
        self.authorize()
        result = autostart.launch(self.event, environ={}, factory_type=self.make_factory)
        self.assertEqual(result['state'], 'pending')
        self.assertFalse((common_dir(self.root) / 'sfc-watch.json').exists())
        self.assertEqual(self.gh.writes + self.proc.events, [])

    def test_root_connection_binding_child_markers_and_unsupported_platform(self):
        self.authorize()
        runtime = self.runtime()
        for field, value in (('agent_id', 'child'), ('permission_mode', 'plan'), ('source', 'fork')):
            event = {**self.event, field: value}
            self.assertEqual(runtime.attach(event, background=False, native_thread=self.event['session_id'])['state'], 'ignored')
        with patch('sfc_lib.session_runtime.supported', return_value=False):
            self.assertEqual(self.attach(runtime)['state'], 'unavailable')
        linked = self.root / '.sfc/worktrees/linked'
        git(self.root, 'worktree', 'add', '-b', 'lifetime-linked', linked)
        self.assertEqual(runtime.attach({**self.event, 'cwd': str(linked)}, background=False, native_thread=self.event['session_id'])['state'], 'paused')
        self.assertEqual(self.attach(runtime)['state'], 'pending')
        self.assertEqual(runtime.attach({**self.event, 'session_id': 'spoofed'}, background=False, native_thread=self.event['session_id'])['state'], 'ignored')
        for marker in ('SFC_WORKER', 'SFC_TEST_CONTEXT'):
            child = SessionRuntime(self.root, environ={marker: '1'})
            self.assertEqual(child.attach(self.event, native_thread=self.event['session_id'])['state'], 'ignored')
        self.assertEqual(self.gh.writes + self.proc.events, [])

    def test_cooperative_window_cutoff_continues_without_laundering_lane_budget(self):
        self.c['watch']['max_seconds'] = 1
        write_json(self.root / '.sfc/config.json', self.c)
        self.authorize()
        runtime = self.runtime()
        self.attach(runtime)
        prior = self.make_factory(self.root)
        prior.record(ticket())['attempts'] = 1
        prior.save()
        def cutoff_execute(*args, **kwargs):
            self.clock.value += 2
            raise Stopped('finite work cutoff')
        # Use the real build/lane dispatch and its durable output reservation.
        with patch.object(self.proc, 'execute', side_effect=cutoff_execute) as worker:
            runtime.window()
            before = self.watcher()
            first_record = self.make_factory(self.root).state['cards']['1']
            self.assertTrue(before['cooperative_cutoff'])
            self.assertEqual(before['no_progress'], 1)
            dispatch = first_record['worker_output_budget']['build']['dispatches'][0]
            self.assertEqual(dispatch['status'], 'window-interrupted')
            self.assertEqual(dispatch['window'], before['id'])
            self.assertTrue(Path(dispatch['log']).with_name('interruption.json').is_file())
            runtime.window()
            second = self.watcher()
            self.assertNotEqual(second['id'], before['id'])
            self.assertEqual(second['no_progress'], 2)
            record = self.make_factory(self.root).state['cards']['1']
            self.assertEqual(record['worker_output_budget']['build']['dispatches'][0], dispatch)
            self.assertEqual(len(record['worker_output_budget']['build']['dispatches']), 2)
            runtime.window()  # Existing 2/2 output cap blocks this ticket, not observation.
            self.assertEqual(worker.call_count, 2)
            self.assertEqual(runtime.status()['state'], 'observing')
            parked = self.make_factory(self.root).state['cards']['1']
            self.assertEqual(parked['status'], 'Blocked')
            self.assertEqual(parked['ticket_failure']['action'], 'output_budget')
            self.assertEqual(parked['worker_output_budget'], record['worker_output_budget'])
            self.assertEqual(self.watcher()['no_progress'], second['no_progress'])
            runtime.window()
            self.assertEqual(worker.call_count, 2)
            self.assertEqual(self.watcher()['no_progress'], second['no_progress'])
        self.assertEqual(record['attempts'], 1)  # Prior findings attempt 1/2 is retained.
        self.assertEqual(read_json(self.root / '.sfc/watch-sessions' / (before['id'] + '.json')), before)

    def test_check_cutoff_has_pinned_receipt_and_carries_no_progress_spending(self):
        self.c['watch']['max_seconds'] = 1
        write_json(self.root / '.sfc/config.json', self.c)
        self.authorize()
        runtime = self.runtime()
        self.attach(runtime)
        def checks(factory, card):
            worktree = factory.worktree(card)
            factory.verify(card, worktree, factory.record(card)['base'])
        def cutoff(*args, **kwargs):
            self.clock.value += 2
            raise Stopped('check window cutoff')
        with patch.object(Factory, 'process', autospec=True, side_effect=checks), \
                patch.object(self.proc, 'execute', side_effect=cutoff) as check:
            runtime.window()
            first = self.watcher()
            receipts = list((self.root / '.sfc/runs').glob('*/issue-1/verify-*/interruption.json'))
            self.assertEqual(len(receipts), 1)
            receipt = read_json(receipts[0])
            self.assertEqual(receipt['window'], first['id'])
            self.assertEqual(receipt['config_digest'], self.auth()['config_digest'])
            self.assertEqual(receipt['interrupted_check'], 1)
            runtime.window()
            self.assertEqual(self.watcher()['no_progress'], 2)
            self.assertEqual(check.call_count, 2)
            self.assertEqual(read_json(receipts[0]), receipt)

    def test_no_progress_and_incomplete_admission_never_create_another_budget(self):
        self.c['watch']['max_polls'] = 1
        write_json(self.root / '.sfc/config.json', self.c)
        self.authorize()
        runtime = self.runtime()
        self.attach(runtime)
        with patch.object(Factory, 'process', return_value=None) as worker:
            runtime.observe()
        self.assertEqual(self.auth()['state'], 'failed')
        self.assertEqual(self.watcher()['no_progress'], self.c['max_no_progress'])
        self.assertEqual(len(self.auth()['windows']), 3)
        count = worker.call_count
        before = self.watcher()
        runtime.observe()
        self.assertEqual(self.watcher(), before)
        self.assertEqual(worker.call_count, count)
        auth = self.auth()
        auth['state'] = 'starting'
        write_json(common_dir(self.root) / 'sfc-autostart.json', auth)
        with self.assertRaisesRegex(Halt, 'incomplete intent'):
            runtime.window()
        self.assertEqual(self.watcher(), before)

    def test_actual_plan_permission_request_cancels_active_work_authority(self):
        from sfc_lib.session_runtime import request_attachment
        self.authorize()
        self.gh.items = []
        runtime = self.runtime()
        self.attach(runtime)
        self.clock.on_wait = lambda: request_attachment({**self.event, 'hook_event_name': 'UserPromptSubmit', 'turn_id': 'root-turn',
                                                        'permission_mode': 'plan'}, environ={})
        runtime.observe()
        self.assertEqual(runtime.status()['state'], 'paused')
        self.assertEqual(self.auth()['state'], 'failed')
        self.assertEqual(self.gh.writes + self.proc.events, [])

    def test_board_identity_and_worker_failure_latch_without_window_retry(self):
        self.authorize()
        runtime = self.runtime()
        self.attach(runtime)
        with patch.object(self.gh, 'metadata', return_value={'id': 'changed'}):
            runtime.observe()
        self.assertEqual(self.auth()['state'], 'failed')
        self.assertEqual(self.gh.writes + self.proc.events, [])
        self.authorize()
        runtime = self.runtime()
        self.attach(runtime)
        with patch.object(Factory, 'process', side_effect=Halt('rate limit / malformed result')) as worker:
            runtime.observe()
            first = self.watcher()
            runtime.observe()
            self.assertEqual(worker.call_count, 1)
        self.assertEqual(self.watcher(), first)

        self.assertEqual(self.auth()['state'], 'stopped')
        value = runtime.status()
        self.assertNotIn('rate limit / malformed result', json.dumps(value))
        errors = list((common_dir(self.root) / 'sfc-session-errors' / runtime.connection).glob('*.json'))
        self.assertTrue(any('rate limit / malformed result' in read_json(p)['detail'] for p in errors))

    def test_ticket_timeout_keeps_same_native_connection_observing_later_work(self):
        self.c.update(max_no_progress=1)
        write_json(self.root / '.sfc/config.json', self.c)
        self.authorize()
        runtime = self.runtime()
        self.attach(runtime)
        original_connection = runtime.connection
        calls = []
        def work(factory, card):
            calls.append(card['number'])
            if card['number'] == 1:
                raise TimedOut('Owned fixture process timeout')
            factory.move(card, 'Done', 'Independent later work verified')
        first_window = None
        def arrival():
            nonlocal first_window
            if first_window is None and self.gh.items[0]['status'] == 'Blocked':
                first_window = self.watcher()['id']
                self.gh.items.append(ticket(2))
            elif (len(self.gh.items) == 2 and self.gh.items[1]['status'] == 'Done' and
                  self.watcher()['no_progress'] == 0):
                runtime.closed.set()
        self.clock.on_wait = arrival
        with patch.object(Factory, '_process', new=work):
            runtime.observe()
        self.assertEqual(calls, [1, 2])
        self.assertEqual(runtime.connection, original_connection)
        self.assertIsNotNone(first_window)
        self.assertEqual([c['status'] for c in self.gh.items], ['Blocked', 'Done'])
        state = read_json(self.root / '.sfc/state.json')
        self.assertEqual(state['cards']['1']['ticket_failure']['phase'], 'blocked')
        self.assertEqual(state['cards']['1']['attempts'], 0)
        self.assertEqual(self.watcher()['no_progress'], 0)

    def test_malformed_root_request_and_authority_pause_without_retry(self):
        for malformed in ('request', 'authorization', 'scope'):
            with self.subTest(malformed=malformed):
                self.authorize()
                runtime = self.runtime()
                self.attach(runtime)
                path = common_dir(self.root) / ('sfc-session-requests/' + runtime.host + '.json'
                                               if malformed == 'request' else 'sfc-autostart.json')
                value = [] if malformed != 'scope' else {**self.auth(), 'scope': []}
                write_json(path, value)
                runtime.observe()
                self.assertEqual(runtime.status()['state'], 'paused')
                self.assertEqual(read_json(path), value)
                self.assertEqual(self.gh.writes + self.proc.events, [])
                self.assertFalse((common_dir(self.root) / 'sfc-watch.json').exists())
                # Restore only this temporary fixture's intentionally malformed
                # authorization so the next case can create an independent grant.
                if malformed == 'authorization':
                    path.unlink()

    def test_malformed_root_request_at_attach_is_reported_and_drift_cancels(self):
        self.authorize()
        runtime = self.runtime()
        self.attach(runtime)
        path = common_dir(self.root) / 'sfc-session-requests' / (runtime.host + '.json')
        write_json(path, [])
        self.assertTrue(runtime.authority_changed(self.auth()))
        self.assertEqual(runtime.attach(self.event, background=False, native_thread=self.event['session_id'])['state'], 'paused')
        self.assertEqual(self.gh.writes + self.proc.events, [])

    def test_background_fault_reports_paused_without_automatic_retry(self):
        self.authorize()
        runtime = self.runtime()
        self.attach(runtime)
        with patch.object(runtime, 'window', side_effect=RuntimeError('fixture background fault')) as window:
            runtime.observe()
        self.assertEqual(window.call_count, 1)
        self.assertEqual(runtime.status()['state'], 'paused')
        self.assertEqual(self.attach(runtime)['state'], 'paused')
        self.assertEqual(self.gh.writes + self.proc.events, [])
        errors = list((common_dir(self.root) / 'sfc-session-errors' / runtime.connection).glob('*.json'))
        self.assertEqual(len(errors), 1)
        self.assertEqual(read_json(errors[0])['exception'], 'RuntimeError')


@unittest.skipUnless(supported(), 'macOS cleanup proof')
class LifelineTests(unittest.TestCase):
    def test_normal_command_exit_also_cleans_up_remaining_descendants(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fifo, ready = root / 'lifetime.fifo', root / 'ready'
            os.mkfifo(fifo)
            reader = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
            child = (f"import os,time; from pathlib import Path; fd=os.open({str(fifo)!r},os.O_WRONLY); "
                     f"os.write(fd,b'ready'); Path({str(ready)!r}).touch(); time.sleep(30)")
            code = (f"import subprocess,sys,time; from pathlib import Path; subprocess.Popen([sys.executable,'-c',{child!r}]); "
                    f"end=time.monotonic()+5\nwhile not Path({str(ready)!r}).exists() and time.monotonic()<end: time.sleep(.01)")
            process = Processes(root / 'stop')
            process.lifetime = True
            try:
                self.assertEqual(process.execute([sys.executable, '-c', code], root, root / 'log', 8), 0)
                self.assertEqual(os.read(reader, 5), b'ready')
                self.assertEqual(os.read(reader, 1), b'')
                self.assertIn('child-reaped', (root / 'log.lifeline.jsonl').read_text())
            finally:
                os.close(reader)

    def test_owned_worker_and_check_prompt_output_observation_timeout_and_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            process = Processes(root / 'stop')
            process.lifetime = True
            seen = []
            script = "import os,sys; print(os.environ['SFC_WORKER']); print(len(sys.stdin.read()))"
            self.assertEqual(process.execute([sys.executable, '-c', script], root, root / 'worker.log', 5,
                                             prompt='x' * 100000, observer=seen.append), 0)
            self.assertEqual((root / 'worker.log').read_text().splitlines(), ['1', '100000'])
            self.assertEqual(seen, ['started', 'inactive'])
            with self.assertRaisesRegex(Halt, 'timed out'):
                process.execute([sys.executable, '-c', 'import time; time.sleep(30)'], root, root / 'check.log', 0.3)
            self.assertEqual(process.children, set())
            self.assertIn('child-reaped', (root / 'check.log.lifeline.jsonl').read_text())

    def test_abrupt_parent_death_reaps_worker_and_check_groups_without_finally(self):
        package = Path(__file__).resolve().parents[1]
        for mode in ('worker', 'check', 'supervisor-io'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                # A FIFO proves the whole inherited group closed without reading
                # or signalling any saved PID. Both command and descendant ignore
                # TERM, requiring the guardian's bounded escalation.
                fifo = root / 'child.fifo'
                os.mkfifo(fifo)
                reader = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
                code = ("import os,signal,time; "
                        f"fd=os.open({str(fifo)!r},os.O_WRONLY); "
                        "signal.signal(signal.SIGTERM,signal.SIG_IGN); os.fork(); "
                        "os.write(fd,b'ready'); time.sleep(30)")
                launcher = ("import sys; sys.path.insert(0,sys.argv[1]); "
                            "from pathlib import Path; from sfc_lib import common; "
                            "from sfc_lib.common import Processes; common.SESSION_LIFELINE=True; "
                            "p=Processes(Path(sys.argv[2])/'stop'); " +
                            ("common.run([sys.executable,'-c',sys.argv[3]],timeout=30)" if mode == 'supervisor-io' else
                             "p.execute([sys.executable,'-c',sys.argv[3]],sys.argv[2],Path(sys.argv[2])/'child.log',30)"))
                parent = subprocess.Popen([sys.executable, '-c', launcher, str(package / 'scripts'), str(root), code],
                                          stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                try:
                    seen = b''
                    end = time.monotonic() + 8
                    while len(seen) < 10 and time.monotonic() < end:
                        try:
                            seen += os.read(reader, 10)
                        except BlockingIOError:
                            pass
                        time.sleep(0.02)
                    self.assertEqual(seen, b'readyready')
                    parent.kill()  # Only this owned fixture Popen is killed.
                    parent.wait(5)
                    ended = False
                    end = time.monotonic() + 8
                    while time.monotonic() < end:
                        try:
                            if os.read(reader, 1) == b'':
                                ended = True
                                break
                        except BlockingIOError:
                            pass
                        time.sleep(0.02)
                    self.assertTrue(ended, 'owned group retained a FIFO writer after parent death')
                    if mode != 'supervisor-io':
                        end = time.monotonic() + 3
                        while time.monotonic() < end and 'child-reaped' not in (root / 'child.log.lifeline.jsonl').read_text():
                            time.sleep(0.02)
                        self.assertIn('child-reaped', (root / 'child.log.lifeline.jsonl').read_text())
                finally:
                    if parent.poll() is None:
                        parent.kill()
                    parent.communicate(timeout=5)
                    os.close(reader)


class PackagedTransportTests(unittest.TestCase):
    def test_manifest_launch_and_marker_forwarding_actual_stdio_adapter(self):
        root = Path(__file__).resolve().parents[1]
        manifest = read_json(root / 'plugin.json')['extensions']['com.openai']
        server = read_json(root / manifest['mcpServers'])['mcpServers']['sfc_session']
        self.assertNotIn('cwd', server)  # Native session cwd must be retained.
        hooks = read_json(root / 'hooks/hooks.json')['hooks']
        self.assertNotIn('SessionEnd', hooks)
        self.assertEqual(hooks['UserPromptSubmit'][1]['hooks'][0]['tool'], 'session_attach_hook')
        self.assertLessEqual(hooks['UserPromptSubmit'][0]['hooks'][0]['timeout'], 5)
        self.assertEqual(server['env_vars'], ['SFC_WORKER', 'SFC_TEST_CONTEXT'])
        with tempfile.TemporaryDirectory() as tmp:
            from package_plugin import contents
            package, target = Path(tmp) / 'package', Path(tmp) / 'target'
            target.mkdir()
            for name, data in contents(root).items():
                path = package / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            # Reproduce the declared launch/whitelist, not native host delivery.
            environment = {key: value for key, value in os.environ.items() if key in ('PATH', 'TMPDIR')}
            environment.update({key: '1' for key in server['env_vars']})
            messages = [dict(jsonrpc='2.0', id=1, method='initialize', params={'protocolVersion': '2025-03-26'}),
                        dict(jsonrpc='2.0', method='notifications/initialized'),
                        dict(jsonrpc='2.0', id=2, method='tools/call', params={'name': 'session_attach', 'arguments': {
                            'session_id': 'child', 'cwd': str(target), 'hook_event_name': 'UserPromptSubmit', 'permission_mode': 'dontAsk'}})]
            argv = [server['command'], *[a.replace('${PLUGIN_ROOT}', str(package)) for a in server['args']]]
            result = subprocess.run(argv, cwd=target, env=environment, input=''.join(json.dumps(m) + '\n' for m in messages),
                                    text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            replies = [json.loads(line) for line in result.stdout.splitlines()]
            self.assertEqual(len(replies), 2)
            value = json.loads(replies[1]['result']['content'][0]['text'])
            self.assertEqual(value['state'], 'ignored')
            self.assertEqual(list(target.iterdir()), [])
            self.assertEqual(list(package.rglob('*.pyc')), [])


if __name__ == '__main__':
    unittest.main()
