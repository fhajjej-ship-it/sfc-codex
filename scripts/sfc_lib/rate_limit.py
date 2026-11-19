"""Bounded waits for positively identified GitHub read failures."""
from contextlib import contextmanager
from datetime import timezone
from email.utils import parsedate_to_datetime
import json
import re
import threading
import time

from .common import Halt, Stopped, _observer_guard


class RateLimited(Halt):
    def __init__(self, headers):
        super().__init__('GitHub read rate limited')
        self.headers = headers


class ConnectionReset(Halt):
    """GitHub did not confirm the request outcome."""


class ReadConnectionReset(ConnectionReset):
    """A read exhausted its retry allowance without a complete response."""


def connection_reset(stderr):
    return bool(re.fullmatch(
        r'(?:gh: )?(?:Get|Post|Patch|Put|Delete) "https?://[^"\r\n]+": '
        r'(?:read|write) tcp \S+->\S+: (?:read|write): connection reset by peer',
        stderr.strip()))


class RateWaitStopped(Stopped):
    """A confirmed read was waiting; it performed no mutation."""


def read_only(args, payload):
    if args[:2] in (('repo', 'view'), ('project', 'view'), ('pr', 'view'),
                    ('pr', 'list'), ('issue', 'view')):
        return True
    if not args or args[0] != 'api' or len(args) < 2:
        return False
    if args[1] == 'graphql':
        try:
            if payload is not None:
                # CLI fields can override an input document. Reject mixed sources.
                if args[2:] not in ((), ('--input', '-')):
                    return False
                query = json.loads(payload)['query']
            else:
                # cards() uses raw CLI fields for query and pagination variables.
                # Accept only literal, unique field names, with one query source.
                fields = {}
                if not args[2:] or len(args[2:]) % 2:
                    return False
                for flag, field in zip(args[2::2], args[3::2]):
                    name, separator, value = field.partition('=')
                    if (flag not in ('-f', '--raw-field') or not separator or
                            not re.fullmatch(r'[_A-Za-z][_0-9A-Za-z]*', name) or name in fields):
                        return False
                    fields[name] = value
                query = fields['query']
            # Explicit queries only. An alias named mutation is still a read;
            # an operation keyword cannot be followed by a colon. Keep all other
            # mutation/unknown documents conservative, including mixed operations.
            return (isinstance(query, str) and bool(re.match(r'^\s*query\b', query)) and
                    not re.search(r'\b(?:mutation|subscription)\b(?!\s*:)', query))
        except (ValueError, TypeError, KeyError):
            return False
    # gh implicitly uses POST when supplied data. Only known GET CLI shapes.
    return payload is None and all(a in ('--paginate', '--slurp') for a in args[2:])


def response(stdout):
    headers = {}
    text = stdout.replace('\r\n', '\n')
    while text.startswith('HTTP/'):
        head, separator, text = text.partition('\n\n')
        if not separator:
            raise Halt('Incomplete GitHub response headers')
        headers = {}
        for line in head.splitlines()[1:]:
            name, colon, value = line.partition(':')
            if colon:
                headers[name.lower()] = value.strip()
    return headers, text


def api_response(stdout, *, slurp=False, paginate=False):
    """Decode gh's per-page --include framing without scanning JSON strings."""
    text = stdout.replace('\r\n', '\n').strip()
    wrapped = slurp and text.startswith('[') and text[1:].lstrip().startswith('HTTP/')
    if wrapped:
        text = text[1:].lstrip()
    pages = []
    decoder = json.JSONDecoder()
    while text.startswith('HTTP/'):
        headers, body = response(text)
        data, end = decoder.raw_decode(body.lstrip())
        pages.append((headers, data))
        text = body.lstrip()[end:].strip()
        if wrapped:
            if text == ']':
                return [data for _, data in pages], pages
            if not text.startswith(','):
                raise ValueError('Invalid GitHub page separator')
            text = text[1:].lstrip()
            if not text.startswith('HTTP/'):
                raise ValueError('Missing GitHub page headers')
        elif not text:
            return ([data for _, data in pages] if slurp or len(pages) > 1 else data), pages
        elif not paginate:
            raise ValueError('Unexpected trailing GitHub response')
    if pages or wrapped:
        raise ValueError('Incomplete GitHub paginated response')
    # Headerless CLI/fake-adapter output retains its original JSON shape.
    data = json.loads(text) if text else None
    return data, [({}, data)]


def quota_message(message):
    # Recognize complete quota messages, not a quota phrase inside an unknown
    # failure. Unrecognized variants halt rather than borrowing retry authority.
    return isinstance(message, str) and bool(re.fullmatch(
        r'(?:API rate limit (?:already )?exceeded(?: for (?:user ID )?[\w.:-]+)?\.?|'
        r'You have exceeded a secondary rate limit\.'
        r'(?: Please wait a few minutes before you try again\.)?)', message, re.I))


def quota_stderr(stderr):
    message = re.sub(r' \(HTTP (?:403|429)\)$', '', stderr.strip())
    if message.startswith('gh: '):
        message = message[4:]
    if message.startswith('GraphQL: '):
        message = message[9:]
    return quota_message(message)


