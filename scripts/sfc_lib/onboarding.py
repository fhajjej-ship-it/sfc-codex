"""Project setup receipts; host trust and live MCP observation stay host-owned."""
from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

from . import __version__, autostart
from .common import Halt, common_dir, load_config, read_json, write_json
from .factory import Factory


def implementation():
    plugin = Path(__file__).resolve().parents[2]
    paths = ('scripts/sfc.py', 'scripts/sfc_lib/onboarding.py',
             'scripts/sfc_lib/work_types.py', 'skills/sfc/references/onboard.md')
    return {'version': __version__, 'plugin_root': str(plugin),
            'sha256': {p: hashlib.sha256((plugin / p).read_bytes()).hexdigest() for p in paths},
            'loaded_skill': 'Not observable from CLI; verify the skill path/version in the native chat'}


def save(root, config, metadata, automatic_start, lifetime, *, efficiency_plan=None, setup_choices=None,
         reviewed_identity=None):
    """Save the reviewed choice before dashboard writes, without granting merges."""
    _, auth_path, _ = autostart.paths(root)
    if auth_path.exists():
        raise Halt('Existing watch authorization; inspect and preserve it before new onboarding')
    receipt = {'version': 1, 'root': str(root.resolve()), 'common': str(common_dir(root)),
               'config_digest': autostart.digest(config), 'metadata': metadata,
               'watch_requested': automatic_start, 'lifetime': lifetime,
               'watch_applied': False, 'implementation': implementation()}
    if reviewed_identity is not None:
        receipt['reviewed_identity'] = reviewed_identity
    if setup_choices is not None:
        from .efficiency import release_scope
        receipt['setup_choices'] = setup_choices
        receipt['release_scope'] = release_scope(config)
    if efficiency_plan is not None:
        receipt['efficiency'] = {k: efficiency_plan[k] for k in
                                 ('profile', 'gaps', 'qualification', 'implicit_model_settings',
                                  'qualification_manifests')
                                 if k in efficiency_plan}
    write_json(root / '.sfc/onboarding.json', receipt)
    return receipt


def pending_host():
    return {'enabled': False, 'state': 'pending-host', 'requested_mode': 'interactive-session',
            'detail': 'Project configuration is saved. Lifetime watching is unavailable on this host. '
                      'When the dashboard is ready, explicitly run autostart --enable for bounded watching, '
                      'or leave watching off.'}


