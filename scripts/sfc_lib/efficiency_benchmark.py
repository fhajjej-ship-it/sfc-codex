"""Bounded delivery comparison and offline qualification. Grants no authority."""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import time
import zipfile

from . import delivery
from .common import Halt, git, now, read_json, write_json
from .verification import checksum

BASELINE = 'efe380bb6ec4cdf71f4ef8c8e1d382fe72708411'
CASES = ('ordinary-feature', 'bug-fix', 'documentation-correction',
         'package-version-correction', 'security-sensitive', 'interrupted-release')
ROUTINE = frozenset(CASES[:4])
MODEL_FIELDS = ('model', 'reasoning_effort', 'service_tier')
TARGETS = {'routine_median_latency': .30, 'comparable_token_usage': .20}
MODEL_OBJECTIVES = {'latency': ('routine_median_latency', .05), 'usage': ('comparable_token_usage', .20)}
PUBLISHED_BOUNDARY = 'verified-downloaded-install'
DRAFT_BOUNDARY = 'verified-draft-install'
LEGACY_RUNTIME_DIRS = ('scripts', 'skills', '.codex-plugin', 'tests')
RUNTIME_DIRS = (*LEGACY_RUNTIME_DIRS, 'schemas')
WORKER_SCHEMAS = ('lane-result.json', 'review-result.json')
COMPOSITE_SCHEMA = 'release-review-result.json'
DEFAULT_WORKER_TIMEOUT = 120
MAX_WORKER_TIMEOUT = 300


def inventory_digest(inventory):
    return hashlib.sha256(json.dumps(sorted(inventory), separators=(',', ':')).encode()).hexdigest()


def runtime_digest(root, directories=RUNTIME_DIRS, *, canonical_order=True):
    files = [p for d in directories
             for p in (root / d).rglob('*') if p.is_file() and '__pycache__' not in p.parts]
    inventory = [(p.relative_to(root).as_posix(), checksum(p)) for p in sorted(files)]
    if not inventory:
        raise Halt('Benchmark runtime source is unavailable')
    if canonical_order:
        return inventory_digest(inventory)
    # Preserve the exact earlier Path-order snapshot hash for retained trials.
    return hashlib.sha256(json.dumps(inventory, separators=(',', ':')).encode()).hexdigest()


def validate_worker_schemas(root, *, require_composite=False):
    names = (*WORKER_SCHEMAS, COMPOSITE_SCHEMA) if require_composite else WORKER_SCHEMAS
    for name in names:
        try:
            if read_json(root / 'schemas' / name).get('type') != 'object':
                raise ValueError('expected object result schema')
        except (OSError, ValueError, AttributeError) as exc:
            raise Halt('Benchmark runtime missing usable worker output schema: ' + name) from exc


def snapshot_runtime(root, destination):
    for directory in RUNTIME_DIRS:
        shutil.copytree(root / directory, destination / directory, ignore=shutil.ignore_patterns('__pycache__'))
    validate_worker_schemas(destination, require_composite=True)


def reference(path):
    return {'path': str(path), 'sha256': checksum(path)} if path.is_file() else None


def pinned(ref):
    path = Path(ref['path'])
    if checksum(path) != ref['sha256']:
        raise Halt('Retained benchmark evidence changed: ' + str(path))
    return path


