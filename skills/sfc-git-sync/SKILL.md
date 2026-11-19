---
name: sfc-git-sync
description: Review selected Git changes, commit logical units, integrate the remote branch without force, and push when the user requests SFC Git sync.
---

# SFC Git sync

Inspect status, current branch, upstream, diff and applicable instructions.
Identify the user's selected changes and preserve unrelated work. If a running
factory owns this worktree/branch, stop or finish that run before standalone sync.

A request to sync includes commit, pull and push for the selected work. A request
for a draft or review does not. Check for credentials, generated artifacts and
accidental deletions. Run relevant project checks before creating logical commits.

Fetch, inspect remote divergence, and integrate with a normal merge when needed;
do not rewrite shared history, force-push, discard local changes or bypass hooks.
Resolve conflicts only within the selected scope and recheck affected behavior.
Push only the intended branch to its verified upstream. Protected base branches
use a PR unless the user explicitly authorizes the permitted direct operation.

Report the commits, branch, push outcome and any unfinished conflict/check.
Verify remote status before calling synchronization complete.
