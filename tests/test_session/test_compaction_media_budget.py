from __future__ import annotations

import base64
import hashlib
import io
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from PIL import Image

from opensquilla.attachment_refs import write_transcript_material
from opensquilla.provider.replay_budget import project_message_replay_budget
from opensquilla.provider.request_proof import estimate_provider_media_tokens
from opensquilla.provider.types import ContentBlockImage, ContentBlockText, Message
from opensquilla.session.compaction import (
    estimate_entries_model_replay_chars,
    estimate_entry_model_replay_tokens,
    project_entry_content_for_provider,
)
from opensquilla.session.tokenizer import estimate_tokens


@pytest.fixture(scope="module")
def png_encodings() -> list[str]:
    encodings = []
    for compression in (0, 9):
        stream = io.BytesIO()
        with Image.new("1", (2048, 2048), 1) as image:
            image.save(stream, format="PNG", compress_level=compression)
        encodings.append(base64.b64encode(stream.getvalue()).decode("ascii"))
    return encodings


def _entry(kind: str, data: str, *, count: int = 1) -> dict[str, Any]:
    if kind == "upload":
        content = json.dumps({
            "text": "Inspect these pictures.",
            "attachments": [
                {"type": "image/png", "data": data, "attachment_id": f"att_synthetic_{i:04d}"}
                for i in range(count)
            ],
        })
        return {
            "role": "user", "content": content, "token_count": estimate_tokens(content),
            "session_id": "history-scope", "message_id": "message-1",
        }
    message = Message(role="user", content=[
        ContentBlockText(text="Loaded pictures."),
        *[ContentBlockImage(media_type="image/png", data=data) for _ in range(count)],
    ])
    return {
        "role": "assistant",
        "content": "The tool loaded the pictures.",
        "assistant_replay": {"version": 1, "messages": [message.model_dump(mode="json")]},
    }


@pytest.mark.parametrize("kind", ["upload", "tool_replay"])
def test_compaction_pressure_does_not_grow_with_png_compression_size(
    png_encodings: list[str], kind: str,
) -> None:
    measurements = []
    for data in png_encodings:
        entry = _entry(kind, data)
        before = deepcopy(entry)
        measurements.append((
            estimate_entry_model_replay_tokens(entry),
            estimate_entries_model_replay_chars([entry]),
        ))
        assert entry == before
    assert measurements[0] == measurements[1]
    assert 4096 <= measurements[0][0] < 4600
    assert measurements[0][1] < 20_000


@pytest.mark.parametrize("kind", ["upload", "tool_replay"])
def test_compaction_counts_each_retained_image_once(png_encodings: list[str], kind: str) -> None:
    data = png_encodings[1]
    one = _entry(kind, data)
    two = _entry(kind, data, count=2)
    reserve = estimate_provider_media_tokens("image", 0, encoded_data=data)
    token_increment = (
        estimate_entry_model_replay_tokens(two) - estimate_entry_model_replay_tokens(one)
    )
    char_increment = (
        estimate_entries_model_replay_chars([two]) - estimate_entries_model_replay_chars([one])
    )
    assert reserve <= token_increment < reserve + 200
    assert reserve * 4 <= char_increment < reserve * 4 + 500


@pytest.mark.parametrize(
    "mime",
    ["application/zip", "application/octet-stream", "application/x-unknown"],
)
def test_compaction_projects_opaque_attachment_without_base64(mime: str) -> None:
    data = base64.b64encode(b"opaque payload" * 10_000).decode("ascii")
    content = json.dumps(
        {
            "text": "Inspect this file.",
            "attachments": [{"type": mime, "name": "payload.bin", "data": data}],
        },
        separators=(",", ":"),
    )
    entry = {"role": "user", "content": content, "token_count": estimate_tokens(content)}
    before = deepcopy(entry)

    tokens = estimate_entry_model_replay_tokens(entry)
    chars = estimate_entries_model_replay_chars([entry])

    assert entry == before
    assert tokens < estimate_tokens(content)
    assert chars < len(content)


