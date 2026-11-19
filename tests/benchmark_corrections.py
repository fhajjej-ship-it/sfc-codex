#!/usr/bin/env python3
"""Measure corrected candidates with real checks and temporary Git repositories."""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
import shlex
import sys
import time
from unittest.mock import patch
from zipfile import ZipFile

from test_factory import BASE_CONFIG, FakeGitHub, issue
from test_pickup import PickupGitHub
from test_review_policy import PolicyProcesses, capabilities
from test_verification_reuse import contract
from sfc_lib import delivery, review_policy
from sfc_lib.common import Halt, git, run, write_json
from sfc_lib.factory import Factory
from sfc_lib.verification import checksum, digest, load, original

ROOT = Path(__file__).resolve().parents[1]
CASES = {
    'documentation': ('guide.md', 'Old wording\n', 'Corrected wording\n'),
    'package-version': ('VERSION', '0.9.0\n', '1.0.0\n'),
}
RUNTIME_FILES = (
    'scripts/sfc_lib/common.py', 'scripts/sfc_lib/delivery.py', 'scripts/sfc_lib/factory.py',
    'scripts/sfc_lib/qa_repair.py', 'scripts/sfc_lib/review_policy.py', 'scripts/sfc_lib/review_retention.py',
    'scripts/sfc_lib/verification.py', 'schemas/review-result.json',
    'tests/benchmark_corrections.py', 'tests/test_factory.py', 'tests/test_pickup.py', 'tests/test_review_policy.py',
    'tests/test_verification_reuse.py',
)


def require(condition, message):
    if not condition:
        raise Halt(message)


def reference(path):
    return {'path': str(path), 'sha256': checksum(path)}


def command(program):
    return shlex.join([sys.executable, '-c', program])


def configure(card, target, selective):
    code_id = review_policy.criteria(card)[0]['id']
    config = copy.deepcopy(BASE_CONFIG)
    members = ('app.py', 'value.txt', 'runtime.txt', target)
    config['verify_commands'] = [
        command("from pathlib import Path; assert Path('value.txt').read_bytes() == b'fixed\\n'; "
                "assert Path('runtime.txt').read_bytes() == b'enabled\\n'; "
                "import runpy; assert runpy.run_path('app.py')['value']() == 'fixed\\n'"),
        command("from pathlib import Path; from zipfile import ZipFile, ZipInfo; "
                "Path('dist').mkdir(exist_ok=True); "
                f"files = {members!r}; "
                "archive = ZipFile('dist/fixture.zip', 'w'); "
                "[archive.writestr(ZipInfo(path), Path(path).read_bytes()) for path in files]; archive.close()"),
        command("import shutil, subprocess, sys; from pathlib import Path; from zipfile import ZipFile; "
                "shutil.rmtree('dist/installed', ignore_errors=True); "
                "archive = ZipFile('dist/fixture.zip'); "
                "assert archive.testzip() is None; "
                f"assert archive.namelist() == {list(members)!r}; "
                "archive.extractall('dist/installed'); "
                "assert all(Path('dist/installed', path).read_bytes() == Path(path).read_bytes() "
                "for path in archive.namelist()); archive.close(); "
                "installed = subprocess.check_output([sys.executable, '-I', '-B', 'app.py'], "
                "cwd='dist/installed', timeout=10); "
                f"assert installed == b'fixed\\n' + Path({target!r}).read_bytes(); "
                "print('Installed command returned:', repr(installed.decode()))"),
    ]
    rule = {**contract(config['verify_commands'][0]), 'scope': 'repository',
            'context': 'source-only', 'outputs': ['dist']}
    config['verification_reuse'] = [rule]
    config['review_policy'] = {'mode': 'baseline'}
    if selective:
        rule['source_inputs'] = ['app.py', 'value.txt', 'runtime.txt']
        config['review_policy']['correction_reuse'] = {
            'correction_paths': [target],
            'criteria': {code_id: {'source_inputs': rule['source_inputs'], 'check_ids': [1]}},
        }
    return config


