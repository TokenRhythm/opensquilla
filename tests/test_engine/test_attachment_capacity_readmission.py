"""Real turn execution cannot send an attachment until fresh history is admitted."""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.engine.agent import Agent
from opensquilla.engine.runtime import TurnRunner
from opensquilla.engine.selector_override import _capacity_deployment_fingerprint
from opensquilla.gateway.config import GatewayConfig
from opensquilla.provider.model_catalog import ModelCatalog
from opensquilla.provider.openai import OpenAIProvider
from opensquilla.provider.selector import ModelSelector, ProviderConfig, SelectorConfig
from opensquilla.provider.types import (
    ChatConfig,
    ContentBlockImage,
    DoneEvent,
    Message,
    TextDeltaEvent,
)
from opensquilla.session.manager import SessionManager
from opensquilla.session.storage import SessionStorage
from opensquilla.tools.types import CallerKind, ToolContext
from tests.helpers.image_bytes import image_bytes


def test_capacity_fingerprint_is_stable_only_within_its_process_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = ProviderConfig(
        provider="custom", model="synthetic-model", api_key="synthetic-key",
        base_url="https://example.invalid/v1",
        extra_body={"settings": {"region": "west", "route": "primary"}},
    )
    equivalent = replace(config, extra_body={
        "settings": {"route": "primary", "region": "west"},
    })
    fingerprint = _capacity_deployment_fingerprint(config, config.provider, config.model)
    assert fingerprint == _capacity_deployment_fingerprint(config, config.provider, config.model)
    assert fingerprint == _capacity_deployment_fingerprint(
        equivalent, equivalent.provider, equivalent.model,
    )

    monkeypatch.setattr(
        "opensquilla.engine.selector_override._CAPACITY_DEPLOYMENT_FINGERPRINT_KEY", b"a" * 32,
    )
    first_scope = _capacity_deployment_fingerprint(config, config.provider, config.model)
    monkeypatch.setattr(
        "opensquilla.engine.selector_override._CAPACITY_DEPLOYMENT_FINGERPRINT_KEY", b"b" * 32,
    )
    assert first_scope != _capacity_deployment_fingerprint(config, config.provider, config.model)


@pytest.mark.parametrize(("field", "value"), [
    ("provider", "another-provider"),
    ("model", "another-model"),
    ("api_key", "another-synthetic-key"),
    ("base_url", "https://another.example.invalid/v1"),
    ("proxy", "http://proxy.example.invalid:8080"),
    ("org_id", "another-synthetic-organization"),
    ("provider_routing", {"order": "latency"}),
    ("extra_body", {"settings": {"route": "secondary"}}),
])
def test_capacity_fingerprint_distinguishes_every_bound_deployment_field(
    field: str, value: Any,
) -> None:
    config = ProviderConfig(
        provider="custom", model="synthetic-model", api_key="synthetic-key",
        base_url="https://example.invalid/v1",
        extra_body={"settings": {"route": "primary"}},
    )
    changed = replace(config, **{field: value})
    assert _capacity_deployment_fingerprint(config, config.provider, config.model) != (
        _capacity_deployment_fingerprint(changed, changed.provider, changed.model)
    )


