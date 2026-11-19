"""Bounded delivery fixtures using normal supervisor entry points, no live board.

Default timing is fake-adapter timing. Two explicit model configurations enable
a paired real-Codex comparison; all GitHub/release operations remain simulated.
Source, config, failed-before oracle, logs and reports are retained under output.
"""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout
from contextvars import ContextVar
import hashlib
from importlib.util import module_from_spec, spec_from_file_location
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import threading
import time
import uuid
from unittest.mock import patch
from zipfile import ZipFile

HARNESS_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ROOT = Path(os.environ.get('SFC_FIXTURE_RUNTIME_ROOT', HARNESS_ROOT)).resolve()
sys.path[:0] = [str(RUNTIME_ROOT / 'scripts'), str(RUNTIME_ROOT / 'tests')]
from sfc_lib.common import Halt, Processes, Stopped, exclusive, git, now, read_json, run, write_json
from sfc_lib import lifeline
if (RUNTIME_ROOT / 'scripts/sfc_lib/delivery.py').is_file():
    from sfc_lib.delivery import report
else:
    report = None
from sfc_lib.factory import Factory
from sfc_lib.releases import Releases, digest as release_digest
from test_factory import FakeProcesses, issue
from test_pickup import PickupGitHub
from test_releases import Remote, config

CASES = [
    {'name': 'ordinary-feature', 'path': 'feature.txt', 'before': 'off\n', 'after': 'on\n',
     'title': 'Enable the feature in the installed command', 'status': 'Feature enabled'},
    {'name': 'bug-fix', 'path': 'lookup.py', 'before': 'def lookup(d, k): return d[k]\n',
     'after': 'def lookup(d, k): return d.get(k)\n', 'title': 'Handle a missing lookup entry without an error',
     'status': 'No entry found'},
    {'name': 'documentation-correction', 'path': 'GUIDE.md', 'before': 'Run the obsolete command.\n',
     'after': 'Run `python3 delivery_cli.py --version` from the extracted directory.\n',
     'title': 'Document how to check the installed command version'},
    {'name': 'package-version-correction', 'path': 'VERSION', 'before': '0.9.0\n', 'after': '1.0.0\n',
     'title': 'Report the correct installed version 1.0.0'},
    {'name': 'security-sensitive', 'path': 'auth.py', 'before': 'ALLOW_ANONYMOUS = True\n',
     'after': 'ALLOW_ANONYMOUS = False\n', 'title': 'Disable anonymous access in the installed configuration',
     'status': 'Anonymous access disabled', 'labels': ['security'], 'expected': 'human-handoff'},
    {'name': 'interrupted-release', 'path': 'feature.txt', 'before': 'off\n', 'after': 'on\n',
     'title': 'Enable the feature in the installed command', 'status': 'Feature enabled', 'interrupt': True},
]
SOURCE_CHECKS = ('python3 -m unittest discover -s tests -v', 'python3 scripts/validate_plugin.py')
ARCHIVE = 'dist/delivery-fixture.zip'
PACK = 'python3 scripts/package_fixture.py pack'
PREFLIGHT = 'python3 scripts/package_fixture.py preflight'
SMOKE = 'python3 scripts/package_fixture.py smoke'
FIXTURE = HARNESS_ROOT / 'tests/fixtures/delivery-benchmark'
# The fixture also runs against releases that predate efficiency_benchmark.py.
DEFAULT_WORKER_TIMEOUT = 120
MAX_WORKER_TIMEOUT = 300


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def runtime_digest():
    directories = ('scripts', 'skills', '.codex-plugin', 'tests', 'schemas')
    inventory = [(path.relative_to(RUNTIME_ROOT).as_posix(), hashlib.sha256(path.read_bytes()).hexdigest())
                 for directory in directories for path in (RUNTIME_ROOT / directory).rglob('*')
                 if path.is_file() and '__pycache__' not in path.parts]
    return hashlib.sha256(json.dumps(sorted(inventory), separators=(',', ':')).encode()).hexdigest()


def is_worker(argv, kwargs):
    return bool(kwargs.get('worker_events')) or argv[0] == 'codex'


