# Worker results and bounded resume

The issue #58 source patch is based on SFC 0.1.12 (unreleased). SFC 0.1.11 and
an unpatched 0.1.12 do **not** have this contract or accounting. A version string
alone is insufficient: the supervisor must pin the final reviewed commit that
includes this patch and verify the packaged `schemas/lane-result.json`,
`scripts/sfc_lib/factory.py` and four lane skills against that commit. This ticket
does not publish a release or install/update a plugin.

## Output contract and schema support

In0.1.14-dev.6, all newly generated verdicts require a nonblank summary and one
concrete, truthful `evidence` text string. Use paragraphs within that text for
files, observations and checks, rather than optional array slots. The actual
Workshop retry on November9 repeated a trailing empty array item despite the
previous schema/prompts; its raw rejected report and spending remain intact.
Earlier array-form reports are still accepted only when nonempty and every item
is nonblank text. An array with even one blank item remains invalid. The
supervisor does not trim, convert, repair or accept a historical bad report.
`pass` requires no unresolved findings. A blocked verdict must describe the
observed limitation in its evidence. The supervisor rejects malformed JSON,
missing/empty evidence, nonstring or blank items, and pass with findings.

The new schema uses string `minLength: 1` and `pattern: "\\S"`; all lane prompts
and skills request that text format. Raw result shape and receipt contents stay
identical; there is no hidden evidence normalization. Runtime `str.strip()` validation remains
authoritative, including Unicode whitespace; neither a regex nor a nonblank
claim establishes that evidence is true. Independent assessment is still required.
Pass-with-findings remains a runtime cross-field check, without unsupported
`if`/`then`/`else` composition.

