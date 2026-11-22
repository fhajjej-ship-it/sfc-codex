# Configuration

Onboard creates `.sfc/config.json` in the target repository. It refuses to
overwrite an existing configuration. Read/edit it when the factory is stopped;
changes invalidate old evidence and require a new run.

The optional [review policy](REVIEW-POLICY.md) configures independent review
providers and minimum risk rules. Omission preserves the existing three lanes.

| Field | Default / meaning |
|---|---|
| `version` | `1` |
| `repo` | Verified `owner/name` GitHub repository |
| `base_branch` | Explicit branch or origin's default |
| `project.owner`, `project.number` | Existing GitHub Project identity |
| `status_field` | `SFC Status` |
| `activity_timezone` | Optional IANA timezone for card report times; omitted defaults to `UTC` |
| `verify_commands` | Required nonempty list of project-owned shell checks. `sfc doctor` reports the configured command count as a top-level JSON integer named `verify_command_count`. The existing `capabilities.project_checks.detail` field lists the configured commands. |
| `merge_policy` | New onboarding: `auto`; explicit `human` alternative; auto also needs separate run/watch merge authority |
| `merge_mode` | Optional `github-protected` default; explicit `local-verification` uses atomic fast-forward only |
| `auto_max_lines` | `400` additions plus deletions |
| `allow_no_checks` | `false`; deliberate opt-in for no GitHub CI checks |
| `model` | New onboarding: `gpt-6.1-sol`; default worker model |
| `reasoning_effort` | New onboarding: `high`; worker reasoning effort |
| `service_tier` | New onboarding: `fast`; requested speed tier |
| `worker_runtime` | Optional `skill_catalog` and `memories` booleans. Omitted keys inherit Codex. Normal efficiency setup/migration sets both to `false` unless an explicit object already exists or the profile is `off`. |
| `work_types` | Optional explicit five-type label mapping pinned by `id`, `name`, `description`; see [work types](WORK-TYPES.md) |
| `models` | `{}`; optional `build`, `qa`, `security`, `review` model overrides |
| `worker_routing` | Optional GPT-6.2 Sol routing with a list of operator-selected routine Build issues. |
| `release` | Optional; omitted/off preserves existing behavior. Separate packaging, artifact verification, version trigger and scoped draft/publication authority; see [release configuration](RELEASES.md). |
| `max_workers` | `2`, allowed 1–16. `sfc doctor` reports the configured worker concurrency limit as a top-level JSON integer named `max_workers`. |
| `max_cycles` | `20` individual ticket admissions, reserved before claim; allowed 1–1000 |
| `max_no_progress` | `3` consecutive tickets without terminal/stage/evidence progress, with conservative outstanding-launch reservations; allowed 1–100 |
| `max_attempts` | `2`, allowed 1–20 |
| `worker_timeout` | `1200` seconds per Codex worker invocation, allowed 1–86400. `sfc doctor` reports the effective limit in the top-level JSON field `worker_timeout` as an integer number of seconds. |
| `verify_timeout` | `900` seconds per verification command, allowed 1–86400. `sfc doctor` reports the effective limit in the top-level JSON field `verify_timeout` as an integer number of seconds. |

The recommended starting preset is **GPT-6.1 Sol + High + Fast** for all four
roles. An entry in `models` takes precedence over the project-wide `model` for
that role. Users can change these choices while the factory is stopped; the
configuration digest invalidates prior evidence when any setting changes.

Onboarding accepts `--model`, `--reasoning-effort` and `--service-tier` overrides.
Use `inherit` for an onboarding option, or null/omit the corresponding JSON
field, to inherit Codex CLI settings. Existing configurations retain their
choices and inheritance behavior; onboarding refuses to overwrite them.

To opt in to GPT-6.2 Sol routing, set these fields while the factory is stopped.

```json
{
  "model": "gpt-6.2-sol",
  "reasoning_effort": "high",
  "service_tier": "default",
  "models": {},
  "worker_routing": { "routine_build_issues": [42] }
}
```

Build uses Medium reasoning for the listed issues. Other Builds, QA, Security,
and Review use High. All use Standard speed by default, including in a watcher.
Add `--waiting` to a one-ticket invocation of `run --include-backlog --issue 42`
and its dry run to request Fast for that ticket's Build. Reviews stay on
Standard. The issue list is local operator configuration. Reassess scope when
the task changes. Run previews show whether Fast Build was requested, and
dispatch receipts record the settings requested for each worker.

