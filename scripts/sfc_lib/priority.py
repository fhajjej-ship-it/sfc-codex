"""Supervisor-only Priority setup and journaled Backlog ticket creation."""
from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from urllib.parse import urlencode

from .common import Halt, STATUSES, now, write_json
from . import work_types
from .ticket_coordination import MARKER, MARKER_START, creation_lock, recorded_request, ticket_directory
from .ticket_validation import lint_card

LEVELS = {
    "P0": ("P0 Immediate", "Ongoing critical harm or imminent sourced critical deadline; no safe workaround."),
    "P1": ("P1 High", "Supported core-workflow failure, substantial recurring impact or its prerequisite."),
    "P2": ("P2 Normal", "Supported bounded impact with a workaround or useful improvement without demonstrated urgency."),
    "P3": ("P3 Low", "Cosmetic or optional improvement with demonstrated limited impact."),
}


def connection(fetch):
    """Read every page; reject repeated cursors and duplicate identities."""
    after, seen, nodes, cursors = None, set(), [], set()
    while True:
        page = fetch(after)
        for node in page["nodes"]:
            if not node.get("id") or node["id"] in seen:
                raise Halt("Incomplete or duplicate inventory identity; no write attempted")
            seen.add(node["id"])
            nodes.append(node)
        if not page["pageInfo"]["hasNextPage"]:
            return nodes
        cursor = page["pageInfo"].get("endCursor")
        if not cursor or cursor in cursors:
            raise Halt("Inventory pagination did not advance; no write attempted")
        cursors.add(cursor)
        after = cursor


FIELD_SELECTION = """fields(first:100,after:$after) {
  pageInfo { hasNextPage endCursor } nodes {
    ... on ProjectV2Field { id name dataType }
    ... on ProjectV2SingleSelectField { id name dataType options { id name description color } }
    ... on ProjectV2IterationField { id name dataType }
    ... on ProjectV2MultiSelectField { id name dataType }
  }
}"""


def inventory(gh, project=None, *, first_page=None):
    project = project or gh.metadata()["id"]

    def fetch(after):
        if after is None and first_page is not None:
            return first_page
        query = 'query($project:ID!, $after:String) { node(id:$project) { ... on ProjectV2 { ' + FIELD_SELECTION + ' } } }'
        return gh.graphql(query, {"project": project, "after": after})["node"]["fields"]

    return connection(fetch)


def resolve(fields, config):
    mapping = config.get("priority")
    if mapping is not None:
        if (not isinstance(mapping, dict) or set(mapping) != {"field_id", "options"} or
                not isinstance(mapping["options"], dict) or set(mapping["options"]) != set(LEVELS) or
                any(not isinstance(v, str) or not v for v in [mapping["field_id"], *mapping["options"].values()]) or
                len(set(mapping["options"].values())) != 4):
            raise Halt("priority requires field_id and four distinct option IDs keyed P0/P1/P2/P3")
        candidates = [f for f in fields if f["id"] == mapping["field_id"]]
    else:
        candidates = [f for f in fields if f["name"].casefold() == "priority"]
    if not candidates:
        if mapping:
            raise Halt("Configured Priority field is missing; inspect mapping before repair")
        return None
    if len(candidates) != 1 or candidates[0].get("dataType") != "SINGLE_SELECT":
        raise Halt("Priority field is ambiguous or incompatible; explicit priority mapping required")
    field = candidates[0]
    options = field.get("options", [])
    if (any(not o.get("id") or not o.get("name") for o in options) or
            len({o["id"] for o in options}) != len(options) or
            len({o["name"] for o in options}) != len(options)):
        raise Halt("Priority options are duplicate/ambiguous; preserve them and resolve explicitly")
    if mapping:
        chosen = mapping["options"]
        if set(chosen.values()) - {o["id"] for o in options}:
            raise Halt("Configured Priority option IDs are missing; inspect mapping")
    else:
        # Bare P0, High/Medium and reversed conventions need explicit meaning mapping.
        chosen = {}
        for code, (name, description) in LEVELS.items():
            matches = [o for o in options if o["name"] == name and o.get("description") == description]
            if len(matches) != 1:
                raise Halt("Priority meanings are not canonical; document an explicit priority mapping")
            chosen[code] = matches[0]["id"]
    if field["id"] == config.get("_status_field_id"):
        raise Halt("Priority must be separate from the routing status field")
    # Explicit mappings establish the user's convention, but IDs alone cannot
    # detect a concurrent rename/redefinition. Pin scalar meanings so every
    # setup/creation comparison checks fresh inventories against preflight.
    by_id = {o["id"]: o for o in options}
    meanings = {code: {"name": by_id[option]["name"], "description": by_id[option].get("description")}
                for code, option in chosen.items()}
    return {"field_id": field["id"], "options": chosen, "option_meanings": meanings}


