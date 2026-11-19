"""Isolated activity contracts: controlled clock, fake adapters and temp repos."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfoNotFoundError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from sfc_lib.activity import Activity, ActivityError, ACTIONS, EventReader, FIELD_NAMES
from sfc_lib.common import Halt, Processes, Stopped, TimedOut, read_json, write_json
from sfc_lib.factory import Factory
from sfc_lib.reasons import reason, render, render_many
from sfc_lib.factory import policy_explanations
from sfc_lib.github import GitHub
from sfc_lib.rate_limit import ConnectionReset
import test_factory as fixtures
import test_dashboard as dashboard


class Clock:
    def __init__(self):
        self.seconds = 0

    def tick(self):
        return self.seconds

    def wall(self):
        import datetime as dt
        return (dt.datetime(2026, 11, 7, tzinfo=dt.timezone.utc) + dt.timedelta(seconds=self.seconds)).isoformat()


class ActivityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sfc-activity-")
        self.root = Path(self.temp.name)
        self.gh = fixtures.FakeGitHub(self.root)
        self.clock = Clock()
        self.records = {}
        self.card = fixtures.issue()
        self.path = self.root / "state.json"
        self.activity = self.new_reporter()

    def tearDown(self):
        self.temp.cleanup()

    def new_reporter(self, **kwargs):
        return Activity(self.gh, lambda c: self.records.setdefault(str(c["number"]), {}),
                        lambda: write_json(self.path, self.records), clock=self.clock.tick, wall=self.clock.wall,
                        **kwargs)

    def test_card_timezone_format_keeps_full_utc_receipts(self):
        cases = [
            ({}, "2026-11-07T21:49:37+00:00", "2026-11-07 21:49 UTC"),
            ({"timezone": "Europe/Stockholm"}, "2026-11-07T21:49:37+00:00", "2026-11-07 22:49 CET"),
            ({"timezone": "Europe/Stockholm"}, "2026-07-07T21:49:37+00:00", "2026-07-07 23:49 CEST"),
            ({"timezone": "Europe/Stockholm"}, "2026-12-31T23:49:37+00:00", "2027-01-01 00:49 CET"),
        ]
        for options, stamp, display in cases:
            with self.subTest(options=options, stamp=stamp):
                self.records, self.gh.activities = {}, {}
                with patch.object(self.clock, "wall", return_value=stamp):
                    self.new_reporter(**options).report(self.card, "Waiting", "human")
                self.assertEqual(self.gh.activities["item-1"][0], f"🟡 Waiting · {display}")
                saved = read_json(self.path)["1"]
                self.assertEqual(saved["activity"]["reported"], stamp)
                self.assertEqual(saved["activity_history"][0]["reported"], stamp)
                self.assertEqual(saved["activity"]["values"], self.gh.activities["item-1"])

    def test_factory_passes_project_timezone_to_activity(self):
        factory = Factory(self.root, config={**fixtures.BASE_CONFIG, "activity_timezone": "Europe/Stockholm"},
                          github=self.gh)
        factory.activity.wall = lambda: "2026-11-07T21:49:37+00:00"
        factory.activity.report(self.card, "Waiting", "human")
        self.assertEqual(self.gh.activities["item-1"][0], "🟡 Waiting · 2026-11-07 22:49 CET")

    def test_utc_default_needs_no_timezone_database_and_unavailable_zone_halts(self):
        with patch("sfc_lib.common.ZoneInfo", side_effect=ZoneInfoNotFoundError("missing data")) as lookup:
            self.new_reporter().report(self.card, "Waiting", "human")
            lookup.assert_not_called()
            self.assertEqual(self.gh.activities["item-1"][0], "🟡 Waiting · 2026-11-07 00:00 UTC")
            count = len(self.gh.activity_writes)
            with self.assertRaisesRegex(Halt, "activity_timezone"):
                self.new_reporter(timezone="Europe/Stockholm")
            self.assertEqual(len(self.gh.activity_writes), count)

    def test_heartbeat_uses_observed_process_and_coalesces_latest_substep(self):
        observe = self.activity.observe(self.card, "build")
        observe("started")
        first = self.gh.activities["item-1"].copy()
        self.clock.seconds = 1
        for _ in range(50):
            observe("command")
        self.assertEqual(len(self.gh.activity_writes), 1)
        self.clock.seconds = 5
        observe("tick")
        self.assertEqual(self.gh.activities["item-1"][1], ACTIONS["command"])
        self.clock.seconds = 64
        observe("tick")
        self.assertEqual(len(self.gh.activity_writes), 2)
        self.clock.seconds = 65
        observe("tick")
        self.assertEqual(len(self.gh.activity_writes), 3)
        self.assertIn("2026-11-07 00:01 UTC", self.gh.activities["item-1"][0])
        self.assertEqual(self.records["1"]["activity"]["reported"], "2026-11-07T00:01:05+00:00")
        self.assertNotEqual(first[0], self.gh.activities["item-1"][0])
        observe("inactive")
        self.clock.seconds = 500
        observe("tick")
        self.assertEqual(len(self.gh.activity_writes), 4)
        self.assertIn("Waiting", self.gh.activities["item-1"][0])

    def test_terminal_and_waiting_states_have_no_heartbeat(self):
        for state, action in (("Waiting", "human"), ("Stopped", "stop"), ("Failed", "failed"),
                              ("Completed", "done"), ("Unknown", "unknown")):
            self.activity.report(self.card, state, action)
            count = len(self.gh.activity_writes)
            self.clock.seconds += 600
            self.activity.report(self.card, state, action)
            self.assertEqual(len(self.gh.activity_writes), count)

    def test_two_concurrent_items_have_independent_actions_heartbeats_and_outcomes(self):
        barrier = threading.Barrier(2)
        def report(card, action, state):
            self.activity.report(card, "Running", action)
            barrier.wait(timeout=5)
            self.activity.report(card, state, "failed" if state == "Failed" else "done")
        threads = [threading.Thread(target=report, args=(fixtures.issue(1), "checks", "Completed")),
                   threading.Thread(target=report, args=(fixtures.issue(2), "review", "Failed"))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
        self.assertIn("Completed", self.gh.activities["item-1"][0])
        self.assertIn("Failed", self.gh.activities["item-2"][0])
        self.assertEqual(self.records["1"]["activity_history"][0]["action"], "checks")
        self.assertEqual(self.records["2"]["activity_history"][0]["action"], "review")
        self.clock.seconds = 60
        self.activity.report(fixtures.issue(1), "Running", "checks")
        self.assertIn("Failed", self.gh.activities["item-2"][0])
        self.activity.report(fixtures.issue(1), "Stopped", "stop")
        self.assertIn("Failed", self.gh.activities["item-2"][0])

    def test_two_live_observers_refresh_only_their_own_timestamps(self):
        one = self.activity.observe(fixtures.issue(1), "checks")
        two = self.activity.observe(fixtures.issue(2), "review")
        one("started")
        two("started")
        initial_two = self.gh.activities["item-2"].copy()
        self.clock.seconds = 60
        one("tick")
        self.assertEqual(self.gh.activities["item-2"], initial_two)
        self.assertIn("2026-11-07 00:01 UTC", self.gh.activities["item-1"][0])
        self.assertEqual(self.records["1"]["activity"]["reported"], "2026-11-07T00:01:00+00:00")
        initial_one = self.gh.activities["item-1"].copy()
        self.clock.seconds = 61
        two("tick")
        self.assertEqual(self.gh.activities["item-1"], initial_one)
        self.assertIn("2026-11-07 00:01 UTC", self.gh.activities["item-2"][0])
        self.assertEqual(self.records["2"]["activity"]["reported"], "2026-11-07T00:01:01+00:00")
        one("inactive")
        self.activity.report(fixtures.issue(1), "Completed", "done")
        self.clock.seconds = 121
        two("tick")
        self.assertIn("Completed", self.gh.activities["item-1"][0])
        self.assertIn("Running", self.gh.activities["item-2"][0])
        self.assertEqual(self.gh.activities["item-2"][1], ACTIONS["review"])
        two("inactive")
        self.activity.report(fixtures.issue(2), "Failed", "failed")
        self.clock.seconds = 500
        one("tick")
        two("tick")
        self.assertIn("Completed", self.gh.activities["item-1"][0])
        self.assertIn("Failed", self.gh.activities["item-2"][0])

    def test_human_edits_are_preserved_and_latched_across_restart(self):
        self.activity.report(self.card, "Running", "build")
        self.gh.activities["item-1"][1] = "Human note: preserve"
        with self.assertRaises(ActivityError):
            self.activity.report(self.card, "Waiting", "human")
        self.records = read_json(self.path)
        with self.assertRaises(ActivityError):
            self.new_reporter().report(self.card, "Failed", "failed")
        self.assertEqual(len(self.gh.activity_writes), 1)
        self.assertEqual(self.gh.activities["item-1"][1], "Human note: preserve")

    def test_partial_failed_and_interrupted_writes_never_retry(self):
        for exception in (Halt("private diagnostic"), KeyboardInterrupt()):
            with self.subTest(exception=type(exception).__name__):
                self.records = {}
                self.gh.activities = {}
                self.activity = self.new_reporter()
                def partial(card, scope, values):
                    self.gh.activities[card["item_id"]] = [values[0], ""]
                    raise exception
                with patch.object(self.gh, "write_activity", side_effect=partial) as write:
                    with self.assertRaises((ActivityError, KeyboardInterrupt)):
                        self.activity.report(self.card, "Running", "build")
                    self.records = read_json(self.path)
                    self.assertTrue(self.records["1"]["activity"]["pending"])
                    with self.assertRaises(ActivityError):
                        self.new_reporter().report(self.card, "Running", "build")
                    self.assertEqual(write.call_count, 1)

    def test_connection_reset_during_activity_write_checks_both_fields_without_replay(self):
        for mode in ('landed', 'partial', 'unchanged'):
            with self.subTest(mode=mode):
                self.records, self.gh.activities = {}, {}
                self.activity = self.new_reporter()
                def interrupted(card, scope, values):
                    if mode == 'landed':
                        self.gh.activities[card['item_id']] = values
                    elif mode == 'partial':
                        self.gh.activities[card['item_id']] = [values[0], '']
                    raise ConnectionReset('fixture reset')
                with patch.object(self.gh, 'write_activity', side_effect=interrupted) as write:
                    if mode == 'landed':
                        self.activity.report(self.card, 'Running', 'build')
                    else:
                        with self.assertRaises(ActivityError):
                            self.activity.report(self.card, 'Running', 'build')
                    saved = read_json(self.path)['1']['activity']
                    self.assertEqual(saved['pending'], mode != 'landed')
                    self.assertTrue(saved['response_lost'])
                    self.assertEqual(saved['previous_values'], ['', ''])
                    self.assertEqual(write.call_count, 1)

    def test_pending_activity_reconciles_only_exact_matching_pair_after_restart(self):
        write = self.gh.write_activity
        def interrupted(*args):
            write(*args)
            raise KeyboardInterrupt
        with patch.object(self.gh, 'write_activity', side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                self.activity.report(self.card, 'Running', 'checks')
        self.records = read_json(self.path)
        original = copy.deepcopy(self.records['1']['activity'])
        exact = self.gh.activities['item-1']
        recovered = self.new_reporter()
        with patch.object(self.gh, 'write_activity', side_effect=AssertionError('No replay')):
            self.gh.activities['item-1'] = [exact[0], 'External edit']
            with self.assertRaises(ActivityError):
                recovered.reconcile(self.card, lambda: None)
            self.assertEqual(read_json(self.path)['1']['activity'], original)
            self.gh.activities['item-1'] = exact
            recovered.reconcile(self.card, lambda: None)
        saved = read_json(self.path)['1']
        self.assertFalse(saved['activity']['pending'])
        self.assertEqual(saved['activity']['values'], exact)
        self.assertEqual(saved['activity_history'][-1], original)

    def test_wrong_readback_and_scope_change_fail_closed(self):
        with patch.object(self.gh, "write_activity") as write:
            with self.assertRaises(ActivityError):
                self.activity.report(self.card, "Running", "qa")
            self.assertEqual(write.call_count, 1)
        self.records, self.gh.activities = {}, {}
        self.activity = self.new_reporter()
        self.activity.report(self.card, "Running", "qa")
        with patch.object(self.gh, "activity_snapshot", return_value=({"project": "other", "fields": ["x", "y"]}, self.gh.activities["item-1"])):
            with self.assertRaises(ActivityError):
                self.activity.report(self.card, "Failed", "failed")
        self.assertEqual(len(self.gh.activity_writes), 1)

    def test_public_text_is_allowlisted_including_arbitrary_private_syntax(self):
        private = "arbitrary-private-value /private/customer.txt command --password=fixture"
        self.card.update(body=private, title=private)
        for state, action in (("Running", "checks"), ("Waiting", "human"), ("Failed", "failed")):
            self.activity.report(self.card, state, action)
        with self.assertRaises(Halt):
            self.activity.report(self.card, "Running", private)
        public = json.dumps(self.gh.activity_writes)
        for fragment in (private, "customer", "password", "fixture"):
            self.assertNotIn(fragment, public)

    def test_json_events_ignore_reasoning_payloads_and_bound_oversize_lines(self):
        path = self.root / "worker.log"
        path.write_bytes(b"")
        events = EventReader(path)
        def append(data):
            with path.open("ab") as output:
                output.write(data)
            return events.poll()
        self.assertIsNone(append(b'{"type":"item.started","item":{"type":"reasoning","text":"editing secret"}}\n'))
        self.assertIsNone(append(b'{"type":"item.started","item":{"type":"command_execution",'))
        self.assertEqual(append(b'"command":"private arbitrary syntax"}}\n'), "command")
        self.assertIsNone(append(b"x" * 140000))
        self.assertEqual(events.poll(), "activity_unavailable")
        self.assertEqual(events.partial, b"")
        self.assertTrue(events.discard)
        self.assertIsNone(events.poll())
        self.assertEqual(append(b'\n{"type":"item.completed","item":{"type":"file_change","status":"completed","changes":["private path"]}}\n'), "edited")
        self.assertIsNone(append(b'{"type":"item.completed","item":{"type":"file_change","status":"failed","changes":["private path"]}}\n'))
        self.assertIsNone(append(b'not-json\n{"type":"item.completed","item":{"type":"agent_message","text":"run checks"}}\n'))

    def test_event_size_boundary_and_ordered_recovery_after_a_drop(self):
        path = self.root / "worker.log"
        command = json.dumps({"type": "item.started", "item": {
            "type": "command_execution"}}).encode()
        # The bound includes the newline; a valid event exactly at it is accepted.
        path.write_bytes(command + b" " * (65535 - len(command)) + b"\n")
        events = EventReader(path)
        self.assertEqual(events.poll(), "command")
        with path.open("ab") as output:
            output.write(command + b" " * (65536 - len(command)) + b"\n")
        self.assertIsNone(events.poll())
        self.assertEqual(events.poll(), "activity_unavailable")
        # Discard a larger line over several bounded reads, then accept later
        # metadata in order instead of latching the unavailable signal forever.
        with path.open("ab") as output:
            output.write(b"x" * 140000)
        self.assertIsNone(events.poll())
        self.assertEqual(events.poll(), "activity_unavailable")
        self.assertIsNone(events.poll())
        with path.open("ab") as output:
            output.write(b"\n" + command + b"\n" + b"x" * 65536 + b"\n" + command + b"\n")
        self.assertEqual(events.poll(), "command")
        self.assertEqual(events.poll(), "command")
        self.assertIsNone(events.poll())
        self.assertFalse(events.discard)
        self.assertEqual(events.partial, b"")

    def test_oversized_completion_resets_each_lane_before_its_next_heartbeat(self):
        for lane in ("build", "qa", "security", "review"):
            with self.subTest(lane=lane):
                self.records = {}
                self.gh = fixtures.FakeGitHub(self.root)
                self.activity = self.new_reporter()
                self.clock.seconds = 0
                observe = self.activity.observe(self.card, lane)
                observe("started")
                path = self.root / "worker.log"
                path.write_text(json.dumps({"type": "item.started", "item": {
                    "type": "command_execution"}}) + "\n")
                events = EventReader(path)
                self.clock.seconds = 5
                observe(events.poll() or "tick")
                self.assertEqual(self.gh.activities["item-1"][1], ACTIONS["command"])
                completion = {"type": "item.completed", "item": {
                    "type": "command_execution", "status": "completed", "exit_code": 0,
                    "aggregated_output": "private-output-" + "x" * 70000}}
                with path.open("a") as output:
                    output.write(json.dumps(completion) + "\n")
                self.clock.seconds = 6
                observations = []
                while events.offset < path.stat().st_size:
                    event = events.poll()
                    observations.append(event)
                    observe(event or "tick")
                self.assertIn("activity_unavailable", observations)
                # Reset the retained action even if the public update is coalesced.
                self.assertEqual(self.gh.activities["item-1"][1], ACTIONS["command"])
                self.clock.seconds = 10
                observe("tick")
                self.assertEqual(self.gh.activities["item-1"][1], ACTIONS[lane])
                self.assertEqual(len(self.gh.activity_writes), 3)
                self.clock.seconds = 70
                observe("tick")
                self.assertEqual(len(self.gh.activity_writes), 4)
                self.assertEqual(self.gh.activities["item-1"][1], ACTIONS[lane])
                self.assertIn("2026-11-07 00:01 UTC", self.gh.activities["item-1"][0])
                self.assertEqual(self.records["1"]["activity"]["reported"], "2026-11-07T00:01:10+00:00")
                self.assertNotIn("private-output", json.dumps(self.gh.activity_writes))

    def test_real_local_process_emits_only_live_observations(self):
        log = self.root / "process.log"
        observed = []
        code = 'import time; print(\'{"type":"item.started","item":{"type":"command_execution","command":"private"}}\', flush=True); time.sleep(0.8)'
        proc = Processes(self.root / "stop")
        result = proc.execute([sys.executable, "-c", code], self.root, log, 5,
                              observer=observed.append, worker_events=True)
        self.assertEqual(result, 0)
        self.assertEqual(observed[0], "started")
        self.assertIn("command", observed)
        self.assertEqual(observed[-1], "inactive")
        self.assertEqual(proc.children, set())
        self.assertFalse(log.with_name(log.name + '.prompt.md').exists())

    def test_worker_stdin_uses_effective_cap_and_preserves_report_format(self):
        class CappedProcesses(Processes):
            def execute(self, argv, cwd, log, timeout, *args, **kwargs):
                return super().execute(argv, cwd, log, min(timeout, 180), *args, **kwargs)

        proc = CappedProcesses(self.root / 'stop')
        argv = [sys.executable, '-c', 'import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())']
        for name, prompt in (('schema', 'Return the required JSON verdict. Inspect résumé and café.\n'),
                             ('native', 'Return the report prefixed with SFC_REVIEW_V1.\n')):
            with self.subTest(format=name):
                folder = self.root / name
                folder.mkdir()
                original = folder / 'prompt.md'
                original.write_text(prompt)
                log = folder / 'worker.log'
                self.assertEqual(proc.execute(argv, self.root, log, 1200, prompt, worker_events=True), 0)
                sent = log.read_text()
                self.assertIn('at most 180 seconds', sent)
                self.assertIn('final 30 seconds', sent)
                self.assertIn('Do not pass incomplete or unsupported obligations.', sent)
                self.assertTrue(sent.endswith('\n\n' + prompt))
                dispatched = log.with_name(log.name + '.prompt.md')
                self.assertEqual(dispatched.read_bytes(), log.read_bytes())
                self.assertEqual(dispatched.stat().st_mode & 0o777, 0o600)
                self.assertEqual(original.read_text(), prompt)

        log = self.root / 'check.log'
        payload = 'Project check input\n'
        self.assertEqual(proc.execute(argv, self.root, log, 180, payload), 0)
        self.assertEqual(log.read_text(), payload)
        self.assertFalse(log.with_name(log.name + '.prompt.md').exists())

    def test_existing_dispatch_prompt_sidecar_does_not_prevent_worker_launch(self):
        log = self.root / 'worker.log'
        dispatched = log.with_name(log.name + '.prompt.md')
        dispatched.write_bytes(b'Existing evidence\n')
        proc = Processes(self.root / 'stop')
        argv = [sys.executable, '-c', 'import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())']
        self.assertEqual(proc.execute(argv, self.root, log, 180, 'Original prompt\n', worker_events=True), 0)
        self.assertEqual(dispatched.read_bytes(), b'Existing evidence\n')
        sent = log.read_bytes()
        self.assertTrue(sent.startswith(b'SFC prompt evidence unavailable (FileExistsError)\n'))
        self.assertIn(b'at most 180 seconds', sent)
        self.assertTrue(sent.endswith(b'Original prompt\n'))

    def test_slow_reporting_cannot_delay_owned_child_stop_or_timeout(self):
        import time
        for stop_requested in (True, False):
            with self.subTest(stop=stop_requested):
                stop = self.root / "stop"
                stop.unlink(missing_ok=True)
                proc = Processes(stop)
                killed_while_reporting = []
                def slow_observer(event):
                    if event != "started":
                        return
                    if stop_requested:
                        stop.touch()
                    deadline = time.monotonic() + 3
                    while time.monotonic() < deadline:
                        with proc.lock:
                            children = list(proc.children)
                        if children and all(child.poll() is not None for child in children):
                            killed_while_reporting.append(True)
                            return
                        time.sleep(0.02)
                    self.fail("Live child survived while activity IO was blocked")
                with self.assertRaises(Stopped if stop_requested else TimedOut):
                    proc.execute([sys.executable, "-c", "import time; time.sleep(30)"],
                                 self.root, self.root / "slow.log", 0.3 if not stop_requested else 10,
                                 observer=slow_observer)
                self.assertEqual(killed_while_reporting, [True])
                self.assertEqual(proc.children, set())


class SetupTests(unittest.TestCase):
    def test_multiselect_custom_field_survives_inventory_visibility_and_repair(self):
        gh = dashboard.DashboardGitHub(styled=True, views=[dashboard.view(filter="label:keep")])
        custom = {"id": "multi", "name": "Custom choices", "dataType": "MULTI_SELECT"}
        gh.data["project"]["fields"]["nodes"].append(custom.copy())
        saved = gh.data["project"]["views"]["nodes"][0]
        saved["configuration"]["visibleFields"]["nodes"] = [{"id": "multi"}]
        saved["verticalGroupByFields"]["nodes"] = [{"id": "multi"}]
        original = gh.graphql

        def graphql(query, variables):
            result = original(query, variables)
            if query.lstrip().startswith("mutation"):
                return result
            # Model GraphQL union projection: an omitted concrete type is {}.
            inventory, views = query.split("views(first:100,after:$viewsAfter)", 1)
            visibility, grouping = views.split("verticalGroupByFields", 1)
            project = result["project"]
            if "... on ProjectV2MultiSelectField { id name dataType }" not in inventory:
                project["fields"]["nodes"][-1] = {}
            for key, section in (("configuration", visibility), ("verticalGroupByFields", grouping)):
                nodes = project["views"]["nodes"][0][key]
                if key == "configuration":
                    nodes = nodes["visibleFields"]
                if "... on ProjectV2MultiSelectField { id }" not in section:
                    nodes["nodes"] = [{} if f["id"] == "multi" else f for f in nodes["nodes"]]
            return result

        with patch.object(gh, "graphql", side_effect=graphql):
            self.assertFalse(gh.dashboard()["checks"]["saved_activity_fields"])
            self.assertEqual(gh.mutations, [])
            report = gh.configure_dashboard()
            self.assertTrue(report["checks"]["saved_activity_fields"])
            self.assertFalse(report["checks"]["saved_columns"])
            count = len(gh.mutations)
            gh.configure_dashboard()
            self.assertEqual(len(gh.mutations), count)
        self.assertEqual(gh.data["project"]["fields"]["nodes"][-1], custom)
        self.assertEqual(saved["configuration"]["visibleFields"]["nodes"],
                         [{"id": "multi"}, {"id": "agent-field"}, {"id": "activity-field"}])
        self.assertEqual(saved["verticalGroupByFields"]["nodes"], [{"id": "multi"}])
        self.assertEqual(saved["filter"], "label:keep")

    def test_explicit_upgrade_creates_text_fields_and_appends_to_saved_visibility(self):
        gh = dashboard.DashboardGitHub(styled=True, views=[dashboard.view(filter="label:keep")])
        gh.data["project"]["fields"]["nodes"] = [{"id": "custom", "name": "Human", "dataType": "TEXT"}]
        gh.data["project"]["views"]["nodes"][0]["configuration"]["visibleFields"]["nodes"] = [{"id": "custom"}]
        before = copy.deepcopy(gh.data)
        self.assertFalse(gh.dashboard()["ready"])
        self.assertEqual(gh.mutations, [])
        self.assertTrue(gh.configure_dashboard()["ready"])
        view = gh.data["project"]["views"]["nodes"][0]
        self.assertEqual(view["filter"], "label:keep")
        self.assertEqual(view["verticalGroupByFields"], before["project"]["views"]["nodes"][0]["verticalGroupByFields"])
        self.assertEqual(view["configuration"]["visibleFields"]["nodes"], [{"id": "custom"}] + [{"id": "new-" + name} for name in FIELD_NAMES])
        self.assertEqual(gh.data["project"]["fields"]["nodes"][0], before["project"]["fields"]["nodes"][0])
        count = len(gh.mutations)
        self.assertTrue(gh.configure_dashboard()["ready"])
        self.assertEqual(len(gh.mutations), count)

    def test_ambiguous_wrong_type_or_truncated_fields_prevent_setup_writes(self):
        for kind in ("duplicate", "type", "truncated", "visible_truncated"):
            gh = dashboard.DashboardGitHub()
            fields = gh.data["project"]["fields"]
            if kind == "duplicate":
                fields["nodes"].append(copy.deepcopy(fields["nodes"][0]))
            elif kind == "type":
                fields["nodes"][0]["dataType"] = "NUMBER"
            elif kind == "truncated":
                fields["pageInfo"]["hasNextPage"] = True
            else:
                gh.data["project"]["views"]["nodes"][0]["configuration"]["visibleFields"]["pageInfo"]["hasNextPage"] = True
            with self.subTest(kind=kind), self.assertRaises(Halt):
                gh.configure_dashboard()
            self.assertEqual(gh.mutations, [])

    def test_visibility_write_must_be_observed_in_saved_readback(self):
        gh = dashboard.DashboardGitHub(styled=True, views=[dashboard.view()])
        gh.data["project"]["views"]["nodes"][0]["configuration"]["visibleFields"]["nodes"] = []
        original = gh.graphql
        attempts = []
        def graphql(query, variables):
            if variables.get("input", {}).get("configuration"):
                attempts.append(copy.deepcopy(variables))
                return {"updateProjectV2View": {"projectV2View": {"id": "factory-view"}}}
            return original(query, variables)
        with patch.object(gh, "graphql", side_effect=graphql), self.assertRaisesRegex(Halt, "not verified"):
            gh.configure_dashboard()
        self.assertEqual(len(attempts), 1)
        self.assertFalse(gh.dashboard()["ready"])


class GitHubActivityContracts(unittest.TestCase):
    def adapter(self):
        with patch("sfc_lib.github.git", return_value="git@github.com:example/app.git"):
            gh = GitHub(Path.cwd(), copy.deepcopy(fixtures.BASE_CONFIG))
        gh._meta = {"id": "project-id", "field": "stage-field"}
        return gh

    def snapshot(self):
        return {"node": {"id": "item-1", "project": {"id": "project-id"},
                         "fields": copy.deepcopy(dashboard.DashboardGitHub(styled=True).data["project"]["fields"]),
                         "content": {"id": "issue-id", "number": 1, "repository": {"nameWithOwner": "example/app"}},
                         "fieldValues": {"pageInfo": {"hasNextPage": False}, "nodes": [
                             {"text": "human custom field", "field": {"id": "custom-field"}},
                             {"text": "current state", "field": {"id": "agent-field"}},
                             {"text": "current action", "field": {"id": "activity-field"}}]}}}

    def test_snapshot_reads_only_target_values_and_checks_item_issue_repository_project(self):
        card = fixtures.issue(id="issue-id")
        gh = self.adapter()
        good = self.snapshot()
        with patch.object(gh, "graphql", return_value=good):
            scope, values = gh.activity_snapshot(card)
        self.assertEqual(scope, {"project": "project-id", "fields": ["agent-field", "activity-field"]})
        self.assertEqual(values, ["current state", "current action"])
        for mutation in (lambda d: d["node"].update(id="other-item"),
                         lambda d: d["node"]["project"].update(id="other-project"),
                         lambda d: d["node"]["content"].update(id="other-issue"),
                         lambda d: d["node"]["content"].update(number=2),
                         lambda d: d["node"]["content"]["repository"].update(nameWithOwner="other/repo"),
                         lambda d: d["node"]["fieldValues"]["pageInfo"].update(hasNextPage=True)):
            changed = copy.deepcopy(good)
            mutation(changed)
            with patch.object(gh, "graphql", return_value=changed), self.assertRaises(Halt):
                gh.activity_snapshot(card)

    def test_missing_upgrade_is_read_only_and_stops_before_process_dispatch(self):
        gh = self.adapter()
        empty = {"node": {"fields": {"nodes": [], "pageInfo": {"hasNextPage": False}}}}
        with patch.object(gh, "graphql", return_value=empty) as query, self.assertRaisesRegex(Halt, "upgrade"):
            gh.activity_snapshot(fixtures.issue(id="issue-id"))
        self.assertEqual(query.call_count, 1)

    def test_mutation_targets_only_two_fields_and_verifies_returned_item_ids(self):
        gh = self.adapter()
        scope = {"project": "project-id", "fields": ["agent-field", "activity-field"]}
        result = {k: {"projectV2Item": {"id": "item-1"}} for k in ("state", "action")}
        with patch.object(gh, "graphql", return_value=result) as query:
            gh.write_activity(fixtures.issue(), scope, ["fixed state", "fixed action"])
        variables = query.call_args.args[1]
        self.assertEqual(set(variables), {"state", "action"})
        self.assertEqual(variables["state"], {"projectId": "project-id", "itemId": "item-1", "fieldId": "agent-field", "value": {"text": "fixed state"}})
        self.assertEqual(variables["action"]["fieldId"], "activity-field")
        result["action"]["projectV2Item"]["id"] = "other-item"
        with patch.object(gh, "graphql", return_value=result), self.assertRaises(Halt):
            gh.write_activity(fixtures.issue(), scope, ["fixed state", "fixed action"])


class ActivityIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.IntegrationTests("runTest")
        self.f.setUp()

    def tearDown(self):
        self.f.tearDown()

    def test_stop_between_cycles_reconciles_only_unfinished_selected_cards(self):
        f = self.f
        f.c["max_workers"] = 3
        f.gh.items = [fixtures.issue(n) for n in (1, 2, 3, 4)]

        barrier = threading.Barrier(3)
        def process(card):
            barrier.wait(5)
            if card["number"] == 1:
                f.gh.move(card, "Review")
                f.factory.activity.report(card, "Waiting", "ci")
            elif card["number"] == 2:
                f.gh.move(card, "Done")
                f.factory.activity.report(card, "Completed", "done")
            else:
                f.gh.move(card, "Blocked")
                f.factory.activity.report(card, "Waiting", "human")

        from sfc_lib import pickup
        original = pickup.cards
        def cards(*args, **kwargs):
            result = original(*args, **kwargs)
            if result[0]["status"] == "Review" and result[1]["status"] == "Done":
                f.proc.stop_path.touch()  # All workers exited; next cycle has not started.
            return result

        with patch.object(f.factory, "process", side_effect=process), patch.object(pickup, "cards", side_effect=cards):
            self.assertEqual(f.factory.run()["outcome"], "stopped")
        self.assertIn("Stopped", f.gh.activities["item-1"][0])
        self.assertEqual(f.gh.activities["item-1"][1], ACTIONS["stop"])
        self.assertIn("Completed", f.gh.activities["item-2"][0])
        self.assertIn("Waiting", f.gh.activities["item-3"][0])
        self.assertEqual(f.gh.activities["item-3"][1], ACTIONS["human"])
        self.assertNotIn("item-4", f.gh.activities)
        self.assertEqual([card["status"] for card in f.gh.items], ["Review", "Done", "Blocked", "Ready"])
        self.assertEqual(f.proc.lanes, [])

    def test_between_cycle_stop_write_failure_halts_without_retry(self):
        f = self.f
        def process(card):
            f.gh.move(card, "Review")
            f.factory.activity.report(card, "Waiting", "ci")

        update_record = f.factory.update
        def update(card, **values):
            update_record(card, **values)
            if values.get("dispatch", {}).get("state") == "finished":
                # The supervisor has collected the completed worker.
                f.proc.stop_path.touch()

        original = f.gh.write_activity
        failed = []
        def write(card, scope, values):
            if values[1] == ACTIONS["stop"]:
                failed.append(values)
                raise Halt("ambiguous stop report")
            return original(card, scope, values)

        with (patch.object(f.factory, "process", side_effect=process),
              patch.object(f.factory, "update", side_effect=update),
              patch.object(f.gh, "write_activity", side_effect=write)):
            with self.assertRaises(ActivityError):
                f.factory.run()
        self.assertEqual(len(failed), 1)
        self.assertEqual(f.factory.state["outcome"], "halted")
        self.assertTrue(f.factory.record(fixtures.issue())["activity"]["pending"])
        self.assertTrue((f.factory.run_dir / "summary.json").is_file())

    def test_stop_before_first_selection_does_not_publish_card_activity(self):
        f = self.f
        with patch.object(f.proc, "stopped", return_value=True):
            self.assertEqual(f.factory.run()["outcome"], "stopped")
        self.assertEqual(f.gh.activity_writes, [])
        self.assertEqual(f.proc.lanes, [])

    def test_two_simultaneous_tickets_one_human_handoff_one_failure(self):
        f = self.f
        f.gh.items = [fixtures.issue(1), fixtures.issue(2)]
        f.c["max_attempts"] = 1
        barrier = threading.Barrier(2)
        def started(lane, cwd):
            if lane == "build":
                barrier.wait(timeout=10)
                self.assertIn("Running", f.gh.activities["item-1"][0])
                self.assertIn("Running", f.gh.activities["item-2"][0])
                barrier.wait(timeout=10)
        f.proc.on_lane = started
        original = f.proc.execute
        def execute(argv, cwd, log, timeout, prompt=None, **kwargs):
            code = original(argv, cwd, log, timeout, prompt, **kwargs)
            if argv[0] == "codex" and Path(cwd).name == "issue-2":
                output = Path(argv[argv.index("--output-last-message") + 1])
                result = read_json(output)
                result.update(verdict="fail", summary="Private fixture detail")
                write_json(output, result)
            return code
        with patch.object(f.proc, "execute", side_effect=execute):
            self.assertEqual(f.factory.run()["outcome"], "waiting")
        self.assertIn("Waiting", f.gh.activities["item-1"][0])
        self.assertEqual(f.gh.activities["item-1"][1], render_many(
            policy_explanations(f.c, fixtures.issue(), f.gh.pr(101), False)) +
            " Project verification: 1 executed, 0 reused.")
        self.assertIn("Failed", f.gh.activities["item-2"][0])
        self.assertEqual(f.gh.merges, [])
        self.assertNotIn("Private fixture detail", json.dumps(f.gh.activity_writes))

    def test_stop_failed_checks_and_exhausted_qa_cannot_reset_attempts(self):
        f = self.f
        f.proc.on_lane = lambda lane, cwd: f.proc.stop_path.touch()
        with self.assertRaises(Stopped):
            f.factory.run()
        self.assertIn("Stopped", f.gh.activities["item-1"][0])
        Path(f.factory.record(fixtures.issue())["worktree"], "value.txt").write_text("original\n")
        f.proc.on_lane = None
        f.gh.items[0]["status"] = "QA"
        f.c["verify_commands"] = ["exit 1"]
        f.c["max_attempts"] = 1
        f.factory = Factory(f.root, f.c, f.gh, f.proc)
        # No PR was published after the stop, so use the QA-only assessment path.
        f.gh.items[0]["labels"] = ["qa"]
        f.factory.run()
        self.assertEqual(f.gh.activities["item-1"][1], render_many([
            reason("check_failed"), reason("attempts", spent=1, limit=1)]))
        f.gh.items[0]["status"] = "QA"
        f.c["verify_commands"] = ["exit 0"]
        f.factory = Factory(f.root, f.c, f.gh, f.proc)
        f.factory.run()
        self.assertIn("Failed", f.gh.activities["item-1"][0])
        self.assertEqual(f.factory.record(f.gh.items[0])['attempts'], 1)
        self.assertEqual(f.gh.items[0]["status"], "QA")
        self.assertIn('attempt', str(f.factory.state['waiting']))

    def test_stale_running_reconciles_unknown_with_local_dependency_wait_without_models(self):
        f = self.f
        f.factory.activity.report(fixtures.issue(), "Running", "build")
        f.gh.items[0]["body"] += "\n## Blocked by\n#2\n"
        f.gh.items.append(fixtures.issue(2, status="Backlog"))
        f.factory = Factory(f.root, f.c, f.gh, f.proc)
        f.factory.run()
        actions = [v[1] for item, v in f.gh.activity_writes if item == "item-1"]
        self.assertIn(ACTIONS["unknown"], actions)
        # Eligibility waits do not annotate a fresh, unclaimed ticket.
        self.assertEqual(actions[-1], ACTIONS['unknown'])
        self.assertIn(render(reason('dependency', issues=[2])), str(f.factory.state['waiting']))
        self.assertEqual(f.proc.lanes, [])

    def test_worker_error_is_failed_and_activity_write_error_cannot_create_success(self):
        f = self.f
        f.proc.malformed = True
        with self.assertRaises(Halt):
            f.factory.run()
        self.assertIn("Failed", f.gh.activities["item-1"][0])
        f.proc.malformed = False
        with patch.object(f.gh, "write_activity", side_effect=Halt("ambiguous")) as write:
            with self.assertRaises(ActivityError):
                f.factory.run()
            self.assertEqual(write.call_count, 1)
        self.assertEqual(f.factory.state["outcome"], "halted")
        self.assertEqual(f.gh.merges, [])
        self.assertEqual(f.gh.prs, {})
        self.assertTrue(f.factory.record(fixtures.issue())["activity"]["pending"])

    def test_unresolved_completion_report_cannot_disappear_on_a_later_drained_run(self):
        f = self.f
        f.gh.items[0]["labels"] = ["qa"]
        original = f.gh.write_activity
        def write(card, scope, values):
            if "Completed" in values[0]:
                raise Halt("completion report outcome unknown")
            return original(card, scope, values)
        with patch.object(f.gh, "write_activity", side_effect=write):
            with self.assertRaises(ActivityError):
                f.factory.run()
        self.assertEqual(f.gh.items[0]["status"], "Done")
        self.assertEqual(f.factory.state["outcome"], "halted")
        f.factory = Factory(f.root, f.c, f.gh, f.proc)
        with patch.object(f.gh, "write_activity") as write:
            with self.assertRaises(ActivityError):
                f.factory.run()
            write.assert_not_called()
        self.assertEqual(f.factory.state["outcome"], "halted")

    def test_finite_limit_is_stopped_not_completed(self):
        f = self.f
        f.c.update(merge_policy="auto", max_cycles=1)
        f.gh.ci = []
        f.factory.run(allow_merge=True)
        self.assertEqual(f.factory.state["outcome"], "cycle-limit")
        self.assertEqual(f.gh.activities["item-1"][1], render(reason("cycle-limit")))
        self.assertIn("Stopped", f.gh.activities["item-1"][0])


if __name__ == "__main__":
    unittest.main()
