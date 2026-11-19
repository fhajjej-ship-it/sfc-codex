"""Keep explicitly scoped criterion evidence through a same-ticket correction."""
from __future__ import annotations

import json
from pathlib import Path
import re
import uuid

from .common import Halt, Stopped, TimedOut, git, now, read_json
from .verification import (checksum, correction_delta, digest, fingerprint_budget,
                           immutable, load, original)

PINS = ('head', 'base', 'task_digest', 'config_digest', 'policy_sha256')
LANES = ('qa', 'security', 'review')
ERRORS = (OSError, ValueError, KeyError, TypeError, Halt)


def settings(c):
    return (c.get('review_policy') or {}).get('correction_reuse')


def validate(c, policy):
    from .review_policy import review_path
    if 'correction_reuse' not in policy:
        return
    contract = policy['correction_reuse']

    def paths(values):
        return (isinstance(values, list) and 0 < len(values) <= 64 and
                all(review_path(path) for path in values) and len(set(values)) == len(values))

    if (not isinstance(contract, dict) or set(contract) != {'correction_paths', 'criteria'} or
            not paths(contract['correction_paths']) or not isinstance(contract['criteria'], dict) or
            not 0 < len(contract['criteria']) <= 32):
        raise Halt('Correction reuse requires exact correction paths and explicit criterion contracts')
    for criterion, rule in contract['criteria'].items():
        if (not isinstance(criterion, str) or not re.fullmatch('[0-9a-f]{16}', criterion) or
                not isinstance(rule, dict) or set(rule) != {'source_inputs', 'check_ids'} or
                not paths(rule['source_inputs']) or not isinstance(rule['check_ids'], list) or
                not rule['check_ids'] or any(type(i) is not int or not 1 <= i <= len(c['verify_commands'])
                                            for i in rule['check_ids']) or
                len(set(rule['check_ids'])) != len(rule['check_ids'])):
            raise Halt('Each correction criterion needs exact source inputs and configured check IDs')


def archive(f, card, label, value):
    folder = f.run_dir / f"issue-{card['number']}" / f'{label}-{uuid.uuid4().hex}'
    folder.mkdir(parents=True)
    return immutable(folder, {'version': 1, 'kind': label, **value}, fingerprint_budget(f))


def origin(record):
    ref = record.get('correction_origin')
    if ref is None:
        return None
    try:
        value = load(ref)
        if value['kind'] != 'correction-origin':
            raise ValueError('unexpected correction archive')
        return value
    except (Stopped, TimedOut):
        raise
    except ERRORS as exc:
        raise Halt('Archived correction findings are unavailable; preserve the original review evidence') from exc


def pending_requests(past):
    from .review_policy import requested_obligations
    if 'pending_requests' in past:
        return past['pending_requests']
    # An earlier archive may have the lane receipts but no saved import
    # history. Preserve the available requests and treat retention as a miss
    # whenever that incomplete history included a QA obligation.
    return {receipt['result_sha256']: {'lane': lane, 'result': receipt['result']}
            for lane, receipt in past.get('receipts', {}).items()
            if requested_obligations(receipt['result'])}


def capture(f, card, result, *, inspection_rejection=None):
    """Before removing current receipts, archive their references and open findings."""
    from .review_policy import requested_obligations, review_requests
    record = f.record(card)
    previous = record.get('correction_origin')
    route = record.get('review_route') or {}
    if ((not settings(f.c) and previous is None) or record.get('release_evidence') or
            'qa' in card.get('labels', []) or not all(route.get(key) for key in PINS)):
        return previous
    scoped = result.get('scope') or {}
    is_independent = (scoped.get('obligation') in LANES and scoped.get('head') == route['head'] and
                      scoped.get('base') == route['base'] and scoped.get('task') == route['task_digest'] and
                      scoped.get('config') == route['config_digest'] and scoped.get('policy') == route['policy_sha256'])
    receipts = {lane: receipt for lane, receipt in record.get('receipts', {}).items()
                if lane in LANES and receipt.get('receipt_sha256') and
                all(receipt.get(key) == route[key] for key in PINS if key != 'policy_sha256')}
    if not is_independent and not receipts and not inspection_rejection:
        return previous
    earlier = origin(record) or {}
    pending = dict(earlier.get('pending_findings', {}))
    obligations = set(earlier.get('pending_obligations', []))
    requests = dict(pending_requests(earlier))

    def add(report):
        if report.get('verdict') not in ('fail', 'blocked'):
            return
        findings = report.get('findings') or [{'message': report['summary']}]
        for finding in findings:
            identity = digest(finding)
            pending.setdefault(identity, {**finding, 'head': route['head']})
        obligations.update(requested_obligations(report))

    if is_independent or inspection_rejection:
        add(result)
    for receipt in receipts.values():
        add(receipt['result'])
    for report in route.get('import_escalations', {}).values():
        add(report['result'])
    # Do not lose a specialist or human request merely because a retry has a
    # different head. A new assessment still follows the current minimums.
    for sha, report in review_requests(record).items():
        if additions := requested_obligations(report['result']):
            requests.setdefault(sha, report)
            obligations.update(additions)
    if 'human' in route['obligations']:
        obligations.add('human')
    return archive(f, card, 'correction-origin', {
        **{key: route[key] for key in PINS},
        'identity': identity_for(card),
        'route': {key: route[key] for key in ('risk', 'providers', 'consolidated', 'obligations')},
        'receipts': receipts, 'pending_findings': pending,
        **({'inspection_rejection': {'path': inspection_rejection['receipt_path'],
                                     'sha256': inspection_rejection['receipt_sha256']}} if inspection_rejection else {}),
        'pending_obligations': sorted(obligations), 'pending_requests': requests, 'time': now()})


