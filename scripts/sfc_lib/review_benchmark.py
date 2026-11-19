"""Predeclared, bounded comparison gate. Offline adapters cannot promote policy."""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
from pathlib import Path
import re
import statistics

from .common import Halt, git, read_json, validate_config
from . import review_policy
from .verification import checksum, load, regular_file, validate_evidence

CRITERIA = {
    'version': 10, 'max_cases': 12, 'max_runs_per_case': 3,
    'minimum_cases': 3, 'detection_recall': 1.0, 'acceptance_coverage': 1.0,
    'max_added_false_positives': 0, 'maximum_latency_ratio': 0.95,
    'required_kinds': ['retained-real-finding', 'seeded-regression', 'clean'],
}
FIXTURE_CHECKS = ['python3 -B check_behavior.py']
EXECUTION_LIMITS = {'worker_timeout': 120, 'verify_timeout': 90}
BEHAVIORAL_PROBES = {
    'deleted-record': ('seeded-regression', ['missing-record']),
    'retained-double-lookup': ('retained-real-finding', ['double-lookup']),
}
INSPECTION_CRITERIA = {
    'version': 4, 'required_kinds': ['clean', 'failed-check', 'retained-real-finding'],
    'maximum_pairs_per_profile': 6, 'maximum_latency_ratio': 0.95, 'minimum_check_seconds': 5,
}
SOURCE_INSPECTION_MAX_DISPATCHES = 10
SOURCE_INSPECTION_MAX_WORKER_SECONDS = SOURCE_INSPECTION_MAX_DISPATCHES * EXECUTION_LIMITS['worker_timeout']
DEFECT_ID = re.compile('[a-z][a-z0-9-]{0,63}')
MAX_ADDITIONAL_FINDINGS = 8
MAX_FINDING_PROOF_BYTES = 128 * 1024


def arm_config(variant, models, target_policy, *, routine_candidate=None, target_config=None, project_plan=None):
    policy = dict(target_policy) if variant == 'candidate' else {}
    if routine_candidate is not None and policy.get('mode') == 'risk-based':
        policy['routine_candidate'] = routine_candidate
    if target_config is not None:
        if review_policy.model_settings(target_config) != models:
            raise ValueError('comparison target differs from its measured model profile')
        if project_plan is not None:
            from .project_review_benchmark import isolated_config
            target_config = isolated_config(target_config, project_plan['source_common'], project_plan['isolated_common'])
        elif target_config['verify_commands'] != FIXTURE_CHECKS or target_config.get('verification_reuse'):
            raise ValueError('comparison target must use the exact executable fixture checks and model profile')
        return validate_config({**target_config, 'review_policy': policy})
    return validate_config({'version': 1, 'repo': 'fixture/review', 'base_branch': 'main',
        'project': {'owner': 'fixture', 'number': 1}, 'status_field': 'Status',
        'verify_commands': FIXTURE_CHECKS, 'merge_policy': 'human',
        'auto_max_lines': 800, 'allow_no_checks': False, 'max_workers': 1, 'max_cycles': 1,
        'max_no_progress': 1, 'max_attempts': 1, 'worker_timeout': EXECUTION_LIMITS['worker_timeout'], 'verify_timeout': 15,
        **models, 'review_policy': policy})


def run_arm(f, card, root, base, variant, dispatch):
    if variant == 'baseline':
        # The production baseline stops at the first decisive result. The
        # structured coverage instrument does not add lanes to this baseline.
        for lane in review_policy.LANES:
            if dispatch(lane)['verdict'] != 'pass':
                return 'failed'
        return 'passed'

    route = review_policy.plan(f, card, root, base, evaluation=True)
    record = f.record(card)
    record['review_route'] = route
    receipts = record.setdefault('receipts', {})

    def passed(lane):
        result = dispatch(lane)
        review_policy.retain_local_escalation(route, receipts[lane])
        return result['verdict'] == 'pass'

    if route['review_first'] and not passed('review'):
        return 'failed'
    while lane := next((k for k in review_policy.required(f, card) if k != 'review' and k not in receipts), None):
        if not passed(lane):
            return 'failed'
    if not route['review_first'] and not passed('review'):
        return 'failed'

    refreshed = {'review'}
    while lane := next((k for k in review_policy.required(f, card) if k not in refreshed and
                        receipts.get(k, {}).get('result', {}).get('scope', {}).get('review_dependency') !=
                        review_policy.review_dependency(f, card, k)), None):
        refreshed.add(lane)
        if not passed(lane):
            return 'failed'
    if not review_policy.specialist_dependencies_complete(f, card) or not review_policy.acceptance_complete(f, card):
        return 'acceptance_gap'
    if review_policy.human_required(f, card):
        return 'human'
    return 'passed'


def arm_measurement(project_plan):
    if project_plan is not None and project_plan['version'] == 2:
        return {'execution_limits': project_plan['limits'], 'project_plan_sha256': review_policy.digest(project_plan)}
    return {'execution_limits': EXECUTION_LIMITS}


def live_context(sample, value, variant, report, project_plan=None):
    from .factory import Factory

    measurement = arm_measurement(project_plan)
    timing = value['timing']
    expected = {'version': 4, 'kind': 'review-comparison-arm', **measurement, **{key: value[key] for key in
                ('config', 'receipts', 'outcome', 'review_seconds', 'end_to_end_seconds', 'attestation')}}
    if (review_policy.digest({key: value[key] for key in measurement}) != review_policy.digest(measurement) or
            checksum(timing['path']) != timing['sha256'] or
            review_policy.digest(read_json(timing['path'])) != review_policy.digest(expected)):
        raise ValueError('comparison arm timing or evidence changed')
    root = Path(sample['worktree']).resolve()
    head, base = sample['head'], sample['base']
    if (head == base or git(root, 'rev-parse', base + '^{commit}') != base or
            not git(root, 'diff', '--no-ext-diff', '--no-textconv', '--name-only', base, head)):
        raise ValueError('real-model comparison requires an actual candidate diff')
    review_policy.native_source(root, head, base)
    card = sample['card']
    attestation = value['attestation']
    candidate = None
    if attestation is not None:
        pins = {'head': head, 'base': base, 'task': Factory.task_digest(card)}
        if (variant != 'candidate' or (sample['kind'] != 'clean' and
                                      not report['target_policy'].get('behavioral_contract')) or
                report['target_policy'].get('mode') != 'risk-based' or
                not isinstance(attestation, dict) or set(attestation) != {'pins', 'accepted', 'elapsed_seconds'} or
                attestation['pins'] != pins or type(attestation['accepted']) is not bool or
                type(attestation['elapsed_seconds']) not in (float, int) or
                not math.isfinite(attestation['elapsed_seconds']) or attestation['elapsed_seconds'] <= 0):
            raise ValueError('comparison attestation timing or scope invalid')
        if attestation['accepted']:
            candidate = pins
    factory = Factory(root, value['config'], github=object())
    factory.state = {'version': 1, 'cards': {}}
    if (value['config'] != arm_config(
            variant, report['model_settings'], report['target_policy'], routine_candidate=candidate,
            target_config=report['target_config'], project_plan=project_plan) or
            review_policy.model_settings(factory.c) != report['model_settings']):
        raise ValueError('comparison provider or model configuration differs')
    if (factory.task_digest(card) != sample['task_digest'] or
            sample['criteria'] != [row['id'] for row in review_policy.criteria(card)]):
        raise ValueError('comparison task or acceptance criteria changed')
    return factory, card, root


