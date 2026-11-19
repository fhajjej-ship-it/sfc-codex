#!/usr/bin/env python3
"""Verify the portable release ZIP and isolated installation checks. No native install."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile

if __name__ == '__main__':
    sys.dont_write_bytecode = True

from package_plugin import ROOT, contents
from validate_plugin import validate


PUBLIC_EVIDENCE = {'docs/evidence/onboarding-75.json', 'docs/evidence/performance-observation-20261110.json'}
PRIVATE_PATH = re.compile(
    r'/(?:Users|home)/[^/\s`]+/|/Volumes/[^/`<>\r\n]+/|'
    r'/(?:private/)?(?:tmp|var/tmp|var/folders/[^/\s]+/[^/\s]+/T)/sfc[^\s`]*|'
    r'\.sfc/(?:build-[\w.-]+|performance-audit-[\w.-]+|stage-three-[\w.-]+|workshop-trial|'
    r'release(?:-prep|-runs)?-[\w.-]+|findings/[\w.-]+|runs/\d{4}-\d{2}-\d{2}T)')

PACKAGED_RUNTIME_SMOKE = """\
import json
from pathlib import Path
import sys

scripts = Path.cwd() / 'scripts'
sys.path.insert(0, str(scripts))
from sfc_lib import factory, priority, ticket_validation

for module in (factory, priority, ticket_validation):
    if Path(module.__file__).resolve() != scripts / 'sfc_lib' / (module.__name__.split('.')[-1] + '.py'):
        raise SystemExit('Ticket runtime was not imported from the extracted archive')
    if module.lint_card is not ticket_validation.lint_card:
        raise SystemExit('Packaged ticket callers do not use the shared validator')

heading = '## Acceptance criteria\\n'
valid = {'body': heading + '- [ ] Export shows the selected columns\\n'}
invalid = {'body': heading + '- [ ]\\n\\n## Notes\\n- Context for the change\\n'}
if ticket_validation.lint_card(valid):
    raise SystemExit('Packaged ticket validation rejected a populated acceptance criterion')
if 'acceptance criteria need observable bullet points' not in ticket_validation.lint_card(invalid):
    raise SystemExit('Packaged ticket validation accepted an empty checkbox before another section')

print(json.dumps({'check': 'packaged-ticket-runtime',
                  'source': 'extracted archive',
                  'shared_validator_callers': ['factory', 'priority'],
                  'populated_criterion': 'accepted',
                  'empty_checkbox_before_next_section': 'rejected'}))
