"""Bindings for bounded review trials on isolated copies of project history."""
from __future__ import annotations

import copy
from pathlib import Path
import re

from . import efficiency_runtime, review_policy
from .common import common_dir, read_json, validate_config
from .verification import checksum, load

KINDS = {'retained-real-finding', 'seeded-regression', 'clean'}
MAX_CASES = 6
CASE_FIELDS = {'case', 'kind', 'base', 'head', 'title', 'body', 'labels', 'expected', 'oracle'}
SHA = re.compile(r'[0-9a-f]{40}|[0-9a-f]{64}')
CASE = re.compile(r'[a-z][a-z0-9-]{0,63}')
MANIFEST_FIELDS = {'version', 'source_root', 'target_config', 'cases'}
PLAN_FIELDS = {'version', 'target_config', 'source_common', 'isolated_common', 'cases', 'limits',
               'runtime_sha256', 'cli_version'}


def evidence_ref(parent, ref):
    review_policy.validate_ref(ref)
    path = (Path(parent) / ref['path']).resolve(strict=True)
    if not path.is_file() or checksum(path) != ref['sha256']:
        raise ValueError('Project comparison evidence changed')
    return {'path': str(path), 'sha256': ref['sha256']}


def sample_cards(cases, repository):
    if (not isinstance(cases, list) or not len(KINDS) <= len(cases) <= MAX_CASES or
            {row['kind'] for row in cases} != KINDS):
        raise ValueError('Project comparison requires retained, seeded and clean controls within budget')
    result = {}
    for number, row in enumerate(cases, 1):
        if (set(row) != CASE_FIELDS or not CASE.fullmatch(row['case']) or row['case'] in result or
                not all(SHA.fullmatch(row[k]) for k in ('base', 'head')) or row['base'] == row['head'] or
                not isinstance(row['title'], str) or not row['title'].strip() or
                not isinstance(row['body'], str) or not isinstance(row['labels'], list) or
                any(not isinstance(label, str) or not label.strip() for label in row['labels']) or
                not isinstance(row['expected'], list) or len(row['expected']) > 8 or
                any(not isinstance(item, str) or not CASE.fullmatch(item) for item in row['expected']) or
                len(set(row['expected'])) != len(row['expected']) or
                bool(row['expected']) != (row['kind'] != 'clean')):
            raise ValueError('Invalid project comparison sample, candidate or expected finding inventory')
        card = {'number': number, 'id': f'fixture-issue-{number}', 'item_id': f'fixture-item-{number}',
                'repository': {'nameWithOwner': repository}, 'url': f'https://github.com/{repository}/issues/{number}',
                **{k: row[k] for k in ('title', 'body', 'labels')}}
        review_policy.criteria(card)
        review_policy.validate_ref(row['oracle'])
        result[row['case']] = card
    return result


def isolated_config(target, target_common, isolated_common):
    """Recreate a declared checkout-owned runtime inside the trial's Git common."""
    config = copy.deepcopy(target)
    target_common, isolated_common = Path(target_common).resolve(), Path(isolated_common).resolve()
    if target_common == isolated_common:
        raise ValueError('Project comparison requires its own Git common directory')
    for policy in config.get('verification_reuse', []):
        if runtime := policy.get('runtime'):
            efficiency_runtime.validate(runtime)
            suffix = Path(efficiency_runtime.FOLDER) / efficiency_runtime.location_key(runtime)
            if Path(runtime['location']) != target_common / suffix:
                raise ValueError('Target check runtime is outside its verified Git common directory')
            runtime['location'] = str(isolated_common / suffix)
    return validate_config(config)


def trial_limits(cases):
    from .review_benchmark import EXECUTION_LIMITS
    return {**EXECUTION_LIMITS, 'maximum_model_dispatches': len(cases) * 8}


def measurement_limits(manifest, target):
    defaults = trial_limits(manifest['cases'])
    if 'measurement_limits' not in manifest:
        return defaults
    limits = manifest['measurement_limits']
    if (not isinstance(limits, dict) or limits.keys() != defaults.keys() or
            any(type(value) is not int or value <= 0 for value in limits.values()) or
            limits['worker_timeout'] > min(180, target['worker_timeout']) or
            limits['verify_timeout'] > min(defaults['verify_timeout'], target['verify_timeout']) or
            limits['maximum_model_dispatches'] > defaults['maximum_model_dispatches'] or
            limits['maximum_model_dispatches'] * limits['worker_timeout'] >
            defaults['maximum_model_dispatches'] * defaults['worker_timeout']):
        raise ValueError('Invalid project comparison measurement limits')
    return limits


