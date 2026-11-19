from __future__ import annotations

import contextlib
import copy
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from sfc_lib.common import Halt, STATUSES
from sfc_lib.dashboard import STAGE_STYLE, VIEW_NAME
from sfc_lib.activity import FIELD_NAMES
from sfc_lib.github import GitHub

spec = importlib.util.spec_from_file_location("sfc_dashboard_cli", Path(__file__).resolve().parents[1] / "scripts/sfc.py")
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


def field(styled=False):
    return {"id": "stage-field", "name": "SFC Status", "options": [
        {"id": f"option-{index}", "name": name,
         "color": STAGE_STYLE[name][0] if styled else "GRAY",
         "description": STAGE_STYLE[name][1] if styled else ""}
        for index, name in enumerate(STATUSES)]}


def view(name=VIEW_NAME, grouped=True, **changes):
    return {"id": "factory-view", "number": 1, "name": name, "layout": "BOARD_LAYOUT", "filter": "",
            "configuration": {"visibleFields": {"nodes": [{"id": "agent-field"}, {"id": "activity-field"}], "pageInfo": {"hasNextPage": False}}},
            "verticalGroupByFields": {"nodes": [{"id": "stage-field" if grouped else "default-status"}]}, **changes}


class DashboardGitHub(GitHub):
    """In-memory GraphQL boundary; no real project operations in tests."""
    def __init__(self, styled=False, views=None):
        self.data = {"field": field(styled), "project": {"url": "https://github.com/users/example/projects/1",
                     "fields": {"nodes": [{"id": "agent-field", "name": FIELD_NAMES[0], "dataType": "TEXT"}, {"id": "activity-field", "name": FIELD_NAMES[1], "dataType": "TEXT"}], "pageInfo": {"hasNextPage": False}},
                     "views": {"nodes": views if views is not None else [view("View 1", False)],
                               "pageInfo": {"hasNextPage": False}}}}
        self.mutations = []
        self.fail_write = False
        self.corrupt_ids = False

    def priority_setup(self, apply=False):
        return {"ready": True}

    def type_setup(self, apply=False):
        return {"ready": True}

    def metadata(self, create=False):
        return {"id": "project-id", "field": "stage-field",
                "options": {v["name"]: v["id"] for v in self.data["field"]["options"]}}

    def verify_repository(self):
        pass

    def graphql(self, query, variables):
        if not query.lstrip().startswith("mutation"):
            return copy.deepcopy(self.data)
        self.mutations.append(copy.deepcopy(variables["input"]))
        if self.fail_write:
            raise Halt("Ambiguous remote write; inspect before retry")
        payload = variables["input"]
        if "singleSelectOptions" in payload:
            self.data["field"]["options"] = copy.deepcopy(payload["singleSelectOptions"])
            if self.corrupt_ids:
                self.data["field"]["options"][0]["id"] = "changed-id"
            return {"updateProjectV2Field": {"projectV2Field": copy.deepcopy(self.data["field"])}}
        if "dataType" in payload:
            created = {"id": "new-" + payload["name"], "name": payload["name"], "dataType": payload["dataType"]}
            self.data["project"]["fields"]["nodes"].append(created)
            return {"createProjectV2Field": {"projectV2Field": created}}
        if "projectId" in payload:
            created = view(payload["name"], False, id="new-view", number=2)
            self.data["project"]["views"]["nodes"].append(created)
            return {"createProjectV2View": {"projectV2View": {"id": "new-view"}}}
        updated = next(v for v in self.data["project"]["views"]["nodes"] if v["id"] == payload["viewId"])
        if "configuration" in payload:
            updated["configuration"]["visibleFields"]["nodes"] = [{"id": v} for v in payload["configuration"]["visibleFieldIds"]]
        else:
            updated.update({k: v for k, v in payload.items() if k != "viewId"})
        return {"updateProjectV2View": {"projectV2View": {"id": updated["id"]}}}


