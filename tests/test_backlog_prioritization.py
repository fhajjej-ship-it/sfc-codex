"""Semantic checks for saved interactive decisions, not a model-accuracy eval."""
import copy
import hashlib
import json
from pathlib import Path
import unittest

from backlog_replay import FIXTURE, MARKER, REFERENCE, FakeGitHub, annotation, replay

WRITES = {'create_issue', 'add_to_project', 'set_priority', 'create_comment', 'update_comment'}


class BacklogPrioritizationReplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = {c['name']: c for c in json.loads(FIXTURE.read_text())['cases']}
        cls.results = {name: replay(case) for name, case in cls.cases.items()}

    def writes(self, name, operation=None):
        return [t for t in self.results[name]['trace'] if t['operation'] in
                ({operation} if operation else WRITES)]

    def rows(self, name):
        return {r['number']: r for r in self.results[name]['readout']}

    def test_stored_receipt_matches_actual_replay_and_current_workflow(self):
        receipt = json.loads(FIXTURE.with_name('backlog-prioritization-receipt.json').read_text())
        self.assertEqual(receipt['workflow_sha256'], hashlib.sha256(REFERENCE.read_bytes()).hexdigest())
        self.assertEqual(receipt['fixture_sha256'], hashlib.sha256(FIXTURE.read_bytes()).hexdigest())
        self.assertEqual(receipt['results'], list(self.results.values()))
        # Connect the executable evidence to the human acceptance fixture too.
        fixture_md = FIXTURE.with_name('backlog-prioritization.md').read_text()
        self.assertIn('backlog-prioritization-receipt.json', fixture_md)
        self.assertIn('test_backlog_prioritization.py', fixture_md)

    def test_scope_and_preservation_independent_of_fake_transport(self):
        for name, result in self.results.items():
            with self.subTest(name=name):
                before, after = copy.deepcopy(result['before']), copy.deepcopy(result['after'])
                self.assertEqual(result['origin'], 'https://github.com/example/app')
                self.assertEqual(result['config']['project'], {'owner': 'example', 'number': 7})
                self.assertEqual(result['config']['status_field'], 'Pipeline')
                for t in self.writes(name):
                    args = t['input']
                    if name == 'A-create':
                        self.assertEqual(args['number'], 1)
                        continue
                    value = before['items'][str(args['number'])]
                    self.assertEqual((value['repository'], value['type'], value['state'], value['stage']),
                                     ('example/app', 'Issue', 'OPEN', 'Backlog'))
                    # Fake accepts targeted writes, but tests enforce manual/ownership policy.
                    if t['operation'] == 'set_priority':
                        n = str(args['number'])
                        if value['priority'] is not None:
                            prior = result['prior_receipts'][n]
                            self.assertEqual(prior['priority'], value['priority'])
                            self.assertIn(prior['comment'], value['comments'])
                    if t['operation'] == 'update_comment':
                        prior = result['prior_receipts'][str(args['number'])]['comment']
                        self.assertEqual(args['comment_id'], prior['id'])
                        self.assertIn(prior, value['comments'])
                    # Fresh scope/value read must precede each item's mutation group.
                    index = result['trace'].index(t)
                    last_read = next(e for e in reversed(result['trace'][:index])
                                     if e['operation'] == 'issue')
                    self.assertEqual(last_read['input']['number'], args['number'])
                # Whitelist changed paths; everything else must compare deeply equal.
                if name == 'A-create':
                    del after['items']['1']
                    after['pages'] = copy.deepcopy(before['pages'])
                else:
                    for t in self.writes(name):
                        n = str(t['input']['number'])
                        key = 'priority' if t['operation'] == 'set_priority' else 'comments'
                        self.assertIn(t['operation'], {'set_priority','create_comment','update_comment'})
                        after['items'][n][key] = copy.deepcopy(before['items'][n][key])
                self.assertEqual(after, before, 'Unexpected change to fields/options, stage, Team, labels, views or other items')

    def test_mutation_readbacks_and_visible_reasoned_annotations(self):
        for name, result in self.results.items():
            rows = self.rows(name)
            for t in self.writes(name):
                if t['operation'] not in {'create_comment', 'update_comment', 'set_priority'}:
                    continue
                with self.subTest(name=name, operation=t['operation']):
                    args = t['input']
                    n = args['number']
                    index = result['trace'].index(t)
                    later_reads = [e for e in result['trace'][index+1:]
                                   if e['operation'] == 'issue' and e['input']['number'] == n]
                    self.assertTrue(later_reads)
                    if name == 'G-readback-failed':
                        self.assertIn('error', later_reads[-1])
                        self.assertIn('Blocked readback', result['completion'])
                        continue
                    value = later_reads[-1]['output']
                    self.assertEqual(value['stage'], 'Backlog')
                    if t['operation'] == 'set_priority':
                        self.assertEqual(value['priority'], args['option_id'])
                    else:
                        comments = [c for c in value['comments'] if c['body'] == args['body']]
                        self.assertEqual(len(comments), 1)
                        self.assertTrue(comments[0]['url'].startswith(value['url']+'#'))
                        self.assertIn(comments[0]['url'], rows[n]['verified_annotation_urls'])
                        self.assertTrue(args['body'].startswith(MARKER))
                        self.assertEqual(args['body'], annotation(rows[n]))
                        for key in ('priority','reason','evidence','severity','confidence','unknowns',
                                    'urgency','effort','next_action','order','dependencies','overlap'):
                            self.assertIn(key, rows[n])

    def test_new_ticket_requires_followup_and_carries_priority_evidence(self):
        self.assertEqual(self.writes('A-findings'), [])
        self.assertEqual([r['priority'] for r in self.results['A-findings']['readout']], ['P1 High','P3 Low'])
        result = self.results['A-create']
        self.assertEqual(set(result['after']['items']), {'1'})
        issue = result['after']['items']['1']
        self.assertEqual((issue['stage'],issue['priority']), ('Backlog','high-test'))
        self.assertIn('Acceptance criteria', issue['body'])
        for text in ('P1 High','two reproduction attempts','unknown','confidence'):
            self.assertIn(text, issue['body'])
        self.assertEqual(len(self.writes('A-create','create_issue')), 1)

    def test_mixed_backlog_priorities_uncertainty_manual_order_and_all_pages(self):
        rows = self.rows('B-mixed')
        self.assertEqual({n:rows[n]['priority'] for n in (10,11,12,13)},
                         {10:'P1 High',11:'P2 Normal',12:'P3 Low',13:'Needs evidence'})
        self.assertEqual([r['number'] for r in sorted((r for r in rows.values() if r['order']),
                                                    key=lambda r:r['order'])], [14,10,11,12])
        self.assertEqual(rows[11]['severity'], 'major')
        self.assertIn('safe conversion', rows[11]['reason'])
        self.assertEqual(rows[13]['confidence'], 'low')
        self.assertIn('Urgent investigation first', rows[13]['next_action'])
        self.assertIsNone(rows[13]['order'])
        self.assertIn('P3 Low', rows[14]['priority'])
        for n in (10,11,12):
            self.assertEqual((rows[n]['effort'],rows[n]['urgency']), ('unknown','unknown'))
        result = self.results['B-mixed']
        pages = [t['output'] for t in result['trace'] if t['operation']=='items']
        self.assertEqual(len(pages),2)
        self.assertFalse(pages[-1]['pageInfo']['hasNextPage'])
        self.assertEqual(sum(len(p['nodes']) for p in pages),11)
        self.assertIn('#17 stage unknown', result['completion'])
        self.assertEqual({t['input']['number'] for t in self.writes('B-mixed','set_priority')}, {10,11,12})
        self.assertEqual({t['input']['number'] for t in self.writes('B-mixed','create_comment')}, set(range(10,15)))

    def test_dependencies_overlap_and_manual_conflicting_order(self):
        rows = self.rows('C-dependencies')
        self.assertLess(rows[21]['order'],rows[20]['order'])
        self.assertEqual(rows[21]['priority'],'P1 High')
        self.assertIn('elevation',rows[21]['reason'])
        self.assertEqual(rows[22]['overlap'],[20])
        self.assertIn('no independent impact',rows[22]['reason'])
        for n in (23,24,25,26):
            self.assertIsNone(rows[n]['order'])
            self.assertIn('blocked',rows[n]['next_action'])
        self.assertIn('closed',rows[28]['reason'])
        self.assertIsNotNone(rows[28]['order'])
        manual = self.rows('C-manual-order')[20]
        self.assertEqual(manual['order'],1)
        self.assertIn('delivery blocked by #21',manual['next_action'])
        self.assertIn('alternative #21 then #20',manual['next_action'])

    def test_missing_evidence_and_unsupported_mappings_use_comments_only(self):
        for name in ('D-missing-evidence','D-ambiguous','D-critical'):
            self.assertEqual({t['operation'] for t in self.writes(name)}, {'create_comment'})
            self.assertIsNone(self.results[name]['after']['items']['30']['priority'])
        row = self.rows('D-missing-evidence')[30]
        self.assertEqual((row['priority'],row['confidence'],row['effort']),('Needs evidence','low','unknown'))
        self.assertIn('timing sample',row['next_action'])
        self.assertIn('ambiguous',self.rows('D-ambiguous')[30]['reason'])
        row = self.rows('D-critical')[30]
        self.assertEqual((row['priority'],row['severity']),('P0 Immediate','critical'))

    def test_manual_choices_provenance_and_sourced_effort(self):
        result = self.results['E-manual-provenance']
        values = {int(n):i['priority'] for n,i in result['after']['items'].items()}
        self.assertEqual(values,{40:'normal-test',41:None,42:'high-test',43:'normal-test',44:'high-test',45:'normal-test',46:'normal-test'})
        self.assertEqual({t['input']['number'] for t in self.writes('E-manual-provenance','set_priority')},{43,45,46})
        self.assertEqual(len(result['after']['items']['43']['comments']),1)
        rows = self.rows('E-manual-provenance')
        self.assertLess(rows[45]['order'], rows[46]['order'])
        self.assertIn('maintainer estimate',rows[45]['effort'])
        self.assertIn('maintainer estimate',rows[46]['effort'])
        self.assertTrue(all(rows[n]['effort']=='unknown' for n in range(40,45)))

    def test_advice_empty_incomplete_repeats_and_conflicts_never_write(self):
        for name in ('F-advice','F-empty','F-incomplete','G-repeat','G-conflicts','G-cleared'):
            self.assertEqual(self.writes(name),[],name)
        self.assertIn('incomplete',self.results['F-incomplete']['completion'])
        self.assertTrue(any('error' in t for t in self.results['F-incomplete']['trace']))
        self.assertEqual(self.results['G-repeat']['before'],self.results['B-mixed']['after'])
        self.assertIsNone(self.results['G-cleared']['after']['items']['10']['priority'])
        self.assertEqual(self.results['G-cleared']['prior_receipts']['10']['priority'],'high-test')
        for text in ('Ready','concurrently','edited comment','uncertain ownership','manual clear'):
            self.assertIn(text,self.results['G-conflicts']['completion'])

    def test_ambiguous_and_failed_writes_keep_evidence_without_retry(self):
        self.assertEqual(len(self.writes('G-timeout','create_comment')),1)
        result = self.results['G-timeout']
        self.assertEqual(len(result['after']['items']['30']['comments']),1)
        self.assertTrue(any('error' in t for t in result['trace']))
        self.assertIn('no retry',result['completion'])
        result = self.results['G-readback-failed']
        self.assertEqual(len(self.writes('G-readback-failed','create_comment')),1)
        self.assertEqual(len(result['after']['items']['30']['comments']),1)
        self.assertIn('not verified',result['completion'])
        self.assertIn('Retain write receipt',result['completion'])
        self.assertEqual(self.rows('G-readback-failed')[30]['verified_annotation_urls'], [])

    def test_fake_refuses_writes_outside_scenario_authority(self):
        initial = self.cases['B-mixed']['before']
        fake = FakeGitHub(initial)
        self.assertIn('error', fake.call('create_comment', number=10, body='not authorized'))
        self.assertEqual(fake.state, initial)


if __name__ == '__main__':
    unittest.main()
