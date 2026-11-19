"""Frozen Latest policy and combined publication/readback, without mutation retries."""
from __future__ import annotations

import re

from .common import Halt
from .release_github import release_row


def stable_version(tag):
    match = re.fullmatch(r'v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)', tag)
    return tuple(map(int, match.groups())) if match else None


def inventory(rows, exclude):
    return sorted([{'id': r['id'], 'tag_name': r['tag_name'], 'prerelease': r.get('prerelease', False)}
                   for r in rows if not r['draft'] and r['tag_name'] != exclude],
                  key=lambda r: (r['tag_name'], r['id']))


def latest(remote, rows):
    value = remote.latest(rows)
    if value is None:
        return None
    if (type(value.get('id')) is not int or not value.get('tag_name') or
            value.get('draft') is not False or value.get('prerelease') is not False):
        raise Halt('GitHub Latest identity is unavailable or unsafe')
    return {k: value[k] for k in ('id', 'tag_name')}


def require_verifiable(plan):
    # Once a full release exists, the transport requires a Latest endpoint
    # identity; it cannot prove that Latest stayed absent after publication.
    if not plan['prerelease'] and plan['make_latest'] == 'false' and plan['before'] is None:
        raise Halt('Stable non-Latest publication has no prior Latest to preserve; '
                   'choose latest_policy:newer-stable before preparing the first stable release')


def freeze(remote, version, policy):
    releases = remote.releases()
    rows, before = inventory(releases, version), latest(remote, releases)
    if before is not None and not any(all(r[k] == before[k] for k in before) for r in rows):
        raise Halt('GitHub Latest differs from the published release inventory')
    prerelease = '-' in version
    promote = policy == 'newer-stable' and not prerelease
    if promote:
        versions = [stable_version(r['tag_name']) for r in rows if not r['prerelease']]
        target = stable_version(version)
        if target is None or any(v is None for v in versions):
            raise Halt('Latest version ordering unknown; explicitly choose latest_policy:never')
        promote = all(target > v for v in versions)
    plan = {'policy': policy, 'make_latest': 'true' if promote else 'false',
            'prerelease': prerelease, 'before': before, 'inventory': rows}
    require_verifiable(plan)
    return plan


def read(releases):
    j = releases.journal
    rows = releases.remote.releases()
    row = release_row(rows, j['version'])
    fields = ('id', 'tag_name', 'target_commitish', 'name', 'body', 'draft', 'published_at', 'prerelease')
    release = None if row is None else {key: row.get(key) for key in fields}
    return {'release': release, 'latest': latest(releases.remote, rows), 'inventory': inventory(rows, j['version'])}


def matches(releases, value, published):
    j, plan = releases.journal, releases.journal['latest_plan']
    r = value['release']
    expected_latest = ({'id': j['release_id'], 'tag_name': j['version']}
                       if published and plan['make_latest'] == 'true' else plan['before'])
    return bool(r and r['id'] == j['release_id'] and r['tag_name'] == j['version'] and
                r['target_commitish'] == j['candidate'] and r['name'] == j['version'] and
                r['body'] == releases.public_notes() and r['prerelease'] == plan['prerelease'] and
                r['draft'] is (not published) and
                (bool(r['published_at']) if published else r['published_at'] is None) and
                value['latest'] == expected_latest and value['inventory'] == plan['inventory'])


def publish(releases):
    plan = releases.journal['latest_plan']
    if 'publication' not in releases.journal['effects']:
        require_verifiable(plan)  # Also guard plans frozen by an older implementation.
    releases.effect('publication', lambda: read(releases),
                    lambda v: matches(releases, v, True),
                    lambda: releases.remote.publish(releases.journal['release_id'], plan['make_latest']),
                    publication=True, before=lambda v: matches(releases, v, False))


def verify(releases):
    if not matches(releases, read(releases), True):
        raise Halt('Published release/Latest outcome changed; preserve intent and human edits')
