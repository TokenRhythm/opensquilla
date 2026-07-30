from __future__ import annotations

from pathlib import Path

from opensquilla.sandbox.run_mode import RunMode


def test_standard_auto_grants_required_and_asks_for_expansion(tmp_path: Path) -> None:
    from opensquilla.sandbox.backend.windows_default_acl import (
        AclAccess,
        AclGrant,
        AclGrantKind,
        plan_acl_refresh,
    )

    plan = plan_acl_refresh(
        run_mode=RunMode.STANDARD,
        required=(AclGrant(tmp_path / "workspace", AclAccess.RWX, AclGrantKind.REQUIRED),),
        policy=(AclGrant(tmp_path / "policy", AclAccess.RWX, AclGrantKind.POLICY),),
        expansion=(
            AclGrant(tmp_path / "external-cache", AclAccess.RWX, AclGrantKind.EXPANSION),
        ),
        sensitive_marker=lambda path: None,
    )

    assert [item.path.name for item in plan.auto_grants] == ["workspace", "policy"]
    assert [item.path.name for item in plan.approval_required] == ["external-cache"]
    assert plan.denied == ()


def test_trusted_auto_grants_non_sensitive_expansion(tmp_path: Path) -> None:
    from opensquilla.sandbox.backend.windows_default_acl import (
        AclAccess,
        AclGrant,
        AclGrantKind,
        plan_acl_refresh,
    )

    plan = plan_acl_refresh(
        run_mode=RunMode.TRUSTED,
        required=(),
        policy=(),
        expansion=(AclGrant(tmp_path / "gradle", AclAccess.RWX, AclGrantKind.EXPANSION),),
        sensitive_marker=lambda path: None,
    )

    assert [item.path.name for item in plan.auto_grants] == ["gradle"]
    assert plan.approval_required == ()
    assert plan.denied == ()


def test_sensitive_expansion_is_denied_in_trusted(tmp_path: Path) -> None:
    from opensquilla.sandbox.backend.windows_default_acl import (
        AclAccess,
        AclGrant,
        AclGrantKind,
        plan_acl_refresh,
    )

    plan = plan_acl_refresh(
        run_mode=RunMode.TRUSTED,
        required=(),
        policy=(),
        expansion=(AclGrant(tmp_path / ".ssh", AclAccess.RWX, AclGrantKind.EXPANSION),),
        sensitive_marker=lambda path: "user_secret",
    )

    assert plan.auto_grants == ()
    assert plan.approval_required == ()
    assert len(plan.denied) == 1
    assert plan.denied[0].reason == "user_secret"


def test_policy_grants_bypass_legacy_marker_but_expansions_do_not(tmp_path: Path) -> None:
    from opensquilla.sandbox.backend.windows_default_acl import (
        AclAccess,
        AclGrant,
        AclGrantKind,
        plan_acl_refresh,
    )

    policy = AclGrant(tmp_path / "policy", AclAccess.RX, AclGrantKind.POLICY)
    expansion = AclGrant(tmp_path / "expansion", AclAccess.RWX, AclGrantKind.EXPANSION)

    plan = plan_acl_refresh(
        run_mode=RunMode.TRUSTED,
        required=(),
        policy=(policy,),
        expansion=(expansion,),
        sensitive_marker=lambda _path: "legacy_sensitive",
        required_policy_sensitive_marker=lambda _path: None,
    )

    assert plan.auto_grants == (policy,)
    assert [item.grant for item in plan.denied] == [expansion]


def test_full_host_access_has_no_acl_refresh(tmp_path: Path) -> None:
    from opensquilla.sandbox.backend.windows_default_acl import (
        AclAccess,
        AclGrant,
        AclGrantKind,
        plan_acl_refresh,
    )

    plan = plan_acl_refresh(
        run_mode=RunMode.FULL,
        required=(AclGrant(tmp_path, AclAccess.RWX, AclGrantKind.REQUIRED),),
        policy=(),
        expansion=(),
        sensitive_marker=lambda path: None,
    )

    assert plan.auto_grants == ()
    assert plan.approval_required == ()
    assert plan.denied == ()
