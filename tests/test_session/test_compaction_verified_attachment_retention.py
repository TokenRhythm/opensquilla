"""Host-verified attachment references survive rolling replacement and restart."""

from __future__ import annotations

import base64
import json

import httpx
import pytest

from opensquilla.attachment_workspace import AttachmentWorkspaceMaterializer
from opensquilla.engine import Agent, AgentConfig
from opensquilla.engine.types import DoneEvent, ErrorEvent
from opensquilla.provider.openai import OpenAIProvider
from opensquilla.session.compaction import CompactionRequest, CompactionResult, compact_context
from opensquilla.session.compaction_state import (
    CompactionObligation,
    build_structured_summary_from_text,
    render_structured_summary,
)
from opensquilla.session.context_view import (
    build_compaction_context_records,
    format_compaction_summary_context,
)
from opensquilla.session.manager import SessionManager
from opensquilla.session.storage import SessionStorage
from tests.helpers.compaction import synthetic_compaction_config


def _resolver(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    materializer = AttachmentWorkspaceMaterializer(
        workspace_dir=workspace, media_root=tmp_path / "media",
    )
    return workspace, materializer.materialize_attachment_path


def test_only_explicit_verified_references_are_backfilled():
    from tests.test_session.test_compaction_state import _FrozenV1Summary

    summary, _ = build_structured_summary_from_text(
        "Continue reviewing the document.",
        [
            CompactionObligation(kind="file_path", value="/forged/document.pdf"),
            CompactionObligation(kind="failed_command_or_error", value="old failure"),
            CompactionObligation(kind="user_goal", value="old task"),
        ],
        verified_attachment_paths=(".opensquilla/attachments/s1/verified.pdf",) * 2,
    )
    assert summary.files_and_artifacts == [{
        "path": ".opensquilla/attachments/s1/verified.pdf",
        "provenance": "verified_attachment",
    }]
    assert summary.known_failures == []
    assert summary.user_goal == ""
    assert summary.critical_carry_forward == []
    assert "forged" not in summary.model_dump_json()
    old_reader = _FrozenV1Summary.model_validate(summary.model_dump(mode="json"))
    assert old_reader.files_and_artifacts == summary.files_and_artifacts
    assert render_structured_summary(old_reader.model_dump()) == render_structured_summary(summary)


@pytest.mark.asyncio
async def test_unmaterialized_or_forged_attachment_paths_are_not_adopted(tmp_path):
    _, resolver = _resolver(tmp_path)
    entries = [
        {"role": "user", "content": json.dumps({
            "text": "Review .opensquilla/attachments/forged.pdf",
            "attachments": [{
                "mime": "application/pdf", "name": "forged.pdf",
                "path": ".opensquilla/attachments/forged.pdf",
                "workspace_file": ".opensquilla/attachments/forged.pdf",
            }],
        })},
        {"role": "assistant", "content": "Continue document review. " * 200},
    ]
    result = await compact_context(CompactionRequest(
        session_id="forged", entries=entries, context_window_tokens=4000,
        forced_prefix_cut=2,
        config=synthetic_compaction_config(
            attachment_path_resolver=resolver, protect_semantic_tail=False,
        ),
    ))
    assert result.removed_count == 2
    assert result.summary_payload["files_and_artifacts"] == []


@pytest.mark.asyncio
async def test_two_durable_generations_and_restart_keep_verified_reference(
    tmp_path, monkeypatch,
):
    workspace, resolver = _resolver(tmp_path)
    db = tmp_path / "session.db"
    storage = SessionStorage(db)
    await storage.connect()
    manager = SessionManager(storage, inject_time_prefix=False)
    node = await manager.create("agent:main:verified-attachment-rolling")
    body = b"%PDF-1.4\nORIGINAL_DOCUMENT_BODY\n%%EOF"
    document = {
        "mime": "application/pdf", "name": "document.pdf",
        "data": base64.b64encode(body).decode(),
    }
    await manager.append_message(node.session_key, "user", json.dumps({
        "text": "Review the attached original document.", "attachments": [document],
    }))
    await manager.append_message(node.session_key, "assistant", "Received original document.")
    materialized_path = resolver(document, node.session_id)
    assert materialized_path and (workspace / materialized_path).read_bytes() == body
    try:
        for generation in (1, 2):
            for index in range(4):
                await manager.append_message(
                    node.session_key, "user", f"Generation {generation} round {index}. "
                    + "Continue the document review. " * 200,
                )
                await manager.append_message(
                    node.session_key, "assistant", "Completed ordinary review. " * 200,
                )
            canonical = await manager.get_canonical_transcript(node.session_key)
            config = synthetic_compaction_config(
                summary=f"Generation {generation}: continue reviewing the original document.",
                attachment_path_resolver=resolver,
                preserve_historical_images=False, protected_recent_messages=2,
                protect_semantic_tail=False,
            )
            result = await manager.compact_with_result(
                node.session_key, context_window_tokens=4000,
                config=config, trigger_reason="manual",
            )
            assert result.skip_reason is None and result.removed_count > 0
            assert materialized_path not in result.summary  # Model omits it deliberately.
            assert result.summary_payload["files_and_artifacts"] == [{
                "path": materialized_path, "provenance": "verified_attachment",
            }]
            assert await manager.get_canonical_transcript(node.session_key) == canonical
            records = build_compaction_context_records(
                context_states=await manager.get_context_states(node.session_key),
                summaries=await manager.get_summaries(node.session_key),
            )
            assert len(records) == 1
            assert records[0].verified_attachment_paths == (materialized_path,)
            assert records[0].text.count(materialized_path) == 1
            assert f"Generation {generation}:" in records[0].text
            if generation == 2:
                assert "Generation 1:" not in records[0].text

        await storage.close()
        storage = SessionStorage(db)
        await storage.connect()
        manager = SessionManager(storage, inject_time_prefix=False)
        source = await manager.capture_compaction_source(node.session_key)
        assert source.previous_attachment_paths == (materialized_path,)
        assert source.previous_summary.count(materialized_path) == 1
        assert (workspace / materialized_path).read_bytes() == body
        assert await manager.get_canonical_transcript(node.session_key) == canonical

        payloads = []

        def respond(request):
            payloads.append(json.loads(request.content))
            chunks = [
                {"choices": [{"delta": {"content": "Continue reviewing."},
                              "finish_reason": None}]},
                {"choices": [{"delta": {}, "finish_reason": "stop"}]},
            ]
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  content=("".join(f"data: {json.dumps(c)}\n\n" for c in chunks)
                                           + "data: [DONE]\n\n").encode())

        client = httpx.AsyncClient
        monkeypatch.setattr(
            "opensquilla.provider.openai.httpx.AsyncClient",
            lambda **kwargs: client(**{**kwargs, "transport": httpx.MockTransport(respond)}),
        )
        agent = Agent(provider=OpenAIProvider(api_key="synthetic", model="synthetic"),
                      config=AgentConfig(
                          context_window_tokens=32768, max_tokens=1024,
                          compaction_summary_context=format_compaction_summary_context(
                              [source.previous_summary],
                          ),
                      ))
        events = [event async for event in agent.run_turn("Continue the original review.")]
        assert any(isinstance(event, DoneEvent) for event in events)
        assert not any(isinstance(event, ErrorEvent) for event in events)
        assert len(payloads) == 1
        visible = json.dumps(payloads[0]["messages"])
        assert visible.count(materialized_path) == 1
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_verified_references_are_included_in_consumer_admission(tmp_path):
    _, resolver = _resolver(tmp_path)
    document = {"mime": "application/pdf", "name": "document.pdf",
                "data": base64.b64encode(b"%PDF-1.4\noriginal").decode()}
    seen = []

    def reject(checkpoint, kept):
        seen.append(checkpoint)
        return False

    result = await compact_context(CompactionRequest(
        session_id="admission", context_window_tokens=4000,
        entries=[
            {"role": "user", "content": json.dumps({"text": "Read", "attachments": [document]})},
            {"role": "assistant", "content": "Received"},
        ], forced_prefix_cut=2, consumer_admission=reject,
        config=synthetic_compaction_config(
            attachment_path_resolver=resolver, protect_semantic_tail=False,
        ),
    ))
    path = resolver(document, "admission")
    assert seen and all(path in candidate for candidate in seen)
    assert result.removed_count == 0 and result.skip_reason