def validate_behavioral_oracle(sample):
    """Bind preflight outcomes without executing report-supplied code on activation."""
    proof = review_policy.load_ref('.', sample['oracle'])
    expected = {'kind': 'behavioral-comparison-oracle', 'version': 1, 'case_kind': sample['kind'],
                **{key: sample[key] for key in ('case', 'head', 'base', 'expected', 'task_digest')}}
    rows = proof['observations']
    if (proof != {**expected, 'observations': rows} or set(rows) != {'base', 'candidate'} or
            rows['base']['exit_code'] != 0 or rows['candidate']['exit_code'] != (sample['kind'] != 'clean') or
            any(set(row) != {'source_sha256', 'exit_code', 'stdout', 'stderr'} or
                not isinstance(row['source_sha256'], str) or len(row['source_sha256']) != 64 or
                type(row['exit_code']) is not int or not isinstance(row['stderr'], str) or
                not isinstance(row['stdout'], str) for row in rows.values())):
        raise ValueError('behavioral fixture preflight is missing or differs from the declared premise')


def live_receipt(f, card, root, receipt, report, threads):
    if load(receipt)['kind'] != 'review':
        raise ValueError('comparison requires an immutable review receipt')
    head, base, lane = receipt['head'], receipt['base'], receipt['lane']
    provider = receipt['provider']
    configured = f.c['review_policy'].get('providers', {}).get(lane, 'codex-exec')
    if (provider['provider'] != configured or provider.get('version') != report['cli_version'] or
            not provider.get('tool') or 'fake' in provider['version'].lower() or
            provider.get('model') != (f.c.get('models', {}).get(lane) or f.c.get('model')) or
            receipt['task_digest'] != f.task_digest(card) or receipt['config_digest'] != f.config_digest):
        raise ValueError('real-model comparison lacks matching task and tool provenance')
    thread, message = review_policy.completed_message(receipt['log'], native=configured == 'codex-native')
    if thread in threads:
        raise ValueError('comparison reused a reviewer context')
    threads.add(thread)
    if configured == 'codex-native':
        if not review_policy.native_proof(receipt, root):
            raise ValueError('native comparison result differs from raw output')
    else:
        if json.loads(message) != receipt['result']:
            raise ValueError('baseline comparison result differs from raw output')
    evidence = receipt['verification']
    if (evidence['kind'] != 'verification' or evidence['passed'] is not True or
            not evidence['checks'] or evidence['head'] != head or evidence['base'] != base or
            evidence['tree'] != git(root, 'rev-parse', 'HEAD^{tree}') or
            evidence['task_digest'] != receipt['task_digest'] or evidence['config_digest'] != f.config_digest or
            [row['command'] for row in evidence['checks']] != f.c['verify_commands']):
        raise ValueError('comparison requires executed checks for this candidate and task')
    # Qualification is historical evidence. Validate that the checks were
    # eligible when this reviewer used them, while still recomputing the live
    # toolchain and source binding. Ticket acceptance uses its own current gate.
    validate_evidence(evidence, root, f, card, base, at=receipt['time'], since=receipt['time'])
    f.record(card)['verification'] = evidence
    scope = review_policy.scope(f, card, head, base, evidence, lane)
    result = receipt['result']
    prior_qa = replay_prior_qa(f, card, root, receipt)
    review_policy.validate_report(f, card, root, result, scope, evidence,
                                  qa_log=receipt['log'] if lane == 'qa' else None, at=receipt['time'],
                                  since=receipt['time'], prior_qa=prior_qa, prior_qa_before=receipt['time'])
    if result['verdict'] == 'blocked' or (result['verdict'] == 'fail' and not result['findings']):
        raise ValueError('comparison reviewer blocked or returned an unadjudicable failure')


def replay_prior_qa(f, card, worktree, receipt):
    anchor = receipt.get('prior_qa')
    if (bool(anchor) != review_policy.uses_prior_qa(receipt['result']) or
            anchor and (receipt['lane'] != 'review' or anchor != review_policy.prior_qa_anchor(
                f, card, worktree, receipt['head'], receipt['base'], at=receipt['time'], since=receipt['time']))):
        raise ValueError('comparison final review lacks its exact preceding QA receipt')
    return anchor


def finding_adjudication_v2(report, *, live):
    version = report.get('finding_adjudication_version', 1)
    if type(version) is not int or version not in (1, 2) or version == 2 and not live:
        raise ValueError('additional finding adjudication requires version 2 real-model evidence')
    return version == 2


def finding_evidence(ref):
    review_policy.validate_ref(ref)
    path = Path(ref['path'])
    if not path.is_absolute() or path.is_symlink():
        raise ValueError('additional finding proof must refer to a retained absolute file')
    with regular_file(path) as stream:
        data = stream.read(MAX_FINDING_PROOF_BYTES + 1)
    if (not 0 < len(data) <= MAX_FINDING_PROOF_BYTES or
            hashlib.sha256(data).hexdigest() != ref['sha256']):
        raise ValueError('additional finding proof bytes changed or exceed the evidence limit')
    return data


def finding_json(ref):
    try:
        value = json.loads(finding_evidence(ref))
    except RecursionError as exc:
        raise ValueError('additional finding proof JSON exceeds the nesting limit') from exc
    if not isinstance(value, dict):
        raise ValueError('additional finding proof JSON must be an object')
    return value


def validate_additional_findings(sample, adjudicator):
    """Read independent reproduction records without executing report-supplied probes."""
    findings = sample.get('additional_findings', {})
    expected = inspection_expected(sample) if 'obligations' in sample else set(sample['expected'])
    if (not isinstance(findings, dict) or len(findings) > MAX_ADDITIONAL_FINDINGS or
            any(not isinstance(key, str) or not DEFECT_ID.fullmatch(key) or key in expected for key in findings)):
        raise ValueError('invalid additional finding inventory')
    if not findings:
        return set()
    if (not isinstance(adjudicator, str) or not adjudicator.strip() or
            sample['kind'] in ('clean', 'failed-check')):
        raise ValueError('additional findings require an independent adjudicator and a defect case')
    from .factory import Factory

    if Factory.task_digest(sample['card']) != sample['task_digest']:
        raise ValueError('additional finding task changed')
    criteria = {row['id'] for row in review_policy.criteria(sample['card'])}
    scope = {key: sample[key] for key in ('case', 'base', 'head', 'task_digest')}
    for identifier, ref in findings.items():
        proof = finding_json(ref)
        kind = proof.get('kind')
        pins = {'version': 1, **scope, 'id': identifier, 'adjudicator': adjudicator}
        keys = {*pins, 'kind', 'reason', 'probe', 'runs'}
        if kind == 'acceptance-gap':
            keys.add('criterion')
        if (set(proof) != keys or type(proof['version']) is not int or
                any(proof.get(key) != value for key, value in pins.items()) or
                kind not in ('regression', 'acceptance-gap') or
                kind == 'acceptance-gap' and proof['criterion'] not in criteria or
                not isinstance(proof['reason'], str) or not proof['reason'].strip() or
                not isinstance(proof['runs'], dict) or set(proof['runs']) != {'base', 'head'}):
            raise ValueError('additional finding proof scope, adjudicator or contract differs')
        finding_evidence(proof['probe'])
        for side, run in proof['runs'].items():
            if (set(run) != {'commit', 'exit_code', 'log'} or run['commit'] != scope[side] or
                    type(run['exit_code']) is not int or run['exit_code'] != 0):
                raise ValueError('additional finding requires successful observations of both exact revisions')
            observed = finding_json(run['log'])
            if (set(observed) != {'version', 'commit', 'probe_sha256', 'satisfied', 'observation'} or
                    type(observed['version']) is not int or observed['version'] != 1 or
                    observed['commit'] != scope[side] or observed['probe_sha256'] != proof['probe']['sha256'] or
                    type(observed['satisfied']) is not bool or
                    side == 'head' and observed['satisfied'] or
                    side == 'base' and kind == 'regression' and not observed['satisfied'] or
                    not isinstance(observed['observation'], str) or not observed['observation'].strip()):
                raise ValueError('additional finding lacks corroborating base and head contract observations')
    return set(findings)


def validate_additional_detection(verified, baseline_detected, candidate_detected):
    baseline, candidate = (detected & verified for detected in (baseline_detected, candidate_detected))
    if baseline | candidate != verified:
        raise ValueError('additional finding proof is unused')
    if not baseline <= candidate:
        raise ValueError('candidate missed a verified additional baseline finding')


