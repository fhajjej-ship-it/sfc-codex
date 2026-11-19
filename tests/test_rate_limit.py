"""Confirmed read limits use fake HTTP/processes/clocks; no live API calls."""
import copy
import json
from pathlib import Path
import subprocess
import threading
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

from sfc_lib.common import Halt, Processes, Stopped, TimedOut, bounded_io
from sfc_lib.github import GitHub
from sfc_lib.rate_limit import (ConnectionReset, ReadConnectionReset, ReadWait, RateLimited,
                                RateWaitStopped, delay, read_only, factory_reads)
from sfc_lib.activity import Activity, ActivityError
import test_factory as fixtures


class RateLimitTests(unittest.TestCase):
    def test_rest_original_retry_after_is_preserved_with_and_without_pagination(self):
        message = {'message': 'You have exceeded a secondary rate limit.'}
        for flags in ((), ('--paginate', '--slurp')):
            with self.subTest(flags=flags):
                self.current, self.gate.until = 1000., 0
                self.waits.clear()
                calls = []
                def transport(argv, *args, **kwargs):
                    calls.append(argv)
                    if 'rate_limit' in argv:
                        return self.result({'resources': {'core': {'remaining': 5000, 'reset': 9999}}})
                    quota = len(calls) == 1
                    body = message if quota else []
                    text = json.dumps(body)
                    if '--include' in argv:
                        text = ('HTTP/1.1 403 Forbidden\r\nretry-after: 120\r\n\r\n' if quota
                                else 'HTTP/1.1 200 OK\r\n\r\n') + text
                    if '--slurp' in argv:
                        text = '[' + text + ']'
                    return subprocess.CompletedProcess([], int(quota), text,
                        'gh: You have exceeded a secondary rate limit. (HTTP 403)' if quota else '')
                with patch('sfc_lib.github.run', side_effect=transport):
                    self.assertEqual(self.gh.call('api', 'repos/example/app/pulls/1/files', *flags),
                                     [[]] if flags else [])
                self.assertEqual(sum(self.waits), 120)
                self.assertEqual(len(calls), 2)
                self.assertTrue(all('--include' in call for call in calls))

    def test_paginated_rest_include_preserves_pages_and_handles_later_quota(self):
        # Observed gh --paginate --slurp --include framing is [HTTP...JSON,HTTP...JSON].
        # A header-looking value inside JSON must remain ordinary page content.
        first = [{'path': 'HTTP/1.1 429, not a header'}]
        second = [{'path': 'second'}]
        success = ('[HTTP/1.1 200 OK\r\nx-ratelimit-remaining: 4\r\n\r\n' + json.dumps(first) +
                   ',HTTP/1.1 200 OK\r\nx-ratelimit-remaining: 3\r\n\r\n' + json.dumps(second) + ']')
        quota = ('[HTTP/1.1 200 OK\r\n\r\n' + json.dumps(first) +
                 ',HTTP/1.1 429 Too Many Requests\r\nretry-after: 120\r\n\r\n' +
                 json.dumps({'message': 'You have exceeded a secondary rate limit.'}) + ']')
        for limited_page in (False, True):
            with self.subTest(limited_page=limited_page):
                self.current, self.gate.until = 1000., 0
                self.waits.clear()
                responses = [subprocess.CompletedProcess([], 0, success, '')]
                if limited_page:
                    responses.insert(0, subprocess.CompletedProcess([], 1, quota,
                        'gh: You have exceeded a secondary rate limit. (HTTP 429)'))
                with patch('sfc_lib.github.run', side_effect=responses) as transport:
                    self.assertEqual(self.gh.call('api', 'repos/example/app/pulls/1/files',
                                                 '--paginate', '--slurp'), [first, second])
                self.assertEqual(transport.call_count, 2 if limited_page else 1)
                self.assertEqual(sum(self.waits), 120 if limited_page else 0)

    def test_paginated_rest_permission_and_malformed_pages_never_retry(self):
        for tail in ('{"message":"Forbidden"}', '{incomplete'):
            body = '[HTTP/1.1 200 OK\r\n\r\n[],HTTP/1.1 403 Forbidden\r\nretry-after: 120\r\n\r\n' + tail + ']'
            result = subprocess.CompletedProcess([], 1, body, 'gh: Forbidden (HTTP 403)')
            with self.subTest(tail=tail), patch('sfc_lib.github.run', return_value=result) as transport:
                with self.assertRaises(Halt):
                    self.gh.call('api', 'repos/example/app/pulls/1/files', '--paginate', '--slurp')
                self.assertEqual(transport.call_count, 1)
                self.assertEqual(self.waits, [])

    def test_rest_unslurped_pages_preserve_headers_and_primary_reset(self):
        first, second = [{'path': 'first'}], [{'path': 'second'}]
        ok = 'HTTP/2.0 200 OK\r\n\r\n'
        quota = ('HTTP/2.0 403 Forbidden\r\nx-ratelimit-remaining: 0\r\n'
                 'x-ratelimit-reset: 1004\r\n\r\n' +
                 json.dumps({'message': 'API rate limit exceeded'}))
        responses = [subprocess.CompletedProcess([], 1, ok + json.dumps(first) + '\n' + quota,
                                                'gh: API rate limit exceeded (HTTP 403)'),
                     subprocess.CompletedProcess([], 0, ok + json.dumps(first) + '\n' +
                                                ok + json.dumps(second), '')]
        with patch('sfc_lib.github.run', side_effect=responses) as transport:
            self.assertEqual(self.gh.call('api', 'repos/example/app/pulls/1/files', '--paginate'),
                             [first, second])
        self.assertEqual(sum(self.waits), 5)
        self.assertEqual(transport.call_count, 2)

    def test_paginated_mixed_errors_and_invalid_framing_never_retry(self):
        ok = 'HTTP/1.1 200 OK\n\n'
        quota = ('HTTP/1.1 429 Too Many Requests\nretry-after: 120\n\n' +
                 json.dumps({'message': 'You have exceeded a secondary rate limit.'}))
        bodies = [
            '[' + ok + '{"errors":[{"type":"FORBIDDEN"}]},' + quota + ']',
            '[' + ok + '{"message":"Forbidden"},' + quota + ']',
            '[' + ok + 'null,' + quota + ']',
            '[' + quota + ',' + ok + '[]]',
            '[' + ok + '[],' + quota + ',]',
            '[' + ok + '[],' + quota,
            '[' + ok + '[],{}]',
            '[' + ok + '[]]' + 'unexpected',
        ]
        for body in bodies:
            result = subprocess.CompletedProcess([], 1, body,
                'gh: You have exceeded a secondary rate limit. (HTTP 429)')
            with self.subTest(body=body), patch('sfc_lib.github.run', return_value=result) as transport:
                with self.assertRaises(Halt):
                    self.gh.call('api', 'repos/example/app/pulls/1/files', '--paginate', '--slurp')
                self.assertEqual(transport.call_count, 1)
                self.assertEqual(self.waits, [])

    def test_original_mixed_or_malformed_failures_never_retry(self):
        quota = {'errors': [{'type': 'RATE_LIMITED'}]}
        cases = [
            self.result(quota, 1, stderr='network connection reset'),
            self.result({**quota, 'message': 'Forbidden'}, 1),
            self.result({**quota, 'data': {'viewer': {'login': 'partial'}}}, 1),
            self.result({'errors': {'type': 'RATE_LIMITED'}}, 1),
            self.result({'errors': [{'type': 'RATE_LIMITED', 'message': 'Forbidden'}]}, 1),
            self.result({'message': 'API rate limit exceeded; network connection reset'}, 1),
            self.result({'message': 'Forbidden: secondary rate limit'}, 1),
            self.result({'message': 'Bad credentials'}, 1, stderr='gh: API rate limit exceeded'),
            self.result([], 1, stderr='gh: API rate limit exceeded'),
            self.result(None, 1, stderr='gh: API rate limit exceeded'),
            subprocess.CompletedProcess([], 1, '{incomplete', 'gh: API rate limit exceeded'),
            subprocess.CompletedProcess([], 1, '', 'gh: API rate limit exceeded\nnetwork connection reset'),
            subprocess.CompletedProcess([], 1, '', 'gh: API rate limit exceeded; Forbidden'),
            subprocess.CompletedProcess([], 1, '', 'GraphQL: API rate limit exceeded, Forbidden'),
        ]
        for args, payload in [(('api', 'graphql', '--input', '-'), json.dumps({'query': 'query { viewer { login } }'})),
                              (('api', 'repos/example/app'), None), (('pr', 'list'), None)]:
            for result in cases:
                with self.subTest(args=args, result=result):
                    self.current, self.gate.until = 1000., 0
                    self.waits.clear()
                    self.reports.clear()
                    if args[:2] == ('api', 'graphql'):
                        result = subprocess.CompletedProcess([], result.returncode,
                            'HTTP/2.0 403 Forbidden\r\nretry-after: 2\r\n\r\n' + result.stdout, result.stderr)
                    with patch('sfc_lib.github.run', side_effect=[result, self.result({})]) as transport:
                        with self.assertRaises(Halt):
                            self.gh.call(*args, input=payload)
                    self.assertEqual(transport.call_count, 1)
                    self.assertEqual(self.waits, [])
                    self.assertEqual(self.reports, [])

    def test_cli_stderr_only_quota_keeps_verified_diagnostic_recovery(self):
        for message in ('gh: API rate limit exceeded for example. (HTTP 403)',
                        'GraphQL: API rate limit already exceeded for user ID 123.',
                        'gh: GraphQL: API rate limit already exceeded for user ID 123.',
                        'gh: You have exceeded a secondary rate limit. (HTTP 429)'):
            with self.subTest(message=message):
                self.current, self.gate.until = 1000., 0
                self.waits.clear()
                original = subprocess.CompletedProcess([], 1, '', message)
                probe = self.result({'data': {'rateLimit': {
                    'remaining': 0, 'resetAt': '1970-01-01T00:16:42Z'}}})
                with patch('sfc_lib.github.run', side_effect=[original, probe, self.result([])]) as transport:
                    self.assertEqual(self.gh.call('pr', 'list'), [])
                self.assertEqual(transport.call_count, 3)
                self.assertEqual(sum(self.waits), 3)

    def test_original_pure_quota_with_cli_echo_still_retries(self):
        for message in ('API rate limit already exceeded for user ID 123.',
                        'You have exceeded a secondary rate limit. '
                        'Please wait a few minutes before you try again.'):
            with self.subTest(message=message):
                self.waits.clear()
                original = self.result({'errors': [{'type': 'RATE_LIMITED', 'message': message}],
                                        'data': None}, 1,
                                       'HTTP/2.0 403 Forbidden\r\nretry-after: 2',
                                       'gh: GraphQL: ' + message)
                with patch('sfc_lib.github.run', side_effect=[original, self.result({'data': {}})]) as transport:
                    self.assertEqual(self.gh.graphql('query { viewer { login } }', {}), {})
                self.assertEqual(transport.call_count, 2)
                self.assertEqual(sum(self.waits), 2)

    def test_failed_or_malformed_diagnostics_never_retry_original_read(self):
        failures = [
            self.result({'message': 'Bad credentials'}, 1),
            self.result({'errors': [{'type': 'FORBIDDEN'}]}),
            self.result({'errors': [{'type': 'RATE_LIMITED'}, {'type': 'FORBIDDEN'}]}),
            subprocess.CompletedProcess([], 1, '', 'network connection reset'),
            subprocess.CompletedProcess([], 0, 'not JSON', ''),
            self.result({}),
            self.result({'resources': {'core': {'remaining': False, 'reset': 1003}},
                         'data': {'rateLimit': {'remaining': False, 'resetAt': '1970-01-01T00:16:43Z'}}}),
            self.result({'message': 'API rate limit exceeded'}, 1, stderr='network connection reset'),
            self.result({'errors': [{'type': 'RATE_LIMITED'}], 'data': {'viewer': {'login': 'partial'}}}),
            self.result({'resources': {'core': {'remaining': 0, 'reset': 'invalid'}},
                         'data': {'rateLimit': {'remaining': 0, 'resetAt': 'invalid'}}}),
            self.result({'resources': {'core': {'remaining': -1, 'reset': 1003}},
                         'data': {'rateLimit': {'remaining': -1, 'resetAt': '1970-01-01T00:16:43Z'}}}),
            self.result(None),
            self.result({'errors': [{'type': 'RATE_LIMITED'}], 'message': 'Forbidden'}, 1),
            self.result({'message': 'Bad credentials'}, 1, 'HTTP/2.0 401 Unauthorized\r\nretry-after: 2'),
            subprocess.CompletedProcess([], 0, 'HTTP/2.0 200 OK\nretry-after: 2', ''),
        ]
        for args in (('api', 'repos/example/app'), ('pr', 'list')):
            for diagnostic in failures:
                with self.subTest(args=args, diagnostic=diagnostic):
                    self.current, self.gate.until = 1000., 0
                    self.waits.clear()
                    self.reports.clear()
                    responses = [self.result({'message': 'API rate limit exceeded'}, 1),
                                 diagnostic, self.result([])]
                    with patch('sfc_lib.github.run', side_effect=responses) as transport:
                        with self.assertRaises(Halt):
                            self.gh.call(*args)
                    self.assertEqual(transport.call_count, 2)
                    self.assertEqual(self.waits, [])
                    self.assertEqual(self.reports, [])

    def test_diagnostic_transport_exception_never_retries_original_read(self):
        for args in (('api', 'repos/example/app'), ('pr', 'list')):
            with self.subTest(args=args), patch('sfc_lib.github.run', side_effect=[
                    self.result({'message': 'API rate limit exceeded'}, 1),
                    Halt('Command could not finish: timeout')]) as transport:
                with self.assertRaisesRegex(Halt, 'timeout'):
                    self.gh.call(*args)
                self.assertEqual(transport.call_count, 2)
                self.assertEqual(self.waits, [])
                self.assertEqual(self.reports, [])

    def test_graphql_diagnostic_body_reset_is_honored_without_http_headers(self):
        responses = [self.result({'message': 'API rate limit exceeded'}, 1),
                     self.result({'data': {'rateLimit': {'remaining': 0,
                                  'resetAt': '1970-01-01T00:16:47Z'}}}),
                     self.result([])]
        with patch('sfc_lib.github.run', side_effect=responses) as transport:
            self.assertEqual(self.gh.call('pr', 'list'), [])
        self.assertEqual(transport.call_count, 3)
        self.assertEqual(sum(self.waits), 8)

    def test_rest_diagnostic_selects_search_bucket_for_leading_slash(self):
        responses = [self.result({'message': 'API rate limit exceeded'}, 1),
                     self.result({'resources': {'core': {'remaining': 5000, 'reset': 9999},
                                                'search': {'remaining': 0, 'reset': 1003}}}),
                     self.result({'items': []})]
        with patch('sfc_lib.github.run', side_effect=responses) as transport:
            self.assertEqual(self.gh.call('api', '/search/issues'), {'items': []})
        self.assertEqual(transport.call_count, 3)
        self.assertEqual(sum(self.waits), 4)

    def test_confirmed_quota_diagnostic_uses_headers_or_bounded_fallback(self):
        for args in (('api', 'repos/example/app'), ('pr', 'list')):
            for headers, seconds in (('', 60), ('HTTP/2.0 403 Forbidden\r\nretry-after: 2', 2)):
                with self.subTest(args=args, headers=headers):
                    self.waits.clear()
                    for diagnostic in (
                            self.result({'message': 'API rate limit exceeded'}, 1,
                                        headers, 'gh: API rate limit exceeded (HTTP 403)'),
                            self.result({'errors': [{'type': 'RATE_LIMITED'}]}, headers=headers)):
                        self.waits.clear()
                        with patch('sfc_lib.github.run', side_effect=[
                                self.result({'message': 'API rate limit exceeded'}, 1),
                                diagnostic, self.result([])]) as transport:
                            self.assertEqual(self.gh.call(*args), [])
                        self.assertEqual(transport.call_count, 3)
                        self.assertEqual(sum(self.waits), seconds)

    def test_valid_unexhausted_diagnostic_uses_bounded_fallback(self):
        diagnostic = self.result({'resources': {'core': {'remaining': 100, 'reset': 9999}},
                                  'data': {'rateLimit': {'remaining': 100,
                                                       'resetAt': '1970-01-01T00:16:43Z'}}})
        for args in (('api', 'repos/example/app'), ('pr', 'list')):
            with self.subTest(args=args):
                self.waits.clear()
                with patch('sfc_lib.github.run', side_effect=[
                        self.result({'message': 'API rate limit exceeded'}, 1),
                        diagnostic, self.result([])]) as transport:
                    self.assertEqual(self.gh.call(*args), [])
                self.assertEqual(transport.call_count, 3)
                self.assertEqual(sum(self.waits), 60)

    def setUp(self):
        self.current = 1000.
        self.waits, self.reports = [], []
        self.guard = lambda: None
        def wait(seconds):
            self.waits.append(seconds)
            self.current += seconds
        self.gate = ReadWait(lambda: self.guard(), self.reports.append,
                             clock=lambda: self.current, wait=wait)
        with patch('sfc_lib.github.git', return_value='https://github.com/example/app.git'):
            self.gh = GitHub(Path.cwd(), copy.deepcopy(fixtures.BASE_CONFIG))
        self.gh.read_wait = self.gate

    def result(self, body, code=0, headers='', stderr=''):
        return subprocess.CompletedProcess([], code, (headers + '\n\n' if headers else '') + json.dumps(body), stderr)

    def test_connection_reset_retries_only_confirmed_reads(self):
        reset = subprocess.CompletedProcess([], 1, '',
            'Post "https://api.github.com/graphql": read tcp 192.0.2.1:123->192.0.2.2:443: read: connection reset by peer\n')
        with patch('sfc_lib.github.run', side_effect=[reset, reset, self.result({'data': {'ok': True}})]) as run:
            self.assertEqual(self.gh.graphql('query { ok }', {}), {'ok': True})
        self.assertEqual(run.call_count, 3)
        self.assertEqual(sum(self.waits), 3)
        self.assertEqual(self.reports[0]['reason'], 'connection-reset')
        with patch('sfc_lib.github.run', return_value=reset) as run, self.assertRaises(ConnectionReset):
            self.gh.graphql('mutation { update }', {})
        self.assertEqual(run.call_count, 1)

    def test_repeated_read_resets_respect_the_retry_bound_and_stop(self):
        for stopped in (False, True):
            self.gate.until = 0
            self.waits.clear()
            def read():
                raise ReadConnectionReset('fixture reset')
            def guard():
                if stopped and self.waits:
                    raise Stopped('fixture stop')
            self.guard = guard
            operation = Mock(side_effect=read)
            with self.assertRaises(RateWaitStopped if stopped else ReadConnectionReset):
                self.gate.call(operation, lambda: self.fail('No quota probe for a reset'))
            self.assertEqual(operation.call_count, 1 if stopped else 4)
            self.assertEqual(sum(self.waits), 1 if stopped else 7)

    def test_graphql_waits_for_header_reset_and_repeats_only_read(self):
        responses = [self.result({'errors': [{'type': 'RATE_LIMITED'}]}, headers=
                     'HTTP/2.0 200 OK\r\nx-ratelimit-remaining: 0\r\nx-ratelimit-reset: 1005'),
                     self.result({'data': {'node': {'id': 'actual'}}})]
        with patch('sfc_lib.github.run', side_effect=responses) as run:
            value = self.gh.graphql('query { node(id:"x") { id } }', {})
        self.assertEqual(value, {'node': {'id': 'actual'}})
        self.assertEqual(run.call_count, 2)
        self.assertEqual(sum(self.waits), 6)
        self.assertTrue(all(n <= 1 for n in self.waits))
        self.assertEqual([v['state'] for v in self.reports], ['waiting', 'ready'])

    def test_secondary_retry_after_and_finite_exponential_fallback(self):
        self.assertEqual(delay({'retry-after': '17'}, 1000, 0), 17)
        self.assertEqual(delay({'retry-after': 'Thu, 01 Jan 1970 00:17:00 GMT'}, 1000, 0), 20)
        self.assertEqual(delay({}, 1000, 2), 240)
        self.assertEqual(delay({'x-ratelimit-remaining': '0', 'x-ratelimit-reset': '999'}, 1000, 0), 60)
        with self.assertRaises(Halt): delay({'retry-after': 'invalid'}, 1000, 0)
        calls = []
        def limited():
            calls.append(True)
            raise RateLimited({})
        with self.assertRaisesRegex(Halt, 'bounded read retries'):
            self.gate.call(limited, lambda: {})
        self.assertEqual(len(calls), 4)
        self.assertEqual(sum(self.waits), 60 + 120 + 240)

    def test_rest_probe_uses_rest_bucket_not_graphql(self):
        responses = [self.result({'message': 'API rate limit exceeded'}, code=1),
                     self.result({'resources': {'core': {'remaining': 0, 'reset': 1003},
                                                'graphql': {'remaining': 5000, 'reset': 9999}}}),
                     self.result({'id': 1})]
        with patch('sfc_lib.github.run', side_effect=responses) as run:
            self.assertEqual(self.gh.call('api', 'repos/example/app'), {'id': 1})
        self.assertEqual(sum(self.waits), 4)
        self.assertIn('rate_limit', run.call_args_list[1].args[0])

    def test_supervisor_cli_reads_wait_and_retry_through_adapter(self):
        cases = [
            (lambda: self.gh.pull_requests('sfc/issue-87'), [], False),
            (lambda: self.gh.issue_state(87), {'state': 'OPEN'}, False),
            (self.gh.cards, {'data': {'node': {'items': {
                'nodes': [], 'pageInfo': {'hasNextPage': False}}}}}, True),
        ]
        self.gh._meta = {'id': 'project'}
        for operation, body, graphql in cases:
            with self.subTest(operation=operation):
                self.waits.clear()
                self.reports.clear()
                # CLI list/view hide headers; GraphQL CLI fields expose them.
                quota = self.result({'message': 'API rate limit exceeded'}, 1,
                                    'HTTP/2.0 403 Forbidden\r\nretry-after: 2' if graphql else '')
                probe = self.result({'data': {'rateLimit': {'remaining': 10,
                                     'resetAt': '1970-01-01T00:16:43Z'}}},
                                    headers='HTTP/2.0 200 OK\r\nretry-after: 2')
                responses = [quota, self.result(body)] if graphql else [quota, probe, self.result(body)]
                with patch('sfc_lib.github.run', side_effect=responses) as run:
                    self.assertEqual(operation(), 'OPEN' if isinstance(body, dict) and 'state' in body else [])
                self.assertEqual(run.call_count, 2 if graphql else 3)
                self.assertEqual(sum(self.waits), 2)
                self.assertEqual([v['state'] for v in self.reports], ['waiting', 'ready'])
                if graphql:
                    self.assertIn('--include', run.call_args_list[0].args[0])

    def test_local_merge_capability_alias_is_a_read_and_waits(self):
        fields = lambda names: [{'name': name} for name in names]
        data = {'mutation': {'fields': [{'name': 'updateRefs', 'args': fields(['input'])}]},
                'update': {'inputFields': fields(['name', 'beforeOid', 'afterOid', 'force'])},
                'input': {'inputFields': fields(['repositoryId', 'refUpdates', 'clientMutationId'])}}
        responses = [self.result({'errors': [{'type': 'RATE_LIMITED'}]}, headers=
                                 'HTTP/2.0 200 OK\r\nretry-after: 2'),
                     self.result({'data': data})]
        with patch('sfc_lib.github.run', side_effect=responses) as run:
            self.gh.local_merge_capabilities()
        self.assertEqual(run.call_count, 2)
        self.assertEqual(sum(self.waits), 2)

    def test_ambiguous_graphql_field_sources_and_mutations_never_retry(self):
        cases = [
            (('api', 'graphql', '-f', 'query=mutation { change }'), None),
            (('api', 'graphql', '-f', 'query=query { mutation: viewer { login } } mutation { change }'), None),
            (('api', 'graphql', '-f', 'query=query {}', '-f', 'query=mutation { change }'), None),
            (('api', 'graphql', '--input', 'request.json'), None),
            (('api', 'graphql', '-f', 'query=mutation { change }'), json.dumps({'query': 'query {}'})),
            (('api', 'graphql', '-f', 'query=query {}', '--method', 'POST'), None),
            (('api', 'graphql'), json.dumps({'query': None})),
        ]
        for args, payload in cases:
            with self.subTest(args=args, payload=payload):
                self.assertFalse(read_only(args, payload))
                with patch('sfc_lib.github.run', return_value=self.result(
                        {'message': 'API rate limit exceeded'}, 1)) as run:
                    with self.assertRaises(Halt):
                        self.gh.call(*args, input=payload)
                self.assertEqual(run.call_count, 1)
        self.assertEqual(self.waits, [])

    def test_secondary_http_failure_retries_after_header_without_diagnostic(self):
        responses = [self.result({'message': 'You have exceeded a secondary rate limit.'}, 1,
                                'HTTP/2.0 403 Forbidden\r\nretry-after: 2'),
                     self.result({'data': {'viewer': {'login': 'example'}}})]
        with patch('sfc_lib.github.run', side_effect=responses) as run:
            self.assertEqual(self.gh.graphql('query { viewer { login } }', {}),
                             {'viewer': {'login': 'example'}})
        self.assertEqual(run.call_count, 2)
        self.assertEqual(sum(self.waits), 2)

    def test_transport_exception_never_waits_or_probes(self):
        with patch('sfc_lib.github.run', side_effect=Halt('Command could not finish: timeout')) as run:
            with self.assertRaisesRegex(Halt, 'timeout'):
                self.gh.graphql('query { viewer { login } }', {})
        self.assertEqual(run.call_count, 1)
        self.assertEqual(self.reports, [])
        self.assertEqual(self.waits, [])

    def test_factory_wait_respects_original_completion_allowance_after_work_cutoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            factory = SimpleNamespace(gh=self.gh, mutex=threading.RLock(), state={},
                                      state_path=Path(tmp) / 'state.json', save=Mock(),
                                      ensure_config_unchanged=Mock(),
                                      ensure_running=Mock(side_effect=Stopped('work cutoff')))
            calls = []
            def operation():
                calls.append(True)
                raise RateLimited({'retry-after': '60'})
            with factory_reads(factory):
                gate = self.gh.read_wait
                gate.clock, gate.wait = lambda: self.current, self.gate.wait
                with bounded_io(lambda: 1002 - self.current):
                    with self.assertRaises(RateWaitStopped):
                        gate.call(operation, lambda: {})
                self.assertEqual(factory.state['rate_limit_wait']['state'], 'waiting')
            self.assertEqual(self.current, 1002)
            self.assertEqual(len(calls), 1)
            factory.ensure_running.assert_not_called()
            self.assertTrue(factory.ensure_config_unchanged.called)
            self.assertIs(self.gh.read_wait, self.gate)

    def test_completion_allowance_is_rechecked_after_slow_config_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            def validate():
                self.current = 1002  # Validation itself spends the remaining grace.
            factory = SimpleNamespace(gh=self.gh, mutex=threading.RLock(), state={},
                                      state_path=Path(tmp) / 'state.json', save=Mock(),
                                      ensure_config_unchanged=Mock(side_effect=validate),
                                      ensure_running=Mock(side_effect=Stopped('work cutoff')))
            operation = Mock(side_effect=RateLimited({'retry-after': '2'}))
            with factory_reads(factory):
                gate = self.gh.read_wait
                gate.clock, gate.wait = lambda: self.current, self.gate.wait
                with bounded_io(lambda: 1002 - self.current):
                    with self.assertRaises(RateWaitStopped):
                        gate.call(operation, lambda: {})
            operation.assert_called_once()
            factory.ensure_config_unchanged.assert_called_once()
            factory.ensure_running.assert_not_called()
            self.assertEqual(self.waits, [])

    def test_mutations_never_retry_even_for_rate_limit(self):
        cases = [(('api', 'graphql', '--input', '-'), json.dumps({'query': 'mutation { change }'})),
                 (('api', 'repos/example/app/issues', '--method', 'POST'), '{}'),
                 (('project', 'item-edit', '1'), None)]
        for args, payload in cases:
            with self.subTest(args=args), patch('sfc_lib.github.run', side_effect=Halt('API rate limit exceeded')) as run:
                with self.assertRaises(Halt): self.gh.call(*args, input=payload)
                self.assertEqual(run.call_count, 1)
        self.assertEqual(self.waits, [])

    def test_unknown_shapes_permission_network_partial_errors_never_retry(self):
        self.assertFalse(read_only(('api', 'repos/example/app', '-f', 'data=x'), None))
        self.assertFalse(read_only(('api', 'graphql'), json.dumps({'query': '{ viewer {login} }'})))
        self.assertFalse(read_only(('api', 'graphql'), json.dumps({'query': 'query {} mutation {}'})))
        for result in (self.result({'message': 'Forbidden'}, 1),
                       self.result({'errors': [{'type': 'RATE_LIMITED'}, {'type': 'FORBIDDEN'}]}),
                       subprocess.CompletedProcess([], 1, '', 'network failure')):
            with patch('sfc_lib.github.run', return_value=result) as run, self.assertRaises(Halt):
                self.gh.call('api', 'graphql', input=json.dumps({'query': 'query {}'}))
            self.assertEqual(run.call_count, 1)
        self.assertEqual(self.waits, [])

    def test_cancellation_and_original_deadline_prevent_another_request(self):
        calls = []
        def limited():
            calls.append(True)
            raise RateLimited({'retry-after': '60'})
        def guard():
            if self.current >= 1002: raise Stopped('original deadline/stop')
        self.guard = guard
        with self.assertRaises(RateWaitStopped): self.gate.call(limited, lambda: {})
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.current, 1002)

    def test_shared_cooldown_expiry_still_checks_peer_cutoff_before_first_request(self):
        self.gate.until = 1002  # Another reader owns this confirmed cooldown.
        def guard():
            if self.current >= 1002:
                raise Stopped('peer cutoff')
        self.guard = guard
        operation = Mock(return_value='must not dispatch')
        with self.assertRaises(RateWaitStopped):
            self.gate.call(operation, lambda: {})
        operation.assert_not_called()
        self.assertEqual(self.current, 1002)

    def test_exact_one_hour_wait_bound_prevents_retry(self):
        operation = Mock(side_effect=RateLimited({'retry-after': '3600'}))
        with self.assertRaisesRegex(Halt, 'one-hour bound'):
            self.gate.call(operation, lambda: {})
        self.assertEqual(operation.call_count, 1)
        self.assertEqual(sum(self.waits), 3600)

    def test_drift_halts_in_wait_without_repeating_read(self):
        def guard():
            if self.current > 1000: raise Halt('config or authority changed')
        self.guard = guard
        with self.assertRaisesRegex(Halt, 'authority'):
            self.gate.call(lambda: (_ for _ in ()).throw(RateLimited({})), lambda: {})

    def test_prewrite_stop_preserves_verified_receipt_but_postwrite_latches(self):
        gh = fixtures.FakeGitHub(Path.cwd())
        for postwrite in (False, True):
            record = {}
            original = gh.activity_snapshot
            reads = []
            def snapshot(card):
                reads.append(True)
                if len(reads) == (2 if postwrite else 1): raise RateWaitStopped('deadline')
                return original(card)
            with patch.object(gh, 'activity_snapshot', side_effect=snapshot):
                with self.assertRaises(ActivityError if postwrite else RateWaitStopped):
                    Activity(gh, lambda _: record, lambda: None).report(fixtures.issue(), 'Running', 'build')
            if postwrite:
                self.assertTrue(record['activity']['pending'])
                self.assertIn('error', record['activity'])
            else:
                self.assertEqual(record, {})

    def test_prewrite_stop_keeps_existing_verified_activity_history(self):
        gh = fixtures.FakeGitHub(Path.cwd())
        record = {}
        activity = Activity(gh, lambda _: record, lambda: None)
        card = fixtures.issue()
        activity.report(card, 'Waiting', 'assess')
        verified = copy.deepcopy(record)
        writes = copy.deepcopy(gh.activity_writes)
        with patch.object(gh, 'activity_snapshot', side_effect=RateWaitStopped('disconnect')):
            with self.assertRaises(RateWaitStopped):
                activity.report(card, 'Running', 'build')
        self.assertEqual(record, verified)
        self.assertEqual(gh.activity_writes, writes)

    def test_activity_cancellation_at_exact_cooldown_expiry_preserves_write_boundary(self):
        for postwrite in (False, True):
            with self.subTest(postwrite=postwrite):
                self.current = 1000.
                self.gate.until = 0
                gh = fixtures.FakeGitHub(Path.cwd())
                record = {}
                activity = Activity(gh, lambda _: record, lambda: None)
                card = fixtures.issue()
                activity.report(card, 'Waiting', 'assess')
                verified = copy.deepcopy(record)
                writes = copy.deepcopy(gh.activity_writes)
                original = gh.activity_snapshot
                reads, calls = [], []
                def operation():
                    calls.append(True)
                    raise RateLimited({'retry-after': '2'})
                def snapshot(card):
                    reads.append(True)
                    if len(reads) == (2 if postwrite else 1):
                        return self.gate.call(operation, lambda: {})
                    return original(card)
                def guard():
                    if self.current >= 1002:
                        raise Stopped('original cutoff')
                self.guard = guard
                with patch.object(gh, 'activity_snapshot', side_effect=snapshot):
                    with self.assertRaises(ActivityError if postwrite else RateWaitStopped):
                        activity.report(card, 'Running', 'build')
                self.assertEqual(self.current, 1002)
                self.assertEqual(len(calls), 1)
                if postwrite:
                    self.assertTrue(record['activity']['pending'])
                    self.assertIn('error', record['activity'])
                    self.assertEqual(len(gh.activity_writes), len(writes) + 1)
                else:
                    self.assertEqual(record, verified)
                    self.assertEqual(gh.activity_writes, writes)

    def test_concurrent_reads_have_one_wait_owner_without_duplicate_dispatch(self):
        first = threading.Event()
        release = threading.Event()
        results, calls = [], []
        def operation():
            calls.append(True)
            if len(calls) == 1: raise RateLimited({'retry-after': '1'})
            return 'read-only'
        def wait(seconds):
            first.set()
            self.assertTrue(release.wait(3))
            self.current += seconds
        self.gate.wait = wait
        def work(): results.append(self.gate.call(operation, lambda: {}))
        threads = [threading.Thread(target=work) for _ in range(2)]
        threads[0].start()
        self.assertTrue(first.wait(3))
        threads[1].start()
        release.set()
        for t in threads: t.join(3); self.assertFalse(t.is_alive())
        self.assertEqual(results, ['read-only', 'read-only'])
        self.assertEqual(len(calls), 3)

    def test_nonwaiting_cleanup_read_retains_existing_stop_protocol(self):
        # Supervisor cleanup has its own guards. Do not reject its first read
        # merely because the worker/window has already stopped.
        self.guard = lambda: (_ for _ in ()).throw(Stopped('worker stopped'))
        self.assertEqual(self.gate.call(lambda: 'verified cleanup read', lambda: {}), 'verified cleanup read')

    def test_process_timeout_cancels_activity_wait_without_retry_or_stale_running(self):
        # Exercise the real process watchdog and activity/read-wait integration,
        # with a fake child, GitHub adapter and clock. No process or network IO.
        for postwrite in (False, True):
            for cooldown in (1, 60):
                with self.subTest(postwrite=postwrite, cooldown=cooldown), tempfile.TemporaryDirectory() as tmp:
                    self.current = 1000.
                    terminated = threading.Event()
                    child = Mock(pid=123456, returncode=None, _waitpid_lock=threading.Lock())
                    child.poll = lambda: child.returncode
                    child.wait = Mock(side_effect=lambda **_: child.returncode)
                    child.communicate = Mock(return_value=('', ''))
                    def kill(pid, sig):
                        child.returncode = -sig
                        terminated.set()
                    gh = fixtures.FakeGitHub(Path(tmp))
                    record = {}
                    activity = Activity(gh, lambda _: record, lambda: None)
                    card = fixtures.issue()
                    activity.report(card, 'Waiting', 'assess')
                    verified = copy.deepcopy(record)
                    writes = copy.deepcopy(gh.activity_writes)
                    snapshot = gh.activity_snapshot
                    reads, attempts = [], []
                    factory = SimpleNamespace(gh=self.gh, mutex=threading.RLock(), state={},
                                              state_path=Path(tmp) / 'state.json', save=Mock(),
                                              ensure_running=Mock())
                    with factory_reads(factory):
                        gate = self.gh.read_wait
                        def wait(seconds):
                            self.current += seconds
                            self.assertTrue(terminated.wait(2), 'watchdog did not terminate child')
                        gate.clock, gate.wait = lambda: self.current, wait
                        def operation():
                            attempts.append(self.current)
                            if len(attempts) == 1:
                                raise RateLimited({'retry-after': str(cooldown)})
                            return snapshot(card)
                        def read(card):
                            reads.append(self.current)
                            if len(reads) == (2 if postwrite else 1):
                                return gate.call(operation, lambda: {})
                            return snapshot(card)
                        owner = Processes(Path(tmp) / 'stop')
                        with patch('sfc_lib.common.time.monotonic', side_effect=lambda: self.current), \
                                patch('sfc_lib.lifeline.spawn', return_value=(child, None)), \
                                patch('sfc_lib.common.os.killpg', side_effect=kill), \
                                patch.object(gh, 'activity_snapshot', side_effect=read):
                            with self.assertRaises(ActivityError if postwrite else TimedOut):
                                owner.execute(['fake-worker'], tmp, Path(tmp) / 'worker.log', 1,
                                              observer=activity.observe(card, 'build'))
                        self.assertEqual(owner.children, set())
                        self.assertTrue(terminated.is_set())
                        self.assertEqual(self.current, 1001)
                        self.assertEqual(attempts, [1000])
                        if postwrite:
                            self.assertTrue(record['activity']['pending'])
                            self.assertIn('error', record['activity'])
                            self.assertEqual(len(gh.activity_writes), len(writes) + 1)
                        else:
                            self.assertEqual(record, verified)
                            self.assertEqual(gh.activity_writes, writes)
                        # The observer's expired deadline must not leak into
                        # normal cleanup; it retains the shared cooldown only.
                        self.assertEqual(gate.call(lambda: 'cleanup', lambda: {}), 'cleanup')

    def test_process_exit_during_activity_wait_allows_inactive_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            child = Mock(pid=123456, returncode=None, _waitpid_lock=threading.Lock())
            child.poll = lambda: child.returncode
            child.communicate = Mock(return_value=('', ''))
            gh = fixtures.FakeGitHub(Path(tmp))
            record = {}
            activity = Activity(gh, lambda _: record, lambda: None)
            card = fixtures.issue()
            activity.report(card, 'Waiting', 'next')
            snapshot = gh.activity_snapshot
            operation = Mock(side_effect=RateLimited({'retry-after': '1'}))
            def wait(seconds):
                self.current += seconds
                child.returncode = 0  # Normal exit while preflight waits.
            self.gate.wait = wait
            def read(card):
                if child.returncode is None:
                    return self.gate.call(operation, lambda: {})
                return snapshot(card)
            owner = Processes(Path(tmp) / 'stop')
            with patch('sfc_lib.common.time.monotonic', side_effect=lambda: self.current), \
                    patch('sfc_lib.lifeline.spawn', return_value=(child, None)), \
                    patch('sfc_lib.common.os.killpg') as kill, \
                    patch.object(gh, 'activity_snapshot', side_effect=read):
                code = owner.execute(['fake-worker'], tmp, Path(tmp) / 'worker.log', 30,
                                     observer=activity.observe(card, 'build'))
            self.assertEqual(code, 0)
            operation.assert_called_once()
            self.assertEqual(record['activity']['state'], 'Waiting')
            self.assertEqual(record['activity']['action'], 'assess')
            self.assertFalse(record['activity']['pending'])
            self.assertNotIn('error', record['activity'])
            self.assertFalse(any('Running' in values[0] for _, values in gh.activity_writes))
            self.assertEqual(owner.children, set())
            kill.assert_not_called()

    def test_slow_activity_preflight_cannot_write_after_original_process_deadline(self):
        # Successful IO can also cross the cutoff; a slow quota failure must
        # not issue a diagnostic read after the child has timed out, either.
        for quota in (False, True):
            with self.subTest(quota=quota), tempfile.TemporaryDirectory() as tmp:
                self.current = 1000.
                child = Mock(pid=123456, returncode=None, _waitpid_lock=threading.Lock())
                child.poll = lambda: child.returncode
                gh = fixtures.FakeGitHub(Path(tmp))
                record = {}
                activity = Activity(gh, lambda _: record, lambda: None)
                card = fixtures.issue()
                snapshot = gh.activity_snapshot
                probe = Mock(return_value={})
                def operation():
                    self.current = 1001
                    child.returncode = -15
                    if quota:
                        raise RateLimited({})
                    return snapshot(card)
                owner = Processes(Path(tmp) / 'stop')
                with patch('sfc_lib.common.time.monotonic', side_effect=lambda: self.current), \
                        patch('sfc_lib.lifeline.spawn', return_value=(child, None)), \
                        patch.object(gh, 'activity_snapshot', side_effect=lambda _: self.gate.call(operation, probe)):
                    with self.assertRaises(TimedOut):
                        owner.execute(['fake-worker'], tmp, Path(tmp) / 'worker.log', 1,
                                      observer=activity.observe(card, 'build'))
                probe.assert_not_called()
                self.assertEqual(gh.activity_writes, [])
                self.assertEqual(record, {})
                self.assertEqual(owner.children, set())

    def test_observer_write_boundary_retains_intent_and_allows_normal_readback(self):
        for expire_before_write in (False, True):
            with self.subTest(expire_before_write=expire_before_write), tempfile.TemporaryDirectory() as tmp:
                self.current = 1000.
                child = Mock(pid=123456, returncode=None, _waitpid_lock=threading.Lock())
                child.poll = lambda: child.returncode
                child.communicate = Mock(return_value=('', ''))
                gh = fixtures.FakeGitHub(Path(tmp))
                record = {}
                write = gh.write_activity
                def save():
                    if expire_before_write:
                        self.current = 1001  # Saving the intent spends the last second.
                        child.returncode = -15
                def mutate(card, scope, values):
                    write(card, scope, values)
                    child.returncode = 0  # Normal exit during a successful mutation.
                activity = Activity(gh, lambda _: record, save)
                card = fixtures.issue()
                owner = Processes(Path(tmp) / 'stop')
                with patch('sfc_lib.common.time.monotonic', side_effect=lambda: self.current), \
                        patch('sfc_lib.lifeline.spawn', return_value=(child, None)), \
                        patch.object(gh, 'write_activity', side_effect=mutate):
                    if expire_before_write:
                        with self.assertRaises(ActivityError):
                            owner.execute(['fake-worker'], tmp, Path(tmp) / 'worker.log', 1,
                                          observer=activity.observe(card, 'build'))
                        self.assertEqual(gh.activity_writes, [])
                        self.assertTrue(record['activity']['pending'])
                        self.assertIn('error', record['activity'])
                    else:
                        self.assertEqual(owner.execute(['fake-worker'], tmp, Path(tmp) / 'worker.log', 30,
                                                       observer=activity.observe(card, 'build')), 0)
                        self.assertEqual(record['activity']['state'], 'Waiting')
                        self.assertEqual(record['activity']['action'], 'assess')
                        self.assertFalse(record['activity']['pending'])
                        self.assertNotIn('error', record['activity'])
                self.assertEqual(owner.children, set())

    def test_normal_exit_during_postwrite_wait_finishes_readback_once(self):
        for mode in ('quota', 'lock'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                child = Mock(pid=123456, returncode=None, _waitpid_lock=threading.Lock())
                child.poll = lambda: child.returncode
                child.communicate = Mock(return_value=('', ''))
                gh, record = fixtures.FakeGitHub(Path(tmp)), {}
                snapshot, write = gh.activity_snapshot, gh.write_activity
                gate = ReadWait(lambda: None, clock=lambda: self.current,
                                wait=lambda seconds: setattr(self, 'current', self.current + seconds))
                owner = Processes(Path(tmp) / 'stop')
                def mutate(card, scope, values):
                    write(card, scope, values)
                    child.returncode = 0
                def read(card):
                    if len(gh.activity_writes) == 1:
                        operation = Mock(side_effect=[RateLimited({'retry-after': '1'}), snapshot(card)])
                        if mode == 'lock':
                            gate.lock = Mock(acquire=Mock(side_effect=[False, True]), release=Mock())
                            operation = Mock(return_value=snapshot(card))
                        return gate.call(operation, lambda: {})
                    return snapshot(card)
                activity = Activity(gh, lambda _: record, lambda: None)
                with patch('sfc_lib.common.time.monotonic', side_effect=lambda: self.current), \
                        patch('sfc_lib.lifeline.spawn', return_value=(child, None)), \
                        patch.object(gh, 'write_activity', side_effect=mutate), \
                        patch.object(gh, 'activity_snapshot', side_effect=read):
                    self.assertEqual(owner.execute(['check'], tmp, Path(tmp) / 'check.log', 30,
                                                   observer=activity.observe(fixtures.issue(), 'checks')), 0)
                self.assertEqual(sum('Running' in v[0] for _, v in gh.activity_writes), 1)
                self.assertEqual(record['activity']['state'], 'Waiting')
                self.assertFalse(record['activity']['pending'])
                self.assertNotIn('error', record['activity'])

    def test_normal_exit_before_mutation_cancels_only_unsent_intent(self):
        for boundary in ('preflight', 'saved-intent'):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as tmp:
                child = Mock(pid=123456, returncode=None, _waitpid_lock=threading.Lock())
                child.poll = lambda: child.returncode
                child.communicate = Mock(return_value=('', ''))
                gh, record = fixtures.FakeGitHub(Path(tmp)), {}
                snapshot = gh.activity_snapshot
                def save():
                    if boundary == 'saved-intent' and record.get('activity', {}).get('state') == 'Running':
                        child.returncode = 0
                def read(card):
                    if boundary == 'preflight':
                        child.returncode = 0
                    return snapshot(card)
                activity = Activity(gh, lambda _: record, save)
                with patch('sfc_lib.lifeline.spawn', return_value=(child, None)), \
                        patch.object(gh, 'activity_snapshot', side_effect=read):
                    self.assertEqual(Processes(Path(tmp) / 'stop').execute(
                        ['check'], tmp, Path(tmp) / 'check.log', 30,
                        observer=activity.observe(fixtures.issue(), 'checks')), 0)
                self.assertFalse(any('Running' in v[0] for _, v in gh.activity_writes))
                self.assertEqual(record['activity']['state'], 'Waiting')
                self.assertNotIn('error', record['activity'])
                cancelled = [v for v in record['activity_history'] if v.get('not_written')]
                self.assertEqual(len(cancelled), int(boundary == 'saved-intent'))

    def test_postwrite_exit_keeps_deadline_stop_and_authority_guards(self):
        for failure in ('deadline', 'stop', 'authority'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp:
                self.current = 1000.
                child = Mock(pid=123456, returncode=None, _waitpid_lock=threading.Lock())
                child.poll = lambda: child.returncode
                gh, record, stopped = fixtures.FakeGitHub(Path(tmp)), {}, [False]
                snapshot, write = gh.activity_snapshot, gh.write_activity
                owner = Processes(Path(tmp) / 'stop')
                owner.cancel_check = lambda: stopped[0]
                def mutate(card, scope, values):
                    write(card, scope, values)
                    child.returncode = 0
                def wait(seconds):
                    self.current += 30 if failure == 'deadline' else seconds
                    stopped[0] = failure == 'stop'
                def guard():
                    if failure == 'authority':
                        raise Halt('authority changed')
                gate = ReadWait(guard, clock=lambda: self.current, wait=wait)
                def read(card):
                    if gh.activity_writes:
                        return gate.call(lambda: (_ for _ in ()).throw(RateLimited({'retry-after': '1'})), lambda: {})
                    return snapshot(card)
                activity = Activity(gh, lambda _: record, lambda: None)
                with patch('sfc_lib.common.time.monotonic', side_effect=lambda: self.current), \
                        patch('sfc_lib.lifeline.spawn', return_value=(child, None)), \
                        patch.object(gh, 'write_activity', side_effect=mutate), \
                        patch.object(gh, 'activity_snapshot', side_effect=read):
                    with self.assertRaises(ActivityError):
                        owner.execute(['check'], tmp, Path(tmp) / 'check.log', 10,
                                      observer=activity.observe(fixtures.issue(), 'checks'))
                self.assertEqual(len(gh.activity_writes), 1)
                self.assertTrue(record['activity']['pending'])
                self.assertNotEqual(record['activity']['error'], 'Child is no longer active')

    def test_state_callbacks_do_not_invert_caller_mutex_and_read_lock(self):
        mutex = threading.RLock()
        held, ready = threading.Event(), threading.Event()
        results, errors = [], []
        def report(value):
            with mutex:
                pass
        gate = ReadWait(lambda: None, report)
        def worker():
            try:
                with mutex:
                    held.set()
                    if not ready.wait(2):
                        raise AssertionError('scheduler did not begin')
                    results.append(gate.call(lambda: 'worker', lambda: {}))
            except BaseException as exc:
                errors.append(exc)
        def scheduler():
            try:
                results.append(gate.call(lambda: ready.set() or 'scheduler', lambda: {}))
            except BaseException as exc:
                errors.append(exc)
        threads = [threading.Thread(target=worker, daemon=True),
                   threading.Thread(target=scheduler, daemon=True)]
        threads[0].start()
        self.assertTrue(held.wait(2))
        threads[1].start()
        for thread in threads:
            thread.join(3)
        self.assertFalse(any(thread.is_alive() for thread in threads), 'inverted lock order')
        self.assertEqual(errors, [])
        self.assertCountEqual(results, ['worker', 'scheduler'])

    def test_obsolete_callback_cannot_replace_latest_factory_wait(self):
        with tempfile.TemporaryDirectory() as tmp:
            factory = SimpleNamespace(gh=self.gh, mutex=threading.RLock(), state={},
                                      state_path=Path(tmp) / 'state.json', save=Mock(),
                                      ensure_running=Mock())
            with factory_reads(factory):
                gate = self.gh.read_wait
                obsolete = {'state': 'ready'}
                current = {'state': 'waiting', 'retry_at': 2000, 'attempt': 1}
                gate.latest = current
                self.assertTrue(gate.report(current))
                self.assertFalse(gate.report(obsolete))
                self.assertEqual(factory.state['rate_limit_wait'], current)
                self.assertEqual(factory.save.call_count, 1)
