"""Bounded watcher recovery of supervisor-observed conditions, never worker prose.

The ordinary scheduler owns slots and priority. A spent recovery is not a new
Build grant, findings refund, merge permission or publication permission.
"""
from pathlib import Path
import copy
import datetime as dt
import json
import os
import threading
import uuid

from . import pickup
from .common import Halt, Stopped, TimedOut, git, common_dir, now, read_json
from .priority import ProjectItemMissing, connection
from .rate_limit import ReadConnectionReset
from .reasons import RECOVERY_ITEM_MISSING, RECOVERY_READ_WAIT
from .worker_recovery import digest

FAILED_CHECK = 'failed-project-check'
SUPPORTED = {'transient-read', 'dependency', 'missing-check-evidence', FAILED_CHECK}
STAGES = {'Building', 'QA', 'Review'}


class MissingCheckEvidence(Halt):
    pass


def _evidence(f, paths):
    from .verification import checksum, fingerprint_budget

    guard = fingerprint_budget(f)
    evidence = {}
    for name in paths:
        path = Path(name)
        if path.is_symlink() or not path.resolve().is_relative_to((f.dir / 'runs').resolve()):
            raise Halt('Recovery evidence escapes retained runs')
        if path.exists() and not path.is_file():
            raise Halt('Recovery evidence is not a regular file')
        evidence[name] = checksum(path, guard) if path.is_file() else None
    return evidence


def binding(f, card, *, retained_verification=None):
    r = f.state['cards'].get(str(card['number']), {})
    claim = r.get('claim', {})
    if (claim.get('state') != 'verified' or any(
            claim.get('observed', {}).get(k) != card.get(k) for k in ('id', 'number', 'item_id')) or
            any(r.get(k) for k in ('ticket_failure', 'handoff', 'local_merge', 'external_completion',
                                  'release_completion', 'pending_base')) or
            r.get('workflow_kind') == 'release' or f.creation_problem(card) or
            r.get('activity', {}).get('pending') or r.get('activity', {}).get('error')):
        raise Halt('Recovery requires an unambiguous retained supervisor claim')
    wt = Path(r['worktree']).resolve()
    if (not wt.is_relative_to((f.dir / 'worktrees').resolve()) or
            common_dir(wt) != common_dir(f.root) or
            git(wt, 'branch', '--show-current') != r['branch'] or
            git(wt, 'rev-parse', 'HEAD') != r.get('head', r['base']) or
            git(wt, 'status', '--porcelain', '--untracked-files=all')):
        raise Halt('Recovery source or worktree changed; inspect saved work')
    # Keep both present bytes and absence pinned. Late worker/check output is
    # drift, not a replacement for the original failed attempt.
    paths = []
    for budget in r.get('worker_output_budget', {}).values():
        for dispatch in budget['dispatches']:
            paths.extend(dispatch[k] for k in ('log', 'result_path'))
            paths.append(str(Path(dispatch['result_path']).parent / 'receipt.json'))
    blocker = r.get('blocker', {})
    retained = retained_verification or blocker.get('legacy_verification') or blocker.get('failed_verification', {})
    for receipt in [retained, r.get('verification', {}), *r.get('receipts', {}).values()]:
        paths.extend(receipt[k] for k in ('receipt_path',) if receipt.get(k))
        for check in receipt.get('checks', []):
            for execution in (check, check.get('reuse') or {}):
                paths.extend(execution[k] for k in ('log', 'receipt_path') if execution.get(k))
    return {'config': f.config_digest, 'task': f.task_digest(card), 'scope': card['_pickup_scope'],
            'identity': {k: card[k] for k in ('number', 'id', 'item_id', 'url', 'repository')},
            'claim': digest(claim), 'head': r.get('head', r['base']), 'base': r['base'],
            'branch': r['branch'], 'pr': r.get('pr'), 'worktree': str(wt),
            'attempts': r['attempts'], 'budgets': digest(r.get('worker_output_budget', {})),
            'receipts': digest(r.get('receipts', {})), 'verification': digest(r.get('verification', {})),
            'evidence': _evidence(f, paths)}


def _matches_binding(f, current, original, *, replacing_verification, retaining_prior=False):
    if not replacing_verification:
        if not retaining_prior:
            return current == original
        if current['verification'] != original['verification']:
            return False
    if any(current.get(k) != v for k, v in original.items() if k not in ('verification', 'evidence')):
        return False
    # A new receipt can omit original paths, but their bytes or absence remain pinned.
    retained = current['evidence']
    evidence = original['evidence']
    displaced = {path: value for path, value in evidence.items() if path not in retained}
    return (all(retained[path] == value for path, value in evidence.items() if path in retained) and
            _evidence(f, displaced) == displaced)


def mark(f, card, kind, stage):
    """Called only at a known supervisor failure, never by interpreting text."""
    r = f.record(card)
    if kind not in SUPPORTED or stage not in STAGES:
        return
    try:
        pins = binding(f, card)
    except (Stopped, TimedOut):
        raise
    except (Halt, KeyError, OSError):
        return  # Unsupported/dirty/ambiguous work needs explicit owner recovery.
    f.update(card, blocker={'kind': kind, 'stage': stage, 'since': now(), 'binding': pins})


def check_request(f, result):
    """Only configured numeric check IDs can request execution; no shell text."""
    request = result.get('recovery')
    if request is None:
        return []
    if (not isinstance(request, dict) or set(request) != {'kind', 'check_ids'} or
            request['kind'] != 'missing-check-evidence' or result.get('verdict') != 'blocked' or
            result.get('findings') or not isinstance(request['check_ids'], list) or not request['check_ids']):
        raise ValueError('Invalid structured recovery check request')
    # Older accepted results used the public check-N labels. Worker packets
    # identify those same configured checks by their one-based integer IDs.
    legacy_ids = {f'check-{i}': i for i in range(1, len(f.c['verify_commands']) + 1)}
    ids = [legacy_ids.get(v) if isinstance(v, str) else v for v in request['check_ids']]
    if (any(type(v) is not int or not 1 <= v <= len(legacy_ids) for v in ids) or
            len(ids) != len(set(ids))):
        raise ValueError('Invalid structured recovery check request')
    return ids


def legacy_blocker(f, card, record):
    """Recognize a gap in the exact evidence of the last accepted Blocked lane."""
    from .verification import fingerprint_budget, load

    if (record.get('blocker') or not record.get('pr') or
            record.get('claim', {}).get('observed', {}).get('_pickup_scope') != card.get('_pickup_scope')):
        return None
    try:
        feedback = json.loads(record['feedback'])
        if feedback['verdict'] != 'blocked' or feedback['findings']:
            return None
        dispatch = max((entry for budget in record.get('worker_output_budget', {}).values()
                        for entry in budget['dispatches']), key=lambda entry: entry.get('time', ''), default={})
        if dispatch.get('status') != 'accepted' or dispatch.get('lane') not in ('qa', 'security', 'review'):
            return None
        result_path = Path(dispatch['result_path'])
        receipt_path = result_path.parent / 'receipt.json'
        files = _evidence(f, (str(receipt_path), dispatch['result_path'], dispatch['log']))
        receipt = read_json(receipt_path)
        fields = {'head': record['head'], 'base': record['base'], 'config_digest': f.config_digest,
                  'task_digest': f.task_digest(card)}
        if (receipt['kind'] not in ('review', 'baseline-source-review') or
                any(dispatch[k] != v or receipt[k] != v for k, v in fields.items()) or
                any(receipt[k] != dispatch[k] for k in ('log', 'result_path', 'lane')) or
                receipt['result'] != feedback or read_json(result_path) != feedback or
                files[dispatch['result_path']] != receipt['result_sha256'] or
                files[dispatch['log']] != receipt['log_sha256']):
            return None
        verification = receipt['verification']
        pins = binding(f, card, retained_verification=verification)
        guard = fingerprint_budget(f)
        if (load(verification, guard)['kind'] != 'verification' or verification['passed'] is not True or
                any(verification[k] != v for k, v in fields.items()) or
                dispatch['worktree'] != pins['worktree'] or verification['worktree'] != pins['worktree'] or
                verification['repository'] != f.c['repo'] or
                [check['command'] for check in verification['checks']] != f.c['verify_commands']):
            return None
        # A previous worker's explanation is free text. Require an actual
        # missing execution receipt or raw log from that exact packet.
        missing = False
        for check in verification['checks']:
            execution = check.get('reuse') or check
            log_sha = pins['evidence'][execution['log']]
            receipt_sha = pins['evidence'][execution['receipt_path']]
            if (check['exit_code'] != 0 or execution['exit_code'] != 0 or execution['command'] != check['command'] or
                    check['log'] != execution['log'] or
                    log_sha not in (None, execution['log_sha256']) or
                    receipt_sha not in (None, execution['receipt_sha256'])):
                return None
            missing |= log_sha is None or receipt_sha is None
        if missing:
            return {'kind': 'missing-check-evidence', 'stage': 'QA', 'since': receipt['time'],
                    'binding': pins, 'legacy_verification': verification}
    except (Stopped, TimedOut):
        raise
    except (Halt, OSError, ValueError, KeyError, TypeError):
        return None
    return None


