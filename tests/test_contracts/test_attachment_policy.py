from __future__ import annotations

import ast
from pathlib import Path

from opensquilla.contracts import attachments
from opensquilla.gateway import attachment_ingest


def test_fifty_mib_staged_policy_keeps_other_limits_separate() -> None:
    from opensquilla.gateway.config import GatewayConfig
    from opensquilla.gateway.uploads import UploadStore

    for mime in ("application/pdf", "text/plain", attachments.DOCX_MIME, "application/zip"):
        assert attachments.attachment_size_limit_for_mime(mime, staged=True) == 50 * 1024 * 1024
    assert UploadStore(marker_dir=None).max_file_bytes == 50 * 1024 * 1024
    config = GatewayConfig()
    assert config.attachments.opaque_max_bytes == 50 * 1024 * 1024
    assert config.attachments.artifact_max_bytes == 30 * 1024 * 1024
    assert config.attachments.upload_store_max_total_bytes == 300 * 1024 * 1024
    assert attachments.IMAGE_ATTACHMENT_BYTES == 5 * 1024 * 1024
    assert attachments.EMAIL_ATTACHMENT_BYTES == attachments.INLINE_ATTACHMENT_BYTES == 2_000_000
    assert attachments.MAX_TOTAL_ATTACHMENT_BYTES == 60 * 1024 * 1024
    assert attachments.MAX_ATTACHMENTS == 16


def test_attachment_policy_is_shared_with_gateway_ingest() -> None:
    assert attachment_ingest.ALLOWED_MEDIA_TYPES is attachments.ALLOWED_MEDIA_TYPES
    assert attachment_ingest.MAX_ATTACHMENT_BYTES == attachments.MAX_ATTACHMENT_BYTES
    assert (
        attachment_ingest.attachment_size_limit_for_mime("application/pdf", staged=True)
        == attachments.MAX_STAGED_PDF_BYTES
    )


def test_office_documents_are_stageable_above_the_inline_threshold() -> None:
    # Office docs commonly exceed the 2MB inline threshold; they must be
    # stageable so primary clients route them through the upload endpoint
    # instead of rejecting them as "too large".
    for mime in (attachments.DOCX_MIME, attachments.XLSX_MIME, attachments.PPTX_MIME):
        assert attachments.can_stage_attachment_mime(mime) is True
        assert (
            attachments.attachment_size_limit_for_mime(mime, staged=True)
            == attachments.OFFICE_ATTACHMENT_BYTES
        )
        assert attachments.OFFICE_ATTACHMENT_BYTES > attachments.INLINE_ATTACHMENT_BYTES


def test_email_is_capped_at_the_text_limit_and_not_stageable() -> None:
    # Email is plain text whose headers are forgeable, and only bounded body
    # text is extracted, so it must NOT get a larger cap or a staged path that
    # arbitrary content could claim to bypass the 2MB text limit.
    assert attachments.EMAIL_ATTACHMENT_BYTES == attachments.TEXT_ATTACHMENT_BYTES
    for mime in (attachments.EML_MIME, attachments.MBOX_MIME, attachments.MSG_MIME):
        assert mime in attachments.ALLOWED_MEDIA_TYPES
        assert attachments.can_stage_attachment_mime(mime) is False
        assert (
            attachments.attachment_size_limit_for_mime(mime, staged=True)
            == attachments.TEXT_ATTACHMENT_BYTES
        )


def test_attachment_category_is_total() -> None:
    # Every input — rendered mime, unknown mime, garbage, non-strings — must
    # classify; the opaque category is the closed-world catch-all.
    for mime in attachments.ALLOWED_MEDIA_TYPES:
        assert attachments.attachment_category(mime) != "opaque"
        assert attachments.attachment_category(mime) in attachments.ATTACHMENT_CATEGORIES
    for unknown in (
        "application/zip",
        "application/x-7z-compressed",
        "audio/mpeg",
        "video/mp4",
        "application/octet-stream",
        "not-a-mime",
        "",
        None,
        42,
    ):
        assert attachments.attachment_category(unknown) == "opaque"