@pytest.mark.parametrize(
    "attachment",
    [
        {"type": "application/zip", "name": "broken.zip", "data": "invalid-base64" * 8000},
        {"type": "application/octet-stream", "name": "bad.bin", "sha256_ref": "bad-ref"},
        {
            "type": "application/zip", "name": "missing.zip",
            "data": "invalid-base64" * 8000, "missing_reason": "material was pruned",
        },
        {
            "type": "application/zip", "name": "retained.zip",
            "data": "eA==", "missing_reason": "material was pruned",
        },
    ],
)
def test_invalid_opaque_attachment_has_bounded_provider_estimate(
    attachment: dict[str, str],
) -> None:
    content = json.dumps({"text": "Inspect.", "attachments": [attachment]})
    raw_tokens = estimate_tokens(content)
    entry = {"role": "user", "content": content, "token_count": raw_tokens + 17}

    projected, complete = project_entry_content_for_provider(content)

    assert complete is True
    assert isinstance(projected, str)
    assert projected.startswith("Inspect.\n[historical attachment")
    if attachment.get("data"):
        assert attachment["data"] not in projected
    assert estimate_entry_model_replay_tokens(entry) == estimate_tokens(projected) + 17
    assert estimate_entries_model_replay_chars([entry]) < max(len(content), 2_000)


@pytest.mark.parametrize("attachment", [
    {"type": "application/x-unknown", "name": "empty.bin"},
    {"type": "image/png", "sha256": "a" * 64},
    {"type": "image/png", "material_id": "a" * 64},
])
def test_historical_attachment_without_runtime_material_is_skipped(
    attachment: dict[str, str],
) -> None:
    content = json.dumps({"text": "Inspect.", "attachments": [attachment]})
    assert project_entry_content_for_provider(content, preserve_images=True) == (
        "Inspect.", True,
    )


def test_unverified_opaque_ref_uses_bounded_capacity_marker() -> None:
    content = json.dumps({
        "text": "Inspect.",
        "attachments": [{
            "type": "application/zip", "name": "archive.zip", "sha256_ref": "a" * 64,
        }],
    })

    projected, complete = project_entry_content_for_provider(content)

    assert complete is True
    assert projected.startswith("Inspect.\n[historical attachment")
    assert "a" * 64 not in projected
    assert len(projected) < 2_000


def test_opaque_projection_bounds_display_metadata_replayed_to_provider() -> None:
    from opensquilla.engine.runtime import TurnRunner

    name = "/synthetic/source/path/" + "n" * 10_000
    mime = "application/x-" + "m" * 2_000
    content = json.dumps({
        "text": "Inspect.",
        "attachments": [{"type": mime, "name": name, "data": "eA=="}],
    })

    projected, complete = project_entry_content_for_provider(content)
    actual = TurnRunner._maybe_unpack_attachments(content)

    assert complete is True
    assert isinstance(projected, str)
    assert isinstance(actual, str)
    assert "/synthetic/source/path" not in actual
    assert name not in projected
    assert mime not in projected
    assert len(projected) < 2_000
    assert estimate_tokens(projected) >= estimate_tokens(actual)


@pytest.mark.parametrize(
    "material",
    [{"data": "invalid-base64"}, {"missing_reason": "synthetic material missing"}],
)
def test_opaque_history_fallback_marker_bounds_display_metadata(
    tmp_path: Path, material: dict[str, str],
) -> None:
    from opensquilla.engine.runtime import TurnRunner

    name = "/synthetic/source/path/" + "n" * 10_000 + ".zip"
    mime = "application/x-" + "m" * 2_000
    content = json.dumps({
        "text": "Inspect.",
        "attachments": [{"type": mime, "name": name, **material}],
    })
    projected, complete = project_entry_content_for_provider(content, session_id="scope")
    actual = TurnRunner._maybe_unpack_attachments(
        content,
        materialize_historical_attachments=True,
        media_root=tmp_path / "media",
        workspace_dir=tmp_path / "workspace",
        session_id="scope",
    )

    assert complete is True
    assert isinstance(projected, str)
    assert isinstance(actual, str)
    assert "/synthetic/source/path" not in actual
    assert name not in projected
    assert mime not in actual
    assert len(actual) < 400
    assert estimate_tokens(projected) >= estimate_tokens(actual)
    assert len(json.dumps(projected)) >= len(json.dumps(actual))


