from __future__ import annotations

import copy
import datetime as dt
import json
from pathlib import Path
import shlex
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

import test_lane_evidence as lane_fixture
import test_local_merge as merge_fixture
from test_factory import BASE_CONFIG, FakeProcesses, FakeGitHub, issue
from sfc_lib.common import Halt, Stopped, git, run, write_json, validate_config
from sfc_lib.factory import Factory
from sfc_lib import review_cohort, review_policy as policy
from sfc_lib.review_benchmark import replay_prior_qa
from sfc_lib.verification import checksum, digest, source_tree


class PolicyProcesses(FakeProcesses):
    def __init__(self, stop):
        super().__init__(stop)
        self.mutate = lambda result: None
        self.interrupt = False
        self.complete = True
        self.message_matches = True
        self.native_mutate = lambda verdict: None
        self.prompts = []
        self.commands = []
        self.qa_events = []

    def execute(self, argv, cwd, log, timeout, prompt=None, **kwargs):
        if argv[0] != 'codex' or 'an SFC build worker' in prompt:
            return super().execute(argv, cwd, log, timeout, prompt, **kwargs)
        lane = next(v for v in ('qa', 'security', 'review') if f'an SFC {v} worker' in prompt)
        self.lanes.append(lane)
        self.prompts.append(prompt)
        self.commands.append(argv)
        if self.interrupt:
            Path(log).write_text('interrupted native review\n')
            raise Stopped('test interruption')
        scope = json.loads(prompt.split('Exact scope (echo unchanged): ')[1].splitlines()[0])
        criteria = json.loads(prompt.split('Acceptance obligations: ')[1].splitlines()[0])
        evidence = json.loads(prompt.split('configuration:\n')[1].splitlines()[0])
        result = {'verdict': 'pass', 'summary': 'Independent fixture review', 'findings': [],
                  'evidence': 'Actual temporary-repository configured check log inspected',
                  'completed': True, 'scope': scope, 'escalate': [], 'coverage': [
                      {'criterion': v['id'], 'status': 'covered', 'reason': 'Fixture assertion exercises acceptance',
                       'evidence': [{'kind': 'check', 'id': '1', 'sha256': checksum(evidence['checks'][0]['log'])}]}
                      for v in criteria]}
        self.mutate(result)
        output = Path(argv[argv.index('--output-last-message') + 1])
        thread = str(uuid4())
        if 'review' in argv:
            findings = result.pop('findings')
            message = policy.NATIVE_REPORT + json.dumps(result)
            native = {'overall_correctness': 'patch is correct' if result['verdict'] == 'pass' else 'patch is incorrect',
                      'overall_explanation': message, 'overall_confidence_score': 1.0, 'findings': [
                          {'title': '[P1] ' + finding['message'], 'body': 'Reproduced against the candidate.',
                           'priority': 1, 'confidence_score': 1.0, 'code_location': {
                               'absolute_file_path': str(Path(cwd) / finding['file']),
                               'line_range': {'start': finding['line'], 'end': finding['line']}}}
                          for finding in findings]}
            self.native_mutate(native)
            if findings:
                message += '\n\nReview comment:\n\n- [P1] ' + findings[0]['message']
                message += f" — {Path(cwd) / findings[0]['file']}:{findings[0]['line']}-{findings[0]['line']}\n"
                message += '  Reproduced against the candidate.'
            output.write_text(message + '\n')
            records = [{'type': 'session_meta', 'payload': {'id': thread, 'cwd': str(cwd), 'cli_version': 'fixture'}},
                       {'type': 'event_msg', 'payload': {'type': 'item_completed', 'thread_id': thread,
                           'turn_id': 'turn-1', 'item': {'type': 'ExitedReviewMode', 'review_output': native}}}]
            if self.complete:
                records.append({'type': 'event_msg', 'payload': {'type': 'task_complete', 'turn_id': 'turn-1'}})
            session = policy.native_sessions() / '2026/11/11' / f'rollout-test-{thread}.jsonl'
            session.parent.mkdir(parents=True, exist_ok=True)
            session.write_text(''.join(json.dumps(row) + '\n' for row in records))
        else:
            message = json.dumps(result)
            write_json(output, result)
        events = [{'type': 'thread.started', 'thread_id': thread}, {'type': 'turn.started'}]
        if 'review' in argv:
            events.append({'type': 'turn.started'})
        if lane == 'qa':
            events.extend(self.qa_events)
        if observer := kwargs.get('observer'):
            observer('started')
            observer('command')
            observer('inactive')
        events.append({'type': 'item.completed', 'item': {'type': 'agent_message', 'id': 'item_1',
                       'text': message if self.message_matches else 'Different final message'}})
        if self.complete:
            events.append({'type': 'turn.completed', 'usage': {'input_tokens': 1}})
        Path(log).write_text('\n'.join(json.dumps(v) for v in events) + '\n')
        return 0


def capabilities(argv, *args, **kwargs):
    if argv[0] != 'codex':
        return run(argv, *args, **kwargs)
    return subprocess.CompletedProcess(argv, 0, 'codex-cli fixture' if argv[-1] == '--version' else
        '--json --ephemeral --output-last-message', '')


def write_github_review(path, receipt, result, review_id, *, pr=101):
    log = path.with_suffix('.raw.json')
    result_path = path.with_suffix('.result.json')
    cloud = {'id': review_id, 'user': {'id': 42, 'type': 'Bot'}, 'commit_id': receipt['head'],
             'submitted_at': receipt['time'], 'body': json.dumps(result),
             'state': 'COMMENTED' if result['verdict'] == 'pass' else 'CHANGES_REQUESTED'}
    write_json(log, cloud)
    write_json(result_path, result)
    write_json(path, {**receipt, 'provider': {'provider': 'github-native', 'pr': pr, 'review_id': review_id},
                      'log': str(log), 'log_sha256': checksum(log), 'result': result,
                      'result_path': str(result_path), 'result_sha256': checksum(result_path)})
    return cloud