def compare(root, output, inventory=CASES, *, cases=None, baseline=None, candidate=None, profile='both',
            model_only=False, model_objective=None, worker_timeout_seconds=DEFAULT_WORKER_TIMEOUT,
            draft_only=False, saved_config=None, legacy_release_bindings=False, baseline_repository=None):
    """Run at most six scenarios; real runs permit only one matched profile pair."""
    validate_worker_timeout(worker_timeout_seconds)
    if tuple(inventory) != CASES or profile not in ('fresh', 'migrated', 'both'):
        raise Halt('Unknown delivery fixture or profile')
    if (baseline is None) != (candidate is None):
        raise Halt('Real comparison requires both explicit model configurations')
    real = baseline is not None
    if legacy_release_bindings and (model_only or draft_only or saved_config is not None):
        raise Halt('Legacy release bindings require a v0.1.17 publication comparison')
    if model_only and not real:
        raise Halt('Model comparison requires actual, explicit model settings')
    if real and draft_only and not model_only:
        raise Halt('Real draft measurements require a same-runtime model comparison')
    if saved_config is not None and not draft_only:
        raise Halt('Saved configuration is supported only for draft comparisons')
    if saved_config is not None and profile == 'both':
        raise Halt('Saved draft comparison requires one profile')
    if model_objective is not None and (not model_only or model_objective not in MODEL_OBJECTIVES):
        raise Halt('A model objective requires a same-runtime model comparison')
    if model_only and model_objective is None:
        raise Halt('Predeclare the intended model benefit: latency or usage')
    if real and profile == 'both':
        raise Halt('Real comparison requires one current profile: fresh or migrated')
    for settings in (baseline, candidate):
        if settings is not None and (not isinstance(settings, dict) or any(
                not isinstance(settings.get(k), str) or not settings[k].strip() for k in MODEL_FIELDS)):
            raise Halt('Real comparison needs explicit model, effort and tier')
    if model_only and all(baseline[k] == candidate[k] for k in MODEL_FIELDS):
        raise Halt('Model comparison requires different requested settings')
    selected = [case for case in CASES if cases is None or case in cases]
    if not selected or cases is not None and len(selected) != len(cases):
        raise Halt('Select known, distinct, bounded delivery scenarios')
    root, output = Path(root).resolve(), Path(output).resolve()
    baseline_root = root if baseline_repository is None else Path(baseline_repository).resolve()
    try:
        archive = subprocess.check_output(['git', 'archive', '--format=zip', BASELINE],
                                          cwd=baseline_root, stderr=subprocess.PIPE, timeout=30)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise Halt('The pinned v0.1.17 source is unavailable. Supply a checkout containing it with '
                   '--baseline-repository.') from exc
    output.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    released, current = output / 'runtime-released', output / 'runtime-current'
    archive_path = output / 'released-source.zip'
    archive_path.write_bytes(archive)
    with zipfile.ZipFile(io.BytesIO(archive)) as source:
        source.extractall(released)
    validate_worker_schemas(released)
    snapshot_runtime(root, current)
    environment = None
    from .fixture_trust import FIXTURE_TRUST_MODE
    if real:
        from .profile_qualification import model_environment
        environment = model_environment(current)
    runtimes = {name: {'root': str(runtime), 'sha256': runtime_digest(runtime), 'commit': commit}
                for name, runtime, commit in (('released', released, BASELINE),
                                              ('current', current, git(root, 'rev-parse', 'HEAD')))}
    # The current runtime snapshot is source-bound even when local edits exist.
    # Its Git commit is the checkout base; the snapshot digest identifies bytes.
    runtimes['released']['archive'] = reference(archive_path)
    profiles = ('fresh', 'migrated') if profile == 'both' else (profile,)
    arm_runtimes = {'released': 'current' if model_only or saved_config is not None else 'released',
                    **{name: 'current' for name in profiles}}
    objective = ({'name': model_objective, 'metric': MODEL_OBJECTIVES[model_objective][0],
                  'minimum_reduction': MODEL_OBJECTIVES[model_objective][1]} if model_only else None)
    worker_launch_limit = sum(4 if case == 'security-sensitive' else 7 for case in selected) * (1 + len(profiles))
    saved = None
    route = None
    original_config = None
    if saved_config is not None:
        from .profile_qualification import model_route_binding
        saved_config = Path(saved_config).resolve()
        saved = read_json(saved_config)
        original_config = reference(saved_config)
        if real and baseline != {name: saved.get(name) for name in MODEL_FIELDS}:
            raise Halt('Baseline model settings differ from the saved project')
        if real and any(baseline[name] != candidate[name] for name in ('model', 'service_tier')):
            raise Halt('Saved draft comparison changes reasoning effort only')
        route = {'source': model_route_binding({k: v for k, v in saved.items() if k != 'release'},
                                               saved_config.parent.parent),
                 'release': model_route_binding(saved, saved_config.parent.parent)}
        write_json(output / 'saved-config.json', saved)
        saved = reference(output / 'saved-config.json')
    plan_path = output / 'comparison-plan.json'
    write_json(plan_path, {'version': 1, 'created_at': now(), 'cases': selected, 'profiles': list(profiles),
                          'runtimes': runtimes, 'arm_runtimes': arm_runtimes,
                          'comparison_kind': 'model' if model_only else 'delivery',
                          'model_objective': objective, 'model_environment': environment,
                          'baseline_settings': baseline, 'candidate_settings': candidate,
                          'maximum_attempts_per_ticket': 1, 'worker_timeout_seconds': worker_timeout_seconds,
                          'fixture_trust': FIXTURE_TRUST_MODE if real else None,
                          'completion_boundary': DRAFT_BOUNDARY if draft_only else PUBLISHED_BOUNDARY,
                          **({'legacy_release_bindings': True} if legacy_release_bindings else {}),
                          **({'saved_config': saved, 'saved_route': route,
                              'critical_case': 'bug-fix' if 'bug-fix' in selected else None} if saved else {}),
                          'maximum_worker_launches': worker_launch_limit,
                          'maximum_worker_seconds': worker_launch_limit * worker_timeout_seconds})
    rows = []
    for index, case in enumerate(selected):
        arms = ('released', *profiles)
        if index % 2:
            arms = tuple(reversed(arms))
        for arm in arms:
            if real and (model_environment(current) != environment or
                         original_config is not None and checksum(saved_config) != original_config['sha256']):
                raise Halt('Model environment changed before dispatch; retain all runs without promotion')
            worker_cap = 4 if case == 'security-sensitive' else 7
            folder = output / 'runs' / case / arm
            settings = baseline if arm == 'released' else candidate
            script = current / 'tests/benchmark_delivery.py'
            fixture_arm = profile if (model_only or saved) and arm == 'released' else arm
            cmd = [sys.executable, str(script), '--output', str(folder), '--case', case, '--fixture-arm', fixture_arm]
            cmd += ['--normal-profile', '--worker-cap', str(worker_cap),
                    '--fixture-worker-timeout', str(worker_timeout_seconds)]
            if draft_only:
                cmd += ['--draft-only']
            if legacy_release_bindings and arm == 'released':
                cmd += ['--legacy-release-bindings']
            if saved:
                cmd += ['--saved-config', str(pinned(saved))]
            if real:
                cmd += ['--pin-fixture-trust']
            if settings is not None:
                path = output / ('settings-' + arm + '.json')
                if not path.exists():
                    write_json(path, {k: settings[k] for k in MODEL_FIELDS})
                cmd += ['--baseline-model-config', str(path)]
            runtime = runtimes[arm_runtimes[arm]]['root']
            # Only the fixture process imports the exact alternate source.
            child_env = {**os.environ, 'SFC_FIXTURE_RUNTIME_ROOT': runtime}
            child_log = output / f'{case}-{arm}.log'
            with child_log.open('w') as log:
                try:
                    # Allow time for fixture setup, non-model checks and child cleanup.
                    arm_timeout = worker_cap * worker_timeout_seconds + 120
                    child = subprocess.run(cmd, cwd=output, env=child_env, stdout=log,
                                           stderr=subprocess.STDOUT, timeout=arm_timeout)
                    code = child.returncode
                except subprocess.TimeoutExpired:
                    code = None
            outcome = reference(folder / 'scenario-outcome.json')
            observed = read_json(pinned(outcome)) if outcome else {}
            report = Path(observed['report']) if observed.get('report') else folder / 'delivery-report.json'
            if not report.exists() and (folder / 'repo/.git').exists():
                # Released and interrupted arms may lack a final delivery
                # report. Rebuild from their untouched retained receipts.
                write_json(report, delivery.report(folder / 'repo'))
            rows.append({'case': case, 'arm': arm, 'exit_code': code,
                         'scenario': reference(folder / 'scenario.json'), 'outcome': outcome,
                         'adapter_trace': reference(folder / 'adapter-executions.jsonl'),
                         'delivery_report': reference(report), 'child_log': reference(child_log)})
            # Keep a bounded interrupted experiment inspectable without claiming
            # that its incomplete arms constitute a qualification input.
            write_json(output / 'comparison-progress.json', {
                'plan': reference(plan_path), 'rows': rows,
                'evaluation_wall_seconds': time.monotonic() - started, 'complete': False})
            if real and (model_environment(current) != environment or
                         original_config is not None and checksum(saved_config) != original_config['sha256']):
                raise Halt('Model environment changed during comparison; retain all runs without promotion')
            expected_outcome = expected_delivery(case, DRAFT_BOUNDARY if draft_only else PUBLISHED_BOUNDARY)
            if real and (code != 0 or observed.get('observed') != expected_outcome):
                write_json(output / 'comparison-stop.json', {
                    'plan': reference(plan_path), 'case': case, 'arm': arm,
                    'outcome': outcome, 'exit_code': code,
                    'reason': 'An arm did not complete; remaining paid comparisons were not dispatched',
                    'qualified': False, 'evaluation_wall_seconds': time.monotonic() - started})
                raise Halt('Delivery arm did not complete; retained evidence, no further paid dispatches or promotion')
    manifest = output / 'qualification-input.json'
    elapsed = time.monotonic() - started
    write_json(manifest, {'version': 1, 'execution': 'real-Codex-with-fake-GitHub' if real else 'fake-adapters',
                          'runtimes': runtimes, 'profiles': list(profiles), 'rows': rows,
                          'runtime_inventory_version': 2,
                          'plan': reference(plan_path),
                          'model_environment': environment,
                          'comparison_kind': 'model' if model_only else 'delivery',
                          **({'model_objective': objective} if model_only else {}),
                          'arm_runtimes': arm_runtimes,
                          'evaluation_wall_seconds': elapsed})
    result = evaluate(manifest)
    write_json(output / 'qualification.json', result)
    write_json(output / 'comparison-progress.json', {
        'plan': reference(plan_path), 'rows': rows, 'evaluation_wall_seconds': elapsed,
        'complete': True, 'input': reference(manifest)})
    return result