@pytest.mark.parametrize("storage", ["inline", "missing_ref"])
def test_opaque_budget_covers_materialized_history_without_measurement_writes(
    tmp_path: Path, storage: str,
) -> None:
    from opensquilla.engine.runtime import TurnRunner

    payload = b"synthetic archive data" * 512
    data = base64.b64encode(payload).decode("ascii")
    attachment: dict[str, Any] = {
        "type": "application/zip", "name": "archive.zip", "size": len(payload),
    }
    if storage == "inline":
        attachment["data"] = data
    else:
        attachment["sha256_ref"] = "a" * 64
    content = json.dumps({"text": "Inspect the archive.", "attachments": [attachment]})
    entry = {"role": "user", "content": content, "token_count": estimate_tokens(content)}
    original = deepcopy(entry)
    workspace = tmp_path / "workspace"

    projected, complete = project_entry_content_for_provider(content)
    projected_tokens = estimate_entry_model_replay_tokens(entry)
    projected_chars = estimate_entries_model_replay_chars([entry])

    assert complete is True
    assert isinstance(projected, str)
    assert data not in projected
    assert entry == original
    assert not workspace.exists()

    actual = TurnRunner._maybe_unpack_attachments(
        content,
        materialize_historical_attachments=True,
        media_root=tmp_path / "media",
        workspace_dir=workspace,
        session_id="history-scope",
    )
    assert isinstance(actual, str)
    assert data not in actual
    actual_payload = json.dumps(
        [{"role": "user", "content": actual}], ensure_ascii=False, sort_keys=True,
    )
    assert projected_tokens >= estimate_tokens(actual)
    assert projected_chars >= len(actual_payload)
    if storage == "inline":
        assert "historical attachment available" in actual
        assert list((workspace / ".opensquilla" / "attachments").rglob("*.zip"))
    else:
        assert "historical attachment unavailable" in actual


def test_opaque_budget_covers_bounded_working_path_marker(tmp_path: Path) -> None:
    from opensquilla.attachment_workspace import AttachmentWorkspaceMaterializer
    from opensquilla.engine.runtime import TurnRunner

    payload = b"synthetic archive data" * 512
    data = base64.b64encode(payload).decode("ascii")
    content = json.dumps({
        "text": "Inspect the archive.",
        "attachments": [{
            "type": "application/zip", "name": "archive.zip", "size": len(payload),
            "data": data,
        }],
    })
    entry = {"role": "user", "content": content, "token_count": estimate_tokens(content)}
    workspace = tmp_path / "workspace"
    sha = hashlib.sha256(payload).hexdigest()
    rel_path = f".opensquilla/attachments/history-scope/{sha[:12]}-archive.zip"
    long_working_path = "src/" + "x" * 2_000 + ".zip"
    materializer = AttachmentWorkspaceMaterializer(
        media_root=tmp_path / "media",
        workspace_dir=workspace,
        working_files={rel_path: {"path": long_working_path}},
    )

    projected, complete = project_entry_content_for_provider(content)
    projected_tokens = estimate_entry_model_replay_tokens(entry)
    projected_chars = estimate_entries_model_replay_chars([entry])
    assert complete is True
    assert isinstance(projected, str)
    assert not workspace.exists()

    actual = TurnRunner._maybe_unpack_attachments(
        content,
        materialize_historical_attachments=True,
        historical_materializer=materializer,
        media_root=tmp_path / "media",
        workspace_dir=workspace,
        session_id="history-scope",
    )
    assert isinstance(actual, str)
    assert long_working_path not in actual
    assert data not in actual
    actual_payload = json.dumps(
        [{"role": "user", "content": actual}], ensure_ascii=False, sort_keys=True,
    )
    assert projected_tokens >= estimate_tokens(actual)
    assert projected_chars >= len(actual_payload)


