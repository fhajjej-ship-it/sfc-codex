# Interactive session runtime — 0.1.15-dev.6 development candidate

This mode, the default for new onboarding, observes the configured project throughout its native loaded
Codex chat/MCP connection, including idle time between model turns. It uses a
bundled Python 3.10+ standard-library local STDIO MCP server, not a timed watcher
hook, hosted server, OS daemon, cron job or another app-server subscription.
The candidate is unpublished. Native packaged acceptance remains
a separate post-integration step; installation is not activation.

### Current desktop packaging

Codex 0.167.0 and the app's bundled 0.167.0-alpha.3.2 did not discover hooks
from the portable root manifest during native acceptance. Their compatibility
loader does discover the packaged hooks, but does not expand `${PLUGIN_ROOT}`
in legacy MCP arguments. From an extracted package, explicitly run
`python3 scripts/install_codex_plugin.py` for a fresh install or upgrade.
It supports an empty profile without requiring an existing SFC installation.
The maintainer-only `scripts/install_development_plugin.py` retains its strict
existing-enabled-development-install precondition. Both export compatibility format and
resolves that argument from the native installer's reported location, with
no `cwd` override. It never edits a cache, trusts hooks or enables a project.
Stop the configured maintainer factory before updating: the installer now holds
the common supervisor lock and refuses an active runner before replacement.
Reload already loaded chats after updating so they use the new runtime paths.
Keep the extracted installation source and its versioned journal; Codex registers
that source. Installer failures latch their phase for inspection, without blind
reinstallation or cache rewriting. Explicit disabled choices stay disabled.
The host-resolved native artifact is derived from the unchanged portable archive
by selecting the compatibility manifest and resolving only its MCP argument.
Record both hashes; a source ZIP alone is not proof of native lifecycle support.

To return to a supported prior package, stop the factory, verify that prior
source ZIP and extract it into a new retained directory. From this installer's
extracted package run `python3 scripts/install_codex_plugin.py --package
/path/to/prior/sfc` (and `--codex` when needed). This makes a new native export
from the prior bytes through the official CLI; it does not move Git refs,
restore old run state or copy trust. The prior package must support the native
MCP template and validate the exported layout without the portable root manifest.
The installer materializes that layout in a temporary directory and runs the
selected package's own validator before any native installer/cache operation.
Incompatible packages fail before replacing the installed plugin.
Retain both source folders and their receipts. Reopen Codex and review hook trust
after a version change. Do not delete an older journal to force a reinstall.

## Authorization and startup

Keep existing choices. New `onboard` defaults to auto-start with session lifetime
watching after explaining the actual repository/Project, current/future
Backlog/Ready scope, holds/exclusions, finite limits and continued worker usage.
Use `onboard --no-auto-start` to leave watching off, or `--no-session-lifetime`
for legacy bounded auto-start. No worker starts during onboarding; attachment
requires a subsequent eligible native hook event and trusted hooks. Incomplete
dashboard setup returns `pending-setup` without arming watching. On unsupported
lifetime hosts, onboarding still saves the project configuration and attempts
dashboard setup. It reports `pending-host` and leaves watching off. After the
dashboard is ready, explicitly choose bounded watching with `autostart --enable`
or leave watching off. Until you choose bounded watching, `onboard-finish` on the
same unsupported host reports `pending-host`. Once bounded watching is
authorized, `doctor` and `onboard-finish` report the verified bounded choice.
Existing version-1 receipts and existing configurations are never migrated.
Plain `autostart --enable` retains legacy bounded mode. For an existing project,
after reviewing the same scope and usage, explicitly select:

```sh
python3 "<plugin>/scripts/sfc.py" --root "<repo>" autostart --enable --session-lifetime
# New onboarding already defaults to this mode; it does not authorize merges.
```

