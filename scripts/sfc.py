#!/usr/bin/env python3
"""SFC supervisor CLI. Run from any target Git repository with --root."""
from __future__ import annotations

import argparse
import json
import signal
from pathlib import Path
import sys

from sfc_lib import __version__
from sfc_lib.common import (DEFAULT_MODEL, DEFAULT_REASONING_EFFORT, DEFAULT_SERVICE_TIER,
                            REASONING_EFFORTS, Halt, STATUSES, common_dir, exclusive, git, load_config,
                            read_json, repo_root, run, validate_config, write_json)
from sfc_lib.factory import Factory, lint_card
from sfc_lib.github import GitHub
from sfc_lib.priority import create_ticket
from sfc_lib.ticket_coordination import creation_lock
from sfc_lib.work_types import TYPES
from sfc_lib.watch import status as watch_status
from sfc_lib import autostart, chat_tracking
from sfc_lib import onboarding, setup_review
from sfc_lib import efficiency, review_policy


def worker_settings(config):
    return {"model": config.get("model"),
            "reasoning_effort": config.get("reasoning_effort"),
            "service_tier": config.get("service_tier"),
            "models": config.get("models", {}),
            **({"worker_routing": config["worker_routing"]} if "worker_routing" in config else {})}


def onboard(root, args):
    path = root / ".sfc/config.json"
    if path.exists():
        raise Halt("SFC is already configured; inspect the existing config rather than overwrite it")
    if (root / '.sfc/onboarding.json').exists():
        raise Halt('Existing onboarding receipt; preserve and inspect partial setup before retrying')
    automatic_start = getattr(args, 'auto_start', None) is not False
    lifetime_choice = getattr(args, 'session_lifetime', None)
    lifetime = automatic_start if lifetime_choice is None else lifetime_choice
    if lifetime and not automatic_start:
        raise Halt('--session-lifetime conflicts with --no-auto-start')
    from sfc_lib.lifeline import supported
    host_pending = lifetime and not supported()
    repo = args.repo or GitHub(root, {"project": {}}).call("repo", "view", "--json", "nameWithOwner")["nameWithOwner"]
    branch = args.base or git(root, "symbolic-ref", "--short", "refs/remotes/origin/HEAD", check=False).removeprefix("origin/")
    if not branch:
        raise Halt("Set --base explicitly; remote default branch is unavailable")
    c = validate_config({"version": 1, "repo": repo, "base_branch": branch,
                         "project": {"owner": args.owner, "number": args.project},
                         "status_field": args.status_field, "verify_commands": args.verify,
                         "activity_timezone": getattr(args, "activity_timezone", "UTC"),
                         "merge_policy": args.merge_policy or "auto", "auto_max_lines": 400,
                         "merge_mode": getattr(args, "merge_mode", "github-protected"),
                         "allow_no_checks": getattr(args, "allow_no_checks", False), "models": {},
                         **{key: default if (value := getattr(args, key, None)) is None
                                 else None if value == "inherit" else value
                            for key, default in (("model", DEFAULT_MODEL),
                                                 ("reasoning_effort", DEFAULT_REASONING_EFFORT),
                                                 ("service_tier", DEFAULT_SERVICE_TIER))},
                         "max_workers": 2,
                         "max_cycles": 20, "max_no_progress": 3, "max_attempts": 2,
                         "worker_timeout": 1200, "verify_timeout": 900})
    watch_settings = {key: getattr(args, 'watch_' + key) for key in autostart.settings()
                      if getattr(args, 'watch_' + key, None) is not None}
    if not getattr(args, 'pickup', True):
        watch_settings['pickup'] = False
    if watch_settings:
        c['watch'] = autostart.settings(watch_settings)
    release_requested = setup_review.release_choice(c, args, root=root)
    if getattr(args, 'review_efficiency', 'qualified') == 'baseline':
        c['review_policy'] = {}
    profile = getattr(args, 'efficiency_profile', 'auto')
    def propose(selected):
        return efficiency.propose(root, c, selected,
            review_qualification=getattr(args, 'review_qualification', None),
            model_qualification=getattr(args, 'model_qualification', None),
            explicit_model_settings=frozenset(k for k in ('model', 'reasoning_effort', 'service_tier')
                                             if getattr(args, k, None) is not None))
    efficiency_plan = propose(profile)
    if (profile == 'auto' and efficiency_plan['qualification']['review']['status'] != 'selected' and
            efficiency.recommended_profile(root, c) == 'sfc-python-docs'):
        # Never invalidate an already qualified review route by renumbering its checks.
        efficiency_plan = propose('sfc-python-docs')
    c = efficiency_plan['config']
    allow_merge = getattr(args, 'allow_merge', False)
    if allow_merge and (not automatic_start or c['merge_policy'] != 'auto'):
        raise Halt('--allow-merge requires watching and auto merge policy')
    requested = {'watch_requested': automatic_start, 'lifetime': lifetime, 'allow_merge': allow_merge,
                 **release_requested}
    preview = getattr(args, 'preview', False)
    expected = getattr(args, 'expected_review', None)
    if not preview and expected is None and (allow_merge or c.get('release', {}).get('authority')):
        raise Halt('Adding merge or release authority requires --expected-review from onboard --preview')
    gh = GitHub(root, c)
    reviewed_identity = None
    if preview or expected is not None:
        reviewed_identity = gh.onboarding_identity()
        if c.get('release', {}).get('authority'):
            setup_review.bind_release_authority(c, reviewed_identity)
        review = setup_review.review(root, c, requested=requested, plan=efficiency_plan, gh=gh,
                                    identity=reviewed_identity)
        review['identity'] = reviewed_identity
        review['review_digest'] = autostart.digest([c, requested, review])
        if preview:
            return {'setup_review': review, 'proposed_config': c, 'efficiency': efficiency_plan['qualification']}
        if expected != review['review_digest']:
            raise Halt('Setup review changed; preview again before applying the exact choices')
    else:
        gh.verify_repository()
    if autostart.paths(root)[1].exists():
        raise Halt('Existing watch authorization; inspect and preserve it before new onboarding')
    efficiency.install_runtimes(c, root=root)
    efficiency.validate_selected_qualifications(root, efficiency_plan)
    # Existing field choices are never rewritten; create a separate SFC field.
    if reviewed_identity is None:
        metadata = gh.metadata(create=True)
    else:
        autostart.verify_onboarding_identity(gh, reviewed_identity)
        metadata = gh.metadata(create=True, project_id=reviewed_identity['project_id'])
        autostart.verify_onboarding_identity(gh, reviewed_identity)
    save_project_config(root, c)
    receipt = onboarding.save(root, c, metadata, automatic_start, lifetime, efficiency_plan=efficiency_plan,
                              setup_choices=requested, reviewed_identity=reviewed_identity)
    # Persist the configuration before presentation writes so a partial setup can
    # be inspected/repaired with dashboard --apply without recreating the field.
    dashboard = gh.configure_dashboard()
    if automatic_start and dashboard['ready'] and not host_pending:
        automatic = autostart.configure(Factory(root, c, gh), enabled=True, lifetime=lifetime, allow_merge=allow_merge,
                                        reviewed_identity=reviewed_identity,
                                        expected_metadata=metadata if reviewed_identity is not None else None)
        receipt['watch_applied'] = True
        write_json(root / '.sfc/onboarding.json', receipt)
    elif host_pending:
        automatic = onboarding.pending_host()
    elif automatic_start:
        automatic = {'enabled': False, 'state': 'pending-setup',
                     'requested_mode': 'interactive-session' if lifetime else 'bounded',
                     'detail': 'Finish the dashboard setup, then run onboard-finish for the saved watching choice'}
    else:
        automatic = {'enabled': False}
    return {"config": str(path), "project_id": metadata["id"], "merge_policy": c["merge_policy"],
            "merge_mode": c["merge_mode"], "allow_no_checks": c["allow_no_checks"],
            "merge_method": "fast-forward" if c["merge_mode"] == "local-verification" else "squash",
            "worker_settings": worker_settings(c),
            "dashboard": dashboard, "autostart": automatic,
            **onboarding.report(root, c, dashboard, automatic, efficiency_plan=efficiency_plan, gh=gh)}


