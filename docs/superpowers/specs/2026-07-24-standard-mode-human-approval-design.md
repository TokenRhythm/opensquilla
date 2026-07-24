# Standard Mode Human Approval Design

## Problem

OpenSquilla currently reads `sandbox.approvals_reviewer` without considering
the active per-session run mode. Because the configured default is
`auto_review`, an exact elevation request created while a session is in
`standard` mode can be approved by deterministic rules and executed on the
host without the user clicking Approve.

The production log demonstrates the invalid state:

```text
run_mode='standard' elevation_grant=True
```

## Required behavior

- `standard` mode must never automatically approve or consume an elevation.
- Every filesystem, shell, background-process, cached-network, package-network,
  and live-network elevation in `standard` mode must be owned by the user.
- `trusted` mode may continue using the configured reviewer, including
  `auto_review`.
- `full` mode remains direct host access and does not create elevation reviews.
- The active request/session run mode is authoritative. A process-wide
  `sandbox.run_mode = "trusted"` default must not override a session that the
  user changed to `standard`.
- If an old `auto_review` request reaches the reviewer after its session has
  become `standard`, it must be converted to a human-actionable request instead
  of being resolved automatically.

## Design

Add one pure helper in `opensquilla.sandbox.elevation` that resolves the
effective reviewer from `(configured_reviewer, active_run_mode)`. It always
returns `user` for `standard`; otherwise it returns the valid configured value
and fails safely to `user`.

All elevation producers use this helper:

- exact tool elevation through `gate_elevated_action`;
- cached network and package-bundle preflight in sandbox integration;
- live network approval in `NetworkApprovalService`.

The agent's automatic-review consumer applies the same helper immediately
before review. If the active tool context is `standard`, it updates the queued
record to `reviewer="user"` and `humanActionable=true`, emits the normal
approval lifecycle event, and leaves the request unresolved for the UI.

## Verification

Regression tests must first reproduce:

1. exact elevation in a standard `ToolContext` creates a user approval even
   when runtime configuration requests `auto_review`;
2. the same configuration in trusted mode still creates an automatic review;
3. live network approvals in standard mode never call the automatic reviewer;
4. a legacy queued automatic request is converted to human review when the
   active mode is standard.

Focused approval, network, dispatch, and interactive retry suites run before
the complete sandbox suite. A real runtime smoke test confirms that the target
side effect does not occur until the exact approval is manually resolved.

