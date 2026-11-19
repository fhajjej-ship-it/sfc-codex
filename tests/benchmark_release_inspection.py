#!/usr/bin/env python3
"""Compare sequential and overlapping release assessment on generated packages."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import time
from unittest.mock import patch
from uuid import uuid4

from benchmark_delivery import (ARCHIVE, CASES, HARNESS_ROOT, PACK, PREFLIGHT, SMOKE, SOURCE_CHECKS, AdapterTrace,
                                Board, Destination, Workers, fixture_commit, fixture_repo, is_worker, setup_package)
from benchmark_package_workload import copy_package_workload, load_package_workload, prepare_package_workload
from sfc_lib import lifeline, review_inspection
from sfc_lib.common import Halt, now, read_json, run, validate_config, write_json
from sfc_lib.factory import Factory
from sfc_lib.fixture_trust import fixture_trust_override
from sfc_lib.profile_qualification import model_environment
from sfc_lib.releases import Releases
from sfc_lib.review_benchmark import INSPECTION_CRITERIA, evaluate_inspection, inspection_binding, inspection_policy
from sfc_lib.review_policy import criteria, digest, exec_provenance, model_settings, runtime_binding
from sfc_lib.verification import checksum
from test_factory import issue
from test_releases import config


PROBES = (
    ('install-clean', 'clean', None),
    ('incorrect-version', 'failed-check', 'installed-version-mismatch'),
    ('obsolete-install-command', 'retained-real-finding', 'obsolete-install-command'),
)
MAX_LAUNCHES = 4
WORKER_TIMEOUT = 120


def ref(path):
    path = Path(path)
    return {'path': str(path), 'sha256': checksum(path)}


def fixture_config(settings, package_version=None, *, worker_timeout=WORKER_TIMEOUT):
    result = config()
    result.update(verify_commands=list(SOURCE_CHECKS), worker_timeout=worker_timeout, verify_timeout=20,
                  max_workers=2, max_attempts=1, max_cycles=8,
                  review_policy={'mode': 'baseline', 'inspection': {'enabled': ['release']}})
    if settings is not None:
        fields = ('model', 'reasoning_effort', 'service_tier')
        if not isinstance(settings, dict) or set(settings) != set(fields) or any(
                not isinstance(settings[k], str) or not settings[k].strip() for k in fields):
            raise Halt('Provide explicit model, reasoning_effort and service_tier for this comparison')
        result.update(settings)
    result['release'].update(package_commands=[PACK], preflight_commands=[PREFLIGHT], verify_commands=[SMOKE],
                             artifacts=[ARCHIVE], public_docs={'Installation': 'INSTALL.md'})
    if package_version:
        archive = f'sfc-{package_version}.zip'
        checksum_file = archive + '.sha256'
        result['verify_timeout'] = 90
        result['release']['package_commands'].append(
            f'mkdir -p sfc-runtime/dist && cp sfc-runtime/qualification-input/{archive} '
            f'sfc-runtime/qualification-input/{checksum_file} sfc-runtime/dist/')
        result['release']['verify_commands'].append('python3 sfc-runtime/scripts/verify_release_package.py')
        result['release']['artifacts'].extend(f'sfc-runtime/dist/{name}' for name in (archive, checksum_file))
    return validate_config(result)


def fixture_source(folder, kind, workload):
    case = CASES[0]
    root = fixture_repo(folder)
    (root / case['path']).write_text(case['before'])
    setup_package(root, case)
    if workload:
        frozen = copy_package_workload(workload, root / 'sfc-runtime')
        with (root / '.gitignore').open('a') as ignore:
            ignore.write('/sfc-runtime/dist/\n')
        with (root / 'INSTALL.md').open('a') as guide:
            guide.write(f'\nThe accompanying `sfc-{frozen["version"]}.zip` contains a separately versioned '
                        'SFC plugin. Its included README describes checksum and installation steps.\n')
        with (root / 'AGENTS.md').open('a') as instructions:
            instructions.write('\nThe bundled SFC plugin is an unchanged, separately versioned archive under '
                               '`sfc-runtime/`. Its artifact check verifies exact archive bytes and runs '
                               'installation tests from a fresh extraction in temporary directories. '
                               'The release version identifies the delivery fixture.\n')
    base = fixture_commit(root, 'Published release boundary')
    (root / case['path']).write_text(case['after'])
    if kind == 'failed-check':
        (root / 'VERSION').write_text('0.9.0\n')
    elif kind == 'retained-real-finding':
        guide = root / 'INSTALL.md'
        guide.write_text(guide.read_text().replace('delivery_cli.py', 'delivery.py'))
    return case, root, base, fixture_commit(root, case['title'])


class InspectionWorkers(Workers):
    def __init__(self, *args, kind, **kwargs):
        super().__init__(*args, **kwargs)
        self.kind = kind

    def _execute(self, argv, cwd, log, timeout, prompt=None, **kwargs):
        code = super()._execute(argv, cwd, log, timeout, prompt, **kwargs)
        if self.real or not is_worker(argv, kwargs):
            return code
        output = Path(argv[argv.index('--output-last-message') + 1])
        lane = Path(log).parent.name.split('-', 1)[0]
        value = read_json(output)
        if self.kind == 'retained-real-finding' and lane in ('inspection', 'qa'):
            value.update(verdict='fail', findings=[{'file': 'INSTALL.md', 'line': 1, 'severity': 'high',
                'message': 'The documented delivery.py command is absent from the archive.'}])
        if Path(argv[argv.index('--output-schema') + 1]).name == 'release-review-result.json':
            state = read_json(self.fixture_root / '.sfc/state.json')
            descriptor = state['cards']['2']['release_evidence']['provisional_qa']
            value['release_qa'] = {
                **{key: descriptor[key] for key in ('inspection_sha256', 'verification_bindings_sha256')},
                'artifact_evidence': 'Fake model adapter uses the current checked fixture archive.',
                'installation_evidence': 'Fake model adapter accepts the completed fixture smoke evidence.',
                'acceptance_evidence': 'Fixture route exercise with no independent model quality claim.'}
        write_json(output, value)
        events = [{'type': 'thread.started', 'thread_id': 'fake-fixture-' + uuid4().hex},
                  {'type': 'turn.started'},
                  {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': json.dumps(value)}},
                  {'type': 'turn.completed'}]
        Path(log).write_text('Fake Codex adapter\n' + ''.join(json.dumps(v) + '\n' for v in events))
        return code


def run_arm(folder, kind, variant, config, *, real=False, workload=None):
    case, root, base, head = fixture_source(folder, kind, workload)
    work = issue(1, title=case['title'], state='CLOSED', status='Done')
    board = Board(root, [work])
    trace = AdapterTrace(folder)
    workers = InspectionWorkers(root / '.sfc/stop', case, real, trace, kind=kind, worker_cap=MAX_LAUNCHES,
                                worker_timeout=config['worker_timeout'], fixture_root=root,
                                fixture_trust_override=fixture_trust_override(root) if real else None)
    if real:
        workers.lifetime = True
    factory = Factory(root, config, board, workers)
    remote = Destination(factory)
    remote.seed_previous(base, case['title'])
    release = Releases(factory, remote)

    def create(*args, **kwargs):
        board.items.append(issue(2, title=kwargs['title'], body=kwargs['body'], status='Backlog', labels=['maintenance']))
        return {'issue': {'number': 2}}

    original_qualified = review_inspection.qualified
    original_history = review_inspection.representative_history

    def qualified(current, profile, **scope):
        # This evaluation dispatch exists only for the isolated, generated
        # comparison. It neither creates nor installs a qualification receipt.
        if current is factory and profile == 'release':
            return variant == 'candidate'
        return original_qualified(current, profile, **scope)

    def history(current, profile):
        if current is factory and profile == 'release':
            return True  # Eligibility is measured independently before real comparison dispatch.
        return original_history(current, profile)

    def version(argv, *args, **kwargs):
        if not real and argv[:2] == ['codex', '--version']:
            return subprocess.CompletedProcess(argv, 0, 'FAKE release benchmark adapter', '')
        return run(argv, *args, **kwargs)

    with patch('sfc_lib.priority.create_ticket', side_effect=create):
        request = release.request('1.0.0')
    card = board.items[-1]
    card['status'] = 'Ready'
    pinned_card = dict(card)
    failure = None
    assessed_at = None
    assess = Releases.assess
    assess_package = Releases.assess_package

    def assess_timed(workflow, item, lane, area, boundary):
        nonlocal assessed_at
        result = assess(workflow, item, lane, area, boundary)
        if assessed_at is None and (not result or lane == 'review'):
            assessed_at = time.monotonic()
        return result

    def assess_package_timed(workflow, item, area, boundary):
        nonlocal assessed_at
        result = assess_package(workflow, item, area, boundary)
        if result is not None:
            assessed_at = time.monotonic()
        return result

    started = time.monotonic()
    with patch('sfc_lib.releases.ReleaseGitHub', return_value=remote), \
            patch('sfc_lib.factory.run', side_effect=version), \
            patch('sfc_lib.review_policy.run', side_effect=version), \
            patch.object(lifeline, 'spawn', trace.spawn), \
            patch.object(Releases, 'assess', assess_timed), \
            patch.object(Releases, 'assess_package', assess_package_timed), \
            patch.object(review_inspection, 'representative_history', side_effect=history), \
            patch.object(review_inspection, 'qualified', side_effect=qualified):
        try:
            factory.run(allow_merge=True)
        except (Halt, OSError, ValueError) as exc:
            failure = {'type': type(exc).__name__, 'message': str(exc)}
    elapsed = (assessed_at or time.monotonic()) - started
    record = factory.record(card)
    journal = read_json(request['receipt'])
    receipts = [record['receipts'][lane] for lane in ('qa', 'security', 'review')
                if lane in record.get('receipts', {})]
    # A passing parallel peer must never obscure a decisive failure.
    decisive = next((row for row in receipts if row['result']['verdict'] != 'pass'),
                    receipts[-1] if receipts else None)
    refs = [ref(Path(row['result_path']).with_name('receipt.json')) for row in receipts if row is not decisive]
    checks = record.get('verification')
    rejection = record.get('inspection_rejection')
    outcome = ('checks-failed' if checks and not checks['passed'] and not receipts else
               'inspection-fail' if rejection and failure is None else
               decisive['result']['verdict'] if decisive and failure is None else 'failed')
    evidence = record.get('release_evidence')
    package_folder = Path(journal['candidate_area']).parent / 'artifacts'
    snapshot = {k: journal[k] for k in ('version', 'boundary', 'candidate', 'changes', 'identity',
                                       'card', 'config_digest', 'public_notes', 'latest_plan')}
    snapshot['artifacts'] = [{'path': str(package_folder / row['name']), **row} for row in journal['artifacts']]
    measured = {'config': config, 'checks': checks,
                'assessment': ref(Path(decisive['result_path']).with_name('receipt.json')) if decisive else None,
                'specialists': refs, 'inspection': next(iter(record.get('review_inspections', [])), None),
                'outcome': outcome, 'elapsed_seconds': elapsed,
                **({'rejection': ref(Path(rejection['receipt_path']))} if rejection else {})}
    measurement = folder / 'inspection-measurement.json'
    write_json(measurement, measured)
    return {'head': head, 'base': base, 'card': pinned_card, 'worktree': journal['candidate_area'],
            'release_snapshot': snapshot, 'release_evidence': evidence, 'release_journal': ref(request['receipt']),
            'remote_writes': list(remote.writes), 'worker_launches': len(workers.lanes),
            'failure': failure, 'adapter_trace': ref(trace.path) if trace.path.is_file() else None,
            'measurement': ref(measurement), 'adjudications': [], 'coverage_adjudication': {}, **measured}


def release_inspection_benchmark(output, settings=None, *, sfc_workload=False, prepared_workload=None,
                                 worker_timeout=WORKER_TIMEOUT):
    real = settings is not None
    if real and not lifeline.supported():
        raise Halt('The bounded real comparison requires supported macOS child cleanup')
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    workload = (prepare_package_workload(output / 'sfc-workload', HARNESS_ROOT) if sfc_workload else
                Path(prepared_workload).resolve() if prepared_workload else None)
    material = load_package_workload(workload) if workload else None
    if type(worker_timeout) is not int or not 1 <= worker_timeout <= 180:
        raise Halt('Release trial worker limit must be between 1 and 180 seconds')
    config = fixture_config(settings, material['version'] if material else None, worker_timeout=worker_timeout)
    model = model_settings(config)
    report = {'criteria': INSPECTION_CRITERIA, 'execution': 'real-model' if real else 'offline-fake-adapters',
              'binding': inspection_binding(config), 'target_policy': inspection_policy(config),
              'target_config': config, 'model_settings': model,
              'cli_version': exec_provenance(None)['version'] if real else 'FAKE release benchmark adapter',
              'runtime_sha256': runtime_binding(), 'independent_adjudicator': None,
              'profiles': {'release': []}}
    plan = output / 'comparison-plan.json'
    write_json(plan, {'time': now(), 'profile': 'release', 'cases': PROBES, 'settings': model,
                      'worker_timeout_seconds': worker_timeout, 'max_worker_launches_per_arm': MAX_LAUNCHES,
                      'max_worker_launches': 2 * len(PROBES) * MAX_LAUNCHES, 'binding': report['binding'],
                      'runtime_sha256': report['runtime_sha256'], 'comparator': ref(Path(__file__).resolve()),
                      'workload': ref(workload) if workload else None})
    report['plan'] = ref(plan)
    report['workload'] = ref(workload) if workload else None
    if real:
        pilot = run_arm(output / 'duration-pilot', 'clean', 'baseline', config, workload=workload)
        artifact = (pilot['release_evidence'] or {}).get('artifact_checks', {})
        duration = (pilot['checks'] or {}).get('elapsed_seconds', 0) + artifact.get('elapsed_seconds', 0)
        report['duration_pilot'] = {'check_seconds': duration, 'evidence': pilot, 'actual_model_launches': 0}
        minimum = INSPECTION_CRITERIA['minimum_check_seconds']
        if pilot['outcome'] != 'pass' or duration < minimum:
            decision = {'qualified': [], 'failures': [
                f'Release duration pilot did not pass with at least {minimum} seconds of qualifying checks. '
                'No actual model was launched.'], 'metrics': {}, 'credits': None}
            write_json(output / 'comparison.json', report)
            write_json(output / 'decision.json', decision)
            return decision
        report['model_environment'] = model_environment(HARNESS_ROOT)
    for index, (name, kind, expected) in enumerate(PROBES):
        order = ('baseline', 'candidate') if index % 2 == 0 else ('candidate', 'baseline')
        arms = {}
        for variant in order:
            folder = output / f'case-{index + 1}' / variant
            try:
                arms[variant] = run_arm(folder, kind, variant, config, real=real, workload=workload)
            except (Halt, OSError, ValueError, KeyError) as exc:
                folder.mkdir(parents=True, exist_ok=True)
                failure = {'error': type(exc).__name__, 'detail': str(exc), 'evidence_root': str(folder)}
                write_json(folder / 'comparison-failure.json', failure)
                arms[variant] = failure
            if real and model_environment(HARNESS_ROOT) != report['model_environment']:
                write_json(output / 'environment-drift.json', {'case': name, 'arm': variant, 'result': arms[variant]})
                raise Halt('Runtime or model environment changed during the release comparison; retained original work')
        baseline = arms['baseline']
        pair = {'case': name, 'kind': kind, 'expected': expected, **arms}
        if 'head' in baseline:
            card = baseline['card']
            pair.update(head=baseline['head'], base=baseline['base'], card=card, worktree=baseline['worktree'],
                        task_digest=Factory.task_digest(card), obligations=[row['id'] for row in criteria(card)],
                        obligations_sha256=digest(baseline['release_snapshot']))
        report['profiles']['release'].append(pair)
        write_json(output / 'comparison.json', report)
    decision = evaluate_inspection(report)
    write_json(output / 'decision.json', decision)
    return decision


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--real-model-config', type=Path)
    parser.add_argument('--worker-timeout', type=int, default=WORKER_TIMEOUT,
                        help='Equal finite worker timeout in both comparison arms (maximum 180 s)')
    workload = parser.add_mutually_exclusive_group()
    workload.add_argument('--sfc-workload', action='store_true', help='Freeze and verify an actual SFC portable package')
    workload.add_argument('--prepared-sfc-workload', type=Path, help='Reuse an existing frozen SFC workload manifest')
    args = parser.parse_args()
    print(json.dumps(release_inspection_benchmark(
        args.output, read_json(args.real_model_config) if args.real_model_config else None,
        sfc_workload=args.sfc_workload, prepared_workload=args.prepared_sfc_workload,
        worker_timeout=args.worker_timeout), indent=2))
