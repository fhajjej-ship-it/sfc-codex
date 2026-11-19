"""Validate the bundled qualification for one narrowly scoped SFC review route."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import select
import shutil
import subprocess
import time

from . import efficiency_runtime, worker_runtime
from .common import Halt, PACKAGE, _io_allowance, git, run
from .verification import checksum, digest

CERTIFICATE = 'assets/qualification/sfc-python-review.json'
MODULE = 'scripts/sfc_lib/ticket_validation.py'
SCHEMA = {'version', 'evaluation', 'upstream_base', 'policy', 'profile_sha256', 'runtime_sha256',
          'cli', 'report_sha256', 'adjudication_sha256', 'full_activation_sha256', 'criteria_sha256', 'metrics'}
METRICS = {'retained_cases', 'seeded_cases', 'clean_cases', 'detection_recall', 'acceptance_coverage',
           'baseline_false_positives', 'candidate_false_positives', 'median_review_ratio',
           'median_end_to_end_ratio', 'max_clean_end_to_end_ratio'}
INSPECTION_PROOF = {'criteria_sha256', 'report_sha256', 'adjudication_sha256', 'controls_sha256', 'metrics'}
INSPECTION_METRICS = {'case_counts', 'minimum_clean_check_seconds', 'median_end_to_end_ratio',
                      'max_clean_end_to_end_ratio', 'detection_recall', 'acceptance_coverage',
                      'baseline_false_positives', 'candidate_false_positives'}
ERRORS = (AttributeError, KeyError, OSError, TypeError, ValueError, Halt)
ROUTE_ENV = {'OPENAI_BASE_URL', 'OPENAI_API_BASE', 'AZURE_OPENAI_ENDPOINT', 'CODEX_OSS_BASE_URL',
             'CODEX_APP_SERVER_CHATGPT_BASE_URL', 'CODEX_AUTHAPI_BASE_URL', 'CODEX_URL',
             'CODEX_API_KEY', 'OPENAI_API_KEY'}
ROUTE_FIELDS = {'id', 'result', 'config', 'authMethod', 'requiresOpenaiAuth', 'model', 'model_provider',
                'model_providers', 'openai', 'openai_base_url', 'chatgpt_base_url', 'model_reasoning_effort',
                'service_tier', 'model_catalog_json', 'model_instructions_file'}


def sha256(value):
    return isinstance(value, str) and bool(re.fullmatch('[0-9a-f]{64}', value))


def validate_certificate(value):
    from .review_benchmark import CRITERIA
    if (not isinstance(value, dict) or not SCHEMA <= set(value) or set(value) - SCHEMA - {'inspection'} or
            type(value['version']) is not int or value['version'] != 1 or
            value['evaluation'] != 'paired-real-model-focused-upstream' or
            not re.fullmatch('[0-9a-f]{40}', value['upstream_base']) or
            not all(sha256(value[k]) for k in ('profile_sha256', 'runtime_sha256', 'report_sha256',
                                              'adjudication_sha256', 'full_activation_sha256')) or
            value['criteria_sha256'] != digest(CRITERIA)):
        raise ValueError('Bundled review evidence is incomplete or uses different criteria')
    policy, cli, metrics = (value[k] for k in ('policy', 'cli', 'metrics'))
    contract = policy['behavioral_contract']
    if (set(policy) != {'mode', 'providers', 'behavioral_contract'} or policy['mode'] != 'risk-based' or
            policy['providers'] != {'review': 'codex-native'} or
            set(contract) != {'paths', 'check_ids', 'instructions'} or
            contract['paths'] != [MODULE] or contract['check_ids'] != [1] or
            not contract['instructions'] or
            not all(sha256(sha) for sha in contract['instructions'].values()) or
            set(cli) != {'version', 'sha256', 'provider_sha256'} or
            not all(sha256(cli[key]) for key in ('sha256', 'provider_sha256')) or
            not cli['version'].startswith('codex-cli ')):
        raise ValueError('Bundled review evidence covers a different policy or CLI')
    if (set(metrics) != METRICS or any(type(metrics[k]) not in (float, int) or metrics[k] != 1
                for k in ('detection_recall', 'acceptance_coverage')) or
            any(type(metrics[k]) is not int or not 1 <= metrics[k] <= CRITERIA['max_cases']
                for k in ('retained_cases', 'seeded_cases', 'clean_cases')) or
            sum(metrics[k] for k in ('retained_cases', 'seeded_cases', 'clean_cases')) > CRITERIA['max_cases'] or
            any(type(metrics[k]) is not int or metrics[k] < 0
                for k in ('baseline_false_positives', 'candidate_false_positives')) or
            metrics['candidate_false_positives'] > metrics['baseline_false_positives'] or
            any(type(metrics[k]) not in (float, int) or not math.isfinite(metrics[k]) or
                not 0 < metrics[k] <= CRITERIA['maximum_latency_ratio']
                for k in ('median_review_ratio', 'median_end_to_end_ratio', 'max_clean_end_to_end_ratio'))):
        raise ValueError('Bundled review evidence does not satisfy the measured promotion gates')


def read_certificate(expected=None):
    path = PACKAGE / CERTIFICATE
    if path.resolve() != path or not path.is_file() or path.stat().st_size > 16384:
        raise ValueError('Bundled upstream review qualification is unavailable')
    data = path.read_bytes()
    actual = hashlib.sha256(data).hexdigest()
    if expected is not None and expected != actual:
        raise ValueError('Bundled upstream review qualification changed')
    value = json.loads(data)
    validate_certificate(value)
    return value, actual


def read_route(tool, wt, args):
    """Inspect only effective routing and token-free auth status over local STDIO."""
    command = [tool, 'app-server', '--stdio', *args]
    allowance = _io_allowance.get()
    deadline = time.monotonic() + (min(10, allowance()) if allowance else 10)
    with subprocess.Popen(command, cwd=wt, env={**os.environ, 'SFC_WORKER': '1'}, stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as proc:
        buffer = b''
        remaining = 2 * 1024 * 1024

        def request(method, params, identifier=None):
            nonlocal buffer, remaining
            message = {'method': method, 'params': params}
            if identifier is not None:
                message['id'] = identifier
            proc.stdin.write((json.dumps(message) + '\n').encode())
            proc.stdin.flush()
            while identifier is not None and time.monotonic() < deadline:
                if b'\n' in buffer:
                    line, buffer = buffer.split(b'\n', 1)
                    reply = json.loads(line, object_pairs_hook=lambda pairs: {
                        key: value for key, value in pairs if key in ROUTE_FIELDS})
                    if reply.get('id') == identifier:
                        if 'result' in reply:
                            return reply['result']
                        break
                    continue
                if not select.select([proc.stdout], [], [], max(0, deadline - time.monotonic()))[0]:
                    break
                data = os.read(proc.stdout.fileno(), 65536)
                remaining -= len(data)
                if not data or remaining < 0:
                    break
                buffer += data
            if identifier is not None:
                raise ValueError('Local Codex provider readback is unavailable')

        try:
            request('initialize', {'clientInfo': {'name': 'sfc-provider-check', 'version': '1'}}, 1)
            request('initialized', {})
            result = request('config/read', {'cwd': str(Path(wt).resolve()), 'includeLayers': False}, 2)
            auth = request('getAuthStatus', {'includeToken': False, 'refreshToken': False}, 3)
            return result['config'], auth
        finally:
            if proc.poll() is None:
                proc.kill()


def provider_binding(effective, auth, expected):
    if (ROUTE_ENV.intersection(os.environ) or effective.get('model_provider') not in (None, 'openai') or
            'openai' in (effective.get('model_providers') or {}) or effective.get('openai_base_url') is not None or
            effective.get('chatgpt_base_url') not in (None, 'https://chatgpt.com/backend-api/',
                                                     'https://chatgpt.com/backend-api') or
            effective.get('model_catalog_json') is not None or effective.get('model_instructions_file') is not None or
            auth.get('authMethod') != 'chatgpt' or auth.get('requiresOpenaiAuth') is not True or
            any(effective.get(key) != value for key, value in expected.items())):
        raise ValueError('Codex is outside the qualified first-party ChatGPT provider route')
    return digest({'provider': 'openai', 'auth_method': 'chatgpt', 'settings': expected})


def cli_identity(wt, config):
    tool = shutil.which('codex')
    if not tool:
        raise ValueError('Qualified Codex CLI is unavailable')
    expected = {'model': config.get('models', {}).get('review') or config.get('model'),
                'model_reasoning_effort': config.get('reasoning_effort'), 'service_tier': config.get('service_tier')}
    args = worker_runtime.arguments(config)
    for key, value in expected.items():
        if not value:
            raise ValueError('Qualified Codex provider requires explicit worker settings')
        args += ['-c', f'{key}={json.dumps(value)}']
    effective, auth = read_route(tool, wt, args)
    return {'version': run([tool, '--version'], timeout=15).stdout.strip(), 'sha256': checksum(tool),
            'provider_sha256': provider_binding(effective, auth, expected)}


def profile_binding(root, config):
    """Remove host paths only after binding them to the supported local toolchain."""
    from .efficiency import COMMANDS
    from .review_policy import model_settings

    policies = config.get('verification_reuse', [])
    if config['verify_commands'] != list(COMMANDS) or [p['command'] for p in policies] != list(COMMANDS):
        raise ValueError('Qualified review requires both ordered SFC project checks and their contracts')
    local = efficiency_runtime.plan(root)
    git_tool = shutil.which('git')
    if not local or not git_tool:
        raise ValueError('Qualified review requires the supported isolated Python check runtime')
    tools = [local['executable'], str(Path(git_tool).resolve())]
    normalized = []
    for policy in policies:
        if (policy.get('runtime') != local or policy.get('tools') != tools or
                policy.get('python_stdlib') != local['stdlib'] or policy.get('dependencies') or
                policy.get('artifacts') or policy.get('source_inputs')):
            raise ValueError('Qualified review check contract differs from the supported runtime')
        normalized.append({k: v for k, v in policy.items() if k not in ('runtime', 'tools', 'python_stdlib')})
    tool_fingerprints = {}
    for name in sorted({name for row in policies for name in row.get('path_tools', [])}):
        executable = shutil.which(name)
        if not executable:
            raise ValueError('Qualified review check tool is unavailable')
        tool_fingerprints[name] = checksum(executable)
    return digest({'checks': normalized, 'model': model_settings(config),
                   'worker_runtime': worker_runtime.settings(config),
                   'worker_timeout': config['worker_timeout'], 'verify_timeout': config['verify_timeout'],
                   'tools': tool_fingerprints,
                   'python': {'version': local['version'], 'executable_sha256': local['executable_sha256'],
                              'native_libraries': sorted(local['native_libraries'].values())}})


def base_revision(root, config):
    branch = config['base_branch']
    return (git(root, 'rev-parse', '--verify', f'refs/remotes/origin/{branch}^{{commit}}', check=False) or
            git(root, 'rev-parse', '--verify', f'refs/heads/{branch}^{{commit}}'))


def source_matches(wt, base, certificate):
    anchor = certificate['upstream_base']
    if run(['git', 'merge-base', '--is-ancestor', anchor, base], wt, check=False).returncode != 0:
        return False
    changed = git(wt, 'diff', '--no-ext-diff', '--no-textconv', '--no-renames', '--name-only', '-z',
                  anchor, base).split('\0')
    if set(changed) - {'', MODULE, CERTIFICATE, 'README.md'}:
        return False
    # The target module must still be an ordinary tracked source file.
    entry = git(wt, '--literal-pathspecs', 'ls-tree', base, '--', MODULE)
    if not entry.startswith('100644 blob '):
        return False
    instructions = certificate['policy']['behavioral_contract']['instructions']
    tracked = git(wt, 'ls-tree', '-r', '--name-only', '-z', base).split('\0')
    names = ('AGENTS.md', 'AGENTS.override.md')
    if set(instructions) != {name for name in tracked if Path(name).name in names}:
        return False
    # Extra instructions in the checkout can also affect the selected lane.
    for directory in Path(MODULE).parents:
        for name in names:
            relative = (directory / name).as_posix()
            target = Path(wt) / relative
            if (target.exists() or target.is_symlink()) and relative not in instructions:
                return False
    return all((path := Path(wt) / name).is_file() and not path.is_symlink() and checksum(path) == sha
               for name, sha in instructions.items())


def match(root, config, wt, base, certificate):
    from .review_policy import comparison_policy, runtime_binding
    if (certificate['policy'] != comparison_policy(config) or
            certificate['profile_sha256'] != profile_binding(root, config)):
        return False, 'baseline retained; review or project check settings differ from the qualified upstream profile'
    if not source_matches(wt, base, certificate):
        return False, 'baseline retained; base source or instructions are outside the qualified upstream profile'
    try:
        cli = cli_identity(wt, config)
    except ERRORS:
        return False, 'baseline retained; the qualified first-party ChatGPT provider route could not be verified'
    if certificate['runtime_sha256'] != runtime_binding() or certificate['cli'] != cli:
        return False, 'baseline retained; Codex CLI, provider or review runtime differs from the qualified upstream profile'
    return True, 'bundled upstream review qualification covers this configuration and base'


def qualify(root, config, *, wt=None, base=None):
    try:
        certificate, _ = read_certificate(config['review_policy']['cohort'])
        return match(root, config, wt or root, base or base_revision(root, config), certificate)
    except ERRORS:
        return False, 'baseline retained; bundled upstream review qualification is unavailable or incomplete'


def inspection_controls(config, profile):
    """Bind the measured concurrency and, for releases, package/publication controls."""
    controls = {'profile': profile, 'max_workers': config['max_workers'], 'max_attempts': config['max_attempts']}
    if profile == 'release':
        release = config.get('release') or {}
        if not release.get('enabled'):
            raise ValueError('Bundled release inspection needs enabled release configuration')
        release = dict(release)
        for key in ('authority', 'approved_summaries'):
            if key in release:
                release[key] = {k: v for k, v in release[key].items()
                                if k not in ('authorization_id', 'repo', 'base_branch')}
        controls.update(release=release, merge_mode=config.get('merge_mode', 'github-protected'),
                        allow_no_checks=config['allow_no_checks'])
    return digest(controls)


def inspection_proof(config, certificate, profile, *, allow_unconfigured=False):
    from .review_benchmark import INSPECTION_CRITERIA as criteria

    proof = certificate.get('inspection', {}).get(profile)
    if (not isinstance(proof, dict) or set(proof) != INSPECTION_PROOF or config['max_workers'] < 2 or
            proof['criteria_sha256'] != digest(criteria) or
            not all(sha256(proof[key]) for key in ('report_sha256', 'adjudication_sha256', 'controls_sha256'))):
        return None
    metrics = proof['metrics']
    if not isinstance(metrics, dict) or set(metrics) != INSPECTION_METRICS:
        return None
    counts = metrics['case_counts']
    if (not isinstance(counts, dict) or set(counts) != set(criteria['required_kinds']) or
            any(type(v) is not int or v < 1 for v in counts.values()) or
            sum(counts.values()) > criteria['maximum_pairs_per_profile'] or
            any(type(metrics[k]) not in (float, int) or metrics[k] != 1
                for k in ('detection_recall', 'acceptance_coverage')) or
            any(type(metrics[k]) is not int or metrics[k] < 0
                for k in ('baseline_false_positives', 'candidate_false_positives')) or
            metrics['candidate_false_positives'] > metrics['baseline_false_positives'] or
            any(type(metrics[k]) not in (float, int) or not math.isfinite(metrics[k]) or
                not 0 < metrics[k] <= criteria['maximum_latency_ratio']
                for k in ('median_end_to_end_ratio', 'max_clean_end_to_end_ratio')) or
            type(metrics['minimum_clean_check_seconds']) not in (float, int) or
            not math.isfinite(metrics['minimum_clean_check_seconds']) or
            metrics['minimum_clean_check_seconds'] < criteria['minimum_check_seconds']):
        return None
    if profile == 'release' and allow_unconfigured and 'release' not in config:
        return proof
    return proof if proof['controls_sha256'] == inspection_controls(config, profile) else None


def qualify_inspection(root, config, profile, *, wt=None, revision=None):
    try:
        certificate, _ = read_certificate(config['review_policy']['cohort'])
        if not inspection_proof(config, certificate, profile):
            return False
        return match(root, config, wt or root, revision or base_revision(root, config), certificate)[0]
    except ERRORS:
        return False


def propose(root, config):
    try:
        from .review_policy import validate
        certificate, sha = read_certificate()
        policy = {**certificate['policy'], 'cohort': sha}
        proposed = {**config, 'review_policy': policy}
        validate(proposed)
        active, reason = match(root, proposed, root, base_revision(root, proposed), certificate)
        if active:
            evidence = [certificate[k] for k in ('report_sha256', 'adjudication_sha256', 'full_activation_sha256')]
            inspection = {}
            for profile in ('source', 'release'):
                try:
                    proof = inspection_proof(config, certificate, profile, allow_unconfigured=True)
                except ERRORS:
                    continue
                if proof:
                    inspection[profile] = ('pending release configuration' if profile == 'release' and
                                           'release' not in config else 'selected')
                    evidence.extend(proof[k] for k in ('report_sha256', 'adjudication_sha256'))
            if inspection:
                policy['inspection'] = {'enabled': list(inspection)}
                validate(proposed)
                return policy, reason, {'cohort_sha256': sha, 'evidence_sha256': evidence, 'inspection': inspection}
            return policy, reason, {'cohort_sha256': sha, 'evidence_sha256': evidence}
        return None, reason, {}
    except ERRORS:
        return None, 'No matching bundled upstream review qualification', {}
