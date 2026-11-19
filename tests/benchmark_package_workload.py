"""Capture the genuine SFC portable archive and exact source for benchmark fixtures."""
from __future__ import annotations

import hashlib
from pathlib import Path
import shutil
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from package_plugin import contents
from sfc_lib.common import Halt, read_json, write_json
from sfc_lib.verification import checksum, digest


def package_ref(path):
    return {'path': str(path), 'sha256': checksum(path)}


def prepare_package_workload(output, source):
    output, source = Path(output).resolve(), Path(source).resolve()
    output.mkdir(parents=True, exist_ok=False)
    snapshot = output / 'source'
    started = time.monotonic()
    files = contents(source)
    inventory = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
    for name, data in files.items():
        path = snapshot / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    if contents(source) != files:
        raise Halt('SFC package workload source changed during capture')
    build_log = output / 'package.log'
    with build_log.open('w') as log:
        result = subprocess.run([sys.executable, 'scripts/package_plugin.py'], cwd=snapshot,
                                stdout=log, stderr=subprocess.STDOUT, timeout=120)
    if result.returncode:
        raise Halt(f'Unable to build the exact SFC workload archive. See {build_log}')
    version = read_json(snapshot / '.codex-plugin/plugin.json')['version']
    archive = snapshot / 'dist' / f'sfc-{version}.zip'
    checksum_file = archive.with_suffix('.zip.sha256')
    manifest = {'version': version, 'source': str(snapshot), 'source_sha256': digest(inventory),
                'files': inventory, 'archive': package_ref(archive), 'checksum': package_ref(checksum_file),
                'build_log': package_ref(build_log), 'preparation_seconds': time.monotonic() - started}
    path = output / 'workload.json'
    write_json(path, manifest)
    return path


def load_package_workload(path):
    value = read_json(path)
    source = Path(value['source'])
    files = contents(source)
    observed = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
    if (observed != value['files'] or digest(observed) != value['source_sha256'] or
            read_json(source / '.codex-plugin/plugin.json')['version'] != value['version']):
        raise Halt('Frozen SFC package workload source changed')
    for key in ('archive', 'checksum', 'build_log'):
        evidence = value[key]
        if package_ref(Path(evidence['path'])) != evidence:
            raise Halt('Frozen SFC package workload evidence changed')
    archive = Path(value['archive']['path'])
    checksum_path = Path(value['checksum']['path'])
    if (archive != source / 'dist' / f"sfc-{value['version']}.zip" or
            checksum_path != archive.with_suffix('.zip.sha256') or
            checksum_path.read_text().strip() != f"{value['archive']['sha256']}  {archive.name}"):
        raise Halt('Frozen SFC package workload archive/checksum binding changed')
    return value


def copy_package_workload(path, destination):
    value = load_package_workload(path)
    destination, source = Path(destination), Path(value['source'])
    for name, expected in value['files'].items():
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / name, target)
        if checksum(target) != expected:
            raise Halt('SFC package workload source changed during fixture copy')
    inputs = destination / 'qualification-input'
    inputs.mkdir()
    for key in ('archive', 'checksum'):
        item = value[key]
        original = Path(item['path'])
        target = inputs / original.name
        shutil.copyfile(original, target)
        if checksum(target) != item['sha256']:
            raise Halt('SFC package workload archive changed during fixture copy')
    return value