def failed_check_id(f, card, receipt):
    from .verification import fingerprint_budget, load, original

    record = f.record(card)
    pins = {'head': record['head'], 'base': record['base'], 'worktree': record['worktree'],
            'config_digest': f.config_digest, 'task_digest': f.task_digest(card), 'repository': f.c['repo']}
    guard = fingerprint_budget(f)
    checks = receipt['checks']
    if (receipt.get('kind') != 'verification' or receipt.get('passed') is not False or
            any(receipt.get(k) != value for k, value in pins.items()) or
            not 0 < len(checks) <= len(f.c['verify_commands']) or
            [check['command'] for check in checks] != f.c['verify_commands'][:len(checks)]):
        raise ValueError('Failed verification does not match the retained candidate')
    _evidence(f, (receipt['receipt_path'],))
    load(receipt, guard)
    for index, check in enumerate(checks, 1):
        ref = check.get('reuse') or check
        hashes = _evidence(f, (ref['receipt_path'], ref['log']))
        execution, _ = original(check, guard)
        if (check['exit_code'] != execution['exit_code'] or type(execution['exit_code']) is not int or
                bool(execution['exit_code']) != (index == len(checks)) or
                check['command'] != execution['command'] or check['log'] != execution['log'] or
                hashes[execution['log']] != execution['log_sha256']):
            raise ValueError('Failed verification execution evidence differs')
        if execution['exit_code'] and (check.get('reuse') or any(execution.get(k) != value for k, value in pins.items())):
            raise ValueError('Failed execution does not match the retained candidate')
    return len(checks)


def legacy_failed_check(f, card, record):
    """Recover the failed check that led to a spent, blocked recovery Build."""
    from .ticket_recovery import journal

    if (not record.get('pr') or record.get('verification') or record.get('receipts') or
            record.get('claim', {}).get('observed', {}).get('_pickup_scope') != card.get('_pickup_scope')):
        return None
    budgets = record.get('worker_output_budget', {})
    builds = budgets.get('build', {}).get('dispatches', [])
    if not builds or any(budgets.get(lane, {}).get('dispatches') for lane in ('qa', 'security', 'review')):
        return None
    dispatch = builds[-1]
    grants = [g for g in record.get('ticket_recoveries', []) if g.get('kind') == 'checks' and
              g.get('status') == 'spent' and g['request_id'] == dispatch.get('ticket_recovery_id')]
    if dispatch.get('status') != 'accepted' or len(grants) != 1:
        return None
    try:
        g = grants[0]
        journal(f, g)
        old = g['binding']
        pins = binding(f, card)
        if (any(old[k] != pins[k] for k in ('head', 'base', 'branch', 'worktree', 'config', 'task', 'claim')) or
                old['pr']['number'] != pins['pr'] or old['pr']['headRefOid'] != pins['head'] or
                _evidence(f, [entry['path'] for entry in old['evidence']]) !=
                {entry['path']: entry.get('sha256') for entry in old['evidence']}):
            return None
        result = json.loads(record['feedback'])
        result_path = Path(dispatch['result_path'])
        worker = read_json(result_path.parent / 'receipt.json')
        if (result['verdict'] != 'blocked' or read_json(result_path) != result or worker['result'] != result or
                any(worker[k] != dispatch[k] or dispatch[k] != value for k, value in {
                    'head': pins['head'], 'base': pins['base'], 'config_digest': pins['config'],
                    'task_digest': pins['task']}.items()) or dispatch['worktree'] != pins['worktree']):
            return None
        refs = [entry for entry in old['evidence']
                if Path(entry['path']).name == 'receipt.json' and Path(entry['path']).parent.name.startswith('verify-')]
        if len(refs) != 1:
            return None
        path = refs[0]['path']
        verification = {**read_json(Path(path)), 'receipt_path': path, 'receipt_sha256': refs[0]['sha256']}
        failed_check_id(f, card, verification)
        return {'kind': FAILED_CHECK, 'stage': 'QA', 'since': worker['time'],
                'binding': binding(f, card, retained_verification=verification), 'failed_verification': verification}
    except (Stopped, TimedOut):
        raise
    except (Halt, OSError, ValueError, KeyError, TypeError):
        return None


def effective_blocker(f, card, record, kinds):
    saved = record.get('blocker') or {}
    if FAILED_CHECK in kinds and saved.get('kind') in (None, 'human-or-unsupported'):
        if result := legacy_failed_check(f, card, record):
            return result
    if not saved and 'missing-check-evidence' in kinds:
        return legacy_blocker(f, card, record) or {}
    return saved


def defer_read(watcher, card):
    # Scheduler slot refill polls can run immediately. Give other tickets a
    # chance before this read is retried on the normal poll cadence.
    watcher.recovery_read_retry_at[card['number']] = watcher.clock() + watcher.settings['poll_interval']


def waiting_on_read(watcher, card):
    retry_at = watcher.recovery_read_retry_at.get(card['number'])
    return retry_at is not None and watcher.clock() < retry_at


def eligibility(f, card):
    r = f.record(card)
    if not f.watcher or card['status'] != 'Blocked' or r.get('status') != 'Blocked':
        return False, 'No owned Blocked execution'
    if (card['number'] not in f.issue_scope or f.watcher.excludes(card) or card['state'] != 'OPEN'):
        return False, 'Watcher hold label or explicit exclusion'
    if r.get('recovery_wait'):
        return False, r['recovery_wait']
    session = f.watcher.session or {'policy': f.watcher.policy()}
    kinds = session['policy'].get('blocked_recovery_kinds', [])
    b = effective_blocker(f, card, r, kinds)
    if b.get('kind') not in SUPPORTED:
        return False, 'Human decision or unsupported blocker; explicit recovery required'
    if b['kind'] not in kinds:
        return False, 'Saved watcher policy does not authorize this recovery kind'
    if b.get('binding', {}).get('config') != f.config_digest or b['binding'].get('task') != f.task_digest(card):
        return False, 'Recovery source or scope drift; inspect retained evidence'
    if b['binding'].get('scope') != card.get('_pickup_scope'):
        return False, 'Recovery source or scope drift; inspect retained evidence'
    actions = r.get('blocked_recoveries', [])
    pending = next((a for a in reversed(actions) if a['phase'] in ('reserved', 'move-intent', 'admitted')), None)
    if pending:
        if pending['blocker'] != b or not same_window_scope(f, pending, session):
            return False, 'Recovery reservation belongs to another blocker or watcher'
    elif b['kind'] == FAILED_CHECK and any(a['blocker']['kind'] == FAILED_CHECK for a in actions):
        return False, 'The one-time failed project check retry was spent'
    elif (len(actions) + sum(bool(g.get('automatic')) for g in r.get('ticket_recoveries', [])) >=
          f.watcher.settings.get('max_recoveries_per_ticket', 0)):
        return False, 'Automatic recovery allowance exhausted; explicit recovery required'
    if r['attempts'] >= f.c['max_attempts'] and b.get('stage') == 'Building':
        return False, 'Findings attempt budget exhausted; explicit recovery required'
    if waiting_on_read(f.watcher, card):
        return False, RECOVERY_READ_WAIT
    problems = pickup.prerequisite_problems(f.gh, card)
    if problems:
        return False, problems[0]
    return True, 'Supervisor condition ready for verified recovery'


