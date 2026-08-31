"""Gateway attachment history replay e2e tests.

These tests exercise the production upload -> sessions.send -> transcript
material -> SquillaRouter -> TurnRunner history path with deterministic fake
providers. They intentionally avoid live LLM credentials.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

import opensquilla.engine.steps.squilla_router as squilla_router_step
from opensquilla.attachment_refs import transcript_material_path
from opensquilla.engine import Agent, AgentConfig
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
from opensquilla.provider.protocol import (
    IMAGE_INPUT_UNSUPPORTED_CODE,
    IMAGE_INPUT_UNSUPPORTED_MESSAGE,
)
from opensquilla.provider.types import (
    ContentBlockImage,
    ContentBlockText,
    ModelInfo,
    TextDeltaEvent,
)
from opensquilla.session.manager import SessionManager
from opensquilla.session.storage import SessionStorage
from opensquilla.token_estimation import estimate_tokens

_PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
    b"\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4"
    b"\x89\x00\x00\x00\nIDATx\x9cc\xf8\x0f\x00\x01\x01"
    b"\x01\x00\x18\xdd\x8d\xb0\x00\x00\x00\x00IEND\xaeB`\x82"
)

_PROVIDER_ID = "tokenrhythm"
_TEXT_MODEL = "deepseek-v4-pro-0813"
_GATE_MODEL = "deepseek-v4-flash-0731"
_VISION_MODEL = "kimi-k2.6"
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
    active_provider_id = _PROVIDER_ID

    def __init__(
        self,
        providers: dict[str, _RecordingProvider],
        model: str = _TEXT_MODEL,
    ) -> None:
        self.providers = providers
        self.model = model

    def clone(self) -> _RecordingSelector:
        return _RecordingSelector(self.providers, self.model)

    @property
    def current_config(self) -> SimpleNamespace:
        return SimpleNamespace(provider=_PROVIDER_ID, model=self.model)

    def remaining_chain(self) -> list[SimpleNamespace]:
        return [self.current_config]

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
        provider: str = _PROVIDER_ID,  # noqa: ARG002
    ) -> int:
        return user_override if user_override > 0 else 1024

    def resolve_context_window(
        self,
        model_id: str,
        *,
        provider: str = _PROVIDER_ID,  # noqa: ARG002
    ) -> int:
        # Mirror the live TokenRhythm shape: the stable text/base consumer has
        # a much larger durable-history window than the one-turn image route.
        return 1_000_000 if model_id == _TEXT_MODEL else 128_000

    def get_capabilities(
        self,
        model_id: str,
        provider_name: str = _PROVIDER_ID,  # noqa: ARG002
        base_url: str = "",  # noqa: ARG002
    ) -> ModelCapabilities:
        return ModelCapabilities(supports_vision=model_id == _VISION_MODEL)

    def resolve_vision_support(
        self,
        model_id: str,
        *,
        provider_name: str = _PROVIDER_ID,  # noqa: ARG002
        base_url: str = "",  # noqa: ARG002
    ) -> str:
        return "supported" if model_id == _VISION_MODEL else "unsupported"


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
            "provider": _PROVIDER_ID,
            "model": _GATE_MODEL,
            "supports_image": False,
        },
        "c1": {
            "provider": _PROVIDER_ID,
            "model": _TEXT_MODEL,
            "supports_image": False,
        },
        "image_model": {
            "provider": _PROVIDER_ID,
            "model": _VISION_MODEL,
            "supports_image": True,
            "image_only": True,
        },
    }
    config.squilla_router.default_tier = "c1"
    config.llm.provider = _PROVIDER_ID
    config.llm.model = _TEXT_MODEL
    # Synthetic model ids are absent from the production catalog; the fake
    # catalog above declares their per-deployment windows explicitly.
    config.llm.context_window_tokens = 0
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


async def _upload_text(app: Any) -> str:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/api/v1/files/upload",
            files={"file": ("capacity.txt", b"capacity material", "text/plain")},
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
    expected_error_code: str | None = None,
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
            if (
                expected_error_code
                and new_errors[-1].get("code") == expected_error_code
            ):
                if task is not None:
                    await asyncio.wait_for(
                        asyncio.shield(task),
                        timeout=_TURN_TASK_DRAIN_TIMEOUT_SECONDS,
                    )
                return
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


def _assert_persisted_png_attachment(entry: Any) -> None:
    persisted = json.loads(entry.content)
    attachments = persisted.get("attachments")
    assert isinstance(attachments, list) and len(attachments) == 1
    attachment = attachments[0]
    assert attachment["mime"] == "image/png"
    assert attachment["name"] == "first.png"
    assert attachment["sha256_ref"] == hashlib.sha256(_PNG_BYTES).hexdigest()


def _deterministic_png_payload(*, seed: str, size: int = 80_000) -> bytes:
    """Return stable high-entropy PNG-like bytes for capacity regression fixtures."""

    payload = bytearray(_PNG_BYTES)
    counter = 0
    while len(payload) < size:
        payload.extend(hashlib.sha256(f"{seed}:{counter}".encode()).digest())
        counter += 1
    return bytes(payload[:size])


def _inline_image_envelope(text: str, *payloads: bytes) -> str:
    return json.dumps(
        {
            "text": text,
            "attachments": [
                {
                    "type": "image/png",
                    "name": f"legacy-{index}.png",
                    "size": len(payload),
                    "data": base64.b64encode(payload).decode("ascii"),
                }
                for index, payload in enumerate(payloads, start=1)
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


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
async def test_gateway_single_text_model_returns_structured_error_without_provider_call(
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
        expected_error_code=IMAGE_INPUT_UNSUPPORTED_CODE,
    )

    assert len(gate_provider.calls) == gate_calls_before
    assert len(text_provider.calls) == text_calls_before
    assert len(vision_provider.calls) == vision_calls_before
    assert len(usage_sink.started) == usage_started_before
    assert len(usage_sink.finalized) == usage_finalized_before
    assert len(usage_sink.unknown) == usage_unknown_before
    assert _event_payloads(sink, "session.event.text_delta") == []
    errors = _event_payloads(sink, "session.event.error")
    assert errors[-1]["code"] == IMAGE_INPUT_UNSUPPORTED_CODE
    assert errors[-1]["message"] == IMAGE_INPUT_UNSUPPORTED_MESSAGE
    assert _event_payloads(sink, "session.event.done") == []
    transcript = await manager.get_transcript(key)
    assert transcript[-1].role == "system"
    assert IMAGE_INPUT_UNSUPPORTED_MESSAGE in str(transcript[-1].content or "")


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
async def test_gateway_current_image_capacity_uses_route_limited_media_history(
    _e2e_stack: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy inline images must not create a false Router capacity rejection."""

    manager: SessionManager = _e2e_stack["manager"]
    runner: TurnRunner = _e2e_stack["runner"]
    subscription_manager: SubscriptionManager = _e2e_stack["subscription_manager"]
    sink: _EventSink = _e2e_stack["sink"]
    text_provider: _RecordingProvider = _e2e_stack["text_provider"]
    gate_provider: _RecordingProvider = _e2e_stack["gate_provider"]
    vision_provider: _RecordingProvider = _e2e_stack["vision_provider"]
    bootstrap_configs: list[AgentConfig] = _e2e_stack["bootstrap_configs"]
    key = "agent:main:attachment-capacity-replay"
    preflight_calls = 0
    router_capacity_calls: list[dict[str, Any]] = []

    run_preflight = runner._maybe_preflight_compact

    async def _record_preflight(*args: Any, **kwargs: Any) -> None:
        nonlocal preflight_calls
        preflight_calls += 1
        await run_preflight(*args, **kwargs)

    # Exercise the real preflight boundary. The stable text/base deployment has
    # a 1M window while the image route has 128k, so this synthetic history is
    # raw-overflowing for Router admission but naturally below durable
    # compaction pressure, matching the live TokenRhythm topology.
    monkeypatch.setattr(runner, "_maybe_preflight_compact", _record_preflight)
    project_router_capacity = runner._router_history_capacity_for_request

    async def _record_router_capacity(
        session_key: str,
        request: Any,
        **kwargs: Any,
    ) -> dict[str, Any]:
        result = await project_router_capacity(session_key, request, **kwargs)
        router_capacity_calls.append({**kwargs, "result": dict(result)})
        return result

    monkeypatch.setattr(
        runner,
        "_router_history_capacity_for_request",
        _record_router_capacity,
    )
    session = await manager.create(session_key=key, agent_id="main")
    subscription_manager.subscribe_messages(sink.conn_id, key)

    payloads = [
        _deterministic_png_payload(seed=f"legacy-{index}") for index in range(4)
    ]
    envelopes = [
        _inline_image_envelope("legacy turn one", payloads[0]),
        _inline_image_envelope("legacy turn two", payloads[1]),
        _inline_image_envelope("legacy turn three", payloads[2], payloads[3]),
    ]
    assert sum(estimate_tokens(envelope) for envelope in envelopes) > 100_000
    for index, envelope in enumerate(envelopes, start=1):
        await manager.append_message(key, "user", envelope)
        await manager.append_message(key, "assistant", f"legacy answer {index}")

    file_uuid = await _upload_png(_e2e_stack["app"])
    event_count_before = len(sink.events)
    await _send_session_turn(
        ctx=_e2e_stack["ctx"],
        key=key,
        sink=sink,
        message="Describe only the current image.",
        attachments=[_file_uuid_attachment(file_uuid)],
    )

    assert len(text_provider.calls) == 0
    assert len(gate_provider.calls) == 0
    assert len(vision_provider.calls) == 1
    assert not any(
        event == "session.event.error"
        for event, _payload in sink.events[event_count_before:]
    )

    sent_messages = vision_provider.calls[0]["messages"]
    historical_users = [
        message
        for message in sent_messages[:-1]
        if message.role == "user" and _message_has_image(message)
    ]
    assert len(historical_users) == 1
    decoded_images = [
        base64.b64decode(block.data, validate=True)
        for message in sent_messages
        for block in _message_image_blocks(message)
    ]
    assert payloads[0] not in decoded_images
    assert payloads[1] not in decoded_images
    assert payloads[2] in decoded_images
    assert payloads[3] in decoded_images
    assert _PNG_BYTES in decoded_images

    # The provider may receive typed image blocks, but legacy envelope/base64
    # must never survive as text in the projected history.
    legacy_data = {
        base64.b64encode(payload).decode("ascii") for payload in payloads
    }
    for message in sent_messages:
        text_parts: list[str] = []
        if isinstance(message.content, str):
            text_parts.append(message.content)
        elif isinstance(message.content, list):
            text_parts.extend(
                block.text
                for block in message.content
                if isinstance(block, ContentBlockText)
            )
        projected_text = "\n".join(text_parts)
        assert '"attachments":' not in projected_text
        assert all(data not in projected_text for data in legacy_data)

    projected_history_parts: list[str] = []
    replayed_legacy_user_turns = 0
    for message in sent_messages[:-1]:
        message_parts: list[str] = []
        if isinstance(message.content, str):
            message_parts.append(message.content)
        elif isinstance(message.content, list):
            message_parts.extend(
                block.text
                for block in message.content
                if isinstance(block, ContentBlockText)
            )
        projected_history_parts.extend(message_parts)
        if message.role == "user" and "legacy turn" in "\n".join(message_parts):
            replayed_legacy_user_turns += 1
    projected_history_text = "\n".join(projected_history_parts)
    assert replayed_legacy_user_turns == 1
    assert "legacy turn one" not in projected_history_text
    assert "legacy turn two" not in projected_history_text
    assert "legacy answer 1" not in projected_history_text
    assert "legacy answer 2" not in projected_history_text
    assert "legacy turn three" in projected_history_text
    assert "legacy answer 3" in projected_history_text

    router_events = _event_payloads(sink, "session.event.router_decision")
    assert router_events[-1]["source"] == "image_route"
    assert router_events[-1]["model"] == _VISION_MODEL
    done_events = _event_payloads(sink, "session.event.done")
    assert done_events[-1]["image_route_reason"] == "current_turn"
    assert bootstrap_configs[-1].max_history_turns == 1
    assert len(router_capacity_calls) == 1
    assert router_capacity_calls[0]["max_history_turns"] == 1
    assert router_capacity_calls[0]["preserve_image_attachments"] is True
    assert router_capacity_calls[0]["reachable_provider_kinds"] == frozenset(
        {_PROVIDER_ID}
    )
    assert router_capacity_calls[0]["result"]["history_capacity_message_count"] == 2
    assert router_capacity_calls[0]["result"]["history_capacity_estimate_complete"] is True
    assert preflight_calls == 1
    persisted = await manager.get_session(key)
    assert persisted is not None
    assert persisted.session_id == session.session_id
    assert persisted.compaction_count == 0


