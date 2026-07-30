from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from scripts import live_harness_security as security


def _load_matrix_module():
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "live_multi_provider_matrix.py"
    spec = importlib.util.spec_from_file_location("live_multi_provider_matrix", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


matrix = _load_matrix_module()


def _execution(
    stage: str,
    *,
    model: str = "gpt-5.4-mini",
    failure_text: str | None = None,
) -> dict[str, Any]:
    if failure_text is not None:
        return {
            "returncode": 1,
            "latency_ms": 7,
            "report": {"ok": False, "results": [{"error": failure_text}]},
            "stdout": "",
            "stderr": "",
            "spawn_failure": False,
            "timeout": False,
        }
    if stage in matrix._PROBE_STAGES:
        return {
            "returncode": 1,
            "latency_ms": 3,
            "report": {
                "ok": False,
                "results": [
                    {
                        "direct_status": "content_mismatch",
                        "stream_status": "skipped",
                        "response_model": model,
                        "usage": {"direct": {"prompt_tokens": 8, "completion_tokens": 1}},
                        "cost": {},
                    }
                ],
            },
            "stdout": "",
            "stderr": "",
            "spawn_failure": False,
            "timeout": False,
        }
    if stage in matrix._STREAM_STAGES:
        return {
            "returncode": 0,
            "latency_ms": 11,
            "report": {
                "ok": True,
                "results": [
                    {
                        "direct_status": "passed",
                        "stream_status": "passed",
                        "response_model": model,
                        "usage": {
                            "direct": {"prompt_tokens": 8, "completion_tokens": 4},
                            "stream": {"input_tokens": 8, "output_tokens": 4},
                        },
                        "cost": {"opensquilla_estimated_cost_usd": 0.00001},
                    }
                ],
            },
            "stdout": "",
            "stderr": "",
            "spawn_failure": False,
            "timeout": False,
        }
    if stage in matrix._SPECIAL_STAGES:
        return {
            "returncode": 0,
            "latency_ms": 17,
            "report": {
                "ok": True,
                "results": [
                    {
                        "status": "passed",
                        "model": model,
                        "usage": {
                            "input_tokens": 9,
                            "output_tokens": 3,
                            "reasoning_tokens": 1 if stage == "thinking_on" else 0,
                        },
                        "cost": {"opensquilla_estimated_cost_usd": 0.00001},
                        "done_event": True,
                        "marker_verified": True,
                    }
                ],
            },
            "stdout": "",
            "stderr": "",
            "spawn_failure": False,
            "timeout": False,
        }
    assert stage == "gateway_main"
    return {
        "returncode": 0,
        "latency_ms": 13,
        "report": {
            "ok": True,
            "results": [
                {
                    "status": "passed",
                    "model": model,
                    "usage": {"input_tokens": 9, "output_tokens": 3},
                    "cost": {"opensquilla_estimated_cost_usd": 0.00001},
                }
            ],
        },
        "stdout": "",
        "stderr": "",
        "spawn_failure": False,
        "timeout": False,
    }


def test_fixed_provider_inventory_remains_stable() -> None:
    assert matrix.DEFAULT_PROVIDERS == (
        "dashscope",
        "openai",
        "deepseek",
        "gemini",
        "moonshot",
        "zhipu",
        "volcengine",
        "qianfan",
        "minimax",
    )
    assert matrix.EXPLICIT_EMPTY_PROVIDERS == (
        "openrouter",
        "siliconflow",
        "groq",
        "mistral",
        "byteplus",
        "aihubmix",
    )


def test_provider_keys_parser_is_literal_allowlisted_and_reports_hygiene(
    tmp_path: Path,
) -> None:
    should_not_exist = tmp_path / "shell-was-evaluated"
    source = tmp_path / "provider.keys"
    source.write_text(
        "\n".join(
            [
                "not an assignment",
                f"OPENAI_API_KEY=$(touch {should_not_exist})",
                "export DEEPSEEK_API_KEY='deepseek secret'",
                "OPENAI_MODEL=gpt-5.4-mini",
                "DEEPSEEK_REASONER_MODEL=deepseek-v4-pro",
                "OPENAI_BASE_URL=https://attacker.invalid/v1",
                "UNLISTED_MODEL=private/model",
                "GEMINI_MODEL=$(touch-bad)",
                "# comment",
                "",
            ]
        ),
        encoding="utf-8",
    )
    source.chmod(0o644)

    inventory = security.parse_provider_keys_file(source)

    assert inventory.secrets == {
        "OPENAI_API_KEY": f"$(touch {should_not_exist})",
        "DEEPSEEK_API_KEY": "deepseek secret",
    }
    assert inventory.models == {
        "OPENAI_MODEL": "gpt-5.4-mini",
        "DEEPSEEK_REASONER_MODEL": "deepseek-v4-pro",
    }
    assert inventory.ignored_line_numbers == (1, 6, 7, 8)
    if os.name != "nt":
        assert inventory.file_mode == 0o644
        assert inventory.permission_warning is not None
        assert "0644" in inventory.permission_warning
    else:
        assert inventory.permission_warning is None
    assert not should_not_exist.exists()


def test_legacy_secrets_parser_remains_silent_and_does_not_mutate_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "provider.keys"
    source.write_text(
        "OPENAI_API_KEY=literal\nBROKEN PRIVATE LINE\nOPENAI_BASE_URL=https://bad.invalid\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    parsed = security.parse_secrets_file(source, allowed_names={"OPENAI_API_KEY"})

    assert parsed == {"OPENAI_API_KEY": "literal"}
    assert "OPENAI_API_KEY" not in os.environ
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_provider_keys_parser_does_not_warn_for_private_mode(tmp_path: Path) -> None:
    source = tmp_path / "provider.keys"
    source.write_text("OPENAI_API_KEY=value\n", encoding="utf-8")
    source.chmod(0o600)

    inventory = security.parse_provider_keys_file(source)

    if os.name != "nt":
        assert inventory.file_mode == 0o600
    assert inventory.permission_warning is None


def test_registry_endpoint_policy_rejects_operator_overrides() -> None:
    assert security.registry_endpoint("openai") == "https://api.openai.com/v1"
    assert (
        security.registry_endpoint("minimax")
        == "https://api.minimaxi.com/anthropic"
    )
    with pytest.raises(ValueError, match="endpoint override rejected"):
        security.registry_endpoint("openai", "https://attacker.invalid/v1")
    with pytest.raises(ValueError, match="no runnable registry endpoint"):
        security.registry_endpoint("custom")


def test_child_environment_contains_only_selected_registry_credential() -> None:
    env = security.child_environment(
        "openai",
        {
            "OPENAI_API_KEY": "selected-openai-secret",
            "DEEPSEEK_API_KEY": "other-provider-secret",
        },
        base_environment={
            "PATH": "/usr/bin",
            "HOME": "/must/not/pass",
            "HTTP_PROXY": "http://proxy.invalid",
            "HTTPS_PROXY": "http://proxy.invalid",
            "ALL_PROXY": "socks5://proxy.invalid",
            "NO_PROXY": "*",
            "GITHUB_TOKEN": "unrelated-token",
            "OPENSQUILLA_UNRELATED_OVERRIDE": "must-not-pass",
            "OPENAI_API_KEY": "ambient-openai-secret",
            "DEEPSEEK_API_KEY": "ambient-deepseek-secret",
            "OPENAI_MODEL": "ambient-model",
            "OPENAI_BASE_URL": "https://attacker.invalid/v1",
            "OPENSQUILLA_LLM_API_KEY": "ambient-generic-secret",
            "OPENSQUILLA_LLM_BASE_URL": "https://attacker.invalid/v1",
        },
    )

    assert env["OPENAI_API_KEY"] == "selected-openai-secret"
    assert "DEEPSEEK_API_KEY" not in env
    assert "OPENAI_MODEL" not in env
    assert "OPENAI_BASE_URL" not in env
    assert "OPENSQUILLA_LLM_API_KEY" not in env
    assert "OPENSQUILLA_LLM_BASE_URL" not in env
    assert "HOME" not in env
    assert "HTTP_PROXY" not in env
    assert "HTTPS_PROXY" not in env
    assert "ALL_PROXY" not in env
    assert "NO_PROXY" not in env
    assert "GITHUB_TOKEN" not in env
    assert "OPENSQUILLA_UNRELATED_OVERRIDE" not in env
    assert env["OPENSQUILLA_LIVE_DISABLE_DOTENV"] == "1"


def test_minimal_child_environment_handles_windows_key_casing() -> None:
    env = security.minimal_child_environment(
        {
            "Path": r"C:\bin",
            "SystemRoot": r"C:\Windows",
            "ComSpec": r"C:\Windows\System32\cmd.exe",
            "USERPROFILE": r"C:\SyntheticHome",
            "http_proxy": "http://proxy.invalid",
            "OPENSQUILLA_LLM_MODEL": "ambient-override",
        }
    )

    assert env["Path"] == r"C:\bin"
    assert env["SystemRoot"] == r"C:\Windows"
    assert env["ComSpec"].endswith("cmd.exe")
    assert "PATH" not in env  # no duplicate fallback when Path already exists
    assert "USERPROFILE" not in env
    assert "http_proxy" not in env
    assert "OPENSQUILLA_LLM_MODEL" not in env


def test_safe_report_writer_redacts_scans_and_uses_private_mode(tmp_path: Path) -> None:
    secret = "provider-secret-value"
    output = tmp_path / "report.json"

    safe = security.write_safe_report(
        output,
        {
            "provider": "openai",
            "model": "gpt-5.4-mini",
            "status": "failed",
            "failure_class": f"Authorization: Bearer {secret}",
            "api_key": "another-sensitive-value",
        },
        {"OPENAI_API_KEY": secret},
    )

    serialized = output.read_text(encoding="utf-8")
    assert secret not in serialized
    assert "another-sensitive-value" not in serialized
    assert not security.report_contains_secret(safe, {"OPENAI_API_KEY": secret})
    if os.name != "nt":
        assert output.stat().st_mode & 0o777 == 0o600
    assert list(tmp_path.glob(".report.json.tmp-*")) == []


def test_safe_report_writer_rejects_non_temporary_destination() -> None:
    with pytest.raises(ValueError, match="temporary directory"):
        security.write_safe_report(
            Path("report-outside-system-temp.json"),
            {"ok": True},
            {},
        )


def test_temporary_tree_cleanup_scans_and_removes_clean_artifacts(tmp_path: Path) -> None:
    root = tmp_path / "opensquilla-clean-live-artifacts"
    root.mkdir()
    (root / "gateway.log").write_text("synthetic public output", encoding="utf-8")

    security.scan_and_remove_temporary_tree(
        root,
        {"OPENAI_API_KEY": "offline-secret-not-present"},
    )

    assert not root.exists()


def test_temporary_tree_cleanup_detects_cross_chunk_secret_then_removes(
    tmp_path: Path,
) -> None:
    secret = "offline-cross-chunk-provider-secret"
    root = tmp_path / "opensquilla-leaking-live-artifacts"
    root.mkdir()
    (root / "gateway.log").write_bytes(b"x" * (64 * 1024 - 5) + secret.encode("utf-8"))

    with pytest.raises(RuntimeError, match="credential detected"):
        security.scan_and_remove_temporary_tree(root, {"OPENAI_API_KEY": secret})

    assert not root.exists()


def test_temporary_tree_cleanup_refuses_unowned_or_symlink_roots(tmp_path: Path) -> None:
    unowned = tmp_path / "other-tool-artifacts"
    unowned.mkdir()
    with pytest.raises(ValueError, match="non-owned"):
        security.scan_and_remove_temporary_tree(unowned, {})
    assert unowned.is_dir()

    target = tmp_path / "outside-target"
    target.mkdir()
    link = tmp_path / "opensquilla-symlink-root"
    try:
        link.symlink_to(target, target_is_directory=True)
    except (NotImplementedError, OSError):
        return
    with pytest.raises(ValueError, match="link"):
        security.scan_and_remove_temporary_tree(link, {})
    assert link.is_symlink()
    assert target.is_dir()


def test_temporary_tree_cleanup_never_suppresses_scan_or_delete_failures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    scan_root = tmp_path / "opensquilla-scan-failure"
    scan_root.mkdir()
    monkeypatch.setattr(
        security,
        "_temporary_tree_contains_secret",
        lambda *_args: (_ for _ in ()).throw(OSError("synthetic scan failure")),
    )
    with pytest.raises(RuntimeError, match="unable to scan"):
        security.scan_and_remove_temporary_tree(scan_root, {"OPENAI_API_KEY": "offline"})
    assert not scan_root.exists()

    delete_root = tmp_path / "opensquilla-delete-failure"
    delete_root.mkdir()
    monkeypatch.setattr(security, "_temporary_tree_contains_secret", lambda *_args: False)
    monkeypatch.setattr(
        security.shutil,
        "rmtree",
        lambda _path: (_ for _ in ()).throw(OSError("synthetic delete failure")),
    )
    with pytest.raises(OSError, match="synthetic delete failure"):
        security.scan_and_remove_temporary_tree(delete_root, {})
    assert delete_root.is_dir()


@pytest.mark.parametrize(
    "model",
    [
        "gpt-4o",
        "gpt-4.1",
        "gpt-5",
        "gpt-5.4-pro",
        "o1-mini",
        "o3",
        "o4-mini",
        "anthropic/claude-sonnet-4.5",
        "claude-opus-4.8",
        "gemini-3.1-pro-preview",
        "gemini-ultra",
        "qwen3.7-max",
    ],
)
def test_premium_model_guard_rejects_expensive_families(model: str) -> None:
    assert security.is_premium_model(model) is True


@pytest.mark.parametrize("model", ["gpt-4o-mini", "gpt-5.4-mini", "claude-haiku-4.5"])
def test_premium_model_guard_keeps_low_cost_variants(model: str) -> None:
    assert security.is_premium_model(model) is False


def test_stage_commands_are_secret_free_fixed_and_registry_only(tmp_path: Path) -> None:
    secret = "must-never-enter-argv"
    probe = matrix._stage_command(
        "probe",
        "openai",
        "gpt-5.4-mini",
        tmp_path / "probe.json",
        smoke_max_tokens=64,
        gateway_max_tokens=64,
        gateway_timeout_seconds=1.0,
    )
    responses = matrix._stage_command(
        "openai_responses_stream",
        "openai",
        "gpt-5.4-mini",
        tmp_path / "responses.json",
        smoke_max_tokens=64,
        gateway_max_tokens=64,
        gateway_timeout_seconds=1.0,
    )
    gateway = matrix._stage_command(
        "gateway_main",
        "openai",
        "gpt-5.4-mini",
        tmp_path / "gateway.json",
        smoke_max_tokens=64,
        gateway_max_tokens=64,
        gateway_timeout_seconds=1.0,
    )
    deep = matrix._stage_command(
        "deep_multi_model",
        "deepseek",
        "deepseek-v4-flash",
        tmp_path / "deep.json",
        smoke_max_tokens=32,
        gateway_max_tokens=64,
        gateway_timeout_seconds=1.0,
    )
    thinking = matrix._stage_command(
        "thinking_on",
        "deepseek",
        "deepseek-v4-flash",
        tmp_path / "thinking.json",
        smoke_max_tokens=32,
        gateway_max_tokens=64,
        gateway_timeout_seconds=1.0,
        special_max_tokens=128,
    )
    vision = matrix._stage_command(
        "vision_synthetic_color_block",
        "gemini",
        "gemini-3.5-flash",
        tmp_path / "vision.json",
        smoke_max_tokens=32,
        gateway_max_tokens=64,
        gateway_timeout_seconds=1.0,
        special_max_tokens=256,
    )

    assert secret not in " ".join([*probe, *responses, *gateway, *deep, *thinking, *vision])
    assert probe[probe.index("--max-tokens") + 1] == "1"
    assert "--skip-stream" in probe
    assert "--exact-max-tokens" in probe
    assert "--child-report" in probe
    assert responses[responses.index("--provider") + 1] == "openai_responses"
    assert responses[responses.index("--base-url") + 1] == security.registry_endpoint(
        "openai_responses"
    )
    assert gateway[gateway.index("--gateway-max-tokens") + 1] == "64"
    assert "live_provider_profile_gateway_e2e.py" not in " ".join(gateway)
    assert deep[deep.index("--max-tokens") + 1] == "32"
    assert thinking[thinking.index("--special-max-tokens") + 1] == "128"
    assert vision[vision.index("--special-max-tokens") + 1] == "256"
    assert "--child-special" in thinking
    assert "--child-special" in vision


def test_matrix_probe_gates_then_runs_chat_responses_stream_and_one_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "child-only-openai-secret"
    calls: list[dict[str, Any]] = []

    def fake_run_stage(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        command = kwargs["command"]
        return _execution(
            kwargs["stage"],
            model=command[command.index("--model") + 1],
        )

    monkeypatch.setattr(matrix, "_run_stage", fake_run_stage)
    report = matrix.run_matrix(
        providers=["openai"],
        secrets={"OPENAI_API_KEY": secret},
        models={"OPENAI_MODEL": "gpt-5.4-mini"},
        smoke_max_tokens=64,
        gateway_max_tokens=64,
        gateway_timeout_seconds=1.0,
        stage_timeout_seconds=2.0,
        base_environment={"PATH": os.environ.get("PATH", "")},
    )

    assert [call["stage"] for call in calls] == [
        *matrix.STAGE_ORDER,
        "deep_multi_model",
        "deep_multi_model",
        "thinking_off",
        "thinking_on",
    ]
    assert all(call["env"]["OPENAI_API_KEY"] == secret for call in calls)
    assert all(secret not in " ".join(call["command"]) for call in calls)
    assert report["ok"] is True
    assert report["providers"][0]["status"] == "passed"
    assert [stage["status"] for stage in report["providers"][0]["stages"]] == [
        "passed",
        "passed",
        "passed",
        "passed",
        "passed",
    ]
    assert report["deep_provider_selection"] == ["openai"]
    assert [row["model"] for row in report["deep_multi_model"]] == [
        "gpt-5.4-mini",
        "gpt-5.4-nano",
    ]
    assert [row["stage"] for row in report["thinking"]] == [
        "thinking_off",
        "thinking_on",
    ]
    assert secret not in json.dumps(report)


def test_probe_auth_failure_stops_all_later_network_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_run_stage(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs["stage"])
        return _execution(kwargs["stage"], failure_text="HTTP 401 unauthorized")

    monkeypatch.setattr(matrix, "_run_stage", fake_run_stage)
    report = matrix.run_matrix(
        providers=["openai"],
        secrets={"OPENAI_API_KEY": "present"},
        smoke_max_tokens=32,
        gateway_max_tokens=64,
        gateway_timeout_seconds=1.0,
        stage_timeout_seconds=2.0,
        base_environment={"PATH": os.environ.get("PATH", "")},
    )

    assert calls == ["probe"]
    result = report["providers"][0]
    assert result["failure_class"] == "auth"
    assert result["stages"][0]["status"] == "failed"
    assert all(stage["failure_class"] == "blocked-by-probe" for stage in result["stages"][1:])


def test_transient_probe_retries_exactly_once_then_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_run_stage(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs["stage"])
        return _execution(kwargs["stage"], failure_text="HTTP 429 rate limit")

    monkeypatch.setattr(matrix, "_run_stage", fake_run_stage)
    report = matrix.run_matrix(
        providers=["deepseek"],
        secrets={"DEEPSEEK_API_KEY": "present"},
        smoke_max_tokens=32,
        gateway_max_tokens=64,
        gateway_timeout_seconds=1.0,
        stage_timeout_seconds=2.0,
        base_environment={"PATH": os.environ.get("PATH", "")},
    )

    assert calls == ["probe", "probe"]
    assert report["providers"][0]["failure_class"] == "rate-limit"


def test_missing_expected_key_is_skip_and_never_starts_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        matrix,
        "_run_stage",
        lambda **kwargs: pytest.fail("missing credentials must not start a child process"),
    )

    report = matrix.run_matrix(
        providers=["qianfan"],
        secrets={},
        smoke_max_tokens=32,
        gateway_max_tokens=64,
        gateway_timeout_seconds=1.0,
        stage_timeout_seconds=2.0,
        base_environment={
            "PATH": os.environ.get("PATH", ""),
            "OPENROUTER_API_KEY": "ambient-must-not-substitute",
        },
    )

    assert report["ok"] is False
    assert report["providers"][0]["status"] == "skipped"
    assert report["providers"][0]["failure_class"] == "missing-credential"
    assert [row["provider"] for row in report["explicit_empty_provider_inventory"]] == list(
        matrix.EXPLICIT_EMPTY_PROVIDERS
    )
    assert all(
        row["status"] == "skipped"
        for row in report["explicit_empty_provider_inventory"]
    )


@pytest.mark.parametrize(
    ("diagnostic", "expected"),
    [
        ("HTTP 401 unauthorized", "auth"),
        ("insufficient balance", "balance"),
        ("HTTP 403 not entitled", "not-entitled"),
        ("model does not exist", "model-unavailable"),
        ("HTTP 429 rate limit", "rate-limit"),
        ("ConnectError: DNS failure", "transport"),
        ("unexpected response schema", "implementation"),
    ],
)
def test_failure_classification_is_bounded(diagnostic: str, expected: str) -> None:
    assert matrix._failure_class_from_text(diagnostic) == expected


def test_token_budgets_are_enforced_before_any_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        matrix,
        "_run_stage",
        lambda **kwargs: pytest.fail("invalid budget must fail before child process"),
    )
    common = {
        "providers": ["openai"],
        "secrets": {"OPENAI_API_KEY": "present"},
        "gateway_timeout_seconds": 1.0,
        "stage_timeout_seconds": 2.0,
    }
    with pytest.raises(ValueError, match="smoke_max_tokens"):
        matrix.run_matrix(**common, smoke_max_tokens=65, gateway_max_tokens=64)
    with pytest.raises(ValueError, match="gateway_max_tokens"):
        matrix.run_matrix(**common, smoke_max_tokens=64, gateway_max_tokens=65)
    with pytest.raises(ValueError, match="special_max_tokens"):
        matrix.run_matrix(
            **common,
            smoke_max_tokens=64,
            gateway_max_tokens=64,
            special_max_tokens=257,
        )


def test_deep_models_use_file_then_repo_c0_c2_dedup_and_skip_premium() -> None:
    assert matrix._deep_models("deepseek", {"DEEPSEEK_MODEL": "custom-low-model"}) == (
        "custom-low-model",
        "deepseek-v4-flash",
        "deepseek-v4-pro",
    )
    assert matrix._deep_models("openai", {"OPENAI_MODEL": "gpt-5.5"}) == (
        "gpt-5.4-mini",
        "gpt-5.4-nano",
    )
    assert matrix._deep_models("gemini", {"GEMINI_MODEL": "gemini-3.1-flash-lite"}) == (
        "gemini-3.1-flash-lite",
    )


def test_deep_provider_selection_fills_failed_priority_slots_in_fixed_order() -> None:
    selected = matrix._selected_deep_providers(
        {"openai", "dashscope", "moonshot", "volcengine", "qianfan"}
    )

    assert selected == ("openai", "dashscope", "moonshot", "volcengine", "qianfan")


def test_full_inventory_cannot_pass_without_required_deep_coverage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run_stage(**kwargs: Any) -> dict[str, Any]:
        command = kwargs["command"]
        return _execution(
            kwargs["stage"],
            model=command[command.index("--model") + 1],
        )

    monkeypatch.setattr(matrix, "_run_stage", fake_run_stage)
    monkeypatch.setattr(
        matrix,
        "_deep_case_rows",
        lambda **kwargs: ((), [], [], []),
    )
    secrets = {
        matrix.get_provider_spec(provider).env_key: "present"
        for provider in matrix.DEFAULT_PROVIDERS
    }

    report = matrix.run_matrix(
        providers=list(matrix.DEFAULT_PROVIDERS),
        secrets=secrets,
        smoke_max_tokens=32,
        gateway_max_tokens=32,
        gateway_timeout_seconds=1.0,
        stage_timeout_seconds=2.0,
        special_max_tokens=128,
        base_environment={"PATH": os.environ.get("PATH", "")},
    )

    assert all(row["status"] == "passed" for row in report["providers"])
    assert report["deep_coverage_complete"] is False
    assert report["ok"] is False


def test_deep_failure_does_not_interrupt_remaining_models_or_providers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, str]] = []

    def fake_run_stage(**kwargs: Any) -> dict[str, Any]:
        stage = kwargs["stage"]
        provider = kwargs["provider"]
        command = kwargs["command"]
        model = command[command.index("--model") + 1]
        calls.append((stage, provider, model))
        if stage == "deep_multi_model" and model == "deepseek-v4-pro":
            return _execution(stage, failure_text="model does not exist")
        return _execution(stage, model=model)

    monkeypatch.setattr(matrix, "_run_stage", fake_run_stage)
    report = matrix.run_matrix(
        providers=["deepseek", "openai"],
        secrets={"DEEPSEEK_API_KEY": "one", "OPENAI_API_KEY": "two"},
        smoke_max_tokens=32,
        gateway_max_tokens=32,
        gateway_timeout_seconds=1.0,
        stage_timeout_seconds=2.0,
        special_max_tokens=128,
        base_environment={"PATH": os.environ.get("PATH", "")},
    )

    assert report["ok"] is False
    assert any(
        row["provider"] == "deepseek"
        and row["model"] == "deepseek-v4-pro"
        and row["failure_class"] == "model-unavailable"
        for row in report["deep_multi_model"]
    )
    assert ("deep_multi_model", "openai", "gpt-5.4-nano") in calls
    assert ("thinking_on", "openai", "gpt-5.4-mini") in calls


