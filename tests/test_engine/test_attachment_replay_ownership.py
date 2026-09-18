from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from opensquilla.engine.runtime import TurnRunner
from opensquilla.gateway.config import GatewayConfig
from opensquilla.session.attachment_manifest import build_attachment_manifest
from opensquilla.session.manager import SessionManager
from opensquilla.session.models import SessionIntent
from opensquilla.session.storage import SessionStorage, StaleEpochError
from opensquilla.tools.types import ToolContext


@pytest_asyncio.fixture
async def replay_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[TurnRunner, SessionManager, SessionManager, Any, Any, str]]:
    monkeypatch.setenv("OPENSQUILLA_SESSION_ARCHIVE_DIR", str(tmp_path / "archives"))
    database = str(tmp_path / "attachment-owners.db")
    storage = SessionStorage(database)
    other_storage = SessionStorage(database)
    await storage.connect()
    await other_storage.connect()
    try:
        manager = SessionManager(storage, inject_time_prefix=False)
        other_manager = SessionManager(other_storage, inject_time_prefix=False)
        node = await manager.create("agent:main:attachment-owner")
        entry = await manager.append_message(
            node.session_key,
            "user",
            json.dumps({
                "text": "Inspect this image.",
                "attachments": [{"type": "image/png", "name": "sample.png", "data": "aW1hZ2U="}],
            }),
        )
        manifest = build_attachment_manifest(
            [entry], session_id=node.session_id, session_key=node.session_key
        )
        runner = TurnRunner(
            provider_selector=MagicMock(),
            session_manager=manager,
            config=GatewayConfig(),
        )
        yield runner, manager, other_manager, node, entry, manifest.occurrences[0].attachment_id
    finally:
        await other_storage.close()
        await storage.close()