Support was checked on November 8, 2026 against the installed CLI's
`codex exec --help` (`--output-schema` accepts a JSON Schema file), the official
[Codex structured-output interface](https://learn.chatgpt.com/docs/non-interactive-mode#create-structured-outputs-with-a-schema)
and the official [Structured Outputs supported subset](https://developers.openai.com/api/docs/guides/structured-outputs#supported-schemas).
That historical array-contract check listed `pattern` and `minItems` as supported, with additional restrictions
for fine-tuned models. No paid model call was used to test backend acceptance.
An unsupported configured model/provider must fail closed; there is no weakened
schema fallback or claim of backend verification for arbitrary custom providers.

## Failure accounting

Worker prompts include the process time limit after any adapter cap and ask the
worker to reserve time for its final report. The worker should finish once it
has assessed the required scope and report confirmed findings and any remaining
evidence gaps in its existing format. When available, the private
`worker.log.prompt.md` records the full UTF-8 prompt prepared for launch. The
process log and receipts establish whether the worker launched and produced an
accepted result.

Before launching a worker, the supervisor saves a per-card/per-lane
`worker_output_budget` in `.sfc/state.json` and a `dispatch.json` in the unique
lane folder. The record pins the input head/base/task/config and worktree, plus
raw result/log paths. An accepted result gets the normal `receipt.json`. A
malformed result instead gets `failure.json` and halts the entire run; the raw
`result.json`, `worker.log`, prompt and modified worktree remain unchanged.
No malformed result can reach build publication, merge or Done as a fallback.
An earlier lane receipt is removed from the active state before dispatch, while
its original file remains available for inspection.

Each lane allows at most `max_attempts` unsuccessful dispatches for that card.
The initial limit is pinned and a lower current setting is also enforced.
Malformed results, worker exits, missing outputs, process interruption and
uncertain launches consume a reservation. A crash between intent and acceptance
is conservatively spent even if a result or receipt exists on disk. Accepted
outputs do not consume the malformed-output allowance; prior spending is never
refunded. Valid fail/blocked verdicts still use the existing findings/attempts
path. No attempt, cycle, no-progress, claim or session counters are reset.
Changing task/config/head/base or restarting does not renew this allowance.

On first use of the journal, retained historical folders matching
`.sfc/runs/*/issue-N/LANE-*` without an acceptance receipt are conservatively
counted as `legacy-unaccepted`, including interrupted/missing outputs. The
supervisor records only their paths; it never rewrites them or makes acceptance
receipts. A dispatch journal without its matching state budget stops for manual
reconciliation. Missing historical folders are an evidence gap: do not resume
a legacy claim until its original history has been located and preserved.

There is no automatic retry, output-only regeneration, result editing, or
replay of an interrupted result. A separately authorized supervisor resume may
run the **full lane** again in the same retained worktree while allowance remains;
it may inspect and extend existing implementation work. It receives no new
merge authority. At exhaustion, stop and inspect the retained paths/worktree.
There is no supported counter-reset or automatic exhaustion override. The
explicit supplemental recovery below requires separately granted spending
authority. Do not delete state/worktrees, increase settings to renew the pinned
limit, recreate the claim or start another watcher as a workaround.

## Explicit one-use Build recovery (0.1.13-dev.4)

After inspecting the original outputs/logs and retained worktree, an interactive
operator can authorize **one additional complete Build dispatch** for the same
verified claim. This is separate spending authority, never inferred from a ticket,
normal watching, reinstallation or a restart. It is not output-only regeneration
or acceptance of a malformed result. The original limit and dispatches stay intact.

```sh
python3 "<plugin>/scripts/sfc.py" --root "<repo>" stop
python3 "<plugin>/scripts/sfc.py" --root "<repo>" recover-worker-output \
  --issue 4 --request-id "owner-approved-issue-4-recovery" \
  --reason "Owner authorized exactly one additional complete Build dispatch"
# Only after explicit authority for this supplemental spending:
python3 "<plugin>/scripts/sfc.py" --root "<repo>" recover-worker-output \
  --issue 4 --request-id "owner-approved-issue-4-recovery" \
  --reason "Owner authorized exactly one additional complete Build dispatch" --grant-one
```

The default command previews without state writes. Granting holds the common
supervisor lock, requires a stopped/halted run and the same live open Building
issue/item/verified claim, and rejects an existing PR. All original results/logs
must remain. Worktree, branch, head/base, task/config, exact dirty diff/status,
history and raw evidence hashes are pinned. An interrupted dispatch that never
produced a result pins that exact path as absent; it does not create or accept
a replacement historical result. A later file appearing there invalidates the
grant. Logs remain mandatory, and missing accepted/malformed outputs still halt
as lost evidence. Untracked implementation files need inspection before a grant.
An intent journal precedes the local state write and
readback; ambiguity remains latched. The same request ID is idempotent. A second
request cannot grant another dispatch for that claim, even after failure.

Granting launches nothing, clears no stop, creates no watcher budget and grants
no merge permission. Explicitly re-arm or resume only already authorized scope
under the normal runtime contract. Immediately before worker launch the supervisor
rechecks all pins, spends the grant alongside its durable dispatch reservation,
and then runs the full Build lane with the current strict result schema. A crash,
interruption, bad output or success all spend the supplemental grant. No fourth
dispatch is permitted by this output-recovery grant after the two original failures plus this one. Valid Build
output must still pass real project checks, independent QA/security/review and
all normal publication/merge gates. Original evidence is never edited or refunded.

## Retained Workshop Prep Agent claim

This patch does not rerun or establish recovery of the external trial. Its two
reported malformed builder runs used installed 0.1.11 and had no accepted PR.
Their empty findings-attempt counter is not a fresh retry grant.

Before any separately authorized continuation, the owner must:

1. Pin the reviewed patched commit/package described above. Keep the old package
   version, both original raw outputs/logs, private trial owner receipts,
   state, claim, branch and implementation worktree. Confirm both historical lane
   folders are retained under the original target root. Do not edit their evidence.
2. Read back the exact existing issue/claim/worktree/head/base/task/config and
   remote branch/PR state. Reuse any existing PR; never create a replacement issue
   or claim. If identities or history cannot be established, stop for reconciliation.
3. Check the original trial authorization and original finite limits. The two
   unaccepted legacy builder folders consume two slots at journal adoption. For
   example, an unchanged `max_attempts` of 2 leaves **zero** builder launches;
   3 leaves at most **one**, subject to the original session/time/cycle budget
   and a separately approved continuation. Do not raise a limit to create room.
4. If scope, evidence and budget permit, the supervisor may resume only that same
   single issue using its original mode/allowlist. A watcher must resume its named
   original session with the original deadline/counters and preview issue #15
   exclusion. Expired/exhausted sessions stop; do not renew or start a watcher for
   this ticket. Auto-start failure/stop latches still require their own explicit
   re-arming contract; this patch does not re-arm them.
5. A new valid builder result must still pass normal project checks, independent
   QA/security/review, exact pins, GitHub checks and existing authorization gates.
   Report each observed stage. Call the live trial recovered only after a real
   valid result and the subsequent required gates have actually been observed.

Without separately authorized supplemental recovery above, the owner must stop
when no original allowance remains. Local fake-adapter fixtures demonstrate accounting and normal gates;
they cannot grant external-trial spending, verify its current state or substitute
for the supervisor's full project verification and independent lanes.

## Explicit retained-ticket recovery (0.1.14-dev.6)

For a retained interrupted Build timeout, supervisor base conflict, pinned failed
project check, malformed Build, or accepted blocked Build whose prerequisite has
been resolved, use
`recover-ticket --issue N --request-id ID --reason TEXT` after stopping and
inspecting the supervisor. Preview first, then apply `--grant-one` only for the
user's explicit recovery request. This separately authorizes one complete Build
for watcher or named scoped pickup; it does not reset original attempts/output
reservations, renew a watcher window, grant merging or change the size limit.
Every new recovery requires explicit owner authority for one full Build. A spent
older supplement never authorizes a further dispatch: an additional malformed
result needs its own newly authorized, failure-specific grant. Historical bad
outputs are never repaired, stripped, regenerated or accepted. Failed checks need
the actual candidate/task/config-pinned supervisor receipt and matching raw check
logs; a worker assertion cannot substitute. Accepted blocked outputs must match
their original receipt. Stable failure identity prevents a renamed request or
changed local edit from refunding a grant spent before dispatch.
The base conflict must still reproduce. Candidate, live base, claim, task/config,
dirty/untracked bytes, original logs/results/receipts and legitimate absent
interrupted output are pinned. Changed evidence prevents dispatch. Admission
intent/readback latches uncertainty. The grant is spent before base preparation,
even if preparation or launch is interrupted. Failed recovery blocks the card;
another poll cannot retry it. Successful recovery still needs fresh project
checks, QA, security, review and the existing merge gates. Superseded failure
journals are archived, not deleted. Existing independent-finding recovery below
retains its narrower named-run contract.

In 0.1.14-dev.7, retained-ticket recovery also recognizes an owned Build rejected
before model work because Codex explicitly reports that the selected model is
unsupported with the account. The last reserved dispatch must match its original
dispatch JSON, with no result, PR, agent/tool work or completed turn in its log.
Only the recognized pre-turn model-metadata and tier diagnostics are allowed
alongside the terminal failure events; other item events prevent this recovery.
Both terminal CLI error events must agree. Select an explicit replacement Build
model before preview; the command neither substitutes models nor establishes
account availability. The dispatch, original log, absent result and replacement
configuration are pinned. All earlier launches remain spent. Unknown worker
exits and workers that began work are not this recovery kind, and another poll
cannot retry them. The same explicit one-Build authority and every normal
check/review/merge gate still apply.

## Explicit corrective Build after independent findings (0.1.13-dev.6)

`recover-findings` separately authorizes one complete corrective Build for an
existing open PR rejected by QA, security or final review. It can follow spent
findings/output limits, without resetting either. Only new explicit operator
authority permits this; watching, a restart or a different request ID does not.

Stop first, then preview `recover-findings --issue N --request-id ID --reason TEXT`.
Apply that same command with `--grant-one` only for an explicitly authorized
additional Build. Resume with `run --include-backlog --issue N`; generic watching
and Ready-only runs cannot spend corrective grants. Granting starts no worker
or board write and supplies no merge exception or watcher/session budget.

The grant pins the verified claim, current PR identity/head/base, task/config,
attempts, failed independent result/receipt, all retained lane outputs/logs and
legitimate missing outputs, worktree diff and old spending. One finding snapshot
cannot receive a second corrective grant under another ID. Existing journals
and counters remain. Blocked-to-Building admission journals intent and exact
readback; ambiguous admission stays latched without repeating the write.
Immediately before the full Build, all pins are rechecked and the grant is spent
with its durable launch reservation. Interruption or malformed output never
refunds it. Another finding follows normal accounting and cannot silently get
another Build. Other lane caps, project checks, independent QA/security/review
and all normal merge gates remain unchanged.

If the base has advanced since the failed review, recovery retains that review's
original base and separately pins the current descendant base. Base movement
after granting rejects admission or dispatch. The corrective Build does not
reuse the failed review: ordinary QA integrates the updated base and runs fresh
project checks, QA, security and review. A rewritten base history still requires
manual reconciliation; integration conflicts grant no additional Build.
