# SFC 0.1.0 verification

Observed November 4, 2026. These are local development results, not a live-board
or real-model accuracy claim.

## Runtime

`python3 -m unittest discover -s tests -v`: **36 tests passed**.

Tests use real temporary git repositories, bare remotes and linked worktrees,
with simulated GitHub/Codex adapters. Coverage includes:

- Human PR preparation and external-merge reconciliation.
- Automatic merge authority, CI and strict base-protection requirements.
- SHA/config/task pinning; changed PR heads, issue criteria and queue status.
- Moving base branches and conflicting-base recovery through a second build.
- QA-only assessments, with no manufactured implementation PR.
- Parallel preparation of seven independent human-review handoffs.
- Failure feedback and finite retry/no-progress limits.
- Malformed model output, project-check failures and preserved resume state.
- Stop at worker exit, during checks and after review; owned group termination.
- Shared-repository locking, read-only dry runs and dependency waits.
- Credential, path, worktree, deletion and protected-push literal guards.
- Origin repository identity, paginated PR files and GraphQL error handling.

An independent forward test of the packaged workflow found stop timing,
completion-accounting and task-freshness defects. Those defects were corrected
and the corresponding regression tests pass. That test used no live GitHub
operations or model calls.

## Packaging

- `python3 scripts/validate_plugin.py`: passed.
- All nine skills passed Skill Creator's `quick_validate.py`.
- Root `plugin.json` passed the declared Agent Plugins 1.0.0 JSON schema.
- Codex CLI **0.157.0** registered the local marketplace and installed
  `sfc@sfc-local` into a temporary isolated `CODEX_HOME`.
- `codex plugin list --marketplace sfc-local --json` reported version 0.1.0,
  installed and enabled.
- The installed plugin's Codex app-server `skills/list` returned all nine
  namespaced skills, from `sfc:sfc` to `sfc:sfc-visual`, at their installed paths.

The user's normal Codex installation/configuration was not changed. Hook trust
was not bypassed. No live issue, project, PR or deployment was modified; no paid
Codex workers were invoked. The source was not committed, pushed or published.

Before operating a real board, onboard the actual repository, run doctor/lint
and a dry run, then perform one explicitly authorized issue through the model
and GitHub path. Review actual logs and behavior before relying on unattended
operation. That operational validation remains unperformed in this release.
