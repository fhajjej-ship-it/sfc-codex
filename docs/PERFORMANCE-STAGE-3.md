# Stage 3 performance measurements

Whole-delivery measurement now has a separate
[delivery accounting workflow](DELIVERY-ACCOUNTING.md), including linked work
and release reports, retained offline fixtures and a bounded real-Codex
comparison entry point. The component observations below retain their original
scope; they do not establish end-to-end customer delivery savings.

The first change reduces repeated GitHub reads during external merge detection.
The scheduler previously loaded full PR metadata and all changed-file pages
for every saved PR it checked. At that point it only needed the PR state.
It now reads the number and state in one call. A reported merge still goes
through the existing reconciliation checks, including live repository, PR,
source, base, evidence and board checks. The state read uses the existing
rate-limit handling and is repeated on each observation.

## Matched observations

Five read-only pairs on the SFC maintainer repository used PRs #95 and #96,
alternating the order of the two readers. Both paths observed the same merged
state in every pair. The baseline code is the full PR reader from `fc38d5e`,
the merged Stage Two head. Call counts refer to adapter CLI calls. The removed
call is the paginated REST file listing.

| Saved PR state observation | Full PR reader | State reader |
| --- | ---: | ---: |
| CLI calls per PR | 2 | 1 |
| Median elapsed | 1.124 s | 0.412 s |
| Observed range | 0.943–1.421 s | 0.372–0.505 s |

The median difference was 0.712 seconds per observed PR. These observations
measure state detection. They exclude the time required to validate and
reconcile a merge, and do not establish an issue-to-delivery speedup. The
internal GraphQL cost of `gh pr view` was unavailable.

## Remaining critical path

| Observation | Evidence and limits |
| --- | --- |
| Queue discovery after Stages 1 and 2 | One live read-only observation took 26.620 s and returned 23 cards. It used 53 CLI calls, including 49 explicit GraphQL queries with a total reported cost of 49. The four other CLI calls did not expose their cost. |
| Stage Two admission and startup | The retained #93 corrective run took 49.525 s from run start to the Build reservation. The first timestamped CLI diagnostic followed 5.288 s later. This is a startup proxy, without a timestamp for the first useful worker action. |
| Stage Two verification and review | The same run recorded 620.122 s of verification, then 250.569 s from QA reservation to its accepted receipt. QA started 0.101 s after the verification receipt. This single sequential run did not exercise overlap. |
| Release coordination | The pre-Stage-Two v0.1.16 journal records 127.890 s from the first to the last of four Target release write intents. That interval includes coordination. A new publication trace is needed to establish the current baseline. |

Raw before/after timings and the queue trace are retained in the private
maintainer evidence archive. No worker or release was dispatched for the
live measurements.

## Queue reads around reconciliation

Two consecutive read-only inventories on the maintainer board returned the
same 23 cards and stages. The first took 28.220 s and the second 28.633 s.
Each used 53 CLI calls and 49 reported GraphQL points. These timings measure
inventory reads with warm metadata. The live measurement performed no
reconciliation, claims, or worker dispatch.

The watcher can wait for another poll using the first inventory when no worker
is active, the merge mutex is available, reconciliation completes using local
state, and the plan selects no work. It refreshes after initial ranking on the
final permitted idle poll and before any admission based on the idle inventory.
This lets newly eligible and higher-priority tickets compete. Within that poll,
ranking can reuse a verified closed prerequisite.
The watcher discards closed observations contradicted by the refreshed
inventory. Open prerequisites are read again after refresh. Admission rechecks the
selected card and its prerequisites live. If live admission rejects that card,
the watcher defers it until the next poll, refreshes the queue, and ranks
eligible peers once more in the same poll.
Finite runs always refresh after reconciliation, including before an empty
plan can end the run. Startup activity reporting, live PR observation, live
recovery checks, active workers, and unavailable merge mutexes also require a
refresh. Each selected card still goes through the live scope, task, Priority,
and prerequisite checks before admission.

The original PR #97 measurement ran five before and five after trials using 23
blocked cards, temporary repositories, and a fake transport with a 3 ms
delay per GraphQL request. All passes ended waiting, with zero board writes
and zero worker launches.

| Idle scheduler pass | Before | After |
| --- | ---: | ---: |
| Full queue reads | 2 | 1 |
| GraphQL calls for queue inventory | 98 | 49 |
| Median elapsed in the fixture | 1.102 s | 0.992 s |

