"""Tests for the portable attachment occurrence manifest."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest

from opensquilla.session.attachment_manifest import (
    ATTACHMENT_MANIFEST_STATE_KIND,
    MATERIAL_AVAILABLE,
    MATERIAL_INVALID,
    MATERIAL_MISSING,
    AttachmentManifest,
    AttachmentManifestError,
    AttachmentManifestStore,
    attachment_manifest_from_context_state,
    build_attachment_manifest,
    extract_attachment_occurrences,
    extract_attachment_occurrences_from_envelope,
    legacy_attachment_id,
    manifest_context_state,
    merge_attachment_occurrences,
    normalize_attachment_name,
)
from opensquilla.session.models import SessionNode, TranscriptEntry
from opensquilla.session.storage import SessionStorage


def _b64(payload: bytes) -> str:
    return base64.b64encode(payload).decode("ascii")


def _entry(
    message_id: str,
    content: object,
    *,
    entry_id: int | None = None,
    created_at: int = 100,
    session_id: str = "session-a",
) -> TranscriptEntry:
    encoded = content if isinstance(content, str) else json.dumps(content)
    return TranscriptEntry(
        id=entry_id,
        session_id=session_id,
        session_key="agent:main:webchat:manifest",
        message_id=message_id,
        role="user",
        content=encoded,
        created_at=created_at,
    )


def test_legacy_attachment_id_matches_stable_algorithm() -> None:
    first = legacy_attachment_id(
        session_id="session-a",
        message_id="message-1",
        index=0,
        sha256="a" * 64,
    )
    forked = legacy_attachment_id(
        session_id="session-b",
        message_id="message-1",
        index=0,
        sha256="a" * 64,
    )
    different_index = legacy_attachment_id(
        session_id="session-a",
        message_id="message-1",
        index=1,
        sha256="a" * 64,
    )
    different_message = legacy_attachment_id(
        session_id="session-a",
        message_id="message-2",
        index=0,
        sha256="a" * 64,
    )
    different_hash = legacy_attachment_id(
        session_id="session-a",
        message_id="message-1",
        index=0,
        sha256="b" * 64,
    )
    digest = hashlib.sha256(
        ("message-1\0" + "0" + "\0" + "a" * 64).encode("utf-8")
    ).digest()[:18]
    expected = "att_legacy_" + base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    assert first == expected
    assert first == forked
    assert len({first, different_index, different_message, different_hash}) == 4
    assert first.startswith("att_legacy_")


@pytest.mark.parametrize(
    ("raw_name", "expected"),
    [
        ("/private/tmp/uploads/diagram.png", "diagram.png"),
        (r"C:\Temp\uploads\diagram.png", "diagram.png"),
        ("/private/tmp/uploads/", "attachment"),
    ],
)
def test_normalize_attachment_name_keeps_only_basename(
    raw_name: str,
    expected: str,
) -> None:
    assert normalize_attachment_name(raw_name) == expected


def test_extracts_inline_ref_and_missing_occurrences_without_bytes_in_payload() -> None:
    payload = b"image-bytes"
    sha = hashlib.sha256(payload).hexdigest()
    entries = [
        _entry(
            "message-1",
            {
                "text": "look",
                "attachments": [
                    {
                        "type": "image/png; charset=binary",
                        "name": "  diagram\n.png ",
                        "data": _b64(payload),
                    },
                    {
                        "attachment_id": "att_explicit_123456",
                        "mime": "image/jpeg",
                        "name": "stored.jpg",
                        "sha256_ref": sha,
                        "size": len(payload),
                    },
                    {
                        "name": "gone.png",
                        "mime": "image/png",
                        "missing_reason": "material was pruned",
                    },
                ],
            },
            entry_id=7,
        )
    ]

    occurrences = extract_attachment_occurrences(entries, session_id="session-a")

    assert len(occurrences) == 3
    inline, stored, missing = occurrences
    assert inline.material_state == MATERIAL_AVAILABLE
    assert inline.sha256_ref == sha
    assert inline.name == "diagram .png"
    assert inline.mime == "image/png"
    assert inline.size == len(payload)
    assert stored.attachment_id == "att_explicit_123456"
    assert stored.material_state == MATERIAL_AVAILABLE
    assert missing.material_state == MATERIAL_MISSING
    assert missing.missing_reason == "material was pruned"
    assert all("data" not in occurrence.to_payload() for occurrence in occurrences)


def test_invalid_material_is_retained_and_legacy_id_is_deterministic() -> None:
    envelope = {
        "attachments": [
            {
                "name": "broken.png",
                "type": "image/png",
                "data": "not-base64",
                "size": 10,
            }
        ]
    }
    first = extract_attachment_occurrences_from_envelope(
        envelope,
        session_id="session-a",
        source_message_id="message-1",
        source_entry_id=3,
    )[0]
    second = extract_attachment_occurrences_from_envelope(
        envelope,
        session_id="session-a",
        source_message_id="message-1",
        source_entry_id=3,
    )[0]
    assert first.material_state == MATERIAL_INVALID
    assert first.attachment_id == second.attachment_id
    assert first.sha256_ref is None
    assert first.missing_reason == "invalid inline attachment data"


def test_size_or_hash_mismatch_is_invalid() -> None:
    payload = b"payload"
    wrong_sha = "0" * 64
    occurrences = extract_attachment_occurrences_from_envelope(
        {
            "attachments": [
                {
                    "type": "image/png",
                    "data": _b64(payload),
                    "sha256_ref": wrong_sha,
                    "size": len(payload) + 1,
                }
            ]
        },
        session_id="s",
        source_message_id="m",
    )
    assert occurrences[0].material_state == MATERIAL_INVALID
    assert occurrences[0].missing_reason == "attachment size mismatch"


def test_non_user_rows_are_not_indexed_by_default() -> None:
    content = {"attachments": [{"type": "image/png", "data": _b64(b"x")}]}
    entries = [
        _entry("assistant-message", content),
        _entry("user-message", content),
    ]
    entries[0].role = "assistant"
    assert len(extract_attachment_occurrences(entries, session_id="s")) == 1
    assert len(
        extract_attachment_occurrences(
            entries,
            session_id="s",
            include_roles=("assistant",),
        )
    ) == 1


def test_manifest_merge_deduplicates_forked_physical_entry_ids() -> None:
    content = {"attachments": [{"type": "image/png", "data": _b64(b"x")}]}
    original = extract_attachment_occurrences(
        [_entry("message-1", content, entry_id=10)], session_id="session-a"
    )
    forked = extract_attachment_occurrences(
        [_entry("message-1", content, entry_id=900)], session_id="session-a"
    )
    merged = merge_attachment_occurrences(original, forked)
    assert len(merged) == 1
    assert merged[0].source_entry_id == 10


def test_manifest_rejects_same_id_for_different_logical_occurrence() -> None:
    first = extract_attachment_occurrences_from_envelope(
        {
            "attachments": [
                {
                    "attachment_id": "att_explicit_123456",
                    "type": "image/png",
                    "data": _b64(b"a"),
                }
            ]
        },
        session_id="s",
        source_message_id="m1",
    )
    second = extract_attachment_occurrences_from_envelope(
        {
            "attachments": [
                {
                    "attachment_id": "att_explicit_123456",
                    "type": "image/png",
                    "data": _b64(b"b"),
                }
            ]
        },
        session_id="s",
        source_message_id="m2",
    )
    with pytest.raises(AttachmentManifestError, match="collision"):
        merge_attachment_occurrences(first, second)


def test_manifest_payload_roundtrip_is_metadata_only() -> None:
    entries = [
        _entry(
            "message-1",
            {"attachments": [{"type": "image/png", "data": _b64(b"x")}]},
            entry_id=12,
        )
    ]
    manifest = build_attachment_manifest(
        entries,
        session_id="session-a",
        session_key="webchat:default",
    )
    payload = manifest.to_payload()
    decoded = AttachmentManifest.from_payload(
        payload,
        session_id="session-a",
        session_key="webchat:default",
    )
    assert decoded == manifest
    serialized = json.dumps(payload)
    assert "data" not in serialized
    assert "path" not in serialized
    assert decoded.session_key == "agent:main:webchat:default"


@pytest.mark.parametrize(
    "invalid_id",
    ["", "att_short", "../../private/image.png", "att_invalid/slash_123"],
)
def test_manifest_payload_rejects_invalid_attachment_id(invalid_id: str) -> None:
    payload = {
        "schema_version": 1,
        "covered_through_id": 1,
        "occurrences": [
            {
                "attachment_id": invalid_id,
                "source_message_id": "message-1",
                "ordinal": 0,
                "material_state": MATERIAL_MISSING,
            }
        ],
    }

    with pytest.raises(AttachmentManifestError, match="occurrence ID is invalid"):
        AttachmentManifest.from_payload(
            payload,
            session_id="session-a",
            session_key="webchat:default",
        )


@pytest.mark.asyncio
async def test_manifest_store_persists_and_reloads_across_storage_restart(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "manifest.db"
    key = "agent:main:webchat:manifest-store"
    session_id = "manifest-session"
    storage = SessionStorage(str(db_path))
    await storage.connect()
    try:
        await storage.upsert_session(
            SessionNode(
                session_key=key,
                session_id=session_id,
                agent_id="main",
                created_at=1,
                updated_at=1,
            )
        )
        entry = _entry(
            "message-1",
            {"attachments": [{"type": "image/png", "data": _b64(b"x")}]},
            entry_id=8,
            session_id=session_id,
        )
        await storage.append_transcript_entry(entry)
        store = AttachmentManifestStore(storage)
        manifest = await store.rebuild(
            session_id=session_id,
            session_key=key,
            entries=await storage.get_canonical_transcript(session_id),
        )
        found = await store.lookup(
            key,
            manifest.occurrences[0].attachment_id,
            session_id=session_id,
        )
        assert found == manifest.occurrences[0]
        states = await storage.get_context_states(
            key,
            provider="portable",
            state_kind=ATTACHMENT_MANIFEST_STATE_KIND,
        )
        assert len(states) == 1
    finally:
        await storage.close()

    restarted = SessionStorage(str(db_path))
    await restarted.connect()
    try:
        store = AttachmentManifestStore(restarted)
        loaded = await store.load(key, session_id=session_id)
        assert loaded.occurrences == manifest.occurrences
        assert await store.lookup(key, "att_missing_123456", session_id=session_id) is None
    finally:
        await restarted.close()


def test_manifest_context_state_factory_and_decoder() -> None:
    manifest = AttachmentManifest(
        session_id="s",
        session_key="agent:main:webchat:m",
        occurrences=(),
        covered_through_id=22,
    )
    state = manifest_context_state(manifest, created_at=123)
    assert state.state_kind == ATTACHMENT_MANIFEST_STATE_KIND
    assert state.portable is True
    assert state.cacheable is True
    assert state.covered_through_id == 22
    assert attachment_manifest_from_context_state(state) == manifest
