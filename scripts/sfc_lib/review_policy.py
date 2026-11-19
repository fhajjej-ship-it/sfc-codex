"""Pinned review obligations; model output cannot reduce supervisor policy."""
from __future__ import annotations

import datetime as dt
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
from uuid import UUID

from . import worker_runtime
from .common import Halt, PACKAGE, git, read_json, run
from .ticket_validation import acceptance_criteria
from .verification import checksum, digest, regular_file

CATEGORIES = {
    'authentication': ['auth/**', '**/auth/**', '*permission*', '**/*permission*', '**/*oauth*'],
    'secrets': ['.env*', '**/.env*', '*secret*', '**/*secret*', '**/*credential*', '*.pem'],
    'money': ['billing/**', '**/billing/**', 'payments/**', '**/payments/**', '**/*payment*'],
    'data': ['migrations/**', '**/migrations/**', '*migration*', '**/*migration*', '**/*schema*'],
    'publication': ['.github/**', '*release*', '**/*release*', '**/*publish*', '**/*deploy*', '**/*package*'],
    'control': ['AGENTS.md', '**/AGENTS.md', 'AGENTS.override.md', '**/AGENTS.override.md',
                '.codex/**', '.agents/**', '.sfc/**',
                '*factory*', '*merge*', '*recovery*', '*review_policy*',
                '*verification*', '*scheduler*', '*watch*', '*autostart*'],
}
TOKENS = {
    'authentication': r'\b(auth|oauth|permission|authorize|authentication|password|session_token|is_?admin)\w*',
    'secrets': r'\b(secret|credential|private_key|api_key)\w*',
    'money': r'\b(payment|billing|invoice|refund|charge|money)\w*',
    'data': (r'\b(DROP|TRUNCATE|DELETE FROM|delete_?(?:many|all)|destroy_?all|migration|migrate|destructive)\b'
             r'|\.\s*(?:delete|destroy)\s*\('),
    'publication': r'\b(?:(?:publish|publication|release|deploy)\w*|(?:GitHub|prior) Latest)\b',
    'control': r'\b(merge_gate|allow_merge|updateRefs|recovery|supervisor)\b',
}
SENSITIVE_LABELS = {'auth', 'security', 'billing', 'payments', 'money', 'schema', 'destructive', 'migration'}
SENSITIVE_PATHS = ('auth/**', '**/auth/**', '**/payments/**', 'payments/**', 'billing/**', '**/billing/**',
                   '**/migrations/**', 'migrations/**', '**/migrate/**', 'drizzle/**',
                   '.github/**', '.codex/**', '.agents/**', '.sfc/**', 'AGENTS.md',
                   '**/AGENTS.md', '.gitmodules')
LANES = ('qa', 'security', 'review')
ROUTINE_TEXT_SUFFIXES = {'.md', '.rst', '.txt'}
CODE_MARKUP = re.compile(
    r'(?m)`|~~~|^[ \t]*\.\.[ \t]+\w[\w-]*::|::[ \t]*$|^(?: {4}|\t)\S'
    r'|#!|<[%!?a-zA-Z]|[{};=\x00]|\w\s*\('
    r'|^[ \t]*(?:def|class|import|from|return|export|sudo|rm|rmdir|find|xargs|exec)[ \t]+'
)
NATIVE_REPORT = 'SFC_REVIEW_V1 '
MAX_NATIVE_BYTES = 16 * 1024 * 1024
SOURCE_QA_EXAMINATION = (
    'Start from the changed function and its existing tests. Use the nearest callable entry point to '
    'probe the behavior before expanding the investigation; a direct call is sufficient when it reproduces '
    'the real contract. Follow changed data into its next consumers. For a changed input contract, compare '
    'the relevant paths that independently interpret the same input. Compare the pinned base and candidate at affected boundaries, '
    'including return values, errors, state changes and the order or count of observable operations where '
    'relevant. Use focused write-free probes for material gaps. Continue assessing the affected contract '
    'after finding a defect. Report all confirmed defects and unresolved evidence gaps. '
    'Request security or human review for new or uncertain security, privacy or authorization boundaries.'
)


def settings(c):
    return c.get('review_policy')


def validate(c):
    p = settings(c)
    if p is None:
        return
    if not isinstance(p, dict) or set(p) - {'mode', 'providers', 'mandatory', 'risk_rules',
                                           'routine_paths', 'routine_candidate', 'max_age_seconds', 'promotion', 'cohort', 'imports',
                                           'github_reviewer_ids', 'behavioral_contract', 'inspection',
                                           'correction_reuse'}:
        raise Halt('Unsupported review_policy configuration')
    if p.get('mode', 'baseline') not in ('baseline', 'risk-based'):
        raise Halt('review_policy.mode must be baseline or risk-based')
    providers = p.get('providers', {})
    if (not isinstance(providers, dict) or set(providers) - set(LANES) or
            any(v not in ('codex-exec', 'codex-native') for v in providers.values()) or
            any(providers.get(k, 'codex-exec') != 'codex-exec' for k in ('qa', 'security'))):
        raise Halt('Review obligations require one supported provider; native supports review only')
    if p.get('mode') == 'risk-based' and providers.get('review') != 'codex-native':
        raise Halt('risk-based policy requires independent native review and coverage evidence')
    mandatory = p.get('mandatory', [])
    rules = p.get('risk_rules', {})
    lists = [mandatory, p.get('routine_paths', [])]
    if not isinstance(rules, dict) or set(rules) - set(CATEGORIES):
        raise Halt('Unknown minimum review risk category')
    lists.extend(rules.values())
    if any(not isinstance(v, list) or any(not isinstance(s, str) or not s.strip() or '\0' in s for s in v)
           for v in lists) or not set(mandatory) <= {'qa', 'security', 'human'}:
        raise Halt('Invalid review minimum rules')
    if type(p.get('max_age_seconds', 86400)) is not int or not 0 < p.get('max_age_seconds', 86400) <= 86400:
        raise Halt('Review evidence age must be finite and at most one day')
    if 'routine_candidate' in p:
        candidate = p['routine_candidate']
        if (p.get('mode') != 'risk-based' or not isinstance(candidate, dict) or
                set(candidate) != {'head', 'base', 'task'} or
                any(not isinstance(candidate[k], str) or
                    not re.fullmatch('[0-9a-f]{64}' if k == 'task' else '[0-9a-f]{40}|[0-9a-f]{64}',
                                     candidate[k]) for k in candidate)):
            raise Halt('Routine candidate attestation requires exact head, base and task digests')
    if 'promotion' in p:
        validate_ref(p['promotion'])
    if 'cohort' in p:
        from .review_cohort import sha256
        if (not sha256(p['cohort']) or p.get('mode') != 'risk-based' or
                set(p) - {'mode', 'providers', 'behavioral_contract', 'cohort', 'inspection'}):
            raise Halt('Bundled review cohort requires its exact behavioral policy and certificate SHA256')
    if 'behavioral_contract' in p:
        contract = p['behavioral_contract']
        if (p.get('mode') != 'risk-based' or not isinstance(contract, dict) or
                set(contract) != {'paths', 'check_ids', 'instructions'} or
                not isinstance(contract['paths'], list) or not 1 <= len(contract['paths']) <= 32 or
                any(not review_path(v) for v in contract['paths']) or
                len(set(contract['paths'])) != len(contract['paths']) or
                not isinstance(contract['check_ids'], list) or not contract['check_ids'] or
                any(type(i) is not int or not 1 <= i <= len(c['verify_commands']) for i in contract['check_ids']) or
                len(set(contract['check_ids'])) != len(contract['check_ids']) or
                not isinstance(contract['instructions'], dict) or len(contract['instructions']) > 32 or
                any(not review_path(v) or Path(v).name not in ('AGENTS.md', 'AGENTS.override.md') or
                    not isinstance(sha, str) or not re.fullmatch('[0-9a-f]{64}', sha)
                    for v, sha in contract['instructions'].items())):
            raise Halt('Behavioral review requires exact paths, configured check IDs and pinned repository instructions')
    from .review_retention import validate as validate_retention
    validate_retention(c, p)
    if 'inspection' in p:
        inspection = p['inspection']
        if (not isinstance(inspection, dict) or 'enabled' not in inspection or
                set(inspection) - {'enabled', 'promotion'} or not isinstance(inspection['enabled'], list) or
                len(inspection['enabled']) > 2 or any(v not in ('source', 'release') for v in inspection['enabled']) or
                len(set(inspection['enabled'])) != len(inspection['enabled'])):
            raise Halt('Review inspection must name explicit source and/or release profiles')
        if 'promotion' in inspection:
            validate_ref(inspection['promotion'])
    imports = p.get('imports', [])
    if not isinstance(imports, list) or len(imports) > 2:
        raise Halt('At most two pinned review imports may be inspected; no cloud polling')
    for ref in imports:
        # Configure stable inbox paths before execution. Hashing an envelope in
        # the config it must itself echo would create a circular config digest.
        if (not isinstance(ref, dict) or set(ref) != {'path'} or
                not isinstance(ref['path'], str) or not ref['path'].strip()):
            raise Halt('Review import must name a supervisor evidence inbox path')
    actors = p.get('github_reviewer_ids', [])
    if not isinstance(actors, list) or any(type(v) is not int or v <= 0 for v in actors):
        raise Halt('GitHub review reuse requires explicitly configured bot IDs')


