# Run, resume and stop

Read `.sfc/config.json` and confirm it still describes the requested work.
For a saved interrupted Build timeout, supervisor base conflict, pinned failed
project check, malformed Build, verified unsupported-model startup rejection or
resolved blocked-worker prerequisite, inspect the
saved work and use the stopped supervisor's `recover-ticket --issue N
--request-id ID --reason TEXT` preview. With explicit user recovery authority,
the same command with `--grant-one` permits one watcher-eligible complete Build
from the pinned saved work. It changes no original spending or merge limits;
re-arm/resume only the already authorized watcher scope. A spent supplement or
watcher restart supplies no new authority for malformed output; obtain explicit
one-Build authority and preserve the original results. See WORKER-RESULTS.md.
For model-startup recovery, explicitly configure a supported replacement model
first; preserve the original dispatch, failure log and missing result. This is
not an automatic fallback or permission to retry a worker that began work.
An exhausted Build-output allowance can use the separately authorized one-use
`recover-worker-output` command described in
[the recovery contract](../../../docs/WORKER-RESULTS.md#explicit-one-use-build-recovery-0113-dev4).
Preview first; it never resets the original attempts or grants merge authority.
An independently rejected saved PR can use `recover-findings` only with new
explicit one-Build authority. Preview it first and follow the corrective section
of the recovery contract. Preserve old spending and use a named scoped run;
generic watching never spends this grant.
An owner may additionally authorize `recover-findings --pin-legacy-startup`
for a retained legacy Build folder with its original prompt but no result, log,
dispatch or receipt. The opt-in pins those absences and the prompt hash. It
never infers that no work ran, recreates evidence, refunds reservations or
broadens normal output recovery. Later artifacts or prompt changes invalidate
the one-use grant before dispatch. Keep this explicit exception and its scope
in the operator's authorization receipt.

Read-only preflight commands:

```sh
python3 "<plugin>/scripts/sfc.py" --root "<repo>" doctor
python3 "<plugin>/scripts/sfc.py" --root "<repo>" lint
python3 "<plugin>/scripts/sfc.py" --root "<repo>" run --dry-run
```

`run` claims eligible Ready issues and resumes saved Building/QA/Review claims.
Dependencies must be closed. Ready means permission to process a card only
within the selected project and the user's authorized factory scope. If Ready
contains unrelated work, have the user select the queue before running.

```sh
python3 "<plugin>/scripts/sfc.py" --root "<repo>" run
```

For an explicitly authorized selection of Backlog/Ready work, use the opt-in
with a repeated issue allowlist, after reading
[the pickup/recovery contract](../../../docs/BACKLOG-PICKUP.md):

```sh
python3 "<plugin>/scripts/sfc.py" --root "<repo>" run --dry-run --include-backlog --issue <authorized-number>
python3 "<plugin>/scripts/sfc.py" --root "<repo>" run --include-backlog --issue <authorized-number>
```

Keep exactly the authorized scope in both commands. The preview shows mapped
Priority/actual creation-age order across saved unassigned and fresh work, occupied
slots, capacity waits and
actionable skip reasons; skipped fresh cards get no GitHub/activity writes.
Claims journal intent, revalidate and read back state before worker dispatch.
Unresolved claim receipts require inspection across restarts, with no automatic
write retry. Priority annotation alone never starts work. Do not include demo
#15, unrelated Ready or Blocked work without separate user authorization. This
mode is per-run only and grants no merge authority or project-config changes.
Pending local merge journals must also be within that authorized issue scope;
otherwise preview reports the blocker and execution halts before recovery writes.

Monitor the command to completion using the host's process tools; provide
meaningful updates while it works. Do not detach and call dispatch success.
New-project onboarding defaults to `merge_policy: auto`; existing saved
policies remain unchanged. Auto merge also requires separately authorized
`run --allow-merge` or `watch --allow-merge`. Once the user has authorized automatic
merges for the selected scope, pass that flag and continue eligible merges without
routine per-PR approval or asking for the same authority again. Without that
authority, or with explicit `merge_policy: human`, verified PRs wait in Blocked
for human merge. Re-running reconciles externally merged PRs before the next admission.
The default policy grants no run scope or new watcher session; named-session
settings, merge authority, counters and deadlines remain immutable on resume.

Each card uses Build → QA → Security → Review. QA executes the nonempty
project verification commands, and checks expected behavior plus test gaps.
Security and Review are separate read-only Codex invocations. Receipts pin
configuration, issue content, head and base. Findings return to Build; a
blocked verdict or the configured attempt cap parks the issue in Blocked.
Read-only lanes receive the supervisor's pinned project-check receipt and raw
logs. They independently assess behavior and test gaps without rerunning suites
that require writable temporary fixtures in the read-only sandbox. Missing,
failed or stale receipts prevent dispatch; write permissions are not broadened.

The merge gate checks the current PR identity/head, passing GitHub checks,
size/sensitive policy and all lane receipts. Default `github-protected` mode
requires strict base protection. It runs
project checks again in a scratch worktree against freshly fetched base.
Changed evidence or base requires QA again. GitHub's expected-head argument
and strict branch protection close concurrent head/base changes at merge time.

Explicit `merge_mode: local-verification` uses atomic non-force expected-value
updates of both refs and fast-forwards to the exact reviewed head. It requires
the current base in that head, all independent lanes, clean candidate checks and
unprotected/unruled refs. It preserves branch history, with no squash/rebase.
Absent GitHub checks still need explicit `allow_no_checks: true`. No fallback or
blind retry is allowed. Retain journals and worktrees after uncertainty; restart
only reads back before deciding completion. Done requires actual candidate
reachability, MERGED PR and CLOSED issue. Read [local merge and recovery](../../../docs/LOCAL-MERGE.md)
before opting in; setup does not authorize a run or merge.
Ordinary Ready-only and scoped Backlog runs automatically allow five durable,
one-second read-only confirmation observations after an acknowledged exact landed
transaction when PR recognition or issue closure lags. They recheck live identity,
rules, task, stage, routing scope and merge authority without rerunning workers or
project checks. Stops/config drift interrupt; spending never resets on restart.
Exhaustion or uncertainty retains the journal for inspection, never a write retry.

Concurrency and all spending limits are finite: `max_workers`, `max_cycles`,
`max_no_progress`, `max_attempts`, `worker_timeout`, `verify_timeout`. Models
use the project-wide `model` setting unless `models` overrides a role.
New onboarding recommends GPT-6.1 Sol, High reasoning and Fast speed, with
configurable `reasoning_effort` and `service_tier`. Missing/null settings inherit
Codex CLI configuration, preserving existing projects. Confirm the selected
model, effort and tier are available to the account before starting; `doctor`
does not establish that availability. Do not silently substitute a different
model/tier, downgrade reasoning or hardcode a pricing ladder.

All run/watch modes use [rolling admission](../../../docs/SCHEDULING.md):
unassigned saved and fresh work compete by verified P0–P3, actual creation time,
then issue number. A released slot refills without waiting for peers; running
cards retain a slot across their lanes/checks. `max_cycles` reserves one ticket
admission before claim/dispatch. Polls and durable waits spend no failure cycles;
outstanding dispatches retain conservative spending after interruption. Missing
Priority setup halts without repairing it or guessing urgency. Preview/status
record occupied slots, the next eligible choice and capacity/eligibility waits.
Finite runs immediately replan other eligible work after an admission rejection.
The rejected ticket waits until a later authorized run. See its waiting reason;
do not enlarge the scope or retry an uncertain claim to fill capacity.

To stop, call `stop` from another command. The supervisor polls the stop flag,
terminates only its owned child process groups, and retains claims/worktrees.
Wait for its final result. `run` removes the prior stop flag after acquiring
the repository lock and resumes. Rate-limit/auth/ambiguous-write failures halt
without automatic retries. Inspect remote state before restarting.

During watching, worker/check timeouts and exhausted output allowances stop
only their ticket: the supervisor journals the retained failure, verifies its
Blocked/activity publication and releases the slot. Known findings and terminal
Blocked results keep spending their original per-card limits, not the observer's
no-progress allowance. Other eligible/future work continues. Failed tickets stay
quarantined until explicitly reconciled; polling never resets or retries them.
Pre-upgrade saved timeout/limit activity also excludes its ticket from automatic
pickup. Pending failure-publication, claim/ref/activity/completion uncertainty,
operator stop, changed authority and unknown errors still stop the supervisor.
Finite non-watcher runs retain their previous failure accounting.

Useful records:

- `.sfc/state.json`: current claims, attempts, pins, feedback and transition history.
- `.sfc/runs/<run>/summary.json`: final run snapshot.
- Per-lane prompt, raw log, result and receipt; per-check exit code and raw log.

Evidence paths are local. PRs/issues must not claim those logs are publicly
available. Publish a sanitized report only when requested.


Factory cards retain `SFC Agent` (state + absolute UTC report time) and
`SFC Activity` throughout stage moves. Supervisor-owned updates show observed
worker/check activity, coalesced substeps and an approximately 60-second running
refresh. Missing finer events are labeled unavailable; never infer model
progress or publish raw output. Human merge handoffs are Waiting, never Completed.
An abruptly dead supervisor can leave its last Running report. Quiet output or
an old timestamp proves neither life nor death. The next real run reconciles
previous Running reports to Unknown before observing new work. Read-only status
and doctor do not change remote reports. Activity errors halt without retry;
retain receipts and follow the bundled docs/ACTIVITY.md recovery procedure.


## Explicit continuous watching (0.1.11)

This section retains manual bounded sessions. The separate unpublished
0.1.13-dev.3 [interactive-session mode](../../../docs/SESSION-RUNTIME.md) defaults
on for new onboarding; existing projects use explicit `autostart --enable
--session-lifetime`. Explain worker usage across
finite windows, and separately authorized merging. Do not start a second manual
supervisor while a session runtime owns the common lock. Use `session_status`
for current connection observation/work state and `autostart-status`/`watch-status`
for recorded authority and budgets. Stop and inspect failures before re-arming;
ordinary turn completion leaves observation alive. Native packaged acceptance
must precede activation.

Read [watch scope, budgets and recovery](../../../docs/WATCH.md). An explicit
watch request may authorize the entire configured repository/Project's matching
current and future Backlog/Ready queue for one finite session. This is broader
than a static run allowlist. Make exclusions and the hold label visible in the
preview; preserve manual Priority/holds. It grants no merge authority.

```sh
python3 "<plugin>/scripts/sfc.py" --root "<repo>" watch --dry-run
python3 "<plugin>/scripts/sfc.py" --root "<repo>" watch --start
python3 "<plugin>/scripts/sfc.py" --root "<repo>" watch-status
python3 "<plugin>/scripts/sfc.py" --root "<repo>" stop
python3 "<plugin>/scripts/sfc.py" --root "<repo>" watch --resume <recorded-session-id>
```

Use the same policy flags for preview/start. Resume preserves session settings,
deadline/counters and claims; repeat --allow-merge only if it was part of that
session's separate authorized merge opt-in. A spent session cannot renew itself;
only an explicitly authorized --start creates another. Stop/signal retains all
worktrees and receipts. Never detach and claim ongoing operation without
observing the process. The host/process must stay running; no service, scheduler,
web server, login startup or implicit installation launch exists. Empty or
blocked polls do not dispatch paid workers. Human handoffs remain waiting while
independent tickets can proceed. Read-only status and preview never repair setup.

# Optional release cards

Read [the release protocol](../../../docs/RELEASES.md) when release configuration
is enabled. Normal run/watch scope, Priority admission, two default worker slots,
stops and finite budgets still apply. Release cards use a supervisor-validated
workflow kind and no implementation PR. Merge permission never grants tag,
draft, upload or publication permission. Version-file triggers create Backlog
cards only under their separately authorized scope; they renew no run budgets.
