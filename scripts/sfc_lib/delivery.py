"""Local delivery accounting. Observations never authorize or validate work.

Original execution records are write-once; interrupted reservations stay unknown.
Reports can be rebuilt offline without Codex, GitHub or project configuration.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import json
from pathlib import Path
import time
import uuid

from .common import Halt, _command_observer, _process_observer, common_dir, now, read_json, write_json
from .verification import checksum

STAGES = ('queueing', 'build', 'checks', 'review', 'coordination', 'publication')
TOKENS = ('input_tokens', 'output_tokens', 'cached_input_tokens')


def capture_gap(f, detail):
    if f.run_dir is not None:
        with f.mutex:
            gaps = f.state.setdefault('delivery_capture_gaps', {}).setdefault(f.run_dir.name, [])
            if len(gaps) < 100:
                gaps.append(detail)
            elif len(gaps) == 100:
                gaps.append('Further delivery capture gaps were omitted')


def append_command(f, value):
    # Append-only events retain a hard-interrupted command's start. Never retain
    # stdin, environment, stdout or stderr. Startup events wait for run admission.
    with f.mutex:
        folder = getattr(f, '_delivery_command_folder', None)
        if folder is None:
            f._delivery_startup_commands.append(value)
            return
        try:
            with (folder / 'delivery-commands.jsonl').open('a') as handle:
                handle.write(json.dumps(value) + '\n')
        except (OSError, ValueError, TypeError) as exc:
            capture_gap(f, 'Administrative command event unavailable: ' + type(exc).__name__)


def flush_commands(f):
    with f.mutex:
        f._delivery_command_folder = f.run_dir
        for value in f._delivery_startup_commands:
            append_command(f, value)
        f._delivery_startup_commands.clear()


@contextmanager
def command_scope(f, card=None, stage='coordination', *, startup=False):
    if startup:
        f._delivery_command_folder = None
        f._delivery_startup_commands = []

    @contextmanager
    def observe(argv, cwd):
        value = {'id': uuid.uuid4().hex, 'issue': card['number'] if card else None,
                 'stage': stage, 'argv': [str(v) for v in argv],
                 'cwd': str(cwd) if cwd is not None else None, 'started': now()}
        append_command(f, {'event': 'started', **value})
        tick = time.monotonic()
        try:
            yield value
        except BaseException as exc:
            value['error_type'] = type(exc).__name__
            raise
        finally:
            append_command(f, {'event': 'completed', **value, 'finished': now(),
                               'elapsed_seconds': time.monotonic() - tick})

    token = _command_observer.set(observe)
    try:
        yield
    finally:
        _command_observer.reset(token)


def stamp(value):
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    except (ValueError, TypeError, AttributeError):
        return None


def union(intervals):
    total, end = 0.0, None
    for start, stop in sorted(intervals):
        if stop < start:
            continue
        total += max(0, stop - max(start, end if end is not None else start))
        end = max(stop, end if end is not None else stop)
    return total


def settings(argv):
    """Requested settings only: inherited/effective provider settings are unknown."""
    result = dict.fromkeys(('model', 'provider', 'reasoning_effort', 'service_tier'))
    for i, arg in enumerate(argv[:-1]):
        if arg in ('-m', '--model'):
            result['model'] = argv[i + 1]
        if arg in ('-c', '--config'):
            key, _, value = argv[i + 1].partition('=')
            key = {'model_reasoning_effort': 'reasoning_effort', 'model_provider': 'provider'}.get(key, key)
            if key in result:
                try:
                    result[key] = json.loads(value)
                except ValueError:
                    result[key] = value
    return result


@contextmanager
def observation(f, card, stage, **metadata):
    if f.run_dir is None:
        # Completion-only reconciliation can be invoked outside a run. Keep its
        # existing checks/authority; there is no current run to charge or invent.
        yield {}
        return
    folder = f.run_dir / 'delivery' / uuid.uuid4().hex
    record = {'version': 1, 'id': folder.name, 'issue': card['number'],
              'run_id': f.run_dir.name, 'stage': stage, 'started': now(),
              'config_digest': f.config_digest, 'task_digest': f.task_digest(card), **metadata}
    try:
        write_json(folder / 'reserved.json', record)
    except (OSError, ValueError, TypeError) as exc:
        capture_gap(f, record['id'] + ': observation reservation unavailable: ' + type(exc).__name__)
    record['_observation_folder'] = str(folder)
    tick = time.monotonic()
    record['started'] = now()
    try:
        yield record
    except BaseException as exc:
        record['error_type'] = type(exc).__name__
        raise
    finally:
        record.pop('_observation_folder', None)
        record.update(finished=now(), elapsed_seconds=time.monotonic() - tick)
        try:
            if record.get('log') and Path(record['log']).is_file():
                record['log_sha256'] = checksum(record['log'])
        except (OSError, ValueError, Halt) as exc:
            record['accounting_gap'] = 'Log fingerprint unavailable: ' + type(exc).__name__
        try:
            write_json(folder / 'completed.json', record)
        except (OSError, ValueError, TypeError) as exc:
            capture_gap(f, record['id'] + ': observation completion unavailable: ' + type(exc).__name__)


def execute(f, card, stage, argv, cwd, log, timeout, prompt=None, *, accounting=None, **kwargs):
    with observation(f, card, stage, kind='process', argv=argv, cwd=str(cwd), log=str(log),
                     worker=kwargs.get('worker_events', False), requested=settings(argv),
                     **(accounting or {})) as record:
        def launched():
            record.update(launch_observed=True, launched_at=now())
            folder = record.get('_observation_folder')
            if folder:
                try:
                    write_json(Path(folder) / 'launched.json', {
                        k: v for k, v in record.items() if not k.startswith('_')})
                except (OSError, ValueError, TypeError) as exc:
                    record['accounting_gap'] = 'Launch receipt unavailable: ' + type(exc).__name__
        token = _process_observer.set(launched)
        try:
            code = f.processes.execute(argv, cwd, log, timeout, prompt, **kwargs)
            record['exit_code'] = code
            return code
        finally:
            _process_observer.reset(token)


def observe_queue(f, cards):
    if (f.run_dir is None or f.state.get('outcome') != 'running' or
            f.state.get('run_id') != f.run_dir.name):
        return  # A read-only preview cannot create accounting state.
    with f.mutex:
        changed = False
        queue = f.state.setdefault('delivery_queue', {})
        periods = f.state.setdefault('delivery_queue_intervals', {})
        run_id, observed = f.run_dir.name, now()
        ready = {str(card['number']) for card in cards if card['status'] == 'Ready'}
        for number, spans in periods.items():
            if spans and spans[-1].get('run_id') == run_id and spans[-1].get('finished') is None:
                if number not in ready:
                    spans[-1]['finished'] = observed
                    changed = True
        for number in sorted(ready, key=int):
            spans = periods.setdefault(number, [])
            if not spans or spans[-1].get('run_id') != run_id or spans[-1].get('finished') is not None:
                queue.setdefault(number, observed)
                spans.append({'run_id': run_id, 'started': observed, 'finished': None})
                changed = True
        if changed:
            try:
                f.save()
            except OSError as exc:
                capture_gap(f, 'Ready observation unavailable: ' + type(exc).__name__)


def queue_intervals(state, number, executions, runs, *, run_id=None, cutoff=None):
    """Ready spans are bounded by the observing run, withdrawal or dispatch."""
    periods = state.get('delivery_queue_intervals', {}).get(str(number))
    launches = [(r.get('run_id'), stamp(r.get('launched_at') or r.get('started')))
                for r in executions if str(r.get('issue')) == str(number)
                and r['execution_observed']]
    if periods is None:
        # Legacy Ready evidence had no observed run or withdrawal time. Only
        # infer a span if a confirmed dispatch supplies a known endpoint.
        a = stamp(ready_observed(state, number))
        ends = [t for _, t in launches if t is not None and a is not None and t >= a]
        if run_id is None and ends:
            return [(a, min(ends + ([cutoff] if cutoff is not None else [])))]
        return []
    result = []
    for period in periods:
        observed_run = period.get('run_id')
        if run_id is not None and observed_run != run_id:
            continue
        a = stamp(period.get('started'))
        if a is None:
            continue
        b = stamp(period.get('finished'))
        end = stamp(runs.get(observed_run, {}).get('finished'))
        limits = [v for v in (b, end) if v is not None]
        limits += [t for rid, t in launches if rid == observed_run and t is not None and t >= a]
        if cutoff is not None and limits:
            limits.append(cutoff)
        if limits and min(limits) >= a:
            result.append((a, min(limits)))
    return result


def ready_observed(state, number):
    return (state.get('delivery_queue', {}).get(str(number)) or
            state.get('cards', {}).get(str(number), {}).get('delivery_ready_observed'))


def native_review_command(argv):
    """Recognize the native adapter's recorded command, including its stdin prompt."""
    return (argv[:2] == ['codex', 'exec'] and argv[-5:-2] == ['review', '--json', '--output-last-message']
            and argv[-1] == '-')


