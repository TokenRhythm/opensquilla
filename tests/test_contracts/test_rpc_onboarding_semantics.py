"""Wire-contract pins for onboarding mutation RPC semantics.

The CLI onboarding hardening pass widened several mutation signatures to
``None`` = keep-current and made re-saves state-dependent. Those semantics
are reachable from the unmodified gateway RPC handlers
(``onboarding.provider.configure``, ``onboarding.router.configure``,
``onboarding.search.configure``, ``onboarding.channel.probe``/``upsert``),
so this module is the explicit sign-off: each test pins one wire-visible
behavior so any future change to it is a conscious contract decision.

Pinned here:

- **Keep-current re-saves** (deliberate change): a same-provider re-save
  carries over stored ``provider_routing``/``max_tokens``; a blank
  ``apiKey`` keeps a required-provider key, while optional-provider keys are
  preserved only with ``preserveApiKey=true``; and an operator-authored
  inline router ladder survives a provider save.
  ``onboarding.router.configure`` with ``mode=disabled`` keeps the effective
  ladder stored inline for re-enable.
- **Explicit JSON null = legacy default** (compatibility): a client sending
  ``null`` for ``model``/``proxy``/``maxResults``/... gets the pre-widening
  reset/derive behavior, not keep-current.
- **Blank required channel secrets hard-fail** (deliberate change): probe
  and upsert reject a genuinely blank secret; with a stored entry both are
  merge-aware, so blank-means-keep round-trips.

All configs are synthetic; no network or credentials involved.
"""

from __future__ import annotations

import tomllib

import pytest

import opensquilla.gateway.rpc_onboarding  # noqa: F401  ensures registration
from opensquilla.gateway.auth import Principal
from opensquilla.gateway.rpc import RpcContext, get_dispatcher


def _admin_ctx() -> RpcContext:
    return RpcContext(
        conn_id="contract",
        principal=Principal(
            role="operator",
            scopes=frozenset({"operator.admin"}),
            is_owner=True,
            authenticated=True,
        ),
    )


async def _dispatch(method: str, params: dict):
    return await get_dispatcher().dispatch("r1", method, params, _admin_ctx())


@pytest.fixture()
def config_file(tmp_path, monkeypatch):
    target = tmp_path / "c.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))
    return target


# ---------------------------------------------------------------------------
# Keep-current re-save semantics (deliberate wire-visible change).
# ---------------------------------------------------------------------------


async def test_provider_resave_keeps_stored_provider_routing_and_max_tokens(
    config_file,
):
    config_file.write_text(
        "[llm]\n"
        'provider = "openrouter"\n'
        'model = "custom/model-x"\n'
        'api_key = "sk-old"\n'
        "max_tokens = 4096\n"
        "[llm.provider_routing]\n"
        '"custom/model-x" = "custom-upstream"\n',
        encoding="utf-8",
    )

    res = await _dispatch(
        "onboarding.provider.configure",
        {"providerId": "openrouter", "model": "custom/model-x", "apiKey": "sk-new"},
    )

    assert res.error is None, res.error
    # providerRouting was never sent: the stored table is carried over
    # (legacy behavior reset it to {}); max_tokens rides the stored section.
    assert res.payload["entry"]["provider_routing"] == {
        "custom/model-x": "custom-upstream"
    }
    data = tomllib.loads(config_file.read_text())
    assert data["llm"]["provider_routing"] == {"custom/model-x": "custom-upstream"}
    assert data["llm"]["max_tokens"] == 4096


async def test_provider_resave_blank_api_key_keeps_stored_key(config_file):
    config_file.write_text(
        '[llm]\nprovider = "openrouter"\nmodel = "custom/model-x"\napi_key = "sk-stored"\n',
        encoding="utf-8",
    )

    res = await _dispatch(
        "onboarding.provider.configure",
        {"providerId": "openrouter", "model": "custom/model-x", "apiKey": ""},
    )

    # Legacy behavior raised "requires an api_key"; blank now keeps stored.
    assert res.error is None, res.error
    data = tomllib.loads(config_file.read_text())
    assert data["llm"]["api_key"] == "sk-stored"


async def test_optional_provider_blank_api_key_keeps_legacy_clear_default(
    config_file,
):
    config_file.write_text(
        "[llm]\n"
        'provider = "custom"\n'
        'model = "custom-model"\n'
        'api_key = "sk-stored"\n'
        'base_url = "https://llm.example.test/v1"\n',
        encoding="utf-8",
    )

    res = await _dispatch(
        "onboarding.provider.configure",
        {
            "providerId": "custom",
            "model": "custom-model",
            "apiKey": "",
            "baseUrl": "https://llm.example.test/v1",
        },
    )

    assert res.error is None, res.error
    data = tomllib.loads(config_file.read_text())
    assert data["llm"].get("api_key", "") == ""


