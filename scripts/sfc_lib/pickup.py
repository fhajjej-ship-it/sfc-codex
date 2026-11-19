"""Read-only, fail-closed inputs for explicitly scoped priority dispatch."""
from __future__ import annotations

from datetime import datetime, timezone
import re

from .common import Halt
from .ticket_validation import dependencies
from .priority import ITEM_SELECTION, connection, inventory, item_snapshot, resolve, validate_routing


ISSUE_FIELDS = 'id number title body url state createdAt repository { nameWithOwner }'
ISSUE_SELECTION = '''repository(owner:$owner,name:$repo) { nameWithOwner issue(number:$number) {
  ''' + ISSUE_FIELDS + ''' labels(first:100,after:$after) {
    nodes { id name } pageInfo { hasNextPage endCursor }
  } } }'''


def routing_scope(gh, *, batch_routing=False):
    reader = getattr(gh, 'routing_inventory', None) if batch_routing else None
    if reader:
        meta = gh.metadata()
        fields = reader(meta['id'])
    else:
        fields = None
    if fields is None:
        gh.verify_repository()
        meta = gh.metadata()
        project = gh.call('project', 'view', gh.c['project']['number'],
                          '--owner', gh.c['project']['owner'], '--format', 'json')
        if project.get('id') != meta['id']:
            raise Halt('Configured Project identity changed; inspect before dispatch')
        fields = inventory(gh, meta['id'])
    validate_routing(fields, gh.c, meta)
    return {'project': {**meta, 'options': dict(meta['options'])}}, fields


def scope(gh, *, batch_routing=False):
    routing, fields = routing_scope(gh, batch_routing=batch_routing)
    meta = routing['project']
    priority = resolve(fields, {**gh.c, '_status_field_id': meta['field']})
    if priority is None:
        raise Halt('Priority field missing; explicit Priority setup required before factory pickup')
    return {'project': meta, 'priority': priority}


def read_issue(gh, number, *, first_page=None):
    """Verify actual Issue identity and every label page, never infer missing = closed."""
    owner, repo = gh.c['repo'].split('/')
    after, cursors, seen, labels, pinned = None, set(), set(), [], None
    while True:
        node = first_page if after is None and first_page is not None else gh.graphql(
            'query($owner:String!, $repo:String!, $number:Int!, $after:String) {' + ISSUE_SELECTION + '}',
            {'owner': owner, 'repo': repo, 'number': number, 'after': after})['repository']
        if not node or node['nameWithOwner'] != gh.c['repo'] or not node.get('issue'):
            raise Halt(f'Issue #{number} unavailable in configured repository')
        value = node['issue']
        current = {k: v for k, v in value.items() if k != 'labels'}
        if (not current.get('id') or current.get('number') != number or
                current.get('repository', {}).get('nameWithOwner') != gh.c['repo'] or
                current.get('state') not in ('OPEN', 'CLOSED')):
            raise Halt(f'Issue #{number} identity/state is unresolved')
        if pinned is not None and current != pinned:
            raise Halt(f'Issue #{number} changed during label pagination')
        pinned = current
        for label in value['labels']['nodes']:
            if not label.get('id') or not label.get('name') or label['id'] in seen:
                raise Halt('Incomplete or duplicate issue label inventory')
            seen.add(label['id'])
            labels.append(label['name'])
        page = value['labels']['pageInfo']
        if not page['hasNextPage']:
            return {**pinned, 'labels': sorted(labels)}
        after = page.get('endCursor')
        if not after or after in cursors:
            raise Halt('Issue label pagination did not advance')
        cursors.add(after)


def card(gh, item_id, pinned, *, expected=None):
    item_page = issue_page = None
    if expected is not None:
        # Inventory already identified the issue. Fetch the independent item
        # and repository views together, then paginate and compare as usual.
        owner, repo = gh.c['repo'].split('/')
        first = gh.graphql(
            'query($item:ID!, $owner:String!, $repo:String!, $number:Int!, $after:String) {'
            + ITEM_SELECTION + '\n' + ISSUE_SELECTION + '}',
            {'item': item_id, 'owner': owner, 'repo': repo, 'number': expected['number'], 'after': None})
        item_page, issue_page = first.get('node'), first.get('repository')
        if not item_page or not issue_page:
            raise Halt('Project item or repository issue identity is unavailable')
    snapshot = item_snapshot(gh, item_id, tolerate_ambiguous=True, first_page=item_page)
    content = snapshot['content']
    meta, priority = pinned['project'], pinned.get('priority', {'options': {}, 'field_id': None})
    if (snapshot['id'] != item_id or snapshot['project']['id'] != meta['id'] or
            content.get('repository', {}).get('nameWithOwner') != gh.c['repo']):
        raise Halt('Project item identity/repository changed')
    if expected is not None and any(content.get(key) != expected[key] for key in ('id', 'number')):
        raise Halt('Changed Project issue identity')
    issue = read_issue(gh, content['number'], first_page=issue_page)
    if any(issue.get(k) != content.get(k) for k in ('id', 'number', 'title', 'body', 'url', 'state', 'repository')):
        raise Halt('Project item and actual issue changed or disagree')
    stages = {value: name for name, value in meta['options'].items()}
    levels = {value: name for name, value in priority['options'].items()}
    return {**issue, 'item_id': item_id,
            'status': stages.get(snapshot['values'].get(meta['field']), 'Unknown'),
            'priority': levels.get(snapshot['values'].get(priority['field_id'])),
            '_pickup_scope': pinned}