def test_opaque_budget_covers_legacy_conflicting_mime_fields(tmp_path: Path) -> None:
    from opensquilla.engine.runtime import TurnRunner

    displayed_mime = "application/x-" + "m" * 2_000
    content = json.dumps({
        "text": "Inspect.",
        "attachments": [{
            "type": displayed_mime,
            "mime": "application/zip",
            "name": "legacy.zip",
            "data": base64.b64encode(b"synthetic archive").decode("ascii"),
        }],
    })
    projected, complete = project_entry_content_for_provider(content, session_id="legacy")
    actual = TurnRunner._maybe_unpack_attachments(
        content,
        materialize_historical_attachments=True,
        media_root=tmp_path / "media",
        workspace_dir=tmp_path / "workspace",
        session_id="legacy",
    )

    assert complete is True
    assert isinstance(projected, str)
    assert isinstance(actual, str)
    assert "historical attachment available" in actual
    assert estimate_tokens(projected) >= estimate_tokens(actual)
    assert len(projected) >= len(actual)


def test_opaque_budget_covers_json_escaped_display_metadata() -> None:
    from opensquilla.engine.runtime import TurnRunner

    content = json.dumps({
        "text": "Inspect.",
        "attachments": [{
            "type": "application/zip",
            "name": "\n" * 500 + '.zip"',
            "data": "invalid!",
        }],
    })
    entry = {"role": "user", "content": content}
    projected, complete = project_entry_content_for_provider(content, session_id="scope")
    actual = TurnRunner._maybe_unpack_attachments(content, session_id="scope")

    assert complete is True
    assert isinstance(projected, str)
    assert isinstance(actual, str)
    actual_payload = json.dumps(
        [{"role": "user", "content": actual}], ensure_ascii=False, sort_keys=True,
    )
    projected_payload = json.dumps(
        [{"role": "user", "content": projected}], ensure_ascii=False, sort_keys=True,
    )
    assert len(projected_payload) >= len(actual_payload)
    assert estimate_entries_model_replay_chars([entry]) >= len(actual_payload)


@pytest.mark.parametrize("sha256_ref", ["a" * 64, "invalid-reference"])
def test_unavailable_history_marker_does_not_expose_stored_source_path(
    tmp_path: Path, sha256_ref: str,
) -> None:
    from opensquilla.engine.runtime import TurnRunner

    raw_name = "/synthetic/source/path/archive.zip"
    content = json.dumps({
        "text": "Inspect.",
        "attachments": [{
            "type": "application/zip", "name": raw_name, "sha256_ref": sha256_ref,
        }],
    })
    projected, complete = project_entry_content_for_provider(content, session_id="scope")
    actual = TurnRunner._maybe_unpack_attachments(
        content,
        materialize_historical_attachments=True,
        media_root=tmp_path / "media",
        workspace_dir=tmp_path / "workspace",
        session_id="scope",
    )

    assert complete is True
    assert isinstance(projected, str)
    assert isinstance(actual, str)
    assert "archive.zip" in actual
    assert "/synthetic/source/path" not in actual
    assert estimate_tokens(projected) >= estimate_tokens(actual)
    assert len(json.dumps(projected)) >= len(json.dumps(actual))


def test_invalid_image_still_needs_media_proof() -> None:
    content = json.dumps({
        "text": "Inspect.", "attachments": [{"type": "image/png", "data": "invalid-base64"}],
    })

    projected, complete = project_entry_content_for_provider(content, preserve_images=True)

    assert projected == content
    assert complete is False
    omitted, omitted_complete = project_entry_content_for_provider(
        content, preserve_images=False,
    )
    assert omitted_complete is True
    assert isinstance(omitted, str)
    assert "invalid-base64" not in omitted
    assert "historical attachment omitted" in omitted