This deliberately authorizes successive finite work windows for eligible native
project connections. Total session duration and total usage can exceed one work
window; individual workers, checks, claims, ticket admissions and per-card attempts stay
bounded. No merge permission is implied. Only separately authorized
`--allow-merge` with configured auto policy grants automatic merging. All normal
independent lanes, check/CI policy, sensitive/size rules and exact pins remain.
Windows carry no-progress spending and preserve card attempts, output-launch
reservations, uncertain claim/ref/activity journals and archived receipts.

Both portable root identity and the Codex compatibility overlay reference the
compatibility `.mcp.json` launch. It explicitly forwards `SFC_WORKER` and
`SFC_TEST_CONTEXT` using `env_vars`; it has no cwd override, so Codex supplies the
actual per-chat checkout. Initialization exposes two narrow tools,
`session_attach`, `session_attach_hook` and `session_status`, and starts no observer,
board read or work. SessionStart/UserPromptSubmit use `session_attach_hook` to
return the event-specific hook JSON those host events require. Ordinary
`session_attach`/`session_status` keep their status-object contract; PreModelRequest
retains the existing readiness path. All attachment paths share the same native
root identity/proof and stored authority checks.
There is no command-execution or enable/re-arm MCP tool. User MCP on/off and tool
policy remain host-owned; no permissive policy override is bundled.

Review/trust the exact updated hooks in Codex. The short command hooks record
actual root SessionStart/UserPromptSubmit fields and a pending attachment
receipt. Child/test contexts, plan/unknown permission, and unsupported starts
cannot admit work. MCP event templates use only fields documented on each
event; optional agent fields are checked by the command receiving the full
event. Prompt/model-request attachment must also match the native turn ID in
that child-checked root prompt receipt. Child hooks share the parent's session
ID, so that ID alone cannot authorize a fallback. Missing, stale or child turn
proofs cannot bind a connection. Root events need no optional agent placeholder.
A supplied session string alone supplies neither liveness nor authority.
The runtime also requires the saved request/authorization and launch checkout,
and generates its own connection identity. It also requires Codex's native
`params._meta.threadId` to match the root hook session ID. The transport
reads this outside tool arguments; a child or another chat cannot replay
a parent's receipt by supplying SessionStart fields. Missing or mismatched
native metadata fails closed. This wire behavior was verified against
Codex 0.167.0 source and two isolated native MCP connections; other hosts
must establish the same identity contract before this mode is enabled. It never reconstructs liveness from
transcripts, private databases, foreground windows, sidebar presence or a PID.

SessionStart may precede MCP readiness. A missing connection leaves Pending;
UserPromptSubmit repeats the short root request and attachment, and
PreModelRequest retries attachment using the saved root request. The command
and MCP handler may race; all requests are idempotent and the later documented
event supplies the fallback. Control hooks have five-second bounds. The
attachment call returns after its private pending receipt, before background
admission/network/work. Neither a hook nor an open request owns the observer.
There is no unsupported MCP SessionEnd handler or hook-trust bypass.

## Windows, ownership and failure

The existing watcher and common-git supervisor lock own each finite window.
Other connections wait; they never steal locks, signal saved PIDs, duplicate
claims or discard state constructed by another supervisor. Each admission
reloads authorization and state under that same lock, verifies configuration,
board identity/readiness, and checks the current watcher against its pinned
predecessor. A durable start intent precedes remote IO. A normal idle exhaustion
archives the old identity and admits a new one under explicit continuation
scope. Expired windows are never revived or reset. Capacity remains at most two.

An idle cooperative disconnect can hand off the original unexpired window,
with the same deadline/counters. An expired idle window stays archived; its
successor gets a new identity. A cooperative work-window cutoff is journaled explicitly: a stopped lane
reservation remains spent, the old window stays archived, and continuation uses
a new identity with carried no-progress spending and the existing per-card output
cap. Cutoff between stages is accounted the same way. This is distinct from an
independent worker timeout or malformed result. Interrupted work at transport
disconnect, or an incomplete start intent, pauses for explicit recovery. No-progress
exhaustion, malformed results or local authority/request receipts, rate limits and uncertain claim/ref/activity
outcomes never create an automatic retry window. Normal per-card findings
handling keeps the existing bounded contract; it does not erase spending.

