from __future__ import annotations

import base64
from pathlib import Path

import pytest

from opensquilla.attachment_refs import (
    is_attachment_ref,
    promote_pending_chat_input_attachments,
    read_attachment_ref_bytes,
    read_pending_chat_input_manifest,
    read_pending_chat_input_promotions,
    transcript_material_path,
)
from opensquilla.contracts.attachments import SNIFF_PEEK_BYTES
from opensquilla.gateway.attachment_ingest import (
    ingest_attachments,
    stage_pending_chat_input_attachments,
)
from opensquilla.gateway.uploads import UploadStore


@pytest.mark.asyncio
async def test_channel_bytes_attachment_normalizes_to_engine_shape() -> None:
    result = await ingest_attachments(
        "read it",
        [{"name": "note.txt", "mime_type": "text/plain", "data": b"hello"}],
        failure_mode="mark",
        mark_bytes_as_staged=True,
    )

    assert result.text == "read it"
    assert result.failures == []
    assert result.attachments == [
        {
            "name": "note.txt",
            "type": "text/plain",
            "data": base64.b64encode(b"hello").decode("ascii"),
            "_was_staged": True,
        }
    ]


@pytest.mark.asyncio
async def test_url_only_channel_attachment_degrades_with_marker() -> None:
    result = await ingest_attachments(
        "read it",
        [{"name": "remote.pdf", "mime_type": "application/pdf", "url": "https://example.test/x.pdf"}],
        failure_mode="mark",
        mark_bytes_as_staged=True,
    )

    assert result.attachments == []
    assert result.failures[0].reason == "missing_data"
    assert "[attachment unavailable: remote.pdf: missing_data]" in result.text


@pytest.mark.asyncio
async def test_download_failure_raises_in_rpc_mode() -> None:
    with pytest.raises(ValueError, match="download_failed: boom"):
        await ingest_attachments(
            "read it",
            [{"name": "remote.pdf", "mime_type": "application/pdf", "_ingest_error": "boom"}],
            failure_mode="raise",
        )


@pytest.mark.asyncio
async def test_failure_marker_sanitizes_attachment_name() -> None:
    result = await ingest_attachments(
        "read it",
        [{"name": "bad\nname.pdf", "mime_type": "application/pdf", "_ingest_error": "boom"}],
        failure_mode="mark",
    )

    assert "[attachment unavailable: bad name.pdf: download_failed]" in result.text


_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _docx_bytes(paragraph: str = "Hello DOCX") -> bytes:
    import io

    from docx import Document

    document = Document()
    document.add_paragraph(paragraph)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


@pytest.mark.asyncio
async def test_docx_bytes_sniffed_and_accepted() -> None:
    result = await ingest_attachments(
        "read it",
        [{"name": "report.docx", "mime_type": _DOCX_MIME, "data": _docx_bytes()}],
        failure_mode="mark",
    )

    assert result.failures == []
    assert len(result.attachments) == 1
    assert result.attachments[0]["type"] == _DOCX_MIME


@pytest.mark.asyncio
async def test_fake_docx_rejected_on_mime_mismatch() -> None:
    # A plain-text payload claiming to be a Word document must be fail-closed:
    # the bytes are not an OOXML zip, so the sniffer cannot confirm the claim.
    result = await ingest_attachments(
        "read it",
        [{"name": "fake.docx", "mime_type": _DOCX_MIME, "data": b"just text, not a zip"}],
        failure_mode="mark",
    )

    assert result.attachments == []
    assert result.failures[0].reason == "mime_mismatch"


_EML_MIME = "message/rfc822"
_MSG_MIME = "application/vnd.ms-outlook"


def _eml_bytes() -> bytes:
    from email.message import EmailMessage

    message = EmailMessage()
    message["From"] = "alice@example.com"
    message["To"] = "bob@example.com"
    message["Subject"] = "Hi"
    message["Date"] = "Mon, 01 Jan 2026 00:00:00 +0000"
    message.set_content("Body.")
    return message.as_bytes()


@pytest.mark.asyncio
async def test_eml_bytes_sniffed_and_accepted() -> None:
    result = await ingest_attachments(
        "read it",
        [{"name": "note.eml", "mime_type": _EML_MIME, "data": _eml_bytes()}],
        failure_mode="mark",
    )

    assert result.failures == []
    assert result.attachments[0]["type"] == _EML_MIME


