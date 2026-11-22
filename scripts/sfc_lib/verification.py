"""Versioned, immutable local evidence; declarations are contracts, not a sandbox."""
from __future__ import annotations

import datetime as dt
from contextlib import contextmanager
import hashlib
import itertools
import json
import os
from pathlib import Path
import platform
import shutil
import stat
import time
import uuid

from .common import Halt, Stopped, TimedOut, _io_allowance, common_dir, git, now, read_json, run, write_json

VERSION = 1
RUNTIME_SUITE = 'python3 tests/run_suite.py --suite runtime'
REMAINING_SUITE = 'python3 tests/run_suite.py --suite remaining'
# No credentials or arbitrary inherited environment enter reusable commands.
ENVIRONMENT = {'PATH', 'LANG', 'LC_ALL', 'LC_CTYPE', 'TZ', 'PYTHONHASHSEED'}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def fingerprint_budget(f=None):
    deadline = time.monotonic() + (f.c.get('verify_timeout', 120) if f else 120)
    def check():
        allowance = _io_allowance.get()
        if allowance:
            # Completion-only recovery already has an exact, nonrenewable
            # stop/deadline authority; preserve it past the work cutoff.
            allowance()
        elif f and f.processes.stopped():
            raise Stopped('Stop requested during verification fingerprinting')
        if time.monotonic() >= deadline:
            raise TimedOut('Verification fingerprinting timed out')
    return check


@contextmanager
def regular_file(path):
    # Check before opening devices, then fstat the actual descriptor to cover
    # replacement races. O_NONBLOCK prevents a raced-in FIFO from blocking open.
    if not stat.S_ISREG(Path(path).stat().st_mode):
        raise ValueError('nonregular fingerprint input unsupported')
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError('nonregular fingerprint input unsupported')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            yield stream
    finally:
        os.close(fd)


def checksum(path, check=None):
    check = check or fingerprint_budget()
    check()
    with regular_file(path) as stream:
        h = hashlib.sha256()
        while True:
            check()
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
        check()
        return h.hexdigest()