@pytest.mark.parametrize("material", [
    {"data": "!" * 10_000},
    {"data": base64.b64encode(b"synthetic non-image bytes" * 1_000).decode("ascii")},
    {"sha256_ref": "invalid-reference" * 1_000, "size": 123},
    {"sha256_ref": "a" * 64, "size": 123},
], ids=["invalid-base64", "invalid-pixels", "invalid-ref", "missing-ref"])
@pytest.mark.parametrize("persisted_surplus", [-17, 17])
def test_unproven_retained_image_estimates_keep_raw_floor(
    tmp_path: Path, material: dict[str, Any], persisted_surplus: int,
) -> None:
    from opensquilla.engine.history import project_history_replay_capacity
    from opensquilla.engine.runtime import TurnRunner
    from opensquilla.gateway.config import GatewayConfig

    content = json.dumps({
        "text": "Inspect.", "attachments": [{"type": "image/png", **material}],
    })
    raw_tokens = estimate_tokens(content)
    entry = {
        "role": "user", "content": content, "token_count": raw_tokens + persisted_surplus,
        "message_id": "synthetic-message",
    }
    media_root = tmp_path / "media"
    session_id = "history-scope"
    projected, complete = project_entry_content_for_provider(
        content, preserve_images=True, media_root=media_root, session_id=session_id,
        message_id=entry["message_id"],
    )
    assert (projected, complete) == (content, False)
    # Preflight and compaction cut/skip decisions use these source estimators.
    assert estimate_entry_model_replay_tokens(
        entry, preserve_images=True, media_root=media_root, session_id=session_id,
    ) == max(raw_tokens, entry["token_count"])
    raw_payload = json.dumps(
        [{"role": "user", "content": content}], ensure_ascii=False, sort_keys=True,
    )
    assert estimate_entries_model_replay_chars(
        [entry], preserve_images=True, media_root=media_root, session_id=session_id,
    ) == len(raw_payload)

    runner = TurnRunner(
        provider_selector=MagicMock(), session_manager=None, config=GatewayConfig(),
    )
    for preserve_images in (True, False):
        replay = runner._project_history_replay(
            [SimpleNamespace(**entry)], excluded_entry_indexes=(), trim_last_user=False,
            bound_slice_applied=False, image_replay_entry_indexes=(0,) if preserve_images else (),
            media_root=media_root, session_id=session_id, require_capacity_proof=True,
        )
        capacity = project_history_replay_capacity(replay)
        assert capacity.media_block_count == 0
        assert capacity.estimate_complete is not preserve_images
        if preserve_images:
            assert capacity.messages[0].content == content
            assert capacity.estimated_tokens >= max(raw_tokens, entry["token_count"])
        else:
            assert capacity.messages[0].content != content
            assert capacity.estimated_tokens < 1_000

    omitted, omitted_complete = project_entry_content_for_provider(
        content, preserve_images=False, media_root=media_root, session_id=session_id,
        message_id=entry["message_id"],
    )
    assert omitted_complete is True
    assert isinstance(omitted, str)
    assert estimate_entry_model_replay_tokens(
        entry, preserve_images=False, media_root=media_root, session_id=session_id,
    ) == estimate_tokens(omitted) + max(0, persisted_surplus)
    assert estimate_entries_model_replay_chars(
        [entry], preserve_images=False, media_root=media_root, session_id=session_id,
    ) < 2_000
    assert not media_root.exists()