def test_failed_priority_probe_uses_probe_passing_fallback_for_deep_cases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []

    def fake_run_stage(**kwargs: Any) -> dict[str, Any]:
        stage = kwargs["stage"]
        provider = kwargs["provider"]
        calls.append((stage, provider))
        if stage == "probe" and provider == "deepseek":
            return _execution(stage, failure_text="HTTP 401 unauthorized")
        return _execution(stage, model="kimi-k2.6")

    monkeypatch.setattr(matrix, "_run_stage", fake_run_stage)
    report = matrix.run_matrix(
        providers=["deepseek", "moonshot"],
        secrets={"DEEPSEEK_API_KEY": "one", "MOONSHOT_API_KEY": "two"},
        smoke_max_tokens=32,
        gateway_max_tokens=32,
        gateway_timeout_seconds=1.0,
        stage_timeout_seconds=2.0,
        special_max_tokens=128,
        base_environment={"PATH": os.environ.get("PATH", "")},
    )

    assert report["deep_provider_selection"] == ["moonshot"]
    assert any(stage == "deep_multi_model" and provider == "moonshot" for stage, provider in calls)
    assert not any(
        stage == "deep_multi_model" and provider == "deepseek" for stage, provider in calls
    )


def test_synthetic_vision_fixture_is_generated_png_not_external_media() -> None:
    encoded = matrix._synthetic_color_block_png_base64()
    payload = __import__("base64").b64decode(encoded)

    assert payload.startswith(b"\x89PNG\r\n\x1a\n")
    assert b"IHDR" in payload
    assert b"IDAT" in payload
    assert len(payload) < 256


