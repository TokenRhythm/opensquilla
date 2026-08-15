# CI optimization operations

The default `CI` workflow keeps the required check name `CI result` while assigning
different responsibilities to pull requests, merge queue entries, `main` pushes, and
the nightly run.

| Stage | Target | Responsibility |
| --- | --- | --- |
| Pull request | 8–15 minutes for ordinary changes | Run always-on contracts plus tests selected from the changed product area. Shared Python core changes run every offline Python shard; unknown, dependency, CI-policy, release, and platform-sensitive paths fail closed to the full matrix. |
| Merge queue | 1–3 minutes when evidence is reusable | Verify exact-tree, base, workflow-run, PR association, and policy attestations. Any mismatch runs the full queue matrix. |
| `main` push | 3–8 minutes in `enforce` | Build the WebUI and wheel, install in a clean environment, import the gateway, run the CLI, and exercise an offline provider/gateway canary. |
| Nightly | Full matrix | Exercise all supported test shards and platform contracts without change-based selection. |

Times are operational targets, not timeouts. High-risk pull requests intentionally remain
slower. Provider-live, credential, and external-network tests remain excluded from required
CI and retain their existing live markers.

## Modes

Set the Actions repository variable `CI_OPTIMIZATION_MODE` to one of:

- `shadow`: compute and report whether merge-queue evidence is reusable, but run
  the normal queue matrix. Change-based PR selection is active so its coverage and duration
  are observable before queue reuse is enabled.
- `enforce` (default): reuse an exact trusted PR result in the merge queue; otherwise fail closed to
  the full queue matrix. Replace the normal `main` push matrix with the installation and
  offline gateway canary.
- `legacy`: emergency rollback mode. It deliberately runs full PR and queue CI and keeps
  the pre-enforcement `main` behavior.

An unset or empty variable resolves to `enforce`. Any non-empty unsupported value fails CI.
Changing modes does not modify code or persisted OpenSquilla configuration.

## Trust boundary

Reusable evidence is accepted only when all of these facts match authoritative GitHub data:

1. The successful source run was a `pull_request` run of `.github/workflows/ci.yml` in this
   repository and is associated with the attested PR head.
2. The PR merge preview and queue entry have the same Git tree and base commit.
3. The attested PR head is an ancestor of the queue commit.
4. Every workflow, CI helper, dependency lock/manifest, and `CODEOWNERS` policy input has the
   same digest as `main`.

Missing, expired, malformed, stale, or unverifiable evidence never bypasses tests; it causes
the queue to run the full matrix. The required branch-protection context remains `CI result`.

Before merging this rollout, confirm that the main ruleset requires `CI result` and
`Validate target branch`, requires conversation resolution, and enforces code-owner review
for `.github/workflows/`, `.github/scripts/`, and `.github/CODEOWNERS`. Keep `enforce` as a
reviewed repository default; a CI-policy pull request still cannot reuse its own evidence
because its policy digest differs from the target branch.

## Shadow validation and rollback

During shadow validation, inspect the `Verify reusable PR CI evidence` summary on merge-group
runs. A stable candidate reports `Reusable: true` while the regular queue jobs still run.
Exercise at least one ordinary Python change, frontend-only change, docs-only change,
platform-sensitive change, cancellation/supersession, and a base update. The base-update and
CI-policy-change cases must report non-reusable evidence and run the fallback matrix.

If enforcement behaves unexpectedly, set `CI_OPTIMIZATION_MODE=legacy`. This immediately
disables evidence reuse without reverting the workflow or changing branch protection.
