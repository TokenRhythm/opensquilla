"""Headless `onboard`/`configure` CLI semantics.

Pins the gate-flag error contract (explicit-but-incomplete flag sets exit 2),
keep-current re-saves, cancellation and config-error productization, restart
guidance, the voice-audio catalog section, and strict `--field` coercion.
All tests are offline and use synthetic dummy data only.
"""

from __future__ import annotations

import re

import pytest
from typer.testing import CliRunner

from opensquilla.cli.main import app
from opensquilla.onboarding.config_store import load_config

runner = CliRunner()
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def _plain(value: str) -> str:
    return " ".join(_ANSI_RE.sub("", value).split())


# ---------------------------------------------------------------------------
# B2-1: explicit-but-incomplete headless flag combinations must exit 2 and
# name the missing gate flag instead of silently no-opping with exit 0.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("args", "missing_flag"),
    [
        (["configure", "router", "--default-tier", "c2"], "--router"),
        (["configure", "provider", "--model", "some/model"], "--provider"),
        (["configure", "search", "--max-results", "9"], "--search-provider"),
        (["configure", "channels", "--channel-type", "slack"], "--name"),
        (["configure", "channels", "--name", "work"], "--channel-type"),
        (["configure", "image", "--primary", "openrouter/x"], "--image-provider"),
        (["configure", "memory", "--onnx-dir", "models/bge"], "--memory-provider"),
    ],
)
def test_configure_incomplete_flags_exit_2_naming_missing_gate(
    tmp_path, monkeypatch, args, missing_flag
):
    target = tmp_path / "c.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))

    result = runner.invoke(app, ["onboard", *args])

    assert result.exit_code == 2, result.output
    assert missing_flag in _plain(result.stderr)
    assert "no changes were made" in _plain(result.stderr)
    assert not target.exists()


def test_configure_router_incomplete_flags_do_not_touch_existing_config(
    tmp_path, monkeypatch
):
    target = tmp_path / "c.toml"
    target.write_text(
        '[llm]\nprovider = "openrouter"\nmodel = "m"\napi_key = "sk"\n',
        encoding="utf-8",
    )
    before = target.read_text(encoding="utf-8")
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))

    result = runner.invoke(app, ["onboard", "configure", "router", "--default-tier", "c2"])

    assert result.exit_code == 2
    assert target.read_text(encoding="utf-8") == before


def test_configure_flags_without_section_exit_2(tmp_path, monkeypatch):
    target = tmp_path / "c.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))

    result = runner.invoke(app, ["onboard", "configure", "--router", "recommended"])

    assert result.exit_code == 2
    assert "target section" in _plain(result.stderr)
    assert not target.exists()


def test_configure_unknown_section_exits_2(tmp_path, monkeypatch):
    target = tmp_path / "c.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))

    result = runner.invoke(app, ["onboard", "configure", "not-a-section"])

    assert result.exit_code == 2
    assert "unknown configure section" in _plain(result.stderr)
    assert "not-a-section" in _plain(result.stderr)
    assert not target.exists()


def test_configure_audio_section_exits_2_with_catalog_pointer(tmp_path, monkeypatch):
    target = tmp_path / "c.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))

    result = runner.invoke(app, ["onboard", "configure", "audio"])

    assert result.exit_code == 2
    assert "opensquilla onboard catalog audio" in _plain(result.stderr)
    assert not target.exists()


def test_configure_bare_non_tty_exits_2_with_hint(tmp_path, monkeypatch):
    target = tmp_path / "c.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))

    result = runner.invoke(app, ["onboard", "configure"])

    assert result.exit_code == 2
    assert "requires a TTY" in result.stdout
    assert not target.exists()


# ---------------------------------------------------------------------------
# B2-2: `onboard catalog audio` must render usable output, and the overview
# must not leak the raw audioProviders key.
# ---------------------------------------------------------------------------


def test_onboard_catalog_audio_prints_provider_rows(tmp_path, monkeypatch):
    target = tmp_path / "c.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))

    result = runner.invoke(app, ["onboard", "catalog", "audio"])

    assert result.exit_code == 0, result.stdout
    assert result.stdout.strip()
    assert "voice audio provider options" in result.stdout
    assert "elevenlabs" in result.stdout
    assert "ELEVENLABS_API_KEY" in result.stdout
    assert "Try:" in result.stdout
    assert not target.exists()


