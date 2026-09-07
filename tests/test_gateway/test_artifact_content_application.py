from __future__ import annotations

from pathlib import Path

import pytest

from opensquilla.application.artifact_workbench import (
    ArtifactContentApplication,
    ArtifactContentQuery,
    AttachmentClaimError,
    AttachmentContentQuery,
    AttachmentOpaqueOversizeError,
    AttachmentStage,
    AttachmentStagingApplication,
    AttachmentStagingPolicy,
    ContentMaterial,
    DocumentContentQuery,
)


class ContentPort:
    def __init__(self) -> None:
        self.calls: list[object] = []

    async def artifact_content(self, query: ArtifactContentQuery) -> ContentMaterial:
        self.calls.append(query)
        return ContentMaterial(Path("artifact.bin"), "application/octet-stream", "a.bin")

    async def document_content(self, query: DocumentContentQuery) -> ContentMaterial:
        self.calls.append(query)
        return ContentMaterial(Path("document.html"), "text/html", "document.html")

    async def attachment_content(self, query: AttachmentContentQuery) -> ContentMaterial:
        self.calls.append(query)
        return ContentMaterial(Path("attachment.bin"), "application/octet-stream")


class StagingPort:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, bytes]] = []

    async def stage_attachment(
        self, *, filename: str, mime: str, payload: bytes
    ) -> tuple[str, float]:
        self.calls.append((filename, mime, payload))
        return "u-attachment", 1234.0


class MimePolicy:
    def validate_claim(self, claimed_mime: str, *, accept_opaque: bool) -> str | None:
        normalized = claimed_mime or None
        if not accept_opaque and normalized is None:
            raise AttachmentClaimError("invalid claim")
        return normalized

    def resolve_mime(self, claimed_mime: str, payload: bytes, *, accept_opaque: bool) -> str:
        del payload, accept_opaque
        return claimed_mime or "application/pdf"

    def is_opaque(self, mime: str) -> bool:
        return mime == "application/octet-stream"


@pytest.mark.asyncio
async def test_content_application_keeps_transport_outside_domain_requests() -> None:
    port = ContentPort()
    application = ArtifactContentApplication(port)

    artifact = await application.artifact(
        ArtifactContentQuery("session-1", "artifact-1", thumbnail=True)
    )
    document = await application.document(
        DocumentContentQuery("session-1", "document-1", "revision-1")
    )
    attachment = await application.attachment(AttachmentContentQuery("session-1", "a" * 64))

    assert artifact.filename == "a.bin"
    assert document.media_type == "text/html"
    assert attachment.path == Path("attachment.bin")
    assert len(port.calls) == 3


@pytest.mark.asyncio
async def test_attachment_staging_resolves_mime_before_port_write() -> None:
    port = StagingPort()
    application = AttachmentStagingApplication(
        port,
        AttachmentStagingPolicy(accept_opaque=True, opaque_max_bytes=1024),
        MimePolicy(),
    )

    staged = await application.stage(AttachmentStage("report.pdf", "", b"%PDF-1.7\nsynthetic"))

    assert staged.file_uuid == "u-attachment"
    assert staged.mime == "application/pdf"
    assert port.calls == [("report.pdf", "application/pdf", b"%PDF-1.7\nsynthetic")]


@pytest.mark.asyncio
async def test_attachment_staging_fails_closed_before_store_mutation() -> None:
    port = StagingPort()
    strict = AttachmentStagingApplication(
        port,
        AttachmentStagingPolicy(accept_opaque=False, opaque_max_bytes=1024),
        MimePolicy(),
    )
    with pytest.raises(AttachmentClaimError):
        strict.validate_claim("")

    opaque = AttachmentStagingApplication(
        port,
        AttachmentStagingPolicy(accept_opaque=True, opaque_max_bytes=3),
        MimePolicy(),
    )
    with pytest.raises(AttachmentOpaqueOversizeError):
        await opaque.stage(
            AttachmentStage("data.bin", "application/octet-stream", b"\x00\x01\x02\x03")
        )
    assert port.calls == []
