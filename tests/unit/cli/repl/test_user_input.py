from __future__ import annotations

import json
from typing import Any

import pytest

from opensquilla.cli.chat.user_input import GatewayUserInput
from opensquilla.cli.gateway_client import GatewayClient, GatewayRPCError


def _request(*, fields: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "kind": "user_input",
        "status": "input_required",
        "request_id": "question-1",
        "run_id": "task-1",
        "clarify_schema": {
            "fields": fields or [
                {"name": "color", "prompt": "Choose a color", "choices": ["red", "blue"]},
            ],
        },
    }


def _client(submit: Any) -> tuple[GatewayUserInput, list[str]]:
    notices: list[str] = []

    async def write(text: str) -> None:
        notices.append(text)

    client = GatewayUserInput(submit=submit, write=write)
    client.reset("agent:main:cli:demo", {})
    return client, notices


@pytest.mark.asyncio
async def test_question_answer_uses_existing_request_rpc(monkeypatch: pytest.MonkeyPatch) -> None:
    client = GatewayClient()
    calls: list[tuple[str, Any]] = []

    async def call(method: str, params: Any) -> dict[str, Any]:
        calls.append((method, params))
        return {"accepted": True}

    monkeypatch.setattr(client, "_call", call)
    questions, notices = _client(client.submit_user_input)
    await questions.observe({
        "event": "session.event.tool_result", "result": json.dumps(_request()),
    })
    assert await questions.answer("2") is True
    assert calls == [("chat.clarify_submit", {
        "sessionKey": "agent:main:cli:demo",
        "request_id": "question-1",
        "fields": {"color": "blue"},
    })]
    assert "Choose a color" in notices[0]
    assert not questions.pending
    await questions.observe({"event": "session.event.tool_result", "result": _request()})
    assert not questions.pending
    assert await questions.answer("ordinary message") is False


@pytest.mark.asyncio
async def test_reconnect_restores_multiple_questions_and_deduplicates_replay() -> None:
    calls: list[dict[str, Any]] = []

    async def submit(_key: str, **kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"accepted": True}

    request = _request(fields=[
        {"name": "color", "prompt": "Choose a color", "choices": ["red", "blue"]},
        {"name": "size", "prompt": "Choose a size", "choices": ["small", "large"]},
    ])
    questions, notices = _client(submit)
    questions.reset("agent:main:cli:demo", {"session": {"pendingUserInputs": [request]}})
    await questions.present()
    assert await questions.answer("1") is True
    await questions.observe({"event": "session.event.tool_result", "result": request})
    assert len(notices) == 2
    assert calls == []
    assert await questions.answer("custom size") is True
    assert calls == [{"request_id": "question-1", "fields": {
        "color": "red", "size": "custom size",
    }}]


