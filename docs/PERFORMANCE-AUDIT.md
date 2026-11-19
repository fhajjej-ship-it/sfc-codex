# SFC observation performance audit — 2026-11-10

Two narrow changes reduce observation work without skipping checks or reviews:
avoid repeated details for completed closed tickets, and stop loading saved
dashboard layouts for runtime activity updates. These changes are local source
work on `codex/performance-audit`, based on stable commit
`5caed6f0831574a72811496edf5f2b0a36fd63a6`. They are not installed or released.

## Measurement and scope

The audit inspected AGENTS.md, DESIGN.md, the actual scheduler, factory, pickup,
activity, GitHub adapter, native session runtime, release and recovery paths.
It compared current GitHub issue titles with merged source/history. Existing
work already covers supervised-builder full-suite ownership (#36), targeted
owned-ticket reads (#42), scoped planning (#44), priority and rolling refill
(#70), lifetime watching (#63), recovery/manual completion (#65/#79), onboarding
(#75) and optional releases (#67). Readable release notes remain separate #83
work. None of those features was reimplemented or dispatched again here.

Historical traces are actual local receipts for issue #80 / PR #81, release
card #82 / v0.1.15, and the interrupted #83 admission. Live measurements below
are read-only SFC maintainer Project observations. No customer board was used,
no mutation was performed by the profiler, and no worker was launched. Synthetic
tests use temporary repositories and fake GitHub/Codex adapters.

The private maintainer evidence archive retains source preservation hashes,
read-only profilers,
before/after JSON, history extraction, startup probe and check logs. No original
run receipt, worktree, retry count or release journal was rewritten.

## Before and after

Warm metadata, same 42-member live Project, two sequential observations per
version. Source-before is the unchanged stable release candidate; source-after
is this working tree. Network variability remains; these are observations, not
a customer latency SLA or a claim about total issue-to-merge speed.

| One live queue observation | Before | After |
| --- | ---: | ---: |
| Elapsed samples | 43.143 s, 42.463 s | 23.108 s, 22.442 s |
| Median elapsed | 42.803 s | 22.775 s |
| API/CLI calls | 91 | 47 |
| Explicit GraphQL calls / reported cost | 87 / 87 | 43 / 43 |
| Project memberships inspected | 42 | 42 |
| Tickets receiving full details | 42 | 20 |

This removes 44 calls and about 20.0 seconds per observed scan (46.8% elapsed
reduction). The four unchanged `gh repo/project view` calls are included in total
calls; their internal GraphQL cost is not included in the explicit-query cost.
Twenty-two safely completed closed cards needed no further detail reads. Other
historical records deliberately retained full reads because they still carried
handoff/dispatch/recovery obligations. They were not silently repaired.

Offline scaling benchmark, one open ticket and all other tickets safely completed:

| Board members | Before GraphQL calls | After GraphQL calls |
| ---: | ---: | ---: |
| 1 | 5 | 5 |
| 25 | 53 | 5 |
| 100 | 203 | 5 |
| 250 | 505 | 7 |

All membership pages and both routing/Priority scope checks still run. The
benchmark adds a controlled 2 ms delay per fake request; its wall-clock values
are not real network speed claims. Reproduce the current measurements with
`python3 tests/benchmark_observation.py`; output includes relevant source hashes.

A separate live read measured the dashboard metadata query at GraphQL cost 2
and the field-only inventory at cost 1. The individual timings were 0.521 s and
0.591 s: **no latency improvement is established for that individual query**.
The improvement is reduced quota/payload and removal of unrelated view pagination.
Activity still performs fresh field and item reads both before and after writes.
For the measured single-page layout, those two metadata reads cost 4 points
before and 2 after; target-item reads and mutations are unchanged.

## Actual workflow baseline

| Segment | Observed result | Interpretation |
| --- | --- | --- |
| #80 run start to Build reservation | 28.3 s | Planning, claim, worktree and activity overhead; not model work |
| #80 Build | 80.136 s | Actual worker dispatch-to-accepted receipt, including startup/IO |
| #80 initial 672-test suite | 503.576 s | Necessary configured project verification |
| #80 QA / security / final review | 90.264 / 87.651 / 96.131 s | Separate actual lane executions |
| #80 final candidate 672-test suite | 522.061 s | Same head/base/task/config, different detached checkout; current merge contract requires execution |
| #80 run start to Done | about 24 m 36 s | Includes all verification, coordination and publication; not pure overhead |
| #82 project suite | 526.344 s | Same source as #80, but different task/config/base range and package context |
| #82 QA / security / final review | 105.154 / 174.398 / 117.440 s | Actual package/release review lanes |
| Release Target-version assignments | 519.495 s between first and last of 15 intents | Median 36.793 s between member intents; includes repeated identity/card/field readbacks |
| Release checksum intent to manifest intent | about 12 m 48 s | Includes quota interruption and operator recovery, not upload bandwidth |
| #83 creation-complete to Build reservation | 172.108 s | Slow admission despite available capacity |
| Last four #83 queue-plan intervals | 140.051–142.647 s | Configured polling interval is 30 s; synchronous queue/reconciliation reads add substantial work |

The #83 saved session halted with a GraphQL rate-limit error. The audit did not
rearm it or erase its retained dispatch. Empty capacity is not automatically a
scheduler bug: the last plans had one occupied slot and no selected eligible
peer. #70's existing barrier tests exercise immediate refill with independent
arrivals and highest-priority selection while a slow peer remains active.

The release recovery reused its accepted QA/security/review receipts and kept
one attempt; it did not rerun those lanes during the readback-only resume.
Publication and Done remain separate verified effects, not a green model verdict.

## Startup and attribution limits

An isolated native SFC MCP process completed initialize, tools/list, unattached
status and EOF shutdown in 0.0349–0.0386 s (five samples; median 0.0361 s).
No hook attachment, live project, paid model or customer configuration was used.
That measures SFC's local protocol process, not the entire Codex app startup.

Seven accepted historical lanes logged their first timestamped Codex diagnostic
3.4–4.2 seconds after dispatch reservation. The interrupted #83 Build had a
235.95-second gap. These are composite intervals, not isolated skill/MCP/model
timings. Logs show inherited plugin discovery and unrelated MCP startup failures;
they do not timestamp every first useful tool action. No settings were disabled
based on speculation. Skill discovery, model startup and time to useful output
need separately timestamped process events before changing worker configuration.

## Changes and invalidation rules

1. `Factory.queue_cards` constructs a per-observation set of safe completed
   records under the state mutex. `pickup.cards` still enumerates every live
   membership and validates identity, repository and state before omitting
   details. Open/reopened cards always receive details. Explicit selections
   retain full diagnostics. Claims, occupied work, uncertain activity, incomplete
   completion journals/dispatches, handoffs, available recovery grants and
   release cards are excluded from the optimization.
2. `GitHub.activity_snapshot` uses the complete paginated field inventory instead
   of full dashboard presentation. Field identities/types and target values are
   refreshed at each boundary. Duplicate/incomplete inventories, replacement
   fields, human edits and lost responses still halt without repeating writes.

This introduces **no verification or review cache**. The only omitted work is
unneeded detail/presentation retrieval. The omission hint is recomputed from
current local state plus current remote membership, never persisted as authority.
There is no TTL-based reuse across task/config/branch changes. Restart without
saved completion performs full reads. Reopening never refunds attempts or
permits automatic duplicate dispatch.

Do not reuse ordinary full-suite receipts across candidate checkouts yet:
ordinary receipts bind head/base/task/config/commands and log existence, but do
not capture a complete environment/toolchain fingerprint or immutable log hash.
Release-specific artifact/log hashes cover a narrower package contract. Broad
reuse would need explicit per-command environment/worktree dependencies,
content hashes, expiry/externally stateful-check policy and immutable references
to the original execution. Legacy or incomplete receipts must miss that cache.
The roughly 522-second final check is an opportunity, not established waste that
can safely be removed now. No old receipt was relabeled as a new execution.

## Ranked remaining work

| Rank | Finding and customer impact | Measured opportunity | Effort / risk | Disposition |
| ---: | --- | --- | --- | --- |
| 1 | Historical ticket details slow every lifetime poll | 44 fewer calls and 20.0 s saved per live scan | Small / bounded eligibility risk | Implemented and tested |
| 2 | Activity reporting fetches unused dashboard presentation | 50% lower metadata-query cost in observed layout | Small / low, fresh fields preserved | Implemented and tested |
| 3 | Quota exhaustion pauses the observer with generic recovery | Actual #83 pause; release also interrupted | Medium / high around uncertain writes | Next priority |
| 4 | Release effects repeat wide identity/card/field reads | 15 member intents span 8 m 39 s | Medium / high write-authority risk | Profile read groups and consolidate within each boundary, never across writes |
| 5 | Scheduler reads the queue twice around reconciliation | Two full observations per planning cycle | Medium / race/recovery risk | Return explicit mutation/readback outcomes before safely removing either read |
| 6 | Candidate verification repeats a full suite | 522 s on the observed final gate | Medium-large / high evidence risk | Establish environment-aware immutable evidence first |
| 7 | Inherited worker startup cost is poorly attributed | 3.4–4.2 s typical diagnostic delay, one 236 s outlier | Medium / capability-loss risk | Add timing evidence before reducing tools/plugins |
| 8 | Occupied-slot explanation incorrectly says the issue has not dispatched | Source-confirmed text mapping in `reasons.py`; no time-saving claim | Small / low | Distinguish actual execution from waiting capacity; separate presentation fix |

Concrete next priority: handle **confirmed read-only rate-limit exhaustion** with
typed error/reset metadata and bounded, cancellation-aware waiting inside the
existing authorized session. Preserve its original deadline, poll/attempt
spending and configuration. Do not automatically retry mutations or clear an
uncertain claim/activity/publication journal, and do not treat permission/network
errors as rate limits. Test reset boundaries, concurrent active work, stop and
authority drift before enabling automatic recovery. This is not implemented here.

Shared-file mentions are not a runtime lock in the current scheduler. Independent
worktrees overlap already; actual base advancement invalidates evidence and
merges/publication share the supervisor's serialization. No dependency was
deleted merely because it mentioned a common file. Broadening lock scope or
removing real source conflicts would not improve safety or quality.

## Verification and delivery boundary

Nine new focused tests cover scale, fresh reopen detection without re-dispatch
or spending resets, explicit selection, required recovery reads, identity/state
and pagination failures, no cross-instance omission reuse, field replacement,
field pagination/type errors and lost-write restart behavior. Existing concurrency,
duplicate-ownership, rolling-priority, timeout isolation, SHA/base/config/task
invalidation and release interruption tests remain part of the required suite.

The project configuration, installed plugin and original dirty maintainer source
are unchanged. The two-worker ceiling and 800-line automatic limit are unchanged.
No commit, PR, merge, installation or release is implied by local verification.
Verification completed: `python3 -m unittest discover -s tests -v` passed all
681 tests (including nine new tests) in 481.590 seconds; plugin validation passed
for all nine skills, and `git diff --check` passed. Suite runtime is recorded as
test evidence, not claimed as a suite speed improvement. Local checks are not
independent model QA/security/review or native installation acceptance for this
change. Final results and file hashes are retained in the local audit receipts.
