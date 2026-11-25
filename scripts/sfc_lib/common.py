from __future__ import annotations

import contextlib
from contextvars import ContextVar
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
import threading
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

PACKAGE = Path(__file__).resolve().parents[2]
STATUSES = ("Backlog", "Ready", "Building", "QA", "Review", "Blocked", "Done")
DEFAULT_MODEL = "gpt-6.1-sol"
DEFAULT_REASONING_EFFORT = "high"
DEFAULT_SERVICE_TIER = "fast"
REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")


class Halt(RuntimeError):
    """Stop without inventing a successful result or retrying a write."""


class Busy(Halt):
    """A live common-git lock owner; no admission has taken place."""


class Stopped(Halt):
    """A requested interruption, distinct from a failed run."""


class ObserverInactive(Stopped):
    """The observed child exited normally during activity IO."""


class TimedOut(Halt):
    """An owned process exceeded its finite deadline."""


class CommandCancelled(Halt):
    """Only this invocation is no longer needed; its reservation remains spent."""


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def read_json(path):
    return json.loads(Path(path).read_text())


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Supervisor state and receipts may contain private task and worker evidence.
    # NamedTemporaryFile creates the sibling with owner-only permissions.
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                     prefix=path.name + '.', suffix='.tmp', delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(json.dumps(value, indent=2) + '\n')
            stream.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


_io_allowance = ContextVar('sfc_io_allowance', default=None)
_command_observer = ContextVar('sfc_command_observer', default=None)
_process_observer = ContextVar('sfc_process_observer', default=None)
_command_cancellation = ContextVar('sfc_command_cancellation', default=None)
# Only active child observations inherit this guard. Cleanup/completion IO must
# retain its own authority after the child exits or its work deadline expires.
_observer_guard = ContextVar('sfc_observer_guard', default=None)
# Post-mutation readback retains the original cutoff/stop, without requiring
# a still-running child. This never grants another write or a longer deadline.
_observer_readback_guard = ContextVar('sfc_observer_readback_guard', default=None)
# Set only by the bundled STDIO entry point, before it starts any threads. It
# applies to supervisor git/gh calls as well as worker/check process groups.
SESSION_LIFELINE = False
_owned_io = ContextVar("sfc_owned_io", default=None)


@contextlib.contextmanager
def command_cancellation(callback):
    """Cancel a context-owned invocation without stopping concurrent tickets."""
    token = _command_cancellation.set(callback)
    try:
        yield
    finally:
        _command_cancellation.reset(token)


@contextlib.contextmanager
def bounded_io(remaining):
    """Bound only this thread's reconciliation IO, never worker execution."""
    token = _io_allowance.set(remaining)
    try:
        yield
    finally:
        _io_allowance.reset(token)


@contextlib.contextmanager
def owned_io(cancel, *, lifetime=False):
    token = _owned_io.set((cancel, lifetime))
    try:
        yield
    finally:
        _owned_io.reset(token)


def execute_owned_io(argv, cwd, timeout, input, env, owner):
    from .lifeline import spawn, terminate
    cancel, lifetime = owner
    if cancel():
        raise Stopped('Delivery IO stopped before launch')
    process, writer = spawn(argv, enabled=lifetime or SESSION_LIFELINE, cwd=cwd, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
                            env={**(os.environ if env is None else env), 'SFC_WORKER': '1'})
    deadline, first = time.monotonic() + timeout, True
    try:
        while True:
            if cancel():
                raise Stopped('Delivery IO interrupted; inspect any retained write intent')
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(argv, timeout)
            try:
                stdout, stderr = process.communicate(input=input if first else None, timeout=min(.1, remaining))
                return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
            except subprocess.TimeoutExpired:
                first = False
    finally:
        if writer is not None:
            os.close(writer)
        terminate(process)


def run(argv, cwd=None, *, timeout=120, input=None, check=True, env=None):
    observer = _command_observer.get()
    context = observer(argv, cwd) if observer else contextlib.nullcontext({})
    with context as observed:
        result = _run(argv, cwd, timeout=timeout, input=input, check=False, env=env)
        observed['exit_code'] = result.returncode
        if check and result.returncode:
            raise Halt(f"{argv[0]} failed ({result.returncode}): {result.stderr[-2000:]}")
        return result