async def test_optional_provider_preserve_api_key_opt_in_keeps_stored_key(
    config_file,
):
    config_file.write_text(
        "[llm]\n"
        'provider = "custom"\n'
        'model = "custom-model"\n'
        'api_key = "sk-stored"\n'
        'base_url = "https://llm.example.test/v1"\n',
        encoding="utf-8",
    )

    res = await _dispatch(
        "onboarding.provider.configure",
        {
            "providerId": "custom",
            "model": "custom-model",
            "preserveApiKey": True,
            "baseUrl": "https://llm.example.test:443/v2",
        },
    )

    assert res.error is None, res.error
    data = tomllib.loads(config_file.read_text())
    assert data["llm"]["api_key"] == "sk-stored"
    assert data["llm"]["base_url"] == "https://llm.example.test:443/v2"


async def test_optional_provider_preserve_treats_whitespace_env_as_blank(
    config_file,
):
    config_file.write_text(
        "[llm]\n"
        'provider = "custom"\n'
        'model = "custom-model"\n'
        'api_key = "sk-stored"\n'
        'base_url = "https://llm.example.test/v1"\n',
        encoding="utf-8",
    )

    res = await _dispatch(
        "onboarding.provider.configure",
        {
            "providerId": "custom",
            "model": "custom-model",
            "apiKeyEnv": "   ",
            "preserveApiKey": True,
            "baseUrl": "https://llm.example.test/v2",
        },
    )

    assert res.error is None, res.error
    data = tomllib.loads(config_file.read_text())
    assert data["llm"]["api_key"] == "sk-stored"
    assert data["llm"].get("api_key_env", "") == ""


async def test_optional_provider_preserve_api_key_rejects_cross_origin_endpoint(
    config_file,
):
    config_file.write_text(
        "[llm]\n"
        'provider = "custom"\n'
        'model = "custom-model"\n'
        'api_key = "sk-origin-a"\n'
        'base_url = "https://a.example.test/v1"\n',
        encoding="utf-8",
    )

    res = await _dispatch(
        "onboarding.provider.configure",
        {
            "providerId": "custom",
            "model": "custom-model",
            "preserveApiKey": True,
            "baseUrl": "https://b.example.test/v1",
        },
    )

    assert res.error is None, res.error
    data = tomllib.loads(config_file.read_text())
    assert data["llm"].get("api_key", "") == ""
    assert data["llm"]["base_url"] == "https://b.example.test/v1"


async def test_optional_provider_env_reference_does_not_cross_origin(config_file):
    config_file.write_text(
        "[llm]\n"
        'provider = "custom"\n'
        'model = "custom-model"\n'
        'api_key_env = "CUSTOM_ORIGIN_A_KEY"\n'
        'base_url = "https://a.example.test/v1"\n',
        encoding="utf-8",
    )

    res = await _dispatch(
        "onboarding.provider.configure",
        {
            "providerId": "custom",
            "model": "custom-model",
            "baseUrl": "https://b.example.test/v1",
        },
    )

    assert res.error is None, res.error
    data = tomllib.loads(config_file.read_text())
    assert data["llm"].get("api_key_env", "") == ""
    assert data["llm"]["base_url"] == "https://b.example.test/v1"


async def test_provider_configure_rejects_non_boolean_preserve_api_key(
    config_file,
):
    config_file.write_text(
        "[llm]\n"
        'provider = "custom"\n'
        'model = "custom-model"\n'
        'api_key = "sk-stored"\n'
        'base_url = "https://llm.example.test/v1"\n',
        encoding="utf-8",
    )

    res = await _dispatch(
        "onboarding.provider.configure",
        {
            "providerId": "custom",
            "model": "custom-model",
            "preserveApiKey": "true",
            "baseUrl": "https://llm.example.test/v1",
        },
    )

    assert res.error is not None
    assert res.error.code == "onboarding.provider.invalid"
    assert "preserveApiKey must be a boolean" in res.error.message


