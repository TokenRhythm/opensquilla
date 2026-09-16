# Merge queue suite reuse

PR checks keep their existing change-based suite selection. Queue validation has
three paths:

1. Exact base, tree and trusted policy match: reuse the existing PR evidence and
   run the combined-tree installation/Gateway canary.
2. A validated PR's base advanced through understood changes: reuse only eligible
   suites with identical execution-input digests and complete platform coverage.
   Execute **every other suite in the full queue matrix**, plus the canary.
3. Missing, stale, foreign, superseded or invalid evidence, policy changes,
   non-ancestor bases, unknown changes, or no eligible suite: run the full matrix.

The first partial-reuse allowlist is `frontend-validation` (including both
Contract platforms) and `tui`. The frontend input boundary includes the complete
WebUI, Contract generators/schemas/adapters/fixtures/tests, Python package entry
point, and pinned dependencies. TUI includes its package, host, build/smoke scripts,
Node version and Bun version/lockfile. The unchanged trust-policy digest pins the
workflow, suite configuration and verifier. A runner label is part of platform
coverage; hosted image updates within that label remain subject to the existing
72-hour evidence freshness window, as with exact reuse.

Python/Windows integration and artifact-producing suites are deliberately not
eligible yet. Shared frontend jobs still execute wheel checks when frontend
validation is reused; artifacts are produced in the current run, never borrowed
from another run. Baseline checks always execute on the partial path.

The result gate requires a disjoint, complete partition of the full suite set
into executed and proven suites. Missing/skipped/failed/cancelled required jobs,
an unsuccessful verifier, or an unsuccessful canary prevent success. A partial
queue run never publishes new root evidence, so it cannot launder inherited proof
into a fresh full-matrix result. No base-branch CI or nightly result substitutes
for missing PR suite coverage.

Queue run summaries show the source run and reused suites; the planner lists the
remaining suites. The default-branch `Merge queue feedback` workflow reports
completed queue results and failed-job links to the originating PR. It uses only
GitHub metadata and never executes candidate code with its write token. Reports
identify the tested queue SHA rather than claiming to describe a newer PR head.

This change itself modifies trusted CI policy and must take the full queue path.
Partial reuse is exercised by synthetic PR/base/queue Git histories, authenticated
API fixtures, matrix/input mutations, aggregate-gate failure cases and workflow
wiring tests. Production savings depend on which of the two eligible suites the
PR actually ran and whether their inputs remained unchanged; this is not a promise
to remove the second CI run or eliminate all Windows test duplication.
