"""Trusted host hook -> existing watcher, under the repository supervisor lock.

Hook trust is enforced by Codex, not by a repository-supplied 'trusted' flag.
Local authorization pins the project independently of hook trust.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import uuid

from .common import Halt, Stopped, common_dir, git, load_config, now, read_json, repo_root, write_json
from .factory import Factory
from .watch import settings

NORMAL_ENDS = {'watch-time-limit', 'watch-polls-limit', 'watch-claims-limit', 'cycle-limit'}


def identifier(value, length=32):
    return isinstance(value, str) and re.fullmatch('[a-f0-9]{' + str(length) + '}', value) is not None


def bounded_receipt_valid(auth):
    """Validate the receipt shape shared by bounded startup and readback."""
    return (isinstance(auth, dict) and type(auth.get('version')) is int and auth['version'] == 1 and
            auth.get('enabled') is True and identifier(auth.get('id')) and
            auth.get('state') in ('armed', 'starting', 'finished', 'cancelled', 'failed', 'stopped') and
            all(k in auth and (auth[k] is None or identifier(auth[k])) for k in ('watcher', 'baseline')) and
            isinstance(auth.get('hosts'), list) and all(identifier(host, 64) for host in auth['hosts']) and
            isinstance(auth.get('scope'), dict) and type(auth['scope'].get('allow_merge')) is bool)


def lifetime_window(auth, latest, current, max_no_progress):
    """Select the next window using the saved authority and carried spending."""
    if (latest['id'] if latest else None) != (auth['watcher'] or auth['baseline']):
        raise Halt('Another watcher replaced the pinned window; inspect and explicitly reauthorize')
    owned = latest and latest['id'] == auth['watcher']
    state = auth.get('state')
    resume = None
    if state == 'suspended' and owned:
        if latest.get('work_in_flight'):
            raise Halt('Interrupted work requires explicit recovery; counters and receipts retained')
        # An idle transport interruption may transfer only its unexpired window.
        if current < latest['deadline']:
            resume = latest['id']
    elif state in ('armed', 'window-finished'):
        if state == 'window-finished' and (not latest or latest['reason'] not in NORMAL_ENDS):
            raise Halt('Prior window did not finish normally; explicit recovery required')
        if owned and latest.get('work_in_flight') and not latest.get('cooperative_cutoff'):
            raise Halt('Work interrupted at a window cutoff; explicit recovery required')
    else:
        raise Halt('Prior work failed, stopped or has an incomplete intent; inspect and explicitly reauthorize')
    no_progress = latest['no_progress'] if owned else 0
    if no_progress >= max_no_progress:
        raise Halt('No-progress allowance exhausted; new windows cannot reset it')
    return resume, no_progress


def bounded_window(auth, latest, host=None):
    """Select a bounded start/resume. A scope review considers a future host."""
    if (latest['id'] if latest else None) != (auth['watcher'] or auth['baseline']):
        raise Halt('A manual watcher replaced the authorized session; inspect it and explicitly re-enable')
    state = auth['state']
    if state == 'cancelled' and latest and latest['id'] == auth['watcher']:
        return latest['id']
    if state in ('armed', 'finished') and (host is None or host not in auth['hosts']):
        return None
    raise Halt('This chat already spent its watcher budget, or a prior start failed/stopped; inspect autostart-status and re-enable explicitly')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def paths(cwd):
    common = common_dir(cwd)
    return common, common / 'sfc-autostart.json', common / 'sfc-autostart-status.json'


def configured_root(cwd):
    common, path, _ = paths(cwd)
    auth = read_json(path) if path.exists() else {}
    if auth.get('root'):
        root = Path(auth['root']).resolve()
    elif Path(git(cwd, 'rev-parse', '--absolute-git-dir')).resolve() == common:
        # --separate-git-dir has no reverse pointer to its main checkout;
        # establish authorization there first, then reuse the saved root.
        root = repo_root(cwd)
    else:
        # Never select a linked checkout's incidental config over the main one.
        main = git(cwd, 'worktree', 'list', '--porcelain', '-z').split('\0', 1)[0]
        if not main.startswith('worktree '):
            raise Halt('Cannot resolve main checkout; enable auto-start from the configured checkout')
        root = Path(main.removeprefix('worktree ')).resolve()
    if repo_root(root) != root or common_dir(root) != common:
        raise Halt('Auto-start checkout/common git root changed; inspect local authorization')
    return root


def summary(factory, allow_merge=False):
    c = factory.c
    return {'root': str(factory.root), 'repository': c['repo'], 'project': c['project'],
            'board': f"{c['project']['owner']} Project {c['project']['number']}",
            'watch': settings(c.get('watch')), 'merge_policy': c['merge_policy'],
            'merge_mode': c.get('merge_mode', 'github-protected'), 'allow_merge': allow_merge,
            'limits': {k: c[k] for k in ('max_cycles', 'max_no_progress', 'max_attempts', 'auto_max_lines')}}


def legacy_recovery_scope(auth):
    scope = auth.get('scope')
    watch = scope.get('watch') if isinstance(scope, dict) else None
    return isinstance(watch, dict) and 'max_recoveries_per_ticket' not in watch


def scope_matches(factory, auth, allow_merge):
    """Compare legacy authority without granting newly introduced recovery."""
    expected = summary(factory, allow_merge)
    if legacy_recovery_scope(auth) and 'max_recoveries_per_ticket' not in factory.c.get('watch', {}):
        expected['watch'].pop('max_recoveries_per_ticket')
    return auth.get('scope') == expected


def recovery_overrides(auth):
    return {'max_recoveries_per_ticket': 0} if legacy_recovery_scope(auth) else {}


def preserve_resume_settings(watcher, latest):
    if watcher.resume and 'max_recoveries_per_ticket' not in latest['settings']:
        watcher.settings.pop('max_recoveries_per_ticket', None)


def verify_onboarding_identity(gh, expected):
    if gh.onboarding_identity() != expected:
        raise Halt('Reviewed GitHub repository or Project identity changed; inspect and preview again')


def configure(factory, *, enabled, allow_merge=False, lifetime=False,
              reviewed_identity=None, expected_metadata=None):
    """Explicit user choice only; caller holds the common supervisor lock."""
    if enabled and factory.c.get('workflow_mode') == 'chat':
        raise Halt('Chat tracking is enabled. Automatic board execution is disabled.')
    common, path, status_path = paths(factory.root)
    old = read_json(path) if path.exists() else None
    if old is not None and (not isinstance(old, dict) or
                            (old.get('id') is not None and not identifier(old['id']))):
        raise Halt('Malformed local auto-start receipt; preserve and inspect it before recovery')
    if allow_merge and (not enabled or factory.c['merge_policy'] != 'auto'):
        raise Halt('Auto-start merge authorization requires enabled auto-start and configured auto policy')
    if lifetime:
        from .lifeline import supported
        if not enabled or not supported() or factory.c['max_workers'] > 2:
            raise Halt('Lifetime authorization requires --enable, macOS cleanup support and max_workers <= 2')
    if enabled:
        factory.gh.verify_repository()
        metadata = factory.gh.metadata()
        if not factory.gh.dashboard()['ready']:
            raise Halt('Finish onboarding/dashboard setup before authorizing auto-start')
        watch_path = common / 'sfc-watch.json'
        previous = read_json(watch_path) if watch_path.exists() else None
        if previous and (previous['root'] != str(factory.root) or
                         previous['reason'] not in NORMAL_ENDS | {'stopped', 'halted', 'no-progress-limit'}):
            raise Halt('Recorded watcher needs manual recovery before re-authorizing auto-start')
        if expected_metadata is not None:
            # The dashboard read can use cached routing metadata. Refresh it on
            # the saved node, then check the live Project lookup before arming.
            metadata = factory.gh.metadata(project_id=expected_metadata['id'])
            if metadata != expected_metadata:
                raise Halt('Project routing changed before saving watcher authority; inspect onboarding')
            if reviewed_identity is None and factory.gh.project_id() != expected_metadata['id']:
                raise Halt('Saved GitHub Project identity changed; inspect onboarding')
        if reviewed_identity is not None:
            verify_onboarding_identity(factory.gh, reviewed_identity)
        auth = {'version': 1, 'id': uuid.uuid4().hex, 'enabled': True,
                'root': str(factory.root), 'common': str(common), 'config_digest': factory.config_digest,
                'scope': summary(factory, allow_merge), 'metadata': metadata,
                'baseline': previous['id'] if previous else None,
                'watcher': None, 'hosts': [], 'state': 'armed', 'authorized_at': now()}
        if lifetime:
            auth.update(version=2, mode='interactive-session', continue_windows=True, windows=[])
        if reviewed_identity is not None:
            auth['reviewed_identity'] = reviewed_identity
    else:
        auth = {**(old or {}), 'enabled': False, 'root': str(factory.root), 'disabled_at': now()}
    if old and old.get('id'):
        write_json(common / 'sfc-autostart-history' / (old['id'] + '-' + uuid.uuid4().hex + '.json'), old)
    write_json(path, auth)
    if enabled:
        (common / 'sfc-stop').unlink(missing_ok=True)
        (factory.root / '.sfc/stop').unlink(missing_ok=True)
    write_json(status_path, {'state': 'armed' if enabled else 'disabled', 'recorded_at': now(),
                             'detail': 'Starts only on a trusted project chat startup/resume; opening the app is insufficient'})
    return auth


def status(cwd):
    common, path, status_path = paths(cwd)
    return {'authorization': read_json(path) if path.exists() else {'enabled': False},
            'last_event': read_json(status_path) if status_path.exists() else None,
            'recorded_connections': [read_json(p) for p in sorted(
                (common / 'sfc-session-connections').glob('*.json'), key=lambda p: p.stat().st_mtime)[-10:]],
            'connection_freshness': 'Recorded facts only; use session_status on the actual MCP connection for current status',
            'detail': 'If no hook runs, review SFC hooks in Codex /hooks or Settings > Hooks; SFC cannot observe skipped/untrusted hooks'}


def eligible(event, environ):
    # No transcript parsing or invented host trust fields. Headless SFC children
    # inherit the marker, including project checks which might invoke Codex.
    return (not environ.get('SFC_WORKER') and not environ.get('SFC_TEST_CONTEXT') and
            not event.get('agent_id') and
            event.get('hook_event_name') == 'SessionStart' and
            event.get('source') in ('startup', 'resume') and
            event.get('permission_mode') in ('default', 'acceptEdits', 'dontAsk', 'bypassPermissions') and
            isinstance(event.get('session_id'), str) and bool(event['session_id'].strip()))


def launch(event, *, environ=None, cancelled=lambda: False, factory_type=Factory, **timing):
    """Run in the host-owned async hook process; never detach a supervisor."""
    if not eligible(event, os.environ if environ is None else environ):
        return {'state': 'ignored', 'detail': 'Not an eligible user project startup/resume'}
    common, path, status_path = paths(event['cwd'])
    def report(state, detail):
        value = {'state': state, 'detail': detail, 'recorded_at': now()}
        write_json(status_path, value)
        return value
    try:
        from .chat_tracking import chat_mode
        root = configured_root(event['cwd'])
        if chat_mode(root):
            return {'state': 'ignored', 'detail': 'This project uses chat tracking'}
        auth = read_json(path) if path.exists() else {}
        if not isinstance(auth, dict):
            raise Halt('Auto-start authorization is malformed; inspect and explicitly re-enable')
        if not auth.get('enabled'):
            return report('disabled', 'Auto-start is off; explicitly enable it during SFC onboarding or with autostart --enable')
        if auth.get('version') == 2:
            return report('pending', 'Lifetime mode awaits the trusted session_attach MCP hook; this command hook owns no observer')
        original_auth = auth
        f = factory_type(root)
        f.processes.cancel_check = cancelled
        host = digest(event['session_id'])

        @contextmanager
        def admission(watcher):
            # Entered and finalized INSIDE the same common lock as run/watch.
            auth = read_json(path)
            if (not bounded_receipt_valid(auth) or auth['id'] != original_auth['id'] or
                    auth.get('root') != str(root) or auth.get('common') != str(common) or
                    auth.get('config_digest') != f.config_digest or
                    digest(load_config(root)) != f.config_digest or
                    auth.get('scope') != original_auth.get('scope') or
                    auth['scope']['allow_merge'] != f.allow_merge or
                    not scope_matches(f, auth, f.allow_merge)):
                raise Halt('Auto-start authorization/configuration changed or incomplete; review scope and explicitly re-enable')
            if (common / 'sfc-stop').exists() or (root / '.sfc/stop').exists() or cancelled():
                raise Halt('Stop/cancellation is pending; inspect receipts and explicitly re-enable after recovery')
            latest = read_json(watcher.path) if watcher.path.exists() else None
            watcher.resume = bounded_window(auth, latest, host)
            watcher.start = watcher.resume is None
            preserve_resume_settings(watcher, latest)
            if host not in auth['hosts']:
                auth['hosts'].append(host)
            auth.update(state='starting', last_event_at=now())
            write_json(path, auth)  # Intent precedes IO; abrupt death cannot retry it.
            report('starting', 'Validating saved project scope before watcher admission')
            try:
                f.gh.verify_repository()
                if f.gh.metadata() != auth['metadata'] or not f.gh.dashboard()['ready']:
                    raise Halt('Authorized board identity/setup changed; repair and explicitly re-enable')
                if 'reviewed_identity' in auth:
                    verify_onboarding_identity(f.gh, auth['reviewed_identity'])
                yield
            except BaseException as exc:
                auth['state'] = 'cancelled' if isinstance(exc, Stopped) and cancelled() else 'failed'
                raise
            else:
                # The factory can observe host cancellation between iterations
                # and return normally instead of raising Stopped.
                outcome = f.state.get('outcome')
                if outcome == 'stopped' and cancelled():
                    auth['state'] = 'cancelled'
                else:
                    auth['state'] = 'finished' if outcome in NORMAL_ENDS else 'failed'
            finally:
                if watcher.session is not None:
                    auth['watcher'] = watcher.session['id']
                operator_stop = (common / 'sfc-stop').exists() or (root / '.sfc/stop').exists()
                if operator_stop:
                    auth['state'] = 'stopped'
                auth['finished_at'] = now()
                write_json(path, auth)
                report(auth['state'], 'Watcher receipts retained; use watch-status/autostart-status before recovery')

        result = f.watch(start=True, allow_merge=original_auth['scope']['allow_merge'],
                         overrides=recovery_overrides(original_auth), admission=admission, **timing)
        return {'state': 'finished', 'outcome': result['outcome'], 'detail': 'Bounded watcher finished; receipts retained'}
    except (Halt, OSError, ValueError, KeyError, TypeError) as exc:
        return report('waiting', str(exc) + '; inspect autostart-status and saved receipts')
