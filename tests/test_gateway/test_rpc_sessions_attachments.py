"""Tests for gateway attachment validation.

Any file type is admitted: rendered families (image/*, application/pdf,
text-family, OOXML office, email) keep extraction and anti-forgery behavior,
and everything else is accepted as an opaque item whose bytes are staged for
the agent workspace, never parsed or inlined. The validator sniffs MIME from
decoded bytes and prefers the sniffed type on mismatch, the per-turn cap is
10, and a {file_uuid: ...} reference shape is accepted for the upload store.
"""

from __future__ import annotations

import base64
from typing import Any

import pytest

from opensquilla.gateway import rpc_sessions
from opensquilla.gateway.rpc_sessions import (
    _ALLOWED_MEDIA_TYPES,
    _MAX_ATTACHMENT_BYTES,
    _MAX_ATTACHMENTS,
    _MAX_TEXT_ATTACHMENT_BYTES,
    _validate_attachments,
)


def _b64(payload: bytes) -> str:
    return base64.b64encode(payload).decode("ascii")


def _attach(media_type: str, payload: bytes, **extra: Any) -> dict[str, Any]:
    item: dict[str, Any] = {"type": media_type, "data": _b64(payload)}
    item.update(extra)
    return item


# ---------------------------------------------------------------------------
# The RENDERED families are locked at exactly the supported MIMEs (images,
# PDF, text-family, modern OOXML office documents, email). This set routes
# model-facing representation; it is no longer an admission gate.
# ---------------------------------------------------------------------------

def test_rendered_media_types_set_contents() -> None:
    assert _ALLOWED_MEDIA_TYPES == {
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
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "message/rfc822",
        "application/mbox",
        "application/vnd.ms-outlook",
    }


def test_max_attachments_per_turn_is_ten() -> None:
    assert _MAX_ATTACHMENTS == 10


# ---------------------------------------------------------------------------
# Inline acceptance for each non-image MIME class.
# ---------------------------------------------------------------------------

def test_pdf_inline_accepted() -> None:
    pdf_bytes = b"%PDF-1.4\n%fake one-page pdf body\n"
    out = _validate_attachments([_attach("application/pdf", pdf_bytes, name="r.pdf")])
    assert len(out) == 1
    assert out[0]["type"] == "application/pdf"
    assert out[0]["data"] == _b64(pdf_bytes)


@pytest.mark.parametrize(
    ("claimed_mime", "payload"),
    [
        ("text/plain", b"hello world\n"),
        ("text/csv", b"col_a,col_b\n1,2\n3,4\n"),
        ("application/json", b'{"k": "v"}'),
        ("text/markdown", b"# title\n\nbody\n"),
    ],
)
def test_text_csv_json_inline_accepted(claimed_mime: str, payload: bytes) -> None:
    name = f"f.{claimed_mime.split('/')[-1]}"
    out = _validate_attachments([_attach(claimed_mime, payload, name=name)])
    assert len(out) == 1
    assert out[0]["type"] == claimed_mime


def test_html_inline_accepted() -> None:
    html = b"<html><body>hi</body></html>"
    out = _validate_attachments([_attach("text/html", html, name="page.html")])
    assert len(out) == 1
    assert out[0]["type"] == "text/html"


# ---------------------------------------------------------------------------
# Opaque admission and rejection paths.
# ---------------------------------------------------------------------------

def test_unknown_binary_mime_accepted_as_opaque() -> None:
    # Binary payloads with unrendered claims are admitted as opaque items:
    # bytes are never parsed or inlined, and the specific claim survives as
    # the label. Unknown *textual* uploads still resolve to text/plain — see
    # test_unknown_textual_upload_accepted_via_utf8_fallback in the ingest suite.
    out = _validate_attachments(
        [_attach("application/x-binary", b"\x00\x01\x02\x03 binary blob", name="x.bin")]
    )
    assert len(out) == 1
    assert out[0]["type"] == "application/x-binary"


def test_unknown_binary_mime_rejected_when_opaque_admission_disabled() -> None:
    # attachments.accept_opaque=false restores the legacy fail-closed gate,
    # including the error copy third-party clients may match on.
    from opensquilla.gateway.attachment_ingest import validate_attachments

    with pytest.raises(ValueError, match="not allowed"):
        validate_attachments(
            [_attach("application/x-binary", b"\x00\x01\x02\x03 binary blob", name="x.bin")],
            accept_opaque=False,
        )


def test_oversize_rejected() -> None:
    payload = b"%PDF-1.4\n" + b"a" * (_MAX_ATTACHMENT_BYTES + 1)
    with pytest.raises(ValueError, match="exceeds"):
        _validate_attachments([_attach("application/pdf", payload, name="big.pdf")])


def test_text_family_above_direct_cap_rejected() -> None:
    payload = b"a" * (_MAX_TEXT_ATTACHMENT_BYTES + 1)
    with pytest.raises(ValueError, match="exceeds"):
        _validate_attachments([_attach("text/plain", payload, name="big.txt")])


def test_too_many_attachments_rejected() -> None:
    items = [_attach("text/plain", b"x", name=f"f{i}.txt") for i in range(_MAX_ATTACHMENTS + 1)]
    with pytest.raises(ValueError, match="at most"):
        _validate_attachments(items)


# ---------------------------------------------------------------------------
# MIME sniffing semantics.
# ---------------------------------------------------------------------------

