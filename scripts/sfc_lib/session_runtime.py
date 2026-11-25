"""One observer per native STDIO connection; finite, journaled work windows.

Construction/initialization grants no project authority. Only attachment in the
launch checkout can inspect stored opt-in. Connection UUIDs are generated here,
never restored from caller strings, timestamps or saved PIDs.
"""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import threading
import time
import uuid

from . import autostart
from .common import (Busy, Halt, Stopped, bounded_io, load_config,
                     now, read_json, repo_root, write_json)
from .factory import Factory
from .lifeline import supported

EVENTS = {'SessionStart', 'UserPromptSubmit', 'PreModelRequest'}


def request_attachment(event, environ=None, *, host='codex'):
    """Short command hook records actual root fields, including absent agent_id.

    Root prompt turn IDs bind MCP fallbacks to the full, child-checked event.
    MCP templates use only documented fields that exist on their event. This
    receipt handles optional fields and startup/readiness races without inventing
    a host event or trusting a model-supplied session ID as authority.
    """
    environ = os.environ if environ is None else environ
    if host == 'claude':
        return claude_attachment(event, environ)
    if (environ.get('SFC_WORKER') or environ.get('SFC_TEST_CONTEXT') or event.get('agent_id') or
            event.get('hook_event_name') not in ('SessionStart', 'UserPromptSubmit') or
            not isinstance(event.get('session_id'), str) or not event['session_id'].strip() or
            (event['hook_event_name'] == 'UserPromptSubmit' and
             (not isinstance(event.get('turn_id'), str) or not event['turn_id'].strip())) or
            (event['hook_event_name'] == 'SessionStart' and event.get('source') not in ('startup', 'resume'))):
        return {'state': 'ignored'}
    end = time.monotonic() + 3
    with bounded_io(lambda: max(0.01, end - time.monotonic())):
        from . import chat_tracking
        root = chat_tracking.project_root(event['cwd'])
        if chat_tracking.chat_mode(root):
            return {'state': 'tracking', 'root': str(root),
                    'prefix': 'chat-' + autostart.digest(event['session_id'])[:16],
                    'turn': autostart.digest(event.get('turn_id'))[:12]}
        root = autostart.configured_root(event['cwd'])
        common, path, status_path = autostart.paths(root)
    auth = read_json(path) if path.exists() else {}
    if not isinstance(auth, dict) or not auth.get('enabled') or auth.get('version') != 2:
        return {'state': 'disabled'}
    request = {'authorization': auth['id'], 'root': str(root),
               'host': autostart.digest(event['session_id']), 'recorded_at': now(),
               'turn_id': event.get('turn_id'),
               'eligible': event.get('permission_mode') in ('default', 'acceptEdits', 'dontAsk', 'bypassPermissions'),
               'event': {key: event.get(key) for key in ('hook_event_name', 'source', 'permission_mode')}}
    write_json(common / 'sfc-session-requests' / (request['host'] + '.json'), request)
    value = {'state': 'pending', 'detail': 'Native attachment requested; MCP startup/trust/policy may still be pending', 'recorded_at': now()}
    write_json(status_path, value)
    return value


def claude_attachment(event, environ):
    """Claude Code chat tracking only; no native MCP observer or watcher window.

    Claude's UserPromptSubmit has no turn ID, so each prompt gets a fresh
    random slot. The session prefix still comes from the host session ID, so
    follow-up prompts find the same chat's tasks with `track status --prefix`.
    Subagent and resumed-compaction events are ignored as on Codex.
    """
    if (environ.get('SFC_WORKER') or environ.get('SFC_TEST_CONTEXT') or event.get('agent_id') or
            event.get('hook_event_name') not in ('SessionStart', 'UserPromptSubmit') or
            not isinstance(event.get('session_id'), str) or not event['session_id'].strip() or
            not isinstance(event.get('cwd'), str) or
            (event['hook_event_name'] == 'SessionStart' and event.get('source') not in ('startup', 'resume'))):
        return {'state': 'ignored'}
    end = time.monotonic() + 3
    with bounded_io(lambda: max(0.01, end - time.monotonic())):
        from . import chat_tracking
        try:
            root = chat_tracking.project_root(event['cwd'])
        except (Halt, OSError):
            # Not a Git checkout: no SFC project, so the prompt is left alone.
            return {'state': 'ignored'}
        if not chat_tracking.chat_mode(root):
            return {'state': 'ignored'}
    return {'state': 'tracking', 'root': str(root),
            'prefix': 'chat-' + autostart.digest(event['session_id'])[:16],
            'turn': autostart.digest(uuid.uuid4().hex)[:12]}