class RecoveryPinMismatch(Halt):
    pass


class RecoveryItemMissing(RecoveryPinMismatch):
    pass


class RecoveryPrerequisite(Halt):
    pass


class RecoveryHeld(Halt):
    pass


class RecoveryReadDeferred(ReadConnectionReset):
    pass


class RecoveryStageUncertain(Halt):
    pass


def same_window_scope(f, action, session):
    if action['session'] == session.get('id'):
        return True
    if not session.get('id'):
        return False
    try:
        old = read_json(f.dir / 'watch-sessions' / (action['session'] + '.json'))
    except (OSError, ValueError):
        return False
    return (old.get('id') == action['session'] and old.get('root') == session.get('root') and
            old.get('config_digest') == session.get('config_digest') and
            old.get('policy') == session.get('policy') and
            old.get('allow_merge') == session.get('allow_merge') and
            old.get('lifetime', {}).get('authorization') == session.get('lifetime', {}).get('authorization'))


def admitted(record):
    actions = record.get('blocked_recoveries', [])
    return actions[-1] if actions and actions[-1]['phase'] == 'admitted' else None


def pending_stage(record):
    actions = record.get('blocked_recoveries', [])
    if not actions:
        return False
    action = actions[-1]
    transition = action.get('stage_transition') or action.get('review_transition') or {}
    return action['phase'] == 'move-intent' or transition.get('phase') == 'intent'


def permits(f, card, r, *, acknowledge=False):
    action = admitted(r)
    if not action or not f.watcher or (not acknowledge and (
            card['number'] not in f.issue_scope or f.watcher.excludes(card))):
        return False
    saved = action.get('checked_binding') or {}
    return (same_window_scope(f, action, f.watcher.session or {}) and
            card['state'] == 'OPEN' and card['status'] == r.get('status') and
            (card['status'] == action['resumed_stage'] or
             {card['status'], action['resumed_stage']} <= {'QA', 'Review'}) and
            saved.get('config') == f.config_digest and saved.get('task') == f.task_digest(card) and
            saved.get('scope') == card.get('_pickup_scope') and
            saved.get('identity') == {k: card[k] for k in ('number', 'id', 'item_id', 'url', 'repository')} and
            saved.get('claim') == digest(r.get('claim')) and
            (action.get('published') or saved).get('head') == r.get('head', r.get('base')) and
            (action.get('published') or saved).get('pr') == r.get('pr') and
            all(saved.get(k) == r.get(k) for k in ('base', 'branch', 'worktree', 'attempts')))


def require_retained_evidence(f, action):
    if not action:
        return
    original = action['blocker']['binding']
    evidence = {}
    try:
        for binding in (original, action.get('start_binding', original), action['checked_binding']):
            for path, sha in binding['evidence'].items():
                if path in evidence and evidence[path] != sha:
                    raise RecoveryPinMismatch('Recovered evidence pins disagree')
                evidence[path] = sha
        if _evidence(f, evidence) != evidence:
            raise RecoveryPinMismatch('Recovered evidence changed before dispatch')
    except (Stopped, TimedOut):
        raise
    except (Halt, KeyError, OSError) as exc:
        raise RecoveryPinMismatch('Recovered evidence changed before dispatch') from exc


def require_pins(f, card, worktree, base, *, check_live=False, allow_dirty=False, candidate_head=None,
                 integration_worktree=None, acknowledge=False, dependency_block=False):
    record = f.record(card)
    action = admitted(record)
    if not action:
        return
    checked = action.get('checked_binding') or {}
    transition = action.get('stage_transition') or action.get('review_transition') or {}
    if acknowledge and (check_live or transition.get('phase') != 'intent' or transition['before'] != card):
        raise RecoveryPinMismatch('Recovered stage readback lacks its exact intent')
    source = action.get('published') or checked
    try:
        verification = record.get('verification', {})
        # Merge gates separately verify the receipt for their detached integration
        # candidate. The admitted receipt's immutable files remain pinned here.
        receipt_matches = (verification.get('worktree') == str(Path(integration_worktree).resolve())
                           if integration_worktree is not None else
                           not action.get('checks_started') or action['verification'] == verification)
        intact = (
            permits(f, card, record, acknowledge=acknowledge) and str(Path(worktree).resolve()) == checked['worktree'] and
            base == checked['base'] and
            git(worktree, 'rev-parse', 'HEAD') == (candidate_head or source['head']) and
            git(worktree, 'branch', '--show-current') == checked['branch'] and
            (allow_dirty or not git(worktree, 'status', '--porcelain', '--untracked-files=all')) and
            receipt_matches)
    except (Stopped, TimedOut):
        raise
    except (Halt, KeyError, OSError) as exc:
        raise RecoveryPinMismatch('Recovered task, source or evidence changed before dispatch') from exc
    if not intact:
        raise RecoveryPinMismatch('Recovered task, source or evidence changed before dispatch')
    pr = None
    if check_live:
        try:
            if f.fetch() != base:
                raise RecoveryPinMismatch('Recovered base changed')
            if source['pr']:
                pr = f.assert_remote(card, worktree, expected_head=source['head'])
            elif f.gh.pull_requests(checked['branch']):
                raise RecoveryPinMismatch('Unexpected PR on recovered work')
        except RecoveryReadDeferred:
            raise
        except ReadConnectionReset as exc:
            raise RecoveryReadDeferred(str(exc)) from exc
        except (Stopped, TimedOut, RecoveryPinMismatch):
            raise
        except Halt as exc:
            raise RecoveryPinMismatch('Recovered task, source or routing could not be verified') from exc
        require_live(f, card, checked['scope'], dependency_block=dependency_block)
    require_retained_evidence(f, action)
    return pr


def require_live(f, card, pinned_scope=None, *, dependency_block=False):
    # Initial verification uses the saved Blocked scope. Later boundaries
    # use the acknowledged recovery, including after a long independent lane.
    if pinned_scope is None:
        action = admitted(f.record(card))
        if not action:
            return
        pinned_scope = action['checked_binding']['scope']
    if not f.watcher:
        raise RecoveryPinMismatch('Recovered work requires matching watcher authority')
    prerequisite = None
    try:
        if pickup.scope(f.gh) != pinned_scope:
            raise RecoveryPinMismatch('Recovered routing changed')
        try:
            pickup.prerequisites(f.gh, card)
        except pickup.OpenPrerequisites as exc:
            prerequisite = exc
        try:
            live = pickup.card(f.gh, card['item_id'], pinned_scope)
        except ProjectItemMissing as exc:
            if pickup.scope(f.gh) != pinned_scope:
                raise RecoveryPinMismatch('Recovered routing changed while reading the ticket') from exc
            raise RecoveryItemMissing(RECOVERY_ITEM_MISSING) from exc
        if pickup.scope(f.gh) != pinned_scope:
            raise RecoveryPinMismatch('Recovered routing changed while reading the ticket')
    except ReadConnectionReset as exc:
        raise RecoveryReadDeferred(str(exc)) from exc
    if f.watcher.excludes(live):
        raise RecoveryHeld('Recovered ticket has a live hold or exclusion')
    if card['number'] not in f.issue_scope or live != card:
        raise RecoveryPinMismatch('Recovered task or routing changed')
    if prerequisite and not dependency_block:
        raise RecoveryPrerequisite('Supervisor observed an unresolved prerequisite') from prerequisite
    if dependency_block and not prerequisite:
        raise RecoveryReadDeferred('The prerequisite closed during Blocked stage preflight')
    f.ensure_running()