def arm_metrics(row):
    outcome = read_json(pinned(row['outcome'])) if row.get('outcome') else {}
    scenario = read_json(pinned(row['scenario'])) if row.get('scenario') else {}
    report = read_json(pinned(row['delivery_report'])) if row.get('delivery_report') else {}
    install = outcome.get('downloaded_install')
    if install:
        for path_key, hash_key in (('artifact', 'artifact_sha256'), ('log', 'log_sha256')):
            if checksum(Path(install[path_key])) != install[hash_key]:
                raise Halt('Downloaded installation evidence changed')
    if row.get('child_log'):
        pinned(row['child_log'])
    trace = {}
    if row.get('adapter_trace'):
        for line in pinned(row['adapter_trace']).read_text().splitlines():
            event = json.loads(line)
            trace.setdefault(event['id'], {})[event['event']] = event
    executions, failed, retries, gaps, infrastructure = [], [], {}, [], []
    usage = {k: {'known': 0, 'unknown_executions': 0} for k in delivery.TOKENS}
    for identifier, events in trace.items():
        start, end = events.get('started'), events.get('completed', {})
        if start is None:
            raise Halt('Adapter trace lacks a start observation')
        observed = 'launched' in events or type(end.get('exit_code')) is int
        log = Path(start['log'])
        item = {**start, **end, 'id': identifier, 'launch_observed': observed,
                'real_process_launch_observed': 'launched' in events,
                'requested': delivery.settings(start['argv']) if start['worker'] else None}
        if not observed:
            gaps.append(identifier + ': launch unconfirmed')
        if item['worker']:
            argv = item['argv']
            if '--output-schema' in argv and not Path(argv[argv.index('--output-schema') + 1]).is_file():
                infrastructure.append(identifier + ': worker output schema unavailable in runtime snapshot')
            lane = log.parent.name.split('-')[0]
            issue = next((x for x in log.parts if x.startswith('issue-')), 'unknown-issue')
            group = issue + '/' + lane
            if observed:
                retries[group] = retries.get(group, 0) + 1
            tokens, complete, unknown = dict.fromkeys(delivery.TOKENS), False, dict.fromkeys(delivery.TOKENS, True)
            if observed and end.get('log_sha256') and log.is_file() and checksum(log) == end['log_sha256']:
                stream_gaps = []
                _, tokens, complete, unknown = delivery.stream(
                    log, native=delivery.native_review_command(argv), gaps=stream_gaps)
                gaps.extend(identifier + ': ' + gap for gap in stream_gaps)
            elif observed:
                gaps.append(identifier + ': worker log missing or changed')
            item['tokens'] = tokens
            if observed:
                for key in delivery.TOKENS:
                    usage[key]['known'] += tokens[key] or 0
                    usage[key]['unknown_executions'] += not complete or unknown[key] or end.get('exit_code') != 0
            if end.get('result_path'):
                try:
                    if checksum(Path(end['result_path'])) != end['result_sha256']:
                        raise ValueError('result bytes changed')
                    item['verdict'] = read_json(end['result_path']).get('verdict')
                except (OSError, ValueError, AttributeError):
                    gaps.append(identifier + ': worker result unavailable or malformed')
        if end.get('error_type') or end.get('exit_code', 0) or item.get('verdict') in ('fail', 'blocked'):
            failed.append(identifier)
        executions.append(item)
    workers = [v for v in executions if v['worker'] and v['launch_observed']]
    full_suite = 'python3 -m unittest discover -s tests -v'
    checks = [r for r in executions if not r['worker'] and r['argv'][-1] == full_suite and r['launch_observed']]
    reuses = [r for r in report.get('verification_references', []) if r['command'] == full_suite and r['reuse']]
    return {'scenario': {k: scenario.get(k) for k in ('source_tree', 'task_sha256', 'route_sha256', 'settings',
                'profile', 'configuration_route_sha256', 'model_route_sha256', 'source_model_route_sha256',
                'release_model_route_sha256', 'runtime_root', 'runtime_sha256', 'worker_launch_cap',
                'fixture_worker_timeout_seconds', 'fixture_trust', 'fixture_trust_override_sha256',
                'legacy_release_bindings')},
            'config_sha256': hashlib.sha256(json.dumps(scenario.get('config'), sort_keys=True).encode()).hexdigest()
            if 'config' in scenario else None,
            'outcome': outcome, 'arm_exit_code': row['exit_code'], 'processes': executions, 'tokens': usage,
            'worker_seconds': sum(v.get('elapsed_seconds') or 0 for v in workers),
            'worker_unknown_durations': sum(v.get('elapsed_seconds') is None for v in workers),
            'retries': {k: n - 1 for k, n in retries.items() if n > 1}, 'failed_execution_ids': failed,
            'full_suite_executions': len(checks), 'full_suite_reuse_references': len(reuses),
            'infrastructure_gaps': infrastructure,
            'unknowns': gaps + report.get('unknowns', []), 'verification_references': report.get('verification_references', []),
            'release_observations': report.get('releases', []),
            'credits': None, 'billing': None, 'currency_estimate': None,
            'evidence': {k: row.get(k) for k in ('scenario', 'outcome', 'adapter_trace', 'delivery_report', 'child_log')}}


