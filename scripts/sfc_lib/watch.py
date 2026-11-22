"""Explicit, finite foreground queue sessions. No remote mutations live here."""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import fcntl
import time
import uuid

from .common import Halt, Stopped, common_dir, now, positive, read_json, write_json
from .reasons import outcome_reason

DEFAULTS = {'poll_interval': 30, 'max_seconds': 3600, 'max_polls': 120,
            'max_claims': 10, 'max_recoveries_per_ticket': 1, 'excluded_issues': [], 'hold_label': 'sfc-hold'}
MAXIMUMS = {'poll_interval': 3600, 'max_seconds': 86400, 'max_polls': 10000, 'max_claims': 1000}
COMPLETION_SECONDS = 30


def settings(value=None):
    if value is None:
        value = {}
    if not isinstance(value, dict) or set(value) - {*DEFAULTS, 'pickup', 'recover_builds', 'build_prerequisite_probes'}:
        raise Halt('watch must contain only documented watcher settings')
    result = {**copy.deepcopy(DEFAULTS), **value}
    pickup = result.pop('pickup', True)
    if type(pickup) is not bool:
        raise Halt('watch.pickup must be boolean')
    # Preserve the shape of legacy watcher scopes and resumable receipts.
    if not pickup:
        result['pickup'] = False
    if 'recover_builds' in result and type(result['recover_builds']) is not bool:
        raise Halt('watch.recover_builds must be boolean')
    if 'build_prerequisite_probes' in result:
        probes = result['build_prerequisite_probes']
        if (not isinstance(probes, dict) or any(
                not isinstance(key, str) or not key.isascii() or not key.isdecimal() or key.startswith('0') or
                not isinstance(argv, list) or not argv or any(
                    not isinstance(arg, str) or not arg.strip() or '\0' in arg for arg in argv)
                for key, argv in probes.items())):
            raise Halt('watch.build_prerequisite_probes must map issue numbers to command argument arrays')
        if not result.get('recover_builds'):
            raise Halt('watch.build_prerequisite_probes requires watch.recover_builds')
    for key, maximum in MAXIMUMS.items():
        positive(result[key], 'watch.' + key, maximum)
    if type(result['max_recoveries_per_ticket']) is not int or not 0 <= result['max_recoveries_per_ticket'] <= 3:
        raise Halt('watch.max_recoveries_per_ticket must be an integer from 0 to 3')
    excluded = result['excluded_issues']
    if not isinstance(excluded, list) or any(type(n) is not int or n <= 0 for n in excluded):
        raise Halt('watch.excluded_issues must be positive repository issue numbers')
    result['excluded_issues'] = sorted(set(excluded))
    label = result['hold_label']
    if not isinstance(label, str) or not label.strip() or label != label.strip() or '\n' in label:
        raise Halt('watch.hold_label must be a nonempty label name')
    return result


def supervisor_locked(root):
    """Read lock occupancy without creating files or inferring process identity."""
    lock = common_dir(root) / 'sfc.lock'
    occupied = None
    if lock.exists():
        with lock.open('r') as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                occupied = False
                fcntl.flock(handle, fcntl.LOCK_UN)
            except BlockingIOError:
                occupied = True
    return occupied


def status(root, clock=time.time):
    """Recorded facts and lock occupancy, never infer process life from a PID."""
    path = common_dir(root) / 'sfc-watch.json'
    if not path.exists():
        return {'state': 'unknown', 'detail': 'No recorded watcher session'}
    session = read_json(path)
    occupied = supervisor_locked(root)
    current = clock()
    return {'state': 'recorded', 'freshness': 'unknown; repository supervisor lock occupied' if occupied else
            ('stale/inactive; no supervisor lock held' if occupied is False else 'unknown; lock unavailable'), 'recorded_at': session.get('recorded_at'),
            'session': session, 'remaining': remaining(session, current),
            'explanation': session_explanation(session, current)}


def session_explanation(session, current):
    # The recorded reason is advisory; spent counters/deadline are authoritative.
    budgets = remaining(session, current)
    for key, code in (("seconds", "watch-time-limit"), ("polls", "watch-polls-limit"),
                      ("claims", "watch-claims-limit"), ("cycles", "cycle-limit"),
                      ("no_progress", "no-progress-limit")):
        if budgets[key] == 0:
            return outcome_reason(code)
    code = session.get('reason')
    if code == 'stopped' and session.get('interrupted') is True:
        code = 'interrupted'
    return outcome_reason(code)