def validate_ref(ref):
    if (not isinstance(ref, dict) or set(ref) != {'path', 'sha256'} or
            not isinstance(ref['path'], str) or not ref['path'] or
            not isinstance(ref['sha256'], str) or not re.fullmatch('[0-9a-f]{64}', ref['sha256'])):
        raise Halt('Review evidence requires an exact path and SHA256')


def review_path(value):
    return (isinstance(value, str) and 0 < len(value) <= 240 and
            all(part not in ('', '.', '..') for part in value.split('/')) and
            not any(char in value for char in '\\*?[]\0\n\r'))


def load_ref(root, ref):
    path = Path(ref['path'])
    path = path if path.is_absolute() else Path(root) / path
    if checksum(path) != ref['sha256']:
        raise Halt('Review evidence hash changed')
    return read_json(path)


def criteria(card):
    return [{'id': digest([i, text])[:16], 'text': text}
            for i, text in enumerate(acceptance_criteria(card.get('body')))]


def sensitive_path(path):
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in SENSITIVE_PATHS)


def risk(c, card, paths, diff='', *, opaque=False, expected_empty=False, behavioral=False):
    p = settings(c) or {}
    rules = p.get('risk_rules', {})
    labels = {label.lower() for label in card.get('labels', [])}
    risk_text = ' '.join(sorted(labels)) + '\n' + diff
    if behavioral:
        # The attested path is only a ceiling on eligible scope. A repository's
        # stated behavior can require a specialist even when that path fits.
        risk_text += '\n' + card.get('title', '') + '\n' + card.get('body', '')
    categories = sorted(k for k, patterns in CATEGORIES.items() if
                        any(fnmatch.fnmatchcase(name, glob) for name in paths
                            for glob in patterns + rules.get(k, [])) or
                        re.search(TOKENS[k], risk_text, re.I))
    if labels & SENSITIVE_LABELS or any(sensitive_path(name) for name in paths):
        categories.append('merge-sensitive')
    # A routine glob scopes ordinary documentation/text. A path match and
    # missing risk tokens cannot classify the behavior of source or config.
    if (opaque or (not expected_empty and (not paths or not diff)) or
            (not behavioral and any(Path(name).suffix.lower() not in ROUTINE_TEXT_SUFFIXES or
                 not any(fnmatch.fnmatchcase(name, glob) for glob in p.get('routine_paths', [])) for name in paths))):
        categories.append('unknown')
    return categories


def prose_only(wt, revisions, paths):
    """Examine both blobs, including context outside the changed lines."""
    for path in paths:
        if Path(path).suffix.lower() not in ROUTINE_TEXT_SUFFIXES:
            return False
        found = False
        for revision in revisions:
            entry = run(['git', '--literal-pathspecs', 'ls-tree', '-z', revision, '--', path], wt).stdout
            if not entry:
                continue
            found = True
            if not entry.startswith('100644 blob ') or not entry.endswith('\t' + path + '\0'):
                return False
            blob = entry.split('\t', 1)[0].split()[2]
            size = int(git(wt, 'cat-file', '-s', blob))
            if size > MAX_NATIVE_BYTES:
                return False
            try:
                content = run(['git', 'cat-file', 'blob', blob], wt).stdout
            except UnicodeDecodeError:
                return False
            if CODE_MARKUP.search(content):
                return False
        if not found:
            return False
    return True


def promotion_binding(c):
    # Bind the entire effective project configuration, including model/tier,
    # merge choices and check commands. Candidate attestations are independent
    # of policy promotion and remain bound by the live config digest.
    return digest({**c, 'review_policy': comparison_policy(c)})


def runtime_binding(package=PACKAGE):
    """Qualify the actual reviewer, check, scope and prompt implementation."""
    paths = [
        *(f'scripts/sfc_lib/{name}.py' for name in
          ('common', 'factory', 'review_policy', 'review_inspection', 'review_retention',
           'verification', 'efficiency_runtime', 'worker_context', 'worker_runtime', 'review_cohort',
           'release_qa_context', 'releases', 'ticket_validation')),
        'schemas/lane-result.json', 'schemas/review-result.json', 'schemas/release-review-result.json',
        'skills/sfc-qa/RELEASE.md',
        *(f'skills/sfc-{lane}/SKILL.md' for lane in LANES),
    ]
    return digest({path: checksum(Path(package) / path) for path in paths})


def comparison_policy(c):
    return {k: v for k, v in settings(c).items()
            if k not in ('promotion', 'cohort', 'imports', 'routine_candidate', 'inspection')}


def model_settings(c):
    return {**{k: c.get(k) for k in ('model', 'reasoning_effort', 'service_tier')},
            'models': c.get('models', {})}


def explicit_review_settings(c):
    models = c.get('models', {})
    return (all(models.get(lane) or c.get('model') for lane in LANES) and
            bool(c.get('reasoning_effort')) and bool(c.get('service_tier')))


def promoted(f, *, wt=None, base=None):
    p = settings(f.c)
    if p.get('mode', 'baseline') != 'risk-based':
        return False, 'baseline retained; consolidation has not been requested'
    if 'promotion' not in p and 'cohort' not in p:
        return False, 'baseline retained; no pinned real-model comparison evidence'
    if not explicit_review_settings(f.c):
        return False, 'baseline retained; review models, reasoning effort and service tier must be explicit'
    if 'cohort' in p:
        from .review_cohort import qualify
        return qualify(f.root, f.c, wt=wt, base=base)
    try:
        from .review_benchmark import evaluate
        report = load_ref(f.root, p['promotion'])
        decision = evaluate(report)
        if (decision['promote'] and (not p.get('behavioral_contract') or
                                     report.get('runtime_sha256') == runtime_binding()) and
                report['policy_sha256'] == promotion_binding(f.c) and
                report['target_policy'] == comparison_policy(f.c) and
                report['model_settings'] == model_settings(f.c) and
                report['cli_version'] == exec_provenance(None)['version']):
            return True, 'pinned real-model evaluation satisfies predeclared promotion criteria'
        return False, 'baseline retained; evaluation failed quality, coverage, timing or policy binding'
    except (OSError, ValueError, KeyError, TypeError, Halt):
        return False, 'baseline retained; promotion evidence unavailable, changed or incomplete'


def routine_attested(f, card, head, base):
    return settings(f.c).get('routine_candidate') == {
        'head': head, 'base': base, 'task': f.task_digest(card)}


def behavioral_match(f, card, wt, head, base, paths, instruction_paths):
    policy = settings(f.c)
    contract = policy.get('behavioral_contract')
    if (not contract or not paths or
            ('routine_candidate' in policy and not routine_attested(f, card, head, base)) or
            not set(paths) <= set(contract['paths']) or set(instruction_paths) != set(contract['instructions'])):
        return False
    try:
        for path, sha256 in contract['instructions'].items():
            target = Path(wt) / path
            if target.is_symlink() or not target.is_file() or checksum(target) != sha256:
                return False
        criteria(card)
    except (OSError, ValueError, Halt):
        return False
    return True


