from __future__ import annotations

import http.server
import importlib.util
import json
import os
import sqlite3
import sys
import threading
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
    assert data["llm"]["max_tokens"] == 1024
    assert "tier_profile" not in data["squilla_router"]
    assert data["squilla_router"]["tiers"] == tiers


def test_tokenrhythm_run_uses_inline_preset_and_4096_output_floor(
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
    assert captured["max_tokens"] == 4096
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
                "usage": {
                    "input_tokens": 2,
                    "output_tokens": 1,
                    "models": [{"prompt": "nested-must-not-report"}],
                    "source": {"session_key": "nested-must-not-report"},
                    "session_key": "must-not-report",
                },
                "cost": {
                    "opensquilla_estimated_cost_usd": 0.00001,
                    "source": {"prompt": "nested-must-not-report"},
                    "prompt": "must-not-report",
                },
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
    assert "prompt" not in final_serialized
    assert "nested-must-not-report" not in final_serialized
    with pytest.raises(RuntimeError, match="invalid field set"):
        e2e._assert_public_report_schema(  # noqa: SLF001
            [{**final_rows[0], "session_key": "forbidden"}]
        )
    with pytest.raises(RuntimeError, match="invalid usage values"):
        e2e._assert_public_report_schema(  # noqa: SLF001
            [
                {
                    **final_rows[0],
                    "usage": {"input_tokens": {"session_key": "forbidden"}},
                }
            ]
        )
    with pytest.raises(RuntimeError, match="invalid cost values"):
        e2e._assert_public_report_schema(  # noqa: SLF001
            [
                {
                    **final_rows[0],
                    "cost": {"source": {"prompt": "forbidden"}},
                }
            ]
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


def test_gateway_main_catches_unexpected_runner_error_and_redacts_stripped_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = tmp_path / "unexpected-runner-error.json"
    output.write_text("stale report", encoding="utf-8")
    stripped_key = "synthetic-stripped-provider-key"
    monkeypatch.setenv("OPENAI_API_KEY", f"  {stripped_key}  ")

    def fail_runner(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise TypeError(f"synthetic unexpected failure: {stripped_key}")

    monkeypatch.setattr(e2e, "_run_provider", fail_runner)
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

    assert e2e.main() == 2
    assert not output.exists()
    captured = capsys.readouterr()
    assert stripped_key not in captured.err
    assert "[REDACTED]" in captured.err
    assert "Traceback" not in captured.err


def test_gateway_main_catches_projection_error_and_removes_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = tmp_path / "projection-error.json"
    monkeypatch.setattr(
        e2e,
        "_run_provider",
        lambda *_args, **_kwargs: {
            "provider": "openai",
            "ok": True,
            "models_covered": ["gpt-4.1-mini"],
            "failure_kinds": [],
            "cases": [],
        },
    )

    def fail_projection(_results: object) -> list[dict[str, object]]:
        raise AttributeError("synthetic projection failure")

    monkeypatch.setattr(e2e, "_public_report_rows", fail_projection)
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

    assert e2e.main() == 2
    assert not output.exists()
    captured = capsys.readouterr()
    assert "unable to write live report" in captured.err
    assert "Traceback" not in captured.err


def test_gateway_main_rejects_directory_output_before_running(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output = tmp_path / "report-directory"
    output.mkdir()
    monkeypatch.setattr(
        e2e,
        "_run_provider",
        lambda *_args, **_kwargs: pytest.fail("runner must not start for directory output"),
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

    with pytest.raises(SystemExit):
        e2e.main()
    assert output.is_dir()


def test_attachment_capacity_fixture_is_over_raw_admission_but_media_bounded() -> None:
    fixture = e2e._attachment_capacity_fixture()  # noqa: SLF001
    metrics = fixture["metrics"]

    assert metrics["history_turn_count"] == 3
    assert metrics["history_image_count"] == 4
    assert metrics["last_history_image_count"] == 2
    assert (
        metrics["raw_history_estimated_tokens"]
        >= metrics["router_admission_token_limit"] + e2e.ATTACHMENT_CAPACITY_MIN_RAW_MARGIN_TOKENS
    )
    assert (
        metrics["configured_max_output_tokens"]
        == e2e.ATTACHMENT_CAPACITY_MAX_OUTPUT_TOKENS
    )
    assert metrics[
        "router_admission_token_limit"
    ] == e2e._attachment_capacity_admission_token_limit(  # noqa: SLF001
        thinking_budget_tokens=0,
    )
    assert metrics[
        "router_max_thinking_admission_token_limit"
    ] == e2e._attachment_capacity_admission_token_limit(  # noqa: SLF001
        thinking_budget_tokens=e2e.MAX_THINKING_BUDGET_TOKENS,
    )
    assert metrics["raw_history_fits_at_zero_thinking"] is False
    assert metrics["projected_media_fits_at_max_thinking"] is True
    assert not e2e._attachment_capacity_request_fits(  # noqa: SLF001
        metrics["raw_history_estimated_tokens"],
        thinking_budget_tokens=0,
    )
    assert e2e._attachment_capacity_request_fits(  # noqa: SLF001
        metrics["projected_media_tokens"],
        thinking_budget_tokens=e2e.MAX_THINKING_BUDGET_TOKENS,
    )
    assert (
        metrics["projected_media_tokens"]
        < metrics["router_max_thinking_admission_token_limit"]
        < metrics["router_admission_token_limit"]
    )
    image_counts = [len(json.loads(turn["user"])["attachments"]) for turn in fixture["turns"]]
    assert image_counts == [1, 1, 2]
    assert fixture["current_attachment"]["type"] == "image/png"
    assert fixture["current_attachment"]["data"]


def test_attachment_capacity_seed_uses_only_isolated_session_state(tmp_path: Path) -> None:
    fixture = e2e._attachment_capacity_fixture()  # noqa: SLF001
    state_dir = tmp_path / "isolated-state"
    session_key = "agent:main:webchat:offline-attachment-capacity"

    e2e._seed_attachment_capacity_history(state_dir, session_key, fixture)  # noqa: SLF001

    connection = sqlite3.connect(state_dir / "sessions.db")
    try:
        rows = connection.execute(
            "SELECT role, content FROM transcript_entries WHERE session_key = ? ORDER BY id",
            (session_key,),
        ).fetchall()
        session_state = connection.execute(
            "SELECT compaction_count, model_routing_mode FROM sessions WHERE session_key = ?",
            (session_key,),
        ).fetchone()
    finally:
        connection.close()
    assert [row[0] for row in rows] == ["user", "assistant"] * 3
    assert [row[1] for row in rows] == [
        value for turn in fixture["turns"] for value in (turn["user"], turn["assistant"])
    ]
    assert session_state == (0, "router")


def test_attachment_capacity_config_is_single_call_and_includes_image_tier(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "gateway.toml"
    tiers = e2e._tokenrhythm_attachment_tiers()  # noqa: SLF001

    e2e._write_config(  # noqa: SLF001
        config_path,
        "tokenrhythm",
        "https://tokenrhythm.studio/v1",
        tiers["c1"]["model"],
        max_tokens=e2e.ATTACHMENT_CAPACITY_MAX_OUTPUT_TOKENS,
        tier_overrides=tiers,
        agent_max_iterations=1,
        llm_request_timeout_seconds=e2e.ATTACHMENT_CAPACITY_PROVIDER_TIMEOUT_SECONDS,
        agent_runtime_timeout_seconds=e2e.ATTACHMENT_CAPACITY_AGENT_TIMEOUT_SECONDS,
        turn_hard_deadline_seconds=e2e.ATTACHMENT_CAPACITY_AGENT_TIMEOUT_SECONDS,
        model_context_window_tokens=e2e.ATTACHMENT_CAPACITY_BASE_CONTEXT_WINDOW_TOKENS,
        model_supports_vision_override=e2e.ATTACHMENT_CAPACITY_MODEL,
    )

    data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert data["agent_max_iterations"] == 1
    assert data["agent_max_provider_retries"] == 0
    assert data["llm"]["max_tokens"] == e2e.ATTACHMENT_CAPACITY_MAX_OUTPUT_TOKENS
    assert data["llm_request_timeout_seconds"] == 60.0
    assert data["agent_runtime_timeout_seconds"] == 75.0
    assert data["task_runtime"]["turn_hard_deadline_s"] == 75.0
    assert data["naming"]["enabled"] is False
    assert data["squilla_router"]["tiers"]["image_model"] == tiers["image_model"]
    assert data["squilla_router"]["tiers"]["image_model"]["model"] == "kimi-k2.6"
    assert all(tiers[slot]["supports_image"] is False for slot in e2e.TEXT_PROFILE_SLOTS)
    assert (
        data["models"]["tokenrhythm"]["deepseek-v4-pro-0813"]["context_window"]
        == e2e.ATTACHMENT_CAPACITY_BASE_CONTEXT_WINDOW_TOKENS
    )
    assert data["models"]["tokenrhythm"]["kimi-k2.6"]["supports_vision"] is True


def test_attachment_capacity_runner_reaches_provider_through_real_gateway(
    tmp_path: Path,
) -> None:
    requests: list[dict[str, object]] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, _format: str, *_args: object) -> None:
            return

        def do_POST(self) -> None:  # noqa: N802 - stdlib handler contract
            if self.path.rstrip("/") != "/v1/chat/completions":
                self.send_error(404)
                return
            length = int(self.headers.get("content-length") or "0")
            payload = json.loads(self.rfile.read(length))
            requests.append(payload)
            model = str(payload.get("model") or "kimi-k2.6")
            chunks = [
                {
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "role": "assistant",
                                "content": "Synthetic image reply.\nATTACHMENT_CAPACITY_LIVE_OK",
                            },
                            "finish_reason": None,
                        }
                    ],
                },
                {
                    "model": model,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 5},
                },
            ]
            body = b"".join(
                f"data: {json.dumps(chunk, separators=(',', ':'))}\n\n".encode()
                for chunk in chunks
            ) + b"data: [DONE]\n\n"
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        result = e2e._run_tokenrhythm_attachment_capacity_in_temp(  # noqa: SLF001
            api_key="synthetic-attachment-capacity-key",
            base_url=f"http://{host}:{port}/v1",
            tmp_path=tmp_path,
            synthetic_vision_capability_override=True,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert result["ok"] is True, result
    assert len(requests) == 1
    assert requests[0]["model"] == "kimi-k2.6"
    case = result["cases"][0]
    assert case["usage"]["physical_request_count"] == 1
    assert case["usage"]["physical_response_count"] == 1
    assert case["usage"]["compaction_count"] == 0
    assert case["usage"]["provider_proof_fits"] is True


@pytest.mark.parametrize(
    ("status_code", "provider_message", "expected_failure"),
    [
        (401, "invalid api key", "auth"),
        (429, "rate limit exceeded", "rate-limit"),
        (503, "service temporarily unavailable", "transport"),
    ],
)
def test_attachment_capacity_runner_bounds_provider_http_failures_to_one_call(
    tmp_path: Path,
    status_code: int,
    provider_message: str,
    expected_failure: str,
) -> None:
    requests: list[dict[str, object]] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, _format: str, *_args: object) -> None:
            return

        def do_POST(self) -> None:  # noqa: N802 - stdlib handler contract
            if self.path.rstrip("/") != "/v1/chat/completions":
                self.send_error(404)
                return
            length = int(self.headers.get("content-length") or "0")
            requests.append(json.loads(self.rfile.read(length)))
            body = json.dumps(
                {
                    "error": {
                        "message": provider_message,
                        "type": "synthetic_provider_error",
                        "code": status_code,
                    }
                },
                separators=(",", ":"),
            ).encode()
            self.send_response(status_code)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        result = e2e._run_tokenrhythm_attachment_capacity_in_temp(  # noqa: SLF001
            api_key="synthetic-attachment-capacity-key",
            base_url=f"http://{host}:{port}/v1",
            tmp_path=tmp_path,
            synthetic_vision_capability_override=True,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert result["ok"] is False
    assert len(requests) == 1
    case = result["cases"][0]
    assert case["failure_kind"] == expected_failure
    assert case["usage"]["physical_request_count"] == 1
    assert case["usage"]["physical_response_count"] == 0
    assert case["usage"]["compaction_count"] == 0


@pytest.mark.parametrize(
    ("mode", "expected_failure", "expected_response_count"),
    [
        ("empty", "implementation", 0),
        ("truncated", "implementation", 0),
        ("marker_missing", "implementation", 1),
        ("timeout", "transport", 0),
    ],
)
def test_attachment_capacity_runner_fails_closed_for_stream_faults(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mode: str,
    expected_failure: str,
    expected_response_count: int,
) -> None:
    requests: list[dict[str, object]] = []
    if mode == "timeout":
        monkeypatch.setattr(e2e, "ATTACHMENT_CAPACITY_PROVIDER_TIMEOUT_SECONDS", 1.0)

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, _format: str, *_args: object) -> None:
            return

        def do_POST(self) -> None:  # noqa: N802 - stdlib handler contract
            if self.path.rstrip("/") != "/v1/chat/completions":
                self.send_error(404)
                return
            length = int(self.headers.get("content-length") or "0")
            payload = json.loads(self.rfile.read(length))
            requests.append(payload)
            model = str(payload.get("model") or "kimi-k2.6")
            if mode == "timeout":
                threading.Event().wait(2.5)
            if mode == "empty":
                body = b""
            elif mode == "truncated":
                body = b'data: {"model":"kimi-k2.6","choices":['
            else:
                chunks = [
                    {
                        "model": model,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {
                                    "role": "assistant",
                                    "content": (
                                        "Synthetic reply without the required marker."
                                        if mode == "marker_missing"
                                        else "Synthetic delayed reply.\nATTACHMENT_CAPACITY_LIVE_OK"
                                    ),
                                },
                                "finish_reason": None,
                            }
                        ],
                    },
                    {
                        "model": model,
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 100, "completion_tokens": 5},
                    },
                ]
                body = b"".join(
                    f"data: {json.dumps(chunk, separators=(',', ':'))}\n\n".encode()
                    for chunk in chunks
                ) + b"data: [DONE]\n\n"
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                # Fault cases intentionally let the client close the synthetic stream.
                pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        result = e2e._run_tokenrhythm_attachment_capacity_in_temp(  # noqa: SLF001
            api_key="synthetic-attachment-capacity-key",
            base_url=f"http://{host}:{port}/v1",
            tmp_path=tmp_path,
            synthetic_vision_capability_override=True,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert result["ok"] is False
    assert len(requests) == 1
    case = result["cases"][0]
    assert case["failure_kind"] == expected_failure
    assert case["usage"]["physical_request_count"] == 1
    assert case["usage"]["physical_response_count"] == expected_response_count
    assert case["usage"]["compaction_count"] == 0
    assert "synthetic-attachment-capacity-key" not in json.dumps(result)


def test_attachment_capacity_absolute_deadline_never_forces_a_positive_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(e2e.time, "monotonic", lambda: 100.0)

    assert e2e._attachment_capacity_remaining_timeout(100.0, 75.0) == 0.0  # noqa: SLF001
    assert e2e._attachment_capacity_remaining_timeout(99.0, 75.0) == 0.0  # noqa: SLF001
    assert e2e._attachment_capacity_remaining_timeout(110.0, 75.0) == 10.0  # noqa: SLF001
    assert (  # noqa: SLF001
        e2e._attachment_capacity_remaining_timeout(
            110.0,
            75.0,
            overrun_reserve_seconds=4.0,
        )
        == 6.0
    )
    assert (
        e2e.ATTACHMENT_CAPACITY_TOTAL_TIMEOUT_SECONDS
        - e2e.ATTACHMENT_CAPACITY_GATEWAY_SHUTDOWN_TIMEOUT_SECONDS
        == 105.0
    )


def test_attachment_capacity_always_removes_raw_tree_after_exception(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    raw_root = tmp_path / "opensquilla-tokenrhythm-attachment-capacity-failure"

    def fake_mkdtemp(*, prefix: str) -> str:
        assert prefix == "opensquilla-tokenrhythm-attachment-capacity-"
        raw_root.mkdir()
        return str(raw_root)

    def fake_inner(**kwargs):
        root = kwargs["tmp_path"]
        (root / "turn-calls").mkdir()
        (root / "turn-calls" / "raw.jsonl").write_text("synthetic raw trace")
        raise RuntimeError("synthetic attachment failure")

    monkeypatch.setattr(e2e.tempfile, "mkdtemp", fake_mkdtemp)
    monkeypatch.setattr(e2e, "_run_tokenrhythm_attachment_capacity_in_temp", fake_inner)

    with pytest.raises(RuntimeError, match="synthetic attachment failure"):
        e2e._run_tokenrhythm_attachment_capacity("synthetic-cleanup-key")  # noqa: SLF001

    assert not raw_root.exists()


def test_attachment_capacity_removes_raw_tree_when_initial_chmod_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    raw_root = tmp_path / "opensquilla-tokenrhythm-attachment-capacity-chmod"
    cleanup_calls: list[Path] = []

    def fake_mkdtemp(*, prefix: str) -> str:
        assert prefix == "opensquilla-tokenrhythm-attachment-capacity-"
        raw_root.mkdir()
        return str(raw_root)

    def fail_chmod(_path: object, _mode: int) -> None:
        raise PermissionError("synthetic chmod failure")

    def fake_cleanup(path: object, _secrets: object) -> None:
        cleanup_calls.append(Path(path))
        raw_root.rmdir()

    monkeypatch.setattr(e2e.tempfile, "mkdtemp", fake_mkdtemp)
    monkeypatch.setattr(e2e.os, "chmod", fail_chmod)
    monkeypatch.setattr(e2e, "scan_and_remove_temporary_tree", fake_cleanup)

    with pytest.raises(PermissionError, match="synthetic chmod failure"):
        e2e._run_tokenrhythm_attachment_capacity("synthetic-cleanup-key")  # noqa: SLF001

    assert cleanup_calls == [raw_root]
    assert not raw_root.exists()


def _attachment_capacity_evidence_records(
    fixture: dict[str, object],
    session_key: str,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    retained = fixture["retained_base64"]
    assert isinstance(retained, list)
    current = fixture["current_attachment"]
    assert isinstance(current, dict)
    request = {
        "session_key": session_key,
        "kind": "llm_request",
        "provider": "tokenrhythm",
        "model": "kimi-k2.6",
        "payload": {
            "messages": [
                {"role": "system", "content": "bounded system"},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Historical image turn three."},
                        {"type": "image", "data": retained[0]},
                        {"type": "image", "data": retained[1]},
                    ],
                },
                {"role": "assistant", "content": "history answer"},
                {
                    "role": "user",
                    "content": "[Request context for this turn]\nsynthetic context",
                },
                {
                    "role": "user",
                    "content": [{"type": "image", "data": current["data"]}],
                },
            ],
            "config": {"model": "kimi-k2.6"},
        },
    }
    response = {
        "session_key": session_key,
        "kind": "llm_response",
        "provider": "tokenrhythm",
        "model": "kimi-k2.6",
        "payload": {
            "usage": {
                "model": "kimi-k2.6",
                "input_tokens": 123,
                "output_tokens": 7,
            }
        },
    }
    decision = {
        "session_key": session_key,
        "image_route_reason": "current_turn",
        "pipeline_steps": [
            {
                "step_name": "apply_squilla_router",
                "routing_source": "image_route",
                "routed_tier": "image_model",
            }
        ],
    }
    return [request, response], [decision]


@pytest.mark.parametrize("physical_request_count", [0, 1, 2])
def test_attachment_capacity_evidence_enforces_zero_or_one_call_boundary(
    physical_request_count: int,
) -> None:
    fixture = e2e._attachment_capacity_fixture()  # noqa: SLF001
    session_key = "agent:main:webchat:offline-evidence"
    records, decisions = _attachment_capacity_evidence_records(fixture, session_key)
    request = records[0]
    records = records[1:]
    records[:0] = [request for _ in range(physical_request_count)]

    evidence = e2e._evaluate_attachment_capacity_evidence(  # noqa: SLF001
        records=records,
        decisions=decisions,
        session_key=session_key,
        fixture=fixture,
        session_metrics={"compaction_count": 0},
        proof={
            "fits": True,
            "estimated_tokens": 1000,
            "effective_proof_token_budget": 80_000,
            "media_blocks": 3,
        },
        turn_error=None,
    )

    assert evidence["request_count"] == physical_request_count
    assert evidence["ok"] is (physical_request_count == 1)
    if physical_request_count == 1:
        assert evidence["response_count"] == 1
        assert evidence["actual_request_model"] == "kimi-k2.6"
        assert evidence["actual_response_model"] == "kimi-k2.6"
        assert evidence["request_projection"] == {
            "history_user_turn_count": 1,
            "media_blocks": 3,
            "excluded_old_media_absent": True,
            "expected_media_present": True,
            "expected_media_text_absent": True,
        }


@pytest.mark.parametrize("compaction_count", [0, 1])
def test_attachment_capacity_evidence_requires_no_compaction(
    compaction_count: int,
) -> None:
    fixture = e2e._attachment_capacity_fixture()  # noqa: SLF001
    session_key = "agent:main:webchat:offline-compaction-evidence"
    records, decisions = _attachment_capacity_evidence_records(fixture, session_key)

    evidence = e2e._evaluate_attachment_capacity_evidence(  # noqa: SLF001
        records=records,
        decisions=decisions,
        session_key=session_key,
        fixture=fixture,
        session_metrics={"compaction_count": compaction_count},
        proof={
            "fits": True,
            "estimated_tokens": 1000,
            "effective_proof_token_budget": 80_000,
            "media_blocks": 3,
        },
        turn_error=None,
    )

    assert evidence["ok"] is (compaction_count == 0)


@pytest.mark.parametrize(
    ("turn_error", "expected_failure"),
    [
        ("HTTP 401 unauthorized", "auth"),
        ("insufficient balance", "balance"),
        ("HTTP 403 forbidden", "not-entitled"),
        ("model kimi-k2.6 not found (404)", "model-unavailable"),
        ("HTTP 429 rate limit", "rate-limit"),
        ("HTTP 503 service unavailable", "transport"),
        ("provider request timed out", "transport"),
        (
            "Error: The model provider returned an invalid response. "
            "(ref: 40471b87)",
            "implementation",
        ),
        ("assistant completion marker missing", "implementation"),
    ],
)
def test_attachment_capacity_failure_taxonomy_is_preserved(
    turn_error: str,
    expected_failure: str,
) -> None:
    fixture = e2e._attachment_capacity_fixture()  # noqa: SLF001
    session_key = "agent:main:webchat:offline-failure-taxonomy"
    records, decisions = _attachment_capacity_evidence_records(fixture, session_key)

    evidence = e2e._evaluate_attachment_capacity_evidence(  # noqa: SLF001
        records=records,
        decisions=decisions,
        session_key=session_key,
        fixture=fixture,
        session_metrics={"compaction_count": 0},
        proof={"fits": True, "media_blocks": 3},
        turn_error=turn_error,
    )

    assert evidence["ok"] is False
    assert evidence["failure_kind"] == expected_failure


@pytest.mark.parametrize(
    ("safe_code", "expected_failure"),
    [
        ("401", "auth"),
        ("provider_auth_invalid", "auth"),
        ("402", "balance"),
        ("provider_insufficient_credits", "balance"),
        ("403", "not-entitled"),
        ("404", "model-unavailable"),
        ("provider_model_not_found", "model-unavailable"),
        ("429", "rate-limit"),
        ("provider_rate_limited", "rate-limit"),
        ("rate_limit", "rate-limit"),
        ("408", "transport"),
        ("501", "transport"),
        ("599", "transport"),
        ("503", "transport"),
        ("transport", "transport"),
        ("unavailable", "transport"),
        ("authentication", "auth"),
        ("not_found", "model-unavailable"),
        ("permission", "not-entitled"),
        ("provider_overloaded", "transport"),
        ("provider_transport_transient", "transport"),
        ("request_error", "transport"),
        ("timeout", "transport"),
    ],
)
def test_attachment_capacity_failure_taxonomy_uses_safe_llm_error_code(
    safe_code: str,
    expected_failure: str,
) -> None:
    fixture = e2e._attachment_capacity_fixture()  # noqa: SLF001
    session_key = "agent:main:webchat:offline-safe-error-code"
    records, decisions = _attachment_capacity_evidence_records(fixture, session_key)
    records.append(
        {
            "session_key": session_key,
            "kind": "llm_error",
            "provider": "tokenrhythm",
            "model": "kimi-k2.6",
            "payload": {
                "error": {
                    "code": safe_code,
                    "code_chars": len(safe_code),
                    "message_chars": 123,
                }
            },
        }
    )

    evidence = e2e._evaluate_attachment_capacity_evidence(  # noqa: SLF001
        records=records,
        decisions=decisions,
        session_key=session_key,
        fixture=fixture,
        session_metrics={"compaction_count": 0},
        proof={"fits": True, "media_blocks": 3},
        turn_error="The task failed before it could finish.",
    )

    assert evidence["ok"] is False
    assert evidence["failure_kind"] == expected_failure


@pytest.mark.parametrize(
    "broken_invariant",
    [
        "llm_error",
        "routing_source",
        "image_route_reason",
        "proof_fits",
        "proof_media_count",
        "input_usage",
        "output_usage",
        "old_media_leak",
        "retained_media_as_text",
    ],
)
def test_attachment_capacity_evidence_rejects_each_safety_invariant(
    broken_invariant: str,
) -> None:
    fixture = e2e._attachment_capacity_fixture()  # noqa: SLF001
    session_key = "agent:main:webchat:offline-broken-invariant"
    records, decisions = _attachment_capacity_evidence_records(fixture, session_key)
    proof = {"fits": True, "media_blocks": 3}

    request = records[0]
    response = records[1]
    if broken_invariant == "llm_error":
        records.append(
            {
                "session_key": session_key,
                "kind": "llm_error",
                "provider": "tokenrhythm",
                "model": "kimi-k2.6",
            }
        )
    elif broken_invariant == "routing_source":
        decisions[0]["pipeline_steps"][0]["routing_source"] = "classifier"
    elif broken_invariant == "image_route_reason":
        decisions[0]["image_route_reason"] = "gate_history"
    elif broken_invariant == "proof_fits":
        proof["fits"] = False
    elif broken_invariant == "proof_media_count":
        proof["media_blocks"] = 2
    elif broken_invariant == "input_usage":
        response["payload"]["usage"]["input_tokens"] = 0
    elif broken_invariant == "output_usage":
        response["payload"]["usage"]["output_tokens"] = 0
    elif broken_invariant == "old_media_leak":
        request["payload"]["messages"][0]["content"] += fixture["excluded_base64"][0]
    elif broken_invariant == "retained_media_as_text":
        request["payload"]["messages"][1]["content"] = "".join(
            fixture["retained_base64"]
        )

    evidence = e2e._evaluate_attachment_capacity_evidence(  # noqa: SLF001
        records=records,
        decisions=decisions,
        session_key=session_key,
        fixture=fixture,
        session_metrics={"compaction_count": 0},
        proof=proof,
        turn_error=None,
    )

    assert evidence["ok"] is False
    assert evidence["failure_kind"] == "implementation"


def test_attachment_capacity_waits_for_ready_not_only_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payloads = iter(({"ready": False}, {"ready": True, "status": "ready"}))
    urls: list[str] = []

    class Proc:
        returncode = None

        @staticmethod
        def poll() -> None:
            return None

    class Response:
        def __init__(self, payload: dict[str, object]) -> None:
            self._body = json.dumps(payload).encode()

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return self._body

    def fake_urlopen(url: str, *, timeout: float) -> Response:
        assert timeout > 0
        urls.append(url)
        return Response(next(payloads))

    monkeypatch.setattr(e2e.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(e2e.time, "sleep", lambda _seconds: None)

    ready, error = e2e._wait_for_attachment_gateway_ready(  # noqa: SLF001
        Proc(),  # type: ignore[arg-type]
        49152,
        timeout_seconds=1.0,
    )

    assert error is None
    assert ready == {"ready": True, "status": "ready"}
    assert urls == ["http://127.0.0.1:49152/ready"] * 2


def test_attachment_capacity_readiness_reports_early_gateway_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Proc:
        returncode = 17

        @staticmethod
        def poll() -> int:
            return 17

    monkeypatch.setattr(
        e2e.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: pytest.fail("readiness must stop after process exit"),
    )

    ready, error = e2e._wait_for_attachment_gateway_ready(  # noqa: SLF001
        Proc(),  # type: ignore[arg-type]
        49152,
        timeout_seconds=1.0,
    )

    assert ready is None
    assert error == "gateway exited early with code 17 before readiness"


def test_attachment_capacity_readiness_times_out_when_ready_stays_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Clock:
        value = -0.1

        def __call__(self) -> float:
            self.value += 0.1
            return self.value

    class Proc:
        returncode = None

        @staticmethod
        def poll() -> None:
            return None

    class Response:
        @staticmethod
        def __enter__():
            return Response()

        @staticmethod
        def __exit__(*_args: object) -> None:
            return None

        @staticmethod
        def read() -> bytes:
            return b'{"ready":false}'

    monkeypatch.setattr(e2e.time, "monotonic", Clock())
    monkeypatch.setattr(e2e.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        e2e.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: Response(),
    )

    ready, error = e2e._wait_for_attachment_gateway_ready(  # noqa: SLF001
        Proc(),  # type: ignore[arg-type]
        49152,
        timeout_seconds=0.5,
    )

    assert ready is None
    assert error == "gateway did not become ready before timeout"


def test_attachment_capacity_upload_rejects_invalid_file_uuid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Response:
        @staticmethod
        def __enter__():
            return Response()

        @staticmethod
        def __exit__(*_args: object) -> None:
            return None

        @staticmethod
        def read() -> bytes:
            return b'{"file_uuid":"not-an-upload-id"}'

    monkeypatch.setattr(
        e2e.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: Response(),
    )

    with pytest.raises(RuntimeError, match="valid file_uuid"):
        e2e._upload_inline_attachment(  # noqa: SLF001
            port=49152,
            attachment=e2e._attachment_capacity_fixture()["current_attachment"],  # noqa: SLF001
            timeout=1.0,
        )


@pytest.mark.parametrize(
    ("request_model", "response_model"),
    [
        ("", "kimi-k2.6"),
        ("wrong-model", "kimi-k2.6"),
        ("kimi-k2.6", ""),
        ("kimi-k2.6", "wrong-model"),
    ],
)
def test_attachment_capacity_requires_independent_request_and_response_models(
    request_model: str,
    response_model: str,
) -> None:
    fixture = e2e._attachment_capacity_fixture()  # noqa: SLF001
    session_key = "agent:main:webchat:offline-model-evidence"
    records, decisions = _attachment_capacity_evidence_records(fixture, session_key)
    records[0]["model"] = request_model
    response_usage = records[1]["payload"]["usage"]
    assert isinstance(response_usage, dict)
    response_usage["model"] = response_model

    evidence = e2e._evaluate_attachment_capacity_evidence(  # noqa: SLF001
        records=records,
        decisions=decisions,
        session_key=session_key,
        fixture=fixture,
        session_metrics={"compaction_count": 0},
        proof={"fits": True, "media_blocks": 3},
        turn_error=None,
    )

    assert evidence["actual_request_model"] == request_model
    assert evidence["actual_response_model"] == response_model
    assert evidence["ok"] is False


def test_attachment_capacity_proof_parser_keeps_only_scalar_evidence(tmp_path: Path) -> None:
    log_path = tmp_path / "debug.log"
    log_path.write_text(
        "provider.request_proof estimated_tokens=37720 "
        "effective_proof_token_budget=87704 media_blocks_reserved=3 fits=True "
        "top_contributors=['must-not-survive']\n",
        encoding="utf-8",
    )

    proof = e2e._provider_proof_from_logs([log_path])  # noqa: SLF001

    assert proof == {
        "estimated_tokens": 37_720,
        "effective_proof_token_budget": 87_704,
        "media_blocks": 3,
        "fits": True,
    }
    assert "must-not-survive" not in json.dumps(proof)


def test_attachment_capacity_http_error_parser_keeps_only_unique_status_scalars(
    tmp_path: Path,
) -> None:
    stdout = tmp_path / "gateway.stdout.log"
    debug = tmp_path / "debug.log"
    stdout.write_text(
        "provider.chat_http_error provider='tokenrhythm' model='kimi-k2.6' "
        "status_code=429 response_body_chars=88\n"
        "unrelated status_code=401\n"
        "provider.chat_http_error status_code=399\n",
        encoding="utf-8",
    )
    debug.write_text(
        "provider.chat_http_error provider='tokenrhythm' model='kimi-k2.6' "
        "status_code=429 response_body_chars=88\n"
        "provider.chat_http_error provider='tokenrhythm' model='kimi-k2.6' "
        "status_code=503 response_body_chars=100\n",
        encoding="utf-8",
    )

    statuses = e2e._attachment_capacity_provider_http_statuses(  # noqa: SLF001
        [stdout, debug, tmp_path / "missing.log"]
    )

    assert statuses == [429, 503]


@pytest.mark.parametrize(
    ("confirm", "opt_in", "key", "expected"),
    [
        (False, "1", "synthetic-key", "--confirm-live-cost"),
        (True, None, "synthetic-key", e2e.ATTACHMENT_CAPACITY_OPT_IN_ENV),
        (True, "1", None, "TOKENRHYTHM_API_KEY"),
    ],
)
def test_attachment_capacity_main_requires_all_three_live_gates(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    confirm: bool,
    opt_in: str | None,
    key: str | None,
    expected: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = tmp_path / "attachment-capacity.json"
    argv = [
        "live_provider_profile_gateway_e2e.py",
        "--attachment-capacity",
        "--output",
        str(output),
    ]
    if confirm:
        argv.append("--confirm-live-cost")
    monkeypatch.setattr(sys, "argv", argv)
    if opt_in is None:
        monkeypatch.delenv(e2e.ATTACHMENT_CAPACITY_OPT_IN_ENV, raising=False)
    else:
        monkeypatch.setenv(e2e.ATTACHMENT_CAPACITY_OPT_IN_ENV, opt_in)
    if key is None:
        monkeypatch.delenv("TOKENRHYTHM_API_KEY", raising=False)
    else:
        monkeypatch.setenv("TOKENRHYTHM_API_KEY", key)
    monkeypatch.setattr(
        e2e,
        "_run_tokenrhythm_attachment_capacity",
        lambda _api_key: pytest.fail("live runner must not start before every gate passes"),
    )

    with pytest.raises(SystemExit):
        e2e.main()

    assert expected in capsys.readouterr().err
    assert not output.exists()


def test_attachment_capacity_main_runs_only_single_tokenrhythm_scenario(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = tmp_path / "attachment-capacity.json"
    synthetic_key = "synthetic-attachment-live-key"
    captured: list[str] = []
    monkeypatch.setenv(e2e.ATTACHMENT_CAPACITY_OPT_IN_ENV, "1")
    monkeypatch.setenv("TOKENRHYTHM_API_KEY", synthetic_key)
    monkeypatch.setattr(
        e2e,
        "_load_env_quietly",
        lambda *_args, **_kwargs: pytest.fail("attachment gate must not load .env"),
    )
    monkeypatch.setattr(
        e2e,
        "_run_provider",
        lambda *_args, **_kwargs: pytest.fail("attachment gate must not run c0-c3 matrix"),
    )

    def fake_attachment_run(api_key: str) -> dict[str, object]:
        captured.append(api_key)
        return {
            "provider": "tokenrhythm",
            "ok": True,
            "models_covered": ["kimi-k2.6"],
            "failure_kinds": [],
            "cases": [
                {
                    "ok": True,
                    "actual_request_model": "kimi-k2.6",
                    "actual_response_model": "kimi-k2.6",
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 5,
                        "physical_request_count": 1,
                        "compaction_count": 0,
                        "provider_proof_fits": True,
                    },
                    "cost": {"opensquilla_estimated_cost_usd": 0.001},
                    "latency_ms": 12,
                }
            ],
        }

    monkeypatch.setattr(e2e, "_run_tokenrhythm_attachment_capacity", fake_attachment_run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "live_provider_profile_gateway_e2e.py",
            "--attachment-capacity",
            "--confirm-live-cost",
            "--output",
            str(output),
        ],
    )

    assert e2e.main() == 0
    assert captured == [synthetic_key]
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload[0]["provider"] == "tokenrhythm"
    assert payload[0]["model"] == "kimi-k2.6"
    assert payload[0]["usage"]["physical_request_count"] == 1
    assert "TOKENRHYTHM_API_KEY" not in os.environ
    rendered = output.read_text(encoding="utf-8") + capsys.readouterr().out
    assert synthetic_key not in rendered
