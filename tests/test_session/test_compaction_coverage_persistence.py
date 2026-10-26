from __future__ import annotations

from copy import deepcopy

import pytest
import pytest_asyncio

from opensquilla.session.compaction import CompactionRequest, compact_context
from opensquilla.session.compaction_state import build_coverage_obligations
from opensquilla.session.manager import SessionManager, _frozen_compaction_prefix_hash
from opensquilla.session.models import SessionIntent
from opensquilla.session.storage import SessionStorage
from tests.helpers.compaction import synthetic_compaction_config


@pytest_asyncio.fixture
async def manager():
    storage = SessionStorage(":memory:")
    await storage.connect()
    yield SessionManager(storage, inject_time_prefix=False)
    await storage.close()


async def seed(manager):
    node = await manager.create("agent:main:coverage-provenance")
    for role, content in (
        ("user", "Inspect apps/backend."),
        ("assistant", "Acknowledged."),
        ("user", "Continue."),
        ("assistant", "Continuing."),
    ):
        if content in {"Inspect apps/backend.", "Acknowledged."}:
            content += " Historical context detail." * 100
        await manager.append_message(node.session_key, role, content, token_count=5)
    return node


async def manual(manager, node, compaction_id="cmp_manual"):
    return await manager.compact_with_result(
        node.session_key,
        context_window_tokens=32_768,
        config=synthetic_compaction_config(
            summary="The probe rounds were environment/refresh-recovery checks only.",
            protected_recent_messages=2,
        ),
        compaction_id=compaction_id,
        trigger_reason="manual",
    )


@pytest.mark.asyncio
async def test_manual_then_automatic_snapshot_preserves_missing_refs_and_atomic_binding(manager):
    node = await seed(manager)
    first = await manual(manager, node)
    assert first.removed_count == 2
    assert first.coverage_status == "fail_reported"
    first_payload = deepcopy(first.summary_payload)
    first_manifest = first_payload["source_coverage"]["exact_reference_manifest"]
    assert first_manifest["session_id"] == node.session_id
    assert first_manifest["compaction_id"] == "cmp_manual"
    assert first_manifest["entries"][0]["value"] == "apps/backend"
    summaries = await manager.get_summaries(node.session_key)
    states = await manager.get_context_states(node.session_key)
    assert summaries[0].summary_payload["source_coverage"] == states[0].payload["source_coverage"]
    assert first_manifest["covered_through_id"] == summaries[0].covered_through_id

    await manager.append_message(node.session_key, "user", "Another exchange.")
    await manager.append_message(node.session_key, "assistant", "Acknowledged.")
    source = await manager.capture_compaction_source(node.session_key)
    assert source.coverage_session_id == node.session_id
    assert source.previous_coverage_checkpoints[0].manifest == first_manifest
    projected = [entry.model_dump(mode="json") for entry in source.entries]
    projected[0]["content"] = "Runtime helper uses runtime/generated-reference. " * 100
    result = await compact_context(
        CompactionRequest(
            session_id="agent-turn",
            coverage_session_id=source.coverage_session_id,
            entries=projected,
            canonical_coverage_entries=source.entries,
            context_window_tokens=32_768,
            config=synthetic_compaction_config(
                summary="Work continues.", protected_recent_messages=2
            ),
            previous_summary=source.previous_summary,
            previous_coverage_checkpoints=source.previous_coverage_checkpoints,
            forced_prefix_cut=2,
            trigger="message_count",
        )
    )
    assert result.coverage_status == "fail_reported"
    assert (
        result.summary_payload["source_coverage"]["exact_reference_manifest"]["entries"]
        == (first_manifest["entries"])
    )
    installed = await manager.persist_compaction_result(
        node.session_key,
        result.summary,
        result.kept_entries,
        summary_payload=result.summary_payload,
        summary_format=result.summary_format,
        coverage_status=result.coverage_status,
        missing_obligations=result.missing_obligations,
        removed_count=result.removed_count,
        source_snapshot=source,
        compaction_id="cmp_auto",
    )
    assert installed.applied
    latest = (await manager.get_summaries(node.session_key))[-1]
    manifest = latest.summary_payload["source_coverage"]["exact_reference_manifest"]
    assert manifest["compaction_id"] == "cmp_auto"
    assert manifest["covered_through_id"] == latest.covered_through_id
    assert latest.coverage_status == "fail_reported"
    assert (await manager.get_summaries(node.session_key))[0].summary_payload == first_payload


@pytest.mark.asyncio
async def test_failed_atomic_rewrite_does_not_install_or_mutate_manifest(manager, monkeypatch):
    node = await seed(manager)
    await manual(manager, node)
    before_summaries = await manager.get_summaries(node.session_key)
    before_states = await manager.get_context_states(node.session_key)
    await manager.append_message(
        node.session_key,
        "user",
        "Inspect apps/new-path." + " Historical detail." * 100,
    )
    await manager.append_message(node.session_key, "assistant", "Acknowledged.")
    before_rows = await manager.get_transcript(node.session_key)
    original_archive = manager._storage._archive_transcript_entries

    async def fail_after_archive(**kwargs):
        await original_archive(**kwargs)
        raise RuntimeError("synthetic atomic failure")

    monkeypatch.setattr(manager._storage, "_archive_transcript_entries", fail_after_archive)
    with pytest.raises(RuntimeError, match="synthetic atomic failure"):
        await manual(manager, node, "cmp_failed")
    assert await manager.get_summaries(node.session_key) == before_summaries
    assert await manager.get_context_states(node.session_key) == before_states
    assert await manager.get_transcript(node.session_key) == before_rows


@pytest.mark.asyncio
async def test_explicit_full_fork_rebinds_owner_and_reset_drops_old_checkpoint_authority(manager):
    parent = await seed(manager)
    await manual(manager, parent)
    child = await manager.branch(
        parent.session_key, "agent:main:coverage-child", fork_transcript=True
    )
    source = await manager.capture_compaction_source(child.session_key)
    manifest = source.previous_coverage_checkpoints[0].manifest
    assert manifest["session_id"] == child.session_id
    assert manifest["entries"][0]["source_session_id"] == parent.session_id
    inherited = build_coverage_obligations(
        [],
        session_id=child.session_id,
        previous_checkpoints=source.previous_coverage_checkpoints,
    )
    assert inherited.manifest["complete"] is True
    reset, _ = await manager.apply_intent(child.session_key, SessionIntent.RESET_SAME_KEY)
    after = await manager.capture_compaction_source(child.session_key)
    assert reset.session_id != child.session_id
    assert after.previous_coverage_checkpoints == ()


def test_singleflight_changes_when_only_host_coverage_context_changes():
    kwargs = dict(
        preimage=(),
        previous_summary="same checkpoint prose",
        context_window_tokens=32_768,
        context_window_chars=None,
        custom_instructions=None,
        config=synthetic_compaction_config(),
    )
    assert _frozen_compaction_prefix_hash(**kwargs, context_fingerprint="ledger-a") != (
        _frozen_compaction_prefix_hash(**kwargs, context_fingerprint="ledger-b")
    )