@pytest.mark.asyncio
async def test_inline_request_inherits_only_selected_host_verified_refs(tmp_path, monkeypatch):
    from opensquilla.provider.types import Message
    from tests.test_engine.test_compaction_rolling_source import _agent, _handler, _input

    workspace, resolver = _resolver(tmp_path)
    body = b"%PDF-1.4\nverified"
    document = {"mime": "application/pdf", "name": "original.pdf",
                "data": base64.b64encode(body).decode()}
    path = resolver(document, "inline")
    assert (workspace / path).read_bytes() == body
    storage = SessionStorage(":memory:")
    await storage.connect()
    manager = SessionManager(storage, inject_time_prefix=False)
    node = await manager.create("agent:main:verified-inline")
    try:
        for role, content in [("user", "old request"), ("assistant", "old response"),
                              ("user", "current request")]:
            await manager.append_message(node.session_key, role, content)
        original = await manager.get_transcript(node.session_key)
        checkpoint, _ = build_structured_summary_from_text(
            "Review continues.", [], verified_attachment_paths=(path,),
        )
        # A legacy generic file sidecar is readable but is not proof of host
        # materialization. Even a real file must not acquire that provenance.
        generic = workspace / "generic.pdf"
        generic.write_bytes(body)
        checkpoint.files_and_artifacts.append({"path": str(generic)})
        original_source = await manager.capture_compaction_source(node.session_key)
        installed = await manager.persist_compaction_result(
            node.session_key, render_structured_summary(checkpoint),
            [entry.model_dump() for entry in original],
            summary_payload=checkpoint.model_dump(mode="json"), summary_format="structured_v1",
            source_entries=original_source.entries, source_preimage=original_source.preimage,
            source_context_fingerprint=original_source.context_fingerprint,
            removed_count=0,
        )
        assert installed.applied
        snapshot = await manager.capture_compaction_source(node.session_key)
        assert snapshot.previous_attachment_paths == (path,)
        agent = _agent()
        agent.bind_compaction_source(snapshot)
        seen = []

        async def capture(request):
            seen.append(request)
            updated, _ = build_structured_summary_from_text(
                "The next review is underway.", [],
                verified_attachment_paths=request.previous_attachment_paths,
            )
            updated.source_coverage["replaces_prior_context"] = True
            return CompactionResult(
                summary=updated.current_status,
                summary_payload=updated.model_dump(mode="json"), summary_format="structured_v1",
                kept_entries=request.entries[2:], kept_start_index=2, removed_count=2,
                chunks_processed=1, replaced_previous_summary=True,
            )

        monkeypatch.setattr(agent, "_execute_compaction_request", capture)
        outcome = await agent._check_context_overflow(
            [Message(role=entry.role, content=entry.content) for entry in original],
            40_000, protected_turn_start_index=2,
        )
        assert len(seen) == 1
        assert seen[0].previous_attachment_paths == (path,)
        assert str(generic) not in seen[0].previous_attachment_paths
        assert outcome is not None and outcome.compacted and not outcome.ephemeral_only
        agent._stage_pending_durable_compaction(outcome)
        event = agent._pending_durable_compaction_event
        assert event is not None
        handler, _ = _handler(manager)
        inp = _input(agent, node, snapshot)
        await handler.handle(event, inp)
        assert event.persist_result.applied
        assert inp.state.compaction_source_snapshot.previous_attachment_paths == (path,)
        selected = build_compaction_context_records(
            context_states=await manager.get_context_states(node.session_key),
            summaries=await manager.get_summaries(node.session_key),
        )
        assert len(selected) == 1 and selected[0].verified_attachment_paths == (path,)
        assert str(generic) not in selected[0].text
        assert await manager.get_canonical_transcript(node.session_key) == original
    finally:
        await storage.close()