def ready_for_action(f, card, scope, drift_reason):
    try:
        require_live(f, card, scope)
    except (RecoveryHeld, RecoveryPrerequisite):
        return False
    except RecoveryItemMissing:
        f.update(card, recovery_wait=RECOVERY_ITEM_MISSING)
        return False
    except RecoveryPinMismatch:
        f.update(card, recovery_wait=drift_reason)
        return False
    except ReadConnectionReset:
        defer_read(f.watcher, card)
        return False
    return True


def run(f, card):
    """Reserve before checks or writes; an uncertain board response is read back.

    The common supervisor lock excludes other processes; the per-ticket lock also
    serializes duplicate calls inside this process. Restart never refunds work.
    """
    with f.mutex:
        lock = f.review_locks.setdefault((card['number'], 'blocked-recovery'), threading.Lock())
    with lock:
        return _run(f, card)


def _run(f, card):
    r = f.record(card)
    actions = r.get('blocked_recoveries', [])
    pending_move = bool(actions and actions[-1]['phase'] == 'move-intent')
    if not pending_move:
        ok, _ = eligibility(f, card)
        if not ok:
            return False
    elif not same_window_scope(f, actions[-1], f.watcher.session):
        raise Halt('Recovery admission is outside saved watcher scope')
    f.ensure_running()
    if not pending_move:
        b = effective_blocker(f, card, r, f.watcher.session['policy'].get('blocked_recovery_kinds', []))
        if not b:
            return False
        if b != r.get('blocker'):
            f.update(card, blocker=b)
    b = r['blocker']
    actions = r.setdefault('blocked_recoveries', [])
    action = actions[-1] if actions and actions[-1]['phase'] in ('reserved', 'move-intent', 'admitted') else None
    original = b['binding']
    if action:
        original = action.get('start_binding', action['blocker']['binding'])
    elif actions and actions[-1]['blocker'] == b:
        original = actions[-1].get('failed_binding', original)
    expected = action.get('checked_binding') if action else None
    # The hold may have been added after the move went through. Read the
    # acknowledgement without making that hold a new task or spending work.
    held = pending_move and f.watcher.excludes(card)
    pinned_card = without_hold(f, card) if held else card
    drift_reason = 'Recovery source or retained evidence drift; explicit recovery required'
    try:
        pins = binding(f, pinned_card)
        same = ((expected is None or pins == expected) and
                _matches_binding(f, pins, original,
                                 replacing_verification=bool(action and action.get('checks_started')),
                                 retaining_prior=original != b['binding']))
        if original != b['binding']:
            retained = b['binding']['evidence']
            same = same and _evidence(f, retained) == retained
    except (Stopped, TimedOut):
        raise
    except (Halt, KeyError, OSError) as exc:
        if pending_move:
            raise Halt('Recovery admission source/evidence drift; inspect retained intent') from exc
        f.update(card, recovery_wait=drift_reason)
        return False
    if not same or f.fetch() != pins['base']:
        f.update(card, recovery_wait=drift_reason)
        return False
    wt = Path(pins['worktree'])
    try:
        if r.get('pr'):
            f.assert_remote(card, wt)
        elif f.gh.pull_requests(r['branch']):
            raise Halt('Unexpected recovery PR; reconcile before any write')
    except ReadConnectionReset:
        if pending_move:
            raise
        defer_read(f.watcher, card)
        return False
    except (Stopped, TimedOut):
        raise
    except Halt as exc:
        if pending_move:
            raise Halt('Recovery admission PR drift; inspect retained intent') from exc
        f.update(card, recovery_wait=drift_reason)
        return False
    if not action:
        if not ready_for_action(f, card, pins['scope'], drift_reason):
            return False
        action = {'id': uuid.uuid4().hex, 'phase': 'reserved', 'session': f.watcher.session['id'],
                  'run_id': f.run_dir.name,
                  'blocker': copy.deepcopy(b), 'reserved_at': now(), 'started_clock': f.watcher.clock(),
                  'checks': [], 'resumed_stage': 'QA' if b['kind'] == 'missing-check-evidence' else b['stage']}
        if original != b['binding']:
            action['start_binding'] = original
        if b['kind'] == FAILED_CHECK:
            prior = b.get('failed_verification') or r.get('verification')
            try:
                action['failed_check_id'] = failed_check_id(f, card, prior)
            except (Stopped, TimedOut):
                raise
            except (Halt, OSError, ValueError, KeyError, TypeError):
                f.update(card, recovery_wait='Failed project check evidence could not be verified')
                return False
            action['prior_verification'] = copy.deepcopy(prior)
        elif b['kind'] == 'missing-check-evidence' and not b.get('legacy_verification'):
            # A failed attempt replaces the active receipt. Keep the original
            # passing checks available for positional reuse on later attempts.
            last = actions[-1] if actions and actions[-1]['blocker'] == b else {}
            prior = last.get('prior_verification') or r.get('verification')
            if prior and prior.get('passed') and digest(prior) == b['binding']['verification']:
                action['prior_verification'] = copy.deepcopy(prior)
        actions.append(action)
        f.save()  # Before any recovery check, lane dispatch or remote write.
    if action['phase'] == 'reserved':
        # Lost acknowledgement of executing checks is not permission to repeat
        # them. Only an already complete, exact receipt can finish this action.
        if action.get('checks_started'):
            receipt = r.get('verification', {})
            if digest(receipt) == original['verification'] or receipt.get('time', '') < action['checks_started']:
                reason = 'Interrupted checks lack complete evidence; allowance spent'
                if receipt == {}:
                    retain_interrupted_checks(f, action, original, reason)
                else:
                    fail(f, action, reason)
                return False
            capture_checks(f, action, receipt)
            if receipt.get('passed') is not True:
                record_failed_checks(f, card, action, original)
                return False
        elif b['kind'] in ('missing-check-evidence', FAILED_CHECK):
            requested = ([action['failed_check_id']] if b['kind'] == FAILED_CHECK else
                         check_request(f, json.loads(r.get('feedback') or '{}')))
            if not ready_for_action(f, card, pins['scope'], drift_reason):
                return False
            if b['kind'] == FAILED_CHECK:
                try:
                    if f.fetch() != pins['base']:
                        raise RecoveryPinMismatch('Failed-check retry base changed before execution')
                    f.assert_remote(card, wt)
                except ReadConnectionReset:
                    defer_read(f.watcher, card)
                    return False
                except (Stopped, TimedOut):
                    raise
                except Halt:
                    f.update(card, recovery_wait=drift_reason)
                    return False
            try:
                current = binding(f, card)
            except (Stopped, TimedOut):
                raise
            except (Halt, KeyError, OSError):
                current = None
            if current != pins:
                f.update(card, recovery_wait=drift_reason)
                return False
            # The live read may take time. Preserve the pinned candidate and
            # original evidence until checks have actually been authorized.
            action['checks_started'] = now()
            f.save()
            try:
                prior = b.get('legacy_verification') or action.get('prior_verification')
                # Keep execution activity local while the ticket is Blocked.
                # Publish the result only after rechecking live authority.
                passed = f.verify(card, wt, pins['base'], force_check_ids=requested,
                                  prior_receipt=prior, report_activity=False)
            except Stopped:
                if r.get('verification') == {}:
                    retain_interrupted_checks(f, action, original, 'Recovery project check stopped; allowance spent')
                raise
            except TimedOut:
                reason = 'Recovery project check timed out; allowance spent'
                if r.get('verification') == {}:
                    retain_interrupted_checks(f, action, original, reason)
                else:
                    fail(f, action, reason)
                report_failed_check(f, card, action, timeout=True)
                return False
            capture_checks(f, action, r.get('verification', {}))
            if not passed:
                record_failed_checks(f, card, action, original)
                return False
        action['reused_evidence'] = {k: copy.deepcopy(r.get(k)) for k in ('verification', 'receipts')}
        # Check commands must not change source or produce unignored files.
        if (git(wt, 'status', '--porcelain', '--untracked-files=all') or
                git(wt, 'rev-parse', 'HEAD') != pins['head'] or f.fetch() != pins['base']):
            f.update(card, recovery_wait=drift_reason)
            return False
        try:
            checked = binding(f, card)
            same = _matches_binding(f, checked, original, replacing_verification=bool(action.get('checks_started')))
        except (Stopped, TimedOut):
            raise
        except (Halt, KeyError, OSError):
            same = False
        if not same:
            f.update(card, recovery_wait=drift_reason)
            return False
        if action.get('checks_started'):
            try:
                f.verification_evidence(card, pins['head'], pins['base'])
            except MissingCheckEvidence:
                f.update(card, recovery_wait=drift_reason)
                return False
        try:
            if r.get('pr'):
                f.assert_remote(card, wt)
            elif f.gh.pull_requests(r['branch']):
                raise RecoveryPinMismatch('Unexpected recovery PR after project checks')
        except ReadConnectionReset:
            defer_read(f.watcher, card)
            return False
        except (Stopped, TimedOut):
            raise
        except Halt:
            f.update(card, recovery_wait=drift_reason)
            return False
        if not ready_for_action(f, card, pins['scope'], drift_reason):
            return False  # A later poll can use the already completed checks.
        action['checked_binding'] = checked
        action['verification'] = copy.deepcopy(r.get('verification'))
        # The last live reads can take time. Recheck retained and replacement
        # receipts after those reads and before recording any mutation intent.
        try:
            validate_admission_evidence(f, card, action)
        except (Stopped, TimedOut):
            raise
        except (Halt, KeyError, OSError):
            f.update(card, recovery_wait=drift_reason)
            return False
        if not dispatch_stage(f, card, action, action['resumed_stage'], admission=True):
            return False
        return announce_admission(f, card, action)
    # Restart after a lost response must observe the exact intended stage. Never
    # resend a move whose outcome is unknown, even when readback says Blocked.
    live = pickup.card(f.gh, card['item_id'], pickup.scope(f.gh))
    held = f.watcher.excludes(live) or live['number'] not in f.issue_scope
    pinned_live = without_hold(f, live) if held else live
    if (pinned_live['status'] != action['resumed_stage'] or
            any(pinned_live.get(k) != pinned_card.get(k) for k in pinned_card if k != 'status')):
        raise Halt('Automatic recovery admission unresolved; readback differs, no write retry')
    checked = binding(f, pinned_live)
    if (checked != action['checked_binding'] or
            not _matches_binding(f, checked, original, replacing_verification=bool(action.get('checks_started')))):
        raise Halt('Recovery admission source/evidence drift; inspect retained intent')
    card['status'] = live['status']
    r['status'] = live['status']
    action.update(phase='admitted', acknowledged_at=now(),
                  admission_report={'phase': 'pending'},
                  blocked_wait_seconds=max(0, (dt.datetime.fromisoformat(now()) -
                                               dt.datetime.fromisoformat(b['since'])).total_seconds()),
                  elapsed_seconds=max(0, f.watcher.clock() - action['started_clock']))
    f.save()
    return not held and announce_admission(f, card, action)