class SessionRuntime:
    def __init__(self, cwd=None, *, environ=None, factory_type=Factory, clock=time.time, wait=None):
        self.cwd = Path(cwd or os.getcwd()).resolve()
        self.environ = dict(os.environ if environ is None else environ)
        self.factory_type, self.clock = factory_type, clock
        self.connection = uuid.uuid4().hex
        self.closed = threading.Event()
        self.wait = wait or self.closed.wait
        self.guard = threading.RLock()
        self.thread = None
        self.authorization = None
        self.host = self.root = self.common = self.receipt = None
        self.value = {'state': 'pending', 'work': 'unattached', 'detail': 'Awaiting a trusted native attachment hook'}

    def report(self, state, detail, *, work=None):
        with self.guard:
            self.value.update(state=state, detail=detail, recorded_at=now(), connection=self.connection)
            if self.authorization:
                self.value['authorization'] = self.authorization
            if work is not None:
                self.value['work'] = work
            if self.receipt:
                write_json(self.receipt, self.value)
            return self.value.copy()

    def status(self):
        with self.guard:
            return self.value.copy()

    def failure(self, exc):
        detail = 'Validation/work paused; inspect saved receipts, stop and explicitly reauthorize to recover'
        if self.common:
            error = self.common / 'sfc-session-errors' / self.connection / (uuid.uuid4().hex + '.json')
            write_json(error, {'recorded_at': now(), 'exception': type(exc).__name__, 'detail': str(exc)})
            detail += '; private diagnostic: ' + str(error)
        return self.report('paused', detail, work='paused')

    def attach(self, event, *, background=True, native_thread=None):
        with self.guard:
            if self.closed.is_set():
                return self.report('ended', 'This MCP connection ended', work='stopped')
            if (self.environ.get('SFC_WORKER') or self.environ.get('SFC_TEST_CONTEXT') or
                    not isinstance(event, dict) or event.get('agent_id') or
                    event.get('hook_event_name') not in EVENTS or
                    (event.get('hook_event_name') != 'PreModelRequest' and
                     event.get('permission_mode') not in ('default', 'acceptEdits', 'dontAsk', 'bypassPermissions')) or
                    (event.get('hook_event_name') != 'SessionStart' and
                     (not isinstance(event.get('turn_id'), str) or not event['turn_id'].strip())) or
                    not isinstance(event.get('session_id'), str) or not event['session_id'].strip() or
                    (event['hook_event_name'] == 'SessionStart' and event.get('source') not in ('startup', 'resume'))):
                return {'state': 'ignored', 'detail': 'Ineligible child/test/event/permission context'}
            # This value comes only from Codex's transport metadata, never
            # a model-controlled argument. Child hook session_id may be
            # the parent ID; native threadId identifies the actual caller.
            if (not isinstance(native_thread, str) or not native_thread.strip() or
                    native_thread != event['session_id']):
                return {'state': 'ignored', 'detail': 'Native root connection identity is missing or mismatched'}
            try:
                end = time.monotonic() + 3
                with bounded_io(lambda: max(0.01, end - time.monotonic())):
                    launch_root = repo_root(self.cwd)
                    if repo_root(event['cwd']) != launch_root:
                        raise Halt('Attachment cwd differs from the native MCP launch checkout')
                    from . import chat_tracking
                    root = chat_tracking.project_root(launch_root)
                    if chat_tracking.chat_mode(root):
                        return self.report('tracking', 'This project records work started in chat', work='unattached')
                    root = autostart.configured_root(launch_root)
                    common, path, _ = autostart.paths(root)
                if not supported():
                    return self.report('unavailable', 'Lifetime cleanup is supported only on macOS', work='paused')
                host = autostart.digest(event['session_id'])
                auth = read_json(path) if path.exists() else {}
                if not isinstance(auth, dict):
                    raise Halt('Malformed authorization; inspect retained receipts')
                if not auth.get('enabled') or auth.get('version') != 2:
                    return {'state': 'disabled', 'detail': 'No explicit interactive-session authorization; bounded mode is unchanged'}
                if self.host is not None and (self.host, self.root, self.common) != (host, root, common):
                    return {'state': 'ignored', 'detail': 'This connection is already bound to another host/project'}
                request_path = common / 'sfc-session-requests' / (host + '.json')
                if not request_path.exists():
                    return self.report('pending', 'Awaiting trusted root command-hook request; readiness fallback will retry', work='unattached')
                request = read_json(request_path)
                if (not isinstance(request, dict) or
                        request.get('host') != host or request.get('root') != str(root) or
                        request.get('authorization') != auth.get('id') or request.get('eligible') is not True):
                    raise Halt('Root request/permission/authorization is unavailable or changed')
                # Child hooks share the parent session ID. The command hook
                # checks the complete event before recording this root-turn
                # proof; projected MCP input alone is insufficient.
                if event['hook_event_name'] != 'SessionStart' and (
                        request.get('event', {}).get('hook_event_name') != 'UserPromptSubmit' or
                        request.get('turn_id') != event['turn_id']):
                    return {'state': 'ignored', 'detail': 'No trusted root prompt for this turn'}
                if self.host is not None:
                    if auth['id'] == self.authorization:
                        return self.status()  # Never retry failed work under the same authority.
                    if self.thread and self.thread.is_alive():
                        return {'state': 'pending', 'detail': 'Prior authorization is still stopping; attachment fallback will retry'}
                self.host, self.root, self.common = host, root, common
                self.authorization = auth['id']
                self.receipt = common / 'sfc-session-connections' / (self.connection + '.json')
                self.report('pending', 'Attachment recorded; admission runs independently of this request', work='pending')
                if background:
                    self.thread = threading.Thread(target=self.observe, name='sfc-session-observer', daemon=True)
                    self.thread.start()
                return self.status()
            except (Halt, OSError, ValueError, KeyError, TypeError) as exc:
                return self.failure(exc)

    def authority_changed(self, pinned):
        try:
            current = read_json(self.common / 'sfc-autostart.json')
            request = read_json(self.common / 'sfc-session-requests' / (self.host + '.json'))
            return (current != pinned or not isinstance(request, dict) or
                    request.get('authorization') != pinned['id'] or request.get('eligible') is not True or
                    request.get('root') != str(self.root) or request.get('host') != self.host or
                    autostart.digest(read_json(self.root / '.sfc/config.json')) != pinned['config_digest'])
        except (OSError, ValueError, KeyError, TypeError):
            return True

    def window(self):
        """One bounded attempt; Busy is the only automatically retried exception."""
        path = self.common / 'sfc-autostart.json'
        f = self.factory_type(self.root)
        f.processes.lifetime = True
        f.processes.cancel_check = self.closed.is_set
        auth = read_json(path)
        if not isinstance(auth, dict) or not isinstance(auth.get('scope'), dict):
            raise Halt('Malformed lifetime authorization/scope; preserve receipts and inspect before recovery')
        scope = auth['scope']
        allow_merge = scope.get('allow_merge')

        @contextmanager
        def admission(watcher):
            nonlocal auth
            auth = read_json(path)  # Factory owns the normal common-git lock now.
            if (not isinstance(auth, dict) or auth.get('version') != 2 or
                    auth.get('mode') != 'interactive-session' or auth.get('continue_windows') is not True or
                    not autostart.identifier(auth.get('id')) or auth['id'] != self.authorization or
                    auth.get('enabled') is not True or
                    any(auth.get(k) is not None and not autostart.identifier(auth[k]) for k in ('watcher', 'baseline')) or
                    not isinstance(auth.get('windows'), list) or
                    any(not autostart.identifier(w) for w in auth['windows']) or
                    auth.get('root') != str(self.root) or auth.get('common') != str(self.common) or
                    auth.get('config_digest') != f.config_digest or
                    autostart.digest(load_config(self.root)) != f.config_digest or
                    auth.get('scope') != scope or
                    type(allow_merge) is not bool or not autostart.scope_matches(f, auth, allow_merge) or
                    f.c['max_workers'] > 2):
                raise Halt('Lifetime authority/configuration changed; inspect and explicitly reauthorize')
            request = read_json(self.common / 'sfc-session-requests' / (self.host + '.json'))
            if (not isinstance(request, dict) or
                    request.get('authorization') != self.authorization or request.get('eligible') is not True or
                    request.get('root') != str(self.root) or request.get('host') != self.host):
                raise Halt('Fresh root attachment/permission is required for this authorization')
            if f.processes.stop_requested() or (self.common / 'sfc-stop').exists():
                raise Stopped('Stop/disconnect precedes window admission')
            latest = read_json(watcher.path) if watcher.path.exists() else None
            resume, no_progress = autostart.lifetime_window(auth, latest, self.clock(), f.c['max_no_progress'])
            watcher.start, watcher.resume = resume is None, resume
            autostart.preserve_resume_settings(watcher, latest)
            watcher.lifetime = True
            watcher.lifetime_authorization = self.authorization
            watcher.connection = self.connection
            watcher.initial_no_progress = no_progress
            auth.update(state='starting', connection=self.connection, host=self.host, last_event_at=now())
            write_json(path, auth)  # Durable intent before remote IO; crashes fail closed.
            pinned = read_json(path)
            f.processes.cancel_check = lambda: self.closed.is_set() or self.authority_changed(pinned)
            self.report('observing', 'Bounded supervisor owns the common-git lock', work='active')
            completed = False
            try:
                gate = getattr(f.gh, 'read_wait', None)
                if gate:
                    original_report = gate.report
                    def rate_report(value):
                        if original_report(value) is False:
                            return
                        with self.guard:
                            if gate.latest is not value:
                                return
                            if value['state'] == 'waiting':
                                detail = ('Reconnecting to GitHub after a temporary read failure'
                                          if value.get('reason') == 'connection-reset'
                                          else 'Waiting for GitHub rate-limit reset; reads retry automatically within original limits')
                                self.report('observing', detail, work='waiting')
                            elif self.value.get('work') == 'waiting':
                                self.report('observing', 'GitHub reads resumed; original supervisor still owns the lock', work='active')
                    gate.report = rate_report
                f.gh.verify_repository()
                if f.gh.metadata() != auth['metadata'] or not f.gh.dashboard()['ready']:
                    raise Halt('Authorized board identity/setup changed; explicit recovery required')
                if 'reviewed_identity' in auth:
                    autostart.verify_onboarding_identity(f.gh, auth['reviewed_identity'])
                yield
                completed = True
            except Stopped:
                if (watcher.session is None or f.processes.stop_requested() or
                        f.state.get('outcome') not in autostart.NORMAL_ENDS):
                    raise
                # Cooperative cutoff can occur in ensure_running between stages,
                # outside the future collector. It is still a spent finite
                # window, never a failed-result or ambiguous-write retry grant.
                completed = True
                if watcher.session.get('work_in_flight'):
                    watcher.session['cooperative_cutoff'] = True
                    watcher.save(f.state['outcome'])
            finally:
                # Never overwrite a concurrently disabled/replaced authorization.
                if read_json(path) == pinned:
                    if watcher.session is not None:
                        auth['watcher'] = watcher.session['id']
                        if auth['watcher'] not in auth['windows']:
                            auth['windows'].append(auth['watcher'])
                    operator = (self.common / 'sfc-stop').exists() or f.processes.stop_path.exists()
                    outcome = f.state.get('outcome')
                    safe_idle = watcher.session is not None and not watcher.session.get('work_in_flight')
                    safe_window = safe_idle or bool(watcher.session and watcher.session.get('cooperative_cutoff'))
                    auth['state'] = ('stopped' if operator else
                                     'suspended' if self.closed.is_set() and safe_idle else
                                     'window-finished' if completed and outcome in autostart.NORMAL_ENDS and safe_window else 'failed')
                    auth['finished_at'] = now()
                    write_json(path, auth)

        f.watch(start=True, allow_merge=allow_merge, overrides=autostart.recovery_overrides(auth),
                admission=admission,
                clock=self.clock, wait=self.wait)
        if self.closed.is_set():
            return self.report('ended', 'Native MCP transport ended; receipts retained', work='stopped')
        if read_json(path).get('state') != 'window-finished':
            raise Halt('Work paused at an unsafe/failed window boundary; inspect receipts and explicitly reauthorize')
        return self.report('observing', 'Window exhausted; next identity preserves card journals and no-progress spending', work='pending')

    def observe(self):
        while not self.closed.is_set():
            try:
                self.window()
            except Busy:
                self.report('waiting', 'Another supervisor owns this common git directory; no lock stealing', work='waiting')
            except Exception as exc:
                # Background failures must not leave a dead observer reporting
                # active work. Retain diagnostics and pause; never retry them.
                self.failure(exc)
                # Stay connected for status/transport cleanup, but never retry a
                # failed/ambiguous write or paid-work attempt automatically.
                return
            self.wait(0.1)

    def close(self):
        self.closed.set()
        if self.thread:
            self.thread.join(timeout=5)
        pending = bool(self.thread and self.thread.is_alive())
        return self.report('ended', 'Native MCP connection ended; cancellation requested; inspect retained cleanup receipts',
                           work='cancellation-pending' if pending else 'stopped')
