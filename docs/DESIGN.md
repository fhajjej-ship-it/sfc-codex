# SFC design

For the 0.1.18 execution-evidence/acceptance separation, complete merge-to-release
flow, supported profiles and measured-claims limits, see
[delivery efficiency](DELIVERY-EFFICIENCY.md). Repository reuse does not transfer
merge or publication authority; final receipts retain exact candidate/base/task
pins. Optional inspection overlap requires qualification and a separate final
assessment consuming the actual completed checks.

SFC [tracks work in Codex chat](CHAT-TRACKING.md). The chat implements,
tests and reviews. Authorized pull requests use the repository's usual process.
SFC saves milestones to a local outbox and reads back GitHub Project changes.
Existing configurations use that path on upgrade. The following sections
describe the previous factory engine and the receipts it left behind.

Local [delivery accounting](DELIVERY-ACCOUNTING.md) links work and release
tickets, command/worker attempts, immutable reuse references and publication
recovery across runs. Status links an offline-rebuildable report with stage wall
time, summed worker time, available tokens and explicit unknown costs. These
observations confer no gate, retry, merge or publication authority.

The plugin packages nine self-contained skills and a Python supervisor. It
requires Codex CLI, git and GitHub CLI. The 0.1.13-dev.3 candidate adds a local
STDIO MCP session runtime. There is no hosted server, database, Claude runtime,
external skill installer or model-specific pricing assumption.

GitHub Projects v2 owns the queue. Local `.sfc/` state preserves transitions,
worktree paths, head/base pins and receipts between runs. A common-git-directory
lock excludes competing supervisors, including linked checkouts. Independent
cards can build concurrently; each card's lanes are sequential. Merges are
serialized and checked against both the reviewed head and current base.

New onboarding stores a configurable GPT-6.1 Sol / High / Fast worker preset.
Role-specific models override the project default. Missing or null settings
inherit Codex CLI configuration; existing projects are never silently migrated.
Model, effort and tier choices are covered by the configuration digest.

