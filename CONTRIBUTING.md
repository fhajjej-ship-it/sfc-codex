# Contribute to SFC

For a bug, include your SFC version, Codex version, operating system, and the
smallest sequence of steps that reproduces it. Describe the expected and
observed results. Redact credentials and private repository details from logs.
Use [GitHub Issues](https://github.com/fhajjej-ship-it/sfc-codex/issues) to
report bugs or propose changes.

Keep a pull request focused on one change and describe how you verified it.
GitHub runs **Tests and plugin validation** on Linux and **macOS session tests**
on pull requests targeting `main`. Open the pull request's **Checks** tab to see
the results. A first-time contributor's workflow may wait for maintainer approval.
Linux checks use the minimum supported Python version. The macOS session checks
use Python 3.14 to exercise native process cleanup.

Run the same checks locally from the repository root.

```sh
python3 tests/run_suite.py
python3 scripts/validate_plugin.py
```

Tests use temporary repositories and fake GitHub and Codex adapters.
The historical comparison requires a separate pinned checkout and is skipped
in the public PR workflow. See the
[qualification guide](docs/DELIVERY-QUALIFICATION.md). For changes to packaging
or install instructions, also build and verify a ZIP using the
[distribution guide](docs/DISTRIBUTION.md).
