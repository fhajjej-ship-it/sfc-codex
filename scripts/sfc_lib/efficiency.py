"""Explicit, inspectable efficiency configuration; never grants run authority."""
from __future__ import annotations

import copy
import os
from pathlib import Path
import shutil
import sys
import sysconfig

from .common import Halt, common_dir, load_config, now, read_json, validate_config, write_json
from .verification import REMAINING_SUITE, RUNTIME_SUITE, digest
from . import efficiency_runtime, profile_qualification

PARALLEL_SUITE = 'python3 tests/run_suite.py'
COMMANDS = ('python3 -m unittest discover -s tests -v', 'python3 scripts/validate_plugin.py')
DOCUMENTATION_CHECKS = (RUNTIME_SUITE, REMAINING_SUITE, COMMANDS[1])
DEPENDENCY_MANIFESTS = ('pyproject.toml', 'setup.py', 'setup.cfg', 'Pipfile', 'Pipfile.lock',
                        'uv.lock', 'poetry.lock', 'environment.yml', '.gitmodules')


class DocumentationContractConflict(Halt):
    """Explicit verification contracts require the ordinary migration fallback."""


def recommended_profile(root, config):
    """Documentation reuse only for the supported partitioned standard suite."""
    runner = Path(root) / 'tests/run_suite.py'
    review = config.get('review_policy') or {}
    if (runner.is_file() and
            'SFC_DOCS_SUITE_VERSION = 1' in runner.read_text(errors='replace').splitlines() and
            tuple(config['verify_commands']) in (COMMANDS, (PARALLEL_SUITE, COMMANDS[1]), DOCUMENTATION_CHECKS) and
            not review.keys() & {'behavioral_contract', 'correction_reuse'}):
        return 'sfc-python-docs'
    return 'auto'


def move_release_preflight(root, release):
    verifier = 'python3 scripts/verify_release_package.py'
    if (release.get('enabled') and release.get('verify_commands') == [verifier]
            and not release.get('preflight_commands') and (root / 'scripts/verify_release_package.py').is_file()):
        release['preflight_commands'] = [verifier]
        release['verify_commands'] = []
        return True
    return False


