"""Attachment policy shared by gateway and channel runtime boundaries.

Any file type may be attached; the policy here decides how each type is
*represented*, not whether it is admitted. ``attachment_category`` routes a
MIME claim into one of six representation families — the five rendered
families (image / pdf / text / office / email) keep their extraction and
anti-forgery behavior, and everything else is ``opaque``: stored and
materialized into the agent workspace for tool access, never decoded,
decompressed, or inlined into a provider prompt.
"""

from __future__ import annotations

from typing import Any, Literal

# Modern Office Open XML (OOXML) document MIME types. These are zip containers,
# so they are extracted to text server-side rather than sent to a provider raw.
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"

# Email message formats. .eml/.mbox are text (RFC 5322); .msg is an OLE
# compound file. All are extracted to bounded text server-side.
EML_MIME = "message/rfc822"
MBOX_MIME = "application/mbox"
MSG_MIME = "application/vnd.ms-outlook"

# Canonical label for opaque payloads (unrecognized or generic binary types).
OPAQUE_MIME = "application/octet-stream"

# The RENDERED media types: attachments whose content is extracted or inlined
# for the model (vision blocks, bounded text extraction). This set is NOT an
# admission gate — types outside it are admitted as opaque workspace files.
ALLOWED_MEDIA_TYPES: frozenset[str] = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "application/pdf",
        "text/plain",
        "text/markdown",
        "text/html",
        "text/csv",
        "application/json",
        DOCX_MIME,
        XLSX_MIME,
        PPTX_MIME,
        EML_MIME,
        MBOX_MIME,
        MSG_MIME,
    }
)

IMAGE_ATTACHMENT_MIMES: frozenset[str] = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
    }
)
TEXT_ATTACHMENT_MIMES: frozenset[str] = frozenset(
    {
        "text/plain",
        "text/markdown",
        "text/html",
        "text/csv",
        "application/json",
    }
)
OFFICE_ATTACHMENT_MIMES: frozenset[str] = frozenset(
    {
        DOCX_MIME,
        XLSX_MIME,
        PPTX_MIME,
    }
)
EMAIL_ATTACHMENT_MIMES: frozenset[str] = frozenset(
    {
        EML_MIME,
        MBOX_MIME,
        MSG_MIME,
    }
)

AttachmentCategory = Literal["image", "pdf", "text", "office", "email", "opaque"]

ATTACHMENT_CATEGORIES: tuple[AttachmentCategory, ...] = (
    "image",
    "pdf",
    "text",
    "office",
    "email",
    "opaque",
)

MAX_ATTACHMENTS = 10
INLINE_ATTACHMENT_BYTES = 2 * 1000 * 1000
TEXT_ATTACHMENT_BYTES = INLINE_ATTACHMENT_BYTES
IMAGE_ATTACHMENT_BYTES = 5 * 1024 * 1024
MAX_ATTACHMENT_BYTES = IMAGE_ATTACHMENT_BYTES
MAX_STAGED_PDF_BYTES = 30 * 1024 * 1024
# Office documents are zip containers extracted to bounded text; the raw upload
# is never forwarded to a provider, so a generous ceiling is safe.
OFFICE_ATTACHMENT_BYTES = 30 * 1024 * 1024
# Email is held to the text cap, NOT a larger ceiling. Only bounded body text +
# headers + an attachment-name listing are extracted (embedded attachment bytes
# are never read), so a large raw email buys nothing — and since email is plain
# text whose headers are trivially forgeable, a larger cap would just let
# arbitrary content claim an email mime to bypass the text limit.
EMAIL_ATTACHMENT_BYTES = TEXT_ATTACHMENT_BYTES
# Staged text may exceed the inline threshold only because ingestion proves the
# WHOLE payload is NUL-free UTF-8 before honoring the larger cap; a binary
# claiming a text mime is reclassified opaque instead of inheriting this limit.
MAX_STAGED_TEXT_BYTES = 30 * 1024 * 1024
# Opaque payloads are never parsed or forwarded to a provider — bytes go to the
# content-addressed store and the agent workspace only — so their staged
# ceiling matches the other staged families.
OPAQUE_ATTACHMENT_BYTES = 30 * 1024 * 1024
MAX_TOTAL_ATTACHMENT_BYTES = 60 * 1024 * 1024
SNIFF_PEEK_BYTES = 1024
PDF_MAGIC = b"%PDF-"
# Local file header signature shared by all zip-based formats (OOXML, ODF…).
ZIP_MAGIC = b"PK\x03\x04"
# OLE2 / Compound File Binary signature (Outlook .msg, legacy Office).
OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

