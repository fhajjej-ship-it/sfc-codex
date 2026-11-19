# Delivery accounting

Normal supervisor run/watch writes local `delivery-report.json` beside
`summary.json`, including on a caught failure or stop. Run output and `status`
expose its path, ranked `slowest_stages`, token subtotals and unknowns. No new
GitHub field, write, polling loop or authority is introduced. Reports contain
private command text and paths; keep them out of public PR/release notes.

Use `python3 <plugin-root>/scripts/sfc.py --root <repo> delivery-report` to
rebuild the latest report offline. `--run-id <saved-directory-name>` selects a
historical run. This uses local files and Git repository discovery, without
loading configuration, contacting GitHub or starting Codex. Retain the repository,
`.sfc/runs`, state and common-Git-directory `sfc-releases` journals together.
Source paths and SHA-256 hashes support inspection. These are historical
observations, never fresh verification that remote state is still valid.
Original receipts remain unchanged; legacy missing timings stay unknown.

## Reading the report

Start with `slowest_stages` and `stages`. Each stage separates overlapping wall
time from summed process durations. `run_wall_seconds` spans supervisor preflight
through its final outcome; report-generation overhead is outside that interval.
`summed_worker_seconds` counts Codex invocations only. Overlapping stages need
not add up to wall time. Each release has a `stages` map linking its release
and work tickets, candidate/boundary, journal, effects and execution IDs across
runs, including recovery. Work ticket observations are bounded by the source
cutoff. Release activity stops at verified completion, even when the receipt
that records completion closes just after that timestamp. Its `tokens` subtotal
covers linked executions across runs; top-level `tokens` covers only the selected
run. Do not add release totals together when releases share work-ticket evidence.

`compute_seconds` sums observed top-level process durations, including checks
and model calls; it is elapsed process time, not CPU time or billed cost.
Read `unknown_compute_durations` alongside it. `inspection_overlap` separately
reports `wall_seconds`: the union of actual inspection/check process
intersections for the same run, ticket, candidate, base, configuration and task.
It excludes unrelated concurrent work and check setup time. This can differ
from the phase intersection recorded by inspection status. Failed processes
still consume time; incomplete intervals appear in `unknown_intervals`.
Its `inspection` and `final_assessment` sections separate observed call counts,
compute seconds, unknown durations, unconfirmed launches and token subtotals.
Final-assessment accounting binds the provisional receipt hash at dispatch,
including a failed or interrupted final call. Historical calls without that
link are not attributed to this overhead. Release summaries expose the same
metrics over their linked executions; these include the linked work-ticket
phases and are not additive with the current-run totals.

| Stage | Observation |
| --- | --- |
| queueing | Within a run, observed Ready until the first confirmed launch, withdrawal from Ready, or run end. The report does not carry Ready into a later run where it was not observed. Time outside these observations is unattributed. |
| build | Build workers and release packaging commands. |
| checks | Actual configured project/artifact commands, including failures. |
| review | Independent QA, security and review workers. |
| publication | Publication attempts, including validation, upload/download and interrupted readback. Resume has a distinct observation without repeating its remote effect. |
| coordination | Wall-time remainder: discovery, administrative Git/GitHub work, scheduler work, inter-run waits and unattributed time. This is not measured human effort. |

Read `unknowns`, `unknown_durations` and each token field's `unknown_executions`
before comparing costs. Zero known tokens plus unknown executions does not mean
free work. A launch receipt establishes execution even if a timeout, stop
or hard termination leaves no exit code. If only the reservation survives, the
launch is unconfirmed and is excluded from `observed_process_count` and token
totals; read `unknowns` for these gaps. A launch without completion has unknown
duration, outcome and usage. Caught exceptions retain attempt duration and
exception type. An exit code establishes execution for legacy receipts.
Administrative Git/GitHub subprocesses are itemized in the append-only
`administrative_commands` journal, including startup and failure observations.
Their summed durations are separate from the coordination wall-time remainder.
Commands inside external adapters or threads without a supervisor observation
context remain unknown. `argv` also itemizes checks, packaging, asset downloads
and workers.
Nested worker command items appear in `commands` and `nested_command_count`;
they are contained in worker time, never added as extra worker invocations.

`executions` retains historical invocations for provenance.
`current_run_execution_ids` selects the invocations charged to the run's time
and token subtotals. Stable observation ID and original log identity prevent
receipt copies, stream start/complete events and restart scans from counting an
invocation twice. `verification_references` records executed-check miss reasons
or direct original reuse references. Reuse adds no original tokens or process
duration to the current run and changes no verification eligibility.

Worker JSON turn completions supply available input, output and cached input
tokens, deduplicated by thread/turn. Missing fields stay unknown while known
subtotals from other turns remain counted. Each execution's `token_unknowns`
marks those partial fields. Cached input is a subset of input, never an extra
input charge. An incomplete additional turn marks usage incomplete while
retaining completed-turn subtotals. A malformed event adds a reporting gap;
usable subtotals from other turns remain available. Invalid, missing or changed
log hashes make stream-derived usage unknown. The native review CLI can emit
zero-filled usage when its delegated review supplies no counters. Reports mark
those counters unknown and exclude the affected samples from comparable usage.
Completed reviews can still contribute elapsed time and quality evidence.
If report construction itself fails, the run summary and status retain a
reporting gap and the original supervisor outcome. Invocation
model/provider/effort/tier overrides are explicitly **requested** settings;
inherited/effective settings stay unknown. CLI adapter identity is not a
model-provider or billing attestation.

