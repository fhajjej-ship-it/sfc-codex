"""Repository-common creation serialization and fail-closed pickup readiness."""
from __future__ import annotations

import contextlib
import fcntl
import re
from pathlib import Path

from .common import Halt, common_dir, git, read_json

REQUEST_ID = r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}"
MARKER = re.compile(r"<!-- sfc:ticket:(" + REQUEST_ID + r") -->")
MARKER_START = re.compile(r"<!--\s*sfc:ticket:")


@contextlib.contextmanager
def creation_lock(root):
    # Never wait behind a network request, worker, or another lock owner.
    # Setup takes sfc.lock first, then this lock; creation never takes sfc.lock.
    with (common_dir(root) / "sfc-create.lock").open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Halt("SFC ticket creation/setup is busy; retry only after inspecting the request receipt") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def ticket_directory(root):
    return common_dir(root) / "sfc-tickets"


def receipt_directories(root):
    """Include pre-upgrade receipts in all registered linked checkouts, read-only."""
    roots = {Path(root).resolve()}
    for field in git(root, "worktree", "list", "--porcelain", "-z").split("\0"):
        if field.startswith("worktree "):
            roots.add(Path(field[len("worktree "):]))
    return [ticket_directory(root), *(p / ".sfc/tickets" for p in sorted(roots))]


def recorded_request(root, request_id):
    return next((directory / (request_id + ".json") for directory in receipt_directories(root)
                 if (directory / (request_id + ".json")).exists()), None)


def readiness(root, card, repo, project):
    """Return a local actionable wait reason, without locks or remote writes.

    Intent precedes the issue POST, which includes its marker. Atomic receipt
    replacement publishes completion only after final readback. Completion is
    terminal: creators never reopen/rewrite a recorded request.
    """
    body = card.get("body") or ""
    markers = MARKER.findall(body)
    # Prose can discuss the protocol prefix. Count only comment starts, also
    # catching malformed/unclosed comments that cannot yield a valid marker.
    marker_starts = MARKER_START.findall(body)
    prefix = "Ticket creation unverified; "
    if marker_starts and (len(markers) != 1 or len(marker_starts) != 1):
        return prefix + "inspect malformed/ambiguous request marker and creation receipts"
    matches = []
    location = "repository-common/legacy ticket receipt inventory"
    try:
        for directory in receipt_directories(root):
            for path in directory.glob("*.json"):
                location = str(path)
                receipt = read_json(path)
                if not isinstance(receipt, dict):
                    raise ValueError("receipt must be an object")
                issue = receipt.get("issue") or {}
                if (path.stem in markers or receipt.get("request_id") in markers or
                        (receipt.get("repo") == repo and
                         (issue.get("number") == card["number"] or
                          (card.get("id") and issue.get("id") == card["id"]) or
                          (card.get("item_id") and receipt.get("item_id") == card["item_id"])))):
                    matches.append((path, receipt))
    except (OSError, ValueError, KeyError, TypeError, AttributeError, Halt) as exc:
        return prefix + f"inspect unreadable creation journal {location} ({exc})"
    if not matches:
        return (prefix + "inspect missing receipt for request " + markers[0]) if markers else None
    if len(matches) != 1:
        return prefix + "inspect conflicting creation receipts: " + ", ".join(str(p) for p, _ in matches)
    path, receipt = matches[0]
    # Ordinary issues need no extra remote metadata read. Resolve only when
    # there is actual creation evidence whose Project identity must match.
    project = project() if callable(project) else project
    issue = receipt.get("issue") or {}
    verified = receipt.get("verified") or {}
    if (not isinstance(issue, dict) or not isinstance(verified, dict) or
            not isinstance(verified.get("project"), dict) or not isinstance(verified.get("content"), dict) or
            receipt.get("state") != "complete" or receipt.get("repo") != repo or
            receipt.get("project") != project or receipt.get("request_id") != path.stem or
            receipt.get("item_id") != card.get("item_id") or
            any(not issue.get(k) or issue.get(k) != card.get(k) for k in ("id", "number", "url")) or
            verified.get("id") != card.get("item_id") or
            verified.get("project", {}).get("id") != project or
            any(verified.get("content", {}).get(k) != issue.get(k) for k in ("id", "number", "url"))):
        return prefix + f"inspect {path} (state={receipt.get('state')}); reconcile remote identity/fields/labels read-only; no automatic retry"
    return None
