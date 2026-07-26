from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest


class _SessionManager:
    def __init__(self):
        self.node = SimpleNamespace(
            session_key="agent:main:webchat:abc",
            agent_id="main",
            origin=None,
        )

    async def get_session(self, session_key: str):
        return self.node if session_key == self.node.session_key else None

    async def update(self, session_key: str, **fields):
        for key, value in fields.items():
            setattr(self.node, key, value)
        return self.node


def _config():
    return SimpleNamespace(
        sandbox=SimpleNamespace(run_mode="standard", sandbox=True, security_grading=True),
        permissions=SimpleNamespace(default_mode="off"),
    )


def _manager_with_session_key(session_key: str) -> _SessionManager:
    manager = _SessionManager()
    manager.node.session_key = session_key
    return manager


class _FailingUpdateSessionManager(_SessionManager):
    async def update(self, session_key: str, **fields):
        raise RuntimeError("persist failed")


@pytest.mark.asyncio
async def test_mount_domain_and_bundle_grants_persist(tmp_path):
    from opensquilla.sandbox.run_context import get_run_context
    from opensquilla.sandbox.run_context_service import (
        add_domain_grant,
        add_mount_grant,
        enable_bundle_grant,
    )

    manager = _SessionManager()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    await add_mount_grant(
        manager,
        manager.node.session_key,
        path=str(outside),
        access="ro",
        scope="chat",
        config=_config(),
        workspace=str(workspace),
    )
    await add_domain_grant(
        manager,
        manager.node.session_key,
        domain="HTTPS://PyPI.org/simple",
        scope="workspace",
        config=_config(),
        workspace=str(workspace),
    )
    await enable_bundle_grant(
        manager,
        manager.node.session_key,
        bundle_id="python-package-install",
        scope="workspace",
        config=_config(),
        workspace=str(workspace),
    )

    ctx = await get_run_context(
        manager,
        manager.node.session_key,
        config=_config(),
        workspace=str(workspace),
    )
    assert ctx.mounts[0].path == str(outside.resolve(strict=False))
    assert ctx.mounts[0].access == "ro"
    assert ctx.domains[0].domain == "pypi.org"
    assert ctx.bundles[0].bundle_id == "python-package-install"


@pytest.mark.asyncio
async def test_workspace_domain_grant_does_not_write_user_store_when_session_persist_fails(
    tmp_path,
):
    from opensquilla.sandbox.run_context_service import add_domain_grant
    from opensquilla.sandbox.user_grants import load_user_grants_payload

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manager = _FailingUpdateSessionManager()

    with pytest.raises(RuntimeError, match="persist failed"):
        await add_domain_grant(
            manager,
            manager.node.session_key,
            domain="example.com",
            scope="workspace",
            config=_config(),
            workspace=str(workspace),
        )

    assert load_user_grants_payload()["domains"] == []


@pytest.mark.asyncio
async def test_workspace_mount_grant_does_not_write_user_store_when_session_persist_fails(
    tmp_path,
):
    from opensquilla.sandbox.run_context_service import add_mount_grant
    from opensquilla.sandbox.user_grants import load_user_grants_payload

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    manager = _FailingUpdateSessionManager()

    with pytest.raises(RuntimeError, match="persist failed"):
        await add_mount_grant(
            manager,
            manager.node.session_key,
            path=str(outside),
            access="ro",
            scope="workspace",
            config=_config(),
            workspace=str(workspace),
        )

    assert load_user_grants_payload()["mounts"] == []


@pytest.mark.asyncio
async def test_workspace_bundle_grant_does_not_write_user_store_when_session_persist_fails(
    tmp_path,
):
    from opensquilla.sandbox.run_context_service import enable_bundle_grant
    from opensquilla.sandbox.user_grants import load_user_grants_payload

    manager = _FailingUpdateSessionManager()

    with pytest.raises(RuntimeError, match="persist failed"):
        await enable_bundle_grant(
            manager,
            manager.node.session_key,
            bundle_id="python-package-install",
            scope="workspace",
            config=_config(),
            workspace=str(tmp_path),
        )

    assert load_user_grants_payload()["bundles"] == []


@pytest.mark.asyncio
async def test_workspace_public_network_grant_does_not_write_user_store_when_session_persist_fails(
    tmp_path,
):
    from opensquilla.sandbox.run_context_service import add_public_network_grant
    from opensquilla.sandbox.user_grants import load_user_grants_payload

    manager = _FailingUpdateSessionManager()

    with pytest.raises(RuntimeError, match="persist failed"):
        await add_public_network_grant(
            manager,
            manager.node.session_key,
            scope="workspace",
            config=_config(),
            workspace=str(tmp_path),
        )

    assert load_user_grants_payload()["public_network"] == []


def test_user_grants_store_round_trips_payloads(tmp_path):
    from opensquilla.sandbox.user_grants import (
        load_user_grants_payload,
        remove_bundle_grant,
        remove_domain_grant,
        remove_mount_grant,
        remove_public_network_grant,
        upsert_bundle_grant,
        upsert_domain_grant,
        upsert_mount_grant,
        upsert_public_network_grant,
    )

    mount_path = str((tmp_path / "outside").resolve(strict=False))

    upsert_domain_grant(
        {"domain": "example.com", "scope": "workspace", "source": "manual"}
    )
    upsert_mount_grant({"path": mount_path, "access": "ro", "scope": "workspace"})
    upsert_bundle_grant(
        {
            "bundle_id": "python-package-install",
            "scope": "workspace",
            "source": "manual",
        }
    )
    upsert_public_network_grant({"scope": "workspace", "source": "manual"})

    assert load_user_grants_payload() == {
        "domains": [
            {"domain": "example.com", "scope": "workspace", "source": "manual"}
        ],
        "mounts": [{"path": mount_path, "access": "ro", "scope": "workspace"}],
        "bundles": [
            {
                "bundle_id": "python-package-install",
                "scope": "workspace",
                "source": "manual",
            }
        ],
        "public_network": [{"scope": "workspace", "source": "manual"}],
    }

    remove_domain_grant("example.com")
    remove_mount_grant(mount_path)
    remove_bundle_grant("python-package-install")
    remove_public_network_grant("workspace")

    assert load_user_grants_payload() == {
        "domains": [],
        "mounts": [],
        "bundles": [],
        "public_network": [],
    }


