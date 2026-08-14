"""Unit tests for opensquilla.contrib.codetask.agent_config (pure assembly)."""

import tomllib

import pytest

from opensquilla.contrib.codetask import agent_config as agent_config_mod
from opensquilla.contrib.codetask.agent_config import (
    OPERATOR_SECTIONS,
    AgentConfigError,
    build_per_run_agent_config,
    load_agent_config_bundle,
    user_config_payload,
)

_TEMPLATE = {
    "workspace_strict": False,
    "sandbox": {"sandbox": False, "security_grading": False},
    "tools": {"deny": ["memory*"]},
    "meta_skill": {"enabled": False},
    "memory": {"flush_enabled": False},
}


def test_operator_sections_replace_template_wholesale():
    template = dict(_TEMPLATE, llm={"provider": "openrouter", "max_tokens": 32768})
    user = {
        "llm": {"provider": "deepseek", "model": "deepseek-chat"},
        "models": {"deepseek": {"deepseek-chat": {"context_window": 128000}}},
        "privacy": {"disable_network_observability": True},
    }
    bundle = build_per_run_agent_config(template, user)
    # Replaced, not field-merged: the template's max_tokens must not leak
    # under the operator's provider.
    assert bundle.payload["llm"] == {"provider": "deepseek", "model": "deepseek-chat"}
    assert bundle.payload["models"]["deepseek"]["deepseek-chat"]["context_window"] == 128000
    assert bundle.payload["privacy"]["disable_network_observability"] is True
    # Template policy survives untouched.
    assert bundle.payload["tools"]["deny"] == ["memory*"]
    assert bundle.payload["meta_skill"]["enabled"] is False


def test_operator_absent_sections_are_dropped_from_template():
    template = dict(_TEMPLATE, llm={"provider": "openrouter"}, squilla_router={"enabled": True})
    bundle = build_per_run_agent_config(template, {})
    for section in OPERATOR_SECTIONS:
        assert section not in bundle.payload, section


def test_primary_api_key_moves_to_child_env():
    user = {"llm": {"provider": "deepseek", "model": "deepseek-chat", "api_key": "sk-secret"}}
    bundle = build_per_run_agent_config(dict(_TEMPLATE), user)
    assert "api_key" not in bundle.payload["llm"]
    assert bundle.child_env == {"OPENSQUILLA_LLM_API_KEY": "sk-secret"}
    # The source payload is not mutated.
    assert user["llm"]["api_key"] == "sk-secret"


def test_empty_api_key_is_not_stripped():
    # An explicitly empty api_key must stay in the written [llm] (the operator
    # resolves via api_key_env); dropping it would let a stale inherited
    # OPENSQUILLA_LLM_API_KEY fill the field in the child.
    user = {"llm": {"provider": "deepseek", "model": "m", "api_key": "", "api_key_env": "DS_KEY"}}
    bundle = build_per_run_agent_config(dict(_TEMPLATE), user)
    assert bundle.payload["llm"]["api_key"] == ""
    assert bundle.child_env == {}


def test_profile_api_keys_stay_in_payload():
    # Profile keys have no env transport channel; they ride in the 0600 file.
    user = {
        "llm": {"provider": "deepseek", "model": "deepseek-chat"},
        "llm_profiles": {"moonshot": {"api_key": "sk-profile"}},
    }
    bundle = build_per_run_agent_config(dict(_TEMPLATE), user)
    assert bundle.payload["llm_profiles"]["moonshot"]["api_key"] == "sk-profile"
    assert bundle.child_env == {}


def test_llm_ensemble_is_never_carried():
    user = {
        "llm": {"provider": "deepseek", "model": "deepseek-chat"},
        "llm_ensemble": {"enabled": True},
    }
    bundle = build_per_run_agent_config(dict(_TEMPLATE), user)
    assert "llm_ensemble" not in bundle.payload