These figures are historical. A finite pass now takes both reads. A separate
idle-watcher test verifies that a poll with no selected work still takes one
read. The live trace measures the cost of each inventory without timing
end-to-end scheduling or delivery.

## Worker startup and card discovery

Three direct, read-only Codex CLI 0.167.0 launches used a temporary repository
and the maintainer model settings (GPT-6.1 Sol, High, Fast). Each read a known
fixture file and returned its contents. Timings are from process launch.

| Observed startup event | Range | Median |
| --- | ---: | ---: |
| First timestamped skill diagnostic | 0.073–0.853 s | 0.710 s |
| First MCP diagnostic | 0.154–1.033 s | 0.866 s |
| Thread started | 0.202–1.213 s | 0.899 s |
| First command that read the fixture | 6.167–13.337 s | 7.813 s |

The diagnostics timestamp observed events. Total skill loading, MCP loading,
and model latency remain unmeasured. These probes establish time to first
useful command on this host with the existing worker profile.

For queue discovery, two read-only before/after pairs used the full maintainer
inventory with warm metadata and reversed order in the second pair. All four
inventories returned 49 cards with identical serialized snapshot hashes. On
the baseline, separate item and repository issue reads accounted for 98 of
105 CLI calls.

| One full inventory | Separate reads | Combined first pages |
| --- | ---: | ---: |
| CLI calls | 105 | 56 |
| Reported GraphQL cost | 101 | 52 |
| Median elapsed | 50.352 s | 31.720 s |
| Observed range | 50.094–50.610 s | 30.486–32.954 s |

The combined query fetches the first Project item field page and the first
repository issue label page in one request per card. Each connection still
follows its own cursor and the two issue views must agree. The scheduler still
checks live scope before and after inventory and performs fresh reads before
admission, claim, and merge. The change introduces no cross-observation reuse.

Five trials per arm of the 23-card fake-board fixture used a 3 ms delay per
GraphQL request and retained both finite-run inventory reads. Calls fell from
98 to 52 and median elapsed time from 1.193 s to 1.013 s. The timed scope is
discovery and fixture scheduling.

## Watcher regression checks

Matched runs on the `b2b39c6` baseline and the follow-up patch used temporary
Git repositories, the same fake GitHub and worker adapters, and a fixed poll
budget. [The scheduler tests](../tests/test_scheduler.py) cover both cases.

| Scenario | Baseline | Follow-up |
| --- | ---: | ---: |
| Worker dispatches when an issue becomes Ready during reconciliation in a one-poll window | 0 | 1 |
| Prerequisite reads made by planning an idle admission with three closed prerequisites | 6 | 3 |

The read count excludes the selected card's live admission check. A separate
test reopens a prerequisite during that check and verifies that a newly Ready
P0 takes the last claim ahead of an existing P1. These probes measure behavior
and calls in the fixture.

## Remaining bottlenecks in priority order

| Priority | Observed impact | Effort and regression risk | Decision |
| --- | --- | --- | --- |
| 1. Queue observation and admission | The merged discovery change saved 49 calls and 18.632 s per 49-card inventory on the maintainer board. A final-poll miss deferred useful work to another window in the fixture. | Narrow change with a priority race if selection uses stale data. | Retain the discovery change, refresh the final poll, and reuse prerequisite reads only during ranking. |
| 2. Release coordination | Temporary-repository publication and recovery used 113 and 115 adapter reads. Their medians were 1.139 s and 0.673 s with no injected delay, and 1.514 s and 1.066 s with 3 ms per read. Recovery finished with one publish and no extra worker dispatch. | Read attribution is low effort. Consolidating reads across publication or recovery boundaries carries high risk. | Trace the next authorized maintainer release by call and validation boundary. The fixture measures coordination and has no live upload latency. |
| 3. Worker startup | First useful command took 6.167–13.337 s in three direct Codex probes. The median thread-start event arrived at 0.899 s. | Isolating Codex startup phases takes a controlled comparison. Reusing the wrong context can drop required skills or bind stale authority. | Supply the exact bundled lane skill with fresh scope/hash pins. Keep fresh CLI sessions and live repository instruction/tool discovery. See the bounded paired QA observation below. |
| 4. Verification and review overlap | The retained Stage Two trace recorded 620.122 s of verification and 250.569 s from QA reservation to receipt. Two sequential and two parallel trials averaged 146.7 s and 117.3 s, but the source-only reviewer lacked final check evidence. | High risk of reviewing incomplete or superseded evidence. | Keep checks ahead of review. The trial does not establish a time saving for verified delivery. |