def announce_admission(f, card, action):
    report = action.setdefault('admission_report', {'phase': 'pending'})
    if report['phase'] == 'verified':
        return True
    if report['phase'] == 'intent':
        acknowledge_admission_report(f, card, action)
        return True
    scope = action['checked_binding']['scope']
    drift_reason = 'Recovered task, routing or retained evidence changed after admission'

    def allowed():
        if not ready_for_action(f, card, scope, drift_reason):
            return False
        try:
            require_retained_evidence(f, action)
        except RecoveryPinMismatch:
            f.update(card, recovery_wait=drift_reason)
            return False
        return True

    def prewrite():
        if not allowed():
            raise RecoveryReportingDeferred('Recovery activity no longer has live write authority')

    if not report.get('activity_verified'):
        try:
            f.activity.report(card, 'Waiting', 'recovered', guard=f.ensure_running, prewrite=prewrite)
        except RecoveryReportingDeferred:
            return False
        with f.mutex:
            report['activity_verified'] = True
            f.save()
    if not allowed():
        return False
    b = action['blocker']
    marker = f"<!-- sfc:recovery:{action['id']} -->"
    message = ('SFC automatically verified the saved blocker (' + b['kind'] +
               ') and resumed ' + action['resumed_stage'] + '. Saved work, PR, source pins and attempts were retained. ' +
               str(sum(not c.get('reuse') for c in action['checks'])) + ' checks executed; ' +
               str(sum(bool(c.get('reuse')) for c in action['checks'])) + ' checks reused.\n\n' + marker)
    path = f.prepare_note(card, message)
    f.ensure_running()
    with f.mutex:
        report.update(phase='intent', body=message + '\n', marker=marker, requested_at=now())
        f.save()
    try:
        f.ensure_running()
    except Halt:
        with f.mutex:
            report.update(phase='pending', stopped_before_send=now())
            f.save()
        raise
    try:
        f.gh.comment(card, path)
    except Exception:
        acknowledge_admission_report(f, card, action)
    else:
        with f.mutex:
            report.update(phase='verified', acknowledged_at=now())
            f.save()
    return True


def acknowledge_admission_report(f, card, action):
    report = action['admission_report']
    identity = action['checked_binding']['identity']
    if (not f.watcher or not same_window_scope(f, action, f.watcher.session) or
            any(card.get(key) != value for key, value in identity.items())):
        raise RecoveryStageUncertain('Recovery summary is outside its saved issue or watcher scope')

    def page(after):
        f.ensure_running()
        issue = f.gh.graphql('''query($issue:ID!, $after:String) {
          node(id:$issue) { ... on Issue { id number repository { nameWithOwner }
            comments(first:100,after:$after) { nodes { id body } pageInfo { hasNextPage endCursor } }
          } }
        }''', {'issue': identity['id'], 'after': after})['node']
        if any(issue.get(key) != identity[key] for key in ('id', 'number', 'repository')):
            raise Halt('Recovery summary issue identity changed')
        return issue['comments']

    try:
        matching = [note for note in connection(page) if report['marker'] in note['body']]
        f.ensure_running()
    except (Halt, OSError, KeyError, TypeError, AttributeError, ValueError) as exc:
        raise RecoveryStageUncertain('Recovery summary needs exact comment readback before further writes') from exc
    if len(matching) != 1 or matching[0]['body'] != report['body']:
        raise RecoveryStageUncertain('Recovery summary could not be verified. No write retry')
    with f.mutex:
        report.update(phase='verified', comment_id=matching[0]['id'], acknowledged_at=now())
        f.save()


def without_hold(f, card):
    label = f.watcher.settings['hold_label'].casefold()
    return {**card, 'labels': [value for value in card['labels'] if value.casefold() != label]}


def retain_interrupted_checks(f, action, original, reason):
    # Verification clears its active receipt before executing a check. Pin
    # that empty receipt but keep all prior byte and absence pins for retry.
    # No reads here: a cooperative stop can already forbid command execution.
    action['failed_binding'] = {**original, 'verification': digest({})}
    fail(f, action, reason)


def record_failed_checks(f, card, action, original):
    try:
        pins = binding(f, card)
        if _matches_binding(f, pins, original, replacing_verification=True):
            action['failed_binding'] = {**pins, 'evidence': {**original['evidence'], **pins['evidence']}}
    except (Stopped, TimedOut):
        raise
    except (Halt, KeyError, OSError):
        # Keep the failed execution and its spent reservation. A dirty source
        # or unavailable pin only prevents a later automatic retry.
        pass
    fail(f, action, 'Recovery project checks failed; allowance spent')
    if 'failed_binding' not in action:
        f.update(card, recovery_wait='Recovery check failed and its retry pins could not be verified')
    report_failed_check(f, card, action)


