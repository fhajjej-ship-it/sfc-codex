#!/usr/bin/env python3
"""Explicit maintainer installation through Codex CLI, never direct cache edits.

Does not trust hooks, authorize a project, run workers, or publish a release.
The retained local marketplace is required for future native discovery/updates.
"""
from __future__ import annotations

import argparse
from contextlib import nullcontext
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from package_plugin import ROOT, contents, package
from sfc_lib.common import exclusive


def install(codex, root=ROOT, *, call=None, allow_fresh=False):
    # An update removes the previous native cache. Serialize against this
    # maintainer checkout's supervisor so its loaded runtime cannot lose code
    # during active worker/check/activity IO. Idle loaded chats need a host
    # reload after replacement; the installer never fabricates that lifecycle.
    with exclusive(root) if (root / '.sfc/config.json').exists() else nullcontext():
        return _install(codex, root, call=call, allow_fresh=allow_fresh)


def _install(codex, root, *, call=None, allow_fresh=False):
    # Validate the selected extracted package, including an older-version
    # upgrade/rollback fixture; never validate a different imported checkout.
    verified_source = subprocess.run([sys.executable, str(root / 'scripts/validate_plugin.py')],
                                    capture_output=True, text=True, timeout=30, check=True)
    info = json.loads(verified_source.stdout)
    version = info['version']
    if '-dev.' not in version and not allow_fresh:
        raise ValueError('This command is for an explicitly requested development installation')
    files = contents(root)
    try:
        # An older portable validator may require plugin.json even though the
        # host-compatible export omits it. Exercise the selected package's
        # actual native format before the first native CLI/cache mutation.
        with tempfile.TemporaryDirectory(prefix='sfc-native-preflight-') as temporary:
            exported_root = Path(temporary).resolve() / 'sfc'
            exported = contents(root, codex_root=exported_root)
            for name, data in exported.items():
                path = exported_root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            native_check = subprocess.run(
                [sys.executable, str(exported_root / 'scripts/validate_plugin.py')],
                capture_output=True, text=True, timeout=30, check=True)
            native_info = json.loads(native_check.stdout)
            if (native_info.get('valid') is not True or native_info.get('name') != 'sfc' or
                    native_info.get('version') != version or native_info.get('format') != 'codex-resolved'):
                raise ValueError('Unexpected native-format validation result')
            if contents(exported_root) != exported or contents(root) != files:
                raise ValueError('Source/export changed during native-format validation')
    except (KeyError, AssertionError, ValueError, OSError, subprocess.SubprocessError) as exc:
        raise ValueError('This source package failed native-format validation; no installed plugin was changed') from exc
    fingerprint = hashlib.sha256(b''.join(n.encode() + b'\0' + d for n, d in sorted(files.items()))).hexdigest()
    stage = root / ('dist/native-install' if allow_fresh else 'dist/development') / version
    if stage.exists():
        raise ValueError('Existing installation journal; inspect it instead of repeating writes')
    stage.mkdir(parents=True)
    journal = stage / 'install-receipt.json'
    receipt = {'state': 'preflight', 'version': version, 'source_fingerprint': fingerprint,
               'codex': str(codex), 'scope': 'SFC local plugin only; no hook trust or project activation'}

    def save():
        journal.write_text(json.dumps(receipt, indent=2) + '\n')

    def invoke(name, args):
        receipt['state'] = name + '_intent'
        save()
        if call is None:
            result = subprocess.run([str(codex), *args, '--json'], cwd=root,
                                    capture_output=True, text=True, timeout=120)
            (stage / (name + '.stderr.log')).write_text(result.stderr)
            (stage / (name + '.json')).write_text(result.stdout)
            result.check_returncode()
            data = json.loads(result.stdout)
        else:
            data = call(args)
        receipt[name] = data
        receipt['state'] = name + '_acknowledged'
        save()
        return data

    save()
    before = invoke('before', ['plugin', 'list', '--marketplace', 'sfc-local'])
    selected = [p for p in before['installed'] if p['pluginId'] == 'sfc@sfc-local']
    if len(selected) > 1 or (selected and not selected[0]['enabled']) or (not selected and not allow_fresh):
        raise ValueError('Expected one enabled SFC development installation; inspect setup before replacing it')
    if selected and selected[0]['version'] == version:
        raise ValueError('Use a new development version; do not overwrite an existing installation')
    marketplace = stage / '.agents/plugins'
    marketplace.mkdir(parents=True)
    (marketplace / 'marketplace.json').write_text(json.dumps({
        'name': 'sfc-local', 'interface': {'displayName': 'SFC'}, 'plugins': [{
            'name': 'sfc', 'source': {'source': 'local', 'path': './sfc'},
            'policy': {'installation': 'AVAILABLE', 'authentication': 'ON_INSTALL'},
            'category': 'Developer Tools'}]}, indent=2) + '\n')
    payload = stage / 'sfc'
    (payload / '.codex-plugin').mkdir(parents=True)
    # Safe bootstrap discovers the native cache location without guessing its
    # private layout. It exposes no skills, hooks or MCP server and never runs.
    (payload / '.codex-plugin/plugin.json').write_text(json.dumps({
        'name': 'sfc', 'version': version, 'description': 'SFC installation bootstrap; no runtime capabilities'}) + '\n')
    if selected:
        invoke('remove_previous', ['plugin', 'remove', 'sfc@sfc-local'])
        invoke('remove_marketplace', ['plugin', 'marketplace', 'remove', 'sfc-local'])
    else:
        inventory = invoke('marketplaces_before', ['plugin', 'marketplace', 'list'])
        rows = inventory.get('marketplaces')
        if not isinstance(rows, list) or any(not isinstance(p, dict) or not isinstance(p.get('name'), str) for p in rows):
            raise ValueError('Unknown native marketplace inventory; no installation writes attempted')
        matches = [p for p in rows if p['name'] == 'sfc-local']
        if len(matches) > 1:
            raise ValueError('Ambiguous existing SFC marketplace; inspect before replacement')
        if matches:
            invoke('remove_marketplace', ['plugin', 'marketplace', 'remove', 'sfc-local'])
    invoke('register', ['plugin', 'marketplace', 'add', str(stage)])
    seed = invoke('bootstrap', ['plugin', 'add', 'sfc@sfc-local'])
    installed = Path(seed['installedPath'])
    if not installed.is_absolute() or seed['version'] != version or seed['pluginId'] != 'sfc@sfc-local':
        raise ValueError('Unexpected native installation identity; retain journal and inspect')
    assert contents(root) == files, 'Source changed during installation; do not package a mixed build'
    materialized = contents(root, codex_root=installed)
    archive = root / 'dist' / f'sfc-{version}-codex.zip'
    receipt['package'] = package(archive, materialized)
    receipt['installed_path'] = str(installed)
    save()
    # Only staging source is written. The official installer owns both cache
    # removal and creation; the bootstrap was not a distributable artifact.
    for name, data in materialized.items():
        path = payload / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    invoke('remove_bootstrap', ['plugin', 'remove', 'sfc@sfc-local'])
    final = invoke('install', ['plugin', 'add', 'sfc@sfc-local'])
    if (final['installedPath'] != str(installed) or final['version'] != version or
            final['pluginId'] != 'sfc@sfc-local'):
        raise ValueError('Native installation location changed; no retry or path guessing')
    for name, data in materialized.items():
        if (installed / name).read_bytes() != data:
            raise ValueError('Installed bytes differ from the pinned package: ' + name)
    after = invoke('after', ['plugin', 'list', '--marketplace', 'sfc-local'])
    installed_rows = [p for p in after['installed'] if p['pluginId'] == 'sfc@sfc-local']
    if len(installed_rows) != 1 or installed_rows[0]['version'] != version or not installed_rows[0]['enabled']:
        raise ValueError('Native installation readback did not verify enabled SFC')
    verified = subprocess.run([sys.executable, str(installed / 'scripts/validate_plugin.py')],
                              capture_output=True, text=True, timeout=30, check=True)
    receipt['installed_validation'] = json.loads(verified.stdout)
    receipt.update(state='verified', hook_trust_changed=False, project_authorization_changed=False)
    save()
    return {'version': version, 'installedPath': str(installed), 'receipt': str(journal), 'package': receipt['package']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--codex', default=shutil.which('codex'), help='Codex executable, preferably this app\'s bundled runtime')
    args = parser.parse_args()
    if not args.codex:
        parser.error('Codex executable unavailable')
    print(json.dumps(install(args.codex), indent=2))


if __name__ == '__main__':
    main()
