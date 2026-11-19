# Isolated collection / backlog prioritization fixtures

These are acceptance fixtures for the interactive `sfc-collect` workflow, not
claims of a live GitHub or model-backed run. Replay with a fake GitHub adapter
and a temporary target repository only. No scenario authorizes service access.
The reference under test is
[prioritization.md](../../skills/sfc-collect/references/prioritization.md).

## Executed offline replay

[Recorded decisions and literal API calls](backlog-prioritization-replay.json)
now supply 17 executable cases. The build worker authored these decisions using
the reference above and the scenario evidence below. The fake transport in
[`backlog_replay.py`](../backlog_replay.py) applies those literal calls in a
temporary git target with fake config; it contains no prioritization rubric,
scope selector or decision engine. It cannot access GitHub or start workers.

Run `python3 tests/backlog_replay.py` to regenerate
[the before/after and tool-call receipt](backlog-prioritization-receipt.json).
The receipt contains every input/output, preserved prior ownership receipt,
readout, and completion/blocked result. Its hashes pin the workflow and recorded
decisions. [`test_backlog_prioritization.py`](../test_backlog_prioritization.py),
included in the normal suite, independently checks semantic recommendations,
allowed write sets, narrow mutations, unchanged board data and readbacks. It
also checks that the saved receipt exactly matches a fresh offline replay.

| Scenario | Executed cases |
|---|---|
| A | Local findings without writes; selected checkout ticket creation with priority/body and board readback |
| B | Both inventory pages, five annotations, three priority writes, six excluded items and retained manual first choice |
| C | Dependency elevation/order, overlap, missing/cyclic/external waits; conflicting manual order |
| D | Missing evidence; no field; ambiguous fields; P0 without a compatible mapping |
| E | Manual Normal/unassigned/High retained; unchanged owned value/comment updated; sourced effort tie-break |
| F | Advice only; empty backlog; second-page access failure blocks all writes |
| G | Identical repeat; changed scope/priority/comment ownership; manual clear; accepted-write timeout inspected without retry; failed readback retained as blocked |

These are **scripted decision replays**, not fresh model invocations, live GitHub
tests or proof that every future model follows the instructions. The remaining
stress variants below (101 items, nested field/comment pagination, malformed
cursors, missing config/origin mismatch, project-add failure and unexpected
readback values) remain manual acceptance cases, not executed coverage claims.
The replay's JSON annotations retain all assessment dimensions for inspection;
the interactive user's concise table remains the presentation contract.

## Shared setup and replay contract

In a temporary git repository, set origin to `https://github.com/example/app`.
Seed `.sfc/config.json` with `repo: example/app`,
`project: {owner: example, number: 7}`, `status_field: Pipeline`; other runtime
settings use the test suite's `BASE_CONFIG`. All IDs below are fake adapter IDs.
The fake tools return the following complete project metadata unless overridden:

| Object | ID | Values |
|---|---|---|
| Project | project-test | example / 7 |
| Pipeline field | stage-test | backlog-test: Backlog; ready-test: Ready; building-test: Building; qa-test: QA; review-test: Review; blocked-test: Blocked; done-test: Done; parked-test: Parked |
| Priority field | priority-test | high-test: High; normal-test: Normal; low-test: Low |
| Team field | team-test | blue-test: Blue |
| Custom view | view-test | Board, grouped by Pipeline, filter `label:customer` |

Project documentation defines High = P1, Normal = P2, Low = P3. P0 and Needs
evidence have no matching option. Each issue below is OPEN, in example/app, stage
Backlog and Team Blue unless stated otherwise. Unset priority is null. IDs are
`item-N`/`issue-N`, URLs are `https://github.com/example/app/issues/N`.

For each scenario:

1. Provide only the listed request, facts, metadata, paginated responses and
   prior receipt to the workflow. No extra reproduction/customer data exists.
2. Capture reads, proposed findings/readout and any mutation payloads. Refuse
   writes outside the allowed set; no factory command or worker is available.
3. Compare output semantically with the expectations below. Equivalent plain
   language is acceptable; missing uncertainty or an unauthorized write fails.
4. Apply allowed writes to the fake board and return them on readback. Compare
   a deep before/after snapshot: only specified priority values and owned ticket
   comments (or explicitly requested new issues/items) may differ. Status,
   Team, custom options, IDs, views, labels and unrelated items remain identical.