async def test_provider_resave_keeps_operator_authored_router_ladder(config_file):
    config_file.write_text(
        "[llm]\n"
        'provider = "openrouter"\n'
        'model = "custom/model-x"\n'
        'api_key = "sk-old"\n'
        "[squilla_router]\n"
        "enabled = true\n"
        "[squilla_router.tiers.c0]\n"
        'provider = "openrouter"\n'
        'model = "custom/cheap"\n'
        "[squilla_router.tiers.c1]\n"
        'provider = "openrouter"\n'
        'model = "custom/model-x"\n'
        "[squilla_router.tiers.c2]\n"
        'provider = "openrouter"\n'
        'model = "custom/mid"\n'
        "[squilla_router.tiers.c3]\n"
        'provider = "openrouter"\n'
        'model = "custom/big"\n',
        encoding="utf-8",
    )

    res = await _dispatch(
        "onboarding.provider.configure",
        {"providerId": "openrouter", "model": "custom/model-x", "apiKey": "sk-new"},
    )

    assert res.error is None, res.error
    data = tomllib.loads(config_file.read_text())
    # Legacy behavior reverted the ladder to the packaged openrouter profile.
    assert data["squilla_router"]["tiers"]["c0"]["model"] == "custom/cheap"
    assert data["squilla_router"]["tiers"]["c3"]["model"] == "custom/big"
    assert "tier_profile" not in data["squilla_router"]


async def test_router_disable_keeps_effective_ladder_inline(config_file):
    config_file.write_text(
        "[llm]\n"
        'provider = "openrouter"\n'
        'model = "custom/model-x"\n'
        'api_key = "sk"\n'
        "[squilla_router]\n"
        "enabled = true\n"
        "[squilla_router.tiers.c0]\n"
        'provider = "openrouter"\n'
        'model = "custom/cheap"\n'
        "[squilla_router.tiers.c1]\n"
        'provider = "openrouter"\n'
        'model = "custom/model-x"\n'
        "[squilla_router.tiers.c2]\n"
        'provider = "openrouter"\n'
        'model = "custom/mid"\n'
        "[squilla_router.tiers.c3]\n"
        'provider = "openrouter"\n'
        'model = "custom/big"\n',
        encoding="utf-8",
    )

    res = await _dispatch("onboarding.router.configure", {"mode": "disabled"})

    assert res.error is None, res.error
    assert res.payload["entry"]["mode"] == "disabled"
    data = tomllib.loads(config_file.read_text())
    assert data["squilla_router"]["enabled"] is False
    # The operator's ladder stays stored inline so a re-enable can restore
    # it (legacy behavior reset it to the packaged profile).
    assert data["squilla_router"]["tiers"]["c0"]["model"] == "custom/cheap"
    assert data["squilla_router"]["tiers"]["c3"]["model"] == "custom/big"


# ---------------------------------------------------------------------------
# Explicit JSON null keeps the LEGACY defaults (compatibility pin).
# ---------------------------------------------------------------------------


async def test_provider_configure_null_model_resets_to_derived_default(config_file):
    config_file.write_text(
        '[llm]\nprovider = "deepseek"\nmodel = "custom-stored-model"\napi_key = "sk-old"\n',
        encoding="utf-8",
    )

    res = await _dispatch(
        "onboarding.provider.configure",
        {"providerId": "deepseek", "model": None, "apiKey": "sk-new"},
    )

    assert res.error is None, res.error
    # null must behave like the legacy empty string: derive the router
    # profile default, NOT keep the stored custom model.
    assert res.payload["entry"]["model"] != "custom-stored-model"
    assert res.payload["entry"]["model"]


async def test_search_configure_null_params_reset_to_legacy_defaults(config_file):
    from opensquilla.search.types import DEFAULT_SEARCH_MAX_RESULTS

    config_file.write_text(
        'search_provider = "duckduckgo"\n'
        "search_max_results = 9\n"
        'search_proxy = "http://127.0.0.1:7890"\n'
        'search_fallback_policy = "network"\n'
        "search_diagnostics = true\n",
        encoding="utf-8",
    )

    res = await _dispatch(
        "onboarding.search.configure",
        {
            "providerId": "duckduckgo",
            "maxResults": None,
            "proxy": None,
            "useEnvProxy": None,
            "fallbackPolicy": None,
            "diagnostics": None,
        },
    )

    assert res.error is None, res.error
    entry = res.payload["entry"]
    assert entry["max_results"] == DEFAULT_SEARCH_MAX_RESULTS
    assert entry["proxy"] == ""
    assert entry["fallback_policy"] == "off"
    assert entry["diagnostics"] is False


