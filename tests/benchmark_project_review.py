"""Compare review routes using pinned local project source and normal checks."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from benchmark_review import real_arm
from sfc_lib import efficiency, review_policy
from sfc_lib.common import Halt, common_dir, git, load_config, read_json, validate_config, write_json
from sfc_lib.factory import Factory
from sfc_lib.project_review_benchmark import (
    evidence_ref, isolated_config, load_trial, measurement_limits, read_trial_manifest, sample_cards, validate_sample,
)
from sfc_lib.review_benchmark import CRITERIA, evaluate
from sfc_lib.verification import checksum, immutable, index_directory


def reference(path):
    return {'path': str(path), 'sha256': checksum(path)}


def read_manifest(path, value=None):
    path = Path(path).resolve()
    if value is None:
        value = read_trial_manifest(path)
    source = Path(value['source_root']).resolve(strict=True)
    if (source / '.codex/config.toml').exists():
        raise ValueError('Project comparison does not load checkout-local Codex configuration')
    target = validate_config(read_json(evidence_ref(path.parent, value['target_config'])['path']))
    baseline = load_config(source)
    policy = target.get('review_policy') or {}
    if ('review_policy' in baseline or {k: v for k, v in target.items() if k != 'review_policy'} != baseline or
            not policy.get('behavioral_contract') or
            set(policy) & {'promotion', 'imports', 'routine_candidate', 'inspection', 'github_reviewer_ids'} or
            not review_policy.explicit_review_settings(target)):
        raise ValueError('Target must match the saved project config plus an eligible behavioral review policy')
    cards = sample_cards(value['cases'], target['repo'])
    measurement_limits(value, target)
    for row in value['cases']:
        oracle = evidence_ref(path.parent, row['oracle'])
        if Path(oracle['path']).is_relative_to(source):
            raise ValueError('Held-out oracles must be outside the source repository')
        row['oracle'] = oracle
        compile(Path(oracle['path']).read_bytes(), oracle['path'], 'exec')
        if (any(git(source, 'rev-parse', row[side] + '^{commit}') != row[side] for side in ('base', 'head')) or
                git(source, 'merge-base', row['base'], row['head']) != row['base'] or
                not git(source, 'diff', '--no-ext-diff', '--name-only', row['base'], row['head'])):
            raise ValueError('Expected pinned project commits with a nonempty descendant change')
    return source, baseline, target, value['cases'], cards


def prepare_oracle(folder, root, commit, oracle, expected):
    review_policy.native_source(root, commit, commit)
    log = folder / (root.name + '-oracle.log')
    with log.open('xb') as handle:
        result = subprocess.run([sys.executable, '-B', '-c', Path(oracle['path']).read_text()], cwd=root,
                                stdout=handle, stderr=subprocess.STDOUT, timeout=30)
    review_policy.native_source(root, commit, commit)
    if result.returncode != expected or (expected and 'AssertionError' not in log.read_text(errors='replace')):
        raise ValueError('Held-out ' + root.name + ' oracle did not reproduce the declared outcome: ' + str(log))
    return {'exit_code': result.returncode, 'log': reference(log)}


def guard_preflight(path, worktree, output):
    path = Path(path).resolve(strict=True)
    sha256 = checksum(path)

    def decision(command):
        event = {'cwd': str(worktree), 'tool_name': 'exec_command', 'tool_input': {'command': command}}
        result = subprocess.run([sys.executable, '-B', str(path)], input=json.dumps(event), cwd=worktree,
                                check=True, capture_output=True, text=True, timeout=10)
        return json.loads(result.stdout)['hookSpecificOutput'] if result.stdout.strip() else {}

    read = decision('git diff --stat')
    protected = decision('git push --force origin main')  # Hook input only. No push runs.
    if (read.get('permissionDecision') not in (None, 'allow') or protected.get('permissionDecision') != 'deny' or
            protected.get('permissionDecisionReason') != 'Force or broad Git push blocked' or checksum(path) != sha256):
        raise ValueError('SFC guard did not allow an ordinary read and deny the protected push probe')
    write_json(output / 'guard-preflight.json', {'path': str(path), 'sha256': sha256, 'worktree': str(worktree),
               'read_allowed': True, 'protected_push_denied': True})


def prepare(manifest, output, *, guard=None):
    manifest = Path(manifest).resolve()
    manifest_ref = reference(manifest)
    declaration = read_trial_manifest(manifest)
    source, baseline, target, cases, cards = read_manifest(manifest, declaration)
    measured = 'measurement_limits' in declaration
    target_common = common_dir(source).resolve()
    output = Path(output).resolve()
    if output.is_relative_to(source) or output.is_relative_to(target_common):
        raise ValueError('Project comparison output must be outside the source checkout and Git common directory')
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    main = output / 'repository'
    git(output, 'init', '--object-format=' + git(source, 'rev-parse', '--show-object-format'), str(main))
    common = common_dir(main)
    commits = sorted({row[side] for row in cases for side in ('base', 'head')})
    git(main, 'fetch', '--no-tags', '--no-auto-maintenance', str(source), *commits)
    git(main, 'checkout', '--detach', cases[0]['base'])
    exclude = common / 'info/exclude'
    exclude.parent.mkdir(parents=True, exist_ok=True)
    with exclude.open('a') as handle:
        handle.write('\n.sfc/\n')
    trial = {'version': 2 if measured else 1, 'target_config': target, 'source_common': str(target_common),
             'isolated_common': str(common), 'cases': cases, 'limits': measurement_limits(declaration, target),
             'runtime_sha256': review_policy.runtime_binding(), 'cli_version': review_policy.exec_provenance(None)['version']}
    if measured:
        trial['manifest'] = manifest_ref
    config = isolated_config(target, target_common, common)
    write_json(main / '.sfc/config.json', config)
    samples = []
    for number, row in enumerate(cases, 1):
        name = row['case']
        folder = output / 'worktrees' / str(number)
        folder.mkdir(parents=True)
        oracle_folder = output / 'oracles' / str(number)
        oracle_folder.mkdir(parents=True)
        oracle = oracle_folder / 'held-out.py'
        oracle.write_bytes(Path(evidence_ref('.', row['oracle'])['path']).read_bytes())
        if checksum(oracle) != row['oracle']['sha256']:
            raise ValueError('Held-out oracle changed during preparation')
        row['oracle'] = reference(oracle)
        observations = {}
        for side in ('base', 'head'):
            checkout = folder / side
            git(main, 'worktree', 'add', '--detach', checkout, row[side])
            if (checkout / '.codex').exists() or (checkout / '.codex').is_symlink():
                raise ValueError('Isolated review comparison does not load project-local Codex settings')
            expected = int(side == 'head' and row['kind'] != 'clean')
            observations[side] = prepare_oracle(oracle_folder, checkout, row[side], row['oracle'], expected)
        card = cards[name]
        sample = {k: row[k] for k in ('case', 'kind', 'base', 'head', 'expected')}
        sample.update(card=card, worktree=str(folder / 'head'), task_digest=Factory.task_digest(card),
                      criteria=[v['id'] for v in review_policy.criteria(card)])
        sample['project_oracle'] = immutable(oracle_folder, {
            'version': 1, 'kind': 'project-review-oracle', **{k: sample[k] for k in ('case', 'base', 'head', 'task_digest')},
            'oracle': row['oracle'], 'runs': observations})
        validate_sample(sample, trial)
        samples.append(sample)
    if load_config(source) != baseline:
        raise ValueError('Normal project configuration changed during trial preparation')
    efficiency.install_runtimes(config, root=Path(samples[0]['worktree']))
    guard_preflight(guard or Path(__file__).resolve().parents[1] / 'hooks/guard.py',
                    Path(samples[0]['worktree']), output)
    path = output / 'project-plan.json'
    write_json(path, trial)
    ref = reference(path)
    load_trial(ref, target)
    return target, trial, ref, samples


def benchmark(manifest, output, *, prepare_only=False, guard=None):
    output = Path(output).resolve()
    target, trial, ref, samples = prepare(manifest, output, guard=guard)
    if prepare_only:
        return {'project_trial': ref, 'cases': len(samples), 'model_dispatches': 0}
    settings, policy = review_policy.model_settings(target), target['review_policy']
    remaining = trial['limits']['maximum_model_dispatches']
    reservations = output / 'model-reservations.jsonl'
    reservations.touch(exist_ok=False)

    def validate_pins():
        if load_trial(ref, target) != trial:
            raise Halt('Project comparison measurement plan changed')
        if (review_policy.runtime_binding() != trial['runtime_sha256'] or
                review_policy.exec_provenance(None)['version'] != trial['cli_version']):
            raise Halt('Project comparison worker runtime or Codex CLI changed')

    def reserve(variant, lane):
        nonlocal remaining
        validate_pins()
        if remaining <= 0:
            raise Halt('Project comparison reached its finite model dispatch cap')
        remaining -= 1
        with reservations.open('a') as handle:
            handle.write(json.dumps({'case': sample['case'], 'variant': variant, 'lane': lane}) + '\n')
            handle.flush()
            os.fsync(handle.fileno())

    for index, sample in enumerate(samples):
        order = ('baseline', 'candidate') if index % 2 == 0 else ('candidate', 'baseline')
        for variant in order:
            folder = output / 'arms' / str(index + 1) / variant
            root = Path(sample['worktree'])
            started = time.monotonic()
            try:
                sample[variant] = real_arm(root, folder, sample['card'], sample['base'], variant,
                    settings, policy, before_dispatch=reserve, target_config=target, project_plan=trial)
            except (OSError, RuntimeError, ValueError) as exc:
                folder.mkdir(parents=True, exist_ok=True)
                sample[variant] = {'error': type(exc).__name__, 'detail': str(exc), 'evidence_root': str(folder),
                                   'end_to_end_seconds': time.monotonic() - started}
                write_json(folder / 'comparison-failure.json', sample[variant])
            finally:
                # Run the same commands freshly in each arm. Preserve indexes
                # outside the live lookup path so arm order cannot grant reuse.
                check_index = index_directory(root)
                if check_index.exists():
                    check_index.rename(folder / 'verification-index')
            write_json(output / 'comparison-progress.json', {'project_trial': ref, 'samples': samples, 'complete': False,
                       'model_reservations': trial['limits']['maximum_model_dispatches'] - remaining})
            validate_pins()
    report = {'criteria': CRITERIA, 'execution': 'real-model', 'policy_sha256': review_policy.promotion_binding(target),
              'target_policy': policy, 'target_config': target, 'model_settings': settings, 'cli_version': trial['cli_version'],
              'runtime_sha256': trial['runtime_sha256'], 'project_trial': ref, 'samples': samples,
              'model_reservations': reference(reservations),
              'independent_adjudicator': None}
    write_json(output / 'comparison.json', report)
    decision = evaluate(report)
    write_json(output / 'decision.json', decision)
    return decision


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--prepare-only', action='store_true', help='Validate pinned source and run oracles with no model calls')
    parser.add_argument('--guard', type=Path, help='Probe this installed SFC guard before any model calls')
    args = parser.parse_args()
    try:
        print(json.dumps(benchmark(args.manifest, args.output, prepare_only=args.prepare_only, guard=args.guard), indent=2))
    except (OSError, RuntimeError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as exc:
        raise SystemExit(str(exc)) from exc