def test_onboard_catalog_overview_names_voice_audio_without_raw_key(
    tmp_path, monkeypatch
):
    target = tmp_path / "c.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))

    result = runner.invoke(app, ["onboard", "catalog"])

    assert result.exit_code == 0, result.stdout
    assert "Voice audio providers" in result.stdout
    assert "audioProviders" not in result.stdout
    compact = "".join(result.stdout.split())
    assert "opensquillaonboardcatalogaudio" in compact


# ---------------------------------------------------------------------------
# B2-3: an explicitly stored enabled=false must survive an image key rotation
# that omits --image-enabled/--no-image-enabled.
# ---------------------------------------------------------------------------


def test_configure_image_rotation_keeps_deliberate_disabled_state(
    tmp_path, monkeypatch
):
    target = tmp_path / "c.toml"
    target.write_text(
        '[llm]\nprovider = "openrouter"\nmodel = "m"\napi_key = "sk"\n'
        "\n"
        "[image_generation]\n"
        "enabled = false\n"
        'primary = "openrouter/google/gemini-3.1-flash-image-preview"\n'
        "\n"
        "[image_generation.providers.openrouter]\n"
        'api_key_env = "OPENSQUILLA_TEST_IMAGE_KEY"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))
    monkeypatch.setenv("OPENSQUILLA_TEST_IMAGE_KEY", "sk-image-env")

    result = runner.invoke(
        app,
        [
            "onboard",
            "configure",
            "image",
            "--image-provider",
            "openrouter",
            "--primary",
            "openrouter/google/gemini-3.1-flash-image-preview",
            "--api-key-env",
            "OPENSQUILLA_TEST_IMAGE_KEY",
        ],
    )

    assert result.exit_code == 0, result.output
    assert load_config(target).image_generation.enabled is False


def test_configure_image_rotation_keeps_enabled_true(tmp_path, monkeypatch):
    target = tmp_path / "c.toml"
    target.write_text(
        "[image_generation]\n"
        "enabled = true\n"
        'primary = "openrouter/google/gemini-3.1-flash-image-preview"\n'
        "\n"
        "[image_generation.providers.openrouter]\n"
        'api_key_env = "OPENSQUILLA_TEST_IMAGE_KEY"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))
    monkeypatch.setenv("OPENSQUILLA_TEST_IMAGE_KEY", "sk-image-env")

    result = runner.invoke(
        app,
        [
            "onboard",
            "configure",
            "image",
            "--image-provider",
            "openrouter",
            "--primary",
            "openrouter/google/gemini-3.1-flash-image-preview",
            "--api-key-env",
            "OPENSQUILLA_TEST_IMAGE_KEY",
        ],
    )

    assert result.exit_code == 0, result.output
    assert load_config(target).image_generation.enabled is True


# ---------------------------------------------------------------------------
# B2-4: key rotation must not clobber stored provider/search settings when
# the corresponding flags are omitted.
# ---------------------------------------------------------------------------


def test_configure_provider_key_rotation_keeps_model_base_url_and_proxy(
    tmp_path, monkeypatch
):
    target = tmp_path / "c.toml"
    target.write_text(
        "[llm]\n"
        'provider = "openrouter"\n'
        'model = "custom/model-x"\n'
        'api_key = "sk-old"\n'
        'base_url = "https://gateway.example.test/v1"\n'
        'proxy = "http://127.0.0.1:7890"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))

    result = runner.invoke(
        app,
        [
            "onboard",
            "configure",
            "provider",
            "--provider",
            "openrouter",
            "--api-key",
            "sk-new",
        ],
    )

    assert result.exit_code == 0, result.output
    cfg = load_config(target)
    assert cfg.llm.model == "custom/model-x"
    assert cfg.llm.base_url == "https://gateway.example.test/v1"
    assert cfg.llm.proxy == "http://127.0.0.1:7890"
    assert cfg.llm.api_key == "sk-new"


def test_configure_provider_explicit_router_disabled_is_authoritative(
    tmp_path, monkeypatch
):
    # Issue #1341: `configure provider --router disabled` on a fresh config
    # used to drop the flag and synthesize the default tier ladder instead.
    # The explicit flag must be authoritative, matching the command help.
    target = tmp_path / "c.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))

    result = runner.invoke(
        app,
        [
            "onboard",
            "configure",
            "provider",
            "--provider",
            "vllm",
            "--model",
            "qa-model",
            "--base-url",
            "http://127.0.0.1:18001/v1",
            "--router",
            "disabled",
        ],
    )

    assert result.exit_code == 0, result.output
    cfg = load_config(target)
    assert cfg.squilla_router.enabled is False


def test_configure_provider_omitted_router_keeps_disabled_state(
    tmp_path, monkeypatch
):
    # Keep-current contract: a provider re-save without --router must never
    # re-enable a deliberately disabled router.
    target = tmp_path / "c.toml"
    target.write_text(
        "[llm]\n"
        'provider = "vllm"\n'
        'model = "qa-model"\n'
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
            "configure",
            "provider",
            "--provider",
            "vllm",
            "--api-key",
            "sk-new",
        ],
    )

    assert result.exit_code == 0, result.output
    cfg = load_config(target)
    assert cfg.squilla_router.enabled is False
    assert cfg.llm.api_key == "sk-new"


def test_configure_provider_invalid_router_mode_exits_2(tmp_path, monkeypatch):
    # Plumb-through makes an invalid --router a real mutation error: it must
    # exit 2 with the mode validation message instead of being ignored.
    target = tmp_path / "c.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))

    result = runner.invoke(
        app,
        [
            "onboard",
            "configure",
            "provider",
            "--provider",
            "vllm",
            "--model",
            "qa-model",
            "--base-url",
            "http://127.0.0.1:18001/v1",
            "--router",
            "nonsense",
        ],
    )

    assert result.exit_code == 2, result.output
    assert "router mode" in _plain(result.stderr)


def test_onboard_provider_key_rotation_keeps_base_url_and_proxy(
    tmp_path, monkeypatch
):
    # The top-level `onboard --provider` path must honor the same
    # keep-current contract as `configure provider`: an omitted --router
    # never re-applies a router profile on a configured install, so the
    # stored model and endpoint settings all survive a key rotation.
    target = tmp_path / "c.toml"
    target.write_text(
        "[llm]\n"
        'provider = "openrouter"\n'
        'model = "custom/model-x"\n'
        'api_key = "sk-old"\n'
        'base_url = "https://gateway.example.test/v1"\n'
        'proxy = "http://127.0.0.1:7890"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))

    result = runner.invoke(
        app,
        ["onboard", "--provider", "openrouter", "--api-key", "sk-new", "--minimal"],
    )

    assert result.exit_code == 0, result.output
    cfg = load_config(target)
    assert cfg.llm.model == "custom/model-x"
    assert cfg.llm.base_url == "https://gateway.example.test/v1"
    assert cfg.llm.proxy == "http://127.0.0.1:7890"
    assert cfg.llm.api_key == "sk-new"


def test_onboard_provider_key_rotation_with_router_disabled_keeps_model(
    tmp_path, monkeypatch
):
    target = tmp_path / "c.toml"
    target.write_text(
        "[llm]\n"
        'provider = "openrouter"\n'
        'model = "custom/model-x"\n'
        'api_key = "sk-old"\n',
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
            "disabled",
            "--minimal",
        ],
    )

    assert result.exit_code == 0, result.output
    cfg = load_config(target)
    assert cfg.llm.model == "custom/model-x"
    assert cfg.llm.api_key == "sk-new"


def test_configure_search_key_rotation_keeps_stored_global_settings(
    tmp_path, monkeypatch
):
    target = tmp_path / "c.toml"
    target.write_text(
        'search_provider = "brave"\n'
        'search_api_key = "sk-old"\n'
        "search_max_results = 9\n"
        'search_proxy = "http://127.0.0.1:7890"\n'
        "search_use_env_proxy = true\n"
        'search_fallback_policy = "network"\n'
        "search_diagnostics = true\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))

    result = runner.invoke(
        app,
        [
            "onboard",
            "configure",
            "search",
            "--search-provider",
            "brave",
            "--api-key",
            "sk-new",
        ],
    )

    assert result.exit_code == 0, result.output
    cfg = load_config(target)
    assert cfg.search_max_results == 9
    assert cfg.search_proxy == "http://127.0.0.1:7890"
    assert cfg.search_use_env_proxy is True
    assert cfg.search_fallback_policy == "network"
    assert cfg.search_diagnostics is True
    assert cfg.search_api_key == "sk-new"


def test_configure_search_explicit_flags_still_override_stored(tmp_path, monkeypatch):
    target = tmp_path / "c.toml"
    target.write_text(
        'search_provider = "duckduckgo"\n'
        "search_max_results = 9\n"
        'search_proxy = "http://127.0.0.1:7890"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))

    result = runner.invoke(
        app,
        [
            "onboard",
            "configure",
            "search",
            "--search-provider",
            "duckduckgo",
            "--max-results",
            "3",
            "--proxy",
            "",
        ],
    )

    assert result.exit_code == 0, result.output
    cfg = load_config(target)
    assert cfg.search_max_results == 3
    assert cfg.search_proxy == ""


# ---------------------------------------------------------------------------
# B2-5: a wizard cancellation (Esc/Ctrl+C) must exit with one short line and
# code 130 instead of a raw traceback.
# ---------------------------------------------------------------------------


def test_onboard_wizard_cancellation_is_productized(tmp_path, monkeypatch):
    from opensquilla.cli import onboard_cmd
    from opensquilla.onboarding.errors import UserCancelledError

    target = tmp_path / "c.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))

    def cancelled(_options):
        raise UserCancelledError(section="provider")

    monkeypatch.setattr(onboard_cmd, "run_interactive_onboard", cancelled)

    result = runner.invoke(app, ["onboard"])

    assert result.exit_code == 130
    assert "Setup cancelled" in _plain(result.stderr)
    assert "Traceback" not in result.output + result.stderr
    assert not target.exists()


def test_configure_wizard_cancellation_is_productized(tmp_path, monkeypatch):
    from opensquilla.cli import onboard_cmd
    from opensquilla.onboarding.errors import UserCancelledError

    target = tmp_path / "c.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))

    def cancelled(_section, *, config_path=None):
        raise UserCancelledError(section="configure")

    monkeypatch.setattr(onboard_cmd, "run_interactive_configure", cancelled)

    result = runner.invoke(app, ["onboard", "configure", "router"])

    assert result.exit_code == 130
    assert "Setup cancelled" in _plain(result.stderr)
    assert "Traceback" not in result.output + result.stderr
    assert not target.exists()


# ---------------------------------------------------------------------------
# B2-6: bare `onboard` with a corrupt config must route through the
# productized config-error handoff (exit 2), never a raw traceback.
# ---------------------------------------------------------------------------


def test_bare_onboard_toml_decode_error_is_productized(tmp_path, monkeypatch):
    target = tmp_path / "c.toml"
    target.write_text("not toml :::", encoding="utf-8")
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))

    result = runner.invoke(app, ["onboard"])

    assert result.exit_code == 2
    assert "OpenSquilla config error" in result.stderr
    assert "Traceback" not in result.output + result.stderr


def test_bare_onboard_validation_error_is_productized(tmp_path, monkeypatch):
    target = tmp_path / "c.toml"
    target.write_text("[search]\n", encoding="utf-8")
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))

    result = runner.invoke(app, ["onboard"])

    assert result.exit_code == 2
    assert "OpenSquilla config error" in result.stderr
    assert "pydantic_core" not in result.stderr
    assert "Traceback" not in result.output + result.stderr