def test_mismatched_tier_profile_heals_in_merged_config():
    # A tier_profile that no longer matches llm.provider used to hard-fail
    # validation; the config-migration layer now clears it (the same healing
    # gateway boot applies to hand-edited configs), so the merged per-run
    # config builds cleanly with the profile pointer dropped instead of
    # failing at subagent boot.
    user = {
        "llm": {"provider": "deepseek", "model": "deepseek-chat"},
        "squilla_router": {"enabled": True, "tier_profile": "moonshot"},
    }
    bundle = build_per_run_agent_config(
        dict(_TEMPLATE), user, user_config_path="/x/config.toml"
    )
    assert "tier_profile" not in bundle.payload.get("squilla_router", {})


def test_user_config_payload_explicit_env_path(monkeypatch, tmp_path):
    cfg = tmp_path / "operator.toml"
    cfg.write_text('[llm]\nprovider = "deepseek"\nmodel = "m"\n', encoding="utf-8")
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(cfg))
    payload, source = user_config_payload()
    assert payload["llm"]["provider"] == "deepseek"
    assert source == str(cfg)


def test_user_config_payload_explicit_env_path_missing_is_sole_candidate(
    monkeypatch, tmp_path
):
    # Mirrors GatewayConfig.load: an explicit path never falls back to the
    # cwd/home chain.
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "config.toml").write_text('[llm]\nprovider = "moonshot"\n', "utf-8")
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(tmp_path / "missing.toml"))
    payload, source = user_config_payload()
    assert payload == {}
    assert source is None


def test_user_config_payload_falls_back_to_home_config(monkeypatch, tmp_path):
    monkeypatch.delenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", raising=False)
    monkeypatch.chdir(tmp_path)  # no ./opensquilla.toml here
    home = tmp_path / "state"
    home.mkdir()
    (home / "config.toml").write_text('[llm]\nprovider = "deepseek"\nmodel = "m"\n', "utf-8")
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(home))
    payload, source = user_config_payload()
    assert payload["llm"]["provider"] == "deepseek"
    assert source == str(home / "config.toml")


def test_user_config_payload_cwd_toml_wins_over_home(monkeypatch, tmp_path):
    monkeypatch.delenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "opensquilla.toml").write_text(
        '[llm]\nprovider = "deepseek"\nmodel = "m"\n', "utf-8"
    )
    home = tmp_path / "state"
    home.mkdir()
    (home / "config.toml").write_text('[llm]\nprovider = "moonshot"\nmodel = "k"\n', "utf-8")
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(home))
    payload, _source = user_config_payload()
    assert payload["llm"]["provider"] == "deepseek"


def test_unparseable_operator_config_raises_actionably(monkeypatch, tmp_path):
    cfg = tmp_path / "broken.toml"
    cfg.write_text("[llm\nprovider=", encoding="utf-8")
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(cfg))
    with pytest.raises(AgentConfigError) as exc:
        user_config_payload()
    assert str(cfg) in str(exc.value)


def test_override_preserves_provider_authority_and_inherits_operator_privacy(
    monkeypatch,
    tmp_path,
):
    override = tmp_path / "override.toml"
    override.write_text(
        '[llm]\nprovider = "deepseek"\nmodel = "deepseek-chat"\napi_key = "sk-keep"\n'
        "[privacy]\ndisable_network_observability = false\n",
        encoding="utf-8",
    )
    operator = tmp_path / "operator.toml"
    operator.write_text(
        '[llm]\nprovider = "moonshot"\nmodel = "kimi-k2.6"\n'
        "[privacy]\ndisable_network_observability = true\n",
        "utf-8",
    )
    monkeypatch.setenv("OPENSQUILLA_CODETASK_AGENT_CONFIG", str(override))
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(operator))
    bundle = load_agent_config_bundle()
    assert bundle.payload["llm"]["provider"] == "deepseek"
    assert bundle.payload["llm"]["api_key"] == "sk-keep"  # full authority, untouched
    assert bundle.payload["privacy"]["disable_network_observability"] is True
    assert bundle.child_env == {}