@pytest.mark.parametrize("preserve_images", [False, True])
@pytest.mark.parametrize(
    "name", ["sample.png", 'folder/quoted"\n' + "x" * 512 + ".png", ('"\n' * 250) + ".png"],
)
def test_image_history_projection_covers_status_and_material_path_without_writes(
    tmp_path: Path, png_encodings: list[str], preserve_images: bool, name: str,
) -> None:
    from opensquilla.engine.runtime import TurnRunner
    from opensquilla.session.attachment_manifest import (
        extract_attachment_occurrences_from_envelope,
    )

    content = json.dumps({
        "text": "Inspect the image.",
        "attachments": [{"type": "image/png", "name": name, "data": png_encodings[1]}],
    })
    occurrence = extract_attachment_occurrences_from_envelope(
        content, session_id="history-scope", source_message_id="message-1",
    )[0]
    workspace = tmp_path / "workspace"

    projected, complete = project_entry_content_for_provider(
        content, preserve_images=preserve_images,
        session_id="history-scope", message_id="message-1",
    )
    assert complete is True
    assert not workspace.exists()

    actual = TurnRunner._maybe_unpack_attachments(
        content,
        preserve_image_attachments=preserve_images,
        allowed_image_attachment_ids=(
            frozenset({occurrence.attachment_id}) if preserve_images else None
        ),
        materialize_historical_attachments=True,
        media_root=tmp_path / "media",
        workspace_dir=workspace,
        session_id="history-scope",
        source_message_id="message-1",
    )
    if preserve_images:
        assert isinstance(projected, list)
        assert isinstance(actual, list)
        projected_text = "\n".join(
            block.text for block in projected if isinstance(block, ContentBlockText)
        )
        actual_text = "\n".join(
            block.text for block in actual if isinstance(block, ContentBlockText)
        )
        assert f"[historical image attachment_id={occurrence.attachment_id}]" in actual_text
    else:
        assert isinstance(projected, str)
        assert isinstance(actual, str)
        projected_text, actual_text = projected, actual
        assert "historical attachment omitted" in actual_text
        assert occurrence.attachment_id in projected_text
    assert "[attachment available:" in actual_text
    assert "[attachment available:" in projected_text
    assert estimate_tokens(projected_text) >= estimate_tokens(actual_text)
    assert len(json.dumps(projected_text)) >= len(json.dumps(actual_text))


def test_image_history_projection_reserves_unknown_session_scope(
    tmp_path: Path, png_encodings: list[str],
) -> None:
    from opensquilla.engine.runtime import TurnRunner

    content = json.dumps({
        "text": "Inspect the image.",
        "attachments": [{"type": "image/png", "data": png_encodings[1]}],
    })
    projected, complete = project_entry_content_for_provider(content)
    actual = TurnRunner._maybe_unpack_attachments(
        content,
        materialize_historical_attachments=True,
        media_root=tmp_path / "media",
        workspace_dir=tmp_path / "workspace",
        session_id="sT9_-" * 36,
    )

    assert complete is True
    assert isinstance(projected, str)
    assert isinstance(actual, str)
    assert estimate_tokens(projected) >= estimate_tokens(actual)
    assert len(json.dumps(projected)) >= len(json.dumps(actual))


def test_missing_image_history_projection_keeps_unavailable_status() -> None:
    from opensquilla.engine.runtime import TurnRunner

    content = json.dumps({
        "text": "Inspect the image.",
        "attachments": [{
            "type": "image/png", "name": "missing.png",
            "missing_reason": "synthetic material unavailable",
        }],
    })

    projected, complete = project_entry_content_for_provider(
        content, session_id="history-scope", message_id="message-1",
    )
    actual = TurnRunner._maybe_unpack_attachments(
        content, session_id="history-scope", source_message_id="message-1",
    )

    assert complete is True
    assert isinstance(projected, str)
    assert isinstance(actual, str)
    assert "历史图片不可用" in actual
    assert estimate_tokens(projected) >= estimate_tokens(actual)
    assert len(json.dumps(projected)) >= len(json.dumps(actual))