@pytest.mark.asyncio
async def test_gateway_known_history_pressure_compacts_once_then_readmits(
    _e2e_stack: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A known image deployment may bind only long enough to compact and retry."""

    manager: SessionManager = _e2e_stack["manager"]
    runner: TurnRunner = _e2e_stack["runner"]
    subscription_manager: SubscriptionManager = _e2e_stack[
        "subscription_manager"
    ]
    sink: _EventSink = _e2e_stack["sink"]
    text_provider: _RecordingProvider = _e2e_stack["text_provider"]
    gate_provider: _RecordingProvider = _e2e_stack["gate_provider"]
    vision_provider: _RecordingProvider = _e2e_stack["vision_provider"]
    key = "agent:main:attachment-capacity-compaction-retry"
    await manager.create(session_key=key, agent_id="main")
    subscription_manager.subscribe_messages(sink.conn_id, key)

    # Keep the durable rows small; the route-capacity port below supplies the
    # deterministic before/after estimates so this orchestration regression
    # does not spend minutes tokenizing a synthetic megabyte-scale transcript.
    await manager.append_message(key, "user", "historical user")
    await manager.append_message(
        key,
        "assistant",
        "historical assistant",
    )

    from opensquilla.provider.model_catalog import ModelCatalog

    catalog = ModelCatalog()
    catalog.set_user_overrides(
        {
            f"{_PROVIDER_ID}/{_VISION_MODEL}": {
                "context_window": 128_000,
                "max_output_tokens": 1_024,
            }
        }
    )
    monkeypatch.setattr("opensquilla.provider.model_catalog._shared_catalog", catalog)

    order: list[str] = []
    capacity_results: list[dict[str, Any]] = []
    capacity_observations: list[tuple[int, bool, int]] = []

    async def _record_router_capacity(
        _session_key: str,
        request: Any,
        **kwargs: Any,
    ) -> dict[str, Any]:
        assert kwargs["max_history_turns"] == 1
        assert kwargs["preserve_image_attachments"] is True
        snapshot = request.transcript_snapshot
        assert snapshot is not None
        entries = list(await snapshot.get_entries())
        has_historical_entries = any(
            str(getattr(entry, "content", "")).startswith("historical")
            for entry in entries
        )
        capacity_observations.append(
            (snapshot.generation, has_historical_entries, len(entries))
        )
        result = {
            "history_capacity_estimated_tokens": (
                200_000 if has_historical_entries else 0
            ),
            "history_capacity_message_count": 2 if has_historical_entries else 0,
            "history_capacity_estimate_complete": True,
        }
        order.append("capacity")
        capacity_results.append(dict(result))
        return result

    monkeypatch.setattr(
        runner,
        "_router_history_capacity_for_request",
        _record_router_capacity,
    )
    preflight_calls = 0
    snapshot_generations: list[tuple[int, int]] = []
    run_preflight = runner._maybe_preflight_compact

    async def _record_real_preflight(
        *args: Any,
        **kwargs: Any,
    ) -> None:
        nonlocal preflight_calls
        preflight_calls += 1
        snapshot = kwargs.get("transcript_snapshot")
        assert snapshot is not None
        generation_before = snapshot.generation
        await run_preflight(*args, **kwargs)
        snapshot_generations.append((generation_before, snapshot.generation))

    monkeypatch.setattr(
        runner,
        "_maybe_preflight_compact",
        _record_real_preflight,
    )

    from opensquilla.session import compaction as compaction_module
    from opensquilla.session.compaction import CompactionResult

    estimate_replay_tokens = compaction_module.estimate_entry_model_replay_tokens

    def _force_historical_token_pressure(entry: Any) -> int:
        if str(getattr(entry, "content", "")).startswith("historical"):
            return 100_000
        return estimate_replay_tokens(entry)

    monkeypatch.setattr(
        compaction_module,
        "estimate_entry_model_replay_tokens",
        _force_historical_token_pressure,
    )
    checkpoint_calls = 0

    async def _record_safe_checkpoint(*_args: Any, **_kwargs: Any) -> bool:
        nonlocal checkpoint_calls
        checkpoint_calls += 1
        return True

    monkeypatch.setattr(
        runner,
        "_record_checkpoint_before_compaction",
        _record_safe_checkpoint,
    )
    compact_calls = 0

    async def _install_compacted_history(
        session_key: str,
        _context_window_tokens: int,
        _config: Any,
        **kwargs: Any,
    ) -> CompactionResult:
        nonlocal compact_calls
        compact_calls += 1
        order.append("compact")
        truncated = await manager.truncate(session_key, max_messages=1)
        assert truncated["truncated"] is True
        return CompactionResult(
            summary="Compacted historical context.",
            kept_entries=[],
            removed_count=2,
            chunks_processed=1,
        )

    monkeypatch.setattr(
        manager,
        "compact_with_result",
        _install_compacted_history,
    )
    original_vision_chat = vision_provider.chat

    async def _guarded_vision_chat(
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        order.append("provider")
        assert preflight_calls == 1
        assert order.count("capacity") == 2
        async for event in original_vision_chat(messages, tools, config):
            yield event

    monkeypatch.setattr(vision_provider, "chat", _guarded_vision_chat)

    captured_turns: list[Any] = []
    original_bootstrap = runner._agent_bootstrap_stage.run

    async def _capture_turn(inp: Any) -> Any:
        captured_turns.append(inp.turn)
        return await original_bootstrap(inp)

    monkeypatch.setattr(runner._agent_bootstrap_stage, "run", _capture_turn)

    file_uuid = await _upload_png(_e2e_stack["app"])
    await _send_session_turn(
        ctx=_e2e_stack["ctx"],
        key=key,
        sink=sink,
        message="Describe only the current image after compacting old history.",
        attachments=[_file_uuid_attachment(file_uuid)],
    )

    assert order == ["capacity", "compact", "capacity", "provider"]
    assert preflight_calls == 1
    assert checkpoint_calls == 1
    assert compact_calls == 1
    assert snapshot_generations == [(0, 1)]
    assert capacity_observations[0][:2] == (0, True)
    assert capacity_observations[1][:2] == (1, False)
    assert capacity_observations[0][2] > capacity_observations[1][2]
    assert len(capacity_results) == 2
    assert (
        capacity_results[0]["history_capacity_estimated_tokens"]
        > capacity_results[1]["history_capacity_estimated_tokens"]
    )
    assert len(text_provider.calls) == 0
    assert len(gate_provider.calls) == 0
    assert len(vision_provider.calls) == 1
    assert captured_turns
    turn_metadata = captured_turns[-1].metadata
    assert turn_metadata["large_context_capacity_retry_attempted"] is True
    assert turn_metadata["large_context_capacity_retry_succeeded"] is True
    assert turn_metadata["large_context_capacity_retry_pending"] is False
    assert turn_metadata["large_context_capacity_status"] == "fits"
    assert turn_metadata["large_context_capacity_provisional_model"] == _VISION_MODEL
    assert turn_metadata["large_context_capacity_compaction_preflight_invoked"] is True
    assert not _event_payloads(sink, "session.event.error")
    sent_messages = vision_provider.calls[0]["messages"]
    assert _message_has_image(sent_messages[-1])
    sent_images = _message_image_blocks(sent_messages[-1])
    assert len(sent_images) == 1
    assert base64.b64decode(sent_images[0].data) == _PNG_BYTES
    persisted_transcript = await manager.get_transcript(key)
    persisted_user_rows = [
        entry for entry in persisted_transcript if entry.role == "user"
    ]
    assert len(persisted_user_rows) == 1
    _assert_persisted_png_attachment(persisted_user_rows[0])


@pytest.mark.asyncio
async def test_gateway_post_compaction_capacity_failure_never_calls_provider(
    _e2e_stack: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager: SessionManager = _e2e_stack["manager"]
    runner: TurnRunner = _e2e_stack["runner"]
    subscription_manager: SubscriptionManager = _e2e_stack[
        "subscription_manager"
    ]
    sink: _EventSink = _e2e_stack["sink"]
    text_provider: _RecordingProvider = _e2e_stack["text_provider"]
    gate_provider: _RecordingProvider = _e2e_stack["gate_provider"]
    vision_provider: _RecordingProvider = _e2e_stack["vision_provider"]
    key = "agent:main:attachment-capacity-compaction-insufficient"
    await manager.create(session_key=key, agent_id="main")
    subscription_manager.subscribe_messages(sink.conn_id, key)
    await manager.append_message(key, "user", "historical user")
    await manager.append_message(key, "assistant", "historical assistant")

    from opensquilla.provider.model_catalog import ModelCatalog

    catalog = ModelCatalog()
    catalog.set_user_overrides(
        {
            f"{_PROVIDER_ID}/{_VISION_MODEL}": {
                "context_window": 128_000,
                "max_output_tokens": 1_024,
            }
        }
    )
    monkeypatch.setattr("opensquilla.provider.model_catalog._shared_catalog", catalog)

    order: list[str] = []
    capacity_calls = 0
    capacity_observations: list[tuple[int, int, int]] = []

    async def _still_too_large(
        _session_key: str,
        request: Any,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        nonlocal capacity_calls
        capacity_calls += 1
        snapshot = request.transcript_snapshot
        assert snapshot is not None
        entries = list(await snapshot.get_entries())
        historical_entry_count = sum(
            str(getattr(entry, "content", "")).startswith("historical")
            for entry in entries
        )
        capacity_observations.append(
            (snapshot.generation, historical_entry_count, len(entries))
        )
        order.append("capacity")
        return {
            "history_capacity_estimated_tokens": (
                200_000 if historical_entry_count > 1 else 150_000
            ),
            "history_capacity_message_count": historical_entry_count,
            "history_capacity_estimate_complete": True,
        }

    monkeypatch.setattr(
        runner,
        "_router_history_capacity_for_request",
        _still_too_large,
    )
    preflight_calls = 0
    snapshot_generations: list[tuple[int, int]] = []
    run_preflight = runner._maybe_preflight_compact

    async def _record_real_preflight(
        *args: Any,
        **kwargs: Any,
    ) -> None:
        nonlocal preflight_calls
        preflight_calls += 1
        snapshot = kwargs.get("transcript_snapshot")
        assert snapshot is not None
        generation_before = snapshot.generation
        await run_preflight(*args, **kwargs)
        snapshot_generations.append((generation_before, snapshot.generation))

    monkeypatch.setattr(
        runner,
        "_maybe_preflight_compact",
        _record_real_preflight,
    )

    from opensquilla.session import compaction as compaction_module
    from opensquilla.session.compaction import CompactionResult

    estimate_replay_tokens = compaction_module.estimate_entry_model_replay_tokens

    def _force_historical_token_pressure(entry: Any) -> int:
        if str(getattr(entry, "content", "")).startswith("historical"):
            return 100_000
        return estimate_replay_tokens(entry)

    monkeypatch.setattr(
        compaction_module,
        "estimate_entry_model_replay_tokens",
        _force_historical_token_pressure,
    )
    checkpoint_calls = 0

    async def _record_safe_checkpoint(*_args: Any, **_kwargs: Any) -> bool:
        nonlocal checkpoint_calls
        checkpoint_calls += 1
        return True

    monkeypatch.setattr(
        runner,
        "_record_checkpoint_before_compaction",
        _record_safe_checkpoint,
    )
    compact_calls = 0

    async def _install_insufficient_compacted_history(
        session_key: str,
        _context_window_tokens: int,
        _config: Any,
        **_kwargs: Any,
    ) -> CompactionResult:
        nonlocal compact_calls
        compact_calls += 1
        order.append("compact")
        truncated = await manager.truncate(session_key, max_messages=2)
        assert truncated["truncated"] is True
        return CompactionResult(
            summary="Compacted historical context.",
            kept_entries=[],
            removed_count=1,
            chunks_processed=1,
        )

    monkeypatch.setattr(
        manager,
        "compact_with_result",
        _install_insufficient_compacted_history,
    )
    captured_turns: list[Any] = []
    original_bootstrap = runner._agent_bootstrap_stage.run

    async def _capture_turn(inp: Any) -> Any:
        captured_turns.append(inp.turn)
        return await original_bootstrap(inp)

    monkeypatch.setattr(runner._agent_bootstrap_stage, "run", _capture_turn)

    file_uuid = await _upload_png(_e2e_stack["app"])
    event_count_before = len(sink.events)
    await _send_session_turn(
        ctx=_e2e_stack["ctx"],
        key=key,
        sink=sink,
        message="Describe the current image after trying to compact old history.",
        attachments=[_file_uuid_attachment(file_uuid)],
        expected_error_code="attachment_capacity_too_large",
    )

    assert order == ["capacity", "compact", "capacity"]
    assert capacity_calls == 2
    assert preflight_calls == 1
    assert checkpoint_calls == 1
    assert compact_calls == 1
    assert snapshot_generations == [(0, 1)]
    assert capacity_observations[0][:2] == (0, 2)
    assert capacity_observations[1][:2] == (1, 1)
    assert capacity_observations[0][2] > capacity_observations[1][2]
    assert len(text_provider.calls) == 0
    assert len(gate_provider.calls) == 0
    assert len(vision_provider.calls) == 0
    errors = [
        payload
        for event, payload in sink.events[event_count_before:]
        if event == "session.event.error"
    ]
    assert errors[-1]["code"] == "attachment_capacity_too_large"
    assert "/compact" in errors[-1]["message"]
    assert "llm.context_window_tokens" not in errors[-1]["message"]
    assert captured_turns
    turn_metadata = captured_turns[-1].metadata
    assert turn_metadata["large_context_capacity_retry_attempted"] is True
    assert turn_metadata["large_context_capacity_retry_pending"] is False
    assert turn_metadata["large_context_capacity_blocked"] is True
    assert turn_metadata["large_context_capacity_status"] == (
        "known_capacity_request_too_large"
    )
    persisted_transcript = await manager.get_transcript(key)
    persisted_user_rows = [
        entry for entry in persisted_transcript if entry.role == "user"
    ]
    assert len(persisted_user_rows) == 1
    _assert_persisted_png_attachment(persisted_user_rows[0])


@pytest.mark.asyncio
async def test_gateway_capacity_unknown_uses_stable_code_without_provider_call(
    _e2e_stack: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config: GatewayConfig = _e2e_stack["config"]
    manager: SessionManager = _e2e_stack["manager"]
    runner: TurnRunner = _e2e_stack["runner"]
    subscription_manager: SubscriptionManager = _e2e_stack[
        "subscription_manager"
    ]
    sink: _EventSink = _e2e_stack["sink"]
    text_provider: _RecordingProvider = _e2e_stack["text_provider"]
    gate_provider: _RecordingProvider = _e2e_stack["gate_provider"]
    vision_provider: _RecordingProvider = _e2e_stack["vision_provider"]
    unknown_model = "catalog-unknown-vision"
    config.squilla_router.tiers["image_model"]["model"] = unknown_model
    config.llm.context_window_tokens = 0

    from opensquilla.provider.model_catalog import ModelCatalog

    monkeypatch.setattr(
        "opensquilla.provider.model_catalog._shared_catalog",
        ModelCatalog(),
    )
    runtime_catalog = runner._model_catalog
    assert runtime_catalog is not None
    monkeypatch.setattr(
        runtime_catalog,
        "get_capabilities",
        lambda _model_id, **_kwargs: ModelCapabilities(supports_vision=True),
    )
    monkeypatch.setattr(
        runtime_catalog,
        "resolve_vision_support",
        lambda _model_id, **_kwargs: "supported",
    )
    key = "agent:main:attachment-capacity-unknown"
    await manager.create(session_key=key, agent_id="main")
    subscription_manager.subscribe_messages(sink.conn_id, key)
    captured_turns: list[Any] = []
    finalize_capacity = squilla_router_step.finalize_squilla_router_capacity

    async def _capture_finalized_turn(turn: Any, **kwargs: Any) -> Any:
        finalized = await finalize_capacity(turn, **kwargs)
        captured_turns.append(finalized)
        return finalized

    monkeypatch.setattr(
        "opensquilla.engine.steps.finalize_squilla_router_capacity",
        _capture_finalized_turn,
    )

    file_uuid = await _upload_png(_e2e_stack["app"])
    event_count_before = len(sink.events)
    await _send_session_turn(
        ctx=_e2e_stack["ctx"],
        key=key,
        sink=sink,
        message="Describe the current image.",
        attachments=[_file_uuid_attachment(file_uuid)],
        expected_error_code="attachment_capacity_unknown",
    )

    errors = [
        payload
        for event, payload in sink.events[event_count_before:]
        if event == "session.event.error"
    ]
    expected_reply = (
        "OpenSquilla could not verify the selected attachment deployment's context "
        "capacity. For a custom or catalog-unknown model, set "
        "llm.context_window_tokens to the deployment's verified context limit."
    )
    assert errors[-1]["code"] == "attachment_capacity_unknown"
    assert errors[-1]["message"] == expected_reply
    assert errors[-1]["terminal_message"] == expected_reply
    assert "internal" not in errors[-1]["message"].lower()
    assert len(text_provider.calls) == 0
    assert len(gate_provider.calls) == 0
    assert len(vision_provider.calls) == 0
    assert captured_turns
    turn_metadata = captured_turns[-1].metadata
    assert turn_metadata["large_context_capacity_blocked"] is True
    assert turn_metadata["large_context_capacity_status"] == "capacity_unknown"
    assert "large_context_capacity_retry_pending" not in turn_metadata
    assert "llm.context_window_tokens" in turn_metadata[
        "large_context_capacity_block_reason"
    ]
    persisted_transcript = await manager.get_transcript(key)
    persisted_user_rows = [
        entry for entry in persisted_transcript if entry.role == "user"
    ]
    assert len(persisted_user_rows) == 1
    _assert_persisted_png_attachment(persisted_user_rows[0])


@pytest.mark.asyncio
@pytest.mark.parametrize("ensemble_scope", ["global", "tier"])
async def test_gateway_ensemble_capacity_block_never_reaches_provider(
    _e2e_stack: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    ensemble_scope: str,
) -> None:
    config: GatewayConfig = _e2e_stack["config"]
    manager: SessionManager = _e2e_stack["manager"]
    runner: TurnRunner = _e2e_stack["runner"]
    subscription_manager: SubscriptionManager = _e2e_stack[
        "subscription_manager"
    ]
    sink: _EventSink = _e2e_stack["sink"]
    text_provider: _RecordingProvider = _e2e_stack["text_provider"]
    gate_provider: _RecordingProvider = _e2e_stack["gate_provider"]
    vision_provider: _RecordingProvider = _e2e_stack["vision_provider"]
    usage_sink: _UsageSink = _e2e_stack["usage_sink"]
    tier_name = "c0" if ensemble_scope == "global" else "c3"
    tier: dict[str, Any] = {
        "provider": _PROVIDER_ID,
        "model": _TEXT_MODEL,
        "supports_image": False,
    }
    if ensemble_scope == "global":
        config.llm_ensemble.enabled = True
        config.llm_ensemble.selection_mode = "static_openrouter_b5"
    else:
        tier["ensemble_enabled"] = True
    config.squilla_router.tiers = {tier_name: tier}
    config.squilla_router.default_tier = tier_name
    monkeypatch.setattr(
        squilla_router_step,
        "_get_strategy",
        lambda _config: _TextTierStrategy(),
    )

    from opensquilla.provider.model_catalog import ModelCatalog

    catalog = ModelCatalog()
    catalog.set_user_overrides(
        {
            f"{_PROVIDER_ID}/{_TEXT_MODEL}": {
                "context_window": 32_000,
                "max_output_tokens": 4_000,
            }
        }
    )
    monkeypatch.setattr("opensquilla.provider.model_catalog._shared_catalog", catalog)
    capacity_calls = 0

    async def _history_pressure(
        _session_key: str,
        request: Any,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        nonlocal capacity_calls
        capacity_calls += 1
        assert request.transcript_snapshot is not None
        return {
            "history_capacity_estimated_tokens": 30_000,
            "history_capacity_message_count": 2,
            "history_capacity_estimate_complete": True,
        }

    monkeypatch.setattr(
        runner,
        "_router_history_capacity_for_request",
        _history_pressure,
    )
    preflight_calls = 0
    run_preflight = runner._maybe_preflight_compact

    async def _record_preflight(*args: Any, **kwargs: Any) -> None:
        nonlocal preflight_calls
        preflight_calls += 1
        await run_preflight(*args, **kwargs)

    monkeypatch.setattr(runner, "_maybe_preflight_compact", _record_preflight)
    captured_turns: list[Any] = []
    finalize_capacity = squilla_router_step.finalize_squilla_router_capacity

    async def _capture_finalized_turn(turn: Any, **kwargs: Any) -> Any:
        finalized = await finalize_capacity(turn, **kwargs)
        captured_turns.append(finalized)
        return finalized

    monkeypatch.setattr(
        "opensquilla.engine.steps.finalize_squilla_router_capacity",
        _capture_finalized_turn,
    )
    key = f"agent:main:attachment-capacity-{ensemble_scope}-ensemble"
    await manager.create(session_key=key, agent_id="main")
    subscription_manager.subscribe_messages(sink.conn_id, key)

    file_uuid = await _upload_text(_e2e_stack["app"])
    event_count_before = len(sink.events)
    await _send_session_turn(
        ctx=_e2e_stack["ctx"],
        key=key,
        sink=sink,
        message="Use the current text attachment.",
        attachments=[
            {
                "file_uuid": file_uuid,
                "mime": "text/plain",
                "name": "capacity.txt",
            }
        ],
        expected_error_code="attachment_capacity_too_large",
    )

    errors = [
        payload
        for event, payload in sink.events[event_count_before:]
        if event == "session.event.error"
    ]
    assert errors[-1]["code"] == "attachment_capacity_too_large"
    assert capacity_calls == 1
    assert preflight_calls == 0
    assert len(text_provider.calls) == 0
    assert len(gate_provider.calls) == 0
    assert len(vision_provider.calls) == 0
    assert usage_sink.started == []
    assert captured_turns
    turn_metadata = captured_turns[-1].metadata
    assert turn_metadata["large_context_capacity_blocked"] is True
    assert turn_metadata["large_context_capacity_status"] == (
        "known_capacity_request_too_large"
    )
    assert "large_context_capacity_retry_pending" not in turn_metadata


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
