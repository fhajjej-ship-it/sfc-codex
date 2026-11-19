# Delivery-efficiency qualification

This release distinguishes tested control-flow improvements from measured
customer delivery performance. The reference release is v0.1.17 at commit
`efe380bb6ec4cdf71f4ef8c8e1d382fe72708411`.

On a checkout without the historical release, supply another checkout containing
that exact commit to `tests/benchmark_delivery.py --qualify --baseline-repository`.
The four historical-comparison tests use `SFC_TEST_BASELINE_REPOSITORY` for
the same purpose. A new public clone can run the rest of the suite on its own.

The review-only comparison also retains the released v0.1.18 runtime at
`2133f015195ddc237a8551184b4b1e9356373e3e`. Its percentages describe independent
review, not complete delivery, and must not be combined with delivery timings.

Current package qualification is separate from the historical trials below.
The fixed bundled `assets/qualification/sfc-python-review.json`, when present,
records the exact runtime, supported upstream source, model/provider, project
check contracts, independent report hashes and measured promotion gates. Normal
setup revalidates these inputs; source and package overlap qualify separately.
An absent or mismatched proof keeps the corresponding thorough route. Release
notes and the source-bound delivery report state which targets were measured,
including failed and inconclusive comparisons. The initial-context reduction
and immutable check reuse do not by themselves establish whole-delivery savings.

## v0.1.25 released defaults

The v0.1.17 reference and v0.1.25 fresh and migrated fixtures reached all 18
expected outcomes with fake GitHub and Codex adapters. Each of the five
publishable cases per profile completed a downloaded install after simulated
publication. Each current profile ran the suite once, and verification at merge
and release used two references to the same original receipt. The reference ran
the suite three times. The security-sensitive case retained a human handoff
in all three arms.

In the documentation and package-version correction fixtures, explicitly
declared input scopes kept one matching code check and reran packaging and
installation checks. The original failure and spent attempt remained in the
record. Representative package preflight failures ran no release project checks
and started no review workers. An interrupted publication resumed the original
attempt, read back the already published release, and closed the ticket with zero
additional workers and no second publication.

The bounded real-Codex comparison held GPT-6.1 Sol High/Fast constant and used
the same temporary fixture repositories and fake GitHub publication adapter.
The current arm used fresh onboarding. Two of six planned pairs completed.

| Completed case | v0.1.17 to v0.1.25 verified install | Time reduction | Input and output token reduction |
| --- | ---: | ---: | ---: |
| Ordinary feature | 626.59 s to 399.82 s | 36.2% | 52.9% |
| Bug fix | 604.21 s to 457.48 s | 24.3% | 41.4% |

Both bug-fix arms reproduced the missing-record exception on the base and
verified the corrected CLI and final package. Independent assessment verified
all criteria in the two completed pairs. Neither pair required intervention
or retry or reported a false positive. The separate offline sensitive flow
preserved human approval on both current profiles.

The v0.1.17 documentation-correction release Security worker hit the common
180-second cap while reconstructing internal verification hashes. The campaign
stopped with 34 of its 78 reserved launches used, one failed worker and zero
retries. Its elapsed evaluation time was about 44 minutes, including setup and
the failed arm. The stopped arm's incomplete token usage remains unknown.
The **30% routine median** and **20% aggregate normalized usage** goals remain
**inconclusive**. The two completed cases show fixture-level results. Real
GitHub latency, customer workloads and billed cost were outside the comparison.

The published v0.1.25 ZIP and checksum passed all seven package checks when
downloaded and verified at release commit
`04ff9a4a3c508145d3e36245c202dc3c508a0576`.

### Follow-up comparison with verified legacy receipt bindings

The fixture supplies v0.1.17 release workers the exact verified inputs and
encoding for the two existing receipt digests. The released archive is
unchanged. The documentation Security lane passed in 110.2 seconds under its
original 180-second cap. Independent workers still inspected artifacts, logs
and release notes, and the supervisor still required the same release verdicts.

| Case | v0.1.17 control | v0.1.25 | Endpoint |
| --- | ---: | ---: | --- |
| Documentation correction | 566.23 s | 358.64 s | Verified install |
| Package version correction | 566.22 s | 352.09 s | Verified install |
| Sensitive change | 219.43 s | 208.63 s | Human handoff |
| Interrupted release | 578.09 s | 393.22 s | Verified install after recovery |

