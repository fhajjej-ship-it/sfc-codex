"""Build or check the delivery fixture's deterministic ZIP distribution."""

import argparse
import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile
from zipfile import ZipFile, ZipInfo


ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = ROOT / 'dist/delivery-fixture.zip'
MEMBERS = ('delivery_cli.py', 'VERSION', 'INSTALL.md', 'feature.txt', 'lookup.py', 'GUIDE.md', 'auth.py')


def files():
    return [name for name in MEMBERS if (ROOT / name).is_file()]


def require(condition, reason):
    if not condition:
        raise SystemExit(reason)


def pack():
    ARCHIVE.parent.mkdir(exist_ok=True)
    with ZipFile(ARCHIVE, 'w') as archive:
        for name in files():
            archive.writestr(ZipInfo(name), (ROOT / name).read_bytes())
    print(f'Packaged {ARCHIVE.name} with {len(files())} files')


def preflight(archive):
    require(archive.namelist() == files(), 'Package member inventory differs from the pinned source')
    require(archive.testzip() is None, 'Package CRC check failed')
    print('Archive inventory and CRC verified: ' + ', '.join(archive.namelist()))


def smoke(archive):
    require(archive.namelist() == files(), 'Cannot install an unexpected package inventory')
    with tempfile.TemporaryDirectory(prefix='sfc-fixture-install-') as location:
        archive.extractall(location)
        installed = Path(location)
        for name in files():
            require((installed / name).read_bytes() == (ROOT / name).read_bytes(),
                    f'Installed bytes differ from pinned source: {name}')
        version = (ROOT / 'VERSION').read_text().strip()
        status = ('Feature enabled' if (ROOT / 'feature.txt').is_file() else
                  'No entry found' if (ROOT / 'lookup.py').is_file() else
                  'Anonymous access disabled' if (ROOT / 'auth.py').is_file() else
                  'Delivery fixture ready')
        for flag, expected in (('--version', version), ('--status', status)):
            command = [sys.executable, '-I', '-B', str(installed / 'delivery_cli.py'), flag]
            result = subprocess.run(command, cwd=installed, capture_output=True, text=True, timeout=10)
            require(result.returncode == 0 and result.stdout == expected + '\n',
                    f'Installed command {flag} failed: {result.stdout}{result.stderr}')
            print(f'Installed command {flag} returned {result.stdout.strip()!r}')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('phase', choices=('pack', 'preflight', 'smoke'))
    parser.add_argument('archive', nargs='?', type=Path, default=ARCHIVE)
    args = parser.parse_args()
    if args.phase == 'pack':
        pack()
        return
    with ZipFile(args.archive) as archive:
        if args.phase == 'preflight':
            preflight(archive)
        else:
            smoke(archive)
    print('Archive SHA-256: ' + hashlib.sha256(args.archive.read_bytes()).hexdigest())


if __name__ == '__main__':
    main()