def cards(gh, issues=None, *, completed=(), batch_routing=False):
    """Inventory every membership; omit details only for known, still-closed work.

    `completed` is a per-observation hint, never a cached Issue response. Live
    state/identity is read for every member, so reopened tickets return on the
    next poll. Explicit finite selections retain their complete diagnostics.
    """
    pinned = scope(gh, batch_routing=batch_routing)
    items = connection(lambda after: gh.graphql('''query($project:ID!, $after:String) {
      node(id:$project) { ... on ProjectV2 { items(first:100,after:$after) {
        nodes { id content { __typename ... on Issue { id number state repository { nameWithOwner } } } }
        pageInfo { hasNextPage endCursor }
      } } }
    }''', {'project': pinned['project']['id'], 'after': after})['node']['items'])
    selected, seen_ids, seen_numbers = [], set(), set()
    for item in items:
        if 'content' not in item:
            raise Halt('Project content identity is unavailable')
        content = item.get('content')
        if content is None:
            continue
        if not isinstance(content, dict) or content.get('__typename') not in ('Issue', 'DraftIssue', 'PullRequest'):
            raise Halt('Project content identity/type is unresolved')
        if content['__typename'] != 'Issue':
            continue
        repository = content.get('repository')
        if (not isinstance(repository, dict) or not isinstance(repository.get('nameWithOwner'), str) or
                not re.fullmatch(r'[^/\s]+/[^/\s]+', repository['nameWithOwner'])):
            raise Halt('Project issue repository identity is unresolved')
        if repository['nameWithOwner'] != gh.c['repo']:
            continue
        number, identity = content.get('number'), content.get('id')
        if (not isinstance(item['id'], str) or not isinstance(identity, str) or not identity or
                type(number) is not int or number <= 0):
            raise Halt('Project issue identity/number is unresolved')
        if identity in seen_ids or number in seen_numbers:
            raise Halt('Duplicate Project issue identity')
        seen_ids.add(identity)
        seen_numbers.add(number)
        if content.get('state') not in ('OPEN', 'CLOSED'):
            raise Halt('Project issue state is unresolved')
        if issues is None and number in completed and content['state'] == 'CLOSED':
            continue
        if issues is None or number in issues:
            selected.append(item)
    result = []
    for item in selected:
        result.append(card(gh, item['id'], pinned, expected=item['content']))
    if scope(gh, batch_routing=batch_routing) != pinned:
        raise Halt('Routing/Priority meanings changed while reading queue')
    return result


def timestamp(card):
    try:
        value = datetime.fromisoformat(card['createdAt'].replace('Z', '+00:00'))
        if value.tzinfo is None:
            raise ValueError('timezone required')
        return value.astimezone(timezone.utc)
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        raise Halt('Missing/invalid issue creation timestamp') from exc


class OpenPrerequisites(Halt):
    pass


def prerequisites(gh, card, *, closed=None):
    numbers = dependencies(card['body'])
    if card['number'] in numbers:
        raise Halt('Issue depends on itself')
    blocked = []
    for n in numbers:
        if closed is not None and n in closed:
            continue
        if read_issue(gh, n)['state'] != 'CLOSED':
            blocked.append(n)
        elif closed is not None:
            closed.add(n)
    if blocked:
        raise OpenPrerequisites('Open prerequisites: ' + ', '.join('#' + str(n) for n in blocked))


def prerequisite_problems(gh, card, *, closed=None):
    try:
        prerequisites(gh, card, closed=closed)
    except (Halt, OSError, ValueError, KeyError, TypeError) as exc:
        return [str(exc)]
    return []


def eligibility(gh, card, lint, *, closed=None):
    problems = lint(card)
    if card.get('priority') not in ('P0', 'P1', 'P2', 'P3'):
        problems.append('Missing/unmapped Priority; assign a verified Project option')
    try:
        timestamp(card)
    except (Halt, OSError, ValueError, KeyError, TypeError) as exc:
        problems.append(str(exc))
    problems.extend(prerequisite_problems(gh, card, closed=closed))
    return problems