All four pairs reached their expected outcome and passed independent
assessment. The 50 worker calls had zero failures or retries. Both sensitive
arms required a human handoff. Both interrupted arms retained the original
attempt and recovered the first publication without another worker or publish.
The two routine correction cases had a 37.2% lower median. The three
publishable cases used 45.9% fewer comparable tokens. The prior two real pairs
used the unassisted baseline. The full 30% and 20% targets remain inconclusive
because neither protocol covers the entire case inventory.

## v0.1.25 draft model comparison

Six matched temporary-repository pairs compared GPT-6.1 Sol High/Fast and
Medium/Fast under the same SFC draft route. Both profiles detected the
declared missing-record regression, covered the acceptance criteria,
retained the sensitive case's human handoff and resumed the interrupted
draft without another worker call or release write. The completed trial
used 78 worker calls and required zero retries.

Routine median time to a verified draft install was 349.1 seconds High/Fast
and 311.9 seconds Medium/Fast, a 10.7% reduction against a declared 5% latency
objective. Comparable input plus output tokens across the five draft
delivery cases fell from 6,549,036 to 6,468,651 (1.2%). The token total
does not establish billing. Individual token totals varied by case.
The trial used fake GitHub and small deterministic fixture checks.
Published customer delivery targets remain inconclusive.

The earlier campaign stopped when a release reviewer flagged the
publication criterion on a draft-only ticket. The corrected campaign
measured both profiles on the same new draft-specific ticket contract.

## v0.1.22 source trials

The candidate fixes the discrepancy between ticket lint and review acceptance
parsing, including bare section headings and oversized prerequisite numbers.
The comparison evaluator accepts independently reproduced additional findings
through hashed proof and observation files. Each arm receives credit only for
findings present in its original review.

The final source trial completed both defect pairs. Both routes detected all
five declared findings and five additional independently reproduced findings
with complete acceptance coverage and zero false positives. End-to-end elapsed
time was 165.36 versus 164.65 seconds on the first case and 167.09 versus 179.78
seconds on the second, baseline first. The clean-case model calls did not start.
The trial stopped when the local Codex configuration and installed guard changed.
The four completed worker reservations remain in the trial ledger.

These cases establish defect detection for the observed pairs. Source overlap,
package overlap and model-profile selection each require a completed comparison
for their applicable route. The two whole-delivery targets remain inconclusive.

An initial 0.1.21 upstream ticket-validation experiment stopped after two
completed QA calls and one interrupted reservation. Baseline QA took 78.20
seconds and found four independently reproduced failure modes. Candidate QA
took 119.97 seconds and missed the downstream acceptance-parser mismatch. Both
reports left criterion coverage unresolved. This candidate did not qualify;
its reservations remain spent. The subsequent diagnostic predeclares all four
modes, uses a shared downstream-contract examination instruction, and pauses
after its first pair for independent assessment before further dispatches.
These experiment costs are evaluation overhead, not successful delivery samples.
The following diagnostic stopped after one baseline reservation timed out at
120 seconds, before a candidate call. Its trace contained 16 shell reads and
about 106 KB of output before the first behavioral probe, followed by repairs
to probe setup. Successful probes eventually reproduced the four modes, but
there was no completed assessment. A later runtime starts from the nearest
callable/test and supports short labels for retained execution evidence.
Those changes are not, by themselves, proof of a latency improvement.

## Whole-delivery comparison for 0.1.20

The reduced behavioral review route **did not qualify**. In the final bounded
comparison, baseline QA found both retained lookup defects, while candidate QA
found the signature regression but missed a separate duplicate-read defect.
The candidate rejected the change, but failed the required full-recall gate.
It cannot enable reduced review. Source/package overlap and alternative model
profiles also remain unqualified; the normal three-lane fallback is retained.

The actual whole-delivery trial on candidate `0ef888649d5953d4736c88bd3165388e8a2912a2`
completed the v0.1.17 ordinary-feature reference, including downloaded installation,
in 646.58 seconds. Current ordinary-feature and bug-fix arms timed out during
release QA after 413.15 and 390.03 seconds including setup. Those failure times
are not successful-delivery speedups. The comparison was stopped, retaining all
19 worker reservations, 16 completed-turn usage records and incomplete outputs.
Observed completed turns used 4,240,414 input tokens (3,827,072 cached) and 50,325
output tokens; incomplete turns and monetary billing remain unknown. No Default
tier comparison was launched. There is no qualified whole-delivery performance
or cost claim from this trial.