def validate(config):
    policies = config.get('verification_reuse', [])
    if not isinstance(policies, list):
        raise Halt('verification_reuse must be a list of per-command contracts')
    seen = set()
    for p in policies:
        keys = {'command', 'max_age_seconds', 'environment', 'tools', 'dependencies',
                'artifacts', 'external_state', 'worktree'}
        if (not isinstance(p, dict) or not keys <= set(p) or
                set(p) - keys - {'scope', 'context', 'source_inputs', 'python_stdlib', 'path_tools', 'runtime', 'outputs',
                                 'fixed_environment', 'documentation_independent'} or
                p['command'] not in config['verify_commands'] or p['command'] in seen or
                type(p['max_age_seconds']) is not int or not 0 < p['max_age_seconds'] <= 86400 or
                any(not isinstance(p[k], list) or any(not isinstance(v, str) or not v or '\0' in v
                    for v in p[k]) for k in ('environment', 'tools', 'dependencies', 'artifacts')) or
                not set(p['environment']) <= ENVIRONMENT or not p['tools'] or
                any(not Path(v).is_absolute() for v in p['tools']) or
                not isinstance(p['external_state'], str) or not isinstance(p['worktree'], str)):
            raise Halt('Incomplete or unsupported verification_reuse contract')
        if p.get('scope', 'ticket') not in ('ticket', 'repository'):
            raise Halt('Unsupported verification reuse scope')
        if p.get('scope') == 'repository' and p.get('context') != 'source-only':
            raise Halt('Repository reuse requires an explicit source-only context contract')
        if 'context' in p and p['context'] != 'source-only':
            raise Halt('Ref/history/task-dependent checks cannot use source-only reuse')
        if 'python_stdlib' in p and (not isinstance(p['python_stdlib'], str) or not Path(p['python_stdlib']).is_absolute()):
            raise Halt('python_stdlib must identify an absolute standard-library directory')
        if 'path_tools' in p and (not isinstance(p['path_tools'], list) or
                any(not isinstance(x, str) or not x or '/' in x or '\0' in x for x in p['path_tools'])):
            raise Halt('path_tools must name bare executable names resolved at each check')
        if p.get('path_tools') and 'PATH' not in p['environment']:
            raise Halt('PATH tool resolution requires an explicit PATH environment binding')
        if 'runtime' in p:
            from .efficiency_runtime import validate as validate_runtime
            validate_runtime(p['runtime'])
        if 'outputs' in p and (not isinstance(p['outputs'], list) or
                any(not isinstance(x, str) or not x or Path(x).is_absolute() or
                    '..' in Path(x).parts or x in ('.', '.git') for x in p['outputs'])):
            raise Halt('outputs must identify explicit untracked output paths')
        if 'source_inputs' in p:
            paths = p['source_inputs']
            if (p.get('scope') != 'repository' or not isinstance(paths, list) or not paths or
                    any(not isinstance(x, str) or not x or Path(x).is_absolute() or
                        '..' in Path(x).parts or x in ('.', '.git') or '\0' in x for x in paths)):
                raise Halt('source_inputs requires explicit repository-relative input paths')
        if 'fixed_environment' in p:
            fixed = p['fixed_environment']
            if (not isinstance(fixed, dict) or fixed.keys() != set(p['environment']) or
                    any(not isinstance(v, str) or '\0' in v for v in fixed.values()) or
                    ('PATH' in fixed and any(not Path(d).is_absolute()
                                             for d in fixed['PATH'].split(os.pathsep)))):
                raise Halt('fixed_environment requires explicit allowed values and an absolute PATH')
        if 'documentation_independent' in p and (
                p['documentation_independent'] is not True or p['command'] != RUNTIME_SUITE or
                not {REMAINING_SUITE, 'python3 scripts/validate_plugin.py'} <= set(config['verify_commands']) or
                p.get('scope') != 'repository' or 'source_inputs' in p):
            raise Halt('Documentation-independent reuse requires both SFC test groups and plugin validation')
        seen.add(p['command'])


def inventory(worktree, paths, check=None):
    check = check or fingerprint_budget()
    result = {}
    for name in paths:
        check()
        path = Path(name)
        if not path.is_absolute():
            if '..' in path.parts:
                raise ValueError('parent path binding unsupported')
            path = Path(worktree) / path
        if path.is_symlink():
            raise ValueError('symlink dependency unsupported')
        directory = path.is_dir()
        entries = itertools.chain([path], path.rglob('*')) if directory else [path]
        values = []
        for entry in entries:
            check()
            if entry.is_symlink() or not (entry.is_dir() or entry.is_file()):
                raise ValueError('missing or unsupported dependency')
            values.append([str(entry.relative_to(path)) if directory else '.',
                           entry.stat().st_mode, checksum(entry, check) if entry.is_file() else 'directory'])
        result[name] = digest(sorted(values))
    check()
    return result


def policy(f, command):
    return next((p for p in f.c.get('verification_reuse', []) if p['command'] == command), None)


