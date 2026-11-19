# Supported onboarding and evidence

The bundled onboarding reference and CLI `capabilities` report use this checklist.
Discover routine facts before questions; reuse compatible existing setup. Explain
consequential defaults together, preserve supplied choices and ask only for
missing decisions/authority. Do not ask whether to spell Feature `enhancement`
or `feature` when the [resolver policy](WORK-TYPES.md) decides it.

| Capability / report key | Discovery and completion evidence |
|---|---|
| Repository/board (`repository_board`) | Origin identity, owner/number, matching linked board; reuse before creating; read actual Project ID back |
| Base (`base`) | Remote default or supplied branch; dry-run verifies the selected base |
| Checks (`project_checks`) | Read AGENTS.md, CI and build files; save real commands; doctor/lint/dry-run results are separate from executing tests |
| Stages (`stages`) | Seven canonical stages, IDs preserved, style/order and saved Factory grouping; browser Column by/save remains pending until readback |
| Priority (`priority`) | Compatible meanings and saved visibility read back |
| Work types/Labels (`work_types_labels`) | Complete label inventory and compatible mapping; saved built-in Labels visibility read back |
| Agent/Activity (`agent_activity`) | TEXT fields and saved visibility read back |
| Workers (`workers`) | Sol/High/Fast defaults or explicit choice; verify host account availability before running |
| Merge (`merge_policy`) | Auto policy default or explicit human choice; separate merge authority, CI/no-checks choice and all existing gates |
| Hooks (`hooks`) | Codex /hooks or Settings > Hooks: trusted/enabled versus untrusted/disabled; CLI cannot infer host trust |
| MCP (`mcp`) | Actual native connection's `session_status`; a packaged server or old receipt is not a connected server |
| Watching (`watcher`) | Lifetime default on supported macOS, explicit off/bounded respected; distinguish pending setup, armed and actually observing |

`onboarding_complete` means project configuration is complete. It never proves
native activation. The report includes `host_activation_verified: false`; the
interactive supervisor must separately verify actual host evidence. Report
unavailable tools and exact unfinished steps rather than claiming full readiness.
Untrusted/disabled hooks require the user's Codex trust controls; never bypass them.
`doctor`, `lint` and `run --dry-run` remain required before a requested factory run.
Setup launches no workers. Configuration alone grants no merge/publication
authority; the unified review captures separately authorized scopes explicitly.

## Recommended setup review

Run `onboard --preview` with the discovered normal onboarding arguments, show
`setup_review.capabilities`, then repeat without `--preview` and with
`--expected-review <review_digest>`. The read-only preview binds the choices and
exact configuration to the GitHub host, repository node ID and Project node ID.
The digest is required to add watcher merge authority or GitHub release draft or
publication authority. Direct callers can still apply setup without new grants.
Apply rechecks the reviewed identities before saving authority, even when the
repository name and Project owner/number are unchanged.

The six entries cover watcher, eligible Backlog/Ready pickup by Priority,
automatic merge, GitHub releases, test-result reuse and qualified review.
Each reports `active`, `pending`, `unavailable` or `off`, its choices and fallback.
Active means configured eligibility with required authority, not an observed
watcher, executed check, merged PR or published release. Existing detailed
`capabilities` retain their evidence levels for board and native activation.

Watching offers lifetime, bounded and off (`--no-auto-start`) modes, with the
queue, limits, exclusions and hold label displayed. Use `--no-pickup` to keep
watching while leaving new Backlog and Ready work tickets unclaimed. Saved work
and separately authorized releases can continue, and explicit runs can still
claim scoped work. Auto merge is recommended;
`--allow-merge` captures the displayed watcher scope, while `--merge-policy human`
is an opt-out. Without separate authority merging stays pending. All pinned
evidence, independent assessments, GitHub checks and human gates remain required.
Unsupported lifetime hosts retain other setup and report watching unavailable.

Releases recommend publish mode on github.com in the exact source repository.
Use `--release-config <file>` for a project-owned supported release object and
`--release-authorization <id>` for the reviewed repository/base/mode authority.
The review lists exact commands, artifacts, version source, notes and trigger.
Imported authority is discarded. `--release-mode draft` and `off` are explicit
alternatives. Missing commands or scope leave publication pending. A requested
version-file trigger without authority is displayed as pending with manual
preparation as its effective fallback. If the release object omits
`version_file`, add it or choose the manual trigger and preview again before
authorizing release scope. Other reviewed settings can still be applied.
Package checks and downloaded asset hashes
remain enforced by the existing supervisor release workflow.