def identity_for(card):
    return {key: card[key] for key in ('number', 'id', 'url', 'repository', 'item_id')}


def same_paths(wt, old, new, paths):
    for revision in (old, new):
        for path in paths:
            entry = git(wt, '--literal-pathspecs', 'ls-tree', revision, '--', path)
            if not re.match(r'^100(?:644|755) blob [0-9a-f]+\t', entry):
                return False
    return True


def potential(f, card, wt, route, past, evidence, *, at=None, since=None):
    from . import review_policy
    from .verification import source_tree

    ids = [row['id'] for row in review_policy.criteria(card)]
    kept, invalid = {}, {}

    def miss(reason):
        return kept, dict.fromkeys(ids, reason)

    contract = settings(f.c)
    if not contract:
        return miss('no correction coverage contract')
    if past['identity'] != identity_for(card) or any(past[key] != route[key] for key in PINS[1:]):
        return miss('ticket, base, task, configuration or review policy changed')
    if any(past['route'][key] != route[key] for key in ('risk', 'providers', 'consolidated')):
        return miss('independent review route, risk or provider changed')
    if 'pending_requests' not in past and 'qa' in past['pending_obligations']:
        return miss('prior QA requests lack a criterion inventory')
    old, head = past['head'], route['head']
    budget = fingerprint_budget(f)
    try:
        changed = correction_delta(wt, old, head, budget)
        if not changed or not changed <= set(contract['correction_paths']):
            return miss('correction paths are empty or include undeclared inputs')
        if (set(review_policy.risk(f.c, card, changed, git(wt, 'diff', '--no-ext-diff',
                 '--no-textconv', '--no-renames', old, head))) - {'unknown'} or
                any(Path(path).name in ('AGENTS.md', 'AGENTS.override.md') for path in changed)):
            return miss('correction affects sensitive or repository instruction inputs')
        if (git(wt, 'rev-parse', 'HEAD') != head or
                source_tree(wt, budget) != git(wt, 'rev-parse', head + '^{tree}')):
            return miss('corrected checkout differs from the pinned source')
    except (Stopped, TimedOut):
        raise
    except ERRORS:
        return miss('unsupported correction delta or source bytes/modes unavailable')

    disputed = set().union(*(review_policy.coverage_gaps(report['result'])
                             for report in pending_requests(past).values()))
    for criterion in ids:
        if criterion in disputed:
            invalid[criterion] = 'prior independent review requested fresh criterion coverage'
            continue
        rule = contract['criteria'].get(criterion)
        if not rule:
            invalid[criterion] = 'no explicit criterion contract'
            continue
        if changed & set(rule['source_inputs']) or not same_paths(wt, old, head, rule['source_inputs']):
            invalid[criterion] = 'declared criterion inputs changed or are unavailable'
            continue
        for lane in ('qa', 'review'):
            receipt = past['receipts'].get(lane)
            if receipt is None or lane not in route['obligations']:
                continue
            try:
                selected = coverage_from(f, wt, route, past, receipt, criterion, rule, evidence,
                                         budget, at=at, since=since)
            except (Stopped, TimedOut):
                raise
            except ERRORS:
                continue
            if selected is not None:
                kept[criterion] = selected
                break
        if criterion not in kept:
            invalid[criterion] = 'no current original coverage with matching execution inputs'
    return kept, invalid


