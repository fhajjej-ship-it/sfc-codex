"""Deterministic public prose from frozen merged titles and opt-in public docs.

Never consume commit messages, PR bodies, worker output or verification logs.
Unknown changes remain accounted for without guessing what they do.
"""
from __future__ import annotations

import hashlib
import json
import re

from .common import Halt, git


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def eligible_title(title):
    return (isinstance(title, str) and 1 <= len(title) <= 240 and
            all(c.isprintable() for c in title) and
            not any(c in title for c in '\n\r`<>\\/') and
            not re.search(r'https?:|\$|\b(?:token|password|secret)\s*[:=]', title, re.I))


class MissingReleaseSummary(Halt):
    """No approved public text is available for the selected source."""


def freeze(root, journal, docs, approval=None):
    changes = journal['changes']
    commits = {m['commit'] for m in changes['commits']}
    groups, seen, sources = {}, set(), []
    repo = journal['identity']['repo']
    base = 'https://github.com/' + repo
    for pr in changes['pull_requests']:
        if pr['merge_commit'] not in commits:
            raise Halt('Public notes PR is outside the frozen merged source range')
        title = pr.get('title', '')
        # Only public title metadata is eligible, never private repository prose.
        # Reject multiline/markup/command-like titles rather than interpreting it.
        if pr.get('public') is not True or not eligible_title(title):
            continue
        match = re.match(r'^(feat|fix|perf|refactor|docs|chore|test|build|ci)(?:\([^)]*\))?(!)?:\s*(.+)$', title, re.I)
        kind, text = (match[1].lower(), match[3]) if match else ('other', title)
        group = 'Fixes' if kind == 'fix' else 'Highlights' if kind == 'feat' else 'Improvements'
        key = text.casefold().strip()
        sources.append({'number': pr['number'], 'title': title, 'merge_commit': pr['merge_commit']})
        if key in seen:
            # One description, all overlapping PR links retained.
            seen_entry = next(e for entries in groups.values() for e in entries if e['key'] == key)
            if pr['number'] not in seen_entry['prs']:
                seen_entry['prs'].append(pr['number'])
            continue
        seen.add(key)
        groups.setdefault(group, []).append({'key': key, 'text': text, 'prs': [pr['number']]})
    approved = []
    if approval and any(approval.get(k) != journal['identity'][k]
                        for k in ('repo', 'base_branch', 'host', 'destination')):
        raise Halt('Approved summary authority differs from frozen source/destination')
    for entry in (approval or {}).get('entries', []):
        if entry['commit'] not in commits:
            continue  # Stored approvals may cover other release ranges.
        if not eligible_title(entry['text']) or len(entry['text'].split()) < 3:
            raise Halt('Approved release summary must be readable bounded plain text')
        approved.append(dict(entry))
    if not groups and not approved:
        raise MissingReleaseSummary('No approved meaningful release summary for candidate ' + journal['candidate'] +
                                    '; configure release.approved_summaries with authorized commit-bound text before requesting publication')
    lines = [f"## What's in {journal['version']}", '',
             'Changes in this release are listed below.']
    if approved:
        lines += ['', '### Approved changes']
        for entry in approved:
            text = re.sub(r'([\[\]*_!])', r'\\\1', entry['text'])
            lines.append(f'- {text} ([source]({base}/commit/{entry["commit"]}))')
    order = ('Highlights', 'Fixes', 'Improvements')
    counts = dict.fromkeys(order, 0)
    # Share the short overview across present groups before filling spare space;
    # an inventory of features must not hide the release's only fix.
    for _ in range(12):
        available = [g for g in order if counts[g] < len(groups.get(g, []))]
        if not available:
            break
        counts[min(available, key=lambda g: counts[g])] += 1
    truncated = 0
    for group in order:
        entries = groups.get(group, [])
        displayed = entries[:counts[group]]
        truncated += len(entries) - len(displayed)
        if not displayed:
            continue
        lines += ['', '### ' + group]
        for entry in displayed:
            # Escape Markdown title punctuation; URLs are supervisor-constructed.
            text = re.sub(r'([\[\]*_!])', r'\\\1', entry['text'])
            links = ', '.join(f'[#{n}]({base}/pull/{n})' for n in entry['prs'])
            lines.append(f'- {text} ({links})')
    if truncated:
        lines += ['', f'{truncated} more change(s) are available in the full changelog.']
    unlinked = len(changes['unlinked_commits'])
    if unlinked:
        noun, verb, pronoun = ('commit', 'has', 'its') if unlinked == 1 else ('commits', 'have', 'their')
        lines += ['', f'{unlinked} additional {noun} {verb} no merged PR link; {pronoun} purpose is not inferred here.']
    omitted = len({p['number'] for p in changes['pull_requests']} - {s['number'] for s in sources})
    if omitted:
        lines += ['', f'{omitted} merged PR(s) have no eligible public title; see the full changelog.']
    documents = []
    for heading, path in docs.items():
        # Config explicitly identifies complete public documents. Read the Git
        # blob, never a working-tree file or a post-cutoff revision.
        obj = journal['candidate'] + ':' + path
        if git(root, 'ls-tree', journal['candidate'], '--', path).split()[0:1] != ['100644']:
            raise Halt('Public release documentation must be a pinned regular file')
        content = git(root, 'show', obj)
        if not content.strip() or len(content) > 12000:
            raise Halt('Public release documentation must be nonblank and bounded')
        documents.append({'section': heading, 'path': path, 'blob': git(root, 'rev-parse', obj), 'sha256': sha(content)})
        lines += ['', '### ' + heading, '', content.strip(), '',
                  f'[Source documentation]({base}/blob/{journal["candidate"]}/{path})']
    target = (base + '/compare/' + journal['boundary'] + '...' + journal['candidate']
              if journal['boundary'] else base + '/commits/' + journal['candidate'])
    lines += ['', f'[Full changelog]({target})', '']
    text = '\n'.join(lines)
    return {'schema': 1, 'text': text, 'sha256': sha(text),
            'source': {'candidate': journal['candidate'], 'boundary': journal['boundary'],
                       'version': journal['version'], 'titles': sources, 'documents': documents,
                       'approved_summaries': {'authorization_id': (approval or {}).get('authorization_id'),
                                              'entries': approved},
                       'membership_sha256': sha(json.dumps(changes, sort_keys=True))}}


def validate(journal):
    notes = journal['public_notes']
    source = notes['source']
    if (notes['sha256'] != sha(notes['text']) or
            any(source[k] != journal[k] for k in ('candidate', 'boundary', 'version')) or
            source['membership_sha256'] != sha(json.dumps(journal['changes'], sort_keys=True))):
        raise Halt('Frozen public release notes/source evidence changed')
    if not source.get('titles') and not source.get('approved_summaries', {}).get('entries'):
        raise Halt('No approved meaningful release summary; reconcile missing input without rewriting original evidence')
    return notes['text']
