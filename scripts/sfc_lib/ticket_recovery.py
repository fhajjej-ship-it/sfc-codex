"""One-Build recovery grants for retained work. Original spending stays intact."""
from pathlib import Path
import hashlib
import json
import re

from . import pickup
from .common import Halt, Stopped, TimedOut, common_dir, git, now, read_json, write_json, run
from .worker_recovery import digest

CHECK_FAILURE = {'verdict': 'fail', 'summary': 'Project verification commands failed; inspect the recorded logs'}


def model_startup_failure(f, r, head):
    """Recognize only an owned, pre-work unsupported-model rejection."""
    builds = r.get('worker_output_budget', {}).get('build', {}).get('dispatches', [])
    d = builds[-1] if builds else {}
    if (r.get('pr') or d.get('status') != 'reserved' or d.get('lane') != 'build' or
            d.get('head') != head or d.get('base') != r['base'] or
            d.get('worktree') != r['worktree']):
        raise Halt('Model-startup recovery requires its retained uncompleted Build without a PR')
    output, log = Path(d['result_path']), Path(d['log'])
    dispatch = output.parent / 'dispatch.json'
    if (output.exists() or log.parent != output.parent or dispatch.is_symlink() or
            not dispatch.is_file() or read_json(dispatch) != d):
        raise Halt('Model-startup dispatch/output evidence differs')
    events = []
    for line in log.read_text().splitlines():
        if line.startswith('{'):
            try:
                event = json.loads(line)
            except ValueError as exc:
                raise Halt('Model-startup log contains invalid event evidence') from exc
            if not isinstance(event, dict) or event.get('type') not in (
                    'thread.started', 'turn.started', 'item.completed', 'error', 'turn.failed'):
                raise Halt('Model-startup recovery cannot retry a worker that began work')
            events.append(event)
    failed = [e for e in events if e['type'] == 'turn.failed']
    message = failed[0].get('error', {}).get('message') if len(failed) == 1 else None
    match = re.fullmatch(r"The '([^'\n]+)' model is not supported when using Codex with a ChatGPT account\.",
                         message or '')
    errors = [e.get('message') for e in events if e['type'] == 'error']
    if not match or not errors or any(e != message for e in errors) or events[-1] != failed[0]:
        raise Halt('No verified unsupported-model startup rejection; inspect without retry')
    started = False
    diagnostics = set()
    metadata = f"Model metadata for `{match[1]}` not found. Defaulting to fallback metadata; this can degrade performance and cause issues."
    tier = (r'Configured service tier `[a-z_-]+` is not advertised as supported for model `'
            + re.escape(match[1]) + r'` and will be omitted from requests\.')
    for event in events:
        if event['type'] == 'turn.started':
            started = True
        elif event['type'] == 'item.completed':
            item = event.get('item', {})
            text = item.get('message', '')
            if (started or item.get('type') != 'error' or not isinstance(text, str) or
                    (text != metadata and not re.fullmatch(tier, text)) or text in diagnostics):
                raise Halt('Model-startup log contains work or unrecognized diagnostics')
            diagnostics.add(text)
    replacement = f.c.get('models', {}).get('build') or f.c.get('model')
    if not isinstance(replacement, str) or not replacement.strip() or replacement == match[1]:
        raise Halt('Select an explicit replacement worker model before model-startup recovery')
    return {'path': str(dispatch), 'sha256': hashlib.sha256(dispatch.read_bytes()).hexdigest()}


