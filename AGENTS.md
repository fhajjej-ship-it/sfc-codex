# SFC

SFC is a Codex-only software factory plugin.
Read docs/DESIGN.md before changing orchestration or merge behavior.

Edit the plugin source in this repository and keep changes scoped.
Normal factory use runs the bundled workflows against the target repository
and keeps the installed plugin's code and skills read-only. Keep project IDs,
credentials and run state in project configuration. Edit the source, never an
installed cache.

Use Python 3.10+ standard library for the runtime. Keep GitHub writes in the
supervisor, never in worker prompts. Tests must use temporary repositories and
fake GitHub/Codex adapters; never mutate a real project while testing.

For standalone maintainer changes, run the full suite with
`python3 tests/run_suite.py` and run `python3 scripts/validate_plugin.py`
before reporting completion. During a supervised SFC Build, run focused checks
with `python3 -B` as needed and report them separately. The supervisor owns
complete project verification before QA and at merge and release.
Preserve receipts and worktrees on failure. Never weaken SHA/base pinning,
missing-check handling, finite run limits, or merge authority to get a test green.
Do not commit, push, publish, install globally, or run a customer board without
the user's request.
