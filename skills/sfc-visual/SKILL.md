---
name: sfc-visual
description: Create a standalone HTML recap, implementation plan or codebase map for SFC work, using actual source and verification receipts.
---

# SFC visual

Select recap, plan or map from the request. Read only the relevant repository
files, diffs and `.sfc/runs/` receipts. A recap separates implemented, verified,
published and unfinished results. A plan labels proposed behavior. A map shows
actual components and their connections without implying unverified runtime use.

Create one self-contained HTML file in `.sfc/reports/` or the requested location.
Use inline CSS/SVG and semantic HTML; no CDN scripts, trackers, secret values or
external network dependency. Escape repository-provided strings in HTML. Favor
clear relationships, compact evidence links and accessible contrast over decoration.
Display source/commit/date and gaps so the visual does not inflate confidence.

Inspect the generated page with an available browser when practical. Check long
labels and narrow viewports. Return a clickable absolute local path. Do not
upload or publish the report without an explicit request.