def report(root, config, dashboard, automatic, *, efficiency_plan=None, gh=None):
    """Expose every capability and its evidence level, including unknown host facts."""
    def entry(status, detail):
        return {'status': status, 'detail': detail}

    checks = dashboard.get('checks', {})
    def board_check(names):
        return 'verified' if all(checks.get(name) is True for name in names) else 'pending'

    from .efficiency import saved_release_request, status as efficiency_status
    from .setup_review import review
    requested = {**saved_release_request(root, config), **pending_watch_choices(root, config, automatic=automatic)}
    setup = review(root, config, automatic=automatic, requested=requested, plan=efficiency_plan, dashboard=dashboard, gh=gh)
    setup['applied'] = True

    enabled = automatic.get('enabled') is True
    watch_state = (('armed' if automatic.get('state') == 'armed' else
                    'recorded-' + str(automatic.get('state', 'unknown')))
                   if enabled else (automatic['state'] if automatic.get('state') in
                                    ('pending-setup', 'pending-host', 'needs-inspection') else
                                    setup['capabilities']['watcher']['status']))
    capabilities = {
        'repository_board': entry('verified', config['repo'] + '; Project identity read back'),
        'base': entry('configured', config['base_branch'] + '; verify remote base during dry-run'),
        'project_checks': entry('configured', config['verify_commands']),
        'stages': entry(board_check(('stage_style', 'stage_order', 'factory_view', 'saved_columns')),
                        dashboard.get('pending', [])),
        'priority': entry('verified' if dashboard.get('priority', {}).get('ready') else 'pending',
                          'Priority meanings and saved visibility'),
        'work_types_labels': entry('verified' if dashboard.get('work_types', {}).get('ready') else 'pending',
                                  'Work types and saved Labels visibility'),
        'agent_activity': entry(board_check(('activity_fields', 'saved_activity_fields')),
                                'Saved Agent/Activity fields and visibility'),
        'workers': entry('configured', 'Model/effort/tier saved; account availability requires host verification'),
        'merge_policy': entry('configured', config['merge_policy'] + '; setup grants no merge authority'),
        'hooks': entry('unverified', 'Review SFC in Codex /hooks or Settings > Hooks; trust/disabled state is host-owned'),
        'mcp': entry('unverified', 'Use session_status on the actual SFC MCP connection; saved receipts do not prove connectivity'),
        'watcher': entry(watch_state, automatic['detail'] if watch_state == 'pending-host' else
                         'Armed is not observing; verify session_status for lifetime or autostart-status for bounded mode'),
    }
    return {'implementation': implementation(), 'capabilities': capabilities, 'setup_review': setup,
            'efficiency': efficiency_status(config, root=root, plan=efficiency_plan),
            'onboarding_complete': bool(dashboard['ready'] and automatic.get('state') not in ('pending-setup', 'pending-host', 'needs-inspection')),
            'completion_scope': 'Project configuration only; host trust, MCP connection and actual observation require native verification',
            'host_activation_verified': False,
            'next_steps': [*dashboard.get('pending', []),
                          *(['After dashboard repair, run onboard-finish to apply the saved watching choice without another approval']
                            if automatic.get('state') == 'pending-setup' else []),
                          *(['Inspect saved onboarding/autostart receipts and actual project; do not retry enables or renew authority']
                            if automatic.get('state') == 'needs-inspection' else []),
                          *(['Lifetime watching unavailable: use an explicit finite run/watch, or onboard-finish on supported macOS; other setup remains configured']
                            if automatic.get('state') == 'pending-host' else []),
                          'Run doctor, lint and run --dry-run; report actual results',
                          'Verify hooks in Codex and session_status on the actual MCP connection before claiming native readiness']}


def valid_choice(root, config, metadata, receipt):
    """Historical completed choices do not pin later explicit configuration updates."""
    if (not isinstance(receipt, dict) or receipt.get('version') != 1 or
            any(type(receipt.get(k)) is not bool for k in ('watch_requested', 'lifetime', 'watch_applied')) or
            receipt.get('root') != str(root.resolve()) or receipt.get('common') != str(common_dir(root))):
        return False
    pending = receipt['watch_requested'] and not receipt['watch_applied']
    return not pending or (receipt.get('config_digest') == autostart.digest(config) and
                           receipt.get('metadata') == metadata)


def pending_watch_choices(root, config, *, automatic=None):
    """Display saved onboarding intent while the watcher scope is still pending."""
    if automatic and automatic.get('enabled') is True:
        return {}
    from .efficiency import local_receipt
    receipt = local_receipt(root, 'onboarding.json')
    common, auth_path, _ = autostart.paths(root)
    if (receipt.get('version') != 1 or receipt.get('watch_applied') is not False or
            receipt.get('common') != str(common)):
        return {}
    if receipt.get('watch_requested') is False:
        return {} if auth_path.exists() else {'watch_requested': False}
    if receipt.get('config_digest') != autostart.digest(config):
        return {}
    choices = receipt.get('setup_choices')
    return {**{k: receipt[k] for k in ('watch_requested', 'lifetime') if type(receipt.get(k)) is bool},
            **({'allow_merge': choices['allow_merge']}
               if isinstance(choices, dict) and type(choices.get('allow_merge')) is bool else {})}


def explicit_bounded_choice(receipt, automatic):
    # Completing a saved lifetime request can only create version 2. A
    # matching version-1 authorization represents a separate bounded choice.
    return receipt['lifetime'] and autostart.bounded_receipt_valid(automatic)


