# Work types (0.1.8)

Every new SFC Backlog ticket has one assessed work type, independently of its
required Priority. The interactive supervisor selects it from the request and
evidence during ordinary creation; no separate classification request is needed.
Honor an explicit user choice. Clarify genuine ambiguity before posting; a
requested improvement alone is not evidence of a defect.

| Type | Meaning | Default label | Accepted name aliases (case insensitive) |
|---|---|---|---|
| Bug | Supported defect in existing intended behavior | `bug` | `bug` |
| Feature | New or expanded user-visible behavior | `feature` | `feature`, `enhancement` |
| Refactor | Internal restructuring without intended behavior change | `refactor` | `refactor` |
| Documentation | Documentation or explanatory content changes | `documentation` | `documentation`, `docs` |
| Maintenance | Dependencies, configuration, CI and routine upkeep | `maintenance` | `maintenance`, `chore` |

Technical Debt is optional extra context, not a required sixth type. For mixed
work, select the primary outcome from the user's request and supporting evidence;
split independently useful work when appropriate. Do not invent impact or defects
to force classification. Explicit choices may be explained in the ticket's reason.

`qa` is a separate explicit workflow label for assessing current code without an
implementation PR. No type implies `qa`. Work-type mappings cannot use `qa` or
sensitive merge-policy labels (`auth`, `security`, `billing`, `payments`, `money`,
`schema`, `destructive`, `migration`); names containing these separate word tokens
are also refused. Risk/source labels and optional context remain auxiliary.
Type assignment does not change dispatch order, Priority meanings, lanes,
worker settings, run limits, SHA/base/task/config pins or merge authority.

## Reuse policy and explicit mappings

SFC reads **all** repository label pages and complete Project field/view/saved
visibility inventories. Default name aliases must match exactly after case
folding; there is no substring or natural-language guessing. Automatic reuse
accepts a null/empty description, the corresponding bundled type description
(see `scripts/sfc_lib/work_types.py`), or these GitHub defaults:

| Type | Also accepted description (exact) |
|---|---|
| Bug | `Something isn't working` |
| Feature | `New feature or request` |
| Documentation | `Improvements or additions to documentation` |