def source_tree(wt, check=None, *, allow_special_entries=False):
    """Verify actual bytes, including files hidden by Git index flags/filters."""
    check = check or fingerprint_budget()
    check()
    root = Path(wt).resolve()
    tree = git(root, 'rev-parse', 'HEAD^{tree}')
    algorithm = git(root, 'rev-parse', '--show-object-format')
    for row in git(root, 'ls-tree', '-r', '-z', 'HEAD').split('\0'):
        check()
        if not row:
            continue
        metadata, name = row.split('\t', 1)
        if '\n' in name or '\r' in name:
            raise ValueError('source filenames containing line breaks are unsupported')
        mode, kind, oid = metadata.split()
        path = root / name
        if any(p.is_symlink() for p in path.parents if root in p.parents):
            raise ValueError('source entry has a symlink parent')
        if allow_special_entries and (mode, kind) == ('160000', 'commit'):
            if path.is_symlink() or not path.is_dir():
                raise ValueError('invalid gitlink checkout')
            if next(path.iterdir(), None) is None:
                continue  # Uninitialized gitlink. Its commit is pinned by the parent tree.
            marker = path / '.git'
            if (marker.is_symlink() or not (marker.is_file() or marker.is_dir()) or
                    git(path, 'rev-parse', 'HEAD') != oid or
                    git(path, 'status', '--porcelain', '--untracked-files=all', '--ignore-submodules=none') or
                    source_tree(path, check, allow_special_entries=True) != git(path, 'rev-parse', oid + '^{tree}')):
                raise ValueError('gitlink checkout does not match its pinned commit')
            continue
        h = hashlib.new(algorithm)
        if allow_special_entries and (mode, kind) == ('120000', 'blob') and path.is_symlink():
            # Git stores the link text as a blob. Never read through the link.
            target = os.readlink(os.fsencode(path))
            h.update(f'blob {len(target)}\0'.encode())
            h.update(target)
        elif kind != 'blob' or mode not in ('100644', '100755') or path.is_symlink():
            raise ValueError('unsupported source entry')
        else:
            if bool(path.stat().st_mode & 0o111) != (mode == '100755'):
                raise ValueError('source mode mismatch')
            h.update(f'blob {path.stat().st_size}\0'.encode())
            with regular_file(path) as stream:
                while True:
                    check()
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    h.update(chunk)
        if h.hexdigest() != oid:
            raise ValueError('source bytes do not match Git tree')
    check()
    return tree


def documentation_path(name):
    path = Path(name)
    return path.parent == Path('docs') and path.suffix == '.md'


def check_environment(policy, worktree, budget=None):
    fixed = policy.get('fixed_environment')
    environment = dict(fixed) if fixed is not None else {k: os.environ.get(k) for k in policy['environment']}
    runtime = None
    if policy.get('runtime'):
        from .efficiency_runtime import inspect
        runtime = inspect(policy['runtime'], budget=budget, root=worktree,
                          path=fixed.get('PATH') if fixed is not None else None)
        environment.update(runtime['environment'])
    return environment, runtime


def correction_delta(wt, old, new, check=None):
    """Only bounded changes to the bytes of existing ordinary text files qualify."""
    check = check or fingerprint_budget()
    check()
    for revision in (old, new):
        if git(wt, 'rev-parse', revision + '^{commit}') != revision:
            raise ValueError('correction requires exact source commits')
    raw = run(['git', 'diff', '--no-ext-diff', '--no-textconv', '--no-renames',
               '--raw', '--no-abbrev', '-z', old, new], cwd=wt).stdout
    if not raw:
        return set()
    fields = raw.split('\0')
    if fields[-1] or len(fields) % 2 != 1 or len(fields) > 129:
        raise ValueError('correction inventory is malformed or too large')
    changed = set()
    for entry, path in zip(fields[::2], fields[1::2]):
        check()
        before, after, before_oid, after_oid, change = entry.removeprefix(':').split()
        if (not entry.startswith(':') or before != '100644' or after != '100644' or change != 'M' or
                before_oid == after_oid or not path or '\n' in path or '\r' in path or path in changed):
            raise ValueError('correction includes a rename, add, removal, executable, mode or special entry')
        for blob in (before_oid, after_oid):
            check()
            if (int(git(wt, 'cat-file', '-s', blob)) > 1024 * 1024 or
                    '\0' in run(['git', 'cat-file', 'blob', blob], cwd=wt).stdout):
                raise ValueError('correction includes binary or oversized source')
        changed.add(path)
    check()
    return changed