def failed_checks(f, card, head):
    """Find the actual pinned failed supervisor check, not a worker assertion."""
    r = f.record(card)
    matches = []
    for path in (f.dir / 'runs').glob(f'*/issue-{card["number"]}/verify-*/receipt.json'):
        if path.is_symlink() or not path.resolve().is_relative_to((f.dir / 'runs').resolve()):
            raise Halt('Failed check receipt escapes retained evidence')
        value = read_json(path)
        if value.get('passed') is not False or any(value.get(k) != expected for k, expected in (
                ('head', head), ('base', r['base']), ('worktree', r['worktree']),
                ('config_digest', f.config_digest), ('task_digest', f.task_digest(card)))):
            continue
        checks = value.get('checks')
        if (not isinstance(value.get('time'), str) or not isinstance(checks, list) or
                not 0 < len(checks) <= len(f.c['verify_commands'])):
            raise Halt('Failed check receipt is malformed')
        evidence = [{'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}]
        for index, check in enumerate(checks):
            log = path.parent / f'check-{index + 1}.log'
            if (check.get('command') != f.c['verify_commands'][index] or
                    type(check.get('exit_code')) is not int or
                    (index < len(checks) - 1 and check['exit_code'] != 0) or
                    (index == len(checks) - 1 and check['exit_code'] == 0) or
                    check.get('log') != str(log) or log.is_symlink() or not log.is_file()):
                raise Halt('Failed check command/log/exit evidence differs')
            evidence.append({'path': str(log), 'sha256': hashlib.sha256(log.read_bytes()).hexdigest()})
        matches.append((value['time'], str(path), evidence))
    if not matches:
        raise Halt('No matching failed supervisor check receipt')
    latest = max(m[0] for m in matches)
    matches = [m for m in matches if m[0] == latest]
    if len(matches) != 1:
        raise Halt('Failed check receipt is ambiguous')
    return matches[0][1], matches[0][2]


def journal(f, grant):
    original = {k: v for k, v in grant.items() if k != 'consumed_at'}
    original['status'] = 'available'
    if (grant.get('version') != 1 or grant.get('additional_builds') != 1 or
            not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,100}', grant.get('request_id', '')) or
            read_json(f.dir / 'recoveries' / (grant['request_id'] + '.json')) !=
            {'state': 'granted', 'grant': original}):
        raise Halt('Ticket recovery journal differs; preserve evidence without retry')


def pending(record):
    return [g for g in record.get('ticket_recoveries', []) if g.get('status') == 'available']


def snapshot(f, card):
    r = f.record(card)
    if (f.creation_problem(card) or r.get('claim', {}).get('state') != 'verified' or
            any(r.get(k) for k in ('handoff', 'local_merge', 'external_completion')) or
            card['state'] != 'OPEN' or card['status'] not in ('Building', 'Blocked') or
            r.get('status') != card['status'] or r.get('activity', {}).get('pending') or
            r.get('activity', {}).get('error') or
            (r.get('ticket_failure') and r['ticket_failure'].get('phase') != 'blocked')):
        raise Halt('Ticket recovery requires an unambiguous retained open claim')
    if any(r['claim'].get('observed', {}).get(k) != card.get(k) for k in ('id', 'number', 'item_id')):
        raise Halt('Recovery claim identity differs')
    pickup.prerequisites(f.gh, card)
    wt = Path(r['worktree']).resolve()
    if (not wt.is_relative_to((f.dir / 'worktrees').resolve()) or
            common_dir(wt) != common_dir(f.root) or git(wt, 'branch', '--show-current') != r['branch']):
        raise Halt('Recovery worktree identity differs')
    head = git(wt, 'rev-parse', 'HEAD')
    if head != r.get('head', r['base']):
        raise Halt('Recovery saved head differs')
    pr = f.assert_remote(card, wt) if r.get('pr') else None
    if not pr and f.gh.pull_requests(r['branch']):
        raise Halt('Unexpected PR requires reconciliation')
    files = {}
    for name in git(wt, 'ls-files', '--others', '--exclude-standard').splitlines():
        path = wt / name
        if path.is_symlink() or not path.resolve().is_relative_to(wt) or not path.is_file():
            raise Halt('Recovery contains an unsupported untracked file')
        files[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    evidence = []
    for budget in r.get('worker_output_budget', {}).values():
        for d in budget['dispatches']:
            for key, filename in (('log', 'worker.log'), ('result_path', 'result.json')):
                path = Path(d[key])
                if (path.is_symlink() or not path.resolve().is_relative_to((f.dir / 'runs').resolve()) or
                        path.name != filename):
                    raise Halt('Recovery output path differs')
                if path.is_file():
                    evidence.append({'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
                elif key == 'result_path' and not path.exists() and d['status'] in ('reserved', 'window-interrupted', 'legacy-unaccepted'):
                    evidence.append({'path': str(path), 'missing': True})
                else:
                    raise Halt('Recovery original evidence is unavailable')
            receipt = Path(d['result_path']).parent / 'receipt.json'
            if receipt.is_symlink() or (d['status'] == 'accepted' and not receipt.is_file()):
                raise Halt('Recovery accepted-output receipt is unavailable')
            if receipt.is_file():
                evidence.append({'path': str(receipt), 'sha256': hashlib.sha256(receipt.read_bytes()).hexdigest()})
    if json.loads(r.get('feedback') or '{}') == CHECK_FAILURE:
        _, check_evidence = failed_checks(f, card, head)
        evidence.extend(check_evidence)
    if (r.get('activity', {}).get('action') == 'worker_exit' or
            any(g.get('kind') == 'model-startup' and g.get('status') == 'available'
                for g in r.get('ticket_recoveries', []))):
        evidence.append(model_startup_failure(f, r, head))
    return {'issue': card['number'], 'id': card['id'], 'item_id': card['item_id'],
            'metadata': f.gh.metadata(), 'config': f.config_digest, 'task': f.task_digest(card),
            'claim': digest(r['claim']), 'worktree': str(wt), 'branch': r['branch'],
            'head': head, 'base': r['base'], 'live_base': f.fetch(), 'pending_base': r.get('pending_base'),
            'attempts': r['attempts'], 'budgets': digest(r.get('worker_output_budget')),
            'old_grants': digest({k: r.get(k) for k in ('corrective_builds', 'worker_output_recovery')}),
            'feedback': r.get('feedback'), 'failure': r.get('ticket_failure'),
            'diff': digest(git(wt, 'diff', '--binary', 'HEAD')),
            'status': git(wt, 'status', '--porcelain', '--untracked-files=all'),
            'index': digest(git(wt, 'ls-files', '--stage')), 'untracked': files, 'evidence': evidence,
            'pr': {k: pr.get(k) for k in ('number', 'state', 'headRefOid', 'headRefName', 'baseRefName', 'isCrossRepository')} if pr else None}


def grant(f, *, issue, request_id, reason, grant_one=False):
    if (type(issue) is not int or issue <= 0 or
            not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,100}', request_id) or not reason.strip()):
        raise Halt('Recovery requires a stable request ID and explicit operator reason')
    if f.state.get('outcome') not in ('stopped', 'halted', 'waiting') or not any(
            p.exists() for p in (f.dir / 'stop', common_dir(f.root) / 'sfc-stop')):
        raise Halt('Stop and inspect the supervisor before recovery')
    r = f.state['cards'].get(str(issue), {})
    for old in r.get('ticket_recoveries', []):
        if old['request_id'] == request_id:
            if old['reason'] != reason.strip():
                raise Halt('Recovery request reason differs')
            journal(f, old)
            return {'state': old['status'], 'grant': old, 'writes': False}
    if pending(r):
        raise Halt('An unspent ticket recovery already exists')
    if (any(g.get('status') == 'available' for g in r.get('corrective_builds', [])) or
            r.get('worker_output_recovery', {}).get('build', {}).get('status') == 'available'):
        raise Halt('An existing unspent Build recovery must be reconciled first')
    cards = pickup.cards(f.gh, [issue])
    if len(cards) != 1:
        raise Halt('Recovery card is missing or ambiguous')
    card = cards[0]
    binding = snapshot(f, card)
    conflict = json.loads(r.get('feedback') or '{}') == {
        'verdict': 'fail', 'summary': 'Current base conflicts; builder must integrate it'}
    timeout = r.get('activity', {}).get('action') == 'timeout'
    checks = json.loads(r.get('feedback') or '{}') == CHECK_FAILURE
    builds = r.get('worker_output_budget', {}).get('build', {}).get('dispatches', [])
    last = builds[-1] if builds else {}
    kind = None
    if conflict:
        kind = 'base-conflict'
        if not binding['pr'] or binding['pending_base'] != binding['live_base']:
            raise Halt('Supervisor conflict base/PR differs')
        proof = run(['git', 'merge-tree', '--write-tree', binding['head'], binding['live_base']],
                    Path(binding['worktree']), check=False)
        if proof.returncode != 1 or 'CONFLICT' not in proof.stdout:
            raise Halt('The retained supervisor base conflict could not be reproduced')
    elif timeout:
        kind = 'timeout'
        if not builds or builds[-1]['status'] not in ('reserved', 'window-interrupted') or binding['pr']:
            raise Halt('Timeout recovery requires the retained interrupted Build without a PR')
    elif checks:
        kind = 'checks'
        if not binding['pr']:
            raise Halt('Failed-check recovery requires its existing open PR')
    elif r.get('activity', {}).get('action') == 'worker_exit' and not binding['pr']:
        # snapshot() verified the actual terminal CLI rejection and pinned the
        # unchanged dispatch, log and missing result, plus explicit replacement.
        kind = 'model-startup'
    elif last.get('status') == 'malformed' and not binding['pr']:
        kind = 'malformed'
    elif last.get('status') == 'accepted' and not binding['pr']:
        result = read_json(Path(last['result_path']))
        receipt = read_json(Path(last['result_path']).parent / 'receipt.json')
        if (result.get('verdict') != 'blocked' or result != json.loads(r.get('feedback') or '{}') or
                receipt.get('result') != result or any(receipt.get(k) != expected for k, expected in (
                    ('head', binding['head']), ('base', binding['base']),
                    ('config_digest', f.config_digest), ('task_digest', f.task_digest(card))))):
            raise Halt('Blocked-worker recovery requires its matching accepted blocked result')
        kind = 'worker-blocked'
    else:
        raise Halt('No supported retained execution failure for recovery')
    return authorize(f, card, binding, kind, request_id, reason, grant_one=grant_one)


def authorize(f, card, binding, kind, request_id, reason, *, grant_one, automatic=None):
    r = f.record(card)
    if pending(r):
        raise Halt('An unspent ticket recovery already exists')
    builds = r.get('worker_output_budget', {}).get('build', {}).get('dispatches', [])
    last = builds[-1] if builds else {}
    failure = digest({'binding': binding, 'kind': kind})
    failure_key = digest({'claim': binding['claim'], 'kind': kind,
                          'receipt': failed_checks(f, card, binding['head'])[0] if kind == 'checks' else last.get('result_path')})
    if any(g['failure_id'] == failure or g.get('failure_key') == failure_key for g in r.get('ticket_recoveries', [])):
        raise Halt('This failure already received its recovery Build')
    g = {'version': 1, 'request_id': request_id, 'reason': reason.strip(), 'status': 'available',
         'additional_builds': 1, 'authorized_at': now(), 'failure_id': failure,
         'failure_key': failure_key, 'kind': kind,
         'origin_stage': card['status'], 'binding': binding}
    if automatic:
        g['automatic'] = automatic
    if not grant_one:
        return {'state': 'preview', 'grant': g, 'writes': False}
    path = f.dir / 'recoveries' / (request_id + '.json')
    if path.exists():
        raise Halt('Recovery journal exists; no duplicate grant')
    write_json(path, {'state': 'intent', 'grant': g})
    r.setdefault('ticket_recoveries', []).append(g)
    f.save()
    if read_json(f.state_path)['cards'][str(card['number'])]['ticket_recoveries'][-1] != g:
        raise Halt('Recovery grant readback differs')
    write_json(path, {'state': 'granted', 'grant': g})
    return {'state': 'granted', 'grant': g, 'writes': True}


def automatic_eligibility(f, card):
    watcher = f.watcher
    if not watcher or not watcher.settings.get('recover_builds'):
        return False, None
    policy = (watcher.session or {}).get('policy') if watcher.session else watcher.policy()
    if not policy or policy.get('recover_builds') is not True:
        return False, None
    r = f.state['cards'].get(str(card['number']), {})
    action = r.get('activity', {}).get('action')
    probe = watcher.settings.get('build_prerequisite_probes', {}).get(str(card['number']))
    if not (action == 'timeout' or action == 'worker_blocked' and probe):
        return False, None
    if (card['status'] != 'Blocked' or r.get('status') != 'Blocked' or card['state'] != 'OPEN' or
            card['number'] not in f.issue_scope or watcher.excludes(card) or r.get('pr') or
            r.get('workflow_kind') == 'release' or 'qa' in card['labels'] or r.get('handoff') or
            r.get('qa_repair') or r.get('corrective_builds') or r.get('worker_output_recovery')):
        return False, None
    grants = r.get('ticket_recoveries', [])
    if any(g.get('automatic') for g in grants) or len(r.get('blocked_recoveries', [])) >= watcher.settings.get('max_recoveries_per_ticket', 0):
        return False, 'Automatic retained Build allowance exhausted'
    if grants:
        return False, 'Prior retained Build recovery requires inspection'
    if r.get('recovery_wait'):
        return False, r['recovery_wait']
    from .blocked_recovery import waiting_on_read
    if waiting_on_read(watcher, card):
        return False, 'Waiting for GitHub before rechecking retained work'
    retry = r.get('build_prerequisite_probe', {})
    if retry.get('retry_at', 0) > watcher.clock():
        return False, 'Waiting for the configured Build prerequisite to pass'
    return True, None


def verify_automatic_failure(f, card, binding):
    r = f.record(card)
    last = r.get('worker_output_budget', {}).get('build', {}).get('dispatches', [])[-1]
    result_path = Path(last['result_path'])
    raw = result_path.parent / 'dispatch.json'
    reserved = {**last, 'status': 'reserved'}
    reserved.pop('window', None)
    if (raw.is_symlink() or not raw.is_file() or read_json(raw) != reserved or
            last.get('lane') != 'build' or any(last.get(k) != value for k, value in (
                ('head', binding['head']), ('base', binding['base']), ('worktree', binding['worktree']),
                ('task_digest', binding['task']), ('config_digest', binding['config'])))):
        raise Halt('Automatic Build requires the exact retained dispatch')
    failure = r.get('ticket_failure')
    if failure and failure.get('action') == 'timeout':
        if (not failure or failure.get('phase') != 'blocked' or failure.get('action') != 'timeout' or
                failure.get('interrupted_lane') != 'build' or
                last.get('status') not in ('reserved', 'window-interrupted') or result_path.exists()):
            raise Halt('Automatic Build requires an interrupted Build without a result')
        receipt = Path(failure['receipt'])
        if (receipt.is_symlink() or not receipt.resolve().is_relative_to((f.dir / 'runs').resolve()) or
                read_json(receipt) != {k: v for k, v in failure.items() if k != 'receipt'} or
                failure.get('worker_output_budget') != r['worker_output_budget']):
            raise Halt('Automatic Build requires intact supervisor interruption evidence')
        return 'timeout'
    result = read_json(result_path)
    receipt = read_json(result_path.parent / 'receipt.json')
    if (last.get('status') != 'accepted' or result.get('verdict') != 'blocked' or
            result != json.loads(r['feedback']) or receipt.get('result') != result or
            any(receipt.get(k) != last.get(k) for k in ('head', 'base', 'task_digest', 'config_digest'))):
        raise Halt('Automatic Build requires intact accepted prerequisite evidence')
    return 'worker-blocked'


def probe_prerequisite(f, card, binding):
    watcher = f.watcher
    command = watcher.settings['build_prerequisite_probes'][str(card['number'])]
    folder = f.run_dir / f"issue-{card['number']}" / ('build-probe-' + f.record(card)['dispatch']['id'])
    log = folder / 'probe.log'
    argv = ['codex', 'sandbox', '-P', ':workspace', '-c', 'approval_policy="never"',
            '-C', binding['worktree'], '--', *command]
    try:
        code = f.execute(card, 'checks', argv, Path(binding['worktree']), log, min(30, f.c['verify_timeout']))
    except TimedOut:
        code = None
    proof = {'log': str(log), 'exit_code': code, 'time': now(), 'session': watcher.session['id'],
             'retry_at': watcher.clock() + watcher.settings['poll_interval']}
    if log.is_file():
        proof['sha256'] = hashlib.sha256(log.read_bytes()).hexdigest()
    f.update(card, build_prerequisite_probe=proof)
    return proof if code == 0 and proof.get('sha256') else None


def automatic_admit(f, card):
    from .blocked_recovery import ready_for_action
    from .rate_limit import ReadConnectionReset

    if not automatic_eligibility(f, card)[0]:
        return False
    drift = 'Automatic Build recovery could not verify retained evidence or authority'
    scope = card['_pickup_scope']
    if not ready_for_action(f, card, scope, drift):
        return False
    try:
        r = f.record(card)
        if r['claim']['observed'].get('_pickup_scope') != scope:
            raise Halt('Retained claim scope changed')
        binding = snapshot(f, card)
        if binding['live_base'] != binding['base']:
            raise Halt('Automatic Build base changed')
        kind = verify_automatic_failure(f, card, binding)
        probe = probe_prerequisite(f, card, binding) if kind == 'worker-blocked' else None
        if kind == 'worker-blocked' and not probe:
            return False
        if snapshot(f, card) != binding or verify_automatic_failure(f, card, binding) != kind:
            raise Halt('Automatic Build evidence changed after prerequisite verification')
    except Stopped:
        raise
    except ReadConnectionReset:
        from .blocked_recovery import defer_read
        defer_read(f.watcher, card)
        return False
    except (Halt, KeyError, IndexError, ValueError, OSError):
        f.update(card, recovery_wait=drift)
        return False
    if not ready_for_action(f, card, scope, drift):
        return False
    automatic = {'session': f.watcher.session['id'], 'scope': scope, 'probe': probe}
    request = 'auto-build-' + digest({'claim': binding['claim'], 'kind': kind})[:32]
    g = authorize(f, card, binding, kind, request, 'Saved watcher Build recovery policy',
                  grant_one=True, automatic=automatic)['grant']
    admit_card(f, card, g)
    return True


def automatic_authorized(f, card, grant):
    authority = grant.get('automatic')
    if not authority:
        return True
    watcher = f.watcher
    if (not watcher or not watcher.session or authority['session'] != watcher.session['id'] or
            watcher.session['policy'].get('recover_builds') is not True or watcher.excludes(card) or
            authority['scope'] != card.get('_pickup_scope')):
        return False
    proof = authority.get('probe')
    if grant['kind'] == 'worker-blocked' and not proof:
        raise Halt('Retained Build prerequisite proof is missing')
    if proof:
        log = Path(proof['log'])
        if (proof.get('exit_code') != 0 or log.is_symlink() or
                not log.resolve().is_relative_to((f.dir / 'runs').resolve()) or not log.is_file() or
                hashlib.sha256(log.read_bytes()).hexdigest() != proof.get('sha256')):
            raise Halt('Retained prerequisite probe evidence changed; no Build dispatched')
    if verify_automatic_failure(f, card, grant['binding']) != grant['kind']:
        raise Halt('Retained Build failure changed; no Build dispatched')
    return True


def permits(f, card, r):
    admission = r.get('ticket_recovery_admission', {})
    grants = [g for g in r.get('ticket_recoveries', []) if g['request_id'] == admission.get('request_id')]
    if (len(grants) != 1 or admission.get('phase') != 'verified' or
            card['number'] not in f.issue_scope or r.get('status') != card['status']):
        return False
    g = grants[0]
    journal(f, g)
    if g['binding']['config'] != f.config_digest or g['binding']['task'] != f.task_digest(card):
        return False
    if card['status'] == 'Building':
        return g['status'] == 'available' and automatic_authorized(f, card, g)
    if card['status'] not in ('QA', 'Review') or g['status'] != 'spent':
        return False
    dispatches = r.get('worker_output_budget', {}).get('build', {}).get('dispatches', [])
    if not dispatches:
        return False
    last = dispatches[-1]
    if last.get('ticket_recovery_id') != g['request_id'] or last.get('status') != 'accepted':
        return False
    result = read_json(Path(last['result_path']))
    return result.get('verdict') == 'pass' and read_json(Path(last['result_path']).parent / 'receipt.json').get('result') == result


def admit(f, cards):
    """Report whether a grant required live admission checks."""
    checked = False
    for card in cards:
        if card['number'] not in f.issue_scope or card['number'] in f.occupied:
            continue
        r = f.state['cards'].get(str(card['number']), {})
        grants = pending(r)
        if not grants:
            continue
        checked = True
        if len(grants) != 1:
            raise Halt('Ambiguous ticket recovery grants')
        admit_card(f, card, grants[0])
    return checked


def admit_card(f, card, g):
    r = f.record(card)
    journal(f, g)
    prior = r.get('ticket_recovery_admission', {})
    if prior.get('request_id') == g['request_id'] and prior.get('phase') != 'verified':
        raise Halt('Unresolved recovery admission; no write retry')
    if not automatic_authorized(f, card, g) or snapshot(f, card) != g['binding']:
        raise Halt('Recovery pins/evidence changed; no worker launched')
    if prior.get('request_id') == g['request_id']:
        return
    r['ticket_recovery_admission'] = {'request_id': g['request_id'], 'phase': 'intent'}
    f.save()
    f.ensure_running()
    if card['status'] == 'Blocked':
        detail = 'Watcher verified retained Build recovery' if g.get('automatic') else 'Explicit retained-work recovery authorized'
        f.move(card, 'Building', detail)
    live = pickup.cards(f.gh, [card['number']])
    if len(live) != 1 or live[0] != card:
        raise Halt('Recovery admission readback differs; no retry')
    r['ticket_recovery_admission']['phase'] = 'verified'
    f.save()


def consume(f, card):
    r = f.record(card)
    if not pending(r):
        return None
    if not permits(f, card, r) or snapshot(f, card) != pending(r)[0]['binding']:
        raise Halt('Recovery pins changed before Build')
    f.ensure_running()
    g = pending(r)[0]
    g.update(status='spent', consumed_at=now())
    if g.get('kind') == 'checks' and g['binding']['live_base'] != r['base']:
        r['pending_base'] = g['binding']['live_base']
    if r.get('ticket_failure'):
        r.setdefault('recovered_ticket_failures', []).append(
            {'request_id': g['request_id'], 'failure': r.pop('ticket_failure')})
    f.save()  # Spent even if base preparation/dispatch is interrupted.
    return g['request_id']
