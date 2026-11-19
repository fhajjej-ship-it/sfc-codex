#!/usr/bin/env python3
"""Download a tagged SFC release and run its bundled Codex installer."""
from __future__ import annotations

import argparse
from hashlib import sha256
from io import BytesIO
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory
from urllib.error import URLError
from urllib.request import urlopen
from zipfile import BadZipFile, ZipFile


RELEASES = 'https://github.com/fhajjej-ship-it/sfc-codex/releases/download'
VERSION = re.compile(r'[0-9]{1,8}\.[0-9]{1,8}\.[0-9]{1,8}')
ARCHIVE_LIMIT = 32 * 1024 * 1024


def download(url, limit):
    with urlopen(url, timeout=60) as response:
        if not response.geturl().startswith('https://'):
            raise ValueError('The release download requires HTTPS')
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError('The release download exceeded its size limit')
    return data


def extract(data, stage):
    with ZipFile(BytesIO(data)) as archive:
        names = set()
        size = 0
        for entry in archive.infolist():
            path = entry.filename.rstrip('/').split('/')
            mode = stat.S_IFMT(entry.external_attr >> 16)
            if (path[0] != 'sfc' or any(part in ('', '.', '..') or '\\' in part for part in path)
                    or mode not in (0, stat.S_IFDIR, stat.S_IFREG) or entry.filename in names):
                raise ValueError('Unexpected file in the release archive')
            names.add(entry.filename)
            size += entry.file_size
        if 'sfc/scripts/install_codex_plugin.py' not in names or size > 4 * ARCHIVE_LIMIT:
            raise ValueError('The release archive does not contain a valid installer')
        archive.extractall(stage)


def prepare(version, releases_dir):
    if not VERSION.fullmatch(version):
        raise ValueError('Use a stable release version such as 0.1.39')
    target = releases_dir / version
    if target.exists() or target.is_symlink():
        raise ValueError(f'This release already has an installation folder. Inspect {target}')

    filename = f'sfc-{version}.zip'
    url = f'{RELEASES}/v{version}/{filename}'
    checksum = download(url + '.sha256', 256).decode('ascii')
    match = re.fullmatch(r'([a-f0-9]{64})  ' + re.escape(filename) + r'\n?', checksum)
    if not match:
        raise ValueError('The checksum does not name the requested release ZIP')
    data = download(url, ARCHIVE_LIMIT)
    if sha256(data).hexdigest() != match[1]:
        raise ValueError('Checksum mismatch. The release was not installed')

    releases_dir.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix='.sfc-download-', dir=releases_dir) as temporary:
        extract(data, Path(temporary))
        target.mkdir()
        package = target / 'sfc'
        (Path(temporary) / 'sfc').rename(package)
    return package


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', required=True, help='Stable SFC release version')
    parser.add_argument('--codex', default='codex', help='Codex CLI path')
    args = parser.parse_args()
    codex = shutil.which(args.codex)
    if sys.version_info < (3, 10) or not codex:
        parser.error('Install Python 3.10 or newer and the Codex CLI first')
    data_dir = Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local/share').expanduser()
    try:
        package = prepare(args.version, data_dir.resolve() / 'sfc/releases')
        subprocess.run([sys.executable, '-B', 'scripts/install_codex_plugin.py',
                        '--codex', str(Path(codex).absolute())], cwd=package, check=True)
    except (BadZipFile, OSError, URLError, ValueError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f'SFC installation stopped. {exc}\n')


if __name__ == '__main__':
    main()