def expected_delivery(case, boundary):
    if case == 'security-sensitive':
        return 'human-handoff'
    return 'verified-draft' if boundary == DRAFT_BOUNDARY else 'done'


def quality_for(case, baseline, current, decisions, *, boundary=PUBLISHED_BOUNDARY):
    if baseline.get('infrastructure_gaps') or current.get('infrastructure_gaps'):
        return 'inconclusive', 'Comparison infrastructure prevented a valid model comparison'
    a, b = baseline['outcome'], current['outcome']
    expected = expected_delivery(case, boundary)
    if (a.get('observed') != expected or b.get('observed') != expected or
            baseline['arm_exit_code'] != 0 or current['arm_exit_code'] != 0):
        return 'missed', 'At least one arm did not reach the expected outcome'
    if not a.get('candidate_tree') or a['candidate_tree'] != b.get('candidate_tree'):
        return 'missed', 'Final source trees differ'
    if b.get('human_handoffs') != a.get('human_handoffs'):
        return 'missed', 'Recorded human handoffs differ'
    fields = ('critical_expected', 'critical_detected', 'criteria_expected', 'criteria_covered',
              'false_positives', 'human_interventions')
    if not isinstance(decisions, dict) or any(
            not isinstance(decisions.get(arm), dict) or any(
                type(decisions[arm].get(k)) is not int or decisions[arm][k] < 0 for k in fields)
            for arm in ('released', 'current')):
        return 'inconclusive', 'No complete independent defect, coverage and intervention assessment'
    old, new = decisions['released'], decisions['current']
    if any(old[k] != new[k] for k in ('critical_expected', 'criteria_expected')) or not new['criteria_expected']:
        return 'inconclusive', 'Assessment obligations differ or are empty'
    if old['critical_detected'] != old['critical_expected'] or old['criteria_covered'] != old['criteria_expected']:
        return 'inconclusive', 'Baseline did not demonstrate required defect detection and acceptance coverage'
    if (new['critical_detected'] != new['critical_expected'] or new['criteria_covered'] != new['criteria_expected']
            or new['false_positives'] > old['false_positives'] or new['human_interventions'] > old['human_interventions']):
        return 'missed', 'Critical regression, acceptance, false-positive or intervention gate failed'
    return 'met', 'Independent assessment covered the fixture obligations'