def test_mime_sniff_overrides_client_claim() -> None:
    """A claimed text/plain payload that is actually PDF magic is upgraded.

    When the sniffed MIME is in the allow-list and differs from the client
    claim, the sniffed type wins.
    """
    pdf_bytes = b"%PDF-1.4\nbody\n"
    out = _validate_attachments([_attach("text/plain", pdf_bytes, name="weird.txt")])
    assert out[0]["type"] == "application/pdf"


def test_claimed_pdf_without_magic_bytes_rejected() -> None:
    """Claimed application/pdf without %PDF- magic is hard-rejected.

    A large blob of arbitrary bytes claiming PDF is a text-bomb vector;
    sniffer must not silently downgrade. Locked decision: raise ValueError
    matching '415' or 'magic'.
    """
    not_a_pdf = b"definitely not a pdf, just text bytes\n"
    with pytest.raises(ValueError, match=r"(magic|415|application/pdf)"):
        _validate_attachments([_attach("application/pdf", not_a_pdf, name="liar.pdf")])


def test_mime_sniff_logs_warning_on_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mismatch between claimed and sniffed MIME emits a structured warning.

    The validator uses structlog (not stdlib logging) so we capture via
    monkeypatch on the module-level logger rather than caplog — testing
    the contract, not the framework plumbing.
    """
    captured: list[tuple[str, dict[str, Any]]] = []

    def _record_warning(event: str, **kwargs: Any) -> None:
        captured.append((event, kwargs))

    real_log = rpc_sessions.log

    class _WarningLog:
        def warning(self, event: str, **kwargs: Any) -> None:
            _record_warning(event, **kwargs)

        def __getattr__(self, name: str) -> Any:
            return getattr(real_log, name)

    monkeypatch.setattr(rpc_sessions, "log", _WarningLog())

    pdf_bytes = b"%PDF-1.4\nbody\n"
    _validate_attachments([_attach("text/plain", pdf_bytes, name="weird.txt")])

    assert any(
        "mime" in event.lower() and "mismatch" in event.lower() for event, _ in captured
    ), captured
    mismatch = next(kwargs for event, kwargs in captured if "mismatch" in event)
    assert mismatch.get("claimed") == "text/plain"
    assert mismatch.get("sniffed") == "application/pdf"


# ---------------------------------------------------------------------------
# file_uuid reference shape.
# ---------------------------------------------------------------------------

def test_file_uuid_reference_resolved() -> None:
    """Validator accepts {file_uuid, name, mime} without inline data.

    The validator must not crash on the staged upload shape and must thread
    ``file_uuid`` through for downstream materialization.
    """
    out = _validate_attachments(
        [
            {
                "file_uuid": "u-deadbeef",
                "mime": "application/pdf",
                "name": "big.pdf",
            }
        ]
    )
    assert len(out) == 1
    assert out[0]["file_uuid"] == "u-deadbeef"
    assert out[0]["type"] == "application/pdf"
    assert "data" not in out[0]


def test_attachment_with_both_data_and_file_uuid_rejected() -> None:
    """Validator rejects 400 if an attachment carries both inline data AND file_uuid.

    Both fields means the client was confused; pick one instead of silently
    coercing.
    """
    item = _attach("application/pdf", b"%PDF-1.4\n", name="x.pdf")
    item["file_uuid"] = "u-1234"
    with pytest.raises(ValueError, match=r"(both|exactly one)"):
        _validate_attachments([item])


async def test_resolve_attachments_expired_uuid_raises_typed_error(tmp_path) -> None:
    """An expired staged uuid must raise a typed, recoverable error carrying the
    attachment index + uuid — not a bare ValueError that collapses to a generic,
    non-retryable INVALID_REQUEST (issue #468)."""
    from opensquilla.gateway.attachment_ingest import (
        ATTACHMENT_EXPIRED_CODE,
        AttachmentResolutionError,
        resolve_attachments,
    )
    from opensquilla.gateway.uploads import AttachmentNotFoundError

    class _ExpiredStore:
        async def get(self, ref: str):  # noqa: ANN202
            raise AttachmentNotFoundError(ref)

    with pytest.raises(AttachmentResolutionError) as excinfo:
        await resolve_attachments(
            [{"file_uuid": "u-gone", "mime": "application/pdf", "name": "x.pdf"}],
            store=_ExpiredStore(),
            material_root=tmp_path,
            session_id="s1",
        )
    err = excinfo.value
    assert err.code == ATTACHMENT_EXPIRED_CODE
    assert err.attachment_index == 1
    assert err.file_uuid == "u-gone"
    assert err.recoverable is True
    assert isinstance(err, ValueError)  # backward compatible with existing handlers


async def test_resolve_attachments_restart_loss_raises_typed_error(tmp_path) -> None:
    from opensquilla.gateway.attachment_ingest import (
        ATTACHMENT_LOST_IN_RESTART_CODE,
        AttachmentResolutionError,
        resolve_attachments,
    )
    from opensquilla.gateway.uploads import AttachmentLostInRestartError

    class _LostStore:
        async def get(self, ref: str):  # noqa: ANN202
            raise AttachmentLostInRestartError(ref)

    with pytest.raises(AttachmentResolutionError) as excinfo:
        await resolve_attachments(
            [{"file_uuid": "u-lost", "mime": "application/pdf", "name": "x.pdf"}],
            store=_LostStore(),
            material_root=tmp_path,
            session_id="s1",
        )
    assert excinfo.value.code == ATTACHMENT_LOST_IN_RESTART_CODE
    assert excinfo.value.recoverable is True