def save_project_config(root, config):
    exclude = common_dir(root) / "info/exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude.read_text() if exclude.exists() else ""
    if ".sfc/" not in existing.splitlines():
        with exclude.open("a") as handle:
            handle.write("\n# SFC local state, worktrees and receipts\n.sfc/\n")
    write_json(root / ".sfc/config.json", config)


def onboard_chat(root, args):
    if chat_tracking.chat_mode(root) or (root / '.sfc/onboarding.json').exists():
        raise Halt('SFC is already configured. Use doctor to check the configured Project.')
    primary = git(root, 'worktree', 'list', '--porcelain', '-z').split('\0', 1)[0]
    if primary.startswith('worktree '):
        root = Path(primary.removeprefix('worktree ')).resolve()
    if chat_tracking.chat_mode(root) or (root / '.sfc/onboarding.json').exists():
        raise Halt('SFC is already configured in the primary checkout. Preserve its saved merge policy.')
    if (args.auto_start is True or args.session_lifetime is True
            or args.release_config or args.release_authorization):
        raise Halt('SFC tracks work started in Codex chat. Set up the Project without a background worker or release grant.')
    merge_policy = args.merge_policy or 'auto'
    if args.allow_merge and merge_policy != 'auto':
        raise Halt('--allow-merge requires the automatic merge policy')
    if args.allow_merge and not args.preview and not args.expected_review:
        raise Halt('Preview --allow-merge, then apply its --expected-review digest before saving merge authority')
    repo = args.repo or GitHub(root, {"project": {}}).call("repo", "view", "--json", "nameWithOwner")["nameWithOwner"]
    branch = args.base or git(root, "symbolic-ref", "--short", "refs/remotes/origin/HEAD", check=False).removeprefix("origin/")
    if not branch:
        raise Halt("Set --base explicitly; remote default branch is unavailable")
    config = validate_config({
        "version": 1, "workflow_mode": "chat", "repo": repo, "base_branch": branch,
        "chat_merge_provider": "atomic-fast-forward",
        "project": {"owner": args.owner, "number": args.project}, "status_field": args.status_field,
        "verify_commands": args.verify or [], "activity_timezone": args.activity_timezone,
        "merge_policy": merge_policy, "merge_mode": "github-protected", "allow_no_checks": False,
        "auto_max_lines": 400, "max_workers": 2, "max_cycles": 20, "max_no_progress": 3,
        "max_attempts": 2, "worker_timeout": 1200, "verify_timeout": 900,
    })
    if args.allow_merge:
        config['chat_merge_authority'] = {'repo': repo, 'base_branch': branch,
                                         'scope': 'user-requested-tasks', 'provider': config['chat_merge_provider']}
    gh = GitHub(root, config)
    identity = gh.onboarding_identity()
    if args.allow_merge:
        config['chat_merge_authority'].update({key: identity[key] for key in ('host', 'repository_id')})
    from sfc_lib.chat_merge import support
    merge_support = support(gh, config)
    digest = autostart.digest([config, identity])
    if args.preview:
        return {"mode": "chat", "proposed_config": config, "project_id": identity["project_id"],
                "setup_review": {"review_digest": digest}, "automatic_merge": merge_support}
    if args.expected_review is not None and args.expected_review != digest:
        raise Halt('Setup review changed; preview the Project again before applying')
    autostart.verify_onboarding_identity(gh, identity)
    metadata = gh.metadata(create=True, project_id=identity["project_id"])
    autostart.verify_onboarding_identity(gh, identity)
    save_project_config(root, config)
    return {"mode": "chat", "config": str(root / ".sfc/config.json"), "project_id": metadata["id"],
            "merge_policy": config['merge_policy'],
            "chat_merge_authority": config.get('chat_merge_authority'),
            "automatic_merge": merge_support,
            "dashboard": gh.configure_dashboard(linked_prs=True)}


