# Optional GitHub release workflow

This unpublished 0.1.15-dev.4 capability is off when `release` is omitted (or is
`{"enabled": false}`). It is a separate supervisor-validated workflow kind,
independent of Bug/Feature/Maintenance labels, QA-only routing and merge policy.
It uses the existing seven columns. Development cards stay Done after verified
merge. A versioned `Release 1.2.3` card packages several merged changes and reaches
Done/Completed only after verified publication and downloaded asset checksums.
This feature is not an added launch-acceptance prerequisite.

The first adapter supports GitHub Releases on **github.com**. Deployments, package
registries, migrations, hosted services and SFC self-update are outside its scope.
Installed plugin code remains read-only. Commands and permission belong to the
target project's stopped `.sfc/config.json`, never its issues or worker output.

## Configuration and authority

Add a `release` object to an existing project configuration only when requested:

```json
{
  "enabled": true,
  "adapter": "github-releases",
  "package_commands": ["python3 tools/package.py"],
  "preflight_commands": ["python3 tools/check_archive.py dist/app.zip"],
  "verify_commands": ["python3 tools/check_install_and_smoke.py dist/app.zip"],
  "artifacts": ["dist/app.zip"],
  "version_file": "VERSION",
  "trigger": "manual"
}
```

The target owns these commands and must include its actual signing, artifact,
install and smoke checks. Optional `release.preflight_commands` run against the
packaged candidate before project `verify_commands`. Put checks that depend only
on the package in preflight. `release.verify_commands` run after the required
project checks and can be empty when preflight covers all artifact checks. See
[release preflight](RELEASE-PREFLIGHT.md) for dependencies, evidence and migration.
Commands run with finite `verify_timeout`, owned process groups, stop handling
and the existing host lifeline. They must preserve tracked source and artifact
bytes. Artifacts are exact relative allowlisted regular files, without symlinks,
globs, traversal, duplicate basenames or the reserved `sfc-release-manifest.json`.
The generated manifest is an additional supervisor-owned asset.

Choose the version explicitly with `release-request --version 1.2.3`, or use the
plain-text `version_file` in the pinned source. Supported versions are numeric
`MAJOR.MINOR.PATCH`, optionally prefixed `v` and with a prerelease suffix. SFC does
not infer breaking-change policy or increment a version. Projects with other
version formats must supply a supported explicit version for this adapter.

Use `{version}` in artifact paths when your packager includes the version in
its filenames. For SFC's own ZIP and checksum, set
`"artifacts": ["dist/sfc-{version}.zip", "dist/sfc-{version}.zip.sha256"]`.
The supervisor expands these paths using the frozen release version. A tag
such as `v1.2.3-rc.1` produces filenames with `1.2.3-rc.1`, matching the
packager's version format. Literal paths also work. The expanded paths must
pass the same allowlist rules and match the files your commands produce.

Publication additionally requires this **separately authorized** scope in `release`:

```json
{
  "authority": {
    "authorization_id": "user-approved-release-policy",
    "repo": "my-org/my-repo",
    "base_branch": "main",
    "host": "github.com",
    "destination": "github-releases",
    "mode": "publish"
  }
}
```

Repository and base must exactly match the parent configuration. Record this
only after the user authorizes this destination and scope. `mode: draft` allows
tag/draft/asset writes but leaves the card waiting in Review, explicitly
unpublished. Its acceptance criteria call for a verified draft, pinned tag
and matching downloaded checksums. Draft creation is an external write too. With `mode: publish`,
valid candidates publish automatically after all checks; do not repeatedly ask
for permission already granted within this scope. Missing authority stops at
publication without writing a tag, draft or asset. Labels, issue prose, Ready,
ordinary watch scope and `--allow-merge` confer no release authority.