class AdapterTrace:
    """Same external process boundary for current and released supervisors."""
    def __init__(self, folder):
        self.path = folder / 'adapter-executions.jsonl'
        self.active = ContextVar('delivery_fixture_execution', default=None)
        self.lock = threading.Lock()
        self.original = lifeline.spawn

    def append(self, value):
        with self.lock, self.path.open('a') as handle:
            handle.write(json.dumps(value) + '\n')

    def spawn(self, *args, **kwargs):
        result = self.original(*args, **kwargs)
        if self.active.get():
            self.append({'id': self.active.get(), 'event': 'launched', 'timestamp': now()})
        return result

    def observe(self, execute, argv, cwd, log, timeout, prompt, kwargs):
        identity = uuid.uuid4().hex
        token = self.active.set(identity)
        self.append({'id': identity, 'event': 'started', 'timestamp': now(), 'log': str(log),
                     'argv': argv, 'cwd': str(cwd), 'timeout': timeout, 'worker': is_worker(argv, kwargs)})
        tick = time.monotonic()
        outcome = {}
        try:
            outcome['exit_code'] = execute(argv, cwd, log, timeout, prompt, **kwargs)
            return outcome['exit_code']
        except BaseException as exc:
            outcome['error_type'] = type(exc).__name__
            raise
        finally:
            self.active.reset(token)
            if '--output-last-message' in argv:
                path = Path(argv[argv.index('--output-last-message') + 1])
                if path.is_file():
                    outcome.update(result_path=str(path), result_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
            self.append({'id': identity, 'event': 'completed', 'timestamp': now(),
                         'elapsed_seconds': time.monotonic() - tick, **outcome,
                         'log_sha256': hashlib.sha256(Path(log).read_bytes()).hexdigest()
                         if Path(log).is_file() else None})


def legacy_release_binding(folder, cwd, prompt):
    """Expose the inputs of the legacy release's existing verification digests."""
    root = Path(cwd).parent
    manifest = root / 'artifacts/sfc-release-manifest.json'
    subject = read_json(manifest)
    journal = read_json(root.with_name(root.name + '.json'))
    if (str(manifest) not in prompt or subject['candidate'] != journal['candidate'] or
            subject['boundary'] != journal['boundary'] or
            hashlib.sha256(manifest.read_bytes()).hexdigest() != journal['manifest_sha256']):
        raise Halt('Legacy fixture release manifest differs from its pinned subject')
    # This phase's saved project receipt is also in the original worker prompt.
    prefix = 'Supervisor project-check evidence for this exact head, base, task and configuration:\n'
    _, found, rest = prompt.partition(prefix)
    if not found:
        raise Halt('Legacy fixture prompt is missing its project receipt')
    verification, _ = json.JSONDecoder().raw_decode(rest)
    inputs = {'project_receipt_sha256': verification,
              'artifact_receipt_sha256': journal['artifact_checks']}
    expected = subject['verification']
    if {name: release_digest(value) for name, value in inputs.items()} != expected:
        raise Halt('Legacy fixture release receipt digest does not match the manifest')
    for check in [*journal['project_logs'], *journal['artifact_checks']['checks']]:
        expected_hash = check.get('sha256', check.get('log_sha256'))
        if hashlib.sha256(Path(check['log']).read_bytes()).hexdigest() != expected_hash:
            raise Halt('Legacy fixture release verification log changed')
    path = folder / 'legacy-release-bindings.json'
    binding = {'schema': 1, 'candidate': subject['candidate'], 'base': subject['boundary'],
               'manifest_sha256': journal['manifest_sha256'], 'inputs': inputs, 'digests': expected,
               'encoding': 'SHA-256 of UTF-8 json.dumps(value, sort_keys=True) with Python defaults'}
    write_json(path, binding)
    return prompt + f"""
The fixture supervisor verified the private receipt digests against the frozen
manifest and verified their original logs. The exact digest inputs and encoding
are in {path} (SHA-256 {hashlib.sha256(path.read_bytes()).hexdigest()}).
Use this indexed evidence to resolve the mechanical bindings. Inspect the
actual artifact, logs, source range, public notes and publication settings for
your assigned lane. Report concrete discrepancies. Do not infer alternative
serialization formats or search for alternative copies of these receipts.
"""


class Workers(FakeProcesses):
    def __init__(self, stop, case, real, trace=None, *, worker_cap=None, worker_timeout=None,
                 fixture_trust_override=None, fixture_root=None, legacy_release_bindings=False):
        super().__init__(stop)
        self.case, self.real, self.trace = case, real, trace
        self.worker_cap, self.worker_timeout = worker_cap, worker_timeout
        self.fixture_trust_override = fixture_trust_override
        self.fixture_root = fixture_root
        self.legacy_release_bindings = legacy_release_bindings
        self.budget_lock = threading.Lock()
        self.reserved = set()

    def execute(self, argv, cwd, log, timeout, prompt=None, **kwargs):
        if (self.legacy_release_bindings and is_worker(argv, kwargs) and
                'This is the supervisor-validated RELEASE workflow' in (prompt or '')):
            folder = Path(log).parent
            prompt = legacy_release_binding(folder, cwd, prompt)
            (folder / 'prompt.md').write_text(prompt)
        if self.worker_timeout is not None and is_worker(argv, kwargs):
            timeout = min(timeout, self.worker_timeout)
        if self.fixture_trust_override and is_worker(argv, kwargs):
            root, worktree = self.fixture_root.resolve(), Path(cwd).resolve()
            common = Path(git(worktree, 'rev-parse', '--git-common-dir'))
            if ((worktree / common).resolve() != root / '.git' or
                    any((path / '.codex').exists() for path in (root, worktree))):
                raise Halt('Fixture trust override left its generated repository scope')
            argv = [*argv[:2], '-c', self.fixture_trust_override, *argv[2:]]
        if self.trace:
            return self.trace.observe(self._execute, argv, cwd, log, timeout, prompt, kwargs)
        return self._execute(argv, cwd, log, timeout, prompt, **kwargs)

    def _execute(self, argv, cwd, log, timeout, prompt=None, **kwargs):
        if not is_worker(argv, kwargs):
            return Processes.execute(self, argv, cwd, log, timeout, prompt, **kwargs)
        folder = Path(log).parent
        lane = folder.name.split('-', 1)[0]
        reservation = (folder.parent.name, lane)
        with self.budget_lock:
            if self.worker_cap is not None and len(self.lanes) >= self.worker_cap:
                raise Halt('Fixture worker launch cap exhausted')
            if self.worker_cap is not None and reservation in self.reserved:
                raise Halt('Fixture worker lane launch cap exhausted')
            self.lanes.append(lane)
            self.reserved.add(reservation)
        if self.real:
            return Processes.execute(self, argv, cwd, log, timeout, prompt, **kwargs)
        if lane == 'build':
            Path(cwd, self.case['path']).write_text(self.case['after'])
        output = Path(argv[argv.index('--output-last-message') + 1])
        write_json(output, {'verdict': 'pass', 'summary': 'Fake adapter fixture outcome', 'findings': [],
                            'evidence': 'Deterministic fixture; no model quality claim'})
        Path(log).write_text('Fake Codex adapter; usage unknown\n')
        return 0


class Board(PickupGitHub):
    def merge(self, number, sha):
        pr = self.pr(number)
        base_ref = 'refs/heads/' + pr['baseRefName']
        base = git(self.root, 'rev-parse', base_ref)
        if pr['headRefOid'] != sha or run(['git', 'merge-base', '--is-ancestor', base, sha],
                                          self.root, check=False).returncode:
            raise Halt('Fixture protected merge requires the current base and expected head')
        tree = git(self.root, 'rev-parse', sha + '^{tree}')
        squash = git(self.root, 'commit-tree', tree, '-p', base, '-m', f'Squash PR #{number}')
        # This fixture uses its checked-out repository as origin. Move it to
        # the squash so both remote readback and local packaging see that commit.
        git(self.root, 'merge', '--ff-only', squash)
        git(self.root, 'fetch', '--no-tags', 'origin', f'{base_ref}:refs/remotes/origin/{pr["baseRefName"]}')
        self.merges.append((number, sha))
        self.prs[number].update(state='MERGED', mergeCommit={'oid': squash}, mergedAt=now())
        next(card for card in self.items if card['number'] == number - 100)['state'] = 'CLOSED'


class Destination(Remote):
    def seed_previous(self, initial, title):
        """A delivery starts after a real source boundary, not an initial import."""
        self.initial = initial
        self.title = title
        self.tags['0.9.0'] = initial
        self.rows['0.9.0'] = {'id': 1, 'name': '0.9.0', 'tag_name': '0.9.0',
            'target_commitish': initial, 'body': 'Existing fixture baseline', 'draft': False,
            'prerelease': False, 'published_at': '2026-01-01T00:00:00Z', 'assets': []}
        self.latest_tag = '0.9.0'

    def changes(self, commits):
        linked = [c for c in commits if c != self.initial]
        return {'commits': [{'commit': c, 'pull_requests': [101] if c in linked else []} for c in commits],
                'pull_requests': [{'number': 101, 'merge_commit': linked[-1], 'issues': [1],
                                   'title': self.title, 'public': True}] if linked else [],
                'issues': [1] if linked else [], 'unlinked_commits': [c for c in commits if c not in linked]}


def normal_setup(root, gh, profile, settings):
    import sfc

    config_path = root / '.sfc/config.json'
    prefix = ['--root', str(root)]
    args = ['onboard', '--repo', 'example/app', '--owner', 'example', '--project', '1',
            '--base', 'main', '--no-auto-start']
    for command in SOURCE_CHECKS:
        args.extend(('--verify', command))
    for key, value in (settings or {}).items():
        args.extend(('--' + key.replace('_', '-'), value))
    if profile == 'migrated':
        args.extend(('--efficiency-profile', 'off'))
    with patch.object(sfc, 'GitHub', return_value=gh), \
            patch.object(gh, 'configure_dashboard', return_value={'ready': True}, create=True), \
            redirect_stdout(io.StringIO()):
        if sfc.main([*prefix, *args]):
            raise Halt('Normal fixture onboarding did not complete')
    if profile == 'migrated':
        migration_args = [*prefix, 'efficiency-profile']
        output = io.StringIO()
        with redirect_stdout(output):
            if sfc.main(migration_args):
                raise Halt('Normal fixture migration preview did not complete')
        preview = json.loads(output.getvalue())
        with redirect_stdout(io.StringIO()):
            if sfc.main([*migration_args, '--apply', '--expected-digest', preview['config_digest'],
                         '--expected-proposed-digest', preview['proposed_digest']]):
                raise Halt('Normal fixture migration apply did not complete')
    return read_json(config_path)


def setup_package(root, case):
    for child in ('.codex-plugin', 'scripts', 'tests'):
        (root / child).mkdir()
    write_json(root / '.codex-plugin/plugin.json', {'name': 'sfc', 'version': '1.0.0'})
    if case['path'] != 'VERSION':
        (root / 'VERSION').write_text('1.0.0\n')
    shutil.copyfile(FIXTURE / 'delivery_cli.py', root / 'delivery_cli.py')
    shutil.copyfile(FIXTURE / 'package_fixture.py', root / 'scripts/package_fixture.py')
    (root / 'INSTALL.md').write_text(
        'Extract `delivery-fixture.zip` into an empty directory. From that directory, '
        'run `python3 delivery_cli.py --version` to check the installed version.\n')
    (root / 'AGENTS.md').write_text(
        '''# Delivery fixture

Use the local checkout and supervisor evidence. The board, PR and release links
identify simulated records. Publication and download verification follow the
independent verdicts.

For release QA, check the frozen source range and all included commits, PRs and
tickets, including any unlinked commits. Inspect the changed package files and
version against the candidate and the extracted CLI checks against INSTALL.md.
The smoke log records the installed version and case-specific status. For a
documentation change, compare the shipped text too. Cite the actual assertions
and investigate any package or behavior gap.

Use the supervisor's validated identities and completed logs. Inspect raw
receipts if a concrete discrepancy calls for it. Security and final review
separately assess disclosure, public notes and publication settings.
''')
    (root / 'scripts/validate_plugin.py').write_text(
        'import ast, json\nfrom pathlib import Path\n'
        'manifest = json.loads(Path(".codex-plugin/plugin.json").read_text())\n'
        'assert manifest == {"name": "sfc", "version": "1.0.0"}\n'
        'ast.parse(Path("delivery_cli.py").read_text())\n'
        'print("Fixture manifest and installed command syntax verified")\n')
    status = case.get('status', 'Delivery fixture ready')
    (root / 'tests/test_fixture.py').write_text(
        'from pathlib import Path\nimport subprocess\nimport sys\nimport unittest\n\n'
        'class FixtureTests(unittest.TestCase):\n'
        '    def test_installed_command_behavior(self):\n'
        '        for flag, expected in (("--version", "1.0.0"), '
        f'("--status", {status!r})):\n'
        '            with self.subTest(flag=flag):\n'
        '                result = subprocess.run([sys.executable, "-I", "-B", "delivery_cli.py", flag],\n'
        '                                        capture_output=True, text=True, timeout=10)\n'
        '                self.assertEqual(result.returncode, 0, result.stderr)\n'
        '                self.assertEqual(result.stdout, expected + "\\n")\n\n'
        '    def test_requested_content(self):\n'
        f'        self.assertEqual(Path({case["path"]!r}).read_text(), {case["after"]!r})\n')


def downloaded_install(folder, root, release, workers):
    archive = next(a['name'] for a in release.journal['artifacts'] if a['name'].endswith('.zip'))
    package = release.directory / release.journal['version'] / 'downloads' / archive
    expected = next(a['sha256'] for a in release.journal['artifacts'] if a['name'] == package.name)
    if hashlib.sha256(package.read_bytes()).hexdigest() != expected:
        raise Halt('Downloaded fixture does not match the exact verified release artifact')
    log = folder / 'downloaded-install.log'
    argv = [sys.executable, 'scripts/package_fixture.py', 'smoke', str(package)]

    if workers.execute(argv, root, log, 15):
        raise Halt('Downloaded fixture failed the installed command smoke check')
    return {'artifact': str(package), 'artifact_sha256': expected, 'log': str(log),
            'log_sha256': hashlib.sha256(log.read_bytes()).hexdigest()}


def fixture_repo(folder, *, bare_remote=False):
    root = folder / 'repo'
    root.mkdir(parents=True)
    git(root, 'init', '-b', 'main')
    git(root, 'config', 'user.name', 'SFC Delivery Fixture')
    git(root, 'config', 'user.email', 'fixture@example.invalid')
    (root / '.gitignore').write_text('.sfc/\n__pycache__/\n/dist/\n')
    remote = folder / 'origin.git' if bare_remote else root
    if bare_remote:
        run(['git', 'init', '--bare', '-b', 'main', str(remote)], folder)
    git(root, 'remote', 'add', 'origin', str(remote))
    return root


def fixture_commit(root, message):
    git(root, 'add', '.')
    fixed_date = '2026-01-01T00:00:00Z'
    run(['git', 'commit', '-m', message], root,
        env={**os.environ, 'GIT_AUTHOR_DATE': fixed_date, 'GIT_COMMITTER_DATE': fixed_date})
    return git(root, 'rev-parse', 'HEAD')


def scenario(folder, case, settings=None, *, profile=None, normal_profile=False, worker_cap=None,
             fixture_worker_timeout=None, pin_fixture_trust=False, draft_only=False, saved_config=None,
             legacy_release_bindings=False):
    if legacy_release_bindings and (profile != 'released' or not normal_profile or draft_only or
            (RUNTIME_ROOT / 'scripts/sfc_lib/profile_qualification.py').exists()):
        raise Halt('Legacy release bindings require the released publication fixture')
    if draft_only and not normal_profile:
        raise Halt('Draft comparison requires the normal supervisor fixture')
    if saved_config is not None and not draft_only:
        raise Halt('A saved fixture configuration requires the draft boundary')
    if normal_profile and (profile not in ('released', 'fresh', 'migrated') or
                           type(worker_cap) is not int or worker_cap < 1):
        raise Halt('Normal fixture requires an exact setup path and a positive worker launch cap')
    if normal_profile and (type(fixture_worker_timeout) is not int or
                           not 1 <= fixture_worker_timeout <= MAX_WORKER_TIMEOUT):
        raise Halt(f'Normal fixture worker timeout must be an integer from 1 to {MAX_WORKER_TIMEOUT} seconds')
    if pin_fixture_trust and (settings is None or not normal_profile):
        raise Halt('Fixture trust override requires a normal real comparison arm')
    folder = Path(folder).resolve()
    folder.mkdir(parents=True, exist_ok=False)
    atomic = saved_config is not None and saved_config.get('merge_mode') == 'local-verification'
    root = fixture_repo(folder, bare_remote=atomic)
    (root / 'value.txt').write_text('package bytes\n')
    (root / case['path']).write_text(case['before'])
    if profile is not None:
        setup_package(root, case)
        if saved_config is not None:
            if (saved_config['verify_commands'] != list(SOURCE_CHECKS) or
                    saved_config['release']['package_commands'] != ['python3 scripts/package_plugin.py'] or
                    saved_config['release']['verify_commands'] != ['python3 scripts/verify_release_package.py'] or
                    saved_config['release']['artifacts'] != ['dist/sfc-{version}.zip', 'dist/sfc-{version}.zip.sha256'] or
                    saved_config.get('verification_reuse') or saved_config.get('review_policy') or
                    saved_config['release'].get('preflight_commands') or saved_config['release']['authority']['mode'] != 'draft'):
                raise Halt('Saved route is outside the supported SFC maintainer fixture')
            (root / 'scripts/package_plugin.py').write_text(
                'import package_fixture as fixture\n'
                'version = (fixture.ROOT / "VERSION").read_text().strip()\n'
                'fixture.ARCHIVE = fixture.ROOT / "dist" / f"sfc-{version}.zip"\n'
                'fixture.pack()\n'
                'digest = fixture.hashlib.sha256(fixture.ARCHIVE.read_bytes()).hexdigest()\n'
                'fixture.ARCHIVE.with_suffix(".zip.sha256").write_text('
                'f"{digest}  {fixture.ARCHIVE.name}\\n")\n')
            (root / 'scripts/verify_release_package.py').write_text(
                'from package_fixture import ROOT, ZipFile, hashlib, preflight, smoke\n'
                'version = (ROOT / "VERSION").read_text().strip()\n'
                'path = ROOT / "dist" / f"sfc-{version}.zip"\n'
                'digest = hashlib.sha256(path.read_bytes()).hexdigest()\n'
                'assert path.with_suffix(".zip.sha256").read_text() == f"{digest}  {path.name}\\n"\n'
                'with ZipFile(path) as archive:\n    preflight(archive)\n    smoke(archive)\n')
            (root / 'INSTALL.md').write_text(
                'Verify the SHA-256 file for the versioned SFC ZIP. Extract it and run '
                '`python3 delivery_cli.py --version` from the extracted directory.\n')
        check = shlex.split(SOURCE_CHECKS[0])
    else:
        oracle = f"from pathlib import Path\nassert Path({case['path']!r}).read_text() == {case['after']!r}\n"
        (root / 'check.py').write_text(oracle)
        check = [sys.executable, 'check.py']
    before = subprocess.run(check, cwd=root, capture_output=True, text=True, timeout=10)
    if before.returncode == 0:
        raise Halt('Fixture must detect the missing change before Build')
    (folder / 'before-defect.log').write_text(before.stdout + before.stderr)
    initial = fixture_commit(root, 'Delivery fixture base')
    if atomic:
        git(root, 'push', 'origin', 'HEAD:refs/heads/main')
    c = config()
    c.update(merge_policy='auto', max_attempts=1, max_workers=2, max_cycles=8,
             worker_timeout=120, verify_timeout=20, verify_commands=[shlex.join(check)])
    if profile is not None:
        c.update(verify_commands=list(SOURCE_CHECKS))
        c['release'].update(artifacts=[ARCHIVE], package_commands=[PACK], verify_commands=[PREFLIGHT, SMOKE],
                            public_docs={'Installation': 'INSTALL.md'})
    version_file = case['name'] == 'package-version-correction' and saved_config is None
    if version_file:
        c['release']['version_file'] = 'VERSION'
        if profile is None:
            c['release'].update(artifacts=['dist/package-version.txt'],
                                package_commands=['mkdir -p dist && cp VERSION dist/package-version.txt'],
                                verify_commands=['cmp VERSION dist/package-version.txt'])
        subprocess.run(['sh', '-c', c['release']['package_commands'][0]], cwd=root,
                       capture_output=True, text=True, timeout=10, check=True)
        before_package = root / c['release']['artifacts'][0]
        if profile is None:
            before_artifact = before_package.read_bytes()
        else:
            with ZipFile(before_package) as package:
                before_artifact = package.read('VERSION')
            shutil.copyfile(before_package, folder / 'before-package.zip')
        if before_artifact != case['before'].encode():
            raise Halt('Fixture before package must contain the wrong candidate version')
        (folder / 'before-package-version.txt').write_bytes(before_artifact)
        before_package.unlink()
    if settings is not None:
        c.update({k: settings[k] for k in ('model', 'reasoning_effort', 'service_tier')})
    route = fingerprint({k: v for k, v in c.items() if k not in ('model', 'reasoning_effort', 'service_tier')})
    if profile in ('fresh', 'migrated'):
        c['release'].update(preflight_commands=[PREFLIGHT], verify_commands=[SMOKE])
    if draft_only:
        c['release']['authority']['mode'] = 'draft'
    if profile in ('fresh', 'migrated') and not normal_profile:
        from sfc_lib.efficiency import install_runtimes, migration, propose
        with exclusive(root):
            if profile == 'fresh':
                c = propose(root, c)['config']
                install_runtimes(c)
            else:
                write_json(root / '.sfc/config.json', c)
                preview = migration(root)
                migration(root, apply=True, expected_digest=preview['config_digest'],
                          expected_proposed_digest=preview['proposed_digest'])
                c = read_json(root / '.sfc/config.json')
    title = case['title'] if profile is not None else 'Improve the delivery fixture'
    criteria = (f"- {title}.\n- Both configured project checks pass." if profile is not None
                else '- python3 check.py passes.')
    if normal_profile and case['name'] == 'bug-fix':
        criteria += '\n- Reproduce the missing-record lookup failure on the base and verify the correction.'
    card = issue(1, title=title, labels=case.get('labels', []),
                 body=f"Change only {case['path']} to exactly {case['after']!r}.\n\n"
                      f"## Acceptance criteria\n{criteria}\n")
    if atomic:
        from test_local_merge import PriorityAtomicGitHub
        gh = PriorityAtomicGitHub(root, folder / 'origin.git', [card])
    else:
        gh = Board(root, [card])
    release_config = c
    if saved_config is not None:
        import copy
        from sfc_lib.profile_qualification import model_route_binding
        release_config = copy.deepcopy(saved_config)
        release_config.update(repo='example/app', project={'owner': 'example', 'number': 1})
        release_config['release']['authority'].update(repo='example/app', authorization_id='draft-fixture')
        if settings is not None:
            release_config.update(settings)
        c = {k: v for k, v in release_config.items() if k != 'release'}
        if model_route_binding(release_config, root) != model_route_binding(saved_config, root):
            raise Halt('Saved fixture release route differs after adaptation')
        write_json(root / '.sfc/config.json', c)
    elif normal_profile:
        c = normal_setup(root, gh, profile, settings)
        release_config = {**c, 'release': release_config['release']}
    trace = AdapterTrace(folder) if profile is not None else None
    trust_mode = trust_override = None
    if pin_fixture_trust:
        # Load the harness helper from the same current source for both arms.
        # The released runtime predates model-qualification utilities.
        spec = spec_from_file_location('delivery_fixture_trust', HARNESS_ROOT / 'scripts/sfc_lib/fixture_trust.py')
        helper = module_from_spec(spec)
        spec.loader.exec_module(helper)
        trust_mode, trust_override = helper.FIXTURE_TRUST_MODE, helper.fixture_trust_override(root)
    workers = Workers(root / '.sfc/stop', case, settings is not None, trace,
                      worker_cap=worker_cap, worker_timeout=fixture_worker_timeout,
                      fixture_trust_override=trust_override, fixture_root=root,
                      legacy_release_bindings=legacy_release_bindings)
    if trace and settings is not None:
        if not lifeline.supported():
            raise Halt('The bounded real comparison requires supported macOS child cleanup')
        workers.lifetime = True
    if profile == 'fresh' and not normal_profile:
        from sfc_lib import onboarding
        write_json(root / '.sfc/config.json', c)
        onboarding.save(root, c, gh.metadata(), False, False)
    f = Factory(root, c, gh, workers)
    destination = Destination(f)
    destination.seed_previous(initial, title)
    original_run = run

    def version(argv, *args, **kwargs):
        if settings is None and argv[:2] == ['codex', '--version']:
            return subprocess.CompletedProcess(argv, 0, 'FAKE delivery adapter', '')
        return original_run(argv, *args, **kwargs)

    initial_tree = git(root, 'rev-parse', initial + '^{tree}')
    # A sandbox path differs between arms, but a fixture-owned isolated Python
    # runtime with the same descriptor content is the same route selection.
    configuration_route = json.loads(json.dumps({
        k: v for k, v in c.items() if k not in ('model', 'reasoning_effort', 'service_tier')}))
    for policy in configuration_route.get('verification_reuse', []):
        if policy.get('runtime'):
            policy['runtime']['location'] = str(Path(policy['runtime']['location']).relative_to(root))
    verified_at = None
    model_route = release_model_route = None
    if (RUNTIME_ROOT / 'scripts/sfc_lib/profile_qualification.py').is_file():
        from sfc_lib.profile_qualification import model_route_binding
        model_route = model_route_binding(c, root)
        if normal_profile and case.get('expected') != 'human-handoff':
            release_model_route = model_route_binding(release_config, root)
    original_complete = Releases.complete

    def complete(release, item):
        nonlocal verified_at
        result = original_complete(release, item)
        if release.journal['state'] == 'done':
            verified_at = time.monotonic()
        return result

    with patch('sfc_lib.factory.run', side_effect=version), \
            patch('sfc_lib.releases.ReleaseGitHub', return_value=destination), \
            patch.object(Releases, 'complete', complete), \
            patch.object(lifeline, 'spawn', trace.spawn if trace else lifeline.spawn):
        version_config = Factory(root, release_config, gh, workers) if normal_profile and version_file else f
        before_version = Releases(version_config).select_version(initial) if version_file else None
        if version_file and before_version != case['before'].strip():
            raise Halt('Fixture base must select its original candidate version')
        write_json(folder / 'scenario.json', {**case, 'config': c, 'initial': initial,
                                            'expected': case.get('expected', 'verified-release'),
                                            'before_oracle_exit': before.returncode,
                                            'before_release_version': before_version, 'source_tree': initial_tree,
                                            'task_sha256': fingerprint(card['body']), 'route_sha256': route,
                                            'profile': profile, 'settings': settings,
                                            **({'legacy_release_bindings': True} if legacy_release_bindings else {}),
                                            'model_route_sha256': model_route,
                                            'runtime_root': str(RUNTIME_ROOT), 'runtime_sha256': runtime_digest(),
                                            **({'fixture_trust': trust_mode,
                                                'fixture_trust_override_sha256': hashlib.sha256(
                                                    trust_override.encode()).hexdigest()} if trust_override else {}),
                                            **({'source_config': c,
                                                'release_config': release_config
                                                if case.get('expected') != 'human-handoff' else None,
                                                'source_model_route_sha256': model_route,
                                                'release_model_route_sha256': release_model_route,
                                                'worker_launch_cap': worker_cap,
                                                'fixture_worker_timeout_seconds': fixture_worker_timeout}
                                               if normal_profile else {}),
                                            'configuration_route_sha256': fingerprint(configuration_route)})
        started = time.monotonic()
        result = f.run(allow_merge=True)
        if gh.items[0]['status'] == 'Blocked' and case.get('expected') == 'human-handoff':
            saved = f.record(card)
            if (not saved.get('handoff') or not f.pinned(card, saved.get('head'), saved.get('base')) or
                    run(['git', 'show', saved['head'] + ':' + case['path']], root).stdout != case['after'] or
                    git(root, 'diff', '--name-only', initial, saved['head']) != case['path']):
                raise Halt('Sensitive fixture did not reach a verified human merge handoff')
            if gh.merges or (atomic and gh.applied) or destination.writes:
                raise Halt('Sensitive fixture crossed the human/publication gate')
            return {'expected': 'human-handoff', 'observed': 'human-handoff',
                    'report': result.get('delivery_report', {}).get('path'),
                    'delivery_seconds': time.monotonic() - started,
                    'candidate_tree': git(root, 'rev-parse', saved['head'] + '^{tree}'),
                    'human_handoffs': 1, 'human_interventions': None}
        if atomic:
            git(root, 'merge', '--ff-only', 'refs/remotes/origin/main')
        if (gh.items[0]['status'] != 'Done' or (root / case['path']).read_text() != case['after'] or
                git(root, 'diff', '--name-only', initial, 'HEAD') != case['path']):
            raise Halt('Fixture did not deliver the expected exact source')

        if normal_profile:
            # The source ticket was completed under normal setup. Release is a
            # separate, explicit local configuration and supervisor admission.
            with exclusive(root):
                write_json(root / '.sfc/config.json', release_config)
            c = release_config
            f = Factory(root, c, gh, workers)
            destination.f = f

        def create(gh, root, **kwargs):
            gh.items.append(issue(2, title=kwargs['title'], body=kwargs['body'], status='Backlog', labels=['maintenance']))
            return {'issue': {'number': 2}}

        with patch('sfc_lib.priority.create_ticket', side_effect=create):
            request = Releases(f).request(None if version_file else '1.0.0')
        if version_file and (request['version'] != case['after'].strip() or request['version'] == before_version):
            raise Halt('Release did not select the corrected version from the frozen candidate')
        gh.items[-1]['status'] = 'Ready'
        recovery = None
        if case.get('interrupt'):
            original_publish = Releases.publish if draft_only else destination.publish
            interrupted_target = Releases if draft_only else destination

            def interrupted(*args):
                original_publish(*args)
                raise Stopped('Fixture interrupted after the release operation')

            with patch.object(interrupted_target, 'publish', side_effect=interrupted, autospec=draft_only):
                try:
                    f.run(allow_merge=True)
                except Stopped:
                    pass
                else:
                    raise Halt('Interrupted fixture did not stop')
            journal_path = Releases(f).directory / (request['version'] + '.json')
            original_journal = folder / 'interrupted-release-journal.json'
            shutil.copyfile(journal_path, original_journal)
            original = read_json(original_journal)
            old_lanes = list(workers.lanes)
            old_writes = list(destination.writes)
            old_trace = trace.path.read_bytes() if trace else None
            old_attempts = f.record(gh.items[-1])['attempts']
            f = Factory(root, c, gh, workers)
            destination.f = f
            result = f.run(allow_merge=True)
            final = read_json(journal_path)
            pins = ('candidate', 'boundary', 'attempt_reservation', 'package_checks', 'preflight_checks',
                    'project_logs', 'artifact_checks', 'manifest_sha256', 'publication_manifest_sha256')
            if (workers.lanes != old_lanes or (trace and trace.path.read_bytes() != old_trace) or
                    destination.writes != (old_writes if draft_only else [*old_writes, 'close']) or
                    f.record(gh.items[-1])['attempts'] != old_attempts or
                    any(original.get(k) != final.get(k) for k in pins)):
                raise Halt('Release recovery changed original evidence, repeated paid work or repeated publication')
            recovery = {'original_journal': str(original_journal),
                        'original_journal_sha256': hashlib.sha256(original_journal.read_bytes()).hexdigest(),
                        'attempts_spent': old_attempts, 'additional_workers': len(workers.lanes) - len(old_lanes),
                        'additional_remote_writes': destination.writes[len(old_writes):]}
        else:
            result = f.run(allow_merge=True)
        if draft_only:
            release = Releases(f)
            release.load(gh.items[-1])
            remote_draft = destination.release(request['version'])
            if (gh.items[-1]['status'] != 'Review' or release.journal['state'] != 'draft-unpublished' or
                    remote_draft['draft'] is not True or 'publish' in destination.writes or
                    'close' in destination.writes):
                raise Halt('Fixture did not reach a verified unpublished draft')
            install = downloaded_install(folder, root, release, workers)
            installed_at = time.monotonic()
            report_path = None
            if report is not None:
                report_path = folder / 'delivery-report.json'
                write_json(report_path, report(root))
            return {'expected': 'verified-draft', 'observed': 'verified-draft',
                    'delivery_seconds': installed_at - started,
                    'candidate_tree': git(root, 'rev-parse', 'HEAD^{tree}'),
                    'human_handoffs': 0, 'human_interventions': None,
                    'report': str(report_path) if report_path else None, 'publish_count': 0,
                    'draft_journal': {'path': str(release.path),
                                      'sha256': hashlib.sha256(release.path.read_bytes()).hexdigest()},
                    'downloaded_install': install, **({'recovery': recovery} if recovery else {})}
        if gh.items[-1]['status'] != 'Done' or destination.writes.count('publish') != 1:
            raise Halt('Publication/restart did not complete exactly once')
        install = None
        if profile is not None:
            release = Releases(f)
            release.load(gh.items[-1])
            install = downloaded_install(folder, root, release, workers)
            installed_at = time.monotonic()
        elif version_file:
            uploaded = [destination.assets[a['id']] for a in destination.rows[request['version']]['assets']
                        if a['name'] == 'package-version.txt']
            if uploaded != [case['after'].encode()]:
                raise Halt('Verified release asset did not contain the corrected candidate version')
    if verified_at is None:
        raise Halt('Fixture did not observe terminal release verification')
    delivery_seconds = (installed_at if install else verified_at) - started
    if report is not None:
        value = report(root)
        write_json(folder / 'delivery-report.json', value)
    return {'expected': 'verified-release', 'observed': 'done', 'delivery_seconds': delivery_seconds,
            'candidate_tree': git(root, 'rev-parse', 'HEAD^{tree}'), 'human_handoffs': 0, 'human_interventions': None,
            'report': str(folder / 'delivery-report.json') if report is not None else None,
            'publish_count': destination.writes.count('publish'),
            **({'downloaded_install': install, 'publication_verified_seconds': verified_at - started} if install else {}),
            **({'recovery': recovery} if recovery else {}),
            **({'before_release_version': before_version, 'release_version': request['version']} if version_file else {})}


def benchmark(output, baseline=None, candidate=None, cases=None):
    if (baseline is None) != (candidate is None):
        raise Halt('Paired real-model comparison requires both model configurations')
    for value in (baseline, candidate):
        if value is not None and any(not isinstance(value.get(k), str) or not value[k].strip()
                                     for k in ('model', 'reasoning_effort', 'service_tier')):
            raise Halt('Real comparison requires explicit model, effort and tier')
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    rows = []
    selected = [c for c in CASES if cases is None or c['name'] in cases]
    if not selected or (cases and len(selected) != len(set(cases))):
        raise Halt('Select known, bounded delivery scenarios')
    for index, case in enumerate(selected):
        arms = [('fixture', None)] if baseline is None else [('baseline', baseline), ('candidate', candidate)]
        if index % 2:
            arms.reverse()
        for arm, settings in arms:
            tick = time.monotonic()
            result = scenario(output / case['name'] / arm, case, settings)
            rows.append({'case': case['name'], 'arm': arm, 'fixture_total_seconds': time.monotonic() - tick, **result})
    value = {'execution': 'fake-adapters' if baseline is None else 'real-Codex-with-fake-GitHub',
             'maximum_scenarios': 6, 'maximum_attempts_per_ticket': 1, 'worker_timeout_seconds': 120,
             'evaluation_wall_seconds': time.monotonic() - started,
             'evaluation_overhead': 'Fixture setup, failing-before oracle and report generation are evaluation work; '
                                    'run reports isolate supervisor time. No live upload latency or billing is measured.',
             'scenarios': rows}
    write_json(output / 'comparison.json', value)
    return value


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--baseline-model-config', type=Path)
    parser.add_argument('--baseline-repository', type=Path,
                        help='Local checkout containing the pinned v0.1.17 source for --qualify')
    parser.add_argument('--candidate-model-config', type=Path)
    parser.add_argument('--case', action='append', choices=[c['name'] for c in CASES])
    parser.add_argument('--qualify', action='store_true', help='Compare the pinned v0.1.17 runtime with current profiles')
    parser.add_argument('--profile', choices=('fresh', 'migrated', 'both'), default='both')
    parser.add_argument('--model-only', action='store_true',
                        help='Compare settings on the same current runtime and route; requires --qualify')
    parser.add_argument('--model-objective', choices=('latency', 'usage'), default=None,
                        help='Predeclared intended benefit for model-only comparisons')
    parser.add_argument('--worker-timeout-seconds', type=int,
                        help=f'Apply one worker cap to both qualification arms (default {DEFAULT_WORKER_TIMEOUT}, '
                             f'maximum {MAX_WORKER_TIMEOUT})')
    parser.add_argument('--fixture-arm', choices=('released', 'fresh', 'migrated'), help=argparse.SUPPRESS)
    parser.add_argument('--normal-profile', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--worker-cap', type=int, help=argparse.SUPPRESS)
    parser.add_argument('--fixture-worker-timeout', type=int, default=DEFAULT_WORKER_TIMEOUT, help=argparse.SUPPRESS)
    parser.add_argument('--pin-fixture-trust', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--draft-only', action='store_true', help='Measure through verified draft installation')
    parser.add_argument('--saved-config', type=Path, help='Use the saved SFC maintainer draft route in temporary repositories')
    parser.add_argument('--legacy-release-bindings', action='store_true',
                        help='Compare against v0.1.17 with fixture-supplied private release receipt bindings')
    args = parser.parse_args()
    if args.worker_timeout_seconds is not None and (not args.qualify or args.fixture_arm):
        parser.error('--worker-timeout-seconds requires --qualify')
    if args.baseline_repository is not None and (not args.qualify or args.fixture_arm):
        parser.error('--baseline-repository requires --qualify')
    baseline = read_json(args.baseline_model_config) if args.baseline_model_config else None
    candidate = read_json(args.candidate_model_config) if args.candidate_model_config else None
    if args.fixture_arm:
        if not args.case or len(args.case) != 1:
            parser.error('A fixture arm requires exactly one case')
        tick = time.monotonic()
        try:
            result = scenario(args.output, next(c for c in CASES if c['name'] == args.case[0]),
                              baseline, profile=args.fixture_arm, normal_profile=args.normal_profile,
                              worker_cap=args.worker_cap,
                              fixture_worker_timeout=args.fixture_worker_timeout if args.normal_profile else None,
                              pin_fixture_trust=args.pin_fixture_trust, draft_only=args.draft_only,
                              saved_config=read_json(args.saved_config) if args.saved_config else None,
                              legacy_release_bindings=args.legacy_release_bindings)
        except Exception as exc:
            result = {'observed': 'failed', 'error_type': type(exc).__name__, 'error': str(exc)}
        write_json(args.output / 'scenario-outcome.json',
                   {**result, 'evaluation_wall_seconds': time.monotonic() - tick})
    elif args.qualify:
        from sfc_lib.efficiency_benchmark import compare
        print(json.dumps(compare(HARNESS_ROOT, args.output, [c['name'] for c in CASES], cases=args.case,
                                 baseline=baseline, candidate=candidate, profile=args.profile,
                                 model_only=args.model_only, model_objective=args.model_objective, draft_only=args.draft_only,
                                 saved_config=args.saved_config,
                                 legacy_release_bindings=args.legacy_release_bindings,
                                 baseline_repository=args.baseline_repository,
                                 worker_timeout_seconds=(DEFAULT_WORKER_TIMEOUT if args.worker_timeout_seconds is None
                                                         else args.worker_timeout_seconds)), indent=2))
    else:
        if (args.model_only or args.model_objective is not None or args.draft_only or args.saved_config or
                args.legacy_release_bindings):
            parser.error('--model-only, --model-objective and --draft-only require --qualify')
        print(json.dumps(benchmark(args.output, baseline, candidate, args.case), indent=2))