def _run(argv, cwd=None, *, timeout=120, input=None, check=True, env=None):
    allowance = _io_allowance.get()
    if allowance:
        timeout = min(timeout, allowance())
    try:
        owner = _owned_io.get()
        if owner is not None:
            result = execute_owned_io(argv, cwd, timeout, input, env, owner)
        elif SESSION_LIFELINE:
            from .lifeline import spawn, terminate
            process, writer = spawn(argv, enabled=True, cwd=cwd, stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, start_new_session=True,
                                    env={**(os.environ if env is None else env), 'SFC_WORKER': '1'})
            try:
                stdout, stderr = process.communicate(input=input, timeout=timeout)
                result = subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
            finally:
                os.close(writer)
                terminate(process)
        else:
            result = subprocess.run(argv, cwd=cwd, input=input, text=True,
                                    capture_output=True, timeout=timeout, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Halt(f"Command could not finish: {argv[0]}: {exc}") from exc
    if check and result.returncode:
        # Do not dump command arguments; project arguments may contain credentials.
        raise Halt(f"{argv[0]} failed ({result.returncode}): {result.stderr[-2000:]}")
    return result


def git(root, *args, check=True):
    return run(["git", *map(str, args)], root, check=check).stdout.strip()


def repo_root(path):
    return Path(git(Path(path).resolve(), "rev-parse", "--show-toplevel")).resolve()


def positive(value, name, maximum=100000):
    if type(value) is not int or not 1 <= value <= maximum:
        raise Halt(f"{name} must be between 1 and {maximum}")


def activity_zone(name="UTC"):
    """Resolve a configured IANA zone; UTC works without a system tz database."""
    if not isinstance(name, str) or not name or name != name.strip():
        raise Halt("activity_timezone must be a nonempty IANA timezone name")
    if name == "UTC":
        return dt.timezone.utc
    try:
        return ZoneInfo(name)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise Halt("activity_timezone must be an available IANA timezone name; install system timezone data or use UTC") from exc


def validate_config(c):
    if not isinstance(c, dict) or c.get("version") != 1:
        raise Halt("Unsupported SFC config version")
    if c.get("workflow_mode", "factory") not in ("chat", "factory"):
        raise Halt("workflow_mode must be chat or factory")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", c.get("repo", "")):
        raise Halt("repo must be owner/name")
    if not isinstance(c.get("base_branch"), str) or not c["base_branch"] or c["base_branch"].startswith("-"):
        raise Halt("base_branch is required")
    run(["git", "check-ref-format", "--branch", c["base_branch"]])
    project = c.get("project", {})
    if not isinstance(project, dict) or not re.fullmatch(r"[A-Za-z0-9-]+", project.get("owner", "")):
        raise Halt("project.owner is required")
    positive(project.get("number"), "project.number")
    commands = c.get("verify_commands")
    if not isinstance(commands, list) or (not commands and c.get('workflow_mode') != 'chat') or any(
            not isinstance(v, str) or not v.strip() or "\0" in v for v in commands):
        raise Halt("verify_commands must contain nonempty project-owned commands")
    from .verification import validate as validate_verification
    validate_verification(c)
    from .review_policy import validate as validate_review
    validate_review(c)
    from .worker_runtime import validate as validate_worker_runtime
    validate_worker_runtime(c)
    for key, maximum in (("max_workers", 16), ("max_cycles", 1000),
                         ("max_no_progress", 100), ("max_attempts", 20),
                         ("worker_timeout", 86400), ("verify_timeout", 86400)):
        positive(c.get(key), key, maximum)
    if c.get("merge_policy") not in ("human", "auto"):
        raise Halt("merge_policy must be human or auto")
    if c.get("merge_mode", "github-protected") not in ("github-protected", "local-verification"):
        raise Halt("merge_mode must be github-protected or local-verification")
    if type(c.get("allow_no_checks")) is not bool:
        raise Halt("allow_no_checks must be a boolean")
    positive(c.get("auto_max_lines"), "auto_max_lines")
    if not isinstance(c.get("status_field"), str) or not c["status_field"].strip():
        raise Halt("status_field is required")
    activity_zone(c.get("activity_timezone", "UTC"))
    if "watch" in c:
        from .watch import settings
        if not isinstance(c["watch"], dict):
            raise Halt("watch must be an object of finite session defaults")
        settings(c["watch"])
    from .releases import validate as validate_release
    validate_release(c)
    from .work_types import validate_mapping
    validate_mapping(c.get("work_types"))
    models = c.get("models", {})
    if not isinstance(models, dict) or any(k not in ("build", "qa", "security", "review") or
            not isinstance(v, str) or not v.strip() for k, v in models.items()):
        raise Halt("models maps lane names to nonempty Codex model identifiers")
    for key in ("model", "service_tier"):
        value = c.get(key)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise Halt(f"{key} must be a nonempty string or null to inherit Codex")
    effort = c.get("reasoning_effort")
    if effort is not None and effort not in REASONING_EFFORTS:
        raise Halt("reasoning_effort must be a supported effort name or null to inherit Codex")
    return c


def load_config(root):
    return validate_config(read_json(Path(root) / ".sfc/config.json"))


def common_dir(root):
    common = Path(git(root, "rev-parse", "--git-common-dir"))
    if not common.is_absolute():
        common = Path(root) / common
    return common.resolve()


@contextlib.contextmanager
def exclusive(root):
    with (common_dir(root) / "sfc.lock").open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Busy("Another SFC supervisor is running for this repository") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


class Processes:
    """Own child process groups, bounded waits and cooperative stop."""

    def __init__(self, stop_path):
        self.stop_path = Path(stop_path)
        self.stop_check = None
        self.shared_stop_path = None
        self.cancel_check = None
        self.lifetime = False
        self.children = set()
        self.lock = threading.Lock()

    def stopped(self):
        return self.stop_requested() or bool(self.stop_check and self.stop_check())

    def stop_requested(self):
        return (self.stop_path.exists() or bool(self.shared_stop_path and self.shared_stop_path.exists()) or
                bool(self.cancel_check and self.cancel_check()))

    def execute(self, argv, cwd, log, timeout, prompt=None, *, observer=None, worker_events=False):
        # Capture before creating the watcher thread; ContextVars intentionally
        # do not leak into other invocations sharing this Processes instance.
        cancellation = _command_cancellation.get()
        def cancelled():
            return bool(cancellation and cancellation())
        if self.stopped():
            raise Stopped("Stopped by request; worktree and receipts preserved")
        if cancelled():
            raise CommandCancelled('Command cancelled before launch; reservation preserved')
        log = Path(log)
        log.parent.mkdir(parents=True, exist_ok=True)
        worker_prompt = worker_events and isinstance(prompt, str)
        if worker_prompt:
            reserve = min(30, timeout / 5)
            prompt = f"""Supervisor execution budget
This invocation has at most {timeout:g} seconds of wall time for startup, tools,
and the final report. A stop or supervisor deadline may end it earlier. Aim to
reserve the final {reserve:g} seconds for the complete response in the required format.
Prioritize the required obligations and use adequate evidence already gathered.
Once evidence resolves an obligation, expand only for a concrete uncovered
criterion, a distinct material risk, or an evidence discrepancy. Stop repeating
equivalent checks and return the report as soon as the required scope is resolved.
If time runs short, report confirmed findings and unresolved gaps in the required
format. Do not pass incomplete or unsupported obligations.

{prompt}"""
        with log.open("w") as output:
            if worker_prompt:
                dispatched = log.with_name(log.name + '.prompt.md')
                try:
                    fd = os.open(dispatched, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                        stream.write(prompt)
                except (OSError, UnicodeError) as exc:
                    with contextlib.suppress(OSError):
                        output.write(f'SFC prompt evidence unavailable ({type(exc).__name__})\n')
                        output.flush()
            try:
                from .lifeline import spawn
                process, lifeline = spawn(argv, enabled=self.lifetime or SESSION_LIFELINE,
                                           receipt=str(log) + '.lifeline.jsonl', cwd=cwd, stdin=subprocess.PIPE,
                                           stdout=output, stderr=subprocess.STDOUT,
                                           text=True, encoding='utf-8' if worker_prompt else None,
                                           start_new_session=True,
                                           env={**os.environ, "PWD": str(Path(cwd).resolve()), "SFC_WORKER": "1",
                                                **({"PYTHONDONTWRITEBYTECODE": "1"} if worker_events else {})})
            except OSError as exc:
                raise Halt(f"Cannot start {argv[0]}: {exc}") from exc
            with self.lock:
                self.children.add(process)
            deadline = time.monotonic() + timeout
            finished = threading.Event()
            termination_lock = threading.Lock()
            interrupted = []

            def signal_owned(sig):
                # Popen's POSIX wait lock serializes with communicate/wait/poll.
                # Until reaped the child PID cannot be reused; once reaped its
                # returncode is set and we never signal that numeric ID again.
                with process._waitpid_lock:
                    if process.returncode is not None:
                        return False
                    try:
                        os.killpg(process.pid, sig)
                        return True
                    except ProcessLookupError:
                        return False

            def terminate():
                with termination_lock:
                    if signal_owned(signal.SIGTERM):
                        try:
                            process.wait(timeout=3)
                        except subprocess.TimeoutExpired:
                            signal_owned(signal.SIGKILL)
                            process.wait()

            def watch():
                # Board IO can be slow. Stop/timeout enforcement must remain
                # independent of an observer awaiting a GitHub response.
                while not finished.wait(0.1) and process.poll() is None:
                    if self.stopped():
                        interrupted.append(Stopped("Stopped by request; inspect the retained log"))
                    elif cancelled():
                        interrupted.append(CommandCancelled('Command cancelled; inspect the retained log'))
                    elif time.monotonic() >= deadline:
                        interrupted.append(TimedOut("Worker/verification timed out; inspect its log"))
                    else:
                        continue
                    terminate()
                    return

            def check_observer(require_active=True):
                if interrupted:
                    raise interrupted[0]
                if self.stopped():
                    raise Stopped("Stopped by request; inspect the retained log")
                if cancelled():
                    raise CommandCancelled('Command cancelled; inspect the retained log')
                if time.monotonic() >= deadline:
                    raise TimedOut("Worker/verification timed out; inspect its log")
                if require_active and process.poll() is not None:
                    raise ObserverInactive("Child is no longer active")

            def observe_active(event):
                token = _observer_guard.set(check_observer)
                readback = _observer_readback_guard.set(lambda: check_observer(False))
                try:
                    check_observer()
                    observer(event)
                except Stopped as exc:
                    if not isinstance(exc, ObserverInactive) and not isinstance(exc.__cause__, ObserverInactive):
                        raise
                    # A cancelled preflight has no write to reconcile. Continue
                    # to the ordinary inactive report outside the active scope.
                finally:
                    _observer_readback_guard.reset(readback)
                    _observer_guard.reset(token)

            watcher = threading.Thread(target=watch, daemon=True)
            watcher.start()
            try:
                # Accounting observes a confirmed supervisor-owned launch even
                # if the activity callback cannot run before timeout/exit.
                on_launch = _process_observer.get()
                if on_launch:
                    on_launch()
                from .activity import EventReader
                events = EventReader(log) if worker_events else None
                if observer:
                    observe_active("started")
                # communicate's timeout loop keeps stdin handling safe for large prompts.
                first = True
                while True:
                    try:
                        process.communicate(input=prompt if first else None, timeout=0.3)
                        if interrupted:
                            raise interrupted[0]
                        if self.stopped():
                            raise Stopped("Stopped by request; inspect the retained log")
                        if cancelled():
                            raise CommandCancelled('Command cancelled; inspect the retained log')
                        if observer:
                            observer("inactive")
                        return process.returncode
                    except subprocess.TimeoutExpired:
                        first = False
                        if self.stopped():
                            raise Stopped("Stopped by request; inspect the retained log")
                        if cancelled():
                            raise CommandCancelled('Command cancelled; inspect the retained log')
                        if time.monotonic() >= deadline:
                            raise TimedOut("Worker/verification timed out; inspect its log")
                        if observer:
                            observe_active((events.poll() if events else None) or "tick")
            finally:
                finished.set()
                if lifeline is not None:
                    os.close(lifeline)
                terminate()
                watcher.join()
                with self.lock:
                    self.children.discard(process)
