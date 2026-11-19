"""Supervisor-owned public activity. Only allowlisted action/reason keys become text."""
from __future__ import annotations

import datetime as dt
import json
import threading
import time

from .common import (Halt, Stopped, TimedOut, ObserverInactive, _observer_guard,
                     _observer_readback_guard, activity_zone, now)
from .evidence_summary import render_verification_check_counts
from .rate_limit import ConnectionReset
from .reasons import TEXT, render_many

FIELD_NAMES = ("SFC Agent", "SFC Activity")
STATES = {"Running": "🟢", "Waiting": "🟡", "Stopped": "⚪", "Failed": "🔴",
          "Completed": "✅", "Unknown": "❔"}
ACTIONS = {
    "recovered": "SFC verified the blocker, retained saved work and evidence, and resumed the saved stage",
    "recovery_wait": "SFC observed a recoverable condition; waiting for bounded verification",
    "prepare": "Preparing isolated work",
    "build": "Build worker active; finer activity unavailable",
    "qa": "QA worker assessing evidence; finer activity unavailable",
    "security": "Security worker assessing evidence; finer activity unavailable",
    "review": "Review worker assessing evidence; finer activity unavailable",
    "native_review": "Native Codex review active",
    "review_baseline": "Review route: QA, security, then independent review",
    "review_native_baseline": "Review route: QA, security, then native Codex review",
    "review_behavioral": "Review route: QA, any required security, then native Codex review",
    "review_provisional_qa": "Review route: provisional QA, then independent assessment of completed checks",
    "review_risk": "Review route: native Codex review, then any required QA or security checks",
    "review_gap": "Acceptance evidence incomplete; targeted QA or additional executed checks required",
    "specialist_gap": "Resolve outstanding independent review requests",
    "qa_human": "Human QA-only assessment required. Inspect the pinned evidence before closing the issue.",
    "command": "Worker command active",
    "edited": "File changes observed",
    "checks": "🧪 Running project checks",
    "assess": "Assessing process result",
    "next": "Waiting for the next supervisor step",
    "done": "Completion verified by supervisor",
}
ACTIONS.update({key: TEXT[key] for key in (
    "dependency", "human", "external_merge", "ci", "policy", "criteria", "failed", "worker_exit",
    "worker_result", "timeout", "attempts", "findings", "check_failed", "stop",
    "limit", "unknown", "output_budget", "worker_blocked", "interrupted")})


class ActivityError(Halt):
    """An ambiguous/edited activity value requires explicit reconciliation."""


def activity_fields(fields):
    result = {}
    for name in FIELD_NAMES:
        matches = [f for f in fields if f["name"] == name]
        if len(matches) > 1 or (matches and matches[0].get("dataType") != "TEXT"):
            raise Halt("SFC activity field name is ambiguous or is not TEXT; preserve it and reconcile")
        if matches:
            result[name] = matches[0]["id"]
    return result


class EventReader:
    """Bounded JSONL reader. Never interpret messages, command text or tool output.

    Codex item metadata describes observed events, not trusted model progress.
    Oversize events invalidate finer activity; malformed/unknown events are ignored.
    """
    def __init__(self, path):
        self.path = path
        self.offset = 0
        self.partial = b""
        self.discard = False

    def poll(self):
        with self.path.open("rb") as source:
            source.seek(self.offset)
            chunk = source.read(65536)
            self.offset = source.tell()
        latest = None
        for part in chunk.splitlines(keepends=True):
            complete = part.endswith(b"\n")
            if len(self.partial) + len(part) > 65536:
                self.partial = b""
                self.discard = True
                # A dropped event may have completed the retained command.
                # Invalidate immediately, even before its newline arrives.
                latest = "activity_unavailable"
            if not self.discard:
                self.partial += part
            if not complete:
                continue
            if not self.discard:
                try:
                    event = json.loads(self.partial)
                    item = event.get("item", {})
                    kind = item.get("type")
                    if event.get("type") == "item.started" and kind == "command_execution":
                        latest = "command"
                    elif (event.get("type") == "item.completed" and kind == "file_change" and
                          item.get("status") == "completed" and isinstance(item.get("changes"), list) and
                          item["changes"]):
                        latest = "edited"
                    elif event.get("type") == "item.completed" and kind == "command_execution":
                        latest = "worker_idle"
                except (ValueError, AttributeError, TypeError):
                    pass
            self.partial, self.discard = b"", False
        return latest