def test_malformed_attachment_items_skipped_by_runtime_have_bounded_estimate() -> None:
    data = "invalid-base64" * 8000
    content = json.dumps({
        "text": "Inspect.",
        "attachments": [data, {"name": "without-mime.bin", "data": data}],
    })
    raw_tokens = estimate_tokens(content)
    entry = {"role": "user", "content": content, "token_count": raw_tokens + 7}

    projected, complete = project_entry_content_for_provider(content)

    assert complete is True
    assert projected == "Inspect."
    assert data not in projected
    assert estimate_entry_model_replay_tokens(entry) == estimate_tokens(projected) + 7


@pytest.mark.parametrize("use_stored_markers", [False, True])
def test_attachment_projection_keeps_runtime_annotation_and_workspace_text(
    use_stored_markers: bool,
) -> None:
    from opensquilla.engine.runtime import TurnRunner

    envelope: dict[str, Any] = {
        "text": "Inspect this file.",
        "attachments": [{"type": "application/zip", "data": "eA=="}],
        "prompt_annotations": [{
            "version": 1, "annotationId": "ann_1", "order": 0, "body": "Old edit",
            "document": {"id": "doc_1", "name": "draft", "kind": "document"},
            "revision": {"id": "rev_1", "sha256": "a" * 64, "generation": 1},
            "anchor": {"id": "anchor_1", "kind": "region", "tagName": "p", "locator": {}},
        }],
        "workspace_files": [{
            "workspaceId": "workspace_1", "relativePath": "src/example.py",
            "name": "example.py", "mime": "text/x-python",
        }],
    }
    if use_stored_markers:
        envelope["_workspace_file_markers"] = ["[stored live file marker: src/example.py]"]
    content = json.dumps(envelope)
    actual = TurnRunner._maybe_unpack_attachments(content)
    projected, complete = project_entry_content_for_provider(content)

    assert complete is True
    assert isinstance(actual, str)
    assert isinstance(projected, str)
    assert projected.startswith(actual.split("[historical attachment omitted:")[0])
    assert "<historical_artifact_prompt_annotations count='1'>" in projected
    if use_stored_markers:
        assert "[stored live file marker: src/example.py]" in projected
    else:
        assert "[live project file reference:" in projected


def test_plain_transcript_estimates_preserve_text_and_persisted_token_floor() -> None:
    entry = {
        "role": "assistant", "content": "Synthetic response", "token_count": 100,
        "tool_calls": [{"name": "lookup", "input": {"key": "value"}}],
        "reasoning_content": "Synthetic reasoning",
    }
    extras = json.dumps(entry["tool_calls"], ensure_ascii=False, sort_keys=True, default=str)
    expected = 100 + estimate_tokens(extras + "\n" + entry["reasoning_content"])
    assert estimate_entry_model_replay_tokens(entry) == expected
    payload = {key: value for key, value in entry.items() if key != "token_count"}
    expected_chars = len(json.dumps([payload], ensure_ascii=False, sort_keys=True, default=str))
    assert estimate_entries_model_replay_chars([entry]) == expected_chars


def test_plain_assistant_replay_estimates_preserve_existing_projection() -> None:
    message = Message(role="assistant", content="Synthetic response").model_dump(mode="json")
    replay = {"version": 1, "messages": [message]}
    entry = {"role": "assistant", "assistant_replay": replay}
    projected = {"version": 1, "messages": [project_message_replay_budget(message)]}
    expected = estimate_tokens(json.dumps(
        projected, ensure_ascii=False, sort_keys=True, default=str,
    ))
    assert estimate_entry_model_replay_tokens(entry) == expected
    expected_chars = len(json.dumps(
        [{"role": "assistant", "assistant_replay": projected}],
        ensure_ascii=False, sort_keys=True, default=str,
    ))
    assert estimate_entries_model_replay_chars([entry]) == expected_chars


def test_image_shape_nested_in_tool_arguments_keeps_its_full_text_cost(
    png_encodings: list[str],
) -> None:
    data = png_encodings[0]
    message = {
        "role": "assistant", "content": [{
            "type": "tool_use", "id": "synthetic-call", "name": "record",
            "input": {"type": "image", "source_type": "base64", "media_type": "image/png",
                      "data": data},
        }],
    }
    entry = {"role": "assistant", "assistant_replay": {"version": 1, "messages": [message]}}
    assert estimate_entry_model_replay_tokens(entry) > 10_000
    assert estimate_entries_model_replay_chars([entry]) > len(data)


