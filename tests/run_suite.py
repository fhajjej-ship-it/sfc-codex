"""Run the entire SFC unittest discovery suite in separate processes."""

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
import io
import json
import multiprocessing
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
GROUPS = []
SFC_DOCS_SUITE_VERSION = 1
RUNTIME_MODULES = frozenset({
    'test_corrective_recovery', 'test_external_merge', 'test_factory', 'test_local_merge',
    'test_ordinary_recognition', 'test_review_policy', 'test_review_retention', 'test_watch',
})


def cases(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from cases(item)
        else:
            yield item.id()


def discover():
    """Allocate complete discovery groups so fixtures stay with their tests."""
    loader = unittest.TestLoader()
    if str(TESTS.parent) not in sys.path:
        sys.path.insert(0, str(TESTS.parent))
    groups = list(loader.discover(str(TESTS)))
    if loader.errors:
        raise RuntimeError('\n'.join(loader.errors))
    if not any(group.countTestCases() for group in groups):
        raise RuntimeError('No tests discovered')
    return groups


def initialize_worker():
    global GROUPS
    GROUPS = discover()


def run_group(index, expected, require_no_skips=False):
    suite = GROUPS[index]
    if tuple(cases(suite)) != expected:
        raise RuntimeError(f'Unittest discovery changed in group {index}')
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    covered = not require_no_skips or not (result.skipped or result.expectedFailures)
    return result.wasSuccessful() and result.testsRun == len(expected) and covered, result.testsRun, stream.getvalue()


def selected_groups(suite):
    for index, group in enumerate(discover()):
        ids = tuple(cases(group))
        if ids:
            runtime = all(case.partition('.')[0] in RUNTIME_MODULES for case in ids)
            if suite == 'all' or runtime == (suite == 'runtime'):
                yield index, ids

def run(workers, suite, expected_ids=None):
    groups = sorted(selected_groups(suite), key=lambda item: len(item[1]), reverse=True)
    ids = sorted(case for _, selected in groups for case in selected)
    if not groups or (expected_ids is not None and ids != expected_ids):
        raise RuntimeError('Test discovery differs from the selected suite in the full checkout')
    expected = sum(len(ids) for _, ids in groups)
    count = 0
    passed = True
    started = time.monotonic()
    jobs = min(workers, len(groups))
    print(f'Running {expected} tests in {len(groups)} groups on {jobs} workers', flush=True)
    with ProcessPoolExecutor(max_workers=jobs, mp_context=multiprocessing.get_context('spawn'),
                             initializer=initialize_worker) as pool:
        futures = {pool.submit(run_group, index, ids, suite == 'runtime'): ids[0] for index, ids in groups}
        for future in as_completed(futures):
            try:
                ok, ran, output = future.result()
            except Exception as exc:
                print(f'{futures[future]}: {exc}', file=sys.stderr, flush=True)
                passed = False
                continue
            print(output, end='', flush=True)
            count += ran
            passed = passed and ok
    print(f'Ran {count} of {expected} tests in {time.monotonic() - started:.3f}s', flush=True)
    print('OK' if passed and count == expected else 'FAILED', flush=True)
    return 0 if passed and count == expected else 1


def run_runtime_snapshot(workers, *, discovery_only=False):
    # The selected test groups never receive the candidate's prose bytes.
    # Everything else in the tracked tree, including test fixtures and skills, is copied.
    sys.path.insert(0, str(ROOT / 'scripts'))
    from sfc_lib.verification import documentation_path

    expected_ids = sorted(case for _, ids in selected_groups('runtime') for case in ids)
    listing = subprocess.check_output(['git', 'ls-files', '--stage', '-z', '--', '.'], cwd=ROOT, text=True)
    with tempfile.TemporaryDirectory(prefix='sfc-runtime-tests-') as folder:
        snapshot = Path(folder)
        for entry in filter(None, listing.split('\0')):
            metadata, name = entry.split('\t', 1)
            mode, _, stage = metadata.split()
            if stage != '0' or mode not in ('100644', '100755'):
                raise RuntimeError('Runtime test snapshot requires ordinary tracked files')
            if mode == '100644' and documentation_path(name):
                continue
            source = ROOT / name
            if source.is_symlink() or any(parent.is_symlink() for parent in source.parents if ROOT in parent.parents):
                raise RuntimeError('Runtime test snapshot contains a symbolic link')
            target = snapshot / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        environment = {key: value for key, value in os.environ.items() if key not in ('PYTHONPATH', 'PWD', 'OLDPWD')}
        invoke = ("import json, sys; sys.path.insert(0, 'tests'); from run_suite import run, selected_groups; "
                  "expected = json.load(sys.stdin); ")
        if discovery_only:
            invoke += ("observed = sorted(case for _, ids in selected_groups('runtime') for case in ids); "
                       "raise SystemExit(0 if observed == expected else 'Runtime test selection changed with documentation')")
        else:
            invoke += "raise SystemExit(run(int(sys.argv[1]), 'runtime', expected))"
        return subprocess.run([sys.executable, '-B', '-c', invoke, str(workers)], cwd=snapshot,
                              env=environment, input=json.dumps(expected_ids), text=True).returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workers', type=int, default=min(4, os.cpu_count() or 1))
    parser.add_argument('--suite', choices=('all', 'runtime', 'remaining'), default='all')
    args = parser.parse_args()
    if args.workers < 1:
        parser.error('--workers must be at least 1')
    sys.dont_write_bytecode = True
    if args.suite == 'runtime':
        return run_runtime_snapshot(args.workers)
    if args.suite == 'remaining' and run_runtime_snapshot(args.workers, discovery_only=True):
        return 1
    return run(args.workers, args.suite)


if __name__ == '__main__':
    raise SystemExit(main())
