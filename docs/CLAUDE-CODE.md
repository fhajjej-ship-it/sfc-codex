# SFC for Claude Code

SFC chat tracking works in Claude Code as well as Codex. The Claude Code chat
implements, tests and reviews the work. SFC records each milestone on the
GitHub Project through the same `track` commands, local outbox and board
readback. The board, configuration, receipts and merge authority are shared;
a project needs no extra setting for Claude Code.

## Install

Build the Claude Code layout from a source checkout. There is no Claude
release asset yet.

```sh
python3 scripts/package_plugin.py --claude
```

This writes `dist/sfc-<version>-claude.zip` (plus `.sha256`) and an unpacked
`dist/sfc-<version>-claude/`. Neither overwrites an earlier export. Install from
the unpacked folder as a local marketplace:

```sh
claude plugin marketplace add "$PWD/dist/sfc-<version>-claude"
claude plugin install sfc@sfc-marketplace
```

To try it for one session without installing:

```sh
claude --plugin-dir "dist/sfc-<version>-claude"
```

Restart Claude Code, open the target repository and review the SFC hooks in
`/hooks`. Onboarding and setup are the same as in Codex; see
[chat tracking](CHAT-TRACKING.md).

## What the layout contains

Claude Code always loads `hooks/hooks.json` and `.mcp.json` from a plugin root,
and the Codex versions of those files aren't valid there. So the Claude layout
is built separately:

- `claude/.claude-plugin/plugin.json` and `marketplace.json` (plugin at `./`).
- `claude/hooks/hooks.json`, which becomes `hooks/hooks.json`. It runs
  `hooks/guard.py --host=claude` on `SessionStart` (startup and resume),
  `UserPromptSubmit`, and `PreToolUse` for `Bash|Edit|Write|MultiEdit|NotebookEdit`.
- The shared `skills/` (without the Codex `agents/openai.yaml` metadata),
  `scripts/`, `schemas/`, `hooks/guard.py`, `docs/` and `assets/`.

`scripts/validate_plugin.py` checks the overlay's version, hooks and marketplace
entry whenever `claude/` is present.

## How tracking attaches

The prompt hook gives the chat the same tracking instructions as on Codex, naming
Claude Code as the place to work. The chat prefix used by `track status --prefix`
comes from Claude Code's session ID, so follow-up prompts in the same chat find
its tasks. Claude Code's `UserPromptSubmit` event has no turn ID, so each prompt
gets a fresh random slot for new task IDs.

On Claude Code the hook only attaches chat tracking. It never requests the
Codex-native session MCP or starts a watcher. Subagent events, other
`SessionStart` sources (`clear`, `compact`), `SFC_WORKER` processes and folders
that aren't Git checkouts are ignored silently.

## Guards

The `PreToolUse` guard applies the same literal checks as on Codex to `Bash`:
credential paths, force or base-branch pushes, and deletions outside the
checkout. For `Edit`, `Write`, `MultiEdit` and `NotebookEdit` it allows targets
inside the session's working directory or the OS temporary folders (`$TMPDIR`,
Python's `tempfile.gettempdir()` and `/tmp`), where scratch files belong. A
temporary folder setting that is `/` or contains your home folder grants
nothing. Links are resolved first, so a link in a temporary folder can't reach
other locations. Everything else, including other repositories and your home
folder, is blocked. Credential files are blocked everywhere, and the written
text is scanned for live-looking secrets. These are mistake guards, not a security
sandbox.

## Limits

- `track merge --prepare` runs an independent native review with
  `codex exec review` and validates Codex's review log. It still needs the Codex
  CLI, including when called from a Claude Code chat. Without it, preparation
  halts with "Native review provider unavailable" and nothing is merged; use the
  repository's normal merge process or a human merge, then `track sync`.
- The legacy factory engine (`run`, `watch`, `codex exec` workers) and the
  Codex-native session MCP remain Codex-only.
