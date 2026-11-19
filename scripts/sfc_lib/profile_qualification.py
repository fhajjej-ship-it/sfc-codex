"""Local qualification inputs for the supported efficiency setup profile."""
from __future__ import annotations

import copy
import os
from pathlib import Path
import shutil
from types import SimpleNamespace

from . import review_inspection, review_policy
from .common import Halt, PACKAGE, git, read_json, run, validate_config
from .verification import checksum, digest

MODEL_FIELDS = ('model', 'reasoning_effort', 'service_tier')
RUNTIME_DIRECTORIES = ('schemas', 'scripts/sfc_lib', 'skills')
OFFLINE_MODULES = {'efficiency_benchmark.py', 'fixture_trust.py', 'review_benchmark.py', 'onboarding.py',
                   'profile_qualification.py'}
EVIDENCE_ERRORS = (AttributeError, OSError, KeyError, TypeError, ValueError, Halt)


def model_environment(runtime):
    """Fingerprint the model adapter and dispatch implementation, without login reads."""
    runtime = Path(runtime)
    tool = shutil.which('codex')
    if not tool:
        raise Halt('Codex CLI is unavailable')
    version = run([tool, '--version'], timeout=15).stdout.strip()
    if not version or 'fake' in version.lower():
        raise Halt('Codex CLI identity is unavailable')
    home = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex').resolve()
    user_config = home / 'config.toml'
    files = [p for directory in RUNTIME_DIRECTORIES for p in (runtime / directory).rglob('*')
             if p.is_file() and '__pycache__' not in p.parts and
             not (directory == 'scripts/sfc_lib' and p.name in OFFLINE_MODULES)]
    required = ('scripts/sfc_lib/factory.py', 'schemas/lane-result.json', 'skills/sfc-build/SKILL.md')
    if not all((runtime / p).is_file() for p in required):
        raise Halt('Worker runtime source is incomplete')
    return {'cli_version': version, 'cli_sha256': checksum(tool),
            'user_config_sha256': checksum(user_config) if user_config.exists() else None,
            'user_context_sha256': digest(str(home)),
            'runtime_sha256': digest({p.relative_to(runtime).as_posix(): checksum(p) for p in sorted(files)})}


def model_route_binding(config, root):
    """Bind supported route controls across equivalent temporary checkouts."""
    root = Path(root).resolve()
    identity = {'repo', 'project', 'base_branch', 'status_field', 'activity_timezone',
                'watch', 'priority', 'work_types', *MODEL_FIELDS}
    route = copy.deepcopy({k: v for k, v in config.items() if k not in identity})
    for check in route.get('verification_reuse', []):
        if runtime := check.get('runtime'):
            location = Path(runtime['location'])
            if location.is_relative_to(root):
                runtime['location'] = location.relative_to(root).as_posix()
    if release := route.get('release'):
        # Candidate note contents and authorization identifiers differ across
        # fixture checkouts. Authority mode and release obligations remain bound.
        if authority := release.get('authority'):
            release['authority'] = {k: v for k, v in authority.items()
                                    if k not in ('authorization_id', 'repo', 'base_branch')}
        release.pop('approved_summaries', None)
    codex_config = root / '.codex/config.toml'
    if codex_config.exists():
        route['local_codex_config_sha256'] = checksum(codex_config)
    return digest(route)


def decision(status, reason, **evidence):
    return {'status': status, 'reason': reason, **evidence}


def manifest(path, keys):
    path = Path(path).resolve()
    sha256 = checksum(path)
    value = read_json(path)
    if (not isinstance(value, dict) or set(value) != {'version', *keys} or
            type(value['version']) is not int or value['version'] != 1 or checksum(path) != sha256):
        raise ValueError('qualification manifest changed or invalid')
    return path, sha256, value


def resolve_ref(parent, ref):
    review_policy.validate_ref(ref)
    path = (parent / ref['path']).resolve()
    if checksum(path) != ref['sha256']:
        raise ValueError('qualification evidence changed')
    return {'path': str(path), 'sha256': ref['sha256']}


def model_refs(path, value):
    return tuple(resolve_ref(path.parent, value[key]) for key in ('input', 'adjudication'))


