"""Public explanations from supervisor facts, never worker text or diagnostics.

These descriptors are display data only. Routing and authority stay with callers.
Only bounded integers may be interpolated; no paths, labels, commands or prose.
"""
from __future__ import annotations

from .common import Halt

WATCHER_ADMISSION_DEFERRED = "Admission deferred after live eligibility changed"
RECOVERY_READ_WAIT = "Waiting for a fresh GitHub read before automatic recovery"
RECOVERY_ITEM_MISSING = "Recovered Project item missing; explicit recovery required"

TEXT = {
    "automatic_recovery_exhausted": "The saved automatic recovery allowance is spent. The ticket remains Blocked; inspect retained checks and recovery attempts before explicit recovery.",
    "automatic_recovery_drift": "Recovery found changed source, scope or retained evidence. The ticket remains Blocked for inspection; no worker was dispatched.",
    "automatic_recovery_human": "This blocker needs a human decision or is outside supported automatic recovery. The ticket remains Blocked with its saved evidence.",
    "automatic_recovery_read": "A GitHub read did not finish. The watcher will recheck the ticket on a later poll.",
    "dependency": "A prerequisite is still open. Wait for its verified closure before starting this issue.",
    "human": "Human merge decision needed for the reviewed PR. A human must inspect its pinned review evidence and decide whether to merge.",
    "external_merge": "External merge completion is unverified. Inspect the retained PR, intended base, reviewed head and issue-closure evidence; resolve the recorded reconciliation gap before completion. No merge retry or issue-close write will be attempted.",
    "human_policy": "Project policy requires a human merge decision for the reviewed PR. Ask a human to inspect the pinned review evidence and decide whether to merge.",
    "size": "The reviewed PR exceeds the automatic merge size limit. A human merge decision for this reviewed PR is needed; the limit stays unchanged.",
    "human_only": "This issue is marked human-only. A human must inspect the reviewed PR and decide whether to merge.",
    "sensitive": "The reviewed PR touches a sensitive policy category. A human must inspect its review evidence and decide whether to merge.",
    "merge_authority": "This run has no automatic merge authority. A human must inspect the reviewed PR and decide whether to merge.",
    "ci": "GitHub checks have not passed. Inspect the PR checks and resolve failed or missing results; merging remains gated.",
    "ci_missing": "No GitHub checks were reported and this project requires them. Have the repository maintainer investigate the missing checks; merging remains gated.",
    "ci_failed": "A GitHub check failed. Inspect the PR check results and resolve the failure before merge can proceed.",
    "ci_pending": "GitHub checks are still pending. Wait for their results; merge will still require all existing gates.",
    "policy": "Required merge protection is not verified. Ask the repository maintainer to inspect the configured protection and retained evidence; no merge was attempted.",
    "strict_protection": "Automatic merging requires GitHub strict up-to-date branch protection.",
    "criteria": "Issue criteria or dependency references are malformed. Correct the issue's observable acceptance criteria and same-repository prerequisites before explicitly requeueing.",
    "failed": "A worker or supervisor operation failed. Inspect retained local receipts and worktree, resolve the cause and confirm supported recovery before any authorized resume.",
    "worker_exit": "A worker exited with an error or no result. Inspect retained local output and its dispatch budget before any explicitly authorized supervisor resume; no automatic retry.",
    "worker_result": "Worker output did not meet the result contract. Inspect retained local output and dispatch budget before any explicitly authorized supervisor resume; output-only repair and automatic retry are unsupported.",
    "output_budget": "The worker output budget is exhausted. Inspect retained dispatch receipts and worktree; automatic recovery and resetting counters are unsupported.",
    "timeout": "A process time limit was reached. Work is preserved. Inspect retained receipts and remaining budgets before deciding on supported, explicitly authorized recovery.",
    "attempts": "The findings attempt budget is exhausted. Inspect retained lane findings and worktree for a supported recovery decision; do not retry this exhausted execution or reset counters.",
    "findings": "A review lane reported findings. Inspect the retained local findings; the supervisor can return the issue to Build only within its existing scope and remaining budgets.",
    "worker_blocked": "A worker reported a blocker. Inspect retained local evidence and resolve its prerequisite before deciding on supported, explicitly authorized recovery.",
    "check_failed": "Project checks failed. Inspect retained local check receipts and logs, resolve the failure and obtain fresh passing evidence before review or merge.",
    "stop": "Stop requested; work is preserved. Inspect retained state and remaining budgets before an explicitly authorized resume.",
    "interrupted": "The host interrupted this session. Inspect retained state; a supported resume uses the original deadline and counters only, if budget remains.",
    "limit": "A finite run budget is exhausted; work is preserved. Inspect retained receipts. Continuing needs a separate explicit decision under the existing recovery rules; resuming does not renew the spent budget.",
    "watch-time-limit": "The watcher time budget is exhausted. Inspect retained receipts; the original session cannot renew its deadline. Any further session requires separate authorization under existing lifecycle rules.",
    "watch-polls-limit": "The watcher poll budget is exhausted. Inspect retained receipts; resuming cannot renew the spent allowance. Any further session requires separate authorization under existing lifecycle rules.",
    "watch-claims-limit": "The watcher claim budget is exhausted. Inspect retained claims; resuming cannot renew the spent allowance. Any further session requires separate authorization under existing lifecycle rules.",
    "cycle-limit": "The work cycle budget is exhausted. Inspect retained work and receipts; resuming cannot renew it. Further work requires an explicit decision under existing recovery rules.",
    "no-progress-limit": "The no-progress budget is exhausted. Inspect retained failures and resolve the cause before an explicit recovery decision; resuming cannot renew it.",
    "capacity": "Worker capacity is occupied. Wait for an existing work slot; this issue has not been dispatched.",
    "admission_deferred": "The issue changed during admission. The watcher will recheck it if another poll is permitted.",
    "recovery": "Saved work requires explicit reconciliation. Inspect retained receipts and current remote state before deciding on supported recovery; no blind retry.",
    "excluded": "This issue is outside the current pickup scope or is held. Leave it untouched unless the authorized scope is explicitly changed.",
    "waiting": "Work is waiting for an eligible next step. Inspect the recorded per-issue reasons; no completion is implied.",
    "drained": "No eligible work remains in this run. Inspect per-issue handoffs for any separate human decisions; this does not mean every issue completed.",
    "completed": "This execution is recorded as completed. No new worker will start for it; preserve its completion evidence.",
    "closed": "This issue is closed and excluded from pickup. No worker will start for it.",
    "done_stage": "The issue is already in Done and is excluded from pickup.",
    "unknown": "Current liveness cannot be established. Inspect retained state and the supervisor lock; do not signal a stored process ID.",
}