`sfc doctor` includes a top-level `worker_settings` object alongside the timeout
fields. It reports the requested `model`, `reasoning_effort` and `service_tier`
from the loaded project configuration. `models` contains the explicit per-lane
model overrides and is `{}` when none are configured. A `null` setting means
SFC leaves that choice to Codex for workers without an applicable override.
Doctor leaves inherited settings unresolved.

Reasoning and speed are independent. SFC passes `reasoning_effort` as Codex's
`model_reasoning_effort`, and `service_tier` as the Codex service tier. Fast is
configured as `fast` (Codex maps it to priority service), can consume more usage,
and requires support from the selected model/account. Check availability before
running; an unsupported choice is a configuration problem, not authority for a
silent fallback. `doctor` checks tooling/GitHub/dashboard readiness, not model
or tier availability. See the [Codex configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference).

Verification executes in fresh worktrees. Include required dependency installation
in the command when needed, and ensure generated test outputs are ignored by
the target project. Commands that modify tracked source invalidate the result.
No secret values belong in configuration, issue bodies or command arguments.

Use project-owned commands for project-owned services. A check requiring a live
mutation or unavailable credential should be redesigned for test data or held
for a user decision; it is not permission to broaden worker access.

`status` and `lint` read the board, configuration and saved state without writes.
`lint` prints JSON with an `issues` array of issue `number` and `problems` entries
for Ready, Building, QA and Review issues. It exits 1 if any of those issues has
a reported problem, and 0 if all pass or the eligible queue is empty. Backlog,
Blocked and Done issues are excluded. This makes `lint` usable as a shell
preflight while preserving its JSON output.

`run --dry-run` shows next eligible choices, occupied slots and dependency waits without claims,
worktree creation, model execution or GitHub writes. It is a planner, not proof
that builds or remote writes will succeed. `doctor` checks tools, GitHub auth
and the required project field without repairing them. It also reports dashboard
readiness separately as `onboarding_complete`; runtime `ready` alone does not
mean the saved board layout is complete.

`dashboard` verifies the saved `🏭 Factory` view, grouping, stage order, colors
and emoji descriptions. It is read-only and exits 2 while setup is incomplete.
`dashboard --apply` applies the bundled presentation during explicit setup/repair,
preserving existing option IDs, extra options and custom views. It does not retry
ambiguous writes. Finish any returned pending Column by step in GitHub's browser
UI, save the view, and rerun `dashboard`. New onboarding includes these defaults;
no memory from the chat is required.


Version 0.1.6 adds fixed reserved TEXT fields `SFC Agent` and `SFC Activity`;
no project configuration migration or worker-permission change is needed.
To use local card times, set `"activity_timezone": "Europe/Stockholm"` (or another
available IANA zone) while stopped, or choose `--activity-timezone` at onboarding.
Omitted settings use UTC independently of the host's local timezone. Invalid or
unavailable zones halt validation; UTC works without system timezone data.
Daylight saving and local date/year rollover follow the selected zone's rules.
Public times use `YYYY-MM-DD HH:MM TZ`; local activity receipts keep full UTC ISO
timestamps, including seconds and offset. Changing the setting invalidates prior
configuration-pinned evidence like any other configuration change.
Existing boards require an explicit `dashboard --apply` upgrade, followed by
read-only `dashboard` verification before a run. Setup refuses duplicate names,
wrong field types and truncated inventories without replacing fields. Activity
field IDs are checked against each item's saved receipt, never silently rebound.
The 60-second running refresh and five-second substep coalescing are bounded
runtime constants, not user-defined public text or a worker-controlled channel.
See [activity reporting](ACTIVITY.md) for state meanings and recovery.

## Priority setup and ticket creation (0.1.7)

New onboarding creates/reuses a single-select Priority field and verifies its
saved visibility on Factory cards. Defaults follow the shared evidence rubric:
P0 Immediate (ongoing critical harm or imminent sourced critical deadline, no
safe workaround), P1 High (supported core-flow failure, substantial recurring
impact or its prerequisite), P2 Normal (bounded impact/workaround or useful
improvement without demonstrated urgency), P3 Low (cosmetic/optional, demonstrated
limited impact). Unknown evidence is not automatically Low.

Existing configurations are not silently migrated. Run `priority-setup` to
inspect; `priority-setup --apply` explicitly creates the missing field and appends
its ID to the saved Factory visible fields. It requires an existing Factory board
view; use `dashboard --apply` for full onboarding/presentation repair. Both apply
paths verify saved visibility; read-only status/doctor/dashboard report missing
setup and never create fields or alter cards. Priority-only setup leaves view
layout, filters, grouping, other visible fields and all options/item values alone.
Full field/view/visible-field inventories are paginated; incomplete or ambiguous
inventories halt writes. Setup receipts under `.sfc/priority-setup/` retain intent,
actual IDs, before view and readback/failure details; repairs do not erase them.

