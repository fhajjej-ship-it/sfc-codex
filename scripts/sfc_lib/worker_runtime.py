"""Prepare invocation-scoped Codex context settings for SFC workers."""
from __future__ import annotations

from .common import Halt


def settings(config):
    value = config.get('worker_runtime', {})
    if not isinstance(value, dict) or set(value) - {'skill_catalog', 'memories'}:
        raise Halt('worker_runtime must be an object containing only skill_catalog and memories')
    result = {'skill_catalog': True, 'memories': True}
    for key, enabled in value.items():
        if not isinstance(enabled, bool):
            raise Halt(f'worker_runtime.{key} must be a boolean')
        result[key] = enabled
    return result


def validate(config):
    settings(config)


def arguments(config):
    """True inherits the host setting. False narrows context for this invocation."""
    value = settings(config)
    args = []
    if not value['skill_catalog']:
        args += ['-c', 'skills.include_instructions=false']
    if not value['memories']:
        args += ['-c', 'memories.use_memories=false', '-c', 'memories.generate_memories=false']
    return args


def receipt(config):
    return {'version': 1, 'settings': settings(config), 'arguments': arguments(config)}
