# Review efficiency

Existing projects retain their configured three-lane route. Optional changes to
review coverage and inspection each require their own pinned qualification.
See [independent review policy](REVIEW-POLICY.md) for provider setup, evidence
imports, escalation and merge gates.

Every read-only lane receives a compact index of the supervisor's validated
check evidence. It includes the exact candidate, base, task and configuration,
the immutable verification receipt, and each command's result and original
execution and log hashes. A reused execution points to its original receipt.
Reviewers can read those private originals when they need more detail. The
supervisor still validates the complete receipts before dispatch and at the gate.

## Routine behavioral changes

In `review_policy.mode: risk-based`, declare an optional `behavioral_contract`.

```json
"behavioral_contract": {
  "paths": ["src/record_lookup.py"],
  "check_ids": [1],
  "instructions": {"AGENTS.md": "<sha256 of the tracked instructions>"}
}
```

Paths are exact repository-relative file names. Check IDs are one-based
positions in `verify_commands`. Include every tracked `AGENTS.md` or
`AGENTS.override.md` in `instructions`, or use `{}` if the repository has none.
The operator approves the exact contract during setup. Eligible candidates run
independent QA before a separate final review. QA verifies behavior and test
coverage; final review independently checks correctness and regressions. The
supervisor pins the head, base, task, changed paths, instruction bytes,
configured check IDs and immutable executed evidence. The default security lane
can be omitted on this qualified route, and remains required if policy or an
independent reviewer calls for it. A changed configuration
or CLI/model profile requires current qualification. Behavioral promotion also
binds the measured worker, scope, verification and prompt implementation.
Missing coverage calls
for targeted QA. Mandatory risks, sensitive labels and paths, opaque files,
and reviewer escalations continue to require their applicable specialist and
human obligations.

Source QA starts with changed functions and existing tests, probes the nearest
callable entry point, and follows changed contracts into their next consumers.
It broadens the trace when a material gap requires it. It examines observable
behavior against the pinned base and continues after finding a defect. For
small, ordinary changes in a behavioral profile, SFC
can supply the full exact diff and small committed Python definitions for the
checks assigned to behavioral coverage. Reviewers can inspect the checkout and
original logs for all checks. Larger or special diffs use normal checkout
inspection.

Final review can cite execution evidence from an exact, passing QA receipt for
the same criterion. It independently assesses the assertion and result and can
request targeted QA for disputed coverage. The supervisor binds any cited QA
receipt to the final report and revalidates its original evidence. Later QA
requests still require current, independent evidence.

Run `tests/benchmark_review.py --real-model-config <config>` with a new local
`--output` directory and a `--fixture-path` included in the contract. Use
`--fixture-instruction-root` when the fixture needs hash-verified instruction
files. The benchmark uses bounded paired temporary repositories and no board.
It confirms fixture behavior before dispatch with held-out assertions. The
retained double-lookup case reproduces a real lazy mapping regression. The
clean control preserves the same base behavior. Both the retained and seeded
regressions must exercise the qualified route. Its adjudicated report compares
detection, acceptance coverage, false positives, any attestation time, observed
model tokens and end-to-end time. Credits remain unknown when the provider emits
no billing evidence. Promotion requires the clean behavioral route and the
predeclared quality and timing gates to pass.

## Release obligations

Release QA examines package integrity and installation checks. Security
examines the candidate's release-specific risk. Final review examines source
membership, notes, version and the frozen publication plan with the completed
check receipts. An exact full-range match to one work ticket with a completed
atomic local merge or verified human merge journal can carry the original
source assessment. SFC checks the prior base, reviewed and merged trees,
work-ticket identity, policy and independent receipt hashes. It verifies the
original check receipts and logs from the merge gate and from each lane's own
assessment. The default three-lane route records immutable source receipts and
can carry all three passing assessments without a custom review policy. A
release-only update to publication authority or approved summaries preserves
the source-policy binding. Package review still uses the frozen release
configuration. Older baseline receipts without hashes fall back to full source
review. The GitHub-protected automatic merge path uses full release source
review because it lacks that completion journal. Initial releases, batches,
unlinked commits, incomplete evidence and proof that depends on a correction,
provisional inspection or specialist report also require full source review.
Evidence changes after the release pins source coverage halt the attempt.
Package, installation, security and publication obligations remain separately
assigned. With verified source coverage and a spare worker slot, ordinary
package QA and Security assess the completed package evidence concurrently.
Final Review starts after both pass. Releases with an inspection snapshot
continue through the qualified inspection route. Release receipts and
source-coverage references stay private.

## Provisional inspection during checks

`review_policy.inspection.enabled` accepts `source`, `release`, or both in an
array. The optional `inspection.promotion` is a `{ "path", "sha256" }` reference
to adjudicated local evidence for the requested profile. Normal setup can also
select an independently qualified profile from the pinned bundled SFC certificate.
The release proof binds the configured package and publication controls. A release inspection can
start after deterministic preflight passes, if configured. SFC uses one spare
slot within `max_workers` and a detached read-only checkout. Release inspection
receives frozen package hashes, source membership, notes and publication inputs.
The configured checks run in the normal candidate checkout. If the profile lacks
qualification or a spare slot, checks and full review proceed sequentially.

Package inspection uses the frozen checkout and verified artifact paths as its
context. It starts with a bounded ZIP member inventory when available and reads
the original artifact for any suspected install mismatch. It avoids inherited
Codex memory and skill catalogs for that invocation. Unavailable inventories
keep the normal direct inspection path.

