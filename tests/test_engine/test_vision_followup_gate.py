"""Image context is structural; no auxiliary model selects historical images."""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from opensquilla.engine.runtime import TurnRunner
from opensquilla.gateway.config import GatewayConfig
from opensquilla.provider.types import ChatConfig, Message, StreamEvent, ToolDefinition
from opensquilla.session.attachment_manifest import build_attachment_manifest
from opensquilla.session.manager import SessionManager
from opensquilla.session.storage import SessionStorage
from tests.helpers.image_bytes import image_bytes


class _NoAuxiliaryProvider:
    provider_name = "test-provider"
    model = "test-model"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def chat(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[StreamEvent]:
        self.calls.append({"messages": messages, "tools": tools, "config": config})
        raise AssertionError("Image-context preparation must not call an auxiliary model")

    async def list_models(self) -> list[Any]:
        return []


def _config(**router_overrides: Any) -> GatewayConfig:
    return GatewayConfig(
        llm={"provider": "openrouter", "model": "test-model"},
        squilla_router={"enabled": False, **router_overrides},
    )


async def _prepare(
    message: str,
    *,
    config: GatewayConfig | None = None,
    manager: SessionManager | None = None,
    session_key: str = "agent:main:image-context",
    attachments: list[dict[str, Any]] | None = None,
    **kwargs: Any,
) -> Any:
    provider = _NoAuxiliaryProvider()
    runner = TurnRunner(provider_selector=None, session_manager=manager, config=config or _config())
    turn, resolved = await runner._run_pipeline(
        message=message,
        semantic_message=message,
        session_key=session_key,
        provider=provider,
        cloned_selector=None,
        tool_defs=[],
        base_prompt="Synthetic test prompt.",
        attachments=attachments or [],
        **kwargs,
    )
    assert resolved is provider
    assert provider.calls == []
    assert not any("vision_followup_gate" in key for key in turn.metadata)
    assert turn.raw_message == message
    return turn


def _image() -> dict[str, str]:
    return {"mime": "image/png", "data": base64.b64encode(image_bytes()).decode("ascii")}


@pytest.mark.parametrize(
    "current_image,history_image", [(False, False), (False, True), (True, False), (True, True)]
)
@pytest.mark.parametrize(
    "message",
    [
        "Compare the latest image with the two earlier images.",
        "Ignore only the first image and inspect the second.",
        "Do not inspect any pictures; answer the text question.",
        "不要只看第一张，请比较另外两张。",
        "Ignoring image content, write a short greeting.",
        "A plain text follow-up without attachment references.",
    ],
)
async def test_wording_does_not_select_or_remove_image_context(
    current_image: bool, history_image: bool, message: str
) -> None:
    turn = await _prepare(
        message,
        attachments=[_image()] if current_image else [],
        history_has_recent_image=history_image,
        history_image_turn_count=2 if history_image else 0,
        turns_since_last_image=1 if history_image else None,
    )
    assert turn.metadata["image_context_has_images"] is (current_image or history_image)
    assert not turn.metadata.get("image_intent_attachment_ids")
    assert len(turn.attachments) == int(current_image)


@pytest.mark.parametrize("gate_enabled", [False, True])
async def test_legacy_gate_configuration_is_readable_but_inactive(gate_enabled: bool) -> None:
    config = _config(
        vision_followup_gate_enabled=gate_enabled,
        vision_followup_gate_model="unused-selection-model",
        vision_followup_gate_tier="c0",
        vision_followup_gate_timeout_seconds=0.1,
        vision_followup_gate_max_output_tokens=16,
        vision_followup_gate_fallback_recent_turns=0,
        vision_followup_gate_unknown_policy="text_only",
    )
    restored = GatewayConfig.model_validate(config.model_dump())
    assert restored.squilla_router.vision_followup_gate_enabled is gate_enabled
    assert restored.squilla_router.vision_followup_gate_model == "unused-selection-model"
    turn = await _prepare(
        "Do not use the first image; compare the remaining images.",
        config=restored,
        history_has_recent_image=True,
    )
    assert turn.metadata["image_context_has_images"] is True


@pytest.mark.parametrize("window", [0, 1, 8])
async def test_legacy_history_window_does_not_change_active_image_context(
    window: int,
) -> None:
    turn = await _prepare(
        "What about the picture?",
        config=_config(vision_history_lookback_turns=window),
        history_has_recent_image=True,
    )
    assert turn.metadata["image_context_has_images"] is True


@pytest_asyncio.fixture
async def persisted_image(tmp_path: Path) -> AsyncIterator[tuple[Any, Any, str]]:
    storage = SessionStorage(str(tmp_path / "image-context.db"))
    await storage.connect()
    try:
        manager = SessionManager(storage, inject_time_prefix=False)
        node = await manager.create("agent:main:image-context")
        entry = await manager.append_message(
            node.session_key,
            "user",
            json.dumps({"text": "A synthetic image.", "attachments": [_image()]}),
        )
        manifest = build_attachment_manifest(
            [entry], session_id=node.session_id, session_key=node.session_key
        )
        yield manager, node, manifest.occurrences[0].attachment_id
    finally:
        await storage.close()


@pytest.mark.parametrize(
    "template", ["Describe {id}.", "Do not use {id}.", "只看其他图片，忽略 {id}。"]
)
async def test_free_text_attachment_id_is_left_for_the_model(
    persisted_image: tuple[Any, Any, str], template: str
) -> None:
    manager, node, attachment_id = persisted_image
    turn = await _prepare(
        template.format(id=attachment_id),
        manager=manager,
        session_key=node.session_key,
        expected_session_id=node.session_id,
        expected_session_epoch=node.epoch,
    )
    assert turn.metadata["image_context_has_images"] is False
    assert "image_intent_attachment_ids" not in turn.metadata


async def test_structured_attachment_reference_preserves_exact_id(
    persisted_image: tuple[Any, Any, str],
) -> None:
    manager, node, attachment_id = persisted_image
    turn = await _prepare(
        "Inspect the attached item.",
        manager=manager,
        session_key=node.session_key,
        config=_config(vision_history_lookback_turns=0),
        attachments=[{"resourceRef": {"type": "attachment", "id": attachment_id}}],
        expected_session_id=node.session_id,
        expected_session_epoch=node.epoch,
    )
    assert turn.metadata["image_context_has_images"] is True
    assert turn.metadata["image_intent_attachment_ids"] == [attachment_id]


async def test_unknown_structured_id_does_not_claim_image_context(
    persisted_image: tuple[Any, Any, str],
) -> None:
    manager, node, _ = persisted_image
    turn = await _prepare(
        "Inspect the attached item.",
        manager=manager,
        session_key=node.session_key,
        attachments=[{"resourceRef": {"type": "attachment", "id": "att_missing_12345"}}],
        expected_session_id=node.session_id,
        expected_session_epoch=node.epoch,
    )
    assert turn.metadata["image_context_has_images"] is False
    assert "image_intent_attachment_ids" not in turn.metadata