def plan(f, card, wt, base, *, evaluation=False, consider_inspection=True):
    p = settings(f.c)
    head = git(wt, 'rev-parse', 'HEAD')
    paths = [n for n in run(['git', 'diff', '--no-renames', '--name-only', '-z', base, head],
                           wt).stdout.split('\0') if n]
    options = ['diff', '--no-ext-diff', '--no-textconv', '--no-renames']
    diff = git(wt, *options, base, head)
    stats = git(wt, *options, '--numstat', '-z', base, head)
    raw = git(wt, *options, '--raw', '-z', base, head).split('\0')
    # Require ordinary non-executable files with changed text for a routine
    # route. Mode-only changes, binaries, symlinks and submodules are opaque.
    opaque = (any(row.startswith(('-\t', '0\t0\t')) for row in stats.split('\0')) or
              any(not set(row[1:].split()[:2]) <= {'000000', '100644'}
                  for row in raw[::2] if row))
    qa_only = 'qa' in card.get('labels', [])
    expected_empty = qa_only and head == base
    instruction_paths = [name for name in git(wt, 'ls-files', '-z').split('\0')
                         if Path(name).name in ('AGENTS.md', 'AGENTS.override.md')]
    behavioral = behavioral_match(f, card, wt, head, base, paths, instruction_paths)
    cats = risk(f.c, card, paths, diff, opaque=opaque, expected_empty=expected_empty, behavioral=behavioral)
    # Names, content heuristics and benchmark success cannot establish that a
    # specific repository never interprets these bytes as runtime inputs.
    if (not cats and not behavioral and not expected_empty and p.get('mode') == 'risk-based' and
            not routine_attested(f, card, head, base)):
        cats.append('unknown')
    if not cats and not behavioral and not prose_only(wt, (base, head), paths):
        cats.append('unknown')
    # Do not infer that prose review requirements have been discharged by a
    # config allowlist. Any tracked instruction document keeps the thorough
    # route; reviewers still follow its actual requirements and may escalate.
    if not expected_empty and instruction_paths and not behavioral:
        cats = sorted(set(cats) | {'repository-instructions'})
    if evaluation and p.get('mode') == 'risk-based':
        consolidated, reason = True, 'evaluating proposed policy in a temporary repository'
    else:
        consolidated, reason = promoted(f, wt=wt, base=base)
    if consolidated and cats:
        consolidated, reason = False, 'minimum risk requires QA, security and human review'
    if f.record(card).get('release_evidence') or qa_only:
        consolidated, reason = False, 'release and QA-only obligations retain all three lanes'
    elif (f.record(card).get('qa_repair') or {}).get('candidate'):
        consolidated, reason = False, 'saved QA findings require independent rechecking'
    reduced_behavioral = consolidated and behavioral
    review_first = consolidated and not reduced_behavioral
    obligations = {'review'} | set(p.get('mandatory', []))
    if reduced_behavioral:
        # Do not collapse acceptance/behavior checks and regression review into
        # a single decision. Only the default security lane is conditional.
        obligations.add('qa')
        reason = 'qualified behavioral contract: independent QA and final review; security if required'
    if not consolidated or cats:
        obligations |= {'qa', 'security'}
    if cats or any(label.lower() == 'human-only' for label in card.get('labels', [])):
        obligations.add('human')
    pins = {'head': head, 'base': base, 'config_digest': f.config_digest,
            'task_digest': f.task_digest(card), 'policy_sha256': digest(p)}
    previous = f.record(card).get('review_route') or {}
    escalations = {}
    if all(previous.get(k) == v for k, v in pins.items()):
        escalations = {key: previous[key] for key in ('import_escalations', 'specialist_escalations')
                       if previous.get(key)}
    provisional = None
    if (reduced_behavioral and consider_inspection and obligations == {'qa', 'review'} and
            not p.get('mandatory') and not p.get('imports') and
            not f.record(card).get('correction_origin')):
        from .review_inspection import final_reference, qualified
        candidate = final_reference(f, card, wt, head, base, role='behavioral-qa')
        if candidate and candidate['result']['verdict'] == 'pass' and (
                evaluation or qualified(f, 'source', wt=wt, revision=base)):
            provisional = candidate['receipt_sha256']
            # A separate final review of the completed checks must close this
            # obligation. A fresh QA lane still runs if the reviewer requests it.
            obligations.remove('qa')
            reason = 'qualified behavioral contract: provisional QA and final assessment of completed checks'
    for reports in escalations.values():
        for report in reports.values():
            obligations.update(requested_obligations(report['result']))
    providers = {lane: p.get('providers', {}).get(lane, 'codex-exec') for lane in LANES}
    if 'cohort' in p and not reduced_behavioral:
        providers['review'] = 'codex-exec'
    return {**pins, **escalations,
            'risk': cats, 'obligations': sorted(obligations),
            **({'behavioral_check_ids': p['behavioral_contract']['check_ids']} if reduced_behavioral else {}),
            **({'provisional_qa': provisional} if provisional else {}),
            'providers': providers,
            'consolidated': consolidated, 'review_first': review_first, 'reason': reason,
            'next_actions': (['Final review of completed checks and provisional QA'] if provisional else
                             ['QA, then independent final review'] if reduced_behavioral else
                             ['independent native review' if review_first else 'QA, security and independent review'])}


def provider_for(f, card, lane, head, base):
    policy = settings(f.c)
    if lane != 'review' or 'cohort' not in policy:
        return policy.get('providers', {}).get(lane, 'codex-exec')
    # Live Review entry requalifies the route. Frozen reconciliation retains
    # the exact provider in the previously validated, immutable merge scope.
    route = f.record(card).get('review_route') or {}
    if (route.get('head'), route.get('base'), route.get('config_digest'), route.get('task_digest'),
            route.get('policy_sha256')) != (head, base, f.config_digest, f.task_digest(card), digest(policy)):
        raise Halt('Cohort review route is missing or stale; replan required obligations before dispatch')
    provider = route.get('providers', {}).get(lane)
    if provider not in ('codex-exec', 'codex-native'):
        raise Halt('Cohort review route has no supported provider; replan required obligations before dispatch')
    return provider


def scope(f, card, head, base, evidence, lane, *, inspection=None):
    # The immutable execution identity survives a final verification wrapper
    # refresh when Stage 1 reuses exactly the original executions.
    from .verification import original
    from .review_retention import scope_binding
    executions = [original(v)[1]['receipt_sha256'] for v in evidence['checks']]
    if inspection is not None:
        executions = {'checks': executions, 'inspection': inspection['receipt_sha256']}
    return {'head': head, 'base': base, 'config': f.config_digest, 'task': f.task_digest(card),
            'policy': digest(settings(f.c)), 'verification': digest(executions),
            'obligation': lane, 'review_dependency': review_dependency(f, card, lane),
            **scope_binding(f, card, head, base)}


def review_dependency(f, card, lane):
    if lane not in ('qa', 'security'):
        return ''
    reports = review_requests(f.record(card))
    hashes = sorted(sha for sha, report in reports.items()
                    if lane in requested_obligations(report['result']) and
                    (report['lane'] != lane or (lane == 'qa' and coverage_gaps(report['result']))))
    if len(hashes) > 1:
        return digest(hashes)
    return hashes[0] if hashes else ''


def review_requests(record):
    route = record.get('review_route') or {}
    correction = route.get('correction') or {}
    criteria_ids = correction.get('kept', {}).keys() | correction.get('invalidated', {}).keys()
    reports = {}
    for sha, report in correction.get('pending_requests', {}).items():
        # An edited task may replace a criterion ID. Keep the archived request
        # in the correction prompt, and enforce coverage for current IDs.
        result = report['result']
        current = {**result, 'coverage': [row for row in result.get('coverage', [])
                                          if row['criterion'] in criteria_ids]}
        if requested_obligations(current):
            reports[sha] = {'lane': report['lane'], 'result': current}
    reports.update({sha: {'lane': 'review', 'result': v['result']}
                    for sha, v in route.get('import_escalations', {}).items()})
    reports.update(route.get('specialist_escalations', {}))
    for lane, receipt in record.get('receipts', {}).items():
        if lane in LANES and receipt.get('result_sha256'):
            reports[receipt['result_sha256']] = {'lane': lane, 'result': receipt['result']}
    return reports


def retain_local_escalation(route, receipt):
    additions = requested_obligations(receipt['result'])
    if additions:
        # Replacement of any lane's receipt cannot withdraw a request.
        route.setdefault('specialist_escalations', {})[receipt['result_sha256']] = {
            'lane': receipt['lane'], 'result': receipt['result']}
        route['obligations'] = sorted(set(route['obligations']) | additions)


def specialist_dependencies_complete(f, card):
    receipts = f.record(card).get('receipts', {})
    return all(receipts.get(lane, {}).get('result', {}).get('scope', {}).get('review_dependency') ==
               review_dependency(f, card, lane) for lane in required(f, card) if lane != 'review')


PROMPT_SOURCE_BYTES = 8192
PROMPT_CHECK_BYTES = 4096
PROMPT_LOG_BYTES = 4096


def compact_context(wt, head, base, evidence, *, check_ids=(), include_check_output=False):
    """Small, complete context for the same independent assessment.

    Inline only committed ordinary UTF-8 source and bounded definitions of
    checks assigned to behavioral coverage.
    Larger or special inputs use the usual exact checkout and original logs.
    None of this material substitutes for receipt or source validation.
    """
    options = ['diff', '--no-ext-diff', '--no-textconv', '--no-renames']
    try:
        paths = git(wt, *options, '--name-only', '-z', base, head).rstrip('\0').split('\0')
        if not 1 <= len(paths) <= 4 or not all(review_path(path) for path in paths):
            return ''
        remaining = PROMPT_SOURCE_BYTES
        for revision in (base, head):
            entries = git(wt, '--literal-pathspecs', 'ls-tree', '-lz', revision, '--', *paths).rstrip('\0')
            for row in entries.split('\0') if entries else []:
                mode, kind, _, size = row.split('\t', 1)[0].split()
                if mode != '100644' or kind != 'blob' or int(size) > remaining:
                    return ''
                remaining -= int(size)
        native_source(wt, head, base)
        diff = run(['git', *options, '--full-index', base, head], wt).stdout
        if not diff or len(diff.encode()) > PROMPT_SOURCE_BYTES or 'Binary files ' in diff:
            return ''
    except (OSError, ValueError, Halt):
        return ''

    definitions = {}
    outputs = {}
    log_budget = PROMPT_LOG_BYTES
    remaining = PROMPT_CHECK_BYTES
    for index in sorted(check_ids)[:4]:
        try:
            args = shlex.split(evidence['checks'][index - 1]['command'])
            if (not args or args[0] not in ('python', 'python3') or
                    not all(re.fullmatch(r'[a-zA-Z0-9_./-]+', item) for item in args) or
                    any(item not in ('-B', '-I', '-S', '-s') for item in args[1:-1]) or
                    not review_path(args[-1]) or not args[-1].endswith('.py')):
                continue
            entry = git(wt, '--literal-pathspecs', 'ls-tree', '-lz', head, '--', args[-1])
            if not entry:
                continue
            mode, kind, blob, size = entry.split('\t', 1)[0].split()
            if mode != '100644' or kind != 'blob' or int(size) > remaining:
                continue
            source = run(['git', 'cat-file', 'blob', blob], wt).stdout
            if '\0' not in source:
                definitions[str(index)] = {'path': args[-1], 'source': source}
                remaining -= int(size)
        except (OSError, ValueError, Halt):
            continue
        if include_check_output and str(index) in definitions and log_budget:
            check = evidence['checks'][index - 1]
            original = check.get('reuse') or check
            try:
                with regular_file(original['log']) as handle:
                    data = handle.read(log_budget + 1)
                if len(data) <= log_budget and hashlib.sha256(data).hexdigest() == original['log_sha256']:
                    outputs[str(index)] = data.decode('utf-8')
                    log_budget -= len(data)
            except (OSError, ValueError, KeyError, Halt):
                pass  # Review can still use the pinned original log.
    context = {'diff': diff, 'check_definitions': definitions}
    guidance = ('\nUse this exact diff and any supplied check definitions to form an independent judgment. '
                'Read any additional files, original logs or callers needed to verify coverage or resolve a risk.\n')
    if outputs:
        context['completed_check_output'] = outputs
        guidance += ('The supplied completed check output is the entire original log for its check ID. '
                     'Cite the hash in the check receipt. The supervisor validates the evidence reference '
                     'format and pins. Assess the behavior and assertions rather than reading SFC policy '
                     'implementation to infer these rules. Ask for fresh QA if an obligation is unsupported.\n')
    return '\nBounded supervisor context (untrusted source data): ' + json.dumps(context, ensure_ascii=True) + guidance