def test_bare_onboard_os_error_is_productized(tmp_path, monkeypatch):
    target = tmp_path / "config-dir.toml"
    target.mkdir()  # opening a directory raises IsADirectoryError (OSError)
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))

    result = runner.invoke(app, ["onboard"])

    assert result.exit_code == 2
    assert "OpenSquilla config error" in result.stderr
    assert "Traceback" not in result.output + result.stderr


def test_onboard_provider_with_corrupt_config_is_productized(tmp_path, monkeypatch):
    target = tmp_path / "c.toml"
    target.write_text("not toml :::", encoding="utf-8")
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))

    result = runner.invoke(
        app,
        ["onboard", "--provider", "openrouter", "--api-key", "sk", "--minimal"],
    )

    assert result.exit_code == 2
    assert "OpenSquilla config error" in result.stderr
    assert "Traceback" not in result.output + result.stderr


# ---------------------------------------------------------------------------
# B2-7: a ValidationError on the `onboard --provider` path must not echo
# pydantic's input_value (which can carry a mispasted secret).
# ---------------------------------------------------------------------------


def test_onboard_provider_validation_error_never_echoes_input_value(
    tmp_path, monkeypatch
):
    from pydantic import BaseModel, ValidationError

    from opensquilla.cli import onboard_cmd

    class _Probe(BaseModel):
        max_tokens: int

    try:
        _Probe(max_tokens="sk-super-secret-value")  # type: ignore[arg-type]
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
        ["onboard", "--provider", "openrouter", "--api-key", "sk", "--minimal"],
    )

    assert result.exit_code == 2
    combined = result.output + result.stderr
    assert "sk-super-secret-value" not in combined
    assert "input_value" not in combined
    assert "max_tokens" in _plain(result.stderr)
    assert "Traceback" not in combined