def limited(data, stderr, failed):
    if not isinstance(data, dict) or data.get('data') is not None:
        return False
    errors = data.get('errors', [])
    if not isinstance(errors, list):
        return False
    if errors:
        if 'message' in data or not all(
                isinstance(e, dict) and e.get('type') == 'RATE_LIMITED' and
                ('message' not in e or quota_message(e['message'])) for e in errors):
            return False
        messages = [e.get('message') for e in errors]
    elif failed and quota_message(data.get('message')):
        messages = [data['message']]
    else:
        return False
    # CLI stderr may echo the same quota failure, never an additional failure.
    return not stderr.strip() or any(
        stderr.strip() == prefix + message + suffix
        for message in messages if isinstance(message, str)
        for prefix in ('', 'gh: ', 'GraphQL: ', 'gh: GraphQL: ')
        for suffix in ('', ' (HTTP 403)', ' (HTTP 429)'))


def delay(headers, current, attempt):
    waits = []
    retry = headers.get('retry-after')
    if retry:
        try:
            waits.append(float(retry))
        except ValueError:
            try:
                stamp = parsedate_to_datetime(retry)
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=timezone.utc)
                waits.append(stamp.timestamp() - current)
            except (ValueError, TypeError, OverflowError):
                raise Halt('Invalid GitHub Retry-After; no automatic retry')
    reset = headers.get('x-ratelimit-reset')
    if headers.get('x-ratelimit-remaining') == '0' and reset:
        try:
            waits.append(float(reset) - current + 1)
        except ValueError:
            raise Halt('Invalid GitHub reset timestamp; no automatic retry')
    seconds = max(waits) if waits else 60 * (2 ** attempt)
    if seconds <= 0:
        seconds = 60 * (2 ** attempt)  # Expired reset/cached response: no tight loop.
    if not 0 < seconds <= 86400:
        raise Halt('GitHub rate-limit delay exceeds bounded policy')
    return seconds


class ReadWait:
    def __init__(self, guard, report=lambda _: None, *, clock=time.time, wait=time.sleep):
        self.guard, self.report, self.clock, self.wait = guard, report, clock, wait
        self.lock = threading.Lock()
        self.until = 0
        self.latest = None

    def _guard_wait(self):
        try:
            observer = _observer_guard.get()
            if observer:
                observer()
            self.guard()
            if observer:
                observer()  # Configuration/authority checks may themselves be slow.
        except Stopped as exc:
            raise RateWaitStopped(str(exc)) from exc

    def call(self, operation, probe):
        deadline = self.clock() + 3600
        for attempt in range(4):
            # Callbacks may need a state mutex already held by another caller.
            # Serialize reads/cooldowns, never callbacks, under the read lock.
            waited = False
            while not self.lock.acquire(timeout=.1):
                waited = True
                self._guard_wait()
            try:
                while self.clock() < self.until:
                    waited = True
                    self._guard_wait()
                    if self.clock() >= deadline:
                        raise Halt('GitHub read wait exhausted its one-hour bound')
                    self.wait(min(1, self.until - self.clock(), deadline - self.clock()))
                if attempt or waited:
                    # Include the exact cooldown-expiry boundary and peers that
                    # waited on the shared gate, while preserving first cleanup IO.
                    self._guard_wait()
                    if self.clock() >= deadline:
                        raise Halt('GitHub read wait exhausted its one-hour bound')
                try:
                    value = operation()
                except ReadConnectionReset:
                    self._guard_wait()
                    if attempt == 3:
                        raise
                    self.until = self.clock() + 2 ** attempt
                    notice = {'state': 'waiting', 'reason': 'connection-reset',
                              'retry_at': self.until, 'attempt': attempt + 1}
                except RateLimited as exc:
                    self._guard_wait()  # The failed read may have outlived its active child.
                    if attempt == 3:
                        raise Halt('Repeated GitHub rate limits exhausted bounded read retries') from exc
                    headers = exc.headers or probe()
                    self.until = self.clock() + delay(headers, self.clock(), attempt)
                    notice = {'state': 'waiting', 'retry_at': self.until, 'attempt': attempt + 1}
                else:
                    notice = {'state': 'ready'}
                self.latest = notice
            finally:
                self.lock.release()
            self.report(notice)
            if notice['state'] == 'ready':
                return value


@contextmanager
def factory_reads(factory, watcher=None):
    """Only an authorized supervisor opts into waiting; scope remains pinned."""
    from .common import _io_allowance, read_json
    gh = factory.gh
    previous = getattr(gh, 'read_wait', None)
    startup_end = time.monotonic() + min(3600, watcher.settings['max_seconds']) if watcher else None
    # Reload under the common lock before any wait-status state publication.
    if factory.state_path.exists():
        factory.state = read_json(factory.state_path)
    def guard():
        allowance = _io_allowance.get()
        if allowance:
            # The callback owns the original grace cutoff, scope and stop checks,
            # but uses cached config. Validate saved config separately without
            # applying the expired work cutoff or replacing the allowance.
            if allowance() <= 0:
                raise Stopped('Completion IO allowance exhausted during rate-limit wait')
            factory.ensure_config_unchanged()
            # Config validation runs git; authority can expire while it executes.
            if allowance() <= 0:
                raise Stopped('Completion IO allowance exhausted during rate-limit wait')
        else:
            factory.ensure_running()
        if watcher and watcher.session is None and time.monotonic() >= startup_end:
            raise Stopped('Watcher startup read-wait allowance exhausted')
    def report(value):
        with factory.mutex:
            if gh.read_wait.latest is not value:
                return False  # A newer read/cooldown superseded this callback.
            previous_value = factory.state.get('rate_limit_wait')
            if value == previous_value or (value['state'] == 'ready' and not previous_value):
                return True
            factory.state['rate_limit_wait'] = value
            factory.save()
            return True
    gh.read_wait = ReadWait(guard, report)
    try:
        yield
    finally:
        gh.read_wait = previous