def adjudicate_findings(rows, receipts, expected, *, inspection=False, additional=(), excerpts=False):
    """Map one independent judgment per raw row to all corroborated defect IDs."""
    locator, scalar = ('lane', 'finding') if inspection else ('receipt_index', 'regression')
    positions = {locator, 'index'}
    findings = {(lane.get('lane', 'inspection') if inspection else position, index): finding
                for position, lane in enumerate(receipts)
                for index, finding in enumerate(lane['result']['findings'])}
    observed, confirmed, unique = set(), {}, set()
    false_positives = unadjudicated = 0
    if not isinstance(rows, list) or len(rows) != len(findings):
        raise ValueError('each finding requires independent adjudication')
    for row in rows:
        indices = (row[locator], row['index'])
        if (type(row[locator]) is not (str if inspection else int) or type(row['index']) is not int or
                indices not in findings or indices in observed):
            raise ValueError('each finding requires exactly one independent adjudication')
        observed.add(indices)
        if not excerpts and set(row) == positions | {scalar, 'reason'}:
            identifier, reason = row[scalar], row['reason']
            if (not isinstance(reason, str) or not reason.strip() or
                    identifier is not None and (not isinstance(identifier, str) or not identifier)):
                raise ValueError('finding adjudication needs reasoning and a regression ID')
            unique.add(identifier)
            if identifier in expected:
                confirmed.setdefault(row[locator], set()).add(identifier)
            elif identifier in (None, 'false-positive'):
                false_positives += 1
            else:
                unadjudicated += 1
            continue
        extras = {'false_positive_reason', 'unadjudicated_reason'} & set(row)
        if (set(row) - positions - {'regressions', *extras} or len(extras) > 1 or
                not isinstance(row.get('regressions'), list) or
                any(not isinstance(row[key], str) or not row[key].strip() for key in extras) or
                not (row['regressions'] or extras)):
            raise ValueError('finding adjudication needs corroborated regressions or a reason for its disposition')
        mapped = set()
        for match in row['regressions']:
            if (set(match) != ({'id', 'reason', 'excerpt'} if excerpts else {'id', 'reason'}) or
                    not isinstance(match['id'], str) or match['id'] in mapped or
                    not isinstance(match['reason'], str) or not match['reason'].strip()):
                raise ValueError('each mapped regression requires a distinct ID and its own reasoning')
            if excerpts and (not isinstance(match['excerpt'], str) or not match['excerpt'].strip() or
                             match['excerpt'] not in findings[indices]['message']):
                raise ValueError('finding excerpt differs from the exact raw report row')
            if match['id'] not in expected and (not excerpts or match['id'] not in additional):
                raise ValueError('additional findings must be retained as unadjudicated')
            mapped.add(match['id'])
        confirmed.setdefault(row[locator], set()).update(mapped)
        unique.update(mapped)
        if 'false_positive_reason' in extras:
            false_positives += 1
            unique.add(('false-positive', *indices))
        elif 'unadjudicated_reason' in extras:
            unadjudicated += 1
            unique.add(('unadjudicated', *indices))
    return confirmed, unique, false_positives, unadjudicated


