"""Bind an automatic code repair to a rejected candidate and its saved QA report."""
from pathlib import Path

from .blocked_recovery import RecoveryPinMismatch, RecoveryReadDeferred, require_live
from .common import Halt, Stopped, TimedOut, git, read_json, run
from .rate_limit import ReadConnectionReset
from .verification import fingerprint_budget, load


def capture(f, card, result):
    record = f.record(card)
    receipt = record.get('receipts', {}).get('qa', {})
    if (card['status'] != 'QA' or 'qa' in card['labels'] or record.get('workflow_kind') == 'release' or
            not record.get('pr') or result['verdict'] != 'fail' or not result.get('findings') or
            receipt.get('result') != result or not receipt.get('receipt_sha256')):
        return None
    return {'pr': record['pr'], 'head': record['head'], 'base': record['base'],
            'task': f.task_digest(card), 'config': f.config_digest,
            'review': {key: receipt[key] for key in ('receipt_path', 'receipt_sha256')}}


def report(f, card, repair):
    try:
        reference = repair['review']
        path = Path(reference['receipt_path'])
        if path.is_symlink() or not path.resolve().is_relative_to((f.dir / 'runs').resolve()):
            raise ValueError('QA receipt escapes the retained runs')
        receipt = load({**read_json(path), **reference}, fingerprint_budget(f))
        if (receipt['lane'] != 'qa' or receipt['kind'] not in ('review', 'baseline-source-review') or
                receipt['head'] != repair['head'] or receipt['base'] != repair['base'] or
                receipt['task_digest'] != repair['task'] or receipt['config_digest'] != repair['config'] or
                repair['task'] != f.task_digest(card) or repair['config'] != f.config_digest or
                receipt['result']['verdict'] != 'fail' or not receipt['result']['findings']):
            raise ValueError('QA repair pins differ from the rejected candidate')
        return receipt['result']
    except (Stopped, TimedOut):
        raise
    except (Halt, OSError, TypeError, ValueError, KeyError) as exc:
        raise RecoveryPinMismatch('Saved QA finding evidence changed; inspect before repairing') from exc


def preflight(f, card, worktree, repair):
    expected_head = repair.get('candidate', repair['head'])
    record = f.record(card)
    if (repair['pr'] != record.get('pr') or repair['base'] != record.get('base') or
            expected_head != record.get('head') or git(worktree, 'rev-parse', 'HEAD') != expected_head):
        raise RecoveryPinMismatch('QA repair candidate changed; inspect the saved PR')
    report(f, card, repair)
    observe_pr(f, card, worktree, expected_head)
    require_task(f, card)


def observe_pr(f, card, worktree, expected_head, previous_head=None):
    try:
        return f.assert_remote(card, worktree, expected_head=expected_head, previous_head=previous_head)
    except ReadConnectionReset as exc:
        if f.watcher:
            raise RecoveryReadDeferred(str(exc)) from exc
        raise
    except (Stopped, TimedOut, RecoveryPinMismatch):
        raise
    except Halt as exc:
        raise RecoveryPinMismatch('The saved QA repair PR could not be verified') from exc


def require_task(f, card):
    if f.watcher:
        require_live(f, card, card['_pickup_scope'])
    elif not f.fresh_task(card):
        raise RecoveryPinMismatch('The task changed before the saved QA repair')


def active(card, record):
    repair = record.get('qa_repair') or {}
    return (card['status'] in ('Building', 'QA') and
            card['status'] == record.get('status') and
            record.get('pr') and repair.get('pr') == record['pr'])


def require_review_lineage(record, worktree, repair, head, base):
    """Carry the rejected finding to fresh QA of descendants on its pinned PR."""
    ancestry = ((repair['candidate'], head), (repair['base'], base), (base, head))
    if (record.get('pr') != repair['pr'] or record.get('head') != head or record.get('base') != base or
            any(ancestor != tip and run(['git', 'merge-base', '--is-ancestor', ancestor, tip],
                                        worktree, check=False).returncode for ancestor, tip in ancestry)):
        raise RecoveryPinMismatch('QA repair evidence does not belong to this PR, source or base')


