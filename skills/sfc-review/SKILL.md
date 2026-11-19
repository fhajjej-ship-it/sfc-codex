---
name: sfc-review
description: Independently review an SFC change for correctness, acceptance-criteria gaps, regressions and weak evidence before the supervisor's merge gate.
---

# SFC Review

Form your own hypotheses from the issue and diff before relying on prior lane
summaries. Inspect the important callers, state transitions, failure handling
and tests. Look especially for plausible cases where the happy path passes but
the requested behavior still fails. Verify prior findings were resolved by the
current code instead of accepting a repeated claim.

Keep findings actionable: cite the triggering input/state, failing behavior,
effect and exact file/line. Distinguish bugs from personal design preferences.
Use small isolated experiments when needed. Do not edit code in this lane or
grow the issue into unrelated refactoring. If the change needs product, legal,
deployment or data decisions, identify the missing decision explicitly.

Assess whether the evidence actually supports the acceptance criteria. Missing
required runtime/visual evidence is a gap, not a pass. Prior QA, security and
project-command receipts belong to the supervisor and are pinned to commits;
you cannot waive them, grant merge authority or approve a different commit.
Distinguish original supervisor executions from immutable reuse references. Use the
supervisor's validated execution bindings and hashes. Independently assess what the
checks and their assertions demonstrate. Investigate concrete discrepancies in the
original evidence. Packaged releases and live required GitHub checks retain their
separate evidence requirements.

For supervisor-validated release cards, review the frozen version, complete
actual source range, unlinked changes, artifact identity/checksums, actual package
checks, fixed public notes and publication readiness. The manifest and artifact
checks are additional review subjects; source review alone cannot establish an
installable package. Check the separately scoped release policy. Your verdict
never authorizes draft/tag/asset/publication writes; only the supervisor performs
them and verifies published downloads before Done.

Return the supplied JSON schema with no unresolved findings on `pass`. The
supervisor checks policy, GitHub CI, base freshness and integration checks;
never invoke a merge or manufacture a GitHub approval from this verdict.
Include a nonblank summary and one truthful, concrete `evidence` text string.
For missing reproducible execution of configured supervisor checks, a Blocked
result with no findings may include `recovery: {"kind": "missing-check-evidence",
"check_ids": [1]}` using the numeric IDs in the supervisor's check evidence. The
supervisor validates and bounds recovery; this request grants no command, Build,
merge or publication authority. Other missing evidence still needs a clear reason.
Put files, observed behavior and actual checks in that string, using paragraphs
as needed. Do not return an evidence array or pad unused entries. Empty or
whitespace-only evidence is invalid for pass, fail and blocked alike.