@pytest.mark.asyncio
async def test_unknown_textual_upload_accepted_via_utf8_fallback() -> None:
    # An unsupported claimed mime whose bytes are clean UTF-8 text degrades to
    # text/plain instead of being rejected.
    result = await ingest_attachments(
        "read it",
        [
            {
                "name": "trace.log",
                "mime_type": "application/x-logfile",
                "data": b"line one\nline two\n",
            }
        ],
        failure_mode="mark",
    )

    assert result.failures == []
    assert result.attachments[0]["type"] == "text/plain"


@pytest.mark.asyncio
async def test_unknown_binary_upload_accepted_as_opaque() -> None:
    # Binary payloads with unrendered claims are admitted as opaque: bytes are
    # never parsed or inlined, and the specific claim survives as the label.
    result = await ingest_attachments(
        "read it",
        [
            {
                "name": "blob.bin",
                "mime_type": "application/x-unknown",
                "data": b"\x00\x01\x02\x03binary",
            }
        ],
        failure_mode="mark",
    )

    assert result.failures == []
    assert result.attachments[0]["type"] == "application/x-unknown"


@pytest.mark.asyncio
async def test_unknown_binary_upload_rejected_when_opaque_admission_disabled() -> None:
    # accept_opaque=False restores the legacy fail-closed gate with the same
    # error copy third-party clients may match on.
    result = await ingest_attachments(
        "read it",
        [
            {
                "name": "blob.bin",
                "mime_type": "application/x-unknown",
                "data": b"\x00\x01\x02\x03binary",
            }
        ],
        failure_mode="mark",
        accept_opaque=False,
    )

    assert result.attachments == []
    assert result.failures[0].reason == "unsupported_mime"
    assert "is not allowed; must be one of" in result.failures[0].detail


@pytest.mark.asyncio
async def test_zip_upload_accepted_as_opaque_not_ooxml() -> None:
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("paper/main.tex", "\\documentclass{article}")
    result = await ingest_attachments(
        "read it",
        [{"name": "paper.zip", "mime_type": "application/zip", "data": buffer.getvalue()}],
        failure_mode="mark",
        mark_bytes_as_staged=True,
    )

    assert result.failures == []
    assert result.attachments[0]["type"] == "application/zip"
    assert result.attachments[0]["_was_staged"] is True


@pytest.mark.asyncio
async def test_unknown_claim_adopts_sniffed_rendered_type() -> None:
    # An image uploaded with a generic claim is routed to the image family by
    # its magic bytes instead of degrading to an opaque blob.
    png = b"\x89PNG\r\n\x1a\n" + b"fake image body"
    result = await ingest_attachments(
        "read it",
        [{"name": "shot", "mime_type": "application/octet-stream", "data": png}],
        failure_mode="mark",
    )

    assert result.failures == []
    assert result.attachments[0]["type"] == "image/png"


@pytest.mark.asyncio
async def test_opaque_limit_bytes_caps_opaque_payloads() -> None:
    result = await ingest_attachments(
        "read it",
        [
            {
                "name": "blob.bin",
                "mime_type": "application/x-unknown",
                "data": b"\x00" + b"a" * 2048,
            }
        ],
        failure_mode="mark",
        mark_bytes_as_staged=True,
        opaque_limit_bytes=1024,
    )

    assert result.attachments == []
    assert result.failures[0].reason == "oversize"


@pytest.mark.asyncio
async def test_binary_claiming_text_above_inline_cap_reclassified_opaque() -> None:
    # A binary claiming a text mime must not shop for the 30MiB staged-text
    # ceiling: without whole-payload UTF-8 proof it is reclassified opaque.
    from opensquilla.contracts.attachments import OPAQUE_MIME, TEXT_ATTACHMENT_BYTES

    payload = b"\xff\xfe" + b"a" * (TEXT_ATTACHMENT_BYTES + 64)
    result = await ingest_attachments(
        "read it",
        [{"name": "fake.csv", "mime_type": "text/csv", "data": payload}],
        failure_mode="mark",
        mark_bytes_as_staged=True,
    )

    assert result.failures == []
    assert result.attachments[0]["type"] == OPAQUE_MIME


@pytest.mark.asyncio
async def test_binary_claiming_text_above_inline_cap_rejected_when_strict() -> None:
    from opensquilla.contracts.attachments import TEXT_ATTACHMENT_BYTES

    payload = b"\xff\xfe" + b"a" * (TEXT_ATTACHMENT_BYTES + 64)
    result = await ingest_attachments(
        "read it",
        [{"name": "fake.csv", "mime_type": "text/csv", "data": payload}],
        failure_mode="mark",
        mark_bytes_as_staged=True,
        accept_opaque=False,
    )

    assert result.attachments == []
    assert result.failures[0].reason == "oversize"