def test_rendered_categories_match_their_mime_sets() -> None:
    for mime in attachments.IMAGE_ATTACHMENT_MIMES:
        assert attachments.attachment_category(mime) == "image"
    assert attachments.attachment_category("application/pdf") == "pdf"
    for mime in attachments.TEXT_ATTACHMENT_MIMES:
        assert attachments.attachment_category(mime) == "text"
    for mime in attachments.OFFICE_ATTACHMENT_MIMES:
        assert attachments.attachment_category(mime) == "office"
    for mime in (attachments.EML_MIME, attachments.MBOX_MIME, attachments.MSG_MIME):
        assert attachments.attachment_category(mime) == "email"


def test_mime_aliases_resolve_to_canonical_types() -> None:
    # Windows-registry and legacy-browser spellings must land in the same
    # category and size class as their canonical MIME.
    assert attachments.normalize_attachment_mime("image/jpg") == "image/jpeg"
    assert attachments.attachment_category("image/jpg") == "image"
    assert (
        attachments.normalize_attachment_mime("application/x-zip-compressed")
        == "application/zip"
    )
    assert attachments.normalize_attachment_mime("IMAGE/JPG; q=0.8") == "image/jpeg"


def test_text_is_stageable_above_the_inline_threshold() -> None:
    # Large text files (LaTeX sources, logs) must have a staged path instead of
    # dead-ending at the 2MB inline cap. The larger staged ceiling is only
    # honored for payloads ingestion proves to be whole-payload UTF-8.
    for mime in attachments.TEXT_ATTACHMENT_MIMES:
        assert attachments.can_stage_attachment_mime(mime) is True
        assert (
            attachments.attachment_size_limit_for_mime(mime, staged=True)
            == attachments.MAX_STAGED_TEXT_BYTES
        )
        assert (
            attachments.attachment_size_limit_for_mime(mime, staged=False)
            == attachments.TEXT_ATTACHMENT_BYTES
        )
    assert attachments.MAX_STAGED_TEXT_BYTES > attachments.INLINE_ATTACHMENT_BYTES


def test_opaque_types_are_stageable_and_capped() -> None:
    # Any file type is admitted as an opaque workspace file: stageable, with
    # its own staged ceiling, and the conservative default cap when inlined.
    for mime in ("application/zip", "audio/mpeg", attachments.OPAQUE_MIME):
        assert attachments.attachment_category(mime) == "opaque"
        assert attachments.can_stage_attachment_mime(mime) is True
        assert (
            attachments.attachment_size_limit_for_mime(mime, staged=True)
            == attachments.OPAQUE_ATTACHMENT_BYTES
        )
        assert (
            attachments.attachment_size_limit_for_mime(mime, staged=False)
            == attachments.MAX_ATTACHMENT_BYTES
        )
    # An unnormalizable claim is never stageable — the caller must resolve a
    # concrete mime (for example OPAQUE_MIME) before staging.
    assert attachments.can_stage_attachment_mime(None) is False
    assert attachments.can_stage_attachment_mime("   ") is False


def test_sniff_module_stays_out_of_gateway() -> None:
    # The sniffer lives in contracts precisely so policy-only consumers can
    # sniff without importing gateway internals; it must never grow one.
    imports = _imports_from(Path("src/opensquilla/contracts/attachment_sniff.py"))
    assert not any(module.startswith("opensquilla.gateway") for module in imports)
    assert "opensquilla.contracts.attachments" in imports


def test_channel_attachment_io_does_not_import_gateway_policy() -> None:
    imports = _imports_from(Path("src/opensquilla/channels/_attachment_io.py"))

    assert "opensquilla.gateway.attachment_ingest" not in imports
    assert "opensquilla.contracts.attachments" in imports


def test_policy_only_consumers_do_not_import_gateway_ingest() -> None:
    for relative in [
        "src/opensquilla/cli/attachments.py",
        "src/opensquilla/engine/runtime.py",
        "src/opensquilla/gateway/uploads.py",
    ]:
        imports = _imports_from(Path(relative))
        assert "opensquilla.gateway.attachment_ingest" not in imports
        assert "opensquilla.contracts.attachments" in imports


def _imports_from(path: Path) -> list[str]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module)
    return imports