The timeout logs showed successful package and installation inspection followed
by repeated reconstruction of internal receipt and release-note hash formats.
The final release separates lane responsibilities and states which mechanical
bindings the supervisor validates. It does not raise timeouts or substitute a
model verdict for validation. The comparison runner now stops further paid arms
after an incomplete outcome, even when the fixture process exits successfully.
These fixes do not retroactively qualify the failed trial.

Four separate review trials retained 19 dispatch reservations in total. The
first stopped for runtime defects, the second for fixture trust/root handling,
the third for latency and an invalid clean control, and the fourth for the
defect miss above. They are evaluation overhead, not additional successful
samples. Original receipts remain private. Failed reports cannot activate a
profile.

The same fixture sources, task, model settings and package checks run under the
released and current supervisors. The reference uses v0.1.17 onboarding. The
current arm uses normal fresh onboarding or the explicit preview/apply migration.
Each starts with the two configured Python checks and adds a separate, authorized
fixture release configuration after the source merge. GitHub is simulated in
temporary repositories. A model trial uses actual Codex workers for both arms.

The fixture publishes a deterministic ZIP containing a Python command and its
version, documentation and applicable behavior files. The archive inventory and
CRC check runs before source verification in the current arm. A separate command
extracts the archive, verifies installed bytes and exercises the installed CLI.
The released arm runs the same archive and installation checks in its artifact
phase. After simulated publication, both arms smoke-test the supervisor's verified
download. The report keeps the publication readback and verified installation
times. The before-change oracle, configuration, archive, logs and worker results
are retained locally. The interrupted-release case also preserves its original
journal and checks that resume adds no worker, check execution, release attempt
or repeated publication write.

All 18 offline executions reached their expected outcomes under the released,
fresh and migrated configurations. Fifteen verified the downloaded installation,
and three security-sensitive cases retained the human handoff. For each publishing
case, the current profiles executed the full source suite once and referenced
that execution twice. The reference ran the suite three times. In the separate
matched documentation and version correction pairs, the current selective route
retained one code check, rebuilt the archive and reran the extracted command.
The original failed verdict and spent reservation remained intact.

An actual 0.1.19 comparison timed out in release QA after five model launches per
arm. The v0.1.17 reference took 336.49 seconds and the 0.1.19 arm took 321.04
seconds through failure, including fixture setup. Both workers tried to infer the
serialization and input for the project receipt hash. The current arm's early
package phase also left an empty post-source artifact receipt. The replacement
fixture supplies separate archive and installation logs. Current release workers
also receive the exact private manifest hash bindings and their encoding. The
120-second evaluation limit remains in place.

A complete bounded real comparison covers all six scenarios. Its outer budget
admits one launch per ticket lane, with at most seven launches per publishing
arm and four for a security-sensitive arm: at most 78 launches for a paired
six-scenario plan. Each worker has a 120-second evaluation cap independent of
normal saved setup limits. Each subprocess uses an invocation-local trust
override scoped to its generated Git repository. Smaller selected comparisons
remain individual results and cannot establish the full qualification gate.
The routine median, aggregate comparable usage, quality gate and real billing
need their respective evidence before drawing broader conclusions.

## What was demonstrated for 0.1.19

Eighteen offline executions covered ordinary feature, bug fix, documentation
correction, package/version correction, security-sensitive work and interrupted
release, across released, fresh and migrated configurations. GitHub and model
adapters were simulated; commands, temporary Git repositories, package bytes,
receipt validation and publication recovery were exercised locally.

All scenarios reached their expected outcomes. Each of the five publishing
scenarios went from three full-suite executions to one with two immutable reuse
references. Security-sensitive work retained its human handoff. The tiny checks
made the new fixtures slower because fingerprinting overhead exceeded the work
avoided. This proves avoided executions, not a delivery-time percentage.