async def test_search_configure_absent_params_also_reset_to_legacy_defaults(
    config_file,
):
    """Over RPC, ABSENT optional search params keep the legacy reset
    semantics (the keep-current widening is CLI-only); pinned so the two
    surfaces cannot drift apart silently."""
    from opensquilla.search.types import DEFAULT_SEARCH_MAX_RESULTS

    config_file.write_text(
        'search_provider = "duckduckgo"\nsearch_max_results = 9\n', encoding="utf-8"
    )

    res = await _dispatch(
        "onboarding.search.configure", {"providerId": "duckduckgo"}
    )

    assert res.error is None, res.error
    assert res.payload["entry"]["max_results"] == DEFAULT_SEARCH_MAX_RESULTS


# ---------------------------------------------------------------------------
# Blank required channel secrets: hard-fail without a stored entry,
# merge-aware with one (probe mirrors upsert).
# ---------------------------------------------------------------------------


async def test_channel_probe_blank_secret_without_stored_entry_fails(config_file):
    res = await _dispatch(
        "onboarding.channel.probe",
        {"entry": {"type": "telegram", "name": "t1", "token": ""}},
    )

    assert res.error is not None
    assert res.error.code == "onboarding.channel.invalid"
    assert "token" in res.error.message


async def test_channel_upsert_blank_secret_without_stored_entry_fails(config_file):
    res = await _dispatch(
        "onboarding.channel.upsert",
        {"entry": {"type": "telegram", "name": "t1", "token": ""}},
    )

    assert res.error is not None
    assert res.error.code == "onboarding.channel.invalid"


async def test_channel_probe_blank_secret_merges_stored_entry(config_file):
    config_file.write_text(
        "[[channels.channels]]\n"
        'type = "telegram"\n'
        'name = "t1"\n'
        'token = "tg-stored"\n',
        encoding="utf-8",
    )

    res = await _dispatch(
        "onboarding.channel.probe",
        {"entry": {"type": "telegram", "name": "t1", "token": ""}},
    )

    assert res.error is None, res.error
    assert res.payload["status"] == "validated"
    assert res.payload["probeKind"] == "local_validation"
    assert res.payload["connected"] is False
    assert "no provider connection" in res.payload["warnings"][0].lower()
    # Secrets never round-trip in the probe response.
    assert res.payload["entry"]["token"] != "tg-stored"


# ---------------------------------------------------------------------------
# Round-tripped '***' redaction masks are keep-current server-side: a
# read-modify-write client echoing a redacted apiKey must never overwrite
# (or probe with) the stored credential. Mirrors the channel-secret merge.
# ---------------------------------------------------------------------------


async def test_provider_configure_round_tripped_mask_keeps_stored_key(config_file):
    config_file.write_text(
        '[llm]\nprovider = "openrouter"\nmodel = "custom/model-x"\napi_key = "sk-stored"\n',
        encoding="utf-8",
    )

    res = await _dispatch(
        "onboarding.provider.configure",
        {"providerId": "openrouter", "model": "custom/model-x", "apiKey": "***"},
    )

    assert res.error is None, res.error
    data = tomllib.loads(config_file.read_text())
    assert data["llm"]["api_key"] == "sk-stored"


async def test_optional_provider_all_asterisk_mask_keeps_stored_key(config_file):
    """Optional providers clear on blank, but a mask always means keep —
    including wider all-asterisk echoes from status-style surfaces."""
    config_file.write_text(
        "[llm]\n"
        'provider = "custom"\n'
        'model = "custom-model"\n'
        'api_key = "sk-stored"\n'
        'base_url = "https://llm.example.test/v1"\n',
        encoding="utf-8",
    )

    res = await _dispatch(
        "onboarding.provider.configure",
        {
            "providerId": "custom",
            "model": "custom-model",
            "apiKey": "********",
            "baseUrl": "https://llm.example.test/v1",
        },
    )

    assert res.error is None, res.error
    data = tomllib.loads(config_file.read_text())
    assert data["llm"]["api_key"] == "sk-stored"


async def test_provider_configure_mask_without_stored_key_is_typed_error(
    config_file,
):
    res = await _dispatch(
        "onboarding.provider.configure",
        {"providerId": "openrouter", "model": "custom/model-x", "apiKey": "***"},
    )

    # No stored key to keep: the mask must produce the missing-key validation
    # error, never persist as the literal credential.
    assert res.error is not None
    assert res.error.code == "onboarding.provider.invalid"
    assert "api_key" in res.error.message
    assert not config_file.exists() or "***" not in config_file.read_text()


