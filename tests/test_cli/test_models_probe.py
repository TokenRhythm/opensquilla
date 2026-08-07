"""CLI tests for `opensquilla models probe` (offline, injected/fake results).

The probe command is live by nature, so these tests never let it reach a
network: probe results are injected by monkeypatching the shared onboarding
probe helpers, and the two real-path cases (missing key, unknown provider id)
short-circuit inside validation before any provider is contacted.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from typer.testing import CliRunner

from opensquilla.cli.main import app
from opensquilla.onboarding.probe import ProviderModelsDiscoverResult, ProviderProbeResult

runner = CliRunner()

# Synthetic sentinel; never a real credential. If redaction ever regresses,
# this exact token would leak into the rendered output and fail the tests.
SENTINEL_SECRET = "sk-test-sentinel-000000000000"  # noqa: S105 - synthetic dummy


def _write_config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def _primary_openai_config(tmp_path: Path) -> Path:
    return _write_config(
        tmp_path,
        """
        [llm]
        provider = "openai"
        model = "gpt-test-dummy"
        api_key = "sk-test-dummy-key"
        """,
    )


def _fake_probe(results: dict[str, ProviderProbeResult], calls: list[dict[str, Any]]):
    async def fake(**kwargs: Any) -> ProviderProbeResult:
        calls.append(kwargs)
        return results[kwargs["provider_id"]]

    return fake


def _fake_discover(
    results: dict[str, ProviderModelsDiscoverResult], calls: list[dict[str, Any]]
):
    async def fake(**kwargs: Any) -> ProviderModelsDiscoverResult:
        calls.append(kwargs)
        return results[kwargs["provider_id"]]

    return fake


def _patch_draft_install_id_context(monkeypatch):
    active_config = SimpleNamespace(
        privacy=SimpleNamespace(disable_network_observability=True)
    )
    load_calls: list[Path | None] = []
    prewarm_calls: list[Any] = []

    def fake_load_config(path: Path | None):
        load_calls.append(path)
        return active_config

    def fake_prewarm(*, config):
        prewarm_calls.append(config)
        return None

    monkeypatch.setattr(
        "opensquilla.cli.models_cmd.load_config",
        fake_load_config,
    )
    monkeypatch.setattr(
        "opensquilla.provider.tokenrhythm_correlation.prewarm_tokenrhythm_install_id",
        fake_prewarm,
    )
    return active_config, load_calls, prewarm_calls


def test_probe_ok_renders_table_and_exits_zero(tmp_path: Path, monkeypatch) -> None:
    config = _primary_openai_config(tmp_path)
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "opensquilla.cli.models_cmd.probe_llm_provider",
        _fake_probe(
            {"openai": ProviderProbeResult(ok=True, provider_id="openai", model="gpt-test-dummy")},
            calls,
        ),
    )

    result = runner.invoke(app, ["models", "probe", "--config", str(config)])

    assert result.exit_code == 0, result.output
    assert "openai" in result.output
    assert "gpt-test-dummy" in result.output
    assert "ok" in result.output
    assert len(calls) == 1
    assert calls[0]["provider_id"] == "openai"
    assert calls[0]["model"] == "gpt-test-dummy"


def test_probe_failure_classifies_and_exits_nonzero(tmp_path: Path, monkeypatch) -> None:
    config = _primary_openai_config(tmp_path)
    monkeypatch.setattr(
        "opensquilla.cli.models_cmd.probe_llm_provider",
        _fake_probe(
            {
                "openai": ProviderProbeResult(
                    ok=False,
                    provider_id="openai",
                    model="gpt-test-dummy",
                    failure_kind="transport_transient",
                    message="injected connection timeout",
                )
            },
            [],
        ),
    )

    result = runner.invoke(app, ["models", "probe", "--config", str(config)])

    assert result.exit_code == 1
    assert "transport_transient" in result.output
    assert "injected connection timeout" in result.output


def test_probe_redacts_sentinel_secret_from_error_detail(
    tmp_path: Path, monkeypatch
) -> None:
    config = _primary_openai_config(tmp_path)
    poisoned = ProviderProbeResult(
        ok=False,
        provider_id="openai",
        model="gpt-test-dummy",
        failure_kind="auth_invalid",
        message=f"Invalid api_key={SENTINEL_SECRET} rejected (Bearer {SENTINEL_SECRET})",
        code="401",
    )
    monkeypatch.setattr(
        "opensquilla.cli.models_cmd.probe_llm_provider",
        _fake_probe({"openai": poisoned}, []),
    )

    table_result = runner.invoke(app, ["models", "probe", "--config", str(config)])
    json_result = runner.invoke(
        app, ["models", "probe", "--config", str(config), "--json"]
    )

    assert table_result.exit_code == 1
    assert json_result.exit_code == 1
    assert "auth_invalid" in table_result.output
    assert SENTINEL_SECRET not in table_result.output
    assert SENTINEL_SECRET not in json_result.output


def test_probe_json_shape(tmp_path: Path, monkeypatch) -> None:
    config = _primary_openai_config(tmp_path)
    monkeypatch.setattr(
        "opensquilla.cli.models_cmd.probe_llm_provider",
        _fake_probe(
            {
                "openai": ProviderProbeResult(
                    ok=False,
                    provider_id="openai",
                    model="gpt-test-dummy",
                    failure_kind="rate_limited",
                    message="injected rate limit",
                    code="429",
                    latency_ms=123,
                )
            },
            [],
        ),
    )

    result = runner.invoke(app, ["models", "probe", "--config", str(config), "--json"])

    assert result.exit_code == 1
    rows = json.loads(result.stdout)
    assert isinstance(rows, list) and len(rows) == 1
    row = rows[0]
    assert row["provider"] == "openai"
    assert row["model"] == "gpt-test-dummy"
    assert row["ok"] is False
    assert row["kind"] == "rate_limited"
    assert row["detail"] == "injected rate limit"
    assert row["code"] == "429"
    assert row["method"] == "chat"
    assert row["source"] == "llm"
    assert row["latency_ms"] == 123


def test_probe_draft_reads_unsaved_credential_from_stdin(monkeypatch) -> None:
    calls: list[dict[str, Any]] = []
    active_config, load_calls, prewarm_calls = _patch_draft_install_id_context(
        monkeypatch
    )
    monkeypatch.setattr(
        "opensquilla.cli.models_cmd.probe_llm_provider",
        _fake_probe(
            {
                "tokenrhythm": ProviderProbeResult(
                    ok=True,
                    provider_id="tokenrhythm",
                    model="deepseek-v4-pro",
                    latency_ms=87,
                )
            },
            calls,
        ),
    )

    result = runner.invoke(
        app,
        ["models", "probe-draft"],
        input=json.dumps(
            {
                "providerId": "tokenrhythm",
                "model": "deepseek-v4-pro",
                "apiKey": SENTINEL_SECRET,
                "baseUrl": "https://tokenrhythm.studio/v1",
            }
        ),
    )

    assert result.exit_code == 0, result.output
    row = json.loads(result.stdout)
    assert row["ok"] is True
    assert row["source"] == "draft"
    assert row["latency_ms"] == 87
    assert calls == [
        {
            "provider_id": "tokenrhythm",
            "model": "deepseek-v4-pro",
            "api_key": SENTINEL_SECRET,
            "api_key_env": "",
            "base_url": "https://tokenrhythm.studio/v1",
            "proxy": "",
            "timeout": 30.0,
        }
    ]
    assert load_calls == [None]
    assert prewarm_calls == [active_config]


def test_probe_draft_redacts_failed_provider_detail(monkeypatch) -> None:
    _patch_draft_install_id_context(monkeypatch)
    monkeypatch.setattr(
        "opensquilla.cli.models_cmd.probe_llm_provider",
        _fake_probe(
            {
                "openai": ProviderProbeResult(
                    ok=False,
                    provider_id="openai",
                    model="gpt-test-dummy",
                    failure_kind="auth_invalid",
                    message=f"Rejected Bearer {SENTINEL_SECRET}",
                    code="401",
                )
            },
            [],
        ),
    )

    result = runner.invoke(
        app,
        ["models", "probe-draft"],
        input=json.dumps(
            {
                "providerId": "openai",
                "model": "gpt-test-dummy",
                "apiKey": SENTINEL_SECRET,
            }
        ),
    )

    assert result.exit_code == 1
    assert SENTINEL_SECRET not in result.stdout
    row = json.loads(result.stdout)
    assert row["ok"] is False
    assert row["kind"] == "auth_invalid"
    assert "***" in row["detail"]


def test_probe_draft_config_load_failure_registers_disabled_privacy(
    monkeypatch,
) -> None:
    from opensquilla.cli import models_cmd

    def fail_load_config(_path):
        raise ValueError("synthetic invalid config")

    captured: list[Any] = []
    monkeypatch.setattr(models_cmd, "load_config", fail_load_config)
    monkeypatch.setattr(
        "opensquilla.provider.tokenrhythm_correlation.prewarm_tokenrhythm_install_id",
        lambda *, config: captured.append(config),
    )

    models_cmd._prewarm_draft_probe_install_id()

    assert len(captured) == 1
    assert captured[0].privacy.disable_network_observability is True


def test_probe_unknown_provider_filter_exits_two(tmp_path: Path) -> None:
    config = _primary_openai_config(tmp_path)

    result = runner.invoke(
        app,
        ["models", "probe", "--config", str(config), "--provider", "not-configured"],
    )

    assert result.exit_code == 2
    combined = result.output + (result.stderr or "")
    assert "not configured" in combined.lower()


def test_probe_missing_key_classifies_auth_invalid_offline(tmp_path: Path) -> None:
    # Real probe path (no monkeypatch): the conftest strips provider env keys
    # and the config has none, so probe_llm_provider short-circuits with
    # AUTH_INVALID before any provider is even built — fully offline.
    config = _write_config(
        tmp_path,
        """
        [llm]
        provider = "openai"
        model = "gpt-test-dummy"
        """,
    )

    result = runner.invoke(app, ["models", "probe", "--config", str(config)])

    assert result.exit_code == 1
    assert "auth_invalid" in result.output
    assert "No API key available" in result.output


def test_probe_unknown_provider_id_reports_invalid_config(tmp_path: Path) -> None:
    # Real probe path: an unregistered provider id fails spec validation
    # before any network contact.
    config = _write_config(
        tmp_path,
        """
        [llm]
        provider = "not-a-real-provider"
        model = "dummy-model"
        """,
    )

    result = runner.invoke(app, ["models", "probe", "--config", str(config)])

    assert result.exit_code == 1
    assert "invalid_config" in result.output


def test_probe_profile_without_tier_model_uses_models_list(
    tmp_path: Path, monkeypatch
) -> None:
    config = _write_config(
        tmp_path,
        """
        [llm]
        provider = "openai"
        model = "gpt-test-dummy"
        api_key = "sk-test-dummy-key"

        [llm_profiles.anthropic]
        api_key = "sk-test-dummy-profile-key"
        """,
    )
    probe_calls: list[dict[str, Any]] = []
    discover_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "opensquilla.cli.models_cmd.probe_llm_provider",
        _fake_probe(
            {"openai": ProviderProbeResult(ok=True, provider_id="openai", model="gpt-test-dummy")},
            probe_calls,
        ),
    )
    monkeypatch.setattr(
        "opensquilla.cli.models_cmd.discover_provider_models",
        _fake_discover(
            {
                "anthropic": ProviderModelsDiscoverResult(
                    ok=True, provider_id="anthropic", source="live"
                )
            },
            discover_calls,
        ),
    )

    result = runner.invoke(app, ["models", "probe", "--config", str(config), "--json"])

    assert result.exit_code == 0, result.output
    rows = json.loads(result.stdout)
    by_provider = {row["provider"]: row for row in rows}
    assert set(by_provider) == {"openai", "anthropic"}
    assert by_provider["openai"]["method"] == "chat"
    assert by_provider["anthropic"]["method"] == "models_list"
    assert by_provider["anthropic"]["source"] == "llm_profiles"
    assert by_provider["anthropic"]["latency_ms"] == 0
    assert [call["provider_id"] for call in probe_calls] == ["openai"]
    assert [call["provider_id"] for call in discover_calls] == ["anthropic"]


def test_probe_provider_filter_and_model_override(tmp_path: Path, monkeypatch) -> None:
    config = _write_config(
        tmp_path,
        """
        [llm]
        provider = "openai"
        model = "gpt-test-dummy"
        api_key = "sk-test-dummy-key"

        [llm_profiles.anthropic]
        api_key = "sk-test-dummy-profile-key"
        """,
    )
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "opensquilla.cli.models_cmd.probe_llm_provider",
        _fake_probe(
            {
                "openai": ProviderProbeResult(
                    ok=True, provider_id="openai", model="override-model-dummy"
                )
            },
            calls,
        ),
    )

    result = runner.invoke(
        app,
        [
            "models",
            "probe",
            "--config",
            str(config),
            "--provider",
            "openai",
            "--model",
            "override-model-dummy",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    rows = json.loads(result.stdout)
    assert len(rows) == 1  # the filter drops the profile target
    assert rows[0]["model"] == "override-model-dummy"
    assert calls[0]["model"] == "override-model-dummy"