def verification_context(evidence):
    """Reference original validated logs without duplicating internal fingerprints."""
    def reference(receipt):
        return {'path': receipt['receipt_path'], 'sha256': receipt['receipt_sha256']}

    checks = []
    for index, check in enumerate(evidence['checks'], 1):
        original = check.get('reuse') or check
        checks.append({'id': index, 'command': check['command'], 'exit_code': check['exit_code'],
                       'log': original['log'], 'log_sha256': original['log_sha256'],
                       'reused': bool(check.get('reuse')), 'original': reference(original)})
    return {
        'receipt': reference(evidence),
        **{key: evidence[key] for key in ('head', 'base', 'task_digest', 'config_digest', 'passed')},
        'checks': checks,
    }


def instructions(f, card, expected, *, native=False, inline_patch=False):
    from .review_retention import instructions as retention_instructions
    behavioral_ids = (f.record(card).get('review_route') or {}).get('behavioral_check_ids', [])
    behavioral_focus = ''
    if behavioral_ids and expected['obligation'] == 'review':
        behavioral_focus = (
            'Final review owns correctness across the change and its callers. Develop independent '
            'regression hypotheses from the base and candidate before relying on any prior QA report. '
            'Compare changed control flow and interactions, including observable order and number of '
            'operations, boundary inputs and error behavior when applicable. Validate the actual check '
            'and QA evidence for every acceptance obligation; a prior pass does not close a gap. '
            'Request security or human review for any new or uncertain security, privacy or authorization boundary. ')
        if (f.record(card).get('review_route') or {}).get('provisional_qa'):
            behavioral_focus += (
                'Provisional QA independently examined source and behavior before checks completed. '
                'Resolve its observations using actual completed checks and your own assessment. '
                'Map every criterion to valid evidence; request fresh QA for any unresolved test gap. ')
    return ('\nReview policy: ' + json.dumps(settings(f.c)) +
            '\nExact scope (echo unchanged): ' + json.dumps(expected) +
            '\nAcceptance obligations: ' + json.dumps(criteria(card)) +
            '\nFor EVERY criterion supply a coverage row with criterion (the exact ID), '
            'status (covered or qa_required), reason (concrete reasoning), and evidence (an array of references). '
            'Execution evidence uses kind=check, id=quoted 1-based configured check index (for example, "1"), sha256=raw log hash. ' +
            ('Label a focused read-only reproduction by starting its command with the comment '
             '"# sfc-qa-proof: boundary-check" followed by a newline and the actual probe. Use a unique '
             'lowercase label of at most 64 letters, digits or hyphens, starting with a letter. Cite it '
             'with kind=qa-command, id="proof:boundary-check", sha256=empty string. Reuse that reference '
             'in your report; do not rerun a successful probe merely to shorten its citation. An unlabeled '
             'command can still be cited using its exact submitted text as the id. The supervisor '
             'matches one successful execution in the raw CLI event log and binds its '
             'bytes to this receipt. A reproduction of a defect can exit successfully after asserting '
             'the observed incorrect behavior. Explain the assertion and actual result. '
             if expected['obligation'] == 'qa' else '') +
            'Source evidence uses kind=source, id=repository relative file path, sha256=file hash; '
            'source alone requires targeted QA. Do not claim a test covers behavior it never asserts. '
            'When review_dependency is set, address the requesting independent reports. '
            'Escalate using qa/security/human; never downgrade minimum risk or repository requirements. '
            'Use covered when execution establishes what happened for the criterion, including a reproduced '
            'failure. A reproduced failure requires verdict fail and a concrete finding. Use qa_required '
            'when evidence is insufficient or disputed. Pass requires that the criterion is satisfied. ' +
            behavioral_focus +
            (f'Of the configured project checks, only these declared IDs can establish routine coverage: {behavioral_ids}. '
             'Escalate coverage gaps and repository instruction obligations. ' if behavioral_ids else '') +
            'Return completed=true only after inspecting the exact diff and evidence. ' +
            ('The full exact diff is provided above. Reopen it if more context is needed.\n'
             if inline_patch else 'Review the full diff with git diff --no-ext-diff --no-textconv ' +
             expected['base'] + ' ' + expected['head'] + '; do not infer scope from a mutable branch or merge base.\n') +
            (f'Use the native review rubric JSON with its normal findings and overall_correctness. '
             'Inspect symlinks as link text and submodules at their pinned commits. '
             f'Put the SFC report in overall_explanation as one line: {NATIVE_REPORT}'
             'followed by the compact JSON object with verdict, summary, evidence, completed, scope, escalate '
             'and coverage. Leave findings out of that object and use the native findings field, with a '
             '[P0] to [P3] priority in each title and a repository file location. Use pass only for a '
             'correct patch with zero findings and use fail or blocked for an incorrect patch.\n'
             if native else '') + retention_instructions(f, card, expected['obligation']))


def validate_result(result):
    fields = {'verdict', 'summary', 'findings', 'evidence', 'completed', 'scope', 'escalate', 'coverage'}
    if (not isinstance(result, dict) or set(result) - {'recovery'} != fields or
            result['verdict'] not in ('pass', 'fail', 'blocked') or
            any(not isinstance(result[key], str) or not result[key].strip() for key in ('summary', 'evidence')) or
            not isinstance(result['findings'], list) or (result['verdict'] == 'pass' and result['findings'])):
        raise ValueError('invalid review verdict, summary, findings or evidence')
    for finding in result['findings']:
        if (not isinstance(finding, dict) or set(finding) != {'file', 'line', 'severity', 'message'} or
                not isinstance(finding['file'], str) or type(finding['line']) is not int or
                finding['severity'] not in ('critical', 'high', 'medium', 'low') or
                not isinstance(finding['message'], str)):
            raise ValueError('invalid review finding')


def prior_qa_receipt(f, card, wt, head, base, *, anchor=None, at=None, since=None, validate_imports=False):
    """Validate a live QA receipt or the immutable original cited by final review."""
    if not (f.record(card).get('review_route') or {}).get('behavioral_check_ids'):
        return None
    try:
        if anchor is None:
            receipt = f.record(card).get('receipts', {}).get('qa', {})
        else:
            validate_ref(anchor)
            receipt = {**load_ref(f.root, anchor), 'receipt_path': anchor['path'], 'receipt_sha256': anchor['sha256']}
        if (receipt.get('lane') == 'qa' and receipt['result']['verdict'] == 'pass' and
                (receipt['head'], receipt['base'], receipt['task_digest'], receipt['config_digest']) ==
                (head, base, f.task_digest(card), f.config_digest) and
                proof_valid(f, card, wt, receipt, head, base, frozen_at=at, since=since,
                            validate_imports=validate_imports, _archived_qa=anchor is not None)):
            return receipt
    except (OSError, ValueError, KeyError, TypeError, Halt):
        pass
    return None


def prior_qa_anchor(f, card, wt, head, base, *, at=None, since=None):
    receipt = prior_qa_receipt(f, card, wt, head, base, at=at, since=since, validate_imports=at is not None)
    if receipt is not None:
        return {'path': receipt['receipt_path'], 'sha256': receipt['receipt_sha256']}
    return None


def uses_prior_qa(result):
    return any(ref['kind'] == 'prior-qa' for row in result['coverage'] for ref in row['evidence'])


def prior_qa_criteria(qa, behavioral_ids):
    declared = {str(index) for index in behavioral_ids}
    return {row['criterion'] for row in qa['result']['coverage'] if row['status'] == 'covered' and any(
        ref['kind'] == 'qa-command' or ref['kind'] == 'check' and ref['id'] in declared for ref in row['evidence'])}