def propose(root, config, profile='auto', *, review_qualification=None, model_qualification=None,
            explicit_model_settings=frozenset(profile_qualification.MODEL_FIELDS)):
    root = Path(root)
    result = copy.deepcopy(config)
    changes, gaps, manifests = [], [], {}
    implicit = sorted(set(profile_qualification.MODEL_FIELDS) - explicit_model_settings)
    qualification = {'verification': 'No reusable setup contract selected',
                     'review': profile_qualification.decision('retained', 'Existing independent review obligations retained'),
                     'model': profile_qualification.decision('retained', 'Existing model settings retained'),
                     'model_cost': 'unknown', 'end_to_end_savings': 'not established'}
    manifest = root / '.codex-plugin/plugin.json'
    try:
        supported = read_json(manifest).get('name') == 'sfc'
    except (OSError, ValueError, AttributeError):
        supported = False
    supported = supported and (root / 'scripts/validate_plugin.py').is_file() and (root / 'tests').is_dir()
    if profile == 'off':
        return {'profile': 'off', 'config': result, 'changes': [], 'gaps': ['Efficiency profile disabled'],
                'qualification': qualification, 'checks': status(result)['checks'],
                'implicit_model_settings': implicit, 'qualification_manifests': manifests}
    if profile not in ('auto', 'sfc-python', 'sfc-python-docs'):
        raise Halt('Unsupported efficiency profile')
    docs_profile = profile == 'sfc-python-docs'
    # Context scoping changes no project commands or available capabilities;
    # it is useful even when this repository has no supported reuse contract.
    if 'worker_runtime' not in result:
        result['worker_runtime'] = {'skill_catalog': False, 'memories': False}
        changes.append('Scope worker context to the assigned skill and current task; preserve tools and hooks')
    if not supported:
        gaps.append('No supported SFC Python/plugin layout; checks retain existing behavior')
    if supported and (any((root / name).exists() for name in DEPENDENCY_MANIFESTS) or
                      any(root.glob('requirements*.txt'))):
        supported = False
        gaps.append('Detected project dependencies outside the standard-library contract; fresh checks retained')
    python = shutil.which('python3')
    git = shutil.which('git')
    library = sysconfig.get_path('stdlib')
    if supported and (not python or Path(python).resolve() != Path(sys.executable).resolve() or not git or
                      not library or not Path(library).is_dir() or
                      any(not shutil.which(tool) for tool in ('shasum', 'unzip'))):
        supported = False
        gaps.append('Python, Git or package utility requirements cannot be bound; fresh checks retained')
    runtime = efficiency_runtime.plan(root) if supported else None
    if supported and runtime is None:
        supported = False
        gaps.append('Cannot prepare an isolated standard-library Python runtime; fresh execution retained')
    if supported:
        commands = set(COMMANDS)
        runner = root / 'tests/run_suite.py'
        if runner.is_file():
            commands.add(PARALLEL_SUITE)
        contract = {'max_age_seconds': 3600,
                    'environment': ['PATH', 'LANG', 'LC_ALL', 'LC_CTYPE', 'TZ', 'PYTHONHASHSEED'],
                    'tools': [str(Path(python).resolve()), str(Path(git).resolve())],
                    'path_tools': ['python3', 'git', 'shasum', 'unzip'],
                    'dependencies': [], 'artifacts': [], 'external_state': 'none',
                    'worktree': 'independent', 'scope': 'repository', 'context': 'source-only',
                    'python_stdlib': runtime['stdlib'], 'runtime': runtime, 'outputs': ['dist']}
        policies = result.get('verification_reuse', [])
        proposed = {command: {'command': command, **contract} for command in commands}
        if docs_profile:
            if (PARALLEL_SUITE not in commands or
                    'SFC_DOCS_SUITE_VERSION = 1' not in runner.read_text(errors='replace').splitlines()):
                raise Halt('Documentation reuse requires an SFC source checkout with the partitioned test runner')
            configured = tuple(result['verify_commands'])
            if configured != DOCUMENTATION_CHECKS:
                if configured not in (COMMANDS, (PARALLEL_SUITE, COMMANDS[1])):
                    raise Halt('Documentation reuse requires the standard SFC suite and validator')
                review = result.get('review_policy') or {}
                if review.keys() & {'behavioral_contract', 'correction_reuse'}:
                    raise Halt('Documentation reuse cannot split checks while review contracts reference their check IDs')
                result['verify_commands'] = list(DOCUMENTATION_CHECKS)
                changes.append('Split the complete suite into documentation-independent and remaining checks')
            path = os.pathsep.join(dict.fromkeys([
                *(str(Path(shutil.which(name)).parent) for name in ('git', 'shasum', 'unzip')),
                *os.defpath.split(os.pathsep), '/usr/sbin', '/sbin']))
            fixed = {'PATH': path, 'LC_ALL': 'C', 'TZ': 'UTC', 'PYTHONHASHSEED': '0'}
            proposed = {command: {'command': command, **contract, 'environment': list(fixed),
                                  'fixed_environment': fixed} for command in DOCUMENTATION_CHECKS}
            proposed[RUNTIME_SUITE]['documentation_independent'] = True
            if any(p != proposed.get(p['command']) and p != {'command': p['command'], **contract} for p in policies):
                raise DocumentationContractConflict('Documentation reuse cannot preserve custom verification contracts')
            policies = [p for p in policies if p == proposed.get(p['command'])]
        existing = {p['command'] for p in policies}
        for index, command in enumerate(result['verify_commands'], 1):
            if command in existing:
                continue  # Explicit contracts are never overwritten.
            if command not in proposed:
                gaps.append(f'Unsupported check-{index} retains fresh execution')
                continue
            policies.append(proposed[command])
            existing.add(command)
            changes.append(f'Enable repository evidence lookup for check-{index}')
        if policies:
            result['verification_reuse'] = policies
        if docs_profile:
            qualification['verification'] = 'Independent runtime tests plus full-source documentation and package checks'
        elif any(command in commands for command in config['verify_commands']):
            qualification['verification'] = 'Whole-source standard-library contract; offline regression coverage'
        if move_release_preflight(root, result.get('release', {})):
            changes.append('Run SFC package/install preflight before source verification')
        policy, qualification['review'] = profile_qualification.review_selection(root, result, review_qualification)
        if policy is not None:
            result['review_policy'] = policy
            if cohort := policy.get('cohort'):
                manifests['review'] = {'kind': 'bundled-cohort', 'sha256': cohort}
            changes.append('Apply independently qualified review configuration')
        settings, qualification['model'] = profile_qualification.model_selection(
            root, result, model_qualification, explicit_model_settings)
        if settings is not None:
            result.update(settings)
            changes.append('Apply model settings from a matching whole-task comparison')
    for kind in ('review', 'model'):
        if qualification[kind]['status'] == 'retained':
            gaps.append(qualification[kind]['reason'])
    for kind, path in (('review', review_qualification), ('model', model_qualification)):
        if sha256 := qualification[kind].get('manifest_sha256'):
            manifests[kind] = {'path': str(Path(path).resolve()), 'sha256': sha256}
    validate_config(result)
    selected = 'sfc-python-docs' if docs_profile else 'sfc-python'
    return {'profile': selected if supported else 'unsupported', 'config': result,
            'changes': changes, 'gaps': gaps, 'checks': status(result)['checks'], 'qualification': qualification,
            'implicit_model_settings': implicit, 'qualification_manifests': manifests,
            'contract': 'Checks must be source-only and stdlib-only. Ref/history/task/network dependencies require fresh execution. '
                        'Full source tree, Python standard library, tools, environment, freshness and artifacts are revalidated.'}


