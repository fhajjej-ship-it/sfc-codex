# Bounded foreground watcher (0.1.11)

`watch --start` explicitly authorizes current **and future** matching Backlog/Ready
issues in the actual configured repository/Project for one finite session. No
per-ticket enumeration is required. This is broad queue authority: inspect the
preview, configure exclusions/holds and authorize that policy before starting.
Ready-only and exact-allowlist Backlog eligibility stay unchanged. All modes use
the shared [rolling priority scheduler](SCHEDULING.md).
No existing configuration is migrated and installing/onboarding never starts it.
Version 0.1.12 adds separately authorized [project-chat auto-start](AUTOSTART.md)
through a trusted Codex lifecycle hook, retaining this same watcher and lock.

## Automatic recovery of owned Blocked work

New watcher sessions save `max_recoveries_per_ticket` (default 1, configurable
from 0 to 3) in their authorized policy. Spending is cumulative per retained
ticket, not renewed by another poll, restart or new window. Legacy saved sessions
and auto-start grants without this setting continue with their authorized queue
and gain no automatic recovery authority. Recovery starts from a saved watcher
policy. Merge and publication require separate authority.

The watcher recognizes structured supervisor blockers: an exhausted transient
read, an open same-repository prerequisite, missing project-check execution
evidence, or a failed project check before independent review. It checks identity, current routing scope,
task/configuration, claim, clean retained worktree, exact head/base/PR and original
receipt/log bytes. Holds, exclusions, human handoffs, release journals, ambiguous
writes, source drift and unsupported blockers retain their existing gates.
Waiting dependencies use no worker slot or model call. Eligible recovery follows
the ordinary Priority ordering and consumes a normal bounded scheduler slot. A
failed transient PR read defers the ticket until the next regular polling
interval so unrelated work can proceed. If a prerequisite reopens during checks,
the watcher waits for verified closure and revalidates the completed evidence
before resuming.

Independent QA/security/review can return optional `recovery` with
`kind: "missing-check-evidence"` and `check_ids: [1, ...]` on a Blocked
result with no findings. Only configured check IDs are accepted; worker command
text never becomes execution authority. Requested checks execute fresh on the
pinned candidate, while other passing checks may reuse their immutable contracts.
All independent review runs against the actual completed logs. A missing-evidence
result uses the separate recovery allowance and preserves the available findings attempts. An evidence-only recovery can resume QA at the findings cap without
refunding attempts or granting another Build. A pre-upgrade Blocked ticket can recover when the last accepted
independent result matches the saved Blocked verdict and its exact supervisor
packet has a missing execution receipt or log. The worker's prose does not
classify the legacy blocker.

A new watcher with a positive recovery allowance can retry a failed supervisor
project check once on the same open PR commit and base. It forces the failed
command to run fresh, runs any remaining required commands, and reuses earlier
passes only under their existing evidence contracts. If the checks pass, it
continues with the configured independent QA and review gates. If the check
fails again, the card stays Blocked and the retry remains spent across windows.
This does not launch a Build or renew a Build or findings attempt. A ticket
already blocked after a spent failed-check Build grant can use the same retry
when its original failed check, the accepted Blocked Build, and the unchanged
PR and worktree can all be verified.

Each action is reserved durably before dispatch. A crash cannot refund it or
accept an old passing receipt as fresh execution. Admission and subsequent
recovered stage moves share one intent and readback protocol. It records the
expected stage and local handoff before the write. A lost response can be
confirmed by readback, including on a held ticket, without repeating the
mutation or dispatching more work. A recovered Build records its push and draft
intents before dispatch. It verifies the remote branch and PR before continuing
to QA, including when a response is lost or a hold pauses advancement. Recovered
PR summary and ready changes and protected GitHub merges also require exact
readback before subsequent writes. Admission pins are rechecked before checks,
worker dispatch, stage advancement and merge.
A hold leaves completed checks saved. A later authorized window with the same
scope can resume that reservation after the hold clears and the evidence is
revalidated. Failed or interrupted actions remain spent across windows. Each
execution observes its authorized window's deadline and configuration guards.

