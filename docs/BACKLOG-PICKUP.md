# Explicit priority pickup (0.1.10)

Ordinary `run` remains Ready-only, using the shared validated Priority order.
There is no configuration migration, daemon or implicit Backlog promotion.
Version 0.1.11 adds a separately authorized [bounded foreground watcher](WATCH.md). Interactive prioritization/annotation remains a separate
workflow and never dispatches workers.

An authorized supervisor can explicitly include **selected** Backlog and Ready
issues in a finite run:

```sh
python3 "<plugin>/scripts/sfc.py" --root "<repo>" run --dry-run --include-backlog --issue 24 --issue 28
python3 "<plugin>/scripts/sfc.py" --root "<repo>" run --include-backlog --issue 24 --issue 28
```

The numbers above are placeholders: use only issue numbers covered by the user's
run authorization. Repeated `--issue` selects an allowlist in the configured
repository and Project; it is mandatory with `--include-backlog`. It applies to
resumed work as well as fresh work. Neither flag is persisted as a project default.
Do not add demonstration #15, unrelated Ready issues or Blocked work to a run
merely because they exist. Workers never choose tickets or operate GitHub.

Unassigned saved Building/QA/Review and fresh eligible issues rank together by
the Project's mapped P0, P1, P2, P3 level, then actual issue
`createdAt` oldest first (normalized to UTC), then issue number. Updated time,
board position, body, labels and title do not determine Priority. The supervisor
claims one fresh issue after durable admission reservation, then replans before
selecting another. At most `max_workers` whole tickets execute; a released slot
refills without waiting for busy peers.
A later higher-priority issue does not interrupt or restart active work.
An admission revalidation rejection defers only that ticket until a later run;
the finite invocation immediately replans independent eligible work within its
existing allowlist and limits, even when no worker is active.

Read-only preview uses the same scheduler and reports `options`, `selected`,
`order` (stage/Priority/creation timestamp), and reasons in `waiting`. Entries
marked `excluded: true` are already closed/Done, or held/excluded in watcher mode,
rather than pending work in this run. Finite scoped previews enumerate every
Project item's lightweight identity with full pagination, but read detailed
fields/issues/labels only for allowlisted issues. Unrelated detailed rows and
their waiting reasons are not part of these previews. Watcher inventory remains
broad so it can discover current/future work and hold labels; ordinary Ready-only
inventory also verifies Priority/routing completely. Missing allowlisted issues are reported. Capacity waits,
missing criteria, invalid creation timestamps, unknown stage/Priority, malformed
or self dependencies, open prerequisites, and unreadable prerequisites remain
visible without comments, activity writes or stage changes on skipped cards.
Unknown Project values never fall back to Backlog or an invented Priority level.

With warmed metadata and one authorized issue, the fake-adapter planning benchmark
on boards of 1/25/100 issues drops from 5/53/203 to 5/5/5 GraphQL queries. At
101/250 issues it uses 6/7 queries: lightweight membership pagination still grows
with the board. These are query counts, not measured live latency or end-to-end
delivery savings. Dependency reads remain separate and fresh.

Only actual open Issues in the configured repository with explicit Backlog or
Ready values are fresh candidates. Fields/options/items/values/labels are read
completely with pagination checks; ambiguous/incomplete inventories halt safely.
Duplicate stage/Priority values are treated as unknown. The existing Priority
resolver supports canonical meanings or documented custom option-ID mappings;
manual values and meanings are never repaired by dispatch. Setup is a separate,
explicit action. Scope checks include fresh field IDs and option names/descriptions.

For all modes, including resumed Building/QA/Review claims, `## Blocked by`
must be empty or contain one same-repository `#number` per line, optionally
prefixed by `-` or `*`. Other
text or cross-repository references are malformed. Prerequisites are read from
the verified configured repository, even when present on the board. Missing,
unreadable or unknown state fails closed. Cycles cannot dispatch because each
member has an open prerequisite. Resumed claims recheck prerequisites during
planning and immediately before dispatch. Reopened or unresolved prerequisites
produce waiting reasons without running a worker or altering the remote card;
saved claims, attempts and worktrees remain available for a later eligible run.
SFC does not close or annotate those issues.

Immediately before a fresh claim, the supervisor rechecks prerequisites, live
Project/routing/Priority meanings and the issue's identity, state, explicit stage,
Priority value, title/body, labels and creation timestamp. Observable drift
invalidates the selection or halts; it never executes the stale plan. The
supervisor saves claim intent before its single stage write, moves to Building
(or QA for an explicit `qa` label), then reads the actual item/task/fields and
prerequisites back before dispatch. Successful claims traverse all ordinary
Build/QA/Security/Review checks, isolated worktrees, finite retry/time limits,
activity reporting and sensitive/large-change policies. QA-only keeps its usual
QA/Security/Review path without an implementation PR.

GitHub Project fields have **no atomic compare-and-swap**. Preflight and readback
catch changes observable at those boundaries; an edit inside the narrow
read/write race cannot be prevented atomically. Reads across separate Project,
issue and prerequisite queries are not a transactional snapshot either.

Every real run records `run_options` and each plan's occupied slots, selected order and reasons
in `.sfc/state.json` and `.sfc/runs/<run>/summary.json`. Per-issue `claim` stores
intent, expected task/scope/target and verified observations. Lost responses,
failed writes, mismatched readback or process death after intent leave an
`intent`/`incomplete` claim, including read-only observations when available.
A matching remote Building value alone does not prove the claim succeeded.
Both ordinary and opt-in restarts halt on unresolved claims before other work,
without repeating the write or starting a worker. Preserve receipts/worktrees and inspect remote state
before explicitly reconciling local claim state while no supervisor runs.
There is no automatic repair command. A fully verified claim interrupted before
worktree creation can resume only with matching identity, task and target stage,
and currently satisfied prerequisites; attempts and prior evidence are retained.
Fresh cards with saved execution or
exhausted attempts require explicit recovery.

Priority grants no execution or merge authority. The user must authorize the
run scope. New onboarding defaults to auto policy, while existing saved
policies remain unchanged. This pickup mode does not enable auto merge,
change project settings, install a plugin, or authorize publication. Automatic
merges still require the separate configured policy and `--allow-merge`, with
all existing SHA/base/check/sensitive-change gates.

Local-verification merge remains a separate opt-in. Before a scoped run can
start, every pending local merge journal must be within its authorized issue
allowlist. An out-of-scope journal halts recovery before any completion write;
the read-only preview selects no workers and explains the required recovery
scope. It never silently adds the journal's issue to the run. Within scope,
restart reads back the existing merge outcome without repeating the ref write;
verified claims and attempt counts remain intact.