def validate_routing(fields, config, meta):
    """Compare live routing identities/meanings with the pinned metadata cache."""
    candidates = [f for f in fields if f["name"] == config["status_field"]]
    if (len(candidates) != 1 or candidates[0]["id"] != meta["field"] or
            candidates[0].get("dataType") != "SINGLE_SELECT"):
        raise Halt("Routing status field changed or is ambiguous/incompatible; no write attempted")
    options = candidates[0].get("options", [])
    if (any(not o.get("id") or not o.get("name") for o in options) or
            len({o["id"] for o in options}) != len(options) or
            len({o["name"] for o in options}) != len(options)):
        raise Halt("Routing status options are incomplete or ambiguous; no write attempted")
    meanings = {o["name"]: o["id"] for o in options}
    if set(STATUSES) - meanings.keys() or meanings != meta["options"]:
        raise Halt("Routing status option meanings changed; Backlog mapping is not verified")


def setup(gh, apply=False):
    journal = {}
    path = Path(gh.root) / ".sfc/priority-setup" / (uuid.uuid4().hex + ".json") if apply else None

    def record(state, **details):
        journal.update(state=state, time=now(), **details)
        write_json(path, journal)

    try:
        result = _setup(gh, apply, record)
        if journal:
            result["receipt"] = str(path)
        return result
    except (Halt, OSError, ValueError, KeyError, TypeError) as exc:
        if journal:
            try:
                journal["observed_fields"] = inventory(gh)
            except (Halt, OSError, ValueError, KeyError, TypeError) as read_error:
                journal["readback_error"] = str(read_error)
            record("incomplete", error=str(exc))
        if journal:
            raise Halt(f"{exc}; inspect setup receipt {path} before repair") from exc
        if isinstance(exc, Halt):
            raise
        raise Halt("Priority setup failed; inspect saved setup receipt before repair") from exc


def _setup(gh, apply, record):
    """Read-only by default. Explicit repair never rewrites existing options/items."""
    meta = gh.metadata()
    config = {**gh.c, "_status_field_id": meta["field"]}

    def inspect():
        fields = inventory(gh, meta["id"])
        validate_routing(fields, config, meta)
        return resolve(fields, config)

    scope = inspect()
    data = gh.dashboard_data()
    view = gh.factory_view(data)  # Existing presentation API refuses truncated inventories.
    if not view or view["layout"] != "BOARD_LAYOUT":
        return {"ready": False, "pending": "Run dashboard --apply to prepare the saved Factory view"}
    created_field = apply and scope is None
    if created_field:
        if inspect() != scope:
            raise Halt("Priority setup changed before field creation")
        record("creating_field", project=meta["id"], before_view=view)
        created = gh.graphql("""mutation($input:CreateProjectV2FieldInput!) {
          createProjectV2Field(input:$input) { projectV2Field {
            ... on ProjectV2SingleSelectField { id }
          } }
        }""", {"input": {"projectId": meta["id"], "name": "Priority", "dataType": "SINGLE_SELECT",
                          "singleSelectOptions": [dict(name=name, description=description, color=color)
                              for (name, description), color in zip(LEVELS.values(), ("RED", "ORANGE", "BLUE", "GRAY"))]}})
        made_id = created["createProjectV2Field"]["projectV2Field"]["id"]
        record("field_created", field_id=made_id)
        scope = inspect()
        if scope is None or scope["field_id"] != made_id:
            raise Halt("Priority creation not verified; inspect remote state before repair")
    if scope is None:
        return {"ready": False, "pending": "Priority field missing; explicitly run priority-setup --apply"}
    # Re-read presentation immediately before the targeted visibility write.
    data = gh.dashboard_data()
    view = gh.factory_view(data)
    visible = [f["id"] for f in view["configuration"]["visibleFields"]["nodes"]]
    wanted = visible + ([scope["field_id"]] if scope["field_id"] not in visible else [])
    if apply and wanted != visible:
        if inspect() != scope:
            raise Halt("Priority setup changed before visibility write")
        record("setting_visibility", project=meta["id"], scope=scope, before_view=view, wanted=wanted)
        gh.graphql("""mutation($input:UpdateProjectV2ViewInput!) {
          updateProjectV2View(input:$input) { projectV2View { id } }
        }""", {"input": {"viewId": view["id"], "configuration": {"visibleFieldIds": wanted}}})
        saved = gh.factory_view(gh.dashboard_data())
        if (not saved or saved["id"] != view["id"] or saved["layout"] != view["layout"] or
                saved["filter"] != view["filter"] or saved["verticalGroupByFields"] != view["verticalGroupByFields"] or
                [f["id"] for f in saved["configuration"]["visibleFields"]["nodes"]] != wanted or
                inspect() != scope):
            raise Halt("Priority visibility/preservation readback failed; inspect before repair")
        visible = wanted
    if inspect() != scope:
        raise Halt("Priority setup changed during verification")
    ready = scope["field_id"] in visible
    if apply:
        # A matching repeat remains read-only, including local receipts.
        if created_field or wanted != [f["id"] for f in view["configuration"]["visibleFields"]["nodes"]]:
            record("complete", scope=scope, visible_fields=visible)
    return {"ready": ready, **scope, "pending": None if ready else "Run priority-setup --apply to show Priority on saved Factory cards"}


