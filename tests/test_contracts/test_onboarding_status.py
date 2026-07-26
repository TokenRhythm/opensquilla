"""Wire-contract freeze for the ``onboarding.status`` RPC payload.

The status payload drives the Web UI onboarding checklist and the CLI
``onboard status`` renderers, so its camelCase key names are a public
protocol contract (see CLAUDE.md: public RPC field names are stable).

- Renaming or removing any frozen key is a contract break and must fail here.
- Adding a key requires deliberately extending the frozen set in this file —
  that friction is the point: wire additions should be a conscious decision.

The payload is built by ``rpc_onboarding._status_payload`` against a fully
synthetic ``GatewayConfig`` handed to the RPC context, so no config file, no
network, and no credentials are involved (tests/conftest.py already strips
provider keys from the environment).
"""

from __future__ import annotations

import pytest

from opensquilla.gateway.config import GatewayConfig, LlmProviderConfig
from opensquilla.gateway.rpc import RpcContext
from opensquilla.gateway.rpc_onboarding import _status_payload

# Exact top-level shape of onboarding.status as shipped today.
STATUS_TOP_LEVEL_KEYS = frozenset(
    {
        "configPath",
        "hasConfig",
        "llmConfigured",
        "llmSource",
        "llmEnvKey",
        "llmCredentialStatus",
        # Additive, secret-free deployment readiness for Router/Ensemble
        # provider pickers. Older gateways omit it and clients must tolerate
        # that absence; new gateways freeze the entry shape below.
        "llmProfileStatus",
        "imageGenerationConfigured",
        "imageGenerationEnabled",
        "imageGenerationSource",
        "imageGenerationProvider",
        "imageGenerationPrimary",
        "imageGenerationEnvKey",
        "audioConfigured",
        "audioEnabled",
        "audioSource",
        "audioProvider",
        "audioEnvKey",
        "searchConfigured",
        "searchProvider",
        "searchSource",
        "searchEnvKey",
        "memoryEmbeddingConfigured",
        "memoryEmbeddingProvider",
        "memoryEmbeddingSource",
        "memoryEmbeddingEnvKey",
        "channelCount",
        "channelsConfigured",
        "ensembleCredentialStatus",
        "needsOnboarding",
        "sections",
        "sectionDetails",
        "envRecoveryCommands",
        "warnings",
        # Frozen compatibility key. Migration discovery is settings-only, so
        # this value remains null through the current major version.
        "legacyData",
    }
)

# Section names double as wire keys inside ``sections`` / ``sectionDetails``.
# ``ensemble`` is a deliberate additive extension of this frozen set: the
# ``[llm_ensemble]`` routing surface gained CLI onboarding parity
# (``opensquilla onboard configure ensemble``), and the CLI status table
# renders straight from this payload, so the section rides the same frozen
# contract. Its verifier only ever reports ``ok`` (enabled) or ``optional``
# (disabled) — it can never block onboarding or add action-required noise
# for existing clients. Extending this set here is the conscious decision
# the freeze forces.
SECTION_NAMES = frozenset(
    {
        "llm",
        "router",
        "ensemble",
        "search",
        "channels",
        "image_generation",
        "audio",
        "memory_embedding",
    }
)

# Per-section detail card shape. ``detail`` is the only optional key (a
# human-readable annotation); everything else must always be present.
SECTION_DETAIL_REQUIRED_KEYS = frozenset(
    {"label", "status", "required", "optional", "blocking", "actionRequired"}
)

# Additive per-section keys, allowed only on the named section. ``routerMode``
# is a deliberate router-card addition: a server-computed
# ``recommended|openrouter-mix|custom|disabled`` value so clients stop
# inferring the mode from (provider, tier_profile) pairs. Adding to this map is
# the conscious decision the friction forces.
SECTION_EXTRA_KEYS = {
    "router": frozenset(
        {"routerMode", "routerBinding", "routerProviderConflicts"}
    )
}

