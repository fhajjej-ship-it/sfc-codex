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

New setup defaults to automatic merge policy. Explain the repository, base,
required checks and change-size limit during setup. When the user authorizes
automatic delivery for their requested tasks, preview with `--allow-merge`
and apply that exact `--expected-review` digest; setup
retains that exact repository/base scope as `chat_merge_authority`. Offer
`--merge-policy human` for an explicit merge gate. Existing project settings
retain their saved policy and authority. An automatic policy preference alone
still needs the user's merge authority.

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
Codex chooses delivery from the requested outcome. New tasks default to
`pr-required` for changes intended for the shared repository, including
documentation and user-facing workflow guidance. Use `--delivery local-only`
when the user explicitly requests local delivery. Existing choices are retained
across turns, including previously recorded local work. Routine questions
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

Carry authorized shared-repository work through a PR. After focused checks pass, open a draft PR during QA so GitHub CI
can run during verification. Pass the exact URL returned by GitHub with
`--pr-url`. SFC verifies the PR and its configured target branch, then adds
the link to the issue's QA milestone. This works across repositories and
keeps the link on the issue side. A public issue can link only to a public PR.
Existing `--pr NUMBER` links still refer to the tracked issue's repository.

Sync attaches the verified PR to the issue through GitHub's native
`addCloseIssueReferences` API. The issue's Development section and the board's
Linked pull requests field then show GitHub's standard PR badge. This works
across repositories and preserves existing attachments. A lost response is
confirmed by readback before another operation; missing confirmation or an
external unlink remains pending. Keep Linked pull requests visible in the
saved board view. Milestone comments retain their PR link as supporting context.
Attachment warnings are reported separately from milestone sync. A missing
badge preserves its write intent and cannot block an independently verified
QA, Review or merged Done update.

Record Review when the change is reviewed. A genuine blocker can be recorded as
Blocked and later followed by Building or QA. PR-required work reaches Done
after verification of the linked merge. For local-only work, record the
verification and use `--local-done`. Existing tasks saved before delivery
choices were introduced retain their original completion rules.

With configured automatic merge policy and existing user authority for the
repository, base and task, Codex completes eligible merges after required checks
and independent review. It preserves the configured size limit, sensitive-path
rules, branch protections and exact candidate/base evidence. It can enable the
repository's supported merge provider only when it binds the reviewed head
and base through the write. Expected-head-only queueing cannot supply that
guarantee. Unsupported providers and legacy local-verification configurations
receive a concrete maintainer or human merge handoff; their settings are
preserved. A queued merge remains in Review until GitHub confirms it merged. Human merge
policies and explicit local delivery choices still apply. See the SFC skill's
delivery steps for the required verification and merge methods.

```sh
python3 "<plugin>/scripts/sfc.py" --root "<repo>" track update \
  --task chat-example-export --event draft --stage QA \
  --pr-url https://github.com/owner/code-repo/pull/42 \
  --summary "Focused CSV export tests passed. A draft PR is open for CI."
python3 "<plugin>/scripts/sfc.py" --root "<repo>" track sync --task chat-example-export
```

Each sync refreshes the recorded PR and reports the observed CI state.
After a manual merge, the next sync verifies the PR's repository and base,
then writes and reads back Done in SFC Status and the standard Status field
when present. A PR closed without merging leaves the card at its current
milestone. GitHub's native Development link is available from the issue
sidebar if you want GitHub itself to close the issue on merge. For a QA or
Review task without a recorded PR, SFC can
reconcile a cross-repository merge when GitHub confirms that the single
linked PR merged into the configured base and closed the tracked issue.
Run sync at a chat checkpoint to reflect changes made on GitHub.

`start` and `update` save milestones locally before any GitHub call.
Sync Building before editing and sync later milestones as they happen.
Continue working during GitHub outages.
`track status --task ID` shows pending milestones, and `track sync --task ID`
retries them. Codex can show the linked issue and latest recorded milestone
in chat alongside the last verified board stage. Lost write responses require
readback before reconciliation.
The tracker changes Project fields and appends milestone notes. It grants
no merge or publication authority.