def validate_report(f, card, wt, result, expected, evidence, *, qa_log=None, at=None, since=None,
                    prior_qa=None, prior_qa_before=None, validate_imports=False):
    from .blocked_recovery import check_request
    from .review_retention import checked, validate_closures, validate_reference
    validate_result(result)
    check_request(f, result)
    if (result.get('completed') is not True or result.get('scope') != expected or
            not isinstance(result.get('escalate'), list) or
            any(v not in ('qa', 'security', 'human') for v in result['escalate'])):
        raise ValueError('incomplete review, scope drift or invalid escalation')
    coverage = result.get('coverage')
    ids = [v['id'] for v in criteria(card)]
    if (not isinstance(coverage, list) or len(coverage) != len(ids) or
            sorted(v.get('criterion', '') for v in coverage if isinstance(v, dict)) != sorted(ids)):
        raise ValueError('each acceptance criterion requires exactly one evidence mapping')
    correction = checked(f, card, wt, expected, evidence, at=at, since=since)
    commands = None
    qa_coverage = None
    behavioral_ids = (f.record(card).get('review_route') or {}).get('behavioral_check_ids', [])
    for row in coverage:
        if (set(row) != {'criterion', 'status', 'reason', 'evidence'} or
                row.get('status') not in ('covered', 'qa_required') or
                not isinstance(row.get('reason'), str) or not row['reason'].strip() or
                not isinstance(row.get('evidence'), list)):
            raise ValueError('invalid acceptance mapping')
        executed = False
        for ref in row['evidence']:
            if (not isinstance(ref, dict) or set(ref) != {'kind', 'id', 'sha256'} or
                    not isinstance(ref['id'], str) or not isinstance(ref['sha256'], str)):
                raise ValueError('invalid coverage reference')
            if ref['kind'] == 'check':
                if not isinstance(ref['id'], str) or not ref['id'].isdigit() or not 1 <= int(ref['id']) <= len(evidence['checks']):
                    raise ValueError('unknown executed check')
                check = evidence['checks'][int(ref['id']) - 1]
                if checksum(check['log']) != ref['sha256']:
                    raise ValueError('check evidence changed')
                if expected['obligation'] != 'review' or not behavioral_ids or int(ref['id']) in behavioral_ids:
                    executed = True
            elif ref['kind'] == 'qa-command':
                if expected['obligation'] != 'qa' or qa_log is None or ref['sha256'] != '':
                    raise ValueError('focused command evidence requires its own QA execution')
                if commands is None:
                    commands = {}
                    _, message = completed_message(qa_log, commands=commands)
                    if json.loads(message) != result:
                        raise ValueError('QA report differs from the completed turn')
                if not successful_qa_command(commands, ref['id']):
                    raise ValueError('focused command missing, unsuccessful or ambiguous in QA execution')
                executed = True
            elif ref['kind'] == 'prior-qa':
                if (expected['obligation'] != 'review' or not behavioral_ids or not prior_qa or
                        row['status'] != 'covered' or ref['id'] != row['criterion'] or
                        ref['sha256'] != prior_qa['sha256']):
                    raise ValueError('prior QA reference must bind the same final review criterion and receipt')
                if qa_coverage is None:
                    qa = prior_qa_receipt(f, card, wt, expected['head'], expected['base'],
                                          anchor=prior_qa, at=at, since=since, validate_imports=validate_imports)
                    if qa is None or (prior_qa_before is not None and qa['time'] > prior_qa_before):
                        raise ValueError('prior QA receipt is missing, stale or changed')
                    qa_coverage = prior_qa_criteria(qa, behavioral_ids)
                if ref['id'] not in qa_coverage:
                    raise ValueError('prior QA lacks valid execution evidence for this criterion')
                executed = True
            elif ref['kind'] == 'retained':
                validate_reference(correction, row, ref, expected)
                executed = True
            elif ref['kind'] == 'source':
                path = Path(ref['id'])
                target = Path(wt) / path
                if (path.is_absolute() or '..' in path.parts or
                        not target.resolve().is_relative_to(Path(wt).resolve()) or
                        target.is_symlink() or not git(wt, '--literal-pathspecs', 'ls-tree',
                            expected['head'], '--', str(path)) or checksum(target) != ref['sha256']):
                    raise ValueError('source evidence changed or outside checkout')
            else:
                raise ValueError('unsupported evidence kind')
        if row['status'] == 'covered' and not executed:
            raise ValueError('source-only or empty evidence cannot certify tested acceptance')
    validate_closures(correction, result, expected)
    return coverage


def native_command(f, worktree, output):
    """Custom scope avoids the CLI's --base/custom-prompt conflict; no resume."""
    try:
        version = run(['codex', '--version'], timeout=15).stdout.strip()
        help_text = run(['codex', 'exec', 'review', '--help'], timeout=15).stdout
    except (OSError, Halt) as exc:
        raise Halt('Native review provider unavailable; baseline policy retained, no automatic fallback') from exc
    if not version or not all(flag in help_text for flag in ('--json', '--output-last-message')):
        raise Halt('Native review provider unavailable: required supported CLI interface missing')
    argv = ['codex', 'exec', '-C', str(worktree), '-s', 'read-only',
            '-c', 'approval_policy="never"', 'review', '--json',
            '--output-last-message', str(output), '-']
    model = f.c.get('models', {}).get('review') or f.c.get('model')
    if model:
        argv[2:2] = ['-m', model, '-c', f'review_model={json.dumps(model)}']
    for key, setting in (('reasoning_effort', 'model_reasoning_effort'), ('service_tier', 'service_tier')):
        if f.c.get(key) is not None:
            argv[2:2] = ['-c', f'{setting}={json.dumps(f.c[key])}']
    argv[2:2] = worker_runtime.arguments(f.c)
    return argv, {'provider': 'codex-native', 'version': version, 'tool': shutil.which('codex'),
                  'help_sha256': digest(help_text), 'model': model}


def exec_provenance(model):
    version = run(['codex', '--version'], timeout=15).stdout.strip()
    if not version:
        raise Halt('Configured Codex review provider did not report a version')
    return {'provider': 'codex-exec', 'version': version, 'tool': shutil.which('codex'), 'model': model}


def native_source(wt, head, base):
    from .verification import source_tree
    try:
        if (git(wt, 'rev-parse', 'HEAD') != head or git(wt, 'rev-parse', base + '^{commit}') != base or
                git(wt, 'status', '--porcelain', '--untracked-files=all', '--ignore-submodules=none') or
                source_tree(wt, allow_special_entries=True) != git(wt, 'rev-parse', head + '^{tree}')):
            raise ValueError('candidate source mismatch')
    except (OSError, ValueError, Halt) as exc:
        raise Halt('Native review requires exact clean candidate bytes and an immutable base commit') from exc


def submitted_qa_command(recorded):
    """Unwrap one recognized shell invocation from a recorded Codex event."""
    try:
        argv = shlex.split(recorded)
    except ValueError:
        return recorded
    if len(argv) == 3 and argv[1] in ('-c', '-lc') and argv[2].strip():
        shell = Path(argv[0])
        if shell.name in ('bash', 'dash', 'sh', 'zsh') and (shell.is_absolute() or argv[0] == shell.name):
            return argv[2]
    return recorded


def exact_qa_command(commands, reference):
    names = {reference, submitted_qa_command(reference)} if reference in commands else {reference}
    outcomes = [ok for recorded, results in commands.items()
                if recorded in names or submitted_qa_command(recorded) in names
                for ok in results]
    return outcomes == [True]


def successful_qa_command(commands, reference):
    label = reference.removeprefix('proof:')
    if label == reference or not re.fullmatch(r'[a-z][a-z0-9-]{0,63}', label):
        return exact_qa_command(commands, reference)
    successful = []
    for recorded, outcomes in commands.items():
        first, separator, body = submitted_qa_command(recorded).partition('\n')
        if (separator and first == '# sfc-qa-proof: ' + label and
                any(line.strip() and not line.lstrip().startswith('#') for line in body.split('\n'))):
            if None in outcomes:
                return False
            successful.extend(recorded for outcome in outcomes if outcome is True)
    return len(successful) == 1 and exact_qa_command(commands, successful[0])


def completed_message(log, *, native=False, commands=None):
    stages = ('thread.started', 'turn.started', 'turn.completed')
    index, thread, message = 0, None, None
    started = {}
    completed = set()
    path = Path(log)
    if path.stat().st_size > MAX_NATIVE_BYTES:
        raise ValueError('review event log exceeds the validation limit')
    with path.open() as stream:
        for line in stream:
            # stderr diagnostics can accompany JSONL, but a broken event may
            # conceal a failure. A complete thread must end at turn.completed.
            if not line.lstrip().startswith('{'):
                continue
            value = json.loads(line)
            kind = value.get('type') if isinstance(value, dict) else None
            if not isinstance(kind, str) or kind in ('turn.failed', 'error') or index == len(stages):
                raise ValueError('review event stream failed or ended out of order')
            if kind in stages:
                # Exec emits a synthetic start for review/start and can also
                # forward the server start for the same native turn.
                if native and kind == 'turn.started' and index == 2 and message is None:
                    continue
                if kind != stages[index]:
                    raise ValueError('review lifecycle events out of order')
                if kind == 'thread.started':
                    thread = value.get('thread_id')
                index += 1
            elif commands is not None and kind == 'item.started':
                item = value.get('item')
                if index != 2 or not isinstance(item, dict):
                    raise ValueError('malformed QA item')
                if item.get('type') == 'command_execution':
                    item_id, command = item.get('id'), item.get('command')
                    if (not isinstance(item_id, str) or not item_id or item_id in started or
                            not isinstance(command, str) or not command.strip() or
                            item.get('status') != 'in_progress'):
                        raise ValueError('invalid QA command start')
                    started[item_id] = command
                    message = None
            elif kind == 'item.completed' and (index == 2 or commands is not None):
                item = value.get('item')
                if index != 2 or not isinstance(item, dict):
                    raise ValueError('malformed review item')
                if item.get('type') == 'agent_message':
                    message = item.get('text')
                elif commands is not None and item.get('type') == 'command_execution':
                    item_id, command = item.get('id'), item.get('command')
                    if (not isinstance(item_id, str) or item_id not in started or
                            started[item_id] != command or item_id in completed):
                        raise ValueError('invalid QA command completion')
                    completed.add(item_id)
                    message = None
                    status, code = item.get('status'), item.get('exit_code')
                    valid = (type(code) is int and isinstance(item.get('aggregated_output'), str) and
                             (status == 'completed' or status == 'failed' and code != 0))
                    # A malformed completion cannot be treated as a failed setup probe.
                    commands.setdefault(command, []).append(code == 0 if valid else None)
    if commands is not None and started.keys() != completed:
        raise ValueError('QA execution has unfinished commands')
    if (index != len(stages) or not isinstance(thread, str) or not thread.strip() or
            not isinstance(message, str) or not message.strip()):
        raise ValueError('review event stream lacks a completed reviewer message')
    return thread, message.strip()