For an eligible source change, provisional QA performs the independent source
and behavior examination while project checks run. For an eligible release,
provisional QA examines the frozen package and installation obligations while
the required checks run; release security remains independent. A passing
provisional result can save the ordinary QA dispatch when a separate final
reviewer assesses the actual completed checks and verifies the required
acceptance evidence. The final receipt binds the inspection and completed
check evidence. Gaps or mandatory specialist work require fresh assessment.
For provisional source inspection, the bounded pinned diff and declared
check definitions are available as soon as the candidate is frozen.
For source final review, small completed UTF-8 check logs with verified hashes
appear alongside their declared check definitions and the pinned diff. Larger
logs stay available at their original paths. The provisional receipt contributes
its verdict, findings and summary, with a path to its original observations.
The final reviewer still verifies the check assertions, maps every criterion to
evidence and investigates any disagreement.
A qualified inspection that confirms a concrete defect can reject the
candidate after the required checks complete. The private rejection receipt
binds the inspection and check evidence. Source failures follow the normal
correction budget and release failures block the release. Incomplete
inspections fall back to ordinary QA. A passing final review that disagrees
with a retained provisional finding halts for reconciliation. Failed checks
prevent acceptance and cancel the unfinished inspector while
retaining its spent reservation and available evidence. Candidate drift
invalidates the overlapping phase.

The first run under a new profile is sequential. To schedule overlap, SFC
requires a recent, matching execution under the same configuration that took
at least five seconds. The release estimate includes artifact checks. This
keeps profiles qualified on substantive workloads from adding a separate
inspection to short or unmeasured checks. A prior execution estimates duration
only; it supplies no current acceptance evidence.
Each dispatch has a durable reservation, with at most one launch per exact
scope and the configured finite attempt limit across scopes. Restart preserves
the reservation and original outputs. A complete inspection can be used again
only while its candidate, base, task, policy, obligations and evidence still
match. Merge and publication still use their separate gates and authority.

`status` records obligation assignments, receipt references and inspection
states. Delivery accounting retains distinct model and check execution time,
observed token usage and separate final review. Inspection status also reports
the measured intersection of the inspection and check intervals.
The offline delivery report additionally separates actual process overlap,
total process compute time, inspection usage and final-assessment call overhead;
see [delivery accounting](DELIVERY-ACCOUNTING.md). These measurements do not
qualify a profile. Source review qualification alone enables neither concurrent
route: each needs its own adjudicated real-Codex inspection comparison.

Release Security and final review receive direct paths to the verified artifacts,
original check logs, manifest and frozen public notes. The packet includes the
complete change inventory, publication plan and any carried source coverage.
Security starts from the changed source and the actual package/public metadata.
Final review uses the pinned source and finished checks to verify public claims,
installation and publication readiness. Both can open the full evidence for
discrepancies and still block on missing coverage.

Release final review receives one provisional inspection packet with its original
receipt reference, observations and findings, alongside completed project,
artifact and installation evidence and the composite QA descriptor. The prompt
omits the duplicate frozen inspection snapshot and refers to that descriptor
without repeating it. Original receipts, snapshot validation and final-result
bindings are unchanged. Smaller prompts are a structural result; latency or
whole-delivery improvements still require matched real-Codex trials.

When a release has eligible provisional package QA and a spare worker slot,
Security and final review assess the same frozen candidate after checks in
parallel. Both lanes must pass before publication. If the slot is occupied or
the release lacks eligible provisional QA, the existing sequential route runs.
Keep inspection/check overlap distinct from the overlap between completed
Security and final review when comparing total time and model compute.

## Qualifying overlap

Use `tests/benchmark_review.py --real-inspection-config <config> --output <new-directory>`
with `--package-workload-root <frozen SFC source>` to run paired source fixtures
through the real SFC package compatibility suite and a packaged behavior check.
Use `--checks-only` to measure the workload without model calls. The runner
preserves failed arms and prints a draft for independent adjudication.
Use `--retained-worker-timeout` or `--clean-worker-timeout` to declare the
same 120–180 second cap for both arms of the selected case before dispatch.
This comparison records its exact case plan, Codex provider and finite
dispatch reservations.

`tests/benchmark_release_inspection.py` runs the isolated package comparison
through normal release stages. Use the same
candidate, task, obligations and provider settings in each pair. Capture a
clean accepted candidate, an actual failed check and a retained defect. Include
the complete check receipt, all specialist and decisive assessment receipts
or the bound rejection receipt,
the provisional receipt, full wall time and independent adjudication. The
release profile also needs its actual frozen release evidence. The comparison
binds the target policy and model settings, separate reviewer contexts and raw
completed check and review output. The promotion also pins the measured runtime,
and adjudication covers each actual finding, including provisional findings.
For a rejected case, independently map every obligation to its check log hash
or a successful command from the original inspection turn. The retained defect
requires a successful inspection probe.
Measure overlap, compute time, final call
time and observed tokens independently. Only profiles that retain coverage and
defect detection, avoid more false positives, and improve median and clean-case
wall time by at least 5% through the final assessment or rejection can be
enabled. Evaluate with
`python3 tests/benchmark_review.py --evaluate-inspection <report.json>`.

Source comparisons default to 120 seconds per worker and 90 per required
check. An explicit pre-execution plan may assign a matching worker cap of up
to 180 seconds to both arms of a retained-finding case. The plan binds each
arm's measurement and charges its full worker cap before each dispatch. The
comparison stays within ten dispatches and 1,200 reserved worker-seconds.

The bounded source and release workflow tests exercise these contracts with
temporary repositories and fake adapters. A real-model overlap comparison is
required before either profile can become active.
