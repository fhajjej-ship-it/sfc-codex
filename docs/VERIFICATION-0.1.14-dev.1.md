# 0.1.14-dev.1 local build verification

Issue #70, checked November 9, 2026. This is a local build candidate, ready for
independent QA/security/review; it is not an installation, publication or live
board verification.

## Checks actually executed

- `python3 -m unittest discover -s tests -v`: 472 tests passed.
- `PYTHONPATH=scripts:tests python3 -m unittest test_scheduler -v`: all 10
  scheduler tests passed, including the strengthened two-review barrier test.
- Focused activity, lifecycle, explanations and ordinary merge-recognition
  regressions passed after the stop-report and failure-cutoff fixes.
- `python3 scripts/validate_plugin.py`: passed with version `0.1.14-dev.1` and
  all nine bundled skills.
- `git diff --check`: passed.

Scheduler tests use temporary repositories, fake GitHub/Codex adapters, injected
clocks and synchronization barriers. Actual admissions show saved P3 losing to
fresh P1; a new P0 starting while a slower peer remains busy in all three run
modes; no duplicate or over-capacity launch; manual holds, reopened prerequisites
and Priority changes being reread; and whole-card slot ownership. Concurrent
merges obtain fresh independent evidence after a peer advances the base.
CI waits release capacity without spending repeated model work. Concurrent work
cutoffs preserve original spending/deadlines and admit no replacement worker.
Preview checks observe occupied slots and the next eligible choice without writes.

Ready-only fixtures now provide verified Priority setup. Unclaimed invalid or
dependency-blocked tickets wait locally without remote annotations. Manual stage
movement cannot reset exhausted attempts. These are deliberate scheduling and
safety assertions, not fallbacks for missing setup or weakened execution limits.

The local archive is `dist/sfc-0.1.14-dev.1.zip`, with its SHA-256 sidecar.
Build logs and the archive comparison receipt are retained in the private
maintainer evidence archive. Installed caches remain read-only.
The supervisor still owns independent QA/security/final review and final-candidate
project verification before any merge.
