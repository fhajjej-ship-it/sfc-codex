"""Rolling ticket admission. Only the supervisor thread chooses and claims work."""
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import time
import uuid

from . import pickup
from .activity import ActivityError
from .blocked_recovery import admitted as admitted_recovery, defer_read, pending_stage, waiting_on_read
from .common import Halt, Stopped, now
from .priority import ProjectItemMissing
from .rate_limit import ReadConnectionReset
from .reasons import RECOVERY_ITEM_MISSING, RECOVERY_READ_WAIT, WATCHER_ADMISSION_DEFERRED
from .releases import Releases, ReleaseTriggerReadReset, UnfinishedReleaseCreation, guard_release_creation
from .watch import settings


def schedule(f, watcher):
    clock = watcher.clock if watcher else time.monotonic
    pause = watcher.wait if watcher else time.sleep
    interval = watcher.settings['poll_interval'] if watcher else settings(f.c.get('watch'))['poll_interval']
    cycle = watcher.session['cycles'] if watcher else 0
    failures = watcher.session['no_progress'] if watcher else 0
    active = {}  # Future -> (card, reservation); one future owns every lane/check.
    dispatched = {}  # Only actually launched cards may receive a stop report.
    reservations = set()
    first = True
    next_poll = clock()
    release_retry_at = next_poll
    failed = False
    f.admission_deferred.clear()

    def defer(card):
        nonlocal next_poll
        f.admission_deferred.add(card['number'])
        if not watcher:
            # A finite run must still drain independent work after a rejection.
            # Do not spin on the changed card or silently accept its new task.
            next_poll = clock()

    def wait_for_github():
        nonlocal next_poll
        next_poll = clock() + interval
        watcher.session['next_poll'] = next_poll
        watcher.save('waiting for GitHub connection')

    def observe_queue():
        try:
            return f.queue_cards()
        except ReadConnectionReset:
            if not watcher:
                raise
            # Discard the incomplete observation, including during a refresh.
            # This cannot replay a claim or any other supervisor write.
            wait_for_github()
            return None

    def spending(message):
        # Outstanding reservations remain spent after interruption. Only this
        # live supervisor knows they are ongoing work rather than failures.
        f.state.update(cycle=cycle, no_progress=failures + len(reservations),
                       occupied=f.selection_order(list(f.occupied.values())))
        f.save()
        if watcher:
            watcher.active_reservations = len(reservations)
            watcher.session.update(cycles=cycle, no_progress=failures + len(reservations),
                                   occupied=f.state['occupied'], work_in_flight=bool(active or reservations))
            watcher.save(message)

    def collect():
        nonlocal failures, next_poll
        for future in list(active):
            if not future.done():
                continue
            card, reservation = active[future]
            try:
                result = future.result()
            except Exception as exc:
                # A cooperative deadline keeps its reserved spending and original
                # work window. All other unknown outcomes halt before admission.
                if (watcher and isinstance(exc, Stopped) and clock() >= watcher.session['deadline']
                        and not f.processes.stop_requested()):
                    if getattr(watcher, 'lifetime', False):
                        watcher.session['cooperative_cutoff'] = True
                    continue
                if not (f.processes.cancel_check and f.processes.cancel_check()):
                    f.processes.stop_path.touch()
                raise
            record = f.record(card)
            # A peer advancing the base can legitimately return this card to
            # QA. Newly accepted lane evidence/stage advancement is work, not a
            # failed admission. Findings still spend their original allowance.
            advanced = (card['status'] in ('QA', 'Review') and card['status'] != reservation['stage']) or any(
                receipt.get('time') != reservation['receipts'].get(lane)
                for lane, receipt in record.get('receipts', {}).items())
            progress = (card['status'] == 'Done' or (card['status'] == 'Blocked' and record.get('handoff')) or
                        (record.get('attempts', 0) == reservation['attempts'] and advanced))
            terminal_failure = card['status'] == record.get('status') == 'Blocked' and not record.get('handoff')
            if watcher and terminal_failure:
                # An earlier successful lane does not refund observer spending
                # when this admission ultimately times out or becomes blocked.
                progress = False
            # Known per-ticket outcomes are bounded by that ticket's attempts,
            # output reservations and terminal Blocked stage. They do not spend
            # the observer's no-progress allowance or renew a failed ticket.
            isolated = bool(watcher and not progress and (
                terminal_failure or result == WATCHER_ADMISSION_DEFERRED or
                record.get('attempts', 0) > reservation['attempts'] or
                (admitted_recovery(record) and (record.get('recovery_wait') or waiting_on_read(watcher, card)))))
            reservations.remove(reservation['id'])
            if not isolated:
                failures = 0 if progress else failures + 1
            reservation.update(state='finished', finished=now(), progress=bool(progress),
                               isolated_ticket_outcome=isolated)
            f.update(card, dispatch=reservation)
            del active[future]
            with f.mutex:
                del f.occupied[card['number']]
            spending('ticket released worker slot')
            next_poll = clock()

    with ThreadPoolExecutor(max_workers=f.c['max_workers']) as pool:
        try:
            while True:
                collect()
                if (not active and (not watcher or clock() < watcher.session['deadline']) and
                        any(r.get('local_merge') and r['local_merge'].get('phase') != 'done'
                            for r in list(f.state['cards'].values()))):
                    # Admission exhaustion does not suppress journal recovery.
                    # This path cannot create a new merge/ref mutation.
                    with f.merge_mutex:
                        f.reconcile(f.queue_cards())
                if f.processes.stopped():
                    f.state['outcome'] = (watcher.limit() if watcher else None) or 'stopped'
                    break
                # Completed failure exhaustion must not look like a normal
                # poll/claim cutoff to the lifetime window admission protocol.
                limit = ('no-progress-limit' if failures >= f.c['max_no_progress'] else
                         watcher.limit() if watcher else None)
                limit = limit or ('cycle-limit' if cycle >= f.c['max_cycles'] else
                                  'no-progress-limit' if failures >= f.c['max_no_progress'] else None)
                if limit:
                    if active:
                        # Admission exhaustion is not a command cancellation.
                        done, _ = wait(active, timeout=0.01, return_when=FIRST_COMPLETED)
                        if not done:
                            pause(0.1)
                        continue
                    f.state['outcome'] = limit
                    break
                if clock() < next_poll:
                    if active:
                        done, _ = wait(active, timeout=0.01, return_when=FIRST_COMPLETED)
                        if done:
                            continue
                    pause(min(0.1, max(0.000001, next_poll - clock())))
                    continue
                f.ensure_running()
                if watcher:
                    f.admission_deferred.clear()
                    watcher.poll()
                guard_release_creation(f.root)
                if not f.occupied and clock() >= release_retry_at:
                    f.state.pop('release_trigger', None)
                    if f.c.get('release', {}).get('enabled'):
                        try:
                            release_trigger = Releases(f).trigger()
                        except ReleaseTriggerReadReset as exc:
                            # Only the automatic release probe failed. The
                            # ordinary queue still needs its own fresh inventory.
                            release_retry_at = clock() + interval
                            release_trigger = {'state': 'pending', 'reason': str(exc)}
                        except Exception:
                            # If the trigger failed after it saved a creating
                            # journal, latch that journal while preserving the
                            # original read/write failure in the run receipt.
                            try:
                                guard_release_creation(f.root)
                            except UnfinishedReleaseCreation as creation:
                                creation.latch(f.processes.stop_path)
                            raise
                        if release_trigger is not None:
                            f.state['release_trigger'] = release_trigger
                cards = observe_queue()
                if cards is None:
                    continue
                # An executing card owns its own journal. Idle uncertainty takes
                # precedence over every ordinary priority admission.
                idle_records = [r for n, r in list(f.state['cards'].items()) if int(n) not in f.occupied]
                if any(r.get('claim') and r['claim'].get('state') != 'verified' for r in idle_records):
                    raise Halt('Unresolved priority claim receipt; inspect remote state before restarting')
                pending_activity = {int(n) for n, r in list(f.state['cards'].items())
                                    if int(n) not in f.occupied and
                                    (r.get('activity', {}).get('pending') or r.get('activity', {}).get('error'))}
                for number in sorted(pending_activity):
                    card = next((c for c in cards if c['number'] == number), None)
                    if card is None or (f.include_backlog and number not in f.issue_scope):
                        raise ActivityError('Unresolved activity is outside the verified queue scope')
                    f.activity.reconcile(card, f.ensure_running)
                if any(r.get('ticket_failure') and r['ticket_failure'].get('phase') != 'blocked' for r in idle_records):
                    raise Halt('Unresolved ticket-failure publication; inspect retained receipts before resuming')
                # A finite run can exit on this plan, so it always refreshes
                # after reconciliation even when the first inventory is empty.
                refresh_queue = not watcher or bool(active)
                if first:
                    first = False
                    for card in cards:
                        record = f.state['cards'].get(str(card['number']), {})
                        completion = record.get('external_completion', {})
                        if (not f.creation_problem(card) and
                                (not f.include_backlog or (card['number'] in f.issue_scope and card['status'] in ('Building', 'QA', 'Review'))) and
                                not (completion.get('kind') == 'protected-squash' and completion.get('phase') != 'done') and
                                not pending_stage(record) and not admitted_recovery(record) and
                                record.get('activity', {}).get('state') == 'Running'):
                            f.activity.report(card, 'Unknown', 'unknown')
                            refresh_queue = True
                # Recovery and ordinary merges share serialization. Do not block
                # periodic queue observation behind a peer's merge verification.
                if f.merge_mutex.acquire(blocking=False):
                    try:
                        refresh_queue = f.reconcile(cards) is not False or refresh_queue
                    finally:
                        f.merge_mutex.release()
                else:
                    refresh_queue = True
                if refresh_queue:
                    cards = observe_queue()
                    if cards is None:
                        continue
                    selected, waiting = f.plan(cards)
                else:
                    # A poll can reuse positive dependency observations for
                    # ranking. Open dependencies are reread after refresh,
                    # and the selected card is checked live before admission.
                    closed = set()
                    selected, waiting = f.plan(cards, closed=closed)
                    if selected or watcher.limit() == 'watch-polls-limit':
                        cards = observe_queue()
                        if cards is None:
                            continue
                        closed.difference_update(card['number'] for card in cards if card['state'] != 'CLOSED')
                        selected, waiting = f.plan(cards, closed=closed)
                if f.processes.stopped():
                    f.state['outcome'] = (watcher.limit() if watcher else None) or 'stopped'
                    break
                if watcher and clock() >= watcher.session['deadline']:
                    f.state['outcome'] = 'watch-time-limit'
                    break
                occupied = f.selection_order(list(f.occupied.values()))
                f.state['plans'].append({'cycle': cycle, 'time': now(), 'occupied': occupied,
                                         'max_workers': f.c['max_workers'], 'next_eligible': f.next_eligible,
                                         'selected': f.selection_order(selected), 'waiting': waiting})
                f.state.update(waiting=waiting, occupied=occupied)
                f.save()
                if watcher:
                    watcher.session.update(occupied=occupied, max_workers=f.c['max_workers'], next_eligible=f.next_eligible,
                                           waiting=waiting, next_poll=clock() + interval)
                    watcher.save('queue planned')
                next_poll = clock() + interval
                if not selected:
                    if active:
                        continue
                    if watcher:
                        watcher.idle('waiting for eligible work' if any(not w.get('excluded') for w in waiting) else 'empty queue')
                        next_poll = clock()
                        continue
                    f.state['outcome'] = 'waiting' if any(not w.get('excluded') for w in waiting) else 'drained'
                    break
                # Admit one ticket, then reread membership/fields/holds/priority
                # before selecting another. Claims never run in worker threads.
                can_replan = watcher is not None
                while selected:
                    card = selected[0]
                    f.ensure_running()
                    if f.creation_problem(card):
                        reason = 'ticket creation became unverified'
                    else:
                        reason = 'live task/scope/prerequisites changed'
                        try:
                            live_scope = pickup.scope(f.gh)
                            current = pickup.card(f.gh, card['item_id'], live_scope)
                        except ReadConnectionReset:
                            if not (watcher and (card['status'] == 'Blocked' or admitted_recovery(f.record(card)))):
                                raise
                            defer_read(watcher, card)
                            reason = RECOVERY_READ_WAIT
                        except ProjectItemMissing:
                            if not (watcher and live_scope == card['_pickup_scope'] and
                                    (card['status'] == 'Blocked' or admitted_recovery(f.record(card))) and
                                    pickup.scope(f.gh) == live_scope):
                                raise
                            reason = RECOVERY_ITEM_MISSING
                            f.update(card, recovery_wait=reason)
                        else:
                            if (current == card and live_scope == card['_pickup_scope'] and
                                    (not watcher or not watcher.excludes(current)) and
                                    not pickup.prerequisite_problems(f.gh, current)):
                                break
                    f.event(card, 'Admission deferred: ' + reason)
                    defer(card)
                    if not can_replan:
                        selected = []
                        break
                    # Reconsider the current queue once in this poll after
                    # an admission rejection, without stale ranking hints.
                    can_replan = False
                    cards = observe_queue()
                    if cards is None:
                        selected = []
                        break
                    selected, waiting = f.plan(cards)
                    f.state['plans'][-1].update(selected=f.selection_order(selected),
                                                next_eligible=f.next_eligible, waiting=waiting)
                    f.state['waiting'] = waiting
                    f.save()
                    watcher.session.update(next_eligible=f.next_eligible, waiting=waiting)
                    watcher.save('queue replanned')
                if not selected:
                    continue
                with f.mutex:
                    # An inspection can take the last spare slot after queue
                    # planning. Reserve before claiming or spending admission.
                    if not f.worker_capacity():
                        continue
                    f.occupied[card['number']] = card
                cycle += 1
                reservation = {'id': uuid.uuid4().hex, 'state': 'intent', 'time': now(),
                               'run_id': f.state['run_id'], 'cycle': cycle}
                f.update(card, dispatch=reservation)
                spending('ticket admission reserved')
                if card['status'] in ('Backlog', 'Ready'):
                    if not f.claim_priority(card):
                        with f.mutex:
                            del f.occupied[card['number']]
                        defer(card)
                        continue
                f.ensure_running()
                record = f.record(card)
                reservation.update(stage=card['status'], attempts=record.get('attempts', 0),
                                   receipts={lane: receipt.get('time') for lane, receipt in record.get('receipts', {}).items()})
                reservations.add(reservation['id'])
                spending('worker dispatch reserved')
                reservation.update(state='launched')
                f.update(card, dispatch=reservation)
                future = pool.submit(f.process, card)
                active[future] = (card, reservation)
                dispatched[card['number']] = card
                spending('ticket executing')
                if len(active) < f.c['max_workers']:
                    next_poll = clock()
        except BaseException as exc:
            if (watcher and isinstance(exc, Stopped) and clock() >= watcher.session['deadline']
                    and not f.processes.stop_requested()):
                f.state['outcome'] = 'watch-time-limit'
                if getattr(watcher, 'lifetime', False):
                    watcher.session['cooperative_cutoff'] = True
            else:
                failed = True
                if not (f.processes.cancel_check and f.processes.cancel_check()):
                    if isinstance(exc, UnfinishedReleaseCreation):
                        exc.latch(f.processes.stop_path)
                    else:
                        f.processes.stop_path.touch()
                raise
        finally:
            # Keep the common supervisor lock through shutdown. Completion-only
            # recovery cannot run concurrently with any admitted worker.
            pending_error = None
            for future in active:
                try:
                    future.result()
                except Exception as exc:
                    if (watcher and isinstance(exc, Stopped) and clock() >= watcher.session['deadline']
                            and not f.processes.stop_requested()):
                        if getattr(watcher, 'lifetime', False):
                            watcher.session['cooperative_cutoff'] = True
                    elif pending_error is None:
                        pending_error = exc
            f.occupied.clear()
            f.admission_deferred.clear()
            f.state['occupied'] = []
            if watcher:
                watcher.session['occupied'] = []
            if pending_error is not None and not failed:
                raise pending_error
            if not failed and f.state['outcome'] in ('stopped', 'cycle-limit', 'no-progress-limit'):
                for card in dispatched.values():
                    if (card['status'] not in ('Done', 'Blocked') and
                            f.record(card).get('activity', {}).get('state') != 'Stopped'):
                        f.stopped_activity(card, None if f.state['outcome'] == 'stopped' else f.state['outcome'])