def test_user_grants_store_migrates_legacy_json(tmp_path):
    from opensquilla.paths import state_dir
    from opensquilla.sandbox.user_grants import load_user_grants_payload

    mount_path = str((tmp_path / "outside").resolve(strict=False))
    legacy_path = state_dir("sandbox_user_grants.json")
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text(
        json.dumps(
            {
                "domains": [
                    {
                        "domain": "example.com",
                        "scope": "workspace",
                        "source": "manual",
                    }
                ],
                "mounts": [
                    {"path": mount_path, "access": "ro", "scope": "workspace"}
                ],
                "bundles": [
                    {
                        "bundle_id": "python-package-install",
                        "scope": "workspace",
                        "source": "manual",
                    }
                ],
                "public_network": [
                    {
                        "scope": "workspace",
                        "source": "manual",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    assert load_user_grants_payload() == {
        "domains": [
            {"domain": "example.com", "scope": "workspace", "source": "manual"}
        ],
        "mounts": [{"path": mount_path, "access": "ro", "scope": "workspace"}],
        "bundles": [
            {
                "bundle_id": "python-package-install",
                "scope": "workspace",
                "source": "manual",
            }
        ],
        "public_network": [{"scope": "workspace", "source": "manual"}],
    }
    assert legacy_path.exists() is False


def test_user_grants_store_migration_tolerates_concurrent_legacy_unlink(
    monkeypatch: pytest.MonkeyPatch,
):
    from pathlib import Path

    from opensquilla.paths import state_dir
    from opensquilla.sandbox.user_grants import load_user_grants_payload

    legacy_path = state_dir("sandbox_user_grants.json")
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text(
        json.dumps(
            {
                "domains": [
                    {
                        "domain": "example.com",
                        "scope": "workspace",
                        "source": "manual",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    original_unlink = Path.unlink

    def concurrent_unlink(self: Path, *args, **kwargs) -> None:
        if self == legacy_path:
            original_unlink(self, *args, **kwargs)
            raise FileNotFoundError
        original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", concurrent_unlink)

    assert load_user_grants_payload()["domains"] == [
        {"domain": "example.com", "scope": "workspace", "source": "manual"}
    ]
    assert legacy_path.exists() is False


@pytest.mark.asyncio
async def test_durable_user_domain_is_not_materialized_into_session_origin(tmp_path):
    from opensquilla.sandbox.run_context import get_run_context
    from opensquilla.sandbox.run_context_service import add_domain_grant, remove_domain_grant
    from opensquilla.sandbox.user_grants import upsert_domain_grant

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    upsert_domain_grant(
        {"domain": "example.com", "scope": "workspace", "source": "manual"}
    )
    manager = _manager_with_session_key("agent:main:webchat:first")

    await add_domain_grant(
        manager,
        manager.node.session_key,
        domain="chat.example.com",
        scope="chat",
        config=_config(),
        workspace=str(workspace),
    )

    assert manager.node.origin["sandbox_run_context"]["domains"] == [
        {"domain": "chat.example.com", "scope": "chat", "source": "manual"}
    ]

    remover = _manager_with_session_key("agent:main:webchat:second")
    await remove_domain_grant(
        remover,
        remover.node.session_key,
        domain="example.com",
        config=_config(),
        workspace=str(workspace),
    )

    ctx = await get_run_context(
        manager,
        manager.node.session_key,
        config=_config(),
        workspace=str(workspace),
    )

    assert [(grant.domain, grant.scope) for grant in ctx.domains] == [
        ("chat.example.com", "chat")
    ]


@pytest.mark.asyncio
async def test_user_domain_revoke_in_fresh_session_does_not_leave_saved_copy(tmp_path):
    from opensquilla.sandbox.run_context import get_run_context
    from opensquilla.sandbox.run_context_service import add_domain_grant, remove_domain_grant

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    first = _manager_with_session_key("agent:main:webchat:first")

    await add_domain_grant(
        first,
        first.node.session_key,
        domain="example.com",
        scope="workspace",
        config=_config(),
        workspace=str(workspace),
    )

    assert first.node.origin["sandbox_run_context"]["domains"] == []

    second = _manager_with_session_key("agent:main:webchat:second")
    await remove_domain_grant(
        second,
        second.node.session_key,
        domain="example.com",
        config=_config(),
        workspace=str(workspace),
    )

    ctx = await get_run_context(
        first,
        first.node.session_key,
        config=_config(),
        workspace=str(workspace),
    )

    assert ctx.domains == ()


@pytest.mark.asyncio
async def test_legacy_materialized_user_grants_in_origin_are_ignored(tmp_path):
    from opensquilla.sandbox.run_context import (
        PackageBundleGrant,
        PublicNetworkGrant,
        get_run_context,
        run_context_from_origin_payload,
    )

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    manager = _SessionManager()
    origin_payload = {
        "sandbox_run_context": {
            "run_mode": "standard",
            "workspace": str(workspace),
            "mounts": [
                {
                    "path": str(outside),
                    "access": "ro",
                    "scope": "workspace",
                }
            ],
            "domains": [
                {
                    "domain": "example.com",
                    "scope": "workspace",
                    "source": "manual",
                }
            ],
            "bundles": [
                {
                    "bundle_id": "python-package-install",
                    "scope": "workspace",
                    "source": "manual",
                },
                {
                    "bundle_id": "node-package-install",
                    "scope": "workspace",
                    "source": "disabled",
                },
            ],
            "publicNetwork": [
                {
                    "scope": "workspace",
                    "source": "manual",
                },
                {
                    "scope": "chat",
                    "source": "manual",
                },
            ],
        }
    }
    manager.node.origin = origin_payload

    ctx = await get_run_context(
        manager,
        manager.node.session_key,
        config=_config(),
        workspace=str(workspace),
    )

    assert ctx.mounts == ()
    assert ctx.domains == ()
    assert ctx.bundles == (
        PackageBundleGrant(
            bundle_id="node-package-install",
            scope="workspace",
            source="disabled",
        ),
    )
    assert ctx.public_network == (PublicNetworkGrant(scope="chat", source="manual"),)

    routed = run_context_from_origin_payload(
        origin_payload["sandbox_run_context"],
        source="saved",
    )
    assert routed is not None
    assert routed.mounts == ()
    assert routed.domains == ()
    assert routed.bundles == ctx.bundles
    assert routed.public_network == ctx.public_network


@pytest.mark.asyncio
async def test_workspace_domain_grant_persists_to_fresh_session_user_store(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    from opensquilla.sandbox.run_context import DomainGrant, get_run_context
    from opensquilla.sandbox.run_context_service import add_domain_grant

    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path / "home"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manager = _manager_with_session_key("agent:main:webchat:first")

    await add_domain_grant(
        manager,
        manager.node.session_key,
        domain="example.com",
        scope="workspace",
        config=_config(),
        workspace=str(workspace),
    )

    fresh = _manager_with_session_key("agent:main:webchat:fresh")
    ctx = await get_run_context(
        fresh,
        fresh.node.session_key,
        config=_config(),
        workspace=str(workspace),
    )

    assert DomainGrant(domain="example.com", scope="workspace", source="manual") in ctx.domains


@pytest.mark.asyncio
async def test_workspace_mount_grant_persists_to_fresh_session_user_store(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    from opensquilla.sandbox.run_context import MountGrant, get_run_context
    from opensquilla.sandbox.run_context_service import add_mount_grant

    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path / "home"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    manager = _manager_with_session_key("agent:main:webchat:first")

    await add_mount_grant(
        manager,
        manager.node.session_key,
        path=str(outside),
        access="ro",
        scope="workspace",
        config=_config(),
        workspace=str(workspace),
    )

    fresh = _manager_with_session_key("agent:main:webchat:fresh")
    ctx = await get_run_context(
        fresh,
        fresh.node.session_key,
        config=_config(),
        workspace=str(workspace),
    )

    assert (
        MountGrant(
            path=str(outside.resolve(strict=False)),
            access="ro",
            scope="workspace",
        )
        in ctx.mounts
    )


@pytest.mark.asyncio
async def test_workspace_bundle_grant_persists_to_fresh_session_user_store(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    from opensquilla.sandbox.run_context import PackageBundleGrant, get_run_context
    from opensquilla.sandbox.run_context_service import enable_bundle_grant

    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path / "home"))
    manager = _manager_with_session_key("agent:main:webchat:first")

    await enable_bundle_grant(
        manager,
        manager.node.session_key,
        bundle_id="python-package-install",
        scope="workspace",
        config=_config(),
        workspace=str(tmp_path),
    )

    fresh = _manager_with_session_key("agent:main:webchat:fresh")
    ctx = await get_run_context(
        fresh,
        fresh.node.session_key,
        config=_config(),
        workspace=str(tmp_path),
    )

    assert (
        PackageBundleGrant(
            bundle_id="python-package-install",
            scope="workspace",
            source="manual",
        )
        in ctx.bundles
    )


@pytest.mark.asyncio
async def test_workspace_grant_removals_update_user_store(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    from opensquilla.sandbox.run_context import get_run_context
    from opensquilla.sandbox.run_context_service import (
        add_domain_grant,
        add_mount_grant,
        disable_bundle_grant,
        enable_bundle_grant,
        remove_domain_grant,
        remove_mount_grant,
    )

    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path / "home"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    manager = _manager_with_session_key("agent:main:webchat:first")

    await add_domain_grant(
        manager,
        manager.node.session_key,
        domain="example.com",
        scope="workspace",
        config=_config(),
        workspace=str(workspace),
    )
    await add_mount_grant(
        manager,
        manager.node.session_key,
        path=str(outside),
        access="ro",
        scope="workspace",
        config=_config(),
        workspace=str(workspace),
    )
    await enable_bundle_grant(
        manager,
        manager.node.session_key,
        bundle_id="python-package-install",
        scope="workspace",
        config=_config(),
        workspace=str(workspace),
    )

    await remove_domain_grant(
        manager,
        manager.node.session_key,
        domain="example.com",
        config=_config(),
        workspace=str(workspace),
    )
    await remove_mount_grant(
        manager,
        manager.node.session_key,
        path=str(outside),
        config=_config(),
        workspace=str(workspace),
    )
    await disable_bundle_grant(
        manager,
        manager.node.session_key,
        bundle_id="python-package-install",
        config=_config(),
        workspace=str(workspace),
    )

    fresh = _manager_with_session_key("agent:main:webchat:fresh")
    ctx = await get_run_context(
        fresh,
        fresh.node.session_key,
        config=_config(),
        workspace=str(workspace),
    )

    assert [grant.domain for grant in ctx.domains] == []
    assert [grant.path for grant in ctx.mounts] == []
    assert [
        grant.bundle_id
        for grant in ctx.bundles
        if grant.bundle_id == "python-package-install"
    ] == []


@pytest.mark.asyncio
async def test_credential_named_mount_is_allowed_by_permission_model(tmp_path):
    from opensquilla.sandbox.run_context_service import add_mount_grant

    manager = _SessionManager()
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    target = tmp_path / ".ssh" / "id_rsa"
    updated = await add_mount_grant(
        manager,
        manager.node.session_key,
        path=str(target),
        access="ro",
        scope="chat",
        config=_config(),
        workspace=str(workspace),
    )

    assert [(mount.path, mount.access) for mount in updated.mounts] == [
        (str(target.resolve(strict=False)), "ro")
    ]


@pytest.mark.asyncio
async def test_remove_mount_grant_normalizes_caller_path(tmp_path):
    from opensquilla.sandbox.run_context_service import (
        add_mount_grant,
        remove_mount_grant,
    )

    manager = _SessionManager()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    await add_mount_grant(
        manager,
        manager.node.session_key,
        path=str(outside),
        access="ro",
        scope="chat",
        config=_config(),
        workspace=str(workspace),
    )

    updated = await remove_mount_grant(
        manager,
        manager.node.session_key,
        path=str(outside / "nested" / ".."),
        config=_config(),
        workspace=str(workspace),
    )

    assert updated.mounts == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("path_kind", ["root", "sensitive"])
async def test_remove_absent_root_or_credential_mount_is_a_noop(
    tmp_path,
    path_kind,
):
    from opensquilla.sandbox.run_context_service import (
        add_mount_grant,
        remove_mount_grant,
    )

    manager = _SessionManager()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    sensitive_path = tmp_path / ".ssh" / "id_rsa"

    await add_mount_grant(
        manager,
        manager.node.session_key,
        path=str(outside),
        access="ro",
        scope="chat",
        config=_config(),
        workspace=str(workspace),
    )
    origin_before = manager.node.origin
    removal_path = "/" if path_kind == "root" else str(sensitive_path)

    await remove_mount_grant(
        manager,
        manager.node.session_key,
        path=removal_path,
        config=_config(),
        workspace=str(workspace),
    )

    assert manager.node.origin is origin_before
    assert manager.node.origin["sandbox_run_context"]["mounts"] == [
        {"path": str(outside.resolve(strict=False)), "access": "ro", "scope": "chat"}
    ]


@pytest.mark.asyncio
async def test_absent_removals_do_not_create_saved_context(tmp_path):
    from opensquilla.sandbox.run_context_service import (
        disable_bundle_grant,
        remove_domain_grant,
        remove_mount_grant,
    )

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    manager = _SessionManager()
    await remove_mount_grant(
        manager,
        manager.node.session_key,
        path=str(outside),
        config=_config(),
        workspace=str(workspace),
    )
    assert manager.node.origin is None

    manager = _SessionManager()
    await remove_domain_grant(
        manager,
        manager.node.session_key,
        domain="pypi.org",
        config=_config(),
        workspace=str(workspace),
    )
    assert manager.node.origin is None

    manager = _SessionManager()
    await disable_bundle_grant(
        manager,
        manager.node.session_key,
        bundle_id="python-package-install",
        config=_config(),
        workspace=str(workspace),
    )
    assert manager.node.origin["sandbox_run_context"]["bundles"] == [
        {
            "bundle_id": "python-package-install",
            "scope": "workspace",
            "source": "disabled",
        }
    ]


@pytest.mark.asyncio
async def test_absent_removals_preserve_saved_origin(tmp_path):
    from opensquilla.sandbox.run_context_service import (
        disable_bundle_grant,
        remove_domain_grant,
        remove_mount_grant,
    )

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    mounted = tmp_path / "mounted"
    mounted.mkdir()
    absent_mount = tmp_path / "absent"
    absent_mount.mkdir()
    saved_origin = {
        "sandbox_run_context": {
            "run_mode": "standard",
            "workspace": str(workspace),
            "mounts": [
                {
                    "path": str(mounted),
                    "access": "ro",
                    "scope": "chat",
                }
            ],
            "domains": [
                {
                    "domain": "pypi.org",
                    "scope": "chat",
                    "source": "manual",
                }
            ],
            "bundles": [
                {
                    "bundle_id": "python-package-install",
                    "scope": "workspace",
                    "source": "manual",
                }
            ],
        }
    }

    manager = _SessionManager()
    manager.node.origin = saved_origin
    await remove_mount_grant(
        manager,
        manager.node.session_key,
        path=str(absent_mount),
        config=_config(),
        workspace=str(workspace),
    )
    assert manager.node.origin is saved_origin
    assert manager.node.origin == saved_origin

    manager = _SessionManager()
    manager.node.origin = saved_origin
    await remove_domain_grant(
        manager,
        manager.node.session_key,
        domain="files.pythonhosted.org",
        config=_config(),
        workspace=str(workspace),
    )
    assert manager.node.origin is saved_origin
    assert manager.node.origin == saved_origin

    manager = _SessionManager()
    manager.node.origin = saved_origin
    await disable_bundle_grant(
        manager,
        manager.node.session_key,
        bundle_id="node-package-install",
        config=_config(),
        workspace=str(workspace),
    )
    assert manager.node.origin["sandbox_run_context"]["bundles"] == [
        {
            "bundle_id": "node-package-install",
            "scope": "workspace",
            "source": "disabled",
        },
    ]


@pytest.mark.asyncio
async def test_duplicate_mount_grant_replaces_existing_entry(tmp_path):
    from opensquilla.sandbox.run_context_service import add_mount_grant

    manager = _SessionManager()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    await add_mount_grant(
        manager,
        manager.node.session_key,
        path=str(outside),
        access="ro",
        scope="chat",
        config=_config(),
        workspace=str(workspace),
    )
    updated = await add_mount_grant(
        manager,
        manager.node.session_key,
        path=str(outside / "nested" / ".."),
        access="rw",
        scope="workspace",
        config=_config(),
        workspace=str(workspace),
    )
    assert len(updated.mounts) == 1
    assert updated.mounts[0].access == "rw"
    assert updated.mounts[0].scope == "workspace"


@pytest.mark.asyncio
async def test_duplicate_same_mount_grant_is_noop(tmp_path):
    from opensquilla.sandbox.run_context_service import add_mount_grant

    manager = _SessionManager()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    await add_mount_grant(
        manager,
        manager.node.session_key,
        path=str(outside),
        access="ro",
        scope="chat",
        config=_config(),
        workspace=str(workspace),
    )
    origin_before = manager.node.origin
    updated = await add_mount_grant(
        manager,
        manager.node.session_key,
        path=str(outside / "nested" / ".."),
        access="ro",
        scope="chat",
        config=_config(),
        workspace=str(workspace),
    )

    assert updated.source == "saved"
    assert manager.node.origin is origin_before


@pytest.mark.asyncio
async def test_duplicate_same_mount_grant_ignores_stale_workspace_origin_grants(
    tmp_path,
):
    from opensquilla.sandbox.run_context_service import add_mount_grant

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    first = tmp_path / "first"
    middle = tmp_path / "middle"
    last = tmp_path / "last"
    for path in (first, middle, last):
        path.mkdir()
    mount_payload = [
        {"path": str(first.resolve(strict=False)), "access": "ro", "scope": "chat"},
        {"path": str(middle.resolve(strict=False)), "access": "ro", "scope": "chat"},
        {"path": str(last.resolve(strict=False)), "access": "rw", "scope": "workspace"},
    ]
    saved_origin = {
        "sandbox_run_context": {
            "run_mode": "standard",
            "workspace": str(workspace),
            "mounts": mount_payload,
        }
    }
    manager = _SessionManager()
    manager.node.origin = saved_origin

    updated = await add_mount_grant(
        manager,
        manager.node.session_key,
        path=str(middle / "nested" / ".."),
        access="ro",
        scope="chat",
        config=_config(),
        workspace=str(workspace),
    )

    assert [mount.path for mount in updated.mounts] == [
        str(first.resolve(strict=False)),
        str(middle.resolve(strict=False)),
    ]
    assert manager.node.origin is saved_origin
    assert manager.node.origin["sandbox_run_context"]["mounts"] == mount_payload


@pytest.mark.asyncio
async def test_duplicate_domain_grant_replaces_existing_entry(tmp_path):
    from opensquilla.sandbox.run_context_service import add_domain_grant

    manager = _SessionManager()

    await add_domain_grant(
        manager,
        manager.node.session_key,
        domain="https://pypi.org/simple",
        scope="chat",
        config=_config(),
        workspace=str(tmp_path),
    )
    updated = await add_domain_grant(
        manager,
        manager.node.session_key,
        domain="pypi.org",
        scope="workspace",
        config=_config(),
        workspace=str(tmp_path),
    )
    assert len(updated.domains) == 1
    assert updated.domains[0].scope == "workspace"


@pytest.mark.asyncio
async def test_duplicate_same_domain_grant_is_noop(tmp_path):
    from opensquilla.sandbox.run_context_service import add_domain_grant

    manager = _SessionManager()

    await add_domain_grant(
        manager,
        manager.node.session_key,
        domain="https://pypi.org/simple",
        scope="chat",
        config=_config(),
        workspace=str(tmp_path),
    )
    origin_before = manager.node.origin
    updated = await add_domain_grant(
        manager,
        manager.node.session_key,
        domain="pypi.org",
        scope="chat",
        config=_config(),
        workspace=str(tmp_path),
    )

    assert updated.source == "saved"
    assert manager.node.origin is origin_before


@pytest.mark.asyncio
async def test_duplicate_same_domain_grant_ignores_stale_workspace_origin_grants(
    tmp_path,
):
    from opensquilla.sandbox.run_context_service import add_domain_grant

    domain_payload = [
        {"domain": "files.pythonhosted.org", "scope": "chat", "source": "manual"},
        {"domain": "pypi.org", "scope": "workspace", "source": "manual"},
        {"domain": "registry.npmjs.org", "scope": "chat", "source": "manual"},
    ]
    saved_origin = {
        "sandbox_run_context": {
            "run_mode": "standard",
            "workspace": str(tmp_path),
            "domains": domain_payload,
        }
    }
    manager = _SessionManager()
    manager.node.origin = saved_origin

    updated = await add_domain_grant(
        manager,
        manager.node.session_key,
        domain="https://pypi.org/simple",
        scope="workspace",
        config=_config(),
        workspace=str(tmp_path),
    )

    assert [domain.domain for domain in updated.domains] == [
        "files.pythonhosted.org",
        "registry.npmjs.org",
        "pypi.org",
    ]
    assert manager.node.origin["sandbox_run_context"]["domains"] == [
        {"domain": "files.pythonhosted.org", "scope": "chat", "source": "manual"},
        {"domain": "registry.npmjs.org", "scope": "chat", "source": "manual"},
    ]


@pytest.mark.asyncio
async def test_duplicate_bundle_grant_replaces_existing_entry(tmp_path):
    from opensquilla.sandbox.run_context_service import enable_bundle_grant

    manager = _SessionManager()

    await enable_bundle_grant(
        manager,
        manager.node.session_key,
        bundle_id="python-package-install",
        scope="chat",
        config=_config(),
        workspace=str(tmp_path),
    )
    updated = await enable_bundle_grant(
        manager,
        manager.node.session_key,
        bundle_id="python-package-install",
        scope="workspace",
        config=_config(),
        workspace=str(tmp_path),
    )
    assert len(updated.bundles) == 1
    assert updated.bundles[0].scope == "workspace"


@pytest.mark.asyncio
async def test_duplicate_same_bundle_grant_is_noop(tmp_path):
    from opensquilla.sandbox.run_context_service import enable_bundle_grant

    manager = _SessionManager()

    await enable_bundle_grant(
        manager,
        manager.node.session_key,
        bundle_id="python-package-install",
        scope="workspace",
        config=_config(),
        workspace=str(tmp_path),
    )
    origin_before = manager.node.origin
    updated = await enable_bundle_grant(
        manager,
        manager.node.session_key,
        bundle_id=" python-package-install ",
        scope="workspace",
        config=_config(),
        workspace=str(tmp_path),
    )

    assert updated.source == "saved"
    assert manager.node.origin is origin_before


@pytest.mark.asyncio
async def test_duplicate_same_bundle_grant_ignores_stale_workspace_origin_grants(
    tmp_path,
):
    from opensquilla.sandbox.run_context_service import enable_bundle_grant

    bundle_payload = [
        {
            "bundle_id": "node-package-install",
            "scope": "workspace",
            "source": "manual",
        },
        {
            "bundle_id": "python-package-install",
            "scope": "workspace",
            "source": "manual",
        },
        {
            "bundle_id": "rust-package-install",
            "scope": "chat",
            "source": "manual",
        },
    ]
    saved_origin = {
        "sandbox_run_context": {
            "run_mode": "standard",
            "workspace": str(tmp_path),
            "bundles": bundle_payload,
        }
    }
    manager = _SessionManager()
    manager.node.origin = saved_origin

    updated = await enable_bundle_grant(
        manager,
        manager.node.session_key,
        bundle_id=" python-package-install ",
        scope="workspace",
        config=_config(),
        workspace=str(tmp_path),
    )

    assert [bundle.bundle_id for bundle in updated.bundles] == [
        "rust-package-install",
        "python-package-install",
    ]
    assert manager.node.origin["sandbox_run_context"]["bundles"] == [
        {
            "bundle_id": "rust-package-install",
            "scope": "chat",
            "source": "manual",
        },
    ]


@pytest.mark.asyncio
async def test_disable_bundle_grant_persists_disabled_default_override(tmp_path):
    from opensquilla.sandbox.run_context import PackageBundleGrant
    from opensquilla.sandbox.run_context_service import (
        disable_bundle_grant,
        enable_bundle_grant,
    )

    manager = _SessionManager()

    await enable_bundle_grant(
        manager,
        manager.node.session_key,
        bundle_id="python-package-install",
        scope="workspace",
        config=_config(),
        workspace=str(tmp_path),
    )
    updated = await disable_bundle_grant(
        manager,
        manager.node.session_key,
        bundle_id=" python-package-install ",
        config=_config(),
        workspace=str(tmp_path),
    )

    assert updated.bundles == (
        PackageBundleGrant(
            bundle_id="python-package-install",
            scope="workspace",
            source="disabled",
        ),
    )
    assert manager.node.origin["sandbox_run_context"]["bundles"] == [
        {
            "bundle_id": "python-package-install",
            "scope": "workspace",
            "source": "disabled",
        }
    ]


@pytest.mark.asyncio
async def test_enable_bundle_grant_clears_disabled_default_override(tmp_path):
    from opensquilla.sandbox.network_guard import decide_network_access
    from opensquilla.sandbox.run_context import PackageBundleGrant
    from opensquilla.sandbox.run_context_service import (
        disable_bundle_grant,
        enable_bundle_grant,
    )

    manager = _SessionManager()

    disabled = await disable_bundle_grant(
        manager,
        manager.node.session_key,
        bundle_id="node-package-install",
        config=_config(),
        workspace=str(tmp_path),
    )
    assert disabled.bundles[0].source == "disabled"
    assert decide_network_access("registry.npmjs.org", disabled).status == "ask"

    updated = await enable_bundle_grant(
        manager,
        manager.node.session_key,
        bundle_id="node-package-install",
        scope="workspace",
        config=_config(),
        workspace=str(tmp_path),
    )

    assert updated.bundles == (
        PackageBundleGrant(
            bundle_id="node-package-install",
            scope="workspace",
            source="manual",
        ),
    )
    assert decide_network_access("registry.npmjs.org", updated).status == "allow"


@pytest.mark.asyncio
async def test_disable_bundle_grant_rejects_unknown_without_mutation(tmp_path):
    from opensquilla.sandbox.run_context_service import (
        disable_bundle_grant,
        enable_bundle_grant,
    )

    manager = _SessionManager()
    await enable_bundle_grant(
        manager,
        manager.node.session_key,
        bundle_id="python-package-install",
        scope="workspace",
        config=_config(),
        workspace=str(tmp_path),
    )
    origin_before = manager.node.origin

    with pytest.raises(ValueError, match="unknown_package_bundle"):
        await disable_bundle_grant(
            manager,
            manager.node.session_key,
            bundle_id="python-package-intsall",
            config=_config(),
            workspace=str(tmp_path),
        )

    assert manager.node.origin is origin_before
    assert manager.node.origin["sandbox_run_context"]["bundles"] == []


@pytest.mark.asyncio
async def test_set_workspace_normalizes_before_persisting(tmp_path):
    from opensquilla.sandbox.run_context_service import set_workspace

    manager = _SessionManager()
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    updated = await set_workspace(
        manager,
        manager.node.session_key,
        workspace_path=str(workspace / "nested" / ".."),
        config=_config(),
        current_workspace=None,
    )

    assert updated.workspace == str(workspace.resolve(strict=False))
    assert (
        manager.node.origin["sandbox_run_context"]["workspace"]
        == str(workspace.resolve(strict=False))
    )


@pytest.mark.asyncio
async def test_set_workspace_same_normalized_path_is_noop(tmp_path):
    from opensquilla.sandbox.run_context_service import set_workspace

    manager = _SessionManager()
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    updated = await set_workspace(
        manager,
        manager.node.session_key,
        workspace_path=str(workspace / "nested" / ".."),
        config=_config(),
        current_workspace=str(workspace.resolve(strict=False)),
    )

    assert updated.workspace == str(workspace.resolve(strict=False))
    assert manager.node.origin is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "workspace_path",
    [
        "/",
        "/root/project",
        "/run/docker.sock",
        "/var/run/docker.sock",
        "/private/var/run/docker.sock",
        "/root/.opensquilla/workspace/.env.local",
        None,
    ],
)
async def test_set_run_mode_preserves_name_agnostic_fallback_workspace(
    tmp_path,
    workspace_path,
):
    from opensquilla.sandbox.run_context import normalize_workspace_path, set_run_mode
    from opensquilla.sandbox.run_mode import RunMode

    manager = _SessionManager()
    fallback_workspace = (
        str(tmp_path / ".ssh" / "id_rsa")
        if workspace_path is None
        else workspace_path
    )

    updated = await set_run_mode(
        manager,
        manager.node.session_key,
        RunMode.TRUSTED,
        config=_config(),
        workspace=fallback_workspace,
    )

    expected = normalize_workspace_path(fallback_workspace)
    assert updated.workspace == expected
    assert manager.node.origin["sandbox_run_context"]["workspace"] == expected


@pytest.mark.asyncio
async def test_set_workspace_rejects_empty_path(tmp_path):
    from opensquilla.sandbox.run_context_service import set_workspace

    manager = _SessionManager()

    with pytest.raises(ValueError):
        await set_workspace(
            manager,
            manager.node.session_key,
            workspace_path="",
            config=_config(),
            current_workspace=str(tmp_path),
        )
    assert manager.node.origin is None


@pytest.mark.asyncio
async def test_set_workspace_allows_root_nested_deployment_workspace():
    from opensquilla.sandbox.run_context_service import set_workspace

    for workspace_path in (
        "/root/.opensquilla/workspace",
        "/root/.opensquilla/workspace/project/src",
    ):
        manager = _SessionManager()

        updated = await set_workspace(
            manager,
            manager.node.session_key,
            workspace_path=workspace_path,
            config=_config(),
            current_workspace=None,
        )

        assert updated.workspace == workspace_path
        assert manager.node.origin["sandbox_run_context"]["workspace"] == workspace_path


@pytest.mark.asyncio
async def test_set_workspace_allows_paths_without_sensitive_name_rules():
    from opensquilla.sandbox.run_context import normalize_workspace_path
    from opensquilla.sandbox.run_context_service import set_workspace

    for workspace_path in (
        "/run/docker.sock",
        "/var/run/docker.sock",
        "/private/var/run/docker.sock",
        "/root",
        "/root/project",
        "/root/.aws",
        "/root/.kube",
        "/root/.docker/config",
        "/root/.gnupg",
        "/root/.ssh",
        "/root/.opensquilla/workspace/.aws/credentials",
        "/root/.opensquilla/workspace/.kube/config",
        "/root/.opensquilla/workspace/.docker/config",
        "/root/.opensquilla/workspace/.docker/config.json",
        "/root/.opensquilla/workspace/.gnupg/private-keys-v1.d/key",
        "/root/.opensquilla/workspace/id_rsa",
        "/root/.opensquilla/workspace/.ssh/id_rsa",
        "/root/.opensquilla/workspace/.env",
        "/root/.opensquilla/workspace/.env.local",
        "/root/.opensquilla/workspace/.envrc",
        "/root/.opensquilla/workspace/project/.aws/credentials",
        "/root/.opensquilla/workspace/project/.kube/config",
        "/root/.opensquilla/workspace/project/.docker/config.json",
        "/root/.opensquilla/workspace/project/.gnupg/private-keys-v1.d/key",
        "/root/.opensquilla/workspace/project/.env_secret",
    ):
        manager = _SessionManager()
        updated = await set_workspace(
            manager,
            manager.node.session_key,
            workspace_path=workspace_path,
            config=_config(),
            current_workspace=None,
        )
        assert updated.workspace == normalize_workspace_path(workspace_path)


@pytest.mark.asyncio
async def test_set_workspace_allows_credential_named_path(tmp_path):
    from opensquilla.sandbox.run_context_service import set_workspace

    manager = _SessionManager()

    target = tmp_path / ".ssh" / "id_rsa"
    updated = await set_workspace(
        manager,
        manager.node.session_key,
        workspace_path=str(target),
        config=_config(),
        current_workspace=str(tmp_path),
    )
    assert updated.workspace == str(target.resolve(strict=False))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "workspace_parts",
    [
        ("ws", ".aws", "credentials"),
        ("ws", ".kube", "config"),
        ("ws", ".docker", "config"),
        ("ws", ".docker", "config.json"),
        ("ws", ".gnupg", "key"),
        ("ws", ".envrc"),
        ("ws", ".env_secret"),
    ],
)
async def test_set_workspace_allows_non_root_credential_named_targets(
    tmp_path,
    workspace_parts,
):
    from opensquilla.sandbox.run_context_service import set_workspace

    manager = _SessionManager()

    target = tmp_path.joinpath(*workspace_parts)
    updated = await set_workspace(
        manager,
        manager.node.session_key,
        workspace_path=str(target),
        config=_config(),
        current_workspace=None,
    )

    assert updated.workspace == str(target.resolve(strict=False))


@pytest.mark.asyncio
async def test_non_root_nested_workspace_is_allowed_for_set_saved_and_fallback(
    tmp_path,
):
    from opensquilla.sandbox.run_context import get_run_context, set_run_mode
    from opensquilla.sandbox.run_context_service import set_workspace
    from opensquilla.sandbox.run_mode import RunMode

    workspace_path = tmp_path / "ws" / "project" / "src"
    normalized = str(workspace_path.resolve(strict=False))

    manager = _SessionManager()
    updated = await set_workspace(
        manager,
        manager.node.session_key,
        workspace_path=str(workspace_path),
        config=_config(),
        current_workspace=None,
    )
    assert updated.workspace == normalized

    manager = _SessionManager()
    manager.node.origin = {
        "sandbox_run_context": {
            "run_mode": "standard",
            "workspace": str(workspace_path),
        }
    }
    ctx = await get_run_context(
        manager,
        manager.node.session_key,
        config=_config(),
        workspace=None,
    )
    assert ctx.workspace == normalized

    manager = _SessionManager()
    updated = await set_run_mode(
        manager,
        manager.node.session_key,
        RunMode.TRUSTED,
        config=_config(),
        workspace=str(workspace_path),
    )
    assert updated.workspace == normalized
    assert manager.node.origin["sandbox_run_context"]["workspace"] == normalized


@pytest.mark.asyncio
async def test_saved_bundle_id_payload_without_scope_is_ignored_as_user_grant_copy(
    tmp_path,
):
    from opensquilla.sandbox.run_context import get_run_context

    manager = _SessionManager()
    manager.node.origin = {
        "sandbox_run_context": {
            "run_mode": "standard",
            "workspace": str(tmp_path),
            "bundles": [{"bundleId": "python-package-install"}],
        }
    }

    ctx = await get_run_context(
        manager,
        manager.node.session_key,
        config=_config(),
        workspace=str(tmp_path),
    )

    assert ctx.bundles == ()


@pytest.mark.asyncio
async def test_saved_workspace_bundle_payloads_are_ignored_from_origin(tmp_path):
    from opensquilla.sandbox.run_context import get_run_context

    manager = _SessionManager()
    manager.node.origin = {
        "sandbox_run_context": {
            "run_mode": "standard",
            "workspace": str(tmp_path),
            "bundles": [
                {"bundleId": "python-package-install"},
                {"bundle_id": "unknown-package-install"},
            ],
        }
    }

    ctx = await get_run_context(
        manager,
        manager.node.session_key,
        config=_config(),
        workspace=str(tmp_path),
    )

    assert ctx.bundles == ()


@pytest.mark.asyncio
async def test_saved_invalid_scopes_default_safely(tmp_path):
    from opensquilla.sandbox.run_context import get_run_context

    outside = tmp_path / "outside"
    outside.mkdir()
    manager = _SessionManager()
    manager.node.origin = {
        "sandbox_run_context": {
            "run_mode": "standard",
            "workspace": str(tmp_path),
            "mounts": [{"path": str(outside), "scope": "GLOBAL"}],
            "domains": [{"domain": "pypi.org", "scope": "GLOBAL"}],
            "bundles": [
                {
                    "bundle_id": "python-package-install",
                    "scope": "GLOBAL",
                }
            ],
        }
    }

    ctx = await get_run_context(
        manager,
        manager.node.session_key,
        config=_config(),
        workspace=str(tmp_path),
    )

    assert ctx.mounts[0].scope == "chat"
    assert ctx.domains[0].scope == "chat"
    assert ctx.bundles == ()


@pytest.mark.asyncio
async def test_saved_duplicate_bundle_payload_keeps_chat_when_workspace_copy_ignored(
    tmp_path,
):
    from opensquilla.sandbox.run_context import get_run_context

    manager = _SessionManager()
    manager.node.origin = {
        "sandbox_run_context": {
            "run_mode": "standard",
            "workspace": str(tmp_path),
            "bundles": [
                {
                    "bundle_id": "python-package-install",
                    "scope": "chat",
                    "source": "legacy",
                },
                {
                    "bundleId": " python-package-install ",
                    "scope": "workspace",
                    "source": "manual",
                },
            ],
        }
    }

    ctx = await get_run_context(
        manager,
        manager.node.session_key,
        config=_config(),
        workspace=str(tmp_path),
    )

    assert [
        (bundle.bundle_id, bundle.scope, bundle.source) for bundle in ctx.bundles
    ] == [("python-package-install", "chat", "legacy")]


@pytest.mark.asyncio
async def test_saved_root_workspace_is_preserved(tmp_path):
    from opensquilla.sandbox.run_context import get_run_context

    manager = _SessionManager()
    manager.node.origin = {
        "sandbox_run_context": {
            "run_mode": "standard",
            "workspace": "/",
        }
    }

    ctx = await get_run_context(
        manager,
        manager.node.session_key,
        config=_config(),
        workspace=str(tmp_path),
    )

    assert ctx.workspace == "/"


@pytest.mark.asyncio
async def test_saved_root_nested_workspace_is_allowed():
    from opensquilla.sandbox.run_context import get_run_context

    for workspace_path in (
        "/root/.opensquilla/workspace",
        "/root/.opensquilla/workspace/project/src",
    ):
        manager = _SessionManager()
        manager.node.origin = {
            "sandbox_run_context": {
                "run_mode": "standard",
                "workspace": workspace_path,
            }
        }

        ctx = await get_run_context(
            manager,
            manager.node.session_key,
            config=_config(),
            workspace=None,
        )

        assert ctx.workspace == workspace_path


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "workspace_path",
    [
        "/run/docker.sock",
        "/var/run/docker.sock",
        "/private/var/run/docker.sock",
        "/root",
        "/root/project",
        "/root/.aws",
        "/root/.kube",
        "/root/.docker/config",
        "/root/.gnupg",
        "/root/.ssh",
        "/root/.opensquilla/workspace/.aws/credentials",
        "/root/.opensquilla/workspace/.kube/config",
        "/root/.opensquilla/workspace/.docker/config",
        "/root/.opensquilla/workspace/.docker/config.json",
        "/root/.opensquilla/workspace/.gnupg/private-keys-v1.d/key",
        "/root/.opensquilla/workspace/id_rsa",
        "/root/.opensquilla/workspace/.ssh/id_rsa",
        "/root/.opensquilla/workspace/.env",
        "/root/.opensquilla/workspace/.env.local",
        "/root/.opensquilla/workspace/.envrc",
        "/root/.opensquilla/workspace/project/.aws/credentials",
        "/root/.opensquilla/workspace/project/.kube/config",
        "/root/.opensquilla/workspace/project/.docker/config.json",
        "/root/.opensquilla/workspace/project/.gnupg/private-keys-v1.d/key",
        "/root/.opensquilla/workspace/project/.env_secret",
    ],
)
async def test_saved_workspace_is_name_agnostic(workspace_path):
    from opensquilla.sandbox.run_context import get_run_context, normalize_workspace_path

    manager = _SessionManager()
    manager.node.origin = {
        "sandbox_run_context": {
            "run_mode": "standard",
            "workspace": workspace_path,
        }
    }

    ctx = await get_run_context(
        manager,
        manager.node.session_key,
        config=_config(),
        workspace=None,
    )

    assert ctx.workspace == normalize_workspace_path(workspace_path)


@pytest.mark.asyncio
async def test_saved_credential_named_workspace_is_preserved(tmp_path):
    from opensquilla.sandbox.run_context import get_run_context

    manager = _SessionManager()
    manager.node.origin = {
        "sandbox_run_context": {
            "run_mode": "standard",
            "workspace": str(tmp_path / ".ssh" / "id_rsa"),
        }
    }

    ctx = await get_run_context(
        manager,
        manager.node.session_key,
        config=_config(),
        workspace=str(tmp_path),
    )

    assert ctx.workspace == str((tmp_path / ".ssh" / "id_rsa").resolve(strict=False))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "workspace_parts",
    [
        ("ws", ".aws", "credentials"),
        ("ws", ".kube", "config"),
        ("ws", ".docker", "config"),
        ("ws", ".docker", "config.json"),
        ("ws", ".gnupg", "key"),
        ("ws", ".envrc"),
        ("ws", ".env_secret"),
    ],
)
async def test_saved_non_root_credential_named_workspace_is_preserved(
    tmp_path,
    workspace_parts,
):
    from opensquilla.sandbox.run_context import get_run_context

    manager = _SessionManager()
    manager.node.origin = {
        "sandbox_run_context": {
            "run_mode": "standard",
            "workspace": str(tmp_path.joinpath(*workspace_parts)),
        }
    }

    ctx = await get_run_context(
        manager,
        manager.node.session_key,
        config=_config(),
        workspace=None,
    )

    assert ctx.workspace == str(tmp_path.joinpath(*workspace_parts).resolve(strict=False))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "workspace_parts",
    [
        ("ws", ".aws", "credentials"),
        ("ws", ".docker", "config.json"),
        ("ws", ".envrc"),
        ("ws", ".env_secret"),
    ],
)
async def test_set_run_mode_preserves_non_root_credential_named_fallback_workspace(
    tmp_path,
    workspace_parts,
):
    from opensquilla.sandbox.run_context import set_run_mode
    from opensquilla.sandbox.run_mode import RunMode

    manager = _SessionManager()

    updated = await set_run_mode(
        manager,
        manager.node.session_key,
        RunMode.TRUSTED,
        config=_config(),
        workspace=str(tmp_path.joinpath(*workspace_parts)),
    )

    expected = str(tmp_path.joinpath(*workspace_parts).resolve(strict=False))
    assert updated.workspace == expected
    assert manager.node.origin["sandbox_run_context"]["workspace"] == expected


@pytest.mark.asyncio
async def test_saved_workspace_mount_ignores_durable_copy_but_keeps_chat_copy(tmp_path):
    from opensquilla.sandbox.run_context import get_run_context

    valid = tmp_path / "outside"
    valid.mkdir()
    manager = _SessionManager()
    manager.node.origin = {
        "sandbox_run_context": {
            "run_mode": "standard",
            "mounts": [
                {"path": str(tmp_path / ".ssh" / "id_rsa"), "access": "ro"},
                {"path": str(valid), "access": "rw", "scope": "workspace"},
            ],
        }
    }

    ctx = await get_run_context(
        manager,
        manager.node.session_key,
        config=_config(),
        workspace=str(tmp_path / "workspace"),
    )

    assert [(mount.path, mount.access, mount.scope) for mount in ctx.mounts] == [
        (str((tmp_path / ".ssh" / "id_rsa").resolve(strict=False)), "ro", "chat")
    ]


@pytest.mark.asyncio
async def test_saved_workspace_domain_origin_grant_is_ignored(tmp_path):
    from opensquilla.sandbox.run_context import get_run_context

    manager = _SessionManager()
    manager.node.origin = {
        "sandbox_run_context": {
            "run_mode": "standard",
            "domains": [
                {"domain": "127.0.0.1"},
                {"domain": "HTTPS://PyPI.org/simple", "scope": "workspace"},
            ],
        }
    }

    ctx = await get_run_context(
        manager,
        manager.node.session_key,
        config=_config(),
        workspace=str(tmp_path),
    )

    assert ctx.domains == ()


@pytest.mark.asyncio
async def test_saved_duplicate_mounts_and_domains_keep_chat_when_workspace_copy_ignored(
    tmp_path,
):
    from opensquilla.sandbox.run_context import get_run_context

    outside = tmp_path / "outside"
    outside.mkdir()
    manager = _SessionManager()
    manager.node.origin = {
        "sandbox_run_context": {
            "run_mode": "standard",
            "mounts": [
                {"path": str(outside), "access": "ro", "scope": "chat"},
                {
                    "path": str(outside / "nested" / ".."),
                    "access": "rw",
                    "scope": "workspace",
                },
            ],
            "domains": [
                {"domain": "HTTPS://PyPI.org/simple", "scope": "chat"},
                {"domain": "pypi.org", "scope": "workspace", "source": "manual"},
            ],
        }
    }

    ctx = await get_run_context(
        manager,
        manager.node.session_key,
        config=_config(),
        workspace=str(tmp_path),
    )

    assert [(mount.path, mount.access, mount.scope) for mount in ctx.mounts] == [
        (str(outside.resolve(strict=False)), "ro", "chat")
    ]
    assert [(domain.domain, domain.scope, domain.source) for domain in ctx.domains] == [
        ("pypi.org", "chat", "manual")
    ]


@pytest.mark.asyncio
async def test_unrelated_mutation_preserves_permission_valid_saved_entries(tmp_path):
    from opensquilla.sandbox.run_context import get_run_context
    from opensquilla.sandbox.run_context_service import enable_bundle_grant

    valid_mount = tmp_path / "outside"
    valid_mount.mkdir()
    manager = _SessionManager()
    manager.node.origin = {
        "sandbox_run_context": {
            "run_mode": "standard",
            "workspace": "/",
            "mounts": [
                {"path": str(tmp_path / ".ssh" / "id_rsa"), "access": "ro"},
                {"path": str(valid_mount), "access": "rw"},
            ],
            "domains": [
                {"domain": "127.0.0.1"},
                {"domain": "HTTPS://PyPI.org/simple"},
            ],
        }
    }

    await enable_bundle_grant(
        manager,
        manager.node.session_key,
        bundle_id="python-package-install",
        scope="workspace",
        config=_config(),
        workspace=str(tmp_path),
    )

    saved = manager.node.origin["sandbox_run_context"]
    assert saved["workspace"] == "/"
    assert saved["mounts"] == [
        {
            "path": str((tmp_path / ".ssh" / "id_rsa").resolve(strict=False)),
            "access": "ro",
            "scope": "chat",
        },
        {"path": str(valid_mount.resolve(strict=False)), "access": "rw", "scope": "chat"}
    ]
    assert saved["domains"] == [
        {"domain": "pypi.org", "scope": "chat", "source": "manual"}
    ]
    assert saved["bundles"] == []
    effective = await get_run_context(
        manager,
        manager.node.session_key,
        config=_config(),
        workspace=str(tmp_path),
    )
    assert [
        (bundle.bundle_id, bundle.scope, bundle.source)
        for bundle in effective.bundles
    ] == [("python-package-install", "workspace", "manual")]


@pytest.mark.asyncio
async def test_temporary_grants_round_trip(tmp_path):
    from opensquilla.sandbox.run_context import (
        PublicNetworkGrant,
        RunContext,
        TemporaryGrant,
        get_run_context,
        persist_run_context,
    )
    from opensquilla.sandbox.run_mode import RunMode

    manager = _SessionManager()
    grant = TemporaryGrant(
        kind="domain",
        value="pypi.org",
        fingerprint="abc123",
        expires_after="once",
    )

    await persist_run_context(
        manager,
        manager.node.session_key,
        RunContext(
            run_mode=RunMode.STANDARD,
            workspace=str(tmp_path),
            public_network=(PublicNetworkGrant(scope="chat", source="manual"),),
            temporary_grants=(grant,),
            source="saved",
        ),
    )
    ctx = await get_run_context(
        manager,
        manager.node.session_key,
        config=_config(),
        workspace=str(tmp_path),
    )
    payload = ctx.to_origin_payload()

    assert ctx.temporary_grants == (grant,)
    assert ctx.public_network == (PublicNetworkGrant(scope="chat", source="manual"),)
    assert payload["public_network"] == [{"scope": "chat", "source": "manual"}]
    assert payload["temporary_grants"] == [
        {
            "kind": "domain",
            "value": "pypi.org",
            "fingerprint": "abc123",
            "expires_after": "once",
        }
    ]


@pytest.mark.asyncio
async def test_set_run_mode_preserves_bundle_and_temporary_grants(tmp_path):
    from opensquilla.sandbox.run_context import (
        PackageBundleGrant,
        PublicNetworkGrant,
        RunContext,
        TemporaryGrant,
        persist_run_context,
        set_run_mode,
    )
    from opensquilla.sandbox.run_mode import RunMode
    from opensquilla.sandbox.user_grants import upsert_bundle_grant

    manager = _SessionManager()
    bundle = PackageBundleGrant(bundle_id="python-package-install")
    public_network = PublicNetworkGrant(scope="chat")
    temporary = TemporaryGrant(
        kind="domain",
        value="pypi.org",
        fingerprint="abc123",
    )
    upsert_bundle_grant(
        {
            "bundle_id": bundle.bundle_id,
            "scope": bundle.scope,
            "source": bundle.source,
        }
    )
    await persist_run_context(
        manager,
        manager.node.session_key,
        RunContext(
            run_mode=RunMode.STANDARD,
            workspace=str(tmp_path),
            public_network=(public_network,),
            temporary_grants=(temporary,),
            source="saved",
        ),
    )

    updated = await set_run_mode(
        manager,
        manager.node.session_key,
        RunMode.TRUSTED,
        config=_config(),
        workspace=str(tmp_path),
    )

    assert updated.bundles == (bundle,)
    assert updated.public_network == (public_network,)
    assert updated.temporary_grants == (temporary,)


@pytest.mark.asyncio
async def test_apply_network_choice_persists_chat_domain_grant(tmp_path):
    from opensquilla.sandbox.escalation import (
        apply_sandbox_approval_choice,
        build_network_approval_params,
    )
    from opensquilla.sandbox.network_guard import NetworkDecision
    from opensquilla.sandbox.run_context import get_run_context

    manager = _SessionManager()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    params = build_network_approval_params(
        NetworkDecision(
            status="ask",
            normalized_host="example.com",
            reason="unknown_domain",
            source=None,
        ),
        session_key=manager.node.session_key,
        workspace=str(workspace),
        fingerprint="fp123",
    )

    await apply_sandbox_approval_choice(
        params,
        choice="allow_same_type",
        approved=True,
        session_manager=manager,
        config=_config(),
    )

    ctx = await get_run_context(
        manager,
        manager.node.session_key,
        config=_config(),
        workspace=str(workspace),
    )
    assert ("example.com", "chat") in [(grant.domain, grant.scope) for grant in ctx.domains]


@pytest.mark.asyncio
async def test_apply_network_choice_persists_chat_package_bundle_grant(tmp_path):
    from opensquilla.sandbox.escalation import (
        apply_sandbox_approval_choice,
        build_package_bundle_approval_params,
    )
    from opensquilla.sandbox.run_context import get_run_context

    manager = _SessionManager()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    params = build_package_bundle_approval_params(
        "python-package-install",
        session_key=manager.node.session_key,
        workspace=str(workspace),
        fingerprint="fp123",
    )

    await apply_sandbox_approval_choice(
        params,
        choice="allow_same_type",
        approved=True,
        session_manager=manager,
        config=_config(),
    )

    ctx = await get_run_context(
        manager,
        manager.node.session_key,
        config=_config(),
        workspace=str(workspace),
    )
    assert ("python-package-install", "chat") in [
        (grant.bundle_id, grant.scope) for grant in ctx.bundles
    ]


def test_request_sandbox_approval_reissues_matching_approved_approval() -> None:
    from opensquilla.gateway.approval_queue import get_approval_queue, reset_approval_queue
    from opensquilla.sandbox.escalation import (
        build_network_approval_params,
        request_sandbox_approval,
    )
    from opensquilla.sandbox.network_guard import NetworkDecision

    reset_approval_queue()
    params = build_network_approval_params(
        NetworkDecision(
            status="ask",
            normalized_host="example.com",
            reason="unknown_domain",
            source=None,
        ),
        session_key="agent:main:webchat:abc",
        workspace="/tmp/ws",
        fingerprint="fp123",
    )
    assert params is not None
    first = request_sandbox_approval(
        params,
        message="Resolve this approval and retry.",
    )
    old_approval_id = str(first["approval_id"])
    queue = get_approval_queue()
    queue.resolve(old_approval_id, True)

    second = request_sandbox_approval(
        params,
        approval_id=old_approval_id,
        message="Resolve this approval and retry.",
    )

    new_approval_id = str(second["approval_id"])
    assert second["status"] == "approval_required"
    assert new_approval_id != old_approval_id
    assert queue.get(new_approval_id).resolved is False

    reset_approval_queue()


def test_channel_sandbox_approval_request_denies_without_queue() -> None:
    from opensquilla.gateway.approval_queue import get_approval_queue, reset_approval_queue
    from opensquilla.sandbox.escalation import (
        build_path_approval_params,
        request_sandbox_approval,
    )
    from opensquilla.sandbox.path_validation import MountDecision
    from opensquilla.tools.types import CallerKind, ToolContext, current_tool_context

    reset_approval_queue()
    params = build_path_approval_params(
        MountDecision(
            status="request",
            normalized_path="/tmp/outside",
            access="rw",
            reason="outside_sandbox_mounts",
        ),
        session_key="agent:main:feishu:user-1",
        workspace="/tmp/ws",
    )
    assert params is not None
    token = current_tool_context.set(
        ToolContext(
            caller_kind=CallerKind.CHANNEL,
            session_key="agent:main:feishu:user-1",
            channel_kind="feishu",
            sender_id="user-1",
        )
    )
    try:
        payload = request_sandbox_approval(
            params,
            message="Resolve this approval and retry.",
        )
    finally:
        current_tool_context.reset(token)

    assert payload["status"] == "approval_denied"
    assert payload["approval_id"] == ""
    assert "channel sandbox approvals are disabled" in str(payload["message"]).lower()
    assert "/sandbox full" in str(payload["message"])
    assert get_approval_queue().list_pending("exec") == []

    reset_approval_queue()


def test_request_sandbox_approval_reuses_pending_network_class_approval() -> None:
    from opensquilla.gateway.approval_queue import get_approval_queue, reset_approval_queue
    from opensquilla.sandbox.escalation import (
        build_network_approval_params,
        request_sandbox_approval,
    )
    from opensquilla.sandbox.network_guard import NetworkDecision

    reset_approval_queue()
    first_params = build_network_approval_params(
        NetworkDecision(
            status="ask",
            normalized_host="first.example",
            reason="unknown_domain",
            source=None,
        ),
        session_key="agent:main:webchat:abc",
        workspace="/tmp/ws",
        fingerprint="fp-first",
    )
    second_params = build_network_approval_params(
        NetworkDecision(
            status="ask",
            normalized_host="second.example",
            reason="unknown_domain",
            source=None,
        ),
        session_key="agent:main:webchat:abc",
        workspace="/tmp/ws",
        fingerprint="fp-second",
    )
    assert first_params is not None
    assert second_params is not None

    first = request_sandbox_approval(first_params, message="Resolve this approval and retry.")
    second = request_sandbox_approval(second_params, message="Resolve this approval and retry.")

    assert first["status"] == "approval_required"
    assert second["status"] == "approval_pending"
    assert second["approval_id"] == first["approval_id"]
    assert second["host"] == "second.example"
    assert len(get_approval_queue().list_pending("exec")) == 1

    reset_approval_queue()


def test_reused_network_approval_reissues_after_narrow_approval_mismatch() -> None:
    from opensquilla.gateway.approval_queue import get_approval_queue, reset_approval_queue
    from opensquilla.sandbox.escalation import (
        build_network_approval_params,
        request_sandbox_approval,
    )
    from opensquilla.sandbox.network_guard import NetworkDecision

    reset_approval_queue()
    first_params = build_network_approval_params(
        NetworkDecision(
            status="ask",
            normalized_host="first.example",
            reason="unknown_domain",
            source=None,
        ),
        session_key="agent:main:webchat:abc",
        workspace="/tmp/ws",
        fingerprint="fp-first",
    )
    second_params = build_network_approval_params(
        NetworkDecision(
            status="ask",
            normalized_host="second.example",
            reason="unknown_domain",
            source=None,
        ),
        session_key="agent:main:webchat:abc",
        workspace="/tmp/ws",
        fingerprint="fp-second",
    )
    assert first_params is not None
    assert second_params is not None

    first = request_sandbox_approval(first_params, message="Resolve this approval and retry.")
    approval_id = str(first["approval_id"])
    queue = get_approval_queue()
    queue.resolve(approval_id, True)

    second = request_sandbox_approval(
        second_params,
        approval_id=approval_id,
        message="Resolve this approval and retry.",
    )

    assert second["status"] == "approval_required"
    assert second["approval_id"] != approval_id
    assert len(queue.list_pending("exec")) == 1

    reset_approval_queue()


@pytest.mark.asyncio
async def test_apply_network_choice_rejects_removed_public_choice(tmp_path):
    from opensquilla.sandbox.escalation import (
        apply_sandbox_approval_choice,
        build_network_approval_params,
    )
    from opensquilla.sandbox.network_guard import NetworkDecision

    manager = _SessionManager()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    params = build_network_approval_params(
        NetworkDecision(
            status="ask",
            normalized_host="example.com",
            reason="unknown_domain",
            source=None,
        ),
        session_key=manager.node.session_key,
        workspace=str(workspace),
        fingerprint="fp123",
    )

    with pytest.raises(ValueError, match="unknown_sandbox_choice"):
        await apply_sandbox_approval_choice(
            params,
            choice="allow_public_user",
            approved=True,
            session_manager=manager,
            config=_config(),
        )


@pytest.mark.asyncio
async def test_apply_network_once_choice_stays_transient_and_updates_overlay(tmp_path):
    from opensquilla.sandbox.escalation import (
        apply_sandbox_approval_choice,
        build_network_approval_params,
        resolved_run_context_overlay,
    )
    from opensquilla.sandbox.network_guard import NetworkDecision
    from opensquilla.sandbox.run_context import get_run_context

    manager = _SessionManager()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    params = build_network_approval_params(
        NetworkDecision(
            status="ask",
            normalized_host="example.com",
            reason="unknown_domain",
            source=None,
        ),
        session_key=manager.node.session_key,
        workspace=str(workspace),
        fingerprint="fp123",
    )

    await apply_sandbox_approval_choice(
        params,
        choice="allow_once",
        approved=True,
        session_manager=manager,
        config=_config(),
    )

    ctx = await get_run_context(
        manager,
        manager.node.session_key,
        config=_config(),
        workspace=str(workspace),
    )
    assert ctx.temporary_grants == ()
    overlay = resolved_run_context_overlay(manager.node.session_key, str(workspace))
    assert overlay is not None
    assert [(grant.kind, grant.value, grant.fingerprint) for grant in overlay.temporary_grants] == [
        ("domain", "example.com", "fp123")
    ]


@pytest.mark.asyncio
async def test_apply_path_choice_persists_requested_mount(tmp_path):
    from opensquilla.sandbox.escalation import (
        apply_sandbox_approval_choice,
        build_path_approval_params,
    )
    from opensquilla.sandbox.path_validation import MountDecision
    from opensquilla.sandbox.run_context import get_run_context

    manager = _SessionManager()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    params = build_path_approval_params(
        MountDecision(
            status="request",
            normalized_path=str(outside.resolve(strict=False)),
            access="rw",
            reason="outside_sandbox_mounts",
        ),
        session_key=manager.node.session_key,
        workspace=str(workspace),
    )

    await apply_sandbox_approval_choice(
        params,
        choice="allow_same_type",
        approved=True,
        session_manager=manager,
        config=_config(),
    )

    ctx = await get_run_context(
        manager,
        manager.node.session_key,
        config=_config(),
        workspace=str(workspace),
    )
    assert [(grant.path, grant.access, grant.scope) for grant in ctx.mounts] == [
        (str(outside.resolve(strict=False)), "rw", "chat")
    ]


def test_host_once_is_not_a_sandbox_approval_kind():
    import opensquilla.sandbox.escalation as escalation

    assert escalation.is_sandbox_approval_kind("host_once") is False
    assert not hasattr(escalation, "build_backend_failure_approval_params")


@pytest.mark.asyncio
async def test_remove_domain_grant_rejects_invalid_domain(tmp_path):
    from opensquilla.sandbox.run_context_service import remove_domain_grant

    manager = _SessionManager()
    manager.node.origin = {
        "sandbox_run_context": {
            "run_mode": "standard",
            "domains": [{"domain": "pypi.org"}],
        }
    }

    with pytest.raises(ValueError, match="ip_literal"):
        await remove_domain_grant(
            manager,
            manager.node.session_key,
            domain="127.0.0.1",
            config=_config(),
            workspace=str(tmp_path),
        )
    assert manager.node.origin == {
        "sandbox_run_context": {
            "run_mode": "standard",
            "domains": [{"domain": "pypi.org"}],
        }
    }


@pytest.mark.asyncio
async def test_once_mount_grant_is_not_persisted_to_session_origin(tmp_path):
    # "Allow once" must not survive into the durable session origin — otherwise a
    # gateway restart would silently re-grant it (issue #418).
    from opensquilla.sandbox.run_context import RUN_CONTEXT_ORIGIN_KEY
    from opensquilla.sandbox.run_context_service import add_mount_grant

    manager = _SessionManager()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    await add_mount_grant(
        manager,
        manager.node.session_key,
        path=str(outside),
        access="rw",
        scope="once",
        config=_config(),
        workspace=str(workspace),
    )

    origin = manager.node.origin or {}
    persisted = origin.get(RUN_CONTEXT_ORIGIN_KEY, {})
    persisted_mounts = persisted.get("mounts", [])
    assert all(m.get("scope") != "once" for m in persisted_mounts), persisted_mounts


def test_prune_once_mount_grants_expires_only_once_scoped_overlay_mounts():
    # A "once" grant lives in the resolved overlay for the granting turn; pruning
    # at the next turn start must drop it while leaving session-scoped grants.
    from opensquilla.sandbox.escalation import (
        prune_once_mount_grants,
        remember_resolved_run_context,
        reset_resolved_run_context_overlays,
        resolved_run_context_overlay,
    )
    from opensquilla.sandbox.run_context import MountGrant, RunContext
    from opensquilla.sandbox.run_mode import RunMode

    reset_resolved_run_context_overlays()
    try:
        session_key = "agent:main:webchat:once"
        context = RunContext(
            run_mode=RunMode.STANDARD,
            workspace=None,
            mounts=(
                MountGrant(path="/tmp/once", access="rw", scope="once"),
                MountGrant(path="/tmp/session", access="ro", scope="chat"),
            ),
            source="resolved_overlay",
        )
        remember_resolved_run_context(session_key, None, context)

        pruned = prune_once_mount_grants(session_key)
        assert pruned == 1

        overlay = resolved_run_context_overlay(session_key, None)
        remaining_scopes = {m.scope for m in overlay.mounts}
        assert "once" not in remaining_scopes
        assert any(m.path == "/tmp/session" for m in overlay.mounts)
    finally:
        reset_resolved_run_context_overlays()
