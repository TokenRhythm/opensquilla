"""Gateway attachment history replay e2e tests.

These tests exercise the production upload -> sessions.send -> transcript
material -> SquillaRouter -> TurnRunner history path with deterministic fake
providers. They intentionally avoid live LLM credentials.
"""

from __future__ import annotations

import asyncio
import base64
import json
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

import opensquilla.engine.steps.squilla_router as squilla_router_step
from opensquilla.attachment_refs import transcript_material_path
from opensquilla.engine import Agent, AgentConfig
from opensquilla.engine.agent import UNSUPPORTED_IMAGE_INPUT_REPLY
from opensquilla.engine.runtime import TurnRunner
from opensquilla.gateway import rpc_sessions as _rpc_sessions  # noqa: F401
from opensquilla.gateway.agent_tasks import get_agent_task_registry
from opensquilla.gateway.app import create_gateway_app
from opensquilla.gateway.auth import Principal
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.rpc import RpcContext, get_dispatcher
from opensquilla.gateway.uploads import (
    AttachmentNotFoundError,
    UploadStore,
    set_upload_store,
)
from opensquilla.gateway.websocket import SubscriptionManager, get_registry
from opensquilla.provider import ChatConfig, DoneEvent, Message, ModelCapabilities
from opensquilla.provider.types import (
    ContentBlockImage,
    ContentBlockText,
    ModelInfo,
    TextDeltaEvent,
)
from opensquilla.session.manager import SessionManager
from opensquilla.session.storage import SessionStorage

_PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
    b"\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4"
    b"\x89\x00\x00\x00\nIDATx\x9cc\xf8\x0f\x00\x01\x01"
    b"\x01\x00\x18\xdd\x8d\xb0\x00\x00\x00\x00IEND\xaeB`\x82"
)

_TEXT_MODEL = "test/text"
_GATE_MODEL = "test/gate"
_VISION_MODEL = "test/vision"
_TURN_TERMINAL_EVENT_TIMEOUT_SECONDS = 30.0
_TURN_TASK_DRAIN_TIMEOUT_SECONDS = 10.0


class _RecordingProvider:
    provider_name = "fake"

    def __init__(self, text: str = "ok") -> None:
        self.text = text
        self.calls: list[dict[str, Any]] = []

    async def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        self.calls.append({"messages": messages, "tools": tools, "config": config})
        yield TextDeltaEvent(text=self.text)
        yield DoneEvent(stop_reason="end_turn", input_tokens=3, output_tokens=1)

    async def list_models(self) -> list[ModelInfo]:
        return []


class _RecordingSelector:
    active_provider_id = "openrouter"

    def __init__(
        self,
        providers: dict[str, _RecordingProvider],
        model: str = _TEXT_MODEL,
    ) -> None:
        self.providers = providers
        self.model = model

    def clone(self) -> _RecordingSelector:
        return _RecordingSelector(self.providers, self.model)

    def override_model(self, model: str) -> None:
        self.model = model

    def override_model_with_fallback_chain(
        self,
        model: str,
        fallback_chain: list[object],  # noqa: ARG002
    ) -> None:
        self.override_model(model)

    def resolve(self) -> _RecordingProvider:
        return self.providers.get(self.model, self.providers[_TEXT_MODEL])

    async def list_models(self) -> list[dict[str, Any]]:
        return []


class _FakeModelCatalog:
    def resolve_max_tokens(
        self,
        model_id: str,  # noqa: ARG002
        *,
        user_override: int = 0,
        provider: str = "openrouter",  # noqa: ARG002
    ) -> int:
        return user_override if user_override > 0 else 1024

    def resolve_context_window(
        self,
        model_id: str,  # noqa: ARG002
        *,
        provider: str = "openrouter",  # noqa: ARG002
    ) -> int:
        return 8192

    def get_capabilities(
        self,
        model_id: str,
        provider_name: str = "openrouter",  # noqa: ARG002
        base_url: str = "",  # noqa: ARG002
    ) -> ModelCapabilities:
        return ModelCapabilities(supports_vision=model_id == _VISION_MODEL)