Automatic reuse requires the canonical option names **and meanings** shipped by
SFC. Bare P0/P1, High/Medium/Low or custom descriptions require an explicit mapping;
the runtime never guesses meanings, rewrites options or overwrites item values.
Document the actual convention and its evidence in the project, then configure:

```json
"priority": {
  "field_id": "<actual-single-select-field-id>",
  "options": {
    "P0": "<actual-immediate-option-id>",
    "P1": "<actual-high-option-id>",
    "P2": "<actual-normal-option-id>",
    "P3": "<actual-low-option-id>"
  }
}
```

This is an explicit assertion of semantic equivalence, not a field rename or
option replacement. IDs must exist in the configured Project, be distinct and
belong to a single-select field separate from the routing stage. Explicit mapping
can select one field amid duplicate names; it never changes the other fields.
Configuration changes invalidate configuration-pinned factory evidence as usual.

The interactive supervisor automatically assesses new tickets with the shared
rubric, preserving explicit user choices and explaining sources/unknowns. It uses
`create-ticket --request-id <stable-id> --title <title> --body-file <file>
--priority P0|P1|P2|P3 --type Bug|Feature|Refactor|Documentation|Maintenance --reason <evidence> --unknowns <limits>` with the standard
plugin script/target prefix. Required setup, level and nonempty assessment are
validated before issue creation. The command creates, enrolls, sets explicit
Backlog and Priority, and verifies actual issue identity, title/body, repository,
Project membership and both field values. This is separate from existing-backlog
prioritization, which retains its manual-value/annotation ownership rules.

