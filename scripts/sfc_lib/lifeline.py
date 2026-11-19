"""POSIX ownership guardian. Only signals an unreaped child it created.

The runtime alone holds the pipe writer. EOF survives SIGKILL of the runtime;
neither the guardian nor its child inherits a writer. No saved PID is consulted.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import time


def supported():
    # This candidate has a macOS process/pipe acceptance fixture. Other platforms
    # need their own cleanup proof before enabling lifetime work.
    return (sys.platform == 'darwin' and hasattr(os, 'killpg') and
            hasattr(os, 'waitid') and hasattr(os, 'WNOWAIT'))


def signal_child(process, sig):
    with process._waitpid_lock:
        if process.returncode is not None:
            return False
        try:
            os.killpg(process.pid, sig)
            return True
        except ProcessLookupError:
            return False


def terminate(process):
    if signal_child(process, signal.SIGTERM):
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            signal_child(process, signal.SIGKILL)
            process.wait()


def spawn(argv, *, enabled=False, receipt=None, **kwargs):
    """Return an owned Popen and its private lifeline writer (or None)."""
    if not enabled:
        return subprocess.Popen(argv, **kwargs), None
    if not supported():
        raise OSError('Session lifetime cleanup is currently supported only on macOS')
    reader, writer = os.pipe()
    try:
        command = [sys.executable, str(Path(__file__).resolve()), str(reader),
                   str(receipt) if receipt else '', *argv]
        process = subprocess.Popen(command, pass_fds=(reader,), **kwargs)
        return process, writer
    except BaseException:
        os.close(writer)
        raise
    finally:
        os.close(reader)


def warden():
    """Keep the group leader alive until the guardian's final group signal.

    macOS can return EPERM when signalling a group whose only member is a
    zombie. A live warden makes cleanup independent of that edge case and pins
    the group identity even when the actual command finishes before descendants.
    """
    signal.signal(signal.SIGTERM, lambda *_: None)
    signal.signal(signal.SIGINT, lambda *_: None)
    channel = int(sys.argv[2])
    try:
        process = subprocess.Popen(sys.argv[3:], stdin=sys.stdin, stdout=sys.stdout,
                                   stderr=sys.stderr, close_fds=True)
        code = process.wait()
    except OSError:
        code = 125
    os.write(channel, (str(code) + '\n').encode())
    os.close(channel)
    while True:
        signal.pause()


def main():
    reader, receipt, argv = int(sys.argv[1]), sys.argv[2], sys.argv[3:]
    stopping = False

    def stop(*_):
        nonlocal stopping
        stopping = True

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, stop)

    def disconnected():
        return bool(select.select([reader], [], [], 0)[0])

    def record(state):
        if receipt:
            # Private diagnostic only; never used as signalling authority.
            with open(receipt, 'a') as output:
                output.write(json.dumps({'state': state, 'at': time.time()}) + '\n')

    if stopping or disconnected():
        record('parent-ended-before-launch')
        return 125
    status_reader, status_writer = os.pipe()
    process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--warden', str(status_writer), *argv],
                               stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr,
                               start_new_session=True, pass_fds=(status_writer,))
    os.close(status_writer)
    code = 125
    try:
        record('child-started')
        while True:
            if select.select([status_reader], [], [], 0)[0]:
                value = os.read(status_reader, 64)
                if value:
                    code = int(value)
                break
            if os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None:
                break
            if stopping or disconnected():
                record('parent-ended' if not stopping else 'stop')
                # Finish cleanup before the owning runtime's three-second reap
                # timeout. Hold the child unreaped until the final group signal.
                if signal_child(process, signal.SIGTERM):
                    end = time.monotonic() + 1
                    while time.monotonic() < end:
                        time.sleep(0.02)
                break
            time.sleep(0.02)
    finally:
        # Even normal command exit can leave same-group descendants. Don't poll
        # again after termination: wait/reap only after the last owned signal.
        if process.returncode is None:
            try:
                signal_child(process, signal.SIGKILL)
            except PermissionError:
                # Unexpected warden death with no signalable group is a failed
                # launch, never a successful child result.
                if os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is None:
                    raise
                code = 125
        process.wait()
        os.close(reader)
        os.close(status_reader)
        record('child-reaped')
    return code if code >= 0 else 128 - code


if __name__ == '__main__':
    if sys.argv[1] == '--warden':
        warden()
    else:
        sys.exit(main())