ITEM_SELECTION = """node(id:$item) { ... on ProjectV2Item { id project { id }
    content { ... on Issue { id number title body url state repository { nameWithOwner } } }
    fieldValues(first:100,after:$after) { pageInfo { hasNextPage endCursor } nodes {
      ... on ProjectV2ItemFieldSingleSelectValue { optionId field { ... on ProjectV2SingleSelectField { id } } }
    } }
  } }"""


class ProjectItemMissing(Halt):
    """The Project item read returned a null node."""


def item_snapshot(gh, item_id, *, tolerate_ambiguous=False, first_page=None):
    after, values, identity = None, {}, None
    cursors = set()
    while True:
        node = first_page if after is None and first_page is not None else gh.graphql(
            "query($item:ID!, $after:String) {" + ITEM_SELECTION + "}",
            {"item": item_id, "after": after})["node"]
        if node is None:
            raise ProjectItemMissing("Created item identity is unavailable")
        if not node or not node.get("content"):
            raise Halt("Created item identity is unavailable")
        current = {k: node[k] for k in ("id", "project", "content")}
        if identity is not None and current != identity:
            raise Halt("Created item changed during pagination")
        identity = current
        for value in node["fieldValues"]["nodes"]:
            if not value.get("field"):
                continue
            key = value["field"]["id"]
            if key in values:
                if tolerate_ambiguous:
                    values[key] = None  # Unknown, never an invented stage/level.
                    continue
                raise Halt("Duplicate item field values; inspect before retry")
            values[key] = value["optionId"]
        page = node["fieldValues"]["pageInfo"]
        if not page["hasNextPage"]:
            return {**identity, "values": values}
        after = page.get("endCursor")
        if not after or after in cursors:
            raise Halt("Item field pagination did not advance")
        cursors.add(after)


def create_ticket(gh, root, **request):
    """Serialize every creator, including direct callers and linked checkouts."""
    with creation_lock(root):
        return _create_ticket(gh, root, **request)