def read_trial_manifest(path):
    value = read_json(path)
    if (not isinstance(value, dict) or set(value) - {'measurement_limits'} != MANIFEST_FIELDS or
            type(value['version']) is not int or value['version'] != 1 or
            not isinstance(value['source_root'], str) or not Path(value['source_root']).is_absolute()):
        raise ValueError('Invalid project comparison manifest')
    return value


def validate_measured_plan(plan, target):
    path = Path(evidence_ref('.', plan['manifest'])['path'])
    manifest = read_trial_manifest(path)
    proposal = read_json(evidence_ref(path.parent, manifest['target_config'])['path'])
    sample_cards(manifest['cases'], target['repo'])
    if ('measurement_limits' not in manifest or validate_config(proposal) != target or
            str(common_dir(Path(manifest['source_root'])).resolve()) != plan['source_common'] or
            [{**row, 'oracle': row['oracle']['sha256']} for row in manifest['cases']] !=
            [{**row, 'oracle': row['oracle']['sha256']} for row in plan['cases']] or
            review_policy.digest(plan['limits']) != review_policy.digest(measurement_limits(manifest, target))):
        raise ValueError('Project comparison plan differs from its declared measurement manifest')


def validate_fresh_checks(config, verification):
    checks = verification['checks']
    contracted = {p['command'] for p in config.get('verification_reuse', [])}
    if ([v['command'] for v in checks] != config['verify_commands'] or
            any(v.get('reuse') or (v['command'] in contracted and not v.get('binding')) for v in checks)):
        raise ValueError('Paired project comparison lacks fresh, bound configured checks')


def load_trial(ref, target):
    plan = read_json(evidence_ref('.', ref)['path'])
    if (not isinstance(plan, dict) or type(plan.get('version')) is not int or plan['version'] not in (1, 2) or
            set(plan) != (PLAN_FIELDS | {'manifest'} if plan['version'] == 2 else PLAN_FIELDS) or
            plan['target_config'] != target or
            not re.fullmatch(r'[0-9a-f]{64}', plan['runtime_sha256']) or not plan['cli_version'].strip() or
            any(not isinstance(plan[k], str) or not Path(plan[k]).is_absolute()
                for k in ('source_common', 'isolated_common'))):
        raise ValueError('Project comparison plan does not bind the exact target config')
    sample_cards(plan['cases'], target['repo'])
    if plan['version'] == 2:
        validate_measured_plan(plan, target)
    elif plan['limits'] != trial_limits(plan['cases']):
        raise ValueError('Project comparison plan has unexpected default measurement limits')
    isolated_config(target, plan['source_common'], plan['isolated_common'])
    return plan


def validate_sample(sample, plan):
    card = sample_cards(plan['cases'], plan['target_config']['repo'])[sample['case']]
    expected = next(row for row in plan['cases'] if row['case'] == sample['case'])
    worktree = Path(sample['worktree']).resolve()
    if (sample['card'] != card or any(sample[k] != expected[k] for k in ('kind', 'base', 'head', 'expected')) or
            common_dir(worktree).resolve() != Path(plan['isolated_common']).resolve()):
        raise ValueError('Project comparison sample differs from its pinned source or task')
    oracle = load(sample['project_oracle'])
    if (oracle['kind'] != 'project-review-oracle' or oracle['case'] != sample['case'] or
            any(oracle[k] != expected[k] for k in ('base', 'head')) or oracle['oracle'] != expected['oracle'] or
            oracle['task_digest'] != sample['task_digest']):
        raise ValueError('Project comparison oracle changed')
    evidence_ref('.', oracle['oracle'])
    if set(oracle['runs']) != {'base', 'head'}:
        raise ValueError('Both isolated source revisions require held-out oracle executions')
    for side, row in oracle['runs'].items():
        code = int(side == 'head' and sample['kind'] != 'clean')
        log = Path(evidence_ref('.', row['log'])['path'])
        if row['exit_code'] != code or (code and 'AssertionError' not in log.read_text(errors='replace')):
            raise ValueError('Project comparison failed its held-out oracle')