@pytest.fixture
async def attachment_retry_stack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from opensquilla import token_estimation

    # Admission must be deterministic and offline even without tokenizer caches.
    monkeypatch.setattr(token_estimation, "_encoding", token_estimation._ENCODING_UNAVAILABLE)
    calls: list[dict[str, Any]] = []
    agents: list[Agent] = []
    projections: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []
    selectors: list[ModelSelector] = []

    class SyntheticProvider(OpenAIProvider):
        async def chat(
            self, messages: list[Message], tools: Any = None, config: ChatConfig | None = None,
        ) -> AsyncIterator[Any]:
            calls.append({"model": self.model, "messages": messages, "config": config})
            yield TextDeltaEvent(text="The current synthetic image is available.")
            yield DoneEvent(stop_reason="stop", input_tokens=100, output_tokens=10)

    async def route(turn: Any) -> Any:
        turn.model = "synthetic-image"
        turn.metadata.update({
            "routing_applied": True,
            "routed_tier": "c1",
            "routed_model": "synthetic-image",
            "routing_source": "image_route",
            "image_route_reason": "current_turn",
            "image_input_mode": "native",
            "router_image_tier_support": {"c1": "supported"},
        })
        return turn

    original_bind = Agent.bind_durable_consumer

    def capture_bind(agent: Agent, **kwargs: Any) -> None:
        agents.append(agent)
        original_bind(agent, **kwargs)

    async def summary(*_args: Any, **kwargs: Any) -> str:
        summaries.append(kwargs)
        return (
            "All archived batches were completed successfully. Their repeated synthetic "
            "details require no further action. Continue with the user's current image request."
        )

    catalog = ModelCatalog()
    catalog._populate_from_data([{
        "id": "synthetic-image", "architecture": {"input_modalities": ["text", "image"]},
    }])
    catalog.set_user_overrides({
        "openai/synthetic-base": {"context_window": 200_000, "max_output_tokens": 1_024},
        "openai/synthetic-image": {"context_window": 64_000, "max_output_tokens": 1_024},
    })
    config = GatewayConfig(
        llm={
            "provider": "openai", "model": "synthetic-base", "api_key": "synthetic-offline",
            "max_tokens": 1_024, "thinking": "off",
        },
        squilla_router={
            "enabled": True, "auto_thinking": False,
            "tiers": {"c1": {
                "provider": "openai", "model": "synthetic-image", "thinking_level": "off",
            }},
        },
        llm_ensemble={"enabled": False},
        agent_max_provider_retries=0,
    )
    config.state_dir = str(tmp_path / "state")
    config.workspace_dir = str(tmp_path / "workspace")
    config.attachments.media_root = str(tmp_path / "media")
    selector = ModelSelector(SelectorConfig(primary=ProviderConfig(
        provider="openai", model="synthetic-base", api_key="synthetic-offline",
        base_url="https://example.invalid/v1",
    )))
    monkeypatch.setattr("opensquilla.provider.model_catalog._shared_catalog", catalog)
    monkeypatch.setattr("opensquilla.engine.steps.apply_squilla_router", route)
    monkeypatch.setattr(ModelSelector, "resolve", lambda self: SyntheticProvider(
        api_key="synthetic-offline", model=self.current_config.model,
        base_url="https://example.invalid/v1",
    ))
    original_clone = ModelSelector.clone

    def capture_clone(selector: ModelSelector) -> ModelSelector:
        cloned = original_clone(selector)
        selectors.append(cloned)
        return cloned

    monkeypatch.setattr(ModelSelector, "clone", capture_clone)
    monkeypatch.setattr(Agent, "bind_durable_consumer", capture_bind)
    monkeypatch.setattr("opensquilla.session.compaction.call_compaction_llm", summary)
    monkeypatch.setattr("opensquilla.session.compaction.call_compaction_provider", summary)
    storage = SessionStorage(str(tmp_path / "sessions.sqlite"))
    await storage.connect()
    manager = SessionManager(
        storage, inject_time_prefix=False, media_root=config.attachments.media_root,
        checkpoint_workspace_dir=config.workspace_dir,
    )
    key = "agent:main:synthetic-attachment-readmission"
    session, _created = await manager.get_or_create(key)
    for index in range(12):
        await manager.append_message(key, "user", f"Archived batch {index}. " + "detail " * 2_000)
        await manager.append_message(key, "assistant", f"Batch {index} completed successfully.")
    runner = TurnRunner(
        provider_selector=selector, config=config, model_catalog=catalog, session_manager=manager,
    )
    original_project = runner._router_history_capacity_for_request

    async def capture_projection(session_key: str, request: Any, **kwargs: Any) -> dict[str, Any]:
        result = await original_project(session_key, request, **kwargs)
        projections.append({
            **result, "snapshot_generation": request.transcript_snapshot.generation,
            "snapshot_load_count": request.transcript_snapshot.load_count,
        })
        return result

    monkeypatch.setattr(runner, "_router_history_capacity_for_request", capture_projection)
    try:
        yield {
            "runner": runner, "manager": manager, "config": config, "catalog": catalog,
            "key": key, "calls": calls, "agents": agents, "projections": projections,
            "summaries": summaries, "selector": selector,
            "selectors": selectors,
            "session": session,
            "attachment": {
                "type": "image/png", "name": "synthetic.png",
                "data": base64.b64encode(image_bytes()).decode("ascii"),
            },
        }
    finally:
        await storage.close()


async def _run(stack: dict[str, Any]) -> list[Any]:
    return [event async for event in stack["runner"].run(
        stack.get("message", "Inspect this current image."), stack["key"],
        tool_context=stack.get("tool_context", ToolContext(
            is_owner=True, caller_kind=CallerKind.CLI,
        )),
        attachments=stack.get("attachments", [stack["attachment"]]),
        history_has_persisted_user=stack.get("history_has_persisted_user", False),
        bound_user_message_id=stack.get("bound_user_message_id"),
        expected_session_id=stack.get("expected_session_id"),
        expected_session_epoch=stack.get("expected_session_epoch"),
        model=stack.get("model"),
        no_memory_capture=True,
    )]


