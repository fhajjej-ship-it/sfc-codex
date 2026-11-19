"""Real setup/onboarding/CLI/create paths; only transports are fake."""
import contextlib
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import sfc
from sfc_lib.common import Halt, common_dir, git, read_json, validate_config, write_json
from sfc_lib.priority import create_ticket
from sfc_lib.work_types import ALIASES, TYPES, labels, setup
from test_priority import TicketGitHub
from test_dashboard import view


class WorkTypeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="sfc-types-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        git(self.root, "init")
        self.gh = TicketGitHub(self.root)
        self.args = dict(request_id="type-1", title="Supported request", body="## Acceptance criteria\n- [ ] Visible outcome",
                         work_type="Feature", priority="P2", reason="Bounded improvement; explicit fixture evidence",
                         unknowns="Urgency and frequency unknown")

    def create(self, **changes):
        return create_ticket(self.gh, self.root, **(self.args | changes))

    def receipt(self):
        return read_json(common_dir(self.root) / "sfc-tickets/type-1.json")

    def explicit(self):
        self.gh.c["work_types"] = {kind: {"id": r["node_id"], "name": r["name"], "description": r["description"]}
                                   for kind, r in zip(TYPES, self.gh.repo_labels)}

    def reset(self):
        self.gh = TicketGitHub(self.root)
        path = common_dir(self.root) / "sfc-tickets/type-1.json"
        if path.exists():
            path.unlink()  # Independent fixture scenario, never a retry.

    def test_all_five_explicit_choices_receive_exactly_one_label_in_initial_post(self):
        for n, kind in enumerate(TYPES):
            with self.subTest(kind=kind):
                result = self.create(request_id=f"kind-{n}", work_type=kind)
                self.assertTrue(result["complete"])
                self.assertEqual(result["work_type"], kind)
                self.assertEqual(self.gh.issues[n + 1]["labels"], [TYPES[kind][0]])
                receipt = read_json(Path(result["receipt"]))
                self.assertEqual([r["name"] for r in receipt["observed_labels"]], [TYPES[kind][0]])
                self.assertEqual(receipt["verified"]["values"], {"stage-field": "option-0", "priority-field": "priority-P2"})
        self.assertEqual(self.gh.writes.count("create"), 5)
        self.assertNotIn("label-create", self.gh.writes)

    def test_every_default_alias_preserves_name_description_and_color(self):
        for kind, aliases in ALIASES.items():
            for name in aliases:
                with self.subTest(kind=kind, name=name):
                    self.reset()
                    record = self.gh.repo_labels[list(TYPES).index(kind)]
                    record.update(name=name.upper(), color="123abc", description="")
                    if name == "enhancement":
                        record["description"] = "New feature or request"
                    if name == "documentation":
                        record["description"] = "Improvements or additions to documentation"
                    before = copy.deepcopy(self.gh.repo_labels)
                    self.assertTrue(setup(self.gh, apply=True)["ready"])
                    result = self.create(work_type=kind)
                    self.assertEqual(result["label"]["name"], name.upper())
                    self.assertEqual(self.gh.repo_labels, before)

    def test_missing_invalid_type_and_auxiliary_types_fail_before_post(self):
        for changes in ({"work_type": None}, {"work_type": "qa"}, {"work_type": "Technical Debt"},
                        {"labels": ["bug"]}, {"labels": ["unknown"]}, {"labels": ["qa", "QA"]}):
            with self.subTest(changes=changes), self.assertRaises(Halt):
                self.create(**changes)
            self.assertEqual(self.gh.writes + self.gh.mutations, [])
        args = dict(self.args)
        args.pop("work_type")
        with self.assertRaisesRegex(Halt, "assessed work type"):
            create_ticket(self.gh, self.root, **args)

    def test_auxiliary_and_explicit_qa_workflow_labels_are_separate_and_preserved(self):
        for name in ("risk:low", "source:user", "Technical Debt", "qa", "security", "human-added"):
            self.gh.repo_labels.append(dict(node_id="extra-" + name, name=name, description="", color="ededed"))
        def edit(gh):
            if gh.snapshots == 1:
                gh.issues[1]["labels"].append("human-added")
        self.gh.on_snapshot = edit
        self.create(work_type="Maintenance", labels=["risk:low", "source:user", "Technical Debt", "qa", "security"])
        self.assertEqual(set(self.gh.issues[1]["labels"]),
                         {"maintenance", "risk:low", "source:user", "Technical Debt", "qa", "security", "human-added"})
        self.assertNotIn("label-create", self.gh.writes)

    def test_read_only_missing_labels_or_visibility_block_creation(self):
        for missing in ("label", "visibility"):
            with self.subTest(missing=missing):
                self.reset()
                if missing == "label":
                    self.gh.repo_labels.pop()
                else:
                    self.gh.data["project"]["views"]["nodes"][0]["configuration"]["visibleFields"]["nodes"] = []
                before = list(self.root.rglob("*.json"))
                self.assertFalse(setup(self.gh)["ready"])
                with self.assertRaises(Halt):
                    self.create()
                self.assertEqual(self.gh.writes + self.gh.mutations, [])
                self.assertEqual(list(self.root.rglob("*.json")), before)

    def test_explicit_setup_reuses_bootstrap_aliases_creates_only_missing_and_is_idempotent(self):
        self.gh.repo_labels = [
            dict(node_id="existing-bug", name="bug", description="Something isn't working", color="111111"),
            dict(node_id="existing-feature", name="enhancement", description="New feature or request", color="222222"),
            dict(node_id="existing-docs", name="documentation", description="Improvements or additions to documentation", color="333333")]
        saved = self.gh.data["project"]["views"]["nodes"][0]
        saved["filter"] = "label:mine"
        saved["configuration"]["visibleFields"]["nodes"] = [{"id": "custom"}, {"id": "priority-field"}]
        self.gh.data["project"]["views"]["nodes"].append(view("Custom", id="custom-view", filter="assignee:me"))
        before_labels = copy.deepcopy(self.gh.repo_labels)
        before_data = copy.deepcopy(self.gh.data)
        self.gh.items["manual"] = {"values": {"priority-field": "priority-P0", "custom": "mine"}}
        write_json(self.root / ".sfc/config.json", self.gh.c)
        config = (self.root / ".sfc/config.json").read_bytes()
        self.assertTrue(setup(self.gh, apply=True)["ready"])
        self.assertEqual(self.gh.repo_labels[:3], before_labels)
        self.assertEqual(self.gh.writes, ["label-create", "label-create"])
        self.assertEqual(saved["configuration"]["visibleFields"]["nodes"],
                         [{"id": "custom"}, {"id": "priority-field"}, {"id": "labels-field"}])
        before_data["project"]["views"]["nodes"][0]["configuration"] = copy.deepcopy(saved["configuration"])
        self.assertEqual(self.gh.data, before_data)
        self.assertEqual(self.gh.items["manual"]["values"], {"priority-field": "priority-P0", "custom": "mine"})
        writes = copy.deepcopy((self.gh.writes, self.gh.mutations))
        receipts = {p: p.read_bytes() for p in self.root.rglob("*.json")}
        self.assertTrue(setup(self.gh, apply=True)["ready"])
        self.assertEqual((self.gh.writes, self.gh.mutations), writes)
        self.assertEqual({p: p.read_bytes() for p in self.root.rglob("*.json")}, receipts)
        self.assertEqual((self.root / ".sfc/config.json").read_bytes(), config)

    def test_equivalent_feature_aliases_use_canonical_spelling_without_mutating_labels(self):
        self.gh.repo_labels.append(dict(node_id="other", name="enhancement",
                                       description="New feature or request", color="111111"))
        before = copy.deepcopy(self.gh.repo_labels)
        self.gh.page_size = 1
        self.assertEqual(setup(self.gh, apply=True)["mapping"]["Feature"]["name"], "feature")
        self.assertTrue(self.create()["complete"])
        self.assertEqual(self.gh.issues[1]["labels"], ["feature"])
        self.assertEqual(self.gh.repo_labels, before)

    def test_explicit_feature_mapping_wins_over_equivalent_aliases(self):
        self.explicit()
        record = dict(node_id="other", name="enhancement", description="New feature or request", color="111111")
        self.gh.repo_labels.append(record)
        self.gh.c['work_types']['Feature'] = {"id": record['node_id'], "name": record['name'],
                                              "description": record['description']}
        self.assertEqual(setup(self.gh, apply=True)['mapping']['Feature']['id'], 'other')
        self.create()
        self.assertEqual(self.gh.issues[1]['labels'], ['enhancement'])

    def test_equivalent_feature_resolution_is_case_and_order_independent(self):
        from sfc_lib.work_types import resolve
        for first, second in ((None, ''), (TYPES['Feature'][1], 'New feature or request')):
            rows = [dict(id='e', name='Enhancement', description=first, color='111111'),
                    dict(id='f', name='FEATURE', description=second, color='222222')]
            self.assertEqual(resolve(rows, {})['Feature']['id'], 'f')
            self.assertEqual(resolve(rows[::-1], {})['Feature']['id'], 'f')

    def test_conflicting_aliases_and_unsupported_meanings_refuse_setup_before_any_write(self):
        for change in (lambda rows: rows.append(dict(node_id="other", name="enhancement", description="Team-specific incompatible meaning", color="111111")),
                       lambda rows: rows[0].update(description="This means a feature request"),
                       lambda rows: rows.append(copy.deepcopy(rows[0])),
                       lambda rows: rows[0].pop("description"),
                       lambda rows: rows[0].pop("node_id")):
            with self.subTest(change=change):
                self.reset()
                change(self.gh.repo_labels)
                with self.assertRaises(Halt):
                    setup(self.gh, apply=True)
                with self.assertRaises(Halt):
                    self.create()
                self.assertEqual(self.gh.writes + self.gh.mutations, [])

    def test_explicit_custom_mapping_pins_meanings_and_selects_amid_aliases(self):
        for kind, record in zip(TYPES, self.gh.repo_labels):
            record.update(name="team:" + kind.lower(), description="User-approved custom meaning")
        self.explicit()
        self.gh.repo_labels.append(dict(node_id="old-feature", name="enhancement", description="New feature or request", color="123456"))
        before = copy.deepcopy(self.gh.repo_labels)
        self.assertTrue(setup(self.gh, apply=True)["ready"])
        self.create(work_type="Bug")
        self.assertEqual(self.gh.issues[1]["labels"], ["team:bug"])
        self.assertEqual(self.gh.repo_labels, before)

    def test_invalid_conflicting_stale_and_reserved_mappings_fail_before_post(self):
        cases = [lambda c: c.update(work_types={}),
                 lambda c: c["work_types"]["Bug"].update(id="missing"),
                 lambda c: c["work_types"]["Bug"].update(name="enhancement"),
                 lambda c: c["work_types"]["Bug"].update(description="stale"),
                 lambda c: c["work_types"]["Bug"].update(description="New feature or request"),
                 lambda c: c["work_types"].update(Bug=copy.deepcopy(c["work_types"]["Feature"]))]
        cases += [lambda c, name=name: c["work_types"]["Feature"].update(name=name)
                  for name in ("qa", "security", "auth", "billing", "payments", "money", "schema", "destructive", "migration")]
        for change in cases:
            with self.subTest(change=change):
                self.reset()
                self.explicit()
                change(self.gh.c)
                with self.assertRaises(Halt):
                    setup(self.gh, apply=True)
                with self.assertRaises(Halt):
                    self.create()
                self.assertEqual(self.gh.writes + self.gh.mutations, [])

    def test_configuration_rejects_unsafe_mapping_without_network(self):
        self.explicit()
        self.gh.c["work_types"]["Bug"]["name"] = "qa"
        with self.assertRaisesRegex(Halt, "Workflow/merge-policy"):
            validate_config(self.gh.c)

    def test_full_paginated_labels_are_used_for_mapping_and_issue_readback(self):
        self.gh.page_size = 1
        for n in range(101):
            self.gh.repo_labels.insert(0, dict(node_id=f"extra-{n}", name=f"extra-{n}", description="", color="111111"))
        self.assertEqual(len(labels(self.gh)), 106)
        self.create(labels=["extra-100", "extra-99"])
        self.assertEqual(len(self.receipt()["observed_labels"]), 3)

    def test_identity_name_description_or_color_changes_during_creation_halt(self):
        for key, value in (("node_id", "replacement"), ("name", "human-name"),
                           ("description", "Human changed meaning"), ("color", "ffffff")):
            for when in (1, 2, 3):
                with self.subTest(key=key, when=when):
                    self.reset()
                    self.explicit()
                    def edit(gh):
                        if gh.snapshots == when:
                            gh.repo_labels[1][key] = value
                    self.gh.on_snapshot = edit
                    with self.assertRaisesRegex(Halt, "Incomplete ticket creation"):
                        self.create()
                    self.assertEqual(self.receipt()["state"], "incomplete")
                    self.assertEqual(self.gh.repo_labels[1][key], value)
                    self.assertEqual(self.gh.writes, ["create", "enroll"] + ["stage-field", "priority-field"][:when - 1])

    def test_manual_change_clear_or_conflicting_type_never_relabels_or_completes(self):
        for names in ([], ["bug"], ["feature", "bug"]):
            for when in (1, 2, 3):
                with self.subTest(names=names, when=when):
                    self.reset()
                    def edit(gh):
                        if gh.snapshots == when:
                            gh.issues[1]["labels"] = names.copy()
                    self.gh.on_snapshot = edit
                    with self.assertRaises(Halt):
                        self.create()
                    self.assertEqual(self.gh.issues[1]["labels"], names)
                    self.assertEqual(self.receipt()["state"], "incomplete")
                    self.assertEqual(self.receipt()["item_id"], "item-1")
                    writes = self.gh.writes.copy()
                    with self.assertRaisesRegex(Halt, "already recorded"):
                        self.create()
                    self.assertEqual(self.gh.writes, writes)

    def test_missing_posted_label_stops_before_enrollment_and_retains_actual_issue(self):
        call = self.gh.call
        def dropped(*args, **kwargs):
            result = call(*args, **kwargs)
            if args[:2] == ("api", "repos/example/app/issues"):
                self.gh.issues[1]["labels"] = []
            return result
        self.gh.call = dropped
        with self.assertRaises(Halt):
            self.create()
        self.assertEqual(self.gh.writes, ["create"])
        self.assertEqual(self.receipt()["issue"]["id"], "issue-1")
        self.assertEqual(self.receipt()["labels_after_failure"], [])

    def test_new_alias_added_after_preflight_halts_before_post(self):
        report = setup(self.gh)
        # Creation calls the module setup directly; mutate on its next inventory.
        def edit(gh, endpoint):
            if gh.label_reads == 3:
                gh.repo_labels.append(dict(node_id="new-alias", name="enhancement", description="", color="111111"))
        self.gh.label_reads = 0
        self.gh.on_label_read = edit
        with self.assertRaises(Halt):
            self.create()
        self.assertEqual(self.gh.writes, [])
        self.assertTrue(report["ready"])

    def test_partial_or_lost_creation_writes_never_duplicate_with_type_assigned(self):
        for failure in ("create", "enroll", "stage-field", "after-priority-field", "drop-priority-field"):
            with self.subTest(failure=failure):
                self.reset()
                self.gh.fail = failure
                with self.assertRaises(Halt):
                    self.create()
                receipt = self.receipt()
                self.assertEqual(receipt["state"], "incomplete")
                self.assertEqual(receipt["issue"]["id"], "issue-1")
                self.assertEqual(self.gh.issues[1]["labels"], ["feature"])
                writes = self.gh.writes.copy()
                with self.assertRaisesRegex(Halt, "already recorded"):
                    self.create()
                self.assertEqual(self.gh.writes, writes)

    def test_lost_label_creation_is_journaled_and_not_blindly_retried(self):
        self.gh.repo_labels.pop()
        self.gh.fail = "label-create"
        with self.assertRaisesRegex(Halt, "receipt"):
            setup(self.gh, apply=True)
        receipts = list((self.root / ".sfc/type-setup").glob("*.json"))
        self.assertEqual(len(receipts), 1)
        receipt = read_json(receipts[0])
        self.assertEqual(receipt["state"], "incomplete")
        self.assertEqual(receipt["observed"]["mapping"]["Maintenance"]["name"], "maintenance")
        before = receipts[0].read_bytes()
        self.gh.fail = None
        self.assertTrue(setup(self.gh, apply=True)["ready"])
        self.assertEqual(self.gh.writes, ["label-create"])
        self.assertEqual(receipts[0].read_bytes(), before)

    def test_missing_write_and_pending_visibility_repair_are_latched(self):
        self.gh.repo_labels.pop()
        self.gh.fail = "drop-label-create"
        with self.assertRaisesRegex(Halt, "readback"):
            setup(self.gh, apply=True)
        self.gh.fail = None
        with self.assertRaisesRegex(Halt, "Unresolved"):
            setup(self.gh, apply=True)
        self.assertEqual(self.gh.writes, ["label-create"])

    def test_unfinished_setup_intents_after_process_exit_block_mutations(self):
        for state in ("creating_label", "label_created", "setting_visibility", "incomplete"):
            with self.subTest(state=state):
                self.reset()
                self.gh.repo_labels.pop()
                path = self.root / ".sfc/type-setup/interrupted.json"
                write_json(path, {"state": state, "next_label": {"name": "maintenance"}})
                before = path.read_bytes()
                self.assertFalse(setup(self.gh)["ready"])
                with self.assertRaisesRegex(Halt, "Unresolved"):
                    setup(self.gh, apply=True)
                self.assertEqual(self.gh.writes + self.gh.mutations, [])
                self.assertEqual(path.read_bytes(), before)

    def test_setup_detects_existing_label_meaning_edit_after_create_and_preserves_it(self):
        self.gh.repo_labels.pop()
        call = self.gh.call
        def edit(*args, **kwargs):
            result = call(*args, **kwargs)
            if args[:2] == ("api", "repos/example/app/labels"):
                self.gh.repo_labels[0]["description"] = "Human meaning edit"
            return result
        self.gh.call = edit
        with self.assertRaisesRegex(Halt, "receipt"):
            setup(self.gh, apply=True)
        self.assertEqual(self.gh.repo_labels[0]["description"], "Human meaning edit")
        self.assertEqual(self.gh.writes, ["label-create"])
        self.assertEqual(self.gh.mutations, [])
        receipt = read_json(next((self.root / ".sfc/type-setup").glob("*.json")))
        self.assertTrue(any(r["description"] == "Human meaning edit" for r in receipt["observed_labels"]))
        self.assertEqual(receipt["created_label"]["name"], "maintenance")
        self.assertIn("observed", receipt["readback_errors"])

    def test_removed_label_or_replaced_identity_during_setup_is_not_recreated(self):
        for change in (lambda rows: rows.pop(0), lambda rows: rows[0].update(node_id="replacement")):
            with self.subTest(change=change):
                self.reset()
                self.gh.repo_labels.pop()
                call = self.gh.call
                def edited(*args, **kwargs):
                    result = call(*args, **kwargs)
                    if args[:2] == ("api", "repos/example/app/labels"):
                        change(self.gh.repo_labels)
                    return result
                self.gh.call = edited
                with self.assertRaisesRegex(Halt, "readback"):
                    setup(self.gh, apply=True)
                self.assertEqual(self.gh.writes, ["label-create"])
                self.assertEqual(self.gh.mutations, [])
                # Archive only this completed test scenario's receipt so the
                # next subtest is an independent setup, not a retry.
                for receipt in (self.root / ".sfc/type-setup").glob("*.json"):
                    receipt.rename(receipt.with_suffix(".fixture-done"))

    def test_dropped_or_lost_visibility_write_never_reports_false_completion(self):
        for failure in ("drop", "lost"):
            with self.subTest(failure=failure):
                self.reset()
                self.gh.data["project"]["views"]["nodes"][0]["configuration"]["visibleFields"]["nodes"] = []
                graphql = self.gh.graphql
                def changed(query, variables):
                    if query.lstrip().startswith("mutation") and "viewId" in variables["input"]:
                        if failure == "drop":
                            self.gh.mutations.append(copy.deepcopy(variables["input"]))
                            return {"updateProjectV2View": {"projectV2View": {"id": "factory-view"}}}
                        graphql(query, variables)
                        raise Halt("Visibility response lost")
                    return graphql(query, variables)
                self.gh.graphql = changed
                with self.assertRaisesRegex(Halt, "receipt"):
                    setup(self.gh, apply=True)
                self.assertEqual(len(self.gh.mutations), 1)
                self.gh.graphql = graphql
                if failure == "lost":
                    self.assertTrue(setup(self.gh, apply=True)["ready"])
                else:
                    with self.assertRaisesRegex(Halt, "Unresolved"):
                        setup(self.gh, apply=True)
                self.assertEqual(len(self.gh.mutations), 1)
                for receipt in (self.root / ".sfc/type-setup").glob("*.json"):
                    receipt.rename(receipt.with_suffix(".fixture-done"))

    def test_saved_visibility_mismatch_latches_incomplete_setup(self):
        saved = self.gh.data["project"]["views"]["nodes"][0]
        saved["configuration"]["visibleFields"]["nodes"] = []
        self.gh.fail = "visibility-readback"
        with self.assertRaisesRegex(Halt, "readback"):
            setup(self.gh, apply=True)
        self.assertEqual(len(self.gh.mutations), 1)
        receipt = read_json(next((self.root / ".sfc/type-setup").glob("*.json")))
        self.assertEqual(receipt["state"], "incomplete")
        self.assertEqual(receipt["observed_view"]["filter"], "label:human-edit")

    def test_real_onboarding_and_cli_upgrade_and_creation(self):
        self.gh.repo_labels = []
        self.gh.data["project"]["views"]["nodes"][0]["configuration"]["visibleFields"]["nodes"].remove({"id": "labels-field"})
        args = SimpleNamespace(repo="example/app", owner="example", project=1, base="main",
                               status_field="SFC Status", verify=["python3 -m unittest"], merge_policy="human", auto_start=False)
        with patch.object(sfc, "GitHub", return_value=self.gh):
            result = sfc.onboard(self.root, args)
        self.assertTrue(result["onboarding_complete"])
        self.assertTrue(result["dashboard"]["work_types"]["ready"])
        self.assertEqual(self.gh.writes, ["label-create"] * 5)
        body = self.root / "body.md"
        body.write_text(self.args["body"])
        with patch.object(sfc, "GitHub", return_value=self.gh), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(sfc.main(["--root", str(self.root), "type-setup", "--apply"]), 0)
            self.assertEqual(sfc.main(["--root", str(self.root), "create-ticket", "--request-id", "type-1",
                "--title", self.args["title"], "--body-file", str(body), "--priority", "P2", "--type", "Refactor",
                "--reason", self.args["reason"], "--unknowns", self.args["unknowns"]]), 0)
        self.assertEqual(self.gh.issues[1]["labels"], ["refactor"])
        self.assertIn('"complete": true', output.getvalue())

    def test_cli_existing_project_upgrade_preserves_config_and_missing_type_is_required(self):
        self.gh.repo_labels.pop()
        write_json(self.root / ".sfc/config.json", self.gh.c)
        path = self.root / ".sfc/config.json"
        before = path.read_bytes()
        with patch.object(sfc, "GitHub", return_value=self.gh), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(sfc.main(["--root", str(self.root), "type-setup"]), 2)
            self.assertEqual(self.gh.writes, [])
            self.assertEqual(sfc.main(["--root", str(self.root), "type-setup", "--apply"]), 0)
        self.assertEqual(self.gh.writes, ["label-create"])
        self.assertEqual(path.read_bytes(), before)
        with patch.object(sfc, "GitHub") as adapter, contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            sfc.main(["--root", str(self.root), "create-ticket", "--request-id", "type-1", "--title", "Test",
                      "--body-file", "unused.md", "--priority", "P2", "--reason", "Evidence", "--unknowns", "Unknown"])
        self.assertEqual(error.exception.code, 2)
        adapter.assert_not_called()

    def test_real_read_only_status_doctor_dashboard_and_preflight_never_migrate(self):
        self.gh.repo_labels = []
        write_json(self.root / ".sfc/config.json", self.gh.c)
        before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.assertFalse(self.gh.dashboard()["work_types"]["ready"])
        with patch.object(sfc, "GitHub", return_value=self.gh), patch.object(sfc, "run", return_value=SimpleNamespace(returncode=0)):
            self.assertFalse(sfc.doctor(self.root)["onboarding_complete"])
        with patch("sfc_lib.factory.GitHub", return_value=self.gh), patch.object(self.gh, "cards", return_value=[]), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(sfc.main(["--root", str(self.root), "status"]), 0)
        self.assertFalse(json.loads(output.getvalue())["type_setup"]["ready"])
        with patch.object(sfc, "GitHub", return_value=self.gh), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(sfc.main(["--root", str(self.root), "type-setup"]), 2)
        self.assertEqual({p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}, before)
        with self.assertRaises(Halt):
            self.create()
        self.assertEqual(self.gh.writes + self.gh.mutations, [])
        after = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        # Authorized creation initializes only its empty coordination lock;
        # missing-setup preflight still cannot migrate any product/config data.
        self.assertEqual(after.pop(self.root / ".git/sfc-create.lock"), b"")
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