For batching, the default trigger is `manual`. Explicitly setting
`trigger: version-file` plus `version_file` and the scoped authority enables a
trigger during an already authorized finite factory run/watch. Changing the
version file to a new supported version creates one release card. File-based
selection is rechecked against the final frozen candidate; if the base
changes its version during request preparation, the request stops before writes.
Only an explicitly chosen version overrides the file. Unchanged
versions and duplicate triggers do not create cards or rerun paid work. A
missing or invalid configured version file, an already-published matching
version, a missing approved summary, or a previous draft awaiting publication
leaves the automatic trigger pending in the saved run state under
`status` → `run.release_trigger`. Ordinary queue pickup can continue.
If a read-only trigger preflight exhausts its connection retries before saving
a creation intent, the trigger stays pending and ordinary pickup can use its
own fresh queue read during the same poll, provided no unfinished release
journal needs recovery. An unresolved creation stops admission even if the
version file is missing or a later version already has a journal. There is
no default release after each merge, new watcher, renewed budget or background
service. Triggered cards enter Backlog through normal ticket creation; they use
ordinary authorized Backlog/Ready scope, Priority, exclusions, prerequisites,
claims and worker capacity. A Ready-only run does not automatically promote them.

## Request and board metadata

Use the bundled supervisor CLI, resolved from the actual installed skill:

```sh
python3 "<plugin-root>/scripts/sfc.py" --root "<target-repo>" release-request --version 1.2.3
python3 "<plugin-root>/scripts/sfc.py" --root "<target-repo>" release-status
```

`release-request` holds the common-git supervisor lock and uses the existing
serialized ticket creation protocol for identity, Backlog, Priority and work
type. It creates no implementation branch/PR and does not publish. Normal
run/watch subsequently admits the card. `release-status` is read-only and reports
local preserved journals, not a claim of current remote state.

A repeated version request returns the recorded card. When the local journal is
missing, the supervisor checks repository issues, including closed cards, before
creating one. An existing version issue requires its owning journal. Use one
supervisor to create releases for a repository. GitHub issue creation has no
atomic uniqueness constraint across independent clones.

The request adds a missing shared Projects **TEXT** field named `Target release`
and appends its ID to saved Factory visibility. It preserves existing IDs,
options, seven columns, grouping, filters, extra visible fields and custom views.
It never silently repairs an incompatible field or observable manual edit.
The release card and every linked ticket card are assigned automatically. An
included ticket missing from this Project is an actionable gap, not a silently
omitted ticket. Included-change links and every commit are inspectable in the
release card body. Target membership means **planned**, not verified publication.

The supervisor reads saved visibility and queries Project items using
`target-release:"1.2.3"`, requiring all assigned items in the result and an empty
negative-control query. Extra manual assignments are disclosed, never cleared.
Users can use the same filter in the existing board; SFC does not overwrite a
custom view's filter or claim a browser view was saved. Unsupported API/schema,
filtering, access or readback blocks the operation with its journal preserved.
No new dashboard or column is introduced. Live API/UI verification on a target
still belongs to its authorized setup/run; fixture results are not visual proof.

## Frozen source and evidence

The cutoff is the exact merged commit fetched from the configured base. The
previous boundary is the most recent published source by ancestry, resolved
from actual release tags, including annotated tags. Initial releases include
the entire candidate history. Divergent/unavailable boundaries block rather
than guessing a hand-selected range. All commits in `boundary..candidate`, their
actual merged PR associations and same-repository closing-ticket links are
recorded; commits without those links are explicitly listed. This includes
extra merged changes regardless of labels. Non-closing prose references are not
asserted to be verified ticket membership.

The version, candidate, source boundary, complete membership, configuration and
release-card identity freeze in a repository-common journal. Only one unfinished
release is admitted. Later merges stay out of it; a base rewrite or changed
previous publication invalidates it. Packaging uses a detached writable candidate
area in the common git directory. No worker reimplements merged changes.

Building executes packaging, optional preflight, required project checks and
any remaining artifact checks. QA and security inspect pinned receipts, raw logs and copied
artifact identities read-only; Review checks version, notes, membership and
publication readiness. Each lane is an independent bounded Codex invocation.
The manifest binds source/config/card identities, artifact sizes/SHA-256 and
hashes of verification receipts. A final manifest also hashes independent QA,
security and review receipts and the reviewed subject manifest. Raw commands,
logs, absolute paths and arbitrary worker prose never enter public notes or
manifests. Source review is retained but does not replace package verification.

## Publication and recovery

