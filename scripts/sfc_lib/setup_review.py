"""One inspectable setup plan; configuration never substitutes for scoped authority."""
from __future__ import annotations

import copy
import time
from types import SimpleNamespace
from urllib.parse import urlparse

from . import autostart, efficiency, onboarding, profile_qualification
from .common import Halt, git, load_config, read_json, validate_config
from .release_github import ReleaseGitHub, ReleaseVersionFileInvalid
from .releases import ReleaseTriggerPending, UnfinishedReleaseCreation, guard_release_creation, parse_version


def origin_host(root):
    origin = git(root, 'remote', 'get-url', 'origin', check=False)
    host = urlparse(origin).hostname
    if host is None and origin.startswith('git@') and ':' in origin:
        host = origin.split('@', 1)[1].split(':', 1)[0]
    return host.lower() if host else None


def release_choice(config, args, *, root):
    """Apply project-owned release settings and return the choices for review."""
    mode = getattr(args, 'release_mode', 'publish')
    source = getattr(args, 'release_config', None)
    requested = {'release_mode': mode}
    if mode == 'off':
        config['release'] = {'enabled': False}
        return requested
    if source:
        release = copy.deepcopy(read_json(source))
        if not isinstance(release, dict):
            raise Halt('--release-config requires a project-owned release object')
        # Authority is captured at this review, never imported from a command file.
        release.pop('authority', None)
        release['enabled'] = True
        trigger = release.get('trigger', 'manual')
        if trigger not in ('manual', 'version-file'):
            raise Halt('release.trigger must be manual or version-file; never every merge')
        requested['release_trigger'] = trigger
        required = ['adapter', 'package_commands', 'artifacts']
        if 'verify_commands' not in release or not release.get('preflight_commands'):
            required.append('verify_commands')
        missing = [key for key in required if release.get(key) in (None, '', [])]
        if missing:
            requested['pending_release'] = {'missing': missing, 'inputs': release}
            return requested
        missing_version_file = trigger == 'version-file' and release.get('version_file') in (None, '')
        if missing_version_file:
            release.pop('version_file', None)
        authorization = getattr(args, 'release_authorization', None)
        supported_host = origin_host(root) in (None, 'github.com')
        saved = config.get('release') or {}
        saved_authority = saved.get('authority') or {}
        if authorization and not missing_version_file and supported_host:
            release['authority'] = {'authorization_id': authorization, 'repo': config['repo'],
                                    'base_branch': config['base_branch'], 'host': 'github.com',
                                    'destination': 'github-releases', 'mode': mode}
        elif not authorization and saved_authority.get('mode') == mode:
            saved_scope = {key: value for key, value in saved.items() if key != 'authority'}
            normalized = copy.deepcopy(release)
            efficiency.move_release_preflight(root, normalized)
            if saved_scope == release or saved_scope == normalized:
                # Retain the exact saved scope, including its effective command phases.
                release = copy.deepcopy(saved)
        if trigger == 'version-file' and not release.get('authority'):
            # Preserve the requested trigger for review, with a safe effective configuration.
            release['trigger'] = 'manual'
        config['release'] = release
        validate_config(config)
    elif getattr(args, 'release_authorization', None):
        raise Halt('--release-authorization requires --release-config with exact package/check commands and artifacts')
    return requested


def bind_release_authority(config, identity):
    config['release']['authority'].update(repository_id=identity['repository_id'], project=identity['project_id'])