Supported standard-library commands receive passing-result reuse by default.
The partitioned SFC suite selects documentation-independent reuse only for its
supported profile; custom commands retain fresh execution. Qualified review is
selected automatically when existing proof matches. Missing/stale proof retains
baseline independent lanes. Choose `--review-efficiency baseline` or
`--efficiency-profile off` to opt out.

Existing projects use read-only `setup-defaults` to preview recommendations and
explicitly apply with `--apply --expected-digest <config_digest>
--expected-proposed-digest <proposed_digest>`. This uses the existing journaled,
locked efficiency migration, preserves opt-outs and saved authority, and never
renews watcher limits. Changed configuration requires explicit watcher re-arm.
Completion and `doctor` expose the same six entries and applicable fallbacks.
The upgrade review reads current Project metadata to validate saved watcher
scope. Doctor and upgrade review report pending watcher admission when the
dashboard needs repair or a manual watcher replaced the pinned window. A legacy
project with no saved watcher choice reports pending and directs the operator to
`autostart --enable`. An explicit opt-out reports off. Doctor reports eligible
lifetime continuation after a normal window or safe suspension, and bounded
eligibility for a new chat after normal completion. Once an interrupted bounded
window expires, doctor and the upgrade review report that it cannot resume
watching, picking up tickets or auto merging within that window. A retained
watcher start intent reports pending; verify live observation and, if the
supervisor is gone, inspect the receipts before explicitly reauthorizing. Use
`session_status` for live observation.
To add or change release scope on an existing project, include the same
`--release-config`, `--release-mode` and `--release-authorization` choices in both
the `setup-defaults` preview and apply commands. Unspecified release settings
and authority are preserved, including when the supplied release configuration
matches the saved scope after the supported package verifier moves into
preflight. New release grants bind the reviewed repository and Project node IDs
and are checked against live identity before processing. `onboard --preview`
and `doctor` check the current credential's push access. For a version-file
trigger, they also read the configured file on the selected merged base and
report a missing, invalid, or unverified file as pending. Add a supported
plain-text version on that base or choose the manual trigger. An explicit
release push permission denial or an invalid version file leaves the automatic
release pending while ordinary ticket pickup continues.
Unresolved release journals and scope changes still require recovery.

Eligible fresh setups and explicitly migrated projects can reuse matching,
immutable project-check evidence through verification, merge and release.
Model and review alternatives require proof for the selected route. An absent
or unmatched proof retains the configured worker preset and standard review.
See [delivery qualification](DELIVERY-QUALIFICATION.md) for the tested workloads,
measured results and coverage limits.

Incomplete setup retains `.sfc/config.json` and `.sfc/onboarding.json` before
dashboard presentation writes. Finish the returned saved-view steps and use
`onboard-finish` under the same command prefix. It verifies the pinned checkout,
common git directory, config digest and Project/routing identity, then applies
only the saved watching choice. Reviewed setups also recheck the repository and
Project node IDs at the watcher authorization boundary. Older pending receipts
retain their saved choices and require an exact match to their saved routing
metadata. `onboard-finish` neither repairs dashboards nor renews an existing
authorization. New stop/authorization/config drift halts; lost enable
responses retain evidence without a blind retry. The pending-choice receipt
protects its one use. After successful finish or an explicit off choice, doctor
and repeated finish assess current configuration, dashboard and authorization.
Watcher admission rechecks the node IDs recorded with a reviewed authorization.
An explicit later configuration change requires separately refreshing enabled
watch authorization with `autostart`; stale authorization remains needs-inspection.
An off choice stays off without requiring authorization. Repeats only read state.
Existing projects without this receipt use explicit repair and
`autostart` commands; there is no silent migration.

The CLI records runtime version, plugin path and hashes of the relevant source
and onboarding reference. The native chat must identify the actually loaded
skill separately. This separates stale skill/package drift from resolver defects.
Local archives include this checklist, reference, runtime and tests. Packaging
does not install, publish, establish native trust or operate any target board.

The [onboarding regression tests](../tests/test_onboarding.py) use temporary projects and fake GitHub adapters.
Broader packaged/native lifecycle acceptance remains issue #57.
