---
name: sfc-ui-refine
description: Refine a selected page or component through browser critique, focused implementation and visual verification within a Codex or Claude Code project.
---

# SFC UI refinement

Scope the route/component and the intended user action. Read the project's
design conventions, existing components and supplied reference. Start the
project-owned development command, verify readiness and inspect the current
screen through an available browser/native app tool. If that tool is unavailable,
state the visual-verification gap rather than claim a completed visual pass.

Capture the relevant states and viewports. Identify a short prioritized set of
specific problems in hierarchy, layout, typography, interaction and accessibility.
Implement a coherent focused improvement using existing components. Preserve
the product's identity and content unless the user asked to redesign them.

Run relevant checks and revisit the same viewport and flow. Verify focus,
keyboard behavior, overflow and the primary action, plus error/empty/loading
states where affected. Compare actual screenshots and stop when the requested
outcome is met. Use at most three refinement passes by default; if the same
problem persists twice, report the cause or needed decision.

Clean up only development processes you started. Return the changed behavior,
before/after evidence, checks and any remaining gaps. Do not deploy, publish or
commit solely because a visual refinement was requested.
