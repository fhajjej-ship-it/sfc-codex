# SFC 0.1.10 integrated local build verification

Observed November 8, 2026, in the isolated issue-22 worktree at supplied head
`6f8ad7c6132208615265092aa186f50093cea661`, resolving the supervisor-prepared
merge of main `489c3b3b323696d32ae05a132dc128a17311837f` (merged PR #21).
These are build-worker checks on the combined working tree. The supervisor owns
staging/committing the merge and repeating pinned independent QA, security and
final review before any authorized merge.

- `python3 -m unittest discover -s tests -v`: **267 tests passed**.
- `python3 scripts/validate_plugin.py`: passed for **0.1.10**, all nine skills.
- `git diff HEAD --check`: passed; conflict markers cleared in working files.
- `python3 scripts/package_plugin.py`: local ZIP only; exact inventory,
  CRC, SHA-256 sidecar and byte-for-byte source identity verified separately.

The combined suite preserves the priority scheduler/claim and atomic local merge
coverage, using temporary repositories, local bare remotes, fake GitHub transport
and fake Codex lanes. Four added cross-feature tests in `tests/test_local_merge.py`
cover scoped Backlog pickup through all independent lanes and exact fast-forward,
human merge policy even with run merge authority, separate run merge authority
even with auto policy, and lost merge-response restart with changed run scope.
The last case verifies read-only preview blocks dispatch, execution makes no
out-of-scope recovery writes, and restoring the authorized scope finishes by
readback without a duplicate ref mutation/worker or reset claims/attempts.
The scope-preview regression failed before the integration guard was added.

Existing reopened-prerequisite recovery regressions in `tests/test_pickup.py`
pass, including reopening before restart and between planning and dispatch.
Default Ready-only behavior, QA-only routing, ordering/capacity, custom Priority
meanings, complete pagination, concurrent claim changes, partial/lost claim
responses, finite limits, worktree isolation, task/config/head/base evidence,
missing-check handling, sensitive/large-change policy and merge gates remain
covered. Human merge remains the default. Neither Priority nor pickup opts a
project into auto merge or broadens user scope.

Raw full-suite and validator logs, package output and a source-identity receipt
are preserved in the private maintainer evidence archive. Package output remains
`dist/sfc-0.1.10.zip` with its SHA-256 sidecar. Packaging does not install the
plugin. GitHub Project fields still lack atomic CAS; preflight/readback cannot
close the documented narrow race or provide transactional multi-query reads.

No live GitHub operations, queue/configuration changes, global installation,
deployment, migration, recursive factory run or extra agent were performed.
No live-board or real-model verification is claimed. The build worker leaves
the supervisor's merge index for supervisor staging and committing; resolving
working files does not itself complete the Git merge.