## Dashboard evidence acceptance

The six [progress-evidence tests](../tests/test_progress_evidence.py) exercise
the actual supervisor with temporary Git repositories, fake Codex processes and
a fake GitHub board. The stored readbacks cover the same three fields configured
on the real Factory view: SFC Status, SFC Agent and SFC Activity. A read-only
maintainer dashboard check confirmed saved columns and both visible activity
fields; it did not start workers or manufacture live stage transitions.

| Observed fixture state | Stage and activity | Verification evidence |
| --- | --- | --- |
| Queued | Ready, no active-worker claim | No passed-check claim |
| Executing | Building, observed Build process Running | No completed-delivery claim |
| Checking | QA, observed project-check process Running | Current command is still executing |
| Reviewing | Review, observed independent reviewer Running | 2 executed, 0 reused |
| Waiting for CI | Review, Waiting with pending-CI next action | 2 executed, 0 reused; no merge |
| Human handoff | Blocked, Waiting for an authorized merge decision | 1 executed, 1 reused; PR remains open |
| Verified completion | Done, Completed after final merge/readback | 1 executed, 1 reused |
| Exhausted QA findings | Blocked, Failed with retained-work next action | No completion or retry-budget renewal |

The mixed receipt preserves the original execution and marks only its eligible
check as reused. Public PR rows label each check separately. Tests include
private command/path/output sentinels and malformed count/provenance inputs.
They also preserve a historical Failed observation when an operator edits only
the stage to Done; a stage edit alone cannot establish a verified merge.

A paired fixture with and without the count suffix produced the same activity
state/action sequence and the same number of board writes. The change adds no
write callsites or polling loops; existing coalescing still reacts to changed
descriptions. This is a presentation correctness improvement, not a measured
end-to-end speed gain. Raw serialized readbacks and logs are retained in the
private maintainer evidence archive.

## Pinned lane context

Three alternating pairs used the actual production QA prompt and argv captured
from `Factory.lane`, real passing supervisor-check receipts and a temporary Git
repository. Both arms used fresh ephemeral read-only Codex workers, the same
model/settings/tools, repository instructions, candidate and acceptance criteria.
The treatment supplied the exact bundled skill bytes and source hash instead of
asking the worker to open that skill file. All six workers passed, checked the
fixture and retained its repository-policy marker in their evidence.

| Pair | Read skill file, worker exit | Supplied skill bytes, worker exit |
| --- | ---: | ---: |
| 1 | 36.044 s | 26.612 s |
| 2 | 27.745 s | 27.271 s |
| 3 | 39.158 s | 29.560 s |
| Median | 36.044 s | 27.271 s |

The observed median was 8.773 s lower (24.3%) in this small QA fixture. First task
command time and command counts did not improve consistently. These observations
do not establish faster startup in every lane or faster issue-to-merge delivery.
Earlier simplified probes had unequal successful outcomes and are excluded; all
trials and raw production-prompt streams are retained in the private maintainer
evidence archive.

The runtime supplies freshly read bundled skill bytes and pins the skill and
result-schema hashes with source path, head, base, task and configuration. It
revalidates the package before reserving a worker and before accepting its result.
There is no persisted chat, tool-discovery or authority cache. Repository AGENTS
instructions and required skill-relative references must still be read; every
worker retains its original fresh CLI, tools, model and sandbox settings.
Unavailable or unsupported package reads keep ordinary skill discovery. Drift
after preparation halts with original work, output and spent reservations intact.
[Package invalidation tests](../tests/test_worker_context.py) and
[Factory lane tests](../tests/test_worker_context_lane.py) cover all four lanes,
source/task/config changes, skill/schema changes, unsafe files, fallback and
independent parallel contexts.

## Combined validation

The final discovered inventory contained 890 tests across 34 modules. Every
test passed with no skips in isolated parallel module runs (155.208 s); the
runner compared the complete test-ID inventory with normal unittest discovery.
The earlier full `python3 -m unittest discover -s tests -v` run identified four
display-adapter/assertion compatibility failures, which were corrected and
covered by both focused reruns and the final complete inventory. Plugin
validation passed. Independent chat subagents reviewed the context integration
and evidence-reporting boundary. The project configuration remained byte-for-byte
unchanged, including its 800-line automatic merge limit and two-worker setting.
No live board transition, SFC work run, merge or installation was performed.
