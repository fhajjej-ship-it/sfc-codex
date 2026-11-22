"""Automatic recovery using temporary git repositories and fake adapters."""
import copy
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from sfc_lib import blocked_recovery as recovery, delivery, pickup, qa_repair
from sfc_lib.common import Halt, Stopped, TimedOut, common_dir, read_json, write_json
from sfc_lib.factory import Factory
from sfc_lib.rate_limit import ReadConnectionReset
from sfc_lib.watch import Watch
from test_watch import Clock
from test_pickup import PickupGitHub, ticket
import test_factory as fixtures


class BlockedRecoveryTests(unittest.TestCase):
    tearDown = fixtures.IntegrationTests.tearDown

    def setUp(self):
        fixtures.IntegrationTests.setUp(self)
        self.c['auto_max_lines'] = 800
        self.gh = PickupGitHub(self.root, [ticket()])
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        self.clock = Clock()
        self.clock.active = lambda: bool(self.factory.occupied)
        self.options = {'poll_interval': 1, 'max_polls': 5, 'max_seconds': 60, 'max_claims': 10}

    def watch(self, resume=None, *, allow_merge=False):
        return self.factory.watch(allow_merge=allow_merge, clock=self.clock, wait=self.clock.wait, overrides=self.options,
                                  **({'resume': resume} if resume else {'start': True}))

    def missing_checks(self, structured=False, *, legacy=False, remove_logs=True):
        execute = self.proc.execute
        def blocked(argv, cwd, log, timeout, prompt=None, **kwargs):
            code = execute(argv, cwd, log, timeout, prompt, **kwargs)
            if argv[0] == 'codex' and self.proc.lanes[-1] == 'security':
                r = self.factory.state['cards']['1']
                if not structured and remove_logs:
                    for check in r['verification']['checks']:
                        Path(check['log']).unlink()
                path = Path(argv[argv.index('--output-last-message') + 1])
                result = read_json(path)
                result.update(verdict='blocked', summary='Execution evidence missing from packet')
                if structured:
                    packet = json.loads(prompt.split('Supervisor project-check evidence', 1)[1].splitlines()[1])
                    result['recovery'] = {'kind': 'missing-check-evidence', 'check_ids': [packet['checks'][0]['id']]}
                write_json(path, result)
            return code

        failure = self.factory.fail
        def record_failure(card, result):
            if not legacy:
                return failure(card, result)
            # Older fail() cleared the live receipt pointers and persisted no
            # structured marker. The accepted lane files stayed on disk.
            with patch.object(self.factory, 'verification_evidence', return_value=None):
                failure(card, result)
            record = self.factory.record(card)
            self.assertNotIn('receipts', record)
            self.assertNotIn('verification', record)
            record.pop('blocker')
            self.factory.save()

        with (patch.object(self.proc, 'execute', side_effect=blocked),
              patch.object(self.factory, 'fail', side_effect=record_failure)):
            self.factory.run(include_backlog=True, issues=(1,))
        self.card = pickup.cards(self.gh)[0]
        self.r = self.factory.record(self.card)
        self.assertEqual(self.r['status'], 'Blocked')
        if legacy:
            self.assertNotIn('blocker', self.r)
        else:
            self.assertEqual(self.r['blocker']['kind'], 'missing-check-evidence')
        self.old = copy.deepcopy(self.r)

    def prepare_watch(self):
        f = self.factory
        f.watcher = Watch(f, start=True, overrides=self.options, clock=self.clock, wait=self.clock.wait)
        f.include_backlog = True
        f.issue_scope = (1,)
        f.watcher.begin(False)
        self.card = pickup.cards(self.gh)[0]

    def test_failed_project_check_retries_exact_pr_and_runs_normal_reviews(self):
        execute = self.proc.execute
        checks = []

        def fail_once(argv, cwd, log, *args, **kwargs):
            if argv[0] != 'codex':
                checks.append(recovery.git(cwd, 'rev-parse', 'HEAD'))
                if len(checks) == 1:
                    Path(log).write_text('Fixture prerequisite unavailable\n')
                    return 1
            return execute(argv, cwd, log, *args, **kwargs)

        with patch.object(self.proc, 'execute', side_effect=fail_once):
            self.watch()
        r = self.factory.state['cards']['1']
        retry, = r['blocked_recoveries']
        self.assertEqual(retry['blocker']['kind'], recovery.FAILED_CHECK)
        self.assertEqual(checks, [r['head'], r['head']])
        self.assertEqual(retry['blocker']['binding']['pr'], r['pr'])
        self.assertEqual(retry['phase'], 'finished')
        self.assertEqual(r['attempts'], 0)
        self.assertEqual(r['handoff'], r['head'])
        self.assertEqual(self.proc.lanes, ['build', 'qa', 'security', 'review'])
        self.assertEqual(self.gh.merges, [])

    def test_failed_project_check_spends_only_one_retry_across_restart(self):
        self.c['watch'] = {'max_recoveries_per_ticket': 3}
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        execute = self.proc.execute
        checks = []

        def fail_check(argv, cwd, log, *args, **kwargs):
            if argv[0] == 'codex':
                return execute(argv, cwd, log, *args, **kwargs)
            checks.append(recovery.git(cwd, 'rev-parse', 'HEAD'))
            Path(log).write_text('Fixture prerequisite still unavailable\n')
            return 1

        with patch.object(self.proc, 'execute', side_effect=fail_check):
            self.watch()
            self.factory = Factory(self.root, self.c, self.gh, self.proc)
            self.watch()
        r = self.factory.state['cards']['1']
        retry, = r['blocked_recoveries']
        self.assertEqual(retry['phase'], 'failed')
        self.assertEqual(checks, [r['head'], r['head']])
        self.assertEqual(self.proc.lanes, ['build'])
        self.assertEqual(r['status'], 'Blocked')
        self.assertEqual(self.gh.merges, [])

    def test_failed_project_check_recovers_legacy_blocked_build_at_attempt_cap(self):
        from sfc_lib.ticket_recovery import grant

        self.c['max_attempts'] = 1
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        execute = self.proc.execute

        def fail_check(argv, cwd, log, *args, **kwargs):
            if argv[0] == 'codex':
                return execute(argv, cwd, log, *args, **kwargs)
            Path(log).write_text('Recorded failed supervisor check\n')
            return 1

        with patch.object(self.proc, 'execute', side_effect=fail_check):
            self.factory.run(include_backlog=True, issues=(1,))
        (self.factory.dir / 'stop').touch()
        grant(self.factory, issue=1, request_id='prior-check-recovery', reason='Fixture recovery', grant_one=True)

        def blocked_build(argv, cwd, log, *args, **kwargs):
            code = execute(argv, cwd, log, *args, **kwargs)
            if argv[0] == 'codex':
                path = Path(argv[argv.index('--output-last-message') + 1])
                write_json(path, {**read_json(path), 'verdict': 'blocked'})
            return code

        with (patch.object(self.proc, 'execute', side_effect=blocked_build),
              patch.object(recovery, 'SUPPORTED', recovery.SUPPORTED - {recovery.FAILED_CHECK})):
            self.watch()
        old = copy.deepcopy(self.factory.state['cards']['1'])
        self.assertEqual(old['status'], 'Blocked')
        self.assertEqual(old['ticket_recoveries'][0]['status'], 'spent')
        self.assertEqual(self.proc.lanes, ['build', 'build'])
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        self.proc.stop_path.write_text('operator stop\n')
        self.proc.shared_stop_path.write_text('shared stop\n')
        state_before = self.factory.state_path.read_bytes()
        session_path = common_dir(self.root) / 'sfc-watch.json'
        session_before = session_path.read_bytes()
        writes_before = list(self.gh.writes)
        executions_before = list(self.proc.events)
        preview = self.factory.watch(dry_run=True, overrides=self.options)
        self.assertEqual(preview['selected'], [1])
        self.assertFalse(preview['options']['allow_merge'])
        self.assertEqual(self.factory.state_path.read_bytes(), state_before)
        self.assertEqual(session_path.read_bytes(), session_before)
        self.assertEqual(self.proc.stop_path.read_text(), 'operator stop\n')
        self.assertEqual(self.proc.shared_stop_path.read_text(), 'shared stop\n')
        self.assertEqual(self.gh.writes, writes_before)
        self.assertEqual(self.proc.events, executions_before)
        from sfc_lib.verification import fingerprint_budget
        with self.assertRaisesRegex(Stopped, 'Stop requested during verification fingerprinting'):
            fingerprint_budget(self.factory)()
        self.watch()
        r = self.factory.state['cards']['1']
        self.assertEqual(self.proc.lanes, ['build', 'build', 'qa', 'security', 'review'])
        self.assertEqual(r['handoff'], old['head'])
        for key in ('pr', 'head', 'base', 'worktree', 'attempts', 'ticket_recoveries'):
            self.assertEqual(r[key], old[key], key)
        self.assertEqual(self.gh.merges, [])

    def test_143_checks_same_candidate_and_reviews_without_build(self):
        self.missing_checks()
        before = len(self.proc.lanes)
        self.watch()
        r = self.factory.record(self.card)
        self.assertEqual(self.proc.lanes[before:], ['qa', 'security', 'review'])
        for key in ('worktree', 'branch', 'pr', 'head', 'base', 'claim', 'attempts'):
            self.assertEqual(r[key], self.old[key], key)
        self.assertEqual(r['worker_output_budget']['build'], self.old['worker_output_budget']['build'])
        self.assertEqual(len([e for e in self.proc.events if e[0] == 'check']), 2)
        self.assertTrue(r['verification']['passed'])
        self.assertTrue(all(Path(c['log']).is_file() for c in r['verification']['checks']))
        self.assertEqual(r['blocked_recoveries'][0]['resumed_stage'], 'QA')
        self.assertEqual(r['blocked_recoveries'][0]['phase'], 'finished')
        self.assertTrue(r['handoff'])
        self.assertEqual(self.gh.merges, [])
        self.assertTrue(any('checks executed' in note for note in self.gh.notes))
        report = read_json(self.factory.state['delivery_report']['path'])
        reported = report['blocked_recovery']['1']
        self.assertEqual(reported['blocked_recoveries'][0]['resumed_stage'], 'QA')
        self.assertIn('merge_authority', [value['code'] for value in reported['handoff_reasons']])
        self.assertGreaterEqual(r['blocked_recoveries'][0]['blocked_wait_seconds'], 0)

    def test_143_upgrade_recovers_split_suite_logs_from_preexisting_blocked_record(self):
        from sfc_lib.verification import RUNTIME_SUITE, REMAINING_SUITE
        self.c['verify_commands'] = [RUNTIME_SUITE, REMAINING_SUITE]
        self.c['max_attempts'] = 1
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        execute = self.proc.execute
        observed = []

        def split_checks(argv, cwd, log, timeout, prompt=None, **kwargs):
            if argv[0] != 'codex':
                command = argv[-1]
                self.assertIn(command, self.c['verify_commands'])
                observed.append((command, recovery.git(cwd, 'rev-parse', 'HEAD')))
                Path(log).write_text(command + '\nAll fixture checks passed\n')
                return 0
            return execute(argv, cwd, log, timeout, prompt, **kwargs)

        with patch.object(self.proc, 'execute', side_effect=split_checks):
            self.missing_checks(legacy=True)
            self.factory = Factory(self.root, self.c, self.gh, self.proc)
            before = list(self.proc.lanes)
            self.watch()
        record = self.factory.record(self.card)
        self.assertEqual(self.proc.lanes[len(before):], ['qa', 'security', 'review'])
        self.assertEqual(observed, [(command, self.old['head'])
                                   for command in self.c['verify_commands'] * 2])
        self.assertEqual(record['attempts'], 1)
        for key in ('worktree', 'branch', 'pr', 'head', 'base', 'claim', 'attempts'):
            self.assertEqual(record[key], self.old[key], key)
        for check in record['verification']['checks']:
            self.assertIn('All fixture checks passed', Path(check['log']).read_text())
        self.assertEqual(self.gh.merges, [])

    def test_legacy_worker_prose_with_intact_supervisor_evidence_remains_blocked(self):
        self.missing_checks(legacy=True, remove_logs=False)
        before = (list(self.proc.lanes), list(self.gh.writes))
        self.watch()
        self.assertEqual((self.proc.lanes, self.gh.writes), before)
        self.assertFalse(self.factory.record(self.card).get('blocked_recoveries'))

    def test_historical_delivery_report_uses_the_selected_run_snapshot(self):
        self.missing_checks()
        initial_run = self.factory.run_dir.name
        self.watch()
        report = delivery.report(self.root, run_id=initial_run)
        self.assertEqual(report['blocked_recovery']['1']['blocker']['kind'], 'missing-check-evidence')
        self.assertEqual(report['blocked_recovery']['1']['blocked_recoveries'], [])

    def test_failed_recovery_spent_without_repeat_dispatch(self):
        self.missing_checks()
        execute = self.proc.execute
        def fail(argv, cwd, log, *args, **kwargs):
            if argv[0] != 'codex':
                self.clock.value += 2
                Path(log).write_text('fixture check failure\n')
                return 1
            return execute(argv, cwd, log, *args, **kwargs)
        before = len(self.proc.lanes)
        with patch.object(self.proc, 'execute', side_effect=fail):
            self.watch()
        r = self.factory.record(self.card)
        self.assertEqual(len(self.proc.lanes), before)
        self.assertEqual(len(r['blocked_recoveries']), 1)
        action = r['blocked_recoveries'][0]
        self.assertEqual(action['phase'], 'failed')
        self.assertEqual(action['checks'], r['verification']['checks'])
        self.assertEqual(action['checks'][0]['exit_code'], 1)
        self.assertEqual(Path(action['checks'][0]['log']).read_text(), 'fixture check failure\n')
        self.assertGreaterEqual(action['elapsed_seconds'], 2)
        report = delivery.report(self.root)
        self.assertEqual(report['blocked_recovery']['1']['blocked_recoveries'], [action])
        self.assertTrue(any('failed on configured check 1' in note for note in self.gh.notes))
        self.assertEqual(r['status'], 'Blocked')
        self.assertIn('allowance exhausted', self.factory.state['waiting'][0]['reasons'][0])
        self.watch()
        self.assertEqual(len(self.factory.record(self.card)['blocked_recoveries']), 1)

    def test_hold_exclusion_human_task_drift_spend_nothing(self):
        self.missing_checks()
        original = copy.deepcopy(self.gh.items)
        for kind in ('hold', 'exclude', 'human', 'task'):
            with self.subTest(kind=kind):
                self.gh.items = copy.deepcopy(original)
                r = self.factory.record(self.card)
                r['blocker'] = copy.deepcopy(self.old['blocker'])
                self.options['excluded_issues'] = []
                if kind == 'hold': self.gh.items[0]['labels'].append('sfc-hold')
                if kind == 'exclude': self.options['excluded_issues'] = [1]
                if kind == 'human': r['blocker']['kind'] = 'human-or-unsupported'
                if kind == 'task': self.gh.items[0]['body'] += '\nNew task'
                self.factory.save()
                before = len(self.proc.lanes)
                self.watch()
                self.assertEqual(len(self.proc.lanes), before)
                self.assertFalse(self.factory.record(self.card).get('blocked_recoveries'))

    def test_source_drift_prevents_dispatch(self):
        self.missing_checks()
        Path(self.r['worktree'], 'value.txt').write_text('changed by another owner\n')
        before = len(self.proc.lanes)
        self.watch()
        self.assertEqual(len(self.proc.lanes), before)
        self.assertFalse(self.factory.record(self.card).get('blocked_recoveries'))
        self.assertIn('drift', self.factory.record(self.card)['recovery_wait'])

    def test_missing_evidence_leaves_a_repair_attempt_for_a_recovered_qa_finding(self):
        self.options['max_polls'] = 12
        self.missing_checks(structured=True)
        self.assertEqual(self.r['attempts'], 0)
        execute = self.proc.execute
        review_heads = []
        prompts = []

        def worker(argv, cwd, log, timeout, prompt=None, **kwargs):
            code = execute(argv, cwd, log, timeout, prompt, **kwargs)
            if argv[0] != 'codex':
                return code
            lane = self.proc.lanes[-1]
            output = Path(argv[argv.index('--output-last-message') + 1])
            if lane == 'qa':
                review_heads.append(recovery.git(cwd, 'rev-parse', 'HEAD'))
                prompts.append(prompt)
                if len(review_heads) == 1:
                    result = read_json(output)
                    result.update(verdict='fail', findings=[
                        {'file': 'value.txt', 'line': 1, 'severity': 'medium', 'message': 'Value must be corrected'}])
                    write_json(output, result)
            elif lane == 'build':
                self.assertIn('Reproduce each finding', prompt)
                self.assertIn('Value must be corrected', prompt)
                (Path(cwd) / 'value.txt').write_text('corrected\n')
                result = read_json(output)
                result['evidence'] = 'Value assertion failed before the edit and passed afterward.'
                write_json(output, result)
            return code

        before = len(self.proc.lanes)
        with patch.object(self.proc, 'execute', side_effect=worker):
            self.watch()
        record = self.factory.record(self.card)
        self.assertEqual(self.proc.lanes[before:], ['qa', 'build', 'qa', 'security', 'review'])
        self.assertEqual(record['attempts'], 1)
        self.assertEqual(record['pr'], self.old['pr'])
        self.assertNotEqual(review_heads[0], review_heads[1])
        self.assertEqual(review_heads[-1], record['head'])
        self.assertIn('Value assertion failed before the edit and passed afterward.', prompts[-1])
        self.assertTrue(record['handoff'])

    def test_no_change_qa_repair_parks_only_its_ticket(self):
        self.options['max_polls'] = 12
        self.missing_checks(structured=True)
        self.gh.items.append(ticket(2, priority='P3'))
        execute = self.proc.execute

        def worker(argv, cwd, log, timeout, prompt=None, **kwargs):
            code = execute(argv, cwd, log, timeout, prompt, **kwargs)
            if argv[0] == 'codex' and 'an SFC qa worker' in prompt and Path(cwd).name == 'issue-1':
                output = Path(argv[argv.index('--output-last-message') + 1])
                result = read_json(output)
                result.update(verdict='fail', findings=[
                    {'file': 'value.txt', 'line': 1, 'severity': 'medium', 'message': 'Value must be corrected'}])
                write_json(output, result)
            return code

        with patch.object(self.proc, 'execute', side_effect=worker):
            self.watch()
        record = self.factory.record(self.card)
        self.assertEqual(record['status'], 'Blocked')
        self.assertEqual(record['head'], self.old['head'])
        self.assertEqual(record['pr'], self.old['pr'])
        self.assertEqual(len(record['worker_output_budget']['build']['dispatches']), 2)
        self.assertIn('QA repair did not change', record['blocker']['reason'])
        self.assertIn('Value must be corrected', record['feedback'])
        self.assertTrue(self.factory.state['cards']['2']['handoff'])

    def assert_qa_repair_read_defers(self, *, after_commit, lose_push_response=False):
        self.options['max_polls'] = 18
        self.c['max_workers'] = 1
        self.missing_checks(structured=True)
        execute = self.proc.execute
        method = 'pull_requests' if after_commit else 'pr_state'
        lookup = getattr(self.gh, method)
        corrections = []
        failures = []
        pushes = []
        old = self.old
        worktree = Path(old['worktree'])

        def worker(argv, cwd, log, timeout, prompt=None, **kwargs):
            code = execute(argv, cwd, log, timeout, prompt, **kwargs)
            if argv[0] != 'codex' or Path(cwd) != worktree:
                return code
            lane = self.proc.lanes[-1]
            if lane == 'qa' and not corrections:
                output = Path(argv[argv.index('--output-last-message') + 1])
                result = read_json(output)
                result.update(verdict='fail', findings=[
                    {'file': 'value.txt', 'line': 1, 'severity': 'medium', 'message': 'Correct the value'}])
                write_json(output, result)
            if lane == 'build':
                corrections.append(cwd)
                (worktree / 'value.txt').write_text('corrected\n')
            return code

        def unavailable(key):
            record = self.factory.record(self.card)
            head = recovery.git(worktree, 'rev-parse', 'HEAD')
            right_repair = key == (old['branch'] if after_commit else old['pr']) and record.get('qa_repair')
            right_stage = head != old['head'] if after_commit else record['status'] == 'Building'
            if right_repair and right_stage and not self.factory.state['cards'].get('2', {}).get('handoff'):
                if not failures:
                    failures.append(head)
                    self.gh.items.append(ticket(2, priority='P3'))
                raise ReadConnectionReset('fixture transient QA-repair PR lookup')
            return lookup(key)

        def git_with_response_loss(cwd, command, *args):
            result = recovery.git(cwd, command, *args)
            if command == 'push':
                pushes.append(args)
                if lose_push_response:
                    raise Halt('fixture push completed but its response was lost')
            return result

        with (patch.object(self.proc, 'execute', side_effect=worker),
              patch.object(self.gh, method, side_effect=unavailable),
              patch.object(qa_repair, 'git', side_effect=git_with_response_loss)):
            self.watch()
        record = self.factory.record(self.card)
        self.assertEqual(len(failures), 1)
        self.assertEqual(len(corrections), 1)
        self.assertEqual(pushes, [('origin', f"{record['head']}:refs/heads/{old['branch']}")])
        self.assertEqual(record['pr'], old['pr'])
        self.assertEqual(record['attempts'], 1)
        self.assertTrue(self.factory.state['cards']['2']['handoff'])
        self.assertEqual(record['handoff'], record['head'])
        if after_commit:
            self.assertEqual(record['head'], failures[0])

    def test_qa_repair_pr_read_after_commit_reuses_the_correction(self):
        self.assert_qa_repair_read_defers(after_commit=True)

    def test_qa_repair_queue_pr_read_defers_to_other_tickets(self):
        self.assert_qa_repair_read_defers(after_commit=False)

    def test_qa_repair_reads_back_a_lost_push_response(self):
        self.assert_qa_repair_read_defers(after_commit=True, lose_push_response=True)

    def assert_qa_rechecks_the_original_finding(self, *, base_conflict):
        self.options['max_polls'] = 18
        self.c['max_attempts'] = 3
        self.missing_checks(structured=True)
        worktree = Path(self.old['worktree'])
        execute = self.proc.execute
        qa = self.factory.qa
        builds = []
        qa_prompts = []
        new_bases = []

        def worker(argv, cwd, log, timeout, prompt=None, **kwargs):
            if not base_conflict and argv[0] != 'codex' and Path(cwd) == worktree and len(builds) == 1:
                return execute(['/bin/sh', '-c', 'exit 1'], cwd, log, timeout, prompt, **kwargs)
            code = execute(argv, cwd, log, timeout, prompt, **kwargs)
            if argv[0] != 'codex' or Path(cwd) != worktree:
                return code
            lane = self.proc.lanes[-1]
            output = Path(argv[argv.index('--output-last-message') + 1])
            if lane == 'qa':
                qa_prompts.append(prompt)
                if not builds:
                    result = read_json(output)
                    result.update(verdict='fail', findings=[{
                        'file': 'value.txt', 'line': 1, 'severity': 'medium', 'message': 'Original value defect'}])
                    write_json(output, result)
            elif lane == 'build':
                builds.append(cwd)
                (worktree / 'value.txt').write_text(f'correction revision {len(builds)}\n')
                if len(builds) == 1:
                    result = read_json(output)
                    result['evidence'] = 'Focused reproduction of the original defect after correction'
                    write_json(output, result)
            return code

        def advance_base_before_first_corrected_qa(card, cwd):
            if base_conflict and len(builds) == 1 and not new_bases:
                (self.root / 'value.txt').write_text('conflicting main edit\n')
                recovery.git(self.root, 'add', 'value.txt')
                recovery.git(self.root, 'commit', '-m', 'Advance the fake remote base')
                recovery.git(self.root, 'push', 'origin', 'refs/heads/main:refs/heads/main')
                new_bases.append(recovery.git(self.root, 'rev-parse', 'HEAD'))
            return qa(card, cwd)

        with (patch.object(self.proc, 'execute', side_effect=worker),
              patch.object(self.factory, 'qa', side_effect=advance_base_before_first_corrected_qa)):
            self.watch()
        record = self.factory.record(self.card)
        self.assertEqual(len(builds), 2)
        self.assertEqual(record['attempts'], 2)
        self.assertEqual(len(qa_prompts), 2)
        self.assertEqual(record['pr'], self.old['pr'])
        self.assertEqual(record['handoff'], record['head'])
        self.assertNotEqual(record['head'], record['qa_repair']['publication']['head'])
        if base_conflict:
            self.assertEqual(record['base'], new_bases[0])
            self.assertEqual(record['qa_repair']['base'], self.old['base'])
            divergent = {**record['qa_repair'], 'base': record['qa_repair']['publication']['head']}
            with self.assertRaises(recovery.RecoveryPinMismatch):
                qa_repair.require_review_lineage(record, worktree, divergent, record['head'], record['base'])
        self.assertIn('Original value defect', qa_prompts[-1])
        self.assertIn('Focused reproduction of the original defect after correction', qa_prompts[-1])

    def test_qa_rechecks_the_original_finding_after_a_followup_build(self):
        self.assert_qa_rechecks_the_original_finding(base_conflict=False)

    def test_qa_rechecks_the_original_finding_after_resolving_a_base_conflict(self):
        self.assert_qa_rechecks_the_original_finding(base_conflict=True)

    def test_structured_split_check_request_at_findings_cap_keeps_attempts(self):
        self.c['verify_commands'] = ['python3 -c "print(\'runtime suite executed\')"',
                                     'python3 -c "print(\'remaining suite executed\')"']
        self.missing_checks(structured=True)
        self.r['attempts'] = self.c['max_attempts']
        recovery.mark(self.factory, self.card, 'missing-check-evidence', 'QA')
        before = len(self.proc.lanes)
        self.watch()
        r = self.factory.record(self.card)
        self.assertEqual(r['attempts'], self.c['max_attempts'])
        self.assertEqual(self.proc.lanes[before:], ['qa', 'security', 'review'])
        self.assertEqual(len([e for e in self.proc.events if e[0] == 'check']), 4)
        logs = ''.join(Path(c['log']).read_text() for c in r['verification']['checks'])
        self.assertIn('runtime suite executed', logs)
        self.assertIn('remaining suite executed', logs)

    def test_structured_policy_accepts_null_and_recovers_explicit_check_request(self):
        from test_review_policy import PolicyProcesses, capabilities

        self.c['review_policy'] = {'mode': 'baseline'}
        self.proc = PolicyProcesses(self.root / '.sfc/stop')
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        blocked = []

        def mutate(result):
            result['recovery'] = None
            if result['scope']['obligation'] == 'security' and not blocked:
                blocked.append(True)
                result.update(verdict='blocked', recovery={
                    'kind': 'missing-check-evidence', 'check_ids': ['check-1']})

        self.proc.mutate = mutate
        with patch('sfc_lib.review_policy.run', new=capabilities):
            self.factory.run(include_backlog=True, issues=(1,))
            card = pickup.cards(self.gh)[0]
            self.assertEqual(card['status'], 'Blocked')
            self.assertEqual(self.factory.record(card)['blocker']['kind'], 'missing-check-evidence')
            self.watch()
        self.assertEqual(self.proc.lanes, ['build', 'qa', 'security', 'security', 'review'])
        record = self.factory.record(card)
        self.assertEqual(record['receipts']['security']['provider']['version'], 'codex-cli fixture')
        self.assertTrue(record['handoff'])

    def test_pr_read_failure_defers_without_spending_or_parking(self):
        self.missing_checks()
        self.prepare_watch()
        writes = list(self.gh.writes)
        with patch.object(self.factory, 'assert_remote', side_effect=ReadConnectionReset('fixture read reset')):
            self.assertFalse(recovery.run(self.factory, self.card))
        self.assertNotIn('recovery_wait', self.r)
        self.assertFalse(self.r.get('blocked_recoveries'))
        self.assertEqual(self.gh.writes, writes)
        self.clock.value += self.options['poll_interval']
        self.assertTrue(recovery.run(self.factory, self.card))

    def assert_pr_read_defers_to_independent_work(self, method):
        self.missing_checks()
        self.gh.items.append(ticket(2, priority='P3'))
        original = getattr(self.gh, method)
        failures = []

        def read(number):
            independent = self.factory.state['cards'].get('2', {})
            if number == self.old['pr'] and not independent.get('handoff'):
                failures.append(self.clock())
                raise ReadConnectionReset('fixture unavailable blocked PR')
            return original(number)

        with patch.object(self.gh, method, side_effect=read):
            self.watch()
        self.assertEqual(len(failures), 1)
        self.assertTrue(self.factory.state['cards']['2']['handoff'])
        record = self.factory.record(self.card)
        self.assertTrue(record['handoff'])
        self.assertEqual(len(record['blocked_recoveries']), 1)

    def test_queue_pr_state_read_defers_to_independent_work(self):
        self.assert_pr_read_defers_to_independent_work('pr_state')

    def test_pr_preflight_read_defers_to_independent_work(self):
        # The real adapter's PR-state read is independent of its full PR read.
        with patch.object(self.gh, 'pr_state', side_effect=lambda number: self.gh.prs[number]['state']):
            self.assert_pr_read_defers_to_independent_work('pr')

    def test_prerequisite_reopened_during_checks_defers_and_reuses_exact_results(self):
        self.gh.items[0]['body'] += '\n## Blocked by\n- #9\n'
        self.gh.extra_issues[9] = ticket(9)
        self.gh.extra_issues[9]['state'] = 'CLOSED'
        self.missing_checks()
        self.gh.items.append(ticket(2, priority='P3'))
        verify = self.factory.verify

        def reopen(card, *args, **kwargs):
            self.assertTrue(verify(card, *args, **kwargs))
            if card['number'] == 1:
                self.gh.extra_issues[9]['state'] = 'OPEN'
            return True

        writes = len(self.gh.writes)
        checks_at_closure = []

        def resolve_after_independent_work():
            if not self.factory.state['cards'].get('2', {}).get('handoff') or checks_at_closure:
                return
            record = self.factory.record(self.card)
            self.assertEqual(record['status'], 'Blocked')
            self.assertEqual(record['blocked_recoveries'][0]['phase'], 'reserved')
            self.assertTrue(all(number == 2 for number, _ in self.gh.writes[writes:]))
            checks_at_closure.append(sum(e[0] == 'check' for e in self.proc.events))
            self.gh.extra_issues[9]['state'] = 'CLOSED'

        self.clock.on_wait = resolve_after_independent_work
        with patch.object(self.factory, 'verify', side_effect=reopen):
            self.watch()
        self.assertTrue(checks_at_closure)
        self.assertTrue(self.factory.state['cards']['2']['handoff'])
        self.assertTrue(self.factory.record(self.card)['handoff'])
        self.assertEqual(sum(e[0] == 'check' for e in self.proc.events), checks_at_closure[0])

    def test_hold_before_recovery_checks_spends_no_action(self):
        self.missing_checks()
        self.prepare_watch()
        fetch = self.factory.fetch

        def hold():
            self.gh.items[0]['labels'].append('sfc-hold')
            return fetch()

        before = (list(self.proc.events), list(self.gh.writes))
        with patch.object(self.factory, 'fetch', side_effect=hold):
            self.assertFalse(recovery.run(self.factory, self.card))
        self.assertEqual((self.proc.events, self.gh.writes), before)
        self.assertFalse(self.r.get('blocked_recoveries'))
        self.assertFalse(self.r.get('recovery_wait'))

    def test_source_changed_during_final_check_preflight_prevents_execution(self):
        self.missing_checks(structured=True)
        self.prepare_watch()
        verification = self.r['verification']

        def edit(query, variables):
            if 'fieldValues(first:100,after:$after)' in query and self.r.get('blocked_recoveries'):
                (Path(self.r['worktree']) / 'value.txt').write_text('changed during recovery preflight\n')

        self.gh.on_read = edit
        before = list(self.proc.events)
        self.assertFalse(recovery.run(self.factory, self.card))
        self.assertEqual(self.proc.events, before)
        self.assertEqual(self.r['verification'], verification)
        self.assertEqual(self.r['status'], 'Blocked')
        self.assertIn('drift', self.r['recovery_wait'])

    def assert_held_check_failure_stays_local(self, *, timeout):
        self.missing_checks(structured=True)
        self.prepare_watch()
        execution = self.proc.execute

        def held(argv, cwd, log, *args, **kwargs):
            self.gh.items[0]['labels'].append('sfc-hold')
            if timeout:
                Path(log).write_text('held during timed-out check\n')
                raise TimedOut('fixture check timeout')
            return execution(['/bin/sh', '-c', 'exit 1'], cwd, log, *args, **kwargs)

        before = (list(self.gh.activity_writes), list(self.gh.notes), list(self.gh.writes))
        with patch.object(self.proc, 'execute', side_effect=held):
            self.assertFalse(recovery.run(self.factory, self.card))
        self.assertEqual((self.gh.activity_writes, self.gh.notes, self.gh.writes), before)
        self.assertEqual(self.r['status'], 'Blocked')
        self.assertEqual(self.r['blocked_recoveries'][0]['phase'], 'failed')

    def test_failed_recovery_check_cannot_write_activity_on_live_hold(self):
        self.assert_held_check_failure_stays_local(timeout=False)

    def test_timed_out_recovery_check_cannot_write_activity_on_live_hold(self):
        self.assert_held_check_failure_stays_local(timeout=True)

    def test_restart_reports_saved_failed_checks_without_repeating_them(self):
        self.missing_checks(structured=True)
        self.prepare_watch()
        verify = self.factory.verify
        execution = self.proc.execute

        def fail_check(argv, cwd, log, *args, **kwargs):
            return execution(['/bin/sh', '-c', 'exit 1'], cwd, log, *args, **kwargs)

        def interrupt(*args, **kwargs):
            self.assertFalse(verify(*args, **kwargs))
            raise Halt('fixture interrupted after saving failed check receipt')

        with (patch.object(self.proc, 'execute', side_effect=fail_check),
              patch.object(self.factory, 'verify', side_effect=interrupt),
              self.assertRaisesRegex(Halt, 'after saving failed check receipt')):
            recovery.run(self.factory, self.card)

        session = self.factory.watcher.session['id']
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        before = list(self.proc.events)
        self.watch(resume=session)
        record = self.factory.record(self.card)
        self.assertEqual(self.proc.events, before)
        self.assertEqual(record['status'], 'Blocked')
        self.assertEqual(record['blocked_recoveries'][0]['phase'], 'failed')
        self.assertEqual(record['activity']['action'], 'check_failed')
        self.assertTrue(any('failed on configured check 1' in note for note in self.gh.notes))

    def test_qa_only_consumes_recovered_check_receipt(self):
        self.gh.items[0]['labels'].append('qa')
        self.missing_checks(structured=True)
        checks = sum(e[0] == 'check' for e in self.proc.events)
        lanes = len(self.proc.lanes)
        self.watch()
        r = self.factory.record(self.card)
        self.assertEqual(r['status'], 'Done')
        self.assertEqual(self.proc.lanes[lanes:], ['qa', 'security', 'review'])
        self.assertEqual(sum(e[0] == 'check' for e in self.proc.events), checks + 1)
        self.assertEqual(r['verification'], r['blocked_recoveries'][0]['verification'])
        [completion] = [note for note in self.gh.notes if note.startswith('SFC QA-only assessment passed')]
        self.assertIn(r['base'], completion)
        self.assertNotIn(str(self.root), completion)

    def test_reopened_prerequisite_during_final_checks_cannot_merge(self):
        self.c['merge_policy'] = 'auto'
        self.assert_reopened_prerequisite_blocks_merge('gate-')
        self.assertEqual(self.gh.merges, [])

    def test_reopened_prerequisite_during_local_final_checks_cannot_merge(self):
        from test_local_merge import PriorityAtomicGitHub
        self.c.update(merge_policy='auto', merge_mode='local-verification')
        self.gh = PriorityAtomicGitHub(self.root, self.remote)
        self.assert_reopened_prerequisite_blocks_merge('local-gate-')
        self.assertEqual(self.gh.attempts, [])

    def assert_reopened_prerequisite_blocks_merge(self, gate_prefix):
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        self.gh.items[0]['body'] += '\n## Blocked by\n- #9\n'
        self.gh.extra_issues[9] = {**ticket(9), 'state': 'CLOSED'}
        self.missing_checks()
        verify = self.factory.verify

        def reopen(card, worktree, *args, **kwargs):
            result = verify(card, worktree, *args, **kwargs)
            if worktree.name.startswith(gate_prefix):
                self.gh.extra_issues[9]['state'] = 'OPEN'
            return result

        with patch.object(self.factory, 'verify', side_effect=reopen):
            self.watch(allow_merge=True)
        r = self.factory.record(self.card)
        self.assertEqual(self.gh.extra_issues[9]['state'], 'OPEN')
        self.assertEqual(r['status'], 'Blocked')
        self.assertEqual(r['blocker']['kind'], 'dependency')
        self.assertEqual(r['verification'], r['blocked_recoveries'][0]['verification'])

    def test_hold_added_during_live_dependency_read_prevents_dispatch(self):
        self.gh.items[0]['body'] += '\n## Blocked by\n- #9\n'
        self.gh.extra_issues[9] = {**ticket(9), 'state': 'CLOSED'}
        self.missing_checks()
        self.prepare_watch()
        reads = 0

        def hold(query, variables):
            nonlocal reads
            if variables.get('number') == 9:
                reads += 1
                if reads == 2:
                    self.gh.items[0]['labels'].append('sfc-hold')

        self.gh.on_read = hold
        before = (list(self.proc.events), list(self.gh.writes))
        self.assertFalse(recovery.run(self.factory, self.card))
        self.assertEqual((self.proc.events, self.gh.writes), before)
        self.assertFalse(self.r.get('blocked_recoveries'))

    def test_failed_qa_only_lane_does_not_move_ticket_held_during_review(self):
        self.gh.items[0]['labels'].append('qa')
        self.missing_checks(structured=True)

        def hold(lane, _):
            if lane == 'review':
                self.gh.items[0]['labels'].append('sfc-hold')

        self.proc.on_lane = hold
        self.proc.fail_lane = 'review'
        before = len(self.gh.writes)
        self.watch()
        r = self.factory.record(self.card)
        self.assertEqual(r['status'], 'QA')
        self.assertEqual(self.gh.writes[before:], [(1, 'QA')])
        self.assertEqual(r['attempts'], self.old['attempts'])
        self.assertFalse(r.get('recovery_wait'))

    def test_hold_in_last_qa_lane_prevents_review_stage_write(self):
        self.missing_checks()

        def hold(lane, _):
            if lane == 'security':
                self.gh.items[0]['labels'].append('sfc-hold')

        self.proc.on_lane = hold
        before = len(self.gh.writes)
        self.watch()
        r = self.factory.record(self.card)
        self.assertEqual(r['status'], 'QA')
        self.assertNotIn((1, 'Review'), self.gh.writes[before:])
        self.assertEqual(r['blocked_recoveries'][0]['phase'], 'admitted')
        self.assertFalse(r.get('recovery_wait'))

    def test_hold_at_resumed_dispatch_leaves_allowance_for_other_work(self):
        self.c['max_no_progress'] = 1
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        self.missing_checks()
        self.prepare_watch()
        self.assertTrue(recovery.run(self.factory, self.card))
        self.gh.items.append(ticket(2, priority='P3'))
        assert_remote = self.factory.assert_remote

        def hold(card, worktree, *, expected_head=None):
            if card['number'] == 1 and expected_head:
                self.gh.items[0]['labels'].append('sfc-hold')
            return assert_remote(card, worktree, expected_head=expected_head)

        before = len(self.proc.lanes)
        with patch.object(self.factory, 'assert_remote', side_effect=hold):
            self.watch(resume=self.factory.watcher.session['id'])
        self.assertEqual(self.proc.lanes[before:], ['build', 'qa', 'security', 'review'])
        self.assertEqual(self.r['status'], 'QA')
        self.assertTrue(self.factory.state['cards']['2']['handoff'])

    def test_pr_drift_after_checks_is_local_to_the_ticket(self):
        self.missing_checks()
        remote = self.factory.assert_remote
        self.gh.items.append(ticket(2, priority='P3'))

        def read(card, *args, **kwargs):
            if card['number'] == 1 and self.factory.record(card)['blocked_recoveries']:
                raise Halt('fixture PR head changed during checks')
            return remote(card, *args, **kwargs)

        with patch.object(self.factory, 'assert_remote', side_effect=read):
            self.watch()
        r = self.factory.record(self.card)
        self.assertEqual(r['status'], 'Blocked')
        self.assertIn('drift', r['recovery_wait'])
        self.assertTrue(self.factory.state['cards']['2']['handoff'])

    def test_stop_during_pin_read_preserves_recovery_eligibility(self):
        self.missing_checks()
        self.prepare_watch()
        with patch.object(recovery, 'binding', side_effect=Stopped('fixture stop')):
            with self.assertRaises(Stopped):
                recovery.run(self.factory, self.card)
        self.assertFalse(self.r.get('recovery_wait'))
        self.assertFalse(self.r.get('blocked_recoveries'))
        self.assertTrue(recovery.run(self.factory, self.card))

    def test_failed_receipt_does_not_prevent_a_second_spent_action(self):
        from test_verification_reuse import contract
        reusable = 'python3 -c "print(2)"'
        self.c['verify_commands'].append(reusable)
        self.c['verification_reuse'] = [contract(reusable)]
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        self.options['max_recoveries_per_ticket'] = 2
        self.missing_checks(structured=True)
        execute = self.proc.execute
        failed = False

        def fail_first(argv, cwd, log, *args, **kwargs):
            nonlocal failed
            if argv[0] != 'codex' and not failed:
                failed = True
                Path(log).write_text('retained first failure\n')
                return 1
            return execute(argv, cwd, log, *args, **kwargs)

        with patch.object(self.proc, 'execute', side_effect=fail_first):
            self.watch()
        r = self.factory.record(self.card)
        self.assertFalse(r.get('recovery_wait'))
        first, second = r['blocked_recoveries']
        self.assertEqual(first['phase'], 'failed')
        self.assertEqual(Path(first['checks'][0]['log']).read_text(), 'retained first failure\n')
        self.assertEqual(second['phase'], 'finished')
        self.assertEqual(second['checks'][1]['reuse'], self.old['verification']['checks'][1])
        self.assertTrue(r['handoff'])

    def test_next_matching_window_continues_completed_checks_within_same_allowance(self):
        self.missing_checks()
        verify = self.factory.verify

        def hold(*args, **kwargs):
            result = verify(*args, **kwargs)
            self.gh.items[0]['labels'].append('sfc-hold')
            return result

        with patch.object(self.factory, 'verify', side_effect=hold):
            self.watch()
        r = self.factory.record(self.card)
        self.assertEqual(r['status'], 'Blocked')
        self.assertEqual(r['blocked_recoveries'][0]['phase'], 'reserved')
        self.assertTrue(r['blocked_recoveries'][0]['verification']['passed'])
        self.gh.items[0]['labels'].remove('sfc-hold')
        before = sum(e[0] == 'check' for e in self.proc.events)
        self.watch()
        r = self.factory.record(self.card)
        self.assertEqual(sum(e[0] == 'check' for e in self.proc.events), before)
        self.assertEqual(len(r['blocked_recoveries']), 1)
        self.assertTrue(r['handoff'])

    def test_hold_added_during_checks_keeps_other_tickets_moving(self):
        self.missing_checks()
        self.gh.items.append(ticket(2, priority='P3'))
        verify = self.factory.verify
        writes, lanes = len(self.gh.writes), len(self.proc.lanes)

        def hold(card, *args, **kwargs):
            try:
                return verify(card, *args, **kwargs)
            finally:
                if card['number'] == 1:
                    self.gh.items[0]['labels'].append('sfc-hold')

        with patch.object(self.factory, 'verify', side_effect=hold):
            self.watch()
        record = self.factory.record(self.card)
        self.assertEqual(record['status'], 'Blocked')
        self.assertEqual(record['blocked_recoveries'][0]['phase'], 'reserved')
        self.assertTrue(record['blocked_recoveries'][0]['checks'])
        self.assertTrue(all(number == 2 for number, _ in self.gh.writes[writes:]))
        self.assertEqual(self.proc.lanes[lanes:], ['build', 'qa', 'security', 'review'])
        self.assertTrue(self.factory.state['cards']['2']['handoff'])

    def test_dependency_reopened_after_acknowledgement_can_recover_after_closure(self):
        self.gh.items[0]['body'] += '\n## Blocked by\n- #9\n'
        self.gh.extra_issues[9] = ticket(9)
        self.gh.extra_issues[9]['state'] = 'CLOSED'
        self.missing_checks()
        self.options['max_recoveries_per_ticket'] = 2
        self.prepare_watch()
        self.assertTrue(recovery.run(self.factory, self.card))
        before = len(self.proc.lanes)
        self.gh.extra_issues[9]['state'] = 'OPEN'
        self.factory.process(self.card)
        self.assertEqual(self.proc.lanes[before:], [])
        self.assertEqual(self.r['status'], 'Blocked')
        self.assertEqual(self.r['blocker']['kind'], 'dependency')
        self.assertEqual(self.r['blocked_recoveries'][0]['phase'], 'finished')
        self.assertEqual(self.r['blocked_recoveries'][0]['final_stage'], 'Blocked')
        checks = sum(event[0] == 'check' for event in self.proc.events)
        self.gh.extra_issues[9]['state'] = 'CLOSED'
        self.watch(resume=self.factory.watcher.session['id'])
        record = self.factory.record(self.card)
        self.assertEqual([action['phase'] for action in record['blocked_recoveries']], ['finished', 'finished'])
        self.assertEqual(record['blocked_recoveries'][1]['blocker']['kind'], 'dependency')
        self.assertEqual(self.proc.lanes[before:], ['qa', 'security', 'review'])
        self.assertEqual(sum(event[0] == 'check' for event in self.proc.events), checks)
        self.assertTrue(record['handoff'])

    def test_changed_replacement_execution_receipt_prevents_admission(self):
        self.missing_checks()
        self.prepare_watch()
        verify = self.factory.verify

        def change_replacement(*args, **kwargs):
            self.assertTrue(verify(*args, **kwargs))
            path = Path(self.r['verification']['checks'][0]['receipt_path'])
            path.write_text('Replacement evidence changed before admission.\n')
            return True

        writes = list(self.gh.writes)
        with patch.object(self.factory, 'verify', side_effect=change_replacement):
            self.assertFalse(recovery.run(self.factory, self.card))
        self.assertIn('drift', self.r['recovery_wait'])
        self.assertEqual(self.r['status'], 'Blocked')
        self.assertEqual(self.gh.writes, writes)

    def test_invalid_structured_request_never_becomes_command_authority(self):
        for ids in ([0], [99], [True], [{}], ['python3 arbitrary.py'], [1, 'check-1']):
            with self.assertRaises(ValueError):
                recovery.check_request(self.factory, {'verdict': 'blocked', 'findings': [],
                    'recovery': {'kind': 'missing-check-evidence', 'check_ids': ids}})

    def test_requested_check_runs_fresh_while_other_passing_check_is_reused(self):
        from test_verification_reuse import contract
        self.c['verify_commands'] = ['python3 -c "print(1)"', 'python3 -c "print(2)"']
        self.c['verification_reuse'] = [contract(c) for c in self.c['verify_commands']]
        self.missing_checks(structured=True)
        old = copy.deepcopy(self.r['verification'])
        self.watch()
        r = self.factory.record(self.card)
        self.assertNotIn('reuse', r['verification']['checks'][0])
        self.assertEqual(r['verification']['checks'][1]['reuse'], old['checks'][1])
        self.assertEqual(len([e for e in self.proc.events if e[0] == 'check']), 3)

    def test_duplicate_command_only_forces_requested_position(self):
        from test_verification_reuse import contract
        command = 'python3 -c "print(1)"'
        self.c['verify_commands'] = [command, command]
        self.c['verification_reuse'] = [contract(command)]
        self.missing_checks(structured=True)
        checks = sum(e[0] == 'check' for e in self.proc.events)
        self.watch()
        first, second = self.factory.record(self.card)['verification']['checks']
        self.assertNotIn('reuse', first)
        self.assertIn('reuse', second)
        self.assertEqual(sum(e[0] == 'check' for e in self.proc.events), checks + 1)

    def test_restart_before_fresh_checks_cannot_accept_old_passing_evidence(self):
        self.missing_checks(structured=True)
        self.prepare_watch()
        with patch.object(self.factory, 'verify', side_effect=Halt('fixture interrupted before checks')):
            with self.assertRaisesRegex(Halt, 'interrupted before checks'):
                recovery.run(self.factory, self.card)
        before = len(self.proc.events)
        self.assertFalse(recovery.run(self.factory, self.card))
        self.assertEqual(len(self.proc.events), before)
        self.assertEqual(self.r['blocked_recoveries'][0]['phase'], 'failed')
        self.assertEqual(self.r['status'], 'Blocked')

    def interrupt_after_recovery_verification(self):
        self.missing_checks()
        verify = self.factory.verify
        def interrupted(*args, **kwargs):
            self.assertTrue(verify(*args, **kwargs))
            raise Halt('fixture interrupted after completed recovery checks')
        with patch.object(self.factory, 'verify', side_effect=interrupted):
            with self.assertRaisesRegex(Halt, 'interrupted after completed'):
                self.watch()
        saved = read_json(self.factory.state_path)['cards']['1']
        action = saved['blocked_recoveries'][0]
        self.assertEqual(action['phase'], 'reserved')
        self.assertIn('checks_started', action)
        self.assertNotIn('checked_binding', action)
        self.assertNotEqual(saved['verification'], self.old['verification'])
        self.completed = copy.deepcopy(saved['verification'])
        self.session = read_json(common_dir(self.root) / 'sfc-watch.json')['id']
        self.factory = Factory(self.root, self.c, self.gh, self.proc)

    def test_restart_after_completed_checks_resumes_review_without_reexecution(self):
        self.interrupt_after_recovery_verification()
        before = len(self.proc.lanes)
        checks = len([e for e in self.proc.events if e[0] == 'check'])
        with patch.object(self.factory, 'verification_evidence',
                          wraps=self.factory.verification_evidence) as validate:
            self.watch(resume=self.session)
        validate.assert_called()
        r = self.factory.record(self.card)
        self.assertEqual(self.proc.lanes[before:], ['qa', 'security', 'review'])
        self.assertEqual(len([e for e in self.proc.events if e[0] == 'check']), checks)
        self.assertEqual(r['verification'], self.completed)
        self.assertEqual(len(r['blocked_recoveries']), 1)
        self.assertEqual(r['blocked_recoveries'][0]['phase'], 'finished')
        self.assertNotIn('recovery_wait', r)
        for key in ('worktree', 'branch', 'pr', 'head', 'base', 'claim', 'attempts'):
            self.assertEqual(r[key], self.old[key], key)
        self.assertEqual(r['worker_output_budget']['build'], self.old['worker_output_budget']['build'])
        for lane, budget in self.old['worker_output_budget'].items():
            self.assertEqual(r['worker_output_budget'][lane]['dispatches'][:len(budget['dispatches'])],
                             budget['dispatches'])
        self.assertTrue(r['handoff'])
        self.assertEqual(self.gh.merges, [])

    def test_restart_after_completed_checks_rejects_changed_original_receipt(self):
        self.interrupt_after_recovery_verification()
        Path(self.old['verification']['receipt_path']).write_text('changed retained receipt\n')
        checks = len([e for e in self.proc.events if e[0] == 'check'])
        before, writes = len(self.proc.lanes), len(self.gh.writes)
        self.watch(resume=self.session)
        r = self.factory.record(self.card)
        self.assertIn('drift', r['recovery_wait'])
        self.assertEqual(r['status'], 'Blocked')
        self.assertEqual(r['blocked_recoveries'][0]['phase'], 'reserved')
        self.assertEqual(len(self.proc.lanes), before)
        self.assertEqual(len(self.gh.writes), writes)
        self.assertEqual(len([e for e in self.proc.events if e[0] == 'check']), checks)

    def test_restart_after_completed_checks_rejects_late_original_log(self):
        self.interrupt_after_recovery_verification()
        Path(self.old['verification']['checks'][0]['log']).write_text('late original check output\n')
        before, writes = len(self.proc.lanes), len(self.gh.writes)
        self.watch(resume=self.session)
        r = self.factory.record(self.card)
        self.assertIn('drift', r['recovery_wait'])
        self.assertEqual(r['status'], 'Blocked')
        self.assertEqual(r['blocked_recoveries'][0]['phase'], 'reserved')
        self.assertEqual(len(self.proc.lanes), before)
        self.assertEqual(len(self.gh.writes), writes)

    def assert_checks_reject_original_evidence_drift(self, path):
        execute = self.proc.execute

        def change_original(argv, *args, **kwargs):
            code = execute(argv, *args, **kwargs)
            if argv[0] != 'codex':
                path.write_text('Original evidence changed during recovery.\n')
            return code

        writes, lanes = list(self.gh.writes), list(self.proc.lanes)
        checks = len([e for e in self.proc.events if e[0] == 'check'])
        with patch.object(self.proc, 'execute', side_effect=change_original):
            self.watch()
        r = self.factory.record(self.card)
        self.assertTrue(r['verification']['passed'])
        self.assertIn('drift', r['recovery_wait'])
        self.assertEqual(r['status'], 'Blocked')
        self.assertEqual([a['phase'] for a in r['blocked_recoveries']], ['reserved'])
        self.assertEqual(self.gh.writes, writes)
        self.assertEqual(self.proc.lanes, lanes)
        self.assertEqual(len([e for e in self.proc.events if e[0] == 'check']), checks + 1)

    def test_uninterrupted_checks_reject_changed_original_receipt(self):
        self.missing_checks()
        self.assert_checks_reject_original_evidence_drift(Path(self.old['verification']['receipt_path']))

    def test_uninterrupted_checks_reject_changed_original_execution_receipt(self):
        self.missing_checks()
        path = Path(self.old['verification']['checks'][0]['receipt_path'])
        self.assert_checks_reject_original_evidence_drift(path)

    def test_restart_after_checks_rejects_changed_original_execution_receipt(self):
        self.interrupt_after_recovery_verification()
        Path(self.old['verification']['checks'][0]['receipt_path']).write_text('changed execution receipt\n')
        before = list(self.gh.writes)
        self.watch(resume=self.session)
        self.assertIn('drift', self.factory.record(self.card)['recovery_wait'])
        self.assertEqual(self.gh.writes, before)

    def test_uninterrupted_checks_reject_late_original_log(self):
        self.missing_checks()
        self.assert_checks_reject_original_evidence_drift(Path(self.old['verification']['checks'][0]['log']))

    def test_move_readback_rejects_original_evidence_drift(self):
        self.missing_checks()
        original = self.gh.move

        def drift(card, stage):
            original(card, stage)
            if stage == 'QA':
                Path(self.old['verification']['checks'][0]['log']).write_text('Late original log.\n')

        moves, lanes = self.gh.writes.count((1, 'QA')), list(self.proc.lanes)
        with patch.object(self.gh, 'move', side_effect=drift), self.assertRaisesRegex(Halt, 'drift'):
            self.watch()
        self.assertEqual(self.gh.writes.count((1, 'QA')), moves + 1)
        self.assertEqual(self.proc.lanes, lanes)
        saved = read_json(self.factory.state_path)['cards']['1']
        self.assertEqual([a['phase'] for a in saved['blocked_recoveries']], ['move-intent'])

    def test_move_intent_restart_revalidates_the_original_pins(self):
        self.missing_checks()
        original = self.gh.move

        def lost(card, stage):
            original(card, stage)
            if stage == 'QA':
                raise Halt('fixture lost recovery response')

        with patch.object(self.gh, 'move', side_effect=lost), self.assertRaisesRegex(Halt, 'lost recovery'):
            self.watch()
        session = read_json(common_dir(self.root) / 'sfc-watch.json')
        before, lanes, events = list(self.gh.writes), list(self.proc.lanes), list(self.proc.events)
        Path(self.old['verification']['checks'][0]['log']).write_text('Late original log.\n')
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        with self.assertRaisesRegex(Halt, 'source/evidence drift|pins changed'):
            self.watch(resume=session['id'])
        self.assertEqual(self.gh.writes, before)
        self.assertEqual(self.proc.lanes, lanes)
        self.assertEqual(self.proc.events, events)
        self.assertEqual(self.factory.record(self.card)['blocked_recoveries'][0]['phase'], 'move-intent')

    def test_scope_drift_and_exhausted_window_do_not_recover(self):
        self.missing_checks()
        self.prepare_watch()
        self.card['_pickup_scope']['priority']['field_id'] = 'different-field'
        self.assertFalse(recovery.eligibility(self.factory, self.card)[0])
        self.card = pickup.cards(self.gh)[0]
        self.factory.processes.stop_check = lambda: True
        with self.assertRaises(Halt):
            recovery.run(self.factory, self.card)
        self.assertFalse(self.r.get('blocked_recoveries'))

    def test_acknowledged_recovery_cannot_dispatch_an_edited_task_on_resume(self):
        self.missing_checks()
        self.prepare_watch()
        self.assertTrue(recovery.run(self.factory, self.card))
        session = self.factory.watcher.session['id']
        self.gh.items[0]['body'] += '\nChanged after recovery acknowledgement.\n'
        before = (list(self.proc.lanes), list(self.gh.writes))
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        self.watch(resume=session)
        self.assertEqual((self.proc.lanes, self.gh.writes), before)
        self.assertEqual(self.factory.record(self.card)['blocked_recoveries'][-1]['phase'], 'admitted')
        self.assertIn('Recovered task, source or scope changed', str(self.factory.state['waiting']))

    def test_base_change_after_acknowledgement_cannot_rebase_or_publish(self):
        self.missing_checks()
        self.prepare_watch()
        self.assertTrue(recovery.run(self.factory, self.card))
        worktree = Path(self.r['worktree'])
        head = self.r['head']
        before = list(self.proc.lanes)
        with (patch.object(self.factory, 'fetch', side_effect=[self.r['base'], 'a' * 40]),
              patch.object(self.factory, 'publish') as publish):
            self.factory.process(self.card)
        publish.assert_not_called()
        self.assertEqual(self.proc.lanes, before)
        self.assertEqual(self.r['head'], head)
        self.assertEqual(recovery.git(worktree, 'rev-parse', 'HEAD'), head)
        self.assertIn('drift', self.r['recovery_wait'])
        self.assertEqual(self.r['blocked_recoveries'][-1]['phase'], 'admitted')

    def test_task_edited_during_independent_review_cannot_update_pr(self):
        self.missing_checks()

        def edit(lane, _):
            if lane == 'review':
                self.gh.items[0]['body'] += '\nChanged during the independent review.\n'

        self.proc.on_lane = edit
        with (patch.object(self.gh, 'update_pr_body') as update,
              patch.object(self.gh, 'ready') as ready):
            self.watch()
        update.assert_not_called()
        ready.assert_not_called()
        self.assertIn('drift', self.factory.record(self.card)['recovery_wait'])
        self.assertEqual(self.gh.merges, [])

    def test_lost_move_ack_readback_on_same_session_resume(self):
        self.missing_checks()
        original = self.gh.move
        def lost(card, stage):
            original(card, stage)
            if stage == 'QA': raise Halt('fixture lost recovery response')
        with patch.object(self.gh, 'move', side_effect=lost), self.assertRaisesRegex(Halt, 'lost recovery'):
            self.watch()
        session = read_json(common_dir(self.root) / 'sfc-watch.json')
        checks = len([e for e in self.proc.events if e[0] == 'check'])
        moves = self.gh.writes.count((1, 'QA'))
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        self.watch(resume=session['id'])
        r = self.factory.record(self.card)
        self.assertEqual(self.gh.writes.count((1, 'QA')), moves)
        self.assertEqual(len([e for e in self.proc.events if e[0] == 'check']), checks)
        self.assertEqual(len(r['blocked_recoveries']), 1)
        self.assertEqual(r['blocked_recoveries'][0]['phase'], 'finished')

    def test_lost_move_readback_reconciles_even_when_held(self):
        self.missing_checks()
        move = self.gh.move

        def lost(card, stage):
            move(card, stage)
            if stage == 'QA':
                raise Halt('fixture lost recovery response')

        with patch.object(self.gh, 'move', side_effect=lost), self.assertRaisesRegex(Halt, 'lost recovery'):
            self.watch()
        session = read_json(common_dir(self.root) / 'sfc-watch.json')
        self.gh.items[0]['labels'].append('sfc-hold')
        self.gh.items.append(ticket(2, priority='P3'))
        before = len(self.gh.writes)
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        self.watch(resume=session['id'])
        r = self.factory.record(self.card)
        self.assertEqual(r['status'], 'QA')
        self.assertEqual(r['blocked_recoveries'][0]['phase'], 'admitted')
        self.assertTrue(all(number == 2 for number, _ in self.gh.writes[before:]))
        self.assertTrue(self.factory.state['cards']['2']['handoff'])

    def test_missing_card_cannot_drop_a_pending_recovery_write(self):
        self.missing_checks()
        self.prepare_watch()

        with patch.object(self.gh, 'move', side_effect=Halt('fixture unknown move response')):
            with self.assertRaisesRegex(Halt, 'unknown move response'):
                recovery.run(self.factory, self.card)
        action = self.r['blocked_recoveries'][0]
        self.assertEqual(action['phase'], 'move-intent')
        self.gh.items = [ticket(2)]
        before = list(self.gh.writes)
        with self.assertRaisesRegex(Halt, 'Recovery admission card missing'):
            self.watch(resume=self.factory.watcher.session['id'])
        self.assertEqual(self.gh.writes, before)
        self.assertEqual(self.factory.record(self.card)['blocked_recoveries'][0]['phase'], 'move-intent')

    def interrupt_recovery_check(self, interruption, *, cap=2):
        from test_verification_reuse import contract
        reusable = 'python3 -c "print(2)"'
        self.c['verify_commands'].append(reusable)
        self.c['verification_reuse'] = [contract(reusable)]
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        self.options['max_recoveries_per_ticket'] = cap
        self.missing_checks(structured=True)
        self.prepare_watch()

        def interrupt(argv, cwd, log, *args, **kwargs):
            self.assertNotEqual(argv[0], 'codex')
            self.assertEqual(self.r['verification'], {})
            Path(log).write_text('retained interrupted check\n')
            if interruption is Stopped:
                self.proc.stop_check = lambda: True
            raise interruption('fixture interrupted recovery check')

        with patch.object(self.proc, 'execute', side_effect=interrupt):
            if interruption is Stopped:
                try:
                    with self.assertRaisesRegex(Stopped, 'fixture interrupted'):
                        recovery.run(self.factory, self.card)
                finally:
                    self.proc.stop_check = None
            else:
                self.assertFalse(recovery.run(self.factory, self.card))
        action = self.r['blocked_recoveries'][0]
        receipt, = (self.factory.run_dir / 'issue-1').glob('verify-*/interruption.json')
        log = Path(read_json(receipt)['log'])
        self.assertEqual(log.read_text(), 'retained interrupted check\n')
        return action, log

    def assert_interrupted_check_retries_within_cap(self, interruption):
        first, log = self.interrupt_recovery_check(interruption)
        if first['phase'] == 'reserved':
            self.assertFalse(recovery.run(self.factory, self.card))
        self.assertEqual(first['phase'], 'failed')
        self.assertEqual(self.r['verification'], {})
        self.assertTrue(recovery.run(self.factory, self.card))
        self.assertEqual([a['phase'] for a in self.r['blocked_recoveries']], ['failed', 'admitted'])
        self.assertEqual(log.read_text(), 'retained interrupted check\n')
        self.assertEqual(self.r['verification']['checks'][1]['reuse'], self.old['verification']['checks'][1])
        for key in ('head', 'base', 'claim', 'attempts', 'worker_output_budget'):
            self.assertEqual(self.r[key], self.old[key], key)

    def test_timed_out_recovery_check_can_spend_remaining_allowance(self):
        self.assert_interrupted_check_retries_within_cap(TimedOut)

    def test_stopped_recovery_check_can_spend_remaining_allowance(self):
        self.assert_interrupted_check_retries_within_cap(Stopped)

    def test_timed_out_recovery_does_not_renew_exhausted_allowance(self):
        self.interrupt_recovery_check(TimedOut, cap=1)
        before = (list(self.proc.events), list(self.gh.writes))
        self.assertFalse(recovery.run(self.factory, self.card))
        self.assertEqual([a['phase'] for a in self.r['blocked_recoveries']], ['failed'])
        self.assertEqual((self.proc.events, self.gh.writes), before)
        self.assertIn('allowance exhausted', recovery.eligibility(self.factory, self.card)[1])

    def test_timed_out_recovery_cannot_retry_with_changed_original_evidence(self):
        self.interrupt_recovery_check(TimedOut)
        Path(self.old['verification']['checks'][0]['log']).write_text('changed original check evidence\n')
        before = (list(self.proc.events), list(self.gh.writes))
        self.assertFalse(recovery.run(self.factory, self.card))
        self.assertEqual([a['phase'] for a in self.r['blocked_recoveries']], ['failed'])
        self.assertEqual((self.proc.events, self.gh.writes), before)
        self.assertIn('drift', self.r['recovery_wait'])

    def test_timed_out_recovery_cannot_reactivate_old_verification(self):
        self.interrupt_recovery_check(TimedOut)
        self.factory.update(self.card, verification=self.old['verification'])
        before = (list(self.proc.events), list(self.gh.writes))
        self.assertFalse(recovery.run(self.factory, self.card))
        self.assertEqual([a['phase'] for a in self.r['blocked_recoveries']], ['failed'])
        self.assertEqual((self.proc.events, self.gh.writes), before)
        self.assertIn('drift', self.r['recovery_wait'])

    def assert_removed_project_item_is_issue_local(self, removal_read):
        self.missing_checks()
        self.gh.items.append(ticket(2, priority='P3'))
        graphql = self.gh.graphql
        reads = 0

        def read(query, values):
            nonlocal reads
            if values.get('item') == self.card['item_id'] and 'number' not in values:
                reads += 1
                if reads == removal_read:
                    self.gh.items = [item for item in self.gh.items if item['number'] != 1]
                    return {'node': None}
            return graphql(query, values)

        before_checks = sum(event[0] == 'check' for event in self.proc.events)
        before_writes = len(self.gh.writes)
        with patch.object(self.gh, 'graphql', side_effect=read):
            self.watch()
        record = self.factory.record(self.card)
        self.assertIn('missing', record['recovery_wait'].lower())
        self.assertFalse(record.get('blocked_recoveries'))
        self.assertEqual(sum(event[0] == 'check' for event in self.proc.events), before_checks + 1)
        self.assertTrue(all(number == 2 for number, _ in self.gh.writes[before_writes:]))
        self.assertTrue(self.factory.state['cards']['2']['handoff'])

    def test_project_item_removed_during_admission_preflight_is_issue_local(self):
        self.assert_removed_project_item_is_issue_local(removal_read=1)

    def test_project_item_removed_during_recovery_preflight_is_issue_local(self):
        self.assert_removed_project_item_is_issue_local(removal_read=2)

    def test_simultaneous_polls_reserve_once(self):
        self.missing_checks()
        self.prepare_watch()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: recovery.run(self.factory, copy.deepcopy(self.card)), range(2)))
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(len(self.r['blocked_recoveries']), 1)
        self.assertEqual(self.gh.writes.count((1, 'QA')), 2)
        self.assertEqual(len([e for e in self.proc.events if e[0] == 'check']), 2)

    def test_waiting_prerequisite_frees_slot_then_closed_condition_recovers(self):
        self.missing_checks()
        self.gh.items[0]['body'] += '\n## Blocked by\n- #9\n'
        self.gh.extra_issues[9] = ticket(9)
        self.card = pickup.cards(self.gh)[0]
        recovery.mark(self.factory, self.card, 'dependency', 'QA')
        self.gh.items.append(ticket(2, priority='P0'))
        before = len(self.proc.lanes)
        self.watch()
        self.assertFalse(self.factory.record(self.card).get('blocked_recoveries'))
        self.assertEqual(self.proc.lanes[before:], ['build', 'qa', 'security', 'review'])
        self.gh.extra_issues[9]['state'] = 'CLOSED'
        self.watch()
        r = self.factory.record(self.card)
        self.assertEqual(len(r['blocked_recoveries']), 1)
        self.assertEqual(r['blocked_recoveries'][0]['blocker']['kind'], 'dependency')

    def test_transient_read_resumes_saved_review(self):
        original = self.factory.review
        once = []
        def read(card, wt):
            if not once:
                from sfc_lib.rate_limit import ReadConnectionReset
                once.append(True)
                raise ReadConnectionReset('fixture exhausted safe read retries')
            return original(card, wt)
        with patch.object(self.factory, 'review', side_effect=read):
            self.watch()
        r = self.factory.state['cards']['1']
        self.assertEqual(r['blocked_recoveries'][0]['blocker']['kind'], 'transient-read')
        self.assertEqual(r['blocked_recoveries'][0]['resumed_stage'], 'Review')
        self.assertEqual(self.proc.lanes, ['build', 'qa', 'security', 'review'])
        self.assertEqual(r['attempts'], 0)

    def test_initial_block_read_reset_respects_late_operator_changes(self):
        review = self.factory.review
        for operation in ('hold', 'move'):
            with self.subTest(operation=operation):
                if operation == 'move':
                    self.tearDown()
                    self.setUp()
                    review = self.factory.review
                self.options['max_polls'] = 1
                injected = []

                def interrupted(card, wt):
                    if injected:
                        return review(card, wt)
                    injected.append(True)
                    if operation == 'hold':
                        self.gh.items[0]['labels'].append('sfc-hold')
                    else:
                        self.gh.move(dict(card), 'QA')
                    raise ReadConnectionReset('fixture read failed after operator changed the issue')

                with patch.object(self.factory, 'review', side_effect=interrupted):
                    self.watch()
                self.assertTrue(injected)
                self.assertNotIn((1, 'Blocked'), self.gh.writes)

    def test_lost_initial_block_response_recovers_without_reissuing_stage_write(self):
        move = self.gh.move

        def fail_read(card, wt):
            raise ReadConnectionReset('fixture original Review lookup failed')

        def lose_ack(card, stage):
            move(card, stage)
            if stage == 'Blocked':
                raise Halt('fixture lost initial Blocked acknowledgement')

        with (patch.object(self.factory, 'review', side_effect=fail_read),
              patch.object(self.gh, 'move', side_effect=lose_ack),
              self.assertRaisesRegex(Halt, 'lost initial Blocked')):
            self.watch()
        self.assertEqual(self.factory.state['cards']['1']['status'], 'Review')
        session = read_json(common_dir(self.root) / 'sfc-watch.json')
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        writes_before_resume = len(self.gh.writes)
        self.watch(resume=session['id'])
        record = self.factory.state['cards']['1']
        self.assertEqual(self.gh.writes[writes_before_resume:], [(1, 'Review'), (1, 'Blocked')])
        self.assertEqual(record['initial_block_transition']['phase'], 'verified')
        self.assertEqual(record['blocked_recoveries'][-1]['blocker']['kind'], 'transient-read')
        self.assertTrue(record['handoff'])

    def test_structured_blocker_persists_before_issue_comment_acknowledgement(self):
        original = self.gh.comment

        def lose_ack(number, body):
            original(number, body)
            if 'SFC Blocked:' in body.read_text():
                raise Halt('fixture lost Blocked note acknowledgement')

        with (patch.object(self.gh, 'comment', side_effect=lose_ack),
              self.assertRaisesRegex(Halt, 'lost Blocked note')):
            self.missing_checks(structured=True)
        self.assertEqual(self.factory.state['cards']['1']['blocker']['kind'], 'missing-check-evidence')

    def test_hold_after_recovered_build_acceptance_resumes_without_building_again(self):
        def build_symlink(worktree):
            worktree.joinpath('value-link').symlink_to('value.txt')

        self.hold_after_accepting_build(before_hold=build_symlink)
        self.assertEqual(self.proc.lanes, ['build', 'qa', 'security', 'review'])
        self.assertEqual(recovery.git(self.factory.record(self.card)['worktree'], 'show', 'HEAD:value-link'),
                         'value.txt')
        self.assertTrue(self.factory.state['cards']['1']['handoff'])

    def test_drift_in_accepted_edit_hidden_by_textconv_cannot_publish(self):
        recovery.git(self.root, 'config', 'diff.masked.textconv', 'cut -d " " -f 1')
        attributes = self.root / '.gitattributes'
        attributes.write_text('value.txt diff=masked\n')
        recovery.git(self.root, 'add', attributes)
        recovery.git(self.root, 'commit', '-m', 'configure human-readable diff')
        recovery.git(self.root, 'push', 'origin', 'main')

        def edit(worktree):
            worktree.joinpath('value.txt').write_text('new drifted\n')

        self.hold_after_accepting_build(after_hold=edit, accepted_value='new accepted\n')
        self.assertEqual(self.proc.lanes, ['build'])
        self.assertFalse(self.gh.prs)
        self.assertIn('saved edits changed', self.factory.state['cards']['1']['recovery_wait'])

    def hold_after_accepting_build(self, *, before_hold=None, after_hold=None, accepted_value=None):
        self.options['max_polls'] = 10
        lane = self.factory._lane
        interrupted, held, released = [], [], []

        def build(card, name, wt, base, **kwargs):
            if name == 'build' and not interrupted:
                interrupted.append(True)
                raise ReadConnectionReset('fixture read failed before the original Build')
            result = lane(card, name, wt, base, **kwargs)
            if name == 'build' and not held:
                if accepted_value:
                    wt.joinpath('value.txt').write_text(accepted_value)
                if before_hold:
                    before_hold(wt)
                held.append(wt)
                self.gh.items[0]['labels'].append('sfc-hold')
            return result

        def release_hold():
            if held and not released:
                self.assertFalse(self.gh.prs)
                if after_hold:
                    after_hold(held[0])
                released.append(True)
                self.gh.items[0]['labels'].remove('sfc-hold')

        self.clock.on_wait = release_hold
        with patch.object(self.factory, '_lane', side_effect=build):
            self.watch()
        self.assertTrue(released)
        self.card = pickup.cards(self.gh)[0]

    def test_transient_read_before_initial_build_resumes_the_original_worktree(self):
        lane = self.factory._lane
        paused = []

        def interrupted(card, name, wt, base, **kwargs):
            if name == 'build' and not paused:
                paused.append((str(wt), base))
                raise ReadConnectionReset('fixture exhausted safe read retries before Build')
            return lane(card, name, wt, base, **kwargs)

        with patch.object(self.factory, '_lane', side_effect=interrupted):
            self.watch()
        record = self.factory.state['cards']['1']
        action = record['blocked_recoveries'][0]
        self.assertEqual(action['resumed_stage'], 'Building')
        self.assertEqual(action['phase'], 'finished')
        self.assertEqual((record['worktree'], record['base']), paused[0])
        self.assertEqual(action['checked_binding']['head'], paused[0][1])
        self.assertEqual(self.proc.lanes, ['build', 'qa', 'security', 'review'])
        self.assertEqual(record['attempts'], 0)
        self.assertTrue(record['handoff'])

    def test_legacy_saved_policy_grants_no_recovery(self):
        self.missing_checks()
        self.prepare_watch()
        self.factory.watcher.settings.pop('max_recoveries_per_ticket')
        self.assertFalse(recovery.eligibility(self.factory, self.card)[0])
        self.assertFalse(self.r.get('blocked_recoveries'))

    def test_preview_does_not_need_live_session_or_spend_recovery(self):
        self.missing_checks()
        before = self.factory.state_path.read_bytes()
        writes = len(self.gh.writes)
        result = self.factory.watch(dry_run=True, overrides=self.options)
        self.assertEqual(result['selected'], [1])
        self.assertEqual(self.factory.state_path.read_bytes(), before)
        self.assertEqual(len(self.gh.writes), writes)


    def assert_retained_log_drift_blocks_final_merge(self, gate_prefix):
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        self.missing_checks(structured=True)
        original = Path(self.old['verification']['checks'][0]['log'])
        verify = self.factory.verify

        def drift(card, worktree, *args, **kwargs):
            result = verify(card, worktree, *args, **kwargs)
            if worktree.name.startswith(gate_prefix):
                original.write_text('pinned recovery log changed during final checks\n')
            return result

        with patch.object(self.factory, 'verify', side_effect=drift):
            self.watch(allow_merge=True)
        self.assertIn('drift', self.factory.record(self.card).get('recovery_wait', ''))
        self.assertTrue(original.is_file())

    def test_rechecks_retained_evidence_at_github_merge_boundary(self):
        self.c['merge_policy'] = 'auto'
        self.assert_retained_log_drift_blocks_final_merge('gate-')
        self.assertEqual(self.gh.merges, [])

    def test_rechecks_retained_evidence_at_atomic_merge_boundary(self):
        from test_local_merge import PriorityAtomicGitHub
        self.c.update(merge_policy='auto', merge_mode='local-verification')
        self.gh = PriorityAtomicGitHub(self.root, self.remote)
        self.assert_retained_log_drift_blocks_final_merge('local-gate-')
        self.assertEqual(self.gh.attempts, [])

    def test_protected_merge_read_gap_keeps_recovery_pins_and_reads_back_once(self):
        self.c['merge_policy'] = 'auto'
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        self.missing_checks(structured=True)
        pr = self.gh.pr

        def read(number):
            if self.gh.merges:
                raise ReadConnectionReset('fixture exhausted merged PR read')
            return pr(number)

        with patch.object(self.gh, 'pr', side_effect=read), self.assertRaises(Halt):
            self.watch(allow_merge=True)
        session = read_json(common_dir(self.root) / 'sfc-watch.json')
        record = self.factory.record(self.card)
        self.assertEqual(self.gh.items[0]['status'], 'Review')
        self.assertEqual(self.gh.writes.count((1, 'Blocked')), 1)
        self.assertEqual(record['blocked_recoveries'][-1]['phase'], 'admitted')
        self.assertEqual(record['external_completion']['head'], self.old['head'])
        self.assertEqual(record['external_completion']['base'], self.old['base'])
        self.assertEqual(record['external_completion']['pr'], self.old['pr'])
        self.assertNotEqual(record['activity']['state'], 'Failed')
        lanes = self.proc.lanes.copy()

        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        self.watch(resume=session['id'], allow_merge=True)
        record = self.factory.record(self.card)
        self.assertEqual(record['status'], 'Done')
        self.assertEqual(record['activity']['state'], 'Completed')
        self.assertEqual(record['blocked_recoveries'][-1]['phase'], 'finished')
        self.assertEqual(self.gh.merges, [(self.old['pr'], self.old['head'])])
        self.assertEqual(self.gh.writes.count((1, 'Done')), 1)
        self.assertEqual(self.proc.lanes, lanes)

    def assert_final_gate_base_drift_does_not_release_recovery_pins(self, gate_prefix):
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        self.missing_checks(structured=True)
        verify = self.factory.verify

        def drift(card, worktree, base, **kwargs):
            result = verify(card, worktree, base, **kwargs)
            if worktree.name.startswith(gate_prefix):
                tree = recovery.git(self.root, 'rev-parse', base + '^{tree}')
                advanced = recovery.git(self.root, 'commit-tree', tree, '-p', base, '-m', 'Move fixture base')
                recovery.git(self.root, 'push', 'origin', advanced + ':refs/heads/main')
            return result

        with patch.object(self.factory, 'verify', side_effect=drift):
            self.watch(allow_merge=True)
        record = self.factory.record(self.card)
        self.assertEqual(self.gh.items[0]['status'], 'Review')
        self.assertEqual(record['status'], 'Review')
        self.assertEqual(record['blocked_recoveries'][-1]['phase'], 'admitted')
        self.assertIn('drift', record['recovery_wait'])
        self.assertEqual((record['head'], record['base']), (self.old['head'], self.old['base']))
        self.assertEqual(self.proc.lanes, ['build', 'qa', 'security', 'qa', 'security', 'review'])

    def test_protected_gate_base_drift_preserves_admitted_recovery(self):
        self.c['merge_policy'] = 'auto'
        self.assert_final_gate_base_drift_does_not_release_recovery_pins('gate-')
        self.assertEqual(self.gh.merges, [])

    def test_protected_merge_lost_acknowledgement_verifies_landed_squash(self):
        self.c['merge_policy'] = 'auto'
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        self.missing_checks(structured=True)
        merge = self.gh.merge

        def lost(number, head):
            on_disk = read_json(self.factory.state_path)['cards']['1']['external_completion']
            self.assertEqual(on_disk['merge_response'], 'pending')
            self.assertEqual((on_disk['pr'], on_disk['head'], on_disk['base']), (number, head, self.old['base']))
            merge(number, head)
            raise Halt('fixture lost merge acknowledgment')

        with patch.object(self.gh, 'merge', side_effect=lost) as dispatch:
            self.watch(allow_merge=True)
        self.assertEqual(dispatch.call_count, 1)
        record = self.factory.record(self.card)
        self.assertEqual(record['external_completion']['merge_response'], 'ambiguous')
        self.assertEqual(record['external_completion']['phase'], 'done')
        self.assertEqual(record['activity']['state'], 'Completed')
        self.assertEqual(self.gh.writes.count((1, 'Done')), 1)

    def test_protected_merge_readback_rejects_squash_on_changed_base(self):
        self.c['merge_policy'] = 'auto'
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        self.missing_checks(structured=True)
        merge = self.gh.merge

        def moved_base(number, head):
            base = self.old['base']
            tree = recovery.git(self.root, 'rev-parse', base + '^{tree}')
            advanced = recovery.git(self.root, 'commit-tree', tree, '-p', base, '-m', 'Move fixture base')
            recovery.git(self.root, 'push', 'origin', advanced + ':refs/heads/main')
            merge(number, head)

        with patch.object(self.gh, 'merge', side_effect=moved_base), self.assertRaises(Halt):
            self.watch(allow_merge=True)
        self.assertEqual(self.gh.items[0]['status'], 'Review')
        self.assertNotIn((1, 'Done'), self.gh.writes)
        record = self.factory.record(self.card)
        self.assertEqual(record['blocked_recoveries'][-1]['phase'], 'admitted')
        self.assertIn('shape/tree', record['external_completion_error'])
        self.assertNotEqual(record['activity']['state'], 'Failed')

    def test_atomic_gate_base_drift_preserves_admitted_recovery(self):
        from test_local_merge import PriorityAtomicGitHub
        self.c.update(merge_policy='auto', merge_mode='local-verification')
        self.gh = PriorityAtomicGitHub(self.root, self.remote)
        self.assert_final_gate_base_drift_does_not_release_recovery_pins('local-gate-')
        self.assertEqual(self.gh.attempts, [])

    def test_lost_recovered_review_stage_ack_reads_back_without_repeating_move(self):
        self.missing_checks()
        move = self.gh.move

        def uncertain(card, stage):
            move(card, stage)
            if stage == 'Review':
                raise Halt('fixture lost review stage response')

        with patch.object(self.gh, 'move', side_effect=uncertain), self.assertRaises(Halt):
            self.watch()
        session = read_json(common_dir(self.root) / 'sfc-watch.json')
        r = self.factory.record(self.card)
        self.assertNotEqual(r.get('activity', {}).get('state'), 'Failed')
        lanes = self.proc.lanes.copy()
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        self.watch(resume=session['id'])
        self.assertEqual(self.gh.writes.count((1, 'Review')), 1)
        self.assertEqual(self.proc.lanes, lanes + ['review'])
        self.assertTrue(self.factory.record(self.card).get('handoff'))

    def test_admitted_pr_state_read_reset_yields_to_other_tickets(self):
        self.missing_checks()
        self.prepare_watch()
        self.assertTrue(recovery.run(self.factory, self.card))
        self.gh.items.append(ticket(2, priority='P3'))
        session = self.factory.watcher.session['id']
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        original = self.gh.pr_state
        failures = []

        def read(number):
            if number == self.old['pr'] and not self.factory.state['cards'].get('2', {}).get('handoff'):
                failures.append(number)
                raise ReadConnectionReset('fixture recovered PR read reset')
            return original(number)

        with patch.object(self.gh, 'pr_state', side_effect=read):
            self.watch(resume=session)
        self.assertTrue(failures)
        self.assertTrue(self.factory.state['cards']['2']['handoff'])
        self.assertTrue(self.factory.record(self.card)['handoff'])

    def test_admitted_pr_detail_read_reset_defers_without_another_blocked_move(self):
        self.c['max_workers'] = 1
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        self.missing_checks()
        self.gh.items.append(ticket(2, priority='P3'))
        original = self.gh.pr
        failures = []

        def read(number):
            if (number == self.old['pr'] and self.factory.record(self.card)['status'] == 'Review' and
                    not self.factory.state['cards'].get('2', {}).get('handoff')):
                failures.append(number)
                raise ReadConnectionReset('fixture exhausted PR details read')
            return original(number)

        with (patch.object(self.gh, 'pr_state', side_effect=lambda number: original(number)['state']),
              patch.object(self.gh, 'pr', side_effect=read)):
            self.watch()
        record = self.factory.record(self.card)
        self.assertTrue(failures)
        self.assertTrue(self.factory.state['cards']['2']['handoff'])
        self.assertTrue(record['handoff'])
        self.assertEqual(self.gh.writes.count((1, 'Blocked')), 2)  # Initial gap and final human handoff.
        self.assertEqual(len(record['blocked_recoveries']), 1)

    def test_hold_during_pr_ready_keeps_recovered_ticket_in_review(self):
        self.missing_checks()
        ready = self.gh.ready

        def hold(number):
            ready(number)
            self.gh.items[0]['labels'].append('sfc-hold')

        before = len(self.gh.writes)
        with patch.object(self.gh, 'ready', side_effect=hold):
            self.watch()
        r = self.factory.record(self.card)
        self.assertEqual(r['status'], 'Review')
        self.assertNotIn((1, 'Blocked'), self.gh.writes[before:])
        self.assertFalse(r.get('handoff'))

    def test_unknown_prerequisite_state_never_publishes_a_dependency_blocker(self):
        self.gh.items[0]['body'] += '\n## Blocked by\n- #9\n'
        self.gh.extra_issues[9] = {**ticket(9), 'state': 'CLOSED'}
        self.missing_checks()
        self.prepare_watch()
        self.gh.move(self.card, 'QA')
        self.r['status'] = 'QA'

        def lose_read(query, variables):
            if variables.get('number') == 9:
                raise ReadConnectionReset('fixture unavailable dependency state')

        before = list(self.gh.writes)
        self.gh.on_read = lose_read
        self.factory.process(self.card)
        self.assertEqual(self.gh.writes, before)
        self.assertEqual(self.r['status'], 'QA')
        self.assertNotEqual(self.r.get('blocker', {}).get('kind'), 'dependency')

    def test_recovery_report_includes_the_strict_branch_protection_blocker(self):
        self.c['merge_policy'] = 'auto'
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        self.missing_checks()
        self.gh.strict = False
        self.watch(allow_merge=True)
        report = delivery.report(self.root)
        blocker = report['blocked_recovery']['1']['blocker']
        self.assertEqual(blocker.get('reason', {}).get('code'), 'strict_protection')
        self.assertEqual(self.gh.merges, [])

    def assert_recovered_pr_update_reads_back_once(self, method, field):
        self.missing_checks()
        write, read = getattr(self.gh, method), self.gh.pr
        landed = False

        def lost(*args, **kwargs):
            nonlocal landed
            write(*args, **kwargs)
            landed = True
            raise Halt('fixture PR update response lost')

        def unavailable(number):
            if landed:
                raise ReadConnectionReset('fixture PR update readback reset')
            return read(number)

        with patch.object(self.gh, method, side_effect=lost) as writes:
            with patch.object(self.gh, 'pr', side_effect=unavailable), self.assertRaises(Halt):
                self.watch()
            record = self.factory.record(self.card)
            intent = record['blocked_recoveries'][-1]['pr_updates'][-1]
            self.assertEqual((intent['phase'], intent['field']), ('intent', field))
            self.assertEqual(self.gh.items[0]['status'], 'Review')
            self.assertNotEqual(record['activity']['state'], 'Failed')
            lanes = self.proc.lanes.copy()
            session = read_json(common_dir(self.root) / 'sfc-watch.json')
            self.factory = Factory(self.root, self.c, self.gh, self.proc)
            self.watch(resume=session['id'])
        writes.assert_called_once()
        self.assertEqual(self.proc.lanes, lanes)
        record = self.factory.record(self.card)
        self.assertEqual(record['handoff'], self.old['head'])
        intents = record['blocked_recoveries'][-1]['pr_updates']
        self.assertEqual([item['phase'] for item in intents if item['field'] == field], ['verified'])

    def test_recovered_pr_body_update_reads_back_once(self):
        self.assert_recovered_pr_update_reads_back_once('update_pr_body', 'body')

    def test_recovered_pr_ready_update_reads_back_once(self):
        self.assert_recovered_pr_update_reads_back_once('ready', 'isDraft')


    def test_intact_recovery_evidence_allows_checked_github_merge(self):
        self.c['merge_policy'] = 'auto'
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        self.missing_checks(structured=True)
        self.watch(allow_merge=True)
        self.assertEqual(self.gh.merges, [(self.old['pr'], self.old['head'])])
        self.assertEqual(self.factory.record(self.card)['status'], 'Done')

    def test_intact_recovery_evidence_allows_checked_atomic_merge(self):
        from test_local_merge import PriorityAtomicGitHub
        self.c.update(merge_policy='auto', merge_mode='local-verification')
        self.gh = PriorityAtomicGitHub(self.root, self.remote)
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        self.missing_checks(structured=True)
        self.watch(allow_merge=True)
        self.assertEqual(len(self.gh.attempts), 1)
        self.assertEqual(self.factory.record(self.card)['status'], 'Done')

    def test_unconfirmed_recovered_review_move_is_never_replayed(self):
        self.missing_checks()
        move = self.gh.move

        def uncertain(card, stage):
            if stage == 'Review':
                raise Halt('fixture unknown stage response')
            move(card, stage)

        with patch.object(self.gh, 'move', side_effect=uncertain), self.assertRaises(Halt):
            self.watch()
        before = list(self.gh.writes)
        session = read_json(common_dir(self.root) / 'sfc-watch.json')
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        with self.assertRaisesRegex(Halt, 'could not be verified'):
            self.watch(resume=session['id'])
        self.assertEqual(self.gh.writes, before)
        action = self.factory.record(self.card)['blocked_recoveries'][-1]
        self.assertEqual(action['stage_transition']['phase'], 'intent')

    def test_drift_before_recovered_review_move_never_changes_board_stage(self):
        self.missing_checks(structured=True)
        original = Path(self.old['verification']['checks'][0]['log'])

        def drift(lane, _):
            if lane == 'security':
                original.write_text('retained recovery log changed before stage write\n')

        self.proc.on_lane = drift
        self.watch()
        r = self.factory.record(self.card)
        self.assertEqual(r['status'], 'QA')
        self.assertNotIn((1, 'Review'), self.gh.writes)
        self.assertIn('drift', r.get('recovery_wait', ''))
        self.assertEqual(r['blocked_recoveries'][-1]['stage_transition']['target'], 'QA')
        self.assertEqual(r['blocked_recoveries'][-1]['stage_transition']['phase'], 'verified')

    def test_recovered_ci_preflight_read_reset_leaves_other_tickets_selectable(self):
        self.missing_checks()
        self.prepare_watch()
        self.assertTrue(recovery.run(self.factory, self.card))
        self.gh.move(self.card, 'Review')
        self.r['status'] = 'Review'
        self.factory.allow_merge = True
        self.gh.items.append(ticket(2, priority='P3'))
        self.factory.issue_scope = (1, 2)
        cards = pickup.cards(self.gh)
        with (patch.object(self.factory, 'pinned', return_value=True),
              patch.object(self.factory, 'verification_evidence', return_value={}),
              patch.object(self.factory, 'assert_remote', side_effect=ReadConnectionReset('fixture CI read reset'))):
            selected, waiting = self.factory.priority_plan(cards)
        self.assertEqual([card['number'] for card in selected], [2])
        self.assertEqual(waiting[0]['reasons'], [recovery.RECOVERY_READ_WAIT])

    def test_stop_after_review_intent_leaves_a_known_undispatched_move(self):
        self.missing_checks()
        self.prepare_watch()
        self.assertTrue(recovery.run(self.factory, self.card))
        action = self.r['blocked_recoveries'][-1]
        ensure = self.factory.ensure_running

        def stop_before_send(*args, **kwargs):
            if action.get('stage_transition', {}).get('phase') == 'intent':
                raise Stopped('fixture stopped before Project stage mutation')
            return ensure(*args, **kwargs)

        before = list(self.gh.writes)
        with (patch.object(self.factory, 'ensure_running', side_effect=stop_before_send),
              self.assertRaises(Stopped)):
            self.factory.move(self.card, 'Review', 'fixture recovery stage move')
        self.assertEqual(self.gh.writes, before)
        self.assertEqual(action['stage_transition']['phase'], 'not-dispatched')
        self.factory.move(self.card, 'Review', 'fixture recovery stage move')
        self.assertEqual(self.gh.writes.count((1, 'Review')), 1)

    def test_missing_project_item_during_optional_failed_check_note_is_ticket_local(self):
        from sfc_lib.priority import ProjectItemMissing
        self.missing_checks()
        self.prepare_watch()
        action = {'checks': [{'exit_code': 1}]}
        with (patch.object(pickup, 'card', side_effect=ProjectItemMissing('fixture null node')),
              patch.object(self.factory, 'note') as note):
            recovery.report_failed_check(self.factory, self.card, action)
        note.assert_not_called()

    def test_evidence_drift_in_last_admission_read_prevents_stage_write(self):
        self.missing_checks(structured=True)
        original = Path(self.old['verification']['checks'][0]['log'])
        assert_remote = self.factory.assert_remote

        def drift(card, *args, **kwargs):
            record = self.factory.record(card)
            actions = record.get('blocked_recoveries', [])
            if actions and actions[-1]['phase'] == 'reserved' and actions[-1]['checks']:
                original.write_text('retained log changed during last live admission read\n')
            return assert_remote(card, *args, **kwargs)

        before = self.gh.writes.count((1, 'QA'))
        with patch.object(self.factory, 'assert_remote', side_effect=drift):
            self.watch()
        self.assertEqual(self.gh.writes.count((1, 'QA')), before)
        r = self.factory.record(self.card)
        self.assertEqual(r['status'], 'Blocked')
        self.assertIn('drift', r['recovery_wait'])

    def test_lost_human_handoff_write_resumes_without_repeating_review(self):
        self.missing_checks()
        move = self.gh.move

        def lost(card, stage):
            handoff = card['status'] == 'Review' and stage == 'Blocked'
            move(card, stage)
            if handoff:
                raise Halt('fixture lost human-handoff response')

        with patch.object(self.gh, 'move', side_effect=lost), self.assertRaises(Halt):
            self.watch()
        r = self.factory.record(self.card)
        self.assertNotEqual(r.get('activity', {}).get('state'), 'Failed')
        session = read_json(common_dir(self.root) / 'sfc-watch.json')
        before = list(self.gh.writes), list(self.proc.lanes)
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        self.watch(resume=session['id'])
        self.assertEqual((self.gh.writes, self.proc.lanes), before)
        r = self.factory.record(self.card)
        self.assertEqual(r['status'], 'Blocked')
        self.assertEqual(r['handoff'], r['head'])
        self.assertIn('merge_authority', [choice['code'] for choice in r['handoff_reasons']])
        self.assertEqual((r['activity']['state'], r['activity']['action']), ('Waiting', 'human'))

    def test_hold_during_recovered_draft_creation_resumes_published_candidate(self):
        self.options['max_polls'] = 10
        lane = self.factory._lane
        create = self.gh.create_pr
        interrupted = False
        released = False

        def first_read_reset(card, name, wt, base, **kwargs):
            nonlocal interrupted
            if name == 'build' and not interrupted:
                interrupted = True
                raise ReadConnectionReset('fixture read reset before Build')
            return lane(card, name, wt, base, **kwargs)

        def hold(*args, **kwargs):
            result = create(*args, **kwargs)
            self.gh.items[0]['labels'].append('sfc-hold')
            return result

        def clear_hold():
            nonlocal released
            if self.gh.prs and not released:
                released = True
                self.gh.items[0]['labels'].remove('sfc-hold')

        self.clock.on_wait = clear_hold
        with (patch.object(self.factory, '_lane', side_effect=first_read_reset),
              patch.object(self.gh, 'create_pr', side_effect=hold) as drafts):
            self.watch()
        self.assertTrue(released)
        drafts.assert_called_once()
        self.assertEqual(self.proc.lanes, ['build', 'qa', 'security', 'review'])
        r = self.factory.state['cards']['1']
        self.assertTrue(r.get('handoff'))
        self.assertFalse(r.get('recovery_wait'))

    def test_lost_recovered_draft_response_reads_back_without_a_second_build(self):
        lane = self.factory._lane
        create, lookup = self.gh.create_pr, self.gh.pr
        interrupted = False

        def first_read_reset(card, name, wt, base, **kwargs):
            nonlocal interrupted
            if name == 'build' and not interrupted:
                interrupted = True
                raise ReadConnectionReset('fixture read reset before Build')
            return lane(card, name, wt, base, **kwargs)

        def lost(*args, **kwargs):
            create(*args, **kwargs)
            raise Halt('fixture draft creation response lost')

        def unreadable(number):
            if self.gh.prs:
                raise ReadConnectionReset('fixture draft readback reset')
            return lookup(number)

        with (patch.object(self.factory, '_lane', side_effect=first_read_reset),
              patch.object(self.gh, 'create_pr', side_effect=lost) as drafts):
            with patch.object(self.gh, 'pr', side_effect=unreadable), self.assertRaises(Halt):
                self.watch()
            record = self.factory.state['cards']['1']
            publication = record['blocked_recoveries'][-1]['publication']
            self.assertEqual(publication['phase'], 'draft-intent')
            self.assertEqual(self.gh.items[0]['status'], 'Building')
            self.assertNotEqual(record['activity']['state'], 'Failed')
            session = read_json(common_dir(self.root) / 'sfc-watch.json')
            self.factory = Factory(self.root, self.c, self.gh, self.proc)
            self.watch(resume=session['id'])

        drafts.assert_called_once()
        self.assertEqual(self.proc.lanes, ['build', 'qa', 'security', 'review'])
        record = self.factory.state['cards']['1']
        self.assertEqual(record['head'], publication['head'])
        self.assertEqual(record['handoff'], publication['head'])
        self.assertEqual(record['blocked_recoveries'][-1]['publication']['phase'], 'verified')

    def test_preupgrade_recovery_pr_read_reset_does_not_stop_other_pickup(self):
        self.missing_checks(legacy=True)
        self.gh.items.append(ticket(2, priority='P3'))
        lookup = self.gh.pr_state

        def unavailable(number):
            if number == self.old['pr'] and not self.factory.state['cards'].get('2', {}).get('handoff'):
                raise ReadConnectionReset('fixture legacy PR lookup reset')
            return lookup(number)

        with patch.object(self.gh, 'pr_state', side_effect=unavailable):
            self.watch()
        self.assertTrue(self.factory.state['cards']['2']['handoff'])
        self.assertTrue(self.factory.state['cards']['1']['handoff'])

    def test_hold_during_recovery_activity_prevents_followup_comment(self):
        self.missing_checks()
        self.prepare_watch()
        report = self.factory.activity.report

        def hold(card, state, action, *args, **kwargs):
            result = report(card, state, action, *args, **kwargs)
            if action == 'recovered':
                self.gh.items[0]['labels'].append('sfc-hold')
            return result

        before = list(self.gh.notes)
        with patch.object(self.factory.activity, 'report', side_effect=hold):
            self.assertFalse(recovery.run(self.factory, self.card))
        self.assertEqual(self.gh.notes, before)
        self.assertEqual(self.r['status'], 'QA')
        self.assertEqual(self.r['blocked_recoveries'][-1]['phase'], 'admitted')

    def test_hold_during_admission_activity_preflight_prevents_both_writes(self):
        self.missing_checks()
        self.prepare_watch()
        snapshot = self.gh.activity_snapshot
        at_hold = []

        def hold(card):
            result = snapshot(card)
            if card['status'] == 'QA' and 'sfc-hold' not in self.gh.items[0]['labels']:
                self.gh.items[0]['labels'].append('sfc-hold')
                at_hold.append((self.gh.activity_writes.copy(), self.gh.notes.copy()))
            return result

        with patch.object(self.gh, 'activity_snapshot', side_effect=hold):
            self.assertFalse(recovery.run(self.factory, self.card))
        self.assertEqual(at_hold, [(self.gh.activity_writes, self.gh.notes)])
        self.assertEqual(self.r['status'], 'QA')
        self.assertEqual(self.r['blocked_recoveries'][-1]['phase'], 'admitted')
        self.assertNotIn('error', self.r['activity'])

    def test_lost_published_build_stage_response_resumes_same_candidate(self):
        self.options['max_polls'] = 10
        lane, move = self.factory._lane, self.gh.move
        interrupted = False

        def before_build(card, name, wt, base, **kwargs):
            nonlocal interrupted
            if name == 'build' and not interrupted:
                interrupted = True
                raise ReadConnectionReset('fixture deferred original Build')
            return lane(card, name, wt, base, **kwargs)

        def lose_ack(card, stage):
            published_build = card['status'] == 'Building' and stage == 'QA'
            move(card, stage)
            if published_build:
                raise Halt('fixture lost Building to QA acknowledgement')

        with (patch.object(self.factory, '_lane', side_effect=before_build),
              patch.object(self.gh, 'move', side_effect=lose_ack), self.assertRaisesRegex(Halt, 'lost Building to QA')):
            self.watch()
        saved = self.factory.state['cards']['1']
        self.assertEqual(saved['status'], 'Building')
        self.assertEqual(saved['blocked_recoveries'][-1]['published'], {'head': saved['head'], 'pr': saved['pr']})
        session = read_json(common_dir(self.root) / 'sfc-watch.json')
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        self.watch(resume=session['id'])
        self.assertEqual(self.gh.writes.count((1, 'QA')), 1)
        self.assertEqual(self.proc.lanes, ['build', 'qa', 'security', 'review'])
        self.assertTrue(self.factory.state['cards']['1']['handoff'])

    def test_lost_recovered_qa_only_done_response_reconciles_both_status_fields(self):
        self.gh.items[0]['labels'].append('qa')
        self.gh.github_status['item-1'] = 'in-progress'
        self.missing_checks(structured=True)
        move = self.gh.move

        def lose_ack(card, stage):
            move(card, stage)
            if stage == 'Done':
                raise Halt('fixture lost QA-only Done acknowledgement')

        with (patch.object(self.gh, 'move', side_effect=lose_ack),
              self.assertRaisesRegex(Halt, 'lost QA-only Done')):
            self.watch()
        session = read_json(common_dir(self.root) / 'sfc-watch.json')
        lanes = list(self.proc.lanes)
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        self.watch(resume=session['id'])
        self.assertEqual(self.gh.writes.count((1, 'Done')), 1)
        self.assertEqual(self.proc.lanes, lanes)
        r = self.factory.state['cards']['1']
        self.assertEqual(r['status'], 'Done')
        self.assertEqual(r['done_status']['state'], 'verified')
        self.assertEqual(self.gh.github_status_writes, ['item-1'])

    def test_protected_merge_known_pre_dispatch_stop_has_no_unresolved_outcome(self):
        from sfc_lib.external_merge import ExternalMerge
        card = pickup.cards(self.gh)[0]
        merge = ExternalMerge(self.factory)
        with (patch.object(merge, 'capture', return_value={'pr_id': 'fixture-pr'}),
              patch.object(self.factory, 'ensure_running', side_effect=Stopped('fixture stopped')),
              patch.object(self.gh, 'merge') as write):
            with self.assertRaises(Stopped):
                merge.protected(card, {'number': 101}, 'a' * 40, 'b' * 40, 'c' * 40,
                                open_read_started='2026-11-17T06:00:00+00:00')
        write.assert_not_called()
        r = self.factory.record(card)
        self.assertNotIn('external_completion', r)
        self.assertEqual(r['undispatched_protected_merge']['phase'], 'not-dispatched')

    def test_waiting_after_review_keeps_the_original_admitted_recovery_pins(self):
        self.missing_checks()
        self.prepare_watch()
        self.assertTrue(recovery.run(self.factory, self.card))
        self.gh.move(self.card, 'Review')
        self.r['status'] = 'Review'
        with patch.object(self.factory, '_process', return_value=None):
            self.factory.process(self.card)
        self.assertEqual(self.r['blocked_recoveries'][0]['phase'], 'admitted')

    def test_recovered_protected_merge_drift_keeps_completion_writes_gated(self):
        self.c['merge_policy'] = 'auto'
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        self.missing_checks(structured=True)
        original = Path(self.old['verification']['checks'][0]['log'])
        merge = self.gh.merge

        def drift(number, head):
            merge(number, head)
            original.write_text('fixture retained recovery evidence changed after merge\n')

        with (patch.object(self.gh, 'merge', side_effect=drift),
              self.assertRaisesRegex(Halt, 'Protected GitHub merge completion unresolved')):
            self.watch(allow_merge=True)
        r = self.factory.record(self.card)
        self.assertEqual(len(self.gh.merges), 1)
        self.assertNotIn((1, 'Done'), self.gh.writes)
        self.assertEqual(r['status'], 'Review')
        self.assertEqual(r['blocked_recoveries'][-1]['phase'], 'admitted')