def stream(path, *, native=False, gaps=None):
    """CLI JSON usage belongs to turns; item start/update/complete is one command.

    Keep available subtotals and per-field unknown flags for partial usage.
    Native review can emit zero-filled usage when no counters reach the CLI.
    Streamed commands lack reliable durations and are not added to parent time.
    """
    commands, turns, thread = {}, {}, None
    malformed = unavailable = False

    def malformed_event(line_number):
        if gaps is not None and not gaps:
            gaps.append(f'malformed worker telemetry at line {line_number}')

    if not path:
        return [], dict.fromkeys(TOKENS), False, dict.fromkeys(TOKENS, True)
    with Path(path).open(errors='replace') as handle:
        turn = 0
        native_extra_start = False
        seen = set()
        for line_number, line in enumerate(handle, 1):
            if len(line) > 2_000_000:
                malformed = True
                malformed_event(line_number)
                continue
            try:
                event = json.loads(line)
            except (ValueError, RecursionError):
                if line.lstrip().startswith('{'):
                    malformed = True
                    malformed_event(line_number)
                continue
            if not isinstance(event, dict):
                continue
            if event.get('type') == 'thread.started':
                identifier = event.get('thread_id')
                if not isinstance(identifier, str):
                    malformed = True
                    malformed_event(line_number)
                    continue
                thread = identifier
            if event.get('type') == 'turn.started':
                # Native review may report both its synthetic and server start.
                if native and turn == 1 and not turns and not native_extra_start:
                    native_extra_start = True
                else:
                    turn += 1
            item = event.get('item', {})
            if isinstance(item, dict) and item.get('type') == 'command_execution':
                if not isinstance(item.get('id'), str) or not item['id']:
                    malformed = True
                    malformed_event(line_number)
                    continue
                # A queued item alone does not prove command execution.
                identity = (thread, item['id'])
                if event.get('type') == 'item.completed' or (
                        event.get('type') == 'item.started' and identity not in commands):
                    commands[identity] = {
                        'thread': thread, 'id': item['id'], 'command': item.get('command'),
                        'exit_code': item.get('exit_code'), 'elapsed_seconds': None}
            if event.get('type') == 'turn.completed':
                identifier = event.get('turn_id', turn)
                if (not isinstance(event.get('usage'), dict) or not isinstance(identifier, (str, int))
                        or isinstance(identifier, bool)):
                    malformed = True
                    malformed_event(line_number)
                    continue
                identity = (thread, identifier)
                raw = event['usage']
                usage = {k: raw.get(k) if type(raw.get(k)) is int and raw[k] >= 0 else None for k in TOKENS}
                # A duplicated completion is not another paid turn. Conflicting
                # usage is ambiguous; count none of its values.
                if identity in turns and turns[identity] != usage:
                    seen.add(identity)
                    malformed = True
                    malformed_event(line_number)
                turns[identity] = usage
                # Codex emits default-zero usage when the delegated native review
                # supplies no token notification to its parent exec turn.
                if (native and usage['input_tokens'] == usage['output_tokens'] == 0 and
                        not any(type(raw.get(k)) is int and raw[k] > 0 for k in
                            ('cached_input_tokens', 'cache_write_input_tokens', 'reasoning_output_tokens'))):
                    seen.add(identity)
                    unavailable = True
        for identity in seen:
            turns[identity] = dict.fromkeys(TOKENS)
    if unavailable and gaps is not None:
        gaps.append('native review token counters unavailable (zero-filled CLI usage)')
    complete = bool(turns) and len(turns) >= turn and not malformed and not unavailable
    usage = {k: sum(v[k] for v in turns.values() if v[k] is not None)
             if any(v[k] is not None for v in turns.values()) else None for k in TOKENS}
    unknown = {k: not complete or any(v[k] is None for v in turns.values()) for k in TOKENS}
    return list(commands.values()), usage, complete, unknown