@pytest.mark.parametrize("persisted_current_turn", [False, True])
async def test_attachment_retry_compacts_real_history_and_preserves_durable_owner(
    attachment_retry_stack: dict[str, Any], persisted_current_turn: bool,
) -> None:
    stack = attachment_retry_stack
    current_message_id = "synthetic-current-user-message"
    if persisted_current_turn:
        session = stack["session"]
        stack.update({
            "history_has_persisted_user": True,
            "bound_user_message_id": current_message_id,
            "expected_session_id": session.session_id,
            "expected_session_epoch": session.epoch,
        })
        await stack["manager"].append_message(
            stack["key"], "user", json.dumps({
                "text": "Inspect this current image.", "attachments": [stack["attachment"]],
            }),
            message_id=current_message_id,
            expected_session_id=session.session_id,
            expected_session_epoch=session.epoch,
        )
    events = await _run(stack)
    assert not [event for event in events if getattr(event, "kind", "") == "error"], {
        "summary_calls": len(stack["summaries"]), "projections": stack["projections"],
    }
    assert len(stack["calls"]) == 1
    assert stack["summaries"]
    assert stack["calls"][0]["model"] == "synthetic-image"
    images = [
        block
        for message in stack["calls"][0]["messages"]
        if isinstance(message.content, list)
        for block in message.content
        if isinstance(block, ContentBlockImage)
    ]
    assert len(images) == 1
    assert images[0].data == stack["attachment"]["data"]
    visible_text = [
        message.content if isinstance(message.content, str) else "".join(
            getattr(block, "text", "") for block in message.content
        )
        for message in stack["calls"][0]["messages"]
    ]
    assert sum(text.count("Inspect this current image.") for text in visible_text) == 1
    if persisted_current_turn:
        transcript = await stack["manager"].get_transcript(stack["key"])
        current_entries = [entry for entry in transcript if entry.message_id == current_message_id]
        assert len(current_entries) == 1
        assert json.loads(current_entries[0].content)["attachments"] == [stack["attachment"]]
    assert stack["agents"][0]._durable_consumer_model_id == "synthetic-base"
    assert stack["agents"][0]._durable_consumer_max_output_tokens == 1_024
    assert len(stack["projections"]) >= 2
    first, last = stack["projections"][0], stack["projections"][-1]
    assert last["history_capacity_estimated_tokens"] < first["history_capacity_estimated_tokens"]
    assert last["snapshot_generation"] > first["snapshot_generation"]
    assert last["snapshot_load_count"] > first["snapshot_load_count"]


async def test_live_workspace_image_uses_original_path_and_complete_readmission(
    attachment_retry_stack: dict[str, Any],
) -> None:
    from opensquilla.execution_workspaces import configured_execution_workspace

    stack = attachment_retry_stack
    workspace = Path(stack["config"].workspace_dir)
    workspace.mkdir(parents=True)
    source = workspace / "current.png"
    payload = image_bytes()
    source.write_bytes(payload)
    binding = configured_execution_workspace(workspace)
    await stack["manager"].update(stack["key"], execution_workspace=binding)
    stack["attachments"] = []
    stack["tool_context"] = ToolContext(
        is_owner=True, caller_kind=CallerKind.CLI, run_mode="full",
        workspace_dir=str(workspace), workspace_files=[{
            "workspaceId": binding["id"], "relativePath": source.name,
            "name": source.name, "mime": "image/png",
        }],
    )

    events = await _run(stack)
    assert not [event for event in events if getattr(event, "kind", "") == "error"]
    assert stack["summaries"]
    assert len(stack["calls"]) == 1
    images = [
        block for message in stack["calls"][0]["messages"]
        if isinstance(message.content, list)
        for block in message.content if isinstance(block, ContentBlockImage)
    ]
    assert len(images) == 1
    assert base64.b64decode(images[0].data) == payload
    assert source.read_bytes() == payload
    assert not (workspace / ".opensquilla" / "attachments").exists()
    assert stack["agents"][0].config.metadata["attachment_image_count"] == 1


