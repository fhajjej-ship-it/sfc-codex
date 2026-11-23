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
   acceptance criteria. Decide where the result needs to land. Choose
   `--delivery pr-required` for changes intended for the shared repo or
   `--delivery local-only` for verified work that stays local. Link the
   named issue with `--issue`. Sync Building before editing and show the
   verified issue link and saved delivery choice in chat.
2. Work in the current chat. As checks, review, a PR or a genuine blocker
   occurs, record actual evidence with `track update` and sync. Keep
   transcripts, credentials, logs and private paths local. During a GitHub
   outage, keep working and retry pending updates at the next checkpoint.
3. When authorized to open a PR, open it as a draft during QA after the
   focused checks pass. Record the exact returned URL with `track update
   --pr-url` and sync the QA milestone so the issue links to the draft.
   GitHub CI can run while verification continues. Move to Review as the change is
   reviewed and follow the repository's normal checks and merge permissions.
   Sync the task after CI or a merge. SFC reports the latest observed PR and
   CI state and records Done when GitHub confirms the merge. For a verified
   local-only delivery, use `--local-done`. A PR-required task needs a linked
   verified merge to reach Done. Explain pending updates in chat.

Routine questions and status checks need no ticket. See
[chat tracking](../../docs/CHAT-TRACKING.md) for commands and examples.

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
