from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from opensquilla.artifacts import ArtifactSource
from opensquilla.engine.runtime import TurnRunner
from opensquilla.engine.types import ArtifactEvent, ErrorEvent
from opensquilla.gateway.config import AttachmentsConfig, GatewayConfig, SquillaRouterConfig
from opensquilla.provider import DoneEvent as ProviderDone
from opensquilla.provider import TextDeltaEvent as ProviderText
from opensquilla.provider import ToolUseEndEvent as ProviderToolUseEnd
from opensquilla.provider import ToolUseStartEvent as ProviderToolUseStart
from opensquilla.session.manager import SessionManager
from opensquilla.session.storage import SessionStorage
from opensquilla.tools.builtin.artifacts import publish_artifact
from opensquilla.tools.registry import ToolRegistry, ToolSpec
from opensquilla.tools.types import CallerKind, ToolContext, current_tool_context


class _PublishProvider:
    provider_name = "test"
    model = "test/model"

    def __init__(self) -> None:
        self.calls = 0

    def chat(self, messages, tools=None, config=None):
        self.calls += 1
        return self._stream(self.calls)

    async def _stream(self, call_number):
        if call_number == 1:
            yield ProviderToolUseStart(tool_use_id="publish", tool_name="publish_file")
            yield ProviderToolUseEnd(
                tool_use_id="publish", tool_name="publish_file", arguments={},
            )
            yield ProviderDone(stop_reason="tool_use", input_tokens=1, output_tokens=1)
            return
        yield ProviderText(text="Here is your file.")
        yield ProviderDone(stop_reason="stop", input_tokens=1, output_tokens=1)


class _ProviderSelector:
    def __init__(self):
        self.provider = _PublishProvider()
        self.current_config = SimpleNamespace(model="test/model")

    def clone(self):
        return _ProviderSelector()

    def override_model(self, model):
        self.current_config = SimpleNamespace(model=model)
        self.provider.model = model

    def resolve(self):
        return self.provider


@pytest.mark.asyncio
@pytest.mark.parametrize("concurrent", [False, True], ids=["sequential", "concurrent"])
async def test_reused_base_context_keeps_publications_local_to_each_turn(
    tmp_path, concurrent: bool,
) -> None:
    (tmp_path / "index.html").write_text("<!doctype html><h1>Report</h1>")
    prior_source = ArtifactSource(path=str(tmp_path / "prior.html"), artifact_id="prior")
    prior_artifact = {"id": "prior", "name": "Prior report.html"}
    adopted = []

    # Plain embedding callbacks have no source-binding hook and remain usable.
    async def adopt(event):
        adopted.append(event)

    base = ToolContext(
        is_owner=True,
        caller_kind=CallerKind.WEB,
        workspace_dir=str(tmp_path),
        published_artifacts=[prior_artifact],
        artifact_source_paths={"prior-publication": prior_source},
        generated_artifact_adopter=adopt,
    )
    contexts = []
    both_started = asyncio.Event()
    registry = ToolRegistry()

    async def publish_file():
        context = current_tool_context.get()
        assert context is not None
        assert context.published_artifacts == []
        assert context.artifact_source_paths == {}
        assert context.generated_artifact_adopter is adopt
        contexts.append(context)
        if concurrent:
            if len(contexts) == 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), timeout=20)
        return await publish_artifact(path="index.html", name="Friendly report.html")

    registry.register(
        ToolSpec(name="publish_file", description="Publish the report", parameters={}),
        publish_file,
    )
    storage = SessionStorage(":memory:")
    await storage.connect()
    manager = SessionManager(storage)
    first_key = "agent:main:webchat:artifact-turn-first"
    second_key = "agent:main:webchat:artifact-turn-second" if concurrent else first_key
    await manager.create(first_key)
    if concurrent:
        await manager.create(second_key)
    runner = TurnRunner(
        provider_selector=_ProviderSelector(),
        tool_registry=registry,
        session_manager=manager,
        config=GatewayConfig(
            attachments=AttachmentsConfig(media_root=str(tmp_path / "media")),
            squilla_router=SquillaRouterConfig(enabled=False),
        ),
    )

    async def run(session_key):
        return [
            event async for event in runner.run(
                "Send the report again", session_key, tool_context=base,
                history_has_persisted_user=False, no_memory_capture=True,
            )
        ]

    try:
        if concurrent:
            turns = await asyncio.gather(run(first_key), run(second_key))
        else:
            turns = [await run(first_key), await run(second_key)]

        for events in turns:
            assert not any(isinstance(event, ErrorEvent) for event in events)
            assert len([event for event in events if isinstance(event, ArtifactEvent)]) == 1
        assert len(contexts) == 2
        assert contexts[0].published_artifacts is not contexts[1].published_artifacts
        assert contexts[0].artifact_source_paths is not contexts[1].artifact_source_paths
        assert all(len(context.published_artifacts) == 1 for context in contexts)
        assert all(len(context.artifact_source_paths) == 1 for context in contexts)
        assert base.published_artifacts == [prior_artifact]
        assert base.artifact_source_paths == {"prior-publication": prior_source}
        assert base.generated_artifact_adopter is adopt
        assert len(adopted) == 2
    finally:
        await storage.close()