def snapshot_artifact(folder, wt, target, expected):
    folder.mkdir()
    package = folder / 'fixture.zip'
    package.write_bytes((wt / 'dist/fixture.zip').read_bytes())
    installed = folder / 'installed-payload'
    installed.write_bytes((wt / 'dist/installed' / target).read_bytes())
    with ZipFile(package) as archive:
        require(archive.read(target) == installed.read_bytes() == expected,
                'Package and extracted installation differ from the expected candidate bytes')
        require(archive.read('value.txt') == b'fixed\n', 'The package lost the unchanged behavior')
    return {'archive': reference(package), 'installed_payload': reference(installed),
            'installed_text': installed.read_text()}


def budget_ids(record):
    return {lane: [attempt['log'] for attempt in ledger['dispatches']]
            for lane, ledger in record['worker_output_budget'].items()}


def check_results(receipt):
    checks = []
    for row in receipt['checks']:
        value, ref = original(row)
        log = reference(value['log'])
        require(log['sha256'] == value['log_sha256'], 'Original check log changed')
        checks.append({'execution': reference(ref['receipt_path']), 'id': value['id'],
                       'head': value['head'], 'reused': bool(row.get('reuse')),
                       'reason': row['reason'], 'log': log,
                       'process_seconds': 0 if row.get('reuse') else value['elapsed_seconds']})
    return checks