def watch_eligibility(root, config, automatic, dashboard):
    """Report what saved authority can support without inferring live observation."""
    if not automatic.get('enabled'):
        reason = {
            'pending-setup': 'Complete dashboard setup, then run onboard-finish with the saved watcher choice',
            'needs-inspection': 'Inspect the saved watcher choice, repository and Project metadata before re-arming',
        }.get(automatic.get('state'), 'Authorize watching with autostart --enable or choose watching during onboarding')
        return 'pending', reason
    common, _, _ = autostart.paths(root)
    if (common / 'sfc-stop').exists() or (root / '.sfc/stop').exists():
        return 'pending', 'A stop is recorded. Inspect the saved state before explicitly re-arming'
    if dashboard is not None and not dashboard.get('ready'):
        return 'pending', 'Repair the dashboard and check its saved grouping and fields before watcher admission'
    state = automatic.get('state')
    if state == 'starting':
        rearm = 'autostart --enable'
        if automatic.get('version') == 2:
            rearm += ' --session-lifetime'
        merge = (f'. To retain automatic merge, explicitly choose {rearm} --allow-merge'
                 if automatic['scope']['allow_merge'] else '')
        return ('pending', 'A watcher start intent is retained. Check session_status or autostart-status. '
                f'If no supervisor is active, inspect the receipts and explicitly reauthorize with {rearm}{merge}')
    if automatic.get('version') == 1 and automatic.get('state') in ('armed', 'finished', 'cancelled'):
        try:
            path = common / 'sfc-watch.json'
            latest = read_json(path) if path.exists() else None
            resume = autostart.bounded_window(automatic, latest)
            if latest is not None and not resume and latest['root'] != str(root.resolve()):
                raise Halt('Recorded watcher belongs to another checkout; use its root to preserve owned work')
            if resume:
                from .watch import remaining, validate_resume
                validate_resume(latest, resume=resume, root=root.resolve(), config_digest=automatic['config_digest'],
                                watch_settings=automatic['scope']['watch'], allow_merge=automatic['scope']['allow_merge'])
                budget = remaining(latest, time.time())
                if not budget['seconds']:
                    return 'unavailable', 'The original bounded watcher window expired; inspect before explicitly re-arming'
                exhausted = next((name for name, amount in budget.items() if not amount), None)
                if exhausted:
                    return 'unavailable', f'The original bounded watcher window has no remaining {exhausted.replace("_", " ")}; inspect before explicitly re-arming'
        except Halt as exc:
            return 'pending', str(exc)
        except (KeyError, OSError, TypeError, ValueError):
            return 'pending', 'Inspect the missing or invalid bounded watcher receipt'
        if resume:
            return 'active', 'The interrupted bounded window can resume with its original budget'
        if automatic['state'] == 'finished':
            return 'active', 'Saved bounded authority permits a new eligible chat to start its own finite window'
        return 'active', 'Saved watcher scope is eligible for trusted native attachment'
    if automatic.get('version') == 2 and state in ('armed', 'window-finished', 'suspended'):
        try:
            if (automatic.get('mode') != 'interactive-session' or automatic.get('continue_windows') is not True
                    or not autostart.identifier(automatic.get('id'))
                    or any(automatic.get(key) is not None and not autostart.identifier(automatic[key])
                           for key in ('watcher', 'baseline'))
                    or (state in ('window-finished', 'suspended') and
                        not autostart.identifier(automatic.get('watcher')))):
                raise Halt('Inspect incomplete saved lifetime authority before re-arming')
            path = common / 'sfc-watch.json'
            latest = read_json(path) if path.exists() else None
            latest_id = latest['id'] if latest else None
            owned = latest is not None and latest_id == automatic['watcher']
            resume, _ = autostart.lifetime_window(automatic, latest, time.time(), config['max_no_progress'])
            if latest and (latest['root'] != str(root.resolve()) or (
                    owned and (
                        latest['config_digest'] != automatic['config_digest']
                        or latest['settings'] != automatic['scope']['watch']
                        or latest['allow_merge'] != automatic['scope']['allow_merge']
                        or latest.get('lifetime', {}).get('authorization') != automatic['id']))):
                raise Halt('The recorded window scope changed. Inspect the saved receipts before re-arming')
            if resume:
                return 'active', 'The original finite window is eligible to resume with its remaining budget'
            if state == 'armed':
                return 'active', 'Saved watcher scope is eligible for trusted native attachment'
            return 'active', 'Saved lifetime authority permits the next finite window'
        except Halt as exc:
            return 'pending', str(exc)
        except (AttributeError, KeyError, OSError, TypeError, ValueError):
            return 'pending', 'Inspect the missing or invalid watcher window receipt before re-arming'
    return 'pending', 'Inspect the recorded watcher state before explicitly re-arming'