def _create_ticket(gh, root, *, request_id, title, body, priority, reason, unknowns, work_type=None, labels=(), workflow_kind='development'):
    """A request ID latches intent before writes. Repeats never create duplicates."""
    if (not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", request_id) or
            priority not in LEVELS or work_type not in work_types.TYPES or workflow_kind not in ('development', 'release') or
            any(not isinstance(v, str) or not v.strip() for v in (title, body, reason, unknowns))):
        raise Halt("Ticket requires request ID, title/body, assessed work type, P0/P1/P2/P3, reason and explicit unknowns")
    if (not isinstance(labels, (list, tuple)) or
            any(not isinstance(v, str) or not v.strip() for v in labels) or
            len({v.casefold() for v in labels}) != len(labels)):
        raise Halt("Auxiliary labels must be distinct nonempty repository label names")
    path = ticket_directory(root) / (request_id + ".json")
    prior_path = recorded_request(root, request_id)
    if prior_path:
        raise Halt(f"Request already recorded; inspect {prior_path}; no writes retried")
    marker = f"<!-- sfc:ticket:{request_id} -->"
    text = body.rstrip() + f"\n\n## Priority assessment\n\n**{LEVELS[priority][0]}**\n\nReason/evidence: {reason.strip()}\n\nUnknowns/limits: {unknowns.strip()}\n\n{marker}\n"
    problems = lint_card({"body": text})
    if MARKER.findall(text) != [request_id] or len(MARKER_START.findall(text)) != 1:
        problems.append("remove embedded sfc:ticket request markers; creation appends its own")
    if problems:
        raise Halt("Invalid ticket body: " + "; ".join(problems))
    gh.verify_repository()
    scope = setup(gh)
    if not scope["ready"]:
        raise Halt(scope["pending"])
    type_report = work_types.setup(gh)
    if not type_report["ready"]:
        raise Halt(type_report["pending"])
    type_scope = work_types.creation_scope(type_report)
    type_names = set().union(*work_types.ALIASES.values()) | {
        r["name"].casefold() for r in type_scope["mapping"].values()}
    existing_names = {r["name"] for r in type_report["inventory"]}
    if any(label not in existing_names or label.casefold() in type_names for label in labels):
        raise Halt("Auxiliary labels must exist and must not assign another work type")
    meta = gh.metadata()
    receipt = {"request_id": request_id, "started": now(), "repo": gh.c["repo"], "project": meta["id"],
               "workflow_kind": workflow_kind,
               "priority": priority, "scope": scope, "work_type": work_type, "type_scope": type_scope,
               "auxiliary_labels": list(labels), "title": title, "body": text, "state": "intent", "issue": None, "item_id": None}
    write_json(path, receipt)

    def save(state):
        receipt["state"] = state
        receipt.setdefault("history", []).append({"state": state, "time": now(),
                                                   "write": receipt.get("next_write") if state == "setting_field" else None})
        write_json(path, receipt)

    def checked_setup():
        if setup(gh) != scope or gh.metadata() != meta:
            raise Halt("Priority/routing setup changed during creation")
        if work_types.creation_scope(work_types.setup(gh)) != type_scope:
            raise Halt("Work-type label identity/name/description or visibility changed during creation")

    def checked_labels():
        receipt["observed_labels"] = work_types.check_issue(gh, receipt["issue"]["number"], type_scope, work_type, labels)

    def checked(snapshot):
        content = snapshot["content"]
        issue = receipt["issue"]
        if (snapshot["id"] != receipt["item_id"] or snapshot["project"]["id"] != meta["id"] or
                any(content.get(k) != issue.get(k) for k in ("id", "number", "url")) or
                content.get("repository", {}).get("nameWithOwner") != gh.c["repo"] or
                content.get("state") != "OPEN" or content.get("body") != text or content.get("title") != title):
            raise Halt("Created issue/body/repository/project/item readback mismatch")
        checked_setup()
        checked_labels()

    try:
        checked_setup()
        save("creating_issue")
        issue = gh.call("api", f"repos/{gh.c['repo']}/issues", "--method", "POST", "--input", "-",
                        input=json.dumps({"title": title, "body": text,
                                          "labels": [type_scope["mapping"][work_type]["name"], *labels]}))
        # REST issue responses use node_id for GraphQL enrollment.
        receipt["issue"] = {"id": issue.get("node_id"), "number": issue.get("number"), "url": issue.get("html_url")}
        save("issue_created")
        if not all(receipt["issue"].values()) or issue.get("body") != text or issue.get("title") != title:
            raise Halt("Issue creation response is incomplete or mismatched")
        problems = lint_card({"body": text, "number": receipt["issue"]["number"]})
        if problems:
            raise Halt("Invalid created ticket: " + "; ".join(problems))
        checked_setup()
        checked_labels()
        save("enrolling")
        added = gh.graphql("""mutation($input:AddProjectV2ItemByIdInput!) {
          addProjectV2ItemById(input:$input) { item { id } }
        }""", {"input": {"projectId": meta["id"], "contentId": receipt["issue"]["id"]}})
        receipt["item_id"] = added["addProjectV2ItemById"]["item"]["id"]
        save("enrolled")
        targets = [(meta["field"], meta["options"]["Backlog"]), (scope["field_id"], scope["options"][priority])]
        expected = {}
        for field_id, option in targets:
            snapshot = item_snapshot(gh, receipt["item_id"])
            checked(snapshot)
            for target_id, target_option in targets:
                current = snapshot["values"].get(target_id)
                if (target_id in expected and current != expected[target_id]) or current not in (None, target_option):
                    raise Halt("Concurrent or populated manual status/priority differs; preserving current values")
                # Observe every target, even before its turn and even when cleared.
                expected[target_id] = current
            # Already matching values need no write.
            if snapshot["values"].get(field_id) != option:
                receipt["before"] = snapshot
                receipt["next_write"] = {"field_id": field_id, "option_id": option}
                save("setting_field")
                result = gh.graphql("""mutation($input:UpdateProjectV2ItemFieldValueInput!) {
                  updateProjectV2ItemFieldValue(input:$input) { projectV2Item { id } }
                }""", {"input": {"projectId": meta["id"], "itemId": receipt["item_id"],
                                  "fieldId": field_id, "value": {"singleSelectOptionId": option}}})
                if result["updateProjectV2ItemFieldValue"]["projectV2Item"]["id"] != receipt["item_id"]:
                    raise Halt("Field write returned a different item")
            expected[field_id] = option
        final = item_snapshot(gh, receipt["item_id"])
        checked(final)
        if any(final["values"].get(k) != v for k, v in expected.items()):
            raise Halt("Backlog/Priority readback mismatch; creation incomplete")
        receipt["verified"] = final
        save("complete")
        return {"complete": True, "issue": receipt["issue"], "item_id": receipt["item_id"],
                "priority": priority, "work_type": work_type, "label": type_scope["mapping"][work_type],
                "status": "Backlog", "receipt": str(path)}
    except (Halt, OSError, ValueError, KeyError, TypeError) as exc:
        receipt["failed_stage"] = receipt["state"]
        receipt["error"] = str(exc)
        # Read-only discovery can recover actual identities after a lost response.
        # The request marker is generated locally and exact body/title must match.
        try:
            if not receipt["issue"] or not all(receipt["issue"].values()):
                query = urlencode({"state": "all", "since": receipt["started"], "per_page": 100})
                pages = gh.call("api", f"repos/{gh.c['repo']}/issues?{query}",
                                "--paginate", "--slurp")
                matches = [issue for page in pages for issue in page if "pull_request" not in issue and
                           issue.get("body") == text and issue.get("title") == title]
                receipt["recovery_issue_candidates"] = [{"id": i.get("node_id"), "number": i.get("number"),
                                                           "url": i.get("html_url")} for i in matches]
                if len(matches) == 1 and all(receipt["recovery_issue_candidates"][0].values()):
                    receipt["issue"] = receipt["recovery_issue_candidates"][0]
            if receipt["issue"] and not receipt["item_id"]:
                items = connection(lambda after: gh.graphql("""query($issue:ID!, $after:String) {
                  node(id:$issue) { ... on Issue { projectItems(first:100,after:$after) {
                    pageInfo { hasNextPage endCursor } nodes { id project { id } }
                  } } }
                }""", {"issue": receipt["issue"]["id"], "after": after})["node"]["projectItems"])
                matches = [i["id"] for i in items if i["project"]["id"] == meta["id"]]
                receipt["recovery_item_candidates"] = matches
                if len(matches) == 1:
                    receipt["item_id"] = matches[0]
        except (Halt, OSError, ValueError, KeyError, TypeError) as recovery_error:
            receipt["identity_recovery_error"] = str(recovery_error)
        if receipt["item_id"]:
            try:
                receipt["observed_after_failure"] = item_snapshot(gh, receipt["item_id"])
            except (Halt, OSError, ValueError, KeyError, TypeError) as read_error:
                receipt["readback_error"] = str(read_error)
        if receipt["issue"] and receipt["issue"].get("number"):
            try:
                receipt["labels_after_failure"] = work_types.labels(gh, receipt["issue"]["number"])
            except (Halt, OSError, ValueError, KeyError, TypeError) as read_error:
                receipt["label_readback_error"] = str(read_error)
        save("incomplete")
        raise Halt(f"Incomplete ticket creation; issue={receipt['issue']}, item={receipt['item_id']}; inspect {path}; no automatic retry") from exc
