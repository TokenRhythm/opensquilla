"""Wire-contract freeze for the ``onboarding.provider.probe`` RPC payload.

The probe envelope feeds the Web UI credential check during onboarding and any
external control client, so its key names are a public protocol contract (see
CLAUDE.md: public RPC field names are stable). These tests pin today's exact
key set:

- Renaming or removing any frozen key is a contract break and must fail here.
- Adding a key requires deliberately extending the frozen set in this file —
  that friction is the point: wire additions should be a conscious decision.

Everything below drives the real RPC handler against a stubbed httpx transport
(the model-discovery contract test's pattern) — zero network, zero credentials
(tests/conftest.py strips provider keys from the environment; only synthetic
keys appear here).
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from opensquilla.contracts.generated.v4.gateway_contract_registry import (
    GATEWAY_METHOD_CONTRACTS,
)
from opensquilla.gateway import rpc_onboarding
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.rpc import RpcContext
from opensquilla.gateway.scopes import ADMIN_SCOPE, METHOD_SCOPES

# Top-level (and only) envelope. ``latencyMs`` remains the legacy end-to-end
# duration, while ``totalMs`` names that duration explicitly and
# ``firstResponseMs`` records the first non-empty text/reasoning delta. Timing
# stays 0/null when the probe never reached the network.
PROBE_ENVELOPE_KEYS = frozenset(
    {
        "ok",
        "providerId",
        "model",
        "failureKind",
        "message",
        "code",
        "latencyMs",
        "firstResponseMs",
        "totalMs",
        "verificationLevel",
        "failureStage",
    }
)


def _sse_ok_body() -> bytes:
    chunks = [
        {"choices": [{"delta": {"content": "pong"}, "finish_reason": None}]},
        {
            "choices": [{"delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        },
    ]
    body = b"".join(f"data: {json.dumps(chunk)}\n\n".encode() for chunk in chunks)
    return body + b"data: [DONE]\n\n"


def _patch_probe_response(monkeypatch: Any, response: httpx.Response) -> None:
    transport = httpx.MockTransport(lambda request: response)
    real_async_client = httpx.AsyncClient

    def patched_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("opensquilla.provider.openai.httpx.AsyncClient", patched_async_client)


def _ok_probe_response() -> httpx.Response:
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=_sse_ok_body(),
    )


def _ctx(tmp_path: Any) -> RpcContext:
    # config_path points at a nonexistent tmp file so the handler never reads
    # the developer's real ~/.opensquilla config.
    return RpcContext(
        conn_id="contract",
        config=GatewayConfig(config_path=str(tmp_path / "opensquilla.toml")),
    )


async def test_probe_envelope_keys_are_frozen_on_ok_path(tmp_path, monkeypatch: Any) -> None:
    _patch_probe_response(monkeypatch, _ok_probe_response())

    payload = await rpc_onboarding._provider_probe(
        {"providerId": "openai", "model": "gpt-4o", "apiKey": "sk-test"}, _ctx(tmp_path)
    )

    assert set(payload) == PROBE_ENVELOPE_KEYS
    # Field-name mapping is part of the contract: clients index into these
    # camelCase names literally.
    assert payload["ok"] is True
    assert payload["providerId"] == "openai"
    assert payload["model"] == "gpt-4o"
    assert payload["failureKind"] == ""
    assert payload["message"] == ""
    assert payload["code"] == ""
    # A mocked transport can complete in under a millisecond, so only the
    # type and sign are pinned — never a wall-clock magnitude.
    assert isinstance(payload["latencyMs"], int)
    assert payload["latencyMs"] >= 0
    assert isinstance(payload["firstResponseMs"], int)
    assert payload["firstResponseMs"] >= 0
    assert payload["totalMs"] == payload["latencyMs"]
    assert payload["verificationLevel"] == "model_verified"
    assert payload["failureStage"] == "model"


async def test_probe_envelope_keys_are_frozen_on_classified_failure(
    tmp_path, monkeypatch: Any
) -> None:
    _patch_probe_response(
        monkeypatch,
        httpx.Response(
            401,
            headers={"content-type": "application/json"},
            content=b'{"error": {"message": "Incorrect API key provided"}}',
        ),
    )

    payload = await rpc_onboarding._provider_probe(
        {"providerId": "openai", "model": "gpt-4o", "apiKey": "sk-bad"}, _ctx(tmp_path)
    )

    assert set(payload) == PROBE_ENVELOPE_KEYS
    assert payload["ok"] is False
    assert payload["failureKind"] == "auth_invalid"
    assert payload["code"] == "401"
    assert isinstance(payload["latencyMs"], int)
    assert payload["latencyMs"] >= 0
    assert payload["firstResponseMs"] is None
    assert payload["totalMs"] == payload["latencyMs"]
    assert payload["verificationLevel"] == "none"
    assert payload["failureStage"] == "model"


async def test_probe_latency_is_zero_when_network_never_reached(
    tmp_path, monkeypatch: Any
) -> None:
    # No explicit key and no env key → the probe short-circuits before any
    # provider is built; latencyMs must not pretend a round-trip happened.
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    payload = await rpc_onboarding._provider_probe(
        {"providerId": "openai", "model": "gpt-4o"}, _ctx(tmp_path)
    )

    assert set(payload) == PROBE_ENVELOPE_KEYS
    assert payload["ok"] is False
    assert payload["failureKind"] == "auth_invalid"
    assert payload["latencyMs"] == 0
    assert payload["firstResponseMs"] is None
    assert payload["totalMs"] == 0
    assert payload["verificationLevel"] == "none"
    assert payload["failureStage"] == "model"


async def test_reachability_mode_is_an_additive_probe_contract(
    tmp_path, monkeypatch: Any
) -> None:
    _patch_probe_response(
        monkeypatch,
        httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=b'{"data":[{"id":"gpt-4o","object":"model"}]}',
        ),
    )

    payload = await rpc_onboarding._provider_probe(
        {
            "providerId": "openai",
            "model": "gpt-4o",
            "apiKey": "sk-test",
            "mode": "reachability",
        },
        _ctx(tmp_path),
    )

    assert set(payload) == PROBE_ENVELOPE_KEYS
    assert payload["ok"] is True
    assert payload["verificationLevel"] == "reachable"
    assert payload["failureStage"] == "reachability"
    assert payload["firstResponseMs"] is None


def test_probe_method_is_admin_scoped() -> None:
    # Frozen on purpose: the probe accepts candidate credentials in params
    # (like onboarding.models.discover), so it must never drop below admin.
    assert METHOD_SCOPES["onboarding.provider.probe"] == ADMIN_SCOPE


@pytest.mark.parametrize(
    ("params", "valid"),
    [
        ({"providerId": "openai", "model": "gpt-4o"}, True),
        ({"providerId": "openai", "model": ""}, True),
        ({"providerId": "openai", "model": None}, True),
        ({"providerId": "openai", "model": None, "mode": None}, True),
        ({"providerId": "openai", "model": "", "mode": "model"}, True),
        ({"providerId": "openai", "mode": "reachability"}, True),
        ({"providerId": "openai", "model": "gpt-4o", "mode": "reachability"}, True),
        ({"providerId": "openai"}, False),
        ({"providerId": "openai", "mode": None}, False),
        ({"providerId": "openai", "mode": "model"}, False),
    ],
)
def test_draft_probe_contract_only_allows_reachability_to_omit_model(
    params: dict[str, object],
    valid: bool,
) -> None:
    descriptor = GATEWAY_METHOD_CONTRACTS["onboarding.llmProfile.draft.probe"]

    if valid:
        descriptor.params_model.model_validate(params)
        descriptor.request_model.model_validate(
            {
                "type": "req",
                "id": "draft-probe-contract",
                "method": "onboarding.llmProfile.draft.probe",
                "params": params,
            }
        )
        return

    with pytest.raises(ValidationError):
        descriptor.params_model.model_validate(params)
    with pytest.raises(ValidationError):
        descriptor.request_model.model_validate(
            {
                "type": "req",
                "id": "draft-probe-contract",
                "method": "onboarding.llmProfile.draft.probe",
                "params": params,
            }
        )
