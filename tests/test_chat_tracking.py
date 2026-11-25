"""Chat milestones use a local outbox and fake Project reads/writes."""
from contextlib import redirect_stderr, redirect_stdout
import copy
import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

import test_factory as fixtures
import sfc
from test_pickup import PickupGitHub, ticket
from sfc_lib import autostart, chat_tracking
from sfc_lib.common import Halt, common_dir, exclusive, git, load_config, read_json, run, write_json
from sfc_lib.github import GitHub
from sfc_lib.session_runtime import SessionRuntime, request_attachment
from sfc_lib.ticket_coordination import ticket_directory


class TrackingGitHub(PickupGitHub):
    chat_closing_pr = GitHub.chat_closing_pr
    chat_pr = GitHub.chat_pr
    verify_chat_link_visibility = GitHub.verify_chat_link_visibility

    def __init__(self, root, cards):
        super().__init__(root, cards)
        self.comments = []
        self.lose_note_response = False
        self.pull = {'number': 41, 'baseRefName': 'main', 'state': 'OPEN', 'isDraft': False,
                     'url': 'https://github.com/example/app/pull/41', 'statusCheckRollup': []}
        self.pr_reads = []
        self.issue_visibility = 'PRIVATE'
        self.pr_visibility = 'PUBLIC'
        self.closing_links = {'totalCount': 0, 'nodes': []}
        self.closer = None
        self.native_refs = []
        self.native_writes = []
        self.lose_attachment_response = False
        self.skip_attachment_write = False

    def chat_pr_attachment(self, issue, repository, number):
        pr_id = repository + ':' + str(number)
        references = self.native_refs.copy()
        if any(pr.get('number') == number for pr in self.closing_links['nodes']) and pr_id not in references:
            references.append(pr_id)
        return {'issue': issue['id'], 'pull_request': pr_id,
                'url': f'https://github.com/{repository}/pull/{number}', 'base': self.c['base_branch'],
                'references': sorted(references)}

    def attach_chat_pr(self, issue_id, pr_id):
        self.native_writes.append((issue_id, pr_id))
        if not self.skip_attachment_write:
            self.native_refs.append(pr_id)
        if self.lose_attachment_response:
            raise Halt('Fixture lost the native attachment response')

    def graphql(self, query, variables):
        if 'issueRepo:repository' in query:
            return {
                'issueRepo': {'nameWithOwner': variables['issueOwner'] + '/' + variables['issueName'],
                              'visibility': self.issue_visibility},
                'prRepo': {'nameWithOwner': variables['prOwner'] + '/' + variables['prName'],
                           'visibility': self.pr_visibility},
            }
        if 'closedByPullRequestsReferences' in query:
            issue = next(item for item in self.items if item['id'] == variables['issue'])
            return {'node': {**issue, 'closedByPullRequestsReferences': self.closing_links,
                             'timelineItems': {'nodes': [{'closer': self.closer}]}}}
        return super().graphql(query, variables)

    def move(self, card, status):
        fixtures.FakeGitHub.move(self, card, status)

    def call(self, *args, **kwargs):
        if args[:2] == ('pr', 'view'):
            number, repository = args[2], args[4]
            self.pr_reads.append(number if repository == self.c['repo'] else (repository, number))
            return self.pull.copy()
        if args[0] == 'api' and '/comments' in args[1]:
            if '--method' not in args:
                return [self.comments.copy()]
            note = {'id': len(self.comments) + 1, **json.loads(kwargs['input'])}
            self.comments.append(note)
            if self.lose_note_response:
                raise Halt('Fixture dropped the note response')
            return note
        return super().call(*args, **kwargs)


