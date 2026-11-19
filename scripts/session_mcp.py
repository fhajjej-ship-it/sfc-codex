#!/usr/bin/env python3
"""Local newline-delimited JSON-RPC MCP adapter. Stdout is protocol only."""
from __future__ import annotations

import json
import signal
import sys

from sfc_lib import __version__, common
from sfc_lib.lifeline import supported
from sfc_lib.session_runtime import SessionRuntime

MAX_MESSAGE = 1024 * 1024
FIELDS = {'turn_id': {'type': 'string'}, 'session_id': {'type': 'string'}, 'cwd': {'type': 'string'},
          'hook_event_name': {'enum': ['SessionStart', 'UserPromptSubmit', 'PreModelRequest']},
          'permission_mode': {'type': 'string'}, 'source': {'type': ['string', 'null']},
          'agent_id': {'type': ['string', 'null']}}
TOOLS = [
    {'name': 'session_attach',
     'description': 'Attach this native connection to stored project lifetime authority, including authorized worker usage and supervisor GitHub writes. No arbitrary commands or authorization changes. Trusted hooks return promptly.',
     'inputSchema': {'type': 'object', 'properties': FIELDS,
                     'required': ['session_id', 'cwd', 'hook_event_name'],
                     'additionalProperties': False},
     'annotations': {'readOnlyHint': False, 'destructiveHint': True, 'idempotentHint': True, 'openWorldHint': True}},
    {'name': 'session_attach_hook',
     'description': 'Native SessionStart/UserPromptSubmit attachment with event-specific hook JSON output. Uses the same stored authority, root proof and native thread checks as session_attach.',
     'inputSchema': {'type': 'object', 'properties': {
         **FIELDS, 'hook_event_name': {'enum': ['SessionStart', 'UserPromptSubmit']}},
                     'required': ['session_id', 'cwd', 'hook_event_name'],
                     'additionalProperties': False},
     'annotations': {'readOnlyHint': False, 'destructiveHint': True, 'idempotentHint': True, 'openWorldHint': True}},
    {'name': 'session_status', 'description': 'Read this connection status; initialization alone never starts observation or work.',
     'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False},
     'annotations': {'readOnlyHint': True, 'openWorldHint': False}}
]


def serve(runtime, source, sink):
    initialized = ready = False
    while not runtime.closed.is_set():
        line = source.readline(MAX_MESSAGE + 1)
        if not line:
            break
        request_id = None
        try:
            if len(line) > MAX_MESSAGE:
                raise ValueError('Message exceeds local protocol bound')
            message = json.loads(line)
            if not isinstance(message, dict) or message.get('jsonrpc') != '2.0':
                raise ValueError('Expected a JSON-RPC object')
            request_id = message.get('id')
            method, params = message.get('method'), message.get('params', {})
            if method == 'notifications/initialized' and initialized:
                ready = True
                continue
            if 'id' not in message:
                continue
            if method == 'initialize' and not initialized:
                version = params.get('protocolVersion')
                if version not in ('2024-11-05', '2025-03-26', '2025-06-18', '2025-11-25'):
                    raise ValueError('Unsupported MCP protocol; lifetime mode unavailable')
                initialized = True
                result = {'protocolVersion': version, 'capabilities': {'tools': {}},
                          'serverInfo': {'name': 'sfc-session', 'version': __version__}}
            elif method == 'ping':
                result = {}
            elif not ready:
                raise ValueError('Pending: MCP initialization is not complete')
            elif method == 'tools/list':
                result = {'tools': TOOLS}
            elif method == 'tools/call':
                name, arguments = params.get('name'), params.get('arguments', {})
                if not isinstance(arguments, dict):
                    raise ValueError('Expected structured arguments')
                if name in ('session_attach', 'session_attach_hook') and not set(arguments) - set(FIELDS):
                    if name == 'session_attach_hook' and arguments.get('hook_event_name') not in ('SessionStart', 'UserPromptSubmit'):
                        raise ValueError('This attachment tool requires a SessionStart/UserPromptSubmit hook')
                    metadata = params.get('_meta')
                    native_thread = metadata.get('threadId') if isinstance(metadata, dict) else None
                    value = runtime.attach(arguments, native_thread=native_thread)
                    if name == 'session_attach_hook':
                        # These two host events parse event-specific hook JSON,
                        # not the bare status object used by ordinary MCP calls.
                        # Only a fixed diagnostic crosses into model context;
                        # raw errors, paths and caller prose remain local.
                        state = value.get('state')
                        if state not in ('pending', 'observing', 'waiting', 'paused', 'ended',
                                         'ignored', 'disabled', 'unavailable'):
                            state = 'unavailable'
                        value = {'hookSpecificOutput': {
                            'hookEventName': arguments['hook_event_name'],
                            'additionalContext': 'SFC session attachment: ' + state +
                                '. Use session_status for current observation and work state.'}}
                elif name == 'session_status' and not arguments:
                    value = runtime.status()
                else:
                    raise ValueError('Unknown tool or unsupported arguments')
                result = {'content': [{'type': 'text', 'text': json.dumps(value)}]}
            else:
                sink.write(json.dumps({'jsonrpc': '2.0', 'id': request_id,
                                       'error': {'code': -32601, 'message': 'Unsupported method'}}) + '\n')
                sink.flush()
                continue
            response = {'jsonrpc': '2.0', 'id': request_id, 'result': result}
        except (ValueError, TypeError, AttributeError) as exc:
            response = {'jsonrpc': '2.0', 'id': request_id,
                        'error': {'code': -32602, 'message': str(exc)}}
        sink.write(json.dumps(response) + '\n')
        sink.flush()


def main():
    # Process-wide so supervisor IO in executor threads uses the same ownership
    # primitive. No command is launched merely by setting this flag.
    common.SESSION_LIFELINE = supported()
    runtime = SessionRuntime()

    def stop(*_):
        runtime.closed.set()
        # Interrupt a blocking stdin read. The finally block cancels work; abrupt
        # host removal is independently covered by inherited pipe guardians.
        raise SystemExit(0)

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, stop)
    try:
        serve(runtime, sys.stdin, sys.stdout)
    finally:
        runtime.close()


if __name__ == '__main__':
    main()
