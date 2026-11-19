"""Explicit supervisor-only metadata edits; original release evidence stays intact."""
from __future__ import annotations

import re

from .common import Halt, Stopped, now, read_json, write_json
from .release_notes import sha
from .releases import VERSION, digest


def edit(releases, version, authorization_id, expected_sha256, text):
    if (not re.fullmatch(VERSION, version) or
            not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', authorization_id) or
            not re.fullmatch(r'[0-9a-f]{64}', expected_sha256) or
            not isinstance(text, str) or not text.strip() or len(text) > 25000):
        raise Halt('Description edit requires version, explicit authorization ID, expected body SHA-256 and bounded approved text')
    releases.path = releases.directory / (version + '.json')
    j = read_json(releases.path)
    releases.journal = j
    publication = j.get('effects', {}).get('publication', {})
    if j.get('state') != 'done' or publication.get('state') != 'verified':
        raise Halt('Description-only edits require a completed verified publication')
    releases.checked()
    path = releases.directory / version / 'description-edits' / (authorization_id + '.json')
    subject = {'version': version, 'candidate': j['candidate'], 'release_id': j['release_id'],
               'authorization_id': authorization_id, 'expected_body_sha256': expected_sha256,
               'text': text, 'text_sha256': sha(text), 'publication_receipt_sha256': digest(publication),
               'publication_manifest_sha256': j['publication_manifest_sha256']}

    def read():
        releases.checked()
        value = releases.remote.release(version)
        if (not value or value['id'] != j['release_id'] or value['tag_name'] != version or
                value['target_commitish'] != j['candidate'] or value['name'] != version or
                value['draft'] or not value['published_at'] or
                releases.remote.tag(version) != j['candidate']):
            raise Halt('Published release identity changed; no description edit')
        return {k: value[k] for k in ('id', 'tag_name', 'target_commitish', 'name', 'draft', 'published_at', 'body')}

    current = read()
    if path.exists():
        receipt = read_json(path)
        if receipt.get('subject') != subject or current != {**receipt['before'], 'body': text}:
            raise Halt('Description edit unresolved or externally edited; readback only, no repeat write')
        if receipt.get('observed') and receipt['observed'] != current:
            raise Halt('Description edit readback identity changed; preserve human edits')
    else:
        if sha(current['body']) != expected_sha256:
            raise Halt('Description was edited externally; preserve human text')
        receipt = {'subject': subject, 'state': 'intent', 'before': current, 'time': now()}
        write_json(path, receipt)
        if read() != current:
            raise Halt('Description changed after intent; no write')
        try:
            releases.remote.edit_description(j['release_id'], text)
        except Stopped:
            raise
        except Exception as exc:
            receipt['error_type'] = type(exc).__name__
            write_json(path, receipt)
        current = read()
        if current != {**receipt['before'], 'body': text}:
            receipt['observed'] = current
            write_json(path, receipt)
            raise Halt('Description edit uncertain; preserve receipt and remote text')
    receipt.update(state='verified', observed=current)
    write_json(path, receipt)
    return {'state': 'verified', 'version': version, 'receipt': str(path), 'body_sha256': sha(text)}