def capture_checks(f, action, receipt):
    action.update(checks=copy.deepcopy(receipt.get('checks', [])),
                  verification=copy.deepcopy(receipt),
                  elapsed_seconds=max(0, f.watcher.clock() - action['started_clock']))
    f.save()


def report_failed_check(f, card, action, *, timeout=False):
    failed = next((i for i, check in enumerate(action['checks'], 1) if check['exit_code'] != 0), None)
    if failed is None and not timeout:
        return
    scope = card['_pickup_scope']
    drift_reason = 'Recovery failed and the saved ticket or routing changed before reporting'

    def prewrite():
        if not ready_for_action(f, card, scope, drift_reason):
            raise RecoveryReportingDeferred('Recovery failure no longer has live reporting authority')

    try:
        prewrite()
        f.activity.report(card, 'Failed', 'timeout' if timeout else 'check_failed',
                          guard=f.ensure_running, prewrite=prewrite)
    except RecoveryReportingDeferred:
        return  # The failure evidence is already retained locally.
    if failed is not None and ready_for_action(f, card, scope, drift_reason):
        f.note(card, f'SFC automatic check-evidence recovery failed on configured check {failed}. '
                     'The ticket remains Blocked. The execution log is saved locally.')


def fail(f, action, reason):
    action.update(phase='failed', finished=now(), reason=reason,
                  elapsed_seconds=max(0, f.watcher.clock() - action['started_clock']))
    f.save()


def finish(f, card, *, save=True):
    r = f.record(card)
    actions = r.get('blocked_recoveries', [])
    if f.watcher and actions and actions[-1]['phase'] == 'admitted':
        actions[-1].update(phase='finished', finished=now(), final_stage=card['status'],
                           elapsed_seconds=max(0, f.watcher.clock() - actions[-1]['started_clock']))
        if save:
            f.save()


def move_stage(f, card, stage, *, blocker_reason=None, updates=None, clear=(), dependency_block=False,
               retry_blocker=None):
    action = admitted(f.record(card))
    checked = action['checked_binding']
    require_pins(f, card, checked['worktree'], checked['base'], check_live=True,
                 allow_dirty=stage == 'Blocked' and card['status'] == 'Building',
                 dependency_block=dependency_block)
    return dispatch_stage(f, card, action, stage, blocker_reason=blocker_reason, updates=updates, clear=clear,
                          retry_blocker=('dependency', card['status']) if dependency_block else retry_blocker)


def dispatch_stage(f, card, action, stage, *, admission=False, blocker_reason=None, updates=None, clear=(),
                   retry_blocker=None):
    previous = action.get('stage_transition')
    if previous is not None:
        if previous['phase'] not in ('verified', 'not-dispatched'):
            raise RecoveryStageUncertain('Recovered stage transition already recorded; reconcile before further writes')
        action.setdefault('previous_stage_transitions', []).append(previous)
    transition = {'phase': 'intent', 'before': copy.deepcopy(card), 'target': stage, 'admission': admission,
                  'blocker_reason': blocker_reason, 'updates': copy.deepcopy(updates or {}),
                  'clear': list(clear), 'retry_blocker': list(retry_blocker) if retry_blocker else None}
    action['stage_transition'] = transition
    if admission:
        action['phase'] = 'move-intent'
    def undispatched():
        if admission:
            action['phase'] = 'reserved'

    return send_stage(f, card, transition, lambda: acknowledge_stage(f, card, action, transition),
                      undispatched=undispatched)


def send_stage(f, card, transition, acknowledge, *, undispatched=None):
    """Persist the intent before the sole write; uncertain sends only read back."""
    f.save()
    try:
        f.ensure_running()
    except Halt:
        transition.update(phase='not-dispatched', stopped_at=now())
        if undispatched:
            undispatched()
        f.save()
        raise
    try:
        if card['status'] != transition['target']:
            f.gh.move(dict(card), transition['target'])
        return acknowledge()
    except Exception as exc:
        raise RecoveryStageUncertain(f'Stage transition requires readback; no write retry. {exc}') from exc


def read_stage(f, transition):
    before, stage = transition['before'], transition['target']
    scope = pickup.scope(f.gh)
    live = pickup.card(f.gh, before['item_id'], scope)
    observed = without_hold(f, live) if f.watcher else live
    if (scope != before['_pickup_scope'] or observed != {**before, 'status': stage} or
            pickup.scope(f.gh) != scope):
        raise RecoveryStageUncertain('Recovered stage transition could not be verified; no write retry')
    return live


def initial_stage(f, card, target, *, blocker_reason, updates, clear, dependency_block, retry_blocker):
    record = f.record(card)
    field = 'initial_block_transition' if target == 'Blocked' else 'qa_repair_transition'
    previous = record.get(field, {})
    if previous.get('phase') == 'intent':
        raise RecoveryStageUncertain('Initial stage transition requires readback before further writes')
    blocker = {'kind': 'human-or-unsupported', 'since': now()}
    if retry_blocker:
        kind, stage = retry_blocker
        try:
            pins = binding(f, card)
        except (Stopped, TimedOut):
            raise
        except (Halt, KeyError, OSError):
            pins = None
        if pins is not None:
            blocker.update(kind=kind, stage=stage, binding=pins)
    if blocker_reason is not None:
        blocker['reason'] = blocker_reason
    # The final reads must follow local evidence collection. A hold, edit or
    # manual stage change observed here precludes a stale write.
    scope = card['_pickup_scope']
    if f.watcher:
        require_live(f, card, scope, dependency_block=dependency_block)
    elif (pickup.scope(f.gh) != scope or pickup.card(f.gh, card['item_id'], scope) != card or
          pickup.scope(f.gh) != scope):
        raise RecoveryPinMismatch('Ticket changed before the Blocked transition')
    transition = {'phase': 'intent', 'before': copy.deepcopy(card), 'target': target,
                  'config': f.config_digest, 'claim': digest(record.get('claim')),
                  'saved_status': record.get('status'), 'blocker': blocker, 'updates': copy.deepcopy(updates), 'clear': list(clear)}
    record[field] = transition
    return send_stage(f, card, transition, lambda: acknowledge_initial_stage(f, card, transition))


def acknowledge_initial_stage(f, card, transition):
    record = f.record(card)
    before = transition['before']
    if (f.config_digest != transition['config'] or record.get('status') != transition['saved_status'] or
            digest(record.get('claim')) != transition['claim']):
        raise RecoveryStageUncertain('Initial stage transition changed configuration, claim or stage')
    live = read_stage(f, transition)
    blocker = copy.deepcopy(transition['blocker'])
    if 'binding' in blocker:
        if binding(f, before) != blocker['binding']:
            raise RecoveryStageUncertain('Initial stage transition evidence changed; inspect the retained intent')
        blocker['binding']['attempts'] = transition['updates'].get('attempts', blocker['binding']['attempts'])
    with f.mutex:
        card['status'] = record['status'] = transition['target']
        for key in transition['clear']:
            record.pop(key, None)
        record.update(transition['updates'])
        if transition['target'] == 'Blocked':
            record['blocker'] = blocker
        transition.update(phase='verified', acknowledged_at=now())
        f.save()
    return not f.watcher or not f.watcher.excludes(live)


