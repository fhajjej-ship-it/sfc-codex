# Release preflight

The release supervisor packages the pinned merged source and freezes the
allowlisted artifact names, sizes and SHA-256 hashes. It then runs configured
`release.preflight_commands` in the detached candidate worktree. A failed or
interrupted preflight stops preparation before project verification and model
inspection. A pass allows the required project checks, any remaining artifact
checks and independent assessment to continue.

Set `release.preflight_commands` to a nonempty ordered list of commands whose
inputs are available immediately after packaging. Keep any check that consumes
project verification output in `release.verify_commands`. Use an empty
`release.verify_commands` list when preflight covers the project's entire
artifact-check requirement. The top-level project `verify_commands` still run.
Existing release configurations keep their original order. The release journal
and `delivery-report` show each phase, its configured commands and dependencies.

## SFC's portable ZIP

For SFC, `python3 scripts/verify_release_package.py` can run in preflight. It
checks the candidate archive and sidecar against the source version, exact
portable inventory and file bytes. The packaged README's current version,
download filenames and Quick start commands must match those assets. It runs the
documented checksum and extraction in a temporary directory, verifies the
extracted bytes, checks the installer and supervisor entry points, and runs the
focused fake-native installation tests. It also imports the shared ticket
validator and its two callers from the extracted archive, then checks that
validation accepts a populated criterion and rejects an empty checkbox before
another section. It requires the same `shasum` and `unzip` commands documented
for customers.

The portable package allows the two existing public evidence summaries in
`docs/evidence/`. Additional material there requires an explicit allowlist
change. Raw documentation logs and concrete maintainer-local paths in packaged
documentation are rejected. Generic examples of where the runtime stores its
configuration and receipts remain valid.

## Receipts and restart

The repository-common release journal preserves the command reservation before
launch, ordered commands, log hashes, timing, candidate and configuration pins,
dependency plan and artifact digest. Completed preflight is resumed only when
its receipt and logs still match. An interrupted or failed reservation retains
its original evidence without automatic re-execution. The reviewed manifest
binds the preflight receipt by hash. Release readiness checks the verified source,
artifact bytes and logs again, and publication verifies downloaded asset hashes.

`delivery-report` includes preflight elapsed time when observed, the project
commands that did not start, and the independent lanes avoided by an early
failure. These counts describe the recorded execution. They do not estimate
production latency, token savings or billed cost.