def evaluate(report):
    """Evaluate saved runs, never launch models or change a project default.

    Each finding gets independent adjudication against the declared defects.
    Every sample retains original lane envelopes and hashes of result/log bytes.
    Model cost/time claims are eligible only for actual model executions.
    """
    from .delivery import stream
    failures, metrics = [], {}
    try:
        if report['criteria'] != CRITERIA:
            raise ValueError('promotion criteria changed or missing')
        samples = report['samples']
        if not isinstance(samples, list) or not 3 <= len(samples) <= 36:
            raise ValueError('comparison budget exceeded or insufficient samples')
        counts, kinds, threads = {}, set(), set()
        live = report['execution'] == 'real-model'
        adjudication_v2 = finding_adjudication_v2(report, live=live)
        routine_control = False
        behavioral_control = False
        behavioral_defects = set()
        behavioral_policy = live and bool(report['target_policy'].get('behavioral_contract'))
        project_plan = None
        project_dispatches = []
        if live:
            if not review_policy.explicit_review_settings(report['model_settings']):
                raise ValueError('real-model comparison requires explicit review models, effort and tier')
            target = report['target_policy']
            target_config = report['target_config']
            if (target_config != validate_config(target_config) or target_config.get('review_policy') != target or
                    report['policy_sha256'] != review_policy.promotion_binding(target_config)):
                raise ValueError('comparison target configuration differs from the promoted policy')
            if (set(target) & {'promotion', 'imports', 'routine_candidate'} or
                    target.get('providers', {}).get('review') != 'codex-native'):
                raise ValueError('comparison must evaluate the proposed native policy without evidence imports')
            if project_ref := report.get('project_trial'):
                from .project_review_benchmark import load_trial
                project_plan = load_trial(project_ref, target_config)
                if (not behavioral_policy or [s['case'] for s in samples] != [r['case'] for r in project_plan['cases']] or
                        any(report[k] != project_plan[k] for k in ('runtime_sha256', 'cli_version'))):
                    raise ValueError('Project comparison sample inventory or behavioral policy differs')
        totals = {name: {'expected': set(), 'detected': set(), 'unique_findings': set(),
                         'verified_additional_findings': 0,
                         'false_positives': 0, 'unadjudicated_findings': 0, 'covered': 0, 'criteria': 0,
                         'review_seconds': [], 'end_to_end_seconds': [],
                         'attestation_seconds': [], 'tokens': {}, 'usage_unknown': set()}
                  for name in ('baseline', 'candidate')}
        for sample_index, sample in enumerate(samples):
            case = sample['case']
            consolidated_control = False
            behavioral = False
            counts[case] = counts.get(case, 0) + 1
            kinds.add(sample['kind'])
            if live and (not sample['criteria'] or
                         bool(sample['expected']) != (sample['kind'] != 'clean')):
                raise ValueError('comparison needs labeled regressions and a clean control')
            if project_plan is not None:
                from .project_review_benchmark import validate_sample
                validate_sample(sample, project_plan)
            elif behavioral_policy:
                validate_behavioral_oracle(sample)
                if case in BEHAVIORAL_PROBES and (sample['kind'], sample['expected']) != BEHAVIORAL_PROBES[case]:
                    raise ValueError('behavioral regression fixture was relabeled')
            expected = set(sample['expected'])
            if 'additional_findings' in sample and not adjudication_v2:
                raise ValueError('additional finding proof requires version 2 adjudication')
            additional = (validate_additional_findings(sample, report['independent_adjudicator'])
                          if adjudication_v2 else set())
            detected_by_arm = {}
            for name, total in totals.items():
                value = sample[name]
                if 'error' in value:
                    raise ValueError(f"{case} {name} failed: {value['error']}; retained {value['evidence_root']}")
                lanes = value['receipts']
                if not isinstance(lanes, list) or not 1 <= len(lanes) <= (5 if name == 'candidate' else 3):
                    raise ValueError('unbounded or missing lane evidence')
                receipts = []
                if live:
                    f, card, root = live_context(sample, value, name, report, project_plan)
                for ref in lanes:
                    if checksum(ref['path']) != ref['sha256']:
                        raise ValueError('comparison receipt changed')
                    receipt = read_json(ref['path'])
                    if (checksum(receipt['result_path']) != receipt['result_sha256'] or
                            checksum(receipt['log']) != receipt['log_sha256'] or
                            read_json(receipt['result_path']) != receipt['result'] or
                            receipt['head'] != sample['head'] or receipt['base'] != sample['base']):
                        raise ValueError('comparison raw evidence or exact source changed')
                    if live:
                        receipt = {**receipt, 'receipt_path': ref['path'], 'receipt_sha256': ref['sha256']}
                    receipts.append(receipt)
                if project_plan is not None:
                    project_dispatches.extend({'case': case, 'variant': name, 'lane': receipt['lane']} for receipt in receipts)
                if live:
                    remaining = iter(receipts)

                    def replay(lane):
                        receipt = next(remaining, None)
                        if receipt is None or receipt['lane'] != lane:
                            raise ValueError('comparison skipped or reordered required review obligations')
                        live_receipt(f, card, root, receipt, report, threads)
                        f.record(card).setdefault('receipts', {})[lane] = receipt
                        return receipt['result']

                    outcome = run_arm(f, card, root, sample['base'], name, replay)
                    if next(remaining, None) is not None or value['outcome'] != outcome:
                        raise ValueError('comparison timing extends past its production outcome or omits its handoff')
                    if outcome in ('human', 'acceptance_gap'):
                        failures.append(f'{case} {name} reached {outcome} without completed acceptance and timing')
                    if name == 'candidate':
                        route = f.record(card)['review_route']
                        behavioral = bool(route.get('behavioral_check_ids'))
                        if project_plan is not None and sample['kind'] != 'clean':
                            if behavioral and outcome == 'failed':
                                behavioral_defects.add(sample['kind'])
                        elif case in BEHAVIORAL_PROBES and behavioral_policy:
                            if behavioral and outcome == 'failed':
                                behavioral_defects.add(case)
                            else:
                                failures.append(f'{case} did not fail under the qualified behavioral route')
                        if sample['kind'] == 'clean' and outcome == 'passed':
                            consolidated_control = route['consolidated']
                    mapped = {row['criterion'] for lane, receipt in f.record(card)['receipts'].items()
                              if lane in ('review', 'qa') for row in receipt['result']['coverage']
                              if row['status'] == 'covered'}
                confirmed, unique, false_positives, unadjudicated = adjudicate_findings(
                    value['adjudications'], receipts, expected, additional=additional, excerpts=adjudication_v2)
                detected = set().union(*confirmed.values())
                detected_by_arm[name] = detected
                total['unique_findings'].update((case, identifier) for identifier in unique)
                total['verified_additional_findings'] += len(detected & additional)
                total['false_positives'] += false_positives
                total['unadjudicated_findings'] += unadjudicated
                if unadjudicated:
                    failures.append(f'{case} {name} has findings outside the declared inventory')
                # Do not hide a miss in one repeat behind success in another.
                total['expected'].update((sample_index, v) for v in expected)
                total['detected'].update((sample_index, v) for v in detected & expected)
                coverage = value['coverage']
                if (not isinstance(coverage, list) or len(coverage) != len(set(coverage)) or
                        not set(coverage) <= set(sample['criteria'])):
                    raise ValueError('invalid acceptance coverage')
                if live:
                    reviewed = value['coverage_adjudication']
                    if (set(coverage) != set(reviewed) or not set(coverage) <= mapped or
                            any(not isinstance(v, str) or not v.strip() for v in reviewed.values())):
                        raise ValueError('claimed coverage lacks matching reviewed criterion and executed check')
                total['covered'] += len(coverage)
                total['criteria'] += len(sample['criteria'])
                for key in ('review_seconds', 'end_to_end_seconds'):
                    seconds = value[key]
                    if type(seconds) not in (float, int) or not math.isfinite(seconds) or seconds <= 0:
                        raise ValueError('missing or invalid measured latency')
                    total[key].append(seconds)
                durations = [v['elapsed_seconds'] for v in receipts]
                if any(type(v) not in (float, int) or not math.isfinite(v) or v <= 0 for v in durations):
                    raise ValueError('missing or invalid reviewer duration')
                elapsed = sum(durations)
                if (not math.isclose(value['review_seconds'], elapsed, rel_tol=1e-6) or
                        value['end_to_end_seconds'] < elapsed):
                    raise ValueError('latency does not match retained process durations')
                if live:
                    verification = receipts[0]['verification']
                    if project_plan is not None:
                        from .project_review_benchmark import validate_fresh_checks
                        validate_fresh_checks(f.c, verification)
                    attestation_seconds = (value['attestation'] or {}).get('elapsed_seconds', 0)
                    total['attestation_seconds'].append(attestation_seconds)
                    for receipt in receipts:
                        _, observed, _, unknown = stream(
                            receipt['log'], native=receipt['provider']['provider'] == 'codex-native')
                        for key, count in observed.items():
                            if count is not None:
                                total['tokens'][key] = total['tokens'].get(key, 0) + count
                            if unknown[key]:
                                total['usage_unknown'].add(key)
                    if any(v['verification'] != verification for v in receipts):
                        raise ValueError('paired arm lacks a common timed verification run')
                    if value['end_to_end_seconds'] < elapsed + verification['elapsed_seconds'] + attestation_seconds:
                        raise ValueError('end-to-end timing omits verification, review or operator attestation')
            if adjudication_v2:
                validate_additional_detection(additional, detected_by_arm['baseline'], detected_by_arm['candidate'])
                if sample['kind'] == 'clean' and any(sample[name]['outcome'] != 'passed' for name in totals):
                    failures.append(f'{case} clean control did not pass in both arms')
            if consolidated_control:
                routine_control = True
                behavioral_control |= behavioral
                baseline = sample['baseline']
                limit = baseline['end_to_end_seconds'] * CRITERIA['maximum_latency_ratio']
                if baseline['outcome'] != 'passed' or set(baseline['coverage']) != set(sample['criteria']):
                    failures.append(f'{case} consolidated control lacks a completed baseline')
                elif sample['candidate']['end_to_end_seconds'] > limit:
                    failures.append(f'{case} consolidated control did not improve end-to-end time at least 5%')
        if project_plan is not None:
            from .project_review_benchmark import evidence_ref
            path = Path(evidence_ref('.', report['model_reservations'])['path'])
            reservations = [json.loads(line) for line in path.read_text().splitlines()]
            if (len(reservations) > project_plan['limits']['maximum_model_dispatches'] or
                    sorted(map(review_policy.digest, reservations)) != sorted(map(review_policy.digest, project_dispatches))):
                raise ValueError('Project comparison worker reservations differ from its retained executions')
        if (not CRITERIA['minimum_cases'] <= len(counts) <= CRITERIA['max_cases'] or
                max(counts.values()) > CRITERIA['max_runs_per_case'] or
                not set(CRITERIA['required_kinds']) <= kinds):
            raise ValueError('retained finding, seeded regression and clean controls required within budget')
        for name, total in totals.items():
            metrics[name] = {
                'detection_recall': len(total['detected']) / len(total['expected']) if total['expected'] else 0,
                'unique_findings': len(total['unique_findings']), 'false_positives': total['false_positives'],
                'unadjudicated_findings': total['unadjudicated_findings'],
                'acceptance_coverage': total['covered'] / total['criteria'] if total['criteria'] else 0,
                'median_review_seconds': statistics.median(total['review_seconds']),
                'median_end_to_end_seconds': statistics.median(total['end_to_end_seconds']),
                'attestation_seconds': sum(total['attestation_seconds']),
                'observed_tokens': total['tokens'], 'usage_unknown': sorted(total['usage_unknown']), 'credits': None,
                **({'verified_additional_findings': total['verified_additional_findings']} if adjudication_v2 else {})}
        a, b = metrics['baseline'], metrics['candidate']
        if b['detection_recall'] != 1 or b['detection_recall'] < a['detection_recall']:
            failures.append('detection parity failed')
        if b['acceptance_coverage'] != 1 or b['acceptance_coverage'] < a['acceptance_coverage']:
            failures.append('acceptance coverage failed')
        if b['false_positives'] > a['false_positives']:
            failures.append('false positives increased')
        for key in ('median_review_seconds', 'median_end_to_end_seconds'):
            if b[key] > a[key] * CRITERIA['maximum_latency_ratio']:
                failures.append(key + ' did not improve at least 5%')
        if not live:
            failures.append('offline fake-adapter results cannot establish model quality or latency')
        elif not routine_control:
            failures.append('no clean fixture completed a consolidated candidate route')
        if behavioral_policy:
            if not behavioral_control:
                failures.append('no clean fixture completed the qualified behavioral route')
            expected_defects = {'retained-real-finding', 'seeded-regression'} if project_plan is not None else set(BEHAVIORAL_PROBES)
            if behavioral_defects != expected_defects:
                failures.append('qualified behavioral route lacks both retained and seeded regression evidence')
        if (not isinstance(report.get('independent_adjudicator'), str) or
                not report['independent_adjudicator'].strip() or not report.get('policy_sha256')):
            failures.append('independent adjudication or policy binding missing')
    except (AttributeError, KeyError, ValueError, TypeError, OSError, Halt) as exc:
        failures.append('incomplete or stale comparison: ' + str(exc))
    return {'promote': not failures, 'failures': failures, 'metrics': metrics,
            'evaluation_overhead': True, 'criteria': CRITERIA}


