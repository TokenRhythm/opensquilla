from __future__ import annotations

import importlib.util
import json
import os
import sys
import tomllib
from pathlib import Path

import pytest


def _load_e2e_module():
    script_path = (
        Path(__file__).resolve().parents[1] / "scripts" / "live_provider_profile_gateway_e2e.py"
    )
    spec = importlib.util.spec_from_file_location("live_provider_profile_gateway_e2e", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


e2e = _load_e2e_module()


def test_gateway_e2e_defaults_cover_all_router_profiles() -> None:
    assert e2e.DEFAULT_PROVIDERS == [
        "openrouter",
        "dashscope",
        "deepseek",
        "gemini",
        "volcengine",
        "byteplus",
        "openai",
        "zhipu",
        "moonshot",
        "tokenrhythm",
    ]


def test_natural_router_cases_are_text_only_marker_checks() -> None:
    for case in e2e.TIER_CASES:
        message = case["message"]
        assert "不要调用工具" in message, case["id"]
        assert "{marker}" in message, case["id"]


def test_structured_compare_case_is_bounded_to_keep_marker_in_smoke_budget() -> None:
    case = next(case for case in e2e.TIER_CASES if case["id"] == "r1_structured_compare")

    assert "不超过" in case["message"]


def test_debugging_case_is_bounded_to_keep_marker_in_smoke_budget() -> None:
    case = next(case for case in e2e.TIER_CASES if case["id"] == "r2_debugging")

    assert "不超过" in case["message"]


def test_case_markers_are_stable_text_not_millisecond_numbers() -> None:
    marker = e2e._case_marker("openrouter", "c2", "coverage_t2")

    assert marker == "E2E_OPENROUTER_C2_COVERAGE_T2"
    assert not marker.rsplit("_", 1)[-1].isdigit()


def test_live_gateway_profile_config_bounds_agent_runtime(tmp_path: Path) -> None:
    config_path = tmp_path / "gateway.toml"

    e2e._write_config(
        config_path,
        "openrouter",
        "https://openrouter.ai/api/v1",
        "deepseek/deepseek-v4-flash",
        max_tokens=384,
    )

    data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert data["agent_max_iterations"] <= 8
    assert data["agent_max_provider_retries"] == 0
    assert data["agent_runtime_timeout_seconds"] < data["llm_request_timeout_seconds"]
    assert data["task_runtime"]["turn_hard_deadline_s"] < 120.0
    assert data["privacy"]["disable_network_observability"] is True
    assert data["tools"]["profile"] == "minimal"
    assert data["tools"]["deny"] == ["*"]
    assert data["llm"]["api_key_env"] == "OPENROUTER_API_KEY"
    if os.name != "nt":
        assert config_path.stat().st_mode & 0o777 == 0o600


def test_live_gateway_inline_nonlegacy_profile_can_force_thinking_off(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "gateway.toml"
    tiers = {
        slot: {
            "provider": "minimax",
            "model": "MiniMax-M2.7",
            "thinking_level": "off",
            "image_only": slot != "c1",
        }
        for slot in ("c0", "c1", "c2", "c3")
    }

    e2e._write_config(
        config_path,
        "minimax",
        "https://api.minimaxi.com/anthropic",
        "MiniMax-M2.7",
        max_tokens=64,
        tier_overrides=tiers,
        llm_thinking="off",
    )

    data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert data["llm"]["thinking"] == "off"
    assert "tier_profile" not in data["squilla_router"]
    assert data["squilla_router"]["tiers"]["c1"]["thinking_level"] == "off"


def test_tokenrhythm_uses_curated_inline_tiers_and_never_persists_profile(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "gateway.toml"
    tiers = e2e._profile_tiers("tokenrhythm")

    e2e._write_config(
        config_path,
        "tokenrhythm",
        "https://tokenrhythm.studio/v1",
        tiers["c1"]["model"],
        max_tokens=1024,
        tier_overrides=tiers,
    )

    data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert "tier_profile" not in data["squilla_router"]
    assert data["squilla_router"]["tiers"] == tiers


def test_tokenrhythm_run_uses_inline_preset_and_1024_output_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_batch(**kwargs):
        captured.update(kwargs)
        tiers = kwargs["tiers"]
        return {
            "ok": True,
            "health": {},
            "cases": [
                {
                    "ok": True,
                    "case_mode": "natural_router",
                    "actual_slot_covered": slot,
                    "actual_request_model": tiers[slot]["model"],
                    "assistant_excerpt": "ok",
                    "failure_kind": None,
                }
                for slot in e2e.TEXT_PROFILE_SLOTS
            ],
            "usage_from_turn_logs": {},
            "error": None,
        }

    monkeypatch.setenv("TOKENRHYTHM_API_KEY", "synthetic-rotated-key")
    monkeypatch.delenv("TOKENRHYTHM_BASE_URL", raising=False)
    monkeypatch.setattr(e2e, "_run_gateway_case_batch", fake_batch)

    result = e2e._run_provider("tokenrhythm", max_tokens=64, timeout_seconds=1.0)

    tiers = e2e._profile_tiers("tokenrhythm")
    assert captured["max_tokens"] == 1024
    assert captured["tier_overrides"] == tiers
    assert result["tier_profile"] is None
    assert result["tier_mode"] == "inline_preset"


def test_profile_slot_targets_cover_slots_not_unique_models() -> None:
    tiers = {
        "c0": {"provider": "deepseek", "model": "deepseek-v4-flash"},
        "c1": {
            "provider": "deepseek",
            "model": "deepseek-v4-flash",
            "thinking_level": "low",
        },
        "c2": {"provider": "deepseek", "model": "deepseek-v4-pro"},
        "c3": {
            "provider": "deepseek",
            "model": "deepseek-v4-pro",
            "thinking_level": "high",
        },
        "image_model": {"provider": "openrouter", "model": "vision", "image_only": True},
    }

    targets = e2e._profile_slot_targets(tiers)

    assert list(targets) == ["c0", "c1", "c2", "c3"]
    assert targets["c0"]["model"] == targets["c1"]["model"]
    assert targets["c1"]["thinking_level"] == "low"


def test_forced_tier_overrides_make_only_target_slot_text_routable() -> None:
    tiers = {
        "c0": {"provider": "deepseek", "model": "deepseek-v4-flash"},
        "c1": {"provider": "deepseek", "model": "deepseek-v4-flash"},
        "c2": {"provider": "deepseek", "model": "deepseek-v4-pro"},
        "c3": {"provider": "deepseek", "model": "deepseek-v4-pro"},
    }

    overrides = e2e._forced_tier_overrides_for_slot(tiers, "c2")

    assert overrides["c2"]["image_only"] is False
    assert overrides["c2"]["model"] == "deepseek-v4-pro"
    assert overrides["c0"]["image_only"] is True
    assert overrides["c1"]["image_only"] is True
    assert overrides["c3"]["image_only"] is True


def test_missing_profile_slots_are_computed_by_slot() -> None:
    tiers = {
        "c0": {"provider": "deepseek", "model": "deepseek-v4-flash"},
        "c1": {"provider": "deepseek", "model": "deepseek-v4-flash"},
        "c2": {"provider": "deepseek", "model": "deepseek-v4-pro"},
        "c3": {"provider": "deepseek", "model": "deepseek-v4-pro"},
    }
    rows = [
        {
            "ok": True,
            "expected_slot": "c0",
            "actual_slot_covered": "c0",
            "expected_model": "deepseek-v4-flash",
            "actual_request_model": "deepseek-v4-flash",
        },
        {
            "ok": True,
            "expected_slot": "c2",
            "actual_slot_covered": "c2",
            "expected_model": "deepseek-v4-pro",
            "actual_request_model": "deepseek-v4-pro",
        },
    ]

    assert e2e._missing_profile_slots(tiers, rows) == ["c1", "c3"]


def test_cost_summary_never_promotes_gateway_placeholder_to_provider_bill() -> None:
    cost = e2e._estimate_cost(
        "glm-5.1",
        {"input_tokens": 1000, "output_tokens": 2000, "billed_cost": 0.0},
    )

    assert cost["provider_billed_cost_usd"] is None
    assert cost["raw_gateway_usage_billed_cost_usd"] == 0.0
    assert cost["cost_source"] == "opensquilla_static_estimate"
    assert cost["opensquilla_estimated_cost_usd"] > 0


def test_openrouter_nonzero_billed_cost_is_recorded_as_provider_bill() -> None:
    cost = e2e._estimate_cost(
        "z-ai/glm-5.1",
        {
            "input_tokens": 1000,
            "output_tokens": 2000,
            "billed_cost": 0.0123,
            "cost_source": "provider_billed",
        },
        provider="openrouter",
    )

    assert cost["provider_billed_cost_usd"] == 0.0123
    assert cost["raw_gateway_usage_billed_cost_usd"] == 0.0123
    assert cost["cost_source"] == "provider_billed"
    assert cost["billing_scope"] == "provider_response"
    assert cost["opensquilla_estimated_cost_usd"] > 0


def test_confirmed_zero_billed_cost_is_not_demoted_to_estimate() -> None:
    cost = e2e._estimate_cost(
        "deepseek-v4-flash",
        {
            "input_tokens": 1000,
            "output_tokens": 20,
            "billed_cost": 0.0,
            "cost_source": "provider_billed",
        },
        provider="tokenrhythm",
    )

    assert cost["provider_billed_cost_usd"] == 0.0
    assert cost["cost_source"] == "provider_billed"
    assert cost["billing_scope"] == "provider_response"


def test_gateway_usage_projection_keeps_source_and_all_four_token_buckets() -> None:
    usage = e2e._accounting_usage_fields(
        {
            "input_tokens": 100,
            "output_tokens": 10,
            "reasoning_tokens": 7,
            "cached_tokens": 40,
            "cache_write_tokens": 5,
            "billed_cost": 0.0,
            "cost_source": "provider_billed",
            "response_text": "must not enter report",
        }
    )

    assert usage == {
        "input_tokens": 100,
        "output_tokens": 10,
        "reasoning_tokens": 7,
        "cached_tokens": 40,
        "cache_write_tokens": 5,
        "billed_cost": 0.0,
        "cost_source": "provider_billed",
    }


def test_router_step_is_extracted_from_decision_log() -> None:
    decision = {
        "pipeline_steps": [
            {"step_name": "resolve_model", "routed_tier": None},
            {
                "step_name": "apply_squilla_router",
                "routed_tier": "c2",
                "routing_source": "v4_phase3",
                "confidence": 0.91,
            },
        ]
    }

    step = e2e._router_step_from_decision(decision)

    assert step["routed_tier"] == "c2"
    assert step["routing_source"] == "v4_phase3"
    assert step["confidence"] == 0.91


def test_gateway_e2e_dotenv_loader_only_accepts_registry_secrets(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env_file = tmp_path / "providers.env"
    env_file.write_text(
        "\n".join(
            [
                "OPENAI_API_KEY=offline-test-secret",
                "OPENAI_BASE_URL=https://attacker.invalid/v1",
                "OPENAI_MODEL=attacker-model",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)

    e2e._load_env_quietly(env_file)

    assert e2e.os.environ["OPENAI_API_KEY"] == "offline-test-secret"
    assert "OPENAI_BASE_URL" not in e2e.os.environ
    assert "OPENAI_MODEL" not in e2e.os.environ


def test_gateway_e2e_rejects_non_registry_endpoint_before_any_live_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", "https://attacker.invalid/v1")

    with pytest.raises(ValueError, match="endpoint override rejected"):
        e2e._run_provider("openai", max_tokens=1, timeout_seconds=1.0)


def test_gateway_batch_always_removes_raw_tree(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    raw_root = tmp_path / "opensquilla-raw-profile-gateway"

    def fake_mkdtemp(*, prefix: str) -> str:
        assert prefix
        raw_root.mkdir()
        return str(raw_root)

    def fake_inner(**kwargs):
        root = kwargs["tmp_path"]
        (root / "turn-calls").mkdir()
        (root / "turn-calls" / "raw.jsonl").write_text("raw")
        raise RuntimeError("synthetic batch failure")

    monkeypatch.setattr(e2e.tempfile, "mkdtemp", fake_mkdtemp)
    monkeypatch.setattr(e2e, "_run_gateway_case_batch_in_temp", fake_inner)
    with pytest.raises(RuntimeError, match="synthetic batch failure"):
        e2e._run_gateway_case_batch(
            provider="openai",
            api_key="synthetic",
            base_url="https://api.openai.com/v1",
            tiers={"c1": {"provider": "openai", "model": "gpt-4.1-mini"}},
            cases=[],
            max_tokens=32,
            timeout_seconds=1,
            case_mode="test",
        )
    assert not raw_root.exists()


def test_gateway_batch_isolates_user_state_and_profile_lock_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    temp_root = tmp_path / "opensquilla-profile-env"
    temp_root.mkdir()
    captured: dict[str, object] = {}

    def fake_popen(*_args, **kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(e2e, "_free_port", lambda: 18701)
    monkeypatch.setattr(e2e.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        e2e,
        "_wait_for_gateway_health",
        lambda _proc, _port: (None, "synthetic offline stop"),
    )
    monkeypatch.setattr(e2e, "_stop_gateway", lambda _proc: ("", ""))

    result = e2e._run_gateway_case_batch_in_temp(  # noqa: SLF001
        provider="openai",
        api_key="offline-profile-secret",
        base_url="https://api.openai.com/v1",
        tiers={"c1": {"provider": "openai", "model": "gpt-4.1-mini"}},
        cases=[],
        max_tokens=32,
        timeout_seconds=1,
        case_mode="test",
        tmp_path=temp_root,
    )

    env = captured["env"]
    assert isinstance(env, dict)
    assert env["OPENSQUILLA_USER_STATE_DIR"] == str(temp_root / "user-state")
    assert env["OPENSQUILLA_TEST_PROFILE_LOCK_ROOT"] == "1"
    assert (temp_root / "user-state").is_dir()
    assert captured["cwd"] == temp_root
    assert result["error"] == "synthetic offline stop"


def test_public_provider_summary_excludes_raw_turn_material() -> None:
    raw = {
        "provider": "openai",
        "ok": True,
        "models_covered": ["gpt-4.1-mini"],
        "usage_from_turn_logs": {"input_tokens": 2, "output_tokens": 1},
        "cases": [
            {
                "ok": True,
                "actual_response_model": "gpt-4.1-mini",
                "usage": {"input_tokens": 2, "output_tokens": 1},
                "cost": {"opensquilla_estimated_cost_usd": 0.00001},
                "latency_ms": 9,
                "assistant_excerpt": "must not leave memory boundary",
                "session_key": "must-not-report",
                "marker": "must-not-report",
                "accepted": {"raw": True},
            }
        ],
    }
    public = e2e._public_provider_result(raw)  # noqa: SLF001

    serialized = str(public)
    assert public["status"] == "passed"
    assert public["cases"][0]["model"] == "gpt-4.1-mini"
    assert public["latency_ms"] == 9
    assert "assistant_excerpt" not in serialized
    assert "session_key" not in serialized
    assert "marker" not in serialized
    assert "accepted" not in serialized

    final_rows = e2e._public_report_rows([raw])  # noqa: SLF001
    assert len(final_rows) == 1
    assert set(final_rows[0]) == e2e._PUBLIC_RESULT_KEYS  # noqa: SLF001
    final_serialized = json.dumps(final_rows)
    assert "cases" not in final_serialized
    assert "session_key" not in final_serialized
    with pytest.raises(RuntimeError, match="invalid field set"):
        e2e._assert_public_report_schema(  # noqa: SLF001
            [{**final_rows[0], "session_key": "forbidden"}]
        )


def test_gateway_main_writes_and_prints_only_exact_public_rows(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = tmp_path / "gateway-profile.json"
    monkeypatch.setattr(
        e2e,
        "_run_provider",
        lambda *_args, **_kwargs: {
            "provider": "openai",
            "ok": True,
            "models_covered": ["gpt-4.1-mini"],
            "failure_kinds": [],
            "cases": [
                {
                    "ok": True,
                    "actual_response_model": "gpt-4.1-mini",
                    "usage": {"input_tokens": 2, "output_tokens": 1},
                    "cost": {"opensquilla_estimated_cost_usd": 0.00001},
                    "latency_ms": 6,
                    "session_key": "must-not-report",
                    "assistant_excerpt": "must-not-report",
                }
            ],
        },
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "live_provider_profile_gateway_e2e.py",
            "--providers",
            "openai",
            "--no-env-file",
            "--output",
            str(output),
        ],
    )

    assert e2e.main() == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert isinstance(payload, list) and len(payload) == 1
    assert set(payload[0]) == e2e._PUBLIC_RESULT_KEYS  # noqa: SLF001
    assert "must-not-report" not in json.dumps(payload)
    captured = capsys.readouterr()
    assert json.loads(captured.out) == payload
    assert "coverage" in captured.err