Thus the existing `bug`, `enhancement` and `documentation` convention is reused
without renaming, recoloring or rewriting descriptions. For Feature, when both
`feature` and `enhancement` have accepted descriptions, prefer `feature`
(case-insensitive), independent of inventory order. A single compatible alias
is reused. A valid explicit mapping always wins, including one selecting
`enhancement`. Check every candidate's meaning before automatic selection:
an unknown/conflicting description still requires focused clarification or an
explicit approved mapping. Do not ask the user to choose equivalent spelling.
Other multiple aliases, unknown descriptions or custom names require an explicit
user-approved mapping. Missing mapped identities and mismatched pins halt; setup does not
rebind stale IDs or create a replacement for an invalid mapping. Document the
semantic equivalence and its evidence, then set all five entries in the existing
`.sfc/config.json` while stopped. Example shape (replace every placeholder with
the live label's exact values; description can be null):

```json
"work_types": {
  "Bug": {"id": "<bug-node-id>", "name": "bug", "description": "Something isn't working"},
  "Feature": {"id": "<enhancement-node-id>", "name": "enhancement", "description": "New feature or request"},
  "Refactor": {"id": "<refactor-node-id>", "name": "refactor", "description": "<exact-live-description>"},
  "Documentation": {"id": "<docs-node-id>", "name": "documentation", "description": "Improvements or additions to documentation"},
  "Maintenance": {"id": "<maintenance-node-id>", "name": "maintenance", "description": "<exact-live-description>"}
}
```

REST `node_id` is the mapping's `id`, not the numeric REST label ID. Every ID and
case-folded name must be distinct. Known alias meanings cannot be mapped to a
different type, and reserved workflow/policy names cannot be mapped at all.
Known descriptions for a different type are also refused. Explicit mapping may
select one existing label amid multiple aliases; it does
not erase the others. Any recognized alternate type alias on a newly created
issue counts as an extra type and prevents completion.

## Authorized setup and saved cards

Use the usual `python3 "<plugin>/scripts/sfc.py" --root "<target>"` prefix:

```sh
python3 "<plugin>/scripts/sfc.py" --root "<target>" type-setup
python3 "<plugin>/scripts/sfc.py" --root "<target>" type-setup --apply
```

New onboarding includes this setup. Existing projects upgrade only on an
explicit setup request: `type-setup --apply` creates only missing type labels
and appends the built-in Labels field ID to saved Factory card visibility;
`dashboard --apply` performs full dashboard setup including Priority and types.
Type-only setup needs an existing Factory board view. It preserves configuration,
all existing labels and issue labels, fields, option IDs/values, custom views,
filters, columns/grouping and existing visibility order. It reads the saved result
back and compares it with the intended narrow change. Matching setup is
idempotent, with no remote writes or new receipts. It never reclassifies existing
tickets. Read-only status/doctor/dashboard/type-setup and creation preflight
never create/recolor/rename/delete labels or change views.

Incomplete/ambiguous writes halt and preserve `.sfc/type-setup/*.json`: before
inventories/view, write intent/history, known created label identities and
observed readback/error. A subsequent apply cannot make more writes while an
incomplete or unfinished receipt remains unresolved (including abrupt process
exit after an intent was saved). Inspect remote state first. If the live
setup already matches, a repeat may verify it read-only without changing that
receipt. Otherwise, reconcile manually within explicit setup authority, retain
the receipt in an archive subdirectory after inspection, then explicitly apply
only remaining missing setup. Do not archive evidence merely to retry an
uncertain write. No rollback, automatic retry or installed-cache edit occurs.

## Creation and observable changes

```sh
python3 "<plugin>/scripts/sfc.py" --root "<target>" create-ticket \
  --request-id "unique-stable-intent" --title "Selected work" \
  --body-file "<prepared-body.md>" --type Feature --priority P2 \
  --reason "Actual request, supporting evidence and priority reason" \
  --unknowns "Confidence and unsupported dimensions"
```

Required assessed type/Priority/reason/unknowns, verified repository, live label
mapping and saved Labels/Priority visibility are checked before posting. The
initial issue POST sends exactly one mapped type label. Optional repeated
`--label <exact-existing-name>` arguments add explicitly requested auxiliary
labels; another type alias is refused. Missing auxiliary labels are refused
before POST. Use `--label qa` only when QA-only workflow was explicitly requested.
Sensitive labels are allowed as explicit auxiliary context, never as types.

The supervisor journals intent, reads actual issue labels, enrolls the actual
issue node ID, writes explicit Backlog/Priority, then verifies issue/body/title,
repository/project/item identity, field values and actual labels before
`complete: true`. Body-only type/priority is incomplete. Type-label identity,
name, description and color, all known type aliases, Labels field identity and
saved visibility are pinned across creation checks. Setup additionally pins
the full repository label inventory across its mutations. Observable changes
halt subsequent writes/completion. Unrelated labels added by humans remain
untouched. The supervisor never replaces issue labels after the initial POST.
A manual type change/clear, extra type alias, missing assignment or mismatched
readback reports incomplete creation and preserves actual values.

Ticket receipts live at `<common-git-dir>/sfc-tickets/<request-id>.json`, including the assessed
type, live mapping, auxiliary labels, observations and actual known issue/item
identities. Failure attempts read-only identity and label recovery; a lost POST
or enrollment response is never blindly repeated. Reusing a request ID never
writes, even after success. Inspect the receipt and actual issue before recovery;
changing the ID or deleting the receipt is not recovery. GitHub has no atomic
compare-and-swap: preflight/readback detect observable changes, but cannot
prevent or detect changes wholly between observations or after final readback.

Isolated tests in `tests/test_work_types.py` and `tests/test_priority.py` exercise
the real supervisor/CLI paths using temporary repositories and fake transports.
These prove local contracts, not live-board rendering or GitHub permissions.
