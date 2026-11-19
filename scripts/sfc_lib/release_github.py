"""GitHub Releases transport. Called only by the checked release supervisor.

No retrying mutations, replacing assets, force-updating tags or worker writes.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
from urllib.parse import quote, urlparse

from .common import Halt
from .release_notes import eligible_title
from .priority import connection, inventory, validate_routing

FIELD = 'Target release'


def release_row(rows, tag):
    matches = [row for row in rows if row['tag_name'] == tag]
    if len(matches) > 1:
        raise Halt('Duplicate release version')
    return matches[0] if matches else None


class ReleasePushPermissionMissing(Halt):
    """The pinned GitHub scope is readable, but the credential cannot push."""

    def __init__(self, identity):
        super().__init__('GitHub release repository push permission is required')
        self.identity = identity


class ReleaseVersionFileInvalid(Halt):
    """The selected merged source has a version entry, but it is not plain text."""


class ReleaseGitHub:
    def __init__(self, gh):
        self.gh = gh
        self.c = gh.c
        self.repo = self.c['repo']
        self.prefix = f'repos/{self.repo}'

    def api(self, path, method=None, body=None):
        args = ['api', self.prefix + '/' + path]
        if method:
            args += ['--method', method, '--input', '-']
        return self.gh.call(*args, **({'input': json.dumps(body)} if method else {}))

    def pages(self, path):
        pages = self.gh.call('api', self.prefix + '/' + path, '--paginate', '--slurp')
        return [v for page in pages for v in page]

    def repository_access(self):
        self.gh.verify_repository()
        return self._repository_access()

    def _repository_access(self):
        value = self.gh.call('api', self.prefix)
        if (not isinstance(value, dict) or value.get('full_name') != self.repo or value.get('archived') or
                value.get('disabled') or not isinstance(value.get('node_id'), str) or not value['node_id'].strip()):
            raise Halt('Release repository identity unavailable')
        permissions = value.get('permissions')
        if not isinstance(permissions, dict) or type(permissions.get('push')) is not bool:
            raise Halt('Release repository push permission unavailable')
        return value['node_id'], permissions['push']

    def base_version_file(self, repository_id, branch, path):
        """Read the file from the branch's exact commit without fetching or writing refs."""
        owner, name = self.repo.split('/')
        try:
            data = self.gh.graphql('''query($owner:String!, $name:String!, $ref:String!, $path:String!) {
              repository(owner:$owner,name:$name) {
                id nameWithOwner ref(qualifiedName:$ref) {
                  name prefix target { __typename ... on Commit { oid file(path:$path) {
                    object { __typename ... on Blob { text isBinary isTruncated } }
                  } } }
                }
              }
            }''', {'owner': owner, 'name': name, 'ref': 'refs/heads/' + branch, 'path': path})
            repo = data['repository']
            ref = repo['ref']
            commit = ref['target']
            if (repo['id'] != repository_id or repo['nameWithOwner'] != self.repo or
                    ref['name'] != branch or ref['prefix'] != 'refs/heads/' or
                    commit['__typename'] != 'Commit' or not re.fullmatch('[0-9a-f]{40}', commit['oid'])):
                raise Halt('Release version source does not match the selected repository and base')
            entry = commit['file']
            if entry is None:
                return None
            blob = entry['object']
            if blob['__typename'] != 'Blob' or blob['isBinary'] is True:
                raise ReleaseVersionFileInvalid('The version file is not valid plain text')
            if blob['isTruncated'] is not False or blob['isBinary'] is not False or not isinstance(blob['text'], str):
                raise Halt('Complete release version file text is unavailable')
            return blob['text']
        except (KeyError, TypeError) as exc:
            raise Halt('Release version source is unavailable') from exc

    def identity(self):
        from .pickup import routing_scope

        routing, _ = routing_scope(self.gh, batch_routing=True)
        repository_id, push = self._repository_access()
        host = urlparse(self.gh.origin).hostname
        identity = {'repo': self.repo, 'repository_id': repository_id, 'host': host,
                    'project': routing['project']['id'], 'base_branch': self.c['base_branch'], 'destination': 'github-releases'}
        if not push:
            raise ReleasePushPermissionMissing(identity)
        return identity

    def tag(self, tag):
        # Listing (including zero matches) distinguishes absence from auth/transport errors.
        refs = self.pages('git/matching-refs/tags/' + quote(tag, safe=''))
        refs = [r for r in refs if r.get('ref') == 'refs/tags/' + tag]
        if not refs:
            return None
        if len(refs) != 1:
            raise Halt('Ambiguous release tag')
        return self.commit_for_tag_object(refs[0]['object'])

    def commit_for_tag_object(self, obj):
        seen = set()
        while obj['type'] == 'tag':
            if obj['sha'] in seen or len(seen) >= 16:
                raise Halt('Release tag dereference is ambiguous')
            seen.add(obj['sha'])
            obj = self.api('git/tags/' + obj['sha'])['object']
        if obj['type'] != 'commit':
            raise Halt('Release tag does not point to a commit')
        return obj['sha']

    def tag_sources(self, versions):
        """Resolve current release refs in bounded GraphQL batches."""
        owner, name = self.repo.split('/')
        result = {}
        for offset in range(0, len(versions), 40):
            batch = versions[offset:offset + 40]
            variables = {'owner': owner, 'name': name}
            declarations = ['$owner: String!', '$name: String!']
            refs = []
            for index, version in enumerate(batch):
                alias = f'tag{index}'
                variables[alias] = 'refs/tags/' + version
                declarations.append(f'${alias}: String!')
                refs.append(f'{alias}: ref(qualifiedName: ${alias}) {{ name prefix target {{ __typename oid }} }}')
            query = ('query(' + ', '.join(declarations) + ') { repository(owner:$owner, name:$name) { '
                     'nameWithOwner ' + ' '.join(refs) + ' } }')
            repository = self.gh.graphql(query, variables).get('repository')
            if not repository or repository.get('nameWithOwner') != self.repo:
                raise Halt('Release repository identity unavailable while reading tags')
            for index, version in enumerate(batch):
                ref = repository.get(f'tag{index}')
                if not ref or ref.get('name') != version or ref.get('prefix') != 'refs/tags/':
                    raise Halt('Published release source boundary is unavailable')
                target = ref.get('target') or {}
                if target.get('__typename') not in ('Commit', 'Tag') or not target.get('oid'):
                    raise Halt('Release tag does not point to a commit')
                result[version] = self.commit_for_tag_object(
                    {'sha': target['oid'], 'type': target['__typename'].lower()})
        return result

    def releases(self):
        return self.pages('releases?per_page=100')

    def release_issues(self, version, request_id):
        """Check repository issues, including archived or closed Project cards."""
        title = 'Release ' + version
        marker = '<!-- sfc:ticket:' + request_id + ' -->'
        return sorted(issue['number'] for issue in self.pages('issues?state=all&per_page=100')
                      if 'pull_request' not in issue and
                      (issue['title'] == title or marker in (issue.get('body') or '')))

    def latest(self, rows=None):
        # Avoid treating an auth/transport 404 as absence. With no full releases
        # the complete inventory proves absence; otherwise require the endpoint.
        if rows is None:
            rows = self.releases()
        if not any(not r['draft'] and not r.get('prerelease', False) for r in rows):
            return None
        return self.api('releases/latest')

    def checks(self, candidate):
        return self.gh.local_merge_checks(candidate)

    def release(self, tag):
        row = release_row(self.releases(), tag)
        if row is None:
            return None
        return self.with_assets(row)

    def release_by_id(self, release_id):
        return self.with_assets(self.api(f'releases/{release_id}'))

    def with_assets(self, row):
        value = dict(row)
        value['assets'] = self.pages(f"releases/{value['id']}/assets?per_page=100")
        return value

    def changes(self, commits):
        """Use actual merged PR associations, not issue labels or commit prose."""
        members, prs = [], {}
        for sha in commits:
            linked = []
            for pr in self.pages(f'commits/{sha}/pulls?per_page=100'):
                if (not pr.get('merged_at') or pr.get('merge_commit_sha') not in commits or
                        pr.get('base', {}).get('repo', {}).get('full_name') != self.repo or
                        pr.get('base', {}).get('ref') != self.c['base_branch']):
                    continue
                number = pr['number']
                if type(number) is not int or number < 1:
                    raise Halt('Invalid release PR identity')
                linked.append(number)
                if number not in prs:
                    owner, name = self.repo.split('/')
                    issues = connection(lambda after: self.gh.graphql('''query($owner:String!, $name:String!, $number:Int!, $after:String) {
                      repository(owner:$owner,name:$name) { pullRequest(number:$number) {
                        closingIssuesReferences(first:100,after:$after) {
                          nodes { id number repository { nameWithOwner } }
                          pageInfo { hasNextPage endCursor }
                        }
                      } }
                    }''', {'owner': owner, 'name': name, 'number': number, 'after': after})[
                        'repository']['pullRequest']['closingIssuesReferences'])
                    prs[number] = {'number': number, 'merge_commit': pr['merge_commit_sha'],
                                   'title': (pr['title'] if pr.get('base', {}).get('repo', {}).get('private') is False
                                             and eligible_title(pr.get('title')) else ''),
                                   'public': pr.get('base', {}).get('repo', {}).get('private') is False,
                                   'issues': sorted(i['number'] for i in issues
                                                    if i['repository']['nameWithOwner'] == self.repo)}
            members.append({'commit': sha, 'pull_requests': sorted(set(linked))})
        return {'commits': members, 'pull_requests': sorted(prs.values(), key=lambda p: p['number']),
                'issues': sorted({i for p in prs.values() for i in p['issues']}),
                'unlinked_commits': [m['commit'] for m in members if not m['pull_requests']]}

    def create_tag(self, tag, sha):
        return self.api('git/refs', 'POST', {'ref': 'refs/tags/' + tag, 'sha': sha})

    def create_draft(self, tag, sha, notes):
        return self.api('releases', 'POST', {'tag_name': tag, 'target_commitish': sha,
                        'name': tag, 'body': notes, 'draft': True, 'prerelease': '-' in tag, 'make_latest': 'false'})

    def upload(self, release_id, name, path):
        # gh supplies credentials internally; they never enter config/journals.
        host = urlparse(self.gh.origin).hostname
        if host != 'github.com':
            raise Halt('First release adapter supports github.com uploads only')
        return self.gh.call('api', f'https://uploads.github.com/{self.prefix}/releases/{release_id}/assets?name={quote(name, safe="")}',
                            '--method', 'POST', '-H', 'Content-Type: application/octet-stream', '--input', str(path))

    def download_to(self, asset, path):
        # Processes owns the group, finite timeout, stop and host lifeline. The
        # output file descriptor preserves binary bytes despite text-mode stdin.
        execute = getattr(self, 'download_execute', self.processes.execute)
        if execute(['gh', 'api', '--hostname', 'github.com',
                                   self.prefix + f"/releases/assets/{asset['id']}",
                                   '-H', 'Accept: application/octet-stream'],
                                  self.gh.root, path, 120):
            raise Halt('Release asset download failed; retained journal')

    def publish(self, release_id, make_latest):
        if make_latest not in ('true', 'false'):
            raise Halt('Publication requires a frozen explicit Latest choice')
        return self.api(f'releases/{release_id}', 'PATCH', {'draft': False, 'make_latest': make_latest})

    def edit_description(self, release_id, text):
        return self.api(f'releases/{release_id}', 'PATCH', {'body': text})

    def fields(self):
        fields = inventory(self.gh)
        validate_routing(fields, self.c, self.gh.metadata())
        matches = [f for f in fields if f['name'] == FIELD]
        if len(matches) > 1 or (matches and matches[0]['dataType'] != 'TEXT'):
            raise Halt('Target release must be one unambiguous shared Project TEXT field')
        return matches[0] if matches else None

    def create_field(self):
        return self.gh.graphql('''mutation($input:CreateProjectV2FieldInput!) {
          createProjectV2Field(input:$input) { projectV2Field { ... on ProjectV2Field { id name dataType } } }
        }''', {'input': {'projectId': self.gh.metadata()['id'], 'name': FIELD, 'dataType': 'TEXT'}})

    def view(self):
        data = self.gh.dashboard_data()
        view = self.gh.factory_view(data)
        if not view:
            raise Halt('Saved Factory view unavailable; explicit dashboard setup required')
        return view

    def visible(self, view_id, fields):
        return self.gh.graphql('''mutation($input:UpdateProjectV2ViewInput!) {
          updateProjectV2View(input:$input) { projectV2View { id } }
        }''', {'input': {'viewId': view_id, 'configuration': {'visibleFieldIds': fields}}})

    def item(self, card, field):
        after, values, identity, cursors = None, [], None, set()
        while True:
            node = self.gh.graphql('''query($item:ID!, $after:String) {
              node(id:$item) { ... on ProjectV2Item { id project { id }
                content { ... on Issue { id number title body url state repository { nameWithOwner } } }
                fieldValues(first:100,after:$after) { nodes {
                  ... on ProjectV2ItemFieldTextValue { text field { ... on ProjectV2Field { id } } }
                } pageInfo { hasNextPage endCursor } }
              } }
            }''', {'item': card['item_id'], 'after': after})['node']
            if not node:
                raise Halt('Release card is unavailable')
            current = {k: node[k] for k in ('id', 'project', 'content')}
            if identity is not None and identity != current:
                raise Halt('Release card changed during pagination')
            identity = current
            values.extend(v['text'] for v in node['fieldValues']['nodes'] if v.get('field', {}).get('id') == field['id'])
            page = node['fieldValues']['pageInfo']
            if not page['hasNextPage']:
                break
            cursor = page.get('endCursor')
            if not cursor or cursor in cursors:
                raise Halt('Release field pagination did not advance')
            cursors.add(cursor)
            after = cursor
        if (current['id'] != card['item_id'] or current['project']['id'] != self.gh.metadata()['id'] or
                any(current['content'].get(k) != card.get(k) for k in ('id', 'number', 'url', 'repository')) or len(values) > 1):
            raise Halt('Release item identity changed or duplicate field values')
        return {'identity': current, 'value': values[0] if values else None}

    def assign(self, card, field, version):
        return self.gh.graphql('''mutation($input:UpdateProjectV2ItemFieldValueInput!) {
          updateProjectV2ItemFieldValue(input:$input) { projectV2Item { id } }
        }''', {'input': {'projectId': self.gh.metadata()['id'], 'itemId': card['item_id'],
                         'fieldId': field['id'], 'value': {'text': version}}})

    def filtered(self, version):
        return connection(lambda after: self.gh.graphql('''query($project:ID!, $query:String!, $after:String) {
          node(id:$project) { ... on ProjectV2 { items(first:100,after:$after,query:$query) {
            nodes { id } pageInfo { hasNextPage endCursor }
          } } }
        }''', {'project': self.gh.metadata()['id'], 'query': 'target-release:"' + version + '"',
                'after': after})['node']['items'])

    def card(self, number):
        from . import pickup
        items = pickup.cards(self.gh, [number], batch_routing=True)
        if len(items) != 1 or items[0]['number'] != number:
            raise Halt(f'Included issue #{number} has no unique card on the existing Factory board')
        return items[0]

    def pinned_card(self, identity):
        from . import pickup
        return pickup.card(self.gh, identity['item_id'], {'project': self.gh.metadata()}, expected=identity)

    def close(self, card):
        return self.api(f"issues/{card['number']}", 'PATCH', {'state': 'closed'})