"""


def validate_public_docs(files, version, archive_name):
    for name, data in files.items():
        if ((name.startswith('docs/evidence/') and name not in PUBLIC_EVIDENCE) or
                (name.startswith('docs/') and Path(name).suffix.lower() in {'.log', '.jsonl'})):
            raise ValueError('Maintainer evidence is forbidden in the release package: ' + name)
        if name != 'README.md' and not name.startswith(('docs/', 'skills/')):
            continue
        if Path(name).suffix.lower() in {'.md', '.markdown', '.json', '.txt'} and PRIVATE_PATH.search(data.decode()):
            raise ValueError('Concrete maintainer evidence reference in packaged documentation: ' + name)

    readme = files['README.md'].decode()
    announcement = re.search(r'^SFC \*\*([^*\n]+)\*\*', readme, re.M)
    if not announcement or announcement[1] != version:
        raise ValueError('Packaged README release version does not match the candidate')
    quick_start = re.search(r'^## Quick start\s*\n(.*?)(?=^## |\Z)', readme, re.M | re.S)
    if not quick_start:
        raise ValueError('Packaged README lacks Quick start instructions')
    expected_names = {archive_name, archive_name + '.sha256'}
    download_names = set(re.findall(r'`(sfc-[^`\s]+\.zip(?:\.sha256)?)`', quick_start[1]))
    if download_names != expected_names:
        raise ValueError('Quick start download filenames do not match the candidate archive and checksum')
    for release_version, filename in re.findall(r'/releases/download/([^/\s)]+)/([^\s)]+)', quick_start[1]):
        if release_version not in (version, 'v' + version) or filename not in expected_names:
            raise ValueError('Quick start download URL does not match the candidate release')

    install = re.search(r'^\s*```sh\n(.*?)^\s*```\s*$', quick_start[1], re.M | re.S)
    documented = [shlex.split(line) for line in install[1].splitlines() if line.strip()] if install else []
    expected = [['shasum', '-a', '256', '-c', archive_name + '.sha256'],
                ['unzip', archive_name], ['cd', 'sfc'], ['python3', 'scripts/install_codex_plugin.py']]
    if documented != expected:
        raise ValueError('Quick start checksum, extraction or installation commands do not match the candidate package')
    return documented


def verify(archive):
    archive = Path(archive).resolve()
    version = validate()['version']
    if archive.name != f'sfc-{version}.zip':
        raise ValueError('Release archive filename does not match the source version')
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    expected_checksum = f'{digest}  {archive.name}'
    if archive.with_suffix('.zip.sha256').read_text().strip() != expected_checksum:
        raise ValueError('Release checksum does not match the archive')
    source = contents()
    with zipfile.ZipFile(archive) as package:
        entries = package.infolist()
        names = [info.filename for info in entries]
        expected = {'sfc/' + name for name in source}
        if len(names) != len(expected) or set(names) != expected:
            raise ValueError('Release archive inventory differs from the source allowlist')
        if any(info.orig_filename != info.filename or stat.S_IFMT(info.external_attr >> 16) not in (0, stat.S_IFREG)
               for info in entries):
            raise ValueError('Release archive contains a nonregular file or ambiguous path')
        if any(info.file_size != len(source[info.filename.removeprefix('sfc/')]) for info in entries):
            raise ValueError('Release archive file sizes differ from the source allowlist')
        files = {name: package.read('sfc/' + name) for name in source}
        for name, data in files.items():
            if data != source[name]:
                raise ValueError('Release archive contains stale or changed source: ' + name)
        documented = validate_public_docs(files, version, archive.name)
    with tempfile.TemporaryDirectory(prefix='sfc-release-check-') as temporary:
        download = Path(temporary)
        shutil.copyfile(archive, download / archive.name)
        shutil.copyfile(archive.with_suffix('.zip.sha256'), download / (archive.name + '.sha256'))
        if (hashlib.sha256((download / archive.name).read_bytes()).hexdigest() != digest or
                (download / (archive.name + '.sha256')).read_text().strip() != expected_checksum):
            raise ValueError('Candidate archive or checksum changed while preparing extraction')
        extracted = download / documented[2][1]
        commands = [(download, command) for command in documented[:2]]
        commands.extend([
            (extracted, [sys.executable, *documented[3][1:], '--help']),
            (extracted, [sys.executable, 'scripts/validate_plugin.py']),
            (extracted, [sys.executable, 'scripts/sfc.py', '--help']),
            (extracted, [sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-p', 'test_packaging.py', '-v']),
            (extracted, [sys.executable, '-I', '-B', '-c', PACKAGED_RUNTIME_SMOKE]),
        ])
        checks = []
        for directory, command in commands:
            if directory == extracted and len(checks) == 2:
                extracted_files = {p.relative_to(extracted).as_posix(): p for p in extracted.rglob('*')
                                   if p.is_symlink() or not p.is_dir()}
                if set(extracted_files) != set(files) or any(
                        p.is_symlink() or not p.is_file() or p.read_bytes() != files[name]
                        for name, p in extracted_files.items()):
                    raise ValueError('Documented extraction did not produce the candidate package files')
            result = subprocess.run(command, cwd=directory, capture_output=True, text=True, timeout=120)
            checks.append({'command': command, 'exit_code': result.returncode,
                           'stdout': result.stdout, 'stderr': result.stderr})
            if result.returncode:
                raise ValueError('Extracted package check failed: ' + json.dumps(checks[-1]))
    return {'valid': True, 'version': version, 'archive': str(archive), 'sha256': digest,
            'files': len(source), 'checks': checks,
            'scope': 'Exact ZIP bytes, customer documentation, documented checksum/extraction, extracted CLI, '
                     'temporary fake-native installer tests and packaged ticket runtime smoke'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', nargs='?', type=Path)
    args = parser.parse_args()
    archive = args.archive or ROOT / 'dist' / f"sfc-{validate()['version']}.zip"
    print(json.dumps(verify(archive), indent=2))


if __name__ == '__main__':
    main()
