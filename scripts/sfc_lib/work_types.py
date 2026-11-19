"""Supervisor-owned work-type label discovery, explicit setup and readback.

No issue-label repair is performed: labels are assigned only in the initial
issue POST. Readback failures preserve human changes and latch ticket receipts.
"""
from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

from .common import Halt, now, read_json, write_json

TYPES = {
    "Bug": ("bug", "A supported defect in existing intended behavior.", "d73a4a"),
    "Feature": ("feature", "New or expanded user-visible behavior.", "a2eeef"),
    "Refactor": ("refactor", "Internal restructuring without intended behavior change.", "c5def5"),
    "Documentation": ("documentation", "Documentation or explanatory content changes.", "0075ca"),
    "Maintenance": ("maintenance", "Dependencies, configuration, CI and routine upkeep.", "ededed"),
}
ALIASES = {
    "Bug": {"bug"}, "Feature": {"feature", "enhancement"},
    "Refactor": {"refactor"}, "Documentation": {"documentation", "docs"},
    "Maintenance": {"maintenance", "chore"},
}
DESCRIPTIONS = {
    "Bug": {"Something isn't working"}, "Feature": {"New feature or request"},
    "Documentation": {"Improvements or additions to documentation"},
}
# These names affect workflow/merge policy, so an explicit mapping cannot confer
# authority to assign them. Keep independent of factory dispatch and policy code.
RESERVED = {"qa", "auth", "security", "billing", "payments", "money", "schema",
            "destructive", "migration"}


def label_record(label):
    if (not isinstance(label, dict) or
            any(not isinstance(label.get(k), str) or not label[k].strip() for k in ("node_id", "name", "color")) or
            "description" not in label or
            label.get("description") is not None and not isinstance(label["description"], str)):
        raise Halt("Incomplete label identity/name/description/color inventory")
    return {"id": label["node_id"], "name": label["name"],
            "description": label.get("description"), "color": label["color"]}


def labels(gh, issue=None):
    endpoint = f"repos/{gh.c['repo']}/" + (f"issues/{issue}/labels" if issue is not None else "labels")
    pages = gh.call("api", endpoint + "?per_page=100", "--paginate", "--slurp")
    if not isinstance(pages, list) or not pages or any(not isinstance(page, list) for page in pages):
        raise Halt("Incomplete paginated label inventory")
    records = [label_record(label) for page in pages for label in page]
    if (len({r["id"] for r in records}) != len(records) or
            len({r["name"].casefold() for r in records}) != len(records)):
        raise Halt("Duplicate or conflicting label inventory")
    return sorted(records, key=lambda r: r["id"])


def validate_mapping(mapping):
    if mapping is None:
        return
    if not isinstance(mapping, dict) or set(mapping) != set(TYPES):
        raise Halt("work_types must map all five types to pinned id/name/description records")
    for kind, record in mapping.items():
        if (not isinstance(record, dict) or set(record) != {"id", "name", "description"} or
                any(not isinstance(record[k], str) or not record[k].strip() for k in ("id", "name")) or
                record["description"] is not None and not isinstance(record["description"], str)):
            raise Halt("work_types requires exact label id/name/description pins")
        name = record["name"].casefold()
        if RESERVED.intersection(re.findall(r"[\w]+", name)):
            raise Halt("Workflow/merge-policy labels cannot be work-type mappings")
        if any(name in aliases for other, aliases in ALIASES.items() if other != kind):
            raise Halt("Work-type mapping conflicts with a known alias meaning")
        if any(record["description"] in {TYPES[other][1], *DESCRIPTIONS.get(other, set())}
               for other in TYPES if other != kind):
            raise Halt("Work-type mapping conflicts with a known description meaning")
    if len({r["id"] for r in mapping.values()}) != 5 or len({r["name"].casefold() for r in mapping.values()}) != 5:
        raise Halt("Work-type mappings must use five distinct labels")


def resolve(records, config):
    mapping = config.get("work_types")
    validate_mapping(mapping)
    chosen = {}
    for kind, (name, description, color) in TYPES.items():
        candidates = [r for r in records if r["id"] == mapping[kind]["id"]] if mapping else [
            r for r in records if r["name"].casefold() in ALIASES[kind]]
        if mapping:
            if len(candidates) != 1 or any(candidates[0][k] != v for k, v in mapping[kind].items()):
                raise Halt("Stale work-type label mapping; inspect live identity/name/description")
        elif not candidates:
            continue
        elif any(r["description"] not in {None, "", description, *DESCRIPTIONS.get(kind, set())}
                 for r in candidates):
            raise Halt("Unsupported work-type description; document an explicit work_types mapping")
        elif len(candidates) != 1:
            if kind != "Feature":
                raise Halt("Ambiguous work-type aliases; document an explicit work_types mapping")
            # All Feature meanings were checked above. Prefer the bundled spelling
            # regardless of API order; explicit pinned mappings still take priority.
            candidates = sorted(candidates, key=lambda r: r["name"].casefold() != name)
        chosen[kind] = candidates[0]
    return chosen


