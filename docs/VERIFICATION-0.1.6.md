# SFC 0.1.6 build verification

Observed November 7, 2026 in the isolated issue-13 build worktree.

- `python3 -m unittest discover -s tests -v`: 131 tests passed.
- `python3 scripts/validate_plugin.py`: valid, version 0.1.6, nine skills.
- `git diff --check`: passed.

The 27 activity-specific tests in `tests/test_activity.py` cover controlled-clock
60-second refresh, coalesced substeps, independent simultaneous cards and
outcomes, all six agent states, human merge handoff, dependency waiting,
failed checks, malformed workers, finite limits, stale/Unknown reconciliation,
interrupted/partial/failed writes with restart latches, human-edit preservation,
field/item/issue/repository/project identity, text privacy, bounded JSONL parsing,
and explicit saved-view upgrade/readback. A local child-process experiment
verifies stop and timeout enforcement while a reporting callback is blocked.
All GitHub/Codex behavior uses fake adapters; git fixtures use temporary repos.
The full existing pinning, missing-check, permission and merge-gate suite passes.

Prior QA defects were reproduced before the fixes: an omitted multi-select union
fragment caused `KeyError('name')`, and a between-cycle stop left a selected card
Waiting for CI. Regression tests now cover multi-select inventory/visibility
repair and preservation, between-cycle stop reporting without changing completed
cards, blocked handoffs or unselected work, failed stop-report writes halting
without retries, and stopping before any selection. The full-suite log for this
build revision is retained in the private maintainer evidence archive.

`python3 scripts/package_plugin.py` creates the local 0.1.6 archive, including
`scripts/sfc_lib/activity.py`, `docs/ACTIVITY.md` and `tests/test_activity.py`.
Packaging does not install, publish or operate a board. Existing installations
need the explicit upgrade described in `docs/ACTIVITY.md`.

No live board/browser visibility check, paid model invocation, independent QA,
security or final-review lane was performed by this build worker. Those remain
supervisor-owned validation steps. No commit, push, PR, merge, installation,
deployment or board write was performed.