# Every mode value the router card may carry; matched verbatim by clients.
ROUTER_MODE_VALUES = frozenset({"recommended", "openrouter-mix", "custom", "disabled"})
ROUTER_BINDING_VALUES = frozenset({"follow_primary", "custom", "legacy"})

# Shape of one env-recovery command row shown when a configured env key is
# not visible in the running shell.
ENV_RECOVERY_COMMAND_KEYS = frozenset({"section", "label", "command"})
LLM_PROFILE_STATUS_KEYS = frozenset(
    {
        "provider",
        "ready",
        "credentialSource",
        "credentialEnv",
        "endpointSource",
        "proxySource",
        "reason",
        "primaryEligible",
        "primaryBlockReason",
    }
)


def _synthetic_config(tmp_path, **overrides) -> GatewayConfig:
    # config_path points at a nonexistent tmp file so the status builder never
    # reads the developer's real ~/.opensquilla config.
    return GatewayConfig(config_path=str(tmp_path / "opensquilla.toml"), **overrides)


async def test_onboarding_status_top_level_keys_are_frozen(tmp_path) -> None:
    cfg = _synthetic_config(tmp_path)
    payload = _status_payload(RpcContext(conn_id="contract", config=cfg))
    assert set(payload) == STATUS_TOP_LEVEL_KEYS
    # configPath must round-trip the running config's path so clients can tell
    # operators which file to edit.
    assert payload["configPath"] == cfg.config_path
    assert payload["llmProfileStatus"]
    assert all(set(row) == LLM_PROFILE_STATUS_KEYS for row in payload["llmProfileStatus"])