class _EventSink:
    authenticated = True

    def __init__(self, conn_id: str) -> None:
        self.conn_id = conn_id
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def send_event(
        self,
        event: str,
        payload: Any = None,
        meta: dict[str, Any] | None = None,  # noqa: ARG002
    ) -> None:
        self.events.append((event, dict(payload or {})))


class _UsageSink:
    def __init__(self) -> None:
        self.started: list[Any] = []
        self.finalized: list[tuple[Any, Any]] = []
        self.unknown: list[tuple[Any, str]] = []

    async def start(self, call: Any) -> None:
        self.started.append(call)

    async def finalize(self, call: Any, result: Any) -> None:
        self.finalized.append((call, result))

    async def mark_unknown(self, call: Any, reason: str) -> None:
        self.unknown.append((call, reason))


class _TextTierStrategy:
    async def classify(
        self,
        message: str,  # noqa: ARG002
        valid_tiers: list[str],
        routing_history: list[dict] | None = None,  # noqa: ARG002
        **kwargs: object,  # noqa: ARG002
    ) -> tuple[str, float, str, dict[str, Any]]:
        tier = "c1" if "c1" in valid_tiers else valid_tiers[0]
        return (
            tier,
            0.87,
            "test_text_route",
            {
                "route_class": "R1",
                "thinking_mode": "T1",
                "prompt_policy": "P0",
            },
        )


def _configure_gateway(tmp_path: Path) -> GatewayConfig:
    config = GatewayConfig()
    config.state_dir = str(tmp_path / "state")
    config.workspace_dir = str(tmp_path / "workspace")
    config.attachments.media_root = str(tmp_path / "media")
    config.squilla_router.enabled = True
    config.squilla_router.rollout_phase = "full"
    config.squilla_router.require_router_runtime = False
    config.squilla_router.vision_history_lookback_turns = 8
    config.squilla_router.vision_history_candidate_turns = 8
    config.squilla_router.vision_sticky_followup_turns = 3
    config.squilla_router.vision_followup_gate_tier = "c0"
    config.squilla_router.tiers = {
        "c0": {
            "provider": "openrouter",
            "model": _GATE_MODEL,
            "supports_image": False,
        },
        "c1": {
            "provider": "openrouter",
            "model": _TEXT_MODEL,
            "supports_image": False,
        },
        "image_model": {
            "provider": "openrouter",
            "model": _VISION_MODEL,
            "supports_image": True,
            "image_only": True,
        },
    }
    config.squilla_router.default_tier = "c1"
    config.llm.provider = "openrouter"
    config.llm.model = _TEXT_MODEL
    # Synthetic model ids are absent from the production catalog. Declare the
    # deployment contract exercised by this end-to-end fixture explicitly.
    config.llm.context_window_tokens = 128_000
    return config


async def _upload_png(app: Any) -> str:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/api/v1/files/upload",
            files={"file": ("first.png", _PNG_BYTES, "image/png")},
        )
    assert response.status_code == 200, response.text
    payload = response.json()
    file_uuid = payload.get("file_uuid")
    assert isinstance(file_uuid, str) and file_uuid.startswith("u-")
    return file_uuid


async def _send_session_turn(
    *,
    ctx: RpcContext,
    key: str,
    sink: _EventSink,
    message: str,
    attachments: list[dict[str, Any]] | None = None,
) -> None:
    done_before = sum(1 for event, _payload in sink.events if event == "session.event.done")
    event_count_before = len(sink.events)
    result = await get_dispatcher().dispatch(
        "test",
        "sessions.send",
        {"key": key, "message": message, "attachments": attachments or []},
        ctx,
    )
    assert result.ok, result.error

    task = get_agent_task_registry().get(key)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _TURN_TERMINAL_EVENT_TIMEOUT_SECONDS
    while loop.time() < deadline:
        done_count = sum(
            1 for event, _payload in sink.events if event == "session.event.done"
        )
        if done_count > done_before:
            if task is not None:
                try:
                    await asyncio.wait_for(
                        asyncio.shield(task),
                        timeout=_TURN_TASK_DRAIN_TIMEOUT_SECONDS,
                    )
                except TimeoutError as exc:
                    raise AssertionError(
                        "timed out waiting for agent task to finish after done event; "
                        f"events={sink.events!r}"
                    ) from exc
            return
        new_errors = [
            payload
            for event, payload in sink.events[event_count_before:]
            if event == "session.event.error"
        ]
        if new_errors:
            raise AssertionError(f"turn emitted error events: {sink.events!r}")
        if task is not None and task.done():
            if task.cancelled():
                raise AssertionError(f"agent task was cancelled; events={sink.events!r}")
            exc = task.exception()
            if exc is not None:
                raise AssertionError(f"agent task failed; events={sink.events!r}") from exc
            raise AssertionError(f"agent task ended without done event; events={sink.events!r}")
        await asyncio.sleep(0.01)
    raise AssertionError(f"timed out waiting for done event; events={sink.events!r}")