Separate preflight regressions exercised the actual package verifier. Stale
Quick start filenames and concrete internal evidence paths stopped the normal
release path before source verification, model dispatch or publication. Other
regressions cover changed source/runtime/dependencies, hidden files, executable
resolution, corrupted receipts, restart reservations and artifact substitution.

## Correction and normal setup evidence

Two separate matched correction pairs exercise an initial failed candidate,
restart and a corrected head through the normal supervisor. Documentation and
package-version corrections retain the original code check and one unaffected
criterion, while packaging and fresh extraction/installation checks execute
again. Fresh final review addresses the original finding; original receipts and
spent reservations survive. Both whole-source arms rerun three checks; each
selective arm executes two and reuses one. These use fake model/GitHub adapters
with real checks, Git and artifact bytes. The tiny checks saved roughly 0.05
seconds of process work, while corrected workflow wall time was about 3.2
seconds slower. This is evidence of selective reuse, not a speed claim.

Twelve further offline scenarios use the actual onboarding command or its
explicit migration path with both supported Python checks and normal limits.
Source work starts without release configuration; release setup occurs after
the checked merge. Ten publishing cases complete once and the two sensitive
cases retain human handoffs. This makes model-profile route evidence applicable
to a real setup configuration rather than an unreachable benchmark-only route.

## Earlier 0.1.19 review experiment

The pre-release review experiment uses GPT-6.1 Sol, High reasoning and
Fast tier. Eight actual model calls cover two defects and a clean control in
paired independent contexts. Both arms detect both defects, map all three
fixture criteria, and produce no false positives. Median check-and-review time
falls from 58.13 to 45.85 seconds (21.1%). The clean control falls from 220.22
to 43.02 seconds. Observed input tokens fall from 863,271 to 352,790 and output
tokens from 14,010 to 3,815. Credits and billing are unknown.

The predeclared gate passed for that exact fixture policy and runtime. A matching
temporary setup proposal accepted the evidence without creating candidate
authority or launching work. This did not demonstrate default onboarding
activation. Existing projects are not silently switched. Scripted fixture
attestation takes about 0.020 seconds; it does not
measure human attestation burden. One sample per case does not establish broad
customer outcomes. An earlier eight-call diagnostic also passed, but its older
runtime is not eligible to enable a newer profile; it remains evaluation
overhead. Smaller immutable check packets are now supplied to all read-only
lanes without dropping their independent obligations.

A subsequent audit hardened original-log validation in release source-review
transfer and preserved disputed QA criteria through corrections. It also fixed
Git's ignored nested repositories appearing inside the explicitly excluded SFC
state/output directories, which could make reuse unavailable. Regressions cover
these cases without excluding adjacent ordinary inputs. These changes alter the
runtime binding: the earlier measured report cannot enable the final package.
Any newly selected route requires a fresh matching report and normal setup
readback. Later qualification outcomes and the exact downloaded artifact are
reported with the release; the earlier percentages are not final-package claims.

The packaged MCP launcher explicitly disables Python bytecode writes, including
when the host filters environment variables. The transport regression launches
a fresh exported package and verifies that startup leaves its source untouched.
This prevents the package's own startup check from creating new verification
inputs during a suite run.

A bounded Fast-versus-Default trial on the earlier candidate reached the
sensitive-case human handoff in 233.11 seconds with four successful Fast workers.
The Codex configuration fingerprint changed, so the comparison stopped before
any Default worker. This is inconclusive and does not select a tier or establish
monetary savings. Failed and interrupted comparisons remain evaluation overhead.

Source overlap remains unqualified. Its actual checks took 0.17–0.53 seconds,
so the trial stopped after one sequential baseline QA call and before any paid
inspector. An additional call cannot be assumed to improve short or fully reused
checks. Release overlap was not measured. Both profiles retain the qualification
gate; no concurrent route is enabled by these measurements.

## Retained 0.1.18 trials and limits

The bounded behavioral-review comparison used GPT-6.1 Sol, High reasoning and
Fast tier. Both arms caught two known defects with complete fixture acceptance
coverage. The corrected clean case took 53.55 seconds with one reviewer and
192.56 seconds with three. Across all three paired cases the median rose from
50.14 to 52.21 seconds, failing the predeclared latency gate. The original clean
fixture contained genuine defects; its discarded comparison is retained as
evaluation overhead. Ten model calls were made across both trials. Fixture
attestation does not establish real operator effort or billing.