def reconcile_initial_stages(f, cards):
    by_number = {card['number']: card for card in cards}
    refreshed = False
    for number, record in list(f.state['cards'].items()):
        number = int(number)
        transition = next((record.get(key, {}) for key in ('initial_block_transition', 'qa_repair_transition')
                           if record.get(key, {}).get('phase') == 'intent'), {})
        if (number in f.occupied or transition.get('phase') != 'intent' or
                (f.include_backlog and not f.watcher and number not in f.issue_scope)):
            continue
        if number not in by_number:
            raise RecoveryStageUncertain('Initial stage transition missing its Project item; no write retry')
        acknowledge_initial_stage(f, by_number[number], transition)
        refreshed = True
    return refreshed


def validate_admission_evidence(f, card, action):
    checked = action['checked_binding']
    original = action.get('start_binding', action['blocker']['binding'])
    retained = action['blocker']['binding']['evidence']
    if (binding(f, card) != checked or
            not _matches_binding(f, checked, original, replacing_verification=bool(action.get('checks_started'))) or
            _evidence(f, retained) != retained):
        raise RecoveryStageUncertain('Recovery admission source/evidence drift; inspect retained intent')


def acknowledge_stage(f, card, action, transition):
    before, stage = transition['before'], transition['target']
    checked = action['checked_binding']
    if not same_window_scope(f, action, f.watcher.session):
        raise RecoveryStageUncertain('Recovered stage transition is outside the saved watcher scope')
    live = read_stage(f, transition)
    admitted_by_move = transition.get('admission')
    if admitted_by_move:
        validate_admission_evidence(f, before, action)
    else:
        require_pins(f, before, checked['worktree'], checked['base'], acknowledge=True,
                     allow_dirty=stage == 'Blocked' and before['status'] == 'Building')
    blocker = None
    if stage == 'Blocked':
        blocker = {'kind': 'human-or-unsupported', 'since': now()}
        if transition.get('blocker_reason') is not None:
            blocker['reason'] = transition['blocker_reason']
        if transition.get('retry_blocker'):
            kind, resume_at = transition['retry_blocker']
            try:
                pins = binding(f, before)
            except (Stopped, TimedOut):
                raise
            except (Halt, KeyError, OSError):
                pins = None  # Blocked is verified. Unsafe automatic retry still stays unavailable.
            if pins is not None:
                pins['attempts'] = transition.get('updates', {}).get('attempts', pins['attempts'])
                blocker.update(kind=kind, stage=resume_at, binding=pins)
    with f.mutex:
        record = f.record(card)
        card['status'] = record['status'] = stage
        for key in transition.get('clear', ()):
            record.pop(key, None)
        record.update(transition.get('updates', {}))
        if blocker:
            record['blocker'] = blocker
        transition.update(phase='verified', acknowledged_at=now())
        if transition.get('updates', {}).get('handoff'):
            transition.update(handoff_activity='pending', prior_activity=digest(record.get('activity', {})))
        if admitted_by_move:
            since = dt.datetime.fromisoformat(action['blocker']['since'])
            action.update(phase='admitted', acknowledged_at=now(),
                          admission_report={'phase': 'pending'},
                          blocked_wait_seconds=max(0, (dt.datetime.fromisoformat(now()) - since).total_seconds()),
                          elapsed_seconds=max(0, f.watcher.clock() - action['started_clock']))
        elif (stage in ('Blocked', 'Done') or (before['status'] == 'Building' and stage == 'QA') or
              (stage == 'Building' and transition.get('updates', {}).get('qa_repair'))):
            finish(f, card, save=False)
        f.save()
    return not f.watcher.excludes(live) and live['number'] in f.issue_scope


class RecoveryReportingDeferred(Stopped):
    """A recovery activity preflight stopped before the write started."""


def announce_handoff(f, card, action, *, comment=False):
    from .evidence_summary import verification_check_counts
    from .reasons import render_many
    from .review_policy import activity_progress

    transition = action['stage_transition']
    if transition.get('handoff_activity') != 'pending':
        return True
    before = transition['before']
    scope = action['checked_binding']['scope']
    record = f.record(card)

    def prewrite():
        if (card['number'] not in f.issue_scope or not same_window_scope(f, action, f.watcher.session) or
                record.get('handoff') != transition['updates']['handoff']):
            raise RecoveryReportingDeferred('Verified handoff reporting is outside its saved scope')
        try:
            current = pickup.card(f.gh, before['item_id'], pickup.scope(f.gh))
        except (ReadConnectionReset, ProjectItemMissing) as exc:
            raise RecoveryReportingDeferred('Verified handoff cannot be observed now') from exc
        if f.watcher.excludes(current) or current != {**before, '_pickup_scope': scope, 'status': 'Blocked'}:
            raise RecoveryReportingDeferred('Verified handoff is held or its task or routing changed')

    try:
        prewrite()
        old = record.get('activity', {})
        if (old.get('state') == 'Waiting' and old.get('action') == 'human' and not old.get('pending') and
                not old.get('error') and digest(old) != transition['prior_activity']):
            if f.gh.activity_snapshot(card) != (old['scope'], old['values']):
                raise Halt('Verified handoff activity changed; retain local stage readback')
        else:
            f.activity.report(card, 'Waiting', 'human', guard=f.ensure_running, prewrite=prewrite,
                              reasons=record['handoff_reasons'], progress=activity_progress(f, card),
                              check_counts=verification_check_counts(record['verification']))
    except RecoveryReportingDeferred:
        return False
    transition['handoff_activity'] = 'verified'
    f.save()
    if comment:
        try:
            prewrite()
        except RecoveryReportingDeferred:
            return False
        f.note(card, 'SFC: ' + render_many(record['handoff_reasons']))
    return True


def write_pr_update(f, card, pr, field, value, write, *, summary=None):
    updates = admitted(f.record(card)).setdefault('pr_updates', [])
    if updates and updates[-1]['phase'] == 'intent':
        raise RecoveryStageUncertain('Recovered PR update already awaits readback')
    intent = {'phase': 'intent', 'pr': pr['number'], 'id': pr['id'], 'head': pr['headRefOid'],
              'field': field, 'expected': digest(value), 'requested_at': now(), 'summary': summary}
    updates.append(intent)
    f.save()
    try:
        f.ensure_running()
    except Halt:
        intent['phase'] = 'not-dispatched'
        f.save()
        raise
    try:
        write()
    except Exception as exc:
        intent['response_error'] = str(exc)
        f.save()
    try:
        observed = f.assert_remote(card, f.record(card)['worktree'])
    except Halt as exc:
        raise RecoveryStageUncertain('Recovered PR update needs readback before any further write') from exc
    reconcile_pr_update(f, card, observed)


def reconcile_pr_update(f, card, pr):
    record = f.record(card)
    updates = admitted(record).get('pr_updates', [])
    if not updates or updates[-1]['phase'] != 'intent':
        return
    intent = updates[-1]
    if (not pr or (pr.get('number'), pr.get('id'), pr.get('headRefOid')) !=
            (intent['pr'], intent['id'], intent['head']) or digest(pr.get(intent['field'])) != intent['expected']):
        raise RecoveryStageUncertain('Recovered PR update differs from its exact saved intent. No write retry')
    with f.mutex:
        if intent['summary']:
            record['pr_summary'] = intent['summary']
        intent.update(phase='verified', acknowledged_at=now())
        f.save()


