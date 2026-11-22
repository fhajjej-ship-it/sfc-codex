"""Prepare invocation-scoped Codex settings for SFC workers."""
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
    if 'worker_routing' not in config:
        return
    routing = config['worker_routing']
    if not isinstance(routing, dict) or set(routing) != {'routine_build_issues'}:
        raise Halt('worker_routing must contain only routine_build_issues')
    issues = routing['routine_build_issues']
    if (not isinstance(issues, list) or any(type(issue) is not int or issue <= 0 for issue in issues)
            or len(issues) != len(set(issues))):
        raise Halt('worker_routing.routine_build_issues must list distinct positive issue numbers')
    if (config.get('model') != 'gpt-6.2-sol' or config.get('models', {}) != {}
            or config.get('reasoning_effort') != 'high' or config.get('service_tier') != 'default'):
        raise Halt('worker_routing requires gpt-6.2-sol, high reasoning, default service tier and no model overrides')


def lane_settings(config, lane, issue, *, waiting=False):
    result = {'model': config.get('models', {}).get(lane) or config.get('model'),
              'reasoning_effort': config.get('reasoning_effort'),
              'service_tier': config.get('service_tier')}
    routing = config.get('worker_routing')
    if routing is not None and lane == 'build':
        if issue in routing['routine_build_issues']:
            result['reasoning_effort'] = 'medium'
        if waiting:
            result['service_tier'] = 'fast'
    return result


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