Status and offline delivery reports retain the blocker, original evidence pins,
action timestamps, blocked wait and recovery durations, executed/reused checks,
resumed stage and outcome. The board receives fixed recovery activity and a
safe check-count/stage summary. This accounting is observation, not evidence of
measured time savings across customers.

A concrete finding in an accepted QA report can send a rejected candidate back
to Build on the same PR while findings attempts remain, including after an
evidence recovery. Build receives the pinned finding and records the focused
failure and verification results when feasible. A corrected candidate returns
to independent QA with the builder's evidence and the supervisor's current
project checks. Check reuse follows the normal immutable evidence rules. If a
repair leaves the rejected candidate unchanged or the ticket exhausts its
findings allowance, that ticket waits for inspection and other eligible work
continues.

### Automatic retained Build recovery

Set `watch.recover_builds` to `true` before claiming work, then preview and
authorize the watcher with these settings. With a positive
`max_recoveries_per_ticket`, the saved policy allows one automatic retained Build
per ticket. This counts against the same cumulative automatic recovery allowance
as dependency, GitHub read, and missing-evidence recoveries. Restarts and later
windows preserve the spent allowance.

- A timeout recorded by the supervisor during an owned Build can qualify when the
  original result is absent and the retained dispatch, worktree, and logs still
  match.
- An accepted blocked Build can qualify when an operator-configured prerequisite
  probe exits successfully. Set `watch.build_prerequisite_probes` to a map from
  repository issue numbers to direct local command argument arrays. For example,
  `{"42": ["test", "-f", ".developer-prerequisite-ready"]}` checks for a
  project-owned readiness marker. Codex runs the probe using its workspace
  sandbox in the retained worktree. Issue and worker text supply no commands.

An unsuccessful probe keeps the card Blocked and waits before another probe.
Probe attempts run in normal scheduler slots and remain bounded by the watcher
window and its cycle budget. A successful probe is recorded before admission.
Changed task, base, source, retained evidence, or unresolved writes prevent
automatic dispatch. The recovery preserves partial changes, requires the normal
project checks and independent reviews, and inherits merge authority from the
saved run. Each probe should be quick, read-only, and test the actual prerequisite.
If the watcher stops before admission, a later session leaves the unused grant
and its Blocked card in place and continues processing other eligible cards.

## Start, preview, status and stop

Use the installed plugin script with the target repository root:

```sh
python3 "<plugin>/scripts/sfc.py" --root "<repo>" watch --dry-run
python3 "<plugin>/scripts/sfc.py" --root "<repo>" watch --start
python3 "<plugin>/scripts/sfc.py" --root "<repo>" watch-status
python3 "<plugin>/scripts/sfc.py" --root "<repo>" stop
python3 "<plugin>/scripts/sfc.py" --root "<repo>" watch --resume <recorded-session-id>
```

Keep the foreground command running and monitor its final outcome. The host must
stay awake and connected and the process must remain running. This is no daemon,
OS service, scheduler, web server or automatic login startup. Stop, SIGINT and
SIGTERM interrupt idle waits in approximately 0.1 seconds and stop only owned
active child process groups. GitHub/tool requests retain their own finite timeout;
network IO may finish before a stop is observed. Evidence/worktrees are retained.
No stored PID is used as signalling authority.

