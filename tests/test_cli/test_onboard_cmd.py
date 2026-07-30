"""CLI tests for `opensquilla onboard` and `configure`."""

from __future__ import annotations

import json as _json
import platform
import re
import shlex
import tomllib

import pytest
from typer.testing import CliRunner

from opensquilla.cli.main import app

runner = CliRunner()
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def _plain_text(value: str) -> str:
    return " ".join(_ANSI_RE.sub("", value).replace("│", " ").split())


def _compact_text(value: str) -> str:
    return "".join(_ANSI_RE.sub("", value).replace("│", " ").split())


def _env_hint(env_key: str) -> str:
    if platform.system().lower().startswith("win"):
        return f'PowerShell: $env:{env_key} = "<your-key>"'
    return f'export {env_key}="<your-key>"'


def _env_command(env_key: str) -> str:
    if platform.system().lower().startswith("win"):
        return f'$env:{env_key} = "<your-key>"'
    return f'export {env_key}="<your-key>"'


def _config_arg(path) -> str:
    return shlex.quote(str(path))


def _effective_config(path):
    """Load the effective config exactly as the gateway would.

    The sparse TOML persist omits values equal to model defaults, so raw-TOML
    asserts are reserved for keys a save actually writes; everything else is
    checked against the loaded (effective) configuration.
    """
    from opensquilla.onboarding.config_store import load_config

    return load_config(path)


