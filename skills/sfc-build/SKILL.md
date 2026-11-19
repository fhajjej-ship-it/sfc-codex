---
name: sfc-build
description: Implement one software-factory issue in an isolated Codex worktree, using its acceptance criteria and prior QA findings. Used by SFC's builder lane or for a specifically selected issue.
---

# SFC Build

Read applicable AGENTS.md files, the issue and existing architecture/checks.
Validate that acceptance criteria describe observable behavior. If a missing
decision prevents a correct implementation, report blocked with that decision.

Implement the smallest complete change for the selected issue. Preserve unrelated
work. Use project-owned tools and conventions; do not add a framework or helper
skill because it happens to be installed. Reproduce a bug before fixing it when
possible, and use meaningful risk-matched tests. Address prior QA/security/review
findings and explain which evidence resolves each one.

For a supervisor-validated release workflow, the supervisor packages the pinned
merged source using target configuration; it does not dispatch Build to rewrite
fixes or manufacture a release implementation PR. A task calling itself a release
does not establish this workflow or grant publication authority.
If the supervisor has prepared a conflicting base merge, resolve its conflict
markers in the assigned worktree; the supervisor stages and commits that merge.

In a supervised build, implement the change, reproduce relevant failures and run
meaningful focused checks as needed. The supervisor owns full execution of the
configured project verification commands before independent QA/security. At the
final merge candidate it revalidates immutable evidence and reruns checks ineligible
for reuse. Do not routinely execute those commands merely
to duplicate the supervisor's upcoming run. Project instructions remain
authoritative: honor explicit builder-specific or full-suite requirements, or a
full run needed to reproduce the selected defect, and explain the reason.
Repository-wide completion requirements are fulfilled by actual supervisor checks
before overall factory completion; this does not override explicit builder-specific
requirements. This is a workflow contract, not a sandbox or proof that a worker
cannot execute arbitrary commands. Standalone use retains the project's and user's
existing verification obligations.

In a supervisor lane, stay in the assigned worktree. Do not commit, push, switch
branches, create PRs, move cards, deploy, migrate a live database, read credentials,
or change factory controls. The supervisor owns those actions. If project tooling
cannot run under the assigned permissions, report the actual limitation.

Inspect your final diff for omissions, accidental files and credential literals.
Return JSON matching the supplied output schema: verdict, summary, findings,
evidence. Evidence should cite changed behavior, files and checks actually run.
Include a nonblank summary and one truthful, concrete `evidence` text string.
Put files, observed behavior and actual checks in that string, using paragraphs
as needed. Do not return an evidence array or pad unused entries. Empty or
whitespace-only evidence is invalid for pass, fail and blocked alike.
Distinguish focused builder checks actually run from deferred supervisor-owned full
verification. Never claim deferred checks passed.
`pass` means implementation is ready for independent QA; it does not establish
overall factory completion, a merged PR or verified production behavior.
Standalone use follows the user's existing authority for commit/push rather than
assuming factory authority.
