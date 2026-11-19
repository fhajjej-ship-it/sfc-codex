"""Opt-in release workflow, independent of work type and merge authority.

All remote effects have durable intent/readback. An unresolved effect is only
read back, never reissued. Journals live in the common git directory so linked
checkouts cannot create a second owner for a version.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import time

from .common import Halt, Stopped, common_dir, git, now, read_json, run, write_json
from .rate_limit import ReadConnectionReset
from .release_github import ReleaseGitHub, ReleasePushPermissionMissing

VERSION = r'v?[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9]+(?:[.-][A-Za-z0-9]+)*)?'
SHA = r'[0-9a-f]{40}'
AUTHORITY_SCOPE = ('repo', 'base_branch', 'host', 'destination')
AUTHORITY_NODE_IDS = ('repository_id', 'project')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def checksum(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def safe_path(value):
    return (isinstance(value, str) and bool(value) and
            re.fullmatch(r'[A-Za-z0-9_./-]+', value) is not None and
            not PurePosixPath(value).is_absolute() and
            all(p not in ('..', '.', '.git', '.sfc') for p in value.split('/')))


def artifact_allowlist(files, version=None):
    error = 'release.artifacts requires distinct exact relative allowlisted paths; use only {version}, no globs or reserved manifest name'
    if not isinstance(files, list) or not files or any(not isinstance(path, str) for path in files):
        raise Halt(error)
    if version is not None:
        if not isinstance(version, str) or len(version) > 80 or not re.fullmatch(VERSION, version):
            raise Halt('Invalid frozen release version for artifact paths')
        files = [path.replace('{version}', version.removeprefix('v')) for path in files]
    names = {Path(path).name for path in files}
    if (len(names) != len(files) or 'sfc-release-manifest.json' in names or
            any(not safe_path(path if version is not None else path.replace('{version}', '0')) for path in files)):
        raise Halt(error)
    return files


def validate(c):
    cfg = c.get('release')
    if cfg is None:
        return
    if not isinstance(cfg, dict) or type(cfg.get('enabled', False)) is not bool:
        raise Halt('release must be a project configuration with boolean enabled')
    if not cfg.get('enabled'):
        if set(cfg) != {'enabled'}:
            raise Halt('Disabled release configuration accepts only enabled:false')
        return
    allowed = {'enabled', 'adapter', 'package_commands', 'verify_commands', 'artifacts',
               'version_file', 'trigger', 'authority', 'public_docs', 'approved_summaries', 'latest_policy',
               'preflight_commands'}
    if set(cfg) - allowed or cfg.get('adapter') != 'github-releases':
        raise Halt('release supports only the explicit github-releases adapter')
    for key in ('package_commands', 'verify_commands', 'preflight_commands'):
        if key == 'preflight_commands' and key not in cfg:
            continue
        commands = cfg.get(key)
        empty_allowed = key == 'verify_commands' and bool(cfg.get('preflight_commands'))
        if not isinstance(commands, list) or (not commands and not empty_allowed) or any(
                not isinstance(v, str) or not v.strip() or '\0' in v for v in commands):
            raise Halt('release.' + key + ' requires nonempty project-owned commands')
    artifact_allowlist(cfg.get('artifacts'))
    if cfg.get('trigger', 'manual') not in ('manual', 'version-file'):
        raise Halt('release.trigger must be manual or version-file; never every merge')
    if ('version_file' in cfg and not safe_path(cfg['version_file'])) or (
            cfg.get('trigger') == 'version-file' and not cfg.get('version_file')):
        raise Halt('release.version_file requires an exact relative plain-text version file')
    docs = cfg.get('public_docs', {})
    if (not isinstance(docs, dict) or set(docs) - {'Installation', 'Update', 'Prerequisites', 'Breaking changes', 'Known limitations'} or
            any(not safe_path(v) or Path(v).suffix.lower() not in ('.md', '.markdown') for v in docs.values())):
        raise Halt('release.public_docs requires supported headings and exact public Markdown documentation paths')
    auth = cfg.get('authority')
    approval = cfg.get('approved_summaries')
    if approval is not None:
        scope = {'repo': c['repo'], 'base_branch': c['base_branch'], 'host': 'github.com',
                 'destination': 'github-releases'}
        if (not isinstance(approval, dict) or set(approval) != {*scope, 'authorization_id', 'entries'} or
                any(approval.get(k) != v for k, v in scope.items()) or
                not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', str(approval.get('authorization_id', ''))) or
                not isinstance(approval.get('entries'), list) or not approval['entries']):
            raise Halt('release.approved_summaries requires explicit text authority scoped to this source repository/base and destination')
        from .release_notes import eligible_title
        for entry in approval['entries']:
            if (not isinstance(entry, dict) or set(entry) != {'commit', 'text'} or
                    not re.fullmatch(SHA, str(entry.get('commit', ''))) or
                    not eligible_title(entry.get('text')) or len(entry['text'].split()) < 3):
                raise Halt('Approved summaries require exact commit hashes and readable safe text')
    if cfg.get('latest_policy', 'newer-stable') not in ('newer-stable', 'never'):
        raise Halt('release.latest_policy must be newer-stable or never')
    legacy_keys = {'authorization_id', 'mode', *AUTHORITY_SCOPE}
    if auth is not None and (not isinstance(auth, dict) or
            set(auth) not in (legacy_keys, legacy_keys | set(AUTHORITY_NODE_IDS)) or
            any(key in auth and (not isinstance(auth[key], str) or not auth[key].strip()) for key in AUTHORITY_NODE_IDS) or
            not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', str(auth.get('authorization_id', ''))) or
            auth.get('repo') != c['repo'] or auth.get('base_branch') != c['base_branch'] or
            auth.get('host') != 'github.com' or auth.get('destination') != 'github-releases' or
            auth.get('mode') not in ('draft', 'publish')):
        raise Halt('release.authority must explicitly bind authorization ID, repository, base, github.com destination and draft/publish mode')
    if cfg.get('trigger') == 'version-file' and not auth:
        raise Halt('Automatic release trigger requires separately authorized release scope')


class _ReleaseCheckFailed(Halt):
    """A command returned a known failure, with its result already retained."""


def package_qa_inspection(f, card, area, head, base, snapshot, *, at=None):
    from .review_inspection import final_reference, qualified

    if snapshot is None or not qualified(f, 'release', wt=area, revision=head):
        return None
    receipt = final_reference(f, card, area, head, base, release_snapshot=snapshot,
                              role='release-package-qa', at=at)
    return receipt if receipt and receipt['result']['verdict'] == 'pass' else None


def release_provisional_qa(f, card, area, head, base, evidence, *, at=None):
    """Resolve the optional package QA input. Only final review can accept it."""
    composite = evidence.get('provisional_qa')
    bindings = evidence.get('verification_bindings')
    if not composite or not bindings or 'qa' in f.record(card).get('receipts', {}):
        return None
    try:
        path = Path(bindings['path'])
        if path.is_symlink() or checksum(path) != bindings['sha256']:
            return None
        checks = read_json(path)['digests']
        phases = {'artifact': evidence['artifact_checks']}
        if 'preflight_checks' in evidence:
            phases['preflight'] = evidence['preflight_checks']
        expected_checks = {'project_receipt_sha256': digest(f.verification_evidence(card, head, base)),
                           **{phase + '_receipt_sha256': digest(value) for phase, value in phases.items()}}
        if (checks != composite['verification'] or checks != expected_checks or
                bindings['sha256'] != composite['verification_bindings_sha256'] or
                not any(value['checks'] for value in phases.values()) or
                any(value['state'] != 'passed' for value in phases.values())):
            return None
        for value in phases.values():
            for check in value['checks']:
                path = Path(check['log'])
                if check['exit_code'] != 0 or path.is_symlink() or checksum(path) != check['log_sha256']:
                    return None
        inspection = package_qa_inspection(f, card, area, head, base, evidence.get('inspection_snapshot'), at=at)
        return inspection if inspection and inspection['receipt_sha256'] == composite['inspection_sha256'] else None
    except (OSError, ValueError, KeyError, TypeError, Halt):
        return None


class ReleaseTriggerPending(Halt):
    """Known release-only prerequisite before any publication or ticket mutation."""


class ReleaseTriggerReadReset(ReadConnectionReset):
    """Automatic trigger preflight reset with no unfinished release journal."""


def parse_version(value, *, automatic=False):
    if isinstance(value, str) and len(value) <= 80 and re.fullmatch(VERSION, value):
        return value
    if automatic:
        raise ReleaseTriggerPending('The version file does not contain a supported release version')
    raise Halt('Choose an explicit semantic version or configure a plain-text version_file; no inferred breaking-change policy')


class UnfinishedReleaseCreation(Halt):
    def __init__(self, path, journal, *, message='A frozen release is unfinished'):
        self.marker = 'sfc-release-creation:' + digest(journal) + '\n'
        version = journal.get('version')
        action = 'Inspect the retained release and ticket receipts'
        if (isinstance(version, str) and len(version) <= 80 and re.fullmatch(VERSION, version)
                and path.stem == version):
            action += (f'; from the owning checkout with the original configuration, run '
                       f'release-request --version {version} --recover-creation for readback')
        super().__init__(f'{message} ({path}). {action}')

    def latch(self, stop_path):
        # An owned stop is tagged only at first creation. Never overwrite an
        # existing stop or tag an unrelated halt as release recovery.
        try:
            with stop_path.open('x', encoding='utf-8') as stop:
                stop.write(self.marker)
        except FileExistsError:
            pass


def guard_release_creation(root):
    """A creation can have no Project card and outlives release settings."""
    for path in sorted((common_dir(root) / 'sfc-releases').glob('*.json')):
        journal = read_json(path)
        if journal.get('state') == 'creating':
            raise UnfinishedReleaseCreation(path, journal)


class Releases:
    def __init__(self, factory, adapter=None):
        self.f = factory
        self.c = factory.c
        self.cfg = self.c.get('release', {})
        self.remote = adapter or ReleaseGitHub(factory.gh)
        self.remote.processes = factory.processes
        self.directory = common_dir(factory.root) / 'sfc-releases'
        self.journal = None
        self.path = None

    def enabled(self):
        if not self.cfg.get('enabled'):
            raise Halt('Release automation is off; explicitly configure and authorize this project')

    def save(self):
        self.journal['updated'] = now()
        write_json(self.path, self.journal)

    def checked(self, *, publication=False, observe_draft=False, guard=None):
        guard = guard or self.f.ensure_running
        guard()
        self.enabled()
        permission_denied = None
        try:
            identity = self.remote.identity()
        except ReleasePushPermissionMissing as exc:
            identity = exc.identity
            permission_denied = exc
        if self.journal and (self.journal['identity'] != identity or
                             self.journal['config_digest'] != self.f.config_digest):
            raise Halt('Release configuration/identity changed; retained candidate and evidence require explicit reconciliation')
        auth = self.cfg.get('authority') or {}
        if any(key in auth and auth[key] != identity.get(key) for key in AUTHORITY_NODE_IDS):
            raise Halt('Reviewed release repository or Project identity changed; preview the scope again')
        if publication:
            if not auth or any(auth[k] != identity[k] for k in AUTHORITY_SCOPE):
                raise Halt('No separately scoped release authority; merge/Ready/watch/labels grant none')
        if permission_denied is not None and (publication or not observe_draft):
            guard()
            raise permission_denied
        if publication and self.journal:
            candidate = self.journal['candidate']
            base = self.f.fetch()
            if git(self.f.root, 'merge-base', candidate, base) != candidate:
                raise Halt('Release candidate no longer belongs to the authorized merged base')
        if self.journal and self.journal.get('card'):
            current = self.remote.pinned_card(self.journal['card'])
            if (any(current.get(k) != v for k, v in self.journal['card'].items()) or
                    self.f.task_digest(current) != self.journal['task_digest']):
                raise Halt('Release card identity/content changed; preserving human edits')
            if observe_draft and (current['status'] != 'Review' or current['state'] != 'OPEN'):
                raise Halt('Unpublished release handoff moved or closed externally; inspect the retained draft')
            if publication and (current['status'] != 'Review' or current['state'] != 'OPEN'):
                completion = any(k in self.journal['effects'] for k in ('close', 'stage-Done'))
                if not completion or current['status'] not in ('Review', 'Done') or current['state'] != 'CLOSED':
                    raise Halt('Release card moved/closed externally; publication authority is stale')
        guard()
        return identity

    def effect(self, key, read, ready, mutate, *, publication=False, before=None, adopt=False):
        """Intent before one write; restart/ambiguous outcomes use readback only."""
        # Remote authorization is refreshed at the write boundary. Local
        # stop/config checks also guard reads, including readback-only recovery.
        guard = self.f.ensure_running if publication else self.checked
        guard()
        operations = self.journal.setdefault('effects', {})
        op = operations.get(key)
        current = read()
        if op:
            if ready(current):
                if (op.get('state') == 'verified' and isinstance(current, dict) and
                        'id' in current and current['id'] != op.get('observed', {}).get('id')):
                    raise Halt('Release resource identity was replaced: ' + key)
                op.update(state='verified', observed=current)
                self.save()
                return current
            raise Halt('Release operation ' + key + ' is unresolved or externally edited; readback only, no repeat write')
        if ready(current):
            # Externally existing resources must not be adopted as owned writes.
            if not adopt:
                raise Halt('Conflicting existing release resource: ' + key)
            operations[key] = {'state': 'verified', 'observed': current, 'adopted': True}
            self.save()
            return current
        if before is not None and not before(current):
            raise Halt('Release preflight changed; preserve manual edit: ' + key)
        if before is None and current is not None:
            raise Halt('Conflicting existing release resource: ' + key)
        operations[key] = {'state': 'intent', 'before': current, 'time': now()}
        self.save()
        guard()
        # Read again immediately before the write. GitHub offers no field CAS.
        if read() != current:
            raise Halt('Release preflight changed after intent; no write: ' + key)
        self.checked(publication=publication)
        try:
            mutate()
        except Exception as exc:
            operations[key]['error_type'] = type(exc).__name__
            self.save()
            # Do not suppress stop/deadline to complete a remote write.
            if isinstance(exc, Stopped):
                raise
        guard()
        observed = read()
        operations[key]['observed'] = observed
        if not ready(observed):
            self.save()
            raise Halt('Release operation ' + key + ' uncertain; preserve journal and reconcile by readback')
        operations[key]['state'] = 'verified'
        self.save()
        return observed

    def previous(self, candidate, ignore_version=None):
        boundaries = []
        versions = [release['tag_name'] for release in self.remote.releases()
                    if not release.get('draft') and release['tag_name'] != ignore_version]
        if len(set(versions)) != len(versions):
            raise Halt('Published release version is ambiguous')
        sources = self.remote.tag_sources(versions)
        for sha in dict.fromkeys(sources.get(version) for version in versions):
            if not sha or not re.fullmatch(SHA, sha):
                raise Halt('Published release source boundary is unavailable')
            # Object IDs are immutable. Only fetch objects absent locally.
            if run(['git', 'cat-file', '-e', sha + '^{commit}'], self.f.root, check=False).returncode:
                git(self.f.root, 'fetch', '--no-tags', 'origin', sha)
            if git(self.f.root, 'merge-base', sha, candidate) != sha:
                raise Halt('Published source is outside candidate ancestry; explicit source reconciliation required')
            boundaries.append(sha)
        boundary = None
        for sha in boundaries:
            if boundary is None or git(self.f.root, 'merge-base', boundary, sha) == boundary:
                boundary = sha
            elif git(self.f.root, 'merge-base', boundary, sha) != sha:
                raise Halt('Published release boundaries diverge; cannot infer previous source')
        return boundary

    def source(self):
        candidate = self.f.fetch()
        if not re.fullmatch(SHA, candidate):
            raise Halt('Candidate must be an exact merged source commit')
        boundary = self.previous(candidate)
        commits = git(self.f.root, 'rev-list', '--reverse', '--topo-order',
                      f'{boundary}..{candidate}' if boundary else candidate).splitlines()
        if not commits:
            raise Halt('No unpublished source changes in candidate')
        return boundary, candidate, self.remote.changes(commits)

    def select_version(self, candidate, explicit=None, *, automatic=False):
        if explicit is None and (path := self.cfg.get('version_file')):
            try:
                source = run(['git', 'show', candidate + ':' + path], self.f.root, check=False)
            except UnicodeDecodeError as exc:
                if automatic:
                    raise ReleaseTriggerPending('The version file is not valid plain text') from exc
                raise Halt('The pinned release version file must contain plain text') from exc
            if source.returncode:
                if automatic and not git(self.f.root, '--literal-pathspecs', 'ls-tree', '--full-tree', candidate, '--', path):
                    raise ReleaseTriggerPending('The configured version file is missing from the merged source')
                raise Halt('The release version file could not be read from the pinned source')
            explicit = source.stdout.strip()
        return parse_version(explicit, automatic=automatic)

    @staticmethod
    def settled_draft(journal):
        effects = journal.get('effects') or {}
        return (journal['state'] == 'draft-unpublished' and bool(journal.get('release_id')) and
                effects.get('draft', {}).get('state') == 'verified' and
                all(effect.get('state') == 'verified' for effect in effects.values()) and
                not {'publication', 'close', 'stage-Done'} & effects.keys())

    def unfinished_release(self, identity, *, automatic, push_denied=False):
        draft_pending = False
        for path in self.directory.glob('*.json'):
            prior = read_json(path)
            if prior.get('state') == 'done':
                continue
            if automatic and prior.get('state') == 'draft-unpublished' and prior.get('identity') == identity:
                if not push_denied or (self.settled_draft(prior) and
                                       prior.get('config_digest') == self.f.config_digest and
                                       prior.get('owner_root', str(self.f.root)) == str(self.f.root)):
                    draft_pending = True
                    continue
            raise Halt('Another frozen release is unfinished; resolve its preserved journal first')
        return draft_pending

    def request(self, version=None, *, automatic=False):
        """Caller holds the common supervisor lock. Stable version deduplicates triggers."""
        self.enabled()
        identity = self.checked(publication=automatic)
        explicit_version = version
        candidate = self.f.fetch() if explicit_version is None else None
        version = self.select_version(candidate, explicit_version, automatic=automatic)
        self.path = self.directory / (version + '.json')
        if self.path.exists():
            saved = read_json(self.path)
            if saved.get('identity') != identity:
                raise Halt('Release version already belongs to another scope')
            if saved.get('state') == 'creating':
                self.journal = saved
                self.recover_creation()
                saved = self.journal
            return {'version': version, 'state': saved['state'], 'card': saved.get('card'),
                    'deduplicated': True, 'receipt': str(self.path)}
        if candidate is None:
            candidate = self.f.fetch()
        draft_pending = self.unfinished_release(identity, automatic=automatic)
        release = self.remote.release(version)
        if release is not None and not automatic:
            raise Halt('Existing conflicting version/tag; never replace or adopt it')
        tag = self.remote.tag(version)
        if release is not None or tag is not None:
            if (automatic and release is not None and release.get('tag_name') == version and
                    release.get('draft') is False and release.get('published_at') and
                    isinstance(tag, str) and re.fullmatch(SHA, tag) and
                    run(['git', 'merge-base', '--is-ancestor', tag, candidate], self.f.root, check=False).returncode == 0):
                raise ReleaseTriggerPending('The current version is already published; waiting for a version change')
            raise Halt('Existing conflicting version/tag; never replace or adopt it')
        if draft_pending:
            raise ReleaseTriggerPending('A previous release draft is awaiting a publication decision')
        request_id = 'release-' + digest({'repository': identity['repository_id'], 'version': version})[:24]
        existing = self.remote.release_issues(version, request_id)
        if existing:
            raise Halt('Release version already has a GitHub issue; recover its owning journal: ' +
                       ', '.join(str(number) for number in existing))
        boundary, candidate, changes = self.source()
        # Read version again from the actual cutoff if a base moved during preparation.
        if version != self.select_version(candidate, explicit_version, automatic=automatic):
            raise Halt('Release version changed during candidate selection')
        artifact_allowlist(self.cfg['artifacts'], version)
        self.journal = {'workflow_kind': 'release', 'owner_root': str(self.f.root),
                        'request_id': request_id, 'version_from_file': explicit_version is None,
                        'version': version, 'candidate': candidate,
                        'boundary': boundary, 'changes': changes, 'identity': identity,
                        'config_digest': self.f.config_digest, 'state': 'creating', 'created': now(), 'effects': {}}
        # Local input failures must not leave a creating journal without a ticket
        # intent, which cannot safely be retried as an external creation.
        from .release_notes import MissingReleaseSummary
        try:
            self.freeze_notes(persist=False)
        except MissingReleaseSummary as exc:
            if not automatic:
                raise
            raise ReleaseTriggerPending('An approved public release summary is required; configure '
                                        'release.approved_summaries for the pinned source') from exc
        try:
            self.freeze_latest(persist=False)
        except ReadConnectionReset:
            # Latest planning only reads GitHub. Ticket creation starts after save.
            self.journal = None
            raise
        self.save()
        from .priority import create_ticket
        if (self.cfg.get('authority') or {}).get('mode') == 'draft':
            completion = ('- Verify the requested unpublished GitHub draft, pinned source tag and downloaded '
                          'checksums. Leave the release in Review pending separate publication authority.\n')
        else:
            completion = '- Verify the requested GitHub publication, source tag and downloaded checksums before Done.\n'
        body = self.notes(planned=True) + '\n## Acceptance criteria\n' + (
            '- Package the pinned merged source with configured commands.\n'
            '- Verify allowlisted artifact bytes, project checks and independent QA/security/review.\n'
            + completion)
        # Existing creation protocol owns issue/enrollment/Priority/work-type writes.
        result = create_ticket(self.f.gh, self.f.root, request_id=self.journal['request_id'], workflow_kind='release',
                               title='Release ' + version, body=body, priority='P2', work_type='Maintenance',
                               reason='Explicit optional release workflow for the frozen merged source range.',
                               unknowns='Publication remains subject to separate scoped authority and verified artifacts.')
        card = self.remote.card(result['issue']['number'])
        self.journal.update(card={k: card[k] for k in ('number', 'id', 'url', 'item_id', 'repository')},
                            task_digest=self.f.task_digest(card), state='planned')
        self.save()
        self.f.update(card, workflow_kind='release', release_version=version)
        self.assign_members(card)
        return {'version': version, 'candidate': candidate, 'boundary': boundary, 'card': self.journal['card'],
                'state': 'planned', 'receipt': str(self.path)}

    def reconcile_creation(self, version):
        """Explicit completion of local binding; no creation or board writes."""
        version = parse_version(version)
        self.path = self.directory / (version + '.json')
        if not self.path.is_file():
            raise Halt('No retained release creation for this exact version')
        self.journal = read_json(self.path)
        marker = UnfinishedReleaseCreation(self.path, self.journal).marker.encode('utf-8')
        stop, shared = self.f.processes.stop_path, common_dir(self.f.root) / 'sfc-stop'

        def guard():
            # Keep the latch. Only this readback may pass a stop created for
            # this exact journal; an operator stop still cancels it.
            self.f.ensure_config_unchanged()
            try:
                with stop.open('rb') as flag:
                    local_stop = flag.read(len(marker) + 1)
            except FileNotFoundError:
                local_stop = None
            if shared.exists() or (local_stop is not None and local_stop != marker):
                raise Stopped('An operator or unrelated stop is recorded; inspect and explicitly acknowledge that stop before release recovery')

        self.recover_creation(guard=guard)
        return {'version': version, 'state': self.journal['state'], 'card': self.journal['card'],
                'deduplicated': True, 'receipt': str(self.path)}

    def creation_scope(self, version):
        if (self.journal.get('state') != 'creating' or self.journal.get('workflow_kind') != 'release' or
                self.journal.get('version') != version or self.journal.get('owner_root') != str(self.f.root) or
                self.journal.get('config_digest') != self.f.config_digest):
            raise Halt('Release creation recovery requires the exact retained version, owning checkout and configuration')

    def recover_creation(self, *, guard=None):
        from .ticket_coordination import MARKER, REQUEST_ID, recorded_request, readiness
        if guard is None:
            guard = self.f.ensure_running
        self.creation_scope(self.path.stem)
        self.checked(guard=guard)
        request = self.journal.get('request_id')
        if not isinstance(request, str) or not re.fullmatch(REQUEST_ID, request):
            raise Halt('Release creation request ID is unavailable; preserve the retained journal')
        path = recorded_request(self.f.root, request)
        if not path:
            raise Halt('Release creation intent has no ticket receipt; preserve it for explicit reconciliation; no POST repeated')
        receipt = read_json(path)
        if receipt.get('state') != 'complete' or receipt.get('workflow_kind') != 'release' or not receipt.get('issue'):
            raise Halt('Release ticket creation incomplete; inspect creation readback/receipt; no POST repeated')
        card = self.remote.card(receipt['issue']['number'])
        problem = readiness(self.f.root, card, self.c['repo'], self.journal['identity']['project'])
        if (problem or MARKER.findall(card.get('body') or '') != [request] or
                card['title'] != 'Release ' + self.journal['version'] or
                card['title'] != receipt['title'] or card['body'] != receipt['body']):
            raise Halt('Release creation readback identity/content unverified; no POST repeated')
        guard()
        self.journal.update(card={k: card[k] for k in ('number', 'id', 'url', 'item_id', 'repository')},
                            task_digest=self.f.task_digest(card), state='planned')
        self.save()
        self.f.update(card, workflow_kind='release', release_version=self.journal['version'])

    def notes(self, planned=False):
        j = self.journal
        lines = ['Planned target membership; publication is not yet verified.' if planned else 'Verified source-range release.',
                 '', 'Version: ' + j['version'], 'Source: ' + j['candidate'],
                 'Previous published source: ' + (j['boundary'] or 'none (complete initial history)'), '',
                 'Included merged pull requests:']
        lines += ['- #' + str(p['number']) for p in j['changes']['pull_requests']] or ['- None']
        lines += ['', 'Included tickets:']
        lines += ['- #' + str(n) for n in j['changes']['issues']] or ['- None']
        lines += ['', 'All included commits:']
        lines += ['- ' + m['commit'] for m in j['changes']['commits']]
        lines += ['', 'Unlinked commits (included in the package):']
        lines += ['- ' + sha for sha in j['changes']['unlinked_commits']] or ['- None']
        return '\n'.join(lines) + '\n'

    def freeze_notes(self, persist=True):
        from .release_notes import freeze, validate
        if 'public_notes' not in self.journal:
            self.journal['public_notes'] = freeze(self.f.root, self.journal, self.cfg.get('public_docs', {}),
                                                self.cfg.get('approved_summaries'))
            if persist:
                self.save()
        validate(self.journal)

    def public_notes(self):
        if 'public_notes' in self.journal:
            from .release_notes import validate
            return validate(self.journal)
        # Legacy reviewed/manifest evidence is immutable. Never silently
        # regenerate its inventory or invalidate already spent lane receipts.
        return self.notes()

    def freeze_latest(self, persist=True):
        from .release_latest import freeze
        if 'latest_plan' not in self.journal:
            self.journal['latest_plan'] = freeze(self.remote, self.journal['version'],
                                                self.cfg.get('latest_policy', 'newer-stable'))
            if persist:
                self.save()

    def setup_field(self):
        field = self.effect('target-field', self.remote.fields, lambda f: bool(f), self.remote.create_field,
                            before=lambda f: f is None, adopt=True)
        initial = self.journal.setdefault('field_identity', field)
        self.save()
        if initial != field:
            raise Halt('Target release field identity changed')
        view = self.remote.view()
        pinned = self.journal.setdefault('factory_view', view)
        self.save()
        visible = [v['id'] for v in pinned['configuration']['visibleFields']['nodes']]
        wanted = visible + ([field['id']] if field['id'] not in visible else [])
        def view_ready(value):
            expected = json.loads(json.dumps(pinned))
            expected['configuration']['visibleFields']['nodes'] = [{'id': v} for v in wanted]
            return value == expected
        self.effect('target-visibility', self.remote.view, view_ready, lambda: self.remote.visible(pinned['id'], wanted),
                    before=lambda v: v == pinned, adopt=True)
        return field

    def assign_members(self, card):
        field = self.setup_field()
        numbers = sorted(set(self.journal['changes']['issues'] + [card['number']]))
        items = self.journal.setdefault('members', {})
        for number in numbers:
            current = self.remote.card(number)
            expected = items.setdefault(str(number), {k: current[k] for k in ('number', 'id', 'url', 'item_id', 'repository')})
            self.save()
            if any(current[k] != v for k, v in expected.items()):
                raise Halt('Included card identity changed')
            key = 'target-' + str(number)
            before = self.remote.item(current, field)
            saved_before = self.journal.setdefault('member_before', {}).setdefault(str(number), before)
            self.save()
            target = {**saved_before, 'value': self.journal['version']}
            self.effect(key, lambda: self.remote.item(current, field), lambda v: v == target,
                        lambda: self.remote.assign(current, field, self.journal['version']),
                        before=lambda v: v == saved_before and v['value'] in (None, self.journal['version']), adopt=True)
        wanted = {v['item_id'] for v in items.values()}
        actual = {v['id'] for v in self.remote.filtered(self.journal['version'])}
        if not wanted <= actual:
            raise Halt('Saved Target release filter membership unverified; inspect supported Projects filter/readback')
        if self.remote.filtered(self.journal['version'] + '-sfc-absent-' + digest(self.pins())[:12]):
            raise Halt('Project release filter was ignored; cannot verify version filtering')
        # Extra manual assignments are disclosed, never erased.
        self.journal['filter_readback'] = {'query': 'target-release:"' + self.journal['version'] + '"',
                                           'items': sorted(actual), 'extra_items': sorted(actual - wanted)}
        self.save()

    def load(self, card, *, allow_draft_observation=False):
        record = self.f.record(card)
        version = record.get('release_version')
        if record.get('workflow_kind') != 'release' or not isinstance(version, str) or not re.fullmatch(VERSION, version):
            raise Halt('No supervisor-validated release workflow identity')
        self.path = self.directory / (version + '.json')
        self.journal = read_json(self.path)
        if self.journal.get('owner_root', str(self.f.root)) != str(self.f.root):
            raise Halt('Release belongs to another checkout; resume its original owner without resetting budgets')
        observe_draft = allow_draft_observation and self.settled_draft(self.journal)
        self.checked(observe_draft=observe_draft)
        if (self.journal.get('workflow_kind') != 'release' or self.journal.get('version') != version or
                self.journal.get('card') != {
                k: card[k] for k in ('number', 'id', 'url', 'item_id', 'repository')} or
                self.journal.get('task_digest') != self.f.task_digest(card)):
            raise Halt('Release card/task changed; frozen candidate evidence cannot be reused')
        # Later merges are allowed, but a rewrite removing the candidate is not.
        base = self.f.fetch()
        if git(self.f.root, 'merge-base', self.journal['candidate'], base) != self.journal['candidate']:
            raise Halt('Frozen release candidate is no longer on the configured merged base')
        return observe_draft

    def attempt_directory(self):
        root = self.directory / self.journal['version']
        if correction := self.journal.get('last_correction'):
            attempt = correction['attempt']
            if type(attempt) is not int or not 2 <= attempt <= self.c['max_attempts']:
                raise Halt('Invalid release correction attempt directory')
            return root / f'correction-{attempt}'
        return root

    def move(self, card, status):
        # Narrow status writes get the same durable uncertainty latch as publication.
        before = self.remote.card(card['number'])['status']
        op = self.journal['effects'].get('stage-' + status)
        if not op and (before != card['status'] or before not in {
                'Building': ('Ready', 'Building'), 'QA': ('Building',), 'Review': ('QA',),
                'Blocked': ('Building', 'QA', 'Review'), 'Done': ('Review',)}[status]):
            raise Halt('Release stage changed externally; preserve manual edit')
        self.effect('stage-' + status, lambda: self.remote.card(card['number'])['status'],
                    lambda value: value == status, lambda: self.f.gh.move(card, status),
                    before=lambda value: value == before)
        card['status'] = status
        self.f.update(card, status=status)

    def commands(self, card, commands, phase, worktree):
        key = phase + '_checks'
        folder = self.attempt_directory() / phase
        if key in self.journal:
            receipt = self.journal[key]
            if (receipt.get('state') != 'passed' or receipt.get('candidate') != self.journal['candidate'] or
                    receipt.get('config_digest') != self.f.config_digest or
                    len(receipt.get('checks', [])) != len(commands) or any(
                        check.get('check') != index or check.get('command') != command or
                        check.get('exit_code') != 0 or check.get('log') != str(folder / f'check-{index}.log')
                        for index, (check, command) in enumerate(zip(receipt.get('checks', []), commands), 1))):
                raise Halt('Release ' + phase + ' previously failed/interrupted; inspect logs without repeated paid work')
            for check in receipt['checks']:
                log = Path(check['log'])
                if log.is_symlink() or not log.is_file() or checksum(log) != check.get('log_sha256'):
                    raise Halt('Missing/changed pinned release ' + phase + ' log')
            return receipt
        receipt = {'state': 'reserved', 'candidate': self.journal['candidate'],
                   'config_digest': self.f.config_digest, 'checks': [], 'started': now()}
        if phase == 'preflight':
            receipt['artifact_digest'] = digest(self.journal['artifacts'])
            receipt['check_plan_sha256'] = digest(self.journal['check_plan'])
        tick = time.monotonic()
        self.journal[key] = receipt
        self.save()
        try:
            for index, command in enumerate(commands, 1):
                self.checked()
                log = folder / f'check-{index}.log'
                code = self.f.execute(card, 'build' if phase == 'package' else 'checks',
                                      ['sh', '-c', command], worktree, log, self.c['verify_timeout'],
                                      accounting={'head': self.journal['candidate'],
                                                  'base': self.journal['boundary'] or self.journal['candidate'],
                                                  'release_phase': phase},
                                      observer=self.f.activity.observe(card, 'checks'))
                receipt['checks'].append({'check': index, 'command': command, 'exit_code': code, 'log': str(log),
                                          'log_sha256': checksum(log)})
                self.save()
                if code:
                    receipt['state'] = 'failed'
                    raise _ReleaseCheckFailed('Release ' + phase + ' check failed; inspect retained logs and candidate')
                if phase == 'preflight':
                    try:
                        self.validate_artifacts()
                    except Halt:
                        receipt['state'] = 'failed'
                        raise
            receipt['state'] = 'passed'
        finally:
            receipt.update(finished=now(), elapsed_seconds=time.monotonic() - tick)
            if phase == 'preflight' and receipt['state'] == 'passed':
                self.journal['preflight_receipt_sha256'] = digest(receipt)
            self.save()
        return receipt

    def artifact_paths(self):
        return self.attempt_directory() / 'artifacts'

    def artifact_inputs(self):
        return artifact_allowlist(self.cfg['artifacts'], self.journal['version'])

    def artifacts(self):
        result = []
        for path in self.artifact_inputs():
            file = self.artifact_paths() / Path(path).name
            if file.is_symlink() or not file.is_file():
                raise Halt('Verified release artifact missing or replaced')
            result.append({'name': file.name, 'size': file.stat().st_size, 'sha256': checksum(file)})
        return result

    def pins(self):
        return {k: self.journal[k] for k in ('version', 'boundary', 'candidate', 'changes', 'identity', 'card', 'config_digest')}

    def check_plan(self):
        preflight = self.cfg.get('preflight_commands', [])
        return {'package': {'commands': self.cfg['package_commands'], 'requires': ['candidate']},
                'preflight': {'commands': preflight, 'requires': ['package']},
                'project': {'commands': self.c['verify_commands'], 'requires': ['preflight' if preflight else 'package']},
                'artifact': {'commands': self.cfg['verify_commands'], 'requires': ['project']},
                'migration': 'Explicit preflight commands depend only on packaging; existing artifact commands retain their post-project order'}

    def preflight(self, card, area):
        j = self.journal
        try:
            self.commands(card, self.cfg['preflight_commands'], 'preflight', area)
            self.check_preflight()
        except BaseException:
            state = j.get('preflight_checks', {}).get('state')
            status = {'failed': 'failed', 'passed': 'invalidated'}.get(state, 'interrupted')
            if j.get('preflight_outcome', {}).get('status') in (None, 'passed'):
                record = self.f.record(card)
                review_prepared = 'release_evidence' in record or bool(record.get('review_inspections'))
                j['preflight_outcome'] = {
                    'status': status,
                    'project_check_commands_not_started': len(self.c['verify_commands'])
                    if 'project_check_state' not in j else 0,
                    'independent_lanes_not_started': [] if review_prepared else ['qa', 'security', 'review']}
                self.save()
            raise
        if 'preflight_outcome' not in j:
            j['preflight_outcome'] = {'status': 'passed', 'project_check_commands_not_started': 0,
                                      'independent_lanes_not_started': []}
            self.save()

    def prepare(self, card):
        j = self.journal
        artifact_inputs = self.artifact_inputs()
        if not j.get('manifest_sha256'):
            self.freeze_notes()
            self.freeze_latest()
        if 'public_notes' not in j or 'latest_plan' not in j:
            raise Halt('Legacy unreviewed presentation requires explicit reconciliation; original evidence retained')
        self.public_notes()
        area = self.attempt_directory() / 'candidate'
        if not j.get('candidate_area'):
            if area.exists():
                raise Halt('Unowned release candidate area exists; preserve it')
            j['candidate_area'] = str(area)
            self.save()
            git(self.f.root, 'worktree', 'add', '--detach', area, j['candidate'])
        if not area.is_dir() or git(area, 'rev-parse', 'HEAD') != j['candidate']:
            raise Halt('Pinned candidate worktree unavailable; do not rebuild a different source')
        if 'artifacts' not in j:
            # Reserve before starting packaging. Crash/restart cannot repeat it blindly.
            self.commands(card, self.cfg['package_commands'], 'package', area)
            if git(area, 'diff', 'HEAD'):
                raise Halt('Packaging modified tracked source; candidate evidence invalid')
            self.artifact_paths().mkdir(parents=True, exist_ok=True)
            for relative in artifact_inputs:
                source = area / relative
                if (source.is_symlink() or not source.is_file() or area.resolve() not in source.resolve().parents or
                        any(p.is_symlink() for p in source.parents if p != area)):
                    raise Halt('Allowlisted artifact missing or symlinked; inspect packaging/signing gap')
                shutil.copyfile(source, self.artifact_paths() / source.name)
            j['artifacts'] = self.artifacts()
            self.save()
        self.validate_artifacts()
        plan = self.check_plan()
        if 'check_plan' not in j:
            j['check_plan'] = plan
            self.save()
        if j['check_plan'] != plan:
            raise Halt('Pinned release check dependencies changed')
        if self.cfg.get('preflight_commands'):
            self.preflight(card, area)
        self.verify_prepared(card, area)
        return area

    def required_checks(self, card, area):
        j = self.journal
        if 'project_check_state' not in j:
            # Reserve before invoking project code, just as packaging/artifact
            # checks do. A crash before Factory.verify saves its receipt must
            # not silently buy another check run on resume.
            j['project_check_state'] = 'reserved'
            self.save()
            previous = {'prior_receipt': j['prior_verification']} if j.get('prior_verification') else {}
            if not self.f.verify(card, area, j['boundary'] or j['candidate'], **previous):
                j['project_check_state'] = 'failed'
                self.save()
                raise _ReleaseCheckFailed('Required project checks failed for release candidate')
        if not self.f.record(card).get('verification'):
            raise Halt('Release project checks interrupted; inspect retained reservation/logs; no automatic repeat')
        self.f.verification_evidence(card, j['candidate'], j['boundary'] or j['candidate'])
        if j['project_check_state'] == 'failed':
            raise Halt('Required project checks previously failed; inspect retained candidate and logs')
        # A successful pinned receipt can reconcile a crash after verify saved
        # it but before this journal advanced. No command is run again.
        j['project_check_state'] = 'passed'
        self.save()
        project_logs = [{'sha256': checksum(c['log']), 'log': c['log']}
                        for c in self.f.record(card)['verification']['checks']]
        if 'project_logs' in j and j['project_logs'] != project_logs:
            raise Halt('Pinned project verification logs changed')
        j['project_logs'] = project_logs
        self.commands(card, self.cfg['verify_commands'], 'artifact', area)
        self.validate_artifacts()
        self.check_logs()

    def verify_prepared(self, card, area):
        from .review_inspection import configured
        from .review_policy import release_source_coverage

        j = self.journal
        artifacts = [{'path': str(self.artifact_paths() / a['name']), **a} for a in j['artifacts']]
        snapshot = {**self.pins(), 'public_notes': j['public_notes'], 'latest_plan': j['latest_plan'],
                    'artifacts': artifacts}
        inspect = configured(self.f, 'release')
        base = j['boundary'] or j['candidate']
        if inspect and (j.get('project_check_state') != 'passed' or j.get('artifact_checks', {}).get('state') != 'passed'):
            failure = None

            def check():
                nonlocal failure
                try:
                    self.required_checks(card, area)
                except _ReleaseCheckFailed as exc:
                    failure = exc
                    return False
                return True

            self.f.overlap_review(card, area, base, check, release_snapshot=snapshot, role='release-package-qa')
            if failure is not None:
                raise failure
        else:
            self.required_checks(card, area)
        if 'source_coverage' in j or 'manifest_sha256' not in j:
            coverage = release_source_coverage(self.f, j)
            if 'source_coverage' in j and j['source_coverage'] != coverage:
                raise Halt('Pinned release source review evidence changed')
            if coverage and 'source_coverage' not in j:
                j['source_coverage'] = coverage
                self.save()
        # Only structured, bounded identifiers/hashes become public. Logs/commands stay local.
        manifest = {**self.pins(), 'schema': 1, 'artifacts': j['artifacts'],
                    'verification': {'project_receipt_sha256': digest(self.f.record(card)['verification']),
                                     'artifact_receipt_sha256': digest(j['artifact_checks'])}}
        if 'preflight_checks' in j:
            manifest['verification']['preflight_receipt_sha256'] = j['preflight_receipt_sha256']
        bindings = self.verification_bindings(card, manifest['verification'])
        if inspect and bindings and 'provisional_qa' not in j and not j.get('manifest_sha256'):
            receipt = package_qa_inspection(self.f, card, area, j['candidate'], base, snapshot)
            if receipt:
                j['provisional_qa'] = {'inspection_sha256': receipt['receipt_sha256'],
                                       'verification_bindings_sha256': bindings['sha256'],
                                       'verification': manifest['verification']}
                self.save()
        if 'public_notes' in j:
            manifest['public_notes'] = j['public_notes']
        manifest['latest_plan'] = j['latest_plan']
        path = self.artifact_paths() / 'sfc-release-manifest.json'
        if j.get('manifest_sha256'):
            if not path.is_file() or checksum(path) != j['manifest_sha256'] or read_json(path) != manifest:
                raise Halt('Release manifest identity changed')
        else:
            write_json(path, manifest)
            j['manifest_sha256'] = checksum(path)
            self.save()
        self.f.update(card, worktree=str(area), release_evidence={
            'workflow_kind': 'release', **self.pins(), 'manifest': str(path), 'manifest_sha256': j['manifest_sha256'],
            'release_notes': self.public_notes(),
            'latest_plan': j['latest_plan'],
            **({'public_notes': j['public_notes']} if 'public_notes' in j else {}),
            'artifacts': artifacts,
            'artifact_checks': j['artifact_checks'],
            **({'verification_bindings': bindings} if bindings else {}),
            **({'inspection_snapshot': snapshot} if inspect else {}),
            **({'provisional_qa': j['provisional_qa']} if 'provisional_qa' in j else {}),
            **({'source_coverage': j['source_coverage']} if 'source_coverage' in j else {}),
            **({'preflight_checks': j['preflight_checks']} if 'preflight_checks' in j else {})})

    def verification_bindings(self, card, expected, *, validate_only=False):
        """Expose exact private hash inputs; reviewers never infer their encoding."""
        j = self.journal
        ref = j.get('verification_bindings')
        # A previously frozen legacy release keeps its original evidence format.
        if ref is None and (validate_only or j.get('manifest_sha256')):
            return None
        path = self.attempt_directory() / 'verification-bindings.json'
        inputs = {'project_receipt_sha256': self.f.record(card)['verification'],
                  'artifact_receipt_sha256': j['artifact_checks']}
        if 'preflight_checks' in j:
            inputs['preflight_receipt_sha256'] = j['preflight_checks']
        if {key: digest(value) for key, value in inputs.items()} != expected:
            raise Halt('Release manifest verification inputs changed')
        value = {'schema': 1, 'encoding': 'SHA256 of UTF-8 json.dumps(value, sort_keys=True); default separators and ensure_ascii=True',
                 'inputs': inputs, 'digests': expected}
        if ref is None:
            if path.exists() or path.is_symlink():
                if path.is_symlink() or read_json(path) != value:
                    raise Halt('Uncommitted release verification binding differs')
            else:
                write_json(path, value)
            ref = {'path': str(path), 'sha256': checksum(path)}
            j['verification_bindings'] = ref
            self.save()
        if (ref != {'path': str(path), 'sha256': ref.get('sha256')} or path.is_symlink()
                or not path.is_file() or checksum(path) != ref['sha256'] or read_json(path) != value):
            raise Halt('Frozen release verification bindings changed')
        return ref

    def provisional_qa(self, card, area, *, at=None):
        j = self.journal
        evidence = self.f.record(card).get('release_evidence', {})
        return release_provisional_qa(self.f, card, area, j['candidate'], j['boundary'] or j['candidate'],
                                      evidence, at=at)

    @staticmethod
    def validate_composite_result(result, descriptor):
        assessment = result.get('release_qa')
        pins = ('inspection_sha256', 'verification_bindings_sha256')
        observations = ('artifact_evidence', 'installation_evidence', 'acceptance_evidence')
        if (not isinstance(assessment, dict) or set(assessment) != {*pins, *observations} or
                any(assessment[k] != descriptor[k] for k in pins) or
                any(not isinstance(assessment[k], str) or not assessment[k].strip() for k in observations)):
            raise ValueError('Final release review lacks exact package QA pins or completed assessment evidence')

    def validate_artifacts(self):
        if self.artifacts() != self.journal.get('artifacts'):
            raise Halt('Release artifact bytes changed; rebuild/check evidence is invalid')
        area = Path(self.journal['candidate_area'])
        if git(area, 'rev-parse', 'HEAD') != self.journal['candidate'] or git(area, 'diff', 'HEAD'):
            raise Halt('Release source changed; verification invalidated')
        for relative, expected in zip(self.artifact_inputs(), self.journal['artifacts']):
            source = area / relative
            if source.is_symlink() or not source.is_file() or checksum(source) != expected['sha256']:
                raise Halt('Tested candidate artifact bytes changed; verification invalidated')

    def check_preflight(self):
        j = self.journal
        if self.cfg.get('preflight_commands'):
            receipt = j.get('preflight_checks', {})
            if (receipt.get('state') != 'passed' or receipt.get('candidate') != j['candidate'] or
                    receipt.get('config_digest') != self.f.config_digest or
                    receipt.get('artifact_digest') != digest(j['artifacts']) or
                    receipt.get('check_plan_sha256') != digest(self.check_plan()) or
                    digest(receipt) != j.get('preflight_receipt_sha256')):
                raise Halt('Missing/stale pinned release preflight receipt')

    def check_logs(self):
        j = self.journal
        self.check_preflight()
        for check in [*j['project_logs'], *j['artifact_checks']['checks'],
                      *j.get('preflight_checks', {}).get('checks', [])]:
            expected = check.get('sha256', check.get('log_sha256'))
            log = Path(check['log'])
            if log.is_symlink() or not log.is_file() or checksum(log) != expected:
                raise Halt('Missing/changed pinned release verification log')

    def reviewed(self, card):
        j = self.journal
        self.public_notes()
        evidence = self.f.record(card).get('release_evidence', {})
        if (evidence.get('release_notes') != self.public_notes() or
                evidence.get('public_notes') != j.get('public_notes') or
                evidence.get('latest_plan') != j.get('latest_plan')):
            raise Halt('Frozen public release notes differ from reviewed evidence')
        self.f.verification_evidence(card, j['candidate'], j['boundary'] or j['candidate'])
        self.validate_artifacts()
        self.check_logs()
        manifest = self.artifact_paths() / 'sfc-release-manifest.json'
        if (manifest.is_symlink() or not manifest.is_file() or not j.get('manifest_sha256') or
                evidence.get('manifest_sha256') != j['manifest_sha256'] or checksum(manifest) != j['manifest_sha256']):
            raise Halt('Reviewed release manifest bytes changed')
        if self.cfg.get('preflight_commands') and evidence.get('preflight_checks') != j['preflight_checks']:
            raise Halt('Frozen release preflight differs from reviewed evidence')
        if 'verification_bindings' in j or 'verification_bindings' in evidence:
            ref = self.verification_bindings(card, read_json(manifest)['verification'], validate_only=True)
            if ref is None or evidence.get('verification_bindings') != ref:
                raise Halt('Reviewed release verification bindings differ')
        if j.get('provisional_qa') != evidence.get('provisional_qa'):
            raise Halt('Frozen release package QA evidence changed')
        if 'source_coverage' in j or 'source_coverage' in evidence:
            from .review_policy import release_source_coverage
            if j.get('source_coverage') != evidence.get('source_coverage') or j.get('source_coverage') != release_source_coverage(self.f, j):
                raise Halt('Pinned release source review evidence changed')
        from .factory import checks_pass
        checks = self.remote.checks(j['candidate'])
        if not isinstance(checks, list) or not checks_pass({'statusCheckRollup': checks}, self.c['allow_no_checks']):
            raise Halt('Release source GitHub checks missing/incomplete/failing; cannot publish')
        j['source_checks_sha256'] = digest(checks)
        self.save()
        receipts = self.f.record(card).get('receipts', {})
        composite = receipts.get('review', {}).get('provisional_qa')
        if composite is not None:
            self.reviewed_package_qa(card, receipts['review'], evidence)
        for lane in (('security', 'review') if composite is not None else ('qa', 'security', 'review')):
            receipt = receipts.get(lane, {})
            if (receipt.get('head') != j['candidate'] or receipt.get('base') != (j['boundary'] or j['candidate']) or
                    receipt.get('config_digest') != self.f.config_digest or
                    receipt.get('task_digest') != self.f.task_digest(card) or
                    receipt.get('release_digest') != digest(evidence) or
                    receipt.get('result', {}).get('verdict') != 'pass' or receipt['result'].get('findings')):
                raise Halt('Missing/stale release ' + lane + ' evidence')
            if lane == 'review' and composite is None and receipt.get('inspection_sha256'):
                from .review_inspection import final_reference
                inspection = final_reference(self.f, card, Path(j['candidate_area']), j['candidate'],
                                             j['boundary'] or j['candidate'],
                                             release_snapshot=evidence.get('inspection_snapshot'))
                if not inspection or inspection['receipt_sha256'] != receipt['inspection_sha256']:
                    raise Halt('Release inspection evidence used in final review changed')

    def reviewed_package_qa(self, card, receipt, evidence):
        from .review_policy import completed_message
        from .verification import load

        descriptor = evidence.get('provisional_qa')
        try:
            if (receipt['provisional_qa'] != descriptor or
                    receipt['inspection_sha256'] != descriptor['inspection_sha256'] or
                    Path(receipt['receipt_path']).is_symlink() or load(receipt)['kind'] != 'release-review'):
                raise ValueError('package QA inspection or final review scope changed')
            for key, digest_key in (('log', 'log_sha256'), ('result_path', 'result_sha256')):
                path = Path(receipt[key])
                if path.is_symlink() or checksum(path) != receipt[digest_key]:
                    raise ValueError('package QA final review bytes changed')
            result = receipt['result']
            thread, message = completed_message(receipt['log'])
            if read_json(receipt['result_path']) != result or json.loads(message) != result:
                raise ValueError('package QA final review differs from the completed turn')
            self.validate_composite_result(result, descriptor)
            # The saved final assessment had to consume a fresh inspection.
            # On publication recovery, validate its age at that assessment.
            inspection = self.provisional_qa(card, Path(self.journal['candidate_area']), at=receipt['time'])
            if not inspection or thread == inspection['thread']:
                raise ValueError('package QA requires a separate completed final review')
        except (OSError, ValueError, KeyError, TypeError, Halt) as exc:
            raise Halt('Missing/stale final release package QA evidence') from exc

    def metadata_ready(self):
        field = self.remote.fields()
        if field != self.journal.get('field_identity'):
            raise Halt('Target release field changed before publication')
        view = self.remote.view()
        expected = self.journal['effects']['target-visibility']['observed']
        if view != expected:
            raise Halt('Saved Factory visibility/view changed; preserve human edits')
        for identity in self.journal['members'].values():
            current = self.remote.card(identity['number'])
            if any(current.get(k) != v for k, v in identity.items()):
                raise Halt('Included card identity changed before publication')
            if self.remote.item(current, field)['value'] != self.journal['version']:
                raise Halt('Target release assignment edited before publication')
        expected = {v['item_id'] for v in self.journal['members'].values()}
        if not expected <= {v['id'] for v in self.remote.filtered(self.journal['version'])}:
            raise Halt('Saved release filter membership unavailable')

    def publish(self, card):
        from .delivery import observation, command_scope
        self.account_downloads(card)
        with command_scope(self.f, card, 'publication'), observation(self.f, card, 'publication', kind='publication',
                         head=self.journal['candidate'], base=self.journal['boundary'],
                         release_version=self.journal['version']):
            return self._publish(card)

    def account_downloads(self, card):
        self.remote.download_execute = lambda *args, **kwargs: self.f.execute(
            card, 'publication', *args, accounting={'head': self.journal['candidate'],
                'base': self.journal['boundary'], 'command_role': 'asset-download'}, **kwargs)

    def _publish(self, card):
        j = self.journal
        self.checked(publication=True)
        self.reviewed(card)
        self.metadata_ready()
        if self.previous(j['candidate'], j['version']) != j['boundary']:
            raise Halt('Previous published source boundary changed; frozen release is stale')
        expected_notes = self.public_notes()
        tag, sha = j['version'], j['candidate']
        self.effect('tag', lambda: self.remote.tag(tag), lambda v: v == sha,
                    lambda: self.remote.create_tag(tag, sha), publication=True)

        def release_read():
            release_id = j['effects'].get('draft', {}).get('acknowledged_id', j.get('release_id'))
            if release_id is None:
                r = self.remote.release(tag)
            else:
                if type(release_id) is not int or release_id < 1:
                    raise Halt('Invalid saved release identity')
                r = self.remote.release_by_id(release_id)
                if not r or r.get('id') != release_id:
                    raise Halt('Acknowledged draft identity is unavailable or changed')
            return None if r is None else {k: r.get(k) for k in ('id', 'tag_name', 'target_commitish', 'name', 'body', 'draft', 'published_at', 'prerelease')}

        def create_draft():
            response = self.remote.create_draft(tag, sha, expected_notes)
            release_id = response.get('id') if isinstance(response, dict) else None
            if type(release_id) is int and release_id > 0:
                # A successful POST may be visible by ID before the
                # repository release list includes the draft.
                j['effects']['draft']['acknowledged_id'] = release_id
                self.save()

        def draft_ready(value):
            return bool(value and value['tag_name'] == tag and value['target_commitish'] == sha and
                        value['name'] == tag and value['body'] == expected_notes and value['draft'] is True and
                        value['prerelease'] == j['latest_plan']['prerelease'])
        if 'draft' not in j.get('effects', {}) or j['effects']['draft']['state'] != 'verified':
            draft = self.effect('draft', release_read, draft_ready, create_draft, publication=True)
            j['release_id'] = draft['id']
            self.save()
        release_id = j['effects']['draft']['observed']['id']
        j['release_id'] = release_id
        self.check_release(release_read(), expected_notes)
        files = [(a['name'], self.artifact_paths() / a['name'], a) for a in j['artifacts']]
        reviewed_path = self.artifact_paths() / 'sfc-release-manifest.json'
        if checksum(reviewed_path) != j['manifest_sha256']:
            raise Halt('Release manifest bytes changed')
        # A final manifest records hashes of independent receipts without copying
        # worker prose/logs or creating a circular hash in the reviewed subject.
        manifest = read_json(reviewed_path)
        manifest['reviewed_subject_sha256'] = j['manifest_sha256']
        receipts = self.f.record(card)['receipts']
        composite = receipts['review'].get('provisional_qa')
        lanes = ('security', 'review') if composite is not None else ('qa', 'security', 'review')
        independent = {lane: digest(receipts[lane]) for lane in lanes}
        manifest['verification']['independent_receipts_sha256'] = independent
        if composite is not None:
            manifest['verification']['package_qa_assessment'] = {
                'inspection_sha256': composite['inspection_sha256'], 'final_review_sha256': independent['review']}
        manifest['verification']['source_checks_sha256'] = j['source_checks_sha256']
        manifest_path = self.directory / tag / 'publication-manifest.json'
        if j.get('publication_manifest_sha256'):
            if not manifest_path.is_file() or checksum(manifest_path) != j['publication_manifest_sha256']:
                raise Halt('Publication manifest changed')
        else:
            write_json(manifest_path, manifest)
            j['publication_manifest_sha256'] = checksum(manifest_path)
            self.save()
        files.append(('sfc-release-manifest.json', manifest_path,
                      {'name': 'sfc-release-manifest.json', 'size': manifest_path.stat().st_size,
                       'sha256': j['publication_manifest_sha256']}))
        for name, path, expected in files:
            def read_asset():
                release = self.remote.release_by_id(release_id)
                self.check_release(release, expected_notes)
                assets = [a for a in release['assets'] if a['name'] == name]
                if len(assets) > 1:
                    raise Halt('Duplicate release assets; never replace')
                if not assets:
                    return None
                asset = assets[0]
                downloaded = self.directory / tag / 'downloads' / name
                downloaded.parent.mkdir(parents=True, exist_ok=True)
                self.f.ensure_running()
                self.remote.download_to(asset, downloaded)
                self.f.ensure_running()
                return {'name': name, 'id': asset['id'], 'size': downloaded.stat().st_size,
                        'sha256': checksum(downloaded), 'state': asset.get('state')}
            def upload_asset():
                if path.is_symlink() or not path.is_file() or path.stat().st_size != expected['size'] or checksum(path) != expected['sha256']:
                    raise Halt('Release asset changed before upload; preserve verified artifact evidence')
                self.remote.upload(release_id, name, path)
            self.effect('asset-' + name, read_asset,
                        lambda v: bool(v and all(v[k] == expected[k] for k in ('name', 'size', 'sha256')) and v['state'] == 'uploaded'),
                        upload_asset, publication=True)
        self.verify_downloads(files)
        if self.cfg['authority']['mode'] == 'draft':
            j['state'] = 'draft-unpublished'
            self.save()
            return False
        self.reviewed(card)
        self.metadata_ready()
        if self.remote.tag(tag) != sha or self.previous(sha, tag) != j['boundary']:
            raise Halt('Release source/tag/boundary changed before publication')
        from .release_latest import publish
        publish(self)
        if self.remote.tag(tag) != sha:
            raise Halt('Published tag source mismatch; cannot complete')
        # Download again after publication, including on recovery.
        self.verify_downloads(files)
        j['state'] = 'publication-verified'
        self.save()
        return True

    def check_release(self, value, notes):
        if (not value or value['id'] != self.journal['release_id'] or value['tag_name'] != self.journal['version'] or
                value['target_commitish'] != self.journal['candidate'] or value['name'] != self.journal['version'] or value['body'] != notes or
                (not value['draft'] and 'publication' not in self.journal['effects'])):
            raise Halt('Release identity/notes/publication changed externally; preserve remote state')

    def check_asset_inventory(self, files):
        release = self.remote.release_by_id(self.journal['release_id'])
        self.check_release(release, self.public_notes())
        assets = release['assets']
        if sorted(a['name'] for a in assets) != sorted(v[0] for v in files):
            raise Halt('Unexpected or missing release assets; only verified allowlisted bytes may publish')
        return {asset['name']: asset for asset in assets}

    def verify_downloads(self, files):
        self.f.ensure_running()
        assets = self.check_asset_inventory(files)
        downloads = self.attempt_directory() / 'downloads'
        downloads.mkdir(parents=True, exist_ok=True)
        for name, path, expected in files:
            self.f.ensure_running()
            asset = assets[name]
            pinned = self.journal['effects']['asset-' + name]['observed']
            if asset['id'] != pinned['id'] or asset.get('state') != 'uploaded':
                raise Halt('Release asset identity replaced; cannot publish/complete')
            downloaded = downloads / name
            self.remote.download_to(asset, downloaded)
            if checksum(downloaded) != expected['sha256'] or downloaded.stat().st_size != expected['size']:
                raise Halt('Downloaded release bytes mismatch; cannot publish/complete')
        fresh = self.check_asset_inventory(files)
        if any((asset['id'], asset.get('state')) != (fresh[name]['id'], fresh[name].get('state'))
               for name, asset in assets.items()):
            raise Halt('Release asset identity changed during download verification')
        self.f.ensure_running()

    def complete(self, card):
        from .delivery import observation, command_scope
        self.account_downloads(card)
        with command_scope(self.f, card, 'publication'), observation(self.f, card, 'publication',
                kind='publication-completion', head=self.journal['candidate'], base=self.journal['boundary'],
                release_version=self.journal['version']):
            return self._complete(card)

    def _complete(self, card):
        j = self.journal
        # Included Done cards stay Done; only their planned Target release field was assigned.
        current = self.remote.card(card['number'])
        expected_identity = {k: current[k] for k in ('number', 'id', 'url', 'item_id', 'repository')}
        if expected_identity != j['card']:
            raise Halt('Release completion card identity changed')
        def published():
            self.checked(publication=True)
            value = self.remote.release(j['version'])
            self.check_release(value, self.public_notes())
            if value['draft'] or not value['published_at'] or self.remote.tag(j['version']) != j['candidate']:
                raise Halt('Published release identity changed before completion')
            from .release_latest import verify
            verify(self)
        published()
        self.f.update(card, release_completion='intent')
        self.effect('close', lambda: self.remote.card(card['number'])['state'], lambda v: v == 'CLOSED',
                    lambda: self.remote.close(card), before=lambda v: v == 'OPEN')
        self.move(card, 'Done')
        def completed():
            published()
            current = self.remote.card(card['number'])
            if current['status'] != 'Done' or current['state'] != 'CLOSED':
                raise Halt('Release Done/closed readback changed; cannot mark Completed')
            return current
        self.f.sync_done_status(completed(), guard=self.f.ensure_running)
        activity = self.f.record(card).get('activity', {})
        if activity.get('pending') or activity.get('error'):
            from .activity import ActivityError
            raise ActivityError('Release Completed activity is uncertain; preserve its existing recovery latch')
        if activity.get('state') == 'Completed' and activity.get('action') == 'done':
            scope, values = self.f.gh.activity_snapshot(card)
            if scope != activity.get('scope') or values != activity.get('values'):
                raise Halt('Completed release activity was edited; preserve human values')
        else:
            self.f.activity.report(card, 'Completed', 'done', prewrite=completed, guard=self.f.ensure_running)
        j['state'] = 'done'
        j.setdefault('delivery_verified_at', now())
        self.save()

    def process(self, card):
        try:
            return self._process(card)
        except Exception as exc:
            if self.journal is not None:
                self.journal['gap'] = {'time': now(), 'type': type(exc).__name__, 'detail': str(exc)}
                self.save()
            raise

    def attempt_scope(self, saved):
        return digest({**{k: saved.get(k) for k in (
            'workflow_kind', 'version', 'boundary', 'candidate', 'changes', 'identity',
            'card', 'config_digest', 'task_digest')},
            'owner_root': saved.get('owner_root', str(self.f.root))})

    def valid_attempt(self, saved, record):
        reservation = record.get('release_attempt')
        return (isinstance(reservation, dict) and set(reservation) == {'attempt', 'scope'} and
                type(reservation['attempt']) is int and
                0 < reservation['attempt'] <= self.c['max_attempts'] and
                type(record.get('attempts')) is int and
                record.get('attempts') == reservation['attempt'] and
                reservation['scope'] == self.attempt_scope(saved))

    def recover_attempt(self, card):
        j, record = self.journal, self.f.record(card)
        if 'release_attempt' not in record and 'attempt_reservation' not in j:
            return  # Legacy active journals retain their existing continuation rules.
        if not self.valid_attempt(j, record):
            raise Halt('Release attempt reservation counter/scope changed; preserve original debit')
        reservation = record['release_attempt']
        if j['state'] == 'planned' and 'attempt_reservation' not in j:
            if (card['status'] != 'Building' or record.get('status') != 'Building' or
                    card['state'] != 'OPEN'):
                raise Halt('Release attempt reservation stage changed; preserve manual edit')
            # Factory.update atomically saved both the debit and its exact scope.
            # Only the journal advance was lost. No counter or remote write is needed.
            j.update(state='building', attempt_reservation=reservation)
            self.save()
        elif j.get('attempt_reservation') != reservation or j['state'] == 'planned':
            raise Halt('Release attempt reservation journal changed; explicit recovery required')

    def assess(self, card, lane, area, base):
        record = self.f.record(card)
        saved = record.get('receipts', {}).get(lane)
        adverse = None
        release = record.get('release_evidence', {})
        if not saved and lane == 'qa' and 'inspection_snapshot' in release:
            from .review_inspection import rejection
            adverse = rejection(self.f, card, area, self.journal['candidate'], base, release=release)
            if not adverse and record.get('inspection_rejection'):
                raise Halt('Saved release inspection rejection cannot be replayed; reconcile its blocked status')
        if saved:
            result = saved['result']
        elif adverse:
            receipt, result = adverse
            self.f.update(card, inspection_rejection=receipt)
        else:
            result = self.f.lane(card, lane, area, base)
        return self.accept_assessment(card, result)

    def accept_assessment(self, card, result):
        if result['verdict'] == 'pass':
            return True
        self.move(card, 'Blocked')
        self.journal['state'] = 'blocked'
        self.save()
        self.f.activity.report(card, 'Failed', 'worker_blocked' if result['verdict'] == 'blocked' else 'findings')
        return False

    def assess_package(self, card, area, base):
        """Join two independent lanes after checks using one spare worker slot."""
        f = self.f
        record = f.record(card)
        evidence = record.get('release_evidence', {})
        if self.provisional_qa(card, area):
            lanes = ('security', 'review')
        elif 'source_coverage' in evidence and 'inspection_snapshot' not in evidence:
            # QA and Security examine the same completed package evidence.
            # Final Review still starts after both independent results pass.
            lanes = ('qa', 'security')
        else:
            return None
        if any(lane in record.get('receipts', {}) for lane in lanes):
            return None
        with f.mutex:
            if not f.worker_capacity():
                return None
            f.aux_inspections += 1
            f.parallel_release_assessments.add(card['number'])
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                first = pool.submit(copy_context().run, f.lane, card, lanes[0], area, base)
                second = f.lane(card, lanes[1], area, base)
                first = first.result()
        finally:
            with f.mutex:
                f.parallel_release_assessments.remove(card['number'])
                f.aux_inspections -= 1
        return self.accept_assessment(card, first) and self.accept_assessment(card, second)

    def _process(self, card):
        self.load(card)
        j = self.journal
        if j.get('pending_correction'):
            raise Halt('Release correction needs explicit readback before dispatch')
        if j['state'] == 'done':
            return
        if j['state'] == 'draft-unpublished':
            self.f.update(card, handoff='release draft is unpublished; separate publish policy required')
            self.f.activity.report(card, 'Waiting', 'next')
            return
        self.recover_attempt(card)
        if card['status'] == 'Ready':
            self.move(card, 'Building')
        self.assign_members(card)
        if card['status'] == 'Building':
            if j['state'] != 'building':
                if j['state'] != 'planned':
                    raise Halt('Release stage changed; cannot reserve another attempt')
                if self.f.record(card)['attempts'] >= self.c['max_attempts']:
                    raise Halt('Release attempts exhausted; no renewed build budget')
                reservation = {'attempt': self.f.record(card)['attempts'] + 1,
                               'scope': self.attempt_scope(j)}
                self.f.update(card, attempts=reservation['attempt'], release_attempt=reservation)
                j.update(state='building', attempt_reservation=reservation)
                self.save()
            elif not 0 < self.f.record(card)['attempts'] <= self.c['max_attempts']:
                raise Halt('Release attempt reservation missing or exceeds current limit')
            area = self.prepare(card)
            self.move(card, 'QA')
            j['state'] = 'qa'
            self.save()
        else:
            area = self.prepare(card)
        base = j['boundary'] or j['candidate']
        if card['status'] == 'QA':
            receipts = self.f.record(card).get('receipts', {})
            for lane in ('security', 'review'):
                saved = receipts.get(lane)
                if saved and saved['result']['verdict'] != 'pass':
                    self.accept_assessment(card, saved['result'])
                    return
            concurrent = self.assess_package(card, area, base)
            if concurrent is False:
                return
            if concurrent is None:
                saved_review = receipts.get('review', {})
                if saved_review.get('provisional_qa'):
                    self.reviewed_package_qa(card, saved_review, self.f.record(card)['release_evidence'])
                elif not self.provisional_qa(card, area) and not self.assess(card, 'qa', area, base):
                    return
                if not self.assess(card, 'security', area, base):
                    return
            self.move(card, 'Review')
            j['state'] = 'review'
            self.save()
        if card['status'] == 'Review':
            # A provisional observation can expire between security and final
            # review or while paused in Review. Use the original QA budget.
            if ('provisional_qa' in j and not self.f.record(card).get('receipts', {}).get('review') and
                    not self.provisional_qa(card, area) and not self.assess(card, 'qa', area, base)):
                return
            if not self.assess(card, 'review', area, base):
                return
            # Serialized with ordinary merges, without borrowing merge authority.
            with self.f.merge_mutex:
                if self.publish(card):
                    self.complete(card)
                else:
                    self.f.update(card, handoff='release draft is unpublished')
                    self.f.activity.report(card, 'Waiting', 'next')

    def trigger(self):
        """Only separately configured version changes, within the existing run lifetime."""
        guard_release_creation(self.f.root)
        if not self.cfg.get('enabled') or self.cfg.get('trigger') != 'version-file':
            return
        self.journal = None
        self.path = None
        try:
            self.checked(publication=True)
            candidate = self.f.fetch()
            version = self.select_version(candidate, automatic=True)
            if (self.directory / (version + '.json')).exists():
                return
            # The request still selects the version from its final frozen cutoff.
            self.request(automatic=True)
        except ReadConnectionReset as exc:
            # The trigger can defer reads before a release journal is saved.
            # Existing unfinished effects and creation readbacks
            # still require the original recovery gate.
            if self.journal is not None:
                raise
            self.unfinished_release(None, automatic=False)
            raise ReleaseTriggerReadReset(str(exc)) from exc
        except ReleasePushPermissionMissing as exc:
            self.unfinished_release(exc.identity, automatic=True, push_denied=True)
            return {'state': 'pending', 'reason': str(exc)}
        except ReleaseTriggerPending as exc:
            return {'state': 'pending', 'reason': str(exc)}

    def continuing_attempt(self, card):
        """Read-only admission for an existing attempt, never a fresh budget."""
        record = self.f.record(card)
        version = record.get('release_version')
        if (not self.cfg.get('enabled') or record.get('workflow_kind') != 'release' or
                not isinstance(version, str) or not re.fullmatch(VERSION, version) or
                not 0 < record.get('attempts', 0) <= self.c['max_attempts']):
            return False
        path = self.directory / (version + '.json')
        if not path.is_file():
            return False
        saved = read_json(path)
        if ('release_attempt' in record or 'attempt_reservation' in saved) and (
                not self.valid_attempt(saved, record) or
                saved.get('attempt_reservation') != record.get('release_attempt')):
            return False
        # All normal live scope/prerequisite/claim and dispatch checks still run.
        # The journal must own this exact in-flight card and unchanged evidence
        # scope; a label or manual stage edit cannot reopen a spent attempt.
        return (saved.get('workflow_kind') == 'release' and saved.get('version') == version and
                not saved.get('pending_correction') and
                saved.get('state') in ('building', 'qa', 'review') and
                card['status'].lower() == saved['state'] and
                saved.get('owner_root', str(self.f.root)) == str(self.f.root) and
                saved.get('config_digest') == self.f.config_digest and
                saved.get('task_digest') == self.f.task_digest(card) and
                saved.get('card') == {k: card[k] for k in ('number', 'id', 'url', 'item_id', 'repository')})

    def bind(self, card):
        """A local supervisor journal selects workflow; labels/body never do."""
        matches = []
        for path in self.directory.glob('*.json'):
            journal = read_json(path)
            if (not journal.get('card') and journal.get('request_id') and
                    '<!-- sfc:ticket:' + journal['request_id'] + ' -->' in card.get('body', '')):
                raise UnfinishedReleaseCreation(path, journal, message='Incomplete release creation')
            identity = journal.get('card', {})
            if identity.get('number') == card['number'] and journal['identity']['repo'] == self.c['repo']:
                matches.append(journal)
        if len(matches) > 1:
            raise Halt('Ambiguous release workflow identity; inspect common journals')
        if matches:
            j = matches[0]
            if any(card.get(k) != v for k, v in j['card'].items()):
                raise Halt('Release Project card identity changed')
            self.f.record(card).update(workflow_kind='release', release_version=j['version'],
                                       release_correction_pending=bool(j.get('pending_correction')))

    def retained_rejection(self, card, record):
        j = self.journal
        pins = {'head': j['candidate'], 'base': j['boundary'] or j['candidate'],
                'config_digest': j['config_digest'], 'task_digest': self.f.task_digest(card)}
        if rejection := record.get('inspection_rejection'):
            from .verification import load
            return (load(rejection)['kind'] == 'inspection-rejection' and rejection['profile'] == 'release' and
                    all(rejection[key] == expected for key, expected in pins.items()))
        release_digest = digest(record.get('release_evidence', {}))
        for lane, receipt in record.get('receipts', {}).items():
            if (lane in ('qa', 'security', 'review') and receipt.get('lane') == lane and
                    all(receipt.get(key) == expected for key, expected in pins.items()) and
                    receipt.get('release_digest') == release_digest and
                    receipt['result']['verdict'] in ('fail', 'blocked')):
                result = Path(receipt['result_path'])
                if (not result.is_symlink() and read_json(result) == receipt['result'] and
                        read_json(result.with_name('receipt.json'))['result'] == receipt['result']):
                    return True
        return False

    def reconcile(self, card):
        """Report whether a nonterminal journal required live recovery checks."""
        version = self.f.record(card).get('release_version')
        if not isinstance(version, str) or not re.fullmatch(VERSION, version):
            raise Halt('Release recovery requires a validated version journal')
        saved = read_json(self.directory / (version + '.json'))
        if saved.get('state') == 'done':
            return False  # Terminal observation grants no new work, even after disable/config edits.
        if saved.get('pending_correction'):
            return False  # The exact correction command owns readback, never a watcher retry.
        # A completed draft handoff can be observed after push permission is
        # lost. Pending mutations still require the normal recovery checks.
        observe_draft = self.load(card, allow_draft_observation=True)
        j = self.journal
        if j['state'] == 'done':
            return True
        self.recover_attempt(card)
        if observe_draft:
            if not self.f.record(card).get('handoff'):
                self.f.update(card, handoff='release draft is unpublished; separate publish policy required')
            return True
        if j['state'] in ('building', 'qa', 'review') and 'stage-Blocked' in j['effects']:
            record = self.f.record(card)
            if (not self.retained_rejection(card, record) or j['effects']['stage-Blocked']['before'] !=
                    {'building': 'Building', 'qa': 'QA', 'review': 'Review'}[j['state']]):
                raise Halt('Release rejection or blocked transition changed')
            self.move(card, 'Blocked')
            j['state'] = 'blocked'
            self.save()
            return True
        target = {'building': 'QA', 'qa': 'Review'}.get(j['state'])
        if target and 'stage-' + target in j['effects']:
            # move() can have saved its intent/readback before either local
            # stage record advanced. Reconcile before attempt-limit admission;
            # an existing effect permits readback only, never another write.
            if j['effects']['stage-' + target].get('before') != {'QA': 'Building', 'Review': 'QA'}[target]:
                raise Halt('Release stage intent source changed; preserve retained transition')
            self.move(card, target)
            j['state'] = target.lower()
            self.save()
        if j['state'] == 'publication-verified' or any(k in j['effects'] for k in ('close', 'stage-Done')):
            # Publication and byte checks are repeated by readback; packaging and
            # model lanes are not dispatched during completion recovery.
            if self.publish(card):
                self.complete(card)
        return True
