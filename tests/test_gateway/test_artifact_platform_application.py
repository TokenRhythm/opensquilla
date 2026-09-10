from __future__ import annotations

from typing import Any

import pytest

from opensquilla.application.artifact_workbench import (
    ArtifactRecoveryApplication,
    NativeArtifactOpen,
    NativeArtifactOpenApplication,
    PreviewLeaseCreate,
    PreviewLeaseGrant,
    PreviewLeaseIdentity,
    PreviewLeaseRenewal,
    PreviewMaterialApplication,
)


class _RecoveryPort:
    async def retire_legacy_editor(self) -> None:
        self.calls.append("retire")

    def __init__(self) -> None:
        self.calls: list[str] = []



    async def recover_resources(self) -> dict[str, int]:
        self.calls.append("resources")
        return {"imports_examined": 1, "imports_applied": 1}


class _PreviewPort:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.grant = PreviewLeaseGrant(
            lease_id="lease-1",
            token="a" * 32,
            entrypoint="index.html",
            mode="offline",
            client="web",
            source={"kind": "single_file"},
            expires_at="2026-09-03T00:00:00Z",
        )

    async def create_lease(self, command: PreviewLeaseCreate) -> PreviewLeaseGrant:
        self.calls.append(("create", command))
        return self.grant

    async def renew_lease(self, identity: PreviewLeaseIdentity) -> PreviewLeaseRenewal:
        self.calls.append(("renew", identity))
        return PreviewLeaseRenewal(identity.lease_id, "2026-09-03T01:00:00Z")

    async def revoke_lease(self, identity: PreviewLeaseIdentity) -> None:
        self.calls.append(("revoke", identity))




class _NativeOpenPort:
    def __init__(self) -> None:
        self.commands: list[NativeArtifactOpen] = []

    async def open_artifact(self, command: NativeArtifactOpen) -> None:
        self.commands.append(command)


@pytest.mark.asyncio
async def test_artifact_recovery_application_preserves_dependency_order() -> None:
    port = _RecoveryPort()

    report = await ArtifactRecoveryApplication(port).reconcile()

    assert port.calls == ["retire", "resources"]
    assert report.resources == {"imports_examined": 1, "imports_applied": 1}


@pytest.mark.asyncio
async def test_preview_material_application_uses_fixed_semantic_commands() -> None:
    port = _PreviewPort()
    application = PreviewMaterialApplication(port)
    create = PreviewLeaseCreate("session-key", "session-id", "artifact-1", "offline", "web")
    identity = PreviewLeaseIdentity("session-key", "session-id", "lease-1")

    assert await application.create(create) is port.grant
    assert (await application.renew(identity)).lease_id == "lease-1"
    await application.revoke(identity)

    assert [name for name, _value in port.calls] == [
        "create",
        "renew",
        "revoke",
    ]


@pytest.mark.asyncio
async def test_native_artifact_open_application_hides_platform_details() -> None:
    port = _NativeOpenPort()
    command = NativeArtifactOpen("session-key", "artifact-1")

    await NativeArtifactOpenApplication(port).open(command)

    assert port.commands == [command]
