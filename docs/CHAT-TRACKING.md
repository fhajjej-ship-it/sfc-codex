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
retains that exact repository/base scope as `chat_merge_authority`.
The grant also retains the reviewed host, repository node ID and provider;
renaming or replacing a repository cannot transfer it. New setup selects the
chat atomic provider and explicitly reports its availability. Protected/ruled
refs and custom review policies require their existing maintainer process;
saving automatic policy or a grant does not claim that provider is available.
Offer `--merge-policy human` for an explicit merge gate. Existing project settings
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

When the PR is intended to deliver and close the tracked issue, record
`--closes-issue`. Sync then attaches the verified PR through GitHub's native
`addCloseIssueReferences` API. The issue's Development section and the board's
Linked pull requests field show GitHub's standard PR badge. A milestone link
alone does not authorize that closing relationship. This works across
repositories and preserves existing attachments. A lost response is
confirmed by readback before another operation; missing confirmation or an
external unlink remains pending. Keep Linked pull requests visible in the
saved board view. Milestone comments retain their PR link as supporting context.
Attachment warnings are reported separately from milestone sync. A missing
badge preserves its write intent and cannot block an independently verified
QA, Review or merged Done update.

Codex records a partial PR with `--milestone-only`. Its merge records progress
without completing the task. After verified terminal readback and pending-write
reconciliation, the task can link its actual delivery PR; the earlier receipt is
retained; current or archived milestone-only PRs cannot be promoted to delivery. Omitted flags and existing records retain their delivery-PR meaning.
`--closes-issue` additionally authorizes GitHub's native closing relationship.

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

`track merge --task ID --worktree PATH --head SHA --base SHA --prepare`
owns actual configured checks and one independent native Codex review. It
requires parsed native completion, exact scope and complete acceptance coverage.
Preparation owns these checks/review; implementation uses focused checks and
does not need a duplicate preliminary full suite or independent review.
Each preparation is reserved durably before execution, bounded by `max_attempts`.
The same pins with `--apply` require Review, validate original hashes, execute
fresh final checks, enforce issue/size/sensitive policy and compare both refs
atomically. A final-check reservation is spent even on interruption. Unknown
merge outcomes keep the intent and permit only readback, never a second write.
Final verification runs in a new detached checkout. Applicable CI follows
[GitHub's test-merge precedence](https://docs.github.com/en/pull-requests/how-tos/merge-and-close-pull-requests/troubleshooting-required-status-checks#conflicts-between-head-commit-and-test-merge-commit),
with complete inventories and the live PR/issue/refs checked last. The final
query reads PR eligibility, both CI rollups, complete-inventory check
identities, issue policy, routing meanings and open PR heads together. Complete
paginated commit-check inventories are retained because PR rollups can omit
checks. The final query refreshes those exact identities as well as the rollups.
It retains check identities and outcomes and
refuses newly reachable heads of other open PRs. Shallow or replaced histories
require a handoff. An
incomplete combined check/label/field/open-PR inventory requires a handoff rather than
using stale pages. A known
pre-dispatch failure retains its history and may use the remaining preparation
allowance; it does not receive a refund. Native CLI capability is checked before
reservation. Completion requires the matching closed issue, including after a
later base advance or deletion of the merged topic branch. A definitely unsent
attempt can also follow an externally verified human merge through ordinary
tracking; its original proof and spending stay intact. Unknown writes still
require owned-candidate readback. A verified landing with an open issue reports
an issue-closure handoff and remains short of Done. Merge authority always comes
from the primary checkout. Gates inspect every newly reachable commit for
sensitive paths and credentials using raw paths; unscanned historical binaries
require a handoff. Commit-message closures outside the tracked issue also
require a handoff. Child invocations observe CLI stop signals; supported macOS
lifelines also clean them up after abrupt supervisor death. Gates revalidate execution receipts, write
capability and live issue policy before dispatch.
Existing configurations and custom review requirements are not migrated.
This provider requires same-repository PRs whose sole closing issue is the tracked task, reviewed identity-bound
consent and unprotected/unruled default-branch refs; unavailable capabilities are
explicit in onboarding and doctor, with a maintainer handoff. It changes no
repository protections and starts no builder or queue watcher.

```sh
python3 "<plugin>/scripts/sfc.py" --root "<repo>" track update \
  --task chat-example-export --event draft --stage QA \
  --pr-url https://github.com/owner/code-repo/pull/42 \
  --closes-issue \
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
reconciles them. Done-field intents require readback after an unknown response,
including when the old value is still visible; sync never blindly resends them.
Codex can show the linked issue and latest recorded milestone
in chat alongside the last verified board stage. Lost write responses require
readback before reconciliation.
The tracker changes Project fields and appends milestone notes. It grants
no merge or publication authority.