def coverage_from(f, wt, route, past, receipt, criterion, rule, evidence, budget, *, at, since):
    from . import review_policy
    if (load(receipt, budget)['kind'] != 'review' or
            any(receipt[key] != past[key] for key in PINS if key != 'policy_sha256') or
            receipt['provider']['provider'] != route['providers'][receipt['lane']] or
            not review_policy.receipt_fresh(f.c, receipt, at=at, since=since) or
            checksum(receipt['log'], budget) != receipt['log_sha256'] or
            checksum(receipt['result_path'], budget) != receipt['result_sha256'] or
            read_json(receipt['result_path']) != receipt['result']):
        return None
    result = receipt['result']
    review_policy.validate_result(result)
    scope = result['scope']
    if (result['verdict'] != 'pass' or result['completed'] is not True or
            scope['policy'] != past['policy_sha256'] or scope['obligation'] != receipt['lane'] or
            any(scope[a] != past[b] for a, b in (('head', 'head'), ('base', 'base'),
                                                ('task', 'task_digest'), ('config', 'config_digest')))):
        return None
    rows = [row for row in result['coverage'] if row['criterion'] == criterion and row['status'] == 'covered']
    if len(rows) != 1:
        return None
    prior = receipt['verification']
    if (load(prior, budget)['kind'] != 'verification' or not prior['passed'] or
            receipt.get('inspection_sha256') or
            scope['verification'] != digest([original(row, budget)[1]['receipt_sha256'] for row in prior['checks']]) or
            any(prior[key] != past[key] for key in PINS if key != 'policy_sha256')):
        return None
    if receipt['provider']['provider'] == 'codex-native' and not review_policy.native_proof(receipt, wt):
        return None
    executions = {}
    refs = rows[0]['evidence']
    for ref in refs:
        if ref['kind'] == 'source':
            path = ref['id']
            if path not in rule['source_inputs'] or checksum(Path(wt) / path, budget) != ref['sha256']:
                return None
            continue
        if ref['kind'] != 'check' or ref['id'] not in {str(i) for i in rule['check_ids']}:
            return None  # No focused-command or previously retained coverage chain.
        index = int(ref['id']) - 1
        old_value, old_ref = original(prior['checks'][index], budget)
        new_value, new_ref = original(evidence['checks'][index], budget)
        if (old_value['exit_code'] != 0 or new_value['exit_code'] != 0 or
                old_ref['receipt_sha256'] != new_ref['receipt_sha256'] or
                old_value['log_sha256'] != ref['sha256'] or
                checksum(old_value['log'], budget) != ref['sha256']):
            return None
        executions[ref['id']] = new_ref['receipt_sha256']
    if set(executions) != {str(i) for i in rule['check_ids']}:
        return None
    return {'receipt': receipt['receipt_path'], 'receipt_sha256': receipt['receipt_sha256'],
            'result_sha256': receipt['result_sha256'], 'lane': receipt['lane'], 'head': receipt['head'],
            'executions': executions, 'coverage': rows[0]}


def prepare(f, card, wt, base, route):
    """Pin a new candidate assessment after completed supervisor checks."""
    record = f.record(card)
    past = origin(record)
    if past is None:
        if (record.get('review_route') or {}).get('correction'):
            raise Halt('Correction assessment has no archived origin; preserve the original review evidence')
        return
    evidence = f.verification_evidence(card, route['head'], base)
    kept, invalid = potential(f, card, wt, route, past, evidence)
    value = {**{key: route[key] for key in PINS}, 'origin': record['correction_origin']['receipt_sha256'],
             'kept': kept, 'invalidated': invalid, 'pending_findings': past['pending_findings'],
             'pending_obligations': past['pending_obligations'],
             'pending_requests': pending_requests(past)}
    previous = (record.get('review_route') or {}).get('correction')
    if previous:
        try:
            saved = load(previous)
            if saved == {'version': 1, 'kind': 'correction-assessment', **value}:
                route['correction'] = previous
        except (Stopped, TimedOut):
            raise
        except ERRORS:
            pass  # Regenerate from the unchanged archived inputs.
    if 'correction' not in route:
        route['correction'] = archive(f, card, 'correction-assessment', value)
    route['obligations'] = sorted(set(route['obligations']) | set(past['pending_obligations']))


