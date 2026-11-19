#!/usr/bin/env python3
"""Build a local plugin archive. No publishing or installation side effects."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import zipfile

if __name__ == '__main__':
    sys.dont_write_bytecode = True

from validate_plugin import ROOT, validate

INCLUDED = ["plugin.json", ".mcp.json", ".codex-plugin", ".agents/plugins", "skills", "scripts", "schemas",
            "hooks", "docs", "assets", "tests", "README.md", "LICENSE"]


def contents(root=ROOT, *, codex_root=None):
    """Export source or a host-resolved compatibility bundle, never a cache edit.

    Legacy Codex neither loads portable hooks nor expands MCP argument macros.
    Its native installer supplies codex_root; leaving cwd unset preserves the
    actual chat checkout used by the runtime's admission checks.
    """
    if codex_root is not None:
        codex_root = Path(codex_root)
        if not codex_root.is_absolute():
            raise ValueError("Codex installation root must be absolute")
        codex_root = codex_root.resolve()
    result = {}
    for entry in INCLUDED:
        if codex_root is not None and entry == 'plugin.json':
            continue
        path = root / entry
        files = sorted(path.rglob('*')) if path.is_dir() else [path]
        for file in files:
            if file.is_file() and not file.is_symlink() and '__pycache__' not in file.parts and file.suffix != '.pyc':
                result[file.relative_to(root).as_posix()] = file.read_bytes()
    if codex_root is not None:
        config = json.loads(result['.mcp.json'])
        server = config['mcpServers']['sfc_session']
        launch = '${PLUGIN_ROOT}/scripts/session_mcp.py'
        assert server['args'] in ([launch], ['-B', launch]) and 'cwd' not in server
        # Preserve a supported prior package's launcher and validator contract.
        server['args'] = [*server['args'][:-1], str(codex_root / 'scripts/session_mcp.py')]
        result['.mcp.json'] = (json.dumps(config, indent=2) + '\n').encode()
    return result


def package(destination, files):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    # A new version/format must never silently replace an earlier artifact.
    with zipfile.ZipFile(destination, 'x', zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(files.items()):
            archive.writestr('sfc/' + name, data)
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    destination.with_suffix('.zip.sha256').write_text(f'{digest}  {destination.name}\n')
    return {'archive': str(destination), 'sha256': digest}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--codex-root', type=Path, help='Export compatibility format for the exact native installedPath')
    args = parser.parse_args()
    info = validate()
    suffix = '-codex' if args.codex_root else ''
    destination = ROOT / 'dist' / f"sfc-{info['version']}{suffix}.zip"
    print(json.dumps(package(destination, contents(codex_root=args.codex_root)), indent=2))


if __name__ == '__main__':
    main()