Creation can run alongside a live run/watch supervisor. Creation/setup use a
separate repository-common lock, and pickup requires completed creation evidence;
see [lock/readiness/recovery contract](WATCH.md#ticket-creation-alongside-a-supervisor).

Creation receipts live at `<common-git-dir>/sfc-tickets/<request-id>.json`. A receipt is saved
before the first remote write. Partial failures report incomplete creation and
retain actual known identities, before/observed values and recovery diagnostics.
Read-only discovery may recover identities after a lost response; ambiguity is
reported and never converted into success. Repeated IDs never perform writes.
Inspect actual remote state before any explicitly scoped manual recovery; do not
remove a receipt or choose another ID as a blind retry. No automatic rollback,
Ready promotion, queue reorder, worker execution or merge is part of creation.
GitHub does not offer compare-and-swap for these writes; immediate preflight and
readback detect observable human edits, but cannot eliminate the narrow race.
Every target value is tracked from its first observation, including a matching
manual Priority that is later cleared; an observable change halts creation.
Fresh full field inventories revalidate the routing field and option-ID meanings
(including explicit Backlog) before creation/enrollment/field writes and at
completion. Cached metadata alone cannot establish the item's routing status.
Setup and creation also pin the live names and descriptions of all four mapped
Priority options at preflight, including explicit custom mappings. Observable
changes halt subsequent writes or completion and retain partial-write receipts;
an unchanged option ID does not establish an unchanged meaning.

## Work-type setup (0.1.8)

New onboarding includes five work types and verified built-in Labels visibility
on Factory cards alongside Priority. Existing projects upgrade explicitly with
`type-setup --apply` or full `dashboard --apply`; configuration is preserved.
Read-only `type-setup`, status, doctor, dashboard and creation preflight never
create labels or change views. `create-ticket --type` is required; actual issue
labels must read back correctly before `complete: true`.

See [work types](WORK-TYPES.md) for definitions, the conservative alias/description
policy, full explicit `work_types` mapping, auxiliary/QA separation, observable
change detection, setup failure receipts and duplicate-prevention recovery.

## Scoped priority run options (0.1.10)

`run --include-backlog --issue <number> [--issue <number> ...]` explicitly opts
this run into mapped Priority pickup of the authorized Backlog/Ready allowlist.
Both flags are required together; they never change `.sfc/config.json`. Use the
same flags plus `--dry-run` to inspect actual eligibility, order and exclusion
reasons without writes. Existing configs and default Ready-only runs retain their
eligibility and merge authority; all modes rank by verified Priority and refill
free slots. `status` stays read-only; the opt-in dry-run supplies detailed
scheduler reasons. The run receipt records options, each plan and occupied ticket slots.
See [Backlog pickup](BACKLOG-PICKUP.md) for complete inventory, dependency grammar,
CAS limitations and latched claim recovery. Existing custom `priority` option-ID
mappings are reused, never rewritten by dispatch. Merge settings are unchanged.

## Merge mode (0.1.9)

New onboarding defaults to `merge_policy: auto`; choose `--merge-policy human`
to leave merging to the user. Existing saved policies and recorded session
authority remain unchanged by installation/update, reopening or read-only
status/doctor. Onboarding refuses an existing config. Policy alone starts no
workers, grants no run scope or merge authority, and creates or renews no watcher
session. Use separately authorized run/watch `--allow-merge`; after authorization
for that scope, eligible PRs need no routine approval. Named-session settings,
counters and deadlines remain immutable on resume.

Omitted `merge_mode` preserves the protected GitHub mode. Onboarding with
`--merge-mode local-verification` (or an explicit config edit while stopped)
opts into atomic fast-forward of the exact reviewed
head; it requires `merge_policy: auto` and an authorized `run --allow-merge`.
`--allow-no-checks` is a separate explicit onboarding choice, default false.
Read [local merge guarantees, history tradeoff and recovery](LOCAL-MERGE.md)
before selecting it. No existing config is migrated.


## Bounded watcher settings (0.1.11)

An optional `watch` object supplies finite defaults only. It never enables
watching or changes ordinary run selection. Omitted settings preserve existing
project behavior. Unknown keys, zero/noninteger budgets and invalid exclusions
fail validation. CLI watch options override these defaults for an explicit session:

```json
"watch": {
  "poll_interval": 30,
  "max_seconds": 3600,
  "max_polls": 120,
  "max_claims": 10,
  "excluded_issues": [],
  "hold_label": "sfc-hold"
}
```

Intervals are positive integer seconds (1–3600); session time is 1–86400
seconds, polls 1–10000, new claims 1–1000. Exclusions are positive repository
issue numbers (the stable numeric issue ID within the configured repository).
Set `watch.pickup` to `false` to leave fresh Backlog and Ready work tickets
unclaimed while watching. Existing owned work and separately configured
releases keep their existing eligibility rules. `onboard --no-pickup` saves
this choice.
The nonempty hold label matches case-insensitively and is never created or
removed by watch. `--exclude-issue` adds to configured exclusions; `--hold-label`
selects the session's hold label. Other CLI overrides are `--poll-interval`,
`--max-seconds`, `--max-polls`, `--max-claims`. See [watch](WATCH.md) for future
queue authority, durable spending, status and recovery. The existing max_workers,
max_cycles, max_no_progress, max_attempts and per-process timeouts still apply.

## Project chat auto-start (0.1.12, unreleased)

New onboarding defaults to lifetime watching: a version-2 local authorization
with `mode: interactive-session`, `continue_windows: true` and a window identity
journal. Explain the repository/board, current/future Backlog/Ready scope,
holds/exclusions, finite limits and continued worker usage before setup. Choose
`onboard --no-auto-start` to leave watching off, or `--no-session-lifetime` for
legacy bounded auto-start. The default does not authorize automatic merges.
Lifetime mode requires supported macOS cleanup and `max_workers <= 2`.
An unsupported host saves the project setup and reports `pending-host` with
watching off. After the dashboard is ready, use `autostart --enable` to explicitly
choose bounded watching, or leave it off. For a supported watching mode, incomplete
dashboard setup reports `pending-setup`. Finish the dashboard, then use
`onboard-finish` to apply the saved choice. Workers can start after trusted native
hook attachment.

Existing configurations and version-1 receipts are never migrated; absent
existing authorization remains off. Existing projects use explicit
`autostart --enable --session-lifetime`, plain `--enable` for bounded mode, or
`--disable`. The common-git authorization pins the configuration digest, actual
checkout/repository/board and chosen watcher scope. `watch` defaults alone do
not enable it. Private `sfc-session-requests/` and `sfc-session-connections/`
receipts also live in the common git directory. Hook trust remains Codex-owned.
Automatic merge authority requires separate `--enable --allow-merge` plus
configured `auto` policy. See [session runtime](SESSION-RUNTIME.md) and
[legacy lifecycle/recovery](AUTOSTART.md). Opening the app alone is not a trigger.

Use the [supported onboarding checklist](ONBOARDING.md) and CLI `capabilities`
report. Project configuration, pending dashboard repair, hook trust, actual MCP
connectivity, watcher arming and actual observation have separate evidence levels.
