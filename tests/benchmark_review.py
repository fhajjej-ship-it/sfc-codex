"""Bounded paired review evaluation in temporary Git repositories.

The default uses executable oracles and cannot promote. An explicit
--real-model-config runs the real providers and retains a draft for independent
adjudication. Neither mode uses a GitHub board.
"""
from __future__ import annotations

import argparse
import json
import os
import select
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from sfc_lib.common import Halt, git, read_json, validate_config, write_json
from sfc_lib.factory import Factory
from sfc_lib.review_benchmark import (
    CRITERIA, EXECUTION_LIMITS, FIXTURE_CHECKS, INSPECTION_CRITERIA, arm_config, arm_measurement, evaluate,
    evaluate_inspection, inspection_binding, inspection_policy, run_arm,
    SOURCE_INSPECTION_MAX_DISPATCHES, SOURCE_INSPECTION_MAX_WORKER_SECONDS,
)
from sfc_lib.review_policy import comparison_policy, criteria, exec_provenance, explicit_review_settings, model_settings, promotion_binding, runtime_binding
from sfc_lib.verification import checksum, digest, immutable

LOOKUP = 'def lookup(rows, key):\n    """Return None for a missing record."""\n    return rows.get(key)\n'
CHOOSE = ('def choose(prior, policy):\n    """Reject never when there is no prior Latest."""\n'
          '    if policy == "never" and prior is None:\n        return "reject"\n'
          '    return policy != "never"\n')
CASES = [
    # Retained #90 QA finding: first stable + never cannot preserve an absent
    # GitHub Latest. Existing production regression: tests/test_releases.py,
    # test_first_stable_never_rejected_before_creation_or_packaging.
    ('first-stable-never', 'retained-real-finding', 'first-stable-never',
     CHOOSE, CHOOSE.replace('    if policy == "never" and prior is None:\n        return "reject"\n', ''),
     'assert choose(None, "never") == "reject"',
     'assert choose("existing", "never") is False',
     'With an existing Latest, the never policy returns False.'),
    ('deleted-record', 'seeded-regression', 'missing-record', LOOKUP,
     LOOKUP.replace('rows.get(key)', 'rows[key]'), 'assert lookup({}, "gone") is None',
     'assert lookup({"a": 42}, "a") == 42', 'An existing record returns its stored value.'),
    ('clean-record', 'clean', None, LOOKUP, LOOKUP.replace('rows.get(key)', 'rows.get(key, None)'),
     'assert lookup({}, "gone") is None', 'assert lookup({"a": 42}, "a") == 42',
     'An existing record returns its stored value.'),
]


TRIM_LOOKUP = '''def lookup(rows, key):
    """Try a trimmed built-in string key for plain dicts."""
    if type(key) is str and type(rows) is dict:
        missing = object()
        value = rows.get(key, missing)
        if value is not missing:
            return value
        trimmed = key.strip()
        if trimmed != key:
            return rows.get(trimmed)
        return None
    return rows.get(key)
'''
DOUBLE_LOOKUP = TRIM_LOOKUP.replace(
    '    return rows.get(key)\n', '    rows.get(key)\n    return rows.get(key)\n')
TRIM_CHECKS = '''
assert lookup({"a": 42}, "a") == 42
assert lookup({}, "gone") is None
assert lookup({"a": 42}, " a ") == 42
assert lookup({"a": 42, " a ": 99}, " a ") == 99
assert lookup({"a": 42, " a ": None}, " a ") is None
assert lookup({1: 42}, 1) == 42 and lookup({}, 1) is None
'''
TRIM_CRITERION = ('For a plain dict, a padded built-in string key falls back to the trimmed key only when '
                  'no exact match exists. Custom getters retain exact lookup.')
GETTER_ORACLE = '''
class KeyOnlyRows(dict):
    def get(self, key):
        return super().get(key)

rows = KeyOnlyRows({"plain": 42})
assert lookup(rows, "plain") == 42
assert lookup(rows, "missing") is None

class NoMembershipRows(KeyOnlyRows):
    def __contains__(self, key):
        raise AssertionError("custom getters do not require membership")

rows = NoMembershipRows({" plain ": None, "plain": 77})
assert lookup(rows, " plain ") is None
assert lookup(NoMembershipRows({" plain ": 42}), " plain ") == 42

class ExactGetter:
    def get(self, key):
        return 42 if key == " plain " else None

assert lookup(ExactGetter(), " plain ") == 42
assert lookup(ExactGetter(), "plain") is None

class StatefulKey:
    comparisons = 0

    def __hash__(self):
        return hash(" plain ")

    def __eq__(self, other):
        self.comparisons += 1
        return self.comparisons == 1 and other == " plain "

key = StatefulKey()
assert lookup({key: 99, "plain": 77}, " plain ") == 99
assert key.comparisons == 1

import tracemalloc

large_key = " " + "x" * (1024 * 1024) + " "
exact = {large_key: 42}
tracemalloc.start()
try:
    assert lookup(exact, large_key) == 42
    assert tracemalloc.get_traced_memory()[1] < len(large_key) // 2
finally:
    tracemalloc.stop()
'''
LAZY_ORACLE = '''
class LazyRows(dict):
    def __init__(self, values=None):
        super().__init__(values or {})
        self.keys_read = []

    def get(self, key, default=None):
        self.keys_read.append(key)
        result = super().get(key, default)
        self.setdefault(key, 42)
        return result

rows = LazyRows()
assert lookup(rows, "plain") is None
assert rows.keys_read == ["plain"]
'''
TRIM_ORACLE = '''
assert lookup({"plain": 77}, " plain ") == 77
assert lookup({"plain": 77, " plain ": None}, " plain ") is None
rows = LazyRows({"plain": 77})
assert lookup(rows, " plain ") is None
assert rows.keys_read == [" plain "]
rows = LazyRows({"plain": 77, " plain ": None})
assert lookup(rows, " plain ") is None
assert rows.keys_read == [" plain "]
'''

