"""Record chat work and reconcile its GitHub Project representation."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
from pathlib import Path
import re

from . import pickup
from .common import Busy, Halt, common_dir, exclusive, git, load_config, now, read_json, repo_root, write_json
from .factory import check_explanation, checks_pass
from .priority import create_ticket
from .ticket_coordination import creation_lock, recorded_request
from .work_types import TYPES

STAGES = ('Building', 'QA', 'Review', 'Blocked', 'Done')
ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,69}')


def chat_mode(root):
    return (root / '.sfc/config.json').is_file()


def project_root(cwd):
    """Resolve the project independently of any saved watcher authorization."""
    root = repo_root(cwd)
    if chat_mode(root):
        return root
    main = git(root, 'worktree', 'list', '--porcelain', '-z').split('\0', 1)[0]
    if main.startswith('worktree '):
        return Path(main.removeprefix('worktree ')).resolve()
    return root


def enable(root):
    with exclusive(root), creation_lock(root):
        config = load_config(root)
        if config.get('workflow_mode') != 'chat':
            write_json(root / '.sfc/config.json', {**config, 'workflow_mode': 'chat'})
    return {'mode': 'chat', 'repository': config['repo'], 'project': config['project']}


def public_text(value, label, limit=2000):
    if (not isinstance(value, str) or not value.strip() or len(value) > limit or
            '\0' in value or '<!--' in value):
        raise Halt(f'{label} requires a brief, repository-safe summary')
    return value.strip()


def scope(config):
    return {k: config[k] for k in ('repo', 'project', 'status_field', 'base_branch')}


def directory(root):
    return common_dir(root) / 'sfc-chat'


def task_path(root, task):
    if not isinstance(task, str) or ID.fullmatch(task) is None:
        raise Halt('Use a stable task ID of up to 70 letters, numbers, dashes or underscores')
    return directory(root) / (task + '.json')


@contextmanager
def local_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Busy('A chat tracking update is already in progress. Retry at the next checkpoint.') from exc
        yield


def view(record):
    latest = record['events'][-1]
    return {'task': record['task'], 'title': record['title'], 'issue': record.get('issue'),
            'pr': record.get('pr'), 'pull_request': record.get('pull_request'),
            'desired_stage': latest['stage'],
            'latest_summary': latest['summary'] or record['summary'],
            'board_stage': record.get('board_stage'),
            'pending': [event['id'] for event in record['events'] if not event.get('synced')]}


def read_task(path, config):
    if not path.exists():
        raise Halt('Unknown chat task. Start the task before updating it.')
    record = read_json(path)
    if record.get('version') != 1 or record.get('scope') != scope(config):
        raise Halt('Tracking scope changed. Review the retained task before syncing.')
    return record


def status(root, config, *, task=None, prefix=None):
    scope(config)
    if task is not None:
        return view(read_task(task_path(root, task), config))
    if prefix is None or ID.fullmatch(prefix) is None:
        raise Halt('Provide an exact task ID or chat prefix')
    return {'tasks': [view(read_task(path, config)) for path in sorted(directory(root).glob(prefix + '-*.json'))]}


def start(root, config, *, task, title, summary, work_type=None, acceptance=(), issue=None):
    path = task_path(root, task)
    binding = scope(config)
    title = public_text(title, 'Title', limit=160)
    summary = public_text(summary, 'Goal')
    if '\n' in title:
        raise Halt('Use a one-line issue title')
    if issue is not None and (type(issue) is not int or issue <= 0):
        raise Halt('Use a positive issue number from the configured Project')
    if issue is None and (work_type not in TYPES or not acceptance):
        raise Halt('A new chat issue needs a work type and observable acceptance criteria')
    acceptance = [public_text(value, 'Acceptance criterion', limit=500) for value in acceptance]
    if any('\n' in value for value in acceptance):
        raise Halt('Pass each acceptance criterion as a separate one-line value')
    intent = {'title': title, 'summary': summary, 'work_type': work_type,
              'acceptance': acceptance, 'requested_issue': issue}
    with local_lock(directory(root) / 'catalog.lock'):
        if path.exists():
            record = read_task(path, config)
            if any(record[k] != v for k, v in intent.items()):
                raise Halt('The task ID is already bound to different work')
            return view(record)
        if issue is not None:
            for existing in directory(root).glob('*.json'):
                other = read_json(existing)
                if (other.get('scope') == binding and issue in
                        (other.get('requested_issue'), (other.get('issue') or {}).get('number'))):
                    raise Halt('This issue already has a chat task. Reuse it: ' + other['task'])
        record = {'version': 1, 'scope': binding, 'task': task, 'since': now(), **intent,
                  'events': [{'id': 'start', 'stage': 'Building', 'summary': None}]}
        write_json(path, record)
    return view(record)


def update(root, config, *, task, event_id, stage, summary, pr=None, local_done=False):
    path = task_path(root, task)
    if not isinstance(event_id, str) or ID.fullmatch(event_id) is None or event_id == 'start' or stage not in STAGES:
        raise Halt('Use a new event ID and a supported chat milestone')
    summary = public_text(summary, 'Milestone evidence')
    if pr is not None and (type(pr) is not int or pr <= 0):
        raise Halt('Use a positive pull request number')
    event = {'id': event_id, 'stage': stage, 'summary': summary, 'pr': pr, 'local_done': local_done}
    with local_lock(path.with_suffix('.lock')):
        record = read_task(path, config)
        for previous in record['events']:
            if previous['id'] == event_id:
                if any(previous.get(k) != v for k, v in event.items()):
                    raise Halt('This milestone ID already records different evidence')
                return view(record)
        if record['events'][-1]['stage'] == 'Done':
            raise Halt('This chat task is complete')
        if pr and record.get('pr') and record['pr'] != pr:
            raise Halt('The chat task is already tied to another pull request')
        linked = pr or record.get('pr')
        if (local_done and (stage != 'Done' or linked) or
                stage == 'Done' and not (local_done or linked)):
            raise Halt('Done requires a linked merged pull request or an explicit local completion')
        if linked:
            record['pr'] = linked
        record['events'].append(event)
        write_json(path, record)
    return view(record)


def bind_issue(gh, root, path, record):
    number = record['requested_issue']
    if number is None:
        request = 'chat-' + record['task']
        receipt_path = recorded_request(root, request)
        if receipt_path is None:
            body = record['summary'] + '\n\n## Acceptance criteria\n' + '\n'.join(
                '- [ ] ' + value for value in record['acceptance'])
            result = create_ticket(gh, root, request_id=request, title=record['title'], body=body,
                                   priority='P2', work_type=record['work_type'],
                                   reason='Chat task tracking. Priority is for board visibility.',
                                   unknowns='Delivery urgency was not assessed.')
            receipt_path = Path(result['receipt'])
        receipt = read_json(receipt_path)
        if not receipt.get('issue') or not receipt.get('item_id'):
            raise Halt('Ticket creation needs reconciliation. Inspect the retained ticket receipt.')
        # A lost response may leave verified identities in an incomplete ticket
        # receipt. The tracker only reflects milestones on a live matching item.
        number = receipt['issue']['number']
    cards = pickup.cards(gh, [number])
    if len(cards) != 1:
        raise Halt('The issue is missing or ambiguous on the configured Project')
    card = cards[0]
    if record['requested_issue'] is None and (
            card['item_id'] != receipt['item_id'] or card['id'] != receipt['issue']['id'] or
            card['_pickup_scope']['project']['id'] != receipt['project'] or receipt['repo'] != gh.c['repo']):
        raise Halt('Created issue identity differs from the creation receipt')
    record['issue'] = {k: card[k] for k in ('id', 'number', 'url', 'item_id')}
    record['project'] = card['_pickup_scope']['project']
    record['board_stage'] = card['status']
    write_json(path, record)


def verified_card(gh, record):
    pinned = pickup.scope(gh)
    if pinned['project'] != record['project']:
        raise Halt('The configured Project or its status choices changed')
    issue = record['issue']
    return pickup.card(gh, issue['item_id'], pinned, expected=issue)


def observe_pr(gh, number, base):
    pr = gh.chat_pr(number)
    expected = f"https://github.com/{gh.c['repo']}/pull/{number}"
    if (not isinstance(pr, dict) or type(pr.get('number')) is not int or pr['number'] != number or
            pr.get('baseRefName') != base or not isinstance(pr.get('url'), str) or
            pr['url'].casefold() != expected.casefold() or
            pr.get('state') not in ('OPEN', 'CLOSED', 'MERGED')):
        raise Halt('The linked pull request repository, base, or state could not be verified')
    checks = pr.get('statusCheckRollup')
    if not isinstance(checks, list) or any(not isinstance(check, dict) for check in checks):
        ci = 'unknown'
    elif not checks:
        ci = 'not-reported'
    elif checks_pass(pr):
        ci = 'passed'
    else:
        ci = 'failed' if check_explanation(pr)['code'] == 'ci_failed' else 'running'
    return {'url': expected, 'state': pr['state'], 'draft': pr.get('isDraft'),
            'head': pr.get('headRefOid'), 'ci': ci, 'observed_at': now()}


def sync_stage(gh, path, record, card, event):
    stage = event['stage']
    current = card['status']
    if current == 'Done' and card['state'] == 'CLOSED' and stage != 'Done':
        record['board_stage'] = current
        return
    if current not in (record['board_stage'], stage):
        raise Halt('The Project status changed outside chat tracking')
    if current != stage:
        gh.move(card, stage)
        card = verified_card(gh, record)
        if card['status'] != stage:
            raise Halt('The Project status update could not be verified')
    record['board_stage'] = stage
    write_json(path, record)
    if stage == 'Done':
        standard = gh.done_status_snapshot(card)
        if standard is not None:
            field, current = standard
            done = field['options']['Done']
            if current != done:
                gh.write_done_status(field)
                if gh.done_status_snapshot(card) != (field, done):
                    raise Halt('GitHub Status Done could not be verified')


def sync_note(gh, path, record, event, pr_url):
    if not event['summary'] or event.get('note') == 'verified':
        return
    endpoint = f"repos/{gh.c['repo']}/issues/{record['issue']['number']}/comments"
    marker = f"<!-- sfc:chat:{record['task']}:{event['id']} -->"
    body = f"**{event['stage']}**\n\n{event['summary']}" + ('\n\n' + pr_url if pr_url else '')
    body += '\n\n' + marker
    if event.get('note') == 'intent':
        pages = gh.call('api', endpoint + '?per_page=100', '--paginate', '--slurp')
        matches = [comment for page in pages for comment in page if marker in comment.get('body', '')]
        if len(matches) != 1 or matches[0]['body'] != body:
            raise Halt('A milestone note needs readback. The original write will not be repeated.')
    else:
        event['note'] = 'intent'
        write_json(path, record)
        result = gh.call('api', endpoint, '--method', 'POST', '--input', '-', input=json.dumps({'body': body}))
        if not result.get('id') or result.get('body') != body:
            raise Halt('The milestone note response did not match')
    event['note'] = 'verified'
    write_json(path, record)


def sync(gh, root, task):
    path = task_path(root, task)
    with exclusive(root), local_lock(path.with_suffix('.lock')):
        record = read_task(path, load_config(root))
        if scope(gh.c) != record['scope']:
            raise Halt('Tracking configuration changed before sync')
        number = record.get('pr')
        latest = record['events'][-1]
        follow_pr = number and latest['stage'] != 'Done'
        if not follow_pr and all(event.get('synced') for event in record['events']):
            return {'state': 'synced', **view(record)}
        try:
            if not record.get('issue'):
                bind_issue(gh, root, path, record)
            pull = observe_pr(gh, number, record['scope']['base_branch']) if number else None
            if pull:
                record['pull_request'] = pull
                if follow_pr and pull['state'] == 'MERGED':
                    # This ID is outside the public milestone-ID alphabet. Save it before any board write.
                    record['events'].append({'id': 'sfc:pr-merged', 'stage': 'Done', 'pr': number,
                                             'summary': 'GitHub confirmed the linked pull request merged.'})
                    write_json(path, record)
            for event in record['events']:
                if event.get('synced'):
                    continue
                if pull and event['stage'] == 'Done' and pull['state'] != 'MERGED':
                    raise Halt('The linked pull request has not merged')
                url = pull['url'] if pull and (event.get('pr') or event['stage'] in ('Review', 'Done')) else None
                sync_stage(gh, path, record, verified_card(gh, record), event)
                sync_note(gh, path, record, event, url)
                event['synced'] = now()
                write_json(path, record)
        except (Halt, OSError, ValueError, KeyError) as exc:
            record['last_sync_error'] = str(exc)
            write_json(path, record)
            return {'state': 'pending', **view(record), 'receipt': str(path)}
        record.pop('last_sync_error', None)
        write_json(path, record)
        return {'state': 'synced', **view(record)}
