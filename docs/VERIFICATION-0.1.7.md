# SFC 0.1.7 local verification

Observed November 7, 2026, in the isolated issue-16 worktree. This is build-worker
verification with temporary git repositories and fake GitHub/Codex transports.
It does not claim live board, browser, customer, deployment or model verification.

- `python3 -m unittest discover -s tests -v`: **169 tests passed**.
- `python3 scripts/validate_plugin.py`: passed for **0.1.7**, all nine skills.
- `git diff --check`: passed.
- `python3 scripts/package_plugin.py`: builds the local `dist/sfc-0.1.7.zip`
  candidate and adjacent SHA-256 file, without installing or publishing.

`tests/test_priority.py` adds 31 tests of the actual setup/creation functions and
CLI, rather than a scripted scenario replay. Coverage includes new onboarding,
idempotent repair, saved visibility preservation, full inventories (fields,
views, visible fields, grouping and item values), explicit custom mappings,
duplicate/conflicting fields/options, retained manual values and user choices,
missing/invalid assessment, origin/identity/body/status/Priority readback,
partial/lost writes, ambiguous identity recovery, durable failure receipts and
no duplicate retry. Read-only status/doctor/dashboard do not repair a board.
The existing factory, activity, pinning, check, finite-limit and merge-policy
tests also pass. The older prioritization replay remains labeled offline
interactive-decision evidence; its reference digest was refreshed, and its
hypothetical inputs/results are unchanged. Development issue #15 was not edited.

Source skills now route creation through the supervisor command and shared
rubric automatically. A prose assessment is insufficient to complete creation.
The runtime validates required setup/assessment, journals before writes, enrolls
the actual issue, sets explicit Backlog/Priority and verifies identity/body/values.
Existing boards require explicit setup; custom meanings require explicit IDs.
Receipts retain partial completion and known/recovered identities without blind
retries. GitHub has no atomic compare-and-swap: observable concurrent edits are
preserved or reported, with the narrow read/write race documented.

Prior QA defects were reproduced before repair. A matching manual Priority cleared
between snapshots now remains cleared and creation is incomplete. Routing checks
now compare fresh full inventories with pinned status field/option-ID meanings,
including Backlog. Tests use production `GitHub.metadata()` with fake transports
and exercise renamed/swapped meanings before issue creation, enrollment, field
writes and completion, plus changed/duplicate routing fields/options and setup
write boundaries. All five added regression tests pass in the full suite.

This worker did not operate a live board, create a real issue/PR, change an
installation, commit, push, merge or deploy. Independent QA, security and final
review remain the supervisor's next stages, stopping at an unmerged PR as scoped.