def install_runtimes(config, root=None):
    for policy in config.get('verification_reuse', []):
        if policy.get('runtime'):
            efficiency_runtime.install(policy['runtime'], root=root)


def migration(root, *, profile='auto', apply=False, expected_digest=None, expected_proposed_digest=None,
              review_qualification=None, model_qualification=None, config_update=None,
              release_request=None, explicit_profile=False):
    config = load_config(root)
    before = digest(config)
    receipt = setup_receipt(root, before)
    implicit = receipt.get('implicit_model_settings', []) if receipt else []
    if not isinstance(implicit, list) or any(k not in profile_qualification.MODEL_FIELDS for k in implicit):
        implicit = []
    explicit = set(profile_qualification.MODEL_FIELDS) - set(implicit)
    selected = validate_config({**config, **config_update}) if config_update else config
    proposal = propose(root, selected, profile, review_qualification=review_qualification,
                       model_qualification=model_qualification, explicit_model_settings=explicit)
    if selected != config:
        proposal['changes'].insert(0, 'Apply explicitly reviewed project release configuration')
    public = {k: v for k, v in proposal.items() if k not in ('config', 'qualification_manifests')}
    after = digest(proposal['config'])
    write_config = proposal['config'] != config or bool(proposal['changes'])
    prior_intent = setup_intent(root)
    intent_choices = dict(prior_intent)
    if explicit_profile:
        intent_choices['profile'] = profile
    release_choices = release_request or saved_release_request(root, config)
    if release_choices:
        intent_choices['release'] = {'choices': release_choices, 'scope': release_scope(proposal['config'])}
    binding = {'qualification': public['qualification']} if (
        review_qualification is not None or model_qualification is not None) else {}
    if intent_choices:
        binding['setup_intent'] = intent_choices
    proposal_digest = digest([after, binding]) if binding else after
    if not write_config and receipt and 'qualification' in receipt:
        public['qualification'] = status(config, root=root)['qualification']
    public.update(config_digest=before, proposed_digest=proposal_digest, proposed_config_digest=after, applied=False)
    if not apply:
        return {**public, 'proposed_config': proposal['config']}
    if expected_digest != before or expected_proposed_digest != public['proposed_digest']:
        raise Halt('Migration preview is stale or missing. Pass its exact --expected-digest and --expected-proposed-digest.')
    if not write_config and (intent_choices == prior_intent or not (explicit_profile or release_request)):
        return public
    # CLI holds the common supervisor lock; recheck the saved input at write boundary.
    if digest(load_config(root)) != before:
        raise Halt('Configuration changed during migration')
    migrations = Path(root) / '.sfc/efficiency-migrations'
    folder = migrations / before
    if write_config:
        intent = folder / 'intent.json'
        if intent.exists():
            raise Halt('Existing migration intent; inspect retained state rather than repeating writes')
        write_json(intent, {'time': now(), 'before': config, 'after': proposal['config'], 'profile': proposal['profile'],
                            'qualification': proposal['qualification'], 'setup_intent': intent_choices,
                            'qualification_manifests': proposal['qualification_manifests']})
        install_runtimes(proposal['config'], root=root)
        validate_selected_qualifications(root, proposal)
        write_json(Path(root) / '.sfc/config.json', proposal['config'])
    if load_config(root) != proposal['config']:
        raise Halt('Migration configuration readback differs; inspect retained intent')
    public['applied'] = True
    public['watching'] = 'Existing authorization was not renewed; changed configuration requires explicit re-arm'
    qualification_receipt = receipt if not write_config and receipt else proposal
    completed = {**public, 'root': str(Path(root).resolve()), 'common': str(common_dir(root)),
                 'setup_intent': intent_choices, 'qualification': qualification_receipt.get('qualification', public['qualification']),
                 'qualification_manifests': qualification_receipt.get('qualification_manifests', {})}
    if write_config:
        write_json(folder / 'complete.json', public)
    current = migrations / 'current.json'
    write_json(current, completed)
    if read_json(current) != completed:
        raise Halt('Migration receipt readback differs; inspect retained state')
    return public


