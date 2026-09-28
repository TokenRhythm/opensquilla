"""Regression tests for review findings on the headless onboard CLI.

Covers: keep-current --router on re-saves (S1), EOFError cancellation
productization (S4), restart guidance at the bare-onboard boundary (F17),
wizard-phase OSError diagnosis (F27), validation-error redaction (F29), and
the single-preflight-load contract of the --provider path (F41).
All tests are offline and use synthetic dummy data only.
"""

from __future__ import annotations

import errno
import re
import tomllib

import pytest
from typer.testing import CliRunner

from opensquilla.cli.main import app
from opensquilla.config_version import LATEST_CONFIG_VERSION
from opensquilla.onboarding.config_store import load_config

runner = CliRunner()
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def _plain(value: str) -> str:
    return " ".join(_ANSI_RE.sub("", value).split())


# ---------------------------------------------------------------------------
# S1: bare `onboard --provider` re-saves keep the stored router state when
# --router is omitted; explicit --router and first-run behavior are unchanged.
# ---------------------------------------------------------------------------


def test_onboard_provider_key_rotation_keeps_router_disabled_and_model(
    tmp_path, monkeypatch
):
    target = tmp_path / "c.toml"
    target.write_text(
        f"config_version = {LATEST_CONFIG_VERSION}\n"
        "[llm]\n"
        'provider = "openrouter"\n'
        'model = "custom/model-x"\n'
        'api_key = "sk-old"\n'
        "\n"
        "[squilla_router]\n"
        "enabled = false\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))

    result = runner.invoke(
        app,
        ["onboard", "--provider", "openrouter", "--api-key", "sk-new", "--minimal"],
    )

    assert result.exit_code == 0, result.output
    cfg = load_config(target)
    assert cfg.squilla_router.enabled is False
    assert cfg.llm.model == "custom/model-x"
    assert cfg.llm.api_key == "sk-new"
    data = tomllib.loads(target.read_text(encoding="utf-8"))
    assert data["squilla_router"]["enabled"] is False


def test_onboard_provider_key_rotation_keeps_hand_customized_tiers(
    tmp_path, monkeypatch
):
    target = tmp_path / "c.toml"
    target.write_text(
        f"config_version = {LATEST_CONFIG_VERSION}\n"
        "[llm]\n"
        'provider = "openrouter"\n'
        'model = "custom/model-x"\n'
        'api_key = "sk-old"\n'
        "\n"
        "[squilla_router.tiers.c0]\n"
        'provider = "openrouter"\n'
        'model = "my-org/custom-c0"\n'
        "[squilla_router.tiers.c1]\n"
        'provider = "openrouter"\n'
        'model = "my-org/custom-c1"\n'
        "[squilla_router.tiers.c2]\n"
        'provider = "openrouter"\n'
        'model = "my-org/custom-c2"\n'
        "[squilla_router.tiers.c3]\n"
        'provider = "openrouter"\n'
        'model = "my-org/custom-c3"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))

    result = runner.invoke(
        app,
        ["onboard", "--provider", "openrouter", "--api-key", "sk-new", "--minimal"],
    )

    assert result.exit_code == 0, result.output
    cfg = load_config(target)
    for tier in ("c0", "c1", "c2", "c3"):
        assert cfg.squilla_router.tiers[tier]["model"] == f"my-org/custom-{tier}"
    assert cfg.llm.api_key == "sk-new"


def test_onboard_provider_explicit_router_flag_still_applies_on_resave(
    tmp_path, monkeypatch
):
    target = tmp_path / "c.toml"
    target.write_text(
        "[llm]\n"
        'provider = "openrouter"\n'
        'model = "custom/model-x"\n'
        'api_key = "sk-old"\n'
        "\n"
        "[squilla_router]\n"
        "enabled = false\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))

    result = runner.invoke(
        app,
        [
            "onboard",
            "--provider",
            "openrouter",
            "--api-key",
            "sk-new",
            "--router",
            "recommended",
            "--minimal",
        ],
    )

    assert result.exit_code == 0, result.output
    cfg = load_config(target)
    assert cfg.squilla_router.enabled is True
    assert cfg.squilla_router.tier_profile == "openrouter"


def test_onboard_provider_first_run_still_applies_recommended_router(
    tmp_path, monkeypatch
):
    # Fresh/unconfigured install: an omitted --router keeps today's first-run
    # behavior and applies the recommended profile.
    target = tmp_path / "c.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))
    monkeypatch.delenv("OPENSQUILLA_LLM_API_KEY", raising=False)

    result = runner.invoke(
        app,
        ["onboard", "--provider", "openrouter", "--api-key", "sk", "--minimal"],
    )

    assert result.exit_code == 0, result.output
    data = tomllib.loads(target.read_text(encoding="utf-8"))
    assert data["squilla_router"]["tier_profile"] == "openrouter"
    cfg = load_config(target)
    assert cfg.squilla_router.enabled is True


def test_onboard_provider_first_run_with_env_key_still_applies_router(
    tmp_path, monkeypatch
):
    # An env-absorbed credential must not make a fresh install look
    # configured: the recommended profile still applies on first setup.
    target = tmp_path / "c.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))
    monkeypatch.setenv("OPENSQUILLA_LLM_API_KEY", "sk-from-env")

    result = runner.invoke(
        app,
        ["onboard", "--provider", "openrouter", "--api-key", "sk", "--minimal"],
    )

    assert result.exit_code == 0, result.output
    data = tomllib.loads(target.read_text(encoding="utf-8"))
    assert data["squilla_router"]["tier_profile"] == "openrouter"


# ---------------------------------------------------------------------------
# S4: EOFError (Ctrl+D / exhausted piped stdin) must be productized exactly
# like Esc/Ctrl+C — one short "Setup cancelled" line, exit 130.
# ---------------------------------------------------------------------------


def test_onboard_wizard_eof_is_productized_as_cancellation(tmp_path, monkeypatch):
    from opensquilla.cli import onboard_cmd

    target = tmp_path / "c.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))

    def eof(_options):
        raise EOFError

    monkeypatch.setattr(onboard_cmd, "run_interactive_onboard", eof)

    result = runner.invoke(app, ["onboard"])

    assert result.exit_code == 130
    assert "Setup cancelled" in _plain(result.stderr)
    assert "Aborted" not in result.output + result.stderr
    assert "Traceback" not in result.output + result.stderr
    assert not target.exists()


def test_configure_wizard_eof_is_productized_as_cancellation(tmp_path, monkeypatch):
    from opensquilla.cli import onboard_cmd

    target = tmp_path / "c.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))

    def eof(_section, *, config_path=None):
        raise EOFError

    monkeypatch.setattr(onboard_cmd, "run_interactive_configure", eof)

    result = runner.invoke(app, ["onboard", "configure", "router"])

    assert result.exit_code == 130
    assert "Setup cancelled" in _plain(result.stderr)
    assert "Aborted" not in result.output + result.stderr
    assert "Traceback" not in result.output + result.stderr
    assert not target.exists()


# ---------------------------------------------------------------------------
# F17: bare `onboard` must surface PersistResult.restart_required with the
# same structured guidance `onboard configure` prints.
# ---------------------------------------------------------------------------


def test_bare_onboard_prints_restart_guidance_when_result_requires_it(
    tmp_path, monkeypatch
):
    from opensquilla.cli import onboard_cmd
    from opensquilla.onboarding.config_store import PersistResult

    target = tmp_path / "c.toml"
    target.write_text(
        '[llm]\nprovider = "openrouter"\nmodel = "m"\napi_key = "sk"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))

    def hub_edit(_options):
        # Simulates onboard -> "Change specific sections" -> a channel edit:
        # the aggregated result carries the sticky restart flag.
        return PersistResult(
            path=target,
            backup_path=None,
            restart_required=True,
            warnings=[],
        )

    monkeypatch.setattr(onboard_cmd, "run_interactive_onboard", hub_edit)

    result = runner.invoke(app, ["onboard"])

    assert result.exit_code == 0, result.output
    plain = _plain(result.stdout)
    assert "restart required" in plain
    assert "opensquilla gateway restart" in plain


def test_bare_onboard_prints_no_restart_guidance_without_flag(tmp_path, monkeypatch):
    from opensquilla.cli import onboard_cmd
    from opensquilla.onboarding.config_store import PersistResult

    target = tmp_path / "c.toml"
    target.write_text(
        '[llm]\nprovider = "openrouter"\nmodel = "m"\napi_key = "sk"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))

    monkeypatch.setattr(
        onboard_cmd,
        "run_interactive_onboard",
        lambda _options: PersistResult(
            path=target,
            backup_path=None,
            restart_required=False,
            warnings=[],
        ),
    )

    result = runner.invoke(app, ["onboard"])

    assert result.exit_code == 0, result.output
    assert "restart required" not in _plain(result.stdout)


# ---------------------------------------------------------------------------
# F27: an OSError raised INSIDE the wizard (e.g. disk full during the final
# persist) is a write failure, not a config-load error — the CLI must not
# claim the config failed to load or suggest editing/moving it.
# ---------------------------------------------------------------------------


def test_onboard_wizard_write_oserror_is_not_misdiagnosed_as_load_error(
    tmp_path, monkeypatch
):
    from opensquilla.cli import onboard_cmd

    target = tmp_path / "c.toml"
    target.write_text(
        '[llm]\nprovider = "openrouter"\nmodel = "m"\napi_key = "sk"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))

    def disk_full(_options):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(onboard_cmd, "run_interactive_onboard", disk_full)

    result = runner.invoke(app, ["onboard"])

    assert result.exit_code == 2
    plain = _plain(result.stderr)
    assert "No space left on device" in plain
    assert "OpenSquilla config error" not in plain
    assert "edit or move this config" not in plain
    assert "Traceback" not in result.output + result.stderr


def test_onboard_prevalidates_config_before_entering_wizard(tmp_path, monkeypatch):
    from opensquilla.cli import onboard_cmd

    target = tmp_path / "c.toml"
    target.write_text("not toml :::", encoding="utf-8")
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))

    def must_not_run(_options):  # pragma: no cover - the pre-check exits first
        raise AssertionError("the wizard must not start over a corrupt config")

    monkeypatch.setattr(onboard_cmd, "run_interactive_onboard", must_not_run)

    result = runner.invoke(app, ["onboard"])

    assert result.exit_code == 2
    assert "OpenSquilla config error" in result.stderr


# ---------------------------------------------------------------------------
# F29: a validator message that interpolates the offending input must be
# masked by the free-text redactor before it reaches stderr.
# ---------------------------------------------------------------------------


def test_onboard_provider_validation_error_redacts_secret_shaped_values(
    tmp_path, monkeypatch
):
    from pydantic import BaseModel, ValidationError, field_validator

    from opensquilla.cli import onboard_cmd

    secret = "sk-live-SECRETSECRETSECRETSECRET123456"

    class _Probe(BaseModel):
        api_key: str

        @field_validator("api_key")
        @classmethod
        def _reject(cls, value: str) -> str:
            raise ValueError(f"key {value!r} looks malformed")

    try:
        _Probe(api_key=secret)
    except ValidationError as exc:
        captured = exc
    else:  # pragma: no cover - the construction above always fails
        raise AssertionError("expected a ValidationError")

    target = tmp_path / "c.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))

    class _RaisingEngine:
        def __init__(self, *_a, **_kw):
            pass

        def apply(self, *_a, **_kw):
            raise captured

        def persist(self, *_a, **_kw):  # pragma: no cover - apply raises first
            raise AssertionError("persist must not run after a failed apply")

    monkeypatch.setattr(onboard_cmd, "SetupEngine", _RaisingEngine)

    result = runner.invoke(
        app,
        ["onboard", "--provider", "openrouter", "--api-key", secret, "--minimal"],
    )

    assert result.exit_code == 2
    combined = result.output + result.stderr
    assert secret not in combined
    assert "***" in _plain(result.stderr)
    assert "api_key" in _plain(result.stderr)
    assert not target.exists()


def test_format_validation_error_masks_interpolated_secret_unit():
    from pydantic import BaseModel, ValidationError, field_validator

    from opensquilla.cli.onboard_cmd import _format_validation_error

    secret = "sk-live-UNITSECRETUNITSECRET987654"

    class _Probe(BaseModel):
        api_key: str

        @field_validator("api_key")
        @classmethod
        def _reject(cls, value: str) -> str:
            raise ValueError(f"key {value!r} looks malformed")

    with pytest.raises(ValidationError) as excinfo:
        _Probe(api_key=secret)

    rendered = _format_validation_error(excinfo.value)
    assert secret not in rendered
    assert "***" in rendered
    assert "api_key" in rendered


# ---------------------------------------------------------------------------
# F41: the `onboard --provider` path must reuse its preflight load for the
# save instead of loading the config again inside the engine.
# ---------------------------------------------------------------------------


def test_onboard_provider_reuses_preflight_load_for_the_save(tmp_path, monkeypatch):
    from opensquilla.cli import onboard_cmd
    from opensquilla.onboarding import setup_engine

    target = tmp_path / "c.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))

    def engine_must_not_load(*_a, **_kw):  # pragma: no cover - regression guard
        raise AssertionError(
            "SetupEngine must receive the preflight-loaded config, not reload it"
        )

    monkeypatch.setattr(setup_engine, "load_config", engine_must_not_load)

    load_calls: list[object] = []
    real_load = onboard_cmd.load_config

    def counting_load(path=None):
        load_calls.append(path)
        return real_load(path)

    monkeypatch.setattr(onboard_cmd, "load_config", counting_load)

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
            "--minimal",
        ],
    )

    assert result.exit_code == 0, result.output
    # One preflight load feeding the engine + one post-save load for the
    # handoff summary; the previous shape performed three full loads.
    assert len(load_calls) == 2
    assert "openrouter" in target.read_text(encoding="utf-8")
