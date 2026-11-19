#!/usr/bin/env python3
"""Explicit first install or upgrade using the native CLI, not cache edits.

Run from an extracted SFC package. Retain its dist/native-install directory;
Codex registers that versioned installation source. Does not trust hooks,
start a factory, edit project configuration or authorize merging.
"""
import argparse
import json
from pathlib import Path
import shutil

from install_development_plugin import install
from package_plugin import ROOT


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--codex', default=shutil.which('codex'),
                        help='Codex CLI executable; use the desktop app bundled CLI when different')
    parser.add_argument('--package', type=Path, default=ROOT,
                        help='An unchanged extracted SFC source package to install, including a supported prior version')
    args = parser.parse_args()
    if not args.codex:
        parser.error('Install Codex CLI or supply --codex with its executable path')
    print(json.dumps(install(args.codex, args.package.resolve(), allow_fresh=True), indent=2))


if __name__ == '__main__':
    main()
