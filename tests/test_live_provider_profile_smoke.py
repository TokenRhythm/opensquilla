from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest


def _load_smoke_module():
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "live_provider_profile_smoke.py"
    spec = importlib.util.spec_from_file_location("live_provider_profile_smoke", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


smoke = _load_smoke_module()


def test_live_smoke_env_maps_cover_openai_zhipu_kimi_and_minimax() -> None:
    assert smoke._MODEL_ENV["anthropic"] == "ANTHROPIC_MODEL"
    assert smoke._BASE_ENV["anthropic"] == "ANTHROPIC_BASE_URL"
    assert smoke._DEFAULT_MODELS["anthropic"]

    assert smoke._MODEL_ENV["openai"] == "OPENAI_MODEL"
    assert smoke._BASE_ENV["openai"] == "OPENAI_BASE_URL"
    assert smoke._DEFAULT_MODELS["openai"] == "gpt-5.4-mini"

    assert smoke._MODEL_ENV["openai_responses"] == "OPENAI_MODEL"
    assert smoke._BASE_ENV["openai_responses"] == "OPENAI_BASE_URL"
    assert smoke._DEFAULT_MODELS["openai_responses"] == "gpt-5.4-mini"

    assert smoke._MODEL_ENV["openrouter"] == "OPENROUTER_MODEL"
    assert smoke._BASE_ENV["openrouter"] == "OPENROUTER_BASE_URL"
    assert smoke._DEFAULT_MODELS["openrouter"] == "deepseek/deepseek-v4-flash"

    assert smoke._MODEL_ENV["dashscope"] == "DASHSCOPE_MODEL"
    assert smoke._BASE_ENV["dashscope"] == "DASHSCOPE_BASE_URL"
    assert smoke._DEFAULT_MODELS["dashscope"] == "qwen3.7-plus"

    assert smoke._MODEL_ENV["gemini"] == "GEMINI_MODEL"
    assert smoke._BASE_ENV["gemini"] == "GEMINI_BASE_URL"
    assert smoke._DEFAULT_MODELS["gemini"] == "gemini-3.5-flash"

    assert smoke._MODEL_ENV["volcengine"] == "VOLCENGINE_MODEL"
    assert smoke._BASE_ENV["volcengine"] == "VOLCENGINE_BASE_URL"
    assert smoke._DEFAULT_MODELS["volcengine"] == "doubao-seed-2-0-lite-260215"

    assert smoke._MODEL_ENV["volcengine_coding_plan"] == "VOLCENGINE_CODING_MODEL"
    assert smoke._BASE_ENV["volcengine_coding_plan"] == "VOLCENGINE_CODING_BASE_URL"
    assert smoke._DEFAULT_MODELS["volcengine_coding_plan"] == "doubao-seed-2.0-pro"

    assert smoke._MODEL_ENV["zhipu"] == "ZAI_MODEL"
    assert smoke._BASE_ENV["zhipu"] == "ZAI_BASE_URL"
    assert smoke._DEFAULT_MODELS["zhipu"] == "glm-5"

    assert smoke._MODEL_ENV["moonshot"] == "MOONSHOT_MODEL"
    assert smoke._BASE_ENV["moonshot"] == "MOONSHOT_BASE_URL"
    assert smoke._DEFAULT_MODELS["moonshot"] == "kimi-k2.6"

    assert smoke._MODEL_ENV["kimi_coding_openai"] == "KIMI_CODING_MODEL"
    assert smoke._BASE_ENV["kimi_coding_openai"] == "KIMI_CODING_OPENAI_BASE_URL"
    assert smoke._DEFAULT_MODELS["kimi_coding_openai"] == "kimi-for-coding"

    assert smoke._MODEL_ENV["kimi_coding_anthropic"] == "KIMI_CODING_MODEL"
    assert smoke._BASE_ENV["kimi_coding_anthropic"] == "KIMI_CODING_ANTHROPIC_BASE_URL"
    assert smoke._DEFAULT_MODELS["kimi_coding_anthropic"] == "kimi-for-coding"

    assert smoke._MODEL_ENV["byteplus"] == "BYTEPLUS_MODEL"
    assert smoke._BASE_ENV["byteplus"] == "BYTEPLUS_BASE_URL"
    assert smoke._DEFAULT_MODELS["byteplus"] == "seed-2-0-lite-260228"

    assert smoke._MODEL_ENV["minimax"] == "MINIMAX_MODEL"
    assert smoke._BASE_ENV["minimax"] == "MINIMAX_BASE_URL"
    assert smoke._DEFAULT_MODELS["minimax"] == "MiniMax-M2.7"

    assert smoke._MODEL_ENV["minimax_openai"] == "MINIMAX_MODEL"
    assert smoke._BASE_ENV["minimax_openai"] == "MINIMAX_OPENAI_BASE_URL"
    assert smoke._DEFAULT_MODELS["minimax_openai"] == "MiniMax-M2.7"

    assert smoke._MODEL_ENV["minimax_coding_openai"] == "MINIMAX_CODING_MODEL"
    assert smoke._BASE_ENV["minimax_coding_openai"] == "MINIMAX_CODING_OPENAI_BASE_URL"
    assert smoke._DEFAULT_MODELS["minimax_coding_openai"] == "MiniMax-M2.7"

    assert smoke._MODEL_ENV["minimax_coding_anthropic"] == "MINIMAX_CODING_MODEL"
    assert smoke._BASE_ENV["minimax_coding_anthropic"] == "MINIMAX_CODING_ANTHROPIC_BASE_URL"
    assert smoke._DEFAULT_MODELS["minimax_coding_anthropic"] == "MiniMax-M2.7"

    assert smoke._MODEL_ENV["mimo_openai"] == "MIMO_MODEL"
    assert smoke._BASE_ENV["mimo_openai"] == "MIMO_OPENAI_BASE_URL"
    assert smoke._DEFAULT_MODELS["mimo_openai"] == "mimo-v2.5"

    assert smoke._MODEL_ENV["mimo_anthropic"] == "MIMO_MODEL"
    assert smoke._BASE_ENV["mimo_anthropic"] == "MIMO_ANTHROPIC_BASE_URL"
    assert smoke._DEFAULT_MODELS["mimo_anthropic"] == "mimo-v2.5-pro"

    assert smoke._MODEL_ENV["tencent_tokenhub"] == "TENCENT_TOKENHUB_MODEL"
    assert smoke._BASE_ENV["tencent_tokenhub"] == "TENCENT_TOKENHUB_BASE_URL"
    assert smoke._DEFAULT_MODELS["tencent_tokenhub"] == "hy3"

    assert smoke._MODEL_ENV["tencent_tokenhub_anthropic"] == "TENCENT_TOKENHUB_MODEL"
    assert smoke._BASE_ENV["tencent_tokenhub_anthropic"] == "TENCENT_TOKENHUB_ANTHROPIC_BASE_URL"
    assert smoke._DEFAULT_MODELS["tencent_tokenhub_anthropic"] == "hy3"

    assert smoke._MODEL_ENV["tencent_tokenhub_intl"] == "TENCENT_TOKENHUB_INTL_MODEL"
    assert smoke._BASE_ENV["tencent_tokenhub_intl"] == "TENCENT_TOKENHUB_INTL_BASE_URL"
    assert smoke._DEFAULT_MODELS["tencent_tokenhub_intl"] == "deepseek-v3.2"

    assert smoke._MODEL_ENV["tencent_token_plan"] == "TENCENT_TOKEN_PLAN_MODEL"
    assert smoke._BASE_ENV["tencent_token_plan"] == "TENCENT_TOKEN_PLAN_BASE_URL"
    assert smoke._DEFAULT_MODELS["tencent_token_plan"] == "hy3"

    assert smoke._MODEL_ENV["tokenrhythm"] == "TOKENRHYTHM_MODEL"
    assert smoke._BASE_ENV["tokenrhythm"] == "TOKENRHYTHM_BASE_URL"
    assert smoke._DEFAULT_MODELS["tokenrhythm"] == "deepseek-v4-flash"
    # Reasoning tokens bill against max_tokens: the default 64 budget would
    # return empty content with finish_reason "length".
    assert smoke._MIN_MAX_TOKENS["tokenrhythm"] == 1024
    assert smoke._MIN_MAX_TOKENS["minimax"] == 64


def test_live_smoke_gemini_compat_root_does_not_duplicate_v1() -> None:
    assert smoke._versioned_chat_url(
        "https://generativelanguage.googleapis.com/v1beta/openai"
    ) == (
        "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
    )


async def test_exact_probe_budget_bypasses_provider_stream_floor(monkeypatch: Any) -> None:
    observed: list[int] = []

    async def fake_direct(
        model: str,
        api_key: str,
        base_url: str,
        expected: str,
        max_tokens: int,
        auth_header_style: str,
    ) -> tuple[str, str, str, dict[str, int], int]:
        del api_key, base_url, auth_header_style
        observed.append(max_tokens)
        return "passed", model, expected, {"output_tokens": 1}, 1

    monkeypatch.setenv("MINIMAX_API_KEY", "synthetic-minimax-key")
    monkeypatch.setattr(smoke, "_direct_anthropic", fake_direct)

    result = await smoke.smoke_provider(
        "minimax",
        include_stream=False,
        model_override="MiniMax-M2.7",
        max_tokens=1,
        apply_token_floor=False,
    )

    assert observed == [1]
    assert result.direct_status == "passed"


def test_live_smoke_uses_moonshot_temperature_required_by_kimi_k2_6() -> None:
    assert smoke._direct_openai_temperature("moonshot", "kimi-k2.6") == 1
    assert smoke._direct_openai_temperature("kimi_coding_openai", "kimi-for-coding") == 1
    assert smoke._direct_openai_temperature("moonshot", "moonshot-v1-8k") == 0
    assert smoke._direct_openai_temperature("openai", "gpt-5.4-mini") == 0
    assert (
        smoke._direct_openai_token_limit_field("openai", "gpt-5.4-mini") == "max_completion_tokens"
    )
    assert smoke._direct_openai_token_limit_field("openai", "gpt-4.1") == "max_tokens"


def test_cost_estimate_is_provider_aware_cache_aware_and_preserves_real_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.engine.pricing import PriceEntry

    resolved_calls: list[tuple[str, str]] = []

    def fake_resolve(model: str, provider: str) -> SimpleNamespace:
        resolved_calls.append((model, provider))
        return SimpleNamespace(
            entry=PriceEntry(
                input_per_m=10.0,
                output_per_m=20.0,
                cache_read_per_m=1.0,
                cache_write_per_m=2.0,
            ),
            source="catalog",
        )

    monkeypatch.setattr(smoke, "resolve_model_price", fake_resolve)
    cost = smoke._cost_estimate(
        "tokenrhythm",
        "deepseek-v4-flash",
        {
            "direct": {"prompt_tokens": 100, "completion_tokens": 10},
            "stream": {
                "input_tokens": 100,
                "output_tokens": 10,
                "cached_tokens": 40,
                "cache_write_tokens": 10,
                "billed_cost": 0.0,
                "cost_source": "provider_billed",
            },
        },
    )

    # 50 fresh*10 + 40 read*1 + 10 write*2 + 10 output*20, per million.
    assert cost["opensquilla_estimated_cost_usd"] == pytest.approx(760 / 1_000_000)
    assert cost["estimate_basis"] == "cache_aware"
    assert cost["provider_billed_cost_usd"] == 0.0
    assert cost["cost_source"] == "provider_billed"
    assert resolved_calls == [("deepseek-v4-flash", "tokenrhythm")]


@pytest.mark.parametrize(
    ("provider", "model", "reasoning_format", "expected"),
    [
        ("gemini", "gemini-2.5-flash", "gemini", {"reasoning_effort": "none"}),
        ("dashscope", "qwen-reasoning", "dashscope", {"enable_thinking": False}),
        ("zhipu", "glm-reasoning", "zai", {"thinking": {"type": "disabled"}}),
        (
            "volcengine",
            "doubao-reasoning",
            "volcengine",
            {"thinking": {"type": "disabled"}},
        ),
    ],
)
def test_direct_smoke_explicitly_disables_supported_reasoning_dialects(
    monkeypatch: Any,
    provider: str,
    model: str,
    reasoning_format: str,
    expected: dict[str, Any],
) -> None:
    monkeypatch.setattr(
        smoke,
        "_model_capabilities",
        lambda *args: SimpleNamespace(
            supports_reasoning=True,
            reasoning_format=reasoning_format,
        ),
    )
    payload: dict[str, Any] = {}

    smoke._apply_direct_reasoning_off(
        payload,
        provider=provider,
        model=model,
        base_url="https://provider.example/v1",
    )

    assert payload == expected


def test_live_smoke_parses_csv_model_lists() -> None:
    assert smoke._csv_values("glm-5, glm-5.1,, kimi-k2.6 ") == [
        "glm-5",
        "glm-5.1",
        "kimi-k2.6",
    ]
    assert smoke._csv_values(None) == []


def test_anthropic_headers_follow_registry_auth_styles() -> None:
    native = smoke._headers_for_anthropic("native-secret")
    assert native["x-api-key"] == "native-secret"
    assert "Authorization" not in native
    assert native["anthropic-version"] == "2023-06-01"

    compatible = smoke._headers_for_anthropic("compat-secret", "bearer")
    assert compatible["Authorization"] == "Bearer compat-secret"
    assert "x-api-key" not in compatible


def test_openai_responses_direct_smoke_uses_responses_protocol(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}

    class FakeAsyncClient:
        def __init__(self, **kwargs: Any) -> None:
            captured["client_kwargs"] = kwargs

        async def __aenter__(self) -> FakeAsyncClient:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def post(
            self,
            url: str,
            *,
            headers: dict[str, str],
            json: dict[str, Any],
        ) -> httpx.Response:
            captured.update({"url": url, "headers": headers, "payload": json})
            return httpx.Response(
                200,
                request=httpx.Request("POST", url),
                json={
                    "model": "gpt-test",
                    "output": [
                        {
                            "type": "message",
                            "content": [{"type": "output_text", "text": "opensquilla response ok"}],
                        }
                    ],
                    "usage": {"input_tokens": 2, "output_tokens": 3},
                },
            )

    monkeypatch.setattr(smoke.httpx, "AsyncClient", FakeAsyncClient)

    result = asyncio.run(
        smoke._direct_openai_responses(
            "gpt-test",
            "test-secret",
            "https://api.openai.com/v1",
            "opensquilla response ok",
            32,
        )
    )

    assert result[0] == "passed"
    assert captured["url"] == "https://api.openai.com/v1/responses"
    assert captured["payload"]["input"] == [
        {"role": "user", "content": "Reply exactly with: opensquilla response ok"}
    ]
    assert captured["payload"]["max_output_tokens"] == 32
    assert captured["payload"]["store"] is False
    assert "messages" not in captured["payload"]
    assert captured["client_kwargs"]["timeout"] == smoke._DIRECT_TIMEOUT_SECONDS


def test_adapter_stream_smoke_does_not_force_sampling_temperature(
    monkeypatch: Any,
) -> None:
    captured: dict[str, Any] = {}

    class FakeProvider:
        async def chat(self, messages: Any, *, config: Any):
            captured.update({"messages": messages, "config": config})
            yield smoke.TextDeltaEvent(text="opensquilla openai_responses smoke ok")
            yield smoke.DoneEvent(input_tokens=2, output_tokens=3, model="gpt-test")

    monkeypatch.setattr(smoke, "_build_provider", lambda config: FakeProvider())

    result = asyncio.run(
        smoke._stream_opensquilla(
            "openai_responses",
            "gpt-test",
            "test-secret",
            "https://api.openai.com/v1",
            "opensquilla openai_responses smoke ok",
            32,
        )
    )

    assert result[0] == "passed"
    assert captured["config"].temperature is None
    assert captured["config"].timeout == smoke._DIRECT_TIMEOUT_SECONDS
    assert captured["config"].model_capabilities is not None


def test_error_summary_redacts_provider_secret() -> None:
    secret = "sk-live-do-not-print"
    response = httpx.Response(
        401,
        request=httpx.Request("POST", "https://api.openai.com/v1/responses"),
        json={"error": {"message": f"Authorization: Bearer {secret}"}},
    )

    summary = smoke._error_summary(response, secrets=(secret,))

    assert secret not in summary
    assert "[REDACTED]" in summary


def test_failed_smoke_returns_nonzero_and_writes_safe_report(
    monkeypatch: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = tmp_path / "smoke.json"

    async def fake_smoke_provider(*args: Any, **kwargs: Any) -> Any:
        return smoke.SmokeResult(
            provider="openai",
            model="gpt-test",
            base_url="https://api.openai.com/v1",
            env_key="OPENAI_API_KEY",
            key_present=True,
            direct_status="failed",
            stream_status="skipped",
            response_model="",
            content_match="not_validated",
            usage={},
            cost={},
            error="synthetic failure",
            latency_ms=1,
        )

    monkeypatch.setattr(smoke, "smoke_provider", fake_smoke_provider)
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-smoke-secret")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "live_provider_profile_smoke.py",
            "--provider",
            "openai",
            "--no-env-file",
            "--output",
            str(output),
        ],
    )

    assert asyncio.run(smoke.main()) == 1
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert isinstance(payload, list) and len(payload) == 1
    assert set(payload[0]) == smoke._PUBLIC_RESULT_KEYS  # noqa: SLF001
    assert payload[0]["status"] == "failed"
    assert payload[0]["failure_class"] == "implementation"
    assert "synthetic-smoke-secret" not in output.read_text(encoding="utf-8")
    captured = capsys.readouterr()
    assert json.loads(captured.out) == payload
    assert "coverage" in captured.err
    assert "synthetic-smoke-secret" not in captured.err
    if os.name != "nt":
        assert output.stat().st_mode & 0o777 == 0o600