def _message_has_image(message: Message) -> bool:
    return isinstance(message.content, list) and any(
        isinstance(block, ContentBlockImage) for block in message.content
    )


def _message_image_blocks(message: Message) -> list[ContentBlockImage]:
    if not isinstance(message.content, list):
        return []
    return [
        block for block in message.content if isinstance(block, ContentBlockImage)
    ]


def _event_payloads(sink: _EventSink, event_name: str) -> list[dict[str, Any]]:
    return [payload for event, payload in sink.events if event == event_name]


def _file_uuid_attachment(file_uuid: str) -> dict[str, str]:
    return {"file_uuid": file_uuid, "mime": "image/png", "name": "first.png"}


@pytest.fixture
async def _e2e_stack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENSQUILLA_OPENROUTER_LIVE_PRICING", "0")
    config = _configure_gateway(tmp_path)
    store = UploadStore(marker_dir=tmp_path / "upload-markers")
    set_upload_store(store)
    storage = SessionStorage(str(tmp_path / "sessions.sqlite"))
    await storage.connect()
    manager = SessionManager(
        storage,
        inject_time_prefix=False,
        media_root=config.attachments.media_root,
    )
    text_provider = _RecordingProvider("text ok")
    gate_provider = _RecordingProvider(
        '{"decision":"needs_image","confidence":0.94,"reason":"visual detail"}'
    )
    vision_provider = _RecordingProvider("vision ok")
    selector = _RecordingSelector(
        {
            _TEXT_MODEL: text_provider,
            _GATE_MODEL: gate_provider,
            _VISION_MODEL: vision_provider,
        }
    )
    usage_sink = _UsageSink()
    runner = TurnRunner(
        provider_selector=selector,
        session_manager=manager,
        config=config,
        model_catalog=_FakeModelCatalog(),
        usage_event_sink=usage_sink,
    )
    bootstrap_configs: list[AgentConfig] = []
    original_bootstrap_run = runner._agent_bootstrap_stage.run

    async def _record_bootstrap_config(inp: Any) -> Any:
        outcome = await original_bootstrap_run(inp)
        if not outcome.terminate and outcome.output is not None:
            bootstrap_configs.append(outcome.output.agent_config)
        return outcome

    runner._agent_bootstrap_stage.run = _record_bootstrap_config  # type: ignore[method-assign]
    subscription_manager = SubscriptionManager()
    sink = _EventSink(f"attachment-history-e2e-{uuid.uuid4().hex}")
    get_registry().register(sink)  # type: ignore[arg-type]
    ctx = RpcContext(
        conn_id=sink.conn_id,
        principal=Principal(
            role="operator",
            scopes=frozenset(["operator.admin"]),
            is_owner=True,
            authenticated=True,
        ),
        session_manager=manager,
        config=config,
        provider_selector=selector,
        subscription_manager=subscription_manager,
        turn_runner=runner,
    )
    app = create_gateway_app(
        config,
        session_manager=manager,
        provider_selector=selector,
        subscription_manager=subscription_manager,
        turn_runner=runner,
    )
    try:
        yield {
            "app": app,
            "bootstrap_configs": bootstrap_configs,
            "config": config,
            "ctx": ctx,
            "gate_provider": gate_provider,
            "manager": manager,
            "runner": runner,
            "sink": sink,
            "storage": storage,
            "store": store,
            "subscription_manager": subscription_manager,
            "text_provider": text_provider,
            "usage_sink": usage_sink,
            "vision_provider": vision_provider,
        }
    finally:
        get_registry().unregister(sink.conn_id)
        set_upload_store(None)
        await storage.close()


