# Contribute to SFC

For a bug, include your SFC version, Codex version, operating system, and the
smallest sequence of steps that reproduces it. Describe the expected and
observed results. Redact credentials and private repository details from logs.
Use [GitHub Issues](https://github.com/fhajjej-ship-it/sfc-codex/issues) to
report bugs or propose changes.

Keep a pull request focused on one change and describe how you verified it.
Run the checks from the repository root.

```sh
python3 tests/run_suite.py
python3 scripts/validate_plugin.py
```

Tests use temporary repositories and fake GitHub and Codex adapters. For
historical benchmarks, see the
[qualification guide](docs/DELIVERY-QUALIFICATION.md). For changes to packaging
or install instructions, also build and verify a ZIP using the
[distribution guide](docs/DISTRIBUTION.md).
