# Pinned independent review policy

Projects without `review_policy` continue to use the three existing lanes and
saved merge choices. Stage 1 checks run before independent review and are
validated again at the final candidate. CI, branch protection, merge authority,
size limits, attempt spending and worker slots remain separate. Release review
and publication retain their existing gates. Optional behavioral coverage,
release source-evidence reuse and provisional inspection are documented in
[review efficiency](REVIEW-EFFICIENCY.md).

## Explicit opt-in and fallback

An operator can select a native provider for final review while retaining QA
and security:

```json
"review_policy": {
  "mode": "baseline",
  "providers": {"qa": "codex-exec", "security": "codex-exec", "review": "codex-native"},
  "mandatory": ["security"],
  "routine_paths": ["docs/**"],
  "risk_rules": {"authentication": ["src/access/**"], "data": ["jobs/purge/**"]},
  "max_age_seconds": 86400
}
```

Omitted providers use `codex-exec`. Built-in rules cover authentication and
permissions, secrets, money, migrations and destructive data, publication, and
merge/recovery control. Control filenames are checked at every depth, including
the repository root. Bulk-delete and ORM delete calls trigger data review.
Administrator checks such as `is_admin` trigger authentication review. The
diff includes removed lines. Repository rules add globs to those minimums.
Both sides of a rename count. Routine paths can match ordinary prose in
`.md`, `.rst` and `.txt` files. Before selecting a routine route, SFC checks
both complete file versions for code blocks, executable markup and code-like
content. Detected code or unclassified content requires full review. Source and
configuration formats outside that set, executable files, binary files, mode-only
changes, symlinks, submodules, missing implementation diffs and paths outside the
routine allowlist count as unknown. The merge policy's sensitive labels and paths also
require QA, security and a human decision. A tracked `AGENTS.md` or `AGENTS.override.md`
keeps implementation changes on the thorough route unless a qualified
behavioral contract covers the exact instruction inventory. Reviewers follow repository
instructions and can add QA, security or human review. Reviewer and builder
output cannot remove an obligation. QA-only retains three lanes. An assessment
of the unchanged base has an expected empty diff. Human assessment still applies
when required by policy, labels or independent review.

`mode: risk-based` requires native review and a supervisor-approved
`promotion` reference (`path`, `sha256`) to a passing real-model comparison
for that policy. Without it the configured three-lane baseline remains.
For a route that starts with a single reviewer, the project operator must attest
that the exact candidate fits the qualified prose contract. Set
`routine_candidate: {"head": "<candidate SHA>", "base": "<base SHA>", "task":
"<task digest>"}` in `review_policy` after checking how the repository uses the
changed files. The task digest is recorded in the review scope. An allowlist,
prose detection and a passing comparison cannot establish that a runtime does
not interpret an arbitrary text file. Missing or stale attestation keeps the
full route for unclassified changes. A valid attestation cannot override risk
signals, repository instructions, mandatory obligations or merge policy.
Only a qualified attested prose candidate starts with one independent native
review. A qualified behavioral contract instead assigns acceptance to independent
QA and correctness to a separate final reviewer. Setup approval of the exact
contract is sufficient; each candidate still needs fresh, pinned evidence. The
supervisor checks the changed paths, tracked instructions, risks and configured
check IDs. A specialist remains required for mandatory risks, repository rules,
and independent escalations.
Coverage gaps or escalations add targeted QA, security or human work in the
same ticket slot. Targeted QA and security receipts bind the exact reports that
requested them, including a security request from targeted QA. SFC retains
cross-lane specialist requests for the same candidate, base, task and policy.
Requests left open after the bounded specialist checks block completion.
Unresolved acceptance waits for executed evidence.

An unavailable provider, missing report, malformed coverage or incomplete turn
halts the configured review and preserves output and spending. To fall back
explicitly, remove `review_policy` or select `codex-exec` in baseline mode.
A configuration change requires fresh evidence; authority, limits and
uncertain-write journals remain. Removing the optional policy stops applying its
saved route and requires fresh evidence under the active three-lane policy.
Do not edit receipts to resume. The Factory card shows the selected route and
pending lanes during review, including targeted QA, specialist escalation and
any human decision. The route remains visible at the CI or human handoff.
`status` exposes the recorded route, risk, obligations and import gaps.
When the policy requires human review of a QA-only assessment, SFC keeps the
issue Blocked with the pinned assessment evidence for the human reviewer.