## A. New findings; ticket creation is separate

Request: "Collect bugs in the checkout; prepare findings."

Evidence: a temporary reproduction log shows checkout fails for a valid cart
on two attempts. No frequency, production telemetry, deadline or effort estimate
is available. A second observed defect is clipped help text with a readable
tooltip. Existing-issue/PR search returns no overlap.

Expected local findings/readout:

| Finding | Proposed priority | Reason / limits | Next action |
|---|---|---|---|
| checkout | P1 High | Major; reproduced core-flow failure. High reproduction confidence; customer reach, urgency and effort unknown. | Scope a fix with observable checkout acceptance criteria. |
| help text | P3 Low | Cosmetic; readable workaround directly verified. High confidence; usage/effort unknown. | Fix when convenient. |

Allowed remote writes: **none**. Follow-up request: "Create only the checkout
ticket." Allowed: create one issue whose body contains P1, its evidence-based
reason, confidence/unknowns and acceptance criteria; add that issue to project-test
in Backlog, fill unset priority-test with high-test and read back the body/fields.
No help-text ticket, Ready move or worker start. If ticket creation succeeds but
project addition fails, retain its real returned URL, report the partial result
and inspect before retry; do not create a second ticket.

## B. Mixed backlog; severity is not delivery priority

Request: "Prioritize the backlog."

| Issue | Evidence | Existing priority |
|---|---|---|
| #10 Checkout fails | Reproduced; support export documents daily reports; no workaround. | unset |
| #11 Historical export fails | Reproduced major failure for a rare archive format, per maintainer; verified safe conversion workaround. | unset |
| #12 Button spacing | Reproduced cosmetic defect with no effect on interaction. | unset |
| #13 Maybe loses data | Anonymous report without steps, logs or a reachable path. | unset |
| #14 Docs refresh | User explicitly selected High and first place in this review. Limited impact verified. | High, manual |

Expected readout: retain #14 first and High (manual), with a separate P3
alternative and explanation. Recommend #10 next, P1/High for recurring core-flow
impact; #11 P2/Normal despite major severity because of its bounded reach and
safe workaround; #12 P3/Low. #13 is Needs evidence, low confidence, **urgent
investigation first**, with delivery order undetermined; never claim verified
data loss or simply rank it Low. Effort and urgency for #10–12 remain unknown
where not supplied. The recommended delivery sequence is #14, #10, #11, #12;
the urgent investigation of #13 is called out separately.

Allowed writes: priority-test on item-10/11/12 only, using existing IDs; one
marked assessment comment on each #10–14. #13 remains unset; #14 remains High.
Every comment includes order/action, reason/source, severity, confidence,
unknowns and effort. The final readout links the comments actually read back.

Additional project items on a second page: #15 CLOSED/Backlog, #16 OPEN/Ready,
an OPEN other/app issue, a draft, a PR and #17 with no Pipeline value. All are
excluded from the write set, even if #17 looks urgent. Report the missing stage
as unknown and excluded. Include both pages in the before/after snapshot.

## C. Dependencies, overlap, cycles and external waits

Request: "Prioritize the backlog."

| Issue | Evidence / links |
|---|---|
| #20 Payment repair | P1 core-flow failure, `## Blocked by` #21. |
| #21 Parser repair | Bounded defect; reproduction proves it is necessary for #20. |
| #22 Same payment failure | Same reproduction/cause as #20; no distinct acceptance need. |
| #23 Missing dependency | `## Blocked by` #99; adapter returns not found. |
| #24 Cycle A | `## Blocked by` #25. |
| #25 Cycle B | `## Blocked by` #24. |
| #26 Wait for migration | `## Blocked by` #27; #27 is OPEN/Ready. |
| #28 Closed prerequisite | P2 with workaround; `## Blocked by` #29, CLOSED. |