def validate_selected_qualifications(root, proposal):
    decisions, refs = proposal['qualification'], proposal['qualification_manifests']
    if decisions['review']['status'] == 'selected':
        active, reason = profile_qualification.review_setup_available(
            refs['review'], decisions['review'], root, proposal['config'])
        if not active:
            raise Halt('Review qualification changed before saving setup. ' + reason)
    if decisions['model']['status'] == 'selected' and not profile_qualification.model_setup_available(
            refs['model'], decisions['model'], root, proposal['config']):
        raise Halt('Model qualification or provider environment changed before saving setup')


def local_receipt(root, path):
    try:
        receipt = read_json(Path(root) / '.sfc' / path)
        if isinstance(receipt, dict) and receipt.get('root') == str(Path(root).resolve()):
            return receipt
    except (OSError, ValueError):
        pass
    return {}


def setup_intent(root):
    """Retain explicit preferences independently of qualification and authority."""
    migration = local_receipt(root, 'efficiency-migrations/current.json')
    if migration.get('applied') is not True:
        return {}
    choices = migration.get('setup_intent')
    if isinstance(choices, dict) and migration.get('common') == str(common_dir(root)):
        return choices
    if ('setup_intent' not in migration and 'common' not in migration and migration.get('profile') == 'off'):
        # The old receipt only proves a conservative opt-out. Never import
        # historic qualification or infer an opt-in from its computed profile.
        return {'profile': 'off'}
    return {}


def profile_intent(root):
    if (profile := setup_intent(root).get('profile')) in ('auto', 'sfc-python', 'sfc-python-docs', 'off'):
        return profile
    setup = local_receipt(root, 'onboarding.json')
    recorded = setup.get('efficiency')
    if (isinstance(recorded, dict) and setup.get('version') == 1 and recorded.get('profile') == 'off'
            and setup.get('common') == str(common_dir(root))):
        return 'off'
    return None


def release_scope(config):
    return digest([config['repo'], config['base_branch'], config.get('release')])


