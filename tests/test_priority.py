"""Real supervisor setup/create functions with fake transports and temporary git repos."""
import contextlib
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import sfc
from sfc_lib.common import Halt, common_dir, git, read_json, write_json
from sfc_lib.github import GitHub
from sfc_lib.factory import dependencies as dispatch_dependencies, lint_card
from sfc_lib.pickup import dependencies as pickup_dependencies
from sfc_lib.priority import LEVELS, create_ticket, inventory, setup
from sfc_lib.work_types import TYPES
from test_dashboard import DashboardGitHub, view
from test_factory import BASE_CONFIG


def priority_field():
    return {"id": "priority-field", "name": "Priority", "dataType": "SINGLE_SELECT", "options": [
        {"id": "priority-" + code, "name": name, "description": description, "color": "BLUE"}
        for code, (name, description) in LEVELS.items()]}


class TicketGitHub(DashboardGitHub):
    # Keep the production setup path; DashboardGitHub only stubs it for unrelated tests.
    priority_setup = GitHub.priority_setup
    type_setup = GitHub.type_setup
    metadata = GitHub.metadata

    def __init__(self, root, with_priority=True):
        super().__init__(styled=True, views=[view()])
        self.root = root
        self.c = copy.deepcopy(BASE_CONFIG)
        self.c["repo"] = "example/app"
        self.project, self._meta = self.c["project"], None
        self.fields = self.data["project"]["fields"]["nodes"]
        self.fields.append({**self.data["field"], "dataType": "SINGLE_SELECT"})
        self.fields.append({"id": "labels-field", "name": "Labels", "dataType": "LABELS"})
        self.data["project"]["views"]["nodes"][0]["configuration"]["visibleFields"]["nodes"].append({"id": "labels-field"})
        if with_priority:
            self.fields.append(priority_field())
            self.data["project"]["views"]["nodes"][0]["configuration"]["visibleFields"]["nodes"].append({"id": "priority-field"})
        self.issues, self.items, self.writes = {}, {}, []
        self.fail = None
        self.corrupt = None
        self.on_snapshot = None
        self.snapshots = 0
        self.page_size = 100
        self.reads = []
        self.repo_labels = [{"node_id": "label-" + kind, "name": name, "description": description, "color": color}
                            for kind, (name, description, color) in TYPES.items()]
        self.on_label_read = None
        self.label_reads = 0

    def call(self, *args, input=None):
        if args[:2] == ("project", "view"):
            return {"id": "project-id"}
        if args[0] == "api" and args[1].endswith("labels?per_page=100"):
            assert args[2:] == ("--paginate", "--slurp")
            self.label_reads += 1
            if self.on_label_read:
                self.on_label_read(self, args[1])
            records = self.repo_labels
            if "/issues/" in args[1]:
                number = int(args[1].split("/issues/")[1].split("/")[0])
                names = self.issues[number]["labels"]
                records = [r for r in records if r["name"] in names]
            return [copy.deepcopy(records[start:start + self.page_size])
                    for start in range(0, len(records), self.page_size)] or [[]]
        if args[:2] == ("api", "repos/example/app/labels"):
            body = json.loads(input)
            self.writes.append("label-create")
            made = {"node_id": "made-" + body["name"], **body}
            if self.fail != "drop-label-create":
                self.repo_labels.append(made)
            if self.fail == "label-create":
                raise Halt("Label creation response lost")
            return copy.deepcopy(made)
        if args[0] == "api" and "?state=" in args[1]:
            return [copy.deepcopy(list(self.issues.values()))]
        assert args[:2] == ("api", "repos/example/app/issues")
        body = json.loads(input)
        self.writes.append("create")
        n = len(self.issues) + 1
        made = {"node_id": f"issue-{n}", "number": n, "html_url": f"https://github.com/example/app/issues/{n}", **body}
        self.issues[n] = made
        if self.fail == "create":
            raise Halt("Creation response lost")
        return copy.deepcopy(made)

    def graphql(self, query, variables):
        if not query.lstrip().startswith("mutation"):
            self.reads.append(copy.deepcopy(variables))
            if "fields(first:100,after:$after)" in query:
                start = int(variables["after"] or 0)
                end = start + self.page_size
                return {"node": {"fields": {"nodes": copy.deepcopy(self.fields[start:end]),
                         "pageInfo": {"hasNextPage": end < len(self.fields), "endCursor": str(end)}}}}
            if "projectItems(first:100,after:$after)" in query:
                return {"node": {"projectItems": {"nodes": [{"id": i["id"], "project": i["project"]} for i in self.items.values()],
                                                   "pageInfo": {"hasNextPage": False}}}}
            if "fieldValues(first:100,after:$after)" in query:
                self.snapshots += 1
                if self.on_snapshot:
                    self.on_snapshot(self)
                item = copy.deepcopy(self.items[variables["item"]])
                if self.corrupt:
                    self.corrupt(item)
                start = int(variables["after"] or 0)
                end = start + self.page_size
                nodes = [{"field": {"id": k}, "optionId": v} for k, v in item.pop("values").items()]
                item["fieldValues"] = {"nodes": nodes[start:end], "pageInfo": {
                    "hasNextPage": end < len(nodes), "endCursor": str(end)}}
                return {"node": item}
            return super().graphql(query, variables)
        payload = variables["input"]
        if "addProjectV2ItemById" in query:
            self.writes.append("enroll")
            issue = next(i for i in self.issues.values() if i["node_id"] == payload["contentId"])
            item = {"id": "item-" + str(issue["number"]), "project": {"id": payload["projectId"]},
                    "content": {"id": issue["node_id"], "number": issue["number"], "url": issue["html_url"],
                                "title": issue["title"], "body": issue["body"], "state": "OPEN",
                                "repository": {"nameWithOwner": "example/app"}}, "values": {}}
            self.items[item["id"]] = item
            if self.fail == "enroll":
                raise Halt("Enrollment response lost")
            return {"addProjectV2ItemById": {"item": {"id": item["id"]}}}
        if "updateProjectV2ItemFieldValue" in query:
            field = payload["fieldId"]
            self.writes.append(field)
            if self.fail == field:
                raise Halt("Field write failed")
            if self.fail != "drop-" + field:
                self.items[payload["itemId"]]["values"][field] = payload["value"]["singleSelectOptionId"]
            if self.fail == "after-" + field:
                raise Halt("Field response lost after write")
            return {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": payload["itemId"]}}}
        if payload.get("dataType") == "SINGLE_SELECT":
            self.writes.append("field-create")
            made = {"id": "priority-field", "name": payload["name"], "dataType": payload["dataType"],
                    "options": [{"id": "priority-" + code, **option} for code, option in zip(LEVELS, payload["singleSelectOptions"])]}
            self.fields.append(made)
            if self.fail == "field-create":
                raise Halt("Field creation response lost")
            return {"createProjectV2Field": {"projectV2Field": {"id": made["id"]}}}
        result = super().graphql(query, variables)
        if self.fail == "visibility-readback":
            self.data["project"]["views"]["nodes"][0]["filter"] = "label:human-edit"
        return result


class PriorityTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="sfc-priority-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        git(self.root, "init")
        self.gh = TicketGitHub(self.root)
        self.args = dict(request_id="finding-1", title="Example finding", body="## Acceptance criteria\n- [ ] Observable outcome",
                         priority="P2", work_type="Feature", reason="Supported bounded workflow gap; safe text workaround (fixture reproduction).",
                         unknowns="Confidence high in the fixture; customer frequency, urgency and effort unknown.")

    def create(self, **changes):
        return create_ticket(self.gh, self.root, **{**self.args, **changes})

    def receipt(self):
        return read_json(common_dir(self.root) / "sfc-tickets/finding-1.json")

    def cli_create(self, body, **changes):
        args = {**self.args, **changes}
        write_json(self.root / ".sfc/config.json", self.gh.c)
        body_file = self.root / "finding.md"
        body_file.write_text(body)
        with patch.object(sfc, "GitHub", return_value=self.gh), \
                contextlib.redirect_stdout(io.StringIO()) as output, \
                contextlib.redirect_stderr(io.StringIO()) as errors:
            code = sfc.main(["--root", str(self.root), "create-ticket", "--request-id", args["request_id"],
                             "--title", args["title"], "--body-file", str(body_file), "--priority", args["priority"],
                             "--type", args["work_type"], "--reason", args["reason"], "--unknowns", args["unknowns"]])
        return code, output.getvalue(), errors.getvalue()

    def test_cli_rejects_dependency_prose_before_any_remote_write(self):
        body = self.args["body"] + "\n\n## Blocked by\n- #58\n\nIntegrate the contract repair first."
        code, output, errors = self.cli_create(body)
        self.assertEqual(code, 1)
        self.assertIn("Malformed dependency", json.loads(errors)["error"])
        self.assertEqual(output, "")
        self.assertEqual(self.gh.writes + self.gh.mutations, [])
        self.assertFalse((common_dir(self.root) / "sfc-tickets").exists())

    def test_cli_rejects_invalid_final_bodies_without_receipts_or_setup_writes(self):
        criteria = self.args["body"]
        cases = [
            ("## Goal\nMissing criteria", {}, "missing ## Acceptance criteria"),
            ("## Acceptance criteria\n- [ ]\n* [x]\t", {}, "observable bullet points"),
            ("## Acceptance criteria\n- [ ]\n## Notes\n- Not a criterion", {}, "observable bullet points"),
            (criteria + "\n## Blocked by\n- #58\n## Blocked by", {}, "Use one"),
            (criteria + "\n## Blocked by: prerequisites\n- #58", {}, "heading"),
            *[(criteria + "\n## Blocked by\n" + reference, {}, "Malformed dependency")
              for reference in ("- #0", "- #-2", "- #02", "- #x", "- #2 and #3", "- #2 explanation",
                                "- other/repo#2", "- https://github.com/example/app/issues/2", "None")],
            # Validate the final composition, including multiline assessment inputs.
            (criteria, {"reason": "Evidence\n\n## Blocked by\n- #58"}, "Malformed dependency"),
            (criteria + "\n## Blocked by\n- #58", {"unknowns": "Unknown\n\n## Blocked by"}, "Use one"),
            (criteria + "\n<!-- sfc:ticket:other -->", {}, "request markers"),
            (criteria, {"unknowns": "Unknown\n<!-- sfc:ticket:unclosed"}, "request markers"),
        ]
        for body, changes, message in cases:
            with self.subTest(body=body, changes=changes):
                # Incomplete setup must not mask body errors or trigger repairs.
                self.gh = TicketGitHub(self.root, with_priority=False)
                code, output, errors = self.cli_create(body, **changes)
                self.assertEqual(code, 1)
                self.assertIn(message, json.loads(errors)["error"])
                self.assertEqual(output, "")
                self.assertEqual(self.gh.writes + self.gh.mutations, [])
                self.assertEqual(self.gh.reads, [])
                self.assertFalse((common_dir(self.root) / "sfc-tickets").exists())

    def test_cli_valid_layouts_preserve_final_body_receipt_and_deduplication(self):
        criteria = self.args["body"]
        cases = [(criteria, []), (criteria + "\n\n## Blocked by", []),
                 (criteria + "\n## Blocked by\n\n## Dependency notes\nNo prerequisites.", []),
                 (criteria + "\n## Blocked by\n- #58\n* #59\n#58\n\n## Dependency notes\nIntegrate #58 first.", [58, 59]),
                 ("## Acceptance criteria\r\n* [X] Observable outcome\r\n\r\n##\tblocked BY\t\r\n- #58\r\n", [58])]
        for index, (body, expected) in enumerate(cases):
            with self.subTest(body=body):
                self.gh = TicketGitHub(self.root)
                request_id = f"valid-{index}"
                code, output, errors = self.cli_create(body, request_id=request_id)
                self.assertEqual((code, errors), (0, ""))
                result = json.loads(output)
                self.assertTrue(result["complete"])
                receipt = read_json(Path(result["receipt"]))
                final_body = receipt["verified"]["content"]["body"]
                self.assertEqual(final_body, body.replace("\r\n", "\n").rstrip() +
                                 f"\n\n## Priority assessment\n\n**P2 Normal**\n\nReason/evidence: {self.args['reason']}"
                                 f"\n\nUnknowns/limits: {self.args['unknowns']}\n\n<!-- sfc:ticket:{request_id} -->\n")
                self.assertEqual(lint_card(receipt["verified"]["content"]), [])
                self.assertEqual(dispatch_dependencies(final_body), expected)
                self.assertEqual(pickup_dependencies(final_body), expected)
                self.assertEqual(receipt["state"], "complete")
                self.assertEqual(self.gh.writes, ["create", "enroll", "stage-field", "priority-field"])
                before = Path(result["receipt"]).read_bytes()
                # Repeats keep the original latch, even with a changed malformed body.
                code, output, errors = self.cli_create("No criteria", request_id=request_id)
                self.assertEqual(code, 1)
                self.assertIn("already recorded", json.loads(errors)["error"])
                self.assertEqual(output, "")
                self.assertEqual(Path(result["receipt"]).read_bytes(), before)
                self.assertEqual(self.gh.writes, ["create", "enroll", "stage-field", "priority-field"])

    def test_self_dependency_checked_only_after_actual_identity_is_known(self):
        body = self.args["body"] + "\n## Blocked by\n- #1"
        code, output, errors = self.cli_create(body)
        self.assertEqual(code, 1)
        self.assertEqual(output, "")
        self.assertIn("Incomplete ticket creation", json.loads(errors)["error"])
        self.assertEqual(self.gh.writes, ["create"])
        receipt = self.receipt()
        self.assertEqual(receipt["state"], "incomplete")
        self.assertIn("itself", receipt["error"])
        self.assertEqual(receipt["issue"]["number"], 1)
        self.assertIsNone(receipt["item_id"])
        with self.assertRaisesRegex(Halt, "already recorded"):
            self.create(body=body)
        self.assertEqual(self.gh.writes, ["create"])

        # A #1 dependency is valid when the actual new number is #2. No guessing.
        code, output, errors = self.cli_create(body, request_id="different-identity")
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(json.loads(output)["issue"]["number"], 2)

    def test_missing_setup_is_read_only_and_prevents_issue(self):
        self.gh = TicketGitHub(self.root, with_priority=False)
        self.assertFalse(setup(self.gh)["ready"])
        with self.assertRaisesRegex(Halt, "missing"):
            self.create()
        self.assertEqual(self.gh.writes, [])
        self.assertFalse((common_dir(self.root) / "sfc-tickets").exists())

    def test_setup_creates_priority_and_saves_visibility_preserving_everything_else(self):
        self.gh = TicketGitHub(self.root, with_priority=False)
        saved = self.gh.data["project"]["views"]["nodes"][0]
        saved["filter"] = "label:mine"
        saved["configuration"]["visibleFields"]["nodes"].insert(0, {"id": "custom"})
        custom = view("My private planning", id="other", filter="assignee:me", layout="TABLE_LAYOUT")
        self.gh.data["project"]["views"]["nodes"].append(copy.deepcopy(custom))
        before = copy.deepcopy(saved)
        result = setup(self.gh, apply=True)
        self.assertTrue(result["ready"])
        self.assertEqual(self.gh.writes, ["field-create"])
        self.assertEqual(self.gh.fields[-1], {**priority_field(), "options": [
            {**option, "color": color} for option, color in zip(priority_field()["options"], ("RED", "ORANGE", "BLUE", "GRAY"))]})
        self.assertEqual(saved["filter"], before["filter"])
        self.assertEqual(saved["verticalGroupByFields"], before["verticalGroupByFields"])
        self.assertEqual(saved["configuration"]["visibleFields"]["nodes"], before["configuration"]["visibleFields"]["nodes"] + [{"id": "priority-field"}])
        self.assertEqual(self.gh.data["project"]["views"]["nodes"][1], custom)
        writes = copy.deepcopy((self.gh.writes, self.gh.mutations))
        self.assertTrue(setup(self.gh, apply=True)["ready"])
        self.assertEqual((self.gh.writes, self.gh.mutations), writes)

    def test_existing_manual_values_and_option_ids_meanings_remain_unchanged(self):
        self.gh.items["manual"] = {"values": {"priority-field": "priority-P1", "team": "my-team"}}
        before = copy.deepcopy((self.gh.fields, self.gh.items))
        self.assertTrue(setup(self.gh, apply=True)["ready"])
        self.assertEqual((self.gh.fields, self.gh.items), before)
        self.assertEqual(self.gh.writes + self.gh.mutations, [])

    def test_ambiguous_incompatible_or_conflicting_meanings_refuse_all_writes(self):
        cases = [lambda f: f.append(priority_field()),
                 lambda f: f[-1].update(dataType="TEXT"),
                 lambda f: f[-1]["options"][0].update(description="P0 means least urgent"),
                 lambda f: f[-1]["options"][0].update(name="P1 High"),
                 lambda f: f[-1]["options"][0].update(id="priority-P1")]
        for change in cases:
            with self.subTest(change=change):
                self.gh = TicketGitHub(self.root)
                change(self.gh.fields)
                before = copy.deepcopy(self.gh.fields)
                with self.assertRaises(Halt):
                    setup(self.gh, apply=True)
                self.assertEqual(self.gh.fields, before)
                self.assertEqual(self.gh.writes + self.gh.mutations, [])

    def test_documented_explicit_mapping_reuses_custom_convention_without_changes(self):
        self.gh.fields[-1]["name"] = "Business ranking"
        for n, option in enumerate(self.gh.fields[-1]["options"]):
            option["name"], option["description"] = f"Rank {n}", "Existing business meaning"
        self.gh.fields.append({"id": "unrelated-priority", "name": "Priority", "dataType": "TEXT"})
        self.gh.c["priority"] = {"field_id": "priority-field", "options": {code: "priority-" + code for code in LEVELS}}
        before = copy.deepcopy(self.gh.fields)
        self.assertTrue(setup(self.gh, apply=True)["ready"])
        self.assertEqual(self.gh.fields, before)
        self.assertTrue(self.create()["complete"])

    def test_invalid_or_missing_mapping_ids_and_routing_field_refused(self):
        for mapping in ({}, {"field_id": "gone", "options": {code: "priority-" + code for code in LEVELS}},
                        {"field_id": "priority-field", "options": {code: "same" for code in LEVELS}}):
            with self.subTest(mapping=mapping):
                self.gh.c["priority"] = mapping
                with self.assertRaises(Halt):
                    setup(self.gh, apply=True)
                self.assertEqual(self.gh.writes, [])

    def test_visibility_readback_mismatch_never_claims_setup_success(self):
        self.gh.data["project"]["views"]["nodes"][0]["configuration"]["visibleFields"]["nodes"].pop()
        self.gh.fail = "visibility-readback"
        with self.assertRaisesRegex(Halt, "readback"):
            setup(self.gh, apply=True)
        self.assertEqual(len(self.gh.mutations), 1)

    def test_full_field_and_item_value_pagination_used(self):
        self.gh.page_size = 1
        self.assertEqual(len(inventory(self.gh)), 5)
        self.assertTrue(self.create()["complete"])
        self.assertTrue(any(r.get("after") == "2" for r in self.gh.reads))
        self.assertEqual(self.receipt()["verified"]["values"], {"stage-field": "option-0", "priority-field": "priority-P2"})

    def test_happy_path_verifies_actual_identity_body_backlog_and_priority(self):
        result = self.create()
        self.assertTrue(result["complete"])
        self.assertEqual(self.gh.writes, ["create", "enroll", "stage-field", "priority-field"])
        receipt = self.receipt()
        self.assertEqual(receipt["state"], "complete")
        self.assertEqual(receipt["verified"]["content"]["repository"]["nameWithOwner"], "example/app")
        self.assertIn(self.args["reason"], receipt["verified"]["content"]["body"])
        self.assertIn(self.args["unknowns"], receipt["verified"]["content"]["body"])
        self.assertEqual(receipt["verified"]["id"], result["item_id"])
        self.assertEqual(receipt["verified"]["project"]["id"], "project-id")

    def test_explicit_user_priority_is_preserved(self):
        self.create(priority="P3", reason="Explicit user choice; fixture evidence remains bounded.")
        self.assertEqual(self.receipt()["verified"]["values"]["priority-field"], "priority-P3")

    def test_invalid_priority_reason_unknowns_request_or_body_prevents_remote_writes(self):
        for changes in ({"priority": "Needs evidence"}, {"priority": "P4"}, {"reason": "  "},
                        {"unknowns": ""}, {"request_id": "../escape"}, {"title": ""}, {"body": ""}):
            with self.subTest(changes=changes), self.assertRaises(Halt):
                self.create(**changes)
            self.assertEqual(self.gh.writes, [])

    def test_partial_failures_have_receipts_no_blind_retries(self):
        for stage in ("create", "enroll", "stage-field", "priority-field", "after-priority-field", "drop-stage-field", "drop-priority-field"):
            with self.subTest(stage=stage):
                self.gh = TicketGitHub(self.root)
                self.gh.fail = stage
                path = common_dir(self.root) / "sfc-tickets/finding-1.json"
                if path.exists():
                    path.unlink()
                with self.assertRaisesRegex(Halt, "Incomplete ticket creation"):
                    self.create()
                receipt = self.receipt()
                self.assertEqual(receipt["state"], "incomplete")
                self.assertIn("error", receipt)
                self.assertEqual(len(self.gh.issues), 1)
                if stage != "create":
                    self.assertEqual(receipt["issue"]["number"], 1)
                if "field" in stage:
                    self.assertEqual(receipt["item_id"], "item-1")
                    self.assertIn("observed_after_failure", receipt)
                writes = self.gh.writes.copy()
                with self.assertRaisesRegex(Halt, "already recorded"):
                    self.create()
                self.assertEqual(self.gh.writes, writes)

    def test_repeated_success_does_not_duplicate(self):
        self.create()
        with self.assertRaisesRegex(Halt, "already recorded"):
            self.create()
        self.assertEqual(len(self.gh.issues), 1)
        self.assertEqual(self.receipt()["state"], "complete")

    def test_readback_identity_and_body_mismatches_preserve_receipt(self):
        changes = [lambda i: i.update(id="other-item"), lambda i: i["project"].update(id="other-project"),
                   lambda i: i["content"].update(id="other-issue"), lambda i: i["content"].update(number=99),
                   lambda i: i["content"].update(body="Human body edit"), lambda i: i["content"].update(state="CLOSED"),
                   lambda i: i["content"]["repository"].update(nameWithOwner="other/repo")]
        for change in changes:
            with self.subTest(change=change):
                self.gh = TicketGitHub(self.root)
                self.gh.corrupt = change
                path = common_dir(self.root) / "sfc-tickets/finding-1.json"
                if path.exists():
                    path.unlink()
                with self.assertRaises(Halt):
                    self.create()
                self.assertEqual(self.gh.writes, ["create", "enroll"])
                self.assertEqual(self.receipt()["state"], "incomplete")

    def test_concurrent_manual_priority_and_status_are_preserved(self):
        for field, value, when in (("priority-field", "priority-P0", 1), ("stage-field", "option-1", 1),
                                   ("stage-field", "option-1", 2), ("priority-field", "priority-P1", 3)):
            with self.subTest(field=field, when=when):
                self.gh = TicketGitHub(self.root)
                def edit(gh):
                    if gh.snapshots == when:
                        gh.items["item-1"]["values"][field] = value
                self.gh.on_snapshot = edit
                path = common_dir(self.root) / "sfc-tickets/finding-1.json"
                if path.exists():
                    path.unlink()
                with self.assertRaises(Halt):
                    self.create()
                self.assertEqual(self.gh.items["item-1"]["values"][field], value)
                self.assertEqual(self.receipt()["state"], "incomplete")

    def test_observed_matching_priority_clear_is_preserved(self):
        def edit(gh):
            values = gh.items["item-1"]["values"]
            if gh.snapshots == 1:
                values["priority-field"] = "priority-P2"
            elif gh.snapshots == 2:
                values.pop("priority-field")
        self.gh.on_snapshot = edit
        with self.assertRaisesRegex(Halt, "Incomplete ticket creation"):
            self.create()
        self.assertNotIn("priority-field", self.gh.items["item-1"]["values"])
        self.assertEqual(self.gh.writes, ["create", "enroll", "stage-field"])
        self.assertEqual(self.receipt()["state"], "incomplete")

    def test_routing_meaning_changes_halt_before_creation_writes_or_completion(self):
        for when in ("preflight", 1, 2, 3):
            with self.subTest(when=when):
                self.gh = TicketGitHub(self.root)
                path = common_dir(self.root) / "sfc-tickets/finding-1.json"
                if path.exists():
                    path.unlink()
                def swap():
                    options = next(f for f in self.gh.fields if f["id"] == "stage-field")["options"]
                    options[0]["name"], options[1]["name"] = options[1]["name"], options[0]["name"]
                if when == "preflight":
                    graphql = self.gh.graphql
                    reads = []
                    def changed_inventory(query, variables):
                        if "fields(first:100,after:$after)" in query:
                            reads.append(variables)
                            if len(reads) == 2:
                                swap()
                        return graphql(query, variables)
                    self.gh.graphql = changed_inventory
                else:
                    self.gh.on_snapshot = lambda gh: swap() if gh.snapshots == when else None
                with self.assertRaises(Halt):
                    self.create()
                expected = [] if when == "preflight" else ["create", "enroll"] + [
                    "stage-field", "priority-field"][:when - 1]
                self.assertEqual(self.gh.writes, expected)
                if when != "preflight":
                    self.assertEqual(self.receipt()["state"], "incomplete")
                    self.assertIn("Routing status option meanings changed", self.receipt()["error"])

    def test_mapped_priority_meaning_changes_halt_before_writes_or_completion(self):
        for meaning in ("name", "description", "both"):
            for when in ("preflight", "before_issue", "after_issue", 1, 2, 3):
                with self.subTest(meaning=meaning, when=when):
                    self.gh = TicketGitHub(self.root)
                    self.gh.c["priority"] = {"field_id": "priority-field", "options": {
                        code: "priority-" + code for code in LEVELS}}
                    self.gh.metadata()  # Separate routing-cache reads from Priority preflight.
                    path = common_dir(self.root) / "sfc-tickets/finding-1.json"
                    if path.exists():
                        path.unlink()
                    def swap():
                        options = self.gh.fields[-1]["options"]
                        for key in (("name", "description") if meaning == "both" else (meaning,)):
                            options[0][key], options[2][key] = options[2][key], options[0][key]
                    if when in ("preflight", "before_issue"):
                        graphql = self.gh.graphql
                        reads = []
                        def changed_inventory(query, variables):
                            if "fields(first:100,after:$after)" in query:
                                reads.append(variables)
                                if len(reads) == (2 if when == "preflight" else 3):
                                    swap()
                            return graphql(query, variables)
                        self.gh.graphql = changed_inventory
                    elif when == "after_issue":
                        call = self.gh.call
                        def changed_issue(*args, input=None):
                            result = call(*args, input=input)
                            if args[:2] == ("api", "repos/example/app/issues"):
                                swap()
                            return result
                        self.gh.call = changed_issue
                    else:
                        self.gh.on_snapshot = lambda gh: swap() if gh.snapshots == when else None
                    with self.assertRaises(Halt):
                        self.create()
                    expected = ([] if when in ("preflight", "before_issue") else ["create"]
                                if when == "after_issue" else ["create", "enroll"] + [
                                    "stage-field", "priority-field"][:when - 1])
                    self.assertEqual(self.gh.writes, expected)
                    if when == "preflight":
                        self.assertFalse(path.exists())
                    else:
                        receipt = self.receipt()
                        self.assertEqual(receipt["state"], "incomplete")
                        self.assertIn("setup changed", receipt["error"])
                        if when != "before_issue":
                            self.assertEqual(receipt["issue"]["id"], "issue-1")
                        if isinstance(when, int):
                            self.assertEqual(receipt["item_id"], "item-1")
                        writes = self.gh.writes.copy()
                        with self.assertRaisesRegex(Halt, "already recorded"):
                            self.create()
                        self.assertEqual(self.gh.writes, writes)

    def test_mapped_priority_meaning_changes_halt_visibility_setup(self):
        for after_write in (False, True):
            with self.subTest(after_write=after_write):
                self.gh = TicketGitHub(self.root)
                self.gh.c["priority"] = {"field_id": "priority-field", "options": {
                    code: "priority-" + code for code in LEVELS}}
                self.gh.data["project"]["views"]["nodes"][0]["configuration"]["visibleFields"]["nodes"] = []
                dashboard_data = self.gh.dashboard_data
                def changed():
                    result = dashboard_data()
                    if not after_write or self.gh.mutations:
                        self.gh.fields[-1]["options"][2]["description"] = "Human changed meaning"
                    return result
                with patch.object(self.gh, "dashboard_data", side_effect=changed), self.assertRaises(Halt):
                    setup(self.gh, apply=True)
                self.assertEqual(len(self.gh.mutations), int(after_write))
                self.assertEqual(self.gh.writes, [])
                if after_write:
                    receipts = list((self.root / ".sfc/priority-setup").glob("*.json"))
                    self.assertEqual(len(receipts), 1)
                    self.assertEqual(read_json(receipts[0])["state"], "incomplete")

    def test_routing_change_after_issue_creation_prevents_enrollment(self):
        call = self.gh.call
        def renamed(*args, input=None):
            result = call(*args, input=input)
            if args[:2] == ("api", "repos/example/app/issues"):
                self.gh.fields[2]["options"][0]["name"] = "Human renamed Backlog"
            return result
        with patch.object(self.gh, "call", side_effect=renamed), self.assertRaises(Halt):
            self.create()
        self.assertEqual(self.gh.writes, ["create"])
        self.assertEqual(self.receipt()["issue"]["number"], 1)
        self.assertEqual(self.receipt()["state"], "incomplete")

    def test_routing_changes_before_setup_writes_are_preserved(self):
        for with_priority in (False, True):
            with self.subTest(with_priority=with_priority):
                self.gh = TicketGitHub(self.root, with_priority=with_priority)
                self.gh.data["project"]["views"]["nodes"][0]["configuration"]["visibleFields"]["nodes"] = []
                dashboard_data = self.gh.dashboard_data
                def renamed():
                    result = dashboard_data()
                    self.gh.fields[2]["options"][0]["name"] = "Human renamed Backlog"
                    return result
                with patch.object(self.gh, "dashboard_data", side_effect=renamed), self.assertRaises(Halt):
                    setup(self.gh, apply=True)
                self.assertEqual(self.gh.writes + self.gh.mutations, [])

    def test_invalid_live_routing_field_or_options_prevent_creation(self):
        changes = [lambda fields: fields[2].update(dataType="TEXT"),
                   lambda fields: fields[2].update(id="replacement-stage-field"),
                   lambda fields: fields.append({**fields[2], "id": "duplicate-stage-field"}),
                   lambda fields: fields[2]["options"][1].update(id="option-0"),
                   lambda fields: fields[2]["options"][1].update(name="Backlog")]
        for change in changes:
            with self.subTest(change=change):
                self.gh = TicketGitHub(self.root)
                self.gh.metadata()  # Pin the original routing identities/meanings.
                change(self.gh.fields)
                with self.assertRaises(Halt):
                    self.create()
                self.assertEqual(self.gh.writes + self.gh.mutations, [])

    def test_cli_creation_and_explicit_setup_are_real_paths(self):
        write_json(self.root / ".sfc/config.json", self.gh.c)
        body = self.root / "finding.md"
        body.write_text(self.args["body"])
        with patch.object(sfc, "GitHub", return_value=self.gh), contextlib.redirect_stdout(io.StringIO()) as output:
            code = sfc.main(["--root", str(self.root), "create-ticket", "--request-id", "finding-1", "--title", self.args["title"],
                             "--body-file", str(body), "--priority", "P2", "--type", "Feature", "--reason", self.args["reason"], "--unknowns", self.args["unknowns"]])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(output.getvalue())["complete"])
        self.gh = TicketGitHub(self.root, with_priority=False)
        with patch.object(sfc, "GitHub", return_value=self.gh), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(sfc.main(["--root", str(self.root), "priority-setup"]), 2)
            self.assertEqual(self.gh.writes, [])
            self.assertEqual(sfc.main(["--root", str(self.root), "priority-setup", "--apply"]), 0)
        self.assertEqual(self.gh.writes, ["field-create"])

    def test_new_onboarding_real_setup_creates_and_reads_visible_priority(self):
        self.gh = TicketGitHub(self.root, with_priority=False)
        args = SimpleNamespace(repo="example/app", owner="example", project=1, base="main",
                               status_field="SFC Status", verify=["python3 -m unittest"], merge_policy="human", auto_start=False)
        with patch.object(sfc, "GitHub", return_value=self.gh):
            report = sfc.onboard(self.root, args)
        self.assertTrue(report["onboarding_complete"])
        self.assertTrue(report["dashboard"]["priority"]["ready"])
        self.assertEqual(self.gh.writes, ["field-create"])
        self.assertEqual(read_json(Path(report["dashboard"]["priority"]["receipt"]))["state"], "complete")

    def test_real_read_only_dashboard_doctor_status_do_not_repair_missing_priority(self):
        self.gh = TicketGitHub(self.root, with_priority=False)
        write_json(self.root / ".sfc/config.json", {**self.gh.c, "worker_timeout": 2400, "verify_timeout": 1500})
        before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.assertFalse(self.gh.dashboard()["priority"]["ready"])
        with patch.object(sfc, "GitHub", return_value=self.gh), \
                patch.object(sfc, "run", return_value=SimpleNamespace(returncode=0)), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(sfc.main(["--root", str(self.root), "doctor"]), 0)
        doctor = json.loads(output.getvalue())
        self.assertIs(type(doctor["worker_timeout"]), int)
        self.assertEqual(doctor["worker_timeout"], 2400)
        self.assertIs(type(doctor["verify_timeout"]), int)
        self.assertEqual(doctor["verify_timeout"], 1500)
        self.assertTrue(doctor["ready"])
        self.assertFalse(doctor["onboarding_complete"])
        with patch("sfc_lib.factory.GitHub", return_value=self.gh), patch.object(self.gh, "cards", return_value=[]), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(sfc.main(["--root", str(self.root), "status"]), 0)
        self.assertFalse(json.loads(output.getvalue())["priority_setup"]["ready"])
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()})
        self.assertEqual(self.gh.writes + self.gh.mutations, [])

    def test_setup_failure_retains_identity_receipt_and_safe_explicit_repair(self):
        self.gh = TicketGitHub(self.root, with_priority=False)
        self.gh.fail = "field-create"
        with self.assertRaisesRegex(Halt, "receipt"):
            setup(self.gh, apply=True)
        receipts = list((self.root / ".sfc/priority-setup").glob("*.json"))
        self.assertEqual(len(receipts), 1)
        before = receipts[0].read_bytes()
        failed = read_json(receipts[0])
        self.assertEqual(failed["state"], "incomplete")
        self.assertIn("priority-field", [f["id"] for f in failed["observed_fields"]])
        self.gh.fail = None
        self.assertTrue(setup(self.gh, apply=True)["ready"])
        self.assertEqual(self.gh.writes, ["field-create"])
        self.assertEqual(receipts[0].read_bytes(), before)

    def test_lost_responses_recover_actual_issue_and_item_identity_without_retry(self):
        for stage in ("create", "enroll"):
            with self.subTest(stage=stage):
                self.gh = TicketGitHub(self.root)
                self.gh.fail = stage
                path = common_dir(self.root) / "sfc-tickets/finding-1.json"
                if path.exists():
                    path.unlink()
                with self.assertRaises(Halt):
                    self.create()
                self.assertEqual(self.receipt()["issue"]["number"], 1)
                if stage == "enroll":
                    self.assertEqual(self.receipt()["item_id"], "item-1")
                self.assertEqual(self.receipt()["state"], "incomplete")
                self.assertEqual(self.gh.writes.count("create"), 1)

    def test_late_duplicate_priority_and_nonadvancing_inventory_block_all_writes(self):
        self.gh.page_size = 1
        self.gh.fields.append(priority_field())
        self.gh.fields[-1]["id"] = "another-priority"
        with self.assertRaisesRegex(Halt, "ambiguous"):
            setup(self.gh, apply=True)
        self.assertEqual(self.gh.writes, [])
        def stuck(query, variables):
            return {"node": {"fields": {"nodes": [], "pageInfo": {"hasNextPage": True, "endCursor": "same"}}}}
        with patch.object(self.gh, "graphql", side_effect=stuck), self.assertRaisesRegex(Halt, "advance"):
            inventory(self.gh)

    def test_dashboard_reads_all_field_view_and_visible_field_pages(self):
        self.gh.metadata()  # Prime production cache before the pagination-only transport.
        first = copy.deepcopy(self.gh.data)
        first["project"]["fields"]["pageInfo"] = {"hasNextPage": True, "endCursor": "fields-2"}
        first["project"]["views"]["pageInfo"] = {"hasNextPage": True, "endCursor": "views-2"}
        first["project"]["views"]["nodes"][0]["configuration"]["visibleFields"]["pageInfo"] = {"hasNextPage": True, "endCursor": "visible-2"}
        def pages(query, variables):
            if variables.get("after") == "visible-2":
                return {"node": {"configuration": {"visibleFields": {"nodes": [{"id": "last-visible"}], "pageInfo": {"hasNextPage": False}}}}}
            if variables.get("fieldsAfter"):
                result = copy.deepcopy(first)
                result["project"]["fields"] = {"nodes": [{"id": "last-field"}], "pageInfo": {"hasNextPage": False}}
                return result
            if variables.get("viewsAfter"):
                result = copy.deepcopy(first)
                result["project"]["views"] = {"nodes": [view("Other", id="last-view")], "pageInfo": {"hasNextPage": False}}
                return result
            return copy.deepcopy(first)
        with patch.object(self.gh, "graphql", side_effect=pages):
            data = self.gh.dashboard_data()
        self.assertEqual(data["project"]["fields"]["nodes"][-1]["id"], "last-field")
        self.assertEqual(data["project"]["views"]["nodes"][-1]["id"], "last-view")
        self.assertEqual(data["project"]["views"]["nodes"][0]["configuration"]["visibleFields"]["nodes"][-1]["id"], "last-visible")

    def test_routing_field_cannot_be_mapped_as_priority(self):
        self.gh.c["priority"] = {"field_id": "stage-field", "options": {code: f"option-{n}" for n, code in enumerate(LEVELS)}}
        with self.assertRaisesRegex(Halt, "separate"):
            setup(self.gh, apply=True)
        self.assertEqual(self.gh.writes + self.gh.mutations, [])

    def test_origin_mismatch_prevents_any_creation(self):
        with patch.object(self.gh, "verify_repository", side_effect=Halt("origin mismatch")), self.assertRaisesRegex(Halt, "origin mismatch"):
            self.create()
        self.assertEqual(self.gh.writes, [])
        self.assertFalse((common_dir(self.root) / "sfc-tickets").exists())

    def test_ambiguous_identity_recovery_records_candidates_without_guess_or_retry(self):
        self.gh.fail = "create"
        call = self.gh.call
        def ambiguous(*args, input=None):
            result = call(*args, input=input)
            if "?state=" in args[1]:
                second = copy.deepcopy(result[0][0])
                second.update(node_id="issue-2", number=2, html_url="https://github.com/example/app/issues/2")
                result[0].append(second)
            return result
        with patch.object(self.gh, "call", side_effect=ambiguous), self.assertRaises(Halt):
            self.create()
        receipt = self.receipt()
        self.assertIsNone(receipt["issue"])
        self.assertEqual(len(receipt["recovery_issue_candidates"]), 2)
        self.assertEqual(receipt["state"], "incomplete")
        self.assertEqual(self.gh.writes, ["create"])

    def test_grouping_inventory_reads_remaining_pages_without_overwriting_them(self):
        self.gh.metadata()
        first = copy.deepcopy(self.gh.data)
        first["project"]["views"]["nodes"][0]["verticalGroupByFields"]["pageInfo"] = {"hasNextPage": True, "endCursor": "group-2"}
        def pages(query, variables):
            if variables.get("after") == "group-2":
                self.assertIn("verticalGroupByFields(first:100,after:$after)", query)
                return {"node": {"verticalGroupByFields": {"nodes": [{"id": "last-group"}], "pageInfo": {"hasNextPage": False}}}}
            return copy.deepcopy(first)
        with patch.object(self.gh, "graphql", side_effect=pages):
            data = self.gh.dashboard_data()
        self.assertEqual(data["project"]["views"]["nodes"][0]["verticalGroupByFields"]["nodes"], [{"id": "stage-field"}, {"id": "last-group"}])
        self.assertEqual(self.gh.mutations, [])


if __name__ == "__main__":
    unittest.main()