def inspection_binding(config):
    return review_policy.digest({**config, 'review_policy': inspection_policy(config)})


def inspection_policy(config):
    policy = config['review_policy']
    return {**{k: v for k, v in policy.items() if k != 'routine_candidate'},
            'inspection': {k: v for k, v in policy['inspection'].items() if k != 'promotion'}}


def release_snapshot_identity(snapshot):
    return {**snapshot, 'artifacts': [{k: v for k, v in row.items() if k != 'path'}
                                      for row in snapshot['artifacts']]}


def historical_source_route(f, card, worktree, base, assessment, provisional, behavioral):
    """Replay the qualification route at its final verdict's retained time."""
    from .review_inspection import final_reference

    route = review_policy.plan(f, card, worktree, base, evaluation=behavioral, consider_inspection=False)
    if provisional is None or assessment['lane'] != 'review':
        return route
    time = assessment['time']
    observed = final_reference(f, card, worktree, assessment['head'], base, at=time, since=time,
                               role='behavioral-qa' if behavioral else None)
    if observed != provisional:
        raise ValueError('source inspection was not eligible at the recorded final assessment')
    if behavioral and provisional['result']['verdict'] == 'pass':
        policy = review_policy.settings(f.c)
        if (not route.get('behavioral_check_ids') or route['obligations'] != ['qa', 'review'] or
                policy.get('mandatory') or policy.get('imports') or f.record(card).get('correction_origin')):
            raise ValueError('source comparison attempted provisional QA outside the qualified behavioral route')
        route.update(obligations=['review'], provisional_qa=provisional['receipt_sha256'])
    return route


def inspection_expected(pair):
    value = pair['expected']
    if value is None or value == '':
        value = []
    elif isinstance(value, str):
        value = [value]
    if (not isinstance(value, list) or len(value) > 8 or
            any(not isinstance(item, str) or not DEFECT_ID.fullmatch(item) for item in value) or
            len(set(value)) != len(value) or bool(value) != (pair['kind'] != 'clean') or
            pair['kind'] == 'failed-check' and len(value) != 1):
        raise ValueError('invalid predeclared inspection finding inventory')
    return set(value)


def inspection_release_checks(arm, snapshot, head, at):
    release = arm['release_evidence']
    phases = [release[key] for key in ('preflight_checks', 'artifact_checks') if key in release]
    if (release['inspection_snapshot'] != snapshot or release['candidate'] != head or
            not any(phase['checks'] for phase in phases) or
            any(phase['state'] != 'passed' or phase['finished'] >= at or
                any(v['exit_code'] != 0 or checksum(v['log']) != v['log_sha256'] for v in phase['checks'])
                for phase in phases) or
            any(checksum(v['path']) != v['sha256'] for v in release['artifacts'])):
        raise ValueError('release assessment did not verify the completed package checks')
    artifact = release['artifact_checks']
    seconds = (datetime.fromisoformat(artifact['finished']) - datetime.fromisoformat(artifact['started'])).total_seconds()
    return release, seconds


def inspection_rejection_coverage(rows, obligations, provisional, checks, release):
    if set(rows) != set(obligations):
        raise ValueError('rejected case lacks independent coverage of every obligation')
    commands = {}
    _, message = review_policy.completed_message(provisional['log'], commands=commands)
    if json.loads(message) != provisional['result']:
        raise ValueError('rejected case differs from its completed inspection')
    logs = {row['log_sha256'] for row in checks['checks']}
    for key in ('preflight_checks', 'artifact_checks'):
        logs.update(row['log_sha256'] for row in (release or {}).get(key, {}).get('checks', []))
    for row in rows.values():
        if (not isinstance(row, dict) or set(row) != {'kind', 'id', 'reason'} or
                not isinstance(row['reason'], str) or not row['reason'].strip() or not isinstance(row['id'], str) or
                not (row['kind'] == 'check' and row['id'] in logs or
                     row['kind'] == 'qa-command' and review_policy.successful_qa_command(commands, row['id']))):
            raise ValueError('rejected case coverage lacks a bound check log or successful inspection probe')
    if not any(row['kind'] == 'qa-command' for row in rows.values()):
        raise ValueError('the retained defect requires a successful inspection probe')


def source_inspection_limits(report, pairs):
    if not (ref := report.get('plan')):
        return None
    review_policy.validate_ref(ref)
    plan = review_policy.load_ref('.', ref)
    if 'measurement_limits' not in plan:
        return None
    limits = plan['measurement_limits']
    config = report['target_config']
    inventory = [{key: pair[key] for key in ('case', 'kind', 'base', 'head', 'expected')} for pair in pairs]
    if (plan['profile'] != 'source' or plan['config'] != config or plan['case_inventory'] != inventory or
            plan['criteria'] != report['criteria'] or
            any(plan[key] != report[key] for key in ('binding', 'runtime_sha256')) or
            plan['cli']['version'] != report['cli_version'] or
            not re.fullmatch(r'[0-9a-f]{64}', plan['cli']['provider_sha256']) or
            not isinstance(limits, dict) or set(limits) != {'cases', 'maximum_model_dispatches', 'maximum_worker_seconds'} or
            type(limits['maximum_model_dispatches']) is not int or
            not 0 < limits['maximum_model_dispatches'] <= SOURCE_INSPECTION_MAX_DISPATCHES or
            type(limits['maximum_worker_seconds']) is not int or
            not 0 < limits['maximum_worker_seconds'] <= SOURCE_INSPECTION_MAX_WORKER_SECONDS or
            not isinstance(limits['cases'], dict) or set(limits['cases']) != {pair['case'] for pair in pairs}):
        raise ValueError('source inspection measurement plan or aggregate budget changed')
    for pair in pairs:
        caps = limits['cases'][pair['case']]
        worker_max = EXECUTION_LIMITS['worker_timeout'] if pair['kind'] == 'failed-check' else 180
        if (not isinstance(caps, dict) or caps.keys() != EXECUTION_LIMITS.keys() or
                any(type(value) is not int or value <= 0 for value in caps.values()) or
                caps['worker_timeout'] > min(worker_max, config['worker_timeout']) or
                caps['verify_timeout'] > min(EXECUTION_LIMITS['verify_timeout'], config['verify_timeout'])):
            raise ValueError('source inspection case exceeds its allowed measurement limits')
    return limits


