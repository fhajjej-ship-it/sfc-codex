# SFC 0.1.9 local build verification

Observed November 8, 2026, in the isolated issue-20 worktree based on
`b3eaf28f5e5549d116cc680c0f7e9929ff82ff76`. These are build-worker checks,
not independent QA/security/final review or live GitHub merge evidence.

- `python3 -m unittest discover -s tests -v`: **235 tests passed**, including
  **38 new local-merge tests**. Final run: 69.092 seconds.
- `python3 scripts/validate_plugin.py`: passed for **0.1.9**, all nine skills.
- `git diff --check`: passed.
- `python3 scripts/package_plugin.py`: local archive generation. ZIP CRC,
  SHA-256 sidecar, unique/exact file inventory and source-byte equality are
  checked separately; no installation or publication is part of packaging.

The tests use temporary repositories, bare remotes and real Git transactions,
with fake GitHub/Codex adapters. They exercise:

- Explicit project mode/policy, separate missing-check choice, CLI defaults and
  setup output, unchanged legacy configuration and run authority.
- Actual candidate verification, clean worktrees, ancestor/tree pins and each
  failed independent lane; changed head/base/task/config pins reject merging.
- An atomic compare-only head plus expected-base fast-forward. Head and base
  races immediately before the simulated server transaction change no other
  refs. Adapter tests assert both beforeOid values and force:false in one
  GraphQL mutation. There is no head-only merge or push fallback.
- Missing/pending/failed checks, complete paginated check/status inventories,
  unsupported capabilities/permissions, sensitive labels/paths, human-only,
  size limits and sensitive old filenames hidden by a rename.
- Two-card merge serialization, base integration and fresh independent lanes.
- Lost responses, retained intent after stop, partial remote state, delayed PR
  recognition, an open issue, manual card movement and missing Done readback.
  Restart reads actual state and never resends a ref mutation or uncertain
  board transition. Current base refs, rather than historical PR metadata, establish reachability.
- Protected/ruled refs and incomplete identity/schema inventories halt. An
  external merge without a local journal cannot use legacy Done reconciliation.

The [protocol and limits](LOCAL-MERGE.md) cite GitHub's documented atomic
updateRefs, RefUpdate and indirect-merge contracts. Tests emulate the documented
server transaction with Git's atomic ref transaction; they do not establish
live GitHub behavior, private-account permissions, asynchronous PR recognition
or real-model correctness. Unsupported live capabilities must halt.

No real issue, board, PR, branch, deployment or installed plugin was changed.
No agent was spawned and no worker was recursively dispatched. HEAD remains the
supplied base commit. The supervisor owns committing the reviewed scope and
independent QA, security and final review. This build is ready for those lanes;
no merge is authorized or performed by this receipt.