def valid_route(f, card, route):
    record = f.record(card)
    ref = route.get('correction')
    if record.get('correction_origin') is None:
        return ref is None
    try:
        past = origin(record)
        value = load(ref)
        return (value['kind'] == 'correction-assessment' and all(value[key] == route[key] for key in PINS) and
                value['origin'] == record['correction_origin']['receipt_sha256'] and
                value['pending_findings'] == past['pending_findings'] and
                value['pending_obligations'] == past['pending_obligations'] and
                value['pending_requests'] == pending_requests(past) and
                set(past['pending_obligations']) <= set(route['obligations']))
    except (Stopped, TimedOut):
        raise
    except ERRORS:
        return False


def scope_binding(f, card, head, base):
    ref = (f.record(card).get('review_route') or {}).get('correction') or {}
    if (ref.get('head') == head and ref.get('base') == base and
            ref.get('task_digest') == f.task_digest(card) and ref.get('config_digest') == f.config_digest):
        return {'correction': ref['receipt_sha256']}
    return {}


def checked(f, card, wt, expected, evidence, *, at=None, since=None):
    """Recheck the immutable plan and only the coverage consumed on this candidate."""
    record = f.record(card)
    if record.get('correction_origin') is None:
        if 'correction' in expected:
            raise ValueError('correction archive missing')
        return {}
    route = record.get('review_route') or {}
    ref = route.get('correction') or {}
    past = origin(record)
    if (not ref or ref['receipt_sha256'] != expected.get('correction') or
            (ref.get('head'), ref.get('base'), ref.get('task_digest'), ref.get('config_digest')) !=
            (expected['head'], expected['base'], expected['task'], expected['config']) or
            ref.get('policy_sha256') != expected['policy']):
        raise ValueError('missing exact corrected-candidate assessment')
    value = load(ref)
    if (value['kind'] != 'correction-assessment' or value['origin'] != record['correction_origin']['receipt_sha256'] or
            value['pending_findings'] != past['pending_findings'] or
            value['pending_obligations'] != past['pending_obligations'] or
            value['pending_requests'] != pending_requests(past) or
            not set(past['pending_obligations']) <= set(route['obligations'])):
        raise ValueError('correction archive or obligations changed')
    if value['kept']:
        kept, _ = potential(f, card, wt, route, past, evidence, at=at, since=since)
        value['kept'] = {key: entry for key, entry in value['kept'].items() if kept.get(key) == entry}
    return value


def instructions(f, card, lane):
    if lane not in LANES:
        return ''
    correction = (f.record(card).get('review_route') or {}).get('correction')
    if not correction:
        return ''
    payload = {key: correction[key] for key in ('kept', 'invalidated', 'pending_findings')}
    payload['pending_requests'] = correction.get('pending_requests', {})
    return ('\nPinned correction assessment: ' + json.dumps(payload) +
            '\nIndependently inspect the correction and its interactions against the exact full candidate diff. '
            'Preserved evidence covers only its named criterion; it does not certify the correction. '
            'QA or final review may cite a kept row using kind=retained, id=criterion ID, '
            'sha256=the original review receipt hash. Use current executed evidence for affected criteria and '
            'for targeted reviews with a review_dependency. '
            'For each pending finding, confirm the fix or continue to report it as unresolved. '
            'A passing final review must include a separate line in evidence for every pending ID: '
            'SFC_RESOLVED <finding ID> <concrete evidence of resolution>. '
            'Explain how you verified the correction and relevant interactions.\n')


def validate_reference(correction, row, ref, expected):
    selected = correction.get('kept', {}).get(row['criterion'])
    if (expected['obligation'] not in ('qa', 'review') or expected.get('review_dependency') or
            row['status'] != 'covered' or
            not selected or ref['id'] != row['criterion'] or ref['sha256'] != selected['receipt_sha256']):
        raise ValueError('retained criterion evidence is missing, stale or outside the declared scope')


def validate_closures(correction, result, expected):
    if not correction or expected['obligation'] != 'review' or result['verdict'] != 'pass':
        return
    required = set(correction.get('pending_findings', {}))
    observed = []
    for line in result['evidence'].splitlines():
        if line.startswith('SFC_RESOLVED '):
            match = re.fullmatch(r'SFC_RESOLVED ([0-9a-f]{64}) (\S.*)', line)
            if not match:
                raise ValueError('invalid correction finding resolution evidence')
            observed.append(match.group(1))
    if len(observed) != len(required) or set(observed) != required:
        raise ValueError('fresh final review did not address every unresolved correction finding')