def run_arm(folder, case, *, selective):
    target, before, after = CASES[case]
    folder.mkdir(parents=True)
    root = folder / 'project'
    root.mkdir()
    remote = folder / 'origin.git'
    run(['git', 'init', '--bare', str(remote)])
    git(root, 'init', '-b', 'main')
    git(root, 'config', 'user.name', 'SFC Correction Fixture')
    git(root, 'config', 'user.email', 'fixture@example.invalid')
    app = ('from pathlib import Path\n\n\n'
           'def value():\n'
           '    assert Path("runtime.txt").read_text() == "enabled\\n"\n'
           '    return Path("value.txt").read_text()\n\n\n'
           'if __name__ == "__main__":\n'
           f'    print(value() + Path({target!r}).read_text(), end="")\n')
    for name, text in {'.gitignore': '.sfc/\ndist/\n', 'app.py': app, 'value.txt': 'broken\n',
                       'runtime.txt': 'enabled\n', target: before}.items():
        (root / name).write_text(text)
    git(root, 'add', '.')
    git(root, 'commit', '-m', 'Fixture base')
    git(root, 'remote', 'add', 'origin', remote)
    git(root, 'push', '-u', 'origin', 'main')

    card = issue(title='Correct the packaged delivery', body=(
        f'Fix the value and ship {target} containing {after!r}.\n\n'
        '## Acceptance criteria\n- The value is fixed with the runtime setting enabled.\n'
        f'- The installed {target} contains {after!r}.\n'))
    config = configure(card, target, selective)
    gh = PickupGitHub(root, [card])
    gh.move = lambda item, status: FakeGitHub.move(gh, item, status)
    workers = PolicyProcesses(root / '.sfc/stop')
    f = Factory(root, config, gh, workers)
    f.run_dir = root / '.sfc/runs/initial'
    f.run_dir.mkdir(parents=True)
    first = True
    issue_text = f'Installed {target} contains the old payload.'
    wt = f.worktree(card)

    def build(lane, cwd):
        if lane == 'build' and not first:
            require(issue_text in f.record(card)['feedback'], 'The builder lost the original finding')
            (cwd / target).write_text(after)

    def assess(result):
        lane = result['scope']['obligation']
        current = f.record(card)
        checks = current['verification']['checks']
        installed = (wt / 'dist/installed' / target).read_bytes()
        with ZipFile(wt / 'dist/fixture.zip') as archive:
            require(archive.read(target) == installed == (wt / target).read_bytes(),
                    'Independent assessment observed inconsistent install and source bytes')
        docs = result['coverage'][1]
        docs.update(reason='The package and install checks exercise the current payload',
                    evidence=[{'kind': 'check', 'id': '3', 'sha256': checksum(checks[2]['log'])}])
        if installed != after.encode():
            docs.update(status='qa_required', reason='The actual installed payload is incorrect')
            if lane == 'review':
                result.update(verdict='fail', findings=[{'file': target, 'line': 1, 'severity': 'medium',
                                                         'message': issue_text}])
        elif lane == 'review':
            result['evidence'] += f'\nRead and compared installed {target} with the acceptance criterion.'
            if selective:
                assessment = current['review_route']['correction']
                finding, = assessment['pending_findings']
                result['evidence'] += f'\nSFC_RESOLVED {finding} Verified corrected installed bytes.'
                kept = result['coverage'][0]
                origin = assessment['kept'][kept['criterion']]
                kept['evidence'] = [{'kind': 'retained', 'id': kept['criterion'],
                                     'sha256': origin['receipt_sha256']}]

    workers.on_lane = build
    workers.mutate = assess
    with patch('sfc_lib.factory.run', side_effect=capabilities), \
            patch('sfc_lib.review_policy.run', side_effect=capabilities):
        started = time.monotonic()
        f.move(card, 'Building', 'Fixture admitted')
        require(f.build(card, wt) and f.qa(card, wt), 'Initial candidate did not reach independent review')
        record = f.record(card)
        initial = record['verification']
        first_qa = record['receipts']['qa']
        f.review(card, wt)
        first_seconds = time.monotonic() - started
        initial_package = snapshot_artifact(folder / 'initial-artifact', wt, target, before.encode())
        record = f.record(card)
        require(card['status'] == 'Building' and record['attempts'] == 1 and
                issue_text in record['feedback'], 'Initial finding did not enter the normal corrective state')
        first_dispatch = record['worker_output_budget']['review']['dispatches'][-1]
        first_review_path = Path(first_dispatch['log']).parent / 'receipt.json'
        first_review = reference(first_review_path)
        old_result = json.loads(first_review_path.read_text())['result']
        require(old_result['verdict'] == 'fail' and old_result['findings'][0]['message'] == issue_text,
                'Original independent failure is missing')
        old_budget = budget_ids(record)

        started_correction = time.monotonic()
        first = False
        f = Factory(root, config, gh, workers)
        f.run_dir = root / '.sfc/runs/corrected'
        f.run_dir.mkdir()
        wt = f.worktree(card)
        require(budget_ids(f.record(card)) == old_budget, 'Restart lost spent worker reservations')
        require(f.build(card, wt) and f.qa(card, wt), 'Corrected candidate did not reach final review')
        record = f.record(card)
        accepted = record['verification']
        require(not f.pinned(card, accepted['head'], accepted['base']),
                'Corrected head was accepted before its final review')
        require(initial['head'] != accepted['head'] and initial['base'] == accepted['base'] and
                initial['task_digest'] == accepted['task_digest'] and
                git(wt, 'diff', '--name-only', initial['head'], accepted['head']) == target,
                'Correction changed inputs outside the paired scope')
        f.review(card, wt)
        corrected_seconds = time.monotonic() - started_correction
        total_seconds = first_seconds + corrected_seconds
        installed_package = snapshot_artifact(folder / 'corrected-artifact', wt, target, after.encode())

    record = f.record(card)
    require(record.get('handoff') == accepted['head'] and f.pinned(card, accepted['head'], accepted['base']) and
            not gh.merges, 'Corrected head did not reach its verified handoff')
    new_budget = budget_ids(record)
    require(record['attempts'] == 1 and set(old_budget) == set(new_budget) and
            all(new_budget[lane][:-1] == old_budget[lane] for lane in old_budget),
            'Correction did not preserve finite spending')
    load(initial)
    load(first_qa)
    require(reference(first_review_path) == first_review, 'Original failed review changed')
    initial_checks, corrected_checks = check_results(initial), check_results(accepted)
    require([check['reused'] for check in corrected_checks] == [selective, False, False],
            'Check rerun and reuse counts differ from the declared contract')
    require((corrected_checks[0]['id'] == initial_checks[0]['id']) == selective and all(
        corrected_checks[i]['id'] != initial_checks[i]['id'] for i in (1, 2)),
        'Corrected candidate did not retain the expected original execution identity')
    final = record['receipts']['review']
    assessment = record['review_route'].get('correction')
    corrected_report = delivery.report(root, run_id='corrected')
    require(corrected_report['correction_coverage']['used_in_passing_final_review'] == int(selective),
            'Delivery report missed the corrected-head review evidence')
    for run_id in ('initial', 'corrected'):
        write_json(folder / f'delivery-{run_id}.json', delivery.report(root, run_id=run_id))
    return {
        'base_tree': git(wt, 'rev-parse', initial['base'] + '^{tree}'),
        'initial_tree': initial['tree'], 'corrected_tree': accepted['tree'],
        'task_digest': accepted['task_digest'], 'initial_head': initial['head'], 'corrected_head': accepted['head'],
        'initial_seconds': first_seconds, 'corrected_seconds': corrected_seconds, 'total_seconds': total_seconds,
        'initial_checks': initial_checks, 'corrected_checks': corrected_checks,
        'correction_counts': corrected_report['correction_coverage'],
        'initial_artifact': initial_package, 'corrected_artifact': installed_package,
        'initial_verification': reference(initial['receipt_path']), 'original_review': first_review,
        'corrected_verification': reference(accepted['receipt_path']),
        'correction_assessment': reference(assessment['receipt_path']) if assessment else None,
        'final_review': reference(final['receipt_path']), 'attempts_spent': record['attempts'],
        'worker_reservations_before': old_budget, 'worker_reservations_after': new_budget,
    }