@pytest.mark.asyncio
async def test_gateway_single_text_model_returns_friendly_reply_without_provider_call(
    _e2e_stack: dict[str, Any],
) -> None:
    config: GatewayConfig = _e2e_stack["config"]
    manager: SessionManager = _e2e_stack["manager"]
    subscription_manager: SubscriptionManager = _e2e_stack["subscription_manager"]
    sink: _EventSink = _e2e_stack["sink"]
    gate_provider: _RecordingProvider = _e2e_stack["gate_provider"]
    text_provider: _RecordingProvider = _e2e_stack["text_provider"]
    vision_provider: _RecordingProvider = _e2e_stack["vision_provider"]
    usage_sink: _UsageSink = _e2e_stack["usage_sink"]
    config.squilla_router.enabled = False
    key = "agent:main:single-text-model-image"
    await manager.create(session_key=key, agent_id="main")
    subscription_manager.subscribe_messages(sink.conn_id, key)
    for index in range(6):
        await manager.append_message(key, "user", f"history-{index}:" + "u" * 5_000)
        await manager.append_message(key, "assistant", "a" * 5_000)

    file_uuid = await _upload_png(_e2e_stack["app"])
    gate_calls_before = len(gate_provider.calls)
    text_calls_before = len(text_provider.calls)
    vision_calls_before = len(vision_provider.calls)
    usage_started_before = len(usage_sink.started)
    usage_finalized_before = len(usage_sink.finalized)
    usage_unknown_before = len(usage_sink.unknown)
    await _send_session_turn(
        ctx=_e2e_stack["ctx"],
        key=key,
        sink=sink,
        message="请分析这张图片。",
        attachments=[_file_uuid_attachment(file_uuid)],
    )

    assert len(gate_provider.calls) == gate_calls_before
    assert len(text_provider.calls) == text_calls_before
    assert len(vision_provider.calls) == vision_calls_before
    assert len(usage_sink.started) == usage_started_before
    assert len(usage_sink.finalized) == usage_finalized_before
    assert len(usage_sink.unknown) == usage_unknown_before
    deltas = _event_payloads(sink, "session.event.text_delta")
    assert deltas[-1]["text"] == UNSUPPORTED_IMAGE_INPUT_REPLY
    assert _event_payloads(sink, "session.event.error") == []
    done = _event_payloads(sink, "session.event.done")[-1]
    assert done["text"] == UNSUPPORTED_IMAGE_INPUT_REPLY
    assert done["input_tokens"] == 0
    assert done["output_tokens"] == 0
    transcript = await manager.get_transcript(key)
    assert transcript[-1].role == "assistant"
    assert transcript[-1].content == UNSUPPORTED_IMAGE_INPUT_REPLY