def validate_source_inspection_reservations(report, limits, dispatches):
    from .project_review_benchmark import evidence_ref

    plan = review_policy.load_ref('.', report['plan'])
    path = Path(evidence_ref('.', report['model_reservations'])['path'])
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    fields = {'case', 'variant', 'lane', 'worker_timeout'}
    if (len(rows) > limits['maximum_model_dispatches'] or
            any(set(row) != fields | {'index', 'at', 'plan_sha256', 'provider_sha256'} or
                type(row['index']) is not int or row['index'] != index or
                type(row['at']) not in (int, float) or not math.isfinite(row['at']) or row['at'] <= 0 or
                row['plan_sha256'] != report['plan']['sha256'] or
                row['provider_sha256'] != plan['cli']['provider_sha256'] or
                type(row['worker_timeout']) is not int or
                row['worker_timeout'] != limits['cases'][row['case']]['worker_timeout']
                for index, row in enumerate(rows, 1)) or
            sum(row['worker_timeout'] for row in rows) > limits['maximum_worker_seconds'] or
            sorted(review_policy.digest({key: row[key] for key in fields}) for row in rows) !=
            sorted(map(review_policy.digest, dispatches))):
        raise ValueError('source inspection reservations exceed the plan or differ from retained dispatches')


def inspection_arm(report, pair, profile, variant, threads, *, limits=None, additional=(), excerpts=False):
    from .delivery import stream
    from .factory import Factory
    from .review_inspection import decisive_finding, rejection_value, release_rejection_checks, source_matches, valid_receipt

    arm = pair[variant]
    if 'error' in arm:
        raise ValueError(f"{pair['case']} {variant} failed: {arm['error']}; retained {arm['evidence_root']}")
    measured = review_policy.load_ref('.', arm['measurement'])
    measurement_keys = ('config', 'checks', 'specialists', 'assessment', 'inspection', 'outcome', 'elapsed_seconds')
    if 'rejection' in arm:
        measurement_keys += ('rejection',)
    if profile == 'source':
        expected_caps = limits['cases'][pair['case']] if limits is not None else EXECUTION_LIMITS
        if review_policy.digest(arm.get('execution_limits')) != review_policy.digest(expected_caps):
            raise ValueError('source inspection comparison lacks its bounded execution limits')
        measurement_keys += ('execution_limits',)
        if limits is not None:
            if arm.get('measurement_plan_sha256') != report['plan']['sha256']:
                raise ValueError('source inspection arm does not bind the exact measurement plan')
            measurement_keys += ('measurement_plan_sha256', 'reserved_lanes')
    if measured != {k: arm[k] for k in measurement_keys}:
        raise ValueError('measured arm changed')
    config = arm['config']
    if (config != report['target_config'] or
            review_policy.model_settings(config) != report['model_settings']):
        raise ValueError('inspection arms did not use the exact comparison profile')
    worktree = arm.get('worktree', pair['worktree'])
    f = Factory(worktree, config, github=object())
    f.state = {'version': 1, 'cards': {}}
    head, base, card = pair['head'], pair['base'], pair['card']
    behavioral = profile == 'source' and bool(report['target_policy'].get('behavioral_contract'))
    role = 'behavioral-qa' if behavioral else 'release-package-qa' if profile == 'release' else None
    snapshot = None
    if profile == 'release':
        snapshot = arm['release_snapshot']
        baseline_snapshot = pair['baseline']['release_snapshot']
        if (arm['head'] != head or arm['base'] != base or f.task_digest(arm['card']) != f.task_digest(card) or
                review_policy.digest(baseline_snapshot) != pair['obligations_sha256'] or
                release_snapshot_identity(snapshot) != release_snapshot_identity(baseline_snapshot)):
            raise ValueError('paired release snapshots differ from the frozen package or task')
    source_matches(worktree, head, base, snapshot)
    checks = arm['checks']
    f.record(card)['verification'] = checks
    if (load(checks)['kind'] != 'verification' or checks['head'] != head or checks['base'] != base or
            checks['task_digest'] != pair['task_digest'] or checks['config_digest'] != f.config_digest or
            checks['repository'] != config['repo'] or not checks['checks'] or
            checks['tree'] != git(worktree, 'rev-parse', head + '^{tree}')):
        raise ValueError('comparison did not run pinned project checks')
    for execution in checks['checks']:
        if (load(execution)['kind'] != 'execution' or checksum(execution['log']) != execution['log_sha256'] or
                any(execution[k] != checks[k] for k in ('head', 'base', 'task_digest', 'config_digest'))):
            raise ValueError('comparison check execution changed')
    failed = any(row['exit_code'] for row in checks['checks'])
    commands = [row['command'] for row in checks['checks']]
    if (checks['passed'] is not (not failed) or failed != (pair['kind'] == 'failed-check') or
            (failed and (commands != config['verify_commands'][:len(commands)] or
                         any(row['exit_code'] for row in checks['checks'][:-1]) or
                         not checks['checks'][-1]['exit_code'])) or
            (not failed and commands != config['verify_commands'])):
        raise ValueError('project check outcome differs from the predeclared case')
    if not failed:
        validate_evidence(checks, worktree, f, card, base, freshness=False)
    inspection = arm['inspection']
    report_refs = []
    overlap_seconds = 0.0
    assessment_overlap_seconds = 0.0
    release_final_seconds = None
    artifact_seconds = 0.0
    provisional = None
    cancellation = None
    if variant == 'candidate':
        expected = {'head': head, 'base': base, 'profile': profile,
                    'config_digest': checks['config_digest'], 'task_digest': checks['task_digest'],
                    'obligations': review_policy.digest(snapshot) if snapshot is not None else pair['obligations_sha256'],
                    **({'role': role} if role else {})}
        if inspection['pins'] != expected or inspection['state'] != ('checks-failed' if failed else 'checks-passed'):
            raise ValueError('provisional inspection did not overlap the measured checks')
        provisional = inspection.get('receipt')
        if provisional:
            valid_receipt(provisional, expected)
            if provisional['result']['verdict'] == 'blocked':
                raise ValueError('provisional inspection did not complete')
            overlap_seconds = min(provisional['finished'], inspection['check_finished']) - max(
                provisional['started'], inspection['check_started'])
            if overlap_seconds <= 0 or not math.isclose(overlap_seconds, inspection['overlap_seconds']):
                raise ValueError('no measured check/inspection overlap')
            report_refs.append(provisional)
        elif not (failed and inspection.get('cancelled_after_check_failure') is True):
            raise ValueError('provisional inspection neither completed nor cancelled after failed checks')
        else:
            cancellation = inspection['cancellation']
            if (load(cancellation)['kind'] != 'inspection-cancellation' or
                    any(cancellation.get(key) != value for key, value in expected.items()) or
                    cancellation['finished'] < inspection['check_finished'] or
                    not math.isclose(cancellation['elapsed_seconds'], cancellation['finished'] - cancellation['started']) or
                    (cancellation['log'] and checksum(cancellation['log']) != cancellation['log_sha256'])):
                raise ValueError('cancelled inspection evidence changed')
            overlap_seconds = max(0, min(cancellation['finished'], inspection['check_finished']) -
                                  max(cancellation['started'], inspection['check_started']))
        f.record(card)['review_inspections'] = [inspection]
    elif inspection is not None:
        raise ValueError('baseline includes a provisional model call')
    assessment = arm['assessment']
    rejection = arm.get('rejection')
    if failed:
        if assessment or rejection or arm['specialists'] or arm['outcome'] != 'checks-failed':
            raise ValueError('failed checks allowed a final acceptance assessment')
    elif rejection:
        review_policy.validate_ref(rejection)
        stopped = review_policy.load_ref('.', rejection)
        if (variant != 'candidate' or pair['kind'] != 'retained-real-finding' or
                assessment or arm['specialists'] or arm['outcome'] != 'inspection-fail' or
                not decisive_finding(provisional) or
                stopped['time'] <= checks['time'] or
                not review_policy.receipt_fresh(config, provisional, at=stopped['time'])):
            raise ValueError('inspection rejection cannot replace a completed acceptance assessment')
        release = None
        if profile == 'release':
            release, artifact_seconds = inspection_release_checks(arm, snapshot, head, stopped['time'])
            release_rejection_checks(config, release, checks)
        else:
            route = review_policy.plan(f, card, worktree, base, evaluation=True, consider_inspection=False)
            if not route.get('behavioral_check_ids') or route['obligations'] != ['qa', 'review']:
                raise ValueError('inspection rejection is outside the qualified behavioral route')
        if stopped != rejection_value(provisional, checks, release, stopped['time']):
            raise ValueError('inspection rejection differs from its exact inspection and completed checks')
        obligations = {row['id'] for row in review_policy.criteria(card)}
        if set(pair['obligations']) != obligations:
            raise ValueError('inspection rejection obligations differ from the exact task')
        inspection_rejection_coverage(arm['coverage_adjudication'], obligations, provisional, checks, release)
    else:
        def review_ref(ref):
            receipt = review_policy.load_ref('.', ref)
            return {**receipt, 'receipt_path': ref['path'], 'receipt_sha256': ref['sha256']}

        receipt = review_ref(assessment)
        final_review = receipt['lane'] == 'review'
        composite = bool(provisional and role and provisional['result']['verdict'] == 'pass')
        specialists = [review_ref(ref) for ref in arm['specialists']]
        parallel_rejection = (profile == 'release' and composite and receipt['lane'] == 'security' and
                              receipt['result']['verdict'] == 'fail' and len(specialists) == 1 and
                              specialists[0]['lane'] == 'review' and
                              all('execution_interval' in row for row in (*specialists, receipt)))
        if len(specialists) > 4 or any(
                (v['lane'] not in ('qa', 'security') or v['result']['verdict'] != 'pass') and
                not (parallel_rejection and v['result']['verdict'] in ('pass', 'fail')) or
                v['head'] != head or v['base'] != base or v['config_digest'] != checks['config_digest'] or
                v['task_digest'] != pair['task_digest'] or v['verification'] != checks for v in specialists):
            raise ValueError('specialist lanes lack their completed matching checks')
        report_refs.extend(specialists)
        if (receipt['head'] != head or receipt['base'] != base or
                (not final_review and (receipt['lane'] not in ('qa', 'security') or receipt['result']['verdict'] != 'fail')) or
                receipt['config_digest'] != checks['config_digest'] or receipt['task_digest'] != pair['task_digest'] or
                receipt['time'] <= checks['time'] or
                receipt.get('inspection_sha256') != (provisional['receipt_sha256'] if provisional and final_review else None) or
                receipt['result']['verdict'] not in ('pass', 'fail') or
                arm['outcome'] != receipt['result']['verdict'] or receipt.get('verification') != checks):
            raise ValueError('missing final assessment against the actual completed checks')
        completed = [*specialists, receipt]
        if behavioral:
            expected_lanes = ['review'] if composite else ['qa', 'review']
        elif profile == 'release' and composite:
            expected_lanes = ['security', 'review']
        else:
            expected_lanes = list(review_policy.LANES)
        lanes = [row['lane'] for row in completed]
        if not parallel_rejection and lanes != expected_lanes[:len(completed)]:
            raise ValueError('inspection comparison omitted or reordered required independent lanes')
        if profile == 'source':
            route = historical_source_route(f, card, worktree, base, receipt, provisional, behavioral)
            if bool(route.get('behavioral_check_ids')) != behavioral or bool(route.get('provisional_qa')) != composite:
                raise ValueError('source comparison did not use the exact scoped route')
            f.record(card)['review_route'] = route
            f.record(card)['verification'] = checks
            expected_criteria = {row['id'] for row in review_policy.criteria(card)}
            executed_criteria = set()
            for row in completed:
                if load(row)['kind'] != 'review':
                    raise ValueError('missing immutable source assessment')
                observation = provisional if row['lane'] == 'review' else None
                scope = review_policy.scope(f, card, head, base, checks, row['lane'], inspection=observation)
                prior_qa = replay_prior_qa(f, card, worktree, row)
                covered = review_policy.validate_report(f, card, worktree, row['result'], scope, checks,
                                                        qa_log=row['log'] if row['lane'] == 'qa' else None,
                                                        at=row['time'], since=row['time'], prior_qa=prior_qa,
                                                        prior_qa_before=row['time'])
                if row['lane'] in ('qa', 'review'):
                    executed_criteria.update(item['criterion'] for item in covered if item['status'] == 'covered')
                f.record(card).setdefault('receipts', {})[row['lane']] = row
            if (expected_criteria != set(pair['obligations']) or not expected_criteria <= executed_criteria or
                    (arm['outcome'] == 'pass' and (not review_policy.specialist_dependencies_complete(f, card) or
                                                   not review_policy.acceptance_complete(f, card)))):
                raise ValueError('source assessment did not verify the completed project checks and specialist requests')
        else:
            release, artifact_seconds = inspection_release_checks(arm, snapshot, head, receipt['time'])
            if any(row['release_digest'] != review_policy.digest(release) for row in completed):
                raise ValueError('release assessment did not verify the completed package checks')
            release_review = next((row for row in completed if row['lane'] == 'review'), None)
            release_final_seconds = release_review['elapsed_seconds'] if release_review else 0
            if composite and release_review:
                from .releases import Releases
                descriptor = release['provisional_qa']
                if (release_review.get('provisional_qa') != descriptor or
                        release_review.get('inspection_sha256') != provisional['receipt_sha256'] or
                        descriptor['inspection_sha256'] != provisional['receipt_sha256']):
                    raise ValueError('release final assessment did not bind the provisional package QA')
                Releases.validate_composite_result(release_review['result'], descriptor)
            if composite and set(lanes) == {'security', 'review'}:
                if all('execution_interval' in row for row in completed):
                    intervals = [row['execution_interval'] for row in completed]
                    for window, assessment in zip(intervals, completed):
                        if (not isinstance(window, list) or len(window) != 2 or
                                any(type(t) not in (int, float) or not math.isfinite(t) for t in window) or
                                not math.isclose(window[1] - window[0], assessment['elapsed_seconds']) or
                                window[1] <= window[0] or window[0] < max(inspection['check_finished'], provisional['finished'])):
                            raise ValueError('invalid completed release assessment timing')
                    assessment_overlap_seconds = max(
                        0, min(window[1] for window in intervals) - max(window[0] for window in intervals))
                    if parallel_rejection and not assessment_overlap_seconds:
                        raise ValueError('parallel release rejection lacks intersecting worker intervals')
        report_refs.append(receipt)
    durations, tokens, missing = [], {}, set()
    if cancellation and cancellation['log']:
        durations.append(cancellation['elapsed_seconds'])
        _, observed, _, unknown = stream(cancellation['log'])
        tokens.update({key: value for key, value in observed.items() if value is not None})
        missing.update(key for key, value in unknown.items() if value)
    for receipt in report_refs:
        adapter = receipt['provider']
        lane = receipt.get('lane', 'review')
        model_lane = 'qa' if receipt.get('role') else lane
        model = report['model_settings']['models'].get(model_lane) or report['model_settings']['model']
        expected_provider = (report['target_policy'].get('providers', {}).get(lane, 'codex-exec')
                             if profile == 'source' and 'lane' in receipt else 'codex-exec')
        if (adapter['version'] != report['cli_version'] or 'fake' in adapter['version'].lower() or
                adapter['provider'] != expected_provider or adapter['model'] != model or not adapter['tool'] or
                checksum(receipt['log']) != receipt['log_sha256'] or
                checksum(receipt['result_path']) != receipt['result_sha256']):
            raise ValueError('missing real provider provenance')
        native = adapter['provider'] == 'codex-native'
        thread, message = review_policy.completed_message(receipt['log'], native=native)
        if thread in threads or (not native and json.loads(message) != receipt['result']):
            raise ValueError('reused thread or changed completed assessment')
        if native and not review_policy.native_proof(receipt, worktree):
            raise ValueError('native final assessment output changed')
        threads.add(thread)
        duration = receipt['elapsed_seconds']
        if type(duration) not in (int, float) or not math.isfinite(duration) or duration <= 0:
            raise ValueError('missing model duration')
        durations.append(duration)
        _, observed, _, unknown = stream(receipt['log'], native=native)
        for key, value in observed.items():
            if value is not None:
                tokens[key] = tokens.get(key, 0) + value
            if unknown[key]:
                missing.add(key)
    elapsed = arm['elapsed_seconds']
    if (type(elapsed) not in (int, float) or not math.isfinite(elapsed) or elapsed <= 0 or
            elapsed < checks['elapsed_seconds'] + artifact_seconds + sum(durations) -
            overlap_seconds - assessment_overlap_seconds):
        raise ValueError('wall time excludes measured check, inspection or final-assessment work')
    if not rejection and (set(arm['coverage_adjudication']) != set(pair['obligations']) or
                          any(not v.strip() for v in arm['coverage_adjudication'].values())):
        raise ValueError('obligation coverage lacks independent adjudication')
    expected = inspection_expected(pair)
    confirmed, _, false_positives, unadjudicated = adjudicate_findings(
        arm['adjudications'], report_refs, expected, inspection=True, additional=additional, excerpts=excerpts)
    decisive = 'inspection' if rejection else report_refs[-1]['lane'] if assessment else None
    assessed = confirmed.get(decisive, set())
    detected = expected if failed else assessed & expected
    if limits is not None:
        dispatches = [receipt.get('lane', 'inspection') for receipt in report_refs]
        # A prelaunch cancellation can leave no log. Count it only when the
        # immutable arm measurement records its actual reservation callback.
        if cancellation and (cancellation['log'] or arm['reserved_lanes'] == ['inspection']):
            dispatches.append('inspection')
        if arm['reserved_lanes'] != dispatches:
            raise ValueError('source inspection arm reservations differ from retained worker evidence')
    return {'elapsed_seconds': elapsed, 'overlap_seconds': overlap_seconds,
            **({'assessment_overlap_seconds': assessment_overlap_seconds} if profile == 'release' else {}),
            'check_seconds': checks['elapsed_seconds'], 'artifact_check_seconds': artifact_seconds,
            'compute_seconds': checks['elapsed_seconds'] + artifact_seconds + sum(durations),
            'model_seconds': sum(durations),
            'final_assessment_seconds': (release_final_seconds if release_final_seconds is not None
                                         else durations[-1] if assessment else 0),
            'detected': detected, 'false_positives': false_positives,
            'unadjudicated_findings': unadjudicated,
            'tokens': tokens, 'usage_unknown': sorted(missing),
            **({'additional_detected': assessed.intersection(additional)} if excerpts else {}),
            **({'dispatches': dispatches} if limits is not None else {})}


