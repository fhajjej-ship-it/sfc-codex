---
name: sfc-collect
description: Collect and verify software problems, propose evidence-based priorities for new tickets, or prioritize the configured repository's existing Backlog with a reasoned order.
---

# SFC Collect

For collection and backlog review, read [the prioritization workflow](references/prioritization.md).
Use its rubric and readout for every finding and ticket. An explicit request to
prioritize the backlog follows that workflow's inventory and writeback steps;
it does not run the factory. A request only for advice produces a local readout.

Use the requested source and time range. If unspecified, inspect the selected
repository and recent relevant changes. Use connected error/analytics/issue
tools only when relevant and available; do not invent access or treat missing
metrics as zero. This mode investigates; it does not automatically edit code.

For each candidate, reproduce or establish the code path, practical impact and
limits. Separate verified defects, source-supported risks and unverified leads.
Check existing GitHub issues/PRs before proposing another ticket; preserve links
to duplicates and new evidence. Telemetry showing no event can reflect missing
instrumentation or a planned feature, not a broken customer flow.

Write a concise findings document under the project's normal docs location or
`.sfc/findings/`. Include source/time, reproduction, expected/actual behavior,
file/line evidence, confidence, impact and a proposed observable acceptance
criterion. Include a proposed delivery priority, a short evidence-based reason,
severity, urgency, dependencies/overlaps and effort (explicitly unknown when
unsupported). Prioritize failures that affect the user's core workflow using
the shared rubric; severity alone does not determine delivery order.

When asked to create a ticket or turn findings into issues, automatically assess
priority using the shared evidence rubric and one of Bug, Feature, Refactor,
Documentation or Maintenance using [the type policy](../../docs/WORK-TYPES.md).
Preserve explicit user choices. No separate classification or prioritization
request is needed. Clarify genuine ambiguity; do not call a requested improvement
a defect without evidence. Maintenance includes dependencies, configuration, CI
and routine upkeep; Technical Debt is optional context. Keep `qa` a separate
explicit workflow choice. Create only selected nonduplicates
through the bundled supervisor `create-ticket` command described in the reference.
Use its exact ticket-body layout: observable bullets under `## Acceptance criteria`,
only same-repository issue references under `## Blocked by`, and explanatory prose
under a separate `## Dependency notes` heading. An empty dependency section is valid.
It requires `--type`, the assessed priority level, reason, explicit unknowns and
a stable request ID. It verifies live type-label mapping and saved Labels/Priority
visibility before posting, sends exactly one type label in the initial POST,
enrolls the issue in the configured Project, sets Backlog and Priority, and
verifies issue/body/repository/item/fields and actual labels. Body-only type or
priority is incomplete. `--label` preserves explicitly requested existing auxiliary
labels; use `--label qa` only for an explicitly requested QA-only assessment.
If evidence cannot support even a qualified provisional level, retain a local
finding and explain the missing decision instead of inventing impact or urgency.
Missing/ambiguous setup blocks creation before an issue is posted. An authorized
setup/repair uses `type-setup --apply` and/or `priority-setup --apply` (or
`dashboard --apply` for full setup); read-only commands never migrate.
Research alone does not grant permission to post comments, contact customers,
promote Ready or start workers. Report partial failures with the actual receipt
and identities; inspect before recovery and never generate a new request ID to
blindly retry an uncertain creation.