Every field, tag, draft, asset, publish, close and Done mutation records intent
before writing and verifies readback. Repository/Project identity, current push
permission, routing meanings, configuration and stop are checked immediately
before writes. Readbacks inspect the exact affected resource. Release asset
verification checks one inventory around each set of downloads and checks the
bytes of each asset. Prior release tags are resolved in batches. Publication
rechecks task/stage, source and artifact evidence.
Tag creation pins the exact candidate; it never force-moves a tag. Draft creation,
each binary upload and publication are separate requests. Existing conflicting
versions/tags/assets halt. No endpoint deletes, replaces or clobbers an asset.
All assets are downloaded and hashed before publication and again afterward.
Done requires the published version, expected tag/source and exact asset bytes.

A successful draft response records its ID. SFC reads that exact draft by ID
even when the repository release list has yet to include it.
A lost response is reconciled by readback. If the intended outcome can be proved,
the receipt advances without another mutation. If absent, ambiguous, edited or
mismatched, it halts without blindly retrying. Interrupted packaging/check
reservations and worker-output budgets stay spent. Failures preserve the
candidate, logs, receipts and partial remote outcomes. Project checks also reserve
their execution before launch: an interrupted run without a successful pinned
receipt stops for inspection; a saved successful receipt reconciles without
running the commands again. Missing signing tools,
permissions, artifacts or checks are gaps to fix, never a successful release.
Draft-only preparation waits without repeated paid work. Changing configuration
(including draft-to-publish authority) invalidates evidence and requires explicit
reconciliation; this version does not silently upgrade an old frozen authorization.

Building spends one card attempt. A journal-matched active Building, QA or Review
card may resume that same attempt through normal scoped scheduler admission even
when `attempts == max_attempts`. The counter is not reset or incremented on
continuation. Saved passing checks and lane receipts are reused only after the
existing pin/readback checks; interrupted reservations without valid results
still halt rather than purchasing more work. Completed, blocked, manually moved,
task/configuration-changed or foreign-checkout journals do not grant this
continuation. Concurrency, cycle/no-progress limits, stops and watch lifetime
remain unchanged.

The attempt debit and a reservation pinned to its exact source, membership,
version, card, task, configuration and owning checkout are saved atomically in
factory state before the release journal advances to Building. If interrupted
between those writes, recovery advances only the matching planned journal on the
same open Building card, without changing the counter or issuing remote writes.
Both records retain the reservation for later counter/scope checks. An ambiguous
legacy planned journal with a spent counter and no reservation cannot be adopted;
counter drift, mismatched reservations and manual stage edits require explicit
recovery. No packaging or paid lane runs during reservation reconciliation.

Before admission, recovery reads back retained Building-to-QA and QA-to-Review
transition intents, including verified writes whose local stage journal was not
yet advanced. Only the owned transition's exact target can advance local state;
recovery never repeats the board write or resets attempts. Missing intents,
changed scope/identity, manual stage moves and intents without a matching remote
outcome cannot grant continuation. Valid checks and lane receipts remain reused.

### Correcting a candidate on its original card

After a confirmed check failure or independent finding, merge the correction to
the configured base. With the supervisor idle, preview the exact newer base tip
from the owning checkout, then use the same arguments with `--apply` to authorize
the corrected attempt.

```sh
python3 "<plugin-root>/scripts/sfc.py" --root "<target-repo>" release-recover --version 1.2.3 --candidate "<merged-sha>"
python3 "<plugin-root>/scripts/sfc.py" --root "<target-repo>" release-recover --version 1.2.3 --candidate "<merged-sha>" --apply
```

The command keeps the open issue and Project item, snapshots the previous
attempt, and reserves the next attempt within the saved limit. The project and
task, configuration, previous published boundary, and any file-selected
version must still match. The issue body retains the original plan.
`release-status` shows the current candidate and prior attempt reference.
A GitHub tag, release or publication intent requires
the existing publication recovery. If interrupted, repeat the same correction
command to read back its saved stage transition. An uncertain write stays
latched for inspection.

Normal run/watch admission resumes the corrected card in Building. The corrected
candidate has its own worktree and package evidence. A prior passing project
receipt can supply checks under the configured reuse contracts when inputs,
logs, and freshness still match. Packaging, configured artifact verification,
and applicable independent package reviews use the corrected candidate.
Publication and download verification use the final reviewed bytes.

