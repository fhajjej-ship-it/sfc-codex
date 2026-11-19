# Stage 1: immutable verification evidence

Repository-wide handoff and explicit scoped-input contracts extend this mechanism
in 0.1.18. See [delivery efficiency](DELIVERY-EFFICIENCY.md) for setup, migration,
the merge-to-release diagram and the distinction between original execution and
new candidate acceptance. Existing contracts keep their ticket-bound semantics.

The supervisor owns configured full checks. Build keeps the focused-check contract
introduced by #36/#38: reproduce defects, run meaningful focused checks, and record
explicit builder obligations or necessary full-suite reproduction exceptions.
Generic repository completion obligations are fulfilled by supervisor checks before
overall completion. A builder pass only requests independent QA. Prompts and these
declarations are workflow contracts, not a security sandbox.

## Opt-in command contracts

Existing configurations are unchanged. Commands without an explicit contract always
execute, including at the final gate. To enable one command, add a project-owned
`verification_reuse` list to configuration. Every entry must contain all fields:

```json
{
  "command": "python3 scripts/check.py",
  "max_age_seconds": 3600,
  "environment": ["PATH", "LANG"],
  "tools": ["/absolute/path/to/python3"],
  "dependencies": ["requirements.lock", "/absolute/runtime-and-installed-dependencies"],
  "artifacts": ["ignored-output/report.json"],
  "external_state": "none",
  "worktree": "independent"
}
```

This is a schema example, not a tested contract for an arbitrary Python project.
The owner must enumerate relevant tools, interpreter/runtime libraries, installed
dependencies, lockfiles, ignored inputs and required outputs. Git source is bound
automatically, with actual file bytes/modes checked against Git blobs even when
index flags hide changes. Unsupported source symlinks/submodules miss reuse.
File/directory content inventories include the root entry's mode and recursive
names; missing files, special files and symlink dependencies miss reuse. Tool
symlinks bind both the resolved target and its bytes/mode. Tool targets must be
regular files: devices, FIFOs and directories miss reuse without reading them.
Hashing also checks the opened descriptor, uses nonblocking opens against FIFO
replacement races, and checks the supervisor stop/deadline signal between chunks
and inventory entries. Each fingerprint phase has a finite `verify_timeout`
budget (120 seconds for helpers called without a factory); expiration or a stop
halts verification without publishing success. These cooperative guards do not
bound an individual kernel filesystem call on an unresponsive mount.
Absolute dependency
paths bind host inputs; relative paths bind equivalent content in each checkout.
Do not point these fields at secrets. Only PATH, LANG, LC_ALL, LC_CTYPE, TZ and
PYTHONHASHSEED can be inherited by eligible commands; unspecified variables are
removed with `env -i`, and SFC_WORKER=1 is fixed. The command must work in that
environment. Credentials, HOME-dependent behavior, undeclared tool discovery and
external services require fresh verification. Declarations cannot infer hidden
inputs or make hostile commands hermetic; do not opt in unknown dependencies.

`external_state: none` declares no network/time-sensitive external authority.
Other values are unsupported and always execute. `worktree: independent` declares
checkout-path/ref/index/ignored-state independence beyond the recorded inputs;
`path` binds the absolute checkout, so detached gates miss. Unknown values miss.
Freshness is per command, at most 86400 seconds from original completion, without
renewal on reuse. Future timestamps miss. Source-check reuse never certifies a
release package, installation, live CI or external acceptance obligation.

## Receipt and final-gate protocol

Version 1 records repository/common-git identity, source tree, exact head/base,
task/identity/configuration/command, platform, bounded environment, toolchain and
dependency hashes, timestamps/duration, exit status, raw-log and required-artifact
hashes. Execution and aggregate receipts are exclusive-created and fsynced in
unique attempt directories. State retains their byte hashes. Files are never
rewritten to claim a new execution. These are tamper-evident local records under
the existing trusted supervisor/host model, not signatures against a hostile host.

Each new attempt clears current success durably before dispatch. Only the previous
completed successful aggregate is considered; interrupted, failed, legacy,
incomplete, changed, expired or tampered evidence misses. No directory scan promotes
an orphaned check after a crash. Interrupted logs/receipts remain available. An
eligible reuse entry points directly to the original execution receipt and hash;
its original time, checkout, log and execution ID remain unchanged. Per-command
miss reasons, executions and original reuse IDs appear in receipts/local history.

