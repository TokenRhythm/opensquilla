"""Wire-contract freeze for the ``config.get`` public config view.

``config.get`` returns ``GatewayConfig.to_public_dict()`` verbatim, and both
the Web UI settings screens and ``opensquilla config`` script against its
key names, so they are a public protocol contract (see CLAUDE.md: config
keys and public RPC field names are stable).

Unlike the onboarding payloads, the top level here mirrors the whole config
model and legitimately grows a new section with almost every feature, so
these tests assert *presence* of today's names (superset-friendly) rather
than an exact set: purely additive changes stay green, while renaming or
removing any key listed below is a contract break and must fail.

The config is default-constructed against a tmp state dir with provider env
keys already stripped by tests/conftest.py — no file IO, no network, and
only a synthetic API key is used to prove redaction.
"""

from __future__ import annotations

import pytest

from opensquilla.gateway.config import GatewayConfig, LlmProviderConfig

# Top-level sections/scalars shipped in the public config view today.
# Presence-only on purpose (see module docstring): additions are free,
# renames/removals fail.
PUBLIC_TOP_LEVEL_KEYS = frozenset(
    {
        "host",
        "port",
        "version",
        "debug",
        "tls",
        "auth",
        "cors",
        "attachments",
        "rate_limit",
        "tools",
        "permissions",
        "task_runtime",
        "skills",
        "llm",
        "llm_profiles",
        "llm_ensemble",
        # Deliberate 0.5.x additions: model-catalog behavior and per-model
        # metadata overrides landed as additive top-level sections (schema
        # first; provider wiring follows). Once listed here they are frozen
        # like every other name.
        "model_catalog",
        "models",
        "prompt_cache",
        "safety",
        "prompt",
        "memory",
        "squilla_router",
        "agent_token_saving",
        "compaction",
        "naming",
        "mcp",
        "heartbeat",
        "image_generation",
        "audio",
        "sandbox",
        "channels",
        "agents",
        "agents_defaults",
        "subagents",
        "meta_skill",
        "control_ui",
        "privacy",
    }
)

# Keys operators and the Web UI toggle inside these sections. selection_mode
# is deliberately pinned: it is the new ensemble routing switch and must not
# be renamed once shipped.
LLM_ENSEMBLE_REQUIRED_KEYS = frozenset({"enabled", "selection_mode"})
SQUILLA_ROUTER_REQUIRED_KEYS = frozenset(
    {"enabled", "tier_profile", "tiers", "default_tier", "visual_mode"}
)

# The literal marker clients receive instead of a secret value. Asserted as a
# literal (not imported) because the *string on the wire* is the contract.
REDACTION_MARKER = "[redacted]"


@pytest.fixture()
def public_config(tmp_path, monkeypatch: pytest.MonkeyPatch) -> GatewayConfig:
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path / "state"))
    return GatewayConfig(config_path=str(tmp_path / "opensquilla.toml"))


def test_public_dict_top_level_sections_are_present(public_config: GatewayConfig) -> None:
    data = public_config.to_public_dict()
    missing = PUBLIC_TOP_LEVEL_KEYS - set(data)
    assert not missing, missing


def test_llm_ensemble_public_keys_are_frozen(public_config: GatewayConfig) -> None:
    ensemble = public_config.to_public_dict()["llm_ensemble"]
    missing = LLM_ENSEMBLE_REQUIRED_KEYS - set(ensemble)
    assert not missing, missing


def test_squilla_router_public_keys_are_frozen(public_config: GatewayConfig) -> None:
    router = public_config.to_public_dict()["squilla_router"]
    missing = SQUILLA_ROUTER_REQUIRED_KEYS - set(router)
    assert not missing, missing
    # The default tier is canonical c1 (router_tiers.py); clients display it raw.
    assert router["default_tier"] == "c1"


def test_secret_keys_are_redacted_not_leaked(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path / "state"))
    secret = "sk-test-000"  # synthetic; never a real credential
    cfg = GatewayConfig(
        config_path=str(tmp_path / "opensquilla.toml"),
        llm=LlmProviderConfig(api_key=secret),
    )

    data = cfg.to_public_dict()

    # The key must survive (clients render "configured" state from it) while
    # the value is replaced with the literal marker.
    assert data["llm"]["api_key"] == REDACTION_MARKER
    # Nothing anywhere in the public view may carry the raw secret.
    assert secret not in repr(data)


def test_privacy_carries_the_effective_network_observability_flag(
    public_config: GatewayConfig,
) -> None:
    # to_public_dict injects the unified config/dedicated-env answer. Legacy
    # telemetry/update env vars must not make the UI imply correlation is off.
    privacy = public_config.to_public_dict()["privacy"]
    assert "network_observability_disabled_effective" in privacy
