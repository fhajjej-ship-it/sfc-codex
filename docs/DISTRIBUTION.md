# SFC distribution

The [public GitHub repository](https://github.com/fhajjej-ship-it/sfc-codex)
hosts the source, issue tracker, and tagged releases. SFC ships under the
[MIT License](../LICENSE).

## Install

Follow the [README](../README.md#install-in-one-command) to install from a
specific release. Releases supply a portable ZIP and a matching SHA-256
checksum. The bundled installer registers a local Codex marketplace.
Keep the extracted package so the registration continues to point to it.

Review hook trust in Codex after installing. Onboard only your own target
repository and GitHub Project. The package uses Codex and GitHub credentials
already configured on your computer.

## Prepare a public release

Set the version in both plugin manifests, the Python runtime, and the README.
From the exact release candidate, run the suite, validate the plugin, build the
ZIP, and verify installation from the archive.

```sh
python3 tests/run_suite.py
python3 scripts/validate_plugin.py
python3 scripts/package_plugin.py
python3 scripts/verify_release_package.py
```

Publish the ZIP and checksum to the matching GitHub tag. Download both from
the published release and verify their bytes before sharing the link.