async def test_attachment_helpers_use_admitted_owner_without_resolving_key(
    replay_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, manager, _, node, entry, attachment_id = replay_session

    async def unexpected_resolution(*args: Any, **kwargs: Any) -> str:
        raise AssertionError("exact-owner replay must not resolve the key again")

    monkeypatch.setattr(runner, "_resolve_session_id_for_log", unexpected_resolution)
    owner = {"expected_session_id": node.session_id, "expected_session_epoch": node.epoch}
    assert await runner._validated_image_attachment_ids(
        node.session_key, [attachment_id], **owner
    ) == (attachment_id,)
    await runner._persist_attachment_manifest_best_effort(node.session_key, [entry], **owner)
    states = await manager.get_context_states(node.session_key, **owner)
    assert len(states) == 1
    assert states[0].session_id == node.session_id


async def test_attachment_workfile_context_restores_mapping_and_merges_origin(
    replay_session: Any,
) -> None:
    runner, manager, _, node, _, _ = replay_session
    source = f".opensquilla/attachments/{node.session_id}/original.txt"
    target = f".opensquilla/attachments/{node.session_id}/working/original.txt"
    record = {"path": target, "sha256": "a" * 64, "session_id": node.session_id}
    caller = ToolContext()
    context = await runner._with_artifact_context(caller, node.session_key)
    context.attachment_working_files[source] = record
    await manager.update(node.session_key, origin={"synthetic_other_field": "preserve"})
    assert context.persist_attachment_working_files is not None
    await context.persist_attachment_working_files()

    restored = await runner._with_artifact_context(caller, node.session_key)
    assert restored.attachment_working_files == {source: record}
    assert restored.attachment_working_files is not context.attachment_working_files
    assert caller.attachment_working_files == {}
    saved = await manager.get_session(node.session_key)
    assert saved.origin["synthetic_other_field"] == "preserve"


async def test_attachment_workfile_context_without_owner_drops_caller_bindings() -> None:
    manager = MagicMock()
    manager.get_session = AsyncMock(return_value=None)
    manager.update = AsyncMock()
    runner = TurnRunner(provider_selector=None, session_manager=manager)
    stale_files = {"old-source": {"path": "old-working-copy", "session_id": "retired"}}
    caller = ToolContext(
        attachment_working_files=stale_files,
        persist_attachment_working_files=AsyncMock(),
    )

    context = await runner._with_artifact_context(caller, "agent:main:legacy")

    assert context.session_epoch is None
    assert context.attachment_working_files == {}
    assert context.persist_attachment_working_files is None
    assert caller.attachment_working_files == stale_files
    manager.update.assert_not_awaited()


async def test_attachment_workfile_context_propagates_durable_lookup_failure(
    replay_session: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, manager, _, node, _, _ = replay_session
    get_session = manager.get_session
    identity_resolved = False

    async def fail_after_identity(key: str) -> Any:
        nonlocal identity_resolved
        if identity_resolved:
            raise RuntimeError("synthetic durable lookup failure")
        identity_resolved = True
        return await get_session(key)

    monkeypatch.setattr(manager, "get_session", fail_after_identity)
    with pytest.raises(RuntimeError, match="synthetic durable lookup failure"):
        await runner._with_artifact_context(ToolContext(), node.session_key)


@pytest.mark.parametrize("replacement", ["reset", "epoch"])
async def test_attachment_workfile_save_rejects_retired_session(
    replay_session: Any, replacement: str,
) -> None:
    runner, manager, other_manager, node, _, _ = replay_session
    context = await runner._with_artifact_context(ToolContext(), node.session_key)
    context.attachment_working_files["synthetic-source"] = {
        "path": "synthetic-working-copy", "sha256": "a" * 64,
        "session_id": node.session_id,
    }
    if replacement == "reset":
        await other_manager.apply_intent(node.session_key, SessionIntent.RESET_SAME_KEY)
    else:
        await other_manager.storage.increment_epoch(node.session_key)
    assert context.persist_attachment_working_files is not None
    with pytest.raises(StaleEpochError, match="workfile save"):
        await context.persist_attachment_working_files()
    saved = await manager.get_session(node.session_key)
    assert not (saved.origin or {}).get("attachment_working_files")


async def test_workspace_reference_replay_marks_deleted_live_file_unavailable(
    replay_session: Any, tmp_path: Path,
) -> None:
    from opensquilla.execution_workspaces import configured_execution_workspace
    from opensquilla.session.compaction import _summarize_if_envelope

    runner, manager, _, node, _, _ = replay_session
    workspace = tmp_path / "project"
    workspace.mkdir()
    source = workspace / "report.txt"
    source.write_text("Synthetic current file body.")
    binding = configured_execution_workspace(workspace)
    await manager.update(node.session_key, execution_workspace=binding)
    ref = {
        "workspaceId": binding["id"], "relativePath": "report.txt",
        "name": "report.txt", "mime": "text/plain",
    }
    envelope = json.dumps({"text": "Edit this report.", "workspace_files": [ref]})
    entry = await manager.append_message(node.session_key, "user", envelope)
    context = await runner._with_artifact_context(ToolContext(
        run_mode="full", workspace_dir=str(workspace), workspace_files=[ref],
    ), node.session_key)
    blocks, descriptors = await runner._workspace_file_input_blocks(context)
    assert descriptors == [{"type": "text/plain", "name": "report.txt"}]
    assert "live project file" in blocks[0].text
    assert "Synthetic current file body" not in blocks[0].text
    source.unlink()
    with pytest.raises(FileNotFoundError):
        await runner._workspace_file_input_blocks(context)
    replay = await runner._workspace_file_history_projection([entry], context, node.session_key)
    text = runner._maybe_unpack_attachments(replay[0].content)
    assert "live project file unavailable" in text
    assert binding["id"] in text
    assert entry.content == envelope
    compacted = _summarize_if_envelope(envelope)
    assert binding["id"] in compacted and "report.txt" in compacted
    assert "historical contents are not retained" in compacted


@pytest.mark.parametrize("operation", ["canonical", "validate", "persist"])
@pytest.mark.parametrize("replacement", ["reset", "epoch"])
async def test_attachment_helpers_reject_retired_owner(
    replay_session: Any, operation: str, replacement: str
) -> None:
    runner, manager, other_manager, node, entry, attachment_id = replay_session
    if replacement == "reset":
        await other_manager.apply_intent(node.session_key, SessionIntent.RESET_SAME_KEY)
    else:
        await other_manager.storage.increment_epoch(node.session_key)
    owner = {"expected_session_id": node.session_id, "expected_session_epoch": node.epoch}
    with pytest.raises(StaleEpochError):
        if operation == "canonical":
            await runner._canonical_transcript_for_attachment_replay(
                node.session_key, [entry], **owner
            )
        elif operation == "validate":
            await runner._validated_image_attachment_ids(
                node.session_key, [attachment_id], **owner
            )
        else:
            await runner._persist_attachment_manifest_best_effort(
                node.session_key, [entry], **owner
            )
    assert await manager.get_context_states(node.session_key, valid_only=False) == []


async def test_canonical_attachment_read_rejects_reset_after_storage_read(
    replay_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, manager, other_manager, node, entry, _ = replay_session
    original_read = manager.storage.get_canonical_transcript

    async def reset_after_read(*args: Any, **kwargs: Any) -> Any:
        result = await original_read(*args, **kwargs)
        await other_manager.apply_intent(node.session_key, SessionIntent.RESET_SAME_KEY)
        await other_manager.append_message(node.session_key, "user", "Replacement input.")
        return result

    monkeypatch.setattr(manager.storage, "get_canonical_transcript", reset_after_read)
    with pytest.raises(StaleEpochError, match="canonical transcript read"):
        await runner._canonical_transcript_for_attachment_replay(
            node.session_key,
            [entry],
            expected_session_id=node.session_id,
            expected_session_epoch=node.epoch,
        )
    assert [row.content for row in await manager.get_transcript(node.session_key)] == [
        "Replacement input."
    ]


@pytest.mark.parametrize("replacement", ["reset", "epoch"])
async def test_manifest_save_rechecks_owner_inside_write_transaction(
    replay_session: Any, monkeypatch: pytest.MonkeyPatch, replacement: str
) -> None:
    runner, manager, other_manager, node, entry, _ = replay_session
    original_transaction = manager.storage._write_transaction
    save_attempted = False

    @asynccontextmanager
    async def replace_before_transaction(
        operation: str, **kwargs: Any
    ) -> AsyncIterator[Any]:
        nonlocal save_attempted
        if operation == "save_context_state":
            save_attempted = True
            if replacement == "reset":
                await other_manager.apply_intent(node.session_key, SessionIntent.RESET_SAME_KEY)
            else:
                await other_manager.storage.increment_epoch(node.session_key)
        async with original_transaction(operation, **kwargs) as connection:
            yield connection

    monkeypatch.setattr(manager.storage, "_write_transaction", replace_before_transaction)
    with pytest.raises(StaleEpochError):
        await runner._persist_attachment_manifest_best_effort(
            node.session_key,
            [entry],
            expected_session_id=node.session_id,
            expected_session_epoch=node.epoch,
        )
    assert save_attempted is True
    assert await manager.get_context_states(node.session_key, valid_only=False) == []


@pytest.mark.parametrize("replacement", ["reset", "epoch"])
async def test_bound_attachment_owner_failure_stops_pipeline(
    replay_session: Any, monkeypatch: pytest.MonkeyPatch, replacement: str
) -> None:
    runner, _, other_manager, node, entry, _ = replay_session
    if replacement == "reset":
        await other_manager.apply_intent(node.session_key, SessionIntent.RESET_SAME_KEY)
    else:
        await other_manager.storage.increment_epoch(node.session_key)
    pipeline_entered = False

    async def unexpected_pipeline(*args: Any, **kwargs: Any) -> Any:
        nonlocal pipeline_entered
        pipeline_entered = True
        raise AssertionError("A retired bound prompt must not reach pipeline steps")

    monkeypatch.setattr("opensquilla.engine.pipeline.run_pipeline", unexpected_pipeline)
    with pytest.raises(StaleEpochError):
        await runner._run_pipeline(
            "Inspect the attachment again.",
            node.session_key,
            MagicMock(),
            None,
            [],
            "system",
            [],
            bound_user_message_id=entry.message_id,
            expected_session_id=node.session_id,
            expected_session_epoch=node.epoch,
        )
    assert pipeline_entered is False