@pytest.mark.parametrize("cache_enabled", [False, True])
async def test_attachment_route_counts_plan_reference_without_promoting_or_repeating_it(
    attachment_retry_stack: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
    cache_enabled: bool,
) -> None:
    from opensquilla.engine import steps
    from opensquilla.session.plans import new_plan_revision

    stack = attachment_retry_stack
    stack["config"].prompt_cache.mode = "on" if cache_enabled else "off"
    stack["catalog"]._populate_from_data([
        {"id": model, "architecture": {"input_modalities": ["text", "image"]}}
        for model in ("synthetic-image", "synthetic-image-large")
    ])
    stack["catalog"].set_user_overrides({
        "openai/synthetic-base": {"context_window": 200_000, "max_output_tokens": 1_024},
        "openai/synthetic-image": {"context_window": 128_000, "max_output_tokens": 1_024},
        "openai/synthetic-image-large": {"context_window": 200_000, "max_output_tokens": 1_024},
    })
    stack["config"].squilla_router.tiers["c2"] = {
        "provider": "openai", "model": "synthetic-image-large", "thinking_level": "off",
    }
    route = steps.apply_squilla_router

    async def route_with_large_image_candidate(turn: Any) -> Any:
        turn = await route(turn)
        turn.metadata["router_image_tier_support"]["c2"] = "supported"
        return turn

    monkeypatch.setattr(steps, "apply_squilla_router", route_with_large_image_candidate)
    proposal = "SYNTHETIC_PLAN_REFERENCE " + "Synthetic agreed step. " * 4_000
    revision = new_plan_revision(
        source_session_key=stack["key"], source_session_id=stack["session"].session_id,
        source_epoch=0, title="Synthetic proposal", markdown=proposal,
        steps=[{"title": "Inspect the current image"}],
    )
    stack["tool_context"] = ToolContext(
        is_owner=True, caller_kind=CallerKind.CLI,
        plan_run_id="synthetic-plan-run", plan_revision=revision,
    )

    events = await _run(stack)
    assert not [event for event in events if getattr(event, "kind", "") == "error"]
    assert len(stack["calls"]) == 1
    assert stack["calls"][0]["model"] == "synthetic-image-large"
    assert not stack["summaries"]
    call = stack["calls"][0]
    assert "SYNTHETIC_PLAN_REFERENCE" not in (call["config"].system or "")
    assert sum(
        message.content.count("SYNTHETIC_PLAN_REFERENCE")
        for message in call["messages"] if isinstance(message.content, str)
    ) == 1


@pytest.mark.parametrize("skill_state", ["valid", "changed", "oversized"])
async def test_selected_skill_and_attachment_share_verified_request_capacity(
    attachment_retry_stack: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    skill_state: str,
) -> None:
    from opensquilla.skills import eligibility
    from opensquilla.skills.tree import compute_tree_sha256
    from opensquilla.skills.types import SkillLayer, SkillSpec
    from opensquilla.tools.registry import ToolRegistry, tool
    from opensquilla.tools.types import PlanAccess

    stack = attachment_retry_stack
    directory = tmp_path / "synthetic-selected-skill"
    directory.mkdir()
    content = "SYNTHETIC_SELECTED_INSTRUCTIONS " + (
        "Synthetic explicit constraint. " * (10_000 if skill_state == "oversized" else 20)
    )
    (directory / "SKILL.md").write_text(content, encoding="utf-8")
    skill = SkillSpec(
        name="synthetic-reader", description="Synthetic attachment reader",
        layer=SkillLayer.PERSONAL, always=False, triggers=[], content=content,
        base_dir=str(directory), instance_id="synthetic-instance",
        tree_digest=compute_tree_sha256(directory),
    )
    catalog = SimpleNamespace(skills=(skill,), generation=1)
    monkeypatch.setattr(stack["runner"], "_resolve_skill_catalog", lambda: catalog)
    monkeypatch.setattr(eligibility, "_live_skills_cfg_getter", None)
    registry = ToolRegistry()

    @tool(
        name="skill_view", description="Synthetic skill reader", default_access="deny",
        plan_access=PlanAccess.READ_ONLY, registry=registry,
    )
    async def skill_reader():
        return content

    stack["runner"]._tool_registry = registry
    stack["tool_context"] = ToolContext(
        is_owner=True, caller_kind=CallerKind.CLI,
        selected_skills=({
            "name": skill.name, "instanceId": skill.instance_id, "digest": skill.tree_digest,
        },),
    )
    if skill_state == "changed":
        (directory / "SKILL.md").write_text("Changed synthetic instructions", encoding="utf-8")

    events = await _run(stack)
    errors = [event for event in events if getattr(event, "kind", "") == "error"]
    if skill_state != "valid":
        assert errors
        assert not stack["calls"]
        assert not stack["summaries"]
        return

    assert not errors
    assert stack["summaries"]
    assert len(stack["calls"]) == 1
    call = stack["calls"][0]
    visible = (call["config"].system or "") + "\n".join(
        message.content for message in call["messages"] if isinstance(message.content, str)
    )
    assert visible.count("SYNTHETIC_SELECTED_INSTRUCTIONS") == 1
    images = [
        block for message in call["messages"] if isinstance(message.content, list)
        for block in message.content if isinstance(block, ContentBlockImage)
    ]
    assert len(images) == 1 and images[0].data == stack["attachment"]["data"]
    receipts = [event.content for event in events if getattr(event, "kind", "") == "skill_load"]
    assert [receipt["status"] for receipt in receipts] == ["loading", "loaded"]
    assert stack["projections"][-1]["snapshot_generation"] > (
        stack["projections"][0]["snapshot_generation"]
    )