def binding(f, card, wt, base, command):
    p = policy(f, command)
    if not p:
        return None, 'command has no reuse contract'
    if p['external_state'] != 'none' or p['worktree'] not in ('independent', 'path'):
        return None, 'external-state or worktree binding unsupported'
    try:
        check = fingerprint_budget(f)
        check()
        if git(wt, 'status', '--porcelain', '--untracked-files=no'):
            return None, 'checkout has unbound changes'
        environment, runtime = check_environment(p, wt, check)
        tools = {v: {'resolved': str(Path(v).resolve()), 'sha256': checksum(Path(v).resolve(), check),
                     'mode': Path(v).stat().st_mode}
                 for v in ['/bin/sh', '/usr/bin/env', *p['tools']]}
        value = {'repository': f.c['repo'], 'common_dir': str(common_dir(wt).resolve()),
                 'head': git(wt, 'rev-parse', 'HEAD'), 'base': base,
                 'tree': source_tree(wt, check),
                 'task': f.task_digest(card), 'config': f.config_digest, 'command': command,
                 'identity': {k: card[k] for k in ('number', 'id', 'url', 'repository', 'item_id')},
                 'policy': json.loads(json.dumps(p)), 'platform': [platform.system(), platform.release(), platform.machine()],
                 'environment': environment,
                 'tools': tools, 'dependencies': inventory(wt, p['dependencies'], check)}
        if runtime is not None:
            value['runtime'] = runtime['fingerprint']
        if p.get('path_tools'):
            value['path_tools'] = {}
            for name in p['path_tools']:
                resolved = shutil.which(name, path=environment.get('PATH'))
                if not resolved:
                    return None, 'PATH tool unavailable: ' + name
                tool = Path(resolved).resolve(strict=True)
                value['path_tools'][name] = {'path': resolved, 'resolved': str(tool),
                                             'mode': tool.stat().st_mode, 'sha256': checksum(tool, check)}
        # Git ignored files may still be imported or scanned. Pin them too;
        # only explicit output contracts and supervisor-owned state are omitted.
        excluded = ['.sfc', *p.get('outputs', [])]
        extras = set()
        for ignored in (False, True):
            args = ['ls-files', '--others', '--exclude-standard', '-z']
            if ignored:
                args.append('--ignored')
            args += ['--', '.', *[':(exclude,literal)' + x for x in excluded]]
            extras.update(filter(None, run(['git', *args], cwd=wt).stdout.split('\0')))
        # Git can still emit an ignored nested repository beneath an excluded
        # directory. Enforce the same explicit output/state boundaries on its
        # returned paths before walking them; adjacent names remain inputs.
        extras = {name for name in extras if not any(
            name.rstrip('/') == path.rstrip('/') or name.startswith(path.rstrip('/') + '/')
            for path in excluded)}
        if any('\n' in name or '\r' in name for name in extras):
            return None, 'untracked filenames containing line breaks are unsupported'
        value['untracked_inputs'] = inventory(wt, sorted(extras), check)
        if p.get('python_stdlib'):
            library = Path(p['python_stdlib'])
            if not library.is_dir() or library.is_symlink():
                return None, 'Python standard library unavailable'
            # A stdlib-only contract excludes third-party imports. Existing
            # bytecode is still executable input even with writes disabled.
            # Regular-file runtime links pin both the link and
            # resolved bytes; directory links and dangling links cannot qualify.
            paths = []
            for directory, dirs, files in os.walk(library, followlinks=False):
                check()
                dirs[:] = sorted(x for x in dirs if x != 'site-packages')
                for name in [*dirs, *files]:
                    path = Path(directory) / name
                    if path.is_symlink():
                        target = path.resolve(strict=True)
                        if not target.is_file():
                            raise ValueError('runtime directory link unsupported')
                        paths.append([str(path.relative_to(library)), path.lstat().st_mode,
                                      os.readlink(path), str(target), checksum(target, check)])
                        continue
                    if path.is_file():
                        paths.append([str(path.relative_to(library)), path.stat().st_mode, checksum(path, check)])
                paths.append([str(Path(directory).relative_to(library)), Path(directory).stat().st_mode, 'directory'])
            value['python_stdlib'] = digest(sorted(paths))
        if 'source_inputs' in p:
            # Full source_tree validation above still verifies actual bytes and modes.
            # The operator declares that this command reads only these inputs.
            rows = []
            for row in git(wt, 'ls-tree', '-r', '-z', 'HEAD').split('\0'):
                if not row:
                    continue
                metadata, name = row.split('\t', 1)
                if any(name == x.rstrip('/') or name.startswith(x.rstrip('/') + '/')
                       for x in p['source_inputs']):
                    rows.append([name, metadata])
            for name in p['source_inputs']:
                if not any(r[0] == name.rstrip('/') or r[0].startswith(name.rstrip('/') + '/') for r in rows):
                    return None, 'declared source input missing'
            value['source_inputs'] = digest(sorted(rows))
        if p.get('documentation_independent'):
            rows = []
            for row in git(wt, 'ls-tree', '-r', '-z', 'HEAD').split('\0'):
                if row:
                    metadata, name = row.split('\t', 1)
                    rows.append((name, 'regular Markdown' if documentation_path(name) and
                                 metadata.startswith('100644 blob ') else metadata))
            value['documentation_inputs'] = digest(sorted(rows))
        if p['worktree'] == 'path':
            value['worktree'] = str(Path(wt).resolve())
        check()
        return value, 'matching immutable evidence'
    except (Stopped, TimedOut):
        raise
    except (OSError, ValueError, KeyError, TypeError, Halt):
        return None, 'required source/toolchain/dependency binding unavailable'