def token_usage(executions):
    workers = [r for r in executions if r.get('worker') and r['execution_observed']]
    return {k: {'known': sum(r['tokens'][k] or 0 for r in workers),
                'unknown_executions': sum(r['token_unknowns'][k] or not r['usage_complete']
                                          for r in workers)} for k in TOKENS}


def process_totals(executions):
    observed = [r for r in executions if r.get('kind') == 'process' and r['execution_observed']]
    return {'calls': len(observed),
            'unconfirmed_launches': sum(not r['execution_observed'] for r in executions),
            'compute_seconds': sum(r.get('elapsed_seconds') or 0 for r in observed),
            'unknown_durations': sum(r.get('elapsed_seconds') is None for r in observed),
            'tokens': token_usage(observed)}


def inspection_overlap(executions):
    """Account actual process intersections, never overlap between unrelated cards.

    This is historical telemetry, not an acceptance or qualification decision.
    Failed/cancelled processes count too; an unfinished interval remains unknown.
    """
    inspectors = [r for r in executions if r.get('lane') == 'inspection']
    assessments = [r for r in executions if r.get('lane') == 'review' and r.get('inspection_sha256')]
    security = [r for r in executions if r.get('lane') == 'security']
    checks = [r for r in executions if r['stage'] == 'checks' and r.get('kind') == 'process']
    pins = ('run_id', 'issue', 'head', 'base', 'config_digest', 'task_digest')
    intervals, unknown = [], 0
    for inspector in inspectors:
        if not inspector['execution_observed']:
            continue
        if any(inspector.get(key) is None for key in pins):
            unknown += 1
            continue
        matching = [row for row in checks if all(row.get(key) == inspector[key] for key in pins)]
        a, b = stamp(inspector.get('started')), stamp(inspector.get('finished'))
        if a is None or b is None or any(not row['execution_observed'] or stamp(row.get('started')) is None or
                                       stamp(row.get('finished')) is None for row in matching):
            unknown += 1
        if a is None or b is None:
            continue
        for row in matching:
            if not row['execution_observed']:
                continue
            c, d = stamp(row.get('started')), stamp(row.get('finished'))
            if c is not None and d is not None and max(a, c) < min(b, d):
                intervals.append((max(a, c), min(b, d)))
    post_checks = []
    for review in assessments:
        if not review['execution_observed'] or any(review.get(key) is None for key in pins):
            continue
        for specialist in security:
            if not specialist['execution_observed'] or any(specialist.get(key) != review[key] for key in pins):
                continue
            starts = [stamp(row.get('started')) for row in (review, specialist)]
            ends = [stamp(row.get('finished')) for row in (review, specialist)]
            if None not in starts + ends and max(starts) < min(ends):
                post_checks.append((max(starts), min(ends)))
    return {'wall_seconds': union(intervals), 'assessment_wall_seconds': union(post_checks),
            'unknown_intervals': unknown,
            'inspection': process_totals(inspectors), 'final_assessment': process_totals(assessments)}