def prepare_publication(f, card, worktree, repair, result):
    """Pin the accepted index before committing or reading the publication state."""
    record = f.record(card)
    tree = git(worktree, 'write-tree')
    if tree == git(worktree, 'rev-parse', repair['base'] + '^{tree}'):
        raise RecoveryPinMismatch('QA repair would remove the entire implementation')
    prepared = {key: value for key, value in repair.items() if key != 'accepted_build'}
    prepared['publication'] = {'phase': 'commit-ready', 'tree': tree, 'branch': record['branch'],
                               'pr': repair['pr'], 'evidence': result['evidence']}
    f.update(card, qa_repair=prepared)
    return prepared


def publish(f, card, worktree, repair):
    """Continue the same corrected commit and verify its original PR before QA."""
    record = f.record(card)
    publication = repair['publication']
    old = repair['head']
    branch = publication['branch']
    if (repair['pr'] != publication['pr'] or repair['pr'] != record.get('pr') or
            branch != record.get('branch') or repair['base'] != record.get('base') or record.get('head') != old):
        raise RecoveryPinMismatch('QA repair publication pins changed')
    report(f, card, repair)
    head = git(worktree, 'rev-parse', 'HEAD')
    if publication['phase'] == 'commit-ready':
        if head == old:
            preflight(f, card, worktree, repair)
            if (git(worktree, 'write-tree') != publication['tree'] or git(worktree, 'diff', '--name-only') or
                    git(worktree, 'ls-files', '--others', '--exclude-standard')):
                raise RecoveryPinMismatch('QA repair index changed before committing')
            f.ensure_running()
            git(worktree, 'commit', '-m', f"Fix #{card['number']}: {card['title']}")
            head = git(worktree, 'rev-parse', 'HEAD')
        if (head == old or git(worktree, 'show', '-s', '--format=%P', head) != old or
                git(worktree, 'rev-parse', head + '^{tree}') != publication['tree']):
            raise RecoveryPinMismatch('QA repair commit does not match the accepted index')
        publication.update(head=head, phase='ready-to-push')
        f.save()
    if (head != publication.get('head') or git(worktree, 'branch', '--show-current') != branch or
            git(worktree, 'status', '--porcelain', '--untracked-files=all')):
        raise RecoveryPinMismatch('QA repair checkout changed before publication')

    ref = 'refs/heads/' + branch

    def remote_head():
        f.ensure_running()
        try:
            return git(worktree, 'ls-remote', '--refs', 'origin', ref)
        except (Stopped, TimedOut):
            raise
        except Halt as exc:
            if f.watcher:
                raise RecoveryReadDeferred('QA repair remote branch read is temporarily unavailable') from exc
            raise

    if publication['phase'] == 'ready-to-push':
        prs = f.gh.pull_requests(branch)
        opened = [pr['number'] for pr in prs if pr['state'] == 'OPEN' and pr['baseRefName'] == f.c['base_branch']]
        if (any(pr['state'] == 'MERGED' for pr in prs) or opened != [repair['pr']] or
                remote_head() != f'{old}\t{ref}'):
            raise RecoveryPinMismatch('QA repair PR or rejected remote head changed before push')
        require_task(f, card)
        publication['phase'] = 'push-intent'
        f.save()
        try:
            f.ensure_running()
        except Halt:
            publication['phase'] = 'ready-to-push'
            f.save()
            raise
        try:
            git(worktree, 'push', 'origin', f'{head}:{ref}')
        except (Stopped, TimedOut):
            raise
        except Halt:
            pass  # The push may have landed. Observe the exact ref without repeating it.
    if publication['phase'] != 'push-intent' or remote_head() != f'{head}\t{ref}':
        raise RecoveryPinMismatch('QA repair push outcome is unresolved; preserve the saved intent')
    pr = observe_pr(f, card, worktree, head, previous_head=old)
    if pr['headRefOid'] != head:
        if f.watcher:
            raise RecoveryReadDeferred('QA repair PR has not observed the verified push yet')
        raise Halt('QA repair PR has not observed the verified push yet; resume its saved intent')
    verified = {**repair, 'publication': {**publication, 'phase': 'verified'},
                'candidate': head, 'build_evidence': publication['evidence'], 'awaiting_qa': True}
    f.update(card, head=head, qa_repair=verified)
    return verified
