from __future__ import annotations

from pathlib import Path

from opensquilla.sandbox.file_policy import (
    authority_roots_for_state,
    decide_file_access,
)
from opensquilla.sandbox.policy_models import SandboxPolicy


def test_authority_roots_are_non_overridable_deny_read_and_write(tmp_path: Path) -> None:
    state = tmp_path / "state"
    target = state / "sessions.db"
    policy = SandboxPolicy.model_validate(
        {"files": {"customDenyWritePaths": []}}
    )

    for operation in ("read", "write", "delete"):
        decision = decide_file_access(
            operation,
            target,
            policy,
            authority_roots=authority_roots_for_state(state),
            platform="linux",
        )

        assert decision.allowed is False
        assert decision.approval_required is False
        assert decision.code == "sandbox_authority_read_denied"
        assert decision.rule_source == "authority"


def test_authority_descendant_and_root_are_both_denied(tmp_path: Path) -> None:
    authority = tmp_path / "backup-vault"
    roots = (authority,)

    assert (
        decide_file_access(
            "read",
            authority,
            SandboxPolicy(),
            authority_roots=roots,
        ).allowed
        is False
    )
    assert (
        decide_file_access(
            "read",
            authority / "entry" / "manifest.json",
            SandboxPolicy(),
            authority_roots=roots,
        ).allowed
        is False
    )