The current worktree and all bindings are rechecked, including log/artifact hashes.
Changes during execution invalidate success. Final gates validate the detached
candidate and again validate evidence before the merge intent/write. A gate miss
executes; drift after checking halts or returns to QA. Base/head/task/configuration
changes retain the existing integration and independent-review requirements.
Review-lane evidence is hash-checked before model dispatch; stale legacy review
state returns to QA. Same-ticket verification is serialized; different tickets
keep separate locks. The existing common-git supervisor lock excludes competing
processes/checkouts. Restart never grants worker, check, merge or mutation authority.
Independent QA/security/final review, live required CI, missing-check policy,
800-line policy, two-worker project setting, finite budgets and uncertain-write
journals remain. Package/artifact checks keep their separate release reservations.

## Selective reuse after a correction

A repository-scoped command can opt into `source_inputs` only when the owner
can enumerate everything the command reads. Use tracked file or directory paths
for source and test code, and include any text, executable examples, skills or
configuration read by that command. Toolchain, runtime, environment, declared
dependencies, artifacts and non-output ignored/untracked inputs still have to
match. A changed candidate only qualifies if its delta contains existing ordinary
text files with changed bytes and unchanged modes. Renames, additions, removals,
mode changes, executable changes, special entries, unsupported text and missing
bindings rerun the check. This restriction applies even outside the named inputs.
Keep generated inputs in the binding. Declare an output exclusion only for a
path the check never reads. Commands without a contract always execute. Package
and installation checks continue to run for the corrected candidate.

Optional `review_policy.correction_reuse` carries **criterion evidence** through
a correction on the same ticket:

```json
"correction_reuse": {
  "correction_paths": ["docs/guide.md", "src/lookup.py"],
  "criteria": {
    "REPLACE_WITH_CRITERION_ID": {
      "source_inputs": ["src/lookup.py", "tests/test_lookup.py", "data/rules.txt"],
      "check_ids": [1]
    }
  }
}
```

Use the 16-character criterion ID from the structured review's acceptance
mapping. List exact files; the source inputs describe what the criterion and
its interaction checks depend on, not a convenient subset of modified files.
Check IDs are one-based positions in `verify_commands`. Each check must
already support reuse under its own contract. The original independent receipt
must show that those exact checks covered the criterion, and the new acceptance
must validate the same original executions. The input files must exist and
remain unchanged. All correction paths must be explicitly declared and meet the
ordinary-text delta rule; sensitive and repository instruction changes cannot
qualify. A changed task, base, policy, configuration, risk route, toolchain,
dependency or relevant input invalidates retention. Focused command evidence
and evidence already carried from an older review cannot be carried again.

On an independent failure, the supervisor archives original receipts and open
findings before clearing current acceptance. On retry it records a new, immutable
assessment naming the candidate, the kept and invalidated criteria with reasons,
and any unresolved findings or specialist requests. The original execution,
review and attempt reservations remain unchanged. The current lanes still run
as required. A fresh reviewer can cite a retained criterion in its new report;
it must inspect the correction and its interactions and resolve each archived
finding with explicit evidence before final acceptance. A prior specialist
request invalidates retention for the disputed criterion. Fresh QA must address
the original report and provide coverage for that criterion on the corrected
candidate. Disabling the optional coverage contract stops reuse and does not
remove the findings. Keep structured review enabled
while the correction has open findings or requests.

The local delivery report distinguishes reused check references, actual check
processes, retained criterion evidence and its use in passing final review.
Retained criteria do not count as skipped model calls or measured time savings.
These records do not confer merge or publication authority.

## Reproducible historical baseline

Read-only sources: two retained maintainer runs from 2026-11-10 for issue #90.
Their full identifiers and locations remain in the private evidence archive.
Use each `verify-*/receipt.json` for command/head/base/checkout and the referenced
`check-1.log` for unittest's own elapsed time. Pair lane `dispatch.json` and
`receipt.json` timestamps for worker intervals. Do not count both item.started and
item.completed JSON events as separate command executions.

| Actual full unittest execution | Candidate | Tests / suite seconds | Why |
| --- | --- | --- | --- |
| First Build, build-5b57d8be item_58 | Intermediate dirty source; not final | 753 / 634.852 | Worker explicitly cited AGENTS.md completion wording |
| First supervisor verify-0c581dff | 183452ff4551 | 756 / 553.482 | Configured checks before QA, which found a release defect |
| Final supervisor verify-bf24c00a | ec79762b53b7 | 761 / 645.503 | Changed final source required new checks |
| Detached gate verify-b5634dc6 | ec79762b53b7 | 761 / 540.185 | Prior contract required a second checkout execution |

