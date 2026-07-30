"""Offline contracts for the opt-in mixed-provider live Gateway harness."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest

from opensquilla.gateway.config import GatewayConfig
from opensquilla.onboarding.mutations import upsert_llm_profile
from opensquilla.provider.deployment import resolve_provider_deployment


def _load_script():
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "live_mixed_provider_gateway.py"
    spec = importlib.util.spec_from_file_location("live_mixed_provider_gateway", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolves postponed annotations through sys.modules.
    import sys

    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


live = _load_script()


def _load_cross_provider_entrypoint():
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "live_cross_provider_tiers.py"
    spec = importlib.util.spec_from_file_location("live_cross_provider_tiers", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _target(provider: str, model: str):
    return live.parse_target(f"{provider}={model}")


def test_parse_target_uses_registry_endpoint_and_rejects_premium_models() -> None:
    target = _target("deepseek", "deepseek-chat")
    assert target.provider == "deepseek"
    assert target.model == "deepseek-chat"
    assert target.env_key == "DEEPSEEK_API_KEY"
    assert target.endpoint == live.registry_endpoint("deepseek")

    with pytest.raises(ValueError, match="premium model rejected"):
        _target("openai", "gpt-5.4-pro")
    with pytest.raises(ValueError, match="premium model rejected"):
        _target("openrouter", "anthropic/claude-opus-4.8")
    for model in (
        "gpt-4o",
        "gpt-4.1",
        "gpt-5",
        "o1-mini",
        "o3",
        "o4-mini",
        "anthropic/claude-sonnet-4.5",
        "gemini-3.1-pro-preview",
        "qwen3.7-max",
    ):
        with pytest.raises(ValueError, match="premium model rejected"):
            _target("openrouter", model)
    with pytest.raises(ValueError, match="provider=model"):
        live.parse_target("deepseek-chat")


def test_normalize_targets_rejects_duplicate_provider() -> None:
    with pytest.raises(ValueError, match="supplied more than once"):
        live.normalize_targets(["openai=gpt-4.1-mini", "openai=gpt-4.1-nano"])


def test_credential_preflight_blocks_missing_key_without_network() -> None:
    targets = [_target("deepseek", "deepseek-chat"), _target("openai", "gpt-4.1-mini")]
    ready, blocked = live.credential_preflight(
        targets,
        {"DEEPSEEK_API_KEY": "synthetic-deepseek-key"},
    )
    assert [target.provider for target in ready] == ["deepseek"]
    assert blocked == [
        {
            "provider": "openai",
            "model": "gpt-4.1-mini",
            "ok": False,
            "failure_class": "missing-credential",
            "blocked_before_gateway": True,
            "network_requests": 0,
        }
    ]


def test_run_matrix_missing_key_returns_incomplete_before_gateway(monkeypatch) -> None:
    target = _target("openai", "gpt-4.1-mini")

    def forbidden_run(*args, **kwargs):  # pragma: no cover - assertion helper
        raise AssertionError("Gateway must not start when preflight fails")

    monkeypatch.setattr(live.asyncio, "run", forbidden_run)
    report = live.run_live_matrix(targets=[target], secrets={})
    assert report["ok"] is False
    assert report["status"] == "incomplete"
    assert report["preflight"]["blocked"][0]["network_requests"] == 0


def test_gateway_environment_contains_only_selected_file_credentials() -> None:
    deepseek = _target("deepseek", "deepseek-chat")
    openai = _target("openai", "gpt-4.1-mini")
    env = live.build_gateway_environment(
        [deepseek, openai],
        {
            "DEEPSEEK_API_KEY": "synthetic-ds",
            "OPENAI_API_KEY": "synthetic-oa",
            "GEMINI_API_KEY": "synthetic-gemini-must-not-pass",
        },
        base_environment={
            "PATH": os.environ.get("PATH", ""),
            "TMPDIR": "/private/var/synthetic-temp",
            "HOME": "/must/not/pass",
            "HTTPS_PROXY": "http://proxy.invalid",
            "ALL_PROXY": "socks5://proxy.invalid",
            "NO_PROXY": "*",
            "GITHUB_TOKEN": "unrelated-token",
            "OPENSQUILLA_UNRELATED_OVERRIDE": "must-not-pass",
            "GEMINI_API_KEY": "ambient-gemini-must-not-pass",
            "OPENAI_BASE_URL": "https://override.invalid/v1",
            "OPENSQUILLA_LLM_API_KEY": "ambient-primary-must-not-pass",
        },
    )
    assert env["DEEPSEEK_API_KEY"] == "synthetic-ds"
    assert env["OPENAI_API_KEY"] == "synthetic-oa"
    assert env["TMPDIR"] == "/private/var/synthetic-temp"
    assert "GEMINI_API_KEY" not in env
    assert "OPENAI_BASE_URL" not in env
    assert "OPENSQUILLA_LLM_API_KEY" not in env
    assert "HOME" not in env
    assert "HTTPS_PROXY" not in env
    assert "ALL_PROXY" not in env
    assert "NO_PROXY" not in env
    assert "GITHUB_TOKEN" not in env
    assert "OPENSQUILLA_UNRELATED_OVERRIDE" not in env
    assert live.MISSING_ENV_SENTINEL not in env
    assert env["OPENSQUILLA_LIVE_DISABLE_DOTENV"] == "1"


def test_render_config_references_env_names_and_official_endpoints_not_secrets() -> None:
    targets = [_target("deepseek", "deepseek-chat"), _target("openai", "gpt-4.1-mini")]
    text = live.render_gateway_config(targets, router_max_tokens=64)
    assert "synthetic-secret" not in text
    assert 'api_key_env = "DEEPSEEK_API_KEY"' in text
    assert 'api_key_env = "OPENAI_API_KEY"' in text
    assert live.registry_endpoint("deepseek") in text
    assert live.registry_endpoint("openai") in text
    assert "api_key =" not in text
    assert "max_tokens = 64" in text
    assert 'thinking = "off"' in text
    assert "agent_max_provider_retries = 0" in text
    assert "disable_network_observability = true" in text
    assert '[tools]\nprofile = "minimal"\ndeny = ["*"]' in text

    with pytest.raises(ValueError, match="32..64"):
        live.render_gateway_config(targets, router_max_tokens=65)


def test_rotating_triples_cover_every_provider_and_reuse_earliest() -> None:
    targets = [
        _target("deepseek", "deepseek-chat"),
        _target("openai", "gpt-4.1-mini"),
        _target("gemini", "gemini-2.5-flash-lite"),
        _target("dashscope", "qwen3.6-flash"),
        _target("zhipu", "glm-4.5-flash"),
    ]
    groups = live.rotating_triples(targets)
    assert [[target.provider for target in group] for group in groups] == [
        ["deepseek", "openai", "gemini"],
        ["dashscope", "zhipu", "deepseek"],
    ]
    assert {target.provider for group in groups for target in group} == {
        target.provider for target in targets
    }


def test_payloads_preserve_full_provider_model_identity_and_force_one_tier() -> None:
    group = (
        _target("deepseek", "deepseek-chat"),
        _target("openai", "gpt-4.1-mini"),
        _target("gemini", "gemini-2.5-flash-lite"),
    )
    router = live.forced_router_payload(group[0], forced_tier="c2")
    assert router["crossProviderTiers"] is True
    assert router["tierProviderMismatch"] == "veto"
    assert router["defaultTier"] == "c2"
    assert router["tiers"]["c2"]["imageOnly"] is False
    assert all(router["tiers"][tier]["imageOnly"] is True for tier in ("c0", "c1", "c3"))

    ensemble = live.ensemble_payload(group)
    assert [(row["provider"], row["model"], row["role"]) for row in ensemble["candidates"]] == [
        ("deepseek", "deepseek-chat", "primary"),
        ("openai", "gpt-4.1-mini", "contrast"),
        ("gemini", "gemini-2.5-flash-lite", "aggregator"),
    ]
    negative = live.ensemble_payload(group, bad_proposer=True, bad_aggregator=True)
    assert negative["candidates"][1]["model"] == live.BAD_MODEL_ID
    assert negative["candidates"][2]["model"] == live.BAD_MODEL_ID
    assert negative["minSuccessfulProposers"] == 1


def test_private_provider_state_detection_is_recursive() -> None:
    assert live._contains_private_provider_state(  # noqa: SLF001
        {"messages": [{"content": [{"signature": "synthetic-signature"}]}]}
    )


def test_turn_call_observation_extracts_request_response_and_member_evidence() -> None:
    target = _target("deepseek", "deepseek-chat")
    prior_marker = "FIRST_ASSISTANT_MARKER"
    records = [
        {
            "session_key": "s",
            "kind": "llm_request",
            "provider": target.provider,
            "model": target.model,
            "payload": {
                "messages": [{"role": "assistant", "content": prior_marker}],
                "config": {"model": target.model},
            },
        },
        {
            "session_key": "s",
            "kind": "llm_response",
            "provider": target.provider,
            "model": target.model,
            "payload": {
                "duration_ms": 17,
                "usage": {
                    "model": target.model,
                    "input_tokens": 8,
                    "output_tokens": 2,
                    "billed_cost": 0.001,
                    "model_usage_breakdown": [
                        {
                            **target.public(),
                            "role": "primary",
                            "input_tokens": 8,
                            "output_tokens": 2,
                            "elapsed_ms": 17,
                            "billed_cost": 0.001,
                        }
                    ],
                },
            },
        },
    ]

    observation = live._observation_from_records(  # noqa: SLF001
        records=records,
        session_key="s",
        marker="DONE",
        assistant_text="DONE",
        error=None,
        decision={},
        expected_prior_marker=prior_marker,
    )

    assert observation["request_identity"] == target.public()
    assert observation["request_identities"] == [target.public()]
    assert observation["response_identity"] == target.public()
    assert observation["prior_assistant_context_present"] is True
    assert observation["tools_empty"] is True
    assert observation["latency_ms"] == 17
    assert observation["cost"] == {"billed_cost_usd": 0.001}
    assert observation["usage_breakdown"][0]["usage"]["input_tokens"] == 8
    assert observation["usage_breakdown"][0]["latency_ms"] == 17


def test_turn_call_observation_rejects_nonempty_tools() -> None:
    target = _target("deepseek", "deepseek-chat")
    observation = live._observation_from_records(  # noqa: SLF001
        records=[
            {
                "session_key": "s",
                "kind": "llm_request",
                "provider": target.provider,
                "model": target.model,
                "payload": {
                    "tools": [{"name": "must-not-be-present"}],
                    "config": {"model": target.model},
                },
            }
        ],
        session_key="s",
        marker="DONE",
        assistant_text="DONE",
        error=None,
        decision={},
    )

    assert observation["tools_empty"] is False


@pytest.mark.asyncio
async def test_second_turn_uses_continue_and_waits_for_prior_assistant(monkeypatch) -> None:
    calls: dict[str, object] = {}

    def fake_post(_url, payload, _timeout):
        calls["intent"] = payload["intent"]
        return {"ok": True}

    def fake_wait(**kwargs):
        calls["previous_assistant_count"] = kwargs["previous_assistant_count"]
        return ({"text": "SECOND"}, {"messages": []}, None)

    monkeypatch.setattr(live, "_post_json", fake_post)
    monkeypatch.setattr(live, "_wait_for_assistant_reply", fake_wait)
    monkeypatch.setattr(
        live,
        "_read_turn_call_records",
        lambda _path: [
            {
                "session_key": "same-session",
                "kind": "llm_request",
                "provider": "openai",
                "model": "gpt-4.1-mini",
                "payload": {
                    "messages": [{"role": "assistant", "content": "FIRST"}],
                    "tools": [],
                    "config": {"model": "gpt-4.1-mini"},
                },
            }
        ],
    )

    async def fake_rpc(_port, _method, _params):
        return {"decisions": []}

    monkeypatch.setattr(live, "_rpc_call", fake_rpc)
    result = await live._send_turn(  # noqa: SLF001
        port=1,
        turn_log_dir=Path("unused"),
        session_key="same-session",
        marker="SECOND",
        timeout_seconds=1,
        previous_assistant_count=1,
        intent="continue",
        expected_prior_marker="FIRST",
    )

    assert calls == {"intent": "continue", "previous_assistant_count": 1}
    assert result["prior_assistant_context_present"] is True
    assert not live._contains_private_provider_state(  # noqa: SLF001
        {"messages": [{"role": "assistant", "content": "safe history"}]}
    )


def test_router_observation_requires_requested_and_executed_identity() -> None:
    target = _target("deepseek", "deepseek-chat")
    observation = {
        "marker_present": True,
        "usage": {"input_tokens": 4, "output_tokens": 1, "reasoning_tokens": 0},
        "decision": {
            "finalTier": "c2",
            "requestedProvider": "deepseek",
            "requestedModel": "deepseek-chat",
            "executedProvider": "deepseek",
            "executedModel": "deepseek-chat",
            "fallbackHops": 0,
        },
        "private_provider_state_replayed": False,
        "prior_assistant_context_present": True,
        "tools_empty": True,
        "request_identity": {"provider": "deepseek", "model": "deepseek-chat"},
        "response_identity": {"provider": "deepseek", "model": "deepseek-chat"},
        "response_model": "deepseek-chat",
        "latency_ms": 8,
        "cost": {"billed_cost_usd": 0.0},
        "error": "",
    }
    assert live.validate_router_observation(observation, target, expected_tier="c2") == (
        True,
        [],
    )

    observation["decision"] = {**observation["decision"], "executedProvider": "openai"}
    ok, failures = live.validate_router_observation(observation, target, expected_tier="c2")
    assert ok is False
    assert "executed_provider_mismatch" in failures


def test_router_accepts_provider_reported_deepseek_alias_but_keeps_request_strict() -> None:
    target = _target("deepseek", "deepseek-chat")
    observation = {
        "marker_present": True,
        "usage": {"input_tokens": 4, "output_tokens": 1, "reasoning_tokens": 0},
        "decision": {
            "finalTier": "c0",
            "requestedProvider": target.provider,
            "requestedModel": target.model,
            "executedProvider": target.provider,
            "executedModel": target.model,
            "fallbackHops": 0,
        },
        "private_provider_state_replayed": False,
        "prior_assistant_context_present": True,
        "tools_empty": True,
        "request_identity": target.public(),
        "response_identity": {"provider": "deepseek", "model": "deepseek-v4-flash"},
        "response_model": "deepseek-v4-flash",
        "latency_ms": 8,
        "cost": {"billed_cost_usd": 0.0},
        "error": "",
    }

    assert live.validate_router_observation(observation, target, expected_tier="c0") == (
        True,
        [],
    )

    observation["request_identity"] = {
        "provider": "deepseek",
        "model": "deepseek-v4-flash",
    }
    ok, failures = live.validate_router_observation(observation, target, expected_tier="c0")
    assert ok is False
    assert "request_model_mismatch" in failures


def test_provider_reported_alias_never_crosses_provider_boundary() -> None:
    target = _target("openai", "deepseek-chat")

    assert not live.provider_response_model_matches(
        target.provider,
        target.model,
        "deepseek-v4-flash",
    )


def test_google_invalid_key_wording_is_classified_as_auth() -> None:
    assert (
        live.classify_failure(
            'HTTP 400: {"error":{"message":"Please pass a valid API key"}}'
        )
        == "auth"
    )


def test_fail_closed_audit_rejects_any_earlier_foreign_model_request() -> None:
    primary = _target("deepseek", "deepseek-chat")
    observation = {
        "request_identities": [
            {"provider": primary.provider, "model": "foreign/model"},
            primary.public(),
        ]
    }

    all_primary, foreign_sent, identities = live._fail_closed_request_audit(  # noqa: SLF001
        observation,
        primary=primary,
        foreign_model="foreign/model",
    )

    assert all_primary is False
    assert foreign_sent is True
    assert identities == observation["request_identities"]


def test_router_phase_acceptance_gates_state_isolation_and_fail_closed() -> None:
    rows = [{"ok": True} for _ in range(live.MIN_ROUTER_PROVIDERS)]
    assert live._router_phase_ok(  # noqa: SLF001
        rows=rows,
        target_count=len(rows),
        state_switch={"ok": True},
        fail_closed={"ok": True},
    )
    assert not live._router_phase_ok(  # noqa: SLF001
        rows=rows,
        target_count=len(rows),
        state_switch={"ok": False},
        fail_closed={"ok": True},
    )
    assert not live._router_phase_ok(  # noqa: SLF001
        rows=rows,
        target_count=len(rows),
        state_switch={"ok": True},
        fail_closed={"ok": False},
    )


def test_legacy_cross_provider_entrypoint_delegates_to_safe_matrix(monkeypatch) -> None:
    legacy = _load_cross_provider_entrypoint()
    captured: dict[str, object] = {}

    def fake_safe_main(argv):
        captured["argv"] = argv
        return 7

    monkeypatch.setattr(legacy, "_run_safe_mixed_provider_matrix", fake_safe_main)
    argv = ["--secrets-file", "keys", "--target", "deepseek=deepseek-chat"]

    assert legacy.main(argv) == 7
    assert captured["argv"] is argv


def test_ensemble_observation_requires_progress_identity_breakdown_and_no_fallback() -> None:
    group = (
        _target("deepseek", "deepseek-chat"),
        _target("openai", "gpt-4.1-mini"),
        _target("gemini", "gemini-2.5-flash-lite"),
    )
    observation = {
        "marker_present": True,
        "usage": {"input_tokens": 12, "output_tokens": 3, "reasoning_tokens": 0},
        "error": "",
        "response_model": "gemini-2.5-flash-lite",
        "tools_empty": True,
        "latency_ms": 15,
        "cost": {"billed_cost_usd": 0.0},
        "ensemble": {
            "successful_proposers": 2,
            "fallback_used": False,
            "llm_request_count": 3,
            "candidates": [
                {
                    "provider": group[0].provider,
                    "model": "deepseek-v4-flash",
                    "ok": True,
                    "usage": {"input_tokens": 4, "output_tokens": 1},
                    "latency_ms": 4,
                    "cost": {"billed_cost_usd": 0.0},
                },
                {
                    **group[1].public(),
                    "ok": True,
                    "usage": {"input_tokens": 4, "output_tokens": 1},
                    "latency_ms": 5,
                    "cost": {"billed_cost_usd": 0.0},
                },
            ],
            "aggregator": {
                **group[2].public(),
                "role": "aggregator",
                "usage": {"input_tokens": 4, "output_tokens": 1},
                "latency_ms": 6,
                "cost": {"billed_cost_usd": 0.0},
            },
        },
        "usage_breakdown": [
            {
                "provider": target.provider,
                "model": (
                    "deepseek-v4-flash" if target.provider == "deepseek" else target.model
                ),
                "usage": {"input_tokens": 4, "output_tokens": 1},
                "latency_ms": 4,
                "cost": {"billed_cost_usd": 0.0},
            }
            for target in group
        ],
    }
    assert live.validate_ensemble_observation(observation, group) == (True, [])

    observation["ensemble"] = {**observation["ensemble"], "fallback_used": True}
    ok, failures = live.validate_ensemble_observation(observation, group)
    assert ok is False
    assert "fallback_used" in failures


def test_ensemble_accepts_deepseek_alias_only_in_provider_reported_fields() -> None:
    group = (
        _target("openai", "gpt-4.1-mini"),
        _target("gemini", "gemini-2.5-flash-lite"),
        _target("deepseek", "deepseek-chat"),
    )
    observation = {
        "marker_present": True,
        "usage": {"input_tokens": 12, "output_tokens": 3, "reasoning_tokens": 0},
        "error": "",
        "response_model": "deepseek-v4-flash",
        "tools_empty": True,
        "latency_ms": 15,
        "cost": {"billed_cost_usd": 0.0},
        "ensemble": {
            "successful_proposers": 2,
            "fallback_used": False,
            "llm_request_count": 3,
            "candidates": [
                {
                    **target.public(),
                    "ok": True,
                    "usage": {"input_tokens": 4, "output_tokens": 1},
                    "latency_ms": 4,
                    "cost": {"billed_cost_usd": 0.0},
                }
                for target in group[:2]
            ],
            # Execution identity remains the configured target, not the
            # provider-reported concrete alias.
            "aggregator": {
                **group[2].public(),
                "role": "aggregator",
                "usage": {"input_tokens": 4, "output_tokens": 1},
                "latency_ms": 6,
                "cost": {"billed_cost_usd": 0.0},
            },
        },
        "usage_breakdown": [
            {
                **target.public(),
                "usage": {"input_tokens": 4, "output_tokens": 1},
                "latency_ms": 4,
                "cost": {"billed_cost_usd": 0.0},
            }
            for target in group[:2]
        ]
        + [
            {
                "provider": "deepseek",
                "model": "deepseek-v4-flash",
                "usage": {"input_tokens": 4, "output_tokens": 1},
                "latency_ms": 6,
                "cost": {"billed_cost_usd": 0.0},
            }
        ],
    }

    assert live.validate_ensemble_observation(observation, group) == (True, [])


def test_bad_proposer_validation_accepts_quorum_continuation_with_omitted_tools() -> None:
    group = (
        _target("deepseek", "deepseek-chat"),
        _target("openai", "gpt-4.1-mini"),
        _target("gemini", "gemini-2.5-flash-lite"),
    )
    observation = {
        "marker_present": True,
        "error": "",
        "tools_empty": True,
        "usage": {"input_tokens": 12, "output_tokens": 3},
        "ensemble": {
            "successful_proposers": 1,
            "total_candidates": 2,
            "llm_request_count": 3,
            "fallback_used": False,
            "candidates": [
                {
                    "provider": "deepseek",
                    "model": "deepseek-v4-flash",
                    "ok": True,
                },
                {
                    "provider": "openai",
                    "model": live.BAD_MODEL_ID,
                    "ok": False,
                },
            ],
            "aggregator": group[2].public(),
        },
    }

    assert live._bad_proposer_quorum_ok(observation, group)  # noqa: SLF001
    observation["ensemble"] = {**observation["ensemble"], "llm_request_count": 2}
    assert not live._bad_proposer_quorum_ok(observation, group)  # noqa: SLF001


@pytest.mark.asyncio
async def test_missing_profile_runtime_guard_restores_profile(monkeypatch) -> None:
    target = _target("openai", "gpt-4.1-mini")
    calls: list[tuple[str, dict]] = []

    async def fake_rpc(port: int, method: str, params: dict):
        calls.append((method, params))
        if method == "onboarding.llmProfile.probe":
            raise live.GatewayRPCError(
                method,
                code="onboarding.llmProfile.invalid",
                message="provider profile is not executable: credential_pool_exhausted",
            )
        return {}

    monkeypatch.setattr(live, "_rpc_call", fake_rpc)
    result = await live._missing_profile_runtime_guard(port=12345, target=target)  # noqa: SLF001
    assert result["ok"] is True
    assert result["blocked_before_probe_adapter"] is True
    assert [method for method, _params in calls] == [
        "onboarding.llmProfile.upsert",
        "onboarding.llmProfile.probe",
        "onboarding.llmProfile.upsert",
    ]
    assert calls[0][1]["apiKeyEnvPool"] == [live.MISSING_ENV_SENTINEL]
    assert calls[0][1]["baseUrl"] == live.MISSING_CREDENTIAL_BASE_URL
    assert calls[-1][1]["apiKeyEnv"] == "OPENAI_API_KEY"
    assert calls[-1][1]["apiKeyEnvPool"] == []
    assert calls[-1][1]["baseUrl"] == target.endpoint


def test_missing_credential_profile_is_cross_origin_and_contains_no_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _target("deepseek", "deepseek-chat")
    payload = live._missing_credential_profile_payload(target)  # noqa: SLF001
    assert payload == {
        "providerId": "deepseek",
        "apiKeyEnv": "",
        "apiKeyEnvPool": [live.MISSING_ENV_SENTINEL],
        "baseUrl": live.MISSING_CREDENTIAL_BASE_URL,
    }
    monkeypatch.setenv(target.env_key, "synthetic-registry-key-must-not-follow")
    config = upsert_llm_profile(
        GatewayConfig(),
        provider_id=target.provider,
        api_key_env=payload["apiKeyEnv"],
        api_key_env_pool=payload["apiKeyEnvPool"],
        base_url=payload["baseUrl"],
    ).config

    resolution = resolve_provider_deployment(config, target.provider, target.model)
    assert resolution.ready is False
    assert resolution.reason == "missing_credential"
    assert resolution.provider_config is not None
    assert resolution.provider_config.api_key == ""


def test_safe_report_redacts_values_and_refuses_secret_in_mapping_key(tmp_path: Path) -> None:
    output = tmp_path / "report.json"
    secrets = {"OPENAI_API_KEY": "synthetic-secret-value"}
    live._write_safe_report(  # noqa: SLF001
        output,
        {"error": "Authorization: Bearer synthetic-secret-value"},
        secrets,
    )
    assert "synthetic-secret-value" not in output.read_text()
    if os.name != "nt":
        assert output.stat().st_mode & 0o777 == 0o600

    with pytest.raises(RuntimeError, match="provider credentials"):
        live._write_safe_report(  # noqa: SLF001
            output,
            {"synthetic-secret-value": "leaked-as-key"},
            secrets,
        )
    assert not output.exists()


def test_cli_reuses_full_inventory_hygiene_and_atomic_writer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "provider.keys"
    output = tmp_path / "mixed.json"
    source.write_text(
        "broken line\nDEEPSEEK_API_KEY=synthetic-secret\n"
        "DEEPSEEK_BASE_URL=https://ignored.invalid/v1\n",
        encoding="utf-8",
    )
    source.chmod(0o644)

    def fake_run(**kwargs):
        assert kwargs["secrets"] == {"DEEPSEEK_API_KEY": "synthetic-secret"}
        return {
            "ok": True,
            "status": "passed",
            "preflight": {
                "ready": [{"provider": "deepseek", "model": "deepseek-chat"}],
                "blocked": [],
            },
            "router": {
                "targets": [
                    {
                        "provider": "deepseek",
                        "model": "deepseek-chat",
                        "ok": True,
                        "decision": {"sessionKey": "must-not-report"},
                        "usage": {"input_tokens": 2, "output_tokens": 1},
                        "cost": {"billed_cost_usd": 0.0},
                        "latency_ms": 7,
                    }
                ]
            },
        }

    monkeypatch.setattr(live, "run_live_matrix", fake_run)
    assert (
        live.main(
            [
                "--secrets-file",
                str(source),
                "--target",
                "deepseek=deepseek-chat",
                "--output",
                str(output),
            ]
        )
        == 0
    )

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert isinstance(payload, list) and len(payload) == 1
    assert set(payload[0]) == live._PUBLIC_RESULT_KEYS  # noqa: SLF001
    assert payload[0]["provider"] == "deepseek"
    assert "sessionKey" not in json.dumps(payload)
    assert "synthetic-secret" not in output.read_text(encoding="utf-8")
    assert "ignored.invalid" not in output.read_text(encoding="utf-8")
    if os.name != "nt":
        assert output.stat().st_mode & 0o777 == 0o600
    captured = capsys.readouterr()
    assert json.loads(captured.out) == payload
    if os.name != "nt":
        assert "0644" in captured.err
    else:
        assert "expected 0600" not in captured.err
    assert "ignored_line_numbers" in captured.err
    assert "synthetic-secret" not in captured.err
    assert "ignored.invalid" not in captured.err


def test_public_projector_strips_decisions_sessions_traces_and_evidence() -> None:
    detailed = {
        "status": "passed",
        "preflight": {
            "ready": [{"provider": "deepseek", "model": "deepseek-chat"}],
            "blocked": [],
        },
        "router": {
            "targets": [
                {
                    "provider": "deepseek",
                    "model": "deepseek-chat",
                    "ok": True,
                    "decision": {
                        "sessionKey": "mixed-router:secret-session",
                        "trail": [{"raw": "decision"}],
                    },
                    "request_identity": {"provider": "deepseek", "model": "deepseek-chat"},
                    "marker_present": True,
                    "usage": {"input_tokens": 3, "output_tokens": 1},
                    "cost": {"billed_cost_usd": 0.0},
                    "latency_ms": 9,
                }
            ]
        },
        "ensemble": {
            "lineups": [
                {
                    "ok": True,
                    "trace": {"fallback_reason": "must-not-report"},
                    "proposers": [
                        {"provider": "deepseek", "model": "deepseek-chat"},
                        {"provider": "openai", "model": "gpt-4.1-mini"},
                    ],
                    "aggregator": {"provider": "gemini", "model": "gemini-flash"},
                    "usage_breakdown": [
                        {
                            "provider": provider,
                            "model": model,
                            "role": role,
                            "usage": {"input_tokens": 2, "output_tokens": 1},
                            "cost": {"billed_cost_usd": 0.0},
                            "latency_ms": 4,
                        }
                        for provider, model, role in (
                            ("deepseek", "deepseek-chat", "primary"),
                            ("openai", "gpt-4.1-mini", "contrast"),
                            ("gemini", "gemini-flash", "aggregator"),
                        )
                    ],
                }
            ]
        },
    }

    rows = live._public_report_rows(detailed)  # noqa: SLF001

    assert len(rows) == 4
    assert all(set(row) == live._PUBLIC_RESULT_KEYS for row in rows)  # noqa: SLF001
    serialized = json.dumps(rows)
    for forbidden in ("decision", "sessionKey", "trace", "role", "marker_present"):
        assert forbidden not in serialized
    with pytest.raises(RuntimeError, match="invalid field set"):
        live._assert_public_report_schema([{**rows[0], "decision": {}}])  # noqa: SLF001


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("HTTP 401 unauthorized", "auth"),
        ("insufficient balance", "balance"),
        ("HTTP 403 not entitled", "not-entitled"),
        ("model does not exist", "model-unavailable"),
        ("429 rate limit", "rate-limit"),
        ("connection timeout", "transport"),
        ("DNS lookup failed", "transport"),
        ("TLS certificate verify failed", "transport"),
        ("HTTP 503 upstream unavailable", "transport"),
        ("unexpected parser state", "implementation"),
    ],
)
def test_failure_classification(text: str, expected: str) -> None:
    assert live._failure_class(text) == expected  # noqa: SLF001


def test_argument_parser_enforces_fixed_token_caps(tmp_path: Path) -> None:
    common = [
        "--secrets-file",
        str(tmp_path / "keys"),
        "--target",
        "deepseek=deepseek-chat",
        "--output",
        str(tmp_path / "out.json"),
    ]
    with pytest.raises(SystemExit):
        live._parse_args([*common, "--router-max-tokens", "65"])  # noqa: SLF001
    with pytest.raises(SystemExit):
        live._parse_args([*common, "--router-max-tokens", "31"])  # noqa: SLF001
    with pytest.raises(SystemExit):
        live._parse_args([*common, "--ensemble-max-tokens", "257"])  # noqa: SLF001
    with pytest.raises(SystemExit):
        live._parse_args([*common, "--ensemble-max-tokens", "127"])  # noqa: SLF001


def test_router_validation_failure_defaults_to_implementation() -> None:
    target = _target("deepseek", "deepseek-chat")
    row = live._router_result_row(  # noqa: SLF001
        target=target,
        expected_tier="c0",
        observation={"failure_class": None, "error": ""},
        ok=False,
        failures=["marker_missing"],
    )
    assert row["failure_class"] == "implementation"


def test_forced_tier_rotation_covers_all_tiers() -> None:
    assert [live._forced_tier_for_index(index) for index in range(6)] == [  # noqa: SLF001
        "c0",
        "c1",
        "c2",
        "c3",
        "c0",
        "c1",
    ]


def test_state_isolation_pair_prefers_a_different_adapter_shape() -> None:
    pair = live._state_isolation_pair(  # noqa: SLF001
        [
            _target("dashscope", "qwen3.6-flash"),
            _target("openai", "gpt-4.1-mini"),
            _target("minimax", "MiniMax-M2.7"),
        ]
    )
    assert pair is not None
    assert [target.provider for target in pair] == ["dashscope", "minimax"]


@pytest.mark.asyncio
async def test_isolated_gateway_always_removes_raw_tree(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    raw_root = tmp_path / "opensquilla-raw-gateway"

    def fake_mkdtemp(*, prefix: str) -> str:
        assert prefix
        raw_root.mkdir()
        return str(raw_root)

    async def fake_inner(**kwargs):
        root = kwargs["temp_root"]
        (root / "turn-calls").mkdir()
        (root / "turn-calls" / "raw.jsonl").write_text("raw")
        raise RuntimeError("synthetic setup failure")

    monkeypatch.setattr(live.tempfile, "mkdtemp", fake_mkdtemp)
    monkeypatch.setattr(live, "_run_isolated_gateway_in_temp", fake_inner)
    with pytest.raises(RuntimeError, match="synthetic setup failure"):
        await live._run_isolated_gateway(  # noqa: SLF001
            targets=[_target("deepseek", "deepseek-chat")],
            secrets={"DEEPSEEK_API_KEY": "synthetic"},
            router_max_tokens=32,
            ensemble_max_tokens=128,
            timeout_seconds=1,
            transient_retries=0,
            base_environment={"PATH": os.environ.get("PATH", "")},
        )
    assert not raw_root.exists()


@pytest.mark.asyncio
async def test_isolated_gateway_scopes_user_state_and_profile_lock_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    temp_root = tmp_path / "opensquilla-mixed-env"
    temp_root.mkdir()
    captured: dict[str, object] = {}

    def fake_popen(*_args, **kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(live, "_free_port", lambda: 18702)
    monkeypatch.setattr(live.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        live,
        "_wait_for_gateway_health",
        lambda _proc, _port: (None, "synthetic offline stop"),
    )
    monkeypatch.setattr(live, "_stop_gateway", lambda _proc: ("", ""))

    result = await live._run_isolated_gateway_in_temp(  # noqa: SLF001
        targets=[_target("deepseek", "deepseek-chat")],
        secrets={"DEEPSEEK_API_KEY": "offline-mixed-secret"},
        router_max_tokens=32,
        ensemble_max_tokens=128,
        timeout_seconds=1,
        transient_retries=0,
        base_environment={"PATH": os.environ.get("PATH", "")},
        temp_root=temp_root,
    )

    env = captured["env"]
    assert isinstance(env, dict)
    assert env["OPENSQUILLA_USER_STATE_DIR"] == str(temp_root / "user-state")
    assert env["OPENSQUILLA_TEST_PROFILE_LOCK_ROOT"] == "1"
    assert (temp_root / "user-state").is_dir()
    assert captured["cwd"] == temp_root
    assert result["failure_class"] == "implementation"