## Native and existing-review evidence

[OpenAI's local review guide](https://learn.chatgpt.com/docs/code-review)
describes the dedicated reviewer and custom scope. The `codex exec review`
entry point exposes JSON events and last-message output in CLI 0.167.0.
Native review uses its own rubric. SFC requests structured acceptance evidence
in `overall_explanation` and reads the structured verdict and findings from the
completed `ExitedReviewMode` item in Codex's local session rollout. The CLI's
rendered message alone cannot distinguish a valid zero-finding review from
Codex's text fallback after a parse failure. SFC checks the rollout's thread,
checkout, CLI version and turn completion against the dispatch and event log.
A fresh read-only invocation uses clean candidate bytes and a custom prompt
with both immutable SHAs and the full diff range. It creates a persisted Codex
session so the supervisor can retain and hash its structured review alongside
the raw CLI events, last message and normalized receipt. These are private
local records. Native source validation hashes tracked symlink text and verifies
initialized submodule checkouts against their pinned commits. An uninitialized
submodule can be reviewed at its recorded Git commit pointer. Stage 1 check
reuse still requires its original source binding. Older native receipts without
the structured rollout require fresh review before reuse.

Each covered acceptance criterion needs an execution reference and an
explanation of which assertion covers the behavior. A configured check uses
its recorded log hash. QA can also reference a focused read-only experiment
from its own turn using `kind: qa-command` and an empty `sha256`. A command
whose first line is `# sfc-qa-proof: boundary-check` can use the short
`id: proof:boundary-check`. The label must start with a lowercase letter and
contain at most 64 lowercase letters, digits or hyphens; a nonempty command
body must follow. Markers in output or later lines do not qualify. Unlabeled
commands can use their exact submitted text as the `id`. A label resolves to
one successful execution in the completed Codex event stream. A failed setup
attempt can reuse the label if its command text differs from the successful
command. The successful command must appear only once. A malformed completion
under the same label invalidates the reference. The full recorded command
including its shell wrapper remains a valid reference. A command can
successfully reproduce a failing criterion by asserting the observed incorrect
behavior. Its coverage row records the result and the lane reports a failure
with a finding. Use `qa_required` when execution evidence is insufficient or
disputed. The immutable QA
receipt binds the full log hash and the candidate, task, policy and requesting
reports. Receipt reuse rechecks that evidence. Command success supplies
execution evidence. QA still has to assess which assertion it verified.
Source evidence can document a gap. Incomplete mappings or an incomplete turn
cannot certify acceptance. Original acceptance evidence stays pinned
when final verification executes fresh checks. Missing, expired or changed
evidence blocks the gate. Valid existing exact review is reused.

Optional `imports: [{"path": "<supervisor inbox path>"}]` names at most two
stable supervisor-owned evidence envelopes. Configure the paths before
collecting reviews so the configuration digest can be echoed. A native
envelope contains an original SFC receipt reference with raw completion log,
structured result and immutable verification snapshot. A GitHub envelope uses
the same scope and evidence, `provider.provider: "github-native"`, PR and
review IDs, the raw GitHub review JSON as `log`, and the structured review body
as `result`. GitHub reuse requires configured `github_reviewer_ids` (numeric
bot IDs), an exact commit and submitted time, a completed state, full coverage
and live readback of the review body and all inline-comment pages. A review
body claiming zero findings is reusable only when the comments endpoint
confirms an empty inventory. A verified failing or blocked result retains its
structured findings or blocker and takes precedence over a cached or imported
pass. Each verified envelope can add obligations even when a different receipt
satisfies review. The supervisor retains those requests for the same candidate,
base, task and policy, including requests made by a local reviewer whose
receipt is later replaced. Targeted QA and security bind to the reports that
requested them. A disputed criterion requires targeted QA with executed
evidence for that criterion. Configured inboxes are read at Review entry and
again after final verification, before either automatic merge write. New
findings stop the merge. SFC returns to Review when an import adds obligations.
After the final review readback, SFC rechecks issue freshness before requesting
the merge or recording the atomic ref write intent. Each check reads the configured inboxes once and
pins the bytes of accepted envelopes. The atomic merge journal records the
validated route and review time before dispatch. Recovery checks the same
evidence at that recorded time and verifies the live merge outcome.

[OpenAI's GitHub review guide](https://learn.chatgpt.com/docs/third-party/github)
notes that default reviews flag P0 and P1 only. A comment, emoji, green check,
partial or dismissed review, or missing base/config/coverage binding is a reuse
miss. SFC records the gap and runs the configured provider. Native output
format or capability changes fail closed until verified.

## Bounded evaluation and promotion

`scripts/sfc_lib/review_benchmark.py` fixes the criteria before measurement:
three to twelve cases, at most three paired runs per case, including a retained
real finding, a seeded regression and a clean control. Keep source and task
identical between arms. Retain raw logs, timed lane receipts and independent
finding and coverage adjudication. Promotion requires complete known regression
recall and acceptance coverage, no increase in false positives, and at least
5% lower median review time and end-to-end latency. Each clean control that
completes the consolidated route must also have a completed, fully covered
baseline and improve end-to-end latency by at least 5% against that same
fixture. Report unique findings and both timings. Label extra runs as evaluation
overhead. A report never changes a default by itself.

`python3 tests/benchmark_review.py --output <new-local-evidence-directory>`
executes bounded temporary-repository fixtures with distinct base and candidate
commits. Use `--fixture-path docs/example.md` to place the synthetic change
under a docs-only allowlist. The same relative path and source are used in both
arms. The executable Markdown fixtures require full review. The clean prose
control changes documentation and checks both the statement in the guide and
the behavior of the unchanged implementation. The retained case
replays the first-stable `never` choice with no prior Latest reported in #90. Its
production regression is `test_first_stable_never_rejected_before_creation_or_packaging`
in `tests/test_releases.py`. The other cases are a missing-record regression
and a clean control. Executable oracles stand in for reviewers. The run
measures fixture behavior and process overhead only. It cannot promote policy.
The #90 historical review duration is an unmatched observation.

Under supervisor authority, add `--real-model-config <project-config.json>`
to run both arms on each of the three temporary diffs. This uses the configured
model, effort and tier, the same acceptance criteria and actual fixture checks
for each arm, and no GitHub board. Both arms use the structured coverage
contract. The baseline runs the existing QA, security and review sequence and
stops at the first decisive failure. The candidate applies the target policy's
providers, mode, risk rules, routine paths, mandatory lanes and reviewer
escalations to the fixture. Project evidence imports and the previous
promotion report do not apply to synthetic candidates. Real comparisons and
promotion require explicit review models, reasoning effort and service tier.
Projects that inherit Codex settings retain the baseline review route. Promotion requires a
clean fixture that completes the consolidated route. With an interactive
terminal, pass `--attest-clean-control` to inspect and attest the exact clean
candidate as an operator. The decision is optional and the prompt times
out after five minutes. With no positive attestation, the candidate uses the
full review route. The runner starts timing before the operator inspects the
candidate and includes that elapsed time in the immutable arm record. A human
handoff or unresolved acceptance gap prevents promotion because the timed arm
ends before acceptance. End-to-end time covers attestation, fixture verification
and review up to the arm's outcome. The paired run itself is evaluation overhead.
Reports created under earlier promotion criteria or without timed attestation
cannot promote the policy. Rerun the comparison to obtain current evidence.

Retain `comparison.json` and its referenced repositories and evidence.
An independent adjudicator records their identity in
`independent_adjudicator`, assigns each reported finding to a known regression
or false positive in each arm's `adjudications` using zero-based `receipt_index`
and finding `index`, and lists verified criterion IDs in `coverage` with a
reason per ID in `coverage_adjudication`. Evaluate
that copy with `python3 tests/benchmark_review.py --evaluate <comparison.json>`.
SFC verifies the actual diff, matching task, separate completed reviewer
contexts, raw messages, check receipts, policy, lane order and recorded timings
before accepting the adjudication. A criterion claimed as covered must map to
a check in an actual QA or review result. Keep the baseline until independent
review and the comparison satisfy the gate.

Set `finding_adjudication_version` to `2` in an independently adjudicated
real-model report to retain verified findings beyond the predeclared inventory.
Each sample or inspection pair can add up to eight IDs in `additional_findings`,
with an absolute `path` and `sha256` for each proof. The proof records `version`
1, `kind`, `id`, `case`, `base`, `head`, `task_digest`, `adjudicator`, `reason`,
a pinned `probe` file and `runs` for `base` and `head`. Each run records its
exact `commit`, a successful `exit_code` and a pinned JSON `log`. The log contains
`version` 1, `commit`, `probe_sha256`, boolean `satisfied` and a concrete
`observation`. A `regression` requires the base to satisfy the contract and the
head to violate it. An `acceptance-gap` names the original `criterion` ID and
requires a violation on the head. The evaluator reads the retained evidence.
Independent reproduction is done outside the evaluator.

In version 2, each mapped raw finding uses `regressions` entries with its `id`,
`reason` and an `excerpt` from the exact finding message. Additional findings
must be mapped to at least one arm, and the candidate must report every verified
additional finding reported by the baseline. The predeclared recall denominator
stays fixed. Verified additional findings are counted separately. Disputed or
unverified observations still prevent promotion.

### Project-specific behavioral comparison

Prepare the target with normal onboarding or migration. Copy its saved config
to a local proposal file and add the proposed `review_policy`. Keep the exact
project identity, verification commands, existing reuse contracts and worker
settings. The proposed policy needs a behavioral contract and an explicit
native reviewer. Attach the report's promotion reference after qualification.

The trial manifest has `version` 1, an absolute `source_root`, a `target_config`
reference and a `cases` array. A reference contains `path` and `sha256`. Relative
reference paths resolve from the manifest directory. Each case contains `case`,
`kind`, full `base` and `head` commit IDs, `title`, `body`, `labels`, `expected`
finding IDs and an `oracle` reference. Use three to six distinct cases with
`retained-real-finding`, `seeded-regression` and `clean` controls. Both regression
kinds and a clean control must exercise the proposed behavioral route. Put the
actual acceptance criteria under one `## Acceptance criteria` heading in each
issue body. Lint and review use the same parser for `-` and `*` bullets,
optional checkboxes, and continuation text on populated items. Empty bullet
placeholders cannot supply a criterion or borrow text from the next line.
Store each held-out Python oracle outside the source repository. It must pass on
the base and the clean candidate, and fail with an assertion on a regression
candidate. The separate oracle evidence stays local.

```sh
python3 tests/benchmark_project_review.py --manifest <manifest.json> --output <new-private-directory> --prepare-only
python3 tests/benchmark_project_review.py --manifest <manifest.json> --output <another-new-private-directory>
```

Preparation imports only the pinned Git history into a new Git common directory
outside the project and executes the held-out oracles in detached checkouts.
The isolated main checkout holds the measured configuration. Pass `--guard`
with the installed SFC `hooks/guard.py` to verify that it permits an ordinary
Git read and denies a synthetic protected push request before model dispatch.
The real comparison uses the normal check and review workers in that isolated
repository and performs no GitHub operations. Each arm runs the same configured
checks freshly. Generated Python check runtimes are recreated inside the isolated
Git common directory. The qualification still binds the full original target
configuration. Project checks and oracles execute locally with their normal
command permissions.

By default the trial reserves at most eight model calls per case, with 120
seconds per worker and 90 seconds per verification command. A manifest may
predeclare `measurement_limits` with the positive integer keys `worker_timeout`,
`verify_timeout` and `maximum_model_dispatches`. The worker timeout can be at
most 180 seconds and must fit the configured worker timeout. The verification
cap stays at or below 90 seconds. The dispatch count and its product with the
worker timeout must fit the default case-count budget. The same limits apply
to both arms, within the saved project timeouts. The trial pins the original
declaration and records the applied limits in each arm's timing receipt.

The trial keeps the plan, completed arms and private receipts, including
failures. Independently adjudicate the
retained `comparison.json` using the same evaluation command above. A passing
report can be supplied to normal setup using the review qualification manifest
described in [delivery efficiency](DELIVERY-EFFICIENCY.md).