class ChatTrackingTests(unittest.TestCase):
    tearDown = fixtures.IntegrationTests.tearDown

    def setUp(self):
        fixtures.IntegrationTests.setUp(self)
        write_json(self.root / '.sfc/config.json', self.c)
        self.c = load_config(self.root)
        self.gh = TrackingGitHub(self.root, [ticket(1, status='Backlog')])
        self.gh.c = self.c
        self.task = 'chat-example-feature'

    def start(self, *, issue=1, delivery='pr-required'):
        return chat_tracking.start(self.root, self.c, task=self.task, title='Add export',
                                   summary='Export the selected rows', issue=issue, work_type='Feature',
                                   acceptance=['Selected rows export to CSV'], delivery=delivery)

    def update(self, event_id, stage, **values):
        return chat_tracking.update(self.root, self.c, task=self.task, event_id=event_id, stage=stage,
                                    summary='Observed ' + stage, **values)

    def sync(self):
        return chat_tracking.sync(self.gh, self.root, self.task)

    def test_uncertain_routing_done_is_readback_only_with_delayed_visibility(self):
        self.uncertain_done_field('routing')

    def test_uncertain_standard_done_is_readback_only_with_delayed_visibility(self):
        self.uncertain_done_field('standard')

    def uncertain_done_field(self, field):
        self.task = 'uncertain-done-' + field
        self.gh.items[0]['status'] = 'Backlog'
        self.gh.github_status['item-1'] = 'in-progress'
        self.gh.pull['state'] = 'OPEN'
        self.start(); self.update('review', 'Review', pr=41); self.sync()
        self.gh.pull['state'] = 'MERGED'
        attempts = []
        def unknown(*args):
            attempts.append(args)
            raise Halt('Fixture lost field response; readback still shows old value')
        with patch.object(self.gh, 'move' if field == 'routing' else 'write_done_status', side_effect=unknown):
            self.assertEqual(self.sync()['state'], 'pending')
            second = self.sync()
            self.assertEqual(second['state'], 'pending')
            self.assertIn('readback', second['detail'])
        self.assertEqual(len(attempts), 1)
        if field == 'routing':
            self.gh.items[0]['status'] = 'Done'
        else:
            self.gh.github_status['item-1'] = 'done'
        self.assertEqual(self.sync()['state'], 'synced')
        self.assertEqual(self.gh.github_status['item-1'], 'done')


    def test_guard_rejected_unsent_note_can_retry_but_unknown_post_is_readback_only(self):
        self.start()
        path = chat_tracking.task_path(self.root, self.task)
        record = read_json(path)
        record['issue'] = {'number': 1}
        event = {'id': 'done-note', 'stage': 'Done', 'summary': 'GitHub verified merge'}
        record['events'].append(event)
        with self.assertRaisesRegex(Halt, 'guard rejected'):
            chat_tracking.sync_note(self.gh, path, record, event, None,
                                    guard=lambda: (_ for _ in ()).throw(Halt('guard rejected')))
        self.assertEqual(read_json(path)['events'][-1]['note'], 'unsent')
        self.assertEqual(self.gh.comments, [])
        self.gh.lose_note_response = True
        with self.assertRaisesRegex(Halt, 'dropped'):
            chat_tracking.sync_note(self.gh, path, record, event, None)
        self.assertEqual(read_json(path)['events'][-1]['note'], 'intent')
        self.assertEqual(len(self.gh.comments), 1)
        self.gh.lose_note_response = False
        chat_tracking.sync_note(self.gh, path, record, event, None)
        self.assertEqual(event['note'], 'verified')
        self.assertEqual(len(self.gh.comments), 1)

    def test_records_a_single_issue_and_tracks_only_observed_milestones(self):
        self.gh.github_status['item-1'] = 'in-progress'
        with exclusive(self.root):
            self.assertEqual(self.start()['latest_summary'], 'Export the selected rows')
            self.start()
            self.update('tests', 'QA')
        self.assertEqual(self.gh.reads + self.gh.writes + self.gh.comments, [])
        self.assertEqual(self.sync()['board_stage'], 'QA')
        self.update('opened', 'Review', pr=41)
        self.update('finished', 'Done')
        pending = self.sync()
        self.assertEqual(pending['board_stage'], 'Review')
        self.assertEqual(pending['latest_summary'], 'Observed Done')
        self.assertEqual(pending['pending'], ['finished'])
        self.gh.pull['state'] = 'MERGED'
        done = self.sync()
        self.assertEqual((done['state'], done['board_stage']), ('synced', 'Done'))
        self.assertEqual(self.gh.writes, [(1, 'Building'), (1, 'QA'), (1, 'Review'), (1, 'Done')])
        self.assertEqual(self.gh.github_status['item-1'], 'done')
        self.assertIn('https://github.com/example/app/pull/41', self.gh.comments[-1]['body'])
        self.assertEqual(self.proc.lanes, [])

    def test_draft_ci_and_manual_merge_sync_both_status_fields(self):
        self.gh.github_status['item-1'] = 'in-progress'
        git(self.root, 'switch', '-c', 'topic')
        (self.root / 'value.txt').write_text('Ready to merge\n')
        git(self.root, 'add', 'value.txt')
        git(self.root, 'commit', '-m', 'Implement selected export')
        candidate = git(self.root, 'rev-parse', 'HEAD')
        git(self.root, 'switch', 'main')
        self.gh.pull.update(isDraft=True, headRefOid=candidate,
                            statusCheckRollup=[{'status': 'IN_PROGRESS', 'conclusion': ''}])

        self.start()
        self.update('draft', 'QA', pr=41)
        qa = self.sync()
        self.assertEqual(qa['board_stage'], 'QA')
        self.assertEqual((qa['pull_request']['draft'], qa['pull_request']['ci']), (True, 'running'))
        self.assertIn(self.gh.pull['url'], self.gh.comments[-1]['body'])
        self.assertEqual(self.gh.pr_reads, [41])

        self.gh.pull.update(isDraft=False, statusCheckRollup=[{'status': 'COMPLETED', 'conclusion': 'FAILURE'}])
        self.update('reviewed', 'Review')
        self.assertEqual(self.sync()['pull_request']['ci'], 'failed')
        self.gh.pull['state'] = 'CLOSED'
        closed = self.sync()
        self.assertEqual((closed['board_stage'], closed['pull_request']['state']), ('Review', 'CLOSED'))
        self.assertEqual(self.gh.github_status['item-1'], 'in-progress')

        # The operator merges the temporary branch. GitHub's adapter observes the result.
        git(self.root, 'merge', '--no-ff', 'topic', '-m', 'Manually merge export')
        run(['git', 'merge-base', '--is-ancestor', candidate, 'HEAD'], self.root)
        self.gh.pull.update(state='MERGED', statusCheckRollup=[{'status': 'COMPLETED', 'conclusion': 'SUCCESS'}])
        done = self.sync()
        self.assertEqual((done['state'], done['pending'], done['pull_request']['ci']), ('synced', [], 'passed'))
        self.assertEqual(self.gh.items[0]['status'], 'Done')
        self.assertEqual(self.gh.github_status['item-1'], 'done')
        self.assertEqual(self.gh.github_status_writes, ['item-1'])
        self.assertEqual(self.gh.pr_reads, [41, 41, 41, 41])
        self.assertEqual(self.gh.merges, [])
        self.assertEqual(self.proc.lanes, [])

        notes = self.gh.comments.copy()
        self.assertEqual(self.sync(), done)
        self.assertEqual(self.gh.comments, notes)
        self.assertEqual(self.gh.pr_reads, [41, 41, 41, 41])

    def test_partial_merge_stays_open_and_can_link_the_actual_delivery_pr(self):
        self.start()
        self.update('partial', 'QA', pr=41, milestone_only=True)
        self.sync(); self.gh.pull['state'] = 'MERGED'
        partial = self.sync()
        self.assertEqual((partial['board_stage'], partial['pr_role']), ('QA', 'milestone'))
        self.assertNotIn((1, 'Done'), self.gh.writes)
        with self.assertRaisesRegex(Halt, 'milestone-only'):
            self.update('premature', 'Done')
        self.gh.pull.update(number=42, url='https://github.com/example/app/pull/42', state='OPEN')
        self.update('delivery', 'Review', pr=42, closes_issue=True)
        self.sync()
        record = read_json(chat_tracking.task_path(self.root, self.task))
        self.assertEqual((record['pr'], record['milestone_prs'][0]['pr']), (42, 41))
        self.assertEqual(record['milestone_prs'][0]['pull_request']['state'], 'MERGED')
        self.gh.pull['state'] = 'MERGED'
        self.assertEqual(self.sync()['board_stage'], 'Done')

    def test_partial_pr_with_uncertain_notes_cannot_be_replaced(self):
        self.start(); self.update('partial', 'QA', pr=41, milestone_only=True)
        self.gh.pull['state'] = 'MERGED'; self.gh.lose_note_response = True
        self.assertEqual(self.sync()['state'], 'pending')
        with self.assertRaisesRegex(Halt, 'already tied'):
            self.update('delivery', 'Review', pr=42, closes_issue=True)
        record = read_json(chat_tracking.task_path(self.root, self.task))
        self.assertEqual(record['pr'], 41)
        self.assertNotIn('milestone_prs', record)

    def test_milestone_pr_does_not_authorize_issue_closure(self):
        self.start()
        self.update('partial', 'QA', pr=41)
        self.assertEqual(self.sync()['board_stage'], 'QA')
        self.assertEqual(self.gh.native_writes, [])
        self.update('complete-scope', 'Review', closes_issue=True)
        self.sync()
        self.assertEqual(len(self.gh.native_writes), 1)

    def test_closure_intent_requires_a_recorded_pr(self):
        self.start()
        with self.assertRaises(Halt):
            self.update('closure-without-pr', 'QA', closes_issue=True)
        self.assertEqual(len(read_json(chat_tracking.task_path(self.root, self.task))['events']), 1)

    def test_explicit_sync_detects_unlink_after_done_without_reopening_or_rewriting(self):
        self.start()
        self.update('draft', 'QA', pr=41, closes_issue=True)
        self.sync()
        self.gh.pull['state'] = 'MERGED'
        self.assertEqual(self.sync()['board_stage'], 'Done')
        notes, writes = self.gh.comments.copy(), self.gh.writes.copy()
        self.gh.native_refs.clear()
        checked = self.sync()
        self.assertEqual(checked['board_stage'], 'Done')
        self.assertEqual(checked['native_pr_attachment']['state'], 'pending')
        self.assertEqual((self.gh.comments, self.gh.writes), (notes, writes))
        self.assertEqual(len(self.gh.native_writes), 1)

    def test_native_attachment_is_saved_and_repeated_sync_does_not_write_again(self):
        self.start()
        self.update('draft', 'QA', pr=41, closes_issue=True)
        self.assertEqual(self.sync()['state'], 'synced')
        self.assertEqual(self.gh.native_writes, [(self.gh.items[0]['id'], 'example/app:41')])
        self.assertEqual(self.sync()['state'], 'synced')
        self.assertEqual(len(self.gh.native_writes), 1)

    def test_lost_attachment_response_is_read_back_without_a_second_write(self):
        self.start()
        self.update('draft', 'QA', pr=41, closes_issue=True)
        self.gh.lose_attachment_response = True
        waiting = self.sync()
        self.assertEqual((waiting['state'], waiting['board_stage']), ('synced', 'QA'))
        self.assertEqual(waiting['native_pr_attachment']['state'], 'pending')
        self.gh.lose_attachment_response = False
        self.assertEqual(self.sync()['state'], 'synced')
        self.assertEqual(len(self.gh.native_writes), 1)

    def test_missing_attachment_readback_preserves_the_original_intent(self):
        self.start()
        self.update('draft', 'QA', pr=41, closes_issue=True)
        self.gh.skip_attachment_write = True
        self.assertEqual(self.sync()['native_pr_attachment']['state'], 'pending')
        self.gh.skip_attachment_write = False
        self.assertEqual(self.sync()['native_pr_attachment']['state'], 'pending')
        self.assertEqual(len(self.gh.native_writes), 1)

    def test_external_native_unlink_is_preserved(self):
        self.start()
        self.update('draft', 'QA', pr=41, closes_issue=True)
        self.assertEqual(self.sync()['state'], 'synced')
        self.gh.native_refs.clear()
        self.assertEqual(self.sync()['native_pr_attachment']['state'], 'pending')
        self.assertEqual(len(self.gh.native_writes), 1)

    def test_removed_attachment_does_not_block_verified_milestones_or_merge_completion(self):
        self.start()
        self.update('draft', 'QA', pr=41, closes_issue=True)
        self.sync()
        self.gh.native_refs.clear()
        self.update('reviewed', 'Review')
        review = self.sync()
        self.assertEqual(review['board_stage'], 'Review')
        self.assertEqual(review['native_pr_attachment']['state'], 'pending')
        self.gh.pull['state'] = 'MERGED'
        done = self.sync()
        self.assertEqual((done['state'], done['board_stage']), ('synced', 'Done'))
        self.assertEqual(done['native_pr_attachment']['state'], 'pending')
        self.assertEqual(self.sync()['board_stage'], 'Done')
        self.assertEqual(len(self.gh.native_writes), 1)
        self.gh.native_refs.append('example/app:41')
        self.assertEqual(self.sync()['native_pr_attachment']['state'], 'verified')

    def test_ten_existing_native_attachments_are_preserved(self):
        self.start()
        self.update('draft', 'QA', pr=41, closes_issue=True)
        self.gh.native_refs = ['other-' + str(number) for number in range(10)]
        self.assertEqual(self.sync()['native_pr_attachment']['state'], 'pending')
        self.assertEqual(self.gh.native_writes, [])

    def test_native_reference_drift_before_write_does_not_attach(self):
        self.start()
        self.update('draft', 'QA', pr=41, closes_issue=True)
        observe = self.gh.chat_pr_attachment
        reads = 0

        def changed(*args):
            nonlocal reads
            reads += 1
            value = observe(*args)
            if reads == 2:
                value['references'].append('concurrent-pr')
            return value

        with patch.object(self.gh, 'chat_pr_attachment', side_effect=changed):
            self.assertEqual(self.sync()['native_pr_attachment']['state'], 'pending')
        self.assertEqual(self.gh.native_writes, [])

    def test_native_attachment_transport_checks_identity_and_pagination(self):
        issue = {'id': 'I1', 'number': 1}
        payload = {'issue': {**issue, 'repository': {'nameWithOwner': 'example/app'},
                             'closedByPullRequestsReferences': {'totalCount': 0, 'nodes': [],
                                                               'pageInfo': {'hasNextPage': False}}},
                   'repository': {'nameWithOwner': 'example/code', 'pullRequest': {
                       'id': 'PR41', 'number': 41, 'url': 'https://github.com/example/code/pull/41',
                       'baseRefName': 'main'}}}
        with patch.object(self.gh, 'graphql', side_effect=lambda *args: copy.deepcopy(payload)):
            observed = GitHub.chat_pr_attachment(self.gh, issue, 'example/code', 41)
            self.assertEqual(observed['pull_request'], 'PR41')
            payload['issue']['closedByPullRequestsReferences']['pageInfo']['hasNextPage'] = True
            with self.assertRaisesRegex(Halt, 'complete reference inventory'):
                GitHub.chat_pr_attachment(self.gh, issue, 'example/code', 41)
            payload['issue']['closedByPullRequestsReferences']['pageInfo']['hasNextPage'] = False
            payload['repository']['pullRequest']['baseRefName'] = 'other'
            with self.assertRaisesRegex(Halt, 'identity'):
                GitHub.chat_pr_attachment(self.gh, issue, 'example/code', 41)

    def test_verified_manual_merge_retries_an_interrupted_status_readback(self):
        self.gh.github_status['item-1'] = 'in-progress'
        self.start()
        self.update('reviewed', 'Review', pr=41)
        self.sync()
        self.gh.pull['state'] = 'MERGED'
        write = self.gh.write_done_status

        def drop_response(scope):
            write(scope)
            raise Halt('Fixture lost the status response after the update')

        with patch.object(self.gh, 'write_done_status', side_effect=drop_response):
            self.assertEqual(self.sync()['state'], 'pending')
        done = self.sync()
        self.assertEqual((done['state'], done['board_stage'], done['pending']), ('synced', 'Done', []))
        self.assertEqual(self.gh.github_status['item-1'], 'done')
        self.assertEqual(self.gh.github_status_writes, ['item-1'])
        self.assertEqual(self.gh.writes.count((1, 'Done')), 1)

    def test_merge_observation_preserves_the_board_for_a_different_base(self):
        self.start()
        self.update('reviewed', 'Review', pr=41)
        self.sync()
        self.gh.pull.update(state='MERGED', baseRefName='develop')
        self.assertEqual(self.sync()['state'], 'pending')
        self.assertEqual(self.gh.items[0]['status'], 'Review')

    def test_cross_repo_draft_links_on_issue_and_reconciles_manual_merge(self):
        url = 'https://github.com/example/public-app/pull/41'
        self.gh.github_status['item-1'] = 'in-progress'
        self.gh.pull.update(url=url, isDraft=True, headRefOid='a' * 40,
                            statusCheckRollup=[{'status': 'IN_PROGRESS', 'conclusion': ''}])
        self.start()
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(sfc.main(['--root', str(self.root), 'track', 'update',
                                       '--task', self.task, '--event', 'draft', '--stage', 'QA',
                                       '--summary', 'Observed QA', '--pr-url', url]), 0)
        self.assertEqual(json.loads(output.getvalue())['pr_url'], url)
        with patch.object(self.gh, 'call', wraps=self.gh.call) as calls:
            self.gh.lose_note_response = True
            self.assertEqual(self.sync()['pending'], ['draft'])
            self.gh.lose_note_response = False
            qa = self.sync()
        self.assertEqual((qa['state'], qa['board_stage'], qa['pr_url']), ('synced', 'QA', url))
        self.assertEqual((qa['pull_request']['draft'], qa['pull_request']['ci']), (True, 'running'))
        self.assertEqual(self.gh.pr_reads, [('example/public-app', 41)] * 2)
        self.assertEqual(len(self.gh.comments), 1)
        self.assertIn(url, self.gh.comments[0]['body'])
        posts = [call.args[:4] for call in calls.call_args_list if '--method' in call.args and 'POST' in call.args]
        self.assertEqual(posts, [('api', 'repos/example/app/issues/1/comments', '--method', 'POST')])

        self.update('reviewed', 'Review')
        self.gh.pull.update(state='CLOSED', isDraft=False)
        self.assertEqual(self.sync()['board_stage'], 'Review')
        self.assertEqual(self.gh.github_status['item-1'], 'in-progress')
        self.gh.pull.update(state='MERGED', statusCheckRollup=[{'status': 'COMPLETED', 'conclusion': 'SUCCESS'}])
        done = self.sync()
        self.assertEqual((done['state'], done['board_stage'], done['pending']), ('synced', 'Done', []))
        self.assertEqual(self.gh.github_status['item-1'], 'done')
        self.assertEqual(self.gh.merges, [])
        self.assertEqual(self.sync(), done)

    def test_pr_url_refuses_wrong_repository_or_target_branch(self):
        self.start()
        self.sync()
        url = 'https://github.com/example/public-app/pull/41'
        self.update('draft', 'QA', pr_url=url)
        self.gh.pull['state'] = 'MERGED'
        self.assertEqual(self.sync()['board_stage'], 'Building')
        self.gh.pull.update(url=url, baseRefName='develop')
        self.assertEqual(self.sync()['board_stage'], 'Building')
        self.assertEqual(self.gh.writes, [(1, 'Building')])
        self.assertEqual(self.gh.comments, [])
        with self.assertRaisesRegex(Halt, 'another pull request'):
            self.update('other', 'QA', pr=41)

    def test_pr_url_is_exact_and_cannot_change_a_local_delivery(self):
        self.start(delivery='local-only')
        for value in ('https://git.example.com/example/app/pull/41',
                      'https://github.com/example/app/pull/41?token=fixture',
                      'https://github.com/example/app/issues/41',
                      'https://github.com/example/../pull/41'):
            with self.subTest(url=value), self.assertRaisesRegex(Halt, 'exact https'):
                self.update('draft', 'QA', pr_url=value)
        with self.assertRaisesRegex(Halt, 'local-only'):
            self.update('draft', 'QA', pr_url='https://github.com/example/public-app/pull/41')
        self.assertEqual(self.gh.pr_reads, [])

    def test_public_issue_cannot_publish_a_private_pr_link(self):
        self.start()
        self.sync()
        url = 'https://github.com/example/other-app/pull/41'
        self.gh.pull.update(url=url, isDraft=True)
        self.gh.issue_visibility = 'PUBLIC'
        self.gh.pr_visibility = 'PRIVATE'
        self.update('draft', 'QA', pr_url=url)
        self.assertEqual(self.sync()['pending'], ['draft'])
        self.assertEqual(self.gh.writes, [(1, 'Building')])
        self.assertEqual(self.gh.comments, [])
        self.gh.pr_visibility = 'PUBLIC'
        self.assertEqual(self.sync()['board_stage'], 'QA')
        self.assertIn(url, self.gh.comments[0]['body'])

    def set_cross_repo_closure(self):
        pr = {**self.gh.pull, 'id': 'public-pr-41', 'state': 'MERGED', 'isDraft': False,
              'repository': {'nameWithOwner': 'example/public-app'},
              'url': 'https://github.com/example/public-app/pull/41'}
        self.gh.closing_links = {'totalCount': 1, 'nodes': [pr]}
        self.gh.closer = {'id': pr['id']}
        self.gh.items[0]['state'] = 'CLOSED'
        return pr

    def test_verified_cross_repo_closure_syncs_both_status_fields(self):
        self.gh.github_status['item-1'] = 'done'
        self.start()
        self.update('reviewed', 'Review')
        self.assertEqual(self.sync()['board_stage'], 'Review')
        pr = self.set_cross_repo_closure()

        result = self.sync()
        self.assertEqual((result['state'], result['board_stage'], result['pending']), ('synced', 'Done', []))
        self.assertEqual(result['pull_request']['url'], pr['url'])
        self.assertIsNone(result['pr'])
        self.assertEqual(self.gh.items[0]['status'], 'Done')
        self.assertEqual(self.gh.github_status['item-1'], 'done')
        self.assertEqual(self.gh.github_status_writes, [])
        self.assertIn(pr['url'], self.gh.comments[-1]['body'])
        self.assertEqual(self.gh.pr_reads, [])
        self.assertEqual(self.gh.merges, [])
        notes = self.gh.comments.copy()
        self.assertEqual(self.sync()['board_stage'], 'Done')
        self.assertEqual(self.gh.comments, notes)
        self.assertEqual(self.gh.writes.count((1, 'Done')), 1)

    def test_inferred_completed_pr_unlink_is_only_an_attachment_warning(self):
        self.start()
        self.update('reviewed', 'Review')
        self.sync()
        pr = self.set_cross_repo_closure()
        self.assertEqual(self.sync()['board_stage'], 'Done')
        self.gh.pull = pr
        self.gh.closing_links = {'totalCount': 0, 'nodes': []}
        self.gh.closer = None
        notes, writes = self.gh.comments.copy(), self.gh.writes.copy()
        result = self.sync()
        self.assertEqual((result['state'], result['board_stage'], result['pending']), ('synced', 'Done', []))
        self.assertEqual(result['native_pr_attachment']['state'], 'pending')
        self.assertEqual((self.gh.comments, self.gh.writes), (notes, writes))
        self.assertEqual(self.gh.native_writes, [])

    def test_inferred_attachment_initial_read_failure_retries_after_done_without_replaying_milestones(self):
        self.start()
        self.update('reviewed', 'Review')
        self.sync()
        pr = self.set_cross_repo_closure()
        with patch.object(self.gh, 'chat_pr_attachment', side_effect=Halt('Fixture read failed')):
            result = self.sync()
        self.assertEqual((result['state'], result['board_stage']), ('synced', 'Done'))
        self.assertEqual(result['native_pr_attachment']['state'], 'pending')
        self.gh.pull = pr
        notes, writes = self.gh.comments.copy(), self.gh.writes.copy()
        links = self.gh.closing_links
        self.gh.closing_links = {'totalCount': 0, 'nodes': []}
        self.assertEqual(self.sync()['native_pr_attachment']['state'], 'pending')
        self.assertEqual(self.gh.native_writes, [])
        self.gh.closing_links = links
        result = self.sync()
        self.assertEqual((result['state'], result['board_stage']), ('synced', 'Done'))
        self.assertEqual(result['native_pr_attachment']['state'], 'verified')
        self.assertEqual((self.gh.comments, self.gh.writes), (notes, writes))

    def test_unverified_closures_preserve_review(self):
        self.start()
        self.update('reviewed', 'Review')
        self.sync()
        failures = ('open issue', 'manual closure', 'multiple links', 'closed PR', 'wrong base', 'wrong URL')
        for failure in failures:
            with self.subTest(failure=failure):
                pr = self.set_cross_repo_closure()
                if failure == 'open issue':
                    self.gh.items[0]['state'] = 'OPEN'
                elif failure == 'manual closure':
                    self.gh.closer = None
                elif failure == 'multiple links':
                    self.gh.closing_links['totalCount'] = 2
                elif failure == 'closed PR':
                    pr['state'] = 'CLOSED'
                elif failure == 'wrong base':
                    pr['baseRefName'] = 'develop'
                else:
                    pr['url'] = 'https://github.com/example/unspecified/pull/41'
                self.assertEqual(self.sync()['board_stage'], 'Review')
        self.assertNotIn((1, 'Done'), self.gh.writes)

    def test_interrupted_cross_repo_completion_rechecks_its_pinned_link(self):
        self.gh.github_status['item-1'] = 'in-progress'
        self.start()
        self.update('reviewed', 'Review')
        self.sync()
        pr = self.set_cross_repo_closure()
        with patch.object(self.gh, 'write_done_status', side_effect=Halt('Temporary failure')):
            self.assertEqual(self.sync()['pending'], ['sfc:pr-merged'])

        self.gh.closer = None
        self.assertEqual(self.sync()['state'], 'pending')
        self.assertEqual(self.gh.github_status['item-1'], 'in-progress')
        self.gh.closer = {'id': pr['id']}
        self.assertEqual(self.sync()['state'], 'pending')  # An unknown field write is never replayed.
        self.gh.github_status['item-1'] = 'done'  # Eventual readback confirms the original intent.
        self.assertEqual(self.sync()['state'], 'synced')
        self.assertEqual(self.gh.github_status['item-1'], 'done')
        self.assertEqual(self.gh.writes.count((1, 'Done')), 1)

    def test_pr_required_decision_is_saved_and_prevents_local_completion(self):
        self.assertEqual(self.start(delivery=None)['delivery'], 'pr-required')
        self.assertEqual(self.start()['delivery'], 'pr-required')
        with self.assertRaisesRegex(Halt, 'cannot change its delivery choice'):
            self.start(delivery='local-only')
        with self.assertRaisesRegex(Halt, 'requires a linked merged pull request'):
            self.update('local', 'Done', local_done=True)
        with self.assertRaisesRegex(Halt, 'requires a linked merged pull request'):
            self.update('done', 'Done')
        self.assertEqual(self.sync()['board_stage'], 'Building')

    def test_default_does_not_replace_explicit_local_delivery_on_resume(self):
        self.start(delivery='local-only')
        self.assertEqual(self.start(delivery=None)['delivery'], 'local-only')
        self.update('local', 'Done', local_done=True)
        self.assertEqual(self.sync()['board_stage'], 'Done')

    def test_pre_upgrade_task_can_resume_without_a_delivery_decision(self):
        self.start(delivery='local-only')
        path = chat_tracking.task_path(self.root, self.task)
        record = read_json(path)
        del record['delivery']
        write_json(path, record)
        self.assertIsNone(self.start(delivery=None)['delivery'])
        self.update('local', 'Done', local_done=True)
        self.assertEqual(self.sync()['board_stage'], 'Done')

    def test_github_failure_queues_updates_and_note_readback_prevents_duplicate(self):
        self.start()
        self.update('tests', 'QA')
        with patch.object(self.gh, 'verify_repository', side_effect=Halt('Fixture GitHub outage')):
            self.assertEqual(self.sync()['pending'], ['start', 'tests'])
        self.gh.lose_note_response = True
        self.assertEqual(self.sync()['pending'], ['tests'])
        self.assertEqual(len(self.gh.comments), 1)
        self.assertEqual(self.sync()['state'], 'synced')
        self.assertEqual(len(self.gh.comments), 1)
        self.assertEqual(self.gh.writes, [(1, 'Building'), (1, 'QA')])

    def test_created_issue_binding_uses_the_same_creation_receipt_after_restart(self):
        self.start(issue=None, delivery='local-only')
        self.start(issue=None, delivery='local-only')

        def create(gh, root, **request):
            self.assertEqual(request['request_id'], 'chat-' + self.task)
            self.assertEqual(request['work_type'], 'Feature')
            self.assertIn('## Delivery\nLocal only.', request['body'])
            path = ticket_directory(root) / (request['request_id'] + '.json')
            write_json(path, {'issue': {'id': gh.items[0]['id'], 'number': 1},
                              'item_id': gh.items[0]['item_id'], 'project': 'p', 'repo': gh.c['repo']})
            raise Halt('Fixture lost the create response after enrollment')

        with patch('sfc_lib.chat_tracking.create_ticket', side_effect=create) as creation:
            self.assertEqual(self.sync()['state'], 'pending')
            self.assertEqual(self.sync()['issue']['number'], 1)
            self.assertEqual(self.sync()['state'], 'synced')
            creation.assert_called_once()
        self.update('local-fix', 'Done', local_done=True)
        self.assertEqual(self.sync()['board_stage'], 'Done')
        self.assertEqual(self.gh.writes, [(1, 'Building'), (1, 'Done')])

    def test_preserves_an_external_stage_change(self):
        self.start()
        self.sync()
        self.gh.items[0]['status'] = 'Blocked'
        self.update('tests', 'QA')
        self.assertEqual(self.sync()['state'], 'pending')
        self.assertEqual(self.gh.items[0]['status'], 'Blocked')

    def test_cli_records_local_work_without_calling_github(self):
        cli = Path(__file__).resolve().parents[1] / 'scripts/sfc.py'

        def track(*args):
            result = run([sys.executable, '-B', str(cli), '--root', str(self.root), 'track', *args], self.root)
            return json.loads(result.stdout)

        track('start', '--task', self.task, '--type', 'Bug', '--title', 'Fix export',
              '--delivery', 'local-only', '--summary', 'Preserve the selection',
              '--acceptance', 'Export only selected rows')
        track('update', '--task', self.task, '--event', 'verified', '--stage', 'Done',
              '--summary', 'Focused regression test passed', '--local-done')
        status = track('status', '--prefix', 'chat-example')['tasks'][0]
        self.assertEqual(status['desired_stage'], 'Done')
        self.assertEqual(status['delivery'], 'local-only')
        self.assertEqual(status['latest_summary'], 'Focused regression test passed')
        self.assertIsNone(status['board_stage'])
        self.assertEqual(status['pending'], ['start', 'verified'])

    def test_cli_defaults_new_work_to_pr_delivery(self):
        cli = Path(__file__).resolve().parents[1] / 'scripts/sfc.py'
        result = run([sys.executable, '-B', str(cli), '--root', str(self.root),
                      'track', 'start', '--task', self.task, '--type', 'Documentation',
                      '--title', 'Document delivery', '--summary', 'Explain shared delivery',
                      '--acceptance', 'Repository guidance explains delivery'], self.root)
        task = json.loads(result.stdout)
        self.assertEqual(task['delivery'], 'pr-required')
        self.assertEqual(task['desired_stage'], 'Building')
        self.assertIsNone(task['board_stage'])

    def test_chat_prompt_hook_cues_tracking_and_watcher_cannot_start(self):
        event = {'hook_event_name': 'UserPromptSubmit', 'session_id': 'chat-fixture', 'turn_id': 'turn-1',
                 'cwd': str(self.root), 'permission_mode': 'default'}
        context = request_attachment(event, environ={})
        self.assertEqual(context['state'], 'tracking')
        next_turn = request_attachment({**event, 'turn_id': 'turn-2'}, environ={})
        self.assertEqual(next_turn['prefix'], context['prefix'])
        self.assertNotEqual(next_turn['turn'], context['turn'])
        env = {key: value for key, value in os.environ.items() if key not in ('SFC_WORKER', 'SFC_TEST_CONTEXT')}
        command = Path(__file__).resolve().parents[1] / 'hooks/guard.py'
        output = run([sys.executable, str(command), '--attachment'], self.root, input=json.dumps(event), env=env)
        instructions = json.loads(output.stdout)['hookSpecificOutput']['additionalContext']
        self.assertIn('record the task before editing', instructions)
        self.assertIn('Show the task title, delivery choice and verified issue link', instructions)
        self.assertIn('--delivery pr-required', instructions)
        self.assertIn('--delivery local-only', instructions)
        self.assertIn('track update --pr-url', instructions)
        self.assertIn('default for shared-repository changes, including documentation', instructions)
        self.assertIn('without repeated per-PR approval', instructions)
        self.assertIn('configured change-size limit', instructions)
        runtime = SessionRuntime(self.root, environ={})
        self.assertEqual(runtime.attach(event, native_thread=event['session_id'])['state'], 'tracking')
        auth_path = common_dir(self.root) / 'sfc-autostart.json'
        write_json(auth_path, {'version': 2, 'root': str(self.root), 'enabled': True, 'state': 'armed'})
        before = auth_path.read_bytes()
        self.assertEqual(runtime.attach(event, native_thread=event['session_id'])['state'], 'tracking')
        factory = Mock()
        result = autostart.launch({**event, 'hook_event_name': 'SessionStart', 'source': 'startup'},
                                  environ={}, factory_type=factory)
        self.assertEqual(result['state'], 'ignored')
        factory.assert_not_called()
        self.assertFalse((common_dir(self.root) / 'sfc-watch.json').exists())
        self.assertEqual(auth_path.read_bytes(), before)
        self.assertNotIn('workflow_mode', load_config(self.root))
        legacy_arguments = [('run',), ('watch', '--start'), ('autostart', '--enable'),
                            ('recover-ticket', '--issue', '1', '--adopt-pr', '41', '--head', 'a' * 40,
                             '--request-id', 'saved-issue', '--reason', 'test', '--grant-one')]
        for args in legacy_arguments:
            with self.subTest(args=args), patch.object(sfc, 'Factory') as legacy, redirect_stderr(io.StringIO()) as error:
                self.assertEqual(sfc.main(['--root', str(self.root), *args]), 1)
                self.assertIn('your Codex chat', error.getvalue())
                legacy.assert_not_called()

    def test_merge_cli_uses_primary_authority_even_with_a_copied_linked_config(self):
        checkout = self.root.parent / 'linked-copy'
        git(self.root, 'worktree', 'add', '--detach', checkout)
        copied = {**self.c, 'merge_policy': 'auto'}
        write_json(checkout / '.sfc/config.json', copied)
        from sfc_lib import chat_merge
        with patch.object(sfc, 'GitHub') as github, patch.object(chat_merge, 'delivery', return_value={'state': 'fixture'}) as delivery:
            with redirect_stdout(io.StringIO()):
                self.assertEqual(sfc.main(['--root', str(checkout), 'track', 'merge', '--task', self.task,
                    '--worktree', str(checkout), '--head', 'a' * 40, '--base', 'b' * 40, '--prepare']), 0)
        self.assertEqual(github.call_args.args[0], self.root.resolve())
        self.assertEqual(github.call_args.args[1]['merge_policy'], self.c['merge_policy'])
        self.assertEqual(delivery.call_args.args[1], self.root.resolve())

    def test_linked_checkout_tracks_from_the_configured_project_even_if_old_receipt_is_invalid(self):
        checkout = self.root / '.sfc' / 'worktrees' / 'track-local'
        git(self.root, 'worktree', 'add', '--detach', checkout)
        autostart.paths(self.root)[1].write_text('unreadable historical watcher receipt')
        event = {'hook_event_name': 'UserPromptSubmit', 'session_id': 'chat-fixture', 'turn_id': 'turn-1',
                 'cwd': str(checkout), 'permission_mode': 'default'}
        attachment = request_attachment(event, environ={})
        self.assertEqual((attachment['state'], Path(attachment['root'])), ('tracking', self.root.resolve()))

    def test_local_review_can_move_to_done_without_a_pull_request(self):
        self.start(delivery='local-only')
        with self.assertRaisesRegex(Halt, 'local-only'):
            self.update('draft', 'QA', pr=41)
        for event, stage in [('tests', 'QA'), ('review', 'Review'), ('finished', 'Done')]:
            self.update(event, stage, local_done=stage == 'Done')
            self.assertEqual(self.sync()['board_stage'], stage)

    def test_onboarding_sets_up_tracking_with_no_worker_grants_or_codex_cli(self):
        (self.root / '.sfc/config.json').unlink()
        github = Mock()
        github.onboarding_identity.return_value = {'project_id': 'project', 'host': 'github.com',
                                                      'repository_id': 'repo-id', 'repository': 'example/app'}
        github.metadata.return_value = {'id': 'project'}
        github.configure_dashboard.return_value = {'ready': True}
        github.dashboard.return_value = {'ready': True}
        checkout = self.root / '.sfc/worktrees/setup-from-linked'
        git(self.root, 'worktree', 'add', '--detach', checkout)
        arguments = ['--root', str(checkout)]
        onboard = ['onboard', '--repo', 'example/app', '--owner', 'example',
                   '--project', '1', '--base', 'main']
        with patch.object(sfc, 'GitHub', return_value=github), patch.object(sfc, 'Factory') as legacy:
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(sfc.main([*arguments, *onboard, '--preview']), 0)
            preview = json.loads(output.getvalue())
            github.metadata.assert_not_called()
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(sfc.main([*arguments, *onboard, '--expected-review',
                                           preview['setup_review']['review_digest']]), 0)
            self.assertEqual(json.loads(output.getvalue())['merge_policy'], 'auto')
            self.assertIsNone(json.loads(output.getvalue())['chat_merge_authority'])
            with redirect_stdout(io.StringIO()) as output, patch.object(sfc, 'run') as worker:
                self.assertEqual(sfc.main([*arguments, 'doctor']), 0)
                worker.assert_not_called()
            self.assertEqual(json.loads(output.getvalue())['worker_execution'], 'current Codex chat')
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(sfc.main([*arguments, 'onboard-finish']), 0)
            self.assertEqual(json.loads(output.getvalue())['mode'], 'chat')
            self.assertFalse((checkout / '.sfc/config.json').exists())
            github.priority_setup.return_value = {'ready': True}
            github.type_setup.return_value = {'ready': True}
            for command in ('dashboard', 'priority-setup', 'type-setup'):
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(sfc.main([*arguments, command]), 0)
                self.assertEqual(sfc.GitHub.call_args.args[0], self.root.resolve())
            legacy.assert_not_called()
        self.assertEqual(load_config(self.root)['workflow_mode'], 'chat')
        self.assertFalse(autostart.paths(self.root)[1].exists())
        github.metadata.assert_called_once_with(create=True, project_id='project')

    def test_chat_onboarding_retains_only_explicit_merge_scope_and_human_choice(self):
        (self.root / '.sfc/config.json').unlink()
        github = Mock()
        github.onboarding_identity.return_value = {'project_id': 'project', 'host': 'github.com',
                                                      'repository_id': 'repo-id', 'repository': 'example/app'}
        github.metadata.return_value = {'id': 'project'}
        github.configure_dashboard.return_value = {'ready': True}
        command = ['--root', str(self.root), 'onboard', '--repo', 'example/app',
                   '--owner', 'example', '--project', '1', '--base', 'main']
        with patch.object(sfc, 'GitHub', return_value=github):
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(sfc.main([*command, '--preview', '--merge-policy', 'human']), 0)
            preview = json.loads(output.getvalue())
            self.assertEqual(preview['proposed_config']['merge_policy'], 'human')
            self.assertNotIn('chat_merge_authority', preview['proposed_config'])
            with redirect_stderr(io.StringIO()):
                self.assertEqual(sfc.main([*command, '--merge-policy', 'human', '--allow-merge']), 1)
            self.assertFalse((self.root / '.sfc/config.json').exists())
            with redirect_stderr(io.StringIO()):
                self.assertEqual(sfc.main([*command, '--allow-merge']), 1)
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(sfc.main([*command, '--allow-merge', '--preview']), 0)
            automatic = json.loads(output.getvalue())
            with redirect_stderr(io.StringIO()):
                self.assertEqual(sfc.main([*command, '--allow-merge', '--expected-review',
                                           preview['setup_review']['review_digest']]), 1)
            self.assertFalse((self.root / '.sfc/config.json').exists())
            github.onboarding_identity.return_value['repository_id'] = 'replacement'
            with redirect_stderr(io.StringIO()):
                self.assertEqual(sfc.main([*command, '--allow-merge', '--expected-review',
                                           automatic['setup_review']['review_digest']]), 1)
            self.assertFalse((self.root / '.sfc/config.json').exists())
            github.onboarding_identity.return_value['repository_id'] = 'repo-id'
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(sfc.main([*command, '--allow-merge', '--expected-review',
                                           automatic['setup_review']['review_digest']]), 0)
            applied = json.loads(output.getvalue())
        expected = {'repo': 'example/app', 'base_branch': 'main', 'scope': 'user-requested-tasks',
                    'provider': 'atomic-fast-forward', 'host': 'github.com', 'repository_id': 'repo-id'}
        self.assertEqual(applied['chat_merge_authority'], expected)
        self.assertEqual(load_config(self.root)['chat_merge_authority'], expected)

    def test_linked_checkout_cannot_shadow_the_primary_human_merge_policy(self):
        config = {**self.c, 'merge_policy': 'human'}
        write_json(self.root / '.sfc/config.json', config)
        original = (self.root / '.sfc/config.json').read_bytes()
        checkout = self.root / '.sfc/worktrees/onboarding-shadow'
        git(self.root, 'worktree', 'add', '--detach', checkout)
        with patch.object(sfc, 'GitHub') as github, redirect_stderr(io.StringIO()):
            result = sfc.main(['--root', str(checkout), 'onboard', '--repo', 'example/app',
                               '--owner', 'example', '--project', '1', '--base', 'main',
                               '--allow-merge', '--expected-review', 'prior-preview'])
        self.assertEqual(result, 1)
        github.assert_not_called()
        self.assertEqual((self.root / '.sfc/config.json').read_bytes(), original)
        self.assertFalse((checkout / '.sfc/config.json').exists())