Expected: #21 elevated to High because it unlocks #20, with #21 before #20 in
the suggested delivery order. #22 stays in the readout linked as overlap with
#20; suggest consolidation, don't claim an additional independent core failure.
#23 says order blocked / verify #99, #24–25 say cycle / clarify dependency,
and #26 says waiting on #27 without touching it. #28 is not blocked by #29.
No dependency/body edits, automatic closure, enrollment of #27/#29 or stage
changes. Comments may link related issues but never silently rewrite Blocked by.
Repeat with a user instruction to list #20 first: retain that manual choice but
mark delivery blocked by #21, showing #21 → #20 as the feasible alternative;
do not claim the requested order can be executed.

## D. Missing evidence and no compatible priority field

Request: "Prioritize the backlog."

Override metadata: there is no Priority field or documented priority-label
convention. One Backlog issue #30 says only "slow sometimes". No logs,
reproduction or metrics; its effort and impact are unknown.

Expected: Needs evidence; low confidence; next action obtain steps and a timing
sample, not an invented affected-customer count or one-hour fix. Ticket comment
records that priority and the evidence-gathering order. No field/option/label
creation, no Low default and no need to change the board schema. The ticket
annotation satisfies visible writeback; readout says no compatible board field.
Repeat with two undocumented Priority-like fields: same safe fallback, with the
ambiguity reported. Repeat with verified ongoing destructive behavior: propose
P0 Immediate in the comment without guessing a High mapping for P0.

## E. Manual choices, known effort and provenance

Request: "Prioritize the backlog, keep #40 Normal and #41 unassigned."

#40 has a reproduced P1 core defect and a manual Normal field. #41 has a
reproduced P2 workaround defect and a null field. #42 has manual High from an
existing maintainer comment. #43 has High and an unchanged SFC annotation plus
a matching prior receipt proving SFC set it; new evidence supports Normal.
#44 has High with no ownership receipt. #45/#46 have comparable P2 impact and
confidence, no manual order; a sourced maintainer estimate says #45 is small,
#46 large. No other estimates exist.

Expected: preserve Normal on #40 and null on #41; show the recommendations
separately, explicitly identifying the user choice. Preserve #42 and #44 High.
Only #43 may change a populated priority, from high-test to normal-test; update
its unchanged owned comment. Fill unset #45/#46 with Normal. #45 precedes #46
as a known-effort tie-break, without invented hours;
effort for all other issues remains unknown. All still receive visible reasons
or preserved-comment conflict reports, with no automatic implementation.

## F. Advice, empty scope, incomplete inventory and unavailable tools

- Request "Suggest priorities only" with scenario B: same assessment, **zero**
  GitHub writes. Local results are explicitly recommendations, not board updates.
- Request "Prioritize backlog" with zero eligible items: report empty, zero writes.
- Seed 101 items across two pages, with the sole eligible issue on page two:
  read both and annotate only that issue. Also paginate its field values and
  maintainer comments, with an explicit manual choice on their second pages:
  honor that choice. A first-page-only assessment fails this fixture.
- Return `hasNextPage: true` with missing/repeated cursor, or a failed page for
  fields, items, field values or comments: writeback blocked, partial local
  inventory marked incomplete, zero writes. Never imply the entire backlog was read.
- Remove config, return origin mismatch, or deny required GitHub access:
  remote triage blocked with the actual reason, zero writes. No onboarding,
  alternate repository guess, secret read or fabricated runtime evidence.

## G. Preservation, repeat runs and partial writes

Run B against the fake adapter, then repeat with an unchanged assessment and
matching receipt. Expect no duplicate comments or redundant field writes; the
date alone does not cause an update. Preserve all unrelated data in the shared
snapshot, including custom parked-test, Team and view filters.

Before writeback, change #10 to Ready and alter #11's priority manually. Skip
both, report concurrent changes and preserve the new values. Before editing an
owned comment, change its content/author: preserve it and report conflict.
Remove its receipt but leave the marker: preserve uncertain ownership and report,
not overwrite/duplicate. Manually clear a formerly SFC-set priority: preserve the
clear rather than assuming the null value is available for reuse.

Make a comment write return a timeout after the fake board accepts it. Expect
no blind retry; inspect live state before deciding whether it exists. Fail a
readback or return an unexpected value: retain receipts, report only verified
writes as complete and mark remaining writeback blocked. No rollback touching
human data, lost worktree or statement that the whole board was updated.

These fixture outcomes concern interactive recommendations and annotations.
They do not change the factory planner's resume/issue-number ordering or test
worker dispatch, merge authority or model accuracy.