def test_smoke_public_projector_has_exact_schema_and_no_transport_details() -> None:
    result = smoke.SmokeResult(
        provider="openai",
        model="gpt-test",
        base_url="https://api.openai.com/v1",
        env_key="OPENAI_API_KEY",
        key_present=True,
        direct_status="passed",
        stream_status="passed",
        response_model="gpt-test-2026",
        content_match="exact",
        usage={"direct": {"output_tokens": 1}},
        cost={"opensquilla_estimated_cost_usd": 0.0001},
        error="",
        latency_ms=8,
    )

    rows = smoke._public_report_rows([result])  # noqa: SLF001

    assert set(rows[0]) == smoke._PUBLIC_RESULT_KEYS  # noqa: SLF001
    serialized = json.dumps(rows)
    for forbidden in ("base_url", "env_key", "key_present", "direct_status", "content_match"):
        assert forbidden not in serialized
    with pytest.raises(RuntimeError, match="invalid field set"):
        smoke._assert_public_report_schema([{**rows[0], "base_url": "forbidden"}])  # noqa: SLF001


def test_hidden_child_report_keeps_internal_matrix_evidence(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    output = tmp_path / "child-smoke.json"

    async def fake_smoke_provider(*_args: Any, **_kwargs: Any) -> Any:
        return smoke.SmokeResult(
            provider="openai",
            model="gpt-test",
            base_url="https://api.openai.com/v1",
            env_key="OPENAI_API_KEY",
            key_present=False,
            direct_status="skipped",
            stream_status="skipped",
            response_model="",
            content_match="not_run",
            usage={},
            cost={},
            error="missing credential",
            latency_ms=0,
        )

    monkeypatch.setattr(smoke, "smoke_provider", fake_smoke_provider)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "live_provider_profile_smoke.py",
            "--provider",
            "openai",
            "--no-env-file",
            "--child-report",
            "--output",
            str(output),
        ],
    )

    assert asyncio.run(smoke.main()) == 1
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    assert payload["results"][0]["direct_status"] == "skipped"


def test_smoke_rejects_non_temporary_report_before_provider_call(
    monkeypatch: Any,
) -> None:
    called = False

    async def forbidden_smoke_provider(*args: Any, **kwargs: Any) -> Any:
        nonlocal called
        called = True
        raise AssertionError("provider call must not start")

    monkeypatch.setattr(smoke, "smoke_provider", forbidden_smoke_provider)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "live_provider_profile_smoke.py",
            "--provider",
            "openai",
            "--no-env-file",
            "--output",
            str(Path.cwd() / "non-temporary-live-report.json"),
        ],
    )

    with pytest.raises(SystemExit, match="2"):
        asyncio.run(smoke.main())
    assert called is False