def reduction(before, after):
    return (before - after) / before if before > 0 else None


def normalized_tokens(arm):
    tokens = arm['tokens']
    fields = ('input_tokens', 'output_tokens')
    if (any(tokens[k]['unknown_executions'] for k in fields) or not any(
            row['worker'] and row['launch_observed'] for row in arm['processes'])):
        return None
    return sum(tokens[k]['known'] for k in fields)


def validate_installed_boundary(measured, repo):
    install = measured['outcome'].get('downloaded_install')
    if not isinstance(install, dict):
        raise Halt('Successful delivery lacks downloaded-install evidence')
    commands = [p for p in measured['processes'] if not p['worker'] and
                Path(p['log']).resolve() == Path(install['log']).resolve()]
    if len(commands) != 1:
        raise Halt('Downloaded installation lacks one recorded command')
    command = commands[0]
    if (not command['real_process_launch_observed'] or command.get('exit_code') != 0 or
            command.get('error_type') or command.get('log_sha256') != install['log_sha256'] or
            command['argv'][1:] != ['scripts/package_fixture.py', 'smoke', install['artifact']] or
            Path(command.get('cwd', '')).resolve() != repo.resolve()):
        raise Halt('Downloaded installation lacks successful matching command evidence')


def validate_draft_boundary(measured, repo):
    outcome = measured['outcome']
    draft = read_json(pinned(outcome['draft_journal']))
    assets = {asset['name']: asset for asset in draft['artifacts']}
    downloadable = Path(outcome['downloaded_install']['artifact'])
    downloads = repo / '.git/sfc-releases' / draft['version'] / 'downloads'
    if (draft['state'] != 'draft-unpublished' or 'publication' in draft['effects'] or
            draft['effects']['draft']['observed']['draft'] is not True or
            not draft.get('publication_manifest_sha256') or outcome.get('publish_count') != 0 or
            downloadable.parent != downloads or downloadable.name not in assets or
            any(draft['effects']['asset-' + name]['state'] != 'verified' or
                checksum(downloads / name) != asset['sha256'] for name, asset in assets.items())):
        raise Halt('Draft delivery lacks a verified unpublished asset set')
    validate_installed_boundary(measured, repo)


def verified_settings(arm):
    settings = arm['scenario']['settings']
    workers = [r for r in arm['processes'] if r['worker']]
    return bool(settings and workers and all(r['real_process_launch_observed'] and
                all(r['requested'].get(k) == settings[k] for k in MODEL_FIELDS) for r in workers))


def validate_worker_timeout(seconds):
    if type(seconds) is not int or not 1 <= seconds <= MAX_WORKER_TIMEOUT:
        raise Halt(f'Benchmark worker timeout must be an integer from 1 to {MAX_WORKER_TIMEOUT} seconds')