def test_special_child_uses_adapter_thinking_toggle_and_generated_vision_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.provider import selector
    from opensquilla.provider.types import (
        ContentBlockImage,
        DoneEvent,
        ReasoningDeltaEvent,
        TextDeltaEvent,
    )

    calls: list[tuple[Any, Any]] = []

    class FakeProvider:
        async def chat(self, messages: Any, *, config: Any):
            calls.append((messages, config))
            marker = (
                "OPENSQUILLA_VISION_SYNTHETIC_COLOR_BLOCK_OK"
                if isinstance(messages[0].content, list)
                else "OPENSQUILLA_THINKING_ON_OK"
            )
            if config.thinking:
                yield ReasoningDeltaEvent(text="synthetic reasoning evidence")
            yield TextDeltaEvent(text=marker)
            yield DoneEvent(
                input_tokens=8,
                output_tokens=3,
                reasoning_tokens=1 if config.thinking else 0,
                model="deepseek-v4-flash" if config.thinking else "gemini-3.5-flash",
            )

    monkeypatch.setattr(selector, "_build_provider", lambda config: FakeProvider())
    monkeypatch.setenv("DEEPSEEK_API_KEY", "child-secret")
    monkeypatch.setenv("GEMINI_API_KEY", "child-vision-secret")

    thinking, thinking_secrets = asyncio.run(
        matrix._special_child_report(
            "thinking_on",
            "deepseek",
            "deepseek-v4-flash",
            max_tokens=128,
        )
    )
    vision, vision_secrets = asyncio.run(
        matrix._special_child_report(
            "vision_synthetic_color_block",
            "gemini",
            "gemini-3.5-flash",
            max_tokens=256,
        )
    )

    assert thinking["ok"] is True
    assert thinking["results"][0]["toggle_verified"] is True
    assert thinking_secrets == {"DEEPSEEK_API_KEY": "child-secret"}
    assert calls[0][1].thinking is True
    assert calls[0][1].max_tokens == 128
    assert calls[0][1].thinking_budget_explicit is False
    assert vision["ok"] is True
    assert vision_secrets == {"GEMINI_API_KEY": "child-vision-secret"}
    assert calls[1][1].thinking is False
    assert calls[1][1].max_tokens == 256
    image_blocks = [
        block for block in calls[1][0][0].content if isinstance(block, ContentBlockImage)
    ]
    assert len(image_blocks) == 1
    assert image_blocks[0].source_type == "base64"
    assert image_blocks[0].media_type == "image/png"
    assert __import__("base64").b64decode(image_blocks[0].data).startswith(b"\x89PNG")


