"""Operator-authorized pre-tag correction of a retained release card."""
from __future__ import annotations

from pathlib import Path
import re

from .common import Halt, git, now, read_json, write_json
from .releases import SHA, checksum, digest, parse_version


def live_card(releases):
    journal = releases.journal
    card = releases.remote.pinned_card(journal['card'])
    if (card.get('state') != 'OPEN' or
            any(card.get(key) != value for key, value in journal['card'].items()) or
            releases.f.task_digest(card) != journal['task_digest']):
        raise Halt('Correction requires the original open release issue, item and task')
    return card


def no_publication(releases):
    journal = releases.journal
    if (journal.get('release_id') or
            any(key in journal['effects'] for key in ('tag', 'draft', 'publication', 'close', 'stage-Done')) or
            releases.remote.tag(journal['version']) is not None or
            releases.remote.release(journal['version']) is not None):
        raise Halt('Release correction requires no tag, release or publication intent')


def failed_checks(releases, record):
    journal = releases.journal
    for phase in ('package', 'preflight', 'artifact'):
        receipt = journal.get(phase + '_checks', {})
        if (receipt.get('state') == 'failed' and receipt.get('candidate') == journal['candidate'] and
                receipt.get('config_digest') == journal['config_digest']):
            for check in receipt.get('checks', []):
                log = Path(check['log'])
                if check['exit_code'] and not log.is_symlink() and log.is_file() and checksum(log) == check['log_sha256']:
                    return True
    if journal.get('project_check_state') == 'failed':
        from .verification import load, original
        try:
            receipt = load(record['verification'])
            if (receipt['kind'] != 'verification' or receipt['passed'] or
                    any(receipt[key] != journal[pin] for key, pin in (
                        ('head', 'candidate'), ('config_digest', 'config_digest'), ('task_digest', 'task_digest')))):
                return False
            value, _ = original(receipt['checks'][-1])
            return bool(value['exit_code'] and checksum(value['log']) == value['log_sha256'])
        except (IndexError, KeyError, OSError, TypeError, ValueError):
            return False
    return False


def corrected_plan(releases, candidate):
    journal = releases.journal
    boundary, head, changes = releases.source()
    if (head != candidate or boundary != journal['boundary'] or
            git(releases.f.root, 'merge-base', journal['candidate'], head) != journal['candidate']):
        raise Halt('Correction must be the exact merged base tip extending the frozen source and boundary')
    if journal.get('version_from_file', bool(releases.cfg.get('version_file'))):
        if releases.select_version(head) != journal['version']:
            raise Halt('Corrected source version differs from the retained release card')
    if not set(journal['changes']['issues']) <= set(changes['issues']):
        raise Halt('Correction would remove a previously assigned release member')
    from .release_latest import freeze as freeze_latest
    from .release_notes import freeze as freeze_notes
    plan = {'candidate': head, 'boundary': boundary, 'changes': changes}
    plan['public_notes'] = freeze_notes(releases.f.root, {**journal, **plan},
                                       releases.cfg.get('public_docs', {}), releases.cfg.get('approved_summaries'))
    plan['latest_plan'] = freeze_latest(releases.remote, journal['version'],
                                         releases.cfg.get('latest_policy', 'newer-stable'))
    return plan


def correct_unstarted_plan(releases, card, record, result, *, apply):
    journal = releases.journal
    if (card['status'] not in ('Backlog', 'Ready') or record.get('status', card['status']) != card['status'] or
            record.get('workflow_kind') != 'release' or record.get('release_version') != journal['version'] or
            record.get('attempts') != 0 or journal['effects'] or 'attempt_reservation' in journal or
            any(key in record for key in ('claim', 'release_attempt', 'worktree', 'verification', 'release_evidence', 'receipts'))):
        raise Halt('Correction requires an untouched planned release with no attempts or release effects')
    candidate = result['candidate']
    if candidate == journal['candidate']:
        if journal.get('previous_plan'):
            return {**result, 'state': 'planned', 'attempt': 1, 'deduplicated': True}
        raise Halt('Correction requires a newer merged source commit')
    plan = corrected_plan(releases, candidate)
    if not apply:
        return {**result, 'state': 'preview', 'attempt': 1}

    # No board transition or debit is needed. Retain the original frozen plan
    # before atomically changing the pinned candidate in its existing journal.
    if live_card(releases)['status'] != card['status']:
        raise Halt('Planned release stage changed; preserve manual edit')
    no_publication(releases)
    releases.f.ensure_running()
    snapshot = releases.directory / journal['version'] / f"prior-plan-{journal['candidate']}.json"
    if snapshot.is_symlink() or (snapshot.exists() and read_json(snapshot) != journal):
        raise Halt('Prior release plan snapshot conflicts; preserve its evidence')
    if not snapshot.exists():
        write_json(snapshot, journal)
    releases.journal = {**journal, **plan, 'previous_plan': {'path': str(snapshot), 'sha256': checksum(snapshot)}}
    releases.save()
    return {**result, 'state': 'planned', 'attempt': 1}