PAIRED_LOOKUP = LOOKUP + '''

def lookup_pair(rows, first, second):
    """Return two exact lookups in the order requested."""
    return lookup(rows, first), lookup(rows, second)
'''
PAIR_CRITERION = ('Return two exact lookup results in order. A missing key returns None. '
                  'Keep the existing single-key lookup behavior.')
PAIR_CHECKS = '''
assert lookup_pair({"a": 42, "b": None}, "a", "b") == (42, None)
assert lookup_pair({"a": 42}, "missing", "a") == (None, 42)
assert lookup_pair({"a": 42}, "a", "a") == (42, 42)

class OrderedRows:
    def __init__(self):
        self.calls = []

    def get(self, key):
        self.calls.append(key)
        if key == "error":
            raise LookupError("original getter error")
        return len(self.calls)

rows = OrderedRows()
assert lookup_pair(rows, "first", "second") == (1, 2)
assert rows.calls == ["first", "second"]
rows = OrderedRows()
try:
    lookup_pair(rows, "error", "second")
except LookupError as error:
    assert str(error) == "original getter error"
else:
    raise AssertionError("expected the first getter error")
assert rows.calls == ["error"]
'''


def behavioral_cases():
    retained, seeded, _ = CASES
    control = ('clean-record', 'clean', None, LOOKUP, TRIM_LOOKUP, GETTER_ORACLE + LAZY_ORACLE + TRIM_ORACLE,
               TRIM_CHECKS, TRIM_CRITERION)
    # Both variants meet the public feature assertion and ordinary project
    # checks. Only the clean one preserves the original lazy mapping behavior.
    double_lookup = ('retained-double-lookup', 'retained-real-finding', 'double-lookup',
                     LOOKUP, DOUBLE_LOOKUP, GETTER_ORACLE + LAZY_ORACLE, TRIM_CHECKS, TRIM_CRITERION)
    return retained, seeded, double_lookup, control


def behavioral_oracle(root, sample, loader, base_program, candidate_program):
    """Check pinned fixture behavior without sharing the oracle with workers."""
    rows = {}
    expected_codes = {'base': 0, 'candidate': 0 if sample['kind'] == 'clean' else 1}
    for arm, program in (('base', base_program), ('candidate', candidate_program)):
        ref = sample['base'] if arm == 'base' else sample['head']
        code = loader(ref) + program
        run = subprocess.run([sys.executable, '-B', '-c', code], cwd=root,
                             capture_output=True, text=True, timeout=10)
        rows[arm] = {'source_sha256': digest(code), 'exit_code': run.returncode,
                     'stdout': run.stdout, 'stderr': run.stderr}
        if run.returncode != expected_codes[arm]:
            raise Halt(f'Behavioral fixture {sample["case"]}: held-out {arm} premise failed')
        exception = 'KeyError: \'gone\'' if sample['case'] == 'deleted-record' else 'AssertionError'
        if expected_codes[arm] and not run.stderr.rstrip().endswith(exception):
            raise Halt(f'Behavioral fixture {sample["case"]}: held-out defect did not fail its assertion')
    return {'kind': 'behavioral-comparison-oracle', 'version': 1,
            **{key: sample[key] for key in ('case', 'head', 'base', 'expected', 'task_digest')},
            'case_kind': sample['kind'], 'observations': rows}


def package_behavior_check(check):
    """Run SFC's actual compatibility suite and exercise the packaged API."""
    return '''from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from zipfile import ZipFile

with tempfile.TemporaryDirectory(prefix='fixture-install-') as tmp:
    root = Path(tmp)
    package = shutil.copytree('package_fixture', root / 'package')
    shutil.copy2('behavior.py', package / 'scripts' / 'behavior.py')
    subprocess.run([sys.executable, '-B', '-m', 'unittest', 'discover', '-s', 'tests',
                    '-p', 'test_packaging.py', '-v'], cwd=package, check=True)
    subprocess.run([sys.executable, '-B', 'scripts/package_plugin.py'], cwd=package, check=True)
    archive, = (package / 'dist').glob('sfc-*.zip')
    with ZipFile(archive) as zipped:
        assert zipped.testzip() is None
        behavior = zipped.read('sfc/scripts/behavior.py')
    exec(compile(behavior, 'sfc/scripts/behavior.py', 'exec'))
    exec(CANDIDATE_CHECKS)
'''.replace('CANDIDATE_CHECKS', repr(check))