async def test_llm_profile_status_reflects_exhausted_global_credential_pool(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.gateway.llm_runtime import (
        NoCredentialsAvailable,
        reset_profile_credential_pools,
    )
    from opensquilla.provider.failures import ProviderFailureKind

    env_name = "OPENSQUILLA_TEST_STATUS_EXHAUSTED_POOL"
    secret = "synthetic-status-exhausted-secret"
    monkeypatch.setenv(env_name, secret)
    cfg = _synthetic_config(
        tmp_path,
        llm_profiles={"openai": {"api_key_env_pool": [env_name]}},
    )
    pools = reset_profile_credential_pools()
    try:
        assert pools.acquire_for_session("openai", [env_name], "failed-turn") is not None
        pools.report_failure("openai", "failed-turn", ProviderFailureKind.AUTH_INVALID)

        payload = _status_payload(RpcContext(conn_id="contract", config=cfg))
        profile = next(
            row for row in payload["llmProfileStatus"] if row["provider"] == "openai"
        )

        assert profile["ready"] is False
        assert profile["reason"] == "credential_pool_exhausted"
        assert secret not in repr(payload)
        # Status inspection must not rebuild the pool or clear its parked state.
        with pytest.raises(NoCredentialsAvailable):
            pools.acquire_for_session("openai", [env_name], "after-status")
    finally:
        reset_profile_credential_pools()


async def test_llm_profile_status_does_not_advance_pool_rotation(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.gateway.llm_runtime import reset_profile_credential_pools

    env_a = "OPENSQUILLA_TEST_STATUS_POOL_A"
    env_b = "OPENSQUILLA_TEST_STATUS_POOL_B"
    secret_a = "synthetic-status-pool-secret-a"
    secret_b = "synthetic-status-pool-secret-b"
    monkeypatch.setenv(env_a, secret_a)
    monkeypatch.setenv(env_b, secret_b)
    cfg = _synthetic_config(
        tmp_path,
        llm_profiles={"openai": {"api_key_env_pool": [env_a, env_b]}},
    )
    pools = reset_profile_credential_pools()
    try:
        first = pools.acquire_for_session("openai", [env_a, env_b], "before-status")
        assert first is not None and first.env_name == env_a

        payload = _status_payload(RpcContext(conn_id="contract", config=cfg))
        profile = next(
            row for row in payload["llmProfileStatus"] if row["provider"] == "openai"
        )
        assert profile["ready"] is True
        assert profile["credentialSource"] == "profile_pool"
        assert secret_a not in repr(payload)
        assert secret_b not in repr(payload)

        # With a read-only status lookup, the next real acquisition remains B.
        second = pools.acquire_for_session("openai", [env_a, env_b], "after-status")
        assert second is not None and second.env_name == env_b
    finally:
        reset_profile_credential_pools()


async def test_onboarding_status_section_keys_are_frozen(tmp_path) -> None:
    payload = _status_payload(RpcContext(conn_id="contract", config=_synthetic_config(tmp_path)))

    assert set(payload["sections"]) == SECTION_NAMES
    assert set(payload["sectionDetails"]) == SECTION_NAMES

    for name, detail in payload["sectionDetails"].items():
        missing = SECTION_DETAIL_REQUIRED_KEYS - set(detail)
        assert not missing, (name, missing)
        allowed_extra = {"detail"} | SECTION_EXTRA_KEYS.get(name, frozenset())
        extra = set(detail) - SECTION_DETAIL_REQUIRED_KEYS - allowed_extra
        assert not extra, (name, extra)


async def test_router_section_carries_an_explicit_router_mode(tmp_path) -> None:
    # routerMode must always be present on the router card and only ever be
    # one of the four frozen mode strings.
    payload = _status_payload(RpcContext(conn_id="contract", config=_synthetic_config(tmp_path)))
    assert payload["sectionDetails"]["router"]["routerMode"] in ROUTER_MODE_VALUES
    assert payload["sectionDetails"]["router"]["routerBinding"] == "legacy"
    assert payload["sectionDetails"]["router"]["routerBinding"] in ROUTER_BINDING_VALUES
    assert isinstance(
        payload["sectionDetails"]["router"]["routerProviderConflicts"], list
    )


async def test_sparse_disabled_router_follows_primary_without_claiming_explicit_tiers(
    tmp_path,
) -> None:
    def binding_for(cfg: GatewayConfig) -> str:
        payload = _status_payload(RpcContext(conn_id="contract", config=cfg))
        return str(payload["sectionDetails"]["router"]["routerBinding"])

    sparse = _synthetic_config(
        tmp_path,
        llm=LlmProviderConfig(provider="deepseek", model="deepseek-chat"),
        squilla_router={"enabled": False},
    )
    assert "tiers" not in sparse.squilla_router.model_fields_set
    assert binding_for(sparse) == "follow_primary"

    historical = _synthetic_config(
        tmp_path,
        llm=LlmProviderConfig(provider="deepseek", model="deepseek-chat"),
        squilla_router={
            "enabled": False,
            "tiers": {
                "c0": {"provider": "openrouter", "model": "legacy-model"},
            },
        },
    )
    assert "tiers" in historical.squilla_router.model_fields_set
    assert binding_for(historical) == "legacy"


async def test_router_mode_computation_is_frozen(tmp_path) -> None:
    """Pin the (enabled, provider, tier_profile) → routerMode mapping."""

    def mode_for(cfg: GatewayConfig) -> str:
        payload = _status_payload(RpcContext(conn_id="contract", config=cfg))
        return payload["sectionDetails"]["router"]["routerMode"]

    # Default config: tokenrhythm provider, router enabled, no tier_profile.
    assert mode_for(_synthetic_config(tmp_path)) == "custom"

    # Explicit openrouter with no tier_profile is the openrouter-mix alias.
    assert (
        mode_for(_synthetic_config(tmp_path, llm={"provider": "openrouter"}))
        == "openrouter-mix"
    )

    # Router off wins regardless of provider/profile.
    assert (
        mode_for(_synthetic_config(tmp_path, squilla_router={"enabled": False})) == "disabled"
    )

    # A persisted legacy tier_profile is the recommended shape.
    assert (
        mode_for(
            _synthetic_config(
                tmp_path,
                llm=LlmProviderConfig(provider="deepseek", model="deepseek-v4-flash"),
                squilla_router={"enabled": True, "tier_profile": "deepseek"},
            )
        )
        == "recommended"
    )

    # Explicit ownership supersedes legacy tier-profile shape inference.
    assert (
        mode_for(
            _synthetic_config(
                tmp_path,
                squilla_router={"preset_binding": "follow_primary"},
            )
        )
        == "recommended"
    )
    assert (
        mode_for(
            _synthetic_config(
                tmp_path,
                llm=LlmProviderConfig(provider="deepseek", model="deepseek-chat"),
                squilla_router={
                    "tier_profile": "deepseek",
                    "preset_binding": "custom",
                },
            )
        )
        == "custom"
    )

    # Enabled with no tier_profile on a non-openrouter provider is custom.
    # (groq is outside the legacy nine, so the boot auto-default never
    # assigns it a tier_profile.)
    assert (
        mode_for(
            _synthetic_config(
                tmp_path,
                llm=LlmProviderConfig(provider="groq", model="m"),
                squilla_router={"enabled": True},
            )
        )
        == "custom"
    )


async def test_env_recovery_command_rows_are_frozen(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A provider pointed at an env key that is not visible in this shell is
    # the one state that must produce a recovery command; freeze its row shape.
    env_key = "OPENSQUILLA_TEST_UNSET_KEY"
    monkeypatch.delenv(env_key, raising=False)
    cfg = _synthetic_config(tmp_path, llm=LlmProviderConfig(api_key_env=env_key))

    payload = _status_payload(RpcContext(conn_id="contract", config=cfg))

    assert payload["llmSource"] == "missing_env"
    assert payload["llmEnvKey"] == env_key
    commands = payload["envRecoveryCommands"]
    assert commands, "missing_env must surface a recovery command"
    for command in commands:
        assert set(command) == ENV_RECOVERY_COMMAND_KEYS


# Every value ``llmSource`` may carry over the wire. ``unsupported`` is a
# deliberate additive extension: registered-but-runtime-unsupported providers
# (e.g. coding-plan stubs) used to report ``not_required``, which read as a
# satisfied credential state for a provider nothing can run against. Client
# authors switching on llmSource must treat unknown values as
# not-configured; extending this set here is the conscious decision the
# freeze forces.
LLM_SOURCE_VALUES = frozenset(
    {"explicit", "env", "missing_env", "none", "not_required", "unsupported"}
)


async def test_llm_source_value_space_is_frozen(tmp_path) -> None:
    payload = _status_payload(
        RpcContext(conn_id="contract", config=_synthetic_config(tmp_path))
    )
    assert payload["llmSource"] in LLM_SOURCE_VALUES


async def test_unsupported_provider_source_is_consistent_across_the_payload(
    tmp_path,
) -> None:
    """llmSource and llmCredentialStatus.source must agree for a registered
    but runtime-unsupported provider: both say "unsupported" (never a
    satisfied "not_required") and the credential is not available."""
    cfg = _synthetic_config(
        tmp_path,
        llm=LlmProviderConfig(provider="github_copilot", model="stub-model"),
    )

    payload = _status_payload(RpcContext(conn_id="contract", config=cfg))

    assert payload["llmSource"] == "unsupported"
    credential = payload["llmCredentialStatus"]
    assert credential["source"] == "unsupported"
    assert credential["available"] is False
    assert payload["sectionDetails"]["llm"]["detail"] == (
        "registered but not runtime-supported"
    )


async def test_legacy_data_block_is_null_without_a_candidate(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Onboarding must not scan even when discovery would find a candidate.
    monkeypatch.setattr(
        "opensquilla.migration.legacy_detect.detect_legacy_home",
        lambda target=None: (_ for _ in ()).throw(AssertionError("unexpected scan")),
    )

    payload = _status_payload(
        RpcContext(conn_id="contract", config=_synthetic_config(tmp_path))
    )

    assert payload["legacyData"] is None


async def test_legacy_data_block_stays_null_when_a_candidate_exists(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "opensquilla.migration.legacy_detect.detect_legacy_home",
        lambda target=None: (_ for _ in ()).throw(AssertionError("unexpected scan")),
    )

    payload = _status_payload(
        RpcContext(conn_id="contract", config=_synthetic_config(tmp_path))
    )

    assert payload["legacyData"] is None