def evaluate_inspection(report):
    """Require paired real checks and final assessments for every promoted profile."""
    failures, metrics, qualified = [], {}, []
    try:
        adjudicator = report.get('independent_adjudicator')
        if (report['criteria'] != INSPECTION_CRITERIA or report['execution'] != 'real-model' or
                not isinstance(adjudicator, str) or not adjudicator.strip() or not report['binding'] or
                report['target_config'] != validate_config(report['target_config']) or
                report['binding'] != inspection_binding(report['target_config']) or
                report['target_policy'] != inspection_policy(report['target_config']) or
                not review_policy.explicit_review_settings(report['model_settings']) or
                not 1 <= len(report['profiles']) <= 2 or set(report['profiles']) - {'source', 'release'}):
            raise ValueError('inspection requires independent real-model comparison under the declared criteria')
        adjudication_v2 = finding_adjudication_v2(report, live=True)
        threads = set()
        for profile, pairs in report['profiles'].items():
            try:
                if (not 3 <= len(pairs) <= INSPECTION_CRITERIA['maximum_pairs_per_profile'] or
                        len({p['case'] for p in pairs}) != len(pairs) or
                        set(p['kind'] for p in pairs) != set(INSPECTION_CRITERIA['required_kinds'])):
                    raise ValueError('need bounded clean, failed-check and retained-finding pairs')
                if not any(pair['kind'] == 'retained-real-finding' and
                           pair['candidate'].get('outcome') == 'inspection-fail' for pair in pairs):
                    raise ValueError('need a measured rejection of a retained finding')
                metrics[profile] = []
                from .factory import Factory
                if profile not in report['target_policy']['inspection']['enabled']:
                    raise ValueError('profile was not enabled in the exact comparison configuration')
                if profile == 'source' and not report['target_policy'].get('behavioral_contract'):
                    raise ValueError('source inspection promotion requires the qualified two-stage behavioral route')
                limits = source_inspection_limits(report, pairs) if profile == 'source' else None
                dispatches = []
                for pair in pairs:
                    if not pair['obligations'] or pair['head'] == pair['base']:
                        raise ValueError('case lacks a labeled exact change and obligations')
                    expected = inspection_expected(pair)
                    if (Factory.task_digest(pair['card']) != pair['task_digest'] or
                            (profile == 'source' and pair['obligations_sha256'] !=
                             review_policy.digest(review_policy.criteria(pair['card'])))):
                        raise ValueError('case task or obligation scope changed')
                    if 'additional_findings' in pair and not adjudication_v2:
                        raise ValueError('additional finding proof requires version 2 adjudication')
                    additional = (validate_additional_findings(pair, report['independent_adjudicator'])
                                  if adjudication_v2 else set())
                    a = inspection_arm(report, pair, profile, 'baseline', threads, limits=limits,
                                       additional=additional, excerpts=adjudication_v2)
                    b = inspection_arm(report, pair, profile, 'candidate', threads, limits=limits,
                                       additional=additional, excerpts=adjudication_v2)
                    if limits is not None:
                        cap = limits['cases'][pair['case']]['worker_timeout']
                        for variant, observed in (('baseline', a), ('candidate', b)):
                            dispatches.extend({'case': pair['case'], 'variant': variant, 'lane': lane, 'worker_timeout': cap}
                                              for lane in observed.pop('dispatches'))
                    if a['unadjudicated_findings'] or b['unadjudicated_findings']:
                        raise ValueError(f'{pair["case"]} has findings outside the declared inventory')
                    if (a.pop('detected') != expected or b.pop('detected') != expected or
                            b['false_positives'] > a['false_positives'] or
                            (pair['kind'] == 'clean' and
                             (pair['baseline']['outcome'] != 'pass' or pair['candidate']['outcome'] != 'pass'))):
                        raise ValueError('defect detection, acceptance or false positives regressed')
                    if adjudication_v2:
                        baseline_additional, candidate_additional = (value.pop('additional_detected') for value in (a, b))
                        validate_additional_detection(additional, baseline_additional, candidate_additional)
                        a['verified_additional_findings'] = len(baseline_additional)
                        b['verified_additional_findings'] = len(candidate_additional)
                    metrics[profile].append({'case': pair['case'], 'baseline': a, 'candidate': b})
                    if pair['kind'] == 'clean' and (
                            min(value['check_seconds'] + value['artifact_check_seconds'] for value in (a, b)) <
                            INSPECTION_CRITERIA['minimum_check_seconds'] or
                            b['elapsed_seconds'] > a['elapsed_seconds'] * INSPECTION_CRITERIA['maximum_latency_ratio']):
                        raise ValueError('clean paired checks were too short or final wall time did not improve by 5%')
                if limits is not None:
                    validate_source_inspection_reservations(report, limits, dispatches)
                a, b = (statistics.median(row[key]['elapsed_seconds'] for row in metrics[profile])
                        for key in ('baseline', 'candidate'))
                if b > a * INSPECTION_CRITERIA['maximum_latency_ratio']:
                    raise ValueError('end-to-end wall time did not improve by 5% after final assessment')
                qualified.append(profile)
            except (AttributeError, OSError, KeyError, TypeError, ValueError, Halt) as exc:
                failures.append(f'{profile}: {exc}')
    except (AttributeError, OSError, KeyError, TypeError, ValueError, Halt) as exc:
        failures.append(str(exc))
    return {'qualified': qualified, 'failures': failures, 'metrics': metrics, 'credits': None}