# Non-canonical spellings seen in the wild (Windows registry entries, older
# browsers) mapped to their canonical MIME so category and size policy resolve
# deterministically instead of falling through to the opaque default.
_MIME_ALIASES: dict[str, str] = {
    "image/jpg": "image/jpeg",
    "application/x-zip-compressed": "application/zip",
    "application/x-gzip": "application/gzip",
}


def normalize_attachment_mime(mime: Any) -> str | None:
    if not isinstance(mime, str):
        return None
    normalized = mime.split(";", 1)[0].strip().lower()
    if not normalized:
        return None
    return _MIME_ALIASES.get(normalized, normalized)


def attachment_category(mime: Any) -> AttachmentCategory:
    """Route any MIME claim to its representation category (total function)."""

    normalized = normalize_attachment_mime(mime)
    if normalized in IMAGE_ATTACHMENT_MIMES:
        return "image"
    if normalized == "application/pdf":
        return "pdf"
    if normalized in TEXT_ATTACHMENT_MIMES:
        return "text"
    if normalized in OFFICE_ATTACHMENT_MIMES:
        return "office"
    if normalized in EMAIL_ATTACHMENT_MIMES:
        return "email"
    return "opaque"


def can_stage_attachment_mime(mime: Any) -> bool:
    # Email is intentionally NOT stageable: it is capped at the text limit
    # (only bounded, forgeable-header text is ever extracted), so a staged path
    # would raise the abuse ceiling without buying anything.
    normalized = normalize_attachment_mime(mime)
    if normalized is None:
        return False
    return attachment_category(normalized) != "email"


def attachment_size_limit_for_mime(mime: Any, *, staged: bool = False) -> int:
    category = attachment_category(mime)
    if category == "pdf":
        return MAX_STAGED_PDF_BYTES if staged else MAX_ATTACHMENT_BYTES
    if category == "text":
        return MAX_STAGED_TEXT_BYTES if staged else TEXT_ATTACHMENT_BYTES
    if category == "image":
        return IMAGE_ATTACHMENT_BYTES
    if category == "office":
        return OFFICE_ATTACHMENT_BYTES
    if category == "email":
        return EMAIL_ATTACHMENT_BYTES
    return OPAQUE_ATTACHMENT_BYTES if staged else MAX_ATTACHMENT_BYTES


__all__ = [
    "ALLOWED_MEDIA_TYPES",
    "ATTACHMENT_CATEGORIES",
    "AttachmentCategory",
    "IMAGE_ATTACHMENT_BYTES",
    "IMAGE_ATTACHMENT_MIMES",
    "INLINE_ATTACHMENT_BYTES",
    "MAX_ATTACHMENT_BYTES",
    "MAX_ATTACHMENTS",
    "MAX_STAGED_PDF_BYTES",
    "MAX_STAGED_TEXT_BYTES",
    "MAX_TOTAL_ATTACHMENT_BYTES",
    "OPAQUE_ATTACHMENT_BYTES",
    "OPAQUE_MIME",
    "PDF_MAGIC",
    "SNIFF_PEEK_BYTES",
    "TEXT_ATTACHMENT_BYTES",
    "TEXT_ATTACHMENT_MIMES",
    "attachment_category",
    "attachment_size_limit_for_mime",
    "can_stage_attachment_mime",
    "normalize_attachment_mime",
]