def saved_release_request(root, config):
    scope = release_scope(config)
    release = setup_intent(root).get('release')
    if isinstance(release, dict) and release.get('scope') == scope:
        choices = release.get('choices')
    else:
        setup = local_receipt(root, 'onboarding.json')
        choices = None
        if setup.get('version') == 1 and setup.get('common') == str(common_dir(root)):
            matches = (setup['release_scope'] == scope if 'release_scope' in setup else
                       setup.get('config_digest') == digest(config))
            if matches:
                choices = setup.get('setup_choices')
    if (isinstance(choices, dict) and choices.get('release_mode') in ('publish', 'draft', 'off')
            and choices.get('release_trigger', 'manual') in ('manual', 'version-file')):
        result = {k: choices[k] for k in ('release_mode', 'release_trigger') if k in choices}
        pending = choices.get('pending_release')
        if (isinstance(pending, dict) and isinstance(pending.get('inputs'), dict)
                and isinstance(pending.get('missing'), list)
                and all(key in ('adapter', 'package_commands', 'verify_commands', 'artifacts') for key in pending['missing'])):
            result['pending_release'] = pending
        return result
    return {}


def setup_receipt(root, config_digest):
    """Only saved decisions for this exact configuration are relevant."""
    receipts = (('efficiency-migrations/current.json', 'proposed_config_digest'), ('onboarding.json', 'config_digest'))
    for path, key in receipts:
        receipt = local_receipt(root, path)
        if receipt.get(key) == config_digest:
            saved = receipt.get('efficiency', receipt)
            if isinstance(saved, dict):
                return saved
    return None


def observed_checks(config, state):
    counts = {'executed': 0, 'reused': 0, 'failed': 0}
    config_digest = digest(config)
    for record in state.get('cards', {}).values():
        evidence = record.get('verification') or {}
        if evidence.get('config_digest') != config_digest:
            continue
        for check in evidence.get('checks', []):
            kind = 'failed' if check.get('exit_code') != 0 else ('reused' if check.get('reuse') else 'executed')
            counts[kind] += 1
    return counts


def status(config, *, root=None, state=None, plan=None):
    contracts = {p['command']: p for p in config.get('verification_reuse', [])}
    result = {'checks': [{'id': 'check-' + str(i + 1),
                          'reuse_scope': contracts.get(command, {}).get('scope', 'ticket') if command in contracts else 'off',
                          'fallback': 'Execute on missing, changed, expired or invalid evidence'}
                         for i, command in enumerate(config['verify_commands'])],
              'review': 'configured' if config.get('review_policy') else 'baseline independent lanes',
              'worker_runtime': config.get('worker_runtime', {}),
              'savings': 'Use delivery-report for measured executions and unknown usage'}
    receipt = plan if plan is not None else setup_receipt(root, digest(config)) if root is not None else None
    if receipt:
        result.update({k: receipt[k] for k in ('profile', 'gaps', 'qualification') if k in receipt})
        review = result.get('qualification', {}).get('review', {})
        if plan is None and review.get('status') == 'selected':
            ref = receipt.get('qualification_manifests', {}).get('review')
            active, reason = profile_qualification.review_setup_available(ref, review, root, config)
            if not active:
                result['qualification'] = {**result['qualification'], 'review': {
                    **review, 'status': 'stale', 'reason': reason}}
        model = result.get('qualification', {}).get('model', {})
        if plan is None and model.get('status') == 'selected':
            ref = receipt.get('qualification_manifests', {}).get('model')
            if not profile_qualification.model_setup_available(ref, model, root, config):
                result['qualification'] = {**result['qualification'], 'model': {
                    **model, 'status': 'stale', 'reason': 'Saved model evidence or provider environment changed. '
                    'Review the recorded baseline before running.'}}
    if plan is None and root is not None and profile_intent(root) == 'off':
        result['profile'] = 'off'
    if state is not None:
        result['latest_ticket_checks'] = observed_checks(config, state)
    return result