Owned worker/check timeouts and exhausted output allowances are ticket-local
during watching. The supervisor journals the failure, verifies Blocked/activity
publication and releases the slot; the same connection keeps observing other
eligible work. Known findings/Blocked outcomes retain their per-card spending
without spending or refunding the observer's existing no-progress count. An
incomplete failure-publication intent, ambiguous claim/ref/activity/completion,
configuration drift, operator stop or unknown failure still pauses the runtime.
No quarantined ticket is retried automatically, and no worker budget is renewed.

`session_status` distinguishes pending, observing/active work, waiting for the
common lock, paused work requiring action, and ended transport. Paused work
does not poll a failed API or relaunch workers; the MCP connection remains
available for inspection. `autostart-status` shows the saved authorization,
last hook event and recent private connection receipts, explicitly as recorded
facts rather than liveness proof. `watch-status` retains actual window budgets.

Operator `stop` takes precedence over cancellation/window exhaustion. Stop,
disable, plan-mode requests, authority/config drift and host shutdown cancel or
pause work. Re-arm only after inspecting retained receipts and stopping the old
owner; use the explicit lifetime option again. Enabling retains old archives and
never resets card attempts. A failure does not re-arm simply because another
prompt arrives or a different chat connects. Ordinary turn completion does not
close the MCP connection or cancel observation.

## Descendant cleanup and platform gate

Lifetime mode is currently available only on macOS with `waitid/WNOWAIT` and
POSIX process groups. Linux and Windows fail closed pending separate cleanup
proof; Windows needs an appropriate supported handle/job mechanism.

Every worker/check and supervisor git/gh command in the STDIO runtime uses a
private pipe guardian. Only the runtime holds the writer. EOF from abrupt
runtime removal triggers TERM then KILL of the guardian's own unreaped group,
followed by reaping. A live warden pins that group until the final signal,
including when the command exits before descendants. The guardian never signals
an arbitrary saved PID. Prompt stdin, worker JSON/log observation, stage
timeouts and existing cancellation precedence are retained. Worker/check
guardians append cleanup facts beside the original private log. Graceful EOF
or signals also request cooperative shutdown, but safety does not rely on
Python finally/EOF callbacks running in the removed runtime. This is owned-group
cleanup, not a sandbox against commands intentionally escaping process groups.

## Evidence and native acceptance boundary

`tests/test_session_runtime.py` uses temporary repositories, fake GitHub/Codex
adapters and fake time for same-connection observation past the old hour,
successive identities, readiness fallback, duplicate ownership, handoff,
stop/config/permission drift, recursion suppression and preserved failure
spending. Harmless real process fixtures kill their owning fixture with SIGKILL
and prove that TERM-ignoring commands and descendants close their FIFO writers;
worker/check cleanup receipts record reaping. No real boards or paid workers
are used. The manifest launch test runs the actual STDIO adapter with the
declared environment whitelist; it is not native Codex plugin-loader evidence.

Before activation, the interactive maintainer must observe the integrated
packaged plugin in native Codex: manifest loading and worker-marker forwarding,
hook review/trust and tool policy, startup before readiness, prompt/model-request
fallback, resume and duplicate chats, continued idle observation between model
turns, and unload/disconnect with owned child cleanup. Do not infer those results
from fake-host tests or the earlier feasibility probes. Full configured checks
and independent QA/security/review belong to the normal supervisor gates.

Interfaces: [local plugin STDIO servers](https://developers.openai.com/plugins/deploy/desktop-only#configure-a-local-mcp-server),
[compatibility packaging and env forwarding](https://developers.openai.com/api/docs/guides/agents-api/tools/plugins),
[MCP tool hooks/readiness and templates](https://learn.chatgpt.com/docs/hooks#mcp-tool-hooks),
[loaded-thread unloading](https://learn.chatgpt.com/docs/app-server#unsubscribe-from-a-loaded-thread).
This local MCP addition does not establish public-directory eligibility or
authorize submission, installation, activation or a live queue run.