@pytest.mark.asyncio
async def test_unknown_text_head_with_binary_tail_is_not_text() -> None:
    # The head (peek window) is clean ASCII but the tail is binary. The text
    # fallback validates the whole payload, so these stay opaque, never text.
    clean_head = b"a" * (SNIFF_PEEK_BYTES + 64)
    for tail in (b"\x00\x01\x02", b"\xff\xfe\xfd"):  # NUL tail, then invalid-UTF-8 tail
        result = await ingest_attachments(
            "read it",
            [
                {
                    "name": "sneaky.dat",
                    "mime_type": "application/x-unknown",
                    "data": clean_head + tail,
                }
            ],
            failure_mode="mark",
        )
        assert result.failures == []
        assert result.attachments[0]["type"] == "application/x-unknown"


@pytest.mark.asyncio
async def test_multibyte_char_straddling_peek_boundary_still_text() -> None:
    # A multibyte character split by the sniff peek window must not classify an
    # otherwise clean UTF-8 payload as binary.
    payload = b"a" * (SNIFF_PEEK_BYTES - 1) + "中文正文，跨越窥探窗口。".encode()
    result = await ingest_attachments(
        "read it",
        [{"name": "notes.tex", "mime_type": "application/x-tex", "data": payload}],
        failure_mode="mark",
    )

    assert result.failures == []
    assert result.attachments[0]["type"] == "text/plain"


@pytest.mark.asyncio
async def test_undecodable_head_with_valid_utf8_payload_is_not_misread() -> None:
    # An invalid-UTF-8 head must not be sniffed as text even when the bytes
    # after it are clean: the whole-payload check stays authoritative, so the
    # item is admitted opaque under its claimed label rather than as text.
    payload = b"\xff\xfe" + b"clean tail " * 8
    result = await ingest_attachments(
        "read it",
        [{"name": "blob.bin", "mime_type": "application/x-unknown", "data": payload}],
        failure_mode="mark",
    )

    assert result.failures == []
    assert result.attachments[0]["type"] == "application/x-unknown"


@pytest.mark.asyncio
async def test_text_fallback_does_not_override_claimed_type() -> None:
    # CSV content sniffs as text/plain, but the weak fallback must not downgrade
    # the more specific claimed text/csv type.
    result = await ingest_attachments(
        "read it",
        [{"name": "data.csv", "mime_type": "text/csv", "data": b"a,b\n1,2\n"}],
        failure_mode="mark",
    )

    assert result.failures == []
    assert result.attachments[0]["type"] == "text/csv"


@pytest.mark.asyncio
async def test_plain_text_claiming_email_rejected() -> None:
    # Arbitrary text must not claim message/rfc822 to inherit the larger email
    # size ceiling instead of the 2MB text cap; without email structure it is
    # rejected rather than accepted under the email label.
    result = await ingest_attachments(
        "read it",
        [
            {
                "name": "notes.eml",
                "mime_type": _EML_MIME,
                "data": b"just some plain notes, definitely not an email\n",
            }
        ],
        failure_mode="mark",
    )

    assert result.attachments == []
    assert result.failures[0].reason == "mime_mismatch"


_MBOX_MIME = "application/mbox"


def _mbox_bytes() -> bytes:
    return b"From alice@example.com Mon Jan  1 00:00:00 2026\n" + _eml_bytes() + b"\n"


@pytest.mark.asyncio
async def test_prose_starting_with_from_claiming_mbox_rejected() -> None:
    # A bare "From " prefix is not an mbox: prose must not inherit the email cap.
    result = await ingest_attachments(
        "read it",
        [
            {
                "name": "story.mbox",
                "mime_type": _MBOX_MIME,
                "data": b"From the very start this was prose.\nStill prose here.\n",
            }
        ],
        failure_mode="mark",
    )

    assert result.attachments == []
    assert result.failures[0].reason == "mime_mismatch"


@pytest.mark.asyncio
async def test_real_mbox_sniffed_and_accepted() -> None:
    result = await ingest_attachments(
        "read it",
        [{"name": "inbox.mbox", "mime_type": _MBOX_MIME, "data": _mbox_bytes()}],
        failure_mode="mark",
    )

    assert result.failures == []
    assert result.attachments[0]["type"] == _MBOX_MIME


