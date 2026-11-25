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


CLAUDE_INCLUDED = ["skills", "scripts", "schemas", "hooks/guard.py", "docs", "assets", "README.md", "LICENSE"]


def claude_contents(root=ROOT):
    """Claude Code layout: the Codex manifests, hooks and MCP file are excluded.

    Claude Code always loads hooks/hooks.json and .mcp.json from a plugin root,
    so the overlay in claude/ supplies Claude hooks and no Codex file reaches
    that layout. Codex-only skill interface metadata is dropped.
    """
    result = {}
    for entry in [*CLAUDE_INCLUDED, "claude"]:
        path = root / entry
        files = sorted(path.rglob('*')) if path.is_dir() else [path]
        for file in files:
            if (not file.is_file() or file.is_symlink() or '__pycache__' in file.parts or
                    file.suffix == '.pyc' or file.name == '.DS_Store'):
                continue
            name = file.relative_to(root).as_posix()
            if entry == "claude":
                name = name.removeprefix("claude/")
            elif name.startswith("skills/") and "/agents/" in name:
                continue
            result[name] = file.read_bytes()
    return result


def export(destination, files):
    """Unpacked plugin folder for `claude --plugin-dir` or a local marketplace."""
    destination = Path(destination)
    if destination.exists():
        raise FileExistsError(f"{destination} exists; never overwrite an earlier export")
    for name, data in files.items():
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return {'directory': str(destination), 'files': len(files)}


def build_claude(dist, version, files):
    """Archive and unpacked folder side by side: sfc-<version>-claude(.zip).

    The name is appended, never derived with Path.with_suffix: a version such
    as 0.2.2 would make '.2-claude' look like the extension.
    """
    folder = Path(dist) / f"sfc-{version}-claude"
    return {**package(folder.with_name(folder.name + '.zip'), files), **export(folder, files)}


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
    parser.add_argument('--claude', action='store_true', help='Build the Claude Code plugin archive and unpacked folder')
    args = parser.parse_args()
    info = validate()
    if args.claude:
        print(json.dumps(build_claude(ROOT / 'dist', info['version'], claude_contents()), indent=2))
        return
    suffix = '-codex' if args.codex_root else ''
    destination = ROOT / 'dist' / f"sfc-{info['version']}{suffix}.zip"
    print(json.dumps(package(destination, contents(codex_root=args.codex_root)), indent=2))


if __name__ == '__main__':
    main()
