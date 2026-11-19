import copy
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
import unittest
from unittest.mock import patch

from benchmark_review import benchmark, real_arm, source_inspection_benchmark
from sfc_lib import review_benchmark
from sfc_lib.common import Halt, Processes, git, read_json, write_json
from sfc_lib.review_policy import completed_message, model_settings, promotion_binding
from sfc_lib.review_benchmark import (
    EXECUTION_LIMITS, INSPECTION_CRITERIA, adjudicate_findings, evaluate, inspection_arm, source_inspection_limits,
    validate_additional_detection, validate_additional_findings, validate_behavioral_oracle,
    validate_source_inspection_reservations,
)
from sfc_lib.verification import checksum
from test_review_policy import PolicyProcesses, capabilities
from test_factory import BASE_CONFIG, issue

REVIEW_CONFIG = {**BASE_CONFIG, 'model': 'fixture-model', 'reasoning_effort': 'medium', 'service_tier': 'fast',
                 'verify_commands': ['python3 -B check_behavior.py']}


def proof_ref(path):
    return {'path': str(path), 'sha256': checksum(path)}


def additional_proof(folder, sample, adjudicator, *, kind='regression'):
    folder.mkdir()
    probe = folder / 'probe.py'
    probe.write_text('# Independent reproduction fixture; evaluator never executes this file.\n')
    proof = {'version': 1, 'kind': kind, 'id': folder.name, 'adjudicator': adjudicator,
             'reason': 'The independently exercised input violates its observable contract at head',
             **{key: sample[key] for key in ('case', 'base', 'head', 'task_digest')},
             'probe': proof_ref(probe), 'runs': {}}
    if kind == 'acceptance-gap':
        proof['criterion'] = sample['criteria'][0]
    for side in ('base', 'head'):
        log = folder / f'{side}.json'
        write_json(log, {'version': 1, 'commit': sample[side], 'probe_sha256': proof['probe']['sha256'],
                         'satisfied': side == 'base' and kind == 'regression',
                         'observation': f'Observed contract behavior at {side}'})
        proof['runs'][side] = {'commit': sample[side], 'exit_code': 0, 'log': proof_ref(log)}
    path = folder / 'proof.json'
    write_json(path, proof)
    return proof_ref(path)