def test_gateway_child_forces_inline_tiers_and_global_thinking_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_batch(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {
            "ok": True,
            "cases": [
                {
                    "ok": True,
                    "actual_response_model": "MiniMax-M2.7",
                    "usage": {"input_tokens": 3, "output_tokens": 2},
                    "cost": {},
                }
            ],
        }

    monkeypatch.setattr(
        "scripts.live_provider_profile_gateway_e2e._run_gateway_case_batch",
        fake_batch,
    )
    monkeypatch.setenv("MINIMAX_API_KEY", "synthetic-child-secret")

    payload, _ = matrix._gateway_child_report(
        "minimax",
        "MiniMax-M2.7",
        max_tokens=64,
        timeout_seconds=1.0,
    )

    assert payload["ok"] is True
    assert captured["llm_thinking"] == "off"
    assert captured["tier_overrides"]["c1"]["thinking_level"] == "off"


def test_run_stage_deletes_raw_child_report(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output = tmp_path / "raw-child.json"

    def fake_subprocess_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        output.write_text(
            json.dumps(
                {
                    "ok": False,
                    "results": [{"error": "Authorization: Bearer secret"}],
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="")

    monkeypatch.setattr(matrix.subprocess, "run", fake_subprocess_run)
    execution = matrix._run_stage(
        stage="probe",
        provider="openai",
        command=[sys.executable, "synthetic-child.py"],
        env={},
        output=output,
        secrets={"OPENAI_API_KEY": "secret"},
        timeout_seconds=1.0,
    )

    assert not output.exists()
    assert "secret" not in json.dumps(execution)


def test_matrix_cli_writes_hygiene_only_report_in_temp_without_live_calls(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secrets_file = tmp_path / "provider.keys"
    output = tmp_path / "matrix.json"
    secrets_file.write_text(
        "unrecognized line\nOPENAI_API_KEY=\nOPENAI_BASE_URL=https://ignored.invalid/v1\n",
        encoding="utf-8",
    )
    secrets_file.chmod(0o644)
    monkeypatch.setattr(
        matrix,
        "_run_stage",
        lambda **kwargs: pytest.fail("empty credential must not start a child process"),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "live_multi_provider_matrix.py",
            "--secrets-file",
            str(secrets_file),
            "--output",
            str(output),
        ],
    )

    assert matrix.main() == 1
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert isinstance(payload, list) and payload
    assert all(set(row) == matrix._PUBLIC_RESULT_KEYS for row in payload)  # noqa: SLF001
    if os.name != "nt":
        assert output.stat().st_mode & 0o777 == 0o600
    assert "https://ignored.invalid" not in output.read_text(encoding="utf-8")
    assert set(matrix.DEFAULT_PROVIDERS) <= {row["provider"] for row in payload}
    captured = capsys.readouterr()
    assert json.loads(captured.out) == payload
    if os.name != "nt":
        assert "0644" in captured.err
    else:
        assert "expected 0600" not in captured.err
    assert "ignored_line_numbers" in captured.err
    assert "https://ignored.invalid" not in captured.err


def test_matrix_public_projector_flattens_cases_and_strips_stage_evidence() -> None:
    detailed = {
        "providers": [
            {
                "provider": "openai",
                "model": "gpt-5.4-mini",
                "status": "passed",
                "failure_class": None,
                "usage": {},
                "cost": {},
                "latency_ms": 7,
                "stages": [
                    {
                        "stage": "probe",
                        "provider": "openai",
                        "model": "gpt-5.4-mini",
                        "status": "passed",
                        "failure_class": None,
                        "usage": {"direct": {"output_tokens": 1}},
                        "cost": {},
                        "latency_ms": 3,
                        "done_event": True,
                    }
                ],
            }
        ],
        "deep_multi_model": [
            {
                "stage": "deep_multi_model",
                "provider": "openai",
                "model": "gpt-5.4-nano",
                "status": "failed",
                "failure_class": "model-unavailable",
                "usage": {},
                "cost": {},
                "latency_ms": 5,
                "error": "must-not-report",
            }
        ],
    }

    rows = matrix._public_report_rows(detailed)  # noqa: SLF001

    assert len(rows) == 2
    assert all(set(row) == matrix._PUBLIC_RESULT_KEYS for row in rows)  # noqa: SLF001
    serialized = json.dumps(rows)
    assert "stage" not in serialized
    assert "done_event" not in serialized
    assert "must-not-report" not in serialized
    with pytest.raises(RuntimeError, match="invalid field set"):
        matrix._assert_public_report_schema([{**rows[0], "stage": "probe"}])  # noqa: SLF001


def test_matrix_cli_accepts_only_a_unique_subset_of_fixed_provider_inventory(
    tmp_path: Path,
) -> None:
    args = matrix._parse_args(  # noqa: SLF001
        [
            "--secrets-file",
            str(tmp_path / "provider.keys"),
            "--providers",
            "dashscope",
            "deepseek",
            "minimax",
            "--output",
            str(tmp_path / "report.json"),
        ]
    )

    assert args.providers == ["dashscope", "deepseek", "minimax"]

    with pytest.raises(SystemExit):
        matrix._parse_args(  # noqa: SLF001
            [
                "--secrets-file",
                str(tmp_path / "provider.keys"),
                "--providers",
                "unknown-provider",
                "--output",
                str(tmp_path / "report.json"),
            ]
        )

    with pytest.raises(SystemExit):
        matrix._parse_args(  # noqa: SLF001
            [
                "--secrets-file",
                str(tmp_path / "provider.keys"),
                "--providers",
                "deepseek",
                "deepseek",
                "--output",
                str(tmp_path / "report.json"),
            ]
        )


def test_final_case_reports_exclude_raw_diagnostics_and_endpoint_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        matrix,
        "_run_stage",
        lambda **kwargs: _execution(kwargs["stage"], model="deepseek-v4-flash"),
    )
    report = matrix.run_matrix(
        providers=["deepseek"],
        secrets={"DEEPSEEK_API_KEY": "present"},
        smoke_max_tokens=64,
        gateway_max_tokens=64,
        gateway_timeout_seconds=1.0,
        stage_timeout_seconds=2.0,
        base_environment={"PATH": os.environ.get("PATH", "")},
    )

    serialized = json.dumps(report, sort_keys=True)
    assert "stdout" not in serialized
    assert "stderr" not in serialized
    assert "base_url" not in serialized
    assert "env_key" not in serialized
    assert "authorization" not in serialized.lower()


def test_stream_response_model_mismatch_is_an_implementation_failure() -> None:
    summary = matrix._stage_summary(  # noqa: SLF001
        "adapter_stream",
        "openai",
        "gpt-5.4-mini",
        _execution("adapter_stream", model="gpt-5.4-nano"),
    )
    assert summary["status"] == "failed"
    assert summary["failure_class"] == "implementation"


def test_deepseek_rolling_alias_response_is_not_a_model_identity_failure() -> None:
    summary = matrix._stage_summary(  # noqa: SLF001
        "adapter_stream",
        "deepseek",
        "deepseek-chat",
        _execution("adapter_stream", model="deepseek-v4-flash"),
    )

    assert summary["status"] == "passed"
    # Preserve the concrete upstream identity in the sanitized report.
    assert summary["model"] == "deepseek-v4-flash"
    assert summary["failure_class"] is None


def test_deepseek_concrete_model_mismatch_still_fails_closed() -> None:
    summary = matrix._stage_summary(  # noqa: SLF001
        "adapter_stream",
        "deepseek",
        "deepseek-v4-pro",
        _execution("adapter_stream", model="deepseek-v4-flash"),
    )

    assert summary["status"] == "failed"
    assert summary["failure_class"] == "implementation"
