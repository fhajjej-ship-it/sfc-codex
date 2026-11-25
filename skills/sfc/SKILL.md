---
name: sfc
description: Track work started in Codex chat on GitHub Projects. Use for recording features, bugs and maintenance, linking PRs, reflecting checks or review outcomes, and configuring Projects.
---

# SFC

SFC records work done in the current Codex chat. Use that chat's model and the
repository's usual tools for implementation, tests and review. Work starts with
the user's request. A board label or column never authorizes work.

The plugin root is two directories above this SKILL.md. Resolve it from this
file's installed location. Commands use
`python3 "<plugin-root>/scripts/sfc.py" --root "<target-repo>"`. Keep installed
plugin code and skills read-only.

## Track changes

1. For an actionable feature, fix, maintenance, refactor or documentation
   request, use `track status --prefix PREFIX` to find an existing task. For
   new work, use `track start` with a stable task ID, work type and observable
   acceptance criteria. Decide where the result needs to land from the user's
   requested outcome, without asking them to manage the delivery mode. Choose
   `--delivery pr-required` for changes intended for the shared repo or
   `--delivery local-only` when the user explicitly requests local delivery.
   Repository documentation and workflow guidance intended for users require
   a PR. New tasks default to `pr-required` when the option is omitted. Link the
   named issue with `--issue`. Sync Building before editing and show the
   verified issue link and saved delivery choice in chat.
2. Work in the current chat. As checks, review, a PR or a genuine blocker
   occurs, record actual evidence with `track update` and sync. Keep
   transcripts, credentials, logs and private paths local. During a GitHub
   outage, keep working and retry pending updates at the next checkpoint.
3. Carry authorized shared-repository delivery through a PR. Open it as a draft during QA after the
   focused checks pass. Record the exact returned URL with `track update
   --pr-url` and sync the QA milestone so the issue links to the draft.
   GitHub CI can run while verification continues. Move to Review as the change is
   reviewed and follow the repository's normal checks and merge permissions.
   With configured `auto` policy and existing authority for the repository,
   base and task, complete the eligible merge without another per-PR question.
   Apply the delivery checks below. Sync the task after CI or a merge. SFC reports the latest observed PR and
   CI state and records Done when GitHub confirms the merge. For a verified
   local-only delivery, use `--local-done`. A PR-required task needs a linked
   verified merge to reach Done. Explain pending updates in chat.

Routine questions and status checks need no ticket. See
[chat tracking](../../docs/CHAT-TRACKING.md) for commands and examples.

## Complete delivery

Read the repository instructions and saved merge policy before publishing.
Verify that existing user authority covers the selected repository, base and
task. A shared delivery request covers opening its PR when the repository
instructions permit it. Keep explicit local choices and human merge gates.
Explain a missing authorization once with the concrete PR ready for review.

Finish the required checks and independent review on the exact candidate.
Compare the current remote head and base with that evidence, and refresh the
affected verification after either changes. Check the configured change-size
limit, sensitive-path policy and repository protections. A draft, failed or
missing required check, unresolved finding or uncertain write remains pending.
Use the repository's configured merge method. For GitHub-protected merging,
the normal GitHub CLI path can enable `gh pr merge --auto --match-head-commit
<verified-sha>` with the repository's permitted merge strategy. Where atomic
local verification is configured, use its checked supervisor and required
receipts as described in [local merging](../../docs/LOCAL-MERGE.md).
After a lost response, inspect the existing merge request before any new write.

Keep the task in Review while auto-merge is queued and report its actual wait.
Read back the merged PR, then sync the task so Done means verified delivery.

## Ticket chats

New bugs, maintenance tasks and features get their own Codex chat. Check for an
existing ticket chat and board card first, and reuse both for follow-ups.
Keep routine questions and initial discussion in the current chat. Start the
work in the ticket's chat once the user asks to proceed.

## Project administration

| Request | Action |
|---|---|
| Set up a project | Use `onboard --preview` with the Project owner, number and base. Apply with `onboard` and verify the saved view with `doctor`. |
| Dashboard repair | Inspect with `dashboard`; use `dashboard --apply` when authorized. |
| Triage an issue | Use `sfc-collect` for evidence-based type and priority. Use the supervisor for GitHub writes. |
| Prior reports | Read saved results with `delivery-report`, `watch-status` or `release-status`. |
| Review or test a change | Use the repository's instructions and checks in the current chat. |
| Publish a release | Follow the repository's release process with user authority. |

Verify repository and board identity on writes and read back the result.
Preserve existing receipts and worktrees. A failed board write does not decide
whether the work can proceed. Report verified milestones and pending syncs.
