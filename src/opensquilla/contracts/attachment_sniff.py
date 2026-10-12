"""Byte-level MIME sniffing shared by every attachment admission surface.

Lives in ``contracts`` (not the gateway) so policy-only consumers — the
upload route, the CLI, channel adapters — can sniff without importing
gateway internals. Only magic bytes and complete-payload checks are used;
no member of a container is ever decompressed beyond the zip central
directory, so sniffing is not an extraction vector.
"""

from __future__ import annotations

from opensquilla.contracts.attachments import (
    DOCX_MIME,
    EML_MIME,
    MBOX_MIME,
    MSG_MIME,
    OLE_MAGIC,
    PDF_MAGIC,
    PPTX_MIME,
    SNIFF_PEEK_BYTES,
    XLSX_MIME,
    ZIP_MAGIC,
)

__all__ = [
    "sniff_mime_from_bytes",
]


def _sniff_ooxml_mime(raw: bytes) -> str | None:
    """Identify a specific OOXML subtype from a zip container's part names.

    Reads only the central directory (``namelist``); no member is decompressed,
    so this is not a zip-bomb vector. Returns ``None`` for non-OOXML zips.
    """

    import io
    import zipfile

    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            names = set(archive.namelist())
    except (zipfile.BadZipFile, OSError, ValueError):
        return None
    if "word/document.xml" in names:
        return DOCX_MIME
    if "xl/workbook.xml" in names:
        return XLSX_MIME
    if "ppt/presentation.xml" in names:
        return PPTX_MIME
    return None


def _looks_like_rfc5322_headers(text: str) -> bool:
    """True when ``text`` opens with an RFC 5322 header block carrying a strong
    email signal (Message-ID/Received/MIME-Version, or both From and Date)."""

    header_names: set[str] = set()
    for line in text.splitlines():
        if not line:
            break  # blank line terminates the header block
        if line[0] in " \t":
            continue  # folded continuation of the previous header
        name, sep, _ = line.partition(":")
        if not sep or not name or any(ch <= " " for ch in name):
            return False  # first non-header line and no email evidence yet
        header_names.add(name.strip().lower())
        if header_names & {"message-id", "received", "mime-version"} or {
            "from",
            "date",
        } <= header_names:
            return True
    return False


def _sniff_email_mime(text: str) -> str | None:
    """Detect a text-based email (.eml / .mbox) from the decoded head.

    Requires a strong RFC 5322 signal so ordinary prose is not misread as an
    email. An mbox must carry a real ``From `` envelope line *followed by* a
    header block — a bare ``From `` prefix (e.g. "From the start…") is not
    enough, so prose cannot inherit the larger email size cap.
    """

    if text.startswith("From "):
        newline = text.find("\n")
        if newline != -1 and _looks_like_rfc5322_headers(text[newline + 1 :]):
            return MBOX_MIME
        return None
    if _looks_like_rfc5322_headers(text):
        return EML_MIME
    return None


def sniff_mime_from_bytes(raw: bytes) -> str | None:
    """Detect MIME from authoritative magic bytes or complete JSON payloads."""

    head = raw[:SNIFF_PEEK_BYTES]
    if head.startswith(PDF_MAGIC):
        return "application/pdf"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"GIF87a") or head.startswith(b"GIF89a"):
        return "image/gif"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "image/webp"
    if head.startswith(ZIP_MAGIC):
        ooxml = _sniff_ooxml_mime(raw)
        if ooxml is not None:
            return ooxml
    if head.startswith(OLE_MAGIC):
        # OLE2 compound file — treated as an Outlook .msg here; the extractor
        # degrades gracefully if it turns out to be another OLE format.
        return MSG_MIME

    text: str | None
    try:
        text = head.decode("utf-8")
    except UnicodeDecodeError as exc:
        if (
            len(raw) > len(head)
            and exc.end >= len(head)
            and exc.reason == "unexpected end of data"
        ):
            # A multibyte sequence straddles the peek boundary; the complete
            # prefix is still clean, so head-based sniffs can run and the
            # whole-payload check below decides text-ness.
            text = head[: exc.start].decode("utf-8")
        else:
            # Genuinely undecodable head: skip the head-based sniffs but still
            # fall through to the whole-payload check rather than declaring
            # binary from the peek window alone.
            text = None

    if text is not None:
        email_mime = _sniff_email_mime(text)
        if email_mime is not None:
            return email_mime

        stripped = text.lstrip()
        if stripped.startswith("{") or stripped.startswith("["):
            try:
                import json as _json

                _json.loads(raw.decode("utf-8"))
                return "application/json"
            except (UnicodeDecodeError, ValueError):
                pass

    # Last resort: a fully clean-UTF-8, NUL-free payload is treated as plain
    # text so unknown-but-textual uploads degrade to readable context instead of
    # a hard rejection. The ENTIRE payload is validated (not just the peek
    # window) so a text head with a binary tail is not misclassified. This is a
    # weak signal — callers never let it override a more specific claimed type.
    if b"\x00" in raw:
        return None
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return "text/plain"