def evaluate(path, *, adjudication=None):
    """Evaluate retained evidence without model calls or writes."""
    path = Path(path).resolve()
    value = read_json(path)
    if value['version'] != 1 or value['runtimes']['released']['commit'] != BASELINE:
        raise Halt('Qualification requires the exact released v0.1.17 baseline')
    inventory_version = value.get('runtime_inventory_version', 1)
    if inventory_version not in (1, 2):
        raise Halt('Unknown benchmark runtime inventory version')
    directories = RUNTIME_DIRS if inventory_version == 2 else LEGACY_RUNTIME_DIRS
    released = value['runtimes']['released']
    canonical_order = inventory_version == 2 or bool(released.get('archive'))
    for entry in value['runtimes'].values():
        if runtime_digest(Path(entry['root']), directories, canonical_order=canonical_order) != entry['sha256']:
            raise Halt('Benchmark runtime changed; preserve the original source snapshot')
        if inventory_version == 2:
            validate_worker_schemas(Path(entry['root']))
    if released.get('archive'):
        with zipfile.ZipFile(pinned(released['archive'])) as source:
            inventory = [(i.filename, hashlib.sha256(source.read(i)).hexdigest()) for i in source.infolist()
                         if not i.is_dir() and i.filename.split('/')[0] in directories
                         and '__pycache__' not in i.filename.split('/')]
            if source.comment != BASELINE.encode() or inventory_digest(inventory) != released['sha256']:
                raise Halt('Released source archive does not match the runtime snapshot')
    kind = value.get('comparison_kind', 'delivery')
    model_only = kind == 'model'
    objective = value.get('model_objective')
    if objective is not None:
        if not model_only or not isinstance(objective, dict) or objective.get('name') not in MODEL_OBJECTIVES:
            raise Halt('Unknown predeclared model comparison objective')
        metric, threshold = MODEL_OBJECTIVES[objective['name']]
        if objective != {'name': objective['name'], 'metric': metric, 'minimum_reduction': threshold}:
            raise Halt('Model comparison objective differs from its predeclared gate')
        if not value.get('plan'):
            raise Halt('Model objective requires its retained pre-execution plan')
    plan = None
    boundary = PUBLISHED_BOUNDARY
    if value.get('plan'):
        plan = read_json(pinned(value['plan']))
        boundary = plan.get('completion_boundary', PUBLISHED_BOUNDARY)
        if boundary not in (PUBLISHED_BOUNDARY, DRAFT_BOUNDARY) or (boundary == DRAFT_BOUNDARY and not model_only
                and value['execution'] != 'fake-adapters'):
            raise Halt('Unknown or unsupported comparison completion boundary')
        if any(plan.get(k) != value.get(k) for k in
               ('runtimes', 'profiles', 'arm_runtimes', 'comparison_kind', 'model_objective', 'model_environment')):
            raise Halt('Comparison differs from its pre-execution plan')
        if (plan.get('version') != 1 or not isinstance(plan.get('cases'), list) or
                not plan['cases'] or len(plan['cases']) != len(set(plan['cases'])) or
                set(plan['cases']) - set(CASES)):
            raise Halt('Comparison has an invalid planned case inventory')
        if 'worker_timeout_seconds' in plan or 'maximum_worker_launches' in plan:
            validate_worker_timeout(plan.get('worker_timeout_seconds'))
        if (plan.get('worker_timeout_seconds', DEFAULT_WORKER_TIMEOUT) != DEFAULT_WORKER_TIMEOUT and
                'maximum_worker_seconds' not in plan):
            raise Halt('An explicit worker timeout requires its planned worker time budget')
        if 'maximum_worker_seconds' in plan and (
                type(plan['maximum_worker_seconds']) is not int or
                type(plan.get('maximum_worker_launches')) is not int or
                plan['maximum_worker_seconds'] != plan['maximum_worker_launches'] * plan['worker_timeout_seconds']):
            raise Halt('Comparison has an invalid planned worker time budget')
    judgments = {}
    if adjudication is not None:
        review = read_json(adjudication)
        if review.get('input_sha256') != checksum(path) or not review.get('reviewer'):
            raise Halt('Independent assessment does not bind this comparison')
        judgments = review['cases']
    rows = {}
    profiles = value['profiles']
    if not profiles or len(profiles) != len(set(profiles)) or set(profiles) - {'fresh', 'migrated'}:
        raise Halt('Unknown or duplicate comparison profiles')
    for row in value['rows']:
        key = (row['case'], row['arm'])
        if key in rows or key[0] not in CASES or key[1] not in ['released', *profiles]:
            raise Halt('Duplicate or unknown delivery comparison arm')
        rows[key] = arm_metrics(row)
    real = value['execution'] == 'real-Codex-with-fake-GitHub'
    saved_route = plan.get('saved_route') if plan else None
    legacy_bindings = bool(plan and plan.get('legacy_release_bindings'))
    if legacy_bindings and (model_only or boundary != PUBLISHED_BOUNDARY or saved_route):
        raise Halt('Legacy release bindings require a v0.1.17 publication comparison')
    if saved_route:
        pinned(plan['saved_config'])
    expected_bindings = {'released': 'current' if model_only or saved_route else 'released',
                         **{n: 'current' for n in profiles}}
    if kind not in ('delivery', 'model') or (value.get('arm_runtimes', expected_bindings) != expected_bindings):
        raise Halt('Comparison runtime bindings differ')
    if plan is not None:
        expected_rows = {(case, arm) for case in plan['cases'] for arm in ('released', *profiles)}
        if set(rows) != expected_rows:
            raise Halt('Comparison cases differ from its pre-execution plan')
        for (case, arm), measured in rows.items():
            scenario = measured['scenario']
            runtime = value['runtimes'][expected_bindings[arm]]
            expected_settings = plan['baseline_settings' if arm == 'released' else 'candidate_settings']
            if (scenario.get('settings') != expected_settings or scenario.get('runtime_root') != runtime['root'] or
                    scenario.get('runtime_sha256') != runtime['sha256']):
                raise Halt('Observed arm model or runtime differs from its pre-execution plan')
            if bool(scenario.get('legacy_release_bindings')) != (legacy_bindings and arm == 'released'):
                raise Halt('Observed release evidence adapter differs from its pre-execution plan')
            if saved_route and (scenario.get('source_model_route_sha256') != saved_route['source'] or
                    (case != 'security-sensitive' and scenario.get('release_model_route_sha256') != saved_route['release'])):
                raise Halt('Observed arm differs from the saved source or release route')
            if plan.get('completion_boundary') in (PUBLISHED_BOUNDARY, DRAFT_BOUNDARY):
                outcome = measured['outcome']
                repo = Path(measured['evidence']['scenario']['path']).parent / 'repo'
                if boundary == PUBLISHED_BOUNDARY and outcome.get('observed') == 'done':
                    validate_installed_boundary(measured, repo)
                elif boundary == DRAFT_BOUNDARY and outcome.get('observed') == 'verified-draft':
                    validate_draft_boundary(measured, repo)
            if 'maximum_worker_launches' in plan:
                cap = 4 if case == 'security-sensitive' else 7
                workers = [p for p in measured['processes'] if p['worker']]
                timeout = plan['worker_timeout_seconds']
                if (scenario.get('worker_launch_cap') != cap or
                        scenario.get('fixture_worker_timeout_seconds') != timeout or
                        len(workers) > cap or any(type(p.get('timeout')) not in (int, float) or
                            not 0 < p['timeout'] <= timeout or
                            ('maximum_worker_seconds' in plan and p['timeout'] != timeout) for p in workers)):
                    raise Halt('Observed arm exceeded its predeclared worker limits')
            if plan.get('fixture_trust'):
                from .fixture_trust import FIXTURE_TRUST_MODE, fixture_trust_override, verify_fixture_trust
                repo = Path(measured['evidence']['scenario']['path']).parent / 'repo'
                override = fixture_trust_override(repo)
                if (plan['fixture_trust'] != FIXTURE_TRUST_MODE or
                        scenario.get('fixture_trust') != FIXTURE_TRUST_MODE or
                        scenario.get('fixture_trust_override_sha256') != hashlib.sha256(override.encode()).hexdigest() or
                        any(not verify_fixture_trust(p['argv'], repo) for p in measured['processes'] if p['worker'])):
                    raise Halt('Observed fixture trust differs from its pre-execution plan')
            for process in measured['processes']:
                if not process['worker']:
                    continue
                argv = process.get('argv', [])
                if '--output-schema' not in argv:
                    raise Halt('Observed worker lacks its runtime schema provenance')
                schema = Path(argv[argv.index('--output-schema') + 1]).resolve()
                if schema not in {Path(runtime['root']).resolve() / 'schemas' / name
                                  for name in (*WORKER_SCHEMAS, COMPOSITE_SCHEMA)}:
                    raise Halt('Observed worker was dispatched from a different runtime')
        if 'maximum_worker_launches' in plan:
            maximum = sum(4 if case == 'security-sensitive' else 7 for case in plan['cases']) * (1 + len(profiles))
            if plan['maximum_worker_launches'] != maximum or sum(
                    p['worker'] for measured in rows.values() for p in measured['processes']) > maximum:
                raise Halt('Comparison exceeded its predeclared worker limits')
    comparisons = {}
    for profile in profiles:
        pairs, before, after, before_tokens, after_tokens = {}, [], [], [], []
        critical_obligations = 0
        for case in CASES:
            a, b = rows.get((case, 'released')), rows.get((case, profile))
            if a is None or b is None:
                continue
            keys = ('source_tree', 'task_sha256', 'route_sha256')
            if model_only:
                keys += ('configuration_route_sha256',)
                if value.get('model_environment'):
                    keys += ('model_route_sha256',)
                    if case != 'security-sensitive':
                        keys += ('release_model_route_sha256',)
            matched = all(a['scenario'][k] and a['scenario'][k] == b['scenario'][k] for k in keys)
            if real:
                matched = matched and verified_settings(a) and verified_settings(b)
            decisions = judgments.get(case, {}).get(profile)
            quality, reason = quality_for(case, a, b, decisions, boundary=boundary)
            if (quality == 'met' and saved_route and case == plan['critical_case'] and
                    not decisions['current']['critical_expected']):
                quality, reason = 'inconclusive', 'Predeclared draft critical case lacks defect adjudication'
            if not matched and quality != 'missed' and not (a.get('infrastructure_gaps') or b.get('infrastructure_gaps')):
                quality, reason = 'inconclusive', 'Source, task, route or requested model settings could not be matched'
            if quality == 'met':
                critical_obligations += decisions['current']['critical_expected']
            a_time, b_time = (v['outcome'].get('delivery_seconds') for v in (a, b))
            a_tokens, b_tokens = normalized_tokens(a), normalized_tokens(b)
            a_model, b_model = ((v['scenario']['settings'] or {}).get('model') for v in (a, b))
            successful = (matched and case != 'security-sensitive' and
                          a['outcome'].get('observed') == b['outcome'].get('observed') == expected_delivery(case, boundary))
            if successful and case in ROUTINE and a_time is not None and b_time is not None:
                before.append(a_time)
                after.append(b_time)
            if successful and a_model and a_model == b_model and a_tokens is not None and b_tokens is not None:
                before_tokens.append(a_tokens)
                after_tokens.append(b_tokens)
            pairs[case] = {'matched': matched, 'quality': quality, 'reason': reason, 'assessment': decisions,
                           'delivery_seconds_delta': b_time - a_time if a_time is not None and b_time is not None else None,
                           'baseline': a, 'candidate': b}
        full = set(pairs) == set(CASES)
        quality = ('missed' if any(x['quality'] == 'missed' for x in pairs.values()) else
                   'met' if real and full and critical_obligations > 0
                   and all(x['quality'] == 'met' for x in pairs.values()) else 'inconclusive')
        medians = [statistics.median(items) if items else None for items in (before, after)]
        latency = reduction(*medians) if before else None
        token_saving = reduction(sum(before_tokens), sum(after_tokens)) if before_tokens else None
        targets = {}
        for key, observed, count, needed in (
                ('routine_median_latency', latency, len(before), len(ROUTINE)),
                ('comparable_token_usage', token_saving, len(before_tokens), len(CASES) - 1)):
            threshold = TARGETS[key]
            targets[key] = {'target_reduction': threshold, 'observed_reduction': observed, 'sample_size': count,
                            'status': ('met' if observed >= threshold else 'missed')
                            if real and count >= needed and observed is not None else 'inconclusive'}
        if boundary == DRAFT_BOUNDARY:
            # The product goals extend through publication and customer install.
            draft_targets = targets
            targets = {k: {**v, 'observed_reduction': None, 'sample_size': 0, 'status': 'inconclusive'}
                       for k, v in draft_targets.items()}
        else:
            draft_targets = targets
        consistent_settings = all(len({json.dumps(p[arm]['scenario']['settings'], sort_keys=True)
                                       for p in pairs.values()}) == 1 for arm in ('baseline', 'candidate'))
        qualified = quality == 'met' and consistent_settings and all(v['status'] == 'met' for v in targets.values())
        intended = None
        if objective is not None:
            selected_target = draft_targets[objective['metric']]
            observed = selected_target['observed_reduction']
            measured = selected_target['status'] != 'inconclusive'
            intended = {**objective, 'observed_reduction': observed,
                        'status': ('met' if observed >= objective['minimum_reduction'] else 'missed')
                                  if measured and observed is not None else 'inconclusive'}
            # This selects a model's intended measured benefit. The independent
            # whole-delivery 30%/20% product targets above remain unchanged.
            qualified = quality == 'met' and consistent_settings and intended['status'] == 'met'
        recommendation = pairs[CASES[0]]['candidate']['scenario']['settings'] if model_only and qualified else None
        comparisons[profile] = {'quality': quality, 'pairs': pairs, 'targets': targets,
                                **({'draft_targets': draft_targets} if boundary == DRAFT_BOUNDARY else {}),
                                'configuration_route_sha256s': sorted({
                                    p['candidate']['scenario']['model_route_sha256'] for p in pairs.values()
                                    if p['matched'] and p['candidate']['scenario'].get('model_route_sha256')}),
                                'routine_median_seconds': {'baseline': medians[0], 'candidate': medians[1]},
                                'comparable_tokens': {'baseline': sum(before_tokens), 'candidate': sum(after_tokens)}
                                if before_tokens else None,
                                'slower_cases': [case for case, p in pairs.items() if (p['delivery_seconds_delta'] or 0) > 0],
                                'qualified': qualified, 'model_objective': intended,
                                'recommended_model_settings': recommendation}
    return {'version': 1, 'input': reference(path), 'baseline_commit': BASELINE, 'execution': value['execution'],
            'completion_boundary': boundary,
            'runtimes': value['runtimes'], 'arm_runtimes': expected_bindings, 'comparison_kind': kind,
            'model_environment': value.get('model_environment'), 'plan': value.get('plan'),
            'model_objective': objective,
            **({'baseline_release_evidence': 'v0.1.17 with fixture-supplied private receipt bindings'}
               if legacy_bindings else {}),
            'profiles': comparisons, 'evaluation_wall_seconds': value['evaluation_wall_seconds'],
            'human_intervention_basis': 'Actual intervention is unknown absent independent assessment.',
            'money': None, 'automatic_profile_change': False,
            'model_attribution': ('Same current runtime and route; only requested model settings differ.' if model_only
                                  else 'Different SFC runtimes confound model-only attribution; no model profile selected.'),
            'fallback': 'Keep the existing supported settings unless a separately authorized profile change is made.',
            'evaluation_overhead': 'Setup, before-defect oracle, source snapshots and final report reconstruction are separate. '
                                   'Per-arm Ready to verified completion uses the same external fixture boundary.',
            'scope': 'Bounded local temporary repositories and fake GitHub. No customer upload latency or billing.'}