def matches_model_route(profile, config, root):
    """Verify every measured phase against the target's version of that phase."""
    target = model_route_binding(config, root)
    source_only = model_route_binding({k: v for k, v in config.items() if k != 'release'}, root)
    release_enabled = bool((config.get('release') or {}).get('enabled'))
    source_routes = set()
    for case, pair in profile['pairs'].items():
        for side in ('baseline', 'candidate'):
            scenario = pair[side]['scenario']
            source = scenario.get('source_model_route_sha256') or scenario.get('model_route_sha256')
            if source not in (target, source_only) or scenario.get('model_route_sha256') != source:
                return False
            source_routes.add(source)
            release = scenario.get('release_model_route_sha256')
            if case == 'security-sensitive':
                if release is not None:
                    return False
            elif release_enabled and release != target:
                return False
    return len(source_routes) == 1


def matches_completion_boundary(report, config):
    from .efficiency_benchmark import DRAFT_BOUNDARY, PUBLISHED_BOUNDARY
    authority = (config.get('release') or {}).get('authority') or {}
    expected = DRAFT_BOUNDARY if authority.get('mode') == 'draft' else PUBLISHED_BOUNDARY
    return report.get('completion_boundary', PUBLISHED_BOUNDARY) == expected


def model_setup_available(ref, selected, root, config):
    """Check retained references and local provider inputs without model calls."""
    try:
        path, sha256, value = manifest(ref['path'], {'input', 'adjudication', 'target_route'})
        source, assessment = model_refs(path, value)
        return (sha256 == ref['sha256'] == selected['manifest_sha256'] and
                source['sha256'] == selected['input_sha256'] and
                assessment['sha256'] == selected['adjudication_sha256'] and
                value['target_route'] == model_route_binding(config, root) and
                digest(model_environment(PACKAGE)) == selected['environment_sha256'])
    except EVIDENCE_ERRORS:
        return False


def review_selection(root, config, path):
    if 'review_policy' in config:
        return None, decision('retained', 'Existing review policy is an explicit choice')
    if path is None:
        from .review_cohort import propose
        policy, reason, evidence = propose(root, config)
        return policy, decision('selected' if policy else 'retained', reason, **evidence)
    try:
        path, sha256, value = manifest(path, {'review_policy'})
        policy = value['review_policy']
        if not isinstance(policy, dict) or set(policy) & {'routine_candidate', 'imports', 'github_reviewer_ids', 'cohort'}:
            raise ValueError('qualification cannot confer candidate or import authority')
        if 'promotion' in policy:
            policy['promotion'] = resolve_ref(path.parent, policy['promotion'])
        inspection = policy.get('inspection', {})
        if 'promotion' in inspection:
            inspection['promotion'] = resolve_ref(path.parent, inspection['promotion'])
        proposed = validate_config({**config, 'review_policy': policy})
        active, reason = review_qualified(root, proposed)
        if not active:
            return None, decision('retained', reason, manifest_sha256=sha256)
        return policy, decision('selected', reason, manifest_sha256=sha256,
                                evidence_sha256=[ref['sha256'] for ref in
                                                 (policy.get('promotion'), inspection.get('promotion')) if ref])
    except EVIDENCE_ERRORS:
        return None, decision('retained', 'Review qualification is unavailable, changed or incomplete')


def review_setup_available(ref, selected, root, config):
    """Revalidate the saved qualification and exact policy without dispatch."""
    configured = config.get('review_policy') or {}
    if cohort := configured.get('cohort'):
        if ref != {'kind': 'bundled-cohort', 'sha256': cohort}:
            return False, 'Bundled review qualification reference changed'
        baseline = {key: value for key, value in config.items() if key != 'review_policy'}
        policy, check = review_selection(root, baseline, None)
        if policy != configured or check != selected:
            return False, check['reason'] if policy is None else 'Bundled review qualification or policy changed'
        return True, check['reason']
    if not ref or ref.get('sha256') != selected.get('manifest_sha256'):
        return False, 'Review qualification manifest is unavailable or changed'
    baseline = {key: value for key, value in config.items() if key != 'review_policy'}
    policy, check = review_selection(root, baseline, ref.get('path'))
    if (policy is None or policy != config.get('review_policy') or
            check.get('manifest_sha256') != ref['sha256'] or
            check.get('evidence_sha256') != selected.get('evidence_sha256')):
        return False, check['reason'] if policy is None else 'Review policy or qualification evidence changed'
    return True, check['reason']