def immutable(folder, value, check=None):
    path = folder / 'receipt.json'
    with path.open('x') as stream:
        stream.write(json.dumps(value, indent=2) + '\n')
        stream.flush()
        os.fsync(stream.fileno())
    return {**value, 'receipt_path': str(path), 'receipt_sha256': checksum(path, check)}


def load(receipt, check=None):
    path = Path(receipt['receipt_path'])
    if checksum(path, check) != receipt['receipt_sha256']:
        raise ValueError('receipt hash changed')
    value = read_json(path)
    if value != {k: v for k, v in receipt.items() if k not in ('receipt_path', 'receipt_sha256')}:
        raise ValueError('receipt reference changed')
    if not isinstance(value, dict) or value.get('version') != VERSION:
        raise ValueError('legacy or unsupported receipt')
    return value


def original(check, budget=None):
    # Reuse points directly at the original execution, never a chain of rewrites.
    if check.get('reuse'):
        ref = check['reuse']
        value = load(ref, budget)
        if value['kind'] != 'execution':
            raise ValueError('reuse must reference an execution')
        return value, ref
    value = load(check, budget)
    if value['kind'] != 'execution':
        raise ValueError('not an execution')
    return value, check


def execution_inputs(value):
    """Execution facts; new acceptance retains exact candidate/task/config pins."""
    result = dict(value)
    if value.get('policy', {}).get('scope') == 'repository' and value['policy'].get('context') == 'source-only':
        for key in ('head', 'base', 'task', 'config', 'identity'):
            result.pop(key, None)
        if 'source_inputs' in value or 'documentation_inputs' in value:
            result.pop('tree', None)
    return result


def index_directory(wt):
    return common_dir(wt) / 'sfc-verification-index'


def register(receipt, wt, f):
    """Index only complete successful immutable verifications, never partial runs.

    Unique per-verification files avoid lost entries from concurrent publishers.
    The index is only a lookup hint; all original evidence is revalidated.
    """
    if not receipt.get('passed'):
        return
    load(receipt, fingerprint_budget(f))
    if not any(policy(f, c['command']) and policy(f, c['command']).get('scope') == 'repository'
               for c in receipt['checks']):
        return
    write_json(index_directory(wt) / (receipt['id'] + '.json'), receipt)


