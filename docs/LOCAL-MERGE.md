# Local verification auto-merge (0.1.9)

New onboarding defaults to auto policy; explicit `--merge-policy human` leaves
merging to the user. Existing configurations are never rewritten. Auto policy
alone grants no run/watch merge authority.
The existing `github-protected` mode still requires strict GitHub up-to-date
branch protection and uses the expected-head squash merge API.

For a trusted project without GitHub CI or paid private branch protection,
explicitly select all three settings while the factory is stopped:

```json
{
  "merge_policy": "auto",
  "merge_mode": "local-verification",
  "allow_no_checks": true
}
```

These are a fragment of the existing project configuration, not a complete
configuration. Keep the project's nonempty `verify_commands` and finite limits.
Omit `allow_no_checks` or leave it false when GitHub checks are expected.
Any present checks still need acceptable completed results, even with that flag.
Local mode fully enumerates latest check runs and combined status contexts,
pins them to the candidate SHA and rejects incomplete/changing inventories.
It preserves the existing success/neutral/skipped check-run policy.

New onboarding exposes `--merge-policy auto --merge-mode local-verification`
and, only when deliberately accepting absent checks, `--allow-no-checks`.
Setup output names the chosen mode and fast-forward method. No setup choice authorizes a merge by
itself: a future user-authorized `run --allow-merge` is also required. Issue text,
labels, worker results and this document cannot grant that authority.

## Exact candidate and tradeoff

This mode **fast-forwards the base to the exact independently reviewed head**.
It preserves all branch commits, including base integration commits. It does not
squash, rebase, manufacture a new merge commit, rewrite history or use a force
update. A team requiring squash history should use human or protected merging.
The head must already contain the freshly fetched base. A moved/stale base
returns to QA for supervisor integration and fresh project checks, QA, security
and final review. It never lands a newly combined tree using old lane evidence.

The common-git-directory supervisor lock and merge mutex serialize SFC merges.
The gate rechecks repository/PR/ref/issue identity, live task/config pins, clean
source worktree, complete remote and local diff inventories, size, human-only and sensitive
path/label policy. The local diff includes deleted paths and both sides of
renames; binary changes need human review. A separate detached worktree checks
the exact candidate commit again with the actual project commands. Both worktrees
must remain clean, with no unfinished integration, and the candidate tree is pinned.
Existing missing/failed/stale lane evidence and human gates remain blocking.

## Server transaction