def package_workload_files(package_root):
    import importlib.util

    # Keep the executable packaging workload and keep benchmark history out
    # of the source checkout seen by the reviewer.
    spec = importlib.util.spec_from_file_location('fixture_package_plugin',
                                                 Path(__file__).parents[1] / 'scripts/package_plugin.py')
    plugin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plugin)
    files = plugin.contents(Path(package_root).resolve())
    workload = {f'package_fixture/{name}': data for name, data in files.items()
                if not name.startswith(('tests/', 'docs/', 'assets/qualification/'))
                and name not in ('README.md', 'scripts/sfc_lib/review_benchmark.py')
                or name == 'tests/test_packaging.py' or name.startswith('tests/fixtures/pre-native-validator/')}
    if ('package_fixture/tests/test_packaging.py' not in workload or
            'package_fixture/scripts/package_plugin.py' not in workload or
            'package_fixture/scripts/behavior.py' in workload):
        raise Halt('Package workload requires an exact SFC package source inventory')
    return workload


class LocalActivity:
    def observe(self, *_, progress=None, check_counts=None):
        return None

    def report(self, *_, progress=None):
        return None


def attest_from_terminal(root, pins):
    if not sys.stdin.isatty():
        raise Halt('Attestation needs an interactive terminal')
    print(f'Inspect the candidate in {root}.', file=sys.stderr)
    print(f"Compare {pins['base']} to {pins['head']} and check repository use of the changed files.",
          file=sys.stderr)
    print(f"Task digest {pins['task']}. Enter {pins['head']} only if the candidate meets the declared review scope.",
          file=sys.stderr, flush=True)
    ready, _, _ = select.select([sys.stdin], [], [], 300)
    return bool(ready) and sys.stdin.readline().strip() == pins['head']


def bound_trial_processes(factory, limits=EXECUTION_LIMITS):
    """Restrict isolated comparison calls without changing saved config binding."""
    execute = factory.processes.execute

    def bounded(argv, cwd, log, timeout, *args, **kwargs):
        limit = limits['worker_timeout' if argv[0] == 'codex' else 'verify_timeout']
        return execute(argv, cwd, log, min(timeout, limit), *args, **kwargs)

    factory.processes.execute = bounded


def real_arm(root, output, card, base, variant, settings, target_policy, *, attest=None, before_dispatch=None,
             factory_type=Factory, target_config=None, project_plan=None):
    started = time.monotonic()
    attestation = candidate = None
    if attest is not None and variant == 'candidate' and target_policy.get('mode') == 'risk-based':
        tick = time.monotonic()
        pins = {'head': git(root, 'rev-parse', 'HEAD'), 'base': base, 'task': Factory.task_digest(card)}
        accepted = attest(root, pins)
        if type(accepted) is not bool:
            raise Halt('Attestation must record an explicit decision')
        attestation = {'pins': pins, 'accepted': accepted, 'elapsed_seconds': time.monotonic() - tick}
        if accepted:
            candidate = pins
    config = arm_config(variant, settings, target_policy, routine_candidate=candidate, target_config=target_config,
                        project_plan=project_plan)
    factory = factory_type(root, config, github=object())
    measurement = arm_measurement(project_plan)
    bound_trial_processes(factory, measurement['execution_limits'])
    factory.state = {'version': 1, 'cards': {}}
    factory.run_dir = output
    factory.activity = LocalActivity()
    if not factory.verify(card, root, base):
        raise Halt('Evaluation fixture project check failed')
    if project_plan is not None:
        from sfc_lib.project_review_benchmark import validate_fresh_checks
        validate_fresh_checks(config, factory.record(card)['verification'])
    receipts = []

    def dispatch(lane):
        if before_dispatch:
            before_dispatch(variant, lane)
        result = factory.lane(card, lane, root, base)
        receipts.append(factory.record(card)['receipts'][lane])
        return result

    outcome = run_arm(factory, card, root, base, variant, dispatch)
    evidence = {'config': config, 'receipts': [{'path': row['receipt_path'], 'sha256': row['receipt_sha256']}
                                                for row in receipts],
                'outcome': outcome, 'attestation': attestation, **measurement,
                'review_seconds': sum(row['elapsed_seconds'] for row in receipts),
                'end_to_end_seconds': time.monotonic() - started}
    timing = immutable(output, {'version': 4, 'kind': 'review-comparison-arm', **evidence})
    return {**evidence, 'timing': {'path': timing['receipt_path'], 'sha256': timing['receipt_sha256']},
            'adjudications': [], 'coverage': [], 'coverage_adjudication': {}}