class DashboardTests(unittest.TestCase):
    def test_defaults_apply_colors_and_emojis_without_recreating_options(self):
        gh = DashboardGitHub()
        before = [(o["id"], o["name"]) for o in gh.data["field"]["options"]]
        result = gh.configure_dashboard()
        self.assertEqual([(o["id"], o["name"]) for o in gh.data["field"]["options"]], before)
        self.assertEqual({o["color"] for o in gh.data["field"]["options"]}, {"GRAY", "BLUE", "ORANGE", "YELLOW", "PURPLE", "RED", "GREEN"})
        self.assertTrue(all(o["description"] for o in gh.data["field"]["options"]))
        self.assertEqual(result["view_name"], VIEW_NAME)
        self.assertFalse(result["ready"])
        self.assertFalse(result["checks"]["saved_columns"])
        self.assertIn("Save view", " ".join(result["pending"]))

    def test_repeated_apply_is_read_only_when_already_configured(self):
        gh = DashboardGitHub(styled=True, views=[view()])
        self.assertTrue(gh.configure_dashboard()["ready"])
        self.assertTrue(gh.configure_dashboard()["ready"])
        self.assertEqual(gh.mutations, [])

    def test_saved_column_grouping_is_required_even_with_styled_factory_view(self):
        gh = DashboardGitHub(styled=True, views=[view(grouped=False)])
        self.assertFalse(gh.dashboard()["ready"])
        gh.data["project"]["views"]["nodes"][0]["verticalGroupByFields"] = {"nodes": [{"id": "stage-field"}]}
        self.assertTrue(gh.dashboard()["ready"])
        self.assertEqual(gh.mutations, [])

    def test_custom_views_filters_and_extra_options_are_preserved(self):
        custom = view("My sprint", False, filter="label:current", layout="TABLE_LAYOUT")
        gh = DashboardGitHub(views=[copy.deepcopy(custom)])
        extra = {"id": "extra-id", "name": "Parked", "color": "PINK", "description": "Existing custom option"}
        gh.data["field"]["options"].append(copy.deepcopy(extra))
        report = gh.configure_dashboard()
        self.assertEqual(gh.data["project"]["views"]["nodes"][0], custom)
        self.assertEqual(gh.data["field"]["options"][-1], extra)
        self.assertEqual(report["view_id"], "new-view")
        self.assertTrue(report["url"].endswith("/views/2"))

    def test_view_one_is_preserved_even_without_a_filter(self):
        for layout in ("ROADMAP_LAYOUT", "TABLE_LAYOUT", "BOARD_LAYOUT"):
            with self.subTest(layout=layout):
                original = view("View 1", True, layout=layout)
                gh = DashboardGitHub(styled=True, views=[copy.deepcopy(original)])
                result = gh.configure_dashboard()
                self.assertEqual(gh.data["project"]["views"]["nodes"][0], original)
                self.assertEqual(result["view_id"], "new-view")
                self.assertFalse(result["ready"])

    def test_ambiguous_factory_views_refuse_all_writes(self):
        gh = DashboardGitHub(views=[view(), view(id="another-view", number=2)])
        with self.assertRaisesRegex(Halt, "Multiple Factory"):
            gh.configure_dashboard()
        self.assertEqual(gh.mutations, [])

    def test_truncated_view_inventory_refuses_all_writes(self):
        gh = DashboardGitHub()
        gh.data["project"]["views"]["pageInfo"]["hasNextPage"] = True
        with self.assertRaisesRegex(Halt, "truncated"):
            gh.configure_dashboard()
        self.assertEqual(gh.mutations, [])

    def test_ambiguous_write_is_not_retried(self):
        gh = DashboardGitHub()
        gh.fail_write = True
        with self.assertRaisesRegex(Halt, "Ambiguous"):
            gh.configure_dashboard()
        self.assertEqual(len(gh.mutations), 1)

    def test_changed_option_id_stops_before_view_mutation(self):
        gh = DashboardGitHub()
        gh.corrupt_ids = True
        with self.assertRaisesRegex(Halt, "preserve"):
            gh.configure_dashboard()
        self.assertEqual(len(gh.mutations), 1)

    def test_custom_stage_order_is_reported_without_silent_reordering(self):
        gh = DashboardGitHub(styled=True, views=[view()])
        gh.data["field"]["options"].reverse()
        result = gh.configure_dashboard()
        self.assertFalse(result["checks"]["stage_order"])
        self.assertFalse(result["ready"])
        self.assertEqual(gh.mutations, [])


class OnboardingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sfc-onboard-")
        self.root = Path(self.temp.name)
        subprocess.run(["git", "init", "-b", "main", str(self.root)], capture_output=True, check=True)
        self.args = SimpleNamespace(repo="example/app", owner="example", project=1, base="main",
                                    status_field="SFC Status", verify=["python3 -m unittest"], merge_policy="human")
        support = patch('sfc_lib.lifeline.supported', return_value=True)
        support.start()
        self.addCleanup(support.stop)

    def tearDown(self):
        self.temp.cleanup()

    def test_default_lifetime_onboarding_persists_scope_without_starting(self):
        from sfc_lib.common import common_dir, read_json
        gh = DashboardGitHub(styled=True, views=[view()])
        with patch.object(cli, 'GitHub', return_value=gh), contextlib.redirect_stdout(io.StringIO()):
            code = cli.main(['--root', str(self.root), 'onboard', '--repo', 'example/app',
                             '--owner', 'example', '--project', '1', '--base', 'main',
                             '--verify', 'python3 -m unittest',
                             '--watch-max-seconds', '600', '--watch-exclude-issue', '15',
                             '--watch-hold-label', 'custom-hold'])
        self.assertEqual(code, 0)
        auth = read_json(common_dir(self.root) / 'sfc-autostart.json')
        self.assertTrue(auth['enabled'])
        self.assertEqual(auth['version'], 2)
        self.assertEqual(auth['mode'], 'interactive-session')
        self.assertTrue(auth['continue_windows'])
        self.assertFalse(auth['scope']['allow_merge'])
        self.assertEqual(auth['scope']['repository'], 'example/app')
        self.assertEqual(auth['scope']['project'], {'owner': 'example', 'number': 1})
        self.assertEqual(auth['scope']['watch']['max_seconds'], 600)
        self.assertEqual(auth['scope']['watch']['excluded_issues'], [15])
        self.assertEqual(auth['scope']['watch']['hold_label'], 'custom-hold')
        self.assertFalse((common_dir(self.root) / 'sfc-watch.json').exists())
        self.assertFalse((self.root / '.sfc/state.json').exists())

    def test_explicit_disabled_choice_and_existing_authority_are_never_overwritten(self):
        from sfc_lib.common import common_dir, write_json
        gh = DashboardGitHub(styled=True, views=[view()])
        self.args.auto_start = False
        with patch.object(cli, 'GitHub', return_value=gh):
            result = cli.onboard(self.root, self.args)
        self.assertEqual(result['autostart'], {'enabled': False})
        self.assertFalse((common_dir(self.root) / 'sfc-autostart.json').exists())
        before = (self.root / '.sfc/config.json').read_bytes()
        authority = common_dir(self.root) / 'sfc-autostart.json'
        write_json(authority, {'enabled': False, 'version': 1, 'state': 'stopped'})
        before_authority = authority.read_bytes()
        del self.args.auto_start
        with self.assertRaises(Halt):
            cli.onboard(self.root, self.args)
        self.assertEqual((self.root / '.sfc/config.json').read_bytes(), before)
        self.assertEqual(authority.read_bytes(), before_authority)

    def test_incomplete_setup_cannot_enable_auto_start(self):
        from sfc_lib.common import common_dir
        gh = DashboardGitHub()
        self.args.auto_start = True
        with patch.object(cli, 'GitHub', return_value=gh):
            result = cli.onboard(self.root, self.args)
        self.assertFalse(result['autostart']['enabled'])
        self.assertEqual(result['autostart']['state'], 'pending-setup')
        self.assertEqual(result['autostart']['requested_mode'], 'interactive-session')
        self.assertFalse((common_dir(self.root) / 'sfc-autostart.json').exists())

    def onboard_cli(self, *options):
        gh = DashboardGitHub(styled=True, views=[view()])
        with patch.object(cli, 'GitHub', return_value=gh), contextlib.redirect_stdout(io.StringIO()) as output:
            code = cli.main(['--root', str(self.root), 'onboard', '--repo', 'example/app',
                             '--owner', 'example', '--project', '1', '--base', 'main',
                             '--verify', 'python3 -m unittest', *options])
        self.assertEqual(code, 0)
        return json.loads(output.getvalue())

    def test_cli_can_disable_default_watching(self):
        from sfc_lib.common import common_dir
        result = self.onboard_cli('--no-auto-start')
        self.assertEqual(result['autostart'], {'enabled': False})
        self.assertFalse((common_dir(self.root) / 'sfc-autostart.json').exists())

    def test_cli_preserves_explicit_legacy_bounded_choice(self):
        result = self.onboard_cli('--no-session-lifetime')
        self.assertTrue(result['autostart']['enabled'])
        self.assertEqual(result['autostart']['version'], 1)
        self.assertNotIn('continue_windows', result['autostart'])
        self.assertFalse(result['autostart']['scope']['allow_merge'])

    def test_conflicting_choices_fail_before_any_setup(self):
        self.args.auto_start, self.args.session_lifetime = False, True
        with patch.object(cli, 'GitHub') as gh, self.assertRaisesRegex(Halt, 'conflicts'):
            cli.onboard(self.root, self.args)
        gh.assert_not_called()
        self.assertFalse((self.root / '.sfc/config.json').exists())

    def test_unsupported_default_keeps_watcher_unavailable_and_configures_other_capabilities(self):
        with patch('sfc_lib.lifeline.supported', return_value=False):
            result = self.onboard_cli()
            self.assertEqual(result['setup_review']['capabilities']['watcher']['status'], 'unavailable')
            self.assertTrue((self.root / '.sfc/config.json').exists())

    def test_new_onboarding_applies_theme_and_reports_pending_saved_view(self):
        gh = DashboardGitHub()
        with patch.object(cli, "GitHub", return_value=gh):
            result = cli.onboard(self.root, self.args)
        self.assertTrue((self.root / ".sfc/config.json").is_file())
        self.assertFalse(result["onboarding_complete"])
        self.assertTrue(result["dashboard"]["checks"]["stage_style"])
        self.assertIn(".sfc/", (self.root / ".git/info/exclude").read_text())
        saved = json.loads((self.root / ".sfc/config.json").read_text())
        self.assertEqual(saved["model"], "gpt-6.1-sol")
        self.assertEqual(saved["reasoning_effort"], "high")
        self.assertEqual(saved["service_tier"], "fast")
        self.assertEqual(saved["activity_timezone"], "UTC")
        self.assertEqual(result["worker_settings"], {"model": "gpt-6.1-sol", "reasoning_effort": "high", "service_tier": "fast", "models": {}})

    def test_onboarding_cli_accepts_custom_model_and_inherited_speed(self):
        gh = DashboardGitHub()
        with patch.object(cli, "GitHub", return_value=gh), contextlib.redirect_stdout(io.StringIO()):
            status = cli.main(["--root", str(self.root), "onboard", "--repo", "example/app", "--owner", "example",
                               "--project", "1", "--base", "main", "--verify", "python3 -m unittest",
                               "--model", "chosen-model", "--reasoning-effort", "medium", "--service-tier", "inherit",
                               "--activity-timezone", "Europe/Stockholm"])
        self.assertEqual(status, 0)
        saved = json.loads((self.root / ".sfc/config.json").read_text())
        self.assertEqual(saved["model"], "chosen-model")
        self.assertEqual(saved["reasoning_effort"], "medium")
        self.assertIsNone(saved["service_tier"])
        self.assertEqual(saved["activity_timezone"], "Europe/Stockholm")

    def test_invalid_timezone_stops_onboarding_before_github_or_config_writes(self):
        self.args.activity_timezone = "Invalid/Zone"
        with patch.object(cli, "GitHub") as github, self.assertRaisesRegex(Halt, "activity_timezone"):
            cli.onboard(self.root, self.args)
        github.assert_not_called()
        self.assertFalse((self.root / ".sfc/config.json").exists())

    def test_onboarding_never_replaces_existing_model_choices(self):
        gh = DashboardGitHub()
        with patch.object(cli, "GitHub", return_value=gh):
            cli.onboard(self.root, self.args)
        path = self.root / ".sfc/config.json"
        before = path.read_bytes()
        self.args.model = "another-model"
        with patch.object(cli, "GitHub", return_value=gh), self.assertRaises(Halt):
            cli.onboard(self.root, self.args)
        self.assertEqual(path.read_bytes(), before)

    def test_partial_setup_preserves_config_for_explicit_dashboard_repair(self):
        gh = DashboardGitHub()
        gh.fail_write = True
        with patch.object(cli, "GitHub", return_value=gh), self.assertRaises(Halt):
            cli.onboard(self.root, self.args)
        self.assertTrue((self.root / ".sfc/config.json").is_file())
        with patch.object(cli, "GitHub", return_value=gh), self.assertRaisesRegex(Halt, "already configured"):
            cli.onboard(self.root, self.args)

    def test_dashboard_cli_returns_incomplete_until_saved_grouping_matches(self):
        gh = DashboardGitHub(styled=True, views=[view(grouped=False)])
        with patch.object(cli, "GitHub", return_value=gh), patch.object(cli, "load_config", return_value={}), contextlib.redirect_stdout(io.StringIO()) as output:
            code = cli.main(["--root", str(self.root), "dashboard"])
        self.assertEqual(code, 2)
        self.assertFalse(json.loads(output.getvalue())["ready"])
        gh.data["project"]["views"]["nodes"][0]["verticalGroupByFields"] = {"nodes": [{"id": "stage-field"}]}
        with patch.object(cli, "GitHub", return_value=gh), patch.object(cli, "load_config", return_value={}), contextlib.redirect_stdout(io.StringIO()) as output:
            code = cli.main(["--root", str(self.root), "dashboard"])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(output.getvalue())["ready"])


if __name__ == "__main__":
    unittest.main()
