"""Offline transport for recorded, model-authored collect workflow decisions.

This adapter has no rubric, ordering, scope selection or writeback policy. The
JSON transcript supplies those decisions. It never imports the factory or calls
GitHub/Codex. Replay proves the recorded decisions against fake state, not the
accuracy of an arbitrary future model invocation.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/backlog-prioritization-replay.json"
REFERENCE = ROOT / "skills/sfc-collect/references/prioritization.md"
MARKER = "<!-- sfc:backlog-priority -->"


class FakeGitHub:
    """Small stateful fake API: targeted mutations and independent readbacks."""

    def __init__(self, state, allowed_writes=()):
        self.state = copy.deepcopy(state)
        self.trace = []
        self.read_counts = {}
        self.allowed_writes = {tuple(entry) for entry in allowed_writes}

    def call(self, operation, **args):
        entry = {"operation": operation, "input": copy.deepcopy(args)}
        self.trace.append(entry)
        try:
            result = self._call(operation, args)
        except (TimeoutError, PermissionError) as exc:
            entry["error"] = str(exc)
            return {"error": str(exc)}
        entry["output"] = copy.deepcopy(result)
        return copy.deepcopy(result)

    def _call(self, op, args):
        if op in {"create_issue", "add_to_project", "set_priority", "create_comment", "update_comment"}:
            if (op, args["number"]) not in self.allowed_writes:
                raise PermissionError(f"write outside scenario authority: {op} #{args['number']}")
        key = f"{op}:{args.get('number', args.get('after', 'first'))}"
        self.read_counts[key] = self.read_counts.get(key, 0) + 1
        if self.state.get("fail_reads", {}).get(key) == self.read_counts[key]:
            raise PermissionError(f"fake access denied for {key}")
        if op == "project":
            return self.state["project"]
        if op == "items":
            pages = self.state["pages"]
            cursor = args.get("after")
            index = 0 if cursor is None else int(cursor)
            return {"nodes": [self.state["items"][str(n)] for n in pages[index]],
                    "pageInfo": {"hasNextPage": index + 1 < len(pages),
                                 "endCursor": str(index + 1)}}
        if op == "issue":
            return self.state["items"].get(str(args["number"]), {"error": "not found"})
        if op == "create_issue":
            number = args["number"]
            assert str(number) not in self.state["items"]
            value = item(number, args["title"], body=args["body"], stage=None)
            self.state["items"][str(number)] = value
            return value
        value = self.state["items"][str(args["number"])]
        if op == "add_to_project":
            assert args["project_id"] == self.state["project"]["id"]
            value["stage"] = "Backlog"
            self.state["pages"][0].append(args["number"])
        elif op == "set_priority":
            field = self.state["project"]["fields"]["priority-test"]
            assert args["field_id"] == "priority-test"
            assert args["option_id"] in field["options"]
            assert args["item_id"] == value["item_id"]
            value["priority"] = args["option_id"]
        elif op == "create_comment":
            comment_id = f'comment-{args["number"]}-{len(value["comments"]) + 1}'
            comment = {"id": comment_id, "author": "sfc-test", "body": args["body"],
                       "url": value["url"] + "#" + comment_id}
            value["comments"].append(comment)
            if args.get("timeout_after_accept"):
                raise TimeoutError("timeout after accepted write; inspect before retry")
            return comment
        elif op == "update_comment":
            comment = next(c for c in value["comments"] if c["id"] == args["comment_id"])
            comment["body"] = args["body"]
            return comment
        else:
            raise AssertionError(f"Unexpected API operation: {op}")
        return value


def item(number, title, **overrides):
    return {"number": number, "item_id": f"item-{number}", "id": f"issue-{number}",
            "url": f"https://github.com/example/app/issues/{number}",
            "title": title, "body": "", "repository": "example/app", "type": "Issue",
            "state": "OPEN", "stage": "Backlog", "priority": None,
            "team": "blue-test", "labels": ["bug", "customer"], "comments": [], **overrides}


def state(items, priority=True, pages=None):
    fields = {"stage-test": {"name": "Pipeline", "options": {
        f"{name.lower()}-test": name for name in
        ("Backlog", "Ready", "Building", "QA", "Review", "Blocked", "Done", "Parked")}},
        "team-test": {"name": "Team", "options": {"blue-test": "Blue"}}}
    if priority:
        fields["priority-test"] = {"name": "Priority", "options": {
            "high-test": "High", "normal-test": "Normal", "low-test": "Low"},
            "meanings": {"High": "P1", "Normal": "P2", "Low": "P3"}}
    return {"project": {"id": "project-test", "fields": fields,
                        "views": [{"id": "view-test", "layout": "Board",
                                   "group": "Pipeline", "filter": "label:customer"}]},
            "items": {str(i["number"]): i for i in items},
            "pages": pages if pages is not None else [[i["number"] for i in items]]}


def annotation(assessment):
    # Formatting only. All rubric decisions and prose come from the transcript.
    content = {k: v for k, v in assessment.items() if k != "verified_annotation_urls"}
    return MARKER + "\n" + json.dumps(content, sort_keys=True)


def replay(case):
    """Execute literal recorded calls; preserve full before/after/tool evidence."""
    from test_factory import BASE_CONFIG
    with tempfile.TemporaryDirectory(prefix="sfc-priority-replay-") as directory:
        target = Path(directory)
        subprocess.run(["git", "init", "-q", str(target)], check=True)
        subprocess.run(["git", "-C", str(target), "remote", "add", "origin",
                        "https://github.com/example/app"], check=True)
        config = {**copy.deepcopy(BASE_CONFIG), "project": {"owner": "example", "number": 7},
                  "status_field": "Pipeline"}
        (target / ".sfc").mkdir()
        (target / ".sfc/config.json").write_text(json.dumps(config))
        adapter = FakeGitHub(case["before"], case["allowed_writes"])
        before = copy.deepcopy(adapter.state)
        for call in case["calls"]:
            adapter.call(call["operation"], **call.get("input", {}))
        readout = copy.deepcopy(case["readout"])
        for row in readout:
            # Attach only links actually returned by the last issue readback;
            # the fake's internal state is not proof of a successful readback.
            reads = [t for t in adapter.trace if t["operation"] == "issue"
                     and t["input"]["number"] == row["number"]]
            value = reads[-1].get("output", {}) if reads else {}
            row["verified_annotation_urls"] = [c["url"] for c in value.get("comments", [])
                                                if c["body"] == annotation(row)]
        result = {"case": case["name"], "request": case["request"],
                  "origin": subprocess.check_output(["git", "-C", str(target),
                                                      "remote", "get-url", "origin"], text=True).strip(),
                  "config": json.loads((target / ".sfc/config.json").read_text()),
                  "prior_receipts": case.get("prior_receipts", {}),
                  "before": before, "readout": readout,
                  "trace": adapter.trace, "after": adapter.state,
                  "completion": case["completion"]}
        (target / ".sfc/replay.json").write_text(json.dumps(result))
        return json.loads((target / ".sfc/replay.json").read_text())


def record():
    cases = json.loads(FIXTURE.read_text())["cases"]
    results = [replay(case) for case in cases]
    receipt = {"kind": "offline recorded decision replay; no live service or new model invocation",
               "workflow_sha256": hashlib.sha256(REFERENCE.read_bytes()).hexdigest(),
               "fixture_sha256": hashlib.sha256(FIXTURE.read_bytes()).hexdigest(),
               "results": results}
    destination = FIXTURE.with_name("backlog-prioritization-receipt.json")
    destination.write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps({"receipt": str(destination), "cases": len(results),
                      "calls": sum(len(r["trace"]) for r in results)}))


if __name__ == "__main__":
    record()