def current(root, config, metadata, *, gh=None):
    """Read-only doctor projection; never consume a pending choice or infer host trust."""
    path = root / '.sfc/onboarding.json'
    receipt = read_json(path) if path.exists() else None
    if receipt is not None and not valid_choice(root, config, metadata, receipt):
        return {'enabled': False, 'state': 'needs-inspection'}
    auth_path = autostart.paths(root)[1]
    try:
        automatic = read_json(auth_path) if auth_path.exists() else {'enabled': False}
    except (OSError, ValueError):
        return {'enabled': False, 'state': 'needs-inspection'}
    if not isinstance(automatic, dict):
        return {'enabled': False, 'state': 'needs-inspection'}
    if (receipt and receipt['watch_requested'] and not receipt['watch_applied'] and
            not explicit_bounded_choice(receipt, automatic)):
        from .lifeline import supported
        if not auth_path.exists() and receipt['lifetime'] and not supported():
            return pending_host()
        return {'enabled': False, 'state': 'needs-inspection' if auth_path.exists() else 'pending-setup'}
    if automatic.get('enabled'):
        scope = automatic.get('scope')
        allow_merge = scope.get('allow_merge') if isinstance(scope, dict) else None
        if ((automatic.get('version') == 1 and not autostart.bounded_receipt_valid(automatic)) or
                automatic.get('root') != str(root.resolve()) or
                automatic.get('common') != str(common_dir(root)) or
                automatic.get('config_digest') != autostart.digest(config) or
                automatic.get('metadata') != metadata or type(allow_merge) is not bool or
                not autostart.scope_matches(SimpleNamespace(root=root.resolve(), c=config), automatic, allow_merge)):
            return {'enabled': False, 'state': 'needs-inspection'}
        if gh is not None and 'reviewed_identity' in automatic:
            try:
                autostart.verify_onboarding_identity(gh, automatic['reviewed_identity'])
            except Halt:
                return {'enabled': False, 'state': 'needs-inspection'}
    return automatic


def finish(root, gh):
    """Apply a pinned pending choice once; otherwise assess current setup read-only."""
    path = root / '.sfc/onboarding.json'
    receipt = read_json(path) if path.exists() else {}
    config = load_config(root)
    gh.verify_repository()
    metadata = gh.metadata()
    if not valid_choice(root, config, metadata, receipt):
        raise Halt('Missing/stale onboarding choice; inspect project/configuration rather than renewing authority')
    pending = receipt['watch_requested'] and not receipt['watch_applied']
    reviewed_identity = receipt.get('reviewed_identity')
    if pending and reviewed_identity is not None:
        autostart.verify_onboarding_identity(gh, reviewed_identity)
    dashboard = gh.dashboard()
    _, auth_path, _ = autostart.paths(root)
    automatic = read_json(auth_path) if auth_path.exists() else {'enabled': False}
    if not isinstance(automatic, dict):
        raise Halt('Malformed watch authorization; preserve it and inspect before finishing onboarding')
    if pending and auth_path.exists():
        automatic = current(root, config, metadata, gh=gh)
        if not explicit_bounded_choice(receipt, automatic):
            raise Halt('Watching choice changed or an enable response was lost; inspect authorization without retrying')
    elif pending:
        if (common_dir(root) / 'sfc-stop').exists() or (root / '.sfc/stop').exists():
            raise Halt('Stop requested after onboarding; inspect before explicitly re-arming')
        from .lifeline import supported
        if receipt['lifetime'] and not supported():
            automatic = pending_host()
        elif dashboard['ready']:
            automatic = autostart.configure(Factory(root, config, gh), enabled=True, lifetime=receipt['lifetime'],
                                             allow_merge=receipt.get('setup_choices', {}).get('allow_merge', False),
                                             reviewed_identity=reviewed_identity, expected_metadata=metadata)
            receipt['watch_applied'] = True
            write_json(path, receipt)
        else:
            automatic = {'enabled': False, 'state': 'pending-setup'}
    else:
        automatic = current(root, config, metadata, gh=gh)
    return {'dashboard': dashboard, 'autostart': automatic, **report(root, config, dashboard, automatic, gh=gh)}
