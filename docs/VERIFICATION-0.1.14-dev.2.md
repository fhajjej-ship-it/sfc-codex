# 0.1.14-dev.2 corrective builder verification

Issue #70, November 9, 2026. This candidate addresses independent review's
finite-run admission finding against `2022eba6579cf8ffcc4749833637a25632791b54`.
It is a local builder result, not factory completion, installation or release.

## Reproduction and change

Before the fix, deterministic Ready-only Priority-change and scoped Backlog
stage-change execution tests both failed: the selected P1 ticket changed during
admission, and the unchanged eligible P2 peer never launched (`[]` instead of
`[2]`). The scheduler ended the entire idle finite run at that rejection.

Finite runs now defer only the rejected ticket until a later invocation and
immediately replan the remaining queue. Each ticket can be rejected only once
per finite invocation, preventing a revalidation spin without launching paid
work. Waiting receipts explain the deferral. Current scope, recovery precedence,
claim uncertainty handling, worker capacity and existing limits are unchanged.
Rejections before intent spend no cycle; rejected claim preflights retain the
already reserved cycle. No allowance or deadline is refunded or enlarged.

## Checks actually executed

- `PYTHONPATH=scripts:tests python3 -m unittest test_scheduler test_pickup -v`:
  63 tests passed (18 scheduler and 45 pickup tests).
- The finite churn/later-run test passed again after adding explicit cleanup of
  invocation-local deferrals, including its new empty-deferral assertion.
- `PYTHONPATH=scripts:tests python3 -m unittest test_watch test_autostart.HookContractTests.test_next_unreleased_version_is_consistent -v`:
  all 63 tests passed after the diagnostic assertion correction described below.
- `python3 scripts/validate_plugin.py`: passed for `0.1.14-dev.2`, nine skills.
- `git diff --check`: passed.

The new tests execute actual admission with fake GitHub/Codex adapters and
temporary repositories: Priority/stage changes in both finite modes, changes
during claim revalidation, bounded repeated edits, a later invocation rechecking
deferred tickets, and ticket creation becoming unverified after planning. They
assert peer execution, unchanged scope, no claim on the rejected ticket and
retained admission spending. The existing resumed-prerequisite test now permits
the additional replan while retaining its no-worker/no-remote-write assertions.

The first watch/version run exposed a pre-existing race in the hold-confirmation
test's diagnostic expectation. Repeating that test 12 times with the original
HEAD scheduler reproduced the same mismatch once: either the worker's watcher
policy guard or the supervisor's refreshed scope guard may detect the hold
first. The test now accepts those two specific halt diagnostics and additionally
checks that the hold and unfinished journal remain. Its no-Done-write and
single-ref-attempt assertions remain; no production safety guard was changed.

Focused logs and the development-package byte/hash comparison receipt are
retained in the private maintainer evidence archive. Packaging is local only;
installed caches remain read-only.

The configured full `python3 -m unittest discover -s tests -v` is deferred to
the supervisor under the supervised-build verification contract. The supervisor
must run configured checks before independent QA/security/review and repeat
them at the final merge candidate. No deferred check is claimed as passed here.