def remaining(session, current):
    return {'seconds': max(0, session['deadline'] - current),
            'polls': max(0, session['settings']['max_polls'] - session['polls']),
            'claims': max(0, session['settings']['max_claims'] - session['claims']),
            'cycles': max(0, session['limits']['max_cycles'] - session['cycles']),
            'no_progress': max(0, session['limits']['max_no_progress'] - session['no_progress'])}


def validate_resume(session, *, resume, root, config_digest, watch_settings, allow_merge):
    if (session['id'] != resume or session['root'] != str(root) or
            session['config_digest'] != config_digest or session['settings'] != watch_settings or
            session['allow_merge'] != allow_merge):
        raise Halt('Watcher session/root/config/scope/merge authority changed; resume cannot broaden authority')


class Watch:
    def __init__(self, factory, *, start=False, resume=None, overrides=None,
                 clock=time.time, wait=time.sleep):
        self.f, self.start, self.resume = factory, start, resume
        self.clock, self.wait = clock, wait
        self.path = common_dir(factory.root) / 'sfc-watch.json'
        base = factory.c.get('watch', {})
        if resume and self.path.exists():
            base = read_json(self.path)['settings']
        self.settings = settings({**base, **(overrides or {})})
        if resume and 'max_recoveries_per_ticket' not in base:
            self.settings.pop('max_recoveries_per_ticket', None)
        self.session = None
        self.recovery_read_retry_at = {}

    def policy(self):
        recovery = {}
        if self.settings.get('max_recoveries_per_ticket', 0):
            from .blocked_recovery import SUPPORTED
            recovery = {'blocked_recovery_kinds': sorted(SUPPORTED)}
        return {'mode': 'watch', 'repository': self.f.c['repo'], 'project': self.f.c['project'],
                'status_field': self.f.c['status_field'], 'future_tickets': True,
                'fresh_stages': ['Backlog', 'Ready'] if self.settings.get('pickup', True) else [],
                **self.settings, **recovery}

    def excludes(self, card):
        return card['number'] in self.settings['excluded_issues'] or any(
            v.casefold() == self.settings['hold_label'].casefold() for v in card['labels'])

    def begin(self, allow_merge):
        f = self.f
        if self.start == bool(self.resume):
            raise Halt('watch requires exactly one explicit --start or --resume SESSION')
        if self.start:
            if self.path.exists():
                previous = read_json(self.path)
                if previous['root'] != str(f.root):
                    raise Halt('Recorded watcher belongs to another checkout; use its root to preserve owned work')
            current = self.clock()
            self.session = {'id': uuid.uuid4().hex, 'root': str(f.root), 'started': now(),
                            'deadline': current + self.settings['max_seconds'],
                            'settings': self.settings, 'policy': self.policy(),
                            'allow_merge': allow_merge, 'config_digest': f.config_digest,
                            'limits': {k: f.c[k] for k in ('max_cycles', 'max_no_progress')},
                            'polls': 0, 'claims': 0, 'cycles': 0,
                            'no_progress': getattr(self, 'initial_no_progress', 0),
                            'last_poll': None, 'next_poll': None, 'reason': 'starting'}
        else:
            if not self.path.exists():
                raise Halt('No watcher session to resume')
            self.session = read_json(self.path)
            validate_resume(self.session, resume=self.resume, root=f.root, config_digest=f.config_digest,
                            watch_settings=self.settings, allow_merge=allow_merge)
        if getattr(self, 'lifetime', False):
            grant = self.session.setdefault('lifetime', {'authorization': self.lifetime_authorization, 'connections': []})
            if grant.get('authorization') != self.lifetime_authorization:
                raise Halt('Cannot transfer a window to different lifetime authority')
            if self.connection not in grant['connections']:
                grant['connections'].append(self.connection)
        self.save('running')

    def save(self, reason=None):
        if reason is not None:
            self.session['reason'] = reason
        self.session['recorded_at'] = now()
        self.session['remaining'] = remaining(self.session, self.clock())
        for key in ('deadline', 'last_poll', 'next_poll'):
            value = self.session.get(key)
            self.session[key + '_at'] = datetime.fromtimestamp(value, timezone.utc).isoformat() if value is not None else None
        # The common ledger is authoritative. Publish spending there first;
        # an interrupted archive copy must never restore older allowances.
        write_json(self.path, self.session)
        write_json(self.f.dir / 'watch-sessions' / (self.session['id'] + '.json'), self.session)

    def limit(self):
        s = self.session
        if self.clock() >= s['deadline']:
            return 'watch-time-limit'
        for key, spent in (('max_polls', 'polls'), ('max_claims', 'claims')):
            if s[spent] >= self.settings[key]:
                return 'watch-' + spent + '-limit'
        if s['cycles'] >= s['limits']['max_cycles']:
            return 'cycle-limit'
        if s['no_progress'] - getattr(self, 'active_reservations', 0) >= s['limits']['max_no_progress']:
            return 'no-progress-limit'
        return None

    def acknowledge(self, card, intent):
        """A returned atomic response before cutoff is the only admission path.

        Persisted with the merge response; neither restart nor --start renews it.
        The allowance ends relative to the ORIGINAL work deadline, not recovery.
        """
        current = self.clock()
        if current < self.session['deadline']:
            intent['watch_completion'] = {
                'session': self.session['id'], 'acknowledged': current,
                'work_deadline': self.session['deadline'],
                'deadline': self.session['deadline'] + COMPLETION_SECONDS,
                'policy': copy.deepcopy(self.policy()),
                'scope': copy.deepcopy(card['_pickup_scope'])}

    def completion_remaining(self, card, intent):
        f, grant = self.f, intent.get('watch_completion', {})
        if (grant.get('session') != self.session['id'] or
                intent.get('phase') not in ('responded', 'confirmed', 'unresolved', 'done') or
                grant.get('work_deadline') != self.session['deadline'] or
                grant.get('deadline') != self.session['deadline'] + COMPLETION_SECONDS or
                not isinstance(grant.get('scope'), dict) or
                not isinstance(grant.get('acknowledged'), (int, float)) or
                grant['acknowledged'] >= self.session['deadline'] or
                grant.get('policy') != self.policy() or not self.session['allow_merge'] or
                not f.allow_merge or f.c['merge_policy'] != 'auto' or
                f.c.get('merge_mode') != 'local-verification' or
                f.record(card).get('local_merge', {}).get('id') != intent['id']):
            raise Halt('No exact pre-cutoff acknowledged watcher completion authority; inspect merge journal')
        if f.processes.stop_requested():
            raise Stopped('Operator stop requested; preserving merge completion journal')
        left = grant['deadline'] - self.clock()
        if left <= 0 or grant.get('exhausted_at'):
            if not grant.get('exhausted_at'):
                grant['exhausted_at'] = now()
                f.update(card, local_merge=intent)
            raise Halt('Watcher completion allowance exhausted; inspect retained merge/Done/activity receipts and reconcile manually')
        return left

    def complete_pending(self):
        """After cutoff, no planner, claims, checks, workers or ref writes."""
        from .local_merge import LocalMerge
        f = self.f
        if f.processes.stop_requested():
            raise Stopped('Operator stop requested; preserving merge completion journal')
        if any(r.get('local_merge', {}).get('phase') == 'done' and
               (r.get('activity', {}).get('pending') or r.get('activity', {}).get('error'))
               for r in f.state['cards'].values()):
            raise Halt('Unresolved activity write on terminal merge journal; inspect retained receipts without retry')
        pending = [r['local_merge'] for r in f.state['cards'].values()
                   if r.get('local_merge') and r['local_merge'].get('phase') != 'done']
        for intent in pending:
            # Do not infer acknowledgement from reachability or an old latch.
            card = intent['card_identity']
            self.completion_remaining(card, intent)
            LocalMerge(f).reconcile(card)

    def poll(self):
        self.session['polls'] += 1
        self.session.update(last_poll=self.clock(), next_poll=None)
        self.save('polling live queue')

    def spend_claim(self):
        # Before intent and remote mutation; lost responses still spend the claim.
        if self.clock() >= self.session['deadline'] or self.session['claims'] >= self.settings['max_claims']:
            raise Stopped('Watcher claim/time budget exhausted')
        self.session['claims'] += 1
        self.save('claim intent')

    def idle(self, reason):
        self.session['next_poll'] = self.clock() + self.settings['poll_interval']
        self.save(reason)
        while True:
            current = self.clock()
            delay = min(0.1, self.session['next_poll'] - current, self.session['deadline'] - current)
            if delay <= 0:
                return
            if self.f.processes.stopped():
                raise Stopped('Stop requested during watcher wait')
            self.wait(delay)
