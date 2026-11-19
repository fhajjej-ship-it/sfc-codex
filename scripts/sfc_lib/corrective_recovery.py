"""Explicit corrective Builds for retained, independently rejected PRs.

Separate authority from output recovery: no old spending, findings or receipts
are reset. A grant is for one finding snapshot and one complete Build only.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

from . import pickup
from .common import Halt, common_dir, git, now, read_json, write_json, run
from .worker_recovery import binding, digest


def available(record):
    return [g for g in record.get('corrective_builds', []) if g.get('status') == 'available']


def permits_findings_continuation(factory, card, record):
    """Narrow planning exception; dispatch still validates/spends the grant.

    A paid Build needs the available one-use grant. After that Build passes,
    a stopped scoped run may finish its read-only lanes, never another Build.
    """
    if (factory.watcher or not factory.include_backlog or card['number'] not in factory.issue_scope or
            record.get('status') != card['status'] or
            record.get('claim', {}).get('state') != 'verified'):
        return False
    admission = record.get('corrective_admission', {})
    grants = [g for g in record.get('corrective_builds', []) if g.get('request_id') == admission.get('request_id')]
    if admission.get('phase') != 'verified' or len(grants) != 1:
        return False
    g = grants[0]
    journal(factory, g)
    if (g['binding']['config_digest'] != factory.config_digest or
            g['binding']['task_digest'] != factory.task_digest(card)):
        return False
    if card['status'] == 'Building':
        return g['status'] == 'available'
    if card['status'] not in ('QA', 'Review') or g['status'] != 'spent':
        return False
    dispatches = record.get('worker_output_budget', {}).get('build', {}).get('dispatches', [])
    if not dispatches:
        return False
    last = dispatches[-1]
    if last.get('status') != 'accepted' or last.get('corrective_request_id') != g['request_id']:
        return False
    path = Path(last['result_path']).resolve()
    if not path.is_relative_to((factory.dir / 'runs').resolve()):
        raise Halt('Corrective output path escaped retained evidence')
    result = read_json(path)
    receipt = read_json(path.parent / 'receipt.json')
    return result.get('verdict') == 'pass' and receipt.get('result') == result


def journal(factory, grant):
    if (grant.get('version') != 1 or grant.get('additional_dispatches') != 1 or
            grant.get('origin_stage') not in ('Blocked', 'Building') or
            not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,100}', grant.get('request_id', ''))):
        raise Halt('Corrective grant identity/allowance is malformed')
    original = {k: v for k, v in grant.items() if k not in ('consumed_at',)}
    original['status'] = 'available'
    path = factory.dir / 'recoveries' / (grant['request_id'] + '.json')
    if read_json(path) != {'state': 'granted', 'grant': original}:
        raise Halt('Corrective grant journal differs; inspect without retry')


def snapshot(factory, card, request_id, *, pin_legacy_startup=False):
    r = factory.record(card)
    if factory.creation_problem(card):
        raise Halt('Corrective ticket creation evidence is incomplete')
    if r.get('status') != card['status']:
        raise Halt('Corrective saved stage differs from the live card')
    if r.get('claim', {}).get('state') != 'verified' or r.get('handoff') or r.get('local_merge') or r.get('external_completion'):
        raise Halt('Corrective Build requires a verified claim without a merge/handoff journal')
    observed = r['claim'].get('observed', {})
    if any(observed.get(k) != card.get(k) for k in ('id', 'number', 'item_id')):
        raise Halt('Corrective claim identity differs')
    if card['state'] != 'OPEN' or card['status'] not in ('Blocked', 'Building'):
        raise Halt('Corrective Build requires the same open Blocked/Building card')
    pickup.prerequisites(factory.gh, card)
    wt = Path(r['worktree'])
    head = git(wt, 'rev-parse', 'HEAD')
    live_base = factory.fetch()
    if r.get('head') != head:
        raise Halt('Corrective candidate changed; reconcile first')
    # Preserve the failed review's original base and pin the current base
    # separately. Forward base movement can be integrated by ordinary QA after
    # the corrective Build; it never makes the old review valid on the new base.
    if run(['git', 'merge-base', '--is-ancestor', r['base'], live_base], wt, check=False).returncode:
        raise Halt('Corrective base history changed; reconcile first')
    pr = factory.assert_remote(card, wt)
    feedback = json.loads(r.get('feedback') or '{}')
    if feedback.get('verdict') != 'fail' or not feedback.get('findings'):
        raise Halt('Corrective Build requires retained actionable failed lane findings')
    matches = []
    for lane, budget in r.get('worker_output_budget', {}).items():
        for d in budget['dispatches']:
            if lane not in ('qa', 'security', 'review') or d.get('status') != 'accepted':
                continue
            p = Path(d['result_path'])
            if (not p.resolve().is_relative_to((factory.dir / 'runs').resolve()) or p.name != 'result.json' or
                    not Path(d['log']).resolve().is_relative_to((factory.dir / 'runs').resolve()) or
                    Path(d['log']).name != 'worker.log'):
                raise Halt('Corrective finding paths escape retained lane evidence')
            if read_json(p) == feedback:
                receipt = read_json(p.parent / 'receipt.json')
                if not isinstance(receipt.get('time'), str) or not receipt['time']:
                    raise Halt('Corrective finding receipt time is unavailable')
                matches.append((receipt['time'], lane, d, receipt))
    if not matches:
        raise Halt('Corrective finding evidence is missing or ambiguous')
    latest = max(m[0] for m in matches)
    matches = [m for m in matches if m[0] == latest]
    if len(matches) != 1:
        raise Halt('Corrective finding evidence is ambiguous')
    _, lane, dispatch, receipt = matches[0]
    if (receipt.get('result') != feedback or receipt.get('result_path') != dispatch['result_path'] or
            receipt.get('log') != dispatch['log'] or
            any(receipt.get(k) != v for k, v in (
                ('head', head), ('base', r['base']), ('config_digest', factory.config_digest),
                ('task_digest', factory.task_digest(card))))):
        raise Halt('Corrective finding receipt pins differ')
    # Existing binding verifies original Build files, including pinned absences.
    value = binding(factory, card, wt, head, r['base'], r['worker_output_budget']['build'],
                    pin_legacy_startup=pin_legacy_startup)
    for old_lane, budget in r['worker_output_budget'].items():
        if old_lane != 'build':
            value['evidence'].extend(binding(factory, card, wt, head, r['base'], budget)['evidence'])
    value['stage'] = 'Building'  # Only the journaled Blocked -> Building admission may differ.
    value.update(live_base=live_base, attempts=r['attempts'], feedback=feedback,
                 budgets=digest(r['worker_output_budget']),
                 old_output_grant=digest(r.get('worker_output_recovery')),
                 claim=digest(r['claim']), priority=card.get('priority'),
                 pr={k: pr.get(k) for k in ('number', 'state', 'headRefOid', 'headRefName',
                                          'baseRefName', 'isCrossRepository', 'isDraft')},
                 prior_grants=[g for g in r.get('corrective_builds', []) if g['request_id'] != request_id])
    for key in ('result_path', 'log'):
        p = Path(dispatch[key])
        value['evidence'].append({'path': str(p), 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()})
    p = Path(dispatch['result_path']).parent / 'receipt.json'
    value['evidence'].append({'path': str(p), 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()})
    value['finding_lane'] = lane
    return value


def grant(factory, *, issue, request_id, reason, grant_one=False, pin_legacy_startup=False):
    if type(pin_legacy_startup) is not bool:
        raise Halt('Legacy startup recovery requires an explicit boolean choice')
    if (type(issue) is not int or issue <= 0 or
            not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,100}', request_id) or not reason.strip()):
        raise Halt('Corrective recovery needs an issue, stable request ID and reason')
    if factory.state.get('outcome') not in ('halted', 'stopped', 'waiting') or not any(
            p.exists() for p in (factory.dir / 'stop', common_dir(factory.root) / 'sfc-stop')):
        raise Halt('Stop and inspect the prior run before corrective recovery')
    r = factory.state['cards'].get(str(issue), {})
    for old in r.get('corrective_builds', []):
        if old['request_id'] == request_id:
            if (old['reason'] != reason.strip() or
                    old['binding'].get('pin_legacy_startup', False) != pin_legacy_startup):
                raise Halt('Corrective request reason changed')
            journal(factory, old)
            return {'state': old['status'], 'grant': old, 'writes': False}
    if available(r):
        raise Halt('An unspent corrective grant already exists')
    cards = pickup.cards(factory.gh, [issue])
    if len(cards) != 1:
        raise Halt('Corrective issue/item is missing or ambiguous')
    card = cards[0]
    pinned = snapshot(factory, card, request_id, pin_legacy_startup=pin_legacy_startup)
    if any(g['binding']['feedback'] == pinned['feedback'] for g in r.get('corrective_builds', [])):
        raise Halt('This finding already received its corrective Build; new request IDs do not renew it')
    value = {'version': 1, 'request_id': request_id, 'reason': reason.strip(), 'status': 'available',
             'additional_dispatches': 1, 'origin_stage': card['status'], 'binding': pinned,
             'authorized_at': now()}
    if not grant_one:
        return {'state': 'preview', 'grant': value, 'writes': False}
    path = factory.dir / 'recoveries' / (request_id + '.json')
    if path.exists():
        raise Halt('Corrective journal exists; inspect before recovery')
    write_json(path, {'state': 'intent', 'grant': value})
    r.setdefault('corrective_builds', []).append(value)
    factory.save()
    if read_json(factory.state_path)['cards'][str(issue)]['corrective_builds'][-1] != value:
        raise Halt('Corrective grant state readback differs')
    write_json(path, {'state': 'granted', 'grant': value})
    return {'state': 'granted', 'grant': value, 'writes': True}


def admit(factory, cards):
    """Report whether a grant required live admission checks."""
    # Never let a generic watcher or ordinary Ready run spend corrective authority.
    if factory.watcher or not factory.include_backlog:
        return False
    checked = False
    for card in cards:
        if card['number'] not in factory.issue_scope or card['number'] in getattr(factory, 'occupied', {}):
            continue
        r = factory.state['cards'].get(str(card['number']), {})
        pending = available(r)
        if not pending:
            continue
        checked = True
        if len(pending) != 1:
            raise Halt('Ambiguous corrective grants')
        g = pending[0]
        journal(factory, g)
        previous = r.get('corrective_admission', {})
        if previous.get('request_id') == g['request_id'] and previous.get('phase') != 'verified':
            raise Halt('Unresolved corrective admission; inspect remote state without retry')
        if snapshot(factory, card, g['request_id'], pin_legacy_startup=g['binding'].get('pin_legacy_startup', False)) != g['binding']:
            raise Halt('Corrective pins/history/evidence changed; no worker launched')
        expected = 'Building' if previous.get('request_id') == g['request_id'] else g['origin_stage']
        if card['status'] != expected:
            raise Halt('Corrective admission stage changed')
        if previous.get('request_id') == g['request_id']:
            continue
        r['corrective_admission'] = {'request_id': g['request_id'], 'phase': 'intent', 'before': card['status']}
        factory.save()
        factory.ensure_running()
        if card['status'] == 'Blocked':
            factory.move(card, 'Building', 'Explicit one-use corrective Build authorized')
        live = pickup.cards(factory.gh, [card['number']])
        if len(live) != 1 or live[0] != card:
            raise Halt('Corrective admission readback differs; no write retry')
        r['corrective_admission']['phase'] = 'verified'
        factory.save()
    return checked


def consume(factory, card, wt, head, base):
    r = factory.record(card)
    pending = available(r)
    if len(pending) != 1:
        raise Halt('No unique unspent corrective Build grant')
    g = pending[0]
    if g.get('version') != 1 or g.get('additional_dispatches') != 1:
        raise Halt('Corrective grant is malformed')
    journal(factory, g)
    if (factory.watcher or not factory.include_backlog or card['number'] not in factory.issue_scope or
            r.get('corrective_admission', {}).get('request_id') != g['request_id'] or
            r['corrective_admission'].get('phase') != 'verified'):
        raise Halt('Corrective Build needs explicit scoped admission')
    live = pickup.cards(factory.gh, [card['number']])
    if len(live) != 1 or live[0] != card or snapshot(factory, live[0], g['request_id'],
            pin_legacy_startup=g['binding'].get('pin_legacy_startup', False)) != g['binding']:
        raise Halt('Corrective pins/history/evidence changed before dispatch')
    if (head, base) != (g['binding']['head'], g['binding']['base']):
        raise Halt('Corrective worker head/base changed')
    factory.ensure_running()
    g.update(status='spent', consumed_at=now())
    return g['request_id']
