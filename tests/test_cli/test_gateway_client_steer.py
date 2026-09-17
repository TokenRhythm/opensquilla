from __future__ import annotations

from typing import Any

import pytest

from opensquilla.cli.gateway_client import GatewayClient, GatewayRPCError
from opensquilla.cli.tui.backend.input_identity import tui_input_identity_scope


@pytest.mark.asyncio
async def test_steer_session_prefers_v2_for_known_active_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = GatewayClient()
    client._active_turn_ids["agent:main:test"] = "turn-1"  # noqa: SLF001
    calls: list[tuple[str, dict[str, Any]]] = []

    async def fake_call(method: str, params: dict[str, Any] | None = None) -> Any:
        calls.append((method, dict(params or {})))
        return {
            "accepted": True,
            "turn_id": "turn-1",
            "disposition": "steering",
        }

    monkeypatch.setattr(client, "_call", fake_call)

    with tui_input_identity_scope("message-1"):
        result = await client.steer_session("agent:main:test", "make it shorter")

    assert result["accepted"] is True
    assert calls == [
        (
            "sessions.steer.v2",
            {
                "key": "agent:main:test",
                "message": "make it shorter",
                "expected_turn_id": "turn-1",
                "client_request_id": "steer:message-1",
                "client_message_id": "message-1",
                "surface_id": client.surface_id,
                "_source": {
                    "caller_kind": "cli",
                    "channel_kind": "cli",
                    "channel_id": "cli:chat",
                    "source_kind": "cli",
                    "source_name": "chat",
                    "client_message_id": "message-1",
                    "surface_id": client.surface_id,
                },
            },
        )
    ]


@pytest.mark.asyncio
async def test_steer_session_queues_without_calling_legacy_when_v2_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = GatewayClient()
    client._active_turn_ids["agent:main:test"] = "turn-1"  # noqa: SLF001
    calls: list[str] = []

    async def fake_call(method: str, params: dict[str, Any] | None = None) -> Any:
        calls.append(method)
        if method == "sessions.steer.v2":
            raise GatewayRPCError(
                method,
                code="METHOD_NOT_FOUND",
                message="unknown method",
            )
        raise AssertionError("the unversioned steer endpoint must not be called")

    monkeypatch.setattr(client, "_call", fake_call)

    with tui_input_identity_scope("message-2"):
        result = await client.steer_session("agent:main:test", "change tone")

    assert result["accepted"] is False
    assert result["disposition"] == "queue_only"
    assert result["failure_code"] == "STEER_V2_UNAVAILABLE"
    assert calls == ["sessions.steer.v2"]


@pytest.mark.asyncio
async def test_steer_session_queues_when_active_turn_identity_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = GatewayClient()
    calls: list[str] = []

    async def fake_call(method: str, params: dict[str, Any] | None = None) -> Any:
        calls.append(method)
        return {"accepted": True}

    monkeypatch.setattr(client, "_call", fake_call)

    with tui_input_identity_scope("message-no-turn"):
        result = await client.steer_session("agent:main:test", "change tone")

    assert result["accepted"] is False
    assert result["disposition"] == "queue_only"
    assert calls == []


@pytest.mark.asyncio
async def test_steer_session_does_not_fallback_after_ambiguous_v2_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = GatewayClient()
    client._active_turn_ids["agent:main:test"] = "turn-1"  # noqa: SLF001
    calls: list[str] = []

    async def fake_call(method: str, params: dict[str, Any] | None = None) -> Any:
        calls.append(method)
        raise GatewayRPCError(
            method,
            code="STORAGE_BUSY",
            message="retry later",
            data={"fallback_safe": False},
        )

    monkeypatch.setattr(client, "_call", fake_call)

    with tui_input_identity_scope("message-3"):
        with pytest.raises(GatewayRPCError, match="STORAGE_BUSY"):
            await client.steer_session("agent:main:test", "add citations")

    assert calls == ["sessions.steer.v2"]


@pytest.mark.asyncio
async def test_steer_session_retry_reuses_exact_v2_request_after_turn_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = GatewayClient()
    client._active_turn_ids["agent:main:test"] = "turn-1"  # noqa: SLF001
    calls: list[tuple[str, dict[str, Any]]] = []

    async def fake_call(method: str, params: dict[str, Any] | None = None) -> Any:
        calls.append((method, dict(params or {})))
        if len(calls) == 1:
            raise ConnectionError("reply lost after request write")
        return {
            "accepted": True,
            "replayed": True,
            "turn_id": "turn-1",
            "disposition": "applied",
        }

    monkeypatch.setattr(client, "_call", fake_call)

    with tui_input_identity_scope("message-retry"):
        with pytest.raises(ConnectionError, match="reply lost"):
            await client.steer_session("agent:main:test", "keep this exact text")

    # The originating stream may finish before the TUI can retry. The retained
    # v2 request must still target the original turn rather than falling back
    # to the unversioned method or generating a new idempotency identity.
    client._active_turn_ids.clear()  # noqa: SLF001
    with tui_input_identity_scope("message-retry"):
        result = await client.steer_session("agent:main:test", "keep this exact text")

    assert result["replayed"] is True
    assert [method for method, _params in calls] == [
        "sessions.steer.v2",
        "sessions.steer.v2",
    ]
    assert calls[1][1] == calls[0][1]
    assert calls[1][1]["expected_turn_id"] == "turn-1"
    assert calls[1][1]["client_request_id"] == "steer:message-retry"


@pytest.mark.asyncio
async def test_steer_session_retains_exact_request_for_retryable_v2_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = GatewayClient()
    client._active_turn_ids["agent:main:test"] = "turn-retryable"  # noqa: SLF001
    calls: list[dict[str, Any]] = []

    async def fake_call(method: str, params: dict[str, Any] | None = None) -> Any:
        assert method == "sessions.steer.v2"
        calls.append(dict(params or {}))
        if len(calls) == 1:
            raise GatewayRPCError(
                method,
                code="SESSION_CHANGED",
                message="retry admission",
                data={"fallback_safe": True},
                retryable=True,
            )
        return {"accepted": True, "replayed": False, "turn_id": "turn-retryable"}

    monkeypatch.setattr(client, "_call", fake_call)

    with tui_input_identity_scope("message-retryable"):
        with pytest.raises(GatewayRPCError, match="SESSION_CHANGED"):
            await client.steer_session("agent:main:test", "same message")

    client._active_turn_ids.clear()  # noqa: SLF001
    with tui_input_identity_scope("message-retryable"):
        result = await client.steer_session("agent:main:test", "same message")

    assert result["accepted"] is True
    assert calls[1] == calls[0]