# ---------------------------------------------------------------------------
# B2-8: headless channels/memory saves must surface PersistResult's
# restart_required instead of printing only the saved path.
# ---------------------------------------------------------------------------


def test_configure_channels_prints_restart_guidance(tmp_path, monkeypatch):
    target = tmp_path / "c.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))

    result = runner.invoke(
        app,
        [
            "onboard",
            "configure",
            "channels",
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

    assert result.exit_code == 0, result.output
    plain = _plain(result.stdout)
    assert "restart required" in plain
    assert "opensquilla gateway restart" in plain


def test_configure_memory_prints_restart_guidance_with_config_path(
    tmp_path, monkeypatch
):
    default_target = tmp_path / "default.toml"
    target = tmp_path / "custom.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(default_target))

    result = runner.invoke(
        app,
        [
            "onboard",
            "configure",
            "memory",
            "--memory-provider",
            "local",
            "--onnx-dir",
            "models/bge",
            "--config",
            str(target),
        ],
    )

    assert result.exit_code == 0, result.output
    plain = _plain(result.stdout)
    assert "restart required" in plain
    assert "opensquilla gateway restart" in plain
    assert str(target) in plain
    assert not default_target.exists()


def test_configure_search_does_not_print_restart_guidance(tmp_path, monkeypatch):
    target = tmp_path / "c.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))

    result = runner.invoke(
        app,
        ["onboard", "configure", "search", "--search-provider", "duckduckgo"],
    )

    assert result.exit_code == 0, result.output
    assert "restart required" not in _plain(result.stdout)


# ---------------------------------------------------------------------------
# B2-10: strict --field coercion (bool typos and numeric garbage must name
# the offending field and the accepted spellings).
# ---------------------------------------------------------------------------


def test_configure_channels_field_bool_typo_exits_2_naming_field(
    tmp_path, monkeypatch
):
    target = tmp_path / "c.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))

    result = runner.invoke(
        app,
        [
            "onboard",
            "configure",
            "channels",
            "--channel-type",
            "slack",
            "--name",
            "work",
            "--token",
            "xoxb-secret",
            "--field",
            "signing_secret=ss",
            "--field",
            "enabled=ture",
        ],
    )

    assert result.exit_code == 2
    plain = _plain(result.output + result.stderr)
    assert "enabled" in plain
    assert "true/false" in plain
    assert "'ture'" in plain
    assert not target.exists()


def test_configure_channels_field_int_garbage_exits_2_naming_field(
    tmp_path, monkeypatch
):
    target = tmp_path / "c.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))

    result = runner.invoke(
        app,
        [
            "onboard",
            "configure",
            "channels",
            "--channel-type",
            "discord",
            "--name",
            "guild",
            "--token",
            "bot-secret",
            "--field",
            "intents=lots",
        ],
    )

    assert result.exit_code == 2
    plain = _plain(result.output + result.stderr)
    assert "intents" in plain
    assert "integer" in plain
    assert not target.exists()


def test_parse_channel_field_pairs_strict_coercion_unit():
    import typer

    from opensquilla.cli.channel_fields import parse_channel_field_pairs

    assert parse_channel_field_pairs(["enabled=TRUE"], "slack")["enabled"] is True
    assert parse_channel_field_pairs(["enabled=off"], "slack")["enabled"] is False

    with pytest.raises(typer.BadParameter, match=r"--field enabled .*'ture'"):
        parse_channel_field_pairs(["enabled=ture"], "slack")
    with pytest.raises(typer.BadParameter, match=r"--field intents .*integer"):
        parse_channel_field_pairs(["intents=lots"], "discord")
    with pytest.raises(typer.BadParameter, match=r"--field poll_idle_sleep_s .*number"):
        parse_channel_field_pairs(["poll_idle_sleep_s=fast"], "telegram")


# ---------------------------------------------------------------------------
# B2-11: previously untested branches.
# ---------------------------------------------------------------------------


def test_onboard_probe_flag_exits_nonzero_when_probe_raises(tmp_path, monkeypatch):
    from opensquilla.onboarding import probe as probe_module

    target = tmp_path / "c.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))

    async def exploding_probe(**_kwargs):
        raise RuntimeError("socket exploded mid-flight")

    monkeypatch.setattr(probe_module, "probe_llm_provider", exploding_probe)

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
            "--probe",
        ],
    )

    assert result.exit_code == 1
    assert "Probe failed" in result.stderr
    assert "socket exploded mid-flight" in result.stderr
    assert "Traceback" not in result.output + result.stderr
    # The probe is a gate, not a rollback: the save sticks.
    assert "openrouter" in target.read_text()