def doctor_chat(root):
    config = load_config(root)
    gh = GitHub(root, config)
    gh.verify_repository()
    dashboard = gh.dashboard(linked_prs=True)
    from sfc_lib.chat_merge import support
    return {"mode": "chat", "ready": True, "onboarding_complete": dashboard["ready"],
            "dashboard": dashboard, "worker_timeout": config["worker_timeout"],
            "automatic_merge": support(gh, config),
            "verify_commands": config["verify_commands"],
            "verify_command_count": len(config["verify_commands"]),
            "verify_timeout": config["verify_timeout"],
            "worker_execution": "current Codex chat", "board_execution": False}


def doctor(root):
    checks = []
    for tool in (["git", "--version"], ["gh", "--version"], ["codex", "--version"], ["gh", "auth", "status"]):
        result = run(tool, root, check=False)
        checks.append({"check": " ".join(tool), "passed": result.returncode == 0})
    c = load_config(root)
    gh = GitHub(root, c)
    gh.verify_repository()
    metadata = gh.metadata()
    checks.append({"check": "GitHub Project status field", "passed": bool(metadata)})
    ready = all(v["passed"] for v in checks)
    dashboard = gh.dashboard()
    automatic = onboarding.current(root, c, metadata, gh=gh)
    setup_report = onboarding.report(root, c, dashboard, automatic, gh=gh)
    return {"ready": ready, "checks": checks, "dashboard": dashboard,
            "max_workers": c["max_workers"],
            "verify_command_count": len(c["verify_commands"]),
            "documentation_independent_check_count": sum(
                1 for contract in c.get("verification_reuse", [])
                if contract.get("documentation_independent") is True),
            "worker_timeout": c["worker_timeout"], "verify_timeout": c["verify_timeout"],
            "worker_settings": worker_settings(c),
            **setup_report, "onboarding_complete": ready and setup_report['onboarding_complete']}