def candidates(f, wt, command):
    p = policy(f, command)
    if not p or p.get('scope') != 'repository':
        return
    folder = index_directory(wt)
    if not folder.exists():
        return
    # Bounded lookup; unselected entries remain auditable. A miss executes.
    budget = fingerprint_budget(f)
    try:
        paths = sorted(folder.glob('*.json'), key=lambda x: x.stat().st_mtime_ns, reverse=True)
    except OSError:
        return  # Optional index vanished; execute conservatively.
    for path in paths[:256]:
        try:
            budget()
            receipt = read_json(path)
            load(receipt, budget)
            if (not receipt.get('passed') or receipt['repository'] != f.c['repo'] or
                    receipt['common_dir'] != str(common_dir(wt).resolve())):
                continue
            for check in receipt['checks']:
                if check['command'] == command:
                    yield check
        except (OSError, ValueError, KeyError, TypeError):
            continue


def acceptance(f, card, wt, base, current):
    return {'head': git(wt, 'rev-parse', 'HEAD'), 'base': base,
            'tree': git(wt, 'rev-parse', 'HEAD^{tree}'), 'repository': f.c['repo'],
            'task_digest': f.task_digest(card), 'config_digest': f.config_digest,
            'binding': current}


def matching(check, current, wt, f=None, *, at=None, since=None):
    try:
        budget = fingerprint_budget(f)
        value, ref = original(check, budget)
        if value['exit_code'] != 0 or not current or not value['binding']:
            return None, 'failed or incomplete execution bindings'
        old_inputs, new_inputs = execution_inputs(value['binding']), execution_inputs(current)
        if old_inputs != new_inputs:
            changed = sorted(k for k in old_inputs.keys() | new_inputs.keys()
                             if old_inputs.get(k) != new_inputs.get(k))
            return None, 'bindings changed: ' + ', '.join(changed)
        if (value['binding'].get('tree') != current.get('tree') and
                any(key in current and key in value['binding'] for key in ('source_inputs', 'documentation_inputs'))):
            try:
                changed = correction_delta(wt, value['binding']['head'], current['head'], budget)
                if 'documentation_inputs' in current and not all(documentation_path(path) for path in changed):
                    return None, 'source change is outside the documentation scope'
            except (Stopped, TimedOut):
                raise
            except (Halt, OSError, TypeError, ValueError):
                return None, 'unsupported scoped source delta (rename, mode, binary or unknown source)'
        now = dt.datetime.now(dt.timezone.utc)
        assessed = dt.datetime.fromisoformat(at) if at is not None else now
        earliest = dt.datetime.fromisoformat(since) if since is not None else assessed
        finished = dt.datetime.fromisoformat(value['finished'])
        age = (assessed - finished).total_seconds()
        if (not finished <= earliest <= assessed <= now or
                not 0 <= age <= current['policy']['max_age_seconds']):
            return None, 'evidence expired or timestamp is in the future'
        if checksum(value['log'], budget) != value['log_sha256']:
            return None, 'raw log hash changed'
        if inventory(wt, current['policy']['artifacts'], budget) != value['artifacts']:
            return None, 'required artifact hash changed or missing'
        return ref, 'matching immutable evidence'
    except (OSError, ValueError, KeyError, TypeError):
        return None, 'legacy, incomplete, tampered or missing evidence'


