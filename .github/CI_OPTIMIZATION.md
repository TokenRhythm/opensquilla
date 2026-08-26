# CI optimization operations

The default `CI` workflow keeps the required check name `CI result` while assigning
different responsibilities to pull requests, merge queue entries, `main` pushes, and
the nightly run.

| Stage | Target | Responsibility |
| --- | --- | --- |
| Pull request | 8–15 minutes for ordinary changes | Run always-on contracts plus suites selected by the canonical planner. Registered Python, WebUI, Electron, and TUI dependency inputs receive ecosystem-specific coverage; unknown manifests and merge-critical CI policy inputs fail closed to the full matrix. |
| Merge queue | 1–3 minutes when evidence is reusable | Verify exact-tree, base, workflow-run, PR association, and policy attestations. Any mismatch runs the full queue matrix. |
| `main` push | 3–8 minutes in `enforce` | Build the WebUI and wheel, install in a clean environment, import the gateway, run the CLI, and exercise an offline provider/gateway canary. |
| Nightly | Full matrix | Exercise all supported test shards and platform contracts without change-based selection. |

Times are operational targets, not timeouts. High-risk pull requests intentionally remain
slower. Provider-live, credential, and external-network tests remain excluded from required
CI and retain their existing live markers.

Pull-request path selection compares the PR head with its merge base, not directly with the
current base tree. A branch that is behind `main` therefore does not inherit base-only changes
as if the PR introduced them. This affects suite selection only: merge-queue reuse remains bound
to the exact current base and tested tree, so a base advance still triggers the full fail-closed
queue matrix. For an otherwise eligible non-policy PR, refreshing the branch and completing a
new green PR CI run can make a later queue entry exact; reuse is decided again at queue time.

## Modes

Set the Actions repository variable `CI_OPTIMIZATION_MODE` to one of:

- `shadow`: compute and report whether merge-queue evidence is reusable without accepting it.
  The merge-group entry runs the full fail-closed matrix. Use this only as the emergency switch
  for disabling queue evidence reuse while keeping the merge queue active.
- `enforce` (default): reuse exact-base, exact-tree trusted PR evidence in the merge queue;
  otherwise run the full fail-closed queue matrix. Replace the normal `main` push matrix with the
  installation and offline gateway canary.
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
4. Every merge-critical input declared in `.github/ci/trust-policy.v1.json` has the same digest
   as the queue base. The manifest includes itself, the suite contract, required workflow,
   planner, gate, attestation verifier, shard assignment policy, and local CI executors.
5. The planner digest, selected suites, platform cells, and per-suite execution digests match the
   tested tree. Scheduling-only duration data stays outside the trust root but remains covered by
   planner contracts and suite execution digests.

Missing, expired, malformed, stale, or unverifiable evidence never bypasses tests. The exact
merge-group entry runs the full matrix when evidence cannot be reused. Nightly health remains an
independent diagnostic and never changes a PR plan or queue decision. The required
branch-protection context remains `CI result`.

Before merging this rollout, record the current `protect-main` ruleset history version and
confirm that it is active on the default branch, has no bypass actors, requires `CI result` and
`Validate target branch` with strict status checks, and still enables the merge queue. Keep
`enforce` as a reviewed repository default; a CI-policy pull request still cannot reuse its own
evidence because its policy digest differs from the target branch. This CI workflow does not
modify repository governance settings.

## Validation and rollback

After a CI-policy rollout merges, validate reuse with an ordinary non-policy pull request based on
the new `main`. A successful queue fast path runs only evidence verification, the combined-tree
canary, and `CI result`. It does not publish derived evidence because only root PR evidence is a
queue trust source. Base/tree/policy mismatches and API or artifact failures must run the full
fallback matrix. Nightly failures must not affect either decision.

If evidence is accepted incorrectly or is not bound to the queue SHA, set
`CI_OPTIMIZATION_MODE=shadow`, keep the merge queue active, and revert the policy change. If the
planner omits a required suite, set `CI_OPTIMIZATION_MODE=legacy` so PR and queue entries run full
CI while the policy is reverted. Over-selection or a low reuse rate is safe and does not require a
mode change.