def review(root, config, *, automatic=None, requested=None, plan=None, dashboard=None, gh=None, identity=None):
    automatic = automatic or {'enabled': False}
    if not isinstance(automatic, dict):
        automatic = {'enabled': False, 'state': 'needs-inspection'}
    requested = requested or {}
    if automatic.get('enabled') and automatic.get('config_digest') != autostart.digest(config):
        automatic = {**automatic, 'enabled': False, 'state': 'needs-inspection'}
    scope = autostart.summary(SimpleNamespace(root=root.resolve(), c=config),
                              automatic.get('scope', {}).get('allow_merge', False))
    opted_out = automatic.get('enabled') is False and (
        automatic.get('disabled_at') or
        (automatic.get('version') in (1, 2) and automatic.get('state') != 'needs-inspection'))
    watching = requested.get('watch_requested', not opted_out)
    watch_mode_unset = automatic == {'enabled': False} and 'watch_requested' not in requested
    lifetime = requested.get('lifetime', automatic.get('version') == 2)
    allow_merge = requested.get('allow_merge', scope['allow_merge'])
    scope['allow_merge'] = allow_merge
    def capability(status, reason, fallback, choices, **details):
        return dict(status=status, reason=reason, fallback=fallback, choices=choices, **details)
    host_supported = True
    if watching and lifetime and automatic.get('state') != 'needs-inspection':
        from .lifeline import supported
        host_supported = supported()
    eligibility, watch_reason = watch_eligibility(root, config, automatic, dashboard)
    if not host_supported:
        watch_reason = 'Lifetime watching requires a supported host'
    watch_status = 'off' if not watching else 'unavailable' if not host_supported else eligibility
    if watch_status == 'off':
        watch_reason = 'Automatic watching is explicitly off'
    pickup_enabled = scope['watch'].get('pickup', True)
    pickup_reason = ('Automatic pickup of new work tickets is off' if not pickup_enabled else
                     'Authorized watcher scope selects eligible Backlog and Ready by Priority'
                     if watch_status == 'active' else watch_reason)
    merge_reason = ('Requires auto policy and separately saved run/watch merge authority' if not allow_merge else watch_reason)
    capabilities = {
        'watcher': capability(watch_status, watch_reason,
                             'Use an explicit finite watch or run; no background observation is inferred',
                             ['session-lifetime', 'bounded', 'off'], recommended='session-lifetime',
                             mode=('off' if not watching else 'unset' if watch_mode_unset else
                                   'session-lifetime' if lifetime else 'bounded'), scope=scope,
                             authority='Worker usage across finite windows while the native connection lives' if lifetime
                                       else 'Worker usage within one finite watcher window'),
        'pickup': capability(watch_status if pickup_enabled else 'off', pickup_reason,
                            'Explicit scoped run; holds, exclusions, dependencies, criteria and recovery gates still apply',
                            ['on', 'off'], recommended='on', stages=['Backlog', 'Ready'] if pickup_enabled else [],
                            order='Project Priority', limits=scope['watch'], authority='Same repository/Project watcher scope'),
        'automatic_merge': capability('off' if config['merge_policy'] == 'human' else
                                     watch_status if allow_merge and watch_status in ('active', 'unavailable') else 'pending',
                                     merge_reason,
                                     'Leave reviewed PRs for a human; never bypass GitHub checks or sensitive/large-change gates',
                                     ['auto', 'human'], recommended='auto', base=config['base_branch'],
                                     mode=config.get('merge_mode', 'github-protected'), allow_merge=allow_merge,
                                     authority='Exact repository, Project, base and eligible merge policy in watcher scope'),
    }
    effective_release = config.get('release') or {}
    pending_release = requested.get('pending_release')
    release = pending_release['inputs'] if pending_release else effective_release
    auth = {} if pending_release else effective_release.get('authority') or {}
    mode = auth.get('mode', requested.get('release_mode', 'publish'))
    trigger = requested.get('release_trigger', release.get('trigger', 'manual'))
    missing_version_file = trigger == 'version-file' and not release.get('version_file')
    state = ('off' if release.get('enabled') is False or mode == 'off' else
             'active' if release.get('enabled') and auth and not missing_version_file else 'pending')
    host = origin_host(root)
    unsupported_host = host is not None and host != 'github.com'
    release_reason = 'Configured project package/check commands and separately scoped draft/publication authority required'
    release_fallback = 'No automatic publication; supply --release-config and --release-authorization, or select --release-mode off'
    creation_problem = None
    try:
        guard_release_creation(root)
    except UnfinishedReleaseCreation as exc:
        creation_problem = (str(exc),
                            'Ordinary admission is paused. Inspect unknown or incomplete ticket creation; never repeat the issue POST')
    except (OSError, ValueError, AttributeError):
        creation_problem = ('Release creation journals could not be verified',
                            'Inspect the repository-common release receipts before attempting recovery or admitting work')
    if creation_problem:
        state = 'pending'
        release_reason, release_fallback = creation_problem
    elif state != 'off' and unsupported_host:
        state = 'unavailable'
        release_reason = 'GitHub Releases supports github.com only; select off for this repository host'
    elif state == 'off':
        release_reason = 'GitHub releases are explicitly off'
    elif pending_release:
        missing = ', '.join('release.' + key for key in pending_release['missing'])
        release_reason = f'Set {missing}, then preview again before authorizing'
    elif missing_version_file:
        release_reason = 'Set release.version_file or choose the manual trigger, then preview again before authorizing'
    elif state == 'active':
        release_reason = 'Package and artifact checks are configured and the selected release mode is authorized'
        if gh is not None:
            remote = ReleaseGitHub(gh)
            try:
                if identity is None and 'repository_id' in auth:
                    identity = gh.onboarding_identity()
                repository_id, push = remote.repository_access()
            except Halt:
                state = 'pending'
                release_reason = 'Verify the reviewed GitHub repository, Project and push permission before running releases'
            else:
                if identity is not None and ((auth['repository_id'], auth['project']) !=
                                             (identity['repository_id'], identity['project_id'])
                                             or auth['repository_id'] != repository_id):
                    state = 'pending'
                    release_reason = 'Reviewed GitHub repository or Project changed; preview the release scope again'
                elif not push:
                    state = 'pending'
                    release_reason = 'GitHub release repository push permission is required'
                    release_fallback = 'Use a GitHub credential with push permission to the configured repository'
                elif trigger == 'version-file':
                    branch, path = config['base_branch'], release['version_file']
                    try:
                        version_file = remote.base_version_file(repository_id, branch, path)
                        if version_file is None:
                            raise ReleaseTriggerPending('The configured version file is missing from the merged source')
                        parse_version(version_file.strip(), automatic=True)
                    except Halt as exc:
                        state = 'pending'
                        release_reason = (str(exc) if isinstance(exc, (ReleaseTriggerPending, ReleaseVersionFileInvalid))
                                          else 'Could not verify the configured version file on the merged base')
                        release_fallback = f'Verify that {path} contains a supported plain-text version on {branch}, or choose the manual trigger'
    capabilities['github_releases'] = capability(state, release_reason, release_fallback,
        ['publish', 'draft', 'off'], recommended='publish', repository=config['repo'], host='github.com',
        base=config['base_branch'], mode=mode, trigger=trigger,
        effective_trigger=effective_release.get('trigger', 'manual'), artifacts=release.get('artifacts', []),
        package_commands=release.get('package_commands', []), version_file=release.get('version_file'),
        version='Explicit release-request --version or configured plain-text version_file',
        notes={'public_docs': release.get('public_docs', {}), 'policy': 'Pinned source-range public notes and independent review'},
        preflight_commands=release.get('preflight_commands', []), verify_commands=release.get('verify_commands', []),
        authority=auth or 'Pending separate authorization ID scoped to this repository/base, github.com and selected mode')
    details = efficiency.status(config, root=root, plan=plan)
    reuse = any(check['reuse_scope'] != 'off' for check in details['checks'])
    off = details.get('profile') == 'off'
    capabilities['test_result_reuse'] = capability('active' if reuse else 'off' if off else 'pending',
        'Only passing immutable evidence with matching source, environment, coverage and candidate pins is eligible',
        'Execute fresh checks whenever the command, qualification or evidence is unsupported/missing/stale',
        ['auto', 'sfc-python', 'sfc-python-docs', 'off'], recommended='auto', checks=details['checks'],
        documentation_independent_check_count=sum(p.get('documentation_independent') is True
                                                 for p in config.get('verification_reuse', [])))
    review_policy = config.get('review_policy')
    qualification = details.get('qualification', {}).get('review') or {}
    selected = qualification.get('status') == 'selected'
    if review_policy and qualification.get('status') not in ('selected', 'stale'):
        try:
            selected, reason = profile_qualification.review_qualified(root, config)
        except profile_qualification.EVIDENCE_ERRORS:
            selected, reason = False, 'Configured review qualification is unavailable, changed or incomplete'
        qualification = profile_qualification.decision('selected' if selected else 'retained', reason)
    capabilities['review_efficiency'] = capability('active' if selected else
                                                 'off' if review_policy == {} or (off and not review_policy) else 'pending',
        qualification.get('reason', 'Matching independent qualification required'),
        'Baseline independent QA, Security and Review; fresh assessment whenever qualification or evidence is missing',
        ['qualified', 'baseline'], recommended='qualified', qualification=qualification or None)
    return {'capabilities': capabilities, 'applied': False,
            'status_scope': 'Active means configured eligibility. Use session_status for live observation and run receipts for execution or publication.',
            'authority_notice': 'Review choices before applying. Setup starts no workers; only explicit scope grants merge/publication authority.'}


