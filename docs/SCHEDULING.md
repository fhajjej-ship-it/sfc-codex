# Rolling priority admission (0.1.14-dev.2 candidate)

At each available ticket slot, all eligible unassigned saved Building/QA/Review
claims compete with fresh work by verified Project Priority (P0, P1, P2, P3),
actual creation time normalized to UTC, then issue number. Saved claims keep
identity, stage, worktrees, attempts and evidence while waiting. They have no
automatic precedence. Running work finishes safely without preemption; one ticket
owns its slot across Build, QA, security, review, checks and merge verification.
`max_workers` is the hard ticket concurrency ceiling.

Optional release cards retain one attempt across Building, QA and Review. A
matching active release journal can continue at the per-card attempt limit;
this is admission of the existing attempt, without resetting counters or
repeating verified work. Normal scope, prerequisites, slot/cycle/lifetime limits
and pinned evidence gates still apply. Terminal or changed journals grant no
continuation; see [release recovery](RELEASES.md#publication-and-recovery).

The supervisor keeps one executor and common-git lock for the run/window.
Any completed future causes fresh inventory and selection while peers may still
execute. Queue observation also continues at `watch.poll_interval` during active
work, including finite runs, with bounded waits rather than a spin loop. New
dispatch rereads membership, complete routing/Priority inventories, task identity,
labels, holds and prerequisites. One supervisor thread reserves and claims one
ticket, verifies readback, then replans before selecting another. Workers cannot
claim cards. Incomplete/ambiguous inventories halt. Missing/unmapped per-card
Priority waits; missing/ambiguous setup halts every mode without fallback.

If a selected ticket fails live creation/task/scope/prerequisite revalidation,
a finite run defers that ticket for the rest of the invocation and immediately
replans the remaining queue, even with no worker active. It records an admission
deferral waiting reason; a later authorized run rechecks the ticket from scratch.
Each rejected ticket is considered at most once per finite invocation, preventing
repeated edits from causing a revalidation loop. This does not discard claims or
evidence. Watchers retain their bounded periodic retry behavior.

Ready-only runs still admit only fresh Ready work and saved owned claims.
Scoped Backlog runs still detail/admit only the exact issue allowlist, including
resumed work. Watchers retain their authorized current/future repository/Project
policy, exclusions and hold label. No Priority, hold or configuration repair is
implicit. Unchanged CI, human, dependency and excluded waits launch no paid work.
Passing CI still returns through normal evidence and merge gates.

Uncertain idle claim/activity journals halt before admission; unresolved merge
journals receive recovery precedence. An active ticket owns its in-flight journal
until its future exits; the scheduler never reconciles it concurrently. Recovery
and ordinary merges share the merge mutex. Exact head/base/task/config pinning,
independent lanes, configured checks, sensitive/size policy, missing checks and
separate merge authority remain unchanged. A base advanced by a peer requires
integration and fresh evidence through the existing path.

## Finite accounting

`max_cycles` counts individual ticket admissions, not fixed waves. Before a
claim/dispatch operation, a durable per-card `dispatch` intent and session cycle
spending are saved. Claim allowance is independently reserved before claim
intent/write; uncertain claims never refund spending or create replacement claims.
Worker dispatch reserves a conservative no-progress count before launch.
The running supervisor knows which reservations are ongoing work and excludes
those from completed-failure admission limits. Terminal Done or a verified human
handoff resets consecutive failures, preserving all outstanding reservations.
Accepted new lane evidence or a return to QA after base movement also counts as
progress when no findings attempt was spent. During watching, known findings and
terminal Blocked outcomes are ticket-local: their original attempts/output
allowances remain spent, but they neither add to nor refund the observer's
existing no-progress count. Owned worker/check timeouts and output exhaustion
park the ticket and release its slot after verified publication; later polls
never automatically retry it. Other no-progress completions retain the shared
limit. Finite non-watcher runs retain their prior failure accounting. Interruption
retains all outstanding conservative spending; resume does not treat an unknown
prior dispatch as known ongoing work or refund it.
Completed failure exhaustion takes precedence over a simultaneous poll/claim
limit, so it cannot masquerade as a normal lifetime-window rollover.

Polling and unused capacity spend no cycles or failure allowance. Watch polls
still spend the original poll allowance; completion-triggered replans also count
as polls. Admission limits let admitted work finish, while operator stop,
cancellation and the original deadline stop owned commands. Ambiguous writes,
unsafe journals, changed authority and unknown failures stop peers and retain
receipts. A ticket-failure publication intent must be reconciled before another
supervisor can admit work; an acknowledged terminal failure blocks only that
ticket. Pre-upgrade saved timeout/output-exhaustion activity also keeps its
ticket out of automatic watcher pickup until explicit recovery. The completion-only allowance remains tied to
the original deadline and acknowledged atomic outcome. No budget or deadline
is enlarged for refill; no per-card findings/output launch allowance is reset.
Lifetime observation, work-window admission and unsafe interruption recovery stay
governed by [SESSION-RUNTIME.md](SESSION-RUNTIME.md).
Deferral before the admission intent spends no cycle; a failed claim preflight
after intent retains its reserved cycle. Neither path refunds prior spending,
launches a worker, or prevents independent eligible work from using capacity.
Operator stops and finite work limits report Stopped only on actually dispatched,
unfinished tickets; completed cards and human handoffs keep their reports.

Preview includes `occupied`, `max_workers`, `next_eligible` (even while full),
ordered next choices and waiting reasons. Each run plan records occupied slots
and capacity/eligibility waits.
Watch status records occupancy, next choice and the waiting snapshot. These are
timestamped observations, not process liveness proof. A separate preview process
uses the recorded occupancy only while the common supervisor lock is held and
labels that source; an inactive stale snapshot does not suppress preview choices.
Conservative no-progress spending includes pending reservations; during known
live work this does not mean
those tickets failed. On restart unknown reservations remain spent.

Tests use temporary repositories, fake GitHub/Codex, injected clocks and events
to prove actual admission/refill. Local package validation is not independent
QA/security/review, installation or release.
See the [local candidate verification](VERIFICATION-0.1.14-dev.2.md).

## Observation cost

Lifetime queue observations still enumerate every Project membership and verify
current repository, issue identity/state and routing/Priority meanings. They
omit detailed bodies, labels and field values only when local state records
Done and the live issue is still closed. Running/occupied cards, pending claims,
uncertain activity or completion, unfinished dispatches, handoffs, recovery grants
and release cards keep their full reads. No receipt or spending is discarded.

The omission set is rebuilt for each observation under the state mutex. A reopened
issue receives full details on the next observation; reopening does not reset
attempts or authorize another Build. Explicit finite selections and ordinary
non-watcher diagnostics keep their full reads. Completed cards stay visible on
GitHub, but no longer appear as redundant waiting entries in lifetime plans.

There is no cross-poll cache of task, labels, Priority, configuration, authority,
reviews or checks. Existing dispatch revalidation and journal recovery remain
mandatory. See [the measured audit](PERFORMANCE-AUDIT.md) for scope and limits.