def test_image_reference_uses_fallback_until_request_bytes_are_available() -> None:
    entry = {"role": "user", "content": json.dumps({
        "text": "Inspect.", "attachments": [{"type": "image/png", "sha256_ref": "a" * 64}],
    })}
    assert 1024 <= estimate_entry_model_replay_tokens(entry) < 1500
    assert 4096 <= estimate_entries_model_replay_chars([entry]) < 6000


@pytest.mark.parametrize("empty_inline_data", [False, True])
def test_retained_image_reference_uses_verified_request_media(
    png_encodings: list[str], tmp_path: Path, empty_inline_data: bool,
) -> None:
    raw = base64.b64decode(png_encodings[1])
    media_root = tmp_path / "media"
    sha, _path, _written = write_transcript_material(
        media_root=media_root, session_id="history-scope", payload=raw,
    )
    attachment: dict[str, Any] = {
        "type": "image/png", "sha256_ref": sha, "size": len(raw),
    }
    if empty_inline_data:
        attachment["data"] = ""
    content = json.dumps({"text": "Inspect.", "attachments": [attachment]})
    entry = {"role": "user", "content": content, "session_id": "history-scope"}
    projected, complete = project_entry_content_for_provider(
        content, preserve_images=True, media_root=media_root,
        session_id="history-scope", require_media_proof=True,
    )
    assert complete is True
    assert isinstance(projected, list)
    assert any(isinstance(block, ContentBlockImage) and block.data == png_encodings[1]
               for block in projected)
    assert 4096 <= estimate_entry_model_replay_tokens(
        entry, media_root=media_root, session_id="history-scope",
    ) < 4600
    assert estimate_entries_model_replay_chars(
        [entry], media_root=media_root, session_id="history-scope",
    ) >= 4096 * 4
    # A text-only route excludes that media reserve without charging its
    # stored reference or a maximum-size image placeholder.
    assert estimate_entry_model_replay_tokens(entry, preserve_images=False) < 500
    assert estimate_entries_model_replay_chars([entry], preserve_images=False) < 1000


def test_inline_image_replay_uses_data_despite_stale_metadata(
    png_encodings: list[str],
) -> None:
    content = json.dumps({"text": "Inspect.", "attachments": [{
        "type": "image/png", "data": png_encodings[1],
        "sha256_ref": "0" * 64, "size": 1,
    }]})
    entry = {"role": "user", "content": content}
    projected, complete = project_entry_content_for_provider(
        content, preserve_images=True,
    )
    assert complete is True
    assert isinstance(projected, list)
    assert 4096 <= estimate_entry_model_replay_tokens(entry) < 4600


@pytest.mark.parametrize("damage", ["missing", "wrong-size", "wrong-hash", "bad-image"])
def test_retained_image_reference_requires_verifiable_material(
    png_encodings: list[str], tmp_path: Path, damage: str,
) -> None:
    raw = b"synthetic corrupt PNG bytes" if damage == "bad-image" else base64.b64decode(
        png_encodings[1]
    )
    media_root = tmp_path / "media"
    sha, path, _written = write_transcript_material(
        media_root=media_root, session_id="history-scope", payload=raw,
    )
    if damage == "missing":
        path.unlink()
    content = json.dumps({"text": "Inspect.", "attachments": [{
        "type": "image/png", "sha256_ref": "0" * 64 if damage == "wrong-hash" else sha,
        "size": 1 if damage == "wrong-size" else len(raw),
    }]})
    projected, complete = project_entry_content_for_provider(
        content, preserve_images=True, media_root=media_root,
        session_id="history-scope", require_media_proof=True,
    )
    assert complete is False
    assert projected == content