class Activity:
    """One serialized writer, independently coalesced per Project item.

    Receipts are persisted before mutations. A partial/unknown write is latched
    across restarts; there is no automatic retry. GitHub offers no field CAS, so
    check for human edits immediately before and after targeted updates.
    """
    def __init__(self, github, record, save, *, clock=time.monotonic, wall=now, state_lock=None,
                 timezone="UTC"):
        self.gh, self.record, self.save = github, record, save
        self.clock, self.wall = clock, wall
        self.zone = activity_zone(timezone)
        self.lock = threading.RLock()
        self.state_lock = state_lock or threading.RLock()
        self.last = {}

    def reconcile(self, card, guard):
        """Confirm a pending pair by readback before any further activity write."""
        with self.lock:
            r = self.record(card)
            receipt = r.get('activity', {})
            if (receipt.get('pending') is not True or
                    receipt.get('item_id') != card['item_id'] or not receipt.get('scope')):
                raise ActivityError('Unresolved activity write; inspect saved receipts before resuming')
            guard()
            scope, values = self.gh.activity_snapshot(card)
            guard()
            if scope != receipt['scope'] or values != receipt.get('values'):
                raise ActivityError('Pending activity differs from live fields; preserve intent for explicit recovery')
            confirmed = {**receipt, 'pending': False, 'readback_at': now()}
            confirmed.pop('error', None)
            with self.state_lock:
                r['activity'] = confirmed
                self.save()

    def report(self, card, state, action, *, coalesce=False, guard=None, prewrite=None, reasons=None,
               progress=None, check_counts=None):
        if state not in STATES or action not in ACTIONS:
            raise Halt("Invalid public activity key")
        description = render_many(reasons) if reasons is not None else ACTIONS[action]
        if progress is not None:
            route, pending = progress
            labels = {'qa': 'QA', 'security': 'Security', 'review': 'Review', 'human': 'Human decision'}
            if (route not in ('review_risk', 'review_behavioral', 'review_provisional_qa',
                              'review_native_baseline', 'review_baseline') or not set(pending) <= labels.keys()):
                raise Halt('Invalid review progress keys')
            if action != route:
                description = f"{description.rstrip('.')}. {ACTIONS[route]}"
            if pending:
                description += ". Pending: " + ", ".join(labels[key] for key in pending)
        if check_counts is not None:
            description = f"{description.rstrip('.')}. {render_verification_check_counts(check_counts)}"
        key = card["item_id"]
        # Active observations may not publish after child cancellation/exit.
        # Leave the normal first post-write readback under its existing guards;
        # only waits/retries inherit the child's cutoff at that boundary.
        active_guard = _observer_guard.get() if state == "Running" else None
        with self.lock:
            r = self.record(card)
            receipt = r.get("activity", {})
            if receipt.get("pending") or receipt.get("error"):
                raise ActivityError("Activity write unresolved; inspect remote values and local receipt before explicit recovery")
            previous = self.last.get(key)
            elapsed = self.clock() - previous[0] if previous else float("inf")
            if previous:
                unchanged = previous[1:] == (state, action, description)
                if (unchanged and (state != "Running" or elapsed < 60)) or (coalesce and elapsed < 5):
                    return
            stamp = self.wall()
            # Parse/normalize our clock, never accept arbitrary text as a public timestamp.
            instant = dt.datetime.fromisoformat(stamp).astimezone(dt.timezone.utc)
            stamp = instant.isoformat(timespec="seconds")
            display = instant.astimezone(self.zone).strftime("%Y-%m-%d %H:%M %Z")
            values = [f"{STATES[state]} {state} · {display}", description]
            intent = {"state": state, "action": action, "reported": stamp,
                      "item_id": key, "values": values, "pending": True}
            mutation_started = False
            try:
                # Cheap stop/time guard at IO boundaries; live policy preflight
                # runs once immediately before persisting the write intent.
                if guard:
                    guard()
                scope, current = self.gh.activity_snapshot(card)
                if receipt and (receipt.get("scope") != scope or receipt.get("item_id") != key):
                    raise ActivityError("Activity scope changed; preserve remote values and reconcile")
                expected = receipt.get("values", ["", ""])
                if current != expected:
                    raise ActivityError("Activity fields edited externally; preserving values for reconciliation")
                if prewrite:
                    prewrite()
                if guard:
                    guard()
                if active_guard:
                    active_guard()
                intent.update(scope=scope, previous_values=list(current))
                with self.state_lock:
                    r["activity"] = intent
                    r.setdefault("activity_history", []).append(intent)
                    self.save()
                if guard:
                    guard()
                if active_guard:
                    active_guard()
                mutation_started = True  # Any subsequent failure retains the write latch.
                try:
                    self.gh.write_activity(card, scope, values)
                except ConnectionReset:
                    # The request may have landed. Verify both fields without
                    # sending another mutation.
                    intent['response_lost'] = True
                if guard:
                    guard()
                readback_guard = _observer_readback_guard.get()
                token = _observer_guard.set(readback_guard)
                try:
                    if readback_guard:
                        readback_guard()
                    checked_scope, checked_values = self.gh.activity_snapshot(card)
                    if readback_guard:
                        readback_guard()
                finally:
                    _observer_guard.reset(token)
                if guard:
                    guard()
                if checked_scope != scope or checked_values != values:
                    raise ActivityError("Activity write not verified; inspect remote state before retry")
                with self.state_lock:
                    intent["pending"] = False
                    self.save()
            except Exception as exc:
                inactive = isinstance(exc, ObserverInactive) or isinstance(exc.__cause__, ObserverInactive)
                if inactive and not mutation_started:
                    # The child ended before any mutation was called. Retain a
                    # cancelled intent as history, never as a remote expectation.
                    with self.state_lock:
                        if r.get('activity') is intent:
                            intent.update(pending=False, not_written=True)
                            if receipt:
                                r['activity'] = receipt
                            else:
                                r.pop('activity', None)
                            self.save()
                    return
                if isinstance(exc, (Stopped, TimedOut)) and not r.get('activity', {}).get('pending'):
                    # Preflight read never wrote. A cooperative deadline/stop is
                    # not an uncertain activity mutation; keep original receipt.
                    raise
                # Preserve the prior expectation and all attempted values, including
                # interrupted BaseException writes via the persisted pending intent.
                with self.state_lock:
                    r.setdefault("activity", receipt)["error"] = str(exc)
                    self.save()
                raise ActivityError("Activity reporting halted; detailed diagnostics retained locally") from exc
            self.last[key] = (self.clock(), state, action, description)

    def observe(self, card, default, *, progress=None, check_counts=None):
        current = default
        active = True

        def observer(event):
            nonlocal current, active
            if not active:
                return
            if event == "inactive":
                active = False
                self.report(card, "Waiting", "assess", progress=progress, check_counts=check_counts)
                return
            if event in ("command", "edited"):
                current = event
            elif event in ("worker_idle", "activity_unavailable"):
                current = default
            self.report(card, "Running", current, coalesce=event != "started", progress=progress,
                        check_counts=check_counts)
        return observer