async def test_llm_profile_upsert_round_tripped_mask_keeps_stored_key(config_file):
    config_file.write_text(
        "[llm]\n"
        'provider = "openrouter"\n'
        'model = "custom/model-x"\n'
        'api_key = "sk-primary"\n'
        "[llm_profiles.deepseek]\n"
        'model = "deepseek-chat"\n'
        'api_key = "sk-profile-stored"\n',
        encoding="utf-8",
    )

    res = await _dispatch(
        "onboarding.llmProfile.upsert",
        {"providerId": "deepseek", "apiKey": "***"},
    )

    assert res.error is None, res.error
    assert res.payload["entry"]["api_key"] == "***"  # redacted echo, not the key
    data = tomllib.loads(config_file.read_text())
    assert data["llm_profiles"]["deepseek"]["api_key"] == "sk-profile-stored"


async def test_llm_profile_draft_resolves_stored_key_for_masked_payload(config_file):
    """The draft-probe path builds its config here: a masked apiKey must
    resolve to the stored credential, not a literal '***' bearer token."""
    from opensquilla.gateway.rpc_onboarding import _draft_llm_profile_config

    config_file.write_text(
        "[llm]\n"
        'provider = "openrouter"\n'
        'model = "custom/model-x"\n'
        'api_key = "sk-primary"\n'
        "[llm_profiles.deepseek]\n"
        'model = "deepseek-chat"\n'
        'api_key = "sk-profile-stored"\n',
        encoding="utf-8",
    )

    provider, draft = _draft_llm_profile_config(
        {"providerId": "deepseek", "apiKey": "***"}, _admin_ctx()
    )

    assert provider == "deepseek"
    assert draft.llm_profiles["deepseek"].api_key == "sk-profile-stored"


async def test_provider_probe_mask_is_not_sent_as_bearer_credential(
    config_file, monkeypatch
):
    """A masked probe without a stored key degrades to the typed missing-key
    result without ever building a provider client."""
    import opensquilla.onboarding.probe as probe_mod

    def _fail_build(*args, **kwargs):
        raise AssertionError("probe must not reach the network with a masked key")

    monkeypatch.setattr(probe_mod, "build_provider", _fail_build)

    res = await _dispatch(
        "onboarding.provider.probe",
        {"providerId": "openrouter", "model": "custom/model-x", "apiKey": "***"},
    )

    assert res.error is None, res.error
    assert res.payload["ok"] is False
    assert "No API key available" in res.payload["message"]


async def test_search_configure_round_tripped_mask_keeps_stored_key(config_file):
    config_file.write_text(
        'search_provider = "brave"\nsearch_api_key = "sk-search-stored"\n',
        encoding="utf-8",
    )

    res = await _dispatch(
        "onboarding.search.configure", {"providerId": "brave", "apiKey": "***"}
    )

    assert res.error is None, res.error
    data = tomllib.loads(config_file.read_text())
    assert data["search_api_key"] == "sk-search-stored"


async def test_image_generation_configure_mask_keeps_stored_key(config_file):
    config_file.write_text(
        "[image_generation.providers.openai]\n"
        'api_key = "sk-img-stored"\n',
        encoding="utf-8",
    )

    res = await _dispatch(
        "onboarding.imageGeneration.configure",
        {"providerId": "openai", "apiKey": "***"},
    )

    assert res.error is None, res.error
    data = tomllib.loads(config_file.read_text())
    assert (
        data["image_generation"]["providers"]["openai"]["api_key"] == "sk-img-stored"
    )


async def test_audio_configure_mask_keeps_stored_key(config_file):
    config_file.write_text(
        "[audio.providers.elevenlabs]\n"
        'api_key = "sk-audio-stored"\n',
        encoding="utf-8",
    )

    res = await _dispatch(
        "onboarding.audio.configure", {"providerId": "elevenlabs", "apiKey": "***"}
    )

    assert res.error is None, res.error
    data = tomllib.loads(config_file.read_text())
    assert data["audio"]["providers"]["elevenlabs"]["api_key"] == "sk-audio-stored"


async def test_memory_embedding_configure_mask_keeps_stored_key(config_file):
    config_file.write_text(
        "[memory.embedding]\n"
        'provider = "openai"\n'
        "[memory.embedding.remote]\n"
        'api_key = "sk-embed-stored"\n',
        encoding="utf-8",
    )

    res = await _dispatch(
        "onboarding.memory_embedding.configure",
        {"providerId": "openai", "apiKey": "***"},
    )

    assert res.error is None, res.error
    data = tomllib.loads(config_file.read_text())
    assert data["memory"]["embedding"]["remote"]["api_key"] == "sk-embed-stored"