def recover(releases, version, candidate, *, apply=False):
    """The caller owns the common supervisor lock. Repeating a transition reads back only."""
    version = parse_version(version)
    if not re.fullmatch(SHA, candidate):
        raise Halt('Correction requires a full merged commit SHA')
    releases.path = releases.directory / (version + '.json')
    if not releases.path.is_file():
        raise Halt('No original release journal for this version; inspect the existing release card')
    j = releases.journal = read_json(releases.path)
    f = releases.f
    if (j.get('workflow_kind') != 'release' or j.get('version') != version or not j.get('card') or
            j.get('owner_root', str(f.root)) != str(f.root)):
        raise Halt('Correction requires the original release journal and owning checkout')
    releases.checked()
    result = {'version': version, 'candidate': candidate, 'card': j['card'], 'receipt': str(releases.path)}
    pending = j.get('pending_correction')
    if not pending and j.get('last_correction', {}).get('candidate') == candidate:
        return {**result, 'state': j['state'], 'deduplicated': True}
    card = live_card(releases)
    no_publication(releases)
    record = f.record(card)
    if not pending and j['state'] == 'planned':
        return correct_unstarted_plan(releases, card, record, result, apply=apply)
    if pending:
        if pending['plan']['candidate'] != candidate:
            raise Halt('A different correction is pending; reconcile its exact candidate first')
        if not apply:
            return {**result, 'state': 'correction-pending', 'attempt': pending['attempt']}
    else:
        if (not releases.valid_attempt(j, record) or
                j.get('attempt_reservation') != record.get('release_attempt')):
            raise Halt('Correction requires a pinned prior attempt and its original debit')
        if (record['status'] != card['status'] or
                j['state'] not in ('building', 'qa', 'review', 'blocked') or
                j['state'] != card['status'].lower() or
                any(effect.get('state') != 'verified' for effect in j['effects'].values())):
            raise Halt('Release stage or prior effects require reconciliation before correction')
        if j['state'] == 'blocked':
            effect = j['effects'].get('stage-Blocked', {})
            if effect.get('observed') != 'Blocked' or not releases.retained_rejection(card, record):
                raise Halt('Blocked release has no pinned independent finding')
        elif not failed_checks(releases, record):
            raise Halt('Release has no completed pinned check failure to correct')
        attempt = record['attempts'] + 1
        if attempt > f.c['max_attempts']:
            raise Halt('Release attempts exhausted; keep the original card and evidence')
        if candidate == j['candidate']:
            raise Halt('Correction requires a newer merged source commit')
        plan = corrected_plan(releases, candidate)
        if not apply:
            return {**result, 'state': 'preview', 'attempt': attempt}
        snapshot = releases.directory / version / f'prior-attempt-{attempt - 1}.json'
        previous = {'journal': j, 'card_record': record}
        if snapshot.exists() or snapshot.is_symlink():
            if snapshot.is_symlink() or read_json(snapshot) != previous:
                raise Halt('Prior release attempt snapshot conflicts; preserve its evidence')
        else:
            write_json(snapshot, previous)
        pending = {'attempt': attempt, 'from': card['status'], 'plan': plan,
                   'prior': {'path': str(snapshot), 'sha256': checksum(snapshot)}, 'authorized_at': now()}
        j['pending_correction'] = pending
        releases.save()

    attempt = pending['attempt']
    prior = Path(pending['prior']['path'])
    if (prior != releases.directory / version / f'prior-attempt-{attempt - 1}.json' or prior.is_symlink() or
            checksum(prior) != pending['prior']['sha256']):
        raise Halt('Prior release attempt snapshot changed; preserve its evidence')
    previous = read_json(prior)
    previous_record = previous['card_record']
    frozen = {
        key: j[key] for key in ('workflow_kind', 'owner_root', 'request_id', 'version', 'identity',
                               'config_digest', 'created', 'card', 'task_digest', 'version_from_file',
                               'field_identity', 'factory_view', 'members', 'member_before') if key in j
    }
    frozen.update(pending['plan'], state='building',
                  effects={key: value for key, value in j['effects'].items() if key.startswith('target-')},
                  last_correction={'candidate': candidate, 'attempt': attempt, 'prior': pending['prior'],
                                   'authorized_at': pending['authorized_at']})
    if previous_record.get('verification', {}).get('passed'):
        frozen['prior_verification'] = previous_record['verification']
    reservation = {'attempt': attempt, 'scope': releases.attempt_scope(frozen)}
    frozen['attempt_reservation'] = reservation
    old_debit = (previous_record['attempts'], previous_record.get('release_attempt'))
    new_debit = (attempt, reservation)
    actual_debit = (record.get('attempts'), record.get('release_attempt'))
    if (not 2 <= attempt <= f.c['max_attempts'] or previous_record['attempts'] != attempt - 1 or
            actual_debit not in (old_debit, new_debit)):
        raise Halt('Release correction attempt counter changed; preserve both attempts')
    if (git(f.root, 'merge-base', candidate, f.fetch()) != candidate or
            releases.previous(candidate, version) != frozen['boundary'] or
            git(f.root, 'merge-base', j['candidate'], candidate) != j['candidate']):
        raise Halt('Corrected source ancestry or published boundary changed before admission')

    def move():
        no_publication(releases)
        current = live_card(releases)
        if current['status'] != pending['from']:
            raise Halt('Correction source stage changed; preserve manual edit')
        f.gh.move(current, 'Building')

    if pending['from'] != 'Building':
        releases.effect('correction-Building', lambda: live_card(releases)['status'],
                        lambda status: status == 'Building', move, before=lambda status: status == pending['from'])
    if live_card(releases)['status'] != 'Building':
        raise Halt('Correction stage changed; preserve manual edit')
    f.ensure_running()
    if actual_debit == old_debit:
        area = releases.directory / version / f'correction-{attempt}' / 'candidate'
        with f.mutex:
            for field in ('verification', 'release_evidence', 'inspection_rejection', 'receipts'):
                record.pop(field, None)
            f.update(card, attempts=attempt, release_attempt=reservation, status='Building',
                     worktree=str(area), release_correction_pending=False)
    frozen['last_correction']['transition'] = j['effects'].get('correction-Building')
    releases.journal = frozen
    releases.save()
    return {**result, 'state': 'building', 'attempt': attempt}
