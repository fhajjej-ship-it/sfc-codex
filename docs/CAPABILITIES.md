# SFC capabilities

| Capability | Implementation |
|---|---|
| GitHub Project queue | Projects v2 API, paginated issue inventory, seven SFC Status options |
| Onboard / lint / status / run / stop | `sfc` skill and deterministic Python CLI |
| Per-ticket activity | Two saved Factory card text fields; supervisor-owned lifecycle, safe observed substeps and bounded live-process refresh |
| Explicit priority pickup | Per-run `--include-backlog` plus required `--issue` allowlist; verified Backlog/Ready, mapped P0–P3/creation age/number order, read-only exclusion reasons and journaled/read-back claims; default Ready-only and saved merge policy preserved |
| Opt-in continuous watcher | `watch --start`, `--resume SESSION`, `--dry-run`, `watch-status`, shared `stop`; future Backlog/Ready scope, holds/exclusions, finite durable time/poll/claim/work budgets, one common-git lock |
| Interactive-session watching | Local Codex-owned MCP observation, trusted hooks, finite work windows, and retained attempts and failure records. Available on supported macOS hosts. Verify activation in the project chat. |
| Rolling priority admission | Saved unassigned and fresh work rank by verified P0–P3, creation time and number; freed slots refill while peers execute; same-repo prerequisites gate dispatch |
| Build worktrees / PRs | Per-issue branches/worktrees; supervisor commits, pushes and draft PRs |
| QA / security / review | Separate Codex invocations; project-command exit-code receipts |
| QA-only cards | Assess base without an implementation PR |
| Optional versioned releases | Separate supervisor-validated workflow on the same board; frozen merged source range, automatic shared Target release metadata, isolated packaging and artifact checks, independent lanes, journaled GitHub tag/draft/assets/publication, downloaded checksum verification before Done. Off until configured/authorized; see [release workflow](RELEASES.md). |
| Retry / blocked handling | Finite attempt, time, cycle and no-progress limits; feedback to builder |
| Merge gate | Current head/base/issue/config pins, CI, candidate checks, policy and protected merge or opt-in atomic fast-forward |
| Stop / resume | Cooperative flag, owned process-group shutdown, persistent claims and evidence |
| Problem collection / backlog priority | `sfc-collect` uses available sources; deduplicated findings and open configured Backlog review with reasoned priorities; supervisor `create-ticket` enforces assessed type/priority, one live mapped type label, saved Labels/Priority visibility, Backlog and identity/body/label readback |
| UI refinement | `sfc-ui-refine` uses current Codex browser/native tools, bounded critique loop |
| Visual recap / map | `sfc-visual` generates a self-contained local HTML artifact |
| Git sync | `sfc-git-sync` handles selected commits, merge integration and non-force push |
| Command guards | Codex SessionStart/PreToolUse hooks for common literal secret/path/delete/push mistakes |

Operating boundaries:

- Codex only. No Claude/OpenCode host discovery, Claude usage meter, Workflow
  runtime, `.claude/` files, external skill downloads or automatic model ladder.
- New onboarding defaults to auto policy; explicit human merging remains
  available. Existing saved policies remain unchanged. Auto also needs separate
  run/watch merge authority; policy alone grants no scope or session. Protected
  mode requires strict GitHub protection; opt-in local verification compares both
  refs atomically and fast-forwards only. See [local merge](LOCAL-MERGE.md).
- Uses its own project field to avoid rewriting existing Status option IDs.
- No automatic database migrations or skill recalibration. Those are separate
  explicitly scoped operations, not prerequisites to a basic factory run.
- Hooks are local/manual-install features requiring Codex trust. Literal guards
  are not equivalent to enforcing every shell/MCP/Python action.
- No configuration/state migration or implicit background startup. Ordinary runs
  exit when drained/waiting; opt-in foreground watch sessions poll until stopped
  or a finite session/work budget is spent. The host and process must stay running.
  Separately authorized interactive-session watching uses the bundled local MCP
  runtime; ordinary turn completion leaves it alive. See [runtime limits](SESSION-RUNTIME.md).
- The four auxiliary workflows are skills executed by Codex in the active chat;
  SFC does not bundle an analytics connector, browser or separate UI renderer.
- Only GitHub issues belonging to the configured repository are queue cards;
  draft notes, pull-request cards and cross-repository dependency syntax are
  intentionally excluded.

The [onboarding checklist](ONBOARDING.md) distinguishes completed setup from
observed host activation. The [release workflow](RELEASES.md) describes the
evidence needed to verify a published package.