Actual Ready entry time, human intervention count/duration, credits and billing
are unknown. `ready_observed_to_verified_seconds` needs linked work-ticket Ready
observations and a final verified release timestamp. `ready_to_verified_seconds`
stays unknown without actual Ready transitions. Legacy done journals without
that timestamp stay unknown. Currency estimates are null: no bundled rates or
subscription-credit conversion is assumed. Future estimates must include a
dated rate basis applicable to the provider/model/tier, separate from actual
usage and billing.

## Reproducible baseline and comparison

Run `python3 tests/benchmark_delivery.py --output /tmp/new-delivery-evidence`.
Six bounded scenarios retain base source, exact acceptance oracle, before-defect
log, scenario/configuration, Git objects, original receipts and reports. Each
starts with an oracle failure and uses normal `Factory.run`, verification/merge
gates, `Releases.request` and release scheduler entry points. Ticket creation
and GitHub/Codex use fake adapters; packaging and verification execute actual
local commands. There are no customer-board or external writes.

| Scenario | Expected acceptance/defect outcome |
| --- | --- |
| Ordinary feature | Enable the missing feature, then verified fake publication. |
| Bug fix | Correct missing-record lookup, then verified fake publication. |
| Documentation correction | Replace obsolete instructions; exact prose oracle passes, then publication. |
| Package/version correction | The base selects 0.9.0 and packages its old VERSION bytes. The corrected candidate selects 1.0.0 from VERSION without an override, then publishes the new version file as the verified artifact. |
| Security-sensitive change | Correct the unsafe default; retain human merge handoff without merge/publication writes. |
| Interrupted release | Stop after fake publication before readback; restart at the original attempt limit, publish once, rerun no workers, verify Done. |

These test deterministic adapters and accounting, not actual model detection or
customer delivery speed. `comparison.json` labels the execution basis.
`evaluation_wall_seconds` includes setup and the failing-before oracle;
supervisor reports separate this evaluation overhead. `--case <name>` narrows
the fixed inventory. One attempt per ticket, eight scheduler cycles and
120-second worker timeouts remain finite; recovery renews none of them. Tests
also cover duplicate stream events, unknown usage, concurrency, failed/hard-
interrupted attempts, Ready withdrawal, a frozen release cutoff, malformed
telemetry, cross-run reuse and offline reconstruction.

Fixture timings depend on the host. Retain each comparison with its raw
source, configuration and receipts. Single-run fixture timings are not
matched speedups or currency estimates. Fake adapters do not measure actual
Codex usage, customer upload latency, human effort or billing.

For an explicitly authorized real-Codex comparison, provide both
`--baseline-model-config <json>` and `--candidate-model-config <json>`.
Each JSON needs explicit `model`, `reasoning_effort`, and `service_tier`.
Only these settings are imported; authority and commands remain fixture owned.
Arm order alternates across the same selected scenarios, at most six scenarios
and two arms, retaining actual Codex logs and available usage. GitHub, uploads
and releases remain simulated. No result changes project policy. A model failure
retains work and terminates the comparison without an automatic retry to obtain
a pass. Offline runs do not imply real-Codex measurements.

## Qualifying workflow and model changes

From an SFC source checkout with the released v0.1.17 commit available locally:

```sh
python3 tests/benchmark_delivery.py --qualify --output <new-evidence-directory>
```

This runs the six fixed scenarios on the exact released supervisor and the
current snapshot, using fresh and explicitly migrated configurations. The same
fixture source, task and routing inputs are pinned across arms. The fixture's
source check is deliberately tiny. Read actual full-suite execution and reuse
counts separately from elapsed time: hashing and validation can outweigh a
tiny saved command. This does not measure the size or speed of your suite.
The released supervisor has no delivery report writer, so its unmodified
retained receipts are read offline and supplemented by the same adapter
observation used on the current supervisor.

Use `--profile fresh` or `--profile migrated`, both explicit model config files,
and optionally `--case` to run a bounded real pair. On a host with supported
process cleanup, the arms use actual Codex with a fake board and release
destination. The same external fixture clock runs from Ready to verified
completion; setup, the failing-before oracle and final report reconstruction
are separate. Failures, retries, unknown usage and slower cases remain in
`qualification.json`. Normalized usage adds input and output tokens from
complete same-model samples; cached input is already included in input. The
report makes no credit, billing or currency estimate.

The workflow comparison changes runtime and configuration, so it cannot
attribute any difference to the model. Add `--model-only` to compare different
model settings on the same current runtime and profile with all other route
settings pinned. A real comparison requires one current profile. Retain
existing settings when a candidate cannot start or the evidence does not
qualify it.

The 30% routine median latency and 20% comparable usage goals require enough
matched real samples. Fake timing and a single real pair remain inconclusive.
Quality requires all six outcomes, independently assessed complete critical
defect detection and acceptance coverage, and no increase in false positives
or human interventions. An observed human handoff is separate from unknown
human intervention. A comparison does not qualify a route without this
evidence or change any project settings.

Rebuild qualification offline with `python3 scripts/sfc.py
efficiency-qualification <evidence-directory>/qualification-input.json`.
An independent reviewer can attach `--adjudication <review.json>`. Its
`input_sha256` binds the qualification input, `reviewer` identifies the
reviewer, and `cases` maps each case and profile to `released` and `current`
assessments. Each assessment records `critical_expected`, `critical_detected`,
`criteria_expected`, `criteria_covered`, `false_positives` and
`human_interventions`. Keep the underlying defect and acceptance evidence
with the comparison. A missing assessment remains inconclusive.

Accounting remains separate from exact candidate/base validation, immutable
verification receipts, missing-check policy, finite launch/attempt budgets and
separate merge/publication authority. A report grants none of them.