def validate_evidence(receipt, wt, f=None, card=None, base=None, *, freshness=True, at=None, since=None):
    """Hash-check evidence for lanes/recovery; final gates also recompute bindings."""
    budget = fingerprint_budget(f)
    load(receipt, budget)
    if since is not None and dt.datetime.fromisoformat(receipt['time']) > dt.datetime.fromisoformat(since):
        raise ValueError('acceptance completed after the earliest possible merge')
    for check in receipt['checks']:
        value, _ = original(check, budget)
        if value['exit_code'] != 0 or checksum(value['log'], budget) != value['log_sha256']:
            raise ValueError('invalid execution/log')
        if check['command'] != value['command'] or check['log'] != value['log'] or check['exit_code'] != 0:
            raise ValueError('changed check reference')
        if value.get('binding'):
            current, _ = binding(f, card, wt, base, check['command']) if freshness else (value['binding'], '')
            if freshness and not matching(check, current, wt, f, at=at, since=since)[0]:
                raise ValueError('verification bindings changed or expired')
        if since is not None and dt.datetime.fromisoformat(value['finished']) > dt.datetime.fromisoformat(since):
            raise ValueError('verification completed after the earliest possible merge')
        p = policy(f, check['command']) if f else None
        if p and inventory(wt, p['artifacts'], budget) != value['artifacts']:
            raise ValueError('required artifact hash changed')
        provenance = check.get('acceptance', value)
        if check.get('acceptance'):
            if not check.get('reuse') or not value.get('binding') or value['binding']['policy'].get('scope') != 'repository':
                raise ValueError('unsupported cross-ticket acceptance')
            if execution_inputs(provenance.get('binding', {})) != execution_inputs(value['binding']):
                raise ValueError('acceptance execution inputs changed')
            for nested, key in (('head', 'head'), ('base', 'base'), ('tree', 'tree'),
                                ('repository', 'repository'), ('config', 'config_digest'), ('task', 'task_digest')):
                if provenance['binding'].get(nested) != receipt.get(key):
                    raise ValueError('acceptance binding provenance changed')
        for key in ('head', 'base', 'tree', 'repository', 'config_digest', 'task_digest'):
            if provenance.get(key) != receipt.get(key):
                raise ValueError('execution provenance changed')