All supervisor receipts pin base bcfbf84e700dc34420480b229b81112e1ac0821f.
The final pair pins the same task/configuration/head/base. There were four actual
full unittest executions across these attempts, three supervisor validator
executions and two completed Build validator commands (items 57 and 108).
The Build worker log explains the exception; its result explicitly says the full
run began before a final refinement. Its later focused checks do not turn that
intermediate run into final-source evidence. No new ownership feature was needed.

Build intervals were 945.591 and 317.690 seconds; the first includes its 634.852
second suite. Initial failing QA took 170.033 seconds. Final QA/security/review
intervals were 250.971 / 210.530 / 184.505 seconds (646.006 total, including startup/IO).
The final resumed run start to Done-request interval was 1926.972 seconds; subtracting
the two suite runtimes and final lanes leaves about 95.279 seconds for coordination,
validator execution and timing-boundary differences. Old receipts lack precise
per-command wall clocks: these suite timings are not complete subprocess timings.
The full earlier-to-final interval includes interruption/operator gaps, not pure
factory overhead. No model-only timing or customer-wide savings is established.
The observed 540.185-second repeat is an opportunity; old receipts remain ineligible.

## Before/after fixture and verification boundary

Run `python3 tests/benchmark_verification.py`. It alternates three fresh temporary
repository flows per mode with identical source tree/command, real 0.2-second
checks and fake GitHub/Codex. Both modes traverse Build, QA, security, review and
the atomic local-merge protocol. JSON reports source identity, implementation hash,
execution/reuse counts, verification subprocess/wall and end-to-end seconds.
This is an offline representative issue flow, not customer-board acceptance.

Initial local measurement (seconds), implementation SHA-256
`b0f91a4ffe5a738bc559a4a56149596c47bc15fc58a18cd38ce72b2b909b600c`:

| Three alternating flows per mode | Fresh execution at both gates | Immutable reuse |
| --- | ---: | ---: |
| Actual check executions per flow | 2 | 1 |
| Reuse references per flow | 0 | 1 |
| Median check-process seconds | 0.5465 | 0.2497 |
| End-to-end samples | 1.6836, 1.9378, 1.9476 | 2.1121, 2.0195, 2.1512 |
| Median end-to-end seconds | 1.9378 | 2.1121 |

All six flows kept all four lanes and exactly one fake atomic merge. Verification
process time fell, but end-to-end time increased by 0.1742 seconds on this tiny
fixture: conservative fingerprinting has measurable overhead. This does not prove
a net customer speedup or convert the historical nine-minute opportunity into
measured savings. Raw samples are retained in the private maintainer evidence archive.
Focused builder regression groups passed: 128 merge/factory tests and 164
recovery/session/release tests. Final reuse/lane checks also cover source-byte
validation, both merge modes, misses, tampering, restart and concurrent attempts.
Plugin validation and `git diff --check` passed. These are builder checks;
The supervisor owns the full unittest run on the actual committed implementation,
plugin validation and independent QA/security/review before factory completion.

### QA correction measurement

After binding directory roots and rejecting special tool files, the same six-flow
benchmark used implementation SHA-256
`b50f0fbb234ee3b6ff41f44e7de2e5fc337dbfdeaeee6f6d5945157750b0432a`.
Fresh/reuse modes again executed two/one checks and retained all four lanes and
one fake atomic merge per flow. Median check-process time was 0.4753/0.2429 seconds;
end-to-end samples were 1.7761, 1.7651, 1.6723 / 2.0351, 2.0034, 2.0199 seconds
(medians 1.7651/2.0199). The small fixture still shows no net speedup. Host load
was uncontrolled, with unittest verification also running, so differences between
this run and the initial measurement cannot be attributed to these fixes alone.
Raw correction results are retained in the private maintainer evidence archive.

The correction regressions reproduce both prior failures: a 0755-to-0555 directory
root mode change accepted reuse, and device/FIFO tool inputs exceeded a three-second
external timeout. They now check fresh execution for changed dependency/artifact
roots, immediate special-tool misses, raced-in FIFO descriptors, and stop/timeout
propagation without publishing successful evidence. Completion-only recovery
retains its existing bounded allowance rather than extending the work deadline.

Correction verification: 19 reuse tests and 10 completion-recovery tests passed.
This repository's explicit AGENTS.md full-suite requirement was the builder
exception: the final source passed 780 tests in 602.703 seconds, plugin validation
returned `valid: true`, and `git diff --check` passed. The earlier full run loaded
code before completion-allowance compatibility was corrected; it finished with
8 failures and 5 errors, all from the fingerprint stop check preempting that
allowance. The intermediate run and the repeat on final source are retained in the private
maintainer evidence archive. Neither run is a supervisor
receipt for a committed candidate. Supervisor-pinned verification and independent
QA/security/review remain required before overall factory completion.
