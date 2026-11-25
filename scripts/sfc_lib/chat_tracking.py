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
DELIVERY_MODES = ('pr-required', 'local-only')
ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,69}')
PR_URL = re.compile(r'https://github\.com/([A-Za-z0-9-]+/[A-Za-z0-9_.-]+)/pull/([1-9][0-9]{0,9})')


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
    attachment = None
    receipt = record.get('native_pr_attachment')
    if receipt:
        attachment = {'state': receipt['state'], 'url': receipt['binding']['url']}
    if record.get('native_pr_attachment_error'):
        attachment = {**(attachment or {}), 'state': 'pending',
                      'detail': record['native_pr_attachment_error']}
    return {'task': record['task'], 'title': record['title'], 'issue': record.get('issue'),
            'delivery': record.get('delivery'), 'pr': record.get('pr'), 'pr_url': record.get('pr_url'),
            'pull_request': record.get('pull_request'),
            'native_pr_attachment': attachment,
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
    if 'delivery' in record and record['delivery'] not in DELIVERY_MODES:
        raise Halt('Saved delivery choice is invalid. Inspect the retained task.')
    return record


def status(root, config, *, task=None, prefix=None):
    scope(config)
    if task is not None:
        return view(read_task(task_path(root, task), config))
    if prefix is None or ID.fullmatch(prefix) is None:
        raise Halt('Provide an exact task ID or chat prefix')
    return {'tasks': [view(read_task(path, config)) for path in sorted(directory(root).glob(prefix + '-*.json'))]}


def start(root, config, *, task, title, summary, work_type=None, acceptance=(), issue=None, delivery=None):
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
            if delivery is not None and delivery != record.get('delivery'):
                raise Halt('Restarting a task cannot change its delivery choice')
            return view(record)
        if delivery is None:
            delivery = 'pr-required'
        if delivery not in DELIVERY_MODES:
            raise Halt('Invalid delivery choice; use pr-required or local-only')
        if issue is not None:
            for existing in directory(root).glob('*.json'):
                other = read_json(existing)
                if (other.get('scope') == binding and issue in
                        (other.get('requested_issue'), (other.get('issue') or {}).get('number'))):
                    raise Halt('This issue already has a chat task. Reuse it: ' + other['task'])
        record = {'version': 1, 'scope': binding, 'task': task, 'since': now(), 'delivery': delivery, **intent,
                  'events': [{'id': 'start', 'stage': 'Building', 'summary': None}]}
        write_json(path, record)
    return view(record)


def pr_target(repository, number, url):
    if url is None:
        if number is not None:
            return repository.casefold(), number
        return None
    match = PR_URL.fullmatch(url) if isinstance(url, str) else None
    if not match or match[1].split('/')[1] in ('.', '..'):
        raise Halt('Use an exact https://github.com/OWNER/REPOSITORY/pull/NUMBER URL')
    return match[1].casefold(), int(match[2])


def update(root, config, *, task, event_id, stage, summary, pr=None, pr_url=None, local_done=False,
           closes_issue=False):
    path = task_path(root, task)
    if not isinstance(event_id, str) or ID.fullmatch(event_id) is None or event_id == 'start' or stage not in STAGES:
        raise Halt('Use a new event ID and a supported chat milestone')
    summary = public_text(summary, 'Milestone evidence')
    if pr is not None and (type(pr) is not int or pr <= 0):
        raise Halt('Use a positive pull request number')
    if pr is not None and pr_url is not None:
        raise Halt('Provide one pull request number or URL')
    target = pr_target(config['repo'], pr, pr_url)
    if pr_url is not None:
        pr_url = pr_url.casefold()
    event = {'id': event_id, 'stage': stage, 'summary': summary, 'pr': pr, 'pr_url': pr_url, 'local_done': local_done}
    if closes_issue:
        event['closes_issue'] = True
    with local_lock(path.with_suffix('.lock')):
        record = read_task(path, config)
        for previous in record['events']:
            if previous['id'] == event_id:
                if any(previous.get(k) != v for k, v in event.items()):
                    raise Halt('This milestone ID already records different evidence')
                return view(record)
        if record['events'][-1]['stage'] == 'Done':
            raise Halt('This chat task is complete')
        bound = pr_target(config['repo'], record.get('pr'), record.get('pr_url'))
        if target and bound and target != bound:
            raise Halt('The chat task is already tied to another pull request')
        delivery = record.get('delivery')
        if delivery == 'local-only' and target:
            raise Halt('This task is recorded as local-only; it cannot link a pull request')
        linked = target or bound
        if closes_issue and not linked:
            raise Halt('Issue closure intent requires a recorded pull request')
        if delivery == 'pr-required' and (local_done or stage == 'Done' and not linked):
            raise Halt('This task requires a linked merged pull request before Done')
        if (local_done and (stage != 'Done' or linked) or
                stage == 'Done' and not (local_done or linked)):
            raise Halt('Done requires a linked merged pull request or an explicit local completion')
        if not bound:
            if pr_url is not None:
                record['pr_url'] = pr_url
            elif pr is not None:
                record['pr'] = pr
        if closes_issue:
            record['closes_issue'] = True
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
            delivery = record.get('delivery')
            if delivery is not None:
                body += '\n\n## Delivery\n' + ('Pull request required.' if delivery == 'pr-required' else 'Local only.')
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


def observe_pr(pr, number, base, repository):
    expected = f"https://github.com/{repository}/pull/{number}"
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


def observe_closing_pr(gh, record):
    pr = gh.chat_closing_pr(record['issue'])
    if pr is None:
        return None
    repository = pr['repository']['nameWithOwner']
    number = pr['number']
    if (not isinstance(repository, str) or re.fullmatch(r'[A-Za-z0-9-]+/[A-Za-z0-9_.-]+', repository) is None
            or type(number) is not int or number <= 0):
        raise Halt('GitHub closing pull request identity could not be verified')
    return observe_pr(pr, number, record['scope']['base_branch'], repository)


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


def sync_pr_attachment(gh, path, record, target):
    repository, number = target
    observed = gh.chat_pr_attachment(record['issue'], repository, number)
    binding = {key: observed[key] for key in ('issue', 'pull_request', 'url', 'base')}
    previous = record.get('native_pr_attachment')
    if previous and previous['binding'] != binding:
        raise Halt('Native PR attachment identity changed; inspect the saved receipt')
    linked = observed['pull_request'] in observed['references']
    if previous and not linked:
        raise Halt('Native PR attachment needs readback or was removed; the write will not be repeated')
    if not linked and not record.get('closes_issue'):
        return  # A milestone PR alone does not authorize automatic issue closure.
    if not linked:
        if len(observed['references']) >= 10:
            raise Halt('The issue already has ten linked PRs; preserve them and inspect the attachment')
        verified_card(gh, record)
        if repository != gh.c['repo'].casefold():
            gh.verify_chat_link_visibility(repository)
        if gh.chat_pr_attachment(record['issue'], repository, number) != observed:
            raise Halt('Native PR attachment scope or references changed before writing')
        record['native_pr_attachment'] = {'state': 'intent', 'binding': binding, 'requested_at': now()}
        write_json(path, record)
        gh.attach_chat_pr(observed['issue'], observed['pull_request'])
        checked = gh.chat_pr_attachment(record['issue'], repository, number)
        if ({key: checked[key] for key in binding} != binding or
                checked['pull_request'] not in checked['references']):
            raise Halt('Native PR attachment write was not verified; inspect before another operation')
    if not previous or previous['state'] != 'verified':
        record['native_pr_attachment'] = {**record.get('native_pr_attachment', {}),
                                           'state': 'verified', 'binding': binding, 'verified_at': now()}
        write_json(path, record)


def sync(gh, root, task):
    path = task_path(root, task)
    with exclusive(root), local_lock(path.with_suffix('.lock')):
        record = read_task(path, load_config(root))
        if scope(gh.c) != record['scope']:
            raise Halt('Tracking configuration changed before sync')
        number = record.get('pr')
        pr_url = record.get('pr_url')
        target = pr_target(gh.c['repo'], number, pr_url)
        latest = record['events'][-1]
        completed = latest['stage'] == 'Done' and latest.get('synced')
        if completed and target is None and record.get('native_pr_attachment'):
            target = pr_target(gh.c['repo'], None, record['native_pr_attachment']['binding']['url'])
        follow_pr = target is not None and latest['stage'] != 'Done'
        pending = any(not event.get('synced') for event in record['events'])
        attachment_pending = bool(record.get('native_pr_attachment_error') or
                                  record.get('native_pr_attachment'))
        closing_pr_url = latest.get('closing_pr_url') if pending else None
        follow_closure = (target is None and record.get('delivery') != 'local-only'
                          and (latest['stage'] in ('QA', 'Review') or closing_pr_url is not None))
        if not (follow_pr or follow_closure or pending or attachment_pending):
            return {'state': 'synced', **view(record)}
        try:
            if not record.get('issue'):
                bind_issue(gh, root, path, record)
            pull = None
            if pr_url is not None or (target is not None and number is None):
                repository, pull_number = target
                pull = observe_pr(gh.chat_pr(pull_number, repository=repository), pull_number,
                                  record['scope']['base_branch'], repository)
                if repository != gh.c['repo'].casefold() and (pending or pull['state'] == 'MERGED'):
                    gh.verify_chat_link_visibility(repository)
            elif number is not None:
                pull = observe_pr(gh.chat_pr(number), number, record['scope']['base_branch'], gh.c['repo'])
            elif follow_closure:
                pull = observe_closing_pr(gh, record)
            if closing_pr_url is not None and (pull is None or pull['url'] != closing_pr_url):
                raise Halt('The verified closing pull request changed before Done synced')
            if pull:
                record['pull_request'] = pull
                attachment_target = target or pr_target(gh.c['repo'], None, pull['url'])
                try:
                    sync_pr_attachment(gh, path, record, attachment_target)
                except (Halt, OSError, ValueError, KeyError) as exc:
                    # Link writes retain their own intent. Independently
                    # verified milestone and merge observations can still sync.
                    record['native_pr_attachment_error'] = str(exc)
                    write_json(path, record)
                else:
                    record.pop('native_pr_attachment_error', None)
                if latest['stage'] != 'Done' and pull['state'] == 'MERGED':
                    # Save the completion event before writing either status field.
                    completion = {'id': 'sfc:pr-merged', 'stage': 'Done', 'pr': number,
                                  'summary': 'GitHub confirmed the linked pull request merged.'}
                    if follow_closure:
                        completion['closing_pr_url'] = pull['url']
                        completion['summary'] = 'GitHub confirmed the linked pull request merged and closed this issue.'
                    record['events'].append(completion)
                    write_json(path, record)
            for event in record['events']:
                if event.get('synced'):
                    continue
                if pull and event['stage'] == 'Done' and pull['state'] != 'MERGED':
                    raise Halt('The linked pull request has not merged')
                url = pull['url'] if pull and (event.get('pr') or event.get('pr_url') or
                                               event['stage'] in ('Review', 'Done')) else None
                card = verified_card(gh, record)
                if event.get('closing_pr_url') and card['state'] != 'CLOSED':
                    raise Halt('The tracked issue reopened before Done synced')
                sync_stage(gh, path, record, card, event)
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