If release setup stopped before the first attempt and before any release
effects, the same command can update the planned candidate on its original
Backlog or Ready card. The supervisor keeps the previous plan and checks the
same source, task, and publication boundaries. Normal admission reserves the
first attempt on the corrected candidate.

Journals are under `<common-git-dir>/sfc-releases/`; ordinary factory state and
lane receipts remain in `.sfc/`. Resume from the original owning checkout to
preserve all counters. Incomplete creation stops ordinary dispatch even when
the trigger changes or releases are turned off. In the owning checkout, inspect
`release-status` and the ticket creation receipt and use the original configuration
with `release-request --version <exact-version> --recover-creation`. This checks
the completed ticket receipt and live card and binds them locally without
repeating the issue POST or making any other GitHub writes. It can pass the
scheduler stop recorded for that exact journal and leaves that stop in place.
An operator stop or an unattributed stop still requires separate inspection and
acknowledgment. A missing or incomplete ticket receipt requires manual readback
of the uncertain creation; no automatic retry is allowed. Resume work separately
with explicit authority. Completion recovery
reads published bytes again and handles a lost Done/Completed response without
creating duplicate transitions. Preserve journals, budgets and frozen candidate
evidence. Use the explicit correction command for eligible pre-tag source changes.

GitHub field writes lack compare-and-swap. Preflight/readback preserve observable
human edits but cannot eliminate a change in the narrow race between them.
External changes after final readback remain possible; these receipts attest to
observed outcomes, not perpetual immutability.

## References and verification boundary