def measure_inspection_arm(factory, card, worktree, base, variant, checks, assess, *, release_snapshot=None,
                           inspection_role=None, execution_limits=None, measurement_plan_sha256=None,
                           reserved_lanes=None):
    """Fixture-only helper; callbacks execute the supervisor check and lane paths."""
    from sfc_lib.review_inspection import overlap, rejection
    if type(factory.gh) is not object or not isinstance(factory.activity, LocalActivity):
        raise Halt('Inspection comparison requires a temporary fixture adapter')
    record = factory.record(card)
    if record.get('verification') or record.get('receipts') or record.get('review_inspections'):
        raise Halt('Each inspection arm requires fresh fixture state')
    started = time.monotonic()
    passed = (overlap(factory, card, worktree, base, checks, release_snapshot, evaluation=True,
                      role=inspection_role)
              if variant == 'candidate' else checks())
    adverse = None
    if passed is not False and variant == 'candidate' and release_snapshot is None:
        head = record['verification']['head']
        adverse = rejection(factory, card, worktree, head, base, evaluation=True)
    if adverse:
        stopped, _ = adverse
        outcome, receipts = 'inspection-fail', []
    else:
        outcome, receipts = ('checks-failed', []) if passed is False else assess()
    refs = []
    for receipt in receipts:
        path = Path(receipt['result_path']).parent / 'receipt.json'
        refs.append({'path': str(path), 'sha256': checksum(path)})
    measured = {'config': factory.c, 'checks': record['verification'], 'assessment': refs[-1] if refs else None,
                'specialists': refs[:-1], 'inspection': next(iter(record.get('review_inspections', [])), None),
                'outcome': outcome, 'elapsed_seconds': time.monotonic() - started,
                **({'rejection': {'path': stopped['receipt_path'], 'sha256': stopped['receipt_sha256']}} if adverse else {}),
                **({'execution_limits': execution_limits} if execution_limits is not None else {}),
                **({'measurement_plan_sha256': measurement_plan_sha256,
                    'reserved_lanes': list(reserved_lanes)} if measurement_plan_sha256 is not None else {})}
    timing = factory.run_dir / 'inspection-measurement.json'
    if timing.exists():
        raise Halt('Inspection comparison measurement already exists')
    write_json(timing, measured)
    return {**measured, 'measurement': {'path': str(timing), 'sha256': checksum(timing)},
            'adjudications': [], 'coverage_adjudication': {},
            **({'release_evidence': record['release_evidence']} if 'release_evidence' in record else {})}


def create_fixture(output, case, fixture_path, before, source, check_program, *,
                   prefix='', suffix='', instructions=None, prose=False, workload=None):
    instruction_files = {**(instructions or {}), **(workload or {})}
    # This temporary repository and all its original evidence are retained
    # for reproduction, including failed assertions. No user repo is changed.
    root = Path(tempfile.mkdtemp(prefix=case + '-', dir=output))
    git(root, 'init', '-b', 'main')
    git(root, 'config', 'user.name', 'SFC Fixture')
    git(root, 'config', 'user.email', 'fixture@example.invalid')
    (root / '.gitignore').write_text('.sfc/\n')
    (root / 'check_behavior.py').write_text(check_program + '\n')
    fixture = root / fixture_path
    fixture.parent.mkdir(parents=True, exist_ok=True)
    fixture.write_text(prefix + before + suffix)
    tracked = ['.gitignore', 'check_behavior.py', fixture_path]
    for path, content in instruction_files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        tracked.append(path)
    if prose:
        (root / 'fixture_lookup.py').write_text(LOOKUP)
        tracked.append('fixture_lookup.py')
    git(root, '--literal-pathspecs', 'add', '--', *tracked)
    git(root, 'commit', '-m', 'fixture base')
    base = git(root, 'rev-parse', 'HEAD')
    fixture.write_text(prefix + source + suffix)
    git(root, 'commit', '-am', 'fixture candidate')
    head = git(root, 'rev-parse', 'HEAD')
    return root, head, base


