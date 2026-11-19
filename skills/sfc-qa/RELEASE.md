# SFC release QA

Independently assess the pinned package as the customer will receive it. Check
the actual artifact inventory and version, the complete included commit, PR and
closing-ticket membership, and the documented installation path. Check whether
the completed preflight and other installation assertions exercise the package
behavior this change can affect. Identify material gaps, including a mismatch
between the reviewed source and installed behavior or a changed dependency that
the existing installer checks do not exercise.

The supervisor already validated exact candidate and artifact identity, receipt
integrity and eligible check reuse. Use those facts. Read the relevant assertion
or output when its meaning is unclear or a concrete discrepancy appears.
Distinguish a temporary fake-native installer test from a live install.

When the packet includes accepted source coverage, use it for the exact
historical work-ticket obligations. Focus your independent assessment on how
that source is packaged, installed and exercised. Revisit a source assertion
only when a package interaction or gap requires it, using the original
assessment references to investigate discrepancies. Without accepted source
coverage, independently assess the source changes that affect the release
acceptance criteria and the actual assertions supporting them.

Use focused read-only probes when needed. Cite the concrete package files,
membership and relevant installation or behavior assertions. Return the
supplied verdict once these obligations are assessed. Fail on a confirmed
defect and block when required evidence is missing or inconsistent. Do not
rerun filesystem-writing checks, rebuild the package or publish anything.