class ReviewPolicyTests(lane_fixture.LaneEvidenceTests):
    def setUp(self):
        super().setUp()
        self.config['review_policy'] = {'providers': {'review': 'codex-native'}, 'routine_paths': ['value.txt']}
        self.processes = PolicyProcesses(self.root / '.sfc/stop')
        self.factory = Factory(self.root, self.config, FakeGitHub(self.root), self.processes)
        self.factory.run_dir = self.root / '.sfc/runs/policy'
        self.factory.verify(self.card, self.root, self.head)
        self.factory.update(self.card, worktree=str(self.root), base=self.head,
                            review_route=policy.plan(self.factory, self.card, self.root, self.head))
        self.cap = patch('sfc_lib.review_policy.run', side_effect=capabilities)
        self.cap.start()
        self.addCleanup(self.cap.stop)
        sessions = patch('sfc_lib.review_policy.native_sessions', return_value=self.root / '.sfc/sessions')
        sessions.start()
        self.addCleanup(sessions.stop)

    # Parent tests exercise the legacy contract separately, not the policy schema.
    def test_read_only_lanes_receive_real_pinned_execution_logs(self):
        for lane in ('qa', 'security', 'review'):
            self.factory.lane(self.card, lane, self.root, self.head)
        self.assertTrue(self.factory.pinned(self.card, self.head, self.head))
        argv = self.processes.commands[-1]
        self.assertIn('review', argv)
        self.assertNotIn('resume', argv)
        self.assertNotIn('--base', argv)
        self.assertNotIn('--output-schema', argv)
        self.assertEqual(argv[argv.index('-s') + 1], 'read-only')
        proof = self.factory.record(self.card)['receipts']['review']
        self.assertEqual(proof['provider']['version'], 'codex-cli fixture')
        self.assertTrue(policy.completed_message(proof['log'], native=True)[1].startswith(policy.NATIVE_REPORT))
        self.assertTrue(Path(proof['native_message']).read_text().startswith(policy.NATIVE_REPORT))
        rollout = Path(proof['native_rollout'])
        self.assertEqual(checksum(rollout), proof['native_rollout_sha256'])
        self.assertEqual(rollout.stat().st_mode & 0o077, 0)
        self.assertEqual(rollout.parent.stat().st_mode & 0o077, 0)
        self.assertEqual(Path(proof['result_path']).name, 'review-result.json')
        self.assertIn(self.head, self.processes.prompts[-1])

    def test_native_review_uses_configured_worker_context(self):
        config = {**self.config, 'worker_runtime': {'skill_catalog': False, 'memories': False}}
        factory = Factory(self.root, config, self.factory.gh, self.processes)
        factory.run_dir = self.factory.run_dir
        self.assertTrue(factory.verify(self.card, self.root, self.head))

        factory.lane(self.card, 'review', self.root, self.head)
        command = self.processes.commands[-1]
        options = command[:command.index('review')]
        for setting in ('skills.include_instructions=false', 'memories.use_memories=false',
                        'memories.generate_memories=false'):
            self.assertEqual(options[options.index(setting) - 1], '-c')
        self.assertEqual(options[options.index('-s') + 1], 'read-only')

    def test_behavioral_qa_shares_baseline_scope_and_inlines_assigned_check_source(self):
        source = self.root / 'message.py'
        behavior_check = "from pathlib import Path\nassert 'hello' in Path('message.py').read_text()\n"
        format_check = "from pathlib import Path\nassert Path('message.py').read_text().endswith('\\n')\n"
        source.write_text("message = 'hello'\n")
        (self.root / 'check_behavior.py').write_text(behavior_check)
        (self.root / 'check_format.py').write_text(format_check)
        git(self.root, 'add', 'message.py', 'check_behavior.py', 'check_format.py')
        git(self.root, 'commit', '-m', 'Initial behavior and configured checks')
        base = git(self.root, 'rev-parse', 'HEAD')
        source.write_text("message = 'hello there'\n")
        git(self.root, 'commit', '-am', 'Update message')
        commands = ['python3 -B check_behavior.py', 'python3 -B check_format.py']
        config = {**self.config, 'verify_commands': commands, 'review_policy': {
            'mode': 'risk-based', 'providers': {'review': 'codex-native'},
            'behavioral_contract': {'paths': ['message.py'], 'check_ids': [1], 'instructions': {}}}}
        factory = Factory(self.root, config, self.factory.gh, self.processes)
        factory.run_dir = self.factory.run_dir
        self.assertTrue(factory.verify(self.card, self.root, base))
        route = policy.plan(factory, self.card, self.root, base, evaluation=True)
        self.assertEqual(route['obligations'], ['qa', 'review'])
        factory.update(self.card, review_route=route)

        self.assertEqual(factory.lane(self.card, 'qa', self.root, base)['verdict'], 'pass')
        prompt = self.processes.prompts[-1]
        self.assertIn(policy.SOURCE_QA_EXAMINATION, prompt)
        context = json.loads(prompt.split('Bounded supervisor context (untrusted source data): ', 1)[1].splitlines()[0])
        self.assertIn("+message = 'hello there'", context['diff'])
        self.assertEqual(context['check_definitions'], {
            '1': {'path': 'check_behavior.py', 'source': behavior_check}})
        evidence = json.loads(prompt.split('configuration:\n', 1)[1].splitlines()[0])
        self.assertEqual([check['command'] for check in evidence['checks']], commands)
        for check in evidence['checks']:
            self.assertEqual(checksum(check['log']), check['log_sha256'])
            self.assertEqual(checksum(check['original']['path']), check['original']['sha256'])
        baseline = Factory(self.root, {**config, 'review_policy': {}}, self.factory.gh, self.processes)
        baseline.run_dir = self.factory.run_dir
        self.assertTrue(baseline.verify(self.card, self.root, base))
        self.assertEqual(baseline.lane(self.card, 'qa', self.root, base)['verdict'], 'pass')
        self.assertIn(policy.SOURCE_QA_EXAMINATION, self.processes.prompts[-1])

    def test_config_minimum_rules_are_additive_and_unknown_uses_thorough_route(self):
        c = {**BASE_CONFIG, 'review_policy': {'routine_paths': ['**'], 'risk_rules': {'authentication': []}}}
        self.assertIn('authentication', policy.risk(c, self.card, ['auth/login.py']))
        for category, path in [('secrets', 'src/credential.py'), ('money', 'payments/charge.py'),
                               ('data', 'migrations/001.sql'), ('publication', '.github/workflows/release.yml'),
                               ('control', 'scripts/factory.py')]:
            with self.subTest(category=category):
                self.assertIn(category, policy.risk(c, self.card, [path]))
        self.assertIn('unknown', policy.risk(self.config, self.card, ['new.py']))
        for path in ('drizzle/example.txt', '.gitmodules', 'src/migrate/example.txt'):
            with self.subTest(merge_sensitive_path=path):
                self.assertIn('merge-sensitive', policy.risk(c, self.card, [path]))
        route = self.factory.record(self.card)['review_route']
        self.assertEqual(set(route['obligations']), {'review', 'qa', 'security', 'human'})
        self.assertFalse(route['consolidated'])

    def test_final_review_retains_prior_qa_and_targeted_qa_closes_distinct_gap(self):
        source = self.root / 'message.py'
        source.write_text("message = 'hello'\n")
        git(self.root, 'add', 'message.py')
        git(self.root, 'commit', '-m', 'Initial behavior')
        base = git(self.root, 'rev-parse', 'HEAD')
        source.write_text("message = 'hello there'\n")
        git(self.root, 'commit', '-am', 'Update message')
        head = git(self.root, 'rev-parse', 'HEAD')
        config = {**self.config, 'review_policy': {'mode': 'risk-based', 'providers': {'review': 'codex-native'},
                  'behavioral_contract': {'paths': ['message.py'], 'check_ids': [1], 'instructions': {}}}}
        factory = Factory(self.root, config, self.factory.gh, self.processes)
        factory.run_dir = self.factory.run_dir
        card = {**self.card, 'body': '## Acceptance criteria\n- The message begins with hello.\n- The message has a suffix.'}
        self.assertTrue(factory.verify(card, self.root, base))
        factory.update(card, review_route=policy.plan(factory, card, self.root, base, evaluation=True))
        record = factory.record(card)

        command = "python3 -B -c 'from message import message; assert message.startswith(\"hello\")'"
        completed = subprocess.run(shlex.split(command), cwd=self.root, text=True, capture_output=True, timeout=5)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        event = {'type': 'command_execution', 'id': 'focused', 'command': command}
        self.processes.qa_events = [
            {'type': 'item.started', 'item': {**event, 'status': 'in_progress'}},
            {'type': 'item.completed', 'item': {**event, 'status': 'completed', 'exit_code': completed.returncode,
                                               'aggregated_output': completed.stdout}}]
        self.processes.mutate = lambda report: report['coverage'][0].update(
            evidence=[{'kind': 'qa-command', 'id': command, 'sha256': ''}])
        factory.lane(card, 'qa', self.root, base)
        qa = record['receipts']['qa']
        anchor = {'path': qa['receipt_path'], 'sha256': qa['receipt_sha256']}
        replay_route = copy.deepcopy(record['review_route'])

        def cite_prior_qa(report):
            first, second = report['coverage']
            first.update(evidence=[{'kind': 'prior-qa', 'id': first['criterion'], 'sha256': anchor['sha256']}])
            second.update(status='qa_required', evidence=[])

        self.processes.mutate = cite_prior_qa
        factory.lane(card, 'review', self.root, base)
        review = record['receipts']['review']
        self.assertEqual(review['prior_qa'], anchor)
        self.assertIn('Validated prior QA receipt: ' + json.dumps(anchor), self.processes.prompts[-1])
        self.assertFalse(policy.acceptance_complete(factory, card))

        replay = Factory(self.root, config, self.factory.gh, self.processes)
        replay.update(card, review_route=replay_route, receipts={'qa': qa}, verification=review['verification'])
        self.assertEqual(replay_prior_qa(replay, card, self.root, review), anchor)
        replay.record(card)['receipts'].clear()
        with self.assertRaisesRegex(ValueError, 'exact preceding QA'):
            replay_prior_qa(replay, card, self.root, review)

        self.processes.mutate = lambda report: None
        self.assertTrue(factory.finish_review_obligations(card, self.root, head, base))
        self.assertEqual(self.processes.lanes, ['qa', 'review', 'qa'])
        self.assertNotEqual(record['receipts']['qa']['receipt_sha256'], anchor['sha256'])
        self.assertTrue(policy.proof_valid(factory, card, self.root, review, head, base))

        with Path(qa['log']).open('a') as output:
            output.write('changed prior QA evidence\n')
        self.assertFalse(policy.proof_valid(factory, card, self.root, review, head, base))

    def test_allowlisted_sensitive_and_unknown_source_changes_require_thorough_review(self):
        config = {**self.config, 'review_policy': {'mode': 'risk-based',
            'providers': {'review': 'codex-native'}, 'routine_paths': ['*']}}
        f = Factory(self.root, config, self.factory.gh, self.processes)
        for path, original, changed, category in (
                ('merge.py', 'return approved', 'return True', 'control'),
                ('records.py', 'records.delete_many({"old": True})', 'records.delete_many({})', 'data'),
                ('records.js', 'records.deleteMany({old: true})', 'records.deleteMany({})', 'data'),
                ('cleanup.py', 'rows.filter_by(tenant=tenant).delete()', 'rows.delete()', 'data'),
                ('access.py', 'return requester.is_admin', 'return True', 'authentication'),
                # An allowlist and the absence of known tokens cannot prove
                # that a behavioral source change is routine.
                ('service.py', 'return lookup(requester)', 'return lookup(None)', None)):
            with self.subTest(path=path):
                target = self.root / path
                target.write_text(original + '\n')
                git(self.root, 'add', path)
                git(self.root, 'commit', '-m', 'Fixture starting behavior')
                base = git(self.root, 'rev-parse', 'HEAD')
                target.write_text(changed + '\n')
                git(self.root, 'commit', '-am', 'Fixture behavior change')
                with patch.object(policy, 'promoted', return_value=(True, 'test promotion')):
                    route = policy.plan(f, self.card, self.root, base)
                self.assertIn(category or 'unknown', route['risk'])
                self.assertFalse(route['consolidated'])
                self.assertEqual(set(route['obligations']), {'review', 'qa', 'security', 'human'})

    def test_malformed_coverage_scope_and_completion_fail_closed(self):
        changes = [lambda r: r.update(completed=False), lambda r: r.update(coverage=[]),
                   lambda r: r['scope'].update(base='0' * 40), lambda r: r['scope'].update(config='changed'),
                   lambda r: r['coverage'][0].update(evidence=[]), lambda r: r.update(escalate=['downgrade']),
                   lambda r: r['coverage'][0]['evidence'][0].update(sha256='changed'),
                   lambda r: r['coverage'][0]['evidence'][0].update(id='2')]
        for index, change in enumerate(changes):
            # New card per case preserves spent failure reservations.
            card = issue(number=10 + index)
            self.factory.verify(card, self.root, self.head)
            self.processes.mutate = change
            with self.subTest(index=index), self.assertRaisesRegex(Halt, 'Malformed'):
                self.factory.lane(card, 'review', self.root, self.head)
            dispatch = self.factory.record(card)['worker_output_budget']['review']['dispatches'][-1]
            self.assertEqual(dispatch['status'], 'malformed')
            self.assertTrue(Path(dispatch['log']).exists())
            self.assertNotIn('review', self.factory.record(card).get('receipts', {}))

    def test_focused_qa_command_requires_matching_successful_turn_and_immutable_log(self):
        command = "python3 -B -c 'assert 1 + 1 == 2'"
        item = {'type': 'command_execution', 'id': 'command-1', 'command': command}
        start = {'type': 'item.started', 'item': {**item, 'status': 'in_progress'}}
        complete = {'type': 'item.completed', 'item': {
            **item, 'status': 'completed', 'exit_code': 0, 'aggregated_output': ''}}
        reference = {'kind': 'qa-command', 'id': command, 'sha256': ''}
        self.processes.mutate = lambda report: report['coverage'][0].update(evidence=[reference])
        self.processes.qa_events = [start, complete]
        self.factory.lane(self.card, 'qa', self.root, self.head)
        proof = self.factory.record(self.card)['receipts']['qa']
        self.assertTrue(policy.proof_valid(self.factory, self.card, self.root, proof, self.head, self.head))
        with Path(proof['log']).open('a') as output:
            output.write('changed retained log\n')
        self.assertFalse(policy.proof_valid(self.factory, self.card, self.root, proof, self.head, self.head))
        with self.assertRaisesRegex(Halt, 'Malformed'):
            self.factory.lane(self.card, 'security', self.root, self.head)

        cases = [
            [complete],
            [start],
            [start, {**complete, 'item': {**complete['item'], 'command': 'different command'}}],
            [start, {**complete, 'item': {**complete['item'], 'exit_code': 1}}],
            [start, {**complete, 'item': {**complete['item'], 'status': 'failed'}}],
            [start, complete] + [{**e, 'item': {**e['item'], 'id': 'command-2'}} for e in (start, complete)],
        ]
        for index, events in enumerate(cases):
            card = issue(number=70 + index)
            self.factory.verify(card, self.root, self.head)
            self.processes.qa_events = events
            with self.subTest(index=index), self.assertRaisesRegex(Halt, 'Malformed'):
                self.factory.lane(card, 'qa', self.root, self.head)
            self.assertNotIn('qa', self.factory.record(card).get('receipts', {}))

        for key in ('complete', 'message_matches'):
            with self.subTest(invalid=key), patch.object(self.processes, key, False):
                card = issue(number=80)
                self.factory.verify(card, self.root, self.head)
                self.processes.qa_events = [start, complete]
                with self.assertRaisesRegex(Halt, 'Malformed'):
                    self.factory.lane(card, 'qa', self.root, self.head)

    def test_focused_qa_command_aliases_require_one_exact_successful_execution(self):
        command = "printf '%s\\n' 'literal \"$HOME\"; `text`'\ntrue\n"
        recorded = shlex.join(['/bin/zsh', '-lc', command])
        alternate = shlex.join(['/bin/sh', '-c', command])
        for reference in (command, recorded):
            with self.subTest(reference=reference):
                self.assertTrue(policy.successful_qa_command({recorded: [True]}, reference))
                self.assertFalse(policy.successful_qa_command({recorded: [False]}, reference))
                self.assertFalse(policy.successful_qa_command({recorded: [True, True]}, reference))
                self.assertFalse(policy.successful_qa_command({recorded: [True], alternate: [True]}, reference))
                self.assertFalse(policy.successful_qa_command({recorded: [True], alternate: [False]}, reference))
        for reference in ('true', command.strip(), alternate):
            self.assertFalse(policy.successful_qa_command({recorded: [True]}, reference))
        for argv in (['/bin/echo', '-c', command], ['/bin/sh', '-c', command, 'argument']):
            self.assertFalse(policy.successful_qa_command({shlex.join(argv): [True]}, command))

    def test_qa_can_cover_a_failing_criterion_with_a_successful_reproduction(self):
        path = self.root / 'message.py'
        path.write_text("message = 'error'\n")
        git(self.root, 'add', 'message.py')
        git(self.root, 'commit', '-m', 'Return the wrong message')
        card = issue(body='## Acceptance criteria\n- The message is OK.')
        self.assertTrue(self.factory.verify(card, self.root, self.head))
        command = "# sfc-qa-proof: message-result\npython3 -B - <<'PY'\nfrom message import message\nassert message == 'error'\nprint(message)\nPY"
        argv = ['/bin/sh', '-c', command]
        run_result = subprocess.run(argv, cwd=self.root, text=True, capture_output=True, timeout=5)
        self.assertEqual(run_result.returncode, 0, run_result.stderr)
        item = {'type': 'command_execution', 'id': 'reproduction', 'command': shlex.join(argv)}
        setup = command.split('\n', 1)[0] + '\npython3 -B -c "raise SystemExit(1)"'
        failed = {'type': 'command_execution', 'id': 'failed-setup',
                  'command': shlex.join(['/bin/sh', '-c', setup])}
        self.processes.qa_events = [
            {'type': 'item.started', 'item': {**failed, 'status': 'in_progress'}},
            {'type': 'item.completed', 'item': {**failed, 'status': 'failed', 'exit_code': 1,
                                               'aggregated_output': ''}},
            {'type': 'item.started', 'item': {**item, 'status': 'in_progress'}},
            {'type': 'item.completed', 'item': {**item, 'status': 'completed', 'exit_code': run_result.returncode,
                                               'aggregated_output': run_result.stdout}}]

        def report_failure(result):
            result.update(verdict='fail', findings=[{'file': 'message.py', 'line': 1, 'severity': 'medium',
                                                      'message': 'The message is error instead of OK'}])
            result['coverage'][0].update(status='covered', reason='Reproduced error instead of the required OK',
                                         evidence=[{'kind': 'qa-command', 'id': 'proof:message-result', 'sha256': ''}])

        self.processes.mutate = report_failure
        result = self.factory.lane(card, 'qa', self.root, self.head)
        self.assertEqual(result['verdict'], 'fail')
        self.assertEqual(result['coverage'][0]['status'], 'covered')
        self.assertIn('A reproduced failure requires verdict fail and a concrete finding', self.processes.prompts[-1])
        with self.assertRaisesRegex(ValueError, 'invalid review verdict'):
            policy.validate_result({**result, 'verdict': 'pass'})

        bad = issue(number=2, body=card['body'])
        self.assertTrue(self.factory.verify(bad, self.root, self.head))
        self.processes.qa_events[1]['item']['exit_code'] = '1'
        with self.assertRaisesRegex(Halt, 'Malformed'):
            self.factory.lane(bad, 'qa', self.root, self.head)

    def test_qa_proof_labels_require_unique_successful_first_line_markers(self):
        command = '# sfc-qa-proof: boundary\npython3 -B -c "assert True"'
        wrapped = shlex.join(['/bin/zsh', '-lc', command])
        other = '# sfc-qa-proof: boundary\npython3 -B -c "assert 1 == 1"'
        ref = 'proof:boundary'
        self.assertTrue(policy.successful_qa_command({wrapped: [True]}, ref))
        self.assertTrue(policy.successful_qa_command({command: [True]}, ref))
        for outcomes in ([False], [None], [True, True], [True, False], [False, True]):
            self.assertFalse(policy.successful_qa_command({wrapped: outcomes}, ref))
        self.assertTrue(policy.successful_qa_command({other: [False, False], wrapped: [True]}, ref))
        self.assertFalse(policy.successful_qa_command({wrapped: [False], other: [False]}, ref))
        self.assertFalse(policy.successful_qa_command({wrapped: [True], command: [False]}, ref))
        for outcome in (True, None):
            self.assertFalse(policy.successful_qa_command({wrapped: [True], other: [outcome]}, ref))
        self.assertFalse(policy.successful_qa_command({ref: [True]}, ref))
        for unexecuted in ('boundary', 'proof:bound', 'proof:Boundary'):
            self.assertFalse(policy.successful_qa_command({wrapped: [True]}, unexecuted))
        for invalid in ('echo ' + shlex.quote(command), 'true\n' + command,
                        '# sfc-qa-proof: boundary', '# sfc-qa-proof: boundary\n# only a comment',
                        '# sfc-qa-proof: boundary\rprintf ran', '# sfc-qa-proof: boundary\u2028printf ran',
                        shlex.join(['/bin/echo', '-c', command]),
                        '# sfc-qa-proof: Boundary\ntrue'):
            self.assertFalse(policy.successful_qa_command({invalid: [True]}, ref))

    def test_routine_prose_requires_exact_operator_attestation_even_with_promotion(self):
        config = {**self.config, 'review_policy': {'mode': 'risk-based',
            'providers': {'review': 'codex-native'}, 'routine_paths': ['value.txt']}}
        (self.root / 'value.txt').write_text('ordinary text\n')
        git(self.root, 'add', 'value.txt')
        git(self.root, 'commit', '-m', 'Fixture prose')
        head = git(self.root, 'rev-parse', 'HEAD')
        attestation = {'head': head, 'base': self.head}
        for proof in (None, {**attestation, 'head': self.head}, {**attestation, 'base': head},
                      {**attestation, 'task': '0' * 64}, attestation):
            with self.subTest(attestation=proof):
                candidate_config = copy.deepcopy(config)
                if proof is not None:
                    candidate_config['review_policy']['routine_candidate'] = {
                        'task': self.factory.task_digest(self.card), **proof}
                f = Factory(self.root, candidate_config, self.factory.gh, self.processes)
                with patch.object(policy, 'promoted', return_value=(True, 'test promotion')):
                    route = policy.plan(f, self.card, self.root, self.head)
                self.assertEqual(route['consolidated'], proof == attestation)
                self.assertEqual(set(route['obligations']),
                                 {'review'} if proof == attestation else {'review', 'qa', 'security', 'human'})

    def test_shell_consumed_text_cannot_take_the_unattested_routine_route(self):
        config = {**self.config, 'review_policy': {'mode': 'risk-based',
            'providers': {'review': 'codex-native'}, 'routine_paths': ['instructions.txt']}}
        instructions = self.root / 'instructions.txt'
        instructions.write_text('cleanup --dry-run fixture\n')
        # A non-executable text file is read by a tracked shell script.
        (self.root / 'driver.sh').write_text('cleanup() { printf "%s\\n" "$@"; }\n. ./instructions.txt\n')
        git(self.root, 'add', 'instructions.txt', 'driver.sh')
        git(self.root, 'commit', '-m', 'Add temporary shell fixture')
        base = git(self.root, 'rev-parse', 'HEAD')
        instructions.write_text('cleanup fixture\n')
        git(self.root, 'commit', '-am', 'Remove fixture dry-run flag')
        f = Factory(self.root, config, self.factory.gh, self.processes)
        head = git(self.root, 'rev-parse', 'HEAD')
        self.assertTrue(policy.prose_only(self.root, (base, head), ['instructions.txt']))
        with patch.object(policy, 'promoted', return_value=(True, 'test promotion')):
            route = policy.plan(f, self.card, self.root, base)
        self.assertIn('unknown', route['risk'])
        self.assertFalse(route['consolidated'])
        self.assertEqual(set(route['obligations']), {'review', 'qa', 'security', 'human'})

    def test_native_exit_zero_without_turn_completion_is_not_acceptance(self):
        self.processes.complete = False
        with self.assertRaisesRegex(Halt, 'Malformed'):
            self.factory.lane(self.card, 'review', self.root, self.head)

    def test_native_message_mismatch_fails_closed(self):
        self.processes.message_matches = False
        with self.assertRaisesRegex(Halt, 'Malformed'):
            self.factory.lane(self.card, 'review', self.root, self.head)

    def test_rendered_pass_with_native_parse_fallback_or_incorrect_verdict_fails_closed(self):
        for index, correctness in enumerate(('', 'patch is incorrect')):
            card = issue(number=30 + index)
            self.factory.verify(card, self.root, self.head)
            self.processes.native_mutate = lambda result: result.update(overall_correctness=correctness)
            with self.subTest(correctness=correctness), self.assertRaisesRegex(Halt, 'Malformed'):
                self.factory.lane(card, 'review', self.root, self.head)
            self.assertNotIn('review', self.factory.record(card).get('receipts', {}))

    def test_native_structured_findings_are_preserved_and_cannot_pass(self):
        finding = {'file': 'value.txt', 'line': 1, 'severity': 'high', 'message': 'Fix the value'}
        self.processes.mutate = lambda result: result.update(verdict='fail', findings=[finding])
        result = self.factory.lane(self.card, 'review', self.root, self.head)
        self.assertEqual(result['findings'], [{**finding, 'message': 'Fix the value\nReproduced against the candidate.'}])
        card = issue(number=33)
        self.factory.verify(card, self.root, self.head)
        self.processes.mutate = lambda result: result.update(findings=[finding])
        with self.assertRaisesRegex(Halt, 'Malformed'):
            self.factory.lane(card, 'review', self.root, self.head)

    def test_binary_allowlisted_file_requires_thorough_route(self):
        (self.root / 'figure.bin').write_bytes(b'\x00\x01\x02')
        git(self.root, 'add', 'figure.bin')
        git(self.root, 'commit', '-m', 'opaque fixture')
        self.factory.c['review_policy']['routine_paths'] = ['*.bin']
        with patch('sfc_lib.review_policy.promoted', return_value=(True, 'test promotion')):
            route = policy.plan(self.factory, self.card, self.root, self.head)
        self.assertIn('unknown', route['risk'])
        self.assertFalse(route['consolidated'])
        self.assertIn('human', route['obligations'])

    def test_existing_code_fence_outside_the_diff_requires_thorough_review(self):
        self.factory.c['review_policy']['routine_paths'] = ['guide.md']
        path = self.root / 'guide.md'
        prefix = '```python\n' + '\n' * 10
        path.write_text(prefix + 'rows = [1, 2]\nrows.remove(1)\n```\n')
        git(self.root, 'add', 'guide.md')
        git(self.root, 'commit', '-m', 'Fixture example')
        base = git(self.root, 'rev-parse', 'HEAD')
        path.write_text(prefix + 'rows = [1, 2]\nrows.clear()\n```\n')
        git(self.root, 'commit', '-am', 'Fixture edit')
        with patch.object(policy, 'promoted', return_value=(True, 'test promotion')):
            route = policy.plan(self.factory, self.card, self.root, base)
        self.assertFalse(route['consolidated'])
        self.assertIn('unknown', route['risk'])
        self.assertEqual(set(route['obligations']), {'review', 'qa', 'security', 'human'})

    def test_native_reviews_unchanged_symlinks_and_gitlinks_at_the_exact_source(self):
        child = self.root / 'child'
        child.mkdir()
        git(child, 'init', '-b', 'main')
        git(child, 'config', 'user.name', 'SFC Test')
        git(child, 'config', 'user.email', 'test@example.invalid')
        (child / 'value').write_text('pinned child\n')
        git(child, 'add', 'value')
        git(child, 'commit', '-m', 'child fixture')
        child_head = git(child, 'rev-parse', 'HEAD')
        link = self.root / 'link'
        link.symlink_to('absent')  # The link contents are tracked, including dangling targets.
        git(self.root, 'add', 'link')
        git(self.root, 'update-index', '--add', '--cacheinfo', f'160000,{child_head},child')
        git(self.root, 'update-index', '--add', '--cacheinfo', f'160000,{child_head},uninitialized')
        (self.root / 'uninitialized').mkdir()
        git(self.root, 'commit', '-m', 'base with special entries')
        base = git(self.root, 'rev-parse', 'HEAD')
        (self.root / 'value.txt').write_text('candidate\n')
        git(self.root, 'add', 'value.txt')
        git(self.root, 'commit', '-m', 'ordinary edit')
        head = git(self.root, 'rev-parse', 'HEAD')
        self.factory.verify(self.card, self.root, base)
        self.assertEqual(self.factory.lane(self.card, 'review', self.root, base)['verdict'], 'pass')
        with self.assertRaisesRegex(ValueError, 'unsupported source entry'):
            source_tree(self.root)  # Stage 1 reuse still uses the stricter binding.

        git(self.root, 'update-index', '--assume-unchanged', 'link')
        link.unlink()
        link.symlink_to('different')
        self.assertEqual(git(self.root, 'status', '--porcelain', '--ignore-submodules=none'), '')
        with self.assertRaisesRegex(Halt, 'exact clean candidate bytes'):
            policy.native_source(self.root, head, base)
        link.unlink()
        link.symlink_to('absent')
        git(child, 'update-index', '--assume-unchanged', 'value')
        (child / 'value').write_text('unbound child bytes\n')
        self.assertEqual(git(self.root, 'status', '--porcelain', '--ignore-submodules=none'), '')
        with self.assertRaisesRegex(Halt, 'exact clean candidate bytes'):
            policy.native_source(self.root, head, base)

    def test_native_unavailable_is_explicit_and_has_no_automatic_fallback(self):
        with patch('sfc_lib.review_policy.run', side_effect=Halt('unavailable')):
            with self.assertRaisesRegex(Halt, 'provider unavailable'):
                self.factory.lane(self.card, 'review', self.root, self.head)
        self.assertEqual(self.processes.lanes, [])

    def test_interruption_keeps_spending_and_does_not_fall_back_to_old_receipt(self):
        self.processes.interrupt = True
        for _ in range(2):
            with self.assertRaises(Stopped):
                self.factory.lane(self.card, 'review', self.root, self.head)
        with self.assertRaisesRegex(Halt, 'budget exhausted'):
            self.factory.lane(self.card, 'review', self.root, self.head)
        record = self.factory.record(self.card)
        self.assertEqual(len(record['worker_output_budget']['review']['dispatches']), 2)
        self.assertNotIn('review', record.get('receipts', {}))

    def test_duplicate_native_dispatch_is_serialized_and_reuses_valid_proof(self):
        errors = []
        def invoke():
            try:
                self.factory.lane(self.card, 'review', self.root, self.head)
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=invoke) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(self.processes.lanes, ['review'])
        self.assertEqual(len(self.factory.record(self.card)['worker_output_budget']['review']['dispatches']), 1)

    def test_candidate_base_config_task_raw_output_and_age_drift_invalidate_proof(self):
        self.factory.lane(self.card, 'review', self.root, self.head)
        proof = copy.deepcopy(self.factory.record(self.card)['receipts']['review'])
        for head, base in [('0' * 40, self.head), (self.head, '0' * 40)]:
            self.assertFalse(policy.proof_valid(self.factory, self.card, self.root, proof, head, base))
        for key in ('config', 'task', 'policy'):
            changed = copy.deepcopy(proof)
            changed['result']['scope'][key] = 'changed'
            self.assertFalse(policy.proof_valid(self.factory, self.card, self.root, changed, self.head, self.head))
        changed = copy.deepcopy(proof)
        changed['time'] = '2000-01-01T00:00:00+00:00'
        self.assertFalse(policy.proof_valid(self.factory, self.card, self.root, changed, self.head, self.head))
        for key in ('log', 'native_message', 'native_rollout'):
            with self.subTest(tampered=key):
                path = Path(proof[key])
                before = path.read_bytes()
                path.write_text('tampered')
                self.assertFalse(policy.proof_valid(self.factory, self.card, self.root, proof, self.head, self.head))
                path.write_bytes(before)

    def test_final_fresh_verification_does_not_destroy_valid_independent_review(self):
        for lane in ('qa', 'security', 'review'):
            self.factory.lane(self.card, lane, self.root, self.head)
        self.assertTrue(self.factory.pinned(self.card, self.head, self.head))
        self.factory.verify(self.card, self.root, self.head)
        self.assertTrue(self.factory.pinned(self.card, self.head, self.head))

    def test_reviewer_escalates_and_builder_cannot_remove_obligations(self):
        self.processes.mutate = lambda r: r.update(escalate=['security', 'human'])
        self.factory.lane(self.card, 'review', self.root, self.head)
        record = self.factory.record(self.card)
        self.assertIn('human', record['review_route']['obligations'])
        record['review_route']['obligations'] = ['review']
        self.assertFalse(policy.route_valid(self.factory, self.card, self.root, self.head, self.head))

    def test_import_incomplete_cloud_review_explains_gap_and_dispatches_native(self):
        path = self.root / '.sfc/cloud.json'
        write_json(path, {'state': 'PENDING', 'commit_id': self.head, 'body': 'No P0/P1 findings'})
        # Test import matching directly; configuration mutation would invalidate all proof.
        self.factory.c['review_policy']['imports'] = [{'path': str(path)}]
        receipts, gaps = policy.imported(self.factory, self.card, self.root, self.head, self.head)
        self.assertEqual(receipts, [])
        self.assertIn('completion', gaps[0])

    def test_no_promotion_or_fake_evidence_retains_baseline(self):
        self.factory.c['review_policy']['mode'] = 'risk-based'
        ok, reason = policy.promoted(self.factory)
        self.assertFalse(ok)
        self.assertIn('baseline retained', reason)

    def test_compatible_native_import_reuses_exact_proof_without_dispatch(self):
        path = self.root / '.sfc/import.json'
        config = copy.deepcopy(self.config)
        config['review_policy']['imports'] = [{'path': str(path)}]
        f = Factory(self.root, config, self.factory.gh, self.processes)
        f.run_dir = self.root / '.sfc/runs/import'
        f.verify(self.card, self.root, self.head)
        f.update(self.card, review_route=policy.plan(f, self.card, self.root, self.head))
        f.lane(self.card, 'review', self.root, self.head)
        receipt = f.record(self.card)['receipts'].pop('review')
        write_json(path, receipt)
        self.assertEqual(f.lane(self.card, 'review', self.root, self.head)['verdict'], 'pass')
        self.assertEqual(self.processes.lanes, ['review'])
        reused = f.record(self.card)['receipts']['review']
        self.assertEqual(reused['import_sha256'], checksum(path))
        write_json(path, {'changed': True})
        self.assertFalse(policy.proof_valid(f, self.card, self.root, reused, self.head, self.head))

    def test_github_review_requires_exact_bot_completion_and_live_readback(self):
        path = self.root / '.sfc/cloud-import.json'
        config = copy.deepcopy(self.config)
        config['review_policy'].update(imports=[{'path': str(path)}], github_reviewer_ids=[42])
        f = Factory(self.root, config, self.factory.gh, self.processes)
        f.run_dir = self.root / '.sfc/runs/cloud'
        f.verify(self.card, self.root, self.head)
        f.update(self.card, review_route=policy.plan(f, self.card, self.root, self.head))
        f.lane(self.card, 'review', self.root, self.head)
        receipt = f.record(self.card)['receipts'].pop('review')
        cloud_log = self.root / '.sfc/raw-cloud.json'
        cloud = {'id': 5, 'user': {'id': 42, 'type': 'Bot'}, 'commit_id': self.head,
                 'submitted_at': receipt['time'], 'body': json.dumps(receipt['result']), 'state': 'PENDING'}
        receipt.update(provider={'provider': 'github-native', 'pr': 101, 'review_id': 5}, log=str(cloud_log))
        for state in ('PENDING', 'COMMENTED'):
            cloud['state'] = state
            write_json(cloud_log, cloud)
            receipt['log_sha256'] = checksum(cloud_log)
            write_json(path, receipt)
            with patch.object(f.gh, 'call', side_effect=[cloud, [[]]]) as get:
                imports, gaps = policy.imported(f, self.card, self.root, self.head, self.head)
                if state == 'PENDING':
                    self.assertEqual(imports, [])
                    self.assertTrue(gaps)
                    get.assert_not_called()
                else:
                    imported, = imports
                    self.assertEqual(imported['provider']['provider'], 'github-native')
                    self.assertEqual([call.args for call in get.call_args_list], [
                        ('api', 'repos/example/app/pulls/101/reviews/5'),
                        ('api', 'repos/example/app/pulls/101/reviews/5/comments?per_page=100', '--paginate', '--slurp')])
        for comments in ([[{'id': 7, 'body': '[P1] Unresolved regression'}]], [], {}, Halt('read failed')):
            with self.subTest(comments=comments), patch.object(f.gh, 'call', side_effect=[cloud, comments]):
                self.assertFalse(policy.proof_valid(f, self.card, self.root, imported, self.head, self.head))
        with patch.object(f.gh, 'call', return_value={**cloud, 'state': 'DISMISSED'}):
            self.assertFalse(policy.proof_valid(f, self.card, self.root, imported, self.head, self.head))

    def test_verified_cloud_findings_override_cached_pass_and_earlier_passing_import(self):
        path = self.root / '.sfc/cloud-import.json'
        failing_path = self.root / '.sfc/failing-cloud-import.json'
        config = copy.deepcopy(self.config)
        config['review_policy'].update(imports=[{'path': str(path)}, {'path': str(failing_path)}],
                                       github_reviewer_ids=[42])
        f = Factory(self.root, config, self.factory.gh, self.processes)
        f.run_dir = self.root / '.sfc/runs/cloud-findings'
        f.verify(self.card, self.root, self.head)
        f.update(self.card, base=self.head, worktree=str(self.root), review_route=policy.plan(f, self.card, self.root, self.head))
        for lane in ('qa', 'security', 'review'):
            f.lane(self.card, lane, self.root, self.head)
        cached = f.record(self.card)['receipts']['review']
        finding = {'file': 'value.txt', 'line': 1, 'severity': 'high', 'message': 'Preserve the behavior'}
        negative = {**cached['result'], 'verdict': 'fail', 'findings': [finding]}
        passing_cloud = {'id': 5, 'user': {'id': 42, 'type': 'Bot'}, 'commit_id': self.head,
                         'submitted_at': cached['time'], 'state': 'COMMENTED', 'body': json.dumps(cached['result'])}
        failing_cloud = {**passing_cloud, 'id': 6, 'state': 'CHANGES_REQUESTED', 'body': json.dumps(negative)}
        for inbox, remote, result in ((path, passing_cloud, cached['result']),
                                      (failing_path, failing_cloud, negative)):
            log = inbox.with_suffix('.raw.json')
            result_path = inbox.with_suffix('.result.json')
            write_json(log, remote)
            write_json(result_path, result)
            write_json(inbox, {**cached, 'provider': {'provider': 'github-native', 'pr': 101,
                'review_id': remote['id']}, 'log': str(log), 'log_sha256': checksum(log),
                'result': result, 'result_path': str(result_path), 'result_sha256': checksum(result_path)})
        lanes = self.processes.lanes[:]
        comments = [[{'id': 7, 'body': '[P1] Preserve the behavior'}]]
        with patch.object(f.gh, 'call', side_effect=[passing_cloud, [[]], failing_cloud, comments]) as get:
            self.assertEqual(f.lane(self.card, 'review', self.root, self.head), negative)
            self.assertEqual(get.call_count, 4)
        self.assertEqual(self.processes.lanes, lanes)
        self.assertEqual(f.record(self.card)['receipts']['review']['import_path'], str(failing_path))
        with patch.object(f.gh, 'call', side_effect=[failing_cloud, comments]):
            self.assertEqual(f.lane(self.card, 'review', self.root, self.head), negative)
        self.assertEqual(self.processes.lanes, lanes)
        f.record(self.card)['receipts']['review'] = cached
        self.assertTrue(f.pinned(self.card, self.head, self.head))
        with patch.object(f.gh, 'call', side_effect=[passing_cloud, [[]], failing_cloud, comments]), \
             patch.object(f, 'assert_remote', return_value={'number': 101}), \
             patch.object(f, 'fetch', return_value=self.head), patch.object(f, 'fail') as fail:
            f.review(self.card, self.root)
        fail.assert_called_once_with(self.card, negative)
        self.assertEqual(self.processes.lanes, lanes)

    def test_github_review_with_missing_fields_is_not_reused(self):
        path = self.root / '.sfc/cloud-import.json'
        config = copy.deepcopy(self.config)
        config['review_policy'].update(imports=[{'path': str(path)}], github_reviewer_ids=[42])
        f = Factory(self.root, config, self.factory.gh, self.processes)
        f.run_dir = self.root / '.sfc/runs/cloud-malformed'
        f.verify(self.card, self.root, self.head)
        f.lane(self.card, 'review', self.root, self.head)
        receipt = f.record(self.card)['receipts']['review']
        cloud_log = self.root / '.sfc/raw-cloud.json'
        cloud = {'id': 5, 'user': {'id': 42, 'type': 'Bot'}, 'commit_id': self.head,
                 'submitted_at': receipt['time'], 'state': 'COMMENTED'}
        receipt = {**receipt, 'provider': {'provider': 'github-native', 'pr': 101, 'review_id': 5},
                   'log': str(cloud_log)}
        for key in ('summary', 'evidence'):
            malformed = {k: v for k, v in receipt['result'].items() if k != key}
            cloud.update(body=json.dumps(malformed))
            write_json(cloud_log, cloud)
            write_json(receipt['result_path'], malformed)
            envelope = {**receipt, 'result': malformed, 'result_sha256': checksum(receipt['result_path']),
                        'log_sha256': checksum(cloud_log)}
            write_json(path, envelope)
            with self.subTest(missing=key), patch.object(f.gh, 'call', return_value=cloud) as get:
                self.assertEqual(policy.imported(f, self.card, self.root, self.head, self.head)[0], [])
                get.assert_not_called()

    def test_invalid_configuration_rejected(self):
        for p in ({'providers': {'review': 'unavailable'}}, {'mandatory': ['skip-security']},
                  {'risk_rules': {'unknown-category': []}}, {'routine_paths': 'all'},
                  {'max_age_seconds': True}, {'mode': 'risk-based'}, {'imports': [{}]}):
            with self.subTest(policy=p), self.assertRaises(Halt):
                validate_config({**BASE_CONFIG, 'review_policy': p})

    def test_multiline_acceptance_criteria_preserves_full_text(self):
        card = issue(body='## Acceptance criteria\n- [ ] first\n  continuation\n- [x] second\n## Notes\nignored')
        values = policy.criteria(card)
        self.assertEqual([v['text'] for v in values], ['first\ncontinuation', 'second'])
        self.assertNotEqual(values[0]['id'], values[1]['id'])
        self.assertEqual(policy.criteria({**card, 'body': card['body'].replace('\n', '\r\n')}), values)
        with self.assertRaisesRegex(Halt, 'observable'):
            policy.criteria(issue(body='## Acceptance criteria\n- [ ]'))

    def test_review_freshness_requires_evidence_valid_across_merge_window(self):
        self.config['review_policy']['max_age_seconds'] = 60
        second = dt.datetime.now(dt.timezone.utc).replace(microsecond=0) - dt.timedelta(minutes=2)
        issued = second + dt.timedelta(milliseconds=100)
        opened = second + dt.timedelta(milliseconds=250)
        end = second + dt.timedelta(seconds=1)
        self.assertTrue(policy.receipt_fresh(self.config, {'time': issued.isoformat()},
                                            at=end.isoformat(), since=opened.isoformat()))
        self.assertFalse(policy.receipt_fresh(self.config, {'time': issued.isoformat()},
                                             at=end.isoformat(), since=second.isoformat()))
        expired = second - dt.timedelta(seconds=59, milliseconds=500)
        self.assertFalse(policy.receipt_fresh(self.config, {'time': expired.isoformat()},
                                             at=end.isoformat(), since=opened.isoformat()))

    def test_inherited_model_settings_keep_baseline(self):
        self.factory.c['review_policy'].update(mode='risk-based', promotion={'path': 'evidence'})
        for key in ('model', 'reasoning_effort', 'service_tier'):
            config = {**self.factory.c, 'model': 'fixture', 'reasoning_effort': 'medium', 'service_tier': 'fast'}
            config[key] = None
            with self.subTest(key=key), patch.object(self.factory, 'c', config), \
                 patch.object(policy, 'load_ref') as read:
                allowed, reason = policy.promoted(self.factory)
                self.assertFalse(allowed)
                self.assertIn('must be explicit', reason)
                read.assert_not_called()

    def test_promotion_binding_covers_project_model_and_check_changes(self):
        before = policy.promotion_binding(self.config)
        for change in ({'model': 'different'}, {'verify_commands': ['different check']}):
            self.assertNotEqual(policy.promotion_binding({**self.config, **change}), before)


