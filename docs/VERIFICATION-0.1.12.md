# 0.1.12 focused builder evidence — unreleased

Issue #52 adds explicit per-project chat auto-start. No installation, release,
real board run, development-project opt-in or preview issue #15 change was made.
Published 0.1.11 evidence/assets remain unchanged.

Checks actually run with temporary repositories and fake GitHub/Codex adapters:

- `python3 -m unittest discover -s tests -p test_autostart.py -v`: 22 passed.
  Exercises the launcher and existing watcher path: startup/resume, skipped
  fake-host dispatch, off/disabled choices, child markers, simultaneous chats,
  linked/separate git directories, cancellation with an actual owned child,
  original budgets/expiry, operator stops including the admission race, config
  and board drift, uncertain starts, manual watcher choices and merge authority.
- `python3 -m unittest discover -s tests -p test_dashboard.py -v`: 19 passed,
  including enabled/default-off onboarding and incomplete-setup refusal.
- `python3 -m unittest discover -s tests -p test_watch.py -v`: 61 passed,
  including completion-only cutoffs, attempt limits and existing merge gates.
- Focused `test_factory.ProcessTests`, `test_factory.HookTests` and two
  real-process/slow-observer cases in `test_activity.ActivityTests`: 8 passed.
- `git diff --check`: passed.

The supervised builder contract defers the configured full suite
(`python3 -m unittest discover -s tests -v`) and package validation
(`python3 scripts/validate_plugin.py`) to the supervisor before independent lanes
and again at the final merge candidate. Neither is claimed passed here.
Independent QA, security and final review remain required. The change exceeds
the unchanged 400-line automatic-merge cap and needs its existing human handoff;
the two-attempt setting and all other merge/verification gates remain intact.

Actual Codex startup/resume, hook review and host cancellation delivery require
the separate post-integration maintainer smoke check described in
[the lifecycle contract](AUTOSTART.md). Fake-host replays do not prove those
live host behaviors. Builder pass means ready for independent QA.