def report(root, *, run_id=None):
    root = Path(root).resolve()
    directory = root / '.sfc'
    state = read_json(directory / 'state.json') if (directory / 'state.json').exists() else {'cards': {}}
    run_id = run_id or state.get('run_id')
    unknowns, records, references, sources = [], {}, [], []
    corrections, final_assessments = {}, []

    def read(path, *, retain_source=True):
        try:
            value = read_json(path)
            if not isinstance(value, dict):
                raise ValueError('expected an object')
            if retain_source:
                sources.append({'path': str(path), 'sha256': checksum(path)})
            return value
        except (OSError, ValueError) as exc:
            unknowns.append(str(path) + ': ' + type(exc).__name__)
            return {}

    for folder in sorted((directory / 'runs').glob('*/delivery/*')):
        for name in ('completed.json', 'launched.json', 'reserved.json'):
            path = folder / name
            if not path.exists():
                continue
            value = read(path)
            if (value.get('id') == folder.name and value.get('stage') in STAGES and
                    type(value.get('issue')) is int and value.get('run_id') == folder.parent.parent.name):
                records[value['id']] = {**value, 'evidence': str(path)}
                break
            unknowns.append(str(path) + ': incomplete observation identity')
    administrative = {}
    for path in sorted((directory / 'runs').glob('*/delivery-commands.jsonl')):
        sources.append({'path': str(path), 'sha256': checksum(path)})
        with path.open() as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                    identity = (path.parent.name, row['id'])
                    if row.get('event') == 'completed' or identity not in administrative:
                        administrative[identity] = {**row, 'run_id': path.parent.name, 'evidence': str(path)}
                except (ValueError, KeyError, TypeError):
                    unknowns.append(str(path) + ': incomplete command event')
    # Legacy immutable receipts remain useful; never count a reused check as a
    # new execution. A log path identifies the original across receipt copies.
    def log_identity(path):
        return str(Path(path).resolve()) if isinstance(path, str) and path else None

    logs = {log_identity(r.get('log')) for r in records.values() if r.get('log')}
    for path in sorted((directory / 'runs').glob('*/issue-*/**/receipt.json')):
        value = read(path)
        receipt_run = path.relative_to(directory / 'runs').parts[0]
        if value.get('kind') == 'correction-assessment':
            corrections[sources[-1]['sha256']] = {
                'receipt': str(path), 'run_id': receipt_run, 'head': value.get('head'),
                'origin': value.get('origin'), 'kept': value.get('kept', {}),
                'invalidated': value.get('invalidated', {})}
        if value.get('kind') == 'review' and value.get('lane') == 'review':
            result = value.get('result', {})
            correction = result.get('scope', {}).get('correction')
            if correction and result.get('verdict') == 'pass':
                final_assessments.append((receipt_run, str(path), correction, result.get('coverage', [])))
        if value.get('kind') == 'verification':
            for check in value.get('checks', []):
                references.append({'receipt': str(path), 'run_id': path.relative_to(directory / 'runs').parts[0],
                                   'command': check.get('command'), 'reuse': check.get('reuse'),
                                   'reuse_miss_reason': None if check.get('reuse') else check.get('reason'),
                                   'log': check.get('log')})
        candidates = value.get('checks', []) if value.get('kind') == 'verification' else [value]
        for check in candidates:
            log = check.get('log')
            if check.get('reuse') or not log or log_identity(log) in logs:
                continue
            if not check.get('lane') and check.get('kind') != 'execution':
                continue
            logs.add(log_identity(log))
            lane = check.get('lane')
            records['legacy:' + log] = {
                'id': 'legacy:' + log, 'kind': 'process', 'log': log,
                'run_id': path.relative_to(directory / 'runs').parts[0],
                'issue': int(path.relative_to(directory / 'runs').parts[1].removeprefix('issue-')),
                'stage': 'build' if lane == 'build' else 'review' if lane else 'checks',
                'worker': bool(lane), 'started': check.get('started'), 'finished': check.get('finished'),
                'elapsed_seconds': check.get('elapsed_seconds'), 'requested': {},
                'evidence': str(path), 'exit_code': check.get('exit_code'),
                'log_sha256': check.get('log_sha256'), 'adapter': check.get('provider')}
    executions = list(records.values())
    for row in executions:
        row['commands'], row['tokens'], row['usage_complete'] = [], dict.fromkeys(TOKENS), False
        row['token_unknowns'] = dict.fromkeys(TOKENS, True)
        row['execution_observed'] = row.get('exit_code') is not None or row.get('launch_observed') is True
        if row.get('accounting_gap'):
            unknowns.append(row['id'] + ': ' + row['accounting_gap'])
        if row.get('kind') == 'process' and not row['execution_observed']:
            unknowns.append(row['id'] + ': process launch unconfirmed')
        if row.get('worker'):
            try:
                if not row.get('log_sha256') or checksum(row['log']) != row['log_sha256']:
                    raise ValueError('missing or changed log hash')
                gaps = []
                adapter = row.get('adapter')
                row['commands'], row['tokens'], row['usage_complete'], row['token_unknowns'] = stream(
                    row['log'], native=isinstance(adapter, dict) and adapter.get('provider') == 'codex-native',
                    gaps=gaps)
                unknowns.extend(row['id'] + ': ' + gap for gap in gaps)
                row['usage_complete'] = row['usage_complete'] and row.get('exit_code') == 0 and not row.get('error_type')
            except (OSError, ValueError, TypeError, KeyError) as exc:
                unknowns.append(row['id'] + ': usage unavailable: ' + str(exc))
        if row.get('elapsed_seconds') is None:
            unknowns.append(row['id'] + ': duration unknown (incomplete/legacy observation)')
    runs = {}
    for path in sorted((directory / 'runs').glob('*/summary.json')):
        # The current summary acquires this report's pointer after generation;
        # hashing it here would create an immediately stale circular reference.
        value = read(path, retain_source=path.parent.name != run_id)
        runs[path.parent.name] = value
    runs.setdefault(state.get('run_id'), state)
    selected = [r for r in executions if r['run_id'] == run_id]
    window = runs.get(run_id, {})
    unknowns.extend(window.get('delivery_capture_gaps', {}).get(run_id, []))
    start, end = stamp(window.get('started')), stamp(window.get('finished'))
    intervals = {stage: [] for stage in STAGES}
    for row in selected:
        a, b = stamp(row.get('started')), stamp(row.get('finished'))
        if a is not None and b is not None:
            intervals[row['stage']].append((a, b))
    queue_state = window if 'delivery_queue_intervals' in window else state
    if start is not None and end is not None:
        for number in queue_state.get('delivery_queue_intervals', {}):
            for a, b in queue_intervals(queue_state, number, executions, runs, run_id=run_id, cutoff=end):
                if min(b, end) >= max(a, start):
                    intervals['queueing'].append((max(a, start), min(b, end)))
    known = [v for stage in STAGES if stage != 'coordination' for v in intervals[stage]]
    elapsed = end - start if start is not None and end is not None else None
    stages = {stage: {'wall_seconds': union(intervals[stage]),
                      'summed_process_seconds': sum(r.get('elapsed_seconds') or 0 for r in selected
                                                    if r['stage'] == stage and r.get('kind') == 'process'
                                                    and r['execution_observed']),
                      'unknown_durations': sum(r.get('elapsed_seconds') is None for r in selected
                                               if r['stage'] == stage)} for stage in STAGES}
    stages['coordination']['wall_seconds'] = max(0, elapsed - union(known)) if elapsed is not None else None
    for stage in STAGES:
        stages[stage]['summed_administrative_seconds'] = sum(v.get('elapsed_seconds') or 0
            for v in administrative.values() if v['run_id'] == run_id and v.get('stage') == stage)
        stages[stage]['unknown_administrative_durations'] = sum(v.get('elapsed_seconds') is None
            for v in administrative.values() if v['run_id'] == run_id and v.get('stage') == stage)
    releases = []
    for path in sorted((common_dir(root) / 'sfc-releases').glob('*.json')):
        j = read(path)
        if j.get('workflow_kind') != 'release':
            continue
        members = j.get('changes', {}).get('issues', [])
        ready = [stamp(ready_observed(state, n)) for n in members]
        done = stamp(j.get('delivery_verified_at'))
        created = stamp(j.get('created'))
        release_issue = (j.get('card') or {}).get('number')
        # Work must have begun before the source was frozen. Release activity
        # ends at verification; its completion observation may wrap that point.
        work_cutoff = created if created is not None else done
        releases.append({'release_ticket': j.get('card'), 'work_tickets': members,
                         'candidate': j.get('candidate'), 'boundary': j.get('boundary'),
                         'state': j.get('state'), 'journal': str(path), 'verified_at': j.get('delivery_verified_at'),
                         'ready_observed_to_verified_seconds': done - min(ready) if done is not None and ready and
                         all(t is not None and t <= work_cutoff for t in ready) else None,
                         'ready_to_verified_seconds': None,
                         'check_receipts': {phase: j.get(phase + '_checks')
                                            for phase in ('package', 'preflight', 'artifact')},
                         'check_plan': j.get('check_plan'), 'preflight_outcome': j.get('preflight_outcome'),
                         'effects': j.get('effects', {}), 'gap': j.get('gap')})

        def linked_to_release(row):
            start = stamp(row.get('started'))
            if row['issue'] == release_issue:
                return ((created is None or start is not None and start >= created)
                        and (done is None or start is not None and start <= done)
                        and (not row.get('release_version') or row['release_version'] == j.get('version')))
            if row['issue'] not in members:
                return False
            end = stamp(row.get('finished'))
            return (work_cutoff is None or start is not None and start <= work_cutoff
                    and (end is None or end <= work_cutoff))

        linked = [r for r in executions if linked_to_release(r)]
        durations = {}
        all_intervals = []
        for stage in STAGES:
            spans = [(stamp(r.get('started')), stamp(r.get('finished'))) for r in linked if r['stage'] == stage]
            spans = [(a, min(b, done) if done is not None else b) for a, b in spans
                     if a is not None and b is not None]
            all_intervals.extend(spans)
            durations[stage] = {'wall_seconds': union(spans),
                                'summed_process_seconds': sum(r.get('elapsed_seconds') or 0 for r in linked
                                    if r['stage'] == stage and r.get('kind') == 'process'
                                    and r['execution_observed']),
                                'unknown_durations': sum(r.get('elapsed_seconds') is None for r in linked
                                                          if r['stage'] == stage)}
        queue = []
        for n in members:
            queue.extend(queue_intervals(state, n, linked, runs, cutoff=work_cutoff))
        if release_issue is not None:
            queue.extend(queue_intervals(state, release_issue, linked, runs, cutoff=done))
        durations['queueing']['wall_seconds'] = union(queue)
        observed = releases[-1]['ready_observed_to_verified_seconds']
        durations['coordination']['wall_seconds'] = max(0, observed - union(all_intervals + queue)) if observed is not None else None
        releases[-1].update(stages=durations, execution_ids=[r['id'] for r in linked],
                            compute_seconds=process_totals(linked)['compute_seconds'],
                            unknown_compute_durations=process_totals(linked)['unknown_durations'],
                            inspection_overlap=inspection_overlap(linked),
                            summed_worker_seconds=sum(r.get('elapsed_seconds') or 0 for r in linked
                                                      if r.get('worker') and r['execution_observed']),
                            tokens=token_usage(linked),
                            evidence_basis='Retained supervisor observations, not a fresh remote verification')
    workers = [r for r in selected if r.get('worker') and r['execution_observed']]
    usage = token_usage(selected)
    retained_uses = []
    for receipt_run, path, ref, coverage in final_assessments:
        correction = corrections.get(ref, {})
        for row in coverage:
            kept = correction.get('kept', {}).get(row['criterion'], {})
            if (row['status'] == 'covered' and kept and
                    {'kind': 'retained', 'id': row['criterion'], 'sha256': kept['receipt_sha256']} in row['evidence']):
                retained_uses.append({'run_id': receipt_run, 'final_review': path, 'assessment_sha256': ref,
                                      'criterion': row['criterion'], 'original_review': kept['receipt']})
    run_corrections = [entry for entry in corrections.values() if entry['run_id'] == run_id]
    correction_counts = {'kept': sum(len(entry['kept']) for entry in run_corrections),
                         'invalidated': sum(len(entry['invalidated']) for entry in run_corrections),
                         'used_in_passing_final_review': sum(entry['run_id'] == run_id for entry in retained_uses)}
    run_checks = [entry for entry in references if entry['run_id'] == run_id]
    check_counts = {'executed_in_completed_receipts': sum(not entry['reuse'] for entry in run_checks),
                    'reused_in_completed_receipts': sum(bool(entry['reuse']) for entry in run_checks)}
    recovery = {}
    for number, record in window.get('cards', {}).items():
        actions = [action for action in record.get('blocked_recoveries', [])
                   if action.get('run_id') == run_id or (start is not None and end is not None and
                       any(start <= observed <= end for key in ('reserved_at', 'acknowledged_at', 'finished')
                           if (observed := stamp(action.get(key))) is not None))]
        if actions or record.get('recovery_wait') or (record.get('status') == 'Blocked' and record.get('blocker')):
            recovery[number] = {'blocker': record.get('blocker'), 'blocked_recoveries': actions,
                                'recovery_wait': record.get('recovery_wait'),
                                'handoff_reasons': record.get('handoff_reasons', [])}
    unknowns += ['Actual Ready entry timestamps before first observation are unknown.',
                 'Human intervention count and duration are unknown; coordination includes unattributed waits.',
                 'Effective inherited model/provider/effort/tier and billing/credits are unknown.',
            'Worker command durations and usage of uncompleted turns are unknown.',
            'Administrative commands in uninstrumented external adapters/threads are unknown.']
    return {'version': 1, 'run_id': run_id, 'run_wall_seconds': elapsed, 'stages': stages,
            'blocked_recovery': recovery,
            'slowest_stages': sorted(stages, key=lambda s: stages[s]['wall_seconds'] or 0, reverse=True),
            'summed_worker_seconds': sum(r.get('elapsed_seconds') or 0 for r in workers),
            'compute_seconds': process_totals(selected)['compute_seconds'],
            'unknown_compute_durations': process_totals(selected)['unknown_durations'],
            'inspection_overlap': inspection_overlap(selected),
            'tokens': usage, 'currency_estimate': None, 'billing': None, 'human_intervention_seconds': None,
            'executions': executions, 'current_run_execution_ids': [r['id'] for r in selected],
            'observed_process_count': sum(r.get('kind') == 'process' and r['execution_observed'] for r in selected),
            'administrative_commands': list(administrative.values()),
            'current_run_administrative_count': sum(v['run_id'] == run_id and v.get('exit_code') is not None
                                                     for v in administrative.values()),
            'nested_command_count': sum(len(r['commands']) for r in selected),
            'verification_references': references, 'verification_check_counts': check_counts,
            'correction_coverage': correction_counts, 'correction_assessments': list(corrections.values()),
            'retained_review_references': retained_uses, 'releases': releases, 'sources': sources,
            'unknowns': unknowns, 'timing_basis': 'Supervisor observations; stages may overlap. '
            'Coordination is the unclassified wall-time remainder, not measured human effort. '
            'Stream commands are nested within worker processes, never additional worker time. '
            'Historical executions are retained but excluded from current-run time and usage.'}


def save_report(f):
    # Telemetry is never a release/merge gate and must not replace a scheduler
    # failure. Retain an explicit gap even if report IO itself is unavailable.
    path = f.run_dir / 'delivery-report.json'
    try:
        value = report(f.root, run_id=f.run_dir.name)
        write_json(path, value)
        return {'path': str(path), 'run_id': value['run_id'], 'slowest_stages': value['slowest_stages'],
                'tokens': value['tokens'], 'unknowns': value['unknowns']}
    except Exception as exc:
        gap = {'version': 1, 'run_id': f.run_dir.name, 'path': str(path), 'slowest_stages': [], 'tokens': None,
               'unknowns': ['Delivery report unavailable: ' + type(exc).__name__],
               'reporting_gap': True}
        try:
            write_json(path, gap)
        except Exception:
            gap['path'] = None
        return gap