def benchmark(output, project_config=None, *, fixture_path='behavior.py', attest=None,
              case_names=None, before_dispatch=None, instruction_root=None, baseline_factory=Factory):
    parts = fixture_path.split('/')
    reserved = {'', '.', '..', '.git', '.sfc', '.gitignore', '.gitattributes', '.gitmodules',
                'check_behavior.py', 'fixture_lookup.py'}
    if (len(fixture_path) > 240 or '\\' in fixture_path or any(ord(char) < 32 for char in fixture_path) or
            any(part.casefold() in reserved for part in parts)):
        raise Halt('Benchmark fixture path must be a relative file path outside runner files')
    settings = binding = cli_version = target_config = None
    if project_config is not None:
        project_config = validate_config(project_config)
        if (project_config.get('review_policy') or {}).get('providers', {}).get('review') != 'codex-native':
            raise Halt('Real-model evaluation requires an explicit native review project configuration')
        settings = model_settings(project_config)
        if not explicit_review_settings(settings):
            raise Halt('Real-model evaluation requires explicit review models, reasoning effort and service tier')
        target_policy = comparison_policy(project_config)
        target_config = validate_config({**project_config, 'review_policy': target_policy})
        arm_config('candidate', settings, target_policy, target_config=target_config)
        binding = promotion_binding(project_config)
        cli_version = exec_provenance(None)['version']
    behavioral = project_config is not None and bool(target_policy.get('behavioral_contract'))
    instruction_files = {}
    if behavioral:
        for path, expected in target_policy['behavioral_contract']['instructions'].items():
            if instruction_root is None:
                raise Halt('Supply an explicit fixture instruction root for the declared instruction inventory')
            root = Path(instruction_root).resolve()
            source = root / path
            if (source.is_symlink() or not source.resolve().is_relative_to(root) or checksum(source) != expected):
                raise Halt('Fixture instruction differs from the declared review contract')
            instruction_files[path] = source.read_bytes()
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    samples, oracles, prepared = [], [], []
    cases = behavioral_cases() if behavioral else CASES
    for index, (case, kind, regression, before, source, assertion, check, criterion) in enumerate(cases):
        if case_names is not None and case not in case_names:
            continue
        prose = not behavioral and kind == 'clean' and Path(fixture_path).suffix.lower() in {'.md', '.rst', '.txt'}
        prefix, suffix = ('```python\n', '```\n') if not prose and fixture_path.lower().endswith('.md') else ('', '')
        loader = f'from pathlib import Path\n\nsource = Path({fixture_path!r}).read_text()\n'
        if prose:
            before = 'An existing record has a stored value.\n'
            source = 'An existing record returns its stored value.\n'
            loader = 'from fixture_lookup import lookup\n' + loader
            check += f'\nassert source == {source!r}'
            criterion += ' The guide states that behavior.'
        else:
            if prefix:
                loader += f'source = source.removeprefix({prefix!r}).removesuffix({suffix!r})\n'
            loader += f'exec(compile(source, {fixture_path!r}, "exec"))\n'
        location = f'case-{index + 1}' if behavioral else case
        root, head, base = create_fixture(output, location, fixture_path, before, source, loader + check,
                                          prefix=prefix, suffix=suffix, instructions=instruction_files, prose=prose)
        card = {'number': index + 1, 'title': 'Review the behavior change', 'labels': [],
                'body': 'Preserve the existing behavior contracts.\n\n## Acceptance criteria\n- ' + criterion}
        sample = {'case': case, 'kind': kind, 'head': head, 'base': base, 'worktree': str(root),
                  'expected': [regression] if regression else [], 'card': card,
                  'criteria': [row['id'] for row in criteria(card)], 'task_digest': Factory.task_digest(card)}
        if behavioral:
            def committed_loader(ref):
                code = git(root, 'show', ref + ':' + fixture_path)
                return code.removeprefix(prefix).removesuffix(suffix.rstrip()) + '\n'
            base_assertion = GETTER_ORACLE + LAZY_ORACLE if kind == 'clean' else assertion
            oracles.append((sample, behavioral_oracle(root, sample, committed_loader, base_assertion, assertion),
                            location))
        prepared.append((sample, root, location, prose, loader, check, prefix, source, assertion))

    # Complete all fixture preflights before starting either comparison arm.
    for index, (sample, root, location, prose, loader, check, prefix, source, assertion) in enumerate(prepared):
        card, head, base = sample['card'], sample['head'], sample['base']
        regression = next(iter(sample['expected']), None)
        if settings is not None:
            # Alternate starting arms to limit systematic warm-up bias.
            order = ('baseline', 'candidate') if case_names is None and index % 2 == 0 else ('candidate', 'baseline')
            for variant in order:
                arm_dir = output / location / variant
                started = time.monotonic()
                try:
                    sample[variant] = real_arm(root, arm_dir, card, base, variant, settings,
                                               target_policy, attest=attest if behavioral or prose else None,
                                               before_dispatch=before_dispatch,
                                               factory_type=baseline_factory if variant == 'baseline' else Factory,
                                               target_config=target_config)
                except (OSError, RuntimeError, ValueError) as exc:
                    # Independent later fixtures are still useful. Keep the
                    # failed arm in the comparison so it can never promote.
                    error = {'error': type(exc).__name__, 'detail': str(exc), 'evidence_root': str(arm_dir),
                             'end_to_end_seconds': time.monotonic() - started}
                    arm_dir.mkdir(parents=True, exist_ok=True)
                    write_json(arm_dir / 'comparison-failure.json', error)
                    sample[variant] = error
            samples.append(sample)
            continue
        for variant, lanes in [('baseline', ('qa', 'security', 'review')), ('candidate', ('review',))]:
            started = time.monotonic()
            receipts, adjudications = [], []
            for lane in lanes:
                folder = root / variant / lane
                folder.mkdir(parents=True)
                tick = time.monotonic()
                program = loader + check if prose else source + '\n' + assertion
                p = subprocess.run([sys.executable, '-B', '-c', program],
                                   cwd=root, capture_output=True, text=True, timeout=10)
                duration = time.monotonic() - tick
                result = {'verdict': 'fail' if p.returncode else 'pass', 'summary': 'Fake reviewer executable oracle',
                          'evidence': 'Actual fixture assertion execution; not model reasoning',
                          'findings': [] if not p.returncode else [{'file': fixture_path, 'line': 3 if prefix else 2,
                              'severity': 'high', 'message': 'Fixture acceptance assertion failed'}]}
                log, result_path, receipt_path = folder / 'worker.log', folder / 'result.json', folder / 'receipt.json'
                log.write_text(p.stdout + p.stderr)
                write_json(result_path, result)
                receipt = {'lane': lane, 'head': head, 'base': base, 'result': result,
                           'provider': {'provider': 'codex-native' if variant == 'candidate' else 'codex-exec',
                                        'version': 'FAKE executable oracle'},
                           'result_path': str(result_path), 'result_sha256': checksum(result_path),
                           'log': str(log), 'log_sha256': checksum(log), 'elapsed_seconds': duration}
                write_json(receipt_path, receipt)
                receipts.append({'path': str(receipt_path), 'sha256': checksum(receipt_path)})
                if p.returncode:
                    adjudications.append({'receipt_index': len(receipts) - 1, 'index': 0, 'regression': regression,
                                          'reason': 'Independent fixture oracle confirms the failed assertion'})
                    break
            elapsed = time.monotonic() - started
            sample[variant] = {'receipts': receipts, 'adjudications': adjudications, 'coverage': sample['criteria'],
                               'review_seconds': sum(json.loads(Path(v['path']).read_text())['elapsed_seconds'] for v in receipts),
                               'end_to_end_seconds': elapsed}
        samples.append(sample)
    # No labels or held-out programs are included in a worker's checkout or
    # evidence prompt. Publish reproduction receipts only after dispatch.
    for sample, oracle, location in oracles:
        folder = output / location / 'oracle'
        folder.mkdir(parents=True)
        receipt = immutable(folder, oracle)
        sample['oracle'] = {'path': receipt['receipt_path'], 'sha256': receipt['receipt_sha256']}
    report = {'criteria': CRITERIA, 'execution': 'offline-fake-adapters', 'policy_sha256': 'evaluation-only',
              'independent_adjudicator': 'deterministic assertion oracle; no independent model judgment',
              'samples': samples}
    if settings is not None:
        report.update(execution='real-model', policy_sha256=binding, target_policy=target_policy,
                      target_config=target_config,
                      model_settings=settings, cli_version=cli_version, runtime_sha256=runtime_binding(),
                      independent_adjudicator=None)
    write_json(output / 'comparison.json', report)
    decision = evaluate(report)
    write_json(output / 'decision.json', decision)
    return decision


