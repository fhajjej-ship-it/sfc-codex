# SFC 0.1.2 dashboard onboarding verification

Observed November 5, 2026.

The developer-maintenance instruction placed in the runtime skill in this
version was a scope error. Version 0.1.3 removes it from normal plugin operation;
maintainer guidance remains in the source repository's AGENTS.md.

This update includes the dashboard onboarding behavior and the instruction to
carry accepted SFC improvements into its source, package and installed copy.

`python3 -m unittest discover -s tests -v`: **49 tests passed**.
`python3 scripts/validate_plugin.py`: passed.

The dashboard fixture tests new onboarding and explicit repair through fake
GitHub adapters. They verify the seven default colors and emoji descriptions,
unchanged status IDs/names, preserved extra options and existing views,
read-only repeat application, rejection of ambiguous/truncated inventories,
retained configuration after partial failure and no automatic mutation retries.
CLI completion stays false (dashboard exit 2) until the saved grouping matches.

An independent forward test found that a customized view still named `View 1`
could be overwritten. Setup now creates a dedicated Factory view whenever no
named Factory view exists. A regression covers existing board, table and roadmap
views with that default name.

Read-only queries against the existing SFC project verified the public schema
and saved dashboard data. The new dashboard command reported all four checks
true: stage style, stage order, Factory board view and saved columns. Doctor
reported `onboarding_complete: true` for that existing board.

The current public view mutation cannot set Column by. The bundled onboarding
skill therefore performs and saves that step with browser tools, reopens the
returned view URL and requires a passing dashboard check. An unavailable browser
remains an explicit pending setup step.

No new live project was created to test this change, and no real issues/cards
were mutated by tests. Fake adapters do not establish general API behavior or
model quality. The core factory was not started as part of this update.