@pytest.mark.parametrize("explicit_model", [
    "synthetic-base", "synthetic-image", "synthetic-other-small",
])
async def test_explicit_attachment_model_admits_full_request_before_compaction(
    attachment_retry_stack: dict[str, Any], explicit_model: str,
) -> None:
    stack = attachment_retry_stack
    stack["model"] = explicit_model
    stack["catalog"].set_user_overrides({
        "openai/synthetic-base": {"context_window": 200_000, "max_output_tokens": 1_024},
        "openai/synthetic-image": {"context_window": 64_000, "max_output_tokens": 1_024},
        "openai/synthetic-other-small": {"context_window": 64_000, "max_output_tokens": 1_024},
    })

    events = await _run(stack)
    errors = [event for event in events if getattr(event, "kind", "") == "error"]
    if explicit_model == "synthetic-other-small":
        assert errors
        assert not stack["calls"]
        assert not stack["summaries"]
        return

    assert not errors
    assert len(stack["calls"]) == 1
    assert stack["calls"][0]["model"] == explicit_model
    assert bool(stack["summaries"]) is (explicit_model == "synthetic-image")
    assert stack["agents"][0]._durable_consumer_model_id == "synthetic-base"


@pytest.mark.parametrize("failure", [
    "summary_failed", "history_unchanged", "disabled", "unknown", "oversized_current",
    "deployment_changed", "endpoint_changed", "extra_body_changed",
])
async def test_attachment_retry_failures_never_execute_the_ordinary_provider(
    attachment_retry_stack: dict[str, Any], monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    stack = attachment_retry_stack
    if failure == "summary_failed":
        async def failed_summary(*_args: Any, **_kwargs: Any) -> None:
            return None

        monkeypatch.setattr(
            "opensquilla.session.compaction.call_compaction_provider", failed_summary,
        )
    elif failure == "history_unchanged":
        async def skipped_preflight(*_args: Any, **_kwargs: Any) -> None:
            return None

        monkeypatch.setattr(stack["runner"], "_maybe_preflight_compact", skipped_preflight)
    elif failure == "disabled":
        stack["config"].compaction.enabled = False
    elif failure == "unknown":
        monkeypatch.setattr("opensquilla.provider.model_catalog._shared_catalog", ModelCatalog())
    elif failure == "oversized_current":
        stack["message"] = "Current material " * 40_000
    elif failure in {"deployment_changed", "endpoint_changed", "extra_body_changed"}:
        stage = stack["runner"]._compaction_and_history_stage
        original_run = stage.run

        async def change_deployment(inp: Any) -> Any:
            outcome = await original_run(inp)
            field, value = {
                "deployment_changed": ("api_key", "other-synthetic-deployment"),
                "endpoint_changed": ("base_url", "https://changed.example.invalid/v1"),
                "extra_body_changed": ("extra_body", {"settings": {"route": "secondary"}}),
            }[failure]
            setattr(stack["selectors"][-1].current_config, field, value)
            return outcome

        monkeypatch.setattr(stage, "run", change_deployment)

    events = await _run(stack)
    assert [event for event in events if getattr(event, "kind", "") == "error"]
    assert not stack["calls"]
    if failure in {"disabled", "unknown", "oversized_current"}:
        assert not stack["summaries"]
    if failure == "history_unchanged":
        first, last = stack["projections"][0], stack["projections"][-1]
        assert (
            last["history_capacity_estimated_tokens"] == first["history_capacity_estimated_tokens"]
        )
        assert last["snapshot_generation"] > first["snapshot_generation"]
    if failure in {"deployment_changed", "endpoint_changed", "extra_body_changed"}:
        assert stack["summaries"]
        assert stack["agents"][0]._durable_consumer_model_id == "synthetic-base"