Workers use `codex exec` with JSON output schemas and finite timeouts.
The supervisor can supply the exact bundled lane-skill bytes in a pinned context
package, avoiding a separate model-directed skill read. It reads the package
fresh and validates skill/schema hashes and source/head/base/task/config scope
before dispatch and result acceptance. Unsupported package reads retain normal
skill discovery. Repository instructions and referenced assets stay live reads;
workers keep fresh CLI sessions and tool discovery. No authority, review or retry
budget is reused by this context preparation. Normal efficiency setup and migration
sets invocation-only `worker_runtime` options to omit the automatic skill catalog
and personal-memory injection/collection. The exact assigned skill is still
supplied, project instructions still apply, and installed skill files, tools,
hooks, credentials and model settings remain available. Existing explicit context
settings are preserved; missing settings in legacy projects inherit Codex until
an inspected migration is applied. No host configuration is edited. Build
gets workspace-write; QA, security and review get read-only. The supervisor
owns commits, pushes, draft PRs, board writes and merges. QA-only cards test the
existing base without manufacturing a PR. Findings return a card to Build;
repeated failures block it. Dependencies are read from `## Blocked by`.
During watching, an owned worker/check timeout or exhausted output allowance
parks only its ticket in Blocked after verified supervisor publication. Known
findings and terminal Blocked results spend their existing ticket allowances,
not the observer's no-progress allowance. Other eligible work can take the freed
slot and future arrivals remain observable. Unresolved failure-publication,
claim/ref/completion/activity writes, stops, configuration drift and unknown
errors still halt the supervisor. Worktrees, raw receipts and spent reservations
remain intact; quarantined execution failures require explicit recovery and gain no retry.
New watcher policies separately authorize finite automatic recovery of structured
read, prerequisite, missing-evidence and failed-check blockers; see [watcher recovery](WATCH.md#automatic-recovery-of-owned-blocked-work).
These actions preserve source/evidence and spent attempts, never grant another
Build at its findings cap, and retain independent review and merge/publication gates.
The operator can explicitly grant one watcher-eligible retained-work Build for
a verified interrupted Build timeout, supervisor base conflict, failed project
check, malformed Build, verified unsupported-model startup rejection or resolved
blocked-worker prerequisite. Every such grant
requires new explicit owner authority; it cannot renew a spent supplement.
Recovery pins the live candidate/base, dirty and untracked work, claim,
original outputs/receipts and spending. It journals admission and consumes the
one-use grant before base integration, without refunding interrupted work or
granting a merge exception. See `recover-ticket` in WORKER-RESULTS.md.
New verdict schemas use a nonblank evidence text field to avoid optional-array
padding. Legacy array-form verdicts still require every item to be nonblank;
both forms require a nonblank summary. Raw results and receipts are preserved
without normalization. Malformed output halts without publication or automatic repair. Durable
per-lane launch reservations bound unsuccessful outputs across interruptions and
resumes, independently of findings attempts; retained pre-journal failures count
toward that bound. See [worker result and recovery limits](WORKER-RESULTS.md).
Explicit one-use Build recovery pins retained log/result hashes and, for an
interrupted worker without a result, that result path's absence. Any later
evidence change invalidates the grant; no historical result is reconstructed
or accepted, and original reservations remain spent.
An explicitly operator-authorized corrective Build can address an independent
finding on a saved PR after earlier limits are spent. It pins the candidate,
finding and retained history, never resets those limits or changes merge gates,
and can only be admitted by a named scoped run. It never renews automatically.
Supervised builders reproduce relevant failures and run meaningful focused checks;
they do not routinely duplicate configured full verification. Project instructions
remain authoritative: explicit builder-specific/full-suite requirements and runs
needed to reproduce the defect are honored, with the reason recorded. Standalone
builds retain project/user verification obligations. Builder evidence distinguishes
checks actually run from deferred supervisor checks; pass means ready for independent
QA. Repository-wide completion requirements are fulfilled by actual supervisor
checks before overall factory completion, without overriding explicit builder duties.
This prompt/skill contract is not a sandbox or proof against arbitrary worker commands.
The supervisor owns nonempty configured checks before independent lanes and validates
them at the final merge candidate. Explicit per-command contracts permit reuse of
matching immutable execution evidence; all misses execute fresh checks, regardless
of file type or builder assertions. Existing configuration keeps executing checks.
Failures prevent merging. Independent lanes and all existing gates remain.
See [verification evidence and measurements](VERIFICATION-REUSE.md).
An optional [review policy](REVIEW-POLICY.md) selects a provider per obligation
and pins exact source, risk, coverage and completion evidence. The existing
three-lane policy remains the default. Specialist, human and release
obligations retain their evidence and authority.
The supervisor supplies read-only lanes with its actual project-check receipt
and raw log paths, pinned to the head, base, task and configuration. Missing,
failed or stale evidence prevents model dispatch. The lanes inspect that evidence
and independently assess behavior and coverage without rerunning suites that
require writable temporary fixtures inside the read-only sandbox.

After a pinned final review, the supervisor replaces its exact pending PR-body
paragraph with a concise summary. Public project checks use ordered check IDs
matching the local receipt, with fixed public descriptions for known checks or
tool categories; command text, arguments and raw output stay local. Each check
is labeled executed or reused from validated original evidence. Existing guarded
activity reports for independent lanes, reviewed handoffs and verified completion
include the same executed/reused counts through existing write callsites. Neither
the count nor a passing review changes the completion or merge gates.
Shell commands can contain private data in arbitrary syntax, so publication
never relies on credential-pattern detection. Human edits outside that paragraph
are preserved. The exact published text is tracked per PR so unchanged generated
summaries can be refreshed after a new head/base passes review; edited, missing
or duplicated sections are left untouched.

The smallest complete flow covers onboarding, lint, status, run, stop, build,
QA, review and PR security. Collection, UI refinement, HTML recaps and git sync
are interactive skills using Codex's available tools, without another runtime.
External telemetry sources and browser tools are optional, used only when
connected and relevant. No live migrations or automatic skill rewriting.

Optional release cards (0.1.15-dev.4) are a distinct supervisor-validated workflow
kind, selected by repository-common release journals rather than labels or task
prose. They reuse the same seven columns, scheduler slots, finite budgets, lock,
process ownership and independent read-only lanes. Building packages an exact
merged candidate; QA receives actual project/artifact-check receipts; Review
checks the full source range, version, artifact identities and fixed notes. There
are separate frozen public notes and a complete card audit inventory. Public notes
use merged public titles and explicitly public pinned project documentation, with
accuracy and sensitive-data checks in the existing independent lanes. Explicitly
approved commit-bound summaries support private repositories without exposing
private titles. Missing meaningful prose blocks preparation. A frozen newer-stable
Latest policy (or explicit never choice) is reviewed with the final description;
publication and completion verify GitHub Latest readback without mutation retries.
Separately
authorized description-only edits retain original publication and artifact evidence.
There is no implementation PR or merge for a release card. Ordinary merged cards stay
Done and receive planned shared Target release metadata automatically. The first
adapter is GitHub Releases on github.com, off by default with separately scoped
draft/publication authority. A manual request or authorized version-file trigger
batches changes. Durable intents/readbacks cover every remote mutation; exact
published tag/source and downloaded asset hashes precede Done/Completed. The
workflow retains file-based version selection through final candidate freezing.
An interrupted active release resumes its original attempt through normal slot
admission, including at its attempt limit, without renewing command/lane budgets.
The attempt debit and its pinned reservation share one atomic factory-state
write; a lost release-journal advance is reconciled from that exact reservation
before admission, without debiting again or repeating remote writes.
This does not activate release permission, install a plugin, renew a watcher budget
or add launch prerequisites. See [configuration, protocol and limits](RELEASES.md).

New-project onboarding defaults merge policy to auto; explicit human policy
remains available. Existing configurations and recorded sessions keep their saved
policy and authority. Configured auto policy still needs separately authorized
run/watch merge authority; saving the merge policy alone starts no workers, grants no scope and
creates or renews no watcher budget. Once authorized for the selected scope,
eligible auto merges need no routine per-PR approval. Sensitive/large changes
still require a human. Verification commands
must be nonempty; present CI must pass, and absent checks need a separate explicit
project choice. Omitted `merge_mode` retains `github-protected`: scratch
integration checks, expected-head squash merging and strict up-to-date branch
protection. Configurations are never silently migrated.

The normal onboarding reference presents one read-only `onboard --preview`
review, then applies its exact digest with supplied choices. The digest pins
the live GitHub host, repository node ID and Project node ID and is required
for new merge or release grants. Apply revalidates those identities before
saving authority, including when a pending watcher choice finishes. It recommends
watching, eligible Priority pickup, automatic merge, GitHub publication and
qualified check/review reuse together. Watcher merge scope is explicit via
`--allow-merge`; release commands/assets come from project configuration and
draft/publication authority is scoped separately in the same review. Missing
release prerequisites or qualification leave that capability pending with a
fallback. Unsupported lifetime hosts retain the other configuration. Completion
and doctor expose these six statuses alongside the existing detailed board/host
evidence. `setup-defaults` previews and explicitly applies eligible efficiency
recommendations without replacing opt-outs or renewing saved authority.

The opt-in `local-verification` mode (0.1.9) instead requires the current base to
be an ancestor of the exact reviewed head and validates checks in a detached
candidate worktree, executing them unless immutable evidence is eligible for reuse.
It performs a single atomic non-force `updateRefs` transaction:
a compare-only head entry plus a base fast-forward, both with expected beforeOid.
It never creates an unreviewed merge/squash tree. Schema/rules/identity uncertainty
halts with no fallback; refs with active protection/rules require human or strict
mode. Task/config/head/base-pinned QA, security and review, finite limits, sensitive
and size policy and merge serialization remain in force. Base movement returns
for integration and fresh evidence. Local and paginated remote inventories cover
the candidate and its GitHub checks.

A durable local-merge intent precedes the ref mutation. Unknown/partial outcomes
retain all evidence, owned refs and worktrees. Restart reconciles by readback
without a repeat ref write. Done requires reachability of the approved candidate
on the intended base, actual MERGED PR recognition and a CLOSED matching issue;
board completion intents also latch lost responses. See
[the protocol and recovery contract](LOCAL-MERGE.md) for limits and source links.

Human-merged reviewed PRs have a separate completion-only receipt. Ordinary,
scoped Backlog and watcher reconciliation verify saved ticket/review ownership,
the actual merged PR and closed issue, Git reachability and a strictly supported
parent/tree shape before durable Done/Completed writes. Human merge policy needs
no auto-merge permission to observe that outcome and gains no merge authority.
Existing atomic journals always take precedence; uncertain evidence/writes halt
with retained receipts and existing activity/status diagnostics. See
[human merge completion and limits](LOCAL-MERGE.md#completing-a-human-merge).

Commands and hooks reduce mistakes; they are not a security sandbox for hostile
repository code. Project verification commands execute locally. Hook trust is
owned by Codex; SFC never bypasses it. Shell guards intentionally cover common
literal commands, not arbitrary Python, obfuscation or every tool surface.

Authorized supervisors wait on positively identified GitHub rate-limited reads.
HTTP Retry-After/reset headers determine the wait; hidden CLI response headers
use one read-only diagnostic for the relevant resource, with bounded exponential
fallback. At most three retries and one hour of waiting per logical read are
allowed, inside the original worker/window/stop/authority boundaries. One shared
adapter gate coordinates concurrent readers; local wait status does not write
the rate-limited board. Native observation reports waiting and resumes reads
automatically without a new attachment or approval while authority remains valid.
Mutations, mixed/unknown errors, permission failures and malformed responses
never receive automatic retries. Existing uncertain-write journals remain latched.
An empty-response connection reset retries a proven read up to three times,
with one-, two- and four-second delays inside those same limits. If an admission
queue read still fails, the watcher waits for its next poll without admitting work
from an old snapshot. If only a version-file release preflight read fails after
those retries, the trigger can wait while the scheduler reads and plans the
ordinary queue in the same poll. This requires no unfinished release journal and
no new creation intent. An unresolved creation stops admission independently of
version file changes, trigger selection or disabling releases. A recovery
command binds the exact retained creation using readback only. It can pass its
own journal-pinned stop without clearing it and still honors operator stops.
Reads after release creation begins retain the stop and recovery gates. For a lost
activity write response, readback can confirm the intended pair of fields in
the pinned Project item without resending it.
Startup uses the same exact readback rule for pending activity. Partial,
unchanged or externally edited pairs retain the recovery latch.
A cancelled preflight read preserves its prior verified activity receipt;
interrupted post-mutation readback retains its pending/error latch. No worker
launch, attempt reservation, source pin or merge permission is renewed by waiting.
Normal child exit cancels an unsent Running update, retaining its cancelled intent
as history. Once a mutation starts, readback keeps the original timeout, stop and
authority guards while no longer requiring a live child. Failed or mismatched
readback remains latched; normal exit alone cannot certify a check or retry a write.

Verification: real temporary git repositories for isolation/integration;
simulated GitHub and Codex processes for queue/transition/failure contracts;
manifest and skill validation; an isolated Codex marketplace installation.
Live board operation and real model correctness require a separate actual run.

Onboarding also owns dashboard presentation. It applies the bundled color and
emoji defaults with existing option IDs, preserving custom views and extra
options. `dashboard` checks saved view layout, vertical grouping, stage order
and style through read-only API calls. Incomplete grouping remains a visible
pending browser step because GitHub's public mutation cannot set Column by.
`dashboard --apply` is explicit, idempotent presentation repair. Runtime queue
routing continues to use canonical stage names, independent of appearance.


Activity reporting uses two SFC-owned Project text fields on the same item ID,
separate from canonical routing. Explicit setup/repair appends their IDs to the
saved Factory view's configuration.visibleFieldIds, preserving existing visible
fields, filters and grouping, then reads configuration.visibleFields back. The
supported [GitHub Projects schema](https://docs.github.com/en/graphql/reference/projects)
defines these APIs. Unsupported server schemas remain an explicit setup error;
Column by still requires the pending browser step. Read-only commands never repair.

Public state timestamps use `YYYY-MM-DD HH:MM TZ`, defaulting to UTC. An optional
validated IANA `activity_timezone` project setting (also exposed at onboarding)
converts only the card display, including DST and date/year rollover. UTC works
without system timezone data; unavailable configured zones halt rather than
fall back. Local activity receipt/history timestamps remain full UTC ISO values.
The exact public values remain in receipts for ownership/readback checks, and
the setting remains covered by the existing configuration digest.

At ticket completion, SFC verifies its routing field is Done before updating
a separate GitHub Status field with the standard Todo, In Progress and Done
options. Projects with a custom Status field or without one use their routing
field alone. SFC pins the ticket and field option IDs before writing and reads
back both fields before reporting completion. After an uncertain write,
recovery uses readback and preserves observed manual edits.

Runtime activity snapshots enumerate fresh field IDs/types and target item
values at both write boundaries. They do not load dashboard views, visibility
or grouping, which remain the responsibility of explicit dashboard checks.
This is a narrower read, not a field-identity cache or relaxed write protocol.

The supervisor observes owned child process start/exit and bounded Codex --json
item metadata. It never publishes worker prose or interprets reasoning. Started
command events, completed file-change events and supervisor project checks map
to fixed descriptions; absent finer events, the active lane says so explicitly.
An oversized event resets the retained substep to that lane fallback, since a
dropped completion cannot establish that the previous command remains active.
A per-process observer retains the latest substep, coalesces changes for up to
five seconds, and refreshes Running after 60 seconds while the child remains
observed active. Waiting/Stopped/Failed/Completed/Unknown never heartbeat.
Stop and timeout enforcement run independently of slow activity API responses.

One supervisor activity lock serializes read/intent/write/readback, with separate
per-item clocks and persistent expectations. Each update verifies project, field,
item, issue and repository identity; it changes only the two text fields. Local
intent/history precede mutations. Partial writes, mismatched readback or human
edits latch recovery across restarts and halt work without retrying the write.
GitHub has no compare-and-swap for text fields: an edit in the narrow read/write
race cannot be prevented atomically. Preflight/readback detect edits observable
at those boundaries; unexpected values are preserved rather than overwritten on
later reports. Detailed diagnostics remain local. See docs/ACTIVITY.md for
recovery and abrupt-supervisor-death limitations.

Priority is a required creation-time Project single-select value, independent of
routing and of existing-backlog annotations. Explicit setup creates only a missing
Priority field, otherwise preserves IDs/options/values and requires a documented
option-ID mapping for custom or ambiguous meanings. Saved Factory visibility is
appended and read back. Existing boards upgrade explicitly; status/doctor/dashboard
reads never migrate. Field, view and visible-field connections are fully paginated
and ambiguous/incomplete inventories halt writes.

The interactive supervisor applies the shared evidence rubric automatically when
ticket creation is requested. The bundled `create-ticket` path requires a level,
reason, unknowns and stable request ID, preflights setup, journals intent, creates
the issue, enrolls its actual node ID, sets explicit Backlog and Priority, then
checks issue/body/repository/project/item identity and values. Narrow writes and
preflight/readback preserve observable manual changes. All target values are
tracked from the first snapshot, including matching
values and subsequent clears. Fresh field inventories must match the pinned
routing field and option-ID meanings before setup/creation writes and completion;
Priority scope also pins each mapped option's name and description at preflight,
including explicit custom mappings, and rejects observable meaning changes;
cached IDs alone are insufficient. GitHub lacks atomic CAS, so the
read/write race remains explicit. Receipts latch partial/ambiguous writes;
read-only recovery discovers identities where possible without a duplicate
creation attempt. Repeated IDs never write. Workers only implement/test this
path with fake adapters; they do not operate it on real boards. Collection uses
this executable path; a prose-only priority is incomplete. This adds no authority
to promote Ready, start a factory or merge.

Work types (0.1.8) extend that same supervisor creation contract. The interactive
supervisor selects Bug/Feature/Refactor/Documentation/Maintenance from evidence
without a second request; explicit user choice wins and genuine ambiguity needs
clarification. `create-ticket --type` is mandatory. Before posting, full paginated
label and Project/view inventories establish a live compatible mapping and saved
Labels visibility alongside Priority. The initial issue POST assigns exactly one
mapped type plus explicitly requested existing auxiliary labels. Subsequent
operations only read labels; observable human changes/clears remain untouched
and prevent completion. Final readback verifies actual labels as well as the
existing body/identity/Backlog/Priority contract.

Only authorized onboarding, `type-setup --apply` or full `dashboard --apply`
creates missing labels or appends built-in Labels visibility. Default aliases
include enhancement/documentation; supported descriptions or explicit
id/name/description mappings establish meanings, never guessed substrings.
Setup pins the full label inventory across mutations; creation pins all type
labels/aliases including identity, name, description and color. Incomplete setup
writes retain intent/history/observed state and are never blindly retried.
No existing issue is bulk labeled. QA and sensitive policy labels are forbidden
as type mappings; `qa` is only an explicit auxiliary workflow choice. Risk, source
and Technical Debt context stay separate. Factory dispatch, priority levels,
lanes, worker defaults, finite limits, SHA/config/task pins and merge authority
are unchanged. See [work types](WORK-TYPES.md) for policy and recovery limits.

Priority pickup (0.1.10) is a per-run opt-in, requiring an explicit issue
allowlist. Default fresh eligibility remains Ready-only; all modes now share
the [rolling priority scheduler](SCHEDULING.md). A separate read-only
inventory path verifies the actual configured repository/Project, canonical
routing field, complete option/value/item/label connections and existing Priority
resolver. Unknown stages never become Backlog. Fresh eligible Backlog/Ready work
ranks together with unassigned saved Building/QA/Review claims by mapped P0–P3,
actual creation time, then issue number. Saved claims have no automatic precedence.
Resumed claims recheck prerequisites at planning and dispatch; reopened or
unresolved dependencies wait without worker execution or remote annotations.
The supervisor journals and claims fresh issues sequentially in selected
order, revalidates task/identity/stage/Priority/prerequisites, reads back actual
state, then assigns at most max_workers isolated workers. Uncertain claim writes
latch manual recovery across both default and opt-in restarts. Skips have local
reasons and no remote annotations. Run scope/order are preserved in receipts.
Finite admission revalidation defers only the changed ticket until a later run
and immediately replans independent eligible work within existing limits.
No priority annotation or ticket body grants dispatch/merge authority.
Pending local merge journals outside the run allowlist halt before recovery
writes or worker dispatch; dry-run reports the same scope blocker.
GitHub field writes and multi-query reads are not atomic; preflight/readback do
not close the narrow race. See [Backlog pickup](BACKLOG-PICKUP.md) for the contract.


Continuous pickup (0.1.11) is a separate, explicitly started foreground watcher.
It reuses the verified pickup planner and sequential claim path while retaining
the common-git lock across idle waits, readback recovery and workers. Every live
poll recomputes queue membership and hold/exclusion policy; unassigned saved and
fresh work compete by the same verified Priority order. Human handoffs wait without blocking other
eligible tickets. Idle polls and pinned, fully reviewed GitHub-check waits spend
only the poll/time budget; CI waits leave capacity for independent arrivals and
return through all normal review/merge gates once checks pass. Rolling ticket
admissions reserve a cycle before claim and conservative failure spending before launch;
claims reserve a finite session allowance before intent/write. Restart reloads
claims under lock and resumes a named session's immutable scope, merge opt-in,
deadline and counters. Manual sessions require explicit --start. No config
migration, daemon or OS startup service is added. See [watcher design/recovery](WATCH.md).

Project-chat auto-start (0.1.12, unreleased) is a separate explicit local
authorization, off when unset. A trusted Codex startup/resume background hook
enters the existing watcher under the common supervisor lock. Authorization pins
the actual checkout/common root, configuration, board identity, finite settings,
holds/exclusions and separate merge opt-in. Each chat gets one budget; cooperative
host cancellation resumes that budget, normal exhaustion permits a previously
unseen chat, and stops/failures/uncertain intents require explicit re-arming.
All SFC child commands inherit a recursion marker. No detached supervisor or
global Codex config edits are introduced. See [lifecycle/recovery policy](AUTOSTART.md).

Interactive-session watching is the accepted new-onboarding default on supported
macOS hosts, with explicit off/bounded alternatives. Existing configuration and
legacy version-1 authorizations are never migrated. Setup saves the reviewed
choice without starting workers; incomplete dashboard setup leaves it pending.
The pinned onboarding receipt permits finishing that same choice after repair,
without renewed authority or automatic retries. The [onboarding checklist](ONBOARDING.md)
separates project configuration from native hook trust, MCP connectivity and
actual observation. A native
per-chat local STDIO MCP connection owns observation between completed turns;
short trusted root requests plus SessionStart/UserPromptSubmit/PreModelRequest
MCP attachment handle readiness races. Initialization grants no board authority.
The existing watcher admits finite, independently journaled work windows under
the common-git lock. Lifetime authorization covers continued windows, not
unbounded per-card attempts or hidden failure retries. No-progress spending,
claims, output budgets and uncertain journals persist; unsafe interruption or
drift pauses work. Pipe guardians bind worker/check/supervisor command groups to
the runtime even after abrupt removal. The candidate is gated to macOS cleanup
support; native integrated acceptance remains required before activation. See
[the runtime/admission and cleanup contract](SESSION-RUNTIME.md).

Development installation on the tested Codex 0.167 desktop runtimes uses a
compatibility export. The portable root manifest suppresses lifecycle hooks on
those hosts, and the legacy MCP loader does not expand portable path macros.
An explicit installer obtains the native cache location using a capability-free
bootstrap, then exports the same source using the compatibility manifest and
an absolute MCP entry point. The native CLI owns cache replacement; source or
installed-location drift, partial outcomes and existing journals halt without
automatic retries. Exported cwd remains unset, preserving launch-checkout and
native-thread admission. No runtime authorization, attempt/merge limit, hook
trust or project configuration is changed by this packaging operation.
For a configured maintainer checkout, installation holds its common supervisor
lock through replacement and rejects an active runner before any CLI/cache
mutation. Loaded idle chats still require a host reload afterward; the installer
does not synthesize native attachment events.

Local merge recovery checks verified live membership, task/stage/routing scope and
separate auto-merge authority before completion writes; watchers also enforce holds/exclusions.
Only an acknowledged
atomic outcome on the exact intended candidate/base may receive five one-second
read-only confirmation observations while GitHub recognizes the PR/issue.
Ordinary Ready-only and scoped Backlog runs share this recognition window for
delayed PR recognition or issue closure, without workers or project-check reruns.
Spending precedes each wait and never resets on resume. Fresh routing inventories
and card readbacks guard ordinary completion; Completed activity must verify
before the journal becomes terminal. Identity/rules uncertainty and old/partial
snapshots halt; unknown ref responses never enter the wait or repeat a mutation.
All observations retain the
journal and original evidence. Process termination serializes signalling with
Popen's POSIX reap lock so a reaped/reused PID is never signalled.

An acknowledged pre-cutoff local merge also pins a completion-only allowance to
the original watcher session and work deadline plus 30 seconds. Only its verified
Done/Completed reconciliation may cross the work cutoff; workers, checks, claims,
publication and ref writes retain the original cutoff. Restart cannot renew the
allowance or spending. Stops, scope/evidence drift and uncertain-write latches
still halt it; completion IO has finite remaining-allowance timeouts. Distinct
readbacks and unfinished/exhausted journals remain available for manual recovery.


Ticket creation uses a separate common-git creation/setup lock, so it can complete
without releasing the single-supervisor run/watch lock. Shared terminal request
receipts publish readiness only after final creator readback; incomplete or
ambiguous SFC-created items wait locally before claims, annotations or workers.
Explicit setup takes both locks in supervisor-then-creation order, nonblocking.
See [the coordination/recovery contract](WATCH.md#ticket-creation-alongside-a-supervisor).