def native_sessions():
    home = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex').expanduser()
    return home / 'sessions'


def capture_native_rollout(log, destination):
    thread, _ = completed_message(log, native=True)
    if str(UUID(thread)) != thread:
        raise ValueError('native review did not report a canonical thread ID')
    sessions = native_sessions().resolve()
    paths = list(sessions.glob(f'*/*/*/rollout-*-{thread}.jsonl'))
    if (len(paths) != 1 or paths[0].is_symlink() or not paths[0].is_file() or
            not paths[0].resolve().is_relative_to(sessions)):
        raise ValueError('missing or ambiguous structured native review rollout')
    with paths[0].open('rb') as stream:
        raw = stream.read(MAX_NATIVE_BYTES + 1)
    if len(raw) > MAX_NATIVE_BYTES:
        raise ValueError('native review rollout exceeds the validation limit')
    # Raw review transcripts can contain private task text and tool output.
    # Exclusive creation rejects existing files and symlinks before any write.
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(raw)


def native_result(log, raw, rollout, worktree, version):
    thread, message = completed_message(log, native=True)
    if Path(raw).stat().st_size > MAX_NATIVE_BYTES or Path(raw).read_text().strip() != message:
        raise ValueError('native last message differs from its completed event')
    if Path(rollout).stat().st_size > MAX_NATIVE_BYTES:
        raise ValueError('native review rollout exceeds the validation limit')
    # The CLI renders both a parsed zero-finding review and a parse failure as
    # assistant text. The same thread's completed native item retains the
    # correctness field, which Codex leaves empty on a parse failure.
    with Path(rollout).open() as stream:
        first = json.loads(stream.readline())
        meta = first['payload']
        root = Path(worktree).resolve()
        if (first['type'] != 'session_meta' or meta['id'] != thread or
                meta.get('forked_from_id') or meta.get('parent_thread_id') or meta.get('history_base') or
                Path(meta['cwd']).resolve() != root or 'codex-cli ' + meta['cli_version'] != version):
            raise ValueError('native review rollout identity differs from the completed CLI thread')
        native_items, completions, aborted = [], [], set()
        for index, line in enumerate(stream):
            row = json.loads(line)
            if row['type'] != 'event_msg':
                continue
            event = row['payload']
            kind = event['type']
            if kind == 'item_completed' and event['item']['type'] == 'ExitedReviewMode':
                if event['thread_id'] != thread:
                    raise ValueError('native review item belongs to a different thread')
                native_items.append((index, event['turn_id'], event['item']['review_output']))
            elif kind == 'task_complete':
                completions.append((index, event['turn_id'], event.get('error')))
            elif kind == 'turn_aborted':
                aborted.add(event.get('turn_id'))
    if len(native_items) != 1:
        raise ValueError('missing or ambiguous structured native review result')
    index, turn, verdict = native_items[0]
    finished = [(i, error) for i, ident, error in completions if ident == turn]
    if (not isinstance(turn, str) or not turn or turn in aborted or len(finished) != 1 or
            finished[0][0] <= index or finished[0][1] is not None or not isinstance(verdict, dict) or
            verdict.get('overall_correctness') not in ('patch is correct', 'patch is incorrect') or
            not isinstance(verdict.get('findings'), list)):
        raise ValueError('native verdict was not parsed and completed')
    confidence = verdict.get('overall_confidence_score')
    if type(confidence) not in (int, float) or not 0 <= confidence <= 1:
        raise ValueError('malformed native verdict confidence')
    header = verdict.get('overall_explanation')
    if (not isinstance(header, str) or not header.startswith(NATIVE_REPORT) or
            header != message.partition('\n\n')[0]):
        raise ValueError('native review is missing the acceptance report in overall_explanation')
    result = json.loads(header[len(NATIVE_REPORT):])
    if not isinstance(result, dict) or 'findings' in result:
        raise ValueError('native acceptance report must leave findings to the native renderer')
    if result.get('verdict') == 'pass' and verdict['overall_correctness'] != 'patch is correct':
        raise ValueError('SFC pass conflicts with native correctness verdict')
    findings = []
    for finding in verdict['findings']:
        priority, title, body = finding['priority'], finding['title'], finding['body']
        confidence = finding['confidence_score']
        code = finding['code_location']
        location = Path(code['absolute_file_path'])
        start, end = code['line_range']['start'], code['line_range']['end']
        if (type(priority) is not int or not 0 <= priority <= 3 or
                type(confidence) not in (int, float) or not 0 <= confidence <= 1 or
                not isinstance(title, str) or not title.startswith(f'[P{priority}] ') or
                not title[5:].strip() or not isinstance(body, str) or not body.strip() or
                type(start) is not int or type(end) is not int or not 0 < start <= end or
                not location.is_absolute() or not location.resolve().is_relative_to(root)):
            raise ValueError('malformed native review finding or location outside the candidate')
        findings.append({'file': str(location.resolve().relative_to(root)), 'line': start,
                         'severity': ('critical', 'high', 'medium', 'low')[priority],
                         'message': title[5:] + '\n' + body})
    return {**result, 'findings': findings}


def native_proof(receipt, worktree):
    raw, rollout = receipt['native_message'], receipt['native_rollout']
    return (checksum(raw) == receipt['native_message_sha256'] and
            checksum(rollout) == receipt['native_rollout_sha256'] and
            native_result(receipt['log'], raw, rollout, worktree, receipt['provider']['version']) == receipt['result'])


def receipt_fresh(c, receipt, *, at=None, since=None):
    try:
        now = dt.datetime.now(dt.timezone.utc)
        assessed = dt.datetime.fromisoformat(at) if at is not None else now
        earliest = dt.datetime.fromisoformat(since) if since is not None else assessed
        issued = dt.datetime.fromisoformat(receipt['time'])
        age = (assessed - issued).total_seconds()
        return issued <= earliest <= assessed <= now and 0 <= age <= settings(c).get('max_age_seconds', 86400)
    except (KeyError, TypeError, ValueError):
        return False


def fresh_receipts(f, card, *, at=None):
    if settings(f.c) is None:
        return True
    receipts = f.record(card).get('receipts', {})
    return all(receipt_fresh(f.c, receipts.get(lane, {}), at=at) for lane in required(f, card))


def proof_valid(f, card, wt, receipt, head, base, *, frozen_at=None, since=None, validate_imports=False,
                _archived_qa=False):
    try:
        from .verification import load, validate_evidence
        original = {k: v for k, v in receipt.items() if k not in ('import_path', 'import_sha256')}
        live_imports = frozen_at is None or validate_imports
        if 'import_path' in receipt and live_imports:
            if (checksum(receipt['import_path']) != receipt['import_sha256'] or
                    read_json(receipt['import_path']) != original):
                return False
        if receipt['provider']['provider'] != 'github-native' and load(original)['kind'] != 'review':
            return False
        verification_scope = {'freshness': live_imports}
        if validate_imports and frozen_at is not None:
            verification_scope.update(at=frozen_at, since=since)
        f.verification_evidence(card, head, base, **verification_scope)
        evidence = receipt['verification']
        if (not evidence['passed'] or evidence['head'] != head or evidence['base'] != base or
                evidence['config_digest'] != f.config_digest or evidence['task_digest'] != f.task_digest(card) or
                [v['command'] for v in evidence['checks']] != f.c['verify_commands']):
            return False
        validate_evidence(evidence, wt, f, card, base, **verification_scope)
        if (checksum(receipt['log']) != receipt['log_sha256'] or
                checksum(receipt['result_path']) != receipt['result_sha256'] or
                read_json(receipt['result_path']) != receipt['result']):
            return False
        if not receipt_fresh(f.c, receipt, at=frozen_at, since=since):
            return False
        provider = receipt['provider']['provider']
        inspection = None
        if receipt['lane'] == 'review':
            from .review_inspection import adverse_pending, final_reference
            if receipt['result']['verdict'] == 'pass' and adverse_pending(f, card, head, base):
                return False
            inspection = final_reference(f, card, wt, head, base, at=frozen_at, since=since)
        if receipt.get('inspection_sha256') != (inspection or {}).get('receipt_sha256'):
            return False
        expected = scope(f, card, head, base, evidence, receipt['lane'], inspection=inspection)
        if _archived_qa:
            if receipt['lane'] != 'qa':
                return False
            # The final review consumed this exact QA report before any new
            # targeted requests. The current QA lane validates those requests.
            expected['review_dependency'] = receipt['result']['scope']['review_dependency']
        prior_qa = receipt.get('prior_qa')
        if (bool(prior_qa) != uses_prior_qa(receipt['result']) or
                prior_qa and (receipt['lane'] != 'review' or provider == 'github-native')):
            return False
        validate_report(f, card, wt, receipt['result'],
                        expected, evidence, qa_log=receipt['log'] if provider == 'codex-exec' else None,
                        at=frozen_at, since=since, prior_qa=prior_qa, prior_qa_before=receipt['time'],
                        validate_imports=validate_imports)
        if provider not in ('codex-exec', 'codex-native', 'github-native'):
            return False
        configured = provider_for(f, card, receipt['lane'], head, base)
        if provider != configured and not (provider == 'github-native' and configured == 'codex-native'):
            return False
        if provider == 'codex-native':
            if not native_proof(receipt, wt):
                return False
            native_source(wt, head, base)
        if provider == 'github-native':
            remote = read_json(receipt['log'])
            pr, review = receipt['provider']['pr'], receipt['provider']['review_id']
            passing = receipt['result']['verdict'] == 'pass'
            states = ('APPROVED', 'COMMENTED') if passing else ('APPROVED', 'COMMENTED', 'CHANGES_REQUESTED')
            if (remote.get('commit_id') != head or remote.get('state') not in states or
                    json.loads(remote['body']) != receipt['result'] or
                    remote.get('id') != review or type(pr) is not int or type(review) is not int or
                    min(pr, review) <= 0 or remote.get('user', {}).get('type') != 'Bot' or
                    remote.get('user', {}).get('id') not in settings(f.c).get('github_reviewer_ids', []) or
                    dt.datetime.fromisoformat(remote['submitted_at'].replace('Z', '+00:00')) !=
                    dt.datetime.fromisoformat(receipt['time'])):
                return False
            if not live_imports:
                # The supervisor verified this exact receipt and its live
                # GitHub readback before issuing the atomic write.
                return True
            endpoint = f"repos/{f.c['repo']}/pulls/{pr}/reviews/{review}"
            if f.gh.call('api', endpoint) != remote:
                return False
            comments = f.gh.call('api', endpoint + '/comments?per_page=100', '--paginate', '--slurp')
            if passing and comments != [[]]:
                return False
            if (not isinstance(comments, list) or not comments or
                    any(not isinstance(page, list) or any(not isinstance(row, dict) for row in page)
                        for page in comments)):
                return False
        return True
    except (AttributeError, OSError, ValueError, TypeError, KeyError, Halt):
        return False


