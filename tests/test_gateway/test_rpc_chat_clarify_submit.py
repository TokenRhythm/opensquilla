"""RPC handler tests for chat.clarify_submit (PR5 Task 2).

Verifies:
* The pure helper ``_clarify_fields_to_text`` serializes the form dict
  into the deterministic ``key: value\\n`` form that
  ``opensquilla.skills.meta.clarify_text.parse_clarify_reply`` accepts.
* The RPC handler rejects malformed params with a clear error.
* The RPC handler forwards a normal ``chat.send`` call when given a
  valid submission (handler internals are unit-tested elsewhere).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from opensquilla.gateway import rpc_chat as rpc_chat_module
from opensquilla.gateway.rpc import RpcContext
from opensquilla.gateway.rpc_chat import (
    _clarify_fields_to_text,
    _handle_chat_clarify_submit,
)
from opensquilla.skills.meta.clarify_text import parse_clarify_reply
from opensquilla.skills.meta.types import ClarifyField, ClarifyStepConfig

# ── pure helper ──

def test_clarify_fields_to_text_basic():
    text = _clarify_fields_to_text({
        "destination": "Tokyo",
        "days": 5,
        "party_size": 2,
        "budget": "mid",
    })
    # Order is dict iteration order (preserved in py3.7+).
    assert "destination: Tokyo" in text
    assert "days: 5" in text
    assert "party_size: 2" in text
    assert "budget: mid" in text


def test_clarify_fields_to_text_bool_renders_lowercase():
    text = _clarify_fields_to_text({"enabled": True, "disabled": False})
    assert "enabled: true" in text
    assert "disabled: false" in text


def test_clarify_fields_to_text_skips_empty_and_none():
    text = _clarify_fields_to_text({
        "destination": "Tokyo",
        "notes": "",
        "extra": None,
        "days": 5,
    })
    # Empty/None fields not included (signal "user left it blank").
    assert "notes" not in text
    assert "extra" not in text
    assert "destination: Tokyo" in text
    assert "days: 5" in text


def test_clarify_fields_to_text_roundtrips_through_real_parser():
    """The whole point: the synthetic text must be parseable by
    clarify_text.parse_clarify_reply so meta_resolution's awaiting
    branch picks it up exactly like a hand-typed reply."""
    schema = ClarifyStepConfig(
        mode="form",
        fields=(
            ClarifyField(name="destination", type="string", required=True),
            ClarifyField(name="days", type="int", required=True, min=1, max=14),
            ClarifyField(name="party_size", type="int", required=True),
            ClarifyField(name="budget", type="enum",
                         choices=("budget", "mid", "premium")),
        ),
    )
    text = _clarify_fields_to_text({
        "destination": "Tokyo",
        "days": 5,
        "party_size": 2,
        "budget": "mid",
    })
    parsed, errors = parse_clarify_reply(text, schema, surface="web")
    assert errors == []
    assert parsed == {
        "destination": "Tokyo",
        "days": 5,
        "party_size": 2,
        "budget": "mid",
    }


# ── RPC handler ──

@pytest.mark.asyncio
async def test_clarify_submit_rejects_non_dict_params():
    ctx = RpcContext(conn_id="c", principal=SimpleNamespace(role="operator"))
    with pytest.raises(ValueError, match="sessionKey, fields"):
        await _handle_chat_clarify_submit(None, ctx)
    with pytest.raises(ValueError, match="sessionKey, fields"):
        await _handle_chat_clarify_submit("not-a-dict", ctx)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_clarify_submit_rejects_empty_fields():
    ctx = RpcContext(conn_id="c", principal=SimpleNamespace(role="operator"))
    with pytest.raises(ValueError, match="non-empty mapping"):
        await _handle_chat_clarify_submit(
            {"sessionKey": "S1", "fields": {}}, ctx,
        )
    with pytest.raises(ValueError, match="non-empty mapping"):
        await _handle_chat_clarify_submit(
            {"sessionKey": "S1", "fields": "not a dict"}, ctx,
        )


@pytest.mark.asyncio
async def test_clarify_submit_allows_all_empty_values_for_server_autofill(monkeypatch):
    """Empty form submissions must reach meta_resolution so required fields
    can be inferred from context instead of trapping the user in the form."""
    captured: dict = {}

    async def _fake_send(send_params, ctx):
        captured["send_params"] = send_params
        return {"ok": True, "sessionKey": send_params["sessionKey"]}

    monkeypatch.setattr(
        "opensquilla.gateway.rpc_chat._handle_chat_send",
        _fake_send,
    )

    ctx = RpcContext(conn_id="c", principal=SimpleNamespace(role="operator"))
    result = await _handle_chat_clarify_submit(
        {"sessionKey": "S1", "fields": {"a": "", "b": None}},
        ctx,
    )

    assert result["ok"] is True
    assert captured["send_params"]["message"] == ""
    assert captured["send_params"]["inputProvenance"] == {
        "kind": "clarify_form",
        "source": "webui",
    }


@pytest.mark.asyncio
async def test_clarify_submit_forwards_to_chat_send(monkeypatch):
    """Valid submission: forwards a chat.send call with the serialised
    text + clarify_submit intent + run_id tagged on _source."""
    captured: dict = {}

    async def _fake_send(send_params, ctx):
        captured["send_params"] = send_params
        captured["ctx"] = ctx
        return {"ok": True, "sessionKey": send_params["sessionKey"]}

    monkeypatch.setattr(
        "opensquilla.gateway.rpc_chat._handle_chat_send",
        _fake_send,
    )

    ctx = RpcContext(conn_id="c", principal=SimpleNamespace(role="operator"))
    result = await _handle_chat_clarify_submit(
        {
            "sessionKey": "agent:main:webchat:abc",
            "fields": {"destination": "Tokyo", "days": 5},
            "run_id": "r-xyz",
        },
        ctx,
    )

    assert result["ok"] is True
    sp = captured["send_params"]
    assert "destination: Tokyo" in sp["message"]
    assert "days: 5" in sp["message"]
    # PR7 E2E fix: intent is no longer forwarded — SessionIntent enum
    # rejects unknown values, and meta_resolution's awaiting branch keys
    # off session_key + provenance tag, not intent.
    assert "intent" not in sp
    assert sp["inputProvenance"] == {"kind": "clarify_form", "source": "webui"}
    src = sp["_source"]
    assert src["channel_kind"] == "webchat"
    assert src["clarify_run_id"] == "r-xyz"


@pytest.mark.asyncio
async def test_clarify_submit_with_request_id_resolves_same_turn(monkeypatch):
    captured: dict = {}

    class Runtime:
        async def resolve_user_input(self, **kwargs):
            captured.update(kwargs)
            return {
                "resolved": True,
                "replayed": False,
                "request_id": kwargs["request_id"],
            }

    async def _unexpected_send(*_args, **_kwargs):
        raise AssertionError("deferred input must not create a new chat turn")

    monkeypatch.setattr(
        "opensquilla.gateway.rpc_chat._handle_chat_send",
        _unexpected_send,
    )
    ctx = RpcContext(
        conn_id="c",
        principal=SimpleNamespace(role="operator"),
        task_runtime=Runtime(),
    )

    result = await _handle_chat_clarify_submit(
        {
            "sessionKey": "agent:main:webchat:abc",
            "request_id": "input-1",
            "run_id": "task-1",
            "fields": {"scope": "Core"},
        },
        ctx,
    )

    assert result == {
        "sessionKey": "agent:main:webchat:abc",
        "resolved": True,
        "replayed": False,
        "request_id": "input-1",
    }
    assert captured == {
        "session_key": "agent:main:webchat:abc",
        "request_id": "input-1",
        "fields": {"scope": "Core"},
    }


@pytest.mark.asyncio
async def test_clarify_submit_logs_safe_entry_metadata(monkeypatch):
    """Entry logging proves the Web UI submit reached RPC without
    exposing field values in gateway logs."""
    captured: dict = {}

    async def _fake_send(send_params, ctx):
        return {"ok": True, "sessionKey": send_params["sessionKey"]}

    def _fake_info(event, **kwargs):
        captured["event"] = event
        captured["kwargs"] = kwargs

    monkeypatch.setattr(rpc_chat_module, "_handle_chat_send", _fake_send)
    real_log = rpc_chat_module.log

    class _InfoLog:
        def info(self, event, **kwargs):
            _fake_info(event, **kwargs)

        def __getattr__(self, name):
            return getattr(real_log, name)

    monkeypatch.setattr(rpc_chat_module, "log", _InfoLog())

    ctx = RpcContext(conn_id="c", principal=SimpleNamespace(role="operator"))
    await _handle_chat_clarify_submit(
        {
            "sessionKey": "agent:main:webchat:abc",
            "fields": {"review": "ok"},
            "run_id": "r-xyz",
        },
        ctx,
    )

    assert captured["event"] == "chat.clarify_submit.params"
    assert captured["kwargs"] == {
        "session_key": "agent:main:webchat:abc",
        "field_count": 1,
        "run_id": "r-xyz",
    }


@pytest.mark.asyncio
async def test_clarify_submit_works_without_run_id(monkeypatch):
    """run_id is optional; absent → no _source tag, but submission still
    flows through normally."""
    captured: dict = {}

    async def _fake_send(send_params, ctx):
        captured["send_params"] = send_params
        return {"ok": True, "sessionKey": send_params["sessionKey"]}

    monkeypatch.setattr(
        "opensquilla.gateway.rpc_chat._handle_chat_send",
        _fake_send,
    )

    ctx = RpcContext(conn_id="c", principal=SimpleNamespace(role="operator"))
    await _handle_chat_clarify_submit(
        {"sessionKey": "S1", "fields": {"x": "y"}},
        ctx,
    )
    assert "_source" not in captured["send_params"]