def review_qualified(root, config):
    policy = config['review_policy']
    factory = SimpleNamespace(root=root, c=config)
    consolidation = policy.get('mode') == 'risk-based'
    profiles = policy.get('inspection', {}).get('enabled', [])
    if not consolidation and not profiles:
        return False, 'No qualified review optimization requested'
    if consolidation:
        active, reason = review_policy.promoted(factory)
        if not active:
            return False, reason
        if 'promotion' in policy:
            report = review_policy.load_ref(root, policy['promotion'])
            if report.get('runtime_sha256') != review_policy.runtime_binding():
                return False, 'Review qualification uses a different worker runtime'
        if contract := policy.get('behavioral_contract'):
            tracked = set(git(root, 'ls-tree', '-r', '--name-only', '-z', 'HEAD').split('\0'))
            instructions = {name for name in tracked if Path(name).name in ('AGENTS.md', 'AGENTS.override.md')}
            if (not set(contract['paths']) <= tracked or instructions != set(contract['instructions']) or
                    any(checksum(Path(root) / name) != sha256 for name, sha256 in contract['instructions'].items())):
                return False, 'Behavioral paths or repository instructions differ from the qualified source scope'
    if policy.get('providers', {}).get('review') == 'codex-native':
        review_policy.native_command(factory, root, Path(root) / '.sfc/qualification-probe.json')
    if profiles and not all(review_inspection.qualified(factory, profile) for profile in profiles):
        return False, 'Requested inspection profiles lack matching qualification or a spare slot'
    return True, 'Pinned review qualification covers the requested route and completed assessment'


def model_selection(root, config, path, explicit_model_settings):
    if path is None:
        return None, decision('retained', 'No model qualification requested')
    if config.get('models') or not all(config.get(k) for k in MODEL_FIELDS):
        return None, decision('retained', 'Existing lane overrides or inherited model settings are preserved')
    try:
        from .efficiency_benchmark import evaluate

        path, sha256, value = manifest(path, {'input', 'adjudication', 'target_route'})
        source, assessment = model_refs(path, value)
        report = evaluate(source['path'], adjudication=assessment['path'])
        if (report['input'] != source or checksum(assessment['path']) != assessment['sha256'] or
                report['comparison_kind'] != 'model' or len(report['profiles']) != 1):
            raise ValueError('model comparison does not match the requested evidence')
        profile = next(iter(report['profiles'].values()))
        retained = {'manifest_sha256': sha256, 'input_sha256': source['sha256'],
                    'adjudication_sha256': assessment['sha256']}
        if not report.get('plan') or not report.get('model_objective'):
            return None, decision('retained', 'Model comparison lacks a pinned predeclared objective', **retained)
        if profile['qualified'] is not True:
            return None, decision('retained', 'Whole-task model qualification is inconclusive or failed', **retained)
        baseline = {k: config[k] for k in MODEL_FIELDS}
        settings = profile['recommended_model_settings']
        if (not isinstance(settings, dict) or set(settings) != set(MODEL_FIELDS) or
                not all(pair['baseline']['scenario']['settings'] == baseline for pair in profile['pairs'].values())):
            return None, decision('retained', 'Measured baseline differs from the configured model settings', **retained)
        changed = {k for k in MODEL_FIELDS if baseline[k] != settings[k]}
        if changed & explicit_model_settings:
            return None, decision('retained', 'Explicit worker model, effort or tier choices are preserved', **retained)
        if not changed or not profile['pairs']:
            raise ValueError('missing measured alternative or matched cases')
        if (value['target_route'] != model_route_binding(config, root) or
                not matches_completion_boundary(report, config) or not matches_model_route(profile, config, root)):
            return None, decision('retained', 'Measured verification, release or review route differs', **retained)
        environment = model_environment(PACKAGE)
        if report.get('model_environment') != environment:
            return None, decision('retained', 'Codex provider configuration or worker runtime differs from the comparison',
                                  **retained)
        proposed = validate_config({**config, **settings})
        policy = proposed.get('review_policy') or {}
        if policy.get('promotion') or policy.get('cohort') or policy.get('inspection', {}).get('enabled'):
            if not review_qualified(root, proposed)[0]:
                return None, decision('retained', 'Existing review qualification does not cover the alternative settings',
                                      **retained)
        return settings, decision('selected', 'Matched whole-task comparison retained quality and met its objective',
                                  baseline=baseline, selected=settings, environment_sha256=digest(environment), **retained)
    except EVIDENCE_ERRORS:
        return None, decision('retained', 'Model qualification or Codex provider evidence is unavailable, changed or incomplete')