def test_override_survives_broken_operator_config_with_privacy_disabled(
    monkeypatch,
    tmp_path,
):
    override = tmp_path / "override.toml"
    override.write_text(
        '[llm]\nprovider = "deepseek"\nmodel = "deepseek-chat"\n',
        encoding="utf-8",
    )
    operator = tmp_path / "broken.toml"
    operator.write_text("[privacy\ndisable_network_observability =", encoding="utf-8")
    monkeypatch.setenv("OPENSQUILLA_CODETASK_AGENT_CONFIG", str(override))
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(operator))

    bundle = load_agent_config_bundle()

    assert bundle.payload["llm"]["provider"] == "deepseek"
    assert bundle.payload["privacy"]["disable_network_observability"] is True


def test_override_fails_privacy_closed_when_operator_value_is_invalid(
    monkeypatch,
    tmp_path,
):
    override = tmp_path / "override.toml"
    override.write_text(
        '[llm]\nprovider = "deepseek"\nmodel = "deepseek-chat"\n',
        encoding="utf-8",
    )
    operator = tmp_path / "operator.toml"
    operator.write_text(
        '[privacy]\ndisable_network_observability = "not-a-boolean"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENSQUILLA_CODETASK_AGENT_CONFIG", str(override))
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(operator))

    bundle = load_agent_config_bundle()

    assert bundle.payload["privacy"]["disable_network_observability"] is True


def test_blank_override_env_is_treated_as_unset(monkeypatch, tmp_path):
    # A quoted-empty OPENSQUILLA_CODETASK_AGENT_CONFIG must not be taken as a
    # path (which would hard-fail every run reading a whitespace filename);
    # inheritance proceeds normally.
    operator = tmp_path / "operator.toml"
    operator.write_text('[llm]\nprovider = "deepseek"\nmodel = "m"\n', encoding="utf-8")
    monkeypatch.setenv("OPENSQUILLA_CODETASK_AGENT_CONFIG", "   ")
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(operator))
    bundle = load_agent_config_bundle()
    assert bundle.payload["llm"]["provider"] == "deepseek"


def test_bundled_template_pins_no_provider_sections():
    # The shipped template is a run-policy file; pinning [llm]/[squilla_router]
    # there would shadow the operator's provider (issue #541 regression guard).
    from opensquilla.contrib.codetask.config import _DATA_DIR

    template = tomllib.loads(
        (_DATA_DIR / "agent_config" / "config.toml").read_text(encoding="utf-8")
    )
    for section in OPERATOR_SECTIONS:
        assert section not in template, section
    for section in ("tools", "sandbox", "meta_skill", "memory"):
        assert section in template, section


def test_merged_payload_round_trips_through_gateway_config(monkeypatch, tmp_path):
    """End to end: the assembled payload for a deepseek operator loads as a
    deepseek config with an auto-aligned router tier profile."""
    operator = tmp_path / "operator.toml"
    operator.write_text(
        '[llm]\nprovider = "deepseek"\nmodel = "deepseek-chat"\n', encoding="utf-8"
    )
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(operator))
    monkeypatch.delenv("OPENSQUILLA_CODETASK_AGENT_CONFIG", raising=False)
    bundle = load_agent_config_bundle()

    from opensquilla.gateway.config import GatewayConfig

    conf = GatewayConfig(**bundle.payload)
    assert conf.llm.provider == "deepseek"
    assert conf.llm.model == "deepseek-chat"
    assert conf.squilla_router.tier_profile in ("deepseek", None)


def test_agent_config_path_still_honors_override(monkeypatch, tmp_path):
    # Regression guard for the existing escape-hatch contract.
    custom = tmp_path / "custom.toml"
    monkeypatch.setenv("OPENSQUILLA_CODETASK_AGENT_CONFIG", str(custom))
    assert agent_config_mod.agent_config_path() == custom
