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
Routine questions and status checks need no ticket. Keep transcripts,
credentials, private paths and raw logs local.

```sh
python3 "<plugin>/scripts/sfc.py" --root "<repo>" track start \
  --task chat-example-export --type Feature --title "Export selected rows" \
  --summary "Add CSV export for the current selection." \
  --acceptance "Only the selected rows appear in the CSV."
python3 "<plugin>/scripts/sfc.py" --root "<repo>" track sync --task chat-example-export
```

Record tests, pull requests and review milestones as they occur. A locally
delivered change can go from Building straight to Done. A genuine blocker
can be recorded as Blocked and later followed by Building or QA. Attach the
PR number when recording a pull request in Review. A task with a linked PR reaches Done once
the tracker confirms it merged into the configured base. For work delivered
locally, record the evidence and pass `--local-done`.

```sh
python3 "<plugin>/scripts/sfc.py" --root "<repo>" track update \
  --task chat-example-export --event tests --stage QA --summary "The CSV export tests passed."
python3 "<plugin>/scripts/sfc.py" --root "<repo>" track update \
  --task chat-example-export --event reviewed --stage Review --pr 42 \
  --summary "PR opened for review. Project checks passed."
python3 "<plugin>/scripts/sfc.py" --root "<repo>" track sync --task chat-example-export
```

`start` and `update` save milestones locally before any GitHub call.
Sync Building before editing and sync later milestones as they happen.
Continue working during GitHub outages.
`track status --task ID` shows pending milestones, and `track sync --task ID`
retries them. Codex can show the linked issue and latest recorded milestone
in chat alongside the last verified board stage. Lost write responses require
readback before reconciliation.
The tracker changes Project fields and appends milestone notes. It grants
no merge or publication authority.