def test_onboard_catalog_unknown_section_exits_2(tmp_path, monkeypatch):
    target = tmp_path / "c.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))

    result = runner.invoke(app, ["onboard", "catalog", "not-a-section"])

    assert result.exit_code == 2
    assert "unknown setup section" in _plain(result.stderr)
    assert "Traceback" not in result.output + result.stderr


def test_onboard_if_needed_names_unfinished_sections(tmp_path, monkeypatch):
    # has_config=True but the referenced env key is missing: the gate must
    # explain which sections are unfinished before falling into the wizard.
    target = tmp_path / "c.toml"
    target.write_text(
        "[llm]\n"
        'provider = "openrouter"\n'
        'model = "deepseek/deepseek-v4-flash"\n'
        'api_key_env = "CUSTOM_LLM_KEY"\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("CUSTOM_LLM_KEY", raising=False)
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))

    result = runner.invoke(app, ["onboard", "--if-needed"])

    assert result.exit_code == 2  # non-TTY still exits 2 afterwards
    assert "onboarding has unfinished sections" in result.stdout
    assert "Provider" in result.stdout


def test_bare_configure_hub_exit_without_changes_is_success(tmp_path, monkeypatch):
    import sys
    import types

    target = tmp_path / "c.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))

    from opensquilla.onboarding import flow

    monkeypatch.setattr(flow, "_is_tty", lambda: True)

    class _Answer:
        def __init__(self, value):
            self.value = value

        def ask(self):
            return self.value

    class _Questionary(types.SimpleNamespace):
        def select(self, message: str, **kwargs):
            assert message == "Section"
            assert kwargs["choices"][-1] == "Exit (nothing changed)"
            return _Answer("Exit (nothing changed)")

        def text(self, message: str, **_kwargs):
            raise AssertionError(f"unexpected text prompt: {message}")

        def confirm(self, message: str, **_kwargs):
            raise AssertionError(f"unexpected confirm prompt: {message}")

        def password(self, message: str, **_kwargs):
            raise AssertionError(f"unexpected password prompt: {message}")

        def checkbox(self, message: str, **_kwargs):
            raise AssertionError(f"unexpected checkbox prompt: {message}")

    monkeypatch.setitem(sys.modules, "questionary", _Questionary())

    result = runner.invoke(app, ["onboard", "configure"])

    assert result.exit_code == 0, result.output
    assert "requires a TTY" not in result.stdout
    assert not target.exists()


# ---------------------------------------------------------------------------
# B2-9: router tier ids in the CLI stay sourced from the router_tiers
# helpers instead of raw tuples/literals.
# ---------------------------------------------------------------------------


def test_router_catalog_tracks_router_tier_helpers(tmp_path, monkeypatch):
    from opensquilla.cli import onboard_cmd
    from opensquilla.router_tiers import DEFAULT_TEXT_TIER, TEXT_TIERS

    assert (
        f"--default-tier {DEFAULT_TEXT_TIER}"
        in onboard_cmd._CATALOG_COMMANDS["routerProfiles"]
    )

    target = tmp_path / "c.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))
    result = runner.invoke(app, ["onboard", "catalog", "router"])

    assert result.exit_code == 0, result.stdout
    for tier in TEXT_TIERS:
        assert f"{tier}:" in result.stdout
