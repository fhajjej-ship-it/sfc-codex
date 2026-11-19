# Factory card activity (0.1.6)

Each selected issue keeps two SFC-owned TEXT fields on its original Project
item, through Building, QA, Review, Blocked and Done:

- **SFC Agent**: state and concise absolute report time, including year/date,
  minutes and timezone, for example `🟡 Waiting · 2026-11-07 22:49 CET`.
- **SFC Activity**: a fixed description of the observed action or reason.

These fields do not route work or rename stages. A card in Blocked can correctly
say Waiting when its reviewed PR needs a human merge decision.

Waiting and failure descriptions use the shared supervisor reason formatter.
Known policy gates include the supported next action. For example, a reviewed
PR with 1,024 added/deleted lines and a 400-line automatic merge cap reports both
numbers, the PR number and the need for a human decision on that reviewed PR.
The cap and every merge gate remain unchanged. Sensitive paths and labels select
a fixed category description; their names are not copied into activity or handoff
comments. Worker summaries, findings, command text and raw diagnostics stay in
local receipts; failure comments direct the user to that evidence instead.

Open prerequisites can display up to 20 verified same-repository issue numbers.
Missing, failed and pending GitHub checks have distinct explanations. Malformed
criteria/output, findings attempts and worker dispatch exhaustion explain their
existing recovery limits. No explanation grants a retry, scope change, counter
reset or merge permission. The formatter accepts only allowlisted text keys and
bounded numeric facts, never worker-authored text.

