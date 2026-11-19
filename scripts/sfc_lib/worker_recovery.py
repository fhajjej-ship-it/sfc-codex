"""Explicit one-use recovery; never edits or accepts a historical worker result."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

from . import pickup
from .common import Halt, common_dir, git, now, read_json, write_json


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def verified_journal(factory, grant):
    request = grant.get('request_id')
    if not isinstance(request, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,100}', request):
        raise Halt('Recovery grant request identity is malformed')
    path = factory.dir / 'recoveries' / (request + '.json')
    try:
        journal = read_json(path)
    except (OSError, ValueError) as exc:
        raise Halt('Recovery grant journal is unavailable; inspect before recovery') from exc
    if journal != {'state': 'granted', 'grant': grant}:
        raise Halt('Recovery grant journal is not verified; inspect intent/state before recovery')


def binding(factory, card, worktree, head, base, budget, *, pin_legacy_startup=False):
    """Pin the retained implementation and each historical raw result/log."""
    record = factory.record(card)
    worktree = Path(worktree).resolve()
    if (not worktree.is_relative_to((factory.dir / 'worktrees').resolve()) or
            not worktree.is_dir() or common_dir(worktree) != common_dir(factory.root) or
            str(worktree) != record.get('worktree') or
            git(worktree, 'branch', '--show-current') != record.get('branch') or
            base != record.get('base') or git(worktree, 'rev-parse', 'HEAD') != head):
        raise Halt('Recovery worktree/head/base/branch identity changed')
    if git(worktree, 'ls-files', '--others', '--exclude-standard'):
        raise Halt('Recovery requires inspection of untracked worktree files first')
    if (type(budget.get('limit')) is not int or budget['limit'] <= 0 or
            not isinstance(budget.get('dispatches'), list)):
        raise Halt('Recovery budget is malformed')
    evidence = []
    for dispatch in budget['dispatches']:
        if not isinstance(dispatch, dict) or not isinstance(dispatch.get('status'), str):
            raise Halt('Recovery dispatch history is malformed')
        legacy = False
        if pin_legacy_startup and dispatch['status'] == 'legacy-unaccepted':
            folder = Path(dispatch.get('folder', ''))
            resolved = folder.resolve()
            if (folder.is_symlink() or not resolved.is_dir() or
                    not resolved.is_relative_to((factory.dir / 'runs').resolve()) or
                    resolved.parent.name != f"issue-{card['number']}" or
                    not re.fullmatch(r'build-[0-9a-f]{8}', resolved.name)):
                raise Halt('Legacy startup folder identity is unavailable')
            paths = [folder / name for name in ('result.json', 'worker.log', 'dispatch.json', 'receipt.json')]
            legacy = all(not p.exists() and not p.is_symlink() for p in paths)
            if legacy:
                if any(Path(dispatch.get(k, '')).resolve() != (resolved / name)
                       for k, name in (('result_path', 'result.json'), ('log', 'worker.log'))):
                    raise Halt('Legacy startup artifact paths differ from retained folder')
                evidence.extend({'path': str(p.resolve()), 'missing': True} for p in paths[2:])
                prompt = folder / 'prompt.md'
                if prompt.is_symlink() or not prompt.is_file():
                    raise Halt('Legacy startup prompt is unavailable')
                evidence.append({'path': str(prompt.resolve()), 'sha256': hashlib.sha256(prompt.read_bytes()).hexdigest()})
        for key, filename in (('result_path', 'result.json'), ('log', 'worker.log')):
            original = Path(dispatch.get(key, ''))
            path = original.resolve()
            if (not path.is_relative_to((factory.dir / 'runs').resolve()) or
                    path.name != filename):
                raise Halt('Recovery requires retained original result/log files')
            if not path.is_file():
                # An interrupted worker may never have written its output.
                # Pin that absence, never fabricate an output or accept a late
                # one. Binding is compared again before the one-use dispatch.
                if ((key == 'result_path' or legacy) and not path.exists() and not original.is_symlink() and
                        dispatch['status'] in ('reserved', 'window-interrupted', 'legacy-unaccepted')):
                    evidence.append({'path': str(path), 'missing': True})
                    continue
                raise Halt('Recovery requires retained original result/log files')
            evidence.append({'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
    value = {'root': str(factory.root), 'repo': factory.c['repo'], 'project': factory.gh.metadata(),
            'issue': card['number'], 'issue_id': card.get('id'), 'item_id': card['item_id'],
            'stage': card['status'], 'task_digest': factory.task_digest(card),
            'config_digest': factory.config_digest, 'worktree': str(worktree),
            'branch': record['branch'], 'head': head, 'base': base,
            'worktree_diff': digest(git(worktree, 'diff', '--binary', 'HEAD')),
            'worktree_status': git(worktree, 'status', '--porcelain', '--untracked-files=all'),
            'budget_digest': digest(budget), 'evidence': evidence}
    if pin_legacy_startup:
        value['pin_legacy_startup'] = True
    return value


def recover(factory, *, issue, request_id, reason, grant_one=False):
    """Caller holds the supervisor lock. Preview does not mutate saved state."""
    if (type(issue) is not int or issue <= 0 or not isinstance(request_id, str) or
            not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,100}', request_id) or
            not isinstance(reason, str) or not reason.strip()):
        raise Halt('Recovery requires one issue, a stable request ID and a nonblank reason')
    if factory.state.get('outcome') not in ('halted', 'stopped'):
        raise Halt('Stop and inspect the halted run before recovery')
    if not any(p.exists() for p in (factory.dir / 'stop', common_dir(factory.root) / 'sfc-stop')):
        raise Halt('Explicitly stop the supervisor before recovery')
    record = factory.state.get('cards', {}).get(str(issue))
    if not record or record.get('claim', {}).get('state') != 'verified':
        raise Halt('Recovery requires the existing verified claim')
    previous = record.get('worker_output_recovery', {}).get('build')
    if previous is not None:
        if previous.get('request_id') != request_id or previous.get('reason') != reason.strip():
            raise Halt('Only one supplemental Build grant is supported per existing claim')
        if previous.get('status') != 'available':
            return {'state': 'spent', 'grant': previous, 'writes': False}
        verified_journal(factory, previous)
        return {'state': 'existing', 'grant': previous, 'writes': False}
    cards = pickup.cards(factory.gh, [issue])
    if len(cards) != 1 or cards[0]['state'] != 'OPEN' or cards[0]['status'] != 'Building':
        raise Halt('Recovery requires the same open Building issue')
    card = cards[0]
    claim = record['claim'].get('observed', {})
    if any(claim.get(k) != card.get(k) for k in ('id', 'number', 'item_id')):
        raise Halt('Recovery claim identity differs from the live issue/item')
    pickup.prerequisites(factory.gh, card)
    if record.get('pr') or factory.gh.pull_requests(record.get('branch', '')):
        raise Halt('Reconcile the existing PR before Build-output recovery')
    budget = record.get('worker_output_budget', {}).get('build')
    if not isinstance(budget, dict):
        raise Halt('Inspect and journal the retained output history before recovery')
    if (type(budget.get('limit')) is not int or not isinstance(budget.get('dispatches'), list) or
            any(not isinstance(d, dict) for d in budget['dispatches'])):
        raise Halt('Recovery budget is malformed')
    limit = min(budget['limit'], factory.c['max_attempts'])
    spent = sum(d.get('status') != 'accepted' for d in budget['dispatches'])
    if limit <= 0 or spent < limit:
        raise Halt('Existing Build output allowance is not exhausted; use normal scoped resume')
    worktree = Path(record.get('worktree', ''))
    pinned = binding(factory, card, worktree, git(worktree, 'rev-parse', 'HEAD'), record.get('base'), budget)
    grant = {'version': 1, 'request_id': request_id, 'reason': reason.strip(),
             'lane': 'build', 'additional_dispatches': 1, 'status': 'available',
             'binding': pinned, 'authorized_at': now()}
    if not grant_one:
        return {'state': 'preview', 'spent': spent, 'original_limit': budget['limit'], 'grant': grant,
                'detail': 'Explicit --grant-one permits one complete Build dispatch; starts no worker'}
    path = factory.dir / 'recoveries' / (request_id + '.json')
    if path.exists():
        raise Halt('Recovery journal already exists; reconcile it instead of repeating writes')
    write_json(path, {'state': 'intent', 'grant': grant})
    record.setdefault('worker_output_recovery', {})['build'] = grant
    factory.save()
    if read_json(factory.state_path)['cards'][str(issue)]['worker_output_recovery']['build'] != grant:
        raise Halt('Recovery grant readback differs; preserve journal and inspect')
    write_json(path, {'state': 'granted', 'grant': grant})
    verified_journal(factory, grant)
    return {'state': 'granted', 'spent': spent, 'original_limit': budget['limit'], 'grant': grant,
            'receipt': str(path), 'detail': 'No counters reset, worker started, results accepted or merge authority granted'}


def consume(factory, card, lane, worktree, head, base, budget):
    """Called under the state mutex immediately before dispatch reservation."""
    grant = factory.record(card).get('worker_output_recovery', {}).get(lane)
    if (lane != 'build' or not isinstance(grant, dict) or grant.get('version') != 1 or
            grant.get('lane') != 'build' or grant.get('additional_dispatches') != 1 or
            grant.get('status') != 'available'):
        raise Halt('Build output allowance exhausted; no unused supplemental recovery grant')
    verified_journal(factory, grant)
    live = pickup.cards(factory.gh, [card['number']])
    if (len(live) != 1 or live[0]['state'] != 'OPEN' or live[0]['status'] != 'Building' or
            any(live[0].get(k) != card.get(k) for k in
                ('id', 'number', 'item_id', 'state', 'status', 'title', 'body', 'labels'))):
        raise Halt('Live recovery issue/task/stage changed; no grant consumed')
    pickup.prerequisites(factory.gh, live[0])
    if binding(factory, card, worktree, head, base, budget) != grant.get('binding'):
        raise Halt('Recovery pins/history/evidence changed; no worker launched and no grant consumed')
    factory.ensure_running()
    grant.update(status='spent', consumed_at=now())
    return grant['request_id']