@pytest.mark.asyncio
async def test_gateway_upload_history_image_replays_through_squilla_router_gate_history(
    _e2e_stack: dict[str, Any],
) -> None:
    manager: SessionManager = _e2e_stack["manager"]
    subscription_manager: SubscriptionManager = _e2e_stack["subscription_manager"]
    sink: _EventSink = _e2e_stack["sink"]
    store: UploadStore = _e2e_stack["store"]
    vision_provider: _RecordingProvider = _e2e_stack["vision_provider"]
    gate_provider: _RecordingProvider = _e2e_stack["gate_provider"]
    config: GatewayConfig = _e2e_stack["config"]
    bootstrap_configs: list[AgentConfig] = _e2e_stack["bootstrap_configs"]
    key = "agent:main:attachment-history-e2e"
    session = await manager.create(
        session_key=key,
        agent_id="main",
        display_name="attachment history e2e",
    )
    subscription_manager.subscribe_messages(sink.conn_id, key)

    file_uuid = await _upload_png(_e2e_stack["app"])
    await _send_session_turn(
        ctx=_e2e_stack["ctx"],
        key=key,
        sink=sink,
        message="Describe this image.",
        attachments=[_file_uuid_attachment(file_uuid)],
    )

    with pytest.raises(AttachmentNotFoundError):
        await store.get(file_uuid)

    assert vision_provider.calls
    first_call_messages = vision_provider.calls[-1]["messages"]
    current_turn = next(message for message in first_call_messages if _message_has_image(message))
    assert isinstance(current_turn.content, list)
    current_turn_markers = [
        block.text
        for block in current_turn.content
        if isinstance(block, ContentBlockText)
        and block.text.startswith("[attachment available:")
    ]
    workspace_images = list(
        (Path(config.workspace_dir) / ".opensquilla" / "attachments").glob("**/*-first.png")
    )
    assert len(workspace_images) == 1
    assert workspace_images[0].read_bytes() == _PNG_BYTES
    relative_workspace_image = workspace_images[0].relative_to(config.workspace_dir).as_posix()
    assert current_turn_markers == [
        (
            f"[attachment available: first.png (image/png, {len(_PNG_BYTES)} bytes) "
            f"at {relative_workspace_image}]"
        )
    ]

    transcript = await manager.get_transcript(key)
    first_user = transcript[0]
    persisted = json.loads(first_user.content)
    attachment = persisted["attachments"][0]
    assert "file_uuid" not in json.dumps(persisted)
    assert attachment["mime"] == "image/png"
    assert attachment["name"] == "first.png"
    sha = attachment["sha256_ref"]
    assert isinstance(sha, str) and len(sha) == 64
    material_path = transcript_material_path(
        Path(config.attachments.media_root or ""),
        session.session_id,
        sha,
    )
    assert material_path.is_file()
    assert material_path.read_bytes() == _PNG_BYTES

    await manager.append_message(key, "user", "A text-only turn in between.")
    await manager.append_message(key, "assistant", "Text answer in between.")

    vision_calls_before = len(vision_provider.calls)
    await _send_session_turn(
        ctx=_e2e_stack["ctx"],
        key=key,
        sink=sink,
        message="What color is the small corner?",
    )

    assert len(gate_provider.calls) == 1
    assert len(vision_provider.calls) == vision_calls_before + 1
    final_call = vision_provider.calls[-1]
    sent_messages = final_call["messages"]
    image_blocks = [
        block
        for message in sent_messages[:-1]
        for block in _message_image_blocks(message)
    ]
    assert image_blocks
    assert base64.b64decode(image_blocks[0].data, validate=True) == _PNG_BYTES
    assert isinstance(sent_messages[-1].content, str)
    assert sent_messages[-1].content.startswith("What color is the small corner?")

    router_events = _event_payloads(sink, "session.event.router_decision")
    assert router_events[-1]["source"] == "image_route"
    assert router_events[-1]["model"] == _VISION_MODEL
    done_events = _event_payloads(sink, "session.event.done")
    assert done_events[-1]["image_route_reason"] == "gate_history"
    assert done_events[-1]["vision_followup_needs_image"] is True
    assert done_events[-1]["vision_followup_gate_decision"] == "needs_image"
    assert bootstrap_configs[-1].preserve_historical_images is True
    assert (
        bootstrap_configs[-1].max_history_turns
        == config.squilla_router.vision_history_lookback_turns
    )


@pytest.mark.asyncio
async def test_historical_image_material_is_not_replayed_without_vision_support(
    _e2e_stack: dict[str, Any],
) -> None:
    manager: SessionManager = _e2e_stack["manager"]
    runner: TurnRunner = _e2e_stack["runner"]
    subscription_manager: SubscriptionManager = _e2e_stack["subscription_manager"]
    sink: _EventSink = _e2e_stack["sink"]
    key = "agent:main:attachment-history-no-vision"
    await manager.create(session_key=key, agent_id="main")
    subscription_manager.subscribe_messages(sink.conn_id, key)
    file_uuid = await _upload_png(_e2e_stack["app"])
    await _send_session_turn(
        ctx=_e2e_stack["ctx"],
        key=key,
        sink=sink,
        message="Describe this image.",
        attachments=[_file_uuid_attachment(file_uuid)],
    )

    provider = _RecordingProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            model_capabilities=ModelCapabilities(supports_vision=False),
            preserve_historical_images=True,
        ),
    )
    await runner._load_history(agent, key)
    events = [event async for event in agent.run_turn("Follow up.")]

    assert any(getattr(event, "kind", None) == "done" for event in events)
    assert not any(_message_has_image(message) for message in provider.calls[0]["messages"])