def upgrade(root, github_type, *, apply=False, expected_digest=None, expected_proposed_digest=None,
            release_config=None, release_mode=None, release_authorization=None):
    """Explicit efficiency migration; existing settings and authority are never renewed."""
    config = load_config(root)
    metadata = None
    dashboard = None
    gh = None
    release = config.get('release') or {}
    saved_release = efficiency.saved_release_request(root, config)
    if (autostart.paths(root)[1].exists() or (root / '.sfc/onboarding.json').exists() or
            (release.get('enabled') and release.get('authority'))):
        try:
            gh = github_type(root, config)
            gh.verify_repository()
            metadata = gh.metadata()
            dashboard = gh.dashboard()
        except Halt:
            # A failed readiness read leaves watcher admission pending.
            dashboard = {'ready': False}
    update = None
    requested = None
    if release_config or release_mode or release_authorization:
        if not release_config and release_mode != 'off':
            raise Halt('Release scope changes require --release-config with the exact reviewed commands and artifacts')
        selected = copy.deepcopy(config)
        saved_authority = release.get('authority') or {}
        saved_mode = 'off' if release.get('enabled') is False else saved_authority.get('mode')
        mode = release_mode or saved_mode or saved_release.get('release_mode', 'publish')
        release_args = SimpleNamespace(release_config=release_config, release_mode=mode,
                                       release_authorization=release_authorization)
        requested = release_choice(selected, release_args, root=root)
        if not requested.get('pending_release') and release_authorization and selected.get('release', {}).get('authority'):
            gh = gh or github_type(root, config)
            bind_release_authority(selected, gh.onboarding_identity())
        if 'release' in selected:
            update = {'release': selected['release']}
    # A previous opt-out remains an opt-out across upgrades.
    profile = 'off' if efficiency.profile_intent(root) == 'off' else 'auto'
    if profile != 'off' and efficiency.recommended_profile(root, config) == 'sfc-python-docs':
        baseline = efficiency.migration(root, profile='auto', config_update=update)
        if baseline['qualification']['review']['status'] != 'selected':
            try:
                efficiency.migration(root, profile='sfc-python-docs', config_update=update)
            except efficiency.DocumentationContractConflict:
                pass  # Retain auto's preservation of explicit verification contracts.
            else:
                profile = 'sfc-python-docs'
    saved_request = requested
    if requested and requested.get('pending_release') and config.get('release'):
        saved_request = None  # Preserve the current usable release scope and its recorded choices.
    result = efficiency.migration(root, profile=profile, apply=apply, expected_digest=expected_digest,
                                  expected_proposed_digest=expected_proposed_digest, config_update=update,
                                  release_request=saved_request)
    proposed = result.get('proposed_config') or load_config(root)
    automatic = onboarding.current(root, proposed, metadata, gh=gh)
    watch_requested = onboarding.pending_watch_choices(root, config, automatic=automatic)
    result['setup_review'] = review(root, proposed, automatic=automatic, plan=result, dashboard=dashboard, gh=gh,
                                    requested={**(requested or saved_release), **watch_requested})
    result['setup_review']['applied'] = result['applied']
    result['authority'] = 'Saved watcher/release authority and opt-outs retained; configuration changes require explicit watcher re-arm'
    return result
