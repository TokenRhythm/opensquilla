"""Gateway non-image attachment materialization e2e tests.

These tests exercise the real upload -> sessions.send -> transcript material ->
SquillaRouter -> runtime/provider path. The upstream provider is deterministic
and only captures final request messages; no live LLM credentials are used.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

import opensquilla.engine.steps.squilla_router as squilla_router_step
from opensquilla.attachment_refs import transcript_material_path
from opensquilla.engine import AgentConfig
from opensquilla.engine.runtime import TurnRunner
from opensquilla.gateway import rpc_sessions as _rpc_sessions  # noqa: F401
from opensquilla.gateway.agent_tasks import get_agent_task_registry
from opensquilla.gateway.app import create_gateway_app
from opensquilla.gateway.auth import Principal
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.rpc import RpcContext, get_dispatcher
from opensquilla.gateway.uploads import UploadStore, set_upload_store
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

_TEXT_MODEL = "test/text"
_GATE_MODEL = "test/gate"
_VISION_MODEL = "test/vision"
_TURN_TERMINAL_EVENT_TIMEOUT_SECONDS = 30.0
_TURN_TASK_DRAIN_TIMEOUT_SECONDS = 10.0

_PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
    b"\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4"
    b"\x89\x00\x00\x00\nIDATx\x9cc\xf8\x0f\x00\x01\x01"
    b"\x01\x00\x18\xdd\x8d\xb0\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _sample_pdf_bytes(text: str = "Machine Learning") -> bytes:
    stream = f"BT /F1 24 Tf 72 720 Td ({text}) Tj ET".encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>"
        ),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n"
        + stream + b"\nendstream",
    ]
    body = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for idx, obj in enumerate(objects, start=1):
        offsets.append(len(body))
        body.extend(f"{idx} 0 obj\n".encode("ascii"))
        body.extend(obj)
        body.extend(b"\nendobj\n")
    xref_offset = len(body)
    body.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    body.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        body.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    body.extend(
        f"trailer\n<< /Root 1 0 R /Size {len(objects) + 1} >>\n"
        f"startxref\n{xref_offset}\n%%EOF\n".encode("ascii")
    )
    return bytes(body)


def _b64(payload: bytes) -> str:
    return base64.b64encode(payload).decode("ascii")


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


async def _upload_file(
    app: Any,
    *,
    name: str,
    mime: str,
    payload: bytes,
) -> str:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/api/v1/files/upload",
            files={"file": (name, payload, mime)},
        )
    assert response.status_code == 200, response.text
    file_uuid = response.json().get("file_uuid")
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


def _event_payloads(sink: _EventSink, event_name: str) -> list[dict[str, Any]]:
    return [payload for event, payload in sink.events if event == event_name]


def _attachment(file_uuid: str, *, mime: str, name: str) -> dict[str, str]:
    return {"file_uuid": file_uuid, "mime": mime, "name": name}


def _inline_attachment(payload: bytes, *, mime: str, name: str) -> dict[str, str]:
    return {"type": mime, "mime": mime, "name": name, "data": _b64(payload)}


def _message_text(message: Message) -> str:
    if isinstance(message.content, str):
        return message.content
    if isinstance(message.content, list):
        parts: list[str] = []
        for block in message.content:
            if isinstance(block, ContentBlockText):
                parts.append(block.text)
        return "\n".join(parts)
    return str(message.content)


def _all_provider_text(messages: list[Message]) -> str:
    return "\n".join(_message_text(message) for message in messages)


def _message_has_image(message: Message) -> bool:
    return isinstance(message.content, list) and any(
        isinstance(block, ContentBlockImage) for block in message.content
    )


@pytest.fixture
async def _e2e_stack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENSQUILLA_OPENROUTER_LIVE_PRICING", "0")
    monkeypatch.setattr(squilla_router_step, "_get_strategy", lambda _cfg: _TextTierStrategy())
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
    runner = TurnRunner(
        provider_selector=selector,
        session_manager=manager,
        config=config,
        model_catalog=_FakeModelCatalog(),
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
    sink = _EventSink(f"non-image-attachments-e2e-{uuid.uuid4().hex}")
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
            "vision_provider": vision_provider,
        }
    finally:
        get_registry().unregister(sink.conn_id)
        set_upload_store(None)
        await storage.close()


@pytest.mark.asyncio
async def test_current_turn_pdf_is_materialized_to_workspace_path(
    _e2e_stack: dict[str, Any],
) -> None:
    manager: SessionManager = _e2e_stack["manager"]
    subscription_manager: SubscriptionManager = _e2e_stack["subscription_manager"]
    sink: _EventSink = _e2e_stack["sink"]
    text_provider: _RecordingProvider = _e2e_stack["text_provider"]
    config: GatewayConfig = _e2e_stack["config"]
    key = "agent:main:pdf-materialization-current"
    session = await manager.create(session_key=key, agent_id="main")
    subscription_manager.subscribe_messages(sink.conn_id, key)
    pdf_bytes = _sample_pdf_bytes()

    file_uuid = await _upload_file(
        _e2e_stack["app"],
        name="L11 RL.pdf",
        mime="application/pdf",
        payload=pdf_bytes,
    )
    await _send_session_turn(
        ctx=_e2e_stack["ctx"],
        key=key,
        sink=sink,
        message="请读取这个 PDF",
        attachments=[_attachment(file_uuid, mime="application/pdf", name="L11 RL.pdf")],
    )

    transcript = await manager.get_transcript(key)
    persisted = json.loads(transcript[0].content)
    persisted_attachment = persisted["attachments"][0]
    assert persisted_attachment["name"] == "L11 RL.pdf"
    assert persisted_attachment["mime"] == "application/pdf"
    sha = persisted_attachment["sha256_ref"]
    assert "file_uuid" not in json.dumps(persisted)
    material_path = transcript_material_path(
        Path(config.attachments.media_root or ""),
        session.session_id,
        sha,
    )
    assert material_path.read_bytes() == pdf_bytes

    workspace_paths = list(
        (Path(config.workspace_dir or "") / ".opensquilla" / "attachments").glob("**/*.pdf")
    )
    assert len(workspace_paths) == 1
    assert workspace_paths[0].read_bytes() == pdf_bytes

    sent_text = _all_provider_text(text_provider.calls[-1]["messages"])
    assert "Machine Learning" in sent_text
    assert "attachment available: L11 RL.pdf (application/pdf" in sent_text
    assert ".opensquilla/attachments/" in sent_text
    assert workspace_paths[0].name in sent_text


@pytest.mark.asyncio
async def test_current_turn_inline_pdf_is_materialized_to_workspace_path(
    _e2e_stack: dict[str, Any],
) -> None:
    manager: SessionManager = _e2e_stack["manager"]
    subscription_manager: SubscriptionManager = _e2e_stack["subscription_manager"]
    sink: _EventSink = _e2e_stack["sink"]
    text_provider: _RecordingProvider = _e2e_stack["text_provider"]
    config: GatewayConfig = _e2e_stack["config"]
    key = "agent:main:pdf-materialization-current-inline"
    await manager.create(session_key=key, agent_id="main")
    subscription_manager.subscribe_messages(sink.conn_id, key)
    pdf_bytes = _sample_pdf_bytes()

    await _send_session_turn(
        ctx=_e2e_stack["ctx"],
        key=key,
        sink=sink,
        message="请读取这个 PDF",
        attachments=[
            _inline_attachment(
                pdf_bytes,
                mime="application/pdf",
                name="L11 RL.pdf",
            )
        ],
    )

    transcript = await manager.get_transcript(key)
    persisted = json.loads(transcript[0].content)
    persisted_attachment = persisted["attachments"][0]
    assert persisted_attachment["name"] == "L11 RL.pdf"
    assert persisted_attachment["type"] == "application/pdf"
    assert persisted_attachment["data"] == _b64(pdf_bytes)
    assert "sha256_ref" not in persisted_attachment

    workspace_paths = list(
        (Path(config.workspace_dir or "") / ".opensquilla" / "attachments").glob("**/*.pdf")
    )
    assert len(workspace_paths) == 1
    assert workspace_paths[0].read_bytes() == pdf_bytes

    sent_text = _all_provider_text(text_provider.calls[-1]["messages"])
    assert "Machine Learning" in sent_text
    assert "attachment available: L11 RL.pdf (application/pdf" in sent_text
    assert ".opensquilla/attachments/" in sent_text
    assert workspace_paths[0].name in sent_text


@pytest.mark.asyncio
async def test_historical_pdf_followup_materializes_path_from_sha256_ref(
    _e2e_stack: dict[str, Any],
) -> None:
    manager: SessionManager = _e2e_stack["manager"]
    subscription_manager: SubscriptionManager = _e2e_stack["subscription_manager"]
    sink: _EventSink = _e2e_stack["sink"]
    text_provider: _RecordingProvider = _e2e_stack["text_provider"]
    config: GatewayConfig = _e2e_stack["config"]
    key = "agent:main:pdf-materialization-history"
    await manager.create(session_key=key, agent_id="main")
    subscription_manager.subscribe_messages(sink.conn_id, key)
    pdf_bytes = _sample_pdf_bytes()

    file_uuid = await _upload_file(
        _e2e_stack["app"],
        name="L11 RL.pdf",
        mime="application/pdf",
        payload=pdf_bytes,
    )
    await _send_session_turn(
        ctx=_e2e_stack["ctx"],
        key=key,
        sink=sink,
        message="请读取这个 PDF",
        attachments=[_attachment(file_uuid, mime="application/pdf", name="L11 RL.pdf")],
    )
    await manager.append_message(key, "user", "中间普通文本。")
    await manager.append_message(key, "assistant", "普通回答。")

    calls_before = len(text_provider.calls)
    await _send_session_turn(
        ctx=_e2e_stack["ctx"],
        key=key,
        sink=sink,
        message="把刚才那个 PDF 左下角 Machine Learning 遮住",
    )

    assert len(text_provider.calls) == calls_before + 1
    sent_messages = text_provider.calls[-1]["messages"]
    sent_text = _all_provider_text(sent_messages)
    assert "historical attachment available: L11 RL.pdf (application/pdf" in sent_text
    assert "historical attachment omitted: L11 RL.pdf" not in sent_text
    assert ".opensquilla/attachments/" in sent_text
    assert isinstance(sent_messages[-1].content, str)
    assert sent_messages[-1].content.startswith("把刚才那个 PDF")
    workspace_paths = list(
        (Path(config.workspace_dir or "") / ".opensquilla" / "attachments").glob("**/*.pdf")
    )
    assert len(workspace_paths) == 1
    assert hashlib.sha256(workspace_paths[0].read_bytes()).digest() == hashlib.sha256(
        pdf_bytes
    ).digest()


@pytest.mark.asyncio
async def test_historical_inline_pdf_followup_materializes_path_from_inline_data(
    _e2e_stack: dict[str, Any],
) -> None:
    manager: SessionManager = _e2e_stack["manager"]
    subscription_manager: SubscriptionManager = _e2e_stack["subscription_manager"]
    sink: _EventSink = _e2e_stack["sink"]
    text_provider: _RecordingProvider = _e2e_stack["text_provider"]
    config: GatewayConfig = _e2e_stack["config"]
    key = "agent:main:pdf-materialization-history-inline"
    await manager.create(session_key=key, agent_id="main")
    subscription_manager.subscribe_messages(sink.conn_id, key)
    pdf_bytes = _sample_pdf_bytes()

    await _send_session_turn(
        ctx=_e2e_stack["ctx"],
        key=key,
        sink=sink,
        message="请读取这个 PDF",
        attachments=[
            _inline_attachment(
                pdf_bytes,
                mime="application/pdf",
                name="L11 RL.pdf",
            )
        ],
    )
    await manager.append_message(key, "user", "中间普通文本。")
    await manager.append_message(key, "assistant", "普通回答。")

    calls_before = len(text_provider.calls)
    await _send_session_turn(
        ctx=_e2e_stack["ctx"],
        key=key,
        sink=sink,
        message="把刚才那个 PDF 左下角 Machine Learning 遮住",
    )

    assert len(text_provider.calls) == calls_before + 1
    sent_messages = text_provider.calls[-1]["messages"]
    sent_text = _all_provider_text(sent_messages)
    assert "historical attachment available: L11 RL.pdf (application/pdf" in sent_text
    assert "historical attachment omitted: L11 RL.pdf" not in sent_text
    assert ".opensquilla/attachments/" in sent_text
    assert isinstance(sent_messages[-1].content, str)
    assert sent_messages[-1].content.startswith("把刚才那个 PDF")
    workspace_paths = list(
        (Path(config.workspace_dir or "") / ".opensquilla" / "attachments").glob("**/*.pdf")
    )
    assert len(workspace_paths) == 1
    assert hashlib.sha256(workspace_paths[0].read_bytes()).digest() == hashlib.sha256(
        pdf_bytes
    ).digest()


@pytest.mark.asyncio
async def test_pdf_materialization_does_not_require_image_tier(
    _e2e_stack: dict[str, Any],
) -> None:
    config: GatewayConfig = _e2e_stack["config"]
    config.squilla_router.tiers.pop("image_model", None)
    manager: SessionManager = _e2e_stack["manager"]
    subscription_manager: SubscriptionManager = _e2e_stack["subscription_manager"]
    sink: _EventSink = _e2e_stack["sink"]
    text_provider: _RecordingProvider = _e2e_stack["text_provider"]
    key = "agent:main:pdf-materialization-no-image-tier"
    await manager.create(session_key=key, agent_id="main")
    subscription_manager.subscribe_messages(sink.conn_id, key)

    file_uuid = await _upload_file(
        _e2e_stack["app"],
        name="L11 RL.pdf",
        mime="application/pdf",
        payload=_sample_pdf_bytes(),
    )
    await _send_session_turn(
        ctx=_e2e_stack["ctx"],
        key=key,
        sink=sink,
        message="请读取这个 PDF",
        attachments=[_attachment(file_uuid, mime="application/pdf", name="L11 RL.pdf")],
    )

    sent_text = _all_provider_text(text_provider.calls[-1]["messages"])
    assert "attachment available: L11 RL.pdf (application/pdf" in sent_text
    done_events = _event_payloads(sink, "session.event.done")
    assert done_events[-1].get("image_route_reason") is None


@pytest.mark.asyncio
async def test_mixed_historical_image_and_pdf_followup_replays_image_and_materializes_pdf(
    _e2e_stack: dict[str, Any],
) -> None:
    manager: SessionManager = _e2e_stack["manager"]
    subscription_manager: SubscriptionManager = _e2e_stack["subscription_manager"]
    sink: _EventSink = _e2e_stack["sink"]
    vision_provider: _RecordingProvider = _e2e_stack["vision_provider"]
    key = "agent:main:mixed-image-pdf-history"
    await manager.create(session_key=key, agent_id="main")
    subscription_manager.subscribe_messages(sink.conn_id, key)

    image_uuid = await _upload_file(
        _e2e_stack["app"],
        name="first.png",
        mime="image/png",
        payload=_PNG_BYTES,
    )
    pdf_bytes = _sample_pdf_bytes()
    pdf_uuid = await _upload_file(
        _e2e_stack["app"],
        name="L11 RL.pdf",
        mime="application/pdf",
        payload=pdf_bytes,
    )
    await _send_session_turn(
        ctx=_e2e_stack["ctx"],
        key=key,
        sink=sink,
        message="请同时看看这张图和 PDF",
        attachments=[
            _attachment(image_uuid, mime="image/png", name="first.png"),
            _attachment(pdf_uuid, mime="application/pdf", name="L11 RL.pdf"),
        ],
    )
    await manager.append_message(key, "user", "中间普通文本。")
    await manager.append_message(key, "assistant", "普通回答。")

    vision_calls_before = len(vision_provider.calls)
    await _send_session_turn(
        ctx=_e2e_stack["ctx"],
        key=key,
        sink=sink,
        message="刚才那张图右上角是什么？另外 PDF 也保持可编辑。",
    )

    assert len(vision_provider.calls) == vision_calls_before + 1
    sent_messages = vision_provider.calls[-1]["messages"]
    assert any(_message_has_image(message) for message in sent_messages[:-1])
    sent_text = _all_provider_text(sent_messages)
    assert "historical attachment available: L11 RL.pdf (application/pdf" in sent_text
    done_events = _event_payloads(sink, "session.event.done")
    assert done_events[-1]["image_route_reason"] == "gate_history"
    assert done_events[-1]["vision_followup_needs_image"] is True


@pytest.mark.asyncio
async def test_router_text_only_does_not_replay_image_but_keeps_pdf_path(
    _e2e_stack: dict[str, Any],
) -> None:
    manager: SessionManager = _e2e_stack["manager"]
    subscription_manager: SubscriptionManager = _e2e_stack["subscription_manager"]
    sink: _EventSink = _e2e_stack["sink"]
    gate_provider: _RecordingProvider = _e2e_stack["gate_provider"]
    text_provider: _RecordingProvider = _e2e_stack["text_provider"]
    vision_provider: _RecordingProvider = _e2e_stack["vision_provider"]
    key = "agent:main:mixed-image-pdf-text-only"
    await manager.create(session_key=key, agent_id="main")
    subscription_manager.subscribe_messages(sink.conn_id, key)

    image_uuid = await _upload_file(
        _e2e_stack["app"],
        name="first.png",
        mime="image/png",
        payload=_PNG_BYTES,
    )
    pdf_uuid = await _upload_file(
        _e2e_stack["app"],
        name="L11 RL.pdf",
        mime="application/pdf",
        payload=_sample_pdf_bytes(),
    )
    await _send_session_turn(
        ctx=_e2e_stack["ctx"],
        key=key,
        sink=sink,
        message="请同时看看这张图和 PDF",
        attachments=[
            _attachment(image_uuid, mime="image/png", name="first.png"),
            _attachment(pdf_uuid, mime="application/pdf", name="L11 RL.pdf"),
        ],
    )
    await manager.append_message(key, "user", "中间普通文本。")
    await manager.append_message(key, "assistant", "普通回答。")
    gate_provider.text = (
        '{"decision":"text_only","confidence":0.91,"reason":"file edit only"}'
    )
    text_calls_before = len(text_provider.calls)
    vision_calls_before = len(vision_provider.calls)

    await _send_session_turn(
        ctx=_e2e_stack["ctx"],
        key=key,
        sink=sink,
        message="刚才那个 PDF 的 Machine Learning 文字需要遮住。",
    )

    assert len(text_provider.calls) == text_calls_before + 1
    assert len(vision_provider.calls) == vision_calls_before
    sent_messages = text_provider.calls[-1]["messages"]
    assert not any(_message_has_image(message) for message in sent_messages)
    sent_text = _all_provider_text(sent_messages)
    assert "historical attachment available: L11 RL.pdf (application/pdf" in sent_text
    done_events = _event_payloads(sink, "session.event.done")
    assert done_events[-1]["vision_followup_gate_decision"] == "text_only"
    assert done_events[-1]["vision_followup_needs_image"] is False


@pytest.mark.asyncio
async def test_attachment_filename_traversal_is_sanitized(
    _e2e_stack: dict[str, Any],
) -> None:
    manager: SessionManager = _e2e_stack["manager"]
    subscription_manager: SubscriptionManager = _e2e_stack["subscription_manager"]
    sink: _EventSink = _e2e_stack["sink"]
    text_provider: _RecordingProvider = _e2e_stack["text_provider"]
    config: GatewayConfig = _e2e_stack["config"]
    key = "agent:main:pdf-materialization-traversal"
    await manager.create(session_key=key, agent_id="main")
    subscription_manager.subscribe_messages(sink.conn_id, key)

    file_uuid = await _upload_file(
        _e2e_stack["app"],
        name="../../evil.pdf",
        mime="application/pdf",
        payload=_sample_pdf_bytes(),
    )
    await _send_session_turn(
        ctx=_e2e_stack["ctx"],
        key=key,
        sink=sink,
        message="请读取这个 PDF",
        attachments=[_attachment(file_uuid, mime="application/pdf", name="../../evil.pdf")],
    )

    workspace_root = Path(config.workspace_dir or "").resolve()
    workspace_paths = list((workspace_root / ".opensquilla" / "attachments").glob("**/*.pdf"))
    assert len(workspace_paths) == 1
    materialized = workspace_paths[0].resolve()
    materialized.relative_to(workspace_root)
    assert ".." not in materialized.relative_to(workspace_root).as_posix()
    assert materialized.name.endswith("evil.pdf")
    sent_text = _all_provider_text(text_provider.calls[-1]["messages"])
    assert "../" not in sent_text
    assert ".opensquilla/attachments/" in sent_text


@pytest.mark.asyncio
async def test_missing_material_emits_unavailable_marker(
    _e2e_stack: dict[str, Any],
) -> None:
    manager: SessionManager = _e2e_stack["manager"]
    subscription_manager: SubscriptionManager = _e2e_stack["subscription_manager"]
    sink: _EventSink = _e2e_stack["sink"]
    text_provider: _RecordingProvider = _e2e_stack["text_provider"]
    config: GatewayConfig = _e2e_stack["config"]
    key = "agent:main:pdf-materialization-missing"
    session = await manager.create(session_key=key, agent_id="main")
    subscription_manager.subscribe_messages(sink.conn_id, key)

    file_uuid = await _upload_file(
        _e2e_stack["app"],
        name="L11 RL.pdf",
        mime="application/pdf",
        payload=_sample_pdf_bytes(),
    )
    await _send_session_turn(
        ctx=_e2e_stack["ctx"],
        key=key,
        sink=sink,
        message="请读取这个 PDF",
        attachments=[_attachment(file_uuid, mime="application/pdf", name="L11 RL.pdf")],
    )
    transcript = await manager.get_transcript(key)
    persisted = json.loads(transcript[0].content)
    sha = persisted["attachments"][0]["sha256_ref"]
    transcript_material_path(
        Path(config.attachments.media_root or ""),
        session.session_id,
        sha,
    ).unlink()

    calls_before = len(text_provider.calls)
    await _send_session_turn(
        ctx=_e2e_stack["ctx"],
        key=key,
        sink=sink,
        message="刚才那个 PDF 还能编辑吗？",
    )

    assert len(text_provider.calls) == calls_before + 1
    sent_text = _all_provider_text(text_provider.calls[-1]["messages"])
    assert "historical attachment unavailable: L11 RL.pdf (application/pdf)" in sent_text
    assert "historical attachment available: L11 RL.pdf" not in sent_text