def test_onboard_noninteractive_provider(tmp_path, monkeypatch):
    target = tmp_path / "c.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))
    result = runner.invoke(
        app,
        [
            "onboard",
            "--provider", "openrouter",
            "--model", "deepseek/deepseek-v4-flash",
            "--api-key", "sk",
            "--skip-channels", "--skip-search",
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert "openrouter" in target.read_text()
    assert "sk" not in result.stdout
    assert "onboarding.config_persisted" not in result.stdout
    assert "openrouter ·" not in result.stdout
    assert "OpenSquilla Setup Handoff" in result.stdout
    assert "Provider Configured" not in result.stdout
    # The explicitly selected direct/fallback model is independent from the
    # Router default tier and must survive onboarding unchanged.
    assert "LLM: openrouter / deepseek/deepseek-v4-flash" in result.stdout


def test_onboard_finish_commands_remain_copyable_with_long_config_path(
    tmp_path,
    monkeypatch,
):
    from opensquilla.cli import onboard_cmd

    target = tmp_path / "very-long-config-name-for-onboard-output.toml"
    monkeypatch.setattr(onboard_cmd.console, "width", 48)

    result = runner.invoke(
        app,
        [
            "onboard",
            "--provider",
            "openrouter",
            "--model",
            "deepseek/deepseek-v4-flash",
            "--api-key",
            "sk",
            "--router",
            "disabled",
            "--minimal",
            "--config",
            str(target),
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert f"opensquilla gateway run --config {_config_arg(target)}" in result.stdout
    assert f"opensquilla gateway start --json --config {_config_arg(target)}" in result.stdout
    assert (
        f"opensquilla gateway restart --json --config {_config_arg(target)}"
        in result.stdout
    )


def test_onboard_status_paths_remain_copyable_with_long_config_path(
    tmp_path,
    monkeypatch,
):
    from opensquilla.cli import onboard_cmd

    target = tmp_path / "very-long-config-name-for-onboard-status-output.toml"
    monkeypatch.setattr(onboard_cmd.console, "width", 48)

    result = runner.invoke(app, ["onboard", "status", "--config", str(target)])

    assert result.exit_code == 0, result.stdout
    assert (
        f"Guided CLI: opensquilla onboard --if-needed --config {_config_arg(target)}"
        in result.stdout
    )
    assert f"Web UI: opensquilla gateway run --config {_config_arg(target)}" in result.stdout
    assert (
        "Provider recipes: "
        f"opensquilla onboard catalog providers --config {_config_arg(target)}"
        in result.stdout
    )


def test_onboard_accepts_skip_image_generation_option(tmp_path, monkeypatch):
    target = tmp_path / "c.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))
    result = runner.invoke(
        app,
        [
            "onboard",
            "--provider",
            "openrouter",
            "--model",
            "deepseek/deepseek-v4-flash",
            "--api-key",
            "sk",
            "--skip-channels",
            "--skip-search",
            "--skip-image-generation",
        ],
    )

    assert result.exit_code == 0, result.stdout
    # A save marker: the flag-accepting run must actually persist the
    # provider config; a fresh-config default check alone is vacuous.
    # (Values equal to model defaults are omitted by the sparse persist, so
    # the stored key and router profile are the observable markers.)
    assert target.exists()
    data = tomllib.loads(target.read_text())
    assert data["llm"]["api_key"] == "sk"
    assert data["squilla_router"]["tier_profile"] == "openrouter"
    assert "image_generation" not in data or not data["image_generation"].get("enabled")
    assert _effective_config(target).image_generation.enabled is False


def test_onboard_passes_skip_migration_to_interactive_flow(tmp_path, monkeypatch):
    from opensquilla.cli import onboard_cmd
    from opensquilla.onboarding.config_store import PersistResult

    target = tmp_path / "c.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))
    captured: dict[str, bool] = {}

    def fake_run_interactive_onboard(options):
        captured["skip_migration"] = options.skip_migration
        return PersistResult(
            path=target,
            backup_path=None,
            restart_required=False,
            warnings=[],
        )

    monkeypatch.setattr(
        onboard_cmd,
        "run_interactive_onboard",
        fake_run_interactive_onboard,
    )

    result = runner.invoke(app, ["onboard", "--skip-migration"])

    assert result.exit_code == 0, result.stdout
    assert captured["skip_migration"] is True


def test_onboard_noninteractive_provider_can_use_env_key_and_router(tmp_path, monkeypatch):
    target = tmp_path / "c.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    result = runner.invoke(
        app,
        [
            "onboard",
            "--provider",
            "deepseek",
            "--model",
            "deepseek-chat",
            "--api-key-env",
            "DEEPSEEK_API_KEY",
            "--proxy",
            "http://127.0.0.1:7890",
            "--router",
            "recommended",
            "--minimal",
        ],
    )

    assert result.exit_code == 0, result.stdout
    data = tomllib.loads(target.read_text())
    assert data["llm"]["provider"] == "deepseek"
    assert data["llm"]["api_key_env"] == "DEEPSEEK_API_KEY"
    assert data["llm"]["proxy"] == "http://127.0.0.1:7890"
    assert "api_key" not in data["llm"]
    assert data["squilla_router"]["tier_profile"] == "deepseek"
    assert "tiers" not in data["squilla_router"]
    assert "DEEPSEEK_API_KEY" in result.stdout
    assert "warning" in result.stdout.lower()
    assert "not set in this shell" in result.stdout


def test_onboard_noninteractive_provider_can_omit_model_for_router_profile(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "c.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))
    result = runner.invoke(
        app,
        [
            "onboard",
            "--provider",
            "deepseek",
            "--api-key-env",
            "DEEPSEEK_API_KEY",
            "--minimal",
        ],
    )

    assert result.exit_code == 0, result.stdout
    data = tomllib.loads(target.read_text())
    assert data["llm"]["provider"] == "deepseek"
    assert data["llm"]["model"] == "deepseek-v4-flash"
    assert data["squilla_router"]["tier_profile"] == "deepseek"


def test_onboard_noninteractive_provider_without_router_profile_seeds_router_tiers(
    tmp_path, monkeypatch
):
    target = tmp_path / "c.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))

    result = runner.invoke(
        app,
        [
            "onboard",
            "--provider",
            "anthropic",
            "--model",
            "claude-3-5-sonnet-latest",
            "--api-key-env",
            "ANTHROPIC_API_KEY",
            "--minimal",
        ],
    )

    assert result.exit_code == 0, result.stdout
    data = tomllib.loads(target.read_text())
    assert data["llm"]["provider"] == "anthropic"
    assert data["llm"]["api_key_env"] == "ANTHROPIC_API_KEY"
    assert "api_key" not in data["llm"]
    assert _effective_config(target).squilla_router.enabled is True
    assert "tier_profile" not in data["squilla_router"]
    for tier in ("c0", "c1", "c2", "c3"):
        assert data["squilla_router"]["tiers"][tier]["provider"] == "anthropic"
        assert data["squilla_router"]["tiers"][tier]["model"] == "claude-3-5-sonnet-latest"


def test_onboard_noninteractive_provider_error_is_productized(tmp_path, monkeypatch):
    target = tmp_path / "c.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))

    result = runner.invoke(
        app,
        [
            "onboard",
            "--provider",
            "ollama",
            "--minimal",
        ],
    )

    assert result.exit_code == 2
    assert "model is required" in result.stderr
    assert "--model <model-id>" in result.stderr
    assert "Traceback" not in result.stdout + result.stderr
    assert not target.exists()


def test_onboard_if_needed_skips_when_configured(tmp_path, monkeypatch):
    target = tmp_path / "c.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))
    runner.invoke(
        app,
        [
            "onboard",
            "--provider", "openrouter",
            "--model", "x", "--api-key", "k",
            "--skip-channels", "--skip-search",
        ],
    )
    mtime_before = target.stat().st_mtime
    result = runner.invoke(app, ["onboard", "--if-needed"])
    assert result.exit_code == 0
    assert "core setup is ready" in result.stdout.lower()
    assert "Optional next moves:" in result.stdout
    assert "Channel recipes:" in result.stdout
    assert "Image recipes:" in result.stdout
    assert target.stat().st_mtime == mtime_before


def test_onboard_if_needed_uses_explicit_config_path(tmp_path, monkeypatch):
    default_target = tmp_path / "default.toml"
    target = tmp_path / "custom.toml"
    target.write_text(
        '[llm]\n'
        'provider = "openrouter"\n'
        'model = "deepseek/deepseek-v4-flash"\n'
        'api_key_env = "CUSTOM_LLM_KEY"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(default_target))
    monkeypatch.setenv("CUSTOM_LLM_KEY", "sk-from-custom-env")

    result = runner.invoke(app, ["onboard", "--if-needed", "--config", str(target)])

    assert result.exit_code == 0
    assert "core setup is ready" in result.stdout.lower()
    assert "Optional next moves:" in result.stdout
    assert f"--config{_config_arg(target)}" in "".join(result.stdout.split())
    assert not default_target.exists()


def test_onboard_if_needed_skips_when_key_comes_from_env(tmp_path, monkeypatch):
    target = tmp_path / "c.toml"
    target.write_text(
        '[llm]\n'
        'provider = "openrouter"\n'
        'model = "deepseek/deepseek-v4-flash"\n'
        'api_key_env = "OPENROUTER_API_KEY"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-from-env")
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))

    result = runner.invoke(app, ["onboard", "--if-needed"])

    assert result.exit_code == 0
    assert "core setup is ready" in result.stdout.lower()
    assert "Optional next moves:" in result.stdout


def test_onboard_if_needed_does_not_treat_env_as_config_without_config_file(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "c.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-from-env")

    result = runner.invoke(app, ["onboard", "--if-needed"])

    assert result.exit_code == 2
    assert "requires a TTY" in result.stdout
    assert "already complete" not in result.stdout.lower()
    assert not target.exists()


def test_onboard_if_needed_accepts_default_env_key(tmp_path, monkeypatch):
    # A config file that names the provider but omits api_key_env is ready when
    # the provider's default env var (OPENROUTER_API_KEY) resolves: onboarding
    # status mirrors the runtime, which reads that variable directly.
    target = tmp_path / "c.toml"
    target.write_text(
        '[llm]\n'
        'provider = "openrouter"\n'
        'model = "deepseek/deepseek-v4-flash"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-from-env")
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))

    result = runner.invoke(app, ["onboard", "--if-needed"])

    assert result.exit_code == 0
    assert "core setup is ready" in result.stdout.lower()
    assert "requires a TTY" not in result.stdout


def test_onboard_if_needed_does_not_accept_settings_env_without_config_reference(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "c.toml"
    target.write_text(
        '[llm]\n'
        'provider = "openrouter"\n'
        'model = "deepseek/deepseek-v4-flash"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENSQUILLA_LLM_API_KEY", "sk-from-settings-env")
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))

    result = runner.invoke(app, ["onboard", "--if-needed"])

    assert result.exit_code == 2
    assert "requires a TTY" in result.stdout
    assert "already complete" not in result.stdout.lower()


def test_onboard_if_needed_does_not_accept_settings_env_with_empty_config(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "c.toml"
    target.write_text("", encoding="utf-8")
    monkeypatch.setenv("OPENSQUILLA_LLM_API_KEY", "sk-from-settings-env")
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))

    result = runner.invoke(app, ["onboard", "--if-needed"])

    assert result.exit_code == 2
    assert "requires a TTY" in result.stdout
    assert "already complete" not in result.stdout.lower()


def test_onboard_if_needed_requires_referenced_env_even_with_settings_env(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "c.toml"
    target.write_text(
        '[llm]\n'
        'provider = "openrouter"\n'
        'model = "deepseek/deepseek-v4-flash"\n'
        'api_key_env = "OPENROUTER_API_KEY"\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("OPENSQUILLA_LLM_API_KEY", "sk-from-settings-env")
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))

    result = runner.invoke(app, ["onboard", "--if-needed"])

    assert result.exit_code == 2
    assert "requires a TTY" in result.stdout
    assert "already complete" not in result.stdout.lower()


def test_onboard_status_uses_explicit_config_path(tmp_path, monkeypatch):
    default_target = tmp_path / "default.toml"
    target = tmp_path / "custom.toml"
    target.write_text(
        '[llm]\n'
        'provider = "openrouter"\n'
        'model = "deepseek/deepseek-v4-flash"\n'
        'api_key_env = "CUSTOM_LLM_KEY"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(default_target))
    monkeypatch.delenv("CUSTOM_LLM_KEY", raising=False)

    result = runner.invoke(app, ["onboard", "status", "--json", "--config", str(target)])

    assert result.exit_code == 0, result.stdout
    payload = _json.loads(result.stdout)
    assert payload["configPath"] == str(target)
    assert payload["sections"]["llm"] == "degraded"
    assert not default_target.exists()


@pytest.mark.parametrize("system_name", ["Linux", "Windows"])
def test_onboard_status_json_exposes_provider_section_alias(
    tmp_path,
    monkeypatch,
    system_name,
):
    monkeypatch.setattr(platform, "system", lambda: system_name)
    target = tmp_path / "custom.toml"
    target.write_text(
        '[llm]\n'
        'provider = "openrouter"\n'
        'model = "deepseek/deepseek-v4-flash"\n'
        'api_key_env = "CUSTOM_LLM_KEY"\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("CUSTOM_LLM_KEY", raising=False)

    result = runner.invoke(app, ["onboard", "status", "--json", "--config", str(target)])

    assert result.exit_code == 0, result.stdout
    payload = _json.loads(result.stdout)
    assert payload["sections"]["provider"] == payload["sections"]["llm"]
    assert payload["sectionDetails"]["provider"] == payload["sectionDetails"]["llm"]
    assert payload["sectionDetails"]["provider"]["label"] == "Provider"
    assert payload["sectionAliases"] == {"llm": "provider"}
    assert payload["envRecoveryCommands"] == [
        {
            "section": "llm",
            "label": "Set provider key",
            "command": _env_command("CUSTOM_LLM_KEY"),
        }
    ]


def test_onboard_status_reports_invalid_config_without_traceback(tmp_path):
    target = tmp_path / "bad.toml"
    target.write_text("[search]\n", encoding="utf-8")

    result = runner.invoke(app, ["onboard", "status", "--config", str(target)])

    assert result.exit_code == 2
    assert "OpenSquilla config error" in result.stderr
    assert _compact_text(str(target)) in _compact_text(result.stderr)
    assert "search" in result.stderr
    assert "Fix:" in result.stderr
    assert (
        f"opensquillaonboard--if-needed--config{_config_arg(target)}"
        in "".join(result.stderr.split())
    )
    assert "Traceback" not in result.stderr
    assert "pydantic_core" not in result.stderr


def test_onboard_status_table_keeps_explicit_config_path_in_next_step(
    tmp_path,
    monkeypatch,
):
    default_target = tmp_path / "default.toml"
    target = tmp_path / "custom.toml"
    target.write_text(
        '[llm]\n'
        'provider = "openrouter"\n'
        'model = "deepseek/deepseek-v4-flash"\n'
        'api_key_env = "CUSTOM_LLM_KEY"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(default_target))
    monkeypatch.delenv("CUSTOM_LLM_KEY", raising=False)

    result = runner.invoke(app, ["onboard", "status", "--config", str(target)])

    assert result.exit_code == 0, result.stdout
    assert (
        f"opensquillaonboard--if-needed--config{_config_arg(target)}"
        in "".join(result.stdout.split())
    )
    assert not default_target.exists()


def test_onboard_status_table_offers_cli_web_and_recipe_setup_paths(
    tmp_path,
    monkeypatch,
):
    default_target = tmp_path / "default.toml"
    target = tmp_path / "custom.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(default_target))

    result = runner.invoke(app, ["onboard", "status", "--config", str(target)])

    assert result.exit_code == 0, result.stdout
    collapsed = "".join(result.stdout.split())
    assert "Setup paths:" in result.stdout
    assert "Guided CLI:" in result.stdout
    assert (
        f"opensquillaonboard--if-needed--config{_config_arg(target)}"
        in collapsed
    )
    assert "Web UI:" in result.stdout
    assert (
        f"opensquillagatewayrun--config{_config_arg(target)}"
        in collapsed
    )
    assert "http://127.0.0.1:18791/control/setup" in result.stdout
    assert "Explore options:" in result.stdout
    assert (
        f"opensquillaonboardcatalog--config{_config_arg(target)}"
        in collapsed
    )
    assert "opensquilla onboard catalog --json" not in result.stdout
    assert "Provider recipes:" in result.stdout
    assert (
        "opensquillaonboardcatalogproviders"
        f"--config{_config_arg(target)}"
        in collapsed
    )
    assert "--provider<id>" not in collapsed
    assert "--model<model>" not in collapsed
    assert "--api-key-env<ENV_NAME>" not in collapsed
    assert not default_target.exists()


def test_onboard_status_points_missing_provider_to_provider_recipes(
    tmp_path,
    monkeypatch,
):
    default_target = tmp_path / "default.toml"
    target = tmp_path / "custom.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(default_target))

    result = runner.invoke(app, ["onboard", "status", "--config", str(target)])

    assert result.exit_code == 0, result.stdout
    collapsed = "".join(result.stdout.split())
    assert "Provider recipes:" in result.stdout
    assert (
        f"opensquillaonboardcatalogproviders--config{_config_arg(target)}"
        in collapsed
    )
    assert "Headless provider:" not in result.stdout
    assert "--provider <id>" not in result.stdout
    assert "--model <model>" not in result.stdout
    assert "--api-key-env <ENV_NAME>" not in result.stdout
    assert not default_target.exists()


def test_onboard_status_leads_with_recommended_next_move(tmp_path, monkeypatch):
    default_target = tmp_path / "default.toml"
    target = tmp_path / "custom.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(default_target))

    result = runner.invoke(app, ["onboard", "status", "--config", str(target)])

    assert result.exit_code == 0, result.stdout
    collapsed = "".join(result.stdout.split())
    assert "Recommended next move:" in result.stdout
    assert "Guided CLI:" in result.stdout
    assert (
        f"GuidedCLI:opensquillaonboard--if-needed--config{_config_arg(target)}"
        in collapsed
    )
    assert result.stdout.count("Guided CLI:") == 1
    assert result.stdout.index("Recommended next move:") < result.stdout.index(
        "Setup paths:"
    )
    assert not default_target.exists()


def test_onboard_status_web_path_uses_gateway_and_control_ui_config(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "custom.toml"
    target.write_text(
        'host = "0.0.0.0"\n'
        "port = 19999\n"
        "\n"
        "[control_ui]\n"
        'base_path = "/ops"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(tmp_path / "default.toml"))

    result = runner.invoke(app, ["onboard", "status", "--config", str(target)])

    assert result.exit_code == 0, result.stdout
    assert "Web UI:" in result.stdout
    assert "http://127.0.0.1:19999/ops/setup" in result.stdout
    assert "http://0.0.0.0:19999" not in result.stdout


def test_onboard_status_does_not_offer_disabled_web_ui_path(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "custom.toml"
    target.write_text(
        "[control_ui]\n"
        "enabled = false\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(tmp_path / "default.toml"))

    result = runner.invoke(app, ["onboard", "status", "--config", str(target)])

    assert result.exit_code == 0, result.stdout
    assert "Guided CLI:" in result.stdout
    assert "Provider recipes:" in result.stdout
    assert "Web UI:" not in result.stdout
    assert "/control/setup" not in result.stdout


def test_onboard_status_table_uses_product_labels_and_scope_column(
    tmp_path,
    monkeypatch,
):
    default_target = tmp_path / "default.toml"
    target = tmp_path / "custom.toml"
    target.write_text(
        '[llm]\n'
        'provider = "openrouter"\n'
        'model = "deepseek/deepseek-v4-flash"\n'
        'api_key_env = "CUSTOM_LLM_KEY"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(default_target))
    monkeypatch.delenv("CUSTOM_LLM_KEY", raising=False)

    result = runner.invoke(app, ["onboard", "status", "--config", str(target)])

    assert result.exit_code == 0, result.stdout
    assert "OpenSquilla setup readiness" in result.stdout
    assert "OpenSquilla ready:" in result.stdout
    assert "Needs onboarding:" not in result.stdout
    assert "Provider" in result.stdout
    assert "Web search" in result.stdout
    assert "Image generation" in result.stdout
    assert "Memory embedding" in result.stdout
    assert "Required" in result.stdout
    assert "Optional" in result.stdout
    assert "Later" in result.stdout
    assert "env key not visible" in result.stdout
    assert "llm" not in result.stdout
    assert "missing_env" not in result.stdout
    assert "image_generation" not in result.stdout
    assert not default_target.exists()


@pytest.mark.parametrize("system_name", ["Linux", "Windows"])
def test_onboard_status_prioritizes_missing_env_recovery(
    tmp_path,
    monkeypatch,
    system_name,
):
    monkeypatch.setattr(platform, "system", lambda: system_name)
    target = tmp_path / "custom.toml"
    target.write_text(
        '[llm]\n'
        'provider = "openrouter"\n'
        'model = "deepseek/deepseek-v4-flash"\n'
        'api_key_env = "CUSTOM_LLM_KEY"\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("CUSTOM_LLM_KEY", raising=False)

    result = runner.invoke(app, ["onboard", "status", "--config", str(target)])

    assert result.exit_code == 0, result.stdout
    assert f"Set provider key: {_env_hint('CUSTOM_LLM_KEY')}" in _plain_text(
        result.stdout
    )
    assert result.stdout.index("Set provider key:") < result.stdout.index("Guided CLI:")


def test_onboard_status_prints_action_guide_without_squeezing_detail(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "custom.toml"
    target.write_text(
        '[llm]\n'
        'provider = "openrouter"\n'
        'model = "deepseek/deepseek-v4-flash"\n'
        'api_key_env = "CUSTOM_LLM_KEY"\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("CUSTOM_LLM_KEY", raising=False)

    result = runner.invoke(app, ["onboard", "status", "--config", str(target)])

    assert result.exit_code == 0, result.stdout
    assert "Action guide:" in result.stdout
    assert "Fix: blocked or action-required" in result.stdout
    assert "Review: ready" in result.stdout
    assert "Configure: optional later" in result.stdout
    assert "┃ Action" not in result.stdout.split("Action guide:", 1)[0]
    assert "env key not visible" in result.stdout


def test_onboard_help_exposes_configure_command():
    result = runner.invoke(app, ["onboard", "--help"])

    assert result.exit_code == 0, result.stdout
    assert "status" in result.stdout
    assert "catalog" in result.stdout
    compact = _compact_text(result.stdout)
    assert "Listonboardingsetupoptionsforeveryconfigurablesection." in compact
    assert "configure" in result.stdout
    assert (
        "Reconfigureprovider,router,ensemble,channels,search,imagegeneration,"
        "ormemory."
        in compact
    )


def test_onboard_catalog_json_exposes_all_setup_options(tmp_path, monkeypatch):
    target = tmp_path / "custom.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))

    result = runner.invoke(app, ["onboard", "catalog", "--json", "--config", str(target)])

    assert result.exit_code == 0, result.stdout
    payload = _json.loads(result.stdout)
    assert {
        "providers",
        "routerProfiles",
        "searchProviders",
        "channels",
        "imageGenerationProviders",
        "memoryEmbeddingProviders",
    } <= payload.keys()
    assert any(row["providerId"] == "openrouter" for row in payload["providers"])
    assert any(row["providerId"] == "duckduckgo" for row in payload["searchProviders"])
    assert any(row["providerId"] == "openrouter" for row in payload["imageGenerationProviders"])
    assert any(row["providerId"] == "auto" for row in payload["memoryEmbeddingProviders"])
    assert any(row["type"] == "discord" for row in payload["channels"])
    assert not target.exists()


def test_onboard_catalog_help_names_short_capability_section_aliases():
    result = runner.invoke(app, ["onboard", "catalog", "--help"])

    assert result.exit_code == 0, result.stdout
    compact = _compact_text(result.stdout)
    assert "image(aliasforimage-generation)" in compact
    assert "memory(aliasformemory-embedding)" in compact


def test_onboard_catalog_can_focus_one_setup_section():
    result = runner.invoke(app, ["onboard", "catalog", "search", "--json"])

    assert result.exit_code == 0, result.stdout
    payload = _json.loads(result.stdout)
    assert list(payload) == ["searchProviders"]
    assert any(row["providerId"] == "duckduckgo" for row in payload["searchProviders"])
    assert all("requiresApiKey" in row for row in payload["searchProviders"])


@pytest.mark.parametrize(
    ("alias", "section_key", "expected"),
    [
        ("image", "imageGenerationProviders", "openrouter/google/gemini"),
        ("memory", "memoryEmbeddingProviders", "openai-compatible"),
    ],
)
def test_onboard_catalog_accepts_short_capability_section_aliases(
    alias,
    section_key,
    expected,
):
    result = runner.invoke(app, ["onboard", "catalog", alias, "--json"])

    assert result.exit_code == 0, result.stdout
    payload = _json.loads(result.stdout)
    assert list(payload) == [section_key]
    assert expected in result.stdout


@pytest.mark.parametrize(
    ("section", "expected"),
    [
        ("providers", ["openrouter", "OPENROUTER_API_KEY", "deepseek/deepseek-v4-pro"]),
        ("router", ["recommended", "openrouter-mix", "c0", "c3"]),
        ("search", ["duckduckgo", "brave", "BRAVE_SEARCH_API_KEY"]),
        ("channels", ["discord", "Bot token", "opensquilla channels describe discord --json"]),
        ("image-generation", ["openrouter", "openrouter/google/gemini", "OPENROUTER_API_KEY"]),
        ("memory-embedding", ["auto", "local", "openai-compatible", "FTS-only"]),
    ],
)
def test_onboard_catalog_focused_human_output_lists_usable_options(section, expected):
    result = runner.invoke(app, ["onboard", "catalog", section])

    assert result.exit_code == 0, result.stdout
    for text in expected:
        assert text in result.stdout


def test_onboard_catalog_provider_rows_name_squillarouter_tier_support():
    result = runner.invoke(app, ["onboard", "catalog", "providers"])

    assert result.exit_code == 0, result.stdout
    assert "route SquillaRouter ready" in result.stdout
    assert "route Direct only" in result.stdout
    openrouter_row = next(
        line for line in result.stdout.splitlines() if line.startswith("- openrouter:")
    )
    anthropic_row = next(
        line for line in result.stdout.splitlines() if line.startswith("- anthropic:")
    )
    assert openrouter_row.index("route SquillaRouter ready") < openrouter_row.index(
        "key OPENROUTER_API_KEY"
    )
    assert anthropic_row.index("route Direct only") < anthropic_row.index(
        "key ANTHROPIC_API_KEY"
    )
    assert "SquillaRouter tiers yes" not in result.stdout
    assert "SquillaRouter tiers no" not in result.stdout
    assert "router yes" not in result.stdout
    assert "router no" not in result.stdout


def test_onboard_catalog_focused_commands_are_actionable_with_config_path(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "catalog-target.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))

    result = runner.invoke(app, ["onboard", "catalog", "channels", "--config", str(target)])

    assert result.exit_code == 0, result.stdout
    assert (
        "Try: opensquilla onboard configure channels --channel-type discord "
        "--name <name> --token <token>"
    ) in result.stdout
    assert f"--config {_config_arg(target)}" in result.stdout
    assert "opensquilla onboard configure channels --channel-type <type>\n" not in result.stdout
    assert "Configure with:" not in result.stdout
    assert not target.exists()


@pytest.mark.parametrize(
    "section, forbidden",
    [
        ("providers", "opensquilla onboard configure provider --provider <id>"),
        ("search", "--search-provider <provider>"),
        ("channels", "opensquilla onboard configure channels --channel-type <type>"),
        ("image-generation", "--image-provider <provider>"),
        ("memory-embedding", "--memory-provider <provider>"),
    ],
)
def test_onboard_catalog_focused_output_uses_recipes_not_generic_templates(
    section,
    forbidden,
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "focused-catalog.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))

    result = runner.invoke(app, ["onboard", "catalog", section, "--config", str(target)])

    assert result.exit_code == 0, result.stdout
    assert "Copy a Try line" in result.stdout
    assert "Configure with:" not in result.stdout
    assert forbidden not in result.stdout
    assert f"--config {_config_arg(target)}" in result.stdout
    assert not target.exists()


def test_onboard_catalog_router_focus_offers_a_real_recipe(tmp_path, monkeypatch):
    target = tmp_path / "router-catalog.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))

    result = runner.invoke(app, ["onboard", "catalog", "router", "--config", str(target)])

    assert result.exit_code == 0, result.stdout
    assert "Try: opensquilla onboard configure router --router recommended --default-tier c1" in (
        result.stdout
    )
    assert "Configure with:" not in result.stdout
    assert f"--config {_config_arg(target)}" in result.stdout
    assert not target.exists()


def test_onboard_catalog_focused_provider_examples_match_key_requirements(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "provider-catalog.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))

    result = runner.invoke(
        app,
        ["onboard", "catalog", "providers", "--config", str(target)],
    )

    assert result.exit_code == 0, result.stdout
    assert (
        "Try: opensquilla onboard configure provider --provider openrouter "
        "--model deepseek/deepseek-v4-pro --api-key-env OPENROUTER_API_KEY "
        f"--config {_config_arg(target)}"
    ) in result.stdout
    assert (
        "Try: opensquilla onboard configure provider --provider anthropic "
        "--model <model> --api-key-env ANTHROPIC_API_KEY "
        f"--config {_config_arg(target)}"
    ) in result.stdout
    assert (
        "Try: opensquilla onboard configure provider --provider ollama "
        "--model <model> "
        f"--config {_config_arg(target)}"
    ) in result.stdout
    ollama_line = next(
        line
        for line in result.stdout.splitlines()
        if line.startswith("  Try: opensquilla onboard configure provider --provider ollama")
    )
    assert "--api-key-env" not in ollama_line
    assert not target.exists()


def test_onboard_catalog_focused_capability_examples_match_key_requirements(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "capability-catalog.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))

    search = runner.invoke(
        app,
        ["onboard", "catalog", "search", "--config", str(target)],
    )
    memory = runner.invoke(
        app,
        ["onboard", "catalog", "memory-embedding", "--config", str(target)],
    )

    assert search.exit_code == 0, search.stdout
    assert (
        "Try: opensquilla onboard configure search --search-provider brave "
        "--api-key-env BRAVE_SEARCH_API_KEY "
        f"--config {_config_arg(target)}"
    ) in search.stdout
    assert (
        "Try: opensquilla onboard configure search --search-provider duckduckgo "
        f"--config {_config_arg(target)}"
    ) in search.stdout
    duckduckgo_line = next(
        line
        for line in search.stdout.splitlines()
        if line.startswith(
            "  Try: opensquilla onboard configure search --search-provider duckduckgo"
        )
    )
    assert "--api-key-env" not in duckduckgo_line

    assert memory.exit_code == 0, memory.stdout
    assert (
        "Try: opensquilla onboard configure memory --memory-provider auto "
        f"--config {_config_arg(target)}"
    ) in memory.stdout
    assert (
        "Try: opensquilla onboard configure memory --memory-provider openai "
        "--api-key-env OPENAI_API_KEY "
        f"--config {_config_arg(target)}"
    ) in memory.stdout
    auto_line = next(
        line
        for line in memory.stdout.splitlines()
        if line.startswith(
            "  Try: opensquilla onboard configure memory --memory-provider auto"
        )
    )
    assert "--api-key-env" not in auto_line
    assert "--model" not in auto_line
    assert not target.exists()


def test_onboard_catalog_focused_channel_examples_include_required_fields(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "channel-catalog.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))

    result = runner.invoke(
        app,
        ["onboard", "catalog", "channels", "--config", str(target)],
    )

    assert result.exit_code == 0, result.stdout
    assert (
        "Try: opensquilla onboard configure channels --channel-type discord "
        "--name <name> --token <token> "
        f"--config {_config_arg(target)}"
    ) in result.stdout
    assert (
        "Try: opensquilla onboard configure channels --channel-type feishu "
        "--name <name> --field app_id=<value> --field app_secret=<value> "
        f"--config {_config_arg(target)}"
    ) in result.stdout
    assert "opensquilla channels describe feishu --json" in result.stdout
    assert not target.exists()


def test_onboard_catalog_focused_image_and_metadata_search_examples_are_specific(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "image-catalog.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))

    image = runner.invoke(
        app,
        ["onboard", "catalog", "image-generation", "--config", str(target)],
    )
    search = runner.invoke(
        app,
        ["onboard", "catalog", "search", "--config", str(target)],
    )

    assert image.exit_code == 0, image.stdout
    assert (
        "Try: opensquilla onboard configure image "
        "--image-provider openrouter "
        "--primary openrouter/google/gemini-3.1-flash-image-preview "
        "--api-key-env OPENROUTER_API_KEY "
        f"--config {_config_arg(target)}"
    ) in image.stdout

    assert search.exit_code == 0, search.stdout
    assert "- exa: Exa | ready | key EXA_API_KEY | cloud" in search.stdout
    assert (
        "Try: opensquilla onboard configure search --search-provider exa "
        "--api-key-env EXA_API_KEY "
        f"--config {_config_arg(target)}"
    ) in search.stdout
    assert "- perplexity: Perplexity | metadata only" in search.stdout
    assert "Try: not configurable in this build" in search.stdout
    assert not target.exists()


def test_onboard_catalog_overview_commands_keep_active_config_path(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "catalog-overview.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))

    result = runner.invoke(app, ["onboard", "catalog", "--config", str(target)])

    assert result.exit_code == 0, result.stdout
    compact = result.stdout.replace(" ", "").replace("\n", "")
    assert (
        f"opensquillaonboardcatalogproviders--config{_config_arg(target)}".replace(
            " ", ""
        )
        in compact
    )
    assert (
        f"opensquillaonboardcatalogchannels--config{_config_arg(target)}".replace(
            " ", ""
        )
        in compact
    )
    assert (
        f"opensquillaonboardcatalogrouter--config{_config_arg(target)}".replace(" ", "")
        in compact
    )
    assert (
        f"opensquillaonboardcatalogsearch--config{_config_arg(target)}".replace(" ", "")
        in compact
    )
    assert (
        f"opensquillaonboardcatalogimage--config{_config_arg(target)}".replace(
            " ", ""
        )
        in compact
    )
    assert (
        f"opensquillaonboardcatalogmemory--config{_config_arg(target)}".replace(
            " ", ""
        )
        in compact
    )
    assert "Open section" in result.stdout
    assert "option-specific Try commands" in result.stdout
    assert not target.exists()


def test_onboard_catalog_overview_hides_generic_configure_templates(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "catalog-overview.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))

    result = runner.invoke(app, ["onboard", "catalog", "--config", str(target)])

    assert result.exit_code == 0, result.stdout
    assert "Configure templates:" not in result.stdout
    assert "opensquilla onboard configure provider --provider <id>" not in result.stdout
    assert "--api-key-env <ENV_NAME>" not in result.stdout
    assert "--search-provider <provider>" not in result.stdout
    assert "--memory-provider <provider>" not in result.stdout
    assert not target.exists()


def test_onboard_configure_help_groups_options_by_setup_area():
    result = runner.invoke(app, ["onboard", "configure", "--help"])

    assert result.exit_code == 0, result.stdout
    expected_panels = [
        "Target section",
        "Text provider",
        "Shared keys and endpoints",
        "Router",
        "Search",
        "Channels",
        "Image generation",
        "Memory embedding",
        "Global",
    ]
    for panel in expected_panels:
        assert panel in result.stdout

    expected_option_locations = [
        ("Text provider", "--provider"),
        ("Shared keys and endpoints", "--model"),
        ("Shared keys and endpoints", "--api-key-env"),
        ("Shared keys and endpoints", "--base-url"),
        ("Shared keys and endpoints", "--proxy"),
        ("Router", "--router"),
        ("Search", "--search-provider"),
        ("Channels", "--channel-type"),
        ("Image generation", "--image-provider"),
        ("Memory embedding", "--memory-provider"),
        ("Global", "--config"),
    ]
    for panel, option in expected_option_locations:
        assert result.stdout.index(panel) < result.stdout.index(option)

    compact = _compact_text(result.stdout)
    assert "APIkeyforprovider,search,imagegeneration,ormemoryembedding." in compact
    assert "image(aliasforimage-generation)" in compact
    assert "memory(aliasformemory-embedding)" in compact


def test_onboard_status_summarizes_blocking_and_optional_sections(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "custom.toml"
    target.write_text(
        '[llm]\n'
        'provider = "openrouter"\n'
        'model = "deepseek/deepseek-v4-flash"\n'
        'api_key_env = "CUSTOM_LLM_KEY"\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("CUSTOM_LLM_KEY", raising=False)

    result = runner.invoke(app, ["onboard", "status", "--config", str(target)])

    assert result.exit_code == 0, result.stdout
    assert "OpenSquilla Setup Cockpit" in result.stdout
    assert "Blocking setup: Provider" in result.stdout
    status_plain = _plain_text(result.stdout)
    assert "Optional later: LLM ensemble, Channels, Image generation, Voice audio" in status_plain
    assert "llm" not in result.stdout
    assert "image_generation" not in result.stdout


def test_onboard_status_explains_router_ready_before_provider_setup(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "custom.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(tmp_path / "default.toml"))

    result = runner.invoke(app, ["onboard", "status", "--config", str(target)])

    assert result.exit_code == 0, result.stdout
    assert "Router" in result.stdout
    collapsed = "".join(result.stdout.split())
    assert "Provider first" in result.stdout
    assert "usesSquillaRouterafter" in collapsed
    assert "providersetup" in collapsed

    json_result = runner.invoke(
        app,
        ["onboard", "status", "--json", "--config", str(target)],
    )
    assert json_result.exit_code == 0, json_result.stdout
    payload = _json.loads(json_result.stdout)
    assert (
        payload["sectionDetails"]["router"]["detail"]
        == "uses SquillaRouter after provider setup"
    )


def test_onboard_status_table_hides_image_generation_internal_source(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "custom.toml"
    target.write_text(
        '[llm]\n'
        'provider = "openrouter"\n'
        'model = "deepseek/deepseek-v4-flash"\n'
        'api_key = "sk-or"\n'
        '\n'
        '[image_generation]\n'
        'enabled = true\n'
        'primary = "openrouter/google/gemini-3.1-flash-image-preview"\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    result = runner.invoke(app, ["onboard", "status", "--config", str(target)])

    assert result.exit_code == 0, result.stdout
    assert "Image generation" in result.stdout
    assert "openrouter (same provider key)" in result.stdout
    assert "llm_fallback" not in result.stdout


def test_onboard_status_table_names_missing_env_keys_for_optional_capabilities(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "custom.toml"
    target.write_text(
        'search_provider = "brave"\n'
        'search_api_key_env = "BRAVE_SEARCH_API_KEY"\n'
        "\n"
        "[llm]\n"
        'provider = "openrouter"\n'
        'model = "deepseek/deepseek-v4-flash"\n'
        'api_key = "sk-or"\n'
        "\n"
        "[image_generation]\n"
        "enabled = true\n"
        'primary = "openai/gpt-image-1"\n'
        "\n"
        "[image_generation.providers.openai]\n"
        'api_key_env = "OPENAI_IMAGE_KEY"\n'
        "\n"
        "[memory.embedding]\n"
        'provider = "openai"\n'
        "\n"
        "[memory.embedding.remote]\n"
        'api_key_env = "OPENAI_EMBEDDINGS_API_KEY"\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_IMAGE_KEY", raising=False)
    monkeypatch.delenv("OPENAI_EMBEDDINGS_API_KEY", raising=False)

    result = runner.invoke(app, ["onboard", "status", "--config", str(target)])

    assert result.exit_code == 0, result.stdout
    assert "Blocking setup: Memory embedding" in result.stdout
    assert "Web search" in result.stdout
    assert "env key not visible" in result.stdout
    assert "BRAVE_SEARCH_API_KEY" in result.stdout
    assert "Image generation" in result.stdout
    assert "openai" in result.stdout
    assert "OPENAI_IMAGE_KEY" in result.stdout
    assert "Memory embedding" in result.stdout
    assert "OPENAI_EMBEDDINGS_API_KEY" in result.stdout
    stdout_plain = _plain_text(result.stdout)
    assert f"Set search key: {_env_hint('BRAVE_SEARCH_API_KEY')}" in stdout_plain
    assert f"Set image key: {_env_hint('OPENAI_IMAGE_KEY')}" in stdout_plain
    assert (
        f"Set memory key: {_env_hint('OPENAI_EMBEDDINGS_API_KEY')}" in stdout_plain
    )
    assert "Fix now:" in result.stdout
    assert result.stdout.index("Fix now:") < result.stdout.index("Set memory key:")
    assert result.stdout.index("Set image key:") < result.stdout.index("Setup paths:")
    assert "Set search key:" not in result.stdout.split("Setup paths:", 1)[1]
    assert "Web UI after env fix:" in result.stdout
    assert result.stdout.index("Set memory key:") < result.stdout.index("Set search key:")
    assert "not configured" not in result.stdout

    from opensquilla.cli.onboard_cmd import _status_cockpit_summary
    from opensquilla.onboarding.config_store import load_config
    from opensquilla.onboarding.status import get_onboarding_status

    summary = _status_cockpit_summary(get_onboarding_status(load_config(target)))
    assert summary == (
        "Blocking setup: Memory embedding"
        " · Optional later: LLM ensemble, Web search, Channels, Image generation, Voice audio"
    )


def test_onboard_status_offers_optional_capability_paths_when_core_ready(
    tmp_path,
):
    target = tmp_path / "custom.toml"
    target.write_text(
        "[llm]\n"
        'provider = "openrouter"\n'
        'model = "deepseek/deepseek-v4-flash"\n'
        'api_key = "sk-or"\n',
        encoding="utf-8",
    )

    result = runner.invoke(app, ["onboard", "status", "--config", str(target)])

    assert result.exit_code == 0, result.stdout
    assert "OpenSquilla ready: yes" in result.stdout
    assert "Optional next moves:" in result.stdout
    assert "Web UI:" in result.stdout
    assert "Explore options:" in result.stdout
    assert "Setup catalog:" not in result.stdout
    assert "opensquilla onboard catalog --json" not in result.stdout
    assert "Channel recipes:" in result.stdout
    compact = "".join(result.stdout.split())
    assert f"opensquillaonboardcatalog--config{_config_arg(target)}" in compact
    assert f"opensquillaonboardcatalogchannels--config{_config_arg(target)}" in compact
    assert "opensquillachannelsdescribe<type>--json" not in compact
    assert "opensquillaonboardconfigurechannels--channel-type<type>" not in compact
    assert "Image recipes:" in result.stdout
    assert (
        f"opensquillaonboardcatalogimage--config{_config_arg(target)}"
        in compact
    )
    assert "opensquillaonboardconfigureimage--image-provider<provider>" not in compact
    assert f"--config{_config_arg(target)}" in compact
    assert "Provider recipes:" not in result.stdout


def test_onboard_status_offers_ready_next_moves_when_all_sections_ready(
    tmp_path,
):
    target = tmp_path / "custom.toml"
    target.write_text(
        'search_provider = "duckduckgo"\n'
        "\n"
        "[llm]\n"
        'provider = "openrouter"\n'
        'model = "deepseek/deepseek-v4-flash"\n'
        'api_key = "sk-or"\n'
        "\n"
        "[image_generation]\n"
        "enabled = true\n"
        'primary = "openrouter/google/gemini-3.1-flash-image-preview"\n'
        "\n"
        "[[channels.channels]]\n"
        'type = "slack"\n'
        'name = "w"\n'
        'token = "xoxb-test"\n'
        "\n"
        "[audio]\n"
        "enabled = true\n"
        "\n"
        "[audio.providers.elevenlabs]\n"
        'api_key = "el-test"\n',
        encoding="utf-8",
    )

    result = runner.invoke(app, ["onboard", "status", "--config", str(target)])

    assert result.exit_code == 0, result.stdout
    assert "OpenSquilla ready: yes" in result.stdout
    assert "Optional next moves:" in result.stdout
    assert "Headless ensemble:" in result.stdout
    assert "opensquilla onboard configure ensemble --enabled" in result.stdout
    assert "Ready next moves:" not in result.stdout
    assert "Recommended next move:" not in result.stdout


def test_onboard_if_needed_non_tty_hint_keeps_explicit_config_path(
    tmp_path,
    monkeypatch,
):
    default_target = tmp_path / "default.toml"
    target = tmp_path / "custom.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(default_target))

    result = runner.invoke(app, ["onboard", "--if-needed", "--config", str(target)])

    assert result.exit_code == 2
    assert f"--config {_config_arg(target)}" in result.stdout
    assert "Guided CLI:" in result.stdout
    assert "Web UI:" in result.stdout
    assert "http://127.0.0.1:18791/control/setup" in result.stdout
    assert "Provider recipes:" in result.stdout
    assert "Check status:" in result.stdout
    assert not default_target.exists()


def test_onboard_if_needed_non_tty_hint_prioritizes_runnable_paths(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "custom.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))

    result = runner.invoke(app, ["onboard", "--if-needed"])

    assert result.exit_code == 2
    assert "Use a runnable setup path from this shell:" in result.stdout
    assert result.stdout.index("Web UI:") < result.stdout.index("Provider recipes:")
    assert result.stdout.index("Provider recipes:") < result.stdout.index("Guided CLI:")
    assert "Guided CLI: opensquilla onboard --if-needed" in result.stdout
    assert "(interactive terminal only)" in result.stdout
    assert not target.exists()


def test_onboard_if_needed_non_tty_hint_stays_provider_neutral(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "custom.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))

    result = runner.invoke(app, ["onboard", "--if-needed"])

    assert result.exit_code == 2
    assert "Provider recipes:" in result.stdout
    assert "opensquilla onboard catalog providers" in result.stdout
    assert "opensquilla onboard configure provider --provider <id>" not in result.stdout
    assert "--model <model>" not in result.stdout
    assert "--api-key-env <ENV_NAME>" not in result.stdout
    assert "openrouter" not in result.stdout.lower()
    assert "brave" not in result.stdout.lower()
    assert "slack" not in result.stdout.lower()
    assert "configure search" not in result.stdout
    assert "channels add" not in result.stdout
    assert not target.exists()


def test_onboard_if_needed_non_tty_hint_hides_disabled_web_ui_path(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "custom.toml"
    target.write_text("[control_ui]\nenabled = false\n", encoding="utf-8")
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))

    result = runner.invoke(app, ["onboard", "--if-needed"])

    assert result.exit_code == 2
    assert "Guided CLI:" in result.stdout
    assert "Provider recipes:" in result.stdout
    assert "Check status:" in result.stdout
    assert result.stdout.index("Provider recipes:") < result.stdout.index("Guided CLI:")
    assert "Web UI:" not in result.stdout
    assert "/control/setup" not in result.stdout


def test_onboard_if_needed_non_tty_hint_targets_blocking_memory_section(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "custom.toml"
    target.write_text(
        "[llm]\n"
        'provider = "openrouter"\n'
        'model = "deepseek/deepseek-v4-flash"\n'
        'api_key = "sk-or"\n'
        "\n"
        "[memory.embedding]\n"
        'provider = "openai"\n'
        "\n"
        "[memory.embedding.remote]\n"
        'api_key_env = "OPENAI_EMBEDDINGS_API_KEY"\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("OPENAI_EMBEDDINGS_API_KEY", raising=False)

    result = runner.invoke(app, ["onboard", "--if-needed", "--config", str(target)])

    assert result.exit_code == 2
    assert "Use a runnable setup path from this shell:" in result.stdout
    assert "Headless memory embedding:" in result.stdout
    assert (
        "opensquilla onboard configure memory --memory-provider auto"
        in result.stdout
    )
    assert f"--config {_config_arg(target)}" in result.stdout
    assert "Provider recipes:" not in result.stdout


@pytest.mark.parametrize(
    ("section", "label", "command"),
    [
        (
            "provider",
            "Provider recipes:",
            "opensquilla onboard catalog providers",
        ),
        (
            "router",
            "Headless router:",
            "opensquilla onboard configure router --router recommended --default-tier c1",
        ),
        (
            "ensemble",
            "Headless ensemble:",
            "opensquilla onboard configure ensemble --enabled",
        ),
        (
            "channels",
            "Channel recipes:",
            "opensquilla onboard catalog channels",
        ),
        (
            "search",
            "Headless search:",
            "opensquilla onboard configure search --search-provider duckduckgo",
        ),
        (
            "image-generation",
            "Image recipes:",
            "opensquilla onboard catalog image",
        ),
        (
            "memory-embedding",
            "Headless memory embedding:",
            "opensquilla onboard configure memory --memory-provider auto",
        ),
    ],
)
def test_configure_without_tty_hint_targets_selected_section(
    tmp_path,
    monkeypatch,
    section,
    label,
    command,
):
    target = tmp_path / "custom.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))

    result = runner.invoke(app, ["configure", section, "--config", str(target)])

    # A non-TTY fallthrough is a failure to configure anything: exit 2 like
    # bare `onboard`, with the hint still naming runnable setup paths.
    assert result.exit_code == 2
    assert label in result.stdout
    assert command in result.stdout
    assert f"--config {_config_arg(target)}" in result.stdout
    assert "opensquilla onboard configure provider --provider <id>" not in result.stdout
    assert "opensquilla channels describe <type>" not in result.stdout
    assert "--image-provider <provider>" not in result.stdout
    assert not target.exists()


def test_onboard_configure_provider_alias_uses_setup_engine(tmp_path, monkeypatch):
    target = tmp_path / "c.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))

    result = runner.invoke(
        app,
        [
            "onboard",
            "configure",
            "provider",
            "--provider",
            "openrouter",
            "--model",
            "deepseek/deepseek-v4-flash",
            "--api-key-env",
            "OPENROUTER_API_KEY",
        ],
    )

    assert result.exit_code == 0, result.stdout
    data = tomllib.loads(target.read_text())
    assert _effective_config(target).llm.provider == "openrouter"
    assert data["llm"]["api_key_env"] == "OPENROUTER_API_KEY"
    assert "api_key" not in data["llm"]


def test_configure_provider_noninteractive_uses_setup_engine(tmp_path, monkeypatch):
    target = tmp_path / "c.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))

    result = runner.invoke(
        app,
        [
            "configure",
            "provider",
            "--provider",
            "openrouter",
            "--model",
            "deepseek/deepseek-v4-flash",
            "--api-key-env",
            "OPENROUTER_API_KEY",
            "--proxy",
            "http://127.0.0.1:7890",
        ],
    )

    assert result.exit_code == 0, result.stdout
    data = tomllib.loads(target.read_text())
    assert _effective_config(target).llm.provider == "openrouter"
    assert data["llm"]["api_key_env"] == "OPENROUTER_API_KEY"
    assert data["llm"]["proxy"] == "http://127.0.0.1:7890"
    assert "api_key" not in data["llm"]


def test_configure_provider_uses_explicit_config_path(tmp_path, monkeypatch):
    default_target = tmp_path / "default.toml"
    target = tmp_path / "custom.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(default_target))

    result = runner.invoke(
        app,
        [
            "configure",
            "provider",
            "--provider",
            "openrouter",
            "--model",
            "deepseek/deepseek-v4-flash",
            "--api-key-env",
            "OPENROUTER_API_KEY",
            "--config",
            str(target),
        ],
    )

    assert result.exit_code == 0, result.stdout
    data = tomllib.loads(target.read_text())
    assert _effective_config(target).llm.provider == "openrouter"
    assert data["llm"]["api_key_env"] == "OPENROUTER_API_KEY"
    assert not default_target.exists()


def test_configure_provider_can_omit_model_for_router_profile(tmp_path, monkeypatch):
    target = tmp_path / "c.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))

    result = runner.invoke(
        app,
        [
            "configure",
            "provider",
            "--provider",
            "deepseek",
            "--api-key-env",
            "DEEPSEEK_API_KEY",
        ],
    )

    assert result.exit_code == 0, result.stdout
    data = tomllib.loads(target.read_text())
    assert data["llm"]["provider"] == "deepseek"
    assert data["llm"]["model"] == "deepseek-v4-flash"


def test_configure_provider_fails_closed_for_unclassified_foreign_router(tmp_path, monkeypatch):
    target = tmp_path / "c.toml"
    target.write_text(
        '[llm]\nprovider = "deepseek"\nmodel = "deepseek-chat"\n'
        '[squilla_router]\ntier_profile = "deepseek"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))

    result = runner.invoke(
        app,
        [
            "configure",
            "provider",
            "--provider",
            "openai",
            "--model",
            "gpt-5.4-mini",
            "--api-key-env",
            "OPENAI_API_KEY",
        ],
    )

    assert result.exit_code == 2
    assert "custom Router tiers reference provider(s)" in result.output
    data = tomllib.loads(target.read_text())
    # A legacy config has no explicit ownership binding, so a headless client
    # cannot assume consent to replace the ladder. The failed switch is atomic.
    assert data["llm"]["provider"] == "deepseek"
    assert data["squilla_router"]["tier_profile"] == "deepseek"


def test_configure_saved_path_escapes_rich_markup_chars(tmp_path, monkeypatch):
    root = tmp_path / "opensquilla-[review]"
    root.mkdir()
    target = root / "config[dev].toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))

    result = runner.invoke(
        app,
        [
            "configure",
            "provider",
            "--provider",
            "openrouter",
            "--model",
            "deepseek/deepseek-v4-flash",
            "--api-key-env",
            "OPENROUTER_API_KEY",
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert str(target) in result.stdout


def test_configure_provider_errors_go_to_stderr(tmp_path, monkeypatch):
    target = tmp_path / "c.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))

    result = runner.invoke(app, ["configure", "provider", "--provider", "not-a-provider"])

    assert result.exit_code == 2
    assert "unknown provider" in result.stderr
    assert "unknown provider" not in result.stdout


def test_configure_provider_reports_invalid_config_without_schema_traceback(tmp_path):
    target = tmp_path / "bad.toml"
    target.write_text("[search]\n", encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "configure",
            "provider",
            "--provider",
            "deepseek",
            "--model",
            "deepseek-chat",
            "--api-key-env",
            "DEEPSEEK_API_KEY",
            "--config",
            str(target),
        ],
    )

    assert result.exit_code == 2
    assert "OpenSquilla config error" in result.stderr
    assert _compact_text(str(target)) in _compact_text(result.stderr)
    assert "search" in result.stderr
    assert (
        f"opensquillaonboard--if-needed--config{_config_arg(target)}"
        in "".join(result.stderr.split())
    )
    assert "pydantic.dev" not in result.stderr
    assert "Traceback" not in result.stderr


def test_configure_router_noninteractive_can_disable(tmp_path, monkeypatch):
    target = tmp_path / "c.toml"
    target.write_text(
        '[llm]\nprovider = "openrouter"\nmodel = "deepseek/deepseek-v4-flash"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))

    result = runner.invoke(app, ["configure", "router", "--router", "disabled"])

    assert result.exit_code == 0, result.stdout
    data = tomllib.loads(target.read_text())
    assert data["squilla_router"]["enabled"] is False


def test_configure_router_noninteractive_can_set_default_tier(tmp_path, monkeypatch):
    target = tmp_path / "c.toml"
    target.write_text(
        '[llm]\nprovider = "deepseek"\nmodel = "deepseek-chat"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))

    result = runner.invoke(
        app,
        ["configure", "router", "--router", "recommended", "--default-tier", "c2"],
    )

    assert result.exit_code == 0, result.stdout
    data = tomllib.loads(target.read_text())
    router = _effective_config(target).squilla_router
    assert router.enabled is True
    assert router.tier_profile == "deepseek"
    assert data["squilla_router"]["default_tier"] == "c2"


def test_configure_router_rejects_invalid_default_tier_without_writing(
    tmp_path, monkeypatch
):
    target = tmp_path / "c.toml"
    target.write_text(
        '[llm]\nprovider = "ollama"\nmodel = "llama3"\n',
        encoding="utf-8",
    )
    before = target.read_text(encoding="utf-8")
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))

    result = runner.invoke(
        app,
        ["configure", "router", "--router", "recommended", "--default-tier", "bad"],
    )

    assert result.exit_code == 2
    assert "defaultTier must reference a text tier" in result.output
    assert "Traceback" not in result.output
    assert target.read_text(encoding="utf-8") == before


def test_configure_router_invalid_mode_reports_clean_error(tmp_path, monkeypatch):
    target = tmp_path / "c.toml"
    target.write_text(
        '[llm]\nprovider = "deepseek"\nmodel = "deepseek-chat"\n'
        '[squilla_router]\ntier_profile = "deepseek"\n',
        encoding="utf-8",
    )
    before = target.read_text(encoding="utf-8")
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))

    result = runner.invoke(app, ["configure", "router", "--router", "openrouter-mix"])

    assert result.exit_code == 2
    assert "openrouter-mix router mode is only valid" in result.output
    assert "Traceback" not in result.output
    assert target.read_text(encoding="utf-8") == before


def test_configure_search_noninteractive(tmp_path, monkeypatch):
    target = tmp_path / "c.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))

    result = runner.invoke(
        app,
        [
            "configure",
            "search",
            "--search-provider",
            "duckduckgo",
            "--max-results",
            "7",
            "--proxy",
            "http://127.0.0.1:7890",
            "--use-env-proxy",
            "--fallback-policy",
            "network",
            "--diagnostics",
        ],
    )

    assert result.exit_code == 0, result.stdout
    data = tomllib.loads(target.read_text())
    assert _effective_config(target).search_provider == "duckduckgo"
    assert data["search_max_results"] == 7
    assert data["search_proxy"] == "http://127.0.0.1:7890"
    assert data["search_use_env_proxy"] is True
    assert data["search_fallback_policy"] == "network"
    assert data["search_diagnostics"] is True


def test_configure_search_can_use_env_key_reference(tmp_path, monkeypatch):
    target = tmp_path / "c.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))
    monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)

    result = runner.invoke(
        app,
        [
            "configure",
            "search",
            "--search-provider",
            "brave",
            "--api-key-env",
            "BRAVE_SEARCH_API_KEY",
        ],
    )

    assert result.exit_code == 0, result.stdout
    data = tomllib.loads(target.read_text())
    assert data["search_provider"] == "brave"
    assert data["search_api_key_env"] == "BRAVE_SEARCH_API_KEY"
    assert "search_api_key" not in data
    assert "warning" in result.stdout.lower()
    assert "BRAVE_SEARCH_API_KEY" in result.stdout


def test_configure_image_generation_missing_env_is_blocked(tmp_path, monkeypatch):
    target = tmp_path / "c.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    result = runner.invoke(
        app,
        [
            "configure",
            "image-generation",
            "--image-provider",
            "openrouter",
            "--primary",
            "openrouter/google/gemini-3.1-flash-image-preview",
        ],
    )

    assert result.exit_code == 2
    assert "requires an api_key" in result.stderr


def test_configure_channel_noninteractive_adds_slack(tmp_path, monkeypatch):
    target = tmp_path / "c.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))

    result = runner.invoke(
        app,
        [
            "configure",
            "channel",
            "--channel-type",
            "slack",
            "--name",
            "work",
            "--token",
            "xoxb-secret",
            "--field",
            "signing_secret=ss",
        ],
    )

    assert result.exit_code == 0, result.stdout
    data = tomllib.loads(target.read_text())
    assert data["channels"]["channels"][0]["type"] == "slack"
    assert data["channels"]["channels"][0]["name"] == "work"


def test_configure_channels_noninteractive_accepts_spec_fields_for_feishu(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "c.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))

    result = runner.invoke(
        app,
        [
            "configure",
            "channels",
            "--channel-type",
            "feishu",
            "--name",
            "feishu-main",
            "--field",
            "app_id=cli_123",
            "--field",
            "app_secret=secret_123",
        ],
    )

    assert result.exit_code == 0, result.stdout
    data = tomllib.loads(target.read_text())
    entry = data["channels"]["channels"][0]
    assert entry["type"] == "feishu"
    assert entry["name"] == "feishu-main"
    assert entry["app_id"] == "cli_123"
    assert entry["app_secret"] == "secret_123"
    assert "secret_123" not in result.stdout


def test_configure_channels_rejects_unknown_field(tmp_path, monkeypatch):
    target = tmp_path / "c.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))

    result = runner.invoke(
        app,
        [
            "configure",
            "channels",
            "--channel-type",
            "feishu",
            "--name",
            "feishu-main",
            "--field",
            "not_a_field=value",
        ],
    )

    assert result.exit_code == 2
    assert "unknown field" in result.output.lower()
    assert not target.exists()


def test_configure_channels_token_only_does_not_complete_feishu(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "c.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))

    result = runner.invoke(
        app,
        [
            "configure",
            "channels",
            "--channel-type",
            "feishu",
            "--name",
            "feishu-main",
            "--token",
            "secret",
        ],
    )

    assert result.exit_code == 2
    assert "app_id" in result.output or "app_secret" in result.output
    assert not target.exists()


def test_configure_image_generation_noninteractive_uses_env(tmp_path, monkeypatch):
    target = tmp_path / "c.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-image-env")

    result = runner.invoke(
        app,
        [
            "configure",
            "image-generation",
            "--image-provider",
            "openrouter",
            "--primary",
            "openrouter/google/gemini-3.1-flash-image-preview",
        ],
    )

    assert result.exit_code == 0, result.stdout
    data = tomllib.loads(target.read_text())
    assert data["image_generation"]["enabled"] is True
    assert _effective_config(target).image_generation.providers.openrouter.api_key == ""


def test_configure_image_generation_can_use_nondefault_env_reference(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "c.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("OPENSQUILLA_TEST_IMAGE_KEY", "sk-image-env")

    result = runner.invoke(
        app,
        [
            "configure",
            "image-generation",
            "--image-provider",
            "openrouter",
            "--primary",
            "openrouter/google/gemini-3.1-flash-image-preview",
            "--api-key-env",
            "OPENSQUILLA_TEST_IMAGE_KEY",
        ],
    )

    assert result.exit_code == 0, result.stdout
    data = tomllib.loads(target.read_text())
    provider = data["image_generation"]["providers"]["openrouter"]
    assert _effective_config(target).image_generation.providers.openrouter.api_key == ""
    assert provider["api_key_env"] == "OPENSQUILLA_TEST_IMAGE_KEY"


def test_configure_image_generation_can_save_missing_env_reference(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "c.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))
    monkeypatch.delenv("OPENSQUILLA_TEST_IMAGE_KEY", raising=False)

    result = runner.invoke(
        app,
        [
            "configure",
            "image-generation",
            "--image-provider",
            "openrouter",
            "--primary",
            "openrouter/google/gemini-3.1-flash-image-preview",
            "--api-key-env",
            "OPENSQUILLA_TEST_IMAGE_KEY",
        ],
    )

    assert result.exit_code == 0, result.stdout
    data = tomllib.loads(target.read_text())
    provider = data["image_generation"]["providers"]["openrouter"]
    assert provider["api_key_env"] == "OPENSQUILLA_TEST_IMAGE_KEY"
    assert "warning" in result.stdout.lower()
    assert "OPENSQUILLA_TEST_IMAGE_KEY" in result.stdout


def test_configure_image_generation_can_disable_from_cli(tmp_path, monkeypatch):
    target = tmp_path / "c.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))
    monkeypatch.setenv("OPENSQUILLA_TEST_IMAGE_KEY", "sk-image-env")

    result = runner.invoke(
        app,
        [
            "configure",
            "image-generation",
            "--image-provider",
            "openrouter",
            "--primary",
            "openrouter/google/gemini-3.1-flash-image-preview",
            "--api-key-env",
            "OPENSQUILLA_TEST_IMAGE_KEY",
            "--no-image-enabled",
        ],
    )

    assert result.exit_code == 0, result.stdout
    data = tomllib.loads(target.read_text())
    provider = data["image_generation"]["providers"]["openrouter"]
    # The explicit off decision must be IN THE FILE, not just resolvable as
    # the model default — keep-current on a later rotation depends on it.
    assert data["image_generation"]["enabled"] is False
    assert _effective_config(target).image_generation.enabled is False
    assert data["image_generation"]["primary"] == (
        "openrouter/google/gemini-3.1-flash-image-preview"
    )
    assert provider["api_key_env"] == "OPENSQUILLA_TEST_IMAGE_KEY"


def test_configure_image_first_time_disable_survives_key_rotation(
    tmp_path, monkeypatch
):
    """R1 end-to-end: an explicit --no-image-enabled at FIRST configure (fresh
    config, value equal to the model default) must be written to the file and
    must survive a later key rotation that omits the flag — the rotation used
    to fall into configure-implies-enable and flip the tool back on."""
    target = tmp_path / "c.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))

    first = runner.invoke(
        app,
        [
            "configure",
            "image",
            "--image-provider",
            "openai",
            "--primary",
            "openai/gpt-image-1",
            "--api-key",
            "sk-image-1",
            "--no-image-enabled",
        ],
    )
    assert first.exit_code == 0, first.output
    data = tomllib.loads(target.read_text())
    assert data["image_generation"]["enabled"] is False

    rotation = runner.invoke(
        app,
        [
            "configure",
            "image",
            "--image-provider",
            "openai",
            "--api-key",
            "sk-image-2",
        ],
    )
    assert rotation.exit_code == 0, rotation.output
    data = tomllib.loads(target.read_text())
    assert data["image_generation"]["enabled"] is False
    assert data["image_generation"]["providers"]["openai"]["api_key"] == "sk-image-2"
    assert _effective_config(target).image_generation.enabled is False


def test_configure_image_generation_can_disable_without_provider_from_cli(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "c.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    # Seed the non-default state so a silent no-op cannot masquerade as a
    # save: the command must actually flip the on-disk value.
    target.write_text("[image_generation]\nenabled = true\n", encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "configure",
            "image-generation",
            "--no-image-enabled",
        ],
    )

    assert result.exit_code == 0, result.stdout
    data = tomllib.loads(target.read_text())
    assert data["image_generation"]["enabled"] is False
    assert _effective_config(target).image_generation.enabled is False
    assert "requires an api_key" not in result.output


def test_configure_accepts_short_capability_section_aliases(tmp_path, monkeypatch):
    target = tmp_path / "c.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    image = runner.invoke(app, ["configure", "image", "--no-image-enabled"])
    memory = runner.invoke(
        app,
        [
            "configure",
            "memory",
            "--memory-provider",
            "local",
            "--onnx-dir",
            "models/bge",
        ],
    )

    assert image.exit_code == 0, image.output
    assert memory.exit_code == 0, memory.output
    data = tomllib.loads(target.read_text())
    assert _effective_config(target).image_generation.enabled is False
    assert data["memory"]["embedding"]["provider"] == "local"
    assert data["memory"]["embedding"]["local"]["onnx_dir"] == "models/bge"


def test_configure_image_generation_can_disable_provider_without_key_from_cli(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "c.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    result = runner.invoke(
        app,
        [
            "configure",
            "image-generation",
            "--image-provider",
            "openrouter",
            "--primary",
            "openrouter/google/gemini-3.1-flash-image-preview",
            "--no-image-enabled",
        ],
    )

    assert result.exit_code == 0, result.output
    data = tomllib.loads(target.read_text())
    assert _effective_config(target).image_generation.enabled is False
    assert data["image_generation"]["primary"] == (
        "openrouter/google/gemini-3.1-flash-image-preview"
    )
    assert "requires an api_key" not in result.output


def test_configure_memory_embedding_noninteractive(tmp_path, monkeypatch):
    target = tmp_path / "c.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))

    result = runner.invoke(
        app,
        [
            "configure",
            "memory-embedding",
            "--memory-provider",
            "local",
            "--onnx-dir",
            "models/bge",
        ],
    )

    assert result.exit_code == 0, result.stdout
    data = tomllib.loads(target.read_text())
    assert data["memory"]["embedding"]["provider"] == "local"
    assert data["memory"]["embedding"]["local"]["onnx_dir"] == "models/bge"


def test_configure_memory_embedding_can_use_env_key_reference(tmp_path, monkeypatch):
    target = tmp_path / "c.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))
    monkeypatch.delenv("OPENAI_EMBEDDINGS_API_KEY", raising=False)

    result = runner.invoke(
        app,
        [
            "configure",
            "memory-embedding",
            "--memory-provider",
            "openai",
            "--model",
            "text-embedding-3-small",
            "--api-key-env",
            "OPENAI_EMBEDDINGS_API_KEY",
        ],
    )

    assert result.exit_code == 0, result.stdout
    data = tomllib.loads(target.read_text())
    remote = data["memory"]["embedding"]["remote"]
    assert data["memory"]["embedding"]["provider"] == "openai"
    assert remote["model"] == "text-embedding-3-small"
    assert remote["api_key_env"] == "OPENAI_EMBEDDINGS_API_KEY"
    assert "api_key" not in remote
    assert "warning" in result.stdout.lower()
    assert "OPENAI_EMBEDDINGS_API_KEY" in result.stdout


def test_onboard_without_tty_prints_hint_without_writing_config(tmp_path, monkeypatch):
    target = tmp_path / "c.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))

    result = runner.invoke(app, ["onboard"])

    assert result.exit_code == 2
    assert "requires a TTY" in result.stdout
    assert "Provider recipes:" in result.stdout
    assert "opensquilla onboard catalog providers" in result.stdout
    assert "--api-key-env" not in result.stdout
    assert "--api-key $" not in result.stdout
    assert "Check status:" in result.stdout
    assert not target.exists()


def test_init_help_mentions_onboard():
    result = runner.invoke(app, ["init", "--help"])
    assert "onboard" in result.stdout.lower()


def test_configure_ensemble_noninteractive(tmp_path, monkeypatch):
    target = tmp_path / "c.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))

    result = runner.invoke(
        app,
        [
            "configure",
            "ensemble",
            "--enabled",
            "--selection-mode",
            "router_dynamic",
            "--model-options",
            "prov/model-a, prov/model-b",
            "--min-successful-proposers",
            "2",
            "--all-failed-policy",
            "error",
        ],
    )

    assert result.exit_code == 0, result.output
    data = tomllib.loads(target.read_text())
    ensemble = data["llm_ensemble"]
    assert ensemble["enabled"] is True
    assert ensemble["selection_mode"] == "router_dynamic"
    assert ensemble["model_options"] == ["prov/model-a", "prov/model-b"]
    assert ensemble["min_successful_proposers"] == 2
    assert ensemble["all_failed_policy"] == "error"


def test_onboard_configure_ensemble_alias_uses_setup_engine(tmp_path, monkeypatch):
    target = tmp_path / "c.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))
    # Seed the non-default state: `--disabled` must observably flip the file,
    # not exit 0 while persisting/applying nothing.
    target.write_text("[llm_ensemble]\nenabled = true\n", encoding="utf-8")

    result = runner.invoke(
        app,
        ["onboard", "configure", "ensemble", "--disabled"],
    )

    assert result.exit_code == 0, result.output
    data = tomllib.loads(target.read_text())
    assert data["llm_ensemble"]["enabled"] is False
    assert _effective_config(target).llm_ensemble.enabled is False


def test_configure_ensemble_omitted_flags_keep_stored_values(tmp_path, monkeypatch):
    target = tmp_path / "c.toml"
    target.write_text(
        "[llm_ensemble]\n"
        "enabled = true\n"
        'selection_mode = "router_dynamic"\n'
        'model_options = ["stored/model-a", "stored/model-b"]\n'
        "min_successful_proposers = 3\n"
        'all_failed_policy = "error"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))

    result = runner.invoke(
        app,
        ["configure", "ensemble", "--min-successful-proposers", "2"],
    )

    assert result.exit_code == 0, result.output
    data = tomllib.loads(target.read_text())
    ensemble = data["llm_ensemble"]
    # Only the passed flag changed; everything else kept the stored values.
    assert ensemble["min_successful_proposers"] == 2
    assert ensemble["enabled"] is True
    assert ensemble["selection_mode"] == "router_dynamic"
    assert ensemble["model_options"] == ["stored/model-a", "stored/model-b"]
    assert ensemble["all_failed_policy"] == "error"


def test_configure_ensemble_rejects_invalid_selection_mode(tmp_path, monkeypatch):
    target = tmp_path / "c.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))

    result = runner.invoke(
        app,
        ["configure", "ensemble", "--selection-mode", "not-a-mode"],
    )

    assert result.exit_code == 2
    assert "selection_mode must be one of" in result.stderr
    assert "Traceback" not in result.output + result.stderr
    assert not target.exists()


def test_onboard_status_includes_ensemble_section(tmp_path, monkeypatch):
    target = tmp_path / "c.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))

    result = runner.invoke(app, ["onboard", "status", "--json", "--config", str(target)])

    assert result.exit_code == 0, result.stdout
    payload = _json.loads(result.stdout)
    assert payload["sections"]["ensemble"] == "optional"
    detail = payload["sectionDetails"]["ensemble"]
    assert detail["label"] == "LLM ensemble"
    assert detail["required"] is False
    assert detail["blocking"] is False


def test_onboard_status_table_shows_disabled_ensemble_detail(tmp_path, monkeypatch):
    target = tmp_path / "c.toml"
    target.write_text("[llm_ensemble]\nenabled = false\n", encoding="utf-8")
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))

    result = runner.invoke(app, ["onboard", "status", "--json", "--config", str(target)])

    assert result.exit_code == 0, result.stdout
    payload = _json.loads(result.stdout)
    assert payload["sections"]["ensemble"] == "optional"
    assert payload["sectionDetails"]["ensemble"]["detail"] == "disabled"


def _fake_probe_result(ok: bool, **overrides):
    from opensquilla.onboarding.probe import ProviderProbeResult

    async def fake_probe(**kwargs):
        fake_probe.calls.append(kwargs)
        return ProviderProbeResult(
            ok=ok,
            provider_id=kwargs["provider_id"],
            model=kwargs["model"],
            **overrides,
        )

    fake_probe.calls = []
    return fake_probe


def test_onboard_probe_flag_exits_zero_when_probe_passes(tmp_path, monkeypatch):
    from opensquilla.onboarding import probe as probe_module

    target = tmp_path / "c.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))
    fake_probe = _fake_probe_result(ok=True)
    monkeypatch.setattr(probe_module, "probe_llm_provider", fake_probe)

    result = runner.invoke(
        app,
        [
            "onboard",
            "--provider", "openrouter",
            "--model", "deepseek/deepseek-v4-flash",
            "--api-key", "sk",
            "--minimal",
            "--probe",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Probe OK" in result.stdout
    assert [call["provider_id"] for call in fake_probe.calls] == ["openrouter"]
    # The probe verifies exactly the credentials that were just saved.
    assert fake_probe.calls[0]["api_key"] == "sk"
    assert "sk" in target.read_text()


def test_onboard_probe_flag_exits_nonzero_when_probe_fails(tmp_path, monkeypatch):
    from opensquilla.onboarding import probe as probe_module

    target = tmp_path / "c.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))
    fake_probe = _fake_probe_result(
        ok=False,
        failure_kind="auth_invalid",
        message="Incorrect API key provided",
    )
    monkeypatch.setattr(probe_module, "probe_llm_provider", fake_probe)

    result = runner.invoke(
        app,
        [
            "onboard",
            "--provider", "openrouter",
            "--model", "deepseek/deepseek-v4-flash",
            "--api-key", "sk-bad",
            "--minimal",
            "--probe",
        ],
    )

    assert result.exit_code == 1
    assert "Probe failed" in result.stderr
    assert "auth_invalid" in result.stderr
    assert "Incorrect API key provided" in result.stderr
    # The probe is a CI gate, not a rollback: the config stays saved.
    assert "openrouter" in target.read_text()


def test_onboard_without_probe_flag_never_probes(tmp_path, monkeypatch):
    from opensquilla.onboarding import probe as probe_module

    target = tmp_path / "c.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))

    def unexpected_probe(**_kwargs):
        raise AssertionError("the probe must only run behind --probe")

    monkeypatch.setattr(probe_module, "probe_llm_provider", unexpected_probe)

    result = runner.invoke(
        app,
        [
            "onboard",
            "--provider", "openrouter",
            "--model", "deepseek/deepseek-v4-flash",
            "--api-key", "sk",
            "--minimal",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Probe" not in result.stdout


def test_advertised_router_command_actionable_for_non_registry_provider(
    tmp_path, monkeypatch
):
    """X1: the advertised next-steps router one-liner against a hand-edited,
    non-registry llm.provider used to exit 2 with the cryptic "router tier
    'c0' must be an object"; the error must now name the provider and the
    two runnable ways out (exit code stays 2)."""
    target = tmp_path / "c.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))
    target.write_text(
        '[llm]\nprovider = "acme-llm"\nmodel = "acme-model"\n', encoding="utf-8"
    )

    result = runner.invoke(
        app,
        [
            "onboard",
            "configure",
            "router",
            "--router",
            "recommended",
            "--default-tier",
            "c1",
        ],
    )

    assert result.exit_code == 2, result.output
    text = _plain_text(result.output)
    assert "acme-llm" in text
    assert "opensquilla onboard configure provider --provider" in text
    assert "--router disabled" in text
    assert "must be an object" not in text


def test_onboard_interactive_value_error_is_productized(tmp_path, monkeypatch):
    """R9 boundary: a mutation-level ValueError escaping the wizard (e.g.
    "model is required" for a provider without a derivable default) must
    exit 2 with a productized message, not a raw traceback."""
    from opensquilla.cli import onboard_cmd

    target = tmp_path / "c.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))

    def boom(_options):
        raise ValueError("model is required")

    monkeypatch.setattr(onboard_cmd, "run_interactive_onboard", boom)

    result = runner.invoke(app, ["onboard"])

    assert result.exit_code == 2, result.output
    assert "model is required" in _plain_text(result.output)
    assert "Traceback" not in result.output


def test_configure_interactive_value_error_is_productized(tmp_path, monkeypatch):
    """R8 boundary: a mutation ValueError from the configure wizard (e.g. a
    blank required channel secret) must exit 2 like the headless path."""
    from opensquilla.cli import onboard_cmd

    target = tmp_path / "c.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))

    def boom(_section, config_path=None):
        raise ValueError("channel field 'token' requires a non-empty value")

    monkeypatch.setattr(onboard_cmd, "run_interactive_configure", boom)

    result = runner.invoke(app, ["configure", "channels"])

    assert result.exit_code == 2, result.output
    assert "requires a non-empty value" in _plain_text(result.output)
    assert "Traceback" not in result.output
