# Project chat auto-start (0.1.12, unreleased)

This page specifies the preserved **legacy bounded mode**. New onboarding
defaults to [interactive session lifetime](SESSION-RUNTIME.md); use
`onboard --no-session-lifetime` to choose this bounded mode, or `--no-auto-start`
to leave watching off. Existing authorizations are never migrated.
That mode uses short attachment hooks and a Codex-owned local STDIO MCP runtime.

Choose **Start SFC automatically when a project chat starts or resumes in
Codex** for legacy bounded setup. Existing/unset authorization stays off; new
onboarding uses the lifetime default described above. Opening the application window
alone is not a supported trigger. Onboarding saves authorization and never
launches work itself. Existing projects/choices are preserved.

Before choosing, inspect the actual repository/board, session duration,
exclusions/hold label, finite budgets and merge policy. Auto-start authorizes
current and future eligible Backlog/Ready tickets on that project, including
worker usage and the usual supervisor board/PR writes. It grants no authority
for other projects or automatic merges.

```sh
python3 "<plugin>/scripts/sfc.py" --root "<repo>" autostart
python3 "<plugin>/scripts/sfc.py" --root "<repo>" autostart --enable
python3 "<plugin>/scripts/sfc.py" --root "<repo>" autostart-status
python3 "<plugin>/scripts/sfc.py" --root "<repo>" watch-status
python3 "<plugin>/scripts/sfc.py" --root "<repo>" stop
python3 "<plugin>/scripts/sfc.py" --root "<repo>" autostart --disable
```

Disable/reconfigure after stopping the supervisor; configuration writes require
the same common lock. Set `watch` defaults in `.sfc/config.json` only while
stopped, preview, then explicitly enable to authorize that exact snapshot.
Onboarding accepts `--auto-start` and optional `--watch-max-seconds`,
`--watch-poll-interval`, `--watch-max-polls`, `--watch-max-claims`, repeated
`--watch-exclude-issue` and `--watch-hold-label`. Unfinished dashboard setup
cannot enable the feature; finish setup and explicitly enable afterward.

Enabling alone stores `allow_merge: false`, including on an `auto` project.
Only separately requested `autostart --enable --allow-merge` stores automatic
merge authority, and configured policy must also be `auto`. The 400-line default
cap, two-attempt default, sensitive-change policy, all independent lanes,
supervisor checks, missing-check gates and SHA/base/config/task pins still apply.
Changing any configuration requires fresh explicit authorization.

## Trust and lifetime

Review the bundled SFC hook in Codex `/hooks` or **Settings > Hooks**. SFC never
edits global user configuration or supplies a hook-trust bypass flag. A local
authorization receipt is separate from Codex's review of the installed hook.
No event-supplied `trusted` boolean grants authority. If Codex skips a disabled
or untrusted hook, SFC cannot observe that invocation: `autostart-status` explains
where to review hooks, and old receipts do not prove current host trust.

The supported [SessionStart](https://learn.chatgpt.com/docs/hooks#sessionstart)
sources are `startup`/`resume`; compact/clear/fork, plan mode, unknown/missing
permission modes and child agent events are ignored. Hook trust is independent
of a user's permission mode; `dontAsk` does not itself prove a headless child.
All SFC-owned child
processes, including project checks, inherit `SFC_WORKER=1`, suppressing recursive
headless worker hooks. This is a lifecycle guard, not a hostile-code sandbox.

The [background command hook](https://learn.chatgpt.com/docs/hooks#run-hooks-in-the-background)
executes the existing watcher in the hook process. Codex owns that process;
there is no detached supervisor, cron job, login service or stored-PID signalling.
The hook's finite 86760-second timeout covers the maximum 86400-second watcher,
its original 30-second completion-only allowance and setup/cleanup time. Codex
cancels unfinished background hooks when their session ends. Cooperative
SIGINT/SIGTERM cancellation requests stop the owned children and retains receipts.
Network IO keeps its finite timeouts. Abrupt killing can leave uncertain state,
which fails closed for manual inspection. Switching chats alone does not
necessarily end the host session. The computer must stay awake and connected.

## Duplicate events, restart and recovery

Admission and final receipts are written under the existing common-git
supervisor lock, held throughout watcher execution. Other chats and linked
worktrees report occupied/waiting; they cannot create competing supervisors.
Resolve the authorized actual checkout using its common git directory, including
subdirectory entry and separate git directories. Incidental linked-worktree
configuration does not replace the authorized checkout.

Each hashed Codex session ID may admit one watcher budget per explicit
authorization. Repeated startup/resume events cannot renew it. After cooperative
host cancellation, a later eligible event resumes the same watcher ID, deadline,
scope, merge opt-in and spent counters, including offline time. Expiry only
permits the existing exact completion-only reconciliation; it never admits work.
After normal time/poll/claim/cycle exhaustion, a previously unseen Codex chat ID
may start a genuinely new bounded watcher session. Prior per-card claims,
attempts, journals and archived watcher sessions remain intact. An already seen
chat cannot obtain another budget by starting/resuming again.

An operator stop, failure, no-progress limit, incomplete start intent or a manual
watcher replacing the saved session requires inspection and explicit
`autostart --enable` to re-arm. It never automatically retries. Re-arming archives
the prior authorization and acknowledges/clears existing stop flags under lock;
it does not reset claims, attempts or merge journals. An unfinished manual
watcher requires manual recovery first. Missing/drifted configuration or board
identity/readiness fails closed before claims or paid workers. Normal manual
`run`/`watch` selection, finite limits and merge gates remain unchanged.

Local common-git files: `sfc-autostart.json` (authorization/intent/chat history),
`sfc-autostart-history/<authorization-id>-<change-id>.json` (retained choice snapshots),
`sfc-autostart-status.json` (last observed hook result) and the existing
`sfc-watch.json`. The configured checkout retains watcher archives, claims,
attempt history, run logs and worktrees. Recorded status is not proof of liveness.

Deterministic fake-host/temporary-repository tests exercise the launcher and real
watcher path. Actual Codex startup, hook review and cancellation delivery still
need a separate maintainer smoke check after reviewed integration. They are not
proven by a fake replay. This change does not enable the development project,
alter excluded preview issue #15, install a plugin, or publish a release.