class ReviewBenchmarkTests(unittest.TestCase):
    def test_deeply_nested_additional_proof_and_observation_are_invalid(self):
        from sfc_lib.factory import Factory

        card = issue()
        sample = {'case': 'retained', 'kind': 'retained-real-finding', 'base': 'a' * 40, 'head': 'b' * 40,
                  'card': card, 'task_digest': Factory.task_digest(card), 'expected': ['known']}
        reviewer = 'Independent fixture adjudicator'
        depth = sys.getrecursionlimit() + 10
        nested_json = ('[' * depth + '0' + ']' * depth).encode()
        parse = json.loads

        def recursive_parser(data):
            # Decoder recursion behavior varies across Python versions.
            if data == nested_json:
                raise RecursionError('fixture decoder recursion limit')
            return parse(data)

        with tempfile.TemporaryDirectory() as tmp:
            for file in ('proof.json', 'base.json', 'head.json'):
                with self.subTest(file=file):
                    folder = Path(tmp) / file / 'novel'
                    folder.parent.mkdir()
                    ref = additional_proof(folder, sample, reviewer)
                    proof_path = Path(ref['path'])
                    proof = read_json(proof_path)
                    (folder / file).write_bytes(nested_json)
                    if file != 'proof.json':
                        proof['runs'][Path(file).stem]['log'] = proof_ref(folder / file)
                        write_json(proof_path, proof)
                    sample['additional_findings'] = {'novel': proof_ref(proof_path)}
                    with self.assertRaisesRegex(ValueError, 'proof JSON (must be an object|exceeds the nesting limit)'):
                        validate_additional_findings(sample, reviewer)
                    with patch.object(review_benchmark.json, 'loads', side_effect=recursive_parser):
                        with self.assertRaisesRegex(ValueError, 'proof JSON exceeds the nesting limit'):
                            validate_additional_findings(sample, reviewer)

    def test_additional_proofs_require_exact_reproduction_and_raw_row_excerpts(self):
        from sfc_lib.factory import Factory
        from sfc_lib.review_policy import criteria

        card = issue()
        sample = {'case': 'retained', 'kind': 'retained-real-finding', 'base': 'a' * 40, 'head': 'b' * 40,
                  'card': card, 'task_digest': Factory.task_digest(card),
                  'criteria': [row['id'] for row in criteria(card)], 'expected': ['known']}
        reviewer = 'Independent fixture adjudicator'
        receipts = [{'lane': 'qa', 'result': {'findings': [{'message': 'Known, novel, and required-gap behavior.'}]}}]

        def mapping(identifier, excerpt):
            return {'id': identifier, 'reason': 'Reproduced independently', 'excerpt': excerpt}

        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            novel = additional_proof(folder / 'novel', sample, reviewer)
            gap = additional_proof(folder / 'required-gap', sample, reviewer, kind='acceptance-gap')
            sample['additional_findings'] = {'novel': novel, 'required-gap': gap}
            verified = validate_additional_findings(sample, reviewer)
            self.assertEqual(verified, {'novel', 'required-gap'})
            matches = [mapping('known', 'Known'), mapping('novel', 'novel'), mapping('required-gap', 'required-gap')]
            rows = [{'receipt_index': 0, 'index': 0, 'regressions': matches}]
            detected, unique, fp, unknown = adjudicate_findings(rows, receipts, {'known'},
                                                               additional=verified, excerpts=True)
            self.assertEqual(detected, {0: {'known', 'novel', 'required-gap'}})
            self.assertEqual((unique, fp, unknown), (detected[0], 0, 0))
            validate_additional_detection(verified, {'known', 'novel'}, detected[0])
            with self.assertRaisesRegex(ValueError, 'candidate missed'):
                validate_additional_detection(verified, detected[0], {'known', 'novel'})
            with self.assertRaisesRegex(ValueError, 'proof is unused'):
                validate_additional_detection(verified, {'known'}, {'known', 'novel'})
            with self.assertRaisesRegex(ValueError, 'raw report row'):
                adjudicate_findings([{**rows[0], 'regressions': [mapping('novel', 'unreported')]}], receipts,
                                    {'known'}, additional=verified, excerpts=True)
            with self.assertRaisesRegex(ValueError, 'must be retained as unadjudicated'):
                adjudicate_findings(rows, receipts, {'known'}, additional={'novel'}, excerpts=True)
            with self.assertRaisesRegex(ValueError, 'scope, adjudicator or contract differs'):
                validate_additional_findings(sample, 'Other adjudicator')
            proof_path = Path(gap['path'])
            proof = read_json(proof_path)
            write_json(proof_path, {**proof, 'criterion': 'unrelated'})
            sample['additional_findings']['required-gap'] = proof_ref(proof_path)
            with self.assertRaisesRegex(ValueError, 'scope, adjudicator or contract differs'):
                validate_additional_findings(sample, reviewer)
            write_json(proof_path, proof)
            sample['additional_findings']['required-gap'] = proof_ref(proof_path)
            Path(proof['runs']['base']['log']['path']).write_text('changed')
            with self.assertRaisesRegex(ValueError, 'proof bytes changed'):
                validate_additional_findings(sample, reviewer)

    def test_source_inspection_plan_binds_case_caps_and_aggregate_reservations(self):
        pairs = [{'case': case, 'kind': kind, 'base': 'base', 'head': 'head', 'expected': expected}
                 for case, kind, expected in (('retained', 'retained-real-finding', ['defect']),
                                              ('failed', 'failed-check', ['check-failure']), ('clean', 'clean', []))]
        caps = {'cases': {row['case']: {**EXECUTION_LIMITS, 'worker_timeout': 180 if row['case'] == 'retained' else 120}
                          for row in pairs}, 'maximum_model_dispatches': 10, 'maximum_worker_seconds': 1200}
        config = {**REVIEW_CONFIG, 'worker_timeout': 1200, 'verify_timeout': 900}
        report = {'target_config': config, 'criteria': INSPECTION_CRITERIA, 'binding': 'binding',
                  'runtime_sha256': '1' * 64, 'cli_version': 'fixture'}
        provider = '2' * 64
        plan = {'profile': 'source', 'config': config, 'case_inventory': pairs, 'criteria': INSPECTION_CRITERIA,
                'binding': report['binding'], 'runtime_sha256': report['runtime_sha256'],
                'cli': {'version': report['cli_version'], 'provider_sha256': provider}, 'measurement_limits': caps}
        self.assertIsNone(source_inspection_limits(report, pairs))
        with tempfile.TemporaryDirectory() as temp:
            plan_path, ledger = Path(temp) / 'plan.json', Path(temp) / 'reservations.jsonl'

            def pin(path):
                return {'path': str(path), 'sha256': checksum(path)}

            def set_plan(value):
                write_json(plan_path, value)
                report['plan'] = pin(plan_path)

            set_plan(plan)
            self.assertEqual(source_inspection_limits(report, pairs), caps)
            write_json(plan_path, {**plan, 'binding': 'changed'})
            with self.assertRaisesRegex(Halt, 'hash changed'):
                source_inspection_limits(report, pairs)
            for case, key, value in (('retained', 'worker_timeout', 181), ('clean', 'worker_timeout', 181),
                                     ('failed', 'verify_timeout', 91)):
                invalid = copy.deepcopy(plan)
                invalid['measurement_limits']['cases'][case][key] = value
                set_plan(invalid)
                with self.subTest(case=case, key=key), self.assertRaisesRegex(ValueError, 'case exceeds'):
                    source_inspection_limits(report, pairs)
            for key, value in (('maximum_model_dispatches', 11), ('maximum_worker_seconds', 1201)):
                set_plan({**plan, 'measurement_limits': {**caps, key: value}})
                with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'aggregate budget'):
                    source_inspection_limits(report, pairs)

            set_plan(plan)
            dispatches = [{'case': case, 'variant': variant, 'lane': lane,
                           'worker_timeout': caps['cases'][case]['worker_timeout']}
                          for case, variant, lanes in (
                              ('retained', 'baseline', ['qa']), ('retained', 'candidate', ['inspection']),
                              ('failed', 'candidate', ['inspection']), ('clean', 'baseline', ['qa', 'review']),
                              ('clean', 'candidate', ['inspection', 'review'])) for lane in lanes]

            def set_reservations(observed):
                rows = [{**row, 'index': index, 'at': 1, 'plan_sha256': report['plan']['sha256'],
                         'provider_sha256': provider} for index, row in enumerate(observed, 1)]
                ledger.write_text(''.join(json.dumps(row) + '\n' for row in rows))
                report['model_reservations'] = pin(ledger)

            self.assertEqual(sum(row['worker_timeout'] for row in dispatches), 960)
            set_reservations(dispatches)
            validate_source_inspection_reservations(report, caps, dispatches)
            for bad in (dispatches[1:], [{**dispatches[0], 'worker_timeout': 120}, *dispatches[1:]]):
                set_reservations(bad)
                with self.assertRaisesRegex(ValueError, 'reservations exceed'):
                    validate_source_inspection_reservations(report, caps, dispatches)
            over_budget = dispatches + [dispatches[0], dispatches[1]]
            self.assertLess(len(over_budget), caps['maximum_model_dispatches'])
            set_reservations(over_budget)
            with self.assertRaisesRegex(ValueError, 'reservations exceed'):
                validate_source_inspection_reservations(report, caps, over_budget)

    def test_one_raw_finding_can_map_to_multiple_predeclared_regressions(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'comparison'
            benchmark(output)
            report = read_json(output / 'comparison.json')
            sample = report['samples'][0]
            sample['expected'].append('second-defect')
            mappings = [{'id': identifier, 'reason': f'Independent reproduction of {identifier}'}
                        for identifier in sample['expected']]
            for arm in ('baseline', 'candidate'):
                sample[arm]['adjudications'] = [{'receipt_index': 0, 'index': 0,
                                                  'regressions': copy.deepcopy(mappings)}]
            decision = evaluate(report)
            self.assertEqual(decision['metrics']['candidate']['detection_recall'], 1)
            self.assertEqual(decision['metrics']['candidate']['false_positives'], 0)

            group = sample['candidate']['adjudications'][0]['regressions']
            group.pop()
            decision = evaluate(report)
            self.assertIn('detection parity failed', decision['failures'])
            self.assertLess(decision['metrics']['candidate']['detection_recall'], 1)

            group.append(group[0])
            self.assertIn('distinct ID', ' '.join(evaluate(report)['failures']))
            group.pop()
            group[0]['reason'] = ' '
            self.assertIn('own reasoning', ' '.join(evaluate(report)['failures']))

    def test_unadjudicated_finding_blocks_promotion_without_counting_as_false_positive(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'comparison'
            benchmark(output)
            report = read_json(output / 'comparison.json')
            sample = report['samples'][0]
            known, = sample['expected']
            sample['baseline']['adjudications'] = [{'receipt_index': 0, 'index': 0, 'regressions': [
                {'id': known, 'reason': 'The original failure is confirmed'}],
                'unadjudicated_reason': 'The same finding reports additional behavior outside this inventory'}]
            decision = evaluate(report)
            self.assertEqual(decision['metrics']['baseline']['detection_recall'], 1)
            self.assertEqual(decision['metrics']['baseline']['false_positives'], 0)
            self.assertEqual(decision['metrics']['baseline']['unadjudicated_findings'], 1)
            self.assertIn(f"{sample['case']} baseline has findings outside the declared inventory",
                          decision['failures'])

            sample['candidate']['adjudications'][0]['regression'] = 'unverified-new-defect'
            decision = evaluate(report)
            self.assertEqual(decision['metrics']['candidate']['false_positives'], 0)
            self.assertEqual(decision['metrics']['candidate']['unadjudicated_findings'], 1)

            row = sample['baseline']['adjudications'][0]
            row['regressions'].append({'id': 'unverified-new-defect', 'reason': 'Outside the inventory'})
            self.assertIn('must be retained as unadjudicated', ' '.join(evaluate(report)['failures']))

    def test_comparison_rejects_substituting_an_unmeasured_target_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'comparison'
            config = {**REVIEW_CONFIG, 'verify_commands': ['true'], 'review_policy': {
                'mode': 'risk-based', 'providers': {'review': 'codex-native'}}}
            with self.assertRaisesRegex(ValueError, 'exact executable fixture checks'):
                benchmark(output, config)
            self.assertFalse(output.exists())

    def test_behavioral_comparison_checks_oracles_and_keeps_two_independent_lanes(self):
        execute = Processes.execute

        def fixture_execute(process, argv, cwd, log, timeout, prompt=None, **kwargs):
            if argv[0] != 'codex':
                return execute(process, argv, cwd, log, timeout, prompt, **kwargs)
            self.assertNotIn('LazyRows', prompt)
            self.assertNotIn('retained-double-lookup', prompt)
            self.assertFalse(list(Path(cwd).parent.glob('case-*/oracle/receipt.json')))
            fixture = PolicyProcesses(Path(cwd) / '.sfc/stop')
            code = (Path(cwd) / 'behavior.py').read_text()
            found = ('def choose' in code or 'return rows[key]' in code or
                     ('\n    rows.get(key)\n    return rows.get(key)\n' in code and
                      'an SFC review worker' in prompt))
            if found:
                fixture.mutate = lambda r: r.update(verdict='fail', findings=[
                    {'file': 'behavior.py', 'line': 3, 'severity': 'high', 'message': 'Preserve behavior'}])
            return fixture.execute(argv, cwd, log, timeout, prompt, **kwargs)

        config = {**REVIEW_CONFIG, 'review_policy': {
            'mode': 'risk-based', 'providers': {'review': 'codex-native'},
            'behavioral_contract': {'paths': ['behavior.py'], 'check_ids': [1], 'instructions': {}}}}
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(Processes, 'execute', fixture_execute), \
             patch('sfc_lib.review_policy.run', side_effect=capabilities), \
             patch('sfc_lib.review_policy.native_sessions', return_value=Path(tmp) / 'sessions'), \
             patch('sfc_lib.review_policy.shutil.which', return_value='/fixture/codex'):
            output = Path(tmp) / 'comparison'
            self.assertFalse(benchmark(output, config)['promote'])
            report = read_json(output / 'comparison.json')
            samples = {sample['case']: sample for sample in report['samples']}
            for sample in samples.values():
                self.assertIn('receipts', sample['candidate'], sample['candidate'])
                self.assertIsNone(sample['candidate']['attestation'])
                validate_behavioral_oracle(sample)
                result = read_json(sample['oracle']['path'])['observations']
                self.assertEqual(result['base']['exit_code'], 0)
                self.assertEqual(result['candidate']['exit_code'], sample['kind'] != 'clean')
            for case, outcome in (('retained-double-lookup', 'failed'), ('clean-record', 'passed')):
                arm = samples[case]['candidate']
                self.assertEqual(arm['outcome'], outcome)
                self.assertEqual([read_json(ref['path'])['lane'] for ref in arm['receipts']], ['qa', 'review'])
            changed = copy.deepcopy(samples['retained-double-lookup'])
            changed['oracle'] = samples['clean-record']['oracle']
            with self.assertRaisesRegex(ValueError, 'preflight'):
                validate_behavioral_oracle(changed)

    def test_real_executable_seed_regressions_and_clean_control_cannot_promote_fake_models(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'comparison'
            decision = benchmark(output)
            self.assertFalse(decision['promote'])
            self.assertIn('offline fake-adapter results', ' '.join(decision['failures']))
            for variant in ('baseline', 'candidate'):
                self.assertEqual(decision['metrics'][variant]['detection_recall'], 1)
                self.assertEqual(decision['metrics'][variant]['false_positives'], 0)
                self.assertEqual(decision['metrics'][variant]['acceptance_coverage'], 1)
            report = read_json(output / 'comparison.json')
            changed = copy.deepcopy(report)
            changed['criteria']['maximum_latency_ratio'] = 999
            self.assertFalse(evaluate(changed)['promote'])
            changed = copy.deepcopy(report)
            changed['samples'] *= 13
            self.assertFalse(evaluate(changed)['promote'])
            changed = copy.deepcopy(report)
            changed['samples'][0]['candidate']['coverage'] = []
            self.assertIn('acceptance coverage failed', evaluate(changed)['failures'])
            raw = Path(report['samples'][0]['candidate']['receipts'][0]['path'])
            raw.write_text('{}')
            self.assertIn('receipt changed', ' '.join(evaluate(report)['failures']))

    def test_paired_runner_retains_candidate_checks_and_validates_mapped_coverage(self):
        execute = Processes.execute
        fixture_path = 'docs/edge example.md'

        def fixture_execute(process, argv, cwd, log, timeout, prompt=None, **kwargs):
            if argv[0] != 'codex':
                return execute(process, argv, cwd, log, timeout, prompt, **kwargs)
            fixture = PolicyProcesses(Path(cwd) / '.sfc/stop')
            if not Path(cwd).name.startswith('clean-record'):
                fixture.mutate = lambda r: r.update(verdict='fail', findings=[
                    {'file': fixture_path, 'line': 3, 'severity': 'high', 'message': 'Preserve the edge behavior'}])
            return fixture.execute(argv, cwd, log, timeout, prompt, **kwargs)

        def attest(root, pins):
            self.assertEqual(git(root, 'rev-parse', 'HEAD'), pins['head'])
            time.sleep(0.02)
            return True

        config = {**REVIEW_CONFIG, 'review_policy': {'mode': 'risk-based', 'routine_paths': ['docs/**'],
                                                   'providers': {'review': 'codex-native'}}}
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(Processes, 'execute', fixture_execute), \
             patch('sfc_lib.review_policy.run', side_effect=capabilities), \
             patch('sfc_lib.review_policy.native_sessions', return_value=Path(tmp) / 'sessions'), \
             patch('sfc_lib.review_policy.shutil.which', return_value='/fixture/codex'):
            output = Path(tmp) / 'comparison'
            self.assertFalse(benchmark(output, config, fixture_path=fixture_path, attest=attest)['promote'])
            report = read_json(output / 'comparison.json')
            report['independent_adjudicator'] = 'Simulated independent adjudicator'
            for sample in report['samples']:
                self.assertNotEqual(sample['head'], sample['base'])
                self.assertEqual(git(sample['worktree'], 'diff', '--name-only', sample['base'], sample['head']),
                                 fixture_path)
                for name in ('baseline', 'candidate'):
                    arm = sample[name]
                    for position, ref in enumerate(arm['receipts']):
                        receipt = read_json(ref['path'])
                        for index, _ in enumerate(receipt['result']['findings']):
                            arm['adjudications'].append({'receipt_index': position, 'index': index,
                                                        'regression': sample['expected'][0],
                                                        'reason': 'Reproduced fixture regression'})
                    arm.update(coverage=sample['criteria'], coverage_adjudication={
                        criterion: 'Assertion exercises this criterion' for criterion in sample['criteria']})
            decision = evaluate(report)
            self.assertTrue(decision['metrics'], decision['failures'])
            different_target = copy.deepcopy(report)
            different_target['target_config']['worker_timeout'] += 1
            different_target['policy_sha256'] = promotion_binding(different_target['target_config'])
            self.assertIn('configuration differs', ' '.join(evaluate(different_target)['failures']))
            self.assertEqual(decision['metrics']['candidate']['acceptance_coverage'], 1)
            self.assertEqual(decision['metrics']['candidate']['detection_recall'], 1)
            self.assertNotIn('no clean fixture completed a consolidated candidate route', decision['failures'])

            extended = copy.deepcopy(report)
            extended['finding_adjudication_version'] = 2
            for sample in extended['samples']:
                for name in ('baseline', 'candidate'):
                    arm = sample[name]
                    arm['adjudications'] = [
                        {'receipt_index': row['receipt_index'], 'index': row['index'],
                         'regressions': [{'id': row['regression'], 'reason': row['reason'],
                                          'excerpt': 'Preserve the edge behavior'}]}
                        for row in arm['adjudications']]
            sample = extended['samples'][0]
            sample['additional_findings'] = {'extra': additional_proof(output / 'extra', sample,
                                                                         report['independent_adjudicator'])}
            match = {'id': 'extra', 'reason': 'Independent reproduction establishes an additional mode',
                     'excerpt': 'Preserve the edge behavior'}
            sample['candidate']['adjudications'][0]['regressions'].append(match)
            candidate_only = evaluate(extended)
            self.assertEqual(candidate_only['metrics']['candidate']['detection_recall'], 1)
            self.assertEqual(candidate_only['metrics']['candidate']['verified_additional_findings'], 1)
            self.assertEqual(candidate_only['metrics']['candidate']['false_positives'], 0)
            self.assertEqual(candidate_only['metrics']['baseline']['verified_additional_findings'], 0)
            sample['baseline']['adjudications'][0]['regressions'].append(match)
            self.assertEqual(evaluate(extended)['metrics']['baseline']['verified_additional_findings'], 1)
            sample['candidate']['adjudications'][0]['regressions'].pop()
            self.assertIn('candidate missed a verified additional baseline finding',
                          ' '.join(evaluate(extended)['failures']))
            sample['candidate']['adjudications'][0]['regressions'] = [match]
            missing_known = evaluate(extended)
            self.assertLess(missing_known['metrics']['candidate']['detection_recall'], 1)
            self.assertIn('detection parity failed', missing_known['failures'])

            self.assertEqual([read_json(v['path'])['lane'] for v in report['samples'][-1]['candidate']['receipts']],
                             ['review'])
            clean = report['samples'][-1]
            arm = clean['candidate']
            self.assertGreaterEqual(arm['attestation']['elapsed_seconds'], 0.02)
            self.assertEqual(arm['attestation']['pins'], {
                'head': clean['head'], 'base': clean['base'], 'task': clean['task_digest']})
            self.assertIsNone(clean['baseline']['attestation'])

            def retain_timing(arm, name, **fields):
                timing = Path(tmp) / f'{name}-timing.json'
                write_json(timing, {**read_json(arm['timing']['path']), **fields})
                arm.update(fields, timing={'path': str(timing), 'sha256': checksum(timing)})

            paired = copy.deepcopy(report)
            # Fix the synthetic timings so both pooled measures always pass.
            for index, sample in enumerate(paired['samples']):
                for name, lane_seconds, end_to_end in (('baseline', 20, 1000), ('candidate', 10, 100)):
                    arm = sample[name]
                    prefix = f'{index}-{name}'
                    receipts = []
                    for lane_index, ref in enumerate(arm['receipts']):
                        path = Path(tmp) / f'{prefix}-lane-{lane_index}.json'
                        write_json(path, {**read_json(ref['path']), 'elapsed_seconds': lane_seconds})
                        receipts.append({'path': str(path), 'sha256': checksum(path)})
                    retain_timing(arm, prefix, receipts=receipts, review_seconds=lane_seconds * len(receipts),
                                  end_to_end_seconds=end_to_end)

            control = paired['samples'][-1]['candidate']
            retain_timing(control, 'control-at-threshold', end_to_end_seconds=950)
            self.assertTrue(evaluate(paired)['promote'])

            changed = copy.deepcopy(paired)
            slow = changed['samples'][-1]['candidate']
            retain_timing(slow, 'slow-control', end_to_end_seconds=1100,
                          attestation={**slow['attestation'], 'elapsed_seconds': 1050})
            decision = evaluate(changed)
            self.assertEqual(decision['metrics']['candidate']['median_review_seconds'], 10)
            self.assertEqual(decision['metrics']['baseline']['median_review_seconds'], 20)
            self.assertEqual(decision['metrics']['candidate']['median_end_to_end_seconds'], 100)
            self.assertEqual(decision['metrics']['baseline']['median_end_to_end_seconds'], 1000)
            self.assertFalse(decision['promote'])
            self.assertEqual(decision['failures'],
                             ['clean-record consolidated control did not improve end-to-end time at least 5%'])

            baseline = paired['samples'][-1]['baseline']
            baseline.update(coverage=[], coverage_adjudication={})
            self.assertIn('clean-record consolidated control lacks a completed baseline', evaluate(paired)['failures'])

            changed = copy.deepcopy(report)
            arm = changed['samples'][-1]['candidate']
            receipt = read_json(arm['receipts'][0]['path'])
            arm['end_to_end_seconds'] = arm['review_seconds'] + receipt['verification']['elapsed_seconds']
            short_timing = Path(tmp) / 'short-timing.json'
            write_json(short_timing, {**read_json(arm['timing']['path']),
                                      'end_to_end_seconds': arm['end_to_end_seconds']})
            arm['timing'] = {'path': str(short_timing), 'sha256': checksum(short_timing)}
            self.assertIn('omits verification, review or operator attestation',
                          ' '.join(evaluate(changed)['failures']))

            changed = copy.deepcopy(report)
            arm = changed['samples'][-1]['candidate']
            arm['attestation'] = None
            missing_attestation = Path(tmp) / 'missing-attestation.json'
            write_json(missing_attestation, {**read_json(arm['timing']['path']), 'attestation': None})
            arm['timing'] = {'path': str(missing_attestation), 'sha256': checksum(missing_attestation)}
            self.assertIn('configuration differs', ' '.join(evaluate(changed)['failures']))
            for sample in report['samples'][:2]:
                self.assertEqual(sample['baseline']['outcome'], 'failed')
                self.assertEqual([read_json(v['path'])['lane'] for v in sample['baseline']['receipts']], ['qa'])
                self.assertEqual(sample['baseline']['review_seconds'],
                                 read_json(sample['baseline']['receipts'][0]['path'])['elapsed_seconds'])

            # A valid extra receipt still cannot be charged after a terminal failure.
            changed = copy.deepcopy(report)
            arm = changed['samples'][0]['baseline']
            arm['receipts'].append(arm['receipts'][0])
            timing = {**read_json(arm['timing']['path']), 'receipts': arm['receipts']}
            extra_timing = Path(tmp) / 'extra-timing.json'
            write_json(extra_timing, timing)
            arm['timing'] = {'path': str(extra_timing), 'sha256': checksum(extra_timing)}
            self.assertIn('extends past its production outcome', ' '.join(evaluate(changed)['failures']))

            changed = copy.deepcopy(report)
            changed['target_policy']['mandatory'] = ['security', 'human']
            self.assertIn('configuration differs', ' '.join(evaluate(changed)['failures']))

            changed = copy.deepcopy(report)
            changed['samples'][0]['candidate']['coverage_adjudication'] = {}
            self.assertIn('claimed coverage lacks', ' '.join(evaluate(changed)['failures']))
            changed = copy.deepcopy(report)
            changed['samples'][0]['head'] = changed['samples'][0]['base']
            self.assertIn('actual candidate diff', ' '.join(evaluate(changed)['failures']))

            # Simulate a self-consistently rehashed receipt with no reviewer
            # message. Lifecycle markers still cannot prove the result.
            arm = report['samples'][0]['baseline']
            ref = arm['receipts'][0]
            receipt = read_json(ref['path'])
            log = Path(receipt['log'])
            log.write_text('\n'.join(line for line in log.read_text().splitlines()
                                     if json.loads(line)['type'] != 'item.completed') + '\n')
            with self.assertRaisesRegex(ValueError, 'completed reviewer message'):
                completed_message(log)
            receipt['log_sha256'] = checksum(log)
            write_json(ref['path'], receipt)
            ref['sha256'] = checksum(ref['path'])
            timing = read_json(arm['timing']['path'])
            timing['receipts'] = arm['receipts']
            write_json(arm['timing']['path'], timing)
            arm['timing']['sha256'] = checksum(arm['timing']['path'])
            self.assertIn('completed reviewer message', ' '.join(evaluate(report)['failures']))

    def test_clean_docs_check_exercises_lookup_and_documentation(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'comparison'
            fixture_path = 'docs/lookup.md'
            benchmark(output, fixture_path=fixture_path)
            sample = read_json(output / 'comparison.json')['samples'][-1]
            root = Path(sample['worktree'])
            check = [sys.executable, '-B', 'check_behavior.py']
            self.assertEqual(subprocess.run(check, cwd=root, capture_output=True).returncode, 0)
            code = root / 'fixture_lookup.py'
            source = code.read_text()
            code.write_text('def lookup(rows, key): return None\n')
            self.assertNotEqual(subprocess.run(check, cwd=root, capture_output=True).returncode, 0)
            code.write_text(source)
            (root / fixture_path).write_text('The guide has no lookup example.\n')
            self.assertNotEqual(subprocess.run(check, cwd=root, capture_output=True).returncode, 0)

    def test_fixture_path_cannot_escape_or_replace_runner_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'comparison'
            for path in ('../outside.py', '/absolute.py', '.git/config', '.sfc/state.json',
                         'check_behavior.py', 'fixture_lookup.py'):
                with self.subTest(path=path), self.assertRaisesRegex(Halt, 'fixture path'):
                    benchmark(output, fixture_path=path)
            self.assertFalse(output.exists())

    def test_source_inspection_harness_keeps_failed_checks_and_requires_all_lanes(self):
        from test_review_inspection import InspectionProcesses
        execute = Processes.execute

        def fixture_execute(process, argv, cwd, log, timeout, prompt=None, **kwargs):
            if argv[0] != 'codex':
                return execute(process, argv, cwd, log, timeout, prompt, **kwargs)
            fixture = InspectionProcesses(Path(cwd) / '.sfc/stop')
            fixture.wait_for_check = False
            if 'independent provisional inspector' in prompt and any(
                    case in Path(log).parts for case in ('first-stable-never', 'deleted-record')):
                fixture.adverse_inspection = True
            elif Path(cwd).name.startswith('first-stable-never'):
                fixture.mutate = lambda r: r.update(verdict='fail', findings=[
                    {'file': 'behavior.py', 'line': 3, 'severity': 'high', 'message': 'Missing Latest guard'}])
            return fixture.execute(argv, cwd, log, timeout, prompt, **kwargs)

        config = {**REVIEW_CONFIG, 'repo': 'fixture/review', 'max_workers': 2, 'worker_timeout': 120,
                  'verify_commands': ['python3 -B check_behavior.py'],
                  'review_policy': {'inspection': {'enabled': ['source']}}}
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(Processes, 'execute', fixture_execute), \
             patch('sfc_lib.review_policy.run', side_effect=capabilities), \
             patch('sfc_lib.review_policy.shutil.which', return_value='/fixture/codex'):
            output = Path(tmp) / 'source-overlap'
            decision = source_inspection_benchmark(output, config)
            self.assertEqual(decision['qualified'], [])
            report = read_json(output / 'comparison.json')
            retained, failed, clean = report['profiles']['source']
            self.assertEqual(retained['candidate']['outcome'], 'fail')
            # A grouped QA finding must retain each expected ID in the actual
            # decisive assessment rather than obtaining credit from the inspector.
            retained['expected'] = [retained['expected'], 'another-guard-failure']
            retained_arm = retained['candidate']
            retained_arm['coverage_adjudication'] = {item: 'The configured check exercises this criterion.'
                                                    for item in retained['obligations']}
            retained_arm['adjudications'] = [
                {'lane': 'inspection', 'index': 0, 'regressions': [],
                 'unadjudicated_reason': 'The provisional report still needs independent adjudication'},
                {'lane': 'qa', 'index': 0, 'regressions': [
                    {'id': identifier, 'reason': f'Independent reproduction of {identifier}'}
                    for identifier in retained['expected']]}]
            grouped = inspection_arm(report, retained, 'source', 'candidate', set())
            self.assertEqual(grouped['detected'], set(retained['expected']))
            self.assertEqual(grouped['false_positives'], 0)
            self.assertNotIn('additional_detected', grouped)
            retained_arm['adjudications'][1]['regressions'].pop()
            incomplete = inspection_arm(report, retained, 'source', 'candidate', set())
            self.assertEqual(incomplete['detected'], {retained['expected'][0]})

            reviewer = 'Independent fixture adjudicator'
            retained['additional_findings'] = {'extra': additional_proof(output / 'extra', retained, reviewer)}
            extra = validate_additional_findings(retained, reviewer)
            final_matches = [{'id': identifier, 'reason': 'Independent reproduction', 'excerpt': 'Missing Latest guard'}
                             for identifier in retained['expected']]
            final_extra = {'id': 'extra', 'reason': 'Additional mode in the final report',
                           'excerpt': 'Missing Latest guard'}
            provisional_extra = {**final_extra, 'excerpt': 'Original value contract was removed'}
            baseline = retained['baseline']
            baseline['coverage_adjudication'] = retained_arm['coverage_adjudication']
            baseline['adjudications'] = [{'lane': 'qa', 'index': 0, 'regressions': [*final_matches, final_extra]}]
            retained_arm['adjudications'] = [
                {'lane': 'inspection', 'index': 0, 'regressions': [provisional_extra]},
                {'lane': 'qa', 'index': 0, 'regressions': final_matches}]
            observed = {name: inspection_arm(report, retained, 'source', name, set(), additional=extra, excerpts=True)
                        for name in ('baseline', 'candidate')}
            self.assertEqual(observed['baseline']['additional_detected'], extra)
            self.assertEqual(observed['candidate']['additional_detected'], set())
            self.assertEqual(observed['candidate']['detected'], set(retained['expected']))
            with self.assertRaisesRegex(ValueError, 'candidate missed'):
                validate_additional_detection(extra, observed['baseline']['additional_detected'],
                                              observed['candidate']['additional_detected'])
            retained_arm['adjudications'][1]['regressions'].append(final_extra)
            observed = inspection_arm(report, retained, 'source', 'candidate', set(), additional=extra, excerpts=True)
            self.assertEqual(observed['additional_detected'], extra)
            self.assertEqual(observed['false_positives'], 0)

            for name in ('baseline', 'candidate'):
                self.assertEqual(failed[name]['outcome'], 'checks-failed')
                self.assertIsNone(failed[name]['assessment'])
                self.assertEqual(clean[name]['outcome'], 'pass')
            self.assertEqual(failed['candidate']['inspection']['state'], 'checks-failed')
            failed['candidate']['coverage_adjudication'] = {item: 'The required check failed and prevented acceptance.'
                                                           for item in failed['obligations']}
            failed['candidate']['adjudications'] = [
                {'lane': 'inspection', 'index': 0, 'regressions': [provisional_extra]}]
            blocked = inspection_arm(report, failed, 'source', 'candidate', set(), additional=extra, excerpts=True)
            self.assertEqual(blocked['detected'], {failed['expected']})
            self.assertEqual(blocked['additional_detected'], set())
            arm = clean['candidate']
            self.assertEqual(len(arm['specialists']), 2)
            self.assertEqual(read_json(arm['assessment']['path'])['inspection_sha256'],
                             arm['inspection']['receipt']['receipt_sha256'])
            arm['coverage_adjudication'] = {item: 'The configured check exercises this criterion.'
                                            for item in clean['obligations']}
            self.assertGreater(inspection_arm(report, clean, 'source', 'candidate', set())['overlap_seconds'], 0)
            arm['specialists'].pop()
            measured = Path(arm['measurement']['path'])
            write_json(measured, {**read_json(measured), 'specialists': arm['specialists']})
            arm['measurement']['sha256'] = checksum(measured)
            with self.assertRaisesRegex(ValueError, 'omitted or reordered'):
                inspection_arm(report, clean, 'source', 'candidate', set())

    def test_candidate_times_mandatory_security_and_records_human_handoff(self):
        execute = Processes.execute

        def fixture_execute(process, argv, cwd, log, timeout, prompt=None, **kwargs):
            if argv[0] != 'codex':
                return execute(process, argv, cwd, log, timeout, prompt, **kwargs)
            fixture = PolicyProcesses(Path(cwd) / '.sfc/stop')
            if 'review' in argv:
                fixture.mutate = lambda r: r.update(escalate=['qa', 'human'] if 'unknown-route' in str(log)
                                                    else ['human'])
            elif 'unknown-route' in str(log) and 'an SFC qa worker' in prompt:
                def escalate_targeted_qa(result):
                    if result['scope']['review_dependency']:
                        result['escalate'] = ['security']
                fixture.mutate = escalate_targeted_qa
            return fixture.execute(argv, cwd, log, timeout, prompt, **kwargs)

        fixture_path = 'docs/example.md'
        policy = {'mode': 'risk-based', 'routine_paths': [fixture_path],
                  'mandatory': ['security', 'human'], 'providers': {'review': 'codex-native'}}
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(Processes, 'execute', fixture_execute), \
             patch('sfc_lib.review_policy.run', side_effect=capabilities), \
             patch('sfc_lib.review_policy.native_sessions', return_value=Path(tmp) / 'sessions'), \
             patch('sfc_lib.review_policy.shutil.which', return_value='/fixture/codex'):
            output = Path(tmp) / 'comparison'
            decision = benchmark(output, {**REVIEW_CONFIG, 'review_policy': policy},
                                 fixture_path=fixture_path, attest=lambda *_: True)
            report = read_json(output / 'comparison.json')
            self.assertEqual(report['target_policy'], policy)
            self.assertIn('reached human', ' '.join(decision['failures']))
            for sample in report['samples']:
                arm = sample['candidate']
                self.assertEqual(arm['outcome'], 'human')
                expected = ['review', 'security'] if sample['kind'] == 'clean' else ['qa', 'security', 'review']
                self.assertEqual([read_json(v['path'])['lane'] for v in arm['receipts']], expected)
            clean = report['samples'][-1]
            arm = real_arm(Path(clean['worktree']), Path(tmp) / 'escalation', clean['card'], clean['base'],
                           'candidate', model_settings(REVIEW_CONFIG), {**policy, 'mandatory': []}, attest=lambda *_: True)
            self.assertEqual(arm['outcome'], 'human')
            self.assertEqual([read_json(v['path'])['lane'] for v in arm['receipts']], ['review'])
            arm = real_arm(Path(clean['worktree']), Path(tmp) / 'unknown-route', clean['card'], clean['base'],
                           'candidate', model_settings(REVIEW_CONFIG), {**policy, 'mandatory': []},
                           attest=lambda *_: False)
            self.assertFalse(arm['attestation']['accepted'])
            self.assertNotIn('routine_candidate', arm['config']['review_policy'])
            receipts = [read_json(v['path']) for v in arm['receipts']]
            self.assertEqual([v['lane'] for v in receipts], ['qa', 'security', 'review', 'qa', 'security'])
            self.assertEqual(receipts[3]['result']['scope']['review_dependency'], receipts[2]['result_sha256'])
            self.assertEqual(receipts[4]['result']['scope']['review_dependency'], receipts[3]['result_sha256'])


if __name__ == '__main__':
    unittest.main()