@pytest.mark.asyncio
async def test_historical_image_material_outside_lookback_is_not_replayed(
    _e2e_stack: dict[str, Any],
) -> None:
    manager: SessionManager = _e2e_stack["manager"]
    runner: TurnRunner = _e2e_stack["runner"]
    config: GatewayConfig = _e2e_stack["config"]
    subscription_manager: SubscriptionManager = _e2e_stack["subscription_manager"]
    sink: _EventSink = _e2e_stack["sink"]
    key = "agent:main:attachment-history-lookback"
    await manager.create(session_key=key, agent_id="main")
    subscription_manager.subscribe_messages(sink.conn_id, key)
    file_uuid = await _upload_png(_e2e_stack["app"])
    await _send_session_turn(
        ctx=_e2e_stack["ctx"],
        key=key,
        sink=sink,
        message="Describe this image.",
        attachments=[_file_uuid_attachment(file_uuid)],
    )
    await manager.append_message(key, "user", "A later text-only user turn.")
    await manager.append_message(key, "assistant", "A later text-only answer.")
    config.squilla_router.vision_history_lookback_turns = 1

    provider = _RecordingProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            model_capabilities=ModelCapabilities(supports_vision=True),
            preserve_historical_images=True,
        ),
    )
    await runner._load_history(agent, key, trim_last_user=False)
    events = [event async for event in agent.run_turn("Follow up.")]

    assert any(getattr(event, "kind", None) == "done" for event in events)
    assert not any(_message_has_image(message) for message in provider.calls[0]["messages"])


@pytest.mark.asyncio
async def test_gate_text_only_followup_stays_text_and_does_not_replay_history_image(
    _e2e_stack: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(squilla_router_step, "_get_strategy", lambda _cfg: _TextTierStrategy())
    manager: SessionManager = _e2e_stack["manager"]
    subscription_manager: SubscriptionManager = _e2e_stack["subscription_manager"]
    sink: _EventSink = _e2e_stack["sink"]
    gate_provider: _RecordingProvider = _e2e_stack["gate_provider"]
    text_provider: _RecordingProvider = _e2e_stack["text_provider"]
    vision_provider: _RecordingProvider = _e2e_stack["vision_provider"]
    key = "agent:main:attachment-history-text-only"
    await manager.create(session_key=key, agent_id="main")
    subscription_manager.subscribe_messages(sink.conn_id, key)

    file_uuid = await _upload_png(_e2e_stack["app"])
    await _send_session_turn(
        ctx=_e2e_stack["ctx"],
        key=key,
        sink=sink,
        message="Describe this image.",
        attachments=[_file_uuid_attachment(file_uuid)],
    )
    await manager.append_message(key, "user", "A text-only turn in between.")
    await manager.append_message(key, "assistant", "Text answer in between.")
    gate_provider.text = (
        '{"decision":"text_only","confidence":0.91,"reason":"new coding task"}'
    )
    gate_calls_before = len(gate_provider.calls)
    text_calls_before = len(text_provider.calls)
    vision_calls_before = len(vision_provider.calls)

    await _send_session_turn(
        ctx=_e2e_stack["ctx"],
        key=key,
        sink=sink,
        message="Write a small Python script.",
    )

    assert len(gate_provider.calls) == gate_calls_before + 1
    assert len(text_provider.calls) == text_calls_before + 1
    assert len(vision_provider.calls) == vision_calls_before
    sent_messages = text_provider.calls[-1]["messages"]
    assert not any(_message_has_image(message) for message in sent_messages)
    done_events = _event_payloads(sink, "session.event.done")
    assert done_events[-1]["vision_followup_gate_decision"] == "text_only"
    assert done_events[-1]["vision_followup_needs_image"] is False
    assert done_events[-1].get("image_route_reason") is None
