# SFC 0.1.8 local build verification

Observed November 8, 2026, in the isolated issue-18 worktree based on
`da08469e0431ea28d7890749989e52399865164d`. These are local implementation checks,
not independent QA/security/review or live-board evidence.

- `python3 -m unittest discover -s tests -v`: **197 tests passed**.
- `python3 scripts/validate_plugin.py`: passed for **0.1.8**, all nine skills.
- `git diff --check`: passed.
- `python3 tests/backlog_replay.py`: replayed 17 offline cases / 180 fake calls.
  The receipt's workflow hash was refreshed for the updated creation instructions;
  fixture hash and all recorded results remain unchanged.
- `python3 scripts/package_plugin.py`: local archive generation only. Package
  checks validate ZIP CRC, SHA-256 sidecar, exact file inventory (no duplicates)
  and byte-for-byte equality of every packaged file with source, including the
  new runtime, type policy and tests. No installation is part of packaging.

`tests/test_work_types.py` adds 26 tests using temporary repositories and fake
GitHub adapters through real setup/onboarding/CLI/creation functions. Coverage:

- All five explicit choices; every documented alias; bootstrap `enhancement`
  and `documentation` reuse with original names, colors and descriptions.
- Required type and setup before POST; explicit custom mappings and rejection
  of stale, duplicate, conflicting, unsupported and workflow/policy mappings.
- Paginated label discovery/readback, saved Labels visibility, idempotence,
  preserved auxiliary/QA choices, config, custom views and item values.
- Observable identity/name/description/color changes, manual type changes and
  clears, missing/extra readback, partial/lost issue/enrollment/field writes.
- Missing/lost setup writes, retained actual identities and raw failure
  observations, plus unfinished write-intent receipts after abrupt process exit.
- Repeated ticket IDs never write; incomplete/unfinished setup receipts prevent
  further mutations until inspected. Matching setup verification remains read-only.
- Status/doctor/dashboard/preflight never migrate missing type setup.

Existing Priority, full Project/view pagination, human-edit, duplicate-prevention,
factory lane, SHA/base/config/task pin, limit and merge-policy tests still pass.
Factory orchestration and merge-policy source are unchanged.

No real GitHub issue, Project, PR, deployment or installed plugin was changed.
No worker was recursively dispatched. The supervisor must commit the reviewed
scope and run independent QA, security and final review; this build result only
establishes readiness for those lanes. The eventual PR must remain unmerged.