The separate whole-delivery pair was inconclusive. Release security blocked the
reference arm over ambiguous fixture release notes. The candidate snapshot
omitted a worker schema and stopped before a model request. The snapshot bug was
fixed with a regression test; no paid retry was used to turn that failure into
a success claim. That trial predates the final isolated-runtime integration and
does not qualify the final package's model performance.

The proposed 30% routine median latency and 20% comparable usage reductions are
**inconclusive** for complete delivery. No alternative model profile is selected.
The review experiment above applies only to its earlier matching review profile;
source/package inspection overlap remains gated. Passing package checks and
published download verification do not establish delivery performance or billing.

## Reproduction

`tests/benchmark_delivery.py --qualify` compares the pinned reference source with
a captured current source using temporary repositories and fake GitHub. Without
model configuration it is an offline adapter test. To select the ordinary-feature
pair, pass `--profile fresh --case ordinary-feature` and provide both
`--baseline-model-config` and `--candidate-model-config`. Use a new local output
directory. The plan and original evidence retain failed as well as successful
executions. Hold model, effort and tier constant to measure the shipped workflow.
Use the same finite timeout for both arms. `--profile both` tests fresh onboarding
and migration together with fake adapters. A real comparison measures one current
profile at a time. The fixture requires QA to reproduce the missing-record
failure on the base and verify the corrected behavior.
`sfc.py efficiency-qualification <qualification-input.json>` reconstructs the
result from pinned evidence without dispatching a model or changing configuration.
Include `--adjudication <file>` to evaluate independently verified coverage,
defect detection and human handoffs against the original input hash.

Use `--legacy-release-bindings` as a separate publication comparison when
v0.1.17 release workers stall reconstructing verification hashes. The fixture
checks the original receipt inputs, logs and manifest against their digests
and supplies the exact private inputs and their encoding to baseline release
lanes. The baseline runtime, review obligations, timeout and publication
checks are preserved. The plan records the adapter. Keep its results
separate from runs using the unassisted v0.1.17 baseline.

`--model-only --model-objective latency` (or `usage`) predeclares a same-runtime
model comparison and uses actual normal onboarding. It records the plan before
dispatch and checks the observed source, settings, cases and schema paths against
that plan. Publishing cases also require matched release routes. Reports track
latency, comparable token usage and quality separately from the whole-delivery
targets. Actual billing and missing usage remain unknown. Raw evidence stays local.

For an SFC maintainer workspace with draft-only release authority, use
`--draft-only --saved-config <project>/.sfc/config.json` with one selected
profile. The fixture keeps the saved verification, merge and draft route
settings. Temporary repositories use small deterministic project checks and
archives with the configured command names. This measures the supervisor
route on the fixture workload. The separate full-suite verification records
the actual maintainer workload. Draft cases finish after exact downloaded
asset validation and an install smoke test. The interrupted case restarts
from a verified draft receipt with zero repeated remote writes or launches.
The missing-record regression in the bug-fix case supplies a declared
defect control. Report draft comparisons separately from publication targets.

The 0.1.22 plan compares GPT-6.1 Sol High/Fast with one benchmark-only
alternative, GPT-6.1 Sol Medium/Fast, against a latency objective. Adoption
requires matched whole-task evidence that meets the objective, passes independent
quality assessment and has separate authorization for the configuration change.
An inconclusive comparison leaves the High/Fast preset in effect.

`tests/benchmark_release_inspection.py` runs the separate sequential versus
overlapping release comparison. It uses a clean package, a wrong-version check
failure and a package whose documentation references a missing command. With
`--sfc-workload` it also freezes a portable SFC archive once and verifies that
same artifact, checksum and source in both arms. Each arm runs the actual
SFC package verifier, including tests of installation from the extracted archive.
An existing frozen workload can be shared via `--prepared-sfc-workload`. An
explicit `--real-model-config` runs an unpaid check-duration pilot before any
actual model launch. The comparison retains failures and requires independent
adjudication of findings and coverage before any route qualifies.

See [the complete workflow and configuration contracts](DELIVERY-EFFICIENCY.md),
[review qualification](REVIEW-EFFICIENCY.md) and
[delivery accounting](DELIVERY-ACCOUNTING.md).