def source_inspection_benchmark(output, config, *, before_dispatch=None, baseline_factory=Factory,
                                package_workload_root=None, checks_only=False, retained_worker_timeout=None,
                                clean_worker_timeout=None):
    """Pair actual checks and read-only model lanes for the source profile."""
    config = validate_config(config)
    policy = inspection_policy(config)
    behavioral = bool(policy.get('behavioral_contract'))
    if (config['max_workers'] != 2 or config['verify_commands'] != FIXTURE_CHECKS or
            config.get('verification_reuse') or not explicit_review_settings(config) or
            policy['inspection']['enabled'] != ['source'] or
            policy.get('mode', 'baseline') != ('risk-based' if behavioral else 'baseline') or
            policy.get('mandatory') or set(policy) & {'imports', 'routine_candidate'} or
            (behavioral and (not package_workload_root or
                             policy['behavioral_contract'] != {'paths': ['behavior.py'], 'check_ids': [1],
                                                               'instructions': {}}))):
        raise Halt('Source overlap needs an isolated profile and exact behavioral package workload')
    workload = package_workload_files(package_workload_root) if behavioral else {}
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    settings = model_settings(config)
    report = {'criteria': INSPECTION_CRITERIA, 'execution': 'real-model',
              'binding': inspection_binding(config), 'target_policy': policy, 'target_config': config,
              'model_settings': settings, 'cli_version': exec_provenance(None)['version'],
              'runtime_sha256': runtime_binding(), 'independent_adjudicator': None,
              'profiles': {'source': []}}
    from sfc_lib.review_policy import plan
    cases = behavioral_cases()[1:] if behavioral else CASES
    prepared = []
    for index, (case, kind, regression, before, source, assertion, check, criterion) in enumerate(cases):
        if kind == 'seeded-regression':
            kind = 'failed-check'
        elif kind == 'clean' and behavioral:
            source, check, criterion = PAIRED_LOOKUP, PAIR_CHECKS, PAIR_CRITERION
            assertion = GETTER_ORACLE + LAZY_ORACLE + PAIR_CHECKS
        elif kind == 'clean':
            source, check, criterion = TRIM_LOOKUP, TRIM_CHECKS, TRIM_CRITERION
        code = 'from pathlib import Path\nexec(compile(Path("behavior.py").read_text(), "behavior.py", "exec"))\n'
        # The source/profile uses real matrix assertions on distinct inputs,
        # not a sleep. All pairs run the same pinned check on the same tree.
        if kind == 'retained-real-finding':
            matrix = ('for index in range(2 ** 20):\n'
                      '    prior = str(index)\n'
                      '    assert choose(prior, "never") is False\n'
                      '    assert choose(prior, "auto") is True\n')
        elif kind == 'clean' and behavioral:
            matrix = ('for index in range(2 ** 20):\n'
                      '    rows = {"first": index, "second": -index}\n'
                      '    assert lookup_pair(rows, "first", "second") == (index, -index)\n'
                      '    assert lookup_pair(rows, "missing", "first") == (None, index)\n')
        elif kind == 'clean':
            matrix = ('for index in range(2 ** 20):\n'
                      '    key = str(index)\n'
                      '    padded = " " + key + " "\n'
                      '    assert lookup({key: index}, padded) == index\n'
                      '    assert lookup({key: index, padded: None}, padded) is None\n'
                      '    assert lookup({key: index}, key) == index\n')
        else:
            matrix = ('for index in range(2 ** 20):\n'
                      '    key = str(index)\n'
                      '    assert lookup({key: index}, key) == index\n')
        assertions = check + '\n' + (assertion + '\n' if kind == 'failed-check' else '')
        if behavioral and kind == 'clean':
            code = package_behavior_check(assertion + '\n' + matrix)
        elif behavioral:
            code = package_behavior_check(assertions)
        else:
            code += matrix + assertions
        location = f'case-{index + 1}' if behavioral else case
        root, head, base = create_fixture(output, location, 'behavior.py', before, source, code, workload=workload)
        card = {'number': index + 1, 'title': 'Review the behavior change', 'labels': [],
                'body': 'Preserve the existing behavior contracts.\n\n## Acceptance criteria\n- ' + criterion}
        expected = [row['id'] for row in criteria(card)]
        pair = {'case': case, 'kind': kind, 'head': head, 'base': base, 'worktree': str(root),
                'expected': regression, 'card': card, 'task_digest': Factory.task_digest(card),
                'obligations': expected, 'obligations_sha256': digest(criteria(card))}
        if behavioral:
            oracle_folder = output / location / 'oracle'
            oracle_folder.mkdir(parents=True)
            base_assertion = GETTER_ORACLE + LAZY_ORACLE if kind == 'clean' else assertion
            oracle = behavioral_oracle(root, pair, lambda ref: git(root, 'show', ref + ':behavior.py') + '\n',
                                       base_assertion, assertion)
            proof = immutable(oracle_folder, oracle)
            pair['oracle'] = {'path': proof['receipt_path'], 'sha256': proof['receipt_sha256']}
        prepared.append((index, location, root, card, base, pair))
    caps = None
    reservations = []
    if (retained_worker_timeout is not None or clean_worker_timeout is not None) and not checks_only:
        from sfc_lib.profile_qualification import model_environment
        overrides = {'retained-real-finding': retained_worker_timeout, 'clean': clean_worker_timeout}
        if not behavioral or any(value is not None and (
                type(value) is not int or not EXECUTION_LIMITS['worker_timeout'] <= value <= 180
                or value > config['worker_timeout']) for value in overrides.values()):
            raise Halt('Source comparison timeout must be between 120 and 180 seconds within its configured limit')
        maximum_dispatches = SOURCE_INSPECTION_MAX_DISPATCHES
        maximum_seconds = SOURCE_INSPECTION_MAX_WORKER_SECONDS
        caps = {pair['case']: {**EXECUTION_LIMITS,
                             'worker_timeout': overrides.get(pair['kind']) or EXECUTION_LIMITS['worker_timeout']}
                for *_, pair in prepared}
        environment = model_environment(Path(__file__).resolve().parents[1])
        provider = {'version': report['cli_version'], 'provider_sha256': environment['cli_sha256']}
        measurement_plan = output / 'comparison-plan.json'
        write_json(measurement_plan, {'profile': 'source', 'config': config, 'criteria': report['criteria'],
                                     'binding': report['binding'], 'runtime_sha256': report['runtime_sha256'],
                                     'case_inventory': [{key: pair[key] for key in ('case', 'kind', 'base', 'head', 'expected')}
                                                        for *_, pair in prepared],
                                     'cli': provider, 'model_environment': environment,
                                     'measurement_limits': {'cases': caps, 'maximum_model_dispatches': maximum_dispatches,
                                                            'maximum_worker_seconds': maximum_seconds}})
        report['plan'] = {'path': str(measurement_plan), 'sha256': checksum(measurement_plan)}
        ledger = output / 'model-reservations.jsonl'
        ledger.touch(mode=0o600, exist_ok=False)
    for index, location, root, card, base, pair in prepared:
        order = ('baseline', 'candidate') if index % 2 == 0 else ('candidate', 'baseline')
        if checks_only:
            order = ('baseline',)
        for variant in order:
            factory_type = baseline_factory if variant == 'baseline' else Factory
            f = factory_type(root, config, github=object())
            limits = caps[pair['case']] if caps else EXECUTION_LIMITS
            arm_lanes = []
            bound_trial_processes(f, limits)
            f.state = {'version': 1, 'cards': {}}
            f.activity = LocalActivity()
            f.run_dir = output / location / variant
            f.occupied[card['number']] = card
            if before_dispatch is not None or caps:
                execute = f.execute

                def counted(card, stage, argv, *args, _execute=execute, _variant=variant, **kwargs):
                    if argv[0] == 'codex':
                        lane = kwargs['accounting']['lane']
                        if caps:
                            if model_environment(Path(__file__).resolve().parents[1]) != environment:
                                raise Halt('Effective Codex environment changed; source trial is incomplete')
                            seconds = limits['worker_timeout']
                            with f.mutex:
                                if len(reservations) >= maximum_dispatches or (
                                        sum(row['worker_timeout'] for row in reservations) + seconds > maximum_seconds):
                                    raise Halt('Source trial reached its finite model budget')
                                row = {'case': pair['case'], 'variant': _variant, 'lane': lane,
                                       'worker_timeout': seconds, 'index': len(reservations) + 1,
                                       'at': time.monotonic(), 'plan_sha256': report['plan']['sha256'],
                                       'provider_sha256': provider['provider_sha256']}
                                with ledger.open('a') as handle:
                                    handle.write(json.dumps(row) + '\n')
                                    handle.flush()
                                    os.fsync(handle.fileno())
                                reservations.append(row)
                                arm_lanes.append(lane)
                        if before_dispatch is not None:
                            before_dispatch(_variant, lane)
                    return _execute(card, stage, argv, *args, **kwargs)
                f.execute = counted

            def assess():
                f.record(card)['review_route'] = plan(f, card, root, base, evaluation=behavioral)
                receipts = []

                def dispatch(lane):
                    result = f.lane(card, lane, root, base)
                    receipts.append(f.record(card)['receipts'][lane])
                    return result

                outcome = run_arm(f, card, root, base, 'candidate' if behavioral else 'baseline', dispatch)
                return {'passed': 'pass', 'failed': 'fail'}.get(outcome, outcome), receipts

            started = time.monotonic()
            try:
                if checks_only:
                    f.verify(card, root, base)
                    pair[variant] = {'checks': f.record(card)['verification'],
                                     'elapsed_seconds': time.monotonic() - started}
                    continue
                pair[variant] = measure_inspection_arm(f, card, root, base, variant,
                                                      lambda: f.verify(card, root, base), assess,
                                                      execution_limits=limits,
                                                      measurement_plan_sha256=report['plan']['sha256'] if caps else None,
                                                      reserved_lanes=arm_lanes)
            except (OSError, RuntimeError, ValueError) as exc:
                error = {'error': type(exc).__name__, 'detail': str(exc), 'evidence_root': str(f.run_dir),
                         'elapsed_seconds': time.monotonic() - started}
                f.run_dir.mkdir(parents=True, exist_ok=True)
                write_json(f.run_dir / 'comparison-failure.json', error)
                pair[variant] = error
        report['profiles']['source'].append(pair)
        if caps:
            report['model_reservations'] = {'path': str(ledger), 'sha256': checksum(ledger)}
        write_json(output / 'comparison.json', report)
    if checks_only:
        return {pair['case']: pair['baseline'] for pair in report['profiles']['source']}
    decision = evaluate_inspection(report)
    write_json(output / 'decision.json', decision)
    return decision


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', help='New directory for paired run evidence')
    parser.add_argument('--fixture-path', default='behavior.py',
                        help='Relative changed path in each fixture, e.g. docs/example.md for docs-only policies')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--real-model-config', type=Path, help='Run real providers using this project config for binding')
    mode.add_argument('--real-inspection-config', type=Path, help='Run a real paired source inspection comparison')
    mode.add_argument('--evaluate', type=Path, help='Evaluate an independently adjudicated comparison JSON')
    mode.add_argument('--evaluate-inspection', type=Path, help='Evaluate an adjudicated inspection comparison JSON')
    parser.add_argument('--attest-fixtures', '--attest-clean-control', dest='attest_clean_control', action='store_true',
                        help='Time interactive operator attestations of fixture candidates')
    parser.add_argument('--fixture-instruction-root', type=Path,
                        help='Explicit source of declared, hash-verified AGENTS instructions for behavioral fixtures')
    parser.add_argument('--package-workload-root', type=Path, help='Frozen SFC package source for inspection checks')
    parser.add_argument('--checks-only', action='store_true', help='Measure the source fixture checks without model calls')
    parser.add_argument('--retained-worker-timeout', type=int,
                        help='Predeclare an equal worker timeout for both retained source arms (120–180 s)')
    parser.add_argument('--clean-worker-timeout', type=int,
                        help='Predeclare an equal worker timeout for both clean source arms (120–180 s)')
    args = parser.parse_args()
    if args.attest_clean_control and (not args.real_model_config or not sys.stdin.isatty()):
        parser.error('--attest-fixtures requires --real-model-config and an interactive terminal')
    if args.evaluate:
        print(json.dumps(evaluate(read_json(args.evaluate)), indent=2))
    elif args.evaluate_inspection:
        print(json.dumps(evaluate_inspection(read_json(args.evaluate_inspection)), indent=2))
    elif args.real_inspection_config and args.output:
        print(json.dumps(source_inspection_benchmark(args.output, read_json(args.real_inspection_config),
                         package_workload_root=args.package_workload_root, checks_only=args.checks_only,
                         retained_worker_timeout=args.retained_worker_timeout,
                         clean_worker_timeout=args.clean_worker_timeout), indent=2))
    elif args.output:
        config = validate_config(read_json(args.real_model_config)) if args.real_model_config else None
        attest = attest_from_terminal if args.attest_clean_control else None
        print(json.dumps(benchmark(args.output, config, fixture_path=args.fixture_path, attest=attest,
                                   instruction_root=args.fixture_instruction_root), indent=2))
    else:
        parser.error('--output is required to run the benchmark')