def setup(gh, apply=False):
    """Explicit setup creates missing labels and appends only Labels visibility."""
    from .priority import inventory, validate_routing

    meta = gh.metadata()

    def inspect():
        fields = inventory(gh, meta["id"])
        validate_routing(fields, gh.c, meta)
        builtins = [f for f in fields if f.get("dataType") == "LABELS"]
        if len(builtins) != 1:
            raise Halt("Need exactly one built-in Labels field; no label/view write attempted")
        records = labels(gh)
        return {"labels_field_id": builtins[0]["id"], "mapping": resolve(records, gh.c), "inventory": records}

    scope = inspect()
    view = gh.factory_view(gh.dashboard_data())
    if not view or view["layout"] != "BOARD_LAYOUT":
        return {"ready": False, "pending": "Run dashboard --apply to prepare the saved Factory view", **scope}
    path = Path(gh.root) / ".sfc/type-setup" / (uuid.uuid4().hex + ".json")
    journal = {}

    def record(state, **details):
        journal.update(state=state, time=now(), repo=gh.c["repo"], project=meta["id"], **details)
        journal.setdefault("history", []).append({"state": state, "time": now(), **details})
        write_json(path, journal)

    def before_write():
        if inspect() != scope or gh.factory_view(gh.dashboard_data()) != view:
            raise Halt("Work-type labels or saved view changed before setup write")
        # Never blindly retry a prior ambiguous setup mutation. A matching
        # repeat needs no writes; otherwise an operator must inspect/archive the
        # failure receipt after reconciling actual remote state.
        for previous in path.parent.glob("*.json"):
            if previous != path and read_json(previous).get("state") != "complete":
                raise Halt(f"Unresolved work-type setup receipt {previous}; inspect before repair")

    try:
        for kind, (name, description, color) in TYPES.items():
            if kind in scope["mapping"] or not apply:
                continue
            before_write()
            record("creating_label", before=scope, before_view=view, next_label={"name": name, "description": description, "color": color})
            made = label_record(gh.call("api", f"repos/{gh.c['repo']}/labels", "--method", "POST", "--input", "-",
                                       input=json.dumps({"name": name, "description": description, "color": color})))
            record("label_created", created_label=made)
            fresh = inspect()
            if (made != {"id": made["id"], "name": name, "description": description, "color": color} or
                    fresh["mapping"] != {**scope["mapping"], kind: made} or
                    fresh["inventory"] != sorted(scope["inventory"] + [made], key=lambda r: r["id"]) or
                    fresh["labels_field_id"] != scope["labels_field_id"]):
                raise Halt("Work-type label creation/preservation readback failed")
            scope = fresh
        missing = sorted(set(TYPES) - scope["mapping"].keys())
        visible = [f["id"] for f in view["configuration"]["visibleFields"]["nodes"]]
        wanted = visible + ([scope["labels_field_id"]] if scope["labels_field_id"] not in visible else [])
        if apply and not missing and wanted != visible:
            before_write()
            record("setting_visibility", before=scope, before_view=view, wanted=wanted)
            gh.graphql("""mutation($input:UpdateProjectV2ViewInput!) {
              updateProjectV2View(input:$input) { projectV2View { id } }
            }""", {"input": {"viewId": view["id"], "configuration": {"visibleFieldIds": wanted}}})
            saved = gh.factory_view(gh.dashboard_data())
            expected_view = {**view, "configuration": {**view["configuration"], "visibleFields": {
                **view["configuration"]["visibleFields"], "nodes": [{"id": v} for v in wanted]}}}
            if saved != expected_view:
                raise Halt("Saved Labels visibility/preservation readback failed")
            view, visible = saved, wanted
        if inspect() != scope or gh.factory_view(gh.dashboard_data()) != view:
            raise Halt("Work-type setup changed during verification")
        ready = not missing and scope["labels_field_id"] in visible
        if journal:
            record("complete", verified=scope, saved_view=view)
        return {"ready": ready, **scope, **({"receipt": str(path)} if journal else {}), "pending": None if ready else
                "Explicitly run type-setup --apply to prepare missing work types/Labels card visibility"}
    except (Halt, OSError, ValueError, KeyError, TypeError) as exc:
        if journal:
            # Keep raw identities even if semantic resolution now fails. Each
            # read is independent; a meaning change must not hide other evidence.
            for key, readback in (("observed_labels", lambda: labels(gh)),
                                  ("observed_fields", lambda: inventory(gh, meta["id"])),
                                  ("observed_view", lambda: gh.factory_view(gh.dashboard_data())),
                                  ("observed", inspect)):
                try:
                    journal[key] = readback()
                except (Halt, OSError, ValueError, KeyError, TypeError) as error:
                    journal.setdefault("readback_errors", {})[key] = str(error)
            record("incomplete", failed_stage=journal["state"], error=str(exc))
            raise Halt(f"{exc}; inspect setup receipt {path}; no automatic retry") from exc
        raise


def creation_scope(report):
    # Other repository labels may be added independently. Pin all mapped labels
    # and aliases, including color, but do not halt on unrelated inventory edits.
    names = set().union(*ALIASES.values())
    ids = {r["id"] for r in report["mapping"].values()}
    return {k: v for k, v in report.items() if k != "inventory"} | {
        "type_inventory": [r for r in report["inventory"] if r["id"] in ids or r["name"].casefold() in names]}


def check_issue(gh, issue_number, scope, kind, auxiliary):
    actual = labels(gh, issue_number)
    chosen = scope["mapping"][kind]
    names = set().union(*ALIASES.values())
    ids = {r["id"] for r in scope["mapping"].values()}
    types = [r for r in actual if r["id"] in ids or r["name"].casefold() in names]
    if types != [chosen] or not set(auxiliary).issubset({r["name"] for r in actual}):
        raise Halt("Missing/conflicting/manual work-type or requested auxiliary label readback; preserving labels")
    return actual