def main(argv=None):
    parser = argparse.ArgumentParser(description="SFC: track Codex chat work on GitHub Projects")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--root", default=".", help="target repository, not the plugin directory")
    sub = parser.add_subparsers(dest="command", required=True)
    setup = sub.add_parser("onboard", help="configure an existing GitHub Project")
    setup.add_argument("--repo", help="owner/repo; defaults to origin")
    setup.add_argument("--owner", required=True, help="GitHub Project owner")
    setup.add_argument("--project", type=int, required=True, help="GitHub Project number")
    setup.add_argument("--base")
    setup.add_argument("--status-field", default="SFC Status")
    setup.add_argument("--activity-timezone", default="UTC",
                       help="IANA timezone for card report times (default: UTC); receipts remain UTC")
    setup.add_argument("--verify", action="append", help="optional project check; repeat for multiple checks")
    setup.add_argument('--efficiency-profile', choices=('auto', 'sfc-python', 'sfc-python-docs', 'off'), default='auto',
                       help=argparse.SUPPRESS)
    setup.add_argument('--preview', action='store_true', help='verify the repository and Project before applying setup')
    setup.add_argument('--expected-review', help='require the same configuration and Project identity as --preview')
    setup.add_argument('--allow-merge', action='store_true',
                       help='authorize eligible automatic merges for user-requested chat tasks on the selected repository and base')
    setup.add_argument('--release-config', type=Path, help=argparse.SUPPRESS)
    setup.add_argument('--release-mode', choices=('publish', 'draft', 'off'), default='publish', help=argparse.SUPPRESS)
    setup.add_argument('--release-authorization', help=argparse.SUPPRESS)
    setup.add_argument('--review-efficiency', choices=('qualified', 'baseline'), default='qualified', help=argparse.SUPPRESS)
    setup.add_argument("--merge-policy", choices=("human", "auto"),
                       help="automatic delivery by default; human keeps an explicit merge gate")
    setup.add_argument("--merge-mode", choices=("github-protected", "local-verification"),
                       default="github-protected", help=argparse.SUPPRESS)
    setup.add_argument("--allow-no-checks", action="store_true",
                       help=argparse.SUPPRESS)
    setup.add_argument('--auto-start', action=argparse.BooleanOptionalAction, default=None,
                       help=argparse.SUPPRESS)
    setup.add_argument('--session-lifetime', action=argparse.BooleanOptionalAction, default=None,
                       help=argparse.SUPPRESS)
    setup.add_argument('--pickup', action=argparse.BooleanOptionalAction, default=True,
                       help=argparse.SUPPRESS)
    for key in ('poll_interval', 'max_seconds', 'max_polls', 'max_claims'):
        setup.add_argument('--watch-' + key.replace('_', '-'), type=int, help=argparse.SUPPRESS)
    setup.add_argument('--watch-exclude-issue', dest='watch_excluded_issues', type=int, action='append', help=argparse.SUPPRESS)
    setup.add_argument('--watch-hold-label', help=argparse.SUPPRESS)
    setup.add_argument("--model", help=argparse.SUPPRESS)
    setup.add_argument("--reasoning-effort", choices=(*REASONING_EFFORTS, "inherit"),
                       help=argparse.SUPPRESS)
    setup.add_argument("--service-tier",
                       help=argparse.SUPPRESS)
    sub.add_parser("doctor", help="read-only readiness checks")
    sub.add_parser('onboard-finish', help='verify the saved Project after dashboard repair')
    migration = sub.add_parser('efficiency-profile', help='preview/apply bounded efficiency contracts; no run authority')
    migration.add_argument('--profile', choices=('auto', 'sfc-python', 'sfc-python-docs', 'off'), default='auto')
    migration.add_argument('--apply', action='store_true')
    migration.add_argument('--expected-digest', help='exact config_digest from migration preview')
    migration.add_argument('--expected-proposed-digest', help='exact proposed_digest from migration preview')
    for entry in (setup, migration):
        entry.add_argument('--review-qualification', type=Path, help='pinned local review qualification manifest')
        entry.add_argument('--model-qualification', type=Path, help='pinned local model qualification manifest')
    defaults = sub.add_parser('setup-defaults', help='preview explicitly applicable recommendations, preserving existing opt-outs and authority')
    defaults.add_argument('--apply', action='store_true')
    defaults.add_argument('--expected-digest')
    defaults.add_argument('--expected-proposed-digest')
    defaults.add_argument('--release-config', type=Path)
    defaults.add_argument('--release-mode', choices=('publish', 'draft', 'off'))
    defaults.add_argument('--release-authorization')
    qualification = sub.add_parser('efficiency-qualification', help='evaluate retained paired delivery evidence; no promotion writes')
    qualification.add_argument('input', type=Path)
    qualification.add_argument('--adjudication', type=Path)
    dashboard_parser = sub.add_parser("dashboard", help="verify the saved factory dashboard; exit 2 if incomplete")
    dashboard_parser.add_argument("--apply", action="store_true", help="apply stage presentation, activity/Priority/Labels visibility and missing work-type labels")
    priority_parser = sub.add_parser("priority-setup", help="inspect Priority setup; explicit --apply repairs")
    priority_parser.add_argument("--apply", action="store_true")
    type_parser = sub.add_parser("type-setup", help="inspect work-type labels/Labels visibility; explicit --apply upgrades")
    type_parser.add_argument("--apply", action="store_true", help="create only missing labels and append saved Labels visibility")
    ticket_parser = sub.add_parser("create-ticket", help="supervisor: create a verified Backlog ticket with Priority and one work-type label")
    ticket_parser.add_argument("--request-id", required=True, help="stable unique ID; repeat attempts never write")
    ticket_parser.add_argument("--title", required=True)
    ticket_parser.add_argument("--body-file", required=True)
    ticket_parser.add_argument("--priority", choices=("P0", "P1", "P2", "P3"), required=True)
    ticket_parser.add_argument("--type", dest="work_type", choices=tuple(TYPES), required=True,
                               help="assessed work type; honor the user's explicit choice")
    ticket_parser.add_argument("--label", action="append", default=[], help="existing auxiliary label; repeat; qa requires explicit workflow intent")
    ticket_parser.add_argument("--reason", required=True, help="assessment with supporting evidence")
    ticket_parser.add_argument("--unknowns", required=True, help="confidence and unsupported rubric dimensions")
    tracker = sub.add_parser('track', help='record work from this chat on the configured Project')
    tracking = tracker.add_subparsers(dest='action', required=True)
    tracking.add_parser('enable', help='use chat tracking for this configured project')
    tracked_start = tracking.add_parser('start', help='queue a new chat task or link a Project issue')
    tracked_start.add_argument('--task', required=True)
    tracked_start.add_argument('--title', required=True)
    tracked_start.add_argument('--summary', required=True)
    tracked_start.add_argument('--type', dest='work_type', choices=tuple(TYPES))
    tracked_start.add_argument('--acceptance', action='append', default=[])
    tracked_start.add_argument('--issue', type=int)
    tracked_start.add_argument('--delivery', choices=chat_tracking.DELIVERY_MODES,
                               help='new tasks default to pr-required; use local-only for explicit local delivery')
    tracked_update = tracking.add_parser('update', help='queue an observed milestone and its evidence')
    tracked_update.add_argument('--task', required=True)
    tracked_update.add_argument('--event', required=True)
    tracked_update.add_argument('--stage', choices=chat_tracking.STAGES, required=True)
    tracked_update.add_argument('--summary', required=True)
    tracked_pr = tracked_update.add_mutually_exclusive_group()
    tracked_pr.add_argument('--pr', type=int, help='pull request number in the tracked issue repository')
    tracked_pr.add_argument('--pr-url', help='exact GitHub pull request URL, including from another repository')
    tracked_update.add_argument('--local-done', action='store_true')
    tracked_update.add_argument('--closes-issue', action='store_true',
                                help='the recorded PR delivers and should close the tracked issue')
    tracked_update.add_argument('--milestone-only', action='store_true', help='link partial PR progress without completing the task on its merge')
    tracked_status = tracking.add_parser('status', help='read the local issue binding and pending milestones')
    select_task = tracked_status.add_mutually_exclusive_group(required=True)
    select_task.add_argument('--task')
    select_task.add_argument('--prefix')
    tracking.add_parser('sync', help='sync one task and read back the board state').add_argument('--task', required=True)
    delivery = tracking.add_parser('merge', help='supervisor: prepare reviewed chat delivery or apply its pinned atomic merge')
    delivery.add_argument('--task', required=True)
    delivery.add_argument('--worktree', required=True, type=Path)
    delivery.add_argument('--head', required=True)
    delivery.add_argument('--base', required=True)
    choice = delivery.add_mutually_exclusive_group(required=True)
    choice.add_argument('--prepare', action='store_true', help='spend one finite preparation on actual checks and native review')
    choice.add_argument('--apply', action='store_true', help='consume qualified evidence and existing merge authority')
    release = sub.add_parser("release-request", help="create a frozen versioned release card; no publication")
    release.add_argument("--version", help="explicit version; otherwise configured plain-text version_file")
    release.add_argument("--recover-creation", action="store_true",
                         help="read back an existing creation only; requires its exact --version and writes no GitHub data")
    correction = sub.add_parser('release-recover', help='prepare a corrected merged candidate on an existing release card')
    correction.add_argument('--version', required=True)
    correction.add_argument('--candidate', required=True, help='exact merged correction commit SHA')
    correction.add_argument('--apply', action='store_true', help='authorize one corrected attempt on the retained card')
    sub.add_parser("release-status", help="read-only saved release receipts")
    delivery = sub.add_parser('delivery-report', help='offline delivery accounting from retained local receipts')
    delivery.add_argument('--run-id', help='saved run directory name (default: latest state)')
    description = sub.add_parser('release-description-edit', help='explicitly authorized published description-only edit')
    description.add_argument('--version', required=True)
    description.add_argument('--authorization-id', required=True)
    description.add_argument('--expected-body-sha256', required=True)
    description.add_argument('--body-file', required=True, help='operator-approved public Markdown; never a worker result')
    sub.add_parser("status", help="read-only board and local state")
    sub.add_parser("lint", help="read-only issue criteria checks")
    recovery = sub.add_parser('recover-worker-output', help='preview or explicitly grant one supplemental full Build dispatch')
    recovery.add_argument('--issue', type=int, required=True)
    recovery.add_argument('--request-id', required=True)
    recovery.add_argument('--reason', required=True)
    recovery.add_argument('--grant-one', action='store_true', help='explicit spending authority; preserves all old attempts and gates')
    corrective = sub.add_parser('recover-findings', help='preview/grant one corrective Build for an independently rejected saved PR')
    corrective.add_argument('--issue', type=int, required=True)
    corrective.add_argument('--request-id', required=True)
    corrective.add_argument('--reason', required=True)
    corrective.add_argument('--grant-one', action='store_true')
    corrective.add_argument('--pin-legacy-startup', action='store_true',
                            help='explicit owner exception: pin absent legacy Build startup files; no refunds or fabricated logs')
    retained = sub.add_parser('recover-ticket', help='preview/grant one explicitly authorized retained-work Build with pinned failure evidence')
    retained.add_argument('--issue', type=int, required=True)
    retained.add_argument('--request-id', required=True)
    retained.add_argument('--reason', required=True)
    retained.add_argument('--grant-one', action='store_true')
    retained.add_argument('--adopt-pr', type=int, metavar='PR',
                          help='verify a newer commit on this saved PR without running another Build')
    retained.add_argument('--head', metavar='FULL_SHA', help='exact approved PR head for --adopt-pr')
    start = sub.add_parser("run", help="drain eligible cards with finite limits")
    start.add_argument("--dry-run", action="store_true")
    start.add_argument("--include-backlog", action="store_true", help="opt in to priority pickup for explicitly scoped Backlog/Ready issues")
    start.add_argument("--issue", type=int, action="append", default=[], help="authorized issue number; repeat; required with --include-backlog")
    start.add_argument("--allow-merge", action="store_true", help="authorize configured automatic merges for this run")
    start.add_argument("--waiting", action="store_true", help="request Fast Build for one scoped, routed issue")
    watch = sub.add_parser("watch", help="explicit bounded foreground session for current and future Backlog/Ready work")
    choice = watch.add_mutually_exclusive_group()
    choice.add_argument("--start", action="store_true", help="authorize a NEW finite queue session")
    choice.add_argument("--resume", metavar="SESSION", help="resume recorded budgets and authority")
    watch.add_argument("--dry-run", action="store_true", help="read-only live queue preview; grants no authority")
    watch.add_argument("--allow-merge", action="store_true", help="separate opt-in to configured auto merge policy")
    for name in ("poll-interval", "max-seconds", "max-polls", "max-claims"):
        watch.add_argument("--" + name, type=int, default=None)
    watch.add_argument("--exclude-issue", type=int, action="append", default=None,
                       help="repository issue number; repeat; added to configured exclusions")
    watch.add_argument("--hold-label", default=None, help="hold label; default sfc-hold")
    sub.add_parser("watch-status", help="read-only recorded watcher scope, freshness and budgets")
    automatic = sub.add_parser('autostart', help='preview or explicitly authorize/disable project chat auto-start')
    choice = automatic.add_mutually_exclusive_group()
    choice.add_argument('--enable', action='store_true', help='authorize saved scope; re-arm after inspected stop/failure')
    choice.add_argument('--disable', action='store_true')
    automatic.add_argument('--allow-merge', action='store_true', help='separate saved auto-merge opt-in; requires --enable')
    automatic.add_argument('--session-lifetime', action='store_true',
                           help='with --enable, explicitly authorize worker usage in successive finite windows while the native MCP connection lives (macOS)')
    sub.add_parser('autostart-status', help='read-only local authorization and last hook event; no host trust inference')
    sub.add_parser("stop", help="request child shutdown; preserve work and evidence")
    args = parser.parse_args(argv)
    try:
        root = repo_root(args.root)
        if args.command in ('doctor', 'onboard-finish', 'dashboard', 'priority-setup', 'type-setup'):
            root = chat_tracking.primary_root(root)
        if (((args.command in ('run', 'watch') and not args.dry_run) or
             (args.command == 'autostart' and args.enable) or
             (args.command in ('recover-ticket', 'recover-findings', 'recover-worker-output') and args.grant_one))
                and chat_tracking.chat_mode(chat_tracking.project_root(root))):
            raise Halt('SFC runs work in your Codex chat. Ask Codex to continue the ticket and record its milestones with sfc track.')
        if args.command == "onboard":
            if args.preview:
                result = onboard_chat(root, args)
            else:
                with exclusive(root), creation_lock(root):
                    result = onboard_chat(root, args)
        elif args.command == 'setup-defaults':
            if args.apply:
                with exclusive(root):
                    result = setup_review.upgrade(root, GitHub, apply=True, expected_digest=args.expected_digest,
                                                  expected_proposed_digest=args.expected_proposed_digest,
                                                  release_config=args.release_config, release_mode=args.release_mode,
                                                  release_authorization=args.release_authorization)
            else:
                result = setup_review.upgrade(root, GitHub, release_config=args.release_config, release_mode=args.release_mode,
                                              release_authorization=args.release_authorization)
        elif args.command == 'efficiency-qualification':
            from sfc_lib.efficiency_benchmark import evaluate
            result = evaluate(args.input, adjudication=args.adjudication)
        elif args.command == 'efficiency-profile':
            if args.apply:
                with exclusive(root):
                    result = efficiency.migration(root, profile=args.profile, apply=True, expected_digest=args.expected_digest,
                                                  expected_proposed_digest=args.expected_proposed_digest,
                                                  explicit_profile=True,
                                                  review_qualification=args.review_qualification,
                                                  model_qualification=args.model_qualification)
            else:
                result = efficiency.migration(root, profile=args.profile,
                                              explicit_profile=True,
                                              review_qualification=args.review_qualification,
                                              model_qualification=args.model_qualification)
        elif args.command == 'onboard-finish' and chat_tracking.chat_mode(root):
            result = doctor_chat(root)
        elif args.command == 'onboard-finish':
            with exclusive(root), creation_lock(root):
                result = onboarding.finish(root, GitHub(root, load_config(root)))
        elif args.command == "dashboard":
            gh = GitHub(root, load_config(root))
            if args.apply:
                with exclusive(root), creation_lock(root):
                    gh.verify_repository()
                    result = gh.configure_dashboard(linked_prs=chat_tracking.chat_mode(root))
            else:
                result = gh.dashboard(linked_prs=chat_tracking.chat_mode(root))
        elif args.command in ("priority-setup", "type-setup"):
            gh = GitHub(root, load_config(root))
            prepare = gh.priority_setup if args.command == "priority-setup" else gh.type_setup
            if args.apply:
                with exclusive(root), creation_lock(root):
                    gh.verify_repository()
                    result = prepare(apply=True)
            else:
                result = prepare()
        elif args.command == "release-request":
            from sfc_lib.releases import Releases
            with exclusive(root):
                factory = Factory(root)
                factory.processes.shared_stop_path = common_dir(root) / "sfc-stop"
                releases = Releases(factory)
                if args.recover_creation:
                    if args.version is None:
                        raise Halt('--recover-creation requires the retained --version')
                    result = releases.reconcile_creation(args.version)
                else:
                    result = releases.request(args.version)
        elif args.command == 'release-recover':
            from sfc_lib.releases import Releases
            from sfc_lib.release_recovery import recover
            with exclusive(root):
                factory = Factory(root)
                factory.processes.shared_stop_path = common_dir(root) / "sfc-stop"
                result = recover(Releases(factory), args.version, args.candidate, apply=args.apply)
        elif args.command == 'release-description-edit':
            from sfc_lib.releases import Releases
            from sfc_lib.release_description import edit
            with exclusive(root):
                factory = Factory(root)
                factory.processes.shared_stop_path = common_dir(root) / "sfc-stop"
                result = edit(Releases(factory), args.version, args.authorization_id,
                              args.expected_body_sha256, Path(args.body_file).read_text())
        elif args.command == 'delivery-report':
            from sfc_lib.delivery import report
            result = report(root, run_id=args.run_id)
        elif args.command == "release-status":
            result = {"releases": [read_json(path) for path in sorted((common_dir(root) / "sfc-releases").glob("*.json"))]}
        elif args.command == "create-ticket":
            gh = GitHub(root, load_config(root))
            result = create_ticket(gh, root, request_id=args.request_id, title=args.title,
                                   body=Path(args.body_file).read_text(), priority=args.priority,
                                   reason=args.reason, unknowns=args.unknowns, work_type=args.work_type, labels=args.label)
        elif args.command == 'track':
            root = (chat_tracking.primary_root(root) if args.action == 'merge'
                    else chat_tracking.project_root(root))
            config = load_config(root)
            if args.action == 'enable':
                result = chat_tracking.enable(root)
            elif args.action == 'start':
                result = chat_tracking.start(root, config, task=args.task, title=args.title, summary=args.summary,
                                             work_type=args.work_type, acceptance=args.acceptance, issue=args.issue,
                                             delivery=args.delivery)
            elif args.action == 'update':
                result = chat_tracking.update(root, config, task=args.task, event_id=args.event, stage=args.stage,
                                              summary=args.summary, pr=args.pr, pr_url=args.pr_url,
                                              local_done=args.local_done, closes_issue=args.closes_issue, milestone_only=args.milestone_only)
            elif args.action == 'status':
                result = chat_tracking.status(root, config, task=args.task, prefix=args.prefix)
            elif args.action == 'merge':
                from sfc_lib.chat_merge import delivery
                result = delivery(GitHub(root, config), root, args.task, args.worktree,
                                  args.head, args.base, apply=args.apply)
            else:
                result = chat_tracking.sync(GitHub(root, config), root, args.task)
        elif args.command == "watch-status":
            result = watch_status(root)
        elif args.command == 'recover-worker-output':
            from sfc_lib.worker_recovery import recover
            with exclusive(root):
                result = recover(Factory(root), issue=args.issue, request_id=args.request_id,
                                 reason=args.reason, grant_one=args.grant_one)
        elif args.command == 'recover-findings':
            from sfc_lib.corrective_recovery import grant
            with exclusive(root):
                result = grant(Factory(root), issue=args.issue, request_id=args.request_id,
                               reason=args.reason, grant_one=args.grant_one, pin_legacy_startup=args.pin_legacy_startup)
        elif args.command == 'recover-ticket':
            with exclusive(root):
                if (args.adopt_pr is None) != (args.head is None):
                    raise Halt('--adopt-pr and --head must be supplied together')
                if args.adopt_pr is not None:
                    from sfc_lib.pr_adoption import recover
                    result = recover(Factory(root), issue=args.issue, pr_number=args.adopt_pr,
                                     candidate=args.head, request_id=args.request_id,
                                     reason=args.reason, apply=args.grant_one)
                else:
                    from sfc_lib.ticket_recovery import grant
                    result = grant(Factory(root), issue=args.issue, request_id=args.request_id,
                                   reason=args.reason, grant_one=args.grant_one)
        elif args.command == 'autostart-status':
            result = autostart.status(root)
        elif args.command == 'autostart':
            root = autostart.configured_root(root)
            if args.allow_merge and not args.enable:
                raise Halt('--allow-merge requires explicit autostart --enable')
            if args.session_lifetime and not args.enable:
                raise Halt('--session-lifetime requires explicit autostart --enable')
            if args.enable or args.disable:
                with exclusive(root):
                    result = autostart.configure(Factory(root), enabled=args.enable, allow_merge=args.allow_merge,
                                                 lifetime=args.session_lifetime)
            else:
                result = {**autostart.status(root), 'proposed_scope': autostart.summary(Factory(root))}
        elif args.command == "stop":
            path = common_dir(root) / "sfc-stop"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
            # Preserve interoperability with supervisors predating shared stop.
            legacy = root / ".sfc/stop"
            legacy.parent.mkdir(parents=True, exist_ok=True)
            legacy.touch()
            result = {"stop_requested": True, "detail": "Supervisor observes this flag and terminates owned child process groups; no unrelated process is signalled"}
        elif args.command == "doctor":
            result = doctor_chat(root) if chat_tracking.chat_mode(root) else doctor(root)
        else:
            factory = Factory(root)
            if args.command in ("run", "watch"):
                stop_path = common_dir(root) / "sfc-stop"
                def request_stop(signum, frame):
                    stop_path.touch()
                if not args.dry_run:
                    signal.signal(signal.SIGTERM, request_stop)
                    signal.signal(signal.SIGINT, request_stop)
                if args.command == "watch":
                    overrides = {key: getattr(args, key) for key in
                                 ("poll_interval", "max_seconds", "max_polls", "max_claims", "hold_label")
                                 if getattr(args, key) is not None}
                    if args.exclude_issue is not None:
                        overrides["excluded_issues"] = sorted(set(factory.c.get("watch", {}).get("excluded_issues", []) + args.exclude_issue))
                    result = factory.watch(start=args.start, resume=args.resume, dry_run=args.dry_run,
                                           allow_merge=args.allow_merge, overrides=overrides)
                else:
                    result = factory.run(allow_merge=args.allow_merge, dry_run=args.dry_run,
                                         include_backlog=args.include_backlog, issues=args.issue, waiting=args.waiting)
            else:
                cards = factory.gh.cards()
                if args.command == "lint":
                    result = {"issues": [{"number": v["number"], "problems": lint_card(v)} for v in cards
                                         if v["status"] in ("Ready", "Building", "QA", "Review")
                                         and v["state"] == "OPEN"]}
                else:
                    state = factory.state
                    result = {"columns": {s: sum(v["status"] == s for v in cards) for s in STATUSES},
                              "efficiency": efficiency.status(factory.c, root=root, state=factory.state),
                              "cards": [{**{k: v[k] for k in ("number", "title", "status", "url")},
                                         "blocker": state.get("cards", {}).get(str(v["number"]), {}).get("blocker"),
                                         "blocked_recoveries": state.get("cards", {}).get(str(v["number"]), {}).get("blocked_recoveries", []),
                                         "recovery_wait": state.get("cards", {}).get(str(v["number"]), {}).get("recovery_wait"),
                                         "recorded_activity": state.get("cards", {}).get(str(v["number"]), {}).get("activity", {}).get("values"),
                                         "review_obligations": review_policy.describe(factory, v),
                                         "review_route": (state.get("cards", {}).get(str(v["number"]), {}).get("review_route")
                                                          if factory.c.get("review_policy") is not None else None),
                                         "review_reuse_gaps": (state.get("cards", {}).get(str(v["number"]), {}).get("review_reuse_gaps", [])
                                                               if factory.c.get("review_policy") is not None else [])}
                                        for v in cards],
                              "run": {k: state.get(k) for k in ("run_id", "outcome", "started", "finished", "error",
                                                              "explanation", "release_trigger")},
                              "delivery_report": state.get('delivery_report'),
                              "waiting": [{"issue": w["issue"], "explanation": w.get("explanation")}
                                          for w in state.get("waiting", [])],
                              "stop_requested": factory.processes.stopped() or (common_dir(root) / "sfc-stop").exists(),
                              "watcher": watch_status(root),
                              "priority_setup": factory.gh.priority_setup(),
                              "type_setup": factory.gh.type_setup()}
        print(json.dumps(result, indent=2))
        if args.command == "lint" and any(issue["problems"] for issue in result["issues"]):
            return 1
        if args.command in ("dashboard", "priority-setup", "type-setup") and not result["ready"]:
            return 2
        if args.command == "doctor" and not result["ready"]:
            return 1
        return 0
    except (Halt, OSError, ValueError, KeyError) as exc:
        print(json.dumps({"error": str(exc), "detail": "Preserve worktrees and evidence; inspect remote state before retrying writes"}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
