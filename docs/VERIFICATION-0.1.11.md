# SFC 0.1.11 local build verification

Observed November 8, 2026 in the isolated issue-28 worktree, based on
`35ee4281a2041086ea41d74f76763cde07a1cac8`. This is build-worker evidence for
independent review, not a live watcher, PR, installation or release receipt.

## Executed checks

- `python3 -m unittest discover -s tests -v`: **303 tests passed** (89.683 seconds).
- `python3 scripts/validate_plugin.py`: passed, version **0.1.11**, all nine skills.
- `git diff --check`: passed.
- `python3 scripts/sfc.py --version`: **0.1.11**; `watch --help` exposes the
  documented explicit start/resume, preview, scope and budget options.

The new `tests/test_watch.py` provides 36 temporary-repository/fake-adapter tests.
Injected clocks/waits advance idle/session/confirmation time without real-minute
sleeps. The active-stop case uses an actual owned local Python child and stops
it promptly; GitHub and Codex remain fake. The saved-claim ordering test uses one
worker to assert ordering without depending on concurrent thread scheduling.

Coverage includes empty-to-arriving and later-priority queues; priority/age/number
ties; holds/exclusions and their removal; closed and reopened prerequisites;
unchanged defaults; idle without worker/attempt/failure-wave spending; finite
poll/time/claim/cycle/failure budgets and spent resume; conservative dispatch and
claim spending across interruption; archive-copy failure without restored budget;
verified-claim resume without duplicate claim; stale selection revalidation;
human handoff plus later independent work; shared linked-checkout run/watch lock;
read-only preview/status with honest freshness; idle/active stop and signal
handlers; protection from signalling a reaped/reused child PID; merge policy and
separate merge opt-in; authorized journal recovery; excluded/held/removed journals;
uncertain claim/ref/Done outcomes; bounded acknowledged PR or issue recognition
latency with one atomic ref transaction; and old-base snapshots refusing that
recognition window. Existing merge/security/check/worker-isolation tests remain
part of the full passing suite.

## CI-wait correction

The follow-up build at `78f4020261d6ed70657f8adbf23c76af4a106dfe` reproduced
premature `no-progress-limit` shutdown with two new regression tests before the
fix. After the fix, `python3 -m unittest discover -s tests -v` passed **307 tests**
in **87.842 seconds**; plugin validation and `git diff --check` also passed.

Four added watcher tests verify pinned CI waits spend no worker/cycle/failure
budget or remote writes, an independent arrival runs with one worker before
delayed CI success resumes the original review, preview stays read-only and
missing merge authority still produces a human handoff, and stale review evidence
still dispatches the required review. The watcher suite now contains 40 tests.
The normal merge gates are retained; the arrival test uses a fake merge gate,
not a live merge. The full-suite log is retained in the private maintainer evidence archive.

## Package and limits

`python3 scripts/package_plugin.py` creates `dist/sfc-0.1.11.zip` and its SHA-256
sidecar. The local build receipt verifies ZIP integrity, exact source file set,
byte-for-byte equality of every member and the sidecar. Packaging performs no
installation or publication.

Raw full-suite and validator logs, focused watcher evidence, package output and
source-identity details are retained in the private maintainer evidence archive. Runtime policy and restart limitations are documented in
[WATCH.md](WATCH.md), with shared merge details in [LOCAL-MERGE.md](LOCAL-MERGE.md).

No live board, customer repository, paid Codex worker, global installation, OS
service or watcher was started. No commit, push, branch switch, PR/board mutation
or merge was performed. Supervisor-owned independent QA, security and final
review, then an unmerged implementation PR, remain the next gates.
