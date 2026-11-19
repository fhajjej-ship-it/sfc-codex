"""Invocation-local Codex trust for generated benchmark repositories."""
from __future__ import annotations

import json
from pathlib import Path

FIXTURE_TRUST_MODE = 'codex-cli-local-fixture-project-v1'


def fixture_trust_override(root):
    # Codex can otherwise persist implicit trust on the first workspace-write
    # thread. The inline table preserves literal paths containing dots or spaces.
    key = json.dumps(str(Path(root).resolve()), ensure_ascii=False)
    return 'projects={' + key + '={trust_level="trusted"}}'


def verify_fixture_trust(argv, root):
    if argv[1:4] != ['exec', '-c', fixture_trust_override(root)]:
        return False
    for index, argument in enumerate(argv[4:], 4):
        value = None
        if argument in ('-c', '--config'):
            value = argv[index + 1] if index + 1 < len(argv) else ''
        elif argument.startswith('--config='):
            value = argument[len('--config='):]
        elif argument.startswith('-c'):
            value = argument[2:]
        if value is not None and value.split('=', 1)[0].strip().strip('"').startswith('projects'):
            return False
    return True