def required(f, card):
    if settings(f.c) is None:
        return LANES
    route = f.record(card).get('review_route')
    return tuple(k for k in LANES if not route or k in route['obligations'])


def human_required(f, card):
    route = f.record(card).get('review_route') or {}
    return settings(f.c) is not None and 'human' in route.get('obligations', [])


def route_valid(f, card, wt, head, base):
    from .review_retention import valid_route
    try:
        route = f.record(card)['review_route']
        if not valid_route(f, card, route):
            return False
        current = plan(f, card, wt, base)
        if head != current['head'] or any(route[k] != current[k] for k in
                ('head', 'base', 'config_digest', 'task_digest', 'policy_sha256', 'providers', 'risk', 'consolidated', 'review_first')):
            return False
        if route.get('behavioral_check_ids') != current.get('behavioral_check_ids'):
            return False
        if route.get('provisional_qa') != current.get('provisional_qa'):
            return False
        minimum = set(current['obligations'])
        for receipt in f.record(card).get('receipts', {}).values():
            minimum.update(requested_obligations(receipt['result']))
        return minimum <= set(route['obligations'])
    except (KeyError, ValueError, TypeError, OSError, Halt):
        return False


def imports_enabled(f):
    p = settings(f.c) or {}
    return bool(p.get('imports') and p.get('providers', {}).get('review') == 'codex-native')


def imported(f, card, wt, head, base):
    """Read-only reuse; no cloud dispatch/poll or duplicate equivalent review.

    A configured import is a supervisor-captured envelope, with raw evidence
    hashes. GitHub completion is independently read back before accepting it.
    Default P0/P1 comments lack the full scope/coverage contract and are misses.
    """
    gaps, verified = [], []
    for ref in settings(f.c).get('imports', []):
        try:
            path = Path(ref['path'])
            path = path if path.is_absolute() else f.root / path
            before = checksum(path)
            receipt = read_json(path)
            if (receipt.get('lane') != 'review' or receipt.get('head') != head or
                    receipt.get('base') != base or receipt.get('config_digest') != f.config_digest or
                    receipt.get('task_digest') != f.task_digest(card) or
                    not proof_valid(f, card, wt, receipt, head, base)):
                raise ValueError('candidate, scope, coverage, completion or provenance gap')
            if checksum(path) != before:
                raise ValueError('import changed during observation')
            verified.append({**receipt, 'import_path': str(path), 'import_sha256': before})
        except (AttributeError, OSError, ValueError, KeyError, TypeError, Halt):
            gaps.append('Existing review cannot satisfy exact candidate/base, policy coverage, completion or provenance')
    return verified, gaps


def pending_lanes(f, card):
    receipts = f.record(card).get('receipts', {})
    return [lane for lane in required(f, card) if
            receipts.get(lane, {}).get('result', {}).get('verdict') != 'pass' or
            receipts[lane]['result'].get('scope', {}).get('review_dependency', '') !=
            review_dependency(f, card, lane)]


def activity_progress(f, card):
    route = f.record(card).get('review_route')
    if (settings(f.c) is None or not route or route['config_digest'] != f.config_digest or
            route['task_digest'] != f.task_digest(card)):
        return None
    pending = pending_lanes(f, card)
    if route.get('provisional_qa'):
        action = 'review_provisional_qa'
    elif route.get('behavioral_check_ids'):
        action = 'review_behavioral'
    elif route.get('review_first', route['consolidated']):
        action = 'review_risk'
        if 'review' in pending:
            pending.remove('review')
            pending.insert(0, 'review')
    else:
        action = 'review_native_baseline' if route['providers']['review'] == 'codex-native' else 'review_baseline'
    if human_required(f, card):
        pending.append('human')
    return action, tuple(pending)


def escalate(f, card, result, imports=()):
    route = f.record(card)['review_route']
    obligations = set(route['obligations']) | requested_obligations(result)
    for receipt in imports:
        additions = requested_obligations(receipt['result'])
        if not additions:
            continue
        # A verified import can only add obligations to these pins. Retain its
        # report and provenance even if the inbox is later replaced or expires.
        route.setdefault('import_escalations', {}).setdefault(receipt['result_sha256'], {
            k: receipt[k] for k in ('result', 'import_path', 'import_sha256', 'provider', 'log', 'log_sha256')})
        obligations.update(additions)
    route['obligations'] = sorted(obligations)
    if result['verdict'] != 'pass':
        route['next_actions'] = ['Resolve the independent findings or review blocker']
        f.update(card, review_route=route)
        return
    route['next_actions'] = [f'{lane}: verify pinned acceptance evidence' for lane in pending_lanes(f, card)]
    if not route['next_actions']:
        route['next_actions'].append('Revalidate acceptance evidence and required GitHub checks')
    if 'human' in route['obligations']:
        route['next_actions'].append('Human QA-only assessment required' if 'qa' in card.get('labels', [])
                                     else 'Human merge decision required')
    f.update(card, review_route=route)


def requested_obligations(result):
    additions = set(result.get('escalate', []))
    if coverage_gaps(result):
        additions.add('qa')
    return additions


def coverage_gaps(result):
    return {row['criterion'] for row in result.get('coverage', []) if row['status'] == 'qa_required'}


def acceptance_complete(f, card):
    record = f.record(card)
    receipts = record.get('receipts', {})
    qa = receipts.get('qa', {}).get('result', {})
    provisional = (record.get('review_route') or {}).get('provisional_qa')
    if provisional and receipts.get('review', {}).get('inspection_sha256') != provisional:
        return False
    gaps = set()
    for report in review_requests(record).values():
        gaps.update(coverage_gaps(report['result']))
    if gaps:
        dependency = review_dependency(f, card, 'qa')
        covered = {row['criterion'] for row in qa.get('coverage', []) if row['status'] == 'covered'}
        if not dependency or qa.get('scope', {}).get('review_dependency') != dependency or not gaps <= covered:
            return False
    for criterion in criteria(card):
        if not any(any(row['criterion'] == criterion['id'] and row['status'] == 'covered'
                       for row in receipts.get(lane, {}).get('result', {}).get('coverage', []))
                   for lane in ('review', 'qa')):
            return False
    return True


def source_review_config(config):
    """Bind source policy separately from the release's note and write authority."""
    bound = dict(config)
    if 'release' in bound:
        bound['release'] = {key: value for key, value in bound['release'].items()
                            if key not in ('authority', 'approved_summaries')}
    return digest(bound)