def accepted_build_binding(f, card, wt):
    from .verification import checksum, fingerprint_budget
    from .common import run

    record = f.record(card)
    dispatch = record['worker_output_budget']['build']['dispatches'][-1]
    receipt = record['receipts']['build']
    if (dispatch.get('status') != 'accepted' or receipt['result']['verdict'] != 'pass' or
            any(receipt[k] != v for k, v in (
                ('head', record.get('head', record['base'])), ('base', record['base']),
                ('config_digest', f.config_digest), ('task_digest', f.task_digest(card)),
                ('result_path', dispatch['result_path']), ('log', dispatch['log']))) or
            read_json(receipt['result_path']) != receipt['result']):
        raise RecoveryPinMismatch('Recovered Build no longer matches its passing receipt')
    guard = fingerprint_budget(f)
    extras = run(['git', 'ls-files', '--others', '--exclude-standard', '-z'], cwd=wt).stdout
    evidence = _evidence(f, (receipt['log'], receipt['result_path'],
                             str(Path(receipt['result_path']).parent / 'receipt.json')))
    if not all(evidence.values()) or read_json(Path(receipt['result_path']).parent / 'receipt.json') != receipt:
        raise RecoveryPinMismatch('Recovered Build receipt files are unavailable or changed')
    untracked = {}
    root = Path(wt).resolve()
    for name in filter(None, extras.split(chr(0))):
        guard()
        path = root / name
        if not path.parent.resolve().is_relative_to(root):
            raise RecoveryPinMismatch('Recovered Build has an untracked path outside the worktree')
        if path.is_symlink():
            untracked[name] = {'symlink': os.readlink(path)}
        elif path.is_file():
            untracked[name] = {'mode': path.stat().st_mode, 'sha256': checksum(path, guard)}
        else:
            raise RecoveryPinMismatch('Recovered Build has an unsupported untracked entry')
    changes = run(['git', 'diff', '--no-ext-diff', '--no-textconv', '--binary', 'HEAD'], cwd=wt).stdout
    index = run(['git', 'ls-files', '--stage', '-z'], cwd=wt).stdout
    return {'dispatch': digest(dispatch), 'receipt': digest(receipt), 'evidence': evidence,
            'changes': digest(changes), 'index': digest(index), 'untracked': untracked}


def retained_build(f, card, wt, action):
    try:
        still_pinned = accepted_build_binding(f, card, wt) == action['accepted_build']
    except (Stopped, TimedOut):
        raise
    except (Halt, KeyError, OSError, ValueError) as exc:
        raise RecoveryPinMismatch('Recovered Build evidence or saved edits changed') from exc
    if not still_pinned:
        raise RecoveryPinMismatch('Recovered Build evidence or saved edits changed')
    return f.record(card)['receipts']['build']['result']


def publish_build(f, card, wt, *, head=None):
    """Publish the accepted recovered Build, or read back its existing writes."""
    record = f.record(card)
    action = admitted(record)
    base, branch = record['base'], record['branch']
    publication = action.get('publication')
    if publication is None:
        require_pins(f, card, wt, base, candidate_head=head)
        publication = {'head': head, 'prior_pr': record.get('pr'), 'phase': 'ready-to-push'}
        action['publication'] = publication
        f.save()
    if publication['phase'] not in ('ready-to-push', 'push-intent', 'pushed', 'draft-intent'):
        raise RecoveryStageUncertain('Unexpected recovered publication phase. Inspect its retained intent')
    head = publication['head']
    require_pins(f, card, wt, base, candidate_head=head)

    def dispatch(before, intent, write):
        publication.update(phase=intent, requested_at=now())
        f.save()
        try:
            f.ensure_running()
        except Halt:
            publication['phase'] = before
            f.save()
            raise
        try:
            return write()
        except Exception as exc:
            # The mutation may have landed. The caller can only read back.
            publication['response_error'] = str(exc)
            f.save()

    if publication['phase'] == 'ready-to-push':
        require_pins(f, card, wt, base, check_live=True, candidate_head=head)
        dispatch('ready-to-push', 'push-intent',
                 lambda: git(wt, 'push', '--set-upstream', 'origin', branch))
    ref = f'refs/heads/{branch}'
    try:
        pushed = git(wt, 'ls-remote', '--refs', 'origin', ref) == f'{head}\t{ref}'
    except Halt as exc:
        raise RecoveryStageUncertain('Recovered push needs exact ref readback') from exc
    if not pushed:
        raise RecoveryStageUncertain('Recovered push is unresolved. Retain its intent without retry')
    if publication['phase'] == 'push-intent':
        publication.update(phase='pushed', push_verified_at=now())
        f.save()

    prior_pr = publication['prior_pr']
    if not prior_pr and publication['phase'] == 'pushed':
        body = f.prepare_draft(card)
        require_pins(f, card, wt, base, check_live=True, candidate_head=head)
        created = dispatch('pushed', 'draft-intent', lambda: f.gh.create_pr(card, branch, body))
        if isinstance(created, dict) and type(created.get('number')) is int:
            publication['created_pr'] = created['number']
            f.save()

    try:
        number = prior_pr
        if not number:
            prs = f.gh.pull_requests(branch)
            if len(prs) != 1 or type(prs[0].get('number')) is not int:
                raise Halt('Recovered draft identity is unresolved')
            number = prs[0]['number']
            if publication.get('created_pr', number) != number:
                raise Halt('Recovered draft differs from its acknowledged PR number')
        pr = f.gh.pr(number)
        if (not pr.get('id') or pr['number'] != number or pr['state'] != 'OPEN' or
                pr['headRefOid'] != head or pr['headRefName'] != branch or
                pr['baseRefName'] != f.c['base_branch'] or pr['isCrossRepository'] or
                (not prior_pr and (pr['isDraft'] is not True or pr['body'] != f.draft_body(card)))):
            raise Halt('Recovered PR state, source or draft body is unresolved')
    except (Halt, KeyError, TypeError) as exc:
        raise RecoveryStageUncertain('Recovered PR publication needs exact readback. No mutation retry') from exc
    require_pins(f, card, wt, base, candidate_head=head)

    # Acknowledge the publication independently of the next live stage guard.
    # A new hold can pause QA with the published candidate securely saved.
    with f.mutex:
        publication.update(phase='verified', verified_at=now())
        action['published'] = {'head': head, 'pr': pr['number']}
        record.update(head=head, pr=pr['number'])
        f.save()
    f.event(card, f"Recovered PR #{pr['number']} publication verified")


def reconcile(f, cards):
    refreshed = False
    by_number = {card['number']: card for card in cards}
    for number, record in list(f.state['cards'].items()):
        number = int(number)
        if number in f.occupied:
            continue
        actions = record.get('blocked_recoveries', [])
        action = actions[-1] if actions else {}
        if action.get('admission_report', {}).get('phase') == 'intent':
            card = by_number.get(number)
            if card is None:
                raise RecoveryStageUncertain('Recovery summary card missing; inspect retained comment intent')
            acknowledge_admission_report(f, card, action)
        transition = action.get('stage_transition')
        legacy_review = action.get('review_transition')
        if not transition and legacy_review and legacy_review['phase'] == 'intent':
            transition = {**legacy_review, 'target': 'Review'}
            action['stage_transition'] = transition
            f.save()
        if transition and transition['phase'] == 'intent':
            card = by_number.get(number)
            if card is None:
                raise RecoveryStageUncertain('Recovery admission card missing; inspect retained stage intent')
            acknowledge_stage(f, card, action, transition)
            refreshed = True
        elif action.get('phase') == 'move-intent':
            card = by_number.get(number)
            if card is None:
                raise Halt('Recovery admission card missing; inspect retained intent without retry')
            if not _run(f, card) and action['phase'] != 'admitted':
                raise Halt('Recovery admission pins changed; inspect retained intent without retry')
            refreshed = True
        if transition and transition.get('handoff_activity') == 'pending' and number in by_number:
            announce_handoff(f, by_number[number], action)
    return refreshed


def restore_candidate_checks(f, card, action):
    # A paused merge gate retains its detached verification on disk. A later
    # admission must still start from the exact receipt for the owned worktree.
    accepted = (action or {}).get('verification')
    current = f.record(card).get('verification', {}).get('worktree')
    if accepted and current and current != accepted['worktree']:
        f.update(card, verification=accepted)


def verified_checks(f, card, head, base):
    r = f.record(card)
    if not permits(f, card, r):
        return False
    action = r['blocked_recoveries'][-1]
    if not action.get('verification') or action['verification'] != r.get('verification'):
        return False
    try:
        f.verification_evidence(card, head, base)
    except MissingCheckEvidence:
        if action.get('checks_started'):
            raise
        return False
    return True