class PolicyFlowTests(unittest.TestCase):
    setUp = merge_fixture.LocalMergeTests.setUp
    tearDown = merge_fixture.LocalMergeTests.tearDown
    prepare_review = merge_fixture.LocalMergeTests.prepare_review

    def configure(self, mode='baseline', *, inboxes=()):
        self.c['review_policy'] = {'mode': mode, 'providers': {'review': 'codex-native'},
                                   'routine_paths': ['value.txt']}
        if inboxes:
            self.c['review_policy'].update(imports=[{'path': str(path)} for path in inboxes],
                                           github_reviewer_ids=[42])
        self.proc = PolicyProcesses(self.root / '.sfc/stop')
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        sessions = patch('sfc_lib.review_policy.native_sessions', return_value=self.root / '.sfc/sessions')
        sessions.start()
        self.addCleanup(sessions.stop)

    @patch.object(policy, 'run', new=capabilities)
    def test_all_verified_import_escalations_apply_to_cached_or_first_passing_review(self):
        first, second = (self.root / '.sfc' / name for name in ('first.json', 'second.json'))
        self.configure(inboxes=[first, second])
        card, wt = self.prepare_review()
        f, base = self.factory, self.factory.record(card)['base']
        f.lane(card, 'review', wt, base)
        r = f.record(card)
        cached = r['receipts']['review']
        head = cached['head']
        original_receipts = r['receipts'].copy()
        initial_route = copy.deepcopy(r['review_route'])
        first_report = {**cached['result'], 'escalate': ['security']}
        first_cloud = write_github_review(first, cached, first_report, 5)
        escalation = copy.deepcopy(cached['result'])
        escalation['escalate'] = ['security', 'human']
        escalation['coverage'][0].update(status='qa_required', evidence=[])
        second_cloud = write_github_review(second, cached, escalation, 6)
        qa_dependency = checksum(second.with_suffix('.result.json'))
        security_dependency = digest(sorted([qa_dependency, checksum(first.with_suffix('.result.json'))]))

        def readback(*args):
            if '/comments?' in args[1]:
                return [[]]
            return {5: first_cloud, 6: second_cloud}[int(args[1].rsplit('/', 1)[-1])]

        for use_cache in (True, False):
            with self.subTest(use_cache=use_cache), patch.object(f.gh, 'call', side_effect=readback):
                f.update(card, receipts=original_receipts.copy(), review_route=copy.deepcopy(initial_route))
                if not use_cache:
                    r['receipts'].pop('review')
                self.assertEqual(f.lane(card, 'review', wt, base), cached['result'] if use_cache else first_report)
                self.assertEqual(r['receipts']['review'].get('import_path'), None if use_cache else str(first))
                self.assertTrue(policy.human_required(f, card))
                self.assertFalse(f.pinned(card, head, base))
                self.assertTrue(f.finish_review_obligations(card, wt, head, base))
                self.assertTrue(f.pinned(card, head, base))
                for lane, expected, prompt in zip(('qa', 'security'), (qa_dependency, security_dependency),
                                                  self.proc.prompts[-2:]):
                    self.assertIn('Imported review escalation reports', prompt)
                    self.assertEqual(r['receipts'][lane]['result']['scope']['review_dependency'], expected)
        second.unlink()
        with patch.object(f.gh, 'call', side_effect=readback):
            f.update(card, review_route=policy.plan(f, card, wt, base))
            self.assertEqual(f.lane(card, 'review', wt, base), first_report)
            self.assertTrue(policy.human_required(f, card))
            self.assertTrue(f.pinned(card, head, base))
        changed_task = {**card, 'title': 'New acceptance scope'}
        new_route = policy.plan(f, changed_task, wt, base)
        self.assertNotIn('import_escalations', new_route)
        self.assertNotIn('human', new_route['obligations'])
        self.assertEqual(self.proc.lanes, ['build', 'qa', 'security', 'review', 'qa', 'security', 'qa', 'security'])

    @patch.object(policy, 'run', new=capabilities)
    def check_late_import(self, mode, *, adverse=False, escalations=(), human_label=False):
        self.c['merge_mode'] = mode
        inbox = self.root / '.sfc/late.json'
        self.configure(inboxes=[inbox])
        f = self.factory
        f.allow_merge = True
        card, wt = self.prepare_review()
        r = f.record(card)
        base = r['base']
        f.lane(card, 'review', wt, base)
        cached = r['receipts']['review']
        report = copy.deepcopy(cached['result'])
        if adverse:
            report.update(verdict='fail', findings=[{'file': 'value.txt', 'line': 1, 'severity': 'high',
                                                     'message': 'P1 regression in late review'}])
        else:
            report['escalate'] = list(escalations)
        comments = [[{'id': 7, 'body': '[P1] Regression'}]] if adverse else [[]]
        original_verify, original_call = f.verify, f.gh.call
        cloud = None

        def verify(card, scratch, base):
            nonlocal cloud
            result = original_verify(card, scratch, base)
            self.assertNotEqual(scratch, wt)
            cloud = write_github_review(inbox, cached, report, 6, pr=r['pr'])
            return result

        def readback(*args):
            if '/reviews/6' in args[1]:
                if '/comments?' in args[1]:
                    if human_label:
                        self.gh.items[0]['labels'].append('human-only')
                    return comments
                return cloud
            return original_call(*args)

        with patch.object(f, 'verify', side_effect=verify) as check, patch.object(f.gh, 'call', side_effect=readback):
            f.review(card, wt)
            check.assert_called_once()
            self.assertEqual(self.proc.lanes, ['build', 'qa', 'security', 'review'])
            if human_label:
                self.assertEqual(card['status'], 'QA')
                self.assertEqual(self.gh.merges + self.gh.attempts, [])
                self.assertNotIn('local_merge', r)
                return
            if not adverse and not escalations:
                self.assertEqual(card['status'], 'Done')
                self.assertEqual(len(f.gh.merges) + len(f.gh.attempts), 1)
                return
            self.assertEqual(len(f.gh.merges) + len(f.gh.attempts), 0)
            self.assertNotIn('local_merge', r)
            if adverse:
                self.assertEqual(card['status'], 'Building')
                self.assertEqual(json.loads(r['feedback']), report)
            else:
                self.assertEqual(card['status'], 'Review')
                self.assertTrue(policy.human_required(f, card))
                f.review(card, wt)
                self.assertEqual(card['status'], 'Blocked')
                self.assertEqual(self.proc.lanes, ['build', 'qa', 'security', 'review', 'security'])
                self.assertEqual(len(f.gh.merges) + len(f.gh.attempts), 0)

    def test_late_adverse_review_prevents_github_merge(self):
        self.check_late_import('github-protected', adverse=True)

    def test_late_adverse_review_prevents_atomic_ref_write(self):
        self.check_late_import('local-verification', adverse=True)

    def test_late_escalation_requires_specialist_and_human_review(self):
        self.check_late_import('local-verification', escalations=['security', 'human'])

    def test_late_passing_review_without_new_obligations_preserves_merge(self):
        self.check_late_import('github-protected')

    def test_human_label_during_final_review_readback_prevents_github_merge(self):
        self.check_late_import('github-protected', human_label=True)

    def test_human_label_during_final_review_readback_prevents_atomic_ref_write(self):
        self.check_late_import('local-verification', human_label=True)

    def test_native_baseline_preserves_independent_lanes_and_final_verification(self):
        self.configure()
        with patch('sfc_lib.review_policy.run', side_effect=capabilities):
            self.assertEqual(self.factory.run(allow_merge=True)['outcome'], 'drained')
        self.assertEqual(self.proc.lanes, ['build', 'qa', 'security', 'review'])
        self.assertEqual(len([v for v in self.proc.events if v[0] == 'check']), 2)
        self.assertEqual(len(self.gh.attempts), 1)

    def cohort_factory(self):
        self.configure('risk-based')
        self.c.update(model='fixture', reasoning_effort='high', service_tier='fast', review_policy={
            'mode': 'risk-based', 'providers': {'review': 'codex-native'}, 'cohort': 'a' * 64,
            'behavioral_contract': {'paths': ['value.txt'], 'check_ids': [1], 'instructions': {}}})
        return Factory(self.root, self.c, self.gh, self.proc)

    def test_missing_cohort_dispatches_ordinary_baseline_review(self):
        f = self.cohort_factory()
        with patch.object(review_cohort, 'read_certificate', side_effect=ValueError('certificate changed')), \
             patch.object(policy, 'run', side_effect=capabilities), \
             patch.object(policy, 'native_command') as native:
            self.assertEqual(f.run(allow_merge=True)['outcome'], 'drained')
        native.assert_not_called()
        record = f.record(self.gh.items[0])
        self.assertEqual(self.proc.lanes, ['build', 'qa', 'security', 'review'])
        self.assertEqual(record['review_route']['providers'], dict.fromkeys(policy.LANES, 'codex-exec'))
        self.assertEqual(record['receipts']['review']['provider']['provider'], 'codex-exec')
        self.assertEqual(len(self.gh.attempts), 1)

    def test_qualified_cohort_dispatches_native_review_and_cli_drift_invalidates_route(self):
        f = self.cohort_factory()
        card = self.gh.items[0]
        with patch.object(review_cohort, 'qualify', return_value=(True, 'test qualification')), \
             patch.object(policy, 'run', side_effect=capabilities), \
             patch.object(policy, 'native_command', wraps=policy.native_command) as native:
            f.run()
        native.assert_called_once()
        record = f.record(card)
        self.assertEqual(self.proc.lanes, ['build', 'qa', 'review'])
        self.assertEqual(record['receipts']['review']['provider']['provider'], 'codex-native')
        wt = Path(record['worktree'])
        with patch.object(review_cohort, 'qualify', return_value=(False, 'CLI differs')):
            self.assertFalse(policy.route_valid(f, card, wt, record['head'], record['base']))
            f.review(card, wt)
        self.assertEqual(card['status'], 'QA')
        self.assertEqual(self.proc.lanes, ['build', 'qa', 'review'])

    def test_unattested_text_used_as_a_runtime_input_keeps_all_review_lanes(self):
        self.configure('risk-based')
        f = self.factory
        card = self.gh.items[0]
        # An extension and innocuous text cannot establish how a project
        # interprets a file. Exercise the live policy with a promoted profile.
        with patch.object(policy, 'run', side_effect=capabilities), \
             patch.object(policy, 'promoted', return_value=(True, 'test promotion')):
            f.run(allow_merge=True)
        self.assertEqual(self.proc.lanes, ['build', 'qa', 'security', 'review'])
        self.assertEqual(set(f.record(card)['review_route']['obligations']),
                         {'qa', 'security', 'review', 'human'})
        self.assertEqual(self.gh.attempts, [])

    def test_local_merge_ci_wait_retains_review_route(self):
        from sfc_lib.local_merge import LocalMerge

        self.configure()
        f = self.factory
        with patch.object(policy, 'run', side_effect=capabilities):
            card, wt = self.prepare_review()
            r = f.record(card)
            for lane in ('qa', 'security', 'review'):
                f.lane(card, lane, wt, r['base'])
            f.allow_merge = True
            pr = f.assert_remote(card, wt)
            for checks, explanation in (([{'state': 'PENDING'}], 'GitHub checks are still pending'),
                                        ([{'state': 'FAILURE'}], 'A GitHub check failed'),
                                        ([], 'No GitHub checks were reported')):
                with self.subTest(checks=checks), patch.object(self.gh, 'local_merge_checks', return_value=checks):
                    self.assertFalse(LocalMerge(f).eligible(card, wt, pr, r['head'], r['base']))
                    state, detail = self.gh.activities[card['item_id']]
                    self.assertIn('Waiting', state)
                    self.assertIn(explanation, detail)
                    self.assertIn('Review route: QA, security, then native Codex review', detail)
        self.assertEqual(f.record(card)['activity']['action'], 'ci')
        self.assertEqual(self.gh.attempts, [])

    def test_promoted_routine_route_uses_one_review_and_preserves_ci_gate(self):
        self.configure('risk-based')
        self.gh.ci = []
        with patch('sfc_lib.review_policy.run', side_effect=capabilities), \
             patch.object(policy, 'routine_attested', return_value=True), \
             patch('sfc_lib.review_policy.promoted', return_value=(True, 'test promotion')):
            self.factory.run(allow_merge=True)
        self.assertEqual(self.proc.lanes, ['build', 'review'])
        self.assertEqual(self.gh.attempts, [])

    def test_promoted_security_label_still_runs_specialist_review(self):
        self.configure('risk-based')
        card = self.gh.items[0]
        card['labels'] = ['Security']
        with patch('sfc_lib.review_policy.run', side_effect=capabilities), \
             patch('sfc_lib.review_policy.promoted', return_value=(True, 'test promotion')):
            self.factory.run(allow_merge=True)
        self.assertEqual(self.proc.lanes, ['build', 'qa', 'security', 'review'])
        self.assertIn('human', self.factory.record(card)['review_route']['obligations'])
        self.assertEqual(self.gh.attempts, [])

    def test_promoted_review_can_require_qa_security_and_human_handoff(self):
        self.configure('risk-based')
        card = self.gh.items[0]
        command = [sys.executable, '-B', '-c', 'from pathlib import Path; '
                   'assert Path("value.txt").read_text() == "fixed\\n"']
        command_text = shlex.join(command)

        def escalate(result):
            if self.proc.lanes[-1] == 'review':
                result['coverage'][0].update(status='qa_required', evidence=[])
                result['escalate'] = ['security', 'human']
            elif self.proc.lanes[-1] == 'qa':
                path = self.factory.record(card)['worktree']
                executed = subprocess.run(command, cwd=path, text=True, capture_output=True, timeout=5)
                self.assertEqual(executed.returncode, 0, executed.stderr)
                item = {'type': 'command_execution', 'id': 'focused', 'command': command_text}
                self.proc.qa_events = [
                    {'type': 'item.started', 'item': {**item, 'status': 'in_progress'}},
                    {'type': 'item.completed', 'item': {**item, 'status': 'completed',
                        'exit_code': executed.returncode, 'aggregated_output': executed.stdout}}]
                result['coverage'][0].update(evidence=[{'kind': 'qa-command', 'id': command_text, 'sha256': ''}])

        self.proc.mutate = escalate
        snapshots = []
        readback = self.gh.activity_snapshot

        def capture(card):
            scope, values = readback(card)
            snapshots.append(values[1])
            return scope, values

        with patch('sfc_lib.review_policy.run', side_effect=capabilities), \
             patch.object(policy, 'routine_attested', return_value=True), \
             patch('sfc_lib.review_policy.promoted', return_value=(True, 'test promotion')), \
             patch.object(self.gh, 'activity_snapshot', side_effect=capture):
            self.factory.run(allow_merge=True)
            r = self.factory.record(card)
            self.assertTrue(self.factory.pinned(card, r['head'], r['base']))
        self.assertEqual(self.proc.lanes, ['build', 'review', 'qa', 'security'])
        qa = next(v for v in snapshots if v.startswith('QA worker'))
        security = next(v for v in snapshots if v.startswith('Security worker'))
        for observed, pending in ((qa, 'QA, Security, Human decision'), (security, 'Security, Human decision')):
            self.assertIn('Review route: native Codex review', observed)
            self.assertIn('Pending: ' + pending, observed)
            self.assertNotIn(command_text, observed)
        final = readback(card)[1][1]
        self.assertIn('Review route: native Codex review', final)
        self.assertIn('Pending: Human decision', final)
        self.assertEqual(r['receipts']['qa']['result']['scope']['review_dependency'],
                         r['receipts']['review']['result_sha256'])
        self.assertEqual(card['status'], 'Blocked')
        self.assertEqual(self.gh.attempts, [])

    def test_qa_only_replaces_earlier_qa_with_pinned_targeted_qa(self):
        self.configure()
        self.gh.items[0]['labels'] = ['qa']

        def request_qa(result):
            if self.proc.lanes[-1] == 'review':
                result['coverage'][0].update(status='qa_required', evidence=[])

        self.proc.mutate = request_qa
        with patch('sfc_lib.review_policy.run', side_effect=capabilities):
            self.assertEqual(self.factory.run()['outcome'], 'drained')
        self.assertEqual(self.proc.lanes, ['qa', 'security', 'review', 'qa'])
        self.assertEqual(self.gh.items[0]['status'], 'Done')
        receipts = self.factory.record(self.gh.items[0])['receipts']
        self.assertEqual(receipts['qa']['result']['scope']['review_dependency'],
                         receipts['review']['result_sha256'])

    def test_qa_only_on_unchanged_base_with_empty_policy_completes(self):
        self.c['review_policy'] = {}
        self.proc = PolicyProcesses(self.root / '.sfc/stop')
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        card = self.gh.items[0]
        card['labels'] = ['qa']
        with patch('sfc_lib.review_policy.run', side_effect=capabilities):
            self.assertEqual(self.factory.run()['outcome'], 'drained')
        self.assertEqual(self.proc.lanes, ['qa', 'security', 'review'])
        self.assertEqual(card['status'], 'Done')
        route = self.factory.record(card)['review_route']
        self.assertEqual(route['risk'], [])
        self.assertNotIn('human', route['obligations'])

    def test_security_reassesses_targeted_qa_escalation(self):
        self.configure()
        self.gh.items[0]['labels'] = ['qa']

        def escalate(result):
            if self.proc.lanes[-1] == 'review':
                result['coverage'][0].update(status='qa_required', evidence=[])
            elif self.proc.lanes[-1] == 'qa' and self.proc.lanes.count('qa') == 2:
                result.update(escalate=['security'], summary='Targeted QA found a specialist concern')

        self.proc.mutate = escalate
        with patch('sfc_lib.review_policy.run', side_effect=capabilities):
            self.assertEqual(self.factory.run()['outcome'], 'drained')
        self.assertEqual(self.proc.lanes, ['qa', 'security', 'review', 'qa', 'security'])
        card = self.gh.items[0]
        self.assertEqual(card['status'], 'Done')
        receipts = self.factory.record(card)['receipts']
        self.assertEqual(receipts['security']['result']['scope']['review_dependency'],
                         receipts['qa']['result_sha256'])
        self.assertIn('Targeted QA found a specialist concern', self.proc.prompts[-1])

    def test_reciprocal_specialist_requests_block_after_bounded_refresh(self):
        self.configure()
        card = self.gh.items[0]
        card['labels'] = ['qa']

        def escalate(result):
            lane = self.proc.lanes[-1]
            if lane == 'review':
                result['escalate'] = ['qa']
            elif self.proc.lanes.count(lane) == 2:
                result['escalate'] = ['security' if lane == 'qa' else 'qa']

        self.proc.mutate = escalate
        with patch('sfc_lib.review_policy.run', side_effect=capabilities):
            self.factory.run()
        self.assertEqual(self.proc.lanes, ['qa', 'security', 'review', 'qa', 'security'])
        self.assertEqual(card['status'], 'Blocked')
        self.assertFalse(policy.specialist_dependencies_complete(self.factory, card))
        self.assertEqual(self.factory.record(card)['activity']['action'], 'specialist_gap')

    def test_qa_only_explicit_human_obligation_waits_for_assessment(self):
        self.configure()
        self.c['review_policy']['mandatory'] = ['human']
        self.gh.items[0]['labels'] = ['qa']
        with patch('sfc_lib.review_policy.run', side_effect=capabilities):
            self.factory.run()
        card = self.gh.items[0]
        self.assertEqual(self.proc.lanes, ['qa', 'security', 'review'])
        self.assertEqual(card['status'], 'Blocked')
        route = self.factory.record(card)['review_route']
        self.assertEqual(route['next_actions'], ['Human QA-only assessment required'])

    def test_replaced_local_review_keeps_original_escalations(self):
        self.configure()
        with patch.object(policy, 'run', side_effect=capabilities):
            card, wt = self.prepare_review()
            f = self.factory
            base = f.record(card)['base']
            self.proc.mutate = lambda report: report.update(escalate=['security', 'human'])
            f.lane(card, 'review', wt, base)
            receipt = f.record(card)['receipts']['review']
            self.proc.mutate = lambda report: None
            f._lane(card, 'review', wt, base)
            f.update(card, review_route=policy.plan(f, card, wt, base))
            self.assertTrue(policy.human_required(f, card))
            self.assertTrue(f.finish_review_obligations(card, wt, receipt['head'], base))
            changed_task = {**card, 'title': 'New acceptance scope'}
            self.assertNotIn('human', policy.plan(f, changed_task, wt, base)['obligations'])
        security = f.record(card)['receipts']['security']
        self.assertEqual(security['result']['scope']['review_dependency'], receipt['result_sha256'])
        self.assertIn(receipt['result_sha256'], self.proc.prompts[-1])

    def test_passing_review_does_not_resolve_initial_qa_gap(self):
        self.configure()
        card = self.gh.items[0]
        card['labels'] = ['qa']

        def require_targeted_qa(result):
            if self.proc.lanes == ['qa']:
                result['coverage'][0].update(status='qa_required', evidence=[])

        self.proc.mutate = require_targeted_qa
        with patch.object(policy, 'run', side_effect=capabilities):
            self.assertEqual(self.factory.run()['outcome'], 'drained')
        self.assertEqual(self.proc.lanes, ['qa', 'security', 'review', 'qa'])
        self.assertEqual(card['status'], 'Done')
        self.assertTrue(self.factory.record(card)['receipts']['qa']['result']['scope']['review_dependency'])

    def test_targeted_qa_must_resolve_the_disputed_criterion(self):
        self.configure()
        card = self.gh.items[0]
        card.update(labels=['qa'], body='## Acceptance criteria\n- First behavior\n- Second behavior')

        def leave_one_gap(result):
            if self.proc.lanes[-1] == 'qa':
                result['coverage'][0].update(status='qa_required', evidence=[])

        self.proc.mutate = leave_one_gap
        with patch.object(policy, 'run', side_effect=capabilities):
            self.assertEqual(self.factory.run()['outcome'], 'waiting')
        self.assertEqual(self.proc.lanes, ['qa', 'security', 'review', 'qa'])
        self.assertEqual(card['status'], 'Blocked')
        qa = self.factory.record(card)['receipts']['qa']['result']
        self.assertTrue(qa['scope']['review_dependency'])
        self.assertEqual([row['status'] for row in qa['coverage']], ['qa_required', 'covered'])
        self.assertFalse(policy.acceptance_complete(self.factory, card))

    def test_github_protected_merge_rechecks_age_at_dispatch(self):
        self.configure()
        self.c.update(merge_mode='github-protected')
        self.c['review_policy']['max_age_seconds'] = 60
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        f = self.factory
        actual = dt.datetime

        class Clock(actual):
            elapsed = dt.timedelta()

            @classmethod
            def now(cls, tz=None):
                return actual.now(tz) + cls.elapsed

        def final_read(*args):
            Clock.elapsed = dt.timedelta(minutes=2)
            return True

        with patch.object(policy, 'run', side_effect=capabilities), patch.object(policy.dt, 'datetime', Clock), \
             patch.object(f, 'recheck_merge_reviews', side_effect=final_read), \
             self.assertRaisesRegex(Halt, 'expired before GitHub merge'):
            f.run(allow_merge=True)
        self.assertEqual(self.gh.merges, [])

    def test_local_atomic_merge_rechecks_age_after_persisting_intent(self):
        self.configure()
        self.c['review_policy']['max_age_seconds'] = 60
        f = Factory(self.root, self.c, self.gh, self.proc)
        actual = dt.datetime

        class Clock(actual):
            elapsed = dt.timedelta()

            @classmethod
            def now(cls, tz=None):
                return actual.now(tz) + cls.elapsed

        event = f.event

        def expire_after_journal(card, detail):
            event(card, detail)
            if detail.startswith('atomic local merge requested'):
                Clock.elapsed = dt.timedelta(minutes=2)

        with patch.object(policy, 'run', side_effect=capabilities), patch.object(policy.dt, 'datetime', Clock), \
             patch.object(f, 'event', side_effect=expire_after_journal), \
             self.assertRaisesRegex(Halt, 'expired before atomic local merge dispatch'):
            f.run(allow_merge=True)
        self.assertEqual(self.gh.attempts, [])
        intent = f.record(self.gh.items[0])['local_merge']
        self.assertEqual(intent['phase'], 'intent')
        self.assertEqual(intent['review_validated_at'], intent['time'])

    def test_external_merge_recognizes_evidence_valid_at_github_merge_time(self):
        self.check_external_merge_time(250000, accepted=True)

    def test_external_merge_rejects_handoff_at_start_of_next_second(self):
        self.check_external_merge_time(1000000, accepted=False)

    def check_external_merge_time(self, handoff_microseconds, *, accepted):
        self.configure()
        self.c['review_policy']['max_age_seconds'] = 60
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        f = self.factory
        actual = dt.datetime

        class Clock(actual):
            @classmethod
            def now(cls, tz=None):
                return actual.now(tz) + dt.timedelta(minutes=2)

        with patch.object(policy, 'run', side_effect=capabilities):
            f.run()
        card = self.gh.items[0]
        r = f.record(card)
        head = r['head']
        handoff = actual.fromisoformat(r['reviewed_completion']['review_validated_at'])
        # GitHub reports the start of a second that may also contain a
        # microsecond-precision handoff. Receipts predate that second.
        same_second = handoff.replace(microsecond=0) + dt.timedelta(seconds=1)
        r['reviewed_completion']['review_validated_at'] = (
            same_second + dt.timedelta(microseconds=handoff_microseconds)).isoformat()
        f.save()
        merged = same_second.isoformat()
        git(self.remote, 'update-ref', 'refs/heads/main', head)
        self.gh.prs[r['pr']]['state'] = 'MERGED'
        card['state'] = 'CLOSED'
        self.gh.edit_snapshot = lambda snapshot: snapshot['pullRequest'].update(
            mergeCommit={'oid': head}, mergedAt=merged)

        with patch.object(policy.dt, 'datetime', Clock), patch.object(policy, 'run', side_effect=capabilities), \
             patch('sfc_lib.external_merge.now', side_effect=lambda: Clock.now(dt.timezone.utc).isoformat()):
            if not accepted:
                with self.assertRaisesRegex(Halt, 'verified merge time'):
                    f.run()
                self.assertEqual(card['status'], 'Blocked')
                return
            f.run()
        self.assertEqual(card['status'], 'Done')
        self.assertEqual(f.record(card)['external_completion']['merged_at'], merged)
        self.assertEqual(self.gh.merges + self.gh.attempts, [])

    def test_identical_handoff_preserves_original_validated_at(self):
        from sfc_lib.external_merge import ExternalMerge

        self.configure()
        f = self.factory
        with patch.object(policy, 'run', side_effect=capabilities):
            f.run()
        card = self.gh.items[0]
        r = f.record(card)
        first = copy.deepcopy(r['reviewed_completion'])
        card = {**card, '_pickup_scope': first['scope']}
        later = (dt.datetime.fromisoformat(first['review_validated_at']) + dt.timedelta(seconds=1)).isoformat()
        with patch('sfc_lib.external_merge.now', return_value=later), \
             patch.object(policy, 'fresh_receipts', return_value=True):
            ExternalMerge(f).remember(card, self.gh.pr(r['pr']), r['head'], r['base'], open_read_started=later)
            self.assertEqual(r['reviewed_completion'], first)
            r['verification'] = {**r['verification'], 'elapsed_seconds': 1.0}
            ExternalMerge(f).remember(card, self.gh.pr(r['pr']), r['head'], r['base'], open_read_started=later)
            self.assertEqual(r['reviewed_completion']['review_validated_at'], later)

    def test_acknowledged_atomic_merge_can_be_confirmed_after_review_expires(self):
        self.configure()
        self.c['review_policy']['max_age_seconds'] = 60
        self.factory = Factory(self.root, self.c, self.gh, self.proc)
        real_datetime = dt.datetime

        class Clock(real_datetime):
            offset = dt.timedelta()

            @classmethod
            def now(cls, tz=None):
                return real_datetime.now(tz) + cls.offset

        def expire_after_write(intent):
            Clock.offset = dt.timedelta(minutes=2)

        self.gh.after_write = expire_after_write
        with patch.object(policy.dt, 'datetime', Clock), patch.object(policy, 'run', side_effect=capabilities):
            self.assertEqual(self.factory.run(allow_merge=True)['outcome'], 'drained')
        card = self.gh.items[0]
        self.assertEqual(card['status'], 'Done')
        self.assertEqual(len(self.gh.attempts), 1)
        self.assertEqual(self.factory.record(card)['local_merge']['phase'], 'done')

    def test_atomic_merge_recovery_rejects_changed_frozen_review_bytes(self):
        self.configure()

        def change_evidence_after_write(intent):
            Path(intent['receipts']['review']['log']).write_text('changed review log\n')

        self.gh.after_write = change_evidence_after_write
        with patch.object(policy, 'run', side_effect=capabilities), self.assertRaisesRegex(Halt, 'evidence changed'):
            self.factory.run(allow_merge=True)
        card = self.gh.items[0]
        self.assertEqual(card['status'], 'Review')
        self.assertEqual(len(self.gh.attempts), 1)
        self.assertEqual(self.factory.record(card)['local_merge']['phase'], 'responded')

    def test_removed_policy_ignores_old_human_route_at_local_merge(self):
        card, wt = self.prepare_review()
        self.factory.update(card, review_route={'obligations': ['qa', 'security', 'review', 'human']})
        self.factory.allow_merge = True
        self.factory.review(card, wt)
        self.assertEqual(self.gh.items[0]['status'], 'Done')
        self.assertEqual(len(self.gh.attempts), 1)


if __name__ == '__main__':
    unittest.main()