Unverified external merge completion uses the same reason catalog and existing
Waiting activity surface. It directs the operator to retained PR, intended-base,
reviewed-head and issue-closure evidence; it grants no merge retry or issue-close
write. See [manual merge completion and evidence limits](LOCAL-MERGE.md#completing-a-human-merge).

`status` includes the last **recorded** card activity and run explanation;
dry-run waiting entries include a safe explanation alongside existing planner
evidence. Saved handoffs retain validated reason descriptors so a later preview
can show the same PR number, size facts and human action. These are read-only
views, without new board queries for display text.
`watch-status` explains spent time/poll/claim/cycle/no-progress allowances from
the original saved counters and deadline. A host interruption can resume only
the original remaining budget through supported recovery; spent budgets cannot
be renewed by resuming. Starting any further session remains subject to the
existing explicit authorization and lifecycle rules. These fields do not prove
current worker liveness or successful completion.

Card times default to UTC, for example `🟡 Waiting · 2026-11-07 21:49 UTC`.
The optional project setting `activity_timezone` accepts an available IANA zone
such as `Europe/Stockholm`; its rules select CET in winter and CEST in summer,
including local date/year rollover. Onboarding exposes `--activity-timezone`.
Omitting the setting preserves the portable UTC default, independent of the
machine's local timezone and without requiring a timezone database. Invalid or
unavailable zones stop configuration validation rather than silently falling
back; non-UTC zones need system IANA timezone data. Public times omit the
`reported` label, ISO `T`, seconds and long numeric offset. The local activity
receipt and history retain full UTC ISO timestamps with year/date, seconds and
`+00:00`; only the public display changes. Receipt `values` still records the
exact public text for write verification and human-edit detection.

| State | Meaning |
|---|---|
| Running | Supervisor is preparing work or has observed its owned worker/check process active |
| Waiting | Dependency, next supervisor step, CI, criteria, lane decision or human merge decision needed |
| Stopped | User stop or finite cycle/no-progress limit; work is preserved |
| Failed | Process error/timeout, failed checks or actionable lane findings; inspect local evidence |
| Completed | Supervisor verified merge or completed a pinned QA-only assessment |
| Unknown | Prior Running report reconciled without evidence of current liveness |

Independent check-assessment lanes, reviewed handoffs and verified completion
also show `Project verification: N executed, M reused.` Counts describe the
current validated project-check receipt. A reused check refers to its original
execution and logs; it does not claim that another check ran. The linked PR
summary identifies each configured check as executed or reused. Commands,
paths, logs and worker prose stay private. These counts are appended to existing
guarded activity updates, without new write callsites, another polling loop or
per-check board writes. The existing coalescer still considers changed descriptions.
A passing check count is not a completed delivery; the activity state and the
existing merge/publication completion gates still determine that.

Codex [JSONL events](https://learn.chatgpt.com/docs/non-interactive-mode#make-output-machine-readable)
can report a command starting/completing and file
changes observed. Only the event type selects a fixed label; command text,
filenames, arguments, tool payloads, prompts, worker messages and reasoning are
never published. Other events are ignored. A lane without finer supported
events explicitly reports that finer activity is unavailable. Events exceeding
65,536 bytes are discarded and reset the retained substep to that lane fallback,
so a dropped completion cannot keep reporting a finished command as active.
Later supported events can restore finer activity. Check execution is
observed directly by the supervisor. A process exit changes the report to
Waiting while its result is assessed; a successful worker exit alone does not
mean the ticket completed.

Active processes refresh Running approximately every 60 seconds even when they
produce no output. A per-item observer coalesces rapid substep changes for up to
five seconds; transitions and process exit report immediately, subject to API
latency. The latest observed substep wins. No background heartbeat survives the
process or a waiting/terminal state. Each publication uses one GraphQL request
with two targeted field mutations and preflight/readback queries. Updates are
serialized across tickets; field values, clocks and receipts remain per item.
Slow GitHub responses do not delay child-process stop/timeout enforcement.
When a stop arrives between cycles, the supervisor also reports Stopped on
unfinished cards selected in the preceding cycle. Completed cards and blocked
handoffs retain their outcome; unselected cards are untouched.

GitHub text cannot animate, fade, compute relative age or automatically expire a
Running label. An abruptly dead supervisor can leave its last report indefinitely.
Quiet output or an old timestamp proves neither life nor death. The next real run
reconciles saved Running reports to Unknown before observing new work. Read-only
commands leave the saved board untouched. The timestamp records a supervisor
observation, not a guarantee that a process remains alive when someone reads it.

# Setup and existing-project upgrade

After updating the installed plugin to 0.1.6, run these against the selected repo:

```sh
python3 "<plugin>/scripts/sfc.py" --root "<repo>" dashboard --apply
python3 "<plugin>/scripts/sfc.py" --root "<repo>" dashboard
```

New onboarding includes the same repair. It idempotently reuses fields with the
reserved names and TEXT type or creates missing ones. It appends their IDs to
the saved Factory view's `configuration.visibleFieldIds` and reads
`configuration.visibleFields` back. Existing field/option IDs, custom fields,
visible fields, filters and grouping are preserved. Duplicate names, wrong types,
truncated inventories and unverified writes halt rather than recreating fields.
Inventory and saved-view queries include custom multi-select fields so their
IDs remain available during visibility repair.
An unsupported server schema is a setup failure; do not claim visibility is saved.
GitHub's public Column by mutation remains unsupported: follow the returned
pending browser instruction for grouping and save that view. Verify `ready: true`
before declaring setup complete. No live board or browser verification is implied
by the isolated automated tests.

`status`, `doctor`, `dashboard` without `--apply`, lint and dry-run do not migrate
fields or publish activity. Existing configurations remain intact. An actual run
requires the text fields; missing fields stop before worker dispatch with the
explicit upgrade instruction. Never edit an installed cache to upgrade: distribute
from the source package. `scripts/package_plugin.py` includes the activity runtime,
this documentation and its tests in `dist/sfc-0.1.6.zip`; building an archive does
not install or publish it.

# Failed writes and human edits

The supervisor checks project, item, issue, repository and field identity before
writing. It compares both text values with the last confirmed local values (or
empty fields for a first claim), persists intent/history, writes only these two
fields, and reads both back. A new claim does not overwrite preexisting text.
Unexpected edits are preserved and halt reporting. GitHub does not provide CAS
for these text updates, so an edit in the narrow preflight/write race cannot be
atomically protected; readback catches differences observable afterward. Avoid
concurrent manual edits to SFC-owned activity fields.

When a connection reset hides the result of an activity write, SFC reads both
fields back. An exact match for the intended pair and pinned scope confirms the
write, including during restart. SFC never resends the pending mutation.

An unresolved API failure, partial mutation, interruption or mismatched readback leaves
`.sfc/state.json` card `activity.pending`/`activity.error` latched. A lost report
never makes the work pass. Subsequent runs refuse to retry that item's activity
write, including attempts to publish a failure label. Inspect the current run
summary, card `activity` and `activity_history`, and retained raw logs/worktrees.
Public fields contain only fixed descriptions; full diagnostics remain local.

Recovery is an explicit supervisor/operator action after the factory stops:

1. Preserve a copy of the state/receipt and inspect both remote text values,
   item/issue/repository/project identity and the exact field IDs.
2. If both intended values actually arrived, confirm that outcome. If only one
   arrived or a human edited the values, obtain a deliberate resolution. Preserve
   human text unless its owner authorizes SFC to replace it. Do not infer success
   from an API timeout or clear a latch merely to get a run green.
3. Only after that resolution, reconcile the card's local `activity.scope`,
   `item_id` and `values` to the verified remote expectation, set `pending` false
   and remove `error`. Keep the previous intent/history and run summary intact.
   This acknowledges the observed remote result; it does not certify any work
   checks or authorize a merge. Replacing human text requires explicit takeover
   authorization because the next report updates these fields.
4. Read the board back, then resume only the authorized queue. Existing head/base,
   task/config pins, verification and merge authority remain mandatory.