class ExplainedHalt(Halt):
    """Retain local diagnostics while selecting fixed public failure text."""
    def __init__(self, message, action, values):
        super().__init__(message)
        self.action, self.values = action, values


def reason(code, **facts):
    """Validate and normalize an allowlisted descriptor before public use."""
    if not isinstance(code, str) or code not in TEXT:
        raise Halt("Invalid public reason key")
    allowed = {"size": {"changed", "limit", "pr"}, "dependency": {"issues"},
               **{key: {"pr"} for key in ("human_policy", "human_only", "sensitive", "merge_authority")},
               "attempts": {"spent", "limit"}, "output_budget": {"spent", "limit"}}
    if set(facts) - allowed.get(code, set()):
        raise Halt("Invalid public reason facts")
    clean = {}
    for key, value in facts.items():
        values = value if key == "issues" and isinstance(value, list) else [value]
        if (not values or len(values) > 20 or
                any(type(n) is not int or not 0 <= n <= 10**12 for n in values)):
            raise Halt("Invalid public reason number")
        clean[key] = sorted(set(values)) if key == "issues" else value
    return {"code": code, "facts": clean}


def render(value):
    value = reason(value["code"], **value.get("facts", {}))
    code, facts = value["code"], value["facts"]
    detail = ""
    if code == "size" and {"changed", "limit", "pr"} <= facts.keys():
        detail = f"PR #{facts['pr']}: {facts['changed']:,} changed lines; configured automatic merge limit: {facts['limit']:,}. "
    elif "pr" in facts:
        detail = f"PR #{facts['pr']}: "
    elif code == "dependency" and facts.get("issues"):
        detail = "Open prerequisites: " + ", ".join(f"#{n}" for n in facts['issues']) + ". "
    elif code in ("attempts", "output_budget") and {"spent", "limit"} <= facts.keys():
        detail = f"Spent {facts['spent']}/{facts['limit']}. "
    return detail + TEXT[code]


