"""Shared, read-only ticket-body contract for creation, dispatch and review."""
from __future__ import annotations

import re

from .common import Halt


def acceptance_criteria(body):
    """Read one acceptance section, ignoring empty list placeholders."""
    sections = re.split(r'^##[ \t]+Acceptance criteria[ \t]*$', (body or '').replace('\r\n', '\n'),
                        flags=re.M | re.I)
    if len(sections) == 1:
        raise Halt('missing ## Acceptance criteria')
    if len(sections) != 2:
        raise Halt('Use one ## Acceptance criteria section')
    section = re.split(r'^##(?:[ \t]+|$)', sections[1], maxsplit=1, flags=re.M)[0]
    items = []
    current = None
    for line in section.splitlines():
        marker = re.match(r'^[ \t]*[-*](?:[ \t]+(.*)|$)', line)
        if marker:
            text = re.sub(r'^\[[ xX]\][ \t]*', '', marker[1] or '').strip()
            current = [text] if text else None
            if current is not None:
                items.append(current)
        elif current is not None and line.strip():
            current.append(line.strip())
    if not items:
        raise Halt('acceptance criteria need observable bullet points')
    return ['\n'.join(lines) for lines in items]


def dependencies(body):
    headings = list(re.finditer(r'(?im)^##[ \t]+Blocked by\b[^\r\n]*', body or ''))
    if len(headings) > 1:
        raise Halt('Use one ## Blocked by section')
    if not headings:
        return []
    heading = headings[0]
    if not re.fullmatch(r'##[ \t]+Blocked by[ \t]*', heading[0], re.I):
        raise Halt('Malformed dependency heading: use ## Blocked by on its own line')
    section = re.split(r'(?m)^##\s', body[heading.end():], maxsplit=1)[0]
    numbers = set()
    for line in section.splitlines():
        if not line.strip():
            continue
        match = re.fullmatch(r'\s*(?:[-*]\s+)?#([1-9][0-9]*)\s*', line)
        if not match:
            raise Halt('Malformed dependency: use one same-repository #issue per bullet; put notes under a separate ## heading')
        try:
            number = int(match[1])
        except ValueError as exc:
            raise Halt('Malformed dependency: issue number is too large to parse') from exc
        numbers.add(number)
    return sorted(numbers)


def lint_card(card):
    """Check syntax now; check self-dependency only with a known issue number."""
    problems = []
    try:
        acceptance_criteria(card.get("body"))
    except Halt as exc:
        problems.append(str(exc))
    try:
        if card.get("number") in dependencies(card.get("body")):
            problems.append("issue depends on itself")
    except Halt as exc:
        problems.append(str(exc))
    return problems
