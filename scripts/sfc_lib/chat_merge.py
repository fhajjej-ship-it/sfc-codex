"""Explicit chat delivery: owned checks/native review, then a pinned atomic merge.

No builder, queue drain, protection bypass or mutation retry lives here.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import shlex
import signal
import sys
import threading
from types import SimpleNamespace
import uuid

from .common import Halt, PACKAGE, Processes, common_dir, exclusive, git, load_config, now, owned_io, read_json, run, write_json
from . import chat_tracking as tracking, review_policy
from .factory import checks_pass, policy_reasons, secret_problem
from .local_merge import LocalMerge
from .verification import checksum, digest, source_tree
from .ticket_validation import acceptance_criteria

PROVIDER = 'atomic-fast-forward'


@contextmanager
def owned_processes(processes):
    """Keep child cleanup attached to this invocation, including CLI signals."""
    from .lifeline import supported
    from .common import Stopped
    if threading.current_thread() is not threading.main_thread():
        raise Halt('Chat delivery requires its own main-thread supervisor for signal ownership')
    lifetime, cancel = processes.lifetime, processes.cancel_check
    processes.lifetime = supported()
    stopped = threading.Event()
    handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    processes.cancel_check = lambda: stopped.is_set() or bool(cancel and cancel())
    try:
        for sig in handlers:
            signal.signal(sig, lambda *_: stopped.set())
        with owned_io(processes.stopped, lifetime=processes.lifetime):
            yield
        if stopped.is_set():
            raise Stopped('Delivery interrupted; original work and spending preserved')
    finally:
        for sig, handler in handlers.items():
            signal.signal(sig, handler)
        processes.lifetime, processes.cancel_check = lifetime, cancel


def commit_closure_scope(messages, repository, number, host):
    """Conservatively hand off messages that can close outside this task."""
    directive = re.compile(r'\b(?:close[sd]?|fix(?:es|ed)?|resolve[sd]?)(?:\s*:\s*|\s+)'
                           r'(?:(https?://[^\s)>,]+)|((?:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)?#[0-9]+))', re.I)
    for message in messages.split('\0'):
        for match in directive.finditer(message):
            url, short = match.groups()
            if url:
                target = re.match(r'https?://([^/]+)/([^/]+/[^/]+)/(issues|pull)/([0-9]+)(?=[/?#]|$)',
                                  url.rstrip('.,;:!?]}'), re.I)
                if target is None:
                    continue  # A linked documentation URL is not an issue-closing reference.
                valid = (target and target[1].casefold() == host.casefold() and
                         target[2].casefold() == repository.casefold() and target[3].casefold() == 'issues' and
                         int(target[4]) == number)
            else:
                repo, n = short.rsplit('#', 1)
                valid = (not repo or repo.casefold() == repository.casefold()) and int(n) == number
            if not valid:
                raise Halt('Commit-message issue closures exceed the tracked issue scope; maintainer handoff required')


def support(gh, config):
    """Expose unavailable automation at setup; never change repository rules."""
    try:
        if config.get('chat_merge_provider') != PROVIDER or config.get('review_policy') is not None:
            raise Halt('Select the chat atomic provider; custom review policies require their existing reviewed provider')
        if not config['verify_commands']:
            raise Halt('Configure meaningful --verify commands before automatic delivery')
        gh.local_merge_capabilities()
        owner, name = config['repo'].split('/')
        repo = gh.graphql('''query($owner:String!,$name:String!,$base:String!) {
          repository(owner:$owner,name:$name) { defaultBranchRef { name } ref(qualifiedName:$base) {
            branchProtectionRule { id } rules(first:1) { totalCount }
          } }
        }''', {'owner': owner, 'name': name, 'base': 'refs/heads/' + config['base_branch']})['repository']
        ref = repo['ref']
        if repo['defaultBranchRef']['name'] != config['base_branch']:
            raise Halt('Atomic closing-PR delivery requires the live default branch; use a maintainer handoff')
        if ref['branchProtectionRule'] is not None or type(ref['rules']['totalCount']) is not int or ref['rules']['totalCount'] != 0:
            raise Halt('Atomic chat merging refuses protected or ruled refs; use the repository maintainer merge process')
        review_policy.native_command(SimpleNamespace(c=config), Path(gh.root), Path(gh.root) / '.sfc/review-probe')
    except (Halt, KeyError, TypeError) as exc:
        return {'available': False, 'provider': PROVIDER, 'reason': str(exc)}
    return {'available': True, 'provider': PROVIDER}


class Delivery:
    def __init__(self, gh, root, record, path, worktree, head, base, processes=None):
        self.gh, self.root, self.record, self.path = gh, Path(root), record, path
        self.c, self.wt, self.head, self.base = gh.c, Path(worktree).resolve(), head, base
        self.config = digest(self.c)
        self.processes = processes or Processes(self.root / '.sfc/stop')
        self.processes.shared_stop_path = common_dir(root) / 'sfc-stop'
        self.local = LocalMerge(SimpleNamespace(c=self.c, watcher=None))

    def running(self):
        if digest(load_config(tracking.primary_root(self.root))) != self.config or self.processes.stopped():
            raise Halt('Delivery stopped or configuration changed; preserve spending and receipts')

    def guard(self, *, completion=False):
        self.running()
        if common_dir(self.wt) != common_dir(self.root):
            raise Halt('Candidate worktree belongs to a different repository')
        self.gh.verify_repository()
        authority = self.c.get('chat_merge_authority', {})
        identity = self.gh.onboarding_identity()
        expected = {'repo': self.c['repo'], 'base_branch': self.c['base_branch'],
                    'scope': 'user-requested-tasks', 'host': identity['host'],
                    'repository_id': identity['repository_id'], 'provider': PROVIDER}
        if (authority != expected or self.c.get('chat_merge_provider') != PROVIDER or
                self.c.get('review_policy') is not None):
            raise Halt('Chat merge authority/provider is missing, unsupported or belongs to a different repository identity')
        if tracking.pr_target(self.c['repo'], self.record.get('pr'), self.record.get('pr_url'))[0] != self.c['repo'].casefold():
            raise Halt('Cross-repository tracking does not transfer merge authority')
        if self.record.get('pr_role') == 'milestone' or not self.record.get('closes_issue'):
            raise Halt('Automatic delivery requires the task delivery PR and explicit closing intent')
        self.card = tracking.verified_card(self.gh, self.record)
        if not completion and (self.card['state'] != 'OPEN' or self.card['status'] not in ('QA', 'Review')):
            raise Halt('Chat delivery requires the open tracked issue in QA or Review')
        self.local.clean(self.wt, self.head)
        if (self.head == self.base or run(['git', 'merge-base', '--is-ancestor', self.base, self.head],
                                        self.wt, check=False).returncode):
            raise Halt('Integrate the current base before checking and reviewing this candidate')

    def snapshot(self, intent):
        snapshot = self.gh.chat_delivery_snapshot(intent['pr'], self.card['number'], intent['base_ref'], intent['head_ref'],
                                                  self.card['item_id'], self.card['_pickup_scope']['project']['field'])
        pr, issue = self.local.identity(self.card, intent, snapshot)
        if snapshot['id'] != self.c['chat_merge_authority']['repository_id']:
            raise Halt('Reviewed repository identity changed')
        if snapshot['defaultBranchRef']['name'] != self.c['base_branch']:
            raise Halt('The delivery base is not the live default branch; maintainer handoff required')
        if (pr['closingIssuesReferences']['totalCount'] != 1 or
                [node['id'] for node in pr['closingIssuesReferences']['nodes']] != [self.card['id']]):
            raise Halt('Automatic delivery requires the tracked issue to be the sole closing reference; maintainer handoff required')
        self.check_policy_snapshot(snapshot)
        self.refs = {'repository': snapshot['id'], 'pr': pr['id'],
                     **{key: snapshot[key]['id'] for key in ('head', 'base')}}
        return snapshot, pr, issue

    def check_policy_snapshot(self, snapshot):
        from .priority import item_snapshot
        issue, item, field = snapshot['issue'], snapshot['tracking_item'], snapshot['routing_field']
        if issue['labels']['pageInfo']['hasNextPage'] or item['fieldValues']['pageInfo']['hasNextPage']:
            raise Halt('Complete combined policy inventory is unavailable; maintainer handoff required')
        labels = issue['labels']['nodes']
        meta = self.card['_pickup_scope']['project']
        values = item_snapshot(self.gh, self.card['item_id'], first_page=item)
        keys = ('id', 'number', 'title', 'body', 'url', 'state', 'repository')
        options = {row['name']: row['id'] for row in field['options']}
        if (any(issue[k] != self.card[k] or values['content'][k] != issue[k] for k in keys) or
                len({row['id'] for row in labels}) != len(labels) or
                not all(row['id'] and row['name'] for row in labels) or
                sorted(row['name'] for row in labels) != sorted(self.card['labels']) or
                item['id'] != self.card['item_id'] or item['project']['id'] != meta['id'] or
                field['id'] != meta['field'] or field['name'] != self.c['status_field'] or
                len(options) != len(field['options']) or options != meta['options'] or
                values['values'].get(meta['field']) != options[self.card['status']]):
            raise Halt('Live issue policy or routing changed during candidate validation')

    def candidate(self, *, ready=False, ci=None):
        self.guard()
        _, number = tracking.pr_target(self.c['repo'], self.record.get('pr'), self.record.get('pr_url'))
        pr = self.gh.pr(number)
        paths = [p for p in git(self.wt, 'diff', '--no-renames', '--name-only', '-z', self.base, self.head).split('\0') if p]
        lines = 0
        for row in git(self.wt, 'diff', '--no-renames', '--numstat', '-z', self.base, self.head).split('\0'):
            if row:
                a, d, _ = row.split('\t', 2)
                if '-' in (a, d):
                    raise Halt('Binary changes require a maintainer handoff')
                lines += int(a) + int(d)
        history = ['git', 'log', '--format=', '--no-renames', '--diff-merges=separate']
        messages = run(['git', 'log', '--format=%B%x00', self.base + '..' + self.head], self.wt).stdout
        commit_closure_scope(messages, self.c['repo'], self.card['number'], self.c['chat_merge_authority']['host'])
        history_paths = run([*history, '--name-only', '-z', self.base + '..' + self.head], self.wt).stdout.split('\0')
        history_stats = run([*history, '--numstat', '-z', self.base + '..' + self.head], self.wt).stdout
        if any(row.split('\t', 2)[:2] == ['-', '-'] for row in history_stats.split('\0') if row):
            raise Halt('Binary changes in published history require a maintainer handoff')
        history_patch = run([*history, '-p', '--no-ext-diff', '--no-textconv', self.base + '..' + self.head], self.wt).stdout
        problems = policy_reasons(self.c, self.card, {**pr, 'additions': max(lines, pr['additions'] + pr['deletions']),
                                                    'deletions': 0, 'files': pr['files'] + [{'path': p} for p in paths + history_paths if p]})
        problem = secret_problem(paths + history_paths, history_patch)
        if problems or problem or not paths:
            raise Halt('; '.join(problems + ([problem] if problem else []) + ([] if paths else ['Empty candidate'])))
        intent = {'id': uuid.uuid4().hex, 'pr': number, 'pr_url': pr['url'], 'head': self.head, 'base': self.base,
                  'head_ref': 'refs/heads/' + pr['headRefName'], 'base_ref': 'refs/heads/' + self.c['base_branch'],
                  'tree': source_tree(self.wt)}
        snapshot, live, issue = self.snapshot(intent)
        if not self.local.matches_before(snapshot, intent) or live['state'] != 'OPEN' or issue['state'] != 'OPEN':
            raise Halt('Candidate head/base or issue changed; repeat checks and review')
        if ready and (live['isDraft'] is not False or self.card['status'] != 'Review' or
                      self.record['events'][-1]['stage'] != 'Review'):
            raise Halt('A ready PR and live Review milestone are required')
        if ready and (any(not event.get('synced') for event in self.record['events']) or
                      self.record.get('native_pr_attachment', {}).get('state') != 'verified' or
                      self.record.get('native_pr_attachment_error')):
            raise Halt('Sync pending milestones and native attachment readback before merging')
        if ci is not None and self.ci_candidate(snapshot, intent) != ci:
            raise Halt('The applicable PR check candidate changed before dispatch')
        intent.update(repository_id=snapshot['id'], pr_id=live['id'],
                      head_id=snapshot['head']['id'], base_id=snapshot['base']['id'])
        self.intent = intent
        return intent, pr

    def binding(self):
        return {'head': self.head, 'base': self.base, 'tree': source_tree(self.wt), 'config': self.config,
                'tracking': digest({k: self.record.get(k) for k in ('task', 'issue', 'delivery', 'pr', 'pr_url', 'pr_role', 'closes_issue')}),
                'refs': self.refs,
                'task': digest({k: self.card[k] for k in ('id', 'item_id', 'number', 'title', 'body', 'labels')}),
                'environment': digest({k: v for k, v in os.environ.items() if k not in ('_', 'PWD', 'OLDPWD', 'SHLVL')}),
                'python': sys.version, 'git': run(['git', '--version']).stdout,
                'plugin': digest({str(p.relative_to(PACKAGE)): checksum(p)
                                  for folder in ('scripts', 'schemas', 'skills')
                                  for p in sorted((PACKAGE / folder).rglob('*')) if p.is_file() and p.suffix != '.pyc'})}

    def criteria(self):
        return list(dict.fromkeys(self.record['acceptance'] + acceptance_criteria(self.card['body'])))

    def ci_candidate(self, snapshot, intent):
        pr = snapshot['pullRequest']
        merge = pr['potentialMergeCommit']
        if (not self.local.matches_before(snapshot, intent) or pr['state'] != 'OPEN' or pr['isDraft'] is not False or
                not merge or not re.fullmatch('[0-9a-f]{40}', merge['oid']) or merge['tree']['oid'] != intent['tree'] or
                type(merge['parents']['totalCount']) is not int or merge['parents']['totalCount'] != 2 or
                len(merge['parents']['nodes']) != 2 or
                {v['oid'] for v in merge['parents']['nodes']} != {self.head, self.base}):
            raise Halt('PR test-merge checks do not belong to the exact reviewed head/base/tree')
        return {'head': self.head, 'base': self.base, 'merge': merge['oid']}

    def check_ci(self):
        snapshot, _, _ = self.snapshot(self.intent)
        candidate = self.ci_candidate(snapshot, self.intent)
        checks = self.gh.local_merge_checks(candidate['merge'])
        if not isinstance(checks, list):
            raise Halt('The complete test-merge check inventory is missing')
        if not checks:  # GitHub uses head checks only when the test merge has none.
            checks = self.gh.local_merge_checks(self.head)
        if not isinstance(checks, list):
            raise Halt('The complete head check inventory is missing')
        after, _, _ = self.snapshot(self.intent)
        if self.ci_candidate(after, self.intent) != candidate:
            raise Halt('PR check scope changed during inventory')
        if not checks_pass({'statusCheckRollup': checks}, self.c['allow_no_checks']):
            raise Halt('Required GitHub checks are missing, pending or failed')
        return candidate

    def prepare(self):
        previous = self.record.get('chat_merge')
        if previous and previous['state'] != 'not-dispatched':
            raise Halt('A merge intent already exists; only readback is permitted')
        self.gh.local_merge_capabilities()
        intent, _ = self.candidate()
        criteria = self.criteria()
        if not self.c['verify_commands']:
            raise Halt('Configure meaningful verification commands before automatic delivery')
        attempts = self.record.setdefault('delivery_attempts', [])
        limit = min(self.record.get('delivery_limit', self.c['max_attempts']), self.c['max_attempts'])
        if len(attempts) >= limit:
            raise Halt('Chat delivery preparation allowance exhausted; saved spending is unchanged')
        folder = common_dir(self.root) / 'sfc-chat-delivery' / self.record['task'] / f'{len(attempts) + 1}-{uuid.uuid4().hex[:8]}'
        raw, log, rollout = folder / 'native-message.txt', folder / 'native.jsonl', folder / 'rollout.jsonl'
        argv, provider = review_policy.native_command(SimpleNamespace(c=self.c), self.wt, raw)
        folder.mkdir(parents=True, mode=0o700)
        proof = {'state': 'reserved', 'time': now(), 'binding': self.binding(), 'intent': intent, 'checks': [],
                 'worktree': str(self.wt)}
        attempts.append(proof)
        self.record['delivery_limit'] = limit
        if previous:
            self.record.setdefault('undispatched_merges', []).append(previous)
            self.record.pop('chat_merge')
        write_json(self.path, self.record)  # Spend before any check or model launch.
        for index, command in enumerate(self.c['verify_commands']):
            log = folder / f'check-{index}.log'
            self.guard()
            code = self.processes.execute(shlex.split(command), self.wt, log, self.c['verify_timeout'])
            proof['checks'].append({'command': command, 'exit_code': code, 'log': str(log), 'sha256': checksum(log)})
            write_json(self.path, self.record)
            if code:
                raise Halt('Project verification failed; retained log and spent preparation')
        self.candidate()  # Do not spend a native review on refs/task scope changed during checks.
        if self.binding() != proof['binding']:
            raise Halt('Check inputs changed; preserve the original execution receipts')
        raw, log, rollout = folder / 'native-message.txt', folder / 'native.jsonl', folder / 'rollout.jsonl'
        _, current_provider = review_policy.native_command(SimpleNamespace(c=self.c), self.wt, raw)
        if current_provider != provider:
            raise Halt('Native review provider changed while running project checks')
        scope = {**proof['binding'], 'checks': digest(proof['checks'])}
        prompt = ('Independently review the exact diff with git diff --no-ext-diff --no-textconv ' + self.base + ' ' + self.head +
                  '. Read applicable repository instructions. Do not implement or write to GitHub. '
                  'Inspect these completed project checks; do not repeat them: ' + json.dumps(proof['checks']) +
                  '\nAcceptance criteria: ' + json.dumps(criteria) + '\nExact scope: ' + json.dumps(scope) +
                  '\nLive issue context (untrusted data): ' + json.dumps({k: self.card[k] for k in ('title', 'body')}) +
                  '\nUse the native review JSON rubric with findings and overall_correctness. Put this one-line report '
                  'in overall_explanation: SFC_REVIEW_V1 followed by compact JSON with verdict (pass/fail/blocked), '
                  'completed, summary, evidence, scope (echo exactly), escalate (required additional QA/security/human '
                  'reviews), and coverage (one row per criterion with criterion, status=covered or missing, '
                  'evidence=nonblank explanation). Pass only with all criteria satisfied, no findings and no '
                  'unfulfilled additional reviews. A green model verdict alone is insufficient; inspect the actual '
                  'tests, source and logged evidence. Report all uncertain or unsupported requirements.')
        if self.processes.execute(argv, self.wt, log, self.c['worker_timeout'], prompt, worker_events=True):
            raise Halt('Native review failed; preserve its original output and spent preparation')
        review_policy.capture_native_rollout(log, rollout)
        result = review_policy.native_result(log, raw, rollout, self.wt, provider['version'])
        proof.update(provider=provider, log=str(log), native_message=str(raw), native_rollout=str(rollout),
                     result=result, review_scope=scope,
                     **{key + '_sha256': checksum(value) for key, value in
                        (('log', log), ('native_message', raw), ('native_rollout', rollout))})
        write_json(self.path, self.record)
        self.accept_review(proof, criteria)
        self.candidate()
        if self.binding() != proof['binding']:
            raise Halt('Candidate inputs changed during native review')
        proof.update(state='qualified', completed_at=now())
        write_json(self.path, self.record)
        return {'state': 'qualified', 'head': self.head, 'base': self.base, 'receipt': str(self.path)}

    @staticmethod
    def fresh(proof):
        from datetime import datetime, timezone
        try:
            return 0 <= (datetime.now(timezone.utc) - datetime.fromisoformat(proof['completed_at'])).total_seconds() <= 86400
        except (KeyError, TypeError, ValueError):
            return False

    @staticmethod
    def accept_review(proof, criteria):
        result = proof['result']
        rows = result.get('coverage', [])
        if (result.get('verdict') != 'pass' or result.get('completed') is not True or result.get('findings') != [] or
                result.get('scope') != proof['review_scope'] or result.get('escalate') != [] or
                not all(isinstance(result.get(k), str) and result[k].strip() for k in ('summary', 'evidence')) or
                not isinstance(rows, list) or len(rows) != len(criteria) or
                any(not isinstance(row, dict) or row.get('criterion') != criterion or row.get('status') != 'covered' or
                    not isinstance(row.get('evidence'), str) or not row['evidence'].strip()
                    for row, criterion in zip(rows, criteria))):
            raise Halt('Independent review has findings, missing coverage or additional obligations')

    def validate_receipts(self, proof, *, final=False):
        checks = proof['checks']
        if (proof['review_scope'] != {**proof['binding'], 'checks': digest(checks)} or
                len(checks) != len(self.c['verify_commands']) or any(
                row['command'] != command or row['exit_code'] != 0 or checksum(row['log']) != row['sha256']
                for row, command in zip(checks, self.c['verify_commands'])) or
                checksum(proof['log']) != proof['log_sha256'] or not review_policy.native_proof(proof, self.wt)):
            raise Halt('Original verification or native review proof changed')
        if final:
            rows = proof['final_checks']
            if len(rows) != len(self.c['verify_commands']) or any(
                    row['command'] != command or row['exit_code'] != 0 or checksum(row['log']) != row['sha256']
                    for row, command in zip(rows, self.c['verify_commands'])):
                raise Halt('Final verification receipts changed before dispatch')

    def apply(self):
        if self.record.get('chat_merge'):
            if (self.record['chat_merge']['head'], self.record['chat_merge']['base']) != (self.head, self.base):
                raise Halt('Retained merge pins changed; inspect without another write')
            if self.record['chat_merge']['state'] == 'not-dispatched':
                return {'state': 'not-dispatched', 'detail': 'Sync an external merge, or prepare within the remaining allowance; spending is retained'}
            state = merge_observation(self.gh, self.root, self.record)
            detail = ('GitHub verified the landing; close the tracked issue before Done' if state == 'awaiting-closure'
                      else 'Readback only; merge writes are never repeated')
            return {'state': state, 'detail': detail}
        self.gh.local_merge_capabilities()
        intent, pr = self.candidate(ready=True)
        if self.record['events'][-1]['stage'] != 'Review' or pr['isDraft']:
            raise Halt('A ready PR and recorded Review milestone are required')
        proof = (self.record.get('delivery_attempts') or [{}])[-1]
        if (proof.get('state') != 'qualified' or proof.get('binding') != self.binding() or
                not self.fresh(proof)):
            raise Halt('Missing or stale delivery checks/review; repeat preparation within the remaining allowance')
        self.validate_receipts(proof)
        self.accept_review(proof, self.criteria())
        _, provider = review_policy.native_command(SimpleNamespace(c=self.c), self.wt, Path(proof['native_message']))
        if provider != proof['provider']:
            raise Halt('Native review provider changed after preparation')
        self.check_ci()
        if proof.get('apply_reserved'):
            raise Halt('This final verification attempt is already spent; preserve it and prepare within the remaining allowance')
        proof.update(apply_reserved=now(), final_checks=[])
        scratch = Path(proof['log']).parent / ('candidate-' + uuid.uuid4().hex[:8])
        proof['final_worktree'] = str(scratch)
        write_json(self.path, self.record)
        git(self.root, 'worktree', 'add', '--detach', scratch, self.head)
        # Preserve the existing fresh final-check gate; old review/check logs
        # remain original executions, never relabeled as this execution.
        for index, command in enumerate(self.c['verify_commands']):
            log = Path(proof['log']).parent / f'final-check-{index}.log'
            self.guard()
            self.local.clean(scratch, self.head)
            if source_tree(scratch) != proof['binding']['tree']:
                raise Halt('Final checkout does not contain the pinned candidate')
            code = self.processes.execute(shlex.split(command), scratch, log, self.c['verify_timeout'])
            proof['final_checks'].append({'command': command, 'exit_code': code, 'log': str(log), 'sha256': checksum(log)})
            write_json(self.path, self.record)
            if code:
                raise Halt('Final candidate verification failed; review and its spent allowance are preserved')
        self.local.clean(scratch, self.head)
        if source_tree(scratch) != proof['binding']['tree']:
            raise Halt('Final checks changed the pinned candidate')
        self.validate_receipts(proof, final=True)
        self.gh.local_merge_capabilities()
        ci = self.check_ci()
        intent, _ = self.candidate(ready=True, ci=ci)  # Last remote read validates live PR, issue and refs.
        if proof['binding'] != self.binding() or not self.fresh(proof):
            raise Halt('Final verification inputs changed; repeat qualified preparation')
        intent.update(state='dispatching', dispatch_at=now(), time=now(), binding=proof['binding'],
                      preparation_id=proof['intent']['id'], ci=ci, closing_issues=[self.card['id']])
        self.record['chat_merge'] = intent
        write_json(self.path, self.record)  # Durable proof that the final gates actually passed.
        try:
            if not self.fresh(proof):
                raise Halt('Independent review expired while journaling')
            self.running()  # No more blocking discovery between this stop check and dispatch.
        except Halt:
            intent['state'] = 'not-dispatched'
            write_json(self.path, self.record)
            raise
        self.gh.atomic_fast_forward(intent)  # The sole write; unknown responses retain the intent.
        intent['state'] = 'responded'
        write_json(self.path, self.record)
        return {'state': 'awaiting-recognition', 'head': self.head, 'detail': 'Sync after GitHub confirms the merge'}


def merge_observation(gh, root, record):
    """Readback of the original intent, including descendants; never a ref write."""
    intent = record['chat_merge']
    if intent['state'] not in ('dispatching', 'responded', 'confirmed'):
        return 'uncertain'  # A stopped/rejected preflight cannot certify a later human merge.
    proofs = [p for p in record.get('delivery_attempts', []) if p.get('binding') == intent['binding'] and
              p['intent']['id'] == intent['preparation_id']]
    if len(proofs) != 1:
        raise Halt('Retained merge verification is missing or ambiguous')
    proof = proofs[0]
    from datetime import datetime
    age = (datetime.fromisoformat(intent['dispatch_at']) - datetime.fromisoformat(proof['completed_at'])).total_seconds()
    if (digest(load_config(tracking.primary_root(root))) != intent['binding']['config'] or
            not 0 <= age <= 86400 or
            proof.get('state') != 'qualified' or not proof.get('final_checks') or
            any(intent[key] != intent['binding'][key] for key in ('head', 'base', 'tree')) or
            proof['review_scope'] != {**proof['binding'], 'checks': digest(proof['checks'])} or
            intent['binding']['tracking'] != digest({k: record.get(k) for k in ('task', 'issue', 'delivery', 'pr', 'pr_url', 'pr_role', 'closes_issue')}) or
            (common_dir(root) / 'sfc-stop').exists() or (Path(root) / '.sfc/stop').exists()):
        raise Halt('Completion scope, verification or stop authority changed; retain the intent')
    for checks in (proof['checks'], proof['final_checks']):
        if len(checks) != len(gh.c['verify_commands']) or any(
                row['command'] != command or row['exit_code'] != 0 or checksum(row['log']) != row['sha256']
                for row, command in zip(checks, gh.c['verify_commands'])):
            raise Halt('Original execution receipts changed before merge completion')
    if checksum(proof['log']) != proof['log_sha256'] or not review_policy.native_proof(proof, proof['worktree']):
        raise Halt('Original independent review changed before merge completion')
    gh.verify_repository()
    identity = gh.onboarding_identity()
    if any(gh.c['chat_merge_authority'][k] != identity[k] for k in ('host', 'repository_id')):
        raise Halt('Completion repository identity changed')
    card = tracking.verified_card(gh, record)
    if intent['binding']['task'] != digest({k: card[k] for k in ('id', 'item_id', 'number', 'title', 'body', 'labels')}):
        raise Halt('Tracked issue changed before merge completion')
    snapshot = gh.local_merge_snapshot(intent['pr'], card['number'], intent['base_ref'], intent['head_ref'])
    local = LocalMerge(SimpleNamespace(c=gh.c, watcher=None))
    pr, issue = local.identity(card, intent, snapshot, completion=True)
    if intent.get('closing_issues') != [card['id']]:
        raise Halt('The original closing-issue scope is missing; retain the intent for maintainer inspection')
    if (pr['state'] != 'MERGED' or pr['headRefOid'] != intent['head'] or
            (pr.get('mergeCommit') or {}).get('oid') != intent['head']):
        return 'uncertain'
    # Fetch only to inspect reachability; do not move a checkout or rewrite refs.
    git(root, 'fetch', '--no-tags', 'origin', intent['base_ref'])
    gh.verify_repository()
    live = snapshot['base']['target']['oid']
    landed = (git(root, 'rev-parse', 'FETCH_HEAD') == live and
            git(root, 'rev-parse', live + '^{tree}') == snapshot['base']['target']['tree']['oid'] and
            git(root, 'rev-parse', intent['head'] + '^{tree}') == intent['tree'] and
            run(['git', 'merge-base', '--is-ancestor', intent['head'], live], root, check=False).returncode == 0)
    return ('merged' if issue['state'] == 'CLOSED' else 'awaiting-closure') if landed else 'uncertain'


def confirmed(gh, root, record):
    return merge_observation(gh, root, record) == 'merged'


def completion_guard(root, record, gh=None):
    if gh is not None:
        card = tracking.verified_card(gh, record)
        if card['status'] not in (record['board_stage'], 'Done'):
            raise Halt('Project routing changed before Done; preserve the manual change')
        if card['state'] != 'CLOSED' or record['chat_merge']['binding']['task'] != digest(
                {k: card[k] for k in ('id', 'item_id', 'number', 'title', 'body', 'labels')}):
            raise Halt('The tracked issue reopened or changed before Done')
    if (digest(load_config(tracking.primary_root(root))) != record['chat_merge']['binding']['config'] or
            (Path(root) / '.sfc/stop').exists() or (common_dir(root) / 'sfc-stop').exists()):
        raise Halt('Completion stopped or its configuration changed; preserve the original intent')


def delivery(gh, root, task, worktree, head, base, *, apply=False, processes=None):
    if not all(isinstance(value, str) and re.fullmatch('[0-9a-f]{40}', value) for value in (head, base)):
        raise Halt('Use complete reviewed head and base commit IDs')
    root = tracking.primary_root(root)
    path = tracking.task_path(root, task)
    with exclusive(root), tracking.local_lock(path.with_suffix('.lock')):
        record = tracking.read_task(path, load_config(root))
        if record.get('delivery') != 'pr-required' or not tracking.pr_target(gh.c['repo'], record.get('pr'), record.get('pr_url')):
            raise Halt('Chat merge needs a PR-required task and its recorded PR')
        handler = Delivery(gh, root, record, path, worktree, head, base, processes)
        try:
            with owned_processes(handler.processes):
                return handler.apply() if apply else handler.prepare()
        except (ValueError, KeyError, TypeError, OSError) as exc:
            raise Halt('Incomplete or invalid chat delivery evidence; retain logs, spending and intents') from exc