@pytest.mark.asyncio
@pytest.mark.parametrize("resolution", ["answered", "terminal", "switch"])
async def test_closed_question_never_consumes_later_chat(resolution: str) -> None:
    async def submit(_key: str, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("closed questions must not be submitted")

    questions, _notices = _client(submit)
    await questions.observe({"event": "session.event.tool_result", "result": _request()})
    if resolution == "answered":
        await questions.observe({"event": "session.event.tool_result", "result": {
            "kind": "user_input", "status": "answered", "request_id": "question-1",
        }})
    elif resolution == "terminal":
        await questions.observe({"event": "session.event.done", "turn_id": "task-1"})
    else:
        questions.reset("agent:main:cli:other", {})
    assert await questions.answer("new task") is False


@pytest.mark.asyncio
async def test_old_session_observer_cannot_reopen_question_after_switch() -> None:
    questions, notices = _client(None)
    questions.reset("agent:main:cli:other", {})
    await questions.observe(
        {"event": "session.event.tool_result", "result": _request()},
        session_key="agent:main:cli:demo",
    )
    assert not questions.pending
    assert notices == []


@pytest.mark.asyncio
async def test_unknown_rpc_outcome_retries_exact_answer_without_new_turn() -> None:
    calls: list[dict[str, Any]] = []

    async def submit(_key: str, **kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        if len(calls) == 1:
            raise TimeoutError("test timeout")
        return {"accepted": True, "replayed": True}

    questions, _notices = _client(submit)
    await questions.observe({"event": "session.event.tool_result", "result": _request()})
    assert await questions.answer("blue") is True
    assert questions.pending
    assert await questions.answer("retry") is True
    assert calls[0] == calls[1]
    assert calls[1]["fields"] == {"color": "blue"}


@pytest.mark.asyncio
async def test_expired_request_does_not_reinterpret_answer_as_new_chat() -> None:
    async def submit(_key: str, **_kwargs: Any) -> dict[str, Any]:
        raise GatewayRPCError("chat.clarify_submit", code="USER_INPUT_EXPIRED", accepted=False)

    questions, notices = _client(submit)
    await questions.observe({"event": "session.event.tool_result", "result": _request()})
    assert await questions.answer("blue") is True
    assert not questions.pending
    assert notices[-1] == "This question is no longer waiting for an answer."


@pytest.mark.asyncio
async def test_question_accepts_path_but_leaves_control_commands_available() -> None:
    calls: list[dict[str, Any]] = []

    async def submit(_key: str, **kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"accepted": True}

    questions, _notices = _client(submit)
    request = _request(fields=[{"name": "path", "prompt": "Which output path?"}])
    await questions.observe({"event": "session.event.tool_result", "result": request})
    assert await questions.answer("/goal pause") is False
    assert await questions.answer("/exit") is False
    assert await questions.answer("/example/output.txt") is True
    assert calls[0]["fields"] == {"path": "/example/output.txt"}


@pytest.mark.asyncio
async def test_invalid_earlier_answer_without_accepted_flag_can_be_corrected() -> None:
    from opensquilla.gateway.user_input_broker import validate_user_input_fields

    request = _request(fields=[
        {"name": "path", "prompt": "Which path?", "type": "string", "required": True},
        {"name": "color", "prompt": "Which color?", "type": "string", "required": True},
    ])
    calls: list[dict[str, Any]] = []

    async def submit(_key: str, **kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        try:
            validate_user_input_fields(request, kwargs["fields"])
        except ValueError as exc:
            # The RPC dispatcher uses this exact shape for validation errors.
            raise GatewayRPCError(
                "chat.clarify_submit", code="INVALID_REQUEST", message=str(exc),
            ) from exc
        return {"accepted": True}

    questions, notices = _client(submit)
    await questions.observe({"event": "session.event.tool_result", "result": request})
    assert await questions.answer("x" * 2001) is True
    assert await questions.answer("blue") is True
    assert questions.pending
    assert notices[-1].startswith("Waiting for your answer (1/2):")
    assert await questions.answer("/example/output.txt") is True
    assert await questions.answer("red") is True
    assert not questions.pending
    assert len(calls) == 2
    assert calls[-1]["fields"] == {"path": "/example/output.txt", "color": "red"}


@pytest.mark.asyncio
@pytest.mark.parametrize("still_pending", [False, True])
async def test_snapshot_reconciles_uncertain_answer(still_pending: bool) -> None:
    calls: list[dict[str, Any]] = []

    async def submit(_key: str, **kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        if len(calls) == 1:
            raise TimeoutError("receipt lost")
        return {"accepted": True, "replayed": True}

    questions, notices = _client(submit)
    request = _request()
    await questions.observe({"event": "session.event.tool_result", "result": request})
    assert await questions.answer("blue") is True
    questions.reset("agent:main:cli:demo", {
        "session": {"pendingUserInputs": [request] if still_pending else []},
    })
    await questions.present()
    if still_pending:
        assert "unconfirmed" in notices[-1]
        assert await questions.answer("retry") is True
        assert calls[0] == calls[1]
    else:
        assert not questions.pending
        await questions.observe({"event": "session.event.tool_result", "result": request})
        assert not questions.pending
        assert len(calls) == 1
