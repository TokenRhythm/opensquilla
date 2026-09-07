from __future__ import annotations

from typing import Any

import pytest

from opensquilla.application.artifact_workbench import (
    ArtifactRecoveryApplication,
    CandidatePreviewGrant,
    NativeArtifactOpen,
    NativeArtifactOpenApplication,
    PreviewLeaseCreate,
    PreviewLeaseGrant,
    PreviewLeaseIdentity,
    PreviewLeaseRenewal,
    PreviewMaterialApplication,
)


class _RecoveryPort:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def recover_drafts(self) -> dict[str, int]:
        self.calls.append("drafts")
        return {"examined": 1, "rejected": 1}

    async def recover_mutations(self) -> dict[str, int]:
        self.calls.append("mutations")
        return {"examined": 2, "applied": 2}

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

    async def resolve_candidate(self, handle: str) -> CandidatePreviewGrant:
        self.calls.append(("resolve_candidate", handle))
        return CandidatePreviewGrant(handle, "artifact-1", "session-key", self.grant)

    async def release_candidate(self, handle: str) -> None:
        self.calls.append(("release_candidate", handle))


class _NativeOpenPort:
    def __init__(self) -> None:
        self.commands: list[NativeArtifactOpen] = []

    async def open_artifact(self, command: NativeArtifactOpen) -> None:
        self.commands.append(command)


@pytest.mark.asyncio
async def test_artifact_recovery_application_preserves_dependency_order() -> None:
    port = _RecoveryPort()

    report = await ArtifactRecoveryApplication(port).reconcile()

    assert port.calls == ["drafts", "mutations", "resources"]
    assert report.drafts == {"examined": 1, "rejected": 1}
    assert report.mutations == {"examined": 2, "applied": 2}
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
    assert (await application.resolve_candidate("candidate_abcdefghijklmnop")).lease is port.grant
    await application.release_candidate("candidate_abcdefghijklmnop")

    assert [name for name, _value in port.calls] == [
        "create",
        "renew",
        "revoke",
        "resolve_candidate",
        "release_candidate",
    ]


@pytest.mark.asyncio
async def test_native_artifact_open_application_hides_platform_details() -> None:
    port = _NativeOpenPort()
    command = NativeArtifactOpen("session-key", "artifact-1")

    await NativeArtifactOpenApplication(port).open(command)

    assert port.commands == [command]