The elapsed work deadline still stops admission, workers, project checks, PR
publication and merge/ref writes. An atomic local merge **acknowledged before**
that deadline has a separate completion-only allowance ending 30 seconds after
the original deadline. Its journal pins the original session, Project/item/issue,
scope, merge authority and evidence; restart cannot renew it. Within that allowance
only readback and the matching Done/Completed writes may finish, after actual
MERGED PR, CLOSED issue and candidate reachability on the intended base are proven.
Holds, exclusions (including an explicitly excluded #15), manual edits and evidence
drift still halt completion. Completion IO timeouts are clipped to the remaining
allowance; explicit stop/signals still win. No worker or claim budget is reset.

An expired session can use `--resume SESSION --allow-merge` only to finish its
already acknowledged journal within that same allowance. Unknown/late merge
responses grant no completion authority and never retry a ref mutation. Exhaustion
is latched durably; inspect the retained merge/Done/activity receipts and reconcile
manually if unfinished. Done/activity intents and distinct completion observations
are retained; ambiguous writes and old activity latches are never cleared/retried.
The supervisor keeps its common-git lock throughout, with no background service.

`watch --dry-run` reads the same verified queue/planner without claims, workers,
setup repair or state writes. It can inspect saved evidence while a stop flag is
present and leaves the flag in place. Its options show repository/Project, future-ticket
policy, stages, exclusions, hold label and budgets; order and waiting reasons
show actual selection. It grants no authority. `watch-status` works without a
GitHub request and includes recorded session/scope, last/next poll, stop/wait
reason and remaining budgets. `status` also includes that record. Lock occupancy
alone cannot identify a live watcher: it could be another supervisor, so status
reports that uncertainty. No held lock means inactive/stale. Recorded timestamps
or old Running reports never prove current operation.

## Narrow exclusions and actual selection

Optional project configuration (only while stopped) or explicit session flags:

```json
"watch": {
  "poll_interval": 30,
  "max_seconds": 3600,
  "max_polls": 120,
  "max_claims": 10,
  "excluded_issues": [],
  "hold_label": "sfc-hold"
}
```

`--exclude-issue <number>` can repeat and adds to configured exclusions. These are
stable numeric issue IDs within the configured repository. `--hold-label <name>`
selects a nonempty, case-insensitive hold label; default `sfc-hold`. Watch never
creates/removes labels, rewrites Priority or changes manual hold decisions.
Held/excluded owned work waits too. The SFC development demonstration issue #15
must be explicitly excluded if a development watcher is eventually authorized,
for example `--exclude-issue 15` on both preview and start. There are no hardcoded
maintainer IDs in runtime selection. This implementation does not enable it.

Each poll reads the complete paginated verified repository/Project/field/option/
item/value/issue-label inventories used by priority pickup. Unknown stages never
become Backlog; unmapped Priority and missing criteria wait with reasons. Open,
unreadable, malformed or self prerequisites wait. Subsequent polls discover
arrivals, changed Priority, removed holds and newly closed prerequisites.

Unassigned owned Building/QA/Review and fresh Backlog/Ready work rank together
by actual mapped P0–P3, creation time and issue number. `max_workers` bounds
whole tickets through every lane/check. A released slot refills after fresh queue
readback while peers remain busy. Work is never preempted. Claims are sequential, journaled and read back;
task/scope/prerequisites revalidate immediately before claim and owned dispatch.
Verified claims retain attempts/worktrees across restarts. Completed, blocked,
uncertain claims and human handoffs do not dispatch again; human handoffs do not
prevent independent work. Manual movement of claimed work back to a fresh stage
requires explicit inspection, never a duplicate claim.

## Finite spending and restart

Defaults are 30 seconds between logical polls, at most 3600 elapsed seconds,
120 polls and 10 new claims. CLI overrides: `--poll-interval` (1–3600 seconds),
`--max-seconds` (1–86400), `--max-polls` (1–10000), `--max-claims` (1–1000).
All must be positive integers. Each logical poll can perform multiple paginated
API reads. Any exhausted session budget ends the session; it never renews itself.
The claim budget also counts uncertain writes and conservatively reserved intent
spending, even if a crash prevents a remote claim.

Existing `max_cycles`, `max_no_progress`, `max_attempts`, worker/check timeouts and
retry policy remain enforced. Idle/blocked polls spend no paid worker attempts
or admission cycles. Fully reviewed, nondraft PRs waiting only for GitHub checks also
spend only poll/time budgets and leave worker capacity available for independent
arrivals. Preview and recorded waiting reasons show this deferral. Each poll
checks the saved head/task/config evidence and current PR identity/checks without
remote writes; stale evidence or a required human handoff still enters review.
When checks succeed, normal review revalidates the current base and every merge
gate before any merge write. Watch activation still grants no merge authority.
Each ticket admission reserves a cycle before claim and conservative no-progress
spending before worker launch. Live outstanding reservations are ongoing work,
not completed failures; after interruption they remain spent. Verified terminal,
stage or new accepted evidence progress without findings can reset consecutive
failures while preserving outstanding reservations.
Polling and unused capacity spend neither. See [precise rolling accounting](SCHEDULING.md).
Known findings and terminal Blocked outcomes spend only their per-ticket
allowances; they do not add to or refund existing observer no-progress spending.
An owned worker/check timeout or exhausted output allowance is journaled and
the ticket is moved to Blocked, leaving the watcher free to admit other eligible
work and observe future arrivals. The failed ticket keeps its original work,
attempts and output reservations and requires explicit operator recovery; it is
not retried by polling. A partially published failure remains a shared recovery
gate, as do uncertain claim/ref/completion/activity writes and unknown errors.
Repeated true no-progress results still hit the existing shared cap. Idle waits
and all work count against the elapsed deadline. Offline time
also counts on resume. Session time uses the host wall clock; keep its clock
correct. A backwards clock does not renew poll/claim/work allowances.

`--resume SESSION` reloads that exact session's scope/settings, deadline, counters,
config digest and merge opt-in. It cannot change authority or revive a spent
budget. Custom settings are loaded from the session; omitted overrides need not
be repeated. If its original opt-in included `--allow-merge`, repeat that flag.
Changes to settings/config require a separately explicit new `--start`, never an
automatic fallback. Existing card attempts and uncertain journals still apply
in a new session. Use the original checkout root to preserve its saved work.
Starting/resuming a watcher from another linked checkout with a recorded watcher
is rejected; inspect the recorded root and use it. One common-git lock spans
run/watch/linked checkouts, including all idle waits, with no per-poll lock gap.

Records:

- Common git directory `sfc-watch.json`: latest session and durable counters.
- Target `.sfc/watch-sessions/<id>.json`: retained per-session record.
- Target `.sfc/state.json`, `.sfc/runs/<run>/summary.json`: card claims, attempts,
  pins, selected orders, waits and outcome. Each resume has a separate run receipt.
- Common git directory `sfc-stop`: shared stop request; legacy target `.sfc/stop`
  is still recognized. Explicit start/run/resume clears prior stop under lock.

## Ticket creation alongside a supervisor

Authorized `create-ticket` can finish while run/watch holds its supervisor lock,
including during idle waits and worker execution. The supervisor keeps
`<common-git-dir>/sfc.lock` throughout its session; a second run/watch is still
rejected from every linked checkout. Creation uses a separate common
`sfc-create.lock`, held from request validation/preflight through final receipt
publication (or failure recovery). It never acquires the supervisor lock and
never writes watcher state or configuration. Explicit onboarding and
`dashboard/priority-setup/type-setup --apply` take the supervisor lock first,
then the creation lock, preserving their serialization against both operations.
All acquisitions are nonblocking. Contention returns an actionable busy error;
there is no lock wait, automatic retry or worker-attempt spending.

New request receipts are canonical at
`<common-git-dir>/sfc-tickets/<request-id>.json`, shared across linked checkouts.
Use `git rev-parse --git-common-dir` to locate them. Creators serialize and check
for existing requests before any remote write. A recorded ID is terminal for
creation: success, failure and ambiguity all reject a repeat without modifying
the receipt or issuing another POST. Different IDs do not overwrite each other.
Pre-upgrade `.sfc/tickets` receipts in the current and registered linked checkouts
are also checked, read-only; they are not automatically moved or rewritten.
Conflicting copies require inspection. Keep those checkouts/receipts available.

The initial issue POST includes the stable `sfc:ticket` request marker. Intent
is saved before that POST. Run/watch check local evidence at planning, claim,
dispatch and reconciliation, before activity or issue annotations. An SFC marker
requires one matching **complete** receipt with verified issue/repository/Project/
item identity. Known receipt identities also prevent a removed marker from
bypassing incomplete creation. Missing, malformed, conflicting, unreadable or
incomplete evidence waits with the receipt/request and recovery reason; no
claim, annotation, worker or attempt is spent. Unreadable journals conservatively
wait even if their affected identity cannot be established. Ordinary issues
without a marker or matching receipt keep their existing eligibility rules.
Plain-text mentions of `sfc:ticket:` do not count as markers; request markers
start an HTML comment (`<!-- sfc:ticket:REQUEST-ID -->`). Malformed or duplicate
request-marker comments still wait for inspection.

Remote Backlog/Priority values are insufficient: readiness is published only by
atomic local receipt replacement **after** final identity, body/title, field,
setup and label readback. A creator paused after field writes remains ineligible.
A dead creator releases its OS lock but leaves a noncomplete intent journal, so
pickup still waits after restart. A completed receipt becomes eligible on a
subsequent live poll, subject to unchanged Priority/criteria/dependency/hold/
exclusion rules and session budgets. Waiting spends only poll/time budgets.

On uncertainty, inspect the named journal and actual remote issue, membership,
fields and labels **read-only** first. Retain all partial/ambiguous evidence.
Do not delete receipts, substitute a new request ID, mark a journal complete or
retry a remote write just to unblock pickup. There is no automatic recovery or
completion command: any manual reconciliation needs separate authority and
verified evidence. Lost responses retain the existing read-only identity
recovery; they never cause a second creation/enrollment/field mutation.

This is same-machine, shared-common-git coordination, not cross-machine or
cross-clone deduplication. All participating runtime entrypoints must use this
protocol; stop an older supervisor before upgrading it. Removed/inaccessible
legacy checkouts, copied/deleted/edited journals and out-of-band remote/local
changes cannot provide a distributed exactly-once guarantee. GitHub offers no
atomic CAS for these fields: preflight/readback detect observable manual edits,
not edits wholly between observations. If an unknown POST outcome's marker and
identity evidence are both removed, local matching cannot identify that issue.
Atomic replacement covers process interruption, not guaranteed power-loss
persistence. No model, merge, configuration or authorization policy changes.

## Merge and uncertainty

Watch alone grants no merge authority. New onboarding defaults to auto policy;
explicit human policy remains available and existing saved policies stay unchanged.
Auto still needs project `merge_policy: auto` and separately authorized `watch --allow-merge`,
with exact head/base/task/config receipts, independent QA/security/review, local
checks, GitHub check policy, sensitive/size/human-only gates and the existing
atomic non-force or protected merge protocol. No gate or fallback is weakened.

Pending local merge journals must remain inside verified live membership and the
session's holds/exclusions before recovery writes. Watch recovery additionally
requires the separate auto-merge authority. Removed/held/excluded journals halt
before recovery or worker dispatch; preview shows the scope blocker. An unresolved
claim receipt also halts a restarted watcher until inspected; it never dispatches
independent work around that uncertainty. Never widen
the queue or remove a hold merely to finish recovery.

Only normal acknowledged atomic-merge recognition latency on the exact intended
candidate/base receives a bounded five one-second **read-only** observation
window. Observations persist; restart never renews that allowance. Completion
still needs exact identity/rules, candidate reachability on the intended base,
actual MERGED PR and CLOSED issue and verified Done. A lost ref response, old or
partial snapshot, permissions/identity/capability uncertainty or unresolved
confirmation halts with evidence. There is never another ref mutation, alternate
merge API, blind completion retry or cleared latch. Ordinary Ready-only and scoped
Backlog runs share the bounded recognition window, without the watcher's cutoff
allowance. See [local merge recovery](LOCAL-MERGE.md) and [claim recovery](BACKLOG-PICKUP.md).

Tests use temporary repositories, fake GitHub/Codex and injected time/waits.
No live broad watcher, customer-board operation or global installation is implied.