def release_source_coverage(f, journal):
    """Carry a completed review only when it covers the entire release diff."""
    from .verification import load, original, validate_evidence
    try:
        changes = journal['changes']
        candidate, boundary = journal['candidate'], journal['boundary']
        if (not boundary or len(changes['issues']) != 1 or
                len(changes['pull_requests']) != 1 or changes['unlinked_commits'] or
                changes['pull_requests'][0]['merge_commit'] != candidate):
            return None
        number = changes['issues'][0]
        record = f.state['cards'][str(number)]
        completion = record.get('local_merge') or record.get('external_completion') or {}
        source_config_digest = completion['config_digest']
        if source_config_digest != f.config_digest:
            same_source_policy = completion.get('source_review_config_sha256') == source_review_config(f.c)
            # A source run without release settings may add them after merge.
            added_release = (f.c.get('release', {}).get('enabled') is True and
                             digest({key: value for key, value in f.c.items() if key != 'release'}) == source_config_digest)
            if not (same_source_policy or added_release):
                return None
        head = completion['head']
        tree = git(f.root, 'rev-parse', head + '^{tree}')
        if (completion['phase'] != 'done' or completion['pr'] != changes['pull_requests'][0]['number'] or
                completion['card_identity']['number'] != number or
                completion['card_identity']['repository'].get('nameWithOwner') != f.c['repo'] or completion['base'] != boundary or
                completion.get('merge_oid', head) != candidate or
                tree != git(f.root, 'rev-parse', candidate + '^{tree}')):
            return None
        baseline = settings(f.c) is None
        route = completion.get('review_route')
        if baseline:
            if route is not None or set(completion['receipts']) != set(LANES):
                return None
        elif (route['head'] != head or route['base'] != boundary or route['config_digest'] != source_config_digest or
              route['task_digest'] != completion['task_digest'] or route['policy_sha256'] != digest(settings(f.c))):
            return None
        evidence = completion['verification']

        def check_snapshot(snapshot):
            if (snapshot['kind'] != 'verification' or snapshot['passed'] is not True or
                    snapshot['head'] != head or snapshot['base'] != boundary or snapshot['tree'] != tree or
                    snapshot['repository'] != f.c['repo'] or snapshot['config_digest'] != source_config_digest or
                    snapshot['task_digest'] != completion['task_digest'] or
                    [check['command'] for check in snapshot['checks']] != f.c['verify_commands']):
                raise ValueError('Incomplete historical source verification')
            # The merge already validated live bindings. A historical
            # assessment can still verify original receipts, logs and their
            # acceptance provenance after its checkout has been removed.
            validate_evidence(snapshot, f.root, freshness=False)
            return [original(check)[1]['receipt_sha256'] for check in snapshot['checks']]

        def lane_reference(receipt, lane):
            result = receipt['result']
            kind = 'baseline-source-review' if baseline else 'review'
            if (load(receipt)['kind'] != kind or receipt['lane'] != lane or receipt['head'] != head or
                    receipt['base'] != boundary or receipt['task_digest'] != completion['task_digest'] or
                    receipt['config_digest'] != source_config_digest or result['verdict'] != 'pass' or
                    (not baseline and result['completed'] is not True) or result['findings'] != [] or
                    checksum(receipt['log']) != receipt['log_sha256'] or
                    checksum(receipt['result_path']) != receipt['result_sha256'] or
                    read_json(receipt['result_path']) != result):
                raise ValueError('Incomplete historical source assessment')
            if baseline:
                if receipt['provider']['provider'] != 'codex-exec':
                    raise ValueError('Unexpected provider in baseline source assessment')
                check_snapshot(receipt['verification'])
                return {'sha256': receipt['receipt_sha256'], 'provider': 'codex-exec',
                        'result': result, 'receipt': receipt['receipt_path']}
            if receipt['provider']['provider'] == 'codex-native' and any(
                    checksum(receipt[key]) != receipt[key + '_sha256'] for key in ('native_message', 'native_rollout')):
                raise ValueError('Historical native assessment changed')
            scope = result['scope']
            # These reports rely on another review or inspection archive.
            # Require a full release source review until the complete chain
            # can be validated without the old worktree.
            if ('correction' in scope or receipt.get('inspection_sha256') or scope['review_dependency'] or any(
                    ref['kind'] == 'retained' for row in result['coverage'] for ref in row['evidence'])):
                raise ValueError('Historical source assessment has an unsupported dependency')
            lane_checks = check_snapshot(receipt['verification'])
            if (scope['head'] != head or scope['base'] != boundary or scope['config'] != source_config_digest or
                    scope['task'] != completion['task_digest'] or scope['policy'] != route['policy_sha256'] or
                    scope['obligation'] != lane or scope['verification'] != digest(lane_checks) or
                    receipt['provider']['provider'] != route['providers'][lane]):
                raise ValueError('Historical source assessment scope differs')
            return {'sha256': receipt['receipt_sha256'], 'provider': receipt['provider']['provider'],
                    'result': result, 'receipt': receipt['receipt_path']}

        checks = check_snapshot(evidence)
        references = {lane: lane_reference(completion['receipts'][lane], lane)
                      for lane in LANES if baseline or lane in route['obligations']}
        if 'review' not in references:
            return None
        review = completion['receipts']['review']
        anchor = review.get('prior_qa')
        citations = [(row, ref) for row in review['result'].get('coverage', []) for ref in row['evidence']
                     if ref['kind'] == 'prior-qa']
        if bool(anchor) != bool(citations):
            return None
        if anchor:
            validate_ref(anchor)
            qa = {**load_ref(f.root, anchor), 'receipt_path': anchor['path'], 'receipt_sha256': anchor['sha256']}
            lane_reference(qa, 'qa')
            commands = {}
            _, message = completed_message(qa['log'], commands=commands)
            criteria_ids = prior_qa_criteria(qa, route.get('behavioral_check_ids', []))
            if (not route.get('behavioral_check_ids') or not receipt_fresh(f.c, qa, at=review['time']) or
                    json.loads(message) != qa['result'] or uses_prior_qa(qa['result']) or qa.get('prior_qa') or
                    any(not successful_qa_command(commands, ref['id']) for row in qa['result']['coverage']
                        for ref in row['evidence'] if ref['kind'] == 'qa-command') or
                    any(not 1 <= int(ref['id']) <= len(qa['verification']['checks']) or
                        checksum(qa['verification']['checks'][int(ref['id']) - 1]['log']) != ref['sha256']
                        for row in qa['result']['coverage'] for ref in row['evidence'] if ref['kind'] == 'check') or
                    any(row['status'] != 'covered' or ref['id'] != row['criterion'] or
                        ref['id'] not in criteria_ids or ref['sha256'] != anchor['sha256'] for row, ref in citations)):
                return None
        return {'issue': number, 'pr': completion['pr'], 'head': head, 'base': boundary,
                'release_candidate': candidate, 'verification_sha256': evidence['receipt_sha256'],
                'check_executions': checks, 'lanes': references, **({'prior_qa': anchor} if anchor else {})}
    except (AttributeError, OSError, ValueError, KeyError, TypeError, Halt):
        return None


def describe(f, card):
    """Report recorded evidence assignments without making an acceptance decision."""
    record = f.state['cards'].get(str(card['number']), {})
    route, release = record.get('review_route') or {}, record.get('release_evidence')
    receipts = record.get('receipts', {})
    assignments = {}
    acceptance = []
    if release:
        coverage = release.get('source_coverage')
        assignments['source-range'] = {'provider': 'completed work-ticket review' if coverage else 'release review',
                                       'scope': 'release source range',
                                       'recorded_state': 'reused' if coverage else 'review required'}
        if coverage:
            assignments['source-range']['references'] = {lane: v['sha256'] for lane, v in coverage['lanes'].items()}
        for name, lane in (('package-integrity', 'qa'), ('installation', 'qa'), ('release-security', 'security'),
                           ('membership-and-notes', 'review'), ('publication-readiness', 'review')):
            assignments[name] = {'provider': f'independent {lane}', 'scope': 'frozen release package and completed checks',
                                 'recorded_state': 'passed' if receipts.get(lane, {}).get('result', {}).get('verdict') == 'pass'
                                 else 'pending'}
    elif route and route['config_digest'] == f.config_digest and route['task_digest'] == f.task_digest(card):
        pending = pending_lanes(f, card)
        if provisional := route.get('provisional_qa'):
            final = receipts.get('review', {})
            assignments['provisional-qa'] = {
                'provider': 'independent source QA and final reviewer',
                'scope': 'candidate, base, task and completed required checks', 'reference': provisional,
                'recorded_state': 'passed' if final.get('inspection_sha256') == provisional and
                final.get('result', {}).get('verdict') == 'pass' and not pending else 'pending final assessment',
            }
        for lane in route['obligations']:
            receipt = receipts.get(lane, {})
            passed = receipt.get('result', {}).get('verdict') == 'pass' and lane not in pending
            assignments[lane] = {'provider': receipt.get('provider', {}).get('provider', route['providers'].get(lane, 'human')),
                                 'scope': 'candidate, base, task, policy and required check executions',
                                 'recorded_state': 'passed' if passed else 'pending',
                                 'reference': receipt.get('receipt_sha256')}
        try:
            accepted_criteria = criteria(card)
        except Halt:
            accepted_criteria = []
        for criterion in accepted_criteria:
            covered = [lane for lane in ('review', 'qa') if assignments.get(lane, {}).get('recorded_state') == 'passed' and
                       any(row['criterion'] == criterion['id'] and row['status'] == 'covered'
                           for row in receipts[lane]['result'].get('coverage', []))]
            acceptance.append({'criterion': criterion['id'], 'recorded_providers': covered,
                               'recorded_state': 'covered' if covered else 'pending'})
    inspections = [{k: attempt[k] for k in ('pins', 'state', 'overlap_seconds') if k in attempt}
                   | ({'inspection_seconds': attempt['receipt']['elapsed_seconds'],
                       'reference': attempt['receipt']['receipt_sha256']} if 'receipt' in attempt else {})
                   for attempt in record.get('review_inspections', [])]
    return {'assignments': assignments, 'acceptance': acceptance, 'provisional_inspections': inspections}
