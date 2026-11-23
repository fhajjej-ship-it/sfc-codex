# Track Codex work on a GitHub Project

Chat tracking starts with the work the user asks Codex to do. The Project
records what happened, including checks, reviews, blockers, and completion.
Use a single issue per unit of work and reuse it on follow-up turns.

## Set up a Project

Existing SFC configurations use chat tracking when the updated plugin is
loaded. Keep the existing Project, issue history and receipts. Stop any
session still running from an older installation before upgrading, then reopen
Codex and review the updated hooks in `/hooks`.

For a new project, open the repository in Codex and specify a GitHub Project
owner, number and base branch. Use `onboard --preview` to verify the identity,
then `onboard` to save the board configuration. If GitHub requires you to save the
view grouping manually, open the returned URL, set **Column by → SFC Status**,
then ask Codex to check the view with `doctor`.

The approved prompt hook guides the current Codex chat. Its selected model
implements and reviews the work. It can run the repository's usual check
commands and open an authorized pull request. The board follows the work as
it happens. Merge and release use the repository's normal process and require
the user's authority.

## Record actual work

Use the chat prefix supplied by the hook to find work already recorded.
Use a stable task ID for each independent change. If the user names an issue
on the configured Project, pass `--issue NUMBER` when starting. Otherwise the
first sync creates a typed issue with observable acceptance criteria.
Choose the delivery mode at the start. Use `--delivery pr-required` for
changes intended for the shared repository and `--delivery local-only` for
work delivered locally. SFC retains that choice across turns. Routine questions
and status checks need no ticket. Keep transcripts, credentials, private paths
and raw logs local.

```sh
python3 "<plugin>/scripts/sfc.py" --root "<repo>" track start \
  --task chat-example-export --type Feature --title "Export selected rows" \
  --delivery pr-required \
  --summary "Add CSV export for the current selection." \
  --acceptance "Only the selected rows appear in the CSV."
python3 "<plugin>/scripts/sfc.py" --root "<repo>" track sync --task chat-example-export
```

After focused checks pass, open an authorized draft PR during QA so GitHub CI
can run during verification. Attach its number to the QA milestone. Record
Review when the change is reviewed. A genuine blocker can be recorded as
Blocked and later followed by Building or QA. PR-required work reaches Done
after verification of the linked merge. For local-only work, record the
verification and use `--local-done`. Existing tasks saved before delivery
choices were introduced retain their original completion rules.

```sh
python3 "<plugin>/scripts/sfc.py" --root "<repo>" track update \
  --task chat-example-export --event draft --stage QA --pr 42 \
  --summary "Focused CSV export tests passed. A draft PR is open for CI."
python3 "<plugin>/scripts/sfc.py" --root "<repo>" track sync --task chat-example-export
```

Each sync refreshes the active linked PR and reports the observed CI state.
After a manual merge, the next sync verifies the PR's repository and base,
then writes and reads back Done in SFC Status and the standard Status field
when present. A PR closed without merging leaves the card at its current
milestone. Run sync at a chat checkpoint to reflect changes made on GitHub.

`start` and `update` save milestones locally before any GitHub call.
Sync Building before editing and sync later milestones as they happen.
Continue working during GitHub outages.
`track status --task ID` shows pending milestones, and `track sync --task ID`
retries them. Codex can show the linked issue and latest recorded milestone
in chat alongside the last verified board stage. Lost write responses require
readback before reconciliation.
The tracker changes Project fields and appends milestone notes. It grants
no merge or publication authority.