def verify(f, card, wt, base, *, force_check_ids=(), prior_receipt=None, report_activity=True):
    head = git(wt, 'rev-parse', 'HEAD')
    previous = prior_receipt or f.record(card).get('verification', {})
    folder = f.run_dir / f"issue-{card['number']}" / f'verify-{uuid.uuid4().hex}'
    folder.mkdir(parents=True)
    # An interruption cannot leave an old successful receipt as current authority.
    f.update(card, verification={})
    write_json(folder / 'started.json', {'head': head, 'base': base, 'time': now()})
    try:
        load(previous, fingerprint_budget(f))
        # Explicit recovery may supply an immutable partial verification. Each
        # execution must still pass the ordinary reuse contract independently.
        previous_checks = previous['checks'] if previous.get('passed') or prior_receipt else []
    except (OSError, ValueError, KeyError, TypeError):
        previous_checks = []
    records = []
    started = time.monotonic()
    for index, command in enumerate(f.c['verify_commands']):
        f.ensure_running()
        p = policy(f, command)
        current, reason = binding(f, card, wt, base, command)
        old = previous_checks[index] if index < len(previous_checks) else {}
        ref, miss = matching(old, current, wt, f)
        if not ref and current:
            for candidate in candidates(f, wt, command):
                ref, candidate_miss = matching(candidate, current, wt, f)
                if ref:
                    miss = 'repository evidence: equivalent declared execution inputs'
                    break
                if miss == 'legacy, incomplete, tampered or missing evidence':
                    miss = candidate_miss
        if index + 1 in force_check_ids:
            ref, miss = None, 'recovery requested fresh configured check evidence'
        if ref:
            value = load(ref, fingerprint_budget(f))
            reused = {'command': command, 'exit_code': 0, 'log': value['log'], 'reuse': ref, 'reason': miss}
            if p.get('scope') == 'repository':
                reused['acceptance'] = acceptance(f, card, wt, base, current)
            records.append(reused)
            continue
        check_dir = folder / f'check-{index + 1}'
        check_dir.mkdir()
        log = folder / f'check-{index + 1}.log'
        argv = ['/bin/sh', '-c', command]
        if p is not None:
            # A fingerprint miss disables reuse, but the execution contract still applies.
            allowed = current['environment'] if current else check_environment(p, wt, fingerprint_budget(f))[0]
            env = {k: v for k, v in allowed.items() if v is not None}
            argv = ['/usr/bin/env', '-i', 'SFC_WORKER=1', *[f'{k}={v}' for k, v in sorted(env.items())], *argv]
        begin, tick = now(), time.monotonic()
        try:
            code = f.execute(card, 'checks', argv, wt, log, f.c['verify_timeout'],
                                       accounting={'reuse_miss_reason': miss if current else reason,
                                                   'head': head, 'base': base},
                                       observer=f.activity.observe(card, 'checks') if report_activity else None)
        except BaseException:
            write_json(folder / 'interruption.json', {'head': head, 'base': base,
                       'window': f.watcher.session['id'] if f.watcher else None,
                       'config_digest': f.config_digest, 'task_digest': f.task_digest(card),
                       'completed_checks': records, 'interrupted_check': index + 1, 'log': str(log)})
            raise
        finished, command_elapsed = now(), time.monotonic() - tick
        after, _ = binding(f, card, wt, base, command)
        if current != after:
            write_json(folder / 'invalidated.json', {'version': VERSION, 'command': command,
                       'exit_code': code, 'log': str(log), 'log_sha256': checksum(log, fingerprint_budget(f)),
                       'before': current, 'after': after, 'time': now()})
            raise Halt('Verification source/environment/dependency bindings changed during execution')
        try:
            artifacts = inventory(wt, p['artifacts'], fingerprint_budget(f)) if p else {}
        except (OSError, ValueError):
            raise Halt('Verification required artifacts missing or unsupported')
        value = {'version': VERSION, 'kind': 'execution', 'id': uuid.uuid4().hex,
                 'repository': f.c['repo'], 'head': head, 'base': base,
                 'tree': git(wt, 'rev-parse', 'HEAD^{tree}'),
                 'config_digest': f.config_digest, 'task_digest': f.task_digest(card),
                 'command': command, 'exit_code': code, 'log': str(log), 'log_sha256': checksum(log, fingerprint_budget(f)),
                 'started': begin, 'finished': finished, 'elapsed_seconds': command_elapsed,
                 'binding': current, 'artifacts': artifacts, 'worktree': str(Path(wt).resolve()),
                 'reason': miss if current else reason}
        records.append(immutable(check_dir, value, fingerprint_budget(f)))
        if code:
            break
    receipt = {'version': VERSION, 'kind': 'verification', 'id': folder.name, 'time': now(),
               'repository': f.c['repo'], 'common_dir': str(common_dir(wt).resolve()),
               'head': head, 'base': base, 'tree': git(wt, 'rev-parse', 'HEAD^{tree}'),
               'checks': records, 'worktree': str(Path(wt).resolve()),
               'config_digest': f.config_digest, 'task_digest': f.task_digest(card),
               'elapsed_seconds': time.monotonic() - started,
               'passed': len(records) == len(f.c['verify_commands']) and all(v['exit_code'] == 0 for v in records)}
    # Never publish successful evidence before source-integrity validation.
    if git(wt, 'rev-parse', 'HEAD') != head or git(wt, 'diff', 'HEAD'):
        write_json(folder / 'invalidated.json', receipt)
        raise Halt('Verification modified tracked source; results invalidated')
    receipt = immutable(folder, receipt, fingerprint_budget(f))
    f.update(card, verification=receipt)
    register(receipt, wt, f)
    if report_activity and not receipt['passed']:
        f.activity.report(card, 'Failed', 'check_failed')
    f.event(card, 'Project checks: ' + ', '.join(
        f"{i + 1} reused {v['reuse']['id']}" if v.get('reuse') else f"{i + 1} executed: {v['reason']}"
        for i, v in enumerate(records)))
    return receipt['passed']