GitHub documents [atomic `updateRefs` and expected `beforeOid` values](https://docs.github.com/en/graphql/reference/git#updaterefs).
SFC checks server schema support, then makes one transaction containing:

1. Head ref: `beforeOid = reviewed head`, `afterOid = reviewed head`, `force = false`.
2. Base ref: `beforeOid = tested base`, `afterOid = reviewed head`, `force = false`.

The first entry is a comparison with no intended ref change. Its expected value
is part of the same transaction as the base update. A head or base race rejects
the transaction. A read followed by a head-only merge would not provide this
guarantee. Unsupported schema, unavailable rules inventory, permission errors or
rejection halt; there is no alternate merge API, push, admin bypass or retry.
Local mode conservatively refuses refs with branch protection or active
repository/organization rules, including when the actor could bypass them.
Use the protected mode or a human on those branches. No rules are edited.

This protocol relies on the documented server implementation, including checks
on comparison-only entries. Temporary Git tests exercise an equivalent atomic
compare/update transaction and adapter tests assert the exact GraphQL payload.
They do not prove live GitHub behavior or account permissions. No live branch
write is needed to run the tests, and this feature's implementation does not
perform one. A server that rejects the transaction is never worked around.

## Journal, completion and recovery

Before the ref mutation, `.sfc/state.json` stores `cards.<issue>.local_merge`:
transaction ID, repository/PR/ref node IDs and names, reviewed head, tested base,
candidate tree, task/config fingerprints, source card identity, complete lane
and project-check receipts, scratch path and phase. Intent precedes dispatch.
The owned issue branch, candidate worktree and logs remain available, including
after success. The journal is copied into the run summary as usual.

GitHub can [recognize an indirect merge](https://docs.github.com/en/pull-requests/reference/pull-request-merges#indirect-merges)
when commits reach the base outside the PR merge API. SFC requires actual readback:
the intended repository/base contains the exact candidate commit/tree, the
original PR reports MERGED with that commit, the same issue reports CLOSED, and
the same Project card is still in Review (or Done following a lost stage response).
Only then can it mark Done. Configured nondefault bases can leave an issue open;
that remains incomplete rather than manufacturing a closed outcome.

Lost responses, exhausted recognition waits, missing/deleted refs, identity changes,
partial outcomes or an uncertain Done write retain the journal and halt. A later
run reconciles **only by reads** before planning workers. It can complete a
confirmed landed change but never resends the ref mutation. If a Done response
was lost, an observed Done is accepted; an observed Review with a prior Done
intent requires human recovery instead of repeating the board write. A missing
card also halts. Changed config/task pins require human reconciliation.

After inspection, any manual recovery must have its own explicit scope. Do not
clear a journal, delete a worktree or requeue a card to force another attempt.
An external merge without a local merge journal uses the separate verified
completion flow below; it cannot use the legacy head-only Done heuristic.
Read-only `status`, `doctor` and `run --dry-run` do not reconcile or migrate.
With scoped `--include-backlog` runs, pending journals outside the explicit
issue allowlist halt before recovery writes or worker dispatch. Dry-run reports
the same blocker without widening scope. Authorize the original journal's
recovery scope separately; no claim, attempt or journal is reset.
No daemon, plan upgrade, workflow installation or global plugin installation is
introduced.

## Completing a human merge

After SFC hands a reviewed PR to a human (including a Blocked size/sensitive-policy
handoff), the user may merge that exact PR themselves. The next authorized
supervisor reconciliation can mark its original card Done and report Completed.
This works in ordinary Ready runs, explicitly scoped Backlog runs and finite
watchers, under either merge mode and with human merge policy. Observing the
already performed merge does not require `--allow-merge` and grants no authority
to merge. It performs no remote ref update, merge request or issue-close write. Size
and sensitive rules continue to govern automatic merging without a cap increase.

Before handoff SFC saves `reviewed_completion`: original issue/item/repository
identity, PR number/URL (and node ID when returned), intended refs, reviewed
head/base, task/config fingerprints, routing scope and independent lane/project
receipts. Existing watcher/Backlog handoffs can instead use their verified claim's
original identity/scope plus the saved handoff head and pinned review receipts.
Legacy ordinary records without saved issue identity cannot be upgraded from
the current card alone: inspect and reconcile those explicitly.

Completion requires fresh configured Project membership and routing meanings,
the saved claim where applicable, unchanged task/config/head/base and complete
passing local evidence. Watcher holds/exclusions, creation integrity, human stage
edits and activity ownership still apply. A human-set Done without a completion
intent is left untouched. Other Project fields and PR prose are not rewritten.
The actual PR must match the saved repository, identity, source and intended base,
report MERGED on the reviewed head, and identify the matching CLOSED issue in a
complete closing-issue inventory. GitHub checks on the reviewed head must still
satisfy the configured missing/passing-check policy.

Supported Git shapes are deliberately narrow. The reviewed base must be an
ancestor of the reviewed head, and the reported merge commit must be reachable
from the freshly fetched intended live base. Accept either the exact reviewed
head, a squash commit whose sole parent is the reviewed base, or a two-parent
merge whose ordered parents are the reviewed base and head. All must have exactly
the reviewed head's tree. Later base advancement is allowed when the recognized
merge remains reachable; a deleted head branch is allowed when the PR retains
the reviewed head identity. Changed-base integrations, conflict-resolution trees,
changed heads, multi-commit rebases, ambiguous/truncated identities and absent
receipts remain gaps. A single rewritten commit is accepted only if it has the
same parent/tree proof as the supported squash shape. No old review evidence
certifies an uncertain integration.

`.sfc/state.json` retains a separate `external_completion` receipt with snapshots,
merge/tree/check evidence, and durable Done and Completed intents. An unresolved
factory-owned `local_merge` journal always takes precedence and keeps its existing
stronger authority/recovery requirements; this path never creates or clears one.
Lost Done responses are resolved by fresh readback: observed Done may continue;
the original stage with a saved Done intent halts without another stage write.
Activity retains its existing partial/unknown-write latch. Restart after a verified
Completed receipt only reads it back; it does not publish Completed twice.
Terminal completion follows both verified readbacks. No external completion gains
the atomic protocol's post-cutoff watcher allowance or extra work budget.

Unrecognized PR/issue closure, missing cards, scope/hold drift, changed evidence or
uncertain writes preserve receipts/worktrees and halt with an actionable reason
in the existing run/status error and local card history. When ownership still
verifies and no completion/activity write is uncertain, the existing activity
fields say that external merge evidence needs inspection; no separate activity
system or arbitrary worker text is published. Restore or explicitly reconcile
the original scope/evidence after inspection; never erase an intent to force a
retry. Once completion verifies, the normal later planner can admit dependents
whose prerequisites are actually closed. Read-only status and dry-run never
perform completion writes. Tests use temporary repositories and fake adapters;
they do not establish live GitHub timing or board behavior.


## Bounded recognition window

Ordinary Ready-only runs, scoped Backlog runs and watchers may observe an acknowledged atomic
update whose exact candidate/tree is already on the intended live base, but whose
PR remains OPEN or whose matching issue closure lags. At most five additional
one-second read-only observations spend durable `confirmation_reads` before each
wait; restart never renews the counter. Stops/config checks remain cooperative
during waits, and all IO remains finite. No workers or project checks rerun while
waiting. Complete identity/rules, live task/membership/stage, routing meanings and
applicable run scope/merge authority are revalidated between observations and
before completion. Ready-only routing requires no Priority setup. Only actual MERGED recognition, matching merge commit,
CLOSED issue, fetched base reachability and verified Done readback complete it.
An empty, old-base, partial, ambiguous-write or identity-changing snapshot never
grants another mutation or success. Exhaustion retains an unresolved journal.
Done and Completed writes retain their independent uncertain-write latches;
the journal becomes terminal only after verified Completed activity. The active
run also retains the verified Done stage when its original cycle cap is reached.

Before any watcher journal completion write, the issue must still belong to the
verified configured Project/repository and pass the session's hold/exclusion
policy, with separately authorized configured auto-merge. Scope drift halts
without widening authority. Lost ref responses never repeat the atomic request;
ambiguous Done intents never repeat a completion write. See [watch recovery](WATCH.md).
The watcher's original cutoff and pinned completion-only allowance are unchanged;
ordinary runs gain no watcher session, daemon, new deadline or extra worker wave.