def benchmark(output, cases=None):
    cases = list(CASES if cases is None else cases)
    require(cases and len(cases) == len(set(cases)) and set(cases) <= CASES.keys(),
            'Choose distinct supported correction cases')
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    runtime = {name: checksum(ROOT / name) for name in RUNTIME_FILES}
    pairs = {}
    for index, case in enumerate(cases):
        arms = ('whole-source', 'selective')
        if index % 2:
            arms = tuple(reversed(arms))
        rows = {arm: run_arm(output / case / arm, case, selective=arm == 'selective') for arm in arms}
        baseline, candidate = rows['whole-source'], rows['selective']
        require(all(baseline[key] == candidate[key] for key in
                    ('base_tree', 'initial_tree', 'corrected_tree', 'task_digest')),
                'Paired correction source trees or task differ')
        require(all(baseline[phase][key]['sha256'] == candidate[phase][key]['sha256']
                    for phase in ('initial_artifact', 'corrected_artifact')
                    for key in ('archive', 'installed_payload')), 'Paired package or installed bytes differ')
        pairs[case] = {'arms': rows, 'corrected_wall_seconds_delta':
                       candidate['corrected_seconds'] - baseline['corrected_seconds'],
                       'corrected_check_seconds_delta':
                       sum(check['process_seconds'] for check in candidate['corrected_checks']) -
                       sum(check['process_seconds'] for check in baseline['corrected_checks'])}
    require(runtime == {name: checksum(ROOT / name) for name in RUNTIME_FILES},
            'Correction benchmark runtime changed during measurement')
    result = {'version': 1, 'execution': 'real local checks with fake GitHub and Codex adapters',
              'scope': 'One offline pair per case, through a verified human handoff',
              'runtime_sha256': digest(runtime), 'runtime_files': runtime,
              'checkout_commit': git(ROOT, 'rev-parse', 'HEAD'), 'pairs': pairs}
    write_json(output / 'report.json', result)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--case', action='append', choices=CASES)
    options = parser.parse_args()
    benchmark(options.output, options.case)
    print(options.output.resolve() / 'report.json')
