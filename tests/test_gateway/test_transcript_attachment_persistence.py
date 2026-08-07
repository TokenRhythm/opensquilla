"""Tests for transcript attachment persistence.

The persistence path is tested via a pure-function helper
``build_transcript_attachment_envelope`` that takes resolved attachments
and produces both the JSON envelope written to ``transcript_entries``
and the side-effect of writing staged attachments to
``media/transcripts/<session>/<sha256>``.

The envelope MUST NOT use the field name ``file_uuid``; that is an
upload-store concept which would leak into the engine on replay. Staged
attachments use ``sha256_ref`` instead.

Deferred coverage: dedupe-within-session against an existing on-disk sha. The
current implementation deduplicates implicitly because sha-keyed paths overwrite
to the same content.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest

from opensquilla.gateway.transcripts import (
    build_transcript_attachment_envelope,
    rebuild_attachments_for_replay,
)


def _b64(payload: bytes) -> str:
    return base64.b64encode(payload).decode("ascii")


# ---------------------------------------------------------------------------
# Test 1 — regression: inline attachment shape preserved.
# ---------------------------------------------------------------------------

def test_inline_attachment_stored_in_transcript_envelope(tmp_path: Path) -> None:
    inline = {"type": "image/png", "data": _b64(b"\x89PNG\r\n\x1a\n"), "name": "p.png"}
    envelope, writes = build_transcript_attachment_envelope(
        text="hi",
        attachments=[inline],
        session_id="s1",
        media_root=tmp_path,
        persist_enabled=True,
    )
    parsed = json.loads(envelope)
    assert parsed["text"] == "hi"
    persisted = parsed["attachments"][0]
    assert persisted["type"] == "image/png"
    assert persisted["data"] == inline["data"]
    assert "sha256_ref" not in persisted
    assert writes == []


def test_transcript_envelope_can_separate_provider_and_display_text(tmp_path: Path) -> None:
    inline = {"type": "image/png", "data": _b64(b"\x89PNG\r\n\x1a\n"), "name": "p.png"}
    envelope, _writes = build_transcript_attachment_envelope(
        text="Describe these attachments",
        display_text="",
        attachments=[inline],
        session_id="s1",
        media_root=tmp_path,
        persist_enabled=True,
    )

    parsed = json.loads(envelope)

    assert parsed["text"] == "Describe these attachments"
    assert parsed["display_text"] == ""
    assert parsed["attachments"][0]["name"] == "p.png"


def test_meta_replay_history_persists_readable_text_without_capability_token(
    tmp_path: Path,
) -> None:
    envelope, _writes = build_transcript_attachment_envelope(
        text="/meta-replay",
        display_text="Retry failed step · meta-paper-write",
        attachments=[],
        session_id="replay-session",
        media_root=tmp_path,
        persist_enabled=True,
    )

    parsed = json.loads(envelope)
    assert parsed["text"] == "/meta-replay"
    assert parsed["display_text"] == "Retry failed step · meta-paper-write"
    assert "token" not in envelope.lower()


# ---------------------------------------------------------------------------
# Test 2 — staged attachment persisted to disk by sha256, envelope uses sha256_ref.
# ---------------------------------------------------------------------------

def test_staged_attachment_persisted_to_disk_and_referenced_by_sha256(
    tmp_path: Path,
) -> None:
    pdf = b"%PDF-1.4\nbody\n"
    sha = hashlib.sha256(pdf).hexdigest()
    staged = {
        "type": "application/pdf",
        "data": _b64(pdf),
        "name": "r.pdf",
        "_was_staged": True,
    }
    envelope, writes = build_transcript_attachment_envelope(
        text="summarise",
        attachments=[staged],
        session_id="s1",
        media_root=tmp_path,
        persist_enabled=True,
    )
    parsed = json.loads(envelope)
    persisted = parsed["attachments"][0]
    assert persisted["sha256_ref"] == sha
    assert persisted["name"] == "r.pdf"
    assert persisted["mime"] == "application/pdf"
    assert persisted["size"] == len(pdf)
    # NOT file_uuid; upload-store identifiers stay out of persisted transcript envelopes.
    assert "file_uuid" not in persisted
    assert "data" not in persisted

    # Disk write happened.
    expected_path = tmp_path / "transcripts" / "s1" / sha
    assert expected_path.exists()
    assert expected_path.read_bytes() == pdf
    assert any(Path(w["path"]) == expected_path for w in writes)


def test_transcript_envelope_uses_sha256_ref_not_file_uuid(tmp_path: Path) -> None:
    """Envelope field name is sha256_ref, never file_uuid."""

    pdf = b"%PDF-1.4\n"
    staged = {
        "type": "application/pdf",
        "data": _b64(pdf),
        "name": "r.pdf",
        "_was_staged": True,
    }
    envelope, _ = build_transcript_attachment_envelope(
        text="x",
        attachments=[staged],
        session_id="s",
        media_root=tmp_path,
        persist_enabled=True,
    )
    assert "file_uuid" not in envelope
    assert "sha256_ref" in envelope


# ---------------------------------------------------------------------------
# Test 3 — persist disabled keeps staged material out of the envelope.
# ---------------------------------------------------------------------------

def test_persist_transcripts_disabled_skips_disk_copy(tmp_path: Path) -> None:
    pdf = b"%PDF-1.4\n"
    staged = {
        "type": "application/pdf",
        "data": _b64(pdf),
        "name": "r.pdf",
        "_was_staged": True,
    }
    envelope, writes = build_transcript_attachment_envelope(
        text="x",
        attachments=[staged],
        session_id="s1",
        media_root=tmp_path,
        persist_enabled=False,
    )
    parsed = json.loads(envelope)
    persisted = parsed["attachments"][0]
    assert persisted == {
        "name": "r.pdf",
        "mime": "application/pdf",
        "size": len(pdf),
        "missing_reason": "attachment persistence disabled",
    }
    assert "data" not in persisted
    assert "sha256_ref" not in persisted
    assert writes == []


# ---------------------------------------------------------------------------
# Test 4 — dedupe by sha (free side effect of sha-keyed paths).
# ---------------------------------------------------------------------------

def test_transcript_dedup_within_session(tmp_path: Path) -> None:
    pdf = b"%PDF-1.4\nidentical\n"
    staged = {
        "type": "application/pdf",
        "data": _b64(pdf),
        "name": "r.pdf",
        "_was_staged": True,
    }
    build_transcript_attachment_envelope(
        text="first", attachments=[staged], session_id="s1",
        media_root=tmp_path, persist_enabled=True,
    )
    build_transcript_attachment_envelope(
        text="second", attachments=[staged], session_id="s1",
        media_root=tmp_path, persist_enabled=True,
    )
    sha = hashlib.sha256(pdf).hexdigest()
    files = list((tmp_path / "transcripts" / "s1").iterdir())
    assert [f.name for f in files] == [sha], files


# ---------------------------------------------------------------------------
# Test 5 — replay rebuilds from sha256_ref by reading the on-disk copy.
# ---------------------------------------------------------------------------

def test_replay_preserves_sha256_ref_without_reinlining_bytes(tmp_path: Path) -> None:
    """Historical replay keeps ref metadata and never recreates base64 data."""

    pdf = b"%PDF-1.4\nbody\n"
    sha = hashlib.sha256(pdf).hexdigest()
    persist_dir = tmp_path / "transcripts" / "s1"
    persist_dir.mkdir(parents=True, exist_ok=True)
    (persist_dir / sha).write_bytes(pdf)

    envelope = json.dumps(
        {
            "text": "replay me",
            "attachments": [
                {
                    "sha256_ref": sha,
                    "name": "r.pdf",
                    "mime": "application/pdf",
                    "size": len(pdf),
                }
            ],
        }
    )
    text, attachments = rebuild_attachments_for_replay(
        envelope, session_id="s1", media_root=tmp_path
    )
    assert "replay me" in text
    assert "[historical attachment omitted: r.pdf (application/pdf)]" in text
    assert attachments == []
    assert sha not in text


# ---------------------------------------------------------------------------
# Test 6 — replay degrades gracefully when the persisted file is missing.
# ---------------------------------------------------------------------------

def test_transcript_persistence_rejects_budget_exceeded_staged_attachment(
    tmp_path: Path,
) -> None:
    """Current staged uploads must not fall back to persistent inline base64."""
    pdf = b"%PDF-1.4\n" + b"a" * 50_000  # 50 KB
    staged = {
        "type": "application/pdf",
        "data": _b64(pdf),
        "name": "r.pdf",
        "_was_staged": True,
    }
    with pytest.raises(ValueError, match="disk budget"):
        build_transcript_attachment_envelope(
            text="x",
            attachments=[staged],
            session_id="s1",
            media_root=tmp_path,
            persist_enabled=True,
            disk_budget_bytes=10_000,  # cap below the payload size
        )
    transcripts_dir = tmp_path / "transcripts" / "s1"
    assert not transcripts_dir.exists() or not list(transcripts_dir.iterdir())


def test_replay_with_missing_persisted_file_degrades_gracefully(
    tmp_path: Path,
) -> None:
    sha = "a" * 64
    envelope = json.dumps(
        {
            "text": "replay me",
            "attachments": [
                {
                    "sha256_ref": sha,
                    "name": "missing.pdf",
                    "mime": "application/pdf",
                    "size": 12345,
                }
            ],
        }
    )
    text, attachments = rebuild_attachments_for_replay(
        envelope, session_id="s1", media_root=tmp_path
    )
    # No crash; attachments dropped; text carries a placeholder marker for
    # the missing one so the model is informed.
    assert "[attachment unavailable: missing.pdf]" in text
    assert attachments == []


def test_replay_missing_marker_sanitizes_attachment_name(tmp_path: Path) -> None:
    sha = "b" * 64
    envelope = json.dumps(
        {
            "text": "replay me",
            "attachments": [
                {
                    "sha256_ref": sha,
                    "name": "bad\nname.pdf",
                    "mime": "application/pdf",
                    "size": 12345,
                }
            ],
        }
    )

    text, attachments = rebuild_attachments_for_replay(
        envelope, session_id="s1", media_root=tmp_path
    )

    assert "[attachment unavailable: bad name.pdf]" in text
    assert attachments == []