def render_many(values):
    if not isinstance(values, list) or not 1 <= len(values) <= 12:
        raise Halt("Invalid public reason list")
    return " ".join(render(value) for value in values)


def outcome_reason(outcome):
    code = {"stopped": "stop", "halted": "failed"}.get(outcome, outcome)
    return render(reason(code)) if code in TEXT else None


def explain_wait(wait, record=None, max_attempts=None):
    """Add safe display text without changing planner results or extra queries."""
    record = record or {}
    if wait.get("blocked_by"):
        # Bound the public list; the full prerequisite inventory stays local.
        values = [reason("dependency", issues=wait["blocked_by"][:20])]
    else:
        values = []
        for raw in wait.get("reasons", []):
            if raw == "Saved human handoff; waiting for human merge" and record.get("handoff_reasons"):
                values.extend(record["handoff_reasons"])
                continue
            if raw == "Saved completed/blocked execution or attempt limit; explicit recovery required":
                if record.get("status") == "Done":
                    values.append(reason("completed"))
                    continue
                spent = record.get("attempts", 0)
                if max_attempts is not None and spent >= max_attempts:
                    values.append(reason("attempts", spent=spent, limit=max_attempts))
                    continue
            if raw.startswith("Open prerequisites: "):
                # Only the supervisor's strict numeric dependency message matches.
                import re
                if re.fullmatch(r"Open prerequisites: #[1-9][0-9]*(?:, #[1-9][0-9]*)*", raw):
                    nums = [int(n) for n in re.findall(r"#([0-9]+)", raw)][:20]
                    values.append(reason("dependency", issues=nums))
                    continue
            code = {
                "Automatic recovery allowance exhausted; explicit recovery required": "automatic_recovery_exhausted",
                "Recovery source or scope drift; inspect retained evidence": "automatic_recovery_drift",
                "Recovery source or retained evidence drift; explicit recovery required": "automatic_recovery_drift",
                RECOVERY_ITEM_MISSING: "automatic_recovery_drift",
                "Human decision or unsupported blocker; explicit recovery required": "automatic_recovery_human",
                RECOVERY_READ_WAIT: "automatic_recovery_read",
                "Saved human handoff; waiting for human merge": "human",
                "Waiting for successful GitHub checks; no work wave dispatched": "ci",
                "Waiting for successful GitHub checks; no worker dispatched": "ci",
                "Executing in an occupied worker slot": "capacity",
                "Waiting for max_workers capacity": "capacity",
                WATCHER_ADMISSION_DEFERRED: "admission_deferred",
                "Watcher hold label or explicit exclusion": "excluded",
                "Outside explicit run issue scope": "excluded",
                "Issue is not open": "closed",
                "Issue is already in Done": "done_stage",
                "missing ## Acceptance criteria": "criteria",
                "acceptance criteria need observable bullet points": "criteria",
                "issue depends on itself": "criteria",
                "Issue depends on itself": "criteria",
                "Blocked by must use same-repository #issue references": "criteria",
                "Malformed dependency: use one same-repository #issue per bullet": "criteria",
                "Use one ## Blocked by section": "criteria",
            }.get(raw, "recovery")
            value = reason(code)
            if value not in values:
                values.append(value)
        values = values[:12] or [reason("recovery")]
    return {**wait, "explanation": render_many(values)}