@pytest.mark.asyncio
async def test_fake_msg_rejected_on_mime_mismatch() -> None:
    result = await ingest_attachments(
        "read it",
        [{"name": "fake.msg", "mime_type": _MSG_MIME, "data": b"not an OLE compound file"}],
        failure_mode="mark",
    )

    assert result.attachments == []
    assert result.failures[0].reason == "mime_mismatch"


_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


@pytest.mark.asyncio
async def test_ole_payload_with_specific_claim_stays_opaque() -> None:
    # OLE magic is shared by legacy Office formats: a specific non-Outlook
    # claim must survive both the claimed-None carve-out and the
    # mismatch-resolution branch instead of being misfiled as email.
    result = await ingest_attachments(
        "read it",
        [
            {
                "name": "legacy.doc",
                "mime_type": "application/msword",
                "data": _OLE_MAGIC + b"rest of a word file",
            }
        ],
        failure_mode="mark",
    )

    assert result.failures == []
    assert result.attachments[0]["type"] == "application/msword"


@pytest.mark.asyncio
async def test_ole_payload_without_claim_still_sniffs_as_msg() -> None:
    result = await ingest_attachments(
        "read it",
        [{"name": "mystery", "data": _OLE_MAGIC + b"rest of an OLE file"}],
        failure_mode="mark",
    )

    assert result.failures == []
    assert result.attachments[0]["type"] == "application/vnd.ms-outlook"


@pytest.mark.asyncio
async def test_unknown_textual_upload_accepted_when_opaque_admission_disabled() -> None:
    # Legacy parity is two-sided: strict mode still honors the UTF-8 fallback.
    result = await ingest_attachments(
        "read it",
        [{"name": "trace.log", "mime_type": "application/x-logfile", "data": b"line one\n"}],
        failure_mode="mark",
        accept_opaque=False,
    )

    assert result.failures == []
    assert result.attachments[0]["type"] == "text/plain"


@pytest.mark.asyncio
async def test_strict_mode_keeps_staged_text_at_inline_cap() -> None:
    # accept_opaque=False restores the legacy stageable set, so staged text
    # stays at the 2MB inline cap instead of the 30MiB staged ceiling.
    from opensquilla.contracts.attachments import TEXT_ATTACHMENT_BYTES

    payload = b"a" * (TEXT_ATTACHMENT_BYTES + 1)
    result = await ingest_attachments(
        "read it",
        [{"name": "big.csv", "mime_type": "text/csv", "data": payload}],
        failure_mode="mark",
        mark_bytes_as_staged=True,
        accept_opaque=False,
    )

    assert result.attachments == []
    assert result.failures[0].reason == "oversize"


@pytest.mark.asyncio
async def test_windows_jpeg_alias_admitted_in_strict_mode() -> None:
    # image/jpg normalizes to image/jpeg via the alias table in every mode —
    # a deliberate strict-mode improvement over the legacy 415.
    result = await ingest_attachments(
        "read it",
        [{"name": "photo.jpg", "mime_type": "image/jpg", "data": b"\xff\xd8\xff" + b"j" * 32}],
        failure_mode="mark",
        accept_opaque=False,
    )

    assert result.failures == []
    assert result.attachments[0]["type"] == "image/jpeg"


@pytest.mark.asyncio
@pytest.mark.parametrize("persist_enabled", [False, True], ids=["disabled", "default"])
async def test_uuid_ingress_respects_persistence_without_consuming_upload(
    tmp_path: Path, persist_enabled: bool,
) -> None:
    payload = b"\x89PNG\r\n\x1a\n" + b"synthetic image material"
    media_root = tmp_path / "media"
    store = UploadStore(marker_dir=media_root / "uploads")
    file_uuid = await store.put("sample.png", "image/png", payload)
    original_upload = await store.get(file_uuid)
    options = {} if persist_enabled else {"persist_enabled": False, "disk_budget_bytes": 0}

    result = await ingest_attachments(
        "inspect", [{"file_uuid": file_uuid, "type": "image/png", "name": "sample.png"}],
        store=store, material_root=media_root, session_id="accepted-session", **options,
    )

    assert result.failures == []
    assert result.consumed_file_uuids == ([] if persist_enabled else [file_uuid])
    assert (await store.get(file_uuid))[0] == original_upload[0]
    assert len(result.attachments) == 1
    if persist_enabled:
        ref = result.attachments[0]
        assert is_attachment_ref(ref)
        assert ref["store"] == "inputs"
        assert ref["owner"] == "accepted-session"
        assert ref["resource_id"] == file_uuid
        assert read_attachment_ref_bytes(ref, media_root=media_root) == payload
        assert (
            media_root / "inputs" / "accepted-session" / file_uuid / "sample.png"
        ).read_bytes() == payload
    else:
        assert result.attachments == [{
            "type": "image/png", "name": "sample.png",
            "data": base64.b64encode(payload).decode("ascii"), "_was_staged": True,
        }]
        assert not (media_root / "transcripts").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("persist_enabled", [False, True], ids=["disabled", "default"])