API contracts rechecked against [gh release create](https://cli.github.com/manual/gh_release_create),
[GitHub Releases REST](https://docs.github.com/en/rest/releases/releases),
[release assets](https://docs.github.com/en/rest/releases/assets),
[Projects GraphQL](https://docs.github.com/en/graphql/reference/projects) and
[Project filtering](https://docs.github.com/en/issues/planning-and-tracking-with-projects/customizing-views-in-your-project/filtering-projects).
The runtime checks the target's actual capabilities/readback; documentation is
not a claim that a real customer board or publication was operated.

`tests/test_releases.py` uses temporary real git repositories and fake GitHub and
Codex adapters. It packages real fixture bytes and exercises both normal scheduler
routing and interrupted publication/completion. No test publishes a real release,
reads production credentials, operates a customer board or installs globally.
The normal supervisor owns full project verification before independent lanes
and final integration. This implementation ticket does not authorize a real SFC
release, activating this development board's publication permission or installation.

## Readable public notes and description edits

New releases freeze public notes separately from the card's complete audit body.
The deterministic generator groups eligible public merged PR titles as Highlights,
Fixes and Improvements, combines duplicate descriptions while retaining their PR
links, and displays at most twelve descriptions plus a pinned full-changelog link.
The overview shares space across present groups so highlights cannot hide all fixes.
It reports unlinked commits and unavailable titles without inventing their purpose.
Commit messages, PR bodies, worker prose and private verification evidence are
never inputs. Private or unavailable repository visibility suppresses title prose;
unsafe/multiline title markup is omitted, including from the public manifest.
Titles describe submitted work; independent lanes must still verify their accuracy
and supported impact against the pinned implementation. No performance, security,
compatibility or breaking-change conclusion is inferred from a version or label.

Optional `release.public_docs` maps `Installation`, `Update`, `Prerequisites`,
`Breaking changes` or `Known limitations` to exact project-owned public Markdown
files. For example: `{"Installation": "docs/public-install.md"}`. The operator must
identify documents intended for public distribution, with only supported guidance
and public installation commands. Complete bounded documents are quoted verbatim
from regular Git blobs at the frozen cutoff and linked there; do not select an
internal runbook, raw log or verification recipe. No SFC installation instructions
are supplied to other projects. Without explicit documentation, guidance is omitted.

Text, SHA-256, complete membership digest, title evidence and documentation blob
identities freeze in the journal and reviewed manifest before paid lanes start.
QA/security/review assess accuracy, readability and sensitive-data boundaries as
part of their existing bounded invocations. Resume reuses this text and evidence;
there is no generation worker or extra launch allowance. Legacy manifests and lane
subjects without public notes retain their original inventory text; they are never
silently regenerated. Unfinished legacy presentations without meaningful notes or
a reviewed Latest plan require explicit reconciliation, retaining original evidence.
Target release membership and board columns are unchanged.

### Approved summaries for private repositories

Private access and release-publication authority do not authorize private prose.
The supervisor never uses private titles, PR bodies, commit messages or worker
prose as public descriptions. Before requesting a release, the operator can store
explicit approval of exact plain-text summaries and their supporting commit hashes
in the target's `release.approved_summaries` configuration:

```json
{
  "approved_summaries": {
    "authorization_id": "owner-approved-summary-text",
    "repo": "my-org/my-repo",
    "base_branch": "main",
    "host": "github.com",
    "destination": "github-releases",
    "entries": [
      {"commit": "0123456789012345678901234567890123456789", "text": "Preserve saved document selections when reopening a workspace"}
    ]
  }
}
```

Record this only after the owner approves these exact texts for this source and
destination; a worker cannot grant that authority. Summaries may explain any
included commit, including unlinked commits. Each must be bounded safe plain text
(at least three words, at most 240 characters); link syntax and commands are not
interpreted. Entries outside the frozen range are excluded. Stored approval can
be reused for the same source/text/destination without routine repeated approval.
Changed text or source needs new authority. Publication authority remains separate.

Without eligible public titles or an approved summary in the range, preparation
halts before packaging/review/publication with the candidate and missing-input next
action. Documentation or an inventory alone cannot satisfy the change summary.
The final text, used summaries, authorization ID and membership freeze before
independent lanes, bound by the configuration and source identities in the reviewed
manifest. Lanes verify readability and factual support against the actual source;
approval does not establish accuracy. Resume never regenerates the description.

### Latest policy

`release.latest_policy` defaults to `newer-stable`. Publication promotes a stable
version only when its numeric major/minor/patch is strictly newer than every
published stable version. `v` prefixes are accepted. Older maintenance releases,
equal semantic versions and prereleases never steal Latest. Prerelease suffixes
also set GitHub's `prerelease` flag; drafts always remain unpublished/non-Latest.
Choose `never` explicitly to publish without changing the observed Latest.
Unknown version ordering stops preparation and requests that explicit choice.
For a first stable release with no prior Latest, `never` is rejected before
preparation: an absent Latest cannot be verified after a full release exists.
Choose `newer-stable` for that release. First prereleases can still use `never`;
they do not create a full release. Previously frozen unsupported plans also stop
before publication, preserving their evidence rather than silently changing policy.

The policy, prerelease flag, prior Latest identity and published inventory freeze
before review in `latest_plan`. Publication sends an explicit `make_latest` value
and verifies the intended outcome through GitHub's Latest endpoint (or a complete
inventory proving no full releases). An unchanged prior Latest is required for a
non-Latest choice. Changed inventory/Latest, body or release identity fails closed.
The publication intent includes both release and Latest readback; a lost response,
partial outcome or interruption never causes another mutation. Resume can finish
only by proving the same intended outcome. Completion rechecks Latest before Done.
Observable human edits remain untouched; GitHub has no atomic CAS for this metadata.

Observable GitHub body changes halt publication/recovery without overwriting them.
After Done, the operator can separately authorize a prose-only correction through
`release-description-edit --version 1.2.3 --authorization-id <explicit-approval-id>
--expected-body-sha256 <current-body-digest> --body-file <approved-public-markdown>`
using the same bundled supervisor CLI and target root. Only invoke this after the
user approves the exact public text and edit scope; normal publication authority
or a worker result does not authorize this correction. The expected digest binds
the observed current body, including any human edits. A separate description-edit
intent/readback receipt binds the original publication receipt and manifest hashes.
It changes only `body`, never source pins, original notes, receipts or asset bytes;
no package, check or paid lane is rerun. Repeated/interrupted requests reconcile by
readback only. Mismatched or concurrent edits remain untouched. GitHub has no body
compare-and-swap: an edit inside the final read/write race cannot be prevented;
readback detects conflicting observed outcomes and retains the recovery receipt.