async def test_queue_promotion_respects_persistence_and_retains_queue_material(
    tmp_path: Path, persist_enabled: bool,
) -> None:
    payload = b"\x89PNG\r\n\x1a\n" + b"synthetic queued image material"
    store = UploadStore(marker_dir=tmp_path / "upload-markers")
    file_uuid = await store.put("queued.png", "image/png", payload)
    original_upload = await store.get(file_uuid)
    media_root = tmp_path / "media"
    staged = await stage_pending_chat_input_attachments(
        [{"file_uuid": file_uuid, "type": "image/png", "name": "queued.png"}],
        store=store, material_root=media_root, session_id="queued-session",
        pending_input_id="queued-input", enqueue_fingerprint="sha256:" + "a" * 64,
    )
    assert staged.failures == []
    assert staged.consumed_file_uuids == [file_uuid]
    assert len(staged.attachments) == 1
    queue_ref = dict(staged.attachments[0])
    assert is_attachment_ref(queue_ref)
    assert read_attachment_ref_bytes(queue_ref, media_root=media_root) == payload
    manifest_before = read_pending_chat_input_manifest(
        media_root=media_root, session_id="queued-session", pending_input_id="queued-input",
    )
    assert manifest_before is not None
    files_before = {
        path: path.read_bytes() for path in media_root.rglob("*") if path.is_file()
    }
    canonical_path = transcript_material_path(
        media_root, "accepted-session", queue_ref["sha256"],
    )
    assert not canonical_path.exists()
    assert not transcript_material_path(
        media_root, "queued-session", queue_ref["sha256"],
    ).exists()
    options = {} if persist_enabled else {"persist_enabled": False, "disk_budget_bytes": 0}

    promoted = promote_pending_chat_input_attachments(
        staged.attachments, media_root=media_root, pending_input_id="queued-input",
        target_session_id="accepted-session", **options,
    )

    assert staged.attachments == [queue_ref]
    assert read_attachment_ref_bytes(queue_ref, media_root=media_root) == payload
    assert await store.get(file_uuid) == original_upload
    assert read_pending_chat_input_manifest(
        media_root=media_root, session_id="queued-session", pending_input_id="queued-input",
    ) == manifest_before
    promotions = read_pending_chat_input_promotions(
        media_root=media_root, source_session_id="queued-session", pending_input_id="queued-input",
    )
    assert len(promoted) == 1
    assert promoted[0] is not staged.attachments[0]
    if persist_enabled:
        assert is_attachment_ref(promoted[0])
        assert promoted[0]["scope"] == "accepted-session"
        assert canonical_path.read_bytes() == payload
        assert read_attachment_ref_bytes(promoted[0], media_root=media_root) == payload
        assert promotions == {"accepted-session": {queue_ref["sha256"]}}
    else:
        assert promoted == [{
            "type": "image/png", "name": "queued.png",
            "data": base64.b64encode(payload).decode("ascii"), "_was_staged": True,
        }]
        assert promotions == {}
        assert not canonical_path.exists()
        assert {
            path: path.read_bytes() for path in media_root.rglob("*") if path.is_file()
        } == files_before

@pytest.mark.asyncio
async def test_persisted_upload_ref_uses_store_filename_and_survives_session_scope(
    tmp_path: Path,
) -> None:
    media_root = tmp_path / "media"
    store = UploadStore(marker_dir=media_root / "uploads")
    file_uuid = await store.put("actual-name.txt", "text/plain", b"hello")

    result = await ingest_attachments(
        "inspect",
        [{"file_uuid": file_uuid, "type": "text/plain", "name": "spoofed.txt"}],
        store=store,
        material_root=media_root,
        session_id="source-session",
    )

    ref = result.attachments[0]
    assert ref["store"] == "inputs"
    assert ref["name"] == "actual-name.txt"
    assert read_attachment_ref_bytes(ref, media_root=media_root) == b"hello"
    assert not result.consumed_file_uuids
    # The resource is claimed by the accepted session and survives the upload lease.
    assert (media_root / "inputs" / "source-session" / file_uuid / "actual-name.txt").is_file()
